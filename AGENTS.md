# AGENTS.md — mypds Setup Guide

This file is for AI agents. Follow it top-to-bottom to go from zero to a live, federated ATProto Personal Home Node in one session.

---

## Restarting a running instance

**Two separate processes must both be running** when deployed via Cloudflare Tunnel. Error 1033 = one is dead. 502 = server dead, tunnel alive.

### Step 1 — find the config

```bash
# Where is the repo checked out?
find / -name "mypds" -path "*/plugins/*" 2>/dev/null | head -1
# or just ask the user / check git log

# What port is it using?
ss -tlnp | grep python  # look for the listen port

# Where is the cloudflared config?
ls ~/.cloudflared/*.yml   # or /root/.cloudflared/
grep -l "mypds\|<hostname>" ~/.cloudflared/*.yml

# What tunnel ID?
grep "^tunnel:" ~/.cloudflared/<config>.yml
```

### Step 2 — restart the Python server

```bash
WORK_DIR=<repo working directory>   # e.g. /home/user/mypds
PORT=<listen port>                  # e.g. 3030

# Kill server + orphaned plugin subprocesses
ps aux | grep "mypds\|plugin_runner" | grep -v grep | awk '{print $2}' | xargs -r kill -9

# Remove stale Unix sockets left by dead plugins
rm -f "$WORK_DIR/data/plugins/*.sock"

# Start
cd "$WORK_DIR" && python3 -m mypds run --listen_port=$PORT >> /tmp/mypds.log 2>&1 &

# Wait for all plugins to load (~8s), then verify
sleep 8 && curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:$PORT/
# Expected: 200
```

### Step 3 — restart Cloudflare tunnel (if used)

```bash
TUNNEL_CFG=<path to yml>    # e.g. ~/.cloudflared/mypds.yml
TUNNEL_ID=<id from yml>     # grep "^tunnel:" $TUNNEL_CFG

cloudflared tunnel --config "$TUNNEL_CFG" run "$TUNNEL_ID" >> /tmp/cf-tunnel.log 2>&1 &
sleep 3 && tail -3 /tmp/cf-tunnel.log
# Expected: "Registered tunnel connection" lines
```

> **Note:** plugin subprocesses (activity, portal, etc.) are spawned automatically by the server — do not start them manually.

---

---

## Step 0 — Choose deployment modality

**Ask the user which modality they want before doing anything else.** The answer determines domain, tunnel, and DNS setup for all subsequent steps.

| # | Modality | Best for | What you need |
|---|----------|----------|---------------|
| **A** | **Cloudflare Tunnel** + own domain | Fastest setup, no open ports | Cloudflare account + domain |
| **B** | **nginx + certbot** + own domain | VPS with open ports 80/443 | Domain pointing to server IP |
| **C** | **Other reverse proxy** | Advanced users | User provides their setup |

---

## Modality A — Cloudflare Tunnel + own domain

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

## Modality B — nginx + certbot

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
Created identity for alice.example.com at https://plc.directory/did:plc:xxxxxxxxxxxx
rotation key → alice.example.com_rotation_key.pem   ← STORE SAFELY, NEVER ON SERVER
repo signing key → alice.example.com_repo_key.pem   ← needed on server
did:plc string → alice.example.com_did.txt
```

**Tell the user:** The rotation key is their master identity key. They must save it offline and keep it off the server. If it's lost, the DID cannot be recovered.

```bash
cp "${HANDLE}_repo_key.pem" /opt/mypds/repo_key.pem
chmod 600 /opt/mypds/repo_key.pem
DID=$(cat "${HANDLE}_did.txt")
```

---

## Step 3 — DNS handle resolution

The handle must resolve to the DID. Two methods:

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

## Step 4 — Initialize the database

```bash
source /opt/mypds/.venv/bin/activate

# Initialize: sets pds_did, creates tables
mypds init "$DOMAIN"

# Optional: if you have a pre-generated DID, set it now:
# python3 -c "import sqlite3; con=sqlite3.connect('/opt/mypds/data/mypds.sqlite3'); con.execute('UPDATE config SET pds_did=?', ('did:plc:xxx',)); con.commit()"
```

The `init` command sets `pds_did = did:web:<DOMAIN>` automatically. The database is now ready.

---

## Step 4b — Create the account

```bash
mypds account create "$DID" "$HANDLE" \
  --signing_key=/opt/mypds/repo_key.pem \
  --unsafe_password=changeme123
```

Default password is `changeme123` — tell the user to change it after first login. The `--unsafe_password` flag skips interactive prompting.

---

## Step 5 — Start mypds

### Quick start (foreground / testing)
```bash
source /opt/mypds/.venv/bin/activate
mypds run --listen_host=127.0.0.1 --listen_port=3030
```

All config (pds_did, pds_pfx, etc.) is read from the DB created in Step 4. No flags needed beyond listen address.

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
ExecStart=/opt/mypds/.venv/bin/mypds run --listen_host=127.0.0.1 --listen_port=3030

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now mypds
systemctl status mypds
```

Verify the server is responding:
```bash
curl -s http://localhost:3030/xrpc/com.atproto.server.describeServer | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'DID: {d[\"did\"]}')"
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
| `/pages` | public/owner | `com.whtwnd.blog.entry` blog pages |
| `/places` | public/owner | `pub.places.pin` map of pinned locations |
| `/links` | public | `pub.social.linktree` link list |
| `/files` | owner | Upload files via blob store |
| `/dropbox` | public | File inbox — anyone can send files |
| `/connected-apps` | owner | OAuth apps that have authenticated via this PDS |
| `/settings` | owner | Nickname, profile photo, accent color |
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

### Cloudflare tunnel not running / 502 errors
The PDS process and the Cloudflare tunnel are **two separate processes** — both must be running. If you see 502:
```bash
# Check both are up
ps aux | grep mypds
ps aux | grep cloudflared

# Restart tunnel if dead (adjust path/tunnel-id to match your setup)
cd ~/.cloudflared && cloudflared tunnel --protocol http2 --config mypds.yml run <TUNNEL_ID> >> /tmp/tunnel.log 2>&1 &

# Restart PDS if dead
systemctl restart mypds
# or foreground:
source /opt/mypds/.venv/bin/activate && mypds run --listen_host=127.0.0.1 --listen_port=3030
```

Also verify the tunnel config points to TCP (not unix socket):
```yaml
ingress:
  - hostname: pds.example.com
    service: http://127.0.0.1:3030   # must be TCP, not unix socket
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
├── .venv/                    # Python virtualenv
├── repo_key.pem              # ATProto repo signing key (keep secret, but OK on server)
└── data/
    ├── mypds.sqlite3         # ATProto repo: MST, blobs, DIDs, auth, OAuth tokens
    └── web.sqlite3           # Web layer: sessions, pages, media metadata, connected apps

# Store these offline, never on server:
<handle>_rotation_key.pem   # master identity key — loss = unrecoverable DID
<handle>_did.txt            # DID string backup
```
