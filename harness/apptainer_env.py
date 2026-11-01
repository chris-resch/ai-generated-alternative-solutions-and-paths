"""Apptainer environment for mini-swe-agent runs on the cluster.

A small subclass of mini-swe-agent's own SingularityEnvironment (version 2.4.6,
src/minisweagent/environments/singularity.py). The parent already does the main job: per run it
unpacks the task image into a private, writable folder (a "sandbox") and runs every agent command
there with `apptainer exec ... bash -c <command>`. This class changes four things:

1. Image. mini-swe-agent hands the parent `docker://docker.io/swebench/sweb.eval.x86_64.<task>:latest`,
   which would be downloaded at run time. Compute nodes have no internet, so the image is read from a
   SIF file built beforehand on a login node (cluster/build_task_sifs.sh), named after the image:
   <sif_dir>/sweb.eval.x86_64.<task>.sif.
2. Containment flags as described to the computing centre: --containall --no-home --cleanenv. The parent's default also
   has --fakeroot, which is left out (the unpacked files belong to us, so no fake root is needed).
3. A per-run work folder (--workdir). With --containall, each command would otherwise start with an
   empty /tmp; with it, /tmp and /var/tmp persist between the commands of one run, as in Docker.
4. Optional (record_worktree: true): after every command, record the state of the repository
   (`git diff HEAD` plus the list of untracked files) in the trajectory. It is stored in the result
   message's `extra` field, which mini-swe-agent saves but never sends to the model, so the agent does
   not see it. Only changes are stored in full; unchanged steps carry just a fingerprint.

Used through cluster/run_swebench.py, which runs mini-swe-agent's SWE-bench runner with the name
"singularity" pointing to this class (config: environment.environment_class: singularity).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from minisweagent.environments.singularity import SingularityEnvironment, SingularityEnvironmentConfig

_APPTAINER_VERSION: str | None = None
MAX_DIFF_CHARS = 200_000  # larger diffs are cut and flagged (a run that rewrites a whole module)


class ApptainerEnvironmentConfig(SingularityEnvironmentConfig):
    executable: str = os.getenv("MSWEA_SINGULARITY_EXECUTABLE", "apptainer")
    exec_args: list[str] = ["--containall", "--no-home", "--cleanenv"]
    """Flags for every `apptainer exec`: no host files except the sandbox, no home folder, no host variables."""
    sif_dir: str = os.getenv("TASK_SIF_DIR", "")
    """Folder with one SIF per task image (cluster/build_task_sifs.sh)."""
    sandbox_root: str = os.getenv("SANDBOX_ROOT", "")
    """Where the per-run sandboxes are unpacked. Empty: the system temp folder ($TMPDIR, else /tmp)."""
    use_workdir: bool = True
    """Give each run its own /tmp and /var/tmp that persist between its commands."""
    record_worktree: bool = False
    """After every command, store `git diff HEAD` and untracked files in the trajectory (hidden from the agent)."""
    worktree_timeout: int = 30


def sif_for_image(image: str, sif_dir: str) -> Path:
    """docker://docker.io/swebench/sweb.eval.x86_64.django_1776_django-11099:latest
    -> <sif_dir>/sweb.eval.x86_64.django_1776_django-11099.sif"""
    if image.endswith(".sif"):
        path = Path(image)
    else:
        name = image.removeprefix("docker://").rsplit("/", 1)[-1]
        name = name.split("@", 1)[0].split(":", 1)[0]
        if not sif_dir:
            raise ValueError("No sif_dir set (environment.sif_dir or TASK_SIF_DIR)")
        path = Path(sif_dir) / f"{name}.sif"
    if not path.is_file():
        raise FileNotFoundError(
            f"No SIF for image {image!r} at {path}. Build it on a login node: bash cluster/build_task_sifs.sh <task id>"
        )
    return path


def apptainer_version(executable: str) -> str:
    global _APPTAINER_VERSION
    if _APPTAINER_VERSION is None:
        try:
            out = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=30)
            _APPTAINER_VERSION = out.stdout.strip() or out.stderr.strip()
        except Exception as e:  # recorded, not fatal
            _APPTAINER_VERSION = f"unknown ({type(e).__name__})"
    return _APPTAINER_VERSION


def remove_tree(path: Path) -> None:
    """Delete a sandbox. Image folders can be read-only, so give ourselves write permission first."""
    if not path.exists():
        return
    subprocess.run(["chmod", "-R", "u+rwX", str(path)], capture_output=True)
    shutil.rmtree(path, ignore_errors=True)


class ApptainerEnvironment(SingularityEnvironment):
    def __init__(self, *, config_class: type = ApptainerEnvironmentConfig, **kwargs):
        self.base_dir: Path | None = None
        self.work_dir: Path | None = None
        self.sif_path: Path | None = None
        self.sandbox_build_seconds: float | None = None
        self._last_diff_sha: str | None = None
        super().__init__(config_class=config_class, **kwargs)

    # --- setup and teardown -------------------------------------------------------------------

    def _build_sandbox(self) -> Path:
        """Unpack the task's SIF into <sandbox_root>/minisweagent-<id>/rootfs; work folder next to it."""
        self.sif_path = sif_for_image(self.config.image, self.config.sif_dir)
        root = Path(self.config.sandbox_root or tempfile.gettempdir())
        root.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None
        for attempt in range(self.config.sandbox_build_retries):
            base = root / f"minisweagent-{uuid.uuid4().hex[:8]}"
            sandbox = base / "rootfs"
            work = base / "work"
            work.mkdir(parents=True)
            t0 = time.time()
            try:
                subprocess.run(
                    [self.config.executable, *self.config.global_args, "build", "--sandbox", str(sandbox), str(self.sif_path)],
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError as e:
                last_error = e
                remove_tree(base)
                self.logger.error(
                    f"Building sandbox from {self.sif_path} failed (attempt {attempt + 1}): {e.stderr!r}"
                )
                continue
            self.sandbox_build_seconds = round(time.time() - t0, 1)
            self.base_dir, self.work_dir = base, work
            return sandbox
        raise RuntimeError(f"Could not build sandbox from {self.sif_path}") from last_error

    def cleanup(self):
        base = getattr(self, "base_dir", None)
        if base is not None:
            remove_tree(base)
            self.base_dir = None

    # --- running commands ---------------------------------------------------------------------

    def _exec_command(self, command: str, cwd: str = "") -> list[str]:
        cmd = [self.config.executable, *self.config.global_args, "exec", *self.config.exec_args]
        if self.config.use_workdir and self.work_dir is not None:
            cmd.extend(["--workdir", str(self.work_dir)])
        work_dir = cwd or self.config.cwd
        if work_dir and work_dir != "/":
            cmd.extend(["--pwd", work_dir])
        for key in self.config.forward_env:
            if (value := os.getenv(key)) is not None:
                cmd.extend(["--env", f"{key}={value}"])
        for key, value in self.config.env.items():
            cmd.extend(["--env", f"{key}={value}"])
        cmd.extend(["--writable", str(self.sandbox_dir), "bash", "-c", command])
        return cmd

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        """Run one agent command. Same result dict as the parent; with record_worktree also extra.worktree."""
        command = action.get("command", "")
        try:
            result = subprocess.run(
                self._exec_command(command, cwd),
                text=True,
                timeout=timeout or self.config.timeout,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            output = {"output": result.stdout, "returncode": result.returncode, "exception_info": ""}
        except Exception as e:
            raw_output = getattr(e, "output", None)
            raw_output = (
                raw_output.decode("utf-8", errors="replace") if isinstance(raw_output, bytes) else (raw_output or "")
            )
            output = {
                "output": raw_output,
                "returncode": -1,
                "exception_info": f"An error occurred while executing the command: {e}",
                "extra": {"exception_type": type(e).__name__, "exception": str(e)},
            }
        self._check_finished(output)  # raises Submitted on the submit command; the patch is the record then
        if self.config.record_worktree:
            output.setdefault("extra", {})["worktree"] = self._worktree_state()
        return output

    def _worktree_state(self) -> dict[str, Any]:
        """git diff HEAD and untracked files in the working directory, read without changing anything.

        --no-optional-locks keeps git from refreshing its index file, so recording leaves the repository
        exactly as the agent left it."""
        script = (
            "git --no-optional-locks diff --no-color --no-ext-diff HEAD; "
            "printf '\\n__UNTRACKED__\\n'; "
            "git --no-optional-locks ls-files --others --exclude-standard"
        )
        t0 = time.time()
        try:
            res = subprocess.run(
                self._exec_command(script),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=self.config.worktree_timeout,
            )
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
        diff, _, untracked = res.stdout.rpartition("\n__UNTRACKED__\n")
        sha = hashlib.sha1(res.stdout.encode("utf-8", errors="replace")).hexdigest()
        state: dict[str, Any] = {"sha1": sha, "seconds": round(time.time() - t0, 2)}
        if res.returncode != 0:
            state["error"] = res.stderr[-2000:]
        if sha != self._last_diff_sha:
            state["changed"] = True
            state["diff"] = diff[:MAX_DIFF_CHARS]
            if len(diff) > MAX_DIFF_CHARS:
                state["diff_truncated_chars"] = len(diff) - MAX_DIFF_CHARS
            state["untracked"] = [line for line in untracked.splitlines() if line]
            self._last_diff_sha = sha
        else:
            state["changed"] = False
        return state

    # --- record in the trajectory -------------------------------------------------------------

    def serialize(self) -> dict:
        data = super().serialize()
        sif = self.sif_path
        data["info"]["environment_run"] = {
            "sif": str(sif) if sif else None,
            "sif_bytes": sif.stat().st_size if sif and sif.exists() else None,
            "sandbox_build_seconds": self.sandbox_build_seconds,
            "apptainer": apptainer_version(self.config.executable),
            "host": os.uname().nodename,
            "slurm_job_id": os.getenv("SLURM_JOB_ID"),
        }
        return data


__all__ = ["ApptainerEnvironment", "ApptainerEnvironmentConfig", "sif_for_image"]
