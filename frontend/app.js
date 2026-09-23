const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";

const loginBox = document.querySelector("#login");
const appBox = document.querySelector("#app");
const rows = document.querySelector("#rows");
const attemptsRows = document.querySelector("#attempts");
const live = document.querySelector("#live");
const form = document.querySelector("#form");
const limitValue = document.querySelector("#limitValue");
const limitEdit = document.querySelector("#limitEdit");
const limitInput = document.querySelector("#limitInput");
const limitMsg = document.querySelector("#limitMsg");

function fmtTime(iso) {
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString();
}

function paint(list) {
  rows.innerHTML = list
    .map(
      (r) =>
        `<tr><td>${r.site}</td><td>${r.ch4_pct}</td><td class="${r.level === "报警" ? "alarm" : "ok"}">${r.level}</td><td>${r.note}</td></tr>`,
    )
    .join("");
}

function paintAttempts(list) {
  attemptsRows.innerHTML = list
    .map(
      (a) =>
        `<tr><td>${a.site}</td><td>${a.operator}</td><td>${fmtTime(a.attempted_at)}</td><td>${a.limit_value}</td></tr>`,
    )
    .join("");
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.detail || "请求失败");
    err.status = res.status;
    throw err;
  }
  return data;
}

function showApp() {
  loginBox.hidden = true;
  appBox.hidden = false;
  document.querySelector("#who").textContent = role === "writer" ? "检查员" : "查看";
  document.querySelector("#out").hidden = false;
  // 旁观账号：无上报表单，也不能改门槛
  form.hidden = role !== "writer";
  limitEdit.hidden = role !== "writer";
  connect();
  load();
  loadLimit();
  loadAttempts();
}

async function load() {
  paint(await api("/api/readings"));
}

async function loadLimit() {
  const data = await api("/api/limit");
  limitValue.textContent = data.limit_per_hour;
  limitInput.value = data.limit_per_hour;
}

async function loadAttempts() {
  paintAttempts(await api("/api/over-limit-attempts"));
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = (ev) => {
    const row = JSON.parse(ev.data);
    live.textContent = `刚推送：${row.site} ${row.level}`;
    load();
  };
}

document.querySelector("#go").onclick = async () => {
  const data = await api("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({
      username: document.querySelector("#user").value,
      password: document.querySelector("#pass").value,
    }),
  });
  token = data.access_token;
  role = data.role;
  localStorage.setItem(tokenKey, token);
  localStorage.setItem("methane_role", role);
  showApp();
};

form.onsubmit = async (e) => {
  e.preventDefault();
  limitMsg.textContent = "";
  try {
    await api("/api/readings", {
      method: "POST",
      body: JSON.stringify({
        site: document.querySelector("#site").value,
        ch4_pct: Number(document.querySelector("#ch4").value),
      }),
    });
  } catch (err) {
    live.textContent = err.message;
    // 被拒（超次）后刷新尝试册，让新记录立刻可见
    if (err.status === 429) loadAttempts();
  }
};

document.querySelector("#limitSave").onclick = async () => {
  limitMsg.textContent = "";
  try {
    const data = await api("/api/limit", {
      method: "PUT",
      body: JSON.stringify({ limit_per_hour: Number(limitInput.value) }),
    });
    limitValue.textContent = data.limit_per_hour;
    limitInput.value = data.limit_per_hour;
    limitMsg.textContent = "门槛已更新，只影响之后的上报";
  } catch (err) {
    limitMsg.textContent = err.message;
  }
};

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

if (token) showApp();
