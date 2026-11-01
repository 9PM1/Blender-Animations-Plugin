--[=[
	Small rigs built from real Instances for the specs.
]=]

local roblox = require("@lune/roblox")
local Instance = require("./harness").Instance
local CFrame = roblox.CFrame
local Vector3 = roblox.Vector3

local Rigs = {}

local function part(model, name, cframe, size)
	local p = Instance.new("Part")
	p.Name = name
	p.CFrame = cframe
	p.Size = size or Vector3.new(1, 2, 1)
	p.Parent = model
	return p
end

local function motor(part0, part1, name, c0, c1)
	local m = Instance.new("Motor6D")
	m.Name = name or part1.Name
	m.Part0 = part0
	m.Part1 = part1
	m.C0 = c0 or part0.CFrame:ToObjectSpace(part1.CFrame)
	m.C1 = c1 or CFrame.new()
	m.Parent = part0
	return m
end

Rigs.part = part
Rigs.motor = motor

-- An R6-shaped rig: HumanoidRootPart -> Torso -> Head / arms / legs.
function Rigs.r6(options)
	options = options or {}
	local model = Instance.new("Model")
	model.Name = options.name or "Rig"
	local humanoid = Instance.new("Humanoid")
	humanoid.Parent = model
	local root = part(model, "HumanoidRootPart", CFrame.new(0, 3, 0), Vector3.new(2, 2, 1))
	if not options.noPrimaryPart then
		model.PrimaryPart = root
	end
	local torso = part(model, "Torso", CFrame.new(0, 3, 0), Vector3.new(2, 2, 1))
	local head = part(model, "Head", CFrame.new(0, 4.5, 0), Vector3.new(2, 1, 1))
	local rightArm = part(model, "Right Arm", CFrame.new(1.5, 3, 0))
	local leftArm = part(model, "Left Arm", CFrame.new(-1.5, 3, 0))
	local rightLeg = part(model, "Right Leg", CFrame.new(0.5, 1, 0))
	local leftLeg = part(model, "Left Leg", CFrame.new(-0.5, 1, 0))
	motor(root, torso, "RootJoint")
	motor(torso, head, "Neck")
	motor(torso, rightArm, "Right Shoulder")
	motor(torso, leftArm, "Left Shoulder")
	motor(torso, rightLeg, "Right Hip")
	motor(torso, leftLeg, "Left Hip")
	return model, {
		root = root,
		torso = torso,
		head = head,
		rightArm = rightArm,
		leftArm = leftArm,
		rightLeg = rightLeg,
		leftLeg = leftLeg,
	}
end

return Rigs
