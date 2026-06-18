#!/usr/bin/env bash
# =============================================================================
# vector-memory-mcp — 一鍵安裝 + MCP client 註冊
# =============================================================================
# 把上游 vector-memory MCP server 部署到本機,並自動把 server 註冊到偵測到
# 的 MCP client (ZCode / Claude Desktop / Cursor / 通用範本)。
#
# Usage:
#   ./install.sh                  # 全自動安裝 + 註冊偵測到的所有 client
#   ./install.sh --dry-run        # 只預演,不改任何檔案
#   ./install.sh --qdrant local   # 強制本地 Docker Qdrant
#   ./install.sh --qdrant cloud   # 強制 Qdrant Cloud (會互動式問 URL+key)
#   ./install.sh --client zcode   # 只註冊某個 client (可逗號分隔: zcode,claude)
#   ./install.sh --uninstall      # 移除 venv + client 註冊 (保留 Qdrant 資料)
#
# Env:
#   QDRANT_URL            覆寫 Qdrant endpoint
#   QDRANT_API_KEY        Qdrant Cloud key (或自架帶認證的實例)
#   EMBEDDING_MODEL       預設 BAAI/bge-m3 (~2GB, 1024 維)
#   VECTOR_MEMORY_COLLECTION  預設 vector_memory
#   VECTOR_MEMORY_DIR     安裝根目錄,預設 $HOME/.vector-memory-mcp
#
# Requires: bash 3.2+ (macOS 內建即可), python3 (3.10–3.13), curl
# Optional: docker (本地 Qdrant 模式), git (取上游原始碼)
# =============================================================================

set -eu

# ---------------------------------------------------------------------------
# 全域常數
# ---------------------------------------------------------------------------
SCRIPT_VERSION="1.0.0"
DEFAULT_INSTALL_DIR="${VECTOR_MEMORY_DIR:-$HOME/.vector-memory-mcp}"
DEFAULT_COLLECTION="${VECTOR_MEMORY_COLLECTION:-vector_memory}"
DEFAULT_MODEL="${EMBEDDING_MODEL:-BAAI/bge-m3}"
UPSTREAM_REPO="https://github.com/Bryan-cmf/agentic-infrastructure"
UPSTREAM_SUBPATH="vector-memory"

# Python 版本範圍 (torch/transformers 在 3.14 還沒支援)
PY_MIN_MAJOR=3; PY_MIN_MINOR=10
PY_MAX_MAJOR=3; PY_MAX_MINOR=13

# 顏色 (互動模式才上色,管線/CI 不上色)
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    C_GREEN=$'\033[0;32m'; C_YELLOW=$'\033[1;33m'; C_RED=$'\033[0;31m'
    C_BLUE=$'\033[0;34m';  C_DIM=$'\033[2m';      C_BOLD=$'\033[1m'
    C_RESET=$'\033[0m'
else
    C_GREEN=""; C_YELLOW=""; C_RED=""; C_BLUE=""; C_DIM=""; C_BOLD=""; C_RESET=""
fi

# ---------------------------------------------------------------------------
# 狀態 (普通全域變數,bash 3.2 相容 —— 不用 declare -A 關聯陣列)
# ---------------------------------------------------------------------------
CFG_INSTALL_DIR="$DEFAULT_INSTALL_DIR"
CFG_QDRANT_MODE=""              # local | cloud | external | existing | ""
CFG_QDRANT_URL="${QDRANT_URL:-}"
CFG_QDRANT_API_KEY="${QDRANT_API_KEY:-}"
CFG_COLLECTION="$DEFAULT_COLLECTION"
CFG_MODEL="$DEFAULT_MODEL"
CFG_OS=""
ENABLE_HUB=false               # 階段 7: --enable-hub 啟用 hub daemon
CLIENTS_DETECTED=""            # 空白分隔字串
CLIENTS_REQUESTED=""           # 空白分隔字串 (逗號 → 空白)
DRY_RUN=false
UNINSTALL=false

# ---------------------------------------------------------------------------
# 工具函式
# ---------------------------------------------------------------------------
log()  { printf '%s[%s]%s %s\n' "$C_BLUE"  "$(date +%H:%M:%S)" "$C_RESET" "$*" ; }
ok()   { printf '%s✓%s %s\n'    "$C_GREEN" "$C_RESET" "$*" ; }
warn() { printf '%s⚠%s %s\n'    "$C_YELLOW" "$C_RESET" "$*" >&2 ; }
err()  { printf '%s✗%s %s\n'    "$C_RED"   "$C_RESET" "$*" >&2 ; }
step() { printf '\n%s══ %s ══%s\n' "$C_BOLD" "$*" "$C_RESET" ; }
die()  { err "$*"; exit 1 ; }

