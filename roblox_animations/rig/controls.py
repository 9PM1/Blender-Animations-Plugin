"""
Animation controls for a Roblox rig ("Add controls").

Control bones live in their own bone collection and drive the joint bones
through constraints. The exporter samples the evaluated joint bones (every
frame, because they are constrained) and never exports a control. Nothing
here changes a joint bone's rest or its Roblox joint data, so at rest the rig
exports exactly as it did before, and removing the controls restores the
plain rig.

    CTRL-Root            root mover: the body follows it like a parent and
                         keeps its own keys
    CTRL-Foot.L/R        foot IK control; the foot turns about the ankle
    CTRL-Knee.L/R        knee pole
    CTRL-Grip.L/R        grip handle: the hand turns about the palm
      CTRL-Hand.L/R      hand IK control; the hand turns about the wrist
    CTRL-Elbow.L/R       elbow pole

R6 limbs are single parts: their IK chain is one bone and the grip handle is
the hand control. Each limb is solved on hidden MCH copies of its joint
bones, so keys left on the joint bones can't leak into the IK; the joint
bones copy the MCH bones' local transforms.
"""

from __future__ import annotations

import json
import math
from contextlib import contextmanager

import bpy
from mathutils import Matrix, Vector

from ..core.constants import get_transform_to_blender
from ..core.utils import (
    CONTROL_BONE_PROP,
    get_action_fcurves,
    get_animation_data_action_slot,
    is_control_bone,
    to_matrix,
)
from . import display_offsets

COLLECTION = "RBX Controls"
MECHANISM_COLLECTION = "RBX Control Mechanism"
DRIVEN_COLLECTION = "RBX Controlled Bones"
CONSTRAINT_PREFIX = "RBX Controls"
LAYOUT_PROP = "rbx_controls"

ROLE_ROOT = "root"
ROLE_IK = "ik"
ROLE_GRIP = "grip"
ROLE_POLE = "pole"
ROLE_MECHANISM = "mechanism"

# Roblox part names of each limb, root to tip. R15 limbs end in a hand or
# foot that follows the control's rotation; R6 limbs are a single part.
_LIMB_PARTS = {
    "arm": (("{side}UpperArm", "{side}LowerArm", "{side}Hand"), ("{side} Arm",)),
    "leg": (("{side}UpperLeg", "{side}LowerLeg", "{side}Foot"), ("{side} Leg",)),
}
_SIDES = (("Left", "L"), ("Right", "R"))
_POLE_NAMES = {"arm": "Elbow", "leg": "Knee"}
_SIDE_COLORS = {"L": "THEME04", "R": "THEME01", "": "THEME09"}

SHAPES = ("SPHERE", "RING", "ARROW")
_SHAPE_PREFIX = "__RBXShape_"

# A limb whose middle joint is within this fraction of its reach from the
# straight line counts as straight; its IK copy is bent by _PRE_BEND.
_STRAIGHT = 0.01
_PRE_BEND = 0.01


def _part_name(bone) -> str:
    source = bone.get("rbx_source_name")
    return source if isinstance(source, str) and source else bone.name


def has_controls(armature_obj) -> bool:
    if armature_obj is None or armature_obj.type != "ARMATURE":
        return False
    return bool(armature_obj.get(LAYOUT_PROP)) or any(
        is_control_bone(bone) for bone in armature_obj.data.bones
    )


def load_layout(armature_obj):
    try:
        return json.loads(armature_obj.get(LAYOUT_PROP, ""))
    except (TypeError, ValueError):
        return None


def describe_rig(armature_obj) -> dict:
    """The rig root, the bones that follow it, and the arms and legs found."""
    bones = armature_obj.data.bones
    joints = [
        bone for bone in bones
        if display_offsets.is_joint_bone(bone) and not is_control_bone(bone)
    ]
    by_part = {}
    for bone in joints:
        by_part.setdefault(_part_name(bone), bone)
    roots = [bone for bone in joints if bone.parent is None and not bone.get("worldspace_bone")]
    root = by_part.get("HumanoidRootPart")
    if root not in roots:
        root = max(roots, key=lambda bone: len(bone.children_recursive), default=None)

    limbs = []
    for kind, layouts in _LIMB_PARTS.items():
        for side, suffix in _SIDES:
            for parts in layouts:
                chain = [by_part.get(part.format(side=side)) for part in parts]
                if None in chain or chain[0].parent is None:
                    continue
                if any(child.parent != parent for parent, child in zip(chain, chain[1:])):
                    continue
                limbs.append({"kind": kind, "side": suffix, "chain": [bone.name for bone in chain]})
                break
    return {
        "root": root.name if root is not None else None,
        "followers": [
            bone.name for bone in joints if root is not None and bone.parent == root
        ],
        "limbs": limbs,
        "joints": [bone.name for bone in joints],
    }


