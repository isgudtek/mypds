# PLUGINS.md — mypds Plugin Development Guide

A mypds plugin is a self-contained **app** that adds a new content type to the node. Each app gets an ATProto lexicon (NSID), a set of aiohttp routes, Jinja2 templates, and a dashboard tile. Everything lives in the same Python package — there is no plugin loader or separate process.

This guide walks through adding a plugin called `pub.notes.entry` ("notes") as a concrete example.

---

## Anatomy of a plugin

| Component | Where | What it does |
|-----------|-------|--------------|
| App name | `web_store.py` → `KNOWN_APPS` | Registers the toggle key |
| NSID constant | `web.py` | Identifies ATProto collection |
| DB helper | `web.py` | Fetches records from the repo |
| Routes | `web.py` | Public view + owner create/delete |
| Templates | `templates/` | Jinja2 HTML pages |
| Dashboard tile | `node_dashboard.html` | On/off switch in the apps grid |
| Nav link | `node_base.html` | Public nav item (gated on app switch) |
| Stats card | `node_dashboard.html` | Record count on dashboard |
| Homepage entry | `node_home.html` (optional) | Sidebar preview on the home page |

---

## Step 1 — Register the app name

In `web_store.py`, add the app slug to `KNOWN_APPS`:

```python
KNOWN_APPS = ["compose", "pages", "files", "gallery", "links", "places", "dropbox", "notes"]
```

The toggle route (`POST /apps/{app}/toggle`) and `get_all_app_settings()` work automatically for any slug in this list. Default state is **on**.

---

## Step 2 — Define the ATProto lexicon (NSID)

Pick a reverse-DNS NSID for the record type. Declare it as a constant in `web.py`:

```python
NOTES_NSID = "pub.notes.entry"
```

The record lives in the user's signed ATProto repo at:
```
at://<did>/pub.notes.entry/<rkey>
```

Any ATProto client can read it. It's stored in CBOR in the local `record` table.

### Lexicon schema (optional — for interoperability)

Create `lexicons/pub/notes/entry.json`:

```json
{
  "lexicon": 1,
  "id": "pub.notes.entry",
  "defs": {
    "main": {
      "type": "record",
      "key": "tid",
      "record": {
        "type": "object",
        "required": ["text", "createdAt"],
        "properties": {
          "text":      { "type": "string", "maxLength": 10000 },
          "title":     { "type": "string", "maxLength": 200 },
          "tags":      { "type": "array", "items": { "type": "string" } },
          "createdAt": { "type": "string", "format": "datetime" }
        }
      }
    }
  }
}
```

---

## Step 3 — DB helper

Add a function to `web.py` to fetch records from the ATProto repo:

```python
def _get_notes(db, did: str, limit: int = 50) -> list:
    user_id = db.con.execute("SELECT id FROM user WHERE did=?", (did,)).get
    if user_id is None:
        return []
    rows = db.con.execute(
        "SELECT rkey, value FROM record WHERE repo=? AND nsid=? ORDER BY rkey DESC LIMIT ?",
        (user_id, NOTES_NSID, limit),
    ).fetchall()
    notes = []
    for rkey, value in rows:
        try:
            rec = cbrrr.decode_dag_cbor(value)
            notes.append({
                "rkey":      rkey,
                "text":      rec.get("text", ""),
                "title":     rec.get("title", ""),
                "tags":      rec.get("tags", []),
                "created_at": rec.get("createdAt", ""),
            })
        except Exception:
            continue
    return notes
```

