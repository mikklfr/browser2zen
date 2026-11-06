"""
Zen profile backup + restore.

The migration pipeline takes another browser as input and writes to a
Zen profile. This module is the parallel two-way path for Zen itself:
export the user's current Zen profile to a single portable
``.zenbackup`` file, restore it into a Zen profile on another machine.

The archive format is a gzip-compressed tar with a small ``manifest.json``
at the root and the profile files under ``profile/``. The manifest
carries a ``format_version`` so future browser2zen releases can evolve
the layout without breaking older readers.

What goes in: user picks via the GUI's category toggles. The four
"safe core" categories (workspaces, browsing, cookies, favicons) cover
what almost everyone wants. The riskier three (passwords, prefs,
extensions) default off because their portability is conditional on
the target machine matching the source's NSS / Zen version / pref
shape.

The exporter snapshots SQLite files + their WAL/SHM siblings into a
tempdir before tarring (same pattern as
``chromium_history_importer.HistoryImporter._snapshot``) so we never
read a half-flushed page cache.

The importer takes a ``.backup.<ts>`` of every existing target file
before overwriting (mirrors ``app/bridge.py:restore_backup``) so the
user can roll back via the existing Backups screen.
"""

from __future__ import annotations

import json
import logging
import shutil
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# Bumped when the archive layout or manifest schema changes. The
# importer refuses any other value so a v1.2 install doesn't try to
# read a future-shaped archive.
ARCHIVE_FORMAT_VERSION = 1


# Category → list of profile-relative paths. SQLite files implicitly
# include their WAL/SHM/journal siblings (handled by the snapshot logic
# below). Directories under ``extensions`` recurse.
CATEGORY_FILES: dict[str, tuple[str, ...]] = {
    "workspaces": (
        ".browser2zen-identities.json",
        ".browser2zen-migrated",
        "workspace_setup_guide.json",
        "containers.json",
        "zen-sessions.jsonlz4",
        "sessionstore.jsonlz4",
        "sessionstore-backups",
    ),
    "browsing": (
        "places.sqlite",
    ),
    "cookies": (
        "cookies.sqlite",
    ),
    "favicons": (
        "favicons.sqlite",
    ),
    "passwords": (
        "key4.db",
        "key3.db",
        "logins.json",
        "signons.txt",
    ),
    "prefs": (
        "prefs.js",
        "user.js",
        "xulstore.json",
    ),
    "extensions": (
        "addons.json",
        "extensions.json",
        "addonStartup.json.lz4",
        "extensions",   # directory; recursed below
    ),
    "mods": (
        # Zen Mods (UI customisations, themes) live in the profile's
        # chrome/ directory: userChrome.css, userContent.css, plus any
        # per-mod subfolders the Zen Mods feature drops in there.
        "chrome",       # directory; recursed below — captures everything
    ),
}

# All categories in canonical order — used as default for "include
# everything in the archive" on the importer.
ALL_CATEGORIES = tuple(CATEGORY_FILES.keys())

DEFAULT_CATEGORIES = ("workspaces", "browsing", "cookies", "favicons", "mods")

# SQLite file suffixes that must travel with the main file.
_SQLITE_SIBLINGS = ("-wal", "-shm", "-journal")


# --------------------------------------------------------------------- helpers


def _is_sqlite(rel_path: str) -> bool:
    return rel_path.endswith(".sqlite") or rel_path.endswith(".db")


def _profile_display_name(dir_name: str) -> str:
    # Zen profile dirs are "<random>.<human-readable>" (e.g. "o9i57a6u.Default (release)").
    # Strip the prefix so the manifest shows what the user actually recognises.
    return dir_name.split(".", 1)[1] if "." in dir_name else dir_name


def _sqlite_sibling_paths(src: Path) -> list[Path]:
    """Return the WAL/SHM/journal siblings of a SQLite file, if any exist."""
    return [src.with_name(src.name + suffix)
            for suffix in _SQLITE_SIBLINGS
            if src.with_name(src.name + suffix).exists()]


def _archive_member(rel_path: str) -> str:
    """Where this file lives inside the archive."""
    return f"profile/{rel_path}"


# --------------------------------------------------------------------- exporter


