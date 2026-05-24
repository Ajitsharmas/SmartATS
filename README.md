# SmartATS

An AI-powered Applicant Tracking System built with FastAPI, Celery, and Google Gemini.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     Browser  (Recruiter / Candidate)                    │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                           GCP  e2-micro  VM                             │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │                         Docker  Compose                           │  │
│  │                                                                   │  │
│  │  ┌─────────────────────────────────────────────────────────────┐  │  │
│  │  │                           Nginx                             │  │  │
│  │  │                       Port  80 / 443                        │  │  │
│  │  │         Reverse Proxy · SSL Termination · Rate Limiting     │  │  │
│  │  └─────────────────────────────────────────────────────────────┘  │  │
│  │                                                                   │  │
│  │  ┌──────────────────────────────┐  ┌──────────────────────────┐  │  │
│  │  │           FastAPI            │  │      Celery  Worker      │  │  │
│  │  │          Port  8000          │  │                          │  │  │
│  │  │   REST API · Auth            │  │   AI  Scoring            │  │  │
│  │  │   File  Handling             │  │   Email  Dispatch        │  │  │
│  │  └──────────────────────────────┘  └──────────────────────────┘  │  │
│  │                                                                   │  │
│  │  ┌─────────────────────────────────────────────────────────────┐  │  │
│  │  │                           Redis                             │  │  │
│  │  │                          Port  6379                         │  │  │
│  │  │           Task  Message  Broker · Rate  Limit  Store        │  │  │
│  │  └─────────────────────────────────────────────────────────────┘  │  │
│  │                                                                   │  │
│  │  ┌──────────────────────────────┐  ┌──────────────────────────┐  │  │
│  │  │         PostgreSQL           │  │          MinIO           │  │  │
│  │  │          Port  5432          │  │    Port  9000 / 9001     │  │  │
│  │  │   Jobs · Users               │  │   Resume  PDF  Storage   │  │  │
│  │  │   Applications               │  │                          │  │  │
│  │  └──────────────────────────────┘  └──────────────────────────┘  │  │
│  │                                                                   │  │
│  │  ┌─────────────────────────────────────────────────────────────┐  │  │
│  │  │                          Certbot                            │  │  │
│  │  │                        SSL  Renewal                         │  │  │
│  │  └─────────────────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────┐  ┌─────────────────────┐  ┌─────────────────────┐
│    Google  Gemini   │  │       Resend         │  │   Let's  Encrypt    │
│    AI  Scoring  API │  │  Transactional Email │  │  Certificate  CA    │
└─────────────────────┘  └─────────────────────┘  └─────────────────────┘
```

---

## Documentation

| Topic | Description |
|---|---|
| [Rate Limiting](docs/rate-limiting.md) | SlowAPI + Redis rate limits on all endpoints, dual user+IP limiting for the AI health probe, race condition safety, fixed window behaviour |
| [DDoS Resistance](docs/ddos-resistance.md) | Nginx connection limits, request rate zones, how Nginx matches zones to URL paths, known gaps |
| [Deployment — GCP](docs/deployment-gcp.md) | Step-by-step GCP Compute Engine free-tier deployment, static IP billing rules, swap space, DB migration, DBeaver firewall |
| [HTTPS / Let's Encrypt](docs/https-ssl.md) | Certbot + Nginx SSL setup, `setup-ssl.sh` usage, certificate auto-renewal, nginx config details |
| [Scaling Workers](docs/scaling-workers.md) | How to scale Celery workers under resume processing load, concurrency tuning, memory limits on e2-micro, when to upgrade the VM |
| [AI Features Roadmap](docs/ai-features/roadmap.md) | Planned RAG / semantic search / agent features, shared pgvector infrastructure, implementation phases, free-tier compatibility |
| [AI Phase 0 — Foundation](docs/ai-features/phase-0-foundation.md) | pgvector setup, embedding tables, ORM choice (SQLModel + drop-down to SQLAlchemy), Gemini embeddings client, smoke test |
| [AI Phase 1 — Embedding Pipeline + Task Recovery](docs/ai-features/phase-1-embedding-pipeline.md) | Resume chunking, embedding Celery task, parallel dispatch, manual retry mechanism for failed tasks (retrofitted to all existing tasks) |
| [AI Phase 2 — Semantic Search](docs/ai-features/phase-2-semantic-search.md) | Semantic search across the recruiter's applicant pool, owner-scoped multi-tenancy, pagination, Redis query embedding cache |
| [AI Phase 3 — Cross-Job Matching](docs/ai-features/phase-3-cross-job-matching.md) | Job description embedding, cross-job match SQL, per-candidate and bulk recheck endpoints, dashboard match badges |
