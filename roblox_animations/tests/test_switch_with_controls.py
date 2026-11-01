"""Parent switching (B6) with the animation controls and pivots (B3) on one rig.

The R6 sword rig of test_parent_switch, with controls added: bones the
controls drive refuse to switch or unparent (it would cut them off from
their control), a control can switch space itself, and the exported poses,
played back the way Studio plays them, put every part where Blender shows it.

Run inside Blender:
blender --background --factory-startup --online-mode --python <runner> -- test_switch_with_controls
"""

import unittest

import bpy
from mathutils import Matrix, Vector

from . import test_parent_switch as switching
from .test_ledger_fixes import channel_names, export
from ..animation.parent_switch import follow_constraint
from ..core.utils import get_action_fcurves
from ..rig import controls

LAST, RELEASE, CATCH = switching.LAST, switching.RELEASE, switching.CATCH


class SwitchWithControlsCase(switching.ParentSwitchCase):
    def add_controls(self, match):
        bpy.context.view_layer.objects.active = self.armature
        self.settings.rbx_anim_armature = self.armature.name
        self.assertEqual(
            bpy.ops.object.rbxanims_add_controls(match_animation=match), {"FINISHED"}
        )
        switching.pose_mode(self.armature, "Sword")
        return controls.load_layout(self.armature)

    def remove_controls(self):
        bpy.context.view_layer.objects.active = self.armature
        self.settings.rbx_anim_armature = self.armature.name
        self.assertEqual(
            bpy.ops.object.rbxanims_remove_controls(keep_motion=True), {"FINISHED"}
        )
        switching.pose_mode(self.armature)

    def right_grip(self, layout):
        return next(
            limb for limb in layout["limbs"] if limb["kind"] == "arm" and limb["side"] == "R"
        )["grip"]

    def placements(self, names):
        return {
            frame: switching.part_placements(self.armature, frame, names) for frame in self.frames()
        }

    def check_studio_playback(self, animation, names):
        tracks = switching.pose_tracks(animation)
        for frame in self.frames():
            expected = switching.part_placements(self.armature, frame, names)
            played = switching.studio_placements(tracks, self.fixture, (frame - 1) / switching.FPS)
            for name in names:
                self.assertLess(switching.difference(played[name], expected[name]), 2e-3, (frame, name))
        self.assertFalse(
            [name for name in channel_names(animation) if name.startswith(("CTRL-", "MCH-"))]
        )

    def check_same_placements(self, before, names, limit=1e-3):
        after = self.placements(names)
        for frame in self.frames():
            for name in names:
                self.assertLess(
                    switching.difference(before[frame][name], after[frame][name]), limit, (frame, name)
                )


class TestControlledBones(SwitchWithControlsCase):
    def test_bones_the_controls_drive_refuse_to_switch_or_unparent(self):
        layout = self.add_controls(match=False)
        arm = self.armature.pose.bones["Right Arm"]
        follows_control = [c for c in arm.constraints if c.name.startswith(controls.CONSTRAINT_PREFIX)]
        self.assertTrue(follows_control)

        def unchanged(name, parent):
            bone = self.armature.data.bones[name]
            self.assertEqual(bone.parent.name, parent)
            self.assertFalse(bone.get("worldspace_bone"))
            self.assertIsNone(follow_constraint(self.armature.pose.bones[name]))

        self.assertEqual(
            bpy.ops.object.rbxanims_parent_switch(bone_name="Right Arm"), {"CANCELLED"}
        )
        switching.pose_mode(self.armature, "Right Arm")
        self.assertEqual(bpy.ops.object.rbxanims_worldspace_unparent(), {"CANCELLED"})
        unchanged("Right Arm", "Torso")
        self.assertFalse(any(constraint.mute for constraint in follows_control))

        # In a mixed selection the controlled bones are skipped, the rest switch.
        switching.pose_mode(self.armature, "Right Arm", "Torso", "Sword")
        self.assertEqual(bpy.ops.object.rbxanims_parent_switch(), {"FINISHED"})
        unchanged("Right Arm", "Torso")
        unchanged("Torso", "HumanoidRootPart")
        self.assertIsNotNone(follow_constraint(self.armature.pose.bones["Sword"]))
        self.assertEqual(controls.driving_control(self.armature, "Torso"), layout["root"])
        self.assertEqual(controls.driving_control(self.armature, "Right Arm"), self.right_grip(layout))
        self.assertIsNone(controls.driving_control(self.armature, "Sword"))

    def test_sword_thrown_from_a_controlled_hand(self):
        self.add_controls(match=True)
        switching.throw(self.armature)
        self.check_studio_playback(export(self.armature), ("Torso", "Right Arm", "Sword"))

    def test_removing_the_controls_keeps_a_thrown_sword(self):
        self.add_controls(match=True)
        switching.throw(self.armature)
        names = ("Torso", "Right Arm", "Sword")
        before = self.placements(names)

        self.remove_controls()

        self.assertFalse(controls.has_controls(self.armature))
        self.assertIsNotNone(follow_constraint(self.armature.pose.bones["Sword"]))
        self.check_same_placements(before, names)
        self.check_studio_playback(export(self.armature), names)


