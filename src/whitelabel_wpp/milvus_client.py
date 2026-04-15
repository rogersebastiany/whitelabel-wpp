"""Milvus client — topic and summary embeddings, semantic search + dedup.

BDD coverage:
- Message Processing: topic semantic dedup (cosine > 0.85 = merge)
- Discussion Summaries: embed + store summaries, cross-period retrieval
- Owner Chat: semantic search for query resolution
- Multi-Tenancy: group_id filter on all searches

From Marvin's memory.py: OpenAI text-embedding-3-small, pymilvus Collection,
IVF_FLAT index, _embed() pattern.
"""

from __future__ import annotations

import logging

import openai
from pymilvus import (
    Collection, CollectionSchema, DataType, FieldSchema,
    MilvusClient as PyMilvusClient, connections, utility,
)

from whitelabel_wpp.config import Settings

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
MAX_EMBED_CHARS = 30000
TOPIC_DEDUP_THRESHOLD = 0.85


class MilvusClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._openai = openai.OpenAI(api_key=settings.openai_api_key)
        connections.connect(uri=settings.milvus_uri)
        self._ensure_collections()

    def _ensure_collections(self):
        if not utility.has_collection("wpp_topics"):
            schema = CollectionSchema([
                FieldSchema("id", DataType.INT64, is_primary=True, auto_id=True),
                FieldSchema("topic_name", DataType.VARCHAR, max_length=500),
                FieldSchema("group_id", DataType.VARCHAR, max_length=200),
                FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=EMBEDDING_DIM),
                FieldSchema("created_at", DataType.INT64),
            ])
            col = Collection("wpp_topics", schema)
            col.create_index("embedding", {
                "index_type": "HNSW", "metric_type": "COSINE",
                "params": {"M": 16, "efConstruction": 256},
            })
            col.load()

        if not utility.has_collection("wpp_summaries"):
            schema = CollectionSchema([
                FieldSchema("id", DataType.INT64, is_primary=True, auto_id=True),
                FieldSchema("group_id", DataType.VARCHAR, max_length=200),
                FieldSchema("period_start", DataType.INT64),
                FieldSchema("period_end", DataType.INT64),
                FieldSchema("summary_text", DataType.VARCHAR, max_length=10000),
                FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=EMBEDDING_DIM),
            ])
            col = Collection("wpp_summaries", schema)
            col.create_index("embedding", {
                "index_type": "HNSW", "metric_type": "COSINE",
                "params": {"M": 16, "efConstruction": 256},
            })
            col.load()

    def _embed(self, text: str) -> list[float]:
        truncated = text[:MAX_EMBED_CHARS]
        response = self._openai.embeddings.create(
            input=truncated, model=EMBEDDING_MODEL,
        )
        return response.data[0].embedding

    # ── Topic dedup (BDD: Message Processing — semantic dedup) ─────────

    def find_similar_topic(self, topic_name: str, group_id: str) -> dict | None:
        """Check if a similar topic exists (cosine > 0.85). Returns match or None."""
        vec = self._embed(topic_name)
        col = Collection("wpp_topics")
        results = col.search(
            data=[vec],
            anns_field="embedding",
            param={"metric_type": "COSINE", "params": {"ef": 64}},
            limit=1,
            expr=f'group_id == "{group_id}"',
            output_fields=["topic_name", "group_id"],
        )
        if results and results[0]:
            hit = results[0][0]
            if hit.score >= TOPIC_DEDUP_THRESHOLD:
                return {
                    "topic_name": hit.entity.get("topic_name"),
                    "score": hit.score,
                    "id": hit.id,
                }
        return None

    def store_topic(self, topic_name: str, group_id: str, created_at: int) -> int:
        """Store a new topic embedding. Returns the inserted ID."""
        vec = self._embed(topic_name)
        col = Collection("wpp_topics")
        result = col.insert([{
            "topic_name": topic_name,
            "group_id": group_id,
            "embedding": vec,
            "created_at": created_at,
        }])
        col.flush()
        return result.primary_keys[0]

    # ── Summary embeddings (BDD: Discussion Summaries) ─────────────────

    def store_summary(
        self, group_id: str, period_start: int, period_end: int,
        summary_text: str,
    ) -> int:
        vec = self._embed(summary_text)
        col = Collection("wpp_summaries")
        result = col.insert([{
            "group_id": group_id,
            "period_start": period_start,
            "period_end": period_end,
            "summary_text": summary_text,
            "embedding": vec,
        }])
        col.flush()
        return result.primary_keys[0]

    def search_summaries(self, query: str, group_id: str, limit: int = 5) -> list[dict]:
        """Semantic search across summaries for a group."""
        vec = self._embed(query)
        col = Collection("wpp_summaries")
        results = col.search(
            data=[vec],
            anns_field="embedding",
            param={"metric_type": "COSINE", "params": {"ef": 64}},
            limit=limit,
            expr=f'group_id == "{group_id}"',
            output_fields=["summary_text", "period_start", "period_end"],
        )
        hits = []
        if results and results[0]:
            for hit in results[0]:
                hits.append({
                    "summary_text": hit.entity.get("summary_text"),
                    "period_start": hit.entity.get("period_start"),
                    "period_end": hit.entity.get("period_end"),
                    "score": hit.score,
                })
        return hits

    def close(self):
        connections.disconnect("default")
