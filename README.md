# whitelabel-wpp

WhatsApp Group Analytics Platform — white-label SaaS that listens to group conversations via Telnyx (Meta-approved WhatsApp BSP), extracts insights, and lets owners query their data through WhatsApp.

## Features

- **Group Discussion Summaries** — summarize conversations within a time frame, stored in DB
- **Recurrent Topic Memory** — track topics that keep coming up across conversations
- **Key Member Identification** — interaction quality score based on replies, insights, and engagement depth (not just message count)
- **Engagement Metrics** — activity tracking linked to phone number / LID
- **Owner Chat Interface** — 1:1 WhatsApp chat where owners query their group analytics

## Architecture

```
WhatsApp ←→ Meta ←→ Telnyx BSP ──webhook──> API Gateway + Lambda
                                                     │
                                                     ▼
                                               Secrets Manager
                                                     │
                                                     ▼
                                                    SQS ◄── EventBridge Scheduler
                                                     │       (summaries, engagement,
                                                     │        topic recurrence, IQS)
                                                     ▼
                                             ECS Fargate (processing)
                                           0.25 vCPU / 0.5 GB / Spot
                                           auto-scale 0→3 on queue depth
                                            ┌────┼────┬────────┐
                                            ▼    ▼    ▼        ▼
                                         Neo4j Milvus LanceDB  Cognee
                                           │     │    (sync)     │
                                           │     ◄────────────────┘
                                           └─────┘
                                                     │
                                                     ▼
                                          Owner 1:1 Chat (query)
                                                     │
                                                     ▼
                                          Telnyx API ──→ Meta ──→ WhatsApp
```

### LGPD: Process-and-Discard

No raw messages are stored anywhere. The pipeline:
1. Webhook receives message
2. Extracts metadata: sender phone/LID, timestamp, group_id, msg_id, reply context
3. LLM extracts topics/entities from text (text never persisted)
4. Stores only: interaction graph, topics, metrics, aggregated summaries
5. Raw text discarded after processing

### Data Flow

1. **Ingestion** — Lambda receives Telnyx webhook, parses event payload, queues interaction metadata to SQS
2. **Processing** — ECS Fargate consumes SQS, extracts topics (Cognee → LanceDB → Milvus sync), embeds summaries (Milvus)
3. **Analytics** — EventBridge triggers scheduled jobs: daily summaries, weekly engagement reports, topic recurrence (6h), IQS recalculation
4. **Owner Interface** — natural language queries in 1:1 chat, resolved against Neo4j/Milvus, responses sent via Telnyx WhatsApp API

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
(:Interaction {msg_id, timestamp, group_id, has_media})
(:Topic {name, embedding_id})
(:Summary {group_id, period_start, period_end, text, embedding_id})

(:Owner)-[:OWNS]->(:Group)
(:Member)-[:BELONGS_TO]->(:Group)
(:Member)-[:SENT]->(:Interaction)
(:Interaction)-[:IN_GROUP]->(:Group)
(:Interaction)-[:REPLIES_TO]->(:Interaction)
(:Interaction)-[:MENTIONS_TOPIC]->(:Topic)
(:Topic)-[:RECURS_IN]->(:Group)
(:Summary)-[:COVERS]->(:Group)
```

## Deployed Infrastructure (dev)

Stack: `whitelabel-wpp-dev` | Region: `sa-east-1` | IaC: CloudFormation

### API Gateway
- **Type**: REST API
- **Endpoint**: `https://y618087e12.execute-api.sa-east-1.amazonaws.com/dev/webhook`
- **Methods**: GET (webhook verification), POST (incoming events)
- **Auth**: None (Meta validates via verify token, we validate via signature)

### Lambda (Webhook Receiver)
- **Name**: `whitelabel-wpp-webhook-dev`
- **Runtime**: Python 3.12
- **Memory**: 256 MB
- **Timeout**: 30s
- **Logic**: Validates X-Hub-Signature-256 with HMAC-SHA256, parses webhook payload, routes message events to SQS
- **Env vars**: META_VERIFY_TOKEN, META_APP_SECRET, META_ACCESS_TOKEN, META_PHONE_NUMBER_ID, SQS_QUEUE_URL

