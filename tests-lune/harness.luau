--[=[
	Loads the Studio plugin's modules outside Studio, with Lune.

	Modules are loaded straight from src/ with Rojo-style `script` objects, so
	`require(script.Parent.Rig)` works as it does in the plugin. Roblox
	services the plugin touches are small fakes; Instance, CFrame and the
	other datatypes come from Lune's Roblox library, with a few engine
	methods (Model:PivotTo, KeyframeSequence:GetKeyframes...) filled in.

	Run from the repository root:  lune run tests-lune/run
]=]

local fs = require("@lune/fs")
local luau = require("@lune/luau")
local roblox = require("@lune/roblox")
local serde = require("@lune/serde")
local task = require("@lune/task")

local Harness = {}

local PLUGIN_ROOT = "src/ServerScriptService/BlenderAnimationsInternal"

-- ---------------------------------------------------------------------------
-- Engine methods Lune doesn't implement
-- ---------------------------------------------------------------------------

local CFrame = roblox.CFrame

local function sortedChildrenOfClass(instance, className)
	local list = {}
	for _, child in instance:GetChildren() do
		if child:IsA(className) then
			table.insert(list, child)
		end
	end
	return list
end

roblox.implementMethod("KeyframeSequence", "GetKeyframes", function(self)
	return sortedChildrenOfClass(self, "Keyframe")
end)
roblox.implementMethod("Keyframe", "GetMarkers", function(self)
	return sortedChildrenOfClass(self, "KeyframeMarker")
end)
roblox.implementMethod("Keyframe", "AddMarker", function(self, marker)
	marker.Parent = self
end)

local function pivotOf(model)
	local primary = model.PrimaryPart
	if primary then
		return primary.CFrame
	end
	return CFrame.new()
end

roblox.implementMethod("Model", "GetPivot", function(self)
	return pivotOf(self)
end)
roblox.implementMethod("Model", "PivotTo", function(self, target)
	local delta = target * pivotOf(self):Inverse()
	for _, descendant in self:GetDescendants() do
		if descendant:IsA("BasePart") then
			descendant.CFrame = delta * descendant.CFrame
		end
	end
end)
roblox.implementMethod("Model", "GetScale", function(_self)
	return 1
end)

-- Bone.WorldCFrame is computed by the engine: parent part (or bone) * CFrame.
local function boneWorld(bone)
	local parent = bone.Parent
	local base = CFrame.new()
	if parent and parent:IsA("Bone") then
		base = boneWorld(parent)
	elseif parent and parent:IsA("BasePart") then
		base = parent.CFrame
	end
	return base * bone.CFrame
end

-- ---------------------------------------------------------------------------
-- Instance identity
--
-- Roblox returns the same userdata for an Instance every time, so the plugin
-- keys tables by Instance (joint caches, "already visited" sets). Lune makes
-- a new userdata on every property access, which silently breaks those
-- lookups. Every Instance the specs see is wrapped in one interned proxy.
-- ---------------------------------------------------------------------------

local RawInstance = roblox.Instance
local ID_ATTRIBUTE = "__luneProxyId"
local proxyMeta = {}
local proxiesById = {}
local nextProxyId = 0

local function isProxy(value)
	return type(value) == "table" and getmetatable(value) == proxyMeta
end

local function unwrap(value)
	if isProxy(value) then
		return rawget(value, "__raw")
	end
	return value
end

local wrapDeep

local function wrap(value)
	if typeof(value) ~= "Instance" then
		return value
	end
	local id = value:GetAttribute(ID_ATTRIBUTE)
	if id == nil then
		nextProxyId += 1
		id = nextProxyId
		value:SetAttribute(ID_ATTRIBUTE, id)
	end
	local proxy = proxiesById[id]
	if proxy ~= nil and rawget(proxy, "__raw") ~= value then
		-- Clone() copies attributes, so a clone arrives with its source's id.
		nextProxyId += 1
		id = nextProxyId
		value:SetAttribute(ID_ATTRIBUTE, id)
		proxy = nil
	end
	if proxy == nil then
		proxy = setmetatable({ __raw = value }, proxyMeta)
		proxiesById[id] = proxy
	end
	return proxy
end

wrapDeep = function(value)
	if typeof(value) == "Instance" then
		return wrap(value)
	end
	if type(value) == "table" and not isProxy(value) and getmetatable(value) == nil then
		local copy = {}
		for key, item in value do
			copy[key] = wrapDeep(item)
		end
		return copy
	end
	return value
end

proxyMeta.__index = function(proxy, key)
	local raw = rawget(proxy, "__raw")
	if (key == "WorldCFrame" or key == "TransformedWorldCFrame") and raw:IsA("Bone") then
		-- Engine-computed; Lune has no value for it.
		return boneWorld(raw)
	end
	local value = raw[key]
	if type(value) == "function" then
		return function(self, ...)
			local args = table.pack(...)
			for index = 1, args.n do
				args[index] = unwrap(args[index])
			end
			local results = table.pack(value(unwrap(self), table.unpack(args, 1, args.n)))
			for index = 1, results.n do
				results[index] = wrapDeep(results[index])
			end
			return table.unpack(results, 1, results.n)
		end
	end
	return wrapDeep(value)
