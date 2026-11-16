"""Import Google login cookies from the user's everyday Firefox profile."""

from __future__ import annotations

import configparser
import shutil
import sqlite3
import tempfile
from pathlib import Path

FIREFOX_ROOTS = [
    Path.home() / "snap/firefox/common/.mozilla/firefox",
    Path.home() / ".mozilla/firefox",
    Path.home() / ".var/app/org.mozilla.firefox/.mozilla/firefox",
]


def find_firefox_profile() -> Path:
    for root in FIREFOX_ROOTS:
        ini = root / "profiles.ini"
        if not ini.exists():
            continue
        cp = configparser.ConfigParser()
        cp.read(ini)
        # Newer Firefox records the active profile under [Install...] Default=
        for sec in cp.sections():
            if sec.startswith("Install") and cp[sec].get("Default"):
                p = root / cp[sec]["Default"]
                if (p / "cookies.sqlite").exists():
                    return p
        for sec in cp.sections():
            s = cp[sec]
            if s.get("Default") == "1" and s.get("Path"):
                p = root / s["Path"] if s.get("IsRelative", "1") == "1" else Path(s["Path"])
                if (p / "cookies.sqlite").exists():
                    return p
    raise FileNotFoundError("No Firefox profile with cookies.sqlite found")


def load_google_cookies(profile: Path | None = None) -> list[dict]:
    """Return Google cookies in Playwright's add_cookies() format."""
    profile = profile or find_firefox_profile()
    with tempfile.TemporaryDirectory() as td:
        # Firefox holds a lock and keeps recent writes in the WAL; copy both.
        for name in ("cookies.sqlite", "cookies.sqlite-wal"):
            src = profile / name
            if src.exists():
                shutil.copy2(src, Path(td) / name)
        con = sqlite3.connect(Path(td) / "cookies.sqlite")
        rows = con.execute(
            "SELECT host, name, value, path, expiry, isSecure, isHttpOnly, sameSite "
            "FROM moz_cookies WHERE host LIKE '%google.com'"
        ).fetchall()
        con.close()

    same_site = {0: "None", 1: "Lax", 2: "Strict"}
    out = []
    for host, name, value, path, expiry, secure, http_only, ss in rows:
        if expiry > 1e11:  # some Firefox versions store milliseconds
            expiry //= 1000
        out.append(
            {
                "name": name,
                "value": value,
                "domain": host,
                "path": path or "/",
                "expires": float(expiry),
                "secure": bool(secure),
                "httpOnly": bool(http_only),
                "sameSite": same_site.get(ss, "None"),
            }
        )
    return out