### SQS
- **Queue**: `whitelabel-wpp-messages-dev`
- **URL**: `https://sqs.sa-east-1.amazonaws.com/525320085764/whitelabel-wpp-messages-dev`
- **Visibility timeout**: 300s
- **Retention**: 24h
- **DLQ**: `whitelabel-wpp-dlq-dev` (14 day retention, 3 max receives)

### ECS Fargate (Processor)
- **Cluster**: `whitelabel-wpp-dev`
- **Task CPU**: 0.25 vCPU (cheapest)
- **Task Memory**: 0.5 GB (cheapest)
- **Capacity**: Fargate Spot 80% / Fargate 20%
- **Desired count**: 0 (scales from zero)
- **Container port**: 8000
- **Secrets**: Injected from Secrets Manager (META_ACCESS_TOKEN, OPENAI_API_KEY, NEO4J_URI, NEO4J_PASSWORD, MILVUS_URI, META_PHONE_NUMBER_ID)

### ALB (Application Load Balancer)
- **DNS**: `wpp-alb-dev-1714740046.sa-east-1.elb.amazonaws.com`
- **Scheme**: Internet-facing
- **Listener**: HTTP:80
- **Health check**: GET /health (30s interval, 2 healthy / 3 unhealthy threshold)
- **Target**: ECS tasks on port 8000

### Auto-scaling
- **Min**: 0 tasks
- **Max**: 3 tasks
- **Scale up**: SQS ApproximateNumberOfMessagesVisible > 0 (1min eval, +1 task; >50 msgs: +2 tasks)
- **Scale down**: SQS queue empty for 5 min (-1 task)
- **Cooldown**: 120s up, 300s down

### ECR (Container Registry)
- **URI**: `525320085764.dkr.ecr.sa-east-1.amazonaws.com/whitelabel-wpp`
- **Scan on push**: Enabled
- **Lifecycle**: Keep last 5 images

### Secrets Manager
- **ARN**: `arn:aws:secretsmanager:sa-east-1:525320085764:secret:whitelabel-wpp/dev/credentials-SI9XnP`
- **Keys**: META_VERIFY_TOKEN, META_APP_SECRET, META_ACCESS_TOKEN, META_PHONE_NUMBER_ID, OPENAI_API_KEY, NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD, MILVUS_URI

### EventBridge Scheduler
| Rule | Schedule | Action | Status |
|------|----------|--------|--------|
| Daily summary | `cron(0 6 * * ? *)` — 6 AM UTC daily | generate_summary | DISABLED |
| Weekly engagement | `cron(0 7 ? * MON *)` — Mondays 7 AM UTC | engagement_report + IQS recalc | DISABLED |
| Topic recurrence | `rate(6 hours)` | topic_recurrence analysis | DISABLED |

All rules disabled until processor is deployed.

### VPC
- **ID**: `vpc-00afd030974f72236`
- **CIDR**: 10.0.0.0/16
- **Subnets**: 10.0.1.0/24 (sa-east-1a), 10.0.2.0/24 (sa-east-1b)
- **Internet Gateway**: Attached
- **Route**: 0.0.0.0/0 → IGW

### CloudWatch
- **Log group**: `/ecs/whitelabel-wpp-processor-dev` (14 day retention)
- **Alarms**: SQS queue depth (triggers auto-scale up/down)

## Spec-Driven Design

### 1. Project Structure

