"""Email worker agent: read messages from Outlook folders, summarize each one.

    uv run python -m swarm.agents.email_worker --batch               # daily job: config folders, since last successful run
    uv run python -m swarm.agents.email_worker --batch --dry-run     # list what the batch would process (opens nothing)
    uv run python -m swarm.agents.email_worker --folder "Newsletters" -n 1 [--dry-run]   # ad hoc

The model gets one email as text and has no tools: it cannot click, send, move, or edit anything.
Batch results go to data/swarm.db, reports/YYYY-MM-DD.json and logs/email_worker.log.
"""

import argparse
import json
import logging
import re
import sys
import tomllib
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path

from swarm import logs
from swarm.delivery import SUBJECT_PREFIX
from swarm.connectors import outlook_web
from swarm.connectors.outlook_web import NotSignedIn, OutlookError
from swarm.llm import LLMError
from swarm.skills.summarize import summarize
from swarm.store import Store

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "config" / "email_worker.toml"  # your copy of config/email_worker.example.toml (git-ignored)


def load_config(path: Path = CONFIG) -> dict:
    if not path.exists():
        raise SystemExit(f"Missing {path}. Copy {path.with_name('email_worker.example.toml')} to it and edit.")
    return tomllib.loads(path.read_text(encoding="utf-8"))
REPORTS = ROOT / "reports"

log = logging.getLogger("email_worker")


def exclusion(row_label: str, patterns: list[str]) -> str | None:
    """Which exclude pattern (whole word/phrase, case-insensitive) matches the row's sender/subject/preview,
    if any. Our own digests are always excluded so the pipeline never summarizes itself."""
    if SUBJECT_PREFIX.lower() in row_label.lower():
        return SUBJECT_PREFIX
    return next((p for p in patterns if re.search(rf"\b{re.escape(p)}\b", row_label, re.IGNORECASE)), None)


def to_report(message: dict, folder: str | None, dry_run: bool) -> dict:
    report = {
        "folder": folder,
        "conversation_id": message["conversation_id"],
        "received_at": message["received_at"],
        "subject": message.get("subject"),
        "inbox_row": message["aria_label"],
    }
    if dry_run:
        texts = message.get("bodies", []) + message.get("attached_emails", [])
        if texts:
            report["preview"] = max(texts, key=len)[:300]
        return report
    try:
        report["summary"] = summarize(message)
    except LLMError as e:
        report["error"] = str(e)
    return report


def run(folder: str | None, n: int, dry_run: bool = False, headed: bool = False) -> Iterator[dict]:
    for message in outlook_web.read_messages(folder=folder, n=n, headed=headed):
        yield to_report(message, folder, dry_run)


def batch(config_path: Path = CONFIG, dry_run: bool = False, headed: bool = False, since: datetime | None = None) -> int:
    """Summarize everything in the configured folders since the last successful run (or `since`, to backfill).
    Returns an exit code."""
    config = load_config(config_path)
    store = Store()
    cutoff = since or store.next_cutoff(timedelta(hours=config.get("first_run_lookback_hours", 24)))
    log.info("Batch start%s: folders=%s cutoff=%s", " (dry run)" if dry_run else "", config["folders"], cutoff)
    run_id = None if dry_run else store.start_run(cutoff)
    reports, failed_folders = [], []
    patterns = config.get("exclude", [])

    def skip(meta: dict) -> bool:
        if reason := exclusion(meta["aria_label"] or "", patterns):
            # Never opened, never sent to the model; log the time only, not the row (it may be sensitive).
            log.info("Excluded %s (matched %r)", meta["received_at"], reason)
            return True
        return store.done(meta["conversation_id"], meta["received_at"])

    try:
        with outlook_web.session(headed=headed) as page:
            for folder in config["folders"]:
                try:
                    outlook_web.open_folder(page, folder)
                    # Max over visible rows, not the first row: pinned emails sit on top regardless of date.
                    stamps = [outlook_web.received_at(r) for r in page.locator(outlook_web.LIST_ITEM).all()]
                    newest = max((t for t in stamps if t), default=None)
                    before = len(reports)
                    for message in outlook_web.read_since(page, cutoff, skip=skip, open_messages=not dry_run):
                        report = to_report(message, folder, dry_run)
                        reports.append(report)
                        if not dry_run:
                            store.save(report, run_id)
                        # Log sender/subject only (the row label starts with them), not email content.
                        what = "ERROR " + report["error"] if "error" in report else (report["inbox_row"] or "")[:100]
                        log.info("%s | %s | %s", folder, report["received_at"], what)
                    # Per-folder line so a folder that silently yields nothing is visible in the log.
                    log.info("%s: newest row %s, %d new since cutoff", folder, newest, len(reports) - before)
                except NotSignedIn:
                    raise
                except OutlookError as e:  # one broken folder shouldn't sink the others
                    failed_folders.append(folder)
                    log.error("Folder %s failed: %s", folder, e)
    except NotSignedIn as e:
        log.error("%s  Re-run: uv run tests/outlook_browser/extract_emails.py login", e)
        if run_id:
            store.finish_run(run_id, "failed", 0, 0, str(e))
        return 1

    summarized = sum("summary" in r for r in reports)
    errors = sum("error" in r for r in reports)
    if dry_run:
        print(json.dumps(reports, indent=2, ensure_ascii=False))
        log.info("Dry run: %d emails would be processed", len(reports))
        return 0

    write_daily_report(reports)
    # A folder-level failure means its emails were never seen, so don't advance the cutoff past them.
    status = "failed" if failed_folders else "ok"
    note = f"failed folders: {failed_folders}" if failed_folders else ""
    store.finish_run(run_id, status, summarized, errors, note)
    log.info("Batch %s: %d summarized, %d failed%s", status, summarized, errors, f", {note}" if note else "")
    return 0 if status == "ok" else 1


def write_daily_report(reports: list[dict]) -> None:
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"{date.today().isoformat()}.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    path.write_text(json.dumps(existing + reports, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote %s", path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch", action="store_true", help="process config folders since the last successful run")
    parser.add_argument("--config", type=Path, default=CONFIG, help="batch config file")
    parser.add_argument("--since", type=datetime.fromisoformat, help='batch: backfill from this time, e.g. "2026-10-21 02:54"')
    parser.add_argument("--folder", help="ad hoc: Outlook folder (default Inbox)")
    parser.add_argument("-n", type=int, default=1, help="ad hoc: how many newest messages")
    parser.add_argument("--dry-run", action="store_true", help="don't call the model (batch: don't open messages either)")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    args = parser.parse_args()
    logs.setup("email_worker")

    if args.batch:
        sys.exit(batch(args.config, args.dry_run, args.headed, args.since))

    failed = False
    try:
        for report in run(args.folder, args.n, args.dry_run, args.headed):
            print(json.dumps(report, indent=2, ensure_ascii=False))
            failed |= "error" in report
    except OutlookError as e:
        sys.exit(str(e))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
