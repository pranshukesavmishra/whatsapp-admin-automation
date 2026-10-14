"""Bot logic: welcome new users, answer FAQs, handle opt-in/opt-out, run broadcasts."""
from __future__ import annotations

import logging
import re
import time

from .config import BotContent
from .db import DB
from .whatsapp import WhatsAppClient, WhatsAppError

log = logging.getLogger("wa_admin.bot")
WINDOW_SECONDS = 24 * 3600


def personalize(text: str, name: str) -> str:
    return text.replace("{name}", name or "there")


def match_faq(text: str, faqs: list[dict]) -> str | None:
    """Return the answer of the first FAQ whose keywords appear as whole words in the message."""
    words = set(re.findall(r"[\w']+", text.lower()))
    lowered = text.lower()
    for faq in faqs:
        for kw in faq.get("keywords", []):
            kw = kw.lower()
            if (" " in kw and kw in lowered) or kw in words:
                return faq["answer"]
    return None


def handle_inbound(db: DB, wa: WhatsAppClient, content: BotContent,
                   wa_id: str, name: str, text: str, wamid: str) -> str | None:
    """Process one incoming message. Returns the reply text that was sent, if any."""
    if wamid and db.seen_wamid(wamid):
        log.info("duplicate webhook for %s ignored", wamid)
        return None
    is_new = db.upsert_contact(wa_id, name, inbound=True)
    db.log_message(wa_id, "in", text, wamid=wamid, status="received")

    cmd = text.strip().lower()
    if cmd in content.opt_out_keywords:
        db.set_opt_in(wa_id, False)
        reply = content.opt_out_reply
    elif cmd in content.opt_in_keywords:
        db.set_opt_in(wa_id, True)
        reply = content.opt_in_reply
    else:
        answer = match_faq(text, content.faqs)
        if is_new:
            reply = personalize(content.welcome, name) + (f"\n\n{answer}" if answer else "")
        else:
            reply = answer or content.fallback

    reply = personalize(reply, name)
    try:
        out_id = wa.send_text(wa_id, reply)
        db.log_message(wa_id, "out", reply, wamid=out_id, status="sent")
    except WhatsAppError as exc:
        log.error("reply to %s failed: %s", wa_id, exc)
        db.log_message(wa_id, "out", reply, status="failed")
    return reply


def run_broadcast(db: DB, wa: WhatsAppClient, content: BotContent, body: str,
                  tag: str | None = None, delay: float = 0.0) -> int:
    """Send `body` to each opted-in contact individually (never a group).

    Inside the 24h window a personalised free-form text is sent; outside it, the approved
    template is used with the contact's name and the message as parameters, as Meta requires.
    """
    broadcast_id = db.create_broadcast(body, tag)
    now = time.time()
    for c in db.list_contacts(tag=tag, opted_in_only=True):
        text = personalize(body, c["name"])
        in_window = c["last_inbound"] and now - c["last_inbound"] < WINDOW_SECONDS
        try:
            if in_window:
                wamid = wa.send_text(c["wa_id"], text)
            else:
                wamid = wa.send_template(c["wa_id"], content.broadcast_template, content.template_language,
                                         [c["name"] or "there", text])
            db.log_message(c["wa_id"], "out", text, wamid=wamid, status="sent", broadcast_id=broadcast_id)
        except WhatsAppError as exc:
            log.error("broadcast %s to %s failed: %s", broadcast_id, c["wa_id"], exc)
            db.log_message(c["wa_id"], "out", text, status="failed", broadcast_id=broadcast_id)
        if delay:
            time.sleep(delay)  # stay well under Cloud API throughput limits
    return broadcast_id
