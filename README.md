# vector-memory-mcp

把 [vector-memory](https://github.com/Bryan-cmf/agentic-infrastructure/tree/main/vector-memory) MCP server 一鍵裝到本機,並自動註冊到你的 MCP client(ZCode / Claude Desktop / Cursor / 通用)。

**vector-memory 是什麼** —— 一個基於 Qdrant + BGE-m3 的長期記憶 MCP server,給 AI agent 用 `mem_save` / `mem_search` / `mem_health` 等工具記住和召回跨對話的記憶。

## 一鍵安裝

```bash
curl -fsSL https://raw.githubusercontent.com/Bryan-cmf/vector-memory-mcp/main/install.sh -o install.sh
bash install.sh
```

安裝包會:

1. **preflight** — 檢查 bash 4+ / Python 3.10–3.13 / curl / OS
2. **Qdrant** — auto-detect:已有本地 Qdrant?沒有的話,問你要本地 Docker 還是 Qdrant Cloud
3. **server** — git sparse-checkout 上游 `vector-memory/` → 建 venv → pip install(torch + transformers + mcp + qdrant-client)
4. **model** — 預先下載 BGE-m3(~2GB),避免首次啟動超時
5. **client 偵測** — 掃 ZCode / Claude Desktop / Cursor 的 config 路徑,看哪些裝了
6. **註冊** — 把 server 寫進偵測到的 client config(也可用 `--client` 指定)
7. **self-test** — 送一個 MCP `initialize` handshake,確認 server 真能回應

## 完整選項

```bash
bash install.sh                  # 全自動 (互動式問 Qdrant + 偵測所有 client)
bash install.sh --dry-run        # 預演,不改任何檔案
bash install.sh --qdrant local   # 強制本地 Docker Qdrant
bash install.sh --qdrant cloud   # 強制 Qdrant Cloud (互動式問 URL+key)
bash install.sh --client zcode   # 只註冊某 client (逗號分隔: zcode,claude,cursor)
bash install.sh --uninstall      # 移除 venv + client 註冊 (保留 Qdrant 資料)
bash install.sh --help           # 完整說明
bash install.sh --version        # 版本
```

## 環境變數(免互動 / CI 用)

| 變數 | 預設 | 用途 |
|---|---|---|
| `QDRANT_URL` | (偵測) | 覆寫 Qdrant endpoint,設了就跳過起服務 |
| `QDRANT_API_KEY` | (空) | Qdrant Cloud key 或帶認證的自架實例 |
| `EMBEDDING_MODEL` | `BAAI/bge-m3` | embedding 模型 HuggingFace 名 |
| `VECTOR_MEMORY_COLLECTION` | `vector_memory` | Qdrant collection 名 |
| `VECTOR_MEMORY_DIR` | `~/.vector-memory-mcp` | 安裝根目錄 |
| `NO_COLOR` | (未設) | 設了就停用彩色輸出(CI 友善) |

### 非互動範例(Qdrant Cloud)

```bash
QDRANT_URL="https://xyz.aws.cloud.qdrant.io:6333" \
QDRANT_API_KEY="xxxxx" \
bash install.sh --client zcode,claude
```

### 非互動範例(本地 Docker)

```bash
bash install.sh --qdrant local --client zcode
```

## 系統需求

| 項目 | 版本 | 備註 |
|---|---|---|
| **bash** | 4.0+ | macOS 內建是 3.2 → `brew install bash` |
| **Python** | 3.10–3.13 | ⚠️ **3.14 不支援**(torch/transformers 還沒支援) |
| **curl** | 任意 | Qdrant 健康檢查用 |
| **Docker** | 任意 | 本地 Qdrant 模式才需要;Cloud 模式免 |

**磁碟**:~3GB(venv + torch + bge-m3 模型)
**RAM**:首次載入模型 ~1.5GB;穩態 ~800MB

## MCP client 對應的 config 格式

安裝包會自動處理這 4 種格式的差異:

| Client | config 路徑 | 格式 |
|---|---|---|
| **ZCode** | `~/.zcode/cli/config.json` | `mcp.servers.<name>` |
| **Claude Desktop** | `~/Library/Application Support/Claude/claude_desktop_config.json` | `mcpServers.<name>` |
| **Cursor** | `<project>/.cursor/mcp.json` | `mcpServers.<name>` |
| **OpenCode(原版)** | `config.json` | `mcp.<name>` 平鋪 |

## 可用工具(server 註冊後,在 client 對話裡就能呼叫)

| 工具 | 用途 |
|---|---|
| `mem_save` | 存一段記憶(自動 embedding + 寫 Qdrant) |
| `mem_search` | 語意搜尋記憶(回最相關的 N 筆) |
| `mem_delete` | 依 ID 刪除 |
| `mem_health` | 記憶庫健康狀態 + 統計 |
| `mem_stats` | collection 統計(point 數、磁碟用量) |
| `mem_list_collections` | 列所有 collection |
| `mem_decay` | 依時間衰減記憶權重 |
| `mem_dedup` | 去重相似記憶 |
| `mem_contradict` | 偵測矛盾記憶 |
| `mem_time_travel` | 依時間戳回溯 |
| `mem_federated` | 跨 collection 聯邦搜尋 |
| `mem_graph` | 記憶圖譜關聯 |

## 解除安裝

```bash
bash install.sh --uninstall
```

會:
- 從各 client config 拿掉 `vector-memory` 註冊(逐個確認)
- 可選停止 + 移除 Qdrant 容器(**資料保留**在 `~/.vector-memory-mcp/qdrant_storage/`)
- 可選刪除整個 `~/.vector-memory-mcp/`(含 venv + server,~3GB)

Qdrant Cloud 資料不會被動到,要清除請去 Qdrant Cloud dashboard。

## 常見問題

### Q: 裝完重啟 client,但 MCP server 沒出現?

查 client log。ZCode 在 `~/.zcode/v2/logs/` 找 `mcpServerCount` 和 `mcpServerNames`:

- 數字沒增加 → config 沒讀到,檢查 JSON 合法性 `python3 -c "import json; json.load(open('你的config路徑'))"`
- 數字有增加但 server name 不對 → 改 `mcp.servers.<name>` 那層的 key

### Q: `Python 3.14 太新` 錯誤

torch 和 transformers 在 3.14 還沒發布正式 wheel。改用 3.13:

```bash
brew install python@3.13
# 或用 pyenv:
pyenv install 3.13 && pyenv shell 3.13
```

### Q: 首次啟動 MCP server 很慢(5–10 秒)

正常 —— BGE-m3 模型要載入記憶體。安裝包已經 `pre-download`,所以是「磁碟載入到 RAM」的時間,不是網路下載。生產環境可用 `EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5` 換小模型(512 維,載入快但精度差)。

### Q: macOS 上 `command not found: docker` 但裝了 Docker Desktop

Docker Desktop 的 CLI 工具沒掛進 PATH。開 Docker Desktop → Settings → Advanced → "Install docker-cli" 勾起來;或 `brew install docker`.

### Q: ZCode 跟 OpenCode 的 config 格式為什麼不同?

ZCode 是 OpenCode 的 fork,但 MCP 區塊改成了 `mcp.servers.<name>` 嵌套(多一層 `servers`),OpenCode 原版是 `mcp.<name>` 平鋪。安裝包會根據偵測到的 client 寫對應格式。

## License

MIT — 見 [LICENSE](./LICENSE)。上游 vector-memory 程式碼亦為 MIT(Bryan-cmf)。
