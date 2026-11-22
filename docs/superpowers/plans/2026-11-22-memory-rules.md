# Memory Rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Python pool cut Studio memory on its own: stop the embedded browser, offer a playtest with no client, and replace Studios that have grown from long use without ever closing one an agent holds.

**Architecture:** A new `memory.py` holds everything that doesn't need a pool: reading the process table, finding a Studio's browser processes, reading a process's memory, and the wear decision. `rsp.py` calls it from the places that already own those moments: `acquire` (browser), `play` / `_start_stop` (mode, playtest count), `_dirty_reason` (replace at release), the reaper and `status`.

**Tech Stack:** Python standard library only, Windows only (`ctypes` for process memory, PowerShell `Get-CimInstance` for the process table, as `child_processes` already does). Tests are `unittest`, run from `spikes/python-pool/`.

**Spec:** `docs/superpowers/specs/2026-11-22-memory-rules-design.md`

## Global Constraints

- Standard library only. No new dependencies.
- The pool never closes a Studio an agent holds because of memory. It only recommends, through `slot.event(..., notice=True)`.
- The flag is `--mode server-only`. The opt-out flag is `--keep-browser`.
- Defaults: `RSP_WORN_PLAYS=40`, `RSP_WORN_GROWTH_MB=1500`. `0` turns a threshold off.
- Play stays the default playtest mode. Existing commands behave as before when no new flag is passed.
- Nothing game-specific goes in the pool.
- One PR per task, each on its own branch from `main`: `feat/stop-browser`, `feat/server-only-play`, `feat/wear-tracking`.
- All work is in `spikes/python-pool/`. Run tests with `python -m unittest test_memory test_group_ids test_cloudplace` from that folder.
- A live check needs the daemon and a logged-in Studio on this Windows machine: `python rsp.py serve --cap 2`, then `python rsp.py acquire --place-file <any local .rbxl> --client plan`.

## Review Focus

1. **The browser can't be stopped** (no browser processes found, or `taskkill` fails): `acquire` still succeeds, and the lease gets a notice. Test in Task 1.
2. **The Studio exits between finding its pid and reading its memory:** `working_set_mb` returns `None` and wear tracking skips that reading. It never raises into `play stop` or the reaper. Test in Task 3.
3. **A lease is released while a server-only run is going:** the Studio is closed, not kept warm still running. Test in Task 2.
4. **`--restart-on-code` during a server-only run** restarts in server-only, not Play. Test in Task 2.
5. **A warm Studio whose browser is already stopped** goes to a lease without `--keep-browser`: the pool doesn't try to stop it again, and a `--keep-browser` lease never gets it. Test in Task 1.

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
  - `process_table() -> list[dict]`: every process as `{"pid": int, "parent": int, "image": str}`, from one `Get-CimInstance Win32_Process` call.
  - `browser_roots(table: list[dict], studio_pid: int) -> list[int]`: pids of `BROWSER_IMAGE` processes that descend from `studio_pid` and whose parent is not itself a `BROWSER_IMAGE` process. Image match is case-insensitive.
  - `stop_browser(studio_pid: int) -> int`: `taskkill /PID <pid> /T /F` on each root; returns how many roots it killed. Raises nothing: a failed kill counts as not killed.
- Produces, in `rsp.py`: `Slot.browser: str` (`"running"` or `"stopped"`), `acquire(..., keep_browser=False)`.

- [ ] **Step 1: Live check on Windows, before any code.** Acquire a Studio through the pool. With PowerShell, list `msedgewebview2.exe` processes descending from the slot's pid and sum their working set. Record the total. Kill them. Then verify and record each of: `rsp luau LEASE "return 1"` works, `rsp play LEASE start` and `stop` work, a Rojo sync works if a worktree is at hand, and after 5 minutes plus one Play cycle the browser processes have not come back. Also acquire with `--clients 1` and record whether the test server and client windows have browser processes.

  **Stop and report to the user if** Studio respawns the browser, or any of the checks fail. Don't build the rest.

- [ ] **Step 2: Record the result** as a "Windows check" bullet under Spike 5's Evidence in `FINDINGS.md`: the saving in GB, whether it respawned, and whether group windows have browsers.

- [ ] **Step 3: Write the failing tests** in `test_memory.py`:

