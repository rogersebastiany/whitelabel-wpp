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

### LGPD: Process-and-Discard

No raw messages are stored anywhere. The pipeline:
1. Webhook receives message
2. Extracts metadata: sender phone/LID, timestamp, group_id, msg_id, reply context
3. LLM extracts topics/entities from text (text never persisted)
4. Stores only: interaction graph, topics, metrics, aggregated summaries
5. Raw text discarded after processing

### Data Flow

1. **Ingestion** — Lambda validates webhook signature (X-Hub-Signature-256), parses payload, queues interaction metadata to SQS
2. **Processing** — ECS Fargate consumes SQS, extracts topics (Cognee), embeds summaries (Milvus), indexes for hybrid search (LanceDB)
3. **Analytics** — EventBridge triggers scheduled jobs: daily summaries, weekly engagement reports, topic recurrence (6h), IQS recalculation
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
| IaC | CloudFormation |
| Infrastructure | AWS (Lambda, API Gateway, SQS, ECS Fargate, ALB, ECR, Secrets Manager, EventBridge, CloudWatch) |
| Package Manager | uv |

## Deploy

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

# Update stack
aws cloudformation update-stack \
  --stack-name whitelabel-wpp-dev \
  --template-body file://infra/template.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters ParameterKey=Stage,ParameterValue=dev ...

# Push docker image
aws ecr get-login-password --region sa-east-1 | docker login --username AWS --password-stdin 525320085764.dkr.ecr.sa-east-1.amazonaws.com
docker build -t whitelabel-wpp .
docker tag whitelabel-wpp:latest 525320085764.dkr.ecr.sa-east-1.amazonaws.com/whitelabel-wpp:latest
docker push 525320085764.dkr.ecr.sa-east-1.amazonaws.com/whitelabel-wpp:latest
```

## Setup (local dev)

```bash
uv sync
cp .env.example .env  # fill in credentials
```

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
