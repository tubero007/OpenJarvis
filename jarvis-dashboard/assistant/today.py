"""Builds the Today view from read-only sources.

Every item carries its source and when that source was last checked, and any
estimated amount or date says why it's an estimate.
"""

from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from . import bills as bills_mod
from . import ical, mail, paths, secrets

DEFAULT_WINDOWS = {"deadline_days": 7, "payment_days": 10, "renewal_days": 14}
CACHE_TTL = 15 * 60

SETUP_HINTS = {
    "bills": "./jarvis-dashboard bills",
    "canvas": "./jarvis-dashboard setup canvas",
    "email": "./jarvis-dashboard setup email",
    "calendar": "./jarvis-dashboard setup calendar",
    "apple_calendar": "./jarvis-dashboard setup apple-calendar",
}


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _id(*parts: Any) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def _day_start(d: date, tz) -> datetime:  # noqa: ANN001
    return datetime(d.year, d.month, d.day, tzinfo=tz)


class SourceResult:
    def __init__(self, sid: str, label: str, kind: str) -> None:
        self.id, self.label, self.kind = sid, label, kind
        self.ok = False
        self.configured = True
        self.error: Optional[str] = None
        self.checked_at: Optional[datetime] = None
        self.detail = ""
        self.data: Any = None
        self.count = 0

    def public(self) -> Dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "kind": self.kind,
            "configured": self.configured, "ok": self.ok, "error": self.error,
            "checked_at": _iso(self.checked_at), "detail": self.detail, "count": self.count,
            "setup_hint": None if self.configured else SETUP_HINTS.get(self.kind),
        }