```python
import unittest
import memory

TABLE = [
    {"pid": 10, "parent": 1, "image": "RobloxStudioBeta.exe"},
    {"pid": 11, "parent": 10, "image": "msedgewebview2.exe"},      # browser root
    {"pid": 12, "parent": 11, "image": "msedgewebview2.exe"},      # its renderer
    {"pid": 13, "parent": 10, "image": "RobloxCrashHandler.exe"},
    {"pid": 14, "parent": 13, "image": "MSEDGEWEBVIEW2.EXE"},      # root under another child
    {"pid": 20, "parent": 1, "image": "RobloxStudioBeta.exe"},
    {"pid": 21, "parent": 20, "image": "msedgewebview2.exe"},      # another Studio's browser
]


class BrowserRoots(unittest.TestCase):
    def test_finds_only_this_studios_top_browser_processes(self):
        self.assertEqual(sorted(memory.browser_roots(TABLE, 10)), [11, 14])

    def test_other_studio_is_untouched(self):
        self.assertEqual(memory.browser_roots(TABLE, 20), [21])

    def test_no_browser_is_empty(self):
        self.assertEqual(memory.browser_roots(TABLE, 999), [])

    def test_a_parent_cycle_does_not_hang(self):
        table = [{"pid": 1, "parent": 2, "image": "a.exe"}, {"pid": 2, "parent": 1, "image": "a.exe"}]
        self.assertEqual(memory.browser_roots(table, 1), [])
```

- [ ] **Step 4: Run them.** `python -m unittest test_memory` → fails with `ModuleNotFoundError: No module named 'memory'`.

- [ ] **Step 5: Implement `memory.py`** with the three functions above. `process_table` follows `child_processes` in `rsp.py` (PowerShell, `ConvertTo-Json -Compress`, a single object comes back as a dict), selecting `ProcessId,ParentProcessId,Name` with no filter.

- [ ] **Step 6: Run the tests.** `python -m unittest test_memory` → 4 pass.

- [ ] **Step 7: Write the failing pool tests** in `test_memory.py`, building a `Pool` the way `test_group_ids.py` does (launch and MCP mocked):

```python
    def test_browser_is_stopped_after_a_fresh_launch(self):
        # acquire(place_file=...) with memory.stop_browser mocked to return 1
        # -> stop_browser called once with the slot's pid; slot.browser == "stopped"

    def test_keep_browser_leaves_it_running(self):
        # acquire(..., keep_browser=True) -> stop_browser not called; slot.browser == "running"

    def test_keep_browser_lease_never_gets_a_stopped_warm_slot(self):
        # acquire + release (kept warm, browser stopped), then acquire(same place, keep_browser=True)
        # -> out["reused"] is False

    def test_warm_stopped_slot_is_not_stopped_twice(self):
        # acquire + release, acquire again without the flag -> reused True, stop_browser called once in total

    def test_warm_running_slot_is_stopped_for_a_plain_lease(self):
        # acquire(keep_browser=True) + release, acquire without the flag -> reused True, stop_browser called once

    def test_failed_stop_is_a_notice_not_an_error(self):
        # stop_browser returns 0 -> acquire succeeds; slot.browser == "running"; take_notices(lease) has one
        # entry of kind "browser"
```

- [ ] **Step 8: Run them** → fail (`acquire() got an unexpected keyword argument 'keep_browser'`).

- [ ] **Step 9: Implement in `rsp.py`.**
  - `Slot.browser: str = "running"`.
  - `acquire(..., keep_browser=False)` passes it to `_take_slot` and `_pick`. In `_pick`'s warm-slot loop, skip a slot when `keep_browser and s.browser == "stopped"`.
  - In `acquire`, after the launch-or-reuse and sync block succeeds: if `not keep_browser and slot.browser == "running"`, call `memory.stop_browser(slot.proc.pid)`. More than 0 → `slot.browser = "stopped"` and `slot.event("browser", "stopped the embedded browser (Toolbox, Assistant and start page are off until this Studio restarts)")`. 0 → `slot.event("browser", "no embedded browser processes found to stop", notice=True)`.
  - Add `"browser": slot.browser` to `acquire`'s result and to each slot in `status`.
  - Group leases (`clients > 0`): apply to the server and each client window only if Step 1 found browser processes there. If it didn't, leave `_acquire_group` alone and say so in the PR.
  - CLI: `a.add_argument("--keep-browser", action="store_true", help="leave the embedded browser running (needed for the Toolbox, Assistant and start page); costs about <Step 1 figure> GB")`, sent as `keep_browser` in the `/acquire` body.