end
proxyMeta.__newindex = function(proxy, key, value)
	rawget(proxy, "__raw")[key] = unwrap(value)
end
proxyMeta.__tostring = function(proxy)
	return tostring(rawget(proxy, "__raw"))
end

local Instance = {
	new = function(className, parent)
		local proxy = wrap(RawInstance.new(className))
		if parent ~= nil then
			proxy.Parent = parent
		end
		return proxy
	end,
}

local function proxyTypeof(value)
	if isProxy(value) then
		return "Instance"
	end
	return typeof(value)
end

Harness.Instance = Instance
Harness.typeof = proxyTypeof

-- ---------------------------------------------------------------------------
-- Fake services
-- ---------------------------------------------------------------------------

local function newSignal()
	local handlers = {}
	local signal = {}
	function signal:Connect(fn)
		table.insert(handlers, fn)
		local connection = {}
		function connection:Disconnect()
			local index = table.find(handlers, fn)
			if index then
				table.remove(handlers, index)
			end
		end
		return connection
	end
	function signal:Fire(...)
		for _, fn in table.clone(handlers) do
			fn(...)
		end
	end
	return signal
end

local function newHttpService()
	local service = {}
	function service:JSONEncode(value)
		return serde.encode("json", value)
	end
	function service:JSONDecode(text)
		return serde.decode("json", text)
	end
	function service:UrlEncode(text)
		return (string.gsub(text, "[^%w%-_%.~]", function(char)
			return string.format("%%%02X", string.byte(char))
		end))
	end
	function service:RequestAsync(_options)
		error("HttpError: ConnectFail")
	end
	return service
end

local function newChangeHistory()
	local service = { recordings = {}, finished = {}, nextId = 0, busy = false }
	function service:TryBeginRecording(name, _displayName)
		if self.busy then
			return nil
		end
		self.nextId += 1
		local id = "recording" .. self.nextId
		self.recordings[id] = name
		self.busy = true
		return id
	end
	function service:FinishRecording(id, operation)
		table.insert(self.finished, { id = id, name = self.recordings[id], operation = operation })
		self.busy = false
	end
	return service
end

local function newSelection()
	local service = { current = {}, SelectionChanged = newSignal() }
	function service:Get()
		return table.clone(self.current)
	end
	function service:Set(list)
		self.current = table.clone(list)
		self.SelectionChanged:Fire()
	end
	return service
end

function Harness.newGame()
	local services = {
		HttpService = newHttpService(),
		ChangeHistoryService = newChangeHistory(),
		Selection = newSelection(),
		RunService = { Heartbeat = newSignal() },
		AnimationClipProvider = {
			RegisterAnimationClip = function(_self, _clip)
				return "rbxtemp://1"
			end,
		},
		Workspace = Instance.new("Workspace"),
	}
	local game = { HttpService = services.HttpService, Workspace = services.Workspace }
	function game:GetService(name)
		local service = services[name]
		if service == nil then
			error("fake game has no service " .. tostring(name))
		end
		return service
	end
	game.Selection = services.Selection
	return game, services
end

-- ---------------------------------------------------------------------------
-- Fusion 0.2 stand-in (state.lua only needs Value and Computed)
-- ---------------------------------------------------------------------------

local FUSION_SOURCE = [[
local Fusion = {}
function Fusion.Value(initial)
	local value = { _value = initial }
	function value:get() return self._value end
	function value:set(newValue) self._value = newValue end
	return value
end
function Fusion.Computed(fn)
	local computed = {}
	function computed:get() return fn() end
	return computed
end
function Fusion.Observer(_state)
	return { onChange = function() return function() end end }
end
return Fusion
]]

-- ---------------------------------------------------------------------------
-- Rojo-style module tree
-- ---------------------------------------------------------------------------

