"""FastAPI app + background SQS consumer loop.

BDD coverage:
- All features — this is the entry point that wires everything together.
"""

from __future__ import annotations

import asyncio
import json
import logging

import boto3
from fastapi import FastAPI

from whitelabel_wpp.config import get_settings
from whitelabel_wpp.models import SQSMessage
from whitelabel_wpp.neo4j_client import Neo4jClient
from whitelabel_wpp.milvus_client import MilvusClient
from whitelabel_wpp.telnyx_client import TelnyxClient
from whitelabel_wpp.processor import Processor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="whitelabel-wpp", version="0.1.0")

# ── Globals (initialized on startup) ──────────────────────────────────
_processor: Processor | None = None
_sqs_task: asyncio.Task | None = None


@app.on_event("startup")
async def startup():
    global _processor, _sqs_task

    settings = get_settings()
    neo4j = Neo4jClient(settings)
    milvus = MilvusClient(settings)
    telnyx = TelnyxClient(settings)

    _processor = Processor(
        neo4j=neo4j,
        milvus=milvus,
        telnyx=telnyx,
        business_number="",  # set from config or WABA
    )

    # Start background SQS consumer
    if settings.sqs_queue_url:
        _sqs_task = asyncio.create_task(_sqs_consumer(settings.sqs_queue_url))
        logger.info("SQS consumer started: %s", settings.sqs_queue_url)
    else:
        logger.warning("No SQS_QUEUE_URL — running without SQS consumer")


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
    return {"status": "ok"}


@app.post("/process")
async def manual_process(body: dict):
    """Manual trigger for dev/testing."""
    if not _processor:
        return {"error": "processor not initialized"}
    msg = SQSMessage(**body)
    await _processor.dispatch(msg)
    return {"status": "processed", "action": msg.action}


# ── SQS Consumer ──────────────────────────────────────────────────────

async def _sqs_consumer(queue_url: str):
    """Background asyncio task that polls SQS and dispatches messages."""
    sqs = boto3.client("sqs")
    logger.info("SQS consumer polling %s", queue_url)

    while True:
        try:
            response = await asyncio.to_thread(
                sqs.receive_message,
                QueueUrl=queue_url,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=20,  # long polling
            )

            messages = response.get("Messages", [])
            for sqs_msg in messages:
                try:
                    body = json.loads(sqs_msg["Body"])
                    msg = SQSMessage(**body)
                    await _processor.dispatch(msg)

                    # Delete from queue on success
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