def _frame(origin, y_axis, z_hint) -> Matrix:
    """A bone matrix at ``origin`` pointing along ``y_axis``, Z towards ``z_hint``."""
    y = Vector(y_axis).normalized()
    x = y.cross(Vector(z_hint))
    if x.length < 1e-6:
        x = y.orthogonal()
    x.normalize()
    z = x.cross(y)
    matrix = Matrix((x, y, z)).transposed().to_4x4()
    matrix.translation = origin
    return matrix


def _moved(matrix, point) -> Matrix:
    result = matrix.copy()
    result.translation = point
    return result


def _signed_angle(u, v, normal) -> float:
    angle = u.angle(v)
    if u.cross(v).angle(normal) < 1:
        angle = -angle
    return angle


def _pole_angle(base, ik_bone, pole_location) -> float:
    """The IK pole angle that keeps the chain exactly at its rest pose."""
    pole_normal = (ik_bone.tail - base.head).cross(pole_location - base.head)
    projected = pole_normal.cross(base.tail - base.head)
    return _signed_angle(base.x_axis, projected, base.tail - base.head)


def _build_bones(armature_obj, rig) -> dict:
    """Create the control and MCH bones (Edit Mode). Returns the layout."""
    edit_bones = armature_obj.data.edit_bones
    t2b = get_transform_to_blender()
    taken = set(edit_bones.keys())
    roles = {}

    def new_bone(name, role, matrix=None, length=None, parent=None, copy_of=None):
        unique, index = name, 1
        while unique in taken:
            unique = f"{name}.{index:03d}"
            index += 1
        taken.add(unique)
        edit_bone = edit_bones.new(unique)
        if copy_of is not None:
            edit_bone.head = copy_of.head.copy()
            edit_bone.tail = copy_of.tail.copy()
            edit_bone.roll = copy_of.roll
        else:
            edit_bone.head = (0.0, 0.0, 0.0)
            edit_bone.tail = (0.0, max(length, 1e-3), 0.0)
            edit_bone.matrix = matrix
        edit_bone.parent = parent
        edit_bone.use_connect = False
        edit_bone.use_deform = False
        edit_bone[CONTROL_BONE_PROP] = role
        roles[unique] = role
        return edit_bone

    joints = [edit_bones[name] for name in rig["joints"]]
    heights = [point.z for bone in joints for point in (bone.head, bone.tail)]
    ground = min(heights)
    size = max(0.1, (max(heights) - ground) * 0.06)
    up = Vector((0.0, 0.0, 1.0))

    root = edit_bones[rig["root"]]
    forward = (t2b @ to_matrix(root["transform"])).to_3x3() @ Vector((0.0, 0.0, -1.0))
    forward.z = 0.0
    forward = forward.normalized() if forward.length > 1e-6 else Vector((0.0, 1.0, 0.0))
    root_control = new_bone(
        "CTRL-Root", ROLE_ROOT,
        _frame(Vector((root.head.x, root.head.y, ground)), forward, up), size * 4,
    )

    limbs = []
    for limb in rig["limbs"]:
        chain = [edit_bones[name] for name in limb["chain"]]
        two_bone = len(chain) == 3
        ik_bones = chain[:2] if two_bone else chain[:1]
        end = ik_bones[-1]
        side = limb["side"]

        mechanism = []
        for joint in chain:
            parent = mechanism[-1] if mechanism else chain[0].parent
            mechanism.append(new_bone(f"MCH-{joint.name}", ROLE_MECHANISM, parent=parent, copy_of=joint))

        grip = None
        if limb["kind"] == "arm":
            if two_bone:
                hand = chain[2]
                palm = (t2b @ to_matrix(hand["transform"])).to_translation()
                grip = new_bone(f"CTRL-Grip.{side}", ROLE_GRIP, _moved(hand.matrix, palm),
                                hand.length, parent=root_control)
                control = new_bone(f"CTRL-Hand.{side}", ROLE_IK, parent=grip, copy_of=hand)
            else:
                grip = control = new_bone(f"CTRL-Grip.{side}", ROLE_GRIP, _moved(end.matrix, end.tail),
                                          size * 2, parent=root_control)
        elif two_bone:
            control = new_bone(f"CTRL-Foot.{side}", ROLE_IK, parent=root_control, copy_of=chain[2])
        else:
            control = new_bone(f"CTRL-Foot.{side}", ROLE_IK, _moved(end.matrix, end.tail),
                               size * 2, parent=root_control)
        target = new_bone(f"MCH-IK-{end.name}", ROLE_MECHANISM, _moved(control.matrix, end.tail),
                          size, parent=control)

        # The pole sits on the side the limb already bends to; a straight limb
        # bends knees forward and elbows back. Its angle keeps the rest exact.
        start, tip = ik_bones[0].head, end.tail
        reach = (tip - start).length
        axis = (tip - start).normalized()
        direction = None
        if two_bone:
            bend = end.head - start
            bend -= axis * bend.dot(axis)
            if bend.length > _STRAIGHT * reach:
                direction = bend.normalized()
        if direction is None:
            preferred = forward if limb["kind"] == "leg" else -forward
            direction = preferred - axis * preferred.dot(axis)
            if direction.length < 1e-6:
                direction = ik_bones[0].z_axis.copy()
            direction.normalize()
            if two_bone:
                # IK can't choose a bend for a straight chain, so the hidden
                # copy gets a slight one; the joint bones keep their rest.
                upper, lower = mechanism[0], mechanism[1]
                upper.tail = upper.tail + direction * _PRE_BEND * reach
                lower.head = lower.head + direction * _PRE_BEND * reach
        middle = end.head if two_bone else (start + tip) / 2
        pole_head = middle + direction * max((tip - start).length, size * 2)
        pole = new_bone(f"CTRL-{_POLE_NAMES[limb['kind']]}.{side}", ROLE_POLE,
                        _frame(pole_head, direction, up), size, parent=root_control)
        pole_angle = _pole_angle(mechanism[0], mechanism[len(ik_bones) - 1], pole.head)

        limbs.append({
            "kind": limb["kind"],
            "side": side,
            "chain": limb["chain"],
            "mechanism": [bone.name for bone in mechanism],
            "ik_count": len(ik_bones),
            "control": control.name,
            "grip": grip.name if grip is not None else None,
            "target": target.name,
            "pole": pole.name,
            "pole_angle": pole_angle,
        })

    # New bones join the active bone collection; controls only belong to ours.
    for name in roles:
        edit_bone = edit_bones[name]
        for collection in list(edit_bone.collections):
            collection.unassign(edit_bone)

    return {
        "version": 1,
        "root": root_control.name,
        "followers": list(rig["followers"]),
        "limbs": limbs,
        "roles": roles,
    }


