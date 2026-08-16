"""v1.15.0 - Tests : les zones disque a zero couverture en ont enfin une.

Constat d'audit : quatre familles de code qui ECRIVENT sur le disque
n'avaient aucun test :
- _safe_extract_zip : LA garde anti zip-slip de l'app (un ZIP hostile avec
  des entrees ../ ou absolues doit etre rejete en bloc, avant toute
  extraction) ;
- import_skill_zip / import_mcp_zip : uploads utilisateur ;
- les presets (save / list / apply / delete) qui reecrivent
  claude_desktop_config.json ;
- l'auto-update (check_update / apply_update / restart_self) qui va
  jusqu'a os.execv. restart_self n'est teste qu'avec Thread, sleep et
  execv remplaces : la suite ne peut jamais redemarrer quoi que ce soit.
"""
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


def _zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries:
            zf.writestr(name, content)
    return buf.getvalue()


class SafeExtractZipTests(unittest.TestCase):
    """La garde anti zip-slip, testee pour la premiere fois."""

    def setUp(self):
        self.dest = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dest, ignore_errors=True)

    def test_honest_zip_extracts(self):
        app._safe_extract_zip(_zip_bytes([("dossier/fichier.txt", "corps")]),
                              self.dest)
        self.assertEqual((self.dest / "dossier/fichier.txt").read_text(),
                         "corps")

    def test_dotdot_entry_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            app._safe_extract_zip(_zip_bytes([("../evade.txt", "x")]),
                                  self.dest)
        self.assertIn("../evade.txt", str(ctx.exception))
        self.assertFalse((self.dest.parent / "evade.txt").exists())

    def test_nested_dotdot_is_rejected(self):
        with self.assertRaises(ValueError):
            app._safe_extract_zip(
                _zip_bytes([("ok/../../evade.txt", "x")]), self.dest)

    def test_absolute_path_is_rejected(self):
        with self.assertRaises(ValueError):
            app._safe_extract_zip(_zip_bytes([("/tmp/evade.txt", "x")]),
                                  self.dest)

    def test_one_bad_entry_blocks_the_whole_archive(self):
        """Validation AVANT extractall : tout ou rien, jamais d'extraction
        partielle d'une archive hostile."""
        blob = _zip_bytes([("legitime.txt", "ok"), ("../evade.txt", "x")])
        with self.assertRaises(ValueError):
            app._safe_extract_zip(blob, self.dest)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_garbage_bytes_raise_badzipfile(self):
        with self.assertRaises(zipfile.BadZipFile):
            app._safe_extract_zip(b"pas un zip du tout", self.dest)


class ImportSkillZipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._p = patch.object(app, "SKILLS_DIR", self.tmp / "skills")
        self._p.start()

    def tearDown(self):
        self._p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_nested_skill_md_is_unwrapped(self):
        blob = _zip_bytes([("mon-repo/SKILL.md", "---\nname: d\n---\ncorps"),
                           ("mon-repo/notes.txt", "extra")])
        ok, msg = app.import_skill_zip(blob, "demo-skill.zip")
        self.assertTrue(ok, msg)
        root = self.tmp / "skills/demo-skill"
        self.assertTrue((root / "SKILL.md").exists())
        self.assertTrue((root / "notes.txt").exists())

    def test_hostile_zip_is_refused_and_writes_nothing(self):
        blob = _zip_bytes([("SKILL.md", "x"), ("../evade.txt", "x")])
        ok, msg = app.import_skill_zip(blob, "demo.zip")
        self.assertFalse(ok)
        self.assertIn("Entree non sure", msg)
        self.assertFalse((self.tmp / "skills/demo").exists())

    def test_input_gates(self):
        self.assertFalse(app.import_skill_zip(b"", "a.zip")[0])
        with patch.object(app, "MAX_ZIP_SIZE", 10):
            ok, msg = app.import_skill_zip(b"x" * 20, "a.zip")
        self.assertFalse(ok)
        self.assertIn("volumineux", msg)
        ok, msg = app.import_skill_zip(b"pas un zip", "a.zip")
        self.assertFalse(ok)
        self.assertIn("ZIP invalide", msg)

    def test_dotdot_filename_cannot_name_the_target(self):
        blob = _zip_bytes([("SKILL.md", "x")])
        ok, msg = app.import_skill_zip(blob, "..zip")
        self.assertFalse(ok)
        self.assertIn("Nom de fichier invalide", msg)

    def test_no_skill_md_means_no_import(self):
        ok, msg = app.import_skill_zip(_zip_bytes([("lisez.txt", "x")]),
                                       "demo.zip")
        self.assertFalse(ok)
        self.assertIn("SKILL.md", msg)

    def test_existing_skill_is_never_overwritten(self):
        (self.tmp / "skills/demo").mkdir(parents=True)
        ok, msg = app.import_skill_zip(_zip_bytes([("SKILL.md", "x")]),
                                       "demo.zip")
        self.assertFalse(ok)
        self.assertIn("existe deja", msg)


class ImportMcpZipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._p = patch.object(app, "IMPORTED_REPOS_DIR", self.tmp / "mcps")
        self._p.start()

    def tearDown(self):
        self._p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_single_top_dir_is_unwrapped_and_config_detected(self):
        imported = []
        blob = _zip_bytes([("repo/mcp.json", '{"mcpServers": {}}'),
                           ("__MACOSX/bruit", ")")])
        with patch.object(app, "import_mcp_file",
                          lambda p: imported.append(p) or (True, "config importee")):
            ok, msg = app.import_mcp_zip(blob, "repo.zip")
        self.assertTrue(ok, msg)
        self.assertIn("config importee", msg)
        self.assertEqual(len(imported), 1)
        # Le dossier unique 'repo/' a ete deballe : mcp.json vit a la racine.
        self.assertTrue((self.tmp / "mcps/repo/mcp.json").exists())

    def test_zip_without_config_still_lands_with_a_hint(self):
        ok, msg = app.import_mcp_zip(_zip_bytes([("src/serveur.py", "x")]),
                                     "brut.zip")
        self.assertTrue(ok)
        self.assertIn("Aucun config MCP detecte", msg)

    def test_hostile_zip_is_refused(self):
        ok, msg = app.import_mcp_zip(
            _zip_bytes([("../evade.txt", "x")]), "repo.zip")
        self.assertFalse(ok)
        self.assertIn("Entree non sure", msg)


class PresetsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._patches = [
            patch.object(app, "PRESETS_FILE", self.tmp / "presets.json"),
            patch.object(app, "CONFIG_PATH", self.tmp / "config.json"),
            patch.object(app, "BACKUP_DIR", self.tmp / "backups"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_list_delete_roundtrip(self):
        ok, msg = app.save_preset("focus", ["beta", "alpha", "beta", ""])
        self.assertTrue(ok)
        self.assertIn("2 MCP(s)", msg)  # dedoublonne, vides ignores
        self.assertEqual(app.list_presets()["focus"], ["alpha", "beta"])
        self.assertTrue(app.delete_preset("focus")[0])
        self.assertEqual(app.list_presets(), {})

    def test_input_gates(self):
        self.assertFalse(app.save_preset("  ", ["a"])[0])
        self.assertFalse(app.save_preset("x", "pas une liste")[0])
        self.assertFalse(app.delete_preset("fantome")[0])

    def test_corrupt_presets_file_reads_as_empty(self):
        (self.tmp / "presets.json").write_text("{pas du json")
        self.assertEqual(app.list_presets(), {})

    def test_apply_preset_moves_mcps_and_reports_missing(self):
        (self.tmp / "config.json").write_text(json.dumps({
            "mcpServers": {"a": {"command": "a"}, "b": {"command": "b"}},
            "_disabledMcps": {"c": {"command": "c"}},
        }))
        app.save_preset("focus", ["a", "c", "fantome"])
        ok, msg = app.apply_preset("focus")
        self.assertTrue(ok)
        self.assertIn("2 actif(s)", msg)
        self.assertIn("fantome", msg)  # introuvable, signale
        saved = json.loads((self.tmp / "config.json").read_text())
        self.assertEqual(sorted(saved["mcpServers"]), ["a", "c"])
        self.assertEqual(sorted(saved["_disabledMcps"]), ["b"])

    def test_apply_unknown_preset_touches_nothing(self):
        ok, _ = app.apply_preset("fantome")
        self.assertFalse(ok)
        self.assertFalse((self.tmp / "config.json").exists())


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class CheckUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._patches = [
            patch.object(app, "GITHUB_REPO_FILE", self.tmp / "repo.txt"),
            patch.object(app, "VERSION_FILE", self.tmp / "version.txt"),
        ]
        for p in self._patches:
            p.start()
        (self.tmp / "repo.txt").write_text("fabultra/claude-control\n")
        (self.tmp / "version.txt").write_text("1.15.0\n")

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_newer_release_is_announced(self):
        with patch.object(urllib.request, "urlopen",
                          lambda req, timeout=0: _Resp({
                              "tag_name": "v1.16.0",
                              "html_url": "https://x/rel",
                              "body": "notes " * 200})):
            out = app.check_update()
        self.assertEqual((out["local"], out["latest"]), ("1.15.0", "1.16.0"))
        self.assertTrue(out["update_available"])
        self.assertLessEqual(len(out["release_notes"]), 500)

    def test_same_version_means_no_update(self):
        with patch.object(urllib.request, "urlopen",
                          lambda req, timeout=0: _Resp({"tag_name": "v1.15.0"})):
            self.assertFalse(app.check_update()["update_available"])

    def test_no_release_and_network_errors_are_calm(self):
        def nf(req, timeout=0):
            raise urllib.error.HTTPError("u", 404, "nf", None, None)

        with patch.object(urllib.request, "urlopen", nf):
            out = app.check_update()
        self.assertFalse(out["update_available"])
        self.assertIn("Aucune release", out["error"])

        def down(req, timeout=0):
            raise OSError("reseau coupe")

        with patch.object(urllib.request, "urlopen", down):
            self.assertIn("reseau coupe", app.check_update()["error"])

    def test_missing_repo_file_is_reported(self):
        (self.tmp / "repo.txt").unlink()
        out = app.check_update()
        self.assertFalse(out["update_available"])
        self.assertIn("non configure", out["error"])


class ApplyUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._p = patch.object(app, "HOME", self.tmp)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_repo_dir_fails_before_any_git(self):
        def forbidden(*a, **kw):
            raise AssertionError("git ne doit pas tourner sans repo")

        with patch.object(app.subprocess, "run", forbidden):
            ok, msg = app.apply_update()
        self.assertFalse(ok)
        self.assertIn("introuvable", msg)

    def test_pull_then_copy_into_the_app_bundle(self):
        src = self.tmp / "dev/claude-control/src/app.py"
        src.parent.mkdir(parents=True)
        src.write_text("# nouvelle version")
        dst_dir = self.tmp / "Applications/claude-control"
        dst_dir.mkdir(parents=True)
        pulls = []
        with patch.object(app.subprocess, "run",
                          lambda cmd, **kw: pulls.append(cmd) or type(
                              "R", (), {"returncode": 0, "stdout": "",
                                        "stderr": ""})()):
            ok, msg = app.apply_update()
        self.assertTrue(ok, msg)
        self.assertEqual((dst_dir / "app.py").read_text(), "# nouvelle version")
        self.assertIn("--ff-only", pulls[0])

    def test_failed_pull_is_reported(self):
        (self.tmp / "dev/claude-control").mkdir(parents=True)
        with patch.object(app.subprocess, "run",
                          lambda cmd, **kw: type("R", (), {
                              "returncode": 1, "stdout": "",
                              "stderr": "fatal: divergent"})()):
            ok, msg = app.apply_update()
        self.assertFalse(ok)
        self.assertIn("fatal: divergent", msg)


class RestartSelfTests(unittest.TestCase):
    """os.execv remplace le process : teste UNIQUEMENT avec Thread, sleep et
    execv substitues -- la suite ne peut jamais redemarrer quoi que ce soit."""

    class _Inline:
        def __init__(self, target=None, daemon=None, **kw):
            self._t = target

        def start(self):
            self._t()

    def test_execv_relaunches_the_same_script(self):
        calls = []
        with patch.object(app.threading, "Thread", self._Inline), \
                patch.object(app.time, "sleep", lambda s: None), \
                patch.object(app.os, "execv",
                             lambda exe, argv: calls.append((exe, argv))):
            ok, _ = app.restart_self()
        self.assertTrue(ok)
        exe, argv = calls[0]
        self.assertEqual(exe, sys.executable)
        self.assertEqual(argv[0], sys.executable)
        self.assertTrue(argv[1].endswith("app.py"))

    def test_failed_execv_exits_instead_of_hanging(self):
        exited = []

        def boom(exe, argv):
            raise OSError("exec refuse")

        with patch.object(app.threading, "Thread", self._Inline), \
                patch.object(app.time, "sleep", lambda s: None), \
                patch.object(app.os, "execv", boom), \
                patch.object(app.os, "_exit", lambda code: exited.append(code)):
            app.restart_self()
        self.assertEqual(exited, [0])

    def test_apply_update_then_restart_only_restarts_on_success(self):
        restarts = []
        with patch.object(app, "apply_update", lambda: (True, "maj ok")), \
                patch.object(app, "restart_self",
                             lambda: restarts.append(1) or (True, "go")):
            ok, msg = app._apply_update_then_restart()
        self.assertTrue(ok)
        self.assertIn("redemarrage", msg)
        self.assertEqual(restarts, [1])
        with patch.object(app, "apply_update", lambda: (False, "echec")), \
                patch.object(app, "restart_self",
                             lambda: restarts.append(2)):
            ok, _ = app._apply_update_then_restart()
        self.assertFalse(ok)
        self.assertEqual(restarts, [1])  # pas de redemarrage sur echec


if __name__ == "__main__":
    unittest.main()
