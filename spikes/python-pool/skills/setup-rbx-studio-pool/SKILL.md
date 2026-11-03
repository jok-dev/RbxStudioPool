---
name: setup-rbx-studio-pool
description: Set up the current Roblox repo for rbx-studio-pool - write its .rbx-studio-pool/pool.toml and a CLAUDE.md section telling agents to test in Studio through the pool.
disable-model-invocation: true
---

# Set up a repo for rbx-studio-pool

Goal: after this, any agent in this repo tests in Roblox Studio through the pool, and a pool Studio of a fresh checkout boots the game cleanly.

- `<RSP_HOME>` = the rbx-studio-pool checkout on this machine (ask the user if you can't find it). Write the real path, not the placeholder, into everything below.
- `RSP` = `python <RSP_HOME>/spikes/python-pool/rsp.py`
- Usage skill (what agents read) = `<RSP_HOME>/spikes/python-pool/SKILL.md`
- pool.toml format = the docstring at the top of `<RSP_HOME>/spikes/python-pool/repo.py`

Read the usage skill and the pool.toml format before step 1.

## 1. Survey the repo

Done when you can state each of these, with the file it came from:
- the Rojo project file (usually `default.project.json`);
- the toolchain manifest (`rokit.toml` / `aftman.toml`);
- every file the game needs that is generated or installed rather than committed: compare the project's `$path`s and the code's requires with `.gitignore`, then find the command that produces each in the repo's setup scripts or docs (codegen like `blink`, `wally install`);
- the line the server prints once booted (search `src/server` for its startup `print`);
- whether a `.rbx-studio-pool/pool.toml` or a `rbx-studio-pool` section in CLAUDE.md/AGENTS.md already exists (then update it in place).

## 2. Find the place

Ask the user whether the game has a published place. If yes, get its place ID and universe ID: from the create.roblox.com URL (`/experiences/<universe>/places/<place>`), or, if they have it open in Studio, from the Roblox Studio MCP (`list_roblox_studios` names the place; `execute_luau` on it returns `game.PlaceId`, `game.GameId`, and Team Create = `game:FindService("NetworkClient") ~= nil`). Only read from the user's Studio. A Team Create place goes in pool.toml all the same; tell the user the pool will refuse it unless `--team-create` is passed.

## 3. Write `.rbx-studio-pool/pool.toml`

Only what this repo needs, each entry with a one-line comment saying why:
- `place` / `universe` from step 2 (local copies then act as that place);
- one `[[prepare]]` per generated/installed input from step 1, with `creates` when the output's existence proves it is current (installed packages), without it when it must rerun (codegen);
- a `command` `[[watch]]` that reruns each codegen when its source changes;
- `project` only if it is not `default.project.json`.

Show it to the user before writing.

## 4. Write the CLAUDE.md section

Into the repo's CLAUDE.md (AGENTS.md if that is the repo's agent file; create CLAUDE.md if neither exists), between these markers, replacing anything already between them:

```markdown
<!-- rbx-studio-pool:start -->
## Testing in Roblox Studio

Run, playtest and inspect this game through rbx-studio-pool, the shared Studio pool on this machine: read `<RSP_HOME>/spikes/python-pool/SKILL.md` first. Use pool leases for every Studio session instead of opening Studio or calling the Roblox Studio MCP directly.

- Start: `python <RSP_HOME>/spikes/python-pool/rsp.py acquire --worktree <this checkout> --restart-on-code --client <task name>`
- Booted when the server prints `<boot line from step 1>`: `rsp play LEASE start --wait "<boot line regex>"`
- Finish with `rsp release LEASE --close`, also after failures.
- Pool settings for this repo: `.rbx-studio-pool/pool.toml`.
<!-- rbx-studio-pool:end -->
```

## 5. Prove it on a fresh checkout

Clone the repo into a temporary folder (so generated files are absent, as in a new worktree), copy in only what `[[prepare]]` cannot produce, then follow the section you just wrote: acquire, `play start --wait` on the boot line, `logs --level warning`, release `--close`. Delete the clone.

Done when the playtest reaches the boot line and `logs --level warning` shows no lines from the game itself. A failure means step 1 missed an input: fix pool.toml and rerun. If the daemon is not reachable (`RSP status`), ask the user to start it (`RSP serve --cap 2`).

## 6. Report

Tell the user what was written (both files), what the fresh-checkout run showed, and the commit you suggest. Commit only if they ask; if their current branch is a throwaway, offer to commit on `main` through a temporary worktree and cherry-pick.
