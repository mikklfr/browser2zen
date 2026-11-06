#!/usr/bin/env python3
"""
Zen Sessions Importer

Imports Arc spaces, pinned tabs, and folders into Zen browser's
zen-sessions.jsonlz4 file. This is the modern storage format used
by Zen 1.18+ which replaced the legacy zen_pins/zen_workspaces SQLite tables.

Format: mozlz4 (8-byte magic + 4-byte LE size + lz4 block compressed JSON)
"""

import json
import logging
import shutil
import struct
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import lz4.block
except ImportError:
    raise ImportError(
        "The 'lz4' package is required for Zen 1.18+ support.\n"
        "Install it with: pip install lz4"
    )

from migration_state import backup_file, container_for, load_state, save_state, space_key

logger = logging.getLogger(__name__)

MOZLZ4_MAGIC = b'mozLz40\0'


def read_mozlz4(file_path: Path) -> dict:
    """Read and decompress a mozlz4 file, return parsed JSON."""
    with open(file_path, 'rb') as f:
        magic = f.read(8)
        if magic != MOZLZ4_MAGIC:
            raise ValueError(f"Not a mozlz4 file (magic: {magic!r})")
        size = struct.unpack('<I', f.read(4))[0]
        compressed = f.read()
    decompressed = lz4.block.decompress(compressed, uncompressed_size=size)
    return json.loads(decompressed.decode('utf-8'))


def write_mozlz4(file_path: Path, data: dict) -> None:
    """Serialize JSON data and write as mozlz4 file."""
    json_bytes = json.dumps(data, separators=(',', ':')).encode('utf-8')
    compressed = lz4.block.compress(json_bytes, store_size=False)
    temporary = file_path.with_name(file_path.name + ".tmp")
    with open(temporary, 'wb') as f:
        f.write(MOZLZ4_MAGIC)
        f.write(struct.pack('<I', len(json_bytes)))
        f.write(compressed)
    temporary.replace(file_path)


