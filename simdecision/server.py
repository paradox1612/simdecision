#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2.3,<3", "pyyaml>=6"]
# ///
"""simdecision MCP server - an agent writes a test scenario, a cheap decision-model runtime runs it on simulators and
reports back one compact verdict.

  agent         intent --writes--> scenario YAML --validate--> clean YAML
  run           YAML -> per-platform runtime: deterministic verbs + dynamic goals (decision model) +
                oracles (code first, model last) + invariants -> report (one compact text block)

  claude mcp add simdecision -- simdecision mcp      (run from the project folder holding simdecision.yaml)

Provider: TSIM_PROVIDER=openai|clef, OPENAI_DECISION_MODEL (default gpt-6-luna), OPENAI_BUDGET_USD guard.
"""
import json, os, sys, threading

from simdecision import core as tsim  # noqa: E402

tsim.load_env()
tsim.PROVIDER = os.environ.get("TSIM_PROVIDER", "openai" if os.environ.get("OPENAI_API_KEY") else "clef")
tsim.OPENAI_USD_PER_TOKEN = float(os.environ.get("OPENAI_USD_PER_MTOK", "0.10")) / 1_000_000

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from simdecision import devicelock, engine, prepare as prepare_mod, scenario  # noqa: E402
from simdecision.android import emulators
from simdecision import config  # noqa: E402
from simdecision.apple import booted_tvos  # noqa: E402
from simdecision.ios import booted_iphones  # noqa: E402

mcp = MCPServer("simdecision")
LOCK = threading.Lock()  # one device lane at a time (emulators + builds overlapping can exhaust memory)
SCEN_DIR = config.state("scenarios")


def _load(yaml_or_id):
    """Accept inline YAML or the id of a saved scenario."""
    text = yaml_or_id
    if "\n" not in yaml_or_id and ":" not in yaml_or_id:
        p = os.path.join(SCEN_DIR, yaml_or_id + ".yaml")
        if not os.path.exists(p):
            raise ToolError(f"no saved scenario '{yaml_or_id}' (list_scenarios shows them)")
        text = open(p).read()
    sc, errs, warns = scenario.parse(text)
    if errs:
        raise ToolError("scenario invalid:\n- " + "\n- ".join(errs))
    return sc, text, warns


@mcp.tool()
def schema() -> str:
    """READ THIS FIRST. The scenario YAML schema: verbs, oracles, step kinds, rules and an example. You write the
    YAML from the user's intent (you are the compiler); then call validate, then run."""
    return scenario.authoring_guide()


@mcp.tool()
def validate(scenario_yaml: str) -> str:
    """Check a scenario's YAML. Returns every error at once (fix them in one pass) plus warnings and a per-platform
    plan of what will run, with an estimate of how many steps use the decision model. Free - no device, no model."""
    sc, errs, warns = scenario.parse(scenario_yaml)
    if errs:
        return "INVALID\n- " + "\n- ".join(errs) + (("\nwarnings:\n- " + "\n- ".join(warns)) if warns else "")
    lines = [f"VALID {sc.id} - {sc.title}", f"platforms: {', '.join(sc.platforms)}  inputs: {list(sc.inputs)}"]
    for p in sc.platforms:
        steps = [s for s in sc.steps if not s.only or p in s.only]
        kinds = {k: sum(s.kind == k for s in steps) for k in ("do", "goal", "expect")}
        judges = sum(k == "judge" for s in steps if s.kind == "expect" for k, _ in s.expect)
        cached = sum(os.path.exists(engine.route_path(sc.id, p, s.id)) for s in steps if s.kind == "goal")
        lines.append(f"  {p}: {len(steps)} steps ({kinds['do']} do, {kinds['goal']} goal [{cached} with cached route], "
                     f"{kinds['expect']} expect, {judges} model-judged)")
    return "\n".join(lines + (["warnings:"] + ["- " + w for w in warns] if warns else []))


