"""
mailer.py — Email render and send for Ashcombe AI News Tracker.

Renders the HTML digest via Jinja2 + premailer (inline CSS for email
client compatibility), then sends via SendGrid with SMTP fallback.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import premailer
from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger(__name__)

TEMPLATE_FILE = "template.html"
UK_TZ = ZoneInfo("Europe/London")


def _render_html(
    companies: dict[str, list[dict]],
    run_dt: datetime,
    template_dir: str = ".",
    secondary_digest: dict | None = None,
    profile_changes: list | None = None,
    jobs_changes: list | None = None,
    company_owners: dict | None = None,
) -> str:
    """
    Render the Jinja2 HTML template, then inline all CSS via premailer
    so styles survive webmail clients that strip <style> blocks.
    """
    from collections import Counter

    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template(TEMPLATE_FILE)

    run_dt_uk = run_dt.astimezone(UK_TZ)
    n_companies = len(companies)
    n_items = sum(len(v) for v in companies.values())

    all_items_flat = [item for items in companies.values() for item in items]
    category_counts: dict[str, int] = dict(
        Counter(item.get("category", "") for item in all_items_flat)
    )

    raw_html = template.render(
        companies=companies,
        secondary_digest=secondary_digest or {},
        run_date=run_dt_uk.strftime("%A %d %B %Y"),
        run_time=run_dt_uk.strftime("%H:%M"),
        n_companies=n_companies,
        n_items=n_items,
        profile_changes=profile_changes or [],
        jobs_changes=jobs_changes or [],
        company_owners=company_owners or {},
        category_counts=category_counts,
    )

    # Inline CSS for maximum email client compatibility
    inlined = premailer.transform(
        raw_html,
        remove_classes=False,
        strip_important=False,
    )
    return inlined


def _send_sendgrid(
    html_body: str,
    subject: str,
    sender: str,
    recipient: str,
    api_key: str,
) -> None:
    """Send via SendGrid Web API (no extra SDK required — plain HTTP)."""
    import ssl
    import urllib.error
    import urllib.request
    import json as _json
    import certifi

    recipients = [r.strip() for r in recipient.split(",") if r.strip()]
    payload = _json.dumps({
        "personalizations": [{"to": [{"email": r} for r in recipients]}],
        "from": {"email": sender},
        "subject": subject,
        "content": [{"type": "text/html", "value": html_body}],
    }).encode()

    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"SendGrid HTTP {exc.code}: {body}") from exc

    if status not in (200, 202):
        raise RuntimeError(f"SendGrid returned unexpected status {status}")
    logger.info("Email sent via SendGrid to %s (status %s)", recipient, status)


def _send_smtp(
    html_body: str,
    subject: str,
    sender: str,
    recipient: str,
) -> None:
    """
    SMTP fallback — uses SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD
    environment variables (defaults to Gmail TLS on port 587).
    """
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", sender)
    password = os.environ.get("SMTP_PASSWORD", "")

    recipients = [r.strip() for r in recipient.split(",") if r.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.ehlo()
        server.starttls(context=context)
        server.login(user, password)
        server.sendmail(sender, recipients, msg.as_string())

    logger.info("Email sent via SMTP (%s:%s) to %s", host, port, recipient)


def send_digest(
    companies: dict[str, list[dict]],
    run_dt: Optional[datetime] = None,
    dry_run: bool = False,
    template_dir: str = ".",
    secondary_digest: dict | None = None,
    profile_changes: list | None = None,
    jobs_changes: list | None = None,
    company_owners: dict | None = None,
) -> str:
    """
    Render the digest and (unless dry_run) send it.

    *companies* maps company name → list of item dicts, each with:
        summary, url, source, published (str), category

    Returns the rendered HTML string.
    """
    if run_dt is None:
        run_dt = datetime.now(timezone.utc)

    html = _render_html(
        companies, run_dt,
        template_dir=template_dir,
        secondary_digest=secondary_digest,
        profile_changes=profile_changes,
        jobs_changes=jobs_changes,
        company_owners=company_owners,
    )

    run_dt_uk = run_dt.astimezone(UK_TZ)
    subject = f"Ashcombe News Digest — {run_dt_uk.strftime('%d %b %Y')}"

    if dry_run:
        logger.info("[DRY RUN] Would send: %s", subject)
        print("\n" + "=" * 72)
        print(f"[DRY RUN] Subject: {subject}")
        print(f"[DRY RUN] HTML length: {len(html):,} chars")
        print("=" * 72)
        return html

    recipient = os.environ["RECIPIENT_EMAIL"]
    sender = os.environ["SENDER_EMAIL"]
    sendgrid_key = os.environ.get("SENDGRID_API_KEY", "")

    if sendgrid_key:
        _send_sendgrid(html, subject, sender, recipient, sendgrid_key)
    else:
        logger.info("SENDGRID_API_KEY not set — falling back to SMTP")
        _send_smtp(html, subject, sender, recipient)

    return html


def send_failure_alert(error: Exception, run_dt: datetime) -> None:
    """Send a plain-text failure notification when the tracker crashes."""
    recipient = os.environ.get("RECIPIENT_EMAIL", "")
    sender = os.environ.get("SENDER_EMAIL", "")
    sendgrid_key = os.environ.get("SENDGRID_API_KEY", "")

    if not recipient or not sender:
        logger.error("Cannot send failure alert — RECIPIENT_EMAIL or SENDER_EMAIL not set")
        return

    run_dt_uk = run_dt.astimezone(UK_TZ)
    subject = f"Ashcombe Tracker FAILED — {run_dt_uk.strftime('%d %b %Y %H:%M')}"
    html = f"""<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;padding:32px;max-width:600px;">
  <div style="background:#7f1d1d;padding:20px 24px;border-radius:8px 8px 0 0;">
    <h2 style="color:#fff;margin:0;">Ashcombe Tracker — Run Failed</h2>
  </div>
  <div style="background:#fff;border:1px solid #fca5a5;border-top:none;padding:24px;border-radius:0 0 8px 8px;">
    <p><strong>Time:</strong> {run_dt_uk.strftime('%d %b %Y at %H:%M')} UK time</p>
    <p><strong>Error:</strong> {type(error).__name__}: {error}</p>
    <p style="color:#6b7280;font-size:13px;margin-top:24px;">
      No digest was sent. Check tracker.log for full details.
    </p>
  </div>
