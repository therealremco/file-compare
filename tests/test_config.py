import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from file_compare import main, parse_options


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name) / "config.json"
        self.values = {
            "host": "example.com", "remote_path": "/remote", "local_path": ".",
            "filename_only": True, "port": 2222, "output": "configured.txt",
        }

    def save(self):
        self.config.write_text(json.dumps(self.values), encoding="utf-8")

    def test_config_and_explicit_cli_precedence(self):
        self.save()
        options = parse_options([
            "--config", str(self.config), "--port", "22", "--no-filename-only",
            "--output", "cli.txt",
        ])
        self.assertEqual(options.port, 22)
        self.assertFalse(options.filename_only)
        self.assertEqual(options.host, "example.com")
        self.assertEqual(options.output, Path("cli.txt"))

    def test_defaults_and_alias(self):
        options = parse_options([
            "--host", "example.com", "--remote-path", "/files", "--local-path", ".",
        ])
        self.assertEqual(options.port, 22)
        self.assertFalse(options.filename_only)
        self.assertEqual(options.timeout, 30)
        self.assertFalse(options.debug_ssh)
        options = parse_options([
            "--host", "example.com", "--remote-path", ".", "--local-path", ".",
            "--ignore-extension-and-size",
        ])
        self.assertTrue(options.filename_only)

    def test_debug_ssh_config_and_cli_precedence(self):
        for configured in (False, True):
            self.values["debug_ssh"] = configured
            self.save()
            base = ["--config", str(self.config)]
            self.assertEqual(parse_options(base).debug_ssh, configured)
            self.assertTrue(parse_options([*base, "--debug-ssh"]).debug_ssh)
            self.assertFalse(parse_options([*base, "--no-debug-ssh"]).debug_ssh)

    def test_reject_invalid_configuration(self):
        for key, value in [
            ("port", True), ("port", 0), ("port", 65536), ("port", "22"),
            ("timeout", False), ("timeout", 0), ("timeout", float("inf")),
            ("filename_only", "false"), ("host", "sftp://example.com"),
            ("local_path", None), ("host", ""), ("unknown", 1),
            ("debug_ssh", "false"), ("debug_ssh", 1), ("debug_ssh", None),
        ]:
            with self.subTest(key=key, value=value):
                original = self.values.copy()
                self.values[key] = value
                self.save()
                with self.assertRaises(ValueError):
                    parse_options(["--config", str(self.config)])
                self.values = original

    def test_missing_required_option_is_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                parse_options([])
        self.assertEqual(result.exception.code, 2)

    def test_malformed_config_is_reported(self):
        self.config.write_text("{", encoding="utf-8")
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertEqual(main(["--config", str(self.config)]), 1)
        self.assertIn("Error:", error.getvalue())

    def test_non_object_config_is_rejected(self):
        self.config.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "JSON object"):
            parse_options(["--config", str(self.config)])
