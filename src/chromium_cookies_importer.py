#!/usr/bin/env python3
"""
Chromium → Zen Cookies Importer

Decrypts any Chromium-format cookies (Arc / Chrome / Edge / Brave) and
writes them into Zen's Firefox-format ``cookies.sqlite`` so login
sessions carry over.

Supports macOS and Windows:

- **macOS**: master key fetched from a per-browser Keychain entry
  (``"Arc Safe Storage"`` / ``"Chrome Safe Storage"`` / ...), derived via
  PBKDF2 (1003 iterations, salt ``"saltysalt"``) to a 16-byte AES-128 key.
  Cookie blobs prefixed ``v10`` use AES-128-CBC with an all-spaces IV
  and PKCS7 padding.
- **Windows**: master key read from each browser's ``Local State`` JSON,
  base64-decoded, unwrapped via Windows DPAPI
  (``crypt32!CryptUnprotectData`` through ctypes). Cookie blobs prefixed
  ``v10`` use AES-256-GCM (12-byte nonce + ciphertext + 16-byte
  authentication tag).

The Firefox-side write routes each source profile only to its mapped
container and uses schema-specific expiry units. Re-running merges cookies
without mixing accounts or touching the default context.

The orchestrator passes the source's cookie paths + matching Keychain
service/account + Local State path via the importer's kwargs so the
same code serves every Chromium browser.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from contextlib import closing
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import quote_plus, urlsplit

logger = logging.getLogger(__name__)


_WEBKIT_EPOCH_OFFSET_US = 11_644_473_600_000_000


def _chrome_to_unix_us(chrome_us: int | None) -> int:
    if not chrome_us:
        return 0
    val = chrome_us - _WEBKIT_EPOCH_OFFSET_US
    return val if val > 0 else 0


def _read_keychain_password(service: str = "Arc Safe Storage", account: str = "Arc") -> str | None:
    """Fetch the per-app Chromium safe-storage key from macOS Keychain.

    Arc registers the entry with svce="Arc Safe Storage" and acct="Arc".
    Searching by service alone falls back to account-blind lookup, but
    that's what triggers the "could not be found" error on some setups.
    """
    attempts = [
        ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
        ["security", "find-generic-password", "-s", service, "-w"],
        ["security", "find-generic-password", "-wa", service],
    ]
    last_err = ""
    for cmd in attempts:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            logger.error("Keychain lookup timed out (likely awaiting user approval).")
            return None
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        last_err = result.stderr.strip()
    logger.error(f"Keychain returned error for {service!r}: {last_err or 'denied'}")
    return None


def _derive_aes_key_macos(password: str) -> bytes:
    """Chromium on macOS: PBKDF2-HMAC-SHA1, salt='saltysalt', iterations=1003, keylen=16."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=16,
        salt=b"saltysalt",
        iterations=1003,
    )
    return kdf.derive(password.encode("utf-8"))