class TodayService:
    def __init__(self, now_fn: Callable[[], datetime] = lambda: datetime.now().astimezone()) -> None:
        self._now = now_fn
        self._lock = threading.Lock()
        self._cache: Dict[str, SourceResult] = {}
        self._cache_time = 0.0

    # ---------------------------------------------------------------- sources

    def _source_specs(self, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        specs = [{"id": "bills", "label": "Bills & subscriptions list", "kind": "bills"}]
        canvas = cfg.get("canvas") or {}
        specs.append({"id": "canvas", "label": canvas.get("label") or "Canvas", "kind": "canvas",
                      "enabled": canvas.get("enabled", False)})
        for cal in cfg.get("calendars") or []:
            cid = cal["id"] if cal["id"].startswith("cal:") else "cal:" + cal["id"]
            specs.append({"id": cid, "label": cal.get("label") or cal["id"],
                          "kind": "calendar", "enabled": cal.get("enabled", True), "cal": cal})
        if (cfg.get("apple_calendar") or {}).get("enabled"):
            specs.append({"id": "apple_calendar", "label": "Mac Calendar", "kind": "apple_calendar",
                          "enabled": True, "cfg": cfg["apple_calendar"]})
        email = cfg.get("email") or {}
        specs.append({"id": "email", "label": "Email (%s)" % email["address"] if email.get("address") else "Email", "kind": "email",
                      "enabled": email.get("enabled", False), "cfg": email})
        return specs

    def _run(self, spec: Dict[str, Any], now: datetime) -> SourceResult:
        r = SourceResult(spec["id"], spec["label"], spec["kind"])
        try:
            if spec["kind"] == "bills":
                path = paths.bills_path()
                if not path.exists():
                    r.configured = False
                    return r
                r.data = bills_mod.load(path, now.date())
                mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
                r.detail = "%s · you last edited it %s" % (path, mtime.strftime("%b %d, %I:%M %p").replace(" 0", " "))
                if r.data["problems"]:
                    r.detail += " · problems: " + "; ".join(r.data["problems"])
                r.count = len(r.data["items"])
            elif spec["kind"] in ("canvas", "calendar"):
                if not spec.get("enabled"):
                    r.configured = False
                    return r
                key = "ical:canvas" if spec["kind"] == "canvas" else "ical:" + spec["id"]
                url = secrets.get(key)
                if not url:
                    r.configured = False
                    r.error = "feed URL not found in Keychain"
                    return r
                text = ical.fetch(url)
                items = ical.parse(text)
                window_end = now + timedelta(days=max(DEFAULT_WINDOWS.values()) + 1)
                r.data = ical.expand(items, now - timedelta(days=1), window_end)
                r.detail = "iCal feed %s" % ical.redact(url)
                r.count = len(r.data)
            elif spec["kind"] == "apple_calendar":
                from openjarvis.connectors.apple_calendar import AppleCalendarConnector

                conn = AppleCalendarConnector(db_path=spec["cfg"].get("db_path", ""), days_ahead=2, days_behind=1)
                docs = list(conn.sync())
                events = []
                for d in docs:
                    try:
                        start = datetime.fromisoformat(d.metadata["start"])
                    except (KeyError, ValueError):
                        continue
                    if start.tzinfo is None:
                        start = start.astimezone()
                    events.append({"summary": d.metadata.get("summary") or d.title, "when": start,
                                   "all_day": d.metadata.get("all_day", False),
                                   "location": d.metadata.get("location") or "",
                                   "calendar": d.metadata.get("calendar") or ""})
                r.data = events
                r.detail = "macOS Calendar database (read-only, on this Mac)"
                r.count = len(events)
            elif spec["kind"] == "email":
                ecfg = spec["cfg"]
                if not spec.get("enabled") or not ecfg.get("address"):
                    r.configured = False
                    return r
                password = secrets.get("imap:" + ecfg["address"])
                if not password:
                    r.configured = False
                    r.error = "app password not found in Keychain"
                    return r
                got = mail.fetch_recent(
                    ecfg["address"], password, ecfg.get("host") or mail.default_host(ecfg["address"]),
                    int(ecfg.get("port", 993)), ecfg.get("security", "tls"),
                    days=int(ecfg.get("lookback_days", 3)), limit=int(ecfg.get("max_messages", 40)),
                )
                r.data = mail.triage(got["messages"], ecfg.get("important_senders") or [], now)
                r.detail = "INBOX via IMAP (read-only) · last %s days · %d messages scanned" % (
                    ecfg.get("lookback_days", 3), len(got["messages"]))
                r.count = len(got["messages"])
            r.ok = True
        except Exception as exc:  # noqa: BLE001 - one source failing must not break Today
            r.error = str(exc)[:300] or exc.__class__.__name__
        finally:
            r.checked_at = self._now()
        return r

    def refresh(self, force: bool = False) -> Dict[str, SourceResult]:
        with self._lock:
            if not force and self._cache and time.time() - self._cache_time < CACHE_TTL:
                return self._cache
            cfg = paths.load_sources()
            now = self._now()
            specs = self._source_specs(cfg)
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(lambda s: self._run(s, now), specs))
            self._cache = {r.id: r for r in results}
            self._cache_time = time.time()
            return self._cache

    # ---------------------------------------------------------------- view

    def build(self, force: bool = False) -> Dict[str, Any]:
        results = self.refresh(force)
        cfg = paths.load_sources()
        win = dict(DEFAULT_WINDOWS, **(cfg.get("windows") or {}))
        now = self._now()
        tz = now.tzinfo
        today = now.date()
        sod = _day_start(today, tz)
        eod = sod + timedelta(days=1)
        sections: Dict[str, List[Dict[str, Any]]] = {k: [] for k in ("schedule", "deadlines", "payments", "renewals", "email")}

        def src(r: SourceResult, detail: str = "") -> Dict[str, Any]:
            return {"id": r.id, "label": r.label, "kind": r.kind,
                    "checked_at": _iso(r.checked_at), "detail": detail or r.detail}

        for r in results.values():
            if not r.ok:
                continue
            if r.kind in ("canvas", "calendar"):
                for ev in r.data:
                    when: datetime = ev["when"]
                    if r.kind == "canvas":
                        c = ical.canvas_item(ev)
                        if c["is_deadline"]:
                            if sod - timedelta(days=1) <= when <= now + timedelta(days=win["deadline_days"]):
                                sections["deadlines"].append({
                                    "id": _id(r.id, ev.get("uid"), when),
                                    "title": c["title"], "subtitle": c["course"],
                                    "when": _iso(when), "all_day": ev.get("all_day", False),
                                    "overdue": when < now,
                                    "link": ical.safe_link(ev.get("url")),
                                    "source": src(r, "Canvas calendar feed — it shows due dates, not whether you've submitted"),
                                })
                            continue
                        title, sub = c["title"], c["course"]
                    else:
                        title, sub = ev.get("summary") or "(untitled)", ev.get("location") or ""
                    if sod <= when < eod or (ev.get("all_day") and when.date() == today):
                        sections["schedule"].append({
                            "id": _id(r.id, ev.get("uid"), when), "title": title, "subtitle": sub,
                            "when": _iso(when), "end": _iso(ev.get("until")),
                            "all_day": ev.get("all_day", False), "source": src(r),
                        })
            elif r.kind == "apple_calendar":
                for ev in r.data:
                    if sod <= ev["when"] < eod:
                        sections["schedule"].append({
                            "id": _id(r.id, ev["summary"], ev["when"]), "title": ev["summary"],
                            "subtitle": ev.get("location") or ev.get("calendar", ""),
                            "when": _iso(ev["when"]), "all_day": ev["all_day"], "source": src(r),
                        })
            elif r.kind == "bills":
                for b in r.data["items"]:
                    days = (b["date"] - today).days
                    limit = win["payment_days"] if b["kind"] == "bill" else win["renewal_days"]
                    if days < -3 or days > limit:
                        continue
                    when = _day_start(b["date"], tz)
                    notes = []
                    if b["amount_estimate"]:
                        notes.append("you marked this amount as an estimate")
                    if b["date_projected"]:
                        notes.append("date projected from your %s schedule — confirm with the biller" % b["repeat"])
                    sections["payments" if b["kind"] == "bill" else "renewals"].append({
                        "id": _id(r.id, b["name"], b["date"]), "title": b["name"],
                        "subtitle": " · ".join(x for x in ("autopay" if b["autopay"] else "", b["note"]) if x),
                        "when": _iso(when), "all_day": True, "overdue": days < 0,
                        "amount": None if b["amount"] is None else {
                            "value": b["amount"], "currency": b["currency"], "estimate": b["amount_estimate"]},
                        "date_estimate": b["date_projected"],
                        "estimate_note": "; ".join(notes) or None,
                        "source": src(r, "your bills list (bills.toml)"),
                    })
            elif r.kind == "email":
                for m in r.data["important"]:
                    sections["email"].append({
                        "id": _id(r.id, m["uid"]), "title": m["subject"], "subtitle": m["from_name"],
                        "when": _iso(m["date"]), "snippet": m["snippet"], "reasons": m["reasons"],
                        "unread": m["unread"], "score": m["score"],
                        "source": src(r, "email from %s" % m["from_addr"]),
                    })
                for kind, bucket in (("payments", "payments"), ("renewals", "renewals")):
                    for hint in r.data[kind]:
                        m = hint["message"]
                        d = hint["date"]
                        if d is not None and not (-3 <= (d - today).days <= win["payment_days" if kind == "payments" else "renewal_days"]):
                            continue
                        sections[bucket].append({
                            "id": _id(r.id, "hint", m["uid"]), "title": m["subject"],
                            "subtitle": "from " + m["from_name"],
                            "when": _iso(_day_start(d, tz)) if d else None, "all_day": True,
                            "amount": {"value": hint["amount"]["value"], "currency": hint["amount"]["currency"], "estimate": True},
                            "date_estimate": True,
                            "estimate_note": "amount%s read from the email text — check the original before paying" % (
                                " and date" if d else " (no due date found)"),
                            "from_email": True,
                            "source": src(r, "email from %s, %s" % (m["from_addr"], m["date"].strftime("%b %d") if m["date"] else "")),
                        })

        far = datetime.max.replace(tzinfo=timezone.utc)
        for key, items in sections.items():
            items.sort(key=lambda it: (datetime.fromisoformat(it["when"]) if it.get("when") else far))
            if key == "email":
                items.sort(key=lambda it: -(it.get("score") or 0))

        return {
            "generated_at": _iso(now),
            "date": today.isoformat(),
            "windows": win,
            "sections": sections,
            "sources": [r.public() for r in results.values()],
        }
