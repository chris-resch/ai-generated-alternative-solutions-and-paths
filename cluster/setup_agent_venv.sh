#!/bin/bash
# Once, on a LOGIN node (needs internet): Python environment for the agent driver.
#
# The driver (mini-swe-agent) runs on the compute node itself, not in a container, because it starts
# a task container for every command with `apptainer exec`, and Apptainer cannot be started from inside
# another Apptainer container. So it needs its own Python on the cluster. Compute nodes are offline,
# so everything is installed here and later only used.
#
# The Python must run on the compute nodes too. The cluster's Python modules are built for the login
# nodes' Intel CPUs (AVX-512) and crash on the DGX nodes' AMD CPUs ("Illegal instruction"), so use
# a Python built for any x86-64 CPU: the operating system's own (/usr/bin/python3.12) or a portable build.
#
# Usage, from the code folder, in a shell without Python modules loaded:
#   cd $PROJ/code       (PROJ: the project folder that holds code/)
#   module purge
#   PYTHON=/usr/bin/python3.12 VENV=.venv-agent-sys bash cluster/setup_agent_venv.sh
#
# Versions are those of the laptop's poetry.lock (mini-swe-agent 2.4.6, LiteLLM 1.106.0, datasets 5.1.0).
# Everything pip resolves beyond them is written to <venv>/requirements.lock.txt for the record.

set -euo pipefail

CODE=${PROJ:?set PROJ to the project folder that holds code/}/code
VENV=$(cd "$CODE" && realpath -m "${VENV:-.venv-agent-sys}")
PY=${PYTHON:-/usr/bin/python3.12}

if ! command -v "$PY" >/dev/null; then
    echo "No '$PY' found. List the system Pythons with: ls -l /usr/bin/python3*" >&2
    exit 1
fi
"$PY" - <<'EOF'
import sys
if sys.version_info < (3, 10):
    sys.exit(f"Python {sys.version.split()[0]} is too old; mini-swe-agent needs 3.10 or newer")
print("Python", sys.version.split()[0], "at", sys.executable)
EOF

umask 007                                   # files readable by the project group only
if [ -e "$VENV" ]; then
    echo "$VENV exists; remove it first (rm -rf $VENV) or choose another VENV=" >&2
    exit 1
fi
"$PY" -m venv "$VENV"                       # a private Python environment
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install \
    "mini-swe-agent==2.4.6" "litellm==1.106.0" "datasets==5.1.0" mcp

"$VENV/bin/python" -m pip freeze > "$VENV/requirements.lock.txt"
cp "$VENV/requirements.lock.txt" "$CODE/cluster/requirements-agent.lock.txt"
# Modules the job must load again: only if this Python comes from a module (not from /usr).
case "$(readlink -f "$PY")" in
    /usr/*) : > "$VENV/modules.txt" ;;
    *)      echo "${LOADEDMODULES:-}" > "$VENV/modules.txt" ;;
esac
"$VENV/bin/python" - <<'EOF'
import importlib.metadata as md
for pkg in ["mini-swe-agent", "litellm", "datasets", "mcp", "openai", "pydantic"]:
    print(f"{pkg:15s} {md.version(pkg)}")
EOF
echo "Environment ready: $VENV"
echo "Modules the job will load: $(cat "$VENV/modules.txt")"
