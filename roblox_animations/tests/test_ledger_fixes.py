"""Regression tests for the user-flow ledger fixes (USER_FLOW_FAILURES.md).

Each test names the ledger entry it covers. Run inside Blender:
blender --background --factory-startup --online-mode --python <runner> -- test_ledger_fixes
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import math
import os
import socket
import struct
import threading
import time
import unittest
import zlib
from unittest import mock

import bpy
from mathutils import Euler, Matrix, Vector

from . import test_rbxm as binary
from .test_rbxm_rebuild import clean_scene, import_binary
from .test_studio_template_roundtrip import templates
from ..animation import serialization
from ..core import auth, utils
from ..core.utils import mat_to_cf
from ..rig import constraints as rig_constraints
from ..rig import display_offsets
from ..server import dispatch, handler, live_sync_delta, requests, security
from ..server import server as server_module

# Real classic R6 part sizes (the shared template fixture uses 1x1x1 parts).
R6_SIZES = {
    "HumanoidRootPart": (2, 2, 1),
    "Torso": (2, 2, 1),
    "Head": (2, 1, 1),
    "Left Arm": (1, 2, 1),
    "Right Arm": (1, 2, 1),
    "Left Leg": (1, 2, 1),
    "Right Leg": (1, 2, 1),
}


def r6_binary(sizes=R6_SIZES):
    """The Studio R6 template as an .rbxm, with real part sizes."""
    fixture = templates()["TemplateR6"]
    names = list(fixture["parts"])
    refs = {name: index + 1 for index, name in enumerate(names)}
    joints = fixture["joints"]
    joint_refs = list(range(1000, 1000 + len(joints)))
    chunks = [
        binary._inst_chunk(1, "Model", [0]),
        binary._inst_chunk(2, "Part", list(refs.values())),
        binary._inst_chunk(3, "Motor6D", joint_refs),
        binary._prop_chunk(1, "Name", 1, binary._enc_string("TemplateR6")),
        binary._prop_chunk(2, "Name", 1, b"".join(binary._enc_string(n) for n in names)),
        binary._prop_chunk(
            2, "CFrame", 0x10, binary._enc_cframes([fixture["parts"][n] for n in names])
        ),
        binary._prop_chunk(
            2, "Size", 0x0E, binary._enc_vector3s([sizes.get(n, (1, 1, 1)) for n in names])
        ),
        binary._prop_chunk(
            3, "Name", 1, b"".join(binary._enc_string(j["part1"]) for j in joints)
        ),
    ]
    for key in ("Part0", "Part1"):
        chunks.append(binary._prop_chunk(
            3, key, 0x13, binary._enc_referents([refs[j[key.lower()]] for j in joints])
        ))
    for key in ("C0", "C1"):
        chunks.append(binary._prop_chunk(
            3, key, 0x10, binary._enc_cframes([j[key.lower()] for j in joints])
        ))
    chunks.append(binary._prnt_chunk(
        [0] + list(refs.values()) + joint_refs,
        [-1] + [0] * len(names) + [refs[j["part0"]] for j in joints],
    ))
    return binary._rbxm(chunks, 3, 1 + len(names) + len(joints))


def r6_rig():
    armature, meta = import_binary("TemplateR6", r6_binary())
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, 10
    scene.render.fps, scene.render.fps_base = 30, 1.0
    return armature, meta


def key_rotation(armature, bone_name, frame, euler):
    pose_bone = armature.pose.bones[bone_name]
    pose_bone.rotation_mode = "XYZ"
    pose_bone.rotation_euler = euler
    pose_bone.keyframe_insert("rotation_euler", frame=frame)


def export(armature, **kwargs):
    task = dispatch.new_task_id("test")
    requests.execute_in_main_thread(task, armature.name, **kwargs)
    success, payload = requests.pending_responses.pop(task)
    if not success:
        raise AssertionError(payload)
    return json.loads(zlib.decompress(payload))


def channel_names(animation):
    return {name for keyframe in animation["kfs"] for name in keyframe.get("kf", {})}


def free_port_pair():
    for _ in range(50):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        if port + 1 > 65534:
            continue
        try:
            with socket.socket() as second:
                second.bind(("127.0.0.1", port + 1))
            return port
        except OSError:
            continue
    raise RuntimeError("no free port pair")


# ---------------------------------------------------------------------------
# X1-X3: request validation
# ---------------------------------------------------------------------------


class TestRequestSecurity(unittest.TestCase):
    def test_hosts(self):
        for host in ("localhost:31337", "127.0.0.1:31337", "[::1]:31337", "LOCALHOST", None, ""):
            self.assertTrue(security.host_allowed(host), host)
        for host in ("evil.example:31337", "127.0.0.1.nip.io:31337", "192.168.0.2:31337"):
            self.assertFalse(security.host_allowed(host), host)

    def test_origins(self):
        for origin in (None, "", "https://www.roblox.com", "https://create.roblox.com"):
            self.assertTrue(security.origin_allowed(origin), origin)
        for origin in ("https://evil.example", "null", "http://localhost:3000",
                       "https://roblox.com.evil.example", "file://", "chrome-extension://x"):
            self.assertFalse(security.origin_allowed(origin), origin)

    def test_body_and_decompression_caps(self):
        import io

        with self.assertRaises(security.RequestRejected) as raised:
            security.read_request_body({"Content-Length": "100"}, io.BytesIO(b"x" * 100), limit=10)
        self.assertEqual(raised.exception.status, 413)
        with self.assertRaises(security.RequestRejected):
            security.read_request_body({"Content-Length": "-1"}, io.BytesIO(b""))
        bomb = zlib.compress(b"\0" * 5000)
        with self.assertRaises(security.RequestRejected):
            security.decompress_body(bomb, limit=1000)
        self.assertEqual(security.decompress_body(zlib.compress(b'{"a":1}')), b'{"a":1}')
        self.assertEqual(security.decompress_body(b'{"kfs":[]}'), b'{"kfs":[]}')

    def test_content_types(self):
        self.assertTrue(security.is_json_or_binary_content_type("application/json; charset=utf-8"))
        self.assertTrue(security.is_json_or_binary_content_type("application/octet-stream"))
        for value in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", None):
            self.assertFalse(security.is_json_or_binary_content_type(value))


# ---------------------------------------------------------------------------
# X1, X5, X6, C2, C7: the HTTP server end to end
# ---------------------------------------------------------------------------


class TestSyncServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        clean_scene()
        bpy.ops.object.add(type="ARMATURE")
        cls.rig = bpy.context.object
        cls.rig.name = "Rig+1 Test"
        cls.port = free_port_pair()
        assert server_module.start_server(cls.port), server_module.last_start_error

    @classmethod
    def tearDownClass(cls):
        server_module.stop_server()

    def request(self, method, path, body=None, headers=None, tick=True):
        def call():
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
            try:
                connection.request(method, path, body=body, headers=headers or {})
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()

        result = {}

        def run():
            try:
                result["value"] = call()
            except Exception as exc:  # surfaced below
                result["error"] = exc

        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic() + 30
        while thread.is_alive() and time.monotonic() < deadline:
            if tick:
                server_module._server_tick()
            time.sleep(0.005)
        thread.join(5)
        if "error" in result:
            raise result["error"]
        return result["value"]

    def test_ping_without_main_thread(self):
        status, headers, body = self.request("GET", "/ping", tick=False)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["protocol"], handler.PROTOCOL_VERSION)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_browser_origins_and_foreign_hosts_are_rejected(self):
        status, headers, _ = self.request("GET", "/list_armatures", headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, _, _ = self.request("GET", "/list_armatures", headers={"Host": "evil.example:80"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("OPTIONS", "/import_animation")
        self.assertEqual(status, 403)

    def test_import_requires_json_content_type(self):
        status, _, _ = self.request(
            "POST", "/import_animation?armature=x", body=b'{"kfs":[]}',
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(status, 415)

    def test_import_without_content_length_is_a_clean_error(self):
        status, _, body = self.request(
            "POST", "/import_animation?armature=x", body=None,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertIn("JSON", json.loads(body)["message"])

    def test_plus_in_armature_name_survives(self):
        status, _, body = self.request("GET", "/animation_status?armature=Rig%2B1%20Test&last_known_hash=")
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["has_update"])

    def test_busy_blender_answers_503(self):
        with mock.patch.object(handler, "_QUICK_START_TIMEOUT", 0.3):
            started = time.monotonic()
            status, headers, body = self.request("GET", "/list_armatures", tick=False)
        self.assertEqual(status, 503)
        self.assertIn("busy", json.loads(body)["message"])
        self.assertEqual(headers.get("Retry-After"), "1")
        self.assertLess(time.monotonic() - started, 5)
        # The abandoned job must never run later.
        self.assertEqual(dispatch.drain(), 0)

    def test_second_server_cannot_share_the_port(self):
        # C3: a second Blender must fail to start, not silently share the port.
        with self.assertRaises(OSError):
            extra = server_module.SafeTCPServer(("127.0.0.1", self.port), handler.AnimationHandler)
            extra.server_close()
        from ..server.websocket import LiveSyncWebSocketServer

        with self.assertRaises(OSError):
            LiveSyncWebSocketServer(self.port + 1).start()

    def test_localhost_over_ipv6(self):
        # C9: Studio connects to "localhost", which can resolve to ::1.
        if not server_module._extra_servers:
            self.skipTest("IPv6 loopback is not available on this machine")

        def call():
            connection = http.client.HTTPConnection("::1", self.port, timeout=10)
            try:
                connection.request("GET", "/ping")
                return connection.getresponse().status
            finally:
                connection.close()

        self.assertEqual(call(), 200)

    def test_port_validation(self):
        self.assertIn("65534", server_module.validate_port(65535))
        self.assertEqual(server_module.validate_port(31337), "")
        # An invalid port is refused without stopping the running server.
        self.assertFalse(server_module.start_server(65535))
        self.assertIn("65534", server_module.last_start_error)
        self.assertTrue(server_module.get_server_status())
        self.assertEqual(server_module.server_port, self.port)


# ---------------------------------------------------------------------------
# X2, X4: WebSocket handshake and control frames
# ---------------------------------------------------------------------------


def _ws_frame(payload: bytes, opcode=0x1, fin=True):
    mask = os.urandom(4)
    head = bytes(((0x80 if fin else 0) | opcode,))
    length = len(payload)
    if length < 126:
        head += bytes((0x80 | length,))
    else:
        head += bytes((0x80 | 126,)) + length.to_bytes(2, "big")
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return head + mask + masked


class TestWebSocket(unittest.TestCase):
    def setUp(self):
        from ..server.websocket import LiveSyncWebSocketServer

        self.port = free_port_pair()
        self.server = LiveSyncWebSocketServer(self.port)
        self.server.start()

    def tearDown(self):
        self.server.stop()

    def connect(self, origin=None):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            "GET /live_sync HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
        )
        if origin:
            request += f"Origin: {origin}\r\n"
        # Split the handshake across packets: it used to need one recv.
        data = (request + "\r\n").encode()
        sock.sendall(data[:20])
        time.sleep(0.05)
        sock.sendall(data[20:])
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
        return sock, response

    def read_frame(self, sock):
        header = sock.recv(2)
        length = header[1] & 0x7F
        if length == 126:
            length = int.from_bytes(sock.recv(2), "big")
        payload = b""
        while len(payload) < length:
            payload += sock.recv(length - len(payload))
        return header[0] & 0x0F, payload

    def test_web_origin_rejected(self):
        sock, response = self.connect(origin="https://evil.example")
        sock.close()
        self.assertIn(b"403", response.split(b"\r\n", 1)[0])

    def test_ping_fragments_and_close(self):
        sock, response = self.connect()
        self.assertIn(b"101", response.split(b"\r\n", 1)[0])
        sock.sendall(_ws_frame(b"hi", opcode=0x9))
        opcode, payload = self.read_frame(sock)
        self.assertEqual((opcode, payload), (0xA, b"hi"))
        hello = json.dumps({"type": "hello", "armature": "Rig"}).encode()
        sock.sendall(_ws_frame(hello[:5], opcode=0x1, fin=False) + _ws_frame(hello[5:], opcode=0x0))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and "Rig" not in self.server.subscribed_armatures():
            time.sleep(0.02)
        self.assertIn("Rig", self.server.subscribed_armatures())
        sock.sendall(_ws_frame(struct.pack(">H", 1000), opcode=0x8))
        opcode, payload = self.read_frame(sock)
        self.assertEqual(opcode, 0x8)
        sock.close()

    def test_sync_request_for_another_armature_follows_it(self):
        sock, _ = self.connect()
        sock.sendall(_ws_frame(json.dumps({"type": "hello", "armature": "A"}).encode()))
        sock.sendall(_ws_frame(json.dumps({
            "type": "sync_request", "armature": "B", "request_id": "1",
        }).encode()))
        deadline = time.monotonic() + 3
        requests_seen = []
        while time.monotonic() < deadline and not requests_seen:
            requests_seen = self.server.drain_sync_requests()
            time.sleep(0.02)
        sock.close()
        self.assertEqual(len(requests_seen), 1)
        self.assertEqual(requests_seen[0][0].armature, "B")


# ---------------------------------------------------------------------------
# A1, A4-A9, B5, X9, X11, I12: export
# ---------------------------------------------------------------------------


class TestExport(unittest.TestCase):
    def setUp(self):
        self.armature, self.meta = r6_rig()
        key_rotation(self.armature, "Right Arm", 1, (0, 0, 0))
        key_rotation(self.armature, "Right Arm", 10, (math.radians(60), 0, 0))
        bpy.ops.object.mode_set(mode="OBJECT")

    def test_export_leaves_fps_mode_and_selection_alone(self):
        scene = bpy.context.scene
        scene.render.fps, scene.render.fps_base = 30, 1.001
        bpy.ops.mesh.primitive_cube_add()
        cube = bpy.context.object
        self.assertEqual(bpy.context.view_layer.objects.active, cube)
        export(self.armature)
        self.assertEqual(scene.render.fps, 30)
        self.assertAlmostEqual(scene.render.fps_base, 1.001, places=6)
        self.assertEqual(bpy.context.view_layer.objects.active, cube)
        self.assertEqual(self.armature.mode, "OBJECT")

    def test_edit_mode_refuses_with_reason(self):
        bpy.context.view_layer.objects.active = self.armature
        bpy.ops.object.mode_set(mode="EDIT")
        try:
            task = dispatch.new_task_id("t")
            requests.execute_in_main_thread(task, self.armature.name)
            success, message = requests.pending_responses.pop(task)
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")
        self.assertFalse(success)
        self.assertIn("Edit Mode", message)

    def test_renamed_and_truncated_bones_export_roblox_names(self):
        bone = self.armature.data.bones["Right Arm"]
        bone.name = "MyArm"
        long_name = "Right Arm " + "x" * 80
        bone["rbx_source_name"] = long_name
        animation = export(self.armature)
        names = channel_names(animation)
        self.assertIn(long_name, names)
        self.assertNotIn("MyArm", names)

    def test_quoted_bone_name_keeps_its_keys(self):
        bone = self.armature.data.bones["Right Arm"]
        bone.name = 'Right "Arm" \\ x'
        animation = export(self.armature)
        times = sorted(kf["t"] for kf in animation["kfs"] if "Right Arm" in kf["kf"])
        self.assertTrue(times, "the quoted bone's channel was dropped")
        self.assertAlmostEqual(times[-1], 9 / 30, places=5)

    def test_warnings_for_scale_object_animation_and_range(self):
        pose_bone = self.armature.pose.bones["Left Arm"]
        pose_bone.scale = (1.5, 1.5, 1.5)
        pose_bone.keyframe_insert("scale", frame=5)
        self.armature.location = (0, 0, 1)
        self.armature.keyframe_insert("location", frame=5)
        key_rotation(self.armature, "Right Arm", 30, (0, 0, 0))
        warnings = " ".join(export(self.armature)["export_info"]["warnings"])
        self.assertIn("scale", warnings)
        self.assertIn("Left Arm", warnings)
        self.assertIn("armature object itself is animated", warnings)
        self.assertIn("scene range", warnings)

    def test_unparented_bone_is_sampled_every_frame(self):
        # Torso swings between its keys; the unparented arm keeps its own keys
        # sparse. The arm's Roblox pose (relative to Torso) changes every frame.
        key_rotation(self.armature, "Torso", 1, (0, 0, 0))
        key_rotation(self.armature, "Torso", 10, (0, 0, math.radians(90)))
        bpy.context.view_layer.objects.active = self.armature
        bpy.ops.object.mode_set(mode="EDIT")
        self.armature.data.edit_bones["Right Arm"].parent = None
        bpy.ops.object.mode_set(mode="OBJECT")
        data = self.armature.data.bones["Right Arm"]
        data["worldspace_bone"] = True
        data["worldspace_original_parent"] = "Torso"
        animation = export(self.armature)
        frames = {round(kf["t"] * 30) for kf in animation["kfs"] if "Right Arm" in kf["kf"]}
        self.assertTrue(set(range(0, 10)).issubset(frames), sorted(frames))

    def test_nan_is_sanitized_with_a_warning(self):
        clean, replaced = live_sync_delta.sanitize_non_finite({"a": [1.0, float("nan")], "b": float("inf")})
        self.assertEqual((clean, replaced), ({"a": [1.0, 0.0], "b": 0.0}, 2))

    def test_live_sync_snapshot_cache_is_bounded(self):
        requests.live_sync_snapshots.clear()
        for index in range(requests._LIVE_SYNC_SNAPSHOT_LIMIT + 5):
            requests._remember_snapshot(("rig", str(index)), {"kfs": []}, str(index))
        self.assertEqual(len(requests.live_sync_snapshots), requests._LIVE_SYNC_SNAPSHOT_LIMIT)

    def test_delta_uses_cached_revision(self):
        task = dispatch.new_task_id("d")
        requests.execute_in_main_thread(task, self.armature.name, None, False, "")
        first = json.loads(zlib.decompress(requests.pending_responses.pop(task)[1]))
        key_rotation(self.armature, "Right Arm", 10, (math.radians(30), 0, 0))
        task = dispatch.new_task_id("d")
        requests.execute_in_main_thread(task, self.armature.name, None, False, first["hash"])
        second = json.loads(zlib.decompress(requests.pending_responses.pop(task)[1]))
        self.assertIn(second["type"], {"animation_delta", "animation_full"})
        if second["type"] == "animation_delta":
            self.assertEqual(second["base_hash"], first["hash"])


# ---------------------------------------------------------------------------
# R1, R2, A2, A7, A8: Studio -> Blender import
# ---------------------------------------------------------------------------


class TestImport(unittest.TestCase):
    def setUp(self):
        self.armature, _ = r6_rig()
        key_rotation(self.armature, "Left Arm", 1, (0.3, 0, 0))
        self.previous_action = self.armature.animation_data.action
        driver = self.armature.pose.bones["Left Leg"].driver_add("location", 0)
        driver.driver.expression = "0.0"
        bpy.ops.object.mode_set(mode="OBJECT")

    def import_animation(self, data):
        task = dispatch.new_task_id("import")
        requests.execute_import_animation(task, data, self.armature.name)
        success, message = requests.pending_responses.pop(task)
        self.assertTrue(success, message)
        return message

    def payload(self):
        identity = mat_to_cf(Matrix.Identity(4))
        turned = mat_to_cf(Euler((0.5, 0, 0)).to_matrix().to_4x4())
        return {
            "t": 1.0,
            "kfs": [
                {"t": 0, "kf": {"Right Arm": [identity, "Linear", "Out"], "NoSuchPart": identity},
                 "fc": {"JawDrop": {"value": 0.0, "easingStyle": "Linear", "easingDirection": "Out"}}},
                {"t": 1.0, "kf": {"Right Arm": turned},
                 "fc": {"JawDrop": {"value": 1.0, "easingStyle": "Linear", "easingDirection": "Out"}}},
            ],
            "markers": [
                {"name": "Hit", "time": 0.5, "type": "Event", "value": "sword"},
                {"name": "Start", "time": 0.0, "type": "Name"},
            ],
        }

    def test_import_keeps_old_action_drivers_fps_and_editor_state(self):
        scene = bpy.context.scene
        scene.render.fps, scene.render.fps_base = 30, 1.001
        bpy.ops.mesh.primitive_cube_add()
        cube = bpy.context.object
        message = self.import_animation(self.payload())
        animation_data = self.armature.animation_data
        self.assertIsNot(animation_data.action, self.previous_action)
        self.assertTrue(self.previous_action.use_fake_user)
        self.assertIn(self.previous_action.name, message)
        self.assertEqual(len(animation_data.drivers), 1)
        self.assertEqual(scene.render.fps, 30)
        self.assertAlmostEqual(scene.render.fps_base, 1.001, places=6)
        self.assertEqual(bpy.context.view_layer.objects.active, cube)
        self.assertEqual(self.armature.mode, "OBJECT")
        self.assertIn("NoSuchPart", message)

    def test_markers_are_imported(self):
        self.import_animation(self.payload())
        action = self.armature.animation_data.action
        names = sorted(marker.name for marker in action.pose_markers)
        self.assertEqual(names, ["Hit: sword", "Start"])

    def test_markers_round_trip_back_to_studio(self):
        self.import_animation(self.payload())
        markers = {marker["name"]: marker for marker in export(self.armature).get("markers", [])}
        self.assertEqual(markers["Hit"]["type"], "Event")
        self.assertEqual(markers["Hit"]["value"], "sword")
        self.assertAlmostEqual(markers["Hit"]["time"], 0.5, places=5)
        self.assertEqual(markers["Start"]["type"], "Name")

    def test_motor6d_bone_on_skinned_mesh_imports_as_joint(self):
        # A Motor6D bone that deforms a skinned mesh must still be converted
        # with the Motor6D math, even in a payload flagged as a deform rig.
        self.armature.data.bones["Right Arm"].use_deform = True
        payload = self.payload()
        payload["is_deform_bone_rig"] = True
        self.import_animation(payload)
        bpy.context.scene.frame_set(bpy.context.scene.frame_start + 30)
        from ..animation.sampling import serialize_animation_state

        state = serialize_animation_state(self.armature)
        expected = mat_to_cf(Euler((0.5, 0, 0)).to_matrix().to_4x4())
        self.assertLess(max(abs(a - b) for a, b in zip(state["Right Arm"], expected)), 1e-4)

    def test_source_name_resolver(self):
        bone = self.armature.data.bones["Right Arm"]
        bone.name = "Right Arm.001"
        resolve = requests.build_pose_bone_resolver(self.armature, False)
        self.assertEqual(resolve("Right Arm").name, "Right Arm.001")
        self.assertIsNone(resolve("Nope"))


# ---------------------------------------------------------------------------
# X7, X8, X10
# ---------------------------------------------------------------------------


class TestReadOnlyAndHashing(unittest.TestCase):
    def setUp(self):
        self.armature, _ = r6_rig()
        key_rotation(self.armature, "Right Arm", 1, (0, 0, 0))
        key_rotation(self.armature, "Right Arm", 10, (1, 0, 0))

    def test_bone_rest_is_read_only(self):
        bpy.ops.object.mode_set(mode="POSE")
        pose_bone = self.armature.pose.bones["Left Arm"]
        pose_bone.rotation_mode = "XYZ"
        pose_bone.rotation_euler = (0.4, 0, 0)
        utils.pose_bone_set_selected(pose_bone, True)
        utils.pose_bone_set_selected(self.armature.pose.bones["Head"], False)
        task = dispatch.new_task_id("rest")
        requests.execute_get_bone_rest(task, self.armature.name)
        success, data = requests.pending_responses.pop(task)
        self.assertTrue(success, data)
        self.assertAlmostEqual(pose_bone.rotation_euler.x, 0.4, places=5)
        self.assertTrue(utils.pose_bone_selected(pose_bone))
        self.assertFalse(utils.pose_bone_selected(self.armature.pose.bones["Head"]))
        self.assertEqual(self.armature.mode, "POSE")
        self.assertIn("Right Arm", json.loads(data)["bone_poses"])
        bpy.ops.object.mode_set(mode="OBJECT")

    def test_hash_sees_nla_drivers_rate_and_rest(self):
        name = self.armature.name
        base = utils.get_armature_timeline_hash(name)
        scene = bpy.context.scene
        scene.render.fps_base = 1.001
        rate = utils.get_armature_timeline_hash(name)
        self.assertNotEqual(base, rate)
        driver = self.armature.pose.bones["Head"].driver_add("location", 0)
        driver.driver.expression = "0.1"
        driven = utils.get_armature_timeline_hash(name)
        self.assertNotEqual(rate, driven)
        track = self.armature.animation_data.nla_tracks.new()
        track.strips.new("strip", 1, self.armature.animation_data.action)
        nla = utils.get_armature_timeline_hash(name)
        self.assertNotEqual(driven, nla)

    def test_hash_cache_is_per_armature(self):
        utils._action_hash_cache.clear()
        first = utils.get_cached_armature_hash(self.armature.name)
        with mock.patch.object(utils, "get_armature_timeline_hash", return_value="other") as compute:
            self.assertEqual(utils.get_cached_armature_hash("SomeOtherRig"), "other")
            compute.assert_called_once()
        self.assertEqual(utils.get_cached_armature_hash(self.armature.name), first)


# ---------------------------------------------------------------------------
# B1, B2, I10, I11: building the rig
# ---------------------------------------------------------------------------


class TestRigBuild(unittest.TestCase):
    def setUp(self):
        self.armature, self.meta = r6_rig()

    def direction(self, name):
        bone = self.armature.data.bones[name]
        return (bone.tail_local - bone.head_local).normalized()

    def test_scale_is_locked(self):
        for pose_bone in self.armature.pose.bones:
            self.assertEqual(tuple(pose_bone.lock_scale), (True, True, True), pose_bone.name)

    def test_r6_limbs_point_along_the_limb(self):
        for name in ("Left Arm", "Right Arm", "Left Leg", "Right Leg"):
            self.assertLess(self.direction(name).z, -0.99, name)
        for name in ("Torso", "Head"):
            self.assertGreater(self.direction(name).z, 0.99, name)

    def test_rebuild_dialog_defaults_to_stored_style(self):
        self.assertEqual(self.armature.get("rbx_rigging_type"), "CONNECT")

    def test_reimported_rig_never_inherits_a_stale_session(self):
        # A rig deleted mid-edit and re-imported under the same name must not
        # be compared against the old object's rest.
        stale_key = self.armature.as_pointer() + 1
        display_offsets._edit_sessions[stale_key] = (
            self.armature.name, {"Left Arm": Matrix.Translation((5, 5, 5))}
        )
        before = [list(r) for r in self.armature.data.bones["Left Arm"]["nicetransform"]]
        display_offsets._edit_mode_exit_handler()
        self.assertNotIn(stale_key, display_offsets._edit_sessions)
        after = [list(r) for r in self.armature.data.bones["Left Arm"]["nicetransform"]]
        self.assertEqual(before, after)

    def test_unedited_bones_are_never_rebased(self):
        # A bone whose stored offset doesn't match its rest (older rigs) keeps
        # exporting exactly as before when nothing edits it.
        bone = self.armature.data.bones["Left Arm"]
        bone["nicetransform"] = [list(row) for row in Matrix.Rotation(0.3, 4, "X")]
        before = [list(r) for r in bone["nicetransform"]]
        bpy.context.view_layer.objects.active = self.armature
        bpy.ops.object.mode_set(mode="EDIT")
        display_offsets._edit_mode_exit_handler()
        bpy.ops.object.mode_set(mode="OBJECT")
        display_offsets._edit_mode_exit_handler()
        # Leaving Edit Mode rebuilds the bones; old Bone references are stale.
        bone = self.armature.data.bones["Left Arm"]
        after = [list(r) for r in bone["nicetransform"]]
        for row_before, row_after in zip(before, after):
            for value_before, value_after in zip(row_before, row_after):
                self.assertAlmostEqual(value_before, value_after, places=6)

    def test_edit_mode_changes_keep_export_and_parts(self):
        constrained = [
            obj for obj in bpy.data.objects
            if any(c.type == "CHILD_OF" and c.subtarget == "Right Arm" for c in obj.constraints)
        ]
        self.assertTrue(constrained)
        before = {obj.name: obj.matrix_world.copy() for obj in constrained}

        bpy.context.view_layer.objects.active = self.armature
        bpy.ops.object.mode_set(mode="EDIT")
        # The depsgraph handler snapshots the rest when Edit Mode starts and
        # refreshes the edited bones when it ends; drive it explicitly here.
        display_offsets._edit_mode_exit_handler()
        edit_bone = self.armature.data.edit_bones["Right Arm"]
        edit_bone.tail += Vector((0.3, 0.2, 0.0))
        edit_bone.roll += 0.7
        edit_bone.head += Vector((0.0, 0.0, 0.2))
        bpy.ops.object.mode_set(mode="OBJECT")
        display_offsets._edit_mode_exit_handler()
        self.assertNotIn(self.armature.as_pointer(), display_offsets._edit_sessions)
        bpy.context.view_layer.update()

        for obj in constrained:
            delta = max(
                abs(a - b) for row_a, row_b in zip(obj.matrix_world, before[obj.name])
                for a, b in zip(row_a, row_b)
            )
            self.assertLess(delta, 1e-4, obj.name)

        key_rotation(self.armature, "Left Arm", 1, (0, 0, 0))
        bpy.ops.object.mode_set(mode="OBJECT")
        animation = export(self.armature)
        identity = mat_to_cf(Matrix.Identity(4))
        for keyframe in animation["kfs"]:
            pose = keyframe["kf"].get("Right Arm")
            if pose is None:
                continue
            components = pose[0] if isinstance(pose[0], list) else pose
            self.assertLess(max(abs(a - b) for a, b in zip(components, identity)), 1e-4)


# ---------------------------------------------------------------------------
# I3, I4, I5, I6, I7: .rbxm import
# ---------------------------------------------------------------------------


def special_mesh_part(mesh_type, scale=(1.0, 1.0, 1.0), mesh_id=""):
    chunks = [
        binary._inst_chunk(1, "Model", [0]),
        binary._inst_chunk(2, "Part", [1]),
        binary._inst_chunk(3, "SpecialMesh", [2]),
        binary._prop_chunk(1, "Name", 1, binary._enc_string("Tool")),
        binary._prop_chunk(2, "Name", 1, binary._enc_string("Blade")),
        binary._prop_chunk(2, "Size", 0x0E, binary._enc_vector3s([(1.0, 4.0, 0.5)])),
        binary._prop_chunk(2, "CFrame", 0x10, binary._enc_cframes([mat_to_cf(Matrix.Identity(4))])),
        binary._prop_chunk(3, "MeshType", 0x12, binary._enc_enums([mesh_type])),
        binary._prop_chunk(3, "Scale", 0x0E, binary._enc_vector3s([scale])),
        binary._prop_chunk(3, "MeshId", 0x22, binary._enc_contents([mesh_id])),
        binary._prnt_chunk([0, 1, 2], [-1, 0, 1]),
    ]
    return binary._rbxm(chunks, 3, 3)


def head_with_decals(faces):
    refs = [0, 1] + [10 + index for index in range(len(faces))]
    chunks = [
        binary._inst_chunk(1, "Model", [0]),
        binary._inst_chunk(2, "Part", [1]),
        binary._inst_chunk(3, "Decal", refs[2:]),
        binary._prop_chunk(1, "Name", 1, binary._enc_string("Character")),
        binary._prop_chunk(2, "Name", 1, binary._enc_string("Head")),
        binary._prop_chunk(2, "Size", 0x0E, binary._enc_vector3s([(2.0, 1.0, 1.0)])),
        binary._prop_chunk(3, "Texture", 1, b"".join(
            binary._enc_string(f"rbxasset://textures/decal{face}.png") for face in faces
        )),
        binary._prop_chunk(3, "Face", 0x12, binary._enc_enums(list(faces))),
        binary._prnt_chunk(refs, [-1, 0] + [1] * len(faces)),
    ]
    return binary._rbxm(chunks, 3, len(refs))


class TestRbxmImport(unittest.TestCase):
    def test_special_mesh_primitives_get_geometry(self):
        entry = binary.rbxm_to_part_aux(special_mesh_part(6, scale=(2.0, 1.0, 1.0)))[0]
        self.assertEqual(entry["shape"], "block")
        self.assertEqual(entry["primitive_size"], [2.0, 4.0, 0.5])
        cylinder = binary.rbxm_to_part_aux(special_mesh_part(4))[0]
        self.assertEqual((cylinder["shape"], cylinder["primitive_axis"]), ("cylinder", "y"))
        from ..rig.primitive_shapes import _build_primitive_mesh_data

        data = _build_primitive_mesh_data(cylinder)
        ys = [position[1] for position in data["positions"]]
        self.assertAlmostEqual(max(ys) - min(ys), 4.0, places=4)

    def test_front_decal_is_the_face(self):
        entry = binary.rbxm_to_part_aux(head_with_decals([1, 5]))[0]
        self.assertEqual(entry["face_decal"]["texture"], "rbxasset://textures/decal5.png")
        self.assertEqual(entry["face_decal_index"], 1)

    def test_failed_import_leaves_no_scaffold(self):
        clean_scene()
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty_tool.rbxm"
            path.write_bytes(special_mesh_part(5, mesh_id=""))
            # bpy.ops raises when the operator reports an error and cancels.
            with self.assertRaises(RuntimeError) as raised:
                bpy.ops.object.rbxanims_import_rbxm(filepath=str(path))
        self.assertIn("empty SpecialMesh", str(raised.exception))
        self.assertFalse([o for o in bpy.data.objects if o.name.startswith("__")])
        self.assertFalse(list(bpy.data.collections))

    def test_unreachable_mesh_becomes_a_reported_placeholder(self):
        clean_scene()
        import tempfile
        from pathlib import Path

        chunks = [
            binary._inst_chunk(1, "Model", [0]),
            binary._inst_chunk(2, "MeshPart", [1]),
            binary._prop_chunk(1, "Name", 1, binary._enc_string("Sword")),
            binary._prop_chunk(2, "Name", 1, binary._enc_string("Blade")),
            binary._prop_chunk(2, "Size", 0x0E, binary._enc_vector3s([(1.0, 4.0, 0.5)])),
            binary._prop_chunk(2, "CFrame", 0x10, binary._enc_cframes([mat_to_cf(Matrix.Identity(4))])),
            binary._prop_chunk(2, "MeshId", 0x22, binary._enc_contents(["rbxassetid://123456"])),
            binary._prnt_chunk([0, 1], [-1, 0]),
        ]
        data = binary._rbxm(chunks, 2, 2)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(auth, "get_auth_headers", return_value={}):
            path = Path(directory) / "sword.rbxm"
            path.write_bytes(data)
            result = bpy.ops.object.rbxanims_import_rbxm(filepath=str(path))
        self.assertEqual(result, {"FINISHED"})
        placeholders = [o for o in bpy.data.objects if o.get("RBXMissingMesh")]
        self.assertEqual(len(placeholders), 1)
        self.assertIn("login", placeholders[0]["RBXMissingMesh"])
        self.assertGreater(len(placeholders[0].data.vertices), 0)

    def test_several_objects_bind_to_one_bone(self):
        clean_scene()
        master = bpy.data.collections.new("Rig.model")
        bpy.context.scene.collection.children.link(master)
        parts = bpy.data.collections.new("Rig.model Parts")
        master.children.link(parts)
        armature_data = bpy.data.armatures.new("Arm")
        armature = bpy.data.objects.new("__Rig_Armature", armature_data)
        master.objects.link(armature)
        bpy.context.view_layer.objects.active = armature
        bpy.ops.object.mode_set(mode="EDIT")
        bone = armature_data.edit_bones.new("Head")
        bone.head, bone.tail = (0, 0, 1), (0, 0, 2)
        bpy.ops.object.mode_set(mode="OBJECT")
        objects = []
        for name, ref in (("Rig.model/Head.Part", 5), ("Head_Decal.000", 5), ("Rig.model/Head.Part.001", None)):
            mesh = bpy.data.meshes.new(name)
            obj = bpy.data.objects.new(name, mesh)
            obj.location = (0.5, 0.0, 1.5)
            if ref is not None:
                obj["RBXInstRef"] = ref
            parts.objects.link(obj)
            objects.append(obj)
        bpy.context.view_layer.update()
        before = [obj.matrix_world.copy() for obj in objects]
        rig_constraints.auto_constraint_parts(armature.name, inst_ref_to_bone={5: "Head"})
        bpy.context.view_layer.update()
        for obj, matrix in zip(objects, before):
            self.assertTrue(
                any(c.type == "CHILD_OF" and c.subtarget == "Head" for c in obj.constraints), obj.name
            )
            self.assertLess((obj.matrix_world.translation - matrix.translation).length, 1e-5, obj.name)


# ---------------------------------------------------------------------------
# I8, I9
# ---------------------------------------------------------------------------


class TestClothingAndLogin(unittest.TestCase):
    def test_clothing_failures_are_not_cached(self):
        from ..rig import clothing

        clothing._TEMPLATE_ID_CACHE.pop(424242, None)
        clothing.take_clothing_problems()
        with mock.patch.object(clothing, "_fetch_url_response", side_effect=OSError("offline")):
            self.assertIsNone(clothing.resolve_clothing_template_id(424242))
        self.assertNotIn(424242, clothing._TEMPLATE_ID_CACHE)
        self.assertTrue(any("424242" in p for p in clothing.take_clothing_problems()))

    def test_login_redirect_reaches_shared_sync_server(self):
        with mock.patch.object(server_module, "server_should_run", True), \
                mock.patch.object(server_module, "server_port", auth._PORT):
            auth._login_done.clear()
            auth._login_cancel.clear()
            auth._login_result.clear()
            ready = threading.Event()
            worker = threading.Thread(
                target=auth._login_worker,
                args=("client", "verifier", "state123", "http://localhost:31337/oauth2/callback", ready),
            )
            worker.start()
            self.assertTrue(ready.wait(5))
            status, text = auth.handle_shared_callback(
                "/oauth2/callback?state=state123&error=access_denied"
            )
            worker.join(5)
        self.assertEqual(status, 400)
        self.assertIn("access_denied", text)
        self.assertTrue(auth._login_done.is_set())
        self.assertIsNone(auth._shared_callback_context)


if __name__ == "__main__":
    unittest.main()
