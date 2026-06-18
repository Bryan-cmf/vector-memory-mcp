#!/usr/bin/env python3
"""隱私/合規層 — 採集時自動 redact 敏感資料 + privacy_score 評估。

可 redact 的模式 (可配置 ~/.vector-memory-mcp/privacy.yml):
- API keys: sk-xxx, gho-xxx, Bearer xxx, api_key=xxx
- 信用卡號: 4-4-4-4 數字格式
- password=xxx, passwd=xxx
- (可選) email: 預設不 redact

每筆記錄加 privacy_score (0.0 完全乾淨 → 1.0 高敏感)。
privacy_score = redacted_count / content_length_ratio

Usage (獨立測試):
    python privacy.py "content with sk-1234567890"
    → 輸出 redact 後 content + score

整合: collect.py 在 upsert 前呼叫 redact_content()。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import NamedTuple

# 預設規則 (可用 privacy.yml 覆蓋)
DEFAULT_RULES = {
    "redact_api_keys": True,
    "redact_credit_cards": True,
    "redact_passwords": True,
    "redact_emails": False,           # 預設不 redact (用戶可開)
    "redact_phones": False,           # 預設不 redact
}

# 正則模式 (順序很重要:長的先比對避免截斷)
PATTERNS = {
    # API keys (各種 prefix)
    "api_keys": [
        (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "[REDACTED:api_key]"),
        (re.compile(r"\bgho_[A-Za-z0-9]{20,}"), "[REDACTED:github_token]"),
        (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "[REDACTED:github_pat]"),
        (re.compile(r"\bBearer\s+[A-Za-z0-9\._\-]{20,}"), "Bearer [REDACTED:token]"),
        (re.compile(r"\bapi[_-]?key\s*[=:]\s*['\"]?[A-Za-z0-9]{16,}['\"]?", re.IGNORECASE), "api_key=[REDACTED]"),
        (re.compile(r"\bxox[bpoa]-[A-Za-z0-9-]{10,}"), "[REDACTED:slack_token]"),   # Slack
        (re.compile(r"\bAIza[A-Za-z0-9_\-]{35}"), "[REDACTED:google_api_key]"),
    ],
    # 信用卡號 (4-4-4-4)
    "credit_cards": [
        (re.compile(r"\b(?:\d[ -]?){13,16}\b"), "[REDACTED:credit_card]"),
    ],
    # password = xxx / passwd: xxx
    "passwords": [
        (re.compile(r"\b(?:password|passwd|pwd)\s*[=:]\s*['\"]?[^\s'\";,]{4,}['\"]?", re.IGNORECASE),
         "password=[REDACTED]"),
    ],
    # email (可選)
    "emails": [
        (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "[REDACTED:email]"),
    ],
    # 電話 (可選,台灣/大陸格式)
    "phones": [
        (re.compile(r"\b(?:\+?886[-\s]?|0)\d[\-\s]?\d{3,4}[\-\s]?\d{3,4}\b"), "[REDACTED:phone]"),
    ],
}

# 對應 rules 旗標 → pattern group
RULE_TO_GROUP = {
    "redact_api_keys": "api_keys",
    "redact_credit_cards": "credit_cards",
    "redact_passwords": "passwords",
    "redact_emails": "emails",
    "redact_phones": "phones",
}


class RedactResult(NamedTuple):
    content: str
    redactions: int
    score: float       # 0.0–1.0,越高越敏感


def load_rules() -> dict:
    """載入 privacy.yml (若有),否則用 DEFAULT_RULES。"""
    yml = Path(os.environ.get("VECTOR_MEMORY_DIR", str(Path.home() / ".vector-memory-mcp"))) / "privacy.yml"
    if not yml.exists():
        return dict(DEFAULT_RULES)
    try:
        # 簡易 YAML 解析 (不依賴 PyYAML,只支援 key: value)
        rules = dict(DEFAULT_RULES)
        for line in yml.read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip().lower()
            if k in DEFAULT_RULES:
                rules[k] = v in ("true", "yes", "1", "on")
        return rules
    except Exception:
        return dict(DEFAULT_RULES)


def redact_content(content: str, rules: dict | None = None) -> RedactResult:
    """對 content 套用 redaction 規則。

    回傳 (redacted_content, redaction_count, privacy_score)。
    privacy_score = min(1.0, redactions / max(1, len(content)/200))
        含義: 每 200 字 1 個 redaction 算 score=1.0
    """
    if rules is None:
        rules = load_rules()

    if not content:
        return RedactResult(content, 0, 0.0)

    original_len = len(content)
    total_redactions = 0

    for rule_key, group_name in RULE_TO_GROUP.items():
        if not rules.get(rule_key, False):
            continue
        patterns = PATTERNS.get(group_name, [])
        for pat, replacement in patterns:
            new_content, n = pat.subn(replacement, content)
            if n > 0:
                content = new_content
                total_redactions += n

    # score: redactions 密度 (每 200 字 1 個 = 滿分)
    score = min(1.0, total_redactions / max(1.0, original_len / 200.0))
    return RedactResult(content, total_redactions, round(score, 3))


def ensure_privacy_config():
    """確保 privacy.yml 存在 (若無則建預設)。"""
    yml = Path(os.environ.get("VECTOR_MEMORY_DIR", str(Path.home() / ".vector-memory-mcp"))) / "privacy.yml"
    if yml.exists():
        return yml
    yml.parent.mkdir(parents=True, exist_ok=True)
    yml.write_text("""# vector-memory-hub 隱私設定
# 採集時自動 redact 這些模式 (true/false)

redact_api_keys: true       # sk-xxx, gho_xxx, Bearer xxx, api_key=xxx
redact_credit_cards: true   # 4-4-4-4 格式
redact_passwords: true      # password=xxx
redact_emails: false        # 預設不 redact (用戶可開)
redact_phones: false        # 預設不 redact
""")
    return yml


if __name__ == "__main__":
    # 獨立測試: python privacy.py "content..."
    ensure_privacy_config()
    text = sys.argv[1] if len(sys.argv) > 1 else """
    測試 redaction:
    API key: sk-1234567890abcdefghijklmnop
    GitHub: gho_abcdefghijklmnopqrstuvwxyz123456
    信用卡: 4111 1111 1111 1111
    password=secret123
    email: user@example.com (預設不 redact)
    """
    rules = load_rules()
    print(f"規則: {rules}\n")
    result = redact_content(text, rules)
    print(f"redactions: {result.redactions}")
    print(f"privacy_score: {result.score}")
    print(f"\n--- redacted ---\n{result.content}")
