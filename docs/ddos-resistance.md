# DDoS Resistance

## Current posture

Application-level rate limiting (SlowAPI) alone does not stop a DDoS. Under a flood of thousands of requests per second, HTTP requests still reach FastAPI, traverse the middleware stack, and query Redis before being rejected. The container CPU and Redis connection pool can exhaust before the rate limiter has meaningful effect. A botnet with thousands of IPs bypasses per-IP limits entirely since each IP gets its own fresh counter.

---

## Nginx-level mitigations (implemented)

The first line of defence is `nginx/nginx.conf`, which blocks abuse at the connection and request level before traffic reaches FastAPI.

### Two rate limiting zones

| Zone | Rate | Applied to |
|---|---|---|
| `general` | 20 requests / second per IP | All traffic (burst of 50 allowed) |
| `auth` | 5 requests / second per IP | `/token`, `/register`, `/forgot-password`, `/reset-password`, `/resend-verification`, `/verify-email` (burst of 5) |

### Connection limit

- Maximum **20 concurrent TCP connections** per IP across all locations
- Defends against Slowloris-style attacks that hold connections open indefinitely without sending data

### How Nginx decides which zone applies

Nginx matches the **URL path** of each incoming request against `location` blocks in the config. It has no awareness of HTTP method, request body, or headers at this level — the path alone determines which block wins.

```
POST /token      → matches location ~ ^/(token|register|...)  → auth zone (5r/s)
POST /upload     → no specific match → falls through to location /  → general zone (20r/s)
```

`conn_limit` is set at the `server` block level, so it applies to **every request** regardless of which location block is matched — no request can escape it.

When a location-level `limit_req` is defined (as in the auth block), it **overrides** the server-level `general` zone for that path. They do not stack — the most specific match wins. `conn_limit` is the only limit that always applies everywhere.

### How burst works

- `burst=50 nodelay` on the general zone means a spike of up to 50 requests above the rate is allowed instantly, but the 51st is rejected with `429` immediately rather than being queued
- Auth endpoints use `burst=5` — these have no legitimate need for rapid repeated calls from one IP

### Response code

Nginx returns `429 Too Many Requests` (not its default `503`) so clients receive the semantically correct status code.

---

## Layers summary

| Layer | Tool | What it stops |
|---|---|---|
| Network edge | — (Cloudflare recommended for production) | Volumetric floods, bot traffic |
| Reverse proxy | Nginx `limit_conn` + `limit_req` | Connection exhaustion, request floods, Slowloris |
| Application | SlowAPI + Redis | Per-endpoint abuse, brute-force, email spam |

---

## Known gaps

- **Botnet / distributed attacks** — per-IP limits are ineffective when an attacker controls thousands of IPs. Cloudflare or a WAF at the network edge is the correct solution.
- **Single-instance Nginx memory** — Nginx rate limiting state is local to the Nginx process. In a multi-Nginx setup, a shared zone backend (e.g. `nginx-plus` or `lua-resty-limit-traffic` with Redis) would be needed.

These gaps are acceptable for small-scale deployments where the Nginx + SlowAPI layers provide solid defence against accidental overload, casual abuse, and brute-force attempts. If the application grows to a scale that attracts distributed attacks, putting it behind Cloudflare (or another edge WAF) is the correct next step — that mitigation is purely additive and does not require any change to the existing layers.
