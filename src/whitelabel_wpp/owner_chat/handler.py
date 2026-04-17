"""Owner chat query handler — route intent to data, format response.

BDD coverage:
- Owner Chat: all 6 scenarios
- Multi-Tenancy: owner can only query their own groups
"""

from __future__ import annotations

import time

from whitelabel_wpp.neo4j_client import Neo4jClient
from whitelabel_wpp.milvus_client import MilvusClient
from whitelabel_wpp.owner_chat.intent import classify_intent


async def handle_owner_query(
    message: str,
    owner_phone: str,
    neo4j: Neo4jClient,
    milvus: MilvusClient,
) -> str:
    intent = classify_intent(message)

    # BDD: Multi-Tenancy — resolve owner's groups
    groups = await neo4j.resolve_owner_groups(owner_phone)
    if not groups:
        return "You don't have any groups registered yet. Add me to a WhatsApp group to start."

    # BDD: Owner has multiple groups — ask to specify
    if len(groups) > 1 and not _message_specifies_group(message, groups):
        group_list = "\n".join(f"• {g['name'] or g['group_id']}" for g in groups)
        return f"Which group?\n\n{group_list}"

    group = _resolve_group(message, groups)
    group_id = group["group_id"]
    group_name = group.get("name", group_id)

    match intent:
        case "summary":
            return await _handle_summary(group_id, group_name, neo4j, milvus)
        case "key_members":
            return await _handle_key_members(group_id, group_name, neo4j)
        case "topics":
            return await _handle_topics(group_id, group_name, neo4j)
        case "engagement":
            return await _handle_engagement(group_id, group_name, neo4j)
        case "member_profile":
            return await _handle_member_profile(message, group_id, neo4j)
        case _:
            return ""


async def _handle_summary(
    group_id: str, group_name: str,
    neo4j: Neo4jClient, milvus: MilvusClient,
) -> str:
    since = str(int(time.time()) - 604800)

    topics = await neo4j.get_recurrent_topics(group_id, since)
    engagement = await neo4j.get_engagement(group_id, since)
    interaction_count = await neo4j.get_interaction_count(group_id, since)

    if interaction_count == 0:
        return f"No activity in {group_name} this week."

    topic_names = [t["name"] for t in topics[:7]]
    member_names = [(m.name or m.phone) for m in engagement[:5]]

    blurb = await _mini_summary(group_name, topic_names, len(engagement), interaction_count)

    lines = [f"📋 *{group_name} — 7 days*\n"]
    if blurb:
        lines.append(f"{blurb}\n")
    lines.append(f"{interaction_count} msgs, {len(engagement)} members\n")

    if topics:
        lines.append("*Topics:*")
        for t in topics[:7]:
            lines.append(f"• {t['name']} ({t['count']}x)")

    if engagement:
        lines.append("\n*Most active:*")
        for m in engagement[:5]:
            name = m.name or m.phone
            lines.append(f"• {name}: {m.messages_sent} msgs")

    return "\n".join(lines)


async def _mini_summary(
    group_name: str, topics: list[str], member_count: int, msg_count: int,
) -> str:
    import openai as oai
    from whitelabel_wpp.config import get_settings

    key = get_settings().openai_api_key
    if not key or not topics:
        return ""

    try:
        client = oai.AsyncOpenAI(api_key=key)
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": (
                    f"Group '{group_name}': {msg_count} messages, {member_count} members, "
                    f"topics: {', '.join(topics)}. "
                    "Write a 1-2 sentence summary in Portuguese (BR). Be concise."
                ),
            }],
            max_tokens=100,
            temperature=0.3,
        )
        return resp.choices[0].message.content or ""
    except Exception:
        return ""


async def _handle_key_members(
    group_id: str, group_name: str, neo4j: Neo4jClient,
) -> str:
    since = str(int(time.time()) - 604800)  # last 7 days
    members = await neo4j.get_iqs(group_id, since)

    if not members:
        return f"Not enough interaction data in {group_name} yet."

    top = members[:5]
    lines = [f"🏆 *Key Contributors — {group_name}*\n"]
    for i, m in enumerate(top, 1):
        name = m.name or m.phone
        lines.append(f"{i}. {name} — IQS: {m.iqs:.1f}")
        lines.append(f"   Replies: {m.reply_depth} | Engagement: {m.insight_engagement} | Unique repliers: {m.unique_repliers}")

    lines.append(
        "\n_IQS measures interaction quality: replies to others (30%), "
        "engagement received (40%), unique repliers (30%)._"
    )
    return "\n".join(lines)


async def _handle_topics(
    group_id: str, group_name: str, neo4j: Neo4jClient,
) -> str:
    since = str(int(time.time()) - 604800 * 4)  # last 4 weeks
    topics = await neo4j.get_recurrent_topics(group_id, since)

    if not topics:
        return f"No recurring topics detected in {group_name} yet."

    lines = [f"🔄 *Recurring Topics — {group_name}*\n"]
    for t in topics[:10]:
        lines.append(f"• {t['name']} — mentioned {t['count']}x")

    return "\n".join(lines)


async def _handle_engagement(
    group_id: str, group_name: str, neo4j: Neo4jClient,
) -> str:
    since = str(int(time.time()) - 604800)  # last 7 days
    metrics = await neo4j.get_engagement(group_id, since)

    if not metrics:
        return f"No activity in {group_name} this week."

    lines = [f"📊 *Engagement — {group_name} (7 days)*\n"]
    for m in metrics[:10]:
        name = m.name or m.phone
        lines.append(f"• {name}: {m.messages_sent} msgs, {m.active_days} active days")

    total_msgs = sum(m.messages_sent for m in metrics)
    lines.append(f"\nTotal: {total_msgs} messages from {len(metrics)} members")
    return "\n".join(lines)


async def _handle_member_profile(
    message: str, group_id: str, neo4j: Neo4jClient,
) -> str:
    # Extract phone number from message
    import re
    phone_match = re.search(r"\+?\d{10,15}", message)
    if not phone_match:
        return "Please include a phone number (e.g. +5511999999999)"

    phone = phone_match.group()
    if not phone.startswith("+"):
        phone = f"+{phone}"

    since = str(int(time.time()) - 604800)
    iqs_list = await neo4j.get_iqs(group_id, since)
    member = next((m for m in iqs_list if m.phone == phone), None)

    if not member:
        return f"No data found for {phone} in this group."

    engagement = await neo4j.get_engagement(group_id, since)
    eng = next((e for e in engagement if e.phone == phone), None)

    lines = [f"👤 *Member Profile — {member.name or phone}*\n"]
    lines.append(f"IQS: {member.iqs:.1f}")
    lines.append(f"Reply depth: {member.reply_depth}")
    lines.append(f"Insight engagement: {member.insight_engagement}")
    lines.append(f"Unique repliers: {member.unique_repliers}")
    if eng:
        lines.append(f"\nMessages: {eng.messages_sent}")
        lines.append(f"Active days: {eng.active_days}")
        lines.append(f"Media shared: {eng.media_count}")

    return "\n".join(lines)


def _message_specifies_group(message: str, groups: list[dict]) -> bool:
    lower = message.lower()
    return any(
        (g.get("name") or "").lower() in lower
        for g in groups
        if g.get("name")
    )


def _resolve_group(message: str, groups: list[dict]) -> dict:
    lower = message.lower()
    for g in groups:
        name = g.get("name") or ""
        if name.lower() in lower:
            return g
    return groups[0]  # default to first group
