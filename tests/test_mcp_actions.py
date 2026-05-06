"""v1.7.6 - Tests pour les nouvelles actions MCP/Extension :
- stop_mcp(name) : kill PIDs sans toucher au config
- delete_extension(name) : retire registry + dir + settings, avec backup
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


class StopMcpTests(unittest.TestCase):
    def setUp(self):
        self._orig = (app.load_config, app._mcp_pids, app._list_extensions, app._extension_pids, os.kill)
        self._kills = []

        def fake_kill(pid, sig):
            self._kills.append((pid, sig))

        os.kill = fake_kill

    def tearDown(self):
        app.load_config, app._mcp_pids, app._list_extensions, app._extension_pids, kill_orig = self._orig
        os.kill = kill_orig

    def test_classic_mcp_with_pids_kills_them(self):
        app.load_config = lambda: {"mcpServers": {"foo": {"command": "node"}}, "_disabledMcps": {}}
        app._mcp_pids = lambda name: [12345, 67890]
        ok, msg = app.stop_mcp("foo")
        self.assertTrue(ok)
        self.assertIn("stoppe", msg)
        self.assertIn("config intact", msg)
        self.assertEqual(len(self._kills), 2)
        self.assertEqual(self._kills[0][1], 9)

    def test_classic_mcp_with_no_pids_returns_already_stopped(self):
        app.load_config = lambda: {"mcpServers": {"foo": {"command": "node"}}, "_disabledMcps": {}}
        app._mcp_pids = lambda name: []
        ok, msg = app.stop_mcp("foo")
        self.assertTrue(ok)
        self.assertIn("aucun process", msg)
        self.assertEqual(self._kills, [])

    def test_extension_with_no_pids_returns_helper_node_message(self):
        """v1.6.6 limitation : Helper Nodes anonymes - kill PID echoue souvent.
        On retourne un message honnete plutot qu'un faux succes."""
        app.load_config = lambda: {"mcpServers": {}, "_disabledMcps": {}}
        app._list_extensions = lambda: [{"id": "ext.id", "name": "MyExt", "version": "1", "enabled": True, "type": "extension", "env_keys": []}]
        app._extension_pids = lambda n: []
        ok, msg = app.stop_mcp("MyExt")
        self.assertTrue(ok)
        self.assertIn("Helper Node", msg)
        self.assertIn("decoche", msg)

    def test_unknown_name_returns_error(self):
        app.load_config = lambda: {"mcpServers": {}, "_disabledMcps": {}}
        app._list_extensions = lambda: []
        ok, msg = app.stop_mcp("ghost")
        self.assertFalse(ok)
        self.assertIn("introuvable", msg)


class StartMcpTests(unittest.TestCase):
    def setUp(self):
        self._orig = (app.load_config, app.save_config, app._list_extensions,
                      app._load_extension_settings, app._save_extension_settings)
        self._saved_configs = []
        self._saved_settings = []

        def fake_save_config(c):
            import copy
            self._saved_configs.append(copy.deepcopy(c))

        app.save_config = fake_save_config
        app._save_extension_settings = lambda eid, s: self._saved_settings.append((eid, dict(s)))

    def tearDown(self):
        (app.load_config, app.save_config, app._list_extensions,
         app._load_extension_settings, app._save_extension_settings) = self._orig

    def test_classic_mcp_active_toggles_off_then_on(self):
        # Premier load_config -> mcp dans active. Deuxieme apres toggle off
        # -> dans disabled.
        configs = [
            {"mcpServers": {"foo": {"command": "node"}}, "_disabledMcps": {}},
            {"mcpServers": {}, "_disabledMcps": {"foo": {"command": "node"}}},
        ]
        app.load_config = lambda: configs.pop(0)
        ok, msg = app.start_mcp("foo")
        self.assertTrue(ok, msg)
        self.assertIn("demarre a chaud", msg)
        # Deux save_config : off puis on
        self.assertEqual(len(self._saved_configs), 2)
        self.assertNotIn("foo", self._saved_configs[0]["mcpServers"])
        self.assertIn("foo", self._saved_configs[1]["mcpServers"])

    def test_classic_mcp_disabled_returns_error(self):
        app.load_config = lambda: {"mcpServers": {}, "_disabledMcps": {"foo": {"command": "node"}}}
        ok, msg = app.start_mcp("foo")
        self.assertFalse(ok)
        self.assertIn("coche-le", msg)

    def test_extension_enabled_toggles_settings(self):
        app.load_config = lambda: {"mcpServers": {}, "_disabledMcps": {}}
        app._list_extensions = lambda: [{"id": "ext.id", "name": "MyExt", "version": "1",
                                          "enabled": True, "type": "extension", "env_keys": []}]
        app._load_extension_settings = lambda eid: {"enabled": True}
        ok, msg = app.start_mcp("MyExt")
        self.assertTrue(ok, msg)
        self.assertIn("demarree a chaud", msg)
        # Deux saves : off puis on
        self.assertEqual(len(self._saved_settings), 2)
        self.assertEqual(self._saved_settings[0][1]["enabled"], False)
        self.assertEqual(self._saved_settings[1][1]["enabled"], True)


class DeleteExtensionTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.install_root = root / "Claude Extensions"
        self.settings_root = root / "Claude Extensions Settings"
        self.backup_dir = root / "backup"
        self.install_root.mkdir(parents=True)
        self.settings_root.mkdir(parents=True)
        self.backup_dir.mkdir(parents=True)

        self.install_file = root / "extensions-installations.json"
        self.install_file.write_text(json.dumps({
            "installations": [
                {"id": "com.example.foo", "manifest": {"display_name": "Foo Ext", "version": "1.0"}},
                {"id": "com.example.other", "manifest": {"display_name": "Other"}},
            ]
        }))

        self._orig = (
            app.HOME, app.EXTENSIONS_INSTALL_FILE, app.EXTENSIONS_SETTINGS_DIR,
            app.BACKUP_DIR, app._list_extensions,
        )
        app.HOME = root.parent
        app.EXTENSIONS_INSTALL_FILE = self.install_file
        app.EXTENSIONS_SETTINGS_DIR = self.settings_root
        app.BACKUP_DIR = self.backup_dir

        # Stub _list_extensions pour matcher l'ext_id qu'on teste sans avoir
        # besoin d'un vrai parser de extensions-installations.json
        app._list_extensions = lambda: [
            {"id": "com.example.foo", "name": "Foo Ext", "version": "1.0",
             "enabled": True, "type": "extension", "env_keys": []},
        ]

        # Cree l'install dir et le settings file
        ext_dir = self.install_root / "com.example.foo"
        ext_dir.mkdir()
        (ext_dir / "manifest.json").write_text('{"name": "foo"}')
        (self.settings_root / "com.example.foo.json").write_text('{"enabled": true}')

        # Repointer le chemin construit dans delete_extension :
        # 'HOME / "Library/Application Support/Claude/Claude Extensions" / ext_id'
        # On contourne en monkey-patch direct du Path utilise.
        # Plus simple : changer HOME pour pointer 2 niveaux au-dessus.
        # (Library/Application Support/Claude/Claude Extensions = root)
        # Donc HOME = tmpdir / .. / .. / .. = ../../..
        # Trop fragile - on ajuste delete_extension pour utiliser EXTENSIONS_SETTINGS_DIR.parent
        # Pas faisable sans patcher le code. On stub l'arborescence en consequence.
        app.HOME = root.parent.parent.parent.parent
        # Cree le path attendu par delete_extension
        expected = app.HOME / "Library/Application Support/Claude/Claude Extensions"
        expected.mkdir(parents=True, exist_ok=True)
        # Symlink le contenu (impossible cross-platform proprement) - on fait une copie
        target_dir = expected / "com.example.foo"
        if target_dir.exists():
            import shutil
            shutil.rmtree(target_dir)
        target_dir.mkdir()
        (target_dir / "manifest.json").write_text('{"name": "foo"}')
        self.expected_install = expected

    def tearDown(self):
        (app.HOME, app.EXTENSIONS_INSTALL_FILE, app.EXTENSIONS_SETTINGS_DIR,
         app.BACKUP_DIR, app._list_extensions) = self._orig
        self.tmpdir.cleanup()
        # Cleanup le dir cree dans HOME
        try:
            import shutil
            cleanup = self._orig[0] / "Library/Application Support/Claude/Claude Extensions/com.example.foo"
            if cleanup.exists():
                shutil.rmtree(cleanup)
        except Exception:
            pass

    def test_delete_extension_removes_files_registry_and_creates_backups(self):
        ok, msg = app.delete_extension("Foo Ext")
        self.assertTrue(ok, msg)
        self.assertIn("supprimee", msg)
        # install dir gone
        self.assertFalse((self.expected_install / "com.example.foo").exists())
        # settings file gone
        self.assertFalse((self.settings_root / "com.example.foo.json").exists())
        # registry entry gone
        data = json.loads(self.install_file.read_text())
        ids = [e["id"] for e in data["installations"]]
        self.assertNotIn("com.example.foo", ids)
        self.assertIn("com.example.other", ids)
        # backups present
        zips = list(self.backup_dir.glob("deleted-extension-com.example.foo-*.zip"))
        self.assertEqual(len(zips), 1)

    def test_delete_extension_unknown_returns_error(self):
        ok, msg = app.delete_extension("Ghost")
        self.assertFalse(ok)
        self.assertIn("introuvable", msg)


if __name__ == "__main__":
    unittest.main()
