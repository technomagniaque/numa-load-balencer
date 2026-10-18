"""Tests de pve-numa-place sur un hôte Proxmox simulé (EPYC 7551P : 4 nœuds).

    python3 -m unittest discover -s numa/tests -v
"""
import importlib.machinery
import importlib.util
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

NUMA_DIR = Path(__file__).resolve().parent.parent


def load_script(name: str, path: Path, env: dict):
    os.environ.update(env)
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # requis par dataclass
    loader.exec_module(mod)
    return mod


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class FakeHost:
    """4 nœuds de 8 cœurs / 16 threads et 32 Go ; P40 sur nœud 2, Radeon (dri) sur nœud 3."""

    def __init__(self, root: Path):
        self.root = root
        for n in range(4):
            d = root / f"sys/devices/system/node/node{n}"
            write(d / "cpulist", f"{n*8}-{n*8+7},{32+n*8}-{32+n*8+7}\n")
            write(d / "meminfo", f"Node {n} MemTotal:       {32 * 1024 * 1024} kB\n")
        for c in range(64):  # threads c et c+32 = même cœur
            topo = root / f"sys/devices/system/cpu/cpu{c}/topology"
            write(topo / "physical_package_id", "0\n")
            write(topo / "core_id", f"{c % 32}\n")
        self.pci("0000:43:00.0", 2)
        self.pci("0000:63:00.0", 3)
        drm = root / "sys/class/drm/renderD128"
        drm.mkdir(parents=True)
        (drm / "device").symlink_to(root / "sys/bus/pci/devices/0000:63:00.0")
        write(root / "proc/driver/nvidia/gpus/0000:43:00.0/information", "Device Minor: 0\n")
        write(root / "pve/mapping/pci.cfg",
              "p40\n\tmap id=10de:1b38,node=pve1,path=0000:43:00.0\n"
              "\tmap id=10de:1b38,node=pve2,path=0000:81:00.0\n")

    def pci(self, addr: str, node: int) -> None:
        write(self.root / f"sys/bus/pci/devices/{addr}/numa_node", f"{node}\n")

    def vm(self, vmid: int, body: str) -> Path:
        p = self.root / f"pve/qemu-server/{vmid}.conf"
        write(p, body)
        return p

    def ct(self, vmid: int, body: str) -> Path:
        p = self.root / f"pve/lxc/{vmid}.conf"
        write(p, body)
        return p


def fake_qm(cmd):
    """Simule « qm set » (y compris --delete) en réécrivant la section principale."""
    assert cmd[:2] == ["qm", "set"], cmd
    path = Path(os.environ["PNP_PVE_DIR"]) / f"qemu-server/{cmd[2]}.conf"
    main, _, rest = path.read_text().partition("\n[")
    lines = main.splitlines()
    for flag, val in zip(cmd[3::2], cmd[4::2]):
        keys = val.split(",") if flag == "--delete" else [flag[2:]]
        lines = [l for l in lines if l.split(":", 1)[0] not in keys]
        if flag != "--delete":
            lines.append(f"{flag[2:]}: {val}")
    path.write_text("\n".join(lines) + "\n" + (f"\n[{rest}" if rest else ""))
    return True


class PveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.host = FakeHost(self.root)
        self.m = load_script("pnp", NUMA_DIR / "pve/pve-numa-place", dict(
            PNP_SYSFS=str(self.root / "sys"), PNP_PROC=str(self.root / "proc"),
            PNP_PVE_DIR=str(self.root / "pve"), PNP_RUN_DIR=str(self.root / "run"),
            PNP_NODENAME="pve1"))

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, **kw):
        nodes = self.m.read_topology()
        return {g.vmid: (g, p) for g, p in self.m.build_plan(nodes, self.m.load_guests(), **kw)}

    def nodes_of(self, vmid, **kw):
        return self.plan(**kw)[vmid][1].node_ids

    def apply_all(self, run=fake_qm, **kw):
        for g, p in self.plan(**kw).values():
            if p.nodes:
                self.m.apply_one(p, g, live=False, run=run)

    def test_topology_counts_physical_cores(self):
        nodes = self.m.read_topology()
        self.assertEqual([(n.id, len(n.cpus), n.cores, n.mem_mb) for n in nodes][2],
                         (2, 16, 8, 32768))

    def test_hostpci_forms_and_mapping_go_to_gpu_node(self):
        self.host.vm(100, "cores: 8\nmemory: 16384\nhostpci0: 0000:43:00.0,pcie=1\n")
        self.host.vm(101, "cores: 4\nmemory: 4096\nhostpci0: mapping=p40,pcie=1\n")
        self.host.vm(102, "cores: 2\nmemory: 2048\nhostpci0: 43:00\n")
        for vmid in (100, 101, 102):
            self.assertEqual(self.nodes_of(vmid), (2,), vmid)

    def test_lxc_gpu_devices(self):
        self.host.ct(200, "cores: 4\nmemory: 4096\ndev0: /dev/dri/renderD128,gid=104\n")
        self.host.ct(201, "cores: 4\nmemory: 4096\n"
                          "lxc.mount.entry: /dev/nvidia0 dev/nvidia0 none bind,optional,create=file\n")
        self.assertEqual(self.nodes_of(200), (3,))
        self.assertEqual(self.nodes_of(201), (2,))

    def test_sixteen_vcpu_vm_spans_two_contiguous_nodes_in_cores_mode(self):
        self.host.vm(100, "sockets: 1\ncores: 16\nmemory: 16384\nhostpci0: 0000:43:00.0\n")
        self.assertEqual(self.nodes_of(100), (2,))
        self.assertEqual(self.nodes_of(100, cores_only=True), (2, 3))

    def test_spanning_vm_gets_one_guest_numa_node_per_host_node(self):
        self.host.vm(100, "cores: 16\nmemory: current=32769\nhostpci0: 0000:43:00.0\n")
        self.apply_all(cores_only=True)
        conf = (self.root / "pve/qemu-server/100.conf").read_text()
        self.assertIn("affinity: 16-31,48-63", conf)
        self.assertIn("numa0: cpus=0-7,hostnodes=2,memory=16384,policy=bind", conf)
        self.assertIn("numa1: cpus=8-15,hostnodes=3,memory=16385,policy=bind", conf)

    def test_shrinking_back_to_one_node_deletes_stale_numa1(self):
        self.host.vm(100, "cores: 16\nmemory: 8192\nhostpci0: 0000:43:00.0\n")
        self.apply_all(cores_only=True)
        self.apply_all(cores_only=False)
        conf = (self.root / "pve/qemu-server/100.conf").read_text()
        self.assertIn("numa0: cpus=0-15,hostnodes=2,", conf)
        self.assertNotIn("numa1:", conf)

    def test_split_guest_with_fewer_vcpus_than_nodes(self):
        self.assertEqual(self.m.split_guest(1, 100, (2, 3)), [("0", "2-3", 100)])

    def test_manual_and_opt_out_untouched(self):
        self.host.vm(400, "cores: 2\nmemory: 2048\naffinity: 0-3\n")
        self.host.vm(401, "cores: 2\nmemory: 2048\ntags: numa-manual\n")
        self.assertEqual(self.nodes_of(400), ())
        self.assertEqual(self.nodes_of(401), ())

    def test_apply_qemu_is_idempotent_and_keeps_snapshots(self):
        self.host.vm(100, "cores: 8\nmemory: current=16384\nhostpci0: 0000:43:00.0\n"
                          "\n[snap1]\ncores: 2\n")
        self.apply_all()
        conf = (self.root / "pve/qemu-server/100.conf").read_text()
        self.assertIn("affinity: 16-23,48-55", conf)
        self.assertIn("numa0: cpus=0-7,hostnodes=2,memory=16384,policy=bind", conf)
        self.assertIn("tags: numa-auto", conf)
        self.assertIn("[snap1]", conf)
        calls = []
        self.apply_all(run=lambda c: calls.append(c) or fake_qm(c))
        self.assertEqual(calls, [])

    def test_apply_lxc_spanning_and_stable(self):
        path = self.host.ct(300, "cores: 24\nmemory: 2048\ntags: web\n\n[snap]\ncores: 1\n")
        self.apply_all()
        text = path.read_text()
        mems = re.search(r"cpuset.mems: (\S+)", text).group(1)
        self.assertRegex(mems, r"^\d-\d$")
        self.assertIn("tags: web;numa-auto", text)
        self.assertTrue(text.endswith("\n[snap]\ncores: 1\n"))
        self.host.vm(301, "cores: 8\nmemory: 30000\n")
        self.assertEqual(self.m.format_cpulist(self.nodes_of(300)), mems)

    def test_live_lxc_writes_cgroup(self):
        self.host.ct(300, "cores: 2\nmemory: 2048\ndev0: /dev/dri/renderD128\n")
        cg = self.root / "sys/fs/cgroup/lxc/300"
        cg.mkdir(parents=True)
        g, p = self.plan()[300]
        self.m.apply_one(p, g, live=True)
        self.assertEqual((cg / "cpuset.cpus").read_text(), "24-31,56-63")
        self.assertEqual((cg / "cpuset.mems").read_text(), "3")

    def test_live_qemu_with_vfio_does_not_migrate_memory(self):
        self.host.vm(100, "cores: 2\nmemory: 2048\nhostpci0: 0000:43:00.0\n"
                          "tags: numa-auto\naffinity: 16-23,48-55\n"
                          "numa: 1\nnuma0: cpus=0-1,hostnodes=2,memory=2048,policy=bind\n")
        write(self.root / "run/qemu-server/100.pid", "4242\n")
        (self.root / "proc/4242").mkdir(parents=True)
        calls = []
        g, p = self.plan()[100]
        self.m.apply_one(p, g, live=True, run=lambda c: calls.append(c) or True)
        self.assertEqual(calls, [["taskset", "-a", "-cp", "16-23,48-55", "4242"]])

    def test_status_reads_numa_maps(self):
        self.host.vm(100, "cores: 2\nmemory: 2048\n")
        write(self.root / "run/qemu-server/100.pid", "4242\n")
        write(self.root / "proc/4242/status", "Cpus_allowed_list:\t0-7,32-39\n")
        write(self.root / "proc/4242/numa_maps",
              "7f00 bind:0 anon=10 N0=262144 kernelpagesize_kB=4\n"
              "7f01 default anon=2 N1=2 kernelpagesize_kB=2048\n")
        guest = self.m.load_guests()[0]
        self.assertEqual(self.m.actual_state(guest), "cpus=0-7,32-39 ram[N0=1024M N1=4M]")


if __name__ == "__main__":
    unittest.main()
