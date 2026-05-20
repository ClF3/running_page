"""Test package setup.

The application scripts historically import modules such as ``config`` and
``gpxtrackposter`` as top-level names from inside ``run_page``. Adding both the
repository root and ``run_page`` keeps tests aligned with that runtime shape.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_PAGE = ROOT / "run_page"

for path in (ROOT, RUN_PAGE):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
