"""Read-only email reader (IMAP) and importance ranking.

Safety properties:
* The connection is wrapped in ReadOnlyIMAP, which only exposes LOGIN,
  CAPABILITY, EXAMINE (read-only SELECT), UID SEARCH, UID FETCH and LOGOUT.
  There is no code path that can STORE flags, COPY/MOVE, EXPUNGE, APPEND or
  send mail.
* Bodies are fetched with BODY.PEEK so messages are not marked as read.
* Message content is treated as untrusted data: it is reduced to plain text,
  truncated, stripped of control characters, and never rendered as HTML.
"""

from __future__ import annotations

import email
import email.header
import email.utils
import html
import imaplib
import re
import ssl
from datetime import date, datetime, timedelta, timezone
from email.message import Message
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import money

PROVIDER_HOSTS = {
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "outlook.com": "outlook.office365.com",
    "hotmail.com": "outlook.office365.com",
    "live.com": "outlook.office365.com",
    "icloud.com": "imap.mail.me.com",
    "me.com": "imap.mail.me.com",
    "mac.com": "imap.mail.me.com",
    "yahoo.com": "imap.mail.yahoo.com",
    "fastmail.com": "imap.fastmail.com",
}

KEYWORDS = re.compile(
    r"\b(due|deadline|payment|pay(?:ment)? reminder|invoice|bill|statement|overdue|past due|"
    r"renew(?:al|s|ing)?|subscription|trial ends?|will be charged|action required|"
    r"exam|midterm|final|grade[sd]?|interview|appointment|reminder|confirm(?:ation)?|"
    r"security alert|verify)\b",
    re.I,
)
PAYMENT_WORDS = re.compile(r"\b(due|payment|pay|bill|statement|invoice|balance|autopay|amount owed|minimum)\b", re.I)
RENEWAL_WORDS = re.compile(r"\b(renew(?:al|s|ing)?|subscription|trial ends?|will be charged|auto-?renew)\b", re.I)


class ReadOnlyIMAP:
    """Only the IMAP commands needed to read. Anything else is unavailable."""

    _ALLOWED_UID = {"SEARCH", "FETCH"}

    def __init__(self, conn: imaplib.IMAP4) -> None:
        self._c = conn

    @property
    def capabilities(self) -> Tuple[str, ...]:
        return tuple(c.decode() if isinstance(c, bytes) else c for c in self._c.capabilities)

    def login(self, user: str, password: str) -> None:
        self._c.login(user, password)

    def examine(self, mailbox: str = "INBOX") -> int:
        status, data = self._c.select(mailbox, readonly=True)  # IMAP EXAMINE
        if status != "OK":
            raise imaplib.IMAP4.error("couldn't open %s read-only" % mailbox)
        try:
            return int(data[0])
        except (TypeError, ValueError, IndexError):
            return 0

    def uid(self, command: str, *args: str):
        if command.upper() not in self._ALLOWED_UID:
            raise PermissionError("IMAP %s is not allowed (read-only)" % command)
        if command.upper() == "FETCH" and re.search(r"BODY\[", " ".join(args), re.I):
            raise PermissionError("use BODY.PEEK so messages stay unread")
        return self._c.uid(command, *args)

    def logout(self) -> None:
        try:
            self._c.logout()
        except Exception:  # noqa: BLE001
            pass


def default_host(address: str) -> str:
    domain = address.rsplit("@", 1)[-1].lower()
    return PROVIDER_HOSTS.get(domain, "imap." + domain)


def connect(host: str, port: int = 993, security: str = "tls", timeout: float = 30) -> imaplib.IMAP4:
    if security == "tls":
        return imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=timeout)
    if security == "starttls":
        conn = imaplib.IMAP4(host, port, timeout=timeout)
        conn.starttls(ssl_context=ssl.create_default_context())
        return conn
    if security == "plain" and host in ("127.0.0.1", "localhost"):
        return imaplib.IMAP4(host, port, timeout=timeout)  # local test servers only
    raise ValueError("IMAP security must be 'tls' or 'starttls'")


# ------------------------------------------------------------------ parsing

def _decode(raw: Optional[str]) -> str:
    if not raw:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(raw)))
    except Exception:  # noqa: BLE001
        return raw


def clean_text(text: str, limit: int) -> str:
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁦-⁩]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _body_text(msg: Message) -> str:
    plain, htmlpart = "", ""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() != "text" or part.get_filename():
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        if part.get_content_subtype() == "plain" and not plain:
            plain = text
        elif part.get_content_subtype() == "html" and not htmlpart:
            htmlpart = text
    if plain:
        return plain
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", htmlpart)
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


