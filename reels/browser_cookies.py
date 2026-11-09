import hashlib
import shutil
import sqlite3
import struct
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

HOME = Path.home()
APP_SUPPORT = HOME / "Library" / "Application Support"
INSTAGRAM = "instagram.com"

# Chromium on macOS derives the cookie key from a Keychain password with these fixed parameters.
KDF_SALT = b"saltysalt"
KDF_ITERATIONS = 1003
CBC_IV = b" " * 16
# From cookie database version 24 on, the plaintext starts with a SHA-256 hash of the cookie's domain.
DOMAIN_HASH_DB_VERSION = 24
CHROMIUM_EPOCH_OFFSET = 11644473600
MAC_EPOCH_OFFSET = 978307200

SAFARI_COOKIE_FILES = (
    HOME / "Library" / "Containers" / "com.apple.Safari" / "Data" / "Library" / "Cookies" / "Cookies.binarycookies",
    HOME / "Library" / "Cookies" / "Cookies.binarycookies",
)
FULL_DISK_ACCESS_HINT = (
    "Safari's cookies need Full Disk Access. Open System Settings, Privacy & Security, Full Disk Access, "
    "and turn it on for Reels Manager. Then quit and reopen Reels Manager."
)


@dataclass(frozen=True)
class ChromiumBrowser:
    name: str
    data_dir: Path
    keychain_service: str


CHROMIUM_BROWSERS = (
    ChromiumBrowser("Chrome", APP_SUPPORT / "Google" / "Chrome", "Chrome Safe Storage"),
    ChromiumBrowser("Arc", APP_SUPPORT / "Arc" / "User Data", "Arc Safe Storage"),
    ChromiumBrowser("Brave", APP_SUPPORT / "BraveSoftware" / "Brave-Browser", "Brave Safe Storage"),
    ChromiumBrowser("Edge", APP_SUPPORT / "Microsoft Edge", "Microsoft Edge Safe Storage"),
    ChromiumBrowser("Chromium", APP_SUPPORT / "Chromium", "Chromium Safe Storage"),
)
BROWSER_NAMES = ", ".join([b.name for b in CHROMIUM_BROWSERS] + ["Safari"])


class BrowserCookieError(Exception):
    pass


@dataclass
class Login:
    browser: str
    last_used: float
    load: Callable[[], dict[str, str]]


def _is_instagram(domain: str) -> bool:
    domain = domain.lstrip(".")
    return domain == INSTAGRAM or domain.endswith(f".{INSTAGRAM}")


def _chromium_cookie_dbs(browser: ChromiumBrowser) -> list[Path]:
    if not browser.data_dir.is_dir():
        return []
    found = []
    for profile in browser.data_dir.iterdir():
        if profile.name != "Default" and not profile.name.startswith("Profile "):
            continue
        for relative in ("Cookies", "Network/Cookies"):
            if (profile / relative).is_file():
                found.append(profile / relative)
                break
    return found


def _read_chromium_db(db_path: Path) -> tuple[list[sqlite3.Row], int]:
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "Cookies"
        shutil.copy2(db_path, copy)
        conn = sqlite3.connect(copy)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "select host_key, name, value, encrypted_value, last_access_utc from cookies "
                "where host_key like '%instagram.com' order by length(host_key)"
            ).fetchall()
            version_row = conn.execute("select value from meta where key = 'version'").fetchone()
        finally:
            conn.close()
    return [r for r in rows if _is_instagram(r["host_key"])], int(version_row[0]) if version_row else 0


def _keychain_password(service: str) -> str:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", service],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise BrowserCookieError("The Keychain prompt timed out. Try again and click Allow.")
    if result.returncode != 0:
        raise BrowserCookieError("macOS did not allow access to the browser's cookie key. Try again and click Allow.")
    return result.stdout.strip()


def derive_key(password: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password.encode(), KDF_SALT, KDF_ITERATIONS, 16)


