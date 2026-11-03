-- selene: allow(undefined_variable)
local CONFIG = { { CONFIG } }
-- Run in Studio via execute_luau (plugin permissions allow localhost HTTP) while lune/relay.luau is running.
-- The caller substitutes CONFIG with a table literal: { roots = {"Workspace", ...}, chunkBytes = number,
-- relayUrl = "http://localhost:<port>", snapshotId = string }.
-- Serializes each child of each root whole when its estimated size fits in chunkBytes, otherwise recurses.
-- Chunks the relay already has are skipped, so re-running after a dropped connection resumes the dump.
local EncodingService = game:GetService("EncodingService")
local HttpService = game:GetService("HttpService")
local SerializationService = game:GetService("SerializationService")

-- Rough .rbxm size per descendant; the real size is only known after serializing
local ESTIMATED_BYTES_PER_DESCENDANT = 400
-- Encoded body limit; the real HttpService request limit must still be confirmed live
local MAX_BODY_BYTES = 900_000
local REQUEST_ATTEMPTS = 4
local RETRY_BASE_DELAY_SECONDS = 0.5
local RETRY_DELAY_MULTIPLIER = 2
local UNSAFE_NAME_PATTERNS = {
	{ pattern = "%.", reason = "name contains '.'" },
	{ pattern = "[/]", reason = "name contains a path separator" },
	{ pattern = '[:*?"<>|]', reason = "name contains a character invalid in Windows filenames" },
	{ pattern = "%c", reason = "name contains a control character" },
}
local ALWAYS_SKIPPED = {
	Workspace = {
		Terrain = "terrain is not serializable as an instance",
		Camera = "camera is runtime state",
	},
}

local manifest = {
	snapshotId = CONFIG.snapshotId,
	roots = CONFIG.roots,
	chunks = {},
	containers = {},
	skipped = {},
}
local donePaths = {}
local resumedChunks = 0
local biggestChunk = { path = "", bytes = 0 }

local function skip(path: string, reason: string)
	warn(`Skipping {path}: {reason}`)
	table.insert(manifest.skipped, { path = path, reason = reason })
end

local function requestWithRetry(callback: () -> any): any
	local delaySeconds = RETRY_BASE_DELAY_SECONDS
	for attempt = 1, REQUEST_ATTEMPTS do
		local ok, result = pcall(callback)
		if ok then
			return result
		end
		if attempt == REQUEST_ATTEMPTS then
			error(`Relay request failed after {REQUEST_ATTEMPTS} attempts: {result}`)
		end
		warn(`Relay request failed (attempt {attempt}), retrying: {result}`)
		task.wait(delaySeconds)
		delaySeconds *= RETRY_DELAY_MULTIPLIER
	end
	return nil
end

local function loadDonePaths()
	local response = requestWithRetry(function()
		return HttpService:GetAsync(`{CONFIG.relayUrl}/done-list`)
	end)
	for _, path in HttpService:JSONDecode(response) do
		donePaths[path] = true
	end
end

-- Returns "uploaded", "tooLarge" or "failed"
local function uploadChunk(path: string, instance: Instance): string
	local serializeOk, data = pcall(SerializationService.SerializeInstancesAsync, SerializationService, { instance })
	if not serializeOk then
		skip(path, `serialize failed: {data}`)
		return "failed"
	end

	local body = buffer.tostring(EncodingService:Base64Encode(data))
	if #body > MAX_BODY_BYTES then
		return "tooLarge"
	end

	requestWithRetry(function()
		return HttpService:PostAsync(`{CONFIG.relayUrl}/chunk?path={HttpService:UrlEncode(path)}`, body, Enum.HttpContentType.TextPlain)
	end)

	if #body > biggestChunk.bytes then
		biggestChunk = { path = path, bytes = #body }
	end
	return "uploaded"
end

local function getSkipReason(parentPath: string, child: Instance, takenNames: { [string]: boolean }): string?
	local alwaysSkippedReason = ALWAYS_SKIPPED[parentPath] and ALWAYS_SKIPPED[parentPath][child.Name]
	if alwaysSkippedReason and child.ClassName == child.Name then
		return alwaysSkippedReason
	end
	for _, unsafe in UNSAFE_NAME_PATTERNS do
		if string.find(child.Name, unsafe.pattern) then
			return unsafe.reason
		end
	end
	if takenNames[string.lower(child.Name)] then
		return "duplicate sibling name (case-insensitive, filenames would collide)"
	end
	return nil
end

local dumpChildren

local function dumpChild(child: Instance, path: string)
	local estimatedBytes = (#child:GetDescendants() + 1) * ESTIMATED_BYTES_PER_DESCENDANT
	local hasChildren = #child:GetChildren() > 0
	if estimatedBytes > CONFIG.chunkBytes and hasChildren then
		table.insert(manifest.containers, path)
		dumpChildren(child, path)
		return
	end

	if donePaths[path] then
		resumedChunks += 1
		table.insert(manifest.chunks, path)
		return
	end

	local result = uploadChunk(path, child)
	if result == "uploaded" then
		table.insert(manifest.chunks, path)
		task.wait()
	elseif result == "tooLarge" and hasChildren then
		table.insert(manifest.containers, path)
		dumpChildren(child, path)
	elseif result == "tooLarge" then
		skip(path, "too large")
	end
end

function dumpChildren(parent: Instance, parentPath: string)
	local takenNames = {}
	for _, child in parent:GetChildren() do
		local path = `{parentPath}.{child.Name}`
		local skipReason = getSkipReason(parentPath, child, takenNames)
		if skipReason then
			skip(path, skipReason)
			continue
		end
		takenNames[string.lower(child.Name)] = true
		dumpChild(child, path)
	end
end

local startTime = os.clock()
loadDonePaths()
for _, rootName in CONFIG.roots do
	local rootOk, root = pcall(game.GetService, game, rootName)
	if not rootOk then
		skip(rootName, "root is not a service")
		continue
	end
	dumpChildren(root, rootName)
end

requestWithRetry(function()
	return HttpService:PostAsync(`{CONFIG.relayUrl}/manifest`, HttpService:JSONEncode(manifest), Enum.HttpContentType.ApplicationJson)
end)

return `Snapshot {CONFIG.snapshotId}: {#manifest.chunks} chunks ({resumedChunks} already on relay), {#manifest.skipped} skipped, biggest {biggestChunk.path} ({biggestChunk.bytes} bytes), {string.format("%.1f", os.clock() - startTime)}s`
