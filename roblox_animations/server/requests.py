"""
Request processing and task management for the animation server.
"""

import json
import math
import traceback
import zlib
import bpy
import bpy_extras
from mathutils import Matrix
from ..core.utils import (
    get_scene_fps,
    cf_to_mat,
    get_object_by_name,
    iter_scene_objects,
)
from ..core import utils
from ..animation.serialization import (
    serialize,
    is_deform_bone_rig,
    resolve_export_frame_range,
)
from ..animation.import_export import import_animation_preserve_ik
from ..animation.import_easing import sample_import_keyframes
from .live_sync_delta import animation_revision, create_delta, sanitize_non_finite
from ..core.editor_state import edit_mode_message, preserved_editor_state


# Global request queues
pending_requests = []
pending_responses = {}
# (armature, calibration) -> (serialized animation, revision); insertion-ordered LRU.
live_sync_snapshots = {}

transform_to_blender = bpy_extras.io_utils.axis_conversion(
    from_forward="Z", from_up="Y", to_forward="-Y", to_up="Z"
).to_4x4()  # transformation matrix from Y-up to Z-up

# Deform Pose CFrames use the exporter-specific (-x, y, -z) basis. This is a
# 180-degree Y rotation and is its own inverse. It is deliberately not the
# general Roblox-object Y-up -> Blender Z-up conversion used by Motor6D rigs.
_DEFORM_DELTA_SWIZZLE = Matrix.Diagonal((-1.0, 1.0, -1.0, 1.0))


def _roblox_deform_delta_to_blender(bone_transform, translation_scale=1.0):
    converted = _DEFORM_DELTA_SWIZZLE @ bone_transform @ _DEFORM_DELTA_SWIZZLE
    converted.translation = converted.to_translation() * float(translation_scale)
    return converted


def _resolve_deform_translation_scale(export_info):
    """Recover the exact translation divisor used by deform serialization."""
    explicit_scale = export_info.get("deform_translation_scale_factor")
    if explicit_scale is not None:
        candidate = explicit_scale
    else:
        candidate = export_info.get("deform_scale_factor", 1.0)
        if export_info.get("deform_scale_mode") in {"auto", "auto_calibrated"}:
            try:
                candidate = float(candidate) * float(
                    export_info.get("armature_object_scale", 1.0)
                )
            except (TypeError, ValueError):
                return 1.0

    try:
        candidate = float(candidate)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(candidate) or abs(candidate) < 1e-8:
        return 1.0
    return candidate


def build_pose_bone_resolver(armature, is_deform_rig):
    """Map Roblox channel names to pose bones.

    Bones keep the Roblox part/bone name in ``rbx_source_name`` because the
    Blender name can differ: Blender adds ".001" suffixes to duplicates and
    truncates names longer than 63 bytes, and users rename bones. Lookups try
    the source name first, then the Blender name. Deform rigs prefer deform
    bones; Motor6D rigs prefer joint bones.
    """
    candidates = {}
    for pose_bone in armature.pose.bones:
        data = pose_bone.bone
        source = data.get("rbx_source_name")
        is_deform = bool(data.get("rbx_is_deform_bone", False))
        structural = pose_bone.name.startswith("__RBX_STRUCTURAL__")
        names = {pose_bone.name}
        if isinstance(source, str) and source:
            names.add(source)
        for name in names:
            candidates.setdefault(name, []).append(
                (pose_bone, is_deform, structural, name == source)
            )

    def score(entry):
        _pose_bone, is_deform, structural, by_source = entry
        type_match = is_deform if is_deform_rig else not is_deform
        return (type_match, not structural, by_source)

    def legacy_deform_match(channel_name):
        # Older deform imports named the deform twin "<name>.001" without
        # storing the source name.
        for pose_bone in armature.pose.bones:
            if pose_bone.bone.get("rbx_is_deform_bone", False) and pose_bone.name.startswith(
                f"{channel_name}."
            ) and "rbx_source_name" not in pose_bone.bone:
                return pose_bone
        return None

    def resolve(channel_name):
        entries = candidates.get(channel_name)
        best = max(entries, key=score) if entries else None
        if is_deform_rig and (best is None or not best[1]):
            legacy = legacy_deform_match(channel_name)
            if legacy is not None:
                return legacy
        return best[0] if best else None

    return resolve


def _find_animation_pose_bone(armature, bone_name, is_deform_rig):
    """Backward-compatible single lookup (builds a resolver each call)."""
    return build_pose_bone_resolver(armature, is_deform_rig)(bone_name)


def _get_timeline_scene_for_armature(context_scene, armature):
    users_scenes = list(getattr(armature, "users_scene", []) or [])
    if not users_scenes:
        return context_scene
    named_scene = bpy.data.scenes.get("Scene")
    if named_scene and named_scene in users_scenes:
        return named_scene
    if context_scene in users_scenes:
        return context_scene
    return users_scenes[0]


