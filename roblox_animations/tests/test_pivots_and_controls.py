"""Pivot/handle tools and the "Add controls" rig (USER_FLOW_FAILURES.md, B3).

Every export here is replayed the way Studio plays it (Part1 = Part0 * C0 *
pose * C1:Inverse() down the joint tree) and compared with the parts Blender
shows. Run inside Blender:
blender --background --factory-startup --online-mode --python <runner> -- test_pivots_and_controls
"""

from __future__ import annotations

import json
import math
import unittest

import bpy
from mathutils import Euler, Matrix, Quaternion, Vector

from .test_ledger_fixes import channel_names, export, key_rotation, r6_rig
from .test_rbxm_rebuild import import_binary, template_binary
from ..core import utils
from ..core.constants import get_transform_to_blender
from ..core.utils import cf_to_mat, mat_to_cf, to_matrix
from ..rig import controls, display_offsets, pivots
from ..server import dispatch, live_sync_delta, requests

T2B = get_transform_to_blender()
IDENTITY = mat_to_cf(Matrix.Identity(4))


def r15_rig():
    armature, meta = import_binary("TemplateR15", template_binary("TemplateR15"))
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, 10
    scene.render.fps, scene.render.fps_base = 30, 1.0
    return armature, meta


def tolerance(armature):
    """Rest data is float32: allow for its precision at the rig's coordinates."""
    reach = max(abs(value) for bone in armature.data.bones for value in bone.head_local)
    return 2e-6 * max(50.0, reach)


def max_difference(left, right):
    return max(abs(left[row][column] - right[row][column]) for row in range(4) for column in range(4))


def part_name(bone):
    return bone.get("rbx_source_name") or bone.name


def joint_bones(armature):
    return [bone for bone in armature.data.bones if display_offsets.is_joint_bone(bone)]


def rows(animation):
    """Each keyframe's poses, with earlier keys carried forward (as Studio holds them)."""
    fps = bpy.context.scene.render.fps
    result, current = {}, {}
    for keyframe in sorted(animation["kfs"], key=lambda item: item["t"]):
        current = dict(current)
        for name, pose in keyframe.get("kf", {}).items():
            current[name] = pose[0] if isinstance(pose[0], list) else pose
        result[round(keyframe["t"] * fps)] = current
    return result


def replay(armature, row, joints=None):
    """Studio playback: every part's world CFrame for one keyframe."""
    bones = joint_bones(armature)
    root = next(bone for bone in bones if bone.parent is None)
    world = {part_name(root): to_matrix(root["transform"])}
    pending = [bone for bone in bones if bone.parent is not None]
    while pending:
        ready = [bone for bone in pending if part_name(bone.parent) in world]
        assert ready, "disconnected joint tree"
        for bone in ready:
            c0, c1 = (joints or {}).get(
                part_name(bone), (to_matrix(bone["transform0"]), to_matrix(bone["transform1"]))
            )
            pose = row.get(part_name(bone))
            delta = cf_to_mat(pose) if pose else Matrix.Identity(4)
            world[part_name(bone)] = world[part_name(bone.parent)] @ c0 @ delta @ c1.inverted()
            pending.remove(bone)
    return world


def shown_parts(armature):
    """Every part's world CFrame as Blender shows it now."""
    evaluated = armature.evaluated_get(bpy.context.evaluated_depsgraph_get())
    parts = {}
    for pose_bone in evaluated.pose.bones:
        bone = pose_bone.bone
        if not display_offsets.is_joint_bone(bone) or bone.parent is None:
            continue
        parts[part_name(bone)] = (
            T2B.inverted() @ pose_bone.matrix
            @ to_matrix(bone["nicetransform"]).inverted()
            @ to_matrix(bone["transform1"]).inverted()
        )
    return parts


def frame_of(time):
    return bpy.context.scene.frame_start + time


def select_only(armature, names):
    bpy.context.view_layer.objects.active = armature
    if armature.mode != "POSE":
        bpy.ops.object.mode_set(mode="POSE")
    for pose_bone in armature.pose.bones:
        utils.pose_bone_set_selected(pose_bone, pose_bone.name in names)


