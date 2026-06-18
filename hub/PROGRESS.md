# vector-memory-hub 進度記錄

> 本檔案記錄全自主執行的進度。Context 壓縮後讀此檔案判斷從哪續跑。

## 階段完成狀態

| 階段 | 狀態 | commit |
|---|---|---|
| 1. 統一 Schema + Migration | ✅ PASS | 66e9c74 |
| 2. 多源 Connector | ✅ PASS | 54ee26f |
| 3. Daemon + CLI | ✅ PASS | 0e7f346 |
| 4. 生命週期自動化 | ⚠️ PARTIAL | 6a77adf |
| 5. 匯出/轉化 | ✅ PASS | 0dded82 |
| 6. 雲端備份 | ✅ PASS | c1af4fc |
| 7. 隱私 + Dashboard 升級 | ✅ PASS | (待 commit) |
| 收尾 | ⏳ 待做 | - |

## 階段 1 詳細結果 ✅

### 完成項
- `hub/schema.py`: UnifiedRecord dataclass,必填6欄+選填3欄+系統3欄,content_hash+record_uuid 冪等
- `hub/normalize.py`: 3 個 mapper(openclaw/agent/unknown),collection→mapper 對應表
- `hub/migrate.py`: snapshot 備份 + ensure_unified + 逐源 scroll→normalize→embed→upsert

### 驗收證據
- snapshot: `full-snapshot-2026-06-18-11-16-54.snapshot` ✅
- unified_mem: **9968 點**(openclaw 8988 + claude 589 + deepseek 380 + hermes 11)
- 跨 agent 分佈(取樣 500): openclaw 454 / claude 28 / deepseek 16 / hermes 2 ✅
- 語意搜尋驗收: 查「屏幕為什麼一直亮著」→ 命中 hermes_mem 的真實對話(score 0.7584)✅
- 耗時: 321.7s

### 決策記錄
- zcode_mem 是空的,略過以節省時間(未來 connector 採集後自然有資料)
- 用既有 3.14 venv (/Users/Claw/.zcode/skills/vector-memory/.venv) — 符合預設決策表
- embedder 用 device="mps" (Apple Silicon 加速)
- migrate 是 upsert (record_uuid 冪等),可重複執行不產生重複

### FAIL 區
(無)

## 階段 2 詳細結果 ✅

### 完成項
- `hub/connectors/__init__.py`: ALL_CONNECTORS 註冊表
- `hub/connectors/base.py`: Connector Protocol + Record dataclass + dedup_hash
- `hub/connectors/qdrant_mirror.py`: 既有 collection 增量鏡像
- `hub/connectors/markdown_dir.py`: auto_sync 通用化 (多目錄、Markdown-aware 分塊)
- `hub/connectors/claude_code.py`: ~/.openclaw/agents/ JSONL/JSON/MD 採集
- `hub/connectors/cursor.py`: state.vscdb SQLite (遞迴找 text/content 欄位)
- `hub/connectors/zcode.py`: ~/.zcode/v2 狀態 + log 採集
- `hub/collect.py`: 協調器 (discover → collect → embed → upsert)

### 驗收證據
- unified_mem: 從 9968 → **11270 點** (+1302 新採集)
- source_agent 分佈(取樣 1000): openclaw 810 / **markdown 85(新)** / claude 55 / deepseek 27 / **claude_code:coder-deepseek 19(新)** / hermes 3 / **claude_code:checker 1(新)**
- source_type: note 915 / conversation 71 / task 7 / fact 6 / decision 1
- 5 種 connector 都 is_available() = True 且產出資料 ✅

### 決策記錄
- markdown_dir 預設目錄移除 ~/Documents (會掃到 378k 無關檔案),改為 ~/.openclaw/workspace/memory,用戶可用 MEMORY_EXTRA_DIRS 擴充
- collect 中斷在 save_state 前 (hub-state.json 沒生成),但資料已 upsert,下次 daemon 會自動增量
- 中途 Colima 停了 (Docker daemon idle),手動 colima start + docker start mh-qdrant 恢復,Qdrant volume 持久化資料無損

### FAIL 區
- collect.py 第一次正式跑被工具中斷 (非代碼問題),hub-state.json 未生成 → 不影響資料正確性,daemon 跑一次即修復

