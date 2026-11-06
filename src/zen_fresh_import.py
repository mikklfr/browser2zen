"""Clear migration destinations after saving a restorable profile-data backup."""

import shutil
import sqlite3
import tempfile
import time
from contextlib import closing
from pathlib import Path

from zen_backup import CATEGORY_FILES, ZenBackupExporter, ZenBackupImporter

RESET_CATEGORIES = ["workspaces", "browsing", "cookies", "favicons"]
BOOKMARK_ROOTS = ("root________", "menu________", "toolbar_____",
                  "unfiled_____", "mobile______", "tags________")


def _empty_database(source: Path, destination: Path) -> None:
    # Preserve the installed browser's schema, metadata and bookmark roots.
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(destination)) as empty, empty:
            original.backup(empty)
            empty.execute("PRAGMA foreign_keys=OFF")
            triggers = empty.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'").fetchall()
            for name, _ in triggers:
                empty.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
            tables = empty.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            for (name,) in tables:
                if name.startswith("sqlite_") or name == "moz_meta":
                    continue
                table = '"' + name.replace('"', '""') + '"'
                if source.name == "places.sqlite" and name == "moz_bookmarks":
                    empty.execute(f"DELETE FROM {table} WHERE guid NOT IN ({','.join('?' for _ in BOOKMARK_ROOTS)})",
                                  BOOKMARK_ROOTS)
                else:
                    empty.execute(f"DELETE FROM {table}")
            for _, sql in triggers:
                empty.execute(sql)
            empty.commit()
            if empty.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError(f"Could not prepare a clean {source.name}")


def clear_for_import(profile: Path) -> Path:
    """Back up first, then reset only the explicitly listed migration data."""
    if not profile.is_dir() or profile.is_symlink() or not (profile / "places.sqlite").is_file():
        raise ValueError("Select an existing Zen profile directory")
    targets = list(dict.fromkeys(path for category in RESET_CATEGORIES for path in CATEGORY_FILES[category]))
    for relative in targets:
        target = profile / relative
        if target.is_symlink() or (target.is_dir() and any(p.is_symlink() for p in target.rglob("*"))):
            raise ValueError("Fresh import cannot clear symbolic links")
    with tempfile.TemporaryDirectory(prefix="browser2zen_fresh_") as temp:
        staged = Path(temp)
        for relative in targets:
            source = profile / relative
            if source.is_file() and source.suffix == ".sqlite":
                _empty_database(source, staged / source.name)
        archive = profile / f"before-import-{time.time_ns()}.zenbackup"
        backup = ZenBackupExporter(profile, archive, includes=RESET_CATEGORIES).export()
        if not backup["ok"]:
            raise OSError("Nothing cleared: backup failed: " + "; ".join(backup["errors"]))
        try:
            for relative in targets:
                target = profile / relative
                if not target.exists():
                    continue
                clean = staged / target.name
                if target.suffix == ".sqlite" and clean.exists():
                    # Remove SQLite sidecars only after their contents were backed up.
                    for suffix in ("-wal", "-shm", "-journal"):
                        target.with_name(target.name + suffix).unlink(missing_ok=True)
                    shutil.copy2(clean, target)
                elif target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
        except Exception as exc:
            restored = ZenBackupImporter(archive, profile, includes=RESET_CATEGORIES).import_archive()
            if not restored["ok"]:
                raise OSError(f"Clear failed; restore the backup at {archive}") from exc
            raise OSError(f"Clear failed; original data restored. Backup: {archive}") from exc
        return archive
