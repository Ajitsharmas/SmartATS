# HTTPS / Let's Encrypt

## How it works

HTTPS is handled by two components working together:

- **Certbot** — requests and renews free SSL certificates from Let's Encrypt
- **Nginx** — serves the ACME HTTP-01 challenge files that Let's Encrypt uses to verify domain ownership, then terminates SSL for all application traffic

Let's Encrypt verifies that you own `smartats.xyz` by asking Certbot to place a file at:
```
http://smartats.xyz/.well-known/acme-challenge/<token>
```
Nginx serves this file from the shared `certbot_webroot` Docker volume. Once verified, the certificate is issued and stored in the `certbot_certs` volume, which Nginx also mounts.

---

## Nginx config files

Two nginx config files are provided:

| File | Purpose |
|---|---|
| `nginx/nginx.conf` | HTTP-only mode — used for initial deployment and cert issuance |
| `nginx/nginx-ssl.conf` | HTTPS mode — activated after cert is issued by `setup-ssl.sh` |

`setup-ssl.sh` copies `nginx-ssl.conf` over `nginx.conf` and reloads Nginx automatically.

---

## Prerequisites before running setup-ssl.sh

### GCP firewall — port 80 must be open to the public internet

Let's Encrypt verifies domain ownership by sending an HTTP request **from their own servers** to your VM on port 80. This is not a browser request — it comes from Let's Encrypt's infrastructure. If port 80 is blocked by the GCP firewall, the challenge times out and the certificate cannot be issued.

Port 80 **must** allow inbound traffic from `0.0.0.0/0` (everyone). This is safe because:
- Nginx only serves the `/.well-known/acme-challenge/` path over HTTP
- All other HTTP traffic is immediately redirected to HTTPS

**Create the HTTP firewall rule:**

GCP Console → **VPC Network → Firewall → Create Firewall Rule**

Fill in every field exactly as follows:

| Field | Value |
|---|---|
| Name | `allow-http` |
| Description | Allow HTTP traffic for Let's Encrypt ACME challenge and HTTP→HTTPS redirect |
| Logs | Off |
| Network | `default` |
| Priority | `1000` |
| Direction of traffic | Ingress |
| Action on match | Allow |
| Targets | All instances in the network |
| Source filter | IPv4 ranges |
| Source IPv4 ranges | `0.0.0.0/0` |
| Protocols and ports | Specified protocols and ports → TCP → `80` |

Click **Create**.

> **Note:** If you ticked "Allow HTTP traffic" when creating the VM, this rule (`default-allow-http`) already exists and no action is needed. Check GCP Console → VPC Network → Firewall to confirm.

Similarly, ensure port 443 is also open for HTTPS traffic after the certificate is issued:

| Field | Value |
|---|---|
| Name | `allow-https` |
| Source IPv4 ranges | `0.0.0.0/0` |
| Protocols and ports | TCP → `443` |

---

## Initial certificate issuance

Run this **once** on the GCP VM after DNS has fully propagated and the port 80 firewall rule is in place:

Run these commands from the **root of your project folder on the GCP VM** (the same folder that contains `docker-compose.prod.yaml`):

```bash
# Make sure you are in the project root on the VM
cd ~/SmartATS

chmod +x setup-ssl.sh
./setup-ssl.sh your@email.com
```

The script does four things in order:
1. Starts the full stack in HTTP-only mode
2. Runs Certbot in webroot mode to obtain the certificate from Let's Encrypt
3. Copies `nginx-ssl.conf` → `nginx.conf` and reloads Nginx
4. Prints a reminder to update `APP_BASE_URL`

After the script completes, update `.env`:
```
APP_BASE_URL=https://smartats.xyz
```

Then restart the stack to apply the new URL:
```bash
docker compose -f docker-compose.prod.yaml up -d
```

---

## Certificate renewal

Certificates from Let's Encrypt expire after **90 days**. The `certbot` service in `docker-compose.prod.yaml` runs a renewal loop that checks every 12 hours and renews automatically when the certificate is within 30 days of expiry — no manual action needed.

After renewal, Nginx must be reloaded to pick up the new certificate files:
```bash
docker compose -f docker-compose.prod.yaml exec nginx nginx -s reload
```

To automate this, add a cron job on the VM:
```bash
crontab -e
# Add this line — reloads Nginx every day at 3am
0 3 * * * docker compose -f /home/YOUR_USER/YOUR_REPO/docker-compose.prod.yaml exec nginx nginx -s reload
```

---

## HTTPS nginx config details

`nginx/nginx-ssl.conf` has two server blocks:

**Port 80 (HTTP):**
- Serves `/.well-known/acme-challenge/` for ongoing Certbot renewals
- Redirects everything else to HTTPS with a permanent `301`

**Port 443 (HTTPS):**
- SSL certificate loaded from `/etc/letsencrypt/live/smartats.xyz/`
- TLS 1.2 and 1.3 only — older versions disabled
- Strong cipher suite — null encryption and MD5 disabled
- SSL session cache to reduce handshake overhead on repeat connections
- All the same rate limiting zones and location blocks as the HTTP config
- Adds `X-Forwarded-Proto: https` header so FastAPI knows requests arrived over HTTPS
