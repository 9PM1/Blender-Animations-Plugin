"""Revisioned, deterministic animation keyframe patches for live sync."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any


_STRUCTURAL_KEYS = (
    "t",
    "is_deform_bone_rig",
    "is_deform_rig",
    "bone_hierarchy",
    "export_info",
    "rig_changes",
)


def sanitize_non_finite(value: Any) -> tuple[Any, int]:
    """Replace NaN/inf floats with 0.0. Returns (clean value, replacements).

    JSON has no NaN, and Studio's decoder rejects it, so one degenerate matrix
    used to fail the whole sync.
    """
    if isinstance(value, float):
        return (value, 0) if math.isfinite(value) else (0.0, 1)
    if isinstance(value, dict):
        replaced = 0
        clean = {}
        for key, item in value.items():
            clean[key], count = sanitize_non_finite(item)
            replaced += count
        return clean, replaced
    if isinstance(value, (list, tuple)):
        replaced = 0
        clean_list = []
        for item in value:
            cleaned, count = sanitize_non_finite(item)
            clean_list.append(cleaned)
            replaced += count
        return clean_list, replaced
    return value, 0


def animation_revision(animation: dict[str, Any]) -> str:
    """Return a stable revision for an already serialized animation payload."""
    encoded = json.dumps(animation, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _time_key(keyframe: dict[str, Any]) -> str:
    return format(float(keyframe["t"]), ".9g")


def create_delta(
    previous: dict[str, Any],
    current: dict[str, Any],
    previous_revision: str | None = None,
    current_revision: str | None = None,
) -> dict[str, Any] | None:
    """Create a replace/remove-keyframe patch, or ``None`` for a full sync.

    Whole keyframes are the smallest safe initial unit: they preserve pose,
    easing, deform markers, and face-control state without a lossy translation.
    Pass precomputed revisions to avoid re-hashing large payloads.
    """
    if any(previous.get(key) != current.get(key) for key in _STRUCTURAL_KEYS):
        return None
    old_list = previous.get("kfs", [])
    new_list = current.get("kfs", [])
    old_frames = {_time_key(frame): frame for frame in old_list}
    new_frames = {_time_key(frame): frame for frame in new_list}
    if len(old_frames) != len(old_list) or len(new_frames) != len(new_list):
        # Two keyframes share a time key; a patch could not express both.
        return None
    upsert = [frame for key, frame in new_frames.items() if old_frames.get(key) != frame]
    remove = [float(old_frames[key]["t"]) for key in old_frames.keys() - new_frames.keys()]
    return {
        "type": "animation_delta",
        "base_hash": previous_revision or animation_revision(previous),
        "hash": current_revision or animation_revision(current),
        "upsert": sorted(upsert, key=lambda frame: float(frame["t"])),
        "remove": sorted(remove),
    }


def apply_delta(animation: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any] | None:
    """Apply a validated patch to a serialized payload; ``None`` means resync."""
    if animation_revision(animation) != delta.get("base_hash"):
        return None
    frames = {_time_key(frame): frame for frame in animation.get("kfs", [])}
    for time_value in delta.get("remove", []):
        frames.pop(format(float(time_value), ".9g"), None)
    for frame in delta.get("upsert", []):
        if not isinstance(frame, dict) or "t" not in frame:
            return None
        frames[_time_key(frame)] = frame
    patched = dict(animation)
    patched["kfs"] = sorted(frames.values(), key=lambda frame: float(frame["t"]))
    return patched if animation_revision(patched) == delta.get("hash") else None
