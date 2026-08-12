"""v1.14.10 - Tests : une erreur qu'on ne peut pas lire n'a pas ete affichee.

Symptome rapporte : "quand je recharge l'app, il y a des messages d'erreurs
que je n'arrive pas a lire".

Trois causes cumulees :

- banner() masquait apres 4,5 s et toast() apres 7 s, quelle que soit la
  gravite ;
- il n'y a qu'un seul element #banner, donc deux messages successifs
  s'ecrasaient ;
- les erreurs de sondage et les exceptions non rattrapees n'allaient que
  dans la console, invisibles pour qui n'ouvre pas les devtools.

Au rechargement, plusieurs sondages partent d'un coup : leurs erreurs
defilaient sans qu'aucune reste lisible.
"""
import re
import sys
import unittest
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "src" / "app.py").read_text()


class RedMessagesPersistTests(unittest.TestCase):
    def test_banner_does_not_auto_hide_red(self):
        body = SRC[SRC.index("function banner(c,m){"):]
        body = body[:body.index("\nfunction ")]
        self.assertIn("if(c!=='red')", body,
                      "le masquage automatique doit epargner les erreurs")

    def test_toast_ttl_is_zero_for_red(self):
        self.assertIn("const ttl = c === 'red' ? 0 :", SRC)

    def test_toast_only_schedules_removal_when_ttl_is_set(self):
        self.assertIn("if(ttl) setTimeout(", SRC)

    def test_non_red_messages_still_auto_hide(self):
        """Les succes ne doivent pas s'accumuler a l'ecran."""
        self.assertIn("setTimeout(()=>b.classList.add('hidden'),4500)", SRC)


class ErrorJournalTests(unittest.TestCase):
    def test_red_toasts_are_archived(self):
        self.assertIn("if(c === 'red') errLogPush(m)", SRC)

    def test_journal_has_a_copy_button(self):
        self.assertIn("copyErrLog()", SRC)
        self.assertIn("navigator.clipboard.writeText(errLogText())", SRC)

    def test_repeated_messages_are_counted_not_stacked(self):
        """Un sondage qui echoue toutes les 5 s noierait les autres."""
        self.assertIn("last.n = (last.n || 1) + 1", SRC)

    def test_journal_is_bounded(self):
        self.assertIn("if(ERR_LOG.length > 50) ERR_LOG.shift()", SRC)

    def test_button_hidden_while_there_is_nothing(self):
        self.assertIn("btn.classList.toggle('hidden', ERR_LOG.length === 0)", SRC)


class SilentErrorsAreCapturedTests(unittest.TestCase):
    def test_poll_failures_reach_the_journal(self):
        self.assertIn("errLogPush((label||'poll')", SRC)

    def test_uncaught_errors_reach_the_journal(self):
        self.assertIn("window.addEventListener('error'", SRC)

    def test_rejected_promises_reach_the_journal(self):
        self.assertIn("window.addEventListener('unhandledrejection'", SRC)

    def test_capture_never_throws_on_its_own(self):
        """Un journal qui plante en enregistrant une erreur masquerait
        justement celle qu'on cherche."""
        for handler in ("window.addEventListener('error'",
                        "window.addEventListener('unhandledrejection'"):
            block = SRC[SRC.index(handler):]
            block = block[:block.index("});")]
            self.assertIn("try{", block)
            self.assertIn("}catch(_){}", block)


class TranslationParityTests(unittest.TestCase):
    KEYS = ("errlog_btn", "errlog_title", "errlog_clear")

    def test_keys_exist_in_both_languages(self):
        for key in self.KEYS:
            self.assertEqual(SRC.count("  " + key + ": "), 2, key)

    def test_count_placeholder_is_substituted(self):
        self.assertIn("tr('errlog_btn').replace('{n}'", SRC)


if __name__ == "__main__":
    unittest.main()
