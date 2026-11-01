--!native
--!strict
--!optimize 2

local State = require(script.Parent.Parent.state)
local _Types = require(script.Parent.Parent.types)
local _PlaybackService = require(script.Parent.PlaybackService)

local BlenderConnection = require(script.Parent.Parent.Components.BlenderConnection)
local RigChanges = require(script.Parent.Parent.Components.RigChanges)

local BlenderSyncManager = {}
BlenderSyncManager.__index = BlenderSyncManager

local REST_DISTANCE_EPSILON = 1e-5
-- A WebSocket sync request with no answer by then is retried (it used to
-- leave live sync waiting forever).
local SYNC_REQUEST_TIMEOUT = 15
BlenderSyncManager.syncRequestTimeout = SYNC_REQUEST_TIMEOUT
-- Confirming applied joint changes to Blender: tries, and the wait between.
local RIG_CONFIRM_ATTEMPTS = 3
BlenderSyncManager.rigConfirmRetrySeconds = 2

local function showProblem(message: string?)
	if not message or message == "" then
		return
	end
	warn("[Blender Sync] " .. message)
	local rigManager = (State :: any).rigManager
	if rigManager and rigManager.addWarning then
		rigManager:addWarning(message)
	end
end

local function clearProblem(message: string?)
	local rigManager = (State :: any).rigManager
	if message and rigManager and rigManager.removeWarning then
		rigManager:removeWarning(message)
	end
end

local function buildTargetBoneRestCalibration(activeRig: any): any?
	if not activeRig or not activeRig.bones then
		return nil
	end

	local bones = {}
	local boneCount = 0
	for boneName, rigPart in pairs(activeRig.bones) do
		local part = rigPart and rigPart.part
		if typeof(part) == "Instance" and part:IsA("Bone") then
			local position = part.CFrame.Position
			local distance = position.Magnitude
			if distance > REST_DISTANCE_EPSILON then
				bones[boneName] = {
					parent = part.Parent and part.Parent.Name or nil,
					distance = distance,
				}
				boneCount += 1
			end
		end
	end

	if boneCount == 0 then
		return nil
	end

	return {
		rig_name = activeRig.model and activeRig.model.Name or nil,
		bone_count = boneCount,
		bones = bones,
	}
end

function BlenderSyncManager.new(playbackService: any, animationManager: any)
	local self = setmetatable({}, BlenderSyncManager)
	
	self.playbackService = playbackService
	self.animationManager = animationManager
	self.blenderConnectionService = BlenderConnection.new(game:GetService("HttpService")) :: any
	self.liveSyncCoroutine = nil :: thread?
	self.periodicRefreshCoroutine = nil :: thread?
	self.autoConnectAttempts = 0
	self.maxAutoConnectAttempts = 3
	self.autoConnectLastAttemptTime = 0
	self.autoConnectCooldown = 5 -- seconds between retry attempts
	self.restCalibrationRig = nil :: any?
	self.restCalibration = nil :: any?
	self.httpService = game:GetService("HttpService")
	self.webStreamClient = nil :: any?
	self.webStreamConnections = {} :: { RBXScriptConnection }
	self.isLiveImportInFlight = false
	self.liveSyncRevision = ""
	self.pendingLiveSyncHash = nil :: string?
	self.webSocketRequestSerial = 0
	self.webSocketRequestId = nil :: string?
	self.webSocketRequestBaseHash = ""
	self.liveSyncPreviewScheduled = false
	self.lastLiveSyncPreviewBuild = 0
	-- Joint changes the rig has and Blender hasn't confirmed yet.
	self.pendingRigConfirmation = nil :: any?
	self.isConfirmingRigChanges = false
	self.rigConfirmMessages = {} :: { string }
	-- Motor6D -> the change Blender rebased onto, to notice an undo.
	self.confirmedRigJoints = {} :: { [Instance]: any }
	self.undoneJointMessages = {} :: { [string]: boolean }
	self.historyConnections = {} :: { RBXScriptConnection }
	local historyOk, history = pcall(function()
		return game:GetService("ChangeHistoryService")
	end)
	if historyOk and history then
		for _, signalName in ipairs({ "OnUndo", "OnRedo" }) do
			local signalOk, signal = pcall(function()
				return (history :: any)[signalName]
			end)
			if signalOk and signal then
				table.insert(self.historyConnections, signal:Connect(function()
					self:_checkConfirmedJoints()
				end))
			end
		end
	end

	return self
