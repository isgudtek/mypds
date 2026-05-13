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
		self._init_tables()

	def _migrate(self):
		"""Add columns to existing DB files that predate schema additions."""
		existing = {row[1] for row in self.con.execute("PRAGMA table_info(page)").fetchall()}
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

			CREATE TABLE IF NOT EXISTS page (
				id          INTEGER PRIMARY KEY AUTOINCREMENT,
				slug        TEXT UNIQUE NOT NULL,
				title       TEXT NOT NULL,
				body        TEXT NOT NULL,
				is_published INTEGER NOT NULL DEFAULT 0,
				created_at  INTEGER NOT NULL,
				updated_at  INTEGER NOT NULL,
				published_at INTEGER,
				at_rkey     TEXT,
				at_uri      TEXT
			);

			CREATE TABLE IF NOT EXISTS media_file (
				id          INTEGER PRIMARY KEY AUTOINCREMENT,
				filename    TEXT NOT NULL,
				orig_name   TEXT NOT NULL,
				mime_type   TEXT NOT NULL,
				size        INTEGER NOT NULL,
				is_public   INTEGER NOT NULL DEFAULT 1,
				created_at  INTEGER NOT NULL
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

	# ── Pages ─────────────────────────────────────────────────────────────────

	def list_pages(self) -> List[Dict]:
		rows = self.con.execute(
			"SELECT id, slug, title, is_published, created_at, updated_at, password_hash FROM page ORDER BY updated_at DESC"
		).fetchall()
		result = []
		for r in rows:
			d = dict(r)
			d["is_protected"] = bool(d.get("password_hash"))
			result.append(d)
		return result

	def get_page(self, slug: str) -> Optional[Dict]:
		row = self.con.execute(
			"SELECT * FROM page WHERE slug=?", (slug,)
		).fetchone()
		return dict(row) if row else None

	def get_page_by_id(self, page_id: int) -> Optional[Dict]:
		row = self.con.execute(
			"SELECT * FROM page WHERE id=?", (page_id,)
		).fetchone()
		return dict(row) if row else None

	def create_page(self, slug: str, title: str, body: str, published: bool = False) -> int:
		now = int(time.time())
		cur = self.con.execute(
			"INSERT INTO page(slug, title, body, is_published, created_at, updated_at, published_at) VALUES (?,?,?,?,?,?,?)",
			(slug, title, body, int(published), now, now, now if published else None),
		)
		self.con.commit()
		return cur.lastrowid

	def update_page(self, page_id: int, slug: str, title: str, body: str, published: bool):
		now = int(time.time())
		# Only set published_at the first time it goes live
		row = self.con.execute("SELECT published_at FROM page WHERE id=?", (page_id,)).fetchone()
		pub_at = (row["published_at"] or now) if published else (row["published_at"] if row else None)
		self.con.execute(
			"UPDATE page SET slug=?, title=?, body=?, is_published=?, updated_at=?, published_at=? WHERE id=?",
			(slug, title, body, int(published), now, pub_at, page_id),
		)
		self.con.commit()

	def set_page_password(self, page_id: int, password_hash: Optional[str]):
		"""Set or clear a page password (pass None to remove protection)."""
		self.con.execute(
			"UPDATE page SET password_hash=? WHERE id=?",
			(password_hash, page_id),
		)
		self.con.commit()

	def verify_page_password(self, page_id: int, raw_password: str) -> bool:
		"""Return True if raw_password matches the stored hash for this page."""
		import hashlib
		row = self.con.execute(
			"SELECT password_hash FROM page WHERE id=?", (page_id,)
		).fetchone()
		if row is None or not row["password_hash"]:
			return True  # no password set → always accessible
		expected = hashlib.sha256(raw_password.encode()).hexdigest()
		return row["password_hash"] == expected

	def set_page_atproto(self, page_id: int, at_rkey: str, at_uri: str):
		self.con.execute(
			"UPDATE page SET at_rkey=?, at_uri=? WHERE id=?",
			(at_rkey, at_uri, page_id),
		)
		self.con.commit()

	def delete_page(self, page_id: int):
		self.con.execute("DELETE FROM page WHERE id=?", (page_id,))
		self.con.commit()

	# ── Media Files ───────────────────────────────────────────────────────────

	def list_files(self) -> List[Dict]:
		rows = self.con.execute(
			"SELECT * FROM media_file ORDER BY created_at DESC"
		).fetchall()
		return [dict(r) for r in rows]

	def get_file(self, file_id: int) -> Optional[Dict]:
		row = self.con.execute(
			"SELECT * FROM media_file WHERE id=?", (file_id,)
		).fetchone()
		return dict(row) if row else None

	def get_file_by_name(self, filename: str) -> Optional[Dict]:
		row = self.con.execute(
			"SELECT * FROM media_file WHERE filename=?", (filename,)
		).fetchone()
		return dict(row) if row else None

	def save_file(self, filename: str, orig_name: str, mime_type: str, size: int, is_public: bool = True) -> int:
		cur = self.con.execute(
			"INSERT INTO media_file(filename, orig_name, mime_type, size, is_public, created_at) VALUES (?,?,?,?,?,?)",
			(filename, orig_name, mime_type, size, int(is_public), int(time.time())),
		)
		self.con.commit()
		return cur.lastrowid

	def delete_file(self, file_id: int):
		row = self.get_file(file_id)
		if row:
			path = os.path.join(MEDIA_DIR, row["filename"])
			try:
				os.remove(path)
			except FileNotFoundError:
				pass
		self.con.execute("DELETE FROM media_file WHERE id=?", (file_id,))
		self.con.commit()

	def toggle_file_visibility(self, file_id: int):
		self.con.execute(
			"UPDATE media_file SET is_public = NOT is_public WHERE id=?", (file_id,)
		)
		self.con.commit()

	# ── App Settings ──────────────────────────────────────────────────────────

	KNOWN_APPS = ["compose", "pages", "files", "gallery", "links", "places"]

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
