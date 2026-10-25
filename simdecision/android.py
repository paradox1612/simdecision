"""Android driver (phone + TV emulators): every action the runtime can take on a device lives here.

Thin and deterministic - wraps tsim's adb/uiautomator helpers. Never drives a physical device. The decision
model never sees this layer; it only sees the compact screen text and the option list built from it.
"""
import os, re, subprocess, sys, time

from . import core as tsim  # noqa: E402
from . import actions as A  # noqa: E402

from . import config  # noqa: E402

PLATFORMS = ("phone", "tv")
# Never offered to the decision model unless the scenario lists the label in `allow:`.
DENY = re.compile(r"\b(buy|rent|purchase|subscribe|subscription|upgrade|donate|pay|checkout|sign out|log ?out|"
                  r"delete account|delete all|remove all|factory reset|uninstall|clear all data)\b|\$\d", re.I)


def _as_list(x):
    """tsim.toward returns a bare string when nothing is focused; callers iterate."""
    return [x] if isinstance(x, str) else list(x)


class DriverError(RuntimeError):
    pass


class PreMutationError(DriverError):
    pre_mutation = True
    """The action was REFUSED before touching the device (target gone, screen changed, tap point covered): nothing happened,
    so the loop may re-observe and choose again. Never raised after a tap/press/type was sent, and never retried blindly."""


def _raw(*a):
    return subprocess.run([tsim.ADB, *a], capture_output=True, text=True, timeout=25).stdout


def is_tv(serial):
    out = _raw("-s", serial, "shell", "getprop", "ro.build.characteristics")
    return "tv" in out


def pick_emulator(platform, avd=None):
    """The emulator for this platform: the configured AVD if set, else the first running emulator of the right kind."""
    emus = emulators()
    if avd:
        match = [s for s, a in emus.items() if a == avd]
        hint = f"Boot it: $ANDROID_HOME/emulator/emulator -avd {avd} -no-snapshot-save"
    else:
        match = [s for s in emus if is_tv(s) == (platform == "tv")]
        hint = "Boot an emulator of that kind, or set app.<platform>.avd in simdecision.yaml"
    if not match:
        raise DriverError(f"no {platform} emulator running (running: {emus or 'none'}). {hint}")
    return match[0]


def emulators():
    out = {}
    for line in _raw("devices").splitlines():
        if line.startswith("emulator-") and "\tdevice" in line:
            s = line.split()[0]
            n = _raw("-s", s, "emu", "avd", "name").splitlines()
            out[s] = n[0].strip() if n else "?"
    return out


