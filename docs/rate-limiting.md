# Rate Limiting

Rate limiting is applied using [SlowAPI](https://github.com/laurentS/slowapi), a FastAPI-compatible wrapper around the `limits` library. The shared limiter instance lives in `app/limiter.py` and is imported by both `app/main.py` and `app/auth.py`.

---

## Limits in place

| Endpoint | Limit | Key |
|---|---|---|
| `POST /token` | 10 / minute | IP |
| `POST /register` | 5 / minute | IP |
| `POST /verify-email` | 20 / minute | IP |
| `POST /resend-verification` | 5 / minute | IP |
| `POST /forgot-password` | 5 / minute | IP |
| `POST /reset-password` | 10 / minute | IP |
| `POST /upload` | 10 / minute | IP |
| `POST /process` | 10 / minute | IP |
| `GET /health/ai` | 2 / minute | User **and** IP (independent counters) |

---

## Why IP for most endpoints

The majority of rate-limited endpoints are either unauthenticated (login, register, forgot-password) or accept anonymous input (upload, process). The client IP is the only reliable identity available at that point, so all of these are keyed on `get_remote_address`.

---

## Why User + IP for `GET /health/ai`

This endpoint makes a live outbound call to the Google Gemini API, consuming quota and incurring cost on every request. Two independent counters are enforced:

- **Per user** — keyed on the email address extracted from the JWT. Prevents a single recruiter account from hammering the endpoint regardless of which IP they come from.
- **Per IP** — keyed on the client's remote address. Prevents abuse from a single machine regardless of how many accounts are used.

A request is rejected if *either* counter is exhausted.

**Example scenarios:**

| Scenario | User counter | IP counter | Result |
|---|---|---|---|
| Recruiter A clicks twice | 2/2 | 2/2 | Both hit — blocked on 3rd click |
| Recruiter A and B on same IP, 2 clicks each | A: 2/2, B: 2/2 | 4/2 | IP counter blocks after 2nd total click from that machine |
| Recruiter A switches to a different IP mid-session | 2/2 | IP1: 1/2, IP2: 1/2 | User counter blocks regardless of IP switching |

If a request arrives without a valid JWT, the user key function falls back to the IP address so unauthenticated probes are still covered.

---

## What happens when the limit is hit

All endpoints return `429 Too Many Requests`, handled automatically by SlowAPI's `_rate_limit_exceeded_handler`.

For `GET /health/ai` specifically, the dashboard shows a plain message:

> *"You can only check the AI status twice per minute. Please wait a moment and try again."*

The status dot resets to grey (neutral) rather than red, since hitting the rate limit is not an application error.

---

## Storage

Counters are stored in **Redis**, configured via `RATE_LIMITER_STORAGE_URL` in `.env`. The same Redis instance used by Celery is reused — no extra infrastructure is needed. Using Redis means counters persist across web container restarts and are shared across all instances, so limits are enforced correctly regardless of which container handles a given request.

```
# .env
RATE_LIMITER_STORAGE_URL=redis://redis:6379/0
```

---

## Race condition safety

SlowAPI wraps the [`limits`](https://limits.readthedocs.io) library, which uses **atomic Lua scripts** for all Redis counter operations. The increment and expiry are executed as a single atomic unit inside Redis, so two simultaneous requests cannot both read a stale counter value and both slip through the limit.

One property worth understanding: SlowAPI uses a **fixed window** strategy by default. A user could theoretically make 2 requests at 11:59:59 and 2 more at 12:00:01 (start of the next window), getting 4 through in 2 seconds. This is not a race condition — it is an inherent characteristic of fixed windows. The alternative is a sliding window, which is more accurate but more expensive on Redis. Fixed window is the right trade-off for this application.
