"""Machine-wide device locks: one run per device lane across ALL chats / server processes.

Every chat that registers the MCP starts its own server process, so an in-process lock is not enough: two chats could
drive the same emulator at once or fight over a bridge port. flock() is held by the OS for the life of the process, so a
crashed run can never leave a stale lock. A sidecar file says who holds the lane so the waiter's message is useful.

Lanes: the two Android AVDs share ONE lane (a second emulator gets no DNS on this Mac), tvOS sim and iOS sim have one each.
"""
import contextlib, fcntl, json, os, time

LOCK_DIR = os.environ.get("QA_LOCK_DIR", "/tmp/simdecision-locks")
LANE = {"phone": "android", "tv": "android", "tvos": "tvos-sim", "ios": "ios-sim"}
DEFAULT_WAIT = float(os.environ.get("QA_LOCK_WAIT", "300"))


class DeviceBusy(RuntimeError):
    pass


def holder(lane):
    try:
        return json.load(open(os.path.join(LOCK_DIR, f"{lane}.holder")))
    except (OSError, ValueError):
        return None


def status():
    """{lane: holder-info | None} for every lane (None = free)."""
    out = {}
    for lane in sorted(set(LANE.values())):
        os.makedirs(LOCK_DIR, exist_ok=True)
        fd = os.open(os.path.join(LOCK_DIR, f"{lane}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            out[lane] = None
        except OSError:
            out[lane] = holder(lane) or {"label": "unknown"}
        finally:
            os.close(fd)
    return out


@contextlib.contextmanager
def device_lock(platform, label, wait=None):
    lane = LANE.get(platform, platform)
    wait = DEFAULT_WAIT if wait is None else wait
    os.makedirs(LOCK_DIR, exist_ok=True)
    fd = os.open(os.path.join(LOCK_DIR, f"{lane}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    end, announced = time.time() + wait, False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() >= end:
                h = holder(lane) or {}
                os.close(fd)
                raise DeviceBusy(f"device lane '{lane}' is busy: {h.get('label', 'another run')} (pid {h.get('pid', '?')}, "
                                 f"running {int(time.time() - h.get('since', time.time()))}s). Waited {int(wait)}s; "
                                 f"retry later or raise QA_LOCK_WAIT.")
            if not announced:
                print(f"[qa] waiting for device lane '{lane}' (held by {(holder(lane) or {}).get('label', '?')})", flush=True)
                announced = True
            time.sleep(1.0)
    json.dump({"label": label, "pid": os.getpid(), "since": time.time()}, open(os.path.join(LOCK_DIR, f"{lane}.holder"), "w"))
    try:
        yield lane
    finally:
        try:
            os.remove(os.path.join(LOCK_DIR, f"{lane}.holder"))
        except OSError:
            pass
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
