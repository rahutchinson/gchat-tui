"""gchat-tui: a terminal client for Google Chat (personal accounts)."""

import argparse
import asyncio
import fcntl
import logging
import sys

from .cookies import BROWSERS, find_sources
from .session import DATA_DIR, Session, ensure_private_dir


def main() -> None:
    p = argparse.ArgumentParser(prog="gchat-tui")
    p.add_argument("--show-browser", action="store_true", help="run the browser visibly (debugging)")
    p.add_argument("--check", action="store_true", help="connect, list conversations, exit 0/1 (health check)")
    p.add_argument(
        "--browser",
        choices=["auto", *BROWSERS],
        default="auto",
        help="browser to import your Google login from (default: the one that used Google most recently)",
    )
    p.add_argument("--list-browsers", action="store_true", help="show browsers with a Google login and exit")
    args = p.parse_args()

    if args.list_browsers:
        sources = find_sources(args.browser)
        if not sources:
            print("No browser with a Google login found.")
        for n, s in enumerate(sources):
            print(f"{'*' if n == 0 else ' '} {s}  (last used Google {_ago(s.last_used)})")
        sys.exit(0 if sources else 1)

    ensure_private_dir(DATA_DIR)
    # One instance at a time: the browser profile can't be shared.
    lock = open(DATA_DIR / "lock", "w")  # noqa: SIM115 - held for the process lifetime
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("gchat-tui is already running (the browser profile is in use). Quit the other one first.")
        sys.exit(1)
    logging.basicConfig(
        filename=DATA_DIR / "gchat-tui.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    session = Session(headless=not args.show_browser, cookie_browser=args.browser)

    if args.check:
        sys.exit(asyncio.run(_check(session)))

    from .app import GChatApp

    GChatApp(session).run()


async def _check(session: Session) -> int:
    from .api import ChatClient

    try:
        await session.start()
        client = ChatClient(session)
        convs = await client.list_conversations(10)
        if not convs:
            print("FAIL: no conversations returned")
            return 1
        msgs = await client.list_messages(convs[0], 3)
        if not msgs or not all(m.time_us for m in msgs):
            print("FAIL: couldn't decode messages")
            return 1
        print(f"OK: {len(convs)} conversations, latest has {len(msgs)} messages")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: {type(e).__name__}: {e}")
        return 1
    finally:
        await session.close()


def _ago(ts: float) -> str:
    import time

    mins = int((time.time() - ts) / 60)
    if mins < 60:
        return f"{mins}m ago"
    if mins < 60 * 48:
        return f"{mins // 60}h ago"
    return f"{mins // 1440}d ago"
