#!/bin/bash
# Agent runs on the cluster. vLLM serves Qwen3.6-35B-A3B-FP8 on two A100s; mini-swe-agent runs
# SWE-bench tasks on the same node, each in its own Apptainer task container, and talks to the
# server over the node's own network (127.0.0.1) with a random per-job key.
#
# First test, one run on one task (default):
#   sbatch cluster/jobs/agent_pilot.sbatch
# Repeated runs of the same model (how far runs diverge), e.g. 5 runs on each of two tasks:
#   sbatch --export=ALL,TASKS="django__django-14351 sympy__sympy-21612",REPS=5 -t 02:00:00 cluster/jobs/agent_pilot.sbatch
# Settings read from the environment (sbatch --export=ALL,NAME=value,...):
#   TASKS            SWE-bench task ids, space-separated            (default django__django-11099)
#   REPS             independent runs per task                      (default 1)
#   RUN_SECONDS      wall-time limit per run                        (default 2400)
#   RECORD_WORKTREE  true: git diff after every command, hidden from the agent (default false)
#   MAX_MODEL_LEN    vLLM context length in tokens, prompt plus reply (default 262144, Qwen3.6's native
#                    length; with replies of up to 32,768 tokens a run can carry 229,376 tokens of history)
#
# Safeguards (as described to the computing centre): task containers with --containall --no-home --cleanenv, nothing
# from the host bound except the run's own sandbox and work folder, no credentials inside (the API key
# lives only in the driver's environment, which --cleanenv keeps out); vLLM on 127.0.0.1 with the key;
# per-run limits on steps (250), output tokens (500k), wall time (RUN_SECONDS), 60 s per command;
# every command and its output is kept in the trajectory.

# Account and partition are settings of your cluster; add them on the command line, e.g.
#   cd $PROJ/code && sbatch -A <account> [-p <partition>] cluster/jobs/agent_pilot.sbatch
# PROJ (the project folder with code/, sif/ and models/) and HPC_SCRATCH (a scratch folder) come from
# the environment, which sbatch passes on to the job.
#SBATCH -J agent-pilot
#SBATCH -t 01:00:00
#SBATCH -n 1
#SBATCH -c 16
#SBATCH --mem-per-cpu=8G
#SBATCH --gres=gpu:a100:2
#SBATCH --constraint=dgx
#SBATCH -o jobs/logs/%x_%j.out

set -uo pipefail
/usr/bin/date +"Job ${SLURM_JOB_ID} START: %F_%T" >&2
umask 007

# ---- settings ---------------------------------------------------------------------------------
TASKS=$(echo ${TASKS:-django__django-11099})    # echo squeezes repeated spaces
REPS=${REPS:-1}
RUN_SECONDS=${RUN_SECONDS:-2400}
RECORD_WORKTREE=${RECORD_WORKTREE:-false}
MAX_MODEL_LEN=${MAX_MODEL_LEN:-262144}           # django-14351 runs passed 131072 tokens after ~100 steps

PROJ=${PROJ:?set PROJ to the project folder that holds code/, sif/ and models/}
CODE=$PROJ/code
VENV=$CODE/${VENV:-.venv-agent-sys}             # agent driver's Python environment (setup_agent_venv.sh)
SIF=$PROJ/sif/vllm-openai_v0.31.0.sif
MODEL_DIR=$PROJ/models/Qwen3.6-35B-A3B-FP8
DATA=$CODE/data/swebench_verified                 # holds test.jsonl (SWE-bench Verified, revision c104f840)
CACHE=${HPC_SCRATCH:?set HPC_SCRATCH to your scratch folder}/vllm-cache
OUT=$CODE/runs/$SLURM_JOB_ID
SERVED=qwen3.6-35b-a3b
mkdir -p "$CACHE/home" "$OUT"
cd "$CODE" || exit 1

N_TASKS=$(wc -w <<< "$TASKS")
N_RUNS=$(( N_TASKS * REPS ))
FILTER="^($(tr ' ' '|' <<< "$TASKS"))\$"
N_GPU=$(echo "${CUDA_VISIBLE_DEVICES:-0}" | tr ',' '\n' | wc -l)
PORT=$((20000 + SLURM_JOB_ID % 20000))
API_KEY=$(openssl rand -hex 16)
BASE=http://127.0.0.1:$PORT
SEED=$SLURM_JOB_ID                                # server seed differs per job, so jobs do not repeat each other

