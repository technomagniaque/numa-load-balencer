"""numa_state — mémoire d'un passage à l'autre, partagée par les trois outils.

Garde, pour chaque invité, sa consommation CPU lissée (moyenne exponentielle)
et la date de son dernier déplacement. Le lissage évite de réagir à un pic de
quelques secondes ; le délai évite qu'une VM fasse des allers-retours entre nœuds.
Les fonctions rendent de nouveaux dictionnaires (aucune mutation). Python 3.6.
"""
import json
import os
import sys
import tempfile
import time
from typing import Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple

ALPHA = 0.5          # poids de la nouvelle mesure dans la moyenne lissée
COOLDOWN_S = 600     # une VM déplacée ne rebouge pas avant 10 minutes

State = Dict[str, Dict[str, object]]


def load(path: str) -> State:
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(path: str, state: Mapping[str, Mapping[str, float]]) -> None:
    """Écriture atomique (fichier temporaire puis renommage)."""
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-")
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        print("état non sauvegardé ({}) : {}".format(path, exc), file=sys.stderr)


def smooth(state: Mapping[str, Mapping[str, float]], samples: Mapping[str, float],
           alpha: float = ALPHA) -> State:
    """Intègre une mesure (pCPU par invité) dans la moyenne lissée."""
    out = {k: dict(v) for k, v in state.items()}
    for key, value in samples.items():
        prev = out.get(key, {}).get("ema")
        ema = value if prev is None else alpha * value + (1 - alpha) * prev
        out[key] = dict(out.get(key, {}), ema=round(ema, 3))
    return out


def usage(state: Mapping[str, Mapping[str, float]], key: str) -> Optional[float]:
    return state.get(key, {}).get("ema")


def in_cooldown(state: Mapping[str, Mapping[str, float]], key: str, now: float,
                cooldown: float = COOLDOWN_S) -> bool:
    moved = state.get(key, {}).get("moved")
    return moved is not None and now - moved < cooldown


def mark_moved(state: Mapping[str, Mapping[str, float]], keys: Iterable[str], now: float) -> State:
    out = {k: dict(v) for k, v in state.items()}
    for key in keys:
        out[key] = dict(out.get(key, {}), moved=now)
    return out


def home(state: Mapping[str, Mapping[str, object]], key: str, boot: str = "") -> Tuple[int, ...]:
    """Nœud(s) où se trouve la RAM d'une VM évacuée, () si inconnu.

    Ignoré si la VM a redémarré depuis (identifiant de démarrage différent : domid
    Xen, PID QEMU) : sa RAM a alors été réallouée là où elle tourne."""
    entry = state.get(key, {})
    nodes = entry.get("home")
    if not nodes or (boot and entry.get("boot") not in (None, "", boot)):
        return ()
    return tuple(int(n) for n in nodes)


def record_moves(state: Mapping[str, Mapping[str, object]],
                 moves: Mapping[str, Tuple[Sequence[int], Sequence[int]]],
                 boots: Mapping[str, str], now: float) -> State:
    """Note chaque déplacement (avant, après) : date, et nœud d'origine de la RAM.

    Le premier déplacement retient l'origine ; le retour sur l'origine l'efface."""
    out = {k: dict(v) for k, v in state.items()}
    for key, (before, after) in moves.items():
        entry = dict(out.get(key, {}), moved=now)
        origin = home(state, key, boots.get(key, ""))
        if origin and tuple(sorted(after)) == origin:
            entry.pop("home", None)
            entry.pop("boot", None)
        elif not origin:
            entry.update(home=sorted(before), boot=boots.get(key, ""))
        out[key] = entry
    return out


def measure(read_times: Callable[[], Dict[str, float]], seconds: float,
            sleep: Optional[Callable[[float], None]] = None,
            clock: Optional[Callable[[], float]] = None) -> Dict[str, float]:
    """pCPU consommés par invité : écart de temps CPU cumulé sur « seconds » secondes.

    read_times() rend {clé: secondes CPU cumulées} ; un invité absent d'une des
    deux lectures (démarré ou arrêté entre-temps) est ignoré."""
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    first, t0 = read_times(), clock()
    sleep(seconds)
    second, t1 = read_times(), clock()
    elapsed = max(t1 - t0, 1e-6)
    return {k: round(max(second[k] - first[k], 0.0) / elapsed, 3)
            for k in second if k in first}
