"""Sending email through Resend, plus the shared HTML shell."""

from __future__ import annotations

import html as html_lib
import logging

import markdown as md

from app import config

log = logging.getLogger(__name__)


class EmailError(Exception):
    pass


def recipients(session) -> list[str]:
    from app import settings_store

    extra = settings_store.get(session, "recipients") or []
    return extra or [config.EMAIL_TO]


def send(to: list[str], subject: str, html: str, text: str = "", scheduled_at: str = "") -> str:
    """Returns the Resend message id. Without an API key, logs and returns ''.

    scheduled_at (ISO 8601) lets us build the newsletter early and have it land on the minute.
    """
    if not config.RESEND_API_KEY:
        log.warning("RESEND_API_KEY not set; not sending %r", subject)
        return ""
    import resend

    resend.api_key = config.RESEND_API_KEY
    params = {"from": config.EMAIL_FROM, "to": to, "subject": subject, "html": html}
    if text:
        params["text"] = text
    if scheduled_at:
        params["scheduled_at"] = scheduled_at
    try:
        resp = resend.Emails.send(params)
    except Exception as e:
        raise EmailError(str(e)) from e
    return (resp or {}).get("id", "")


def markdown_to_html(text: str) -> str:
    return md.markdown(text, extensions=["footnotes", "sane_lists", "smarty"])


SHELL = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title></head>
<body style="margin:0;padding:0;background:#f4f1ea;">
<span style="display:none;max-height:0;overflow:hidden;">{preheader}</span>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f1ea;">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;background:#ffffff;border:1px solid #e3ddd0;">
<tr><td style="padding:28px 32px 8px 32px;font-family:Georgia,'Times New Roman',serif;">
<div style="font-size:13px;letter-spacing:1px;text-transform:uppercase;color:#8a6d3b;">{kicker}</div>
</td></tr>
<tr><td class="content" style="padding:0 32px 24px 32px;font-family:Georgia,'Times New Roman',serif;font-size:17px;line-height:1.6;color:#1d1d1d;">
{body}
</td></tr>
<tr><td style="padding:16px 32px 28px 32px;border-top:1px solid #eee;font-family:-apple-system,Helvetica,Arial,sans-serif;font-size:12px;line-height:1.5;color:#777;">
{footer}
</td></tr>
</table></td></tr></table></body></html>"""


def wrap(title: str, body_html: str, kicker: str, footer_html: str, preheader: str = "") -> str:
    # Light inline styling so links and headings look decent in Gmail.
    body_html = (
        body_html.replace("<a ", '<a style="color:#0b5cad;" ')
        .replace("<h2>", '<h2 style="font-size:22px;line-height:1.3;margin:28px 0 8px;">')
        .replace("<h3>", '<h3 style="font-size:18px;margin:24px 0 6px;">')
        .replace("<blockquote>", '<blockquote style="margin:12px 0;padding:4px 16px;border-left:3px solid #d8cfbd;color:#444;">')
        .replace('<div class="footnote">', '<div class="footnote" style="font-size:14px;color:#555;">')
    )
    return SHELL.format(
        title=html_lib.escape(title),
        preheader=html_lib.escape(preheader),
        kicker=html_lib.escape(kicker),
        body=body_html,
        footer=footer_html,
    )


def footer(extra: str = "") -> str:
    board = f'<a href="{config.BASE_URL}/" style="color:#777;">Open the board</a>'
    inbox = f"Forward newsletters to <b>feeds@{html_lib.escape(config.INBOUND_DOMAIN)}</b> to feed the agents."
    return f"{board} · {inbox}" + (f"<br>{extra}" if extra else "")
