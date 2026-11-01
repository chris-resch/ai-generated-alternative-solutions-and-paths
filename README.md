# Which roads lead to Rome? Code and data

AI coding agents attempt the same software problem many times, independently, and we turn their attempts into the
set of solutions the problem allows, with the path to each. This repository holds the code and data behind every
number, table and figure of the technical appendix: a pilot on 500 public issues with the published attempts of
11 models, linked to the projects' git histories, and repeated runs of one open model on a GPU cluster.

## Run it

| What it rebuilds | Needs | Command |
|---|---|---|
| every number, table and figure, from the shipped files (under a minute) | Python 3.12, Poetry | `make results` |
| the two analyses that read the projects' full git histories | git, internet, about 1.5 GB of disk | `make histories` |
| the shipped derived tables, from the public raw data | internet, awscli, about 5 GB of disk | `make pilot-data` |
| one agent run on one issue, scored | Docker, Ollama | `make one-task TASK=<issue id>` |
| repeated runs of the open model | Slurm, Apptainer, two 40 GB GPUs | job scripts in `cluster/jobs/` |

- **REPRODUCE.md**: every command with its flags, and measured run times.
- **CODE_GUIDE.md**: every file of the repository, what it does, what it reads and writes, and which numbers depend
  on it.

## Where each number comes from

| Numbers | Script | Output |
|---|---|---|
| Issues with a passing agent patch | `scripts/pilot/ladder.py` | `results/pilot/ladder.txt` |
| Steps, first edit, solving runs (input of the analyses below) | `scripts/pilot/parse_trajs.py`, `export_footprints.py`, `path_measures.py` | `data/derived/<model>/`, `results/pilot/paths/` |
| Solutions, distinct solutions, the merged solution among them | `scripts/pilot/choice_in_set.py` | `results/pilot/choice_in_set/summary.md` |
| Files solving required, reach, authors, candidates, Tables A1 and A2 | `scripts/pilot/allocation.py` | `results/pilot/allocation/summary.md` |
| Figures 2 and 3 | `scripts/pilot/plot_pilot.py` | `results/figures/` |
| Our runs: passing runs and their solutions | `cluster/jobs/agent_pilot.sbatch`, the SWE-bench scorer, `choice_in_set.py`, `scripts/runs/our_runs.py` | `data/runs/<job>/`, `results/runs/summary.md` |

People appear in all outputs only as pseudonymous numbers; no name or e-mail address is written.

## Layout

| Folder | Holds |
|---|---|
| `scripts/pilot/` | the pilot: parsing the published runs, path measures, solutions, authors, figures |
| `scripts/runs/` | the summary of our repeated runs |
| `data/` | the public issues and verdicts, the derived tables, our runs (checksums in `data/SHA256SUMS`) |
| `results/` | tables and figures as the scripts write them |
| `harness/`, `cluster/`, `configs/` | the agent's task container, cluster setup and job scripts, agent and model settings |

## Data sources

All raw data are public. Issues: SWE-bench Verified on Hugging Face (`princeton-nlp/SWE-bench_Verified`, revision
c104f840). Attempts: the 11 bash-only runs of 17 Feb 2026 in the public bucket `s3://swe-bench-submissions`.
Verdicts: the SWE-bench experiments repository on GitHub at commit 40f164d5b8. Project histories: the projects'
public repositories on GitHub. The repository ships derived tables, not the raw trajectories. Our own runs are
shipped in full, with user names, folder paths and node names replaced.
