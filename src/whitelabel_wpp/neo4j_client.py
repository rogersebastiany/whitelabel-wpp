"""Neo4j client — interaction graph, IQS queries, engagement metrics.

BDD coverage:
- Message Processing: store_interaction (MERGE Member, CREATE Interaction, REPLIES_TO)
- Key Member IQS: reply_depth, insight_engagement, unique_repliers queries
- Engagement Metrics: aggregation query
- Recurrent Topics: store_topic, get_recurrent_topics
- Discussion Summaries: store_summary, get_summary
- Multi-Tenancy: all queries filter by group_id
- Owner Chat: resolve_owner_groups

From KG: Neo4j Python Driver (bolt, MERGE, execute_query pattern)
From BDD plan: Cypher specs for IQS, engagement, topics, summaries
"""

from __future__ import annotations

import logging
from typing import Any

from neo4j import AsyncGraphDatabase, AsyncDriver

from whitelabel_wpp.config import Settings
from whitelabel_wpp.models import MemberIQS, EngagementMetrics

logger = logging.getLogger(__name__)


class Neo4jClient:
    def __init__(self, settings: Settings):
        self._driver: AsyncDriver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )

    async def close(self):
        await self._driver.close()

    async def _run(self, query: str, **params: Any) -> list[dict]:
        async with self._driver.session() as session:
            result = await session.run(query, params)
            return [record.data() async for record in result]

    # ── Interaction storage (BDD: Message Processing) ──────────────────

    async def store_interaction(
        self,
        phone: str,
        lid: str,
        name: str,
        group_id: str,
        group_name: str,
        msg_id: str,
        timestamp: str,
        has_media: bool,
        reply_to_msg_id: str = "",
    ) -> None:
        """Store interaction metadata in Neo4j. No message text — LGPD."""
        await self._run(
            """
            MERGE (m:Member {phone: $phone})
            ON CREATE SET m.lid = $lid, m.name = $name
            ON MATCH SET m.name = CASE WHEN $name <> '' THEN $name ELSE m.name END
            MERGE (g:Group {group_id: $group_id})
            ON CREATE SET g.name = $group_name
            CREATE (i:Interaction {
                msg_id: $msg_id,
                timestamp: $timestamp,
                group_id: $group_id,
                has_media: $has_media
            })
            CREATE (m)-[:SENT]->(i)
            CREATE (i)-[:IN_GROUP]->(g)
            MERGE (m)-[:BELONGS_TO]->(g)
            """,
            phone=phone, lid=lid, name=name, group_id=group_id,
            group_name=group_name, msg_id=msg_id, timestamp=timestamp,
            has_media=has_media,
        )

        if reply_to_msg_id:
            await self._run(
                """
                MATCH (i:Interaction {msg_id: $msg_id})
                MATCH (parent:Interaction {msg_id: $reply_to_msg_id})
                CREATE (i)-[:REPLIES_TO]->(parent)
                """,
                msg_id=msg_id, reply_to_msg_id=reply_to_msg_id,
            )

    # ── Owner management (BDD: Owner Chat, Multi-Tenancy) ──────────────

    async def register_owner(self, phone: str, name: str) -> None:
        await self._run(
            "MERGE (o:Owner {phone: $phone}) ON CREATE SET o.name = $name",
            phone=phone, name=name,
        )

    async def link_owner_group(self, owner_phone: str, group_id: str) -> None:
        await self._run(
            """
            MATCH (o:Owner {phone: $owner_phone})
            MATCH (g:Group {group_id: $group_id})
            MERGE (o)-[:OWNS]->(g)
            """,
            owner_phone=owner_phone, group_id=group_id,
        )

    async def resolve_owner_groups(self, owner_phone: str) -> list[dict]:
        """BDD: Owner Chat — resolve which groups an owner can query."""
        return await self._run(
            """
            MATCH (o:Owner {phone: $phone})-[:OWNS]->(g:Group)
            RETURN g.group_id AS group_id, g.name AS name
            """,
            phone=owner_phone,
        )

    # ── IQS (BDD: Key Member Identification) ───────────────────────────

    async def get_iqs(self, group_id: str, since: str) -> list[MemberIQS]:
        """Calculate IQS for all members in a group since a timestamp."""
        records = await self._run(
            """
            MATCH (m:Member)-[:BELONGS_TO]->(g:Group {group_id: $group_id})
            OPTIONAL MATCH (m)-[:SENT]->(i:Interaction)-[:REPLIES_TO]->(parent:Interaction)<-[:SENT]-(other:Member)
            WHERE other.phone <> m.phone AND i.timestamp > $since
            WITH m, count(i) AS reply_depth
            OPTIONAL MATCH (m)-[:SENT]->(i2:Interaction)<-[:REPLIES_TO]-(reply:Interaction)
            WHERE i2.timestamp > $since
            WITH m, reply_depth, count(reply) AS insight_engagement
            OPTIONAL MATCH (m)-[:SENT]->(i3:Interaction)<-[:REPLIES_TO]-(r:Interaction)<-[:SENT]-(replier:Member)
            WHERE i3.timestamp > $since AND replier.phone <> m.phone
            WITH m, reply_depth, insight_engagement, count(DISTINCT replier) AS unique_repliers
            RETURN m.phone AS phone, m.name AS name,
                   reply_depth, insight_engagement, unique_repliers
            ORDER BY (reply_depth * 0.3 + insight_engagement * 0.4 + unique_repliers * 0.3) DESC
            """,
            group_id=group_id, since=since,
        )
        results = []
        for r in records:
            m = MemberIQS(
                phone=r["phone"], name=r.get("name", ""),
                reply_depth=r["reply_depth"],
                insight_engagement=r["insight_engagement"],
                unique_repliers=r["unique_repliers"],
            )
            m.calculate_iqs()
            results.append(m)
        return results

    # ── Engagement (BDD: Engagement Metrics) ───────────────────────────

    async def get_engagement(self, group_id: str, since: str) -> list[EngagementMetrics]:
        records = await self._run(
            """
            MATCH (m:Member)-[:SENT]->(i:Interaction)-[:IN_GROUP]->(g:Group {group_id: $group_id})
            WHERE i.timestamp > $since
            WITH m, count(i) AS msg_count,
                 count(CASE WHEN i.has_media THEN 1 END) AS media_count,
                 collect(DISTINCT date(datetime({epochSeconds: toInteger(i.timestamp)}))) AS active_days
            RETURN m.phone AS phone, m.name AS name,
                   msg_count AS messages_sent, media_count,
                   size(active_days) AS active_days
            ORDER BY msg_count DESC
            """,
            group_id=group_id, since=since,
        )
        return [EngagementMetrics(**r) for r in records]

    # ── Topics (BDD: Recurrent Topic Detection) ────────────────────────

    async def store_topic(self, topic_name: str, group_id: str, member_phone: str, msg_id: str) -> None:
        await self._run(
            """
            MERGE (t:Topic {name: $topic_name})
            MERGE (g:Group {group_id: $group_id})
            MERGE (t)-[r:RECURS_IN]->(g)
            ON CREATE SET r.count = 1, r.last_seen = timestamp()
            ON MATCH SET r.count = r.count + 1, r.last_seen = timestamp()
            WITH t
            MATCH (i:Interaction {msg_id: $msg_id})
            MERGE (i)-[:MENTIONS_TOPIC]->(t)
            WITH t
            MATCH (m:Member {phone: $member_phone})
            MERGE (t)-[:DISCUSSED_BY]->(m)
            """,
            topic_name=topic_name, group_id=group_id,
            member_phone=member_phone, msg_id=msg_id,
        )

    async def get_recurrent_topics(self, group_id: str, since: str, limit: int = 20) -> list[dict]:
        return await self._run(
            """
            MATCH (t:Topic)-[r:RECURS_IN]->(g:Group {group_id: $group_id})
            WHERE r.last_seen > toInteger($since)
            RETURN t.name AS name, r.count AS count, r.last_seen AS last_seen
            ORDER BY r.count DESC
            LIMIT $limit
            """,
            group_id=group_id, since=since, limit=limit,
        )

    # ── Summaries (BDD: Discussion Summaries) ──────────────────────────

    async def store_summary(
        self, group_id: str, period_start: int, period_end: int,
        text: str, embedding_id: str = "",
    ) -> None:
        await self._run(
            """
            MATCH (g:Group {group_id: $group_id})
            CREATE (s:Summary {
                group_id: $group_id,
                period_start: $period_start,
                period_end: $period_end,
                text: $text,
                embedding_id: $embedding_id
            })
            CREATE (s)-[:COVERS]->(g)
            """,
            group_id=group_id, period_start=period_start,
            period_end=period_end, text=text, embedding_id=embedding_id,
        )

    async def get_latest_summary(self, group_id: str) -> dict | None:
        records = await self._run(
            """
            MATCH (s:Summary {group_id: $group_id})
            RETURN s.text AS text, s.period_start AS period_start,
                   s.period_end AS period_end
            ORDER BY s.period_end DESC LIMIT 1
            """,
            group_id=group_id,
        )
        return records[0] if records else None

    async def get_active_groups(self) -> list[dict]:
        return await self._run(
            """
            MATCH (g:Group)<-[:IN_GROUP]-(i:Interaction)
            WITH g, max(i.timestamp) AS last_activity
            RETURN g.group_id AS group_id, g.name AS name, last_activity
            ORDER BY last_activity DESC
            """
        )

    async def get_interaction_count(self, group_id: str, since: str) -> int:
        records = await self._run(
            """
            MATCH (i:Interaction)-[:IN_GROUP]->(g:Group {group_id: $group_id})
            WHERE i.timestamp > $since
            RETURN count(i) AS count
            """,
            group_id=group_id, since=since,
        )
        return records[0]["count"] if records else 0