end

function BlenderSyncManager:updateAvailableArmatures()
	local status, result, failureKind, failureMessage = pcall(function()
		return self.blenderConnectionService:ListArmatures(State.serverPort:get())
	end)

	if status and not result and failureKind == "throttled" then
		-- A refresh is already in flight; that's not a disconnect.
		return State.isServerConnected:get()
	end
	if status and not result and failureKind == "busy" then
		State.serverStatus:set("Connected (Blender is busy)")
		return true
	end

	if not status or not result then
		State.availableArmatures:set({})
		State.serverStatus:set("Disconnected")
		local message = if status then failureMessage else ("Error listing armatures: " .. tostring(result))
		if self.lastConnectionProblem ~= message then
			clearProblem(self.lastConnectionProblem)
			self.lastConnectionProblem = message
			showProblem(message)
		end
		return false
	end

	clearProblem(self.lastConnectionProblem)
	self.lastConnectionProblem = nil

	local armatures = result
	State.availableArmatures:set(armatures)
	-- Name the Blender file, so two open Blenders can't be confused.
	local pingOk, info = pcall(function()
		return self.blenderConnectionService:Ping(State.serverPort:get())
	end)
	local blendFile = if pingOk and type(info) == "table" then info.blend_file else nil
	if type(blendFile) == "string" and blendFile ~= "" then
		State.serverStatus:set("Connected to " .. blendFile)
	else
		State.serverStatus:set("Connected")
	end
	
	-- Auto-select if there's only one armature and none is currently selected
	if #armatures == 1 and not State.selectedArmature:get() then
		State.selectedArmature:set(armatures[1])
		print("Auto-selected single armature:", armatures[1].name)
		
		-- Auto-start live sync if enabled and there's only one armature
		if State.liveSyncEnabled:get() and State.isServerConnected:get() then
			print("Auto-starting live sync for single armature:", armatures[1].name)
			self:startLiveSyncing()
		end
	end
	
	return true
end

function BlenderSyncManager:importAnimationFromBlender(livePreview: boolean?)
	if not State.selectedArmature:get() then
		warn("No armature selected")
		return false
	end

	local armature = State.selectedArmature:get()
	if not armature then
		warn("No armature selected")
		return false
	end

	local targetBoneRest = self:_getRestCalibration()
	local responseBody, failureKind, failureMessage = self.blenderConnectionService:ImportAnimation(
		State.serverPort:get(),
		(armature :: any).name,
		targetBoneRest,
		livePreview
	)

	if not responseBody and failureKind == "busy" then
		State.serverStatus:set("Connected (Blender is busy)")
		return false
	end
	if not responseBody then
		-- Say why (Edit Mode, missing armature...), not just "failed".
		showProblem(failureMessage)
		return false
	end

	if responseBody then
		-- The response is binary, so we pass `true`
		local success = self.animationManager:loadAnimDataFromText(responseBody, true, { applyRigChanges = true })
		if success then
			-- Don't set the hash here, it will be set in the polling loop
			self:_confirmRigChanges((armature :: any).name)
			self:_checkConfirmedJoints()
		end
		return success
	else
		warn("Failed to import animation from blender.")
		return false
	end
end

