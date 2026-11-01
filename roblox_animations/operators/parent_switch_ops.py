"""
Keyable parent switching: set bones up and key "Follow parent".

animation/parent_switch.py has the constraint and the export side.
"""

import json

import bpy
from mathutils import Euler, Matrix, Quaternion

from ..animation.parent_switch import (
    add_follow_constraint,
    can_switch_parent,
    follow_constraint,
    follow_curve,
    follow_value,
    original_parent_name,
)
from ..core.utils import get_action_fcurves, pose_bone_selected
from .rig_ops import (
    _bone_transform_path,
    _get_action_frame_range,
    _rekey_bones_with_matrices,
    _rotation_data_path,
    _sample_pose_matrices,
    _sample_world_matrices,
    _snapshot_bone_fcurves,
    _store_fcurve_snapshot,
)


def _matrix_difference(left, right):
    return max(abs(left[r][c] - right[r][c]) for r in range(4) for c in range(4))


def enable_parent_switching(ao, names):
    """Set bones up for parent switching. Returns the names that can switch.

    A parented bone is unparented (with World-Space Unparent's bookkeeping, so
    Restore Parent undoes it) and gets a follow constraint at 1: nothing moves
    and its keys keep their meaning. A world-space bone gets one at 0 and
    stays free. Bones whose settings the constraint can't reproduce
    (connected, partial inheritance, other active constraints) are compared
    with their sampled motion and re-keyed if they moved.
    """
    amt = ao.data
    parented, freed, ready = [], [], []
    for name in names:
        pose_bone = ao.pose.bones[name]
        if follow_constraint(pose_bone) is not None:
            ready.append(name)
        elif original_parent_name(pose_bone):
            freed.append(name)
        elif pose_bone.bone.parent:
            parented.append((name, pose_bone.bone.parent.name))

    def reproducible(pose_bone):
        bone = pose_bone.bone
        return (
            not bone.use_connect
            and bone.use_inherit_rotation
            and bone.inherit_scale == "FULL"
            and bone.use_local_location
            and all(constraint.mute for constraint in pose_bone.constraints)
        )

    parented_names = [name for name, _ in parented]
    checked = [name for name in parented_names if not reproducible(ao.pose.bones[name])]
    if checked:
        frame_start, frame_end = _get_action_frame_range(ao)
        sampled = set(checked) | {parent for name, parent in parented if name in checked}
        before = _sample_world_matrices(ao, sorted(sampled), frame_start, frame_end, substeps=4)

    if parented:
        fcurve_snapshot = _snapshot_bone_fcurves(ao, parented_names)
        for name in parented_names:
            pose_bone = ao.pose.bones[name]
            pose_bone.bone["worldspace_original_basis"] = [v for row in pose_bone.matrix_basis for v in row]
            pose_bone.bone["worldspace_original_connected"] = pose_bone.bone.use_connect
            pose_bone.bone["worldspace_constraints"] = json.dumps(
                {c.name: c.mute for c in pose_bone.constraints}
            )
        bpy.ops.object.mode_set(mode="EDIT")
        for name in parented_names:
            edit_bone = amt.edit_bones.get(name)
            if edit_bone:
                edit_bone.use_connect = False
                edit_bone.parent = None
        bpy.ops.object.mode_set(mode="POSE")
        for name, parent in parented:
            amt.bones[name]["worldspace_bone"] = True
            amt.bones[name]["worldspace_original_parent"] = parent
            add_follow_constraint(ao, ao.pose.bones[name], parent, 1.0)
        _store_fcurve_snapshot(ao, fcurve_snapshot)

    for name in freed:
        pose_bone = ao.pose.bones[name]
        add_follow_constraint(ao, pose_bone, original_parent_name(pose_bone), 0.0)

    if checked:
        after = _sample_world_matrices(ao, checked, frame_start, frame_end)
        moved = [
            name for name in checked
            if any(_matrix_difference(matrix, before[name][frame]) > 1e-4
                   for frame, matrix in after[name].items())
        ]
        targets = {}
        for name in moved:
            # Key the sampled motion in the follow constraint's space, with the
            # bone's other constraints muted as World-Space Unparent does:
            # their effect is part of the samples.
            pose_bone = ao.pose.bones[name]
            constraint = follow_constraint(pose_bone)
            for other in pose_bone.constraints:
                if other != constraint:
                    other.mute = True
            parent = before[original_parent_name(pose_bone)]
            inverse = constraint.inverse_matrix.copy()
            targets[name] = {
                frame: (parent[frame] @ inverse).inverted() @ matrix
                for frame, matrix in before[name].items()
            }
        if targets:
            _rekey_bones_with_matrices(ao, targets)

    if parented:
        # Restore Parent restores losslessly while the keys still match this.
        _store_fcurve_snapshot(
            ao, _snapshot_bone_fcurves(ao, parented_names), prop_name="worldspace_baked_fcurves"
        )
    return ready + parented_names + freed