def _shape_object(kind):
    """A wireframe mesh used as a custom bone shape (shared across rigs)."""
    name = _SHAPE_PREFIX + kind
    existing = bpy.data.objects.get(name)
    if existing is not None and existing.type == "MESH":
        return existing

    vertices, edges = [], []

    def loop(points, closed=True):
        start = len(vertices)
        vertices.extend(points)
        count = len(points)
        edges.extend((start + index, start + (index + 1) % count)
                     for index in range(count if closed else count - 1))

    def circle(radius, plane, center=(0.0, 0.0, 0.0), segments=24):
        points = []
        for index in range(segments):
            angle = 2.0 * math.pi * index / segments
            a, b = math.cos(angle) * radius, math.sin(angle) * radius
            offset = {"XY": (a, b, 0.0), "XZ": (a, 0.0, b), "YZ": (0.0, a, b)}[plane]
            points.append(tuple(c + o for c, o in zip(center, offset)))
        loop(points)

    def arrow(length, head, base=(0.0, 0.0, 0.0)):
        x, y, z = base
        loop([(x, y, z), (x, y + length, z)], closed=False)
        tip = (x, y + length, z)
        for side in ((head, 0.0), (-head, 0.0), (0.0, head), (0.0, -head)):
            loop([tip, (x + side[0], y + length - head * 1.5, z + side[1])], closed=False)

    if kind == "SPHERE":
        for plane in ("XY", "XZ", "YZ"):
            circle(0.5, plane)
    elif kind == "RING":
        circle(0.5, "XZ")
    elif kind == "ARROW":
        arrow(1.0, 0.12)
    elif kind == "ROOT":
        circle(1.0, "XY")
        arrow(1.4, 0.15)
    else:
        raise ValueError(f"Unknown bone shape: {kind}")

    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, edges, [])
    mesh.update()
    shape = bpy.data.objects.new(name, mesh)
    shape.hide_viewport = True
    shape.hide_render = True
    return shape


