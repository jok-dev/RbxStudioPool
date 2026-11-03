-- Relay between Studio (Dump.lua, via execute_luau) and the snapshot directory on disk.
-- lune run spikes/place/lune/relay.luau --port <n> --out <dir> [--changeset <file>]
local net = require("@lune/net")
local process = require("@lune/process")

local Relay = require("./lib/Relay")

local function readFlag(name: string): string?
	for index, argument in process.args do
		if argument == `--{name}` then
			return process.args[index + 1]
		end
	end
	return nil
end

local port = tonumber(readFlag("port"))
local outDir = readFlag("out")
if not port or not outDir then
	warn("Usage: lune run spikes/place/lune/relay.luau --port <n> --out <dir> [--changeset <file>]")
	process.exit(1)
end

net.serve(port, Relay.createHandler({ outDir = outDir, changesetPath = readFlag("changeset") }))
print(`Snapshot relay on http://localhost:{port}, writing to {outDir}`)