def _decrypt_v10_cbc(blob: bytes, key: bytes) -> bytes | None:
    """macOS path. Strip 'v10' magic, AES-128-CBC decrypt with all-spaces IV, PKCS7 unpad."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if not blob.startswith(b"v10"):
        return None
    ciphertext = blob[3:]
    if len(ciphertext) % 16 != 0:
        return None
    iv = b" " * 16
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    pad = padded[-1] if padded else 0
    if not (0 < pad <= 16 and padded.endswith(bytes([pad]) * pad)):
        return None
    return padded[:-pad]


# ---------- Windows DPAPI master-key unwrap ----------

class _DpapiError(RuntimeError):
    """Raised when Windows DPAPI rejects the encrypted blob.

    The ``code`` attribute carries an orchestrator-friendly error key.
    """

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def _local_state_paths_default() -> list[Path]:
    """Return Local State JSON candidates across both Windows install vectors."""
    home = Path.home()
    if os.name != "nt":
        return []
    candidates = [
        home / "AppData/Local/Packages/TheBrowserCompany.Arc_ttt1ap7aakyb4"
             / "LocalCache/Local/Arc/User Data/Local State",
        home / "AppData/Local/Arc/User Data/Local State",
    ]
    return [p for p in candidates if p.is_file()]


def _crypt_unprotect_data(blob: bytes) -> bytes:
    """Call ``crypt32!CryptUnprotectData`` via ctypes and return the plaintext.

    Raises ``_DpapiError`` with a structured code on failure.
    """
    import ctypes
    from ctypes import wintypes  # type: ignore[import-not-found]

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL

    buf = ctypes.create_string_buffer(blob, len(blob))
    in_blob = DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out_blob = DATA_BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1

    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    )
    if not ok:
        err = ctypes.get_last_error()
        # ERROR_INVALID_DATA (13) is the canonical "different user" symptom.
        code = "dpapi_wrong_user" if err == 13 else "dpapi_failed"
        raise _DpapiError(code, f"CryptUnprotectData failed (GetLastError={err})")

    try:
        size = int(out_blob.cbData)
        plaintext = ctypes.string_at(out_blob.pbData, size)
        return plaintext
    finally:
        # Free the allocation Windows made for us.
        kernel32.LocalFree(out_blob.pbData)


def _read_local_state_key_windows(local_state_paths: list[Path] | None = None) -> bytes:
    """Read a Chromium browser's ``Local State`` and unwrap the master key.

    ``local_state_paths`` is optional and lets callers point at any
    Chromium installs (Chrome, Edge, Brave). When omitted we fall back
    to Arc's bundled fallback locations.

    Raises ``_DpapiError`` with a structured code on any failure path.
    """
    paths = (
        [Path(p) for p in local_state_paths if Path(p).is_file()]
        if local_state_paths is not None
        else _local_state_paths_default()
    )
    if not paths:
        raise _DpapiError(
            "chromium_local_state_missing",
            "Browser has not been launched on this Windows account yet.",
        )
    state = json.loads(paths[0].read_text(encoding="utf-8"))
    encrypted_b64 = state.get("os_crypt", {}).get("encrypted_key")
    if not encrypted_b64:
        raise _DpapiError(
            "chromium_no_encrypted_key",
            "Local State has no os_crypt.encrypted_key entry.",
        )

    raw = base64.b64decode(encrypted_b64)
    if raw.startswith(b"APPB"):
        # Chromium v20 / Brave / Edge app-bound encryption; not defeatable
        # without invoking Chromium's elevated COM service.
        raise _DpapiError(
            "chromium_appbound_encryption",
            "Browser cookies use app-bound (v20) encryption.",
        )
    if not raw.startswith(b"DPAPI"):
        raise _DpapiError(
            "chromium_unknown_key_prefix",
            f"Unexpected key prefix {raw[:5]!r}",
        )

    key = _crypt_unprotect_data(raw[5:])
    if len(key) != 32:
        raise _DpapiError(
            "chromium_unexpected_key_length",
            f"Got {len(key)}-byte key from DPAPI; expected 32 bytes.",
        )
    return key


def _decrypt_v10_gcm(blob: bytes, key: bytes) -> bytes | None:
    """Windows path. Strip 'v10' magic, AES-256-GCM decrypt with the embedded nonce.

    Layout: ``b"v10" + nonce[12] + ciphertext + tag[16]``.
    Returns the plaintext or ``None`` on prefix/format mismatch. The
    caller swallows individual decrypt failures and counts them.
    """
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not blob.startswith(b"v10") or len(blob) < 3 + 12 + 16:
        return None
    nonce = blob[3:15]
    ct_and_tag = blob[15:]
    try:
        return AESGCM(key).decrypt(nonce, ct_and_tag, None)
    except InvalidTag:
        return None


def _strip_host_hash(plaintext: bytes, host: bytes, version: int) -> bytes | None:
    """Chromium DB v24 binds each encrypted value to its exact host."""
    if version < 24:
        return plaintext
    digest = hashlib.sha256(host).digest()
    if len(plaintext) < 32 or not hmac.compare_digest(plaintext[:32], digest):
        return None
    return plaintext[32:]


# Chromium SameSite (samesite enum in net/cookies/cookie_constants.h):
#   -1 UNSPECIFIED, 0 NO_RESTRICTION, 1 LAX, 2 STRICT
# Firefox sameSite (in nsICookie.idl):
#   0 SAMESITE_NONE, 1 SAMESITE_LAX, 2 SAMESITE_STRICT, 256 SAMESITE_UNSET
_SAMESITE_MAP = {-1: 256, 0: 0, 1: 1, 2: 2}


def _partition_key(site: str, host: str, source_scheme: int, cross_site: int) -> str:
    """Translate Chromium's schemeful site, retaining Gecko's ancestor boundary.

    Gecko's `f` bit means same-site with the top level through a cross-site
    ancestor. Chromium's bit also includes ordinary third-party frames.
    """
    if not site:
        return ""
    parsed = urlsplit(site)
    domain = parsed.hostname
    if (parsed.scheme not in ("http", "https") or not domain or parsed.username is not None
            or parsed.password is not None or parsed.port is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("unsupported_partition_site")
    if ":" in domain:
        domain = f"[{ipaddress.IPv6Address(domain)}]"
    elif any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in domain):
        raise ValueError("unsupported_partition_site")
    cookie_host = host.lower().lstrip(".")
    same_domain = cookie_host == domain or cookie_host.endswith("." + domain)
    scheme = {1: "http", 2: "https"}.get(source_scheme)
    if cross_site not in (0, 1) or (same_domain and scheme is None):
        raise ValueError("unknown_partition_ancestor")
    foreign = cross_site and same_domain and scheme == parsed.scheme
    key = f"({parsed.scheme},{domain}{',f' if foreign else ''})"
    # OriginAttributes::CreateSuffix sanitizes colons before URLParams encoding.
    return quote_plus(key.replace(":", "+"), safe="")


def _cookie_dbs_default() -> list[Path]:
    """Locate every supported source-browser profile's Cookies SQLite across both supported OSes.

    Newer Chromium builds nest the file as ``<profile>/Network/Cookies``;
    older builds keep it at ``<profile>/Cookies``. We accept either.
    """
    home = Path.home()
    roots: list[Path] = []
    if sys.platform == "darwin":
        roots.append(home / "Library/Application Support/Arc/User Data")
    elif os.name == "nt":
        roots.append(
            home / "AppData/Local/Packages/TheBrowserCompany.Arc_ttt1ap7aakyb4"
                 / "LocalCache/Local/Arc/User Data"
        )
        roots.append(home / "AppData/Local/Arc/User Data")

    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for profile in sorted(root.iterdir()):
            if not profile.is_dir():
                continue
            for candidate in (profile / "Network" / "Cookies", profile / "Cookies"):
                if candidate.is_file():
                    found.append(candidate)
                    break
    return found


class CookiesImporter:
    # Session cookies (Chromium has_expires=0) get this many days of fake
    # persistence in Firefox so Firefox's startup sweep doesn't immediately
    # purge them as expired.
    SESSION_COOKIE_DAYS = 30

    def __init__(
        self,
        zen_profile: Path,
        dry_run: bool = False,
        container_ids: list[int] | None = None,
        cookie_dbs: list[Path] | None = None,
        keychain_service: str = "Arc Safe Storage",
        keychain_account: str = "Arc",
        local_state_paths: list[Path] | None = None,
        cookie_routes: dict[Path, list[int]] | None = None,
    ):
        # ``cookie_dbs`` injects per-browser cookie SQLite paths; the
        # other three knobs let any Chromium browser (Chrome/Edge/Brave)
        # reuse this importer without touching the decrypt logic.
        self.zen_profile = Path(zen_profile)
        self.zen_cookies = self.zen_profile / "cookies.sqlite"
        self.dry_run = dry_run
        self.container_ids = container_ids or []
        self._injected_dbs: list[Path] | None = (
            [Path(p) for p in cookie_dbs] if cookie_dbs is not None else None
        )
        self._keychain_service = keychain_service
        self._keychain_account = keychain_account
        self._local_state_paths: list[Path] | None = (
            [Path(p) for p in local_state_paths] if local_state_paths is not None else None
        )
        self.cookie_routes = ({Path(p): ids for p, ids in cookie_routes.items()}
                              if cookie_routes is not None else None)
        self._tempdir: Path | None = None

    def _snapshot(self, src: Path) -> Path:
        if self._tempdir is None:
            self._tempdir = Path(tempfile.mkdtemp(prefix="browser2zen_cookies_"))
        dest = self._tempdir / f"{len(list(self._tempdir.iterdir()))}.sqlite"
        with closing(sqlite3.connect(f"file:{src}?mode=ro", uri=True)) as source, closing(sqlite3.connect(dest)) as target:
            source.backup(target)
        return dest

    def _cleanup(self) -> None:
        if self._tempdir and self._tempdir.exists():
            shutil.rmtree(self._tempdir, ignore_errors=True)
            self._tempdir = None

    def _decrypt_rows(
        self,
        rows: Iterable[sqlite3.Row],
        key: bytes,
        decrypt_fn: Callable[[bytes, bytes], bytes | None],
        version: int = 0,
        stats: dict | None = None,
    ) -> Iterable[dict]:
        decrypt_fail = 0
        decoded = 0
        stats = stats if stats is not None else {"failed": 0, "unsupported": 0}

        def s(b) -> str:
            if b is None:
                return ""
            if isinstance(b, str):
                return b
            try:
                return b.decode("utf-8")
            except UnicodeDecodeError:
                return b.decode("latin-1", errors="replace")

        for row in rows:
            try:
                partition = _partition_key(
                    s(row["top_frame_site_key"]) if "top_frame_site_key" in row.keys() else "",
                    s(row["host_key"]),
                    row["source_scheme"] if "source_scheme" in row.keys() else 0,
                    row["has_cross_site_ancestor"] if "has_cross_site_ancestor" in row.keys() else 1,
                )
            except ValueError:
                stats["unsupported"] += 1
                continue
            raw_value = row["value"]
            value = s(raw_value)
            blob = row["encrypted_value"]
            if not value and blob:
                pt = decrypt_fn(bytes(blob), key)
                if pt is not None:
                    host_bytes = row["host_key"]
                    if isinstance(host_bytes, str):
                        host_bytes = host_bytes.encode("utf-8")
                    pt = _strip_host_hash(pt, host_bytes, version)
                if pt is None:
                    decrypt_fail += 1
                    stats["failed"] += 1
                    continue
                try:
                    value = pt.decode("utf-8")
                except UnicodeDecodeError:
                    decrypt_fail += 1
                    stats["failed"] += 1
                    continue
                decoded += 1
            # Map Chromium source_scheme (0=unset, 1=http, 2=https) to
            # Firefox schemeMap (0=unset, 1=http, 2=https, 4=file).
            src_scheme = row["source_scheme"] if "source_scheme" in row.keys() else 0
            scheme_map = src_scheme if src_scheme in (1, 2) else (2 if row["is_secure"] else 1)

            yield {
                "partition_key": partition,
                "host_key": s(row["host_key"]),
                "name": s(row["name"]),
                "value": value,
                "path": s(row["path"]),
                "expires_us": _chrome_to_unix_us(row["expires_utc"]),
                "creation_us": _chrome_to_unix_us(row["creation_utc"]),
                "last_access_us": _chrome_to_unix_us(row["last_access_utc"]),
                "is_secure": row["is_secure"],
                "is_httponly": row["is_httponly"],
                "samesite": _SAMESITE_MAP.get(row["samesite"], 256),
                "has_expires": row["has_expires"] if "has_expires" in row.keys() else 1,
                "scheme_map": scheme_map,
            }
        if decrypt_fail:
            logger.warning(f"⚠️  Skipped {decrypt_fail} cookies that could not be decrypted")
        if decoded:
            logger.info(f"🔓 Decrypted {decoded} encrypted cookie values")

    def _resolve_key_and_decrypt_fn(self) -> tuple[bytes, Callable[[bytes, bytes], bytes | None], str | None]:
        """Return ``(key, decrypt_fn, None)`` on success or ``(b"", noop, error_code)``.

        Hides macOS Keychain vs Windows DPAPI behind a single dispatch.
        """
        if sys.platform == "darwin":
            password = _read_keychain_password(
                service=self._keychain_service,
                account=self._keychain_account,
            )
            if password is None:
                return b"", _decrypt_v10_cbc, "keychain_denied"
            return _derive_aes_key_macos(password), _decrypt_v10_cbc, None

        if os.name == "nt":
            try:
                key = _read_local_state_key_windows(self._local_state_paths)
            except _DpapiError as exc:
                logger.error(f"DPAPI key unwrap failed: {exc} ({exc.code})")
                return b"", _decrypt_v10_gcm, exc.code
            return key, _decrypt_v10_gcm, None

        return b"", _decrypt_v10_cbc, "unsupported_platform"

    def import_cookies(self) -> dict:
        result = {"read": 0, "imported": 0, "merged": 0, "skipped": 0,
                  "failed": 0, "unsupported": 0, "expired": 0, "decoded": 0,
                  "session_cookies": 0, "profiles": []}
        if not self.zen_cookies.exists():
            return {**result, "error": "cookies_db_missing"}
        with closing(sqlite3.connect(self.zen_cookies)) as target:
            schema = target.execute("PRAGMA user_version").fetchone()[0]
            columns = {r[1] for r in target.execute("PRAGMA table_info(moz_cookies)")}
        required = {"originAttributes", "name", "value", "host", "path", "expiry",
                    "lastAccessed", "creationTime", "isSecure", "isHttpOnly", "sameSite", "schemeMap"}
        if not required <= columns or schema not in range(1, 18):
            return {**result, "error": "unsupported_cookie_schema"}
        dbs = self._injected_dbs if self._injected_dbs is not None else _cookie_dbs_default()
        routes = self.cookie_routes
        if routes is None:
            # Standalone imports must use the same durable mapping as the app.
            from migration_state import load_state
            state = load_state(self.zen_profile)
            routes = {}
            for db in dbs:
                profile = db.parent.parent if db.parent.name == "Network" else db.parent
                cid = state["profiles"].get(f"arc:{profile.name}")
                if cid:
                    routes[db] = [cid]
            if len(routes) != len(dbs) or self.container_ids:
                return {**result, "error": "cookie_routes_required"}
        if not routes:
            return {**result, "error": "source_cookies_missing"}
        if any(not p.is_file() or not ids or any(not isinstance(cid, int) or cid <= 0 for cid in ids)
               for p, ids in routes.items()):
            return {**result, "error": "cookie_routes_required"}
        key, decrypt_fn, err = self._resolve_key_and_decrypt_fn()
        if err:
            return {**result, "error": err}
        cookies = []
        now_us = int(time.time() * 1_000_000)
        try:
            for db, ids in routes.items():
                profile = db.parent.parent if db.parent.name == "Network" else db.parent
                stats = {"profile": profile.name, "read": 0, "decoded": 0,
                         "failed": 0, "unsupported": 0, "expired": 0,
                         "imported": 0, "merged": 0}
                snap = self._snapshot(db)
                with closing(sqlite3.connect(f"file:{snap}?mode=ro", uri=True)) as source:
                    source.text_factory = bytes
                    source.row_factory = sqlite3.Row
                    source_columns = {r[1].decode() for r in source.execute("PRAGMA table_info(cookies)")}
                    has_meta = source.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone()
                    meta = source.execute("SELECT value FROM meta WHERE key='version'").fetchone() if has_meta else None
                    version = int(meta[0]) if meta else 0
                    fields = ["host_key", "name", "value", "encrypted_value", "path", "expires_utc",
                              "creation_utc", "last_access_utc", "is_secure", "is_httponly", "samesite"]
                    fields += [name if name in source_columns else f"{default} AS {name}"
                               for name, default in (("has_expires", 1), ("source_scheme", 0),
                                                     ("top_frame_site_key", "''"), ("has_cross_site_ancestor", 1))]
                    rows = source.execute("SELECT " + ", ".join(fields) + " FROM cookies").fetchall()
                    stats["read"] = len(rows)
                    for cookie in self._decrypt_rows(rows, key, decrypt_fn, version, stats):
                        stats["decoded"] += 1
                        if cookie["partition_key"] and "isPartitionedAttributeSet" not in columns:
                            stats["unsupported"] += 1
                            continue
                        if cookie["has_expires"] and cookie["expires_us"] <= now_us:
                            stats["expired"] += 1
                            continue
                        cookies.append((cookie, sorted(set(ids)), stats))
                result["profiles"].append(stats)
                for name in ("read", "decoded", "failed", "unsupported", "expired"):
                    result[name] += stats[name]
        finally:
            self._cleanup()
        result["skipped"] = result["failed"] + result["unsupported"] + result["expired"]
        if not cookies:
            if result["failed"] or result["unsupported"]:
                result["error"] = "cookie_decode_failed"
            return result
        if self.dry_run:
            result["dry_run"] = True
            result["imported"] = sum(len(ids) for _, ids, _ in cookies)
            return result
        # SQLite backup includes committed WAL contents.
        with closing(sqlite3.connect(self.zen_cookies)) as source:
            backup_path = self.zen_cookies.with_name(f"cookies.sqlite.backup.{time.time_ns()}")
            with closing(sqlite3.connect(backup_path)) as backup:
                source.backup(backup)
        names = ["originAttributes", "name", "value", "host", "path", "expiry", "lastAccessed",
                 "creationTime", "isSecure", "isHttpOnly", "sameSite", "schemeMap"]
        if "updateTime" in columns:
            names.append("updateTime")
        if "isPartitionedAttributeSet" in columns:
            names.append("isPartitionedAttributeSet")
        placeholders = ",".join("?" for _ in names)
        updates = ",".join(f"{name}=excluded.{name}" for name in names
                           if name not in ("originAttributes", "name", "host", "path", "creationTime"))
        with closing(sqlite3.connect(self.zen_cookies, timeout=30)) as target, target:
            for cookie, ids, stats in cookies:
                expiry_us = cookie["expires_us"]
                if not cookie["has_expires"]:
                    expiry_us = now_us + self.SESSION_COOKIE_DAYS * 86400 * 1_000_000
                    result["session_cookies"] += 1
                expiry = expiry_us // (1000 if schema >= 16 else 1_000_000)
                for cid in ids:
                    oa = f"^userContextId={cid}"
                    if cookie["partition_key"]:
                        oa += "&partitionKey=" + cookie["partition_key"]
                    identity = (oa, cookie["name"], cookie["host_key"], cookie["path"])
                    exists = target.execute("SELECT 1 FROM moz_cookies WHERE originAttributes=? AND name=? AND host=? AND path=?",
                                            identity).fetchone()
                    values = [oa, cookie["name"], cookie["value"], cookie["host_key"], cookie["path"], expiry,
                              cookie["last_access_us"] or now_us, cookie["creation_us"] or now_us,
                              int(bool(cookie["is_secure"])), int(bool(cookie["is_httponly"])),
                              cookie["samesite"], cookie["scheme_map"]]
                    if "updateTime" in columns:
                        values.append(now_us)
                    if "isPartitionedAttributeSet" in columns:
                        values.append(int(bool(cookie["partition_key"])))
                    target.execute(f"INSERT INTO moz_cookies ({','.join(names)}) VALUES ({placeholders}) "
                                   f"ON CONFLICT(name,host,path,originAttributes) DO UPDATE SET {updates}", values)
                    result["merged" if exists else "imported"] += 1
                    stats["merged" if exists else "imported"] += 1
        result["partial"] = bool(result["failed"] or result["unsupported"])
        logger.info("Cookies: imported %s, merged %s; skipped %s", result["imported"], result["merged"], result["skipped"])
        return result


def _discover_user_containers(zen_profile: Path) -> list[int]:
    """Read containers.json and return userContextIds for non-internal identities."""
    import json
    cf = zen_profile / "containers.json"
    if not cf.exists():
        return []
    try:
        data = json.loads(cf.read_text())
    except Exception:
        return []
    out: list[int] = []
    for identity in data.get("identities", []):
        cid = identity.get("userContextId", 0)
        # Skip internal (l10nID starting with userContextIdInternal) and the
        # default (0) and the 4294967295 sentinel.
        l10n = identity.get("l10nID", "") or ""
        if cid and cid < 1_000_000 and not l10n.startswith("userContextIdInternal"):
            out.append(cid)
    return sorted(set(out))


def main() -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    parser = argparse.ArgumentParser(description="Import Chromium-format cookies into Zen")
    parser.add_argument("--zen-profile", help="Zen profile name (partial match)")
    parser.add_argument(
        "--containers",
        help="Deprecated: explicit fan-out is rejected. Run the app to create profile mappings first.",
        default="",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    profiles_root = Path.home() / "Library/Application Support/zen/Profiles"
    profiles = [p for p in profiles_root.iterdir() if p.is_dir()] if profiles_root.exists() else []
    if args.zen_profile:
        profiles = [p for p in profiles if args.zen_profile.lower() in p.name.lower()]
    if not profiles:
        logger.error("No matching Zen profile found")
        return 1
    zen_profile = profiles[0]
    logger.info(f"Using Zen profile: {zen_profile.name}")

    container_ids: list[int] = []
    if args.containers == "auto":
        container_ids = _discover_user_containers(zen_profile)
    elif args.containers:
        container_ids = [int(x) for x in args.containers.split(",") if x.strip().isdigit()]
    if container_ids:
        logger.info(f"Will populate containers: {container_ids}")

    importer = CookiesImporter(zen_profile, dry_run=args.dry_run, container_ids=container_ids)
    summary = importer.import_cookies()
    logger.info(f"Summary: {summary}")
    return 0 if not summary.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
