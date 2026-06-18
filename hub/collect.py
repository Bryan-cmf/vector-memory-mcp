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


def qdrant_upsert(records: list[UnifiedRecord], embedder) -> int:
    """embed + upsert 一批,回傳成功數。"""
    import urllib.request
    texts = [r.content for r in records]
    vectors = embedder.encode(texts)
    points = []
    for rec, vec in zip(records, vectors):
        points.append({"id": rec.record_uuid, "vector": vec, "payload": rec.to_payload()})
    body = json.dumps({"points": points}).encode()
    req = urllib.request.Request(f"{QDRANT_URL}/collections/{UNIFIED}/points?wait=true",
                                 data=body, method="PUT",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()
        return len(points)
    except Exception as e:
        print(f"  ⚠️ upsert 失敗: {e}", file=sys.stderr)
        return 0


def record_to_unified(rec) -> UnifiedRecord:
    """把 connector 的 Record 轉成 UnifiedRecord (填系統欄位)。"""
    return UnifiedRecord(
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

    grand_total = 0
    grand_new = 0
    agent_stats: dict[str, int] = {}

    for c in available:
        print(f"\n🔧 採集: {c.name}")
        batch: list[UnifiedRecord] = []
        conn_new = 0
        conn_total = 0
        errors = 0
        try:
            for rec in c.collect():
                conn_total += 1
                try:
                    ur = record_to_unified(rec)
                    batch.append(ur)
                    conn_new += 1
                    agent_stats[rec.source_agent] = agent_stats.get(rec.source_agent, 0) + 1
                    if len(batch) >= BATCH_SIZE:
                        n = qdrant_upsert(batch, embedder)
                        grand_new += n
                        batch.clear()
                except Exception:
                    errors += 1
                    continue
            if batch:
                n = qdrant_upsert(batch, embedder)
                grand_new += n
                batch.clear()
        except Exception as e:
            print(f"  ⚠️ {c.name} 採集中斷: {e}")
            errors += 1

        c.set_collected(now_iso())
        grand_total += conn_total
        print(f"   {c.name}: 採集 {conn_total}, 寫入 {conn_new}, 錯誤 {errors}")

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
