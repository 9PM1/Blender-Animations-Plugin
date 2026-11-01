"""Keyable parent switching (USER_FLOW_FAILURES.md B6).

An R6 rig holds a sword in its right hand and throws it: hand, then free, then
caught. On every frame the exported poses, played back the way Studio plays
them, must put the sword where Blender shows it, while the sword keeps its
sparse keys wherever it follows the hand.

Run inside Blender:
blender --background --factory-startup --online-mode --python <runner> -- test_parent_switch
"""

import copy
import math
import unittest

import bpy
from mathutils import Matrix

from .test_ledger_fixes import R6_SIZES, export, key_rotation, r6_binary
from .test_rbxm_rebuild import import_binary
from .test_studio_template_roundtrip import templates
from ..animation import serialization
from ..animation.parent_switch import FOLLOW_CONSTRAINT, follow_constraint
from ..core.constants import get_transform_to_blender
from ..core.utils import cf_to_mat, get_action_fcurves, mat_to_cf, pose_bone_set_selected, to_matrix
from ..operators import parent_switch_ops, rig_ops
from ..rig import display_offsets

FPS = 30
LAST = 40
RELEASE, CATCH = 11, 26


def sword_fixture():
    """TemplateR6 with a sword held in the right hand by a Motor6D."""
    fixture = copy.deepcopy(templates()["TemplateR6"])
    c0 = Matrix.Translation((0, -1, 0)) @ Matrix.Rotation(-math.pi / 2, 4, "X")
    c1 = Matrix.Translation((0, -1.2, 0))
    arm = cf_to_mat(fixture["parts"]["Right Arm"])
    fixture["parts"]["Sword"] = mat_to_cf(arm @ c0 @ c1.inverted())
    fixture["joints"].append(
        {"part0": "Right Arm", "part1": "Sword", "c0": mat_to_cf(c0), "c1": mat_to_cf(c1)}
    )
    return fixture


def linear_keys(armature, frame=None):
    """Linear keys, which Studio's Linear poses reproduce between keys."""
    for curve in get_action_fcurves(armature.animation_data.action):
        for point in curve.keyframe_points:
            if point.interpolation == "BEZIER" and (frame is None or abs(point.co.x - frame) < 1e-4):
                point.interpolation = "LINEAR"


def key(armature, bone_name, frame, euler):
    key_rotation(armature, bone_name, frame, euler)
    linear_keys(armature, frame)


def sword_rig(fixture):
    armature, _ = import_binary("TemplateR6", r6_binary(dict(R6_SIZES, Sword=(0.2, 3, 0.2)), fixture))
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, LAST
    scene.render.fps, scene.render.fps_base = FPS, 1.0
    key(armature, "Torso", 1, (0, 0, 0))
    key(armature, "Torso", LAST, (0, 0, math.radians(90)))
    for frame, angle in ((1, 0), (10, -100), (20, 0), (30, -50), (LAST, 0)):
        key(armature, "Right Arm", frame, (math.radians(angle), 0, 0))
    pose_mode(armature, "Sword")
    return armature


def pose_mode(armature, *names):
    bpy.context.view_layer.objects.active = armature
    if armature.mode != "POSE":
        bpy.ops.object.mode_set(mode="POSE")
    for pose_bone in armature.pose.bones:
        pose_bone_set_selected(pose_bone, pose_bone.name in names)


def switch(frame, keep_position=True, bone="Sword"):
    bpy.context.scene.frame_set(frame)
    result = bpy.ops.object.rbxanims_parent_switch(bone_name=bone, keep_position=keep_position)
    assert result == {"FINISHED"}, result


