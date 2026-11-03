-- Pure diff of two instance trees into a change set. No file or network access.
local roblox = require("@lune/roblox")

local ATTRIBUTES_PROPERTY = "Attributes"
local TAGS_PROPERTY = "Tags"
local PATH_SEPARATOR = "."
local KEY_SEPARATOR = "\0"
local READABLE_SCRIPTABILITY = "ReadWrite"
local SKIPPED_TAGS = { "Deprecated", "ReadOnly", "WriteOnly" }
local SKIPPED_PROPERTIES = { Name = true, Parent = true, ClassName = true }
local DOT_REASON = "name contains '.'"
local MISSING_DEFAULT_MARKER = "missing default value"

export type IgnorePath = { path: string, reason: string }
export type IgnoreProperty = { class: string?, property: string, reason: string }
export type Ignore = {
	paths: { IgnorePath },
	properties: { IgnoreProperty },
	-- Paths whose own properties are never compared (snapshot roots and containers, which are structure only)
	structureOnly: { [string]: boolean }?,
}
export type AddedEntry = { path: string, rbxmx: string, className: string, index: number }
export type ChangedEntry = { path: string, property: string, from: string, to: string, className: string, index: number }
export type RemovedEntry = { path: string, className: string, index: number }
-- Instance-scoped entries carry `path`. Class-scoped entries (a property @lune/roblox cannot read) carry `class`,
-- `property` and `instanceCount` instead of a path, because the property is unchecked on every instance of the class.
export type UnsupportedEntry = {
	path: string?,
	class: string?,
	property: string?,
	instanceCount: number?,
	reason: string,
}
-- An ignore rule that actually hid a difference: `count` is the number of hidden differences (or instances for a path rule)
export type IgnoredEntry = { path: string?, class: string?, property: string?, reason: string, count: number }
export type ChangeSet = {
	added: { AddedEntry },
	changed: { ChangedEntry },
	removed: { RemovedEntry },
	unsupported: { UnsupportedEntry },
	ignored: { IgnoredEntry },
}

type PropertyInfo = { name: string, datatype: string }

local Diff = {}

local database = roblox.getReflectionDatabase()
local propertyCache: { [string]: { PropertyInfo } } = {}

function Diff.newChangeSet(): ChangeSet
	return { added = {}, changed = {}, removed = {}, unsupported = {}, ignored = {} }
end

local function getUncheckedReason(instanceCount: number): string
	return `cannot read with @lune/roblox; unchecked on all {instanceCount} instances`
end

local function getIgnoredKey(entry: IgnoredEntry): string
	return `{entry.path or ""}|{entry.class or ""}|{entry.property or ""}`
end

function Diff.append(target: ChangeSet, source: ChangeSet)
	for _, entry in source.added do
		table.insert(target.added, entry)
	end
	for _, entry in source.changed do
		table.insert(target.changed, entry)
	end
	for _, entry in source.removed do
		table.insert(target.removed, entry)
	end
	for _, entry in source.unsupported do
		local existing
		if entry.class then
			for _, candidate in target.unsupported do
				if not candidate.path and candidate.class == entry.class and candidate.property == entry.property then
					existing = candidate
				end
			end
		end
		if existing and existing.instanceCount and entry.instanceCount then
			existing.instanceCount += entry.instanceCount
			existing.reason = getUncheckedReason(existing.instanceCount)
		else
			table.insert(target.unsupported, table.clone(entry))
		end
	end
	for _, entry in source.ignored do
		local existing
		for _, candidate in target.ignored do
			if getIgnoredKey(candidate) == getIgnoredKey(entry) then
				existing = candidate
			end
		end
		if existing then
			existing.count += entry.count
		else
			table.insert(target.ignored, table.clone(entry))
		end
	end
end

function Diff.isEmpty(changeSet: ChangeSet): boolean
	return #changeSet.added == 0 and #changeSet.changed == 0 and #changeSet.removed == 0 and #changeSet.unsupported == 0
end

local function hasAnyTag(tags: { string }, wanted: { string }): boolean
	for _, tag in tags do
		if table.find(wanted, tag) then
			return true
		end
	end
	return false
end

