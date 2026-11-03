-- Merges change sets (diff.luau output) in acceptance order into one change set for Apply.lua.
local fs = require("@lune/fs")
local process = require("@lune/process")
local serde = require("@lune/serde")

local Merge = require("./lib/Merge")

local USAGE = [[
Usage: lune run spikes/place/lune/merge.luau <out.json> <changeset1.json> <changeset2.json> ...

Inputs are changeset.json files (diff.luau output) in acceptance order. Each may carry
  "id": string   (defaults to set<position>)
  "base": [ids]  (the sets already applied when its slot started)
Added subtrees are read from the input's added/<n>.rbxmx sidecars and inlined into <out.json>
(added[].rbxmx holds the XML itself), so the relay can serve the file whole.
<out.json> also lists "merged" (set ids in order) and "conflicts" { kind, paths, sets }.
Conflicting entries are withheld from the merged output. Exit code 1 on usage errors, 2 on conflicts.]]
local CONFLICT_EXIT_CODE = 2
local ADDED_DIR = "added"

local function getDirectory(path: string): string
	return string.match(path, "^(.*)[/][^/]*$") or "."
end

local outPath = process.args[1]
if not outPath or #process.args < 2 then
	warn(USAGE)
	process.exit(1)
end

local sets = {}
local ids = {}
for position = 2, #process.args do
	local path = process.args[position]
	if not fs.isFile(path) then
		warn(`Change set {path} not found`)
		process.exit(1)
	end

	local set = serde.decode("json", fs.readFile(path))
	local directory = getDirectory(path)
	for _, entry in set.added do
		local sidecar = `{directory}/{ADDED_DIR}/{entry.rbxmx}`
		if not fs.isFile(sidecar) then
			warn(`Added subtree file {sidecar} (for {entry.path}) not found`)
			process.exit(1)
		end
		entry.rbxmx = fs.readFile(sidecar)
	end
	set.id = set.id or `set{position - 1}`
	table.insert(ids, set.id)
	table.insert(sets, set)
end

local merged, conflicts = Merge.changeSets(sets)
local envelope = {
	merged = ids,
	added = merged.added,
	changed = merged.changed,
	removed = merged.removed,
	unsupported = merged.unsupported,
	ignored = merged.ignored,
	conflicts = conflicts,
}
fs.writeFile(outPath, serde.encode("json", envelope, true))

for _, conflict in conflicts do
	warn(`CONFLICT {conflict.kind}: {table.concat(conflict.paths, ", ")} (sets {table.concat(conflict.sets, " vs ")})`)
end
print(`{#merged.added} added, {#merged.changed} changed, {#merged.removed} removed, {#merged.unsupported} unsupported, {#merged.ignored} ignore rules applied, {#conflicts} conflicts -> {outPath}`)
if #conflicts > 0 then
	process.exit(CONFLICT_EXIT_CODE)
end
