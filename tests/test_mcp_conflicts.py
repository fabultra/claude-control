"""v1.8.1 - Tests pour _detect_mcp_conflicts qui croise les Desktop
Extensions et les entrees classic dans claude_desktop_config.json.
Apprentissage 2026-05-06 : DC tournait en double (extension MCPB +
config manuelle), 16 Helper Nodes au lieu de 8, cause probable du
freeze Type B.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


class DetectMcpConflictsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.install_file = root / "extensions-installations.json"
        self.settings_dir = root / "Claude Extensions Settings"
        self.settings_dir.mkdir(parents=True)
        self._orig = (
            app.EXTENSIONS_INSTALL_FILE, app.EXTENSIONS_SETTINGS_DIR,
            app.load_config,
        )
        app.EXTENSIONS_INSTALL_FILE = self.install_file
        app.EXTENSIONS_SETTINGS_DIR = self.settings_dir

        self._fake_config = {"mcpServers": {}, "_disabledMcps": {}}
        app.load_config = lambda: dict(self._fake_config)

    def tearDown(self):
        app.EXTENSIONS_INSTALL_FILE, app.EXTENSIONS_SETTINGS_DIR, app.load_config = self._orig
        self.tmpdir.cleanup()

    def _set_extensions(self, extensions):
        self.install_file.write_text(json.dumps({"installations": extensions}))
        for ext in extensions:
            ext_id = ext.get("id")
            if ext_id:
                (self.settings_dir / f"{ext_id}.json").write_text(json.dumps({"enabled": True}))

    def test_conflict_detected_by_normalized_name(self):
        """Cas reel 2026-05-06 : 'Desktop Commander' (display_name MCPB)
        + 'desktop-commander' (cle config) -> matchent apres normalisation
        re.sub(r'[^a-z0-9]', '', name.lower()) = 'desktopcommander'."""
        # Manifest realiste : DXT MCPB inclut typiquement la reference au
        # package npm dans son champ server.
        self._set_extensions([{
            "id": "ant.dir.gh.wonderwhy-er.desktopcommandermcp",
            "manifest": {
                "display_name": "Desktop Commander",
                "version": "0.2.40",
                "server": {"command": "npx", "args": ["-y", "@wonderwhy-er/desktop-commander"]},
            },
        }])
        self._fake_config = {
            "mcpServers": {
                "desktop-commander": {
                    "command": "npx",
                    "args": ["-y", "@wonderwhy-er/desktop-commander"],
                }
            },
            "_disabledMcps": {},
        }
        conflicts = app._detect_mcp_conflicts()
        self.assertEqual(len(conflicts), 1)
        c = conflicts[0]
        self.assertEqual(c["classic_name"], "desktop-commander")
        self.assertEqual(c["extension_name"], "Desktop Commander")
        # Ce cas matche sur le nom (normalise) ET le package npm
        # (wonderwhy-er/desktop-commander present dans args + dans l'id de
        # l'extension).
        self.assertEqual(c["match_type"], "both")
        self.assertIn("@wonderwhy-er/desktop-commander", c["matched_packages"])
        self.assertEqual(c["recommendation"], "remove_classic")

    def test_no_conflict_when_only_extension(self):
        """Si une extension est installee mais aucune entree config manuelle
        avec le meme nom, pas de conflit."""
        self._set_extensions([{
            "id": "com.example.foo",
            "manifest": {"display_name": "Foo Extension"},
        }])
        self._fake_config = {"mcpServers": {}, "_disabledMcps": {}}
        conflicts = app._detect_mcp_conflicts()
        self.assertEqual(conflicts, [])

    def test_no_conflict_when_only_classic_config(self):
        """Si une entree config existe mais pas d'extension correspondante,
        pas de conflit."""
        self._set_extensions([])
        self._fake_config = {
            "mcpServers": {"my-custom-mcp": {"command": "node", "args": ["/path/server.js"]}},
            "_disabledMcps": {},
        }
        conflicts = app._detect_mcp_conflicts()
        self.assertEqual(conflicts, [])

    def test_conflict_detected_by_npm_package_alone(self):
        """Cas edge : noms differents mais le meme package @scope/name est
        present dans args du config ET dans le manifest de l'extension.
        Match par 'package' uniquement."""
        self._set_extensions([{
            "id": "com.example.repackaged",
            "manifest": {
                "display_name": "Re-Packaged Extension",
                "server": {"command": "npx", "args": ["-y", "@some-scope/the-tool"]},
            },
        }])
        self._fake_config = {
            "mcpServers": {
                "completely-different-name": {
                    "command": "npx",
                    "args": ["-y", "@some-scope/the-tool"],
                }
            },
            "_disabledMcps": {},
        }
        conflicts = app._detect_mcp_conflicts()
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["match_type"], "package")
        self.assertIn("@some-scope/the-tool", conflicts[0]["matched_packages"])

    def test_conflict_in_disabled_bucket_also_detected(self):
        """Une entree dans _disabledMcps en doublon avec une extension est
        aussi un conflit (l'utilisateur l'a mise de cote mais elle reste
        un orphelin du config)."""
        self._set_extensions([{
            "id": "com.example.thing",
            "manifest": {"display_name": "Thing"},
        }])
        self._fake_config = {
            "mcpServers": {},
            "_disabledMcps": {"thing": {"command": "node"}},
        }
        conflicts = app._detect_mcp_conflicts()
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["classic_name"], "thing")
        self.assertFalse(conflicts[0]["classic_active"])


class ResolveMcpConflictTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.install_file = root / "extensions-installations.json"
        self.settings_dir = root / "Claude Extensions Settings"
        self.settings_dir.mkdir(parents=True)
        self.install_file.write_text(json.dumps({"installations": [{
            "id": "ant.dir.gh.wonderwhy-er.desktopcommandermcp",
            "manifest": {"display_name": "Desktop Commander", "version": "0.2.40"},
        }]}))
        (self.settings_dir / "ant.dir.gh.wonderwhy-er.desktopcommandermcp.json").write_text(
            json.dumps({"enabled": True}))

        self._fake_config = {
            "mcpServers": {
                "desktop-commander": {"command": "npx", "args": ["-y", "@wonderwhy-er/desktop-commander"]},
                "other-mcp": {"command": "node"},
            },
            "_disabledMcps": {},
        }
        self._saved_configs = []

        self._orig = (
            app.EXTENSIONS_INSTALL_FILE, app.EXTENSIONS_SETTINGS_DIR,
            app.load_config, app.save_config,
        )
        app.EXTENSIONS_INSTALL_FILE = self.install_file
        app.EXTENSIONS_SETTINGS_DIR = self.settings_dir
        app.load_config = lambda: json.loads(json.dumps(self._fake_config))

        def fake_save(c):
            self._saved_configs.append(json.loads(json.dumps(c)))
            self._fake_config = c

        app.save_config = fake_save

    def tearDown(self):
        (app.EXTENSIONS_INSTALL_FILE, app.EXTENSIONS_SETTINGS_DIR,
         app.load_config, app.save_config) = self._orig
        self.tmpdir.cleanup()

    def test_resolve_removes_classic_entry_only(self):
        ok, msg = app.resolve_mcp_conflict("desktop-commander")
        self.assertTrue(ok, msg)
        self.assertEqual(len(self._saved_configs), 1)
        saved = self._saved_configs[0]
        self.assertNotIn("desktop-commander", saved.get("mcpServers", {}))
        self.assertIn("other-mcp", saved.get("mcpServers", {}))

    def test_resolve_no_conflict_returns_error(self):
        ok, msg = app.resolve_mcp_conflict("does-not-exist")
        self.assertFalse(ok)
        self.assertIn("Aucun conflit", msg)


if __name__ == "__main__":
    unittest.main()