local function getProperties(className: string): { PropertyInfo }
	local cached = propertyCache[className]
	if cached then
		return cached
	end

	local properties = {}
	local seen = {}
	local currentName: string? = className
	while currentName do
		local class = database:GetClass(currentName)
		if not class then
			break
		end
		for name, property in class.Properties do
			if seen[name] or SKIPPED_PROPERTIES[name] or property.Scriptability ~= READABLE_SCRIPTABILITY or hasAnyTag(property.Tags, SKIPPED_TAGS) then
				continue
			end
			seen[name] = true
			table.insert(properties, { name = name, datatype = tostring(property.Datatype) })
		end
		currentName = class.Superclass
	end
	table.sort(properties, function(left, right)
		return left.name < right.name
	end)
	propertyCache[className] = properties
	return properties
end

-- rbxmx of one fresh instance of className carrying only this property; nil and a reason when that is impossible
function Diff.wrapProperty(className: string, property: string, value: any): (string?, string?)
	local createOk, wrapper = pcall(roblox.Instance.new, className)
	if not createOk then
		return nil, `cannot create {className}: {wrapper}`
	end

	local applyOk, applyError = pcall(function()
		if property == ATTRIBUTES_PROPERTY then
			for name, attribute in value do
				wrapper:SetAttribute(name, attribute)
			end
		elseif property == TAGS_PROPERTY then
			for _, tag in value do
				wrapper:AddTag(tag)
			end
		else
			wrapper[property] = value
		end
	end)
	if not applyOk then
		return nil, `cannot set {className}.{property}: {applyError}`
	end

	local serializeOk, rbxmx = pcall(roblox.serializeModel, { wrapper }, true)
	if not serializeOk then
		return nil, `cannot serialize {className}.{property}: {rbxmx}`
	end
	return rbxmx, nil
end

local function getFullName(instance: roblox.Instance?): string
	if not instance then
		return ""
	end
	local names = {}
	local current: roblox.Instance? = instance
	while current do
		table.insert(names, 1, current.Name)
		current = current.Parent
	end
	return table.concat(names, PATH_SEPARATOR)
end

local function readValue(instance: roblox.Instance, property: string): (boolean, any)
	if property == ATTRIBUTES_PROPERTY then
		return true, instance:GetAttributes()
	end
	if property == TAGS_PROPERTY then
		local tags = instance:GetTags()
		table.sort(tags)
		return true, tags
	end
	return pcall(function()
		return instance[property]
	end)
end

-- Properties without a reflection default throw until set, so both sides throwing means both are unset
local function isMissingDefault(failure: any): boolean
	return string.find(tostring(failure), MISSING_DEFAULT_MARKER, 1, true) ~= nil
end

local function valuesEqual(className: string, property: string, left: any, right: any): boolean
	if left == right then
		return true
	end

	local leftType = typeof(left)
	if leftType ~= typeof(right) then
		return false
	end
	if leftType == "number" then
		return left ~= left and right ~= right
	end
	if leftType == "string" or leftType == "boolean" or leftType == "nil" then
		return false
	end

	-- Datatypes without an __eq: the wrapper rbxmx is the canonical form, and is what would be applied anyway
	local leftXml = Diff.wrapProperty(className, property, left)
	local rightXml = Diff.wrapProperty(className, property, right)
	return leftXml ~= nil and leftXml == rightXml
end

