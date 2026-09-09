"""Where the bench reads its questions and writes its runs.

One definition, three consumers. `runner.py`, `prep_judge_v3.py` and
`judge.py` each used to resolve these on their own, so `EVALSET_DIR` could
select the seed question set for one stage of a run and not the next —
the runner would answer `evalset-project-akb/q001` and the judge would
score it against a `evalset/q001` that means something else entirely, with
nothing in the output saying so.

Both are read from the environment at import, which is when a module-level
constant is built; a test that changes them reloads the module.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Selects the question set. Default `evalset/` is the private hand-authored
#: one the harness shipped against; `evalset-project-akb/` is the tracked
#: seed set in this repository.
EVALSET_DIR_ENV = "EVALSET_DIR"
#: Selects the run directory. Required for any version other than `runs/`.
RUNS_DIR_ENV = "RUNS_DIR"


def evalset_dir() -> Path:
    """The question set this process scores against."""
    configured = os.environ.get(EVALSET_DIR_ENV)
    return Path(configured) if configured else ROOT / "evalset"


def runs_dir(default: str = "runs") -> Path:
    """The run directory this process reads or writes.

    `default` differs per entry point for historical reasons — the runner
    writes `runs/`, the v3 judge prep reads `runs_v3/` — so the fallback
    stays the caller's, while `RUNS_DIR` overrides all of them alike.
    """
    configured = os.environ.get(RUNS_DIR_ENV)
    return Path(configured) if configured else ROOT / default