function BlenderSyncManager:exportAnimationToBlender()
	if not State.isServerConnected:get() then
		warn("Not connected to Blender server.")
		return false
	end

	if not State.activeRig then
		warn("No active rig found to serialize animation from.")
		return false
	end
	if not self.animationManager then
		warn("Animation manager is unavailable.")
		return false
	end

	-- The preview sequence is ephemeral and can be nil until playback has run.
	-- Rebuild directly from the rig so export always reflects the current editor state.
	local keyframeSequence = self.animationManager:createKeyframeSequenceFromState()
	if not keyframeSequence then
		warn("No active animation data to export.")
		return false
	end

	local AnimationSerializer = require(script.Parent.Parent.Components.AnimationSerializer)
	local animationSerializerService = AnimationSerializer.new()
	
	local animData = animationSerializerService:serialize(keyframeSequence, State.activeRig)
	keyframeSequence:Destroy()
	if not animData then
		warn("Failed to serialize animation.")
		return false
	end

	-- Deform animation translations are normalized when Blender sends them to
	-- Studio. Preserve that calibration so Blender can undo it on the return trip.
	if State.activeRig.isDeformRig then
		local sourceData = State.currentAnimationData:get() or State.lastRawAnimData:get()
		local exportInfo = if type(sourceData) == "table" then sourceData.export_info else nil
		if type(exportInfo) == "table" then
			(animData :: any).export_info = table.clone(exportInfo)
		end
	end

	-- Get target armature from selected armature
	local targetArmature = nil
	if State.selectedArmature:get() then
		targetArmature = (State.selectedArmature:get() :: any).name
	end
	-- The poses are relative to the joints the rig has: Blender needs them too.
	if targetArmature and self.pendingRigConfirmation then
		self:_confirmRigChanges(targetArmature)
	end

	local ok, message = self.blenderConnectionService:ExportAnimation(State.serverPort:get(), animData, targetArmature)
	if not ok then
		showProblem(message)
	end
	return ok
end

function BlenderSyncManager:stopLiveSyncing()
	self:_closeWebSocket()
	self.liveSyncRevision = ""
	self.pendingLiveSyncHash = nil
	if self.liveSyncCoroutine then
		coroutine.close(self.liveSyncCoroutine :: thread)
		self.liveSyncCoroutine = nil
		print("Live sync stopped.")
	end
end

function BlenderSyncManager:_getRestCalibration(): any?
	if self.restCalibrationRig ~= State.activeRig then
		self.restCalibrationRig = State.activeRig
		self.restCalibration = buildTargetBoneRestCalibration(State.activeRig)
		self.liveSyncRevision = ""
	end
	return self.restCalibration
end

