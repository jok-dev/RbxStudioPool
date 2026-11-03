-- selene: allow(undefined_variable)
local CONFIG = { { CONFIG } }
-- Run in the DESTINATION Studio via execute_luau (plugin permissions allow localhost HTTP) while lune/relay.luau
-- serves a merged change set (`--changeset <merged.json>`, from lune/merge.luau).
-- The caller substitutes CONFIG with a table literal: { relayUrl = "http://127.0.0.1:<port>", dryRun = boolean }.
-- Order: removals, adds, property edits. Each `changed` entry is drift-checked: its `from` must equal the current value,
-- otherwise it is reported STALE and left alone. With dryRun = true nothing is changed.
-- Returns the report as one string.
local ChangeHistoryService = game:GetService("ChangeHistoryService")
local HttpService = game:GetService("HttpService")
local SerializationService = game:GetService("SerializationService")

local PATH_SEPARATOR = "."
local ATTRIBUTES_PROPERTY = "Attributes"
local TAGS_PROPERTY = "Tags"
local RECORDING_NAME = "Apply change set"
local YIELD_EVERY = 20

local report = {}
local counts = { added = 0, set = 0, removed = 0, stale = 0, skipped = 0 }
local operationsDone = 0
local plannedAddPaths = {}
local plannedAddCounts = {}
local plannedFolders = {}
-- Dry run only: instances a real run would already have destroyed, treated as absent so later steps match the real run
local plannedRemovals: { [Instance]: boolean } = {}

local function yieldPeriodically()
	operationsDone += 1
	if operationsDone % YIELD_EVERY == 0 then
		task.wait()
	end
end

local function log(line: string)
	table.insert(report, line)
end

local function skip(line: string)
	counts.skipped += 1
	log(`SKIPPED {line}`)
end

-- The index-th child named `name` of class `className`, matching how the diff numbered duplicates
local function findChild(parent: Instance, name: string, className: string?, index: number?): Instance?
	local seen = 0
	for _, child in parent:GetChildren() do
		if not plannedRemovals[child] and child.Name == name and (not className or child.ClassName == className) then
			seen += 1
			if seen == (index or 1) then
				return child
			end
		end
	end
	return nil
end

-- Intermediate segments take the first match; only the final segment carries a class and index
local function findInstance(path: string, className: string, index: number): Instance?
	local segments = string.split(path, PATH_SEPARATOR)
	local node: Instance = game
	for position, segment in segments do
		local isLast = position == #segments
		local child = if isLast then findChild(node, segment, className, index) else findChild(node, segment)
		if not child then
			return nil
		end
		node = child
	end
	return node
end

-- Missing parents become Folders; nil when the service itself is missing.
-- On a dry run nothing is created: the second result is true when the parent would have to be created.
local function findOrCreateParent(segments: { string }): (Instance?, boolean)
	local node: Instance = game
	local walked = nil
	local virtual = false
	for _, segment in segments do
		walked = if walked then `{walked}{PATH_SEPARATOR}{segment}` else segment
		local child = if virtual then nil else findChild(node, segment)
		if not child then
			if node == game then
				return nil, false
			end
			if not CONFIG.dryRun then
				child = Instance.new("Folder")
				child.Name = segment
				child.Parent = node
				log(`Created folder {walked}`)
			else
				virtual = true
				if not plannedFolders[walked] then
					plannedFolders[walked] = true
					log(`Would create folder {walked}`)
				end
			end
		end
		node = child or node
	end
	return node, virtual
end

local function deserializeFirst(rbxmx: string): Instance
	local instances = SerializationService:DeserializeInstancesAsync(buffer.fromstring(rbxmx))
	for position = 2, #instances do
		instances[position]:Destroy()
	end
	return instances[1]
end

local function readWrapperValue(wrapper: Instance, property: string): any
	if property == ATTRIBUTES_PROPERTY then
		return wrapper:GetAttributes()
	elseif property == TAGS_PROPERTY then
		local tags = wrapper:GetTags()
		table.sort(tags)
		return tags
	end
	return (wrapper :: any)[property]
end

local function readCurrentValue(instance: Instance, property: string): any
	if property == ATTRIBUTES_PROPERTY then
		return instance:GetAttributes()
	elseif property == TAGS_PROPERTY then
		local tags = instance:GetTags()
		table.sort(tags)
		return tags
	end
	return (instance :: any)[property]
end

local function describe(value: any): string
	local valueType = typeof(value)
	if valueType == "ColorSequence" or valueType == "NumberSequence" then
		local parts = {}
		for _, keypoint in value.Keypoints do
			local keypointValue = if valueType == "ColorSequence" then tostring(keypoint.Value) else `{keypoint.Value}~{keypoint.Envelope}`
			table.insert(parts, `{keypoint.Time}:{keypointValue}`)
		end
		return `{valueType}[{table.concat(parts, "; ")}]`
	end
	if type(value) ~= "table" then
		return tostring(value)
	end
	local parts = {}
	for key, entry in value do
		table.insert(parts, `{key}={describe(entry)}`)
	end
	table.sort(parts)
	return `\{{table.concat(parts, ", ")}\}`
end

-- `==` compares sequences (and any other reference-wrapped type) by identity, so anything it rejects is compared by
-- canonical description instead; that also covers Attributes and Tags tables
local function valuesEqual(left: any, right: any): boolean
	if left == right then
		return true
	end
	if typeof(left) ~= typeof(right) then
		return false
	end
	return describe(left) == describe(right)
end