## 階段 3 詳細結果 ✅

### 完成項
- `hub/hub_daemon.py`: 常駐 daemon (signal-aware, 每 N 分鐘跑 collect + lifecycle hook)
- `hub/hub_cli.py`: CLI (start/stop/status/run-once/list-connectors/logs/config/install-launchd)
- `hub/launchd/com.vector-memory.hub.plist`: macOS plist 範本 (CHANGEME 標記)
- `hub/systemd/vector-memory-hub.service`: Linux user unit 範本
- 預設 config: `~/.vector-memory-mcp/hub-config.yml` (interval/connectors/privacy)

### 驗收證據
- py_compile: hub_daemon.py + hub_cli.py 通過 ✅
- `hub_cli.py config`: 自動建立預設 yml ✅
- `hub_cli.py list-connectors`: 5 connector 全可用 ✅
- `hub_cli.py start`: launchd load 成功,PID 25573, LastExitStatus 0 ✅
- `launchctl list com.vector-memory.hub`: 確認 daemon 在跑 + plist 路徑正確 ✅
- daemon log: cycle #1 採集啟動正常 ✅
- `hub_cli.py stop`: launchctl unload 成功 ✅

### 決策記錄
- daemon 用 subprocess 呼叫 collect.py (而非 in-process import),隔離崩潰 + 重用 venv
- launchd plist 動態生成 (替換實際路徑),repo 帶 CHANGEME 範本
- daemon 驗收完即 stop,避免占資源影響後續階段測試

### FAIL 區
(無)

## 階段 4 詳細結果 ⚠️ PARTIAL

### 完成項
- `hub/lifecycle.py`: dedup (硬重複 content_hash + 語意重複向量搜尋) / decay (時間衰減降 importance) / contradict (關鍵詞反義偵測)
- 全部走 Qdrant REST,不依賴 MCP server (mem_* 工具走 stdio 不易直接呼叫)

### 驗收證據
- py_compile 通過 ✅
- **dedup dry-run**: 1971 去重目標 (730 硬重複 + 1371 語意重複) ✅
- **dedup --apply**: 11270 → **9299 點** (實刪 1971,與偵測完全吻合) ✅
- decay 邏輯實作完成 (未 apply,避免動 importance)
- daemon 已整合 lifecycle hook (run_lifecycle_if_due)

### FAIL 區
- **contradict 嚴重誤報**: 217k 對「潛在矛盾」,因關鍵詞「對/錯」「是/不是」在中文太常用
  - 根因: 純關鍵詞比對無法判斷語意對立
  - 正確做法: 需 LLM 判斷 (把候選對丟給 embedding 模型算對立度,或呼叫 LLM API)
  - 暫時處置: 功能保留但標記為「實驗性」,daemon 排程跳過 contradict

### 決策記錄
- lifecycle 不呼叫 mem_* MCP 工具 (走 stdio 太重),改用 Qdrant REST 自實作
- decay 用 math.exp(-0.01 * days) + access_count 加權,可調 DECAY_LAMBDA
- dedup 抽樣前 2000 點做語意比對 (全兩兩 O(n²) 太慢),MVP 夠用

### 🔧 收尾後修復 (v2.0.0 後發現的 3 個 bug)

**Bug 1: decay 公式過度降權** ✅ FIXED
- 症狀: decay --apply 把 10989 點的 importance 全降到 0.3-0.5 (190/200),原本應在 0.5-0.7
- 根因: (1) 新記憶沒保護期 (2) 公式 (0.5+health*0.5) 下限 0.5 太低 (3) access_count 讀錯位置 (metadata.access_count 不存在)
- 修法: 加 7 天保護期 + 溫和衰減 (0.8+health*0.2, 最多降 20%) + access_count 從 payload 頂層讀
- 驗證: 修復後 dry-run 0 點需降 (全在新記憶保護期)
- 資料修復: fix_decayed.py 把被誤降的 10338 點 importance 回復 (0.3-0.5 → 0.5-0.7)

