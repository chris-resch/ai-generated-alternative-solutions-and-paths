# Guide to every file

What each file of the repository is, where it comes from, what reads it, and which numbers of the technical appendix
depend on it. Scripts are listed in the order the data flow through them: public raw data, derived tables, measures,
the two analyses that read the projects' git histories, figures, and our own runs. Data and results follow, then the
cluster code. Table and figure numbers refer to the technical appendix.

Line counts come from `wc -l` and include comments and docstrings. "Standard library" means no package beyond
Python 3.12.

| Part | Files | Lines |
|---|---|---|
| Pilot (`scripts/pilot/`) | 8 | 2,179 |
| Our runs (`scripts/runs/`) | 1 | 59 |
| Cluster code and settings (`harness/`, `cluster/`, `configs/`) | 16 | 1,294 (94 of them a package list) |
| Build and environment (`Makefile`, `pyproject.toml`, `poetry.toml`, `.python-version`, `.gitignore`) | 5 | 126 |
| Data (`data/`) | 151 | not code |
| Results (`results/`) | 24 | written by the scripts |

---

## 1. Top-level files

| File | What it is |
|---|---|
| `README.md` | What the repository holds, how to run it, where the data come from. |
| `REPRODUCE.md` | Every command with its flags explained, and measured run times. |
| `CODE_GUIDE.md` | This guide. |
| `Makefile` | The commands behind `make results`, `make histories`, `make pilot-data`, `make one-task` and `make check-data`. Each target is a list of the script calls described below. |
| `pyproject.toml` | The project's Python version (3.12) and packages: mini-swe-agent 2.4.6 (the agent), swebench 4.1.0 (the scorer), datasets (downloads from Hugging Face), numpy, matplotlib, and mcp (needed by the agent's model library). |
| `poetry.lock` | Exact versions of every package and its dependencies, written by Poetry. `poetry install` installs exactly these. Generated; not meant to be read. |
| `poetry.toml` | Two Poetry settings: the environment lives in `.venv/` inside the repository; the repository is not a package. |
| `.python-version` | `3.12`, read by tools that pick the Python version. |
| `.gitignore` | Files git leaves out: Python caches, the raw downloads and clones that `make pilot-data` and `make histories` make, the scorer's reports, and `scratchpad/` (the authors' working files). |

---

## 2. From the published trajectories to derived tables

These two scripts run only in `make pilot-data`. Their outputs `data/derived/<model>/path.jsonl` and
`patches.jsonl` are shipped, so `make results` starts after them. Rerun from the raw trajectories, both scripts
rebuild the shipped files byte for byte.

### `scripts/pilot/parse_trajs.py` (265 lines)
- **Purpose.** Reads one published run (500 trajectories of one model) and turns every bash command into one row
  with its category, the files it opened or edited, its return code and its output size. It also keeps each issue's
  submitted patch.
- **Inputs.** `data/public/<model>/trajs/<issue>/<issue>.traj.json`, as downloaded from the public bucket.
- **Outputs.** `data/derived/<model>/steps.csv` (one row per command), `instances.csv` (one row per issue),
  `patches.jsonl` (the submitted patch per issue, parsed into files and hunks).
- **Rules.**
  - Reads both reply formats of mini-swe-agent: Chat Completions and the OpenAI Responses API.
  - A command's category is the first that matches, in this order: submit, edit, revert, test, setup, run, read,
    search, review, other (regular expressions in `PATTERNS`).
  - A command is malformed if it is empty or a JSON value.
  - A run is stuck if it has 20 or more identical commands in a row (`LOOP_MIN`) or more than half of its calls are
    malformed.
- **Libraries.** Standard library.

### `scripts/pilot/export_footprints.py` (158 lines)
- **Purpose.** Reduces each run's commands to the repository files that existed at the issue's base commit, and
  writes every step as an action letter, the classes of its targets and the files it names.
- **Inputs.** `data/public/swebench_verified.jsonl`, `data/derived/<model>/steps.csv`, bare blobless clones of the
  12 projects in `data/repos/` (made on the first run; only commits and folder listings are downloaded).
