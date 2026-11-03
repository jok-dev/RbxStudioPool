-- Rebuilds instance trees from a snapshot directory (Dump.lua + relay layout) and diffs two of them.
local fs = require("@lune/fs")
local roblox = require("@lune/roblox")
local serde = require("@lune/serde")

local Diff = require("./Diff")

local MANIFEST_FILE = "manifest.json"
local CHUNK_EXTENSION = ".rbxm"
local PATH_SEPARATOR = "."
local PLACEHOLDER_CLASS = "Folder"
local GRANULARITY_REASON = "snapshot granularity differs (container in one snapshot, chunk in the other)"
local MISSING_ROOT_REASON = "root missing from one snapshot"
local ROOT_PROPERTIES_REASON = "root instance properties are not captured by the snapshot (e.g. Lighting.ClockTime)"

export type Manifest = {
	snapshotId: string?,
	roots: { string },
	chunks: { string },
	containers: { string },
	skipped: { { path: string, reason: string } },
}

export type Loaded = {
	manifest: Manifest,
	roots: { [string]: roblox.Instance },
	-- Paths of instances the snapshot did not capture: roots and containers
	structural: { [string]: boolean },
	chunkPaths: { [string]: boolean },
	containerPaths: { [string]: boolean },
	problems: { Diff.UnsupportedEntry },
}

local Snapshot = {}

local function splitPath(path: string): { string }
	return string.split(path, PATH_SEPARATOR)
end

local function getDepth(path: string): number
	return #splitPath(path)
end

local function createPlaceholder(name: string): roblox.Instance
	local placeholder = roblox.Instance.new(PLACEHOLDER_CLASS)
	placeholder.Name = name
	return placeholder
end

local function createRoot(name: string): roblox.Instance
	local ok, root = pcall(roblox.Instance.new, name)
	if ok then
		return root
	end
	warn(`Snapshot: cannot create root class {name}, using a {PLACEHOLDER_CLASS}`)
	return createPlaceholder(name)
end

function Snapshot.load(directory: string): Loaded
	local manifestPath = `{directory}/{MANIFEST_FILE}`
	if not fs.isFile(manifestPath) then
		error(`Snapshot {directory} has no {MANIFEST_FILE}`)
	end
	local manifest: Manifest = serde.decode("json", fs.readFile(manifestPath))
	manifest.containers = manifest.containers or {}
	manifest.skipped = manifest.skipped or {}

	local loaded: Loaded = {
		manifest = manifest,
		roots = {},
		structural = {},
		chunkPaths = {},
		containerPaths = {},
		problems = {},
	}
	for _, rootName in manifest.roots do
		loaded.roots[rootName] = createRoot(rootName)
		loaded.structural[rootName] = true
	end

	-- Walks (creating placeholders as needed) down to the parent of the last segment of path
	local function ensureParent(path: string): roblox.Instance?
		local segments = splitPath(path)
		local current = loaded.roots[segments[1]]
		if not current then
			return nil
		end
		local walkedPath = segments[1]
		for position = 2, #segments - 1 do
			walkedPath = `{walkedPath}{PATH_SEPARATOR}{segments[position]}`
			local child = current:FindFirstChild(segments[position])
			if not child then
				child = createPlaceholder(segments[position])
				child.Parent = current
				loaded.structural[walkedPath] = true
			end
			current = child
		end
		return current
	end

	local function markAncestors(path: string)
		local segments = splitPath(path)
		for length = 2, #segments - 1 do
			loaded.containerPaths[table.concat(segments, PATH_SEPARATOR, 1, length)] = true
		end
	end

	local orderedContainers = table.clone(manifest.containers)
	table.sort(orderedContainers, function(left, right)
		return getDepth(left) < getDepth(right)
	end)
	for _, containerPath in orderedContainers do
		loaded.containerPaths[containerPath] = true
		markAncestors(containerPath)
		local parent = ensureParent(containerPath)
		local name = splitPath(containerPath)[getDepth(containerPath)]
		if parent and not parent:FindFirstChild(name) then
			local placeholder = createPlaceholder(name)
			placeholder.Parent = parent
			loaded.structural[containerPath] = true
		end
	end

	for _, chunkPath in manifest.chunks do
		loaded.chunkPaths[chunkPath] = true
		markAncestors(chunkPath)
		local chunkFile = `{directory}/{chunkPath}{CHUNK_EXTENSION}`
		local parent = ensureParent(chunkPath)
		if not parent or not fs.isFile(chunkFile) then
			warn(`Snapshot: chunk {chunkPath} is listed but has no file or root`)
			table.insert(loaded.problems, { path = chunkPath, reason = "chunk listed in manifest but missing on disk" })
			continue
		end
		local instances = roblox.deserializeModel(fs.readFile(chunkFile))
		if #instances ~= 1 then
			warn(`Snapshot: chunk {chunkPath} holds {#instances} top-level instances, expected 1`)
			table.insert(loaded.problems, { path = chunkPath, reason = `chunk holds {#instances} top-level instances, expected 1` })
			continue
		end
		instances[1].Parent = parent
	end

	for path in loaded.containerPaths do
		loaded.structural[path] = true
	end
	return loaded