local function findPathRule(ignore: Ignore, path: string): IgnorePath?
	for _, entry in ignore.paths do
		if path == entry.path or string.sub(path, 1, #entry.path + 1) == entry.path .. PATH_SEPARATOR then
			return entry
		end
	end
	return nil
end

local function findPropertyRule(ignore: Ignore, instance: roblox.Instance, property: string): IgnoreProperty?
	for _, entry in ignore.properties do
		if entry.property == property and (not entry.class or instance:IsA(entry.class)) then
			return entry
		end
	end
	return nil
end

local function getKey(instance: roblox.Instance): string
	return instance.Name .. KEY_SEPARATOR .. instance.ClassName
end

type UncheckedCount = { class: string, property: string, instanceCount: number }

type Context = {
	ignore: Ignore,
	structureOnly: { [string]: boolean },
	changeSet: ChangeSet,
	ignoredCounts: { [string]: IgnoredEntry },
	-- Properties @lune/roblox cannot read fail on every instance of a class; they are counted and reported per class
	uncheckedCounts: { [string]: UncheckedCount },
}

local function newContext(ignore: Ignore, structureOnly: { [string]: boolean }): Context
	return {
		ignore = ignore,
		structureOnly = structureOnly,
		changeSet = Diff.newChangeSet(),
		ignoredCounts = {},
		uncheckedCounts = {},
	}
end

local function recordIgnored(context: Context, rule: { path: string?, class: string?, property: string?, reason: string })
	local entry: IgnoredEntry = { path = rule.path, class = rule.class, property = rule.property, reason = rule.reason, count = 1 }
	local key = getIgnoredKey(entry)
	local existing = context.ignoredCounts[key]
	if existing then
		existing.count += 1
	else
		context.ignoredCounts[key] = entry
	end
end

local diffInstances

local function diffProperty(context: Context, base: roblox.Instance, final: roblox.Instance, path: string, property: PropertyInfo, index: number)
	local propertyName = property.name
	local rule = findPropertyRule(context.ignore, final, propertyName)

	-- An ignore rule is only recorded when it hides something that would otherwise have been reported
	local function report(entry: UnsupportedEntry?, changed: ChangedEntry?)
		if rule then
			recordIgnored(context, rule)
		elseif entry then
			table.insert(context.changeSet.unsupported, entry)
		elseif changed then
			table.insert(context.changeSet.changed, changed)
		end
	end

	local baseOk, baseValue = readValue(base, propertyName)
	local finalOk, finalValue = readValue(final, propertyName)
	if not baseOk or not finalOk then
		local baseUnset = not baseOk and isMissingDefault(baseValue)
		local finalUnset = not finalOk and isMissingDefault(finalValue)
		if baseUnset and finalUnset then
			return
		end

		if baseUnset or finalUnset then
			report({
				path = path,
				property = propertyName,
				reason = "property is set on one side only and has no default to compare against",
			})
			return
		end

		if rule then
			recordIgnored(context, rule)
			return
		end
		local uncheckedKey = `{final.ClassName}.{propertyName}`
		local unchecked = context.uncheckedCounts[uncheckedKey]
		if unchecked then
			unchecked.instanceCount += 1
		else
			context.uncheckedCounts[uncheckedKey] = { class = final.ClassName, property = propertyName, instanceCount = 1 }
		end
		return
	end

	if property.datatype == "Ref" then
		if getFullName(baseValue) ~= getFullName(finalValue) then
			report({ path = path, property = propertyName, reason = "instance reference property changed" })
		end
		return
	end

	local className = final.ClassName
	if valuesEqual(className, propertyName, baseValue, finalValue) then
		return
	end

	local fromXml, fromReason = Diff.wrapProperty(className, propertyName, baseValue)
	local toXml, toReason = Diff.wrapProperty(className, propertyName, finalValue)
	if not fromXml or not toXml then
		report({
			path = path,
			property = propertyName,
			reason = fromReason or toReason or "cannot wrap property value",
		})
		return
	end
	report(nil, {
		path = path,
		property = propertyName,
		from = fromXml,
		to = toXml,
		className = className,
		index = index,
	})
end

local function diffOwnProperties(context: Context, base: roblox.Instance, final: roblox.Instance, path: string, index: number)
	for _, property in getProperties(final.ClassName) do
		diffProperty(context, base, final, path, property, index)
	end
	diffProperty(context, base, final, path, { name = ATTRIBUTES_PROPERTY, datatype = "Attributes" }, index)
	diffProperty(context, base, final, path, { name = TAGS_PROPERTY, datatype = "Tags" }, index)
end

-- Diffs one matched pair with the path rules off, to learn whether an ignore rule or a dotted name hides anything
local function hasDifferences(context: Context, base: roblox.Instance, final: roblox.Instance, path: string, index: number): boolean
	local scratch = newContext({ paths = {}, properties = context.ignore.properties }, context.structureOnly)
	diffInstances(scratch, base, final, path, index)
	return not Diff.isEmpty(scratch.changeSet)
end

-- A structure-only node stands in for an instance the snapshot did not capture, so it is never added or removed itself
local function emitSubtree(context: Context, instance: roblox.Instance, path: string, index: number, kind: string)
	local pathRule = findPathRule(context.ignore, path)
	if pathRule then
		recordIgnored(context, pathRule)
		return
	end
	if string.find(instance.Name, PATH_SEPARATOR, 1, true) then
		table.insert(context.changeSet.unsupported, { path = path, reason = DOT_REASON })
		return
	end

	if context.structureOnly[path] then
		local occurrences: { [string]: number } = {}
		for _, child in instance:GetChildren() do
			local key = getKey(child)
			occurrences[key] = (occurrences[key] or 0) + 1
			emitSubtree(context, child, `{path}{PATH_SEPARATOR}{child.Name}`, occurrences[key], kind)
		end
		return
	end

	if kind == "added" then
		table.insert(context.changeSet.added, {
			path = path,
			rbxmx = roblox.serializeModel({ instance }, true),
			className = instance.ClassName,
			index = index,
		})
	else
		table.insert(context.changeSet.removed, { path = path, className = instance.ClassName, index = index })
	end
end

local function diffChildren(context: Context, base: roblox.Instance, final: roblox.Instance, path: string)
	local baseGroups: { [string]: { roblox.Instance } } = {}
	for _, child in base:GetChildren() do
		local key = getKey(child)
		baseGroups[key] = baseGroups[key] or {}
		table.insert(baseGroups[key], child)
	end

	local finalCounts: { [string]: number } = {}
	for _, finalChild in final:GetChildren() do
		local key = getKey(finalChild)
		local index = (finalCounts[key] or 0) + 1
		finalCounts[key] = index
		local childPath = `{path}{PATH_SEPARATOR}{finalChild.Name}`
		local baseChild = baseGroups[key] and baseGroups[key][index]
		local pathRule = findPathRule(context.ignore, childPath)
		if not baseChild then
			emitSubtree(context, finalChild, childPath, index, "added")
		elseif pathRule then
			if hasDifferences(context, baseChild, finalChild, childPath, index) then
				recordIgnored(context, pathRule)
			end
		elseif string.find(finalChild.Name, PATH_SEPARATOR, 1, true) then
			if hasDifferences(context, baseChild, finalChild, childPath, index) then
				table.insert(context.changeSet.unsupported, { path = childPath, reason = DOT_REASON })
			end
		else
			diffInstances(context, baseChild, finalChild, childPath, index)
		end
	end

	local baseSeen: { [string]: number } = {}
	for _, baseChild in base:GetChildren() do
		local key = getKey(baseChild)
		local index = (baseSeen[key] or 0) + 1
		baseSeen[key] = index
		if index > (finalCounts[key] or 0) then
			emitSubtree(context, baseChild, `{path}{PATH_SEPARATOR}{baseChild.Name}`, index, "removed")
		end
	end
end

function diffInstances(context: Context, base: roblox.Instance, final: roblox.Instance, path: string, index: number)
	if not context.structureOnly[path] then
		diffOwnProperties(context, base, final, path, index)
	end
	diffChildren(context, base, final, path)
end

local function sortedValues<T>(map: { [string]: T }): { T }
	local keys = {}
	for key in map do
		table.insert(keys, key)
	end
	table.sort(keys)
	local values = {}
	for _, key in keys do
		table.insert(values, map[key])
	end
	return values
end

-- Children match by (Name, ClassName); among duplicates of one key, by order among those duplicates only
function Diff.trees(base: roblox.Instance, final: roblox.Instance, rootPath: string, ignore: Ignore): ChangeSet
	local context = newContext(ignore, ignore.structureOnly or {})
	if base.ClassName ~= final.ClassName then
		warn(`Diff.trees: root class differs at {rootPath} ({base.ClassName} vs {final.ClassName})`)
		table.insert(context.changeSet.unsupported, { path = rootPath, reason = "root class differs between trees" })
		return context.changeSet
	end
	diffInstances(context, base, final, rootPath, 1)

	for _, unchecked in sortedValues(context.uncheckedCounts) do
		table.insert(context.changeSet.unsupported, {
			class = unchecked.class,
			property = unchecked.property,
			instanceCount = unchecked.instanceCount,
			reason = getUncheckedReason(unchecked.instanceCount),
		})
	end
	context.changeSet.ignored = sortedValues(context.ignoredCounts)
	return context.changeSet
end

return Diff
