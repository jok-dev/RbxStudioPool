-- Request handling for the snapshot relay, shared by lune/relay.luau and the relay test.
local fs = require("@lune/fs")
local net = require("@lune/net")
local serde = require("@lune/serde")

local Base64 = require("./Base64")

local CHUNK_EXTENSION = ".rbxm"
local TEMP_EXTENSION = ".tmp"
local MANIFEST_FILE = "manifest.json"
local FORBIDDEN_PATH_PATTERNS = { "%.%.", "/", "\\", "^%.", "%.$", "%c", '[:*?"<>|]' }

export type RelayConfig = {
	outDir: string,
	changesetPath: string?,
}

local Relay = {}

function Relay.isSafePath(path: string?): boolean
	if not path or path == "" then
		return false
	end
	for _, pattern in FORBIDDEN_PATH_PATTERNS do
		if string.find(path, pattern) then
			return false
		end
	end
	return true
end

local function reply(status: number, body: string): net.ServeResponse
	return { status = status, body = body }
end

local function listDonePaths(config: RelayConfig): { string }
	local paths = {}
	for _, name in fs.readDir(config.outDir) do
		if string.sub(name, -#CHUNK_EXTENSION) == CHUNK_EXTENSION then
			table.insert(paths, string.sub(name, 1, -#CHUNK_EXTENSION - 1))
		end
	end
	table.sort(paths)
	return paths
end

local function getChunkFile(config: RelayConfig, path: string): string
	return `{config.outDir}/{path}{CHUNK_EXTENSION}`
end

function Relay.createHandler(config: RelayConfig): (net.ServeRequest) -> net.ServeResponse
	fs.writeDir(config.outDir)

	return function(request)
		if request.path == "/manifest" and request.method == "POST" then
			fs.writeFile(`{config.outDir}/{MANIFEST_FILE}`, request.body or "")
			print(`Wrote {MANIFEST_FILE}`)
			return reply(200, "ok")
		end

		if request.path == "/changeset" and request.method == "GET" then
			if not config.changesetPath then
				return reply(404, "No changeset given")
			end
			return reply(200, fs.readFile(config.changesetPath))
		end

		if request.path == "/done-list" and request.method == "GET" then
			return reply(200, serde.encode("json", listDonePaths(config)))
		end

		if request.path == "/chunk" and request.method == "POST" then
			local path = request.query.path
			if not Relay.isSafePath(path) then
				warn(`Rejected unsafe chunk path: {tostring(path)}`)
				return reply(400, "Invalid path")
			end

			local bytes = Base64.decode(request.body or "")
			if not bytes then
				warn(`Rejected chunk {path}: body is not base64`)
				return reply(400, "Body is not base64")
			end

			-- Write beside then rename, so a kill mid-write never leaves a truncated chunk that counts as done
			local file = getChunkFile(config, path)
			local tempFile = `{file}{TEMP_EXTENSION}`
			fs.writeFile(tempFile, bytes)
			fs.move(tempFile, file, true)
			print(`Wrote {path}{CHUNK_EXTENSION} ({#bytes} bytes)`)
			return reply(200, "ok")
		end

		return reply(404, "Not found")
	end
end

return Relay
