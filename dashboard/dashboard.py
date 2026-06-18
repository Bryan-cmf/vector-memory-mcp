#!/usr/bin/env python3
"""
vector-memory-mcp dashboard — 輕量 Web UI
==========================================
單檔 FastAPI 應用,直接打 Qdrant REST API,顯示記憶庫健康度、
最近記憶、語意搜尋。語意搜尋的 embedding 模型採懶載入
(首次搜尋才載 BGE-m3),純瀏覽統計只用 Qdrant REST,~50MB RAM。

Usage:
    python dashboard.py                           # 預設 localhost:8765
    python dashboard.py --port 9000               # 自訂 port
    python dashboard.py --qdrant http://host:6333 # 自訂 Qdrant
    QDRANT_API_KEY=xxx python dashboard.py        # 帶認證的 Qdrant

Env:
    QDRANT_URL        預設 http://localhost:6333
    QDRANT_API_KEY    帶認證的 Qdrant (可選)
    EMBEDDING_MODEL   預設 BAAI/bge-m3 (語意搜尋用)
"""
from __future__ import annotations

import argparse
import html
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import urllib.request
import urllib.error
import json

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
DEFAULT_QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
DEFAULT_MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-m3")
DEFAULT_PORT = 8765
DEFAULT_BIND = "127.0.0.1"  # 本機 only,不對外


# ===========================================================================
# Qdrant REST 客戶端 (零依賴,只用 stdlib urllib)
# ===========================================================================
class Qdrant:
    """輕量 Qdrant REST 包裝,只做 dashboard 需要的少數操作。"""

    def __init__(self, url: str, api_key: str = "", timeout: int = 10):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.url}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("api-key", self.api_key)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()[:200]
            raise RuntimeError(f"Qdrant HTTP {e.code}: {err_body}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"Qdrant 連不上 {self.url}: {e.reason}") from None

    def root(self) -> dict:
        return self._req("GET", "/")

    def list_collections(self) -> list[str]:
        d = self._req("GET", "/collections")
        cols = d.get("result", {}).get("collections", [])
        return [c["name"] for c in cols]

    def collection_info(self, name: str) -> dict:
        return self._req("GET", f"/collections/{name}")

    def count(self, name: str) -> int:
        d = self._req("POST", f"/collections/{name}/points/count",
                      {"exact": True})
        return d.get("result", {}).get("count", 0)

    def scroll(self, name: str, limit: int = 10, offset: str | None = None) -> dict:
        body: dict = {"limit": limit, "with_payload": True, "with_vector": False}
        if offset:
            body["offset"] = offset
        return self._req("POST", f"/collections/{name}/points/scroll", body)

    def search(self, name: str, vector: list[float], limit: int = 10) -> list[dict]:
        d = self._req("POST", f"/collections/{name}/points/search", {
            "vector": vector,
            "limit": limit,
            "with_payload": True,
            "with_vector": False,
        })
        return d.get("result", [])


# ===========================================================================
# Embedding 模型 (懶載入 — 只在語意搜尋時才 import + 載模型)
# ===========================================================================
class Embedder:
    """語意搜尋的 embedding 產生器,首次 encode() 才載模型(~5s, ~2GB)。"""

    def __init__(self, model_name: str):
        self.model_name = model_name
        self._model = None
        self._dim = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            print(f"🔄 載入 embedding 模型: {self.model_name} (首次 ~5s, ~2GB RAM)",
                  file=sys.stderr, flush=True)
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name)
            self._dim = self._model.get_sentence_embedding_dimension()
            print(f"✓ 模型就緒 ({self._dim} 維)", file=sys.stderr, flush=True)

    def encode(self, text: str) -> list[float]:
        self._ensure_loaded()
        return self._model.encode(text).tolist()


