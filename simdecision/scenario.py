"""Scenario schema: the contract between the agent that writes the YAML and the runtime that executes it.

The authoring agent (Claude, GPT, ...) is the compiler. A decision model cannot write text or plan, so natural language ->
YAML happens in the agent; this module only *validates* it and says exactly what is wrong, so the fix loop is one cheap call.

  scenario: id-slug
  title: one line
  platforms: [phone, ios]         # phone, tv (Android TV), ios, tvos
  inputs: {query: "Bluetooth"}    # the ONLY strings the runtime may type (decision models cannot invent text)
  invariants: [app_foreground, no_crash]    # checked after every step (default: both)
  allow: ["Remove"]               # labels the risk gate would otherwise block
  steps:
    - {id: s1, do: launch}                                  # deterministic verb
    - {id: s2, goal: "Open the Display settings", until_ui_has: "Brightness"}                   # dynamic
    - {id: s3, expect: [{ui_has: "Brightness"}, {judge: "The screen shows a brightness control"}]}  # oracles
"""
import os
from dataclasses import dataclass, field
from typing import Any

import yaml

from . import actions as ACT
from .redact import looks_secret
from .fixtures import FIELDS as FIXTURE_FIELDS, KINDS as FIXTURE_KINDS

PLATFORMS = {"phone", "tv", "tvos", "ios"}
RESERVED_PLATFORMS = set()
DO_VERBS = ACT.ACTIONS
ORACLES = {
    "ui_has": "text appears in the screen tree (deterministic, free)",
    "ui_lacks": "text does NOT appear in the screen tree (deterministic, free)",
    "app_foreground": "the app package is the focused window (deterministic)",
    "logcat_has": "regex matches a log line since the last logcat_clear (deterministic)",
    "logcat_lacks": "regex matches NO log line since the last logcat_clear (deterministic)",
    "judge": "decision-model yes/no over screen text (+ screenshots if vision) - use only when code can't measure it",
    "stable": "screen tree unchanged for N seconds, e.g. stable: 3 (catches spinners that never end)",
    "requests_has": "a fixture saw >= min requests whose text contains `path`: {fixture: api, path: /items, min: 1} (network evidence)",
    "requests_lacks": "a fixture saw NO request containing `path` (or at most `max`): {fixture: api, path: /analytics, max: 0}",
}
STEP_KEYS = {"until_requests", "id", "do", "goal", "expect", "until", "until_ui_has", "max_steps", "only", "severity", "note",
             "value", "vision", "min_p", "timeout"}
TOP_KEYS = {"hints", "allow_destructive", "platform", "start", "scenario", "title", "platforms", "inputs", "invariants", "allow", "steps", "tags", "fixtures", "baseline"}
INVARIANTS = {"app_foreground", "no_crash"}


@dataclass
class Step:
    id: str
    kind: str  # do | goal | expect
    verb: str = ""
    value: Any = None
    goal: str = ""
    until: str = ""
    until_ui_has: str = ""
    max_steps: int = 12
    expect: list = field(default_factory=list)
    only: list = field(default_factory=list)
    severity: str = "fail"  # fail | warn
    vision: bool = False
    min_p: float = 0.6
    note: str = ""
    refs: list = field(default_factory=list)  # input names the step uses ($name)
    until_req: dict = field(default_factory=dict)  # done when a fixture has seen the app's request (network evidence)
    goal_t: str = ""  # goal with $name placeholders, re-rendered once fixture URLs are known


@dataclass
class Scenario:
    id: str
    title: str
    platforms: list
    inputs: dict
    invariants: list
    allow: list
    steps: list
    raw: dict
    secrets: set = field(default_factory=set)  # input names whose values are secret
    fixtures: list = field(default_factory=list)  # [{kind, name, ...opts}]
    hints: list = field(default_factory=list)  # app knowledge the compiler passes to the decision model
    fixture_inputs: dict = field(default_factory=dict)  # input name -> (fixture, field), bound at run time


