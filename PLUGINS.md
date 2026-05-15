# PLUGINS.md — mypds Plugin Development Guide

A mypds plugin is a **self-contained folder** dropped into `src/mypds/plugins/`. It is auto-discovered at startup — no core files need editing.

This guide uses a plugin called `pub.notes.entry` ("notes") as a worked example.

---

## Plugin folder structure

```
src/mypds/plugins/
└── notes/
    ├── __init__.py       # APP_NAME, NSID, SETTINGS, routes, DB helpers
    ├── __main__.py       # subprocess entry point (2 lines, always identical)
    └── templates/
        ├── main.html     # public listing page
        ├── new.html      # owner create form
        ├── tile.html     # dashboard app tile (auto-injected)
        ├── nav.html      # nav <li> entry  (auto-injected)
        └── widget.html   # (optional) homepage sidebar widget
```

That's it. Drop the folder in, restart mypds — the plugin appears in the dashboard and nav.

Each plugin runs as an **isolated subprocess** over a Unix socket at `data/plugins/{name}.sock`. The main process proxies requests to it. Toggling a plugin on/off spawns/kills its subprocess.

---

## What the loader does automatically

When mypds starts, `service.py` scans `src/mypds/plugins/` and for each valid package:

1. Imports the plugin module
2. Reads `APP_NAME` → adds to `KNOWN_APPS` (gets an on/off toggle in the dashboard)
3. Registers `routes` → your routes are live alongside core routes
4. Adds `templates/` → templates are served under the `plugin/<name>/` namespace
5. Injects `tile.html` into the dashboard app grid
6. Injects `nav.html` into the public nav

No core file edits needed.

---

## `__main__.py` (always identical — 2 lines)

```python
from mypds.plugins.notes import routes, APP_NAME
from mypds.plugin_runner import run_plugin
run_plugin(routes, APP_NAME)
```

---

## `__init__.py`

```python
from aiohttp import web
import cbrrr

from mypds.app_util import get_db
from mypds.web import render, get_session, get_web_store, get_node_profile
from mypds import repo_ops, atproto_repo, util

APP_NAME = "notes"
NSID     = "pub.notes.entry"   # set None if no ATProto collection

# Optional: declare configurable settings.
# Rendered automatically at /plugins/notes/settings — no extra code needed.
SETTINGS = [
    {
        "key":         "public_page",
        "type":        "bool",          # bool | str | int | select | text
        "label":       "Public page",
        "description": "Allow visitors to see /notes without logging in.",
        "default":     "0",
        "group":       "visibility",    # optional — groups settings into panels
    },
    {
        "key":     "feed_limit",
        "type":    "select",
        "label":   "Feed size",
        "default": "50",
        "options": [("20", "20"), ("50", "50"), ("100", "100")],
        "group":   "display",
    },
]

# Read a setting anywhere in the plugin:
#   ws = get_web_store(request)
#   val = ws.get_plugin_setting("notes", "public_page")   # returns "0" or "1"
# Settings are also available in ALL templates as:
#   {{ node_settings.plugin_notes_public_page }}

routes = web.RouteTableDef()


# ── DB helper ─────────────────────────────────────────────────────────────────

def _get_notes(db, did: str, limit: int = 50) -> list:
    user_id = db.con.execute("SELECT id FROM user WHERE did=?", (did,)).get
    if user_id is None:
        return []
    rows = db.con.execute(
        "SELECT rkey, value FROM record WHERE repo=? AND nsid=? ORDER BY rkey DESC LIMIT ?",
        (user_id, NSID, limit),
    ).fetchall()
    notes = []
    for rkey, value in rows:
        try:
            rec = cbrrr.decode_dag_cbor(value)
            notes.append({
                "rkey":       rkey,
                "text":       rec.get("text", ""),
                "title":      rec.get("title", ""),
                "tags":       rec.get("tags", []),
                "created_at": rec.get("createdAt", ""),
            })
        except Exception:
            continue
    return notes


# ── Routes ────────────────────────────────────────────────────────────────────

@routes.get("/notes")
async def notes_page(request: web.Request):
    ws = get_web_store(request)
    if not ws.get_app_enabled(APP_NAME):
        raise web.HTTPNotFound()
    db = get_db(request)
    profile = get_node_profile(db)
    notes = _get_notes(db, profile["did"]) if profile["did"] else []
    return render(request, "plugin/notes/main.html", {"profile": profile, "notes": notes})


@routes.get("/notes/new")
async def notes_new_page(request: web.Request):
    session = get_session(request)
    if not session:
        raise web.HTTPFound("/login")
    return render(request, "plugin/notes/new.html", {"error": None})


@routes.post("/notes/new")
async def notes_new_post(request: web.Request):
    session = get_session(request)
    if not session:
        raise web.HTTPFound("/login")

    data  = await request.post()
    text  = data.get("text", "").strip()
    title = data.get("title", "").strip()
    tags  = [t.strip() for t in data.get("tags", "").split(",") if t.strip()]

    if not text:
        return render(request, "plugin/notes/new.html", {"error": "Text is required"})

    db   = get_db(request)
    rkey = util.tid_now()

    write = {
        "$type":      "com.atproto.repo.applyWrites#create",
        "collection": NSID,
        "rkey":       rkey,
        "value": {
            "$type":     NSID,
            "text":      text,
            "title":     title,
            "tags":      tags,
            "createdAt": util.iso_string_now(),
        },
    }
    res, seq, fbytes = repo_ops.apply_writes(db, session["did"], [write], None)
    await atproto_repo.firehose_broadcast(request, (seq, fbytes))
    raise web.HTTPFound("/notes")


@routes.post("/notes/{rkey}/delete")
async def notes_delete(request: web.Request):
    session = get_session(request)
    if not session:
        raise web.HTTPFound("/login")

    rkey = request.match_info["rkey"]
    db   = get_db(request)
    write = {
        "$type":      "com.atproto.repo.applyWrites#delete",
        "collection": NSID,
        "rkey":       rkey,
    }
    res, seq, fbytes = repo_ops.apply_writes(db, session["did"], [write], None)
    await atproto_repo.firehose_broadcast(request, (seq, fbytes))
    raise web.HTTPFound("/notes")
```

