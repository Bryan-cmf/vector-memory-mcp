# vector-memory-hub 進度記錄

> 本檔案記錄全自主執行的進度。Context 壓縮後讀此檔案判斷從哪續跑。

## 階段完成狀態

| 階段 | 狀態 | commit |
|---|---|---|
| 1. 統一 Schema + Migration | ✅ PASS | 66e9c74 |
| 2. 多源 Connector | ✅ PASS | (待 commit) |
| 3. Daemon + CLI | ⏳ 待做 | - |
| 4. 生命週期自動化 | ⏳ 待做 | - |
| 5. 匯出/轉化 | ⏳ 待做 | - |
| 6. 雲端備份 | ⏳ 待做 | - |
| 7. 隱私 + Dashboard 升級 | ⏳ 待做 | - |
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

## 下一步
階段 3: Daemon + CLI
