def classify(ch4_pct: float) -> tuple[str, str]:
    if ch4_pct >= 1.0:
        return "报警", "甲烷达到报警线"
    return "正常", "甲烷低于报警线"
