"""邮件发送：log 模式（本地/联调）与 SMTP 模式（生产）。

``LogMailer`` 只用于 ``MAIL_MODE=log``：它会把完整正文（含确认链接）打到日志，
方便本地联调；生产必须用 smtp，绝不能在日志里出现 token。
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from typing import Protocol

from .config import Settings
from .content import RenderedEmail

logger = logging.getLogger("email.mailer")


class MailDeliveryError(RuntimeError):
    """SMTP 发送失败；上层把请求标记为 failed。"""


class Mailer(Protocol):
    def send(self, to: str, rendered: RenderedEmail) -> None: ...


class LogMailer:
    def send(self, to: str, rendered: RenderedEmail) -> None:
        # 仅开发模式：这是 token 唯一会出现在日志里的地方。
        logger.warning(
            "MAIL_MODE=log（仅开发）to=%s subject=%s\n%s",
            to,
            rendered.subject,
            rendered.text or rendered.html or "",
        )


class SmtpMailer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _message(self, to: str, rendered: RenderedEmail) -> EmailMessage:
        message = EmailMessage()
        from_name = self.settings.mail_from_name.strip()
        message["From"] = (
            formataddr((from_name, self.settings.mail_from))
            if from_name
            else self.settings.mail_from
        )
        message["To"] = to
        message["Subject"] = rendered.subject
        if rendered.text is not None:
            message.set_content(rendered.text)
            if rendered.html:
                message.add_alternative(rendered.html, subtype="html")
        else:
            message.set_content(rendered.html or "", subtype="html")
        return message

    def send(self, to: str, rendered: RenderedEmail) -> None:
        message = self._message(to, rendered)
        settings = self.settings
        try:
            if settings.smtp_ssl:
                smtp = smtplib.SMTP_SSL(
                    settings.smtp_host,
                    settings.smtp_port,
                    timeout=settings.smtp_timeout,
                )
            else:
                smtp = smtplib.SMTP(
                    settings.smtp_host,
                    settings.smtp_port,
                    timeout=settings.smtp_timeout,
                )
            with smtp:
                smtp.ehlo()
                if settings.smtp_starttls and not settings.smtp_ssl:
                    smtp.starttls()
                    smtp.ehlo()
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, settings.smtp_password)
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            raise MailDeliveryError(f"{type(exc).__name__}: {exc}") from exc


def build_mailer(settings: Settings) -> Mailer:
    if settings.mail_mode == "smtp":
        return SmtpMailer(settings)
    logger.warning("MAIL_MODE=log：邮件不会真正发送，确认链接会写入日志（仅开发）")
    return LogMailer()
