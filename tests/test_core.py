"""Tests du calcul de placement (numa_core), sans aucun accès à l'hôte.

    python3 -m unittest discover -s numa/tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import numa_core as core  # noqa: E402

GB = 1024


def epyc_7551p():
    """4 nœuds de 8 cœurs / 16 threads et 32 Go, comme un EPYC 7551P à 8 barrettes."""
    return [core.Node(n, tuple(range(n * 8, n * 8 + 8)) + tuple(range(32 + n * 8, 40 + n * 8)),
                      8, 32 * GB) for n in range(4)]


def req(key, vcpus, mem_gb, pci=(), current=(), auto=False, manual=False):
    return core.Request(key, vcpus, int(mem_gb * GB), tuple(pci), tuple(current), auto, manual)


class CpuListTest(unittest.TestCase):
    def test_roundtrip(self):
        self.assertEqual(core.format_cpulist(core.parse_cpulist("8-15,40-47,3")), "3,8-15,40-47")

    def test_nodes_of_cpus(self):
        self.assertEqual(core.nodes_of_cpus([1, 40], epyc_7551p()), (0, 1))


class NodesNeededTest(unittest.TestCase):
    def test_sixteen_threads_fit_one_node_by_threads(self):
        self.assertEqual(core.nodes_needed(req("a", 16, 8), epyc_7551p(), cores_only=False), 1)

    def test_sixteen_vcpus_need_two_nodes_by_cores(self):
        self.assertEqual(core.nodes_needed(req("a", 16, 8), epyc_7551p(), cores_only=True), 2)

    def test_memory_can_force_more_nodes(self):
        self.assertEqual(core.nodes_needed(req("a", 4, 50), epyc_7551p(), cores_only=False), 2)

    def test_too_big_for_host(self):
        self.assertIsNone(core.nodes_needed(req("a", 65, 8), epyc_7551p(), cores_only=False))


class PlanTest(unittest.TestCase):
    def test_big_vm_spans_contiguous_nodes(self):
        p = core.plan(epyc_7551p(), [req("big", 24, 40)])["big"]
        self.assertEqual(len(p.node_ids), 2)
        self.assertEqual(p.node_ids[1], p.node_ids[0] + 1)
        self.assertEqual(p.policy, "bind")

    def test_spanning_vm_with_gpu_includes_gpu_node(self):
        p = core.plan(epyc_7551p(), [req("ia", 16, 48, pci=[3])], cores_only=True)["ia"]
        self.assertEqual(p.node_ids, (2, 3))

    def test_two_cards_prefer_window_with_both(self):
        p = core.plan(epyc_7551p(), [req("ia", 24, 8, pci=[2, 1])])["ia"]
        self.assertEqual(p.node_ids, (1, 2))

    def test_spread_and_avoid_loaded_nodes(self):
        reqs = [req("gpu", 16, 28, pci=[2])] + [req("v%d" % i, 4, 8) for i in range(3)]
        out = core.plan(epyc_7551p(), reqs)
        self.assertEqual(out["gpu"].node_ids, (2,))
        self.assertEqual(sorted(out["v%d" % i].node_ids[0] for i in range(3)), [0, 1, 3])

    def test_manual_guest_counts_in_load(self):
        out = core.plan(epyc_7551p(), [req("m", 16, 28, current=[0], manual=True), req("n", 8, 8)])
        self.assertEqual(out["m"].nodes, ())
        self.assertNotEqual(out["n"].node_ids, (0,))

    def test_kept_placement_is_stable(self):
        out = core.plan(epyc_7551p(), [req("a", 4, 4, current=[3], auto=True), req("b", 30, 4)])
        self.assertEqual(out["a"].node_ids, (3,))

    def test_rebalance_moves_kept_guest(self):
        out = core.plan(epyc_7551p(), [req("a", 4, 4, current=[3], auto=True)], rebalance=True)
        self.assertEqual(out["a"].node_ids, (0,))

    def test_overcommitted_memory_falls_back_to_preferred(self):
        out = core.plan(epyc_7551p(), [req("g", 8, 30, pci=[1]), req("h", 8, 20, pci=[1])])
        self.assertEqual(out["g"].policy, "bind")
        self.assertEqual(out["h"].policy, "preferred")


if __name__ == "__main__":
    unittest.main()