def key_matrix(armature, name, frame, matrix):
    """Key a pose bone so it sits at an armature-space matrix on a frame."""
    scene = bpy.context.scene
    scene.frame_set(frame)
    bpy.context.view_layer.update()
    pose_bone = armature.pose.bones[name]
    pose_bone.matrix = matrix
    bpy.context.view_layer.update()
    pose_bone.keyframe_insert("location", frame=frame)
    pose_bone.keyframe_insert(
        "rotation_quaternion" if pose_bone.rotation_mode == "QUATERNION" else "rotation_euler",
        frame=frame,
    )


def rest(armature, name):
    return armature.data.bones[name].matrix_local.copy()


def turned(matrix, axis, angle, offset=(0.0, 0.0, 0.0)):
    """``matrix`` turned about its own origin, then moved by ``offset``."""
    origin = matrix.translation.copy()
    return (
        Matrix.Translation(origin + Vector(offset)) @ Matrix.Rotation(angle, 4, axis)
        @ Matrix.Translation(-origin) @ matrix
    )


class ExportChecks(unittest.TestCase):
    def assert_export_matches_blender(self, armature, animation=None, joints=None, convert=None):
        """Studio's playback of the export puts every part where Blender shows it."""
        animation = animation if animation is not None else export(armature)
        limit = tolerance(armature)
        scene = bpy.context.scene
        try:
            for time, row in rows(animation).items():
                if convert:
                    row = convert(row)
                scene.frame_set(frame_of(time))
                shown = shown_parts(armature)
                played = replay(armature, row, joints)
                for name, matrix in shown.items():
                    self.assertLess(max_difference(played[name], matrix), limit, (time, name))
        finally:
            scene.frame_set(scene.frame_start)
        return animation

    def assert_same_animation(self, first, second, limit):
        """Two exports play the same poses (a missing channel is the rest pose)."""
        first_rows, second_rows = rows(first), rows(second)
        self.assertEqual(sorted(first_rows), sorted(second_rows))
        for time in first_rows:
            names = set(first_rows[time]) | set(second_rows[time])
            for name in names:
                a = first_rows[time].get(name, IDENTITY)
                b = second_rows[time].get(name, IDENTITY)
                self.assertLess(max(abs(x - y) for x, y in zip(a, b)), limit, (time, name))


# ---------------------------------------------------------------------------
# Pivot / handle tools
# ---------------------------------------------------------------------------