```
whitelabel-wpp/
├── infra/
│   └── template.yaml          # CloudFormation
├── src/
│   └── whitelabel_wpp/
│       ├── __init__.py
│       ├── main.py             # FastAPI app + SQS consumer loop
│       ├── config.py           # Secrets Manager loader, env config
│       ├── webhook.py          # Lambda handler (deployed inline in CF)
│       ├── models.py           # Pydantic models for all schemas
│       ├── telnyx_client.py     # Telnyx WhatsApp API client (send messages, reply)
│       ├── neo4j_client.py     # Neo4j driver, Cypher queries
│       ├── milvus_client.py    # Milvus collections, embed + semantic search
│       ├── cognee_client.py    # Cognee entity/topic extraction (writes to LanceDB)
│       ├── sync.py             # LanceDB → Milvus vector sync (Cognee compat layer)
│       ├── processor.py        # SQS message handler (dispatch by action)
│       ├── analytics/
│       │   ├── __init__.py
│       │   ├── summarizer.py   # Discussion summary generation
│       │   ├── topics.py       # Recurrent topic detection + dedup
│       │   ├── iqs.py          # Interaction Quality Score calculation
│       │   └── engagement.py   # Engagement metrics aggregation
│       └── owner_chat/
│           ├── __init__.py
│           ├── intent.py       # NL intent classification
│           └── handler.py      # Query routing + response formatting
├── tests/
├── Dockerfile
├── pyproject.toml
├── .env.example
└── README.md
```

### 2. SQS Message Schemas

**message_received** (from Lambda webhook):
```json
{
  "action": "message_received",
  "messaging_product": "whatsapp",
  "metadata": {"display_phone_number": "...", "phone_number_id": "..."},
  "contacts": [{"profile": {"name": "..."}, "wa_id": "5511999999999"}],
  "messages": [{
    "id": "wamid.xxx",
    "from": "5511999999999",
    "timestamp": "1681234567",
    "type": "text",
    "text": {"body": "message content here"},
    "context": {"message_id": "wamid.yyy"}
  }],
  "group": {"id": "group_jid", "subject": "Group Name"}
}
```

**Scheduled actions** (from EventBridge):
```json
{"action": "generate_summary", "period": "daily"}
{"action": "engagement_report", "period": "weekly"}
{"action": "topic_recurrence"}
```

### 3. Processor Dispatch (processor.py)

```python
ACTION_HANDLERS = {
    "message_received": handle_message,
    "generate_summary": handle_summary,
    "engagement_report": handle_engagement,
    "topic_recurrence": handle_topics,
}
```

**handle_message** flow:
1. Parse SQS message → Pydantic model
2. Check if group message or 1:1 owner message
3. If owner 1:1 → route to `owner_chat.handler`
4. If group message:
   - a. Store interaction metadata in Neo4j (no text)
   - b. Extract topics via Cognee (text in memory only → LanceDB)
   - c. Sync LanceDB → Milvus (vector transfer, no re-embedding)
   - d. Store topic nodes + edges in Neo4j
   - e. Milvus semantic dedup (cosine > 0.85 = merge)
   - f. Discard raw text

**handle_summary** flow:
1. For each active group:
   - a. Query Neo4j: all interactions in period
   - b. Query Milvus: topic embeddings for the period
   - c. LLM summarization (OpenAI) — input: topic clusters + interaction patterns
   - d. Store Summary node in Neo4j
   - e. Embed summary in Milvus

**handle_engagement** flow:
1. For each active group:
   - a. Run IQS Cypher queries
   - b. Run engagement metric aggregation
   - c. Update Member nodes in Neo4j
   - d. Optionally notify owner via Cloud API

**handle_topics** flow:
1. For each active group:
   - a. Query Neo4j: topics with MENTIONED_IN edges
   - b. Calculate recurrence score per topic
   - c. Milvus semantic dedup: merge near-duplicate topics (cosine > 0.85)
   - d. Update topic recurrence scores

### 4. FastAPI Endpoints (main.py)

```
GET  /health                → 200 {"status": "ok"}
POST /process               → Manual trigger (dev only)
GET  /metrics/{group_id}    → Engagement metrics JSON
GET  /summary/{group_id}    → Latest summary
GET  /iqs/{group_id}        → IQS leaderboard
```

The main loop is a background asyncio task polling SQS — not an HTTP-driven consumer.

### 5. Neo4j Cypher Specifications

