import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def inbound(wa_id, name, text, wamid):
    return {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": wa_id, "profile": {"name": name}}],
        "messages": [{"from": wa_id, "id": wamid, "type": "text", "text": {"body": text}}],
    }}]}]}


def status(wamid, st):
    return {"entry": [{"changes": [{"value": {"statuses": [{"id": wamid, "status": st}]}}]}]}


@pytest.fixture
def client(tmp_path):
    s = Settings(db_path=str(tmp_path / "t.db"), verify_token="tok", dry_run=True)
    app = create_app(s)
    return TestClient(app), app.state.db


def last_out(db, wa_id):
    return [m for m in db.history(wa_id) if m["direction"] == "out"][-1]


def test_webhook_verification(client):
    c, _ = client
    ok = c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    assert c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "bad"}).status_code == 403


def test_welcome_then_faq_then_fallback(client):
    c, db = client
    c.post("/webhook", json=inbound("15550001", "Ana", "hello", "w1"))
    assert last_out(db, "15550001")["body"].startswith("Hi Ana! Welcome")
    c.post("/webhook", json=inbound("15550001", "Ana", "what are your timings?", "w2"))
    assert "6am" in last_out(db, "15550001")["body"]
    c.post("/webhook", json=inbound("15550001", "Ana", "can I bring a friend", "w3"))
    assert "team member" in last_out(db, "15550001")["body"]


def test_duplicate_webhook_is_ignored(client):
    c, db = client
    c.post("/webhook", json=inbound("15550002", "Ben", "hi", "dup"))
    c.post("/webhook", json=inbound("15550002", "Ben", "hi", "dup"))
    assert len([m for m in db.history("15550002") if m["direction"] == "out"]) == 1


def test_opt_out_excludes_from_broadcast(client):
    c, db = client
    c.post("/webhook", json=inbound("15550003", "Cy", "hi", "a1"))
    c.post("/webhook", json=inbound("15550004", "Di", "hi", "a2"))
    c.post("/webhook", json=inbound("15550004", "Di", "STOP", "a3"))
    assert db.get_contact("15550004")["opted_in"] == 0
    r = c.post("/broadcast", data={"body": "Hi {name}, sale today", "tag": ""}, follow_redirects=False)
    bid = int(r.headers["location"].rsplit("/", 1)[1])
    assert db.broadcast_report(bid)["total"] == 1
    assert last_out(db, "15550003")["body"] == "Hi Cy, sale today"


def test_broadcast_by_tag_and_status_tracking(client):
    c, db = client
    c.post("/contacts/add", data={"wa_id": "+1 555 0005", "name": "Eve", "tags": "vip"})
    c.post("/contacts/add", data={"wa_id": "15550006", "name": "Fay", "tags": "trial"})
    r = c.post("/broadcast", data={"body": "VIP night {name}", "tag": "vip"}, follow_redirects=False)
    bid = int(r.headers["location"].rsplit("/", 1)[1])
    assert db.broadcast_report(bid) == {"sent": 1, "total": 1}
    wamid = last_out(db, "15550005")["wamid"]
    c.post("/webhook", json=status(wamid, "read"))
    c.post("/webhook", json=status(wamid, "delivered"))   # out-of-order webhook must not downgrade
    assert db.broadcast_report(bid) == {"read": 1, "total": 1}


def test_outside_window_uses_template(tmp_path):
    sent = []

    class FakeWA:
        def send_text(self, to, body):
            sent.append(("text", to)); return "w-text"
        def send_template(self, to, name, lang, params):
            sent.append(("template", to, name, params)); return "w-tpl"

    s = Settings(db_path=str(tmp_path / "t.db"), dry_run=True)
    app = create_app(s, wa=FakeWA())
    c, db = TestClient(app), app.state.db
    c.post("/webhook", json=inbound("15550007", "Gus", "hi", "g1"))      # inside 24h window
    db.upsert_contact("15550008", "Hal")                                  # never messaged us
    sent.clear()
    c.post("/broadcast", data={"body": "News {name}", "tag": ""})
    kinds = {s_[1]: s_[0] for s_ in sent}
    assert kinds == {"15550007": "text", "15550008": "template"}


def test_export_csv_and_stats(client):
    c, db = client
    c.post("/contacts/add", data={"wa_id": "15550009", "name": "Ivy", "tags": "vip, monthly"})
    csv_text = c.get("/export.csv").text
    assert "15550009,Ivy,1,monthly;vip" in csv_text
    assert c.get("/").status_code == 200 and db.stats()["contacts"] == 1


def test_signature_check(tmp_path):
    s = Settings(db_path=str(tmp_path / "t.db"), app_secret="shh", dry_run=True)
    c = TestClient(create_app(s))
    body = json.dumps(inbound("15550010", "Jo", "hi", "s1")).encode()
    assert c.post("/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 401
    sig = hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    assert c.post("/webhook", content=body, headers={"X-Hub-Signature-256": f"sha256={sig}",
                                                     "Content-Type": "application/json"}).status_code == 200
