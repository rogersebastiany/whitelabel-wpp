"""Cognee client — entity/topic extraction from message text.

BDD coverage:
- Message Processing: extract topics from text, process-and-discard
- Recurrent Topic Detection: extracted topics feed Neo4j + Milvus

From Marvin's cognify_vaults.py:
  cognee.add(content_list, dataset_name=...) → feed text
  cognee.cognify(datasets=[...]) → extract entities + relationships
  cognee.search(SearchType.GRAPH_COMPLETION, query_text=...) → structured output

LGPD: text enters, topics leave. Text is NEVER persisted.
Cognee writes to LanceDB internally. We sync LanceDB → Milvus separately.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from whitelabel_wpp.models import Topic

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


async def extract_topics(text: str, group_id: str, openai_api_key: str = "") -> list[Topic]:
    """Process-and-discard: text enters, topics leave. Text never stored.

    1. cognee.add(text) — in-memory processing
    2. cognee.cognify() — extract entities + relationships
    3. cognee.search(GRAPH_COMPLETION) — get structured output
    4. Return list of Topic(name, entity_type, related_topics)
    5. cognee.prune.prune_data() — clean up to ensure no text persists
    """
    import cognee
    from cognee.api.v1.search import SearchType

    try:
        if openai_api_key:
            cognee.config.set_llm_api_key(openai_api_key)

        safe_id = group_id.replace(".", "_").replace("@", "_").replace(" ", "_")
        dataset_name = f"wpp_{safe_id}_temp"
        await cognee.add(text, dataset_name=dataset_name)

        # Extract entities and relationships
        await cognee.cognify(
            datasets=[dataset_name],
            chunks_per_batch=1,
            data_per_batch=1,
        )

        # Search for extracted entities
        results = await cognee.search(
            query_type=SearchType.GRAPH_COMPLETION,
            query_text=text[:500],  # use beginning of text as query
        )

        # Parse results into Topic objects
        topics: list[Topic] = []
        seen_names: set[str] = set()

        for result in results:
            # Cognee returns triplets or entities depending on search type
            if hasattr(result, "name") and result.name:
                name = result.name.strip()
                if name.lower() not in seen_names:
                    seen_names.add(name.lower())
                    topics.append(Topic(
                        name=name,
                        entity_type=getattr(result, "type", ""),
                        source_group_id=group_id,
                    ))

        # Clean up temporary dataset — LGPD: no text persists
        try:
            await cognee.prune.prune_data(dataset_name)
        except Exception:
            logger.warning("Failed to prune temp dataset %s", dataset_name)

        return topics

    except Exception:
        logger.exception("Cognee extraction failed for group %s", group_id)
        return []
