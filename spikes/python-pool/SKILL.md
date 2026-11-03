---
name: rbx-studio-pool
description: Lease a Roblox Studio from the local rbx-studio-pool daemon, sync a git worktree into it with Rojo, and drive it (Luau, playtests, logs, screenshots). Use whenever you need to run, test or inspect Roblox game code in Studio.
---

# Using rbx-studio-pool (Python spike)

The pool runs Roblox Studios for you. You get a **lease** on one Studio, your worktree is synced into it with Rojo, and you drive it through the pool. Do not launch Studio yourself and do not use the Roblox Studio MCP directly: other people's Studios run on this machine.

All commands: `python <RSP_HOME>/spikes/python-pool/rsp.py <command>` (the repo's CLAUDE.md section gives the full path), written `rsp` below. Output is JSON except `luau` (prints what the code returns) and `logs` (one line per log entry). Errors are JSON `{"error": ..., "status": ...}` with exit code 1. If `rsp status` says the daemon is not reachable, ask the user to start it; don't start one.

## Workflow

1. **Acquire** (blocks until Studio is open and synced; ~10s for a small project, ~30s for a big place, longer if queued):
   ```
   rsp acquire --worktree C:/path/to/repo --client <your-name>
   ```
   - With only `--worktree`, the Studio opens a throwaway local copy of `rojo build` of the project (code only; nothing can be saved to Roblox). Fine for games whose world is in the repo.
   - `--place-file T.rbxl`: a local copy of that template instead (e.g. a place saved with its map).
   - `--place P --universe U`: the real cloud place. Needed when the game depends on instances only the place has. Never publish or save it.
   - `--restart-on-code`: editing code during a playtest you started restarts it (~6s small game, ~30s big).
   - The result lists `mounts`, `watches` and `rojo` (`state` should be `live`; on `error`, read `rojo.error`).
2. **Play and read logs:**
   - `rsp play LEASE start --wait "REGEX"`: start a playtest and wait for a Server log line (e.g. your game's "ready" print). It fails fast with the error and its stack if the game errors first. `rsp play LEASE stop`. `rsp play LEASE status` shows the mode plus pool events (restarts, hooks, commands, failures) you haven't seen yet.
   - `rsp logs LEASE [--dm Server|Client|Edit] [--level warning] [--grep REGEX] [--since CURSOR]`: the current playtest's log lines, repeats grouped (`x1515, last #1524`). Not limited by Studio's 512-line history. Each playtest starts a new log (cursor back to #1). Noise from Studio plugins installed on this machine (`log-ignore.txt`) is hidden; `--all` shows it, `--exclude REGEX` hides more.
   - `rsp wait-log LEASE "REGEX" [--dm Client] [--timeout 60]`: wait for a line instead of sleeping. It first waits for pending file changes to be acted on, so right after an edit with `--restart-on-code` it waits for the restart and searches the **new** playtest. Like `play start --wait`, it stops at a game error (`--no-fail-on-error` to keep waiting). `--new` only matches lines printed after the command starts; you rarely need it.
   - `rsp play LEASE wait`: return once file changes have been acted on (a restart finished, hooks and commands run), with what happened in `new_events`.
   - Default DataModel for `luau`/`logs`/`wait-log` is Edit, or Server while playing. Luau may yield (`task.wait` is fine).
3. **Run Luau:** `rsp luau LEASE "return workspace:GetChildren()[1].Name" [--dm Client]`, or `--file check.luau`, or `-` for stdin (avoids shell quoting). It prints what the code returns; return a string, or `HttpService:JSONEncode(...)` for tables.
4. **Other Studio MCP tools:** `rsp call LEASE TOOL 'JSON'`, with `studio_id` filled in for you. `rsp tools` lists the tools with their arguments; `rsp tools NAME` gives the full schema. Example: `rsp call LEASE screen_capture '{"capture_id": "shot1"}'` saves an image under the spike's `state/images` (the path is in the output; read it to look). Blocked tools are marked; don't work around them.
5. **Code changes:** edit files in the worktree. Rojo syncs them to Studio in ~2s. A running playtest does **not** rerun changed scripts: `play stop` then `play start`, or acquire with `--restart-on-code`.
6. **Release** when done, also after failures: `rsp release LEASE --close`.

## Repo settings (`.rbx-studio-pool/pool.toml`)

A repo can declare, in its root:
- **mounts:** outside folders synced into the DataModel (e.g. local config checkouts). Override a mount's folder with `--mount NAME=DIR`.
- **watches:** file changes that run a Luau hook in the running Play server, run a command (e.g. regenerate networking code), or restart Play.
- **prepare:** commands run before Rojo serves (e.g. codegen like `blink`, `wally install`), with the repo's toolchain (Rokit/Aftman) on PATH.

If the game fails to boot with "X is not a valid member" for a file that's gitignored, it is probably generated: check the repo's setup scripts and suggest a `[[prepare]]` step to the user.

If the repo has one, read it: it tells you what the pool will do with your files. Hook results show up in `play status` events.

## Good to know

- Problems the pool hits in the background (a failed restart or hook, log capture failing) come back as `notices` on your next command (on stderr for `luau`/`logs`). Read them.
- If `play start`/`stop` keeps failing with "Start play hasn't finished yet", the Studio is stuck: `rsp release LEASE --close` and acquire again.
- Luau from `luau`/`execute_luau` gets its **own copy of required modules**: requiring a game module there doesn't show the game's state. Read Instances, attributes and logs instead.
- Leases expire after 10 min without commands. Any command refreshes the lease.
- Pool Studios run on a hidden desktop so they never take the user's focus; you look at them with screenshots. When the user asks to see, try or playtest it themselves, get it ready (e.g. `play start`), then run `rsp show LEASE`: their screen switches to the pool desktop with your Studio in front, and the lease stays alive while they're there. Tell them Ctrl+Alt+F12 brings them back. `rsp show` switches the user's whole screen, so run it only when they ask.
- A reused (warm) Studio may hold leftovers from an earlier lease; `--fresh` avoids reuse.
- Screenshots (`screen_capture`) show the Client view during Play. The default camera may face a wall: set `workspace.CurrentCamera` from Client Luau first if you need a specific view.
- In Play, the player is the user's own Roblox account.
