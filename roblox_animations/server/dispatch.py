"""
Hands work from the socket threads to Blender's main thread.

Blender's API may only be used from the main thread, and the main thread only
runs our timer while Blender is idle: a file dialog, a render or a modal
transform pauses it. Socket threads therefore never touch ``bpy``. They queue a
job and wait for the timer to run it, and they answer "Blender is busy" instead
of hanging when the timer doesn't get to it in time.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

# A queued job that hasn't started within this time is abandoned.
DEFAULT_START_TIMEOUT = 8.0
# Exports of long animations can take a while once they've started.
DEFAULT_RUN_TIMEOUT = 300.0
# The main thread counts as busy when its timer hasn't ticked for this long.
BUSY_AFTER_SECONDS = 1.5


class BlenderBusy(Exception):
    """The main thread did not pick the job up in time."""


class JobTimedOut(Exception):
    """The job started but did not finish in time."""


class _Job:
    __slots__ = ("fn", "args", "kwargs", "state", "result", "error", "done", "lock")

    def __init__(self, fn: Callable[..., Any], args, kwargs):
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.state = "queued"  # queued -> running -> done | cancelled
        self.result: Any = None
        self.error: Optional[BaseException] = None
        self.done = threading.Event()
        self.lock = threading.Lock()


_jobs: "queue.SimpleQueue[_Job]" = queue.SimpleQueue()
_status: Dict[str, Any] = {"last_tick": 0.0, "blend_file": "", "running_job": False}


def new_task_id(prefix: str = "task") -> str:
    """A collision-free task id (time-based ids collide within one clock tick)."""
    return f"{prefix}:{uuid.uuid4().hex}"


def _on_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


def run_on_main_thread(
    fn: Callable[..., Any],
    *args,
    start_timeout: float = DEFAULT_START_TIMEOUT,
    run_timeout: float = DEFAULT_RUN_TIMEOUT,
    **kwargs,
) -> Any:
    """Run ``fn`` on Blender's main thread and return its result.

    Called from the main thread itself, the job runs inline. Raises BlenderBusy
    when the job was not started within ``start_timeout`` (it is then never
    run), JobTimedOut when it started but did not finish within
    ``run_timeout``, and re-raises any exception ``fn`` raised.
    """
    if _on_main_thread():
        return fn(*args, **kwargs)

    job = _Job(fn, args, kwargs)
    _jobs.put(job)
    deadline = time.monotonic() + start_timeout
    while not job.done.wait(0.05):
        with job.lock:
            if job.state == "queued" and time.monotonic() >= deadline:
                job.state = "cancelled"
                raise BlenderBusy(
                    "Blender is busy (a dialog, render or modal tool is running). "
                    "Try again when it's idle."
                )
            if job.state == "running":
                break
    if not job.done.wait(run_timeout):
        raise JobTimedOut(f"Blender did not finish the request within {int(run_timeout)} s.")
    if job.error is not None:
        raise job.error
    return job.result


def drain(time_budget: float = 0.5) -> int:
    """Run queued jobs on the main thread. Returns how many ran."""
    ran = 0
    started = time.monotonic()
    while True:
        try:
            job = _jobs.get_nowait()
        except queue.Empty:
            return ran
        with job.lock:
            if job.state != "queued":
                continue
            job.state = "running"
        _status["running_job"] = True
        try:
            job.result = job.fn(*job.args, **job.kwargs)
        except BaseException as exc:  # handed back to the waiting thread
            job.error = exc
        finally:
            _status["running_job"] = False
            with job.lock:
                job.state = "done"
            job.done.set()
        ran += 1
        if time.monotonic() - started >= time_budget:
            return ran


def note_tick(blend_file: str = "") -> None:
    """Called by the main-thread timer each tick."""
    _status["last_tick"] = time.monotonic()
    _status["blend_file"] = blend_file


def status_snapshot() -> Dict[str, Any]:
    """Thread-safe view of the main thread's health for /ping."""
    last_tick = float(_status.get("last_tick") or 0.0)
    idle_for = time.monotonic() - last_tick if last_tick else None
    return {
        "busy": bool(_status.get("running_job"))
        or (idle_for is not None and idle_for > BUSY_AFTER_SECONDS),
        "blend_file": _status.get("blend_file") or "",
        "seconds_since_tick": round(idle_for, 2) if idle_for is not None else None,
    }


def clear() -> None:
    """Cancel every queued job (used when the server stops)."""
    while True:
        try:
            job = _jobs.get_nowait()
        except queue.Empty:
            return
        with job.lock:
            if job.state == "queued":
                job.state = "cancelled"
                job.error = BlenderBusy("The sync server stopped.")
                job.done.set()
