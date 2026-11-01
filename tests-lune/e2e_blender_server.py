"""Blender side of the end-to-end sync test (see tests-lune/e2e.luau).

blender --background --factory-startup --online-mode --python tests-lune/e2e_blender_server.py -- <port> <stop_file>

Imports the Studio R6 template, keys an arm swing, starts the sync server and
services it until the stop file appears (timers don't run in background mode,
so this loop calls the server tick itself).
"""

import math
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import bpy  # noqa: E402

import roblox_animations  # noqa: E402

roblox_animations.register()

from roblox_animations.server import server  # noqa: E402
from roblox_animations.tests.test_ledger_fixes import key_rotation, r6_rig  # noqa: E402

args = sys.argv[sys.argv.index("--") + 1:]
port = int(args[0])
stop_file = args[1]

armature, _meta = r6_rig()
key_rotation(armature, "Right Arm", 1, (0, 0, 0))
key_rotation(armature, "Right Arm", 10, (math.radians(60), 0, 0))
if armature.mode != "OBJECT":
    bpy.ops.object.mode_set(mode="OBJECT")

if not server.start_server(port):
    print("START FAILED", server.last_start_error, flush=True)
    sys.exit(1)
print("READY", port, armature.name, flush=True)

deadline = time.monotonic() + 120
while time.monotonic() < deadline and not os.path.exists(stop_file):
    server._server_tick()
    time.sleep(0.005)

action = armature.animation_data.action if armature.animation_data else None
print("FINAL ACTION", action.name if action else None, flush=True)
print("MARKERS", sorted(m.name for m in action.pose_markers) if action else [], flush=True)
server.stop_server()
print("STOPPED", flush=True)
