"""異質 schema → 統一 UnifiedRecord 的 mapper。

處理現有 5 種 collection 的 payload 差異:
- openclaw_mem: {content, source, filename, tags, section_path, synced_at, ...}
- claude_mem/deepseek_mem/hermes_mem: {content, platform, role, session_id, memory_type, channel, ...}

每個 mapper 函式吃一個 raw payload dict,回傳 UnifiedRecord。
冪等: 同樣的 raw payload 永遠產出相同的 record_uuid (靠 source_id + content_hash)。
"""
from __future__ import annotations

from typing import Any

from schema import UnifiedRecord, SOURCE_TYPE_BY_MEMORY_TYPE


def normalize_openclaw(payload: dict[str, Any], point_id: str) -> UnifiedRecord:
    """openclaw_mem: 檔案同步來的筆記/週報/daily log。"""
    content = str(payload.get("content", "")).strip()
    source = str(payload.get("source", payload.get("filename", "unknown")))
    created = str(payload.get("created_at", payload.get("synced_at", "")))
    tags = list(payload.get("tags", []))

    return UnifiedRecord(
        content=content,
        source_agent="openclaw",
        source_type="note",
        source_path=source,
        source_id=point_id,                        # 用原始 Qdrant point_id 回溯
        created_at=_normalize_iso(created),
        tags=tags,
        importance=0.5,
        metadata={
            "filename": payload.get("filename", ""),
            "section_path": payload.get("section_path", ""),
            "char_length": payload.get("char_length", len(content)),
            "platform": payload.get("platform", "openclaw"),
        },
    )


def normalize_agent(payload: dict[str, Any], point_id: str) -> UnifiedRecord:
    """claude_mem/deepseek_mem/hermes_mem: 對話記錄,共通 schema。"""
    content = str(payload.get("content", "")).strip()
    platform = str(payload.get("platform", "unknown"))
    memory_type = str(payload.get("memory_type", "conversation"))
    source_type = SOURCE_TYPE_BY_MEMORY_TYPE.get(memory_type, "conversation")
    session_id = str(payload.get("session_id", ""))
    channel = str(payload.get("channel", ""))
    role = str(payload.get("role", ""))
    created = str(payload.get("created_at", payload.get("timestamp", "")))
    importance = float(payload.get("importance", 0.5))

    # source_path: platform/channel/session
    parts = [p for p in (platform, channel, session_id) if p]
    source_path = "/".join(parts) if parts else platform

    return UnifiedRecord(
        content=content,
        source_agent=platform,
        source_type=source_type,
        source_path=source_path,
        source_id=point_id,
        created_at=_normalize_iso(created),
        tags=list(payload.get("tags", [])),
        importance=importance,
        metadata={
            "session_id": session_id,
            "channel": channel,
            "role": role,
            "memory_type": memory_type,
            "char_length": payload.get("char_length", len(content)),
        },
    )


def normalize_unknown(payload: dict[str, Any], point_id: str) -> UnifiedRecord:
    """fallback: 無法辨識的 collection,盡量保留資訊。"""
    content = str(payload.get("content", payload.get("text", ""))).strip()
    return UnifiedRecord(
        content=content,
        source_agent=str(payload.get("platform", payload.get("source_agent", "unknown"))),
        source_type=str(payload.get("source_type", "unknown")),
        source_path=str(payload.get("source", payload.get("source_path", ""))),
        source_id=point_id,
        created_at=_normalize_iso(str(payload.get("created_at", ""))),
        tags=list(payload.get("tags", [])),
        importance=float(payload.get("importance", 0.5)),
        metadata={k: v for k, v in payload.items()
                  if k not in ("content", "source", "tags", "created_at", "importance")},
    )


# collection → mapper 對應表
COLLECTION_MAPPERS: dict[str, Any] = {
    "openclaw_mem": normalize_openclaw,
    "claude_mem": normalize_agent,
    "deepseek_mem": normalize_agent,
    "hermes_mem": normalize_agent,
    "zcode_mem": normalize_agent,                  # zcode_mem 未來也是 agent schema
}


def get_mapper(collection: str):
    """取得 collection 對應的 mapper 函式,未知則用 fallback。"""
    return COLLECTION_MAPPERS.get(collection, normalize_unknown)


def _normalize_iso(s: str) -> str:
    """把各種時間格式規範成 ISO8601;無法解析則回原字串。"""
    if not s:
        return ""
    s = s.strip()
    # 已是 ISO8601 (含 T) 直接回
    if "T" in s:
        return s
    # 嘗試常見格式
    from datetime import datetime
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            continue
    return s
