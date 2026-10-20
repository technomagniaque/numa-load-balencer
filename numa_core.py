"""numa_core — calcul du placement NUMA partagé par pve-numa-place et xcpng-numa-place.

Ce module ne lit ni n'écrit rien sur l'hôte : il reçoit la topologie et les
besoins des invités, et rend le placement. Compatible Python 3.6 (dom0 XCP-ng 8.3).

La mémoire d'un invité est sa mémoire GARANTIE (plancher du ballooning) : c'est
elle qui compte dans la charge des nœuds. Un invité à ballooning n'est jamais lié
strictement (policy preferred) : s'il regonfle sur un nœud plein, il déborde.

Règles :
  - un invité occupe le plus petit nombre de nœuds qui le contient (vCPU et RAM) ;
  - s'il en faut plusieurs, ce sont des nœuds CONTIGUS (numéros consécutifs) ;
  - avec passthrough, la fenêtre de nœuds contient TOUJOURS le nœud de la carte : une
    telle VM n'est jamais évacuée ni rééquilibrée hors de ce nœud ; si le nœud de la
    carte est introuvable, la VM garde ses nœuds actuels ;
  - un invité déjà placé par l'outil garde sa place (sauf --rebalance)…
  - … sauf une petite VM dont les nœuds sont saturés par la charge CPU RÉELLE
    (ex. une grosse VM devenue active) : elle est évacuée vers les nœuds les moins
    chargés, si le gain dépasse MOVE_GAIN pCPU et qu'elle n'a pas bougé récemment ;
  - quand la charge retombe (nœud d'origine sous RETURN_LOAD), une VM évacuée revient
    sur le nœud où se trouve sa RAM (Xen ne déplace pas la mémoire à chaud) ;
  - une VM « chaude » (HOT_FRACTION de ses vCPU, au moins HOT_MIN pCPU) ne bouge pas ;
  - les invités épinglés à la main ne bougent pas mais comptent dans la charge.

SMT (mode « spread », par défaut dans les outils) : quelle que soit la largeur SMT
(1, 2, 4, 8, ou mélange de cœurs P/E), les threads d'un même cœur physique ne vont
JAMAIS à la même VM. Chaque nœud est découpé en « voies » : la voie j réunit le
j-ième thread de chaque cœur ; une VM reçoit une seule voie, donc au plus un thread
par cœur, et ses vCPU ne se disputent jamais un cœur. Sans SMT il n'y a qu'une voie :
le comportement est celui d'un placement par nœud entier.
"""
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

MEM_FIT = 0.90  # part max de la RAM d'un nœud qu'on accepte de lier (policy bind)
HOT_FRACTION = 0.5   # VM « chaude » : consomme au moins la moitié de ses vCPU…
HOT_MIN = 2.0        # … et au moins 2 pCPU
OVERLOAD = 0.85      # nœud saturé au-delà de 85 % de ses pCPU
MOVE_GAIN = 1.0      # hystérésis : n'évacuer que si l'on gagne au moins 1 pCPU
RETURN_LOAD = 0.60   # retour au nœud de sa RAM quand il redescend sous 60 % (< OVERLOAD)


class Node(NamedTuple):
    id: int
    cpus: Tuple[int, ...]
    cores: int       # cœurs physiques (threads SMT regroupés)
    mem_mb: int
    threads: Tuple[Tuple[int, ...], ...] = ()  # frères SMT de chaque cœur ; () = 1 thread/cœur


class Request(NamedTuple):
    key: str                     # identifiant de l'invité (vmid, uuid…)
    vcpus: int
    mem_mb: int                  # mémoire GARANTIE : minimum du ballooning, sinon la taille fixe
    pci_nodes: Tuple[int, ...]   # nœuds des cartes en passthrough, la 1re prioritaire
    current: Tuple[int, ...]     # nœuds actuellement configurés, () si aucun
    auto: bool                   # déjà géré par l'outil (tag numa-auto)
    manual: bool                 # à laisser tel quel
    elastic: bool = False        # ballooning actif : la RAM peut dépasser mem_mb
    usage: Optional[float] = None  # pCPU réellement consommés (lissés), None = inconnu
    cooldown: bool = False       # déplacé récemment : ne pas le redéplacer tout de suite
    home: Tuple[int, ...] = ()   # nœud(s) où se trouve sa RAM après une évacuation
    lane: int = -1               # voie SMT actuelle (-1 : nœuds entiers ou inconnue)
    passthrough: bool = False    # a un périphérique PCI (GPU, VF…), même si son nœud est inconnu


