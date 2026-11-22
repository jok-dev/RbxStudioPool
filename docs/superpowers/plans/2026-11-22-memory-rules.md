# Memory Rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Python pool cut Studio memory on its own: stop the embedded browser, offer a playtest with no client, and replace Studios that have grown from long use without ever closing one an agent holds.

**Architecture:** A new `memory.py` holds everything that doesn't need a pool: reading the process table, finding a Studio's browser processes, reading a process's memory, and the wear decision. `rsp.py` calls it from the places that already own those moments: `acquire` (browser, memory baseline), `play` / `_start_stop` (mode, playtest count, wear check), `_dirty_reason` (replace at release) and `status`.

**Tech Stack:** Python standard library only, Windows only (`ctypes` for process memory, PowerShell `Get-CimInstance` for the process table, as `child_processes` already does). Tests are `unittest`, run from `spikes/python-pool/`.

**Spec:** `docs/superpowers/specs/2026-11-22-memory-rules-design.md`

**Reviewed** 2026-11-22 by a second model against `rsp.py`; this version has its fixes.

## Global Constraints

- Standard library only. No new dependencies.
- The pool never closes a Studio an agent holds because of memory. It only recommends, through `slot.event(..., notice=True)`.
- The flag is `--mode server-only`. The opt-out flag is `--keep-browser`.
- Defaults: `RSP_WORN_PLAYS=40`, `RSP_WORN_GROWTH_MB=1500`, unless Task 3 Step 1 shows they're wrong for Windows. `0` turns a threshold off.
- Play stays the default playtest mode. Existing commands behave as before when no new flag is passed.
- Nothing game-specific goes in the pool.
- One PR per task, in order: `feat/stop-browser`, `feat/server-only-play`, `feat/wear-tracking`. Tasks 2 and 3 build on files Task 1 creates, so each branch starts from `main` once the previous PR has merged, or is stacked on the previous branch if it hasn't.
- All work is in `spikes/python-pool/`. Run tests with `python -m unittest test_memory test_group_ids test_cloudplace` from that folder.
- A live check needs the daemon and a logged-in Studio on this Windows machine: `python rsp.py serve --cap 2`, then `python rsp.py acquire --place-file <any local .rbxl> --client plan`.
- Each task's first step is a live check with a stop condition. When a stop condition is met, stop and report to the user. Don't build around it.

## Review Focus

1. **The browser can't be stopped** (none found, `taskkill` fails, or the process table can't be read): `acquire` still succeeds and returns its lease. Test in Task 1.
2. **Another Studio's browser, or another app's `msedgewebview2.exe`,** is never killed: not a second pool Studio's, not the user's own Studio's, not one whose dead parent's pid was reused. Test in Task 1, and checked live in Step 1.
3. **A second server-only run** doesn't match log lines from the first: `--wait` and `wait-log` start from that run's cursor. Test in Task 2.
4. **A reading taken during Play** never counts as wear: growth is judged only in Edit, after a stop or at release. Test in Task 3.
5. **The Studio exits between finding its pid and reading its memory:** the read returns `None` and nothing raises into `play stop` or `release`. Test in Task 3.

---

### Task 1: Stop the embedded browser

**Files:**
- Create: `spikes/python-pool/memory.py`
- Create: `spikes/python-pool/test_memory.py`
- Modify: `spikes/python-pool/rsp.py` (`Slot`, `Pool.acquire`, `Pool._take_slot`, `Pool._pick`, `Pool.status`, the `acquire` parser and its `/acquire` request in `main`)
- Modify: `spikes/python-pool/SKILL.md`, `spikes/python-pool/README.md`, `FINDINGS.md`

