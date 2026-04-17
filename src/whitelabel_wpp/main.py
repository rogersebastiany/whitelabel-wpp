"""FastAPI app — local mode (whatsmeow bridge) or production (SQS + Telnyx)."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import FastAPI

from whitelabel_wpp.config import get_settings
from whitelabel_wpp.models import SQSMessage
from whitelabel_wpp.neo4j_client import Neo4jClient
from whitelabel_wpp.milvus_client import MilvusClient
from whitelabel_wpp.processor import Processor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="whitelabel-wpp", version="0.1.0")

_processor: Processor | None = None
_sqs_task: asyncio.Task | None = None


def _create_messaging_client(settings):
    if settings.mode == "local":
        from whitelabel_wpp.whatsmeow_client import WhatsmeowClient
        return WhatsmeowClient(settings.bridge_url)
    else:
        from whitelabel_wpp.telnyx_client import TelnyxClient
        return TelnyxClient(settings)


@app.on_event("startup")
async def startup():
    global _processor, _sqs_task

    settings = get_settings()
    neo4j = Neo4jClient(settings)
    milvus = MilvusClient(settings)
    messaging = _create_messaging_client(settings)

    _processor = Processor(
        neo4j=neo4j,
        milvus=milvus,
        telnyx=messaging,
        business_number="",
    )

    logger.info("Mode: %s", settings.mode)

    if settings.mode == "local":
        logger.info("Local mode — bridge at %s, webhook at POST /webhook/whatsmeow", settings.bridge_url)
    elif settings.sqs_queue_url:
        _sqs_task = asyncio.create_task(_sqs_consumer(settings.sqs_queue_url))
        logger.info("SQS consumer started: %s", settings.sqs_queue_url)
    else:
        logger.warning("No SQS_QUEUE_URL and not local mode — no message source configured")


@app.on_event("shutdown")
async def shutdown():
    if _sqs_task:
        _sqs_task.cancel()
    if _processor:
        await _processor.telnyx.close()
        await _processor.neo4j.close()
        _processor.milvus.close()


# ── Endpoints ──────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "mode": get_settings().mode}


@app.post("/webhook/whatsmeow")
async def whatsmeow_webhook(body: dict):
    """Receive messages from the local whatsmeow bridge."""
    if not _processor:
        return {"error": "processor not initialized"}

    msg = SQSMessage(**body)
    logger.info(
        "Bridge message: from=%s group=%s owner=%s text_len=%d",
        msg.sender_phone, msg.group_id, msg.owner_query, len(msg.text),
    )
    await _processor.dispatch(msg)
    return {"status": "processed", "action": msg.action}


@app.post("/process")
async def manual_process(body: dict):
    """Manual trigger for dev/testing."""
    if not _processor:
        return {"error": "processor not initialized"}
    msg = SQSMessage(**body)
    await _processor.dispatch(msg)
    return {"status": "processed", "action": msg.action}


# ── SQS Consumer (production mode) ───────────────────────────────────

async def _sqs_consumer(queue_url: str):
    import boto3

    sqs = boto3.client("sqs")
    logger.info("SQS consumer polling %s", queue_url)

    while True:
        try:
            response = await asyncio.to_thread(
                sqs.receive_message,
                QueueUrl=queue_url,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=20,
            )

            messages = response.get("Messages", [])
            for sqs_msg in messages:
                try:
                    body = json.loads(sqs_msg["Body"])
                    msg = SQSMessage(**body)
                    await _processor.dispatch(msg)

                    await asyncio.to_thread(
                        sqs.delete_message,
                        QueueUrl=queue_url,
                        ReceiptHandle=sqs_msg["ReceiptHandle"],
                    )
                except Exception:
                    logger.exception("Failed to process SQS message: %s", sqs_msg.get("MessageId"))

        except asyncio.CancelledError:
            logger.info("SQS consumer shutting down")
            break
        except Exception:
            logger.exception("SQS polling error")
            await asyncio.sleep(5)
