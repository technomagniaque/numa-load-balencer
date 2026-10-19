"""Tests de kvm-numa-place (KVM + libvirt, virt-manager) sur un hôte simulé de type
Dell R710 : 2 Xeon Westmere 6 cœurs SMT2, numérotation entrelacée des sockets.

    python3 -m unittest discover -s tests -v
"""
import io
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_pve import load_script, write  # noqa: E402

NUMA_DIR = HERE.parent
NS = "{urn:numa-place:1}"


def r710_sysfs(root: Path) -> None:
    """Nœud 0 = CPU pairs, nœud 1 = impairs ; le frère SMT de c est c ± 12."""
    for n in range(2):
        cpus = list(range(n, 24, 2))
        d = root / "sys/devices/system/node/node{}".format(n)
        write(d / "cpulist", ",".join(map(str, cpus)) + "\n")
        write(d / "meminfo", "Node {0} MemTotal: {1} kB\nNode {0} MemFree: {2} kB\n"
                             "Node {0} FilePages: 0 kB\n".format(n, 48 * 2**20, 40 * 2**20))
    for c in range(24):
        base = c % 12
        write(root / "sys/devices/system/cpu/cpu{}/topology/thread_siblings_list".format(c),
              "{},{}\n".format(base, base + 12))
    write(root / "sys/bus/pci/devices/0000:05:00.0/numa_node", "1\n")   # GPU sur le socket 1


def domain(name, vcpus, mem_gb=4, extra="", current_gb=None):
    cur = "<currentMemory unit='GiB'>{}</currentMemory>".format(current_gb) if current_gb else ""
    return ("<domain type='kvm'><name>{}</name><memory unit='GiB'>{}</memory>{}"
            "<vcpu placement='static'>{}</vcpu>{}<devices/></domain>").format(name, mem_gb, cur, vcpus, extra)


GPU = ("<domain type='kvm'><name>gpu</name><memory unit='GiB'>8</memory>"
       "<vcpu placement='static'>4</vcpu><devices><hostdev mode='subsystem' type='pci' managed='yes'>"
       "<source><address domain='0x0000' bus='0x05' slot='0x00' function='0x0'/></source>"
       "</hostdev></devices></domain>")


class FakeVirsh:
    def __init__(self, defs, running=()):
        self.defs = dict(defs)
        self.running = {name: str(i + 1) for i, name in enumerate(running)}
        self.calls = []
        self.cpu = None

    def __call__(self, cmd):
        assert cmd[:4] == ["virsh", "-q", "-c", "qemu:///system"], cmd
        args = cmd[4:]
        self.calls.append(args)
        if args[0] == "list":
            return "\n".join(self.defs) + "\n"
        if args[0] == "dumpxml":
            return self.defs[args[-1]]
        if args[0] == "domid":
            return self.running.get(args[1], "-") + "\n"
        if args[0] == "define":
            text = Path(args[1]).read_text()
            self.defs[ET.fromstring(text).findtext("name")] = text
            return ""
        if args[0] == "domstats":
            times = next(self.cpu) if self.cpu else {}
            return "".join("Domain: '{}'\n  cpu.time={}\n\n".format(n, int(s * 1e9))
                           for n, s in times.items())
        if args[0] in ("vcpupin", "emulatorpin", "numatune"):
            return ""
        return None


class KvmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        r710_sysfs(self.root)
        self.m = load_script("knp", NUMA_DIR / "kvm/kvm-numa-place", {})
        self.m.SYSFS = str(self.root / "sys")
        self.m.PROC = str(self.root / "proc")
        self.m.PIDS = str(self.root / "run")
        self.m.STATE = str(self.root / "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, virsh, **kw):
        nodes = self.m.linux.read_topology(self.m.SYSFS)
        return {vm.name: (vm, p) for vm, p in self.m.build_plan(nodes, self.m.load_vms(virsh), **kw)}

    def test_topology_r710(self):
        nodes = self.m.linux.read_topology(self.m.SYSFS)
        self.assertEqual([(n.id, len(n.cpus), n.cores) for n in nodes], [(0, 12, 6), (1, 12, 6)])
        self.assertEqual(nodes[0].threads[0], (0, 12))

    def test_gpu_vm_goes_to_its_socket_with_one_thread_per_core(self):
        v = FakeVirsh({"gpu": GPU})
        vm, p = self.plan(v, spread=True)["gpu"]
        self.assertEqual(p.node_ids, (1,))
        self.assertEqual(len({c % 12 for c in p.cpus}), len(p.cpus))   # jamais deux frères
        self.assertEqual(self.m.wanted_numatune(p), ("strict", "1"))

    def test_ballooning_vm_uses_preferred_numatune(self):
        v = FakeVirsh({"web": domain("web", 2, 8, current_gb=2)})
        vm, p = self.plan(v)["web"]
        self.assertEqual(vm.guaranteed_mb, 2048)
        self.assertEqual(self.m.wanted_numatune(p)[0], "preferred")

    def test_hand_made_vcpupin_is_left_alone(self):
        pinned = domain("db", 2, extra="<cputune><vcpupin vcpu='0' cpuset='3'/></cputune>")
        vm, p = self.plan(FakeVirsh({"db": pinned}))["db"]
        self.assertEqual(p.nodes, ())
        self.assertIn("manuel", p.reason)

    def test_apply_writes_cpuset_numatune_marker_and_is_idempotent(self):
        v = FakeVirsh({"gpu": GPU, "web": domain("web", 2)})
        for vm, p in self.plan(v, spread=True).values():
            self.assertTrue(self.m.apply_one(vm, p, live=False, run=v))
        root = ET.fromstring(v.defs["gpu"])
        self.assertEqual(root.find("./numatune/memory").attrib, {"mode": "strict", "nodeset": "1"})
        self.assertEqual(root.find("./metadata/{}place".format(NS)).get("mode"), "auto")
        self.assertEqual(len(root.find("vcpu").get("cpuset").split(",")), 6)  # 6 cœurs, 1 thread chacun
        v.calls.clear()
        for vm, p in self.plan(v, spread=True).values():
            self.m.apply_one(vm, p, live=False, run=v)
        self.assertFalse([c for c in v.calls if c[0] == "define"])

    def test_live_pins_every_vcpu_and_moves_ram_only_without_vfio(self):
        v = FakeVirsh({"web": domain("web", 2, extra="<numatune><memory mode='strict' nodeset='0'/></numatune>"
                                                     "<metadata><np:place xmlns:np='urn:numa-place:1' mode='auto'/>"
                                                     "</metadata>"),
                       "gpu": GPU}, running=("web", "gpu"))
        for vm, p in self.plan(v, spread=True).values():
            self.m.apply_one(vm, p, live=True, run=v)
        pins = [c for c in v.calls if c[0] == "vcpupin"]
        self.assertEqual(len(pins), 2 + 4)                       # un par vCPU
        self.assertTrue(all("--live" in c for c in pins))
        moved = [c for c in v.calls if c[0] == "numatune"]
        self.assertEqual([c[1] for c in moved], ["web"])         # pas la VM avec GPU (VFIO)

    def test_domstats_and_main_measure_usage(self):
        v = FakeVirsh({"web": domain("web", 4)}, running=("web",))
        v.cpu = iter([{"web": 100.0}, {"web": 110.0}])            # 2 pCPU pendant 5 s
        with mock.patch("time.sleep"), mock.patch("time.monotonic", side_effect=[0.0, 5.0]), \
                redirect_stderr(io.StringIO()):
            rc = self.m.main(["apply", "--sample", "5"], run=v)
        self.assertEqual(rc, 0)
        state = self.m.st.load(self.m.STATE)
        self.assertEqual(state["web"]["ema"], 2.0)
        self.assertIn("__host__", state)
        self.assertEqual(self.m.parse_domstats("Domain: 'a b'\n  cpu.time=1500000000\n"), {"a b": 1.5})


if __name__ == "__main__":
    unittest.main()
