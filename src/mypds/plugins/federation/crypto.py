"""
Hybrid encryption for federation club posts.

Per-message flow:
  1. Generate random 32-byte message key
  2. Encrypt plaintext with SecretBox(message_key)  [XSalsa20-Poly1305]
  3. For each member DID, encrypt message_key with SealedBox(member_pubkey)  [X25519+XSalsa20]
  4. Record = {ciphertext, keys: {did: encrypted_key}}

Any member decrypts:
  1. Find their DID in keys{}
  2. SealedBox(privkey).decrypt(keys[own_did]) -> message_key
  3. SecretBox(message_key).decrypt(ciphertext) -> plaintext
"""
import base64
import json

import nacl.public
import nacl.secret
import nacl.utils


def generate_keypair() -> tuple[str, str]:
    """Returns (privkey_b64, pubkey_b64)."""
    priv = nacl.public.PrivateKey.generate()
    return (
        base64.b64encode(bytes(priv)).decode(),
        base64.b64encode(bytes(priv.public_key)).decode(),
    )


def pubkey_from_privkey(privkey_b64: str) -> str:
    priv = nacl.public.PrivateKey(base64.b64decode(privkey_b64))
    return base64.b64encode(bytes(priv.public_key)).decode()


def generate_club_key() -> str:
    """Generate a shared symmetric key for the club."""
    return base64.b64encode(nacl.utils.random(nacl.secret.SecretBox.KEY_SIZE)).decode()


def encrypt(plaintext: str, club_key_b64: str) -> dict:
    """Encrypt with shared club key. Returns {ciphertext}."""
    key = base64.b64decode(club_key_b64)
    box = nacl.secret.SecretBox(key)
    return {"ciphertext": base64.b64encode(box.encrypt(plaintext.encode())).decode()}


def decrypt(record: dict, own_did: str, privkey_b64: str, club_key_b64: str = "") -> str | None:
    """Decrypt shared-key post (v2) or legacy per-member post (v1)."""
    # v2: shared club key
    if "keys" not in record and club_key_b64:
        try:
            key = base64.b64decode(club_key_b64)
            box = nacl.secret.SecretBox(key)
            return box.decrypt(base64.b64decode(record["ciphertext"])).decode()
        except Exception:
            return None
    # v1: per-member key wrapping (legacy)
    encrypted_key = record.get("keys", {}).get(own_did)
    if not encrypted_key:
        return None
    priv = nacl.public.PrivateKey(base64.b64decode(privkey_b64))
    sealed = nacl.public.SealedBox(priv)
    try:
        msg_key = sealed.decrypt(base64.b64decode(encrypted_key))
        box = nacl.secret.SecretBox(msg_key)
        return box.decrypt(base64.b64decode(record["ciphertext"])).decode()
    except Exception:
        return None
