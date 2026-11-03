# Findings

Spike 0 (headless Claude) lived in the earlier prototype and is not included here.

## Spike 1

**Question:**

**Answer (yes/no/partly):**

**Evidence:**

**Design impact:**

## Spike 2

**Question:**

**Answer (yes/no/partly):**

**Evidence:**

**Design impact:**

## Spike 3

**Question:**

**Answer (yes/no/partly):**

**Evidence:**

**Diff (3b), measured locally with Lune 0.10.5:** `lune/lib/Diff.luau` + `Snapshot.luau` diff two snapshot directories and pass `lune/tests/diff.test.luau`. Changed values are wrapped as one-property rbxmx and round-trip when `Diff.trees` is called directly (UDim2, enums, attributes, tags, CFrame; also `Lighting.ClockTime` on a directly supplied root). Through the snapshot pipeline root-instance properties are NOT captured (Dump serializes descendants only, Snapshot rebuilds roots as structure only), so every change set carries a standing `unsupported` entry per root saying so; capturing them needs Studio-side reflection and is an open item for the live pass. Properties `@lune/roblox` cannot read (`CollisionFidelity`, `FluidFidelity`, `Font`, `FontSize`, `formFactor`, `InterpolationThrottling`: "malformed property info") are reported as class-scoped `unsupported` entries `{ class, property, instanceCount, reason }` meaning unchecked on all N instances of that class, aggregated across all roots; real edits to them are not detected. The shipped `ignore.json` only hides BasePart Position/Orientation/Rotation (derived from CFrame), and every rule that hid something is listed in `changeset.json` under `ignored` with a count. Properties absent from Lune 0.10.5's reflection database are invisible to the diff; open item for the live pass: compare against the properties the serialized XML actually carries.

**Design impact:**

## Spike 4

**Question:**

**Answer (yes/no/partly):**

**Evidence:**

**Design impact:**