end

local function addUnsupportedOnce(changeSet: Diff.ChangeSet, seen: { [string]: boolean }, entry: Diff.UnsupportedEntry)
	local key = `{entry.path or ""}|{entry.property or ""}|{entry.reason}`
	if seen[key] then
		return
	end
	seen[key] = true
	table.insert(changeSet.unsupported, entry)
end

function Snapshot.diff(baseDirectory: string, endDirectory: string, ignore: Diff.Ignore): Diff.ChangeSet
	local base = Snapshot.load(baseDirectory)
	local final = Snapshot.load(endDirectory)
	local changeSet = Diff.newChangeSet()
	local seen: { [string]: boolean } = {}

	for _, loaded in { base, final } do
		for _, problem in loaded.problems do
			addUnsupportedOnce(changeSet, seen, problem)
		end
		for _, skipped in loaded.manifest.skipped do
			addUnsupportedOnce(changeSet, seen, { path = skipped.path, reason = skipped.reason })
		end
	end

	local structureOnly = {}
	for path in base.structural do
		structureOnly[path] = true
	end
	for path in final.structural do
		structureOnly[path] = true
	end

	-- Skipped paths were never captured on one side, so comparing the other side would report phantom adds or removes
	local paths = table.clone(ignore.paths)
	for _, loaded in { base, final } do
		for _, skipped in loaded.manifest.skipped do
			table.insert(paths, { path = skipped.path, reason = skipped.reason })
		end
	end
	for path in base.chunkPaths do
		if final.containerPaths[path] then
			table.insert(paths, { path = path, reason = GRANULARITY_REASON })
			addUnsupportedOnce(changeSet, seen, { path = path, reason = GRANULARITY_REASON })
		end
	end
	for path in base.containerPaths do
		if final.chunkPaths[path] then
			table.insert(paths, { path = path, reason = GRANULARITY_REASON })
			addUnsupportedOnce(changeSet, seen, { path = path, reason = GRANULARITY_REASON })
		end
	end

	local combinedIgnore: Diff.Ignore = { paths = paths, properties = ignore.properties, structureOnly = structureOnly }
	local rootNames = {}
	for rootName in base.roots do
		rootNames[rootName] = true
	end
	for rootName in final.roots do
		rootNames[rootName] = true
	end
	local orderedRootNames = {}
	for rootName in rootNames do
		table.insert(orderedRootNames, rootName)
	end
	table.sort(orderedRootNames)

	for _, rootName in orderedRootNames do
		addUnsupportedOnce(changeSet, seen, { path = rootName, reason = ROOT_PROPERTIES_REASON })
		local baseRoot = base.roots[rootName]
		local finalRoot = final.roots[rootName]
		if baseRoot and finalRoot then
			Diff.append(changeSet, Diff.trees(baseRoot, finalRoot, rootName, combinedIgnore))
		else
			warn(`Snapshot: root {rootName} is missing from one snapshot`)
			addUnsupportedOnce(changeSet, seen, { path = rootName, reason = MISSING_ROOT_REASON })
		end
	end
	return changeSet
end

return Snapshot
