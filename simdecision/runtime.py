"""s1 runtime — the generic System-1 loop. Knows nothing about any particular system.

A system plugs in by implementing `Surface` (written by System 2 / Claude for each new system):
    observe()  -> Obs(text, sig, images)        what the loop sees (compact text; sig = change fingerprint)
    options(o) -> [Option(key, desc, risk, run)] grounded actions available NOW (risk: safe|approve|deny)
    checks(o)  -> {name: yes/no question}        optional extra invariants for watch mode

A standing order says what to achieve and when it's done:
    StandingOrder(goal, done="concrete observable condition", policy="...", max_steps=..)

Lessons baked in (from earlier experiments): the model picks WHAT among enumerated options, code does HOW;
risky options are masked BEFORE ranking (approve → escalate, deny → never offered); done is a concrete
condition; "probably done" → re-observe instead of acting; loop guard; low confidence → escalate;
unchanged observation → no model call at all.
"""
import hashlib, json, os, sys, time
from dataclasses import dataclass, field
from typing import Callable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
from .guards import ProgressBreaker  # noqa: E402
from . import core as tsim  # noqa: E402


@dataclass
class Obs:
    text: str
    sig: str = ""
    images: Optional[list] = None

    def __post_init__(self):
        if not self.sig:
            self.sig = hashlib.md5(self.text.encode()).hexdigest()


@dataclass
class Option:
    key: str
    desc: str
    run: Callable[[], None]
    risk: str = "safe"  # safe | approve (needs a human: escalate instead of acting) | deny (never offered)


@dataclass
class StandingOrder:
    goal: str
    done: str
    policy: str = ""
    max_steps: int = 20
    done_at: float = 0.85
    pick_floor: float = 0.30
    done_fn: Optional[Callable[["Obs"], bool]] = None  # code-evaluated done check: skips the model's done question
    # Optional (question, context) for a done check that needs the model: it rides in the SAME request as the
    # next-action choice (one round trip per decision), instead of a separate call before it.
    done_q: Optional[Callable[["Obs"], Optional[tuple]]] = None


@dataclass
class Result:
    outcome: str  # PASS | FAIL | STUCK | ESCALATE | ERROR
    steps: int
    secs: float
    tokens: int
    detail: str
    trace: list = field(default_factory=list)

    def line(self, name=""):
        usd = self.tokens * (tsim.OPENAI_USD_PER_TOKEN if tsim.PROVIDER == "openai" else tsim.CLEF_USD_PER_TOKEN)
        return f"{self.outcome} {name} {self.steps} steps {self.secs:.1f}s · {self.detail} · {self.tokens} tok ${usd:.5f}"


class Surface:
    name = "surface"

    def observe(self) -> Obs: raise NotImplementedError
    def options(self, obs: Obs) -> list: raise NotImplementedError
    def checks(self, obs: Obs) -> dict: return {}
    vision_next = False  # set by the loop when the text tree was not enough: attach one screenshot to the next observation

    def after_action(self, settle: float):
        """Wait for the screen to react to the last action. Default: fixed sleep; surfaces override with an event wait."""
        time.sleep(settle)


def _ask(state, questions, images=None):
    ans, used = tsim.decide(state, questions, images=images)
    return ans, used


