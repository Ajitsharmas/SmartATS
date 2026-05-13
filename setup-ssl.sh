#!/bin/bash
# -----------------------------------------------------------------------------
# Purpose: One-time script to obtain the Let's Encrypt SSL certificate and
#          switch Nginx from HTTP-only mode to full HTTPS mode.
#
# Usage:   ./setup-ssl.sh your@email.com
#
# Run this ONCE on the GCP VM after the stack is deployed and the domain
# DNS has fully propagated. Do not run it again unless you are re-issuing
# the certificate from scratch.
# -----------------------------------------------------------------------------

set -e

DOMAIN="smartats.xyz"
COMPOSE_FILE="docker-compose.prod.yaml"

# --- Validate argument ---
if [ -z "$1" ]; then
    echo "Error: email address required."
    echo "Usage: ./setup-ssl.sh your@email.com"
    exit 1
fi

EMAIL="$1"

echo ""
echo "=== SmartATS SSL Setup ==="
echo "Domain : $DOMAIN"
echo "Email  : $EMAIL"
echo ""

# --- Step 1: Start the stack in HTTP-only mode ---
echo "[1/4] Starting stack in HTTP mode..."
docker compose -f "$COMPOSE_FILE" up -d --build
echo "      Waiting 10 seconds for Nginx to be ready..."
sleep 10

# --- Step 2: Obtain the certificate via webroot challenge ---
echo "[2/4] Requesting Let's Encrypt certificate..."
docker compose -f "$COMPOSE_FILE" run --rm --entrypoint certbot certbot certonly \
    --webroot \
    --webroot-path=/var/www/certbot \
    --email "$EMAIL" \
    --agree-tos \
    --no-eff-email \
    -d "$DOMAIN" \
    -d "www.$DOMAIN"

# --- Step 3: Activate the HTTPS Nginx config ---
echo "[3/4] Activating HTTPS Nginx config..."
cp nginx/nginx-ssl.conf nginx/nginx.conf
docker compose -f "$COMPOSE_FILE" exec nginx nginx -s reload

# --- Step 4: Remind user to update APP_BASE_URL ---
echo "[4/4] Done!"
echo ""
echo "============================================================"
echo " HTTPS is now active at https://$DOMAIN"
echo "============================================================"
echo ""
echo "IMPORTANT: Update your .env file:"
echo "  APP_BASE_URL=https://$DOMAIN"
echo ""
echo "Then restart the stack to apply:"
echo "  docker compose -f $COMPOSE_FILE up -d"
echo ""
