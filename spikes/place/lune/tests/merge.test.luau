local fs = require("@lune/fs")
local process = require("@lune/process")
local serde = require("@lune/serde")

local Diff = require("../lib/Diff")
local Merge = require("../lib/Merge")

local TEMP_DIR = "spikes/place/lune/tests/.merge-test-out"
local FRAME_PATH = "StarterGui.HUD.Frame"
local ROOT_REASON = "root instance properties are not captured by the snapshot"

local failures = 0

local function check(name: string, condition: boolean)
	if condition then
		print(`PASS {name}`)
	else
		failures += 1
		warn(`FAIL {name}`)
	end
end

local function newSet(id: string, base: { string }?): Merge.SetInput
	local set = Diff.newChangeSet() :: Merge.SetInput
	set.id = id
	set.base = base or {}
	table.insert(set.unsupported, { path = "StarterGui", reason = ROOT_REASON })
	return set
end

local function addChange(set: Merge.SetInput, path: string, property: string, from: string, to: string)
	table.insert(set.changed, { path = path, property = property, from = from, to = to, className = "Frame", index = 1 })
end

local function addRemoved(set: Merge.SetInput, path: string)
	table.insert(set.removed, { path = path, className = "Frame", index = 1 })
end

local function addAdded(set: Merge.SetInput, path: string, rbxmx: string)
	table.insert(set.added, { path = path, rbxmx = rbxmx, className = "Frame", index = 1 })
end

local function findChange(changeSet: Diff.ChangeSet, path: string, property: string): Diff.ChangedEntry?
	for _, entry in changeSet.changed do
		if entry.path == path and entry.property == property then
			return entry
		end
	end
	return nil
end

