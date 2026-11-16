"""Import Google login cookies from the user's everyday browser (Firefox or Chrome-family)."""

from __future__ import annotations

import configparser
import hashlib
import json
import logging
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

HOME = Path.home()

FIREFOX_ROOTS = [
    HOME / "snap/firefox/common/.mozilla/firefox",
    HOME / ".mozilla/firefox",
    HOME / ".var/app/org.mozilla.firefox/.mozilla/firefox",
]

# name -> (user-data roots, Secret Service "application" attribute)
CHROMIUM_BROWSERS: dict[str, tuple[list[Path], str]] = {
    "chrome": (
        [HOME / ".config/google-chrome", HOME / ".var/app/com.google.Chrome/config/google-chrome"],
        "chrome",
    ),
    "chromium": (
        [
            HOME / ".config/chromium",
            HOME / "snap/chromium/common/chromium",
            HOME / ".var/app/org.chromium.Chromium/config/chromium",
        ],
        "chromium",
    ),
    "brave": ([HOME / ".config/BraveSoftware/Brave-Browser"], "brave"),
}

BROWSERS = ["firefox", *CHROMIUM_BROWSERS]

SAME_SITE = {0: "None", 1: "Lax", 2: "Strict"}
CHROME_EPOCH_OFFSET = 11644473600  # seconds between 1601-01-01 and 1970-01-01


@dataclass
class CookieSource:
    browser: str  # "firefox", "chrome", ...
    db: Path  # cookies sqlite file
    last_used: float  # unix seconds of most recent google.com cookie access

    def __str__(self) -> str:
        return f"{self.browser} ({self.db.parent})"


def _copy_db(db: Path, td: str) -> Path:
    """Browsers lock the DB and keep recent writes in the WAL; copy both."""
    for suffix in ("", "-wal", "-journal"):
        src = db.with_name(db.name + suffix)
        if src.exists():
            shutil.copy2(src, Path(td) / src.name)
    return Path(td) / db.name


# ---- discovery ------------------------------------------------------------------


def _firefox_dbs() -> list[Path]:
    out = []
    for root in FIREFOX_ROOTS:
        ini = root / "profiles.ini"
        if not ini.exists():
            continue
        cp = configparser.ConfigParser()
        cp.read(ini)
        paths = []
        # Newer Firefox records the active profile under [Install...] Default=
        paths += [root / cp[s]["Default"] for s in cp.sections() if s.startswith("Install") and cp[s].get("Default")]
        for s in cp.sections():
            if cp[s].get("Path"):
                rel = cp[s].get("IsRelative", "1") == "1"
                paths.append(root / cp[s]["Path"] if rel else Path(cp[s]["Path"]))
        for p in dict.fromkeys(paths):
            if (p / "cookies.sqlite").exists():
                out.append(p / "cookies.sqlite")
    return out


def _chromium_dbs(browser: str) -> list[Path]:
    roots, _ = CHROMIUM_BROWSERS[browser]
    out = []
    for root in roots:
        profiles = ["Default"]
        try:
            state = json.loads((root / "Local State").read_text())
            profiles = [state.get("profile", {}).get("last_used") or "Default", *state["profile"]["info_cache"]]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        for prof in dict.fromkeys(profiles):
            for rel in ("Cookies", "Network/Cookies"):
                if (root / prof / rel).exists():
                    out.append(root / prof / rel)
                    break
    return out


def _last_google_use(browser: str, db: Path) -> float:
    if browser == "firefox":
        sql = "SELECT MAX(lastAccessed) FROM moz_cookies WHERE host LIKE '%google.com' AND name='SID'"
    else:
        sql = "SELECT MAX(last_access_utc) FROM cookies WHERE host_key LIKE '%google.com' AND name='SID'"
    with tempfile.TemporaryDirectory() as td:
        try:
            con = sqlite3.connect(_copy_db(db, td))
            try:
                (v,) = con.execute(sql).fetchone()
            finally:
                con.close()
        except (sqlite3.Error, OSError) as e:
            log.info("can't read %s: %s", db, e)
            return 0
    if not v:
        return 0
    return v / 1e6 if browser == "firefox" else v / 1e6 - CHROME_EPOCH_OFFSET


def find_sources(browser: str = "auto") -> list[CookieSource]:
    """Browsers/profiles that have a Google login, most recently used first."""
    wanted = BROWSERS if browser == "auto" else [browser]
    found = []
    for b in wanted:
        dbs = _firefox_dbs() if b == "firefox" else _chromium_dbs(b)
        for db in dbs:
            ts = _last_google_use(b, db)
            if ts:
                found.append(CookieSource(b, db, ts))
    return sorted(found, key=lambda s: s.last_used, reverse=True)


# ---- reading ----------------------------------------------------------------------


