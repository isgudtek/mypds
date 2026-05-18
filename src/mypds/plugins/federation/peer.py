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
                 club_id: str, seed_url: str, membership: str, whitelist_pattern: str):
        self.db = db_conn
        self.own_did = own_did
        self.own_privkey = own_privkey
        self.own_pubkey = own_pubkey
        self.club_id = club_id
        self.seed_url = seed_url.rstrip("/")
        self.membership = membership  # "open" or "whitelist"
        self.whitelist_pattern = whitelist_pattern  # e.g. "*.mycrab.space"
        self._tasks: list[asyncio.Task] = []
        self._known_peers: dict[str, dict] = {}  # did -> {pubkey, pds_url}
        self._running = False

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
        self.db.commit()

    def _store_record(self, author_did: str, rkey: str, plaintext: str, created_at: str, cid: str):
        self.db.execute(
            "INSERT OR IGNORE INTO federation_record"
            " (cid, author_did, rkey, club_id, plaintext, created_at, indexed_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (cid, author_did, rkey, self.club_id, plaintext, created_at,
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        )
        self.db.commit()

    async def _handshake(self, session: aiohttp.ClientSession, peer_url: str):
        """Exchange identity + peer list with a remote node."""
        try:
            payload = {
                "did": self.own_did,
                "pubkey": self.own_pubkey,
                "pds_url": "",  # filled by seed from request origin
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
                            header, payload = cbrrr.decode_dag_cbor(msg.data[:msg.data.index(b'\xa0') if b'\xa0' in msg.data else 0] or msg.data[:8]), {}
                        except Exception:
                            pass
                        try:
                            # Parse CAR frames — simplified: look for club NSID in raw bytes
                            if CLUB_NSID.encode() not in msg.data:
                                continue
                            # Full decode
                            parts = cbrrr.decode_dag_cbor(msg.data, atjson_mode=True)
                            ops = parts.get("ops", []) if isinstance(parts, dict) else []
                            for op in ops:
                                if op.get("action") != "create":
                                    continue
                                # fetch the actual record
                                cid = op.get("cid", {})
                                if isinstance(cid, dict):
                                    cid = cid.get("$link", "")
                                rkey = op.get("path", "").split("/")[-1]
                                await self._fetch_and_store(session, pds_url, peer_did, rkey, cid)
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

    async def run(self):
        self._running = True
        async with aiohttp.ClientSession() as session:
            # Register own pubkey
            self._upsert_member(self.own_did, self.own_pubkey, "")

            # Bootstrap from seed
            await self._handshake(session, self.seed_url)

            # Subscribe to all known peers' firehoses
            peer_tasks = {}
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

                # Re-handshake with seed periodically to get new members
                await self._handshake(session, self.seed_url)
                await asyncio.sleep(300)  # sync every 5 min

    def stop(self):
        self._running = False
