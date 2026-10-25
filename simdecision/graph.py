"""Screen graph: nodes = screens (fuzzy label sets), edges = verified navigations (tap "X" / back) between them.

Learned from every dynamic run (an edge is recorded only after the NEXT observation shows a different screen), so goals
reuse each other's navigation: "open Integrations" after "open Settings" is a one-hop path with no model call per hop.
Pure and device-free (unit-tested). Persisted per platform in <state_dir>/graphs/. Edges that fail to arrive are dropped (drift).
"""
import json, os, re
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
from . import config  # noqa: E402

GRAPHS = config.state("graphs")
MATCH = 0.7        # Jaccard needed for two label sets to be "the same screen"
MAX_FAILS = 2      # an edge that misses its destination this many times is dropped


def clean(labels):
    """Drop per-visit noise (ids, long titles, counters) so the same screen matches across visits."""
    out = set()
    for l in labels:
        if not l or l == "(unlabelled)" or l.startswith("[text field]") or len(l) > 40 or re.search(r"\d{4,}", l):
            continue
        out.add(l)
    return out


def jaccard(a, b):
    return len(a & b) / len(a | b) if (a | b) else 1.0


class ScreenGraph:
    def __init__(self, path=None):
        self.bad = set()   # (node, action) pairs an INFERRED edge was tried on and did not arrive: never inferred again
        self.path, self.nodes, self.edges = path, {}, []   # nodes: id -> {"labels": [...], "names": {label: n}}
        if path and os.path.exists(path):
            try:
                d = json.load(open(path))
                self.nodes, self.edges = d["nodes"], d["edges"]
                self.bad = {tuple(b) for b in d.get("bad", [])}
            except (ValueError, KeyError):
                pass

    def save(self):
        if self.path:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            json.dump({"nodes": self.nodes, "edges": self.edges, "bad": sorted(self.bad)}, open(self.path, "w"), indent=1)

    def node_for(self, labels, create=True, title=None):
        """-> node id of the best fuzzy match (>= MATCH), else a new node (or None when create=False). Updates the node's sample."""
        labels = clean(labels)
        if len(labels) < 3:
            return None  # too little signal to call it a screen
        best, score = None, 0.0
        for nid, n in self.nodes.items():
            s = jaccard(labels, set(n["labels"]))
            if s > score:
                best, score = nid, s
        if best is not None and score >= MATCH:
            self.nodes[best]["labels"] = sorted(labels)
            if title:
                self.nodes[best]["title"] = title
            return best
        if not create:
            return None
        nid = f"n{len(self.nodes) + 1}"
        self.nodes[nid] = {"labels": sorted(labels), "names": {}, "title": title or ""}
        return nid

    def add_edge(self, src, action, dst):
        """action: "tap:<label>" or "back". Records the edge (or confirms it) and names the destination after the tap label."""
        if not src or not dst or src == dst:
            return
        for e in self.edges:
            if e["from"] == src and e["action"] == action:
                if e["to"] != dst:
                    e["to"], e["fails"] = dst, 0   # the app now goes elsewhere: trust the latest observation
                e["ok"] = e.get("ok", 0) + 1
                break
        else:
            self.edges.append({"from": src, "action": action, "to": dst, "ok": 1, "fails": 0})
        if action.startswith("tap:"):
            names = self.nodes[dst]["names"]
            names[action[4:]] = names.get(action[4:], 0) + 1

    def edges_from(self, cur):
        """Learned edges plus INFERRED ones: a tap label seen leading somewhere from another screen, and also present on this
        one (shared chrome such as a tab bar or profile button), probably leads to the same place. Inferred edges are only
        guesses: the walker verifies arrival and a miss marks (cur, action) bad."""
        real = [e for e in self.edges if e["from"] == cur]
        have = {e["action"] for e in real}
        here = self.nodes.get(cur, {}).get("labels", [])
        seen = {}
        for e in self.edges:
            if e["from"] != cur and e["action"].startswith("tap:") and e["action"] not in have \
                    and e["action"][4:] in here and (cur, e["action"]) not in self.bad and e["to"] != cur:
                seen.setdefault(e["action"], {}).setdefault(e["to"], 0)
                seen[e["action"]][e["to"]] += e.get("ok", 1)
        guessed = [{"from": cur, "action": a, "to": max(d, key=d.get), "ok": 0, "inferred": True} for a, d in seen.items()]
        return real + guessed

    def fail(self, src, action):
        if not any(e["from"] == src and e["action"] == action for e in self.edges):
            self.bad.add((src, action))   # it was an inference: remember the miss
            return
        for e in list(self.edges):
            if e["from"] == src and e["action"] == action:
                e["fails"] = e.get("fails", 0) + 1
                if e["fails"] >= MAX_FAILS:
                    self.edges.remove(e)

    def name(self, nid):
        names = self.nodes.get(nid, {}).get("names", {})
        return max(names, key=names.get) if names else None

    def path_to(self, src, dst):
        """Shortest edge list src -> dst (BFS) or None."""
        if src == dst:
            return []
        seen, q = {src}, deque([(src, [])])
        while q:
            cur, path = q.popleft()
            for e in self.edges_from(cur):
                if e["to"] not in seen:
                    p = path + [e]
                    if e["to"] == dst:
                        return p
                    seen.add(e["to"])
                    q.append((e["to"], p))
        return None

    def describe(self, nid):
        n = self.nodes.get(nid, {})
        name, title = self.name(nid), n.get("title")
        if title and name and title.lower() != name.lower():
            return f'the "{title}" screen (reached by tapping "{name}")'
        return f'the screen reached by tapping "{name}"' if name else f'the "{title}" screen'

    def reachable_named(self, src):
        """{node id: name} for named nodes reachable from src (excluding src): the candidates a goal may ask for."""
        out, seen, q = {}, {src}, deque([src])
        while q:
            cur = q.popleft()
            for e in self.edges_from(cur):
                if e["to"] not in seen:
                    seen.add(e["to"]); q.append(e["to"])
                    if self.name(e["to"]) or self.nodes[e["to"]].get("title"):
                        out[e["to"]] = self.describe(e["to"])
        return out


_cache = {}


def for_platform(platform):
    if platform not in _cache:
        _cache[platform] = ScreenGraph(os.path.join(GRAPHS, f"{platform}.json"))
    return _cache[platform]