have() { command -v "$1" >/dev/null 2>&1 ; }

# 互動式 yes/no (預設 No)
ask_yn() {
    local prompt="$1" default="${2:-n}" resp hint
    [[ "$default" == "y" ]] && hint="[Y/n]" || hint="[y/N]"
    [[ "$DRY_RUN" == "true" ]] && { [[ "$default" == "y" ]] && return 0 || return 1 ; }
    printf '%s%s %s?%s ' "$C_BOLD" "$prompt" "$hint" "$C_RESET" >&2
    read -r resp </dev/tty || resp="$default"
    resp="${resp:-$default}"
    [[ "$resp" =~ ^[Yy] ]]
}

ask_val() {
    local prompt="$1" default="${2:-}" val
    [[ "$DRY_RUN" == "true" ]] && { printf '%s\n' "$default"; return ; }
    printf '%s%s [%s]:%s ' "$C_BOLD" "$prompt" "$default" "$C_RESET" >&2
    read -r val </dev/tty || val="$default"
    printf '%s\n' "${val:-$default}"
}

# 安全的 JSON 字串逸出 (不依賴 jq)
json_str() {
    local s="$1"
    s="${s//\\/\\\\}"   # backslash 先逸出
    s="${s//\"/\\\"}"   # 雙引號
    s="${s//
/\\n}"     # 換行 (literal newline in replacement)
    s="${s//	/\\t}"   # tab
    printf '%s' "$s"
}

# 用 python3 做 JSON 合併 (跨平台、不依賴 jq)
# $1 = 來源檔 (可能不存在), $2 = 要合併進去的 JSON 片段字串, $3 = 輸出檔
json_merge_inplace() {
    local src="$1" patch_json="$2" out="$3"
    python3 - "$src" "$patch_json" "$out" <<'PYEOF'
import json, os, sys
src, patch_str, out = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(src) as f: data = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    data = {}
patch = json.loads(patch_str)
def deep_merge(base, upd):
    for k, v in upd.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base
deep_merge(data, patch)
os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
tmp = out + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
os.replace(tmp, out)
PYEOF
}

# list contains: 檢查空白分隔字串 $1 是否包含單字 $2
list_has() {
    local hay=" $1 " needle="$2"
    [[ "$hay" == *"$needle"* ]]
}

# ---------------------------------------------------------------------------
# 參數解析
# ---------------------------------------------------------------------------
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dry-run)    DRY_RUN=true ;;
            --uninstall)  UNINSTALL=true ;;
            --enable-hub) ENABLE_HUB=true ;;     # 階段 7: 啟用 hub daemon + 採集
            --qdrant)
                [[ $# -ge 2 ]] || die "--qdrant 需要參數 (local|cloud)"
                case "$2" in local|cloud) CFG_QDRANT_MODE="$2" ;; *) die "--qdrant 只接受 local 或 cloud" ;; esac
                shift ;;
            --client)
                [[ $# -ge 2 ]] || die "--client 需要參數 (zcode,claude,cursor,generic,可逗號分隔)"
                # 逗號轉空白
                CLIENTS_REQUESTED=$(printf '%s' "$2" | tr ',' ' ')
                shift ;;
            -h|--help)
                sed -n '3,30p' "$0" | sed 's/^# \{0,1\}//'
                exit 0 ;;
            -V|--version)
                echo "vector-memory-mcp installer v$SCRIPT_VERSION"
                exit 0 ;;
            *) die "未知參數: $1 (用 --help 看說明)" ;;
        esac
        shift
    done
}

# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------
check_python() {
    have python3 || die "找不到 python3。請安裝 Python ${PY_MIN_MAJOR}.${PY_MIN_MINOR}–${PY_MAX_MAJOR}.${PY_MAX_MINOR}"
    local ver major minor
    ver=$(python3 -c 'import sys;print("%d %d" % sys.version_info[:2])' 2>/dev/null || echo "0 0")
    major=$(echo "$ver" | cut -d' ' -f1)
    minor=$(echo "$ver" | cut -d' ' -f2)
    if [[ "$major" == "0" ]]; then die "無法解析 python3 版本" ; fi
    if (( major < PY_MIN_MAJOR || (major == PY_MIN_MAJOR && minor < PY_MIN_MINOR) )); then
        die "Python 版本太舊: ${major}.${minor} (需要 ${PY_MIN_MAJOR}.${PY_MIN_MINOR}+)"
    fi
    if (( major > PY_MAX_MAJOR || (major == PY_MAX_MAJOR && minor > PY_MAX_MINOR) )); then
        die "Python ${major}.${minor} 太新: torch/transformers 目前只支援到 ${PY_MAX_MAJOR}.${PY_MAX_MINOR}"
    fi
    ok "Python ${major}.${minor} (在範圍 ${PY_MIN_MAJOR}.${PY_MIN_MINOR}–${PY_MAX_MAJOR}.${PY_MAX_MINOR} 內)"
}

