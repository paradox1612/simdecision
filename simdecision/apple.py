"""tvOS driver: the runtime's hands on the Apple TV simulator, through the QABridge XCUITest server.

  runtime (python) --HTTP 127.0.0.1:QA_PORT--> QABridge (UI test running inside the sim) --XCUIRemote/snapshot--> app

Same surface as android.Android (screen/foreground/screenshot/log/crashes/perform/tap/...), so scenarios, goals,
route caching, oracles and invariants are shared. Only simulators are driven. Everything XCUITest cannot do
(media keys, airplane mode, wiping app data) raises DriverError so a scenario fails loudly instead of pretending.
"""
import base64, glob, json, os, re, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
from . import core as tsim  # noqa: E402
from .android import DriverError, Android, _as_list  # noqa: E402

from . import config  # noqa: E402

BUNDLE = config.app("tvos")["bundle"]
PROCESS = config.app("tvos")["process"]
PORT = int(config.app("tvos").get("bridge_port") or os.environ.get("QA_BRIDGE_PORT", "8785"))
DD = os.environ.get("QA_TVOS_DD") or config.state("build", "tvos")
# What XCUIRemote can press. Nothing else exists on the Siri Remote as far as UI tests go.
REMOTE = {"up": "up", "down": "down", "left": "left", "right": "right", "select": "select", "ok": "select",
          "enter": "select", "back": "menu", "menu": "menu", "home": "home", "play_pause": "play_pause"}


def booted_tvos():
    out = subprocess.run(["xcrun", "simctl", "list", "devices", "booted"], capture_output=True, text=True).stdout
    sims, in_tv = [], False
    for line in out.splitlines():
        if line.startswith("-- "):
            in_tv = "tvOS" in line
        m = re.search(r"^\s+(.*?) \(([0-9A-F-]{36})\) \(Booted\)", line)
        if m and in_tv:
            sims.append((m.group(1), m.group(2)))
    return sims