local function writeValue(instance: Instance, property: string, value: any)
	if property == ATTRIBUTES_PROPERTY then
		for name in instance:GetAttributes() do
			instance:SetAttribute(name, nil)
		end
		for name, attribute in value do
			instance:SetAttribute(name, attribute)
		end
	elseif property == TAGS_PROPERTY then
		for _, tag in instance:GetTags() do
			instance:RemoveTag(tag)
		end
		for _, tag in value do
			instance:AddTag(tag)
		end
	else
		(instance :: any)[property] = value
	end
end

local function isUnderPlannedAdd(path: string): boolean
	for added in plannedAddPaths do
		if path == added or string.sub(path, 1, #added + 1) == added .. PATH_SEPARATOR then
			return true
		end
	end
	return false
end

local function applyRemoval(entry)
	local instance = findInstance(entry.path, entry.className, entry.index)
	if not instance then
		skip(`{entry.path}: instance to remove not found`)
		return
	end

	local descendantCount = #instance:GetDescendants()
	if CONFIG.dryRun then
		plannedRemovals[instance] = true
		log(`Would REMOVE {entry.path} ({descendantCount} descendants)`)
	else
		instance:Destroy()
		log(`REMOVE {entry.path} ({descendantCount} descendants)`)
	end
	counts.removed += 1
end

local function applyAddition(entry)
	local segments = string.split(entry.path, PATH_SEPARATOR)
	local name = table.remove(segments) :: string
	local parent, virtual = findOrCreateParent(segments)
	if not parent then
		skip(`{entry.path}: service {segments[1]} not found`)
		return
	end

	local plannedKey = `{entry.path}|{entry.className}`
	local alreadyThere = 0
	if not virtual then
		for _, child in parent:GetChildren() do
			if not plannedRemovals[child] and child.Name == name and child.ClassName == entry.className then
				alreadyThere += 1
			end
		end
	end
	if alreadyThere + (plannedAddCounts[plannedKey] or 0) >= entry.index then
		skip(`{entry.path}: an instance already exists there`)
		return
	end

	if CONFIG.dryRun then
		plannedAddPaths[entry.path] = true
		plannedAddCounts[plannedKey] = (plannedAddCounts[plannedKey] or 0) + 1
		log(`Would add {entry.path}`)
	else
		local instance = deserializeFirst(entry.rbxmx)
		instance.Parent = parent
		log(`Add {entry.path}`)
	end
	counts.added += 1
end

local function applyChange(entry)
	local label = `{entry.path}{PATH_SEPARATOR}{entry.property}`
	local instance = findInstance(entry.path, entry.className, entry.index)
	local newWrapper = deserializeFirst(entry.to)
	local newValue = readWrapperValue(newWrapper, entry.property)
	newWrapper:Destroy()

	if not instance then
		if CONFIG.dryRun and isUnderPlannedAdd(entry.path) then
			log(`Would set {label}: (added by this change set) -> {describe(newValue)}`)
			counts.set += 1
			return
		end
		skip(`{label}: instance not found`)
		return
	end

	local fromWrapper = deserializeFirst(entry.from)
	local fromValue = readWrapperValue(fromWrapper, entry.property)
	fromWrapper:Destroy()

	local currentValue = readCurrentValue(instance, entry.property)
	if not valuesEqual(currentValue, fromValue) then
		counts.stale += 1
		log(`STALE {label}: expected {describe(fromValue)}, found {describe(currentValue)}`)
		return
	end

	if CONFIG.dryRun then
		log(`Would set {label}: {describe(currentValue)} -> {describe(newValue)}`)
	else
		writeValue(instance, entry.property, newValue)
		log(`Set {label}: {describe(currentValue)} -> {describe(newValue)}`)
	end
	counts.set += 1
end

local function applyAll(changeSet)
	for _, entry in changeSet.removed do
		applyRemoval(entry)
		yieldPeriodically()
	end
	for _, entry in changeSet.added do
		applyAddition(entry)
		yieldPeriodically()
	end
	for _, entry in changeSet.changed do
		local ok, failure = pcall(applyChange, entry)
		if not ok then
			skip(`{entry.path}{PATH_SEPARATOR}{entry.property}: {failure}`)
		end
		yieldPeriodically()
	end
end

local changeSet = HttpService:JSONDecode(HttpService:GetAsync(`{CONFIG.relayUrl}/changeset`))

if #(changeSet.conflicts or {}) > 0 then
	log(`WARNING: the change set lists {#changeSet.conflicts} unresolved conflicts; the conflicting entries were withheld by the merge`)
end

local recording = if CONFIG.dryRun then nil else ChangeHistoryService:TryBeginRecording(RECORDING_NAME)
if not CONFIG.dryRun and not recording then
	error("ChangeHistoryService:TryBeginRecording returned nil; aborting so nothing is applied without an undo point")
end
local ok, failure = pcall(applyAll, changeSet)
if recording then
	ChangeHistoryService:FinishRecording(recording, if ok then Enum.FinishRecordingOperation.Commit else Enum.FinishRecordingOperation.Cancel)
end
if not ok then
	error(`Apply failed, recording cancelled: {failure}\n{table.concat(report, "\n")}`)
end

local summary = if CONFIG.dryRun then "Dry run" else "Applied"
log(`{summary}: {counts.added} added, {counts.set} property edits, {counts.removed} removed, {counts.stale} STALE, {counts.skipped} skipped; {#changeSet.unsupported} unsupported entries were not applied`)
return table.concat(report, "\n")