--[=[
	Tell Blender which of its queued joint changes the rig has now (see
	RigChanges), so it rebases those bones and stops sending them. Until it
	has, every export still lists them and Studio converts their poses, so
	a failed confirmation only costs a retry: a few times here, then on the
	next sync.
]=]
function BlenderSyncManager:_confirmRigChanges(armatureName: string)
	local animationManager = self.animationManager :: any
	local confirmations = if animationManager and animationManager.takeRigChangeConfirmations
		then animationManager:takeRigChangeConfirmations()
		else nil
	if confirmations ~= nil then
		-- A full load says what Blender still lists.
		self.pendingRigConfirmation = if #confirmations > 0
			then { armature = armatureName, outcomes = confirmations, attempts = 0 }
			else nil
	end
	local pending = self.pendingRigConfirmation
	if not pending or pending.armature ~= armatureName or self.isConfirmingRigChanges then
		return
	end

	local entries = {}
	for _, outcome in ipairs(pending.outcomes) do
		table.insert(entries, outcome.change.entry)
	end
	self.isConfirmingRigChanges = true
	local callOk, data, _kind, message = pcall(function()
		return self.blenderConnectionService:ConfirmRigChanges(State.serverPort:get(), armatureName, entries)
	end)
	self.isConfirmingRigChanges = false
	if self.pendingRigConfirmation ~= pending then
		-- A newer load replaced it meanwhile.
		return
	end

	for _, shown in ipairs(self.rigConfirmMessages) do
		clearProblem(shown)
	end
	self.rigConfirmMessages = {}
	local function show(text: string)
		table.insert(self.rigConfirmMessages, text)
		showProblem(text)
	end

	if not callOk or type(data) ~= "table" then
		pending.attempts += 1
		local reason = if callOk then message else data
		show(string.format(
			"Moved the joints Blender sent, but couldn't tell Blender (%s). Studio tries again on the next sync.",
			tostring(reason or "no answer")
		))
		if pending.attempts < RIG_CONFIRM_ATTEMPTS then
			task.delay(BlenderSyncManager.rigConfirmRetrySeconds, function()
				self:_confirmRigChanges(armatureName)
			end)
		end
		return
	end

	self.pendingRigConfirmation = nil
	local byPart = {}
	for _, outcome in ipairs(pending.outcomes) do
		byPart[outcome.change.part1] = outcome
	end
	for _, part1 in ipairs(if type(data.applied) == "table" then data.applied else {}) do
		local outcome = byPart[part1]
		if outcome and outcome.joint then
			self.confirmedRigJoints[outcome.joint] = outcome
		end
	end
	for _, skipped in ipairs(if type(data.skipped) == "table" then data.skipped else {}) do
		if type(skipped) == "table" then
			show(string.format("Blender kept its old joint for %s: %s", tostring(skipped.part1), tostring(skipped.reason)))
		end
	end
end

-- A joint Blender rebased onto can be put back by an undo in Studio; then
-- Blender's animation of it plays wrong here, so say so.
function BlenderSyncManager:_checkConfirmedJoints()
	local rig = State.activeRig :: any
	local model = rig and rig.model
	for joint, outcome in pairs(self.confirmedRigJoints) do
		local message = RigChanges.describeUndone(outcome)
		local inRig = joint.Parent ~= nil and model ~= nil and joint:IsDescendantOf(model)
		if joint.Parent == nil then
			self.confirmedRigJoints[joint] = nil
		end
		if inRig and RigChanges.isUndone(outcome) then
			if not self.undoneJointMessages[message] then
				self.undoneJointMessages[message] = true
				showProblem(message)
			end
		elseif self.undoneJointMessages[message] then
			self.undoneJointMessages[message] = nil
			clearProblem(message)
		end
	end
end

function BlenderSyncManager:_drainLiveSyncUpdates(client: any)
	if self.isLiveImportInFlight or not self.pendingLiveSyncHash or self.webStreamClient ~= client then
		return
	end
	local armature = State.selectedArmature:get()
	if not armature then
		return
	end
	local triggerHash = self.pendingLiveSyncHash
	self.pendingLiveSyncHash = nil
	self.webSocketRequestSerial += 1
	local requestId = tostring(self.webSocketRequestSerial)
	self.webSocketRequestId = requestId
	self.webSocketRequestBaseHash = self.liveSyncRevision
	self.isLiveImportInFlight = true
	-- Watchdog: an unanswered request must not stall live sync forever.
	task.delay(BlenderSyncManager.syncRequestTimeout, function()
		if self.webStreamClient == client and self.webSocketRequestId == requestId then
			self.isLiveImportInFlight = false
			self.webSocketRequestId = nil
			self.webSocketTimeouts = (self.webSocketTimeouts or 0) + 1
			if self.webSocketTimeouts >= 3 then
				-- The socket is open but Blender isn't answering: use HTTP
				-- polling, which notices a closed Blender.
				warn("[Blender Sync] Blender stopped answering live-sync requests; switching to polling.")
				self.webSocketTimeouts = 0
				self:_closeWebSocket()
				if State.liveSyncEnabled:get() and State.isServerConnected:get() then
					self:_startPolling()
				end
				return
			end
			warn("[Blender Sync] Blender didn't answer a live-sync request; retrying.")
			self.pendingLiveSyncHash = self.pendingLiveSyncHash or triggerHash
			self:_drainLiveSyncUpdates(client)
		end
	end)
	local sent = pcall(function()
		client:Send(self.httpService:JSONEncode({
			type = "sync_request",
			request_id = requestId,
			trigger_hash = triggerHash,
			armature = (armature :: any).name,
			base_hash = self.webSocketRequestBaseHash,
			target_bone_rest = self:_getRestCalibration(),
		}))
	end)
	if not sent then
		self.isLiveImportInFlight = false
		self.webSocketRequestId = nil
		self.pendingLiveSyncHash = triggerHash
	end