class Placement(NamedTuple):
    key: str
    nodes: Tuple[Node, ...]      # () : invité non placé
    policy: str                  # "bind" | "preferred" | ""
    reason: str
    lane: int = -1               # voie SMT attribuée (-1 : tous les threads des nœuds)

    @property
    def node_ids(self) -> Tuple[int, ...]:
        return tuple(n.id for n in self.nodes)

    @property
    def cpus(self) -> Tuple[int, ...]:
        if self.lane < 0:
            return tuple(sorted(c for n in self.nodes for c in n.cpus))
        return tuple(sorted(c for n in self.nodes for c in lane_cpus(n, self.lane)))


# ------------------------------------------------------------------ topologie SMT

def group_siblings(cpus: Iterable[int], siblings: Dict[int, Iterable[int]]) -> Tuple[Tuple[int, ...], ...]:
    """Regroupe les CPU d'un nœud par cœur physique.

    siblings[c] = CPU qui partagent le cœur de c (c compris), tels que donnés par le
    noyau (core_cpus_list / thread_siblings_list) ou par Xen ; marche pour SMT1 à SMT8,
    ARM, x86 et cœurs hybrides. Un CPU sans information forme un cœur à lui seul."""
    mine = set(cpus)
    groups = set()
    for c in mine:
        groups.add(tuple(sorted(set(siblings.get(c, (c,))) & mine | {c})))
    return tuple(sorted(groups))


def lanes(node: Node) -> Tuple[Tuple[int, ...], ...]:
    """Voies SMT du nœud : la voie j réunit le j-ième thread de chaque cœur."""
    if not node.threads:
        return (node.cpus,)
    width = max(len(core) for core in node.threads)
    return tuple(tuple(sorted(core[j] for core in node.threads if len(core) > j))
                 for j in range(width))


def smt_width(node: Node) -> int:
    return len(lanes(node))


def lane_cpus(node: Node, lane: int) -> Tuple[int, ...]:
    all_lanes = lanes(node)
    return all_lanes[lane % len(all_lanes)]


def lane_of(cpus: Iterable[int], nodes: Sequence[Node]) -> int:
    """Voie SMT correspondant exactement à un masque, -1 si ce n'est pas une voie."""
    wanted = set(cpus)
    mine = [n for n in nodes if wanted & set(n.cpus)]
    if not mine or max(smt_width(n) for n in mine) <= 1:
        return -1
    for j in range(max(smt_width(n) for n in mine)):
        if wanted == {c for n in mine for c in lane_cpus(n, j)}:
            return j
    return -1



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

class _Ctx(object):
    """Topologie, charge courante et options d'un calcul de placement.

    load[nid] = [Mo de RAM, pCPU sur le nœud, [pCPU par voie SMT]]"""

    def __init__(self, nodes: Sequence[Node], cores_only: bool, spread: bool,
                 reserved_mb: Optional[Dict[int, int]]) -> None:
        self.nodes = list(nodes)
        self.cores_only = cores_only
        self.spread = spread and any(smt_width(n) > 1 for n in nodes)
        self.load = {n.id: [float((reserved_mb or {}).get(n.id, 0)), 0.0, [0.0] * smt_width(n)]
                     for n in nodes}  # type: Dict[int, list]

    def fit_capacity(self, node: Node) -> int:
        """vCPU qu'un nœud peut accueillir pour UNE VM (une voie = un thread par cœur)."""
        return node.cores if (self.cores_only or self.spread) else len(node.cpus)

    def node_capacity(self, node: Node) -> int:
        return node.cores if self.cores_only else len(node.cpus)


def capacity(node: Node, cores_only: bool) -> int:
    return node.cores if cores_only else len(node.cpus)


def cpu_weight(req: Request) -> float:
    """Charge CPU d'un invité : sa consommation mesurée, sinon son nombre de vCPU."""
    return req.usage if req.usage is not None else float(req.vcpus)


def is_hot(req: Request) -> bool:
    return req.usage is not None and req.usage >= max(HOT_MIN, HOT_FRACTION * req.vcpus)


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
    if req.pci_nodes:
        # passthrough : les vCPU restent près de la carte ; la RAM qui ne tient pas
        # déborde sur les nœuds voisins (policy preferred) plutôt que d'éloigner les vCPU
        return k_cpu
    for k in range(k_cpu, len(nodes) + 1):
        if any(sum(n.mem_mb for n in w) * MEM_FIT >= req.mem_mb for w in windows(nodes, k)):
            return k
    return len(nodes)  # RAM trop grosse même pour tout l'hôte : policy preferred


