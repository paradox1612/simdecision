"""Scenario runtime: executes a validated Scenario on one platform and produces a report.

Three kinds of step, cheapest first:
  do      deterministic verb -> free
  goal    DYNAMIC: the decision model picks WHICH visible target; code does HOW (tap / D-pad steering). A route
          that passed once is cached and replayed (code only, + one confirm) until the UI drifts.
  expect  oracles: code measures (ui / logcat / foreground / stability); the model judges only what code can't.

Verdicts: PASS | FAIL (an expectation was false = product finding) | BLOCKED (could not reach the place under
test - a product bug OR a harness limit, a human/Claude must look) | ERROR (infra) | SKIPPED.
"""
import hashlib, json, os, re, sys, time
from dataclasses import dataclass, field, replace

HERE = os.path.dirname(os.path.abspath(__file__))
from . import core as tsim  # noqa: E402
from . import runtime as rt  # noqa: E402
from . import scenario as sc_mod  # noqa: E402
from .android import DENY, Android, DriverError, PreMutationError  # noqa: E402
from . import guards, graph as graph_mod  # noqa: E402
from .apple import AppleTV  # noqa: E402
from .devicelock import DeviceBusy, device_lock  # noqa: E402
from .fixtures import FixtureError, Fixtures  # noqa: E402
from .redact import MASK, Masker  # noqa: E402
from .ios import IOS  # noqa: E402

from . import config  # noqa: E402

ROUTES = config.state("routes")
REPORTS = config.state("reports")


def usd(tokens):
    return tokens * (tsim.OPENAI_USD_PER_TOKEN if tsim.PROVIDER == "openai" else tsim.CLEF_USD_PER_TOKEN)


@dataclass
class StepResult:
    id: str
    desc: str
    outcome: str = "PASS"
    secs: float = 0.0
    tokens: int = 0
    detail: str = ""
    route: str = ""  # replayed | dynamic | ""
    evidence: list = field(default_factory=list)
    checks: list = field(default_factory=list)  # [{oracle, value, ok, note}]


@dataclass
class RunReport:
    run_id: str
    scenario: str
    platform: str
    provider: str
    started: str
    verdict: str = "PASS"
    secs: float = 0.0
    tokens: int = 0
    steps: list = field(default_factory=list)
    dir: str = ""
    masker: object = None  # Masker for secret inputs: EVERYTHING that leaves the runtime passes through it
    notes: list = field(default_factory=list)

    def mask(self, text):
        return self.masker.mask(text) if self.masker else text

    def to_dict(self):
        d = {k: v for k, v in self.__dict__.items() if k not in ("masker", "steps")}
        d["steps"] = [s.__dict__ for s in self.steps]
        d["usd"] = round(usd(self.tokens), 5)
        return d

    def summary(self):
        n_ok = sum(s.outcome == "PASS" for s in self.steps)
        lines = [f"{self.verdict} {self.scenario} [{self.platform}] {n_ok}/{len(self.steps)} steps {self.secs:.0f}s "
                 f"{self.provider} {self.tokens}tok ${usd(self.tokens):.5f} run={self.run_id}"]
        for s in self.steps:
            mark = {"PASS": "ok ", "WARN": "wrn", "FAIL": "FAIL", "BLOCKED": "BLOCKED", "ERROR": "ERROR",
                    "SKIPPED": "skip"}[s.outcome]
            extra = f" ({s.route})" if s.route else ""
            lines.append(f"  {mark:7s} {s.id}: {s.desc}{extra}" + (f" - {s.detail}" if s.outcome != "PASS" and s.detail else ""))
            for c in s.checks:
                if not c["ok"]:
                    lines.append(f"            x {c['oracle']}: {c['value']} -> {c['note']}")
                elif c["oracle"] == "judge":  # model-decided checks always show their probability, pass or fail
                    lines.append(f"            ~ judge ({c['note']}): {c['value']}")
            for e in s.evidence[:2] if s.outcome not in ("PASS", "SKIPPED") else []:
                lines.append(f"            evidence: {e}")
        lines += [f"  note: {n}" for n in self.notes]
        return self.mask("\n".join(lines))

    def markdown(self):
        out = [f"# {self.scenario} on {self.platform} - {self.verdict}", "",
               f"- run `{self.run_id}` - {self.started} - {self.secs:.0f}s - {self.provider} {self.tokens} tok (${usd(self.tokens):.5f})",
               "", "| step | outcome | what | route | secs | tok |", "| --- | --- | --- | --- | --- | --- |"]
        for s in self.steps:
            out.append(f"| {s.id} | {s.outcome} | {s.desc} | {s.route} | {s.secs:.1f} | {s.tokens} |")
        for s in self.steps:
            if s.outcome in ("PASS", "SKIPPED") and not s.checks:
                continue
            out += ["", f"### {s.id} - {s.outcome}", s.detail or ""]
            for c in s.checks:
                out.append(f"- {'ok' if c['ok'] else 'FAILED'} `{c['oracle']}: {c['value']}` - {c['note']}")
            for e in s.evidence:
                out.append(f"- evidence: `{e}`")
        out += [""] + [f"> note: {n}" for n in self.notes]
        return self.mask("\n".join(out) + "\n")


