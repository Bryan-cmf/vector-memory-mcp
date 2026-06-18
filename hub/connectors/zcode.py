"""ZCodeConnector — 採集 ZCode 的對話 log + 設定。

ZCode (OpenCode fork) 把資料存在 ~/.zcode/v2/:
- *.json: 設定/狀態 (bot-state, config, setting)
- logs/*.log: 對話/操作 log

這個 connector 輕量採集:把每個 JSON 的關鍵欄位 + log 的對話行 parse 成 Record。
不做深度語意分析 (留給 embedding),只把有意義的文字片段送進 unified_mem。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Iterator

from connectors.base import Record, now_iso

ZCODE_ROOT = Path.home() / ".zcode"
ZCODE_V2 = ZCODE_ROOT / "v2"
ZCODE_LOGS = ZCODE_V2 / "logs"

# 採集哪些 JSON (排除含敏感資訊的)
COLLECT_JSON = ["bot-state.v2.json", "coding-plan-cache.json", "telemetry-state.json"]
SKIP_JSON = {"credentials.json", "config.json", "setting.json"}  # 含 API key 等
MAX_LOG_SIZE = 200_000


class ZCodeConnector:
    """採集 ZCode 對話 log + 非敏感狀態。

    雙寫: 除 unified_mem 外,也寫進 zcode_mem (跟 claude_mem 等 *_mem 同 schema),
    讓 mem_search(collection="zcode_mem") 能直接用。
    """

    name = "zcode"
    target_collection = "zcode_mem"   # 專屬 collection (跟 *_mem 對齊)

    def __init__(self, state: dict | None = None):
        self.state = state or {}

    @staticmethod
    def payload_for_target(rec) -> dict:
        """把 UnifiedRecord 轉成 *_mem 相容 payload (跟 claude_mem 同 schema)。

        *_mem schema: content, platform, role, channel, memory_type,
                      importance, session_id, timestamp, char_length, created_at
        """
        md = rec.metadata or {}
        return {
            "content": rec.content,
            "platform": "zcode",
            "role": md.get("role", "system"),
            "channel": "zcode-local",
            "memory_type": rec.source_type if rec.source_type in
                           ("conversation", "fact", "decision", "task", "note")
                           else "note",
            "importance": int(round(rec.importance * 10)) if rec.importance <= 1 else int(rec.importance),
            "session_id": md.get("session_id", ""),
            "timestamp": rec.created_at,
            "char_length": md.get("char_length", len(rec.content)),
            "created_at": rec.created_at,
        }

    def is_available(self) -> bool:
        return ZCODE_V2.exists() or ZCODE_LOGS.exists()

    def discover(self) -> int:
        n = 0
        for name in COLLECT_JSON:
            if (ZCODE_V2 / name).exists():
                n += 1
        if ZCODE_LOGS.exists():
            n += len(list(ZCODE_LOGS.glob("*.log")))
        return n

    def collect(self) -> Iterator[Record]:
        # 1) JSON 狀態檔
        for name in COLLECT_JSON:
            f = ZCODE_V2 / name
            if not f.exists():
                continue
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime).isoformat()
                obj = json.loads(f.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            # 把 JSON 摘要成一筆 Record (避免太碎)
            content = self._summarize_json(obj, name)
            if content and len(content) > 20:
                yield Record(
                    content=content,
                    source_agent="zcode",
                    source_type="state",
                    source_path=f"~/.zcode/v2/{name}",
                    source_id=f"zcode:{name}",
                    created_at=mtime,
                    tags=[f"file:{name}"],
                    importance=0.3,
                )

        # 2) Log 檔
        if ZCODE_LOGS.exists():
            for log in ZCODE_LOGS.glob("*.log"):
                try:
                    if log.stat().st_size > MAX_LOG_SIZE:
                        # 大 log 只取最後 1000 行
                        content = self._tail(log, 1000)
                    else:
                        content = log.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                if not content:
                    continue
                mtime = datetime.fromtimestamp(log.stat().st_mtime).isoformat()
                # 把 log 依對話邊界分塊 (簡化: 每筆含 user/assistant 段落)
                yield from self._chunk_log(content, log.name, mtime)

    def _summarize_json(self, obj: dict, name: str) -> str:
        """把 JSON 狀態摘要成可讀文字 (避免直接 dump 含敏感欄位)。"""
        if name == "bot-state.v2.json":
            # bot-state 結構: {bots: {id: {name, lastActive, ...}}}
            bots = obj.get("bots", obj)
            if isinstance(bots, dict):
                lines = []
                for bid, b in list(bots.items())[:20]:
                    if isinstance(b, dict):
                        lines.append(f"- bot {bid}: {b.get('name', '?')}, model={b.get('model', '?')}")
                return f"ZCode bot 狀態:\n" + "\n".join(lines)
        elif name == "coding-plan-cache.json":
            plans = obj if isinstance(obj, dict) else {}
            if plans:
                return f"ZCode coding-plan 快取 ({len(plans)} 項):\n" + json.dumps(list(plans.keys())[:10], ensure_ascii=False)
        # fallback: keys 列表
        if isinstance(obj, dict):
            return f"ZCode {name}: {list(obj.keys())[:10]}"
        return ""

    def _tail(self, path: Path, n: int) -> str:
        try:
            import subprocess
            r = subprocess.run(["tail", "-n", str(n), str(path)], capture_output=True, text=True, timeout=5)
            return r.stdout
        except Exception:
            return ""

    def _chunk_log(self, content: str, log_name: str, mtime: str) -> Iterator[Record]:
        """把 log 依 timestamp 行分塊 (簡化: 每 50 行一塊)。"""
        lines = content.splitlines()
        chunk_size = 50
        for i in range(0, len(lines), chunk_size):
            chunk = "\n".join(lines[i:i+chunk_size]).strip()
            if len(chunk) < 30:
                continue
            yield Record(
                content=chunk,
                source_agent="zcode",
                source_type="log",
                source_path=f"~/.zcode/v2/logs/{log_name}",
                source_id=f"zcode:{log_name}#L{i//chunk_size}",
                created_at=mtime,
                tags=[f"log:{log_name}"],
                importance=0.3,
            )

    def last_collected(self) -> str:
        return self.state.get("zcode_last", "")

    def set_collected(self, ts: str) -> None:
        self.state["zcode_last"] = ts
