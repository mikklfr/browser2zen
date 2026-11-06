"""Read-only diagnosis: only synthetic browser data in a temporary directory.

Run with the project's runtime dependencies installed:
    python docs/research/reproduce_arc_zen.py
Exit 1 means the current migration still violates the checked invariants.
"""

import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from chromium_cookies_importer import CookiesImporter
from extractors.arc import ArcExtractor
from zen_sessions_importer import ZenSessionsImporter, read_mozlz4


def main():
    failures = []

    def check(label, passed):
        print(f"{'PASS' if passed else 'FAIL'}: {label}")
        if not passed:
            failures.append(label)

    with tempfile.TemporaryDirectory(prefix="browser2zen_diagnosis_") as tmp:
        home = Path(tmp)
        sidebar = home / "Library/Application Support/Arc/StorableSidebar.json"
        sidebar.parent.mkdir(parents=True)
        fixture = json.loads((ROOT / "tests/fixtures/arc/StorableSidebar.json").read_text())
        fixture["sidebar"]["containers"][1]["spaces"][1]["profile"] = {
            "custom": {"_0": {"directoryBasename": "Profile 1"}}
        }
        sidebar.write_text(json.dumps(fixture))
        with patch.object(Path, "home", return_value=home):
            export = ArcExtractor().extract().to_legacy_dict()
        check("Arc space retains source profile 'Profile 1' in writer payload",
              "Profile 1" in json.dumps(export))

        zen = home / "zen"
        zen.mkdir()
        payload = {"spaces": [{
            "space_id": "s1", "space_name": "Work", "folders": [],
            "pinned_tabs": [{"url": "https://example.test", "title": "Pinned"}],
            "open_tabs": [{"url": "https://example.test", "title": "Open"}],
        }]}
        check("session importer succeeds", ZenSessionsImporter(zen).import_data(payload, {"Work": 7}))
        session = read_mozlz4(zen / "zen-sessions.jsonlz4")
        check("workspace is bound to mapped container 7", session["spaces"][0]["containerTabId"] == 7)
        check("pinned and open tabs both use mapped container 7",
              all(tab["userContextId"] == 7 for tab in session["tabs"]))

        with sqlite3.connect(zen / "cookies.sqlite") as conn:
            conn.execute("PRAGMA user_version=17")
            conn.execute("""CREATE TABLE moz_cookies (
                originAttributes TEXT, name TEXT, value TEXT, host TEXT, path TEXT,
                expiry INTEGER, lastAccessed INTEGER, creationTime INTEGER,
                isSecure INTEGER, isHttpOnly INTEGER, sameSite INTEGER,
                schemeMap INTEGER, updateTime INTEGER,
                UNIQUE(originAttributes, name, host, path))""")
        dbs = []
        for profile, value in (("Default", "synthetic-personal"), ("Profile 1", "synthetic-work")):
            db = home / "source" / profile / "Cookies"
            db.parent.mkdir(parents=True)
            with sqlite3.connect(db) as conn:
                conn.execute("""CREATE TABLE cookies (
                    host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, path TEXT,
                    expires_utc INTEGER, creation_utc INTEGER, last_access_utc INTEGER,
                    is_secure INTEGER, is_httponly INTEGER, samesite INTEGER,
                    has_expires INTEGER, source_scheme INTEGER)""")
                expiry = int((time.time() + 86400 + 11644473600) * 1000000)
                conn.execute("INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             ("example.test", "session", value, b"", "/", expiry, 0, 0, 1, 1, 1, 1, 2))
            dbs.append(db)
        importer = CookiesImporter(zen, cookie_routes={dbs[0]: [7], dbs[1]: [8]})
        # Plaintext synthetic rows bypass OS credentials; real database read/write stays intact.
        with patch.object(importer, "_resolve_key_and_decrypt_fn", return_value=(b"", lambda *_: None, None)):
            summary = importer.import_cookies()
        with sqlite3.connect(zen / "cookies.sqlite") as conn:
            contexts = dict(conn.execute("SELECT originAttributes, value FROM moz_cookies"))
        check("personal and work cookie contexts preserve different accounts",
              contexts.get("^userContextId=7") == "synthetic-personal"
              and contexts.get("^userContextId=8") == "synthetic-work")
        check("unrelated default cookie context stays untouched", "" not in contexts)
        print(f"Cookie counters: read={summary['read']}, inserted={summary['imported']}, merged={summary['merged']}")
        # An all-failed decryption batch must not look like successful zero-cookie import.
        with sqlite3.connect(dbs[0]) as conn:
            conn.execute("UPDATE cookies SET value='', encrypted_value=?", (b"synthetic-encrypted",))
        failed = CookiesImporter(zen, cookie_routes={dbs[0]: [7]})
        with patch.object(failed, "_resolve_key_and_decrypt_fn", return_value=(b"", lambda *_: None, None)):
            result = failed.import_cookies()
        check("all cookie decryptions failing returns a machine-readable failure",
              bool(result.get("error")))
    print(f"{len(failures)} invariant(s) failed; no live browser data accessed.")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
