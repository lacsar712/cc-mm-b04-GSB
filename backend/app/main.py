from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import DateTime, Float, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.rules import classify

DEFAULT_LIMIT_PER_HOUR = 3


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://app:app@localhost:54391/methane"
    jwt_secret: str = "mine-methane-dev-secret"


settings = Settings()
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)
USERS = {
    "gasman": {"role": "writer", "password_hash": pwd.hash("gas123456")},
    "viewer": {"role": "reader", "password_hash": pwd.hash("view123456")},
}

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


class Reading(Base):
    __tablename__ = "readings"
    id: Mapped[int] = mapped_column(primary_key=True)
    site: Mapped[str] = mapped_column(String(80))
    ch4_pct: Mapped[float] = mapped_column(Float)
    level: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # seed 为开机演示数据，不计入每小时上报次数
    origin: Mapped[str] = mapped_column(String(10), default="report")


class LimitSetting(Base):
    __tablename__ = "limit_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    limit_per_hour: Mapped[int] = mapped_column(Integer)


class OverLimitAttempt(Base):
    __tablename__ = "over_limit_attempts"
    id: Mapped[int] = mapped_column(primary_key=True)
    site: Mapped[str] = mapped_column(String(80))
    operator: Mapped[str] = mapped_column(String(64))
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    limit_value: Mapped[int] = mapped_column(Integer)


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float


class LimitIn(BaseModel):
    limit_per_hour: int = Field(ge=1, le=999)


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = jwt.decode(credentials.credentials, settings.jwt_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="无效令牌") from exc
    username = payload.get("sub")
    if username not in USERS:
        raise HTTPException(status_code=401, detail="无效令牌")
    return {"username": username, "role": payload.get("role")}


def require_writer(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "writer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅瓦斯检查员可操作")
    return user


def get_limit(db: Session) -> int:
    row = db.query(LimitSetting).filter(LimitSetting.id == 1).one()
    return row.limit_per_hour


sockets: set[WebSocket] = set()
app = FastAPI(title="矿井瓦斯班测台")


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(LimitSetting).filter(LimitSetting.id == 1).count() == 0:
            db.add(LimitSetting(id=1, limit_per_hour=DEFAULT_LIMIT_PER_HOUR))
        if db.query(Reading).count() == 0:
            now = datetime.now(timezone.utc)
            for site, ch4 in (("东翼-12", 0.35), ("回风巷", 1.4)):
                level, note = classify(ch4)
                db.add(
                    Reading(
                        site=site,
                        ch4_pct=ch4,
                        level=level,
                        note=note,
                        created_by="gasman",
                        created_at=now,
                        origin="seed",
                    )
                )
        db.commit()
    finally:
        db.close()


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "mine-methane-shift"}


@app.post("/api/auth/login")
def login(body: LoginIn):
    user = USERS.get(body.username.strip())
    if not user or not pwd.verify(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    token = jwt.encode(
        {"sub": body.username.strip(), "role": user["role"], "exp": exp},
        settings.jwt_secret,
        algorithm="HS256",
    )
    return {"access_token": token, "username": body.username.strip(), "role": user["role"]}


@app.get("/api/readings")
def list_readings(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(Reading).order_by(Reading.id.desc()).all()
        return [
            {
                "id": r.id,
                "site": r.site,
                "ch4_pct": r.ch4_pct,
                "level": r.level,
                "note": r.note,
                "created_by": r.created_by,
            }
            for r in rows
        ]
    finally:
        db.close()


@app.get("/api/limit")
def read_limit(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        return {"limit_per_hour": get_limit(db)}
    finally:
        db.close()


@app.put("/api/limit")
def update_limit(body: LimitIn, _user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        row = db.query(LimitSetting).filter(LimitSetting.id == 1).with_for_update().one()
        row.limit_per_hour = body.limit_per_hour
        db.commit()
        return {"limit_per_hour": row.limit_per_hour}
    finally:
        db.close()


@app.get("/api/over-limit-attempts")
def list_over_limit_attempts(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(OverLimitAttempt).order_by(OverLimitAttempt.id.desc()).all()
        return [
            {
                "id": r.id,
                "site": r.site,
                "operator": r.operator,
                "attempted_at": r.attempted_at.isoformat(),
                "limit_value": r.limit_value,
            }
            for r in rows
        ]
    finally:
        db.close()


@app.post("/api/readings", status_code=201)
async def create_reading(body: ReadingIn, user: dict = Depends(require_writer)):
    level, note = classify(body.ch4_pct)
    site = body.site.strip()
    now = datetime.now(timezone.utc)
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    hour_end = hour_start + timedelta(hours=1)

    db = SessionLocal()
    try:
        # 锁住门槛行，把"计数-判定-写入"收在同一事务里
        setting = db.query(LimitSetting).filter(LimitSetting.id == 1).with_for_update().one()
        limit = setting.limit_per_hour
        used = (
            db.query(Reading)
            .filter(
                Reading.site == site,
                Reading.origin == "report",
                Reading.created_at >= hour_start,
                Reading.created_at < hour_end,
            )
            .count()
        )
        if used >= limit:
            db.add(
                OverLimitAttempt(
                    site=site,
                    operator=user["username"],
                    attempted_at=now,
                    limit_value=limit,
                )
            )
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"测点 {site} 本小时已上报 {used} 次，达到上限 {limit} 次，已记入超次尝试册",
            )
        row = Reading(
            site=site,
            ch4_pct=body.ch4_pct,
            level=level,
            note=note,
            created_by=user["username"],
            created_at=now,
            origin="report",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        payload = {"id": row.id, "site": row.site, "ch4_pct": row.ch4_pct, "level": row.level, "note": row.note}
    finally:
        db.close()
    dead = []
    for ws in list(sockets):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.discard(ws)
    return payload


@app.websocket("/ws/alerts")
async def alerts(ws: WebSocket):
    await ws.accept()
    sockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        sockets.discard(ws)