class TestPivots(ExportChecks):
    def setUp(self):
        self.armature, _ = r6_rig()
        key_rotation(self.armature, "Head", 1, (0, 0, 0))
        key_rotation(self.armature, "Head", 10, (0.3, 0, 0))
        bpy.ops.object.mode_set(mode="OBJECT")
        self.parts = [
            obj for obj in bpy.data.objects
            if any(c.type == "CHILD_OF" and c.subtarget == "Right Arm" for c in obj.constraints)
        ]
        self.assertTrue(self.parts)

    def joint_data(self, name):
        bone = self.armature.data.bones[name]
        return {key: [list(row) for row in bone[key]] for key in ("transform", "transform0", "transform1")}

    def move_pivot_to(self, name, point):
        bpy.context.scene.cursor.location = self.armature.matrix_world @ point
        select_only(self.armature, {name})
        self.assertEqual(bpy.ops.object.rbxanims_pivot_to_cursor(), {"FINISHED"})
        bpy.ops.object.mode_set(mode="OBJECT")

    def test_pivot_to_cursor_keeps_joint_parts_and_export(self):
        before = export(self.armature)
        joint = self.joint_data("Right Arm")
        placed = {obj.name: obj.matrix_world.copy() for obj in self.parts}
        bone = self.armature.data.bones["Right Arm"]
        self.assertIsNone(pivots.pivot_offset(bone))
        point = bone.head_local + Vector((0.0, 0.0, -1.0))

        self.move_pivot_to("Right Arm", point)

        bone = self.armature.data.bones["Right Arm"]
        self.assertLess((bone.head_local - point).length, 1e-5)
        self.assertIsNotNone(pivots.pivot_offset(bone))
        self.assertEqual(self.joint_data("Right Arm"), joint)
        bpy.context.view_layer.update()
        for obj in self.parts:
            self.assertLess(max_difference(obj.matrix_world, placed[obj.name]), 1e-5, obj.name)
        self.assert_same_animation(before, export(self.armature), 1e-5)

    def test_custom_pivot_turns_about_the_new_point(self):
        point = self.armature.data.bones["Right Arm"].head_local + Vector((0.0, 0.0, -1.0))
        self.move_pivot_to("Right Arm", point)
        key_rotation(self.armature, "Right Arm", 1, (0, 0, 0))
        key_rotation(self.armature, "Right Arm", 10, (0.9, 0.2, 0.0))
        bpy.ops.object.mode_set(mode="OBJECT")

        animation = self.assert_export_matches_blender(self.armature)

        scene = bpy.context.scene
        scene.frame_set(10)
        evaluated = self.armature.evaluated_get(bpy.context.evaluated_depsgraph_get())
        self.assertLess((evaluated.pose.bones["Right Arm"].head - point).length, 1e-4)
        scene.frame_set(1)
        # Roblox gets the custom pivot as rotation plus an offset on the joint.
        last = rows(animation)[9]["Right Arm"]
        self.assertGreater(Vector(last[:3]).length, 0.1)

    def test_reset_pivot_returns_to_the_roblox_joint(self):
        before = export(self.armature)
        bone = self.armature.data.bones["Right Arm"]
        origin = pivots.joint_origin(bone)
        self.move_pivot_to("Right Arm", bone.head_local + Vector((0.3, 0.0, -0.8)))
        pivots.queue_studio_pivots(self.armature, ["Right Arm"])

        select_only(self.armature, {"Right Arm"})
        self.assertEqual(bpy.ops.object.rbxanims_reset_pivot(), {"FINISHED"})
        bpy.ops.object.mode_set(mode="OBJECT")

        bone = self.armature.data.bones["Right Arm"]
        self.assertLess((bone.head_local - origin).length, 1e-5)
        self.assertIsNone(pivots.pivot_offset(bone))
        self.assertNotIn(utils.PENDING_STUDIO_PIVOT_PROP, bone)
        self.assert_same_animation(before, export(self.armature), 1e-5)

    def test_bone_shape_changes_the_display_only(self):
        before = export(self.armature)
        rests = {bone.name: bone.matrix_local.copy() for bone in self.armature.data.bones}
        for shape in ("SPHERE", "RING", "ARROW"):
            select_only(self.armature, {"Right Arm", "Head"})
            self.assertEqual(bpy.ops.object.rbxanims_bone_shape(shape=shape, size=1.5), {"FINISHED"})
            pose_bone = self.armature.pose.bones["Right Arm"]
            self.assertEqual(pose_bone.custom_shape.name, "__RBXShape_" + shape)
            self.assertAlmostEqual(pose_bone.custom_shape_scale_xyz[0], 1.5, places=5)
        bpy.ops.object.mode_set(mode="OBJECT")
        for bone in self.armature.data.bones:
            self.assertLess(max_difference(bone.matrix_local, rests[bone.name]), 1e-6, bone.name)
        self.assert_same_animation(before, export(self.armature), 1e-6)

        select_only(self.armature, {"Right Arm"})
        bpy.ops.object.rbxanims_bone_shape(shape="NONE")
        self.assertIsNone(self.armature.pose.bones["Right Arm"].custom_shape)
        bpy.ops.object.mode_set(mode="OBJECT")

    def test_studio_pivot_is_queued_without_changing_the_poses(self):
        point = self.armature.data.bones["Right Arm"].head_local + Vector((0.0, 0.0, -1.0))
        self.move_pivot_to("Right Arm", point)
        key_rotation(self.armature, "Right Arm", 1, (0, 0, 0))
        key_rotation(self.armature, "Right Arm", 10, (0.9, 0.2, 0.0))
        bpy.ops.object.mode_set(mode="OBJECT")
        before = export(self.armature)
        self.assertNotIn("rig_changes", before)
        name = self.armature.name
        hash_before = utils.get_armature_timeline_hash(name)

        select_only(self.armature, {"Right Arm", "Left Arm"})
        self.assertEqual(bpy.ops.object.rbxanims_write_pivot_to_studio(), {"FINISHED"})
        bpy.ops.object.mode_set(mode="OBJECT")

        self.assertNotEqual(utils.get_armature_timeline_hash(name), hash_before)
        animation = export(self.armature)
        self.assertEqual(animation["kfs"], before["kfs"])
        changes = animation["rig_changes"]
        self.assertEqual([change["part1"] for change in changes], ["Right Arm"])
        change = changes[0]
        self.assertEqual((change["type"], change["part0"]), ("joint_pivot", "Torso"))
        bone = self.armature.data.bones["Right Arm"]
        offset = cf_to_mat(change["offset"])
        for key, prop in (("c0", "transform0"), ("c1", "transform1")):
            self.assertLess(max_difference(cf_to_mat(change["previous_" + key]), to_matrix(bone[prop])), 1e-6)
            self.assertLess(max_difference(cf_to_mat(change[key]), to_matrix(bone[prop]) @ offset), 1e-6)
        # The new joint sits on the pivot: C1' places the joint at the drawn head.
        joint = to_matrix(bone["transform"]) @ cf_to_mat(change["c1"])
        self.assertLess(((T2B @ joint).to_translation() - point).length, 1e-4)

        # Studio applying it: new C0/C1, this joint's poses as offset^-1 * pose * offset.
        def convert(row):
            row = dict(row)
            if "Right Arm" in row:
                row["Right Arm"] = mat_to_cf(offset.inverted() @ cf_to_mat(row["Right Arm"]) @ offset)
            return row

        self.assert_export_matches_blender(
            self.armature,
            animation,
            joints={"Right Arm": (cf_to_mat(change["c0"]), cf_to_mat(change["c1"]))},
            convert=convert,
        )

    def test_queued_pivot_follows_the_head_and_is_dropped_on_reset(self):
        bone = self.armature.data.bones["Right Arm"]
        self.move_pivot_to("Right Arm", bone.head_local + Vector((0.0, 0.0, -0.5)))
        self.assertEqual(pivots.queue_studio_pivots(self.armature, ["Right Arm", "Head"]), (["Right Arm"], ["Head"]))
        first = pivots.pending_rig_changes(self.armature)[0]["offset"]
        bone = self.armature.data.bones["Right Arm"]
        self.move_pivot_to("Right Arm", bone.head_local + Vector((0.0, 0.0, -0.5)))
        second = pivots.pending_rig_changes(self.armature)[0]["offset"]
        self.assertGreater(abs(Vector(first[:3]).length - Vector(second[:3]).length), 0.4)

        select_only(self.armature, {"Right Arm"})
        self.assertEqual(bpy.ops.object.rbxanims_keep_pivot_in_blender(), {"FINISHED"})
        bpy.ops.object.mode_set(mode="OBJECT")
        self.assertEqual(pivots.pending_rig_changes(self.armature), [])

        pivots.queue_studio_pivots(self.armature, ["Right Arm"])
        pivots.reset_pivots(self.armature, ["Right Arm"])
        self.assertEqual(pivots.pending_rig_changes(self.armature), [])
        self.assertNotIn("rig_changes", export(self.armature))

    def test_applied_studio_pivot_becomes_the_joint(self):
        point = self.armature.data.bones["Right Arm"].head_local + Vector((0.0, 0.0, -1.0))
        self.move_pivot_to("Right Arm", point)
        rest_export = export(self.armature)
        pivots.queue_studio_pivots(self.armature, ["Right Arm"])
        change = pivots.pending_rig_changes(self.armature)[0]

        self.assertTrue(pivots.apply_studio_pivot(self.armature, "Right Arm"))

        bone = self.armature.data.bones["Right Arm"]
        self.assertLess(max_difference(to_matrix(bone["transform0"]), cf_to_mat(change["c0"])), 1e-6)
        self.assertLess(max_difference(to_matrix(bone["transform1"]), cf_to_mat(change["c1"])), 1e-6)
        self.assertIsNone(pivots.pivot_offset(bone))
        self.assertEqual(pivots.pending_rig_changes(self.armature), [])
        self.assert_same_animation(rest_export, export(self.armature), 1e-5)

        key_rotation(self.armature, "Right Arm", 1, (0, 0, 0))
        key_rotation(self.armature, "Right Arm", 10, (0.9, 0.2, 0.0))
        bpy.ops.object.mode_set(mode="OBJECT")
        animation = self.assert_export_matches_blender(self.armature)
        self.assertNotIn("rig_changes", animation)
        # The joint itself turns about the pivot now: a pure rotation.
        last = rows(animation)[9]["Right Arm"]
        self.assertLess(Vector(last[:3]).length, 1e-4)

    def test_rig_changes_force_a_full_live_sync(self):
        previous = {"t": 1.0, "kfs": [{"t": 0.0, "kf": {}}]}
        current = dict(previous, rig_changes=[{"type": "joint_pivot", "part1": "Right Arm"}])
        self.assertIsNone(live_sync_delta.create_delta(previous, current))