def _mem_fits(req: Request, win: Tuple[Node, ...], ctx: _Ctx) -> bool:
    share = req.mem_mb / len(win)
    return all(ctx.load[n.id][0] + share <= n.mem_mb * MEM_FIT for n in win)


def _lane_load(ctx: _Ctx, node: Node, lane: int) -> float:
    per_lane = ctx.load[node.id][2]
    return per_lane[lane % len(per_lane)]


def _peak(req: Request, win: Tuple[Node, ...], ctx: _Ctx, lane: int = -1) -> float:
    """Occupation CPU du nœud (ou de la voie) le plus chargé de la fenêtre, invité compris."""
    share = cpu_weight(req) / len(win)
    if lane < 0:
        return max((ctx.load[n.id][1] + share) / ctx.node_capacity(n) for n in win)
    return max((_lane_load(ctx, n, lane) + share) / len(lane_cpus(n, lane)) for n in win)


def _score(req: Request, win: Tuple[Node, ...], ctx: _Ctx, lane: int) -> float:
    k = len(win)
    mem = sum((ctx.load[n.id][0] + req.mem_mb / k) / n.mem_mb for n in win) / k
    return mem + _peak(req, win, ctx, lane)


def _lanes_for(req: Request, win: Tuple[Node, ...], ctx: _Ctx, hint: int) -> List[int]:
    """Voies candidates : celles assez larges pour les vCPU de la VM (cœurs P/E inégaux)."""
    if not ctx.spread:
        return [-1]
    width = min(smt_width(n) for n in win)
    if 0 <= hint < width:
        return [hint]
    wide = [j for j in range(width) if sum(len(lane_cpus(n, j)) for n in win) >= req.vcpus]
    return wide or [0]


def _position(node: Node, nodes: Sequence[Node]) -> int:
    return sorted(n.id for n in nodes).index(node.id)


def _best(req: Request, cands: List[Tuple[Node, ...]], ctx: _Ctx, reason: str,
          lane_hint: int = -1) -> Placement:
    fitting = [w for w in cands if _mem_fits(req, w, ctx)]
    pool = fitting or cands
    # fenêtres alignées d'abord (0-1, 2-3…) : évite d'isoler des nœuds seuls
    aligned = [w for w in pool if _position(w[0], ctx.nodes) % len(w) == 0] or pool
    options = [(w, j) for w in aligned for j in _lanes_for(req, w, ctx, lane_hint)]
    win, lane = min(options, key=lambda o: (_score(req, o[0], ctx, o[1]), o[0][0].id, o[1]))
    # avec ballooning, la RAM peut regonfler au-delà du nœud : jamais de liaison stricte
    policy = "bind" if fitting and not req.elastic else "preferred"
    span = "" if len(win) == 1 else ", {} nœuds contigus".format(len(win))
    balloon = ", ballooning" if req.elastic else ""
    smt = ", thread {}/{}".format(lane + 1, min(smt_width(n) for n in win)) if lane >= 0 else ""
    return Placement(req.key, win, policy, reason + span + smt + balloon, lane)


def place_one(req: Request, nodes: Sequence[Node], load: Optional[_Ctx] = None,
              cores_only: bool = False, rebalance: bool = False) -> Placement:
    ctx = load if isinstance(load, _Ctx) else _Ctx(nodes, cores_only, False, None)
    if req.manual:
        return Placement(req.key, (), "", "manuel : non modifié")
    k = nodes_needed(req, nodes, ctx.cores_only or ctx.spread)
    if k is None:
        unit = "cœurs" if ctx.spread else "pCPU"
        return Placement(req.key, (), "", "ignoré : {} vCPU > {} de l'hôte".format(req.vcpus, unit))
    cands = windows(nodes, k)
    known = {n.id for n in nodes}
    pci = [p for p in req.pci_nodes if p in known]
    if pci:
        with_card = [w for w in cands if pci[0] in {n.id for n in w}]
        most = max(len(set(pci) & {n.id for n in w}) for w in with_card)
        best = [w for w in with_card if len(set(pci) & {n.id for n in w}) == most]
        return _best(req, best, ctx, "passthrough nœud {}".format(pci[0]), req.lane)
    if req.passthrough and req.current:
        # carte présente mais nœud introuvable : on ne devine pas, la VM ne bouge jamais
        same = [w for w in cands if tuple(n.id for n in w) == tuple(sorted(req.current))]
        if same:
            return _best(req, same, ctx, "passthrough (nœud de la carte inconnu) : figé", req.lane)
    if req.auto and req.current and not rebalance:
        same = [w for w in cands if tuple(n.id for n in w) == tuple(sorted(req.current))]
        if same:
            back = _return_home(req, cands, ctx)
            return back or _keep_or_evacuate(req, same[0], cands, ctx)
    return _best(req, cands, ctx, "équilibrage")


