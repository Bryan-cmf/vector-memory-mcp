"""Connector 抽象基類 + Record dataclass + 去重工具。

所有 connector 產出 Record list,collect.py 統一 embed + upsert 進 unified_mem。
Record 是 UnifiedRecord 的輕量版(collect 時用,不含系統欄位)。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Protocol


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def dedup_hash(source_agent: str, source_id: str, content: str) -> str:
    """去重 hash: source_agent + source_id + content 規範化。"""
    norm = " ".join(content.split())
    raw = f"{source_agent}|{source_id}|{norm[:512]}"  # content 截斷加速
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass
class Record:
    """Connector 產出的單筆記錄 (collect 時的輕量結構)。"""
    content: str
    source_agent: str
    source_type: str
    source_path: str
    source_id: str
    created_at: str = ""
    tags: list[str] = field(default_factory=list)
    importance: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = now_iso()


class Connector(Protocol):
    """所有 connector 遵循的介面 (duck typing,不強制繼承)。"""

    name: str   # connector 識別名

    def is_available(self) -> bool:
        """這台機器上是否有這個 agent 的資料。"""
        ...

    def discover(self) -> int:
        """探測可採集的記錄數 (不實際讀取內容),回傳估計數。"""
        ...

    def collect(self) -> Iterator[Record]:
        """實際讀取,產出 Record。應容錯: 單筆失敗不中斷整個迭代。"""
        ...

    def last_collected(self) -> str:
        """上次採集的時間戳 (增量用),空字串表從未。"""
        ...

    def set_collected(self, ts: str) -> None:
        """記錄本次採集時間。"""
        ...
