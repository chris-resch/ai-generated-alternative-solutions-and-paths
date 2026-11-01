#!/bin/bash
# Smoke test: serve Qwen3.6-35B-A3B-FP8 with vLLM inside Apptainer, check one tool call and
# measure generation speed, then shut down. No agent, no task containers yet.
#
# Default: two A100-SXM4-40GB on a DGX node (model split across both cards).
#   sbatch cluster/jobs/vllm_smoke.sbatch
# One H100 instead (command-line options override the #SBATCH lines):
#   sbatch --gres=gpu:h100:1 --constraint=h100 cluster/jobs/vllm_smoke.sbatch
#
# Safeguards (as described to the computing centre): server bound to 127.0.0.1 with a per-job random API key;
# container with --containall --cleanenv and a scratch folder as home (real home never mounted);
# only model (read-only) and cache dirs bound;
# offline mode (compute nodes have no internet).

# Account and partition are settings of your cluster; add them on the command line, e.g.
#   cd $PROJ/code && sbatch -A <account> [-p <partition>] cluster/jobs/vllm_smoke.sbatch
# PROJ (the project folder with code/, sif/ and models/) and HPC_SCRATCH (a scratch folder) come from
# the environment, which sbatch passes on to the job.
#SBATCH -J vllm-smoke
#SBATCH -t 00:30:00
#SBATCH -n 1
#SBATCH -c 16
#SBATCH --mem-per-cpu=8G
#SBATCH --gres=gpu:a100:2
#SBATCH --constraint=dgx
#SBATCH -o jobs/logs/%x_%j.out

set -uo pipefail
/usr/bin/date +"Job ${SLURM_JOB_ID} START: %F_%T" >&2

PROJ=${PROJ:?set PROJ to the project folder that holds code/, sif/ and models/}
SIF=$PROJ/sif/vllm-openai_v0.31.0.sif
MODEL_DIR=$PROJ/models/Qwen3.6-35B-A3B-FP8
CACHE=${HPC_SCRATCH:?set HPC_SCRATCH to your scratch folder}/vllm-cache   # torch.compile / Triton caches, reused across jobs
OUT=$PROJ/code/jobs/smoke/$SLURM_JOB_ID
mkdir -p "$CACHE/home" "$OUT"

N_GPU=$(echo "${CUDA_VISIBLE_DEVICES:-0}" | tr ',' '\n' | wc -l)
PORT=$((20000 + SLURM_JOB_ID % 20000))
API_KEY=$(openssl rand -hex 16)
SERVED=qwen3.6-35b-a3b
BASE=http://127.0.0.1:$PORT

echo "== node $(hostname), GPUs: $CUDA_VISIBLE_DEVICES (tensor parallel $N_GPU), port $PORT"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv

# --home: a dedicated, initially empty folder on scratch is mounted as the container's home
# (Apptainer forbids setting HOME by --env; the real home folder is never mounted).
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

echo "== versions"
"${APPT[@]}" python3 -c "import vllm, torch; print('vllm', vllm.__version__, '| torch', torch.__version__, '| cuda', torch.version.cuda)"

echo "== starting vLLM"
STARTUP_START=$(date +%s)
"${APPT[@]}" vllm serve /model \
    --served-model-name "$SERVED" \
    --host 127.0.0.1 --port "$PORT" \
    --tensor-parallel-size "$N_GPU" \
    --max-model-len 131072 \
    --gpu-memory-utilization 0.90 \
    --language-model-only \
    --enable-auto-tool-choice --tool-call-parser qwen3_coder \
    --reasoning-parser qwen3 \
    --seed 0 \
    > "$OUT/vllm_server.log" 2>&1 &
SERVER_PID=$!

cleanup() { kill "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null; }
trap cleanup EXIT

# Wait up to 20 minutes for the server (weights load, compile, CUDA graph capture).
READY=0
for _ in $(seq 1 240); do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then echo "!! vLLM exited early; see $OUT/vllm_server.log"; break; fi
    if curl -sf -H "Authorization: Bearer $API_KEY" "$BASE/v1/models" >/dev/null; then READY=1; break; fi
    sleep 5
done
echo "== startup took $(( $(date +%s) - STARTUP_START )) s (ready=$READY)"
if [ "$READY" -ne 1 ]; then tail -n 60 "$OUT/vllm_server.log"; exit 1; fi

post() {  # $1 = JSON body file, $2 = response file; prints curl's total time
    curl -s -o "$2" -w "%{time_total}" -H "Authorization: Bearer $API_KEY" \
         -H "Content-Type: application/json" -d @"$1" "$BASE/v1/chat/completions"
}

echo "== test 1: tool call"
cat > "$OUT/req_tool.json" <<EOF
{"model": "$SERVED", "temperature": 1.0, "top_p": 0.95, "max_tokens": 2048,
 "messages": [{"role": "user", "content": "List the files in the current directory."}],
 "tools": [{"type": "function", "function": {"name": "bash", "description": "Run a bash command",
   "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}]}
EOF
T=$(post "$OUT/req_tool.json" "$OUT/resp_tool.json")
python3 - "$OUT/resp_tool.json" "$T" <<'PY'
import json, sys
r = json.load(open(sys.argv[1])); m = r["choices"][0]["message"]
calls = m.get("tool_calls") or []
print("tool calls:", [(c["function"]["name"], c["function"]["arguments"]) for c in calls] or "NONE")
print("reasoning chars:", len(m.get("reasoning_content") or m.get("reasoning") or ""),
      "| completion tokens:", r["usage"]["completion_tokens"], "| seconds:", sys.argv[2])
PY

echo "== test 2: generation speed (one request, ~1,500 tokens)"
cat > "$OUT/req_speed.json" <<EOF
{"model": "$SERVED", "temperature": 1.0, "top_p": 0.95, "max_tokens": 1500, "min_tokens": 1500,
 "messages": [{"role": "user", "content": "Explain, step by step and in detail, how a hash map handles collisions."}]}
EOF
T=$(post "$OUT/req_speed.json" "$OUT/resp_speed.json")
python3 - "$OUT/resp_speed.json" "$T" <<'PY'
import json, sys
r = json.load(open(sys.argv[1])); n = r["usage"]["completion_tokens"]; t = float(sys.argv[2])
print(f"completion tokens: {n} in {t:.1f} s = {n/t:.0f} tokens/s (single stream)")
PY

echo "== test 3: 8 parallel requests (throughput under load)"
SPEED_START=$(date +%s.%N)
PIDS=()
for i in $(seq 1 8); do post "$OUT/req_speed.json" "$OUT/resp_par_$i.json" >/dev/null & PIDS+=($!); done
wait "${PIDS[@]}"
SPEED_END=$(date +%s.%N)
python3 - "$OUT" "$SPEED_START" "$SPEED_END" <<'PY'
import json, sys, glob
n = sum(json.load(open(f))["usage"]["completion_tokens"] for f in glob.glob(sys.argv[1] + "/resp_par_*.json"))
t = float(sys.argv[3]) - float(sys.argv[2])
print(f"total completion tokens: {n} in {t:.1f} s = {n/t:.0f} tokens/s (8 streams)")
PY

grep -E "Maximum concurrency|KV cache|model weights took|GiB" "$OUT/vllm_server.log" | head -n 8
/usr/bin/date +"Job ${SLURM_JOB_ID} END: %F_%T" >&2
exit 0
