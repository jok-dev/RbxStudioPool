"""Offline tests for playtest modes, on a `Pool` whose Studio is faked (`PoolFixture`).

    python -m unittest test_play
"""

import itertools
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import rsp

EDIT_STATE = "- Current Studio Mode: Edit\n- Available DataModels: Edit"
PLAY_STATE = "- Current Studio Mode: Play\n- Available DataModels: Client, Server"


class FakeMcp:
    """Stands in for the Studio MCP: records every call, and changes mode the way Studio does."""

    def __init__(self):
        self.calls = []  # (tool, args)
        self.state_text = EDIT_STATE
        self.run_mode_starts = True  # False: ExecuteRunModeAsync does nothing, as when Studio refuses it
        self.luau_result = "true"

    def call(self, tool, args, timeout=120):
        self.calls.append((tool, dict(args)))
        if tool == "start_stop_play":
            self.state_text = PLAY_STATE if args["is_start"] else EDIT_STATE
        return {}, self.state_text

    def luau(self, studio_id, code, timeout=60, datamodel="Edit"):
        self.calls.append(("execute_luau", {"studio_id": studio_id, "code": code, "datamodel_type": datamodel}))
        if "ExecuteRunModeAsync" in code and self.run_mode_starts:
            self.state_text = PLAY_STATE
        return self.luau_result

    def list_studios(self):
        return []

    def named(self, tool):
        return [args for name, args in self.calls if name == tool]

    def play_calls(self):
        """What started and stopped playtests, in order: "play", "server-only" or "stop"."""
        out = []
        for tool, args in self.calls:
            if tool == "start_stop_play":
                out.append("play" if args["is_start"] else "stop")
            elif tool == "execute_luau" and "ExecuteRunModeAsync" in args["code"]:
                out.append("server-only")
        return out


