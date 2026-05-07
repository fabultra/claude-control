"""v1.9.0 - Tests pour la feature 'Reparer un skill' :
- _update_skill_frontmatter (parse + update + preserve)
- repair_skill (backup + ecriture frontmatter + creation si absent)
- suggest_skill_description (mock urlopen pour ne jamais appeler la
  vraie API Anthropic dans les tests)
"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


class FrontmatterUpdateTests(unittest.TestCase):
    def test_update_existing_description(self):
        content = "---\nname: foo\ndescription: old\n---\n\nbody here\n"
        out = app._update_skill_frontmatter(content, {"description": "new desc"})
        self.assertIn('description: "new desc"', out)
        self.assertNotIn("description: old", out)
        self.assertIn("body here", out)
        self.assertIn("name: foo", out)

    def test_add_description_when_missing(self):
        content = "---\nname: foo\n---\n\nbody\n"
        out = app._update_skill_frontmatter(content, {"description": "added"})
        self.assertIn('description: "added"', out)
        self.assertIn("name: foo", out)
        self.assertIn("body", out)

    def test_create_frontmatter_when_absent(self):
        content = "Just some markdown body, no frontmatter at all.\n"
        out = app._update_skill_frontmatter(content, {"name": "myskill", "description": "hello"})
        self.assertTrue(out.startswith("---\n"))
        self.assertIn('name: "myskill"', out)
        self.assertIn('description: "hello"', out)
        self.assertIn("Just some markdown body", out)

    def test_quoting_handles_special_chars(self):
        out = app._update_skill_frontmatter("---\nname: x\n---\nbody\n",
                                             {"description": 'has "quotes" and: colons'})
        # json.dumps escapes properly
        self.assertIn('description: "has \\"quotes\\" and: colons"', out)

    def test_preserves_other_keys(self):
        content = "---\nname: foo\ncategory: utils\ntags: [a, b]\n---\nbody\n"
        out = app._update_skill_frontmatter(content, {"description": "added"})
        self.assertIn("category: utils", out)
        self.assertIn("tags: [a, b]", out)
        self.assertIn('description: "added"', out)


class RepairSkillTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.skills_dir = root / "skills"
        self.disabled_dir = root / "skills-disabled"
        self.backup_dir = root / "backup"
        self.skills_dir.mkdir(parents=True)
        self.disabled_dir.mkdir(parents=True)
        self.backup_dir.mkdir(parents=True)
        self._orig = (app.SKILLS_DIR, app.SKILLS_DISABLED_DIR, app.BACKUP_DIR)
        app.SKILLS_DIR = self.skills_dir
        app.SKILLS_DISABLED_DIR = self.disabled_dir
        app.BACKUP_DIR = self.backup_dir

    def tearDown(self):
        app.SKILLS_DIR, app.SKILLS_DISABLED_DIR, app.BACKUP_DIR = self._orig
        self.tmpdir.cleanup()

    def _write_skill(self, name, body="---\nname: x\n---\nfoo body\n", base=None):
        base = base or self.skills_dir
        d = base / name
        d.mkdir()
        (d / "SKILL.md").write_text(body)
        return d

    def test_repair_adds_description_to_frontmatter(self):
        d = self._write_skill("broken-skill")
        ok, msg = app.repair_skill("broken-skill", description="A clear description")
        self.assertTrue(ok, msg)
        content = (d / "SKILL.md").read_text()
        self.assertIn('description: "A clear description"', content)
        # Backup zip cree
        zips = list(self.backup_dir.glob("repaired-skill-broken-skill-*.zip"))
        self.assertEqual(len(zips), 1)

    def test_repair_works_on_disabled_skill(self):
        d = self._write_skill("disabled-skill", base=self.disabled_dir)
        ok, _ = app.repair_skill("disabled-skill", description="hello")
        self.assertTrue(ok)
        self.assertIn('description: "hello"', (d / "SKILL.md").read_text())

    def test_repair_creates_frontmatter_when_absent(self):
        d = self._write_skill("no-fm", body="just body, no frontmatter\n")
        ok, _ = app.repair_skill("no-fm", description="now has desc")
        self.assertTrue(ok)
        content = (d / "SKILL.md").read_text()
        self.assertTrue(content.startswith("---\n"))
        self.assertIn('description: "now has desc"', content)
        self.assertIn("just body", content)

    def test_repair_rejects_invalid_name(self):
        for evil in ("", None, "../etc", "foo/bar", ".hidden"):
            ok, _ = app.repair_skill(evil, description="x")
            self.assertFalse(ok, f"should reject: {evil!r}")

    def test_repair_rejects_empty_description(self):
        self._write_skill("foo")
        ok, _ = app.repair_skill("foo", description="")
        self.assertFalse(ok)
        ok, _ = app.repair_skill("foo", description="   ")
        self.assertFalse(ok)


class SuggestSkillDescriptionTests(unittest.TestCase):
    """Mock urllib.request.urlopen pour ne jamais appeler la vraie API
    Anthropic dans les tests."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.skills_dir = root / "skills"
        self.disabled_dir = root / "skills-disabled"
        self.skills_dir.mkdir(parents=True)
        self.disabled_dir.mkdir(parents=True)
        self._orig = (app.SKILLS_DIR, app.SKILLS_DISABLED_DIR)
        app.SKILLS_DIR = self.skills_dir
        app.SKILLS_DISABLED_DIR = self.disabled_dir
        d = self.skills_dir / "demo"
        d.mkdir()
        (d / "SKILL.md").write_text("---\nname: demo\n---\n\nThis skill processes invoices.\n")

    def tearDown(self):
        app.SKILLS_DIR, app.SKILLS_DISABLED_DIR = self._orig
        self.tmpdir.cleanup()

    def test_returns_error_when_no_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(app, "HOME", Path(self.tmpdir.name) / "fake_home"):
                ok, msg = app.suggest_skill_description("demo")
                self.assertFalse(ok)
                self.assertIn("ANTHROPIC_API_KEY", msg)

    def test_calls_anthropic_api_with_skill_content(self):
        fake_response = json.dumps({
            "content": [{"type": "text", "text": "Use this skill when handling invoice processing."}]
        }).encode("utf-8")
        captured = {}

        class FakeResp:
            def __enter__(self_): return self_
            def __exit__(self_, *a): pass
            def read(self_): return fake_response

        def fake_urlopen(req, timeout=20):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.headers)
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return FakeResp()

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            with patch.object(urllib.request, "urlopen", fake_urlopen):
                ok, payload = app.suggest_skill_description("demo")

        self.assertTrue(ok, payload)
        self.assertEqual(payload["suggestion"],
                         "Use this skill when handling invoice processing.")
        self.assertEqual(captured["url"], "https://api.anthropic.com/v1/messages")
        # Header keys can be lowercased in some Python versions; case-insensitive lookup
        headers_lower = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertEqual(headers_lower.get("x-api-key"), "sk-test-key")
        self.assertIn("invoices", captured["body"]["messages"][0]["content"])

    def test_returns_error_for_unknown_skill(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            ok, msg = app.suggest_skill_description("does-not-exist")
            self.assertFalse(ok)
            self.assertIn("introuvable", msg)

    def _stub_api(self, response_text):
        """v1.9.2 - helper pour stubber la reponse API et tester le
        sanitizer."""
        fake = json.dumps({"content": [{"type": "text", "text": response_text}]}).encode("utf-8")
        class FakeResp:
            def __enter__(s): return s
            def __exit__(s, *a): pass
            def read(s): return fake
        def fake_urlopen(req, timeout=20):
            return FakeResp()
        return patch.object(urllib.request, "urlopen", fake_urlopen)

    def test_sanitizes_markdown_header_prefix(self):
        """Bug observe v1.9.0/1.9.1 : Haiku retourne parfois '## Use this
        skill when...' au lieu du texte brut. Le sanitizer doit nettoyer."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-x"}):
            with self._stub_api("## Use this skill when handling invoices"):
                ok, payload = app.suggest_skill_description("demo")
        self.assertTrue(ok, payload)
        self.assertEqual(payload["suggestion"], "Use this skill when handling invoices")

    def test_sanitizes_description_prefix(self):
        """Sanitizer strip 'Description:', 'Here is the description:', etc."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-x"}):
            with self._stub_api("Description: Use this skill for X"):
                ok, payload = app.suggest_skill_description("demo")
        self.assertTrue(ok)
        self.assertEqual(payload["suggestion"], "Use this skill for X")

    def test_sanitizes_quotes_and_backticks(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-x"}):
            with self._stub_api('"Use this skill for Y"'):
                ok, payload = app.suggest_skill_description("demo")
        self.assertTrue(ok)
        self.assertEqual(payload["suggestion"], "Use this skill for Y")

    def test_sanitizes_multiline_keeps_first_meaningful(self):
        """Si la reponse contient plusieurs lignes (preamble + description),
        on garde la 1ere ligne non-vide apres sanitization."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-x"}):
            with self._stub_api("Sure! Here's the description:\n\nUse this skill for Z\n\nHope this helps."):
                ok, payload = app.suggest_skill_description("demo")
        self.assertTrue(ok)
        # Apres strip prefix 'Sure!' (pas dans la liste mais multiline -> 1ere ligne)
        # Le 1er token est 'Sure! Here's the description:' qui matche le prefix
        # Ensuite split lines, 1ere non-vide = 'Use this skill for Z'
        self.assertIn("Use this skill for Z", payload["suggestion"])

    def test_returns_error_when_api_returns_empty(self):
        """Cas observe par utilisateur : API retourne quelque chose qui se
        sanitize en vide (juste des caracteres markdown). Le backend doit
        retourner une erreur claire avec le raw response."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-x"}):
            with self._stub_api("###"):
                ok, msg = app.suggest_skill_description("demo")
        self.assertFalse(ok)
        self.assertIn("vide", msg)


if __name__ == "__main__":
    unittest.main()
