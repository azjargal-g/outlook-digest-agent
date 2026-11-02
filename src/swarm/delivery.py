"""Digest delivery: HTML file, Windows toast notification, and rendering for the email-to-self."""

import base64
import html
import subprocess
from datetime import datetime
from pathlib import Path

from swarm.agents.inbox_manager import Digest

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "reports"
TIERS = [("act", "Act on", "#c62828"), ("look", "Worth a look", "#9a6b00")]
SUBJECT_PREFIX = "[Inbox digest]"  # the worker never reads emails with this subject (no feedback loop)
# Windows PowerShell's registered app ID, so toasts show without registering our own app.
TOAST_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"


def counts(digest: Digest) -> dict[str, int]:
    return {tier: sum(p.tier == tier for p in digest.picks) for tier, _, _ in TIERS}


def subject(digest: Digest) -> str:
    c = counts(digest)
    return f"{SUBJECT_PREFIX} {datetime.now():%a %b %d}: {c['act']} to act on, {c['look']} worth a look"


def render_html(digest: Digest, emails: list[dict]) -> str:
    esc = html.escape
    parts = [f'<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;max-width:680px">']
    for tier, title, color in TIERS:
        picks = [p for p in digest.picks if p.tier == tier]
        if not picks:
            continue
        parts.append(f'<h3 style="color:{color};margin:18px 0 6px">{title} ({len(picks)})</h3><ol style="padding-left:20px">')
        for p in picks:
            e = emails[p.email - 1]
            meta = " · ".join(x for x in [
                f"<b>{esc(p.action)}</b>" if p.action else "",
                f"due {esc(p.deadline)}" if p.deadline else "",
            ] if x)
            parts.append(
                f'<li style="margin-bottom:10px"><b>{esc(p.headline)}</b>'
                + (f"<br>{meta}" if meta else "")
                + f'<br><span style="color:#555">{esc(e["summary"])}</span>'
                + f'<br><span style="color:#888;font-size:12px">{esc(p.why)} | {esc(e["folder"])}, {esc(e["received_at"][:16].replace("T", " "))}</span></li>'
            )
        parts.append("</ol>")
    if not digest.picks:
        parts.append("<p>Nothing matched your preferences this time.</p>")
    parts.append(f'<p style="color:#666">Skipped: {esc(digest.skipped)}</p>')
    parts.append(f'<p style="color:#999;font-size:12px">{len(emails)} emails reviewed. '
                 f'Tune what shows up in config/preferences.md.</p></div>')
    return "\n".join(parts)


def render_text(digest: Digest, emails: list[dict]) -> str:
    lines = []
    for tier, title, _ in TIERS:
        picks = [p for p in digest.picks if p.tier == tier]
        if picks:
            lines.append(f"{title} ({len(picks)})")
            for p in picks:
                extra = " | ".join(x for x in [p.action, f"due {p.deadline}" if p.deadline else None] if x)
                lines.append(f"- {p.headline}" + (f" ({extra})" if extra else ""))
            lines.append("")
    lines.append(f"Skipped: {digest.skipped}")
    lines.append(f"{len(emails)} emails reviewed.")
    return "\n".join(lines)


def write_html(digest: Digest, emails: list[dict]) -> Path:
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"digest-{datetime.now():%Y-%m-%d-%H%M}.html"
    path.write_text(
        f"<!doctype html><meta charset=utf-8><title>{html.escape(subject(digest))}</title>"
        f"<body style='margin:24px'><h2 style='font-family:Segoe UI,Arial'>{html.escape(subject(digest))}</h2>"
        f"{render_html(digest, emails)}</body>",
        encoding="utf-8",
    )
    return path


def toast(title: str, body: str, open_path: Path | None = None) -> None:
    """Windows notification; clicking it opens `open_path` (e.g. the HTML digest)."""
    q = lambda s: html.escape(s, quote=True)
    launch = f' activationType="protocol" launch="{q(open_path.resolve().as_uri())}"' if open_path else ""
    xml = (f'<toast{launch}><visual><binding template="ToastGeneric">'
           f"<text>{q(title)}</text><text>{q(body)}</text></binding></visual></toast>")
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null\n"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null\n"
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        f"$xml.LoadXml('{xml.replace(chr(39), chr(39) * 2)}')\n"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{TOAST_APP_ID}')"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($xml))\n"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                   check=True, timeout=30, capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)  # no console flash when run windowless