class TestControlSpaceSwitch(SwitchWithControlsCase):
    def test_grip_stays_put_while_the_root_mover_walks_on(self):
        layout = self.add_controls(match=False)
        root, grip = layout["root"], self.right_grip(layout)
        root_bone = self.armature.pose.bones[root]
        for frame, distance in ((1, 0.0), (LAST, 6.0)):
            bpy.context.scene.frame_set(frame)
            root_bone.location = (0.0, distance, 0.0)
            root_bone.keyframe_insert("location", frame=frame)
        switching.linear_keys(self.armature)

        # From frame 20 the hand stays where it is (on a wall, say).
        switching.switch(20, bone=grip)

        held = switching.pose_matrices(self.armature, [grip, root], range(20, LAST + 1))
        for frame in range(21, LAST + 1):
            self.assertLess(switching.difference(held[frame][grip], held[20][grip]), 1e-4, frame)
        self.assertGreater((held[LAST][root].translation - held[20][root].translation).length, 1.0)

        self.settings.rbx_parent_switch_markers = True
        names = ("Torso", "Head", "Right Arm", "Left Arm", "Right Leg", "Sword")
        animation = export(self.armature)
        self.check_studio_playback(animation, names)
        # The grip isn't a part: no ParentSwitch marker for it.
        self.assertFalse([m for m in animation.get("markers", []) if m["name"] == "ParentSwitch"])

        before = self.placements(names)
        self.remove_controls()
        self.check_same_placements(before, names)
        paths = [curve.data_path for curve in get_action_fcurves(self.armature.animation_data.action)]
        self.assertFalse([path for path in paths if "CTRL-" in path or "MCH-" in path])


class TestPivotOnSwitchedParent(SwitchWithControlsCase):
    def test_new_parent_pivot_keeps_the_thrown_sword(self):
        switching.throw(self.armature)
        flying = range(RELEASE, CATCH)
        before = switching.pose_matrices(self.armature, ["Sword"], flying)

        switching.pose_mode(self.armature, "Right Arm")
        head = self.armature.data.bones["Right Arm"].head_local.copy()
        bpy.context.scene.cursor.location = self.armature.matrix_world @ (head + Vector((0.0, 0.3, -0.5)))
        self.assertEqual(bpy.ops.object.rbxanims_pivot_to_cursor(), {"FINISHED"})

        # The arm now turns about its new pivot and carries the sword with it
        # while it follows; in flight the sword keeps its own path.
        after = switching.pose_matrices(self.armature, ["Sword"], flying)
        for frame in flying:
            self.assertLess(
                switching.difference(before[frame]["Sword"], after[frame]["Sword"]), 1e-4, frame
            )
        rest_arm = self.armature.data.bones["Right Arm"]
        self.assertGreater((rest_arm.head_local - head).length, 0.4)
        self.check_studio_playback(export(self.armature), ("Torso", "Right Arm", "Sword"))


if __name__ == "__main__":
    unittest.main()
