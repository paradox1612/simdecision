"""Fixtures: local servers a scenario starts, points the app at, and reads back as network evidence.

    fixtures:
      - {kind: http, name: api, routes: {"/items": {"json": [{"id": 1}]}}}        # built-in mock (fixture_servers/mock_http.py)
      - {kind: command, name: mine, cmd: [python3, my_mock.py, --port, "{port}", --log, "{log}"],
         fields: {url: "{base}/manifest.json"}}                                    # bring your own server

Each fixture gets a free port. The address advertised to the app is the host the platform can reach: the Android emulator
sees the host machine as 10.0.2.2, the Apple simulators share its loopback. A fixture writes one JSON object per request to
{log}, so `requests_has` / `requests_lacks` / `until_requests` can assert on what the app actually called.
"""
import json, os, re, socket, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
HOSTS = {"phone": "10.0.2.2", "tv": "10.0.2.2", "tvos": "127.0.0.1", "ios": "127.0.0.1"}

# kind -> fields a scenario may bind with {fixture: name, field: ...}; `command` fixtures declare their own `fields`.
FIELDS = {"http": ["base"], "command": ["base"]}
KINDS = set(FIELDS)


class FixtureError(RuntimeError):
    pass


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port, proc, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            raise FixtureError(f"fixture server exited early (code {proc.returncode})")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.2)
    raise FixtureError(f"fixture server did not listen on {port} within {timeout}s")


class Fixtures:
    def __init__(self, specs, platform, rundir):
        self.specs, self.platform, self.dir = specs, platform, os.path.join(rundir, "fixtures")
        self.host = HOSTS.get(platform, "127.0.0.1")
        self.procs, self.info = {}, {}  # name -> Popen ; name -> {kind, port, base, fields, log, parse}

    # ---------- lifecycle ----------
    def start(self):
        os.makedirs(self.dir, exist_ok=True)
        values = {}
        for spec in self.specs:
            name, kind = spec["name"], spec["kind"]
            port = _free_port()
            base = f"http://{self.host}:{port}"
            log = os.path.join(self.dir, f"{name}.requests.jsonl")
            out = open(os.path.join(self.dir, f"{name}.stdout.log"), "w")
            cmd, fields, parse = self._command(kind, spec, port, base, log)
            proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, cwd=spec.get("cwd") or None)
            self.procs[name] = proc
            try:
                _wait_port(port, proc)
            except FixtureError as e:
                self.stop()
                raise FixtureError(f"fixture '{name}' ({kind}): {e}")
            self.info[name] = {"kind": kind, "port": port, "base": base, "log": log,
                               "stdout": os.path.join(self.dir, f"{name}.stdout.log"), "parse": parse}
            values[name] = {"base": base, **fields}
        return values

    def _command(self, kind, spec, port, base, log):
        if kind == "http":
            cmd = [PY, os.path.join(HERE, "fixture_servers", "mock_http.py"), "--port", str(port), "--log", log,
                   "--routes", json.dumps(spec.get("routes", {}))]
            return cmd, {}, "jsonl"
        if kind == "command":
            sub = {"port": str(port), "base": base, "log": log, "host": self.host}
            cmd = [str(a).format(**sub) for a in spec["cmd"]]
            return cmd, {k: str(v).format(**sub) for k, v in (spec.get("fields") or {}).items()}, "jsonl"
        raise FixtureError(f"unknown fixture kind '{kind}'")

    def stop(self):
        for p in self.procs.values():
            if p.poll() is None:
                p.terminate()
        for p in self.procs.values():
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    # ---------- evidence ----------
    def requests(self, name):
        """Normalised request list for a fixture: one searchable string per request."""
        info = self.info.get(name)
        if not info:
            raise FixtureError(f"no fixture named '{name}'")
        out = []
        if info["parse"] == "jsonl":
            try:
                for line in open(info["log"]):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                        out.append(" ".join(f"{k}={v}" for k, v in r.items() if k != "t"))
                    except ValueError:
                        out.append(line)
            except OSError:
                pass
        return out

    def count(self, name, needle):
        return sum(needle.lower() in r.lower() for r in self.requests(name))

    def get(self, name, path):
        """Control call to a running fixture (a path your mock server understands), e.g. to change its data mid-run."""
        info = self.info.get(name)
        if not info:
            raise FixtureError(f"no fixture named '{name}'")
        with urllib.request.urlopen(f"http://127.0.0.1:{info['port']}{path}", timeout=15) as r:
            return r.read().decode("utf-8", "replace")[:500]

    def summary(self):
        lines = []
        for name, info in self.info.items():
            reqs = self.requests(name)
            kinds = {}
            for r in reqs:
                m = re.search(r"/[\w./-]*", r)
                key = m.group(0) if m else r[:30]
                kinds[key] = kinds.get(key, 0) + 1
            top = ", ".join(f"{k} x{v}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])[:5]) or "no requests"
            lines.append(f"fixture {name} ({info['kind']}): {len(reqs)} requests - {top}")
        return lines
