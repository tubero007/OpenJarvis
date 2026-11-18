"""Command line for setting up read-only sources and the LaunchAgent.

Run through the launcher, e.g. `./jarvis-dashboard setup canvas`.
Secrets are typed into this terminal (hidden) and go straight to the macOS
Keychain. They are never written to config files or sent anywhere except the
service they belong to.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from typing import List, Optional

from . import bills as bills_mod
from . import ical, launchagent, mail, paths, secrets
from .today import TodayService


def _ask(prompt: str, default: str = "") -> str:
    suffix = " [%s]" % default if default else ""
    value = input("%s%s: " % (prompt, suffix)).strip()
    return value or default


def _ask_secret(prompt: str) -> str:
    if sys.stdin.isatty():
        return getpass.getpass(prompt + " (hidden): ").strip()
    return sys.stdin.readline().strip()  # non-interactive (tests)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "calendar"


def setup_feed(kind: str, label: Optional[str]) -> int:
    cfg = paths.load_sources()
    if kind == "canvas":
        print("Canvas → Calendar → Calendar Feed (bottom right) → copy the URL.")
        print("That URL is private (it contains a token), so it is stored in your Keychain.")
        key, label = "ical:canvas", label or "Canvas"
    else:
        print("Paste a private iCal (.ics) address. Google Calendar: Settings → your calendar →")
        print("'Secret address in iCal format'. iCloud: Calendar → share → Public Calendar link.")
        label = label or _ask("Name for this calendar", "Personal")
        key = "ical:cal:" + _slug(label)
    url = _ask_secret("Feed URL")
    try:
        url = ical.normalize_url(url)
        items = ical.parse(ical.fetch(url))
    except ical.FeedError as exc:
        print("✗ %s" % exc)
        return 1
    now = datetime.now().astimezone()
    upcoming = ical.expand(items, now, now + timedelta(days=14))
    print("✓ Read %d entries (%d in the next 14 days) from %s" % (len(items), len(upcoming), ical.redact(url)))
    secrets.set(key, url)
    if kind == "canvas":
        cfg["canvas"] = {"enabled": True, "label": label}
    else:
        cals = [c for c in cfg.get("calendars", []) if c["id"] != "cal:" + _slug(label) and c["id"] != _slug(label)]
        cals.append({"id": "cal:" + _slug(label), "label": label, "enabled": True})
        cfg["calendars"] = cals
    paths.save_sources(cfg)
    print("✓ Saved. The feed URL is in your Keychain (service 'jarvis-dashboard', account '%s')." % key)
    return 0


def setup_email() -> int:
    cfg = paths.load_sources()
    cur = cfg.get("email") or {}
    print("Read-only email via IMAP. Jarvis opens your inbox read-only and never marks,")
    print("moves, deletes or sends mail. Use an *app password*, not your main password:")
    print("  Gmail: myaccount.google.com/apppasswords (needs 2-Step Verification)")
    print("  iCloud: account.apple.com → Sign-In and Security → App-Specific Passwords")
    print("  Outlook/Microsoft 365: many accounts block app passwords; IMAP may not be available.")
    address = _ask("Email address", cur.get("address", ""))
    if "@" not in address:
        print("✗ That doesn't look like an email address.")
        return 1
    host = _ask("IMAP server", cur.get("host") or mail.default_host(address))
    port = int(_ask("Port", str(cur.get("port", 993))))
    security = cur.get("security", "tls" if port == 993 else "starttls")
    password = _ask_secret("App password")
    try:
        got = mail.fetch_recent(address, password, host, port, security, days=2, limit=10)
    except Exception as exc:  # noqa: BLE001
        print("✗ Couldn't sign in or read the inbox: %s" % exc)
        return 1
    print("✓ Signed in and read %d recent messages (inbox has %d), read-only." % (len(got["messages"]), got["mailbox_total"]))
    secrets.set("imap:" + address, password)
    senders = cur.get("important_senders") or []
    extra = _ask("Important senders or domains, comma-separated (e.g. @myuniversity.edu)", ", ".join(senders))
    cfg["email"] = {
        "enabled": True, "address": address, "host": host, "port": port, "security": security,
        "lookback_days": cur.get("lookback_days", 3), "max_messages": cur.get("max_messages", 40),
        "important_senders": [s.strip() for s in extra.split(",") if s.strip()],
    }
    paths.save_sources(cfg)
    print("✓ Saved. The app password is in your Keychain (account 'imap:%s')." % address)
    return 0


def setup_apple_calendar() -> int:
    try:
        from openjarvis.connectors.apple_calendar import AppleCalendarConnector
    except ImportError:
        print("✗ OpenJarvis' Apple Calendar connector isn't importable from this Python.")
        return 1
    docs = list(AppleCalendarConnector(days_ahead=2).sync())
    if not docs:
        print("! No events read. Either the next two days are empty, or this Python needs")
        print("  Full Disk Access (System Settings → Privacy & Security → Full Disk Access).")
        print("  Python used: %s" % os.path.realpath(sys.executable))
        if _ask("Enable anyway? (y/N)", "n").lower() != "y":
            return 1
    else:
        print("✓ Read %d events from the Mac Calendar database (read-only)." % len(docs))
    cfg = paths.load_sources()
    cfg["apple_calendar"] = {"enabled": True}
    paths.save_sources(cfg)
    return 0


def remove(source: str) -> int:
    cfg = paths.load_sources()
    if source == "canvas":
        secrets.delete("ical:canvas")
        cfg.pop("canvas", None)
    elif source == "email":
        addr = (cfg.get("email") or {}).get("address")
        if addr:
            secrets.delete("imap:" + addr)
        cfg.pop("email", None)
    elif source == "apple-calendar":
        cfg.pop("apple_calendar", None)
    else:
        sid = source if source.startswith("cal:") else "cal:" + _slug(source)
        secrets.delete("ical:" + sid)
        cfg["calendars"] = [c for c in cfg.get("calendars", []) if c["id"] != sid]
    paths.save_sources(cfg)
    print("✓ Removed %s (config and Keychain entry)." % source)
    return 0


def edit_bills(open_editor: bool = True) -> int:
    path = paths.bills_path()
    if not path.exists():
        path.write_text(bills_mod.TEMPLATE, encoding="utf-8")
        path.chmod(0o600)
        print("Created %s with examples — replace them with your own." % path)
    try:
        bills_mod.load(path, datetime.now().date())
    except Exception as exc:  # noqa: BLE001
        print("! bills.toml has a problem: %s" % exc)
    print(path)
    if open_editor and sys.platform == "darwin":
        subprocess.run(["open", "-e", str(path)], check=False)
    return 0


def show_sources() -> int:
    today = TodayService().build(force=True)
    for s in today["sources"]:
        if not s["configured"]:
            state = "not set up  → %s" % (s["setup_hint"] or "")
        elif s["ok"]:
            state = "ok, %d item(s), checked %s" % (s["count"], s["checked_at"][11:16])
        else:
            state = "ERROR: %s" % s["error"]
        print("%-28s %s" % (s["label"][:28], state))
    for name, items in today["sections"].items():
        print("  %-10s %d" % (name, len(items)))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="jarvis-dashboard")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="connect a read-only source")
    s.add_argument("what", choices=["canvas", "calendar", "email", "apple-calendar"])
    s.add_argument("--label")
    r = sub.add_parser("remove", help="remove a source and its Keychain entry")
    r.add_argument("what")
    b = sub.add_parser("bills", help="create/open your bills & subscriptions list")
    b.add_argument("--no-open", action="store_true")
    sub.add_parser("sources", help="check every source now")
    la = sub.add_parser("launchagent", help="start at login (opt-in)")
    la.add_argument("action", choices=["install", "uninstall", "restart", "status", "logs"])
    la.add_argument("--launcher", default=os.environ.get("JARVIS_LAUNCHER", ""))
    args = p.parse_args(argv)

    try:
        if args.cmd == "setup":
            if args.what in ("canvas", "calendar"):
                return setup_feed(args.what, args.label)
            if args.what == "email":
                return setup_email()
            return setup_apple_calendar()
        if args.cmd == "remove":
            return remove(args.what)
        if args.cmd == "bills":
            return edit_bills(not args.no_open)
        if args.cmd == "sources":
            return show_sources()
        if args.cmd == "launchagent":
            return launchagent.run(args.action, args.launcher)
    except secrets.SecretStoreError as exc:
        print("✗ %s" % exc)
        return 1
    except (KeyboardInterrupt, EOFError):
        print()
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
