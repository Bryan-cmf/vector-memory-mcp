"""統一記憶 schema — 所有 connector 產出的標準格式。

Canonical Unified Memory Record (UMR)。各 agent 的異質 payload 經 normalize.py
對齊到此 schema,存進 unified_mem collection。向量欄位由 embedder 額外產生,
本 dataclass 只描述 payload 部分。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any
import hashlib
import uuid


@dataclass
class UnifiedRecord:
    """統一記憶記錄 (payload schema for unified_mem)."""

    # ── 必填 ──
    content: str                                   # 記憶內容
    source_agent: str                              # claude | cursor | zcode | openclaw | markdown | ...
    source_type: str                               # conversation | note | code | decision | fact | task
    source_path: str                               # 原始檔案/DB/session 位置
    source_id: str                                 # 原始記錄 ID (去重 + 回溯用)
    created_at: str                                # ISO8601,原始建立時間(非採集時間)

    # ── 選填 ──
    tags: list[str] = field(default_factory=list)
    importance: float = 0.5                        # 0.0–1.0
    metadata: dict[str, Any] = field(default_factory=dict)  # 各 agent 特有欄位放這

    # ── 系統欄位 (normalize/collect 時填) ──
    collected_at: str = ""                         # 採集時間 ISO8601
    content_hash: str = ""                         # content 的 SHA256(去重用)
    record_uuid: str = ""                          # 本記錄在 unified_mem 的 UUID

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = hash_content(self.content)
        if not self.record_uuid:
            # UUID5(namespace, source_agent + source_id + content_hash) — 冪等
            self.record_uuid = str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{self.source_agent}:{self.source_id}:{self.content_hash[:16]}",
            ))
        if not self.collected_at:
            self.collected_at = now_iso()

    def to_payload(self) -> dict[str, Any]:
        """轉成 Qdrant payload (全部欄位,不含向量)。"""
        return asdict(self)

    def summary(self) -> str:
        """一行摘要 (log 用)。"""
        c = self.content.replace("\n", " ")[:60]
        return f"[{self.source_agent}/{self.source_type}] {c}"


def hash_content(text: str) -> str:
    """content 的 SHA256 hex (去重用,跨 agent 統一)。"""
    # normalize whitespace 讓「只有空白差異」的內容也算重複
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def now_iso() -> str:
    """當下 UTC ISO8601。"""
    return datetime.now(timezone.utc).isoformat()


# 各 source_agent 對應的 source_type 對應表 (給 normalize 用)
SOURCE_TYPE_BY_MEMORY_TYPE = {
    "fact": "fact",
    "decision": "decision",
    "task": "task",
    "conversation": "conversation",
    "note": "note",
    "code": "code",
}
