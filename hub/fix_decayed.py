#!/usr/bin/env python3
"""一次性修復:回復被 buggy decay 公式誤降的 importance。

buggy decay (舊版) 用 (0.5 + health * 0.5) 把 importance 砍半,
這個腳本對有 last_decayed 欄位的點,把 importance 回復到合理範圍。
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lifecycle import qdrant, UNIFIED, scroll_all

print("🔧 修復被誤降的 importance...", flush=True)
points = scroll_all(max_points=20000)
fixed = 0
for p in points:
    payload = p.get("payload", {})
    if "last_decayed" not in payload:
        continue    # 沒被 decay 過的跳過
    old_imp = payload.get("importance", 0.5)
    # 舊公式 multiplier ≈ 0.5-0.7,反推原值: old / 0.65 (取中位數)
    # 但避免超過 1.0,且原本就低的不要拉太高
    est_original = min(1.0, old_imp / 0.65)
    # 只回復「明顯被砍半」的 (old < 0.5 且 est > old + 0.1)
    if old_imp < 0.5 and est_original > old_imp + 0.1:
        new_imp = round(est_original, 3)
        try:
            qdrant("POST", f"/collections/{UNIFIED}/points/payload", {
                "payload": {"importance": new_imp, "decay_fixed": True},
                "points": [p.get("id")],
            })
            fixed += 1
        except Exception:
            pass

print(f"✓ 修復 {fixed} 點的 importance", flush=True)
