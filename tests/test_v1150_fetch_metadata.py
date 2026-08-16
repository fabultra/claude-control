"""v1.15.0 - Tests : Fetch Metadata ferme le trou des GET sans Origin, et
GET /api/state n'ecrit plus rien sur le disque.

Deux suites du meme point d'audit :

1. La garde v1.14.0 (Host + Origin + Content-Type) laissait passer les GET
   "simples" declenches par une page tierce (<img src="http://localhost:
   8765/...">) : pas d'Origin envoye, garde inoperante. Les navigateurs
   modernes annoncent la provenance de CHAQUE requete via Sec-Fetch-Site ;
   une requete declaree cross-site est refusee, a l'exception de la
   navigation haut niveau (GET + navigate + document : cliquer un lien vers
   l'app doit continuer de marcher). Un iframe cross-site reste refuse.
   Header absent (curl, vieux navigateur) : rien d'exige.

2. _compute_state faisait un SKILLS_DIR.mkdir : un simple GET /api/state
   creait des dossiers. Un GET est desormais sans effet de bord -- les
   dossiers sont crees par main() et par les routes POST qui en ont besoin.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402

GOOD_HOST = {"Host": f"localhost:{app.PORT}"}


def _guard(headers, command="GET"):
    fake = app.Handler.__new__(app.Handler)
    fake.headers = dict(headers)
    fake.command = command
    return app.Handler._guard_request(fake)


class FetchMetadataTests(unittest.TestCase):
    def test_cross_site_image_get_is_refused(self):
        """Le vecteur documente : <img src=...> ne porte pas d'Origin."""
        ok, why = _guard({**GOOD_HOST, "Sec-Fetch-Site": "cross-site",
                          "Sec-Fetch-Mode": "no-cors",
                          "Sec-Fetch-Dest": "image"})
        self.assertFalse(ok)
        self.assertIn("cross-site", why)

    def test_cross_site_post_is_refused_even_as_navigation(self):
        """Soumission de formulaire cross-site : mode navigate mais POST."""
        ok, _ = _guard({**GOOD_HOST, "Sec-Fetch-Site": "cross-site",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Dest": "document"}, command="POST")
        self.assertFalse(ok)

    def test_clicking_a_link_to_the_app_still_works(self):
        ok, _ = _guard({**GOOD_HOST, "Sec-Fetch-Site": "cross-site",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Dest": "document"})
        self.assertTrue(ok)

    def test_cross_site_iframe_embedding_is_refused(self):
        ok, _ = _guard({**GOOD_HOST, "Sec-Fetch-Site": "cross-site",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Dest": "iframe"})
        self.assertFalse(ok)

    def test_same_origin_and_direct_navigation_pass(self):
        for site in ("same-origin", "same-site", "none"):
            with self.subTest(site=site):
                ok, _ = _guard({**GOOD_HOST, "Sec-Fetch-Site": site,
                                "Sec-Fetch-Mode": "no-cors"})
                self.assertTrue(ok)

    def test_absent_header_requires_nothing(self):
        """curl et les clients hors navigateur restent servis."""
        ok, _ = _guard(GOOD_HOST)
        self.assertTrue(ok)
        ok, _ = _guard(GOOD_HOST, command="POST")
        self.assertTrue(ok)

    def test_existing_locks_still_hold(self):
        self.assertFalse(_guard({"Host": "evil.com"})[0])
        self.assertFalse(_guard({**GOOD_HOST,
                                 "Origin": "https://evil.com"})[0])


class StateGetHasNoSideEffectTests(unittest.TestCase):
    def test_compute_state_does_not_create_directories(self):
        tmp = Path(tempfile.mkdtemp())
        skills = tmp / "skills"
        disabled = tmp / "skills-disabled"
        with patch.object(app, "HOME", tmp), \
                patch.object(app, "CONFIG_PATH", tmp / "cfg.json"), \
                patch.object(app, "SKILLS_DIR", skills), \
                patch.object(app, "SKILLS_DISABLED_DIR", disabled), \
                patch.object(app, "get_running_mcps", lambda c: {}), \
                patch.object(app, "get_skill_usage", lambda **kw: {}):
            state = app._compute_state()
        self.assertFalse(skills.exists(), "GET /api/state a cree skills/")
        self.assertFalse(disabled.exists(),
                         "GET /api/state a cree skills-disabled/")
        self.assertEqual(state["skills"], [])

    def test_no_mkdir_left_in_compute_state(self):
        import inspect
        self.assertNotIn(".mkdir(", inspect.getsource(app._compute_state))

    def test_main_now_creates_skills_dir(self):
        import inspect
        self.assertIn("SKILLS_DIR.mkdir", inspect.getsource(app.main))


if __name__ == "__main__":
    unittest.main()
