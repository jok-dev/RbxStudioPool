# Mac probe spike

Probes for running rbx-studio-pool on macOS. Findings go here as they are found. Each says whether it was verified or assumed.

Machine: Mac16,7 (arm64), macOS 26.5.1, Studio 0.746.0.7461269, Rojo 7.7.1 through Aftman.

- `macmcp.py`: the Python spike's `StudioMcp` client pointed at the Mac `StudioMCP` binary.
- `probe0_mcp.py`: lists the Studio MCP's tools and Studios.
- `probe1_multi.py N [--mode bin|open]`: launches N Studios on their own copies of `state/blank.rbxl` and checks each one through the MCP.
- `probe2_args.py`, `probe2_cloud.py`: multi-client launch arguments; opening a cloud place.
- `probe3_server_rbxl.py`: does a place copied to `server.rbxl` run in a test server.
- `probe4_mcp_windows.py`: the MCP's view of every test window.
- `probe5_focus.py`, `probe5b_focus_clients.py`, `focuswatch.swift`: focus stealing (build `swiftc -O focuswatch.swift -o state/focuswatch`).
- `probe7_stop.py`: signals, killed clients, killed servers.
- Probes that start test servers move the user's `~/Documents/Roblox/server.rbxl` aside and put it back (`macmcp.server_rbxl`).
- `state/` (gitignored): per-run copies, plus the two places the probes use, built with `rojo build -o state/<name>.rbxl` (Rojo 7.7.1):
  - `blank.rbxl`: `{ "name": "MacProbe", "tree": { "$className": "DataModel", "Workspace": { "$className": "Workspace" } } }`
  - `marker.rbxl`: the same with `"$attributes": { "ProbeMark": "marker3" }` on Workspace.

## Findings (2026-11-05)

- **StudioMCP is a plain stdio binary** at `/Applications/RobloxStudio.app/Contents/MacOS/StudioMCP` (no `mcp.bat`). The spike's client initializes against it unchanged. Verified. With no Studio connected it lists no tools and no Studios.
- **The bundle binary opens a local place:** `.../Contents/MacOS/RobloxStudio <copy.rbxl>` started Studio, logged in automatically (log: `Authenticated : NO`, then `login [end][success]`) and opened the file in ~2s (`open place (identifier = …/slot1.rbxl)`). Verified, one Studio.
- **Setup, same as Windows:** Studio's MCP server setting must be on. The first probe ran before it was and saw no Studios. Turning it on took effect in the running Studio without a relaunch. Verified.
- **Question 1, several Studios at once: passes.** 3 Studios at once, each on its own copy, each its own MCP studio id. Each Studio was in the MCP ~1–2s after launch and answered Luau at once (blank place). A marker written into each Studio read back from that Studio only, so no window opened the wrong file. Verified with both launch modes:
  - `.../Contents/MacOS/RobloxStudio <copy.rbxl>`: the PID comes straight from the launch. Prefer this one for the pool.
  - `open -n -a /Applications/RobloxStudio.app --args <copy.rbxl>`: also works, but `open` returns no PID, so the pool would have to diff `pgrep`.
- **The edit Studio's MCP `name` is `null`** on the Mac. On Windows only test windows had `name: null`, so name can't be used to tell edit Studios from test windows here. Verified.
- **SIGTERM does not stop Studio.** It was still running 15s after `kill PID` (and minutes after an earlier one). SIGKILL stops it at once, and its `RobloxCrashHandler` child exits with it. Verified.
- **A killed Studio leaves `<copy>.rbxl.lock`** next to its place copy, as on Windows. No AutoRecovery files after these short runs (they are written after a few minutes open). Verified.
- **Question 2, launch arguments: the Windows ones work unchanged** (`probe2_args.py`). Verified, except the cloud place line:
  - Local place: the `.rbxl` path as the only argument (question 1).
  - Minimal server line: `RobloxStudio -task StartServer -localProjectFile F -placeId 0 -universeId 0 -port 0 -creatorId 0 -creatorType 0 -placeVersion 0 -numTestServerPlayersUponStartup 1` started a server, and the server started its client as its own child (`pgrep -P <server>`). Both were in the MCP 14s after launch. `get_studio_state` says `Server` / `Client`, as on Windows.
  - The client line, read with `ps -o args= -p PID` (or the first lines of its Studio log), matches Windows: `-rbxTransportToken … -rbxTransportPort … -playerEmulatorSerializedValues {…} -numTestServerPlayersUponStartup 1 -port <server port> -placeId 0 -universeId 0 -localProjectFile F -task StartClient -userid … -parentPid <server pid> -parentSessionGuid … -baseUrl … -instanceId StudioPlayer_0`. The first client is `StudioPlayer_0`.
  - Adding a player: the client line with `-instanceId StudioPlayer_9` joined the running test at once. It was `Player2`, and the server listed `Player1=-1,Player2=-2`.
  - SIGKILL on the server, its client and the added client left no Studio processes. Whether killing only the server ends its clients is question 7.
  - Cloud place (`probe2_cloud.py`): `RobloxStudio -task EditPlace -placeId 15309587074 -universeId 5280141384` opened it. It was in the MCP after 4s and answered Luau after 36s (download included): `game.PlaceId`/`GameId` were the place's. It is a Team Create place: the pool's existing check (`NetworkClient` present, `Players` lists the user) detects it on the Mac too. Read only; Studio was SIGKILLed, nothing saved. Verified.
