#!/usr/bin/env python3
"""rag_common.py - shared helpers for the agent's RAG tool.

Document loading, chunking, Ollama embeddings and the Qdrant client, used by
both rag_ingest.py (build/extend the index) and rag_server.py (search it).

Configuration (environment variables, all optional):
    RAG_OLLAMA_URL    Ollama base URL for embeddings.   Default: http://localhost:11434
    RAG_EMBED_MODEL   Embedding model.                  Default: nomic-embed-text
    RAG_QDRANT_URL    Qdrant server URL.                Default: http://localhost:6333
    RAG_QDRANT_PATH   Embedded Qdrant path (instead of a server; single process).
    RAG_COLLECTION    Qdrant collection name.           Default: fortinet_docs
    RAG_DOCS_DIR      Default folder to ingest.         Default: <repo>/rag_docs
    RAG_CHUNK_CHARS   Target chunk size in characters.  Default: 1200
    RAG_CHUNK_OVERLAP Overlap between chunks.           Default: 150
    RAG_TOP_K         Default number of search hits.    Default: 5

Embeddings go straight to Ollama; only the search *results* travel back through
FortiAIGate (as the tool's output), where the gateway inspects them.
"""
from __future__ import annotations

import os
import re
import uuid
from html.parser import HTMLParser
from pathlib import Path

import httpx

OLLAMA_URL = os.environ.get("RAG_OLLAMA_URL", "http://localhost:11434").rstrip("/")
EMBED_MODEL = os.environ.get("RAG_EMBED_MODEL", "nomic-embed-text")
COLLECTION = os.environ.get("RAG_COLLECTION", "fortinet_docs")
CHUNK_CHARS = int(os.environ.get("RAG_CHUNK_CHARS", "1200"))
CHUNK_OVERLAP = int(os.environ.get("RAG_CHUNK_OVERLAP", "150"))
TOP_K = int(os.environ.get("RAG_TOP_K", "5"))

SUPPORTED_SUFFIXES = {".pdf", ".md", ".markdown", ".txt", ".html", ".htm"}
_ID_NAMESPACE = uuid.UUID("6f9c2b1e-1d3a-5e7f-8a2b-9c0d1e2f3a4b")

# Qdrant client is created once per process and shared (embedded mode allows
# only one handle to a given path).
_QDRANT = None


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return "\n".join(parser.parts)


def load_document(path: Path) -> str | None:
    """Return the plain text of one document, or None if the type is unsupported."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader  # imported lazily so non-PDF use needs no pypdf
        reader = PdfReader(str(path))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    if suffix in (".md", ".markdown", ".txt"):
        return path.read_text(encoding="utf-8", errors="ignore")
    if suffix in (".html", ".htm"):
        return _html_to_text(path.read_text(encoding="utf-8", errors="ignore"))
    return None


def iter_document_paths(root: Path):
    """Yield every supported document under a file or folder."""
    if root.is_file():
        if root.suffix.lower() in SUPPORTED_SUFFIXES:
            yield root
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            yield path


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping chunks, preferring paragraph/sentence breaks."""
    text = re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            window = text[start:end]
            cut = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("\n"))
            if cut > size * 0.5:  # only honor a boundary if it is not too early
                end = start + cut + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks


def point_id(source: str, index: int) -> str:
    """A deterministic id so re-ingesting a file updates its chunks instead of duplicating them."""
    return str(uuid.uuid5(_ID_NAMESPACE, f"{source}::{index}"))


# --------------------------------------------------------------------------
# Embeddings (Ollama)
# --------------------------------------------------------------------------

def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts with the Ollama embedding model."""
    if not texts:
        return []
    # Preferred: the batch /api/embed endpoint on current Ollama.
    try:
        resp = httpx.post(f"{OLLAMA_URL}/api/embed",
                          json={"model": EMBED_MODEL, "input": texts}, timeout=120)
        resp.raise_for_status()
        vectors = resp.json().get("embeddings")
        if vectors:
            return vectors
    except Exception:
        pass
    # Fallback: the older per-text /api/embeddings endpoint.
    out: list[list[float]] = []
    for text in texts:
        resp = httpx.post(f"{OLLAMA_URL}/api/embeddings",
                          json={"model": EMBED_MODEL, "prompt": text}, timeout=120)
        resp.raise_for_status()
        out.append(resp.json()["embedding"])
    return out


# --------------------------------------------------------------------------
# Qdrant
# --------------------------------------------------------------------------

def get_qdrant():
    global _QDRANT
    if _QDRANT is None:
        from qdrant_client import QdrantClient
        path = os.environ.get("RAG_QDRANT_PATH")
        if path:
            _QDRANT = QdrantClient(path=path)                # embedded, single process
        else:
            url = os.environ.get("RAG_QDRANT_URL", "http://localhost:6333")
            _QDRANT = QdrantClient(url=url)
    return _QDRANT


def ensure_collection(dim: int) -> None:
    from qdrant_client.models import Distance, VectorParams
    client = get_qdrant()
    if not client.collection_exists(COLLECTION):
        client.create_collection(
            COLLECTION,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
