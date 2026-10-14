"""Settings: secrets come from environment variables, editable bot content from config.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    access_token: str = ""
    phone_number_id: str = ""
    verify_token: str = "change-me"
    app_secret: str = ""          # enables X-Hub-Signature-256 checks when set
    api_version: str = "v22.0"
    db_path: str = str(ROOT / "data" / "wa_admin.db")
    config_path: str = str(ROOT / "config.yaml")
    dry_run: bool = True          # no real API calls; messages go to the outbox table

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            access_token=os.getenv("WA_ACCESS_TOKEN", ""),
            phone_number_id=os.getenv("WA_PHONE_NUMBER_ID", ""),
            verify_token=os.getenv("WA_VERIFY_TOKEN", "change-me"),
            app_secret=os.getenv("WA_APP_SECRET", ""),
            api_version=os.getenv("WA_API_VERSION", "v22.0"),
            db_path=os.getenv("WA_DB_PATH", str(ROOT / "data" / "wa_admin.db")),
            config_path=os.getenv("WA_CONFIG_PATH", str(ROOT / "config.yaml")),
            dry_run=os.getenv("WA_DRY_RUN", "1") not in ("0", "false", "False"),
        )


@dataclass
class BotContent:
    welcome: str
    fallback: str
    opt_out_keywords: list[str]
    opt_in_keywords: list[str]
    opt_out_reply: str
    opt_in_reply: str
    faqs: list[dict] = field(default_factory=list)
    broadcast_template: str = "announcement"   # approved template used outside the 24h window
    template_language: str = "en"


def load_content(path: str) -> BotContent:
    """Re-read on every call so edits to config.yaml apply without a restart."""
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return BotContent(
        welcome=raw.get("welcome", "Hi {name}! Thanks for messaging us."),
        fallback=raw.get("fallback", "Thanks! A team member will reply soon."),
        opt_out_keywords=[k.lower() for k in raw.get("opt_out_keywords", ["stop", "unsubscribe"])],
        opt_in_keywords=[k.lower() for k in raw.get("opt_in_keywords", ["start", "subscribe"])],
        opt_out_reply=raw.get("opt_out_reply", "You won't receive announcements. Reply START to rejoin."),
        opt_in_reply=raw.get("opt_in_reply", "You're subscribed to announcements again."),
        faqs=raw.get("faqs", []),
        broadcast_template=raw.get("broadcast_template", "announcement"),
        template_language=raw.get("template_language", "en"),
    )
