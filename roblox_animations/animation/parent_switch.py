"""
Keyable parent switching for bones Roblox can't really unparent.

A Motor6D child always follows its parent in Roblox. World-Space Unparent frees
a bone for a whole animation: it's unparented in Blender and the exporter
writes its pose relative to the original parent on every frame. Parent
switching makes that keyable, for throws, catches, a weapon handed between
hands, or a torso that's only separate for part of a move.

A switching bone is unparented the same way, and a Child Of constraint to the
original parent stands in for the parent link. Its inverse is the parent's
rest, so at influence 1 the bone moves exactly as the child it was; at 0 it's
free in armature space. That influence is the keyable "Follow parent" value.

On export a switching bone keeps its own sparse keys where it follows. Where
it doesn't, and on the frame either side of each switch, it's baked every
frame against the original parent, which reproduces the world placement
Blender shows. That compensation comes from this animation's parent motion
only: an animation layered on top in-game that moves the parent still moves
the bone.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

from ..core.constants import identity_cf
from ..core.utils import get_action_fcurves
from .planning import FRAME_KEY_PRECISION, frame_in_set

FOLLOW_CONSTRAINT = "RBX Follow Parent"
PARENT_SWITCH_MARKER = "ParentSwitch"
# Anything short of full influence blends toward free, so it's baked.
_FULL_FOLLOW = 1.0 - 1e-4


def original_parent_name(pose_bone) -> str:
    """The parent a world-space bone's Roblox pose is relative to, or ''."""
    bone = pose_bone.bone
    if not bone.get("worldspace_bone"):
        return ""
    parent = bone.get("worldspace_original_parent", "")
    return parent if isinstance(parent, str) else ""


def is_follow_constraint(pose_bone, constraint) -> bool:
    """A Child Of to the bone's original parent stands in for its parent link."""
    parent = original_parent_name(pose_bone)
    return (
        bool(parent)
        and constraint.type == "CHILD_OF"
        and constraint.target == pose_bone.id_data
        and constraint.subtarget == parent
    )


def follow_constraint(pose_bone):
    """The bone's working follow-parent constraint, or None.

    A world-space bone without one (or with it muted) is free for the whole
    animation, as World-Space Unparent leaves it.
    """
    for constraint in pose_bone.constraints:
        if not constraint.mute and is_follow_constraint(pose_bone, constraint):
            return constraint
    return None


def can_switch_parent(pose_bone) -> bool:
    """A bone with a parent link to switch: parented, or already world-space."""
    return bool(original_parent_name(pose_bone) or pose_bone.bone.parent)


def follows_parent(pose_bone) -> bool:
    """Whether the bone follows its parent at the current frame."""
    constraint = follow_constraint(pose_bone)
    if constraint is None:
        # A parented bone always does; a static world-space bone never does.
        return not original_parent_name(pose_bone)
    return constraint.influence >= 0.5


def add_follow_constraint(armature, pose_bone, parent_name, influence):
    """Stand a follow constraint in for the bone's link to parent_name.

    A bone's Child Of works in armature space, so the parent's rest inverse
    reproduces the parent link whatever the armature object's transform: at
    influence 1 the bone moves exactly as the parent's child.
    """
    constraint = pose_bone.constraints.new("CHILD_OF")
    constraint.name = FOLLOW_CONSTRAINT
    constraint.target = armature
    constraint.subtarget = parent_name
    constraint.inverse_matrix = armature.data.bones[parent_name].matrix_local.inverted()
    constraint.set_inverse_pending = False
    constraint.influence = influence
    # Parenting applies before the bone's own constraints; so does this.
    pose_bone.constraints.move(len(pose_bone.constraints) - 1, 0)
    return constraint


def remove_follow_constraints(armature, pose_bone) -> None:
    """Drop a bone's follow constraints and their keyed values."""
    animation = armature.animation_data
    action = animation.action if animation else None
    for constraint in list(pose_bone.constraints):
        if not is_follow_constraint(pose_bone, constraint):
            continue
        if action is not None:
            path = constraint.path_from_id("influence")
            try:
                curves = get_action_fcurves(action, slot=getattr(animation, "action_slot", None))
            except RuntimeError:
                curves = []
            for curve in [curve for curve in curves if curve.data_path == path]:
                curves.remove(curve)
        pose_bone.constraints.remove(constraint)


