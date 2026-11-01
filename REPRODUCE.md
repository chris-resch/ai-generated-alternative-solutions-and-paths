# How to reproduce

From "every number in under a minute on a laptop" to "repeated agent runs on a GPU cluster". All commands run from
the repository root.

| Rebuilds | Needs | Command | Time |
|---|---|---|---|
| every number, table and figure, from the shipped files | Python 3.12, Poetry | `make results` | under a minute |
| the two analyses that read the projects' full git histories | the above, git, internet, about 1.5 GB of disk | `make histories`, then `make results` | about 3 min once the clones exist; cloning takes longer |
| the shipped derived tables, from the public raw data | the above, awscli, about 5 GB of disk | `make pilot-data`, then `make results` | about 1 h of downloads, then under 1 min |
| one agent run on one issue, scored | the above, Docker (on a Mac: Colima), Ollama, about 20 GB of disk | `make one-task TASK=<issue id>` | about 30 min |
| repeated runs of the open model | a Slurm cluster with Apptainer and two 40 GB GPUs per job | job scripts in `cluster/jobs/` | about 35 min per job of 10 runs |

## Every number from the shipped files

```bash
poetry env use 3.12          # use Python 3.12 for this project's environment
poetry install --no-root     # install exactly the versions in poetry.lock; --no-root: the repository is not a package
make check-data              # every shipped data file against data/SHA256SUMS
make results                 # rebuild results/ from the shipped files
```

- `make check-data` runs `shasum -a 256 -c data/SHA256SUMS` and prints only files that do not match.
- `make results` runs, in this order (each script is described in CODE_GUIDE.md):

| Step | Command (abridged) | Writes |
|---|---|---|
| models solving each issue | `scripts/pilot/ladder.py --verdicts data/public/verdicts --verified data/public/swebench_verified.jsonl --out results/pilot` | `results/pilot/ladder.csv`, `ladder_runs.csv`, `ladder.txt` |
| files of the merged solutions | `scripts/pilot/fix_files.py --verified … --out results/pilot/fix_files.jsonl` | `results/pilot/fix_files.jsonl` |
| path measures | `scripts/pilot/path_measures.py --paths data/derived/*/path.jsonl --results data/public/verdicts/*/per_instance_details.json --human results/pilot/fix_files.jsonl --out results/pilot/paths` | `results/pilot/paths/runs.csv`, `tasks.csv`, `reliability.csv` |
| Figures 2 and 3 | `scripts/pilot/plot_pilot.py` | `results/figures/django-14351_requirement.*`, `author_coefficients.*` |
| our runs | `scripts/runs/our_runs.py` | `results/runs/summary.md` |

- Flags: `--verified` the SWE-bench Verified issues; `--verdicts`/`--results` the published per-issue verdicts of
  the 11 runs; `--paths` the step paths; `--human` the files of the merged solutions; `--out` where to write.
  `plot_pilot.py` and `our_runs.py` take the repository's paths by default.
- `make results` reads the shipped outputs of `make histories`, so it needs no clone.
- The Makefile fixes Python's hash seed (`PYTHONHASHSEED=0`), so that ties between equally common files come out in
  the same order on every run. The last digits of a few floating-point values in `results/pilot/paths/` can differ
  between operating systems; the shipped files were made on macOS.

## The analyses that read the projects' full git histories

```bash
make histories               # REPOS_FULL=<folder> to keep the clones elsewhere (default data/repos_full)
make results
```

`make histories` runs:

1. `scripts/pilot/choice_in_set.py --verified … --patches data/derived/*/patches.jsonl --results data/public/verdicts/*/per_instance_details.json --repos-dir data/repos_full --out results/pilot/choice_in_set`:
   on first use it clones the full history of the 12 projects from GitHub into `--repos-dir` (about 1.5 GB), then
   reads the solution of every passing patch and finds the commit that brought each merged solution into the main
   line. `--patches` are the submitted patches of the 11 runs, `--results` their verdicts; `--jobs` (default 4) sets
   how many issues run at a time.