class ZenBackupExporter:
    """Bundle a Zen profile into a single ``.zenbackup`` file."""

    def __init__(
        self,
        zen_profile: Path,
        output_path: Path,
        includes: list[str] | None = None,
    ):
        self.zen_profile = Path(zen_profile)
        self.output_path = Path(output_path)
        self.includes = list(includes) if includes is not None else list(DEFAULT_CATEGORIES)
        unknown = [c for c in self.includes if c not in CATEGORY_FILES]
        if unknown:
            raise ValueError(f"Unknown backup categories: {unknown}")
        self._tempdir: Path | None = None

    def export(self) -> dict:
        result: dict = {
            "ok": False,
            "archive_path": str(self.output_path),
            "file_count": 0,
            "bytes_in": 0,
            "bytes_out": 0,
            "errors": [],
        }
        if not self.zen_profile.is_dir():
            result["errors"].append("zen_profile_missing")
            return result
        if not self.includes:
            result["errors"].append("no_categories_selected")
            return result

        self._tempdir = Path(tempfile.mkdtemp(prefix="browser2zen_export_"))
        staged_root = self._tempdir / "profile"
        staged_root.mkdir(parents=True, exist_ok=True)

        try:
            inventory: list[str] = []

            for category in self.includes:
                for rel_path in CATEGORY_FILES[category]:
                    src = self.zen_profile / rel_path
                    if not src.exists():
                        # Files within optional categories may simply not
                        # be there (e.g. prefs.js exists; user.js usually
                        # doesn't). That's not an error.
                        continue
                    if src.is_dir():
                        self._stage_directory(src, staged_root / rel_path, inventory)
                    elif _is_sqlite(rel_path):
                        self._stage_sqlite(src, staged_root / rel_path, inventory)
                    else:
                        self._stage_file(src, staged_root / rel_path, inventory)

            manifest = {
                "format_version": ARCHIVE_FORMAT_VERSION,
                "browser2zen_version": _read_version(),
                "source_profile_name": _profile_display_name(self.zen_profile.name),
                "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "included": list(self.includes),
                "file_inventory": sorted(inventory),
            }
            (self._tempdir / "manifest.json").write_text(
                json.dumps(manifest, indent=2), encoding="utf-8"
            )

            # Total bytes staged before compression.
            result["bytes_in"] = sum(
                p.stat().st_size for p in self._tempdir.rglob("*") if p.is_file()
            )

            # Tar.gz the staging dir into the output path.
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(self.output_path, "w:gz", compresslevel=6) as tar:
                tar.add(self._tempdir / "manifest.json", arcname="manifest.json")
                if staged_root.exists():
                    tar.add(staged_root, arcname="profile",
                            filter=_strip_owner)

            result["bytes_out"] = self.output_path.stat().st_size
            result["file_count"] = len(inventory)
            result["ok"] = True
            return result
        except Exception as exc:
            logger.exception("zen export failed")
            result["errors"].append(f"export_failed: {exc}")
            return result
        finally:
            self._cleanup()

    # ---- staging helpers ----

    def _stage_file(self, src: Path, dest: Path, inventory: list) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        inventory.append(str(dest.relative_to(self._tempdir / "profile")))

    def _stage_sqlite(self, src: Path, dest: Path, inventory: list) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        inventory.append(str(dest.relative_to(self._tempdir / "profile")))
        for sibling in _sqlite_sibling_paths(src):
            sib_dest = dest.with_name(sibling.name)
            shutil.copy2(sibling, sib_dest)
            inventory.append(str(sib_dest.relative_to(self._tempdir / "profile")))

    def _stage_directory(self, src: Path, dest: Path, inventory: list) -> None:
        if not src.is_dir():
            return
        for entry in src.rglob("*"):
            if entry.is_file():
                rel = entry.relative_to(src)
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(entry, target)
                inventory.append(str(target.relative_to(self._tempdir / "profile")))

    def _cleanup(self) -> None:
        if self._tempdir and self._tempdir.exists():
            shutil.rmtree(self._tempdir, ignore_errors=True)
            self._tempdir = None


