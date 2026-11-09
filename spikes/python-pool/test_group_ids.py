"""Offline tests for which place a multi-client test server runs as: `rsp.group_ids` on its own, and through
`Pool.acquire(clients=N)` with the launch mocked.

    python -m unittest test_group_ids
"""

import itertools
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import repo
import rsp


class GroupIds(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / repo.DEFAULT_PROJECT).write_text("{}", encoding="utf-8")

    def spec(self, toml=None):
        if toml is not None:
            (self.root / repo.SPEC_DIR).mkdir(exist_ok=True)
            (self.root / repo.SPEC_DIR / repo.SPEC_FILE).write_text(toml, encoding="utf-8")
        return repo.load(self.root)

    def test_pool_toml_ids_are_used_when_the_caller_gives_none(self):
        spec = self.spec("place = 92235304689855\nuniverse = 10588682524\n")
        self.assertEqual(rsp.group_ids(None, None, spec), ("92235304689855", "10588682524"))

    def test_caller_ids_win_over_pool_toml(self):
        spec = self.spec("place = 111\nuniverse = 222\n")
        self.assertEqual(rsp.group_ids(333, 444, spec), ("333", "444"))

    def test_no_worktree_is_place_zero(self):
        self.assertEqual(rsp.group_ids(None, None, None), ("0", "0"))

    def test_worktree_without_ids_is_place_zero(self):
        self.assertEqual(rsp.group_ids(None, None, self.spec()), ("0", "0"))
        self.assertEqual(rsp.group_ids(None, None, self.spec('project = "default.project.json"\n')), ("0", "0"))

    def test_half_a_caller_pair_is_an_error(self):
        spec = self.spec("place = 111\nuniverse = 222\n")
        for place, universe in ((333, None), (None, 444)):
            with self.assertRaises(rsp.PoolError):
                rsp.group_ids(place, universe, spec)

    def test_half_a_pool_toml_pair_is_an_error_naming_the_missing_key(self):
        with self.assertRaisesRegex(rsp.PoolError, r"pool\.toml.*`universe`"):
            rsp.group_ids(None, None, self.spec("place = 111\n"))
        with self.assertRaisesRegex(rsp.PoolError, r"pool\.toml.*`place`"):
            rsp.group_ids(None, None, self.spec("universe = 222\n"))

    def test_caller_ids_make_up_for_half_a_pool_toml_pair(self):
        self.assertEqual(rsp.group_ids(333, 444, self.spec("place = 111\n")), ("333", "444"))


class AcquireGroup(unittest.TestCase):
    """`Pool.acquire(clients=N)` with the Studio launch and `rojo build` mocked: what reaches the `Group` the
    server is launched from, and what the acquire result says."""

    IDS = "place = 92235304689855\nuniverse = 10588682524\n"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "worktree"
        self.root.mkdir()
        (self.root / repo.DEFAULT_PROJECT).write_text("{}", encoding="utf-8")
        self.place_file = Path(tmp.name) / "map.rbxl"
        self.place_file.write_bytes(b"<roblox!")
        self.built = Path(tmp.name) / "built.rbxl"

        # a Pool without its constructor: no Studio MCP, hidden desktop, reaper thread or leftover cleanup
        self.pool = rsp.Pool.__new__(rsp.Pool)
        self.pool.cap = 8
        self.pool.slots, self.pool.leases, self.pool.queue = {}, {}, []
        self.pool.cond = threading.Condition()
        self.pool.tickets = itertools.count()
        for name, value in (("_start_group", None), ("_build_template", self.built)):
            patch = mock.patch.object(self.pool, name, return_value=value)
            setattr(self, name.strip("_"), patch.start())
            self.addCleanup(patch.stop)

    def pool_toml(self, text):
        (self.root / repo.SPEC_DIR).mkdir(exist_ok=True)
        (self.root / repo.SPEC_DIR / repo.SPEC_FILE).write_text(text, encoding="utf-8")

    def acquire(self, **kwargs):
        out = self.pool.acquire(clients=1, client="test", wait=1, **kwargs)
        slot = self.start_group.call_args.args[0]  # the slot the server would be launched from
        return out, slot.group

    def test_worktree_alone_runs_as_the_pool_toml_place(self):
        self.pool_toml(self.IDS)
        out, group = self.acquire(worktree=str(self.root))
        self.assertEqual((group.place_id, group.universe_id), ("92235304689855", "10588682524"))
        self.assertEqual(group.template, self.built)
        self.assertIn("92235304689855", out["warning"])
        self.assertIn("live data", out["warning"])

    def test_worktree_with_a_place_file_runs_as_the_pool_toml_place(self):
        self.pool_toml(self.IDS)
        out, group = self.acquire(worktree=str(self.root), place_file=str(self.place_file))
        self.assertEqual((group.place_id, group.universe_id), ("92235304689855", "10588682524"))
        self.assertEqual(group.template, self.place_file.resolve())
        self.build_template.assert_not_called()
        self.assertIn("92235304689855", out["warning"])

    def test_caller_ids_win_and_warn(self):
        self.pool_toml(self.IDS)
        out, group = self.acquire(worktree=str(self.root), place_file=str(self.place_file), place=333, universe=444)
        self.assertEqual((group.place_id, group.universe_id), ("333", "444"))
        self.assertIn("333", out["warning"])

    def test_no_ids_anywhere_is_place_zero_without_a_warning(self):
        for kwargs in ({"place_file": str(self.place_file)}, {"worktree": str(self.root)}):
            out, group = self.acquire(**kwargs)
            self.assertEqual((group.place_id, group.universe_id), ("0", "0"))
            self.assertNotIn("warning", out)

    def test_half_a_pool_toml_pair_fails_before_anything_is_launched(self):
        self.pool_toml("place = 111\n")
        with self.assertRaisesRegex(rsp.PoolError, "`universe`"):
            self.pool.acquire(clients=1, client="test", wait=1, worktree=str(self.root))
        self.start_group.assert_not_called()
        self.assertEqual(self.pool.slots, {})


if __name__ == "__main__":
    unittest.main()
