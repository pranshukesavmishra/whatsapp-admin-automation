"""SQLite storage: contacts, tags, message history and broadcast delivery tracking."""
from __future__ import annotations

import csv
import io
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    wa_id        TEXT PRIMARY KEY,
    name         TEXT DEFAULT '',
    opted_in     INTEGER DEFAULT 1,
    first_seen   INTEGER,
    last_inbound INTEGER            -- start of the 24h customer-service window
);
CREATE TABLE IF NOT EXISTS tags (
    wa_id TEXT REFERENCES contacts(wa_id) ON DELETE CASCADE,
    tag   TEXT,
    PRIMARY KEY (wa_id, tag)
);
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_id        TEXT,
    direction    TEXT,             -- in | out
    body         TEXT,
    wamid        TEXT,             -- WhatsApp message id
    status       TEXT,             -- received | queued | sent | delivered | read | failed
    broadcast_id INTEGER,
    ts           INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_wamid ON messages(wamid);
CREATE INDEX IF NOT EXISTS idx_messages_wa ON messages(wa_id);
CREATE TABLE IF NOT EXISTS broadcasts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    body       TEXT,
    tag_filter TEXT,
    created    INTEGER
);
"""

STATUS_RANK = {"queued": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4}


class DB:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    # ---- contacts -------------------------------------------------------
    def get_contact(self, wa_id: str):
        return self.conn.execute("SELECT * FROM contacts WHERE wa_id=?", (wa_id,)).fetchone()

    def upsert_contact(self, wa_id: str, name: str = "", inbound: bool = False) -> bool:
        """Create or update a contact. Returns True if the contact is new."""
        now = int(time.time())
        existing = self.get_contact(wa_id)
        if existing is None:
            self.conn.execute(
                "INSERT INTO contacts (wa_id, name, first_seen, last_inbound) VALUES (?,?,?,?)",
                (wa_id, name, now, now if inbound else None),
            )
        else:
            self.conn.execute(
                "UPDATE contacts SET name = COALESCE(NULLIF(?, ''), name),"
                " last_inbound = CASE WHEN ? THEN ? ELSE last_inbound END WHERE wa_id=?",
                (name, inbound, now, wa_id),
            )
        self.conn.commit()
        return existing is None

    def remove_contact(self, wa_id: str) -> None:
        self.conn.execute("DELETE FROM contacts WHERE wa_id=?", (wa_id,))
        self.conn.commit()

    def set_opt_in(self, wa_id: str, opted_in: bool) -> None:
        self.conn.execute("UPDATE contacts SET opted_in=? WHERE wa_id=?", (int(opted_in), wa_id))
        self.conn.commit()

    def add_tag(self, wa_id: str, tag: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO tags (wa_id, tag) VALUES (?,?)", (wa_id, tag.strip().lower()))
        self.conn.commit()

    def remove_tag(self, wa_id: str, tag: str) -> None:
        self.conn.execute("DELETE FROM tags WHERE wa_id=? AND tag=?", (wa_id, tag.strip().lower()))
        self.conn.commit()

    def tags_for(self, wa_id: str) -> list[str]:
        return [r["tag"] for r in self.conn.execute("SELECT tag FROM tags WHERE wa_id=? ORDER BY tag", (wa_id,))]

    def list_contacts(self, tag: str | None = None, opted_in_only: bool = False):
        sql = "SELECT c.* FROM contacts c"
        args: list = []
        if tag:
            sql += " JOIN tags t ON t.wa_id = c.wa_id AND t.tag = ?"
            args.append(tag.lower())
        if opted_in_only:
            sql += " WHERE c.opted_in = 1"
        sql += " ORDER BY c.first_seen DESC"
        return self.conn.execute(sql, args).fetchall()

    # ---- messages -------------------------------------------------------
    def log_message(self, wa_id, direction, body, wamid=None, status=None, broadcast_id=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO messages (wa_id, direction, body, wamid, status, broadcast_id, ts) VALUES (?,?,?,?,?,?,?)",
            (wa_id, direction, body, wamid, status, broadcast_id, int(time.time())),
        )
        self.conn.commit()
        return cur.lastrowid

    def seen_wamid(self, wamid: str) -> bool:
        """WhatsApp retries webhooks; this keeps replies idempotent."""
        return self.conn.execute(
            "SELECT 1 FROM messages WHERE wamid=? AND direction='in'", (wamid,)
        ).fetchone() is not None

    def update_status(self, wamid: str, status: str) -> None:
        row = self.conn.execute("SELECT status FROM messages WHERE wamid=? AND direction='out'", (wamid,)).fetchone()
        if row is None:
            return
        # Webhooks can arrive out of order: never move a message backwards (read -> delivered).
        if STATUS_RANK.get(status, -1) >= STATUS_RANK.get(row["status"] or "queued", -1):
            self.conn.execute("UPDATE messages SET status=? WHERE wamid=? AND direction='out'", (status, wamid))
            self.conn.commit()

    def history(self, wa_id: str):
        return self.conn.execute("SELECT * FROM messages WHERE wa_id=? ORDER BY ts, id", (wa_id,)).fetchall()

    # ---- broadcasts -----------------------------------------------------
    def create_broadcast(self, body: str, tag_filter: str | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO broadcasts (body, tag_filter, created) VALUES (?,?,?)",
            (body, tag_filter or "", int(time.time())),
        )
        self.conn.commit()
        return cur.lastrowid

    def broadcast_report(self, broadcast_id: int) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) n FROM messages WHERE broadcast_id=? GROUP BY status", (broadcast_id,)
        ).fetchall()
        counts = {r["status"]: r["n"] for r in rows}
        counts["total"] = sum(counts.values())
        return counts

    def list_broadcasts(self):
        return self.conn.execute("SELECT * FROM broadcasts ORDER BY id DESC").fetchall()

    # ---- stats & export -------------------------------------------------
    def stats(self) -> dict:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        day_ago = int(time.time()) - 86400
        return {
            "contacts": q("SELECT COUNT(*) FROM contacts"),
            "opted_in": q("SELECT COUNT(*) FROM contacts WHERE opted_in=1"),
            "opted_out": q("SELECT COUNT(*) FROM contacts WHERE opted_in=0"),
            "inbound_24h": q(f"SELECT COUNT(*) FROM messages WHERE direction='in' AND ts >= {day_ago}"),
            "outbound_24h": q(f"SELECT COUNT(*) FROM messages WHERE direction='out' AND ts >= {day_ago}"),
            "broadcasts": q("SELECT COUNT(*) FROM broadcasts"),
        }

    def export_csv(self, tag: str | None = None) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["wa_id", "name", "opted_in", "tags", "first_seen", "last_inbound"])
        for c in self.list_contacts(tag=tag):
            w.writerow([c["wa_id"], c["name"], c["opted_in"], ";".join(self.tags_for(c["wa_id"])),
                        c["first_seen"], c["last_inbound"] or ""])
        return buf.getvalue()
