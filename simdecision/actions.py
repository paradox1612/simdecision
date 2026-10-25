"""Action registry: every action a scenario can name in YAML, and which platforms implement it.

One table drives the validator (`scenario.py`), the authoring guide (MCP `schema`) and the driver dispatch
(`android.py::perform`). Adding a platform = implementing `perform` for the verbs it lists here; nothing else changes.
"""

# Android keycodes. The TV remote and a keyboard are both just keys, so one map serves both.
KEYS = {
    # D-pad + navigation
    "up": 19, "down": 20, "left": 21, "right": 22, "select": 23, "ok": 23, "enter": 66, "back": 4, "home": 3,
    "menu": 82, "recents": 187, "search": 84, "settings": 176, "info": 165, "guide": 172, "captions": 175,
    "tab": 61, "escape": 111, "page_up": 92, "page_down": 93, "move_home": 122, "move_end": 123,
    # media / remote
    "play_pause": 85, "play": 126, "pause": 127, "stop": 86, "fast_forward": 90, "rewind": 89, "next": 87,
    "previous": 88, "volume_up": 24, "volume_down": 25, "mute": 164, "channel_up": 166, "channel_down": 167,
    "dvr": 173, "bookmark": 174, "tv_input": 178, "red": 183, "green": 184, "yellow": 185, "blue": 186,
    # text editing
    "delete": 67, "forward_delete": 112, "space": 62, "copy": 278, "cut": 277, "paste": 279,
}
REMOTE_KEYS = ["up", "down", "left", "right", "select", "back", "home", "menu", "play_pause", "fast_forward",
               "rewind", "volume_up", "volume_down", "mute", "channel_up", "channel_down", "info", "guide", "captions"]

# verb -> (platforms, argument shape, description)
ACTIONS = {
    # --- app lifecycle ---
    "launch": ("phone tv", "-", "start the app fresh (force-stop first)"),
    "stop": ("phone tv", "-", "force-stop the app"),
    "background": ("phone tv", "-", "send the app to the background (Home)"),
    "foreground": ("phone tv", "-", "bring the app back to the front"),
    "clear_data": ("phone tv", "-", "wipe the app's data (needs allow_destructive: true at top level)"),
    "deeplink": ("phone tv", "url", "open a URL / deep link in the app"),
    # --- remote / keyboard keys ---
    "key": ("phone tv", "name | [names] | {name, times, delay}",
            "press key(s): " + ", ".join(sorted(KEYS))),
    "hold": ("phone tv", "{key, ms}", "press and hold a key (long-press), e.g. {key: select, ms: 1500}"),
    "back": ("phone tv", "-", "press Back"),
    # --- pointing / touch ---
    "tap": ("phone tv", "label", "tap a labelled item (TV: steers D-pad focus there, then select)"),
    "tap_xy": ("phone", "'x,y'", "tap raw screen coordinates (last resort)"),
    "long_press": ("phone tv", "label", "long-press a labelled item (TV: hold select on it)"),
    "double_tap": ("phone", "label", "double-tap a labelled item"),
    "swipe": ("phone", "up|down|left|right | {from: 'x,y', to: 'x,y', ms}", "swipe gesture"),
    "scroll": ("phone tv", "down|up", "scroll the screen"),
    # --- text ---
    "type": ("phone tv", "text | {text, field, submit, clear}", "type into a text field (field: its on-screen label; default first/focused), then Enter unless submit: false"),
    "delete": ("phone tv", "N | all", "delete N characters before the cursor, or the whole field"),
    "select_all": ("phone tv", "-", "select all text in the focused field"),
    "copy": ("phone tv", "-", "copy the selection"),
    "cut": ("phone tv", "-", "cut the selection"),
    "paste": ("phone tv", "text | -", "put text on the clipboard (host clipboard sync) and paste it; no value = paste what is there"),
    "dismiss_keyboard": ("ios", "-", "hide the on-screen keyboard (Return key) so the rest of the form is reachable"),
    # --- device state ---
    "rotate": ("phone", "portrait|landscape", "rotate the screen"),
    "appearance": ("phone tv", "dark|light", "system dark / light mode"),
    "font_scale": ("phone tv", "0.85..2.0", "system font scale"),
    "location": ("phone tv", "'lat,lon'", "set the emulator GPS fix"),
    "permission": ("phone tv", "{name, grant|revoke}", "grant/revoke a runtime permission, e.g. {name: POST_NOTIFICATIONS, action: revoke}"),
    "network": ("phone tv", "offline|online", "airplane mode"),
    "wait": ("phone tv", "seconds", "wait (1..120)"),
    # --- fixtures ---
    "fixture": ("phone tv tvos ios", "{name, get}", "control call to a running fixture, e.g. {name: api, get: '/__reset'} (any path your mock server understands)"),
    # --- evidence ---
    "logcat_clear": ("phone tv", "-", "clear log buffers so later logcat oracles only see new lines"),
    "screenshot": ("phone tv", "caption", "save a full-resolution screenshot into the report"),
}
# Verbs the tvOS driver implements (XCUIRemote + simctl). Everything else is Android-only today.
TVOS_OK = {"launch", "stop", "background", "foreground", "deeplink", "key", "hold", "back", "tap", "long_press", "scroll",
           "type", "delete", "appearance", "location", "permission", "wait", "logcat_clear", "screenshot"}