Key points:
- Records are CBOR-encoded — always decode with `cbrrr.decode_dag_cbor(value)`
- **Do not store Python `float`** in ATProto records — use `str` and parse back on read (cbrrr's atjson mode rejects floats)
- `rkey` is a TID (timestamp ID) — use it as the record key and for ordering

---

## Step 4 — Routes

All routes go in `web.py` using `@web_routes.get/post(...)`.

### Public listing

```python
@web_routes.get("/notes")
async def notes_page(request: web.Request):
    ws = get_web_store(request)
    if not ws.get_app_enabled("notes"):
        raise web.HTTPNotFound()
    db = get_db(request)
    profile = get_node_profile(db)
    notes = _get_notes(db, profile["did"]) if profile["did"] else []
    return render(request, "node_notes.html", {"profile": profile, "notes": notes})
```

### Owner: new record form

```python
@web_routes.get("/notes/new")
async def notes_new_page(request: web.Request):
    session = get_session(request)
    if not session:
        raise web.HTTPFound("/login")
    return render(request, "node_notes_new.html", {"error": None})
```

### Owner: create record (write to ATProto repo)

```python
@web_routes.post("/notes/new")
async def notes_new_post(request: web.Request):
    session = get_session(request)
    if not session:
        raise web.HTTPFound("/login")

    data = await request.post()
    text  = data.get("text", "").strip()
    title = data.get("title", "").strip()
    tags  = [t.strip() for t in data.get("tags", "").split(",") if t.strip()]

    if not text:
        return render(request, "node_notes_new.html", {"error": "Text required"})

    db = get_db(request)
    rkey = util.tid_now()   # timestamp-based record key

    write = {
        "$type": "com.atproto.repo.applyWrites#create",
        "collection": NOTES_NSID,
        "rkey": rkey,
        "value": {
            "$type": NOTES_NSID,
            "text": text,
            "title": title,
            "tags": tags,
            "createdAt": util.iso_string_now(),
        },
    }
    res, seq, fbytes = repo_ops.apply_writes(db, session["did"], [write], None)
    await atproto_repo.firehose_broadcast(request, (seq, fbytes))

    raise web.HTTPFound("/notes")
```

### Owner: delete record

```python
@web_routes.post("/notes/{rkey}/delete")
async def notes_delete(request: web.Request):
    session = get_session(request)
    if not session:
        raise web.HTTPFound("/login")

    rkey = request.match_info["rkey"]
    db = get_db(request)
    write = {
        "$type": "com.atproto.repo.applyWrites#delete",
        "collection": NOTES_NSID,
        "rkey": rkey,
    }
    res, seq, fbytes = repo_ops.apply_writes(db, session["did"], [write], None)
    await atproto_repo.firehose_broadcast(request, (seq, fbytes))
    raise web.HTTPFound("/notes")
```

---

## Step 5 — Templates

Templates live in `src/millipds/templates/`. They use Jinja2 and extend `node_base.html`.

Every template has access to these globals (injected by `render()`):
- `session` — dict with `did`, `handle` if logged in, else `None`
- `apps` — dict of `{app_name: bool}` for all known apps
- `node_settings` — dict with `nickname`, `pfp_url`, `accent_color`

### `node_notes.html` (public listing)

```html
{% extends "node_base.html" %}
{% block title %}notes{% endblock %}

{% block content %}
<div class="container" style="padding-top:32px;">

  <div class="flex items-center gap-8" style="margin-bottom:20px;">
    <h1 style="font-size:1.2rem; font-weight:700;">Notes</h1>
    {% if session %}
    <a class="btn btn--primary" href="/notes/new" style="margin-left:auto; cursor:pointer;">+ new note</a>
    {% endif %}
  </div>

  {% for note in notes %}
  <div class="panel" style="margin-bottom:12px;">
    {% if note.title %}<div style="font-weight:600; margin-bottom:6px;">{{ note.title }}</div>{% endif %}
    <div style="font-size:.9rem; white-space:pre-wrap;">{{ note.text }}</div>
    {% if session %}
    <form method="post" action="/notes/{{ note.rkey }}/delete" style="margin-top:10px;">
      <button class="btn btn--ghost" type="submit" style="cursor:pointer; font-size:.75rem;">delete</button>
    </form>
    {% endif %}
  </div>
  {% else %}
  <div class="empty"><div class="empty__icon">📝</div>No notes yet.</div>
  {% endfor %}

</div>
{% endblock %}
```

### CSS

Use existing utility classes from `node.css`:
- `.container`, `.panel`, `.panel__title`
- `.btn`, `.btn--primary`, `.btn--ghost`
- `.empty`, `.empty__icon`
- `.flex`, `.gap-8`, `.items-center`
- `.mono`, `.text-dim`

No new CSS needed for basic layouts.

---

## Step 6 — Dashboard tile

In `node_dashboard.html`, add a tile to the `app-grid` div:

```html
{# notes #}
<div class="app-tile {% if not apps.notes %}app-tile--off{% endif %}">
  <div class="app-tile__icon">
    <!-- Heroicons / Lucide inline SVG -->
    <svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
      <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
    </svg>
  </div>
  <div class="app-tile__name"><a href="/notes" style="color:var(--text)">notes</a></div>
  <div class="app-tile__status" style="color:var(--cyan);">pub.notes.entry</div>
  <form method="post" action="/apps/notes/toggle" style="margin-top:6px;">
    <button class="app-toggle {% if apps.notes %}app-toggle--on{% endif %}" type="submit" style="cursor:pointer;">
      {{ 'on' if apps.notes else 'off' }}
    </button>
  </form>
</div>
```

And a stats card:

```html
<a href="/notes" class="stat-card stat-card--link {% if not apps.notes %}stat-card--off{% endif %}">
  <div class="stat-card__value">{{ notes_count }}</div>
  <div class="stat-card__label">notes</div>
</a>
```

Pass `notes_count` from the dashboard route:

```python
notes_count = db.con.execute(
    "SELECT COUNT(*) FROM record WHERE repo=? AND nsid=?", (user_id, NOTES_NSID)
).get or 0
```

---

## Step 7 — Nav link

In `node_base.html`, add to the nav links:

```html
{% if apps.notes %}<li><a href="/notes">notes</a></li>{% endif %}
```

---

## Step 8 — Homepage preview (optional)

In the `/` route in `web.py`, fetch a preview:

```python
notes = _get_notes(db, profile["did"], limit=3) if (profile["did"] and apps.get("notes", True)) else []
```

Pass it to `node_home.html` and add a sidebar panel.

---

## Key patterns to follow

### Writing ATProto records
Always go through `repo_ops.apply_writes()` — this signs the record, updates the MST, and increments the firehose sequence:

```python
res, seq, fbytes = repo_ops.apply_writes(db, session["did"], writes, None)
await atproto_repo.firehose_broadcast(request, (seq, fbytes))
```

### Encoding gotchas
- `cbrrr` atjson mode **rejects Python `float`** — store coordinates as formatted strings (`f"{lat:.6f}"`) and parse back with `float()` on read
- Blob references use `{"$link": "<cid-string>"}` — see `_store_blob()` and `_get_gallery()` for the full blob write/read pattern
- Use `util.tid_now()` for rkeys and `util.iso_string_now()` for `createdAt` fields

### Auth guards
```python
session = get_session(request)
if not session:
    raise web.HTTPFound("/login")
```

### App enabled guard (public routes)
```python
ws = get_web_store(request)
if not ws.get_app_enabled("notes"):
    raise web.HTTPNotFound()
```

### No emojis as icons
Use inline SVG (Heroicons or Lucide paths). Emoji icons break across OS/browser rendering and cannot be styled.

---

## File checklist

```
src/millipds/
├── web_store.py          ← add "notes" to KNOWN_APPS
├── web.py                ← NOTES_NSID constant, _get_notes(), routes
└── templates/
    ├── node_notes.html         ← public listing
    ├── node_notes_new.html     ← owner: create form
    node_dashboard.html   ← add tile + stat card
    node_base.html        ← add nav link
    node_home.html        ← add sidebar preview (optional)
```

No migrations needed — the `app_settings` table handles all plugins via the `KNOWN_APPS` list.

---

## Bsky ping (optional)

If your plugin creates content worth announcing, post a ping to Bluesky after writing the record:

```python
if announce and session:
    ping_text = f"New note: {title or text[:60]}"
    ping_write = {
        "$type": "com.atproto.repo.applyWrites#create",
        "collection": "app.bsky.feed.post",
        "value": {
            "$type": "app.bsky.feed.post",
            "text": ping_text,
            "createdAt": util.iso_string_now(),
        },
    }
    res2, seq2, fbytes2 = repo_ops.apply_writes(db, session["did"], [ping_write], None)
    await atproto_repo.firehose_broadcast(request, (seq2, fbytes2))
```

---

*For a complete reference implementation, see the `places` plugin: `PLACES_NSID` in `web.py`, routes at `/places`, `/places/new`, `/places/{rkey}/delete`, and templates `node_places.html` / `node_places_new.html`.*