@mcp.tool()
def run(scenario_yaml: str, platforms: list[str] | None = None, replay: bool = True, save: bool = True) -> str:
    """Run a scenario (inline YAML or the id of a saved one) on the Android emulators and return a compact report:
    one verdict line, one line per step, failing expectations, and evidence paths (screenshot + screen text) for
    anything that failed. platforms defaults to the scenario's own list. replay=False forces dynamic re-planning
    (ignores cached routes). Verdicts: PASS | FAIL (expectation false = finding) | BLOCKED (could not reach the
    place under test: bug OR harness limit, look at the evidence) | ERROR (infra) | SKIPPED. Runs one device at
    a time; boot the needed emulator first (the error tells you how)."""
    sc, text, warns = _load(scenario_yaml)
    if save and "\n" in scenario_yaml:
        os.makedirs(SCEN_DIR, exist_ok=True)
        open(os.path.join(SCEN_DIR, sc.id + ".yaml"), "w").write(text)
    plats = platforms or sc.platforms
    out = []
    with LOCK:
        for p in plats:
            rep = engine.run_scenario(sc, p, replay=replay)
            out.append(rep.summary())
    return "\n\n".join(out) + (("\nwarnings: " + "; ".join(warns)) if warns else "")


@mcp.tool()
def prepare(platform: str, rebuild_bridge: bool = False, free_others: bool = False) -> str:
    """Get one platform ready to run scenarios: boots the emulator/simulator if needed (swapping the single Android
    emulator when required), checks the app under test is installed and says when, builds the XCUITest bridge if missing
    or stale, starts it, and returns READY / NOT READY with the exact fix for anything it cannot do (it never builds or
    installs the app). platform: phone | tv | tvos | ios. free_others=True shuts down idle devices of other lanes to
    free memory on this machine. Takes the device lane lock, so it never disturbs a run in progress."""
    try:
        return prepare_mod.prepare(platform, rebuild_bridge, free_others)
    except devicelock.DeviceBusy as e:
        raise ToolError(str(e))


@mcp.tool()
def report(run_id: str) -> str:
    """Full markdown report for a past run (steps, every oracle result, evidence paths)."""
    p = os.path.join(engine.REPORTS, run_id, "report.md")
    if not os.path.exists(p):
        raise ToolError(f"no report '{run_id}'")
    return open(p).read()


@mcp.tool()
def list_scenarios() -> str:
    """Saved scenarios, which cached routes exist, and the most recent runs."""
    lines = ["scenarios:"]
    for f in sorted(os.listdir(SCEN_DIR)) if os.path.isdir(SCEN_DIR) else []:
        if f.endswith(".yaml"):
            sc, errs, _ = scenario.parse(open(os.path.join(SCEN_DIR, f)).read())
            lines.append(f"  {f[:-5]}: {sc.title if sc else 'INVALID ' + errs[0]}")
    runs = sorted(os.listdir(engine.REPORTS))[-8:] if os.path.isdir(engine.REPORTS) else []
    lines.append("recent runs: " + (", ".join(runs) or "none"))
    return "\n".join(lines)


@mcp.tool()
def status() -> str:
    """Which emulators are running, which provider/model the runtime will use, and the spend guard."""
    emus = emulators()
    spent = 0.0
    if os.path.exists(tsim.SPEND_FILE):
        spent = json.load(open(tsim.SPEND_FILE)).get("usd", 0)
    lanes = {k: (f"BUSY: {v['label']} (pid {v['pid']})" if v else "free") for k, v in devicelock.status().items()}
    return (f"device lanes: {lanes}\n"
            f"emulators: {emus or 'none running'}\n"
            f"iOS simulators booted: {booted_iphones() or 'none'} (bridge port {os.environ.get('QA_IOS_BRIDGE_PORT', '8786')}; prepare builds it)\n"
            f"tvOS simulators booted: {booted_tvos() or 'none'} (bridge port {os.environ.get('QA_BRIDGE_PORT', '8785')}; prepare builds it)\n"
            f"provider: {tsim.PROVIDER}  model: {os.environ.get('OPENAI_DECISION_MODEL', 'gpt-6-luna') if tsim.PROVIDER == 'openai' else 'clef'}\n"
            f"openai spend so far ${spent:.4f} of ${os.environ.get('OPENAI_BUDGET_USD', '2.50')}\n"
            f"platforms with a driver: phone, tv, tvos, ios")


def main():
    mcp.run()


if __name__ == "__main__":
    main()
