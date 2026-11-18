"""Minimal, dependency-free iCalendar (RFC 5545) reader.

Enough for Canvas feeds and calendar "secret address" feeds: VEVENT/VTODO,
folded lines, escaped text, TZID / UTC / floating / all-day times, and the
common RRULE subset (DAILY/WEEKLY/MONTHLY/YEARLY with INTERVAL, COUNT, UNTIL,
BYDAY for weekly), EXDATE and RECURRENCE-ID overrides.

Feed text is untrusted data: it is parsed, never executed or rendered as HTML.
"""

from __future__ import annotations

import calendar as _cal
import ipaddress
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_FEED_BYTES = 8 * 1024 * 1024
WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
# Windows-style zone names some servers emit.
_TZ_ALIASES = {
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
    "GMT Standard Time": "Europe/London",
}


class FeedError(RuntimeError):
    pass


def _local_tz() -> tzinfo:
    return datetime.now().astimezone().tzinfo or timezone.utc


# ------------------------------------------------------------------ fetching

def normalize_url(url: str) -> str:
    url = url.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    loopback = host == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if parsed.scheme == "https" or (parsed.scheme == "http" and loopback):
        return url
    raise FeedError("Feed URL must start with https:// (or webcal://)")


def redact(url: str) -> str:
    """Show a feed URL without its secret path/query (for status and logs)."""
    try:
        parsed = urllib.parse.urlparse(normalize_url(url))
        return "%s://%s/…" % (parsed.scheme, parsed.hostname)
    except FeedError:
        return "(invalid URL)"


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        normalize_url(newurl)  # raises on a downgrade to http
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, timeout: float = 20.0) -> str:
    url = normalize_url(url)
    opener = urllib.request.build_opener(_HttpsOnlyRedirects)
    req = urllib.request.Request(url, headers={"User-Agent": "JarvisDashboard/1.0 (calendar reader)"})
    try:
        with opener.open(req, timeout=timeout) as resp:  # noqa: S310 - validated scheme
            data = resp.read(MAX_FEED_BYTES + 1)
    except FeedError:
        raise
    except Exception as exc:  # noqa: BLE001
        reason = getattr(exc, "code", None) or getattr(exc, "reason", None) or exc
        raise FeedError("couldn't download feed (%s)" % reason) from None
    if len(data) > MAX_FEED_BYTES:
        raise FeedError("feed is larger than 8 MB")
    text = data.decode("utf-8", errors="replace")
    if "BEGIN:VCALENDAR" not in text[:2000].upper():
        raise FeedError("that URL didn't return an iCalendar feed")
    return text


# ------------------------------------------------------------------ parsing

