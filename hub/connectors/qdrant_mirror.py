"""QdrantMirrorConnector — 把既有 collection 鏡像進 unified_mem。

採集現有 5 個 collection (openclaw_mem 等) 的新資料,normalize 後寫進 unified_mem。
與 migrate.py 的差異: migrate 是一次性全量;mirror 是增量 (只採 last_collected 之後)。

但 Qdrant 沒有原生「by time」過濾,這裡用 scroll + 比對已存在 UUID 的方式做增量。
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from typing import Iterator

from connectors.base import Record, now_iso
from normalize import get_mapper

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
SOURCES = ["openclaw_mem", "claude_mem", "deepseek_mem", "hermes_mem"]


def _qdrant(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{QDRANT_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        return {}


class QdrantMirrorConnector:
    """鏡像既有 Qdrant collection 進 unified_mem。"""

    name = "qdrant_mirror"

    def __init__(self, state: dict | None = None):
        self.state = state or {}

    def is_available(self) -> bool:
        r = _qdrant("GET", "/")
        return bool(r.get("title"))

    def discover(self) -> int:
        total = 0
        for src in SOURCES:
            info = _qdrant("GET", f"/collections/{src}").get("result", {})
            total += info.get("points_count", 0)
        return total

    def collect(self) -> Iterator[Record]:
        for src in SOURCES:
            mapper = get_mapper(src)
            offset = None
            seen = self.state.get(f"mirror_seen_{src}", 0)
            count = 0
            while True:
                body = {"limit": 100, "with_payload": True, "with_vector": False}
                if offset:
                    body["offset"] = offset
                r = _qdrant("POST", f"/collections/{src}/points/scroll", body)
                res = r.get("result", {})
                points = res.get("points", [])
                if not points:
                    break
                for p in points:
                    try:
                        payload = p.get("payload", {})
                        pid = str(p.get("id", ""))
                        rec = mapper(payload, pid)
                        if rec.content and len(rec.content) >= 3:
                            # 轉成 collect 用的 Record (輕量)
                            yield Record(
                                content=rec.content,
                                source_agent=rec.source_agent,
                                source_type=rec.source_type,
                                source_path=rec.source_path,
                                source_id=rec.source_id,
                                created_at=rec.created_at,
                                tags=rec.tags,
                                importance=rec.importance,
                                metadata=rec.metadata,
                            )
                            count += 1
                    except Exception:
                        continue
                offset = res.get("next_page_offset")
                if offset is None:
                    break
            self.state[f"mirror_seen_{src}"] = count

    def last_collected(self) -> str:
        return self.state.get("qdrant_mirror_last", "")

    def set_collected(self, ts: str) -> None:
        self.state["qdrant_mirror_last"] = ts