def _bone_transform_curves(ao, pose_bone):
    """The bone's location/rotation/scale F-Curves."""
    animation = ao.animation_data
    if not animation or not animation.action:
        return []
    escaped = bpy.utils.escape_identifier(pose_bone.name)
    return [
        curve
        for curve in get_action_fcurves(animation.action, slot=getattr(animation, "action_slot", None))
        if _bone_transform_path(getattr(curve, "data_path", ""), escaped)
    ]


def _bone_key_frames(ao, pose_bone):
    """Every frame where the bone has a location/rotation/scale key."""
    return {float(point.co.x) for curve in _bone_transform_curves(ao, pose_bone) for point in curve.keyframe_points}


def _channel_state(ao, pose_bone, frame):
    """Location and rotation at a frame as keyed: {(path, index): (value, interpolation)}.

    The interpolation is the one in effect there, from the key at or before
    the frame. Unkeyed channels have their current value and the default.
    """
    curves = {(curve.data_path, curve.array_index): curve for curve in _bone_transform_curves(ao, pose_bone)}
    default = bpy.context.preferences.edit.keyframe_new_interpolation_type
    state = {}
    for path in ("location", _rotation_data_path(pose_bone)):
        full_path = pose_bone.path_from_id(path)
        for index, current in enumerate(getattr(pose_bone, path)):
            curve = curves.get((full_path, index))
            points = list(curve.keyframe_points) if curve is not None else []
            if not points:
                state[(path, index)] = (current, default)
                continue
            earlier = [point for point in points if point.co.x <= frame + 1e-4]
            state[(path, index)] = (curve.evaluate(frame), (earlier[-1] if earlier else points[0]).interpolation)
    return state


def _key_channels(pose_bone, frame, state=None):
    """Key location and rotation at a frame: the state's values, else the current pose."""
    for path in ("location", _rotation_data_path(pose_bone)):
        if state is not None:
            setattr(pose_bone, path, [state[(path, index)][0] for index in range(len(getattr(pose_bone, path)))])
        pose_bone.keyframe_insert(path, frame=frame, group=pose_bone.name)


def _set_key_interpolation(ao, pose_bone, frame, interpolation):
    """Set the interpolation of the bone's location/rotation keys at a frame.

    Takes one interpolation, or a channel state's. Blender gives a new key its
    neighbour's interpolation, which after a held key would hold it too.
    """
    paths = ("location", _rotation_data_path(pose_bone))
    short_paths = {pose_bone.path_from_id(path): path for path in paths}
    for curve in _bone_transform_curves(ao, pose_bone):
        path = short_paths.get(curve.data_path)
        if path is None:
            continue
        if isinstance(interpolation, str):
            interpolation_here = interpolation
        else:
            interpolation_here = interpolation[(path, curve.array_index)][1]
        for point in curve.keyframe_points:
            if abs(point.co.x - frame) < 1e-4:
                point.interpolation = interpolation_here


def _compatible_rotation(pose_bone, previous):
    """Keep consecutive keys on the short path; returns the rotation to compare next."""
    if pose_bone.rotation_mode == "QUATERNION":
        rotation = pose_bone.rotation_quaternion.copy()
        if previous is not None:
            rotation.make_compatible(previous)
        pose_bone.rotation_quaternion = rotation
        return rotation
    if pose_bone.rotation_mode != "AXIS_ANGLE":
        rotation = pose_bone.rotation_euler.copy()
        if previous is not None:
            rotation.make_compatible(previous)
        pose_bone.rotation_euler = rotation
        return rotation
    return None


def _key_follow(ao, pose_bone, constraint, frame, value):
    """Key the follow value at a frame, as a step."""
    constraint.influence = value
    constraint.keyframe_insert("influence", frame=frame, group=pose_bone.name)
    animation = ao.animation_data
    curve = follow_curve(constraint, animation.action, getattr(animation, "action_slot", None))
    for point in curve.keyframe_points if curve else ():
        if abs(point.co.x - frame) < 1e-4:
            point.interpolation = "CONSTANT"


