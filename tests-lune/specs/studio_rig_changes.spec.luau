-- Joint changes Blender queues for the Studio rig (rig_changes): apply, skip,
-- conflict, reversed joints, undo, playback of the same export and the
-- confirmation back to Blender.

local roblox = require("@lune/roblox")
local serde = require("@lune/serde")
local task = require("@lune/task")
local CFrame = roblox.CFrame
local Enum = roblox.Enum
local Rigs = require("../rigs")

return function(H)
	local describe, it = H.describe, H.it

	-- The new pivot: the shoulder top, in the joint frame.
	local OFFSET = CFrame.new(0, 1, 0)
	-- Blender turns the arm about the new pivot; its export is relative to
	-- the old joint, so the pose is the turn conjugated by the offset.
	local SWING = CFrame.Angles(0.9, 0.2, 0)
	local BLENDER_POSE = OFFSET * SWING * OFFSET:Inverse()

	local function components(cframe)
		return { cframe:GetComponents() }
	end

	local function expectSameCFrame(actual, expected, label)
		local a, b = components(actual), components(expected)
		for index = 1, 12 do
			H.expectNear(a[index], b[index], 1e-4, string.format("%s [%d]", label, index))
		end
	end

	-- Blender's entry for moving part1's pivot (parent side first).
	local function pivotEntry(parentSide, childSide, part0, part1)
		return {
			type = "joint_pivot",
			part0 = part0,
			part1 = part1,
			joint_type = "Motor6D",
			c0 = components(parentSide * OFFSET),
			c1 = components(childSide * OFFSET),
			previous_c0 = components(parentSide),
			previous_c1 = components(childSide),
			offset = components(OFFSET),
		}
	end

	local function animation(changes)
		local function pose(cframe)
			return { components(cframe), "Linear", "Out" }
		end
		return {
			t = 1,
			kfs = {
				{ t = 0, kf = { ["Right Arm"] = pose(CFrame.new()), ["Left Arm"] = pose(CFrame.new()) } },
				{ t = 1, kf = { ["Right Arm"] = pose(BLENDER_POSE), ["Left Arm"] = pose(BLENDER_POSE) } },
			},
			rig_changes = changes,
		}
	end

	local function setup(options)
		options = options or {}
		local env = H.load()
		local State = env.require("state")
		local RigManager = env.require("Services.RigManager")
		State.rigManager = RigManager.new({ stopRigTrack = function() end }, nil)
		local model, parts = Rigs.r6()
		local shoulder = parts.torso:FindFirstChild("Right Shoulder")
		if options.reversed then
			-- Part0 is the arm: C0 is the arm side, C1 the torso side.
			shoulder.Parent = nil
			shoulder = Rigs.motor(parts.rightArm, parts.torso, "Right Shoulder", CFrame.new(-1.5, 1, 0), CFrame.new(0, 1, 0))
		end
		local Rig = env.require("Components.Rig")
		State.activeRigModel = model
		State.activeRig = Rig.new(model)
		local AnimationManager = env.require("Services.AnimationManager")
		local manager = AnimationManager.new({ playAllRigs = function() end }, nil)
		return {
			env = env,
			State = State,
			manager = manager,
			model = model,
			parts = parts,
			shoulder = shoulder,
			history = env.services.ChangeHistoryService,
			RigChanges = env.require("Components.RigChanges"),
		}
	end

	local function sync(s, data, hash)
		return s.manager:applyLiveSyncEnvelope({ type = "animation_full", hash = hash or "h1", animation = data }, "", true)
	end

	local function warnings(s)
		return table.concat(s.State.activeWarnings:get(), "\n")
	end

	local function rightArmPose(s, time)
		return s.State.activeRig.bones["Right Arm"].poses[time].transform
	end

	local function played(s, c0, pose, c1)
		return s.parts.torso.CFrame * c0 * pose * c1:Inverse()
	end

	describe("Blender rig changes (pivots)", function()
		it("writes C0 and C1 in one undo step and plays that export about the new pivot", function()
			local s = setup()
			local joint = s.shoulder
			local oldC0, oldC1 = joint.C0, joint.C1
			local entry = pivotEntry(oldC0, oldC1, "Torso", "Right Arm")
			H.expectTrue(sync(s, animation({ entry })), "loaded")

			expectSameCFrame(joint.C0, oldC0 * OFFSET, "C0")
			expectSameCFrame(joint.C1, oldC1 * OFFSET, "C1")
			expectSameCFrame(s.parts.torso.CFrame * joint.C0 * joint.C1:Inverse(), s.parts.rightArm.CFrame, "parts stay put")
			local finished = s.history.finished
			H.expectEqual(#finished, 1, "undo steps")
			H.expectEqual(finished[1].name, s.RigChanges.UNDO_NAME, "undo step name")
			H.expectEqual(finished[1].operation, Enum.FinishRecordingOperation.Commit, "committed")

			-- The arm lands where Blender puts it, turning about the new pivot.
			local pose = rightArmPose(s, 1)
			expectSameCFrame(played(s, joint.C0, pose, joint.C1), played(s, oldC0, BLENDER_POSE, oldC1), "played")
			expectSameCFrame(pose, SWING, "pose about the new pivot")
			-- Other joints keep their poses.
			expectSameCFrame(s.State.activeRig.bones["Left Arm"].poses[1].transform, BLENDER_POSE, "Left Arm untouched")

			H.expectContains(warnings(s), "Moved the Right Arm joint (Right Shoulder)", "shown")
			local confirm = s.manager:takeRigChangeConfirmations()
			H.expectEqual(#confirm, 1, "to confirm")
			H.expectEqual(confirm[1].change.entry, entry, "sent back as Blender sent it")
			H.expectEqual(s.manager:takeRigChangeConfirmations(), nil, "taken once")
		end)

		it("skips a joint that already has the change but still plays it about the new pivot", function()
			local s = setup()
			local joint = s.shoulder
			local oldC0, oldC1 = joint.C0, joint.C1
			local entry = pivotEntry(oldC0, oldC1, "Torso", "Right Arm")
			-- Applied by an earlier sync that Blender hasn't confirmed yet.
			joint.C0, joint.C1 = oldC0 * OFFSET, oldC1 * OFFSET
			H.expectTrue(sync(s, animation({ entry })), "loaded")

			H.expectEqual(#s.history.finished, 0, "nothing recorded")
			expectSameCFrame(joint.C0, oldC0 * OFFSET, "C0 kept")
			expectSameCFrame(rightArmPose(s, 1), SWING, "pose about the new pivot")
			H.expectTrue(not string.find(warnings(s), "Moved", 1, true), "no note for a skipped joint")
			local confirm = s.manager:takeRigChangeConfirmations()
			H.expectEqual(confirm[1].status, "unchanged", "still confirmed to Blender")
		end)

		it("leaves a joint that changed since alone and says so", function()
			local s = setup()
			local joint = s.shoulder
			local oldC0, oldC1 = joint.C0, joint.C1
			local entry = pivotEntry(oldC0, oldC1, "Torso", "Right Arm")
			-- Edited in Studio after the rig went to Blender.
			joint.C0 = oldC0 * CFrame.new(0.3, 0, 0)
			local edited = joint.C0
			H.expectTrue(sync(s, animation({ entry })), "loaded")

			expectSameCFrame(joint.C0, edited, "C0 untouched")
			expectSameCFrame(joint.C1, oldC1, "C1 untouched")
			H.expectEqual(#s.history.finished, 0, "nothing recorded")
			expectSameCFrame(rightArmPose(s, 1), BLENDER_POSE, "pose not converted")
			H.expectContains(warnings(s), "Didn't move the Right Arm joint (Right Shoulder)", "conflict shown")
			H.expectEqual(#s.manager:takeRigChangeConfirmations(), 0, "nothing to confirm")

			-- Once Blender stops sending it, the warning goes.
			H.expectTrue(sync(s, animation(nil), "h2"), "loaded without changes")
			H.expectTrue(not string.find(warnings(s), "Didn't move", 1, true), "conflict cleared")
		end)

		it("reports a change for a joint the rig doesn't have", function()
			local s = setup()
			local entry = pivotEntry(CFrame.new(), CFrame.new(), "Right Arm", "Sword")
			H.expectTrue(sync(s, animation({ entry })), "loaded")
			H.expectContains(warnings(s), "no Motor6D between Right Arm and Sword", "missing joint shown")
			H.expectEqual(#s.history.finished, 0, "nothing recorded")
		end)

		it("swaps C0 and C1 on a reversed joint", function()
			local s = setup({ reversed = true })
			local joint = s.shoulder
			local parentSide, childSide = joint.C1, joint.C0
			local torsoBefore = s.parts.rightArm.CFrame * joint.C0 * joint.C1:Inverse()
			local entry = pivotEntry(parentSide, childSide, "Torso", "Right Arm")

			local result = s.RigChanges.apply(s.model, s.State.activeRig, { entry }, true)
			H.expectEqual(result.outcomes[1].status, "applied", "status")
			H.expectTrue(result.outcomes[1].reversed, "found reversed")
			expectSameCFrame(joint.C0, childSide * OFFSET, "C0 is the arm side")
			expectSameCFrame(joint.C1, parentSide * OFFSET, "C1 is the torso side")
			expectSameCFrame(s.parts.rightArm.CFrame * joint.C0 * joint.C1:Inverse(), torsoBefore, "parts stay put")

			local again = s.RigChanges.apply(s.model, s.State.activeRig, { entry }, true)
			H.expectEqual(again.outcomes[1].status, "unchanged", "found applied")
			H.expectEqual(#s.history.finished, 1, "one undo step")
		end)

		it("is one undo step, and an undone change is applied again while Blender lists it", function()
			local s = setup()
			local right = s.shoulder
			local left = s.parts.torso:FindFirstChild("Left Shoulder")
			local rightC0, rightC1, leftC0, leftC1 = right.C0, right.C1, left.C0, left.C1
			local data = animation({
				pivotEntry(rightC0, rightC1, "Torso", "Right Arm"),
				pivotEntry(leftC0, leftC1, "Torso", "Left Arm"),
			})
			H.expectTrue(sync(s, data), "loaded")
			H.expectEqual(#s.history.finished, 1, "both joints in one undo step")
			expectSameCFrame(left.C0, leftC0 * OFFSET, "Left Shoulder moved")

			-- Studio's undo puts the old values back; Blender hasn't heard.
			right.C0, right.C1, left.C0, left.C1 = rightC0, rightC1, leftC0, leftC1
			local again = animation(data.rig_changes)
			H.expectTrue(sync(s, again, "h2"), "loaded again")
			expectSameCFrame(right.C0, rightC0 * OFFSET, "Right Shoulder moved again")
			H.expectEqual(#s.history.finished, 2, "a new undo step")
			expectSameCFrame(rightArmPose(s, 1), SWING, "pose about the new pivot")
		end)

		it("doesn't write joints for an animation loaded another way", function()
			local s = setup()
			local joint = s.shoulder
			local oldC0 = joint.C0
			local data = animation({ pivotEntry(oldC0, joint.C1, "Torso", "Right Arm") })
			s.manager:_takeRigChanges(data, false)
			expectSameCFrame(joint.C0, oldC0, "C0 untouched")
			H.expectEqual(s.manager.lastRigChangeResult.outcomes[1].status, "waiting", "status")
			H.expectEqual(data.kfs[2].kf["Right Arm"][1][2], components(BLENDER_POSE)[2], "pose not converted")
			H.expectEqual(s.manager:takeRigChangeConfirmations(), nil, "nothing to confirm")
		end)

		it("converts live-sync delta keys like the base", function()
			local s = setup()
			local joint = s.shoulder
			H.expectTrue(sync(s, animation({ pivotEntry(joint.C0, joint.C1, "Torso", "Right Arm") })), "loaded")
			local delta = {
				type = "animation_delta",
				base_hash = "h1",
				hash = "h2",
				upsert = { { t = 0.5, kf = { ["Right Arm"] = { components(BLENDER_POSE), "Linear", "Out" } } } },
				remove = {},
			}
			H.expectTrue(s.manager:applyLiveSyncEnvelope(delta, "h1", true), "delta applied")
			expectSameCFrame(rightArmPose(s, 0.5), SWING, "delta key about the new pivot")
			expectSameCFrame(rightArmPose(s, 1), SWING, "base key kept")
		end)
	end)

	describe("Confirming rig changes to Blender", function()
		local function syncManager(s, confirm)
			local BlenderSyncManager = s.env.require("Services.BlenderSyncManager")
			local manager = BlenderSyncManager.new({}, s.manager)
			s.State.selectedArmature:set({ name = "Rig" })
			manager.blenderConnectionService = { ConfirmRigChanges = confirm }
			return manager, BlenderSyncManager
		end

		it("sends the applied entries back once", function()
			local s = setup()
			local sent = {}
			local manager = syncManager(s, function(_self, _port, armature, changes)
				table.insert(sent, { armature = armature, changes = changes })
				return { success = true, applied = { "Right Arm" }, skipped = {} }
			end)
			local entry = pivotEntry(s.shoulder.C0, s.shoulder.C1, "Torso", "Right Arm")
			H.expectTrue(sync(s, animation({ entry })), "loaded")
			manager:_confirmRigChanges("Rig")
			H.expectEqual(#sent, 1, "confirmed")
			H.expectEqual(sent[1].armature, "Rig", "armature")
			H.expectEqual(sent[1].changes[1], entry, "the entry Blender sent")
			-- A delta (same rig_changes) doesn't send it again.
			manager:_confirmRigChanges("Rig")
			H.expectEqual(#sent, 1, "not resent")
		end)

		it("shows a failed confirmation and retries it", function()
			local s = setup()
			local calls = 0
			local manager, BlenderSyncManager = syncManager(s, function()
				calls += 1
				if calls == 1 then
					return nil, "busy", "Blender is busy (a dialog is open)."
				end
				return { success = true, applied = { "Right Arm" }, skipped = {} }
			end)
			BlenderSyncManager.rigConfirmRetrySeconds = 0.02
			H.expectTrue(sync(s, animation({ pivotEntry(s.shoulder.C0, s.shoulder.C1, "Torso", "Right Arm") })), "loaded")
			manager:_confirmRigChanges("Rig")
			H.expectContains(warnings(s), "couldn't tell Blender (Blender is busy", "failure shown")
			task.wait(0.15)
			BlenderSyncManager.rigConfirmRetrySeconds = 2
			H.expectEqual(calls, 2, "retried")
			H.expectTrue(not string.find(warnings(s), "couldn't tell Blender", 1, true), "cleared after the retry")
		end)

		it("an undo after Blender rebased is reported, and a redo clears it", function()
			local s = setup()
			local manager = syncManager(s, function()
				return { success = true, applied = { "Right Arm" }, skipped = {} }
			end)
			local joint = s.shoulder
			local oldC0, oldC1 = joint.C0, joint.C1
			H.expectTrue(sync(s, animation({ pivotEntry(oldC0, oldC1, "Torso", "Right Arm") })), "loaded")
			manager:_confirmRigChanges("Rig")

			joint.C0, joint.C1 = oldC0, oldC1
			s.history.OnUndo:Fire(s.RigChanges.UNDO_NAME)
			H.expectContains(warnings(s), "Redo (Ctrl+Y)", "undo reported")

			joint.C0, joint.C1 = oldC0 * OFFSET, oldC1 * OFFSET
			s.history.OnRedo:Fire(s.RigChanges.UNDO_NAME)
			H.expectTrue(not string.find(warnings(s), "Redo (Ctrl+Y)", 1, true), "redo clears it")
		end)

		it("the Blender import button applies and confirms", function()
			local s = setup()
			local sent = 0
			local manager = syncManager(s, function()
				sent += 1
				return { success = true, applied = { "Right Arm" }, skipped = {} }
			end)
			local joint = s.shoulder
			local oldC0 = joint.C0
			local data = animation({ pivotEntry(oldC0, joint.C1, "Torso", "Right Arm") })
			local body = serde.compress("zlib", serde.encode("json", data))
			manager.blenderConnectionService.ImportAnimation = function()
				return body, nil, nil
			end
			H.expectTrue(manager:importAnimationFromBlender(), "imported")
			expectSameCFrame(joint.C0, oldC0 * OFFSET, "C0")
			expectSameCFrame(rightArmPose(s, 1), SWING, "pose about the new pivot")
			H.expectEqual(sent, 1, "confirmed")
		end)

		it("BlenderConnection posts them to /confirm_rig_changes/<armature>", function()
			local env = H.load()
			local http = env.game:GetService("HttpService")
			local captured
			function http:RequestAsync(options)
				captured = options
				return { Success = true, StatusCode = 200, Body = '{"success":true,"applied":["Right Arm"],"skipped":[]}' }
			end
			local BlenderConnection = env.require("Components.BlenderConnection")
			local data = BlenderConnection.new(http):ConfirmRigChanges(31337, "My Rig", { { type = "joint_pivot", part1 = "Right Arm" } })
			H.expectEqual(captured.Url, "http://localhost:31337/confirm_rig_changes/My%20Rig", "url")
			H.expectEqual(captured.Method, "POST", "method")
			H.expectEqual(captured.Headers["Content-Type"], "application/json", "content type")
			H.expectEqual(http:JSONDecode(captured.Body).changes[1].part1, "Right Arm", "body")
			H.expectEqual(data.applied[1], "Right Arm", "answer")
		end)
	end)
end