def _get_reported_bone_parent_name(bone):
    bone_data = getattr(bone, "bone", bone)
    original_parent = bone_data.get("rbx_original_parent", "") if bone_data is not None else ""
    if isinstance(original_parent, str) and original_parent:
        return original_parent

    parent = getattr(bone, "parent", None)
    if parent is None:
        return None

    parent_name = getattr(parent, "name", None)
    if isinstance(parent_name, str) and parent_name:
        return parent_name

    parent_bone = getattr(parent, "bone", None)
    parent_bone_name = getattr(parent_bone, "name", None)
    if isinstance(parent_bone_name, str) and parent_bone_name:
        return parent_bone_name

    return None


def process_pending_requests():
    """Process any pending animation requests"""
    try:
        if pending_requests:  # Check if list is not empty
            print(f"Blender Addon: Processing {len(pending_requests)} pending requests")
            request = pending_requests.pop(0)
            request_type = request[0]

            if request_type == "export_animation":
                if len(request) >= 6:
                    _, task_id, armature_name, target_bone_rest, live_preview, delta_base_hash = request
                elif len(request) >= 5:
                    _, task_id, armature_name, target_bone_rest, live_preview = request
                    delta_base_hash = None
                elif len(request) >= 4:
                    _, task_id, armature_name, target_bone_rest = request
                    live_preview = False
                    delta_base_hash = None
                else:
                    _, task_id, armature_name = request
                    target_bone_rest = None
                    live_preview = False
                    delta_base_hash = None
                print(
                    f"Blender Addon: Dispatching export_animation task (task_id={task_id}, armature={armature_name})"
                )
                execute_in_main_thread(task_id, armature_name, target_bone_rest, live_preview, delta_base_hash)
            elif request_type == "list_armatures":
                _, task_id = request
                print(
                    f"Blender Addon: Dispatching list_armatures task (task_id={task_id})"
                )
                execute_list_armatures(task_id)
            elif request_type == "import":
                if len(request) == 4:
                    _, task_id, animation_data, target_armature = request
                    print(
                        f"Blender Addon: Dispatching import task (task_id={task_id}, target={target_armature})"
                    )
                    execute_import_animation(task_id, animation_data, target_armature)
                else:
                    _, task_id, animation_data = request
                    print(f"Blender Addon: Dispatching import task (task_id={task_id})")
                    execute_import_animation(task_id, animation_data)
            elif request_type == "get_bone_rest":
                _, task_id, armature_name = request
                print(
                    f"Blender Addon: Dispatching get_bone_rest task (task_id={task_id}, armature={armature_name})"
                )
                execute_get_bone_rest(task_id, armature_name)

    except Exception as e:
        print(f"Blender Addon: Error processing requests: {str(e)}")
        traceback.print_exc()
    return 0.01  # Run every 10ms for good balance


def execute_list_armatures(task_id):
    """Execute the armature listing in the main thread"""
    try:
        # Force refresh by invalidating cache first
        utils.invalidate_armature_cache()

        fresh_armatures = [
            obj.name for obj in iter_scene_objects() if obj.type == "ARMATURE"
        ]

        armatures = []
        for armature_name in fresh_armatures:
            obj = get_object_by_name(armature_name)

            if obj:  # Double-check object still exists
                # build bone hierarchy map: { bone_name: parent_bone_name or None }
                bone_hierarchy = {}
                for bone in obj.data.bones:
                    bone_hierarchy[bone.name] = _get_reported_bone_parent_name(bone)

                armature_info = {
                    "name": obj.name,
                    "bones": [bone.name for bone in obj.data.bones],
                    "num_bones": len(obj.data.bones),
                    "has_animation": bool(
                        obj.animation_data and obj.animation_data.action
                    ),
                    "frame_range": [
                        bpy.context.scene.frame_start,
                        bpy.context.scene.frame_end,
                    ]
                    if obj.animation_data
                    else None,
                    "bone_hierarchy": bone_hierarchy,
                }
                armatures.append(armature_info)


        response = {
            "armatures": armatures,
            "current": getattr(
                getattr(bpy.context.scene, "rbx_anim_settings", None),
                "rbx_anim_armature",
                None,
            ),
            "fps": bpy.context.scene.render.fps,
        }

        data = json.dumps(response).encode("utf-8")
        pending_responses[task_id] = (True, data)

    except Exception as e:
        print(f"Blender Addon: Error in execute_list_armatures: {str(e)}")
        traceback.print_exc()
        pending_responses[task_id] = (
            False,
            f"Error listing armatures: {str(e)}",
        )


_LIVE_SYNC_SNAPSHOT_LIMIT = 8


def _remember_snapshot(cache_key, animation, revision):
    """Keep the last few live-sync payloads (one per armature/calibration)."""
    live_sync_snapshots.pop(cache_key, None)
    live_sync_snapshots[cache_key] = (animation, revision)
    while len(live_sync_snapshots) > _LIVE_SYNC_SNAPSHOT_LIMIT:
        live_sync_snapshots.pop(next(iter(live_sync_snapshots)))


