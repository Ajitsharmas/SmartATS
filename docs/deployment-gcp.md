# Deployment — GCP Compute Engine

## Cost

Everything used is **free**:

| Resource | Cost |
|---|---|
| GCP e2-micro VM | Free (always-free tier) |
| GCP 30 GB standard disk | Free (included) |
| GCP static external IP | Free (while attached to a **running** VM) |
| Let's Encrypt SSL certificate | Free (auto-renews every 90 days) |
| Domain (`smartats.xyz`) | Already owned |

The only billable item is outbound network traffic above 1 GB/month — well within free-tier limits at small scale.

> **Region requirement:** The e2-micro free tier only applies in `us-central1`, `us-west1`, or `us-east1`. Any other region incurs charges.

### Billing account requirement

GCP requires a billing account (credit/debit card) to be set up even for free-tier resources. This is identity verification — you will not be charged as long as you stay within free tier limits. GCP may place a small temporary hold (e.g. $1) to verify the card, which is refunded immediately.

**Recommended:** Set a billing alert so you are notified if anything ever goes over free tier:

GCP Console → **Billing → Budgets & Alerts → Create Budget** → set amount to $1 → enable email alerts at 50%, 90%, 100%.

---

## Static external IP — billing details

| Situation | Cost |
|---|---|
| Static IP attached to a **running** VM | **Free** |
| Static IP attached to a **stopped** VM | Charged (~$0.01/hour) |
| Static IP with **no VM attached** | Charged (~$0.01/hour) |

GCP charges for reserved IPs that are not actively in use to prevent IP address hoarding. As long as your VM is running 24/7 (which it will be since all services use `restart: always`) the IP costs nothing.

**Important:** if you ever stop the VM from the GCP Console, release the static IP first or be aware that charges will accumulate until you start the VM again.

### Reserving the static IP — field reference

GCP Console → **VPC Network → IP Addresses → Reserve External Static Address**

| Field | Value |
|---|---|
| Name | `smartats-ip` (or any name) |
| Network Service Tier | Standard (sufficient for small-scale deployments) |
| IP version | IPv4 |
| Type | Regional |
| Region | Same region as your VM (e.g. `us-central1`) |
| Attached to | Select your VM instance |

---

## Step-by-step deployment

### Step 1 — Create the VM

1. GCP Console → **Compute Engine → VM Instances → Create Instance**
2. Machine type: `e2-micro`
3. Region: `us-central1` (or `us-west1` / `us-east1`)
4. Boot disk: click **Change** next to the boot disk summary and set:

| Field | Value |
|---|---|
| Operating system | Ubuntu |
| Version | Ubuntu 22.04 LTS |
| Boot disk type | **Standard persistent disk** ← must be standard, not balanced |
| Size | **30** GB |

5. Firewall: tick **Allow HTTP traffic** (port 80)
6. Create

> **Note on the cost estimate:** The GCP calculator always shows full pay-as-you-go pricing (~$7.31/month) and never reflects free tier discounts. With the correct configuration above (e2-micro + 30 GB standard disk in a free-tier region), the actual charge is **$0**. The $6.11 compute and $1.20 disk line items are both covered by the always-free tier credit. Set a billing alert to $1 as a safety net.

### Step 2 — Reserve a static external IP

Follow the field reference above. Note the IP address — you need it for DNS.

### Step 3 — Point `smartats.xyz` DNS to the VM

Log into your domain registrar and add two A records:

| Type | Name | Value | TTL |
|---|---|---|---|
| A | `@` | `YOUR_GCP_STATIC_IP` | 300 |
| A | `www` | `YOUR_GCP_STATIC_IP` | 300 |

DNS propagation takes up to 48 hours. Verify with:
```bash
nslookup smartats.xyz
```

### Step 4 — Install gcloud CLI (on your local Mac)

Skip this step if you already have `gcloud` installed (`gcloud --version` to check).

```bash
# Download and install the Google Cloud SDK
curl https://sdk.cloud.google.com | bash

# Restart your shell to pick up the new PATH
exec -l $SHELL

# Initialise and log in
gcloud init
```

`gcloud init` will open a browser window asking you to log in with your Google account and select your GCP project. Follow the prompts.

Verify the install:
```bash
gcloud --version
```

