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

MEM_FIT = 0.98  # part max de la RAM d'un nœud qu'on accepte de lier (policy bind)
HOT_FRACTION = 0.5   # VM « chaude » : consomme au moins la moitié de ses vCPU…
HOT_MIN = 2.0        # … et au moins 2 pCPU
OVERLOAD = 0.80      # nœud saturé au-delà de 80 % de ses pCPU (4,8 / 6 sur R710 → dès 5 cœurs)
MOVE_GAIN = 1.0      # hystérésis : n'évacuer que si l'on gagne au moins 1 pCPU
# Retour au nœud de sa RAM pour une VM SANS carte. Le seuil dépend du die/socket visé :
# un nœud qui SERT une carte PCIe est plus protégé — on n'y revient que s'il est
# nettement libre, pour laisser la place à l'invité dont le lien ou la RAM VFIO y est.
RETURN_LOAD = 0.60          # nœud sans aucun passthrough
RETURN_LOAD_PCI_DIE = 0.50  # nœud qui porte un invité passthrough
# Un invité à carte PCIe ne quitte son nœud QUE sous charge extrême : au moins 5 des
# 6 cœurs du socket à 100 % (adapté au Dell R710 2×6 cœurs). Mesuré sur la consommation
# RÉELLE des invités qui sont sur ce nœud, pas sur la charge que le calcul vient d'y
# poser — un invité à carte est placé en premier, le contexte serait donc vide à ce moment-là.
PCI_EXTREME = 5.0 / 6.0   # part des CŒURS du nœud occupés à 100 % : 5 sur 6 (83,3 %)
PCI_RETURN = 4.0 / 6.0    # … et elle ne revient que sous 4 sur 6 (66,7 %) : bande d'hystérésis
PCI_HOPS = 1              # repli autorisé à 1 saut NUMA au plus (sur bi-socket 10/20 : l'autre socket)
# Sortir à 5/6 (83,3 %) et rentrer à 4/6 (66,7 %) laisse 1 cœur complet de marge. Sans cette
# bande, une charge qui oscille autour du seuil ferait faire l'aller-retour à la VM, et chaque
# aller-retour perd la localité de sa RAM — on paierait le remède plus que le mal.


class Node(NamedTuple):
    id: int
    cpus: Tuple[int, ...]
    cores: int       # cœurs physiques (threads SMT regroupés)
    mem_mb: int
    threads: Tuple[Tuple[int, ...], ...] = ()  # frères SMT de chaque cœur ; () = 1 thread/cœur
    distances: Tuple[int, ...] = ()  # ligne du nœud dans la matrice NUMA ; () = inconnue


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
    pci_shared: bool = False     # IOH PCIe unique partagé entre sockets (ex. Intel 5520 du R710)


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
                 reserved_mb: Optional[Dict[int, int]],
                 measured: Optional[Dict[int, float]] = None,
                 pci_dies: Optional[Iterable[int]] = None) -> None:
        self.nodes = list(nodes)
        self.measured = dict(measured or {})
        self.pci_dies = set(pci_dies or ())
        self.cores_only = cores_only
        self.spread = spread and any(smt_width(n) > 1 for n in nodes)
        self.load = {n.id: [float((reserved_mb or {}).get(n.id, 0)), 0.0, [0.0] * smt_width(n)]
                     for n in nodes}  # type: Dict[int, list]

    def fit_capacity(self, node: Node) -> int:
        """vCPU qu'un nœud peut accueillir pour UNE VM (une voie = un thread par cœur)."""
        return node.cores if (self.cores_only or self.spread) else len(node.cpus)

    def node_capacity(self, node: Node) -> int:
        return node.cores if self.cores_only else len(node.cpus)

    def return_load(self, ids: Iterable[int]) -> float:
        """Seuil de retour : plus bas si l'un des nœuds visés porte une carte."""
        return (RETURN_LOAD_PCI_DIE if any(i in self.pci_dies for i in ids)
                else RETURN_LOAD)

    def die_extreme(self, nid: int, moins: float = 0.0) -> Optional[float]:
        """pCPU mesurés sur ce die s'il est sous charge extrême, sinon None.

        `moins` retire la part d'un invité du calcul : un invité ne doit pas
        pouvoir provoquer sa PROPRE expulsion. Ce qui décide, c'est la charge
        que les AUTRES mettent sur le die — c'est elle qu'il ne pourra pas fuir
        en restant, et celle qu'il soulage en partant."""
        node = next((n for n in self.nodes if n.id == nid), None)
        if node is None or nid not in self.measured:
            return None
        pris = self.measured[nid] - moins
        return pris if pris >= PCI_EXTREME * node.cores else None


