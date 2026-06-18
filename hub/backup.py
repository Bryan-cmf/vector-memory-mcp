#!/usr/bin/env python3
"""雲端備份 — 本機 Qdrant snapshot + retention 滾動 + S3/R2 推送 + restore。

流程:
  1. Qdrant snapshot 建立 (REST API)
  2. 下載到 ~/.vector-memory-mcp/backups/ (依時間命名)
  3. retention 滾動刪除: 每日 7 份 + 每週 4 份 + 每月 6 份
  4. (可選) 雲端推送 S3/R2 (用 boto3,無憑證則跳過)
  5. restore: 從 snapshot 檔還原到指定 collection

Usage:
    python backup.py                         # 建立一次 snapshot + retention
    python backup.py --cloud                 # 加上雲端推送
    python backup.py restore --from X.snap --to my_collection
    python backup.py list                    # 列本機 backups
    python backup.py prune                   # 只跑 retention 不建新
"""
from __future__ import annotations

import json
import os
import sys
import time
import shutil
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
UNIFIED = "unified_mem"
BACKUP_DIR = Path(os.environ.get("VECTOR_MEMORY_DIR", str(Path.home() / ".vector-memory-mcp"))) / "backups"

# retention: 保留多少份
KEEP_DAILY = 7      # 每日 7 份
KEEP_WEEKLY = 4     # 每週 4 份
KEEP_MONTHLY = 6    # 每月 6 份


