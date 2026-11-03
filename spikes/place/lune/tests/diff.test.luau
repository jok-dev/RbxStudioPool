local fs = require("@lune/fs")
local process = require("@lune/process")
local roblox = require("@lune/roblox")
local serde = require("@lune/serde")

local Diff = require("../lib/Diff")
local Snapshot = require("../lib/Snapshot")

local TEMP_DIR = "spikes/place/lune/tests/.diff-test-out"
local ROOT_PATH = "StarterGui"
local NO_IGNORE: Diff.Ignore = { paths = {}, properties = {} }
local ROOT_REASON = "root instance properties are not captured by the snapshot (e.g. Lighting.ClockTime)"

local failures = 0

local function check(name: string, condition: boolean)
	if condition then
		print(`PASS {name}`)
	else
		failures += 1
		warn(`FAIL {name}`)
	end
end

local function createChild(className: string, name: string, parent: roblox.Instance): roblox.Instance
	local instance = roblox.Instance.new(className)
	instance.Name = name
	instance.Parent = parent
	return instance
end

local function createRoot(): roblox.Instance
	local root = roblox.Instance.new("Folder")
	root.Name = ROOT_PATH
	return root
end

local function readValue(rbxmx: string, property: string): any
	return roblox.deserializeModel(rbxmx)[1][property]
end

local function writeChunk(directory: string, path: string, instance: roblox.Instance)
	fs.writeFile(`{directory}/{path}.rbxm`, roblox.serializeModel({ instance }, false))
end

local function writeManifest(directory: string, manifest: { [string]: any })
	fs.writeFile(`{directory}/manifest.json`, serde.encode("json", manifest))
end

local function hasUnsupported(changeSet: Diff.ChangeSet, path: string, reason: string): boolean
	for _, entry in changeSet.unsupported do
		if entry.path == path and entry.reason == reason then
			return true
		end
	end
	return false
end

-- identical_trees_empty
do
	local base = createRoot()
	local final = createRoot()
	for _, root in { base, final } do
		local frame = createChild("Frame", "Main", root)
		frame.Position = roblox.UDim2.new(0.5, 1, 0, 2)
		createChild("Frame", "Title", frame).LayoutOrder = 3
	end
	check("identical_trees_empty", Diff.isEmpty(Diff.trees(base, final, ROOT_PATH, NO_IGNORE)))
end

