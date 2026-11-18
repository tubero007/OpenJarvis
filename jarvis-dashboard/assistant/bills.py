"""Your local bills and subscriptions list (bills.toml).

Recurring items are projected forward from the date you entered; projected
dates are labelled as estimates ("confirm with the biller").
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None  # type: ignore

TEMPLATE = '''# Jarvis bills & subscriptions — read-only list, edited by you.
# Jarvis only READS this file to remind you. It cannot pay or cancel anything.
#
#   amount    : number (leave out if unknown)
#   estimate  : true if the amount varies (utilities, credit cards) — shown as "≈ … estimate"
#   repeat    : "none" | "weekly" | "monthly" | "quarterly" | "yearly"
#   due/renews: the next (or any past) date, YYYY-MM-DD; recurring dates are projected forward
currency = "USD"

[[bills]]
name = "Rent"
amount = 1450.00
due = 2026-12-01
repeat = "monthly"
autopay = false
note = "Landlord portal"

[[bills]]
name = "Electricity"
amount = 85.00
estimate = true
due = 2026-11-24
repeat = "monthly"
autopay = true

[[subscriptions]]
name = "Spotify Premium"
amount = 11.99
renews = 2026-11-28
repeat = "monthly"

[[subscriptions]]
name = "iCloud+ 200 GB"
amount = 2.99
renews = 2026-12-03
repeat = "monthly"
'''

STEP_MONTHS = {"monthly": 1, "quarterly": 3, "yearly": 12, "annual": 12, "annually": 12}


def _add_months(d: date, months: int, day: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    return date(y, m + 1, min(day, calendar.monthrange(y, m + 1)[1]))


def next_occurrence(anchor: date, repeat: str, today: date) -> Tuple[date, bool]:
    """Return (date, projected?). Past recurring dates roll forward."""
    repeat = (repeat or "none").lower()
    if anchor >= today or repeat in ("none", "once", ""):
        return anchor, False
    if repeat == "weekly":
        weeks = -(-(today - anchor).days // 7)
        return anchor + timedelta(weeks=weeks), True
    step = STEP_MONTHS.get(repeat)
    if not step:
        return anchor, False
    n = 0
    d = anchor
    while d < today and n < 1200:
        n += 1
        d = _add_months(anchor, step * n, anchor.day)
    return d, True


def _as_date(v: Any) -> Optional[date]:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v.strip())
        except ValueError:
            return None
    return None


def load(path: Path, today: date) -> Dict[str, Any]:
    """Parse bills.toml into normalized items. Raises ValueError on bad TOML."""
    if tomllib is None:
        raise ValueError("Python 3.11+ is needed to read bills.toml")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    currency = str(data.get("currency", "USD"))
    items: List[Dict[str, Any]] = []
    problems: List[str] = []
    for kind, key, date_key in (("bill", "bills", "due"), ("subscription", "subscriptions", "renews")):
        for i, raw in enumerate(data.get(key, []) or []):
            if not isinstance(raw, dict) or not raw.get("name"):
                problems.append("%s #%d has no name" % (key, i + 1))
                continue
            anchor = _as_date(raw.get(date_key) or raw.get("due") or raw.get("renews"))
            if anchor is None:
                problems.append("%s: missing or invalid %s date" % (raw["name"], date_key))
                continue
            repeat = str(raw.get("repeat", "none"))
            when, projected = next_occurrence(anchor, repeat, today)
            amount = raw.get("amount")
            try:
                amount = float(amount) if amount is not None else None
            except (TypeError, ValueError):
                problems.append("%s: amount isn't a number" % raw["name"])
                amount = None
            items.append({
                "kind": kind,
                "name": str(raw["name"])[:80],
                "amount": amount,
                "currency": str(raw.get("currency", currency)),
                "amount_estimate": bool(raw.get("estimate", False)),
                "date": when,
                "date_projected": projected,
                "repeat": repeat,
                "autopay": bool(raw.get("autopay", False)),
                "note": str(raw.get("note", ""))[:120],
            })
    return {"items": items, "problems": problems}
