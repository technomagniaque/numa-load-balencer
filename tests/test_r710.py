"""Tests dedies au Dell PowerEdge R710 : 2 sockets Xeon X5650 (6 coeurs/socket,
SMT desactive, 12 coeurs, 32 Go/socket) + chipset Intel 5520 IOH unique partage.

    python3 tests/test_r710.py
"""
import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

NUMA_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(NUMA_DIR))
import numa_core as core  # noqa: E402
import numa_linux as linux  # noqa: E402

Node, Request = core.Node, core.Request
R710_MEM_MB = 32135  # 32906748 kB // 1024 mesures sur le R710 reel


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def load_pve(env: dict):
    os.environ.update(env)
    loader = importlib.machinery.SourceFileLoader("pnp_r710", str(NUMA_DIR / "pve/pve-numa-place"))
    spec = importlib.util.spec_from_loader("pnp_r710", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pnp_r710"] = mod
    loader.exec_module(mod)
    return mod


def r710_nodes(mem_mb: int = R710_MEM_MB):
    """2 sockets de 6 coeurs sans SMT, numerotation entrelacee, distances 10 / 20."""
    return [
        Node(0, (0, 2, 4, 6, 8, 10), 6, mem_mb, tuple((c,) for c in (0, 2, 4, 6, 8, 10)), (10, 20)),
        Node(1, (1, 3, 5, 7, 9, 11), 6, mem_mb, tuple((c,) for c in (1, 3, 5, 7, 9, 11)), (20, 10)),
    ]


def shared_vfio(key="101", vcpus=4, mem_mb=30720, current=(), home=(), usage=0.3, cooldown=False):
    return Request(key, vcpus, mem_mb, (), tuple(current), True, False,
                   usage=usage, cooldown=cooldown, home=tuple(home),
                   passthrough=True, pci_shared=True)


def guest(key, vcpus, mem_mb, current=(), usage=0.3, auto=True, home=(), cooldown=False):
    return Request(key, vcpus, mem_mb, (), tuple(current), auto, False,
                   usage=usage, cooldown=cooldown, home=tuple(home))


class FakeR710Host:
    """Reproduit le sysfs reel du R710 (2x X5650, SMT off, Intel 5520 IOH unique)."""

    def __init__(self, root: Path):
        self.root = root
        for n, cpus, dist in ((0, "0,2,4,6,8,10", "10 20"), (1, "1,3,5,7,9,11", "20 10")):
            d = root / f"sys/devices/system/node/node{n}"
            write(d / "cpulist", f"{cpus}\n")
            write(d / "distance", f"{dist}\n")
            write(d / "meminfo", f"Node {n} MemTotal:       32906748 kB\n")
        for c in range(12):
            topo = root / f"sys/devices/system/cpu/cpu{c}/topology"
            write(topo / "core_cpus_list", f"{c}\n")
            write(topo / "physical_package_id", f"{c % 2}\n")
            write(topo / "core_id", f"{c // 2}\n")
        # Intel 5520 IOH unique : 0000:00 porte les root ports PCIe (0x060400) et cartes ;
        # 0000:fe et 0000:ff sont les bus uncore des deux Xeon 5600 (0x060000 uniquement).
        self._pci_dev("pci0000:00", "0000:00:00.0", "0x060000", -1)
        self._pci_dev("pci0000:00", "0000:00:04.0", "0x060400", -1)
        self._pci_dev("pci0000:00", "0000:00:07.0", "0x060400", -1)
        self._pci_dev("pci0000:00", "0000:03:00.0", "0x010700", -1)  # SAS2008
        self._pci_dev("pci0000:00", "0000:06:00.6", "0x020000", -1)  # QL41112 NPAR
        self._pci_dev("pci0000:fe", "0000:fe:00.0", "0x060000", -1)
        self._pci_dev("pci0000:ff", "0000:ff:00.0", "0x060000", -1)

    def _pci_dev(self, root_bus: str, addr: str, cls: str, numa_node: int) -> None:
        write(self.root / f"sys/devices/{root_bus}/{addr}/class", f"{cls}\n")
        write(self.root / f"sys/devices/{root_bus}/{addr}/numa_node", f"{numa_node}\n")
        write(self.root / f"sys/bus/pci/devices/{addr}/numa_node", f"{numa_node}\n")

    def vm(self, vmid: int, body: str) -> Path:
        p = self.root / f"pve/qemu-server/{vmid}.conf"
        write(p, body)
        return p

    def ct(self, vmid: int, body: str) -> Path:
        p = self.root / f"pve/lxc/{vmid}.conf"
        write(p, body)
        return p


class R710CoreTest(unittest.TestCase):
    def place(self, reqs, **kw):
        return core.plan(r710_nodes(), reqs, **kw)

    def test_seuils_r710(self):
        self.assertEqual(core.MEM_FIT, 0.98)
        self.assertEqual(core.OVERLOAD, 0.80)
        self.assertAlmostEqual(core.PCI_EXTREME, 5.0 / 6.0)
        self.assertAlmostEqual(core.PCI_RETURN, 4.0 / 6.0)
        self.assertEqual(core.hops(0, 1, r710_nodes()), 1)

    def test_mem_fit_98_lie_30go_sur_un_socket_de_32go(self):
        # 30720 Mo <= 32135 * 0.98 (31492 Mo) -> policy=bind sur 1 socket
        out = self.place([shared_vfio(mem_mb=30720, current=())])
        self.assertEqual(out["101"].node_ids, (0,))
        self.assertEqual(out["101"].policy, "bind")
        # si l'hote reserve 2 Go sur ce socket, 30720 + 2048 > 31492 -> bascule sur socket 1 (bind)
        out2 = self.place([shared_vfio(mem_mb=30720, current=())], reserved_mb={0: 2048, 1: 0})
        self.assertEqual(out2["101"].node_ids, (1,))
        self.assertEqual(out2["101"].policy, "bind")
        # si les deux sockets ont 2 Go reserves, reste sur 1 socket en policy=preferred
        out3 = self.place([shared_vfio(mem_mb=30720, current=())], reserved_mb={0: 2048, 1: 2048})
        self.assertEqual(len(out3["101"].node_ids), 1)
        self.assertEqual(out3["101"].policy, "preferred")

    def test_pci_extreme_5_sur_6_sur_ioh_partage(self):
        for usage, reste in ((4.0, True), (4.9, True), (5.0, False), (5.9, False)):
            out = self.place([shared_vfio(current=(0,)), guest("102", 6, 4096, current=(0,), usage=usage)])
            if reste:
                self.assertEqual(out["101"].node_ids, (0,), f"{usage}/6 coeurs : reste sur socket 0")
            else:
                self.assertEqual(out["101"].node_ids, (1,), f"{usage}/6 coeurs : repli sur socket 1")

    def test_hysteresis_4_sur_6_a_5_sur_6_avec_home(self):
        # Evacuee sur socket 1 alors que sa RAM VFIO est sur home=(0,)
        for usage in (4.0, 4.9):  # 66,7 % et 81,7 % : reste sur socket 1
            out = self.place([shared_vfio(current=(1,), home=(0,)),
                              guest("102", 6, 4096, current=(0,), usage=usage)])
            self.assertEqual(out["101"].node_ids, (1,), f"{usage}/6 : pas de retour avant 4/6")
        # Sous 4/6 (3,9 coeurs = 65 %) -> retour sur socket 0 (sa RAM VFIO)
        out = self.place([shared_vfio(current=(1,), home=(0,)),
                          guest("102", 6, 4096, current=(0,), usage=3.9)])
        self.assertEqual(out["101"].node_ids, (0,), out["101"].reason)
        self.assertIn("IOH partagé", out["101"].reason)

    def test_ne_provoque_pas_sa_propre_expulsion_sur_6_coeurs(self):
        out = self.place([shared_vfio(vcpus=6, current=(0,), usage=6.0)])
        self.assertEqual(out["101"].node_ids, (0,))

    def test_deux_sockets_satures_reste_sur_son_socket(self):
        out = self.place([
            shared_vfio(current=(0,)),
            guest("a", 6, 4096, current=(0,), usage=5.5),
            guest("b", 6, 4096, current=(1,), usage=5.5),
        ])
        self.assertEqual(out["101"].node_ids, (0,))

    def test_overload_80_evacue_des_5_coeurs_sur_6(self):
        # 4,5 + 0,5 = 5,0 / 6 = 83,3 % > 80 % -> petite VM evacuee sur socket 1
        busy = self.place([guest("big", 6, 8192, current=(0,), usage=4.5),
                           guest("small", 2, 1024, current=(0,), usage=0.5)])
        self.assertEqual(busy["small"].node_ids, (1,))
        self.assertIn("évacuation", busy["small"].reason)
        # 4,2 + 0,5 = 4,7 / 6 = 78,3 % <= 80 % -> reste sur socket 0
        calm = self.place([guest("big", 6, 8192, current=(0,), usage=4.2),
                           guest("small", 2, 1024, current=(0,), usage=0.5)])
        self.assertEqual(calm["small"].node_ids, (0,))

    def test_socket_ioh_partage_protege_par_return_load_pci_die(self):
        # Socket 0 porte la VM passthrough IOH partage -> seuil de retour 50 % (3,0 coeurs sur 6)
        # Avec 2,8 + 0,5 = 3,3 / 6 = 55 % (> 50 %, < 60 %), la VM sans carte ne revient PAS sur socket 0
        out = self.place([
            shared_vfio(current=(0,), usage=2.8),
            guest("106", 2, 1024, current=(1,), home=(0,), usage=0.5),
        ])
        self.assertEqual(out["106"].node_ids, (1,))
        # Avec 2,0 + 0,5 = 2,5 / 6 = 41,7 % <= 50 % -> retour autorise sur socket 0
        out2 = self.place([
            shared_vfio(current=(0,), usage=2.0),
            guest("106", 2, 1024, current=(1,), home=(0,), usage=0.5),
        ])
        self.assertEqual(out2["106"].node_ids, (0,))


class SharedIohDetectionTest(unittest.TestCase):
    def test_detects_r710_intel_5520_single_ioh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            FakeR710Host(root)
            self.assertTrue(linux.shared_pci_ioh(str(root / "sys")))

    def test_false_on_multi_root_complex_even_if_acpi_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            FakeR710Host(root)
            # Ajoute un 2e complexe racine PCIe (ex. 2e socket Xeon E5 / EPYC) a numa_node=-1
            write(root / "sys/devices/pci0000:80/0000:80:01.0/class", "0x060400\n")
            write(root / "sys/bus/pci/devices/0000:80:01.0/numa_node", "-1\n")
            self.assertFalse(linux.shared_pci_ioh(str(root / "sys")))

    def test_false_when_any_device_has_known_numa_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            FakeR710Host(root)
            write(root / "sys/bus/pci/devices/0000:06:00.6/numa_node", "1\n")
            self.assertFalse(linux.shared_pci_ioh(str(root / "sys")))

    def test_false_on_empty_sysfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(linux.shared_pci_ioh(tmp))


class PveR710EndToEndTest(unittest.TestCase):
    def test_r710_real_workload_places_vm101_and_balances_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host = FakeR710Host(root)
            host.vm(101, "sockets: 1\ncores: 4\nmemory: 30720\nnuma: 1\n"
                         "hostpci0: 0000:06:00.6,pcie=1\nhostpci1: 0000:03:00.0,pcie=1\n")
            host.vm(102, "sockets: 1\ncores: 4\nmemory: 16384\nnuma: 1\n")
            host.ct(100, "cores: 6\nmemory: 16384\n")
            host.ct(106, "cores: 2\nmemory: 1024\n")
            m = load_pve(dict(
                PNP_SYSFS=str(root / "sys"), PNP_PROC=str(root / "proc"),
                PNP_PVE_DIR=str(root / "pve"), PNP_RUN_DIR=str(root / "run"),
                PNP_NODENAME="pve",
            ))
            nodes = m.read_topology()
            self.assertEqual([(n.id, n.cores, len(n.cpus), n.distances) for n in nodes],
                             [(0, 6, 6, (10, 20)), (1, 6, 6, (20, 10))])
            plan = {g.vmid: (g, p) for g, p in m.build_plan(nodes, m.load_guests(), spread=True)}
            # VM 101 (passthrough sur IOH partage) est placee sur socket 0 (ancre), PAS ignoree
            self.assertEqual(plan[101][1].node_ids, (0,))
            self.assertEqual(plan[101][1].cpus, (0, 2, 4, 6, 8, 10))
            self.assertEqual(plan[101][1].policy, "bind")
            self.assertIn("IOH partagé", plan[101][1].reason)
            # CT 100 et VM 102 vont sur socket 1 (socket 0 deja a 30/32 Go),
            # puis CT 106 (2 vCPU, 1 Go) equilibre sur socket 0 (6 vCPU vs 10 vCPU sur socket 1)
            self.assertEqual(plan[100][1].node_ids, (1,))
            self.assertEqual(plan[102][1].node_ids, (1,))
            self.assertEqual(plan[106][1].node_ids, (0,))


if __name__ == "__main__":
    unittest.main()