- [ ] **Step 10: Run all tests.** `python -m unittest test_memory test_group_ids test_cloudplace` → all pass.

- [ ] **Step 11: Live check.** Restart the daemon. `acquire` without the flag: `status` shows `browser: stopped`, no `msedgewebview2.exe` under the slot's pid, `luau` and `play start/stop` work. `acquire --keep-browser --fresh`: browser processes present.

- [ ] **Step 12: Docs.** `SKILL.md`: one bullet saying the pool stops the embedded browser by default, what stops working, and that `--keep-browser` on `acquire` keeps it. `README.md`: add `python rsp.py acquire ... --keep-browser` to the command list with a one-line comment.

- [ ] **Step 13: Commit, push, open the PR** from `feat/stop-browser`.

---

### Task 2: `play start --mode server-only`

**Files:**
- Modify: `spikes/python-pool/rsp.py` (`Slot`, `Pool.play`, `_start_stop`, `_restart_play`, `_live_dm`, `_dirty_reason`, `_play_status`, `status`, the `/play` route, the `play` parser and request in `main`)
- Modify: `spikes/python-pool/test_memory.py`, `spikes/python-pool/SKILL.md`, `spikes/python-pool/README.md`, `FINDINGS.md`

**Interfaces:**
- Produces: `MODES = ("play", "server-only")`; `Slot.play_mode: str | None` (the mode running now, `None` when not playing); `play(lease_id, op, wait=None, timeout=120, mode="play")`; `_start_stop(slot, start, mode="play")`.

- [ ] **Step 1: Live check on Windows, before any code.** On an acquired Studio, run `rsp luau LEASE 'game:GetService("RunService"):Run() return "ok"'` and record:
  1. What `rsp call LEASE get_studio_state` prints as "Current Studio Mode" and "Available DataModels" during the run.
  2. Which `--dm` value `rsp luau` needs during the run to reach the running game (`Edit` worked under Wine).
  3. Whether `rsp logs LEASE --dm <that>` shows lines the game prints during the run.
  4. With a worktree synced: whether a file edit reaches the running game, and whether a `pool.toml` watch hook runs.
  5. That `RunService:Stop()` returns the Studio to Edit, and what the mode reads then.

  Add the answers as a bullet under Spike 5's Evidence in `FINDINGS.md`.

  The steps below assume the Wine result: the running game is reached through the `Edit` DataModel, and logs and Rojo keep working there with nothing extra to attach. **If 2, 3 or 4 differ, stop and report to the user** with what was found.

- [ ] **Step 2: Write the failing tests** in `test_memory.py`, with the MCP mocked so `luau` and `call` are recorded:

```python
    def test_server_only_start_runs_runservice_not_play(self):
        # play(lease, "start", mode="server-only") -> one luau call on Edit containing 'RunService"):Run()';
        # no start_stop_play call; slot.play_mode == "server-only"; result["play_mode"] == "server-only"

    def test_stop_ends_the_mode_that_is_running(self):
        # after a server-only start, play(lease, "stop") -> a luau call containing 'RunService"):Stop()';
        # no start_stop_play call; slot.play_mode is None

    def test_play_mode_is_unchanged_by_default(self):
        # play(lease, "start") -> start_stop_play called with is_start True; slot.play_mode == "play"

    def test_live_dm_is_edit_during_server_only(self):
        # slot.playing True, slot.play_mode "server-only" -> pool._live_dm(slot) == "Edit"

    def test_wait_regex_watches_the_edit_datamodel(self):
        # play(lease, "start", wait="ready", mode="server-only") with wait_log mocked
        # -> wait_log called with dm "Edit"

    def test_restart_on_code_keeps_the_mode(self):
        # slot.play_mode "server-only"; pool._restart_play(slot, changed) -> Stop() then Run(), no start_stop_play

    def test_multi_client_lease_rejects_server_only(self):
        # slot.group set -> play(lease, "start", mode="server-only") raises PoolError mentioning server-only

    def test_unknown_mode_is_rejected(self):
        # play(lease, "start", mode="fast") raises PoolError

    def test_release_during_server_only_closes_the_studio(self):
        # slot.play_mode "server-only"; release(lease) -> {"closed": True}
```

