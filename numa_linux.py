"""numa_linux — lecture de la topologie et de la mémoire d'un hôte Linux nu (sysfs, /proc),
partagée par pve-numa-place (Proxmox) et kvm-numa-place (KVM + libvirt).

Fonctionne sur x86 (EPYC, Xeon, Xeon Phi), ARM et POWER : les frères SMT viennent des
listes calculées par le noyau, pas d'hypothèses sur la numérotation. Compatible Python 3.6.
"""
import glob
import re
import subprocess
import sys
from typing import Callable, Dict, Iterable, List, Optional, Tuple

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


def read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def siblings(cpus: Iterable[int], sysfs: str = "/sys") -> Dict[int, Tuple[int, ...]]:
    """Frères SMT de chaque CPU, tels que le noyau les calcule (x86, ARM, POWER, hybrides).

    core_cpus_list (noyaux récents) ou thread_siblings_list ; à défaut, regroupement
    par (paquet, core_id), qui n'est pas unique sur certains ARM."""
    out = {}      # type: Dict[int, Tuple[int, ...]]
    by_core = {}  # type: Dict[tuple, List[int]]
    for c in cpus:
        topo = "{}/devices/system/cpu/cpu{}/topology".format(sysfs, c)
        listed = read(topo + "/core_cpus_list") or read(topo + "/thread_siblings_list")
        if listed and listed.strip():
            out[c] = core.parse_cpulist(listed)
            continue
        key = ((read(topo + "/physical_package_id") or "").strip(),
               (read(topo + "/core_id") or str(c)).strip())
        by_core.setdefault(key, []).append(c)
    for group in by_core.values():
        for c in group:
            out[c] = tuple(group)
    return out


def read_topology(sysfs: str = "/sys") -> List[core.Node]:
    nodes = []
    paths = glob.glob(sysfs + "/devices/system/node/node[0-9]*")
    for d in sorted(paths, key=lambda p: int(p.rsplit("node", 1)[1])):
        nid = int(d.rsplit("node", 1)[1])
        cpus = core.parse_cpulist(read(d + "/cpulist") or "")
        m = re.search(r"MemTotal:\s+(\d+)\s*kB", read(d + "/meminfo") or "")
        if cpus and m:   # nœud sans CPU (MCDRAM, CXL…) : ignoré
            threads = core.group_siblings(cpus, siblings(cpus, sysfs))
            nodes.append(core.Node(nid, cpus, len(threads), int(m.group(1)) // 1024, threads))
    return nodes


def pci_node(addr: str, sysfs: str = "/sys") -> Optional[int]:
    """Nœud d'une carte PCI (ou d'une VF : le noyau renseigne aussi numa_node pour elles)."""
    raw = read("{}/bus/pci/devices/{}/numa_node".format(sysfs, addr))
    if raw is None or int(raw) < 0:
        return None
    return int(raw)


def node_meminfo(nid: int, sysfs: str = "/sys") -> Dict[str, int]:
    text = read("{}/devices/system/node/node{}/meminfo".format(sysfs, nid)) or ""
    return {k: int(v) // 1024 for k, v in re.findall(r"Node \d+ (\w+):\s+(\d+) kB", text)}


def host_memory_mb(nodes: Iterable[core.Node], guest_memory: Iterable[Dict[int, int]],
                   sysfs: str = "/sys") -> Dict[int, int]:
    """RAM prise par l'hôte hors invités, par nœud (Mo).

    Mémoire utilisée du nœud, sans le cache de pages (récupérable), moins la RAM
    des invités en marche sur ce nœud (guest_memory : un {nœud: Mo} par invité).
    Reste : services, noyau, et surtout l'ARC de ZFS, que le placement doit laisser libre."""
    in_guests = {}  # type: Dict[int, int]
    for per_node in guest_memory:
        for nid, mb in per_node.items():
            in_guests[nid] = in_guests.get(nid, 0) + mb
    out = {}
    for n in nodes:
        info = node_meminfo(n.id, sysfs)
        if "MemFree" not in info:   # meminfo incomplet : ne rien réserver plutôt que tout
            out[n.id] = 0
            continue
        used = info["MemTotal"] - info["MemFree"] - info.get("FilePages", 0)
        out[n.id] = max(0, used - in_guests.get(n.id, 0))
    return out


def numa_maps_mb(pid: str, proc: str = "/proc") -> Dict[int, int]:
    """RAM d'un processus par nœud (Mo), d'après /proc/PID/numa_maps."""
    per_node = {}  # type: Dict[int, int]
    for line in (read("{}/{}/numa_maps".format(proc, pid)) or "").splitlines():
        m = re.search(r"kernelpagesize_kB=(\d+)", line)
        page_kb = int(m.group(1)) if m else 4
        for nid, pages in re.findall(r"\bN(\d+)=(\d+)", line):
            per_node[int(nid)] = per_node.get(int(nid), 0) + int(pages) * page_kb
    return {k: v // 1024 for k, v in per_node.items()}


def cgroup_numa_mb(numa_stat_path: str) -> Dict[int, int]:
    """RAM d'un cgroup v2 par nœud (Mo), d'après memory.numa_stat (anon + file)."""
    per_node = {}  # type: Dict[int, int]
    for line in (read(numa_stat_path) or "").splitlines():
        if line.split(" ", 1)[0] in ("anon", "file"):
            for nid, size in re.findall(r"N(\d+)=(\d+)", line):
                per_node[int(nid)] = per_node.get(int(nid), 0) + int(size)
    return {k: v // 2**20 for k, v in per_node.items()}