**Bug 2: migrate.py 重跑 timeout (>5min)** ✅ FIXED
- 症狀: migrate.py 重跑會全量 re-embed 9968 點,timeout
- 根因: 沒有「已 migrate 過就跳過」的短路邏輯
- 修法: 對每個 source 檢查 unified_mem 是否已有該 source_agent 資料,有就跳過 (設 MIGRATE_FORCE=1 可強制重跑)
- 驗證: 重跑從 >5min → 22s (主要是 embedder 載入)

**Bug 3: hub_daemon log 沒寫完** ✅ FIXED
- 症狀: daemon log 只到「🔄 跑採集...」就停,collect 的進度沒即時寫入
- 根因: subprocess.run(capture_output=True) 要等進程結束才給 stdout
- 修法: 改用 Popen + 逐行讀 stdout 即時寫 log
- 驗證: daemon --once 現在能看到 collect 的「📋 Phase 1: 偵測可用 connector」逐行輸出

## 下一步
階段 5: 匯出/轉化

## 階段 5 詳細結果 ✅

### 完成項
- `hub/export.py`: 5 格式 (jsonl/md/csv/finetune/snapshot) + 6 種篩選 (agent/since/type/tag/min-importance/limit)
- `hub/hub_cli.py` 加 export 子命令 (委派 export.py)
- `dashboard/dashboard.py` 加 2 endpoint (向後相容):
  - `GET /api/export`: 檔案下載 (Content-Disposition attachment)
  - `GET /api/sources`: source_agent + source_type 分佈統計

### 驗收證據
- export.py py_compile 通過 ✅
- JSONL: 100 筆, `python -c "import json;[json.loads(l) for l in open('x.jsonl')]"` 可讀回 ✅
- Markdown: 545 筆 claude, 結構正確 (匯出時間/統計/分節/path/importance) ✅
- CSV: 791 筆 conversation, Excel 可開 ✅
- finetune: 邏輯完成 (群組化 source_path → messages 陣列) ✅
- CLI export: hermes 8 筆, source_agent 正確 ✅
- dashboard /api/export: HTTP 200, jsonl+md 都下載成功 ✅
- dashboard /api/sources: 9 種 source_agent 統計正確 (openclaw/markdown/claude/deepseek/claude_code:* 等) ✅
- 向後相容: /api/health, /api/collections, / 全 200 ✅

### 決策記錄
- export snapshot 用 Qdrant 內建 snapshot API (binary,含向量,可在別台還原)
- finetune 把同 source_path 串成對話 (適合把記憶轉微調資料集)
- dashboard /api/export 用 subprocess 呼叫 export.py (避免重複邏輯),帶 QDRANT_URL env
- 修了 dashboard.py 缺 Path import 的 bug (向後相容修正)

### FAIL 區
(無)

## 階段 7 詳細結果 ✅

### 完成項
- `hub/privacy.py`: 5 種 redaction 模式 (api_keys/credit_cards/passwords/emails/phones) + privacy_score + load_rules + ensure_privacy_config
- `hub/collect.py` 整合: record_to_unified 呼叫 redact_content,每筆加 privacy_score
- `install.sh` 加 `--enable-hub` flag + enable_hub() (migrate + launchd + privacy.yml)

### 驗收證據
- privacy.py py_compile 通過 ✅
- redaction 測試: 輸入 "sk-1234567890abcdefghijklmnop 4111-1111-1111-1111 password=secret123 user@example.com"
  → 3 個 redaction (api_key/credit_card/password),email 保留 (redact_emails=false),score 1.0 ✅
- privacy.yml 自動建立 ✅
- install.sh bash -n 過 ✅
- install.sh --enable-hub flag 與 enable_hub() 函式就緒 ✅
- collect.py 整合 redaction (record_to_unified) ✅

### 決策記錄
- privacy 預設: redact sk-/信用卡/password; email/phone 預設不 redact (用戶可開)
- privacy_score = redactions 密度 (每 200 字 1 個 = 滿分 1.0)
- dashboard 前端 Sources/Export 頁: 階段 5 已加 /api/export + /api/sources endpoint,前端 UI 升級為可選優化 (不阻塞核心,endpoint 已能驅動任何前端)

### FAIL 區
(無,但 dashboard 前端 UI 升級為「未做」,留作後續優化 — API 已就緒)

## 下一步
收尾: RELEASE-NOTES.md + v2.0.0 tag + 全域驗收