def set_bone_shape(pose_bone, kind, scale=1.0) -> None:
    """Draw a bone as a sphere, ring or arrow (display only); None restores it."""
    if kind is None:
        pose_bone.custom_shape = None
        return
    pose_bone.custom_shape = _shape_object(kind)
    pose_bone.use_custom_shape_bone_size = True
    pose_bone.custom_shape_scale_xyz = (scale, scale, scale)
    if hasattr(pose_bone, "custom_shape_wire_width"):
        pose_bone.custom_shape_wire_width = 2.0


def _collection(armature_data, name, visible=True):
    collection = armature_data.collections.get(name)
    if collection is None:
        collection = armature_data.collections.new(name)
    collection.is_visible = visible
    return collection


def _constraint(pose_bone, kind, label):
    constraint = pose_bone.constraints.new(kind)
    constraint.name = f"{CONSTRAINT_PREFIX}: {label}"
    return constraint


def _setup_pose(armature_obj, layout) -> None:
    data = armature_obj.data
    pose_bones = armature_obj.pose.bones
    controls = _collection(data, COLLECTION)
    mechanism = _collection(data, MECHANISM_COLLECTION, visible=False)
    driven = _collection(data, DRIVEN_COLLECTION, visible=False)

    sides = {layout["root"]: ""}
    for limb in layout["limbs"]:
        for name in (limb["control"], limb["grip"], limb["pole"]):
            if name:
                sides[name] = limb["side"]

    for name, role in layout["roles"].items():
        bone = data.bones[name]
        pose_bone = pose_bones[name]
        pose_bone.rotation_mode = "QUATERNION"
        pose_bone.lock_scale = (True, True, True)
        if role == ROLE_MECHANISM:
            mechanism.assign(bone)
            pose_bone.lock_location = (True, True, True)
            pose_bone.lock_rotation = (True, True, True)
            pose_bone.lock_rotation_w = True
            continue
        controls.assign(bone)
        pose_bone.color.palette = _SIDE_COLORS[sides.get(name, "")]
        if role == ROLE_ROOT:
            set_bone_shape(pose_bone, "ROOT")
        elif role == ROLE_POLE:
            pose_bone.lock_rotation = (True, True, True)
            pose_bone.lock_rotation_w = True
            set_bone_shape(pose_bone, "SPHERE", 0.6)
        elif role == ROLE_GRIP and name != _limb_control(layout, name):
            set_bone_shape(pose_bone, "SPHERE", 0.8)
        else:
            set_bone_shape(pose_bone, "RING", 1.6)

    for name in layout["followers"]:
        constraint = _constraint(pose_bones[name], "ARMATURE", "Root")
        target = constraint.targets.new()
        target.target = armature_obj
        target.subtarget = layout["root"]
        target.weight = 1.0

    for limb in layout["limbs"]:
        mechanism_bones = limb["mechanism"]
        ik = _constraint(pose_bones[mechanism_bones[limb["ik_count"] - 1]], "IK", "IK")
        ik.target = armature_obj
        ik.subtarget = limb["target"]
        ik.pole_target = armature_obj
        ik.pole_subtarget = limb["pole"]
        ik.pole_angle = limb["pole_angle"]
        ik.chain_count = limb["ik_count"]
        if len(mechanism_bones) > limb["ik_count"]:
            rotation = _constraint(pose_bones[mechanism_bones[-1]], "COPY_ROTATION", "Rotation")
            rotation.target = armature_obj
            rotation.subtarget = limb["control"]
        for joint, copy in zip(limb["chain"], mechanism_bones):
            follow = _constraint(pose_bones[joint], "COPY_TRANSFORMS", "Follow")
            follow.target = armature_obj
            follow.subtarget = copy
            follow.owner_space = "LOCAL"
            follow.target_space = "LOCAL_OWNER_ORIENT"
            driven.assign(data.bones[joint])


def _limb_control(layout, name):
    for limb in layout["limbs"]:
        if name in (limb["control"], limb["grip"]):
            return limb["control"]
    return None