def pose_matrices(armature, names, frames):
    """Evaluated armature-space matrices, {frame: {name: matrix}}."""
    scene = bpy.context.scene
    result = {}
    for frame in frames:
        scene.frame_set(int(frame // 1), subframe=frame % 1)
        evaluated = armature.evaluated_get(bpy.context.evaluated_depsgraph_get())
        result[frame] = {name: evaluated.pose.bones[name].matrix.copy() for name in names}
    return result


def throw(armature):
    """Release at RELEASE, fly in armature space, catch at CATCH: keeping position."""
    switch(RELEASE)
    pose_bone = armature.pose.bones["Sword"]
    released = pose_matrices(armature, ["Sword"], [RELEASE])[RELEASE]["Sword"]
    for frame, lift, spin in ((18, 3.0, 2.5), (25, 1.0, 5.0)):
        bpy.context.scene.frame_set(frame)
        pose_bone.matrix = Matrix.Translation((0, -lift, lift)) @ released @ Matrix.Rotation(spin, 4, "X")
        for path in ("location", rig_ops._rotation_data_path(pose_bone)):
            pose_bone.keyframe_insert(path, frame=frame)
        linear_keys(armature, frame)
    switch(CATCH)


def part_placements(armature, frame, names):
    """Where Blender shows each part, in Roblox space (as Part.CFrame)."""
    to_roblox = get_transform_to_blender().inverted()
    matrices = pose_matrices(armature, names, [frame])[frame]
    bones = armature.data.bones
    return {
        name: to_roblox @ matrix
        @ to_matrix(bones[name]["nicetransform"]).inverted()
        @ to_matrix(bones[name]["transform1"]).inverted()
        for name, matrix in matrices.items()
    }


def pose_tracks(animation):
    tracks = {}
    for keyframe in animation["kfs"]:
        for name, pose in keyframe["kf"].items():
            components, style = (pose[0], pose[1]) if isinstance(pose[0], list) else (pose, "Linear")
            tracks.setdefault(name, []).append((keyframe["t"], cf_to_mat(components), style))
    for track in tracks.values():
        track.sort(key=lambda item: item[0])
    return tracks


def pose_at(track, time):
    """A joint's pose, interpolated between its own keyframes as Studio does."""
    if not track:
        return Matrix.Identity(4)
    previous = track[0]
    if time <= previous[0] + 1e-9:
        return previous[1]
    for item in track[1:]:
        if time <= item[0] + 1e-9:
            if abs(time - item[0]) <= 1e-9:
                return item[1]
            start, left, style = previous
            if style == "Constant":
                return left
            alpha = (time - start) / (item[0] - start)
            location = left.to_translation().lerp(item[1].to_translation(), alpha)
            rotation = left.to_quaternion().slerp(item[1].to_quaternion(), alpha)
            return Matrix.Translation(location) @ rotation.to_matrix().to_4x4()
        previous = item
    return previous[1]


def studio_placements(tracks, fixture, time):
    """Each part's CFrame in Studio: Part1 = Part0 * C0 * pose * C1^-1."""
    world = {"HumanoidRootPart": cf_to_mat(fixture["parts"]["HumanoidRootPart"])}
    pending = list(fixture["joints"])
    while pending:
        ready = [joint for joint in pending if joint["part0"] in world]
        assert ready, "disconnected joint graph"
        for joint in ready:
            pose = pose_at(tracks.get(joint["part1"], []), time)
            world[joint["part1"]] = (
                world[joint["part0"]] @ cf_to_mat(joint["c0"]) @ pose @ cf_to_mat(joint["c1"]).inverted()
            )
            pending.remove(joint)
    return world


def difference(left, right):
    return max(abs(a - b) for a, b in zip(mat_to_cf(left), mat_to_cf(right)))


def follow_keys(armature):
    constraint = follow_constraint(armature.pose.bones["Sword"])
    path = constraint.path_from_id("influence")
    for curve in get_action_fcurves(armature.animation_data.action):
        if curve.data_path == path:
            return [(point.co.x, point.co.y, point.interpolation) for point in curve.keyframe_points]
    return []


class ParentSwitchCase(unittest.TestCase):
    def setUp(self):
        preferences = bpy.context.preferences.edit
        self._interpolation = preferences.keyframe_new_interpolation_type
        # Linear keys interpolate the same way in Blender and in Studio.
        preferences.keyframe_new_interpolation_type = "LINEAR"
        self.settings = bpy.context.scene.rbx_anim_settings
        self._markers = self.settings.rbx_parent_switch_markers
        self.fixture = sword_fixture()
        self.armature = sword_rig(self.fixture)

    def tearDown(self):
        bpy.context.preferences.edit.keyframe_new_interpolation_type = self._interpolation
        self.settings.rbx_parent_switch_markers = self._markers
        if bpy.context.object and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

    def frames(self):
        return range(1, LAST + 1)


class TestSetUp(ParentSwitchCase):
    def test_switching_is_set_up_without_moving_anything(self):
        pose_bone = self.armature.pose.bones["Sword"]
        for frame, angle in ((1, 0), (20, 40), (LAST, -30)):
            key(self.armature, "Sword", frame, (0, math.radians(angle), 0))
        names = ["Sword", "Right Arm"]
        before = pose_matrices(self.armature, names, self.frames())
        keys_before = rig_ops._snapshot_bone_fcurves(self.armature, ["Sword"])

        self.assertEqual(parent_switch_ops.enable_parent_switching(self.armature, ["Sword"]), ["Sword"])
        after = pose_matrices(self.armature, names, self.frames())
        for frame in self.frames():
            for name in names:
                self.assertLess(difference(before[frame][name], after[frame][name]), 1e-4, (frame, name))
        pose_bone = self.armature.pose.bones["Sword"]
        self.assertIsNone(pose_bone.bone.parent)
        self.assertEqual(pose_bone.bone["worldspace_original_parent"], "Right Arm")
        self.assertEqual(pose_bone.constraints[0].name, FOLLOW_CONSTRAINT)
        self.assertEqual(follow_constraint(pose_bone), pose_bone.constraints[0])
        self.assertEqual(rig_ops._snapshot_bone_fcurves(self.armature, ["Sword"]), keys_before)
        # The follow constraint stands in for the parent link, nothing more.
        self.assertNotIn("Sword", serialization.get_all_constrained_bones(self.armature))

    def test_restore_parent_undoes_it_losslessly(self):
        key(self.armature, "Sword", 1, (0, 0, 0))
        key(self.armature, "Sword", 20, (0, 0.5, 0))
        keys_before = rig_ops._snapshot_bone_fcurves(self.armature, ["Sword"])
        parent_switch_ops.enable_parent_switching(self.armature, ["Sword"])
        self.assertEqual(bpy.ops.object.rbxanims_worldspace_reparent(), {"FINISHED"})
        bone = self.armature.data.bones["Sword"]
        self.assertEqual(bone.parent.name, "Right Arm")
        self.assertFalse(self.armature.pose.bones["Sword"].constraints)
        self.assertNotIn("worldspace_bone", bone)
        self.assertEqual(rig_ops._snapshot_bone_fcurves(self.armature, ["Sword"]), keys_before)

    def test_a_connected_bone_is_re_keyed_to_stay_in_place(self):
        bpy.ops.object.mode_set(mode="EDIT")
        edit_bone = self.armature.data.edit_bones["Sword"]
        edit_bone.head = edit_bone.parent.tail
        edit_bone.use_connect = True
        bpy.ops.object.mode_set(mode="POSE")
        pose_bone = self.armature.pose.bones["Sword"]
        pose_bone.location = (0.3, 0.1, 0.0)  # ignored while connected
        key(self.armature, "Sword", 1, (0, 0, 0))
        key(self.armature, "Sword", 20, (0.6, 0, 0))
        before = pose_matrices(self.armature, ["Sword"], self.frames())
        parent_switch_ops.enable_parent_switching(self.armature, ["Sword"])
        after = pose_matrices(self.armature, ["Sword"], self.frames())
        for frame in self.frames():
            self.assertLess(difference(before[frame]["Sword"], after[frame]["Sword"]), 1e-4, frame)

    def test_a_world_space_bone_becomes_switchable_and_stays_free(self):
        bpy.ops.object.rbxanims_worldspace_unparent()
        before = pose_matrices(self.armature, ["Sword"], self.frames())
        switch(20, keep_position=True)
        after = pose_matrices(self.armature, ["Sword"], self.frames())
        for frame in self.frames():
            self.assertLess(difference(before[frame]["Sword"], after[frame]["Sword"]), 1e-4, frame)
        self.assertEqual(follow_keys(self.armature), [(19.0, 0.0, "CONSTANT"), (20.0, 1.0, "CONSTANT")])

    def test_follow_keeps_working_after_the_parent_rest_is_edited(self):
        parent_switch_ops.enable_parent_switching(self.armature, ["Sword"])
        display_offsets.begin_edit_session(self.armature)
        bpy.ops.object.mode_set(mode="EDIT")
        self.armature.data.edit_bones["Right Arm"].head.z += 0.2
        bpy.ops.object.mode_set(mode="POSE")
        display_offsets._edit_mode_exit_handler()
        constraint = follow_constraint(self.armature.pose.bones["Sword"])
        expected = self.armature.data.bones["Right Arm"].matrix_local.inverted()
        self.assertLess(difference(constraint.inverse_matrix, expected), 1e-5)


class TestSwitching(ParentSwitchCase):
    def test_switch_keeping_position_does_not_move_the_bone(self):
        before = pose_matrices(self.armature, ["Sword"], self.frames())
        switch(RELEASE)
        after = pose_matrices(self.armature, ["Sword"], self.frames())
        # Free from the release on, so it stays where it was let go.
        for frame in self.frames():
            expected = before[min(frame, RELEASE)]["Sword"]
            self.assertLess(difference(expected, after[frame]["Sword"]), 1e-4, frame)
        self.assertEqual(
            follow_keys(self.armature),
            [(RELEASE - 1.0, 1.0, "CONSTANT"), (float(RELEASE), 0.0, "CONSTANT")],
        )

    def test_toggle_moves_to_the_keyed_pose_in_the_new_space(self):
        before = pose_matrices(self.armature, ["Sword"], self.frames())
        switch(RELEASE, keep_position=False)
        after = pose_matrices(self.armature, ["Sword"], self.frames())
        for frame in range(1, RELEASE):
            self.assertLess(difference(before[frame]["Sword"], after[frame]["Sword"]), 1e-4, frame)
        # Unkeyed and free: back at its rest place in the armature.
        rest = self.armature.data.bones["Sword"].matrix_local
        self.assertLess(difference(after[RELEASE]["Sword"], rest), 1e-4)
        self.assertGreater(difference(before[RELEASE]["Sword"], after[RELEASE]["Sword"]), 0.1)

    def test_keys_after_a_switch_keep_their_place(self):
        key(self.armature, "Sword", 1, (0, 0, 0))
        key(self.armature, "Sword", 30, (0, 0.8, 0))
        original = pose_matrices(self.armature, ["Sword"], self.frames())
        switch(RELEASE)
        released = pose_matrices(self.armature, ["Sword"], self.frames())
        # Re-expressed in the new space: the key at 30 stays where it was.
        for frame in list(range(1, RELEASE + 1)) + [30]:
            self.assertLess(difference(original[frame]["Sword"], released[frame]["Sword"]), 1e-4, frame)
        switch(CATCH)
        caught = pose_matrices(self.armature, ["Sword"], self.frames())
        # Following again from the key at 30, so it moves with the hand as before.
        for frame in list(range(1, RELEASE + 1)) + list(range(30, LAST + 1)):
            self.assertLess(difference(original[frame]["Sword"], caught[frame]["Sword"]), 1e-4, frame)
        for frame in range(RELEASE, CATCH + 1):
            self.assertLess(difference(released[frame]["Sword"], caught[frame]["Sword"]), 1e-4, frame)


class TestThrowExport(ParentSwitchCase):
    def check_studio_playback(self, animation, names=("Torso", "Right Arm", "Sword"), times=None):
        tracks = pose_tracks(animation)
        for name in names:
            styles = {style for _, _, style in tracks.get(name, [])}
            self.assertLessEqual(styles, {"Linear", "Constant"}, name)
        for frame in times or self.frames():
            expected = part_placements(self.armature, frame, names)
            played = studio_placements(tracks, self.fixture, (frame - 1) / FPS)
            for name in names:
                self.assertLess(difference(played[name], expected[name]), 2e-3, (frame, name))

    def test_thrown_sword_plays_back_where_blender_shows_it(self):
        throw(self.armature)
        animation = export(self.armature)
        self.check_studio_playback(animation)

        # Baked every frame from the frame before the release to the catch;
        # the sword's own sparse keys elsewhere.
        frames = {round(kf["t"] * FPS) + 1 for kf in animation["kfs"] if "Sword" in kf["kf"]}
        self.assertLessEqual(set(range(RELEASE - 1, CATCH + 1)), frames)
        self.assertFalse(frames & (set(range(2, RELEASE - 1)) | set(range(CATCH + 1, LAST))), sorted(frames))

    def test_caught_into_the_grip_with_the_toggle(self):
        switch(RELEASE)
        # Key the grip (no offset from the hand) where it's caught, then let
        # the toggle snap it into the hand there.
        pose_bone = self.armature.pose.bones["Sword"]
        pose_bone.matrix_basis = Matrix.Identity(4)
        for path in ("location", rig_ops._rotation_data_path(pose_bone)):
            pose_bone.keyframe_insert(path, frame=CATCH)
        switch(CATCH, keep_position=False)
        self.check_studio_playback(export(self.armature))
        # Back in the hand at its keyed (rest) grip from the catch on.
        placements = part_placements(self.armature, CATCH + 2, ["Right Arm", "Sword"])
        joint = self.fixture["joints"][-1]
        grip = placements["Right Arm"] @ cf_to_mat(joint["c0"]) @ cf_to_mat(joint["c1"]).inverted()
        self.assertLess(difference(placements["Sword"], grip), 1e-3)

    def test_stepped_parent_stays_stepped_while_free(self):
        for curve in get_action_fcurves(self.armature.animation_data.action):
            for point in curve.keyframe_points:
                point.interpolation = "CONSTANT"
        switch(5, keep_position=False)
        switch(15, keep_position=False)
        animation = export(self.armature)
        # The arm steps at frame 10: the free sword's pose holds, then jumps.
        sword = {round(t * FPS) + 1: style for t, _, style in pose_tracks(animation)["Sword"]}
        self.assertEqual(sword[9], "Constant")
        halves = [frame + 0.5 for frame in range(4, 15)]
        tracks = pose_tracks(animation)
        for frame in halves:
            expected = part_placements(self.armature, frame, ["Sword"])["Sword"]
            played = studio_placements(tracks, self.fixture, (frame - 1) / FPS)["Sword"]
            self.assertLess(difference(played, expected), 2e-3, frame)

    def test_parent_switch_markers(self):
        throw(self.armature)
        self.settings.rbx_parent_switch_markers = False
        names = [marker["name"] for marker in export(self.armature).get("markers", [])]
        self.assertNotIn("ParentSwitch", names)

        self.settings.rbx_parent_switch_markers = True
        markers = [
            (round(marker["time"] * FPS) + 1, marker["value"], marker["type"])
            for marker in export(self.armature).get("markers", [])
            if marker["name"] == "ParentSwitch"
        ]
        self.assertEqual(markers, [
            (RELEASE, "Sword → HumanoidRootPart", "Event"),
            (CATCH, "Sword → Right Arm", "Event"),
        ])

    def test_restore_parent_bakes_the_switched_motion(self):
        throw(self.armature)
        before = pose_matrices(self.armature, ["Sword"], self.frames())
        self.assertEqual(bpy.ops.object.rbxanims_worldspace_reparent(), {"FINISHED"})
        after = pose_matrices(self.armature, ["Sword"], self.frames())
        self.assertEqual(self.armature.data.bones["Sword"].parent.name, "Right Arm")
        self.assertFalse(self.armature.pose.bones["Sword"].constraints)
        self.assertFalse([
            curve for curve in get_action_fcurves(self.armature.animation_data.action)
            if "constraints" in curve.data_path
        ])
        for frame in self.frames():
            self.assertLess(difference(before[frame]["Sword"], after[frame]["Sword"]), 1e-4, frame)


if __name__ == "__main__":
    unittest.main()