def keep_follow_constraints(armature, moved) -> None:
    """Keep follow constraints reproducing parent links whose rest moved.

    ``moved`` maps bone names to (old rest, new rest) after an Edit Mode change.
    """
    for pose_bone in armature.pose.bones:
        for constraint in pose_bone.constraints:
            if not is_follow_constraint(pose_bone, constraint):
                continue
            rests = moved.get(constraint.subtarget)
            if rests is None:
                continue
            old_rest, new_rest = rests
            try:
                # A bone's Child Of inverse is in armature space.
                constraint.inverse_matrix = new_rest.inverted() @ old_rest @ constraint.inverse_matrix
            except ValueError:
                continue
            constraint.set_inverse_pending = False


def follow_curve(constraint, action, slot=None):
    """The F-Curve keying a follow constraint's influence, if it has one."""
    if action is None:
        return None
    path = constraint.path_from_id("influence")
    try:
        curves = get_action_fcurves(action, slot=slot)
    except RuntimeError:
        return None
    for curve in curves:
        if curve.data_path == path:
            return None if curve.mute else curve
    return None


def follow_value(constraint, curve, frame) -> float:
    """The follow value at a frame: 1 follows the parent, 0 is free."""
    value = constraint.influence if curve is None else curve.evaluate(frame)
    return min(1.0, max(0.0, float(value)))


def rig_root(bone):
    """The top of a data bone's original-parent chain: the rig's root part."""
    bones = bone.id_data.bones
    seen = set()
    while bone.name not in seen:
        seen.add(bone.name)
        original = bone.get("worldspace_original_parent") if bone.get("worldspace_bone") else None
        parent = bones.get(original) if original else bone.parent
        if parent is None:
            break
        bone = parent
    return bone


@dataclass
class SwitchPlan:
    """Where one switching bone is baked, and where it changes parent."""

    # Every frame that doesn't fully follow, plus the frame before each switch.
    dense: Set[float] = field(default_factory=set)
    # The first following frame after a free run: a key in the bone's own easing.
    resume: Set[float] = field(default_factory=set)
    # (frame, follows) where the follow state flips, and the first frame when
    # the bone starts free.
    events: List[Tuple[float, bool]] = field(default_factory=list)


def switch_plan(constraint, action, slot, frame_start, frame_end) -> SwitchPlan:
    """Plan one switching bone over the scene range (whole frames and follow keys)."""
    curve = follow_curve(constraint, action, slot)
    frames = {float(frame) for frame in range(frame_start, frame_end + 1)}
    if curve is not None:
        frames.update(
            round(float(point.co.x), FRAME_KEY_PRECISION)
            for point in curve.keyframe_points
            if frame_start <= point.co.x <= frame_end
        )
    plan = SwitchPlan()
    previous = None  # (frame, free, follows)
    for frame in sorted(frames):
        value = follow_value(constraint, curve, frame)
        free = value < _FULL_FOLLOW
        follows = value >= 0.5
        if free:
            plan.dense.add(frame)
            if previous is not None and not previous[1]:
                plan.dense.add(previous[0])
        elif previous is not None and previous[1]:
            plan.resume.add(frame)
        if previous is None:
            if not follows:
                plan.events.append((frame, False))
        elif follows != previous[2]:
            plan.events.append((frame, follows))
        previous = (frame, free, follows)
    return plan


def mark_switch_holds(collected, plans: Dict[str, SwitchPlan], sample, frame_start, fps) -> None:
    """Make baked switch samples Constant where Blender holds and then jumps.

    Baked switch frames are emitted Linear. As the visual bake does, probe
    just before each next sample, once per changing interval, so a stepped
    parent stays stepped instead of ramping across a frame.
    """
    rows = sorted(collected, key=lambda key: key.time)
    for name, plan in plans.items():
        track = [
            (round(frame_start + key.time * fps, FRAME_KEY_PRECISION), key.poses[name])
            for key in rows
            if name in key.poses
        ]
        for (start, left), (end, right) in zip(track, track[1:]):
            if not frame_in_set(plan.dense, start):
                continue
            change = max(abs(a - b) for a, b in zip(left.components, right.components))
            if change <= 2e-6:
                continue
            probe = sample(start + (end - start) * 0.999, {name}).get(name, identity_cf)
            tolerance = max(1e-8, min(2e-6, change * 1e-5))
            if max(abs(a - b) for a, b in zip(probe, left.components)) <= tolerance:
                left.style = "Constant"