def render_goal(goal_t, inputs, secrets):
    """$name -> "value" (secrets show as "<name>": the decision model never sees them)."""
    out = goal_t
    for m in __import__("re").findall(r"\$(\w+)", goal_t):
        if m in inputs:
            shown = f"<{m}>" if m in secrets else inputs[m]
            out = out.replace("$" + m, f'"{shown}"')
    return out


class ScenarioError(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(errors))


def parse(text: str) -> tuple:
    """-> (Scenario | None, errors[], warnings[]). Never raises; every problem is reported at once."""
    errs, warns = [], []
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return None, [f"YAML syntax: {str(e).splitlines()[0]}"], []
    if not isinstance(raw, dict):
        return None, ["top level must be a mapping with scenario/steps"], []
    for k in raw:
        if k not in TOP_KEYS:
            errs.append(f"unknown top-level key '{k}' (allowed: {', '.join(sorted(TOP_KEYS))})")
    sid = str(raw.get("scenario", "")).strip()
    if not sid or not all(c.isalnum() or c in "-_" for c in sid):
        errs.append("scenario: required slug (letters, digits, - _)")
    plats = raw.get("platforms") or ([raw["platform"]] if raw.get("platform") else ["phone"])
    if not isinstance(plats, list):
        errs.append("platforms: must be a list")
        plats = []
    for p in plats:
        if p in RESERVED_PLATFORMS:
            warns.append(f"platform '{p}' is reserved - no driver yet, it will be reported SKIPPED")
        elif p not in PLATFORMS:
            errs.append(f"platforms: unknown '{p}' (phone, tv, tvos, ios)")
    fixtures, fx_names = [], {}
    for i, f in enumerate(raw.get("fixtures") or [], 1):
        if not isinstance(f, dict) or f.get("kind") not in FIXTURE_KINDS or not f.get("name"):
            errs.append(f"fixtures[{i}]: needs kind ({', '.join(sorted(FIXTURE_KINDS))}) and a name")
            continue
        if f["name"] in fx_names:
            errs.append(f"fixtures[{i}]: duplicate name '{f['name']}'")
            continue
        fx_names[f["name"]] = f["kind"]
        fixtures.append(f)
    fixture_inputs = {}

    def fixture_fields(name):
        spec = next(f for f in fixtures if f["name"] == name)
        return FIXTURE_FIELDS[spec["kind"]] + sorted((spec.get("fields") or {}) if spec["kind"] == "command" else [])
    raw_inputs = raw.get("inputs") or {}
    inputs, secret_names = {}, set()
    if not isinstance(raw_inputs, dict):
        errs.append("inputs: must be a mapping")
        raw_inputs = {}
    for k, v in raw_inputs.items():
        if isinstance(v, dict):
            if "env" in v:  # {env: NAME}: read at run time, secret by default (never written to YAML, reports or routes)
                val = os.environ.get(str(v["env"]))
                if val is None:
                    errs.append(f"inputs.{k}: environment variable {v['env']} is not set (put it in .env or export it)")
                    continue
                inputs[k] = val
                if v.get("secret", True):
                    secret_names.add(k)
            elif "fixture" in v:  # late-bound: the real URL exists only once the fixture server is up
                name, fld = v.get("fixture"), v.get("field")
                if name not in fx_names:
                    errs.append(f"inputs.{k}: no fixture named '{name}'")
                elif fld not in fixture_fields(name):
                    errs.append(f"inputs.{k}: fixture '{name}' has fields {fixture_fields(name)}, not '{fld}'")
                else:
                    fixture_inputs[k] = (name, fld)
                    inputs[k] = f"<{name}.{fld}>"
            elif "value" in v:
                if v.get("secret"):
                    errs.append(f"inputs.{k}: a secret must not be a literal in the YAML - use {{env: NAME}}")
                    continue
                inputs[k] = v["value"]
            else:
                errs.append(f"inputs.{k}: use a plain value, {{value: ...}} or {{env: NAME}}")
        else:
            inputs[k] = v
            if looks_secret(str(k)):
                warns.append(f"inputs.{k}: the name looks like a secret but the value is a literal - use {{env: NAME}}")
    inv = raw.get("invariants", sorted(INVARIANTS))
    for i in inv or []:
        if i not in INVARIANTS:
            errs.append(f"invariants: unknown '{i}' (allowed: {', '.join(sorted(INVARIANTS))})")
    allow = raw.get("allow") or []
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        errs.append("steps: required non-empty list")
        steps_raw = []
    steps, seen = [], set()
    for n, s in enumerate(steps_raw, 1):
        if isinstance(s, str):  # plain instruction: "open Settings" -> a dynamic goal, done-condition inferred
            s = {"goal": s}
        if not isinstance(s, dict):
            errs.append(f"step {n}: must be a mapping")
            continue
        sid_ = str(s.get("id") or f"s{n}")
        where = f"step '{sid_}'"
        if sid_ in seen:
            errs.append(f"{where}: duplicate id")
        seen.add(sid_)
        shorthand = [k for k in s if k in ACT.ACTIONS and k not in ("do", "goal", "expect")]
        if shorthand and "do" not in s:  # `- tap: Settings`  /  `- {id: x, key: [down, select]}`
            if len(shorthand) > 1:
                errs.append(f"{where}: more than one action in one step ({shorthand}) - use one step each")
                continue
            s = {**{k: v for k, v in s.items() if k != shorthand[0]}, "do": {shorthand[0]: s[shorthand[0]]}}
        for k in s:
            if k not in STEP_KEYS:
                errs.append(f"{where}: unknown key '{k}'")
        kinds = [k for k in ("do", "goal", "expect") if k in s]
        if len(kinds) != 1:
            errs.append(f"{where}: exactly one of do / goal / expect (found {kinds or 'none'})")
            continue
        only = s.get("only") or []
        for p in only:
            if p not in PLATFORMS | RESERVED_PLATFORMS:
                errs.append(f"{where}: only: unknown platform '{p}'")
        sev = s.get("severity", "fail")
        if sev not in ("fail", "warn"):
            errs.append(f"{where}: severity must be fail|warn")
        st = Step(id=sid_, kind=kinds[0], only=only, severity=sev, note=str(s.get("note", "")),
                  vision=bool(s.get("vision", False)), min_p=float(s.get("min_p", 0.6)),
                  max_steps=int(s.get("max_steps", 12)))
        if st.kind == "do":
            d = s["do"]
            if isinstance(d, dict) and len(d) == 1:  # `do: {tap: Settings}` form
                verb, val = next(iter(d.items()))
            elif isinstance(d, str):
                verb, val = d, s.get("value")
            else:
                errs.append(f"{where}: do must be a verb or a single {{verb: value}}")
                continue
            err = ACT.validate(verb, val, plats)
            if err:
                errs.append(f"{where}: {err}")
                continue
            if verb in ("type", "paste") and isinstance(val, str) and val.startswith("$") and val[1:] not in inputs:
                errs.append(f"{where}: {verb} references ${val[1:]} but inputs has no '{val[1:]}'")
            if verb == "type" and isinstance(val, str) and not val.startswith("$"):
                warns.append(f"{where}: literal typed text - prefer inputs.<name> so it is declared once")
            if verb == "fixture" and isinstance(val, dict) and val.get("name") not in fx_names:
                errs.append(f"{where}: no fixture named '{val.get('name')}'")
            if verb == "clear_data" and not raw.get("allow_destructive"):
                errs.append(f"{where}: clear_data wipes app data - set allow_destructive: true at the top level")
            st.verb, st.value = verb, val
        elif st.kind == "goal":
            st.goal_t = str(s["goal"])
            for m in __import__("re").findall(r"\$(\w+)", st.goal_t):
                if m not in inputs:
                    errs.append(f"{where}: goal references ${m} but inputs has no '{m}'")
                else:
                    st.refs.append(m)
            st.goal = render_goal(st.goal_t, inputs, secret_names)
            ur = s.get("until_requests")
            if ur is not None:
                if not (isinstance(ur, dict) and ur.get("fixture") in fx_names and ur.get("path")):
                    errs.append(f"{where}: until_requests needs {{fixture: <declared fixture>, path: <substring>}}")
                else:
                    st.until_req = ur
            st.until = str(s.get("until", ""))
            st.until_ui_has = str(s.get("until_ui_has", ""))  # both optional: omitted -> inferred by the runtime
            if st.until and st.until.lower().startswith(("open ", "go to ", "navigate", "tap ", "click ")):
                warns.append(f"{where}: 'until' reads like an action - phrase it as what is TRUE on screen")
            for q in __import__("re").findall(r'"([^"]+)"', st.goal):
                if q not in [str(v) for v in inputs.values()] and not (q.startswith("<") and q.endswith(">")):
                    warns.append(f'{where}: "{q}" is quoted in the goal but is not in inputs - the runtime types only inputs values')
        else:
            ex = s["expect"]
            if not isinstance(ex, list) or not ex:
                errs.append(f"{where}: expect must be a non-empty list")
                continue
            norm = []
            for e in ex:
                if not isinstance(e, dict) or len(e) != 1:
                    errs.append(f"{where}: each expectation is a single {{oracle: value}}")
                    continue
                k, v = next(iter(e.items()))
                if k not in ORACLES:
                    errs.append(f"{where}: unknown oracle '{k}' (allowed: {', '.join(ORACLES)})")
                    continue
                if k in ("requests_has", "requests_lacks") and not (isinstance(v, dict) and v.get("fixture") in fx_names and v.get("path")):
                    errs.append(f"{where}: {k} needs {{fixture: <declared fixture>, path: <substring>}}")
                    continue
                norm.append((k, v))
            st.expect = norm
            if norm and all(k == "judge" for k, _ in norm):
                warns.append(f"{where}: only model-judged expectations - add a deterministic oracle (ui_has/logcat_*) if code can measure it")
        steps.append(st)
    if errs:
        return None, errs, warns
    sc = Scenario(id=str(raw["scenario"]).strip(), title=str(raw.get("title", raw["scenario"])), platforms=plats,
                  inputs=inputs, invariants=list(inv) if inv is not None else sorted(INVARIANTS),
                  allow=[str(a) for a in allow], steps=steps, raw=raw, secrets=secret_names,
                  fixtures=fixtures, fixture_inputs=fixture_inputs,
                  hints=[str(h) for h in (raw.get("hints") or [])])
    return sc, [], warns


