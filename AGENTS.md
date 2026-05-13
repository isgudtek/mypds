# AGENTS.md — mypds Setup Guide

This file is for AI agents. Follow it top-to-bottom to go from zero to a live, federated ATProto Personal Home Node in one session.

---

## What you're building

A self-hosted ATProto PDS with a full web UI: posts, blog pages, gallery, file hosting, and a compose UI. All data lives in the user's ATProto repo (MST), propagated on the Bluesky firehose.

**Stack:** Python 3.10+, aiohttp, SQLite (apsw), Jinja2, Cloudflare Tunnel (or nginx+certbot).

---

## Prerequisites

Confirm these before starting:

- [ ] A domain or subdomain the user controls (e.g. `pds.example.com`)
- [ ] DNS managed somewhere (Cloudflare recommended — free tunnel)
- [ ] Python 3.10+ on the server
- [ ] Either: a Cloudflare account (tunnel) or nginx + SSL cert

---

## Step 1 — Install mypds

```bash
# On the server
python3 -m venv /opt/mypds/.venv
source /opt/mypds/.venv/bin/activate
pip install --upgrade pip
pip install git+https://github.com/isgudtek/mypds
```

Verify:
```bash
mypds --help
```

---

## Step 2 — Expose to the internet FIRST

**This must happen before creating the DID.** The PLC genesis op needs to reference your public HTTPS endpoint.

### Option A — Cloudflare Tunnel (recommended, no open ports)

```bash
# Install cloudflared
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared

# Log in (opens browser — user must do this)
cloudflared tunnel login

# Create tunnel
cloudflared tunnel create mypds

# Route your domain to it
cloudflared tunnel route dns mypds pds.example.com

# Config file at ~/.cloudflared/config.yml:
cat > ~/.cloudflared/config.yml <<EOF
tunnel: <TUNNEL_ID_FROM_ABOVE>
credentials-file: /root/.cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: pds.example.com
    service: http://localhost:3030
  - service: http_status:404
EOF

# Start tunnel (in background or as systemd service)
cloudflared tunnel run mypds &
```

### Option B — nginx + certbot

```bash
apt install nginx certbot python3-certbot-nginx

cat > /etc/nginx/sites-enabled/mypds <<'EOF'
server {
    listen 80;
    server_name pds.example.com;
    location / {
        proxy_pass http://127.0.0.1:3030;
        proxy_set_header Host $host;
        proxy_http_version 1.1;
        proxy_set_header Connection "upgrade";
        proxy_set_header Upgrade $http_upgrade;
        proxy_read_timeout 1d;
        proxy_buffering off;
        proxy_request_buffering off;
    }
}
EOF

nginx -s reload
certbot --nginx -d pds.example.com
```

Verify your domain is live (should return connection refused or 502 since mypds isn't started yet — that's fine, HTTPS must work):
```bash
curl -sI https://pds.example.com | head -1
```

---

## Step 3 — Create the ATProto identity (DID)

The handle is what users see (`@alice.example.com`). The DID is permanent.

```bash
# Clone mypds to get the identity creation script
git clone https://github.com/isgudtek/mypds /tmp/mypds-setup
cd /tmp/mypds-setup

# IMPORTANT: replace these values
HANDLE="alice.example.com"        # the user's handle
PDS_URL="https://pds.example.com" # must be HTTPS, must be reachable

./test_data/create_identity.sh "$HANDLE" "$PDS_URL" https://plc.directory
```

Successful output includes:
```
Created identity for alice.example.com at https://plc.directory/did:plc:xxxxxxxxxxxx
rotation key → alice.example.com_rotation_key.pem   ← STORE SAFELY, NEVER ON SERVER
repo signing key → alice.example.com_repo_key.pem   ← needed on server
did:plc string → alice.example.com_did.txt
```

**Save the rotation key somewhere safe and offline.** If the server is compromised, the rotation key lets you recover the identity.

Copy the repo signing key to the server:
```bash
cp alice.example.com_repo_key.pem /opt/mypds/repo_key.pem
chmod 600 /opt/mypds/repo_key.pem
```

---

## Step 4 — DNS handle resolution

The handle must resolve to the DID. Two methods — pick one:

### Method A — DNS TXT record (recommended)
Add a TXT record at `_atproto.alice` (or `_atproto.alice.example.com` if handle is a subdomain) with value:
```
did=did:plc:xxxxxxxxxxxx
```

### Method B — well-known file
Serve `https://alice.example.com/.well-known/atproto-did` with the DID as the body.

Verify resolution:
```bash
dig TXT _atproto.alice.example.com +short
# should return: "did=did:plc:xxxxxxxxxxxx"
```