def _add_export_warning(serialized, message):
    export_info = serialized.setdefault("export_info", {})
    warnings = export_info.setdefault("warnings", [])
    if message not in warnings:
        warnings.append(message)


def execute_in_main_thread(task_id, armature_name, target_bone_rest=None, live_preview=False, delta_base_hash=None):
    """Export the armature's animation. Never changes the scene or the editor."""
    try:
        if not armature_name:
            pending_responses[task_id] = (False, "No armature selected")
            return

        ao = get_object_by_name(armature_name)
        if not ao:
            pending_responses[task_id] = (
                False,
                f"Armature '{armature_name}' not found.",
            )
            return

        if ao.type != "ARMATURE":
            pending_responses[task_id] = (
                False,
                f"Object '{armature_name}' is not an armature "
                f"(type: {ao.type}). Please select a valid armature object.",
            )
            return

        if ao.mode == "EDIT":
            pending_responses[task_id] = (False, edit_mode_message(ao))
            return

        desired_fps = get_scene_fps()

        # Check if this is a deform bone rig or if deform bone serialization is forced
        settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
        force_deform = getattr(settings, "force_deform_bone_serialization", False)

        use_deform_bone_serialization = is_deform_bone_rig(ao) or force_deform
        print(
            f"Server export: Using {'deform bone' if use_deform_bone_serialization else 'Motor6D'} serialization"
        )

        context_scene = bpy.context.scene

        # --- Pre-export validations ---
        if context_scene.frame_start > context_scene.frame_end:
            pending_responses[task_id] = (
                False,
                f"Invalid frame range: start ({int(context_scene.frame_start)}) > end ({int(context_scene.frame_end)}).",
            )
            return

        frame_step = int(getattr(context_scene, "frame_step", 1) or 1)
        if frame_step < 1:
            pending_responses[task_id] = (
                False,
                f"Invalid frame step: {frame_step}. Must be >= 1.",
            )
            return

        if resolve_export_frame_range(ao) is None:
            pending_responses[task_id] = (
                False,
                f"No animation data found on '{armature_name}' or its constraint targets. Please add keyframes or NLA strips.",
            )
            return

        # --- End pre-export validations ---

        timeline_scene = _get_timeline_scene_for_armature(context_scene, ao)
        original_frame_start = int(context_scene.frame_start)
        original_frame_end = int(context_scene.frame_end)
        original_frame_step = int(getattr(context_scene, "frame_step", 1) or 1)
        changed_scene_timeline = timeline_scene is not context_scene

        if changed_scene_timeline:
            context_scene.frame_start = int(
                getattr(timeline_scene, "frame_start", original_frame_start)
            )
            context_scene.frame_end = int(
                getattr(timeline_scene, "frame_end", original_frame_end)
            )
            context_scene.frame_step = int(
                getattr(timeline_scene, "frame_step", original_frame_step) or 1
            )

        # Live preview deliberately samples dense/baked curves at 15 fps. A
        # normal import uses the selected sampling mode; this value is restored.
        if live_preview:
            preview_step = max(1, int(round(desired_fps / 15.0)))
            context_scene.frame_step = max(context_scene.frame_step, preview_step)

        serialized = None
        try:
            serialized = serialize(ao, target_bone_rest=target_bone_rest)
        finally:
            if changed_scene_timeline:
                context_scene.frame_start = original_frame_start
                context_scene.frame_end = original_frame_end
            if int(getattr(context_scene, "frame_step", 1) or 1) != original_frame_step:
                context_scene.frame_step = original_frame_step

        if not serialized:
            pending_responses[task_id] = (
                False,
                f"Failed to serialize animation for '{armature_name}'. Check if the armature has animation data or keyframes.",
            )
            return

        if not serialized.get("kfs") or len(serialized["kfs"]) == 0:
            pending_responses[task_id] = (
                False,
                f"No animation data found for '{armature_name}'. Please add keyframes or animation data to the armature.",
            )
            return

        # JSON has no NaN; one degenerate matrix used to fail the whole sync.
        serialized, replaced = sanitize_non_finite(serialized)
        if replaced:
            _add_export_warning(
                serialized,
                f"{replaced} invalid (NaN/infinite) values were replaced with 0. "
                "A bone may have zero scale or a broken constraint.",
            )

        if delta_base_hash is not None:
            calibration_key = json.dumps(target_bone_rest, sort_keys=True,
                                         separators=(",", ":")) if target_bone_rest else ""
            cache_key = (armature_name, calibration_key)
            previous = live_sync_snapshots.get(cache_key)
            current_hash = animation_revision(serialized)
            delta = None
            if previous and previous[1] == delta_base_hash:
                delta = create_delta(previous[0], serialized, previous[1], current_hash)
            _remember_snapshot(cache_key, serialized, current_hash)
            full_payload = {
                "type": "animation_full",
                "hash": current_hash,
                "animation": serialized,
            }
            full_encoded = json.dumps(full_payload, separators=(",", ":")).encode("utf-8")
            full_compressed = zlib.compress(full_encoded)
            if delta is not None:
                delta_encoded = json.dumps(delta, separators=(",", ":")).encode("utf-8")
                delta_compressed = zlib.compress(delta_encoded)
                compressed = delta_compressed if len(delta_compressed) < len(full_compressed) else full_compressed
            else:
                compressed = full_compressed
        else:
            encoded = json.dumps(serialized, separators=(",", ":"))
            compressed = zlib.compress(encoded.encode("utf-8"))

        pending_responses[task_id] = (True, compressed)

    except Exception as e:
        print(f"Error in main thread: {str(e)}")
        traceback.print_exc()
        pending_responses[task_id] = (False, f"Error during export: {str(e)}")