# ---------- goal surface ----------

class AndroidSurface(rt.Surface):
    name = "android"

    def __init__(self, drv, inputs, allow, recorder, secrets=(), masker=None):
        self.drv, self.inputs, self.allow, self.rec = drv, inputs, allow, recorder
        self.secrets, self.masker = set(secrets), masker or Masker()
        self.items, self.focused = [], None
        self.cur_text = self.prev_text = self.last_action = ""
        self.vision_next = False
        self.graph, self.node, self.pending = None, None, None  # screen graph: current node + the tap awaiting its result
        self.primed = None  # a screen sample taken after the last action: the next observe() reuses it, no second dump

    def after_action(self, settle):
        """Event wait instead of a fixed sleep: return as soon as the tree has changed and is stable, or after a short
        cap when the action changes nothing visible (a focus move). Costs ~one snapshot when the screen reacts fast."""
        self.primed = wait_change(self.drv, self.cur_text, cap=max(settle, 1.0) * 1.5)  # reused by the next observe()

    def observe(self):
        root, text, items, focused = self.primed or self.drv.screen()
        self.primed = None
        self.items, self.focused, self.cur_text = items, focused, self.masker.mask(text)
        self.drv.expect_labels = guards.label_set(items)  # the next tap checks it is still on THIS screen
        if self.graph is not None:  # learn: the action we just ran led from the previous node to this one
            n = self.graph.node_for(guards.screen_signature(self.drv.expect_labels, text), title=guards.screen_title(text))
            if self.pending and self.node and n and n != self.node:
                self.graph.add_edge(self.node, self.pending, n)
            self.pending, self.node = None, n
        imgs = None
        if root is not None and self.vision_next:  # one screenshot, only after the text tree failed to place the goal
            self.vision_next = False
            imgs = self.drv.screenshot_pair(gap=0)[:1]
            text += "\n(a screenshot of the screen is attached)"
        elif root is not None and len(items) < 3:  # video/player surfaces expose no tree
            imgs = self.drv.screenshot_pair()
            text += "\n(tree is nearly empty - likely a full-screen player; two screenshots 1s apart attached)"
        obs = rt.Obs(text=self.masker.mask(text), images=imgs)  # the decision model never sees a secret's value
        obs.raw = text                                          # code checks (done rules) use the real text
        return obs

    def _risk(self, label):
        if DENY.search(label) and not any(a.lower() in label.lower() for a in self.allow):
            return "deny"
        return "safe"

    def options(self, obs):
        opts, seen = [], set()
        for it in self.items:
            lab = it["label"]
            if lab == "(unlabelled)" or lab.startswith("[text field]") or lab in seen:
                continue
            seen.add(lab)
            if it.get("focused"):  # TV: tapping the focused item IS pressing OK - one option, not two that split the vote
                opts.append(rt.Option("key:select", f'open / activate the focused item "{lab}" (press OK)',
                                      self._do("key", "select"), self._risk(lab)))
                continue
            if len(opts) >= 40:  # cap the table, but SAY so: the model must not assume it saw everything
                hidden = sum(1 for i in self.items if i["label"] not in seen and i["label"] != "(unlabelled)")
                obs.text += f"\n(+{hidden} more tappable items not listed - scroll or pick a more specific route)"
                break
            opts.append(rt.Option(f"tap:{lab}", f'tap "{lab}"', self._do("tap", lab), self._risk(lab)))
        fields = [i for i in self.items if i.get("editable")]
        field_ = fields[0] if fields else None
        for f in fields[:3]:  # one option per (input x field): the model chooses WHERE, never WHAT to type
            for name, val in self.inputs.items():
                if f["text"] != str(val):
                    where = f' "{f["label"][13:].strip() or "(empty)"}"' if len(fields) > 1 else ""
                    spec = {"text": str(val), "field": f["label"], "submit": len(fields) == 1}
                    ref = {**spec, "text": "$" + name}  # routes store the reference, never the value
                    shown = f"<{name}>" if name in self.secrets else str(val)
                    opts.append(rt.Option(f"type:{name}:{f['label']}",
                                          f'type "{shown}" into the text field{where}' + (" and submit" if len(fields) == 1 else ""),
                                          self._do("type", spec, rec=ref, shown=f'type "{shown}"'), "safe"))
        if self.drv.platform in ("tv", "tvos"):  # the whole remote, each press grounded in where focus would go
            for k in ("up", "down", "left", "right"):
                nb = tsim.neighbours(self.focused, self.items, k)
                what = (f'focus moves to "{nb[0]["label"]}"' if nb else "nothing visible that way; may open a side rail, scroll, or do nothing")
                opts.append(rt.Option(f"key:{k}", f"press {k.upper()} on the remote ({what})", self._do("key", k), "safe"))
        if field_ and (field_.get("text") or "").strip():
            opts.append(rt.Option("delete_all", "delete all text in the text field", self._do("delete", "all"), "safe"))
        if self.drv.platform == "ios" and any(i["label"] in ("Emoji", "Dictate") for i in self.items):
            opts.append(rt.Option("dismiss_keyboard", "hide the on-screen keyboard to reach the rest of the form (buttons below)",
                                  self._do("dismiss_keyboard", None), "safe"))
        opts.append(rt.Option("back", "go back / close the overlay", self._do("back", ""), "safe"))
        opts.append(rt.Option("scroll_down", "scroll down to reveal more", self._do("scroll", "down"), "safe"))
        opts.append(rt.Option("scroll_up", "scroll up", self._do("scroll", "up"), "safe"))
        for o in opts:  # a typed secret can show up in a field's label; descriptions go to the model
            o.desc = self.masker.mask(o.desc)
        return opts

    def _do(self, verb, val, rec=None, shown=None):
        def run():
            self.rec.append({"a": verb, "v": val if rec is None else rec})
            self.pending = f"tap:{val}" if verb == "tap" else "back" if verb == "back" else None
            self.prev_text, self.last_action = self.cur_text, shown or f"{verb} {val}".strip()
            execute_action(self.drv, verb, val)
        return run


