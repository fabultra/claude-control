"""v1.14.17 - Tests : la tempete de demarrage ne fait plus flasher d'erreurs.

Symptome utilisateur : "des messages d'erreur qui apparaissent furtivement
lorsque je recharge l'app". Cause : la page de boot tire ~12 requetes d'un
coup, et le serveur refaisait le meme travail lourd pour chacune --
/api/overview refaisait le get_state() complet de /api/state, chaque
get_state relisait les meta de TOUS les skills, et surtout re-parcourait
sans TTL l'arborescence Desktop (<session>/<task>/skills/...) qui grossit a
chaque session Claude Desktop et n'est jamais purgee. La premiere salve
depassait le timeout du fetch -> bandeau rouge + toast, puis le poll suivant
reussissait -> l'alerte s'evaporait. Alarme sans objet, a chaque F5.

Quatre coupes, c'etait le point R1 du backlog d'audit :
1. get_state() : cache 2,5 s single-flight, invalide par toute route POST
   qui change l'etat (le loadState apres un toggle reste frais).
2. read_skill_meta : cache par mtime (une reparation qui reecrit le fichier
   invalide naturellement).
3. _list_desktop_skills : TTL 2 min sur le dernier rglob non borne polle.
4. UI : loadState reessaie UNE fois en silence avant le bandeau rouge, et
   efface l'alerte des que le serveur repond a nouveau.
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


class StateCacheTests(unittest.TestCase):
    def setUp(self):
        self._ttl = app._STATE_TTL
        app._STATE_TTL = 60.0
        app._invalidate_state_cache()
        self.computes = []
        self._orig = app._compute_state
        app._compute_state = lambda: (self.computes.append(1)
                                      or {"mcps": [], "stamp": len(self.computes)})

    def tearDown(self):
        app._compute_state = self._orig
        app._STATE_TTL = self._ttl
        app._invalidate_state_cache()

    def test_a_burst_computes_once(self):
        first = app.get_state()
        second = app.get_state()
        self.assertEqual(len(self.computes), 1)
        self.assertIs(first, second)

    def test_invalidation_forces_a_fresh_compute(self):
        app.get_state()
        app._invalidate_state_cache()
        fresh = app.get_state()
        self.assertEqual(len(self.computes), 2)
        self.assertEqual(fresh["stamp"], 2)

    def test_ttl_zero_disables_the_cache(self):
        """La garantie sur laquelle tests/__init__.py s'appuie."""
        app._STATE_TTL = 0.0
        app.get_state()
        app.get_state()
        self.assertEqual(len(self.computes), 2)


class SkillMetaCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.skill = self.tmp / "demo"
        self.skill.mkdir()
        with app._SKILL_META_LOCK:
            app._SKILL_META_CACHE.clear()

    def tearDown(self):
        with app._SKILL_META_LOCK:
            app._SKILL_META_CACHE.clear()

    def _write(self, description):
        (self.skill / "SKILL.md").write_text(
            f"---\nname: demo\ndescription: {description}\n---\ncorps")

    def test_first_read_populates_the_cache(self):
        self._write("premiere")
        meta = app.read_skill_meta(self.skill)
        self.assertEqual(meta["description"], "premiere")
        self.assertIn(str(self.skill / "SKILL.md"), app._SKILL_META_CACHE)

    def test_same_mtime_is_served_from_cache(self):
        self._write("premiere")
        app.read_skill_meta(self.skill)
        # On corrompt la copie cachee du parseur : si la seconde lecture
        # repassait par le fichier, elle ne verrait pas cette valeur.
        key = str(self.skill / "SKILL.md")
        mtime, cached = app._SKILL_META_CACHE[key]
        app._SKILL_META_CACHE[key] = (mtime, dict(cached, description="cachee"))
        self.assertEqual(app.read_skill_meta(self.skill)["description"],
                         "cachee")

    def test_rewrite_invalidates_naturally(self):
        """Le cas reparation : le fichier reecrit doit etre relu."""
        self._write("avant")
        app.read_skill_meta(self.skill)
        time.sleep(0.01)  # mtime_ns doit bouger meme sur fs a faible resolution
        self._write("apres reparation")
        self.assertEqual(app.read_skill_meta(self.skill)["description"],
                         "apres reparation")

    def test_cached_tags_are_not_shared_mutable_state(self):
        (self.skill / "SKILL.md").write_text(
            "---\nname: demo\ntags: [a, b]\n---\ncorps")
        first = app.read_skill_meta(self.skill)
        first["tags"].append("mutation-appelant")
        self.assertEqual(app.read_skill_meta(self.skill)["tags"], ["a", "b"])

    def test_missing_file_is_not_cached(self):
        empty = self.tmp / "sans-md"
        empty.mkdir()
        meta = app.read_skill_meta(empty)
        self.assertIsNone(meta["description"])
        self.assertNotIn(str(empty / "SKILL.md"), app._SKILL_META_CACHE)


class DesktopSkillsTtlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._home = app.HOME
        app.HOME = self.tmp
        with app._DESKTOP_SKILLS_LOCK:
            app._DESKTOP_SKILLS_CACHE.update({"ts": 0.0, "data": None})
        self.base = (self.tmp / "Library/Application Support/Claude/"
                     "local-agent-mode-sessions/skills-plugin")

    def tearDown(self):
        app.HOME = self._home
        with app._DESKTOP_SKILLS_LOCK:
            app._DESKTOP_SKILLS_CACHE.update({"ts": 0.0, "data": None})

    def _add(self, session, name):
        d = self.base / session / "task-1/skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\ncorps")

    def test_within_ttl_the_tree_is_not_rescanned(self):
        self._add("s1", "alpha")
        self.assertEqual([i["name"] for i in app._list_desktop_skills()],
                         ["alpha"])
        self._add("s2", "beta")  # nouveau skill, mais TTL encore chaud
        self.assertEqual([i["name"] for i in app._list_desktop_skills()],
                         ["alpha"])

    def test_expired_ttl_rescans(self):
        self._add("s1", "alpha")
        app._list_desktop_skills()
        self._add("s2", "beta")
        with app._DESKTOP_SKILLS_LOCK:
            app._DESKTOP_SKILLS_CACHE["ts"] = 0.0
        self.assertEqual(sorted(i["name"] for i in app._list_desktop_skills()),
                         ["alpha", "beta"])

    def test_missing_base_dir_is_cached_too(self):
        self.assertEqual(app._list_desktop_skills(), [])
        with app._DESKTOP_SKILLS_LOCK:
            self.assertEqual(app._DESKTOP_SKILLS_CACHE["data"], [])


class BootUiTests(unittest.TestCase):
    def test_loadstate_retries_once_before_alarming(self):
        self.assertIn("STATE_ERR_SHOWN", app.HTML)
        self.assertIn("setTimeout(r, 1500)", app.HTML)

    def test_recovery_clears_the_alert(self):
        # Le bloc de retablissement remet le bandeau cache.
        self.assertIn("STATE_ERR_SHOWN = false", app.HTML)

    def test_mutating_posts_invalidate_the_state_cache(self):
        import inspect
        src = inspect.getsource(app.Handler.do_POST)
        self.assertIn("_invalidate_state_cache", src)
        self.assertIn("_STATE_AFFECTING_ROUTES", src)


if __name__ == "__main__":
    unittest.main()
