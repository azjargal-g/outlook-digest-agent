"""Outlook on the web (outlook.office.com) connector: reads mail by driving a real browser session.

No Graph API or app registration: it reads the DOM of your own logged-in session, using a persistent
browser profile in .cache/owa_profile (keep that folder private, since it holds your session).
Opening a message marks it as read unless Outlook's "Mark as read" setting is turned off.
"""

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[3]
CACHE = ROOT / ".cache"
PROFILE = CACHE / "owa_profile"
OWA_URL = "https://outlook.office.com/mail/"

# Outlook's DOM is not a stable API. These lean on ARIA roles/labels, which change less often than
# class names. Tune them here if extraction breaks (run with --debug to capture the page).
LIST_ITEM = 'div[role="option"][data-convid]'  # data-convid keeps attachment options (also role=option) out
READING_PANE = '#ReadingPaneContainerId, div[role="main"]'
SUBJECT = '[role="heading"]'
BODY = '[aria-label="Message body"]'
FOLDER = '[role="tree"] [role="treeitem"]'
ATTACHMENT = '[aria-label="file attachments"] [role="option"]'  # label: "<name> Open 58 KB"
FILE_EXT = re.compile(r"\.\w{1,5}$")  # attachments named like files (report.pdf) are not attached emails
ICON_GLYPHS = re.compile("[-]")  # icon-font characters (Unicode private use area)
COMPOSE_NEW = 'button[aria-label="New mail"]'
COMPOSE_TO = 'div[aria-label="To"][contenteditable="true"]'
COMPOSE_SUBJECT = 'input[aria-label="Subject"]'
COMPOSE_BODY = 'div[aria-label="Message body"][contenteditable="true"]'
COMPOSE_SEND = 'button[aria-label="Send"]'
COMPOSE_DISCARD = 'button[aria-label="Discard"]'
# Each row has a tooltip with the full local timestamp, e.g. "Tue 10/20/2026 9:37 AM" (US locale in Outlook settings).
STAMP = re.compile(r"\w{3} \d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2} [AP]M")
STAMP_FORMAT = "%a %m/%d/%Y %I:%M %p"


class OutlookError(RuntimeError):
    pass


class NotSignedIn(OutlookError):
    pass


def launch(p, headed: bool, channel: str | None) -> BrowserContext:
    PROFILE.mkdir(parents=True, exist_ok=True)
    return p.chromium.launch_persistent_context(
        PROFILE, headless=not headed, channel=channel, viewport={"width": 1400, "height": 900}
    )


def open_inbox(page: Page, timeout_ms: int) -> bool:
    page.goto(OWA_URL, wait_until="domcontentloaded")
    try:
        page.wait_for_selector(LIST_ITEM, timeout=timeout_ms)
        return True
    except PlaywrightTimeout:
        return False


@contextmanager
def session(headed: bool = False, channel: str | None = None, debug: bool = False) -> Iterator[Page]:
    """A signed-in Outlook page, open on the Inbox, for the duration of the block."""
    with sync_playwright() as p:
        context = launch(p, headed=headed, channel=channel)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            if not open_inbox(page, timeout_ms=45_000):
                fail(page, debug, "Inbox list not found.")
            yield page
        finally:
            context.close()


def dump_debug(page: Page) -> None:
    page.screenshot(path=CACHE / "owa_debug.png", full_page=True)
    (CACHE / "owa_debug.html").write_text(page.content(), encoding="utf-8")
    print(f"Saved {CACHE / 'owa_debug.png'} and owa_debug.html (they contain mail content; delete when done).")


def fail(page: Page, debug: bool, why: str) -> None:
    host = urlparse(page.url).hostname or ""
    if not host.startswith("outlook."):
        raise NotSignedIn(f"Not signed in (landed on {host}). Run the `login` mode first.")
    if debug:
        dump_debug(page)
    raise OutlookError(f"{why} Outlook's layout may have changed: re-run with --debug and adjust the selectors.")


def received_at(item) -> datetime | None:
    for title in item.locator("[title]").evaluate_all("els => els.map(e => e.getAttribute('title'))"):
        if title and STAMP.fullmatch(title):
            return datetime.strptime(title, STAMP_FORMAT)
    return None