def _strip_owner(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Don't leak the source machine's UID/GID into the archive."""
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


# --------------------------------------------------------------------- importer


class ZenBackupImporter:
    """Restore a ``.zenbackup`` archive into a Zen profile."""

    def __init__(
        self,
        archive_path: Path,
        target_zen_profile: Path,
        includes: list[str] | None = None,
    ):
        self.archive_path = Path(archive_path)
        self.target = Path(target_zen_profile)
        # ``includes`` is a per-category allow-list; if None, restore
        # everything the archive contains.
        self.includes = list(includes) if includes is not None else None

    def preview(self) -> dict:
        """Read the manifest without unpacking. Read-only."""
        result: dict = {"ok": False, "errors": []}
        if not self.archive_path.is_file():
            result["errors"].append("archive_missing")
            return result
        try:
            with tarfile.open(self.archive_path, "r:gz") as tar:
                member = tar.getmember("manifest.json")
                fh = tar.extractfile(member)
                if fh is None:
                    result["errors"].append("manifest_unreadable")
                    return result
                manifest = json.loads(fh.read().decode("utf-8"))
        except (tarfile.TarError, KeyError, json.JSONDecodeError) as exc:
            result["errors"].append(f"unreadable_archive: {exc}")
            return result

        if manifest.get("format_version") != ARCHIVE_FORMAT_VERSION:
            result["errors"].append("unsupported_archive_version")
            result["manifest"] = manifest
            return result

        result["ok"] = True
        result["manifest"] = manifest
        result["archive_size"] = self.archive_path.stat().st_size
        return result

    def import_archive(self) -> dict:
        result: dict = {
            "ok": False,
            "restored_files": [],
            "skipped": [],
            "errors": [],
        }

        if not self.archive_path.is_file():
            result["errors"].append("archive_missing")
            return result
        if not self.target.is_dir():
            result["errors"].append("target_profile_missing")
            return result

        preview = self.preview()
        if not preview["ok"]:
            result["errors"].extend(preview["errors"])
            return result
        manifest = preview["manifest"]

        # Resolve which categories to actually restore: the user's
        # opt-in list intersected with what the archive carries. If the
        # caller didn't constrain ``includes``, restore everything in
        # the manifest.
        archive_includes = set(manifest.get("included", []))
        wanted = set(self.includes) if self.includes is not None else archive_includes
        active = wanted & archive_includes
        skipped_categories = wanted - archive_includes
        for cat in skipped_categories:
            result["skipped"].append({"category": cat, "reason": "not_in_archive"})

        if not active:
            result["errors"].append("no_categories_to_restore")
            return result

        active_files: set[str] = set()
        for cat in active:
            cat_files = CATEGORY_FILES.get(cat)
            if cat_files is None:
                # Forward-compat: a newer browser2zen could have written
                # an archive with a category we don't know about. Skip
                # cleanly instead of KeyError-ing.
                result["skipped"].append({"category": cat, "reason": "unknown_category"})
                continue
            for f in cat_files:
                # Both the file itself and any directory prefix are
                # recorded so the per-member filter below can match
                # extension subdirectories like ``extensions/abc/...``.
                active_files.add(f)

        try:
            with tarfile.open(self.archive_path, "r:gz") as tar:
                # Legacy backups have no mapping manifest. A mapping from the
                # newer profile must not survive restoration of older sessions.
                mapping = self.target / ".browser2zen-identities.json"
                inventory = {m.name for m in tar.getmembers()}
                if "workspaces" in active and "profile/.browser2zen-identities.json" not in inventory:
                    self._snapshot_existing(mapping)
                    mapping.unlink(missing_ok=True)
                for member in tar.getmembers():
                    if member.name == "manifest.json":
                        continue
                    if not member.isfile():
                        continue
                    if not member.name.startswith("profile/"):
                        continue
                    rel = member.name[len("profile/"):]
                    if not _matches_active(rel, active_files):
                        continue

                    target_path = self.target / rel
                    self._snapshot_existing(target_path)

                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    fh = tar.extractfile(member)
                    if fh is None:
                        result["skipped"].append({"file": rel, "reason": "unreadable"})
                        continue
                    target_path.write_bytes(fh.read())
                    result["restored_files"].append(rel)

                    # Drop any stale wal/shm so SQLite re-reads the new
                    # page cache cleanly.
                    if _is_sqlite(rel):
                        for suffix in _SQLITE_SIBLINGS:
                            stale = target_path.with_name(target_path.name + suffix)
                            if stale.is_file() and rel + suffix not in {m.name[len('profile/'):] for m in tar.getmembers()}:
                                try:
                                    stale.unlink()
                                except OSError:
                                    pass
        except tarfile.TarError as exc:
            result["errors"].append(f"unreadable_archive: {exc}")
            return result

        try:
            (self.target / ".browser2zen-restored").write_text(
                json.dumps({
                    "ts": time.time(),
                    "format_version": ARCHIVE_FORMAT_VERSION,
                    "source_profile_name": manifest.get("source_profile_name"),
                    "exported_at": manifest.get("exported_at"),
                }),
                encoding="utf-8",
            )
        except OSError:
            pass

        result["ok"] = True
        return result

    @staticmethod
    def _snapshot_existing(target_path: Path) -> None:
        if not target_path.exists() or target_path.is_dir():
            return
        ts = int(time.time())
        backup = target_path.with_name(f"{target_path.name}.backup.{ts}")
        try:
            shutil.copy2(target_path, backup)
        except OSError as exc:
            logger.warning("could not snapshot %s before overwrite: %s", target_path, exc)


def _matches_active(rel: str, active_files: set[str]) -> bool:
    """Whether ``rel`` (a profile-relative archive path) should be restored.

    The archive may carry SQLite WAL/SHM siblings (``places.sqlite-wal``)
    that aren't enumerated in CATEGORY_FILES verbatim — they get included
    if their parent (``places.sqlite``) is active. Same for files inside
    the ``extensions/`` directory.
    """
    if rel in active_files:
        return True
    # WAL / SHM / journal siblings.
    for suffix in _SQLITE_SIBLINGS:
        if rel.endswith(suffix):
            base = rel[: -len(suffix)]
            if base in active_files:
                return True
    # Recurse-into-directory members.
    for entry in active_files:
        if entry and rel.startswith(entry + "/"):
            return True
    return False


def _read_version() -> str:
    """Best-effort lookup of the current browser2zen version."""
    try:
        from app.__version__ import VERSION  # type: ignore[import-not-found]
        return VERSION
    except Exception:
        return "unknown"