**Interfaces:**
- Produces, in `memory.py`:
  - `BROWSER_IMAGE = "msedgewebview2.exe"`
  - `process_table() -> list[dict]`: every process as `{"pid": int, "parent": int, "image": str, "created": float}`, from one `Get-CimInstance Win32_Process` call (`created` from `CreationDate`, as a timestamp).
  - `browser_roots(table: list[dict], studio_pid: int) -> list[int]`: pids of `BROWSER_IMAGE` processes that descend from `studio_pid` and whose parent is not itself a `BROWSER_IMAGE` process. A child only counts if it was created at or after its parent (Windows keeps a dead parent's pid, which can be reused). Image match is case-insensitive.
  - `stop_browser(studio_pid: int) -> int`: `taskkill /PID <pid> /T /F` on each root; returns how many roots it killed. **Never raises:** any failure, including reading the process table, is logged and returns 0.
- Produces, in `rsp.py`: `Slot.browser: str` (`"running"` or `"stopped"`), `acquire(..., keep_browser=False)`.
- Produces, in `test_memory.py`: `PoolFixture`, the shared mocked pool Tasks 2 and 3 reuse (Step 7).

- [ ] **Step 1: Live check on Windows, before any code.** Open two pool Studios and one Studio of your own outside the pool. Record:
  1. For each `msedgewebview2.exe`, which Studio's process tree it hangs off, and when it appears relative to the Studio answering through the Studio MCP.
  2. The total working set of one pool Studio's browser processes.
  3. After killing that one Studio's browser roots with `taskkill /T /F`: the other pool Studio's and your own Studio's browser processes are all still alive, and their Toolbox still works.
  4. On the Studio whose browser was killed: `rsp luau LEASE "return 1"`, `rsp play LEASE start` and `stop`, a Rojo sync if a worktree is at hand, and the Studio MCP tools `search_asset`, `insert_asset` and `screen_capture` through `rsp call`. Record which work and which fail, and that the Studio doesn't crash.
  5. After 5 minutes and one more Play cycle, the browser processes have not come back.

  **Stop and report to the user if** Studios share a browser process (killing one Studio's kills another's, or later Studios have none under their own pid), Studio respawns the browser, the Studio crashes, or `luau`, `play` or Rojo fail.

- [ ] **Step 2: Record the result** as a "Windows check" bullet under Spike 5's Evidence in `FINDINGS.md`: the saving in GB, which MCP tools stop working, whether it respawned.

- [ ] **Step 3: Write the failing tests** in `test_memory.py`:

```python
import os
import unittest
import memory

def row(pid, parent, image, created):
    return {"pid": pid, "parent": parent, "image": image, "created": created}

TABLE = [
    row(10, 1, "RobloxStudioBeta.exe", 100.0),
    row(11, 10, "msedgewebview2.exe", 105.0),      # browser root
    row(12, 11, "msedgewebview2.exe", 106.0),      # its renderer
    row(13, 10, "RobloxCrashHandler.exe", 101.0),
    row(14, 13, "MSEDGEWEBVIEW2.EXE", 107.0),      # root under another child
    row(15, 10, "msedgewebview2.exe", 50.0),       # older than its "parent": pid 10 was reused
    row(20, 1, "RobloxStudioBeta.exe", 100.0),
    row(21, 20, "msedgewebview2.exe", 105.0),      # another Studio's browser
]


class BrowserRoots(unittest.TestCase):
    def test_finds_only_this_studios_top_browser_processes(self):
        self.assertEqual(sorted(memory.browser_roots(TABLE, 10)), [11, 14])

    def test_other_studio_is_untouched(self):
        self.assertEqual(memory.browser_roots(TABLE, 20), [21])

    def test_no_browser_is_empty(self):
        self.assertEqual(memory.browser_roots(TABLE, 999), [])

    def test_a_parent_cycle_does_not_hang(self):
        table = [row(1, 2, "a.exe", 1.0), row(2, 1, "a.exe", 1.0)]
        self.assertEqual(memory.browser_roots(table, 1), [])


class StopBrowser(unittest.TestCase):
    def test_a_failing_process_table_returns_zero(self):
        with mock.patch.object(memory, "process_table", side_effect=subprocess.TimeoutExpired("powershell", 30)):
            self.assertEqual(memory.stop_browser(10), 0)
```

- [ ] **Step 4: Run them.** `python -m unittest test_memory` → fails with `ModuleNotFoundError: No module named 'memory'`.

- [ ] **Step 5: Implement `memory.py`** with the three functions above. `process_table` follows `child_processes` in `rsp.py` (PowerShell, `ConvertTo-Json -Compress`, a single object comes back as a dict), selecting `ProcessId,ParentProcessId,Name,CreationDate` with no filter.

- [ ] **Step 6: Run the tests** → 5 pass.

- [ ] **Step 7: Write `PoolFixture`** in `test_memory.py`: a `unittest.TestCase` base whose `setUp` builds an `rsp.Pool` the way `test_group_ids.py` does, with these so tests can't hang, pass by accident, or touch a live daemon's files:
  - `pool.mcp` is a `FakeMcp`: `call(tool, args, timeout=...)` records the call and returns `({}, self.state_text)`, where `state_text` defaults to `"- Current Studio Mode: Edit\n- Available DataModels: Edit"`; `luau(studio_id, code, ...)` records the call and returns `self.luau_result` (default `"true"`); `list_studios()` returns `[]`.
  - `Pool._launch` is patched with a side effect that sets `slot.proc` to a mock with `pid = 1000 + slot.n` and `poll()` returning `None`, `slot.studio_id = f"studio{slot.n}"`, and `slot.state = "ready"`.
  - `Pool._close` is patched and recorded as `self.closed` (a list of `(slot.n, why)`). `Pool._save_pids`, `Pool._query_logs`, `Pool._copy_place` and `time.sleep` are patched to do nothing.
  - Helpers: `acquire(**kwargs)` (a local `.rbxl` place file in a temp folder), and `slot(out)` returning the `Slot` for an acquire result.

  Every test asserting a close or a non-reuse asserts the **reason** in `self.closed`, or has a control case that stays warm, because an unconfigured mock makes `_dirty_reason` close everything.

- [ ] **Step 8: Write the failing pool tests** on `PoolFixture`, with `memory.stop_browser` mocked:

```python
    def test_control_a_plain_release_keeps_the_studio_warm(self):
        # acquire, release -> {"closed": False}; self.closed == []

    def test_browser_is_stopped_after_a_fresh_launch(self):
        # stop_browser returns 1 -> called once with the slot's pid; slot.browser == "stopped";
        # acquire result has "browser": "stopped"

    def test_keep_browser_leaves_it_running(self):
        # acquire(keep_browser=True) -> stop_browser not called; slot.browser == "running"

    def test_keep_browser_lease_never_gets_a_stopped_warm_slot(self):
        # acquire + release (self.closed == [], so it is warm), then acquire(keep_browser=True)
        # -> out["reused"] is False

    def test_warm_stopped_slot_is_not_stopped_twice(self):
        # acquire + release, acquire again -> reused True; stop_browser called once in total

    def test_warm_running_slot_is_stopped_for_a_plain_lease(self):
        # acquire(keep_browser=True) + release, acquire -> reused True; stop_browser called once

    def test_nothing_to_stop_is_not_an_error_or_a_notice(self):
        # stop_browser returns 0 -> acquire succeeds; slot.browser == "running"; take_notices(lease) == []

    def test_stop_browser_raising_does_not_lose_the_lease(self):
        # stop_browser raises RuntimeError -> acquire still returns a lease id; the slot is still leased
```

- [ ] **Step 9: Run them** → fail (`acquire() got an unexpected keyword argument 'keep_browser'`).

- [ ] **Step 10: Implement in `rsp.py`.**
  - `Slot.browser: str = "running"`.
  - `acquire(..., keep_browser=False)` passes it to `_take_slot` and `_pick`. In `_pick`'s warm-slot loop, skip a slot when `keep_browser and s.browser == "stopped"`.
  - In `acquire`, **inside** the existing `try` that releases the lease on failure, after the launch-or-reuse and sync: if `not keep_browser and slot.browser == "running"`, call `memory.stop_browser(slot.proc.pid)` wrapped so an exception is logged and treated as 0. More than 0 → `slot.browser = "stopped"` and a plain `slot.event("browser", "stopped the embedded browser")`. 0 → a plain `slot.event("browser", "no embedded browser processes found")`. Neither is a notice.
  - Add `"browser": slot.browser` to `acquire`'s result and to each slot in `status`.
  - Multi-client groups (`clients > 0`) are left alone: they're always closed at release.
  - CLI: `a.add_argument("--keep-browser", action="store_true", help="leave the embedded browser running (needed for the Toolbox, Assistant and start page); costs about <Step 1 figure> GB")`, sent as `keep_browser` in the `/acquire` body.

- [ ] **Step 11: Run all tests** → all pass.

- [ ] **Step 12: Live check.** Restart the daemon. `acquire` without the flag: the result and `status` show `browser: stopped`, no `msedgewebview2.exe` under the slot's pid, `luau` and `play start/stop` work, and your own Studio's Toolbox still works. `acquire --keep-browser --fresh`: browser processes present.

- [ ] **Step 13: Docs.** `SKILL.md`: one bullet saying the pool stops the embedded browser by default, which things stop working (from Step 1, including any MCP tools), and that `--keep-browser` on `acquire` keeps it. `README.md`: add `python rsp.py acquire ... --keep-browser` to the command list with a one-line comment.

- [ ] **Step 14: Commit, push, open the PR** from `feat/stop-browser`.

---

### Task 2: `play start --mode server-only`

**Files:**
- Modify: `spikes/python-pool/rsp.py` (`Slot`, `Pool.play`, `_start_stop`, `_restart_play`, `_live_dm`, `_run_hook`, `_dirty_reason`, `_play_status`, `wait_log`, `logs`, `status`, the `/play` route, the `play` parser and request in `main`)
- Modify: `spikes/python-pool/test_memory.py`, `spikes/python-pool/SKILL.md`, `spikes/python-pool/README.md`, `FINDINGS.md`

**Interfaces:**
- Consumes: `PoolFixture` from Task 1.
- Produces: `MODES = ("play", "server-only")`; `Slot.play_mode: str | None` (the mode running now; kept during a restart, `None` when stopped); `Slot.run_cursor: int` (the Edit log cursor when the current server-only run started); `Slot.ran_server_only: bool`; `play(lease_id, op, wait=None, timeout=120, mode="play")`; `_start_stop(slot, start, mode="play")`.

- [ ] **Step 1: Live check on Windows, before any code.** On an acquired Studio whose place has at least one Script that prints and creates or moves something:
  1. Before running: record `#game:GetDescendants()`, one part's position, and that a known script-created instance doesn't exist.
  2. Run `rsp luau LEASE 'game:GetService("RunService"):Run() return "ok"'`. Record what `rsp call LEASE get_studio_state` prints as "Current Studio Mode" and "Available DataModels" during the run, and what `RunService:IsRunning()` returns.
  3. Record which `--dm` value `rsp luau` needs to reach the running game (`Edit` worked under Wine), and whether `rsp logs LEASE --dm <that>` shows lines the game prints during the run.
  4. With a worktree synced: whether a file edit reaches the running game, and whether a `pool.toml` watch hook runs.
  5. Run `RunService:Stop()`. Record the mode, `IsRunning()`, and **the three values from item 1 again: is the place back to how it was before the run?**
  6. Run again. Record whether scripts run from the top a second time (does the print appear again?).

  Add the answers as a bullet under Spike 5's Evidence in `FINDINGS.md`.

  **Stop and report to the user** with what was found if any of these hold. Each changes what this mode is:
  - The place is **not** restored after Stop (item 5), or scripts don't rerun (item 6). The mode would then leave each run's leftovers in the Studio. The user decides between dropping the mode, or shipping it with the Studio always closed at release and a warning that runs on one lease aren't independent.
  - Items 3 or 4 differ from the Wine result (the game is reached through `Edit`; logs and Rojo keep working there).

  The steps below assume: the place is restored, scripts rerun, the game is reached through `Edit`, and `IsRunning()` tells a run from Edit.

- [ ] **Step 2: Write the failing tests** on `PoolFixture`:

```python
    def test_server_only_start_runs_runservice_not_play(self):
        # play(lease, "start", mode="server-only") -> a luau call containing 'RunService"):Run()';
        # no start_stop_play call; slot.play_mode == "server-only"; result["play_mode"] == "server-only"

    def test_start_fails_if_the_run_did_not_begin(self):
        # FakeMcp.luau_result "false" for the IsRunning check -> PoolError; slot.playing is False

    def test_stop_ends_the_mode_that_is_running(self):
        # after a server-only start, play(lease, "stop") -> a luau call containing 'RunService"):Stop()';
        # no start_stop_play call; slot.play_mode is None

    def test_play_mode_is_unchanged_by_default(self):
        # play(lease, "start") -> start_stop_play called with is_start True; slot.play_mode == "play"

    def test_start_while_running_is_rejected(self):
        # server-only running; play(lease, "start") raises PoolError containing "stop first"; and the reverse

    def test_live_dm_is_edit_during_server_only_and_its_restart(self):
        # playing, play_mode "server-only" -> _live_dm == "Edit"; same with restarting True

    def test_wait_ignores_lines_from_before_this_run(self):
        # the start records slot.run_cursor from the Edit log cursor before Run();
        # play(lease, "start", wait="ready", mode="server-only") calls wait_log with dm "Edit"
        # and a start cursor equal to slot.run_cursor

    def test_hooks_run_in_the_edit_datamodel_during_server_only(self):
        # play_mode "server-only"; _run_hook(...) -> its luau/rojo call targets "Edit", not "Server"

    def test_restart_on_code_keeps_the_mode(self):
        # play_mode "server-only"; _restart_play(slot, changed) -> Stop() then Run(), no start_stop_play;
        # slot.play_mode == "server-only" afterwards; slot.run_cursor was re-recorded

    def test_restart_does_nothing_if_play_was_stopped_meanwhile(self):
        # slot.playing False when _restart_play gets the lock -> no start or stop calls

    def test_multi_client_lease_rejects_server_only(self):
        # slot.group set -> PoolError mentioning server-only (not the generic group error)

    def test_team_create_rejects_server_only(self):
        # slot.team_create True -> PoolError mentioning Team Create

    def test_unknown_mode_is_rejected(self):
        # play(lease, "start", mode="fast") raises PoolError

    def test_release_during_a_run_closes_the_studio(self):
        # server-only running; release -> {"closed": True}; self.closed[0] reason contains "run in progress"

    def test_release_after_a_stopped_run_keeps_it_warm(self):
        # server-only start then stop; release -> {"closed": False}

    def test_a_run_the_agent_started_itself_is_caught_at_release(self):
        # play_mode None, FakeMcp IsRunning result "true" -> release closes with "run in progress"
```

- [ ] **Step 3: Run them** → fail (`play() got an unexpected keyword argument 'mode'`).

- [ ] **Step 4: Implement.**
  - `_start_stop(slot, start, mode="play")`. For `"play"`, today's code. For `"server-only"`:
    - Start: make sure the Edit log capture is installed and read its current cursor into `slot.run_cursor`; run `game:GetService("RunService"):Run()` through `self.mcp.luau` on Edit; confirm `RunService:IsRunning()` is true, else raise `PoolError`; then `slot.playing = True`, `slot.ran_server_only = True`. No Server/Client capture and no `_connect_play_server`.
    - Stop: run `:Stop()`, then poll `IsRunning()` until false (30 s, then `PoolError`). Don't use `_wait_mode` here.
    - Set `slot.play_mode` on start. On stop, choose how to stop from `slot.play_mode`, ignoring the argument, and clear it only when `slot.restarting` is false.
  - `play`: validate `mode` against `MODES`. Before the group branch, reject server-only on a group lease (`"server-only is for single-Studio leases; a multi-client test always has clients"`) and on a Team Create slot (`"server-only would run the game's scripts in a shared Team Create session"`). Reject a start while `slot.play_mode` is set: `f"a {slot.play_mode} playtest is running: stop first"`. Pass `mode` to `_start_stop`. For `--wait` in server-only, call `wait_log` on dm `"Edit"` starting from `slot.run_cursor`.
  - `wait_log` and `logs`: while `slot.play_mode == "server-only"` and the caller gave no cursor, start from `slot.run_cursor`.
  - `_restart_play`: inside `play_lock`, return if `not slot.playing`; read `mode = slot.play_mode` there; stop, then start in `mode`. In its `except`, set `slot.play_mode = None` with `slot.playing = False`.
  - `_live_dm`: `"Server"` only when playing or restarting and `slot.play_mode == "play"`. Otherwise `"Edit"`.
  - `_run_hook`: use `self._live_dm(slot)` where it hardcodes `"Server"`.
  - `_dirty_reason`: before the `get_studio_state` call, return `"server-only run in progress"` when `slot.play_mode == "server-only"`. After the existing Edit-mode check passes, ask `RunService:IsRunning()` through Luau and return `"run in progress"` if true.
  - `_play_status` and `status`: add `"play_mode": slot.play_mode`.
  - `/play` route passes `body.get("mode", "play")`. CLI: `pl.add_argument("--mode", choices=MODES, default="play", help="server-only: run the server with no client (about 1 GB less; no LocalPlayer, character or UI)")`.

- [ ] **Step 5: Run all tests** → all pass.

- [ ] **Step 6: Live check.** `play LEASE start --mode server-only --wait <a line the place prints>` matches. `status` shows `play_mode: server-only`. `play LEASE stop`, then the same start again: `--wait` waits for the new run's line and doesn't return at once on the old one. `play LEASE start` while it runs is rejected. `play LEASE stop`, then `play LEASE start` and `stop` still work as Play. Record the memory each mode adds in the PR description.

- [ ] **Step 7: Docs.** `SKILL.md`: when to use `--mode server-only` (server logic only: no client, `LocalPlayer`, character, UI or input), that `luau` and `logs` use `--dm Edit` during it, that it isn't available on Team Create or multi-client leases, and that Play is the default. `README.md`: add the command line.

- [ ] **Step 8: Commit, push, open the PR** from `feat/server-only-play`.

---

### Task 3: Wear tracking

**Files:**
- Modify: `spikes/python-pool/memory.py`, `spikes/python-pool/test_memory.py`
- Modify: `spikes/python-pool/rsp.py` (constants, `Slot`, `acquire`, `_take_slot`, `_pick`, `_start_stop`, `_dirty_reason`, `status`)
- Modify: `spikes/python-pool/SKILL.md`, `spikes/python-pool/README.md`, `FINDINGS.md`

**Interfaces:**
- Consumes: `PoolFixture` from Task 1; `_start_stop(slot, start, mode)` and `slot.play_mode` from Task 2.
- Produces, in `memory.py`:
  - `private_mb(pid: int) -> float | None`: the process's private bytes in MB (`PrivateUsage` from `PROCESS_MEMORY_COUNTERS_EX`) through `ctypes`: `OpenProcess` with `PROCESS_QUERY_LIMITED_INFORMATION`, `K32GetProcessMemoryInfo`, `CloseHandle`. Use `ctypes.WinDLL("kernel32", use_last_error=True)` and set `restype`/`argtypes` (`OpenProcess` returns a `HANDLE`, which the default `c_int` truncates on 64-bit). `None` if the process is gone or can't be opened. Never raises.
  - `worn_reason(plays: int, growth_mb: float | None, max_plays: int, max_growth_mb: int) -> str | None`: `None` when not worn, else a short reason like `"40 playtests"` or `"grew 1.6 GB since launch"`. A threshold of `0` is off. `growth_mb` of `None` only skips the growth check.
- Produces, in `rsp.py`: `WORN_PLAYS = int(os.environ.get("RSP_WORN_PLAYS", 40))`, `WORN_GROWTH_MB = int(os.environ.get("RSP_WORN_GROWTH_MB", 1500))`; `Slot.plays: int = 0`, `Slot.baseline_mb: float | None = None`, `Slot.memory_mb: float | None = None`, `Slot.wear_noticed: bool = False`; `Pool._check_wear(slot) -> str | None`.

- [ ] **Step 1: Measure on Windows, before any code.** Spike 5's numbers are Wine resident set size. On one pool Studio with a real place, record private bytes and working set (PowerShell `Get-Process`): once the acquire returns, then during Play and 15 s after each stop, for five Play cycles. Add the table to Spike 5 in `FINDINGS.md`.

  From it, confirm or change: that private bytes after stop rises steadily (if working set is the steadier signal, use it and rename `private_mb`), and that the first cycle's one-off growth plus a few cycles stays well under `RSP_WORN_GROWTH_MB`. **If the default would trip within the first 10 playtests, or growth per cycle is under 10 MB, stop and report to the user** with the table and a suggested default.

- [ ] **Step 2: Write the failing tests** in `test_memory.py`:

```python
class WornReason(unittest.TestCase):
    def test_fresh_slot_is_not_worn(self):
        self.assertIsNone(memory.worn_reason(0, 0.0, 40, 1500))

    def test_play_count_trips_it(self):
        self.assertEqual(memory.worn_reason(40, 100.0, 40, 1500), "40 playtests")
        self.assertIsNone(memory.worn_reason(39, 100.0, 40, 1500))

    def test_growth_trips_it(self):
        self.assertEqual(memory.worn_reason(3, 1638.4, 40, 1500), "grew 1.6 GB since launch")
        self.assertIsNone(memory.worn_reason(3, 1499.0, 40, 1500))

    def test_zero_turns_a_threshold_off(self):
        self.assertIsNone(memory.worn_reason(500, 100.0, 0, 1500))
        self.assertIsNone(memory.worn_reason(3, 9000.0, 40, 0))

    def test_unknown_growth_only_skips_the_growth_check(self):
        self.assertIsNone(memory.worn_reason(3, None, 40, 1500))
        self.assertEqual(memory.worn_reason(40, None, 40, 1500), "40 playtests")

    def test_shrinking_is_not_worn(self):
        self.assertIsNone(memory.worn_reason(3, -200.0, 40, 1500))


class PrivateMb(unittest.TestCase):
    def test_this_process_has_private_memory(self):
        self.assertGreater(memory.private_mb(os.getpid()), 1)

    def test_a_missing_process_is_none(self):
        self.assertIsNone(memory.private_mb(0x7FFFFFF0))
```

  And on `PoolFixture`, with `memory.private_mb` mocked:

```python
    def test_baseline_is_taken_at_the_end_of_a_fresh_acquire(self):
        # private_mb -> 2000 -> slot.baseline_mb == 2000 after acquire returns; a warm re-acquire keeps it

    def test_each_start_counts_one_play(self):
        # two start/stop cycles -> slot.plays == 2

    def test_a_worn_slot_gets_one_notice_and_is_never_closed_while_leased(self):
        # WORN_PLAYS patched to 2; two cycles -> take_notices(lease) has exactly one entry, kind "wear",
        # naming the playtest count and saying to release and acquire again;
        # a third cycle adds none; self.closed == []; the slot is still "ready" and leased

    def test_memory_is_not_judged_during_play(self):
        # private_mb -> baseline + 3000 while slot.playing; _check_wear(slot) returns None and adds no notice

    def test_a_worn_slot_is_closed_at_release_for_long_use(self):
        # after two cycles with WORN_PLAYS 2, release -> {"closed": True}; reason contains "long use"

    def test_an_unworn_slot_is_kept_warm(self):
        # one cycle, release -> {"closed": False}; self.closed == []

    def test_a_new_lease_can_be_noticed_again(self):
        # wear_noticed True on an idle slot; the next lease of it -> wear_noticed False

    def test_a_worn_idle_slot_is_not_reused(self):
        # an idle ready slot with plays == WORN_PLAYS; acquire(same place) -> reused False

    def test_a_dead_studio_does_not_break_stop_or_release(self):
        # private_mb -> None -> play stop and release raise nothing; slot.memory_mb unchanged

    def test_status_reports_wear(self):
        # status()["slots"][0] has plays, memory_mb and growth_mb
```

- [ ] **Step 3: Run them** → fail (`AttributeError: module 'memory' has no attribute 'worn_reason'`).

- [ ] **Step 4: Implement `private_mb` and `worn_reason`** in `memory.py`. Growth is shown in GB to one decimal (`growth_mb / 1024`).

- [ ] **Step 5: Implement in `rsp.py`.**
  - `acquire`: at the end of a **fresh** launch, after the sync and the browser stop, set `slot.baseline_mb = slot.memory_mb = memory.private_mb(slot.proc.pid)`. A warm reuse keeps its baseline.
  - `_start_stop`: on a successful start, `slot.plays += 1`. After a successful stop (and not during a restart), call `self._check_wear(slot)`.
  - `_check_wear(slot)`: return `None` at once if `slot.playing`, `slot.restarting`, `slot.group`, or `slot.proc` is missing or has exited. Read `private_mb`; if not `None`, store it in `slot.memory_mb`. Compute `worn_reason(slot.plays, growth, WORN_PLAYS, WORN_GROWTH_MB)` where `growth` is `memory_mb - baseline_mb`, or `None` if either is missing. If worn, the slot has a lease, and `not slot.wear_noticed`: set it and `slot.event("wear", f"this Studio has had long use ({reason}). For a fresh one, release and acquire again when convenient. Nothing will be closed while you hold it.", notice=True)`. Return the reason. It never closes anything and never raises.
  - `_take_slot`: where it clears `slot.notices`, also set `slot.wear_noticed = False`.
  - `_dirty_reason`: after the existing checks pass, return `f"long use ({reason})"` if `_check_wear(slot)` gives one.
  - `_pick`: skip a warm slot whose `worn_reason(...)` from its stored `plays` and memory is not `None`.
  - `status`: add `plays`, `memory_mb` (rounded) and `growth_mb` (rounded, or `None`), from the stored values.
  - **Not in the reaper.** Wear is only checked after a play stop and at release.

- [ ] **Step 6: Run all tests** → all pass.

- [ ] **Step 7: Live check.** Start the daemon with `RSP_WORN_PLAYS=2`. Two `play start`/`stop` cycles: the second `stop` prints the wear notice on its result; a third cycle prints none; the Studio stays open throughout. `status` shows `plays: 3` and memory figures. `release` reports `closed: true`. The next `acquire` of the same place reports `reused: false`. Then with the defaults: one cycle, release, re-acquire reports `reused: true`.

- [ ] **Step 8: Docs.** `SKILL.md`: one bullet explaining the wear notice (what it means, that nothing closes while the lease is held, that releasing and acquiring again gets a fresh Studio). `README.md`: add `RSP_WORN_PLAYS` / `RSP_WORN_GROWTH_MB` next to `RSP_LEASE_TTL` / `RSP_IDLE_TTL`, with the defaults and that `0` turns one off.

- [ ] **Step 9: Commit, push, open the PR** from `feat/wear-tracking`.
