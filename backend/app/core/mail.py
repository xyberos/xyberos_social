import logging
import smtplib
from email.message import EmailMessage

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def send_password_reset_email(recipient: str, reset_url: str) -> None:
    send_account_email(
        recipient,
        "Reset your Social+ password",
        "Use this link to reset your password. It expires in 30 minutes:",
        reset_url,
    )


def send_account_email(recipient: str, subject: str, instructions: str, url: str) -> None:
    settings = get_settings()
    if not settings.smtp_host:
        logger.error("Account email was not sent because SMTP_HOST is unset")
        return

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from
    message["To"] = recipient
    message.set_content(
        f"{instructions}\n\n{url}\n\nIf you did not request this, you can ignore this email."
    )

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
            if settings.smtp_starttls:
                smtp.starttls()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password or "")
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException):
        logger.exception("Password reset email delivery failed")
