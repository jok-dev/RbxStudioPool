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

## Spike 5: where does a Studio's memory go, and what can the pool cut?

Measured 2026-11-21/22 on a Linux server (Ryzen 9700X, integrated Radeon GPU) running Studio version-8d7a1009c17e4ff8 under Vinegar (see Spike 6), with a large game opened from a local `.rbxl`. Memory is resident set size (RSS) of `RobloxStudioBeta.exe` unless noted. The Windows numbers may differ: re-measure there before changing the Windows defaults.

**Question:** which memory does a pool-run Studio not need, and which savings can the pool apply on its own, with no change to the game?

**Answer (yes/no/partly):** partly. Three things save real memory with no game changes. Two don't. Most of the rest is the place itself.

**Evidence:**

- **The place is most of it.** In Edit, the large game took about 7.1 GB. Removing content from the place, which is game-specific, brought it to 3.8 GB. The pool can't do this generically. It is the game's choice what goes in its template (see "a realistic template needs the map" in `spikes/python-pool/README.md`).
- **The embedded browser (WebView2) is 2.5–3.3 GB per Studio.** `msedgewebview2.exe` processes host the start page, Toolbox, Marketplace, Assistant and the login flow. Killing them all after the place has loaded saved 2.5–3.3 GB per Studio. Edit, Play, Luau through the Studio MCP and Rojo all kept working. Until Studio restarts, the start page, Toolbox and Assistant stay blank or broken. Studio did not respawn the browser processes during normal use.
- **Run mode instead of Play saves about 1 GB per playtest.** Play starts a server and a client. `RunService:Run()` (from `execute_luau` on the Edit DataModel, stopped with `RunService:Stop()`) runs only the server simulation in the Edit DataModel. Same copy, same place:

  | Mode | Before | During | Change |
  |---|---|---|---|
  | Play | 3.87 GB | 5.20 GB | +1.33 GB |
  | Run | 3.84 GB | 3.87 GB | +0.03 GB |

  Run has no client, no `LocalPlayer` and no character, so it only fits server-side checks. Tests that touch UI, input or anything a client does still need Play.