class PoolFixture(unittest.TestCase):
    """A Pool without its constructor (no Studio MCP process, hidden desktop, reaper thread or leftover cleanup),
    whose launch marks the slot ready at once. Nothing here sleeps, starts a process or writes pool state."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.place_file = Path(tmp.name) / "map.rbxl"
        self.place_file.write_bytes(b"<roblox!")

        self.pool = rsp.Pool.__new__(rsp.Pool)
        self.pool.cap = 8
        self.pool.mcp = self.mcp = FakeMcp()
        self.pool.slots, self.pool.leases, self.pool.queue = {}, {}, []
        self.pool.cond = threading.Condition()
        self.pool.tickets = itertools.count()
        self.pool.launch_lock = threading.Lock()
        self.pool.desktop = None
        self.pool.shown_lease = None

        self.closed = []  # (slot number, why)
        self.log_queries = []  # the DataModel of each log query
        self.patch(self.pool, "_launch", side_effect=self.fake_launch)
        self.patch(self.pool, "_close", side_effect=lambda slot, why: self.closed.append((slot.n, why)))
        self.patch(self.pool, "_query_logs", side_effect=self.fake_query_logs)
        self.patch(self.pool, "_save_pids")
        self.patch(self.pool, "_copy_place")
        self.patch(rsp.time, "sleep")

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def fake_launch(self, slot, allow_team_create):
        slot.proc = mock.Mock(pid=1000 + slot.n)
        slot.proc.poll.return_value = None
        slot.studio_id = f"studio{slot.n}"
        slot.state = "ready"

    def fake_query_logs(self, slot, dm, query, retry_for=0, studio_id=None):
        self.log_queries.append(dm)
        return {"id": "capture", "cursor": 0, "dropped": 0, "entries": []}

    def acquire(self, **kwargs):
        return self.pool.acquire(place_file=str(self.place_file), client="test", wait=1, **kwargs)

    def slot(self, out):
        return self.pool.slots[out["slot"]]


class PlayModes(PoolFixture):
    def setUp(self):
        super().setUp()
        self.patch(rsp, "RUN_MODE_START_TIMEOUT", new=0)
        self.out = self.acquire()
        self.lease = self.out["lease"]
        self.the_slot = self.slot(self.out)

    def test_control_a_plain_release_keeps_the_studio_warm(self):
        self.assertEqual(self.pool.release(self.lease), {"closed": False})
        self.assertEqual(self.closed, [])

    def test_play_is_the_default_mode(self):
        out = self.pool.play(self.lease, "start")
        self.assertEqual(self.mcp.play_calls(), ["play"])
        self.assertEqual(self.the_slot.play_mode, "play")
        self.assertEqual(out["play_mode"], "play")

    def test_server_only_starts_run_mode_from_the_edit_datamodel(self):
        out = self.pool.play(self.lease, "start", mode="server-only")
        self.assertEqual(self.mcp.play_calls(), ["server-only"])
        start = [c for c in self.mcp.named("execute_luau") if "ExecuteRunModeAsync" in c["code"]][0]
        self.assertEqual(start["datamodel_type"], "Edit")
        self.assertEqual(self.the_slot.play_mode, "server-only")
        self.assertTrue(self.the_slot.playing)
        self.assertEqual(out["play_mode"], "server-only")
        self.assertEqual(out["mode"], "Play")

    def test_server_only_start_fails_if_the_run_did_not_begin(self):
        self.mcp.run_mode_starts = False
        with self.assertRaisesRegex(rsp.PoolError, "Play mode"):
            self.pool.play(self.lease, "start", mode="server-only")
        self.assertFalse(self.the_slot.playing)
        self.assertIsNone(self.the_slot.play_mode)

    def test_server_only_captures_server_logs_and_never_asks_a_client(self):
        self.pool.play(self.lease, "start", mode="server-only")
        self.assertEqual(self.log_queries, ["Server"])

    def test_play_captures_server_and_client_logs(self):
        self.pool.play(self.lease, "start")
        self.assertEqual(self.log_queries, ["Server", "Client"])

    def test_stop_ends_a_server_only_run(self):
        self.pool.play(self.lease, "start", mode="server-only")
        out = self.pool.play(self.lease, "stop")
        self.assertEqual(self.mcp.play_calls(), ["server-only", "stop"])
        self.assertIsNone(self.the_slot.play_mode)
        self.assertFalse(self.the_slot.playing)
        self.assertIsNone(out["play_mode"])

    def test_start_while_the_other_mode_runs_is_rejected(self):
        for first, second in (("server-only", "play"), ("play", "server-only")):
            with self.subTest(first=first):
                self.pool.play(self.lease, "start", mode=first)
                before = self.mcp.play_calls()
                with self.assertRaisesRegex(rsp.PoolError, "stop first"):
                    self.pool.play(self.lease, "start", mode=second)
                self.assertEqual(self.mcp.play_calls(), before)
                self.assertEqual(self.the_slot.play_mode, first)
                self.pool.play(self.lease, "stop")

    def test_a_second_server_only_start_is_rejected(self):
        self.pool.play(self.lease, "start", mode="server-only")
        with self.assertRaisesRegex(rsp.PoolError, "stop first"):
            self.pool.play(self.lease, "start", mode="server-only")
        self.assertEqual(self.mcp.play_calls(), ["server-only"])

    def test_wait_looks_for_the_line_in_the_server(self):
        wait_log = self.patch(self.pool, "wait_log", return_value={"msg": "ready"})
        out = self.pool.play(self.lease, "start", wait="ready", timeout=5, mode="server-only")
        wait_log.assert_called_once_with(self.lease, "ready", "Server", 5)
        self.assertEqual(out["matched"], {"msg": "ready"})

    def test_restart_on_code_keeps_the_mode(self):
        self.pool.play(self.lease, "start", mode="server-only")
        self.pool._restart_play(self.the_slot, {("code", "a.luau")})
        self.assertEqual(self.mcp.play_calls(), ["server-only", "stop", "server-only"])
        self.assertEqual(self.the_slot.play_mode, "server-only")
        self.assertTrue(self.the_slot.playing)
        self.assertFalse(self.the_slot.restarting)

    def test_restart_does_nothing_if_the_playtest_was_stopped_meanwhile(self):
        self.pool.play(self.lease, "start", mode="server-only")
        self.pool.play(self.lease, "stop")
        self.pool._restart_play(self.the_slot, {("code", "a.luau")})
        self.assertEqual(self.mcp.play_calls(), ["server-only", "stop"])
        self.assertIsNone(self.the_slot.play_mode)

    def test_a_stop_that_races_a_restart_still_clears_the_mode(self):
        # the watcher sets `restarting` before it gets the play lock; the agent's stop can hold the lock then
        self.pool.play(self.lease, "start", mode="server-only")
        self.the_slot.restarting = True
        self.pool.play(self.lease, "stop")
        self.assertIsNone(self.the_slot.play_mode)
        self.pool._restart_play(self.the_slot, {("code", "a.luau")})
        self.assertEqual(self.mcp.play_calls(), ["server-only", "stop"])
        self.assertIsNone(self.the_slot.play_mode)
        self.pool.play(self.lease, "start")
        self.assertEqual(self.the_slot.play_mode, "play")

    def test_a_failed_restart_leaves_no_mode_behind(self):
        self.pool.play(self.lease, "start", mode="server-only")
        self.mcp.run_mode_starts = False
        self.pool._restart_play(self.the_slot, {("code", "a.luau")})
        self.assertFalse(self.the_slot.playing)
        self.assertIsNone(self.the_slot.play_mode)
        self.assertEqual([n["kind"] for n in self.pool.take_notices(self.lease)], ["restart"])

    def test_a_multi_client_lease_rejects_server_only(self):
        self.the_slot.group = mock.Mock()
        with self.assertRaisesRegex(rsp.PoolError, "server-only is for single-Studio leases"):
            self.pool.play(self.lease, "start", mode="server-only")

    def test_team_create_rejects_server_only(self):
        self.the_slot.team_create = True
        with self.assertRaisesRegex(rsp.PoolError, "Team Create"):
            self.pool.play(self.lease, "start", mode="server-only")
        self.assertEqual(self.mcp.play_calls(), [])

    def test_an_unknown_mode_is_rejected(self):
        with self.assertRaisesRegex(rsp.PoolError, "mode must be one of"):
            self.pool.play(self.lease, "start", mode="fast")
        self.assertEqual(self.mcp.play_calls(), [])

    def test_release_during_a_server_only_run_closes_the_studio(self):
        self.pool.play(self.lease, "start", mode="server-only")
        self.assertEqual(self.pool.release(self.lease), {"closed": True})
        self.assertEqual(len(self.closed), 1)
        self.assertIn("not in edit mode", self.closed[0][1])

    def test_release_after_a_stopped_server_only_run_keeps_it_warm(self):
        self.pool.play(self.lease, "start", mode="server-only")
        self.pool.play(self.lease, "stop")
        self.assertEqual(self.pool.release(self.lease), {"closed": False})
        self.assertEqual(self.closed, [])

    def test_status_reports_the_mode(self):
        self.assertIsNone(self.pool.status()["slots"][0]["play_mode"])
        self.pool.play(self.lease, "start", mode="server-only")
        self.assertEqual(self.pool.status()["slots"][0]["play_mode"], "server-only")
        self.assertEqual(self.pool.play(self.lease, "status")["play_mode"], "server-only")


if __name__ == "__main__":
    unittest.main()