def row(item) -> dict:
    lines = [line.strip() for line in item.inner_text().splitlines() if line.strip()]
    when = received_at(item)
    return {
        "conversation_id": item.get_attribute("data-convid"),
        "received_at": when.isoformat() if when else None,
        "aria_label": item.get_attribute("aria-label"),  # Outlook's own one-line description of the row
        "row_text": lines,
    }


def folder_name(item) -> str:
    # First real text line is the name; later lines are unread counts or (if expanded) child folders.
    lines = [ICON_GLYPHS.sub("", line).strip() for line in item.inner_text().splitlines()]
    return next((line for line in lines if line), "")


def folders_mode(page: Page) -> list[dict]:
    return [
        {"name": folder_name(item), "level": item.get_attribute("aria-level"), "expanded": item.get_attribute("aria-expanded")}
        for item in page.locator(FOLDER).all()
    ]


def open_folder(page: Page, path: str, debug: bool = False) -> None:
    segments = [s.strip() for s in path.split("/") if s.strip()]
    for depth, segment in enumerate(segments):
        items = page.locator(FOLDER).all()
        if not items:
            fail(page, debug, "Folder pane not found (is it collapsed in Outlook?).")
        match = next((it for it in items if folder_name(it).lower() == segment.lower()), None)
        if match is None:
            visible = ", ".join(sorted({folder_name(it) for it in items}))
            raise OutlookError(f'Folder "{segment}" not found. Visible folders: {visible}')

        if depth < len(segments) - 1:  # parent: expand so its children render, then keep descending
            if match.get_attribute("aria-expanded") == "false":
                match.focus()
                page.keyboard.press("ArrowRight")
                page.wait_for_timeout(500)
            continue

        first = page.locator(LIST_ITEM).first
        previous = first.get_attribute("data-convid") if first.count() else None
        match.click()
        try:  # wait for the message list to switch to the new folder
            page.wait_for_function(
                "([sel, prev]) => { const r = document.querySelector(sel); return (r ? r.getAttribute('data-convid') : null) !== prev; }",
                arg=[LIST_ITEM, previous],
                timeout=10_000,
            )
        except PlaywrightTimeout:
            pass  # folder is empty, or its newest conversation is also the previous folder's newest
        settle_list(page)


TOP_JS = """row => {
    let s = row;
    while (s && !(s.scrollHeight > s.clientHeight + 5 && /auto|scroll/.test(getComputedStyle(s).overflowY))) s = s.parentElement;
    if (s) s.scrollTop = 0;
}"""


def settle_list(page: Page, stable_s: int = 3, timeout_s: int = 20) -> None:
    """Outlook first renders a folder from its local cache, then inserts newer mail from the server at the
    top. Reading the cached rows made whole folders look empty, so scroll to the top and wait until the
    newest row has stayed the same for `stable_s` seconds."""
    last, stable = None, 0
    for _ in range(timeout_s):
        first = page.locator(LIST_ITEM).first
        if first.count():
            first.evaluate(TOP_JS)
            current = (first.get_attribute("data-convid"), received_at(first))
        else:
            current = None
        stable = stable + 1 if current == last else 0
        if stable >= stable_s:
            return
        last = current
        page.wait_for_timeout(1000)


def list_mode(page: Page, n: int) -> list[dict]:
    items = page.locator(LIST_ITEM)
    return [row(items.nth(i)) for i in range(min(n, items.count()))]


def attached_emails(page: Page, has_attachments: bool) -> list[str]:
    """Text of emails attached to the open message. Mailing lists often wrap the original email this
    way, leaving only the list footer in the visible body. File attachments (pdf, docx...) are skipped."""
    if not has_attachments:
        return []
    attachments = page.locator(READING_PANE).first.locator(ATTACHMENT)
    try:  # the attachment strip renders after the body
        attachments.first.wait_for(timeout=10_000)
    except PlaywrightTimeout:
        return []
    texts = []
    for attachment in attachments.all():
        name = (attachment.get_attribute("aria-label") or "").split(" Open ")[0].strip()
        if FILE_EXT.search(name):
            continue
        before = page.locator(BODY).all_inner_texts()
        attachment.click()  # opens a preview dialog with the attached email's own body
        try:
            page.wait_for_function(
                "([sel, n]) => document.querySelectorAll(sel).length > n", arg=[BODY, len(before)], timeout=15_000
            )
            seen = {t.strip() for t in before}
            texts += [t.strip() for t in page.locator(BODY).all_inner_texts() if t.strip() not in seen]
        except PlaywrightTimeout:
            pass  # not previewable as an email
        page.keyboard.press("Escape")
        try:
            page.wait_for_function(
                "([sel, n]) => document.querySelectorAll(sel).length <= n", arg=[BODY, len(before)], timeout=5_000
            )
        except PlaywrightTimeout:
            pass
    return texts