end

function BlenderSyncManager:_handleWebSocketPayload(client: any, update: any)
	if self.webStreamClient ~= client or update.request_id ~= self.webSocketRequestId then
		return
	end
	local triggerHash = update.trigger_hash
	local expectedBaseHash = self.webSocketRequestBaseHash
	self.isLiveImportInFlight = false
	self.webSocketRequestId = nil
	if update.type ~= "sync_payload" or type(update.data) ~= "string" then
		warn("Live sync payload failed:", update.error or "unknown error")
		if self.pendingLiveSyncHash then
			self:_drainLiveSyncUpdates(client)
		end
		return
	end

	local decoded, envelope = pcall(function()
		return self.animationManager.animationSerializerService:deserialize(update.data, false)
	end)
	local applied = false
	if decoded and type(envelope) == "table" then
		local ok, result = pcall(function()
			return self.animationManager:applyLiveSyncEnvelope(envelope, expectedBaseHash, true)
		end)
		applied = ok and result == true
	end
	if applied then
		self.webSocketTimeouts = 0
		self.liveSyncRevision = envelope.hash
		if type(triggerHash) == "string" then
			State.lastKnownBlenderAnimHash:set(triggerHash)
		end
		local armature = State.selectedArmature:get()
		if armature then
			task.spawn(function()
				self:_confirmRigChanges((armature :: any).name)
			end)
		end
		self:_checkConfirmedJoints()
		if not self.liveSyncPreviewScheduled then
			self.liveSyncPreviewScheduled = true
			local delaySeconds = math.max(0, 0.1 - (os.clock() - self.lastLiveSyncPreviewBuild))
			task.delay(delaySeconds, function()
				self.liveSyncPreviewScheduled = false
				if self.webStreamClient == client then
					pcall(function()
						self.animationManager:rebuildLiveSyncPreview()
					end)
					self.lastLiveSyncPreviewBuild = os.clock()
				end
			end)
		end
	elseif expectedBaseHash ~= "" then
		-- Request a full snapshot once when the local revision is stale.
		self.liveSyncRevision = ""
		self.pendingLiveSyncHash = if type(triggerHash) == "string" then triggerHash else "resync"
	end

	if self.pendingLiveSyncHash then
		self:_drainLiveSyncUpdates(client)
	end
end

function BlenderSyncManager:startLiveSyncing()
	self:stopLiveSyncing() -- Stop any existing sync loops

	if not State.liveSyncEnabled:get() then
		return
	end

	if self:_startWebSocket() then
		return
	end
	self:_startPolling()
end

function BlenderSyncManager:_closeWebSocket()
	for _, connection in ipairs(self.webStreamConnections) do
		connection:Disconnect()
	end
	table.clear(self.webStreamConnections)
	local client = self.webStreamClient
	self.webStreamClient = nil
	if client then
		pcall(function()
			client:Close()
		end)
	end
end

