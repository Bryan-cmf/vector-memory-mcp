"""vector-memory-hub connectors — 多源採集層。

每個 Connector 實作同一介面,collect.py 協調器跑所有 is_available() 的 connector。
"""
from connectors.base import Connector, Record, dedup_hash
from connectors.markdown_dir import MarkdownDirConnector
from connectors.qdrant_mirror import QdrantMirrorConnector
from connectors.claude_code import ClaudeCodeConnector
from connectors.cursor import CursorConnector
from connectors.zcode import ZCodeConnector

ALL_CONNECTORS = [
    QdrantMirrorConnector,
    MarkdownDirConnector,
    ClaudeCodeConnector,
    CursorConnector,
    ZCodeConnector,
]

__all__ = [
    "Connector", "Record", "dedup_hash",
    "MarkdownDirConnector", "QdrantMirrorConnector",
    "ClaudeCodeConnector", "CursorConnector", "ZCodeConnector",
    "ALL_CONNECTORS",
]
