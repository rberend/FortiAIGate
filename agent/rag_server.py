#!/usr/bin/env python3
"""rag_server.py - a FastMCP server that searches the RAG index.

Exposes one tool, search_docs, that embeds the query with Ollama, finds the
closest chunks in Qdrant, and returns them with their source. The agent offers
this tool to the model; when the model calls it, the retrieved passages travel
back through FortiAIGate as the tool's result and are inspected there.

Run it directly to smoke-test, or let the agent launch it over stdio via
FAIG_MCP_CONFIG (see mcp.config.rag.example.json). Build the index first with
rag_ingest.py. See rag_common.py for the RAG_* configuration variables.
"""
from __future__ import annotations

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit("The 'mcp' package is missing. Install it with: pip install mcp") from exc

from rag_common import COLLECTION, TOP_K, embed_texts, get_qdrant

mcp = FastMCP("rag-docs")


@mcp.tool()
def search_docs(query: str, k: int = TOP_K) -> dict:
    """Search the indexed documentation (FortiSIEM 7.6 and anything else ingested) for passages relevant to the query.

    Returns the top matches, each with its source file, a similarity score and
    the passage text, for the model to read and cite. Use it to answer product
    questions instead of guessing.
    """
    k = max(1, min(int(k), 20))
    try:
        vector = embed_texts([query])[0]
    except Exception as exc:
        return {"error": f"embedding failed ({type(exc).__name__}: {exc}). Is Ollama running and nomic-embed-text pulled?"}
    try:
        client = get_qdrant()
        if hasattr(client, "query_points"):   # qdrant-client >= 1.10
            hits = client.query_points(collection_name=COLLECTION, query=vector,
                                       limit=k, with_payload=True).points
        else:                                  # older clients
            hits = client.search(collection_name=COLLECTION, query_vector=vector,
                                  limit=k, with_payload=True)
    except Exception as exc:
        return {"error": f"search failed ({type(exc).__name__}: {exc}). Is Qdrant running and the index built (rag_ingest.py)?"}
    results = [
        {
            "source": hit.payload.get("source"),
            "chunk": hit.payload.get("chunk"),
            "score": round(float(hit.score), 3),
            "text": hit.payload.get("text"),
        }
        for hit in hits
    ]
    if not results:
        return {"query": query, "results": [], "note": f"no matches in collection '{COLLECTION}'"}
    return {"query": query, "results": results}


if __name__ == "__main__":
    mcp.run()
