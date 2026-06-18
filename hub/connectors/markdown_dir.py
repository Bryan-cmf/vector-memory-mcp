"""MarkdownDirConnector — 採集指定目錄的 .md/.txt 檔案。

通用化的 auto_sync:支援多目錄、Markdown-aware 分塊。
預設掃描 ~/.openclaw/workspace/memory 與使用者 Documents 下的 .md。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from connectors.base import Record, now_iso

# 預設掃描目錄 (可被環境變數覆蓋)
# 注意: 不含 ~/Documents,因為那會掃到海量無關檔案;用戶可用 MEMORY_EXTRA_DIRS 指定
DEFAULT_DIRS = [
    str(Path.home() / ".openclaw" / "workspace" / "memory"),
]
EXTRA_DIRS = os.environ.get("MEMORY_EXTRA_DIRS", "")
MAX_FILE_SIZE = 500_000           # 500KB
SUPPORTED_EXT = {".md", ".txt", ".markdown"}
SKIP_PATTERNS = {".git", "node_modules", ".venv", "__pycache__", ".Trash", "Library"}


def _chunk_markdown(text: str, source: str, max_chunk: int = 800) -> list[dict]:
    """Markdown-aware 分塊: 按 heading 切,每塊不超過 max_chunk 字。"""
    lines = text.split("\n")
    chunks: list[dict] = []
    current: list[str] = []
    current_heading = ""
    current_path = []

    def flush():
        nonlocal current, current_heading
        if not current:
            return
        content = "\n".join(current).strip()
        if len(content) >= 10:                    # 太短的丟掉
            section = " > ".join(current_path) if current_path else source
            chunks.append({
                "content": content,
                "section_path": section,
                "char_length": len(content),
            })
        current = []

    for line in lines:
        # 偵測 heading
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            level = len(m.group(1))
            heading = m.group(2).strip()
            # 結束前一塊
            if current and sum(len(x) + 1 for x in current) > max_chunk:
                flush()
            # 更新 section path (只保留到當前層級)
            current_path = current_path[:level-1] + [heading]
            current_heading = heading
            current.append(line)
        else:
            current.append(line)
            # 塊太大就 flush
            if sum(len(x) + 1 for x in current) > max_chunk:
                flush()
    flush()
    return chunks


class MarkdownDirConnector:
    """採集多個目錄下的 markdown/text 檔案。"""

    name = "markdown_dir"

    def __init__(self, state: dict | None = None, dirs: list[str] | None = None):
        self.state = state or {}
        self.dirs = dirs or ([d for d in DEFAULT_DIRS if Path(d).exists()]
                             + [d.strip() for d in EXTRA_DIRS.split(",") if d.strip()])

    def _should_skip(self, path: Path) -> bool:
        for part in path.parts:
            if part in SKIP_PATTERNS:
                return True
        return False

    def _discover_files(self) -> list[Path]:
        files: list[Path] = []
        for d in self.dirs:
            root = Path(d).expanduser()
            if not root.exists():
                continue
            for p in root.rglob("*"):
                if not p.is_file():
                    continue
                if self._should_skip(p):
                    continue
                if p.suffix.lower() not in SUPPORTED_EXT:
                    continue
                if p.name.startswith("."):
                    continue
                try:
                    if p.stat().st_size > MAX_FILE_SIZE:
                        continue
                except OSError:
                    continue
                files.append(p)
        return sorted(files)

    def is_available(self) -> bool:
        return any(Path(d).expanduser().exists() for d in self.dirs)

    def discover(self) -> int:
        # 估算: 檔案數 × 平均 3 塊
        return len(self._discover_files()) * 3

    def collect(self) -> Iterator[Record]:
        last_ts = self.last_collected()
        last_mtime = 0.0
        if last_ts:
            try:
                last_mtime = datetime.fromisoformat(last_ts).timestamp()
            except ValueError:
                pass

        for f in self._discover_files():
            try:
                mtime = f.stat().st_mtime
                # 增量: 只採 mtime > last_collected 的檔案
                if last_mtime and mtime <= last_mtime:
                    continue
                content = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            chunks = _chunk_markdown(content, str(f))
            for chunk in chunks:
                # 從路徑推 tags
                tags = []
                parts = f.parts
                if "memory" in parts:
                    idx = parts.index("memory")
                    if idx + 1 < len(parts):
                        tags.append(f"sub:{parts[idx+1]}")
                tags.append("format:md")
                try:
                    rel = str(f.relative_to(Path.home()))
                except ValueError:
                    rel = str(f)
                yield Record(
                    content=chunk["content"],
                    source_agent="markdown",
                    source_type="note",
                    source_path=rel,
                    source_id=f"{rel}#{chunk['section_path'][:40]}",
                    created_at=datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
                    tags=tags,
                    importance=0.4,
                    metadata={
                        "filename": f.name,
                        "section_path": chunk["section_path"],
                        "char_length": chunk["char_length"],
                    },
                )

    def last_collected(self) -> str:
        return self.state.get("markdown_last", "")

    def set_collected(self, ts: str) -> None:
        self.state["markdown_last"] = ts
