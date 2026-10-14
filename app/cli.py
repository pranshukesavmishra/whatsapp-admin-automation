"""Command-line admin, for running everything from the VPS without the dashboard.

Examples:
    python cli.py add 15551234567 --name "Ana" --tags vip,monthly
    python cli.py tag 15551234567 trial
    python cli.py optout 15551234567
    python cli.py broadcast "Hi {name}, new class on Friday!" --tag vip --delay 0.2
    python cli.py report 1
    python cli.py export contacts.csv --tag vip
    python cli.py stats
"""
import argparse
import json

from app.bot import run_broadcast
from app.config import Settings, load_content
from app.db import DB
from app.whatsapp import WhatsAppClient


def main() -> None:
    p = argparse.ArgumentParser(description="WhatsApp admin CLI")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("wa_id"); a.add_argument("--name", default=""); a.add_argument("--tags", default="")
    t = sub.add_parser("tag"); t.add_argument("wa_id"); t.add_argument("tag"); t.add_argument("--remove", action="store_true")
    r = sub.add_parser("remove"); r.add_argument("wa_id")
    o = sub.add_parser("optout"); o.add_argument("wa_id")
    i = sub.add_parser("optin"); i.add_argument("wa_id")
    b = sub.add_parser("broadcast"); b.add_argument("body"); b.add_argument("--tag"); b.add_argument("--delay", type=float, default=0.1)
    rp = sub.add_parser("report"); rp.add_argument("broadcast_id", type=int)
    e = sub.add_parser("export"); e.add_argument("path"); e.add_argument("--tag")
    sub.add_parser("stats")
    args = p.parse_args()

    s = Settings.from_env()
    db = DB(s.db_path)
    if args.cmd == "add":
        db.upsert_contact(args.wa_id, args.name)
        for tag in filter(None, args.tags.split(",")):
            db.add_tag(args.wa_id, tag)
    elif args.cmd == "tag":
        (db.remove_tag if args.remove else db.add_tag)(args.wa_id, args.tag)
    elif args.cmd == "remove":
        db.remove_contact(args.wa_id)
    elif args.cmd in ("optout", "optin"):
        db.set_opt_in(args.wa_id, args.cmd == "optin")
    elif args.cmd == "broadcast":
        bid = run_broadcast(db, WhatsAppClient(s), load_content(s.config_path), args.body, args.tag, args.delay)
        print(f"broadcast {bid}:", json.dumps(db.broadcast_report(bid)))
    elif args.cmd == "report":
        print(json.dumps(db.broadcast_report(args.broadcast_id), indent=2))
    elif args.cmd == "export":
        with open(args.path, "w", encoding="utf-8", newline="") as fh:
            fh.write(db.export_csv(args.tag))
        print(f"wrote {args.path}")
    elif args.cmd == "stats":
        print(json.dumps(db.stats(), indent=2))


if __name__ == "__main__":
    main()