# ===========================================================================
# FastAPI app
# ===========================================================================
def create_app(qdrant: Qdrant, embedder: Embedder):
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.responses import HTMLResponse, JSONResponse

    app = FastAPI(title="vector-memory dashboard", docs_url=None, redoc_url=None)

    # ---- API 端點 ----
    @app.get("/api/health")
    def api_health():
        try:
            r = qdrant.root()
            return {"ok": True, "qdrant": r.get("version"), "model_loaded": embedder.loaded}
        except RuntimeError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=503)

    @app.get("/api/collections")
    def api_collections():
        """所有 collection 的健康度摘要。"""
        try:
            names = qdrant.list_collections()
        except RuntimeError as e:
            raise HTTPException(503, str(e))
        out = []
        for n in names:
            try:
                info = qdrant.collection_info(n).get("result", {})
                cnt = qdrant.count(n)
                vc = info.get("config", {}).get("params", {}).get("vectors", {})
                out.append({
                    "name": n,
                    "points_count": cnt,
                    "status": info.get("status", "?"),
                    "vector_size": vc.get("size") if isinstance(vc, dict) else None,
                    "distance": vc.get("distance") if isinstance(vc, dict) else None,
                    "indexed_vectors": info.get("indexed_vectors_count", 0),
                })
            except RuntimeError as e:
                out.append({"name": n, "error": str(e)})
        return out

    @app.get("/api/collection/{name}/recent")
    def api_recent(name: str, limit: int = Query(20, ge=1, le=100)):
        """最近 N 筆記憶 (依 UUID 排序,scroll)。"""
        try:
            d = qdrant.scroll(name, limit=limit)
        except RuntimeError as e:
            raise HTTPException(503, str(e))
        pts = d.get("result", {}).get("points", [])
        items = []
        for p in pts:
            pl = p.get("payload", {})
            items.append({
                "id": p.get("id"),
                "content": pl.get("content", ""),
                "tags": pl.get("tags", []),
                "source": pl.get("source", pl.get("filename", "")),
                "created_at": pl.get("created_at", ""),
                "access_count": pl.get("access_count", 0),
                "char_length": pl.get("char_length", len(pl.get("content", ""))),
            })
        return {"items": items, "next_offset": d.get("result", {}).get("next_page_offset")}

    @app.get("/api/search")
    def api_search(q: str = Query(..., min_length=1),
                   collection: str = Query("openclaw_mem"),
                   limit: int = Query(10, ge=1, le=50)):
        """語意搜尋。首次呼叫會花 ~5s 載模型。"""
        try:
            vec = embedder.encode(q)
        except Exception as e:
            raise HTTPException(500, f"embedding 失敗: {e}")
        try:
            hits = qdrant.search(collection, vec, limit=limit)
        except RuntimeError as e:
            raise HTTPException(503, str(e))
        out = []
        for h in hits:
            pl = h.get("payload", {})
            out.append({
                "score": round(h.get("score", 0), 4),
                "id": h.get("id"),
                "content": pl.get("content", ""),
                "tags": pl.get("tags", []),
                "source": pl.get("source", pl.get("filename", "")),
                "created_at": pl.get("created_at", ""),
            })
        return {"query": q, "collection": collection, "hits": out}

    # ---- 匯出 API (階段 5 新增,向後相容) ----
    @app.get("/api/export")
    def api_export(format: str = Query("jsonl"),
                   agent: str = Query(""),
                   since: str = Query(""),
                   type: str = Query(""),
                   tag: str = Query(""),
                   min_importance: float = Query(0.0, ge=0.0, le=1.0),
                   limit: int = Query(1000, ge=1, le=50000)):
        """匯出記憶。format=jsonl|md|csv。回應檔案下載。"""
        from fastapi.responses import PlainTextResponse, Response
        from urllib.parse import quote

        # 委派給 export.py 邏輯 (避免重複)
        import subprocess as _sp
        import tempfile as _tf
        ext = {"jsonl": "jsonl", "md": "md", "csv": "csv"}.get(format, "jsonl")
        with _tf.NamedTemporaryFile(suffix=f".{ext}", delete=False) as _tf_:
            tmp_path = _tf_.name
        try:
            venv_py = str(Path(__file__).parent.parent / "hub" / ".venv" / "bin" / "python")
            if not Path(venv_py).exists():
                venv_py = sys.executable   # fallback 用當前 python
            cmd = [venv_py, str(Path(__file__).parent.parent / "hub" / "export.py"),
                   "--format", format, "-o", tmp_path, "--limit", str(limit)]
            if agent: cmd += ["--agent", agent]
            if since: cmd += ["--since", since]
            if type: cmd += ["--type", type]
            if tag: cmd += ["--tag", tag]
            if min_importance > 0: cmd += ["--min-importance", str(min_importance)]
            env = {**os.environ, "QDRANT_URL": qdrant.url}
            _sp.run(cmd, capture_output=True, timeout=120, check=False, env=env)
            content = Path(tmp_path).read_bytes()
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        media = {"jsonl": "application/jsonl", "md": "text/markdown", "csv": "text/csv"}.get(format, "text/plain")
        fname = f"vector-memory-export-{agent or 'all'}.{ext}"
        return Response(content, media_type=media,
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    @app.get("/api/sources")
    def api_sources():
        """各 source_agent 的統計 (dashboard 首頁分佈圖用)。"""
        # 直接 scroll unified_mem (跨 agent 統一庫)
        import urllib.request
        try:
            req = urllib.request.Request(
                f"{qdrant.url}/collections/unified_mem/points/scroll",
                data=json.dumps({"limit": 5000, "with_payload": True, "with_vector": False}).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            if qdrant.api_key:
                req.add_header("api-key", qdrant.api_key)
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode())
        except Exception:
            return {"error": "unified_mem 連不上"}

        pts = d.get("result", {}).get("points", [])
        from collections import Counter
        agents = Counter(p.get("payload", {}).get("source_agent", "?") for p in pts)
        types = Counter(p.get("payload", {}).get("source_type", "?") for p in pts)
        return {
            "sampled": len(pts),
            "by_agent": dict(agents.most_common()),
            "by_type": dict(types.most_common()),
        }

    # ---- 前端 (內嵌,零建置) ----
    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTML_PAGE

    return app


# ===========================================================================
# 前端 HTML (內嵌,純 vanilla JS,無框架無 build)
# ===========================================================================
HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>vector-memory dashboard</title>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #c9d1d9; --text-dim: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --yellow: #d29922; --red: #f85149;
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, "PingFang TC", "Microsoft JhengHei", sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.5;
  }
  header {
    padding: 16px 24px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
  }
  header h1 { font-size: 18px; margin: 0; font-weight: 600; }
  header .meta { color: var(--text-dim); font-size: 13px; font-family: var(--mono); }
  .pill {
    display: inline-block; padding: 2px 8px; border-radius: 10px;
    font-size: 11px; font-family: var(--mono);
  }
  .pill.green { background: rgba(63,185,80,.15); color: var(--green); }
  .pill.yellow { background: rgba(210,153,34,.15); color: var(--yellow); }
  .pill.red { background: rgba(248,81,73,.15); color: var(--red); }
  main { max-width: 1200px; margin: 0 auto; padding: 24px; }
  section { margin-bottom: 32px; }
  h2 { font-size: 14px; color: var(--text-dim); text-transform: uppercase;
       letter-spacing: .05em; margin: 0 0 12px; }
  /* collection cards */
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
  .card {
    background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
    padding: 16px; cursor: pointer; transition: border-color .15s;
  }
  .card:hover { border-color: var(--accent); }
  .card .name { font-weight: 600; font-size: 14px; font-family: var(--mono); margin-bottom: 8px; }
  .card .stat { font-size: 12px; color: var(--text-dim); }
  .card .big { font-size: 24px; font-weight: 700; color: var(--text); font-family: var(--mono); }
  /* search */
  .search-bar { display: flex; gap: 8px; margin-bottom: 12px; }
  .search-bar select, .search-bar input, .search-bar button {
    background: var(--surface); border: 1px solid var(--border); color: var(--text);
    padding: 8px 12px; border-radius: 6px; font-size: 14px; font-family: inherit;
  }
  .search-bar input { flex: 1; }
  .search-bar button { background: var(--accent); color: #fff; border: none; cursor: pointer; }
  .search-bar button:hover { opacity: .9; }
  .search-bar button:disabled { opacity: .5; cursor: wait; }
  /* memory item */
  .item {
    background: var(--surface); border: 1px solid var(--border); border-radius: 6px;
    padding: 12px 16px; margin-bottom: 8px;
  }
  .item .head { display: flex; justify-content: space-between; font-size: 12px;
                color: var(--text-dim); margin-bottom: 6px; font-family: var(--mono); }
  .item .score { color: var(--accent); font-weight: 600; }
  .item .content { font-size: 13px; white-space: pre-wrap; word-break: break-word;
                   max-height: 200px; overflow: auto; }
  .item .tags { margin-top: 6px; }
  .item .tags .tag {
    display: inline-block; background: rgba(88,166,255,.1); color: var(--accent);
    padding: 1px 6px; border-radius: 4px; font-size: 11px; margin-right: 4px;
    font-family: var(--mono);
  }
  .empty { color: var(--text-dim); font-style: italic; padding: 24px; text-align: center; }
  .loading { color: var(--text-dim); padding: 12px; }
  .tab-bar { display: flex; gap: 4px; border-bottom: 1px solid var(--border); margin-bottom: 16px; }
  .tab {
    padding: 8px 16px; cursor: pointer; border-bottom: 2px solid transparent;
    color: var(--text-dim); font-size: 14px;
  }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  footer { text-align: center; color: var(--text-dim); font-size: 12px; padding: 24px; }
</style>
</head>
<body>
<header>
  <h1>🧠 vector-memory dashboard</h1>
  <span class="meta" id="qdrant-meta">載入中…</span>
  <span class="meta" id="model-meta"></span>
</header>
<main>
  <section>
    <h2>Collections</h2>
    <div class="grid" id="collections"></div>
  </section>

  <section>
    <div class="tab-bar">
      <div class="tab active" data-tab="recent">最近記憶</div>
      <div class="tab" data-tab="search">語意搜尋</div>
    </div>

    <div id="tab-recent">
      <div class="search-bar">
        <select id="recent-collection"></select>
        <button onclick="loadRecent()">刷新</button>
      </div>
      <div id="recent-items"></div>
    </div>

    <div id="tab-search" style="display:none">
      <div class="search-bar">
        <select id="search-collection"></select>
        <input id="search-query" placeholder="語意搜尋 (例: 安裝步驟、Qdrant 設定)…"
               onkeydown="if(event.key==='Enter')doSearch()">
        <button id="search-btn" onclick="doSearch()">搜尋</button>
      </div>
      <div id="search-hint" class="empty" style="display:none">
        ⏳ 首次搜尋需載入 embedding 模型 (~5s, ~2GB RAM)…
      </div>
      <div id="search-items"></div>
    </div>
  </section>
</main>
<footer>vector-memory-mcp dashboard · 資料來源: Qdrant REST API · 本機 only (127.0.0.1)</footer>

<script>
let CURRENT = null;

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) {
    const e = await r.json().catch(() => ({detail: r.statusText}));
    throw new Error(e.detail || `HTTP ${r.status}`);
  }
  return r.json();
}