def add_controls(armature_obj) -> dict:
    """Build the control rig on a Roblox armature; returns its layout."""
    if armature_obj is None or armature_obj.type != "ARMATURE":
        raise ValueError("Select a Roblox armature first.")
    if armature_obj.mode == "EDIT":
        raise ValueError("Leave Edit Mode before adding controls.")
    if has_controls(armature_obj):
        raise ValueError(f"'{armature_obj.name}' already has controls.")
    rig = describe_rig(armature_obj)
    if rig["root"] is None:
        raise ValueError(f"'{armature_obj.name}' has no Roblox root joint to build controls on.")
    with display_offsets.keep_joints(armature_obj), display_offsets.edit_mode(armature_obj):
        layout = _build_bones(armature_obj, rig)
    _setup_pose(armature_obj, layout)
    armature_obj[LAYOUT_PROP] = json.dumps(layout)
    return layout


def driven_bones(armature_obj) -> list[str]:
    """Joint bones the controls drive (they lose that motion without them)."""
    return [
        pose_bone.name for pose_bone in armature_obj.pose.bones
        if not is_control_bone(pose_bone)
        and any(c.name.startswith(CONSTRAINT_PREFIX) for c in pose_bone.constraints)
    ]


@contextmanager
def muted(armature_obj):
    """Turn the controls off for the block: the joint bones show their own keys."""
    changed = []
    for pose_bone in armature_obj.pose.bones:
        if is_control_bone(pose_bone):
            continue
        for constraint in pose_bone.constraints:
            if constraint.name.startswith(CONSTRAINT_PREFIX) and not constraint.mute:
                constraint.mute = True
                changed.append(constraint)
    try:
        yield
    finally:
        for constraint in changed:
            constraint.mute = False


def _actions(armature_obj):
    animation = armature_obj.animation_data
    if animation is None:
        return []
    found = []
    if animation.action is not None:
        found.append((animation.action, get_animation_data_action_slot(animation, action=animation.action)))
    for track in animation.nla_tracks:
        for strip in track.strips:
            if strip.action is not None:
                found.append((strip.action, getattr(strip, "action_slot", None)))
    return found


def _remove_bone_curves(armature_obj, names) -> None:
    prefixes = tuple(f'pose.bones["{bpy.utils.escape_identifier(name)}"]' for name in names)
    if not prefixes:
        return
    for action, slot in _actions(armature_obj):
        try:
            fcurves = get_action_fcurves(action, slot=slot)
        except Exception:
            continue
        for fcurve in [fc for fc in fcurves if fc.data_path.startswith(prefixes)]:
            fcurves.remove(fcurve)


def remove_controls(armature_obj) -> list[str]:
    """Delete the controls and everything they added; returns the bones they drove.

    The joint bones get back exactly what they had: their own keys, rests
    and Roblox joints were never changed.
    """
    if armature_obj.mode == "EDIT":
        raise ValueError("Leave Edit Mode before removing controls.")
    released = driven_bones(armature_obj)
    for pose_bone in armature_obj.pose.bones:
        for constraint in [c for c in pose_bone.constraints if c.name.startswith(CONSTRAINT_PREFIX)]:
            pose_bone.constraints.remove(constraint)
    names = [bone.name for bone in armature_obj.data.bones if is_control_bone(bone)]
    _remove_bone_curves(armature_obj, names)
    if names:
        with display_offsets.keep_joints(armature_obj), display_offsets.edit_mode(armature_obj):
            edit_bones = armature_obj.data.edit_bones
            for name in names:
                edit_bone = edit_bones.get(name)
                if edit_bone is not None:
                    edit_bones.remove(edit_bone)
    collections = armature_obj.data.collections
    for name in (COLLECTION, MECHANISM_COLLECTION, DRIVEN_COLLECTION):
        collection = collections.get(name)
        if collection is not None:
            collections.remove(collection)
    if LAYOUT_PROP in armature_obj:
        del armature_obj[LAYOUT_PROP]
    return released


def _basis(rest, parent_rest, parent_pose, pose) -> Matrix:
    """The local (keyable) transform that puts a bone at an armature-space pose."""
    if parent_rest is None:
        return rest.inverted() @ pose
    return (parent_pose @ parent_rest.inverted() @ rest).inverted() @ pose


