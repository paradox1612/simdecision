"""Core helpers: Android screen snapshots -> compact text, D-pad geometry, and the provider-neutral decision call.

decide() asks a typed decision model (OpenAI Decisions or Cloudflare Workers AI "Clef") to pick among options the
code enumerated; both providers are multimodal and a failed call falls back to the other when both are configured.
"""
import argparse, base64, json, os, re, subprocess, sys, time, urllib.error, urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ADB = os.path.join(os.environ.get("ANDROID_HOME", "/opt/homebrew/share/android-commandlinetools"),
                   "platform-tools", "adb")
PLATFORM, PKG, PKGS_OK = "phone", "", set()  # bound per run by the Android driver from simdecision.yaml
KEYS = {"up": 19, "down": 20, "left": 21, "right": 22, "select": 23, "back": 4}
CLEF_USD_PER_TOKEN = 0.24 / 1_000_000


def load_env():
    """Read KEY=VALUE lines from $SIMDECISION_ENV or ./.env (never overrides the real environment)."""
    path = os.environ.get("SIMDECISION_ENV") or os.path.join(os.getcwd(), ".env")
    if os.path.exists(path):
        for line in open(path):
            k, _, v = line.strip().partition("=")
            if k and v:
                os.environ.setdefault(k, v)


def serial():
    if os.environ.get("TSIM_SERIAL"):
        return os.environ["TSIM_SERIAL"]
    out = subprocess.run([ADB, "devices"], capture_output=True, text=True).stdout
    emus = [l.split()[0] for l in out.splitlines() if l.startswith("emulator-") and "\tdevice" in l]
    if not emus:
        sys.exit("no emulator attached (refusing to drive a physical device)")
    return emus[0]


def adb(*args, timeout=20):
    return subprocess.run([ADB, "-s", SERIAL, *args], capture_output=True, timeout=timeout)


# ---------- screen → compact text ----------

def bounds(s):
    x1, y1, x2, y2 = map(int, re.findall(r"\d+", s))
    return x1, y1, x2, y2


def label_of(node):
    own = (node.get("text") or node.get("content-desc") or "").strip()
    if own:
        return own
    parts = []
    for d in node.iter():
        t = (d.get("text") or d.get("content-desc") or "").strip()
        if t and t not in parts:
            parts.append(t)
        if len(parts) >= 3:
            break
    return " · ".join(parts)


def snapshot():
    for _ in range(3):
        try:
            r = adb("exec-out", "uiautomator", "dump", "/dev/tty", timeout=15)
            raw = r.stdout.decode("utf-8", "replace")
            xml = raw[raw.find("<?xml"): raw.rfind("</hierarchy>") + len("</hierarchy>")]
            if xml:
                return ET.fromstring(xml)
        except (subprocess.TimeoutExpired, ET.ParseError):
            pass
        time.sleep(0.5)
    return None


def compact(root):
    """Focusable elements + standalone texts, focused one marked. Returns (text, focused, items)."""
    W, H = bounds(root.find("node").get("bounds"))[2:]
    items, texts, focused = [], [], None
    for n in root.iter("node"):
        if n.get("package") not in (PKGS_OK or {PKG}) and n.get("package") is not None:
            continue
        x1, y1, x2, y2 = bounds(n.get("bounds", "[0,0][0,0]"))
        if x2 <= x1 or y2 <= y1 or x2 < 0 or y2 < 0 or x1 > W or y1 > H:
            continue
        interactive = n.get("focusable") == "true" or n.get("focused") == "true"
        if PLATFORM == "phone":
            interactive = n.get("clickable") == "true" or n.get("scrollable") == "true"
        if interactive:
            it = {"label": label_of(n)[:60] or "(unlabelled)", "box": (x1, y1, x2, y2),
                  "focused": n.get("focused") == "true" and PLATFORM == "tv",
                  "scrollable": n.get("scrollable") == "true",
                  "editable": "EditText" in (n.get("class") or ""), "text": (n.get("text") or "").strip()}
            if it["editable"]:  # don't let a typed query masquerade as the result it searched for
                it["label"] = f'[text field] {it["text"] or label_of(n)}'[:60]
            if PLATFORM == "phone" and it["scrollable"] and n.get("clickable") != "true":
                continue  # list containers: reachable via scroll actions, not tap targets
            items.append(it)
            if it["focused"]:
                focused = it
        elif (n.get("text") or "").strip():
            texts.append(((y1, x1), n.get("text").strip()[:60]))
    texts.sort()
    lines = []
    for it in sorted(items, key=lambda i: (i["box"][1], i["box"][0])):
        mark = ">>" if it["focused"] else "  "
        lines.append(f'{mark} "{it["label"]}" @{it["box"][0]},{it["box"][1]}')
    head = [t for _, t in texts[:12]]
    kind = "FOCUSABLE (>> = focused)" if PLATFORM == "tv" else "TAPPABLE"
    text = "TEXT: " + " | ".join(head) + f"\n{kind}:\n" + "\n".join(lines[:60])
    return text, focused, items


