# Python pool spike (throwaway)

A stdlib-only Python spike of rbx-studio-pool, built to feel out the design before the Rust daemon. Windows only.

- `rsp.py` — the daemon (`serve`) plus a CLI client; HTTP+JSON on `127.0.0.1:7811`, no MCP.
- `pool.py` — earlier single-user interactive shell; `rsp.py` reuses its Studio MCP client and helpers.

```
python rsp.py serve --cap 2
python rsp.py acquire --worktree C:/path/to/repo --client me      # local copy of `rojo build` of the project
python rsp.py acquire --worktree DIR --place-file T.rbxl            # local copy of a template (e.g. saved with the map)
python rsp.py acquire --worktree DIR --place PLACE_ID --universe UNIVERSE_ID       # the cloud place
python rsp.py acquire ... --mount config=C:/path/to/config         # source for a mount the repo's pool.toml declares
python rsp.py acquire ... --restart-on-code                         # code edits during Play restart the playtest
python rsp.py play <lease> start --wait "REGEX" | stop | status     # status includes pool events (restarts, hooks)
python rsp.py logs <lease> [--dm Server|Client|Edit] [--level warning] [--grep RE] [--since CURSOR]
python rsp.py wait-log <lease> "REGEX" [--dm D] [--timeout 60] [--new]
python rsp.py luau <lease> "return #workspace:GetChildren()" [--dm Server] | --file f.luau | -
python rsp.py call <lease> screen_capture '{"capture_id":"ScreenCapture_1"}'   # images saved under state/images
python rsp.py sync <lease> C:/path/to/other/worktree
python rsp.py rojo <lease> status | sync-once | live on|off
python rsp.py tools [NAME] | release <lease> [--close] | status | shutdown
```

Agents: see `SKILL.md`. To set a repo up for agents (its `pool.toml` plus a CLAUDE.md section pointing at `SKILL.md`), run `/setup-rbx-studio-pool` in that repo; install it with `cp -r skills/setup-rbx-studio-pool ~/.claude/skills/`. Per-repo behaviour (mounts, watch hooks, prepare commands) comes from the repo's `.rbx-studio-pool/pool.toml`; the format is in `repo.py`. Nothing game-specific is left in the pool: a game's local remote config is a `pool.toml` plus a hook in that game's repo.

Requires the RojoControl plugin fork (see `rojo-plugin/README.md`). `RSP_LEASE_TTL` / `RSP_IDLE_TTL` (seconds) override the 600 / 900 defaults.

## What it does

- Cap on Studios; FIFO queue when full; an idle warm Studio is evicted to make room for a different place.
- A lease is a random id. Unused leases expire (TTL counted from when `acquire` returns); a dead Studio ends its lease.
- Studio MCP tools pass straight through with `studio_id` forced to the lease's Studio; `list_roblox_studios` is blocked.
- `acquire` refuses Team Create places unless `team_create` is set, and only one pool slot per Team Create place.
- Release keeps a Studio warm only if it is a non-Team Create edit session (`get_studio_state` says `Current Studio Mode: Edit`).
- `--place-file`: each slot opens its own throwaway copy of a template `.rbxl` (deleted on close), so nothing can ever save to the cloud place.
- A deny list (`DENIED_TOOLS`) blocks tools that reach other Studios or write to the Roblox account (`upload_image`, `generate_*`, `create_skill`/`edit_skill`, `segment_mesh`, `subagent`).
- Pool pids are written to `state/pids.json`; a restarted daemon kills what a crashed one left behind.

## Findings (2026-10-30, measured on one dev machine)

