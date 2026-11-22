# Memory rules for the Python pool

Approved 2026-11-22. Builds on Spike 5 in [FINDINGS.md](../../../FINDINGS.md).

## Revised after the Windows checks (2026-11-22)

The checks this design asked for changed two of its three parts. Details are in the Windows check under Spike 5 in [FINDINGS.md](../../../FINDINGS.md).

- **1. Stop the embedded browser: dropped from the Python pool.** A pool Studio on Windows has no browser processes, and a hand-opened Studio has about 0.3 GB of them. It stays a Linux item for the Rust backend.
- **2. `--mode server-only`: built on `StudioTestService:ExecuteRunModeAsync`, not `RunService:Run()`.** `Run()` doesn't restore the place. The replacement does, and Studio treats it as a playtest with a Server DataModel, so `play stop`, logs, `--wait`, hooks and the release check work as they do for Play. It saves about half of what Play adds, not all of it.
- **3. Wear tracking: unchanged.**

The sections below are the design as first approved.

## Intent

A pool-run Studio holds memory it doesn't need: an embedded browser, a client in playtests that only test the server, and growth left behind by each playtest. The pool should cut these on its own, so more Studios fit on one machine with no change to any game and nothing for an agent to remember.

This goes into the Python pool (`spikes/python-pool/`), which is what's in use today. The rules are about how the pool behaves, not about a platform, so they carry over to the Rust daemon later.

## Goals

- Stop each Studio's embedded browser once it's ready, by default.
- Let an agent run a playtest with no client.
- Notice when a Studio has grown from long use, tell the agent, and replace it between sessions.

## Non-goals

- Linux support. It waits for the Rust crate.
- Anything game-specific, such as trimming a place's content.
- Budgeting slots by measured memory instead of `--cap`. Worth doing later.

## Anti-goal

**The pool never closes a Studio an agent holds because of memory.** It may recommend. It only replaces a Studio after the lease is released.

## Approaches

For where the rules live:

1. **In the Python pool, on the existing lease and slot model (picked).** Each rule fits a mechanism that's already there: launch, `play`, notices, and the "dirty" check at release. Small, usable now, cheap to port.
2. **In the Rust daemon only.** Nothing is thrown away, but the daemon can't run Studios yet, so the savings wait for it.
3. **Outside the pool, as a watchdog script.** It can't see leases, so it can't tell an idle Studio from one in use, and can't deliver a recommendation to the agent.

For wear, the choice was what to do when a Studio passes the threshold: close it (rejected: it breaks an agent's session), or recommend during the lease and replace at release (picked).

## Design

Three changes, one PR each.

### 1. Stop the embedded browser

- After `_launch` finishes and the Studio answers through the Studio MCP, kill every `msedgewebview2.exe` in that Studio's process tree. Never earlier: login and first-run use the browser.
- On by default. `acquire --keep-browser` opts out, for leases that need the Toolbox, Assistant or start page.
- The slot records that its browser was stopped. `_pick` doesn't hand such a warm slot to a `--keep-browser` lease, which gets a fresh Studio. A slot with its browser intact can serve either kind, and has its browser stopped when a lease without the flag takes it.
- `status` shows the browser state per slot.
- **Check first, on Windows:** the saving (it was 2.5–3.3 GB under Wine), that Studio doesn't respawn the browser, and that Edit, Play, Luau and Rojo keep working. If Studio respawns it, stop and rethink before building.
- The check must also show that stopping one Studio's browser leaves other Studios' browsers alone, including a Studio the user has open outside the pool.
- Multi-client groups are left alone. They're always closed at release.
- Failing to stop the browser never fails the acquire.

### 2. `play start --mode server-only`

- `play LEASE start --mode server-only` runs `RunService:Run()` on the Edit DataModel through `execute_luau`, instead of `start_stop_play`. `--mode play` is the default and is today's behaviour.
- The slot remembers which mode is running. `play stop` ends that one (`RunService:Stop()` for server-only). `play status` reports it.
- `--wait REGEX` works as it does for Play.
- Multi-client leases and Team Create slots reject it with a clear error. Starting one mode while the other is running is rejected too.
- Logs and `--wait` only see lines from the current run, not earlier runs on the same Studio.
- A lease released while a run is going closes the Studio, as releasing mid-Play does today.
- **Open until the Windows check:** whether stopping a server-only run puts the place back as it was. Under Wine it added almost no memory, which suggests it runs in the Edit DataModel with no copy to restore from. If the place isn't restored, the mode leaves each run's leftovers behind, and the user decides whether to drop it or ship it with the Studio always closed at release.
- `--restart-on-code` restarts in the mode that was running.
- **Check first, on Windows:** what `get_studio_state` reports as the mode during a server-only run, which DataModel name `execute_luau` and log capture need there, and whether the Rojo live sync and watch hooks attach the way they do to a Play server. The answers decide how `_start_stop`, `_wait_mode`, `_live_dm` and the release-time dirty check handle the mode. If log capture can't work in it, say so in the result rather than fail silently.

### 3. Wear tracking

- Each slot counts playtests started through the pool (both modes) and records its Studio's memory when it first became ready. Memory is the Studio process's private bytes, read after each `play stop` and at release, and recorded first when the acquire finishes (after the place has loaded and synced). It is never judged during a playtest, when a client's worth of extra memory would look like wear.
- **Check first, on Windows:** repeat Spike 5's five-cycle table, since its numbers are from Wine, and confirm the defaults below don't trip early.
- A slot is **worn** when playtests ≥ `RSP_WORN_PLAYS` (default 40) or growth since ready ≥ `RSP_WORN_GROWTH_MB` (default 1500). Either trips it. `0` turns a threshold off.
- **During a lease:** when the slot becomes worn, one notice goes to the lease through the existing `notices` path, once per lease: how many playtests, how much growth, and that releasing and acquiring again gets a fresh Studio when convenient. Nothing is closed.
- **At release:** worn is one more reason in `_dirty_reason`, so the Studio is closed instead of kept warm. The next acquire launches fresh.
- A worn slot that is somehow idle is never reused by `_pick`.
- `status` shows playtests, memory now and growth per slot.

### Docs

`SKILL.md` gets a few lines per change: the `--keep-browser` flag and what's missing without it, `--mode server-only` and when it fits (no client, no `LocalPlayer`, no UI), and what the wear notice means. The README's command list gets the two flags.

## Left to the builder

- The exact notice wording and `status` field names.
- How to read a process's memory with the standard library only (the pool is stdlib-only and Windows-only).
- Test shape: unit tests for the pure parts (wear decision, slot matching for `--keep-browser`) next to `test_group_ids.py`. The rest is checked live against a Studio.