**Store interaction:**
```cypher
MERGE (m:Member {phone: $phone})
ON CREATE SET m.lid = $lid, m.name = $name
MERGE (g:Group {group_id: $group_id})
CREATE (i:Interaction {msg_id: $msg_id, timestamp: $timestamp, has_media: $has_media})
CREATE (m)-[:SENT]->(i)
CREATE (i)-[:IN_GROUP]->(g)
// If reply:
MATCH (parent:Interaction {msg_id: $reply_to_msg_id})
CREATE (i)-[:REPLIES_TO]->(parent)
```

**IQS — Reply depth** (how often does member reply to others):
```cypher
MATCH (m:Member {phone: $phone})-[:SENT]->(i:Interaction)-[:REPLIES_TO]->(parent)<-[:SENT]-(other:Member)
WHERE other.phone <> m.phone AND i.timestamp > $since
RETURN count(i) AS reply_depth
```

**IQS — Insight engagement** (how many replies did member's messages get):
```cypher
MATCH (m:Member {phone: $phone})-[:SENT]->(i:Interaction)<-[:REPLIES_TO]-(reply:Interaction)
WHERE i.timestamp > $since
RETURN count(reply) AS insight_engagement
```

**IQS — Unique repliers** (how many different people reply to member):
```cypher
MATCH (m:Member {phone: $phone})-[:SENT]->(i:Interaction)<-[:REPLIES_TO]-(reply)<-[:SENT]-(replier:Member)
WHERE i.timestamp > $since AND replier.phone <> m.phone
RETURN count(DISTINCT replier) AS unique_repliers
```

**IQS formula:** `IQS = (reply_depth × 0.3) + (insight_engagement × 0.4) + (unique_repliers × 0.3)`

**Recurrent topics:**
```cypher
MATCH (t:Topic)-[r:RECURS_IN]->(g:Group {group_id: $group_id})
WHERE r.last_seen > $since
RETURN t.name, r.count, r.last_seen
ORDER BY r.count DESC LIMIT 20
```

**Engagement metrics:**
```cypher
MATCH (m:Member)-[:SENT]->(i:Interaction)-[:IN_GROUP]->(g:Group {group_id: $group_id})
WHERE i.timestamp > $since
WITH m, count(i) AS msg_count,
     count(CASE WHEN i.has_media THEN 1 END) AS media_count,
     collect(DISTINCT date(datetime({epochSeconds: toInteger(i.timestamp)}))) AS active_days
RETURN m.phone, m.name, msg_count, media_count, size(active_days) AS active_day_count
ORDER BY msg_count DESC
```

### 6. Milvus Collections

**topics:**
| Field | Type | Notes |
|-------|------|-------|
| id | int64 | PK, auto |
| topic_name | varchar | |
| group_id | varchar | |
| embedding | float_vector[1536] | OpenAI text-embedding-3-small |
| created_at | int64 | epoch |

Index: HNSW (M=16, efConstruction=256). Dedup: cosine > 0.85 = merge.

**summaries:**
| Field | Type | Notes |
|-------|------|-------|
| id | int64 | PK, auto |
| group_id | varchar | |
| period_start | int64 | epoch |
| period_end | int64 | epoch |
| summary_text | varchar | Aggregated, no individual messages |
| embedding | float_vector[1536] | |

Index: HNSW. Use: cross-period retrieval, owner chat queries.

### 7. Cognee Integration (cognee_client.py)

```python
async def extract_topics(text: str, group_id: str) -> list[Topic]:
    """
    Process-and-discard: text enters, topics leave. Text is never stored.

    1. cognee.add(text) — in-memory only
    2. cognee.cognify() — extract entities + relationships
    3. cognee.search("GRAPH_COMPLETION", query=text) — structured output
    4. Return list of Topic(name, entity_type, related_topics)
    5. Text reference released — GC collects
    """
```

### 8. Owner Chat Interface

**Intent classification** (intent.py):
```python
INTENTS = {
    "summary":        ["summary", "summarize", "what happened", "recap"],
    "key_members":    ["who", "contributors", "key members", "top"],
    "topics":         ["topics", "recurring", "what keeps", "common themes"],
    "engagement":     ["engagement", "how active", "metrics", "stats"],
    "member_profile": ["+55", "phone", "member", "about"],
}
```

LLM-based intent classification with fallback to keyword matching.

**Query routing** (handler.py):
```python
async def handle_owner_query(message: str, owner_phone: str) -> str:
    intent = classify_intent(message)
    group_id = resolve_owner_group(owner_phone)

    match intent:
        case "summary":       return await get_or_generate_summary(group_id, period)
        case "key_members":   return await get_iqs_leaderboard(group_id)
        case "topics":        return await get_recurrent_topics(group_id)
        case "engagement":    return await get_engagement_report(group_id)
        case "member_profile": return await get_member_profile(group_id, phone)
```

**Response via Telnyx API** (telnyx_client.py):
```python
async def send_reply(from_number: str, to: str, text: str):
    """POST to api.telnyx.com/v2/messages/whatsapp"""
    payload = {
        "from": from_number,
        "to": to,
        "whatsapp_message": {
            "type": "text",
            "text": {"body": text, "preview_url": False}
        }
    }
```

### 9. Lambda Webhook Spec (webhook.py)

Receives Telnyx webhook events:

- **POST /webhook**:
  1. Parse Telnyx event envelope (`data.event_type`, `data.payload`)
  2. Route by event type:
     - `message.received` → extract sender, text, group context → SQS
     - `message.delivered` / `message.read` → update delivery status (optional)
     - `message.failed` → log error, DLQ
  3. Return 200 immediately

### 10. Config (config.py)

```python
class Settings:
    # From Secrets Manager
    telnyx_api_key: str
    telnyx_messaging_profile_id: str
    openai_api_key: str
    neo4j_uri: str
    neo4j_password: str
    milvus_uri: str

    # From env
    sqs_queue_url: str
    stage: str
```

## Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.12, Go 1.24 |
| WhatsApp (local POC) | whatsmeow via Go bridge (`bridge/`) |
| WhatsApp (production) | Telnyx BSP (Meta-approved) |
| Graph DB | Neo4j |
| Vector DB | Milvus (etcd + MinIO) |
| Topic Extraction | OpenAI GPT-4o-mini |
| LLM | OpenAI |
| Web Framework | FastAPI |
| IaC | CloudFormation |
| Infrastructure | AWS (Lambda, API Gateway, SQS, ECS Fargate, ALB, ECR, Secrets Manager, EventBridge, CloudWatch) |
| Package Manager | uv |

## Local POC

### Prerequisites

- Go 1.24+
- Python 3.12+ with `uv`
- Docker + Docker Compose
- OpenAI API key

### Setup

```bash
# 1. Install dependencies
uv sync
cd bridge && go build -o wpp-bridge . && cd ..

# 2. Start infrastructure (Neo4j, Milvus, etcd, MinIO, Attu)
docker compose up -d

# 3. Configure
cp .env.example .env
# Fill in: OPENAI_API_KEY, OWNER_PHONE

# 4. Start FastAPI processor
uv run uvicorn whitelabel_wpp.main:app --port 8001

# 5. Start WhatsApp bridge (separate terminal)
cd bridge && ./wpp-bridge --webhook=http://localhost:8001/webhook/whatsmeow --owner=YOUR_PHONE
# First run: scan QR code from WhatsApp → Settings → Linked Devices
# Subsequent runs: reconnects automatically from session.db
```

### Ports

| Service | Port |
|---------|------|
| FastAPI | 8001 |
| Bridge send API | 8080 |
| Neo4j Browser | 17474 |
| Neo4j Bolt | 17687 |
| Milvus gRPC | 19531 |
| Attu (Milvus UI) | 3000 |
| MinIO Console | 19001 |

### Owner Chat Commands

Send these in a 1:1 WhatsApp chat with the linked device:

| Command | What it does |
|---------|-------------|
| **summary** / resumo | Weekly summary — topics, activity, short LLM blurb |
| **topics** / temas / assuntos | Recurring topics list |
| **key members** / quem / destaque | IQS leaderboard |
| **engagement** / metrics / stats | Activity metrics (7 days) |
| **+5511...** / member / membro | Member profile by phone |

### Register an Owner

```bash
uv run python -c "
import asyncio
from dotenv import load_dotenv; load_dotenv()
from whitelabel_wpp.config import get_settings
from whitelabel_wpp.neo4j_client import Neo4jClient

async def setup():
    neo4j = Neo4jClient(get_settings())
    await neo4j.register_owner('PHONE', 'NAME')
    await neo4j.link_owner_group('PHONE', 'GROUP_JID')
    await neo4j.close()

asyncio.run(setup())
"
```

## Deploy (Production — Telnyx)

```bash
# Create stack
aws cloudformation create-stack \
  --stack-name whitelabel-wpp-dev \
  --template-body file://infra/template.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters \
    ParameterKey=MetaVerifyToken,ParameterValue=YOUR_TOKEN \
    ParameterKey=MetaAppSecret,ParameterValue=YOUR_SECRET \
    ParameterKey=MetaAccessToken,ParameterValue=YOUR_ACCESS_TOKEN \
    ParameterKey=MetaPhoneNumberId,ParameterValue=YOUR_PHONE_ID

# Push docker image
aws ecr get-login-password --region sa-east-1 | docker login --username AWS --password-stdin 525320085764.dkr.ecr.sa-east-1.amazonaws.com
docker build -t whitelabel-wpp .
docker tag whitelabel-wpp:latest 525320085764.dkr.ecr.sa-east-1.amazonaws.com/whitelabel-wpp:latest
docker push 525320085764.dkr.ecr.sa-east-1.amazonaws.com/whitelabel-wpp:latest
```

## Client Onboarding — Telnyx BSP

Each client gets a dedicated phone number via **Telnyx DID** (Meta-approved BSP):

- **Telnyx is a Meta WhatsApp Business Solution Provider** — not a workaround, officially sanctioned
- **Single vendor** — number provisioning, WABA registration, messaging API, webhooks all through Telnyx
- **Brazilian DID numbers** — requires CPF/CNPJ + address proof, ~3 business days to approve
- **Embedded Signup** — Telnyx handles Meta's WABA registration internally

### Onboarding flow
1. Telnyx API → purchase Brazilian DID number ($1/mo)
2. Telnyx Embedded Signup → auto-registers number as WABA with Meta
3. Phone number verified via SMS or voice call
4. Telnyx webhook configured → our API Gateway endpoint
5. Client's group starts being monitored
6. Owner phone linked → 1:1 chat query interface active

### Cost per client
- Telnyx DID number: ~$1/mo
- Meta conversation pricing (passed through Telnyx): free for first 1,000 service conversations/mo
- Total: low single-digit USD per client/month for infrastructure

## Multi-Tenancy

Shared instances, partitioned by tenant (group_id / owner_phone). No isolated databases per tenant.

| Service | Isolation | How |
|---------|-----------|-----|
| **Neo4j** | `group_id` on every query | All Cypher traversals start from `(:Group {group_id})` which is linked to a single `(:Owner)`. No cross-tenant path exists. |
| **Milvus** | `group_id` partition key | Every vector has `group_id` field. All searches include `expr="group_id == '...'"`. Milvus partition key ensures physical separation. |
| **Topic extraction** | `group_id` in payload | Topics extracted per message, stored in Neo4j and Milvus with `group_id`. |
| **SQS** | Payload carries group_id | Webhook payload includes group JID. Processor routes by it. |
| **Owner chat** | owner_phone → group lookup | `MATCH (o:Owner {phone: $phone})-[:OWNS]->(g:Group) RETURN g.group_id` — owner can only query their own groups. |

## Compliance

- LGPD compliant — process-and-discard, no raw message storage
- All phone-linked data treated as PII
- Credentials in Secrets Manager (not env vars)
- Encryption at rest and in transit
- Tenant isolation by group_id / owner_phone
- Data retention configurable per owner

## Future

- **Cognito** — web dashboard for tenant management and analytics viewing
- **HTTPS on ALB** — ACM certificate + Route53 domain
- **WAF** — rate limiting and DDoS protection on API Gateway