- **Timings:** Studio visible to the Studio MCP ~5–20s after launch, accepts Luau after ~21–28s; `rojo serve` up in ~2.4s; a large game fully synced < 20s after the port attribute is set. Acquire-with-sync ≈ 30–38s fresh, ≈ 3s warm.
- **Memory:** ~1.4 GB per Studio with a large game loaded (2.5 GB for one that had been playtesting). 5 slots ≈ 7–12 GB.
- **Team Create detection** (inside Studio, after load): Edit DataModel has `NetworkClient` and `Players` lists you → Team Create; neither → not. Confirmed on a Team Create place vs a non-TC place. The web API for it needs a login cookie.
- **The Studio MCP keeps listing a killed Studio for ~1s.** The pool waits for it to disappear.
- **Rojo plugin re-sync:** the fork now follows the `RspRojoPort` attribute for the Studio's life (change → end session; new port → connect; nil → disconnect). Switching a warm Studio between worktrees works; live file edits arrive in ~3s.
- **Warm Studios keep leftovers.** Instances from a previous worktree that the next project does not manage stay in the DataModel (seen when switching from a probe project to a game). Same for anything an agent changed with Luau. Reuse needs a policy: fresh Studio per lease, reuse only for the same worktree, or a reset step.
- **Worktrees must be prepared:** a worktree without `wally install` (`Packages` missing) makes `rojo serve` exit; the pool now surfaces Rojo's own error. Each worktree also needs an `aftman.toml` pinning Rojo, or the aftman shim refuses to run.
- **Lease TTL must not tick during launch** (found the hard way: a 20s TTL killed a 27s launch).
- **Local-file mode:** `RobloxStudioBeta.exe <copy.rbxl>` opens the file; ready in 8–15s (vs ~28s for the cloud place) because there is no download and, with a `rojo build` template, no map. `game.PlaceId` and `game.GameId` are 0. A game that branches on its place ids then matches none of them, and DataStores are unavailable, so data-layer behavior at Play time is unknown until a playtest. A realistic template needs the map: save one from Studio (File > Save to File).
- **Local file can impersonate a place:** `game:SetPlaceId(id)` + `game:SetUniverseId(id)` work from `execute_luau` (Edit). The ids carry into the Play server and client and stay after Play stops. With a dev place's ids, `DataStoreService:GetAsync` succeeded in Play, so the copy hits that universe's real DataStores (whatever "Studio access to API services" allows) — same as opening the cloud place. Asset permissions with a mapped template still untested (needs a template with the map).
- **Playtests through the pool work:** `start_stop_play` start/stop, `execute_luau` on `Server` and `Client`, `get_console_output` (truncated by the tool when long), `screen_capture` during Play.
- **Local config:** config repos can be mounted into the DataModel (combined Rojo project in `state/projects/slotN.project.json`: the worktree's project with absolute paths, plus each config repo's top-level entries under the mount target). A game's config loader can be patched to read that tree instead of its remote service; verified in a playtest. `globIgnorePaths` does not apply to absolute paths outside the project folder, hence the per-entry mapping.
- **`execute_luau` gets its own copy of required modules:** requiring a game module from it shows an uninitialised copy, so in-game state has to be reported by the game itself (console prints).
- **Live config in playtests:** when a lease starts Play through the pool, the pool sets the port attribute in the Play server too, so the forked Rojo plugin (now also allowed in the Play server, never the client) syncs into the running game. A per-slot watcher polls mounted folders every second and, on a change, runs the repo's hook in the Play server after Rojo catches up. Measured: file edit -> new value served in ~3s. Whether a game system reacts live depends on it listening for config changes rather than caching at start.
- **Restart on code change** (`--restart-on-code`): the watcher also polls `src/**/*.lua[u]`; a change during a pool-started Play stops and restarts Play (the Edit DataModel already has the code via Rojo). Measured 28.9s from edit to the new code running. Running code cannot be hot-swapped without support from the game's framework (require caches, Scripts do not rerun).
- **Why the Rojo fork:** stock Rojo connects only after a click in its widget (host/port typed), asks to accept the first patch, and joins a Play server only by copying the Edit session when a user setting is on. The fork (~25 lines) connects/switches/disconnects from a CoreGui attribute, auto-accepts its own session, and runs in the Play server.
- **Stock Rojo through `execute_luau` (no plugin) is not viable as is:** threads started by `execute_luau` keep running after the call returns, but HTTP from that context follows game rules ("HTTP requests are not enabled" until `HttpService.HttpEnabled = true`), and once enabled a request to `localhost` never returns and stalls the following MCP calls, while `rojo serve` itself answers instantly (msgpack). So the fork stays; an upstream "external control" API for Rojo is the way off it. Stock Rojo can stay installed and loads beside the fork (harmless).
- **Autosave recovery files:** a killed Studio leaves `%LOCALAPPDATA%\Roblox\RobloxStudio\AutoSaves\<placeId>_AutoRecovery_N.rbxl` (written after a few minutes open), and the next launch of that place opens an "Auto Recovery File Found" modal. The pool now deletes files for the slot's place written since it launched (never older ones, which may be the user's). Local-file slots also left `<copy>.rbxl.lock`, now removed too.
- **RojoControl (paused / live / sync once):** the fork now takes JSON commands on `CoreGui.RojoControlCommand` and reports `CoreGui.RojoControlState` (`state`, `live`, `pending`, `busy`, `error`). Paused sessions count server changes instead of applying them; `sync_once` re-diffs the whole tree. Verified: paused edits stay out of Studio with `pending` counting them, `sync_once` and `live on` apply them, and the same works with raw attributes and no pool (standalone use through the Studio MCP). `acquire`/`sync` now return only after Rojo reports the sync finished, with Rojo's state or error.
- **Agent dogfood (2026-11-02):** a fresh agent did a task in a large game using only `SKILL.md`. Biggest gaps it hit: `get_console_output` is truncated from the front and a per-frame client error drowned everything (fixed with a per-DataModel capture behind `CoreGui.RspLogs`: grouped, filtered, cursor-based, 20k lines, follows Play restarts); no way to wait (fixed: `play start --wait`, `wait-log`); Luau quoting and Edit-only `luau` (fixed: `--dm`, `--file`, stdin); non-JSON errors; `rojo status` failing in Play (now targets the Play server). A code-only `rojo build` of that game cannot boot (some instances and the map only exist in the place), so it needs the cloud place or a saved template; a blank/code-defined game is fine with the built template.
- **Generic repo layer:** mounts + watch hooks + prepare replaced the hardcoded config mapping. A game's config reload is a hook in its own repo, which can check the game is listening and return what it now serves, so a hook result shows whether the change took. Verified on a blank game (mount + hook live, restart 6s) and on a large game's cloud place.
- **The restart lockup seen on a large game** ("Start play hasn't finished yet" after `--restart-on-code`) did not reproduce on a blank game, even with calls hammering the Studio during two restarts. Probably tied to that game's broken code-only boot. Restarts now hold a per-slot play lock (other calls wait), wait for Edit mode between stop and start, and report failures as notices.
- **Focus stealing:** Studio forces itself to the foreground ~10 times while launching and ~8 times on every Play start (Qt "always activate"; `SW_SHOWMINNOACTIVE` at launch changes nothing, and it even re-grabs right after an alt-tab). Virtual desktops (Win+Ctrl+arrows) don't help: Play start still took focus from a fullscreen game, the Studio window ended up on the user's virtual desktop, and `MoveWindowToDesktop` on another process's window is `E_ACCESSDENIED`. A hidden Win32 desktop does: focus is per desktop. Studios there work fully (MCP, Luau, Rojo, Play at 60 fps, real screenshots) and a full pool run logged zero focus changes. `desktop.py`: Studios launch on desktop `rbx-studio-pool`; Ctrl+Alt+F12 (`rsp show`/`hide`) switches the screen there and back (hotkeys are per session, so one registration toggles); a bar on that desktop lists the Studios and has a back button; the daemon switches back on exit. `RSP_VISIBLE=1` launches on the user's desktop instead.
