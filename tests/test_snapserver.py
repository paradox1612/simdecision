"""Fast Android snapshots: format compatibility and fallback, offline (no device)."""
import time, unittest
import xml.etree.ElementTree as ET

from simdecision import core, snapserver

N = ('<node index="0" text="{t}" resource-id="" class="android.widget.TextView" package="{p}" content-desc="" checkable="false" '
     'checked="false" clickable="{c}" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" '
     'password="false" selected="false" bounds="{b}"{end}')
HEAD = "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation=\"0\">"


def node(t, p, c, b, kids=""):
    return N.format(t=t, p=p, c=str(c).lower(), b=b, end=">" + kids + "</node>")


APP = "com.example"
ROWS = node("", APP, False, "[0,0][1080,2400]", node("Wi-Fi", APP, True, "[0,200][1080,300]") + node("Display", APP, True, "[0,300][1080,400]"))
# what `uiautomator dump` returns: the active window only
UIAUTOMATOR = HEAD + ROWS + "</hierarchy>"
# what the helper returns: a synthetic screen root, then every window bottom-most first (app, then system UI on top)
HELPER = (HEAD + node("", "", False, "[0,0][1080,2400]", ROWS + node("12:00", "com.android.systemui", False, "[0,0][1080,80]"))
          + "</hierarchy>")


class Format(unittest.TestCase):
    def setUp(self):
        self.saved = (core.PLATFORM, core.PKG, core.PKGS_OK)
        core.PLATFORM, core.PKG, core.PKGS_OK = "phone", APP, {APP}

    def tearDown(self):
        core.PLATFORM, core.PKG, core.PKGS_OK = self.saved

    def test_helper_output_compacts_exactly_like_uiautomator(self):
        a = core.compact(ET.fromstring(UIAUTOMATOR))
        b = core.compact(ET.fromstring(HELPER))
        self.assertEqual(a[0], b[0])
        self.assertEqual([(i["label"], i["box"]) for i in a[2]], [(i["label"], i["box"]) for i in b[2]])


class Fallback(unittest.TestCase):
    def setUp(self):
        self.saved = (snapserver.ensure, snapserver._failed_at.copy(), core.adb, core.SERIAL)
        core.SERIAL = "emulator-test"
        snapserver._failed_at.clear()

    def tearDown(self):
        snapserver.ensure, failed, core.adb, core.SERIAL = self.saved
        snapserver._failed_at.clear(); snapserver._failed_at.update(failed)

    def test_helper_failure_falls_back_to_uiautomator_and_cools_down(self):
        calls = {"ensure": 0, "uiautomator": 0}

        def broken(adb, serial):
            calls["ensure"] += 1
            raise snapserver.SnapError("no helper")

        class R:
            stdout = UIAUTOMATOR.encode()

        def fake_adb(*args, timeout=20):
            calls["uiautomator"] += "uiautomator" in args
            return R()
        snapserver.ensure, core.adb = broken, fake_adb
        for _ in range(3):
            self.assertIsNotNone(core.snapshot())
        self.assertEqual(calls["ensure"], 1, "after one failure the helper is not retried during the cooldown")
        self.assertEqual(calls["uiautomator"], 3)

    def test_disabled_by_env(self):
        import os
        os.environ["SIMDECISION_FAST_SNAPSHOT"] = "0"
        try:
            self.assertIsNone(snapserver.dump("adb", "emulator-test"))
        finally:
            del os.environ["SIMDECISION_FAST_SNAPSHOT"]

    def test_cooldown_expires(self):
        snapserver._failed_at["emulator-test"] = time.time() - snapserver.COOLDOWN - 1
        snapserver.ensure = lambda adb, serial: (_ for _ in ()).throw(snapserver.SnapError("x"))
        self.assertIsNone(snapserver.dump("adb", "emulator-test"))  # tried again (and failed) once the cooldown passed
        self.assertGreater(snapserver._failed_at["emulator-test"], time.time() - 5)


if __name__ == "__main__":
    unittest.main()
