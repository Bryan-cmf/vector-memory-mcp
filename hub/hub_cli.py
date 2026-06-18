#!/usr/bin/env python3
"""vector-memory-hub CLI — 控制 daemon + 採集 + 狀態查詢。

Usage:
    vector-memory-hub start           # 啟動 daemon (launchd)
    vector-memory-hub stop            # 停止 daemon
    vector-memory-hub status          # 查 daemon + 各 connector 狀態
    vector-memory-hub run-once        # 手動跑一次採集 (前景)
    vector-memory-hub list-connectors # 顯示偵測到的 connector
    vector-memory-hub logs            # 看 daemon log (tail)
    vector-memory-hub config          # 顯示/編輯 config

設定檔: ~/.vector-memory-mcp/hub-config.yml (不存在則用預設)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HUB_DIR = Path(__file__).parent.resolve()
VENV_PYTHON = os.environ.get(
    "HUB_PYTHON",
    "/Users/Claw/.zcode/skills/vector-memory/.venv/bin/python",
)
VM_DIR = Path(os.environ.get("VECTOR_MEMORY_DIR", str(Path.home() / ".vector-memory-mcp")))
STATE_FILE = VM_DIR / "hub-state.json"
CONFIG_FILE = VM_DIR / "hub-config.yml"
LAUNCHD_LABEL = "com.vector-memory.hub"
LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
DAEMON_LOG = VM_DIR / "hub-daemon.log"


# ─────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────
def c(color: str, msg: str) -> str:
    colors = {"g": "\033[32m", "y": "\033[33m", "r": "\033[31m", "b": "\033[34m", "d": "\033[2m", "x": "\033[0m"}
    return f"{colors.get(color,'')}{msg}{colors['x']}" if sys.stdout.isatty else msg


def gen_launchd_plist() -> str:
    """產生 launchd plist 內容。"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{VENV_PYTHON}</string>
    <string>{HUB_DIR / 'hub_daemon.py'}</string>
    <string>--interval</string>
    <string>15</string>
  </array>
  <key>WorkingDirectory</key><string>{HUB_DIR}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>QDRANT_URL</key><string>{os.environ.get('QDRANT_URL','http://localhost:6333')}</string>
    <key>VECTOR_MEMORY_DIR</key><string>{VM_DIR}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{DAEMON_LOG}</string>
  <key>StandardErrorPath</key><string>{DAEMON_LOG}</string>
</dict>
</plist>
"""


def gen_systemd_unit() -> str:
    """產生 systemd user unit (Linux)。"""
    return f"""[Unit]
Description=vector-memory-hub daemon
After=network.target