- **Kernel samepage merging (Linux only) saves about 0.6 GB per extra copy.** With two copies of the same place open and opted in (`PR_SET_MEMORY_MERGE`, see Spike 6), merging saved 1.31 GB (1.19 GB net of its own bookkeeping) with `pages_to_scan=1000`, `sleep_millisecs=20`. `ksmd` showed no measurable CPU in steady state. The saving comes from identical pages across copies (Studio's code, Wine, shared assets), so it grows with the number of copies of the same Studio version. Windows has no per-process equivalent a pool can turn on.
- **Each playtest leaves about 45 MB behind.** Five Play start/stop cycles on one copy, RSS measured after each stop:

  | Cycle | During Play | After stop |
  |---|---|---|
  | 1 | 5.21 GB | 4.28 GB |
  | 2 | 5.28 GB | 4.29 GB |
  | 3 | 5.21 GB | 4.33 GB |
  | 4 | 5.23 GB | 4.38 GB |
  | 5 | 5.25 GB | 4.43 GB |

  The peak during Play doesn't grow, but the floor after each stop does, by about 45 MB a cycle. Clearing the Output window between cycles didn't stop it (it went 4.43 → 4.58 GB over the same five cycles). It isn't released back, so a long-lived Studio slowly grows.
- **No difference:**
  - Turning off script analysis ("Enable Script Analysis" in `GlobalSettings_13.xml`) made no measurable difference in Edit.
  - Stripping modules out of the playtest made Play and Run within 0.1 GB of the full run. This is game-specific anyway, and on a local file the game's boot didn't finish (no MemoryStoreService), so it isn't a fair test.

**Design impact:**

- **Stop the embedded browser after a slot is ready**, by default. Make it per-slot opt-out for leases that want the Toolbox or Assistant. Stop it only after the place has loaded and the Studio MCP sees the Studio, never during launch, because login and the first-run flow use it. A slot that had its browser stopped is marked so a lease asking for the Toolbox gets a fresh Studio.
- **Offer Run mode as a playtest option** (`play start --mode run`) next to Play. Default to Play so existing tests don't change behaviour. Its memory cost is close to nothing, so a server-only test fits several to a machine.
- **Recycle long-lived Studios.** Count playtests and track RSS per slot. Restart an idle slot once it passes a playtest count or a growth threshold since launch, both in config. With about 45 MB a cycle, 50 playtests is about 2 GB.
- **On Linux, opt every Studio into samepage merging** and set `ksmd` to scan fast enough to matter (Spike 6 has how). Report the saving in `status`.
- **Budget slots by measured memory, not a fixed cap.** The cost of a slot depends on the place, the mode, and how long it has lived. The pool already sees RSS, so it can refuse a lease or recycle a slot when free memory runs low, instead of a fixed `--cap`.

## Spike 6: can the pool run Studio on Linux?

**Question:** can Roblox Studio run on a headless Linux server well enough for the pool: logged in, driven through the Studio MCP, playtesting, with several copies open?

**Answer (yes/no/partly):** yes, through Vinegar (Wine in a Flatpak), on a GPU-backed headless Wayland desktop. Setup is in [docs/linux.md](docs/linux.md).

**Evidence:**

- **Graphics:** Studio ran on the integrated Radeon GPU (Mesa RADV through DXVK). Edit and Play ran at 58–60 fps, capped by the virtual display's refresh rate.
- **Login works** once Studio can open a browser: Wine's `winebrowser` calls `xdg-open`. Inside the Flatpak there is none that works, so a shim on Vinegar's `PATH` runs Firefox on the host through `flatpak-spawn --host`. Installing `xdg-desktop-portal` on a headless host instead caused timeouts and a blank login window.
- **The Studio MCP works.** `StudioMCP.exe` from Studio's own version folder runs under Vinegar's Wine as a stdio server. Studio connects to it once "Enable Studio as MCP server" is on. `list_roblox_studios`, `execute_luau` on Edit/Server/Client, `start_stop_play` and `get_console_output` all worked. So the pool's Studio MCP client works unchanged, with a different command to start the MCP.
- **Several copies of the same place work**, each opened from its own `.rbxl` copy, all visible to one StudioMCP.
- **Vinegar is single-instance.** A second `flatpak run org.vinegarhq.Vinegar run <file>` hands its arguments to the Vinegar already running and exits. So the environment a Studio starts with (including the samepage-merging flag) is whatever the first Vinegar had.
- **`flatpak kill org.vinegarhq.Vinegar` kills everything in the app**, including a StudioMCP started through `flatpak run` for the bridge.
- **The samepage-merging flag must be set inside the sandbox.** `PR_SET_MEMORY_MERGE` is inherited by children, but `flatpak run` starts the sandbox through `bwrap` from a fresh process tree, so a flag set on the outside caller doesn't reach Studio. Setting it with `flatpak run --command=python3` inside the sandbox and exec'ing Vinegar there works (`VmFlags` in `/proc/<pid>/smaps` shows `mg`).
- **Input:** `wlrctl` (mouse) and `wtype` (text) drive the Studio window. `wtype` with Ctrl combinations is unreliable through Wine. Paste with `wl-copy` plus a click is more reliable for text, and for anything scriptable the Studio MCP is the way.
- **Viewing:** `wayvnc` on the headless output, plus noVNC, lets a person watch or log in from a browser over an SSH tunnel.

**Design impact:**

- **A Linux platform backend (`platform/linux.rs`) is viable.** It replaces the hidden Win32 desktop with one headless sway session for the whole pool. Focus stealing isn't an issue there: nobody is using that desktop. It also replaces `RobloxStudioBeta.exe <file>` with `vinegar run <file>` and the Windows StudioMCP path with the one under Wine.
- **The pool must own Vinegar's lifetime.** Because Vinegar is single-instance, the pool starts the first Vinegar itself (inside the sandbox, with the samepage-merging flag) and only then opens places. It must never `flatpak kill` the app to stop one Studio: kill that Studio's process instead.
- **Run StudioMCP outside Vinegar's app kill path**, or restart it when it dies, since the pool depends on it for every call.
- **Paths change:** Studio's data (logs, `AutoSaves`, `GlobalSettings_13.xml`, `server.rbxl`) lives under the Wine prefix in `~/.var/app/org.vinegarhq.Vinegar/data/vinegar/prefixes/studio/drive_c/users/<user>/AppData/Local/Roblox`. The autosave cleanup and `server.rbxl` handling from the Python pool apply there.
- **Login is a one-time, per-machine step** done by a person over VNC. The pool should detect a logged-out Studio and say so rather than hang.