# Sandboxes (one unpacked task image per run): node-local disk if it has room, else scratch.
LOCAL=${TMPDIR:-/tmp}
FREE_GB=$(df --output=avail -BG "$LOCAL" 2>/dev/null | tail -1 | tr -dc 0-9)
FREE_GB=${FREE_GB:-0}
if [ "$FREE_GB" -ge $(( 15 * N_RUNS + 20 )) ]; then
    SANDBOX_ROOT=$LOCAL/msa-$SLURM_JOB_ID
else
    SANDBOX_ROOT=${HPC_SCRATCH:?set HPC_SCRATCH to your scratch folder}/sandboxes/$SLURM_JOB_ID
fi
mkdir -p "$SANDBOX_ROOT"

echo "== node $(hostname), GPUs $CUDA_VISIBLE_DEVICES (tensor parallel $N_GPU), port $PORT, seed $SEED"
echo "== tasks: $TASKS | runs per task: $REPS | wall time per run: $RUN_SECONDS s | worktree record: $RECORD_WORKTREE | context: $MAX_MODEL_LEN tokens"
echo "== sandboxes in $SANDBOX_ROOT ($FREE_GB GB free on $LOCAL)"
echo "== CPU: $(LC_ALL=C lscpu | sed -n 's/^Model name: *//p' | head -1), AVX-512: $(grep -q avx512f /proc/cpuinfo && echo yes || echo no)"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv

SERVER_PID=""; GPU_LOG_PID=""
cleanup() {
    [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null
    [ -n "$GPU_LOG_PID" ] && kill "$GPU_LOG_PID" 2>/dev/null
    chmod -R u+rwX "$SANDBOX_ROOT" 2>/dev/null; rm -rf "$SANDBOX_ROOT"
    /usr/bin/date +"Job ${SLURM_JOB_ID} END: %F_%T" >&2
}
trap cleanup EXIT

# ---- model server (settings of vllm_smoke.sbatch, except the seed and the context length) --
APPT=(apptainer exec --nv --containall --cleanenv
      --home "$CACHE/home:/home/$USER"
      --bind "$MODEL_DIR":/model:ro --bind "$CACHE":/cache
      --env XDG_CACHE_HOME=/cache --env VLLM_CACHE_ROOT=/cache/vllm
      --env TRITON_CACHE_DIR=/cache/triton --env TORCHINDUCTOR_CACHE_DIR=/cache/inductor
      --env FLASHINFER_WORKSPACE_BASE=/cache
      --env HF_HUB_OFFLINE=1 --env TRANSFORMERS_OFFLINE=1
      --env VLLM_NO_USAGE_STATS=1 --env DO_NOT_TRACK=1
      --env OPENBLAS_CORETYPE=SkylakeX --env VLLM_API_KEY="$API_KEY"
      "$SIF")

echo "== starting vLLM"
T0=$(date +%s)
"${APPT[@]}" vllm serve /model \
    --served-model-name "$SERVED" \
    --host 127.0.0.1 --port "$PORT" \
    --tensor-parallel-size "$N_GPU" \
    --max-model-len "$MAX_MODEL_LEN" \
    --gpu-memory-utilization 0.90 \
    --language-model-only \
    --enable-auto-tool-choice --tool-call-parser qwen3_coder \
    --reasoning-parser qwen3 \
    --seed "$SEED" \
    > "$OUT/vllm_server.log" 2>&1 &
SERVER_PID=$!

until curl -sf -H "Authorization: Bearer $API_KEY" "$BASE/v1/models" > /dev/null; do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "!! vLLM exited during startup; last lines of its log:"; tail -40 "$OUT/vllm_server.log"; exit 1
    fi
    if [ $(( $(date +%s) - T0 )) -gt 1200 ]; then
        echo "!! vLLM not ready after 20 min"; tail -40 "$OUT/vllm_server.log"; exit 1
    fi
    sleep 10
done
echo "== vLLM ready after $(( $(date +%s) - T0 )) s"
nvidia-smi --query-gpu=timestamp,utilization.gpu,memory.used --format=csv,noheader -l 30 > "$OUT/gpu.csv" &
GPU_LOG_PID=$!

# ---- the driver's environment (never passed into task containers) -----------------------------
if [ -s "$VENV/modules.txt" ] && type module >/dev/null 2>&1; then
    module load $(tr ':' ' ' < "$VENV/modules.txt")   # the modules active when the environment was built
fi
if ! "$VENV/bin/python" -c "import minisweagent" > /dev/null; then
    echo "!! $VENV does not work on this node"; exit 1
fi
export PYTHONPATH=$CODE                           # makes harness.apptainer_env importable
export HOSTED_VLLM_API_KEY=$API_KEY               # LiteLLM sends it to vLLM; not saved in trajectories
export LITELLM_LOCAL_MODEL_COST_MAP=True          # LiteLLM would otherwise fetch its price list online
export HF_HOME=$CODE/.hf HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1
export MSWEA_CONFIGURED=true MSWEA_GLOBAL_CONFIG_DIR=$OUT/.mswea   # no first-run wizard, nothing written to home
export MSWEA_SINGULARITY_EXECUTABLE=apptainer
export TASK_SIF_DIR=$PROJ/sif/tasks SANDBOX_ROOT
export APPTAINER_CACHEDIR=$SANDBOX_ROOT/.cache APPTAINER_TMPDIR=$SANDBOX_ROOT/.tmp   # not in home
mkdir -p "$APPTAINER_CACHEDIR" "$APPTAINER_TMPDIR"
CONFIGS=(-c swebench.yaml -c configs/mini/cluster-vllm.yaml
         -c "model.model_kwargs.api_base=$BASE/v1"
         -c "agent.wall_time_limit_seconds=$RUN_SECONDS"
         -c "environment.record_worktree=$RECORD_WORKTREE")

echo "== versions"
"$VENV/bin/python" -c "import importlib.metadata as m; print(' | '.join(f'{p} {m.version(p)}' for p in ['mini-swe-agent','litellm','datasets']))"
apptainer --version
grep -E "$(tr ' ' '|' <<< "$TASKS")" "$TASK_SIF_DIR/manifest.tsv" || echo "!! no manifest lines for these tasks"

echo "== preflight: two model calls with the agent's own settings"
if ! MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT=2 "$VENV/bin/python" cluster/preflight.py "${CONFIGS[@]}" --out "$OUT/preflight.json"; then
    echo "!! preflight failed, no agent runs started"; exit 1
fi

# ---- agent runs -------------------------------------------------------------------------------
# One mini-swe-agent process per replicate; each runs all tasks in parallel threads. Output layout
# <model>-r<NN>/trajs/<task>/<task>.traj.json matches the public runs, so the laptop scripts read it.
echo "== starting $N_RUNS runs"
RUNS_START=$(date +%s)
PIDS=()
for i in $(seq 1 "$REPS"); do
    r=$(printf %02d "$i")
    timeout --signal=INT --kill-after=120 $(( RUN_SECONDS + 900 )) \
        "$VENV/bin/python" cluster/run_swebench.py --subset "$DATA" --split test --filter "$FILTER" \
            -w "$N_TASKS" -o "$OUT/$SERVED-r$r/trajs" "${CONFIGS[@]}" \
        > "$OUT/driver_r$r.log" 2>&1 &
    PIDS+=($!)
    sleep 15                                      # stagger the unpacking of task images
done
wait "${PIDS[@]}"
echo "== runs finished after $(( $(date +%s) - RUNS_START )) s"

# ---- record ------------------------------------------------------------------------------------
curl -s -H "Authorization: Bearer $API_KEY" "$BASE/metrics" \
    | grep -E '^vllm:(prompt_tokens_total|generation_tokens_total|request_success_total)' > "$OUT/vllm_metrics.txt"
cat "$OUT/vllm_metrics.txt"
"$VENV/bin/python" cluster/summarize_runs.py "$OUT"
echo "== output in $OUT"
