"""Launch background commands without allocating a Windows console window."""
from __future__ import annotations

import subprocess
import sys


def hidden_subprocess_options() -> dict:
    """Options for console helpers; normal browser windows stay visible.

    Keep output redirection with the caller so captured logs, result files and
    explicitly supplied stdin continue to work in source and frozen builds.
    """
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}
