"""Offline mutation-matrix tests for the pre-action guards: each guard must FIRE on its mutation and stay quiet on the
unrelated change. No device, no model. Run: python3 -m unittest discover -s tests"""
import os, sys, unittest
import xml.etree.ElementTree as ET
from simdecision import guards
from simdecision import core as tsim
from simdecision import runtime as rt

PKG = "com.app"


def tree(*nodes):
    body = "".join(
        f'<node package="{p}" clickable="{str(c).lower()}" text="{t}" content-desc="" class="android.view.View" bounds="[{b[0]},{b[1]}][{b[2]},{b[3]}]"/>'
        for (p, c, t, b) in nodes)
    return ET.fromstring(f"<hierarchy>{body}</hierarchy>")


BTN = (PKG, True, "Play", (100, 100, 300, 200))


class Occluder(unittest.TestCase):
    def test_clear_target_is_not_covered(self):
        self.assertIsNone(guards.occluder(tree(BTN), BTN[3], PKG))

    def test_keyboard_over_target_fires(self):
        r = tree(BTN, ("com.google.ime", False, "", (0, 150, 400, 400)))  # a bare IME container shows nothing: ignored
        self.assertIsNone(guards.occluder(r, BTN[3], PKG))
        r2 = tree(BTN, ("com.google.ime", True, "space", (100, 100, 300, 200)))
        self.assertEqual(guards.occluder(r2, BTN[3], PKG), "space")

    def test_clickable_scrim_drawn_later_fires(self):
        self.assertIsNotNone(guards.occluder(tree(BTN, (PKG, True, "", (0, 0, 400, 800))), BTN[3], PKG))

    def test_layout_wrapper_and_caption_text_do_not_fire(self):
        r = tree(BTN, (PKG, False, "", (0, 0, 400, 800)), (PKG, False, "caption", (120, 120, 280, 180)))
        self.assertIsNone(guards.occluder(r, BTN[3], PKG))

    def test_node_drawn_before_target_does_not_fire(self):
        self.assertIsNone(guards.occluder(tree((PKG, True, "", (0, 0, 400, 800)), BTN), BTN[3], PKG))

    def test_own_child_does_not_fire(self):
        self.assertIsNone(guards.occluder(ET.fromstring(
            f'<hierarchy><node package="{PKG}" clickable="true" text="Play" bounds="[100,100][300,200]">'
            f'<node package="{PKG}" clickable="true" text="icon" bounds="[100,100][300,200]"/></node></hierarchy>'), BTN[3], PKG))

    def test_target_off_the_tap_point_not_covered_by_neighbour(self):
        self.assertIsNone(guards.occluder(tree(BTN, (PKG, True, "Next", (300, 100, 500, 200))), BTN[3], PKG))


class ScreenBinding(unittest.TestCase):
    A = {"Home", "Search", "Library", "Settings", "Play"}

    def items(self, labels):
        return [{"label": l} for l in labels]

    def test_same_screen_with_one_new_poster_is_fine(self):
        self.assertFalse(guards.screen_changed(self.A, self.items(self.A | {"New poster"})))

    def test_different_screen_fires(self):
        self.assertTrue(guards.screen_changed(self.A, self.items({"Account", "Switch Profile", "Close", "Layout"})))

    def test_sparse_screens_never_block(self):
        self.assertFalse(guards.screen_changed({"OK"}, self.items({"Cancel"})))


class Breaker(unittest.TestCase):
    def test_three_unchanged_actions_stop(self):
        b = guards.ProgressBreaker(3)
        self.assertFalse(b.note("a", False))
        self.assertFalse(b.note("a", True)); self.assertFalse(b.note("a", True))
        self.assertTrue(b.note("a", True))

    def test_change_resets(self):
        b = guards.ProgressBreaker(3)
        b.note("a", False); b.note("a", True); b.note("a", True)
        self.assertFalse(b.note("b", True))
        self.assertFalse(b.note("b", True))

    def test_waiting_without_acting_is_not_counted(self):
        b = guards.ProgressBreaker(3)
        for _ in range(10):
            self.assertFalse(b.note("a", False))


class Refused(Exception):
    pre_mutation = True


class Surf(rt.Surface):
    """Fake surface: option 0 is refused before mutation, option 1 acts but changes nothing."""
    name = "fake"

    def __init__(self):
        self.taps = []

    def observe(self):
        return rt.Obs(text="SCREEN same")

    def options(self, obs):
        def refuse():
            self.taps.append("refused-call"); raise Refused("covered")
        return [rt.Option("o0", "covered", refuse)] + [
            rt.Option(f"o{i}", f"dead{i}", lambda: self.taps.append("dead-tap")) for i in range(1, 6)]

    def after_action(self, settle):
        pass


class Loop(unittest.TestCase):
    def setUp(self):
        self.real = tsim.decide
        self.picks = []

        def fake(state, qs, images=None, model=None):
            first = self.picks.pop(0) if self.picks else "o1"
            probs = {f"o{i}": 0.0 for i in range(6)}
            probs["none"] = 0.0
            probs[first] = 1.0
            return {"next": {"probabilities": probs}}, 10
        tsim.decide = fake

    def tearDown(self):
        tsim.decide = self.real

    def test_refused_target_is_not_retried_and_loop_continues(self):
        s = Surf()
        self.picks = ["o0", "o1", "o1", "o1", "o1"]
        res = rt.run(s, rt.StandingOrder(goal="g", done="d", max_steps=8, done_fn=lambda o: False))
        self.assertEqual(s.taps.count("refused-call"), 1, "a refused (pre-mutation) option must not be picked again on the same screen")
        self.assertGreaterEqual(s.taps.count("dead-tap"), 1)

    def test_dead_screen_stops_after_three_actions(self):
        s = Surf()
        self.picks = ["o1", "o2", "o3", "o4", "o5"]
        res = rt.run(s, rt.StandingOrder(goal="g", done="d", max_steps=10, done_fn=lambda o: False))
        self.assertEqual(res.outcome, "STUCK")
        self.assertIn("no progress", res.detail)
        self.assertEqual(s.taps.count("dead-tap"), 3)

    def test_non_pre_mutation_errors_propagate(self):
        s = Surf()
        s.options = lambda obs: [rt.Option("o0", "boom", lambda: (_ for _ in ()).throw(RuntimeError("after tap")))]
        self.picks = ["o0"]
        with self.assertRaises(RuntimeError):
            rt.run(s, rt.StandingOrder(goal="g", done="d", max_steps=3, done_fn=lambda o: False))


if __name__ == "__main__":
    unittest.main()


class Signature(unittest.TestCase):
    def test_same_tab_bar_different_headings_differ(self):
        tabs = {"Home", "Search", "Library", "Live", "Sports", "Profile"}
        a = guards.screen_signature(tabs, "TEXT: Library | Saved | Your library is empty\nTAPPABLE:")
        b = guards.screen_signature(tabs, "TEXT: Search | Search movies, shows... | Discover\nTAPPABLE:")
        self.assertLess(guards.jaccard(a, b), 0.7)