function BlenderSyncManager:_startWebSocket(): boolean
	if type(self.httpService.CreateWebStreamClient) ~= "function" then
		return false
	end
	local armature = State.selectedArmature:get()
	if not armature then
		return false
	end

	local ok, client = pcall(function()
		return self.httpService:CreateWebStreamClient(Enum.WebStreamClientType.WebSocket, {
			Url = string.format("ws://localhost:%d/live_sync", State.serverPort:get() + 1),
		})
	end)
	if not ok or not client then
		return false
	end

	self.webStreamClient = client
	self.liveSyncRevision = ""
	self.pendingLiveSyncHash = nil
	self.webSocketRequestId = nil
	self.isLiveImportInFlight = false
	self.liveSyncPreviewScheduled = false
	self.lastLiveSyncPreviewBuild = 0
	table.insert(self.webStreamConnections, client.Opened:Connect(function()
		if self.webStreamClient ~= client then
			return
		end
		pcall(function()
			client:Send(self.httpService:JSONEncode({
				type = "hello",
				armature = (armature :: any).name,
			}))
		end)
		State.serverStatus:set("Live Sync: WebSocket")
	end))
	table.insert(self.webStreamConnections, client.MessageReceived:Connect(function(message: string)
		local decodedOk, update = pcall(function()
			return self.httpService:JSONDecode(message)
		end)
		if not decodedOk or type(update) ~= "table" then
			return
		end
		if update.type == "sync_payload" or update.type == "sync_error" then
			self:_handleWebSocketPayload(client, update)
			return
		end
		if update.type ~= "animation_changed" then
			return
		end
		local selectedArmature = State.selectedArmature:get()
		if not selectedArmature or update.armature ~= (selectedArmature :: any).name or type(update.hash) ~= "string" then
			return
		end
		self.pendingLiveSyncHash = update.hash
		self:_drainLiveSyncUpdates(client)
	end))

	local function fallBackToPolling()
		if self.webStreamClient ~= client then
			return
		end
		self:_closeWebSocket()
		if State.liveSyncEnabled:get() and State.isServerConnected:get() then
			self:_startPolling()
		end
	end
	table.insert(self.webStreamConnections, client.Error:Connect(fallBackToPolling))
	table.insert(self.webStreamConnections, client.Closed:Connect(fallBackToPolling))
	return true
end

