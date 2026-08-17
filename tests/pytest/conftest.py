"""Shared pytest configuration for SoCMake's Python-side (non-CMake) tests.

The scripts under test (e.g. ``cmake/fusesoc/fusesoc_to_socmake.py``) are
plain standalone scripts invoked by CMake via ``execute_process()``, not an
installed package, so they aren't importable by their dotted path. This adds
each script's directory to ``sys.path`` so test modules can just
``import fusesoc_to_socmake``.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

for script_dir in [
    REPO_ROOT / "cmake" / "fusesoc",
]:
    sys.path.insert(0, str(script_dir))
