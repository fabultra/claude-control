"""Verifie que _list_extensions() lit le bon nom (display_name) selon le bug
v1.6.3 : avant le fix, le nom retenu etait toujours le slug technique de l'id
parce que `name` racine etait absent et que le manifest n'etait pas regarde, ce
qui cassait le matching log<->extension pour toute extension dont le
display_name differe de son slug.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


class ListExtensionsNameSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.fake_install = Path(self.tmpdir.name) / "extensions-installations.json"
        self.fake_settings_dir = Path(self.tmpdir.name) / "Claude Extensions Settings"
        self.fake_settings_dir.mkdir(parents=True, exist_ok=True)

        self._orig_install = app.EXTENSIONS_INSTALL_FILE
        self._orig_settings = app.EXTENSIONS_SETTINGS_DIR
        app.EXTENSIONS_INSTALL_FILE = self.fake_install
        app.EXTENSIONS_SETTINGS_DIR = self.fake_settings_dir

    def tearDown(self):
        app.EXTENSIONS_INSTALL_FILE = self._orig_install
        app.EXTENSIONS_SETTINGS_DIR = self._orig_settings
        self.tmpdir.cleanup()

    def _write(self, payload):
        self.fake_install.write_text(json.dumps(payload))

    def test_manifest_display_name_takes_priority(self):
        """Quand manifest.display_name est present, c'est lui le nom retenu
        (et PAS le slug d'id). C'est le coeur du fix v1.6.3 : le log file Claude
        s'appelle 'mcp-server-Desktop Commander.log' et non
        'mcp-server-com.example.dc.log'."""
        self._write({"installations": [{
            "id": "com.example.dc",
            "manifest": {"display_name": "Desktop Commander", "name": "dc-internal", "version": "1.0.0"},
        }]})
        items = app._list_extensions()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Desktop Commander")
        self.assertEqual(items[0]["id"], "com.example.dc")

    def test_falls_back_to_manifest_name_when_no_display_name(self):
        """Sans display_name, on retombe sur manifest.name (et pas sur le slug
        d'id)."""
        self._write({"installations": [{
            "id": "com.example.thing",
            "manifest": {"name": "Thing Pro", "version": "2.0"},
        }]})
        items = app._list_extensions()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Thing Pro")

    def test_falls_back_to_id_slug_when_no_manifest(self):
        """Sans manifest du tout, on retombe sur le dernier segment de l'id
        (ancien comportement). C'est le fallback de derniere chance."""
        self._write({"installations": [{"id": "com.example.legacy-tool"}]})
        items = app._list_extensions()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "legacy-tool")


if __name__ == "__main__":
    unittest.main()
