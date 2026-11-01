"""Two model calls through mini-swe-agent's own model class, before any agent run starts.

Run by the agent job once vLLM is up (not by hand):
    python cluster/preflight.py -c <swebench.yaml> -c configs/mini/cluster-vllm.yaml -c model.model_kwargs.api_base=...

It uses exactly the settings the agent will use and reports:
  tool call   the reply contains a valid bash tool call that mini-swe-agent can parse
  thinking    the reply's hidden reasoning reaches the message mini-swe-agent saves (reasoning_content);
              this is what the trajectory will contain for every turn
  budget      LiteLLM prices the reply from the registry, so cost_limit works as a token budget
  history     a second call, with the first reply (including its reasoning) and a command result in the
              history, is accepted by vLLM, as every later step of a run will be
  next step   whether that second reply again contains a tool call

Exit code 0: go ahead. Exit code 2: the first reply has no usable tool call, or vLLM rejects the history;
the job then stops before starting agents. Everything else only prints WARN: a reply without a tool call
is what mini-swe-agent calls a format error, and in a run it sends its format-error message back to the
model and continues (it stops a run only after 3 in a row).
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from minisweagent.config import get_config_from_spec
from minisweagent.exceptions import FormatError
from minisweagent.models import get_model
from minisweagent.utils.serialize import recursive_merge

SYSTEM = "You are a helpful assistant that can interact with a computer shell to solve programming tasks."
TASK = ("Work in two steps, one bash tool call per step. Step 1: list the Python files in the current "
        "directory. Step 2: count the lines of each of them. Explain your reasoning briefly before each call.")


def describe_format_error(e: FormatError) -> dict:
    """What mini-swe-agent kept of a reply it could not use: its error text and the raw reply."""
    msg = e.messages[0] if e.messages else {}
    response = (msg.get("extra") or {}).get("response") or {}
    choice = (response.get("choices") or [{}])[0] if isinstance(response, dict) else {}
    message = choice.get("message") or {}
    return {
        "error": (msg.get("content") or "").strip(),
        "finish_reason": choice.get("finish_reason"),
        "content": message.get("content"),
        "reasoning_chars": len(message.get("reasoning_content") or message.get("reasoning") or ""),
        "tool_calls": message.get("tool_calls"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", action="append", required=True)
    ap.add_argument("--out", default="", help="write both replies as JSON here")
    args = ap.parse_args()

    config = recursive_merge(*[get_config_from_spec(c) for c in args.config])
    model = get_model(config=config["model"])
    print(f"model {model.config.model_name} at {model.config.model_kwargs.get('api_base')}")
    shown = {k: v for k, v in model.config.model_kwargs.items() if k not in ("api_base", "api_key")}
    print(f"settings {json.dumps(shown)}")

    record: dict = {"first": None, "second": None}
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": TASK}]
    status = 0

    # --- first call ---------------------------------------------------------------------------
    t0 = time.time()
    try:
        first = model.query(messages)
    except FormatError as e:
        record["first"] = {"format_error": describe_format_error(e)}
        print(f"FAIL  tool call  no usable tool call: {record['first']['format_error']}")
        _write(args.out, record)
        return 2
    record["first"] = first
    actions = first.get("extra", {}).get("actions", [])
    print(f"{'PASS' if actions else 'FAIL'}  tool call  {[a.get('command') for a in actions]}  ({time.time() - t0:.1f} s)")
    if not actions:
        _write(args.out, record)
        return 2

    reasoning = first.get("reasoning_content") or ""
    content = first.get("content") or ""
    psf = first.get("provider_specific_fields") or {}
    print(f"{'PASS' if reasoning else 'WARN'}  thinking   {len(reasoning)} characters of reasoning_content, "
          f"{len(content)} of visible text; other fields: {sorted(psf)}")
    if reasoning:
        print(f"      starts: {reasoning[:160]!r}")

    usage = first.get("extra", {}).get("response", {}).get("usage", {}) or {}
    cost = first.get("extra", {}).get("cost", 0.0)
    out_tokens = usage.get("completion_tokens")
    active = bool(out_tokens) and abs(cost - out_tokens * 1e-6) < 1e-9
    print(f"{'PASS' if active else 'WARN'}  budget     cost {cost:.6f} for {out_tokens} output tokens "
          f"({usage.get('prompt_tokens')} input); cost_limit {'is' if active else 'is NOT'} a token budget")

    # --- second call, with the first reply and a command result in the history -------------------
    observation = {"output": "setup.py\nexample.py\n", "returncode": 0, "exception_info": ""}
    history = messages + [first] + model.format_observation_messages(first, [observation] * len(actions))
    t0 = time.time()
    try:
        second = model.query(history)
    except FormatError as e:
        # vLLM accepted the history and answered; the reply just had no usable tool call.
        info = describe_format_error(e)
        record["second"] = {"format_error": info}
        print(f"PASS  history    accepted ({time.time() - t0:.1f} s)")
        print(f"WARN  next step  no tool call in the second reply (finish_reason {info['finish_reason']}, "
              f"{info['reasoning_chars']} characters of reasoning); visible text: {str(info['content'])[:200]!r}")
        print("      in a run, mini-swe-agent sends its format-error message back and the model continues")
    except Exception as e:  # rejected by the server or LiteLLM: every multi-step run would fail
        record["second"] = {"exception": f"{type(e).__name__}: {e}"}
        print(f"FAIL  history    {type(e).__name__}: {str(e)[:500]}")
        status = 2
    else:
        record["second"] = second
        acts = second.get("extra", {}).get("actions", [])
        print(f"PASS  history    accepted ({time.time() - t0:.1f} s), "
              f"{len(second.get('reasoning_content') or '')} characters of reasoning")
        print(f"{'PASS' if acts else 'WARN'}  next step  {[a.get('command') for a in acts]}")

    _write(args.out, record)
    return status


def _write(path: str, record: dict) -> None:
    if path:
        with open(path, "w") as f:
            json.dump(record, f, indent=2, default=str)


if __name__ == "__main__":
    sys.exit(main())