### Step 5 — SSH into the VM

```bash
gcloud compute ssh YOUR_INSTANCE_NAME --zone=us-central1-a
```

Replace `YOUR_INSTANCE_NAME` with the name you gave the VM in Step 1 (visible in GCP Console → Compute Engine → VM Instances).

### Step 6 — Add swap space

The e2-micro has only 1 GB RAM. The full stack (FastAPI + Celery + Redis + Postgres + MinIO + Nginx) pushes against that limit. Add 2 GB of swap to prevent OOM kills:

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### Step 7 — Install Docker

`docker-compose-plugin` is not available in Ubuntu's default apt sources — Docker's official repository must be added first:

```bash
# Add Docker's official GPG key and repository
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Update and install
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Allow running docker without sudo
sudo usermod -aG docker $USER
newgrp docker
```

Verify:
```bash
docker --version
docker compose version
```

### Step 8 — Get the code

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
cd YOUR_REPO
```

For a private repo, use a personal access token in the URL or set up an SSH deploy key.

### Step 9 — Copy `.env` to the VM

**Never commit `.env` to git.**

> **Critical:** The `gcloud compute scp` command must be run from your **local Mac terminal** — NOT from inside the SSH session on the VM. If you run it from inside the VM, gcloud will fail with "insufficient authentication scopes" because the VM's service account does not have permission to copy files to itself.

**Option A — Copy from your local Mac (recommended)**

Open a new terminal on your Mac (close or set aside the SSH session), navigate to your project folder, and run:

```bash
# Run this on your LOCAL Mac — not inside the SSH session
gcloud compute scp .env YOUR_INSTANCE_NAME:~/YOUR_REPO/.env --zone=us-central1-a
```

**Option B — Create it directly on the VM**

If you are already inside the SSH session and don't want to open a new terminal, create the file manually:

```bash
# Run this inside the SSH session on the VM
nano ~/YOUR_REPO/.env
```

Paste your `.env` contents, then save with `Ctrl+X → Y → Enter`.

### Step 10 — Run the DB migration (existing deployments only)

> **Fresh GCP deployment? Skip this step entirely.**
> On a brand new database, SQLModel's `create_all()` runs at startup and creates
> the `User` table with all columns already present. There is nothing to migrate.

These `ALTER TABLE` commands are only needed if you are upgrading an **existing** deployment that was running before the `is_verified`, `verification_token`, and `reset_token` columns were added to the `User` model — i.e. the Postgres volume already exists with the old schema.

```bash
docker compose -f docker-compose.prod.yaml up -d --build

docker compose -f docker-compose.prod.yaml exec db psql -U resume_user -d resume_db -c "
ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS is_verified BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS verification_token TEXT;
ALTER TABLE \"user\" ADD COLUMN IF NOT EXISTS reset_token TEXT;
"
```

### Step 11 — Set up HTTPS

See [https-ssl.md](https-ssl.md) for the full process.

---

## GCP Firewall — IP Restrictions

### Find your current public IP

Run this before configuring any firewall rule:

```bash
curl ifconfig.me
```

> **Note:** Home and office IPs are usually dynamic — your ISP can reassign them. If your app or DBeaver suddenly becomes unreachable, your IP has likely changed. Run `curl ifconfig.me` again and update the relevant firewall rule in GCP Console.

---

### Option A — Restrict only the database and MinIO ports (default recommendation)

This keeps the application itself publicly accessible at `smartats.xyz` while preventing anyone from connecting directly to Postgres or MinIO from outside.

Create **two** firewall rules — one for Postgres and one for MinIO:

**Rule 1 — Postgres (DBeaver access)**

GCP Console → **VPC Network → Firewall → Create Firewall Rule**

| Field | Value |
|---|---|
| Name | `allow-postgres-myip` |
| Direction | Ingress |
| Action | Allow |
| Targets | All instances in the network |
| Source IP ranges | `YOUR_IP/32` |
| Protocols and ports | TCP, port `5432` |

**Rule 2 — MinIO console and API**

GCP Console → **VPC Network → Firewall → Create Firewall Rule**

| Field | Value |
|---|---|
| Name | `allow-minio-myip` |
| Direction | Ingress |
| Action | Allow |
| Targets | All instances in the network |
| Source IP ranges | `YOUR_IP/32` |
| Protocols and ports | TCP, ports `9000,9001` |

The `/32` means exactly that single IP address — no other machine can reach these ports.

**DBeaver connection settings:**

| Field | Value |
|---|---|
| Host | `YOUR_GCP_STATIC_IP` |
| Port | `5432` |
| Database | `resume_db` |
| Username | `resume_user` |
| Password | *(whatever you set in `.env`)* |

**MinIO console:**

Open `http://YOUR_GCP_STATIC_IP:9001` in your browser. Login with the `MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY` values from your `.env`.

