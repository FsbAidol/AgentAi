import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentai import cli


class AgentAITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        variable = "APPDATA" if os.name == "nt" else "XDG_CONFIG_HOME"
        env = patch.dict(os.environ, {variable: self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_add_list_replace_remove_and_private_storage(self):
        parser = cli.build_parser()
        with patch("sys.stdin.isatty", return_value=True), patch("getpass.getpass", return_value="secret-key"):
            cli.add_profile(parser.parse_args(["add"]))
        self.assertEqual(cli.load_profiles()["gwarden"]["api_key"], "secret-key")
        if os.name != "nt":
            self.assertEqual(cli.storage_path().stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(ValueError, "уже существует"):
            cli.add_profile(parser.parse_args(["add"]))
        with patch("sys.stdin.isatty", return_value=True), patch("getpass.getpass", return_value="new-key"):
            cli.add_profile(parser.parse_args(["add", "--replace", "--model", "other-model"]))
        self.assertEqual(cli.load_profiles()["gwarden"]["api_key"], "new-key")
        output = io.StringIO()
        with patch("sys.argv", ["agentai", "list"]), contextlib.redirect_stdout(output):
            cli.main()
        self.assertIn("other-model", output.getvalue())
        self.assertNotIn("new-key", output.getvalue())
        with patch("sys.argv", ["agentai", "remove", "gwarden"]), contextlib.redirect_stdout(output):
            cli.main()
        self.assertEqual(cli.load_profiles(), {})

    def test_invalid_inputs_and_non_terminal(self):
        for url in ["http://example.com/v1", "https://key@example.com/v1", "https://example.com/v1?q=key"]:
            with self.assertRaises(ValueError):
                cli.validate_url(url)
        with self.assertRaises(ValueError):
            cli.validate_name("../oops")
        with self.assertRaises(ValueError):
            cli.validate_model("model with spaces")
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaisesRegex(ValueError, "терминал"):
                cli.add_profile(cli.build_parser().parse_args(["add"]))

    def test_launch_uses_agent_config_and_does_not_put_key_in_arguments(self):
        cli.save_profiles({"gwarden": {"base_url": cli.DEFAULT_URL, "model": "test-model", "api_key": "super-secret"}})
        args = cli.build_parser().parse_args(["start", "--message", "привет; echo test"])
        with patch("shutil.which", return_value="/some/opencode"), patch("subprocess.call", return_value=0) as call:
            self.assertEqual(cli.launch(args), 0)
        command = call.call_args.args[0]
        env = call.call_args.kwargs["env"]
        self.assertNotIn("super-secret", repr(command))
        self.assertEqual(command[-1], "привет; echo test")
        self.assertEqual(env["AGENTAI_API_KEY"], "super-secret")
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["permission"], "ask")
        self.assertEqual(config["agent"]["agentai"]["permission"], "ask")
        self.assertEqual(config["provider"]["agentai"]["options"]["apiKey"], "{env:AGENTAI_API_KEY}")
        self.assertNotIn("super-secret", env["OPENCODE_CONFIG_CONTENT"])

    def test_full_access_requires_confirmation_and_is_explicit(self):
        cli.save_profiles({"gwarden": {"base_url": cli.DEFAULT_URL, "model": "model", "api_key": "key"}})
        args = cli.build_parser().parse_args(["start", "--full-access"])
        with patch("shutil.which", return_value="/some/opencode"), patch("sys.stdin.isatty", return_value=False):
            with self.assertRaisesRegex(ValueError, "отменён"):
                cli.launch(args)
        with patch("shutil.which", return_value="/some/opencode"), patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="FULL"), patch("subprocess.call", return_value=0) as call:
            cli.launch(args)
        config = json.loads(call.call_args.kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["permission"], "allow")
        self.assertEqual(config["agent"]["agentai"]["permission"], "allow")

    def test_corrupt_profiles_report_error(self):
        cli.storage_path().parent.mkdir(parents=True)
        cli.storage_path().write_text('{"gwarden": {}}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "формат"):
            cli.load_profiles()


if __name__ == "__main__":
    unittest.main()
