"""numa_xen — lecture de la topologie NUMA d'un hôte Xen via xl, partagée par
xcpng-numa-place (XCP-ng) et xen-numa-place (Debian + libvirt).

Le dom0 est PV : Linux n'y voit pas la topologie NUMA (numa_node = -1 partout).
On la demande donc à l'hyperviseur, avec « xl info -n », qui donne aussi le nœud
de chaque périphérique PCI (section « device topology »). Compatible Python 3.6.
"""
import re
import subprocess
import sys
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numa_core as core

Runner = Callable[[List[str]], Optional[str]]


def run_cmd(cmd: List[str]) -> Optional[str]:
    """Lance une commande ; rend sa sortie, ou None (erreur journalisée) si elle échoue."""
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True, check=False)
    except OSError as exc:
        print("échec {} : {}".format(" ".join(cmd), exc), file=sys.stderr)
        return None
    if res.returncode != 0:
        print("échec {} : {}".format(" ".join(cmd), res.stderr.strip()), file=sys.stderr)
        return None
    return res.stdout


def _sections(text: str) -> Dict[str, List[str]]:
    """Découpe « xl info -n » en sections (cpu_topology, device topology, numa_info)."""
    out = {}  # type: Dict[str, List[str]]
    current = None
    for line in text.splitlines():
        m = re.match(r"^(\w[\w ]*?)\s+:\s*(.*)$", line)
        if m:
            current = m.group(1).strip()
            out.setdefault(current, [])
        elif current is not None:
            out[current].append(line)
    return out


def parse_xl_info(text: str) -> Tuple[List[core.Node], Dict[str, int]]:
    """Rend (nœuds, {adresse PCI: nœud}) depuis la sortie de « xl info -n »."""
    sec = _sections(text)
    cpus_of = {}   # type: Dict[int, List[int]]
    cores_of = {}  # type: Dict[int, set]
    for line in sec.get("cpu_topology", []):
        m = re.match(r"^\s*(\d+):\s+(\d+)\s+(\d+)\s+(\d+)\s*$", line)
        if m:
            cpu, core_id, socket, node = (int(x) for x in m.groups())
            cpus_of.setdefault(node, []).append(cpu)
            cores_of.setdefault(node, set()).add((socket, core_id))
    mem_of = {}  # type: Dict[int, int]
    for line in sec.get("numa_info", []):
        m = re.match(r"^\s*(\d+):\s+(\d+)\s+(\d+)", line)
        if m:
            mem_of[int(m.group(1))] = int(m.group(2))  # memsize, en Mo
    nodes = [core.Node(n, tuple(sorted(cpus_of[n])), len(cores_of[n]), mem_of[n])
             for n in sorted(cpus_of) if n in mem_of]
    pci = {}  # type: Dict[str, int]
    for line in sec.get("device topology", []):
        m = re.match(r"^\s*([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7])\s+(\d+)\s*$", line, re.I)
        if m:
            pci[m.group(1).lower()] = int(m.group(2))
    return nodes, pci


def read_topology(run: Runner = run_cmd) -> Tuple[List[core.Node], Dict[str, int]]:
    text = run(["xl", "info", "-n"])
    return parse_xl_info(text) if text else ([], {})


def parse_domain_memory(dmesg: str) -> Dict[int, Dict[int, int]]:
    """Mémoire par nœud de chaque domaine (Mo), depuis le dernier dump « xl debug-keys u »."""
    start = dmesg.rfind("Memory location of each domain")
    if start < 0:
        return {}
    out = {}  # type: Dict[int, Dict[int, int]]
    dom = None
    for line in dmesg[start:].splitlines():
        m = re.search(r"(?:Domain |\bd)(\d+) \(total: \d+\)", line)
        if m:
            dom = int(m.group(1))
            out[dom] = {}
            continue
        m = re.search(r"Node (\d+): (\d+)", line)
        if m and dom is not None:
            out[dom][int(m.group(1))] = int(m.group(2)) * 4 // 1024  # pages de 4 Kio
    return out


def domain_memory(run: Runner = run_cmd) -> Dict[int, Dict[int, int]]:
    if run(["xl", "debug-keys", "u"]) is None:
        return {}
    return parse_domain_memory(run(["xl", "dmesg"]) or "")


def pin_live(domain: str, cpus: Sequence[int], run: Runner = run_cmd) -> bool:
    """Épingle à chaud tous les vCPU d'un domaine (affinité dure)."""
    return run(["xl", "vcpu-pin", domain, "all", core.format_cpulist(cpus)]) is not None


def format_memory(per_node: Dict[int, int]) -> str:
    return " ".join("N{}={}M".format(k, v) for k, v in sorted(per_node.items()) if v) or "?"
