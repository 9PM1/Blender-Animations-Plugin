--[=[
	Runs the Studio plugin specs outside Studio.

	    lune run tests-lune/run

	Exits non-zero when anything fails.
]=]

local process = require("@lune/process")
local H = require("./harness")

local specs = {
	require("./specs/studio_rig.spec"),
	require("./specs/studio_sync.spec"),
	require("./specs/studio_services.spec"),
	require("./specs/studio_rig_changes.spec"),
}

for _, spec in specs do
	spec(H)
end

local results = H.results()
for _, failure in results.failures do
	print("FAIL " .. failure)
end
print(string.format("%d passed, %d failed", results.passed, results.failed))
process.exit(if results.failed > 0 then 1 else 0)
