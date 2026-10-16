"""Configuration from environment variables (see docs/companion-design.md)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

DEFAULT_KICAD_CLI = r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe"
DEFAULT_BACKUP_KEEP = 5
KICAD_TIMEOUT_S = 120

# companion/src/acd/config.py -> repo root is parents[3]
DEFAULT_PROFILE = (
    Path(__file__).resolve().parents[3] / "profiles" / "pcbway-standard-2layer.yaml"
)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class AcdError(Exception):
    """A refusal or failure that should be shown to the user as-is."""


@dataclass(frozen=True)
class Settings:
    projects_root: Path
    library_root: Path
    kicad_cli: str
    profile_path: Path
    backup_keep: int

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env
        projects = env.get("ACD_PROJECTS_ROOT")
        library = env.get("ACD_LIBRARY_ROOT")
        if not projects:
            raise AcdError("ACD_PROJECTS_ROOT is not set")
        if not library:
            raise AcdError("ACD_LIBRARY_ROOT is not set")
        keep_raw = env.get("ACD_BACKUP_KEEP", "")
        try:
            keep = int(keep_raw) if keep_raw else DEFAULT_BACKUP_KEEP
        except ValueError as exc:
            raise AcdError(f"ACD_BACKUP_KEEP must be an integer, got {keep_raw!r}") from exc
        if keep < 1:
            raise AcdError("ACD_BACKUP_KEEP must be at least 1")
        return cls(
            projects_root=Path(projects),
            library_root=Path(library),
            kicad_cli=env.get("ACD_KICAD_CLI") or DEFAULT_KICAD_CLI,
            profile_path=Path(env.get("ACD_PROFILE") or DEFAULT_PROFILE),
            backup_keep=keep,
        )


def validate_name(name: str) -> str:
    """Project names are folder names: no separators, no leading dot."""
    if not isinstance(name, str) or not _NAME_RE.match(name) or name.endswith("."):
        raise AcdError(
            f"invalid project name {name!r}: use letters, digits, '.', '_' or '-' "
            "(must start with a letter or digit)"
        )
    return name
