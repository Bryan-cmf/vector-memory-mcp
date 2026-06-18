#!/usr/bin/env python3
"""vector-memory-hub 常駐 Daemon。

每 N 分鐘跑一次採集 + 生命週期任務 (dedup/decay)。
非 fork 型 daemon: 單進程 while loop,靠 launchd/systemd 管理生命週期。

Usage:
    python hub_daemon.py                # 前景跑 (launchd 會呼叫這個)
    python hub_daemon.py --once         # 跑一次就退出 (等於 run-once)
    python hub_daemon.py --interval 30  # 自訂間隔 (分鐘)
"""
from __future__ import annotations

import os
import signal
import sys
import time
import subprocess
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

HUB_DIR = Path(__file__).parent
VENV_PYTHON = os.environ.get(
    "HUB_PYTHON",
    "/Users/Claw/.zcode/skills/vector-memory/.venv/bin/python",
)
STATE_FILE = Path(os.environ.get("VECTOR_MEMORY_DIR", str(Path.home() / ".vector-memory-mcp"))) / "hub-state.json"
DEFAULT_INTERVAL = int(os.environ.get("HUB_INTERVAL_MIN", "15"))   # 分鐘

_running = True


def _sigterm(signum, frame):
    global _running
    _running = False
    print(f"\n📡 收到 signal {signum},準備結束...", flush=True)


signal.signal(signal.SIGTERM, _sigterm)
signal.signal(signal.SIGINT, _sigterm)


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def run_collect() -> dict:
    """跑一次採集,回傳統計。"""
    log("🔄 跑採集...")
    try:
        # 用 Popen + 即時讀 stdout/stderr,讓 log 即時寫入 (避免 capture_output 等到結束)
        proc = subprocess.Popen(
            [VENV_PYTHON, "-u", str(HUB_DIR / "collect.py")],
            cwd=str(HUB_DIR),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            text=True,
        )
        stdout_lines = []
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    log(f"   {line}")
                    stdout_lines.append(line)
        except KeyboardInterrupt:
            proc.terminate()
        proc.wait(timeout=1800)   # 30 min 上限
        ok = proc.returncode == 0
        log(f"   {'✓' if ok else '✗'} collect {'OK' if ok else 'FAIL'} (rc={proc.returncode})")
        return {"ok": ok, "stdout_tail": "\n".join(stdout_lines[-5:])}
    except subprocess.TimeoutExpired:
        log("   ✗ collect 超時 (30min)")
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        log(f"   ✗ collect 異常: {e}")
        return {"ok": False, "error": str(e)}


def run_lifecycle_if_due(state: dict) -> None:
    """若到期,跑生命週期任務 (dedup 每天、decay 每週)。

    階段 4 會實作 lifecycle.py,這裡先留 hook (state 記錄即可,實際呼叫在階段 4 接上)。
    """
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    last_dedup = state.get("lifecycle_last_dedup", "")
    if last_dedup != today:
        log(f"📅 dedup 到期 (last={last_dedup}, today={today})")
        try:
            # 嘗試呼叫 lifecycle.py (階段 4 提供)
            lifecycle = HUB_DIR / "lifecycle.py"
            if lifecycle.exists():
                r = subprocess.run(
                    [VENV_PYTHON, str(lifecycle), "dedup"],
                    cwd=str(HUB_DIR), capture_output=True, text=True, timeout=600,
                )
                state["lifecycle_last_dedup"] = today
                log(f"   ✓ dedup rc={r.returncode}")
            else:
                # lifecycle.py 還沒寫,先標記今日已嘗試避免每分鐘重試
                state["lifecycle_last_dedup"] = today
                log("   ⏭️ lifecycle.py 尚未實作 (階段 4),跳過")
        except Exception as e:
            log(f"   ⚠️ dedup 失敗: {e}")


def main():
    import argparse
    p = argparse.ArgumentParser(description="vector-memory-hub daemon")
    p.add_argument("--once", action="store_true", help="跑一次就退出")
    p.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="間隔分鐘數")
    args = p.parse_args()

    log("=" * 50)
    log(f"🧠 vector-memory-hub daemon 啟動")
    log(f"   interval: {args.interval} min")
    log(f"   venv python: {VENV_PYTHON}")
    log(f"   state: {STATE_FILE}")
    log("=" * 50)

    cycle = 0
    while _running:
        cycle += 1
        log(f"\n--- cycle #{cycle} ---")

        # 1. 採集
        result = run_collect()

        # 2. 生命週期 (若到期)
        try:
            import json
            state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        except Exception:
            state = {}
        run_lifecycle_if_due(state)

        if args.once:
            log("📦 --once 模式,結束")
            break

        # 3. 等下一輪 (可被 signal 中斷)
        log(f"😴 休眠 {args.interval} 分鐘 (Ctrl+C 結束)...")
        sleep_total = args.interval * 60
        slept = 0
        while _running and slept < sleep_total:
            time.sleep(10)
            slept += 10

    log("👋 daemon 結束")


if __name__ == "__main__":
    main()
