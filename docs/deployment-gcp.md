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

The only billable item is outbound network traffic above 1 GB/month — irrelevant for a demo app.

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
| Network Service Tier | Standard (sufficient for a demo) |
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

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-plugin
sudo usermod -aG docker $USER
newgrp docker
```

### Step 8 — Get the code

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
cd YOUR_REPO
```

For a private repo, use a personal access token in the URL or set up an SSH deploy key.

### Step 9 — Copy `.env` to the VM

**Never commit `.env` to git.** Copy it from your local machine:

```bash
# Run this on your local Mac
gcloud compute scp .env YOUR_INSTANCE_NAME:~/YOUR_REPO/.env --zone=us-central1-a
```

Or create it manually on the server with `nano .env` and paste the contents.

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

## GCP firewall for DBeaver (optional)

To connect DBeaver to the production Postgres, add a GCP firewall rule for port 5432 — but restrict the source to **your own IP only**, never `0.0.0.0/0`:

GCP Console → **VPC Network → Firewall → Create Rule**
- Direction: Ingress, Action: Allow
- Source IP: your home/office IP only
- Protocol: TCP, Port: 5432
