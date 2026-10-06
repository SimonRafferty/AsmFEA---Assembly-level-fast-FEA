"""Detached job worker: python -m asmfea.jobs.worker <study> <job id> <stage>..."""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

from ..diagnostics import Blocked, make
from . import ALL_STAGES, JobState


def main(argv: list[str]) -> int:
    from ..pipeline import STAGES, Cancelled

    study, job_id, stages = Path(argv[0]), argv[1], argv[2:]
    js = JobState(study, job_id, stages)
    current = None
    try:
        for name in [s for s in ALL_STAGES if s in stages]:
            if js.cancel_requested():
                raise Cancelled()
            current = name
            js.stage(name)
            STAGES[name](study, js)
            js.stage_done(name)
        js.finish("done", "finished: " + ", ".join(stages))
        return 0
    except Cancelled:
        js.finish("cancelled", f"cancelled during {current}")
        return 2
    except Blocked as b:
        js.add_diags(b.diags)
        js.finish("blocked", f"blocked in {current}: {b.diags[0].what}")
        return 3
    except Exception as e:  # a defect: report it in the same structured form
        js.add_diags([make("JOB_FAILED", job=job_id, stage_failed=current, error=repr(e)[:300],
                           raw=traceback.format_exc()[-4000:])])
        js.finish("failed", f"failed in {current}: {e!r}"[:300])
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
