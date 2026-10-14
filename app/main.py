"""FastAPI app: WhatsApp webhook + a small admin dashboard."""
from __future__ import annotations

import json
import logging

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from jinja2 import DictLoader, Environment, select_autoescape

from .bot import handle_inbound, run_broadcast
from .config import Settings, load_content
from .db import DB
from .whatsapp import WhatsAppClient, verify_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

BASE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WA Admin</title><style>
body{font-family:system-ui,sans-serif;max-width:960px;margin:0 auto;padding:16px;color:#1b2a24;background:#f6f8f7}
nav a{margin-right:14px;color:#0e7a6e;font-weight:600;text-decoration:none}
table{border-collapse:collapse;width:100%;background:#fff}td,th{border:1px solid #dce3df;padding:6px 8px;text-align:left;font-size:14px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}.tile{background:#fff;border:1px solid #dce3df;border-radius:8px;padding:12px}
.tile b{font-size:26px;display:block}input,textarea,select{font:inherit;padding:6px}button{font:inherit;padding:6px 12px;background:#0e7a6e;color:#fff;border:0;border-radius:6px;cursor:pointer}
.in{color:#555}.out{color:#0e7a6e}.pill{background:#e6f2ef;border-radius:99px;padding:1px 8px;margin-right:4px;font-size:12px}
</style></head><body><nav><a href="/">Overview</a><a href="/contacts">Contacts</a><a href="/broadcast">Broadcast</a><a href="/export.csv">Export CSV</a></nav>
{% if dry_run %}<p><span class="pill">DRY RUN: messages are logged, not sent</span></p>{% endif %}
{% block body %}{% endblock %}</body></html>"""

PAGES = {
    "base.html": BASE,
    "index.html": """{% extends "base.html" %}{% block body %}<h2>Overview</h2><div class="tiles">
{% for k, v in stats.items() %}<div class="tile"><b>{{ v }}</b>{{ k.replace('_',' ') }}</div>{% endfor %}</div>
<h3>Recent broadcasts</h3><table><tr><th>#</th><th>Message</th><th>Tag</th><th>Report</th></tr>
{% for b in broadcasts %}<tr><td>{{ b.id }}</td><td>{{ b.body[:60] }}</td><td>{{ b.tag_filter or 'all' }}</td><td><a href="/broadcasts/{{ b.id }}">view</a></td></tr>{% endfor %}</table>{% endblock %}""",
    "contacts.html": """{% extends "base.html" %}{% block body %}<h2>Contacts{% if tag %} tagged "{{ tag }}"{% endif %}</h2>
<form method="post" action="/contacts/add"><input name="wa_id" placeholder="Phone with country code" required>
<input name="name" placeholder="Name"><input name="tags" placeholder="tags, comma separated"><button>Add contact</button></form><br>
<table><tr><th>Number</th><th>Name</th><th>Status</th><th>Tags</th><th></th></tr>
{% for c in contacts %}<tr><td><a href="/contacts/{{ c.wa_id }}">{{ c.wa_id }}</a></td><td>{{ c.name }}</td>
<td>{{ 'opted in' if c.opted_in else 'opted out' }}</td><td>{% for t in tags[c.wa_id] %}<a class="pill" href="/contacts?tag={{ t }}">{{ t }}</a>{% endfor %}</td>
<td><form method="post" action="/contacts/{{ c.wa_id }}/delete"><button>Remove</button></form></td></tr>{% endfor %}</table>{% endblock %}""",
    "contact.html": """{% extends "base.html" %}{% block body %}<h2>{{ c.name or c.wa_id }} <small>{{ c.wa_id }}</small></h2>
<p>{{ 'Opted in' if c.opted_in else 'Opted out' }} · tags: {% for t in tags %}<span class="pill">{{ t }}</span>{% endfor %}</p>
<form method="post" action="/contacts/{{ c.wa_id }}/tag"><input name="tag" placeholder="tag"><button name="op" value="add">Add tag</button> <button name="op" value="remove">Remove tag</button></form>
<form method="post" action="/contacts/{{ c.wa_id }}/optin"><button name="value" value="{{ 0 if c.opted_in else 1 }}">{{ 'Opt out' if c.opted_in else 'Opt in' }}</button></form>
<h3>Conversation</h3><table>{% for m in history %}<tr class="{{ m.direction }}"><td>{{ m.direction }}</td><td>{{ m.body }}</td><td>{{ m.status }}</td></tr>{% endfor %}</table>{% endblock %}""",
    "broadcast.html": """{% extends "base.html" %}{% block body %}<h2>New broadcast</h2>
<p>Each opted-in contact gets their own copy. Use {name} to personalise.</p>
<form method="post" action="/broadcast"><textarea name="body" rows="4" cols="60" required>Hi {name}, our new timings start Monday!</textarea><br>
<input name="tag" placeholder="only contacts with this tag (optional)"> <button>Send to each contact</button></form>{% endblock %}""",
    "report.html": """{% extends "base.html" %}{% block body %}<h2>Broadcast #{{ b.id }}</h2><p>{{ b.body }}</p>
<div class="tiles">{% for k, v in report.items() %}<div class="tile"><b>{{ v }}</b>{{ k }}</div>{% endfor %}</div>{% endblock %}""",
}

env = Environment(loader=DictLoader(PAGES), autoescape=select_autoescape())


def create_app(settings: Settings | None = None, wa: WhatsAppClient | None = None) -> FastAPI:
    s = settings or Settings.from_env()
    db = DB(s.db_path)
    client = wa or WhatsAppClient(s)
    app = FastAPI(title="WhatsApp Admin")
    app.state.db, app.state.wa, app.state.settings = db, client, s

    def render(name: str, **ctx) -> HTMLResponse:
        return HTMLResponse(env.get_template(name).render(dry_run=s.dry_run, **ctx))

    # ---- webhook --------------------------------------------------------
    @app.get("/webhook")
    def verify(request: Request):
        p = request.query_params
        if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == s.verify_token:
            return PlainTextResponse(p.get("hub.challenge", ""))
        raise HTTPException(403, "verification failed")

    @app.post("/webhook")
    async def webhook(request: Request):
        raw = await request.body()
        if not verify_signature(s.app_secret, raw, request.headers.get("X-Hub-Signature-256")):
            raise HTTPException(401, "bad signature")
        data = json.loads(raw or b"{}")
        content = load_content(s.config_path)
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                names = {c["wa_id"]: c.get("profile", {}).get("name", "") for c in value.get("contacts", [])}
                for msg in value.get("messages", []):
                    text = msg.get("text", {}).get("body", "") if msg.get("type") == "text" else f"[{msg.get('type')}]"
                    handle_inbound(db, client, content, msg["from"], names.get(msg["from"], ""), text, msg.get("id", ""))
                for st in value.get("statuses", []):
                    db.update_status(st["id"], st["status"])
        return {"ok": True}  # always 200 quickly, or Meta retries

    # ---- dashboard ------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index():
        return render("index.html", stats=db.stats(), broadcasts=db.list_broadcasts()[:10])

    @app.get("/contacts", response_class=HTMLResponse)
    def contacts(tag: str | None = None):
        cs = db.list_contacts(tag=tag)
        return render("contacts.html", contacts=cs, tag=tag, tags={c["wa_id"]: db.tags_for(c["wa_id"]) for c in cs})

    @app.post("/contacts/add")
    def add_contact(wa_id: str = Form(...), name: str = Form(""), tags: str = Form("")):
        wa_id = "".join(ch for ch in wa_id if ch.isdigit())
        db.upsert_contact(wa_id, name)
        for t in filter(None, (x.strip() for x in tags.split(","))):
            db.add_tag(wa_id, t)
        return RedirectResponse("/contacts", status_code=303)

    @app.get("/contacts/{wa_id}", response_class=HTMLResponse)
    def contact(wa_id: str):
        c = db.get_contact(wa_id)
        if c is None:
            raise HTTPException(404, "no such contact")
        return render("contact.html", c=c, tags=db.tags_for(wa_id), history=db.history(wa_id))

    @app.post("/contacts/{wa_id}/tag")
    def tag(wa_id: str, tag: str = Form(...), op: str = Form("add")):
        (db.add_tag if op == "add" else db.remove_tag)(wa_id, tag)
        return RedirectResponse(f"/contacts/{wa_id}", status_code=303)

    @app.post("/contacts/{wa_id}/optin")
    def optin(wa_id: str, value: int = Form(...)):
        db.set_opt_in(wa_id, bool(value))
        return RedirectResponse(f"/contacts/{wa_id}", status_code=303)

    @app.post("/contacts/{wa_id}/delete")
    def delete(wa_id: str):
        db.remove_contact(wa_id)
        return RedirectResponse("/contacts", status_code=303)

    @app.get("/broadcast", response_class=HTMLResponse)
    def broadcast_form():
        return render("broadcast.html")

    @app.post("/broadcast")
    def broadcast(body: str = Form(...), tag: str = Form("")):
        bid = run_broadcast(db, client, load_content(s.config_path), body, tag or None)
        return RedirectResponse(f"/broadcasts/{bid}", status_code=303)

    @app.get("/broadcasts/{bid}", response_class=HTMLResponse)
    def report(bid: int):
        b = next((x for x in db.list_broadcasts() if x["id"] == bid), None)
        if b is None:
            raise HTTPException(404, "no such broadcast")
        return render("report.html", b=b, report=db.broadcast_report(bid))

    @app.get("/export.csv")
    def export(tag: str | None = None):
        return Response(db.export_csv(tag), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=contacts.csv"})

    return app


app = create_app()