def execute_action(drv, verb, val):
    drv.perform(verb, val)


# ---------- oracles ----------

def eval_oracle(drv, kind, value, ctx, step, fx=None):
    """-> (ok, note, tokens)"""
    if kind in ("requests_has", "requests_lacks"):
        if not fx:
            raise DriverError("requests_* oracle needs the scenario's fixtures")
        n = fx.count(value["fixture"], str(value["path"]))
        if kind == "requests_has":
            lo = int(value.get("min", 1))
            return n >= lo, f"{value['fixture']} saw {n} request(s) matching '{value['path']}' (need >= {lo})", 0
        hi = int(value.get("max", 0))
        return n <= hi, f"{value['fixture']} saw {n} request(s) matching '{value['path']}' (allowed <= {hi})", 0
    if kind in ("ui_has", "ui_lacks"):
        _, text, _, _ = drv.screen()
        hit = str(value).lower() in text.lower()
        ok = hit if kind == "ui_has" else not hit
        return ok, ("found" if hit else "not on screen"), 0
    if kind == "app_foreground":
        fg = drv.foreground()
        return drv.owns(fg), f"foreground={fg}", 0
    if kind in ("logcat_has", "logcat_lacks"):
        lines = drv.log(str(value))
        hit = bool(lines)
        ok = hit if kind == "logcat_has" else not hit
        return ok, (f"{len(lines)} match(es): {lines[0][:140]}" if lines else "no matching line"), 0
    if kind == "stable":
        secs = max(1, int(value))
        base = None
        for _ in range(secs):
            h = hashlib.md5(drv.screen()[1].encode()).hexdigest()
            base = base or h
            if h != base:
                return False, "screen kept changing (spinner / flicker?)", 0
            time.sleep(1)
        return True, f"unchanged for {secs}s", 0
    if kind == "judge":
        root, text, items, _ = drv.screen()
        imgs = None
        if step.vision or len(items) < 3:
            imgs = drv.screenshot_pair()
        text = Masker([ctx.inputs[n] for n in ctx.secrets]).mask(text)
        state = "SCREEN:\n" + text + ("\n(Two screenshots 1s apart attached; if the picture changed, video is playing.)" if imgs else "")
        ans, used = tsim.decide(state, {"q": {"type": "noul", "instructions": f"Is this true of the SCREEN: {value}"}}, images=imgs)
        p = ans["q"]["noul"]
        return p >= (step.min_p - (0.1 if imgs else 0)), f"p={p:.2f}", used
    raise DriverError(f"unknown oracle {kind}")


