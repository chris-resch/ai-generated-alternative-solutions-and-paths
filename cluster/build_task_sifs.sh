#!/bin/bash
# On a LOGIN node (needs internet): one SIF file per SWE-bench task image.
#
# Each SWE-bench task has a ready-made Docker image on Docker Hub with the repository at the task's
# base commit and its packages installed. Compute nodes cannot download it, so it is converted here
# into a single SIF file in the project folder. The agent job unpacks a fresh copy of it for every run.
#
# Usage (inside tmux; a few minutes per image):
#   cd $PROJ/code       (PROJ: the project folder that holds code/, sif/ and models/)
#   bash cluster/build_task_sifs.sh django__django-11099 django__django-14351 sympy__sympy-21612
#
# Writes $PROJ/sif/tasks/sweb.eval.x86_64.<task>.sif and appends one line per image
# to manifest.tsv there: task, Docker Hub digest, SIF size, SIF sha256, build date.

set -euo pipefail
umask 007

PROJ=${PROJ:?set PROJ to the project folder that holds code/, sif/ and models/}
OUT=$PROJ/sif/tasks
SCRATCH=${HPC_SCRATCH:?set HPC_SCRATCH to your scratch folder}
export APPTAINER_CACHEDIR=${APPTAINER_CACHEDIR:-$SCRATCH/apptainer/cache}
export APPTAINER_TMPDIR=${APPTAINER_TMPDIR:-$SCRATCH/apptainer/tmp}
mkdir -p "$OUT" "$APPTAINER_CACHEDIR" "$APPTAINER_TMPDIR"
chmod 2770 "$OUT" 2>/dev/null || true       # group-owned, new files inherit the project group
[ -f "$OUT/manifest.tsv" ] || printf 'task\tdocker_digest\tsif_bytes\tsif_sha256\tbuilt\n' > "$OUT/manifest.tsv"

[ $# -gt 0 ] || { echo "Usage: bash cluster/build_task_sifs.sh <task id> [<task id> ...]" >&2; exit 1; }

for task in "$@"; do
    tag=${task//__/_1776_}                  # same naming rule as mini-swe-agent and SWE-bench
    tag=${tag,,}                            # lower case
    repo=swebench/sweb.eval.x86_64.$tag
    sif=$OUT/sweb.eval.x86_64.$tag.sif
    if [ -f "$sif" ]; then
        echo "== $task: $sif exists, skipped"
        continue
    fi
    echo "== $task: building from docker://docker.io/$repo:latest"
    # Digest of the image as Docker Hub serves it now, so the run can be tied to an exact image.
    digest=$(curl -fsS "https://hub.docker.com/v2/repositories/$repo/tags/latest" \
             | python3 -c 'import json,sys; print(json.load(sys.stdin).get("digest",""))' || echo "")
    t0=$(date +%s)
    apptainer build --mksquashfs-args "-processors 8" "$sif.part" "docker://docker.io/$repo:latest"
    mv "$sif.part" "$sif"
    bytes=$(stat -c %s "$sif")
    sha=$(sha256sum "$sif" | cut -d' ' -f1)
    printf '%s\t%s\t%s\t%s\t%s\n' "$task" "$digest" "$bytes" "$sha" "$(date -Is)" >> "$OUT/manifest.tsv"
    echo "   done in $(( $(date +%s) - t0 )) s, $(numfmt --to=iec "$bytes"), docker digest ${digest:-unknown}"
done
column -t -s $'\t' "$OUT/manifest.tsv"