local function stripExtension(fileName)
	for _, extension in { ".server.lua", ".client.lua", ".lua", ".luau" } do
		if string.sub(fileName, -#extension) == extension then
			return string.sub(fileName, 1, -#extension - 1), extension
		end
	end
	return nil, nil
end

local nodeMeta = {}
nodeMeta.__index = function(node, key)
	local children = rawget(node, "_children")
	local child = children and children[key]
	if child then
		return child
	end
	if key == "FindFirstChild" then
		return function(self, name)
			return rawget(self, "_children")[name]
		end
	end
	if key == "WaitForChild" then
		return function(self, name)
			local found = rawget(self, "_children")[name]
			assert(found, "WaitForChild: no child " .. tostring(name))
			return found
		end
	end
	error(string.format("'%s' is not a valid member of '%s'", tostring(key), rawget(node, "Name")), 2)
end

local function newNode(name, parent, sourcePath, sourceText)
	local node = setmetatable({
		Name = name,
		Parent = parent,
		_children = {},
		_source = sourcePath,
		_sourceText = sourceText,
	}, nodeMeta)
	if parent then
		rawget(parent, "_children")[name] = node
	end
	return node
end

local function buildTree(directory, name, parent)
	local initPath = nil
	for _, candidate in { "init.lua", "init.luau" } do
		if fs.isFile(directory .. "/" .. candidate) then
			initPath = directory .. "/" .. candidate
		end
	end
	local node = newNode(name, parent, initPath, nil)
	for _, entry in fs.readDir(directory) do
		local path = directory .. "/" .. entry
		if fs.isDir(path) then
			buildTree(path, entry, node)
		else
			local moduleName, extension = stripExtension(entry)
			if moduleName and moduleName ~= "init" and extension ~= ".server.lua" and extension ~= ".client.lua" then
				newNode(moduleName, node, path, nil)
			end
		end
	end
	return node
end

function Harness.load(options)
	options = options or {}
	local game, services = Harness.newGame()
	local warnings = {}
	local cache = {}
	local root = buildTree(PLUGIN_ROOT, "BlenderAnimationsInternal", nil)
	local packages = rawget(root, "_children").Packages or newNode("Packages", root, nil, nil)
	newNode("Fusion", packages, nil, FUSION_SOURCE)

	local requireModule

	local function environmentFor(node)
		return {
			script = node,
			require = requireModule,
			game = game,
			workspace = services.Workspace,
			plugin = options.plugin,
			Instance = Instance,
			CFrame = roblox.CFrame,
			Vector3 = roblox.Vector3,
			Vector2 = roblox.Vector2,
			Color3 = roblox.Color3,
			UDim = roblox.UDim,
			UDim2 = roblox.UDim2,
			Enum = roblox.Enum,
			NumberRange = roblox.NumberRange,
			typeof = proxyTypeof,
			task = task,
			warn = function(...)
				local parts = {}
				for index = 1, select("#", ...) do
					parts[index] = tostring((select(index, ...)))
				end
				table.insert(warnings, table.concat(parts, " "))
			end,
			print = options.verbose and print or function() end,
			PluginManager = function()
				return { ExportSelection = function() end }
			end,
		}
	end

	requireModule = function(target)
		if type(target) ~= "table" then
			error("require expects a module node, got " .. typeof(target))
		end
		if cache[target] ~= nil then
			return cache[target]
		end
		local source = rawget(target, "_sourceText")
		local path = rawget(target, "_source")
		if source == nil then
			assert(path, "module '" .. tostring(rawget(target, "Name")) .. "' has no source")
			source = fs.readFile(path)
		end
		local chunk = luau.load(source, {
			debugName = path or rawget(target, "Name"),
			environment = environmentFor(target),
		})
		local result = chunk()
		cache[target] = result
		return result
	end

	return {
		root = root,
		require = function(dotted)
			local node = root
			for segment in string.gmatch(dotted, "[^%.]+") do
				node = node[segment]
			end
			return requireModule(node)
		end,
		game = game,
		services = services,
		warnings = warnings,
	}
end

-- ---------------------------------------------------------------------------
-- Tiny test framework
-- ---------------------------------------------------------------------------

local results = { passed = 0, failed = 0, failures = {} }
local currentSuite = ""

function Harness.describe(name, fn)
	local previous = currentSuite
	currentSuite = if previous == "" then name else previous .. " / " .. name
	fn()
	currentSuite = previous
end

function Harness.it(name, fn)
	local ok, err = pcall(fn)
	if ok then
		results.passed += 1
	else
		results.failed += 1
		table.insert(results.failures, currentSuite .. " / " .. name .. "\n    " .. tostring(err))
	end
end

local function fail(message, level)
	error(message, (level or 1) + 1)
end

function Harness.expectEqual(actual, expected, label)
	if actual ~= expected then
		fail(string.format("%s: expected %s, got %s", label or "value", tostring(expected), tostring(actual)), 2)
	end
end

function Harness.expectTrue(condition, label)
	if not condition then
		fail((label or "condition") .. " was false", 2)
	end
end

function Harness.expectContains(text, fragment, label)
	if type(text) ~= "string" or not string.find(text, fragment, 1, true) then
		fail(string.format("%s: expected %q to contain %q", label or "text", tostring(text), fragment), 2)
	end
end

function Harness.expectNear(actual, expected, tolerance, label)
	if math.abs(actual - expected) > (tolerance or 1e-5) then
		fail(string.format("%s: expected %s, got %s", label or "value", tostring(expected), tostring(actual)), 2)
	end
end

function Harness.results()
	return results
end

return Harness
