"""Fast Android screen snapshots through a persistent on-device helper (android_snap/Snap.java).

`uiautomator dump` starts a fresh JVM, connects to the accessibility service and waits for idle on every call (about
2.2 s on an emulator). The helper connects once and answers over a forwarded local socket in a few milliseconds, with
the same XML. Android allows ONE UiAutomation client per device, so while the helper runs `uiautomator dump` (ours or
any other tool's) fails: the helper exits after IDLE_EXIT seconds without a request, `stop()` frees the device at the
end of a run, and any failure here makes core.snapshot() fall back to `uiautomator dump`.

Disable with SIMDECISION_FAST_SNAPSHOT=0.
"""
import os, socket, subprocess, time

from . import config

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "android_snap", "Snap.java")
REMOTE = "/data/local/tmp/simdecision-snap.jar"
NAME = "simdecision-snap"
IDLE_EXIT = int(os.environ.get("SIMDECISION_SNAP_IDLE_EXIT", "60"))
COOLDOWN = 60.0  # after a failure, use uiautomator for this long before trying the helper again

_state = {}  # serial -> {"port": int, "proc": Popen}
_failed_at = {}


class SnapError(RuntimeError):
    pass


def enabled():
    return os.environ.get("SIMDECISION_FAST_SNAPSHOT", "1") != "0"


def _jar():
    """Build the helper jar once (and again when Snap.java changes); cached in the state dir."""
    out = config.state("build", "simdecision-snap.jar")
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(SRC):
        return out
    r = subprocess.run([os.path.join(HERE, "android_snap", "build.sh"), out], capture_output=True, text=True, timeout=180)
    if r.returncode != 0 or not os.path.exists(out):
        raise SnapError("could not build the snapshot helper: " + (r.stderr or r.stdout)[-300:])
    return out


def _request(port, cmd, timeout=10):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall((cmd + "\n").encode())
        chunks = []
        while True:
            b = s.recv(1 << 16)
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks).decode("utf-8", "replace")
    finally:
        s.close()


def _alive(adb, serial):
    st = _state.get(serial)
    if not st or (st["proc"] and st["proc"].poll() is not None):
        return None
    try:
        return st["port"] if _request(st["port"], "ping", timeout=2).startswith("pong") else None
    except OSError:
        return None


def ensure(adb, serial):
    """-> forwarded host port of a running helper on `serial` (starts it if needed)."""
    port = _alive(adb, serial)
    if port:
        return port
    stop(adb, serial)
    jar = _jar()
    subprocess.run([adb, "-s", serial, "push", jar, REMOTE], capture_output=True, timeout=30, check=True)
    proc = subprocess.Popen(
        [adb, "-s", serial, "shell",
         f"CLASSPATH={REMOTE}:/system/framework/uiautomator.jar app_process /system/bin dev.simdecision.Snap {NAME} {IDLE_EXIT}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    end = time.time() + 15
    line = ""
    while time.time() < end and proc.poll() is None:
        line = proc.stdout.readline()
        if line.startswith("READY"):
            break
    if not line.startswith("READY"):
        proc.kill()
        raise SnapError(f"snapshot helper did not start: {line.strip()[:200]}")
    out = subprocess.run([adb, "-s", serial, "forward", "tcp:0", f"localabstract:{NAME}"],
                         capture_output=True, text=True, timeout=10).stdout.strip()
    _state[serial] = {"port": int(out), "proc": proc}
    return int(out)


def dump(adb, serial):
    """-> hierarchy XML string, or None when the helper is unavailable (caller falls back to uiautomator)."""
    if not enabled() or time.time() - _failed_at.get(serial, 0) < COOLDOWN:
        return None
    try:
        xml = _request(ensure(adb, serial), "dump")
        if "</hierarchy>" not in xml:
            raise SnapError("truncated dump")
        return xml
    except (OSError, SnapError, subprocess.SubprocessError, ValueError):
        _failed_at[serial] = time.time()
        stop(adb, serial)  # free the single UiAutomation slot so the uiautomator fallback works
        return None


def stop(adb, serial):
    """Quit the helper and remove the port forward. Safe to call when nothing runs."""
    st = _state.pop(serial, None)
    if not st:
        return
    try:
        _request(st["port"], "quit", timeout=3)
    except OSError:
        pass
    subprocess.run([adb, "-s", serial, "forward", "--remove", f"tcp:{st['port']}"], capture_output=True, timeout=10)
    if st["proc"] and st["proc"].poll() is None:
        try:
            st["proc"].wait(timeout=3)
        except subprocess.TimeoutExpired:
            st["proc"].kill()


def stop_all(adb):
    for serial in list(_state):
        stop(adb, serial)