- **Question 3, first look: a test server opens `~/Documents/Roblox/server.rbxl`, not its `-localProjectFile`.** The server's log says `open place (identifier = /Users/jok/Documents/Roblox/server.rbxl)`. On this machine that file is 47 MB, from 2026-04-18: a leftover from an earlier Studio test of some game, so the probe's test ran that place instead of the blank copy. Clients log `open place (identifier = )`; they load from the server.
- **Question 3, `server.rbxl`: the Windows fix works on the Mac** (`probe3_server_rbxl.py`). With a marker place copied to `~/Documents/Roblox/server.rbxl`, a test server started on a blank copy read the marker on both the server and its client. The earlier run without the copy ran the old file instead. The test didn't rewrite the file. With the tiny marker place, server and client were in the MCP after 3s (vs 14s for the 47 MB leftover). The user's file was moved aside and put back. Verified. So the pool needs only a per-platform path: `%LOCALAPPDATA%\Roblox\server.rbxl` on Windows, `~/Documents/Roblox/server.rbxl` on the Mac. The race with a user pressing Clients and Servers at the same moment is the same as on Windows.
- **Question 4, Studio MCP and test windows: same as Windows** (`probe4_mcp_windows.py`, server + 2 clients + 1 added from a copied client line, marker place swapped in at `server.rbxl` and put back). Verified:
  - `list_roblox_studios` lists every window as its own studio, all with `name: null`. All 3 were in the MCP 4s after launch; the added client 3s after its launch.
  - `get_studio_state` says `Focused DataModel in the viewport: Server` or `Client` per window.
  - `execute_luau` by `studio_id` reaches the right window: each client returns its own `LocalPlayer.Name` (Player1, Player2, and Player3 for the added one), the server has no LocalPlayer and counts 3 players.
  - The wrong DataModel fails with `Target is not reachable (ExecuteLuau.loadCode, DMN_ID_PLAYSERVER)` (or `…PLAYCLIENT`) after ~2s, so it tells server from client as on Windows. The right one answers in ~0.1s.
  - An attribute set on `workspace` from the server (`RspGroup`) was seen on every client of the group within 1s.
- **Question 5, focus stealing: much milder than Windows, and avoidable for every Studio the pool launches** (`probe5_focus.py`, `probe5b_focus_clients.py`; `focuswatch.swift` logs every app activation from `NSWorkspace`). Measured with Brave in front, blank/marker places:
  - **Plain launch** (bundle binary): Studio activated itself 5 times in the first ~3s of launch, then never again: 0 while loading, 0 on each of 2 Play starts and stops (Play verified running: `Current Studio Mode: Play`). Windows: ~10 on launch and ~8 per Play start.
  - **`open -n -g -j -a RobloxStudio.app --args …`** (background, hidden): 0 activations through launch, load, Play start and stop. The hidden Studio works fully: MCP, Luau, Play, and `screen_capture` returns a real rendered frame during Play (on Windows, hidden-desktop window captures had a blank viewport; the MCP capture worked there too).
  - **Test server launched with `open -g -j`:** the server itself takes no focus, but the clients it starts as its own children do (2–3 activations each in their first ~3s). An added client launched with `open -g -j` takes none; one run directly takes 1–3.
  - **The first client can't be launched by the pool:** it needs the server's `-rbxTransportToken`, which shows only in the clients' own command lines (not the server's log). So a test always has one server-started client (the pool already starts servers with 1 client and adds the rest).
  - **Hiding on activation undoes a grab:** with a watcher that calls `NSRunningApplication.hide()` on any pool Studio that becomes active, focus was back on the user's app within 0.01–0.6s every time. Re-activating the user's previous app instead (`activate()`) worked as well (0.02–0.46s). Activating another app from a background helper is allowed. The app re-activates itself after a hide, so the watcher must keep running, not hide once.
  - Remaining cost: keystrokes typed in those ≤0.6s windows go to Studio. Only the first client of each test does this when the pool launches everything else with `open -g -j`.
  - Not measured: a separate macOS user. Not needed if the above holds up under the real pool.