def _seconds_scale(export_info, scene_fps):
    """Factor that converts payload times to seconds."""
    if isinstance(export_info, dict) and export_info.get("time_unit") == "frames":
        try:
            source_fps = float(export_info.get("fps") or scene_fps)
        except (TypeError, ValueError):
            source_fps = scene_fps
        if source_fps > 0:
            return 1.0 / source_fps
    return 1.0


def _ensure_action_slot(action, ao):
    """Blender 4.4+ actions animate through slots."""
    if not hasattr(action, "slots"):
        return
    if action.slots:
        active_slot = action.slots[0]
    else:
        try:
            active_slot = action.slots.new(id_type="OBJECT", name=f"OB{ao.name}")
        except TypeError:
            active_slot = action.slots.new(id_type="OBJECT")
    if active_slot and hasattr(ao.animation_data, "action_slot"):
        try:
            ao.animation_data.action_slot = active_slot
        except Exception:
            pass


def _channelbag_for(action):
    channelbag = utils.get_action_channelbag(action)
    if channelbag is None or not hasattr(channelbag, "fcurves"):
        legacy_fcurves = getattr(action, "fcurves", None)
        if legacy_fcurves is None:
            raise RuntimeError("Unable to access animation channelbag for import")

        class _LegacyChannelbag:
            def __init__(self, fcurves, groups):
                self.fcurves = fcurves
                self.groups = groups

        channelbag = _LegacyChannelbag(legacy_fcurves, getattr(action, "groups", []))
    return channelbag


def _make_fcurve_factory(action, channelbag):
    group_collections = []
    action_groups = getattr(action, "groups", None)
    if action_groups is not None:
        group_collections.append(action_groups)
    channelbag_groups = getattr(channelbag, "groups", None)
    if channelbag_groups is not None and channelbag_groups is not action_groups:
        group_collections.append(channelbag_groups)

    def ensure_group(group_name):
        for group_collection in group_collections:
            if group_collection is None:
                continue
            try:
                existing = group_collection.get(group_name)
            except Exception:
                existing = None
            if existing is not None:
                return existing
            try:
                return group_collection.new(name=group_name)
            except TypeError:
                try:
                    return group_collection.new(group_name)
                except Exception:
                    continue
            except Exception:
                continue
        return None

    def create_fcurve(data_path, index, group_name):
        fcurve = None
        try:
            fcurve = channelbag.fcurves.new(data_path, index=index)
        except TypeError:
            for candidate in ("group_name", "action_group"):
                try:
                    fcurve = channelbag.fcurves.new(
                        data_path, index=index, **{candidate: group_name}
                    )
                    break
                except TypeError:
                    continue
            if fcurve is None:
                fcurve = channelbag.fcurves.new(data_path, index=index)
        group = ensure_group(group_name)
        if group is not None and hasattr(fcurve, "group"):
            try:
                fcurve.group = group
            except Exception:
                pass
        return fcurve

    return create_fcurve


def _import_markers(action, markers, start_frame, fps, seconds_scale):
    """Studio keyframe names and KeyframeMarkers become action pose markers."""
    if not isinstance(markers, list) or not markers:
        return 0
    created = 0
    exact = []
    for marker in markers:
        if not isinstance(marker, dict):
            continue
        name = str(marker.get("name") or "").strip()
        try:
            seconds = float(marker.get("time", 0.0)) * seconds_scale
        except (TypeError, ValueError):
            continue
        if not name:
            continue
        frame = int(round(start_frame + seconds * fps))
        value = str(marker.get("value") or "")
        marker_type = "Event" if marker.get("type") == "Event" else "Name"
        label = f"{name}: {value}" if value else name
        try:
            pose_marker = action.pose_markers.new(label)
            pose_marker.frame = frame
            created += 1
        except Exception:
            continue
        exact.append({"label": label, "frame": frame, "name": name, "value": value,
                      "type": marker_type, "time": seconds})
    if exact:
        action["rbx_markers"] = json.dumps(exact, separators=(",", ":"))
    return created