-- different_properties_combine
do
	local first = newSet("A")
	addChange(first, FRAME_PATH, "Position", "p0", "p1")
	local second = newSet("B")
	addChange(second, FRAME_PATH, "Size", "s0", "s1")
	local merged, conflicts = Merge.changeSets({ first, second })
	check("different_properties_combine: both changes", #merged.changed == 2 and findChange(merged, FRAME_PATH, "Position") ~= nil and findChange(merged, FRAME_PATH, "Size") ~= nil)
	check("different_properties_combine: no conflicts", #conflicts == 0)
end

-- same_property_conflicts
do
	local first = newSet("A")
	addChange(first, FRAME_PATH, "Position", "p0", "p1")
	local second = newSet("B")
	addChange(second, FRAME_PATH, "Position", "p0", "p2")
	local merged, conflicts = Merge.changeSets({ first, second })
	check("same_property_conflicts: one sameProperty conflict", #conflicts == 1 and conflicts[1].kind == "sameProperty")
	check("same_property_conflicts: names both sets and the property", conflicts[1].sets[1] == "A" and conflicts[1].sets[2] == "B" and conflicts[1].paths[1] == `{FRAME_PATH}.Position`)
	check("same_property_conflicts: values carry both sides", conflicts[1].values ~= nil and conflicts[1].values[1].set == "A" and conflicts[1].values[1].to == "p1" and conflicts[1].values[2].set == "B" and conflicts[1].values[2].from == "p0" and conflicts[1].values[2].to == "p2")
	check("same_property_conflicts: first set's edit kept, second withheld", #merged.changed == 1 and merged.changed[1].to == "p1")
end

-- stale_from_conflicts
do
	local first = newSet("A")
	addChange(first, FRAME_PATH, "Position", "p0", "p1")
	local second = newSet("B", { "A" })
	addChange(second, FRAME_PATH, "Position", "other", "p2")
	local merged, conflicts = Merge.changeSets({ first, second })
	check("stale_from_conflicts: one staleFrom conflict", #conflicts == 1 and conflicts[1].kind == "staleFrom")
	check("stale_from_conflicts: values carry both sides", conflicts[1].values ~= nil and conflicts[1].values[1].to == "p1" and conflicts[1].values[2].from == "other" and conflicts[1].values[2].to == "p2")
	check("stale_from_conflicts: first kept", #merged.changed == 1 and merged.changed[1].to == "p1")
end

-- lineage_chain_collapses (from matches the earlier set's `to`)
do
	local first = newSet("A")
	addChange(first, FRAME_PATH, "Position", "p0", "p1")
	local second = newSet("B", { "A" })
	addChange(second, FRAME_PATH, "Position", "p1", "p2")
	local merged, conflicts = Merge.changeSets({ first, second })
	local entry = findChange(merged, FRAME_PATH, "Position")
	check("lineage_chain_collapses: no conflicts", #conflicts == 0)
	check("lineage_chain_collapses: one entry p0 -> p2", #merged.changed == 1 and entry ~= nil and entry.from == "p0" and entry.to == "p2")
end

-- a later set that did not see the latest edit conflicts even when it saw an earlier one
do
	local first = newSet("A")
	addChange(first, FRAME_PATH, "Position", "p0", "p1")
	local second = newSet("C", { "A" })
	addChange(second, FRAME_PATH, "Position", "p1", "p2")
	local third = newSet("B", { "A" })
	addChange(third, FRAME_PATH, "Position", "p1", "p3")
	local _, conflicts = Merge.changeSets({ first, second, third })
	check("unseen_latest_set_conflicts: sameProperty with C", #conflicts == 1 and conflicts[1].kind == "sameProperty" and conflicts[1].sets[1] == "C" and conflicts[1].sets[2] == "B")
end

-- removal_vs_child_edit_conflicts
do
	local first = newSet("A")
	addRemoved(first, "StarterGui.HUD.X")
	local second = newSet("B")
	addChange(second, "StarterGui.HUD.X.Child", "Visible", "v0", "v1")
	local merged, conflicts = Merge.changeSets({ first, second })
	check("removal_vs_child_edit_conflicts: pathOverlap", #conflicts == 1 and conflicts[1].kind == "pathOverlap")
	check("removal_vs_child_edit_conflicts: removal kept, edit withheld", #merged.removed == 1 and #merged.changed == 0)

	local reversed, reversedConflicts = Merge.changeSets({ second, first })
	check("removal_vs_child_edit_conflicts: order independent", #reversedConflicts == 1 and reversedConflicts[1].kind == "pathOverlap" and #reversed.changed == 1 and #reversed.removed == 0)
end

-- edits on parent and child instances are not an overlap
do
	local first = newSet("A")
	addChange(first, "StarterGui.HUD.X", "Visible", "v0", "v1")
	local second = newSet("B")
	addChange(second, "StarterGui.HUD.X.Child", "Visible", "v0", "v1")
	local merged, conflicts = Merge.changeSets({ first, second })
	check("property_edits_on_nested_instances_combine", #conflicts == 0 and #merged.changed == 2)
end

-- added_vs_nested_add_conflicts, and lineage makes the same shape legal
do
	local first = newSet("A")
	addAdded(first, "StarterGui.Shop", "<roblox/>")
	local second = newSet("B")
	addAdded(second, "StarterGui.Shop.Panel", "<roblox/>")
	local _, conflicts = Merge.changeSets({ first, second })
	check("added_vs_nested_add_conflicts: pathOverlap", #conflicts == 1 and conflicts[1].kind == "pathOverlap")

	local based = newSet("B", { "A" })
	addAdded(based, "StarterGui.Shop.Panel", "<roblox/>")
	local merged, basedConflicts = Merge.changeSets({ first, based })
	check("lineage_allows_nested_add: no conflicts, both added", #basedConflicts == 0 and #merged.added == 2)
end

-- removal of an instance the base set added cancels both; deeper removal is flagged
do
	local first = newSet("A")
	addAdded(first, "StarterGui.Shop", "<roblox/>")
	local second = newSet("B", { "A" })
	addRemoved(second, "StarterGui.Shop")
	local merged, conflicts = Merge.changeSets({ first, second })
	check("add_then_remove_cancels", #conflicts == 0 and #merged.added == 0 and #merged.removed == 0)

	local deeper = newSet("B", { "A" })
	addRemoved(deeper, "StarterGui.Shop.Panel")
	local _, deeperConflicts = Merge.changeSets({ first, deeper })
	check("remove_inside_added_subtree_flagged", #deeperConflicts == 1 and deeperConflicts[1].kind == "pathOverlap")
end

-- identical removals dedupe, remove then re-add under lineage keeps both
do
	local first = newSet("A")
	addRemoved(first, "StarterGui.Old")
	local second = newSet("B")
	addRemoved(second, "StarterGui.Old")
	local merged, conflicts = Merge.changeSets({ first, second })
	check("identical_removals_dedupe", #conflicts == 0 and #merged.removed == 1)

	local readd = newSet("B", { "A" })
	addAdded(readd, "StarterGui.Old", "<roblox/>")
	local replaced, replaceConflicts = Merge.changeSets({ first, readd })
	check("lineage_remove_then_readd_keeps_both", #replaceConflicts == 0 and #replaced.removed == 1 and #replaced.added == 1)
end

-- duplicate-name siblings: same path with a different index is a different instance
do
	local first = newSet("A")
	table.insert(first.changed, { path = FRAME_PATH, property = "Position", from = "p0", to = "p1", className = "Frame", index = 1 })
	local second = newSet("B")
	table.insert(second.changed, { path = FRAME_PATH, property = "Position", from = "p0", to = "p2", className = "Frame", index = 2 })
	local merged, conflicts = Merge.changeSets({ first, second })
	check("different_index_is_different_instance", #conflicts == 0 and #merged.changed == 2)

	local same = newSet("C")
	table.insert(same.changed, { path = FRAME_PATH, property = "Position", from = "p0", to = "p3", className = "Frame", index = 2 })
	local _, sameConflicts = Merge.changeSets({ first, second, same })
	check("same_path_same_index_is_same_instance", #sameConflicts == 1 and sameConflicts[1].kind == "sameProperty" and sameConflicts[1].sets[1] == "B")
end

-- removing a duplicate sibling: direct edits match by identity, descendant edits are ambiguous and become conflicts
do
	local first = newSet("A")
	table.insert(first.changed, { path = FRAME_PATH, property = "Position", from = "p0", to = "p1", className = "Frame", index = 1 })
	table.insert(first.changed, { path = FRAME_PATH, property = "Size", from = "s0", to = "s1", className = "Frame", index = 2 })
	table.insert(first.changed, { path = `{FRAME_PATH}.Child`, property = "Visible", from = "v0", to = "v1", className = "Frame", index = 1 })
	local second = newSet("B", { "A" })
	table.insert(second.removed, { path = FRAME_PATH, className = "Frame", index = 2 })
	local merged, conflicts = Merge.changeSets({ first, second })
	check("remove_index_2_keeps_index_1_direct_edit", #merged.removed == 1 and findChange(merged, FRAME_PATH, "Position") ~= nil and findChange(merged, FRAME_PATH, "Size") == nil)
	check("remove_index_2_descendant_edit_conflicts", #conflicts == 1 and conflicts[1].kind == "pathOverlap" and conflicts[1].paths[1] == `{FRAME_PATH}.Child.Visible` and conflicts[1].paths[2] == FRAME_PATH and conflicts[1].sets[1] == "A" and conflicts[1].sets[2] == "B" and conflicts[1].values[1].to == "v1")
	check("remove_index_2_descendant_edit_not_merged", findChange(merged, `{FRAME_PATH}.Child`, "Visible") == nil and #merged.changed == 1)

	local firstRemoved = newSet("B", { "A" })
	table.insert(firstRemoved.removed, { path = FRAME_PATH, className = "Frame", index = 1 })
	local dropped, droppedConflicts = Merge.changeSets({ first, firstRemoved })
	check("remove_index_1_drops_direct_edit_only", #dropped.changed == 1 and findChange(dropped, FRAME_PATH, "Size") ~= nil)
	check("remove_index_1_descendant_edit_conflicts", #droppedConflicts == 1 and droppedConflicts[1].kind == "pathOverlap" and findChange(dropped, `{FRAME_PATH}.Child`, "Visible") == nil)

	local remover = newSet("A")
	table.insert(remover.removed, { path = FRAME_PATH, className = "Frame", index = 2 })
	local later = newSet("B", { "A" })
	table.insert(later.changed, { path = `{FRAME_PATH}.Child`, property = "Visible", from = "v0", to = "v1", className = "Frame", index = 1 })
	local kept, keptConflicts = Merge.changeSets({ remover, later })
	check("lineage_descendant_edit_after_removal_kept", #keptConflicts == 0 and #kept.changed == 1 and #kept.removed == 1)

	local unrelated = newSet("B")
	table.insert(unrelated.changed, { path = `{FRAME_PATH}.Child`, property = "Visible", from = "v0", to = "v1", className = "Frame", index = 1 })
	local unrelatedMerged, unrelatedConflicts = Merge.changeSets({ remover, unrelated })
	check("unrelated_descendant_edit_conflicts", #unrelatedConflicts == 1 and unrelatedConflicts[1].kind == "pathOverlap" and #unrelatedMerged.changed == 0)
end

-- unsupported and ignored combine without dropping anything
do
	local first = newSet("A")
	table.insert(first.unsupported, { class = "Frame", property = "Foo", instanceCount = 2, reason = "x" })
	table.insert(first.unsupported, { path = "StarterGui.A", property = "Ref", reason = "instance reference property changed" })
	table.insert(first.ignored, { path = "Workspace.Noise", reason = "noise", count = 3 })
	local second = newSet("B")
	table.insert(second.unsupported, { class = "Frame", property = "Foo", instanceCount = 5, reason = "x" })
	table.insert(second.unsupported, { path = "StarterGui.B", property = "Ref", reason = "instance reference property changed" })
	table.insert(second.ignored, { path = "Workspace.Noise", reason = "noise", count = 4 })
	local merged = Merge.changeSets({ first, second })
	local standing = 0
	local classScoped
	for _, entry in merged.unsupported do
		if entry.path == "StarterGui" and entry.reason == ROOT_REASON then
			standing += 1
		elseif entry.class == "Frame" then
			classScoped = entry
		end
	end
	check("unsupported: standing root entry deduped", standing == 1)
	check("unsupported: class-scoped counts summed", classScoped ~= nil and classScoped.instanceCount == 7)
	check("unsupported: instance-scoped entries from both sets kept", #merged.unsupported == 4)
	check("ignored: counts summed", #merged.ignored == 1 and merged.ignored[1].count == 7)
end

-- cli: sidecars inlined, ids default to file order, exit code reflects conflicts
do
	if fs.isDir(TEMP_DIR) then
		fs.removeDir(TEMP_DIR)
	end
	fs.writeDir(`{TEMP_DIR}/a/added`)
	fs.writeDir(`{TEMP_DIR}/b`)
	local envelopeA = {
		id = "A",
		base = {},
		added = { { path = "StarterGui.Shop", rbxmx = "0.rbxmx", className = "Frame", index = 1 } },
		changed = { { path = FRAME_PATH, property = "Position", from = "p0", to = "p1", className = "Frame", index = 1 } },
		removed = {},
		unsupported = {},
		ignored = {},
	}
	fs.writeFile(`{TEMP_DIR}/a/added/0.rbxmx`, "<roblox>shop</roblox>")
	fs.writeFile(`{TEMP_DIR}/a/changeset.json`, serde.encode("json", envelopeA))
	local envelopeB = {
		base = { "A" },
		added = {},
		changed = { { path = FRAME_PATH, property = "Size", from = "s0", to = "s1", className = "Frame", index = 1 } },
		removed = {},
		unsupported = {},
		ignored = {},
	}
	fs.writeFile(`{TEMP_DIR}/b/changeset.json`, serde.encode("json", envelopeB))

	local result = process.exec("lune", { "run", "spikes/place/lune/merge", `{TEMP_DIR}/merged.json`, `{TEMP_DIR}/a/changeset.json`, `{TEMP_DIR}/b/changeset.json` })
	check("cli: exits 0 without conflicts", result.ok)
	local merged = serde.decode("json", fs.readFile(`{TEMP_DIR}/merged.json`))
	check("cli: merged envelope", #merged.changed == 2 and #merged.added == 1 and merged.added[1].rbxmx == "<roblox>shop</roblox>")
	check("cli: merged lists its sources", merged.merged ~= nil and merged.merged[1] == "A" and merged.merged[2] == "set2")

	envelopeB.changed[1].property = "Position"
	envelopeB.changed[1].from = "other"
	fs.writeFile(`{TEMP_DIR}/b/changeset.json`, serde.encode("json", envelopeB))
	local conflicted = process.exec("lune", { "run", "spikes/place/lune/merge", `{TEMP_DIR}/merged.json`, `{TEMP_DIR}/a/changeset.json`, `{TEMP_DIR}/b/changeset.json` })
	local output = serde.decode("json", fs.readFile(`{TEMP_DIR}/merged.json`))
	check("cli: exits non-zero on conflicts", not conflicted.ok)
	check("cli: conflicts recorded in output", output.conflicts ~= nil and #output.conflicts == 1 and output.conflicts[1].kind == "staleFrom")

	fs.removeDir(TEMP_DIR)
end

if failures > 0 then
	warn(`{failures} merge check(s) failed`)
	process.exit(1)
end
print("All merge checks passed")
