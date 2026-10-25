"""Project config: which app to drive on each platform, and where run state lives.

Looked up in $SIMDECISION_CONFIG, else ./simdecision.yaml. Every key has a default, and the defaults drive each OS's
built-in Settings app, so the examples run on a stock emulator/simulator with nothing installed.

    app:
      phone: {package: com.example.app, activity: com.example.app.MainActivity, avd: Pixel_8_API_36,
              also: [com.example.app.helper]}   # extra packages that count as "the app" (optional)
      tv:    {package: com.example.tv, activity: com.example.tv.MainActivity, avd: Android_TV_1080p}
      ios:   {bundle: com.example.app, process: Example, udid: <simulator udid>, bridge_port: 8786}
      tvos:  {bundle: com.example.app, process: ExampleTV}
    state_dir: .simdecision          # routes, screen graphs, reports, bridge build, spend ledger

`avd` / `udid` are optional: without them the first running emulator / booted simulator of the right kind is used.
"""
import copy, os

DEFAULTS = {
    "app": {
        "phone": {"package": "com.android.settings", "activity": "com.android.settings.Settings", "avd": "",
                  "also": ["com.google.android.settings.intelligence"]},  # Settings search runs in its own package
        "tv": {"package": "com.android.tv.settings", "activity": "com.android.tv.settings.MainSettings", "avd": ""},
        "ios": {"bundle": "com.apple.Preferences", "process": "Preferences"},
        "tvos": {"bundle": "com.apple.TVSettings", "process": "TVSettings"},
    },
    "state_dir": ".simdecision",
}

_cfg = None


def _merge(base, over):
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load():
    global _cfg
    if _cfg is None:
        cfg = copy.deepcopy(DEFAULTS)
        path = os.environ.get("SIMDECISION_CONFIG") or os.path.join(os.getcwd(), "simdecision.yaml")
        if os.path.exists(path):
            import yaml
            _merge(cfg, yaml.safe_load(open(path)) or {})
        cfg["state_dir"] = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(path)), cfg["state_dir"]))
        _cfg = cfg
    return _cfg


def app(platform):
    return load()["app"].get(platform, {})


def state(*parts):
    """A path inside the state dir (created on demand)."""
    p = os.path.join(load()["state_dir"], *parts)
    os.makedirs(os.path.dirname(p) if os.path.splitext(p)[1] else p, exist_ok=True)
    return p
