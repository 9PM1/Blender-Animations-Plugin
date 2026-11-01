"""
HTTP and WebSocket server management for live sync.

Sockets are served on background threads; Blender data is only touched by the
``_server_tick`` timer on the main thread, which runs queued jobs (see
``dispatch``) and publishes live-sync notifications.
"""

from __future__ import annotations

import http.server
import os
import socket
import threading
import time
import traceback
from typing import List, Optional

import bpy

from . import dispatch
from .handler import AnimationHandler
from .websocket import LiveSyncWebSocketServer
from .requests import execute_in_main_thread, pending_responses
from ..core.utils import get_armature_timeline_hash

# Live sync uses the next port for its WebSocket, so the HTTP port must leave room.
MIN_PORT = 1024
MAX_PORT = 65534

# Global server state managed via Blender timers
server_instance: Optional["SafeTCPServer"] = None
server_should_run: bool = False
server_port: Optional[int] = None
last_start_error: str = ""
_timer_registered: bool = False
_extra_servers: List["SafeTCPServer"] = []
_server_threads: List[threading.Thread] = []
websocket_server: Optional[LiveSyncWebSocketServer] = None
_last_websocket_publish: float = 0.0
_dirty_armatures: dict[str, float] = {}
_processing_websocket_sync: bool = False


def _live_sync_depsgraph_handler(_scene, depsgraph) -> None:
    """Mark subscribed armatures dirty without hashing them on every graph update."""
    if websocket_server is None or _processing_websocket_sync:
        return
    subscribed = websocket_server.subscribed_armatures()
    if not subscribed:
        return
    try:
        # Only Action edits trigger an immediate check. Armature object updates
        # also fire on every frame of playback; NLA, driver and constraint
        # edits are picked up by the 2-second check (the hash covers them).
        changed = any(isinstance(update.id, bpy.types.Action) for update in depsgraph.updates)
    except Exception:
        changed = True
    if changed:
        dirty_at = time.monotonic()
        for armature in subscribed:
            _dirty_armatures[armature] = dirty_at


def _register_live_sync_handler() -> None:
    if _live_sync_depsgraph_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_live_sync_depsgraph_handler)


def _unregister_live_sync_handler() -> None:
    if _live_sync_depsgraph_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_live_sync_depsgraph_handler)


def _process_websocket_sync_requests() -> None:
    global _processing_websocket_sync
    if websocket_server is None:
        return
    for client, request in websocket_server.drain_sync_requests():
        request_id = request.get("request_id", "")
        trigger_hash = request.get("trigger_hash", "")
        armature = request.get("armature", "")
        task_id = dispatch.new_task_id("ws")
        try:
            _processing_websocket_sync = True
            execute_in_main_thread(
                task_id,
                armature,
                request.get("target_bone_rest"),
                False,
                request.get("base_hash", ""),
            )
            success, payload = pending_responses.pop(task_id)
        except Exception as exc:
            success, payload = False, str(exc)
            traceback.print_exc()
        finally:
            _processing_websocket_sync = False
        websocket_server.publish_sync_response(
            client,
            request_id,
            trigger_hash,
            success,
            payload,
        )


def get_server_status() -> bool:
    """Return whether the live sync server is currently running."""
    running = bool(server_should_run and server_instance is not None)
    if running:
        ensure_server_timer_running()
    return running


def is_server_running() -> bool:
    """Backward-compatible alias for get_server_status."""
    return get_server_status()


class SafeTCPServer(http.server.ThreadingHTTPServer):
    """Threaded HTTP server that owns its port exclusively.

    On Windows, SO_REUSEADDR lets a second process bind the same port and
    steal connections (two Blenders both "listening" on 31337), so the
    socket uses SO_EXCLUSIVEADDRUSE there instead.
    """

    daemon_threads = True
    allow_reuse_address = os.name != "nt"
    request_queue_size = 16

    def __init__(self, server_address, RequestHandlerClass, family=socket.AF_INET):
        self.address_family = family
        super().__init__(server_address, RequestHandlerClass)

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        if self.address_family == socket.AF_INET6 and hasattr(socket, "IPV6_V6ONLY"):
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        super().server_bind()


def _blend_file_label() -> str:
    try:
        return os.path.basename(bpy.data.filepath) or "(unsaved file)"
    except Exception:
        return ""


def _server_tick() -> Optional[float]:
    """Timer callback: run queued requests and publish live-sync updates."""
    global _timer_registered, _last_websocket_publish

    if not server_should_run or server_instance is None:
        _timer_registered = False
        return None

    dispatch.note_tick(_blend_file_label())
    try:
        dispatch.drain()
    except Exception as exc:
        print(f"Blender Addon: Error running a sync request: {exc}")
        traceback.print_exc()

    try:
        _process_websocket_sync_requests()
    except Exception:
        traceback.print_exc()

    if websocket_server is not None:
        now = time.monotonic()
        for armature in websocket_server.drain_new_subscriptions():
            _dirty_armatures[armature] = now - 0.025
        ready = [name for name, dirty_at in _dirty_armatures.items() if now - dirty_at >= 0.025]
        for armature in ready:
            _dirty_armatures.pop(armature, None)
            websocket_server.publish_updates(
                lambda name, expected=armature: get_armature_timeline_hash(name) if name == expected else ""
            )
        # Slow fallback covers initial subscriptions, timeline settings, and
        # Blender builds that omit an Action dependency update.
        if now - _last_websocket_publish >= 2.0:
            _last_websocket_publish = now
            websocket_server.publish_updates(get_armature_timeline_hash)

    dispatch.note_tick(_blend_file_label())
    # Re-run quickly so we remain responsive without blocking Blender
    return 0.01


