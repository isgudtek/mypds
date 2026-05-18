"""
Peer discovery + firehose subscription for federation clubs.

Bootstrap: connect to seed URL, exchange {did, pubkey, pds_url}.
Gossip: on connect, share known peer list; merge incoming.
Firehose: subscribe to each peer's /xrpc/com.atproto.sync.subscribeRepos,
          filter for club NSID, decrypt, store locally.
"""
import asyncio
import base64
import json
import logging
import time

import aiohttp
import cbrrr

from . import crypto

logger = logging.getLogger(__name__)

CLUB_NSID = "space.mycrab.club.post"
RECONNECT_DELAY = 30  # seconds


class FederationPeer:
    def __init__(self, db_conn, own_did: str, own_privkey: str, own_pubkey: str,
                 club_id: str, seed_url: str, membership: str, whitelist_pattern: str,
                 own_pds_url: str = ""):
        self.db = db_conn
        self.own_did = own_did
        self.own_privkey = own_privkey
        self.own_pubkey = own_pubkey
        self.own_pds_url = own_pds_url.rstrip("/")
        self.club_id = club_id
        self.seed_url = seed_url.rstrip("/")
        self.membership = membership  # "open" or "whitelist"
        self.whitelist_pattern = whitelist_pattern  # e.g. "*.mycrab.space"
        self._tasks: list[asyncio.Task] = []
        self._known_peers: dict[str, dict] = {}  # did -> {pubkey, pds_url}
        self._running = False
        self._new_member_event: asyncio.Event | None = None

    def _is_allowed(self, did: str, handle: str = "") -> bool:
        if self.membership == "open":
            return True
        pat = self.whitelist_pattern.strip()
        if not pat:
            return False
        if pat.startswith("*."):
            suffix = pat[2:]
            return handle.endswith("." + suffix) or handle == suffix
        return handle == pat or did == pat

    def _get_members(self) -> dict[str, str]:
        """Returns {did: pubkey_b64} for all known members."""
        rows = self.db.execute(
            "SELECT did, pubkey FROM federation_member WHERE club_id=?", (self.club_id,)
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def _upsert_member(self, did: str, pubkey: str, pds_url: str):
        self.db.execute(
            "INSERT OR REPLACE INTO federation_member (did, pubkey, pds_url, club_id, added_at)"
            " VALUES (?,?,?,?,?)",
            (did, pubkey, pds_url, self.club_id, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        )

    def _store_record(self, author_did: str, rkey: str, plaintext: str, created_at: str, cid: str):
        self.db.execute(
            "INSERT OR IGNORE INTO federation_record"
            " (cid, author_did, rkey, club_id, plaintext, created_at, indexed_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (cid, author_did, rkey, self.club_id, plaintext, created_at,
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        )

    async def _handshake(self, session: aiohttp.ClientSession, peer_url: str):
        """Exchange identity + peer list with a remote node."""
        try:
            payload = {
                "did": self.own_did,
                "pubkey": self.own_pubkey,
                "pds_url": self.own_pds_url,
                "club_id": self.club_id,
                "peers": [
                    {"did": d, "pubkey": p["pubkey"], "pds_url": p["pds_url"]}
                    for d, p in self._known_peers.items()
                ]
            }
            async with session.post(
                f"{peer_url}/xrpc/space.mycrab.federation.join",
                json=payload, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                # Register self from their perspective
                members = data.get("members", [])
                for m in members:
                    mdid, mpubkey, mpds = m.get("did"), m.get("pubkey"), m.get("pds_url", "")
                    if mdid and mpubkey and mdid != self.own_did:
                        self._upsert_member(mdid, mpubkey, mpds)
                        self._known_peers[mdid] = {"pubkey": mpubkey, "pds_url": mpds}
                logger.info(f"[federation] handshake with {peer_url}: got {len(members)} members")
        except Exception as e:
            logger.debug(f"[federation] handshake failed {peer_url}: {e}")

    async def _subscribe_firehose(self, session: aiohttp.ClientSession, pds_url: str, peer_did: str):
        """Subscribe to a peer's firehose and process club records."""
        ws_url = pds_url.replace("https://", "wss://").replace("http://", "ws://")
        ws_url = f"{ws_url}/xrpc/com.atproto.sync.subscribeRepos"
        while self._running:
            try:
                async with session.ws_connect(ws_url, timeout=aiohttp.ClientTimeout(total=None)) as ws:
                    logger.info(f"[federation] subscribed to {pds_url}")
                    async for msg in ws:
                        if not self._running:
                            break
                        if msg.type != aiohttp.WSMsgType.BINARY:
                            continue
                        try:
                            # ATProto firehose: header_cbor || body_cbor
                            # Header is always {t:"#...",op:1} — find boundary from t-value length
                            raw = msg.data
                            header_end = 8 + (raw[3] & 0x1f)
                            body = cbrrr.decode_dag_cbor(raw[header_end:], atjson_mode=True)
                            if not isinstance(body, dict):
                                continue
                            repo_did = body.get("repo", peer_did)
                            for op in body.get("ops", []):
                                if op.get("action") != "create":
                                    continue
                                path = op.get("path", "")
                                if not path.startswith(CLUB_NSID + "/"):
                                    continue
                                rkey = path.split("/", 1)[-1]
                                cid_val = op.get("cid", {})
                                cid = cid_val.get("$link", "") if isinstance(cid_val, dict) else str(cid_val)
                                await self._fetch_and_store(session, pds_url, repo_did, rkey, cid)
                        except Exception:
                            pass
            except Exception as e:
                logger.debug(f"[federation] firehose {pds_url} error: {e}")
            if self._running:
                await asyncio.sleep(RECONNECT_DELAY)

    async def _fetch_and_store(self, session: aiohttp.ClientSession, pds_url: str,
                                author_did: str, rkey: str, cid: str):
        try:
            async with session.get(
                f"{pds_url}/xrpc/com.atproto.repo.getRecord",
                params={"repo": author_did, "collection": CLUB_NSID, "rkey": rkey},
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                rec = data.get("value", {})
                if rec.get("clubId") != self.club_id:
                    return
                plaintext = crypto.decrypt(rec, self.own_did, self.own_privkey)
                if plaintext is None:
                    return
                actual_cid = data.get("cid", cid)
                self._store_record(author_did, rkey, plaintext, rec.get("createdAt", ""), actual_cid)
                logger.debug(f"[federation] stored record from {author_did}/{rkey}")
        except Exception as e:
            logger.debug(f"[federation] fetch record error: {e}")

    async def _backfill(self, session: aiohttp.ClientSession, pds_url: str, peer_did: str):
        """Fetch all existing club records from a peer via listRecords."""
        try:
            cursor = None
            while True:
                params = {"repo": peer_did, "collection": CLUB_NSID, "limit": 50}
                if cursor:
                    params["cursor"] = cursor
                async with session.get(
                    f"{pds_url}/xrpc/com.atproto.repo.listRecords",
                    params=params, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        break
                    data = await resp.json()
                    records = data.get("records", [])
                    for item in records:
                        rec = item.get("value", {})
                        cid = item.get("cid", "")
                        rkey = item.get("uri", "").split("/")[-1]
                        if rec.get("clubId") != self.club_id:
                            continue
                        plaintext = crypto.decrypt(rec, self.own_did, self.own_privkey)
                        if plaintext:
                            self._store_record(peer_did, rkey, plaintext, rec.get("createdAt", ""), cid)
                    cursor = data.get("cursor")
                    if not cursor or not records:
                        break
            logger.info(f"[federation] backfill done from {pds_url}")
        except Exception as e:
            logger.debug(f"[federation] backfill error {pds_url}: {e}")

    async def run(self):
        self._running = True
        self._new_member_event = asyncio.Event()
        async with aiohttp.ClientSession() as session:
            # Register own pubkey
            self._upsert_member(self.own_did, self.own_pubkey, self.own_pds_url)

            # Bootstrap from seed
            await self._handshake(session, self.seed_url)

            # Subscribe to all known peers' firehoses + backfill existing records
            peer_tasks = {}
            backfilled = set()
            while self._running:
                current_peers = {
                    r[0]: r[2] for r in self.db.execute(
                        "SELECT did, pubkey, pds_url FROM federation_member WHERE club_id=? AND did!=?",
                        (self.club_id, self.own_did)
                    ).fetchall() if r[2]
                }
                for peer_did, pds_url in current_peers.items():
                    if peer_did not in peer_tasks or peer_tasks[peer_did].done():
                        t = asyncio.create_task(
                            self._subscribe_firehose(session, pds_url, peer_did)
                        )
                        peer_tasks[peer_did] = t
                    if peer_did not in backfilled:
                        backfilled.add(peer_did)
                        asyncio.create_task(self._backfill(session, pds_url, peer_did))

                # Re-handshake with seed periodically to get new members
                await self._handshake(session, self.seed_url)
                # Wait up to 5 min, but wake immediately if a new member joins
                try:
                    await asyncio.wait_for(self._new_member_event.wait(), timeout=300)
                    self._new_member_event.clear()
                except asyncio.TimeoutError:
                    pass

    def notify_new_member(self):
        """Call from the join endpoint to wake the peer loop immediately."""
        if self._new_member_event:
            self._new_member_event.set()

    def stop(self):
        self._running = False
