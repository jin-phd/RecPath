from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
# RECPATH_DATA_DIR lets test or scratch runs write somewhere other than data/.
DATA_DIR = Path(os.environ.get("RECPATH_DATA_DIR") or PROJECT_ROOT / "data").resolve()
SESSIONS_DIR = DATA_DIR / "sessions"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
PROJECT_BROWSER_PATH = PROJECT_ROOT / ".playwright-browsers"


def project_relative(path: Path) -> str:
    """Store paths relative to the project root so saved sessions stay portable."""
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(resolved)


def resolve_project_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path
