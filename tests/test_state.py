"""Tests de numa_state : moyenne lissée, délai après déplacement, mesure, fichier d'état."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import numa_state as st  # noqa: E402


class StateTest(unittest.TestCase):
    def test_smooth_starts_with_first_sample_then_averages(self):
        s1 = st.smooth({}, {"a": 4.0})
        s2 = st.smooth(s1, {"a": 0.0})
        self.assertEqual(st.usage(s1, "a"), 4.0)
        self.assertEqual(st.usage(s2, "a"), 2.0)
        self.assertEqual(s1, {"a": {"ema": 4.0}})  # pas de mutation de l'état précédent

    def test_cooldown(self):
        s = st.mark_moved({}, ["a"], now=1000.0)
        self.assertTrue(st.in_cooldown(s, "a", now=1000.0 + st.COOLDOWN_S - 1))
        self.assertFalse(st.in_cooldown(s, "a", now=1000.0 + st.COOLDOWN_S + 1))
        self.assertFalse(st.in_cooldown(s, "b", now=1000.0))
        self.assertFalse(st.in_cooldown({"c": {"ema": 1.0}}, "c", now=5.0))  # jamais déplacée

    def test_home_is_set_on_first_move_and_cleared_on_return(self):
        s = st.record_moves({}, {"a": ((1,), (2,))}, {"a": "7"}, now=10.0)
        self.assertEqual(st.home(s, "a", "7"), (1,))
        s = st.record_moves(s, {"a": ((2,), (3,))}, {"a": "7"}, now=20.0)
        self.assertEqual(st.home(s, "a", "7"), (1,))      # l'origine de la RAM ne change pas
        s = st.record_moves(s, {"a": ((3,), (1,))}, {"a": "7"}, now=30.0)
        self.assertEqual(st.home(s, "a", "7"), ())        # revenue : plus d'origine à retenir
        self.assertEqual(s["a"]["moved"], 30.0)

    def test_home_is_forgotten_after_reboot(self):
        s = st.record_moves({}, {"a": ((1,), (2,))}, {"a": "7"}, now=10.0)
        self.assertEqual(st.home(s, "a", "9"), ())        # domid différent : RAM réallouée
        self.assertEqual(st.home(s, "a", ""), (1,))       # VM arrêtée : on garde l'information

    def test_measure_uses_cpu_time_delta(self):
        reads = iter([{"a": 10.0, "b": 5.0}, {"a": 30.0, "c": 1.0}])
        clock = iter([100.0, 110.0])
        out = st.measure(lambda: next(reads), 10, sleep=lambda s: None, clock=lambda: next(clock))
        self.assertEqual(out, {"a": 2.0})  # b arrêté, c démarré : ignorés

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "state.json")
            st.save(path, {"a": {"ema": 1.5, "moved": 3.0}})
            self.assertEqual(st.load(path), {"a": {"ema": 1.5, "moved": 3.0}})
            self.assertEqual(st.load(os.path.join(tmp, "absent.json")), {})


if __name__ == "__main__":
    unittest.main()
