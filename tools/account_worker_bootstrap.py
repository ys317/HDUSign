"""Start source workers from any account directory with the active Python."""
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if os.environ.get("HDUSIGN_PORTABLE_DEPENDENCIES") == "1":
    from tools import portable_tests
from hdusign.__main__ import main
raise SystemExit(main())