def neighbours(focused, items, direction):
    """Focusables lying in a D-pad direction, nearest first (first = where focus most likely lands)."""
    if not focused:
        return []
    fx1, fy1, fx2, fy2 = focused["box"]
    fcx, fcy = (fx1 + fx2) / 2, (fy1 + fy2) / 2
    ranked = []
    for it in items:
        if it is focused:
            continue
        x1, y1, x2, y2 = it["box"]
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        dx, dy = cx - fcx, cy - fcy
        ok = {"right": x1 >= fx2 - 4, "left": x2 <= fx1 + 4,
              "down": y1 >= fy2 - 4, "up": y2 <= fy1 + 4}[direction]
        if not ok:
            continue
        main, cross = (abs(dx), abs(dy)) if direction in ("left", "right") else (abs(dy), abs(dx))
        ranked.append((main + 3 * cross, it))
    ranked.sort(key=lambda t: t[0])
    return [it for _, it in ranked]


def toward(focused, target):
    """Greedy D-pad step from the focused item toward the target's centre (dominant axis first)."""
    if not focused:
        return "left"
    fx1, fy1, fx2, fy2 = focused["box"]
    tx1, ty1, tx2, ty2 = target["box"]
    dx = (tx1 + tx2) / 2 - (fx1 + fx2) / 2
    dy = (ty1 + ty2) / 2 - (fy1 + fy2) / 2
    horiz = "right" if dx > 0 else "left"
    vert = "down" if dy > 0 else "up"
    same_row = ty1 < fy2 and ty2 > fy1
    same_col = tx1 < fx2 and tx2 > fx1
    # TV grids: get into the target's row first, then move along it.
    order = [horiz, vert] if same_row else [vert, horiz]
    return [a for a in order if not (a == horiz and dx == 0) and not (a == vert and dy == 0)] or [horiz]


# ---------- Clef ----------

class ClefError(RuntimeError):
    pass


PROVIDER = os.environ.get("TSIM_PROVIDER", "clef")
OPENAI_USD_PER_TOKEN = float(os.environ.get("OPENAI_USD_PER_MTOK", "0.10")) / 1_000_000
SPEND_FILE = os.environ.get("TSIM_SPEND_FILE") or os.path.join(os.getcwd(), ".simdecision", "openai_spend.json")


def decide(state, questions, images=None, model="clef"):
    """Provider-neutral System-1 call. Questions/answers use Clef's shape; adapters translate."""
    # Both providers are multimodal (images ride in the same request). TSIM_FALLBACK=1 (default) retries a failed call
    # (quota 429, budget, HTTP error) on the other provider, so one exhausted free tier never stops a run.
    chain = [PROVIDER] + ([("clef" if PROVIDER == "openai" else "openai")]
                          if os.environ.get("TSIM_FALLBACK", "1") == "1" and _have_creds("clef" if PROVIDER == "openai" else "openai") else [])
    last = None
    for prov in chain:
        try:
            t0 = time.time()
            out = openai_decide(state, questions, images) if prov == "openai" else clef(state, questions, images, model)
            LAT.append((prov, time.time() - t0))
            return out
        except ClefError as e:
            last = e
    raise last


LAT = []  # (provider, seconds) per model request: bench evidence for "one request per decision"


def _have_creds(prov):
    return bool(os.environ.get("OPENAI_API_KEY")) if prov == "openai" else bool(os.environ.get("CLOUDFLARE_API_TOKEN") or os.environ.get("CF_API_TOKEN"))


