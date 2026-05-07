"""v1.11.0 - Tests pour bridge_plugin_mcp_to_desktop : copie d'un MCP
declare par un plugin Claude Code dans claude_desktop_config.json pour
que Claude Desktop le charge aussi (en plus de Claude Code CLI).
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import app  # noqa: E402


def _write_plugin_with_mcp(install_path, mcp_name, server_def, layout="root"):
    """Cree un faux plugin avec un .mcp.json. layout='root' pour
    <install>/.mcp.json ou layout='nested' pour <install>/.claude-plugin/.mcp.json."""
    p = Path(install_path)
    p.mkdir(parents=True, exist_ok=True)
    if layout == "root":
        (p / ".mcp.json").write_text(json.dumps({"mcpServers": {mcp_name: server_def}}))
    else:
        (p / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (p / ".claude-plugin/.mcp.json").write_text(
            json.dumps({"mcpServers": {mcp_name: server_def}})
        )


class ReadPluginMcpServersTests(unittest.TestCase):
    def test_reads_root_mcp_json(self):
        with tempfile.TemporaryDirectory() as d:
            _write_plugin_with_mcp(d, "memstore", {"command": "node", "args": ["x.js"]})
            servers = app._read_plugin_mcp_servers(d)
            self.assertIn("memstore", servers)
            self.assertEqual(servers["memstore"]["command"], "node")

    def test_reads_nested_claude_plugin_mcp_json(self):
        with tempfile.TemporaryDirectory() as d:
            _write_plugin_with_mcp(d, "memstore", {"command": "py"}, layout="nested")
            servers = app._read_plugin_mcp_servers(d)
            self.assertEqual(servers["memstore"]["command"], "py")

    def test_returns_empty_when_no_mcp_json(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(app._read_plugin_mcp_servers(d), {})

    def test_returns_empty_when_install_path_missing(self):
        self.assertEqual(app._read_plugin_mcp_servers("/nonexistent"), {})

    def test_returns_empty_on_invalid_json(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".mcp.json").write_text("not json {")
            self.assertEqual(app._read_plugin_mcp_servers(d), {})


class BridgePluginMcpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._loaded_config = {"mcpServers": {}, "_disabledMcps": {}}
        self._saved_config = None

        def fake_load_config():
            return json.loads(json.dumps(self._loaded_config))  # deep copy

        def fake_save_config(cfg):
            self._saved_config = cfg

        def fake_load_installed_plugins():
            return self._installed

        self._orig = (app.load_config, app.save_config, app._load_installed_plugins)
        app.load_config = fake_load_config
        app.save_config = fake_save_config
        app._load_installed_plugins = fake_load_installed_plugins

        self._installed = {}

    def tearDown(self):
        app.load_config, app.save_config, app._load_installed_plugins = self._orig
        self.tmp.cleanup()

    def _setup_plugin(self, full_name, mcp_name, server_def):
        install_dir = self.tmp_path / full_name.replace("@", "_")
        _write_plugin_with_mcp(str(install_dir), mcp_name, server_def)
        self._installed[full_name] = [{"installPath": str(install_dir), "version": "1.0.0"}]
        return install_dir

    def test_bridges_mcp_into_mcp_servers(self):
        self._setup_plugin("claude-mem@thedotmack", "memstore",
                           {"command": "node", "args": ["server.js"]})
        ok, msg = app.bridge_plugin_mcp_to_desktop("claude-mem@thedotmack", "memstore")
        self.assertTrue(ok, msg)
        self.assertIn("memstore", self._saved_config["mcpServers"])
        self.assertEqual(self._saved_config["mcpServers"]["memstore"]["command"], "node")
        self.assertIn("Redemarre Claude Desktop", msg)

    def test_refuses_when_plugin_unknown(self):
        ok, msg = app.bridge_plugin_mcp_to_desktop("nope@x", "memstore")
        self.assertFalse(ok)
        self.assertIn("introuvable", msg)
        self.assertIsNone(self._saved_config)

    def test_refuses_when_mcp_not_in_plugin(self):
        self._setup_plugin("claude-mem@thedotmack", "memstore", {"command": "x"})
        ok, msg = app.bridge_plugin_mcp_to_desktop("claude-mem@thedotmack", "ghost")
        self.assertFalse(ok)
        self.assertIn("ghost", msg)
        self.assertIn("introuvable", msg)
        self.assertIsNone(self._saved_config)

    def test_refuses_when_name_collides_in_active(self):
        self._setup_plugin("claude-mem@thedotmack", "memstore", {"command": "node"})
        self._loaded_config = {"mcpServers": {"memstore": {"command": "other"}},
                               "_disabledMcps": {}}
        ok, msg = app.bridge_plugin_mcp_to_desktop("claude-mem@thedotmack", "memstore")
        self.assertFalse(ok)
        self.assertIn("existe deja", msg)
        self.assertIsNone(self._saved_config)

    def test_refuses_when_name_collides_in_disabled(self):
        self._setup_plugin("claude-mem@thedotmack", "memstore", {"command": "node"})
        self._loaded_config = {"mcpServers": {},
                               "_disabledMcps": {"memstore": {"command": "stale"}}}
        ok, msg = app.bridge_plugin_mcp_to_desktop("claude-mem@thedotmack", "memstore")
        self.assertFalse(ok)
        self.assertIn("existe deja", msg)
        self.assertIsNone(self._saved_config)

    def test_empty_args_returns_error(self):
        ok, msg = app.bridge_plugin_mcp_to_desktop("", "")
        self.assertFalse(ok)
        ok, msg = app.bridge_plugin_mcp_to_desktop("plugin", "")
        self.assertFalse(ok)
        ok, msg = app.bridge_plugin_mcp_to_desktop("", "mcp")
        self.assertFalse(ok)


class ScanPluginContentsBridgedFlagTests(unittest.TestCase):
    """v1.11.0 - _scan_plugin_contents annote chaque MCP plugin avec bridged
    (deja present dans claude_desktop_config.json -> Claude Desktop le charge)."""

    def setUp(self):
        self._orig_load = app.load_config

    def tearDown(self):
        app.load_config = self._orig_load

    def test_marks_bridged_true_when_in_active(self):
        with tempfile.TemporaryDirectory() as d:
            _write_plugin_with_mcp(d, "memstore", {"command": "node"})
            app.load_config = lambda: {"mcpServers": {"memstore": {"command": "x"}},
                                       "_disabledMcps": {}}
            contents = app._scan_plugin_contents(d)
            self.assertEqual(contents["mcps"], [{"name": "memstore", "bridged": True}])

    def test_marks_bridged_false_when_absent(self):
        with tempfile.TemporaryDirectory() as d:
            _write_plugin_with_mcp(d, "memstore", {"command": "node"})
            app.load_config = lambda: {"mcpServers": {}, "_disabledMcps": {}}
            contents = app._scan_plugin_contents(d)
            self.assertEqual(contents["mcps"], [{"name": "memstore", "bridged": False}])

    def test_marks_bridged_true_when_in_disabled(self):
        with tempfile.TemporaryDirectory() as d:
            _write_plugin_with_mcp(d, "memstore", {"command": "node"})
            app.load_config = lambda: {"mcpServers": {},
                                       "_disabledMcps": {"memstore": {"command": "x"}}}
            contents = app._scan_plugin_contents(d)
            self.assertEqual(contents["mcps"], [{"name": "memstore", "bridged": True}])

    def test_handles_load_config_failure(self):
        with tempfile.TemporaryDirectory() as d:
            _write_plugin_with_mcp(d, "memstore", {"command": "node"})

            def boom():
                raise RuntimeError("config unreadable")

            app.load_config = boom
            contents = app._scan_plugin_contents(d)
            self.assertEqual(contents["mcps"], [{"name": "memstore", "bridged": False}])


if __name__ == "__main__":
    unittest.main()
