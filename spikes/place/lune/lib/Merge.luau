-- Pure merge of change sets in acceptance order. No file or network access.
local Diff = require("./Diff")

local PATH_SEPARATOR = "."
local IDENTITY_SEPARATOR = "|"

-- A change set plus the lineage fields of the envelope: `id`, and `base`, the ids already applied when its slot started
export type SetInput = Diff.ChangeSet & { id: string?, base: { string }? }
-- pathOverlap rules: sets with no lineage between them conflict when a structural entry (add/remove) is an ancestor of, or the
-- same instance as, another set's entry. Within lineage, a descendant edit that a later set (whose base includes the remover)
-- made after the removal is kept. A descendant edit that predates a removal is a conflict, because descendant paths do not
-- encode which duplicate sibling they belong to (Apply resolves intermediate segments to the first match): it is never
-- silently kept or dropped. An edit on the removed instance itself (path|class|index) is dropped as moot.
export type ConflictKind = "sameProperty" | "staleFrom" | "pathOverlap"
-- One side of a sameProperty/staleFrom conflict: the set and the wrapped values it wanted (rbxmx wrapper strings).
-- `values` is {earlier side, later side}; the earlier side is what the merged output kept.
export type ConflictValue = { set: string, from: string, to: string }
export type Conflict = { kind: ConflictKind, paths: { string }, sets: { string }, values: { ConflictValue }? }

type Operation = {
	kind: "added" | "removed" | "changed",
	path: string,
	identity: string,
	setPosition: number,
	entry: any,
}

type LatestChange = { entry: Diff.ChangedEntry, setId: string }

local Merge = {}

local function getIdentity(entry: { path: string, className: string, index: number }): string
	return `{entry.path}{IDENTITY_SEPARATOR}{entry.className}{IDENTITY_SEPARATOR}{entry.index}`
end

local function getAncestors(path: string): { string }
	local segments = string.split(path, PATH_SEPARATOR)
	local ancestors = {}
	for length = 1, #segments - 1 do
		table.insert(ancestors, table.concat(segments, PATH_SEPARATOR, 1, length))
	end
	return ancestors
end