# ---------- routes ----------

def route_path(scenario, platform, step_id):
    os.makedirs(ROUTES, exist_ok=True)
    return os.path.join(ROUTES, f"{scenario}.{platform}.{step_id}.json")


def load_route(scenario, platform, step_id):
    p = route_path(scenario, platform, step_id)
    return json.load(open(p))["actions"] if os.path.exists(p) else None


def save_route(scenario, platform, step_id, actions):
    json.dump({"saved": time.strftime("%F %T"), "actions": actions}, open(route_path(scenario, platform, step_id), "w"), indent=1)


def drop_route(scenario, platform, step_id):
    p = route_path(scenario, platform, step_id)
    if os.path.exists(p):
        os.remove(p)


# ---------- the run ----------

def describe(step):
    if step.kind == "do":
        return f"{step.verb}" + (f" {step.value}" if step.value is not None else "")
    if step.kind == "goal":
        return f"goal: {step.goal}"
    return "expect " + ", ".join(f"{k}={v}" for k, v in step.expect)


def resolve_deep(value, inputs):
    if isinstance(value, dict):
        return {k: resolve_deep(v, inputs) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_deep(v, inputs) for v in value]
    return resolve(value, inputs)


def resolve(value, inputs):
    if isinstance(value, str) and value.startswith("$"):
        return str(inputs[value[1:]])
    return value


def run_scenario(sc, platform, replay=True, drv=None):
    """Runs under a machine-wide lock for the platform's device lane (see devicelock.py)."""
    run_id = time.strftime("%y%m%d-%H%M%S") + f"-{sc.id}-{platform}"
    try:
        with device_lock(platform, f"{sc.id} [{platform}]"):
            try:
                return _run_locked(sc, platform, replay, drv)
            finally:
                if platform in ("phone", "tv"):
                    from . import snapserver
                    snapserver.stop_all(tsim.ADB)  # free the device's single UiAutomation slot for other tools
    except DeviceBusy as e:
        rep = RunReport(run_id, sc.id, platform, tsim.PROVIDER, time.strftime("%F %T"))
        rep.steps.append(StepResult("-", "device lane", "ERROR", detail=str(e)))
        rep.verdict = "ERROR"
        return rep


def bind_fixtures(sc, values):
    """Late binding: replace placeholder inputs by the fixture servers' real URLs, re-render goals."""
    inputs = dict(sc.inputs)
    for k, (name, fld) in sc.fixture_inputs.items():
        inputs[k] = values[name][fld]
    steps = [replace(st, goal=sc_mod.render_goal(st.goal_t, inputs, sc.secrets)) if st.kind == "goal" else st for st in sc.steps]
    return replace(sc, inputs=inputs, steps=steps)


def _run_locked(sc, platform, replay=True, drv=None):
    """Start the scenario's fixtures (local servers), run, always stop them, add their request summary to the report."""
    run_id = time.strftime("%y%m%d-%H%M%S") + f"-{sc.id}-{platform}"
    rdir = os.path.join(REPORTS, run_id)
    os.makedirs(rdir, exist_ok=True)
    fx = None
    if sc.fixtures and platform in sc_mod.PLATFORMS:
        fx = Fixtures(sc.fixtures, platform, rdir)
        try:
            sc = bind_fixtures(sc, fx.start())
        except FixtureError as e:
            rep = RunReport(run_id, sc.id, platform, tsim.PROVIDER, time.strftime("%F %T"), dir=rdir)
            rep.steps.append(StepResult("fixtures", "start fixtures", "ERROR", detail=str(e)))
            rep.verdict = "ERROR"
            return rep
    try:
        rep = _run_core(sc, platform, replay, drv, run_id, rdir, fx)
    finally:
        if fx:
            lines = fx.summary()
            fx.stop()
    if fx:
        rep.notes += lines
        open(os.path.join(rdir, "report.md"), "w").write(rep.markdown())
        open(os.path.join(rdir, "report.json"), "w").write(rep.mask(json.dumps(rep.to_dict(), indent=1, default=str)))
    return rep


