--[=[
	End-to-end sync test: the real Studio connection, serializer and Rig code
	(run under Lune) against the real Blender sync server.

	Start Blender first:
	    blender --background --factory-startup --online-mode --python tests-lune/e2e_blender_server.py -- <port> <stop_file>
	then:
	    lune run tests-lune/e2e <port> <armature name>
]=]

local net = require("@lune/net")
local process = require("@lune/process")
local roblox = require("@lune/roblox")
local task = require("@lune/task")
local H = require("./harness")
local Rigs = require("./rigs")
local CFrame = roblox.CFrame

local port = tonumber(process.args[1]) or 31337
local armatureName = process.args[2] or "__TemplateR6_Armature"

local env = H.load()
local http = env.game:GetService("HttpService")
function http:RequestAsync(options)
	local ok, response = pcall(net.request, {
		url = options.Url,
		method = options.Method,
		headers = options.Headers,
		body = options.Body,
	})
	if not ok then
		error("HttpError: ConnectFail (" .. tostring(response) .. ")")
	end
	return {
		Success = response.ok,
		StatusCode = response.statusCode,
		StatusMessage = response.statusMessage,
		Body = response.body,
		Headers = response.headers,
	}
end

local BlenderConnection = env.require("Components.BlenderConnection")
local AnimationSerializer = env.require("Components.AnimationSerializer")
local Rig = env.require("Components.Rig")
local connection = BlenderConnection.new(http)

-- Wait for Blender to come up.
local info
for _ = 1, 200 do
	info = connection:Ping(port)
	if info then
		break
	end
	task.wait(0.1)
end

local describe, it = H.describe, H.it

