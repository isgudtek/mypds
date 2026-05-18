#!/bin/bash
# mypds 1-command install
# Usage: NAME=gallo PORT=3032 PASS=gallo2026 TOKEN=xxx bash install.sh
# TOKEN: from api.mycrab.space/reserve-domain

set -e

: "${NAME:?NAME required}"
: "${PORT:?PORT required}"
: "${PASS:?PASS required}"
# TOKEN only needed if cloudflared tunnel not already configured

INSTALL=/opt/$NAME
VENV=/opt/$NAME-venv
KEYS=/opt/$NAME-keys
LOG=/tmp/$NAME.log

echo "=== 1. Clone + install ==="
git clone https://github.com/isgudtek/mypds $INSTALL
python3 -m venv $VENV
$VENV/bin/pip install -e $INSTALL pynacl cbrrr PyJWT -q

echo "=== 2. Init database ==="
cd $INSTALL
$VENV/bin/python3 -m mypds init $NAME.mycrab.space

echo "=== 3. Keys ==="
mkdir -p $KEYS
$VENV/bin/python3 -m mypds util keygen > $KEYS/rotation_key.pem
$VENV/bin/python3 -m mypds util keygen > $KEYS/repo_key.pem

echo "=== 4. DID:PLC ==="
REPO_PUB=$($VENV/bin/python3 -m mypds util print_pubkey $KEYS/repo_key.pem)
DID=$($VENV/bin/python3 -m mypds util plcgen \
  --genesis_json=$KEYS/plc_genesis.json \
  --rotation_key=$KEYS/rotation_key.pem \
  --handle=$NAME.mycrab.space \
  --pds_host=https://$NAME.mycrab.space \
  --repo_pubkey=$REPO_PUB)
echo $DID > $KEYS/did.txt
curl -sf -X POST https://plc.directory/$DID \
  -H 'Content-Type: application/json' \
  -d @$KEYS/plc_genesis.json
echo "DID: $DID"

echo "=== 5. Create account ==="
$VENV/bin/python3 -m mypds account create $DID $NAME.mycrab.space \
  --unsafe_password=$PASS \
  --signing_key=$KEYS/repo_key.pem

echo "=== 6. Federation + local URL settings ==="
$VENV/bin/python3 - <<PYEOF
import apsw
db = apsw.Connection('$INSTALL/data/web.sqlite3')
db.execute('''CREATE TABLE IF NOT EXISTS node_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ""
)''')
db.execute('''CREATE TABLE IF NOT EXISTS app_settings (
    app_name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0
)''')
for k, v in [
    ('plugin_federation_club_id',         'mycrab'),
    ('plugin_federation_seed_url',        'https://mypds.mycrab.space'),
    ('plugin_federation_membership',      'open'),
    ('plugin_federation_whitelist_pattern','*.mycrab.space'),
    ('pds_local_url',                     'http://127.0.0.1:$PORT'),
]:
    db.execute('INSERT OR REPLACE INTO node_settings (key,value) VALUES (?,?)', (k, v))
db.execute('INSERT OR REPLACE INTO app_settings (app_name,enabled) VALUES (?,1)', ('federation',))
print('settings done')
PYEOF

echo "=== 7. Cloudflare tunnel ==="
if [ -f ~/.cloudflared/$NAME.yml ]; then
    echo "  Tunnel config exists — reusing"
else
    : "${TOKEN:?TOKEN required for first-time tunnel setup}"
    curl -s https://mycrab.space/agent-setup-auto.sh | MODE=bot bash -s $TOKEN
    pkill -f 'http.server' 2>/dev/null || true
fi
sed -i "s|localhost:[0-9]*|localhost:$PORT|" ~/.cloudflared/$NAME.yml
pkill -f "cloudflared.*$NAME" 2>/dev/null || true
sleep 1
nohup cloudflared tunnel --protocol http2 --config ~/.cloudflared/$NAME.yml run $NAME \
  > /tmp/$NAME-tunnel.log 2>&1 &

echo "=== 8. Start PDS ==="
nohup $VENV/bin/python3 -m mypds run --listen_host=127.0.0.1 --listen_port=$PORT \
  >> $LOG 2>&1 &
sleep 8

echo "=== 9. Verify ==="
curl -sf https://$NAME.mycrab.space/xrpc/_health && echo " health OK"
curl -sf https://$NAME.mycrab.space/.well-known/atproto-did && echo " DID OK"
curl -sf -X POST https://$NAME.mycrab.space/xrpc/com.atproto.server.createSession \
  -H "Content-Type: application/json" \
  -d "{\"identifier\":\"$NAME.mycrab.space\",\"password\":\"$PASS\"}" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(' login OK:', d.get('did','FAIL'))"

echo ""
echo "=== DONE ==="
echo "  URL:      https://$NAME.mycrab.space"
echo "  Handle:   $NAME.mycrab.space"
echo "  Password: $PASS"
echo "  DID:      $DID"
echo "  Log:      $LOG"