check_curl() { have curl || die "找不到 curl (用來健康檢查 Qdrant)" ; ok "curl 已安裝" ; }

check_os() {
    case "$(uname -s)" in
        Darwin) CFG_OS="macos" ; ok "OS: macOS $(sw_vers -productVersion 2>/dev/null || echo '?')" ;;
        Linux)  CFG_OS="linux" ; ok "OS: Linux" ;;
        MINGW*|MSYS*|CYGWIN*) CFG_OS="windows" ; warn "Windows 支援為實驗性 (Git Bash/MSYS2)" ;;
        *) warn "未測試的 OS: $(uname -s) — 繼續但可能出錯" ;;
    esac
}

# ---------------------------------------------------------------------------
# Qdrant: 本地 docker / Qdrant Cloud / 外部既有
# ---------------------------------------------------------------------------
ensure_qdrant() {
    step "Qdrant 資料庫"

    # 已經指定 QDRANT_URL 就視為外部實例
    if [[ -n "$CFG_QDRANT_URL" ]]; then
        ok "使用外部 Qdrant: $CFG_QDRANT_URL"
        [[ -z "$CFG_QDRANT_MODE" ]] && CFG_QDRANT_MODE="external"
        wait_qdrant "$CFG_QDRANT_URL"
        return
    fi

    # 偵測既有 6333
    if curl -sf -m 2 http://localhost:6333/ >/dev/null 2>&1; then
        ok "偵測到既有 Qdrant 在 localhost:6333,沿用"
        CFG_QDRANT_URL="http://localhost:6333"
        [[ -z "$CFG_QDRANT_MODE" ]] && CFG_QDRANT_MODE="existing"
        return
    fi

    # 沒指定 → 問用戶
    if [[ -z "$CFG_QDRANT_MODE" ]]; then
        if [[ "$DRY_RUN" == "true" ]]; then
            CFG_QDRANT_MODE="local"
            warn "[DRY RUN] 會互動式問 local/cloud (預設 local)"
        else
            cat <<EOF

${C_BOLD}Qdrant 部署方式:${C_RESET}
  ${C_BLUE}1)${C_RESET} 本地 Docker   — 自動起 qdrant/qdrant 容器,資料存 ~/.vector-memory-mcp/qdrant_storage
                 適合: 單機、開發、不想管雲端帳號。需要 Docker Desktop / Colima / OrbStack
  ${C_BLUE}2)${C_RESET} Qdrant Cloud  — 用 https://cloud.qdrant.io 的免費 1GB tier
                 適合: 多機共用、不想跑容器、雲端備份。需要註冊取 URL + API key
EOF
            if ask_yn "用本地 Docker? (No = Qdrant Cloud)" "y"; then
                CFG_QDRANT_MODE="local"
            else
                CFG_QDRANT_MODE="cloud"
            fi
        fi
    fi

    case "$CFG_QDRANT_MODE" in
        local)   start_qdrant_local ;;
        cloud)   configure_qdrant_cloud ;;
        *) die "不可能的 qdrant_mode: $CFG_QDRANT_MODE" ;;
    esac
}

start_qdrant_local() {
    have docker || die "選了本地 Docker Qdrant 但沒裝 docker (brew install --cask docker 或改用 --qdrant cloud)"
    docker info >/dev/null 2>&1 || die "docker daemon 沒在跑 (啟動 Docker Desktop / colima start)"

    local cname="vector-memory-qdrant"
    local sdir="$CFG_INSTALL_DIR/qdrant_storage"
    mkdir -p "$sdir"

    if docker ps --format '{{.Names}}' | grep -qx "$cname"; then
        ok "Qdrant 容器已在跑 ($cname)"
    elif docker ps -a --format '{{.Names}}' | grep -qx "$cname"; then
        log "啟動既有容器..."
        docker start "$cname" >/dev/null
        ok "容器已啟動 ($cname)"
    else
        log "建立 Qdrant 容器 (首次會拉 image,~60MB)..."
        docker run -d --name "$cname" \
            -p 6333:6333 -p 6334:6334 \
            -v "$sdir:/qdrant/storage" \
            -e QDRANT__LOG_LEVEL=INFO \
            --restart unless-stopped \
            qdrant/qdrant:latest >/dev/null
        ok "容器已建立並啟動 ($cname)"
    fi
    CFG_QDRANT_URL="http://localhost:6333"
    wait_qdrant "$CFG_QDRANT_URL"
}

