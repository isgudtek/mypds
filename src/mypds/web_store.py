"""
web_store.py — Persistent store for web UI features.

Manages: sessions, pages, and file metadata.
Uses a separate SQLite file to stay isolated from the ATProto DB.
"""

import sqlite3
import secrets
import time
import os
from pathlib import Path
from typing import Optional, List, Dict

from . import static_config

WEB_DB_PATH = static_config.DATA_DIR + "/web.sqlite3"
MEDIA_DIR = static_config.DATA_DIR + "/media"
SESSION_TTL = 60 * 60 * 24 * 7  # 7 days


class WebStore:
	def __init__(self, db_path: str = WEB_DB_PATH):
		Path(db_path).parent.mkdir(parents=True, exist_ok=True)
		Path(MEDIA_DIR).mkdir(parents=True, exist_ok=True)
		self.con = sqlite3.connect(db_path, check_same_thread=False)
		self.con.row_factory = sqlite3.Row
		self.con.execute("PRAGMA journal_mode=WAL")
		self._init_tables()

	def _migrate(self):
		"""Add columns to existing DB files that predate schema additions."""
		existing = {row[1] for row in self.con.execute("PRAGMA table_info(page)").fetchall()}
		if existing:  # skip if page table doesn't exist (lives in pages plugin DB)
			for col, defn in [
				("published_at",  "INTEGER"),
				("at_rkey",       "TEXT"),
				("at_uri",        "TEXT"),
				("password_hash", "TEXT"),
			]:
				if col not in existing:
					self.con.execute(f"ALTER TABLE page ADD COLUMN {col} {defn}")
		# Ensure app_settings table exists (may not exist on older installs)
		self.con.execute("""
			CREATE TABLE IF NOT EXISTS app_settings (
				app_name TEXT PRIMARY KEY,
				enabled  INTEGER NOT NULL DEFAULT 1
			)
		""")
		# Ensure node_settings table exists
		self.con.execute("""
			CREATE TABLE IF NOT EXISTS node_settings (
				key   TEXT PRIMARY KEY,
				value TEXT NOT NULL
			)
		""")
		# Ensure app_logins table exists
		self.con.execute("""
			CREATE TABLE IF NOT EXISTS app_logins (
				domain     TEXT NOT NULL,
				nsid       TEXT NOT NULL,
				first_seen INTEGER NOT NULL,
				last_seen  INTEGER NOT NULL,
				call_count INTEGER NOT NULL DEFAULT 1,
				client_url TEXT,
				PRIMARY KEY (domain, nsid)
			)
		""")
		# Add client_url to existing installs
		al_cols = {r[1] for r in self.con.execute("PRAGMA table_info(app_logins)").fetchall()}
		if "client_url" not in al_cols:
			self.con.execute("ALTER TABLE app_logins ADD COLUMN client_url TEXT")
		self.con.commit()

	def _init_tables(self):
		self.con.executescript("""
			CREATE TABLE IF NOT EXISTS web_session (
				token     TEXT PRIMARY KEY,
				did       TEXT NOT NULL,
				handle    TEXT NOT NULL,
				created_at INTEGER NOT NULL,
				expires_at INTEGER NOT NULL
			);

			CREATE TABLE IF NOT EXISTS app_settings (
				app_name TEXT PRIMARY KEY,
				enabled  INTEGER NOT NULL DEFAULT 1
			);

			CREATE TABLE IF NOT EXISTS node_settings (
				key   TEXT PRIMARY KEY,
				value TEXT NOT NULL
			);

		""")
		self.con.commit()
		self._migrate()

	# ── Sessions ──────────────────────────────────────────────────────────────

	def create_session(self, did: str, handle: str) -> str:
		token = secrets.token_urlsafe(32)
		now = int(time.time())
		self.con.execute(
			"INSERT INTO web_session(token, did, handle, created_at, expires_at) VALUES (?,?,?,?,?)",
			(token, did, handle, now, now + SESSION_TTL),
		)
		self.con.commit()
		return token

	def get_session(self, token: str) -> Optional[Dict]:
		row = self.con.execute(
			"SELECT did, handle FROM web_session WHERE token=? AND expires_at > ?",
			(token, int(time.time())),
		).fetchone()
		return dict(row) if row else None

	def delete_session(self, token: str):
		self.con.execute("DELETE FROM web_session WHERE token=?", (token,))
		self.con.commit()

	# ── App Settings ──────────────────────────────────────────────────────────

	KNOWN_APPS = ["compose"]  # all other apps are auto-discovered plugins

	def get_app_enabled(self, app_name: str) -> bool:
		row = self.con.execute(
			"SELECT enabled FROM app_settings WHERE app_name=?", (app_name,)
		).fetchone()
		return bool(row["enabled"]) if row else True  # default on

	def set_app_enabled(self, app_name: str, enabled: bool):
		self.con.execute(
			"INSERT INTO app_settings(app_name, enabled) VALUES(?,?) "
			"ON CONFLICT(app_name) DO UPDATE SET enabled=excluded.enabled",
			(app_name, int(enabled)),
		)
		self.con.commit()

	def get_all_app_settings(self) -> Dict[str, bool]:
		rows = self.con.execute("SELECT app_name, enabled FROM app_settings").fetchall()
		settings = {app: True for app in self.KNOWN_APPS}
		for r in rows:
			settings[r["app_name"]] = bool(r["enabled"])
		return settings

	# ── Node Settings ─────────────────────────────────────────────────────────

	def get_node_setting(self, key: str, default: str = "") -> str:
		row = self.con.execute("SELECT value FROM node_settings WHERE key=?", (key,)).fetchone()
		return row["value"] if row else default

	def set_node_setting(self, key: str, value: str):
		self.con.execute(
			"INSERT INTO node_settings(key, value) VALUES(?,?) "
			"ON CONFLICT(key) DO UPDATE SET value=excluded.value",
			(key, value),
		)
		self.con.commit()

	def get_all_node_settings(self) -> Dict[str, str]:
		rows = self.con.execute("SELECT key, value FROM node_settings").fetchall()
		return {r["key"]: r["value"] for r in rows}

	# ── Plugin Settings ───────────────────────────────────────────────────────
	# Stored in node_settings with key "plugin_{plugin}_{key}" so they are
	# automatically available in all templates via the node_settings dict.

	def get_plugin_setting(self, plugin: str, key: str, default: str = "") -> str:
		return self.get_node_setting(f"plugin_{plugin}_{key}", default)

	def set_plugin_setting(self, plugin: str, key: str, value: str):
		self.set_node_setting(f"plugin_{plugin}_{key}", value)

	def is_initialized(self) -> bool:
		return self.get_node_setting("initialized") == "1"

	def mark_initialized(self):
		self.set_node_setting("initialized", "1")

	# ── App login tracking ────────────────────────────────────────────────────

	def track_app_call(self, domain: str, nsid: str, client_url: str = None) -> None:
		now = int(time.time())
		self.con.execute(
			"INSERT INTO app_logins(domain, nsid, first_seen, last_seen, call_count, client_url) VALUES(?,?,?,?,1,?) "
			"ON CONFLICT(domain, nsid) DO UPDATE SET last_seen=excluded.last_seen, call_count=call_count+1, "
			"client_url=COALESCE(excluded.client_url, app_logins.client_url)",
			(domain, nsid, now, now, client_url),
		)
		self.con.commit()

	def get_app_logins(self) -> list:
		rows = self.con.execute(
			"SELECT domain, nsid, first_seen, last_seen, SUM(call_count) as calls, MAX(client_url) as client_url "
			"FROM app_logins GROUP BY domain, nsid ORDER BY domain, last_seen DESC"
		).fetchall()
		# Group by domain
		apps: dict = {}
		for r in rows:
			d = r["domain"]
			if d not in apps:
				apps[d] = {"domain": d, "first_seen": r["first_seen"],
				            "last_seen": r["last_seen"], "nsids": [], "total_calls": 0,
				            "client_url": r["client_url"]}
			apps[d]["nsids"].append({"nsid": r["nsid"], "calls": r["calls"]})
			apps[d]["total_calls"] += r["calls"]
			apps[d]["first_seen"] = min(apps[d]["first_seen"], r["first_seen"])
			apps[d]["last_seen"]  = max(apps[d]["last_seen"],  r["last_seen"])
			if r["client_url"]:
				apps[d]["client_url"] = r["client_url"]
		return sorted(apps.values(), key=lambda a: a["last_seen"], reverse=True)

