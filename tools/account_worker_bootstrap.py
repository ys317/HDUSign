"""Use the portable dependencies when exercising account workers from source."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import portable_tests
from hdusign.__main__ import main
raise SystemExit(main())