def ensure_server_timer_running() -> bool:
    """Ensure the request-loop timer is registered for an active server."""
    global _timer_registered

    if not server_should_run or server_instance is None:
        return False

    try:
        if bpy.app.timers.is_registered(_server_tick):
            _timer_registered = True
            return True
    except Exception:
        if _timer_registered:
            return True

    try:
        bpy.app.timers.register(_server_tick, first_interval=0.0, persistent=True)
        _timer_registered = True
        return True
    except Exception as exc:
        _timer_registered = False
        print(f"Blender Addon: Failed to register server request timer: {exc}")
        traceback.print_exc()
        return False


def _unregister_server_timer() -> None:
    global _timer_registered

    if not _timer_registered:
        return

    try:
        if bpy.app.timers.is_registered(_server_tick):
            bpy.app.timers.unregister(_server_tick)
    except ValueError:
        pass
    except Exception:
        pass
    finally:
        _timer_registered = False


def handle_blend_file_loaded(_dummy=None) -> None:
    """Restore the server request timer after Blender opens another file."""
    if server_should_run and server_instance is not None:
        _register_live_sync_handler()
        ensure_server_timer_running()


def validate_port(port) -> str:
    """Return an error message for an unusable port, or '' when it's fine."""
    try:
        port = int(port)
    except (TypeError, ValueError):
        return "The port must be a number."
    if port < MIN_PORT or port > MAX_PORT:
        return (
            f"Port {port} can't be used: pick {MIN_PORT}-{MAX_PORT}. "
            "Live sync also uses the next port up."
        )
    return ""


def _describe_bind_error(port: int, exc: BaseException) -> str:
    errno = getattr(exc, "errno", None)
    winerror = getattr(exc, "winerror", None)
    if errno in (98, 48, 10048) or winerror in (10048, 10013):
        try:
            from ..core import auth  # noqa: PLC0415

            if auth.is_login_in_progress():
                return (
                    f"Port {port} is held by the Roblox login that's waiting in your "
                    "browser. Finish or cancel the login, then start the server."
                )
        except Exception:
            pass
        return (
            f"Port {port} is already in use, probably by another Blender or app. "
            "Close it or pick a different port (and use the same port in Studio)."
        )
    return f"Could not open port {port}: {exc}"


def _start_thread(server: SafeTCPServer, name: str) -> None:
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.25},
        name=name,
        daemon=True,
    )
    server._rbx_serving = True
    thread.start()
    _server_threads.append(thread)


def start_server(port: int = 31337) -> bool:
    """Start the live sync servers. On failure ``last_start_error`` says why."""
    global server_instance, server_should_run, server_port, websocket_server
    global _last_websocket_publish, last_start_error

    # Check the port before touching a running server: a typo must not cut
    # off a working connection.
    last_start_error = validate_port(port)
    if last_start_error:
        print(f"Blender Addon: {last_start_error}")
        return False
    port = int(port)

    if server_instance is not None:
        stop_server()

    try:
        _dirty_armatures.clear()
        dispatch.clear()
        server_instance = SafeTCPServer(("127.0.0.1", port), AnimationHandler)
        # Studio connects to "localhost", which may resolve to ::1 first.
        if socket.has_ipv6:
            try:
                _extra_servers.append(
                    SafeTCPServer(("::1", port), AnimationHandler, family=socket.AF_INET6)
                )
            except OSError:
                pass
        websocket_server = LiveSyncWebSocketServer(port + 1)
        websocket_server.start()
        _register_live_sync_handler()
        _last_websocket_publish = 0.0
        server_should_run = True
        server_port = port
        dispatch.note_tick(_blend_file_label())

        _start_thread(server_instance, "rbx-sync-http")
        for index, extra in enumerate(_extra_servers):
            _start_thread(extra, f"rbx-sync-http-v6-{index}")

        ensure_server_timer_running()

        print(f"Blender Addon: Server started and listening on port {port}")
        return True
    except Exception as exc:
        failing_port = port + 1 if server_instance is not None else port
        last_start_error = _describe_bind_error(failing_port, exc)
        print(f"Blender Addon: Failed to start server: {last_start_error}")
        traceback.print_exc()
        _shutdown_sockets()
        server_should_run = False
        server_port = None
        _unregister_live_sync_handler()
        _unregister_server_timer()
        return False


def _shutdown_sockets() -> None:
    global server_instance, websocket_server

    servers = ([server_instance] if server_instance is not None else []) + list(_extra_servers)
    for server in servers:
        try:
            # shutdown() waits for serve_forever to exit, so only call it on
            # servers whose thread was actually started.
            if getattr(server, "_rbx_serving", False):
                server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception as exc:
            print(f"Blender Addon: Error closing server socket: {exc}")
    for thread in _server_threads:
        thread.join(timeout=1.0)
    _server_threads.clear()
    _extra_servers.clear()
    server_instance = None
    if websocket_server is not None:
        websocket_server.stop()
        websocket_server = None


def stop_server() -> None:
    """Stop the live sync server and clean up resources."""
    global server_should_run, server_port

    was_running = server_instance is not None
    if was_running:
        print("Blender Addon: Stop server called.")

    server_should_run = False
    _dirty_armatures.clear()
    _unregister_live_sync_handler()
    dispatch.clear()
    _shutdown_sockets()
    server_port = None
    _unregister_server_timer()

    if was_running:
        print("Blender Addon: Server shutdown complete.")
