"""Tests des versions Xen : numa_xen (xl), xcpng-numa-place (xe) et xen-numa-place (virsh).

    python3 -m unittest discover -s numa/tests -v
"""
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

NUMA_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(NUMA_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import numa_xen as xen  # noqa: E402
from test_pve import load_script  # noqa: E402


def xl_info_n() -> str:
    """Sortie de « xl info -n » d'un EPYC 7551P (4 nœuds, 8c/16t, 32 Go) sous Xen."""
    lines = ["host                   : xcp-ng-epyc", "nr_nodes               : 4",
             "cpu_topology           :", "cpu:    core    socket     node"]
    for c in range(64):
        node = (c % 32) // 8
        lines.append("{:3d}:   {:5d}   {:6d}   {:6d}".format(c, c % 32, 0, node))
    lines += ["device topology        :", "device           node",
              "0000:00:00.0      0", "0000:22:00.4      1", "0000:43:00.0      2",
              "0000:63:00.0      3", "0000:64:00.0      -", "0000:41:00.0      2",
              "numa_info              :", "node:    memsize    memfree    distances"]
    for n in range(4):
        lines.append("   {}:     32768      30000      10,16,16,16".format(n))
    lines.append("xen_major              : 4")
    return "\n".join(lines) + "\n"


DMESG = """(XEN) 'u' pressed -> dumping numa info (now = 1)
(XEN) Memory location of each domain:
(XEN) d0 (total: 2097152):
(XEN)     Node 0: 2097152
(XEN) d5 (total: 4194304):
(XEN)     Node 2: 4194304
(XEN)     Node 3: 0
"""


def make_vf(sysfs: Path, vf: str, pf: str = "0000:41:00.0") -> None:
    """Crée /sys/bus/pci/devices/<vf>/physfn -> <pf>, comme le noyau pour une VF SR-IOV."""
    pf_dir = sysfs / "devices/pci0000:40/0000:40:03.1" / pf
    pf_dir.mkdir(parents=True, exist_ok=True)
    vf_dir = sysfs / "bus/pci/devices" / vf
    vf_dir.mkdir(parents=True)
    (vf_dir / "physfn").symlink_to(pf_dir)


class FakeXl:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd == ["xl", "info", "-n"]:
            return xl_info_n()
        if cmd == ["xl", "dmesg"]:
            return DMESG
        if cmd[:2] in (["xl", "debug-keys"], ["xl", "vcpu-pin"]):
            return ""
        return None


class NumaXenTest(unittest.TestCase):
    def test_parse_topology_and_pci(self):
        nodes, pci = xen.read_topology(FakeXl())
        self.assertEqual([(n.id, len(n.cpus), n.cores, n.mem_mb) for n in nodes],
                         [(i, 16, 8, 32768) for i in range(4)])
        self.assertEqual(nodes[2].cpus[:2], (16, 17))
        self.assertEqual(pci["0000:43:00.0"], 2)
        self.assertNotIn("0000:64:00.0", pci)

    def test_domain_memory_from_debug_keys(self):
        self.assertEqual(xen.domain_memory(FakeXl())[5], {2: 16384, 3: 0})

    def test_real_xcp_ng_epyc_output(self):
        """Sortie réelle de xcp-ng-epyc (XCP-ng 8.3, Xen 4.17, 7551P, SMT désactivé)."""
        text = (Path(__file__).parent / "fixtures/xl-info-n-xcp-ng-epyc.txt").read_text()
        nodes, pci = xen.parse_xl_info(text)
        self.assertEqual([(n.id, len(n.cpus), n.cores) for n in nodes], [(i, 8, 8) for i in range(4)])
        self.assertEqual([n.mem_mb for n in nodes], [33536, 32768, 32768, 32767])
        self.assertEqual(nodes[1].cpus, tuple(range(8, 16)))
        self.assertEqual(pci["0000:61:00.0"], 3)  # Tesla P40
        self.assertEqual(len(pci), 118)

    def test_pci_node_falls_back_to_pf_for_vf(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_vf(Path(tmp), "0000:41:01.0")
            pci = {"0000:41:00.0": 2, "0000:43:00.0": 3}
            self.assertEqual(xen.pci_node("0000:43:00.0", pci, tmp), 3)
            self.assertEqual(xen.pci_node("0000:41:01.0", pci, tmp), 2)
            self.assertIsNone(xen.pci_node("0000:99:00.0", pci, tmp))

    def test_old_domain_header_format(self):
        text = "Memory location of each domain:\n(XEN) Domain 7 (total: 256):\n(XEN)     Node 1: 256\n"
        self.assertEqual(xen.parse_domain_memory(text), {7: {1: 1}})


class FakeXe(FakeXl):
    """Hôte XCP-ng seul : VM ia (P40), web, manuelle, grosse."""

    def __init__(self):
        super().__init__()
        self.vms = {
            "u-ia": {"name-label": "kocky97p40", "VCPUs-max": "16", "memory-static-max": str(24 * 2**30),
                     "other-config": "pci: 0/0000:43:00.0; mac_seed: x", "tags": "", "VCPUs-params": "",
                     "dom-id": "5"},
            "u-web": {"name-label": "debian13kdet1", "VCPUs-max": "4", "memory-static-max": str(4 * 2**30),
                      "other-config": "", "tags": "", "VCPUs-params": "", "dom-id": "-1"},
            "u-man": {"name-label": "manuelle", "VCPUs-max": "2", "memory-static-max": str(2**30),
                      "other-config": "", "tags": "", "VCPUs-params": "mask: 0,1", "dom-id": "-1"},
            "u-vf": {"name-label": "debian13vf", "VCPUs-max": "2", "memory-static-max": str(2**30),
                     "other-config": "pci: 0/0000:41:01.2", "tags": "", "VCPUs-params": "",
                     "dom-id": "-1"},
            "u-big": {"name-label": "grosse", "VCPUs-max": "24", "memory-static-max": str(8 * 2**30),
                      "other-config": "", "tags": "", "VCPUs-params": "", "dom-id": "-1"},
        }

    def __call__(self, cmd):
        out = super().__call__(cmd)
        if out is not None or cmd[0] != "xe":
            return out
        self.calls.append(cmd)
        if cmd[1] == "vm-list":
            return ",".join(self.vms)
        if cmd[1] == "host-list":
            return "h1"
        args = dict(a.split("=", 1) for a in cmd[2:] if "=" in a)
        vm = self.vms[args["uuid"]]
        if cmd[1] == "vm-param-get":
            return vm.get(args["param-name"], "") + "\n"
        if cmd[1] == "vm-param-set":
            vm["VCPUs-params"] = "mask: " + args["VCPUs-params:mask"]
            return ""
        if cmd[1] == "vm-param-add":
            vm["tags"] = ", ".join(filter(None, [vm["tags"], args["param-key"]]))
            return ""
        return None


class XcpngTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        make_vf(Path(self.tmp.name), "0000:41:01.2")
        self.m = load_script("xnp", NUMA_DIR / "xcp-ng/xcpng-numa-place", {})
        self.m.SYSFS = self.tmp.name
        self.xe = FakeXe()

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, **kw):
        nodes, pci = xen.read_topology(self.xe)
        return {vm.name: (vm, p) for vm, p in self.m.build_plan(nodes, pci, self.m.load_vms(self.xe), **kw)}

    def test_plan(self):
        p = self.plan(cores_only=True)
        self.assertEqual(p["kocky97p40"][1].node_ids, (2, 3))
        self.assertEqual(p["grosse"][1].node_ids, (0, 1, 2))  # 24 vCPU / 8 cœurs
        self.assertEqual(p["manuelle"][1].nodes, ())
        self.assertEqual(self.plan()["grosse"][1].node_ids, (0, 1))  # 24 vCPU / 16 threads
        self.assertEqual(len(p["debian13kdet1"][1].node_ids), 1)
        self.assertEqual(p["debian13vf"][1].node_ids, (2,))  # VF -> physfn -> PF0, nœud 2

    def test_apply_sets_mask_tag_and_live_pin_then_idempotent(self):
        for vm, p in self.plan().values():
            if p.nodes:
                self.assertTrue(self.m.apply_one(vm, p, live=True, run=self.xe))
        ia = self.xe.vms["u-ia"]
        self.assertEqual(ia["VCPUs-params"], "mask: " + ",".join(map(str, list(range(16, 24)) + list(range(48, 56)))))
        self.assertEqual(ia["tags"], "numa-auto")
        self.assertEqual(self.m.parse_tags(", XOA, 2025.12"), ("XOA", "2025.12"))
        self.assertIn(["xl", "vcpu-pin", "5", "all", "16-23,48-55"], self.xe.calls)
        self.xe.calls.clear()
        for vm, p in self.plan().values():
            if p.nodes:
                self.m.apply_one(vm, p, live=False, run=self.xe)
        self.assertFalse([c for c in self.xe.calls if c[1] in ("vm-param-set", "vm-param-add")])

    def test_status_shows_memory_per_node(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.m.main(["status", "kocky97p40"], run=self.xe)
        self.assertIn("ram[N2=16384M]", buf.getvalue())


DOMAIN_IA = """<domain type='xen'>
  <name>ia</name>
  <memory unit='KiB'>25165824</memory>
  <vcpu placement='static'>16</vcpu>
  <metadata><lo:libosinfo xmlns:lo="http://libosinfo.org/xmlns/libvirt/domain/1.0"/></metadata>
  <devices>
    <hostdev mode='subsystem' type='pci' managed='yes'>
      <source><address domain='0x0000' bus='0x43' slot='0x00' function='0x0'/></source>
    </hostdev>
  </devices>
</domain>"""

DOMAIN_WEB = """<domain type='xen'><name>web</name><memory unit='GiB'>4</memory>
<vcpu>2</vcpu><devices><interface type='network'><source network='chelsio-vf'/></interface></devices></domain>"""

DOMAIN_MAN = """<domain type='xen'><name>man</name><memory unit='MiB'>1024</memory>
<vcpu cpuset='0-1'>2</vcpu></domain>"""


DOMAIN_DB = """<domain type='xen'><name>db</name><memory unit='GiB'>2</memory><vcpu>2</vcpu>
<devices><interface type='network'><source network='chelsio-pool'/></interface>
<interface type='network'><source network='default'/></interface></devices></domain>"""


class FakeVirsh(FakeXl):
    def __init__(self, sysfs: Path):
        super().__init__()
        self.defs = {"ia": DOMAIN_IA, "web": DOMAIN_WEB, "man": DOMAIN_MAN, "db": DOMAIN_DB}
        self.nets = {
            "chelsio-vf": "<network><forward mode='hostdev'><pf dev='enp34s0f4'/></forward></network>",
            "chelsio-pool": "<network><forward mode='hostdev' managed='yes'>"
                            "<address type='pci' domain='0x0000' bus='0x41' slot='0x01' function='0x0'/>"
                            "<address type='pci' domain='0x0000' bus='0x41' slot='0x01' function='0x1'/>"
                            "</forward></network>",
            "default": "<network><forward mode='nat'/></network>",
        }
        self.sysfs = sysfs

    def __call__(self, cmd):
        out = super().__call__(cmd)
        if out is not None or cmd[0] != "virsh":
            return out
        args = cmd[4:]
        if args[0] == "list":
            return "\n".join(self.defs) + "\n"
        if args[0] == "dumpxml":
            return self.defs[args[-1]]
        if args[0] == "domid":
            return "5\n" if args[1] == "ia" else "-\n"
        if args[0] == "net-dumpxml":
            return self.nets.get(args[1])
        if args[0] == "define":
            text = Path(args[1]).read_text()
            self.defs[ET.fromstring(text).findtext("name")] = text
            return ""
        return None


class XenLibvirtTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        sysfs = Path(self.tmp.name)
        dev = sysfs / "devices/pci0000:20/0000:22:00.4"
        dev.mkdir(parents=True)
        net = sysfs / "class/net/enp34s0f4"
        net.mkdir(parents=True)
        (net / "device").symlink_to(dev)
        make_vf(sysfs, "0000:41:01.0")
        self.m = load_script("xlp", NUMA_DIR / "xen-libvirt/xen-numa-place", {"XNP_SYSFS": str(sysfs)})
        self.m.SYSFS = str(sysfs)
        self.virsh = FakeVirsh(sysfs)

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, **kw):
        nodes, pci = xen.read_topology(self.virsh)
        return {vm.name: (vm, p) for vm, p in self.m.build_plan(nodes, pci, self.m.load_vms(self.virsh), **kw)}

    def test_plan_uses_hostdev_and_sriov_pf(self):
        p = self.plan()
        self.assertEqual(p["ia"][0].mem_mb, 24576)
        self.assertEqual(p["ia"][1].node_ids, (2,))
        self.assertEqual(p["web"][1].node_ids, (1,))
        self.assertEqual(p["man"][1].nodes, ())

    def test_vf_pool_listed_by_address_uses_pf_node(self):
        p = self.plan()
        self.assertEqual(p["db"][0].pci, ("0000:41:01.0",))  # réseau NAT ignoré
        self.assertEqual(p["db"][1].node_ids, (2,))
        self.assertIn("passthrough", p["db"][1].reason)

    def test_apply_writes_cpuset_and_marker_then_idempotent(self):
        for vm, p in self.plan(cores_only=True).values():
            if p.nodes:
                self.assertTrue(self.m.apply_one(vm, p, live=True, run=self.virsh))
        root = ET.fromstring(self.virsh.defs["ia"])
        self.assertEqual(root.find("vcpu").get("cpuset"), "16-31,48-63")
        self.assertEqual(root.find("./metadata/{urn:numa-place:1}place").get("mode"), "auto")
        self.assertIsNotNone(root.find("./metadata/{http://libosinfo.org/xmlns/libvirt/domain/1.0}libosinfo"))
        self.assertIn(["xl", "vcpu-pin", "ia", "all", "16-31,48-63"], self.virsh.calls)
        self.virsh.calls.clear()
        for vm, p in self.plan(cores_only=True).values():
            if p.nodes:
                self.m.apply_one(vm, p, live=False, run=self.virsh)
        self.assertFalse([c for c in self.virsh.calls if "define" in c])


if __name__ == "__main__":
    unittest.main()