---

### Option B — Restrict the entire app to your IP only

Use this if you want the app completely private — no one except you can reach it at all.

> **Important:** Let's Encrypt certificate issuance requires port 80 to be open to `0.0.0.0/0`. Complete the SSL setup (`setup-ssl.sh`) first before restricting access. Once the certificate is issued, lock it down using the steps below.

#### Step 1 — Find your current public IP

```bash
curl ifconfig.me
```

Note this IP — you will use it in every firewall rule below.

#### Step 2 — Delete or disable the existing open HTTP/HTTPS rules

GCP Console → **VPC Network → Firewall**

Look for these rules and **delete** them (or disable them):
- `default-allow-http` — allows port 80 from `0.0.0.0/0`
- `default-allow-https` (or `allow-https`) — allows port 443 from `0.0.0.0/0`

#### Step 3 — Create new restricted rules for HTTP and HTTPS

**Rule 1 — HTTP (your IP only)**

GCP Console → **VPC Network → Firewall → Create Firewall Rule**

| Field | Value |
|---|---|
| Name | `allow-http-myip` |
| Description | Allow HTTP access from my IP only |
| Logs | Off |
| Network | `default` |
| Priority | `1000` |
| Direction of traffic | Ingress |
| Action on match | Allow |
| Targets | All instances in the network |
| Source filter | IPv4 ranges |
| Source IPv4 ranges | `YOUR_IP/32` |
| Protocols and ports | TCP → `80` |

**Rule 2 — HTTPS (your IP only)**

| Field | Value |
|---|---|
| Name | `allow-https-myip` |
| Description | Allow HTTPS access from my IP only |
| Logs | Off |
| Network | `default` |
| Priority | `1000` |
| Direction of traffic | Ingress |
| Action on match | Allow |
| Targets | All instances in the network |
| Source filter | IPv4 ranges |
| Source IPv4 ranges | `YOUR_IP/32` |
| Protocols and ports | TCP → `443` |

**Rule 3 — Postgres (your IP only)**

| Field | Value |
|---|---|
| Name | `allow-postgres-myip` |
| Source IPv4 ranges | `YOUR_IP/32` |
| Protocols and ports | TCP → `5432` |

**Rule 4 — MinIO (your IP only)**

| Field | Value |
|---|---|
| Name | `allow-minio-myip` |
| Source IPv4 ranges | `YOUR_IP/32` |
| Protocols and ports | TCP → `9000,9001` |

#### Step 4 — Verify it's working

Open `https://smartats.xyz` in your browser — it should load. Ask someone else to try — they should get a connection timeout (not a 403, just no response at all).

#### When your IP changes

Home and office IPs are dynamic and can change. If `https://smartats.xyz` suddenly stops loading for you:

```bash
curl ifconfig.me
```

Then update **all four rules** (`allow-http-myip`, `allow-https-myip`, `allow-postgres-myip`, `allow-minio-myip`) in GCP Console with the new IP.

GCP Console → **VPC Network → Firewall** → click the rule → **Edit** → update Source IPv4 ranges → **Save**.

#### Temporarily opening access to everyone

If you want to share the app publicly for a period:

1. Edit `allow-http-myip` → change Source IPv4 ranges to `0.0.0.0/0` → Save
2. Edit `allow-https-myip` → change Source IPv4 ranges to `0.0.0.0/0` → Save

To lock it down again, change both back to `YOUR_IP/32`.

---

### Comparison

| Approach | App accessible to public | DB + MinIO accessible to public | Best for |
|---|---|---|---|
| Option A (DB + MinIO only) | Yes | No — your IP only | Default — public app with private data access |
| Option B (full restrict) | No — your IP only | No — your IP only | Private development / testing |
