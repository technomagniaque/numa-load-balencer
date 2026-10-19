"""Voies SMT : jamais deux threads d'un même cœur dans une VM, quelle que soit la topologie.

    python3 -m unittest discover -s tests -v
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import numa_core as core  # noqa: E402
import numa_xen as xen  # noqa: E402

GB = 1024


def node(nid, cores, mem_gb=32):
    """Nœud à partir de la liste des cœurs (chaque cœur = tuple de ses threads)."""
    threads = tuple(tuple(sorted(c)) for c in cores)
    cpus = tuple(sorted(t for c in threads for t in c))
    return core.Node(nid, cpus, len(threads), mem_gb * GB, threads)


def r710():
    """Dell R710, 2 Xeon Westmere 6 cœurs SMT2. Linux entrelace les sockets :
    nœud 0 = CPU pairs, nœud 1 = impairs ; le frère de c est c + 12."""
    return [node(n, [(c, c + 12) for c in range(n, 12, 2)], 48) for n in range(2)]


def xeon_phi():
    """Xeon Phi (KNL) en mode quadrant, SMT4 : 8 cœurs ici (64-72 en vrai)."""
    return [node(0, [(c, c + 8, c + 16, c + 24) for c in range(8)], 96)]


def smt8():
    """2 nœuds de 4 cœurs SMT8 (type POWER, ou SMT8 générique)."""
    return [node(n, [tuple(n * 32 + c + 4 * j for j in range(8)) for c in range(4)]) for n in range(2)]


def hybrid():
    """Hybride : 4 cœurs P à 2 threads + 4 cœurs E à 1 thread (Alder Lake, big.LITTLE…)."""
    return [node(0, [(c, c + 1) for c in range(0, 8, 2)] + [(c,) for c in range(8, 12)])]


def arm_no_smt():
    """ARM (Ampere, Graviton…) : pas de SMT, 1 thread par cœur."""
    return [node(n, [(c,) for c in range(n * 16, n * 16 + 16)]) for n in range(2)]


def req(key, vcpus, mem_gb=2, **kw):
    return core.Request(key, vcpus, int(mem_gb * GB), (), (), False, False, **kw)


def assert_one_thread_per_core(test, nodes, placements):
    core_of = {t: i for i, c in enumerate(c for n in nodes for c in n.threads) for t in c}
    for p in placements.values():
        if not p.nodes:
            continue
        used = [core_of.get(c, c) for c in p.cpus]
        test.assertEqual(len(used), len(set(used)), "{} a deux threads d'un même cœur".format(p.key))


class LanesTest(unittest.TestCase):
    def test_lanes_smt4(self):
        lanes = core.lanes(xeon_phi()[0])
        self.assertEqual(len(lanes), 4)
        self.assertEqual(lanes[1], tuple(range(8, 16)))

    def test_lanes_hybrid_are_uneven(self):
        lanes = core.lanes(hybrid()[0])
        self.assertEqual(lanes[0], (0, 2, 4, 6, 8, 9, 10, 11))  # tous les cœurs
        self.assertEqual(lanes[1], (1, 3, 5, 7))               # seuls les cœurs P

    def test_group_siblings_interleaved_numbering(self):
        sib = {c: (c % 12, c % 12 + 12) for c in range(24)}
        self.assertEqual(core.group_siblings(range(0, 24, 2), sib)[:2], ((0, 12), (2, 14)))

    def test_lane_of_round_trip(self):
        nodes = r710()
        self.assertEqual(core.lane_of(core.lane_cpus(nodes[1], 1), nodes), 1)
        self.assertEqual(core.lane_of(nodes[1].cpus, nodes), -1)        # nœud entier
        self.assertEqual(core.lane_of(range(16), arm_no_smt()), -1)    # pas de SMT


class SpreadTest(unittest.TestCase):
    def test_r710_two_vms_never_share_a_core_within_themselves(self):
        out = core.plan(r710(), [req("a", 6), req("b", 6)], spread=True)
        assert_one_thread_per_core(self, r710(), out)
        self.assertEqual(len(out["a"].cpus), 6)

    def test_r710_vm_bigger_than_a_socket_spans_both_nodes(self):
        out = core.plan(r710(), [req("big", 8)], spread=True)
        self.assertEqual(out["big"].node_ids, (0, 1))
        assert_one_thread_per_core(self, r710(), out)

    def test_r710_vm_bigger_than_all_cores_is_refused(self):
        out = core.plan(r710(), [req("huge", 13)], spread=True)  # 12 cœurs physiques
        self.assertEqual(out["huge"].nodes, ())
        self.assertIn("cœurs", out["huge"].reason)

    def test_smt_share_keeps_whole_node(self):
        out = core.plan(r710(), [req("a", 6)])
        self.assertEqual(out["a"].lane, -1)
        self.assertEqual(len(out["a"].cpus), 12)

    def test_xeon_phi_four_vms_take_the_four_lanes(self):
        out = core.plan(xeon_phi(), [req("vm%d" % i, 4) for i in range(4)], spread=True)
        self.assertEqual(sorted(p.lane for p in out.values()), [0, 1, 2, 3])
        assert_one_thread_per_core(self, xeon_phi(), out)

    def test_smt8_lanes(self):
        out = core.plan(smt8(), [req("vm%d" % i, 2) for i in range(16)], spread=True)
        assert_one_thread_per_core(self, smt8(), out)
        self.assertEqual(len({(p.node_ids, p.lane) for p in out.values()}), 16)  # toutes distinctes

    def test_hybrid_wide_vm_gets_the_lane_with_all_cores(self):
        out = core.plan(hybrid(), [req("wide", 6)], spread=True)
        self.assertEqual(out["wide"].lane, 0)
        self.assertEqual(len(out["wide"].cpus), 8)

    def test_arm_without_smt_behaves_like_whole_nodes(self):
        out = core.plan(arm_no_smt(), [req("a", 4)], spread=True)
        self.assertEqual(out["a"].lane, -1)
        self.assertEqual(out["a"].cpus, arm_no_smt()[0].cpus)

    def test_kept_vm_keeps_its_lane(self):
        lane1 = req("a", 4)._replace(current=(0,), auto=True, lane=1, usage=1.0)
        out = core.plan(r710(), [lane1, req("b", 4)._replace(usage=3.0)], spread=True)
        self.assertEqual((out["a"].node_ids, out["a"].lane), ((0,), 1))

    def test_whole_node_vm_is_moved_onto_a_lane(self):
        legacy = req("a", 4)._replace(current=(0,), auto=True, lane=-1)
        out = core.plan(r710(), [legacy], spread=True)
        self.assertEqual(out["a"].node_ids, (0,))
        self.assertGreaterEqual(out["a"].lane, 0)

    def test_property_random_hosts_and_vms(self):
        rng = random.Random(7)
        for topo in (r710, xeon_phi, smt8, hybrid, arm_no_smt):
            for _ in range(60):
                reqs = [req("vm%d" % i, rng.randint(1, 8), rng.randint(1, 8))
                        ._replace(usage=rng.random() * 4) for i in range(rng.randint(1, 20))]
                assert_one_thread_per_core(self, topo(), core.plan(topo(), reqs, spread=True))


XL_WESTMERE = """host                   : r710
cpu_topology           :
cpu:    core    socket     node
""" + "".join("{:3d}:   {:5d}   {:6d}   {:6d}\n".format(c, (c % 12) // 2, c % 2, c % 2) for c in range(24)) + """device topology        :
device           node
numa_info              :
node:    memsize    memfree    distances
   0:     49152      40000      10,20
   1:     49152      40000      20,10
"""


class XenTopologyTest(unittest.TestCase):
    def test_xl_info_groups_smt_siblings(self):
        nodes, _ = xen.parse_xl_info(XL_WESTMERE)
        self.assertEqual([(n.id, len(n.cpus), n.cores, core.smt_width(n)) for n in nodes],
                         [(0, 12, 6, 2), (1, 12, 6, 2)])
        self.assertEqual(nodes[0].threads[0], (0, 12))


if __name__ == "__main__":
    unittest.main()