- [ ] **Step 3: Run them** → fail (`play() got an unexpected keyword argument 'mode'`).

- [ ] **Step 4: Implement.**
  - `_start_stop(slot, start, mode="play")`: for `"play"`, today's code. For `"server-only"`: start runs `game:GetService("RunService"):Run()` through `self.mcp.luau` on the slot's Studio (Edit), sets `slot.playing = True`; stop runs `:Stop()` and then `_wait_mode(slot, "Edit", 30)`. Skip the Server/Client log capture and `_connect_play_server` for server-only. Set `slot.play_mode` to the mode on start and `None` on stop. On stop, use `slot.play_mode` to choose how to stop, ignoring the argument.
  - `play`: validate `mode` against `MODES`; reject server-only on a group lease with `PoolError("server-only is for single-Studio leases; a multi-client test always has clients")`; pass `mode` to `_start_stop` on start; for `--wait`, use dm `"Edit"` when server-only.
  - `_restart_play`: capture `mode = slot.play_mode` before stopping and pass it to the start.
  - `_live_dm`: `"Server"` only when playing or restarting in `"play"` mode.
  - `_dirty_reason`: return `"server-only run in progress"` when `slot.play_mode == "server-only"`, before the `get_studio_state` call.
  - `_play_status` and `status`: add `"play_mode": slot.play_mode`.
  - `/play` route passes `body.get("mode", "play")`. CLI: `pl.add_argument("--mode", choices=MODES, default="play", help="server-only: run the server with no client (about 1 GB less; no LocalPlayer, character or UI)")`.

- [ ] **Step 5: Run all tests** → all pass.

- [ ] **Step 6: Live check.** `play LEASE start --mode server-only --wait <a line the place prints>` matches. `status` shows `play_mode: server-only`. `luau LEASE "return game:GetService('RunService'):IsRunning()"` returns true. `play LEASE stop` returns to Edit. Then `play LEASE start` and `stop` still work as Play. Record the memory added by each mode in the PR description.

- [ ] **Step 7: Docs.** `SKILL.md`: when to use `--mode server-only` (server logic only: no client, `LocalPlayer`, character, UI or input), that `luau` and `logs` use `--dm Edit` during it, and that Play is the default. `README.md`: add the command line.

- [ ] **Step 8: Commit, push, open the PR** from `feat/server-only-play`.

---

### Task 3: Wear tracking

**Files:**
- Modify: `spikes/python-pool/memory.py`, `spikes/python-pool/test_memory.py`
- Modify: `spikes/python-pool/rsp.py` (constants, `Slot`, `_launch`, `_take_slot`, `_pick`, `_start_stop`, `_dirty_reason`, `_reaper`, `status`)
- Modify: `spikes/python-pool/SKILL.md`, `spikes/python-pool/README.md`

**Interfaces:**
- Consumes: `_start_stop(slot, start, mode)` from Task 2. If Task 2 hasn't merged, branch from `main` and count plays in today's `_start_stop(slot, start)`.
- Produces, in `memory.py`:
  - `working_set_mb(pid: int) -> float | None`: the process's working set in MB through `ctypes` (`OpenProcess` with `PROCESS_QUERY_LIMITED_INFORMATION`, `K32GetProcessMemoryInfo`, `CloseHandle`). `None` if the process is gone or can't be opened.
  - `worn_reason(plays: int, growth_mb: float | None, max_plays: int, max_growth_mb: int) -> str | None`: `None` when not worn, else a short reason like `"40 playtests"` or `"grew 1.6 GB since launch"`. A threshold of `0` is off. `growth_mb` of `None` only skips the growth check.
- Produces, in `rsp.py`: `WORN_PLAYS = int(os.environ.get("RSP_WORN_PLAYS", 40))`, `WORN_GROWTH_MB = int(os.environ.get("RSP_WORN_GROWTH_MB", 1500))`; `Slot.plays: int = 0`, `Slot.baseline_mb: float | None = None`, `Slot.memory_mb: float | None = None`, `Slot.wear_noticed: bool = False`; `Pool._check_wear(slot) -> str | None`.

- [ ] **Step 1: Write the failing tests** in `test_memory.py`:

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


