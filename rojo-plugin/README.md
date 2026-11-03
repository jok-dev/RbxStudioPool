# RojoControl (Rojo plugin fork)

Stock Rojo 7.7.1 plugin plus `rojo-control.patch` and `RojoControl.lua`: anything that can run Luau in Studio can
drive it — the Roblox Studio MCP's `execute_luau`, rbx-studio-pool, or the command bar. No extra server needed.
Works in Edit and in the Play server (never the Play client). Use with a stock `rojo serve` (protocol 5, Rojo 7.7.x).

Build: `bash rojo-plugin/build.sh` → `target/RspRojo.rbxm`; copy it into `%LOCALAPPDATA%\Roblox\Plugins`.
It installs beside stock Rojo, which keeps working as usual.

## States

- **disconnected**
- **connecting**
- **paused** — connected to `rojo serve`, but nothing is applied; server changes are counted in `pending`.
- **live** — every change is applied as it arrives (stock Rojo behavior).
- **error** — the last command failed; see `error`.

`sync_once` re-reads the whole tree from the server and applies the difference (like the initial sync), so it is
correct even if the place changed in Studio meanwhile. Turning live on with pending changes does a `sync_once` first.
Sessions started this way skip Rojo's accept dialog.

## Protocol

Command — set `CoreGui.RojoControlCommand` to JSON, with a new `seq` each time:

```lua
local CoreGui = game:GetService("CoreGui")
CoreGui:SetAttribute("RojoControlCommand", '{"seq":1,"op":"connect","port":34872,"live":false}') -- host defaults to localhost
CoreGui:SetAttribute("RojoControlCommand", '{"seq":2,"op":"sync_once"}')
CoreGui:SetAttribute("RojoControlCommand", '{"seq":3,"op":"live","on":true}')
CoreGui:SetAttribute("RojoControlCommand", '{"seq":4,"op":"disconnect"}')
```

State — read `CoreGui.RojoControlState` (JSON, refreshed every 0.25s):

```json
{ "seq": 2, "busy": false, "state": "paused", "live": false, "pending": 0, "lastSync": 1793426248,
  "project": "My Game", "error": null }
```

A command has finished when `seq` equals the one you sent and `busy` is false.

Sessions started through `RojoControlCommand` are silent: the fork mutes Rojo's notification sounds (connect, disconnect, etc.) from a controlled `connect` until 2s after its `disconnect`. Sessions you start from the widget keep your `playSounds` setting.
