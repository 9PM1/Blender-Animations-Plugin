-- Studio rig selection and validation (ledger S1-S11, X12, A1).

local roblox = require("@lune/roblox")
local Instance = require("../harness").Instance
local CFrame = roblox.CFrame
local Vector3 = roblox.Vector3
local Rigs = require("../rigs")

return function(H)
	local describe, it = H.describe, H.it

	describe("RigValidation.resolveRigFromSelection (S3)", function()
		it("a clicked limb selects its rig", function()
			local env = H.load()
			local RigValidation = env.require("Components.RigValidation")
			local model, parts = Rigs.r6()
			local rig, hint = RigValidation.resolveRigFromSelection(parts.rightArm)
			H.expectEqual(rig, model, "rig")
			H.expectEqual(hint, nil, "hint")
		end)

		it("a nested AnimationController explains itself", function()
			local env = H.load()
			local RigValidation = env.require("Components.RigValidation")
			local outer = Instance.new("Model")
			outer.Name = "Car"
			local inner = Instance.new("Model")
			inner.Name = "Body"
			inner.Parent = outer
			Instance.new("AnimationController").Parent = inner
			local rig, hint = RigValidation.resolveRigFromSelection(outer)
			H.expectEqual(rig, nil, "rig")
			H.expectContains(hint, "isn't a rig", "hint")
			H.expectContains(hint, "Body", "hint")
		end)

		it("Motor6Ds without a Humanoid say what to add", function()
			local env = H.load()
			local RigValidation = env.require("Components.RigValidation")
			local model = Instance.new("Model")
			model.Name = "Mech"
			local a = Rigs.part(model, "A", CFrame.new())
			local b = Rigs.part(model, "B", CFrame.new(0, 2, 0))
			Rigs.motor(a, b)
			local rig, hint = RigValidation.resolveRigFromSelection(model)
			H.expectEqual(rig, nil, "rig")
			H.expectContains(hint, "AnimationController", "hint")
		end)

		it("unrelated parts stay quiet", function()
			local env = H.load()
			local RigValidation = env.require("Components.RigValidation")
			local loose = Instance.new("Part")
			local rig, hint = RigValidation.resolveRigFromSelection(loose)
			H.expectEqual(rig, nil, "rig")
			H.expectEqual(hint, nil, "hint")
		end)
	end)

	describe("Rig joints (S7, S9, S10)", function()
		it("a reversed tool Motor6D is reported, not silently dropped", function()
			local env = H.load()
			local Rig = env.require("Components.Rig")
			local RigValidation = env.require("Components.RigValidation")
			local model, parts = Rigs.r6()
			local handle = Rigs.part(model, "Handle", CFrame.new(2, 2, 0))
			Rigs.motor(handle, parts.rightArm, "Grip")
			local rig = Rig.new(model)
			H.expectEqual(rig.bones.Handle, nil, "reversed tool is not a channel")
			local problems = table.concat(RigValidation.describeJointProblems(model, rig), "\n")
			H.expectContains(problems, "reversed", "problems")
			H.expectContains(problems, "Grip", "problems")
		end)

		it("same-name parts under different parents are reported", function()
			local env = H.load()
			local Rig = env.require("Components.Rig")
			local RigValidation = env.require("Components.RigValidation")
			local model, parts = Rigs.r6()
			local right = Rigs.part(model, "Handle", CFrame.new(2, 2, 0))
			local left = Rigs.part(model, "Handle", CFrame.new(-2, 2, 0))
			Rigs.motor(parts.rightArm, right, "RightGrip")
			Rigs.motor(parts.leftArm, left, "LeftGrip")
			local rig = Rig.new(model)
			H.expectTrue(rig._shadowedChannels.Handle, "shadowed flag")
			local problems = table.concat(RigValidation.describeJointProblems(model, rig), "\n")
			H.expectContains(problems, "Handle", "problems")
		end)

		it("duplicate bone names no longer block the rig", function()
			local env = H.load()
			local Rig = env.require("Components.Rig")
			local model, parts = Rigs.r6()
			for index, host in { parts.head, parts.torso } do
				local bone = Instance.new("Bone")
				bone.Name = "Root"
				bone.CFrame = CFrame.new(0, index, 0)
				bone.Parent = host
			end
			local ok, rigOrError = pcall(Rig.new, model)
			H.expectTrue(ok, "Rig.new succeeded: " .. tostring(rigOrError))
			H.expectTrue(rigOrError._ambiguousAnimationChannels.Root, "Root flagged ambiguous")
		end)
	end)

	describe("Rig:LoadAnimation (A1, X12)", function()
		it("reports unknown and ambiguous channels instead of failing", function()
			local env = H.load()
			local Rig = env.require("Components.Rig")
			local model = Rigs.r6()
			local rig = Rig.new(model)
			rig._ambiguousAnimationChannels.Head = true
			local identity = { CFrame.new():GetComponents() }
			rig:LoadAnimation({
				t = 1,
				kfs = {
					{ t = 0, kf = { ["Right Arm"] = identity, Head = identity, MyArm = identity } },
					{ t = 1, kf = { ["Right Arm"] = identity, MyArm = identity } },
				},
			})
			H.expectEqual(rig.lastLoadReport.unmatched[1], "MyArm", "unmatched")
			H.expectEqual(#rig.lastLoadReport.unmatched, 1, "listed once")
			H.expectEqual(rig.lastLoadReport.ambiguous[1], "Head", "ambiguous")
			H.expectTrue(rig.bones["Right Arm"].poses[0] ~= nil, "valid channel still applied")
		end)

		it("skewed rotations come out orthonormal", function()
			local env = H.load()
			local Rig = env.require("Components.Rig")
			local rig = Rig.new((Rigs.r6()))
			-- A sheared rotation, as rounding or a scaled bone produces.
			local skewed = { 0, 0, 0, 1, 0.3, 0, 0, 1, 0, 0.2, 0, 1 }
			rig:LoadAnimation({ t = 0, kfs = { { t = 0, kf = { ["Right Arm"] = skewed } } } })
			local cf = rig.bones["Right Arm"].poses[0].transform
			local right, up, back = cf.RightVector, cf.UpVector, cf.LookVector
			H.expectNear(right:Dot(up), 0, 1e-5, "x.y")
			H.expectNear(right:Dot(back), 0, 1e-5, "x.z")
			H.expectNear(up:Dot(back), 0, 1e-5, "y.z")
			H.expectNear(right.Magnitude, 1, 1e-5, "|x|")
		end)
	end)

	describe("RigManager:setRig (S4, S5, S8-S11)", function()
		local function newManager(env)
			local RigManager = env.require("Services.RigManager")
			local State = env.require("state")
			local playback = { stopRigTrack = function() end }
			local manager = RigManager.new(playback, nil)
			State.rigManager = manager
			return manager, State
		end

		it("setting a missing PrimaryPart is one undo step", function()
			local env = H.load()
			local manager, State = newManager(env)
			local model, parts = Rigs.r6({ noPrimaryPart = true })
			State.activeRigModel = model
			manager:setRig(model)
			H.expectEqual(model.PrimaryPart, parts.root, "primary part")
			local finished = env.services.ChangeHistoryService.finished
			H.expectEqual(finished[1] and finished[1].name, "Set rig PrimaryPart", "recording")
			H.expectEqual(finished[1].operation, roblox.Enum.FinishRecordingOperation.Commit, "committed")
		end)

		it("joint problems are shown at selection", function()
			local env = H.load()
			local manager, State = newManager(env)
			local model, parts = Rigs.r6()
			local handle = Rigs.part(model, "Handle", CFrame.new(2, 2, 0))
			Rigs.motor(handle, parts.rightArm, "Grip")
			State.activeRigModel = model
			manager:setRig(model)
			local warnings = table.concat(State.activeWarnings:get(), "\n")
			H.expectContains(warnings, "reversed", "warnings")
		end)

		it("a slower, older setRig can't replace the current rig", function()
			local env = H.load()
			local manager, State = newManager(env)
			local Rig = env.require("Components.Rig")
			local first = Rigs.r6({ name = "First" })
			local second = Rigs.r6({ name = "Second" })
			-- The user clicks Second while First is still building.
			local realNew = Rig.new
			local reentered = false
			Rig.new = function(model)
				if model == first and not reentered then
					reentered = true
					manager:setRig(second)
				end
				return realNew(model)
			end
			manager:setRig(first)
			Rig.new = realNew
			H.expectTrue(reentered, "second selection happened mid-build")
			H.expectEqual(State.activeRig and State.activeRig.model, second, "current rig kept")
		end)

		it("warnings don't stack up", function()
			local env = H.load()
			local manager, State = newManager(env)
			manager:clearWarnings()
			manager:addWarning("Invalid port")
			manager:addWarning("Invalid port")
			H.expectEqual(#State.activeWarnings:get(), 1, "deduplicated")
			manager:removeWarning("Invalid port")
			H.expectEqual(#State.activeWarnings:get(), 0, "removed")
		end)

		it("big MeshPart rigs are counted (S11)", function()
			local env = H.load()
			local RigValidation = env.require("Components.RigValidation")
			local model = Instance.new("Model")
			for _ = 1, 3 do
				Instance.new("MeshPart").Parent = model
			end
			H.expectEqual(RigValidation.countBaseParts(model), 3, "mesh parts counted")
		end)
	end)

	describe("Vector3 sanity", function()
		it("lune datatypes behave", function()
			H.expectNear(Vector3.new(3, 4, 0).Magnitude, 5, 1e-9, "magnitude")
		end)
	end)
end