# ---------------------------------------------------------------------------
# "Add controls"
# ---------------------------------------------------------------------------


class ControlChecks(ExportChecks):
    def limb(self, layout, kind, side):
        return next(limb for limb in layout["limbs"] if limb["kind"] == kind and limb["side"] == side)

    def add_controls(self, armature, match=False):
        bpy.context.view_layer.objects.active = armature
        bpy.context.scene.rbx_anim_settings.rbx_anim_armature = armature.name
        self.assertEqual(
            bpy.ops.object.rbxanims_add_controls(match_animation=match), {"FINISHED"}
        )
        return controls.load_layout(armature)

    def remove_controls(self, armature, keep_motion=False):
        bpy.context.view_layer.objects.active = armature
        bpy.context.scene.rbx_anim_settings.rbx_anim_armature = armature.name
        self.assertEqual(
            bpy.ops.object.rbxanims_remove_controls(keep_motion=keep_motion), {"FINISHED"}
        )

    def pose_through_controls(self, armature, layout):
        """Feet, a hand grip, a pole and the root mover, keyed on frames 1 and 10."""
        root = layout["root"]
        foot = self.limb(layout, "leg", "L")
        arm = self.limb(layout, "arm", "R")
        z = Vector((0.0, 0.0, 1.0))
        key_matrix(armature, root, 1, rest(armature, root))
        key_matrix(armature, root, 10, turned(rest(armature, root), z, 0.5, (0.4, 0.3, 0.2)))
        for frame in (1, 10):
            bpy.context.scene.frame_set(frame)
            evaluated = armature.evaluated_get(bpy.context.evaluated_depsgraph_get())
            root_delta = evaluated.pose.bones[root].matrix @ rest(armature, root).inverted()
            lift = (0.0, 0.0, 0.0) if frame == 1 else (0.0, 0.3, 0.6)
            key_matrix(armature, foot["control"], frame,
                       turned(root_delta @ rest(armature, foot["control"]), z, 0.0, lift))
            grip = arm["grip"]
            angle = 0.0 if frame == 1 else 0.6
            key_matrix(armature, grip, frame,
                       turned(root_delta @ rest(armature, grip), Vector((1, 0, 0)), angle,
                              (0.0, 0.0, 0.0) if frame == 1 else (0.2, -0.3, 0.5)))
            key_matrix(armature, arm["pole"], frame,
                       turned(root_delta @ rest(armature, arm["pole"]), z, 0.0,
                              (0.0, 0.0, 0.0) if frame == 1 else (0.0, 0.0, -0.4)))
        bpy.context.scene.frame_set(1)


