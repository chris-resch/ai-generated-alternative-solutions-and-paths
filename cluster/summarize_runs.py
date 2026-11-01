"""One line per agent run of a job: how it ended, how long, how many tokens, how much thinking was saved.

Run by the agent job at the end, or by hand:
    .venv-agent/bin/python cluster/summarize_runs.py runs/<jobid>
Reads runs/<jobid>/<replicate>/trajs/<task>/<task>.traj.json and writes runs/<jobid>/summary.tsv.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

COLUMNS = ["replicate", "task", "exit_status", "patch_lines", "api_calls", "turns_with_thinking",
           "thinking_chars_median", "output_tokens", "input_tokens_last", "minutes", "worktree_changes"]


def summarize(traj_path: Path) -> dict:
    data = json.loads(traj_path.read_text())
    info = data.get("info", {})
    msgs = data.get("messages", [])
    assistant = [m for m in msgs if m.get("role") == "assistant"]
    thinking = sorted(len(m.get("reasoning_content") or "") for m in assistant)
    usage = [((m.get("extra") or {}).get("response") or {}).get("usage") or {} for m in assistant]
    stamps = [(m.get("extra") or {}).get("timestamp") for m in msgs if (m.get("extra") or {}).get("timestamp")]
    records = [(m.get("extra") or {}).get("worktree") for m in msgs if (m.get("extra") or {}).get("worktree")]
    changes = sum(1 for w in records[1:] if w.get("changed"))  # the first record is the starting state
    submission = info.get("submission") or ""
    return {
        "exit_status": info.get("exit_status", ""),
        "patch_lines": sum(1 for line in submission.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---")),
        "api_calls": info.get("model_stats", {}).get("api_calls", len(assistant)),
        "turns_with_thinking": f"{sum(1 for t in thinking if t)}/{len(assistant)}",
        "thinking_chars_median": thinking[len(thinking) // 2] if thinking else 0,
        "output_tokens": sum(u.get("completion_tokens") or 0 for u in usage),
        "input_tokens_last": (usage[-1].get("prompt_tokens") if usage else "") or "",
        "minutes": round((max(stamps) - min(stamps)) / 60, 1) if len(stamps) > 1 else "",
        "worktree_changes": changes,
    }


def main(job_dir: str) -> None:
    root = Path(job_dir)
    rows = []
    for traj in sorted(root.glob("*/trajs/*/*.traj.json")):
        row = {"replicate": traj.parents[2].name, "task": traj.parent.name}
        try:
            row |= summarize(traj)
        except Exception as e:
            row["exit_status"] = f"unreadable: {type(e).__name__}"
        rows.append(row)
    lines = ["\t".join(COLUMNS)] + ["\t".join(str(r.get(c, "")) for c in COLUMNS) for r in rows]
    (root / "summary.tsv").write_text("\n".join(lines) + "\n")
    widths = [max(len(line.split("\t")[i]) for line in lines) for i in range(len(COLUMNS))]
    for line in lines:
        print("  ".join(cell.ljust(w) for cell, w in zip(line.split("\t"), widths)))
    print(f"\n{len(rows)} runs; table in {root / 'summary.tsv'}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