def _run_core(sc, platform, replay, drv, run_id, rdir, fx):
    started = time.strftime("%F %T")
    rep = RunReport(run_id, sc.id, platform, tsim.PROVIDER, started, dir=rdir)
    rep.masker = Masker([sc.inputs[n] for n in sc.secrets])
    if rep.masker:
        rep.notes.append(f"secret inputs in use ({', '.join(sorted(sc.secrets))}): text is masked everywhere, but "
                         "screenshots are NOT redacted - the OS masks password-type fields, plain fields may show the value")
    t0 = time.time()
    if platform not in sc_mod.PLATFORMS:
        rep.steps.append(StepResult("-", "platform", "SKIPPED", detail=f"no driver for '{platform}' yet"))
        rep.verdict = "SKIPPED"
        return rep
    try:
        drv = drv or (AppleTV(platform) if platform == "tvos" else IOS(platform) if platform == "ios" else Android(platform))
    except DriverError as e:
        rep.steps.append(StepResult("-", "device", "ERROR", detail=str(e)))
        rep.verdict = "ERROR"
        return rep
    if sc.raw.get("start", "fresh") == "fresh" and not (sc.steps[0].kind == "do" and sc.steps[0].verb == "launch"):
        try:
            drv.launch(fresh=True)
        except DriverError as e:
            rep.steps.append(StepResult("launch", "launch app", "BLOCKED", detail=str(e)))
            rep.verdict = "BLOCKED"
            return rep
        rep.steps.append(StepResult("launch", "launch app (default start: fresh)"))
    drv.logcat_clear()
    halted = False
    for st in sc.steps:
        if st.only and platform not in st.only:
            continue
        sr = StepResult(st.id, describe(st))
        rep.steps.append(sr)
        if halted:
            sr.outcome = "SKIPPED"
            sr.detail = "an earlier step failed"
            continue
        t1 = time.time()
        try:
            _exec_step(sc, st, platform, drv, sr, rdir, replay, fx)
        except DriverError as e:
            sr.outcome, sr.detail = "BLOCKED", str(e)
        except tsim.ClefError as e:
            sr.outcome, sr.detail = "ERROR", str(e)[:200]
        except Exception as e:  # noqa: BLE001 - a harness bug must surface as ERROR, not kill the run
            sr.outcome, sr.detail = "ERROR", f"{type(e).__name__}: {e}"[:200]
        sr.secs = time.time() - t1
        rep.tokens += sr.tokens
        if sr.outcome == "FAIL" and st.severity == "warn":
            sr.outcome = "WARN"
        if sr.outcome == "PASS":
            viol = _invariants(sc, st, drv)
            if viol:
                sr.outcome, sr.detail = "FAIL", f"invariant violated: {viol}"
                sr.evidence += _evidence(drv, rdir, st.id, rep.masker)
        if sr.outcome in ("FAIL", "BLOCKED", "ERROR"):
            if not sr.evidence:
                sr.evidence += _evidence(drv, rdir, st.id, rep.masker)
            halted = True
    rep.secs = time.time() - t0
    outs = [s.outcome for s in rep.steps]
    rep.verdict = ("ERROR" if "ERROR" in outs else "FAIL" if "FAIL" in outs else
                   "BLOCKED" if "BLOCKED" in outs else "PASS")
    open(os.path.join(rdir, "report.md"), "w").write(rep.markdown())
    open(os.path.join(rdir, "report.json"), "w").write(rep.mask(json.dumps(rep.to_dict(), indent=1, default=str)))
    return rep


def ACT_names(v):
    from .actions import _key_names
    return _key_names(v) or []