class TestControlsR15(ControlChecks):
    def setUp(self):
        self.armature, _ = r15_rig()
        key_rotation(self.armature, "Head", 1, (0, 0, 0))
        key_rotation(self.armature, "Head", 10, (0.3, 0, 0))
        bpy.ops.object.mode_set(mode="OBJECT")

    def test_add_controls_builds_the_layout(self):
        joints = {
            bone.name: (bone.matrix_local.copy(), [list(r) for r in bone["nicetransform"]])
            for bone in joint_bones(self.armature)
        }
        layout = self.add_controls(self.armature)

        self.assertEqual(
            sorted((limb["kind"], limb["side"]) for limb in layout["limbs"]),
            [("arm", "L"), ("arm", "R"), ("leg", "L"), ("leg", "R")],
        )
        self.assertEqual(layout["followers"], ["LowerTorso"])
        names = set(self.armature.data.bones.keys())
        for name in ("CTRL-Root", "CTRL-Foot.L", "CTRL-Knee.R", "CTRL-Grip.L", "CTRL-Hand.R", "CTRL-Elbow.L"):
            self.assertIn(name, names)
        collections = self.armature.data.collections
        self.assertTrue(collections[controls.COLLECTION].is_visible)
        self.assertFalse(collections[controls.MECHANISM_COLLECTION].is_visible)
        self.assertFalse(collections[controls.DRIVEN_COLLECTION].is_visible)
        for bone in self.armature.data.bones:
            role = bone.get(utils.CONTROL_BONE_PROP)
            if role == controls.ROLE_MECHANISM:
                self.assertEqual([c.name for c in bone.collections], [controls.MECHANISM_COLLECTION])
            elif role:
                self.assertEqual([c.name for c in bone.collections], [controls.COLLECTION])
                self.assertFalse(bone.use_deform)
        # The joint bones' rests and Roblox data are untouched.
        for bone in joint_bones(self.armature):
            matrix, nice = joints[bone.name]
            self.assertLess(max_difference(bone.matrix_local, matrix), 1e-6, bone.name)
            self.assertEqual([list(r) for r in bone["nicetransform"]], nice)
        with self.assertRaises(ValueError):
            controls.add_controls(self.armature)

    def test_controls_leave_the_rest_export_identical(self):
        before = export(self.armature)
        self.add_controls(self.armature)
        after = export(self.armature)

        self.assertFalse(channel_names(after) & {b.name for b in self.armature.data.bones if utils.is_control_bone(b)})
        self.assertLessEqual(channel_names(after), {part_name(b) for b in joint_bones(self.armature)})
        self.assert_same_animation(before, after, tolerance(self.armature))
        # Studio's "Sync Bones" never sees a control.
        task = dispatch.new_task_id("rest")
        requests.execute_get_bone_rest(task, self.armature.name)
        success, data = requests.pending_responses.pop(task)
        self.assertTrue(success, data)
        self.assertFalse(any(name.startswith(("CTRL-", "MCH-")) for name in json.loads(data)["bone_poses"]))

    def test_posing_through_controls_exports_what_blender_shows(self):
        layout = self.add_controls(self.armature)
        self.pose_through_controls(self.armature, layout)
        animation = self.assert_export_matches_blender(self.armature)
        driven = {part_name(self.armature.data.bones[name]) for name in controls.driven_bones(self.armature)}
        self.assertLessEqual(driven, channel_names(animation))
        self.assertFalse(any(name.startswith(("CTRL-", "MCH-")) for name in channel_names(animation)))

    def test_knee_bends_forward_and_the_hand_follows_its_controls(self):
        layout = self.add_controls(self.armature)
        leg = self.limb(layout, "leg", "L")
        arm = self.limb(layout, "arm", "R")
        knee = leg["chain"][1]
        forward = (T2B @ to_matrix(self.armature.data.bones["HumanoidRootPart"]["transform"])).to_3x3() @ Vector((0, 0, -1))

        key_matrix(self.armature, leg["control"], 1,
                   turned(rest(self.armature, leg["control"]), Vector((0, 0, 1)), 0.0, (0.0, 0.0, 0.6)))
        evaluated = self.armature.evaluated_get(bpy.context.evaluated_depsgraph_get())
        moved = evaluated.pose.bones[knee].head - self.armature.data.bones[knee].head_local
        self.assertGreater(moved.dot(forward), 0.2)

        grip = rest(self.armature, arm["grip"])
        key_matrix(self.armature, arm["grip"], 1,
                   turned(grip, Vector((1, 0, 0)), 0.7, (0.1, -0.2, 0.4)))
        evaluated = self.armature.evaluated_get(bpy.context.evaluated_depsgraph_get())
        hand = evaluated.pose.bones[arm["chain"][2]].matrix
        self.assertLess(max_difference(hand, evaluated.pose.bones[arm["control"]].matrix), 1e-3)
        # The hand turns about the grip: the palm stays on the grip handle.
        palm = rest(self.armature, arm["chain"][2]).inverted() @ grip.translation
        self.assertLess(((hand @ palm) - evaluated.pose.bones[arm["grip"]].head).length, 1e-3)

    def test_root_mover_moves_the_body_and_keeps_its_keys(self):
        key_rotation(self.armature, "LowerTorso", 1, (0, 0, 0))
        key_rotation(self.armature, "LowerTorso", 10, (0.0, 0.0, 0.4))
        bpy.ops.object.mode_set(mode="OBJECT")
        scene = bpy.context.scene
        scene.frame_set(10)
        own = self.armature.evaluated_get(bpy.context.evaluated_depsgraph_get()).pose.bones["LowerTorso"].matrix.copy()
        layout = self.add_controls(self.armature)
        root = layout["root"]
        moved = turned(rest(self.armature, root), Vector((0, 0, 1)), 0.6, (1.0, 0.5, 0.0))
        key_matrix(self.armature, root, 10, moved)

        evaluated = self.armature.evaluated_get(bpy.context.evaluated_depsgraph_get())
        delta = moved @ rest(self.armature, root).inverted()
        limit = tolerance(self.armature)
        self.assertLess(max_difference(evaluated.pose.bones["LowerTorso"].matrix, delta @ own), limit)
        self.assertLess(
            max_difference(evaluated.pose.bones["HumanoidRootPart"].matrix, rest(self.armature, "HumanoidRootPart")),
            limit,
        )
        scene.frame_set(1)
        self.assert_export_matches_blender(self.armature)

    def test_removing_controls_restores_the_plain_rig(self):
        key_rotation(self.armature, "LeftUpperArm", 1, (0, 0, 0))
        key_rotation(self.armature, "LeftUpperArm", 10, (0.5, 0.2, 0.0))
        bpy.ops.object.mode_set(mode="OBJECT")
        before = export(self.armature)
        bones_before = sorted(self.armature.data.bones.keys())
        collections_before = sorted(c.name for c in self.armature.data.collections_all)
        layout = self.add_controls(self.armature)
        self.pose_through_controls(self.armature, layout)

        self.remove_controls(self.armature, keep_motion=False)

        self.assertFalse(controls.has_controls(self.armature))
        self.assertEqual(sorted(self.armature.data.bones.keys()), bones_before)
        self.assertEqual(sorted(c.name for c in self.armature.data.collections_all), collections_before)
        self.assertFalse(any(pose_bone.constraints for pose_bone in self.armature.pose.bones))
        self.assertNotIn(controls.LAYOUT_PROP, self.armature)
        action = self.armature.animation_data.action
        paths = [fc.data_path for fc in utils.get_action_fcurves(
            action, slot=utils.get_animation_data_action_slot(self.armature.animation_data, action=action))]
        self.assertFalse(any("CTRL-" in path or "MCH-" in path for path in paths))
        self.assert_same_animation(before, export(self.armature), 1e-6)

    def test_remove_controls_can_keep_their_motion(self):
        layout = self.add_controls(self.armature)
        self.pose_through_controls(self.armature, layout)
        with_controls = export(self.armature)

        self.remove_controls(self.armature, keep_motion=True)

        self.assertFalse(controls.has_controls(self.armature))
        self.assert_same_animation(with_controls, export(self.armature), tolerance(self.armature))

    def test_adding_controls_keeps_the_current_animation(self):
        scene = bpy.context.scene
        x, y = Vector((1, 0, 0)), Vector((0, 1, 0))
        for frame, angle in ((1, 0.0), (10, 0.6)):
            scene.frame_set(frame)
            for name, axis, amount in (
                ("LeftUpperLeg", x, angle), ("LeftLowerLeg", x, -2 * angle), ("LeftFoot", x, angle / 2),
                ("RightUpperArm", y, angle), ("RightLowerArm", y, angle * 0.8),
                ("RightHand", Vector((0, 0, 1)), angle),
            ):
                bpy.context.view_layer.update()
                current = self.armature.evaluated_get(bpy.context.evaluated_depsgraph_get()).pose.bones[name].matrix
                key_matrix(self.armature, name, frame, turned(current.copy(), axis, amount))
        scene.frame_set(1)
        bpy.ops.object.mode_set(mode="OBJECT")
        before = export(self.armature)

        self.add_controls(self.armature, match=True)

        after = self.assert_export_matches_blender(self.armature)
        self.assert_same_animation(before, after, 5 * tolerance(self.armature))


