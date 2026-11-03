-- Diffs two snapshot directories (Dump.lua + relay layout) into a change set.
local fs = require("@lune/fs")
local process = require("@lune/process")
local serde = require("@lune/serde")

local Diff = require("./lib/Diff")
local Snapshot = require("./lib/Snapshot")

local USAGE = [[
Usage: lune run spikes/place/lune/diff.luau <baseDir> <endDir> <outDir> [--ignore <file>]

Writes <outDir>/changeset.json and <outDir>/added/<n>.rbxmx.

ignore.json:
  {
    "paths":      [{ "path": "Workspace.Noise", "reason": "..." }],
    "properties": [{ "class": "BasePart", "property": "Position", "reason": "..." }]
  }
  paths: a dot path; that instance and its whole subtree are skipped.
  properties: skipped on every instance (class is optional; when given it matches by IsA).

changeset.json arrays: added, changed, removed, unsupported, ignored.
  unsupported entries are either instance-scoped { path, property?, reason } or class-scoped
    { class, property, instanceCount, reason }: a property @lune/roblox cannot read, unchecked on all
    <instanceCount> instances of that class across every root. Nothing is dropped silently.
  ignored entries { path?, class?, property?, reason, count } list every ignore rule that actually hid a difference.]]
local CHANGESET_FILE = "changeset.json"
local ADDED_DIR = "added"
local ADDED_EXTENSION = ".rbxmx"

local function readFlag(name: string): string?
	for index, argument in process.args do
		if argument == `--{name}` then
			return process.args[index + 1]
		end
	end
	return nil
end

local function readPositionals(): { string }
	local positionals = {}
	local skipNext = false
	for _, argument in process.args do
		if skipNext then
			skipNext = false
		elseif string.sub(argument, 1, 2) == "--" then
			skipNext = true
		else
			table.insert(positionals, argument)
		end
	end
	return positionals
end

local function loadIgnore(path: string?): Diff.Ignore
	if not path then
		return { paths = {}, properties = {} }
	end
	if not fs.isFile(path) then
		warn(`Ignore file {path} not found`)
		process.exit(1)
	end
	local decoded = serde.decode("json", fs.readFile(path))
	return { paths = decoded.paths or {}, properties = decoded.properties or {} }
end

local positionals = readPositionals()
local baseDir, endDir, outDir = positionals[1], positionals[2], positionals[3]
if not baseDir or not endDir or not outDir then
	warn(USAGE)
	process.exit(1)
end

local changeSet = Snapshot.diff(baseDir, endDir, loadIgnore(readFlag("ignore")))

if fs.isDir(outDir) then
	fs.removeDir(outDir)
end
fs.writeDir(`{outDir}/{ADDED_DIR}`)

local added = {}
for position, entry in changeSet.added do
	local fileName = `{position}{ADDED_EXTENSION}`
	fs.writeFile(`{outDir}/{ADDED_DIR}/{fileName}`, entry.rbxmx)
	table.insert(added, { path = entry.path, rbxmx = fileName, className = entry.className, index = entry.index })
end

local envelope = {
	added = added,
	changed = changeSet.changed,
	removed = changeSet.removed,
	unsupported = changeSet.unsupported,
	ignored = changeSet.ignored,
}
fs.writeFile(`{outDir}/{CHANGESET_FILE}`, serde.encode("json", envelope, true))
print(`{#added} added, {#changeSet.changed} changed, {#changeSet.removed} removed, {#changeSet.unsupported} unsupported, {#changeSet.ignored} ignore rules applied -> {outDir}/{CHANGESET_FILE}`)
