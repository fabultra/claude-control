"""v1.9.0 / v1.9.3 - Tests pour la feature 'Reparer un skill' :
- _update_skill_frontmatter (parse + update + preserve)
- repair_skill (backup + ecriture frontmatter + creation si absent)
- suggest_skill_description (v1.9.3 : mock _call_claude_cli au lieu
  d'urlopen, le subprocess CLI remplace l'API HTTP)
"""
import os
import sys
import tempfile
import unittest
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
    """v1.9.3 - Mock _call_claude_cli (pas urlopen). Le CLI est un
    subprocess externe, on patch directement la fonction qui l'appelle."""

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

    def test_returns_error_when_cli_missing(self):
        """Si Claude Code CLI n'est pas dans le PATH, on retourne une erreur
        claire avec instruction d'installation."""
        with patch.object(app, "_claude_cli_path", lambda: None):
            ok, msg = app.suggest_skill_description("demo")
        self.assertFalse(ok)
        self.assertIn("Claude Code CLI", msg)

    def test_calls_cli_with_skill_content(self):
        """Le prompt envoye au CLI doit contenir le system prompt + le nom
        du skill + le content du SKILL.md."""
        captured = {}

        def fake_call(prompt, timeout=60):
            captured["prompt"] = prompt
            return "Use this skill when handling invoice processing."

        with patch.object(app, "_claude_cli_path", lambda: "/usr/local/bin/claude"):
            with patch.object(app, "_call_claude_cli", fake_call):
                ok, payload = app.suggest_skill_description("demo")

        self.assertTrue(ok, payload)
        self.assertEqual(payload["suggestion"],
                         "Use this skill when handling invoice processing.")
        self.assertEqual(payload["source"], "claude_cli")
        # Verifie que le system prompt + content sont dans le prompt envoye
        self.assertIn("descriptions for Claude Code skills", captured["prompt"])
        self.assertIn("invoices", captured["prompt"])
        self.assertIn("Skill name (folder): demo", captured["prompt"])

    def test_returns_error_for_unknown_skill(self):
        with patch.object(app, "_claude_cli_path", lambda: "/usr/local/bin/claude"):
            ok, msg = app.suggest_skill_description("does-not-exist")
            self.assertFalse(ok)
            self.assertIn("introuvable", msg)

    def _stub_cli(self, response_text):
        """v1.9.3 - helper pour stubber le CLI et tester le sanitizer."""
        return patch.object(app, "_call_claude_cli", lambda prompt, timeout=60: response_text)

    def test_sanitizes_markdown_header_prefix(self):
        """Bug observe v1.9.0/1.9.1 : le LLM retourne parfois '## Use this
        skill when...' au lieu du texte brut. Le sanitizer doit nettoyer."""
        with patch.object(app, "_claude_cli_path", lambda: "/usr/local/bin/claude"):
            with self._stub_cli("## Use this skill when handling invoices"):
                ok, payload = app.suggest_skill_description("demo")
        self.assertTrue(ok, payload)
        self.assertEqual(payload["suggestion"], "Use this skill when handling invoices")

    def test_sanitizes_description_prefix(self):
        with patch.object(app, "_claude_cli_path", lambda: "/usr/local/bin/claude"):
            with self._stub_cli("Description: Use this skill for X"):
                ok, payload = app.suggest_skill_description("demo")
        self.assertTrue(ok)
        self.assertEqual(payload["suggestion"], "Use this skill for X")

    def test_sanitizes_quotes_and_backticks(self):
        with patch.object(app, "_claude_cli_path", lambda: "/usr/local/bin/claude"):
            with self._stub_cli('"Use this skill for Y"'):
                ok, payload = app.suggest_skill_description("demo")
        self.assertTrue(ok)
        self.assertEqual(payload["suggestion"], "Use this skill for Y")

    def test_sanitizes_multiline_keeps_first_meaningful(self):
        with patch.object(app, "_claude_cli_path", lambda: "/usr/local/bin/claude"):
            with self._stub_cli("Sure! Here's the description:\n\nUse this skill for Z\n\nHope this helps."):
                ok, payload = app.suggest_skill_description("demo")
        self.assertTrue(ok)
        self.assertIn("Use this skill for Z", payload["suggestion"])

    def test_returns_error_when_cli_returns_empty(self):
        with patch.object(app, "_claude_cli_path", lambda: "/usr/local/bin/claude"):
            with self._stub_cli("###"):
                ok, msg = app.suggest_skill_description("demo")
        self.assertFalse(ok)
        self.assertIn("vide", msg)

    def test_handles_cli_timeout(self):
        import subprocess
        def boom(prompt, timeout=60):
            raise subprocess.TimeoutExpired("claude", timeout)
        with patch.object(app, "_claude_cli_path", lambda: "/usr/local/bin/claude"):
            with patch.object(app, "_call_claude_cli", boom):
                ok, msg = app.suggest_skill_description("demo")
        self.assertFalse(ok)
        self.assertIn("Timeout", msg)


if __name__ == "__main__":
    unittest.main()