class TestControlsR6(ControlChecks):
    def setUp(self):
        self.armature, _ = r6_rig()
        key_rotation(self.armature, "Head", 1, (0, 0, 0))
        key_rotation(self.armature, "Head", 10, (0.3, 0, 0))
        bpy.ops.object.mode_set(mode="OBJECT")

    def test_r6_limbs_are_one_bone_chains(self):
        layout = self.add_controls(self.armature)
        self.assertEqual(layout["followers"], ["Torso"])
        for limb in layout["limbs"]:
            self.assertEqual(limb["ik_count"], 1)
            self.assertEqual(len(limb["chain"]), 1)
        arm = self.limb(layout, "arm", "L")
        self.assertEqual(arm["grip"], arm["control"])

    def test_rest_export_and_posing_through_controls(self):
        before = export(self.armature)
        layout = self.add_controls(self.armature)
        self.assert_same_animation(before, export(self.armature), tolerance(self.armature))
        self.pose_through_controls(self.armature, layout)
        self.assert_export_matches_blender(self.armature)
        self.remove_controls(self.armature, keep_motion=False)
        self.assert_same_animation(before, export(self.armature), 1e-6)

    def test_import_into_a_controlled_rig_keys_the_controls(self):
        self.add_controls(self.armature)
        turn = Euler((0.5, 0.0, 0.0)).to_matrix().to_4x4()
        payload = {
            "t": 1.0,
            "kfs": [
                {"t": 0, "kf": {"Right Arm": [IDENTITY, "Linear", "Out"]}},
                {"t": 1.0, "kf": {"Right Arm": [mat_to_cf(turn), "Linear", "Out"]}},
            ],
        }
        task = dispatch.new_task_id("import")
        requests.execute_import_animation(task, payload, self.armature.name)
        success, message = requests.pending_responses.pop(task)
        self.assertTrue(success, message)

        animation = self.assert_export_matches_blender(self.armature)
        last = rows(animation)[max(rows(animation))]
        self.assertLess(max(abs(a - b) for a, b in zip(last["Right Arm"], mat_to_cf(turn))), 1e-4)
        self.assertTrue(controls.has_controls(self.armature))


