"""numa_libvirt — lecture et écriture des domaines libvirt, partagées par
xen-numa-place (pilote libxl) et kvm-numa-place (pilote QEMU/KVM, virt-manager).

Lit dans la définition persistante : vCPU, mémoire et ballooning (<memory> /
<currentMemory>), cartes PCI (<hostdev>, <interface type='hostdev'>, réseaux
SR-IOV <forward mode='hostdev'>), épinglage existant et marque numa-place.
Écrit : <vcpu cpuset=…>, éventuellement <numatune>, et la marque dans <metadata>.
Compatible Python 3.6.
"""
import os
import tempfile
import xml.etree.ElementTree as ET
from typing import Callable, List, NamedTuple, Optional, Tuple

import numa_core as core

NS = "urn:numa-place:1"
ET.register_namespace("np", NS)
MEM_UNITS = {"b": 1, "bytes": 1, "KiB": 2**10, "k": 2**10, "MiB": 2**20, "M": 2**20,
             "GiB": 2**30, "G": 2**30, "TiB": 2**40, "T": 2**40}

Virsh = Callable[..., Optional[str]]   # virsh("dumpxml", "vm") -> sortie, None si échec


class Domain(NamedTuple):
    name: str
    xml: str                   # définition persistante (--inactive)
    vcpus: int
    mem_mb: int
    pci: Tuple[str, ...]
    cpuset: Tuple[int, ...]    # () si absent
    mode: str                  # "auto" | "manual" | ""
    running: bool
    mem_min_mb: int = 0        # <currentMemory> (cible du ballooning) si < <memory>
    domid: str = ""            # change à chaque démarrage (RAM réallouée)
    tuned: bool = False        # <vcpupin> ou <numatune> déjà présents (épinglage existant)

    @property
    def guaranteed_mb(self) -> int:
        return self.mem_min_mb or self.mem_mb

    @property
    def elastic(self) -> bool:
        return 0 < self.mem_min_mb < self.mem_mb


def is_manual(dom: Domain) -> bool:
    """Marqué « manual », ou épinglé à la main (cpuset / vcpupin / numatune) sans notre marque."""
    return dom.mode == "manual" or ((bool(dom.cpuset) or dom.tuned) and dom.mode != "auto")


# ------------------------------------------------------------------ lecture

def pci_addr(addr: ET.Element) -> str:
    vals = [int(addr.get(k, "0"), 0) for k in ("domain", "bus", "slot", "function")]
    return "{:04x}:{:02x}:{:02x}.{:x}".format(*vals)


def pool_addr(virsh: Virsh, network: str, sysfs: str) -> Optional[str]:
    """Carte d'un réseau libvirt <forward mode='hostdev'> -> adresse PCI.

    Le pool peut lister ses VF (<address …/>, seule forme qui marche avec une
    Chelsio, dont les VF sont sous les PF 0-3) ou nommer la PF (<pf dev=…/>).
    La VM reçoit une VF quelconque du pool : la première sert de référence."""
    text = virsh("net-dumpxml", network)
    if not text:
        return None
    forward = ET.fromstring(text).find("./forward[@mode='hostdev']")
    if forward is None:
        return None
    vf = forward.find("address[@type='pci']")
    if vf is not None:
        return pci_addr(vf)
    pf = forward.find("pf")
    if pf is None or not pf.get("dev"):
        return None
    real = os.path.realpath(os.path.join(sysfs, "class/net", pf.get("dev"), "device"))
    return os.path.basename(real) if os.path.exists(real) else None


def pci_of(root: ET.Element, virsh: Virsh, sysfs: str) -> Tuple[str, ...]:
    addrs = [pci_addr(a) for a in root.findall("./devices/hostdev[@type='pci']/source/address")]
    addrs += [pci_addr(a) for a in root.findall("./devices/interface[@type='hostdev']/source/address")]
    for src in root.findall("./devices/interface[@type='network']/source"):
        card = pool_addr(virsh, src.get("network", ""), sysfs)
        if card and card not in addrs:
            addrs.append(card)
    return tuple(addrs)


def mem_bytes(elem: Optional[ET.Element]) -> int:
    if elem is None or not (elem.text or "").strip():
        return 0
    return int(elem.text) * MEM_UNITS.get(elem.get("unit", "KiB"), 2**10)


def load_domain(virsh: Virsh, name: str, sysfs: str) -> Domain:
    text = virsh("dumpxml", "--inactive", name) or "<domain/>"
    root = ET.fromstring(text)
    vcpu = root.find("vcpu")
    total = mem_bytes(root.find("memory"))
    current = mem_bytes(root.find("currentMemory"))
    mark = root.find("./metadata/{%s}place" % NS)
    domid = (virsh("domid", name) or "").strip()
    tuned = root.find("./cputune/vcpupin") is not None or root.find("numatune") is not None
    return Domain(
        name=name, xml=text,
        vcpus=int(vcpu.text) if vcpu is not None else 1,
        mem_mb=total // 2**20,
        pci=pci_of(root, virsh, sysfs),
        cpuset=core.parse_cpulist(vcpu.get("cpuset")) if vcpu is not None and vcpu.get("cpuset") else (),
        mode=mark.get("mode", "") if mark is not None else "",
        running=domid.isdigit(),
        mem_min_mb=current // 2**20 if 0 < current < total else 0,
        domid=domid if domid.isdigit() else "",
        tuned=tuned,
    )


def load_domains(virsh: Virsh, sysfs: str, exclude: Tuple[str, ...] = ("Domain-0",)) -> List[Domain]:
    names = (virsh("list", "--all", "--name") or "").split()
    return [load_domain(virsh, n, sysfs) for n in names if n not in exclude]


# ------------------------------------------------------------------ écriture

def placed_xml(dom: Domain, cpus: Tuple[int, ...],
               numatune: Optional[Tuple[str, str]] = None) -> str:
    """Définition avec <vcpu cpuset>, la marque « auto » et, pour KVM, <numatune>.

    numatune = (mode, nodeset), ex. ("strict", "2") ; mode « preferred » pour un
    invité à ballooning, qui doit pouvoir déborder plutôt qu'échouer."""
    root = ET.fromstring(dom.xml)
    vcpu = root.find("vcpu")
    if vcpu is None:
        vcpu = ET.SubElement(root, "vcpu")
        vcpu.text = str(dom.vcpus)
    vcpu.set("cpuset", core.format_cpulist(cpus))
    if numatune is not None:
        tune = root.find("numatune")
        if tune is None:
            tune = ET.SubElement(root, "numatune")
        memory = tune.find("memory")
        if memory is None:
            memory = ET.SubElement(tune, "memory")
        memory.set("mode", numatune[0])
        memory.set("nodeset", numatune[1])
    meta = root.find("metadata")
    if meta is None:
        meta = ET.SubElement(root, "metadata")
    mark = meta.find("{%s}place" % NS)
    if mark is None:
        mark = ET.SubElement(meta, "{%s}place" % NS)
    mark.set("mode", "auto")
    return ET.tostring(root, encoding="unicode")


def numatune_of(dom: Domain) -> Optional[Tuple[str, str]]:
    memory = ET.fromstring(dom.xml).find("./numatune/memory")
    if memory is None:
        return None
    return memory.get("mode", "strict"), memory.get("nodeset", "")


def define(virsh: Virsh, xml_text: str) -> bool:
    fd, path = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(xml_text)
        return virsh("define", path) is not None
    finally:
        os.unlink(path)
