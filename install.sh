#!/bin/bash
# mypds 1-command install via mycrab.space tunnel
#
# Usage:
#   bash <(curl -s https://raw.githubusercontent.com/isgudtek/mypds/main/install.sh)
#   TOKEN=xxx bash <(curl -s ...)          # named subdomain (from mycrab.space)
#   TOKEN=xxx PASS=mypass bash <(curl -s ...)
#
# TOKEN  — mycrab token for named subdomain (optional — free auto-name if omitted)
# PASS   — PDS account password (random 16-char if omitted)

set -euo pipefail

TOKEN="${TOKEN:-}"
PASS="${PASS:-$(openssl rand -base64 12 | tr -dc 'a-zA-Z0-9' | head -c16)}"

echo "========================================"
echo "  mypds 1-command install"
echo "========================================"

# ── 0. Detect OS ───────────────────────────────────────────────────
INSTALL_DIR="${INSTALL_DIR:-$HOME/.mypds}"
IS_MAC=false
[[ "$(uname)" == "Darwin" ]] && IS_MAC=true

# ── 1. System deps ─────────────────────────────────────────────────
echo ""
echo "→ Installing system dependencies..."
if $IS_MAC; then
    command -v python3 >/dev/null || brew install python3
    command -v git >/dev/null || brew install git
else
    apt-get install -y python3-venv python3-pip git curl -qq 2>/dev/null || true
fi

# ── 2. mycrab tunnel setup (gets domain name + cloudflared config) ──
echo ""
echo "→ Setting up mycrab tunnel..."

# Snapshot existing yml files so we can find the new one
BEFORE_YMLS=$(ls ~/.cloudflared/*.yml 2>/dev/null | sort || true)

curl -fsSL https://mycrab.space/agent-setup-auto.sh | MODE=bot bash -s "$TOKEN"
pkill -f 'http.server' 2>/dev/null || true   # cleanup temp server from setup

# Find the newly created yml
AFTER_YMLS=$(ls ~/.cloudflared/*.yml 2>/dev/null | sort || true)
NEW_YML=$(comm -13 <(echo "$BEFORE_YMLS") <(echo "$AFTER_YMLS") | head -1)

if [ -z "$NEW_YML" ]; then
    # Fallback: most recently modified yml
    NEW_YML=$(ls -t ~/.cloudflared/*.yml 2>/dev/null | head -1)
fi
if [ -z "$NEW_YML" ]; then
    echo "ERROR: mycrab tunnel setup failed — no config created. Check TOKEN is valid."
    exit 1
fi

NAME=$(basename "$NEW_YML" .yml)
DOMAIN="${NAME}.mycrab.space"
echo "  Domain : $DOMAIN"
echo "  Config : $NEW_YML"

# ── 3. Find a free port ─────────────────────────────────────────────
FREE_PORT=$(python3 -c "
import socket
for p in [8080, 8081, 8082, 8088, 9000, 9090]:
    try:
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('', p)); s.close(); print(p); break
    except OSError:
        pass
")
echo "  Port   : $FREE_PORT"

# ── 4. Install mypds ────────────────────────────────────────────────
echo ""
echo "→ Installing mypds..."
mkdir -p "$INSTALL_DIR"
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip -q
"$INSTALL_DIR/.venv/bin/pip" install "git+https://github.com/isgudtek/mypds" -q
echo "  mypds $("$INSTALL_DIR/.venv/bin/mypds" --version) installed"

# ── 5. Setup: DID:PLC registration + account creation ───────────────
echo ""
echo "→ Running mypds setup (registers DID:PLC, creates account)..."
cd "$INSTALL_DIR"
rm -rf data/   # wipe any leftover state from previous installs
"$INSTALL_DIR/.venv/bin/mypds" setup "$DOMAIN" --unsafe_password="$PASS"

# Read DID from DB
DID=$("$INSTALL_DIR/.venv/bin/python3" -c "
import apsw
c = apsw.Connection('data/mypds.sqlite3')
print(next(c.execute('SELECT pds_did FROM config'))[0])
" 2>/dev/null || echo "unknown")

# ── 6. Point tunnel at the correct port ─────────────────────────────
echo ""
echo "→ Configuring tunnel → port $FREE_PORT..."
if $IS_MAC; then
    sed -i '' "s|localhost:[0-9]*|localhost:$FREE_PORT|g" "$NEW_YML"
else
    sed -i "s|localhost:[0-9]\+|localhost:$FREE_PORT|g" "$NEW_YML"
fi

# ── 7. Start tunnel ─────────────────────────────────────────────────
echo "→ Starting cloudflare tunnel..."
nohup cloudflared tunnel --protocol http2 --config "$NEW_YML" run "$NAME" \
    > /tmp/${NAME}-tunnel.log 2>&1 &
disown $!
sleep 3

# ── 8. Start mypds ──────────────────────────────────────────────────
echo "→ Starting mypds..."
nohup "$INSTALL_DIR/.venv/bin/mypds" run \
    --listen_host=127.0.0.1 --listen_port="$FREE_PORT" \
    >> /tmp/mypds.log 2>&1 &
disown $!
echo "  Waiting for plugins to load (~10s)..."
sleep 10

# ── 9. Verify ───────────────────────────────────────────────────────
echo ""
echo "→ Verifying..."
HEALTH=$(curl -sf "https://$DOMAIN/xrpc/_health" && echo "OK" || echo "FAIL")
DID_CHECK=$(curl -sf "https://$DOMAIN/.well-known/atproto-did" && echo "OK" || echo "FAIL")
LOGIN=$(curl -sf -X POST "https://$DOMAIN/xrpc/com.atproto.server.createSession" \
    -H "Content-Type: application/json" \
    -d "{\"identifier\":\"$DOMAIN\",\"password\":\"$PASS\"}" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print('OK:', d.get('did','FAIL'))" \
    2>/dev/null || echo "FAIL")

echo "  Health : $HEALTH"
echo "  DID    : $DID_CHECK"
echo "  Login  : $LOGIN"

echo ""
echo "========================================"
echo "  DONE"
echo "========================================"
echo "  URL      : https://$DOMAIN"
echo "  Handle   : $DOMAIN"
echo "  Password : $PASS"
echo "  DID      : $DID"
echo "  PDS log  : /tmp/mypds.log"
echo "  Tunnel   : /tmp/${NAME}-tunnel.log"
echo ""
echo "  ⚠  BACK UP YOUR ROTATION KEY (master identity):"
echo "     $INSTALL_DIR/data/${DOMAIN}_rotation_key.pem"
echo "========================================"