---

## Templates

Templates reference their own namespace: `plugin/notes/main.html`.

### `templates/main.html`

```html
{% extends "node_base.html" %}
{% block title %}notes{% endblock %}

{% block content %}
<div class="container" style="padding-top:32px;">
  <div class="flex items-center gap-8" style="margin-bottom:20px;">
    <h1 style="font-size:1.2rem;font-weight:700;">Notes</h1>
    {% if session %}
    <a class="btn btn--primary" href="/notes/new" style="margin-left:auto;cursor:pointer;">+ new note</a>
    {% endif %}
  </div>

  {% for note in notes %}
  <div class="panel" style="margin-bottom:12px;">
    {% if note.title %}<div style="font-weight:600;margin-bottom:6px;">{{ note.title }}</div>{% endif %}
    <div style="font-size:.9rem;white-space:pre-wrap;">{{ note.text }}</div>
    {% if session %}
    <form method="post" action="/notes/{{ note.rkey }}/delete" style="margin-top:10px;">
      <button class="btn btn--ghost" type="submit" style="cursor:pointer;font-size:.75rem;">delete</button>
    </form>
    {% endif %}
  </div>
  {% else %}
  <div class="empty"><div class="empty__icon">📝</div>No notes yet.</div>
  {% endfor %}
</div>
{% endblock %}
```

### `templates/tile.html` — dashboard tile

Include a gear icon linking to `/plugins/notes/settings` when the plugin has `SETTINGS` defined:

```html
<div class="app-tile {% if not apps.notes %}app-tile--off{% endif %}">
  <div class="app-tile__icon">
    <svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
      <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
    </svg>
  </div>
  <div class="app-tile__name">
    <a href="/notes" style="color:var(--text)">notes</a>
    {% if apps.notes %}
    <a href="/plugins/notes/settings" title="settings" style="margin-left:6px;color:var(--dim);text-decoration:none;font-size:.7rem;">
      <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:middle;">
        <circle cx="12" cy="12" r="3"/>
        <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
      </svg>
    </a>
    {% endif %}
  </div>
  <div class="app-tile__status" style="color:var(--cyan);">pub.notes.entry</div>
  <form method="post" action="/apps/notes/toggle" style="margin-top:6px;">
    <button class="app-toggle {% if apps.notes %}app-toggle--on{% endif %}" type="submit" style="cursor:pointer;">
      {{ 'on' if apps.notes else 'off' }}
    </button>
  </form>
</div>
```

### `templates/nav.html` — nav entry

```html
{% if apps.notes %}<li><a href="/notes">notes</a></li>{% endif %}
```

---

## Key rules

| Rule | Detail |
|------|--------|
| **`__main__.py` required** | Without it the plugin won't spawn as a subprocess — 2 lines, always identical |
| **No Python floats in ATProto records** | cbrrr atjson mode rejects them. Store coords as `f"{val:.6f}"` strings, parse back with `float()` on read |
| **Always use `repo_ops.apply_writes()`** | Signs the record, updates MST, emits firehose event |
| **Always broadcast** | `await atproto_repo.firehose_broadcast(request, (seq, fbytes))` after every write |
| **Template namespace** | Reference your templates as `plugin/<name>/filename.html` |
| **No emoji icons** | Use inline SVG (Heroicons / Lucide paths) |
| **Auth guard** | Check `get_session(request)` on all owner routes |
| **App enabled guard** | Check `ws.get_app_enabled(APP_NAME)` on public routes — raise `HTTPNotFound()` if off |
| **Settings** | Declare `SETTINGS = [...]` in `__init__.py`; the main process serves `/plugins/{name}/settings` automatically — no extra routes or templates needed |

---

## Reference implementations

| Plugin | What to look at |
|--------|-----------------|
| `places` | ATProto record write + blob storage |
| `gallery` | Image upload, blob handling |
| `pages` | Own SQLite DB, Markdown, optional ATProto sync |
| `activity` | `SETTINGS` list, domain filter, TID timestamp extraction |
| `dropbox` | Own SQLite DB, file inbox |
