# whitelabel-wpp

WhatsApp Group Analytics Platform — white-label SaaS that listens to group conversations via Meta Cloud API, extracts insights, and lets owners query their data through WhatsApp.

## Features

- **Group Discussion Summaries** — summarize conversations within a time frame, stored in DB
- **Recurrent Topic Memory** — track topics that keep coming up across conversations
- **Key Member Identification** — interaction quality score based on replies, insights, and engagement depth (not just message count)
- **Engagement Metrics** — activity tracking linked to phone number / LID
- **Owner Chat Interface** — 1:1 WhatsApp chat where owners query their group analytics

## Architecture

```
Meta Cloud API ──webhook──> API Gateway + Lambda (ingestion)
                                    │
                                    ▼
                                   SQS
                                    │
                                    ▼
                            ECS Fargate (processing)
                           ┌────┼────┬────────┐
                           ▼    ▼    ▼        ▼
                        Neo4j Milvus LanceDB Cognee
                           │    │    │        │
                           └────┴────┴────────┘
                                    │
                                    ▼
                         Owner 1:1 Chat (query)
                                    │
                                    ▼
                         Cloud API ──reply──> WhatsApp
```

### Data Flow

1. **Ingestion** — webhook receives group messages, validates signature, stores in Neo4j, queues for processing
2. **Processing** — embeds messages (Milvus), extracts entities/topics (Cognee), indexes for hybrid search (LanceDB)
3. **Analytics** — scheduled summaries, topic recurrence detection, interaction quality scoring via Neo4j graph queries
4. **Owner Interface** — natural language queries in 1:1 chat, resolved against Neo4j/Milvus, responses sent via Cloud API

### Key Member Identification (IQS)

Not just message volume. The Interaction Quality Score measures:

- **Reply depth** (0.3) — how often does this person reply to others?
- **Insight engagement** (0.4) — when they post, do others reply?
- **Unique repliers** (0.3) — are multiple people engaging with their messages?

A person who throws messages that nobody responds to scores low. A person whose contributions spark discussion scores high.

### Neo4j Data Model

```
(:Owner {phone, name, plan})
(:Group {group_id, name, owner_phone})
(:Member {phone, lid, name})
(:Message {msg_id, text, timestamp, group_id, media_type})
(:Topic {name, embedding_id})
(:Summary {group_id, period_start, period_end, text, embedding_id})

(:Owner)-[:OWNS]->(:Group)
(:Member)-[:BELONGS_TO]->(:Group)
(:Member)-[:SENT]->(:Message)
(:Message)-[:IN_GROUP]->(:Group)
(:Message)-[:REPLIES_TO]->(:Message)
(:Message)-[:MENTIONS_TOPIC]->(:Topic)
(:Topic)-[:RECURS_IN]->(:Group)
(:Summary)-[:COVERS]->(:Group)
```

## Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.12 |
| WhatsApp API | Meta Cloud API + Webhooks |
| Graph DB | Neo4j |
| Vector DB | Milvus |
| Hybrid Search | LanceDB |
| KG Extraction | Cognee |
| LLM | OpenAI |
| Web Framework | FastAPI |
| Infrastructure | AWS (Lambda, API Gateway, SQS, ECS Fargate, S3, EventBridge) |
| Package Manager | uv |

## Setup

```bash
uv sync
cp .env.example .env  # fill in credentials
```

## Compliance

- LGPD compliant — all phone-linked data treated as PII
- Encryption at rest and in transit
- Tenant isolation by group_id / owner_phone
- Data retention configurable per owner