- **Outputs.** `data/derived/trees.jsonl`, `data/derived/<model>/footprint.jsonl` and `path.jsonl`.
- **Rules.**
  - The file list at the base commit comes from `git ls-tree -r --name-only <base_commit>`.
  - Target classes: `src` (source file at the base commit), `test` (test file), `new` (a file that is not in the
    base commit, such as the agent's own scripts), `outside` (`/tmp`, installed packages), `build`.
  - Action letters: F search, D read, E edit, R run a script, T run tests, V revert, W review own changes, U setup,
    M malformed call, O other, S submit.
- **Libraries.** Standard library; calls `git`.

---

## 3. Measures from the agents' runs

### `scripts/pilot/ladder.py` (66 lines)
- **Purpose.** Counts, per issue, how many of the 11 published runs solved it.
- **Inputs.** `data/public/verdicts/20260217_mini-v2.0.0_<model>/per_instance_details.json` (11 files),
  `data/public/swebench_verified.jsonl`.
- **Outputs.** `results/pilot/ladder.csv` (per issue: models solving, median model calls of the solving runs,
  the annotators' time estimate), `results/pilot/ladder_runs.csv` (per run: issues solved), and the printout
  `results/pilot/ladder.txt`.
- **Numbers.** 441 of 500 issues with a passing agent patch.
- **Libraries.** Standard library; imports `spearman` from `path_measures.py`.

### `scripts/pilot/fix_files.py` (39 lines)
- **Purpose.** Lists the files each merged solution changes, from the dataset's `patch` field.
- **Inputs.** `data/public/swebench_verified.jsonl`.
- **Outputs.** `results/pilot/fix_files.jsonl` (per issue: `fix_files`, `test_files`).
- **Libraries.** Standard library.

### `scripts/pilot/path_measures.py` (361 lines)
- **Purpose.** Computes the path measures of every run: steps, first edit of a source file, submitted files,
  whether the run is stuck. Its per-run table is the input of `allocation.py` and `plot_pilot.py`.
- **Inputs.** `data/derived/<model>/path.jsonl`, the 11 verdict files, `results/pilot/fix_files.jsonl`.
- **Outputs.** `results/pilot/paths/runs.csv` (5,498 runs), `tasks.csv` (500 issues), `reliability.csv`
  (agreement of each measure between two groups of models).
- **Rules.**
  - A place is a source file at the base commit; test files and the agent's own files are not places.
  - First edit = the first step that edits a source file; the final step that writes `patch.txt` is not an edit.
  - Submitted files: those named in the patch step, else those in the submit step, else those of the last edit.
  - Stuck runs (as in `parse_trajs.py`) are flagged in `friction_loop`.
- **Libraries.** Standard library.

---

## 4. The analyses that read the projects' git histories

Both run in `make histories`; their outputs are shipped, so `make results` does not need the clones.

### `scripts/pilot/choice_in_set.py` (605 lines)
- **Purpose.** For every issue, the solutions the agents' passing patches contain, and where the merged solution
  sits among them. It also finds the commit that brought the merged solution into the project's main line, which
  `allocation.py` needs to find its author.
- **Inputs.** `data/public/swebench_verified.jsonl`, `data/derived/<model>/patches.jsonl`, the 11 verdict files,
  full git clones of the 12 projects in `data/repos_full/` (cloned from GitHub on first use, about 1.5 GB). With
  `--tasks` and `--extra` it does the same for the 8 issues of our runs, with our runs added (second call in
  `make histories`).
- **Outputs.** In `results/pilot/choice_in_set/` (and `results/runs/choice_in_set/` for our runs): `tasks.csv`
  (one row per issue), `runs.csv` (one row per passing patch: its solution, whether it is the merged solution's,
  its files), `summary.md`.
- **Rules.**
  - A place is the innermost function or class around a changed line, read with Python's parser in the file
    before and after the patch (`<module>` if there is none); tests, blank lines, comments and docstrings do not
    count. A solution is the set of places a patch changes (`shape` in the outputs).
  - Position of the merged solution: `common` (the most frequent solution among the passing patches), `present`,
    `absent`.
  - `tasks.csv` also holds follow-up columns that the appendix does not use (whether the merged solution's lines
    were changed later); the docstring describes them.
- **Numbers.** 419 issues with at least two passing patches; 230 with two or more distinct solutions (139 with three
  or more); the merged solution among the agents' solutions in 335 of 441 (`summary.md`).
- **Libraries.** Standard library; calls `git`.

### `scripts/pilot/allocation.py` (447 lines)
- **Purpose.** Who authored the merged solution, set against what the agents' paths show solving required:
  Tables A1 and A2 and the descriptive numbers next to them.
- **Inputs.** `data/public/swebench_verified.jsonl`, `results/pilot/choice_in_set/tasks.csv`,
  `results/pilot/paths/runs.csv`, `data/derived/<model>/path.jsonl`, the clones in `data/repos_full/`.
- **Outputs.** `results/pilot/allocation/summary.md` (issue counts, the descriptive path numbers, Tables A1 and
  A2), `estimates.csv` (every coefficient with its standard error and p-value), `tasks.csv` (one row per issue),
  `requirement.csv` (per issue and file read before the first edit: the share of solving runs that read it, whether
  the merged solution changed it, its author's commits to it). People appear only as pseudonymous numbers; no name or
  e-mail address is written.
- **Rules** (the docstring defines each measure):
  - Author of the merged solution: the author of the commit that brought it into the main line; for a merged pull
    request, the person who wrote most of its commits. People are e-mail addresses after the project's `.mailmap`;
    bots are left out.
  - Candidates: everyone with a commit in the 365 days before the issue's base commit, plus the author.
  - Files solving required: every source file a solving run searched or read before its first edit, weighted by the
    share of solving runs that did so. Reach: directories read before the first edit beyond those of the changed
    files, median over solving runs. Core: the smallest group with 80% of the year's commits.
  - Table A1: conditional logit (one row per issue and candidate), standard errors clustered by author. Table A2:
    logit (one row per issue), robust standard errors.
- **Numbers.** 426 issues, 259 authors, 247 core, 128 without a commit in the year before; 3,847 solving runs;
  files solving required (median 4, 3 not changed by the merged solution); 217 and 155 issues; Tables A1 and A2.
- **Libraries.** numpy; calls `git`.

---

## 5. Figures

### `scripts/pilot/plot_pilot.py` (238 lines)
- **Purpose.** Figures 2 and 3.
- **Inputs.** `results/pilot/choice_in_set/runs.csv`, `results/pilot/allocation/` (`tasks.csv`, `estimates.csv`,
  `requirement.csv`), `results/pilot/paths/runs.csv`, `data/derived/<model>/path.jsonl`.
- **Outputs.** `results/figures/django-14351_requirement.{png,svg,pdf}` (Figure 2: the solving runs of one issue,
  grouped by solution, the files solving required, and the author's past commits to them) and
  `results/figures/author_coefficients.{png,svg,pdf}` (Figure 3: Table A1, column 4).
- **Libraries.** matplotlib.

---

## 6. Our runs

### `scripts/runs/our_runs.py` (59 lines)
- **Purpose.** How many of our runs passed, and how many distinct solutions they contain.
- **Inputs.** The scorer's reports `data/runs/<job>/scores/*.json`, `results/runs/choice_in_set/runs.csv`.
- **Outputs.** `results/runs/summary.md`.
- **Numbers.** 33 of 40 runs passed; 14 distinct solutions; two or more in 5 of 8 issues; 3 solutions that no
  published model's passing patch has.
- **Libraries.** Standard library.

---

## 7. Data (`data/`)

Everything a script reads that no script in `make results` writes. `data/SHA256SUMS` holds a checksum for every
file; `make check-data` compares them.

| Path | Files | What it is | Comes from | Read by |
|---|---|---|---|---|
| `data/public/swebench_verified.jsonl` | 1 | The 500 issues: project, base commit, issue text, merged solution and its tests | Hugging Face `princeton-nlp/SWE-bench_Verified`, revision c104f840 (`make pilot-data`) | most scripts |
| `data/public/verdicts/20260217_mini-v2.0.0_<model>/per_instance_details.json` | 11 | Per issue: did the published run pass the maintainers' tests | SWE-bench experiments repository, commit 40f164d5b8 | `ladder.py`, `path_measures.py`, `choice_in_set.py` |
| `data/derived/<model>/path.jsonl` | 11 | One line per run: every step as action letter, target classes, files named, exit code | `parse_trajs.py` + `export_footprints.py` | `path_measures.py`, `allocation.py`, `plot_pilot.py` |
| `data/derived/<model>/patches.jsonl` | 11 | One line per run: the submitted patch | `parse_trajs.py` | `choice_in_set.py` |
| `data/runs/<job>/` | 116 | Our four cluster jobs: Qwen3.6-35B-A3B, five runs each on two issues | `cluster/jobs/agent_pilot.sbatch`; user names, folder paths and node names replaced | `choice_in_set.py`, `our_runs.py` |
| `data/SHA256SUMS` | 1 | Checksums of all files above | `shasum -a 256` | `make check-data` |

Inside each job folder (`data/runs/<job>/`):

| File | What it is |
|---|---|
| `qwen3.6-35b-a3b-r<NN>/trajs/<issue>/<issue>.traj.json` | The full record of one run: every model reply, command and output |
| `qwen3.6-35b-a3b-r<NN>/trajs/preds.json` | The run's submitted patches, in the scorer's format |
| `qwen3.6-35b-a3b-r<NN>/trajs/exit_statuses_<time>.yaml` | How each issue of the run ended, written by mini-swe-agent |
| `scores/hosted_vllm__qwen3.6-35b-a3b.cluster-<job>-r<NN>.json` | The scorer's report per run |
| `summary.tsv` | One line per run and issue: exit status, steps, tokens, minutes (`cluster/summarize_runs.py`) |
| `preflight.json` | The two test calls to the model before the runs (`cluster/preflight.py`) |
| `gpu.csv`, `vllm_metrics.txt` | GPU use every 30 seconds; the model server's counters at the end of the job |

---

## 8. Results (`results/`)

Every file here is written by a script.

| Path | Written by |
|---|---|
| `results/pilot/ladder.csv`, `ladder_runs.csv`, `ladder.txt` | `ladder.py` |
| `results/pilot/fix_files.jsonl` | `fix_files.py` |
| `results/pilot/paths/runs.csv`, `tasks.csv`, `reliability.csv` | `path_measures.py` |
| `results/pilot/choice_in_set/tasks.csv`, `runs.csv`, `summary.md` | `choice_in_set.py` (`make histories`) |
| `results/pilot/allocation/summary.md`, `tasks.csv`, `estimates.csv`, `requirement.csv` | `allocation.py` (`make histories`) |
| `results/runs/choice_in_set/tasks.csv`, `runs.csv`, `summary.md` | `choice_in_set.py` for our runs (`make histories`) |
| `results/runs/summary.md` | `our_runs.py` |
| `results/figures/django-14351_requirement.{png,svg,pdf}`, `author_coefficients.{png,svg,pdf}` | `plot_pilot.py` (Figures 2 and 3) |

---

## 9. Cluster code (cannot run without a GPU cluster)

These files ran our repeated agent runs (`data/runs/`). They are shipped so the runs can be checked and repeated;
`make results` does not use them. REPRODUCE.md explains every command and setting.

| File | Lines | What it does |
|---|---|---|
| `harness/apptainer_env.py` | 243 | Task container for mini-swe-agent under Apptainer: a local image file instead of a download; `--containall --no-home --cleanenv`; a work folder per run so `/tmp` persists between commands; an optional record of the code changes after every command, hidden from the agent |
| `harness/__init__.py` | 0 | Makes `harness` importable |
| `cluster/run_swebench.py` | 26 | mini-swe-agent's SWE-bench runner, with the environment name `singularity` pointing to the class above |
| `cluster/setup_agent_venv.sh` | 61 | Login node, once: the agent driver's Python environment; writes its package list |
| `cluster/requirements-agent.lock.txt` | 94 | That package list: every package of the agent driver's environment with its exact version |
| `cluster/build_task_sifs.sh` | 50 | Login node: one image file per task image, with a manifest of Docker Hub digests and checksums |
| `cluster/check_task_env.py` | 168 | Checks one task container as the agent will use it: base commit, no later history, `/tmp` persists, edits persist, host folders hidden, overhead per command |
| `cluster/preflight.py` | 136 | Run by the job before any agent run: two model calls with the agent's exact settings (tool call, reasoning saved, token budget active, history accepted) |
| `cluster/summarize_runs.py` | 61 | One line per run at the end of a job (`summary.tsv`) |
| `cluster/jobs/agent_pilot.sbatch` | 189 | The job: the model server (vLLM) on two GPUs, preflight, the agent runs in parallel, summary; settings `TASKS`, `REPS`, `RUN_SECONDS`, `RECORD_WORKTREE`, `MAX_MODEL_LEN` |
| `cluster/jobs/node_check.sbatch` | 50 | Short job: CPU, Pythons and environments on a GPU node, then the container check there |
| `cluster/jobs/vllm_smoke.sbatch` | 140 | Model-server test: one tool call and the generation speed |
| `configs/mini/cluster-vllm.yaml` | 37 | Agent settings on the cluster: model, sampling (temperature 0.6, top_p 0.95, top_k 20, min_p 0), 32,768 tokens per reply, 250 steps, 500,000 output tokens, 60 s per command |
| `configs/mini/qwen3.6-35b-a3b_registry.json` | 24 | Prices each output token at 1e-6 so the agent's cost limit works as a token budget; context limits |
| `configs/mini/local-ollama.yaml` | 13 | Agent settings for `make one-task` with a local model (gpt-oss-20b via Ollama, 50 steps) |
| `configs/ollama/gpt-oss-20b-64k.Modelfile` | 2 | The local model with a 65,536-token context |

---

## Checklist: every file with code

Pilot, in the order the data flow through them
- [ ] `scripts/pilot/parse_trajs.py` (265)
- [ ] `scripts/pilot/export_footprints.py` (158)
- [ ] `scripts/pilot/ladder.py` (66)
- [ ] `scripts/pilot/fix_files.py` (39)
- [ ] `scripts/pilot/path_measures.py` (361)
- [ ] `scripts/pilot/choice_in_set.py` (605)
- [ ] `scripts/pilot/allocation.py` (447)
- [ ] `scripts/pilot/plot_pilot.py` (238)

Our runs
- [ ] `scripts/runs/our_runs.py` (59)

Cluster code and settings
- [ ] `harness/apptainer_env.py` (243)
- [ ] `cluster/run_swebench.py` (26)
- [ ] `cluster/setup_agent_venv.sh` (61)
- [ ] `cluster/build_task_sifs.sh` (50)
- [ ] `cluster/check_task_env.py` (168)
- [ ] `cluster/preflight.py` (136)
- [ ] `cluster/summarize_runs.py` (61)
- [ ] `cluster/jobs/agent_pilot.sbatch` (189)
- [ ] `cluster/jobs/node_check.sbatch` (50)
- [ ] `cluster/jobs/vllm_smoke.sbatch` (140)
- [ ] `configs/mini/cluster-vllm.yaml` (37)
- [ ] `configs/mini/qwen3.6-35b-a3b_registry.json` (24)
- [ ] `configs/mini/local-ollama.yaml` (13)
- [ ] `configs/ollama/gpt-oss-20b-64k.Modelfile` (2)

Build and environment
- [ ] `Makefile` (83)
- [ ] `pyproject.toml` (18), `poetry.toml` (3), `.python-version` (1), `.gitignore` (21)