class ZenSessionsImporter:
    """Imports Arc spaces, pinned tabs, and folders into zen-sessions.jsonlz4."""

    def __init__(self, zen_profile_path: Path, folders_collapsed: bool = True):
        self.zen_profile = zen_profile_path
        self.sessions_file = zen_profile_path / "zen-sessions.jsonlz4"
        self.folders_collapsed = folders_collapsed
        self._id_counter = 0

    # --- ID generation ---

    def _generate_space_uuid(self) -> str:
        return "{" + str(uuid.uuid4()) + "}"

    def _generate_sync_id(self) -> str:
        """Generate a TIMESTAMP-NUMBER format ID (used for zenSyncId and folder id)."""
        self._id_counter += 1
        return f"{int(time.time() * 1000)}-{self._id_counter}"

    # --- Backup ---

    def _backup_sessions(self) -> bool:
        if not self.sessions_file.exists():
            return True
        try:
            timestamp = int(datetime.now().timestamp())
            backup_path = self.zen_profile / f"zen-sessions.jsonlz4.backup.{timestamp}"
            shutil.copy2(self.sessions_file, backup_path)
            logger.info(f"  Backed up zen-sessions.jsonlz4 to {backup_path.name}")
            return True
        except Exception as e:
            logger.error(f"Failed to backup zen-sessions.jsonlz4: {e}")
            return False

    # --- Read existing data ---

    def _read_existing(self) -> dict:
        if self.sessions_file.exists():
            try:
                return read_mozlz4(self.sessions_file)
            except Exception as e:
                raise ValueError("Cannot read existing Zen sessions; restore a backup before importing.") from e

        return {
            "spaces": [],
            "tabs": [],
            "folders": [],
            "groups": [],
            "splitViewData": [],
            "lastCollected": int(time.time() * 1000),
        }

    # --- Build structures ---

    def _build_space(self, space_data: dict) -> dict:
        space = {
            "uuid": self._generate_space_uuid(),
            "name": space_data["space_name"],
            "theme": {
                "type": "gradient",
                "gradientColors": [],
                "opacity": 0.5,
                "texture": 0,
            },
            "containerTabId": 0,
            "hasCollapsedPinnedTabs": False,
        }

        icon = space_data.get("icon")
        if icon:
            space["icon"] = icon

        color = space_data.get("color")
        if color and all(k in color for k in ("r", "g", "b")):
            r = int(round(color["r"] * 255))
            g = int(round(color["g"] * 255))
            b = int(round(color["b"] * 255))
            space["theme"]["gradientColors"] = [{
                "c": [r, g, b],
                "isCustom": False,
                "algorithm": "floating",
                "isPrimary": True,
            }]

        return space

    def _build_tab(self, tab_data: dict, workspace_uuid: str,
                   index: int, folder_id: str | None = None) -> dict:
        url = tab_data.get("url", "")
        title = tab_data.get("title", "")
        is_essential = tab_data.get("is_essential", False)
        timestamp_ms = int(time.time() * 1000)

        tab = {
            "entries": [{"url": url, "title": title}],
            "lastAccessed": timestamp_ms,
            "pinned": True,
            "hidden": False,
            "zenWorkspace": workspace_uuid,
            "zenSyncId": self._generate_sync_id(),
            "zenEssential": is_essential,
            "zenDefaultUserContextId": None,
            "zenPinnedIcon": None,
            "zenIsEmpty": False,
            "zenHasStaticIcon": False,
            "zenGlanceId": None,
            "zenIsGlance": False,
            "_zenPinnedInitialState": {
                "entry": {"url": url, "title": title},
                "image": None,
            },
            "searchMode": None,
            "userContextId": 0,
            "attributes": {},
            "index": index,
        }

        if folder_id:
            tab["groupId"] = folder_id

        return tab

    def _build_open_tab(self, tab_data: dict, workspace_uuid: str,
                         user_context_id: int, index: int) -> dict:
        """Build a non-pinned open-tab entry that lives in zen-sessions.jsonlz4.

        Open tabs in modern Zen are restored from this file, NOT from
        sessionstore.jsonlz4 (Zen's #restoreWindowData() overwrites
        sessionstore on every launch). The schema mirrors a native Zen
        open tab: ``entries`` carrying the URL/title, ``zenWorkspace``
        binding the tab to the right workspace, and ``userContextId``
        scoping cookies to the per-space container.
        """
        url = tab_data.get("url", "")
        title = tab_data.get("title", "")
        timestamp_ms = int(time.time() * 1000)

        return {
            "entries": [{"url": url, "title": title}],
            "lastAccessed": timestamp_ms,
            "pinned": False,
            "hidden": False,
            "zenWorkspace": workspace_uuid,
            "zenSyncId": self._generate_sync_id(),
            "zenEssential": False,
            "zenDefaultUserContextId": None,
            "zenIsEmpty": False,
            "zenHasStaticIcon": False,
            "zenGlanceId": None,
            "zenIsGlance": False,
            "searchMode": None,
            "userContextId": user_context_id,
            "attributes": {},
            "index": index,
        }

    def _build_folder(self, folder_data: dict, workspace_uuid: str,
                      parent_folder_id: str | None = None) -> dict:
        return {
            "pinned": True,
            "splitViewGroup": False,
            "id": self._generate_sync_id(),
            "name": folder_data.get("title", "Untitled"),
            "collapsed": self.folders_collapsed,
            "saveOnWindowClose": True,
            "parentId": parent_folder_id,
            "prevSiblingInfo": None,
            "emptyTabIds": [],
            "userIcon": "",
            "workspaceId": workspace_uuid,
        }

    # --- Core import logic ---

    def _process_space(self, space_data: dict,
                       user_context_id: int = 0) -> tuple[dict, list[dict], list[dict]]:
        """Process a single Arc space into Zen space, folders, and tabs
        (pinned + open).

        Every tab and its workspace use the same source profile container.
        Returns (space_dict, folders_list, tabs_list).
        """
        space = self._build_space(space_data)
        space["containerTabId"] = user_context_id
        workspace_uuid = space["uuid"]

        # Build folders with hierarchy
        arc_folders = space_data.get("folders", [])
        arc_folder_id_to_zen_id: dict[str, str] = {}
        zen_folders: list[dict] = []

        # Sort folders by index to preserve Arc ordering
        sorted_folders = sorted(arc_folders, key=lambda f: f.get("index", 0))

        # Map arc folder_id to data for parent lookups
        arc_folder_lookup = {f["folder_id"]: f for f in sorted_folders if f.get("folder_id")}

        def create_folder_recursive(folder_data: dict) -> str:
            """Create a folder and its parents first. Returns zen folder ID."""
            arc_id = folder_data.get("folder_id", "")
            if arc_id in arc_folder_id_to_zen_id:
                return arc_folder_id_to_zen_id[arc_id]

            # Create parent first if needed
            parent_zen_id = None
            parent_arc_id = folder_data.get("parent_id", "")
            if parent_arc_id and parent_arc_id in arc_folder_lookup:
                parent_zen_id = create_folder_recursive(arc_folder_lookup[parent_arc_id])

            zen_folder = self._build_folder(folder_data, workspace_uuid, parent_zen_id)
            zen_folders.append(zen_folder)

            if arc_id:
                arc_folder_id_to_zen_id[arc_id] = zen_folder["id"]

            # Also map by title for folder_path lookups from tabs
            arc_folder_id_to_zen_id[folder_data.get("title", "")] = zen_folder["id"]

            return zen_folder["id"]

        for folder_data in sorted_folders:
            create_folder_recursive(folder_data)

        # Build tabs preserving Arc order
        pinned_tabs = space_data.get("pinned_tabs", [])
        zen_tabs: list[dict] = []

        for i, tab_data in enumerate(pinned_tabs):
            url = tab_data.get("url", "")
            if not url:
                continue

            # Resolve folder assignment
            folder_id = None
            folder_path = tab_data.get("folder_path", [])
            if folder_path:
                # Use the immediate parent folder (last element in path)
                immediate_parent = folder_path[-1]
                folder_id = arc_folder_id_to_zen_id.get(immediate_parent)

            zen_tab = self._build_tab(
                tab_data, workspace_uuid,
                index=i + 1, folder_id=folder_id,
            )
            zen_tab["userContextId"] = user_context_id
            zen_tab["zenDefaultUserContextId"] = user_context_id
            zen_tabs.append(zen_tab)

        # Open (non-pinned) tabs from Arc become open tabs in the Zen
        # workspace. They live in the same ``tabs`` array as pinned tabs;
        # Zen distinguishes them via the ``pinned`` flag.
        open_tabs = space_data.get("open_tabs", []) or []
        base_index = len(zen_tabs)
        for j, tab_data in enumerate(open_tabs):
            url = tab_data.get("url", "")
            if not url:
                continue
            zen_tabs.append(self._build_open_tab(
                tab_data, workspace_uuid,
                user_context_id=user_context_id,
                index=base_index + j + 1,
            ))

        # Create about:blank placeholder tab per folder - Zen requires this for validation
        timestamp_ms = int(time.time() * 1000)
        for folder in zen_folders:
            placeholder_sync_id = self._generate_sync_id()
            placeholder = {
                "entries": [{"url": "about:blank", "triggeringPrincipal_base64": "{\"3\":{}}"}],
                "lastAccessed": timestamp_ms,
                "pinned": True,
                "hidden": False,
                "groupId": folder["id"],
                "zenWorkspace": workspace_uuid,
                "zenSyncId": placeholder_sync_id,
                "zenEssential": False,
                "zenDefaultUserContextId": None,
                "zenPinnedIcon": None,
                "zenIsEmpty": True,
                "zenHasStaticIcon": False,
                "zenGlanceId": None,
                "zenIsGlance": False,
                "searchMode": None,
                "userContextId": 0,
                "attributes": {},
                "index": 1,
            }
            zen_tabs.append(placeholder)
            folder["emptyTabIds"] = [placeholder_sync_id]

        for tab in zen_tabs:
            tab["userContextId"] = user_context_id
            tab["zenDefaultUserContextId"] = user_context_id
        return space, zen_folders, zen_tabs

    def _merge_with_existing(self, existing: dict, new_spaces: list[dict],
                             new_tabs: list[dict], new_folders: list[dict]) -> dict:
        """Merge imported data with existing zen-sessions data."""
        existing_ids = {s["uuid"] for s in existing.get("spaces", [])}
        repairs = {s["uuid"]: s for s in new_spaces if s["uuid"] in existing_ids}
        spaces_to_add = [s for s in new_spaces if s["uuid"] not in existing_ids]
        tabs_to_add = [t for t in new_tabs if t["zenWorkspace"] not in repairs]
        folders_to_add = [f for f in new_folders if f["workspaceId"] not in repairs]
        folder_workspaces = {f["id"]: f.get("workspaceId") for f in existing.get("folders", [])}
        old_tabs = []
        for tab in existing.get("tabs", []):
            tab = dict(tab)
            workspace = tab.get("zenWorkspace") or folder_workspaces.get(tab.get("groupId"))
            if workspace in repairs:
                cid = repairs[workspace]["containerTabId"]
                if tab.get("userContextId", 0) != cid:
                    tab.pop("storage", None)
                tab["userContextId"] = cid
                tab["zenDefaultUserContextId"] = cid
            old_tabs.append(tab)
        old_spaces = [
            {**s, "name": repairs[s["uuid"]]["name"],
             "containerTabId": repairs[s["uuid"]]["containerTabId"]}
            if s["uuid"] in repairs else s for s in existing.get("spaces", [])
        ]
        merged = dict(existing)
        merged["spaces"] = old_spaces + spaces_to_add
        merged["tabs"] = old_tabs + tabs_to_add
        merged["folders"] = existing.get("folders", []) + folders_to_add
        merged["splitViewData"] = existing.get("splitViewData", [])
        merged["lastCollected"] = int(time.time() * 1000)

        # Build groups array from ALL folders - Zen injects sidebar.groups into
        # the sessionstore initialState so Firefox creates tab-group DOM elements.
        # Without matching groups, gZenFolders.restoreDataFromSessionStore() can't
        # find the DOM elements by ID and silently skips all folders.
        all_groups = list(existing.get("groups", []))
        existing_group_ids = {g["id"] for g in all_groups}
        for folder in folders_to_add:
            if folder["id"] not in existing_group_ids:
                all_groups.append({
                    "pinned": True,
                    "splitView": False,
                    "id": folder["id"],
                    "name": folder["name"],
                    "color": "zen-workspace-color",
                    "collapsed": folder.get("collapsed", False),
                    "saveOnWindowClose": True,
                })
        merged["groups"] = all_groups

        return merged

    # --- Sessionstore sync ---

    def _sync_sessionstore(self, merged: dict) -> None:
        """Sync zen-sessions data into sessionstore as a supplementary safety net.

        zen-sessions.jsonlz4 is the authoritative source - on startup, Zen's
        #restoreWindowData() injects its groups/tabs/folders/spaces into Firefox's
        initialState, overwriting whatever was in the sessionstore. This sync
        is belt-and-suspenders: it pre-populates the sessionstore so that even
        if Zen's injection is skipped (e.g. crash recovery path), Firefox still
        has the data it needs to create tab-group DOM elements.
        """
        # Build group entries from all folders
        folder_groups = []
        for folder in merged.get("folders", []):
            folder_groups.append({
                "pinned": True,
                "splitView": False,
                "id": folder["id"],
                "name": folder["name"],
                "color": "zen-workspace-color",
                "collapsed": folder.get("collapsed", False),
                "saveOnWindowClose": True,
            })

        # Try sessionstore files in priority order
        ss_files = [
            self.zen_profile / "sessionstore.jsonlz4",
            self.zen_profile / "sessionstore-backups" / "recovery.jsonlz4",
            self.zen_profile / "sessionstore-backups" / "recovery.baklz4",
        ]

        for ss_path in ss_files:
            if not ss_path.exists():
                continue
            try:
                ss_data = read_mozlz4(ss_path)
            except Exception:
                continue

            updated = False

            # Update every window (open and closed)
            for win_list_key in ("windows", "_closedWindows"):
                for win in ss_data.get(win_list_key, []):
                    # Replace pinned tabs with zen-sessions tabs (ensures consistent zenSyncId + groupId)
                    sync_ids = {t.get("zenSyncId") for t in merged.get("tabs", []) if t.get("zenSyncId")}
                    unpinned = [t for t in win.get("tabs", []) if not t.get("pinned")
                                and t.get("zenSyncId") not in sync_ids]
                    win["tabs"] = list(merged.get("tabs", [])) + unpinned

                    # Replace groups with folder groups
                    win["groups"] = list(folder_groups)

                    # Also sync folders and spaces into window data
                    win["folders"] = list(merged.get("folders", []))
                    win["spaces"] = list(merged.get("spaces", []))
                    win["splitViewData"] = list(merged.get("splitViewData", []))

                    updated = True

            if updated:
                try:
                    backup_file(ss_path)
                    write_mozlz4(ss_path, ss_data)
                    logger.info(f"  Synced {len(merged['tabs'])} tabs + {len(folder_groups)} groups to {ss_path.name}")
                except Exception as e:
                    logger.warning(f"  Could not sync to {ss_path.name}: {e}")

    # --- Public API ---

    def import_data(self, export_data: dict, container_mappings: dict,
                        dry_run: bool = False) -> bool:
        """Import Arc spaces, pinned tabs, and folders into zen-sessions.jsonlz4.

        Args:
            export_data: Parsed Arc export JSON with 'spaces' array.
            container_mappings: Dict mapping source space_id -> container userContextId.
            dry_run: If True, log what would happen without writing.

        Returns:
            True on success, False on failure.
        """
        try:
            logger.info("Importing Arc data into zen-sessions.jsonlz4...")

            existing = self._read_existing()
            state = load_state(self.zen_profile)
            source = export_data.get("source", "arc")
            guide_path = self.zen_profile / "workspace_setup_guide.json"
            guide = json.loads(guide_path.read_text()) if guide_path.exists() else {}
            legacy_names = guide.get("zen_workspace_setup_guide", {}).get("container_mappings", {})
            all_new_spaces = []
            all_new_tabs = []
            all_new_folders = []

            for space_data in export_data.get("spaces", []):
                space_name = space_data["space_name"]
                container_id = container_for(container_mappings, space_data)

                space, folders, tabs = self._process_space(
                    space_data, user_context_id=container_id,
                )
                key = space_key(source, space_data)
                target_uuid = state["spaces"].get(key)
                if target_uuid is None and space_name in legacy_names:
                    candidates = [s for s in existing.get("spaces", []) if s["name"] == space_name]
                    urls = {t["entries"][0]["url"] for t in tabs if not t.get("zenIsEmpty")}
                    if len(candidates) == 1:
                        candidate = candidates[0]
                        old_urls = {t.get("entries", [{}])[0].get("url")
                                    for t in existing.get("tabs", [])
                                    if t.get("zenWorkspace") == candidate["uuid"]}
                        if urls and urls <= old_urls and candidate["uuid"] not in state["spaces"].values():
                            target_uuid = candidate["uuid"]
                            logger.info("Repairing earlier imported workspace: %s", space_name)
                if target_uuid:
                    space["uuid"] = target_uuid
                    for tab in tabs:
                        tab["zenWorkspace"] = target_uuid
                    for folder in folders:
                        folder["workspaceId"] = target_uuid
                state["spaces"][key] = space["uuid"]
                all_new_spaces.append(space)
                all_new_folders.extend(folders)
                all_new_tabs.extend(tabs)

                pinned_n = sum(1 for t in tabs if t.get("pinned"))
                open_n = sum(1 for t in tabs if not t.get("pinned"))
                logger.info(
                    f"  {space_name}: {pinned_n} pinned tabs, "
                    f"{open_n} open tabs, {len(folders)} folders "
                    f"-> workspace {space['uuid']}"
                )

            if dry_run:
                logger.info(
                    f"DRY RUN: Would import {len(all_new_spaces)} spaces, "
                    f"{len(all_new_tabs)} tabs, {len(all_new_folders)} folders"
                )
                return True

            # Backup existing file
            if not self._backup_sessions():
                raise OSError("Could not back up zen-sessions.jsonlz4")

            # Read existing and merge
            merged = self._merge_with_existing(
                existing, all_new_spaces, all_new_tabs, all_new_folders
            )

            # Write
            write_mozlz4(self.sessions_file, merged)

            save_state(self.zen_profile, state)
            if all_new_spaces:
                from zen_space_importer import ZenProfile, ZenSpaceImporter
                if not ZenSpaceImporter(ZenProfile(self.zen_profile.name, self.zen_profile)).update_prefs_for_workspaces(
                    all_new_spaces[0]["uuid"]
                ):
                    raise OSError("Cannot update active workspace preference")

            # Sync folder groups to sessionstore so Firefox creates tab-group DOM elements
            self._sync_sessionstore(merged)

            added_spaces = len(merged["spaces"]) - len(existing.get("spaces", []))
            added_tabs = len(merged["tabs"]) - len(existing.get("tabs", []))
            added_folders = len(merged["folders"]) - len(existing.get("folders", []))

            logger.info(
                f"Successfully imported {added_spaces} spaces, "
                f"{added_tabs} pinned tabs, {added_folders} folders"
            )
            logger.info("Restart Zen browser to see your imported data")
            return True

        except Exception as e:
            logger.error(f"Failed to import Arc data into zen-sessions: {e}")
            return False
