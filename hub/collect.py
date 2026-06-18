#!/usr/bin/env python3
"""採集協調器 — 跑所有 is_available() 的 connector,embed + upsert 進 unified_mem。

Usage:
    python collect.py              # 跑一次所有 connector
    python collect.py --dry-run    # 只 discover,不寫入
    python collect.py --only markdown_dir,zcode   # 只跑指定 connector

狀態檔: ~/.vector-memory-mcp/hub-state.json (各 connector 的 last_collected + 統計)
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from schema import UnifiedRecord, hash_content, now_iso
from connectors.base import dedup_hash
from connectors import ALL_CONNECTORS

# 隱私 redaction (階段 7 整合)
try:
    from privacy import redact_content, ensure_privacy_config
    _PRIVACY_OK = True
except ImportError:
    _PRIVACY_OK = False

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
UNIFIED = "unified_mem"
STATE_FILE = Path(os.environ.get("VECTOR_MEMORY_DIR", str(Path.home() / ".vector-memory-mcp"))) / "hub-state.json"
BATCH_SIZE = 64

# 共用 embedder (跨 connector 重用,避免重複載模型)
_EMBEDDER = None


def get_embedder():
    global _EMBEDDER
    if _EMBEDDER is None:
        from migrate import EmbedderAdapter
        _EMBEDDER = EmbedderAdapter()
        _EMBEDDER.load()
    return _EMBEDDER


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(STATE_FILE)


def ensure_collection(name: str, dim: int = 1024) -> bool:
    """確保 collection 存在,不存在則建立 (1024d Cosine,跟 BGE-m3 對齊)。"""
    import urllib.request
    try:
        req = urllib.request.Request(f"{QDRANT_URL}/collections/{name}",
                                     method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            json.loads(r.read().decode())
        return True   # 已存在
    except Exception:
        pass
    # 建立
    try:
        body = json.dumps({"vectors": {"size": dim, "distance": "Cosine"}}).encode()
        req = urllib.request.Request(f"{QDRANT_URL}/collections/{name}?timeout=60",
                                     data=body, method="PUT",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()
        print(f"   📦 建立 collection: {name} ({dim}d)", flush=True)
        return True
    except Exception as e:
        print(f"   ⚠️ 建立 {name} 失敗: {e}", file=sys.stderr)
        return False


def qdrant_upsert(records: list[UnifiedRecord], embedder,
                  collection: str = UNIFIED,
                  payload_override=None) -> int:
    """embed + upsert 一批,回傳成功數。

    Args:
      collection: 寫進哪個 collection (預設 unified_mem)
      payload_override: 可選函式 (UnifiedRecord) -> dict,
                        用來覆寫 payload (例如寫 *_mem 相容格式時)

    保護:
    - content 超過 MAX_CONTENT_CHARS (8000) 會截斷 (BGE-m3 上限 ~8192 tokens,MPS 對超大張量會崩)
    - embed 失敗的單筆會被跳過,不拖垮整批
    """
    import urllib.request
    MAX_CONTENT_CHARS = 8000   # BGE-m3 安全上限

    # 截斷超長 content (避免 MPS INT_MAX 錯誤)
    texts = []
    for r in records:
        t = r.content[:MAX_CONTENT_CHARS] if len(r.content) > MAX_CONTENT_CHARS else r.content
        texts.append(t)

    # embed (可能對超大/特殊內容失敗,逐筆容錯)
    try:
        vectors = embedder.encode(texts)
    except Exception as e:
        # 整批失敗,退化為逐筆 embed (犧牲速度換成功率)
        print(f"  ⚠️ 整批 embed 失敗 ({str(e)[:80]}),退化逐筆", file=sys.stderr, flush=True)
        vectors = []
        for t in texts:
            try:
                v = embedder.encode([t])
                vectors.append(v[0] if isinstance(v, list) else v)
            except Exception:
                vectors.append(None)   # 這筆放棄

    points = []
    for rec, vec in zip(records, vectors):
        if vec is None:
            continue    # embed 失敗的跳過
        payload = payload_override(rec) if payload_override else rec.to_payload()
        # *_mem 相容 collection 用不同 UUID namespace (避免跟 unified_mem 衝突到同 ID)
        if collection != UNIFIED:
            import uuid as _uuid
            import hashlib as _hashlib
            ns_key = f"{collection}:{rec.record_uuid}"
            point_id = str(_uuid.uuid5(_uuid.NAMESPACE_URL, ns_key))
        else:
            point_id = rec.record_uuid
        points.append({"id": point_id, "vector": vec, "payload": payload})

    if not points:
        return 0
    body = json.dumps({"points": points}).encode()
    req = urllib.request.Request(f"{QDRANT_URL}/collections/{collection}/points?wait=true",
                                 data=body, method="PUT",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()
        return len(points)
    except Exception as e:
        print(f"  ⚠️ upsert 到 {collection} 失敗: {e}", file=sys.stderr)
        return 0


def record_to_unified(rec) -> UnifiedRecord:
    """把 connector 的 Record 轉成 UnifiedRecord (填系統欄位 + 隱私 redact)。"""
    content = rec.content
    privacy_score = 0.0

    # 隱私 redaction (階段 7)
    if _PRIVACY_OK:
        result = redact_content(content)
        content = result.content
        privacy_score = result.score

    # 重新計算 content_hash (因為 redact 可能改了內容)
    return UnifiedRecord(
        content=content,
        source_agent=rec.source_agent,
        source_type=rec.source_type,
        source_path=rec.source_path,
        source_id=rec.source_id,
        created_at=rec.created_at,
        tags=rec.tags,
        importance=rec.importance,
        metadata={**rec.metadata, "privacy_score": privacy_score},
    )


def main():
    import argparse
    p = argparse.ArgumentParser(description="vector-memory-hub 採集協調器")
    p.add_argument("--dry-run", action="store_true", help="只 discover 不寫入")
    p.add_argument("--only", default="", help="只跑指定 connector (逗號分隔)")
    args = p.parse_args()

    t0 = time.time()
    state = load_state()
    print("=" * 60)
    print(f"🔄 採集協調器 {'(DRY RUN)' if args.dry_run else ''}")
    print("=" * 60)

    only_set = set(args.only.split(",")) if args.only else None

    # Phase 1: discover
    print("\n📋 Phase 1: 偵測可用 connector")
    available = []
    for cls in ALL_CONNECTORS:
        c = cls(state=state)
        if only_set and c.name not in only_set:
            continue
        try:
            if c.is_available():
                est = c.discover()
                print(f"  ✓ {c.name}: 可用 (估計 {est} 筆)")
                available.append(c)
            else:
                print(f"  ✗ {c.name}: 不可用")
        except Exception as e:
            print(f"  ✗ {c.name}: 偵測失敗 ({e})")

    if not available:
        print("\n⚠️ 沒有可用的 connector")
        return

    if args.dry_run:
        print(f"\n[DRY RUN] 不寫入,結束")
        return

    # Phase 2: collect + embed + upsert
    print(f"\n📥 Phase 2: 載入 embedder (首次 ~5s)")
    embedder = get_embedder()
    print(f"   ✓ {embedder.model_name} ({embedder.get_dimension()}d)")

    # 確保所有 target_collection 存在 (如 zcode_mem)
    target_cols = {getattr(c, "target_collection") for c in available
                   if getattr(c, "target_collection", None)}
    for tc in target_cols:
        ensure_collection(tc, embedder.get_dimension())

    grand_total = 0
    grand_new = 0
    agent_stats: dict[str, int] = {}

    for c in available:
        print(f"\n🔧 採集: {c.name}")
        # 是否要雙寫進專屬 collection (如 zcode_mem)
        target_col = getattr(c, "target_collection", None)
        target_override = getattr(c, "payload_for_target", None)   # *_mem schema 轉換函式
        batch: list[UnifiedRecord] = []
        conn_new = 0
        conn_total = 0
        errors = 0
        target_new = 0
        try:
            for rec in c.collect():
                conn_total += 1
                try:
                    ur = record_to_unified(rec)
                    batch.append(ur)
                    conn_new += 1
                    agent_stats[rec.source_agent] = agent_stats.get(rec.source_agent, 0) + 1
                    if len(batch) >= BATCH_SIZE:
                        # 1. 寫進 unified_mem (統一庫)
                        n = qdrant_upsert(batch, embedder, collection=UNIFIED)
                        grand_new += n
                        # 2. 若有 target_collection,雙寫進專屬 collection (*_mem schema)
                        if target_col and target_override:
                            tn = qdrant_upsert(batch, embedder,
                                               collection=target_col,
                                               payload_override=target_override)
                            target_new += tn
                        batch.clear()
                except Exception:
                    errors += 1
                    continue
            if batch:
                n = qdrant_upsert(batch, embedder, collection=UNIFIED)
                grand_new += n
                if target_col and target_override:
                    tn = qdrant_upsert(batch, embedder,
                                       collection=target_col,
                                       payload_override=target_override)
                    target_new += tn
                batch.clear()
        except Exception as e:
            print(f"  ⚠️ {c.name} 採集中斷: {e}")
            errors += 1

        c.set_collected(now_iso())
        grand_total += conn_total
        target_msg = f", 專屬 {target_col} +{target_new}" if target_col and target_new else ""
        print(f"   {c.name}: 採集 {conn_total}, 寫入 unified {conn_new}{target_msg}, 錯誤 {errors}")

    save_state(state)

    elapsed = time.time() - t0
    print(f"\n{'=' * 60}")
    print(f"✅ 採集完成")
    print(f"   connector 數: {len(available)}")
    print(f"   採集記錄總計: {grand_total}")
    print(f"   寫入 unified_mem: {grand_new}")
    print(f"   耗時: {elapsed:.1f}s")
    if agent_stats:
        print(f"   source_agent 分佈:")
        for a, n in sorted(agent_stats.items(), key=lambda x: -x[1]):
            print(f"     {a}: {n}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
