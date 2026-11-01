"""Minimal localhost WebSocket notifier for Studio live sync.

This deliberately uses only the Python standard library. Socket I/O runs on a
background thread; Blender data is only inspected by ``publish_updates`` from
the addon timer on Blender's main thread.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import select
import socket
import threading
import time
from dataclasses import dataclass, field

from .security import host_allowed, origin_allowed

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_HANDSHAKE_BYTES = 16 * 1024
HANDSHAKE_TIMEOUT = 5.0
# Studio only sends small JSON control messages (rest calibration included).
MAX_INCOMING_MESSAGE_BYTES = 4 * 1024 * 1024
# A client that stops reading gets dropped instead of buffering forever.
MAX_OUTGOING_BUFFER_BYTES = 128 * 1024 * 1024

_OP_CONTINUATION = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


def _unmask(payload: bytes, mask: bytes) -> bytes:
    """XOR-unmask a frame payload in one big-integer operation."""
    length = len(payload)
    if not length:
        return b""
    repeated = (mask * (length // 4 + 1))[:length]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")).to_bytes(length, "big")


@dataclass
class _Client:
    socket: socket.socket
    buffer: bytearray = field(default_factory=bytearray)
    outgoing: bytearray = field(default_factory=bytearray)
    armature: str | None = None
    fragments: bytearray = field(default_factory=bytearray)
    fragment_opcode: int | None = None
    closing: bool = False


@dataclass
class _PendingHandshake:
    socket: socket.socket
    buffer: bytearray = field(default_factory=bytearray)
    deadline: float = 0.0


class LiveSyncWebSocketServer:
    """Pushes changed-armature hashes to Studio over localhost WebSockets."""

    def __init__(self, port: int):
        self.port = port
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._clients: list[_Client] = []
        self._pending: list[_PendingHandshake] = []
        self._clients_lock = threading.Lock()
        self._outbound: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
        self._sync_requests: queue.SimpleQueue[tuple[_Client, dict]] = queue.SimpleQueue()
        self._sync_responses: queue.SimpleQueue[tuple[_Client, str, str, bool, bytes | str]] = queue.SimpleQueue()
        self._new_subscriptions: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._last_hashes: dict[str, str] = {}
        self._hash_lock = threading.Lock()

    # ----------------------------------------------------------- lifecycle

    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # SO_REUSEADDR on Windows lets another process steal the port.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", self.port))
            listener.listen(8)
        except OSError:
            listener.close()
            raise
        listener.setblocking(False)
        self._listener = listener
        self._running.set()
        self._thread = threading.Thread(target=self._run, name="rbx-live-sync-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        with self._clients_lock:
            clients, self._clients = self._clients, []
            pending, self._pending = self._pending, []
        for client in clients:
            self._close_socket(client.socket)
        for handshake in pending:
            self._close_socket(handshake.socket)

    @staticmethod
    def _close_socket(sock: socket.socket) -> None:
        try:
            sock.close()
        except OSError:
            pass

    # ------------------------------------------------------ main-thread API

    def subscribed_armatures(self) -> set[str]:
        with self._clients_lock:
            return {client.armature for client in self._clients if client.armature}

    def publish_updates(self, hash_for_armature) -> None:
        """Run on Blender's main thread; queue only changed subscribed hashes."""
        for armature in self.subscribed_armatures():
            try:
                animation_hash = hash_for_armature(armature)
            except Exception:
                continue
            if not animation_hash:
                continue
            with self._hash_lock:
                if self._last_hashes.get(armature) == animation_hash:
                    continue
                self._last_hashes[armature] = animation_hash
            self._outbound.put((armature, animation_hash))

    def drain_sync_requests(self) -> list[tuple[_Client, dict]]:
        requests = []
        while True:
            try:
                requests.append(self._sync_requests.get_nowait())
            except queue.Empty:
                return requests

    def drain_new_subscriptions(self) -> list[str]:
        subscriptions = []
        while True:
            try:
                subscriptions.append(self._new_subscriptions.get_nowait())
            except queue.Empty:
                return subscriptions

    def publish_sync_response(
        self,
        client: _Client,
        request_id: str,
        trigger_hash: str,
        success: bool,
        payload: bytes | str,
    ) -> None:
        self._sync_responses.put((client, request_id, trigger_hash, success, payload))

    # ------------------------------------------------------------- framing

    @staticmethod
    def _frame(payload: str | bytes, opcode: int = _OP_TEXT) -> bytes:
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        length = len(data)
        head = 0x80 | opcode
        if length < 126:
            return bytes((head, length)) + data
        if length <= 65535:
            return bytes((head, 126)) + length.to_bytes(2, "big") + data
        return bytes((head, 127)) + length.to_bytes(8, "big") + data

    def _queue_bytes(self, client: _Client, data: bytes) -> None:
        with self._clients_lock:
            if client not in self._clients:
                return
            if len(client.outgoing) + len(data) > MAX_OUTGOING_BUFFER_BYTES:
                # The client stopped reading; drop it rather than grow forever.
                client.closing = True
                return
            client.outgoing.extend(data)

    def _queue_frame(self, client: _Client, payload: dict) -> None:
        self._queue_bytes(client, self._frame(json.dumps(payload, separators=(",", ":"))))

    def _remove(self, client: _Client) -> None:
        with self._clients_lock:
            if client in self._clients:
                self._clients.remove(client)
        self._close_socket(client.socket)

    def _handle_message(self, client: _Client, payload: bytes) -> None:
        try:
            message = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(message, dict):
            return
        armature = message.get("armature")
        if message.get("type") == "hello" and isinstance(armature, str) and armature:
            client.armature = armature
            # Force an initial notification after a new subscription or reconnect.
            with self._hash_lock:
                self._last_hashes.pop(armature, None)
            self._new_subscriptions.put(armature)
        elif (
            message.get("type") == "sync_request"
            and isinstance(armature, str)
            and isinstance(message.get("request_id"), str)
        ):
            if armature != client.armature:
                # Studio switched armatures without a new hello: follow it
                # instead of silently ignoring the request (which stalled sync).
                client.armature = armature
                self._new_subscriptions.put(armature)
            self._sync_requests.put((client, message))

    def _handle_frames(self, client: _Client) -> bool:
        """Parse buffered frames. Returns False when the client must be dropped."""
        while len(client.buffer) >= 2:
            first, second = client.buffer[0], client.buffer[1]
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            offset = 2
            if length == 126:
                if len(client.buffer) < 4:
                    return True
                length = int.from_bytes(client.buffer[2:4], "big")
                offset = 4
            elif length == 127:
                if len(client.buffer) < 10:
                    return True
                length = int.from_bytes(client.buffer[2:10], "big")
                offset = 10
            if length > MAX_INCOMING_MESSAGE_BYTES:
                return False
            if not masked:
                # RFC 6455: client frames must be masked.
                return False
            if len(client.buffer) < offset + 4 + length:
                return True
            mask = bytes(client.buffer[offset:offset + 4])
            payload = _unmask(bytes(client.buffer[offset + 4:offset + 4 + length]), mask)
            del client.buffer[:offset + 4 + length]

            if opcode == _OP_CLOSE:
                # Echo the close frame, then drop the connection.
                self._queue_bytes(client, self._frame(payload[:2], _OP_CLOSE))
                client.closing = True
                return True
            if opcode == _OP_PING:
                self._queue_bytes(client, self._frame(payload, _OP_PONG))
                continue
            if opcode == _OP_PONG:
                continue
            if opcode in (_OP_TEXT, _OP_BINARY):
                if fin:
                    if opcode == _OP_TEXT:
                        self._handle_message(client, payload)
                    continue
                client.fragment_opcode = opcode
                client.fragments = bytearray(payload)
                continue
            if opcode == _OP_CONTINUATION:
                if client.fragment_opcode is None:
                    return False
                client.fragments.extend(payload)
                if len(client.fragments) > MAX_INCOMING_MESSAGE_BYTES:
                    return False
                if fin:
                    data = bytes(client.fragments)
                    message_opcode = client.fragment_opcode
                    client.fragments = bytearray()
                    client.fragment_opcode = None
                    if message_opcode == _OP_TEXT:
                        self._handle_message(client, data)
                continue
            # Unknown opcode.
            return False
        return True

    # ----------------------------------------------------------- handshake

    def _accept(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while True:
            try:
                client_socket, _address = listener.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            client_socket.setblocking(False)
            with self._clients_lock:
                self._pending.append(
                    _PendingHandshake(client_socket, deadline=time.monotonic() + HANDSHAKE_TIMEOUT)
                )

    def _reject_handshake(self, handshake: _PendingHandshake, status: str) -> None:
        try:
            handshake.socket.sendall(
                f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode("ascii")
            )
        except OSError:
            pass
        self._close_socket(handshake.socket)

    def _progress_handshake(self, handshake: _PendingHandshake) -> bool:
        """Read handshake bytes. Returns True once the handshake is finished."""
        try:
            data = handshake.socket.recv(4096)
        except (BlockingIOError, InterruptedError):
            return False
        except OSError:
            self._close_socket(handshake.socket)
            return True
        if not data:
            self._close_socket(handshake.socket)
            return True
        handshake.buffer.extend(data)
        if len(handshake.buffer) > MAX_HANDSHAKE_BYTES:
            self._reject_handshake(handshake, "431 Request Header Fields Too Large")
            return True
        end = handshake.buffer.find(b"\r\n\r\n")
        if end < 0:
            return False

        request = bytes(handshake.buffer[:end]).decode("latin1")
        leftover = bytes(handshake.buffer[end + 4:])
        lines = request.split("\r\n")
        request_line = lines[0].split(" ")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        path = request_line[1] if len(request_line) >= 2 else ""
        key = headers.get("sec-websocket-key")
        if (
            len(request_line) < 3
            or request_line[0] != "GET"
            or path.split("?", 1)[0] != "/live_sync"
            or not key
            or "websocket" not in headers.get("upgrade", "").lower()
        ):
            self._reject_handshake(handshake, "400 Bad Request")
            return True
        if not host_allowed(headers.get("host")) or not origin_allowed(headers.get("origin")):
            print("Blender Addon: Rejected a live-sync connection from a web page.")
            self._reject_handshake(handshake, "403 Forbidden")
            return True

        accept = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("ascii")
        client = _Client(handshake.socket)
        client.outgoing.extend(response)
        client.buffer.extend(leftover)
        with self._clients_lock:
            self._clients.append(client)
        return True

    # ---------------------------------------------------------------- loop

    def _flush_outbound(self) -> None:
        try:
            while True:
                armature, animation_hash = self._outbound.get_nowait()
                frame = self._frame(json.dumps({
                    "type": "animation_changed",
                    "armature": armature,
                    "hash": animation_hash,
                }))
                with self._clients_lock:
                    clients = [client for client in self._clients if client.armature == armature]
                for client in clients:
                    self._queue_bytes(client, frame)
        except queue.Empty:
            pass
        try:
            while True:
                client, request_id, trigger_hash, success, payload = self._sync_responses.get_nowait()
                response = {
                    "type": "sync_payload" if success else "sync_error",
                    "request_id": request_id,
                    "trigger_hash": trigger_hash,
                }
                if success and isinstance(payload, bytes):
                    response["data"] = base64.b64encode(payload).decode("ascii")
                else:
                    response["error"] = str(payload)
                self._queue_frame(client, response)
        except queue.Empty:
            pass

    def _run(self) -> None:
        while self._running.is_set():
            listener = self._listener
            self._flush_outbound()
            with self._clients_lock:
                clients = list(self._clients)
                pending = list(self._pending)
            readable_sockets = [client.socket for client in clients] + [hs.socket for hs in pending]
            if listener is not None:
                readable_sockets.append(listener)
            writable_sockets = [client.socket for client in clients if client.outgoing]
            try:
                readable, writable, _ = select.select(readable_sockets, writable_sockets, [], 0.05)
            except (OSError, ValueError):
                # A socket was closed under us; prune and retry.
                self._prune_dead()
                continue
            readable_set = set(readable)
            writable_set = set(writable)

            if listener is not None and listener in readable_set:
                self._accept()

            now = time.monotonic()
            for handshake in pending:
                done = False
                if handshake.socket in readable_set:
                    done = self._progress_handshake(handshake)
                if not done and now >= handshake.deadline:
                    self._reject_handshake(handshake, "408 Request Timeout")
                    done = True
                if done:
                    with self._clients_lock:
                        if handshake in self._pending:
                            self._pending.remove(handshake)

            for client in clients:
                if client.socket in readable_set:
                    try:
                        data = client.socket.recv(65536)
                    except (BlockingIOError, InterruptedError):
                        data = None
                    except OSError:
                        self._remove(client)
                        continue
                    if data == b"":
                        self._remove(client)
                        continue
                    if data:
                        client.buffer.extend(data)
                        if not self._handle_frames(client):
                            self._remove(client)
                            continue
                if client.outgoing and (client.socket in writable_set or client.closing):
                    try:
                        sent = client.socket.send(client.outgoing)
                        if sent > 0:
                            with self._clients_lock:
                                del client.outgoing[:sent]
                    except (BlockingIOError, InterruptedError):
                        pass
                    except OSError:
                        self._remove(client)
                        continue
                if client.closing and not client.outgoing:
                    self._remove(client)
                elif client.closing and len(client.outgoing) > MAX_OUTGOING_BUFFER_BYTES:
                    self._remove(client)

    def _prune_dead(self) -> None:
        with self._clients_lock:
            clients = list(self._clients)
            pending = list(self._pending)
        for client in clients:
            if client.socket.fileno() < 0:
                self._remove(client)
        for handshake in pending:
            if handshake.socket.fileno() < 0:
                with self._clients_lock:
                    if handshake in self._pending:
                        self._pending.remove(handshake)
