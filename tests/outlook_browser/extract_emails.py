"""Extract emails from Outlook on the web (outlook.office.com) by driving a real browser.

No Graph API or app registration: it reads the page of your own logged-in session, using a
persistent browser profile in .cache/owa_profile (keep that folder private, since it holds your session).

    uv run tests/outlook_browser/extract_emails.py login          # one-time: sign in (incl. MFA) in a visible window
    uv run tests/outlook_browser/extract_emails.py list -n 10     # inbox rows (sender/subject/time/preview)
    uv run tests/outlook_browser/extract_emails.py read -n 3      # open the 3 newest, extract full content
    uv run tests/outlook_browser/extract_emails.py folders        # folder names visible in the folder pane

Options: --folder NAME (read from this folder instead of Inbox; "Parent/Child" for subfolders),
         --headed (show the browser), --channel msedge (use installed Edge instead of bundled Chromium),
         --out FILE (also write JSON), --debug (save screenshot + HTML to .cache/ when selectors fail).

Opening a message in Outlook marks it as read. To avoid that, set Outlook > Settings > Mail >
Message handling > Mark as read > "Don't automatically mark items as read".
"""

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from swarm.connectors.outlook_web import (
    OutlookError, fail, folders_mode, launch, list_mode, open_folder, open_inbox, read_mode,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["login", "list", "read", "folders"])
    parser.add_argument("-n", type=int, default=5, help="how many messages (newest first)")
    parser.add_argument("--folder", help='folder to read instead of Inbox, e.g. "Archive" or "Projects/Clients"')
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--channel", help='browser channel, e.g. "msedge" or "chrome"')
    parser.add_argument("--out", type=Path, help="also write results as JSON to this file")
    parser.add_argument("--debug", action="store_true", help="save screenshot + HTML on failure")
    args = parser.parse_args()

    with sync_playwright() as p:
        context = launch(p, headed=args.headed or args.mode == "login", channel=args.channel)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            if args.mode == "login":
                print("Sign in in the browser window (including any MFA prompt). Waiting up to 5 minutes for your inbox...")
                if not open_inbox(page, timeout_ms=300_000):
                    fail(page, args.debug, "Inbox did not appear.")
                print("Signed in. Session saved; `list` and `read` can now run headless.")
                return

            if not open_inbox(page, timeout_ms=45_000):
                fail(page, args.debug, "Inbox list not found.")
            if args.mode == "folders":
                data = folders_mode(page)
            else:
                if args.folder:
                    open_folder(page, args.folder, args.debug)
                data = list_mode(page, args.n) if args.mode == "list" else read_mode(page, args.n, args.debug)
        finally:
            context.close()

    text = json.dumps(data, indent=2, ensure_ascii=False)
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252 and mangle curly quotes
    print(text)
    if args.out:
        args.out.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except OutlookError as e:
        sys.exit(str(e))