class TestPanel(unittest.TestCase):
    """The panel's new rows draw for a rig with and without controls."""

    class Layout:
        """Stands in for UILayout; records every operator button drawn."""

        def __init__(self, drawn):
            self.drawn = drawn

        def __getattr__(self, name):
            def call(*args, **kwargs):
                if name == "operator":
                    self.drawn.append(args[0])
                return TestPanel.Layout(self.drawn)
            return call

    def draw(self):
        from ..ui.panels import OBJECT_PT_RbxAnimations

        drawn = []
        panel = type("Panel", (), {"layout": self.Layout(drawn)})()
        OBJECT_PT_RbxAnimations.draw(panel, bpy.context)
        return drawn

    def test_panel_draws_controls_and_pivot_rows(self):
        armature, _ = r6_rig()
        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.context.scene.rbx_anim_settings.rbx_anim_armature = armature.name
        drawn = self.draw()
        for name in ("object.rbxanims_add_controls", "object.rbxanims_pivot_to_cursor",
                     "object.rbxanims_write_pivot_to_studio", "object.rbxanims_bone_shape"):
            self.assertIn(name, drawn)
        controls.add_controls(armature)
        armature.data.bones["Right Arm"][utils.PENDING_STUDIO_PIVOT_PROP] = True
        drawn = self.draw()
        self.assertIn("object.rbxanims_remove_controls", drawn)
        self.assertNotIn("object.rbxanims_add_controls", drawn)


if __name__ == "__main__":
    unittest.main()
