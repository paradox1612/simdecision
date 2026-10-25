"""prepare: get a platform's device lane ready to run scenarios, so no chat has to remember the recipe.

For one platform it: takes the device lane lock (so it never boots/kills a device under someone else's run), boots the
emulator / simulator if needed, checks the app under test is installed (and says when that build was installed), builds the
XCUITest bridge when it is missing or older than its source, starts the bridge, and returns a readiness report with the
fix for anything it cannot do itself (e.g. install a build). It never builds or installs the app.
"""
import glob, os, re, subprocess, time

from . import android, apple, ios
from .devicelock import device_lock

HERE = os.path.dirname(os.path.abspath(__file__))
ANDROID_HOME = os.environ.get("ANDROID_HOME", "/opt/homebrew/share/android-commandlinetools")
def _first_available(kind):
    """UDID of the first available (not necessarily booted) iPhone or Apple TV simulator."""
    out = subprocess.run(["xcrun", "simctl", "list", "devices", "available"], capture_output=True, text=True).stdout
    section = ""
    for line in out.splitlines():
        if line.startswith("-- "):
            section = line
        m = re.search(r"^\s+(.*?) \(([0-9A-F-]{36})\)", line)
        if m and (("tvOS" in section) if kind == "tvos" else ("iOS" in section and "iPhone" in m.group(1))):
            return m.group(2)
    return None


def _run(*a, timeout=60):
    return subprocess.run(list(a), capture_output=True, text=True, timeout=timeout)


def _mem_note():
    out = _run("vm_stat").stdout
    free = re.search(r"Pages free:\s+(\d+)", out)
    inactive = re.search(r"Pages inactive:\s+(\d+)", out)
    if not free:
        return ""
    gb = (int(free.group(1)) + int(inactive.group(1) if inactive else 0)) * 16384 / 1e9
    heavy = len(android.emulators()) + len(apple.booted_tvos()) + len(ios.booted_iphones())
    return f"memory: ~{gb:.1f} GB free+inactive, {heavy} emulator/simulator(s) running" + \
           ("  - LOW: boot fewer devices at once (free_others=True)" if gb < 3 else "")


def prepare(platform, rebuild_bridge=False, free_others=False):
    lines, ok = [], True

    def say(msg, good=True):
        nonlocal ok
        ok = ok and good
        lines.append(("  ok  " if good else "  !!  ") + msg)

    if platform not in ("phone", "tv", "tvos", "ios"):
        return f"unknown platform '{platform}' (phone, tv, tvos, ios)"
    with device_lock(platform, f"prepare [{platform}]", wait=120):
        if free_others:
            lines.append("  free_others: " + _free_others(platform))
        if platform in ("phone", "tv"):
            _android(platform, say)
        else:
            _apple(platform, say, rebuild_bridge)
    lines.append("  " + _mem_note())
    return f"{'READY' if ok else 'NOT READY'} {platform}\n" + "\n".join(lines)


# ---------- android ----------

def _android(platform, say):
    conf = android.config.app(platform)
    avd = conf.get("avd")
    emus = android.emulators()
    match = [s for s, a in emus.items() if a == avd] if avd else [s for s in emus if android.is_tv(s) == (platform == "tv")]
    if not match and not avd:
        return say(f"no {platform} emulator running and no app.{platform}.avd in simdecision.yaml to boot", False)
    if not match:
        if emus:  # one Android emulator at a time (a second one gets no DNS) - safe to swap: we hold the lane
            for s in emus:
                _run(android.tsim.ADB, "-s", s, "emu", "kill")
            time.sleep(8)
            say(f"stopped {', '.join(emus.values())} (one Android emulator at a time)")
        subprocess.Popen([os.path.join(ANDROID_HOME, "emulator", "emulator"), "-avd", avd, "-no-snapshot-save", "-no-audio"],
                         stdout=open("/tmp/simdecision_emulator.log", "w"), stderr=subprocess.STDOUT, start_new_session=True)
        end, booted = time.time() + 240, False
        while time.time() < end and not booted:
            time.sleep(4)
            e = android.emulators()
            m = [s for s, a in e.items() if a == avd]
            if m and _run(android.tsim.ADB, "-s", m[0], "shell", "getprop", "sys.boot_completed").stdout.strip() == "1":
                match, booted = m, True
        if not booted:
            return say(f"{avd} did not finish booting within 4 min (see /tmp/simdecision_emulator.log)", False)
        say(f"booted {avd} ({match[0]})")
    else:
        say(f"{avd or platform + ' emulator'} already running ({match[0]})")
    serial = match[0]
    _run(android.tsim.ADB, "-s", serial, "shell", "input", "keyevent", "82")  # wake / unlock
    pkg = conf["package"]
    info = _run(android.tsim.ADB, "-s", serial, "shell", "dumpsys", "package", pkg).stdout
    upd = re.search(r"lastUpdateTime=(.*)", info)
    if upd:
        say(f"app {pkg} installed (updated {upd.group(1).strip()})")
    else:
        say(f"app {pkg} NOT installed - install it (adb install <apk>)", False)


