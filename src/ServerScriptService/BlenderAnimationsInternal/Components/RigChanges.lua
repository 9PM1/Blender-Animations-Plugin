--!strict

--[=[
	Joint changes Blender queues for the Studio rig ("Write to Studio Rig" on
	a moved pivot). They come with an animation as `rig_changes`.

	Each entry moves one Motor6D, between the parts named `part0` (the
	parent) and `part1`, so it turns about a new pivot: C0 and C1 become
	`c0` and `c1`, the old values times the same `offset`, so the parts stay
	where they are. `c0` is the parent side, so a reversed joint (Part0 is
	`part1`) takes them swapped. An entry whose joint already has the new
	values is skipped; one whose joint has neither the new nor the
	`previous_*` values is a conflict (the rig changed after it went to
	Blender) and nothing is written.

	Playback: the poses that come with a change are relative to the old
	joint. Once the joint has the new values, its poses are converted to
	`offset:Inverse() * pose * offset` (`convertPoses`), so that same export
	plays right at once. Studio then confirms the joints to Blender, which
	rebases the bones: later exports are relative to the new joints and no
	longer list them. Joints are only written on the Blender sync paths; an
	animation loaded any other way only converts the poses of joints that
	already have their change.
]=]

local Undo = require(script.Parent.Undo)

local RigChanges = {}

RigChanges.UNDO_NAME = "Apply pivot changes from Blender"

local POSITION_TOLERANCE = 1e-4
local ROTATION_TOLERANCE = 1e-4

export type Change = {
	part0: string,
	part1: string,
	c0: CFrame,
	c1: CFrame,
	previousC0: CFrame,
	previousC1: CFrame,
	offset: CFrame,
	-- The entry as Blender sent it, sent back when confirming.
	entry: any,
}

export type Outcome = {
	change: Change,
	-- "applied": written now; "unchanged": the joint had the new values;
	-- "waiting": not a sync load, the joint still has the old values;
	-- "conflict", "missing", "ambiguous", "failed": nothing changed.
	status: string,
	joint: Motor6D?,
	reversed: boolean,
}

export type Result = {
	outcomes: { Outcome },
	-- part1 -> offset, for every joint that has its change.
	offsets: { [string]: CFrame },
	problems: { string },
}

local function toCFrame(components: any): CFrame?
	if type(components) ~= "table" or #components < 12 then
		return nil
	end
	for index = 1, 12 do
		local value = components[index]
		if type(value) ~= "number" or value ~= value then
			return nil
		end
	end
	return CFrame.new(table.unpack(components, 1, 12))
end

local function near(left: CFrame, right: CFrame): boolean
	local a = { left:GetComponents() }
	local b = { right:GetComponents() }
	local positionTolerance = POSITION_TOLERANCE * math.max(1, left.Position.Magnitude / 10)
	for index = 1, 12 do
		local tolerance = if index <= 3 then positionTolerance else ROTATION_TOLERANCE
		if math.abs(a[index] - b[index]) > tolerance then
			return false
		end
	end
	return true
end

-- The joint's parent-side and child-side offsets, whichever way round it is.
local function sides(joint: Motor6D, reversed: boolean): (CFrame, CFrame)
	if reversed then
		return joint.C1, joint.C0
	end
	return joint.C0, joint.C1
end

function RigChanges.parse(entries: any): ({ Change }, { string })
	local changes: { Change } = {}
	local problems: { string } = {}
	if type(entries) ~= "table" then
		return changes, problems
	end
	for _, entry in ipairs(entries) do
		if type(entry) ~= "table" or entry.type ~= "joint_pivot" then
			local kind = if type(entry) == "table" then tostring(entry.type) else type(entry)
			table.insert(problems, string.format("Blender sent a rig change this plugin doesn't know (%s). Update the plugin.", kind))
			continue
		end
		local c0, c1 = toCFrame(entry.c0), toCFrame(entry.c1)
		local previousC0, previousC1 = toCFrame(entry.previous_c0), toCFrame(entry.previous_c1)
		local offset = toCFrame(entry.offset)
		if type(entry.part0) ~= "string" or type(entry.part1) ~= "string" or not (c0 and c1 and previousC0 and previousC1 and offset) then
			table.insert(problems, string.format("Blender sent a joint change for %s that Studio couldn't read; it was skipped.", tostring(entry.part1)))
			continue
		end
		table.insert(changes, {
			part0 = entry.part0,
			part1 = entry.part1,
			c0 = c0 :: CFrame,
			c1 = c1 :: CFrame,
			previousC0 = previousC0 :: CFrame,
			previousC1 = previousC1 :: CFrame,
			offset = offset :: CFrame,
			entry = entry,
		})
	end
	return changes, problems
