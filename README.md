# mypds — ATProto Personal Home Node

A self-hosted ATProto PDS with a full web UI, gallery, blog, file manager, compose, and password-protected pages.

Fork of [millipds](https://github.com/DavidBuchanan314/millipds) by David Buchanan.

> **AI agents:** see [`AGENTS.md`](./AGENTS.md) for a complete step-by-step setup guide — zero to live federated node in one session.

---

## What is this?

`mypds` turns your PDS into a **Personal Home Node** — not just a protocol relay, but a living website you actually own:

| App | Lexicon | What it does |
|-----|---------|--------------|
| **Compose** | `app.bsky.feed.post` | Write posts, broadcast to firehose, Bluesky-compatible |
| **Blog / Pages** | `com.whtwnd.blog.entry` | Publish pages/posts, readable by WhiteWind and any ATProto client |
| **Gallery** | `pub.gallery.image` | Photo grid with lightbox, tags, drag-and-drop upload |
| **Files** | blob store | Upload and share any file via ATProto blob store |
| **Places** | `pub.places.pin` | Map of pinned locations, viewable publicly |
| **Links** | `pub.social.linktree` | Public link list / linktree |
| **Dropbox** | blob store | File inbox — anyone can drop files for the owner to review |
| **Connected Apps** | — | OAuth apps that have authenticated; tracks calls per NSID |
| **Node Info** | — | Public stats, ATProto endpoints, DID document |

All data lives in **your** ATProto repo (MST), propagated on the firehose. Every photo, every post, every page is a real ATProto record with a real `at://` URI.

---

## Quick start

```sh
git clone https://github.com/isgudtek/mypds
cd mypds
python3 -m pip install -e .
mypds run --pds-pfx https://your.domain --pds-did-plc https://plc.directory
```

---

## Features

- **ATProto-native storage** — every record in the MST, every blob in the blob store
- **Firehose broadcast** — posts and records propagate to relays in real time
- **OAuth 2.0 login** — third-party ATProto apps (bsky.app, clearsky, WhiteWind, tangled, plyr, etc.) authenticate via standard ATProto OAuth with DPoP (RFC 9449)
- **Connected Apps** — tracks every OAuth app that logs in, with per-NSID call counts and clickable links
- **WhiteWind blog** — pages use `com.whtwnd.blog.entry` so they appear in WhiteWind
- **Password-protected pages** — SHA-256 gate, 24h cookie, owner bypass
- **Images in posts and pages** — markdown `![alt](url)` support, inline blob refs
- **Gallery** — `pub.gallery.image` records with tags, ATProto Browser links
- **Places & Links** — `pub.places.pin` map pins and `pub.social.linktree` link list
- **Dropbox** — public file inbox; owner reviews and accepts/rejects uploads
- **Node info page** — public DID, endpoints, stats, software stack
- **Relay crawl on startup** — auto-requests re-index after tunnel restarts
- **Dark-mode UI** — minimal, monospaced, terminal-aesthetic

---

## Architecture

```
mypds/
├── src/mypds/
│   ├── web.py          # all node UI routes (home, compose, gallery, pages, files, node-info)
│   ├── web_store.py    # web.sqlite3 (sessions, pages, media)
│   ├── atproto_repo.py # MST writes, firehose broadcast
│   ├── service.py      # aiohttp app, Jinja2 env, startup crawl retry
│   ├── templates/      # Jinja2 HTML templates (node_*.html)
│   └── static/         # node.css
```

Storage: two SQLite databases via `apsw`
- `data/mypds.sqlite3` — ATProto repo (MST, blobs, DIDs, auth, OAuth tokens)
- `data/web.sqlite3` — pages, sessions, media metadata, connected apps log

---

## Routes

| Path | Auth | Description |
|------|------|-------------|
| `/` | public | Home: profile, recent posts, gallery preview, pages list |
| `/compose` | owner | Write & post to firehose |
| `/gallery` | public | Photo grid with lightbox |
| `/gallery/upload` | owner | Drag-and-drop photo upload |
| `/pages` | owner | Manage blog pages |
| `/p/{slug}` | public* | Read a page (*password gate if protected) |
| `/files` | owner | Upload files, get direct links |
| `/places` | public | Map of `pub.places.pin` records |
| `/links` | public | Link list / linktree |
| `/dropbox` | public | Public file inbox |
| `/connected-apps` | owner | OAuth apps log with per-NSID call counts |
| `/dashboard` | owner | Stats overview and quick actions |
| `/settings` | owner | Nickname, profile photo, accent color |
| `/node-info` | public | DID, ATProto endpoints, stats |
| `/login` `/logout` | — | Session auth |

---

## ATProto Philosophy

Every feature is a **lexicon + UI**. No proprietary formats. Your data is yours:

```
at://did:plc:xxx/pub.gallery.image/3jwxyz
at://did:plc:xxx/com.whtwnd.blog.entry/3jwxyz
at://did:plc:xxx/app.bsky.feed.post/3jwxyz
```

Any ATProto client can read these. Migrate your DID to another PDS and your data follows.

---

## License

MIT — same as upstream millipds.

Original millipds by [David Buchanan](https://github.com/DavidBuchanan314).
