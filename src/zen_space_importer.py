#!/usr/bin/env python3
"""
Zen Space Importer

Creates actual Zen browser spaces (containers) and imports Arc pinned tabs
as pinned tabs in the correct spaces, not as bookmarks.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from migration_state import backup_file, container_for, load_state, profile_key, save_state

logger = logging.getLogger(__name__)

def find_zen_profile() -> Path:
    """Find the active Zen profile directory."""
    profiles_dir = Path("~/Library/Application Support/zen/Profiles").expanduser()

    if not profiles_dir.exists():
        raise FileNotFoundError(f"Zen profiles directory not found: {profiles_dir}")

    # Look for the most recently modified profile (likely the active one)
    profiles = list(profiles_dir.glob("*.Default*"))
    if not profiles:
        raise FileNotFoundError("No Zen profiles found")

    # Sort by modification time, newest first
    profiles.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    return profiles[0]

@dataclass
class ZenContainer:
    """Represents a Zen browser container (space)."""
    userContextId: int
    name: str
    icon: str
    color: str
    public: bool = True

@dataclass
class ZenProfile:
    """Represents a Zen browser profile."""
    name: str
    path: Path

class ZenSpaceImporter:
    """Imports Arc spaces as Zen containers with pinned tabs."""

    # Available container icons and colors
    CONTAINER_ICONS = [
        "fingerprint", "briefcase", "dollar", "cart", "vacation", "gift", "food",
        "fruit", "pet", "tree", "chill", "fence", "circle", "user", "lock"
    ]

    CONTAINER_COLORS = [
        "blue", "turquoise", "green", "yellow", "orange", "red", "pink", "purple"
    ]

    def __init__(self, zen_profile: ZenProfile):
        self.zen_profile = zen_profile
        self.containers_file = zen_profile.path / "containers.json"
        self.prefs_file = zen_profile.path / "prefs.js"

    def load_existing_containers(self) -> dict:
        """Load existing container configuration."""
        try:
            if self.containers_file.exists():
                with open(self.containers_file) as f:
                    return json.load(f)
            else:
                # Create default container structure
                return {
                    "version": 5,
                    "lastUserContextId": 1,
                    "identities": [
                        {
                            "icon": "fingerprint",
                            "color": "blue",
                            "l10nId": "user-context-personal",
                            "public": True,
                            "userContextId": 1
                        }
                    ]
                }
        except Exception as e:
            logger.error(f"Failed to load containers: {e}")
            return {}

    def create_containers_for_spaces(self, arc_spaces: list, source: str = "arc") -> dict[str, int]:
        """One owned container per source profile; never adopt identities by name."""
        config = self.load_existing_containers()
        if not config:
            raise ValueError("Cannot read containers.json")
        state = load_state(self.zen_profile.path)
        identities = config.setdefault("identities", [])
        public_ids = {i["userContextId"] for i in identities if i.get("public")}
        last_id = max([config.get("lastUserContextId", 0),
                       *[i["userContextId"] for i in identities if i["userContextId"] < 1_000_000]])
        mappings = {}
        for space in arc_spaces:
            key = profile_key(source, space)
            cid = state["profiles"].get(key)
            if cid not in public_ids:
                last_id += 1
                cid = last_id
                identities.append({
                    "userContextId": cid, "public": True,
                    "name": f"{source.title()} · {space.get('source_profile') or space['space_name']}",
                    "icon": self.CONTAINER_ICONS[(cid - 1) % len(self.CONTAINER_ICONS)],
                    "color": self.CONTAINER_COLORS[(cid - 1) % len(self.CONTAINER_COLORS)],
                })
                public_ids.add(cid)
                state["profiles"][key] = cid
            mappings[space["space_id"]] = cid
        # Keep unique display-name aliases for older writer/CLI callers.
        for space in arc_spaces:
            if sum(s["space_name"] == space["space_name"] for s in arc_spaces) == 1:
                mappings.setdefault(space["space_name"], mappings[space["space_id"]])
        config["lastUserContextId"] = last_id
        if not self.save_containers(config):
            raise OSError("Cannot save containers.json")
        save_state(self.zen_profile.path, state)
        return mappings

    def save_containers(self, container_config: dict) -> bool:
        """Save container configuration to containers.json."""
        try:
            backup_file(self.containers_file)
            temporary = self.containers_file.with_suffix(".tmp")
            with open(temporary, 'w') as f:
                json.dump(container_config, f, separators=(',', ':'))
            temporary.replace(self.containers_file)
            logger.info("✅ Updated containers.json")
            return True
        except Exception as e:
            logger.error(f"Failed to save containers: {e}")
            return False

    def update_prefs_for_workspaces(self, active_workspace_id: str | None = None) -> bool:
        """Update prefs.js to enable workspace features."""
        try:
            # Read existing prefs
            prefs_content = ""
            if self.prefs_file.exists():
                with open(self.prefs_file) as f:
                    prefs_content = f.read()

            # Ensure workspace preferences are set
            workspace_prefs = [
                'user_pref("zen.workspaces.continue-where-left-off", true);',
                'user_pref("zen.workspaces.force-container-workspace", true);',
                'user_pref("zen.workspaces.hide-default-container-indicator", false);',
            ]

            if active_workspace_id:
                workspace_prefs.append(f'user_pref("zen.workspaces.active", "{active_workspace_id}");')

            backup_file(self.prefs_file)
            # Update existing prefs too, so an old false value cannot defeat routing.
            for pref in workspace_prefs:
                pref_name = pref.split('"')[1]
                pattern = r'user_pref\("' + re.escape(pref_name) + r'",[^\n]*\);'
                if re.search(pattern, prefs_content):
                    prefs_content = re.sub(pattern, lambda _: pref, prefs_content)
                else:
                    prefs_content += "\n" + pref

            # Write back
            temporary = self.prefs_file.with_suffix(".tmp")
            with open(temporary, 'w') as f:
                f.write(prefs_content)
            temporary.replace(self.prefs_file)

            logger.info("✅ Updated workspace preferences")
            return True

        except Exception as e:
            logger.error(f"Failed to update prefs: {e}")
            return False

    def import_spaces_as_containers(self, export_data: dict, dry_run: bool = False) -> dict[str, int]:
        """Import Arc spaces as Zen containers."""
        try:
            arc_spaces = export_data.get('spaces', [])
            if not arc_spaces:
                logger.warning("No Arc spaces found in export data")
                return False

            logger.info(f"🔧 Creating Zen containers for {len(arc_spaces)} Arc spaces...")

            if dry_run:
                logger.info("🧪 DRY RUN - No actual changes will be made")

                for space in arc_spaces:
                    space_name = space['space_name']
                    tab_count = len(space.get('pinned_tabs', []))
                    logger.info(f"  📁 Would create container: {space_name} ({tab_count} pinned tabs)")
                # Return empty dict for dry run
                return {}

            # Create containers for spaces
            space_to_container = self.create_containers_for_spaces(arc_spaces, export_data.get("source", "arc"))

            if not space_to_container:
                logger.error("Failed to create any containers")
                return {}

            if not self.update_prefs_for_workspaces():
                raise OSError("Cannot update workspace preferences")
            return space_to_container

        except Exception as e:
            logger.error(f"Failed to import Arc spaces as containers: {e}")
            return {}

    def create_workspaces_guide(self, space_to_container: dict[str, int], arc_spaces: list) -> None:
        """Create a guide file to help users set up workspaces."""
        try:
            guide_data = {
                "zen_workspace_setup_guide": {
                    "version": "1.0",
                    "created": datetime.now(UTC).isoformat(),
                    "note": "This file contains a guide for manually setting up Zen workspaces for your Arc spaces",
                    "instructions": [
                        "1. Open Zen browser and click 'Default' in the sidebar",
                        "2. Click the '+' icon to create new workspaces",
                        "3. Name each workspace and assign the corresponding container (listed below)",
                        "4. Enable 'Switch to workspace where container is set as default when opening container tabs' in Settings > Tab Management > Workspaces"
                    ],
                    "container_mappings": space_to_container,
                    "workspaces_to_create": []
                }
            }

            # Add workspace creation instructions
            for space in arc_spaces:
                space_name = space['space_name']
                container_id = container_for(space_to_container, space)
                pinned_tab_count = len(space.get('pinned_tabs', []))

                workspace_info = {
                    "workspace_name": space_name,
                    "container_id": container_id,
                    "container_name": space_name,
                    "pinned_tabs_count": pinned_tab_count,
                    "setup_steps": [
                        f"Create workspace named '{space_name}'",
                        f"Assign container '{space_name}' (ID: {container_id})",
                        f"This workspace will handle {pinned_tab_count} pinned tabs"
                    ]
                }
                guide_data["zen_workspace_setup_guide"]["workspaces_to_create"].append(workspace_info)

            # Save the guide file
            guide_file = self.zen_profile.path / "workspace_setup_guide.json"
            with open(guide_file, 'w') as f:
                json.dump(guide_data, f, indent=2)

            logger.info(f"📋 Created workspace setup guide: {guide_file}")
            logger.info("📝 This file contains step-by-step instructions for setting up your workspaces manually")

        except Exception as e:
            logger.error(f"Failed to create workspace guide: {e}")

def main():
    """Main function to run the Arc to Zen space importer."""
    import argparse

    parser = argparse.ArgumentParser(description='Import Arc spaces as Zen containers with pinned tabs')
    parser.add_argument('arc_export_file', help='Path to Arc export JSON file')
    parser.add_argument('--dry-run', action='store_true', help='Perform a dry run without making changes')
    parser.add_argument('--verbose', action='store_true', help='Enable verbose logging')

    args = parser.parse_args()

    # Set up logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format='%(message)s')

    try:
        # Find Zen profile
        zen_profile_path = find_zen_profile()
        print(f"📁 Using Zen profile: {zen_profile_path.name}")

        # Load Arc export data
        with open(args.arc_export_file) as f:
            export_data = json.load(f)

        # Create importer
        zen_profile = ZenProfile("Default", zen_profile_path)
        importer = ZenSpaceImporter(zen_profile)

        # Import spaces
        success = importer.import_spaces_as_containers(export_data, args.dry_run)

        if success:
            print("✅ Arc to Zen migration completed successfully!")
            if not args.dry_run:
                print("🔄 Restart Zen browser to see your imported spaces and pinned tabs")
        else:
            print("❌ Arc to Zen migration failed")
            return 1

    except Exception as e:
        print(f"❌ Error: {e}")
        return 1

    return 0

if __name__ == "__main__":
    exit(main())