def capacity(node: Node, cores_only: bool) -> int:
    return node.cores if cores_only else len(node.cpus)


def hops(card: int, nid: int, nodes: Sequence[Node]) -> int:
    """Sauts NUMA entre deux nœuds : 0 = le même, 1 = voisin, 2+ = plus loin.

    La matrice `distance` du noyau décide quand elle est lue : « voisin » est la
    plus petite distance non locale. Sur un bi-socket (16 intra, 32 inter) elle
    exclut donc l'autre socket, ce que l'adjacence des numéros ne ferait pas.
    Sans matrice, on retombe sur l'adjacence des numéros de nœud."""
    if card == nid:
        return 0
    row = next((n.distances for n in nodes if n.id == card), ())
    if row and max(card, nid) < len(row):
        autres = sorted({d for i, d in enumerate(row) if i != card})
        if autres:
            return 1 if row[nid] <= autres[0] else 2
    return 1 if abs(nid - card) == 1 else 2


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
    if req.pci_nodes or (req.passthrough and req.pci_shared):
        # passthrough : les vCPU restent sur le plus petit nombre de nœuds (près de la
        # carte, ou sur 1 socket sur IOH partagé) ; la RAM qui ne tient pas déborde
        # sur les nœuds voisins (policy preferred) plutôt que d'éloigner les vCPU
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


def _pci_repli(req: Request, card: int, cands: List[Tuple[Node, ...]],
               ctx: _Ctx) -> Optional[Placement]:
    """Sortie du die de la carte, et retour — les deux sous conditions.

    Rend None quand l'invité doit être (re)placé sur le die de sa carte, c'est
    le cas normal, et une Placement quand il doit rester ou aller ailleurs.

    Sortir exige TOUT ceci :
      - le nœud de la carte est à PCI_EXTREME de ses cœurs à 100 % (5 sur 6),
        mesuré sur la consommation réelle des AUTRES invités qui y sont — un
        invité ne provoque pas sa propre expulsion ;
      - le repli est à PCI_HOPS saut NUMA au plus, jamais au-delà : un nœud à deux
        sauts coûterait plus cher que la saturation qu'on fuit ;
      - aucun nœud du repli n'est lui-même sous charge extrême.

    Revenir n'arrive que sous PCI_RETURN (4/6 = 66,7 %). Entre 66,7 % et 83,3 %,
    l'invité déjà sorti RESTE où il est : c'est la bande d'hystérésis, sans quoi une
    charge qui oscille autour du seuil lui ferait faire l'aller-retour indéfiniment.
    """
    node = next((n for n in ctx.nodes if n.id == card), None)
    if node is None or not node.cores or card not in ctx.measured:
        return None
    part = (req.usage / len(req.current)) if (req.usage is not None and req.current) else 0.0
    charge = ctx.measured[card] - (part if card in req.current else 0.0)
    frac = charge / node.cores
    loin = bool(req.current) and card not in req.current
    rester = [w for w in cands if tuple(n.id for n in w) == tuple(sorted(req.current))]

    if loin and (req.cooldown or frac >= PCI_RETURN):
        # déplacé à l'instant, ou die encore au-dessus du seuil de retour
        if rester:
            motif = ("déplacé récemment : on ne le rebouge pas" if req.cooldown else
                     "die {} encore à {:.0%} : pas de retour avant {:.0%}".format(
                         card, frac, PCI_RETURN))
            return _best(req, rester, ctx, "hors du die de la carte, " + motif, req.lane)
    if frac < PCI_EXTREME or req.cooldown:
        return None                     # cas normal : le die de la carte
    voisins = [w for w in cands
               if card not in {n.id for n in w}
               and all(hops(card, n.id, ctx.nodes) <= PCI_HOPS for n in w)
               and all(ctx.die_extreme(n.id, part if n.id in req.current else 0.0) is None
                       for n in w)]
    if not voisins:
        return None                     # nulle part de sain à 1 saut : on reste
    return _best(req, voisins, ctx,
                 "die {} de la carte saturé ({:.1f} cœurs sur {} à 100 %) :"
                 " repli à 1 saut".format(card, charge, node.cores), req.lane)


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
        repli = _pci_repli(req, pci[0], cands, ctx)
        return repli or _best(req, best, ctx, "passthrough nœud {}".format(pci[0]), req.lane)
    if req.passthrough and req.pci_shared:
        # IOH PCIe unique partagé symétriquement entre les sockets (ex. Intel 5520 du R710) :
        # toutes les cartes ont numa_node = -1 car le lien est équidistant de chaque socket.
        # La 1re passe place la VM sur le socket le moins chargé ; ensuite sa RAM VFIO reste
        # ancrée sur son socket d'origine (home après repli, sinon current), qu'elle ne quitte
        # que sous charge extrême (PCI_EXTREME).
        anchor = next((n for n in (req.home or req.current) if n in known), None)
        if anchor is not None:
            with_anchor = [w for w in cands if anchor in {n.id for n in w}]
            repli = _pci_repli(req, anchor, cands, ctx)
            return repli or _best(req, with_anchor, ctx,
                                  "passthrough (IOH partagé) nœud {}".format(anchor), req.lane)
        return _best(req, cands, ctx, "passthrough (IOH partagé)", req.lane)
    if req.passthrough:
        # Carte présente mais nœud introuvable (numa_node = -1 : ACPI muet, machine
        # mono-socket vue comme telle…). On ne devine pas le die : placer à la charge
        # reviendrait à choisir au hasard le die d'une carte dont le lien PCIe est
        # ailleurs, et à l'y laisser. La VM déjà placée est donc figée, et celle qui
        # ne l'est pas n'est pas placée du tout — l'absence d'épinglage est le seul
        # état honnête quand on ignore où est le lien.
        same = [w for w in cands if tuple(n.id for n in w) == tuple(sorted(req.current))]
        if same:
            return _best(req, same, ctx, "passthrough (nœud de la carte inconnu) : figé", req.lane)
        return Placement(req.key, (), "",
                         "passthrough : nœud de la carte inconnu, non placé")
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
    label = "retour près de sa RAM (nœud(s) {}, seuil {:.0%})".format(
        format_cpulist(home), ctx.return_load(home))
    back = _best(req, win, ctx, label)
    seuil = ctx.return_load(home)
    return back if _peak(req, back.nodes, ctx, back.lane) <= seuil else None


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


