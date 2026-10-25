"""iOS driver: the runtime's hands on the iPhone simulator, through the standalone QABridge XCUITest server.

Same bridge plumbing as the tvOS driver (inherits AppleTV), different input model: touch, not a remote. The
accessibility tree is read through XCUITest snapshots; taps/swipes are coordinate gestures at element centres; text
goes in through `typeText`. Simulators only. What XCUITest/simctl cannot do (airplane mode, wiping app data, volume
keys) raises DriverError so a scenario fails loudly instead of pretending.
"""
import os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
from . import core as tsim  # noqa: E402
from .android import DriverError, PreMutationError  # noqa: E402
from .apple import AppleTV  # noqa: E402

from . import config  # noqa: E402

IOS_BUNDLE = config.app("ios")["bundle"]
# simctl content size categories for the font_scale verb
SIZES = [(0.88, "small"), (1.05, "large"), (1.2, "extra-large"), (1.4, "extra-extra-large"),
         (1.7, "extra-extra-extra-large"), (99, "accessibility-extra-large")]


def booted_iphones():
    out = subprocess.run(["xcrun", "simctl", "list", "devices", "booted"], capture_output=True, text=True).stdout
    sims, in_ios = [], False
    for line in out.splitlines():
        if line.startswith("-- "):
            in_ios = "iOS" in line
        m = re.search(r"^\s+(.*?) \(([0-9A-F-]{36})\) \(Booted\)", line)
        if m and in_ios:
            sims.append((m.group(1), m.group(2)))
    return sims


