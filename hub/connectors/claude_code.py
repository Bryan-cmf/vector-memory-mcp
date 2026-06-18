"""ClaudeCodeConnector — 採集 ~/.openclaw/agents/ 下的對話/記憶檔案。

每個 agent 子目錄 (alanzxj, analyst, coder-deepseek...) 內可能含 .jsonl/.json/.md。
每行 JSONL 通常是 {role, content, timestamp, ...} 的對話輪次。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Iterator

from connectors.base import Record, now_iso

AGENTS_ROOT = Path.home() / ".openclaw" / "agents"
SUPPORTED = {".jsonl", ".json", ".md", ".txt"}


class ClaudeCodeConnector:
    """採集 ~/.openclaw/agents/ 下各 agent 的對話/記憶。"""

    name = "claude_code"

    def __init__(self, state: dict | None = None):
        self.state = state or {}

    def is_available(self) -> bool:
        return AGENTS_ROOT.exists() and AGENTS_ROOT.is_dir()

    def _agent_dirs(self) -> list[Path]:
        if not AGENTS_ROOT.exists():
            return []
        return sorted([d for d in AGENTS_ROOT.iterdir() if d.is_dir() and not d.name.startswith(".")])

    def _discover_files(self) -> list[Path]:
        files = []
        for agent_dir in self._agent_dirs():
            for p in agent_dir.rglob("*"):
                if p.is_file() and p.suffix.lower() in SUPPORTED:
                    try:
                        if p.stat().st_size > 500_000:
                            continue
                    except OSError:
                        continue
                    files.append(p)
        return files

    def discover(self) -> int:
        return len(self._discover_files())

    def collect(self) -> Iterator[Record]:
        for agent_dir in self._agent_dirs():
            agent_name = agent_dir.name
            for f in self._discover_files_in(agent_dir):
                try:
                    mtime = f.stat().st_mtime
                    created = datetime.fromtimestamp(mtime).isoformat()
                except OSError:
                    created = now_iso()
                try:
                    rel = str(f.relative_to(AGENTS_ROOT))
                except ValueError:
                    rel = str(f)

                suffix = f.suffix.lower()
                if suffix == ".jsonl":
                    yield from self._collect_jsonl(f, agent_name, rel, created)
                elif suffix == ".json":
                    yield from self._collect_json(f, agent_name, rel, created)
                else:  # .md / .txt
                    yield from self._collect_text(f, agent_name, rel, created)

    def _discover_files_in(self, agent_dir: Path) -> list[Path]:
        out = []
        for p in agent_dir.rglob("*"):
            if p.is_file() and p.suffix.lower() in SUPPORTED:
                try:
                    if p.stat().st_size > 500_000:
                        continue
                except OSError:
                    continue
                out.append(p)
        return out

    def _collect_jsonl(self, f: Path, agent: str, rel: str, created: str) -> Iterator[Record]:
        """JSONL: 每行一個 JSON object (對話輪次)。"""
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return
        for i, line in enumerate(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            # 提取 content: 常見欄位
            content = ""
            for k in ("content", "text", "message", "summary"):
                v = obj.get(k)
                if isinstance(v, str) and len(v) > 5:
                    content = v
                    break
                elif isinstance(v, list):
                    # 可能是 [{type:text,text:...}] 格式
                    parts = [x.get("text", "") for x in v if isinstance(x, dict) and x.get("type") == "text"]
                    if parts:
                        content = " ".join(parts)
                        break
            if not content or len(content) < 10:
                continue
            role = obj.get("role", obj.get("type", ""))
            ts = obj.get("timestamp", obj.get("ts", created))
            yield Record(
                content=content,
                source_agent=f"claude_code:{agent}",
                source_type="conversation",
                source_path=rel,
                source_id=f"{rel}#L{i}",
                created_at=str(ts),
                tags=[f"agent:{agent}", f"role:{role}"] if role else [f"agent:{agent}"],
                importance=0.6,
                metadata={"role": role, "line": i},
            )

    def _collect_json(self, f: Path, agent: str, rel: str, created: str) -> Iterator[Record]:
        try:
            obj = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return
        # 嘗試多種結構
        items = []
        if isinstance(obj, list):
            items = obj
        elif isinstance(obj, dict):
            for k in ("messages", "history", "turns", "conversation"):
                if isinstance(obj.get(k), list):
                    items = obj[k]
                    break
            if not items:
                # 整個 dict 當一筆
                content = json.dumps(obj, ensure_ascii=False)[:2000]
                yield Record(
                    content=content, source_agent=f"claude_code:{agent}",
                    source_type="note", source_path=rel, source_id=rel,
                    created_at=created, importance=0.4,
                )
                return
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            content = ""
            for k in ("content", "text", "message"):
                v = item.get(k)
                if isinstance(v, str) and len(v) > 10:
                    content = v
                    break
            if not content:
                continue
            yield Record(
                content=content,
                source_agent=f"claude_code:{agent}",
                source_type="conversation",
                source_path=rel,
                source_id=f"{rel}#{i}",
                created_at=str(item.get("timestamp", created)),
                importance=0.6,
            )

    def _collect_text(self, f: Path, agent: str, rel: str, created: str) -> Iterator[Record]:
        try:
            content = f.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            return
        if len(content) < 20:
            return
        # 簡單分塊 (按雙換行)
        chunks = [c.strip() for c in content.split("\n\n") if len(c.strip()) > 50]
        if not chunks:
            chunks = [content]
        for i, chunk in enumerate(chunks):
            yield Record(
                content=chunk,
                source_agent=f"claude_code:{agent}",
                source_type="note",
                source_path=rel,
                source_id=f"{rel}#p{i}",
                created_at=created,
                importance=0.4,
            )

    def last_collected(self) -> str:
        return self.state.get("claude_code_last", "")

    def set_collected(self, ts: str) -> None:
        self.state["claude_code_last"] = ts