[Service]
Type=simple
ExecStart={VENV_PYTHON} {HUB_DIR}/hub_daemon.py --interval 15
WorkingDirectory={HUB_DIR}
Environment=QDRANT_URL={os.environ.get('QDRANT_URL','http://localhost:6333')}
Environment=VECTOR_MEMORY_DIR={VM_DIR}
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
"""


# ─────────────────────────────────────────────────────────
# subcommands
# ─────────────────────────────────────────────────────────
def cmd_start():
    """啟動 daemon (macOS launchd / Linux systemd)。"""
    VM_DIR.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        plist_dir = LAUNCHD_PLIST.parent
        plist_dir.mkdir(parents=True, exist_ok=True)
        LAUNCHD_PLIST.write_text(gen_launchd_plist())
        # unload 若已存在,再 load
        subprocess.run(["launchctl", "unload", str(LAUNCHD_PLIST)], capture_output=True)
        r = subprocess.run(["launchctl", "load", str(LAUNCHD_PLIST)])
        if r.returncode == 0:
            print(c("g", "✓ daemon 已啟動 (launchd)"))
            print(f"  plist: {LAUNCHD_PLIST}")
            print(f"  log:   {DAEMON_LOG}")
            print(f"  停止:  vector-memory-hub stop")
        else:
            print(c("r", f"✗ launchctl load 失敗 (rc={r.returncode})"))
    else:
        # Linux systemd user
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit_file = unit_dir / f"{LAUNCHD_LABEL}.service"
        unit_file.write_text(gen_systemd_unit())
        subprocess.run(["systemctl", "--user", "daemon-reload"])
        r = subprocess.run(["systemctl", "--user", "enable", "--now", LAUNCHD_LABEL])
        print(c("g" if r.returncode == 0 else "r",
                f"{'✓' if r.returncode==0 else '✗'} systemd service ({LAUNCHD_LABEL})"))


def cmd_stop():
    """停止 daemon。"""
    if sys.platform == "darwin":
        if LAUNCHD_PLIST.exists():
            subprocess.run(["launchctl", "unload", str(LAUNCHD_PLIST)], capture_output=True)
            print(c("g", "✓ daemon 已停止 (launchd unloaded)"))
        else:
            print(c("y", "⚠️ plist 不存在,可能從未 start"))
    else:
        subprocess.run(["systemctl", "--user", "stop", LAUNCHD_LABEL], capture_output=True)
        subprocess.run(["systemctl", "--user", "disable", LAUNCHD_LABEL], capture_output=True)
        print(c("g", "✓ systemd service stopped"))


def cmd_status():
    """顯示 daemon + connector 狀態。"""
    # 1. daemon 進程
    print(c("b", "Daemon 狀態"))
    if sys.platform == "darwin":
        r = subprocess.run(["launchctl", "list", LAUNCHD_LABEL], capture_output=True, text=True)
        if r.returncode == 0:
            print(c("g", "  ✓ 運行中 (launchd)"))
        else:
            print(c("y", "  ○ 未運行"))
    else:
        r = subprocess.run(["systemctl", "--user", "is-active", LAUNCHD_LABEL], capture_output=True, text=True)
        active = r.stdout.strip() == "active"
        print(c("g" if active else "y", f"  {'✓ active' if active else '○ inactive'}"))

    # 2. 最近 log
    print()
    print(c("b", "最近 log (5 行)"))
    if DAEMON_LOG.exists():
        lines = DAEMON_LOG.read_text(errors="replace").splitlines()[-5:]
        for l in lines:
            print(f"  {c('d', l)}")
    else:
        print(c("d", "  (無 log)"))

    # 3. hub-state
    print()
    print(c("b", "採集狀態"))
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
        for k, v in state.items():
            if k.startswith("mirror_seen_") or k.endswith("_last"):
                print(f"  {k}: {c('d', str(v))}")
    else:
        print(c("d", "  (尚未採集,state 檔不存在)"))

    # 4. unified_mem 點數
    print()
    print(c("b", "unified_mem"))
    try:
        import urllib.request
        info = json.load(urllib.request.urlopen("http://localhost:6333/collections/unified_mem"))["result"]
        print(f"  points: {c('g', str(info['points_count']))}")
    except Exception as e:
        print(c("r", f"  ✗ Qdrant 連不上: {e}"))


def cmd_run_once():
    """手動跑一次採集。"""
    print(c("b", "手動跑一次採集..."))
    r = subprocess.run(
        [VENV_PYTHON, str(HUB_DIR / "collect.py")],
        cwd=str(HUB_DIR),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    sys.exit(r.returncode)


def cmd_list_connectors():
    """列偵測到的 connector。"""
    print(c("b", "偵測 connector..."))
    r = subprocess.run(
        [VENV_PYTHON, str(HUB_DIR / "collect.py"), "--dry-run"],
        cwd=str(HUB_DIR), capture_output=True, text=True,
    )
    print(r.stdout)
    if r.returncode != 0:
        print(c("r", r.stderr[-300:]), file=sys.stderr)


def cmd_logs():
    """tail daemon log。"""
    if not DAEMON_LOG.exists():
        print(c("y", "尚無 log 檔"))
        return
    subprocess.run(["tail", "-n", "50", str(DAEMON_LOG)])


def cmd_config():
    """顯示/建立 config。"""
    print(c("b", "Config"))
    print(f"  設定檔: {CONFIG_FILE}")
    if not CONFIG_FILE.exists():
        print(c("y", "  (不存在,使用預設)"))
        # 建立預設 config
        VM_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text("""# vector-memory-hub 設定
interval_min: 15              # 採集間隔 (分鐘)
qdrant_url: http://localhost:6333
embedding_model: BAAI/bge-m3

# connector 開關 (false 則該 connector 不跑)
connectors:
  qdrant_mirror: true
  markdown_dir: true
  claude_code: true
  cursor: true
  zcode: true

# markdown_dir 額外目錄 (逗號分隔)
memory_extra_dirs: ""

# 隱私
privacy:
  redact_api_keys: true
  redact_credit_cards: true
  redact_emails: false
""")
        print(c("g", "  ✓ 已建立預設 config"))
    else:
        print(CONFIG_FILE.read_text())


def main():
    import argparse
    p = argparse.ArgumentParser(
        prog="vector-memory-hub",
        description="個人 AI 記憶中樞 CLI",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("start", help="啟動 daemon")
    sub.add_parser("stop", help="停止 daemon")
    sub.add_parser("status", help="查狀態")
    sub.add_parser("run-once", help="手動跑一次採集")
    sub.add_parser("list-connectors", help="列偵測到的 connector")
    sub.add_parser("logs", help="看 daemon log")
    sub.add_parser("config", help="顯示/建立 config")
    # 為了 install.sh 用
    sub.add_parser("install-launchd", help="安裝 launchd plist (不啟動)")
    args = p.parse_args()

    if args.cmd == "start":
        cmd_start()
    elif args.cmd == "stop":
        cmd_stop()
    elif args.cmd == "status":
        cmd_status()
    elif args.cmd == "run-once":
        cmd_run_once()
    elif args.cmd == "list-connectors":
        cmd_list_connectors()
    elif args.cmd == "logs":
        cmd_logs()
    elif args.cmd == "config":
        cmd_config()
    elif args.cmd == "install-launchd":
        VM_DIR.mkdir(parents=True, exist_ok=True)
        LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
        LAUNCHD_PLIST.write_text(gen_launchd_plist())
        print(c("g", f"✓ plist 已寫入 {LAUNCHD_PLIST}"))


if __name__ == "__main__":
    main()
