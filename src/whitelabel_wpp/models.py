"""Pydantic models for Telnyx webhook events, SQS messages, and domain objects.

BDD coverage:
- Webhook Ingestion: all scenarios (Telnyx event envelope)
- Message Processing: message_received schema
- Owner Chat: owner_query flag routing
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# ── Telnyx Webhook Models ──────────────────────────────────────────────
# From docs/telnyx-whatsapp-bsp.md — webhook payload structure:
#   data.event_type: "message.sent" | "message.delivered" | "message.read"
#                    | "message.received" | "message.failed"
#   data.payload: message object with from, to, text, type, etc.


class TelnyxWebhookPayload(BaseModel):
    id: str = ""
    type: str = ""  # "whatsapp"
    to: str = ""
    from_: str = Field("", alias="from")
    text: str = ""
    direction: str = ""  # "inbound" | "outbound"
    messaging_profile_id: str = ""

    model_config = {"populate_by_name": True}


class TelnyxWebhookData(BaseModel):
    event_type: str  # message.sent, message.received, etc.
    id: str = ""
    occurred_at: str = ""
    payload: TelnyxWebhookPayload = Field(default_factory=TelnyxWebhookPayload)


class TelnyxWebhookEvent(BaseModel):
    data: TelnyxWebhookData


# ── SQS Message Models ─────────────────────────────────────────────────
# BDD: Webhook Ingestion → queued to SQS with action type


class SQSMessage(BaseModel):
    action: str  # message_received, generate_summary, engagement_report, topic_recurrence
    sender_phone: str = ""
    sender_name: str = ""
    group_id: str = ""
    group_name: str = ""
    msg_id: str = ""
    timestamp: str = ""
    text: str = ""  # present for processing, discarded after
    reply_to_msg_id: str = ""
    has_media: bool = False
    media_type: str = ""  # audio, image, video, document, sticker
    media_url: str = ""
    owner_query: bool = False  # true if 1:1 owner message
    period: str = ""  # for scheduled actions: daily, weekly


# ── Domain Models ──────────────────────────────────────────────────────


class Topic(BaseModel):
    name: str
    entity_type: str = ""
    related_topics: list[str] = Field(default_factory=list)
    source_group_id: str = ""


class MemberIQS(BaseModel):
    phone: str
    name: str = ""
    reply_depth: int = 0
    insight_engagement: int = 0
    unique_repliers: int = 0
    iqs: float = 0.0

    def calculate_iqs(self) -> float:
        self.iqs = (
            self.reply_depth * 0.3
            + self.insight_engagement * 0.4
            + self.unique_repliers * 0.3
        )
        return self.iqs


class EngagementMetrics(BaseModel):
    phone: str
    name: str = ""
    messages_sent: int = 0
    media_count: int = 0
    active_days: int = 0


class Summary(BaseModel):
    group_id: str
    period_start: int  # epoch
    period_end: int  # epoch
    text: str
    embedding_id: str = ""
