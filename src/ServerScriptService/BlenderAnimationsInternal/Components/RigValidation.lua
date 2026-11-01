--!strict

--[=[
	Rig checks that run when a rig is selected, before any animation work.

	Everything here reads the rig and returns plain data or messages, so the
	UI can explain problems up front instead of failing later (after Blender
	has already baked and sent an animation).
]=]

local RigValidation = {}

local function hasAnimator(model: Instance): boolean
	return model:FindFirstChildWhichIsA("Humanoid") ~= nil
		or model:FindFirstChildWhichIsA("AnimationController") ~= nil
end

function RigValidation.isValidRig(object: any): boolean
	return typeof(object) == "Instance" and object:IsA("Model") and hasAnimator(object)
end

--[=[
	The rig a selection refers to.

	Clicking a limb in the Explorer selects the part, not the rig; the nearest
	ancestor rig is used instead. Returns (rig, nil), or (nil, hint) when the
	user selected a model that looks like a rig but can't be one, or (nil, nil)
	for unrelated selections (no message: people click parts all day).
]=]
function RigValidation.resolveRigFromSelection(selected: any): (Model?, string?)
	if typeof(selected) ~= "Instance" then
		return nil, nil
	end
	if RigValidation.isValidRig(selected) then
		return selected :: Model, nil
	end
	local current: Instance? = selected.Parent
	while current and not current:IsA("DataModel") do
		if RigValidation.isValidRig(current) then
			return current :: Model, nil
		end
		current = current.Parent
	end
	if selected:IsA("Model") then
		local nested = selected:FindFirstChildWhichIsA("Humanoid", true)
			or selected:FindFirstChildWhichIsA("AnimationController", true)
		if nested then
			return nil,
				string.format(
					"'%s' isn't a rig: its %s is inside '%s'. Select that model, or move the %s directly under '%s'.",
					selected.Name,
					nested.ClassName,
					nested.Parent and nested.Parent.Name or "?",
					nested.ClassName,
					selected.Name
				)
		end
		if selected:FindFirstChildWhichIsA("Motor6D", true) then
			return nil,
				string.format(
					"'%s' has Motor6Ds but no Humanoid or AnimationController directly inside it, so it can't play animations. Add an AnimationController to it.",
					selected.Name
				)
		end
	end
	return nil, nil
end

function RigValidation.countBaseParts(model: Instance, limit: number?): number
	local count = 0
	for _, descendant in ipairs(model:GetDescendants()) do
		if descendant:IsA("BasePart") then
			count += 1
			if limit and count > limit then
				break
			end
		end
	end
	return count
end

local function sortedKeys(map: { [string]: any }?): { string }
	local keys = {}
	for key in pairs(map or {}) do
		table.insert(keys, key)
	end
	table.sort(keys)
	return keys
end

local function listPreview(names: { string }, limit: number): string
	local shown = {}
	for index = 1, math.min(#names, limit) do
		shown[index] = names[index]
	end
	local text = table.concat(shown, ", ")
	if #names > limit then
		text ..= string.format(" (+%d more)", #names - limit)
	end
	return text
end

--[=[
	Problems with the rig's joints that would make animation channels go
	missing. ``rig`` is the built Rig (or nil when it failed to build).
]=]
function RigValidation.describeJointProblems(rigModel: Model, rig: any?): { string }
	local problems = {}
	local inRig: { [Instance]: boolean } = {}
	if rig and rig.bonesByInstance then
		for instance in pairs(rig.bonesByInstance) do
			inRig[instance] = true
		end
	end

	for _, joint in ipairs(rigModel:GetDescendants()) do
		if joint:IsA("Motor6D") then
			local motor = joint :: Motor6D
			local part0, part1 = motor.Part0, motor.Part1
			if not part0 or not part1 then
				table.insert(
					problems,
					string.format(
						"Motor6D '%s' is missing its %s, so it can't animate anything.",
						motor:GetFullName(),
						if not part0 then "Part0" else "Part1"
					)
				)
			elseif inRig[part1] and not inRig[part0] and part0:IsDescendantOf(rigModel) then
				-- Animations walk Motor6Ds from Part0 to Part1. With the body
				-- part as Part1 the joint points the wrong way, so the part it
				-- holds (often a tool) is left out entirely.
				table.insert(
					problems,
					string.format(
						"Motor6D '%s' is reversed: Part0 is '%s' and Part1 is '%s'. Swap Part0/Part1 (and C0/C1) so the body part is Part0, or '%s' can't be animated.",
						motor.Name,
						part0.Name,
						part1.Name,
						part0.Name
					)
				)
			end
		end
	end

	if rig then
		local ambiguous = sortedKeys(rig._ambiguousAnimationChannels)
		if #ambiguous > 0 then
			table.insert(
				problems,
				"These names are used by more than one animated part or bone, so animations can't tell them apart: "
					.. listPreview(ambiguous, 6)
					.. ". Rename the duplicates."
			)
		end
		local shadowed = sortedKeys(rig._shadowedChannels)
		if #shadowed > 0 then
			table.insert(
				problems,
				"Parts with the same name under different parents: only the first one gets animation channels: "
					.. listPreview(shadowed, 6)
					.. ". Rename them (for example 'Handle' -> 'SwordHandle')."
			)
		end
	end

	return problems
end

return RigValidation
