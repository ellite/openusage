"""Instance-wide SMTP relay, used for password-reset emails. Ported from
candlr's app/notifiers/email.py - stdlib only, no extra dependency."""
import os
import smtplib
from email.mime.text import MIMEText

SMTP_ADDRESS = os.getenv("SMTP_ADDRESS")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_ENCRYPTION = os.getenv("SMTP_ENCRYPTION", "tls")  # "tls", "ssl", or "none"
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
FROM_EMAIL = os.getenv("FROM_EMAIL")


class EmailError(Exception):
    """Raised for any SMTP failure; message is user-facing."""


def send(to_email: str, subject: str, body: str) -> None:
    if not SMTP_ADDRESS:
        raise EmailError("SMTP is not configured on this instance")

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL or SMTP_USERNAME or "openusage@localhost"
    msg["To"] = to_email

    try:
        if SMTP_ENCRYPTION == "ssl":
            server = smtplib.SMTP_SSL(SMTP_ADDRESS, SMTP_PORT, timeout=10)
        else:
            server = smtplib.SMTP(SMTP_ADDRESS, SMTP_PORT, timeout=10)
        with server:
            if SMTP_ENCRYPTION == "tls":
                server.starttls()
            if SMTP_USERNAME:
                server.login(SMTP_USERNAME, SMTP_PASSWORD or "")
            server.sendmail(msg["From"], [to_email], msg.as_string())
    except Exception as e:
        raise EmailError(f"SMTP error: {e}") from e
