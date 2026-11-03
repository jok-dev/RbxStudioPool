# rbx-studio-pool

A local daemon + MCP server that leases Roblox Studio instances from a capped pool to any MCP-capable agent tool. MCP server id: `rbx_studio_pool`.

Split out of an earlier private prototype. The standalone daemon design is still to come.

## Layout

- `crates/rbx-studio-pool` — Studio MCP client, launcher, `SlotPool`, `RojoServer`, `StudioProxy`.
- `crates/process-tree` — child processes in a job object / process group, killed as a tree on drop.
- `rojo-plugin/` — Rojo 7.7.1 auto-connect fork (`build.sh`).
- `spikes/studio-slots` — CLI that exercises the slot pool (Windows, Studio installed).
- `spikes/place/` — Lune place snapshot / diff / merge, plus Studio-side `Dump.lua` and `Apply.lua`.

## Running

`cargo test --workspace` needs no Studio; live tests are `#[ignore]`d and run with `cargo test -- --ignored`. Place tests: `lune run spikes/place/lune/tests/diff.test.luau` (tools pinned in `spikes/place/aftman.toml`). Results are recorded in [FINDINGS.md](FINDINGS.md).