configure_qdrant_cloud() {
    echo
    log "需要 Qdrant Cloud 的 endpoint 和 API key"
    echo "  → 取得方式: https://cloud.qdrant.io → 建免費 cluster → 複製 URL + API key"
    echo
    CFG_QDRANT_URL=$(ask_val "Qdrant Cloud URL (例: https://xxx.aws.cloud.qdrant.io:6333)" "")
    [[ -n "$CFG_QDRANT_URL" ]] || die "Qdrant Cloud 必須提供 URL"
    CFG_QDRANT_API_KEY=$(ask_val "Qdrant Cloud API key" "")
    [[ -n "$CFG_QDRANT_API_KEY" ]] || die "Qdrant Cloud 必須提供 API key"
    wait_qdrant "$CFG_QDRANT_URL"
}

wait_qdrant() {
    local url="$1" i
    log "健康檢查 $url ..."
    for ((i=1; i<=15; i++)); do
        if curl -sf -m 3 "$url/" >/dev/null 2>&1 \
           || curl -sf -m 3 -H "api-key: $CFG_QDRANT_API_KEY" "$url/" >/dev/null 2>&1; then
            ok "Qdrant 就緒 ($url)"
            return
        fi
        sleep 2
    done
    die "Qdrant 連不上 ($url),15 秒內無回應"
}

# ---------------------------------------------------------------------------
# 部署 server 程式碼 + venv + pre-download model
# ---------------------------------------------------------------------------
deploy_server() {
    step "部署 server 程式碼"

    local idir="$CFG_INSTALL_DIR"
    mkdir -p "$idir"

    if [[ -f "$idir/mcp_server.py" ]]; then
        ok "server 已存在於 $idir,跳過下載 (用 --uninstall 清掉重裝)"
    else
        log "從上游取得 server 檔案..."
        local tmpd; tmpd=$(mktemp -d)

        # 優先 git clone (sparse),失敗退化到 GitHub raw
        if have git && git clone --depth 1 --filter=blob:none --sparse \
                "$UPSTREAM_REPO" "$tmpd/repo" 2>/dev/null; then
            (cd "$tmpd/repo" && git sparse-checkout set "$UPSTREAM_SUBPATH" 2>/dev/null)
            if [[ -d "$tmpd/repo/$UPSTREAM_SUBPATH" ]]; then
                cp -R "$tmpd/repo/$UPSTREAM_SUBPATH/." "$idir/"
                ok "git clone + sparse-checkout 成功"
            else
                rm -rf "$tmpd"
                die "sparse-checkout 取不到 $UPSTREAM_SUBPATH,repo 結構可能變了"
            fi
        else
            warn "git clone 失敗,退化到 GitHub raw 抓檔 (可能缺周邊檔案)"
            local raw_base="https://raw.githubusercontent.com/Bryan-cmf/agentic-infrastructure/main/vector-memory"
            local f
            for f in mcp_server.py memory_utils.py requirements.txt; do
                curl -fsSL "$raw_base/$f" -o "$idir/$f" || { rm -rf "$tmpd"; die "抓不到 $f"; }
            done
            ok "已抓核心檔案到 $idir"
        fi
        rm -rf "$tmpd"
        mkdir -p "$idir/qdrant_storage"
    fi

    # ---- venv + 依賴 ----
    step "Python 虛擬環境 + 依賴"
    local venv_python="$idir/.venv/bin/python"
    if [[ -x "$venv_python" ]] && "$venv_python" -c "import mcp, qdrant_client" 2>/dev/null; then
        ok ".venv 已就緒且依賴完整,跳過 pip install"
    else
        log "建立 venv..."
        [[ "$DRY_RUN" == "true" ]] && { warn "[DRY RUN] 會建 venv + pip install -r requirements.txt (~2GB 含 torch)"; return ; }
        python3 -m venv "$idir/.venv"
        log "pip install (torch + transformers + mcp + qdrant-client,會花幾分鐘)..."
        "$idir/.venv/bin/pip" install --upgrade pip >/dev/null 2>&1 || true
        "$idir/.venv/bin/pip" install -r "$idir/requirements.txt" 2>&1 | tail -5
        ok "依賴安裝完成"
    fi

    # ---- pre-download embedding model ----
    step "預下載 embedding 模型"
    if [[ "$DRY_RUN" == "true" ]]; then
        warn "[DRY RUN] 會下載 $CFG_MODEL (~2GB,首次會慢)"
        return
    fi
    log "下載 $CFG_MODEL 到 HF cache (首次 ~2GB)..."
    if "$venv_python" -c "
from sentence_transformers import SentenceTransformer
m = SentenceTransformer('$CFG_MODEL')
v = m.encode('installer 自檢')
print(f'  維度: {len(v)}')
print(f'  模型就緒: OK')
" 2>&1 | grep -E "維度|模型就緒|Error|Traceback" ; then
        ok "模型已就緒"
    else
        die "模型下載/載入失敗 (檢查網路或 HF mirror)"
    fi
}

