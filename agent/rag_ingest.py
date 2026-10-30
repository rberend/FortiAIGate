#!/usr/bin/env python3
"""rag_ingest.py - build or extend the RAG index in Qdrant.

Reads documents (PDF, Markdown, text, HTML), splits them into overlapping
chunks, embeds each chunk with Ollama, and upserts them into a Qdrant
collection. Chunk ids are deterministic, so re-running updates a file's chunks
instead of duplicating them - drop new files in and run again to extend.

    pip install -r requirements.txt     # qdrant-client, pypdf
    ollama pull nomic-embed-text         # on the Ollama server

    # index the default folder (rag_docs/ in the repo):
    python agent/rag_ingest.py

    # or a specific file or folder:
    python agent/rag_ingest.py ~/fortisiem-docs
    python agent/rag_ingest.py ~/fortisiem-docs/some-guide.pdf

See rag_common.py for the RAG_* configuration variables.
"""
from __future__ import annotations

import sys
from pathlib import Path

from rag_common import (
    COLLECTION,
    EMBED_MODEL,
    OLLAMA_URL,
    chunk_text,
    embed_texts,
    ensure_collection,
    get_qdrant,
    iter_document_paths,
    load_document,
    point_id,
)

BATCH = 64


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def ingest(target: Path) -> int:
    records: list[tuple[str, int, str]] = []   # (source, chunk index, text)
    files = list(iter_document_paths(target))
    if not files:
        print(f"No supported documents found under {target}")
        print("Supported types: .pdf .md .markdown .txt .html .htm")
        return 0

    base = target if target.is_dir() else target.parent
    for path in files:
        try:
            text = load_document(path)
        except Exception as exc:
            print(f"  skip {path.name}: {type(exc).__name__}: {exc}")
            continue
        if not text or not text.strip():
            print(f"  skip {path.name}: no extractable text")
            continue
        try:
            source = str(path.relative_to(base))
        except ValueError:
            source = path.name
        chunks = chunk_text(text)
        for index, chunk in enumerate(chunks):
            records.append((source, index, chunk))
        print(f"  {path.name}: {len(chunks)} chunks")

    if not records:
        print("Nothing to index.")
        return 0

    from qdrant_client.models import PointStruct

    print(f"\nEmbedding {len(records)} chunks with {EMBED_MODEL} at {OLLAMA_URL} ...")
    created = False
    total = 0
    for start in range(0, len(records), BATCH):
        batch = records[start:start + BATCH]
        vectors = embed_texts([text for _, _, text in batch])
        if not created:
            ensure_collection(dim=len(vectors[0]))
            created = True
        points = [
            PointStruct(
                id=point_id(source, index),
                vector=vector,
                payload={"source": source, "chunk": index, "text": text},
            )
            for (source, index, text), vector in zip(batch, vectors)
        ]
        get_qdrant().upsert(collection_name=COLLECTION, points=points)
        total += len(points)
        print(f"  upserted {total}/{len(records)}")

    print(f"\nDone. {total} chunks in collection '{COLLECTION}'.")
    return total


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg:
        target = Path(arg).expanduser().resolve()
    else:
        import os
        target = Path(os.environ.get("RAG_DOCS_DIR", _repo_root() / "rag_docs")).expanduser().resolve()
    if not target.exists():
        print(f"Path not found: {target}")
        print("Create it and add documents, or pass a path: python agent/rag_ingest.py <file-or-folder>")
        return 1
    print(f"Ingesting from {target}")
    ingest(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