def _unfold(text: str) -> List[str]:
    lines: List[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _split_prop(line: str) -> Tuple[str, Dict[str, str], str]:
    # NAME;PARAM=VAL;PARAM="V:AL":VALUE  (colon inside quotes is not the separator)
    in_q = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif ch == ":" and not in_q:
            head, value = line[:i], line[i + 1:]
            break
    else:
        return line.upper(), {}, ""
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def _text(value: str) -> str:
    out = re.sub(r"\\([nN])", "\n", value)
    out = re.sub(r"\\([,;\\])", r"\1", out)
    # strip control characters other than newline/tab
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", out).strip()


def _zone(name: Optional[str]) -> tzinfo:
    if not name:
        return _local_tz()
    name = _TZ_ALIASES.get(name, name)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return _local_tz()


def parse_dt(value: str, params: Dict[str, str]) -> Tuple[datetime, bool]:
    """Return (aware datetime, all_day)."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        d = datetime.strptime(value[:8], "%Y%m%d")
        return d.replace(tzinfo=_local_tz()), True
    utc = value.endswith("Z")
    fmt = "%Y%m%dT%H%M%S" if len(value.rstrip("Z")) >= 15 else "%Y%m%dT%H%M"
    d = datetime.strptime(value.rstrip("Z")[:15], fmt)
    if utc:
        return d.replace(tzinfo=timezone.utc), False
    return d.replace(tzinfo=_zone(params.get("TZID"))), False


def parse(text: str) -> List[Dict[str, Any]]:
    """Parse VEVENT/VTODO components into dicts."""
    items: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    depth_other = 0
    for line in _unfold(text):
        if not line:
            continue
        name, params, value = _split_prop(line)
        if name == "BEGIN":
            kind = value.strip().upper()
            if cur is None and kind in ("VEVENT", "VTODO"):
                cur = {"kind": kind, "exdates": [], "categories": []}
            elif cur is not None:
                depth_other += 1  # e.g. VALARM inside an event
            continue
        if name == "END":
            if cur is not None and depth_other:
                depth_other -= 1
            elif cur is not None and value.strip().upper() in ("VEVENT", "VTODO"):
                if "start" in cur or "due" in cur:
                    items.append(cur)
                cur = None
            continue
        if cur is None or depth_other:
            continue
        if name in ("DTSTART", "DTEND", "DUE", "RECURRENCE-ID"):
            try:
                dt, all_day = parse_dt(value, params)
            except ValueError:
                continue
            key = {"DTSTART": "start", "DTEND": "end", "DUE": "due", "RECURRENCE-ID": "recurrence_id"}[name]
            cur[key] = dt
            if key in ("start", "due"):
                cur["all_day"] = all_day
        elif name == "EXDATE":
            for v in value.split(","):
                try:
                    cur["exdates"].append(parse_dt(v, params)[0])
                except ValueError:
                    pass
        elif name in ("SUMMARY", "DESCRIPTION", "LOCATION", "UID", "STATUS"):
            cur[name.lower()] = _text(value)
        elif name == "URL":
            cur["url"] = value.strip()
        elif name == "RRULE":
            cur["rrule"] = dict(p.split("=", 1) for p in value.split(";") if "=" in p)
        elif name == "CATEGORIES":
            cur["categories"] += [_text(c) for c in value.split(",")]
    return items


# ------------------------------------------------------------------ recurrence

def _add_months(d: datetime, months: int, day: int) -> datetime:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    return d.replace(year=y, month=m, day=min(day, _cal.monthrange(y, m)[1]))


def _occurrences(ev: Dict[str, Any], start: datetime, end: datetime) -> Iterable[datetime]:
    first: datetime = ev.get("start") or ev["due"]
    rule = ev.get("rrule")
    if not rule:
        yield first
        return
    freq = rule.get("FREQ", "").upper()
    interval = max(1, int(rule.get("INTERVAL", "1") or 1))
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    until = None
    if rule.get("UNTIL"):
        try:
            until = parse_dt(rule["UNTIL"], {})[0]
        except ValueError:
            until = None
    byday = [WEEKDAYS[d[-2:]] for d in rule.get("BYDAY", "").split(",") if d[-2:] in WEEKDAYS]
    exdates = {x.astimezone(timezone.utc).replace(second=0) for x in ev.get("exdates", [])}
    produced = 0
    n = 0
    cursor = first
    while n < 5000:
        n += 1
        if freq == "WEEKLY" and byday:
            week_start = cursor - timedelta(days=cursor.weekday())
            candidates = sorted(week_start + timedelta(days=wd) for wd in byday)
        else:
            candidates = [cursor]
        for occ in candidates:
            if occ < first:
                continue
            if until and occ > until:
                return
            if count is not None and produced >= count:
                return
            produced += 1
            if occ.astimezone(timezone.utc).replace(second=0) in exdates:
                continue
            if occ > end:
                return
            yield occ
        if freq == "DAILY":
            cursor = cursor + timedelta(days=interval)
        elif freq == "WEEKLY":
            cursor = cursor + timedelta(weeks=interval)
        elif freq == "MONTHLY":
            cursor = _add_months(first, interval * n, first.day)
        elif freq == "YEARLY":
            cursor = _add_months(first, 12 * interval * n, first.day)
        else:
            return


def expand(items: List[Dict[str, Any]], start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """Concrete occurrences overlapping [start, end], sorted by time."""
    overridden = {
        (it.get("uid"), it["recurrence_id"].astimezone(timezone.utc))
        for it in items if it.get("recurrence_id")
    }
    out = []
    for ev in items:
        if (ev.get("status") or "").upper() == "CANCELLED":
            continue
        base_start = ev.get("start") or ev.get("due")
        duration = (ev["end"] - ev["start"]) if ev.get("end") and ev.get("start") else timedelta(0)
        for occ in _occurrences(ev, start, end):
            if ev.get("rrule") and (ev.get("uid"), occ.astimezone(timezone.utc)) in overridden:
                continue
            occ_end = occ + duration
            if occ_end < start and occ < start:
                continue
            if occ > end:
                continue
            inst = dict(ev)
            inst["when"] = occ
            inst["until"] = occ_end
            inst["recurring"] = bool(ev.get("rrule"))
            inst.setdefault("base_start", base_start)
            out.append(inst)
    out.sort(key=lambda e: e["when"])
    return out


# ------------------------------------------------------------------ Canvas

_COURSE = re.compile(r"\s*\[([^\]]+)\]\s*$")


def canvas_item(ev: Dict[str, Any]) -> Dict[str, Any]:
    """Split a Canvas feed entry into title / course and classify it."""
    summary = ev.get("summary") or "(untitled)"
    course = ""
    m = _COURSE.search(summary)
    if m:
        course = m.group(1)
        summary = summary[: m.start()].strip()
    uid = (ev.get("uid") or "").lower()
    is_deadline = (
        "assignment" in uid or "quiz" in uid or "discussion" in uid
        or ev["kind"] == "VTODO"
        or bool(re.search(r"\b(due|deadline|submit)\b", (ev.get("description") or "")[:300], re.I))
    )
    return {"title": summary, "course": course, "is_deadline": is_deadline}


def safe_link(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    url = url.strip()
    return url if re.match(r"^https://[^\s\"'<>]+$", url) else None