def _import_face_controls(ao, keyframes, create_fcurve, start_frame, fps):
    """Key the armature's face-control sliders from Studio NumberPose data.

    ``keyframes`` times must already be in seconds.
    """
    from ..animation.face_controls import face_control_property_name  # noqa: PLC0415
    from ..animation.import_easing import sample_face_control_tracks  # noqa: PLC0415

    holder = getattr(ao, "rbx_face_controls", None)
    tracks = sample_face_control_tracks(keyframes, fps)
    keyed, missing = 0, []
    for control_name, samples in tracks.items():
        prop = face_control_property_name(control_name)
        if holder is None or not hasattr(holder, prop):
            missing.append(control_name)
            continue
        fcurve = create_fcurve(f"rbx_face_controls.{prop}", 0, "Face Controls")
        fcurve.keyframe_points.add(len(samples))
        for index, (frame, value, constant) in enumerate(samples):
            point = fcurve.keyframe_points[index]
            point.co = (start_frame + frame, value)
            point.interpolation = "CONSTANT" if constant else "LINEAR"
        fcurve.keyframe_points.handles_recalc()
        keyed += 1
    return keyed, missing


def execute_import_animation(task_id, animation_data, target_armature=None):
    """Import a Studio animation into a NEW action on the armature.

    The previous action is kept (with a fake user, so saving can't drop it);
    drivers, NLA tracks, the scene FPS and the editor state are left alone.
    """
    try:
        # Use target armature if provided, otherwise fall back to scene selection
        if target_armature:
            armature_name = target_armature
        else:
            settings = getattr(bpy.context.scene, "rbx_anim_settings", None)
            armature_name = settings.rbx_anim_armature if settings else None

        if not armature_name:
            raise ValueError(
                "No armature specified for import. Please provide target armature or select one in the scene."
            )

        ao = get_object_by_name(armature_name)
        if not ao or ao.type != "ARMATURE":
            raise ValueError(
                f"Selected object '{armature_name}' is not a valid armature."
            )
        if ao.mode == "EDIT":
            raise ValueError(edit_mode_message(ao))
        if ao.animation_data and getattr(ao.animation_data, "use_tweak_mode", False):
            raise ValueError(
                f"'{ao.name}' is in NLA tweak mode. Exit tweak mode, then import again."
            )
        if not isinstance(animation_data.get("kfs"), list):
            raise ValueError("Animation data has no keyframes.")

        summary = {}

        def _do_import():
            scene = bpy.context.scene
            fps = get_scene_fps()
            export_info = animation_data.get("export_info", {}) or {}
            seconds_scale = _seconds_scale(export_info, fps)
            deform_translation_scale = _resolve_deform_translation_scale(export_info)
            is_deform_rig = bool(
                animation_data.get("is_deform_bone_rig", False)
                or animation_data.get("is_deform_rig", False)
            )
            start_frame = int(scene.frame_start)

            ad = ao.animation_data_create()
            previous_action = ad.action
            if previous_action is not None:
                # Keep the old animation in the file and in the action list.
                previous_action.use_fake_user = True
            previous_use_nla = bool(getattr(ad, "use_nla", True))

            action = bpy.data.actions.new(name=f"{ao.name}_ImportedAnimation")
            ad.action = action
            _ensure_action_slot(action, ao)

            # NLA strips would be layered under the pose we're converting.
            if previous_use_nla and getattr(ad, "nla_tracks", None):
                ad.use_nla = False

            try:
                # Rest pose for a clean, deterministic conversion.
                for bone in ao.pose.bones:
                    bone.matrix_basis = Matrix.Identity(4)
                bpy.context.view_layer.update()

                resolve = build_pose_bone_resolver(ao, is_deform_rig)

                # { bone_name: { frame: {"location", "rotation_quaternion", "scale", ...} } }
                all_bone_data = {}
                for bone in ao.pose.bones:
                    if is_deform_rig:
                        is_transformable = bone.bone.use_deform
                    else:
                        is_transformable = bool(bone.bone.get("is_transformable", False))
                    if is_transformable:
                        all_bone_data[bone.name] = {
                            start_frame: {
                                "location": bone.location.copy(),
                                "rotation_quaternion": bone.rotation_quaternion.copy(),
                                "scale": bone.scale.copy(),
                            }
                        }

                unmatched = set()
                matched = set()
                keyframes = [kf for kf in animation_data["kfs"] if isinstance(kf, dict)]
                for kf in keyframes:
                    if not isinstance(kf.get("kf"), dict):
                        kf["kf"] = {}
                    if seconds_scale != 1.0:
                        kf["t"] = float(kf.get("t", 0.0)) * seconds_scale

                for kf_data in sample_import_keyframes(keyframes, fps):
                    frame = start_frame + kf_data["t"] * fps
                    state = kf_data["kf"]

                    bones_to_process = []
                    for source_bone_name in state.keys():
                        if source_bone_name.endswith("_deform"):
                            continue
                        pose_bone = resolve(source_bone_name)
                        if pose_bone is None:
                            unmatched.add(source_bone_name)
                            continue
                        matched.add(pose_bone.name)
                        if (
                            is_deform_rig
                            and pose_bone.bone.get("rbx_is_deform_bone", False)
                            and "rbx_source_name" not in pose_bone.bone
                        ):
                            pose_bone.bone["rbx_source_name"] = source_bone_name
                        bones_to_process.append((pose_bone, source_bone_name))

                    bones_to_process.sort(key=lambda item: len(item[0].parent_recursive))

                    # Parents first, so a child's conversion sees its parent's
                    # pose for this frame.
                    for pose_bone, source_bone_name in bones_to_process:
                        bone_name = pose_bone.name
                        pose_bone.rotation_mode = "QUATERNION"
                        pose_data = state.get(source_bone_name)
                        if not pose_data:
                            continue

                        if bone_name not in all_bone_data:
                            all_bone_data[bone_name] = {
                                start_frame: {
                                    "location": pose_bone.location.copy(),
                                    "rotation_quaternion": pose_bone.rotation_quaternion.copy(),
                                    "scale": pose_bone.scale.copy(),
                                }
                            }

                        easing_style = "Linear"
                        cframe_components = None
                        if isinstance(pose_data, list) and len(pose_data) == 3 and isinstance(
                            pose_data[0], (list, tuple)
                        ):
                            cframe_components = pose_data[0]
                            easing_style = pose_data[1]
                        elif isinstance(pose_data, dict):
                            cframe_components = pose_data.get("components", [])
                            easing_style = pose_data.get("easingStyle", "Linear")
                        elif isinstance(pose_data, list):
                            cframe_components = pose_data

                        if not cframe_components or len(cframe_components) < 12:
                            continue

                        bone_transform = cf_to_mat(cframe_components)

                        joint_props = (
                            "nicetransform" in pose_bone.bone
                            and "transform" in pose_bone.bone
                            and "transform1" in pose_bone.bone
                        )
                        # A Motor6D bone that also deforms a skinned mesh is
                        # still a Motor6D channel: the exporter samples it with
                        # the Motor6D math, so the import must invert that.
                        is_deform_bone = is_deform_rig and bool(
                            pose_bone.bone.get("rbx_is_deform_bone", False)
                            or (pose_bone.bone.use_deform and not joint_props)
                        )
                        has_motor6d_props = (not is_deform_bone) and joint_props

                        if is_deform_bone:
                            # Imported Bone deltas use the Roblox rest basis. A
                            # rebuilt display bone may point along a different axis;
                            # conjugate by nicetransform to preserve the same motion.
                            if pose_bone.bone.get("rbx_joint_type") == "Bone" and "transform" in pose_bone.bone:
                                basis = bone_transform.copy()
                                if deform_translation_scale != 1.0:
                                    basis.translation = basis.to_translation() * float(deform_translation_scale)
                                nice = utils.to_matrix(pose_bone.bone.get("nicetransform"))
                                basis = nice.inverted() @ basis @ nice
                                pose_bone.matrix_basis = basis
                            else:
                                pose_bone.matrix_basis = _roblox_deform_delta_to_blender(
                                    bone_transform,
                                    deform_translation_scale,
                                )
                        elif not has_motor6d_props:
                            # Helper bones without Motor6D metadata retain the legacy
                            # object-axis conversion.
                            pose_bone.matrix_basis = (
                                transform_to_blender
                                @ bone_transform
                                @ transform_to_blender.inverted()
                            )
                        else:  # Motor6D rig
                            back_trans = transform_to_blender.inverted()
                            extr_transform = Matrix(pose_bone.bone["nicetransform"]).inverted()

                            orig_mat = Matrix(pose_bone.bone["transform"])
                            orig_mat_tr1 = Matrix(pose_bone.bone["transform1"])

                            if pose_bone.parent and "transform" in pose_bone.parent.bone:
                                parent_orig_mat = Matrix(pose_bone.parent.bone["transform"])
                                parent_orig_mat_tr1 = Matrix(
                                    pose_bone.parent.bone["transform1"]
                                )

                                orig_base_mat = back_trans @ (orig_mat @ orig_mat_tr1)
                                parent_orig_base_mat = back_trans @ (
                                    parent_orig_mat @ parent_orig_mat_tr1
                                )
                                orig_transform = parent_orig_base_mat.inverted() @ orig_base_mat

                                cur_transform = orig_transform @ bone_transform

                                parent_extr_transform = Matrix(
                                    pose_bone.parent.bone["nicetransform"]
                                ).inverted()

                                parent_matrix = pose_bone.parent.matrix
                                parent_obj_transform = back_trans @ (
                                    parent_matrix @ parent_extr_transform
                                )

                                cur_obj_transform = parent_obj_transform @ cur_transform
                            else:
                                cur_obj_transform = bone_transform

                            final_matrix = (
                                transform_to_blender
                                @ cur_obj_transform
                                @ extr_transform.inverted()
                            )
                            pose_bone.matrix = final_matrix

                        if bone_name in all_bone_data:
                            all_bone_data[bone_name][frame] = {
                                "location": pose_bone.location.copy(),
                                "rotation_quaternion": pose_bone.rotation_quaternion.copy(),
                                "scale": pose_bone.scale.copy(),
                                "easingStyle": easing_style,
                            }

                channelbag = _channelbag_for(action)
                create_fcurve = _make_fcurve_factory(action, channelbag)

                for bone_name, frame_data in all_bone_data.items():
                    sorted_frames = sorted(frame_data.keys())
                    # Blender interpolates quaternion components; keep adjacent signs
                    # aligned so equivalent q/-q values cannot introduce a flip.
                    for previous, current in zip(sorted_frames, sorted_frames[1:]):
                        a = frame_data[previous]["rotation_quaternion"]
                        b = frame_data[current]["rotation_quaternion"]
                        if a.dot(b) < 0:
                            b.negate()

                    escaped = bpy.utils.escape_identifier(bone_name)
                    loc_fcurves = [
                        create_fcurve(f'pose.bones["{escaped}"].location', i, bone_name)
                        for i in range(3)
                    ]
                    rot_fcurves = [
                        create_fcurve(f'pose.bones["{escaped}"].rotation_quaternion', i, bone_name)
                        for i in range(4)
                    ]
                    scale_fcurves = [
                        create_fcurve(f'pose.bones["{escaped}"].scale', i, bone_name)
                        for i in range(3)
                    ]

                    num_frames = len(sorted_frames)
                    if num_frames == 0:
                        continue

                    for fcurve in loc_fcurves + rot_fcurves + scale_fcurves:
                        fcurve.keyframe_points.add(num_frames)

                    for idx, frame in enumerate(sorted_frames):
                        transforms = frame_data[frame]
                        loc = transforms["location"]
                        rot = transforms["rotation_quaternion"]
                        scl = transforms["scale"]
                        interpolation = (
                            "CONSTANT" if transforms.get("easingStyle") == "Constant"
                            else "LINEAR"
                        )
                        for fcurves, values, count in (
                            (loc_fcurves, loc, 3),
                            (rot_fcurves, rot, 4),
                            (scale_fcurves, scl, 3),
                        ):
                            for axis in range(count):
                                kp = fcurves[axis].keyframe_points[idx]
                                kp.co = (frame, values[axis])
                                kp.interpolation = interpolation
                                kp.handle_left_type = kp.handle_right_type = "AUTO"

                    # Keys were allocated in sorted order and only use LINEAR or
                    # CONSTANT interpolation. FCurve.update() would deduplicate close
                    # fractional keys and erase immediate Constant In jumps.
                    for fcurve in loc_fcurves + rot_fcurves + scale_fcurves:
                        fcurve.keyframe_points.handles_recalc()

                face_keyed, face_missing = _import_face_controls(
                    ao, keyframes, create_fcurve, start_frame, fps
                )
                marker_count = _import_markers(
                    action, animation_data.get("markers"), start_frame, fps, seconds_scale
                )

                # Make the whole animation visible on the timeline.
                try:
                    duration = float(animation_data.get("t", 0.0)) * seconds_scale
                except (TypeError, ValueError):
                    duration = 0.0
                end_frame = start_frame + math.ceil(round(duration * fps, 6))
                extended_range = end_frame > scene.frame_end
                if extended_range:
                    scene.frame_end = end_frame

                driven = sorted(
                    name for name in matched
                    if name in serialization_driven_bones(ao)
                )
                summary.update({
                    "action": action.name,
                    "previous_action": previous_action.name if previous_action else None,
                    "bones": len(matched),
                    "unmatched": sorted(unmatched),
                    "face_controls": face_keyed,
                    "face_missing": face_missing,
                    "markers": marker_count,
                    "extended_range": end_frame if extended_range else None,
                    "driven": driven,
                })
            finally:
                if previous_use_nla and hasattr(ad, "use_nla"):
                    ad.use_nla = True

        # Run import wrapped with IK preservation (FK import then bake back to
        # IK). It needs the armature active in Pose Mode; the editor state is
        # restored afterwards.
        with preserved_editor_state():
            bpy.context.view_layer.objects.active = ao
            if ao.mode != "POSE":
                bpy.ops.object.mode_set(mode="POSE")
            import_animation_preserve_ik(_do_import)

        pending_responses[task_id] = (True, _import_summary_message(summary))

    except Exception as e:
        print(f"Error in main thread (import_animation): {str(e)}")
        traceback.print_exc()
        pending_responses[task_id] = (False, f"Error importing animation: {str(e)}")


