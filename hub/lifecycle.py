#!/usr/bin/env python3
"""記憶生命週期自動化 — dedup / decay / contradict。

不依賴 MCP server (mem_* 工具走 stdio 不易直接呼叫),改用 Qdrant REST 自實作:
- dedup:   對每筆記錄搜尋相似向量,刪除 score > threshold 的重複 (保留最早建立的)
- decay:   依 last_accessed 時間 + access_count 計算 health_score,降 importance
- contradict: 偵測互相矛盾的記憶 (最佳努力,目前用關鍵詞反義偵測)

Usage:
    python lifecycle.py dedup                  # 跑去重 (預設 dry_run)
    python lifecycle.py dedup --apply          # 真的刪
    python lifecycle.py decay --apply          # 真的降權
    python lifecycle.py contradict             # 偵測矛盾 (永遠 dry_run)
    python lifecycle.py all --apply            # 全跑

狀態: ~/.vector-memory-mcp/hub-state.json 的 lifecycle_* 欄位
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
UNIFIED = "unified_mem"
STATE_FILE = Path(os.environ.get("VECTOR_MEMORY_DIR", str(Path.home() / ".vector-memory-mcp"))) / "hub-state.json"

DECAY_LAMBDA = 0.01   # 衰減係數 (越大衰減越快); math.exp(-0.01 * days)


def qdrant(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{QDRANT_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        raise RuntimeError(f"Qdrant {method} {path}: {e}")


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


def scroll_all(limit_per_call: int = 200, max_points: int = 20000) -> list[dict]:
    """scroll 全部 unified_mem points (含 vector)。"""
    points = []
    offset = None
    while len(points) < max_points:
        body: dict = {"limit": limit_per_call, "with_payload": True, "with_vector": True}
        if offset:
            body["offset"] = offset
        r = qdrant("POST", f"/collections/{UNIFIED}/points/scroll", body)
        res = r.get("result", {})
        batch = res.get("points", [])
        if not batch:
            break
        points.extend(batch)
        offset = res.get("next_page_offset")
        if offset is None:
            break
    return points


# ─────────────────────────────────────────────────────────
# dedup
# ─────────────────────────────────────────────────────────
def run_dedup(threshold: float = 0.92, apply: bool = False) -> dict:
    """語意去重: 對每筆搜尋相似向量,刪除重複 (保留最早建立的)。"""
    print(f"🔍 dedup (threshold={threshold}, apply={apply})", flush=True)
    t0 = time.time()
    points = scroll_all(max_points=10000)
    print(f"   載入 {len(points)} 點", flush=True)

    # 先用 content_hash 做硬去重 (內容完全一樣的)
    by_hash: dict[str, list[dict]] = {}
    for p in points:
        h = p.get("payload", {}).get("content_hash", "")
        if h:
            by_hash.setdefault(h, []).append(p)

    exact_dup_ids = []
    for h, group in by_hash.items():
        if len(group) < 2:
            continue
        # 保留 created_at 最早的,其餘刪
        group.sort(key=lambda x: x.get("payload", {}).get("created_at", ""))
        for dup in group[1:]:
            exact_dup_ids.append(dup.get("id"))

    print(f"   硬重複 (content_hash 相同): {len(exact_dup_ids)}", flush=True)

    # 語意去重 (向量相似度 > threshold):抽樣做 (全兩兩比對太慢)
    sem_dup_ids = []
    sample = points[:2000]  # 只比對前 2000 點 (隨機抽樣可改進,但 MVP 夠用)
    checked = set()
    for i, p in enumerate(sample):
        if p.get("id") in checked:
            continue
        vec = p.get("vector")
        if not vec:
            continue
        try:
            r = qdrant("POST", f"/collections/{UNIFIED}/points/search", {
                "vector": vec,
                "limit": 5,
                "score_threshold": threshold,
                "with_payload": False,
                "with_vector": False,
            })
            hits = r.get("result", [])
            for h in hits[1:]:  # 跳過自己 (hits[0])
                hid = h.get("id")
                if hid and hid != p.get("id") and hid not in checked:
                    sem_dup_ids.append(hid)
                    checked.add(hid)
        except Exception:
            continue

    print(f"   語意重複 (similarity > {threshold}): {len(sem_dup_ids)}", flush=True)

    all_dups = list(set(exact_dup_ids + sem_dup_ids))
    print(f"   總計去重目標: {len(all_dups)}", flush=True)

    deleted = 0
    if apply and all_dups:
        # 分批刪 (Qdrant delete 上限)
        for i in range(0, len(all_dups), 500):
            batch = all_dups[i:i+500]
            try:
                qdrant("POST", f"/collections/{UNIFIED}/points/delete",
                       {"points": batch, "wait": True})
                deleted += len(batch)
            except Exception as e:
                print(f"   ⚠️ delete 失敗: {e}", flush=True)

    elapsed = time.time() - t0
    print(f"   {'🗑️ 已刪' if apply else '🔍 偵測'}: {deleted if apply else len(all_dups)}", flush=True)
    print(f"   耗時: {elapsed:.1f}s", flush=True)
    return {"exact_dup": len(exact_dup_ids), "sem_dup": len(sem_dup_ids),
            "total_target": len(all_dups), "deleted": deleted, "applied": apply}


# ─────────────────────────────────────────────────────────
# decay
# ─────────────────────────────────────────────────────────
def run_decay(apply: bool = False) -> dict:
    """時間衰減: 降 importance 給老舊未存取記憶。"""
    print(f"📉 decay (apply={apply})", flush=True)
    t0 = time.time()
    points = scroll_all(max_points=20000)
    print(f"   載入 {len(points)} 點", flush=True)

    now = datetime.now(timezone.utc)
    updates = []
    decayed = 0
    for p in points:
        payload = p.get("payload", {})
        created = payload.get("created_at", "")
        last_acc = payload.get("collected_at", created)
        try:
            dt = datetime.fromisoformat(last_acc.replace("Z", "+00:00"))
            days = max(0, (now - dt).days)
        except (ValueError, TypeError):
            days = 0

        decay_factor = math.exp(-DECAY_LAMBDA * days)
        access_count = payload.get("metadata", {}).get("access_count", 0)
        access_factor = math.log(1 + access_count) / math.log(101) if access_count > 0 else 0
        health = decay_factor * 0.7 + access_factor * 0.3

        old_imp = payload.get("importance", 0.5)
        new_imp = round(max(0.0, min(1.0, old_imp * (0.5 + health * 0.5))), 3)

        if new_imp < old_imp - 0.05:
            decayed += 1
            if apply:
                updates.append({"id": p.get("id"),
                                "payload": {"importance": new_imp, "last_decayed": now.isoformat()}})

    if apply and updates:
        # set_payload 批次
        for i in range(0, len(updates), 100):
            batch = updates[i:i+100]
            # Qdrant set payload 需逐點 (用 points selector)
            for u in batch:
                try:
                    qdrant("POST", f"/collections/{UNIFIED}/points/payload", {
                        "payload": {"importance": u["payload"]["importance"],
                                    "last_decayed": u["payload"]["last_decayed"]},
                        "points": [u["id"]],
                    })
                except Exception:
                    pass

    elapsed = time.time() - t0
    print(f"   {'📉 已降權' if apply else '🔍 偵測需降權'}: {decayed}", flush=True)
    print(f"   耗時: {elapsed:.1f}s", flush=True)
    return {"decayed_target": decayed, "applied": apply,
            "applied_count": len(updates) if apply else 0}


# ─────────────────────────────────────────────────────────
# contradict (最佳努力,目前用關鍵詞偵測)
# ─────────────────────────────────────────────────────────
# 反義詞對 (中文 + 英文,可擴充)
CONTRADICTION_PAIRS = [
    ("是", "不是"), ("對", "錯"), ("成功", "失敗"),
    ("已安裝", "未安裝"), ("完成", "未完成"), ("啟用", "停用"),
    ("true", "false"), ("yes", "no"), ("works", "broken"),
    ("fixed", "broken"), ("enabled", "disabled"),
]


def run_contradict() -> dict:
    """偵測矛盾記憶 (關鍵詞反義對,最佳努力)。"""
    print(f"⚠️ contradict (dry_run only)", flush=True)
    t0 = time.time()
    points = scroll_all(max_points=10000)

    # 建 source_path → records 索引 (同主題才比)
    by_topic: dict[str, list[dict]] = {}
    for p in points:
        path = p.get("payload", {}).get("source_path", "")
        topic = path.split("/")[0] if path else "unknown"
        by_topic.setdefault(topic, []).append(p)

    contradictions = []
    for topic, group in by_topic.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i+1, len(group)):
                c1 = group[i].get("payload", {}).get("content", "").lower()
                c2 = group[j].get("payload", {}).get("content", "").lower()
                for a, b in CONTRADICTION_PAIRS:
                    if (a in c1 and b in c2) or (b in c1 and a in c2):
                        # 進一步確認不是同一句
                        if abs(len(c1) - len(c2)) < 200:
                            contradictions.append({
                                "topic": topic,
                                "pair_a": group[i].get("id"),
                                "pair_b": group[j].get("id"),
                                "keyword": f"{a}/{b}",
                            })

    elapsed = time.time() - t0
    print(f"   偵測到 {len(contradictions)} 對潛在矛盾", flush=True)
    if contradictions:
        for c_ in contradictions[:5]:
            print(f"     [{c_['topic']}] {c_['pair_a'][:8]}..↔{c_['pair_b'][:8]}.. ({c_['keyword']})", flush=True)
    print(f"   耗時: {elapsed:.1f}s", flush=True)
    return {"contradictions": len(contradictions), "sample": contradictions[:10]}


# ─────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────
def main():
    import argparse
    p = argparse.ArgumentParser(description="vector-memory-hub 生命週期管理")
    p.add_argument("action", choices=["dedup", "decay", "contradict", "all"],
                   help="執行哪個生命週期任務")
    p.add_argument("--apply", action="store_true", help="真的執行 (預設 dry_run)")
    p.add_argument("--threshold", type=float, default=0.92, help="dedup 相似度閾值")
    args = p.parse_args()

    state = load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print("=" * 50)
    print(f"🔄 lifecycle: {args.action} ({'APPLY' if args.apply else 'DRY RUN'})")
    print("=" * 50)

    results = {}
    if args.action in ("dedup", "all"):
        results["dedup"] = run_dedup(threshold=args.threshold, apply=args.apply)
        state["lifecycle_last_dedup"] = today
    if args.action in ("decay", "all"):
        results["decay"] = run_decay(apply=args.apply)
        state["lifecycle_last_decay"] = today
    if args.action in ("contradict", "all"):
        results["contradict"] = run_contradict()

    state["lifecycle_last_run"] = datetime.now(timezone.utc).isoformat()
    state["lifecycle_last_results"] = results
    save_state(state)

    # 統計
    try:
        info = qdrant("GET", f"/collections/{UNIFIED}").get("result", {})
        print(f"\n📊 unified_mem: {info.get('points_count')} pts (status={info.get('status')})", flush=True)
    except Exception:
        pass


if __name__ == "__main__":
    main()