describe("end to end", function()
	it("ping names the Blender file", function()
		H.expectTrue(info ~= nil, "Blender answered")
		H.expectEqual(info.protocol, 2, "protocol")
		H.expectEqual(info.blend_file, "(unsaved file)", "file label")
	end)

	it("lists the armature and reports a change", function()
		local armatures = connection:ListArmatures(port)
		local found = false
		for _, armature in armatures or {} do
			if armature.name == armatureName then
				found = true
			end
		end
		H.expectTrue(found, "armature listed")
		local status = connection:CheckAnimationStatus(port, armatureName, "")
		H.expectTrue(status and status.has_update, "has update")
	end)

	local studioRig
	it("Blender -> Studio: every channel lands on the Studio rig", function()
		local body, kind, message = connection:ImportAnimation(port, armatureName, nil, false)
		H.expectTrue(body ~= nil, "export body (" .. tostring(kind) .. ": " .. tostring(message) .. ")")
		local data = AnimationSerializer.new():deserialize(body, true)
		H.expectTrue(type(data) == "table" and #data.kfs > 0, "decoded keyframes")
		studioRig = Rig.new((Rigs.r6()))
		studioRig:LoadAnimation(data)
		H.expectEqual(#studioRig.lastLoadReport.unmatched, 0, "unmatched channels")
		local keyed = 0
		for _ in studioRig.bones["Right Arm"].poses do
			keyed += 1
		end
		H.expectTrue(keyed >= 2, "Right Arm keyed")
	end)

	it("a pivot written to the Studio rig: Studio applies it, Blender rebases", function()
		local State = env.require("state")
		local RigManager = env.require("Services.RigManager")
		local AnimationManager = env.require("Services.AnimationManager")
		local BlenderSyncManager = env.require("Services.BlenderSyncManager")
		State.rigManager = RigManager.new({ stopRigTrack = function() end }, nil)
		local model, parts = Rigs.template("TemplateR6")
		State.activeRigModel = model
		State.activeRig = Rig.new(model)
		State.serverPort:set(port)
		State.selectedArmature:set({ name = armatureName })
		local sync = BlenderSyncManager.new({}, AnimationManager.new({ playAllRigs = function() end }, nil))
		sync.blenderConnectionService = connection

		local function export()
			local body, kind, message = connection:ImportAnimation(port, armatureName, nil, false)
			H.expectTrue(body ~= nil, "export body (" .. tostring(kind) .. ": " .. tostring(message) .. ")")
			return AnimationSerializer.new():deserialize(body, true)
		end
		local function status(hash)
			task.wait(0.15) -- one status check per 0.1 s
			return connection:CheckAnimationStatus(port, armatureName, hash)
		end
		local function cframe(components)
			return CFrame.new(table.unpack(components, 1, 12))
		end
		local function expectSameCFrame(actual, expected, label)
			local a, b = { actual:GetComponents() }, { expected:GetComponents() }
			for index = 1, 12 do
				H.expectNear(a[index], b[index], 1e-4, string.format("%s [%d]", label, index))
			end
		end

		local before = export()
		local change = before.rig_changes and before.rig_changes[1]
		H.expectTrue(change ~= nil and change.part1 == "Left Arm" and change.part0 == "Torso", "Blender lists the change")
		local hash = status("").hash
		local shoulder = parts.Torso:FindFirstChild("Left Arm")

		H.expectTrue(sync:importAnimationFromBlender(), "synced from Blender")

		expectSameCFrame(shoulder.C0, cframe(change.c0), "C0")
		expectSameCFrame(shoulder.C1, cframe(change.c1), "C1")
		expectSameCFrame(parts.Torso.CFrame * shoulder.C0 * shoulder.C1:Inverse(), parts["Left Arm"].CFrame, "parts stay put")
		local shown = table.concat(State.activeWarnings:get(), " | ")
		H.expectContains(shown, "Moved the Left Arm joint", "note")
		H.expectTrue(not string.find(shown, "couldn't tell Blender", 1, true), "confirmed: " .. shown)

		-- Studio confirmed it: Blender rebased the bone onto the new joint.
		H.expectTrue(status(hash).has_update, "Blender changed")
		local after = export()
		H.expectEqual(after.rig_changes, nil, "the change is no longer listed")
		local scale = if after.export_info and after.export_info.time_unit == "frames" then 1 / after.export_info.fps else 1
		local poses = State.activeRig.bones["Left Arm"].poses
		local function studioPose(time)
			for poseTime, pose in poses do
				if math.abs(poseTime - time) < 1e-4 then
					return pose.transform
				end
			end
			return nil
		end
		local keysBefore, keysAfter = 0, 0
		for _, keyframe in before.kfs do
			if keyframe.kf["Left Arm"] then
				keysBefore += 1
			end
		end
		for _, keyframe in after.kfs do
			local pose = keyframe.kf["Left Arm"]
			if pose then
				keysAfter += 1
				-- Studio played the export it applied the way Blender sends it now.
				local played = studioPose(keyframe.t * scale)
				H.expectTrue(played ~= nil, "Studio has a key at " .. keyframe.t)
				expectSameCFrame(played, cframe(pose[1]), "pose at " .. keyframe.t)
			end
		end
		H.expectTrue(keysAfter >= 2 and keysAfter < keysBefore, string.format("sparse keys again (%d -> %d)", keysBefore, keysAfter))
	end)

	it("Studio -> Blender: import reports what it did", function()
		local kfs = studioRig:ToRobloxAnimation()
		local marker = H.Instance.new("KeyframeMarker")
		marker.Name = "Hit"
		marker.Value = "sword"
		kfs:GetKeyframes()[1]:AddMarker(marker)
		local data = AnimationSerializer.new():serialize(kfs, studioRig)
		local ok, summary = connection:ExportAnimation(port, data, armatureName)
		H.expectTrue(ok, "export to Blender: " .. tostring(summary))
		H.expectContains(summary, "Imported", "summary")
		H.expectContains(summary, "kept", "previous action kept")
	end)

	it("a web page is turned away", function()
		local response = net.request({
			url = string.format("http://127.0.0.1:%d/list_armatures", port),
			method = "GET",
			headers = { Origin = "https://evil.example" },
		})
		H.expectEqual(response.statusCode, 403, "status")
		H.expectEqual(response.headers["access-control-allow-origin"], nil, "no CORS header")
	end)

	it("an unknown armature gives a readable error", function()
		local body, kind, message = connection:ImportAnimation(port, "NoSuchRig", nil, false)
		H.expectEqual(body, nil, "no body")
		H.expectEqual(kind, "error", "kind")
		H.expectContains(message, "not found", "message")
	end)
end)

local results = H.results()
for _, failure in results.failures do
	print("FAIL " .. failure)
end
print(string.format("%d passed, %d failed", results.passed, results.failed))
process.exit(if results.failed > 0 then 1 else 0)
