#!/bin/bash
# What a DGX A100 node offers for the agent driver, in a few minutes and without starting vLLM.
#   sbatch cluster/jobs/node_check.sbatch
# Prints the node's CPU (and whether it has AVX-512), the system Pythons, the Python modules visible
# there, and tries each agent environment that exists: .venv-agent (built with the login node's module)
# and .venv-agent-sys (built with a system Python). If one imports, it also runs the container check on
# this node, i.e. Apptainer on a compute node with the sandbox on scratch.

# Account and partition are settings of your cluster; add them on the command line, e.g.
#   cd $PROJ/code && sbatch -A <account> [-p <partition>] cluster/jobs/node_check.sbatch
# PROJ (the project folder with code/, sif/ and models/) and HPC_SCRATCH (a scratch folder) come from
# the environment, which sbatch passes on to the job.
#SBATCH -J node-check
#SBATCH -t 00:10:00
#SBATCH -n 1
#SBATCH -c 4
#SBATCH --mem-per-cpu=4G
#SBATCH --gres=gpu:a100:1
#SBATCH --constraint=dgx
#SBATCH -o jobs/logs/%x_%j.out

set -o pipefail
cd "${PROJ:?set PROJ to the project folder}/code" || exit 1
echo "== node $(hostname)"
LC_ALL=C lscpu | grep -E '^(Model name|Socket|Core|Thread)'
grep -q avx512f /proc/cpuinfo && echo "AVX-512: yes" || echo "AVX-512: no"

echo "== system Pythons"
for p in /usr/bin/python3*; do
    [ -x "$p" ] && [ ! -L "$p" ] && echo "$p: $("$p" --version 2>&1)"
done

echo "== Python modules visible on this node"
module -t avail python 2>&1 | head -30

for v in .venv-agent .venv-agent-sys; do
    [ -x "$v/bin/python" ] || continue
    echo "== $v ($(readlink -f "$v/bin/python"))"
    if "$v/bin/python" -c "import minisweagent, litellm, datasets; print('imports ok')" 2>&1 | grep -v "^This is mini\|migration guide\|global config"; then
        WORKING=$v
    fi
done

echo "== apptainer: $(apptainer --version)"
if [ -n "${WORKING:-}" ]; then
    echo "== container check on this node with $WORKING"
    "$WORKING/bin/python" cluster/check_task_env.py --task django__django-11099 \
        --sandbox-root "${HPC_SCRATCH:?set HPC_SCRATCH to your scratch folder}/sandboxes/check-$SLURM_JOB_ID" 2>&1 \
        | grep -E "PASS|FAIL|info|Failed|passed|Sandbox"
fi
