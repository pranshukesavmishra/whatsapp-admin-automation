"""Thin client for the official WhatsApp Business Cloud API (Meta Graph API)."""
from __future__ import annotations

import hashlib
import hmac
import logging
import uuid

import httpx

from .config import Settings

log = logging.getLogger("wa_admin.whatsapp")


class WhatsAppError(RuntimeError):
    pass


class WhatsAppClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.s = settings
        self.http = http or httpx.Client(timeout=15)

    @property
    def url(self) -> str:
        return f"https://graph.facebook.com/{self.s.api_version}/{self.s.phone_number_id}/messages"

    def _post(self, payload: dict) -> str:
        """Send one message and return its WhatsApp message id (wamid)."""
        if self.s.dry_run:
            wamid = f"wamid.DRYRUN.{uuid.uuid4().hex[:16]}"
            log.info("DRY RUN -> %s: %s", payload.get("to"), payload)
            return wamid
        resp = self.http.post(self.url, json=payload, headers={"Authorization": f"Bearer {self.s.access_token}"})
        if resp.status_code >= 400:
            raise WhatsAppError(f"{resp.status_code}: {resp.text}")
        return resp.json()["messages"][0]["id"]

    def send_text(self, to: str, body: str) -> str:
        """Free-form text. Only allowed inside the 24h customer-service window."""
        return self._post({
            "messaging_product": "whatsapp", "recipient_type": "individual",
            "to": to, "type": "text", "text": {"preview_url": False, "body": body},
        })

    def send_template(self, to: str, name: str, language: str, body_params: list[str]) -> str:
        """Pre-approved template. Required for business-initiated messages outside the 24h window."""
        return self._post({
            "messaging_product": "whatsapp", "to": to, "type": "template",
            "template": {
                "name": name, "language": {"code": language},
                "components": [{"type": "body", "parameters": [{"type": "text", "text": p} for p in body_params]}],
            },
        })


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Check Meta's X-Hub-Signature-256 header so only real webhook calls are processed."""
    if not app_secret:
        return True  # signature checks disabled (local demo)
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])