def open_message(page: Page, item, meta: dict, previous_body: str | None, debug: bool) -> dict:
    """Click a list row and extract the message from the reading pane."""
    item.click()
    try:
        page.wait_for_selector(BODY, timeout=15_000)
        # The pane re-renders in place; wait until it shows a different message than before.
        page.wait_for_function(
            "([sel, prev]) => { const b = document.querySelector(sel); return b && b.innerText !== prev; }",
            arg=[BODY, previous_body],
            timeout=10_000,
        )
    except PlaywrightTimeout:
        if not page.locator(BODY).count():
            fail(page, debug, "Message opened but no body found.")
    pane = page.locator(READING_PANE).first
    bodies = page.locator(BODY).all_inner_texts()  # a thread may show several messages
    subject = pane.locator(SUBJECT).first
    return {
        **meta,
        "subject": subject.inner_text().strip() if subject.count() else None,
        "bodies": [b.strip() for b in bodies],
        "attached_emails": attached_emails(page, "Has attachments" in (meta["aria_label"] or "")),
    }


def read_mode(page: Page, n: int, debug: bool) -> list[dict]:
    items = page.locator(LIST_ITEM)
    results, previous_body = [], None
    for i in range(min(n, items.count())):
        item = items.nth(i)
        message = open_message(page, item, row(item), previous_body, debug)
        results.append(message)
        previous_body = message["bodies"][0] if message["bodies"] else None
    return results


def read_since(page: Page, cutoff: datetime, skip: Callable[[dict], bool] = lambda meta: False,
               open_messages: bool = True, debug: bool = False) -> Iterator[dict]:
    """Walk the open folder newest-first down to `cutoff`, scrolling the list (Outlook only renders
    ~10 rows at a time). Rows for which skip(row metadata) is true are not opened.
    With open_messages=False, yields row metadata only (nothing is opened or marked as read)."""
    handled, previous_body, idle_scrolls = set(), None, 0
    while True:
        rows = page.locator(LIST_ITEM).all()
        new_rows = 0
        for item in rows:
            convid = item.get_attribute("data-convid")
            if convid in handled:
                continue
            handled.add(convid)
            new_rows += 1
            meta = row(item)
            when = meta["received_at"]
            if when is None or datetime.fromisoformat(when) < cutoff or skip(meta):
                continue  # older rows can still sit above newer ones (pinned), so keep scanning
            if not open_messages:
                yield meta
                continue
            message = open_message(page, item, meta, previous_body, debug)
            previous_body = message["bodies"][0] if message["bodies"] else None
            yield message

        oldest = received_at(rows[-1]) if rows else None
        if not rows or (oldest and oldest < cutoff):
            return
        moved = scroll_list(page, rows[-1])
        idle_scrolls = idle_scrolls + 1 if new_rows == 0 or moved == 0 else 0
        if idle_scrolls >= 3:  # reached the end of the folder
            return
        page.wait_for_timeout(1200)


SCROLL_JS = """row => {
    let s = row;
    while (s && !(s.scrollHeight > s.clientHeight + 5 && /auto|scroll/.test(getComputedStyle(s).overflowY))) s = s.parentElement;
    if (!s) return null;
    const before = s.scrollTop;
    s.scrollBy(0, s.clientHeight * 0.6);
    return s.scrollTop - before;
}"""