def serialization_driven_bones(ao):
    from ..animation.serialization import get_all_driven_bones  # noqa: PLC0415

    return get_all_driven_bones(ao)


def _import_summary_message(summary):
    parts = [f"Imported {summary.get('bones', 0)} bone(s) into action '{summary.get('action')}'."]
    if summary.get("previous_action"):
        parts.append(f"The previous action '{summary['previous_action']}' was kept.")
    unmatched = summary.get("unmatched") or []
    if unmatched:
        shown = ", ".join(unmatched[:8]) + (f" (+{len(unmatched) - 8} more)" if len(unmatched) > 8 else "")
        parts.append(f"{len(unmatched)} channel(s) have no matching bone: {shown}.")
    if summary.get("face_controls"):
        parts.append(f"Keyed {summary['face_controls']} face control(s).")
    if summary.get("face_missing"):
        parts.append(
            f"{len(summary['face_missing'])} face control(s) have no slider on this rig."
        )
    if summary.get("markers"):
        parts.append(f"Added {summary['markers']} marker(s).")
    if summary.get("extended_range"):
        parts.append(f"Extended the scene end to frame {summary['extended_range']}.")
    if summary.get("driven"):
        parts.append(
            f"{len(summary['driven'])} bone(s) have drivers that override the imported keys: "
            + ", ".join(summary["driven"][:5])
            + "."
        )
    return " ".join(parts)