def _read_firefox(db: Path) -> list[dict]:
    with tempfile.TemporaryDirectory() as td:
        con = sqlite3.connect(_copy_db(db, td))
        rows = con.execute(
            "SELECT host, name, value, path, expiry, isSecure, isHttpOnly, sameSite "
            "FROM moz_cookies WHERE host LIKE '%google.com'"
        ).fetchall()
        con.close()
    out = []
    for host, name, value, path, expiry, secure, http_only, ss in rows:
        if expiry > 1e11:  # some Firefox versions store milliseconds
            expiry //= 1000
        out.append(_cookie(name, value, host, path, expiry, secure, http_only, SAME_SITE.get(ss, "None")))
    return out


def _keyring_password(application: str) -> bytes | None:
    """The 'Chrome Safe Storage' secret from GNOME Keyring / KWallet's Secret Service."""
    try:
        import secretstorage

        conn = secretstorage.dbus_init()
        try:
            coll = secretstorage.get_default_collection(conn)
            if coll.is_locked():
                coll.unlock()
            for item in coll.search_items({"application": application}):
                return item.get_secret()
            for item in coll.get_all_items():  # older Chrome: matched by label only
                if item.get_label() == f"{application.capitalize()} Safe Storage":
                    return item.get_secret()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 - no dbus, no keyring, locked, ...
        log.info("keyring lookup for %s failed: %s", application, e)
    return None


def _aes_key(password: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1, 16)


def _decrypt(enc: bytes, keys: dict[bytes, list[bytes]], strip_hash: bool) -> str | None:
    """AES-128-CBC; try each candidate key for the version prefix until one yields valid text."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    body = enc[3:]
    if not body or len(body) % 16:
        return None
    for key in keys.get(enc[:3], []):
        dec = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
        raw = dec.update(body) + dec.finalize()
        pad = raw[-1]
        if not 1 <= pad <= 16 or raw[-pad:] != bytes([pad]) * pad:
            continue
        raw = raw[:-pad]
        if strip_hash:  # cookie DB version >= 24 prepends sha256(host_key)
            raw = raw[32:]
        try:
            return raw.decode()
        except UnicodeDecodeError:
            continue
    return None


def _read_chromium(browser: str, db: Path) -> list[dict]:
    _, app = CHROMIUM_BROWSERS[browser]
    with tempfile.TemporaryDirectory() as td:
        con = sqlite3.connect(_copy_db(db, td))
        try:
            (ver,) = con.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        except (sqlite3.Error, TypeError):
            ver = 0
        rows = con.execute(
            "SELECT host_key, name, value, encrypted_value, path, expires_utc, is_secure, is_httponly, samesite "
            "FROM cookies WHERE host_key LIKE '%google.com'"
        ).fetchall()
        con.close()

    # v10: fixed password (no keyring). v11: password from the desktop keyring.
    fallback = [_aes_key(b"peanuts"), _aes_key(b"")]
    keys = {b"v10": fallback, b"v11": fallback}
    if any(r[3][:3] == b"v11" for r in rows):
        pw = _keyring_password(app)
        if pw is not None:
            keys[b"v11"] = [_aes_key(pw), *fallback]

    out, failed = [], 0
    for host, name, value, enc, path, expires, secure, http_only, ss in rows:
        if not value and enc:
            value = _decrypt(enc, keys, strip_hash=int(ver) >= 24)
            if value is None:
                failed += 1
                continue
        expiry = expires / 1e6 - CHROME_EPOCH_OFFSET if expires else -1
        out.append(_cookie(name, value, host, path, expiry, secure, http_only, SAME_SITE.get(ss, "Lax")))
    if failed:
        log.warning("%s: couldn't decrypt %d of %d cookies", browser, failed, len(rows))
        if not out:
            raise RuntimeError(
                f"Couldn't decrypt {browser}'s cookies. They're protected by a key in your desktop "
                "keyring; make sure you're in a desktop session with the keyring unlocked."
            )
    return out


def _cookie(name, value, host, path, expiry, secure, http_only, same_site) -> dict:
    """Playwright add_cookies() format."""
    return {
        "name": name,
        "value": value,
        "domain": host,
        "path": path or "/",
        "expires": float(expiry),
        "secure": bool(secure),
        "httpOnly": bool(http_only),
        "sameSite": same_site,
    }


def load_google_cookies(browser: str = "auto") -> list[dict]:
    """Google cookies, in Playwright format, from the most recently used logged-in browser."""
    sources = find_sources(browser)
    if not sources:
        which = "any supported browser" if browser == "auto" else browser
        raise FileNotFoundError(f"No Google login found in {which}. Sign in to chat.google.com there first.")
    src = sources[0]
    log.info("importing Google cookies from %s", src)
    if src.browser == "firefox":
        return _read_firefox(src.db)
    return _read_chromium(src.browser, src.db)
