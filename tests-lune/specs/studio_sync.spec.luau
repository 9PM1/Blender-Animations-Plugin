-- Studio <-> Blender data and connection handling (ledger C1-C8, P3, R2, R3, S6, E5, S5).

local roblox = require("@lune/roblox")
local Instance = require("../harness").Instance
local CFrame = roblox.CFrame
local Enum = roblox.Enum
local Rigs = require("../rigs")

return function(H)
	local describe, it = H.describe, H.it

	describe("MirrorNames (P3)", function()
		it("swaps whole words only", function()
			local env = H.load()
			local MirrorNames = env.require("Components.MirrorNames")
			local cases = {
				LeftUpperArm = "RightUpperArm",
				["Left Arm"] = "Right Arm",
				left_leg = "right_leg",
				UpperLeftArm = "UpperRightArm",
				LeftEyeClosed = "RightEyeClosed",
				RightLipCornerPuller = "LeftLipCornerPuller",
				LEFT_HAND = "RIGHT_HAND",
				Leftovers = "Leftovers",
				Brightness = "Brightness",
				Torso = "Torso",
			}
			for input, expected in cases do
				H.expectEqual(MirrorNames.mirror(input), expected, input)
			end
		end)
	end)

	describe("AnimationSerializer (R2, R3, S6)", function()
		local function sequence()
			local kfs = Instance.new("KeyframeSequence")
			for _, time in { 0.5, 1.0 } do
				local keyframe = Instance.new("Keyframe")
				keyframe.Time = time
				keyframe.Parent = kfs
				local rootPose = Instance.new("Pose")
				rootPose.Name = "HumanoidRootPart"
				rootPose.Weight = 0
				rootPose.Parent = keyframe
				local torsoPose = Instance.new("Pose")
				torsoPose.Name = "Torso"
				torsoPose.Weight = 1
				torsoPose.CFrame = CFrame.new(0, time, 0)
				torsoPose.Parent = rootPose
				local face = Instance.new("NumberPose")
				face.Name = "JawDrop"
				face.Value = time
				face.EasingStyle = Enum.PoseEasingStyle.Cubic
				face.EasingDirection = Enum.PoseEasingDirection.In
				face.Parent = torsoPose
				if time == 0.5 then
					keyframe.Name = "Windup"
					local marker = Instance.new("KeyframeMarker")
					marker.Name = "Footstep"
					marker.Value = "left"
					marker.Parent = keyframe
				end
			end
			return kfs
		end

		it("keeps absolute times, markers and face easing", function()
			local env = H.load()
			local AnimationSerializer = env.require("Components.AnimationSerializer")
			local Rig = env.require("Components.Rig")
			local rig = Rig.new((Rigs.r6()))
			local data = AnimationSerializer.new():serialize(sequence(), rig)
			H.expectNear(data.kfs[1].t, 0.5, 1e-9, "first key time")
			H.expectNear(data.t, 1.0, 1e-9, "duration")
			H.expectEqual(data.kfs[1].fc.JawDrop.easingStyle, "Cubic", "face easing")
			H.expectEqual(data.kfs[1].fc.JawDrop.easingDirection, "In", "face direction")
			local byName = {}
			for _, marker in data.markers do
				byName[marker.name] = marker
			end
			H.expectEqual(byName.Windup and byName.Windup.type, "Name", "keyframe name")
			H.expectEqual(byName.Footstep and byName.Footstep.type, "Event", "marker type")
			H.expectEqual(byName.Footstep.value, "left", "marker value")
		end)

		it("a Bone elsewhere doesn't make a Motor6D animation a deform rig", function()
			local env = H.load()
			local AnimationSerializer = env.require("Components.AnimationSerializer")
			local Rig = env.require("Components.Rig")
			local model, parts = Rigs.r6()
			local faceBone = Instance.new("Bone")
			faceBone.Name = "FaceRoot"
			faceBone.Parent = parts.head
			local rig = Rig.new(model)
			H.expectTrue(rig.isDeformRig, "the rig itself has bones")
			local data = AnimationSerializer.new():serialize(sequence(), rig)
			H.expectEqual(data.is_deform_rig, false, "payload deform flag")
		end)
	end)

	describe("BlenderConnection failures (C5, C8)", function()
		it("classifies failures with messages the UI can show", function()
			local env = H.load()
			local BlenderConnection = env.require("Components.BlenderConnection")
			local connection = BlenderConnection.new(env.game:GetService("HttpService"))
			local kind, message = connection:describeFailure(31337, {
				StatusCode = 503,
				StatusMessage = "Service Unavailable",
				Body = '{"message":"Blender is busy (a dialog is open)."}',
			}, nil)
			H.expectEqual(kind, "busy", "503")
			H.expectContains(message, "busy", "503 message")
			kind, message = connection:describeFailure(31337, nil, "HttpError: ConnectFail")
			H.expectEqual(kind, "offline", "connect fail")
			H.expectContains(message, "31337", "names the port")
			kind, message = connection:describeFailure(31337, nil, "Http requests are not enabled. Enable via game settings")
			H.expectEqual(kind, "permission", "permission")
			H.expectContains(message, "Manage Plugins", "permission help")
			kind, message = connection:describeFailure(31337, {
				StatusCode = 500,
				StatusMessage = "Internal Server Error",
				Body = "{\"message\":\"'Rig' is in Edit Mode. Leave Edit Mode to sync.\"}",
			}, nil)
			H.expectEqual(kind, "error", "500")
			H.expectContains(message, "Edit Mode", "server message passed on")
		end)

		it("a closed Blender is reported, not treated as 'no change'", function()
			local env = H.load()
			local BlenderConnection = env.require("Components.BlenderConnection")
			local connection = BlenderConnection.new(env.game:GetService("HttpService"))
			local data, kind = connection:CheckAnimationStatus(31337, "Rig", "")
			H.expectEqual(data, nil, "no data")
			H.expectEqual(kind, "offline", "offline")
		end)
	end)

	describe("Undo (S5)", function()
		it("commits on success and rolls back on error", function()
			local env = H.load()
			local Undo = env.require("Components.Undo")
			local history = env.services.ChangeHistoryService
			H.expectEqual(Undo.record("Ok", function()
				return 42
			end), 42, "returns the result")
			H.expectEqual(history.finished[1].operation, Enum.FinishRecordingOperation.Commit, "commit")
			local ok = pcall(Undo.record, "Broken", function()
				error("boom")
			end)
			H.expectEqual(ok, false, "error propagates")
			H.expectEqual(history.finished[2].operation, Enum.FinishRecordingOperation.Cancel, "cancel")
		end)
	end)

	describe("ExportManager grip detection (E5)", function()
		it("finds the grip whichever way round the joint is", function()
			local env = H.load()
			local ExportManager = env.require("Services.ExportManager")
			local model, parts = Rigs.r6()
			local tool = Instance.new("Model")
			tool.Name = "Sword"
			tool.Parent = model
			local handle = Rigs.part(tool, "Handle", CFrame.new(2, 2, 0))
			local weaponParts = { [handle] = true }
			Rigs.motor(handle, parts.rightArm, "Grip")
			local joint, rigPart, weaponPart = ExportManager.findGripJoint({ model }, weaponParts, model)
			H.expectEqual(joint and joint.Name, "Grip", "reversed joint found")
			H.expectEqual(rigPart, parts.rightArm, "rig side")
			H.expectEqual(weaponPart, handle, "weapon side")
		end)
	end)

	describe("ExportManager rig export (E1)", function()
		it("moves a clone to the origin with the exact rotation", function()
			local env = H.load()
			local ExportManager = env.require("Services.ExportManager")
			local model, parts = Rigs.r6()
			local turned = CFrame.new(5, 3, 7) * CFrame.Angles(0.4, 1.1, -0.3)
			model:PivotTo(turned)
			local before = parts.root.CFrame
			ExportManager._moveCloneToOrigin(model)
			local after = parts.root.CFrame
			H.expectNear(after.X, 0, 1e-5, "x")
			H.expectNear(after.Y, before.Y, 1e-5, "height kept")
			H.expectNear(after.Z, 0, 1e-5, "z")
			local delta = before.Rotation:ToObjectSpace(after.Rotation)
			local axisX, axisY, axisZ = delta:ToEulerAnglesXYZ()
			H.expectNear(axisX + axisY + axisZ, 0, 1e-5, "rotation unchanged")
		end)

		it("exports a clone and never moves the rig in the place", function()
			local env = H.load()
			local State = env.require("state")
			local ExportManager = env.require("Services.ExportManager")
			local model, parts = Rigs.r6()
			parts.root.Parent = model
			model:PivotTo(CFrame.new(10, 3, 10))
			local original = parts.root.CFrame
			local saved = {}
			local plugin = {
				PromptSaveSelectionAsync = function(_self, name)
					local selected = env.game.Selection:Get()[1]
					saved.name = name
					saved.selected = selected
					saved.position = selected.PrimaryPart.CFrame.Position
					return true
				end,
			}
			State.activeRigModel = model
			State.setRigOrigin:set(true)
			env.game.Selection:Set({ parts.head })
			ExportManager.new(plugin):exportRigRbxm()
			H.expectTrue(saved.selected ~= model, "the save dialog got a clone")
			H.expectNear(saved.position.X, 0, 1e-5, "clone moved to origin")
			H.expectNear((parts.root.CFrame.Position - original.Position).Magnitude, 0, 1e-6, "rig in place untouched")
			H.expectEqual(saved.selected.Parent, nil, "clone removed afterwards")
			H.expectEqual(env.game.Selection:Get()[1], parts.head, "selection restored")
			H.expectEqual(State.isSelectionLocked:get(), false, "selection unlocked")
		end)
	end)

	describe("ExportManager connect weapon (E6)", function()
		it("adds an undoable Motor6D from the hand, keeping the weapon in place", function()
			local env = H.load()
			local State = env.require("state")
			local ExportManager = env.require("Services.ExportManager")
			local model, parts = Rigs.r6()
			local sword = Instance.new("Model")
			sword.Name = "Sword"
			sword.Parent = env.game.Workspace
			local handle = Rigs.part(sword, "Handle", CFrame.new(2, 1.5, -1) * CFrame.Angles(0, 0.5, 0))
			sword.PrimaryPart = handle
			local placement = handle.CFrame
			State.activeRigModel = model
			State.selectedWeapon:set(sword)
			local manager = ExportManager.new(nil)
			H.expectTrue(manager:connectWeaponToHand(), "connected")
			local grip = parts.rightArm:FindFirstChild("HandleGrip")
			H.expectTrue(grip ~= nil and grip:IsA("Motor6D"), "Motor6D created")
			H.expectEqual(grip.Part0, parts.rightArm, "body part is Part0")
			H.expectEqual(grip.Part1, handle, "weapon is Part1")
			local rebuilt = parts.rightArm.CFrame * grip.C0 * grip.C1:Inverse()
			H.expectNear((rebuilt.Position - placement.Position).Magnitude, 0, 1e-5, "weapon kept in place")
			H.expectContains(State.weaponConnectionStatus:get(), "Motor6D", "status")
			local finished = env.services.ChangeHistoryService.finished
			H.expectEqual(finished[#finished].name, "Connect weapon to hand", "undo step")
		end)
	end)
end
