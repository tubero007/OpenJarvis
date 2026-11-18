"""Amount and date extraction from free text, plus formatting.

Anything extracted from email text is a best guess and is always labelled as
an estimate by the caller.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, Optional

SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
_AMOUNT = re.compile(
    r"(?P<sym>[$€£¥])\s?(?P<num>\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)"
    r"|(?P<code>USD|EUR|GBP|CAD|AUD)\s?(?P<num2>\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)",
)
_DUEISH = re.compile(r"\b(amount due|total due|balance|minimum (?:payment )?due|payment of|due|charged|renews? (?:at|for)|total)\b", re.I)

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DATE_WORDS = re.compile(
    r"\b(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,?\s+(?P<year>\d{4}))?\b",
    re.I,
)
_DATE_ISO = re.compile(r"\b(?P<y>20\d\d)-(?P<m>\d\d)-(?P<d>\d\d)\b")
_DATE_US = re.compile(r"\b(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{2,4}))?\b")
_DUE_CONTEXT = re.compile(r"\b(due|by|before|renews?|charged|deadline|expires?|ends?)\b", re.I)


def find_amount(text: str) -> Optional[Dict[str, Any]]:
    """Prefer an amount near 'due/balance/charged'; else the first amount."""
    best = None
    for m in _AMOUNT.finditer(text):
        num = (m.group("num") or m.group("num2")).replace(",", "")
        try:
            value = float(num)
        except ValueError:
            continue
        if value <= 0 or value > 1_000_000:
            continue
        currency = SYMBOLS.get(m.group("sym") or "", m.group("code") or "USD")
        near = _DUEISH.search(text[max(0, m.start() - 60): m.start()])
        cand = {"value": round(value, 2), "currency": currency}
        if near:
            return cand
        best = best or cand
    return best


def _mk(y: int, m: int, d: int) -> Optional[date]:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def find_date(text: str, now: datetime) -> Optional[date]:
    """A date that follows due/renews/by-style wording, inferring the year."""
    today = now.date()
    candidates = []
    for m in _DATE_WORDS.finditer(text):
        mon = MONTHS.get(m.group("mon").lower()[:3])
        year = int(m.group("year")) if m.group("year") else today.year
        d = _mk(year, mon, int(m.group("day")))
        if d and not m.group("year") and d < today and (today - d).days > 31:
            d = _mk(year + 1, mon, int(m.group("day")))
        candidates.append((m.start(), d))
    for m in _DATE_ISO.finditer(text):
        candidates.append((m.start(), _mk(int(m.group("y")), int(m.group("m")), int(m.group("d")))))
    for m in _DATE_US.finditer(text):
        y = m.group("y")
        year = (2000 + int(y) if y and len(y) == 2 else int(y)) if y else today.year
        d = _mk(year, int(m.group("m")), int(m.group("d")))
        if d and not y and d < today and (today - d).days > 31:
            d = _mk(year + 1, int(m.group("m")), int(m.group("d")))
        candidates.append((m.start(), d))
    candidates = [(pos, d) for pos, d in candidates if d]
    candidates.sort()
    for pos, d in candidates:
        if _DUE_CONTEXT.search(text[max(0, pos - 30): pos]):
            return d
    return None


def fmt(value: Optional[float], currency: str = "USD") -> str:
    if value is None:
        return ""
    sym = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CAD": "CA$", "AUD": "A$"}.get(currency, currency + " ")
    if value == int(value):
        return "%s%s" % (sym, "{:,}".format(int(value)))
    return "%s%s" % (sym, "{:,.2f}".format(value))
