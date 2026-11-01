"""
Keep the bone you see in step with the Roblox joint it stands for.

Each Motor6D bone stores the Roblox joint frame (``transform`` and
``transform1``) and ``nicetransform``: the offset from that joint frame to the
bone's drawn rest matrix. Export and import go through that offset. It used to
be written once, when the rig was built, so any Edit Mode change (head, tail,
roll) made the attached parts jump and changed what was exported.

When an armature leaves Edit Mode, every bone whose rest changed during that
session gets its offset updated by the same change, and objects bound with
Child Of get their inverse adjusted so they stay exactly where they were. The
Roblox joint itself is untouched: a moved tail or roll only changes how the
bone looks, and a moved head becomes a custom rotation pivot, which the
exporter encodes as rotation plus offset on the original Roblox joint.

Bones that weren't edited are never touched, so rigs whose offsets came from
older versions keep exporting exactly as before.
"""

from __future__ import annotations

import bpy
from bpy.app.handlers import persistent
from mathutils import Matrix

from ..core.constants import get_transform_to_blender
from ..core.utils import to_matrix

_TOLERANCE = 1e-5
# Object pointer -> (object name, {bone name: rest matrix when Edit Mode
# started}). Keyed by pointer, not name: a deleted and re-imported rig with the
# same name must never inherit another object's snapshot.
_edit_sessions: dict[int, tuple[str, dict[str, Matrix]]] = {}
_refreshing = False


def _matrix_to_idprop(value):
    return [list(row) for row in value]


def _max_difference(left: Matrix, right: Matrix) -> float:
    return max(abs(left[row][column] - right[row][column]) for row in range(4) for column in range(4))


def _is_joint_bone(bone) -> bool:
    return (
        not bone.get("rbx_is_deform_bone", False)
        and all(key in bone for key in ("transform", "transform1", "nicetransform"))
    )


def snapshot_rest(armature_obj) -> dict[str, Matrix]:
    """Rest matrices of the joint bones, as stored on the (non-edit) bones."""
    return {
        bone.name: bone.matrix_local.copy()
        for bone in armature_obj.data.bones
        if _is_joint_bone(bone)
    }


def refresh_display_offsets(armature_obj, previous_rest=None) -> list[str]:
    """Update ``nicetransform`` for bones whose rest moved.

    ``previous_rest`` maps bone names to their rest before the edit (taken
    when Edit Mode started). Without it, the rest implied by the stored joint
    data is used, which re-derives every inconsistent offset. Returns the
    names of the bones that changed.
    """
    if armature_obj is None or getattr(armature_obj, "type", None) != "ARMATURE":
        return []
    if armature_obj.mode == "EDIT":
        # Bone (not EditBone) rest data only updates when Edit Mode is left.
        return []

    t2b = get_transform_to_blender()
    moved = {}
    for bone in armature_obj.data.bones:
        if not _is_joint_bone(bone):
            continue
        try:
            nice = to_matrix(bone["nicetransform"])
            if previous_rest is None:
                joint = t2b @ (to_matrix(bone["transform"]) @ to_matrix(bone["transform1"]))
                old_rest = joint @ nice
            else:
                old_rest = previous_rest.get(bone.name)
                if old_rest is None:
                    continue
        except Exception:
            continue
        new_rest = bone.matrix_local.copy()
        # Rest matrices are float32; scale the tolerance with the coordinates.
        tolerance = _TOLERANCE * max(1.0, max(abs(value) for value in new_rest.translation))
        if _max_difference(old_rest, new_rest) <= tolerance:
            continue
        try:
            # Same joint, new drawn rest: nice' = nice @ old_rest^-1 @ new_rest.
            bone["nicetransform"] = _matrix_to_idprop(nice @ old_rest.inverted() @ new_rest)
        except ValueError:
            continue
        moved[bone.name] = (old_rest, new_rest)

    if moved:
        _keep_bound_objects_in_place(armature_obj, moved)
        print(
            f"Blender Addon: kept the Roblox joints of {len(moved)} edited bone(s) on "
            f"'{armature_obj.name}'; attached parts stay where they were."
        )
    return sorted(moved)


def _keep_bound_objects_in_place(armature_obj, moved) -> None:
    """Child Of parts keep their rest placement after their bone's rest moved."""
    world = armature_obj.matrix_world
    for obj in bpy.data.objects:
        for constraint in getattr(obj, "constraints", ()):
            if constraint.type != "CHILD_OF" or constraint.target is not armature_obj:
                continue
            rests = moved.get(constraint.subtarget)
            if rests is None:
                continue
            old_rest, new_rest = rests
            try:
                constraint.inverse_matrix = (
                    (world @ new_rest).inverted() @ (world @ old_rest) @ constraint.inverse_matrix
                )
            except ValueError:
                continue
            try:
                constraint.set_inverse_pending = False
            except AttributeError:
                pass


def begin_edit_session(armature_obj) -> None:
    """Remember an armature's rest before its Edit Mode changes apply."""
    if armature_obj is None:
        return
    key = armature_obj.as_pointer()
    if key not in _edit_sessions:
        _edit_sessions[key] = (armature_obj.name, snapshot_rest(armature_obj))


def _session_object(key, name):
    """The live object a session belongs to, or None if it's gone."""
    obj = bpy.data.objects.get(name)
    if obj is not None and obj.as_pointer() == key:
        return obj
    for candidate in bpy.data.objects:
        if candidate.as_pointer() == key:
            return candidate
    return None


@persistent
def _edit_mode_exit_handler(_scene=None, _depsgraph=None):
    """Snapshot armatures entering Edit Mode; refresh them when they leave."""
    global _refreshing
    if _refreshing:
        return
    context = bpy.context
    try:
        candidates = list(getattr(context, "objects_in_mode", None) or ())
        active = context.view_layer.objects.active
        if active is not None:
            candidates.append(active)
    except Exception:
        candidates = []
    for obj in candidates:
        if obj is not None and obj.type == "ARMATURE" and obj.mode == "EDIT":
            begin_edit_session(obj)

    finished = []
    for key, (name, snapshot) in list(_edit_sessions.items()):
        obj = _session_object(key, name)
        if obj is None:
            _edit_sessions.pop(key, None)
        elif obj.mode != "EDIT":
            _edit_sessions.pop(key, None)
            finished.append((obj, snapshot))
    if not finished:
        return
    _refreshing = True
    try:
        for obj, previous_rest in finished:
            try:
                refresh_display_offsets(obj, previous_rest)
            except Exception as exc:
                print(f"Blender Addon: could not keep the joints of '{obj.name}': {exc}")
    finally:
        _refreshing = False


def register_handlers() -> None:
    if _edit_mode_exit_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_edit_mode_exit_handler)


def unregister_handlers() -> None:
    if _edit_mode_exit_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_edit_mode_exit_handler)
    _edit_sessions.clear()