def authoring_guide() -> str:
    head = __doc__.split('"""')[0].strip()
    verbs = ACT.guide_table()
    orcs = "\n".join(f"  {k:15s} {v}" for k, v in ORACLES.items())
    return f"""SIMPLEST FORM (use this first - a plain sentence per step; the runtime infers when each is done):

scenario: settings-search
platform: phone
inputs: {{text: "Bluetooth"}}
steps:
  - open the settings search
  - type $text into the search field
  - open the Bluetooth result

Add `expect:` steps / `until_ui_has:` only when you need a hard check.

FULL SCHEMA
SCENARIO YAML (you are the compiler: write this from the user's intent; validate fixes mistakes in one call)

{head}

ACTIONS (deterministic, free; use as `- tap: Settings`, `- key: [down, down, select]`, `- type: $text`, `- {{delete: all}}`, `- paste: $text`):
[platforms] = where a verb is implemented; the validator rejects a verb on a platform that lacks it.
{verbs}

EXPECTATION ORACLES (code first, model last):
{orcs}

STEP KINDS
  do:      one verb.                 - {{id: s1, do: launch}}   - {{id: s2, do: {{tap: "Settings"}}}}   - {{id: s3, do: {{type: $query}}}}
  goal:    DYNAMIC. Optional done-conditions, strongest first: `until_requests: {{fixture: api, path: /items}}` (the app's own
           network call reached the fixture - deterministic), `until_ui_has: "text"`, `until: "what is TRUE on screen"`. The decision model picks the next target from what is actually on screen (code does the
           how). Needs a concrete `until` (what is TRUE when done) and/or `until_ui_has: "<text>"` (deterministic,
           preferred). Successful routes are cached and replayed on later runs (1 model call instead of ~5).
  expect:  list of oracles; every one must hold. severity: warn downgrades a failure to a warning.
Any step: only: [tv] restricts it to a platform. Use separate goals for TV vs phone when the nav differs.

WHEN TO USE `judge` (it asks the decision model a yes/no question about the screen)
  Good for what code cannot measure: "a video is playing" (vision: true), "an error message is shown", "this is an empty
  state, not a loading spinner", "the list looks sorted/populated", "text is in the user's language", "layout is not
  broken/overlapping". Options: vision: true (attach two screenshots 1 s apart), min_p: 0.6 (raise for stricter), severity: warn.
  Write the claim as a statement that is TRUE or FALSE of the screen, one claim per judge. The report shows the probability.
  Prefer a code oracle whenever one exists; use judge for the rest. Same call works as a goal's `until`.

FIXTURES (a controlled world: local servers the app talks to; URLs are late-bound into inputs)
  fixtures: [{{kind: http, name: api, routes: {{"/items": {{"json": [{{"id": 1}}]}}, "/slow-items": {{"json": [], "delay_ms": 8000}}}}}},
             {{kind: command, name: mine, cmd: [python3, my_mock.py, --port, "{{port}}", --log, "{{log}}"], fields: {{url: "{{base}}/feed"}}}}]
  inputs:   api_url: {{fixture: api, field: base}}             # http fields: base (+ /slow?ms=N, /status/<code>, /drop built in)
  evidence: expect: [{{requests_has: {{fixture: api, path: /items}}}}]   and   until_requests: {{fixture: api, path: /items}}
            (the app's OWN network call, deterministic; far more reliable than judging a screen). requests_lacks asserts it did NOT call.
  control:  - fixture: {{name: mine, get: "/__reset"}}         # any control path your own mock server understands

HINTS: `hints: ["Account settings are reached through the avatar button (top-right)"]` - app knowledge you (the compiler) know
  and the screen does not say. The decision model reads it on every pick. Use it for hidden navigation, not for scripting steps.

RULES THAT MAKE SCENARIOS RELIABLE
  - Prefer ui_has/ui_lacks/logcat_*/stable over judge. Judge only what code cannot measure (video playing, 'looks right').
  - A goal's text is typed only if it comes from `inputs`. Quote input values in the goal: goal: 'Search for "Bluetooth"'.
  - Add expect steps AFTER the actions you care about; the run stops on the first failed `fail` step.
  - The risk gate blocks purchase/rent/subscribe/sign-out/delete labels; list labels in `allow:` only if the test needs them.
  - Invariants default to app_foreground + no_crash after every step.

EXAMPLE
scenario: search-opens-result
title: Search the Settings app and open a result
platforms: [phone]
inputs: {{query: "Bluetooth"}}
steps:
  - {{id: boot, do: launch}}
  - {{id: clear, do: logcat_clear}}
  - id: reach
    goal: 'Search for "Bluetooth" and open the Bluetooth settings'
    until: "The Bluetooth settings screen is shown"
  - id: check
    expect:
      - ui_has: "Bluetooth"
      - logcat_lacks: "FATAL EXCEPTION"
"""
