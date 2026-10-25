import os, sys, tempfile, unittest
from simdecision.graph import ScreenGraph

HOME = ["Home", "Search", "Library", "Settings", "Play"]
SETTINGS = ["Settings", "Account", "Layout", "Integrations", "Close"]
INTEG = ["Integrations", "Calendar", "Mail", "Close", "Back"]


class G(unittest.TestCase):
    def build(self):
        g = ScreenGraph()
        h, s, i = g.node_for(HOME), g.node_for(SETTINGS), g.node_for(INTEG)
        g.add_edge(h, "tap:Settings", s); g.add_edge(s, "tap:Integrations", i)
        return g, h, s, i

    def test_same_screen_matches_with_noise(self):
        g, h, *_ = self.build()
        self.assertEqual(g.node_for(HOME + ["Poster 123456789", "[text field] x", "(unlabelled)"]), h)

    def test_different_screen_is_a_new_node(self):
        g, *_ = self.build()
        self.assertEqual(len(g.nodes), 3)
        self.assertEqual(g.node_for(["Alpha", "Beta", "Gamma", "Delta"]), "n4")

    def test_sparse_screen_is_no_node(self):
        self.assertIsNone(ScreenGraph().node_for(["OK", "Cancel"]))

    def test_path_two_hops_and_reuse(self):
        g, h, s, i = self.build()
        self.assertEqual([e["action"] for e in g.path_to(h, i)], ["tap:Settings", "tap:Integrations"])
        self.assertEqual([e["action"] for e in g.path_to(s, i)], ["tap:Integrations"])  # a later goal reuses the learned edge
        self.assertIsNone(g.path_to(i, h))                                                # no edge back yet: honest None

    def test_naming_and_reachable(self):
        g, h, s, i = self.build()
        self.assertEqual(g.name(i), "Integrations")
        self.assertEqual(g.reachable_named(h), {s: 'the screen reached by tapping "Settings"', i: 'the screen reached by tapping "Integrations"'})
        g2 = ScreenGraph(); a = g2.node_for(HOME); b = g2.node_for(SETTINGS, title="Settings"); g2.add_edge(a, "tap:Profile", b)
        self.assertIn('"Settings" screen (reached by tapping "Profile")', g2.reachable_named(a)[b])

    def test_failing_edge_is_dropped_after_two_misses(self):
        g, h, s, _ = self.build()
        g.fail(h, "tap:Settings"); self.assertIsNotNone(g.path_to(h, s))
        g.fail(h, "tap:Settings"); self.assertIsNone(g.path_to(h, s))

    def test_changed_destination_overwrites(self):
        g, h, s, i = self.build()
        g.add_edge(h, "tap:Settings", i)
        self.assertEqual(g.path_to(h, i)[0]["to"], i)

    def test_persists(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "g.json")
            g = ScreenGraph(p); h = g.node_for(HOME); s = g.node_for(SETTINGS); g.add_edge(h, "tap:Settings", s); g.save()
            self.assertEqual(len(ScreenGraph(p).edges), 1)

    def test_shared_chrome_edge_is_inferred_and_a_miss_is_remembered(self):
        g = ScreenGraph()
        a = g.node_for(["Home", "Search", "Library", "Profile", "Play"])
        b = g.node_for(["Library", "Home", "Search", "Profile", "Saved", "Servers"])
        c = g.node_for(["Settings", "Account", "Layout", "Close", "Back"])
        g.add_edge(a, "tap:Profile", c)                       # learned only from the first screen
        p = g.path_to(b, c)                                   # b also shows "Profile": one inferred hop
        self.assertEqual([(e["action"], e.get("inferred")) for e in p], [("tap:Profile", True)])
        g.fail(b, "tap:Profile")                              # it did not arrive there
        self.assertIsNone(g.path_to(b, c))
        self.assertIn((b, "tap:Profile"), g.bad)

    def test_self_loop_not_recorded(self):
        g = ScreenGraph(); h = g.node_for(HOME); g.add_edge(h, "tap:Play", h)
        self.assertEqual(g.edges, [])


if __name__ == "__main__":
    unittest.main()