---

## Step 5 — Create the account

```bash
source /opt/mypds/.venv/bin/activate

DID=$(cat /tmp/mypds-setup/alice.example.com_did.txt)

mypds account create "$DID" alice.example.com \
  --signing_key=/opt/mypds/repo_key.pem \
  --pds-pfx=https://pds.example.com \
  --pds-did-plc=https://plc.directory
```

You'll be prompted for a password. This is the web UI login password.

---

## Step 6 — Start mypds

```bash
source /opt/mypds/.venv/bin/activate

mypds run \
  --pds-pfx=https://pds.example.com \
  --pds-did-plc=https://plc.directory \
  --port=3030 \
  &>> /var/log/mypds.log &

echo $! > /var/run/mypds.pid
```

### As a systemd service (recommended for production)

```ini
# /etc/systemd/system/mypds.service
[Unit]
Description=mypds ATProto Personal Home Node
After=network.target

[Service]
Type=simple
Restart=on-failure
RestartSec=5
WorkingDirectory=/opt/mypds
ExecStart=/opt/mypds/.venv/bin/mypds run --pds-pfx=https://pds.example.com --pds-did-plc=https://plc.directory --port=3030

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now mypds
systemctl status mypds
```

---

## Step 7 — Tell the relay you exist

The Bluesky relay (`bsky.network`) doesn't find you automatically. Request a crawl:

```bash
curl --json '{"hostname":"https://pds.example.com"}' \
  "https://bsky.network/xrpc/com.atproto.sync.requestCrawl"
# Expected: HTTP 200 {}
```

mypds does this automatically on startup with a retry loop (5 attempts, 0/8/16/24/32s backoff) to handle tunnel startup lag. You shouldn't need to run this manually, but do it once anyway to be sure.

---

## Step 8 — Trigger identity/account events

The relay and appview need to know your account exists:

1. Log into [bsky.app](https://bsky.app) with:
   - Handle: `alice.example.com`
   - Password: the one you set in Step 5
   - Custom PDS: `https://pds.example.com`

2. Go to Settings → Change Handle → set it to `alice.example.com` again (even though it's the same). This emits an `#identity` event to the firehose.

3. Set a display name and/or bio. This creates your profile record and prompts the appview to start indexing.

4. Post something. Check that it appears on `bsky.app` under your profile.

---

## Step 9 — Access the web UI

Open `https://pds.example.com` in a browser. You should see:

- Homepage with your handle, recent posts, sidebar
- `/login` — log in with your password
- `/dashboard` — compose, gallery, pages, files
- `/gallery` — `pub.gallery.image` photo grid
- `/pages` — `com.whtwnd.blog.entry` blog pages
- `/files` — blob store file uploads
- `/node-info` — public ATProto endpoints and stats

---

## Troubleshooting

### Port already in use
```bash
kill $(lsof -ti:3030) 2>/dev/null; sleep 1
# then restart mypds
```

### Relay not indexing posts
```bash
# Manual crawl request
curl --json '{"hostname":"https://pds.example.com"}' \
  "https://bsky.network/xrpc/com.atproto.sync.requestCrawl"

# Check firehose is emitting
websocat "wss://pds.example.com/xrpc/com.atproto.sync.subscribeRepos"
```

### Handle not resolving
```bash
# Test DNS method
dig TXT _atproto.alice.example.com +short

# Test well-known method
curl https://alice.example.com/.well-known/atproto-did
```

### Tunnel dies and relay loses connection
mypds auto-requests crawl on startup. Restart mypds after restarting the tunnel:
```bash
systemctl restart mypds
```

### `describeServer` returns wrong PDS URL
The `--pds-pfx` flag must exactly match your public HTTPS URL. No trailing slash.

### Posts appear locally but not on bsky.app
1. Check `requestCrawl` was called after the tunnel was live
2. In bsky.app Settings, re-save your handle to emit an `#identity` event
3. Wait ~60s for relay to crawl and appview to index

---

## Upgrading

```bash
source /opt/mypds/.venv/bin/activate
pip install --upgrade --force-reinstall --no-cache-dir git+https://github.com/isgudtek/mypds
systemctl restart mypds
```

---

## File layout after setup

```
/opt/mypds/
├── .venv/              # Python virtualenv
├── repo_key.pem        # ATProto repo signing key (keep secret)
├── millipds.db         # ATProto repo (MST, blobs, DIDs, auth)
└── web.sqlite3         # Web layer (sessions, pages, media metadata)

# Keep these offline / backed up securely:
alice.example.com_rotation_key.pem   # identity rotation key — never put on server
alice.example.com_did.txt            # your DID string
```