def _return_home(req: Request, cands: List[Tuple[Node, ...]], ctx: _Ctx) -> Optional[Placement]:
    """VM évacuée : la ramener sur le nœud de sa RAM dès que la charge y est retombée."""
    home = tuple(sorted(req.home))
    if not home or home == tuple(sorted(req.current)) or req.cooldown or req.usage is None:
        return None
    win = [w for w in cands if tuple(n.id for n in w) == home]
    if not win:
        return None
    label = "retour près de sa RAM (nœud(s) {})".format(format_cpulist(home))
    back = _best(req, win, ctx, label)
    return back if _peak(req, back.nodes, ctx, back.lane) <= RETURN_LOAD else None


def _keep_or_evacuate(req: Request, current: Tuple[Node, ...], cands: List[Tuple[Node, ...]],
                      ctx: _Ctx) -> Placement:
    """Une VM déjà placée reste, sauf petite VM sur des nœuds saturés par la charge réelle."""
    keep = _best(req, [current], ctx, "placement conservé", req.lane)
    if req.usage is None or req.cooldown or is_hot(req):
        return keep
    before = _peak(req, current, ctx, keep.lane)
    if before <= OVERLOAD:
        return keep
    moved = _best(req, cands, ctx, "évacuation")
    unit = len(lane_cpus(current[0], keep.lane)) if keep.lane >= 0 else ctx.node_capacity(current[0])
    gain_pcpu = (before - _peak(req, moved.nodes, ctx, moved.lane)) * unit
    if (moved.node_ids, moved.lane) == (keep.node_ids, keep.lane) or gain_pcpu < MOVE_GAIN:
        return keep
    label = "évacuation : nœud(s) {} à {:.0%}".format(format_cpulist(keep.node_ids), before)
    return moved._replace(reason=moved.reason.replace("évacuation", label, 1))


def _add_load(ctx: _Ctx, req: Request, ids: Sequence[int], lane: int) -> None:
    ids = [i for i in ids if i in ctx.load]
    for i in ids:
        entry = ctx.load[i]
        share = cpu_weight(req) / len(ids)
        entry[0] += req.mem_mb / len(ids)
        entry[1] += share
        per_lane = entry[2]
        if lane >= 0:
            per_lane[lane % len(per_lane)] += share
        else:  # nœud entier (épinglage manuel, mode sans voies) : réparti sur les voies
            for j in range(len(per_lane)):
                per_lane[j] += share / len(per_lane)


def plan(nodes: Sequence[Node], reqs: Sequence[Request], cores_only: bool = False,
         rebalance: bool = False, reserved_mb: Optional[Dict[int, int]] = None,
         spread: bool = False) -> Dict[str, Placement]:
    """reserved_mb : mémoire déjà prise par nœud hors invités (ex. dom0 Xen), en Mo.
    spread : une voie SMT par VM (jamais deux threads d'un même cœur dans une VM)."""
    ctx = _Ctx(nodes, cores_only, spread, reserved_mb)
    for r in reqs:
        if r.manual and r.current:
            _add_load(ctx, r, r.current, r.lane)
    kept = lambda r: r.auto and bool(r.current) and not rebalance  # noqa: E731
    # d'abord les ancres (carte, VM chaudes), puis les plus gros consommateurs :
    # les petites VM sont jugées contre la charge réelle déjà posée
    order = sorted(reqs, key=lambda r: (not r.pci_nodes, not is_hot(r), not kept(r),
                                        -cpu_weight(r), -r.vcpus, -r.mem_mb, r.key))
    out = {}  # type: Dict[str, Placement]
    for r in order:
        p = place_one(r, nodes, ctx, cores_only, rebalance)
        if p.nodes:
            _add_load(ctx, r, p.node_ids, p.lane)
        out[r.key] = p
    return out
