"""Une VM avec passthrough PCI n'est jamais « à ballooning » : toute sa RAM compte.

Sous Xen, pas de populate-on-demand avec une carte passée ; sous KVM, VFIO épingle
toute la RAM pour le DMA. Le plancher du ballooning ne libère donc rien sur le nœud.

    python3 -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

NUMA_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(NUMA_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import numa_libvirt as lv  # noqa: E402
from test_pve import load_script  # noqa: E402

GPU = ("0000:61:00.0",)


class XcpngVmTest(unittest.TestCase):
    def setUp(self):
        m = load_script("xnp_pt", NUMA_DIR / "xcp-ng/xcpng-numa-place", {})
        self.vm = lambda pci: m.Vm("u", "rocky97p40", 8, 32768, pci, (), (), None, 16384)

    def test_passthrough_vm_counts_its_full_memory(self):
        vm = self.vm(GPU)
        self.assertFalse(vm.elastic)
        self.assertEqual(vm.guaranteed_mb, 32768)

    def test_vm_without_card_keeps_ballooning(self):
        vm = self.vm(())
        self.assertTrue(vm.elastic)
        self.assertEqual(vm.guaranteed_mb, 16384)


class LibvirtDomainTest(unittest.TestCase):
    def dom(self, pci):
        return lv.Domain("ia", "<domain/>", 8, 32768, pci, (), "auto", True, 16384)

    def test_passthrough_domain_counts_its_full_memory(self):
        self.assertEqual((self.dom(GPU).elastic, self.dom(GPU).guaranteed_mb), (False, 32768))

    def test_domain_without_card_keeps_ballooning(self):
        self.assertEqual((self.dom(()).elastic, self.dom(()).guaranteed_mb), (True, 16384))


class PveGuestTest(unittest.TestCase):
    def setUp(self):
        self.m = load_script("pnp_pt", NUMA_DIR / "pve/pve-numa-place", {})

    def guest(self, kind, conf, pci=()):
        return self.m.Guest(kind, 101, "/x", (), (), conf, 8, 32768, pci, (), 16384)

    def test_kvm_with_hostpci_counts_its_full_memory(self):
        g = self.guest("qemu", {"hostpci0": ["mapping=p40"]})
        self.assertEqual((g.elastic, g.guaranteed_mb), (False, 32768))

    def test_kvm_without_hostpci_keeps_ballooning(self):
        g = self.guest("qemu", {"balloon": ["16384"]})
        self.assertEqual((g.elastic, g.guaranteed_mb), (True, 16384))

    def test_lxc_with_gpu_device_node_is_not_vfio(self):
        g = self.guest("lxc", {"dev0": ["/dev/nvidia0"]})
        self.assertFalse(g.vfio)


if __name__ == "__main__":
    unittest.main()