2. The same for the 8 issues of our runs (`--tasks`), with our 40 runs added as
   `--extra qwen-<job>-r<NN>=data/runs/<job>/qwen3.6-35b-a3b-r<NN>/trajs/preds.json:data/runs/<job>/scores/<report>.json`
   (name, submitted patches, scorer's report), writing `results/runs/choice_in_set/`.
3. `scripts/pilot/allocation.py --repos-dir data/repos_full`: authors and candidates from the same clones, the
   descriptive numbers and Tables A1 and A2.

The projects' histories keep growing, so a later clone holds more commits. `allocation.py` reads only commits before
each issue's base commit, and `choice_in_set.py` the commit that brought the merged solution in, so later commits
change none of the numbers in the appendix; `choice_in_set.py`'s `summary.md` records the commit each history was
read up to.

## Derived tables from the public raw data

```bash
brew install awscli          # on a Mac; any awscli works, no account is needed
make pilot-data
make results
```

`make pilot-data` runs:

1. `aws s3 sync --no-sign-request s3://swe-bench-submissions/bash-only/20260217_mini-v2.0.0_<model>/trajs/ data/public/<model>/trajs/`
   for each of the 11 models. `sync` copies the folder and skips files already present; `--no-sign-request` reads
   the public bucket without credentials. About 4.7 GB; all 11 took about an hour.
2. `load_dataset('princeton-nlp/SWE-bench_Verified', split='test', revision='c104f840…').to_json(…)`: the 500
   issues at the pinned revision, written to `data/public/swebench_verified.jsonl`.
3. `git clone --filter=blob:none --no-checkout --sparse https://github.com/SWE-bench/experiments.git ext/experiments`,
   `git sparse-checkout set --no-cone '/evaluation/verified/20260217_mini-v2.0.0_*/per_instance_details.json'`,
   `git checkout 40f164d5b8…`: only the 11 verdict files, at the index commit of 3 Sep 2026. `--filter=blob:none`
   downloads no file contents until a file is checked out; `--sparse` with `--no-cone` names single files. The
   verdict files are then copied to `data/public/verdicts/`.
4. `scripts/pilot/parse_trajs.py data/public/<model>/trajs --run <model> --out data/derived/<model>` for each model.
5. `scripts/pilot/export_footprints.py --verified … --derived data/derived --repos data/repos`: on its first run it
   makes blobless bare clones of the 12 projects (about 320 MB).

Steps 4 and 5 take about 45 s for all 11 models once the raw data and clones are on disk. They rebuild the shipped
`path.jsonl` and `patches.jsonl` byte for byte.

## One agent run on one issue, scored

On a Mac, the task images are x86 images and run under emulation in Colima:

```bash
brew install colima docker ollama
colima start --vm-type vz --vz-rosetta --cpu 4 --memory 6 --disk 60
export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
ollama pull gpt-oss:20b
make one-task TASK=django__django-11099
```

- `colima start`: a Linux virtual machine for Docker; `--vm-type vz --vz-rosetta` uses Apple's virtualization with
  Rosetta for x86 images; 4 CPUs, 6 GB memory, 60 GB disk.
- `DOCKER_HOST` points Docker clients at Colima. The scorer's Docker library ignores Docker contexts, so this
  variable is needed.
- `make one-task` runs:
  1. `ollama create gpt-oss-20b-64k -f configs/ollama/gpt-oss-20b-64k.Modelfile`: the local model with a
     65,536-token context (Ollama's default context is shorter and would cut the agent's history silently).
  2. `docker pull --platform linux/amd64 swebench/sweb.eval.x86_64.<issue with __ written _1776_>:latest`: the
     task image for x86 CPUs; without `--platform` Docker on a Mac asks for an arm64 image that does not exist.
  3. `mini-extra swebench-single --subset verified --split test -i <issue> -c swebench.yaml -c configs/mini/local-ollama.yaml -y --exit-immediately -o runs/one-task/<issue>.traj.json`:
     one agent run. `-c` loads mini-swe-agent's own SWE-bench settings, then ours on top (model, 50 steps); `-y`
     and `--exit-immediately` run without questions; `-o` names the trajectory file.
  4. A one-line Python command writes the submitted patch in the scorer's format (`runs/one-task/preds.json`).
  5. `python -m swebench.harness.run_evaluation --dataset_name princeton-nlp/SWE-bench_Verified --predictions_path runs/one-task/preds.json --instance_ids <issue> --max_workers 1 --run_id one-task`:
     scores the patch in the issue's image and writes a report into the current folder.
- Measured on an Apple M3 with 24 GB: 28 min for 50 steps with gpt-oss-20b, which did not finish the issue in
  50 steps; scoring the merged solution of the same issue took 28 s with the image present.

## Repeated runs on a GPU cluster

Needs: Slurm; Apptainer 1.5 or later; jobs with two 40 GB GPUs on one node (the model is split across both);
compute nodes may be offline, because images and weights are prepared on a login node. Every account, partition
and folder below is a placeholder:

| Placeholder | Meaning |
|---|---|
| `PROJ` | project folder with `code/` (this repository), `sif/` (images) and `models/` (weights); exported in the shell |
| `HPC_SCRATCH` | a scratch folder for caches and run sandboxes; exported in the shell |
| `<account>`, `<partition>` | your cluster's Slurm account and, if needed, partition |

```bash
# on a login node, once
cd $PROJ/code
module purge
PYTHON=/usr/bin/python3.11 bash cluster/setup_agent_venv.sh
apptainer build --mksquashfs-args "-processors 8" $PROJ/sif/vllm-openai_v0.31.0.sif docker://vllm/vllm-openai:v0.31.0
hf download Qwen/Qwen3.6-35B-A3B-FP8 --revision 95a723d08a9490559dae23d0cff1d9466213d989 --local-dir $PROJ/models/Qwen3.6-35B-A3B-FP8
bash cluster/build_task_sifs.sh django__django-13121 django__django-16315 pydata__xarray-3305 sphinx-doc__sphinx-9658 \
  pydata__xarray-4075 sphinx-doc__sphinx-8551 matplotlib__matplotlib-24627 scikit-learn__scikit-learn-13439
.venv-agent-sys/bin/python cluster/check_task_env.py --task django__django-13121

# checks on a GPU node, then the runs: one job per pair of issues, as in data/runs/
sbatch -A <account> cluster/jobs/node_check.sbatch
sbatch -A <account> cluster/jobs/vllm_smoke.sbatch
sbatch -A <account> -t 03:00:00 \
  --export=ALL,TASKS="django__django-13121 django__django-16315",REPS=5,RUN_SECONDS=7200,RECORD_WORKTREE=true \
  cluster/jobs/agent_pilot.sbatch
```

- `module purge` and `PYTHON=/usr/bin/python3.11`: the agent driver uses the operating system's Python, which runs
  on every node; Python modules built for the login nodes' CPUs may crash on GPU nodes with other CPUs.
- `setup_agent_venv.sh` writes the environment's package list to `cluster/requirements-agent.lock.txt`; the shipped
  list is the one our runs used.
- `apptainer build`: converts the vLLM Docker image into one file; `-processors 8` compresses with 8 cores.
- `hf download … --revision`: the model weights at a fixed revision.
- `build_task_sifs.sh`: one image file per issue, and a manifest with each Docker Hub digest.
- `check_task_env.py`: one task container as the agent will use it; prints PASS or FAIL per check.
- `sbatch -A <account>`: submits a job; `-t` sets its time limit; `--export=ALL,NAME=value` passes settings to the
  job: `TASKS` (issue ids), `REPS` (runs per issue), `RUN_SECONDS` (wall time per run), `RECORD_WORKTREE` (store
  the code changes after every command, hidden from the agent), `MAX_MODEL_LEN` (context length, default 262,144).
  Our four jobs used the settings shown, with the issue pairs `django__django-13121 django__django-16315`,
  `pydata__xarray-3305 sphinx-doc__sphinx-9658`, `pydata__xarray-4075 sphinx-doc__sphinx-8551` and
  `matplotlib__matplotlib-24627 scikit-learn__scikit-learn-13439`.
- The job writes `runs/<jobid>/`: one folder per run with its trajectories and `preds.json`, plus `preflight.json`,
  `summary.tsv`, `gpu.csv` and logs. Our four jobs are in `data/runs/`.
- Scoring happens on a laptop with Docker, once per run:
  `python -m swebench.harness.run_evaluation --dataset_name princeton-nlp/SWE-bench_Verified --predictions_path data/runs/<jobid>/<run>/trajs/preds.json --max_workers 1 --run_id cluster-<jobid>-<run>`.
  The scorer writes its report into the current folder; ours are in `data/runs/<jobid>/scores/`.
- Measured: 10 runs in parallel took about 35 minutes on two A100 40 GB GPUs; the model server was ready after
  about 220 s.
