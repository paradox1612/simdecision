"""simdecision command line.

  simdecision run <file.yaml|id> [--platform phone] [--no-replay]   run a scenario, print one verdict line per platform
  simdecision validate <file.yaml>                                  check a scenario, list every error at once
  simdecision prepare <phone|tv|ios|tvos>                           boot/check the device, build the iOS/tvOS bridge
  simdecision mcp                                                   serve the MCP tools on stdio

Reads ./simdecision.yaml (or $SIMDECISION_CONFIG) and ./.env (or $SIMDECISION_ENV).
"""
import argparse, os, sys

from . import config, core as tsim


def _setup():
    tsim.load_env()
    tsim.PROVIDER = os.environ.get("TSIM_PROVIDER", "openai" if os.environ.get("OPENAI_API_KEY") else "clef")
    tsim.OPENAI_USD_PER_TOKEN = float(os.environ.get("OPENAI_USD_PER_MTOK", "0.10")) / 1_000_000


def _scenario_path(ref):
    if os.path.exists(ref):
        return ref
    p = os.path.join(config.state("scenarios"), ref + ".yaml")
    if os.path.exists(p):
        return p
    sys.exit(f"no scenario file or saved scenario named '{ref}'")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="simdecision", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("scenario")
    r.add_argument("--platform", action="append")
    r.add_argument("--no-replay", action="store_true", help="ignore cached routes and the screen graph")
    v = sub.add_parser("validate")
    v.add_argument("scenario")
    p = sub.add_parser("prepare")
    p.add_argument("platform", choices=["phone", "tv", "ios", "tvos"])
    p.add_argument("--rebuild-bridge", action="store_true")
    sub.add_parser("mcp")
    a = ap.parse_args(argv)
    _setup()

    if a.cmd == "mcp":
        from . import server
        return server.main()
    if a.cmd == "prepare":
        from . import prepare
        print(prepare.prepare(a.platform, rebuild_bridge=a.rebuild_bridge))
        return 0
    from . import engine, scenario
    sc, errs, warns = scenario.parse(open(_scenario_path(a.scenario)).read())
    for w in warns:
        print("warn:", w)
    if errs:
        print("INVALID\n- " + "\n- ".join(errs))
        return 1
    if a.cmd == "validate":
        print("VALID", sc.id)
        return 0
    worst = 0
    for plat in a.platform or sc.platforms:
        rep = engine.run_scenario(sc, plat, replay=not a.no_replay)
        print(rep.summary())
        worst = max(worst, 0 if rep.verdict == "PASS" else 1)
    return worst


if __name__ == "__main__":
    sys.exit(main())