end

-- Motor6Ds by "Part0 name / Part1 name".
local function indexJoints(model: Instance): { [string]: { Motor6D } }
	local index: { [string]: { Motor6D } } = {}
	for _, descendant in ipairs(model:GetDescendants()) do
		if descendant:IsA("Motor6D") then
			local joint = descendant :: Motor6D
			local part0, part1 = joint.Part0, joint.Part1
			if part0 and part1 then
				local key = part0.Name .. "\0" .. part1.Name
				index[key] = index[key] or {}
				table.insert(index[key], joint)
			end
		end
	end
	return index
end

-- The Motor6D between the two parts, and whether it is reversed.
local function findJoint(index: { [string]: { Motor6D } }, rig: any?, change: Change): (Motor6D?, boolean, string?)
	local candidates = {}
	for _, joint in ipairs(index[change.part0 .. "\0" .. change.part1] or {}) do
		table.insert(candidates, { joint = joint, reversed = false })
	end
	if change.part0 ~= change.part1 then
		for _, joint in ipairs(index[change.part1 .. "\0" .. change.part0] or {}) do
			table.insert(candidates, { joint = joint, reversed = true })
		end
	end
	if #candidates == 0 then
		return nil, false, "missing"
	end
	if #candidates > 1 then
		-- Same-name parts: the joint the rig animates as part1's channel.
		local rigPart = rig and rig.bones and rig.bones[change.part1]
		local channelJoint = rigPart and rigPart.joint
		for _, candidate in ipairs(candidates) do
			if candidate.joint == channelJoint then
				return candidate.joint, candidate.reversed, nil
			end
		end
		return nil, false, "ambiguous"
	end
	return candidates[1].joint, candidates[1].reversed, nil
end

--[=[
	Check each change against the rig's joints and, when `write` is set,
	write the ones that still have the previous values (one undo step).
	`rig` (a Rig) picks between same-name joints; it may be nil.
]=]
function RigChanges.apply(model: Instance?, rig: any?, entries: any, write: boolean): Result
	local changes, problems = RigChanges.parse(entries)
	local result: Result = { outcomes = {}, offsets = {}, problems = problems }
	if #changes == 0 then
		return result
	end
	if not model then
		table.insert(result.problems, "Blender sent joint changes for the rig, but no rig is selected.")
		return result
	end

	local index = indexJoints(model)
	local toWrite: { Outcome } = {}
	for _, change in ipairs(changes) do
		local joint, reversed, reason = findJoint(index, rig, change)
		local outcome: Outcome = { change = change, joint = joint, reversed = reversed, status = reason or "conflict" }
		if joint then
			local parentSide, childSide = sides(joint, reversed)
			if near(parentSide, change.c0) and near(childSide, change.c1) then
				outcome.status = "unchanged"
			elseif near(parentSide, change.previousC0) and near(childSide, change.previousC1) then
				outcome.status = if write then "applied" else "waiting"
				if write then
					table.insert(toWrite, outcome)
				end
			end
		end
		table.insert(result.outcomes, outcome)
	end

	if #toWrite > 0 then
		local ok, err = pcall(function()
			Undo.record(RigChanges.UNDO_NAME, function()
				for _, outcome in ipairs(toWrite) do
					local joint = outcome.joint :: Motor6D
					local change = outcome.change
					if outcome.reversed then
						joint.C0, joint.C1 = change.c1, change.c0
					else
						joint.C0, joint.C1 = change.c0, change.c1
					end
				end
			end)
		end)
		if not ok then
			-- The recording was cancelled, which puts the old values back.
			for _, outcome in ipairs(toWrite) do
				outcome.status = "failed"
			end
			table.insert(result.problems, "Couldn't change the joints Blender sent: " .. tostring(err))
		end
	end

	for _, outcome in ipairs(result.outcomes) do
		if outcome.status == "applied" or outcome.status == "unchanged" then
			result.offsets[outcome.change.part1] = outcome.change.offset
		end
	end
	return result