-- one_property_change
do
	local base = createRoot()
	local final = createRoot()
	createChild("Frame", "Main", base).Position = roblox.UDim2.new(0, 1, 0, 2)
	createChild("Frame", "Main", final).Position = roblox.UDim2.new(0.5, 3, 0.25, 4)
	local changeSet = Diff.trees(base, final, ROOT_PATH, NO_IGNORE)
	local entry = changeSet.changed[1]
	check("one_property_change: single entry", #changeSet.changed == 1 and #changeSet.added == 0 and #changeSet.removed == 0 and #changeSet.unsupported == 0)
	check("one_property_change: path and property", entry ~= nil and entry.path == "StarterGui.Main" and entry.property == "Position")
	check("one_property_change: from round-trips", entry ~= nil and readValue(entry.from, "Position") == roblox.UDim2.new(0, 1, 0, 2))
	check("one_property_change: to round-trips", entry ~= nil and readValue(entry.to, "Position") == roblox.UDim2.new(0.5, 3, 0.25, 4))
end

-- reorder_same_named_siblings_no_change
do
	local base = createRoot()
	local final = createRoot()
	for position = 1, 3 do
		createChild("Frame", "Frame", base).LayoutOrder = position
	end
	createChild("TextLabel", "Inserted", final)
	for position = 1, 3 do
		createChild("Frame", "Frame", final).LayoutOrder = position
	end
	local changeSet = Diff.trees(base, final, ROOT_PATH, NO_IGNORE)
	check("reorder_same_named_siblings_no_change: only the insert is reported", #changeSet.added == 1 and #changeSet.changed == 0 and #changeSet.removed == 0)
end

-- duplicates pair by order among themselves
do
	local base = createRoot()
	local final = createRoot()
	for position = 1, 3 do
		createChild("Frame", "Frame", base).LayoutOrder = position
		createChild("Frame", "Frame", final).LayoutOrder = if position == 2 then 20 else position
	end
	local changeSet = Diff.trees(base, final, ROOT_PATH, NO_IGNORE)
	local entry = changeSet.changed[1]
	check("duplicates: only the second duplicate changed", #changeSet.changed == 1 and entry.index == 2 and entry.className == "Frame")

	createChild("Frame", "Frame", final)
	local grown = Diff.trees(base, final, ROOT_PATH, NO_IGNORE)
	check("duplicates: extra trailing duplicate is an add", #grown.added == 1 and grown.added[1].index == 4)
end

-- same name, different class are different siblings
do
	local base = createRoot()
	local final = createRoot()
	createChild("Frame", "Thing", base)
	createChild("TextLabel", "Thing", final)
	local changeSet = Diff.trees(base, final, ROOT_PATH, NO_IGNORE)
	check("class_change_is_add_and_remove", #changeSet.added == 1 and #changeSet.removed == 1 and #changeSet.changed == 0)
end

-- added_and_removed
do
	local base = createRoot()
	local final = createRoot()
	createChild("Frame", "Gone", base)
	local added = createChild("Frame", "Shop", final)
	added.Size = roblox.UDim2.new(0, 10, 0, 20)
	createChild("TextLabel", "Title", added).Text = "Candy"
	local changeSet = Diff.trees(base, final, ROOT_PATH, NO_IGNORE)
	check("added_and_removed: counts", #changeSet.added == 1 and #changeSet.removed == 1 and #changeSet.changed == 0)
	check("added_and_removed: removal listed", changeSet.removed[1].path == "StarterGui.Gone")
	local entry = changeSet.added[1]
	check("added_and_removed: added path", entry.path == "StarterGui.Shop")
	local restored = roblox.deserializeModel(entry.rbxmx)[1]
	check("added_and_removed: rbxmx round-trips", restored.Name == "Shop" and restored.Size == roblox.UDim2.new(0, 10, 0, 20) and restored.Title.Text == "Candy")
end

-- dot_in_name_flagged
do
	local base = createRoot()
	local final = createRoot()
	createChild("Frame", "a.b", base).LayoutOrder = 1
	createChild("Frame", "a.b", final).LayoutOrder = 2
	local changeSet = Diff.trees(base, final, ROOT_PATH, NO_IGNORE)
	check("dot_in_name_flagged: unsupported with reason", hasUnsupported(changeSet, "StarterGui.a.b", "name contains '.'"))
	check("dot_in_name_flagged: nothing changed", #changeSet.changed == 0 and #changeSet.added == 0 and #changeSet.removed == 0)

	createChild("Frame", "c.d", final)
	createChild("Frame", "e.f", base)
	local addedRemoved = Diff.trees(base, final, ROOT_PATH, NO_IGNORE)
	check("dot_in_name_flagged: add and remove flagged, not emitted", hasUnsupported(addedRemoved, "StarterGui.c.d", "name contains '.'") and hasUnsupported(addedRemoved, "StarterGui.e.f", "name contains '.'") and #addedRemoved.added == 0 and #addedRemoved.removed == 0)

	local quietBase = createRoot()
	local quietFinal = createRoot()
	createChild("Frame", "a.b", quietBase)
	createChild("Frame", "a.b", quietFinal)
	check("dot_in_name_flagged: unchanged dotted name is quiet", Diff.isEmpty(Diff.trees(quietBase, quietFinal, ROOT_PATH, NO_IGNORE)))
end

-- service_property_wraps_or_flags
do
	local base = roblox.Instance.new("Lighting")
	local final = roblox.Instance.new("Lighting")
	final.ClockTime = 3.5
	local changeSet = Diff.trees(base, final, "Lighting", NO_IGNORE)
	local entry
	for _, candidate in changeSet.changed do
		if candidate.property == "ClockTime" then
			entry = candidate
		end
	end
	local flagged = false
	for _, candidate in changeSet.unsupported do
		if candidate.property == "ClockTime" and candidate.reason ~= "" then
			flagged = true
		end
	end
	check("service_property_wraps_or_flags: never missing", entry ~= nil or flagged)
	check("service_property_wraps_or_flags: round-trips when changed", entry == nil or (entry.path == "Lighting" and readValue(entry.to, "ClockTime") == 3.5))
end

-- wrapProperty
do
	local wrapped, reason = Diff.wrapProperty("Frame", "Position", roblox.UDim2.new(0, 5, 0, 6))
	check("wrapProperty: wraps a property", wrapped ~= nil and reason == nil and readValue(wrapped, "Position") == roblox.UDim2.new(0, 5, 0, 6))
	local unknownWrapped, unknownReason = Diff.wrapProperty("NotARealClass", "Position", 1)
	check("wrapProperty: unknown class gives nil and a reason", unknownWrapped == nil and unknownReason ~= nil)
	local badWrapped, badReason = Diff.wrapProperty("Frame", "NotAProperty", 1)
	check("wrapProperty: unknown property gives nil and a reason", badWrapped == nil and badReason ~= nil)
end

-- attributes, tags and enums
do
	local base = createRoot()
	local final = createRoot()
	createChild("Part", "Block", base)
	local block = createChild("Part", "Block", final)
	block:SetAttribute("Label", "x")
	block:AddTag("Glow")
	block.Material = roblox.Enum.Material.Wood
	local changeSet = Diff.trees(base, final, ROOT_PATH, NO_IGNORE)
	local properties = {}
	for _, entry in changeSet.changed do
		properties[entry.property] = entry
	end
	check("attributes diffed", properties.Attributes ~= nil and roblox.deserializeModel(properties.Attributes.to)[1]:GetAttribute("Label") == "x")
	check("tags diffed", properties.Tags ~= nil and roblox.deserializeModel(properties.Tags.to)[1]:HasTag("Glow"))
	check("enum diffed", properties.Material ~= nil and readValue(properties.Material.to, "Material") == roblox.Enum.Material.Wood)
end
-- unreadable properties are reported once per class with a count, never with one instance's path
do
	local base = createRoot()
	local final = createRoot()
	for _, root in { base, final } do
		createChild("TextLabel", "One", root)
		createChild("TextLabel", "Two", root)
	end
	local changeSet = Diff.trees(base, final, ROOT_PATH, NO_IGNORE)
	local fontEntries = {}
	for _, entry in changeSet.unsupported do
		if entry.property == "Font" then
			table.insert(fontEntries, entry)
		end
	end
	local fontEntry = fontEntries[1]
	check("unchecked_property_is_class_scoped_entry", #fontEntries == 1 and fontEntry.path == nil and fontEntry.class == "TextLabel" and fontEntry.instanceCount == 2 and #changeSet.changed == 0)
	check("unchecked_property_reason", fontEntry ~= nil and fontEntry.reason == "cannot read with @lune/roblox; unchecked on all 2 instances")

	local merged = Diff.newChangeSet()
	Diff.append(merged, changeSet)
	Diff.append(merged, changeSet)
	local mergedFont
	for _, entry in merged.unsupported do
		if entry.property == "Font" then
			mergedFont = entry
		end
	end
	check("unchecked_property_aggregates_across_roots", mergedFont ~= nil and mergedFont.instanceCount == 4 and mergedFont.reason == "cannot read with @lune/roblox; unchecked on all 4 instances")

	local ignored = Diff.trees(base, final, ROOT_PATH, { paths = {}, properties = { { property = "Font", reason = "test" } } })
	local ignoredFontEntries = 0
	for _, entry in ignored.unsupported do
		if entry.property == "Font" then
			ignoredFontEntries += 1
		end
	end
	local fontRule = ignored.ignored[1]
	check("ignored_unreadable_property_is_recorded", ignoredFontEntries == 0 and fontRule ~= nil and fontRule.property == "Font" and fontRule.count == 2 and fontRule.reason == "test")
end

-- a part moved only via CFrame reports CFrame, and the derived Position/Orientation ignores are recorded
do
	local base = createRoot()
	local final = createRoot()
	createChild("Part", "Block", base)
	local moved = createChild("Part", "Block", final)
	moved.CFrame = roblox.CFrame.new(5, 6, 7) * roblox.CFrame.Angles(0, math.rad(45), 0)
	-- A real snapshot carries the derived values alongside CFrame
	moved.Position = moved.CFrame.Position
	moved.Orientation = roblox.Vector3.new(0, 45, 0)
	local ignore: Diff.Ignore = {
		paths = {},
		properties = {
			{ class = "BasePart", property = "Position", reason = "derived from CFrame" },
			{ class = "BasePart", property = "Orientation", reason = "derived from CFrame" },
			{ class = "BasePart", property = "Rotation", reason = "derived from CFrame" },
		},
	}
	local changeSet = Diff.trees(base, final, ROOT_PATH, ignore)
	local changedProperties = {}
	for _, entry in changeSet.changed do
		table.insert(changedProperties, entry.property)
	end
	table.sort(changedProperties)
	local ignoredProperties = {}
	for _, entry in changeSet.ignored do
		table.insert(ignoredProperties, entry.property)
	end
	table.sort(ignoredProperties)
	check("cframe_move_reports_cframe_only", table.concat(changedProperties, ",") == "CFrame")
	check("cframe_move_derived_ignores_recorded", table.concat(ignoredProperties, ",") == "Orientation,Position")
end

-- ignore_list_applies
do
	local base = createRoot()
	local final = createRoot()
	for _, root in { base, final } do
		createChild("Folder", "Noise", root)
		createChild("Frame", "Real", root)
	end
	createChild("Frame", "Child", final:FindFirstChild("Noise"))
	final.Real.LayoutOrder = 7
	final.Real.Active = true
	local ignore: Diff.Ignore = {
		paths = { { path = "StarterGui.Noise", reason = "runtime noise" } },
		properties = { { class = "GuiObject", property = "LayoutOrder", reason = "test" } },
	}
	local changeSet = Diff.trees(base, final, ROOT_PATH, ignore)
	check("ignore_list_applies: ignored path subtree skipped", #changeSet.added == 0)
	check("ignore_list_applies: ignored property skipped, others kept", #changeSet.changed == 1 and changeSet.changed[1].property == "Active")
	local unrestricted: Diff.Ignore = { paths = {}, properties = { { property = "Active", reason = "any class" } } }
	local anyClass = Diff.trees(base, final, ROOT_PATH, unrestricted)
	check("ignore_list_applies: class-less property rule", #anyClass.changed == 1 and anyClass.changed[1].property == "LayoutOrder")
end

-- snapshot directories: containers are structure only, chunks at different depths, skipped passthrough
do
	if fs.isDir(TEMP_DIR) then
		fs.removeDir(TEMP_DIR)
	end
	local baseDir = `{TEMP_DIR}/base`
	local endDir = `{TEMP_DIR}/end`
	fs.writeDir(baseDir)
	fs.writeDir(endDir)

	local function buildChunks(directory: string, variant: "base" | "end")
		local frame = roblox.Instance.new("Frame")
		frame.Name = "Main"
		frame.Position = if variant == "end" then roblox.UDim2.new(0, 9, 0, 9) else roblox.UDim2.new(0, 1, 0, 1)
		writeChunk(directory, "StarterGui.Menu.Main", frame)
		writeChunk(directory, "StarterGui.Shared", createChild("Folder", "Shared", createRoot()))
		if variant == "end" then
			local shop = roblox.Instance.new("Frame")
			shop.Name = "Shop"
			writeChunk(directory, "StarterGui.Menu.Shop", shop)
			local fresh = roblox.Instance.new("Frame")
			fresh.Name = "Fresh"
			writeChunk(directory, "StarterGui.NewContainer.Fresh", fresh)
		else
			local retired = roblox.Instance.new("Frame")
			retired.Name = "Retired"
			writeChunk(directory, "StarterGui.Menu.Retired", retired)
		end
	end
	buildChunks(baseDir, "base")
	buildChunks(endDir, "end")
	writeManifest(baseDir, {
		snapshotId = "base",
		roots = { "StarterGui" },
		chunks = { "StarterGui.Menu.Main", "StarterGui.Shared", "StarterGui.Menu.Retired" },
		containers = { "StarterGui.Menu" },
		skipped = { { path = "StarterGui.Menu.Weird.Name", reason = "name contains '.'" } },
	})
	writeManifest(endDir, {
		snapshotId = "end",
		roots = { "StarterGui" },
		chunks = { "StarterGui.Menu.Main", "StarterGui.Shared", "StarterGui.Menu.Shop", "StarterGui.NewContainer.Fresh" },
		containers = { "StarterGui.Menu", "StarterGui.NewContainer" },
		skipped = { { path = "StarterGui.Menu.Weird.Name", reason = "name contains '.'" }, { path = "StarterGui.Locked", reason = "serialize failed" } },
	})

	local changeSet = Snapshot.diff(baseDir, endDir, NO_IGNORE)
	local changedPaths = {}
	for _, entry in changeSet.changed do
		table.insert(changedPaths, `{entry.path}.{entry.property}`)
	end
	local addedPaths = {}
	for _, entry in changeSet.added do
		table.insert(addedPaths, entry.path)
	end
	table.sort(addedPaths)
	check("snapshot: only the chunk's own property changed (containers never report)", table.concat(changedPaths, ",") == "StarterGui.Menu.Main.Position")
	check("snapshot: added chunks, incl. one under a new container", table.concat(addedPaths, ",") == "StarterGui.Menu.Shop,StarterGui.NewContainer.Fresh")
	check("snapshot: removed chunk", #changeSet.removed == 1 and changeSet.removed[1].path == "StarterGui.Menu.Retired")
	check("snapshot: skipped from both manifests pass through, once", hasUnsupported(changeSet, "StarterGui.Menu.Weird.Name", "name contains '.'") and hasUnsupported(changeSet, "StarterGui.Locked", "serialize failed") and #changeSet.unsupported == 3 and hasUnsupported(changeSet, "StarterGui", ROOT_REASON))

	-- CLI end to end: changeset.json envelope plus added/<n>.rbxmx files
	local outDir = `{TEMP_DIR}/out`
	local ignoreFile = `{TEMP_DIR}/ignore.json`
	fs.writeFile(ignoreFile, serde.encode("json", { paths = { { path = "StarterGui.Menu.Retired", reason = "test" } }, properties = {} }))
	local result = process.exec("lune", { "run", "spikes/place/lune/diff.luau", baseDir, endDir, outDir, "--ignore", ignoreFile })
	check("cli: exits cleanly", result.ok)
	local envelope = serde.decode("json", fs.readFile(`{outDir}/changeset.json`))
	check("cli: envelope shape", #envelope.added == 2 and #envelope.changed == 1 and #envelope.removed == 0 and #envelope.unsupported == 3)
	check("cli: ignored rules listed", #envelope.removed == 0 and #envelope.ignored == 1 and envelope.ignored[1].path == "StarterGui.Menu.Retired" and envelope.ignored[1].reason == "test" and envelope.ignored[1].count == 1)
	local addedFile = envelope.added[1].rbxmx
	check("cli: added rbxmx file round-trips", roblox.deserializeModel(fs.readFile(`{outDir}/added/{addedFile}`))[1].Name == string.match(envelope.added[1].path, "[^.]+$"))

	-- Same path is a chunk in base and a container in end: flagged, not reported as add/remove
	local granularityBase = `{TEMP_DIR}/granularity-base`
	local granularityEnd = `{TEMP_DIR}/granularity-end`
	fs.writeDir(granularityBase)
	fs.writeDir(granularityEnd)
	local panel = createChild("Model", "Panel", createRoot())
	createChild("Frame", "Inner", panel)
	writeChunk(granularityBase, "StarterGui.Panel", panel)
	local inner = roblox.Instance.new("Frame")
	inner.Name = "Inner"
	writeChunk(granularityEnd, "StarterGui.Panel.Inner", inner)
	writeManifest(granularityBase, { roots = { "StarterGui" }, chunks = { "StarterGui.Panel" }, containers = {}, skipped = {} })
	writeManifest(granularityEnd, { roots = { "StarterGui" }, chunks = { "StarterGui.Panel.Inner" }, containers = { "StarterGui.Panel" }, skipped = {} })
	local granularity = Snapshot.diff(granularityBase, granularityEnd, NO_IGNORE)
	check("snapshot: granularity mismatch flagged, no phantom add/remove", #granularity.unsupported == 2 and string.find(granularity.unsupported[1].reason, "granularity", 1, true) ~= nil and #granularity.added == 0 and #granularity.removed == 0)

	fs.removeDir(TEMP_DIR)
end

if failures > 0 then
	warn(`{failures} diff check(s) failed`)
	process.exit(1)
end
print("All diff checks passed")
