"""Outgoing mail. One function, three ways of delivering (HELPDESK_MAIL_MODE):

    outbox   write the email as a .eml file under <data_dir>/outbox. Nothing is
             sent. This is the default, and the only mode outside production.
    webhook  POST the email as JSON to a URL. A Power Automate flow receives it
             and sends it from a real mailbox.
    smtp     hand the email to a mail server (an "SMTP relay").
    folder   write the email as a .json file into a OneDrive-synced folder. A
             Power Automate "when a file is created" flow sends it and deletes
             the file. Needs the server PC's OneDrive to be syncing.

The rest of the app only calls send(); it never knows which one is in use.
"""
import json
import re
import secrets
import smtplib
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from . import config

OUTBOX_KEEP = 200
TIMEOUT = 20

_EMAIL = re.compile(r"^[^@\s,;<>\"]+@[^@\s,;<>\"]+\.[^@\s,;<>\"]+$")


class MailError(Exception):
    """Delivery failed. The message is safe to show to root and to log: it never
    contains the webhook URL or a password."""


@dataclass
class Message:
    to: str
    subject: str
    html: str
    text: str


def valid_address(value: str) -> bool:
    """Deliberately simple. It mostly exists to keep whitespace and line breaks
    out of a value that ends up in an email header."""
    return bool(_EMAIL.match(value)) and len(value) <= 254


def mode() -> str:
    """The delivery mode actually in effect."""
    return config.settings.mail_mode if config.ENV == "production" else "outbox"


def describe() -> str:
    """One line for the root page: where mail goes right now."""
    current = mode()
    if current == "outbox":
        why = ("" if config.ENV == "production"
               else f" (forced, because this is the {config.ENV} environment)")
        return f"Outbox: emails are written to files and not sent{why}."
    if current == "webhook":
        return ("Webhook: emails are handed to the Power Automate flow."
                if config.settings.mail_webhook_url
                else "Webhook, but HELPDESK_MAIL_WEBHOOK_URL is not set. Sending will fail.")
    if current == "folder":
        waiting = pending()
        if waiting is None:
            return ("Drop folder, but HELPDESK_MAIL_FOLDER is not set or the folder is "
                    "missing. Sending will fail.")
        return ("Drop folder: emails are written as files for the Power Automate flow to "
                f"send. {waiting} waiting to be picked up"
                + (" (if this number doesn't go down, OneDrive isn't syncing: sign in "
                   "to the server PC)." if waiting else "."))
    return (f"SMTP: emails are handed to {config.settings.smtp_host}."
            if config.settings.smtp_host
            else "SMTP, but HELPDESK_SMTP_HOST is not set. Sending will fail.")


def send(message: Message) -> None:
    """Deliver one email, or raise MailError."""
    if not valid_address(message.to):
        raise MailError(f"not a valid address: {message.to!r}")
    {"outbox": _outbox, "webhook": _webhook, "smtp": _smtp,
     "folder": _folder}[mode()](message)


def _mime(message: Message) -> EmailMessage:
    mime = EmailMessage()
    mime["From"] = config.settings.mail_from
    mime["To"] = message.to
    mime["Subject"] = message.subject
    mime["Date"] = formatdate(localtime=False)
    mime["Message-ID"] = make_msgid(domain="helpdesk.local")
    mime.set_content(message.text)
    mime.add_alternative(message.html, subtype="html")
    return mime


def _file_stem(message: Message) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    who = re.sub(r"[^a-z0-9]+", "_", message.to.lower()).strip("_")[:40]
    return f"{stamp}-{who}-{secrets.token_hex(3)}"


def _folder(message: Message) -> None:
    """One .json file per email. The flow that sends it also deletes it, so a
    file still sitting here is an email that hasn't gone out yet."""
    folder = config.settings.mail_folder
    if folder is None:
        raise MailError("HELPDESK_MAIL_FOLDER is not set")
    if not folder.is_dir():
        raise MailError("the drop folder does not exist (is it mounted into the container?)")
    body = json.dumps({"to": message.to, "subject": message.subject,
                       "html": message.html}, ensure_ascii=True)
    try:
        # One write under the final name. Don't "write a temp name, then rename":
        # OneDrive can upload the temp file first, and a rename in the cloud is
        # not a "file created" event, so the flow would never fire.
        (folder / f"{_file_stem(message)}.json").write_text(body, encoding="utf-8")
    except OSError as exc:
        raise MailError(f"could not write to the drop folder: {exc.strerror or exc}") from None


def pending() -> int | None:
    """Emails written in folder mode that the flow hasn't sent yet."""
    folder = config.settings.mail_folder
    if mode() != "folder" or folder is None or not folder.is_dir():
        return None
    return len(list(folder.glob("*.json")))


def _outbox(message: Message) -> None:
    folder = config.OUTBOX_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{_file_stem(message)}.eml"
    path.write_bytes(_mime(message).as_bytes())
    for old in sorted(folder.glob("*.eml"), reverse=True)[OUTBOX_KEEP:]:
        old.unlink(missing_ok=True)


def _webhook(message: Message) -> None:
    url = config.settings.mail_webhook_url
    if not url:
        raise MailError("HELPDESK_MAIL_WEBHOOK_URL is not set")
    body = json.dumps({"to": message.to, "subject": message.subject,
                       "html": message.html, "text": message.text}).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    handlers = []
    if config.settings.mail_proxy:
        proxy = config.settings.mail_proxy
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    try:
        with urllib.request.build_opener(*handlers).open(request, timeout=TIMEOUT):
            pass
    except urllib.error.HTTPError as exc:
        raise MailError(f"the flow answered HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise MailError(f"could not reach the flow: {reason}") from None


def _smtp(message: Message) -> None:
    host = config.settings.smtp_host
    if not host:
        raise MailError("HELPDESK_SMTP_HOST is not set")
    try:
        with smtplib.SMTP(host, config.settings.smtp_port, timeout=TIMEOUT) as server:
            if config.settings.smtp_starttls:
                server.starttls()
            if config.settings.smtp_username:
                server.login(config.settings.smtp_username, config.settings.smtp_password)
            server.send_message(_mime(message))
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"mail server refused or was unreachable: {exc}") from None
