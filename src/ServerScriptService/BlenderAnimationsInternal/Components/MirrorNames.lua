--!strict

--[=[
	Left/right name swapping for the mirror modifier.

	Only whole words are swapped: "LeftUpperArm", "left_leg", "Left Arm" and
	FACS controls such as "LeftEyeClosed" mirror; "Leftovers" or "Brightness"
	do not. The old substring replace swapped any "Left"/"Right" anywhere.
]=]

local MirrorNames = {}

local SWAPS = {
	{ "Left", "Right" },
	{ "Right", "Left" },
	{ "left", "right" },
	{ "right", "left" },
	{ "LEFT", "RIGHT" },
	{ "RIGHT", "LEFT" },
}

local function isUpper(char: string?): boolean
	return char ~= nil and char:match("%u") ~= nil
end

local function isLower(char: string?): boolean
	return char ~= nil and char:match("%l") ~= nil
end

local function isLetter(char: string?): boolean
	return char ~= nil and char:match("%a") ~= nil
end

local function isWordAt(name: string, first: number, word: string): boolean
	local before = if first > 1 then name:sub(first - 1, first - 1) else nil
	local after = name:sub(first + #word, first + #word)
	if after == "" then
		after = nil
	end
	local startsUpper = isUpper(word:sub(1, 1))
	local allUpper = word:upper() == word

	-- Start of a word: string start, a non-letter, or a CamelCase boundary.
	local startOk = before == nil
		or not isLetter(before)
		or (startsUpper and not allUpper and isLower(before))
	if not startOk then
		return false
	end
	-- End of a word: string end, a non-letter, or the next CamelCase word.
	if after == nil or not isLetter(after) then
		return true
	end
	if allUpper then
		return false
	end
	return isUpper(after)
end

function MirrorNames.mirror(name: string): string
	local pieces = {}
	local index = 1
	local length = #name
	while index <= length do
		local swapped = false
		for _, pair in ipairs(SWAPS) do
			local word, replacement = pair[1], pair[2]
			if name:sub(index, index + #word - 1) == word and isWordAt(name, index, word) then
				table.insert(pieces, replacement)
				index += #word
				swapped = true
				break
			end
		end
		if not swapped then
			table.insert(pieces, name:sub(index, index))
			index += 1
		end
	end
	return table.concat(pieces)
end

return MirrorNames
