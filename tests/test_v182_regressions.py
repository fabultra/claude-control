"""v1.8.2 - Regression tests pour les 3 bugs prod observes le
2026-05-06 14:40 UTC sur le Mac de Fabien :

- Bug A : Desktop Commander en 'Inactifs' alors que log mtime recent.
  Cause : _find_mcp_log retournait le PREMIER match dans glob, qui
  pouvait etre un log obsolete (legacy npx) plutot que le log actif
  (MCPB extension recent). Fix : tie-break par mtime parmi les exact
  matches.

- Bug B : Bandeau watchdog garde 'desktop-commander - Claude arrete'
  apres que l'utilisateur ait retire la cle du config. Fix : auto-reset
  vers 'claude_desktop' quand la cible disparait.

- Bug C : isEnabled (canonique Anthropic recent) vs enabled (legacy)
  desync. Filesystem et Stripe avaient {'isEnabled': false} mais Claude
  Control regardait 'enabled' (absent) -> tombait sur fallback True ->
  UI affichait 'cochee' alors que CD ne lancait pas l'extension. Fix :
  isEnabled prioritaire en lecture, ecrit toujours les 2 cles en sync.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


class BugAMultiLogMatchTests(unittest.TestCase):
    """Bug A : si plusieurs fichiers log normalisent vers le meme nom,
    _find_mcp_log doit retourner le plus recent par mtime, pas le premier
    dans glob."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.fake_logs = Path(self.tmpdir.name)
        self._orig_logs = app.CLAUDE_LOGS_DIR
        app.CLAUDE_LOGS_DIR = self.fake_logs

    def tearDown(self):
        app.CLAUDE_LOGS_DIR = self._orig_logs
        self.tmpdir.cleanup()

    def _write_log(self, filename, age_seconds):
        f = self.fake_logs / filename
        f.write_text("dummy log content")
        past = time.time() - age_seconds
        os.utime(f, (past, past))
        return f

    def test_returns_most_recent_among_exact_matches(self):
        """Cas reel : 'desktop-commander' (legacy npx) et 'Desktop Commander'
        (MCPB extension) coexistent. Tous deux normalisent vers
        'desktopcommander'. Le premier est obsolete (mtime ancien), le
        second est actif (mtime recent). _find_mcp_log doit retourner
        le RECENT, sinon running_by_log echoue."""
        old = self._write_log("mcp-server-desktop-commander.log", age_seconds=3600)
        recent = self._write_log("mcp-server-Desktop Commander.log", age_seconds=2)
        result = app._find_mcp_log("Desktop Commander")
        self.assertEqual(result, recent)

    def test_returns_most_recent_among_exact_matches_reverse_order(self):
        """Symmetrie : meme test avec l'ordre filesystem inverse, pour
        garantir qu'on ne depend pas de l'ordre de glob."""
        recent = self._write_log("mcp-server-desktop-commander.log", age_seconds=2)
        old = self._write_log("mcp-server-Desktop Commander.log", age_seconds=3600)
        result = app._find_mcp_log("Desktop Commander")
        self.assertEqual(result, recent)


