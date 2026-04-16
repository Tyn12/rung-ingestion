"""Environment loading.

Call `load_env()` at the top of every ingestion entry point. It walks up from
the current working directory looking for .env.local first (developer secrets,
git-ignored), then .env (template / CI overrides). Missing files are silently
skipped — in CI the env vars come from GitHub Actions secrets directly, so no
dotenv file needs to exist there.
"""
from __future__ import annotations
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:                             # pragma: no cover
    load_dotenv = None


def load_env(start: Path | None = None) -> None:
    if load_dotenv is None:
        return
    start = (start or Path.cwd()).resolve()
    candidates: list[Path] = []
    for parent in [start, *start.parents]:
        candidates.append(parent / ".env.local")
        candidates.append(parent / ".env")
        # Stop climbing once we hit a repo root marker.
        if (parent / ".git").exists() or (parent / "pyproject.toml").exists():
            break
    # Load in priority order — first hit wins for each var thanks to override=False.
    for path in candidates:
        if path.is_file():
            load_dotenv(path, override=False)
