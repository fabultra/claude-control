"""v1.14.19 - Tests : si la cause est la session expiree, l'APP le demande.

Demande utilisateur, mot pour mot : "si c'est la cause l'app me devrait le
demander". Le jeton OAuth du CLI porte sa date d'expiration : la lire est
local, gratuit, sans reseau -- seul expiresAt est extrait, le secret n'est
jamais conserve ni logge. Une expiration nettement depassee (marge 1 h pour
laisser sa chance au refresh) declenche :
- au demarrage du serveur : une boite de dialogue macOS "Ouvrir le
  Terminal ?" qui pre-remplit `claude /login` ;
- dans l'onglet Skills : un bandeau rouge permanent avec "Se reconnecter" ;
- sur un ClaudeCliNotLoggedIn en cours de reparation : la meme demande.
Le dialogue n'est arme que par main() (jamais depuis les tests) et respecte
un cooldown pour ne pas harceler.
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402

SECRET = "sk-ant-oat01-SECRET-VALUE"


def _write_creds(home, expires_at_ms):
    d = home / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    (d / ".credentials.json").write_text(json.dumps({
        "claudeAiOauth": {"accessToken": SECRET,
                          "refreshToken": SECRET + "-r",
                          "expiresAt": expires_at_ms}}))


class OauthExpiryReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._home = app.HOME
        app.HOME = self.tmp

    def tearDown(self):
        app.HOME = self._home

    def test_reads_milliseconds_and_converts(self):
        _write_creds(self.tmp, 1_800_000_000_000)  # ms
        self.assertAlmostEqual(app._read_cli_oauth_expiry(),
                               1_800_000_000.0, delta=1)

    def test_seconds_form_is_kept_as_is(self):
        _write_creds(self.tmp, 1_800_000_000)  # deja en secondes
        self.assertAlmostEqual(app._read_cli_oauth_expiry(),
                               1_800_000_000.0, delta=1)

    def test_missing_or_malformed_returns_none(self):
        self.assertIsNone(app._read_cli_oauth_expiry())
        d = self.tmp / ".claude"
        d.mkdir()
        (d / ".credentials.json").write_text("{pas du json")
        self.assertIsNone(app._read_cli_oauth_expiry())


class SessionCheckTests(unittest.TestCase):
    def setUp(self):
        with app._CLI_SESSION_LOCK:
            app._CLI_SESSION.update({"status": "unknown", "detail": None,
                                     "checked_ts": 0.0})

    def tearDown(self):
        self.setUp()

    def test_long_expired_is_flagged_with_age(self):
        two_weeks_ago = time.time() - 14 * 86400
        with patch.object(app, "_read_cli_oauth_expiry",
                          lambda: two_weeks_ago):
            s = app._check_cli_session(force=True)
        self.assertEqual(s["status"], "expired")
        self.assertIn("claude /login", s["detail"])
        self.assertIn("14 jour", s["detail"])

    def test_recently_expired_gets_the_refresh_grace(self):
        with patch.object(app, "_read_cli_oauth_expiry",
                          lambda: time.time() - 600):
            s = app._check_cli_session(force=True)
        self.assertEqual(s["status"], "ok")

    def test_future_expiry_is_ok_and_unknown_stays_unknown(self):
        with patch.object(app, "_read_cli_oauth_expiry",
                          lambda: time.time() + 3600):
            self.assertEqual(app._check_cli_session(force=True)["status"], "ok")
        with patch.object(app, "_read_cli_oauth_expiry", lambda: None):
            self.assertEqual(app._check_cli_session(force=True)["status"],
                             "unknown")

    def test_result_is_cached_between_checks(self):
        calls = []
        with patch.object(app, "_read_cli_oauth_expiry",
                          lambda: calls.append(1) or (time.time() + 3600)):
            app._check_cli_session(force=True)
            app._check_cli_session()
        self.assertEqual(len(calls), 1)

    def test_the_secret_never_reaches_the_result(self):
        tmp = Path(tempfile.mkdtemp())
        _write_creds(tmp, (time.time() - 14 * 86400) * 1000)
        with patch.object(app, "HOME", tmp):
            s = app._check_cli_session(force=True)
        self.assertNotIn(SECRET, json.dumps(s))


class ReloginDialogTests(unittest.TestCase):
    def setUp(self):
        self._state = dict(app._RELOGIN_DIALOG)
        app._RELOGIN_DIALOG.update({"armed": False, "last_ts": None})

    def tearDown(self):
        app._RELOGIN_DIALOG.update(self._state)

    def test_disarmed_by_default_for_tests(self):
        def forbidden(*a, **kw):
            raise AssertionError("aucun dialogue depuis les tests")

        with patch.object(app.threading, "Thread", forbidden):
            app._ask_relogin_dialog("x")  # ne doit rien lancer

    def test_armed_dialog_opens_terminal_on_click(self):
        app._RELOGIN_DIALOG.update({"armed": True, "last_ts": None})
        opened = []

        class _Inline:
            def __init__(self, target=None, **kw):
                self._t = target

            def start(self):
                self._t()

        with patch.object(app.threading, "Thread", _Inline), \
                patch.object(app.subprocess, "run",
                             lambda cmd, **kw: type("R", (), {
                                 "stdout": "button returned:Ouvrir le Terminal",
                                 "stderr": "", "returncode": 0})()), \
                patch.object(app, "open_terminal_claude_login",
                             lambda: opened.append(1) or (True, "ok")):
            app._ask_relogin_dialog("session expiree")
        self.assertEqual(opened, [1])

    def test_cooldown_prevents_nagging(self):
        app._RELOGIN_DIALOG.update({"armed": True, "last_ts": None})
        launches = []

        class _Inline:
            def __init__(self, target=None, **kw):
                launches.append(target)

            def start(self):
                pass

        with patch.object(app.threading, "Thread", _Inline):
            app._ask_relogin_dialog("x")
            app._ask_relogin_dialog("x")
        self.assertEqual(len(launches), 1)


class WiringTests(unittest.TestCase):
    def test_state_carries_the_expired_session(self):
        import inspect
        src = inspect.getsource(app._compute_state)
        self.assertIn("cli_session_expired", src)

    def test_skills_banner_offers_the_relogin_button(self):
        self.assertIn("cli_session_expired", app.HTML)
        self.assertIn("openTerminalForLogin()", app.HTML)
        self.assertEqual(app.HTML.count("btn_relogin:"), 2)  # fr + en

    def test_main_arms_and_checks_at_startup(self):
        import inspect
        src = inspect.getsource(app.main)
        self.assertIn('_RELOGIN_DIALOG["armed"] = True', src)
        self.assertIn("_startup_session_check", src)

    def test_not_logged_in_paths_ask(self):
        import inspect
        self.assertIn("_ask_relogin_dialog",
                      inspect.getsource(app.suggest_skill_description))
        self.assertIn("_ask_relogin_dialog",
                      inspect.getsource(app._bulk_repair_probe))


if __name__ == "__main__":
    unittest.main()
