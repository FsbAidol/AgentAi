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
        with patch("shutil.which", return_value="/some/opencode"), patch("agentai.cli.fetch_models", return_value=["test-model", "other"]), patch("subprocess.call", return_value=0) as call:
            self.assertEqual(cli.launch(args), 0)
        command = call.call_args.args[0]
        env = call.call_args.kwargs["env"]
        self.assertEqual(command[command.index("--agent") + 1], "build")
        self.assertNotIn("super-secret", repr(command))
        self.assertEqual(command[-1], "привет; echo test")
        self.assertEqual(env["AGENTAI_API_KEY"], "super-secret")
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertIn("other", config["provider"]["agentai"]["models"])
        self.assertNotIn("permission", config)
        self.assertEqual(config["agent"]["build"]["permission"], "ask")
        self.assertEqual(config["provider"]["agentai"]["options"]["apiKey"], "{env:AGENTAI_API_KEY}")
        self.assertNotIn("super-secret", env["OPENCODE_CONFIG_CONTENT"])

    def test_full_access_requires_confirmation_and_is_explicit(self):
        cli.save_profiles({"gwarden": {"base_url": cli.DEFAULT_URL, "model": "model", "api_key": "key"}})
        args = cli.build_parser().parse_args(["start", "--full-access"])
        with patch("shutil.which", return_value="/some/opencode"), patch("sys.stdin.isatty", return_value=False):
            with self.assertRaisesRegex(ValueError, "отменена"):
                cli.launch(args)
        with patch("shutil.which", return_value="/some/opencode"), patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="FULL"), patch("agentai.cli.fetch_models", return_value=["model"]), patch("subprocess.call", return_value=0) as call:
            cli.launch(args)
        config = json.loads(call.call_args.kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
        self.assertNotIn("permission", config)
        self.assertEqual(config["agent"]["build"]["permission"], "allow")

    def test_persistent_full_access_and_reversal(self):
        cli.save_profiles({"gwarden": {"base_url": cli.DEFAULT_URL, "model": "model", "api_key": "key"}})
        parser = cli.build_parser()
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaisesRegex(ValueError, "отменена"):
                cli.set_access(parser.parse_args(["access", "full"]))
        self.assertNotIn("full_access", cli.load_profiles()["gwarden"])
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="FULL"):
            cli.set_access(parser.parse_args(["access", "full"]))
        self.assertTrue(cli.load_profiles()["gwarden"]["full_access"])
        with patch("shutil.which", return_value="/some/opencode"), patch("agentai.cli.fetch_models", return_value=["model"]), patch("subprocess.call", return_value=0) as call:
            cli.launch(parser.parse_args(["start"]))
        self.assertEqual(call.call_args.args[0][2], "build")
        config = json.loads(call.call_args.kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["agent"]["build"]["permission"], "allow")
        with patch("sys.stdin.isatty", return_value=True), patch("getpass.getpass", return_value="new-key"):
            cli.add_profile(parser.parse_args(["add", "--replace"]))
        self.assertTrue(cli.load_profiles()["gwarden"]["full_access"])
        cli.set_access(parser.parse_args(["access", "ask"]))
        self.assertNotIn("full_access", cli.load_profiles()["gwarden"])

    def test_fetch_and_select_models(self):
        cli.save_profiles({"gwarden": {"base_url": cli.DEFAULT_URL, "model": "old", "api_key": "secret"}})
        body = json.dumps({"data": [{"id": "alpha"}, {"id": "beta"}, {"id": "alpha"}, {"id": "bad id"}]}).encode()
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = body
        with patch("agentai.cli.build_opener") as opener:
            opener.return_value.open.return_value = response
            output = io.StringIO()
            with patch("sys.argv", ["agentai", "models"]), contextlib.redirect_stdout(output):
                cli.main()
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://gwarden.su/v1/models")
            self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(cli.load_profiles()["gwarden"]["models"], ["alpha", "beta"])
        self.assertIn("alpha", output.getvalue())
        with patch("sys.argv", ["agentai", "model", "beta"]), contextlib.redirect_stdout(output):
            cli.main()
        profile = cli.load_profiles()["gwarden"]
        self.assertEqual(profile["model"], "beta")
        self.assertEqual(profile["api_key"], "secret")
        self.assertIn("alpha", cli.make_config(profile, False)["provider"]["agentai"]["models"])

    def test_fetch_failure_falls_back_to_cached_models(self):
        cli.save_profiles({"gwarden": {"base_url": cli.DEFAULT_URL, "model": "old", "api_key": "secret", "models": ["cached"]}})
        args = cli.build_parser().parse_args(["start"])
        with patch("shutil.which", return_value="/some/opencode"), patch("agentai.cli.fetch_models", side_effect=ValueError("offline")), patch("subprocess.call", return_value=0) as call:
            cli.launch(args)
        config = json.loads(call.call_args.kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
        self.assertIn("cached", config["provider"]["agentai"]["models"])

    def test_corrupt_profiles_report_error(self):
        cli.storage_path().parent.mkdir(parents=True)
        cli.storage_path().write_text('{"gwarden": {}}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "формат"):
            cli.load_profiles()


if __name__ == "__main__":
    unittest.main()
