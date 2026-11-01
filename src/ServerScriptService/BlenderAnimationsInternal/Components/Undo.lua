--!strict

--[=[
	Wrap changes the plugin makes to the place in one undo step.

	Setting a PrimaryPart, creating AnimSaves, Animators or synced bones used
	to bypass Studio's undo history entirely. If the work errors, the partial
	changes are rolled back instead of committed.
]=]

local Undo = {}

local function changeHistory(): any?
	local ok, service = pcall(function()
		return game:GetService("ChangeHistoryService")
	end)
	return if ok then service else nil
end

function Undo.record<T...>(name: string, fn: () -> T...): T...
	local service = changeHistory()
	local recording: string? = nil
	if service then
		local ok, identifier = pcall(function()
			return service:TryBeginRecording(name, name)
		end)
		-- nil while another recording is open: the outer one covers this work.
		recording = if ok then identifier else nil
	end

	local results = table.pack(pcall(fn))
	if service and recording then
		pcall(function()
			service:FinishRecording(
				recording,
				if results[1] then Enum.FinishRecordingOperation.Commit else Enum.FinishRecordingOperation.Cancel
			)
		end)
	end
	if not results[1] then
		error(results[2], 0)
	end
	return table.unpack(results, 2, results.n)
end

return Undo