def execute_get_bone_rest(task_id, armature_name):
    """
    Gets the rest pose for all bones in an armature.

    Read-only: it uses rest matrices and the stored Roblox joint data, so it
    never changes the mode, the pose or the bone selection. It uses the same
    rest calculation as the deform bone serializer, so the rig setup and the
    animation export agree.
    """
    try:
        if not armature_name:
            pending_responses[task_id] = (False, "No armature selected")
            return

        ao = get_object_by_name(armature_name)
        if not ao or ao.type != "ARMATURE":
            pending_responses[task_id] = (
                False,
                f"Object '{armature_name}' is not a valid armature.",
            )
            return

        from ..core.constants import get_transform_to_blender

        back_trans = get_transform_to_blender().inverted()
        world_transform = back_trans @ ao.matrix_world

        bone_poses = {}
        rest_transform_cache = {}

        # Parents before children so parent-relative transforms can be built.
        sorted_bones = sorted(ao.data.bones, key=lambda b: len(b.parent_recursive))

        def components(matrix):
            translation = matrix.to_translation()
            return [
                translation.x, translation.y, translation.z,
                matrix[0][0], matrix[0][1], matrix[0][2],
                matrix[1][0], matrix[1][1], matrix[1][2],
                matrix[2][0], matrix[2][1], matrix[2][2],
            ]

        for bone in sorted_bones:
            # Animation controls never become Studio bones or joints.
            if utils.is_control_bone(bone):
                continue
            is_imported_deform_bone = bool(bone.get("rbx_is_deform_bone", False))
            has_motor6d_props = (not is_imported_deform_bone) and (
                "transform" in bone
                and "transform1" in bone
                and "nicetransform" in bone
            )

            if has_motor6d_props:
                # Keep sync-bones aligned with the Motor6D serializer by rebuilding
                # the rest joint frame from the stored Roblox-space transforms.
                orig_mat = Matrix(bone["transform"])
                orig_mat_tr1 = Matrix(bone["transform1"])
                rest_obj_transform = back_trans @ (orig_mat @ orig_mat_tr1)
            else:
                # New/deform bones don't have stored Motor6D metadata, so derive the
                # rest frame from the actual Blender rest pose in object space.
                rest_obj_transform = world_transform @ bone.matrix_local

            rest_transform_cache[bone.name] = rest_obj_transform

            reported_parent_name = _get_reported_bone_parent_name(bone)
            rest_local_transform = rest_obj_transform
            if reported_parent_name:
                parent_rest_transform = rest_transform_cache.get(reported_parent_name)
                if parent_rest_transform is not None:
                    try:
                        rest_local_transform = parent_rest_transform.inverted() @ rest_obj_transform
                    except ValueError:
                        rest_local_transform = rest_obj_transform

            bone_poses[bone.name] = {
                "world": components(rest_obj_transform),
                "relative": components(rest_local_transform),
                "parent": reported_parent_name,
                "is_synthetic_helper": bone.get("is_synthetic_helper", False),
                "source_name": bone.get("rbx_source_name", bone.name),
            }

        response = {"armature": armature_name, "bone_poses": bone_poses}
        pending_responses[task_id] = (True, json.dumps(response).encode("utf-8"))

    except Exception as e:
        traceback.print_exc()
        pending_responses[task_id] = (False, f"Error getting bone rest poses: {str(e)}")
