"""Tests du calcul de placement (numa_core), sans aucun accès à l'hôte.

    python3 -m unittest discover -s tests -v
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


def req(key, vcpus, mem_gb, pci=(), current=(), auto=False, manual=False, elastic=False):
    return core.Request(key, vcpus, int(mem_gb * GB), tuple(pci), tuple(current), auto, manual, elastic)


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

    def test_reserved_memory_pushes_guest_off_dom0_node(self):
        reqs = [req("v", 4, 8)]
        self.assertEqual(core.plan(epyc_7551p(), reqs)["v"].node_ids, (0,))
        out = core.plan(epyc_7551p(), reqs, reserved_mb={0: 8 * GB})
        self.assertNotEqual(out["v"].node_ids, (0,))

    def test_reserved_memory_turns_bind_into_preferred(self):
        out = core.plan(epyc_7551p(), [req("g", 8, 24, pci=[0])], reserved_mb={0: 8 * GB})
        self.assertEqual(out["g"].node_ids, (0,))      # la carte décide du nœud
        self.assertEqual(out["g"].policy, "preferred")  # 8 + 24 Go > 90 % de 32 Go

    def test_no_reservation_keeps_previous_behaviour(self):
        reqs = [req("a", 4, 8), req("b", 4, 8, pci=[3])]
        self.assertEqual(core.plan(epyc_7551p(), reqs), core.plan(epyc_7551p(), reqs, reserved_mb={}))

    def test_elastic_guest_is_never_bound(self):
        p = core.plan(epyc_7551p(), [req("b", 4, 4, elastic=True)])["b"]
        self.assertEqual(p.policy, "preferred")
        self.assertIn("ballooning", p.reason)

    def test_fifty_ballooned_vms_spread_over_all_nodes(self):
        """50 VM à 2 Go garantis (128 Go de RAM) : toutes placées, réparties sur les 4 nœuds."""
        reqs = [req("vm%02d" % i, 2, 2, elastic=True) for i in range(50)]
        out = core.plan(epyc_7551p(), reqs)
        per_node = [sum(1 for p in out.values() if p.node_ids == (n,)) for n in range(4)]
        self.assertEqual(sum(per_node), 50)
        self.assertLessEqual(max(per_node) - min(per_node), 1)

    def test_overcommitted_memory_falls_back_to_preferred(self):
        out = core.plan(epyc_7551p(), [req("g", 8, 30, pci=[1]), req("h", 8, 20, pci=[1])])
        self.assertEqual(out["g"].policy, "bind")
        self.assertEqual(out["h"].policy, "preferred")



def xcp_ng_epyc():
    """Topologie réelle de xcp-ng-epyc : SMT désactivé, 8 pCPU et 32 Go par nœud."""
    return [core.Node(n, tuple(range(n * 8, n * 8 + 8)), 8, 32 * GB) for n in range(4)]


def vm(key, vcpus, mem_gb, current, usage, home=(), cooldown=False, **kw):
    return req(key, vcpus, mem_gb, current=current, auto=True, **kw)._replace(
        usage=usage, home=tuple(home), cooldown=cooldown)


class LoadBalancingTest(unittest.TestCase):
    """La grosse VM devient active : les petites VM qui partagent ses nœuds partent."""

    def test_busy_big_vm_evacuates_small_vm(self):
        out = core.plan(xcp_ng_epyc(), [vm("kdet1", 16, 16, [0, 1], 15.0),
                                        vm("xoce", 4, 2, [1], 0.5)])
        self.assertEqual(out["kdet1"].node_ids, (0, 1))
        self.assertIn(out["xoce"].node_ids[0], (2, 3))
        self.assertIn("évacuation : nœud(s) 1", out["xoce"].reason)

    def test_idle_big_vm_keeps_small_vm(self):
        out = core.plan(xcp_ng_epyc(), [vm("kdet1", 16, 16, [0, 1], 0.3),
                                        vm("xoce", 4, 2, [1], 0.5)])
        self.assertEqual(out["xoce"].node_ids, (1,))
        self.assertEqual(out["xoce"].reason, "placement conservé")

    def test_recently_moved_vm_stays(self):
        out = core.plan(xcp_ng_epyc(), [vm("kdet1", 16, 16, [0, 1], 15.0),
                                        vm("xoce", 4, 2, [1], 0.5)._replace(cooldown=True)])
        self.assertEqual(out["xoce"].node_ids, (1,))

    def test_no_move_when_every_node_is_busy(self):
        reqs = [vm("big%d" % i, 8, 8, [i], 7.5) for i in range(4)] + [vm("small", 2, 1, [1], 0.5)]
        out = core.plan(xcp_ng_epyc(), reqs)
        self.assertEqual(out["small"].node_ids, (1,))  # aucun nœud ne fait gagner 1 pCPU

    def test_hot_vm_is_an_anchor(self):
        out = core.plan(xcp_ng_epyc(), [vm("a", 8, 4, [1], 7.0), vm("b", 8, 4, [1], 6.0)])
        self.assertEqual(out["a"].node_ids, (1,))
        self.assertEqual(out["b"].node_ids, (1,))  # deux VM chaudes : on ne les bouscule pas

    def test_without_measurement_nothing_moves(self):
        out = core.plan(xcp_ng_epyc(), [req("kdet1", 16, 16, current=[0, 1], auto=True),
                                        req("xoce", 4, 2, current=[1], auto=True)])
        self.assertEqual(out["xoce"].node_ids, (1,))

    def test_evacuated_vm_returns_to_its_ram_when_load_drops(self):
        out = core.plan(xcp_ng_epyc(), [vm("kdet1", 16, 16, [0, 1], 0.3),
                                        vm("xoce", 4, 2, [2], 0.5, home=(1,))])
        self.assertEqual(out["xoce"].node_ids, (1,))
        self.assertIn("retour près de sa RAM (nœud(s) 1)", out["xoce"].reason)

    def test_evacuated_vm_stays_away_while_big_vm_is_busy(self):
        out = core.plan(xcp_ng_epyc(), [vm("kdet1", 16, 16, [0, 1], 15.0),
                                        vm("xoce", 4, 2, [2], 0.5, home=(1,))])
        self.assertEqual(out["xoce"].node_ids, (2,))

    def test_return_threshold_is_lower_than_evacuation_threshold(self):
        # 16 vCPU sur 2 nœuds : 9 pCPU -> 4,5 par nœud ; avec la petite VM 5/8 = 62 % > 60 %
        busy = core.plan(xcp_ng_epyc(), [vm("kdet1", 16, 16, [0, 1], 9.0),
                                         vm("xoce", 4, 2, [2], 0.5, home=(1,))])
        self.assertEqual(busy["xoce"].node_ids, (2,))   # ni assez chargé pour évacuer, ni assez calme pour revenir
        calm = core.plan(xcp_ng_epyc(), [vm("kdet1", 16, 16, [0, 1], 4.0),
                                         vm("xoce", 4, 2, [2], 0.5, home=(1,))])
        self.assertEqual(calm["xoce"].node_ids, (1,))

    def test_no_return_during_cooldown(self):
        out = core.plan(xcp_ng_epyc(), [vm("kdet1", 16, 16, [0, 1], 0.3),
                                        vm("xoce", 4, 2, [2], 0.5, home=(1,), cooldown=True)])
        self.assertEqual(out["xoce"].node_ids, (2,))

    def test_new_vms_avoid_busy_nodes(self):
        out = core.plan(xcp_ng_epyc(), [vm("kdet1", 16, 16, [0, 1], 15.0),
                                        req("new", 2, 1)._replace(usage=0.0)])
        self.assertIn(out["new"].node_ids[0], (2, 3))

if __name__ == "__main__":
    unittest.main()