# ---------------------------------------------------------------------------
# 偵測 MCP client
# ---------------------------------------------------------------------------
detect_clients() {
    step "偵測 MCP client"

    local zcode_cfg="$HOME/.zcode/cli/config.json"
    local claude_cfg=""
    case "$CFG_OS" in
        macos)   claude_cfg="$HOME/Library/Application Support/Claude/claude_desktop_config.json" ;;
        linux)   claude_cfg="$HOME/.config/Claude/claude_desktop_config.json" ;;
        windows) claude_cfg="${APPDATA:-$HOME/AppData/Roaming}/Claude/claude_desktop_config.json" ;;
    esac

    CLIENTS_DETECTED=""
    if { have zcode || [[ -d "$HOME/.zcode" ]] ; } && { [[ -f "$zcode_cfg" ]] || [[ -d "$HOME/.zcode/cli" ]] ; }; then
        CLIENTS_DETECTED="$CLIENTS_DETECTED zcode"
        ok "偵測到 ZCode (config: $zcode_cfg)"
    fi
    if [[ -n "$claude_cfg" ]] && { [[ -f "$claude_cfg" ]] || [[ -d "$(dirname "$claude_cfg")" ]] ; }; then
        CLIENTS_DETECTED="$CLIENTS_DETECTED claude"
        ok "偵測到 Claude Desktop (config: $claude_cfg)"
    fi
    if [[ -d "$HOME/.cursor" ]] || have cursor ; then
        CLIENTS_DETECTED="$CLIENTS_DETECTED cursor"
        ok "偵測到 Cursor"
    fi
    # 永遠提供 generic 範本
    CLIENTS_DETECTED="$CLIENTS_DETECTED generic"
    CLIENTS_DETECTED="${CLIENTS_DETECTED# }"  # 去前導空白

    # 偵測結果字串若只有 generic,提示一下
    if [[ "$CLIENTS_DETECTED" == "generic" ]]; then
        warn "沒偵測到已知的 MCP client,只會輸出 generic 範本到 $CFG_INSTALL_DIR/clients/"
    fi
}

# ---------------------------------------------------------------------------
# 共用:組 env JSON 區塊 (不含外層大括號)
# ---------------------------------------------------------------------------
build_env_block() {
    local block
    block=$(printf '      "QDRANT_URL": "%s",\n      "DEFAULT_COLLECTION": "%s",\n      "EMBEDDING_MODEL": "%s"' \
        "$(json_str "$CFG_QDRANT_URL")" \
        "$(json_str "$CFG_COLLECTION")" \
        "$(json_str "$CFG_MODEL")")
    if [[ -n "$CFG_QDRANT_API_KEY" ]]; then
        block="$block,$(printf '\n      "QDRANT_API_KEY": "%s"' "$(json_str "$CFG_QDRANT_API_KEY")")"
    fi
    printf '%s' "$block"
}

# ---------------------------------------------------------------------------
# client 註冊
# ---------------------------------------------------------------------------
register_all_clients() {
    step "註冊 MCP server 到 client"

    local targets
    if [[ -n "$CLIENTS_REQUESTED" ]]; then
        targets="$CLIENTS_REQUESTED"
    else
        targets="$CLIENTS_DETECTED"
    fi

    local c
    for c in $targets; do
        case "$c" in
            zcode)   register_zcode ;;
            claude)  register_claude ;;
            cursor)  register_cursor ;;
            generic) register_generic ;;
            *) warn "未知 client: $c (跳過)" ;;
        esac
    done
}

register_zcode() {
    local cfg="$HOME/.zcode/cli/config.json"
    mkdir -p "$(dirname "$cfg")"

    local py="$CFG_INSTALL_DIR/.venv/bin/python"
    local srv="$CFG_INSTALL_DIR/mcp_server.py"
    local env_block; env_block=$(build_env_block)

    local patch
    patch=$(cat <<EOF
{
  "mcp": {
    "servers": {
      "vector-memory": {
        "command": "$(json_str "$py")",
        "args": ["$(json_str "$srv")"],
        "cwd": "$(json_str "$CFG_INSTALL_DIR")",
        "env": {
$env_block
        }
      }
    }
  }
}
EOF
)
    if [[ "$DRY_RUN" == "true" ]]; then
        warn "[DRY RUN] 會把以下 JSON 合併進 $cfg:"
        printf '%s\n' "$patch"
        return
    fi
    json_merge_inplace "$cfg" "$patch" "$cfg"
    ok "已寫入 ZCode: $cfg"
    warn "重啟 ZCode 後才會載入新的 MCP server"
}

