--[=[
	Compiles every plugin source file, so syntax errors in modules the specs
	don't load (UI, the main script) still fail the run.

	    lune run tests-lune/compile_all
]=]

local fs = require("@lune/fs")
local luau = require("@lune/luau")
local process = require("@lune/process")

local failures = 0
local count = 0

local function visit(directory)
	for _, entry in fs.readDir(directory) do
		local path = directory .. "/" .. entry
		if fs.isDir(path) then
			if not string.find(entry, "testez", 1, true) then
				visit(path)
			end
		elseif string.match(entry, "%.luau?$") then
			count += 1
			local ok, err = pcall(luau.compile, fs.readFile(path))
			if not ok then
				failures += 1
				print("SYNTAX " .. path .. "\n    " .. tostring(err))
			end
		end
	end
end

visit("src")
print(string.format("compiled %d files, %d failed", count, failures))
process.exit(if failures > 0 then 1 else 0)
