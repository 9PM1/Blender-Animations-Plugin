--!native
--!strict
--!optimize 2

--[=[
	This module handles all direct HTTP communication with the Blender addon server.
	It is designed to be a stateless service, with dependencies like HttpService
	and the server port passed in during construction or on each method call.

	Every call returns its result, or nil plus a failure kind and a message the
	UI can show as is:
	  "busy"       Blender is running something modal (dialog, render, drag);
	  "offline"    nothing answered on that port;
	  "permission" Studio blocked the plugin's HTTP access;
	  "error"      Blender answered with an error (its message is passed on).
]=]

type HttpMethod = "CONNECT" | "DELETE" | "GET" | "HEAD" | "OPTIONS" | "PATCH" | "POST" | "PUT" | "TRACE"

local BlenderConnection = {}
BlenderConnection.__index = BlenderConnection

type self = {
	HttpService: HttpService,
	_inFlight: { [string]: boolean },
	_lastRequestTime: { [string]: number },
	lastFailureKind: string?,
	lastFailureMessage: string?,
}

function BlenderConnection.new(httpService: HttpService)
	local self: self = {
		HttpService = httpService,
		_inFlight = {},
		_lastRequestTime = {},
		lastFailureKind = nil,
		lastFailureMessage = nil,
	}
	return setmetatable(self, BlenderConnection)
end

local function baseUrl(port: number): string
	return string.format("http://localhost:%d", port)
end

--[=[
	Turn a failed RequestAsync (thrown error or non-2xx response) into a
	(kind, message) pair.
]=]
function BlenderConnection:describeFailure(port: number, response: any, thrown: any): (string, string)
	if type(response) == "table" and response.StatusCode then
		local message = response.StatusMessage or "Unknown error"
		local okJson, data = pcall(function()
			return self.HttpService:JSONDecode(response.Body)
		end)
		if okJson and type(data) == "table" and type(data.message) == "string" then
			message = data.message
		end
		if response.StatusCode == 503 then
			return "busy", message
		end
		return "error", string.format("Blender: %s", tostring(message))
	end
	local text = tostring(thrown or response)
	local lowered = string.lower(text)
	if
		string.find(lowered, "http requests are not enabled", 1, true)
		or string.find(lowered, "permission", 1, true)
		or string.find(lowered, "not allowed", 1, true)
	then
		return "permission",
			"Studio blocked this plugin's HTTP access. Allow HTTP requests for the plugin (Plugins tab > Manage Plugins > its permissions), then connect again."
	end
	if
		string.find(lowered, "connect", 1, true)
		or string.find(lowered, "refused", 1, true)
		or string.find(lowered, "timed out", 1, true)
		or string.find(lowered, "timeout", 1, true)
		or string.find(lowered, "httperror", 1, true)
	then
		return "offline",
			string.format(
				"Can't reach Blender on port %d. Start the server in Blender's RBXMonkey panel and use the same port here.",
				port
			)
	end
	return "error", text
end

function BlenderConnection:_fail(port: number, response: any, thrown: any): (nil, string, string)
	local kind, message = self:describeFailure(port, response, thrown)
	self.lastFailureKind = kind
	self.lastFailureMessage = message
	return nil, kind, message
end

-- Returns the successful response, or nil, kind, message.
function BlenderConnection:_request(port: number, options: { [string]: any }): (any, string?, string?)
	local ok, response = pcall(function()
		return self.HttpService:RequestAsync(options :: any)
	end)
	if not ok then
		return self:_fail(port, nil, response)
	end
	if not response or not response.Success then
		return self:_fail(port, response, nil)
	end
	self.lastFailureKind = nil
	self.lastFailureMessage = nil
	return response, nil, nil
end

function BlenderConnection:_beginRequest(key: string, cooldown: number?): boolean
	local now = os.clock()
	local last = self._lastRequestTime[key]
	if self._inFlight[key] then
		return false
	end
	if cooldown and last and (now - last) < cooldown then
		return false
	end
	self._inFlight[key] = true
	self._lastRequestTime[key] = now
	return true
end

function BlenderConnection:_endRequest(key: string)
	self._inFlight[key] = nil
end

local function validPort(port: any): boolean
	return type(port) == "number" and port > 0
end