def _invariants(sc, st, drv):
    if st.kind == "do" and (st.verb in ("stop", "network", "background", "clear_data", "permission", "appearance")
                            or (st.verb == "key" and "home" in ACT_names(st.value))):
        return ""
    if "app_foreground" in sc.invariants:
        fg = drv.foreground()
        if not drv.owns(fg):
            time.sleep(1.5)
            fg = drv.foreground()
            if not drv.owns(fg):
                return f"app left the foreground (now {fg})"
    if "no_crash" in sc.invariants:
        c = drv.crashes()
        if c:
            return "crash/ANR in log: " + c[0][:160]
    return ""


def _evidence(drv, rdir, step_id, masker=None):
    out = []
    try:
        out.append(drv.screenshot(os.path.join(rdir, f"{step_id}.png")))
        text = drv.screen()[1]
        open(os.path.join(rdir, f"{step_id}.screen.txt"), "w").write(masker.mask(text) if masker else text)
        out.append(os.path.join(rdir, f"{step_id}.screen.txt"))
    except Exception:  # noqa: BLE001
        pass
    return out


def _exec_step(sc, st, platform, drv, sr, rdir, replay, fx=None):
    if st.kind == "do":
        v = resolve_deep(st.value, sc.inputs)
        if st.verb == "fixture":
            if not fx:
                raise DriverError("fixture verb used but the scenario declares no fixtures")
            sr.detail = f"{v['name']}{v['get']} -> {fx.get(v['name'], v['get'])[:120]}"
        elif st.verb == "screenshot":
            sr.evidence.append(drv.screenshot(os.path.join(rdir, f"{st.id}.png")))
        elif st.verb in ("tap", "long_press", "double_tap", "type", "delete", "paste", "select_all", "copy", "cut"):
            # The target (a label or a text field) may still be loading after a screen change: wait up to 8 s, then fail.
            end = time.time() + 8
            while True:
                try:
                    drv.perform(st.verb, v)
                    break
                except DriverError as e:
                    if not ("no item matching" in str(e) or "no text field" in str(e)) or time.time() > end:
                        raise
                    time.sleep(1.0)
        else:
            drv.perform(st.verb, v)
        time.sleep(1.0)
        if drv.notes:
            sr.outcome, sr.detail = "WARN", "; ".join(drv.notes)
            drv.notes.clear()
        return
    if st.kind == "expect":
        for kind, value in st.expect:
            value = resolve_deep(value, sc.inputs)  # `ui_has: $pw` keeps the literal out of the YAML
            ok, note, used = eval_oracle(drv, kind, value, sc, st, fx)
            sr.tokens += used
            sr.checks.append({"oracle": kind, "value": value, "ok": ok, "note": note})
        if not all(c["ok"] for c in sr.checks):
            sr.outcome = "FAIL"
            sr.detail = f"{sum(not c['ok'] for c in sr.checks)} of {len(sr.checks)} expectations false"
            sr.evidence.append(drv.screenshot(os.path.join(rdir, f"{st.id}.png")))
        return
    try:
        _goal(sc, st, platform, drv, sr, rdir, replay, fx)
    finally:
        graph_mod.for_platform(platform).save()  # navigation learned this step, even if it ended BLOCKED


def _graph_walk(sc, st, drv, surface, sr, done):
    """One model request maps the goal onto a screen the graph already knows; the hops are then plain code, each verified by
    arrival (the observed screen must be the edge's destination, else the edge is penalised and we fall back to dynamic)."""
    g = surface.graph
    surface.observe()
    if surface.node is None:
        return False
    cands = g.reachable_named(surface.node)
    if not cands:
        return False
    crit = dict(cands)  # node id -> description (title + how it was reached)
    crit["none"] = "the goal is not about reaching any of these screens"
    ans, used = tsim.decide(f"GOAL: {st.goal}", {"t": {"type": "choice", "criteria": crit,
                            "instructions": "Which listed screen does the GOAL ask the person to reach?"}})
    sr.tokens += used
    probs = ans["t"]["probabilities"]
    pick = max(probs, key=probs.get)
    if pick == "none" or probs[pick] < 0.7:
        return False
    path = g.path_to(surface.node, pick)
    if not path:
        return False
    steps = []
    for e in path:
        if surface.node != e["from"]:
            return False  # the walk left the known map: let the dynamic planner take over from here
        verb, val = ("tap", e["action"][4:]) if e["action"].startswith("tap:") else ("back", "")
        try:
            surface._do(verb, val)()
        except DriverError as ex:
            g.fail(e["from"], e["action"])
            sr.detail = f"graph hop {e['action']} refused ({ex}); "
            return False
        steps.append(e["action"])
        surface.after_action(1.0)
        surface.observe()
        if surface.node != e["to"]:
            g.fail(e["from"], e["action"])
            sr.detail = f"graph hop {e['action']} did not arrive; "
            return False
    explicit = st.until or st.until_ui_has or st.until_req
    if explicit and not done(surface.observe()):
        return False
    sr.route, sr.detail = "graph", f"{len(steps)} hop(s): " + " > ".join(steps)
    if surface.rec:
        save_route(sc.id, platform_of(drv), st.id, surface.rec)
    return True


