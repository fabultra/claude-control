"""v1.14.1 - Tests de non-regression pour :
- l'invocation du CLI Claude Code (le "CLI n'a pas repondu")
- la generation de descriptions riches (les skills a description trop courte)
- la reparation en lot

Chaque classe documente le comportement casse d'avant.
"""
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


class _FakeCompleted:
    def __init__(self, stdout="ok", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


class CallClaudeCliTests(unittest.TestCase):
    """Avant : `claude -p <prompt>` passait le prompt en argv. Commander
    interprete tout argument commencant par '-' comme une option, et un
    SKILL.md commence par '---' (frontmatter) -> `error: unknown option`
    avant meme que le CLI ne demarre. Le CLI chargeait aussi toute la config
    MCP de l'utilisateur avant de repondre.
    """

    def setUp(self):
        self.calls = []
        self._orig_run = app.subprocess.run

        def fake_run(cmd, **kw):
            self.calls.append({"cmd": cmd, "kw": kw})
            return _FakeCompleted(stdout="une description")

        app.subprocess.run = fake_run
        self._orig_path = app._claude_cli_path
        app._claude_cli_path = lambda: "/usr/local/bin/claude"
        # v1.14.4 - _cli_env() peut consulter le shell de connexion (un
        # subprocess de plus, qui atterrirait dans self.calls[0] et decalerait
        # toutes les assertions). Le cache est amorce ici : ces tests portent
        # sur l'appel au binaire claude, pas sur la recuperation d'env.
        self._orig_shell_cache = dict(app._SHELL_ENV_CACHE)
        app._SHELL_ENV_CACHE.update({"done": True, "env": {}})

    def tearDown(self):
        app.subprocess.run = self._orig_run
        app._claude_cli_path = self._orig_path
        app._SHELL_ENV_CACHE.clear()
        app._SHELL_ENV_CACHE.update(self._orig_shell_cache)

    def test_prompt_goes_through_stdin_not_argv(self):
        app._call_claude_cli("---\nname: demo\n---\nbody")
        cmd = self.calls[0]["cmd"]
        self.assertNotIn("---\nname: demo\n---\nbody", cmd)
        self.assertEqual(self.calls[0]["kw"]["input"], "---\nname: demo\n---\nbody")

    def test_prompt_starting_with_dash_does_not_reach_argv(self):
        """Le cas exact qui cassait : un prompt commencant par un tiret."""
        app._call_claude_cli("--- frontmatter first")
        for arg in self.calls[0]["cmd"]:
            self.assertFalse(arg.startswith("--- "), f"prompt fuite dans argv : {arg!r}")

    def test_mcp_stack_is_not_started(self):
        """v1.14.1 obtenait ca avec --safe-mode ; v1.14.6 l'a retire parce
        que cette option bloquait le CLI 2.1.173 indefiniment. L'objectif
        tient : ne pas demarrer les serveurs MCP de l'utilisateur avant de
        generer une phrase. --strict-mcp-config avec une config vide le fait
        sans toucher au reste du demarrage."""
        app._call_claude_cli("x")
        cmd = self.calls[0]["cmd"]
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(cmd[cmd.index("--mcp-config") + 1],
                         '{"mcpServers":{}}')
        self.assertNotIn("--safe-mode", cmd)
        self.assertNotIn("--bare", cmd)

    def test_session_persistence_disabled(self):
        """Sinon chaque suggestion ecrit une session dans ~/.claude/projects,
        le repertoire meme que get_skill_usage parcourt."""
        app._call_claude_cli("x")
        self.assertIn("--no-session-persistence", self.calls[0]["cmd"])

    def test_system_prompt_is_passed_as_flag(self):
        app._call_claude_cli("x", system_prompt="TU ES UN GENERATEUR")
        cmd = self.calls[0]["cmd"]
        self.assertIn("--system-prompt", cmd)
        self.assertEqual(cmd[cmd.index("--system-prompt") + 1], "TU ES UN GENERATEUR")

    def test_no_system_prompt_flag_when_absent(self):
        app._call_claude_cli("x")
        self.assertNotIn("--system-prompt", self.calls[0]["cmd"])

    def test_runs_in_a_neutral_cwd(self):
        """Sinon le CLI herite du cwd du serveur ('/' via Finder) et traite
        ce repertoire comme le projet courant."""
        app._call_claude_cli("x")
        cwd = self.calls[0]["kw"].get("cwd")
        self.assertIsNotNone(cwd)
        self.assertIn("claude-control-cli", str(cwd))

    def test_the_proven_call_gets_a_generous_timeout(self):
        """v1.14.7 - le budget genereux va desormais a l'appel NU, seul
        prouve sur les CLI concernes. L'appel optimise n'a qu'un essai
        court : le depasser signifie qu'il ne repondra pas."""
        calls = self.calls
        orig = app.subprocess.run

        def stall_then_answer(cmd, **kw):
            calls.append({"cmd": cmd, "kw": kw})
            if len(calls) == 1:
                raise subprocess.TimeoutExpired("claude", kw["timeout"])
            return _FakeCompleted(stdout="une description")

        app.subprocess.run = stall_then_answer
        try:
            app._call_claude_cli("x")
        finally:
            app.subprocess.run = orig
        self.assertEqual(calls[0]["kw"]["timeout"], app.CLI_FAST_PATH_TIMEOUT)
        self.assertGreaterEqual(calls[1]["kw"]["timeout"], 120)

    def test_unknown_option_error_is_named(self):
        app.subprocess.run = lambda cmd, **kw: _FakeCompleted(
            stdout="", stderr="error: unknown option '---'", returncode=1)
        with self.assertRaises(RuntimeError) as ctx:
            app._call_claude_cli("x")
        self.assertIn("option", str(ctx.exception))

    def test_not_logged_in_still_detected(self):
        app.subprocess.run = lambda cmd, **kw: _FakeCompleted(
            stdout="Not logged in. Please run /login", stderr="", returncode=1)
        with self.assertRaises(app.ClaudeCliNotLoggedIn):
            app._call_claude_cli("x")

    def test_quota_error_is_named(self):
        app.subprocess.run = lambda cmd, **kw: _FakeCompleted(
            stdout="", stderr="Your credit balance is too low", returncode=1)
        with self.assertRaises(RuntimeError) as ctx:
            app._call_claude_cli("x")
        self.assertIn("quota", str(ctx.exception).lower())

    def test_silent_failure_mentions_login(self):
        app.subprocess.run = lambda cmd, **kw: _FakeCompleted(
            stdout="", stderr="", returncode=1)
        with self.assertRaises(RuntimeError) as ctx:
            app._call_claude_cli("x")
        self.assertIn("claude /login", str(ctx.exception))


class SuggestPromptTests(unittest.TestCase):
    """Avant : le system prompt demandait explicitement '40-150 chars' et
    'Keep it under 150 chars'. C'est la cause directe des descriptions trop
    courtes -- l'app demandait court, puis notait 'excellent' des 30 chars.
    """

    def test_prompt_no_longer_asks_for_short_descriptions(self):
        p = app._SUGGEST_SYSTEM_PROMPT
        self.assertNotIn("under 150", p)
        self.assertNotIn("40-150", p)

    def test_prompt_asks_for_what_and_when(self):
        p = app._SUGGEST_SYSTEM_PROMPT.lower()
        self.assertIn("when to trigger", p)
        self.assertIn("200-450", p)

    def test_prompt_forbids_invention(self):
        self.assertIn("Invent nothing", app._SUGGEST_SYSTEM_PROMPT)


class SuggestionExtractionTests(unittest.TestCase):
    """Avant : l'extraction triait les lignes par longueur et n'en gardait
    QU'UNE. Correct pour une phrase de 150 chars, destructeur des qu'on
    demande 200-450 chars sur plusieurs phrases : un retour a la ligne du
    modele amputait silencieusement la description ecrite sur disque.
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.skills_dir = root / "skills"
        self.disabled_dir = root / "skills-disabled"
        self.skills_dir.mkdir(parents=True)
        self.disabled_dir.mkdir(parents=True)
        self._orig = (app.SKILLS_DIR, app.SKILLS_DISABLED_DIR)
        app.SKILLS_DIR, app.SKILLS_DISABLED_DIR = self.skills_dir, self.disabled_dir
        d = self.skills_dir / "demo"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: demo\n---\n\nTraite les factures.\n")

    def tearDown(self):
        app.SKILLS_DIR, app.SKILLS_DISABLED_DIR = self._orig
        self.tmpdir.cleanup()

    def _suggest(self, response):
        with patch.object(app, "_claude_cli_path", lambda: "/usr/local/bin/claude"):
            with patch.object(app, "_call_claude_cli",
                              lambda prompt, timeout=120, system_prompt=None: response):
                return app.suggest_skill_description("demo")

    def test_multiline_description_is_joined_not_truncated(self):
        ok, payload = self._suggest(
            "Traite les factures fournisseurs : extraction, validation TVA.\n"
            "A utiliser des qu'un PDF de facture doit etre lu ou verifie.")
        self.assertTrue(ok, payload)
        self.assertIn("extraction, validation TVA", payload["suggestion"])
        self.assertIn("A utiliser", payload["suggestion"],
                      "la seconde phrase ne doit pas etre perdue")

    def test_preamble_is_dropped_but_body_kept_whole(self):
        ok, payload = self._suggest(
            "Voici la description :\n\n"
            "Traite les factures fournisseurs.\n"
            "A utiliser quand un PDF de facture arrive.")
        self.assertTrue(ok)
        self.assertNotIn("Voici la description", payload["suggestion"])
        self.assertIn("Traite les factures", payload["suggestion"])
        self.assertIn("A utiliser quand", payload["suggestion"])

    def test_closing_filler_is_dropped(self):
        """Nouveau risque introduit par la jointure : le bavardage de fin
        serait sinon ecrit dans le SKILL.md."""
        ok, payload = self._suggest(
            "Traite les factures fournisseurs.\nHope this helps!")
        self.assertTrue(ok)
        self.assertNotIn("Hope this helps", payload["suggestion"])

    def test_runaway_output_is_capped(self):
        ok, payload = self._suggest("Traite les factures. " * 200)
        self.assertTrue(ok)
        self.assertLessEqual(len(payload["suggestion"]), 700)

    def test_markdown_and_quotes_still_stripped(self):
        ok, payload = self._suggest('## "Traite les factures fournisseurs"')
        self.assertTrue(ok)
        self.assertEqual(payload["suggestion"], "Traite les factures fournisseurs")


class BulkRepairTests(unittest.TestCase):
    """v1.14.1 - reparer 24 skills a la main = 24 clics et 24 modales, et un
    lot depasse largement le timeout d'une requete HTTP."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.skills_dir = root / "skills"
        self.disabled_dir = root / "skills-disabled"
        self.backup_dir = root / "backup"
        for d in (self.skills_dir, self.disabled_dir, self.backup_dir):
            d.mkdir(parents=True)
        self._orig = (app.SKILLS_DIR, app.SKILLS_DISABLED_DIR, app.BACKUP_DIR)
        app.SKILLS_DIR, app.SKILLS_DISABLED_DIR = self.skills_dir, self.disabled_dir
        app.BACKUP_DIR = self.backup_dir
        self._orig_notify = app._notify
        app._notify = lambda *a, **k: None
        app._BULK_REPAIR.update({"running": False, "total": 0, "done": 0,
                                 "current": None, "results": [], "cancelled": False})

    def tearDown(self):
        app.SKILLS_DIR, app.SKILLS_DISABLED_DIR, app.BACKUP_DIR = self._orig
        app._notify = self._orig_notify
        app._BULK_REPAIR.update({"running": False, "total": 0, "done": 0,
                                 "current": None, "results": [], "cancelled": False})
        self.tmpdir.cleanup()

    def _skill(self, name, description=None, base=None):
        d = (base or self.skills_dir) / name
        d.mkdir()
        fm = f'---\nname: {name}\n'
        if description is not None:
            fm += f'description: "{description}"\n'
        fm += '---\n\nCorps du skill.\n'
        (d / "SKILL.md").write_text(fm)
        return d

    GOOD = ("Traite les factures fournisseurs : extraction des montants, "
            "validation de la TVA et export comptable. A utiliser des qu'un PDF "
            "de facture doit etre lu, verifie ou exporte.")

    def test_selects_only_insufficient_skills(self):
        self._skill("court", "Debug Railway")
        self._skill("vide")
        self._skill("bon", self.GOOD)
        names = {s["name"] for s in app._repairable_user_skills()}
        self.assertEqual(names, {"court", "vide"})

    def test_includes_disabled_skills(self):
        self._skill("off", "trop court", base=self.disabled_dir)
        names = {s["name"] for s in app._repairable_user_skills()}
        self.assertIn("off", names)

    def test_reasons_are_reported(self):
        self._skill("court", "Debug Railway")
        self._skill("vide")
        by = {s["name"]: s["reason"] for s in app._repairable_user_skills()}
        self.assertEqual(by["court"], "too_short")
        self.assertEqual(by["vide"], "no_description")

    def _run_bulk(self, suggestion=None, fail=False):
        def fake_suggest(name, body_max_chars=4000, lang=None):
            if fail:
                return False, "CLI indisponible"
            return True, {"suggestion": suggestion or self.GOOD, "source": "claude_cli"}

        with patch.object(app, "_claude_cli_path", lambda: "/usr/local/bin/claude"), \
             patch.object(app, "_call_claude_cli", return_value="pong"):
            with patch.object(app, "suggest_skill_description", fake_suggest):
                ok, payload = app.start_bulk_repair(lang="fr")
                deadline = time.time() + 5
                while time.time() < deadline and app.bulk_repair_status()["running"]:
                    time.sleep(0.02)
        return ok, payload, app.bulk_repair_status()

    def test_bulk_repair_writes_every_flagged_skill(self):
        self._skill("a", "court")
        self._skill("b")
        self._skill("bon", self.GOOD)
        ok, payload, status = self._run_bulk()
        self.assertTrue(ok, payload)
        self.assertEqual(status["total"], 2)
        self.assertEqual(status["done"], 2)
        self.assertTrue(all(r["ok"] for r in status["results"]), status["results"])
        for n in ("a", "b"):
            content = (self.skills_dir / n / "SKILL.md").read_text()
            self.assertIn("Traite les factures fournisseurs", content)
        # le skill deja bon n'est pas touche
        self.assertIn(self.GOOD, (self.skills_dir / "bon" / "SKILL.md").read_text())

    def test_repaired_skills_become_excellent(self):
        self._skill("a", "court")
        self._run_bulk()
        meta = app.read_skill_meta(self.skills_dir / "a")
        self.assertEqual(app._skill_quality(meta["description"]), "excellent")

    def test_backup_created_before_write(self):
        self._skill("a", "court")
        self._run_bulk()
        self.assertTrue(list(self.backup_dir.glob("repaired-skill-a-*.zip")))

    def test_failures_are_recorded_and_do_not_stop_the_run(self):
        self._skill("a", "court")
        self._skill("b", "court aussi")
        ok, payload, status = self._run_bulk(fail=True)
        self.assertTrue(ok)
        self.assertEqual(status["done"], 2, "un echec ne doit pas interrompre le lot")
        self.assertTrue(all(not r["ok"] for r in status["results"]))
        self.assertIn("CLI indisponible", status["results"][0]["error"])

    def test_nothing_to_repair_is_not_an_error(self):
        self._skill("bon", self.GOOD)
        with patch.object(app, "_claude_cli_path", lambda: "/usr/local/bin/claude"):
            ok, payload = app.start_bulk_repair()
        self.assertTrue(ok)
        self.assertFalse(payload["started"])
        self.assertEqual(payload["total"], 0)

    def test_refuses_without_cli(self):
        self._skill("a", "court")
        with patch.object(app, "_claude_cli_path", lambda: None):
            ok, msg = app.start_bulk_repair()
        self.assertFalse(ok)
        self.assertIn("Claude Code CLI", msg)

    def test_refuses_concurrent_runs(self):
        self._skill("a", "court")
        app._BULK_REPAIR["running"] = True
        with patch.object(app, "_claude_cli_path", lambda: "/usr/local/bin/claude"):
            ok, msg = app.start_bulk_repair()
        self.assertFalse(ok)
        self.assertIn("déjà en cours", msg)

    def test_cancel_without_run_is_reported(self):
        ok, msg = app.cancel_bulk_repair()
        self.assertFalse(ok)

    def test_status_reports_pending_count_when_idle(self):
        self._skill("a", "court")
        self._skill("b")
        self.assertEqual(app.bulk_repair_status()["pending"], 2)


class SyncedSkillsTests(unittest.TestCase):
    """v1.14.1 - Claude Code range les skills synchronises depuis le compte
    dans ~/.claude/skills/synced/<nom>/.

    Avant : 'synced' etait compte comme UN skill sans SKILL.md donc "casse",
    et les dizaines de skills qu'il contient etaient invisibles. La selection
    de reparation en lot allait meme ecrire un SKILL.md a la racine du
    conteneur.
    """

    GOOD = ("Traite les factures fournisseurs : extraction, validation TVA et "
            "export comptable. A utiliser des qu'un PDF de facture doit etre "
            "lu, verifie ou exporte vers la compta.")

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.skills_dir = root / "skills"
        self.disabled_dir = root / "skills-disabled"
        self.synced_dir = self.skills_dir / "synced"
        for d in (self.skills_dir, self.disabled_dir, self.synced_dir):
            d.mkdir(parents=True)
        self._orig = (app.SKILLS_DIR, app.SKILLS_DISABLED_DIR)
        app.SKILLS_DIR, app.SKILLS_DISABLED_DIR = self.skills_dir, self.disabled_dir

    def tearDown(self):
        app.SKILLS_DIR, app.SKILLS_DISABLED_DIR = self._orig
        self.tmpdir.cleanup()

    def _skill(self, base, name, description=None):
        d = base / name
        d.mkdir(parents=True)
        fm = f"---\nname: {name}\n"
        if description is not None:
            fm += f'description: "{description}"\n'
        fm += "---\n\nCorps.\n"
        (d / "SKILL.md").write_text(fm)
        return d

    def test_synced_skills_are_listed(self):
        self._skill(self.synced_dir, "sync-a", "trop court")
        self._skill(self.synced_dir, "sync-b", self.GOOD)
        names = {i["name"] for i in app._list_synced_skills()}
        self.assertEqual(names, {"sync-a", "sync-b"})

    def test_synced_container_is_not_itself_a_skill(self):
        self._skill(self.synced_dir, "sync-a", "x")
        self._skill(self.skills_dir, "vrai", self.GOOD)
        # le conteneur n'a pas de SKILL.md et porte le nom reserve
        self.assertFalse((self.skills_dir / "synced" / "SKILL.md").exists())
        repairable = {s["name"] for s in app._repairable_user_skills()}
        self.assertNotIn("synced", repairable)

    def test_container_without_skill_md_is_never_repairable(self):
        """Un dossier sans SKILL.md est un conteneur, pas un skill casse."""
        (self.skills_dir / "un-dossier-quelconque").mkdir()
        self.assertEqual(app._repairable_user_skills(), [])

    def test_synced_excluded_from_bulk_by_default(self):
        self._skill(self.synced_dir, "sync-a", "trop court")
        self._skill(self.skills_dir, "local", "trop court")
        self.assertEqual([s["name"] for s in app._repairable_user_skills()], ["local"])

    def test_synced_included_on_explicit_opt_in(self):
        self._skill(self.synced_dir, "sync-a", "trop court")
        self._skill(self.skills_dir, "local", "trop court")
        got = {s["name"]: s["source"]
               for s in app._repairable_user_skills(include_synced=True)}
        self.assertEqual(got, {"local": "user", "sync-a": "synced"})

    def test_find_skill_dir_resolves_synced(self):
        d = self._skill(self.synced_dir, "sync-a", "x")
        self.assertEqual(app._find_skill_dir("sync-a"), d)

    def test_find_skill_dir_prefers_local_over_synced(self):
        local = self._skill(self.skills_dir, "dup", "local")
        self._skill(self.synced_dir, "dup", "synced")
        self.assertEqual(app._find_skill_dir("dup"), local)

    def test_find_skill_dir_rejects_traversal(self):
        for evil in ("../etc", "a/b", "..", ".hidden", ""):
            self.assertIsNone(app._find_skill_dir(evil), evil)


class AccentFoldingTests(unittest.TestCase):
    """v1.14.1 - une premiere version des marqueurs listait les formes
    accentuees completes ('a utiliser', 'declencheur'). Resultat : une
    description fraichement generee qui disait "À déclencher dès qu'on..."
    restait classee 'enrich'. Un skill tout juste repare restait rouge, ce
    qui donnait l'impression que la reparation ne marchait pas.
    """

    def _long(self, tail):
        return ("Diagnostique les echecs de deploiement sur cette plateforme, "
                "lit les logs, identifie la cause racine et propose un "
                "correctif concret. ") + tail

    def test_accented_trigger_is_recognised(self):
        self.assertEqual(app._skill_quality(self._long("À déclencher dès qu'un déploiement échoue.")),
                         "excellent")

    def test_unaccented_trigger_is_recognised(self):
        self.assertEqual(app._skill_quality(self._long("A declencher des qu'un deploiement echoue.")),
                         "excellent")

    def test_elided_forms_are_recognised(self):
        for tail in ("Dès qu'on mentionne Railway.", "Lorsqu'un build casse.",
                     "À utiliser pour toute erreur 502."):
            self.assertEqual(app._skill_quality(self._long(tail)), "excellent", tail)

    def test_fold_accents_is_stable(self):
        self.assertEqual(app._fold_accents("À déclencher"), "a declencher")
        self.assertEqual(app._fold_accents("ÉÈÊË"), "eeee")



class BulkRepairPreflightTests(unittest.TestCase):
    """v1.14.2 - un lot de 93 skills lance sur une machine dont le CLI ne
    repond pas enchainait 93 timeouts de 120 s : plus de trois heures a
    afficher "Reparation en cours 1/93" pour finir sur zero description
    reecrite, sans jamais nommer la cause.
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.skills_dir = root / "skills"
        self.disabled_dir = root / "skills-disabled"
        self.backup_dir = root / "backup"
        for d in (self.skills_dir, self.disabled_dir, self.backup_dir):
            d.mkdir(parents=True)
        self._orig = (app.SKILLS_DIR, app.SKILLS_DISABLED_DIR, app.BACKUP_DIR)
        app.SKILLS_DIR, app.SKILLS_DISABLED_DIR = self.skills_dir, self.disabled_dir
        app.BACKUP_DIR = self.backup_dir
        self._orig_notify = app._notify
        app._notify = lambda *a, **k: None
        app.dismiss_bulk_repair()

    def tearDown(self):
        app.SKILLS_DIR, app.SKILLS_DISABLED_DIR, app.BACKUP_DIR = self._orig
        app._notify = self._orig_notify
        app._BULK_REPAIR["running"] = False
        app.dismiss_bulk_repair()
        self.tmpdir.cleanup()

    def _skill(self, name, description="trop court"):
        d = self.skills_dir / name
        d.mkdir()
        (d / "SKILL.md").write_text(
            f'---\nname: {name}\ndescription: "{description}"\n---\n\nCorps.\n')
        return d

    def _targets(self, n):
        return [{"name": f"s{i}", "description": "trop court",
                 "quality": "enrich", "reason": "", "active": True,
                 "source": "user"} for i in range(n)]

    def test_probe_timeout_aborts_before_touching_any_skill(self):
        for i in range(5):
            self._skill(f"s{i}")
        with patch.object(app, "_call_claude_cli",
                          side_effect=app.ClaudeCliTimeout(90, "")):
            app._BULK_REPAIR.update({"running": True, "total": 5, "done": 0,
                                     "phase": "preflight"})
            app._bulk_repair_worker(self._targets(5), None)
        st = app.bulk_repair_status()
        self.assertEqual(st["outcome"], "aborted")
        self.assertEqual(st["done"], 0, "aucun skill ne doit avoir ete traite")
        self.assertIn("n'a pas répondu", st["abort_reason"])
        # les SKILL.md sont intacts
        body = (self.skills_dir / "s0" / "SKILL.md").read_text()
        self.assertIn('description: "trop court"', body)

    def test_probe_measures_pace_for_eta(self):
        self._skill("s0")
        with patch.object(app, "_call_claude_cli", return_value="pong"):
            ok, secs, err = app._bulk_repair_probe()
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertIsInstance(secs, float)

    def test_empty_probe_answer_is_a_failure(self):
        with patch.object(app, "_call_claude_cli", return_value="   "):
            ok, _secs, err = app._bulk_repair_probe()
        self.assertFalse(ok)
        self.assertIn("sans aucun texte", err)

    def test_stops_after_three_consecutive_failures(self):
        for i in range(20):
            self._skill(f"s{i}")
        calls = {"n": 0}

        def _suggest(name, lang=None):
            calls["n"] += 1
            return False, "Timeout : le CLI Claude n'a pas repondu en 120 s."

        with patch.object(app, "_call_claude_cli", return_value="pong"), \
             patch.object(app, "suggest_skill_description", side_effect=_suggest):
            app._BULK_REPAIR.update({"running": True, "total": 20, "done": 0,
                                     "phase": "preflight",
                                     "started_monotonic": time.monotonic()})
            app._bulk_repair_worker(self._targets(20), None)
        st = app.bulk_repair_status()
        self.assertEqual(st["outcome"], "aborted")
        self.assertEqual(calls["n"], 3, "on ne doit pas tenter les 20 skills")
        self.assertIn("échecs consécutifs", st["abort_reason"])

    def test_consecutive_counter_resets_on_success(self):
        seq = [False, False, True, False, False, True]
        it = iter(seq)

        def _suggest(name, lang=None):
            ok = next(it)
            return (True, {"suggestion": BulkRepairTests.GOOD}) if ok else (False, "boom")

        for i in range(6):
            self._skill(f"s{i}")
        with patch.object(app, "_call_claude_cli", return_value="pong"), \
             patch.object(app, "suggest_skill_description", side_effect=_suggest):
            app._BULK_REPAIR.update({"running": True, "total": 6, "done": 0,
                                     "phase": "preflight",
                                     "started_monotonic": time.monotonic()})
            app._bulk_repair_worker(self._targets(6), None)
        st = app.bulk_repair_status()
        self.assertEqual(st["done"], 6, "aucune serie n'atteint 3 echecs")
        self.assertEqual(st["outcome"], "partial")


class BulkRepairOutcomeTests(unittest.TestCase):
    """v1.14.2 - l'UI affichait "Reparation terminee {done}/{total}" quel que
    soit le resultat : {done} compte les skills TRAITES, pas les REPARES. Un
    lot 100% en echec et un lot 100% reussi produisaient le meme texte.
    """

    def tearDown(self):
        app._BULK_REPAIR["running"] = False
        app.dismiss_bulk_repair()

    def _state(self, results, **kw):
        app._BULK_REPAIR.update({
            "running": False, "total": kw.get("total", len(results)),
            "done": len(results), "results": results, "phase": "finished",
            "cancelled": kw.get("cancelled", False),
            "aborted": kw.get("aborted", False),
            "abort_reason": kw.get("abort_reason"),
            "started_monotonic": kw.get("started_monotonic"),
            "probe_seconds": kw.get("probe_seconds"),
        })
        return app.bulk_repair_status()

    def test_all_failed_is_not_reported_as_finished(self):
        st = self._state([{"name": f"s{i}", "ok": False, "error": "timeout"}
                          for i in range(93)])
        self.assertEqual(st["outcome"], "all_failed")
        self.assertEqual(st["ok_count"], 0)
        self.assertEqual(st["failed_count"], 93)

    def test_partial_reports_both_counts(self):
        res = [{"name": "a", "ok": True, "after": "x"},
               {"name": "b", "ok": False, "error": "timeout"}]
        st = self._state(res)
        self.assertEqual(st["outcome"], "partial")
        self.assertEqual((st["ok_count"], st["failed_count"]), (1, 1))

    def test_success_outcome(self):
        st = self._state([{"name": "a", "ok": True, "after": "x"}])
        self.assertEqual(st["outcome"], "success")

    def test_cancelled_is_distinct_from_finished(self):
        st = self._state([{"name": "a", "ok": True, "after": "x"}],
                         total=93, cancelled=True)
        self.assertEqual(st["outcome"], "cancelled")
        self.assertEqual(st["remaining"], 92)

    def test_aborted_wins_over_cancelled(self):
        st = self._state([{"name": "a", "ok": False, "error": "x"}],
                         total=93, cancelled=True, aborted=True,
                         abort_reason="3 echecs consecutifs")
        self.assertEqual(st["outcome"], "aborted")

    def test_idle_when_nothing_ran(self):
        app.dismiss_bulk_repair()
        st = app.bulk_repair_status()
        self.assertEqual(st["outcome"], "idle")

    def test_eta_uses_measured_pace(self):
        app._BULK_REPAIR.update({
            "running": True, "total": 10, "done": 2, "phase": "running",
            "results": [{"name": "a", "ok": True}, {"name": "b", "ok": True}],
            "started_monotonic": time.monotonic() - 20, "cancelled": False,
            "aborted": False, "abort_reason": None, "probe_seconds": 5.0,
        })
        st = app.bulk_repair_status()
        self.assertEqual(st["outcome"], "running")
        self.assertAlmostEqual(st["seconds_per_skill"], 10.0, delta=1.5)
        # 8 restants x ~10 s
        self.assertAlmostEqual(st["eta_seconds"], 80, delta=15)

    def test_dismiss_refuses_while_running(self):
        app._BULK_REPAIR.update({"running": True})
        ok, msg = app.dismiss_bulk_repair()
        self.assertFalse(ok)
        app._BULK_REPAIR["running"] = False

    def test_dismiss_clears_report(self):
        self._state([{"name": "a", "ok": True, "after": "x"}])
        ok, _ = app.dismiss_bulk_repair()
        self.assertTrue(ok)
        self.assertEqual(app.bulk_repair_status()["outcome"], "idle")

if __name__ == "__main__":
    unittest.main()
