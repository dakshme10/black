"""
Version and Git metadata management for AutoSL Quant Bot.
Exposes current Git commit SHA, environment, and startup metadata.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from typing import Dict, Any

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_git_commit_sha() -> str:
    """
    Retrieve the current Git commit SHA (short format).
    Falls back to environment variable or .git_commit file if git binary is unavailable.
    """
    # 1. Environment variable override (useful in containerized/packaged environments)
    env_sha = os.getenv("AUTOSL_GIT_COMMIT", os.getenv("GIT_COMMIT", "")).strip()
    if env_sha:
        return env_sha[:8]

    # 2. Check for .git_commit file in project root
    commit_file = os.path.join(_PROJECT_ROOT, ".git_commit")
    if os.path.isfile(commit_file):
        try:
            with open(commit_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    return content[:8]
        except Exception:
            pass

    # 3. Query git directly
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--short=8", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            cwd=_PROJECT_ROOT,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass

    return "unknown"


def get_deployment_metadata(is_live: bool = False) -> Dict[str, Any]:
    """
    Return structured deployment information for logging and telemetry.
    """
    return {
        "version": "1.0.0-autosl",
        "git_commit": get_git_commit_sha(),
        "environment": "production" if is_live else "dry_run",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
