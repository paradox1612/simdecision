"""Scenario schema + config + fixtures: offline. Regression: the authoring guide once raised NameError (an unescaped
`{delete: all}` inside its f-string), which broke the MCP `schema` tool for every caller."""
import os, tempfile, unittest

from simdecision import config, scenario
from simdecision.fixtures import Fixtures


class Guide(unittest.TestCase):
    def test_guide_renders(self):
        g = scenario.authoring_guide()
        self.assertIn("{delete: all}", g)
        self.assertIn('kind: command', g)


class Parse(unittest.TestCase):
    def test_plain_sentences_parse(self):
        sc, errs, _ = scenario.parse("scenario: s\nplatform: phone\nsteps:\n  - open the Display settings\n")
        self.assertEqual(errs, [])
        self.assertEqual(sc.id, "s")

    def test_command_fixture_fields_are_bindable(self):
        y = ("scenario: f\nplatform: phone\nfixtures:\n  - {kind: command, name: m, cmd: [echo, '{port}'], fields: {feed: '{base}/feed'}}\n"
             "inputs:\n  u: {fixture: m, field: feed}\nsteps:\n  - open the Display settings\n")
        _, errs, _ = scenario.parse(y)
        self.assertEqual(errs, [])

    def test_unknown_fixture_field_is_an_error(self):
        y = ("scenario: f\nplatform: phone\nfixtures:\n  - {kind: http, name: api}\n"
             "inputs:\n  u: {fixture: api, field: nope}\nsteps:\n  - open the Display settings\n")
        _, errs, _ = scenario.parse(y)
        self.assertTrue(any("nope" in e for e in errs), errs)

    def test_unknown_fixture_kind_is_an_error(self):
        _, errs, _ = scenario.parse("scenario: f\nplatform: phone\nfixtures:\n  - {kind: bogus, name: p}\nsteps:\n  - open x\n")
        self.assertTrue(any("fixtures[1]" in e for e in errs), errs)


class Fixture(unittest.TestCase):
    def test_command_placeholders_are_filled(self):
        fx = Fixtures([], "phone", tempfile.mkdtemp())
        cmd, fields, parse = fx._command("command", {"cmd": ["srv", "--port", "{port}", "--log", "{log}"],
                                                     "fields": {"url": "{base}/x"}}, 1234, "http://10.0.2.2:1234", "/tmp/l")
        self.assertEqual(cmd, ["srv", "--port", "1234", "--log", "/tmp/l"])
        self.assertEqual(fields, {"url": "http://10.0.2.2:1234/x"})
        self.assertEqual(fx.host, "10.0.2.2")  # the Android emulator's view of the host machine


class Config(unittest.TestCase):
    def tearDown(self):
        config._cfg = None
        os.environ.pop("SIMDECISION_CONFIG", None)

    def test_defaults_drive_the_os_settings_apps(self):
        config._cfg = None
        os.environ["SIMDECISION_CONFIG"] = "/nonexistent/simdecision.yaml"
        self.assertEqual(config.app("phone")["package"], "com.android.settings")
        self.assertEqual(config.app("ios")["bundle"], "com.apple.Preferences")

    def test_file_overrides_merge_and_state_dir_is_relative_to_it(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "simdecision.yaml")
        open(p, "w").write("app:\n  phone: {package: com.example.app}\nstate_dir: out\n")
        config._cfg = None
        os.environ["SIMDECISION_CONFIG"] = p
        self.assertEqual(config.app("phone")["package"], "com.example.app")
        self.assertEqual(config.app("phone")["activity"], "com.android.settings.Settings")  # untouched keys keep defaults
        self.assertEqual(config.load()["state_dir"], os.path.join(d, "out"))


if __name__ == "__main__":
    unittest.main()
