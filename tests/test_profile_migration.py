"""Account isolation across real extractor/session/cookie writer boundaries."""

import hashlib
import json
import sqlite3
import time

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from chromium_cookies_importer import CookiesImporter, _decrypt_v10_cbc, _partition_key
from extractors import ArcExtractor
from migration_state import STATE_FILE
from zen_sessions_importer import ZenSessionsImporter, read_mozlz4, write_mozlz4
from zen_space_importer import ZenProfile, ZenSpaceImporter


def space(sid, name, profile):
    return {"space_id": sid, "space_name": name, "source_profile": profile,
            "pinned_tabs": [{"url": "https://account.test/", "is_essential": True}],
            "open_tabs": [{"url": "https://open.test/"}],
            "folders": [{"folder_id": f"f-{sid}", "title": "Folder"}]}


def source_db(tmp_path, profile, value, network=False, version=23, encrypted=b"", partition=""):
    db = tmp_path / "source" / profile
    if network:
        db /= "Network"
    db /= "Cookies"
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('version', ?)", (str(version),))
        conn.execute("""CREATE TABLE cookies (
            host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, path TEXT,
            expires_utc INTEGER, creation_utc INTEGER, last_access_utc INTEGER,
            is_secure INTEGER, is_httponly INTEGER, samesite INTEGER,
            has_expires INTEGER, source_scheme INTEGER, top_frame_site_key TEXT)""")
        expiry = int((time.time() + 86400 + 11644473600) * 1_000_000)
        conn.execute("INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     ("account.test", "session", value, encrypted, "/", expiry, 0, 0, 1, 1, -1, 1, 2, partition))
    return db


def target_db(profile, schema=17):
    with sqlite3.connect(profile / "cookies.sqlite") as conn:
        conn.execute(f"PRAGMA user_version={schema}")
        conn.execute("""CREATE TABLE moz_cookies (
            originAttributes TEXT, name TEXT, value TEXT, host TEXT, path TEXT,
            expiry INTEGER, lastAccessed INTEGER, creationTime INTEGER,
            isSecure INTEGER, isHttpOnly INTEGER, sameSite INTEGER, schemeMap INTEGER,
            UNIQUE(name, host, path, originAttributes))""")
        if schema >= 17:
            conn.execute("ALTER TABLE moz_cookies ADD COLUMN updateTime INTEGER")
            conn.execute("ALTER TABLE moz_cookies ADD COLUMN isPartitionedAttributeSet INTEGER DEFAULT 0")


def import_cookies(monkeypatch, target, routes, key=b"", decrypt=None):
    importer = CookiesImporter(target, cookie_routes=routes)
    monkeypatch.setattr(importer, "_resolve_key_and_decrypt_fn",
                        lambda: (key, decrypt or (lambda *_: None), None))
    return importer.import_cookies()


def test_arc_profile_survives_both_serializers(arc_home):
    sidebar = arc_home / "Library/Application Support/Arc/StorableSidebar.json"
    fixture = json.loads(sidebar.read_text())
    fixture["sidebar"]["containers"][1]["spaces"][1]["profile"] = {
        "custom": {"_0": {"directoryBasename": "Profile 1"}}}
    sidebar.write_text(json.dumps(fixture))
    export = ArcExtractor().extract()
    assert export.to_dict()["spaces"][0]["source_profile"] == "Profile 1"
    assert export.to_legacy_dict()["spaces"][0]["source_profile"] == "Profile 1"


def test_profile_sharing_duplicate_labels_and_rerun(tmp_path):
    payload = {"source": "arc", "spaces": [space("a", "Same", "Default"),
                                           space("b", "Other", "Default"),
                                           space("c", "Same", "Profile 1")]}
    importer = ZenSpaceImporter(ZenProfile("test", tmp_path))
    mappings = importer.import_spaces_as_containers(payload)
    assert mappings["a"] == mappings["b"] != mappings["c"]
    assert "Same" not in mappings
    sessions = ZenSessionsImporter(tmp_path)
    assert sessions.import_data(payload, mappings)
    first = read_mozlz4(tmp_path / "zen-sessions.jsonlz4")
    assert len(first["spaces"]) == 3
    by_uuid = {w["uuid"]: w["containerTabId"] for w in first["spaces"]}
    assert all(t["userContextId"] == by_uuid[t["zenWorkspace"]] for t in first["tabs"])
    assert all(t["zenDefaultUserContextId"] == t["userContextId"] for t in first["tabs"])
    assert importer.import_spaces_as_containers(payload) == mappings
    payload["spaces"][0]["space_name"] = "Renamed"
    assert sessions.import_data(payload, mappings)
    second = read_mozlz4(tmp_path / "zen-sessions.jsonlz4")
    assert [s["uuid"] for s in second["spaces"]] == [s["uuid"] for s in first["spaces"]]
    assert second["spaces"][0]["name"] == "Renamed"
    assert len(second["tabs"]) == len(first["tabs"])
    assert len(second["folders"]) == len(first["folders"])
    assert first["spaces"][0]["uuid"] in (tmp_path / "prefs.js").read_text()


def test_repair_legacy_preserves_ids_and_unrelated_state(tmp_path):
    payload = {"source": "arc", "spaces": [space("a", "Work", "Profile 1")]}
    sessions = ZenSessionsImporter(tmp_path)
    old_space, folders, tabs = sessions._process_space(payload["spaces"][0])
    unrelated = {"uuid": "unrelated", "name": "Personal", "containerTabId": 99}
    old = {"spaces": [old_space, unrelated], "tabs": tabs + [
        {"zenWorkspace": "unrelated", "userContextId": 99, "entries": [{"url": "https://unrelated.test"}]}],
        "folders": folders}
    write_mozlz4(tmp_path / "zen-sessions.jsonlz4", old)
    (tmp_path / "workspace_setup_guide.json").write_text(json.dumps({
        "zen_workspace_setup_guide": {"container_mappings": {"Work": 4}}}))
    assert sessions.import_data(payload, {"a": 7})
    repaired = read_mozlz4(tmp_path / "zen-sessions.jsonlz4")
    assert repaired["spaces"][0]["uuid"] == old_space["uuid"]
    assert repaired["spaces"][0]["containerTabId"] == 7
    assert repaired["spaces"][1] == unrelated
    assert [t.get("zenSyncId") for t in repaired["tabs"]] == [t.get("zenSyncId") for t in old["tabs"]]
    assert all(t["userContextId"] == 7 for t in repaired["tabs"][:-1])
    assert repaired["tabs"][-1] == old["tabs"][-1]
    assert list(tmp_path.glob("zen-sessions.jsonlz4.backup.*"))


def test_same_name_without_migration_provenance_is_not_adopted(tmp_path):
    write_mozlz4(tmp_path / "zen-sessions.jsonlz4", {
        "spaces": [{"uuid": "native", "name": "Work", "containerTabId": 99}], "tabs": []})
    assert ZenSessionsImporter(tmp_path).import_data({"source": "arc", "spaces": [space("a", "Work", "Default")]}, {"a": 7})
    data = read_mozlz4(tmp_path / "zen-sessions.jsonlz4")
    assert len(data["spaces"]) == 2
    assert data["spaces"][0]["containerTabId"] == 99


@pytest.mark.parametrize("schema", [15, 16, 17])
def test_cookie_accounts_are_routed_without_touching_other_contexts(tmp_path, monkeypatch, schema):
    target_db(tmp_path, schema)
    a = source_db(tmp_path, "Default", "personal")
    b = source_db(tmp_path, "Profile 1", "work", network=True)
    with sqlite3.connect(tmp_path / "cookies.sqlite") as conn:
        conn.execute("INSERT INTO moz_cookies (originAttributes,name,host,path,value) VALUES ('','session','account.test','/','existing')")
    result = import_cookies(monkeypatch, tmp_path, {a: [7], b: [8]})
    assert result["imported"] == 2 and result["failed"] == 0
    with sqlite3.connect(tmp_path / "cookies.sqlite") as conn:
        rows = conn.execute("SELECT originAttributes,value,expiry,sameSite FROM moz_cookies").fetchall()
    assert {r[0]: r[1] for r in rows} == {"": "existing", "^userContextId=7": "personal", "^userContextId=8": "work"}
    assert all(r[3] == 256 for r in rows if r[0])
    now = time.time() * (1000 if schema >= 16 else 1)
    assert all(now < r[2] < now + 90_000 * (1000 if schema >= 16 else 1) for r in rows if r[0])
    assert import_cookies(monkeypatch, tmp_path, {a: [7], b: [8]})["merged"] == 2


@pytest.mark.parametrize("value", [b"", b"token", "unicode-\u00e9".encode()])
def test_v24_encrypted_host_digest(tmp_path, monkeypatch, value):
    target_db(tmp_path)
    key = b"0123456789abcdef"
    plaintext = hashlib.sha256(b"account.test").digest() + value
    pad = 16 - len(plaintext) % 16
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    blob = b"v10" + encryptor.update(plaintext + bytes([pad]) * pad) + encryptor.finalize()
    db = source_db(tmp_path, "Default", "", version=24, encrypted=blob)
    result = import_cookies(monkeypatch, tmp_path, {db: [7]}, key, _decrypt_v10_cbc)
    assert result["imported"] == 1
    with sqlite3.connect(tmp_path / "cookies.sqlite") as conn:
        assert conn.execute("SELECT value FROM moz_cookies").fetchone()[0] == value.decode()
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE cookies SET host_key='different.test'")
    failed = import_cookies(monkeypatch, tmp_path, {db: [7]}, key, _decrypt_v10_cbc)
    assert failed["error"] == "cookie_decode_failed" and failed["failed"] == 1


def test_failed_and_partitioned_cookies_are_reported(tmp_path, monkeypatch):
    target_db(tmp_path)
    db = source_db(tmp_path, "Default", "", encrypted=b"v20-unsupported")
    result = import_cookies(monkeypatch, tmp_path, {db: [7]})
    assert result["error"] == "cookie_decode_failed"
    assert result["skipped"] == result["failed"] == 1
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE cookies SET top_frame_site_key='file://unsupported'")
    result = import_cookies(monkeypatch, tmp_path, {db: [7]})
    assert result["unsupported"] == 1
    assert result["imported"] == 0


def test_partitioned_cookie_retains_container_and_top_level_site(tmp_path, monkeypatch):
    target_db(tmp_path)
    db = source_db(tmp_path, "Default", "embedded-session", partition="https://partition.test")
    result = import_cookies(monkeypatch, tmp_path, {db: [7]})
    assert result["imported"] == 1 and result["unsupported"] == 0
    with sqlite3.connect(tmp_path / "cookies.sqlite") as conn:
        rows = conn.execute("SELECT originAttributes,value,isPartitionedAttributeSet FROM moz_cookies").fetchall()
    assert rows == [("^userContextId=7&partitionKey=%28https%2Cpartition.test%29", "embedded-session", 1)]


@pytest.mark.parametrize("site,host,scheme,ancestor,key", [
    ("https://example.test", "example.test", 2, 0, "(https,example.test)"),
    ("https://example.test", ".sub.example.test", 2, 1, "(https,example.test,f)"),
    ("https://example.test", "other.test", 2, 1, "(https,example.test)"),
    ("https://example.test", "example.test", 1, 1, "(https,example.test)"),
    ("http://localhost", "localhost", 1, 0, "(http,localhost)"),
    ("https://[::1]", "[::1]", 2, 0, "(https,[::1])"),
])
def test_partition_ancestor_translation(site, host, scheme, ancestor, key):
    from urllib.parse import quote_plus
    assert _partition_key(site, host, scheme, ancestor) == quote_plus(key.replace(":", "+"), safe="")


@pytest.mark.parametrize("site", ["opaque", "file:///tmp/x", "https://user:secret@example.test",
                                   "https://example.test/path", "https://example.test?key=x",
                                   "https://example.test:8443", "https://bad%2Chost.test"])
def test_invalid_partition_is_not_flattened(site):
    with pytest.raises(ValueError):
        _partition_key(site, "account.test", 2, 1)


def test_bulk_partitioned_cookies_remain_separate_on_rerun(tmp_path, monkeypatch):
    target_db(tmp_path)
    db = source_db(tmp_path, "Default", "embedded-session", partition="https://site0.test")
    with sqlite3.connect(db) as conn:
        template = list(conn.execute("SELECT * FROM cookies").fetchone())
        rows = []
        for n in range(1, 998):
            row = list(template)
            row[-1] = f"https://site{n}.test"
            rows.append(row)
        unpartitioned = list(template)
        unpartitioned[2] = "top-level-session"
        unpartitioned[-1] = ""
        rows.append(unpartitioned)
        conn.executemany("INSERT INTO cookies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    result = import_cookies(monkeypatch, tmp_path, {db: [7]})
    assert result["imported"] == 999 and result["unsupported"] == 0 and not result["partial"]
    with sqlite3.connect(tmp_path / "cookies.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM moz_cookies WHERE isPartitionedAttributeSet=1").fetchone()[0] == 998
        assert conn.execute("SELECT value FROM moz_cookies WHERE originAttributes='^userContextId=7'").fetchone()[0] == "top-level-session"
        assert conn.execute("SELECT COUNT(*) FROM moz_cookies WHERE originAttributes=''").fetchone()[0] == 0
    assert import_cookies(monkeypatch, tmp_path, {db: [7]})["merged"] == 999


def test_missing_routes_rejects_unsafe_legacy_fanout(tmp_path):
    target_db(tmp_path)
    db = source_db(tmp_path, "Default", "personal")
    assert CookiesImporter(tmp_path, cookie_dbs=[db], container_ids=[7, 8]).import_cookies()["error"] == "cookie_routes_required"


def test_routes_use_selected_source_profiles_only(tmp_path):
    from app.orchestrator import MigrationOrchestrator
    a = source_db(tmp_path, "Default", "personal")
    b = source_db(tmp_path, "Profile 1", "work", network=True)
    source = ArcExtractor()
    source.cookie_db_paths = lambda: [a, b]
    orch = MigrationOrchestrator(source)
    assert orch._cookie_routes({"spaces": [space("a", "Work", "Profile 1")]}, {"a": 7}) == {b: [7]}
    with pytest.raises(RuntimeError, match="No cookie database"):
        orch._cookie_routes({"spaces": [space("a", "Work", "Missing")]}, {"a": 7})


def test_corrupt_identity_manifest_stops_container_import(tmp_path):
    (tmp_path / STATE_FILE).write_text("not-json")
    importer = ZenSpaceImporter(ZenProfile("test", tmp_path))
    assert not importer.import_spaces_as_containers({"spaces": [space("a", "Work", "Default")]})
    assert not (tmp_path / "containers.json").exists()


def test_orchestrator_profile_routing_and_partial_failure(tmp_path, monkeypatch):
    from app.orchestrator import MigrationOptions, MigrationOrchestrator
    from extractors import ExportData, SpaceRecord, TabRecord

    monkeypatch.setattr("app.env_check.is_zen_running", lambda: False)
    target_db(tmp_path)
    a = source_db(tmp_path, "Default", "personal")
    b = source_db(tmp_path, "Profile 1", "work", network=True)
    source = ArcExtractor()
    source.extract = lambda: ExportData(source="arc", spaces=[
        SpaceRecord(space_id=sid, space_name=name, source_profile=profile,
                    pinned_tabs=[TabRecord(url="https://account.test/")])
        for sid, name, profile in (("a", "Personal", "Default"),
                                   ("b", "Second", "Default"), ("c", "Work", "Profile 1"))])
    source.cookie_db_paths = lambda: [a, b]
    monkeypatch.setattr(CookiesImporter, "_resolve_key_and_decrypt_fn", lambda _: (b"", lambda *_: None, None))
    options = MigrationOptions(zen_profile_path=tmp_path, include_cookies=True,
                               include_bookmarks=False, include_favicons=False)
    orch = MigrationOrchestrator(source)
    list(orch.migrate(options))
    assert not orch.errors
    session = read_mozlz4(tmp_path / "zen-sessions.jsonlz4")
    ids = {s["name"]: s["containerTabId"] for s in session["spaces"]}
    assert ids["Personal"] == ids["Second"] != ids["Work"]
    with sqlite3.connect(tmp_path / "cookies.sqlite") as conn:
        values = dict(conn.execute("SELECT originAttributes,value FROM moz_cookies"))
    assert values == {f"^userContextId={ids['Personal']}": "personal", f"^userContextId={ids['Work']}": "work"}
    with sqlite3.connect(b) as conn:
        conn.execute("UPDATE cookies SET value='', encrypted_value=?", (b"bad",))
    list(orch.migrate(options))
    assert any("Imported some cookies" in error for error in orch.errors)
    assert len(read_mozlz4(tmp_path / "zen-sessions.jsonlz4")["tabs"]) == 3


def test_corrupt_sessions_not_overwritten(tmp_path):
    path = tmp_path / "zen-sessions.jsonlz4"
    path.write_bytes(b"corrupt")
    assert not ZenSessionsImporter(tmp_path).import_data({"spaces": [space("a", "Work", "Default")]}, {"a": 7})
    assert path.read_bytes() == b"corrupt"


def test_cbc_rejects_invalid_padding():
    key = b"0123456789abcdef"
    enc = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    blob = b"v10" + enc.update(b"0123456789abcdef") + enc.finalize()
    assert _decrypt_v10_cbc(blob, key) is None
