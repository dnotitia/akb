"""Make `src` importable as a package when pytest is run from anywhere.

The bench is normally driven as `python -m src.judge` from
`eval/agentic-bench/`, so the package root is that directory rather than the
repository root.
"""

from __future__ import annotations

import sys
from pathlib import Path

BENCH_ROOT = Path(__file__).resolve().parents[1]
if str(BENCH_ROOT) not in sys.path:
    sys.path.insert(0, str(BENCH_ROOT))
