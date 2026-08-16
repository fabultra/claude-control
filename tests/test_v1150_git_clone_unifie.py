"""v1.15.0 - Tests : un seul chemin `git clone` pour les trois importeurs.

Constat d'audit : add_plugin_from_git, import_mcp_git et import_skill_git
portaient chacun leur copie du meme `git clone --depth 1` avec des timeouts
divergents (120/90/90 s) et des gestions d'erreurs differentes --
import_skill_git attrapait meme le timeout dans un `except Exception` au
message inexploitable ("Erreur git : Command ... timed out"). Desormais :
_git_url_ok + _git_repo_name + _git_clone_shallow, memes messages, meme
timeout, et la garde _safe_component de v1.14.12 s'applique enfin AUSSI a
add_plugin_from_git (qui se contentait de `"/" in name`).
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


class HelperTests(unittest.TestCase):
    def test_url_schemes(self):
        for good in ("https://github.com/x/y.git", "http://x/y",
                     "git@github.com:x/y.git", "  https://x/y  "):
            self.assertTrue(app._git_url_ok(good), good)
        for bad in ("", None, "ftp://x/y", "file:///etc", "github.com/x/y"):
            self.assertFalse(app._git_url_ok(bad), bad)

    def test_repo_name_derivation(self):
        self.assertEqual(app._git_repo_name("https://g.com/a/mon-repo.git"),
                         "mon-repo")
        self.assertEqual(app._git_repo_name("https://g.com/a/mon-repo/"),
                         "mon-repo")
        # Les vecteurs v1.14.12 restent fermes dans le helper unique.
        self.assertIsNone(app._git_repo_name("https://x.example/.."))
        self.assertIsNone(app._git_repo_name("https://x.example/.git"))

    def test_clone_success_and_failure_messages(self):
        with patch.object(app.subprocess, "run",
                          lambda *a, **kw: type("R", (), {
                              "returncode": 0, "stdout": "", "stderr": ""})()):
            self.assertEqual(app._git_clone_shallow("https://x/y", "/tmp/t"),
                             (True, ""))
        with patch.object(app.subprocess, "run",
                          lambda *a, **kw: type("R", (), {
                              "returncode": 128, "stdout": "",
                              "stderr": "fatal: not found"})()):
            ok, err = app._git_clone_shallow("https://x/y", "/tmp/t")
        self.assertFalse(ok)
        self.assertIn("fatal: not found", err)

    def test_timeout_and_missing_git_are_homogeneous(self):
        def boom(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

        with patch.object(app.subprocess, "run", boom):
            ok, err = app._git_clone_shallow("https://x/y", "/tmp/t")
        self.assertFalse(ok)
        self.assertIn(f"Timeout git clone ({app._GIT_CLONE_TIMEOUT}s)", err)

        def missing(cmd, **kw):
            raise FileNotFoundError("git")

        with patch.object(app.subprocess, "run", missing):
            ok, err = app._git_clone_shallow("https://x/y", "/tmp/t")
        self.assertFalse(ok)
        self.assertIn("git n'est pas install", err)

    def test_bad_scheme_never_reaches_subprocess(self):
        def forbidden(*a, **kw):
            raise AssertionError("subprocess sur URL invalide")

        with patch.object(app.subprocess, "run", forbidden):
            ok, _ = app._git_clone_shallow("ftp://x/y", "/tmp/t")
        self.assertFalse(ok)


class ImportersShareThePathTests(unittest.TestCase):
    """Chaque importeur passe par le helper -- verifie en le remplacant."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_skill_git_timeout_message_is_now_usable(self):
        """Avant : 'Erreur git : Command ... timed out after 90 seconds'."""
        def hang(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

        with patch.object(app, "SKILLS_DIR", self.tmp / "skills"), \
                patch.object(app.subprocess, "run", hang):
            ok, msg = app.import_skill_git("https://x/nouveau-skill")
        self.assertFalse(ok)
        self.assertIn("Timeout git clone", msg)

    def test_all_three_importers_call_the_single_helper(self):
        calls = []

        def fake_clone(url, target):
            calls.append(str(target))
            return False, "stop ici"

        with patch.object(app, "_git_clone_shallow", fake_clone), \
                patch.object(app, "SKILLS_DIR", self.tmp / "skills"), \
                patch.object(app, "IMPORTED_REPOS_DIR", self.tmp / "mcps"), \
                patch.object(app, "HOME", self.tmp):
            app.import_skill_git("https://x/a")
            app.import_mcp_git("https://x/b")
            app.add_plugin_from_git("https://x/c")
        self.assertEqual(len(calls), 3)
        self.assertTrue(calls[0].endswith("skills/a"))
        self.assertTrue(calls[1].endswith("mcps/b"))
        self.assertIn(".claude/plugins/cache/manual/c", calls[2])

    def test_plugin_importer_gains_the_safe_component_guard(self):
        """`..` etait accepte par l'ancien test `'/' in name` et ne tenait
        qu'a un cache_dir.exists() accidentel."""
        def forbidden(*a, **kw):
            raise AssertionError("git ne doit pas etre invoque sur '..'")

        with patch.object(app, "HOME", self.tmp), \
                patch.object(app.subprocess, "run", forbidden):
            ok, _ = app.add_plugin_from_git("https://x.example/..")
        self.assertFalse(ok)

    def test_no_stray_git_clone_remains_outside_the_helper(self):
        """Le litteral 'git', 'clone' n'apparait qu'une fois dans app.py :
        dans _git_clone_shallow."""
        src = Path(app.__file__).read_text()
        self.assertEqual(src.count('"git", "clone"'), 1)


if __name__ == "__main__":
    unittest.main()
