"""Configuration loader — Secrets Manager in AWS, env vars locally."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    # Telnyx
    telnyx_api_key: str
    telnyx_messaging_profile_id: str

    # OpenAI
    openai_api_key: str

    # Neo4j
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str

    # Milvus
    milvus_uri: str

    # AWS
    sqs_queue_url: str
    stage: str


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    secrets_arn = os.environ.get("SECRETS_ARN")

    if secrets_arn:
        return _from_secrets_manager(secrets_arn)
    return _from_env()


def _from_secrets_manager(arn: str) -> Settings:
    import boto3

    client = boto3.client("secretsmanager")
    resp = client.get_secret_value(SecretId=arn)
    secrets = json.loads(resp["SecretString"])

    return Settings(
        telnyx_api_key=secrets["TELNYX_API_KEY"],
        telnyx_messaging_profile_id=secrets.get("TELNYX_MESSAGING_PROFILE_ID", ""),
        openai_api_key=secrets.get("OPENAI_API_KEY", ""),
        neo4j_uri=secrets.get("NEO4J_URI", "bolt://localhost:7687"),
        neo4j_user=secrets.get("NEO4J_USER", "neo4j"),
        neo4j_password=secrets.get("NEO4J_PASSWORD", ""),
        milvus_uri=secrets.get("MILVUS_URI", "http://localhost:19530"),
        sqs_queue_url=os.environ.get("SQS_QUEUE_URL", ""),
        stage=os.environ.get("STAGE", "dev"),
    )


def _from_env() -> Settings:
    return Settings(
        telnyx_api_key=os.environ.get("TELNYX_API_KEY", ""),
        telnyx_messaging_profile_id=os.environ.get("TELNYX_MESSAGING_PROFILE_ID", ""),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        neo4j_uri=os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        neo4j_user=os.environ.get("NEO4J_USER", "neo4j"),
        neo4j_password=os.environ.get("NEO4J_PASSWORD", ""),
        milvus_uri=os.environ.get("MILVUS_URI", "http://localhost:19530"),
        sqs_queue_url=os.environ.get("SQS_QUEUE_URL", ""),
        stage=os.environ.get("STAGE", "dev"),
    )