class IOS(AppleTV):
    expect_labels = None  # label set of the screen the pending decision was made on; consumed by the next tap
    platform = "ios"
    pkg = IOS_BUNDLE
    PORT = int(config.app("ios").get("bridge_port") or os.environ.get("QA_IOS_BRIDGE_PORT", "8786"))
    DD = os.environ.get("QA_IOS_DD") or config.state("build", "ios")
    TESTID = "QABridgeUITests/QABridge/testServe"
    PROCESS = config.app("ios")["process"]

    def _pick_sim(self):
        want = config.app("ios").get("udid")
        if want:
            return next((s for s in booted_iphones() if s[1] == want), None) or (_ for _ in ()).throw(
                DriverError(f"configured iOS simulator {want} is not booted (xcrun simctl boot {want})"))
        sims = booted_iphones()
        if not sims:
            raise DriverError("no iPhone simulator is booted. Boot one: xcrun simctl boot <udid> (see xcrun simctl list devices available) "
                              "(iPhone 17 Pro). The app must be installed on it.")
        return sims[0]

    # ---------- observation ----------
    def screen(self):
        d = self._get("/screen")
        if d.get("state") != "foreground":
            return None, f"(app is {d.get('state')})", [], None
        items, seen = [], set()
        for it in d.get("items", []):
            label = (it.get("label") or "").strip()[:60] or "(unlabelled)"
            x, y, w, h = it["x"], it["y"], it["w"], it["h"]
            if (label, x, y, w, h) in seen or w < 4 or h < 4:
                continue
            seen.add((label, x, y, w, h))
            editable = bool(it.get("editable"))
            if editable:
                label = f'[text field] {it.get("text") or label}'[:60]
            items.append({"label": label, "id": it.get("id", ""), "box": (x, y, x + w, y + h), "focused": False,
                          "scrollable": False, "editable": editable, "text": it.get("text", "")})
        lines = [f'   "{i["label"]}" @{i["box"][0]},{i["box"][1]}'
                 for i in sorted(items, key=lambda i: (i["box"][1], i["box"][0]))]
        text = "TEXT: " + " | ".join(d.get("texts", [])[:12]) + "\nTAPPABLE:\n" + "\n".join(lines[:60])
        d["_size"] = (d.get("w", 0), d.get("h", 0))
        return d, text, items, None

    # ---------- touch primitives ----------
    @staticmethod
    def _centre(it):
        x1, y1, x2, y2 = it["box"]
        return (x1 + x2) // 2, (y1 + y2) // 2

    def _item(self, label):
        root, _, items, _ = self.screen()
        if root is None:
            raise DriverError("app is not in the foreground")
        it = self.find(items, label)
        if not it:
            raise DriverError(f'no item matching "{label}" on screen')
        return it

    def tap(self, label, **_):
        from . import guards
        root, _, items, _ = self.screen()
        if root is None:
            raise DriverError("app is not in the foreground")
        it = self.find(items, label)
        if not it:
            raise DriverError(f'no item matching "{label}" on screen')
        expected, self.expect_labels = self.expect_labels, None
        if guards.screen_changed(expected, items):
            raise PreMutationError(f'screen changed since the decision; "{label}" not tapped')
        x, y = self._centre(it)
        self._get("/tap", x=x, y=y)

    def long_press(self, label):
        x, y = self._centre(self._item(label))
        self._get("/long_press", x=x, y=y, secs=1.0)

    def double_tap(self, label):
        x, y = self._centre(self._item(label))
        self._get("/double_tap", x=x, y=y)

    def _size(self):
        root = self.screen()[0]
        if root is None:
            raise DriverError("app is not in the foreground")
        return root.get("w") or 402, root.get("h") or 874

    def swipe(self, spec):
        W, H = self._size()
        if isinstance(spec, dict):
            (x1, y1), (x2, y2) = [tuple(int(v) for v in spec[k].split(",")) for k in ("from", "to")]
        else:
            cx, cy = W // 2, H // 2
            x1, y1, x2, y2 = {"up": (cx, int(H * .7), cx, int(H * .3)), "down": (cx, int(H * .3), cx, int(H * .7)),
                              "left": (int(W * .85), cy, int(W * .15), cy), "right": (int(W * .15), cy, int(W * .85), cy)}[spec]
        self._get("/swipe", x1=x1, y1=y1, x2=x2, y2=y2, secs=0.05)

    def scroll(self, direction):
        # "scroll down" = reveal content further down = finger moves UP
        self.swipe("up" if direction == "down" else "down")

    def back(self):
        items = self.screen()[2]
        btn = next((i for i in items if re.fullmatch(r"(back|close|cancel|done)", i["label"].lower())), None)
        if btn:
            self.tap(btn["label"])
        else:  # iOS interactive-pop gesture from the left edge
            W, H = self._size()
            self._get("/swipe", x1=2, y1=H // 2, x2=int(W * .7), y2=H // 2, secs=0.05)

    def key(self, name, times=1, delay=0.35):
        if name != "home":
            raise DriverError(f"iOS has no '{name}' key under XCUITest (only 'home'); use tap / swipe")
        self._get("/key", name="home")

    # ---------- text ----------
    def focus_field(self, label=None):
        _, _, items, _ = self.screen()
        field = self.find(items, label) if label else next((i for i in items if i.get("editable")), None)
        if not field:
            raise DriverError("no text field on screen")
        x, y = self._centre(field)
        self._get("/tap", x=x, y=y)
        time.sleep(0.8)
        return field

    def type_text(self, text, submit=True, clear=False, field=None):
        self.focus_field(field)
        if clear:
            self.delete("all")
        self._get("/type", text=text)
        if submit:
            self._get("/submit")

    def delete(self, n):
        field = next((i for i in self.screen()[2] if i.get("editable")), None)
        count = (len((field or {}).get("text", "")) + 5) if n == "all" else int(n)
        self._get("/delete", count=max(1, count))

    def _edit_menu(self, item_label):
        """Raise the system edit menu on the focused field and pick an item (Select All / Copy / Cut / Paste).
        Which gesture raises it depends on keyboard / caret state, so try long-press, tap, double-tap in turn."""
        field = self.focus_field()
        x, y = self._centre(field)
        for gesture in ("long_press", "tap", "double_tap"):
            if gesture == "long_press":
                self._get("/long_press", x=x, y=y, secs=1.0)
            else:
                self._get("/" + gesture, x=x, y=y)
            time.sleep(0.9)
            btn = next((i for i in self.screen()[2] if i["label"].lower() == item_label.lower()), None)
            if btn:
                self.tap(btn["label"])
                return True
        return False

    def _shown(self):
        """What the first text field displays. Compose puts typed text in the label, not in `value`."""
        f = next((i for i in self.screen()[2] if i.get("editable")), None)
        return (f["label"] if f else "")

    def paste(self, text=None):
        if text is not None:  # the simulator pasteboard is settable from the host: a REAL paste, not typing
            subprocess.run(["xcrun", "simctl", "pbcopy", self.udid], input=text.encode(), timeout=10, check=True)
            time.sleep(0.5)
        before = self._shown()
        ok = self._edit_menu("Paste")
        time.sleep(0.8)
        after = self._shown()
        if text is not None and (not ok or after == before):
            self.notes.append("paste NOT exercised: the Paste menu item did not appear or changed nothing; text was typed instead")
            self._get("/type", text=text)

    # ---------- dispatch ----------
    KEYBOARD_MARKERS = ("Emoji", "Dictate")

    def keyboard_up(self):
        return any(i["label"] in self.KEYBOARD_MARKERS for i in self.screen()[2])

    def dismiss_keyboard(self):
        if not self.keyboard_up():
            return
        self._get("/submit")  # the Return key resigns the field (verified; tapping the nav bar does not in Compose screens)
        time.sleep(0.8)

    def perform(self, verb, value):
        if verb == "dismiss_keyboard":
            self.dismiss_keyboard()
        elif verb == "background":
            self.key("home")
        elif verb == "key":
            from .actions import expand_keys
            for name, delay in expand_keys(value):
                self.key(name)
        elif verb == "tap_xy":
            x, y = (v.strip() for v in str(value).split(","))
            self._get("/tap", x=x, y=y)
        elif verb == "long_press":
            self.long_press(str(value))
        elif verb == "double_tap":
            self.double_tap(str(value))
        elif verb == "swipe":
            self.swipe(value)
        elif verb == "select_all":
            if not self._edit_menu("Select All"):
                raise DriverError("the system edit menu did not offer 'Select All'")
        elif verb in ("copy", "cut"):
            if not self._edit_menu(verb.capitalize()):
                raise DriverError(f"the system edit menu did not offer '{verb.capitalize()}'")
        elif verb == "paste":
            self.paste(None if value is None else str(value))
        elif verb == "rotate":
            self._get("/rotate", to=str(value))
        elif verb == "font_scale":
            cat = next(name for lim, name in SIZES if float(value) <= lim)
            subprocess.run(["xcrun", "simctl", "ui", self.udid, "content_size", cat], capture_output=True, check=True)
        elif verb in ("network", "clear_data"):
            raise DriverError(f"the iOS simulator driver cannot do '{verb}' (no simctl equivalent)")
        elif verb == "hold":
            raise DriverError("iOS has no remote buttons to hold; use long_press: <label>")
        else:
            super().perform(verb, value)
