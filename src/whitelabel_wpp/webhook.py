"""Lambda webhook handler for Telnyx events.

BDD coverage:
- Webhook Ingestion: all 5 scenarios

This is deployed inline in CloudFormation but kept here as the source of truth.
Update the CF template ZipFile when this changes.

Telnyx webhook format (from docs/telnyx-whatsapp-bsp.md):
  data.event_type: message.sent | message.delivered | message.read | message.received | message.failed
  data.payload: {id, type, to, from, text, direction, messaging_profile_id}
"""

from __future__ import annotations

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sqs = boto3.client("sqs")
QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")


def handler(event, context):
    method = event.get("httpMethod", "")

    # POST — Telnyx webhook event
    if method == "POST":
        body = event.get("body", "")
        try:
            webhook = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            logger.error("Invalid JSON payload")
            return {"statusCode": 400, "body": "Invalid JSON"}

        data = webhook.get("data", {})
        event_type = data.get("event_type", "")
        payload = data.get("payload", {})

        if event_type == "message.received":
            # BDD: group text message or owner 1:1
            sqs_body = {
                "action": "message_received",
                "sender_phone": payload.get("from", ""),
                "msg_id": payload.get("id", ""),
                "timestamp": data.get("occurred_at", ""),
                "text": payload.get("text", ""),
                "has_media": payload.get("type", "") != "whatsapp"
                    and payload.get("type", "text") != "text",
                "media_type": payload.get("type", ""),
                # group_id and owner_query will be resolved by the processor
                # based on the messaging context
            }
            sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps(sqs_body))
            logger.info("Queued message from %s", payload.get("from", "unknown"))

        elif event_type == "message.failed":
            # BDD: failed message → log + DLQ
            logger.error("Message failed: %s", json.dumps(payload))

        else:
            # message.sent, message.delivered, message.read — log only
            logger.info("Event %s: %s", event_type, payload.get("id", ""))

        return {"statusCode": 200, "body": "ok"}

    # GET — health check (optional)
    if method == "GET":
        return {"statusCode": 200, "body": "whitelabel-wpp webhook active"}

    return {"statusCode": 405, "body": "Method not allowed"}