def match_controls_to_pose(armature_obj, frames) -> int:
    """Key the limb controls so the controlled rig shows the joints' own keys.

    The joint bones' keys (imported from Studio, or animated before the
    controls were added) are sampled with the controls off; each control is
    then keyed where it reproduces them. The keys stay on the joint bones, so
    removing the controls later shows them again. Returns the frames keyed.
    """
    layout = load_layout(armature_obj)
    frames = sorted({int(frame) for frame in frames})
    if not layout or not layout.get("limbs") or not frames:
        return 0
    bones = armature_obj.data.bones
    pose_bones = armature_obj.pose.bones
    limbs = [
        limb for limb in layout["limbs"]
        if all(name in bones for name in limb["chain"] + limb["mechanism"]
               + [limb["control"], limb["target"], limb["pole"]])
    ]
    root_name = layout["root"] if layout["root"] in bones else None
    scene = bpy.context.scene
    original = (scene.frame_current, scene.frame_subframe)
    rest = {bone.name: bone.matrix_local.copy() for bone in bones}

    def evaluated():
        return armature_obj.evaluated_get(bpy.context.evaluated_depsgraph_get())

    sampled = {}
    try:
        with muted(armature_obj):
            for frame in frames:
                scene.frame_set(frame)
                pose = evaluated().pose
                sampled[frame] = {
                    name: pose.bones[name].matrix.copy()
                    for limb in limbs for name in limb["chain"]
                }

        previous = {}
        for frame in frames:
            scene.frame_set(frame)
            pose = evaluated().pose
            root_pose = pose.bones[root_name].matrix.copy() if root_name else None
            delta = root_pose @ rest[root_name].inverted() if root_name else Matrix.Identity(4)
            fk = sampled[frame]
            for limb in limbs:
                chain, mechanism = limb["chain"], limb["mechanism"]
                control = limb["control"]
                # Each joint copies its MCH twin's turn, so pose the MCH chain
                # with the joints' turns and see where its tip (the IK goal) is.
                twins = {}
                for index in range(limb["ik_count"]):
                    joint, twin = chain[index], mechanism[index]
                    turn = delta @ fk[joint] @ rest[joint].inverted()
                    matrix = (turn.to_3x3() @ rest[twin].to_3x3()).to_4x4()
                    if index == 0:
                        matrix.translation = (turn @ rest[twin]).translation
                    else:
                        above = mechanism[index - 1]
                        matrix.translation = (twins[above] @ rest[above].inverted() @ rest[twin]).translation
                    twins[twin] = matrix
                end = mechanism[limb["ik_count"] - 1]
                tip = twins[end] @ Vector((0.0, bones[end].length, 0.0))

                # The control turns like the hand or foot (an R6 limb's control
                # rides on the limb) and carries the IK goal onto the tip.
                if len(chain) == 3:
                    turned = (delta @ fk[chain[2]]).to_3x3()
                else:
                    turned = (delta @ fk[chain[0]] @ rest[chain[0]].inverted() @ rest[control]).to_3x3()
                goal = (rest[control].inverted() @ rest[limb["target"]]).translation
                placed = turned.to_4x4()
                placed.translation = tip - turned @ goal
                wanted = {control: placed}
                if limb["grip"] and limb["grip"] != control:
                    wanted[limb["grip"]] = placed @ rest[control].inverted() @ rest[limb["grip"]]
                # The pole rides with the limb's first bone, as it does at rest.
                first = mechanism[0]
                wanted[limb["pole"]] = twins[first] @ rest[first].inverted() @ rest[limb["pole"]]
                for name in sorted(wanted, key=lambda n: len(bones[n].parent_recursive)):
                    bone = bones[name]
                    parent = bone.parent
                    parent_pose = None
                    if parent is not None:
                        parent_pose = wanted.get(parent.name)
                        if parent_pose is None:
                            parent_pose = root_pose if parent.name == root_name else pose.bones[parent.name].matrix
                    basis = _basis(rest[name], rest[parent.name] if parent else None, parent_pose, wanted[name])
                    location, rotation, _scale = basis.decompose()
                    if name in previous and previous[name].dot(rotation) < 0:
                        rotation.negate()
                    previous[name] = rotation
                    pose_bone = pose_bones[name]
                    pose_bone.rotation_mode = "QUATERNION"
                    pose_bone.location = location
                    pose_bone.rotation_quaternion = rotation
                    pose_bone.keyframe_insert("location", frame=frame)
                    pose_bone.keyframe_insert("rotation_quaternion", frame=frame)
    finally:
        scene.frame_set(original[0], subframe=original[1])
    return len(frames)
