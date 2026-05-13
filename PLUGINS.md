# PLUGINS.md — mypds Plugin Development Guide

A mypds plugin is a **self-contained folder** dropped into `src/mypds/plugins/`. It is auto-discovered at startup — no core files need editing.

This guide uses a plugin called `pub.notes.entry` ("notes") as a worked example.

---

## Plugin folder structure

```
src/mypds/plugins/
└── notes/
    ├── __init__.py       # APP_NAME, NSID, routes, DB helpers
    └── templates/
        ├── main.html     # public listing page
        ├── new.html      # owner create form
        ├── tile.html     # dashboard app tile (auto-injected)
        └── nav.html      # nav <li> entry  (auto-injected)
```

That's it. Drop the folder in, restart mypds — the plugin appears in the dashboard and nav.

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

## `__init__.py`

```python
from aiohttp import web
import cbrrr

from mypds.app_util import get_db
from mypds.web import render, get_session, get_web_store, get_node_profile
from mypds import repo_ops, atproto_repo, util

APP_NAME = "notes"
NSID     = "pub.notes.entry"

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

```html
<div class="app-tile {% if not apps.notes %}app-tile--off{% endif %}">
  <div class="app-tile__icon">
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

### `templates/nav.html` — nav entry

```html
{% if apps.notes %}<li><a href="/notes">notes</a></li>{% endif %}
```

---

## Key rules

| Rule | Detail |
|------|--------|
| **No Python floats in ATProto records** | cbrrr atjson mode rejects them. Store coords as `f"{val:.6f}"` strings, parse back with `float()` on read |
| **Always use `repo_ops.apply_writes()`** | Signs the record, updates MST, emits firehose event |
| **Always broadcast** | `await atproto_repo.firehose_broadcast(request, (seq, fbytes))` after every write |
| **Template namespace** | Reference your templates as `plugin/<name>/filename.html` |
| **No emoji icons** | Use inline SVG (Heroicons / Lucide paths) |
| **Auth guard** | Check `get_session(request)` on all owner routes |
| **App enabled guard** | Check `ws.get_app_enabled(APP_NAME)` on public routes — raise `HTTPNotFound()` if off |

---

## Reference implementation

The built-in `places` app uses the same patterns:
- `PLACES_NSID` in `web.py`
- Routes at `/places`, `/places/new`, `/places/{rkey}/delete`
- Templates `node_places.html`, `node_places_new.html`

Use it as a reference for blob uploads: see `_store_blob()` and `_get_gallery()` in `web.py`.
