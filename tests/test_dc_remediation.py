"""v1.7.0 - Tests pour la detection de freeze DC et la machine d'etat de
remediation. On mock dc_status() (pour fournir un log_age contrele) et
_claude_responsive() (pour simuler la responsiveness de Claude Desktop).
Aucun appel reel a osascript, pkill ou Claude Desktop pendant les tests.
"""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


def _reset_state():
    app._DC_REMEDIATION_STATE.update({
        "last_action_ts": 0.0,
        "cooldown_until_ts": 0.0,
        "last_toggle_log_mtime": 0.0,
        "pending_verify_until_ts": 0.0,
        "dialog_in_flight": False,
    })


class DcFreezeClassifyTests(unittest.TestCase):
    def setUp(self):
        _reset_state()
        self._orig_dc = app.dc_status
        self._orig_resp = app._claude_responsive
        self.cfg = {
            "dc_inactivity_threshold_seconds": 120,
            "dc_verify_after_toggle_seconds": 30,
            "dc_cooldown_after_dismiss_seconds": 300,
        }

    def tearDown(self):
        app.dc_status = self._orig_dc
        app._claude_responsive = self._orig_resp
        _reset_state()

    def _stub_dc(self, log_age, log_path="/tmp/fake.log"):
        app.dc_status = lambda: {
            "name": "Desktop Commander",
            "id": "dc.id",
            "version": "1.0",
            "enabled": True,
            "log_path": log_path,
            "log_age_seconds": log_age,
            "running": True,
            "running_by_pid": False,
            "running_by_log": log_age is not None and log_age <= 120,
            "pids": [],
            "last_restart_iso": None,
            "last_restart_age_seconds": None,
            "arch_note": None,
        }

    def test_idle_legitimate_when_log_fresh(self):
        """DC actif (log frais) + Claude responsive = ne rien faire."""
        self._stub_dc(log_age=10)
        app._claude_responsive = lambda timeout=2: True
        verdict = app._dc_freeze_classify(self.cfg)["verdict"]
        self.assertEqual(verdict, "idle_legitimate")

    def test_idle_legitimate_when_log_stale_but_no_client_call(self):
        """v1.8.1 - Un log silencieux n'est PAS un gel.

        Ce test remplace test_frozen_isolated_when_log_stale_and_claude_alive,
        qui encodait la doctrine v1.7.0 : "log silencieux au-dela du seuil +
        Claude responsive = gel isole, agir". Mesure du 2026-08-20 sur 2 591
        appels : zero appel sans reponse, DC ne gele jamais cote backend. Un
        log muet signifie le plus souvent que personne n'appelle DC.

        Sans appel client dans la fenetre analysee, le verdict doit etre
        idle_legitimate et le watchdog ne doit rien toucher.
        """
        self._stub_dc(log_age=300)
        app._claude_responsive = lambda timeout=2: True
        with mock.patch.object(
                app, "_classify_dc_log_freeze_type",
                lambda path: {"type": "inconclusive",
                              "details": {"client_ids_count": 0}}):
            resultat = app._dc_freeze_classify(self.cfg)
        self.assertEqual(resultat["verdict"], "idle_legitimate")
        self.assertEqual(resultat.get("idle_reason"),
                         "log_silencieux_sans_appel_client")

    def test_frozen_backend_when_client_calls_unanswered(self):
        """Le vrai gel backend : des appels clients restent sans reponse.

        C'est le seul cas ou le watchdog doit agir. La difference avec le test
        precedent tient a la presence d'appels clients, pas au silence du log.
        """
        self._stub_dc(log_age=300)
        app._claude_responsive = lambda timeout=2: True
        with mock.patch.object(
                app, "_classify_dc_log_freeze_type",
                lambda path: {"type": "frozen_backend",
                              "details": {"client_ids_count": 3,
                                          "unanswered": 3}}):
            verdict = app._dc_freeze_classify(self.cfg)["verdict"]
        self.assertEqual(verdict, "frozen_backend")

    def test_global_freeze_when_claude_unresponsive(self):
        """Si Claude Desktop ne repond pas non plus, c'est un freeze global,
        pas DC isole - le watchdog ne doit PAS agir."""
        self._stub_dc(log_age=300)
        app._claude_responsive = lambda timeout=2: False
        verdict = app._dc_freeze_classify(self.cfg)["verdict"]
        self.assertEqual(verdict, "global_freeze")

    def test_no_dc_when_extension_not_installed(self):
        app.dc_status = lambda: None
        app._claude_responsive = lambda timeout=2: True
        verdict = app._dc_freeze_classify(self.cfg)["verdict"]
        self.assertEqual(verdict, "no_dc")

    def test_no_log_when_log_path_missing(self):
        self._stub_dc(log_age=None, log_path=None)
        app._claude_responsive = lambda timeout=2: True
        verdict = app._dc_freeze_classify(self.cfg)["verdict"]
        self.assertEqual(verdict, "no_log")


class DcStateMachineTests(unittest.TestCase):
    def setUp(self):
        _reset_state()
        self.cfg = {
            "dc_inactivity_threshold_seconds": 120,
            "dc_verify_after_toggle_seconds": 30,
            "dc_cooldown_after_dismiss_seconds": 300,
        }

    def tearDown(self):
        _reset_state()

    def test_cooldown_blocks_new_detection(self):
        """Apres un dismiss, le verdict est 'cooldown' tant que la fenetre
        de cooldown n'est pas expiree, peu importe l'etat reel de DC."""
        app._DC_REMEDIATION_STATE["cooldown_until_ts"] = time.time() + 250
        verdict = app._dc_freeze_classify(self.cfg)["verdict"]
        self.assertEqual(verdict, "cooldown")

    def test_dialog_in_flight_blocks_new_detection(self):
        """Si un dialog est deja ouvert, on ne re-declenche pas."""
        app._DC_REMEDIATION_STATE["dialog_in_flight"] = True
        verdict = app._dc_freeze_classify(self.cfg)["verdict"]
        self.assertEqual(verdict, "dialog_in_flight")

    def test_pending_verify_blocks_new_detection(self):
        """Apres un toggle, on attend la fenetre de verification avant de
        re-declencher."""
        app._DC_REMEDIATION_STATE["pending_verify_until_ts"] = time.time() + 20
        verdict = app._dc_freeze_classify(self.cfg)["verdict"]
        self.assertEqual(verdict, "pending_verify")


if __name__ == "__main__":
    unittest.main()
