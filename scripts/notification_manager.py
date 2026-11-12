#!/usr/bin/env python3
"""Notification manager for Rocket Hunter scan completion.

Supports:
1. Mailtrap Email API (using official `mailtrap` Python SDK)
2. Email via standard SMTP (Mailtrap live/sandbox SMTP, Gmail, Outlook, custom SMTP)
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("notification_manager")

if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = ROOT / "rocket-hunter-data"
CONFIG_FILE = DATA_DIR / "notification_config.json"


def _clean_token(token: str | None) -> str:
    """Clean token string by removing leading/trailing angle brackets, quotes and whitespace."""
    if not token:
        return ""
    t = str(token).strip()
    if t.startswith("<") and t.endswith(">"):
        t = t[1:-1].strip()
    return t.strip("'\"")


def get_default_config() -> dict[str, Any]:
    # Check environment variable for token fallback
    env_token = os.environ.get("MAILTRAP_TOKEN") or os.environ.get("MAILTRAP_API_TOKEN") or ""
    return {
        "enabled": True,
        "email_enabled": True,
        "recipient_email": "moradov22@gmail.com",
        "sender_email": "hello@demomailtrap.co",
        "sender_name": "Rocket Hunter",
        "mailtrap_token": _clean_token(env_token) or "7bf24ba1c51739a5a0a3646cc4e865a3",
        "mailtrap_domain": "demomailtrap.co",
        "smtp_server": "live.smtp.mailtrap.io",
        "smtp_port": 587,
        "smtp_user": "api",
        "smtp_password": "",
        "use_tls": True,
        "last_notified_run_id": "",
        "notify_on_status": ["COMPLETED", "FAILED", "INTERRUPTED"]
    }


def load_config() -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        default_cfg = get_default_config()
        save_config(default_cfg)
        return default_cfg
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        cfg = get_default_config()
        cfg.update(data)
        # Ensure token is cleaned of any brackets
        if "mailtrap_token" in cfg:
            cfg["mailtrap_token"] = _clean_token(cfg["mailtrap_token"])
        return cfg
    except Exception as exc:
        logger.error(f"Error loading notification config: {exc}")
        return get_default_config()


def save_config(cfg: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if "mailtrap_token" in cfg:
        cfg["mailtrap_token"] = _clean_token(cfg["mailtrap_token"])
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def send_mailtrap_sdk_email(
    cfg: dict[str, Any],
    subject: str,
    body_text: str,
    body_html: str | None = None
) -> tuple[bool, str]:
    """Send email using official Mailtrap Python SDK client."""
    token = _clean_token(
        os.environ.get("MAILTRAP_TOKEN")
        or os.environ.get("MAILTRAP_API_TOKEN")
        or cfg.get("mailtrap_token")
    )
    if not token:
        return False, "Mailtrap API token not configured. Set MAILTRAP_TOKEN env var or configure in settings."

    recipient_email = (cfg.get("recipient_email") or "moradov22@gmail.com").strip()
    if not recipient_email:
        return False, "Recipient email address is required"

    sender_email = (cfg.get("sender_email") or "hello@demomailtrap.co").strip()
    sender_name = (cfg.get("sender_name") or "Rocket Hunter").strip()

    try:
        import mailtrap as mt

        mail_kwargs: dict[str, Any] = {
            "sender": mt.Address(email=sender_email, name=sender_name),
            "to": [mt.Address(email=recipient_email)],
            "subject": subject,
            "text": body_text,
            "category": "Scan Notification",
        }
        if body_html:
            mail_kwargs["html"] = body_html

        mail = mt.Mail(**mail_kwargs)
        client = mt.MailtrapClient(token=token)
        response = client.send(mail)
        return True, f"Mailtrap email sent successfully (response: {response})"
    except ImportError:
        return False, "Mailtrap SDK not installed (run `pip install mailtrap`)"
    except Exception as exc:
        err = f"Mailtrap API error: {exc}"
        logger.warning(err)
        return False, err


def send_smtp_email(
    cfg: dict[str, Any],
    subject: str,
    body_text: str,
    body_html: str | None = None
) -> tuple[bool, str]:
    """Send email via standard SMTP (supports live.smtp.mailtrap.io or custom SMTP)."""
    recipient = (cfg.get("recipient_email") or "").strip()
    smtp_server = (cfg.get("smtp_server") or "live.smtp.mailtrap.io").strip()
    smtp_port = int(cfg.get("smtp_port") or 587)
    smtp_user = (cfg.get("smtp_user") or "api").strip()
    # Password can be the mailtrap token if using mailtrap live SMTP
    smtp_password = (
        cfg.get("smtp_password")
        or _clean_token(os.environ.get("MAILTRAP_TOKEN") or cfg.get("mailtrap_token"))
    ).strip()
    use_tls = bool(cfg.get("use_tls", True))

    if not recipient or not smtp_server:
        return False, "Recipient email or SMTP server not configured"

    sender_email = (cfg.get("sender_email") or "hello@demomailtrap.co").strip()
    sender_name = (cfg.get("sender_name") or "Rocket Hunter").strip()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{sender_name} <{sender_email}>"
    msg["To"] = recipient

    msg.attach(MIMEText(body_text, "plain"))
    if body_html:
        msg.attach(MIMEText(body_html, "html"))

    try:
        server = smtplib.SMTP(smtp_server, smtp_port, timeout=15)
        if use_tls:
            server.starttls()
        if smtp_user and smtp_password:
            server.login(smtp_user, smtp_password)
        server.sendmail(sender_email, [recipient], msg.as_string())
        server.quit()
        return True, "Email sent via SMTP successfully"
    except Exception as exc:
        err = f"SMTP error: {exc}"
        logger.error(err)
        return False, err


def send_email_notification(
    cfg: dict[str, Any],
    subject: str,
    body_text: str,
    body_html: str | None = None
) -> tuple[bool, str]:
    """Send email using Mailtrap SDK first, falling back to SMTP if needed."""
    # 1. Try Mailtrap SDK
    ok, msg = send_mailtrap_sdk_email(cfg, subject, body_text, body_html)
    if ok:
        return True, msg

    logger.info(f"Mailtrap SDK delivery returned: {msg}. Attempting SMTP fallback...")
    # 2. Fallback to standard SMTP
    ok_smtp, msg_smtp = send_smtp_email(cfg, subject, body_text, body_html)
    if ok_smtp:
        return True, f"Mailtrap SDK failed ({msg}), but SMTP fallback succeeded: {msg_smtp}"

    return False, f"Email delivery failed: SDK error: {msg} | SMTP error: {msg_smtp}"


def send_scan_completed_notification(
    run_id: str,
    status: str = "COMPLETED",
    started_ist: str = "—",
    finished_ist: str = "—",
    duration_str: str = "—",
    ranked_count: int = 0,
    actionable_count: int = 0,
    top_candidate: str = "—",
    version: str = "v4.26.6"
) -> dict[str, Any]:
    cfg = load_config()
    if not cfg.get("enabled"):
        return {"status": "DISABLED", "message": "Notifications are disabled in settings"}

    allowed_statuses = cfg.get("notify_on_status", ["COMPLETED", "FAILED", "INTERRUPTED"])
    if status not in allowed_statuses:
        return {"status": "SKIPPED", "message": f"Notifications not enabled for status: {status}"}

    # Deduplicate: do not send repeated alerts for same run_id
    if cfg.get("last_notified_run_id") == run_id:
        return {"status": "ALREADY_NOTIFIED", "message": f"Run {run_id} already notified"}

    subject = f"🚀 Rocket Hunter Scan {status}: {run_id}"
    body_text = f"""Rocket Hunter — Scan Complete Notification