def scroll_list(page: Page, any_row) -> float | None:
    """Scroll the message list by 60% of its height, so consecutive views overlap and no row is
    skipped. Returns pixels moved (0 at the end of the list), or None if the scroller wasn't found."""
    moved = any_row.evaluate(SCROLL_JS)
    if moved is None:  # fallback: small wheel steps over the list
        any_row.hover()
        page.mouse.wheel(0, 400)
    return moved


def own_address(page: Page) -> str | None:
    """The signed-in mailbox address, as shown at the top of its folder tree."""
    return next((name for name in (folder_name(i) for i in page.locator(FOLDER).all()) if "@" in name), None)


# Outlook's editor ignores innerHTML writes but handles paste events, so HTML goes in as a synthetic paste.
PASTE_JS = """([html, text]) => {
    const data = new DataTransfer();
    data.setData('text/html', html);
    data.setData('text/plain', text);
    document.activeElement.dispatchEvent(new ClipboardEvent('paste', {clipboardData: data, bubbles: true, cancelable: true}));
}"""


def discard_draft(page: Page) -> None:
    page.locator(COMPOSE_DISCARD).first.click()
    confirm = page.locator('[role="dialog"] button', has_text="OK")
    try:  # Outlook asks "Discard message?" once anything was typed
        confirm.first.click(timeout=5_000)
    except PlaywrightTimeout:
        pass


def send_to_self(page: Page, subject: str, html: str, text: str, send: bool = True) -> str:
    """Compose an email to the signed-in account, and only that account, then send it (or, with
    send=False, discard it after filling it in). Plain code, not the model, controls this.
    Returns the recipient address."""
    me = own_address(page)
    if not me:
        raise OutlookError("Could not determine the signed-in address; refusing to send.")
    page.locator(COMPOSE_NEW).first.click()
    to = page.locator(COMPOSE_TO).first
    to.wait_for(timeout=15_000)
    to.click()
    page.keyboard.type(me, delay=20)
    # Outlook suggests "<Display Name> - <address>"; the resolved pill shows only the name, so this ties them.
    suggestion = page.locator(f'[role="option"][aria-label$=" - {me}" i]')
    try:
        suggestion.first.wait_for(timeout=8_000)
        my_name = (suggestion.first.get_attribute("aria-label") or "").rsplit(" - ", 1)[0].strip()
    except PlaywrightTimeout:
        my_name = me  # no suggestion: the pill then shows the address itself
    page.keyboard.press(";")  # resolve the typed address into a recipient pill
    page.wait_for_timeout(1500)
    pills = to.evaluate("e => [...e.querySelectorAll('span')].filter(s => s.children.length === 0)"
                        ".map(s => s.innerText.replace(/[\\u200b-\\u200d\\ufeff]/g, '').trim()).filter(Boolean)")
    send_enabled = page.locator(COMPOSE_SEND).first.get_attribute("aria-disabled") != "true"
    if pills not in ([my_name], [me]) or not send_enabled:
        discard_draft(page)
        raise OutlookError(f"Recipient check failed (expected only {me} / {my_name}, got {pills}, "
                           f"send enabled: {send_enabled}); discarded the draft.")

    page.locator(COMPOSE_SUBJECT).first.fill(subject)
    body = page.locator(COMPOSE_BODY).first
    body.click()
    page.evaluate(PASTE_JS, [html, text])
    page.wait_for_timeout(1000)
    first_line = text.strip().splitlines()[0][:30]
    if first_line not in body.inner_text():  # paste not accepted: fall back to typing plain text
        for line in text.splitlines():
            page.keyboard.insert_text(line)
            page.keyboard.press("Enter")

    if not send:
        discard_draft(page)
        return me
    page.locator(COMPOSE_SEND).first.click()
    try:
        page.locator(COMPOSE_SEND).first.wait_for(state="detached", timeout=15_000)
    except PlaywrightTimeout:
        raise OutlookError("Clicked Send but the compose window didn't close; check Drafts.")
    return me


def read_messages(folder: str | None = None, n: int = 5, headed: bool = False,
                  channel: str | None = None, debug: bool = False) -> list[dict]:
    """Open Outlook, switch to `folder` (default Inbox), and extract the `n` newest messages."""
    with session(headed=headed, channel=channel, debug=debug) as page:
        if folder:
            open_folder(page, folder, debug)
        return read_mode(page, n, debug)
