"""v1.14.11 - Tests : le PATH transmis au CLI.

Indice declencheur : macOS a reclame Rosetta a l'utilisateur. Rosetta, c'est
un binaire x86_64 lance sur un Mac Apple Silicon.

Deux defauts superposes dans _cli_env() :

1. les versions nvm etaient triees par NOM, pas par numero. "v9.11.2" passe
   avant "v22.1.0" en tri alphabetique, et "v8.17.0" avant "v18.20.0" :
   l'app choisissait le node le plus ANCIEN installe. Un node 8 ou 9 de
   2017, sur lequel le CLI Claude Code ne peut pas fonctionner -- et dont le
   build, sur une machine migree depuis un Intel, reclame Rosetta ;

2. ces chemins devines etaient PREPENDS : les suppositions de l'app
   battaient le PATH de l'utilisateur. Le CLI tournait donc sur un autre
   node que celui de son terminal, ce qui explique exactement "ca marche
   dans mon terminal, ca cale depuis l'app".
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402

VERSIONS = ["v22.1.0", "v20.11.0", "v18.20.0", "v8.17.0", "v9.11.2", "v16.14.0"]


class VersionSortTests(unittest.TestCase):
    def test_numeric_order_not_alphabetical(self):
        names = sorted(VERSIONS, key=lambda n: app._node_version_key(Path(n)),
                       reverse=True)
        self.assertEqual(names[0], "v22.1.0")
        self.assertEqual(names[-1], "v8.17.0")

    def test_alphabetical_sort_really_was_wrong(self):
        """Sans ce constat, le test ci-dessus ne prouverait rien."""
        self.assertEqual(sorted(VERSIONS, reverse=True)[0], "v9.11.2")

    def test_non_numeric_suffixes_do_not_crash(self):
        for name in ("v21.0.0-rc.1", "iojs-v3.3.1", "lts", "v20"):
            with self.subTest(name=name):
                self.assertIsInstance(app._node_version_key(Path(name)), tuple)


class PathPriorityTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        for v in VERSIONS:
            (self.home / ".nvm/versions/node" / v / "bin").mkdir(parents=True)
        self._home = app.HOME
        app.HOME = self.home
        self._shell = dict(app._SHELL_ENV_CACHE)
        app._SHELL_ENV_CACHE.update({"done": True, "env": {}})

    def tearDown(self):
        app.HOME = self._home
        app._SHELL_ENV_CACHE.clear()
        app._SHELL_ENV_CACHE.update(self._shell)

    def _path(self, current):
        with patch.object(app.os, "environ", {"PATH": current}):
            return app._cli_env()["PATH"].split(":")

    def test_user_path_keeps_priority(self):
        """Ce qui marche dans le terminal doit marcher dans l'app."""
        entries = self._path("/Users/x/.nvm/versions/node/v22.1.0/bin:/usr/bin")
        self.assertEqual(entries[0], "/Users/x/.nvm/versions/node/v22.1.0/bin")

    def test_guessed_paths_come_after(self):
        entries = self._path("/usr/bin")
        self.assertEqual(entries[0], "/usr/bin")
        self.assertIn("/opt/homebrew/bin", entries)
        self.assertGreater(entries.index("/opt/homebrew/bin"),
                           entries.index("/usr/bin"))

    def test_newest_node_wins_among_the_guesses(self):
        entries = self._path("/usr/bin")
        nvm = [e for e in entries if ".nvm/versions/node" in e]
        self.assertTrue(nvm[0].endswith("v22.1.0/bin"), nvm[:3])

    def test_ancient_node_is_not_first(self):
        """Le defaut exact : un node 9 en tete du PATH du CLI."""
        entries = self._path("/usr/bin")
        nvm = [e for e in entries if ".nvm/versions/node" in e]
        self.assertFalse(nvm[0].endswith("v9.11.2/bin"))
        self.assertFalse(nvm[0].endswith("v8.17.0/bin"))

    def test_empty_path_still_gets_the_fallbacks(self):
        """Le cas launchd, seule raison d'etre de ces chemins."""
        entries = [e for e in self._path("") if e]
        self.assertIn("/opt/homebrew/bin", entries)

    def test_no_duplicate_entries(self):
        entries = self._path("/opt/homebrew/bin:/usr/bin")
        self.assertEqual(entries.count("/opt/homebrew/bin"), 1)

    def test_missing_nvm_dir_is_survivable(self):
        app.HOME = Path(tempfile.mkdtemp())
        self.assertTrue(self._path("/usr/bin"))


if __name__ == "__main__":
    unittest.main()
