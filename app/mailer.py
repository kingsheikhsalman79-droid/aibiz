"""Gmail sending and reading over SMTP/IMAP with a Google App Password (standard library only)."""
import imaplib
import re
import smtplib
import ssl
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr


class MailError(Exception):
    pass


EMAIL_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")
_SKIP_SENDERS = ("noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "postmaster", "notifications@")


def _clean(value, field):
    value = (value or "").strip()
    if "\r" in value or "\n" in value:
        raise MailError(f"Invalid {field}.")
    return value


def check_login(address, app_password):
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=20) as s:
            s.login(address, app_password)
    except smtplib.SMTPAuthenticationError:
        raise MailError(
            "Gmail rejected the login. Turn on 2-Step Verification, create an App Password and use it here (not your normal password)."
        ) from None
    except (smtplib.SMTPException, OSError):
        raise MailError("Could not reach Gmail. Please try again later.") from None


def send_email(address, app_password, to, subject, body, in_reply_to=None):
    to = _clean(to, "recipient")
    subject = _clean(subject, "subject")
    if not EMAIL_RE.match(to):
        raise MailError("Invalid recipient address.")
    msg = EmailMessage()
    msg["From"] = address
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    if in_reply_to:
        ref = _clean(in_reply_to, "reference")
        msg["In-Reply-To"] = ref
        msg["References"] = ref
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as s:
            s.login(address, app_password)
            s.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise MailError("Gmail login failed. Reconnect Gmail with a fresh App Password.") from None
    except (smtplib.SMTPException, OSError):
        raise MailError("Gmail could not send the message. Please try again later.") from None


def _decode(value):
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return value or ""


def _body_text(msg):
    plain = html = None
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        ctype = part.get_content_type()
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if ctype == "text/plain" and plain is None:
            plain = text
        elif ctype == "text/html" and html is None:
            html = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\n{3,}", "\n\n", (plain or html or "")).strip()[:4000]


def parse_mail(uid, raw):
    msg = message_from_bytes(raw)
    name, addr = parseaddr(msg.get("From", ""))
    auto = (msg.get("Auto-Submitted", "no").lower() != "no") or (msg.get("Precedence", "").lower() in ("bulk", "list", "junk")) or bool(msg.get("List-Id"))
    return {
        "uid": uid,
        "from_name": _decode(name),
        "from_addr": addr.lower(),
        "subject": _decode(msg.get("Subject", ""))[:300],
        "body": _body_text(msg),
        "message_id": (msg.get("Message-ID") or "").strip(),
        "auto": auto,
    }


def should_skip(mail, own_address):
    """Never answer ourselves, robots, newsletters or bounce messages (prevents reply loops)."""
    addr = mail["from_addr"]
    if not addr or addr == (own_address or "").lower():
        return True
    if mail.get("auto"):
        return True
    return any(tag in addr for tag in _SKIP_SENDERS)


def _login_imap(address, app_password):
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=ssl.create_default_context(), timeout=30)
        M.login(address, app_password)
        return M
    except imaplib.IMAP4.error:
        raise MailError("Gmail IMAP login failed. Enable IMAP in Gmail settings and use an App Password.") from None
    except OSError:
        raise MailError("Could not reach Gmail. Please try again later.") from None


def fetch_unseen(address, app_password, limit=5):
    M = _login_imap(address, app_password)
    out = []
    try:
        M.select("INBOX")
        _, data = M.uid("search", None, "UNSEEN")
        uids = (data[0] or b"").split()[-limit:]
        for uid in uids:
            _, parts = M.uid("fetch", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if raw:
                out.append(parse_mail(uid.decode(), raw))
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return out


def mark_seen(address, app_password, uids):
    if not uids:
        return
    M = _login_imap(address, app_password)
    try:
        M.select("INBOX")
        for uid in uids:
            M.uid("store", uid, "+FLAGS", "\\Seen")
    finally:
        try:
            M.logout()
        except Exception:
            pass
