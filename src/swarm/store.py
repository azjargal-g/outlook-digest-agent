"""SQLite store: run history and per-email reports. Stores summaries and IDs only, never email bodies.

An email is keyed by (conversation_id, received_at): a new reply bumps the thread's received_at,
so the thread is summarized again.
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "swarm.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    cutoff TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',  -- running | ok | failed
    summarized INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    note TEXT
);
CREATE TABLE IF NOT EXISTS emails (
    conversation_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    folder TEXT NOT NULL,
    subject TEXT,
    inbox_row TEXT,
    summary TEXT,          -- NULL until summarized successfully
    error TEXT,
    run_id INTEGER REFERENCES runs(id),
    processed_at TEXT NOT NULL,
    PRIMARY KEY (conversation_id, received_at)
);
CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    digest_json TEXT NOT NULL,   -- the manager's structured output
    html_path TEXT,
    emailed INTEGER DEFAULT 0
);
"""


class Store:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(emails)")}
        if "digest_id" not in columns:  # added with the manager layer; NULL = not yet in a digest
            self.db.execute("ALTER TABLE emails ADD COLUMN digest_id INTEGER REFERENCES digests(id)")
            self.db.commit()

    def next_cutoff(self, first_run_lookback: timedelta, retry_window: timedelta = timedelta(days=7)) -> datetime:
        """Start of the last successful run (or now - lookback on the first run), pulled earlier to
        cover recent emails whose summary failed so they get retried."""
        now = datetime.now()
        (last_ok,) = self.db.execute("SELECT MAX(started_at) FROM runs WHERE status = 'ok'").fetchone()
        cutoff = datetime.fromisoformat(last_ok) if last_ok else now - first_run_lookback
        (oldest_failed,) = self.db.execute(
            "SELECT MIN(received_at) FROM emails WHERE summary IS NULL AND received_at >= ?",
            ((now - retry_window).isoformat(),),
        ).fetchone()
        if oldest_failed:
            cutoff = min(cutoff, datetime.fromisoformat(oldest_failed))
        # Outlook shows minute precision; back off a minute so mail that arrived during the last run is
        # not lost. Overlap is harmless because done() skips anything already summarized.
        return cutoff.replace(second=0, microsecond=0) - timedelta(minutes=1)

    def start_run(self, cutoff: datetime) -> int:
        cur = self.db.execute(
            "INSERT INTO runs (started_at, cutoff) VALUES (?, ?)", (datetime.now().isoformat(), cutoff.isoformat())
        )
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str, summarized: int, failed: int, note: str = "") -> None:
        self.db.execute(
            "UPDATE runs SET finished_at = ?, status = ?, summarized = ?, failed = ?, note = ? WHERE id = ?",
            (datetime.now().isoformat(), status, summarized, failed, note, run_id),
        )
        self.db.commit()

    def done(self, conversation_id: str, received_at: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM emails WHERE conversation_id = ? AND received_at = ? AND summary IS NOT NULL",
            (conversation_id, received_at),
        ).fetchone() is not None

    def save(self, report: dict, run_id: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO emails (conversation_id, received_at, folder, subject, inbox_row, summary, error,"
            " run_id, processed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (report["conversation_id"], report["received_at"], report["folder"], report["subject"],
             report["inbox_row"], report.get("summary"), report.get("error"), run_id, datetime.now().isoformat()),
        )
        self.db.commit()

    def last_ok_start(self) -> datetime | None:
        (started,) = self.db.execute("SELECT MAX(started_at) FROM runs WHERE status = 'ok'").fetchone()
        return datetime.fromisoformat(started) if started else None

    def last_run(self) -> dict | None:
        row = self.db.execute("SELECT status, note FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return {"status": row[0], "note": row[1]} if row else None

    def pending_for_digest(self) -> list[dict]:
        """Summarized emails that haven't been through the manager yet, oldest first."""
        cur = self.db.execute(
            "SELECT rowid, folder, received_at, subject, inbox_row, summary FROM emails"
            " WHERE summary IS NOT NULL AND digest_id IS NULL ORDER BY received_at, rowid"
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def save_digest(self, digest_json: str, email_rowids: list[int]) -> int:
        cur = self.db.execute(
            "INSERT INTO digests (created_at, digest_json) VALUES (?, ?)", (datetime.now().isoformat(), digest_json)
        )
        self.db.executemany("UPDATE emails SET digest_id = ? WHERE rowid = ?", [(cur.lastrowid, r) for r in email_rowids])
        self.db.commit()
        return cur.lastrowid

    def digest_emails(self, digest_id: int) -> list[dict]:
        """A digest's emails in the order the manager saw them (its picks refer to these positions)."""
        cur = self.db.execute(
            "SELECT rowid, folder, received_at, subject, inbox_row, summary FROM emails"
            " WHERE digest_id = ? ORDER BY received_at, rowid", (digest_id,)
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def unsent_digests(self, within: timedelta = timedelta(days=2)) -> list[tuple[int, str]]:
        """Recent digests whose email hasn't gone out yet (e.g. a send failed), oldest first."""
        return self.db.execute(
            "SELECT id, digest_json FROM digests WHERE emailed = 0 AND created_at >= ? ORDER BY id",
            ((datetime.now() - within).isoformat(),),
        ).fetchall()

    def mark_digest(self, digest_id: int, html_path: str | None = None, emailed: bool | None = None) -> None:
        if html_path is not None:
            self.db.execute("UPDATE digests SET html_path = ? WHERE id = ?", (html_path, digest_id))
        if emailed is not None:
            self.db.execute("UPDATE digests SET emailed = ? WHERE id = ?", (int(emailed), digest_id))
        self.db.commit()
