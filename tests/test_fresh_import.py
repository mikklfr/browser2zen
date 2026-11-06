"""Fresh imports are opt-in, backed up, and reset only the selected profile."""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from app.orchestrator import MigrationOptions, MigrationOrchestrator
from extractors import ArcExtractor
from zen_backup import ZenBackupImporter
from zen_fresh_import import clear_for_import

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def populated_profile(tmp_path):
    profile = tmp_path / "zen-profile"
    shutil.copytree(FIXTURES / "zen-with-data/Profiles/test.default (release)", profile)
    (profile / ".browser2zen-identities.json").write_text(json.dumps({"profiles": {"arc:Default": 7}, "spaces": {}}))
    (profile / "workspace_setup_guide.json").write_text("{}")
    (profile / "prefs.js").write_text("// keep my settings")
    (profile / "logins.json").write_text("synthetic-saved-passwords")
    (profile / "extensions").mkdir(exist_ok=True)
    (profile / "extensions/keep.xpi").write_bytes(b"synthetic-extension")
    (profile / "sessionstore-backups").mkdir(exist_ok=True)
    (profile / "sessionstore-backups/previous.jsonlz4").write_bytes(b"old-session")
    return profile


def test_clear_backs_up_and_preserves_schema_and_settings(populated_profile):
    profile = populated_profile
    original_sessions = (profile / "zen-sessions.jsonlz4").read_bytes()
    archive = clear_for_import(profile)
    assert archive.is_file()
    assert not (profile / "zen-sessions.jsonlz4").exists()
    assert not (profile / "sessionstore-backups").exists()
    assert not (profile / "containers.json").exists()
    assert not (profile / ".browser2zen-identities.json").exists()
    assert not (profile / "workspace_setup_guide.json").exists()
    assert (profile / "prefs.js").read_text() == "// keep my settings"
    assert (profile / "logins.json").read_text() == "synthetic-saved-passwords"
    assert (profile / "extensions/keep.xpi").read_bytes() == b"synthetic-extension"
    with sqlite3.connect(profile / "places.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM moz_places").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM moz_bookmarks WHERE type=1").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM moz_bookmarks WHERE guid='unfiled_____'").fetchone()[0] == 1
    with sqlite3.connect(profile / "cookies.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM moz_cookies").fetchone()[0] == 0
    assert ZenBackupImporter(archive, profile).import_archive()["ok"]
    assert (profile / "zen-sessions.jsonlz4").read_bytes() == original_sessions
    assert (profile / "sessionstore-backups/previous.jsonlz4").read_bytes() == b"old-session"
    with sqlite3.connect(profile / "cookies.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM moz_cookies").fetchone()[0] > 0


def test_backup_failure_does_not_clear_anything(populated_profile, monkeypatch):
    profile = populated_profile
    originals = {p.relative_to(profile): p.read_bytes() for p in profile.rglob("*") if p.is_file()}
    monkeypatch.setattr("zen_fresh_import.ZenBackupExporter.export", lambda _: {"ok": False, "errors": ["disk full"]})
    with pytest.raises(OSError, match="Nothing cleared"):
        clear_for_import(profile)
    assert all((profile / rel).read_bytes() == content for rel, content in originals.items())


def test_clear_failure_restores_original_data(populated_profile, monkeypatch):
    profile = populated_profile
    sessions = (profile / "zen-sessions.jsonlz4").read_bytes()
    original_unlink = Path.unlink

    def unlink(path, *args, **kwargs):
        if path == profile / "containers.json":
            raise OSError("simulated failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(OSError, match="original data restored"):
        clear_for_import(profile)
    assert (profile / "zen-sessions.jsonlz4").read_bytes() == sessions
    assert (profile / ".browser2zen-identities.json").exists()


def test_clear_then_import_replaces_spaces_instead_of_merging(arc_home, populated_profile, monkeypatch):
    from zen_sessions_importer import read_mozlz4
    monkeypatch.setattr("app.env_check.is_zen_running", lambda: False)
    orch = MigrationOrchestrator(ArcExtractor())
    events = list(orch.migrate(MigrationOptions(
        zen_profile_path=populated_profile, clear_before_import=True, include_favicons=False,
    )))
    assert not orch.errors
    assert any(ev["kind"] == "step_done" and ev["step"] == "clear" for ev in events)
    data = read_mozlz4(populated_profile / "zen-sessions.jsonlz4")
    assert [s["name"] for s in data["spaces"]] == ["Test Space"]
    assert any(p.suffix == ".zenbackup" for p in orch.find_backups(populated_profile))


def test_clear_is_opt_in_and_source_failure_does_not_clear(populated_profile, monkeypatch):
    monkeypatch.setattr("app.env_check.is_zen_running", lambda: False)
    assert not MigrationOptions(zen_profile_path=populated_profile).clear_before_import
    source = ArcExtractor()
    source.extract = lambda: (_ for _ in ()).throw(ValueError("source unavailable"))
    original = (populated_profile / "zen-sessions.jsonlz4").read_bytes()
    orch = MigrationOrchestrator(source)
    list(orch.migrate(MigrationOptions(zen_profile_path=populated_profile, clear_before_import=True)))
    assert orch.errors
    assert (populated_profile / "zen-sessions.jsonlz4").read_bytes() == original
    assert not list(populated_profile.glob("before-import-*.zenbackup"))


def test_empty_source_never_clears(populated_profile, monkeypatch):
    from extractors import ExportData
    monkeypatch.setattr("app.env_check.is_zen_running", lambda: False)
    source = ArcExtractor()
    source.extract = lambda: ExportData(source="arc", spaces=[])
    original = (populated_profile / "zen-sessions.jsonlz4").read_bytes()
    orch = MigrationOrchestrator(source)
    list(orch.migrate(MigrationOptions(zen_profile_path=populated_profile, clear_before_import=True)))
    assert any("No source spaces" in error for error in orch.errors)
    assert (populated_profile / "zen-sessions.jsonlz4").read_bytes() == original


def test_bridge_parses_clear_option_and_nanosecond_backup_dates(populated_profile):
    import time

    from app.bridge import Bridge
    options = {"zenProfilePath": str(populated_profile)}
    assert not Bridge._parse_options(options).clear_before_import
    assert Bridge._parse_options({**options, "clearBeforeImport": True}).clear_before_import
    backup = populated_profile / f"cookies.sqlite.backup.{time.time_ns()}"
    backup.write_bytes(b"synthetic-backup")
    bridge = object.__new__(Bridge)
    items = bridge.list_backups(str(populated_profile))
    assert any(item["name"] == backup.name and item["iso"].startswith("20") for item in items)


def test_restore_legacy_backup_removes_new_identity_mapping(populated_profile):
    mapping = populated_profile / ".browser2zen-identities.json"
    mapping.unlink()
    archive = clear_for_import(populated_profile)
    mapping.write_text(json.dumps({"profiles": {"arc:Default": 99}, "spaces": {}}))
    assert ZenBackupImporter(archive, populated_profile).import_archive()["ok"]
    assert not mapping.exists()
