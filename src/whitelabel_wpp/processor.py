"""SQS message processor — dispatch by action type.

BDD coverage:
- Message Processing: handle_message (all 5 scenarios)
- Discussion Summaries: handle_summary (3 scenarios)
- Engagement Metrics: handle_engagement (2 scenarios)
- Recurrent Topics: handle_topics (2 scenarios)
- Owner Chat: routed from handle_message when owner_query=true
"""

from __future__ import annotations

import logging
import time

from whitelabel_wpp.models import SQSMessage
from whitelabel_wpp.neo4j_client import Neo4jClient
from whitelabel_wpp.milvus_client import MilvusClient
from whitelabel_wpp.telnyx_client import TelnyxClient
from whitelabel_wpp.cognee_client import extract_topics
from whitelabel_wpp.owner_chat.handler import handle_owner_query

logger = logging.getLogger(__name__)


class Processor:
    def __init__(
        self,
        neo4j: Neo4jClient,
        milvus: MilvusClient,
        telnyx: TelnyxClient,
        business_number: str,
    ):
        self.neo4j = neo4j
        self.milvus = milvus
        self.telnyx = telnyx
        self.business_number = business_number

    async def dispatch(self, msg: SQSMessage) -> None:
        handler = ACTION_HANDLERS.get(msg.action)
        if not handler:
            logger.warning("Unknown action: %s", msg.action)
            return
        await handler(self, msg)

    # ── handle_message (BDD: Message Processing) ───────────────────────

    async def handle_message(self, msg: SQSMessage) -> None:
        # BDD: Owner Chat — route 1:1 owner messages
        if msg.owner_query:
            response = await handle_owner_query(
                message=msg.text,
                owner_phone=msg.sender_phone,
                neo4j=self.neo4j,
                milvus=self.milvus,
            )
            if response:
                await self.telnyx.send_text(
                    self.business_number, msg.sender_phone, response,
                )
            return

        # BDD: Store interaction metadata (no text) — LGPD
        await self.neo4j.store_interaction(
            phone=msg.sender_phone,
            lid="",
            name=msg.sender_name,
            group_id=msg.group_id,
            group_name=msg.group_name,
            msg_id=msg.msg_id,
            timestamp=msg.timestamp,
            has_media=msg.has_media,
            reply_to_msg_id=msg.reply_to_msg_id,
        )

        # BDD: Extract topics via Cognee (text in memory only)
        if msg.text:
            topics = await extract_topics(msg.text, msg.group_id, self.milvus._settings.openai_api_key)
            now = int(time.time())

            for topic in topics:
                # BDD: Milvus semantic dedup (cosine > 0.85 = merge)
                existing = self.milvus.find_similar_topic(topic.name, msg.group_id)
                if existing:
                    logger.debug(
                        "Topic '%s' merged with '%s' (score=%.3f)",
                        topic.name, existing["topic_name"], existing["score"],
                    )
                    topic_name = existing["topic_name"]  # use existing name
                else:
                    # BDD: New unique topic — store in Milvus
                    self.milvus.store_topic(topic.name, msg.group_id, now)
                    topic_name = topic.name

                # Store topic edges in Neo4j
                await self.neo4j.store_topic(
                    topic_name=topic_name,
                    group_id=msg.group_id,
                    member_phone=msg.sender_phone,
                    msg_id=msg.msg_id,
                )

        # BDD: raw text discarded — msg goes out of scope here

    # ── handle_summary (BDD: Discussion Summaries) ─────────────────────

    async def handle_summary(self, msg: SQSMessage) -> None:
        now = int(time.time())
        if msg.period == "daily":
            since = str(now - 86400)
        elif msg.period == "weekly":
            since = str(now - 604800)
        else:
            since = str(now - 86400)

        groups = await self.neo4j.get_active_groups()

        for group in groups:
            group_id = group["group_id"]

            # BDD: No activity → no summary
            count = await self.neo4j.get_interaction_count(group_id, since)
            if count == 0:
                continue

            # Get topics for the period
            topics = await self.neo4j.get_recurrent_topics(group_id, since, limit=30)
            topic_names = [t["name"] for t in topics]

            # Get engagement for context
            engagement = await self.neo4j.get_engagement(group_id, since)

            # LLM summarization (OpenAI) — input: topic clusters + interaction patterns
            summary_text = await self._generate_summary(
                group_name=group.get("name", group_id),
                topic_names=topic_names,
                interaction_count=count,
                member_count=len(engagement),
                period=msg.period,
            )

            # Store in Neo4j
            period_end = now
            period_start = now - (86400 if msg.period == "daily" else 604800)
            await self.neo4j.store_summary(
                group_id=group_id,
                period_start=period_start,
                period_end=period_end,
                text=summary_text,
            )

            # Embed in Milvus for cross-period retrieval
            self.milvus.store_summary(
                group_id=group_id,
                period_start=period_start,
                period_end=period_end,
                summary_text=summary_text,
            )

    async def _generate_summary(
        self,
        group_name: str,
        topic_names: list[str],
        interaction_count: int,
        member_count: int,
        period: str,
    ) -> str:
        """LLM summarization — no individual messages, only aggregated patterns."""
        import openai as oai

        prompt = (
            f"Generate a concise {period} discussion summary for the WhatsApp group '{group_name}'.\n"
            f"Period: last {'24 hours' if period == 'daily' else '7 days'}\n"
            f"Total interactions: {interaction_count}\n"
            f"Active members: {member_count}\n"
            f"Topics discussed: {', '.join(topic_names) if topic_names else 'general discussion'}\n\n"
            "Write a 2-3 paragraph summary highlighting key themes, notable activity patterns, "
            "and any topics that received significant attention. Do not reference individual messages."
        )

        client = oai.AsyncOpenAI(api_key=self.milvus._settings.openai_api_key)
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
        )
        return resp.choices[0].message.content or ""

    # ── handle_engagement (BDD: Engagement Metrics + IQS) ──────────────

    async def handle_engagement(self, msg: SQSMessage) -> None:
        now = int(time.time())
        since = str(now - 604800)  # weekly

        groups = await self.neo4j.get_active_groups()
        for group in groups:
            group_id = group["group_id"]

            # BDD: IQS recalculation
            await self.neo4j.get_iqs(group_id, since)

            # BDD: Engagement metrics aggregation
            await self.neo4j.get_engagement(group_id, since)

    # ── handle_topics (BDD: Recurrent Topic Detection) ─────────────────

    async def handle_topics(self, msg: SQSMessage) -> None:
        now = int(time.time())
        since = str(now - 86400 * 30)  # last 30 days for recurrence

        groups = await self.neo4j.get_active_groups()
        for group in groups:
            group_id = group["group_id"]
            await self.neo4j.get_recurrent_topics(group_id, since)
            # Recurrence scores are calculated in the Cypher query
            # Topic dedup is handled at ingestion time (handle_message)


ACTION_HANDLERS = {
    "message_received": Processor.handle_message,
    "generate_summary": Processor.handle_summary,
    "engagement_report": Processor.handle_engagement,
    "topic_recurrence": Processor.handle_topics,
}
