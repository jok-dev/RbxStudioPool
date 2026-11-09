"""Offline tests for which place a multi-client test server runs as (`rsp.group_ids`).

    python -m unittest test_group_ids
"""

import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
