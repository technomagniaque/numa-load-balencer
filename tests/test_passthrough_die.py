"""Un invite a carte PCIe ne quitte son die que sous charge extreme.

La regle, telle que le proprietaire du lab l a fixee :

  - il ne quitte JAMAIS le die ou le lien PCIe est attache, sauf si ce die est a
    au moins PCI_EXTREME de ses coeurs a 100 % — 6 sur 8 ici ;
  - le repli est a PCI_HOPS = 1 saut NUMA au plus. Sur un bi-socket, « 1 saut »
    exclut l autre socket : c est la matrice de distances du noyau qui tranche,
    pas l adjacence des numeros de noeud ;
  - il revient des que le die retombe sous PCI_RETURN = 70 %. Entre 70 et 75 %
    il reste ou il est : sans cette bande, une charge qui oscille autour du seuil
    lui ferait faire l aller-retour, et chaque aller-retour perd sa localite.

Et pour un invite SANS carte, le seuil de retour vers la RAM depend du die vise :
60 % pour un die ordinaire, 50 % pour un die qui porte la carte d un autre — on
n y revient que s il est nettement libre, pour laisser la place au lien.

Lire la memoire d un autre die coute 16 contre 10 sur l EPYC 7551P.

    python3 tests/test_passthrough_die.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numa_core as core  # noqa: E402

Node, Request = core.Node, core.Request
CORES = 8


def hote(n: int = 4, mem_mb: int = 32768):
    """4 dies de 8 coeurs, matrice uniforme comme l EPYC 7551P (10 local, 16 ailleurs)."""
    return [Node(i, tuple(range(i * CORES, (i + 1) * CORES)), CORES, mem_mb, (),
                 tuple(10 if i == k else 16 for k in range(n))) for i in range(n)]


def bisocket():
    """2 sockets de 2 dies : 10 local, 16 dans le socket, 32 a l autre socket."""
    return [Node(i, tuple(range(i * CORES, (i + 1) * CORES)), CORES, 32768, (),
                 tuple(10 if i == k else (16 if i // 2 == k // 2 else 32) for k in range(4)))
            for i in range(4)]


def carte(key="132", die=2, vcpus=4, mem=8192, current=None, usage=0.3, cooldown=False):
    return Request(key, vcpus, mem, (die,), tuple(current if current is not None else (die,)),
                   True, False, usage=usage, cooldown=cooldown, passthrough=True)


def voisine(key, die, usage, vcpus=CORES, mem=4096):
    """Invite sans carte, deja sur `die`, qui y consomme `usage` pCPU."""
    return Request(key, vcpus, mem, (), (die,), True, False, usage=usage)


class DieTest(unittest.TestCase):
    def place(self, reqs, nodes=None, **kw):
        p = core.plan(nodes or hote(), reqs, **kw)
        return {r.key: p[r.key] for r in reqs}

    # ---------------------------------------------------- le die est connu
    def test_le_die_de_la_carte_est_pris_sur_chaque_die(self):
        for die in range(4):
            got = self.place([carte(die=die, current=())])
            self.assertIn(die, got["132"].node_ids, f"carte sur le die {die}")

    def test_charge_moderee_ne_fait_pas_sortir(self):
        # 5 coeurs sur 8 : au-dela du seuil d evacuation ordinaire (85 % d un
        # noeud pour une petite VM) mais PAS de la charge extreme.
        for rebalance in (False, True):
            got = self.place([carte(), voisine("gros", 2, 5.0)], rebalance=rebalance)
            self.assertEqual(got["132"].node_ids, (2,), got["132"].reason)

    def test_le_seuil_de_sortie_est_6_coeurs_sur_8(self):
        for usage, reste in ((5.0, True), (5.9, True), (6.0, False), (7.9, False)):
            got = self.place([carte(), voisine("gros", 2, usage)])
            ids = got["132"].node_ids
            if reste:
                self.assertEqual(ids, (2,), f"{usage} coeurs : pas extreme")
            else:
                self.assertNotIn(2, ids, f"{usage} coeurs : sortie permise")
                self.assertTrue(all(core.hops(2, n, hote()) <= core.PCI_HOPS for n in ids), ids)

    def test_un_invite_ne_provoque_pas_sa_propre_expulsion(self):
        # a lui seul il occupe tout le die : ca ne doit PAS le faire partir
        got = self.place([carte(usage=8.0)])
        self.assertEqual(got["132"].node_ids, (2,), got["132"].reason)

    def test_jamais_deux_sauts_meme_satures(self):
        # die 2 extreme ET die 3 (son seul voisin a 1 saut) extreme aussi :
        # il n y a nulle part de sain a 1 saut, il RESTE sur le die de la carte.
        got = self.place([carte(), voisine("a", 2, 7.0), voisine("b", 3, 7.0)],
                         nodes=bisocket())
        self.assertEqual(got["132"].node_ids, (2,), got["132"].reason)

    def test_bisocket_le_repli_reste_dans_le_socket(self):
        got = self.place([carte(), voisine("gros", 2, 7.0)], nodes=bisocket())
        ids = got["132"].node_ids
        self.assertNotIn(2, ids)
        for n in ids:
            self.assertEqual(core.hops(2, n, bisocket()), 1, f"{n} est a 2 sauts")

    # ------------------------------------------- hysteresis : sortie 75, retour 70
    def test_entre_70_et_75_il_reste_ou_il_est(self):
        for usage in (5.6, 5.9):   # 70 % et 73,75 % de 8 coeurs
            got = self.place([carte(current=(1,)), voisine("gros", 2, usage)])
            self.assertEqual(got["132"].node_ids, (1,),
                             f"{usage} coeurs : pas de retour avant 70 % — {got['132'].reason}")

    def test_sous_70_il_revient_sur_le_die_de_la_carte(self):
        got = self.place([carte(current=(1,)), voisine("gros", 2, 5.5)])  # 68,75 %
        self.assertEqual(got["132"].node_ids, (2,), got["132"].reason)
        self.assertIn("passthrough", got["132"].reason)

    def test_un_deplacement_recent_ne_se_rejoue_pas(self):
        got = self.place([carte(current=(1,), cooldown=True), voisine("gros", 2, 0.1)])
        self.assertEqual(got["132"].node_ids, (1,), got["132"].reason)
        self.assertIn("récemment", got["132"].reason)

    # ------------------------- seuils de retour d un invite SANS carte
    def test_seuils_de_retour_sans_carte(self):
        self.assertEqual(core.RETURN_LOAD, 0.60)
        self.assertEqual(core.RETURN_LOAD_PCI_DIE, 0.50)
        ctx = core._Ctx(hote(), False, False, None, {}, {2})
        self.assertEqual(ctx.return_load([0]), 0.60, "die sans carte")
        self.assertEqual(ctx.return_load([2]), 0.50, "die qui porte une carte")
        self.assertEqual(ctx.return_load([1, 2]), 0.50, "un seul die a carte suffit")

    # ------------------------------ le die est INCONNU (numa_node = -1)
    def test_die_inconnu_et_deja_epingle_reste_fige(self):
        fige = Request("200", 4, 8192, (), (1,), True, False, passthrough=True)
        got = self.place([fige])
        self.assertEqual(got["200"].node_ids, (1,), got["200"].reason)
        self.assertIn("inconnu", got["200"].reason)

    def test_die_inconnu_et_non_epingle_n_est_PAS_place(self):
        libre = Request("201", 4, 8192, (), (), True, False, passthrough=True)
        got = self.place([libre])
        self.assertEqual(got["201"].node_ids, ())
        self.assertEqual(got["201"].policy, "")
        self.assertIn("inconnu", got["201"].reason)

    def test_un_invite_sans_carte_reste_place_normalement(self):
        got = self.place([Request("300", 4, 4096, (), (), True, False)])
        self.assertTrue(got["300"].node_ids)
        self.assertIn("équilibrage", got["300"].reason)


if __name__ == "__main__":
    unittest.main()
