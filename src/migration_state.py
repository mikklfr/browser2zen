"""Stable migration identities, separate from browser-owned JSON formats."""

import json
import shutil
import time
from pathlib import Path

STATE_FILE = ".browser2zen-identities.json"


def load_state(profile: Path) -> dict:
    path = profile / STATE_FILE
    state = json.loads(path.read_text()) if path.exists() else {"profiles": {}, "spaces": {}}
    if not isinstance(state, dict) or not all(isinstance(state.get(k), dict) for k in ("profiles", "spaces")):
        raise ValueError("Invalid migration identity file; restore its backup before migrating again.")
    return state


def backup_file(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, path.with_name(f"{path.name}.backup.{time.time_ns()}"))


def save_state(profile: Path, state: dict) -> None:
    path = profile / STATE_FILE
    backup_file(path)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2))
    temp.replace(path)


def space_key(source: str, space: dict) -> str:
    return f"{source}:{space['space_id']}"


def profile_key(source: str, space: dict) -> str:
    return f"{source}:{space.get('source_profile') or space['space_id']}"


def container_for(mappings: dict, space: dict) -> int:
    # Display-name fallback supports old CLI export payloads.
    return int(mappings.get(space.get("space_id"), mappings.get(space["space_name"], 0)) or 0)
