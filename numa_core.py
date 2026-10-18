"""numa_core — calcul du placement NUMA partagé par pve-numa-place et xcpng-numa-place.

Ce module ne lit ni n'écrit rien sur l'hôte : il reçoit la topologie et les
besoins des invités, et rend le placement. Compatible Python 3.6 (dom0 XCP-ng 8.3).

Règles :
  - un invité occupe le plus petit nombre de nœuds qui le contient (vCPU et RAM) ;
  - s'il en faut plusieurs, ce sont des nœuds CONTIGUS (numéros consécutifs) ;
  - avec passthrough, la fenêtre de nœuds doit contenir le nœud de la carte ;
  - un invité déjà placé par l'outil garde sa place (sauf --rebalance) ;
  - les invités épinglés à la main ne bougent pas mais comptent dans la charge.
"""
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

MEM_FIT = 0.90  # part max de la RAM d'un nœud qu'on accepte de lier (policy bind)


class Node(NamedTuple):
    id: int
    cpus: Tuple[int, ...]
    cores: int       # cœurs physiques (threads SMT regroupés)
    mem_mb: int


class Request(NamedTuple):
    key: str                     # identifiant de l'invité (vmid, uuid…)
    vcpus: int
    mem_mb: int
    pci_nodes: Tuple[int, ...]   # nœuds des cartes en passthrough, la 1re prioritaire
    current: Tuple[int, ...]     # nœuds actuellement configurés, () si aucun
    auto: bool                   # déjà géré par l'outil (tag numa-auto)
    manual: bool                 # à laisser tel quel


class Placement(NamedTuple):
    key: str
    nodes: Tuple[Node, ...]      # () : invité non placé
    policy: str                  # "bind" | "preferred" | ""
    reason: str

    @property
    def node_ids(self) -> Tuple[int, ...]:
        return tuple(n.id for n in self.nodes)

    @property
    def cpus(self) -> Tuple[int, ...]:
        return tuple(sorted(c for n in self.nodes for c in n.cpus))


# ------------------------------------------------------------------ listes de CPU

def parse_cpulist(text: str) -> Tuple[int, ...]:
    cpus = []  # type: List[int]
    for part in filter(None, text.strip().split(",")):
        lo, _, hi = part.partition("-")
        cpus.extend(range(int(lo), int(hi or lo) + 1))
    return tuple(sorted(set(cpus)))


def format_cpulist(cpus: Iterable[int]) -> str:
    out = []  # type: List[str]
    run = []  # type: List[int]
    for c in sorted(set(cpus)):
        if run and c != run[-1] + 1:
            out.append(_fmt_run(run))
            run = []
        run.append(c)
    if run:
        out.append(_fmt_run(run))
    return ",".join(out)


def _fmt_run(run: List[int]) -> str:
    return str(run[0]) if len(run) == 1 else "{}-{}".format(run[0], run[-1])


def nodes_of_cpus(cpus: Iterable[int], nodes: Sequence[Node]) -> Tuple[int, ...]:
    wanted = set(cpus)
    return tuple(n.id for n in nodes if wanted & set(n.cpus))


# ------------------------------------------------------------------ placement

def capacity(node: Node, cores_only: bool) -> int:
    return node.cores if cores_only else len(node.cpus)


def windows(nodes: Sequence[Node], size: int) -> List[Tuple[Node, ...]]:
    ordered = sorted(nodes, key=lambda n: n.id)
    return [tuple(ordered[i:i + size]) for i in range(len(ordered) - size + 1)]


def nodes_needed(req: Request, nodes: Sequence[Node], cores_only: bool) -> Optional[int]:
    """Plus petit nombre de nœuds contigus qui contient l'invité, None si l'hôte est trop petit."""
    k_cpu = None
    for k in range(1, len(nodes) + 1):
        if any(sum(capacity(n, cores_only) for n in w) >= req.vcpus for w in windows(nodes, k)):
            k_cpu = k
            break
    if k_cpu is None:
        return None
    for k in range(k_cpu, len(nodes) + 1):
        if any(sum(n.mem_mb for n in w) * MEM_FIT >= req.mem_mb for w in windows(nodes, k)):
            return k
    return len(nodes)  # RAM trop grosse même pour tout l'hôte : policy preferred