def platform_of(drv):
    return drv.platform


def wait_change(drv, before_text, cap=1.5, quiet=2):
    """Poll the screen until its text differs from `before_text` and then holds for `quiet` consecutive samples
    (or `cap` seconds pass). Replaces fixed 1 s sleeps after every action."""
    end, prev, run, changed = time.time() + cap, None, 0, False
    while True:
        t0 = time.time()
        sample = drv.screen()
        text = sample[1]
        if time.time() - t0 > 0.8:
            quiet = 1  # a slow snapshot (Android uiautomator ~2 s) already waits for idle: one changed sample is enough
        changed = changed or text != before_text
        run = run + 1 if text == prev else 1
        prev = text
        if changed and run >= quiet:
            return sample
        if time.time() >= end:
            return sample
        time.sleep(0.1)


def _settle(drv, timeout=25.0, need=2):
    """Wait until the screen is stable AND interactive: the tree is identical for `need` consecutive samples ~1 s apart
    and offers at least 3 items. A static splash/spinner is 'stable' but offers nothing to choose from, and some screens
    add their navigation chrome a couple of seconds after the content. A full-screen player legitimately has an empty tree,
    so after the timeout we proceed anyway."""
    end, prev, run = time.time() + timeout, None, 0
    while time.time() < end:
        t0 = time.time()
        sample = drv.screen()
        _, text, items, _ = sample
        if time.time() - t0 > 0.8:
            need = 1  # slow snapshot (it waits for idle itself): a populated screen counts as settled at once
        cur = hashlib.md5(text.encode()).hexdigest()
        run = run + 1 if (cur == prev and len(items) >= 3) else 1 if len(items) >= 3 else 0
        if run >= need:
            return sample
        prev = cur
        time.sleep(0.25)


