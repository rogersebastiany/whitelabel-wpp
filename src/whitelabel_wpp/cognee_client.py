"""Topic extraction from message text — OpenAI for POC, Cognee for production.

LGPD: text enters, topics leave. Text is NEVER persisted.
"""

from __future__ import annotations

import json
import logging

import openai

from whitelabel_wpp.models import Topic

logger = logging.getLogger(__name__)


async def extract_topics(text: str, group_id: str, openai_api_key: str = "") -> list[Topic]:
    """Extract topics from text via OpenAI. Text is never stored."""
    if not text.strip() or not openai_api_key:
        return []

    try:
        client = openai.AsyncOpenAI(api_key=openai_api_key)
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract the main topics/entities from this WhatsApp message. "
                        "Return a JSON array of objects with 'name' and 'type' fields. "
                        "Types: person, place, concept, event, product, other. "
                        "Max 5 topics. Short names (1-3 words). "
                        "If the message is too short or trivial, return []."
                    ),
                },
                {"role": "user", "content": text},
            ],
            max_tokens=200,
            temperature=0,
            response_format={"type": "json_object"},
        )

        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        items = data.get("topics", data.get("items", []))
        if isinstance(data, list):
            items = data

        topics = []
        for item in items:
            if isinstance(item, dict) and item.get("name"):
                topics.append(Topic(
                    name=item["name"],
                    entity_type=item.get("type", ""),
                    source_group_id=group_id,
                ))

        logger.info("Extracted %d topics from %d chars for group %s", len(topics), len(text), group_id)
        return topics

    except Exception:
        logger.exception("Topic extraction failed for group %s", group_id)
        return []