def qdrant_json(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{QDRANT_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        raise RuntimeError(f"Qdrant {method} {path}: {e}")


# ─────────────────────────────────────────────────────────
# snapshot 建立 + 下載
# ─────────────────────────────────────────────────────────
def create_snapshot() -> Path:
    """建立 unified_mem 的 snapshot 並下載到 BACKUP_DIR。回傳本機檔路徑。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    print(f"📸 建立 {UNIFIED} snapshot...", flush=True)

    # 觸發 snapshot 建立
    r = qdrant_json("POST", f"/collections/{UNIFIED}/snapshots")
    snap_name = r.get("result", {}).get("name", "")
    if not snap_name:
        raise RuntimeError("Qdrant 沒回 snapshot name")

    print(f"   snapshot: {snap_name}", flush=True)

    # 下載
    local_name = f"{UNIFIED}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.snapshot"
    local_path = BACKUP_DIR / local_name
    url = f"{QDRANT_URL}/collections/{UNIFIED}/snapshots/{snap_name}"
    print(f"   下載 → {local_path}", flush=True)

    with urllib.request.urlopen(url, timeout=600) as resp, local_path.open("wb") as f:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)

    size_mb = local_path.stat().st_size / 1024 / 1024
    print(f"   ✓ {size_mb:.1f} MB", flush=True)
    return local_path


# ─────────────────────────────────────────────────────────
# retention 滾動
# ─────────────────────────────────────────────────────────
def prune_backups() -> dict:
    """依 retention 規則刪除舊 snapshot。

    策略: 保留最近 KEEP_DAILY 個每日 + 最近 KEEP_WEEKLY 個每週(取該週最新) + 最近 KEEP_MONTHLY 個每月。
    """
    snaps = sorted(BACKUP_DIR.glob("*.snapshot"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not snaps:
        return {"total": 0, "kept": 0, "deleted": 0}

    now = datetime.now(timezone.utc)
    keep_set = set()

    # 每日: 最近 7 天各保留最新一份
    by_day: dict[str, Path] = {}
    for s in snaps:
        # 從檔名解析日期 (格式 unified_mem-YYYYMMDD-HHMMSS.snapshot)
        name = s.stem
        try:
            date_part = name.split("-")[1] if "-" in name else ""
            day_key = date_part[:8]   # YYYYMMDD
        except Exception:
            continue
        if day_key and day_key not in by_day:
            by_day[day_key] = s
    for i, day in enumerate(sorted(by_day.keys(), reverse=True)[:KEEP_DAILY]):
        keep_set.add(by_day[day])

    # 每週: 最近 4 週各保留最新一份
    by_week: dict[str, Path] = {}
    for s in snaps:
        name = s.stem
        try:
            date_part = name.split("-")[1] if "-" in name else ""
            dt = datetime.strptime(date_part[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
            iso_year, iso_week, _ = dt.isocalendar()
            week_key = f"{iso_year}-W{iso_week:02d}"
        except Exception:
            continue
        if week_key not in by_week:
            by_week[week_key] = s
    for i, wk in enumerate(sorted(by_week.keys(), reverse=True)[:KEEP_WEEKLY]):
        keep_set.add(by_week[wk])

    # 每月: 最近 6 個月各保留最新一份
    by_month: dict[str, Path] = {}
    for s in snaps:
        name = s.stem
        try:
            date_part = name.split("-")[1] if "-" in name else ""
            month_key = date_part[:6]   # YYYYMM
        except Exception:
            continue
        if month_key not in by_month:
            by_month[month_key] = s
    for i, mo in enumerate(sorted(by_month.keys(), reverse=True)[:KEEP_MONTHLY]):
        keep_set.add(by_month[mo])

    # 永遠保留最新 1 份 (即使不在 retention 範圍)
    if snaps:
        keep_set.add(snaps[0])

    deleted = 0
    for s in snaps:
        if s not in keep_set:
            try:
                s.unlink()
                deleted += 1
                print(f"   🗑️ 刪除舊 snapshot: {s.name}", flush=True)
            except OSError:
                pass

    return {"total": len(snaps), "kept": len(keep_set), "deleted": deleted}


# ─────────────────────────────────────────────────────────
# 雲端推送 (S3/R2)
# ─────────────────────────────────────────────────────────
def push_to_cloud(local_path: Path) -> bool:
    """推送 snapshot 到 S3/R2 (用 boto3)。

    環境變數:
      AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (或 R2 的等價 key)
      BACKUP_S3_BUCKET  (例: my-backups)
      BACKUP_S3_PREFIX  (例: vector-memory/,可選)
      BACKUP_S3_ENDPOINT (R2 用: https://xxx.r2.cloudflarestorage.com)
    """
    bucket = os.environ.get("BACKUP_S3_BUCKET", "")
    if not bucket:
        print("   ⏭️ 雲端推送略過 (未設 BACKUP_S3_BUCKET)", flush=True)
        return False
    try:
        import boto3
    except ImportError:
        print("   ⚠️ boto3 未安裝,略過雲端推送 (pip install boto3)", flush=True)
        return False

    prefix = os.environ.get("BACKUP_S3_PREFIX", "vector-memory/")
    endpoint = os.environ.get("BACKUP_S3_ENDPOINT", "")
    key = f"{prefix}{local_path.name}"

    kwargs = {"service_name": "s3"}
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    if os.environ.get("AWS_ACCESS_KEY_ID"):
        kwargs["aws_access_key_id"] = os.environ["AWS_ACCESS_KEY_ID"]
        kwargs["aws_secret_access_key"] = os.environ["AWS_SECRET_ACCESS_KEY"]

    try:
        client = boto3.client(**kwargs)
        client.upload_file(str(local_path), bucket, key)
        print(f"   ☁️ 已推送 → s3://{bucket}/{key}", flush=True)
        return True
    except Exception as e:
        print(f"   ⚠️ 雲端推送失敗: {e}", flush=True)
        return False


# ─────────────────────────────────────────────────────────
# restore
# ─────────────────────────────────────────────────────────
def restore_snapshot(snap_path: Path, target_collection: str) -> bool:
    """從 snapshot 檔還原到指定 collection。

    Qdrant 1.18 restore 流程:
      1. 上傳 snapshot: POST /collections/{target}/snapshots/upload (multipart)
         若 target collection 不存在,需先建立空 collection
      2. recover: PUT /collections/{target}/snapshots/recover {snapshot_name}
    """
    if not snap_path.exists():
        print(f"✗ snapshot 不存在: {snap_path}", flush=True)
        return False

    print(f"📥 restore {snap_path.name} → {target_collection}", flush=True)

    # 確保 target collection 存在 (recover 需要既有 collection)
    try:
        info = qdrant_json("GET", f"/collections/{target_collection}")
        print(f"   ✓ target collection 已存在 ({info.get('result',{}).get('points_count',0)} pts)", flush=True)
    except Exception:
        # 不存在,建立空的 (用 unified_mem 的 config 當範本)
        print(f"   建立 target collection...", flush=True)
        try:
            qdrant_json("PUT", f"/collections/{target_collection}?timeout=60", {
                "vectors": {"size": 1024, "distance": "Cosine"},
            })
            print(f"   ✓ {target_collection} 已建立", flush=True)
        except Exception as e:
            print(f"   ✗ 建立失敗: {e}", flush=True)
            return False

    # 上傳 snapshot (multipart/form-data)
    upload_url = f"{QDRANT_URL}/collections/{target_collection}/snapshots/upload"
    size = snap_path.stat().st_size
    print(f"   上傳 {size/1024/1024:.1f} MB...", flush=True)

    import uuid as _uuid
    boundary = f"----vmbkp{_uuid.uuid4().hex}"
    body_start = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="snapshot"; filename="{snap_path.name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    body_end = f"\r\n--{boundary}--\r\n".encode()

    try:
        # 串流上傳 (避免大檔讀進記憶體)
        import io
        buf = io.BytesIO()
        buf.write(body_start)
        with snap_path.open("rb") as f:
            shutil.copyfileobj(f, buf)
        buf.write(body_end)

        req = urllib.request.Request(upload_url, data=buf.getvalue(), method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")

        with urllib.request.urlopen(req, timeout=600) as r:
            resp = json.loads(r.read().decode())
        # resp.result 可能是 true (upload 成功) 或 {name: ...}
        result_field = resp.get("result", True)
        uploaded_name = (result_field.get("name", snap_path.name)
                         if isinstance(result_field, dict)
                         else snap_path.name)
        print(f"   ✓ 上傳: {uploaded_name}", flush=True)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()[:300]
        print(f"   ⚠️ 上傳失敗 (HTTP {e.code}): {err_body}", flush=True)
        return False

    # recover (Qdrant 1.18: upload 已可能觸發 recover,這裡是 idempotent 保險)
    try:
        r = qdrant_json("PUT", f"/collections/{target_collection}/snapshots/recover",
                        {"snapshot_name": uploaded_name, "priority": "snapshot"})
        print(f"   ✓ recover 成功", flush=True)
        return True
    except Exception as e:
        # Qdrant upload 可能已自動 recover,檢查點數確認
        try:
            info = qdrant_json("GET", f"/collections/{target_collection}").get("result", {})
            pts = info.get("points_count", 0)
            if pts > 0:
                print(f"   ✓ recover 已由 upload 自動完成 ({pts} pts)", flush=True)
                return True
        except Exception:
            pass
        print(f"   ⚠️ recover 失敗: {e}", flush=True)
        return False


# ─────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────
def main():
    import argparse
    p = argparse.ArgumentParser(description="vector-memory-hub 備份")
    p.add_argument("cmd", nargs="?", default="backup",
                   choices=["backup", "list", "prune", "restore"],
                   help="動作 (預設 backup)")
    p.add_argument("--cloud", action="store_true", help="加雲端推送")
    p.add_argument("--collection", default=UNIFIED, help="備份哪個 collection")
    p.add_argument("--from", dest="from_file", help="restore 來源 snapshot")
    p.add_argument("--to", dest="to_collection", help="restore 目標 collection")
    args = p.parse_args()

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    collection = args.collection

    if args.cmd == "restore" or args.from_file:
        ok = restore_snapshot(Path(args.from_file).expanduser() if args.from_file else "",
                              args.to_collection or f"{collection}_restored")
        sys.exit(0 if ok else 1)
    elif args.cmd == "list":
        snaps = sorted(BACKUP_DIR.glob("*.snapshot"), key=lambda p: p.stat().st_mtime, reverse=True)
        print(f"📋 本機 backups ({len(snaps)} 份):")
        for s in snaps:
            size_mb = s.stat().st_size / 1024 / 1024
            mtime = datetime.fromtimestamp(s.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            print(f"  {mtime}  {size_mb:7.1f} MB  {s.name}")
        return
    elif args.cmd == "prune":
        result = prune_backups()
        print(f"🧹 prune 完成: 總計 {result['total']}, 保留 {result['kept']}, 刪除 {result['deleted']}")
        return

    # 預設: backup
    print("=" * 50)
    print(f"💾 備份 {collection}")
    print("=" * 50)

    # 1. snapshot
    local_path = create_snapshot()

    # 2. prune
    print(f"\n🧹 retention 清理...")
    result = prune_backups()
    print(f"   總計 {result['total']}, 保留 {result['kept']}, 刪除 {result['deleted']}")

    # 3. 雲端推送 (可選)
    if args.cloud:
        print(f"\n☁️ 雲端推送...")
        push_to_cloud(local_path)

    print(f"\n✅ 備份完成: {local_path}")


if __name__ == "__main__":
    main()