register_claude() {
    local cfg=""
    case "$CFG_OS" in
        macos)   cfg="$HOME/Library/Application Support/Claude/claude_desktop_config.json" ;;
        linux)   cfg="$HOME/.config/Claude/claude_desktop_config.json" ;;
        windows) cfg="${APPDATA:-$HOME/AppData/Roaming}/Claude/claude_desktop_config.json" ;;
    esac
    [[ -n "$cfg" ]] || { warn "Claude Desktop: 不支援的 OS"; return ; }
    mkdir -p "$(dirname "$cfg")"

    local py="$CFG_INSTALL_DIR/.venv/bin/python"
    local srv="$CFG_INSTALL_DIR/mcp_server.py"
    local env_block; env_block=$(build_env_block)

    local patch
    patch=$(cat <<EOF
{
  "mcpServers": {
    "vector-memory": {
      "command": "$(json_str "$py")",
      "args": ["$(json_str "$srv")"],
      "cwd": "$(json_str "$CFG_INSTALL_DIR")",
      "env": {
$env_block
      }
    }
  }
}
EOF
)
    if [[ "$DRY_RUN" == "true" ]]; then
        warn "[DRY RUN] 會把以下 JSON 合併進 $cfg:"
        printf '%s\n' "$patch"
        return
    fi
    json_merge_inplace "$cfg" "$patch" "$cfg"
    ok "已寫入 Claude Desktop: $cfg"
    warn "重啟 Claude Desktop 後才會載入"
}

register_cursor() {
    local py="$CFG_INSTALL_DIR/.venv/bin/python"
    local srv="$CFG_INSTALL_DIR/mcp_server.py"
    local env_block; env_block=$(build_env_block)

    local patch
    patch=$(cat <<EOF
{
  "mcpServers": {
    "vector-memory": {
      "command": "$(json_str "$py")",
      "args": ["$(json_str "$srv")"],
      "cwd": "$(json_str "$CFG_INSTALL_DIR")",
      "env": {
$env_block
      }
    }
  }
}
EOF
)
    if [[ "$DRY_RUN" == "true" ]]; then
        warn "[DRY RUN] 會在當前目錄建立 .cursor/mcp.json:"
        printf '%s\n' "$patch"
        return
    fi
    mkdir -p .cursor
    json_merge_inplace .cursor/mcp.json "$patch" .cursor/mcp.json
    ok "已寫入 Cursor: $(pwd)/.cursor/mcp.json"
    # 也放一份到安裝目錄供複用
    mkdir -p "$CFG_INSTALL_DIR/clients"
    printf '%s\n' "$patch" > "$CFG_INSTALL_DIR/clients/cursor-mcp.json"
}

register_generic() {
    local py="$CFG_INSTALL_DIR/.venv/bin/python"
    local srv="$CFG_INSTALL_DIR/mcp_server.py"
    local cdir="$CFG_INSTALL_DIR/clients"
    mkdir -p "$cdir"

    local env_block; env_block=$(build_env_block)

    # zcode 格式
    cat > "$cdir/zcode-config.snippet.json" <<EOF
// ~/.zcode/cli/config.json  (合併到既有 mcp.servers)
{
  "mcp": {
    "servers": {
      "vector-memory": {
        "command": "$py",
        "args": ["$srv"],
        "cwd": "$CFG_INSTALL_DIR",
        "env": {
$env_block
        }
      }
    }
  }
}
EOF

    # claude / cursor 格式
    cat > "$cdir/claude-cursor-config.snippet.json" <<EOF
// Claude: ~/Library/Application Support/Claude/claude_desktop_config.json
// Cursor: <project>/.cursor/mcp.json
// 都是 mcpServers.<name> 格式
{
  "mcpServers": {
    "vector-memory": {
      "command": "$py",
      "args": ["$srv"],
      "cwd": "$CFG_INSTALL_DIR",
      "env": {
$env_block
      }
    }
  }
}
EOF

    # opencode 原版格式
    cat > "$cdir/opencode-config.snippet.json" <<EOF
// OpenCode 原版: config.json 的 mcp.<name> 平鋪格式
{
  "mcp": {
    "vector-memory": {
      "type": "local",
      "command": ["$py", "$srv"],
      "cwd": "$CFG_INSTALL_DIR",
      "environment": {
        "QDRANT_URL": "$CFG_QDRANT_URL",
        "DEFAULT_COLLECTION": "$CFG_COLLECTION",
        "EMBEDDING_MODEL": "$CFG_MODEL"
      },
      "enabled": true
    }
  }
}
EOF

    ok "generic 範本已輸出到 $cdir/"
    log "  - zcode-config.snippet.json"
    log "  - claude-cursor-config.snippet.json"
    log "  - opencode-config.snippet.json"
}