function fmtCount(n) {
  if (n >= 1000) return (n/1000).toFixed(1) + 'k';
  return String(n);
}

function fmtDate(s) {
  if (!s) return '';
  try {
    const d = new Date(s);
    return d.toLocaleString('zh-TW', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
  } catch { return s.slice(0,16); }
}

async function init() {
  try {
    const h = await api('/api/health');
    document.getElementById('qdrant-meta').textContent =
      `Qdrant ${h.qdrant || '?'} · ${h.ok ? '●' : '○'} connected`;
    document.getElementById('model-meta').textContent =
      h.model_loaded ? '· 模型已載入' : '';
  } catch (e) {
    document.getElementById('qdrant-meta').innerHTML =
      `<span style="color:var(--red)">✗ ${e.message}</span>`;
    return;
  }
  await loadCollections();
}

async function loadCollections() {
  const cols = await api('/api/collections');
  CURRENT = cols;
  const grid = document.getElementById('collections');
  grid.innerHTML = '';
  const selR = document.getElementById('recent-collection');
  const selS = document.getElementById('search-collection');
  selR.innerHTML = ''; selS.innerHTML = '';
  let totalPts = 0;
  for (const c of cols) {
    if (c.error) {
      grid.innerHTML += `<div class="card"><div class="name">${c.name}</div>
        <div class="stat" style="color:var(--red)">${c.error}</div></div>`;
      continue;
    }
    totalPts += c.points_count || 0;
    const cls = c.status === 'green' ? 'green' : (c.status === 'yellow' ? 'yellow' : 'red');
    grid.innerHTML += `
      <div class="card" onclick="selectCollection('${c.name}')">
        <div class="name">${c.name} <span class="pill ${cls}">${c.status}</span></div>
        <div class="big">${fmtCount(c.points_count||0)}</div>
        <div class="stat">${c.vector_size||'?'}d · ${c.distance||'?'}</div>
        <div class="stat">已索引 ${fmtCount(c.indexed_vectors||0)}</div>
      </div>`;
    selR.innerHTML += `<option value="${c.name}">${c.name} (${fmtCount(c.points_count||0)})</option>`;
    selS.innerHTML += `<option value="${c.name}">${c.name}</option>`;
  }
  // 預設選 points 最多的 collection
  const top = cols.filter(c=>!c.error).sort((a,b)=>(b.points_count||0)-(a.points_count||0))[0];
  if (top) { selR.value = top.name; selS.value = top.name; }
  loadRecent();
}

function selectCollection(name) {
  document.getElementById('recent-collection').value = name;
  loadRecent();
  document.querySelector('[data-tab="recent"]').click();
}

async function loadRecent() {
  const col = document.getElementById('recent-collection').value;
  if (!col) return;
  const box = document.getElementById('recent-items');
  box.innerHTML = '<div class="loading">載入中…</div>';
  try {
    const d = await api(`/api/collection/${encodeURIComponent(col)}/recent?limit=20`);
    if (!d.items.length) { box.innerHTML = '<div class="empty">(空)</div>'; return; }
    box.innerHTML = d.items.map(it => renderItem(it)).join('');
  } catch (e) {
    box.innerHTML = `<div class="empty" style="color:var(--red)">✗ ${e.message}</div>`;
  }
}

async function doSearch() {
  const q = document.getElementById('search-query').value.trim();
  const col = document.getElementById('search-collection').value;
  const btn = document.getElementById('search-btn');
  const hint = document.getElementById('search-hint');
  const box = document.getElementById('search-items');
  if (!q) return;
  btn.disabled = true; btn.textContent = '搜尋中…';
  box.innerHTML = '';
  hint.style.display = 'block';
  try {
    const t0 = performance.now();
    const d = await api(`/api/search?q=${encodeURIComponent(q)}&collection=${encodeURIComponent(col)}&limit=10`);
    const ms = Math.round(performance.now() - t0);
    hint.style.display = 'none';
    if (!d.hits.length) { box.innerHTML = '<div class="empty">沒有命中</div>'; return; }
    box.innerHTML = `<div class="stat" style="margin-bottom:8px;color:var(--text-dim);font-size:12px;font-family:var(--mono)">
      ${d.hits.length} 筆命中 · ${ms}ms</div>` +
      d.hits.map(h => renderItem(h, h.score)).join('');
  } catch (e) {
    hint.style.display = 'none';
    box.innerHTML = `<div class="empty" style="color:var(--red)">✗ ${e.message}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = '搜尋';
  }
}

function renderItem(it, score) {
  const esc = (s) => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const tags = (it.tags||[]).map(t => `<span class="tag">${esc(String(t))}</span>`).join('');
  const sc = score !== undefined ? `<span class="score">${score}</span> · ` : '';
  return `<div class="item">
    <div class="head">${sc}<span>${esc(it.source||'')}</span> · ${fmtDate(it.created_at)}</div>
    <div class="content">${esc(it.content||'')}</div>
    ${tags ? `<div class="tags">${tags}</div>` : ''}
  </div>`;
}

// tab 切換
document.querySelectorAll('.tab').forEach(t => {
  t.onclick = () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    const tab = t.dataset.tab;
    document.getElementById('tab-recent').style.display = tab==='recent' ? '' : 'none';
    document.getElementById('tab-search').style.display = tab==='search' ? '' : 'none';
    if (tab==='search') document.getElementById('search-query').focus();
  };
});

init();
</script>
</body>
</html>
"""


# ===========================================================================
# main
# ===========================================================================
def main():
    p = argparse.ArgumentParser(description="vector-memory-mcp dashboard")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--host", default=DEFAULT_BIND,
                   help=f"bind host (預設 {DEFAULT_BIND},本機 only)")
    p.add_argument("--qdrant", default=DEFAULT_QDRANT_URL, help="Qdrant URL")
    p.add_argument("--api-key", default=os.environ.get("QDRANT_API_KEY", ""),
                   help="Qdrant API key (或設 QDRANT_API_KEY)")
    p.add_argument("--model", default=DEFAULT_MODEL, help="embedding 模型名")
    args = p.parse_args()

    qdrant = Qdrant(args.qdrant, args.api_key)
    embedder = Embedder(args.model)

    # 啟動前先確認 Qdrant 可達
    try:
        info = qdrant.root()
        print(f"✓ Qdrant {info.get('version')} @ {args.qdrant}", file=sys.stderr)
    except RuntimeError as e:
        print(f"✗ {e}", file=sys.stderr)
        sys.exit(1)

    app = create_app(qdrant, embedder)

    import uvicorn
    print(f"\n🧠 vector-memory dashboard", file=sys.stderr)
    print(f"   http://{args.host}:{args.port}", file=sys.stderr)
    print(f"   Qdrant: {args.qdrant}", file=sys.stderr)
    print(f"   模型: {args.model} (懶載入,搜尋時才載)", file=sys.stderr)
    print(f"   Ctrl+C 停止\n", file=sys.stderr)

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
