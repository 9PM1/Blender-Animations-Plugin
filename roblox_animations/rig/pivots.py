"""
Pivot tools: where a joint bone rotates, and whether the Studio rig agrees.

A joint bone's head is its rotation pivot. Moving the head ("Set pivot to 3D
cursor", or by hand in Edit Mode) keeps the Roblox joint where it is: the
exporter encodes the custom pivot as rotation plus offset on that joint, so
Studio plays it with the rig it already has (see display_offsets).

"Write pivot to the Studio rig" queues a joint change instead. C0 and C1 both
move by the same offset O, the head's position in the joint frame
(C0' = C0·O, C1' = C1·O), so the parts stay exactly where they are and the
Motor6D itself turns about the new pivot. The change is stored as a flag on
the bone and derived from its current head at export time, so it follows the
pivot if the user moves it again. Every export carries it under
``rig_changes`` (see ``pending_rig_changes``) while the poses stay relative
to the joints Studio has now, so playback is right before and after the
Studio plugin learns to apply it.
"""

from __future__ import annotations

from mathutils import Matrix, Vector

from ..core.constants import get_transform_to_blender
from ..core.utils import PENDING_STUDIO_PIVOT_PROP, mat_to_cf, to_matrix
from . import display_offsets


def _matrix_to_idprop(value):
    return [list(row) for row in value]


def _tolerance(point) -> float:
    # Rest data is float32: allow for its precision far from the origin.
    return 1e-4 * max(1.0, max(abs(value) for value in point) / 10.0)


def joint_bones(armature_obj, names):
    """The named bones that stand for a Roblox joint, in the given order."""
    bones = armature_obj.data.bones
    return [
        bones[name] for name in names
        if name in bones and display_offsets.is_joint_bone(bones[name])
    ]


def joint_frame(bone) -> Matrix:
    """The Roblox joint frame (Part1.CFrame * C1) at rest, in Roblox space."""
    return to_matrix(bone["transform"]) @ to_matrix(bone["transform1"])


def joint_origin(bone) -> Vector:
    """Where the Roblox joint is, in armature space."""
    return (get_transform_to_blender() @ joint_frame(bone)).to_translation()


def pivot_offset(bone):
    """O: the drawn head in the joint frame, or None while it's on the joint."""
    head = get_transform_to_blender().inverted() @ bone.head_local
    local = joint_frame(bone).inverted() @ head
    if (bone.head_local - joint_origin(bone)).length <= _tolerance(bone.head_local):
        return None
    return Matrix.Translation(local)


def move_pivots(armature_obj, points) -> list[str]:
    """Put each joint bone's head (its pivot) on an armature-space point.

    The bones keep their direction, length and roll. Their Roblox joints, the
    parts bound to them and the export at rest stay exactly as they were.
    Returns the bones that moved.
    """
    names = [bone.name for bone in joint_bones(armature_obj, points)]
    if not names:
        return []
    with display_offsets.keep_joints(armature_obj) as moved, display_offsets.edit_mode(armature_obj):
        edit_bones = armature_obj.data.edit_bones
        for name in names:
            edit_bone = edit_bones.get(name)
            if edit_bone is not None:
                edit_bone.translate(Vector(points[name]) - edit_bone.head)
    return moved


def reset_pivots(armature_obj, names) -> list[str]:
    """Put the pivots back on their Roblox joints; nothing is left to write."""
    bones = joint_bones(armature_obj, names)
    moved = move_pivots(armature_obj, {bone.name: joint_origin(bone) for bone in bones})
    cancel_studio_pivots(armature_obj, names)
    return moved


def queue_studio_pivots(armature_obj, names):
    """Flag bones whose custom pivot should become their Studio joint.

    Returns (queued, on_joint): bones whose head is on the joint already have
    nothing to write.
    """
    queued, on_joint = [], []
    for bone in joint_bones(armature_obj, names):
        if _parent_part_name(armature_obj, bone) is None or pivot_offset(bone) is None:
            on_joint.append(bone.name)
            continue
        bone[PENDING_STUDIO_PIVOT_PROP] = True
        queued.append(bone.name)
    return queued, on_joint


def cancel_studio_pivots(armature_obj, names) -> list[str]:
    """Keep these pivots in Blender only."""
    cancelled = []
    for name in names:
        bone = armature_obj.data.bones.get(name)
        if bone is not None and PENDING_STUDIO_PIVOT_PROP in bone:
            del bone[PENDING_STUDIO_PIVOT_PROP]
            cancelled.append(name)
    return cancelled


def _part_name(bone) -> str:
    source = bone.get("rbx_source_name")
    return source if isinstance(source, str) and source else bone.name


def _parent_part_name(armature_obj, bone):
    parent = bone.parent
    if parent is None and bone.get("worldspace_bone"):
        parent = armature_obj.data.bones.get(bone.get("worldspace_original_parent", ""))
    return _part_name(parent) if parent is not None else None


def pending_rig_changes(armature_obj) -> list[dict]:
    """Queued joint changes, in the form the Studio plugin applies them.

    Each entry is one Motor6D between ``part0`` (the parent part) and
    ``part1`` (the bone's part). Set C0 to ``c0`` and C1 to ``c1``; ``c0`` is
    the parent-side offset, so swap the two when the Studio joint is reversed
    (its Part0 is ``part1``). ``previous_c0``/``previous_c1`` are the values
    the change applies to: skip the entry when the joint already has the new
    values, and report a conflict when it has neither.

    Poses in the same export are relative to the previous joint. Once the
    joint has the new values, play its poses as
    ``offset:Inverse() * pose * offset``. When Studio reports the change
    applied, ``apply_studio_pivot`` rebases the bone: later exports are
    relative to the new joint and no longer list the change.
    """
    changes = []
    for bone in armature_obj.data.bones:
        if not bone.get(PENDING_STUDIO_PIVOT_PROP) or not display_offsets.is_joint_bone(bone):
            continue
        parent_name = _parent_part_name(armature_obj, bone)
        offset = pivot_offset(bone)
        if parent_name is None or offset is None:
            continue
        c0 = to_matrix(bone["transform0"])
        c1 = to_matrix(bone["transform1"])
        changes.append({
            "type": "joint_pivot",
            "part0": parent_name,
            "part1": _part_name(bone),
            "joint_type": bone.get("rbx_joint_type", "Motor6D"),
            "c0": mat_to_cf(c0 @ offset),
            "c1": mat_to_cf(c1 @ offset),
            "previous_c0": mat_to_cf(c0),
            "previous_c1": mat_to_cf(c1),
            "offset": mat_to_cf(offset),
        })
    return changes


def apply_studio_pivot(armature_obj, name) -> bool:
    """Make a queued pivot the bone's Roblox joint, once Studio has it too.

    The rest and the parts don't move; from now on the bone's poses are
    relative to the new joint, the same C0/C1 the Studio rig uses.
    """
    bone = armature_obj.data.bones.get(name)
    if bone is None or not bone.get(PENDING_STUDIO_PIVOT_PROP):
        return False
    offset = pivot_offset(bone)
    if offset is not None:
        bone["transform0"] = _matrix_to_idprop(to_matrix(bone["transform0"]) @ offset)
        bone["transform1"] = _matrix_to_idprop(to_matrix(bone["transform1"]) @ offset)
        bone["nicetransform"] = _matrix_to_idprop(
            offset.inverted() @ to_matrix(bone["nicetransform"])
        )
    del bone[PENDING_STUDIO_PIVOT_PROP]
    # The exporter reads the evaluated bones.
    armature_obj.data.update_tag()
    return True