- **Question 6, paths and toolchain.** Verified unless marked:
  - **One Studio install,** `/Applications/RobloxStudio.app` (bundle id `com.Roblox.RobloxStudio`, version in `Contents/Info.plist`, here 0.746.0.7461269). No `Versions` folder as on Windows. Binary `Contents/MacOS/RobloxStudio`, MCP `Contents/MacOS/StudioMCP`.
  - **Auto-update kills every running Studio.** The installer log (`~/Library/Logs/Roblox/RobloxStudioInstaller_*.log`) shows: download `RobloxStudioApp.zip` to `~/Library/Roblox`, unzip to `~/Library/Roblox/UnzippedBundle`, `Terminating apps with bundleId com.Roblox.RobloxStudio`, move the new bundle over `/Applications/RobloxStudio.app`, launch it. So an update ends pool Studios (and the user's) and relaunches one plain Studio. On Windows, a probe Studio was relaunched under the new version instead. Seen in the log of today's update, not provoked. What triggers the installer (Studio's own check at launch?) is assumed, not seen.
  - **Logs:** `~/Library/Logs/Roblox/<version>_<time>Z_Studio_<id>_last.log`; each starts with the full command line. Installer logs alongside.
  - **AutoSaves:** `~/Library/Application Support/Roblox/RobloxStudio/AutoSaves/<place file name>_AutoRecovery_N.rbxl`. The first probe's Studio (open ~5 min, then killed) left `slot1_AutoRecovery_0.rbxl`; named after the local file, not a place id. Deleted. Later launches of files named `slot1.rbxl` answered Luau at once, so no blocking prompt, but whether a recovery dialog showed was not seen. The user's autosave interval here is 300s.
  - **Plugins:** `~/Documents/Roblox/Plugins` (holds `RojoManagedPlugin.rbxm`), as the Rust stub has it.
  - **Test server place:** `~/Documents/Roblox/server.rbxl` (question 3). Studio also keeps per-user folders there (`~/Documents/Roblox/<userid>`, `0`, `DefaultInstances`).
  - **Settings:** `~/Library/Roblox` (`GlobalBasicSettings_13_Studio.xml`, `ClientSettings`, …); the Studio MCP keeps `auth.token` and `tools-cache.json` in `~/Library/Roblox/StudioMCP`.
  - **Rojo shims have no `.exe`:** Aftman's is `~/.aftman/bin/rojo` (verified, Rojo 7.7.1). Rokit's would be `~/.rokit/bin/rojo` (Rokit not installed here; assumed). `repo.py`'s `toolchain_rojo` looks for `rojo.exe` and needs the platform's name.
- **Question 7, stopping** (`probe7_stop.py`, background test server + 2 clients). Verified:
  - **Studio ignores SIGTERM,** edit Studios and test servers alike (still alive 10s later). SIGKILL ends it at once (~0.2s), and its crash handler with it.
  - **A killed client leaves the server waiting ~60s,** as on Windows: Player2's process SIGKILLed, the server still listed it for 61s.
  - **Killing the server does not end its clients.** The remaining client was still running, and still listed in the MCP, 90s after the server was SIGKILLed. On Windows `taskkill /T` killed the group. The pool must kill each client itself: note the clients (`pgrep -P <server>`) before killing the server, since they are reparented once it dies. Killing by process group not tried.
- Studio polls `http://localhost:34872/api/rojo` every couple of minutes (a stock Rojo plugin installed on this machine). Harmless.
