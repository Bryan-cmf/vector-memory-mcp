"""CursorConnector — 採集 Cursor 的對話歷史 (state.vscdb SQLite)。

Cursor 把 AI 對話存在 workspaceStorage/*/state.vscdb 的 ItemTable 裡,
key 通常是 composer.composerData / aiService.generations 等,值是 JSON 字串。

最佳努力解析: schema 可能因 Cursor 版本而異,失敗則降級為日誌記錄。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterator

from connectors.base import Record, now_iso

CURSOR_ROOT = Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "workspaceStorage"
# 偵測到可能含對話的 key 前綴
CHAT_KEY_PATTERNS = ("composer", "aiService", "chat", "conversation")
MAX_VALUE_SIZE = 50_000  # 超過就截斷


class CursorConnector:
    """採集 Cursor workspace 對話 (SQLite state.vscdb)。"""

    name = "cursor"

    def __init__(self, state: dict | None = None):
        self.state = state or {}

    def is_available(self) -> bool:
        return CURSOR_ROOT.exists() and any(self._find_dbs())

    def _find_dbs(self) -> Iterator[Path]:
        if not CURSOR_ROOT.exists():
            return
        for ws in CURSOR_ROOT.iterdir():
            if not ws.is_dir():
                continue
            db = ws / "state.vscdb"
            if db.exists() and db.stat().st_size > 0:
                yield db

    def discover(self) -> int:
        count = 0
        for db in self._find_dbs():
            try:
                with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
                    cur = conn.execute(
                        "SELECT COUNT(*) FROM ItemTable WHERE "
                        + " OR ".join([f"key LIKE '%{p}%'" for p in CHAT_KEY_PATTERNS])
                    )
                    count += cur.fetchone()[0]
            except sqlite3.Error:
                continue
        return count

    def collect(self) -> Iterator[Record]:
        for db in self._find_dbs():
            ws_name = db.parent.name
            try:
                records = list(self._collect_from_db(db, ws_name))
                yield from records
            except Exception as e:
                # 容錯: 單一 DB 失敗不中斷
                continue

    def _collect_from_db(self, db: Path, ws: str) -> Iterator[Record]:
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
                where = " OR ".join([f"key LIKE '%{p}%'" for p in CHAT_KEY_PATTERNS])
                cur = conn.execute(f"SELECT key, value FROM ItemTable WHERE {where}")
                for key, value in cur:
                    if not value:
                        continue
                    yield from self._parse_value(key, value, ws, db)
        except sqlite3.Error:
            return

    def _parse_value(self, key: str, value: str, ws: str, db: Path) -> Iterator[Record]:
        """嘗試把 SQLite 的 JSON value 拆成多筆 Record。"""
        # value 可能是巨大 JSON 字串
        if len(value) > MAX_VALUE_SIZE:
            value = value[:MAX_VALUE_SIZE]
        try:
            obj = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            # 非 JSON,當純文字處理
            if len(value) > 30:
                yield Record(
                    content=value,
                    source_agent="cursor",
                    source_type="conversation",
                    source_path=f"cursor/{ws}/{key}",
                    source_id=f"{ws}:{key}",
                    created_at=now_iso(),
                    tags=[f"workspace:{ws}"],
                    importance=0.3,
                )
            return

        # 遞迴找 content/text 欄位
        contents = self._extract_text_fields(obj, depth=0)
        for i, (text, meta) in enumerate(contents):
            if len(text) < 10:
                continue
            yield Record(
                content=text,
                source_agent="cursor",
                source_type="conversation",
                source_path=f"cursor/{ws}/{key}",
                source_id=f"{ws}:{key}#{i}",
                created_at=str(meta.get("timestamp", now_iso())),
                tags=[f"workspace:{ws}", f"key:{key[:30]}"],
                importance=0.4,
                metadata=meta,
            )

    def _extract_text_fields(self, obj, depth: int = 0, path: str = "") -> list[tuple[str, dict]]:
        """遞迴從 JSON 找出 text/content 欄位。"""
        if depth > 5:
            return []
        results = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in ("text", "content", "message", "richText") and isinstance(v, str) and len(v) > 10:
                    meta = {kk: vv for kk, vv in obj.items()
                            if kk in ("role", "timestamp", "createdAt", "type", "id") and isinstance(vv, (str, int, float))}
                    results.append((v, meta))
                elif isinstance(v, (dict, list)):
                    results.extend(self._extract_text_fields(v, depth + 1, f"{path}.{k}"))
        elif isinstance(obj, list):
            for i, item in enumerate(obj[:200]):  # 限制 list 深度
                if isinstance(item, (dict, list)):
                    results.extend(self._extract_text_fields(item, depth + 1, f"{path}[{i}]"))
        return results

    def last_collected(self) -> str:
        return self.state.get("cursor_last", "")

    def set_collected(self, ts: str) -> None:
        self.state["cursor_last"] = ts
