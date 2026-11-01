"""
HTTP request handler for the animation server.

Runs on the server's socket threads. It never touches ``bpy``: every request
that needs Blender data is handed to the main thread through ``dispatch``.
"""

from __future__ import annotations

import http.server
import json
import traceback
import urllib.parse

from . import dispatch
from .security import (
    RequestRejected,
    decompress_body,
    is_json_or_binary_content_type,
    read_request_body,
    validate_request_headers,
)

PROTOCOL_VERSION = 2

# Status checks and armature lists are quick; when Blender can't start them
# within a few seconds it is busy, and Studio should say so rather than wait.
_QUICK_START_TIMEOUT = 4.0
_QUICK_RUN_TIMEOUT = 30.0


def _addon_version() -> str:
    try:
        from .. import bl_info  # noqa: PLC0415

        return ".".join(str(part) for part in bl_info.get("version", ()))
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Main-thread jobs. Each wraps one of the ``requests`` executors, which report
# through ``pending_responses`` keyed by a unique task id.
# ---------------------------------------------------------------------------


def _take_response(task_id):
    from .requests import pending_responses  # noqa: PLC0415

    return pending_responses.pop(task_id, (False, "No response was produced."))


def _job_list_armatures():
    from .requests import execute_list_armatures  # noqa: PLC0415

    task_id = dispatch.new_task_id("list")
    execute_list_armatures(task_id)
    return _take_response(task_id)


def _job_animation_status(armature_name, last_known_hash):
    from ..core.utils import get_cached_armature_hash, get_object_by_name  # noqa: PLC0415

    obj = get_object_by_name(armature_name)
    if not (obj and obj.type == "ARMATURE"):
        return None
    current_hash = get_cached_armature_hash(armature_name)
    return {"has_update": current_hash != last_known_hash, "hash": current_hash}


def _job_bone_rest(armature_name):
    from .requests import execute_get_bone_rest  # noqa: PLC0415

    task_id = dispatch.new_task_id("rest")
    execute_get_bone_rest(task_id, armature_name)
    return _take_response(task_id)


def _job_export(armature_name, target_bone_rest, live_preview, delta_base_hash):
    from .requests import execute_in_main_thread  # noqa: PLC0415

    task_id = dispatch.new_task_id("export")
    execute_in_main_thread(
        task_id, armature_name, target_bone_rest, live_preview, delta_base_hash
    )
    return _take_response(task_id)


def _job_import(animation_data, target_armature):
    from .requests import execute_import_animation  # noqa: PLC0415

    task_id = dispatch.new_task_id("import")
    execute_import_animation(task_id, animation_data, target_armature)
    return _take_response(task_id)


def _job_confirm_rig_changes(armature_name, changes):
    from .requests import execute_confirm_rig_changes  # noqa: PLC0415

    task_id = dispatch.new_task_id("rig")
    execute_confirm_rig_changes(task_id, armature_name, changes)
    return _take_response(task_id)


class AnimationHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler for the animation server endpoints."""

    server_version = "RBXMonkeySync/2"

    # ------------------------------------------------------------------ utils

    def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler API
        # One line per request in Blender's console was the main source of log
        # spam during live sync; errors are still printed by send_detailed_error.
        return

    def _send_body(self, code, body: bytes, content_type: str, extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, code, payload, extra_headers=None):
        self._send_body(
            code,
            json.dumps(payload).encode("utf-8"),
            "application/json",
            extra_headers,
        )

    def send_detailed_error(self, code, message, details=None, extra_headers=None):
        """Send an error with detailed information."""
        if code >= 500 or code == 403:
            print(f"Blender Addon: HTTP {code} for {self.command} {self.path}: {message}")
        self._send_json(
            code,
            {"error": True, "code": code, "message": str(message), "details": details},
            extra_headers,
        )

    def _parsed(self):
        parsed = urllib.parse.urlsplit(self.path)
        # parse_qs decodes exactly once ('+' -> ' ', '%2B' -> '+').
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        return parsed.path, query

    @staticmethod
    def _path_argument(path, prefix):
        """The URL-decoded segment after ``prefix``. '+' in a path is literal."""
        return urllib.parse.unquote(path[len(prefix):])

    def _run(self, fn, *args, quick=False, **kwargs):
        """Run a job on the main thread, translating dispatch errors to HTTP."""
        try:
            if quick:
                return True, dispatch.run_on_main_thread(
                    fn,
                    *args,
                    start_timeout=_QUICK_START_TIMEOUT,
                    run_timeout=_QUICK_RUN_TIMEOUT,
                    **kwargs,
                )
            return True, dispatch.run_on_main_thread(fn, *args, **kwargs)
        except dispatch.BlenderBusy as exc:
            self.send_detailed_error(503, str(exc), extra_headers={"Retry-After": "1"})
        except dispatch.JobTimedOut as exc:
            self.send_detailed_error(504, str(exc))
        except Exception as exc:  # executor bug: report it instead of dropping the socket
            traceback.print_exc()
            self.send_detailed_error(500, f"Server error: {exc}")
        return False, None

    def _validate(self) -> bool:
        try:
            validate_request_headers(self.headers)
        except RequestRejected as exc:
            self.send_detailed_error(exc.status, exc.message)
            return False
        return True

    # --------------------------------------------------------------- methods

    def do_OPTIONS(self):
        """No CORS: browsers must not be able to call this server."""
        self.send_detailed_error(403, "Cross-origin requests are not allowed.")

    def do_GET(self):
        if not self._validate():
            return
        path, query = self._parsed()

        if path == "/oauth2/callback":
            # The Roblox login redirect lands here when this server owns the
            # login's callback port (31337). No bpy access: the token exchange
            # runs on this socket thread like the standalone callback server.
            from ..core import auth  # noqa: PLC0415

            status, text = auth.handle_shared_callback(self.path)
            self._send_body(status, text.encode("utf-8"), "text/plain; charset=utf-8")
            return

        if path == "/ping":
            status = dispatch.status_snapshot()
            self._send_json(
                200,
                {
                    "ok": True,
                    "server": "RBXMonkey",
                    "protocol": PROTOCOL_VERSION,
                    "version": _addon_version(),
                    "busy": status["busy"],
                    "blend_file": status["blend_file"],
                },
            )
            return

        if path == "/animation_status":
            armature_name = (query.get("armature") or [""])[0]
            last_known_hash = (query.get("last_known_hash") or [""])[0]
            if not armature_name:
                self.send_detailed_error(400, "Armature name not provided")
                return
            ok, data = self._run(
                _job_animation_status, armature_name, last_known_hash, quick=True
            )
            if not ok:
                return
            if data is None:
                self.send_detailed_error(404, f"Armature '{armature_name}' not found")
                return
            self._send_json(200, data)
            return

        if path == "/list_armatures":
            ok, result = self._run(_job_list_armatures, quick=True)
            if not ok:
                return
            success, data = result
            if not success:
                self.send_detailed_error(500, data)
                return
            self._send_body(200, data, "application/json")
            return

        if path.startswith("/get_bone_rest/"):
            armature_name = self._path_argument(path, "/get_bone_rest/")
            ok, result = self._run(_job_bone_rest, armature_name)
            if not ok:
                return
            success, data = result
            if not success:
                self.send_detailed_error(500, data)
                return
            self._send_body(200, data, "application/json")
            return

        if path.startswith("/export_animation/"):
            armature_name = self._path_argument(path, "/export_animation/")
            live_preview = (query.get("live_preview") or ["0"])[0] == "1"
            self._respond_export(armature_name, None, live_preview, None)
            return

        self.send_detailed_error(404, "Not Found", "Invalid endpoint")

    def do_POST(self):
        if not self._validate():
            return
        if not is_json_or_binary_content_type(self.headers.get("Content-Type")):
            self.send_detailed_error(
                415, "POST bodies must be application/json or application/octet-stream."
            )
            return
        path, query = self._parsed()
        try:
            body = read_request_body(self.headers, self.rfile)
        except RequestRejected as exc:
            self.send_detailed_error(exc.status, exc.message)
            return

        if path.startswith("/export_animation_delta/") or path.startswith("/export_animation/"):
            is_delta = path.startswith("/export_animation_delta/")
            prefix = "/export_animation_delta/" if is_delta else "/export_animation/"
            armature_name = self._path_argument(path, prefix)
            request_data = self._json_body(body)
            if request_data is _INVALID:
                return
            target_bone_rest = request_data.get("target_bone_rest")
            base_hash = request_data.get("base_hash", "") if is_delta else None
            live_preview = (query.get("live_preview") or ["0"])[0] == "1"
            self._respond_export(
                armature_name,
                target_bone_rest,
                False if is_delta else live_preview,
                (base_hash or "") if is_delta else None,
            )
            return

        if path == "/import_animation":
            target_armature = (query.get("armature") or [None])[0] or None
            try:
                payload = decompress_body(body)
            except RequestRejected as exc:
                self.send_detailed_error(exc.status, exc.message)
                return
            try:
                animation_data = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self.send_detailed_error(400, f"Animation data is not valid JSON: {exc}")
                return
            if not isinstance(animation_data, dict) or not isinstance(
                animation_data.get("kfs"), list
            ):
                self.send_detailed_error(400, "Animation data must be an object with a 'kfs' list.")
                return
            ok, result = self._run(_job_import, animation_data, target_armature)
            if not ok:
                return
            success, message = result
            if not success:
                self.send_detailed_error(500, message)
                return
            self._send_json(200, {"success": True, "message": message})
            return

        if path.startswith("/confirm_rig_changes/"):
            # Studio applied these queued joint changes; rebase the bones.
            armature_name = self._path_argument(path, "/confirm_rig_changes/")
            request_data = self._json_body(body)
            if request_data is _INVALID:
                return
            changes = request_data.get("changes")
            if not armature_name or not isinstance(changes, list):
                self.send_detailed_error(400, "Send the armature name and a 'changes' list.")
                return
            ok, result = self._run(_job_confirm_rig_changes, armature_name, changes)
            if not ok:
                return
            success, data = result
            if not success:
                self.send_detailed_error(500, data)
                return
            self._send_json(200, dict(data, success=True))
            return

        self.send_detailed_error(404, "Not Found")

    # -------------------------------------------------------------- helpers

    def _json_body(self, body):
        if not body:
            return {}
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.send_detailed_error(400, f"Request body is not valid JSON: {exc}")
            return _INVALID
        return data if isinstance(data, dict) else {}

    def _respond_export(self, armature_name, target_bone_rest, live_preview, delta_base_hash):
        if not armature_name:
            self.send_detailed_error(400, "Armature name not provided")
            return
        ok, result = self._run(
            _job_export, armature_name, target_bone_rest, live_preview, delta_base_hash
        )
        if not ok:
            return
        success, data = result
        if not success:
            self.send_detailed_error(500, data)
            return
        self._send_body(200, data, "application/octet-stream")


_INVALID = object()
