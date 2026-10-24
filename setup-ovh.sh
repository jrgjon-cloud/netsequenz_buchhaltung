#!/bin/bash
set -e

if [ -f .setup_completed.lock ]; then
    echo -e "\e[31mFEHLER: Das Setup wurde bereits ausgeführt (siehe .setup_completed.lock). Aus Sicherheitsgründen ist dieses Skript gesperrt.\e[0m"
    exit 1
fi

echo "Willkommen zum interaktiven OVHcloud Setup für das Tax Dashboard!"

# Check requirements
for cmd in docker openssl; do
    if ! command -v $cmd &> /dev/null; then
        echo -e "\e[31mFEHLER: $cmd ist nicht installiert.\e[0m"
        exit 1
    fi
done

# Check docker compose
if ! docker compose version &> /dev/null; then
    echo -e "\e[31mFEHLER: docker compose (v2) ist nicht installiert.\e[0m"
    exit 1
fi

echo -e "\nGeneriere sichere Zufallspasswörter..."
GEN_FLASK_SECRET_KEY=$(openssl rand -hex 24)
GEN_POSTGRES_PASSWORD=$(openssl rand -hex 24)
GEN_MYSQL_ROOT_PASSWORD=$(openssl rand -hex 24)
GEN_MYSQL_PASSWORD=$(openssl rand -hex 24)
GEN_PAPERLESS_SECRET_KEY=$(openssl rand -hex 24)
GEN_INVOICE_NINJA_APP_KEY="base64:$(openssl rand -base64 32)"

echo "Es wurden frische Keys generiert. Drücke Enter, um diese zu übernehmen, oder gib eigene Werte ein."

read -p "FLASK_SECRET_KEY [$GEN_FLASK_SECRET_KEY]: " FLASK_SECRET_KEY
FLASK_SECRET_KEY=${FLASK_SECRET_KEY:-$GEN_FLASK_SECRET_KEY}

read -p "POSTGRES_PASSWORD [$GEN_POSTGRES_PASSWORD]: " POSTGRES_PASSWORD
POSTGRES_PASSWORD=${POSTGRES_PASSWORD:-$GEN_POSTGRES_PASSWORD}

read -p "MYSQL_ROOT_PASSWORD [$GEN_MYSQL_ROOT_PASSWORD]: " MYSQL_ROOT_PASSWORD
MYSQL_ROOT_PASSWORD=${MYSQL_ROOT_PASSWORD:-$GEN_MYSQL_ROOT_PASSWORD}

read -p "MYSQL_PASSWORD [$GEN_MYSQL_PASSWORD]: " MYSQL_PASSWORD
MYSQL_PASSWORD=${MYSQL_PASSWORD:-$GEN_MYSQL_PASSWORD}

read -p "PAPERLESS_SECRET_KEY [$GEN_PAPERLESS_SECRET_KEY]: " PAPERLESS_SECRET_KEY
PAPERLESS_SECRET_KEY=${PAPERLESS_SECRET_KEY:-$GEN_PAPERLESS_SECRET_KEY}

read -p "INVOICE_NINJA_APP_KEY [$GEN_INVOICE_NINJA_APP_KEY]: " INVOICE_NINJA_APP_KEY
INVOICE_NINJA_APP_KEY=${INVOICE_NINJA_APP_KEY:-$GEN_INVOICE_NINJA_APP_KEY}

read -p "TS_AUTHKEY (leer für interaktiven Login): " TS_AUTHKEY

read -p "Tailscale-Domain (TS_CERT_DOMAIN, z. B. tax-dashboard-server.tailxxxx.ts.net): " TS_CERT_DOMAIN
if [ -z "$TS_CERT_DOMAIN" ]; then
    echo -e "\e[31mFEHLER: TS_CERT_DOMAIN darf nicht leer sein.\e[0m"
    exit 1
fi

read -p "Invoice Ninja Domain (INVOICE_NINJA_DOMAIN): " INVOICE_NINJA_DOMAIN
if [ -z "$INVOICE_NINJA_DOMAIN" ]; then
    echo -e "\e[31mFEHLER: INVOICE_NINJA_DOMAIN darf nicht leer sein.\e[0m"
    exit 1
fi

cat <<ENV_EOF > .env
FLASK_SECRET_KEY=$FLASK_SECRET_KEY
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
MYSQL_ROOT_PASSWORD=$MYSQL_ROOT_PASSWORD
MYSQL_PASSWORD=$MYSQL_PASSWORD
PAPERLESS_SECRET_KEY=$PAPERLESS_SECRET_KEY
INVOICE_NINJA_APP_KEY=$INVOICE_NINJA_APP_KEY
TS_AUTHKEY=$TS_AUTHKEY
TS_CERT_DOMAIN=$TS_CERT_DOMAIN
INVOICE_NINJA_DOMAIN=$INVOICE_NINJA_DOMAIN
ENV_EOF

chmod 600 .env
echo "Die Datei .env wurde erfolgreich erstellt (chmod 600)."

sed -i "s/tax-dashboard-server:/${TS_CERT_DOMAIN}:/g" docker/tailscale-serve.json
echo "tailscale-serve.json wurde angepasst."

echo "Starte Tailscale-Container..."
docker compose up -d tailscale

if [ -z "$TS_AUTHKEY" ]; then
    echo "Führe Tailscale interaktiven Login aus..."
    docker compose exec tailscale tailscale up || true
fi

echo "Fordere HTTPS-Zertifikat im Container an..."
docker compose exec tailscale tailscale cert "${TS_CERT_DOMAIN}" || true

echo "Starte den gesamten Stack..."
docker compose up -d

echo "Führe Invoice Ninja Migration aus..."
docker compose exec invoiceninja-app php artisan migrate --force || true

touch .setup_completed.lock
date > .setup_completed.lock
chmod 400 .setup_completed.lock
chmod -x "$0"

echo -e "\e[32mSetup erfolgreich abgeschlossen!\e[0m"
