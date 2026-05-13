# Scaling Celery Workers

## When to scale

Resumes are scored by Celery workers which call the Gemini API. Each call is **I/O-bound** — the worker sends a request and waits for Gemini to respond. This means a single worker spends most of its time waiting, not using CPU. Scaling workers is the right lever when:

- The Redis queue is building up (many tasks sitting unprocessed)
- Resume scoring is taking too long under load
- Workers are idle-waiting on Gemini responses rather than being CPU-saturated

---

## How to check if the queue is backing up

SSH into the GCP VM and run:

```bash
docker compose -f docker-compose.prod.yaml exec redis redis-cli llen celery
```

This returns the number of tasks currently waiting in the queue. If this number is growing and not draining, you need more workers.

---

## Option 1 — Scale worker containers (recommended first step)

Docker Compose can run multiple copies of the worker service. All instances connect to the same Redis broker and pull from the same task queue automatically — no code changes needed.

```bash
# Run 3 worker containers instead of 1
docker compose -f docker-compose.prod.yaml up -d --scale worker=3
```

To scale back down:
```bash
docker compose -f docker-compose.prod.yaml up -d --scale worker=1
```

To check how many workers are running:
```bash
docker compose -f docker-compose.prod.yaml ps
```

---

## Option 2 — Increase concurrency within a single worker

Each worker container runs multiple concurrent task slots (processes). By default Celery sets concurrency equal to the number of CPU cores. On the e2-micro (2 vCPUs) this means 2 slots per worker.

Since Gemini calls are I/O-bound, you can safely increase concurrency well beyond the CPU count. To do this, edit the worker `command` in `docker-compose.prod.yaml`:

```yaml
command: celery -A app.worker.celery_app worker --loglevel=info --concurrency=8
```

This lets a single worker process up to 8 tasks simultaneously. Each slot blocks on the Gemini network call, so having more slots means more resumes being scored in parallel without needing extra containers.

---

## Option 3 — Combine both

Run multiple worker containers, each with higher concurrency:

```bash
docker compose -f docker-compose.prod.yaml up -d --scale worker=2
```

With `--concurrency=8` set in the command, this gives you 2 containers × 8 slots = 16 concurrent Gemini calls.

---

## Memory limits on the e2-micro

The GCP free-tier VM has 1 GB RAM + 2 GB swap. Each worker container uses approximately 150–200 MB of RAM. Practical limits:

| Workers | Approx. RAM used | Safe on e2-micro? |
|---|---|---|
| 1 (default) | ~200 MB | Yes |
| 2 | ~400 MB | Yes |
| 3 | ~600 MB | Yes — monitor closely |
| 4+ | ~800 MB+ | Risky — heavy swap usage will slow everything down |

If you need more than 3 workers reliably, upgrade to a larger VM (e2-small or e2-medium) rather than pushing the e2-micro into swap.

---

## Does `--scale` auto-scale based on load?

No. `--scale worker=3` sets a **fixed** number of containers. Docker Compose has no built-in auto-scaling — you manually decide how many workers you want and run the command. The number stays fixed until you change it again.

### Does `--scale` affect all containers?

No. You always specify the service name explicitly:

```bash
docker compose -f docker-compose.prod.yaml up -d --scale worker=3
```

This only affects the `worker` service. All other services (`web`, `db`, `redis`, `nginx`, `minio`, `certbot`) remain at 1 container each and are completely untouched.

---

## Auto-scaling based on load

If you want containers to scale up and down automatically in response to queue depth or CPU usage, Docker Compose cannot do that. The tools that support auto-scaling are:

| Tool | Complexity | Notes |
|---|---|---|
| **Docker Swarm** | Medium | Built into Docker, no extra infrastructure needed. Supports replica scaling but not event-driven queue-based scaling |
| **Kubernetes** | High | Industry standard for container orchestration. Overkill for a single-VM demo app |
| **KEDA** (Kubernetes Event-driven Autoscaling) | High | Can scale Celery workers specifically based on Redis queue depth — the most targeted solution for this use case, but requires Kubernetes |

For this app on a single GCP e2-micro, **manual scaling with `--scale` is the right approach**. Auto-scaling tools add significant infrastructure complexity and only make sense when:
- You are running across multiple VMs
- You cannot watch the queue manually
- You have unpredictable, spiky traffic that varies greatly throughout the day

---

## When to upgrade the VM instead of scaling workers

Scaling workers on the same VM helps with I/O-bound bottlenecks (waiting on Gemini). It does **not** help if:

- The VM is running out of RAM (containers OOM-killed)
- The VM CPU is pegged at 100% consistently
- All services (web, worker, Postgres, Redis, MinIO) are competing for the same limited resources

In those cases, upgrade the VM to e2-small (2 vCPU, 2 GB RAM) or e2-medium (2 vCPU, 4 GB RAM). These are no longer free tier but cost only $12–$24/month.

---

## Monitoring worker activity

To watch what workers are doing in real time:

```bash
# Stream worker logs live
docker compose -f docker-compose.prod.yaml logs -f worker

# Check how many tasks have been processed
docker compose -f docker-compose.prod.yaml exec redis redis-cli info stats | grep processed
```

---

## How task distribution works

All worker containers (regardless of how many you run) connect to the same Redis broker and compete for tasks from the same queue. Redis acts as a coordinator — when a task arrives, exactly one worker picks it up. There is no risk of two workers processing the same resume twice.

```
Resume uploaded
      │
      ▼
 Redis Queue  ←─── All workers pull from here
      │
   ┌──┴──┐
Worker 1  Worker 2  Worker 3  ...
   │        │          │
Gemini   Gemini     Gemini
   │        │          │
 DB Write DB Write  DB Write
```