</body></html>"""

    try:
        if sendgrid_key:
            _send_sendgrid(html, subject, sender, recipient, sendgrid_key)
        else:
            _send_smtp(html, subject, sender, recipient)
        logger.info("Failure alert sent to %s", recipient)
    except Exception as alert_exc:
        logger.error("Failed to send failure alert: %s", alert_exc)


# ---------------------------------------------------------------------------
# Dry-run smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    from datetime import timezone

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Synthetic digest data
    sample_companies = {
        "Balfour Beatty": [
            {
                "summary": "Balfour Beatty wins £450m HS2 tunnelling contract.",
                "url": "https://example.com/balfour-hs2",
                "source": "Construction News",
                "published": "01 May 2026",
                "category": "contract_win",
            },
            {
                "summary": "Balfour Beatty appoints new Chief Financial Officer.",
                "url": "https://example.com/balfour-cfo",
                "source": "The Times",
                "published": "02 May 2026",
                "category": "senior_hire",
            },
        ],
        "Serco Group": [
            {
                "summary": "Serco partners with NHS to deliver digital health platform.",
                "url": "https://example.com/serco-nhs",
                "source": "Health Service Journal",
                "published": "02 May 2026",
                "category": "partnership",
            },
        ],
        "Atkins Global": [
            {
                "summary": "Atkins Global raises £200m growth equity round.",
                "url": "https://example.com/atkins-funding",
                "source": "City A.M.",
                "published": "30 Apr 2026",
                "category": "funding_ma",
            },
        ],
    }

    html = send_digest(sample_companies, dry_run=True)

    # Write preview for inspection
    out = Path("digest_preview.html")
    out.write_text(html, encoding="utf-8")
    print(f"[DRY RUN] Preview written to {out.resolve()}")
    sys.exit(0)