end

-- The outcomes to confirm to Blender: the joints that have their change.
function RigChanges.toConfirm(result: Result): { Outcome }
	local confirm = {}
	for _, outcome in ipairs(result.outcomes) do
		if outcome.status == "applied" or outcome.status == "unchanged" then
			table.insert(confirm, outcome)
		end
	end
	return confirm
end

-- Whether a joint is back on the values a change replaced (an undo).
function RigChanges.isUndone(outcome: Outcome): boolean
	local joint = outcome.joint
	if not joint or not joint.Parent then
		return false
	end
	local parentSide, childSide = sides(joint, outcome.reversed)
	local change = outcome.change
	return near(parentSide, change.previousC0) and near(childSide, change.previousC1)
		and not (near(parentSide, change.c0) and near(childSide, change.c1))
end

local function convertPose(pose: any, offset: CFrame, inverse: CFrame): boolean
	if type(pose) ~= "table" then
		return false
	end
	-- [components, style, direction], { components = ... } or the flat list.
	local components = pose
	if type(pose[1]) == "table" then
		components = pose[1]
	elseif type(pose.components) == "table" then
		components = pose.components
	end
	local cframe = toCFrame(components)
	if not cframe then
		return false
	end
	local converted = { (inverse * cframe * offset):GetComponents() }
	for index = 1, 12 do
		components[index] = converted[index]
	end
	return true
end

-- Convert the poses of joints that have their change, in place. Returns
-- how many poses changed.
function RigChanges.convertKeyframes(keyframes: any, offsets: { [string]: CFrame }): number
	if type(keyframes) ~= "table" or next(offsets) == nil then
		return 0
	end
	local inverses = {}
	for name, offset in pairs(offsets) do
		inverses[name] = offset:Inverse()
	end
	local count = 0
	for _, keyframe in ipairs(keyframes) do
		local poses = if type(keyframe) == "table" then keyframe.kf else nil
		if type(poses) == "table" then
			for name, offset in pairs(offsets) do
				if convertPose(poses[name], offset, inverses[name]) then
					count += 1
				end
			end
		end
	end
	return count
end

function RigChanges.convertPoses(animData: any, offsets: { [string]: CFrame }): number
	return RigChanges.convertKeyframes(if type(animData) == "table" then animData.kfs else nil, offsets)
end

local function jointLabel(outcome: Outcome): string
	local joint = outcome.joint
	return if joint then string.format("%s joint (%s)", outcome.change.part1, joint.Name) else outcome.change.part1 .. " joint"
end

-- What to tell the user about one change, or nil when there's nothing to say.
function RigChanges.describe(outcome: Outcome): string?
	local change = outcome.change
	if outcome.status == "applied" then
		return string.format("Moved the %s to the pivot set in Blender; the parts stay put. Ctrl+Z undoes it.", jointLabel(outcome))
	elseif outcome.status == "conflict" then
		return string.format(
			"Didn't move the %s to Blender's pivot: its C0/C1 changed after the rig went to Blender. Import the rig into Blender again, or keep that pivot in Blender only.",
			jointLabel(outcome)
		)
	elseif outcome.status == "missing" then
		return string.format(
			"Blender moved the pivot of %s, but this rig has no Motor6D between %s and %s.",
			change.part1,
			change.part0,
			change.part1
		)
	elseif outcome.status == "ambiguous" then
		return string.format(
			"Blender moved the pivot of %s, but several Motor6Ds join %s and %s. Rename the duplicate parts.",
			change.part1,
			change.part0,
			change.part1
		)
	end
	return nil
end

function RigChanges.describeUndone(outcome: Outcome): string
	return string.format(
		"The %s is back on its old pivot (undone?), but Blender now animates %s about the new one, so it plays wrong here. Redo (Ctrl+Y) to match Blender.",
		jointLabel(outcome),
		outcome.change.part1
	)
end

return RigChanges