class AppleTV:
    platform = "tvos"
    pkg = BUNDLE
    PORT = PORT
    DD = DD
    TESTID = "QABridgeTVUITests/QABridge/testServe"
    PROCESS = PROCESS
    _bridge_proc = None

    COMMIT = ("next", "done", "go", "search", "return", "join", "send", "continue")

    def __init__(self, platform="tvos"):
        self.notes = []
        self.entry = ""  # what we typed into the open system keyboard (its entry strip is not in the tree)
        self.name, self.udid = self._pick_sim()
        self.log_since = time.time()
        self._ensure_bridge()

    def _pick_sim(self):
        want = config.app("tvos").get("udid")
        if want:
            return next((s for s in booted_tvos() if s[1] == want), None) or (_ for _ in ()).throw(
                DriverError(f"configured tvOS simulator {want} is not booted (xcrun simctl boot {want})"))
        sims = booted_tvos()
        if not sims:
            raise DriverError("no tvOS simulator is booted. Boot one: xcrun simctl boot <udid> (see xcrun simctl list devices available) "
                              "(Apple TV 4K 3rd gen). The app must be installed on it.")
        return sims[0]

    # ---------- bridge lifecycle ----------
    def _get(self, path, timeout=60, **params):
        url = f"http://127.0.0.1:{self.PORT}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                data = json.load(r)
        except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError) as e:
            raise DriverError(f"tvOS bridge unreachable ({e}); is the QABridge test running?")
        if not data.get("ok", False):
            raise DriverError(f"bridge: {data.get('error', data)}")
        return data

    def _alive(self):
        """A bridge answers AND drives our app. A bridge on this port serving ANOTHER app may belong to another project
        or session: never stop it, refuse and say how to pick a different port."""
        try:
            d = self._get("/ping", timeout=3)
        except DriverError:
            return False
        if d.get("bundle") and d["bundle"] != self.pkg:
            raise DriverError(f"port {self.PORT} already serves a bridge for {d['bundle']} (not {self.pkg}); "
                              f"set app.{self.platform}.bridge_port in simdecision.yaml to another port")
        return True

    def _ensure_bridge(self):
        if self._alive():
            return
        xr = sorted(glob.glob(os.path.join(self.DD, "Build", "Products", "*.xctestrun")))
        if not xr:
            raise DriverError(f"bridge test bundle not built (looked in {self.DD}/Build/Products). Run `simdecision prepare {self.platform}` (or bridge/*_build.sh) first.")
        env = {**os.environ, "TEST_RUNNER_QA_PORT": str(self.PORT), "TEST_RUNNER_QA_BUNDLE_ID": self.pkg}
        logf = open(config.state("build", f"bridge-{self.platform}.log"), "w")
        AppleTV._bridge_proc = subprocess.Popen(
            ["xcodebuild", "test-without-building", "-xctestrun", xr[-1], "-destination", f"id={self.udid}",
             f"-only-testing:{self.TESTID}"], env=env, stdout=logf, stderr=subprocess.STDOUT)
        for _ in range(90):
            time.sleep(2)
            if self._alive():
                return
            if AppleTV._bridge_proc.poll() is not None:
                raise DriverError("bridge test exited early; see <state_dir>/build/bridge-*.log")
        raise DriverError("bridge did not come up within 3 min; see <state_dir>/build/bridge-*.log")

    def close(self):
        try:
            self._get("/quit", timeout=5)
        except DriverError:
            pass

    # ---------- observation ----------
    def screen(self):
        d = self._get("/screen")
        items = []
        for it in d.get("items", []):
            label = (it.get("label") or "").strip()[:60] or "(unlabelled)"
            x, y, w, h = it["x"], it["y"], it["w"], it["h"]
            editable = bool(it.get("editable"))
            if editable:
                label = f'[text field] {it.get("text") or label}'[:60]
            items.append({"label": label, "id": it.get("id", ""), "box": (x, y, x + w, y + h),
                          "focused": bool(it.get("focused")), "scrollable": False, "editable": editable,
                          "text": it.get("text", "")})
        # The system keyboard is ~40 key buttons. Offer ONE text-entry target instead: the model decides to type
        # a declared input, the bridge types it through XCUITest, and "next" / "Uppercase Letters" etc. stay as items.
        kb = next((i for i in items if i["label"] == "Keyboard"), None)
        if kb:
            hint = (d.get("texts") or ["keyboard input"])[0]
            items = [i for i in items if len(i["label"]) > 1 and i["label"] not in ("Keyboard", "SPACE", "delete", "ampersand")]
            items.append({"label": f"[text field] {hint}"[:60], "box": kb["box"], "focused": False, "scrollable": False,
                          "editable": True, "text": self.entry, "keyboard": True})
        else:
            self.entry = ""
        focused = next((i for i in items if i["focused"]), None)
        if d.get("state") != "foreground":
            return None, f"(app is {d.get('state')})", [], None
        lines = []
        for it in sorted(items, key=lambda i: (i["box"][1], i["box"][0])):
            lines.append(f'{">>" if it["focused"] else "  "} "{it["label"]}" @{it["box"][0]},{it["box"][1]}')
        # NOTE: self.entry is driver bookkeeping, deliberately NOT part of the text oracles read - a check must
        # never be satisfied by the driver's own record of what it did, only by what the app shows.
        text = "TEXT: " + " | ".join(d.get("texts", [])[:12]) + "\nFOCUSABLE (>> = focused):\n" + "\n".join(lines[:60])
        return d, text, items, focused

    def owns(self, foreground):
        return foreground == self.pkg

    def foreground(self):
        st = self._get("/state").get("state")
        return self.pkg if st == "foreground" else f"app:{st}"

    def screenshot(self, path):
        # The test runner is sandboxed (cannot write host paths), so the PNG comes back as base64.
        with open(path, "wb") as f:
            f.write(base64.b64decode(self._get("/screenshot", timeout=60)["base64"]))
        return path

    def screenshot_pair(self, gap=1.0):
        def one():
            with tempfile.TemporaryDirectory() as t:
                png, jpg = os.path.join(t, "s.png"), os.path.join(t, "s.jpg")
                self.screenshot(png)
                subprocess.run(["sips", "-Z", "640", "-s", "format", "jpeg", png, "--out", jpg], capture_output=True, check=True)
                return "data:image/jpeg;base64," + base64.b64encode(open(jpg, "rb").read()).decode()
        a = one(); time.sleep(gap)
        return [a, one()]

    # Logs: Apple has no "clear". We remember when the run started and only look at lines after it.
    def logcat_clear(self):
        self.log_since = time.time()

    def log(self, regex=None, buffers=None):
        start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.log_since))
        out = subprocess.run(["xcrun", "simctl", "spawn", self.udid, "log", "show", "--start", start, "--style", "compact",
                              "--predicate", f'process == "{self.PROCESS}"'], capture_output=True, text=True, timeout=60).stdout
        lines = out.splitlines()
        if regex:
            rx = re.compile(regex)
            lines = [l for l in lines if rx.search(l)]
        return lines

    def crashes(self):
        """New crash reports for the app since the run started (host DiagnosticReports holds the simulator's)."""
        found = []
        for f in glob.glob(os.path.expanduser(f"~/Library/Logs/DiagnosticReports/{self.PROCESS}*.ips")):
            if os.path.getmtime(f) >= self.log_since:
                found.append(f"crash report {os.path.basename(f)}")
        return found[:5]

    # ---------- actions ----------
    find = staticmethod(Android.find)
    arrived = staticmethod(Android.arrived)

    def launch(self, fresh=True):
        self._get("/launch", timeout=90)
        for _ in range(30):
            time.sleep(1.5)
            if len(self.screen()[2]) >= 3:
                return
        raise DriverError("app launched but no focusable screen appeared within 45s")

    def stop(self):
        self._get("/terminate")

    def key(self, name, times=1, delay=0.35):
        if name not in REMOTE:
            raise DriverError(f"the Siri Remote has no '{name}' button in XCUITest (available: {', '.join(sorted(set(REMOTE)))})")
        self._get("/key", name=REMOTE[name], times=times, delay=delay)

    def back(self):
        self.key("menu")

    def scroll(self, direction):
        self.key("down" if direction == "down" else "up", times=3, delay=0.25)

    def tap(self, label, max_moves=30, press=True):
        """Steer D-pad focus onto the labelled item (code does the how), then press select."""
        root, text, items, focused = self.screen()
        if root is None:
            raise DriverError("cannot tap: app not in the foreground")
        if press and label.strip().lower() in self.COMMIT and any(i.get("keyboard") for i in items):
            self.commit()  # the keyboard's own next/done: not reachable by geometry, Menu does the same thing
            return
        it = self.find(items, label)
        if not it:
            raise DriverError(f'no item matching "{label}" on screen')
        tried = {}
        for _ in range(max_moves):
            if self.arrived(focused, it):
                if press:
                    self.key("select")
                return
            moves = [m for m in _as_list(tsim.toward(focused, it)) if tried.get(((focused or {}).get("label"), m), 0) < 2]
            if not moves:
                raise DriverError(f'could not steer focus to "{label}"')
            k = ((focused or {}).get("label"), moves[0])
            tried[k] = tried.get(k, 0) + 1
            self.key(moves[0])
            root, text, items, focused = self.screen()
            it = self.find(items, label) or it
        raise DriverError(f'gave up steering to "{label}" after {max_moves} moves')

    def type_text(self, text, submit=True, clear=False, field=None):
        if self._keyboard_up():
            pass  # the system keyboard already has the field: type straight away
        elif field:
            self.tap(field)  # select on a text field opens the system keyboard
            time.sleep(1.0)
        if clear:
            self.delete("all")
        self._get("/type", text=text)
        self.entry += text
        if submit:
            self.commit()

    def commit(self):
        """Finish text entry like a person: Menu closes the system keyboard and keeps the text (verified: the
        form's own text field then exposes the value). The keyboard's "next" key is not reachable by geometry."""
        self.key("menu")
        time.sleep(1.0)

    def _keyboard_up(self):
        return any(i.get("keyboard") for i in self.screen()[2])

    def delete(self, n):
        field = next((i for i in self.screen()[2] if i.get("editable")), None)
        count = (len((field or {}).get("text", "")) + 5 if n == "all" else int(n))
        self._get("/delete", count=max(1, count))
        self.entry = "" if n == "all" else self.entry[:-int(n)]

    def deeplink(self, url):
        subprocess.run(["xcrun", "simctl", "openurl", self.udid, url], capture_output=True, timeout=30, check=True)

    def perform(self, verb, value):
        if verb == "launch":
            self.launch()
        elif verb == "stop":
            self.stop()
        elif verb == "background":
            self.key("home")
        elif verb == "foreground":
            # XCUIApplication.activate() does not leave the tvOS Home screen; simctl launch on a running app foregrounds it.
            subprocess.run(["xcrun", "simctl", "launch", self.udid, self.pkg], capture_output=True, timeout=30)
            for _ in range(20):  # activate() returns before the app is actually in front
                if self._get("/state").get("state") == "foreground":
                    break
                time.sleep(0.5)
            else:
                raise DriverError("app did not come back to the foreground within 10s")
        elif verb == "deeplink":
            self.deeplink(str(value))
        elif verb == "key":
            from .actions import expand_keys
            for name, delay in expand_keys(value):
                self.key(name, delay=delay)
        elif verb == "hold":
            if value["key"] not in REMOTE:
                raise DriverError(f"cannot hold '{value['key']}' on the Siri Remote")
            self._get("/hold", name=REMOTE[value["key"]], secs=int(value.get("ms", 1000)) / 1000)
        elif verb == "back":
            self.back()
        elif verb == "tap":
            self.tap(str(value))
        elif verb == "long_press":
            self.tap(str(value), press=False)
            self._get("/hold", name="select", secs=1.2)
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
        elif verb == "appearance":
            subprocess.run(["xcrun", "simctl", "ui", self.udid, "appearance", str(value)], capture_output=True, check=True)
        elif verb == "location":
            lat, lon = (v.strip() for v in str(value).split(","))
            subprocess.run(["xcrun", "simctl", "location", self.udid, "set", f"{lat},{lon}"], capture_output=True, check=True)
        elif verb == "permission":
            subprocess.run(["xcrun", "simctl", "privacy", self.udid, value["action"], value["name"].lower(), self.pkg],
                           capture_output=True, check=True)
        elif verb == "wait":
            time.sleep(float(value))
        elif verb == "logcat_clear":
            self.logcat_clear()
        else:
            raise DriverError(f"tvos driver does not implement '{verb}' (XCUITest / simctl cannot do it)")