def parse_message(raw: bytes, flags: List[str], labels: List[str], uid: str) -> Dict[str, Any]:
    msg = email.message_from_bytes(raw)
    name, addr = email.utils.parseaddr(_decode(msg.get("From")))
    try:
        when = email.utils.parsedate_to_datetime(msg.get("Date"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        when = None
    body = _body_text(msg)
    return {
        "uid": uid,
        "from_name": clean_text(name or addr, 80),
        "from_addr": addr.lower(),
        "subject": clean_text(_decode(msg.get("Subject")) or "(no subject)", 160),
        "date": when,
        "snippet": clean_text(body, 280),
        "body_for_extraction": clean_text(body, 3000),
        "unread": "\\Seen" not in flags,
        "flagged": "\\Flagged" in flags,
        "important_label": any(l.strip('"').lstrip("\\") == "Important" for l in labels),
        "bulk": bool(msg.get("List-Unsubscribe")) or (msg.get("Precedence", "").lower() in ("bulk", "list")),
    }


def score(m: Dict[str, Any], important_senders: List[str]) -> Tuple[int, List[str]]:
    reasons = []
    s = 0
    if m["flagged"]:
        s += 3; reasons.append("flagged")
    if m["important_label"]:
        s += 3; reasons.append("marked important")
    sender = m["from_addr"]
    if any(p and (sender == p.lower() or sender.endswith(p.lower())) for p in important_senders):
        s += 3; reasons.append("important sender")
    kw = KEYWORDS.search(m["subject"])
    if kw:
        s += 2; reasons.append("mentions “%s”" % kw.group(0).lower())
    if m["unread"]:
        s += 1; reasons.append("unread")
    if m["bulk"] and not (m["flagged"] or kw):
        s -= 3
    return s, reasons


_META = re.compile(rb"UID (\d+)|FLAGS \(([^)]*)\)|X-GM-LABELS \(([^)]*)\)")


def _meta(blob: bytes, out: Dict[str, Any]) -> None:
    for m in _META.finditer(blob):
        if m.group(1):
            out["uid"] = m.group(1).decode()
        if m.group(2) is not None:
            out["flags"] = m.group(2).decode(errors="replace").split()
        if m.group(3) is not None:
            out["labels"] = re.findall(r'"[^"]*"|\S+', m.group(3).decode(errors="replace"))


def fetch_recent(
    address: str,
    password: str,
    host: str,
    port: int = 993,
    security: str = "tls",
    days: int = 3,
    limit: int = 40,
    opener: Callable[..., imaplib.IMAP4] = connect,
) -> Dict[str, Any]:
    imap = ReadOnlyIMAP(opener(host, port, security))
    try:
        imap.login(address, password)
        total = imap.examine("INBOX")
        since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
        status, data = imap.uid("SEARCH", "SINCE", since)
        uids = (data[0] or b"").split() if status == "OK" and data else []
        uids = uids[-limit:]
        messages = []
        if uids:
            items = "(UID FLAGS BODY.PEEK[]<0.24000>%s)" % (
                " X-GM-LABELS" if "X-GM-EXT-1" in imap.capabilities else "")
            status, data = imap.uid("FETCH", b",".join(uids).decode(), items)
            cur: Optional[Dict[str, Any]] = None
            for part in data or []:
                if isinstance(part, tuple):
                    cur = {"flags": [], "labels": [], "uid": ""}
                    _meta(part[0], cur)
                    cur["raw"] = part[1]
                    messages.append(cur)
                elif isinstance(part, bytes) and cur is not None:
                    _meta(part, cur)  # some servers send FLAGS after the literal
        parsed = [parse_message(m["raw"], m["flags"], m["labels"], m["uid"]) for m in messages if m.get("raw")]
        return {"messages": parsed, "mailbox_total": total}
    finally:
        imap.logout()


def triage(messages: List[Dict[str, Any]], important_senders: List[str], now: datetime,
           top: int = 6) -> Dict[str, List[Dict[str, Any]]]:
    """Pick important messages and pull out bill/renewal hints (estimates)."""
    ranked = []
    payments, renewals = [], []
    for m in messages:
        s, reasons = score(m, important_senders)
        m = dict(m, score=s, reasons=reasons)
        text = m["subject"] + " " + m["body_for_extraction"]
        amount = money.find_amount(text)
        due = money.find_date(text, now)
        if amount and RENEWAL_WORDS.search(text):
            renewals.append({"message": m, "amount": amount, "date": due})
        elif amount and PAYMENT_WORDS.search(text):
            payments.append({"message": m, "amount": amount, "date": due})
        if s >= 3:
            ranked.append(m)
    ranked.sort(key=lambda m: (-m["score"], -(m["date"].timestamp() if m["date"] else 0)))
    return {"important": ranked[:top], "payments": payments, "renewals": renewals}