function BlenderSyncManager:_startPolling()

	self.liveSyncCoroutine = coroutine.create(function()
		-- print("Live sync started.")
		local minimumPollInterval = 0.1
		local pollInterval = minimumPollInterval
		local noChangeCount = 0
		local maxPollInterval = 1.0
		local lastArmatureRefresh = 0
		local armatureRefreshInterval = 5.0  -- Refresh armatures every 5 seconds
		local failureCount = 0
		local maxFailuresBeforeStop = 5
		local consecutiveCrashCount = 0
		local maxConsecutiveCrashes = 10
		
		while State.liveSyncEnabled:get() do
			-- Skip polling if widget is not enabled to reduce performance impact
			if not State.widgetsEnabled:get() then
				task.wait(1) -- Wait longer when widget is hidden
				continue
			end
			
			-- Check if we've had too many consecutive crashes
			if consecutiveCrashCount >= maxConsecutiveCrashes then
				warn("Live sync had too many consecutive errors. Stopping to prevent instability.")
				self:cleanupServerConnection()
				break
			end
			
			local isConnected = State.isServerConnected:get()
			local selectedArmature = State.selectedArmature:get()

			if isConnected then
				-- Periodic armature refresh
				local currentTime = os.clock()
				if currentTime - lastArmatureRefresh > armatureRefreshInterval then
					local refreshSuccess = pcall(function()
						self:updateAvailableArmatures()
					end)
					if not refreshSuccess then
						consecutiveCrashCount += 1
					else
						consecutiveCrashCount = 0
					end
					lastArmatureRefresh = currentTime
				end
				
				if selectedArmature then
					local armatureName = (selectedArmature :: any).name
					local lastHash = State.lastKnownBlenderAnimHash:get()
					local serverPort = State.serverPort:get()

					local status, err, failureKind, failureMessage = pcall(
						self.blenderConnectionService.CheckAnimationStatus,
						self.blenderConnectionService,
						serverPort,
						armatureName,
						lastHash
					)
					if status and not err and failureKind == "throttled" then
						task.wait(pollInterval)
						continue
					end
					if status and not err and failureKind == "busy" then
						-- Blender is in a dialog or modal tool: wait, don't disconnect.
						State.serverStatus:set("Live Sync: Blender is busy")
						task.wait(math.max(pollInterval, 0.5))
						continue
					end
					-- A failed request returns nil instead of erroring; it used to count
					-- as "no change", so a closed Blender was never noticed.
					if status and not err then
						status = false
						err = failureMessage
					end

					if not status then
						failureCount += 1
						consecutiveCrashCount += 1
						if State.serverStatus:get() ~= "Live Sync: Connection lost" then
							State.serverStatus:set("Live Sync: Connection lost")
						end
						-- Back off polling on connection loss to avoid socket exhaustion
						pollInterval = math.min(math.max(pollInterval * 2, 0.5), maxPollInterval)
						noChangeCount = 0
						if failureCount >= maxFailuresBeforeStop then
							showProblem(if type(err) == "string" then err else "Lost the connection to Blender.")
							self:cleanupServerConnection()
							break
						end
					else
						failureCount = 0
						consecutiveCrashCount = 0
						local currentStatus = State.serverStatus:get()
						if currentStatus == "Live Sync: Connection lost" or currentStatus == "Live Sync: Blender is busy" then
							State.serverStatus:set("Connected") -- Restore status
						end
						
						if (err :: any) and (err :: any).has_update then
							local importCallSucceeded, importSucceeded = pcall(function()
								return self:importAnimationFromBlender()
							end)
							if importCallSucceeded and importSucceeded then
								State.lastKnownBlenderAnimHash:set((err :: any).hash)
								pollInterval = minimumPollInterval
								noChangeCount = 0
							else
								consecutiveCrashCount += 1
								-- Keep the old hash so a transient failed import is retried.
								pollInterval = math.min(math.max(pollInterval * 2, 0.5), maxPollInterval)
							end
						else
							-- Stay responsive for brief edits, then back off quickly while
							-- Blender is idle instead of issuing 30 requests per second.
							noChangeCount += 1
							local backoffSteps = math.max(0, noChangeCount - 3)
							pollInterval = math.min(minimumPollInterval * (2 ^ backoffSteps), maxPollInterval)
						end
					end
				end
			end
			
			task.wait(pollInterval)
		end
		-- print("Live sync coroutine finished.")
	end)

	if self.liveSyncCoroutine then
		task.spawn(self.liveSyncCoroutine)
	end
end


function BlenderSyncManager:cleanupServerConnection()
	State.isServerConnected:set(false)
	State.serverStatus:set("Disconnected")
	self:stopLiveSyncing() -- Stop live sync when disconnecting
	
	-- Any other network cleanup can go here
end

function BlenderSyncManager:toggleServerConnection()
	if not State.isServerConnected:get() then
		print("Attempting to connect to Blender server...")
		local success = self:updateAvailableArmatures()
		State.isServerConnected:set(success)
		if not success then
			warn("Failed to establish connection")
			self:cleanupServerConnection()
		else
			print("Successfully connected to Blender server")
			self.autoConnectAttempts = 0 -- Reset attempts on success
		end
	else
		self:cleanupServerConnection()
	end
end

function BlenderSyncManager:fetchAnimationFromServer()
	-- ALL LOGIC MOVED TO BlenderConnection.lua
	return false
end

function BlenderSyncManager:cleanup()
	self:stopLiveSyncing()
	self:cleanupServerConnection()
	for _, connection in ipairs(self.historyConnections) do
		connection:Disconnect()
	end
	table.clear(self.historyConnections)
end

return BlenderSyncManager