def switch_parent(ao, pose_bone, frame, keep_position):
    """Flip a switching bone between following and free from a frame on.

    Follow keys are steps, and the value before the frame is held by a key on
    the frame before. With keep_position the bone's own keys from the frame
    up to the next follow key are re-expressed in the new space so nothing
    jumps: the switch frame, the keys in between, and a hold on the frame
    before the next follow key, with the old values keyed on either side.
    Other constraints on the bone aren't accounted for. Returns the new value.
    """
    constraint = follow_constraint(pose_bone)
    animation = ao.animation_data
    action = animation.action if animation else None
    curve = follow_curve(constraint, action, getattr(animation, "action_slot", None))
    held = follow_value(constraint, curve, frame - 1)
    new = 0.0 if follow_value(constraint, curve, frame) >= 0.5 else 1.0
    later = [point.co.x for point in curve.keyframe_points if point.co.x > frame + 1e-4] if curve else []
    end = min(later) if later else None

    if keep_position:
        # Sample everything while the old follow keys still apply.
        parent = original_parent_name(pose_bone)
        frames = {float(frame)}
        frames.update(
            key for key in _bone_key_frames(ao, pose_bone)
            if key > frame and (end is None or key < end)
        )
        if end is not None and end - 1 > frame:
            frames.add(end - 1)
        before = _channel_state(ao, pose_bone, frame - 1)
        at_switch = _channel_state(ao, pose_bone, frame)
        after = _channel_state(ao, pose_bone, end) if end is not None else None
        sampled = _sample_pose_matrices(ao, [pose_bone.name, parent], sorted(frames))

    if curve is None or not any(abs(point.co.x - (frame - 1)) < 1e-4 for point in curve.keyframe_points):
        _key_follow(ao, pose_bone, constraint, frame - 1, held)
    else:
        for point in curve.keyframe_points:
            if abs(point.co.x - (frame - 1)) < 1e-4:
                point.interpolation = "CONSTANT"
    _key_follow(ao, pose_bone, constraint, frame, new)

    if keep_position:
        _key_channels(pose_bone, frame - 1, before)
        _set_key_interpolation(ao, pose_bone, frame - 1, "CONSTANT")
        if after is not None:
            _key_channels(pose_bone, end, after)
            _set_key_interpolation(ao, pose_bone, end, after)
        rotation_path = _rotation_data_path(pose_bone)
        rotation = [before[(rotation_path, index)][0] for index in range(len(getattr(pose_bone, rotation_path)))]
        if pose_bone.rotation_mode == "QUATERNION":
            previous = Quaternion(rotation)
        elif pose_bone.rotation_mode != "AXIS_ANGLE":
            previous = Euler(rotation, pose_bone.rotation_mode)
        else:
            previous = None
        inverse = constraint.inverse_matrix.copy()
        for key in sorted(frames):
            space = sampled[parent][key] @ inverse if new >= 0.5 else Matrix.Identity(4)
            pose_bone.matrix = space.inverted() @ sampled[pose_bone.name][key]
            previous = _compatible_rotation(pose_bone, previous)
            _key_channels(pose_bone, key)
        # The bone carries on from the switch as it moved there before.
        _set_key_interpolation(ao, pose_bone, frame, at_switch)
        if end is not None:
            # Hold the new space until the next follow key takes over.
            _set_key_interpolation(ao, pose_bone, max(frames), "CONSTANT")
    return new


class OBJECT_OT_ParentSwitch(bpy.types.Operator):
    """Key whether bones follow their parent from the current frame on"""

    bl_label = "Switch Parent"
    bl_idname = "object.rbxanims_parent_switch"
    bl_options = {'REGISTER', 'UNDO'}

    bone_name: bpy.props.StringProperty(
        name="Bone",
        description="Bone to switch; empty switches every selected bone with a parent",
        default="",
    )
    keep_position: bpy.props.BoolProperty(
        name="Keep Position",
        description=(
            "Re-key the bone so it stays where it is, instead of moving to its "
            "keyed pose in the new space"
        ),
        default=False,
    )

    @classmethod
    def description(cls, context, properties):
        if properties.keep_position:
            return (
                "Switch the selected bones between following their parent and free "
                "at this frame, re-keying them so they don't jump"
            )
        return (
            "Key whether this bone follows its parent from this frame on. It moves "
            "to its keyed pose in the new space (Switch Here, Keep Position keeps "
            "it in place). Sets up parent switching on first use"
        )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return bool(obj and obj.type == "ARMATURE" and obj.mode == "POSE")

    def execute(self, context):
        ao = context.active_object
        if self.bone_name:
            pose_bone = ao.pose.bones.get(self.bone_name)
            names = [pose_bone.name] if pose_bone and can_switch_parent(pose_bone) else []
        else:
            names = [
                pb.name for pb in ao.pose.bones
                if pose_bone_selected(pb) and can_switch_parent(pb)
            ]
        if not names:
            self.report({"WARNING"}, "Select a bone that has a parent")
            return {"CANCELLED"}

        scene = context.scene
        frame = scene.frame_current
        names = enable_parent_switching(ao, names)
        states = []
        for name in names:
            follows = switch_parent(ao, ao.pose.bones[name], frame, self.keep_position) >= 0.5
            states.append(f"{name} {'follows its parent' if follows else 'is free'}")
        # Show the keyed result rather than the values set while keying.
        scene.frame_set(frame)
        self.report({"INFO"}, f"From frame {frame}: " + ", ".join(states))
        return {"FINISHED"}
