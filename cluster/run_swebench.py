"""mini-swe-agent's own SWE-bench batch runner (`mini-extra swebench`), with one change:
the environment name "singularity" points to harness.apptainer_env.ApptainerEnvironment.

Why a wrapper: the runner passes each task's image name only to environments it knows by name
("docker", "singularity", ...; run/benchmarks/swebench.py, get_sb_environment). Under the name
"singularity" our class receives the image name like mini-swe-agent's own Apptainer environment would,
and maps it to the local SIF. Everything else (prompt, loop, saving, preds.json) is mini-swe-agent 2.4.6.

Same options as `mini-extra swebench`, e.g.
    python cluster/run_swebench.py --subset data/swebench_verified --split test --filter '^(django__django-11099)$' \
        -o runs/test -c swebench.yaml -c configs/mini/cluster-vllm.yaml
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repository root, for `harness`

import minisweagent.environments as environments  # noqa: E402

environments._ENVIRONMENT_MAPPING["singularity"] = "harness.apptainer_env.ApptainerEnvironment"

from minisweagent.run.benchmarks.swebench import app  # noqa: E402

if __name__ == "__main__":
    app()