local function isUnderOrEqual(path: string, ancestor: string): boolean
	return path == ancestor or string.sub(path, 1, #ancestor + 1) == ancestor .. PATH_SEPARATOR
end

local function isStructural(operation: Operation): boolean
	return operation.kind ~= "changed"
end

local function hasPathEntry(list: { Diff.UnsupportedEntry }, entry: Diff.UnsupportedEntry): boolean
	for _, candidate in list do
		if candidate.path == entry.path and candidate.property == entry.property and candidate.reason == entry.reason then
			return true
		end
	end
	return false
end

-- Instance-scoped entries are deduplicated when identical (the standing per-root entry every set carries);
-- class-scoped and ignored entries are aggregated by Diff.append, which sums their counts
local function mergeNotes(target: Diff.ChangeSet, source: Diff.ChangeSet)
	local notes = Diff.newChangeSet()
	for _, entry in source.unsupported do
		if not entry.path or not hasPathEntry(target.unsupported, entry) then
			table.insert(notes.unsupported, entry)
		end
	end
	notes.ignored = source.ignored
	Diff.append(target, notes)
end

function Merge.changeSets(sets: { SetInput }): (Diff.ChangeSet, { Conflict })
	local ids: { string } = {}
	local baseLookups: { { [string]: boolean } } = {}
	for position, set in sets do
		ids[position] = set.id or `set{position}`
		local lookup = {}
		for _, baseId in set.base or {} do
			lookup[baseId] = true
		end
		baseLookups[position] = lookup
	end

	local function isRelated(first: number, second: number): boolean
		return baseLookups[first][ids[second]] == true or baseLookups[second][ids[first]] == true
	end

	local conflicts: { Conflict } = {}
	local withheld: { [any]: boolean } = {}

	local operations: { Operation } = {}
	local operationsByPath: { [string]: { Operation } } = {}
	for position, set in sets do
		for kind, entries in { added = set.added, removed = set.removed, changed = set.changed } do
			for _, entry in entries do
				local operation: Operation = {
					kind = kind :: any,
					path = entry.path,
					identity = getIdentity(entry),
					setPosition = position,
					entry = entry,
				}
				table.insert(operations, operation)
				operationsByPath[entry.path] = operationsByPath[entry.path] or {}
				table.insert(operationsByPath[entry.path], operation)
			end
		end
	end

	-- Overlap between sets that did not see each other: the later set's entry is withheld
	local overlapGroups: { [string]: Conflict } = {}
	local function recordOverlap(ancestor: Operation, other: Operation, paths: { string })
		local earlier, later = ancestor, other
		if other.setPosition < ancestor.setPosition then
			earlier, later = other, ancestor
		end
		withheld[later.entry] = true

		local key = `{ancestor.kind}{IDENTITY_SEPARATOR}{ancestor.identity}{IDENTITY_SEPARATOR}{other.setPosition}`
		local group = overlapGroups[key]
		if not group then
			group = { kind = "pathOverlap", paths = {}, sets = { ids[earlier.setPosition], ids[later.setPosition] } }
			overlapGroups[key] = group
			table.insert(conflicts, group)
		end
		for _, path in paths do
			if not table.find(group.paths, path) then
				table.insert(group.paths, path)
			end
		end
	end

	for _, operation in operations do
		for _, sibling in operationsByPath[operation.path] do
			if sibling.setPosition <= operation.setPosition or isRelated(sibling.setPosition, operation.setPosition) then
				continue
			end
			local bothRemoved = sibling.kind == "removed" and operation.kind == "removed"
			if sibling.identity == operation.identity and (isStructural(sibling) or isStructural(operation)) and not bothRemoved then
				recordOverlap(sibling, operation, { operation.path })
			end
		end
		for _, ancestorPath in getAncestors(operation.path) do
			for _, ancestor in operationsByPath[ancestorPath] or {} do
				if isStructural(ancestor) and ancestor.setPosition ~= operation.setPosition and not isRelated(ancestor.setPosition, operation.setPosition) then
					recordOverlap(ancestor, operation, { ancestor.path, operation.path })
				end
			end
		end
	end

	local merged = Diff.newChangeSet()
	local addedByPath: { [string]: { { entry: Diff.AddedEntry, position: number } } } = {}
	local latestChanges: { [string]: LatestChange } = {}

	for position, set in sets do
		local setId = ids[position]

		for _, entry in set.added do
			if withheld[entry] then
				continue
			end
			local copy = table.clone(entry)
			table.insert(merged.added, copy)
			addedByPath[entry.path] = addedByPath[entry.path] or {}
			table.insert(addedByPath[entry.path], { entry = copy, position = position })
		end

		for _, entry in set.removed do
			if withheld[entry] then
				continue
			end

			local identity = getIdentity(entry)
			local cancelled = false
			for _, added in addedByPath[entry.path] or {} do
				if getIdentity(added.entry) == identity and baseLookups[position][ids[added.position]] then
					table.remove(merged.added, table.find(merged.added, added.entry))
					table.remove(addedByPath[entry.path], table.find(addedByPath[entry.path], added))
					cancelled = true
					break
				end
			end

			local insideAdded
			for _, ancestorPath in getAncestors(entry.path) do
				for _, added in addedByPath[ancestorPath] or {} do
					if baseLookups[position][ids[added.position]] then
						insideAdded = ancestorPath
					end
				end
			end

			local duplicate = false
			for _, existing in merged.removed do
				if getIdentity(existing) == identity then
					duplicate = true
				end
			end

			if insideAdded then
				table.insert(conflicts, { kind = "pathOverlap", paths = { insideAdded, entry.path }, sets = { ids[addedByPath[insideAdded][1].position], setId } })
			elseif not cancelled and not duplicate then
				table.insert(merged.removed, table.clone(entry))
			end

			-- An edit on the removed instance itself (full identity) is moot. A descendant edit is not: its path does not say which
			-- duplicate sibling it belonged to, so it becomes a conflict instead of being kept or dropped
			if not insideAdded then
				for index = #merged.changed, 1, -1 do
					local change = merged.changed[index]
					local changeKey = `{getIdentity(change)}{IDENTITY_SEPARATOR}{change.property}`
					if getIdentity(change) == identity then
						table.remove(merged.changed, index)
						latestChanges[changeKey] = nil
					elseif change.path ~= entry.path and isUnderOrEqual(change.path, entry.path) then
						local owner = latestChanges[changeKey].setId
						table.remove(merged.changed, index)
						latestChanges[changeKey] = nil
						table.insert(conflicts, {
							kind = "pathOverlap",
							paths = { `{change.path}{PATH_SEPARATOR}{change.property}`, entry.path },
							sets = { owner, setId },
							values = { { set = owner, from = change.from, to = change.to } },
						})
					end
				end
			end
		end

		for _, entry in set.changed do
			if withheld[entry] then
				continue
			end

			local key = `{getIdentity(entry)}{IDENTITY_SEPARATOR}{entry.property}`
			local latest = latestChanges[key]
			if not latest then
				local copy = table.clone(entry)
				table.insert(merged.changed, copy)
				latestChanges[key] = { entry = copy, setId = setId }
			elseif not baseLookups[position][latest.setId] then
				table.insert(conflicts, { kind = "sameProperty", paths = { `{entry.path}{PATH_SEPARATOR}{entry.property}` }, sets = { latest.setId, setId }, values = {
					{ set = latest.setId, from = latest.entry.from, to = latest.entry.to },
					{ set = setId, from = entry.from, to = entry.to },
				} })
			elseif entry.from ~= latest.entry.to then
				table.insert(conflicts, { kind = "staleFrom", paths = { `{entry.path}{PATH_SEPARATOR}{entry.property}` }, sets = { latest.setId, setId }, values = {
					{ set = latest.setId, from = latest.entry.from, to = latest.entry.to },
					{ set = setId, from = entry.from, to = entry.to },
				} })
			else
				latest.entry.to = entry.to
				latest.setId = setId
			end
		end

		mergeNotes(merged, set)
	end

	return merged, conflicts
end

return Merge
