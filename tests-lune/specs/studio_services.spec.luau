-- Studio services: connection state, live-sync watchdog, load reports (C5, C6, A1, A9, P1).

local task = require("@lune/task")
local Rigs = require("../rigs")

return function(H)
	local describe, it = H.describe, H.it

	local function newSyncManager(env)
		local State = env.require("state")
		local RigManager = env.require("Services.RigManager")
		State.rigManager = RigManager.new({ stopRigTrack = function() end }, nil)
		local BlenderSyncManager = env.require("Services.BlenderSyncManager")
		local manager = BlenderSyncManager.new({}, {})
		return manager, State, BlenderSyncManager
	end

	describe("BlenderSyncManager connection state (C5)", function()
		it("a throttled refresh is not a disconnect", function()
			local env = H.load()
			local manager, State = newSyncManager(env)
			State.isServerConnected:set(true)
			State.serverStatus:set("Connected")
			manager.blenderConnectionService = {
				ListArmatures = function()
					return nil, "throttled", nil
				end,
			}
			H.expectEqual(manager:updateAvailableArmatures(), true, "still connected")
			H.expectEqual(State.serverStatus:get(), "Connected", "status unchanged")
		end)

		it("an offline Blender says why, once", function()
			local env = H.load()
			local manager, State = newSyncManager(env)
			manager.blenderConnectionService = {
				ListArmatures = function()
					return nil, "offline", "Can't reach Blender on port 31337."
				end,
			}
			H.expectEqual(manager:updateAvailableArmatures(), false, "failed")
			manager:updateAvailableArmatures()
			local warnings = State.activeWarnings:get()
			H.expectEqual(#warnings, 1, "one warning")
			H.expectEqual(warnings[1], "Can't reach Blender on port 31337.", "warning text")
		end)

		it("a connected Blender names its file", function()
			local env = H.load()
			local manager, State = newSyncManager(env)
			manager.blenderConnectionService = {
				ListArmatures = function()
					return { { name = "Rig" }, { name = "Other" } }, nil, nil
				end,
				Ping = function()
					return { blend_file = "Knight.blend" }, nil, nil
				end,
			}
			H.expectEqual(manager:updateAvailableArmatures(), true, "connected")
			H.expectEqual(State.serverStatus:get(), "Connected to Knight.blend", "status")
		end)

		it("import failures pass Blender's reason on", function()
			local env = H.load()
			local manager, State = newSyncManager(env)
			State.selectedArmature:set({ name = "Rig" })
			manager.blenderConnectionService = {
				ImportAnimation = function()
					return nil, "error", "Blender: 'Rig' is in Edit Mode. Leave Edit Mode to sync."
				end,
			}
			H.expectEqual(manager:importAnimationFromBlender(), false, "failed")
			H.expectContains(table.concat(State.activeWarnings:get(), "\n"), "Edit Mode", "reason shown")
		end)
	end)

	describe("Live-sync watchdog (C6)", function()
		it("an unanswered request is retried instead of stalling", function()
			local env = H.load()
			local manager, State, BlenderSyncManager = newSyncManager(env)
			BlenderSyncManager.syncRequestTimeout = 0.05
			State.selectedArmature:set({ name = "Rig" })
			local sent = {}
			local client = {
				Send = function(_self, message)
					table.insert(sent, message)
				end,
			}
			manager.webStreamClient = client
			manager.pendingLiveSyncHash = "hash-1"
			manager:_drainLiveSyncUpdates(client)
			H.expectEqual(#sent, 1, "request sent")
			H.expectTrue(manager.isLiveImportInFlight, "in flight")
			task.wait(0.08)
			H.expectEqual(#sent, 2, "request retried after the timeout")
			-- Live sync must not poll in this test; it only checks the fallback.
			State.liveSyncEnabled:set(false)
			task.wait(0.2)
			H.expectEqual(manager.webStreamClient, nil, "fell back after repeated timeouts")
			H.expectEqual(#sent, 3, "no endless retries")
			BlenderSyncManager.syncRequestTimeout = 15
		end)
	end)

	describe("AnimationManager reports (A1, A3, A9, P1)", function()
		it("unmatched channels and Blender warnings become warnings", function()
			local env = H.load()
			local State = env.require("state")
			local RigManager = env.require("Services.RigManager")
			State.rigManager = RigManager.new({ stopRigTrack = function() end }, nil)
			local AnimationManager = env.require("Services.AnimationManager")
			local Rig = env.require("Components.Rig")
			local manager = AnimationManager.new({}, nil)
			State.activeRig = Rig.new((Rigs.r6()))
			State.activeRig.lastLoadReport = { unmatched = { "MyArm" }, ambiguous = {} }
			manager:reportLoadProblems({ export_info = { warnings = { "Roblox can't play bone scale." } } })
			local warnings = table.concat(State.activeWarnings:get(), "\n")
			H.expectContains(warnings, "MyArm", "unmatched listed")
			H.expectContains(warnings, "Sync Bones", "next step suggested")
			H.expectContains(warnings, "Blender: Roblox can't play bone scale.", "export warning")
		end)

		it("lists active modifiers for the Save/Upload buttons", function()
			local env = H.load()
			local State = env.require("state")
			local AnimationManager = env.require("Services.AnimationManager")
			H.expectEqual(#AnimationManager.activeModifierNames(), 0, "none by default")
			State.mirrorAnimationEnabled:set(true)
			State.speedEnabled:set(true)
			State.speedMultiplier:set(1.5)
			local names = table.concat(AnimationManager.activeModifierNames(), ", ")
			H.expectContains(names, "Mirror", "mirror")
			H.expectContains(names, "Speed x1.50", "speed")
		end)
	end)
end