def _mem_fits(req: Request, win: Tuple[Node, ...], load: Dict[int, List[float]]) -> bool:
    share = req.mem_mb / len(win)
    return all(load[n.id][0] + share <= n.mem_mb * MEM_FIT for n in win)


def _score(req: Request, win: Tuple[Node, ...], load: Dict[int, List[float]],
           cores_only: bool) -> float:
    k = len(win)
    return sum((load[n.id][0] + req.mem_mb / k) / n.mem_mb
               + (load[n.id][1] + req.vcpus / k) / capacity(n, cores_only)
               for n in win) / k


def _position(node: Node, nodes: Sequence[Node]) -> int:
    return sorted(n.id for n in nodes).index(node.id)


def _best(req: Request, cands: List[Tuple[Node, ...]], nodes: Sequence[Node],
          load: Dict[int, List[float]], cores_only: bool, reason: str) -> Placement:
    fitting = [w for w in cands if _mem_fits(req, w, load)]
    pool = fitting or cands
    # fenêtres alignées d'abord (0-1, 2-3…) : évite d'isoler des nœuds seuls
    aligned = [w for w in pool if _position(w[0], nodes) % len(w) == 0] or pool
    win = min(aligned, key=lambda w: (_score(req, w, load, cores_only), w[0].id))
    policy = "bind" if fitting else "preferred"
    span = "" if len(win) == 1 else ", {} nœuds contigus".format(len(win))
    return Placement(req.key, win, policy, reason + span)


def place_one(req: Request, nodes: Sequence[Node], load: Dict[int, List[float]],
              cores_only: bool = False, rebalance: bool = False) -> Placement:
    if req.manual:
        return Placement(req.key, (), "", "manuel : non modifié")
    k = nodes_needed(req, nodes, cores_only)
    if k is None:
        return Placement(req.key, (), "", "ignoré : {} vCPU > capacité de l'hôte".format(req.vcpus))
    cands = windows(nodes, k)
    known = {n.id for n in nodes}
    pci = [p for p in req.pci_nodes if p in known]
    if pci:
        with_card = [w for w in cands if pci[0] in {n.id for n in w}]
        most = max(len(set(pci) & {n.id for n in w}) for w in with_card)
        best = [w for w in with_card if len(set(pci) & {n.id for n in w}) == most]
        return _best(req, best, nodes, load, cores_only, "passthrough nœud {}".format(pci[0]))
    if req.auto and req.current and not rebalance:
        same = [w for w in cands if tuple(n.id for n in w) == tuple(sorted(req.current))]
        if same:
            return _best(req, same, nodes, load, cores_only, "placement conservé")
    return _best(req, cands, nodes, load, cores_only, "équilibrage")


def _add_load(load: Dict[int, List[float]], req: Request, ids: Sequence[int]) -> None:
    ids = [i for i in ids if i in load]
    for i in ids:
        load[i][0] += req.mem_mb / len(ids)
        load[i][1] += req.vcpus / len(ids)


def plan(nodes: Sequence[Node], reqs: Sequence[Request], cores_only: bool = False,
         rebalance: bool = False) -> Dict[str, Placement]:
    load = {n.id: [0.0, 0.0] for n in nodes}  # type: Dict[int, List[float]]
    for r in reqs:
        if r.manual and r.current:
            _add_load(load, r, r.current)
    kept = lambda r: r.auto and bool(r.current) and not rebalance  # noqa: E731
    order = sorted(reqs, key=lambda r: (not r.pci_nodes, not kept(r), -r.vcpus, -r.mem_mb, r.key))
    out = {}  # type: Dict[str, Placement]
    for r in order:
        p = place_one(r, nodes, load, cores_only, rebalance)
        if p.nodes:
            _add_load(load, r, p.node_ids)
        out[r.key] = p
    return out
