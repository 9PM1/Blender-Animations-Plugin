"""Snapshot and restore the parts of Blender's UI state that sync work touches.

Sync requests run on Blender's main thread while the user is working. Anything
they change (active object, its mode, the current frame) has to be put back,
or live sync visibly hijacks the editor.
"""

from contextlib import contextmanager

import bpy


def _set_mode(obj, mode):
    if obj is None or mode is None or obj.mode == mode:
        return
    try:
        bpy.ops.object.mode_set(mode=mode)
    except Exception:
        pass


@contextmanager
def preserved_editor_state(scene=None):
    """Restore active object, its mode and the current frame on exit."""
    context = bpy.context
    scene = scene or context.scene
    view_layer = context.view_layer
    previous_active = view_layer.objects.active
    previous_mode = previous_active.mode if previous_active is not None else None
    previous_frame = (scene.frame_current, scene.frame_subframe)
    try:
        yield
    finally:
        try:
            current = view_layer.objects.active
            if current is not None and current is not previous_active and current.mode != "OBJECT":
                _set_mode(current, "OBJECT")
            if previous_active is not None and previous_active.name in view_layer.objects:
                view_layer.objects.active = previous_active
                _set_mode(previous_active, previous_mode)
        except ReferenceError:
            pass
        except Exception as exc:
            print(f"Blender Addon: could not restore the active object: {exc}")
        try:
            frame, subframe = previous_frame
            if (scene.frame_current, scene.frame_subframe) != previous_frame:
                scene.frame_set(frame, subframe=subframe)
        except Exception:
            pass


def edit_mode_message(obj) -> str:
    """Why an armature can't be synced while it is in Edit Mode."""
    return (
        f"'{obj.name}' is in Edit Mode. Leave Edit Mode to sync: bone edits are "
        "applied when you exit it."
    )