=========================================
Run ID: {run_id}
Status: {status}
Version: {version}
Started (IST): {started_ist}
Finished (IST): {finished_ist}
Duration: {duration_str}

Results Summary:
- Verified Actionable Opportunities: {actionable_count}
- Total Ranked Breakout Candidates: {ranked_count}
- Top Rocket Pick: {top_candidate}

Open your Rocket Hunter application or Forward 24h Watchlist to review opportunities:
http://127.0.0.1:8765/forward-24h-rocket.html

Logs can be viewed at: https://mailtrap.io/sending/email_logs
"""

    body_html = f"""<!doctype html>
<html>
<head><style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background: #0b1120; color: #f8fafc; padding: 24px; }}
.card {{ background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 28px; max-width: 620px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
.badge {{ background: #10b981; color: #fff; padding: 4px 12px; border-radius: 6px; font-weight: bold; font-size: 13px; letter-spacing: 0.5px; }}
.badge-fail {{ background: #ef4444; color: #fff; padding: 4px 12px; border-radius: 6px; font-weight: bold; font-size: 13px; }}
.green {{ color: #4ade80; font-weight: bold; }}
.accent {{ color: #38bdf8; font-weight: 600; }}
.btn {{ display: inline-block; background: #0284c7; color: #ffffff !important; text-decoration: none; padding: 12px 22px; border-radius: 8px; font-weight: bold; margin-top: 20px; box-shadow: 0 4px 12px rgba(2,132,199,0.4); }}
.footer-text {{ color: #94a3b8; font-size: 12px; margin-top: 24px; border-top: 1px solid #334155; padding-top: 14px; }}
</style></head>
<body>
<div class="card">
  <h2>🚀 Rocket Hunter Scan Complete</h2>
  <p><span class="{'badge' if status == 'COMPLETED' else 'badge-fail'}">{status}</span> &nbsp; Run ID: <code>{run_id}</code></p>
  <hr style="border:0; border-top: 1px solid #334155; margin: 16px 0;">
  <p><strong>Finished (Israel Time):</strong> <span class="accent">{finished_ist}</span></p>
  <p><strong>Duration:</strong> {duration_str}</p>
  <p><strong>Verified Actionable Opportunities:</strong> <span class="green">{actionable_count}</span></p>
  <p><strong>Total Ranked Candidates:</strong> {ranked_count}</p>
  <p><strong>Top Rocket Pick:</strong> <strong>{top_candidate}</strong></p>
  <a href="http://127.0.0.1:8765/forward-24h-rocket.html" class="btn">🔭 Open Forward 24h Watchlist</a>
  <div class="footer-text">
    Sent via Mailtrap. Check email delivery logs at <a href="https://mailtrap.io/sending/email_logs" style="color:#38bdf8;">mailtrap.io/sending/email_logs</a>
  </div>
</div>
</body></html>"""

    results: dict[str, Any] = {"run_id": run_id}

    # 1. Native Windows Desktop Toast + Audible Chime Alert
    try:
        import subprocess
        import winsound
        # Play immediate audible notification chime
        try:
            winsound.PlaySound("Notification.Default", winsound.SND_ALIAS | winsound.SND_ASYNC)
        except Exception:
            try:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except Exception:
                pass

        # Trigger native Windows Toast Notification
        ps_script = ROOT / "scripts" / "windows_toast.ps1"
        if ps_script.exists():
            toast_title = f"Rocket Hunter — Scan {status}!"
            toast_msg = f"{actionable_count} Actionable Pick(s). Top: {top_candidate}"
            subprocess.Popen(
                ["powershell", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", str(ps_script), "-Title", toast_title, "-Message", toast_msg],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            results["toast"] = {"success": True}
    except Exception as exc:
        logger.debug(f"Desktop notification failed: {exc}")
        results["toast"] = {"success": False, "error": str(exc)}

    # 2. Email Delivery via Mailtrap SDK / SMTP
    if cfg.get("email_enabled", True):
        ok, msg = send_email_notification(cfg, subject, body_text, body_html)
        results["email"] = {"success": ok, "message": msg}

    cfg["last_notified_run_id"] = run_id
    save_config(cfg)
    return results


def test_notification() -> dict[str, Any]:
    cfg = load_config()
    subject = "🚀 Rocket Hunter — Mailtrap Test Notification"
    body_text = """Rocket Hunter — Test Email
===========================
Congrats! Mailtrap email integration is active and working properly.
Sent from: hello@demomailtrap.co
Check your logs at: https://mailtrap.io/sending/email_logs
"""
    body_html = """<!doctype html>
<html><body style="background:#0f172a;color:#f8fafc;padding:24px;font-family:sans-serif;">
<div style="background:#1e293b;border:1px solid #334155;border-radius:10px;padding:24px;max-width:550px;">
  <h2 style="color:#38bdf8;margin-top:0;">🚀 Rocket Hunter — Mailtrap Test</h2>
  <p>Congrats! Your Mailtrap email delivery is <strong>active and verified</strong>.</p>
  <p>Sender Domain: <code>demomailtrap.co</code></p>
  <p>View sent logs at: <a href="https://mailtrap.io/sending/email_logs" style="color:#38bdf8;">mailtrap.io/sending/email_logs</a></p>
</div>
</body></html>"""

    results: dict[str, Any] = {}
    if cfg.get("email_enabled", True):
        ok, msg = send_email_notification(cfg, subject, body_text, body_html)
        results["email"] = {"success": ok, "message": msg}
    else:
        results["email"] = {"success": False, "message": "Email notification is disabled"}

    return results


if __name__ == "__main__":
    import sys
    res = test_notification()
    print(json.dumps(res, indent=2))
    if not res.get("email", {}).get("success"):
        sys.exit(1)
