"""Pure pre-action guards (no device, no model): unit-tested in tests/test_guards.py.

Ideas borrowed from browser-use/jev-ultrafast: the model only ever picks an index from a table that CODE owns; before
anything mutates, code re-resolves the target on a FRESH tree, checks the screen is still the one the decision was made on,
and checks nothing sits on top of the tap point. Any doubt -> do not act (a "pre-mutation" error: nothing happened, so the
loop can safely re-observe). Mutations are never retried.
"""
import re


def label_set(items):
    return {i["label"] for i in items if i.get("label") and i["label"] != "(unlabelled)"}


def screen_signature(labels, text):
    """Labels alone cannot tell screens apart when a shared tab bar dominates; the visible text fragments (headings,
    empty-state messages) do. Fragments are prefixed so they never collide with a tap label."""
    head = text.split("\n", 1)[0]
    frags = {"§" + f.strip() for f in head.removeprefix("TEXT:").split(" | ") if f.strip()}
    return set(labels) | frags


def screen_title(text):
    """First visible text fragment (usually the screen heading)."""
    for f in text.split("\n", 1)[0].removeprefix("TEXT:").split(" | "):
        if f.strip():
            return f.strip()[:40]
    return ""


def jaccard(a, b):
    return len(a & b) / len(a | b) if (a | b) else 1.0


def screen_changed(expected, items, floor=0.5):
    """True when the fresh tree is a different screen from the one the decision was made on. Screens with fewer than 3
    labelled items carry too little signal to judge, so they never block."""
    if not expected or len(expected) < 3:
        return False
    return jaccard(expected, label_set(items)) < floor


def _box(node):
    return tuple(map(int, re.findall(r"-?\d+", node.get("bounds", "[0,0][0,0]"))))


def occluder(root, target_box, pkg):
    """Android: the first node DRAWN AFTER the target (later in document order = on top) that contains the target's tap
    point, is not part of the target's own subtree, and can actually receive/show something there. Same-package nodes
    must be clickable (plain layout wrappers and caption text overlap legitimately); other packages (IME, system bars)
    count if they show anything. Returns a short description or None."""
    pkgs = {pkg} if isinstance(pkg, str) else set(pkg)
    cx, cy = (target_box[0] + target_box[2]) // 2, (target_box[1] + target_box[3]) // 2
    nodes = list(root.iter("node"))
    idx = next((i for i, n in enumerate(nodes) if _box(n) == tuple(target_box)), None)
    if idx is None:
        return None
    sub = set(id(n) for n in nodes[idx].iter())
    for n in nodes[idx + 1:]:
        if id(n) in sub:
            continue
        x1, y1, x2, y2 = _box(n)
        if not (x1 <= cx < x2 and y1 <= cy < y2) or x2 <= x1 or y2 <= y1:
            continue
        shows = (n.get("text") or n.get("content-desc") or "").strip()
        if n.get("package") in pkgs:
            if n.get("clickable") == "true":
                return shows or n.get("class", "view").split(".")[-1]
        elif n.get("clickable") == "true" or shows:
            return shows or f'{n.get("package")} window'
    return None


class ProgressBreaker:
    """Stops a run that keeps acting on a screen that never changes (a dead tap target, a swallowed press)."""

    def __init__(self, limit=3):
        self.limit, self.n, self.last = limit, 0, None

    def note(self, sig, acted):
        """Call once per observation. `acted`: an action ran since the previous observation. True -> give up."""
        if acted and sig == self.last:
            self.n += 1
        elif sig != self.last:
            self.n = 0
        self.last = sig
        return self.n >= self.limit
