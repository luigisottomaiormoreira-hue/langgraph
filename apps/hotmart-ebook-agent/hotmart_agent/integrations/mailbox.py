"""Caixa de entrada de suporte: leitura via IMAP e envio via SMTP.

O agente nunca apaga e-mail. Ele lê os não-lidos, responde/rascunha e marca com
a label/flag de tratado — assim um humano sempre consegue auditar o que houve.
"""

from __future__ import annotations

import email
import imaplib
import logging
import smtplib
from datetime import datetime
from email.header import decode_header, make_header
from email.message import EmailMessage
from typing import Any, Protocol, runtime_checkable

from ..config import Settings
from ..errors import ConfigError, IntegrationError
from ..models import InboundEmail

logger = logging.getLogger(__name__)


@runtime_checkable
class MailboxClient(Protocol):
    def fetch_unread(self, limit: int = 50) -> list[InboundEmail]: ...

    def send_reply(
        self, to: str, subject: str, body: str, in_reply_to: str
    ) -> dict[str, Any]: ...

    def save_draft(self, to: str, subject: str, body: str) -> dict[str, Any]: ...

    def mark_handled(self, email_id: str, label: str) -> None: ...


class ImapSmtpMailbox:
    """Implementação padrão IMAP+SMTP — funciona com Gmail, Zoho, Titan etc."""

    def __init__(self, settings: Settings) -> None:
        creds = settings.credentials
        if not creds.has_mailbox():
            raise ConfigError("IMAP_HOST/IMAP_USER/IMAP_PASSWORD não configurados")
        self.settings = settings
        self.creds = creds

    def _imap(self) -> imaplib.IMAP4_SSL:
        try:
            conn = imaplib.IMAP4_SSL(self.creds.imap_host)
            conn.login(self.creds.imap_user, self.creds.imap_password)
            return conn
        except (imaplib.IMAP4.error, OSError) as exc:
            raise IntegrationError("imap", str(exc), retryable=True) from exc

    @staticmethod
    def _decode(value: str | None) -> str:
        if not value:
            return ""
        try:
            return str(make_header(decode_header(value)))
        except Exception:  # header malformado não pode derrubar o lote
            return value

    @staticmethod
    def _plain_body(msg: email.message.Message) -> str:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True) or b""
                    return payload.decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
            return ""
        payload = msg.get_payload(decode=True) or b""
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")

    def fetch_unread(self, limit: int = 50) -> list[InboundEmail]:
        conn = self._imap()
        out: list[InboundEmail] = []
        try:
            conn.select("INBOX")
            _, data = conn.search(None, "UNSEEN")
            ids = (data[0] or b"").split()[-limit:]
            for raw_id in ids:
                _, payload = conn.fetch(raw_id, "(RFC822)")
                if not payload or not isinstance(payload[0], tuple):
                    continue
                msg = email.message_from_bytes(payload[0][1])
                try:
                    received = email.utils.parsedate_to_datetime(msg.get("Date", ""))
                except (TypeError, ValueError):
                    received = datetime.utcnow()
                out.append(
                    InboundEmail(
                        id=raw_id.decode(),
                        sender=email.utils.parseaddr(msg.get("From", ""))[1],
                        subject=self._decode(msg.get("Subject")),
                        body=self._plain_body(msg)[:8000],
                        received_at=received,
                        thread_id=msg.get("Message-ID"),
                    )
                )
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return out

    def send_reply(
        self, to: str, subject: str, body: str, in_reply_to: str
    ) -> dict[str, Any]:
        msg = EmailMessage()
        msg["From"] = self.creds.smtp_user or self.creds.imap_user
        msg["To"] = to
        msg["Subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg.set_content(body)
        try:
            with smtplib.SMTP(self.creds.smtp_host, self.creds.smtp_port) as smtp:
                smtp.starttls()
                smtp.login(
                    self.creds.smtp_user or self.creds.imap_user,
                    self.creds.smtp_password or self.creds.imap_password,
                )
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise IntegrationError("smtp", str(exc), retryable=True) from exc
        return {"status": "sent", "to": to}

    def save_draft(self, to: str, subject: str, body: str) -> dict[str, Any]:
        msg = EmailMessage()
        msg["From"] = self.creds.smtp_user or self.creds.imap_user
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        conn = self._imap()
        try:
            conn.append(
                "Drafts",
                "",
                imaplib.Time2Internaldate(datetime.now().timestamp()),
                msg.as_bytes(),
            )
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return {"status": "drafted", "to": to}

    def mark_handled(self, email_id: str, label: str) -> None:
        conn = self._imap()
        try:
            conn.select("INBOX")
            conn.store(email_id, "+FLAGS", "\\Seen")
            conn.store(email_id, "+X-GM-LABELS", label)
        except imaplib.IMAP4.error as exc:  # servidor sem suporte a labels
            logger.debug("mark_handled ignorado: %s", exc)
        finally:
            try:
                conn.logout()
            except Exception:
                pass


def build_mailbox_client(settings: Settings) -> MailboxClient:
    from .fakes import FakeMailboxClient

    if settings.credentials.has_mailbox() and not settings.dry_run:
        return ImapSmtpMailbox(settings)
    logger.info("mailbox: usando cliente simulado (dry-run ou credencial ausente)")
    return FakeMailboxClient(settings)
