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
to the joints Studio has now. The Studio plugin writes the change to the
rig, plays that export's poses for the new joint, and sends the entry back
(``confirm_studio_changes``); the bone is then rebased onto the new joint.
"""

from __future__ import annotations

import math

from mathutils import Matrix, Vector

from ..core.constants import get_transform_to_blender
from ..core.utils import PENDING_STUDIO_PIVOT_PROP, cf_to_mat, mat_to_cf, to_matrix
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


def has_custom_pivot(bone) -> bool:
    """Whether a joint bone turns about a point off its Roblox joint.

    The export conjugates the bone's turn by ``nicetransform``, so its
    translation (the head in the joint frame) is what puts an offset on the
    joint. Generated bones start with their head on the joint.
    """
    if not display_offsets.is_joint_bone(bone):
        return False
    offset = to_matrix(bone["nicetransform"]).to_translation()
    return offset.length > _tolerance(bone.head_local)


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


def apply_studio_pivot(armature_obj, name, offset=None) -> bool:
    """Make a queued pivot the bone's Roblox joint, once Studio has it too.

    The rest and the parts don't move; from now on the bone's poses are
    relative to the new joint, the same C0/C1 the Studio rig uses.
    ``offset`` is the change Studio applied (its entry's ``offset``) and
    defaults to the bone's pivot now. If the pivot moved again after the
    export Studio applied, the bone stays queued for the rest of the way.
    """
    bone = armature_obj.data.bones.get(name)
    if bone is None:
        return False
    if offset is None:
        if not bone.get(PENDING_STUDIO_PIVOT_PROP):
            return False
        offset = pivot_offset(bone)
    if offset is not None:
        bone["transform0"] = _matrix_to_idprop(to_matrix(bone["transform0"]) @ offset)
        bone["transform1"] = _matrix_to_idprop(to_matrix(bone["transform1"]) @ offset)
        bone["nicetransform"] = _matrix_to_idprop(
            offset.inverted() @ to_matrix(bone["nicetransform"])
        )
    if PENDING_STUDIO_PIVOT_PROP in bone and pivot_offset(bone) is None:
        del bone[PENDING_STUDIO_PIVOT_PROP]
    # The exporter reads the evaluated bones.
    armature_obj.data.update_tag()
    return True


def bone_for_joint(armature_obj, part0, part1):
    """The joint bone of the Roblox joint between two parts, or None."""
    for bone in armature_obj.data.bones:
        if (
            display_offsets.is_joint_bone(bone)
            and _part_name(bone) == part1
            and _parent_part_name(armature_obj, bone) == part0
        ):
            return bone
    return None


def _entry_matrix(value) -> Matrix:
    if not isinstance(value, (list, tuple)) or len(value) < 12:
        raise ValueError("not a CFrame")
    numbers = [float(item) for item in value[:12]]
    if not all(math.isfinite(number) for number in numbers):
        raise ValueError("not a CFrame")
    return cf_to_mat(numbers)


def _close(left, right) -> bool:
    limit = _tolerance(left.to_translation())
    return all(abs(left[row][column] - right[row][column]) <= limit for row in range(4) for column in range(4))


def confirm_studio_changes(armature_obj, entries):
    """Rebase the bones whose ``rig_changes`` entry the Studio rig has now.

    ``entries`` are the entries Studio applied (or found already applied),
    as it got them. A bone whose joint is still the entry's previous one is
    rebased by the entry's offset (``apply_studio_pivot``), even if its
    pivot moved or was dropped since: the Studio joint has changed either
    way. A bone already on the new joint only loses its queued flag.
    Returns the confirmed part names and the skipped entries with a reason.
    """
    applied, skipped = [], []
    for entry in entries if isinstance(entries, list) else []:
        part1 = entry.get("part1") if isinstance(entry, dict) else None
        if not isinstance(entry, dict) or entry.get("type") != "joint_pivot":
            skipped.append({"part1": part1, "reason": "not a joint pivot change"})
            continue
        try:
            c0, c1, previous_c0, previous_c1, offset = (
                _entry_matrix(entry.get(key))
                for key in ("c0", "c1", "previous_c0", "previous_c1", "offset")
            )
        except (TypeError, ValueError):
            skipped.append({"part1": part1, "reason": "the change is incomplete"})
            continue
        if not (_close(previous_c0 @ offset, c0) and _close(previous_c1 @ offset, c1)):
            skipped.append({"part1": part1, "reason": "the change doesn't add up"})
            continue
        bone = bone_for_joint(armature_obj, entry.get("part0"), part1)
        if bone is None:
            skipped.append({
                "part1": part1,
                "reason": f"no bone joins {entry.get('part0')} and {part1}",
            })
            continue
        joint = (to_matrix(bone["transform0"]), to_matrix(bone["transform1"]))
        if _close(joint[0], c0) and _close(joint[1], c1):
            offset = Matrix.Identity(4)
        elif not (_close(joint[0], previous_c0) and _close(joint[1], previous_c1)):
            skipped.append({
                "part1": part1,
                "reason": "its joint changed in Blender after that export",
            })
            continue
        apply_studio_pivot(armature_obj, bone.name, offset)
        applied.append(part1)
    return applied, skipped
