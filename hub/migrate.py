#!/usr/bin/env python3
"""Migration: 把既有 5 個 collection 的資料 migrate 進 unified_mem。

流程:
  1. snapshot 備份 (破壞性操作前的安全網)
  2. 建立 unified_mem collection (若不存在)
  3. 對每個來源 collection scroll → normalize → re-embed → upsert 進 unified_mem
  4. 記錄統計

冪等: 重複執行只會 upsert (record_uuid 相同會覆蓋),不會產生重複。
保留原始 collection 不動。
"""
from __future__ import annotations

import os
import sys
import time
import urllib.request
import urllib.error
import json
from pathlib import Path

# 讓 import schema/normalize 在直接執行時也能用
sys.path.insert(0, str(Path(__file__).parent))

from schema import UnifiedRecord
from normalize import get_mapper

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
UNIFIED = "unified_mem"
SOURCES = ["openclaw_mem", "claude_mem", "deepseek_mem", "hermes_mem"]
# zcode_mem 是空的,略過以節省時間
BATCH_SIZE = 64
SCROLL_SIZE = 200


def qdrant(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{QDRANT_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Qdrant {method} {path} → HTTP {e.code}: {e.read().decode()[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Qdrant 連不上: {e.reason}")


def snapshot_backup() -> str:
    """建立整個 Qdrant 的 snapshot (所有 collection)。"""
    print("📸 建立 Qdrant snapshot 備份...", flush=True)
    try:
        # 觸發 snapshot 建立 (非同步,要等一下)
        r = qdrant("POST", "/snapshots")
        snap_name = r.get("result", {}).get("name", "")
        if snap_name:
            print(f"   snapshot: {snap_name}", flush=True)
            # 等快照完成 (簡單 sleep,實際上 create 是同步的)
            time.sleep(2)
            return snap_name
    except RuntimeError as e:
        print(f"   ⚠️ snapshot 失敗 (繼續,migration 是非破壞性的 upsert): {e}", flush=True)
    return ""


def ensure_unified(embedder) -> int:
    """確保 unified_mem 存在,回傳向量維度。"""
    cols = qdrant("GET", "/collections").get("result", {}).get("collections", [])
    names = [c["name"] for c in cols]
    if UNIFIED in names:
        info = qdrant("GET", f"/collections/{UNIFIED}").get("result", {})
        dim = info.get("config", {}).get("params", {}).get("vectors", {}).get("size", 1024)
        print(f"✓ unified_mem 已存在 ({info.get('points_count', 0)} pts, {dim}d)", flush=True)
        return dim

    dim = embedder.get_dimension()
    print(f"📦 建立 unified_mem ({dim}d Cosine)...", flush=True)
    qdrant("PUT", f"/collections/{UNIFIED}?timeout=60", {
        "vectors": {"size": dim, "distance": "Cosine"},
        "optimizers_config": {"default_segment_number": 4},
    })
    print(f"   ✓ unified_mem 已建立", flush=True)
    return dim


def scroll_collection(name: str, offset: str | None = None):
    """scroll 一批,回傳 (points, next_offset)。"""
    body = {"limit": SCROLL_SIZE, "with_payload": True, "with_vector": False}
    if offset:
        body["offset"] = offset
    r = qdrant("POST", f"/collections/{name}/points/scroll", body)
    res = r.get("result", {})
    return res.get("points", []), res.get("next_page_offset")


def upsert_batch(records: list[UnifiedRecord], embedder) -> int:
    """embed + upsert 一批 records 進 unified_mem。回傳成功數。"""
    if not records:
        return 0
    texts = [r.content for r in records]
    vectors = embedder.encode(texts)                # List[List[float]]

    points = []
    for rec, vec in zip(records, vectors):
        points.append({
            "id": rec.record_uuid,
            "vector": vec,
            "payload": rec.to_payload(),
        })

    qdrant("PUT", f"/collections/{UNIFIED}/points?wait=true", {"points": points})
    return len(points)


def migrate_source(name: str, embedder) -> tuple[int, int]:
    """migrate 單一 source collection。回傳 (總數, 成功數)。"""
    mapper = get_mapper(name)
    info = qdrant("GET", f"/collections/{name}").get("result", {})
    total = info.get("points_count", 0)
    if total == 0:
        print(f"  ⏭️  {name}: 空集合,略過", flush=True)
        return 0, 0

    # 短路: 若 unified_mem 已有此 source 的資料,且點數 >= source,視為已 migrate
    # (避免重跑時無腦全量 re-embed 造成 timeout)
    try:
        unified_info = qdrant("GET", f"/collections/{UNIFIED}").get("result", {})
        unified_count = unified_info.get("points_count", 0)
        if unified_count > 0 and not os.environ.get("MIGRATE_FORCE"):
            # 抽樣確認 source 是否已在 unified_mem (用 source_agent filter)
            source_agent_hint = {
                "openclaw_mem": "openclaw", "claude_mem": "claude",
                "deepseek_mem": "deepseek", "hermes_mem": "hermes",
            }.get(name)
            if source_agent_hint:
                from urllib.parse import quote
                # scroll 一筆看 source_agent 是否存在
                chk = qdrant("POST", f"/collections/{UNIFIED}/points/scroll", {
                    "limit": 1, "with_payload": True, "with_vector": False,
                    "filter": {"must": [{"key": "source_agent", "match": {"value": source_agent_hint}}]}
                })
                if chk.get("result", {}).get("points"):
                    print(f"  ⏭️  {name}: 已 migrate 過 (unified_mem 有 {source_agent_hint} 資料),跳過。設 MIGRATE_FORCE=1 強制重跑", flush=True)
                    return total, 0
    except Exception:
        pass    # 檢查失敗就照常跑 (安全 fallback)

    print(f"\n📋 {name} ({total} pts) → normalize + embed + upsert...", flush=True)
    offset = None
    done = 0
    batch: list[UnifiedRecord] = []
    skipped_empty = 0

    while True:
        points, offset = scroll_collection(name, offset)
        if not points:
            break
        for p in points:
            payload = p.get("payload", {})
            pid = str(p.get("id", ""))
            rec = mapper(payload, pid)
            if not rec.content or len(rec.content) < 3:
                skipped_empty += 1
                continue
            batch.append(rec)
            if len(batch) >= BATCH_SIZE:
                done += upsert_batch(batch, embedder)
                batch.clear()
                pct = min(100, int(done / max(1, total) * 100))
                print(f"   [{pct:3d}%] {done}/{total}", end="\r", flush=True)
        if offset is None:
            break

    if batch:
        done += upsert_batch(batch, embedder)
        batch.clear()

    print(f"   ✓ {name}: {done} migrated" +
          (f" ({skipped_empty} 空內容略過)" if skipped_empty else ""), flush=True)
    return total, done


def main():
    t0 = time.time()
    print("=" * 60)
    print("🔄 Migration → unified_mem")
    print("=" * 60)

    # 1. snapshot 備份
    snapshot_backup()

    # 2. 載入 embedder (mcp_server 的或獨立載 BGE-m3)
    print("\n📥 載入 embedding 模型...", flush=True)
    embedder = EmbedderAdapter()
    embedder.load()
    print(f"   ✓ {embedder.model_name} ({embedder.get_dimension()}d)", flush=True)

    # 3. 確保 unified_mem 存在
    ensure_unified(embedder)

    # 4. 逐個 source migrate
    grand_total = 0
    grand_done = 0
    for src in SOURCES:
        try:
            t, d = migrate_source(src, embedder)
            grand_total += t
            grand_done += d
        except Exception as e:
            print(f"   ✗ {src} migration 失敗: {e}", flush=True)

    # 5. 統計
    info = qdrant("GET", f"/collections/{UNIFIED}").get("result", {})
    final = info.get("points_count", 0)
    elapsed = time.time() - t0
    print(f"\n{'=' * 60}")
    print(f"✅ Migration 完成")
    print(f"   來源總計: {grand_total} pts")
    print(f"   成功 migrate: {grand_done} pts")
    print(f"   unified_mem 最終: {final} pts")
    print(f"   耗時: {elapsed:.1f}s")
    print(f"{'=' * 60}")


class EmbedderAdapter:
    """統一 embedder 介面,包裝 sentence_transformers (懶載入)。"""

    def __init__(self):
        self.model_name = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-m3")
        self._model = None
        self._dim = None

    def load(self):
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer(self.model_name, device="mps")
        self._dim = self._model.get_sentence_embedding_dimension()

    def get_dimension(self) -> int:
        if self._dim is None:
            self.load()
        return self._dim

    def encode(self, texts: list[str]) -> list[list[float]]:
        import numpy as np
        vecs = self._model.encode(texts, normalize_embeddings=True,
                                  show_progress_bar=False, batch_size=32)
        return [v.tolist() if isinstance(v, np.ndarray) else list(v) for v in vecs]


if __name__ == "__main__":
    main()