def _goal(sc, st, platform, drv, sr, rdir, replay, fx=None):
    first = _settle(drv)
    recorder = []
    masker = Masker([sc.inputs[n] for n in sc.secrets])
    surface = AndroidSurface(drv, sc.inputs, sc.allow, recorder, sc.secrets, masker)
    surface.primed = first  # the settle's last sample is a fresh view of this screen
    surface.graph = graph_mod.for_platform(platform)
    start_sig = [surface.observe().sig]

    def done(obs, ask=True):
        if st.until_req:  # network evidence: done once the fixture has seen the app's request
            return fx is not None and fx.count(st.until_req["fixture"], str(st.until_req["path"])) >= int(st.until_req.get("min", 1))
        raw_text = getattr(obs, "raw", obs.text)
        needle = resolve_deep(st.until_ui_has, sc.inputs) if st.until_ui_has else ""
        if needle and str(needle).lower() not in raw_text.lower():
            return False
        if st.until_ui_has and not st.until:
            return True
        if not st.until_ui_has and not st.until:
            typed = [str(sc.inputs[n]) for n in st.refs] + [q for q in re.findall(r'"([^"]+)"', st.goal)
                                                          if q in [str(v) for v in sc.inputs.values()]]
            # Only for goals that are ABOUT entering text; "add / save / create" goals need the submit, not just the typing.
            entry_goal = re.search(r"\b(enter|type|input|fill|write|paste|put)\b", st.goal, re.I)
            if entry_goal and typed and surface.last_action and all(q in getattr(obs, "raw", obs.text) for q in typed):
                return True  # code, not the model: the value we were asked to enter is now on screen
            # Inferred done-condition: the instruction's RESULT is on screen, and the screen is not where we started.
            if obs.sig == start_sig[0]:
                return False
            if not ask:
                return False  # the model's verdict arrives with the next-action request (done_q): one round trip
            q, ctx = inferred_q(obs)
            ans, used = tsim.decide(ctx, {"q": {"type": "noul", "instructions": q}}, images=obs.images)
            sr.tokens += used
            return ans["q"]["noul"] >= 0.8
        ans, used = tsim.decide(f"CURRENT STATE:\n{obs.text}",
                                {"q": {"type": "noul", "instructions": f"Is this true of the CURRENT STATE: {st.until}"}},
                                images=obs.images)
        sr.tokens += used
        return ans["q"]["noul"] >= 0.7

    def inferred_q(obs):
        q = (f"A person was told: \"{st.goal}\". Given the LAST ACTION and how the screen changed, has this "
             "instruction now been carried out? Judge by where the screen has led (a screen heading may use "
             "different words than the instruction), not by the starting screen.")
        ctx = (f"LAST ACTION: {surface.last_action or '(none yet)'}\n\nSCREEN BEFORE THE LAST ACTION:\n"
               f"{surface.prev_text[:700] or '(same as start)'}")
        return q, ctx

    def done_q(obs):
        """Model-judged done check, only for plain-sentence goals with no code-checkable condition and a changed screen."""
        if st.until_req or st.until_ui_has or st.until or obs.sig == start_sig[0]:
            return None
        return inferred_q(obs)

    # 1) replay a cached route: code only, plus the done check. Drift -> fall through to dynamic.
    cached = load_route(sc.id, platform, st.id) if replay else None
    if cached:
        try:
            for i, a in enumerate(cached):
                if a["a"] == "tap" or i == len(cached) - 1:  # skip dumps the next action does not need
                    surface.observe()  # screen memory for the done-check ("last action" + "screen before")
                if a["a"] == "tap":
                    # A slow screen (cold emulator) is not drift: give the target up to 8 s to render first.
                    for _ in range(8):
                        if drv.find(surface.items, a["v"]):
                            break
                        time.sleep(1.0)
                        surface.observe()
                    else:
                        raise DriverError(f'route drift: "{a["v"]}" not on screen')
                surface.prev_text, surface.last_action = surface.cur_text, f'{a["a"]} {a["v"]}'.strip()
                execute_action(drv, a["a"], resolve_deep(a["v"], sc.inputs))
                surface.after_action(1.0)
            if done(surface.observe()):
                sr.route, sr.detail = "replayed", f"{len(cached)} cached actions"
                return
            raise DriverError("route ran but the done-condition is false")
        except DriverError as e:
            drop_route(sc.id, platform, st.id)
            sr.detail = f"cached route stale ({e}); re-planning dynamically. "
            recorder.clear()
            start_sig[0] = None  # the replay moved the screen; "unchanged from start" no longer means anything
    # 1b) known navigation: if the goal asks for a screen the graph has reached before, walk the learned path (code only).
    if replay and _graph_walk(sc, st, drv, surface, sr, done):
        return
    # 2) dynamic: the decision model picks targets from what is on screen.
    order = rt.StandingOrder(goal=st.goal, done=st.until or (f'"{st.until_ui_has}" is on screen' if st.until_ui_has else st.goal), policy=
                             "Only tap listed items. Never purchase, rent, subscribe, sign out or delete."
                             + ("".join(f" APP KNOWLEDGE: {h}" for h in sc.hints))
                             + (" The goal needs text entered: if no text field is on screen yet, the right next step is "
                                "the item that leads to one (a label about adding, creating, entering, pasting, "
                                "searching or editing something), not unrelated navigation." if '"' in st.goal else ""),
                             max_steps=st.max_steps, done_fn=lambda o: done(o, ask=False), done_q=done_q)
    res = rt.run(surface, order, settle=1.0)
    sr.tokens += res.tokens
    sr.route = "dynamic"
    if res.outcome == "PASS":
        sr.detail = (sr.detail + f"{res.steps} steps, {len(recorder)} actions").strip()
        if recorder:
            save_route(sc.id, platform, st.id, recorder)
        return
    sr.outcome = "BLOCKED" if res.outcome in ("STUCK", "ESCALATE", "FAIL") else "ERROR"
    sr.detail += f"{res.outcome}: {res.detail} after {res.steps} steps"
    path = [str(r.get("action", "?")) for r in res.trace if r.get("action")][-10:]
    if path:  # what it tried, so a BLOCKED verdict is diagnosable without a re-run
        sr.detail += " - path: " + " > ".join(p[:40] for p in path)