def decrypt_value(encrypted: bytes, key: bytes, db_version: int) -> str:
    if encrypted[:3] not in (b"v10", b"v11"):
        return encrypted.decode()
    decryptor = Cipher(algorithms.AES(key), modes.CBC(CBC_IV)).decryptor()
    padded = decryptor.update(encrypted[3:]) + decryptor.finalize()
    plain = padded[: -padded[-1]]
    if db_version >= DOMAIN_HASH_DB_VERSION:
        plain = plain[32:]
    return plain.decode()


def _chromium_logins() -> list[Login]:
    logins = []
    for browser in CHROMIUM_BROWSERS:
        for db_path in _chromium_cookie_dbs(browser):
            try:
                rows, _ = _read_chromium_db(db_path)
            except (sqlite3.Error, OSError):
                continue
            sessions = [r for r in rows if r["name"] == "sessionid"]
            if not sessions:
                continue
            last_used = max(r["last_access_utc"] for r in sessions) / 1e6 - CHROMIUM_EPOCH_OFFSET

            def load(browser=browser, db_path=db_path) -> dict[str, str]:
                key = derive_key(_keychain_password(browser.keychain_service))
                rows, db_version = _read_chromium_db(db_path)
                cookies = {}
                for row in rows:
                    try:
                        value = row["value"] or decrypt_value(row["encrypted_value"], key, db_version)
                    except (ValueError, UnicodeDecodeError):
                        raise BrowserCookieError(f"Could not decrypt {browser.name}'s cookies. Paste a cookie by hand instead.")
                    if value:
                        cookies[row["name"]] = value
                return cookies

            logins.append(Login(browser.name, last_used, load))
    return logins


def _c_string(data: bytes, offset: int) -> str:
    return data[offset : data.index(b"\x00", offset)].decode("utf-8", "replace")


def parse_binarycookies(data: bytes) -> list[dict]:
    if data[:4] != b"cook":
        raise BrowserCookieError("Safari's cookie file has an unknown format.")
    page_count = struct.unpack(">I", data[4:8])[0]
    page_sizes = struct.unpack(f">{page_count}I", data[8 : 8 + 4 * page_count])
    cookies = []
    position = 8 + 4 * page_count
    for size in page_sizes:
        page = data[position : position + size]
        position += size
        count = struct.unpack("<I", page[4:8])[0]
        offsets = struct.unpack(f"<{count}I", page[8 : 8 + 4 * count])
        for offset in offsets:
            (length,) = struct.unpack("<I", page[offset : offset + 4])
            record = page[offset : offset + length]
            domain_at, name_at, _, value_at = struct.unpack("<4I", record[16:32])
            _, created = struct.unpack("<2d", record[40:56])
            cookies.append({
                "domain": _c_string(record, domain_at),
                "name": _c_string(record, name_at),
                "value": _c_string(record, value_at),
                "created": created + MAC_EPOCH_OFFSET,
            })
    return cookies


def _safari_logins() -> tuple[list[Login], bool]:
    blocked = False
    for path in SAFARI_COOKIE_FILES:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            continue
        except PermissionError:
            blocked = True
            continue
        cookies = [c for c in parse_binarycookies(data) if _is_instagram(c["domain"])]
        sessions = [c for c in cookies if c["name"] == "sessionid"]
        if sessions:
            ordered = sorted(cookies, key=lambda c: len(c["domain"]))
            values = {c["name"]: c["value"] for c in ordered if c["value"]}
            return [Login("Safari", max(c["created"] for c in sessions), lambda: values)], blocked
    return [], blocked


def instagram_cookies() -> tuple[dict[str, str], str]:
    safari, safari_blocked = _safari_logins()
    logins = _chromium_logins() + safari
    if not logins:
        if safari_blocked:
            raise BrowserCookieError(f"No Instagram login found in {BROWSER_NAMES}. {FULL_DISK_ACCESS_HINT}")
        raise BrowserCookieError(f"No Instagram login found in {BROWSER_NAMES}. Log in at instagram.com and try again.")
    login = max(logins, key=lambda entry: entry.last_used)
    return login.load(), login.browser