-- Who is listening: the Blender file name and whether Blender is busy.
function BlenderConnection:Ping(port: number): (any, string?, string?)
	if not validPort(port) then
		return nil, "error", "Invalid port."
	end
	local response, kind, message = self:_request(port, {
		Url = baseUrl(port) .. "/ping",
		Method = "GET" :: HttpMethod,
		Compress = Enum.HttpCompression.None,
	})
	if not response then
		return nil, kind, message
	end
	local decodeOk, data = pcall(function()
		return self.HttpService:JSONDecode(response.Body)
	end)
	if not decodeOk or type(data) ~= "table" then
		-- An older addon without /ping: still a Blender server.
		return {}, nil, nil
	end
	return data, nil, nil
end

function BlenderConnection:ListArmatures(port: number): (any, string?, string?)
	if not validPort(port) then
		warn("Invalid port for ListArmatures")
		return nil, "error", "Invalid port."
	end
	if not self:_beginRequest("list_armatures", 1.0) then
		return nil, "throttled", nil
	end

	local response, kind, message = self:_request(port, {
		Url = baseUrl(port) .. "/list_armatures",
		Method = "GET" :: HttpMethod,
		Compress = Enum.HttpCompression.None,
	})
	self:_endRequest("list_armatures")

	if not response then
		warn("Failed to get armatures:", message)
		return nil, kind, message
	end

	local decodeSuccess, data = pcall(function()
		return self.HttpService:JSONDecode(response.Body)
	end)

	if not decodeSuccess or type(data) ~= "table" then
		warn("Failed to decode armature list:", data)
		return nil, "error", "Blender sent an armature list Studio couldn't read."
	end

	return data.armatures, nil, nil
end

function BlenderConnection:ImportAnimation(port: number, armatureName: string, targetBoneRest: any?, livePreview: boolean?): (string?, string?, string?)
	if not validPort(port) then
		warn("Invalid port for ImportAnimation")
		return nil, "error", "Invalid port."
	end
	if type(armatureName) ~= "string" or #armatureName == 0 then
		warn("Invalid armature name for ImportAnimation")
		return nil, "error", "No armature selected."
	end

	local url = string.format("%s/export_animation/%s", baseUrl(port), self.HttpService:UrlEncode(armatureName))
	if livePreview then
		url ..= "?live_preview=1"
	end
	local options: { [string]: any } = {
		Url = url,
		Method = "GET" :: HttpMethod,
		Headers = { ["Accept"] = "application/octet-stream" },
		Compress = Enum.HttpCompression.None,
	}
	if targetBoneRest then
		options.Method = "POST" :: HttpMethod
		options.Body = self.HttpService:JSONEncode({ target_bone_rest = targetBoneRest })
		options.Headers = {
			["Accept"] = "application/octet-stream",
			["Content-Type"] = "application/json",
		}
	end

	local response, kind, message = self:_request(port, options)
	if not response then
		warn("Failed to import animation: " .. tostring(message))
		return nil, kind, message
	end
	return response.Body, nil, nil
end

function BlenderConnection:ImportAnimationDelta(port: number, armatureName: string, targetBoneRest: any?, baseHash: string): (string?, string?, string?)
	if not validPort(port) or type(armatureName) ~= "string" or armatureName == "" then
		return nil, "error", "Invalid port or armature."
	end
	local response, kind, message = self:_request(port, {
		Url = string.format("%s/export_animation_delta/%s", baseUrl(port), self.HttpService:UrlEncode(armatureName)),
		Method = "POST" :: HttpMethod,
		Body = self.HttpService:JSONEncode({
			target_bone_rest = targetBoneRest,
			base_hash = baseHash,
		}),
		Headers = {
			["Accept"] = "application/octet-stream",
			["Content-Type"] = "application/json",
		},
		Compress = Enum.HttpCompression.None,
	})
	if not response then
		return nil, kind, message
	end
	return response.Body, nil, nil
end