# ---------- apple ----------

def _apple(platform, say, rebuild_bridge):
    drv_cls = apple.AppleTV if platform == "tvos" else ios.IOS
    want = apple.config.app(platform).get("udid")
    booted = (apple.booted_tvos() if platform == "tvos" else ios.booted_iphones())
    if want:
        booted = [b for b in booted if b[1] == want]
    udid = booted[0][1] if booted else (want or _first_available(platform))
    if not udid:
        return say(f"no {platform} simulator available - create one in Xcode > Window > Devices and Simulators", False)
    if not booted:
        r = _run("xcrun", "simctl", "boot", udid)
        if r.returncode not in (0, 149):  # 149 = already booted
            return say(f"could not boot simulator {udid}: {r.stderr.strip()[:120]}", False)
        _run("xcrun", "simctl", "bootstatus", udid, "-b", timeout=240)
        say(f"booted simulator {udid}")
    else:
        say(f"simulator already booted: {booted[0][0]}")
    pkg = drv_cls.pkg
    r = _run("xcrun", "simctl", "get_app_container", udid, pkg)
    if r.returncode != 0:
        say(f"app {pkg} NOT installed on the simulator - build/install it (xcodebuild ... + simctl install)", False)
    else:
        mtime = time.strftime("%F %T", time.localtime(os.path.getmtime(r.stdout.strip())))
        say(f"app {pkg} installed (bundle written {mtime})")
    # bridge bundle: build when missing or older than its source
    src = os.path.join(HERE, "bridge", "QABridge.swift")
    xr = sorted(glob.glob(os.path.join(drv_cls.DD, "Build", "Products", "*.xctestrun")))
    stale = bool(xr) and os.path.getmtime(xr[-1]) < os.path.getmtime(src)
    if rebuild_bridge or not xr or stale:
        why = "forced" if rebuild_bridge else "missing" if not xr else "older than QABridge.swift"
        script = os.path.join(HERE, "bridge", "build.sh")
        b = subprocess.run([script, platform, udid, drv_cls.DD], capture_output=True, text=True, timeout=1800)
        if b.returncode != 0:
            return say(f"bridge build failed ({why}); tail: {b.stdout[-200:].strip() or b.stderr[-200:].strip()}", False)
        say(f"bridge built ({why})")
    else:
        say("bridge bundle up to date")
    try:
        d = drv_cls(platform)  # starts the bridge if it is not running
        say(f"bridge alive on port {drv_cls.PORT}, app state: {d._get('/state').get('state')}")
    except Exception as e:  # noqa: BLE001
        say(f"bridge did not start: {e}", False)


def _free_others(platform):
    """Shut down devices of OTHER lanes that nobody holds, to give the Mac memory. Never touches a held lane."""
    from . import devicelock
    held = {k for k, v in devicelock.status().items() if v}
    mine = devicelock.LANE[platform]
    done = []
    if mine != "android" and "android" not in held:
        for s in android.emulators():
            _run(android.tsim.ADB, "-s", s, "emu", "kill")
            done.append(f"android emulator {s}")
    if mine != "ios-sim" and "ios-sim" not in held:
        for n, u in ios.booted_iphones():
            _run("xcrun", "simctl", "shutdown", u)
            done.append(n)
    if mine != "tvos-sim" and "tvos-sim" not in held:
        for n, u in apple.booted_tvos():
            _run("xcrun", "simctl", "shutdown", u)
            done.append(n)
    return ("shut down " + ", ".join(done)) if done else "nothing idle to shut down"
