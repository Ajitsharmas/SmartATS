# SmartATS

An AI-powered Applicant Tracking System built with FastAPI, Celery, and Google Gemini.

---

## Architecture

```mermaid
graph TB
    subgraph CLIENTS["Clients"]
        BR["Browser\n──────────\nRecruiter · Candidate"]
    end

    subgraph VM["GCP e2-micro VM · Docker Compose"]
        direction TB
        NGX["Nginx\n──────────\nPort 80 · 443\nReverse Proxy\nSSL Termination\nRate Limiting"]

        subgraph APP["Application"]
            API["FastAPI\n──────────\nPort 8000\nREST API\nAuth\nFile Handling"]
            WRK["Celery Worker\n──────────\nAI Scoring\nEmail Dispatch"]
        end

        subgraph DATA["Data"]
            RDS[("Redis\n──────────\nPort 6379\nTask Broker\nRate Limit Store")]
            PG[("PostgreSQL\n──────────\nPort 5432\nJobs · Users\nApplications")]
            MIO[("MinIO\n──────────\nPort 9000 · 9001\nResume PDF Storage")]
        end

        CTB["Certbot\n──────────\nSSL Renewal"]
    end

    subgraph EXT["External Services"]
        GEM["Google Gemini\n──────────\nAI Resume Scoring"]
        SND["Resend\n──────────\nTransactional Email"]
        LEX["Let's Encrypt\n──────────\nCertificate Authority"]
    end

    BR --- NGX
    NGX --- API
    API --- RDS
    API --- PG
    API --- MIO
    API --- SND
    RDS --- WRK
    WRK --- PG
    WRK --- GEM
    WRK --- SND
    NGX --- CTB
    CTB --- LEX
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