class BugBWatchdogTargetAutoResetTests(unittest.TestCase):
    """Bug B : si la cible watchdog disparait du config (utilisateur
    nettoye claude_desktop_config.json), get_watchdog_status doit
    auto-reset vers claude_desktop pour eviter d'afficher un label
    orphelin pour toujours."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.wd_file = root / "watchdog.json"
        self.install_file = root / "extensions-installations.json"
        self.install_file.write_text(json.dumps({"installations": []}))

        self._orig = (
            app.WATCHDOG_FILE, app.EXTENSIONS_INSTALL_FILE,
            app.load_config, app._claude_pids,
        )
        app.WATCHDOG_FILE = self.wd_file
        app.EXTENSIONS_INSTALL_FILE = self.install_file
        # claude_desktop_config.json vide (mcpServers / _disabledMcps absents)
        app.load_config = lambda: {"mcpServers": {}, "_disabledMcps": {}}
        app._claude_pids = lambda: []

    def tearDown(self):
        (app.WATCHDOG_FILE, app.EXTENSIONS_INSTALL_FILE,
         app.load_config, app._claude_pids) = self._orig
        self.tmpdir.cleanup()

    def test_orphan_target_auto_resets_to_claude_desktop(self):
        """Cas reel : Fabien a retire 'desktop-commander' du config,
        mais la cible watchdog reste 'desktop-commander'. Au prochain
        get_watchdog_status, la cible doit auto-reset vers
        claude_desktop (avec event de trace)."""
        # Ecrit la config watchdog avec la cible orpheline
        app.save_watchdog_config({"target": "desktop-commander", "enabled": True})
        # get_watchdog_status doit auto-reset
        status = app.get_watchdog_status()
        self.assertEqual(status["config"]["target"], "claude_desktop")
        self.assertEqual(status["target_label"], "Claude Desktop")
        # Un event de trace 'target_auto_reset' doit avoir ete emis
        actions = [ev["action"] for ev in app._WATCHDOG_EVENTS[-5:]]
        self.assertIn("target_auto_reset", actions)

    def test_existing_target_preserved_when_in_config(self):
        """Anti-regression : si la cible existe toujours dans la config,
        on ne touche a rien."""
        app.load_config = lambda: {"mcpServers": {"my-mcp": {"command": "node"}}, "_disabledMcps": {}}
        app.save_watchdog_config({"target": "my-mcp", "enabled": True})
        status = app.get_watchdog_status()
        self.assertEqual(status["config"]["target"], "my-mcp")

    def test_claude_desktop_target_never_reset(self):
        """Anti-regression : 'claude_desktop' et 'custom' sont des cibles
        speciales, jamais reset."""
        app.save_watchdog_config({"target": "claude_desktop", "enabled": True})
        status = app.get_watchdog_status()
        self.assertEqual(status["config"]["target"], "claude_desktop")
        app.save_watchdog_config({"target": "custom", "target_pattern": "node-server", "enabled": True})
        status = app.get_watchdog_status()
        self.assertEqual(status["config"]["target"], "custom")


class BugCIsEnabledVsEnabledTests(unittest.TestCase):
    """Bug C : Anthropic a renomme 'enabled' -> 'isEnabled' dans le schema
    des fichiers Claude Extensions Settings/*.json. Claude Desktop lit
    'isEnabled'. Claude Control lisait/ecrivait 'enabled'. Resultat :
    cocher dans l'UI Claude Control n'avait aucun effet sur Claude Desktop
    (les fichiers Filesystem et Stripe etaient {'isEnabled': false} -> CD
    ne lancait pas, mais UI montrait 'cochee')."""

    def test_read_isEnabled_priority_over_enabled(self):
        # Cas Filesystem : isEnabled false, enabled absent -> CD pas
        # demarre malgre UI qui affichait true.
        self.assertFalse(app._read_extension_enabled({"isEnabled": False}))
        # Cas inverse explicite
        self.assertTrue(app._read_extension_enabled({"isEnabled": True}))

    def test_read_fallback_enabled_when_no_isEnabled(self):
        # Anciens fichiers : seulement 'enabled' present
        self.assertTrue(app._read_extension_enabled({"enabled": True}))
        self.assertFalse(app._read_extension_enabled({"enabled": False}))

    def test_read_isEnabled_wins_when_both_disagree(self):
        # Fichier mixte coherent : les 2 cles sont d'accord
        self.assertTrue(app._read_extension_enabled({"isEnabled": True, "enabled": True}))
        # Fichier mixte INCOHERENT : isEnabled doit gagner (canonique)
        self.assertFalse(app._read_extension_enabled({"isEnabled": False, "enabled": True}))
        self.assertTrue(app._read_extension_enabled({"isEnabled": True, "enabled": False}))

    def test_read_default_true_when_neither_present(self):
        # Aucun champ : default a True (l'extension est activee si rien
        # n'est explicitement dit)
        self.assertTrue(app._read_extension_enabled({}))

    def test_read_uses_fallback_entry_when_settings_empty(self):
        # Si settings est vide MAIS l'entry de extensions-installations.json
        # a un 'enabled', on l'utilise.
        self.assertFalse(app._read_extension_enabled({}, fallback_entry={"enabled": False}))

    def test_set_helper_writes_both_keys(self):
        s = {}
        app._set_extension_enabled(s, True)
        self.assertEqual(s["isEnabled"], True)
        self.assertEqual(s["enabled"], True)
        app._set_extension_enabled(s, False)
        self.assertEqual(s["isEnabled"], False)
        self.assertEqual(s["enabled"], False)

    def test_save_syncs_both_keys_on_disk(self):
        """Quand on save un dict avec seulement isEnabled OU seulement
        enabled, le fichier sur disque doit contenir les 2 cles avec la
        meme valeur."""
        with tempfile.TemporaryDirectory() as td:
            self._orig = (app.EXTENSIONS_SETTINGS_DIR, app.BACKUP_DIR)
            app.EXTENSIONS_SETTINGS_DIR = Path(td) / "ext-settings"
            app.BACKUP_DIR = Path(td) / "backup"
            try:
                # Cas 1 : on passe isEnabled seulement
                app._save_extension_settings("ext.id.1", {"isEnabled": True})
                data = json.loads((app.EXTENSIONS_SETTINGS_DIR / "ext.id.1.json").read_text())
                self.assertEqual(data["isEnabled"], True)
                self.assertEqual(data["enabled"], True)
                # Cas 2 : on passe enabled seulement
                app._save_extension_settings("ext.id.2", {"enabled": False})
                data = json.loads((app.EXTENSIONS_SETTINGS_DIR / "ext.id.2.json").read_text())
                self.assertEqual(data["isEnabled"], False)
                self.assertEqual(data["enabled"], False)
            finally:
                app.EXTENSIONS_SETTINGS_DIR, app.BACKUP_DIR = self._orig


if __name__ == "__main__":
    unittest.main()