class Android:
    notes = None  # warnings a step should surface (e.g. "paste emulated by typing")

    expect_labels = None  # label set of the screen the pending decision was made on; consumed by the next tap

    def __init__(self, platform):
        self.notes = []
        if platform not in PLATFORMS:
            raise DriverError(f"android driver has no platform '{platform}'")
        conf = config.app(platform)
        self.platform, self.serial = platform, pick_emulator(platform, conf.get("avd"))
        self.pkg, self.activity = conf["package"], conf["activity"]
        self.pkgs = {self.pkg, *conf.get("also", [])}  # helper packages that are part of the app (e.g. a search UI)
        self.bind()

    def owns(self, foreground):
        """Is this foreground package/bundle the app under test (or one of its declared helper packages)?"""
        return foreground in getattr(self, "pkgs", {self.pkg})

    def bind(self):
        tsim.SERIAL, tsim.PLATFORM, tsim.PKG, tsim.PKGS_OK = self.serial, self.platform, self.pkg, self.pkgs

    # ---- observation ----
    def screen(self):
        """-> (root, text, items, focused) | (None, msg, [], None)"""
        self.bind()
        root = tsim.snapshot()
        if root is None:
            return None, "(screen dump failed - a video surface may be busy)", [], None
        text, focused, items = tsim.compact(root)
        return root, text, items, focused

    def foreground(self):
        out = tsim.adb("shell", "dumpsys", "window").stdout.decode("utf-8", "replace")
        m = re.search(r"mCurrentFocus=.*? ([\w.]+)/", out)
        return m.group(1) if m else "?"

    def screenshot(self, path):
        self.bind()
        with open(path, "wb") as f:
            f.write(tsim.adb("exec-out", "screencap", "-p", timeout=20).stdout)
        return path

    def screenshot_pair(self, gap=1.0):
        a = tsim.screenshot_b64(); time.sleep(gap)
        return [a, tsim.screenshot_b64()]

    def log(self, regex=None, buffers="all"):
        out = tsim.adb("logcat", "-b", buffers, "-d", "-v", "brief", timeout=30).stdout.decode("utf-8", "replace")
        lines = out.splitlines()
        if regex:
            rx = re.compile(regex)
            lines = [l for l in lines if rx.search(l)]
        return lines

    def crashes(self):
        """Fatal exceptions / native crashes / ANRs attributed to our package since the last logcat_clear."""
        lines = self.log(buffers="crash") + self.log(r"ANR in " + re.escape(self.pkg))
        return [l for l in lines if self.pkg in l or "FATAL EXCEPTION" in l or "ANR in" in l][:5]

    # ---- actions ----
    def launch(self, fresh=True):
        self.bind()
        if fresh:
            for p in sorted(self.pkgs):  # helper packages too: a leftover helper activity would stay on top
                tsim.adb("shell", "am", "force-stop", p)
        tsim.adb("shell", "am", "start", "-n", f"{self.pkg}/{self.activity}")
        deadline = time.time() + 25
        while time.time() < deadline:
            time.sleep(1.5)
            if len(self.screen()[2]) >= 3:
                return
        raise DriverError("app launched but no interactive screen appeared within 25s")

    def stop(self):
        for p in sorted(self.pkgs):
            tsim.adb("shell", "am", "force-stop", p)

    def key(self, name):
        if name not in A.KEYS:
            raise DriverError(f"unknown key '{name}'")
        tsim.adb("shell", "input", "keyevent", str(A.KEYS[name]))

    def back(self):
        self.key("back")

    def scroll(self, direction):
        root = self.screen()[0]
        if root is None:
            raise DriverError("cannot scroll: no screen")
        tsim.act("scroll_" + direction, None, root)

    def wait(self, seconds):
        time.sleep(float(seconds))

    def deeplink(self, url):
        tsim.adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url, self.pkg)

    def network(self, state):
        tsim.adb("shell", "cmd", "connectivity", "airplane-mode", "enable" if state == "offline" else "disable")

    def logcat_clear(self):
        tsim.adb("logcat", "-b", "all", "-c")

    @staticmethod
    def find(items, label):
        want = label.strip().lower()
        ident = lambda i: (i.get("id") or "").lower()  # noqa: E731 - Apple items carry an accessibility id too
        exact = [i for i in items if i["label"].lower() == want or ident(i) == want]
        if exact:
            return exact[0]
        part = [i for i in items if want in i["label"].lower() or (ident(i) and want in ident(i))]
        return min(part, key=lambda i: len(i["label"])) if part else None

    @staticmethod
    def arrived(focused, it):
        """TV focus lands on a wrapper node for text fields: a focused box that contains the target counts."""
        if not focused:
            return False
        if focused["label"] == it["label"] or (focused.get("id") and focused.get("id") == it.get("id")):
            return True
        fx1, fy1, fx2, fy2 = focused["box"]
        x1, y1, x2, y2 = it["box"]
        return fx1 - 6 <= x1 and fy1 - 6 <= y1 and fx2 + 6 >= x2 and fy2 + 6 >= y2 and (it.get("editable") or it["label"] == "(unlabelled)")

    def tap(self, label, max_moves=30):
        """Phone: tap the item. TV: steer D-pad focus there rows-first (code steers, model only picked the target)."""
        root, text, items, focused = self.screen()
        if root is None:
            raise DriverError("cannot tap: no screen")
        it = self.find(items, label)
        if not it:
            raise DriverError(f'no item matching "{label}" on screen')
        if self.platform == "phone":
            from . import guards
            expected, self.expect_labels = self.expect_labels, None
            if guards.screen_changed(expected, items):
                raise PreMutationError(f'screen changed since the decision; "{label}" not tapped')
            who = guards.occluder(root, it["box"], self.pkgs)
            if who:
                raise PreMutationError(f'"{label}" is covered by "{who}"; not tapped')
            tsim.act("tap " + it["label"], it["box"], root)
            return
        tried = {}
        for _ in range(max_moves):
            if self.arrived(focused, it):
                tsim.act("select", None, root)
                return
            moves = [m for m in _as_list(tsim.toward(focused, it)) if tried.get(((focused or {}).get("label"), m), 0) < 2]
            if not moves:
                raise DriverError(f'could not steer focus to "{label}"')
            k = ((focused or {}).get("label"), moves[0])
            tried[k] = tried.get(k, 0) + 1
            tsim.act(moves[0], None, root)
            time.sleep(0.6)
            root, text, items, focused = self.screen()
            it = self.find(items, label) or it
        raise DriverError(f'gave up steering to "{label}" after {max_moves} moves')

    def _field(self):
        root, _, items, focused = self.screen()
        return root, next((i for i in items if i.get("editable")), None), focused

    def focus_field(self):
        """Phone: tap the first text field so it holds the caret. TV: focus is already where the user put it."""
        root, field, _ = self._field()
        if self.platform == "phone":
            if not field:
                raise DriverError("no text field on screen")
            x1, y1, x2, y2 = field["box"]
            tsim.adb("shell", "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))
            time.sleep(0.6)
        return field

    def type_text(self, text, submit=True, clear=False, field=None):
        """field = on-screen label of the text field to focus first (needed on forms with several fields)."""
        if field:
            self.tap(field)  # phone: tap; TV: steer focus there and press OK (opens the field for input)
            time.sleep(0.8)
        elif self.platform == "phone":
            self.focus_field()
        if clear:
            self.delete("all")
        safe = re.sub(r"([\\'\"()&|;<>$`!*?#])", r"\\\1", text).replace(" ", "%s")
        tsim.adb("shell", "input", "text", safe)
        if submit:
            tsim.adb("shell", "input", "keyevent", "66")

    def delete(self, n):
        if self.platform == "phone":
            field = self.focus_field()
        else:
            field = self._field()[1]
        have = len((field or {}).get("text", "")) if field else 0
        count = (have + 5 if have else 40) if n == "all" else int(n)
        tsim.adb("shell", "input", "keyevent", str(A.KEYS["move_end"]))
        for i in range(0, count, 25):  # batched: one adb call deletes up to 25 characters
            tsim.adb("shell", "input", "keyevent", *([str(A.KEYS["delete"])] * min(25, count - i)))

    def paste(self, text=None):
        """Real keyboard paste when the host clipboard reaches the guest; otherwise typed, and said so loudly."""
        field = self.focus_field() if self.platform == "phone" else self._field()[1]
        before = (field or {}).get("text", "")
        if text is not None:
            subprocess.run(["pbcopy"], input=text.encode(), timeout=5)  # emulator mirrors host clipboard when it can
            time.sleep(1.5)
        tsim.adb("shell", "input", "keyevent", str(A.KEYS["paste"]))
        time.sleep(0.8)
        after = (self._field()[1] or {}).get("text", "")
        if text is not None and after == before:
            self.notes.append("paste NOT exercised: host clipboard did not reach the emulator, text was typed instead")
            self.type_text(text, submit=False)

    def long_press(self, label):
        root, _, items, _ = self.screen()
        it = self.find(items, label)
        if not it:
            raise DriverError(f'no item matching "{label}" on screen')
        if self.platform == "tv":
            self.tap_focus_only(it)
            tsim.adb("shell", "input", "keyevent", "--longpress", str(A.KEYS["select"]))
        else:
            x, y = (it["box"][0] + it["box"][2]) // 2, (it["box"][1] + it["box"][3]) // 2
            tsim.adb("shell", "input", "swipe", str(x), str(y), str(x), str(y), "900")

    def tap_focus_only(self, it):
        """TV: steer focus onto an item without pressing select."""
        for _ in range(30):
            _, _, items, focused = self.screen()
            cur = self.find(items, it["label"]) or it
            if focused and focused["label"] == cur["label"]:
                return
            moves = _as_list(tsim.toward(focused, cur))
            tsim.act(moves[0], None, None)
            time.sleep(0.6)
        raise DriverError(f'could not steer focus to "{it["label"]}"')

    def swipe(self, spec):
        root = self.screen()[0]
        if root is None:
            raise DriverError("no screen to swipe on")
        W, H = tsim.bounds(root.find("node").get("bounds"))[2:]
        if isinstance(spec, dict):
            (x1, y1), (x2, y2) = [tuple(int(v) for v in spec[k].split(",")) for k in ("from", "to")]
            ms = int(spec.get("ms", 350))
        else:
            cx, cy = W // 2, H // 2
            d = {"up": (cx, int(H * .7), cx, int(H * .3)), "down": (cx, int(H * .3), cx, int(H * .7)),
                 "left": (int(W * .8), cy, int(W * .2), cy), "right": (int(W * .2), cy, int(W * .8), cy)}[spec]
            x1, y1, x2, y2 = d
            ms = 350
        tsim.adb("shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(ms))

    def perform(self, verb, value):
        """Single dispatch for every YAML action. `value` already has $inputs resolved."""
        self.bind()
        if verb == "launch":
            self.launch(fresh=True)
        elif verb == "stop":
            self.stop()
        elif verb == "background":
            self.key("home")
        elif verb == "foreground":
            tsim.adb("shell", "am", "start", "-n", f"{self.pkg}/{self.activity}")
        elif verb == "clear_data":
            tsim.adb("shell", "pm", "clear", self.pkg)
        elif verb == "deeplink":
            self.deeplink(str(value))
        elif verb == "key":
            for name, delay in A.expand_keys(value):
                self.key(name)
                time.sleep(delay)
        elif verb == "hold":
            tsim.adb("shell", "input", "keyevent", "--longpress", str(A.KEYS[value["key"]]))
            time.sleep(max(0.0, int(value.get("ms", 1000)) / 1000 - 0.5))
        elif verb == "back":
            self.back()
        elif verb == "tap":
            self.tap(str(value))
        elif verb == "tap_xy":
            x, y = (v.strip() for v in str(value).split(","))
            tsim.adb("shell", "input", "tap", x, y)
        elif verb == "long_press":
            self.long_press(str(value))
        elif verb == "double_tap":
            it = self.find(self.screen()[2], str(value))
            if not it:
                raise DriverError(f'no item matching "{value}" on screen')
            x, y = (it["box"][0] + it["box"][2]) // 2, (it["box"][1] + it["box"][3]) // 2
            tsim.adb("shell", "input", "tap", str(x), str(y)); time.sleep(0.12)
            tsim.adb("shell", "input", "tap", str(x), str(y))
        elif verb == "swipe":
            self.swipe(value)
        elif verb == "scroll":
            self.scroll(str(value))
        elif verb == "type":
            if isinstance(value, dict):
                self.type_text(str(value["text"]), submit=value.get("submit", True), clear=value.get("clear", False),
                               field=value.get("field"))
            else:
                self.type_text(str(value))
        elif verb == "delete":
            self.delete(value)
        elif verb in ("select_all",):
            tsim.adb("shell", "input", "keycombination", "113", "29")  # CTRL+A
        elif verb in ("copy", "cut"):
            self.key(verb)
        elif verb == "paste":
            self.paste(None if value is None else str(value))
        elif verb == "rotate":
            tsim.adb("shell", "settings", "put", "system", "accelerometer_rotation", "0")
            tsim.adb("shell", "settings", "put", "system", "user_rotation", "1" if value == "landscape" else "0")
        elif verb == "appearance":
            tsim.adb("shell", "cmd", "uimode", "night", "yes" if value == "dark" else "no")
        elif verb == "font_scale":
            tsim.adb("shell", "settings", "put", "system", "font_scale", str(value))
        elif verb == "location":
            lat, lon = (v.strip() for v in str(value).split(","))
            _raw("-s", self.serial, "emu", "geo", "fix", lon, lat)
        elif verb == "permission":
            perm = value["name"] if "." in value["name"] else "android.permission." + value["name"]
            tsim.adb("shell", "pm", value["action"], self.pkg, perm)
        elif verb == "network":
            self.network(str(value))
        elif verb == "wait":
            self.wait(value)
        elif verb == "logcat_clear":
            self.logcat_clear()
        else:
            raise DriverError(f"android driver does not implement '{verb}'")
