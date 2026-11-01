# Reproduction targets; run them from the repository root. REPRODUCE.md explains every command and flag.
#
#   make results          every number, table and figure from the shipped files (under a minute)
#   make histories        the analyses that read the projects' full git histories (clones about 1.5 GB)
#   make pilot-data       rebuild data/derived from the public raw data (about an hour; needs internet and awscli)
#   make one-task TASK=…  one agent run on one task with a local model, then scored (needs Docker, Ollama)
#   make check-data       check the shipped data against data/SHA256SUMS
#
# Our repeated runs on a GPU cluster have no target: see cluster/ and REPRODUCE.md.

PYTHON     ?= poetry run python
VERIFIED   := data/public/swebench_verified.jsonl
VERDICTS   := data/public/verdicts
MODELS     := claude-4-5-haiku-high claude-4-5-opus-high claude-4-5-sonnet-high claude-4-6-opus deepseek-3-2-high \
              gemini-3-flash-high glm-5-high gpt-5-2-high gpt-5-mini kimi-k2-5-high minimax-2-5-high
TASK       ?= django__django-11099
REPOS_FULL ?= data/repos_full
# a fixed hash seed, so that ties between equally common files are listed in the same order on every run
export PYTHONHASHSEED := 0

# our repeated runs: four cluster jobs, five runs each, two issues per job
JOBS       := 55791312 55791526 55791937 55792710
OUR_ISSUES := django__django-13121 django__django-16315 pydata__xarray-3305 sphinx-doc__sphinx-9658 \
              pydata__xarray-4075 sphinx-doc__sphinx-8551 matplotlib__matplotlib-24627 scikit-learn__scikit-learn-13439
OUR_RUNS   := $(foreach j,$(JOBS),$(foreach r,01 02 03 04 05,qwen-$(j)-r$(r)=data/runs/$(j)/qwen3.6-35b-a3b-r$(r)/trajs/preds.json:data/runs/$(j)/scores/hosted_vllm__qwen3.6-35b-a3b.cluster-$(j)-r$(r).json))

.PHONY: results histories pilot-data one-task check-data

# ---- every number, table and figure from the shipped files -----------------------------------------------------
results:
	mkdir -p results/pilot
	$(PYTHON) scripts/pilot/ladder.py --verdicts $(VERDICTS) --verified $(VERIFIED) --out results/pilot \
	  > results/pilot/ladder.txt
	$(PYTHON) scripts/pilot/fix_files.py --verified $(VERIFIED) --out results/pilot/fix_files.jsonl
	$(PYTHON) scripts/pilot/path_measures.py --paths data/derived/*/path.jsonl \
	  --results $(VERDICTS)/*/per_instance_details.json --human results/pilot/fix_files.jsonl --out results/pilot/paths
	$(PYTHON) scripts/pilot/plot_pilot.py
	$(PYTHON) scripts/runs/our_runs.py

# ---- the analyses that read the projects' full git histories (cloned into $(REPOS_FULL) on first use) ----------
histories:
	$(PYTHON) scripts/pilot/choice_in_set.py --verified $(VERIFIED) --patches data/derived/*/patches.jsonl \
	  --results $(VERDICTS)/*/per_instance_details.json --repos-dir $(REPOS_FULL) --out results/pilot/choice_in_set
	$(PYTHON) scripts/pilot/choice_in_set.py --verified $(VERIFIED) --patches data/derived/*/patches.jsonl \
	  --results $(VERDICTS)/*/per_instance_details.json --repos-dir $(REPOS_FULL) --tasks $(OUR_ISSUES) \
	  --extra $(OUR_RUNS) --out results/runs/choice_in_set
	$(PYTHON) scripts/pilot/allocation.py --repos-dir $(REPOS_FULL)

# ---- derived tables from the public raw data -------------------------------------------------------------------
pilot-data:
	for m in $(MODELS); do aws s3 sync --no-sign-request \
	  s3://swe-bench-submissions/bash-only/20260217_mini-v2.0.0_$$m/trajs/ data/public/$$m/trajs/; done
	$(PYTHON) -c "from datasets import load_dataset; load_dataset('princeton-nlp/SWE-bench_Verified', split='test', \
	  revision='c104f840cc67f8b6eec6f759ebc8b2693d585d4a').to_json('$(VERIFIED)')"
	test -d ext/experiments || git clone --filter=blob:none --no-checkout --sparse \
	  https://github.com/SWE-bench/experiments.git ext/experiments
	git -C ext/experiments sparse-checkout set --no-cone '/evaluation/verified/20260217_mini-v2.0.0_*/per_instance_details.json'
	git -C ext/experiments checkout -q 40f164d5b8f1d249bf95a6df8b74b577fd8e519d
	for m in $(MODELS); do mkdir -p $(VERDICTS)/20260217_mini-v2.0.0_$$m && \
	  cp ext/experiments/evaluation/verified/20260217_mini-v2.0.0_$$m/per_instance_details.json \
	     $(VERDICTS)/20260217_mini-v2.0.0_$$m/; done
	for m in $(MODELS); do $(PYTHON) scripts/pilot/parse_trajs.py data/public/$$m/trajs --run $$m \
	  --out data/derived/$$m; done
	$(PYTHON) scripts/pilot/export_footprints.py --verified $(VERIFIED) --derived data/derived --repos data/repos

# ---- one agent run on one task ---------------------------------------------------------------------------------
IMAGE = swebench/sweb.eval.x86_64.$(subst __,_1776_,$(TASK)):latest
one-task:
	mkdir -p runs/one-task
	ollama create gpt-oss-20b-64k -f configs/ollama/gpt-oss-20b-64k.Modelfile
	docker pull --platform linux/amd64 $(IMAGE)
	poetry run mini-extra swebench-single --subset verified --split test -i $(TASK) -c swebench.yaml \
	  -c configs/mini/local-ollama.yaml -y --exit-immediately -o runs/one-task/$(TASK).traj.json
	$(PYTHON) -c "import json; t = json.load(open('runs/one-task/$(TASK).traj.json')); \
	  json.dump([{'instance_id': '$(TASK)', 'model_name_or_path': 'local', \
	  'model_patch': t['info'].get('submission') or ''}], open('runs/one-task/preds.json', 'w'))"
	$(PYTHON) -m swebench.harness.run_evaluation --dataset_name princeton-nlp/SWE-bench_Verified \
	  --predictions_path runs/one-task/preds.json --instance_ids $(TASK) --max_workers 1 --run_id one-task

# ---- checks ---------------------------------------------------------------------------------------------------
check-data:
	@bad=$$(shasum -a 256 -c data/SHA256SUMS 2>&1 | grep -v ': OK$$'); \
	  if [ -n "$$bad" ]; then echo "$$bad"; exit 1; fi; echo "data/: every file matches data/SHA256SUMS"