class WorkingSet(unittest.TestCase):
    def test_this_process_has_a_working_set(self):
        self.assertGreater(memory.working_set_mb(os.getpid()), 1)

    def test_a_missing_process_is_none(self):
        self.assertIsNone(memory.working_set_mb(0x7FFFFFF0))
```

  And pool tests, with `memory.working_set_mb` mocked:

```python
    def test_baseline_is_taken_when_the_slot_becomes_ready(self):
        # working_set_mb -> 2000; after acquire, slot.baseline_mb == 2000

    def test_each_start_counts_one_play(self):
        # two start/stop cycles -> slot.plays == 2

    def test_a_worn_slot_gets_one_notice_and_stays_open(self):
        # WORN_PLAYS patched to 2; two start/stop cycles -> take_notices(lease) has one entry of kind "wear"
        # whose detail names the playtest count and says to release and acquire again;
        # a third cycle adds no second notice; the slot is still in pool.slots with state "ready"

    def test_a_worn_slot_is_closed_at_release(self):
        # after the above, release(lease) -> {"closed": True}

    def test_an_unworn_slot_is_kept_warm(self):
        # one cycle, release -> {"closed": False}

    def test_a_new_lease_can_be_noticed_again(self):
        # slot.wear_noticed True on an idle slot; _take_slot leases it -> slot.wear_noticed is False

    def test_a_worn_idle_slot_is_not_reused(self):
        # an idle ready slot with plays == WORN_PLAYS; acquire(same place) -> reused False

    def test_a_dead_studio_does_not_break_stop(self):
        # working_set_mb -> None during play stop -> no exception; slot.memory_mb unchanged

    def test_status_reports_wear(self):
        # status()["slots"][0] has plays, memory_mb and growth_mb
```

- [ ] **Step 2: Run them** → fail (`AttributeError: module 'memory' has no attribute 'worn_reason'`).

- [ ] **Step 3: Implement `working_set_mb` and `worn_reason`** in `memory.py`. Growth is shown in GB to one decimal (`growth_mb / 1024`).

- [ ] **Step 4: Implement in `rsp.py`.**
  - `_launch`: after `slot.state = "ready"`, set `slot.baseline_mb = slot.memory_mb = memory.working_set_mb(slot.proc.pid)`.
  - `_start_stop`: on a successful start, `slot.plays += 1`. After a successful stop, call `self._check_wear(slot)`.
  - `_check_wear(slot)`: read `working_set_mb`; if not `None`, store it in `slot.memory_mb`. Compute `worn_reason(slot.plays, growth, WORN_PLAYS, WORN_GROWTH_MB)` where `growth` is `memory_mb - baseline_mb` or `None` if either is missing. If worn, the slot has a lease, and `not slot.wear_noticed`: set it and `slot.event("wear", f"this Studio has had long use ({reason}). For a fresh one, release and acquire again when convenient. Nothing will be closed while you hold it.", notice=True)`. Return the reason.
  - `_take_slot`: where it clears `slot.notices`, also set `slot.wear_noticed = False`.
  - `_dirty_reason`: after the existing checks pass, return `f"long use ({reason})"` if `_check_wear(slot)` gives one.
  - `_pick`: skip a warm slot whose `worn_reason(...)` from its stored `plays` and memory is not `None`.
  - `_reaper`: once a pass, call `_check_wear` on each leased, ready slot that isn't a group. Outside `self.cond`. It must not close anything.
  - `status`: add `plays`, `memory_mb` (rounded), `growth_mb` (rounded, or `None`).
  - Groups: no wear tracking. They're always closed at release.

- [ ] **Step 5: Run all tests** → all pass.

- [ ] **Step 6: Live check.** Start the daemon with `RSP_WORN_PLAYS=2`. Two `play start`/`stop` cycles: the second `stop` prints the wear notice on its result; a third cycle prints none. `status` shows `plays: 3` and memory figures. `release` reports `closed: true`. The next `acquire` of the same place reports `reused: false`.

- [ ] **Step 7: Docs.** `SKILL.md`: one bullet explaining the wear notice (what it means, that nothing closes while the lease is held, that releasing and acquiring again gets a fresh Studio). `README.md`: add `RSP_WORN_PLAYS` / `RSP_WORN_GROWTH_MB` next to `RSP_LEASE_TTL` / `RSP_IDLE_TTL`, with the defaults and that `0` turns one off.

- [ ] **Step 8: Commit, push, open the PR** from `feat/wear-tracking`.