def run(surface: Surface, order: StandingOrder, log_path: Optional[str] = None, settle: float = 0.8) -> Result:
    """Task mode: act until the standing order's done-condition holds."""
    t0, tokens, trace, history, tried, holds, none_waits = time.time(), 0, [], [], {}, 0, 0
    breaker, acted = ProgressBreaker(3), False
    log = open(log_path, "a") if log_path else None
    step = 0
    try:
        for step in range(1, order.max_steps + 1):
            obs = surface.observe()
            if breaker.note(obs.sig, acted):  # jev: N actions in a row that changed nothing -> stop paying for a dead screen
                return Result("STUCK", step, time.time() - t0, tokens, "no progress: 3 actions in a row left the screen unchanged", trace)
            acted = False
            opts = [o for o in surface.options(obs) if o.risk != "deny"]
            safe = {f"o{i}": o for i, o in enumerate(opts) if o.risk == "safe"}
            gated = {f"o{i}": o for i, o in enumerate(opts) if o.risk == "approve"}
            crit = {k: o.desc for k, o in safe.items()}
            crit.update({k: f"{o.desc} [REQUIRES HUMAN APPROVAL]" for k, o in gated.items()})
            crit["none"] = "no listed action makes progress toward the goal"
            hist = "\n".join(history[-8:]) or "(none)"
            state = (f"GOAL: {order.goal}\nPOLICY: {order.policy or '(none)'}\nSTEP: {step}\n"
                     f"RECENT ACTIONS:\n{hist}\n\nCURRENT STATE:\n{obs.text}")
            if order.done_fn is not None and order.done_fn(obs):
                trace.append({"step": step, "sig": obs.sig[:8], "action": "DONE (code check)", "tokens": 0})
                return Result("PASS", step, time.time() - t0, tokens, "done (code check)", trace)
            dq = order.done_q(obs) if order.done_q else None
            if dq:
                qs = {"done": {"type": "noul", "instructions": dq[0]}}
                state += "\n\n" + dq[1]
            else:
                qs = {} if order.done_fn is not None else \
                    {"done": {"type": "noul", "instructions": f"Is this true of the CURRENT STATE: {order.done}"}}
            if len(crit) >= 2 or not qs:
                qs["next"] = {"type": "choice", "criteria": crit, "instructions":
                              "Which single listed action makes the most progress toward the GOAL while obeying the POLICY?"}
            ans, used = _ask(state, qs, obs.images)
            tokens += used
            p_done = ans["done"]["noul"] if "done" in ans else 0.0
            rec = {"step": step, "sig": obs.sig[:8], "p_done": round(p_done, 3), "tokens": used}
            if p_done >= order.done_at:
                rec["action"] = "DONE"; trace.append(rec)
                return Result("PASS", step, time.time() - t0, tokens, f"done p={p_done:.2f}", trace)
            if p_done >= 0.6 and holds < 2:  # probably there: look again rather than act
                holds += 1; rec["action"] = "hold"; trace.append(rec); time.sleep(settle * 2); continue
            holds = 0
            nxt = ans.get("next")
            if not nxt:
                rec["action"] = "no-options"; trace.append(rec)
                return Result("STUCK", step, time.time() - t0, tokens, "no options available", trace)
            probs = {k: v for k, v in nxt["probabilities"].items() if tried.get((obs.sig, k), 0) < 2}
            if not probs:
                return Result("STUCK", step, time.time() - t0, tokens, "every option tried twice here", trace)
            pick = max(probs, key=probs.get)
            ranked = sorted(probs.values(), reverse=True) + [0.0]
            rec.update(pick=pick, p=round(ranked[0], 3))
            if (pick == "none" or ranked[0] < order.pick_floor) and none_waits < 3:
                # Nothing fits - but the screen may still be building (chrome / content arrives late). Look again
                # before giving up; a settled screen answers the same way and then escalates below.
                none_waits += 1
                surface.vision_next = True  # text tree could not place the goal: let the multimodal model look once
                rec["action"] = "wait (nothing fits yet; re-observing)"; trace.append(rec)
                time.sleep(max(settle, 1.0) * 2.5)
                continue
            if pick == "none" or ranked[0] < order.pick_floor:
                rec["action"] = "escalate"; trace.append(rec)
                return Result("STUCK", step, time.time() - t0, tokens,
                              f"unsure (top p={ranked[0]:.2f}) — escalate to System 2", trace)
            if pick in gated:
                rec["action"] = f"needs-approval: {gated[pick].desc}"; trace.append(rec)
                return Result("ESCALATE", step, time.time() - t0, tokens, f"approval required: {gated[pick].desc}", trace)
            opt = safe[pick]
            rec["action"] = opt.desc; trace.append(rec)
            if log: log.write(json.dumps(rec) + "\n"); log.flush()
            none_waits = 0
            tried[(obs.sig, pick)] = tried.get((obs.sig, pick), 0) + 1
            history.append(f"{step}. {opt.desc}")
            try:
                opt.run()
            except Exception as e:  # noqa: BLE001
                if not getattr(e, "pre_mutation", False):
                    raise
                # Refused BEFORE touching the device (screen changed / target covered): tell the model, look again, never retry blindly.
                history.append(f"{step}. {opt.desc} -> REFUSED: {e}")
                rec["action"] = f"refused: {e}"
                tried[(obs.sig, pick)] = 2
                continue
            acted = True
            surface.after_action(settle)
        return Result("FAIL", step, time.time() - t0, tokens, "max steps", trace)
    except tsim.ClefError as e:
        return Result("ERROR", step, time.time() - t0, tokens, str(e)[:200], trace)
    finally:
        if log:
            for r in trace[-1:]:
                log.write(json.dumps(r) + "\n")
            log.close()


_last_ok = {}


def check(surface: Surface, conditions: dict, force: bool = False) -> dict:
    """Watch mode: evaluate yes/no invariants on the current state. Unchanged state since the last
    all-pass check → no model call (delta, not dump)."""
    obs = surface.observe()
    key = (surface.name, tuple(sorted(conditions)))
    if not force and _last_ok.get(key) == obs.sig:
        return {"skipped": True, "results": {}, "tokens": 0}
    qs = {f"c{i}": {"type": "noul", "instructions": f"Is this true of the CURRENT STATE: {c}"}
          for i, c in enumerate(conditions.values())}
    ans, used = _ask(f"CURRENT STATE:\n{obs.text}", qs, obs.images)
    res = {name: ans[f"c{i}"]["noul"] for i, name in enumerate(conditions)}
    if all(p >= 0.5 for p in res.values()):
        _last_ok[key] = obs.sig
    return {"skipped": False, "results": res, "tokens": used}
