"""Approve one exact upgrade from the deployed Stage 1/2 release to its probe fix."""

from pathlib import Path
import sys

import activate_stage12 as stage12


stage12.BRANCH = "stage/05-06-connect-live-fix"
stage12.PRIOR_SHA = "d918b1af618d9b0bc55a59d7132d357f0836ba83"
stage12.PLAN = Path("/etc/devflow/stage12-probe-fix-plan.json")
stage12.WORKER_SCRIPT = Path(__file__).resolve()


if __name__ == "__main__":
    try:
        stage12.main()
    except Exception as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        print("Inspect service and continuity before any retry.", file=sys.stderr)
        raise SystemExit(1)