TVOS_KEYS = {"up", "down", "left", "right", "select", "ok", "enter", "back", "menu", "home", "play_pause"}
for _v in TVOS_OK:
    _p, _a, _d = ACTIONS[_v]
    ACTIONS[_v] = (_p + " tvos", _a, _d)

IOS_OK = {"launch", "stop", "background", "foreground", "deeplink", "key", "back", "tap", "tap_xy", "long_press", "double_tap",
          "swipe", "scroll", "type", "delete", "select_all", "copy", "cut", "paste", "rotate", "appearance", "font_scale",
          "location", "permission", "wait", "logcat_clear", "screenshot"}
IOS_KEYS = {"home"}
for _v in IOS_OK:
    _p, _a, _d = ACTIONS[_v]
    ACTIONS[_v] = (_p + " ios", _a, _d)

NO_ARG = {v for v, (_, a, _) in ACTIONS.items() if a == "-"}
NEEDS_ARG = set(ACTIONS) - NO_ARG - {"paste", "swipe", "type", "key"}


def validate(verb, val, platform_list):
    """-> error string or ''. Arg-shape checks only; semantics are the driver's job."""
    if verb not in ACTIONS:
        return f"unknown action '{verb}'"
    plats = ACTIONS[verb][0].split()
    bad = [p for p in platform_list if p in ("phone", "tv", "tvos", "ios") and p not in plats]
    if bad:
        return f"'{verb}' is not available on {', '.join(bad)} (available: {', '.join(plats)})"
    if verb in NEEDS_ARG and val is None:
        return f"'{verb}' needs a value ({ACTIONS[verb][1]})"
    if verb == "key":
        names = _key_names(val)
        if names is None:
            return "key: needs a name, a list of names, or {name, times}"
        unknown = [n for n in names if n not in KEYS]
        if unknown:
            return f"key: unknown {unknown} (allowed: {', '.join(sorted(KEYS))})"
        if "ios" in platform_list:
            no = [n for n in names if n not in IOS_KEYS]
            if no:
                return f"key: {no} do not exist on iOS under XCUITest (ios has: home) - use tap / swipe"
        if "tvos" in platform_list:
            no = [n for n in names if n not in TVOS_KEYS]
            if no:
                return f"key: {no} do not exist on the Siri Remote under XCUITest (tvos has: {', '.join(sorted(TVOS_KEYS))})"
    if verb == "fixture" and not (isinstance(val, dict) and val.get("name") and str(val.get("get", "")).startswith("/")):
        return "fixture needs {name: <fixture>, get: '/path?query'}"
    if verb == "hold" and "tvos" in platform_list and isinstance(val, dict) and val.get("key") not in TVOS_KEYS:
        return f"hold: '{val.get('key')}' does not exist on the Siri Remote"
    if verb == "hold" and not (isinstance(val, dict) and val.get("key") in KEYS):
        return "hold: needs {key: <name>, ms: <n>}"
    if verb == "wait" and not (isinstance(val, (int, float)) and 0 < val <= 120):
        return "wait must be 1..120 seconds"
    if verb == "network" and val not in ("offline", "online"):
        return "network must be offline|online"
    if verb == "scroll" and val not in ("down", "up"):
        return "scroll must be down|up"
    if verb == "rotate" and val not in ("portrait", "landscape"):
        return "rotate must be portrait|landscape"
    if verb == "appearance" and val not in ("dark", "light"):
        return "appearance must be dark|light"
    if verb == "delete" and not (val == "all" or (isinstance(val, int) and 0 < val <= 500)):
        return "delete must be 1..500 or 'all'"
    if verb in ("location", "tap_xy") and not (isinstance(val, str) and len(val.split(",")) == 2):
        return f"{verb} needs 'a,b'"
    if verb == "swipe" and not (val in ("up", "down", "left", "right") or isinstance(val, dict)):
        return "swipe: up|down|left|right or {from, to}"
    if verb == "permission" and not (isinstance(val, dict) and val.get("name") and val.get("action") in ("grant", "revoke")):
        return "permission needs {name, action: grant|revoke}"
    if verb == "font_scale" and not (isinstance(val, (int, float)) and 0.5 <= val <= 3):
        return "font_scale must be 0.5..3"
    return ""


def _key_names(val):
    if isinstance(val, str):
        return [val]
    if isinstance(val, list) and all(isinstance(v, str) for v in val):
        return val
    if isinstance(val, dict) and isinstance(val.get("name"), str):
        return [val["name"]]
    return None


def expand_keys(val):
    """key value -> [(name, delay_after)] in press order."""
    if isinstance(val, dict):
        return [(val["name"], float(val.get("delay", 0.4)))] * int(val.get("times", 1))
    return [(n, 0.4) for n in _key_names(val)]


def guide_table():
    rows = [f"  {v:13s} [{p}]  {a}\n{'':17s}{d}" for v, (p, a, d) in ACTIONS.items()]
    return "\n".join(rows)