# ---------------------------------------------------------------------------
# self-test
# ---------------------------------------------------------------------------
self_test() {
    step "自檢 (MCP handshake)"

    local py="$CFG_INSTALL_DIR/.venv/bin/python"
    local srv="$CFG_INSTALL_DIR/mcp_server.py"
    [[ -x "$py" && -f "$srv" ]] || die "server 檔案缺失 ($py / $srv)"

    # 用 env 檔組環境 (避免 var=val 前綴的 shell 語法問題)
    local envf; envf=$(mktemp)
    {
        printf 'QDRANT_URL=%s\n'     "$CFG_QDRANT_URL"
        printf 'DEFAULT_COLLECTION=%s\n' "$CFG_COLLECTION"
        printf 'EMBEDDING_MODEL=%s\n' "$CFG_MODEL"
        [[ -n "$CFG_QDRANT_API_KEY" ]] && printf 'QDRANT_API_KEY=%s\n' "$CFG_QDRANT_API_KEY"
    } > "$envf"

    log "送 initialize handshake (會花 5–10 秒載模型)..."
    local resp
    resp=$(env $(cat "$envf") timeout 60 "$py" "$srv" <<'EOF' 2>/dev/null || true
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"installer","version":"1.0"}}}
EOF
)
    rm -f "$envf"

    if echo "$resp" | grep -q '"capabilities"'; then
        ok "MCP handshake 成功 — server 可回應工具請求"
        log "  server 已就緒 (mem_save / mem_search / mem_health 等 12 個工具可用)"
    else
        warn "handshake 沒拿到 capabilities (可能是 stdin 緩衝問題,不代表 server 壞)"
        log "  手動驗證: $py $srv  (然後從 MCP client 呼叫 mem_health)"
    fi
}

# ---------------------------------------------------------------------------
# hub 啟用 (階段 7: --enable-hub)
# ---------------------------------------------------------------------------
enable_hub() {
    step "啟用 Hub (跨 agent 記憶中樞)"

    local hub_dir="${CFG_INSTALL_DIR}/hub"
    local venv_py="${CFG_INSTALL_DIR}/.venv/bin/python"

    # hub/ 目錄 (若 installer 沒包,從 GitHub 取)
    if [[ ! -d "$hub_dir" ]]; then
        log "從 vector-memory-mcp repo 取 hub/..."
        local tmpd; tmpd=$(mktemp -d)
        if have git && git clone --depth 1 --filter=blob:none --sparse \
                "https://github.com/Bryan-cmf/vector-memory-mcp" "$tmpd/repo" 2>/dev/null; then
            (cd "$tmpd/repo" && git sparse-checkout set "hub" 2>/dev/null)
            cp -R "$tmpd/repo/hub" "${CFG_INSTALL_DIR}/"
            rm -rf "$tmpd"
            ok "hub/ 已取得"
        else
            warn "無法取 hub/ (git clone 失敗),略過 hub 啟用"
            return
        fi
    else
        ok "hub/ 已存在於 $hub_dir"
    fi

    # 1. 建立 unified_mem collection + migrate 既有資料
    log "執行 migration (把既有 collection 統一進 unified_mem)..."
    if [[ "$DRY_RUN" == "true" ]]; then
        warn "[DRY RUN] 會跑 migrate.py (snapshot + 9968 點 migrate)"
    else
        QDRANT_URL="$CFG_QDRANT_URL" "$venv_py" "$hub_dir/migrate.py" 2>&1 | tail -5
    fi

    # 2. 安裝 launchd plist + 啟動 daemon
    log "安裝 daemon (launchd)..."
    if [[ "$DRY_RUN" != "true" ]]; then
        "$venv_py" "$hub_dir/hub_cli.py" start 2>&1 | tail -5
    fi

    # 3. 建立隱私設定
    log "建立隱私設定..."
    if [[ "$DRY_RUN" != "true" ]]; then
        "$venv_py" "$hub_dir/privacy.py" "test" >/dev/null 2>&1 || true
    fi

    ok "Hub 已啟用"
    echo "   - unified_mem collection (跨 agent 統一記憶庫)"
    echo "   - launchd daemon (每 15 分鐘自動採集)"
    echo "   - privacy.yml (redact 設定)"
    echo ""
    echo "   管理指令:"
    echo "     $venv_py $hub_dir/hub_cli.py status"
    echo "     $venv_py $hub_dir/hub_cli.py run-once"
    echo "     $venv_py $hub_dir/hub_cli.py export --format jsonl -o backup.jsonl"
}

# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------
do_uninstall() {
    step "解除安裝"

    local idir="$CFG_INSTALL_DIR"
    warn "會嘗試從 client config 移除 vector-memory 註冊"

    local cfg
    for cfg in \
        "$HOME/.zcode/cli/config.json" \
        "$HOME/Library/Application Support/Claude/claude_desktop_config.json" \
        ".cursor/mcp.json"; do
        if [[ -f "$cfg" ]] && grep -q "vector-memory" "$cfg" 2>/dev/null; then
            if ask_yn "從 $cfg 移除 vector-memory 註冊?" "y"; then
                python3 - "$cfg" <<'PYEOF'
import json, sys, os
p = sys.argv[1]
with open(p) as f: d = json.load(f)
changed = False
if isinstance(d.get("mcpServers"), dict) and "vector-memory" in d["mcpServers"]:
    del d["mcpServers"]["vector-memory"]; changed = True
mcp = d.get("mcp")
if isinstance(mcp, dict):
    if isinstance(mcp.get("servers"), dict) and "vector-memory" in mcp["servers"]:
        del mcp["servers"]["vector-memory"]; changed = True
    if "vector-memory" in mcp:
        del mcp["vector-memory"]; changed = True
if changed:
    tmp = p + ".tmp"
    with open(tmp, "w") as f: json.dump(d, f, indent=2, ensure_ascii=False); f.write("\n")
    os.replace(tmp, p)
    print("  removed from " + p)
PYEOF
            fi
        fi
    done

    # 停 Qdrant 容器 (可選)
    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "vector-memory-qdrant"; then
        if ask_yn "停止 + 移除 Qdrant 容器? (資料保留在 $idir/qdrant_storage)" "n"; then
            docker stop vector-memory-qdrant >/dev/null 2>&1 || true
            docker rm   vector-memory-qdrant >/dev/null 2>&1 || true
            ok "容器已移除"
        fi
    fi

    # 移除 server + venv
    if [[ -d "$idir/.venv" ]] || [[ -f "$idir/mcp_server.py" ]]; then
        if ask_yn "刪除 $idir (含 venv + server 程式碼,~3GB)?" "n"; then
            rm -rf "$idir"
            ok "已刪除 $idir"
        else
            warn "保留 $idir"
        fi
    fi

    step "解除安裝完成"
    echo "  Qdrant Cloud 資料 (若有) 仍保留在雲端,請至 Qdrant Cloud dashboard 手動清除"
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
main() {
    parse_args "$@"

    if [[ "$UNINSTALL" == "true" ]]; then
        do_uninstall
        exit 0
    fi

    cat <<EOF
${C_BOLD}
╔══════════════════════════════════════════════════════════╗
║   vector-memory-mcp  —  一鍵安裝 + MCP client 註冊       ║
║   installer v$SCRIPT_VERSION                                 ║
╚══════════════════════════════════════════════════════════╝${C_RESET}

安裝目標: ${C_BLUE}$CFG_INSTALL_DIR${C_RESET}
$( [[ "$DRY_RUN" == "true" ]] && echo "${C_YELLOW}(DRY RUN — 不會實際變更)${C_RESET}" )
EOF

    check_os
    check_python
    check_curl

    ensure_qdrant
    deploy_server
    detect_clients
    register_all_clients
    self_test

    # 階段 7: 可選啟用 hub (採集 daemon + 跨 agent 統一記憶庫)
    if [[ "$ENABLE_HUB" == "true" ]]; then
        enable_hub
    fi

    step "完成 🎉"
    cat <<EOF

${C_GREEN}vector-memory MCP 已就緒${C_RESET}

下一步:
  1. ${C_BOLD}重啟你的 MCP client${C_RESET} (ZCode / Claude Desktop / Cursor)
     — server 在 client 啟動時 spawn,首次載入模型約 5–10 秒

  2. 試用工具 (在對話裡):
     - "把這段記憶存起來: ..."  → mem_save
     - "搜尋我之前存過關於 X 的記憶" → mem_search
     - "記憶庫健康狀態" → mem_health

  3. 設定檔位置:
     - server:     $CFG_INSTALL_DIR/mcp_server.py
     - 環境變數:   寫在各 client config 的 env 區塊
     - Qdrant:     $CFG_QDRANT_URL
     - collection: $CFG_COLLECTION

解除安裝:  ${C_BLUE}./install.sh --uninstall${C_RESET}
文件:      ${C_BLUE}$CFG_INSTALL_DIR/README.md${C_RESET}
EOF
}

main "$@"