def openai_decide(state, questions, images=None):
    """OpenAI Decisions API (gpt-6-luna): noul→predicate, choice criteria→choices, score criteria→levels."""
    budget = float(os.environ.get("OPENAI_BUDGET_USD", "2.50"))
    spent = json.load(open(SPEND_FILE))["usd"] if os.path.exists(SPEND_FILE) else 0.0
    if spent >= budget:
        raise ClefError(f"openai budget exhausted: ${spent:.4f} of ${budget:.2f}")
    qs = []
    for name, q in questions.items():
        if q["type"] == "noul":
            qs.append({"type": "predicate", "name": name, "instructions": q["instructions"]})
        elif q["type"] == "choice":
            qs.append({"type": "choice", "name": name, "instructions": q["instructions"],
                       "choices": [{"value": k, "description": v} for k, v in q["criteria"].items()]})
        else:
            qs.append({"type": "score", "name": name, "instructions": q["instructions"],
                       "levels": [{"label": str(c), "description": str(c)} for c in q["criteria"]]})
    inp = state if not images else [{"role": "user", "content":
           [{"type": "input_text", "text": state}] + [{"type": "input_image", "image_url": im} for im in images]}]
    req = urllib.request.Request("https://api.openai.com/v1/decisions",
                                 data=json.dumps({"model": os.environ.get("OPENAI_DECISION_MODEL", "gpt-6-luna"), "input": inp, "questions": qs}).encode(),
                                 method="POST", headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                                                         "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        raise ClefError(f"openai HTTP {e.code}: {e.read().decode()[:300]}")
    used = res.get("usage", {}).get("input_tokens", 0)
    os.makedirs(os.path.dirname(SPEND_FILE), exist_ok=True)
    json.dump({"usd": spent + used * OPENAI_USD_PER_TOKEN}, open(SPEND_FILE, "w"))
    out = {}
    for a in res["answers"]:
        if a["type"] == "predicate":
            out[a["name"]] = {"type": "noul", "noul": a["probability"]}
        elif a["type"] == "choice":
            out[a["name"]] = {"type": "choice", "choice": a["choice"], "confidence": a.get("confidence"),
                              "probabilities": {p["value"]: p["probability"] for p in a["probabilities"]}}
        elif a["type"] == "score":
            out[a["name"]] = {"type": "score", "score": a["score"]}
        else:  # refusal: degrade to "unsure" so the loop escalates instead of losing the whole call
            q = questions.get(a.get("name"), {})
            if q.get("type") == "choice":
                keys = list(q["criteria"])
                out[a["name"]] = {"type": "choice", "choice": keys[0], "confidence": 0.0, "refused": True,
                                  "probabilities": {k: 1 / len(keys) for k in keys}}
            else:
                out[a["name"]] = {"type": "noul", "noul": 0.5, "score": None, "refused": True}
    return out, used


def clef(state, questions, images=None, model="clef"):
    acct, tok = os.environ["CLOUDFLARE_ACCOUNT_ID"], os.environ["CLOUDFLARE_API_TOKEN"]
    body = {"model": model, "state": state, "questions": questions}
    if images:
        body["images"] = images
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/@cf/cloudflare/{model}",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.load(r)["result"]
    except urllib.error.HTTPError as e:
        raise ClefError(f"clef HTTP {e.code}: {e.read().decode()[:300]}")
    return res["answers"], res.get("usage", {}).get("input_tokens", 0)


def screenshot_b64(width=640):
    """Screenshot downscaled to a small JPEG (full-res frames blow Clef's 64k context)."""
    import tempfile
    png = adb("exec-out", "screencap", "-p", timeout=20).stdout
    with tempfile.TemporaryDirectory() as d:
        src, dst = os.path.join(d, "s.png"), os.path.join(d, "s.jpg")
        open(src, "wb").write(png)
        subprocess.run(["sips", "-Z", str(width), "-s", "format", "jpeg", src, "--out", dst],
                       capture_output=True, check=True)
        jpg = open(dst, "rb").read()
    return "data:image/jpeg;base64," + base64.b64encode(jpg).decode()


def act(action, tap_box, root, typing=None):
    if typing:
        (x1, y1, x2, y2), text = typing
        adb("shell", "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))
        time.sleep(0.6)
        safe = re.sub(r"([\\'\"()&|;<>$`!*?#])", r"\\\1", text).replace(" ", "%s")
        adb("shell", "input", "text", safe)
        adb("shell", "input", "keyevent", "66")
    elif action.startswith("tap "):
        x1, y1, x2, y2 = tap_box
        adb("shell", "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))
    elif action in ("scroll_down", "scroll_up"):
        W, H = bounds(root.find("node").get("bounds"))[2:]
        a, b = (int(H * 0.75), int(H * 0.3)) if action == "scroll_down" else (int(H * 0.3), int(H * 0.75))
        adb("shell", "input", "swipe", str(W // 2), str(a), str(W // 2), str(b), "350")
    else:
        adb("shell", "input", "keyevent", str(KEYS[action]))
