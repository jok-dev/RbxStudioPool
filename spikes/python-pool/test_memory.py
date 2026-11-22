"""Offline tests for wear tracking: reading a process's memory, the wear decision, and what the pool does with it.

    python -m unittest test_memory
"""

import os
import unittest

import memory
import rsp
from test_play import PoolFixture


class WornReason(unittest.TestCase):
    def test_fresh_slot_is_not_worn(self):
        self.assertIsNone(memory.worn_reason(0, 0.0, 40, 1500))

    def test_play_count_trips_it(self):
        self.assertEqual(memory.worn_reason(40, 100.0, 40, 1500), "40 playtests")
        self.assertIsNone(memory.worn_reason(39, 100.0, 40, 1500))

    def test_growth_trips_it(self):
        self.assertEqual(memory.worn_reason(3, 1638.4, 40, 1500), "grew 1.6 GB in use")
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


class Wear(PoolFixture):
    """`memory.private_mb` is replaced by `self.mb`, so each test sets what the Studio's memory reads as."""

    def setUp(self):
        super().setUp()
        self.mb = 2000.0
        self.patch(memory, "private_mb", side_effect=lambda pid: self.mb)
        self.patch(rsp, "WORN_PLAYS", new=2)
        self.patch(rsp, "WORN_GROWTH_MB", new=1500)

    def cycle(self, lease, mode="play"):
        self.pool.play(lease, "start", mode=mode)
        return self.pool.play(lease, "stop")

    def wear_notices(self, lease):
        return [n for n in self.pool.take_notices(lease) if n["kind"] == "wear"]

    def test_baseline_is_taken_at_the_end_of_a_fresh_acquire(self):
        out = self.acquire()
        self.assertEqual(self.slot(out).baseline_mb, 2000.0)
        self.pool.release(out["lease"])
        self.mb = 2300.0
        again = self.acquire()
        self.assertTrue(again["reused"])
        self.assertEqual(self.slot(again).baseline_mb, 2000.0)

    def test_each_start_counts_one_playtest_in_either_mode(self):
        out = self.acquire()
        self.patch(rsp, "WORN_PLAYS", new=0)
        self.cycle(out["lease"])
        self.cycle(out["lease"], mode="server-only")
        self.assertEqual(self.slot(out).plays, 2)

    def test_a_worn_slot_gets_one_notice_and_is_never_closed_while_leased(self):
        out = self.acquire()
        lease, slot = out["lease"], self.slot(out)
        self.cycle(lease)
        self.assertEqual(self.wear_notices(lease), [])
        self.cycle(lease)
        notices = self.wear_notices(lease)
        self.assertEqual(len(notices), 1)
        self.assertIn("2 playtests", notices[0]["detail"])
        self.assertIn("release and acquire again", notices[0]["detail"])
        self.cycle(lease)
        self.assertEqual(self.wear_notices(lease), [])
        self.assertEqual(self.closed, [])
        self.assertEqual((slot.state, slot.lease), ("ready", lease))

    def test_growth_alone_trips_the_notice(self):
        out = self.acquire()
        self.patch(rsp, "WORN_PLAYS", new=0)
        self.cycle(out["lease"])
        self.mb = 3600.0
        self.cycle(out["lease"])
        notices = self.wear_notices(out["lease"])
        self.assertEqual(len(notices), 1)
        self.assertIn("grew 1.6 GB in use", notices[0]["detail"])

    def test_what_the_first_playtest_leaves_behind_is_not_growth(self):
        # the first playtest costs a one-off that scales with the place; only what later ones add is wear
        out = self.acquire()
        self.patch(rsp, "WORN_PLAYS", new=0)
        self.mb = 3600.0
        self.cycle(out["lease"])
        self.assertEqual(self.slot(out).baseline_mb, 3600.0)
        self.mb = 3700.0
        self.cycle(out["lease"])
        self.assertEqual(self.slot(out).baseline_mb, 3600.0)
        self.assertEqual(self.wear_notices(out["lease"]), [])
        self.assertEqual(self.pool.release(out["lease"]), {"closed": False})

    def test_restarts_on_code_alone_still_get_the_notice(self):
        out = self.acquire()
        slot = self.slot(out)
        self.pool.play(out["lease"], "start")
        self.pool._restart_play(slot, {("code", "a.luau")})
        notices = self.wear_notices(out["lease"])
        self.assertEqual(len(notices), 1)
        self.assertIn("2 playtests", notices[0]["detail"])
        self.assertEqual(self.closed, [])

    def test_memory_is_not_judged_during_play(self):
        out = self.acquire()
        slot = self.slot(out)
        self.pool.play(out["lease"], "start")
        self.mb = 5000.0
        self.assertIsNone(self.pool._check_wear(slot))
        self.assertEqual(slot.memory_mb, 2000.0)
        self.assertEqual(self.wear_notices(out["lease"]), [])

    def test_a_restart_on_code_is_not_a_moment_to_judge(self):
        out = self.acquire()
        slot = self.slot(out)
        self.pool.play(out["lease"], "start")
        self.mb = 5000.0
        self.patch(rsp, "WORN_PLAYS", new=0)
        self.pool._restart_play(slot, {("code", "a.luau")})
        self.assertEqual(slot.plays, 2)
        self.assertEqual((slot.memory_mb, slot.baseline_mb), (2000.0, 2000.0))
        self.assertEqual(self.wear_notices(out["lease"]), [])

    def test_a_worn_slot_is_closed_at_release_for_long_use(self):
        out = self.acquire()
        self.cycle(out["lease"])
        self.cycle(out["lease"])
        self.assertEqual(self.pool.release(out["lease"]), {"closed": True})
        self.assertEqual(len(self.closed), 1)
        self.assertIn("long use (2 playtests)", self.closed[0][1])

    def test_growth_seen_only_at_release_closes_it(self):
        out = self.acquire()
        self.mb = 3600.0
        self.assertEqual(self.pool.release(out["lease"]), {"closed": True})
        self.assertIn("long use (grew 1.6 GB in use)", self.closed[0][1])

    def test_an_unworn_slot_is_kept_warm(self):
        out = self.acquire()
        self.cycle(out["lease"])
        self.assertEqual(self.pool.release(out["lease"]), {"closed": False})
        self.assertEqual(self.closed, [])

    def test_a_new_lease_can_be_noticed_again(self):
        out = self.acquire()
        self.patch(rsp, "WORN_PLAYS", new=0)
        self.slot(out).wear_noticed = True
        self.pool.release(out["lease"])
        again = self.acquire()
        self.assertTrue(again["reused"])
        self.assertFalse(self.slot(again).wear_noticed)

    def test_a_worn_idle_slot_is_not_reused(self):
        out = self.acquire()
        self.patch(rsp, "WORN_PLAYS", new=0)
        self.pool.release(out["lease"])
        self.assertEqual(self.closed, [])  # it is idle and warm
        self.slot(out).plays = 2
        self.patch(rsp, "WORN_PLAYS", new=2)
        again = self.acquire()
        self.assertFalse(again["reused"])
        self.assertNotEqual(again["slot"], out["slot"])

    def test_control_an_unworn_idle_slot_is_reused(self):
        out = self.acquire()
        self.pool.release(out["lease"])
        self.assertTrue(self.acquire()["reused"])

    def test_a_studio_whose_memory_cannot_be_read_does_not_break_stop_or_release(self):
        out = self.acquire()
        slot = self.slot(out)
        self.mb = None
        self.cycle(out["lease"])
        self.assertEqual(slot.memory_mb, 2000.0)
        self.assertEqual(self.pool.release(out["lease"]), {"closed": False})

    def test_a_failing_memory_read_does_not_break_stop(self):
        out = self.acquire()
        self.pool.play(out["lease"], "start")
        memory.private_mb.side_effect = RuntimeError("boom")
        self.assertEqual(self.pool.play(out["lease"], "stop")["play_mode"], None)

    def test_a_multi_client_group_is_never_judged(self):
        out = self.acquire()
        slot = self.slot(out)
        slot.plays, slot.group = 99, object()
        self.assertIsNone(self.pool._check_wear(slot))

    def test_status_reports_wear(self):
        out = self.acquire()
        self.patch(rsp, "WORN_PLAYS", new=0)
        self.cycle(out["lease"])
        self.mb = 2300.0
        self.cycle(out["lease"])
        row = self.pool.status()["slots"][0]
        self.assertEqual((row["plays"], row["memory_mb"], row["growth_mb"]), (2, 2300, 300))


if __name__ == "__main__":
    unittest.main()