def measured_load(reqs: Sequence[Request]) -> Dict[int, float]:
    """pCPU réellement consommés sur chaque nœud, d'après OÙ SONT les invités.

    On ne peut pas lire la charge d'un die dans le contexte de placement : un
    invité à carte est placé en premier (c'est une ancre), le contexte est donc
    encore vide quand on décide de son sort. La seule mesure honnête est celle
    des invités là où ils se trouvent réellement, avant tout calcul."""
    out = {}  # type: Dict[int, float]
    for r in reqs:
        if r.usage is None or not r.current:
            continue
        share = r.usage / len(r.current)
        for nid in r.current:
            out[nid] = out.get(nid, 0.0) + share
    return out


def plan(nodes: Sequence[Node], reqs: Sequence[Request], cores_only: bool = False,
         rebalance: bool = False, reserved_mb: Optional[Dict[int, int]] = None,
         spread: bool = False) -> Dict[str, Placement]:
    """reserved_mb : mémoire déjà prise par nœud hors invités (ex. dom0 Xen), en Mo.
    spread : une voie SMT par VM (jamais deux threads d'un même cœur dans une VM)."""
    pci_dies = {n for r in reqs for n in r.pci_nodes}
    for r in reqs:
        if not r.pci_nodes and r.passthrough and r.pci_shared:
            pci_dies.update(r.home or r.current)
    ctx = _Ctx(nodes, cores_only, spread, reserved_mb, measured_load(reqs), pci_dies)
    for r in reqs:
        if r.manual and r.current:
            _add_load(ctx, r, r.current, r.lane)
    kept = lambda r: r.auto and bool(r.current) and not rebalance  # noqa: E731
    is_pci = lambda r: bool(r.pci_nodes) or (r.passthrough and r.pci_shared)  # noqa: E731
    # d'abord les ancres (carte, VM chaudes), puis les plus gros consommateurs :
    # les petites VM sont jugées contre la charge réelle déjà posée
    order = sorted(reqs, key=lambda r: (not is_pci(r), not is_hot(r), not kept(r),
                                        -cpu_weight(r), -r.vcpus, -r.mem_mb, r.key))
    out = {}  # type: Dict[str, Placement]
    for r in order:
        p = place_one(r, nodes, ctx, cores_only, rebalance)
        if p.nodes:
            _add_load(ctx, r, p.node_ids, p.lane)
            if is_pci(r) and not r.pci_nodes:
                ctx.pci_dies.update(p.node_ids)
        out[r.key] = p
    return out
