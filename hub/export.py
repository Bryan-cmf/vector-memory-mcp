#!/usr/bin/env python3
"""記憶匯出 — JSONL / Markdown / CSV / Qdrant snapshot / OpenAI fine-tune 格式。

支援篩選: --agent --since --type --tag --min-importance --limit

Usage:
    python export.py --format jsonl -o backup.jsonl
    python export.py --format md --agent claude --since 2026-06-01 -o claude.md
    python export.py --format csv --type conversation -o convs.csv
    python export.py --format finetune --agent openclaw -o dataset.jsonl
    python export.py --format snapshot -o full.snapshot    # Qdrant 完整備份
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
UNIFIED = "unified_mem"


def qdrant(method: str, path: str, body: dict | None = None, stream: bool = False):
    url = f"{QDRANT_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=120)


def qdrant_json(method: str, path: str, body: dict | None = None) -> dict:
    with qdrant(method, path, body) as r:
        return json.loads(r.read().decode())


def scroll_filtered(filters: dict, limit: int = 50000) -> list[dict]:
    """scroll unified_mem,套用篩選條件 (must 條件)。"""
    points = []
    offset = None
    must = []
    if filters.get("agent"):
        must.append({"key": "source_agent", "match": {"value": filters["agent"]}})
    if filters.get("type"):
        must.append({"key": "source_type", "match": {"value": filters["type"]}})
    if filters.get("tag"):
        must.append({"key": "tags", "match": {"value": filters["tag"]}})

    while len(points) < limit:
        body: dict = {"limit": min(500, limit - len(points)),
                      "with_payload": True, "with_vector": False}
        if must:
            body["filter"] = {"must": must}
        if offset:
            body["offset"] = offset
        r = qdrant_json("POST", f"/collections/{UNIFIED}/points/scroll", body)
        res = r.get("result", {})
        batch = res.get("points", [])
        if not batch:
            break

        # 客戶端再過濾 (since / min-importrest Qdrant 不直接支援 range on payload)
        for p in batch:
            pl = p.get("payload", {})
            if filters.get("since"):
                created = pl.get("created_at", "")[:10]
                if created < filters["since"]:
                    continue
            if filters.get("min_importance") is not None:
                if pl.get("importance", 0) < filters["min_importance"]:
                    continue
            points.append(p)

        offset = res.get("next_page_offset")
        if offset is None:
            break
    return points


# ─────────────────────────────────────────────────────────
# 格式輸出
# ─────────────────────────────────────────────────────────
def to_jsonl(points: list[dict], out: Path) -> int:
    """每行一個 JSON (含 id + payload)。"""
    n = 0
    with out.open("w", encoding="utf-8") as f:
        for p in points:
            obj = {"id": p.get("id"), **p.get("payload", {})}
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            n += 1
    return n


def to_markdown(points: list[dict], out: Path) -> int:
    """人類可讀 Markdown,按 source_agent 分節。"""
    by_agent: dict[str, list] = {}
    for p in points:
        pl = p.get("payload", {})
        a = pl.get("source_agent", "unknown")
        by_agent.setdefault(a, []).append(p)

    n = 0
    with out.open("w", encoding="utf-8") as f:
        f.write(f"# vector-memory 匯出\n\n")
        f.write(f"- 匯出時間: {datetime.now().isoformat()}\n")
        f.write(f"- 記錄數: {len(points)}\n")
        f.write(f"- agents: {', '.join(sorted(by_agent.keys()))}\n\n---\n\n")

        for agent in sorted(by_agent.keys()):
            group = by_agent[agent]
            f.write(f"## {agent} ({len(group)} 筆)\n\n")
            for p in sorted(group, key=lambda x: x.get("payload", {}).get("created_at", "")):
                pl = p.get("payload", {})
                ts = pl.get("created_at", "")[:19]
                content = pl.get("content", "").strip()
                tags = ", ".join(pl.get("tags", []))
                f.write(f"### [{ts}] {pl.get('source_type', '')}\n\n")
                f.write(f"{content}\n\n")
                if tags:
                    f.write(f"*tags: {tags}*\n")
                f.write(f"*path: `{pl.get('source_path', '')}`* · *importance: {pl.get('importance', 0)}*\n\n---\n\n")
                n += 1
    return n


def to_csv(points: list[dict], out: Path) -> int:
    """CSV: id, created_at, source_agent, source_type, content, tags, importance。"""
    n = 0
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "created_at", "source_agent", "source_type",
                    "source_path", "content", "tags", "importance"])
        for p in points:
            pl = p.get("payload", {})
            w.writerow([
                p.get("id", ""),
                pl.get("created_at", ""),
                pl.get("source_agent", ""),
                pl.get("source_type", ""),
                pl.get("source_path", ""),
                pl.get("content", "").replace("\n", " ⏎ ")[:5000],
                "|".join(pl.get("tags", [])),
                pl.get("importance", 0),
            ])
            n += 1
    return n


def to_finetune(points: list[dict], out: Path) -> int:
    """OpenAI fine-tune 對話格式: {"messages": [{role, content}]}。

    把同 source_path 的記錄按時間串成一段對話。
    適合把記憶轉成微調資料集 (讓模型學會用戶的問答風格)。
    """
    # 群組化: source_path → 時間排序的 content list
    by_path: dict[str, list] = {}
    for p in points:
        pl = p.get("payload", {})
        path = pl.get("source_path", "")
        role = "assistant" if pl.get("source_type") == "decision" else "user"
        by_path.setdefault(path, []).append({
            "role": role,
            "content": pl.get("content", ""),
            "ts": pl.get("created_at", ""),
        })

    n = 0
    with out.open("w", encoding="utf-8") as f:
        for path, msgs in by_path.items():
            msgs.sort(key=lambda x: x["ts"])
            # 至少 2 輪才有微調價值
            if len(msgs) < 2:
                continue
            obj = {"messages": [{"role": m["role"], "content": m["content"]} for m in msgs[:20]]}
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            n += 1
    return n


def to_snapshot(out: Path) -> int:
    """Qdrant 完整 snapshot (含向量,可在別台 Qdrant 還原)。

    走 Qdrant 的 snapshot API,下載 binary。
    """
    # 觸發 snapshot 建立
    r = qdrant_json("POST", f"/collections/{UNIFIED}/snapshots")
    snap_name = r.get("result", {}).get("name", "")
    if not snap_name:
        raise RuntimeError("無法建立 snapshot")

    # 下載 snapshot
    url = f"{QDRANT_URL}/collections/{UNIFIED}/snapshots/{snap_name}"
    with urllib.request.urlopen(url, timeout=300) as resp, out.open("wb") as f:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)
    size = out.stat().st_size
    return size


# ─────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────
def main():
    import argparse
    p = argparse.ArgumentParser(description="vector-memory-hub 匯出")
    p.add_argument("--format", required=True,
                   choices=["jsonl", "md", "csv", "finetune", "snapshot"],
                   help="匯出格式")
    p.add_argument("-o", "--output", required=True, help="輸出檔路徑")
    p.add_argument("--agent", default="", help="只匯某 source_agent")
    p.add_argument("--since", default="", help="只匯某日期之後 (YYYY-MM-DD)")
    p.add_argument("--type", default="", help="只匯某 source_type")
    p.add_argument("--tag", default="", help="只匯含某 tag")
    p.add_argument("--min-importance", type=float, default=None, help="最低 importance")
    p.add_argument("--limit", type=int, default=50000, help="最多匯出幾筆")
    args = p.parse_args()

    out = Path(args.output).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"📤 匯出: format={args.format} → {out}", flush=True)

    # snapshot 不需 scroll,直接走 Qdrant API
    if args.format == "snapshot":
        size = to_snapshot(out)
        print(f"✓ snapshot: {out} ({size/1024/1024:.1f} MB)", flush=True)
        return

    # 其他格式: 先 scroll 帶篩選
    filters = {
        "agent": args.agent, "since": args.since, "type": args.type,
        "tag": args.tag, "min_importance": args.min_importance,
    }
    print(f"   篩選: {filters}", flush=True)
    points = scroll_filtered(filters, limit=args.limit)
    print(f"   符合條件: {len(points)} 筆", flush=True)

    if not points:
        print("⚠️ 無資料符合條件", flush=True)
        return

    writers = {
        "jsonl": to_jsonl, "md": to_markdown, "csv": to_csv, "finetune": to_finetune,
    }
    n = writers[args.format](points, out)
    print(f"✓ 已匯出 {n} 筆 → {out}", flush=True)


if __name__ == "__main__":
    main()
