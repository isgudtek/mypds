# AGENTS.md — mypds Setup Guide

This file is for AI agents. Follow it top-to-bottom to go from zero to a live, federated ATProto Personal Home Node in one session.

---

## Step 0 — Choose deployment modality

**Ask the user which modality they want before doing anything else.** The answer determines domain, tunnel, and DNS setup for all subsequent steps.

| # | Modality | Best for | What you need |
|---|----------|----------|---------------|
| **A** | **mycrab.space** (recommended) | Fastest setup, no domain required | mycrab admin key (see below) |
| **B** | **Cloudflare Tunnel** + own domain | User has a domain on Cloudflare | Cloudflare account + domain |
| **C** | **nginx + certbot** + own domain | VPS with open ports 80/443 | Domain pointing to server IP |
| **D** | **Other reverse proxy** | Advanced users | User provides their setup |

If the user says **"use mycrab"**, **"mycrab skill"**, or any variant — jump directly to [Modality A](#modality-a--mycrabs-pace-skill) below. Do not ask further questions about the domain.

---

## Modality A — mycrab.space skill

> Use this when the user says "use mycrab", "mycrab skill", or similar.

### A1 — Provision a new crab (subdomain)

```bash
# Admin key is at /root/mycrabs/taskai/.mycrab_admin_key on the mycrab server
ADMIN_KEY=$(cat /root/mycrabs/taskai/.mycrab_admin_key)

# Pick a subdomain slug — use the user's name, project name, or ask
SLUG="alice"   # e.g. alice.mycrab.space

curl -s -X POST https://taskai.mycrab.space/api/mycrab/generate \
  -H "X-Admin-Key: $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"subdomain\": \"$SLUG\", \"label\": \"mypds - $SLUG\"}"
```

Response:
```json
{"token": "...", "subdomain": "alice", "url": "https://alice.mycrab.space"}
```

Record `DOMAIN=alice.mycrab.space` and `PDS_URL=https://alice.mycrab.space`.

**Default handle = the mycrab domain** → `HANDLE="alice.mycrab.space"`

### A2 — Ask about vanity domain (optional)

After provisioning the crab, ask the user:

> "Your PDS will be live at `https://alice.mycrab.space`. Do you have a custom domain you'd like to use as your handle instead? (e.g. `alice.com` or `pds.alice.com`) — totally optional, the mycrab domain works fine on its own."

- If **yes**: set `HANDLE` to the vanity domain and configure DNS handle resolution per Step 4 below. The PDS URL stays as the mycrab domain.
- If **no**: proceed with `HANDLE=alice.mycrab.space`. Handle resolution is automatic (mycrab.space handles `.well-known/atproto-did` at the subdomain level).

### A3 — Tunnel setup (already done by mycrab)

mycrab.space uses Cloudflare Tunnel internally. No tunnel setup needed. The subdomain is live immediately after provisioning.

Verify:
```bash
curl -sI https://alice.mycrab.space | head -1
# Expected: HTTP/2 200 (or 502 — fine, mypds not started yet)
```

Then **skip to [Step 1 — Install mypds](#step-1--install-mypds)**.

---

## Modality B — Cloudflare Tunnel + own domain

```bash
# Install cloudflared
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared

# Log in — user must open browser link
cloudflared tunnel login

# Create tunnel and route domain
cloudflared tunnel create mypds
cloudflared tunnel route dns mypds pds.example.com   # replace with actual domain

# Config
cat > ~/.cloudflared/config.yml <<EOF
tunnel: <TUNNEL_ID>
credentials-file: /root/.cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: pds.example.com
    service: http://localhost:3030
  - service: http_status:404
EOF

cloudflared tunnel run mypds &
```

Set: `DOMAIN="pds.example.com"`, `PDS_URL="https://pds.example.com"`, `HANDLE="pds.example.com"` (or ask user for preferred handle).

Verify HTTPS is live:
```bash
curl -sI https://pds.example.com | head -1
```

---

## Modality C — nginx + certbot

```bash
apt install -y nginx certbot python3-certbot-nginx

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

Set: `DOMAIN="pds.example.com"`, `PDS_URL="https://pds.example.com"`, `HANDLE="pds.example.com"`.

---

## Step 1 — Install mypds

```bash
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

## Step 2 — Create the ATProto identity (DID)

> By this point you must have: `$PDS_URL` (HTTPS, live), `$HANDLE` (final handle including optional vanity domain).

```bash
git clone https://github.com/isgudtek/mypds /tmp/mypds-setup
cd /tmp/mypds-setup

./test_data/create_identity.sh "$HANDLE" "$PDS_URL" https://plc.directory
```

Successful output:
```
Created identity for alice.mycrab.space at https://plc.directory/did:plc:xxxxxxxxxxxx
rotation key → alice.mycrab.space_rotation_key.pem   ← STORE SAFELY, NEVER ON SERVER
repo signing key → alice.mycrab.space_repo_key.pem   ← needed on server
did:plc string → alice.mycrab.space_did.txt
```

**Tell the user:** The rotation key is their master identity key. They must save it offline and keep it off the server. If it's lost, the DID cannot be recovered.

```bash
cp "${HANDLE}_repo_key.pem" /opt/mypds/repo_key.pem
chmod 600 /opt/mypds/repo_key.pem
DID=$(cat "${HANDLE}_did.txt")
```

---

## Step 3 — DNS handle resolution

Skip this step if **Modality A with no vanity domain** — mycrab.space handles it automatically.

Otherwise, the handle must resolve to the DID. Two methods:

### Method A — DNS TXT record
Add a TXT record at `_atproto.<handle-subdomain>` with value `did=<DID>`.

For `alice.example.com`: TXT record at `_atproto.alice.example.com` → `did=did:plc:xxxxxxxxxxxx`

### Method B — well-known endpoint
Serve `https://<HANDLE>/.well-known/atproto-did` returning just the DID string as body.

Verify:
```bash
dig TXT "_atproto.$HANDLE" +short
# or
curl "https://$HANDLE/.well-known/atproto-did"
```

---

## Step 4 — Create the account

```bash
source /opt/mypds/.venv/bin/activate

mypds account create "$DID" "$HANDLE" \
  --signing_key=/opt/mypds/repo_key.pem \
  --pds-pfx="$PDS_URL" \
  --pds-did-plc=https://plc.directory \
  --unsafe_password=changeme123
```

Default password is `changeme123` — tell the user to change it after first login. The `--unsafe_password` flag skips interactive prompting.

---

## Step 5 — Start mypds

### Quick start (foreground / testing)
```bash
source /opt/mypds/.venv/bin/activate
mypds run --pds-pfx="$PDS_URL" --pds-did-plc=https://plc.directory --port=3030
```

### Production — systemd service

```bash
cat > /etc/systemd/system/mypds.service <<EOF
[Unit]
Description=mypds ATProto Personal Home Node
After=network.target

[Service]
Type=simple
Restart=on-failure
RestartSec=5
WorkingDirectory=/opt/mypds
ExecStart=/opt/mypds/.venv/bin/mypds run --pds-pfx=$PDS_URL --pds-did-plc=https://plc.directory --port=3030

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now mypds
systemctl status mypds
```

---

## Step 6 — Tell the relay you exist

The Bluesky relay does not auto-discover new PDSes. Request a crawl:

```bash
curl --json "{\"hostname\":\"$PDS_URL\"}" \
  "https://bsky.network/xrpc/com.atproto.sync.requestCrawl"
# Expected: {}
```

mypds also does this automatically on startup (5 attempts, 0/8/16/24/32s backoff). But run it manually once to be safe.

---

## Step 7 — Trigger identity + account events

The appview won't index you until it sees events on the firehose:

1. Log into **bsky.app** → Advanced → use custom PDS: `$PDS_URL`
   - Handle: `$HANDLE`
   - Password: set in Step 4

2. **Settings → Handle → "Change" it to the same value.** This emits an `#identity` event to the firehose — required for the relay to associate your DID with the PDS.

3. Set a display name / bio. Creates the profile record, prompts appview indexing.

4. Post something. Verify it appears on bsky.app under your profile.

---

## Step 8 — Access the web UI

Open `$PDS_URL` in a browser:

| Path | Auth | Description |
|------|------|-------------|
| `/` | public | Home: profile, posts, gallery preview |
| `/login` | — | Log in with your password |
| `/dashboard` | owner | Stats, quick actions, app tiles |
| `/compose` | owner | Write ATProto posts |
| `/gallery` | public | `pub.gallery.image` photo grid |
| `/pages` | owner | `com.whtwnd.blog.entry` blog pages |
| `/files` | owner | Upload files via blob store |
| `/node-info` | public | DID, ATProto endpoints, stats |

---

## Troubleshooting

### Port already in use
```bash
kill $(lsof -ti:3030) 2>/dev/null; sleep 1
systemctl restart mypds
```

### Relay not indexing posts
```bash
curl --json "{\"hostname\":\"$PDS_URL\"}" \
  "https://bsky.network/xrpc/com.atproto.sync.requestCrawl"

# Check firehose is emitting
websocat "wss://$DOMAIN/xrpc/com.atproto.sync.subscribeRepos"
```

### Handle not resolving
```bash
dig TXT "_atproto.$HANDLE" +short
curl "https://$HANDLE/.well-known/atproto-did"
```

### Tunnel dies, relay loses WebSocket
mypds auto-requests crawl on startup. Just restart mypds after tunnel comes back:
```bash
systemctl restart mypds
```

### `describeServer` returns wrong URL
`--pds-pfx` must exactly match the public HTTPS URL. No trailing slash.

### Posts show locally but not on bsky.app
1. Verify `requestCrawl` was called after tunnel was live
2. Re-save handle in bsky.app Settings to emit `#identity`
3. Wait ~60s for relay + appview

---

## Upgrading

```bash
source /opt/mypds/.venv/bin/activate
pip install --upgrade --force-reinstall --no-cache-dir git+https://github.com/isgudtek/mypds
systemctl restart mypds
```

---

## File layout

```
/opt/mypds/
├── .venv/              # Python virtualenv
├── repo_key.pem        # ATProto repo signing key (keep secret, but OK on server)
├── millipds.db         # ATProto repo: MST, blobs, DIDs, auth
└── web.sqlite3         # Web layer: sessions, pages, media metadata

# Store these offline, never on server:
<handle>_rotation_key.pem   # master identity key — loss = unrecoverable DID
<handle>_did.txt            # DID string backup
```
