"""Zen-side writer tests.

Run each writer against the fresh empty Zen fixture, then assert the
expected records appear in containers.json / places.sqlite. Validates
the ``to_legacy_dict()`` → writer contract end-to-end.
"""

from __future__ import annotations

import json
import sqlite3

from extractors import ArcExtractor


def _legacy_dict_from_arc():
    return ArcExtractor().extract().to_legacy_dict()


def test_zen_space_importer_creates_container(arc_home, zen_profile):
    from zen_space_importer import ZenProfile, ZenSpaceImporter

    legacy = _legacy_dict_from_arc()
    zp = ZenProfile(name=zen_profile.name, path=zen_profile)
    mappings = ZenSpaceImporter(zp).import_spaces_as_containers(legacy, dry_run=False)

    # Mappings are space_name -> container userContextId.
    assert "Test Space" in mappings

    containers = json.loads((zen_profile / "containers.json").read_text())
    names = {idn["name"] for idn in containers["identities"] if "name" in idn}
    assert "Arc · Default" in names


def test_zen_bookmark_importer_writes_bookmarks(arc_home, zen_profile):
    from zen_bookmark_importer import ZenBookmarkImporter

    legacy = _legacy_dict_from_arc()
    importer = ZenBookmarkImporter(zen_profile)
    ok = importer.import_bookmarks(legacy, dry_run=False)
    assert ok is True

    conn = sqlite3.connect(zen_profile / "places.sqlite")
    try:
        urls = {row[0] for row in conn.execute("SELECT url FROM moz_places")}
    finally:
        conn.close()
    assert "https://example.com/" in urls
    assert "https://mozilla.org/" in urls


def test_zen_bookmark_importer_uses_bookmark_channel(zen_profile):
    """The bookmark importer must read the dedicated ``bookmarks`` channel,
    not ``pinned_tabs`` — so Chromium's real pinned tabs don't get written
    as bookmarks and its bookmarks aren't lost."""
    from zen_bookmark_importer import ZenBookmarkImporter

    legacy = {
        "source": "chrome",
        "total_spaces": 1,
        "spaces": [{
            "space_id": "s1", "space_name": "Default",
            "icon": None, "color": None,
            "total_pinned_tabs": 1, "total_open_tabs": 0, "total_folders": 0,
            "pinned_tabs": [{"url": "https://session-pin.example/", "title": "Pin",
                             "space_id": "s1", "space_name": "Default",
                             "folder_path": [], "tab_id": "", "parent_id": "",
                             "index": 0, "is_essential": False}],
            "open_tabs": [], "folders": [],
            "bookmarks": [{"url": "https://bookmarked.example/", "title": "BM",
                           "space_id": "s1", "space_name": "Default",
                           "folder_path": [], "tab_id": "", "parent_id": "",
                           "index": 0, "is_essential": False}],
            "bookmark_folders": [],
        }],
    }
    assert ZenBookmarkImporter(zen_profile).import_bookmarks(legacy, dry_run=False)

    conn = sqlite3.connect(zen_profile / "places.sqlite")
    try:
        urls = {row[0] for row in conn.execute("SELECT url FROM moz_places")}
    finally:
        conn.close()
    assert "https://bookmarked.example/" in urls
    assert "https://session-pin.example/" not in urls


def test_zen_sessions_importer_writes_session(arc_home, zen_profile):
    """Verify the sessions importer produces a zen-sessions.jsonlz4 file
    (binary, mozLz4-compressed) without raising."""
    from zen_sessions_importer import ZenSessionsImporter

    legacy = _legacy_dict_from_arc()
    importer = ZenSessionsImporter(zen_profile, folders_collapsed=True)
    importer.import_data(legacy, container_mappings={}, dry_run=False)

    out = zen_profile / "zen-sessions.jsonlz4"
    assert out.exists()
    blob = out.read_bytes()
    # mozLz4 magic header
    assert blob.startswith(b"mozLz40\0")


def test_bookmark_backup_ignores_readonly_launch_directory(arc_home, zen_profile, monkeypatch):
    import errno
    from pathlib import Path

    from zen_bookmark_importer import ZenBookmarkImporter

    legacy = _legacy_dict_from_arc()
    monkeypatch.chdir("/")
    original_mkdir = Path.mkdir

    def mkdir(path, *args, **kwargs):
        if path == Path("/backups"):
            raise OSError(errno.EROFS, "Read-only file system", str(path))
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    assert ZenBookmarkImporter(zen_profile).import_bookmarks(legacy)
    backups = list(zen_profile.glob("places.sqlite.backup.*"))
    assert backups
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
