"""v1.15.0 - Tests : l'installeur ne peut plus executer un script tronque.

Constat d'audit : landing/install.sh (servi par claude.sekoia.ca) faisait
`exec /bin/bash <(curl ...)` -- du STREAMING. Une coupure reseau en plein
transfert faisait executer par bash un prefixe arbitraire du script. Deux
verrous desormais, tous deux prouves ici en EXECUTANT les scripts :

1. Telecharger-verifier-executer : le wrapper ecrit dans un fichier
   temporaire, exige le succes de curl ET la presence du marqueur de fin
   (# claude-control-install-end), et seulement ensuite lance bash.
2. Corps dans main() appele en derniere ligne, pour les DEUX scripts : un
   fichier coupe n'importe ou ne contient au niveau module que des
   definitions -- bash ne peut rien executer d'un prefixe.

La propriete 2 est verifiee par force brute : chaque script est tronque a
CHAQUE frontiere de ligne puis execute avec un HOME jetable et un PATH
vide ; aucun de ces prefixes ne doit toucher le HOME.
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CANONICAL = REPO / "scripts/install.sh"
WRAPPER = REPO / "landing/install.sh"
BASH = shutil.which("bash") or "/bin/bash"
MARKER = "claude-control-install-end"


def _run_truncated(script_text, cut_line, home):
    """Execute les `cut_line` premieres lignes du script, PATH vide."""
    lines = script_text.splitlines(keepends=True)
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write("".join(lines[:cut_line]))
        partial = f.name
    try:
        return subprocess.run(
            [BASH, partial], capture_output=True, text=True, timeout=20,
            env={"HOME": str(home), "PATH": ""})
    finally:
        os.unlink(partial)


class StaticShapeTests(unittest.TestCase):
    def test_both_scripts_are_valid_bash(self):
        for script in (CANONICAL, WRAPPER):
            with self.subTest(script=script.name):
                r = subprocess.run([BASH, "-n", str(script)],
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_wrapper_no_longer_streams_into_bash(self):
        text = WRAPPER.read_text()
        code = "\n".join(l for l in text.splitlines()
                         if not l.lstrip().startswith("#"))
        self.assertNotIn("<(", code)  # plus de process substitution
        self.assertIn("mktemp", code)
        # La verification du marqueur PRECEDE l'execution.
        self.assertLess(text.index(MARKER), text.index('bash "$tmp"'))

    def test_canonical_ends_with_the_marker(self):
        lines = [l for l in CANONICAL.read_text().splitlines() if l.strip()]
        self.assertEqual(lines[-1], f"# {MARKER}")
        self.assertEqual(lines[-2], 'main "$@"')

    def test_no_side_effect_at_module_level(self):
        """Au niveau colonne 0 : uniquement des definitions, set -e,
        commentaires et l'appel final -- jamais rm/git/curl/mkdir."""
        for script in (CANONICAL, WRAPPER):
            for line in script.read_text().splitlines():
                if not line or line.startswith((" ", "\t", "#")):
                    continue
                with self.subTest(script=script.name, line=line):
                    self.assertNotRegex(
                        line, r"^(rm|git|curl|mkdir|bash|exec)\b")


class TruncationPropertyTests(unittest.TestCase):
    """Chaque prefixe possible du script doit etre inerte."""

    def _assert_all_prefixes_inert(self, script):
        text = script.read_text()
        n = len(text.splitlines())
        home = Path(tempfile.mkdtemp())
        try:
            for cut in range(1, n):
                r = _run_truncated(text, cut, home)
                with self.subTest(script=script.name, cut=cut):
                    self.assertEqual(
                        os.listdir(home), [],
                        f"le prefixe {cut}/{n} a ecrit dans HOME "
                        f"(rc={r.returncode}, stderr={r.stderr[:200]})")
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_every_prefix_of_the_canonical_installer_is_inert(self):
        self._assert_all_prefixes_inert(CANONICAL)

    def test_every_prefix_of_the_wrapper_is_inert(self):
        self._assert_all_prefixes_inert(WRAPPER)

    def test_a_cut_inside_main_is_a_parse_error(self):
        text = CANONICAL.read_text()
        n = len(text.splitlines())
        r = _run_truncated(text, n // 2, Path(tempfile.mkdtemp()))
        self.assertNotEqual(r.returncode, 0)


class DownloadVerifyExecuteTests(unittest.TestCase):
    """Le wrapper, execute pour de vrai avec un faux curl."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        fake_curl = self.bin / "curl"
        fake_curl.write_text(
            '#!/bin/bash\nout=""; prev=""\n'
            'for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done\n'
            'cat "$PAYLOAD" > "$out"\n')
        fake_curl.chmod(fake_curl.stat().st_mode | stat.S_IEXEC)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_wrapper(self, payload_text):
        payload = self.tmp / "payload.sh"
        payload.write_text(payload_text)
        env = {"HOME": str(self.home),
               "PATH": f"{self.bin}:/usr/bin:/bin",
               "TMPDIR": str(self.tmp),
               "PAYLOAD": str(payload)}
        return subprocess.run([BASH, str(WRAPPER)], env=env,
                              capture_output=True, text=True, timeout=20)

    def test_truncated_download_is_refused_before_any_execution(self):
        r = self._run_wrapper(
            "#!/bin/bash\ntouch \"$HOME/pwned\"\n")  # pas de marqueur
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("incomplet", r.stderr)
        self.assertFalse((self.home / "pwned").exists())

    def test_complete_download_runs_exactly_once(self):
        r = self._run_wrapper(
            "#!/bin/bash\ntouch \"$HOME/ran\"\n"
            f"# {MARKER}\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.home / "ran").exists())

    def test_failed_curl_stops_everything(self):
        (self.bin / "curl").write_text("#!/bin/bash\nexit 22\n")
        r = self._run_wrapper("ignore")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("telechargement", r.stderr)


if __name__ == "__main__":
    unittest.main()