function BlenderConnection:ExportAnimation(port: number, animationData: any, targetArmature: string?): (boolean, string?)
	if not validPort(port) then
		warn("Invalid port for ExportAnimation")
		return false, "Invalid port."
	end

	local okEncode, encoded = pcall(function()
		return self.HttpService:JSONEncode(animationData)
	end)
	if not okEncode or not encoded then
		warn("Failed to encode animation data for export: " .. tostring(encoded))
		return false, "Couldn't encode the animation."
	end

	local url = baseUrl(port) .. "/import_animation"
	if targetArmature then
		url = url .. "?armature=" .. self.HttpService:UrlEncode(targetArmature)
	end
	local response, _kind, message = self:_request(port, {
		Url = url,
		Method = "POST" :: HttpMethod,
		Headers = {
			["Content-Type"] = "application/json",
		},
		Body = encoded,
		Compress = Enum.HttpCompression.None, -- Disable compression for faster local transfers
	})

	if not response then
		warn("Failed to export animation to Blender: " .. tostring(message))
		return false, message
	end

	local summary = nil
	local okJson, data = pcall(function()
		return self.HttpService:JSONDecode(response.Body)
	end)
	if okJson and type(data) == "table" and type(data.message) == "string" then
		summary = data.message
	end
	print("Successfully exported animation to Blender." .. (if summary then " " .. summary else ""))
	return true, summary
end

-- Send back the `rig_changes` entries the Studio rig has now, so Blender
-- rebases those bones. Returns Blender's answer ({ applied, skipped }).
function BlenderConnection:ConfirmRigChanges(port: number, armatureName: string, changes: { any }): (any, string?, string?)
	if not validPort(port) then
		return nil, "error", "Invalid port."
	end
	if type(armatureName) ~= "string" or #armatureName == 0 then
		return nil, "error", "No armature selected."
	end

	local response, kind, message = self:_request(port, {
		Url = string.format("%s/confirm_rig_changes/%s", baseUrl(port), self.HttpService:UrlEncode(armatureName)),
		Method = "POST" :: HttpMethod,
		Headers = { ["Content-Type"] = "application/json" },
		Body = self.HttpService:JSONEncode({ changes = changes }),
		Compress = Enum.HttpCompression.None,
	})
	if not response then
		return nil, kind, message
	end

	local decodeSuccess, data = pcall(function()
		return self.HttpService:JSONDecode(response.Body)
	end)
	if not decodeSuccess or type(data) ~= "table" then
		return nil, "error", "Blender sent an answer Studio couldn't read."
	end
	return data, nil, nil
end

function BlenderConnection:CheckAnimationStatus(port: number, armatureName: string, lastKnownHash: string): (any, string?, string?)
	if not validPort(port) then
		return nil, "error", "Invalid port."
	end
	if type(armatureName) ~= "string" or #armatureName == 0 then
		return nil, "error", "No armature selected."
	end
	if not self:_beginRequest("animation_status", 0.1) then
		return nil, "throttled", nil
	end

	local response, kind, message = self:_request(port, {
		Url = string.format(
			"%s/animation_status?armature=%s&last_known_hash=%s",
			baseUrl(port),
			self.HttpService:UrlEncode(armatureName),
			self.HttpService:UrlEncode(lastKnownHash or "")
		),
		Method = "GET" :: HttpMethod,
		Compress = Enum.HttpCompression.None,
	})
	self:_endRequest("animation_status")

	if not response then
		return nil, kind, message
	end

	local decodeSuccess, data = pcall(function()
		return self.HttpService:JSONDecode(response.Body)
	end)
	if not decodeSuccess or type(data) ~= "table" then
		return nil, "error", "Blender sent a status Studio couldn't read."
	end
	return data, nil, nil
end

function BlenderConnection:GetBoneRest(port: number, armatureName: string): (any, string?, string?)
	if not validPort(port) then
		warn("Invalid port for GetBoneRest")
		return nil, "error", "Invalid port."
	end
	if type(armatureName) ~= "string" or #armatureName == 0 then
		warn("Invalid armature name for GetBoneRest")
		return nil, "error", "No armature selected."
	end

	-- RequestAsync, not GetAsync: GetAsync may serve a cached (stale) rest pose.
	local response, kind, message = self:_request(port, {
		Url = string.format("%s/get_bone_rest/%s", baseUrl(port), self.HttpService:UrlEncode(armatureName)),
		Method = "GET" :: HttpMethod,
		Compress = Enum.HttpCompression.None,
	})
	if not response then
		warn("Failed to get bone rest poses:", message)
		return nil, kind, message
	end

	local decodeSuccess, data = pcall(function()
		return self.HttpService:JSONDecode(response.Body)
	end)
	if not decodeSuccess then
		warn("Failed to decode bone rest data:", data)
		return nil, "error", "Blender sent rest data Studio couldn't read."
	end
	return data, nil, nil
end

return BlenderConnection
