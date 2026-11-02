"""Daily pipeline (the scheduled task): email worker -> inbox manager -> delivery.

    uv run python -m swarm.pipelines.daily_digest
    uv run python -m swarm.pipelines.daily_digest --skip-batch   # digest whatever is already summarized
    uv run python -m swarm.pipelines.daily_digest --scheduled    # what the task runs: at most once a day, after daily_at

1. Worker: summarize new mail in the configured folders (see swarm.agents.email_worker --batch).
2. Manager: Sonnet ranks all summaries not yet in a digest against config/preferences.md.
3. Delivery: HTML digest in reports/, a Windows notification that opens it, and an email to yourself.
Problems (expired Outlook login, model errors) also surface as Windows notifications.
"""

import argparse
import logging
import socket
import sys
import time
from datetime import datetime

from swarm import delivery, logs
from swarm.agents import email_worker, inbox_manager
from swarm.connectors import outlook_web
from swarm.connectors.outlook_web import OutlookError
from swarm.llm import LLMError
from swarm.store import Store

log = logging.getLogger("daily_digest")


def notify(enabled: bool, title: str, body: str, open_path=None) -> None:
    if not enabled:
        return
    try:
        delivery.toast(title, body, open_path)
    except Exception as e:  # a notification problem must never break the pipeline
        log.warning("Notification failed: %s", e)


def due(store: Store, daily_at: str) -> bool:
    """The task fires at daily_at, at logon and on wake; only the first trigger at/after daily_at with no
    successful run since then does work. Windows' own "run missed task" setting proved unreliable across sleep."""
    hour, minute = map(int, daily_at.split(":"))
    now = datetime.now()
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < scheduled:
        log.info("Not due: before today's %s run time.", daily_at)
        return False
    last_ok = store.last_ok_start()
    if last_ok and last_ok >= scheduled:
        log.info("Not due: already ran successfully at %s.", last_ok)
        return False
    return True


def wait_for_network(host: str = "outlook.office.com", timeout_s: int = 300) -> bool:
    """Right after wake/logon Wi-Fi may still be connecting; wait instead of failing."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            socket.create_connection((host, 443), timeout=5).close()
            return True
        except OSError:
            log.info("Waiting for network...")
            time.sleep(10)
    return False


def run(skip_batch: bool = False) -> int:
    config = email_worker.load_config().get("digest", {})
    toast_on = config.get("windows_notification", True)
    store = Store()
    code = 0

    if not skip_batch:
        code = email_worker.batch()
        last = store.last_run()
        if last and last["status"] == "failed":
            note = last["note"] or ""
            if "Not signed in" in note:
                notify(toast_on, "Inbox agent: Outlook login expired",
                       "Run: uv run tests/outlook_browser/extract_emails.py login")
            else:
                notify(toast_on, "Inbox agent: email batch had problems", note[:200] or "See logs/daily_digest.log")

    try:
        result = inbox_manager.run(store)
    except LLMError as e:
        log.error("Manager failed: %s", e)
        notify(toast_on, "Inbox agent: digest failed", str(e)[:200])
        return 1
    if result is None:
        log.info("No new summaries since the last digest.")
    else:
        digest, emails = result
        digest_id = store.save_digest(digest.model_dump_json(), [e["rowid"] for e in emails])
        path = delivery.write_html(digest, emails)
        store.mark_digest(digest_id, html_path=str(path))
        c = delivery.counts(digest)
        log.info("Digest %d: %d act, %d look, from %d emails -> %s", digest_id, c["act"], c["look"], len(emails), path)
        top = "; ".join(p.headline for p in digest.picks[:2]) or "Nothing matched your preferences."
        notify(toast_on, f"Inbox digest: {c['act']} to act on, {c['look']} worth a look", top, open_path=path)

    if config.get("email_to_self", False):
        code = max(code, email_unsent(store, toast_on))
    return code


def email_unsent(store: Store, toast_on: bool) -> int:
    """Email every recent digest not yet emailed (today's, plus any whose send failed before)."""
    unsent = store.unsent_digests()
    if not unsent:
        return 0
    try:
        with outlook_web.session() as page:
            for digest_id, digest_json in unsent:
                digest = inbox_manager.Digest.model_validate_json(digest_json)
                emails = store.digest_emails(digest_id)
                to = outlook_web.send_to_self(page, delivery.subject(digest),
                                              delivery.render_html(digest, emails), delivery.render_text(digest, emails))
                store.mark_digest(digest_id, emailed=True)
                log.info("Emailed digest %d to %s", digest_id, to)
    except Exception as e:  # browser hiccups included; the digest stays unsent and is retried next run
        log.error("Emailing the digest failed: %s", e)
        notify(toast_on, "Inbox agent: couldn't email the digest", f"{str(e)[:150]} (HTML copy is in reports/)")
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-batch", action="store_true", help="don't read new mail; digest what's already summarized")
    parser.add_argument("--scheduled", action="store_true", help="run only if today's run is due (used by the task)")
    args = parser.parse_args()
    logs.setup("daily_digest")
    if args.scheduled:
        log.info("Scheduled trigger fired.")
        schedule = email_worker.load_config().get("schedule", {})
        if not due(Store(), schedule.get("daily_at", "10:00")):
            sys.exit(0)
        if not wait_for_network():
            log.error("No network after waiting; the next trigger will retry.")
            sys.exit(1)
    try:
        sys.exit(run(args.skip_batch))
    except Exception as e:  # windowless runs have no console, so make every crash visible
        log.exception("Pipeline crashed")
        notify(True, "Inbox agent crashed", f"{type(e).__name__}: {str(e)[:150]} (see logs/daily_digest.log)")
        sys.exit(1)


if __name__ == "__main__":
    main()
