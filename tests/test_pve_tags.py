"""« qm set --tags » NORMALISE les tags — garde-fou contre une boucle de qm set.

⚠️ Ce fichier a ete ecrit en croyant avoir trouve un defaut. Il n y en a PAS :
une fois « numa-auto » pose, want et current sont tous deux la liste LUE dans la
configuration, donc ils concordent quel que soit le tri de PVE. Le banc reste
utile — il fige cette propriete, qui n est pas evidente — mais il ne corrige
rien, et c est lui qui l a montre en passant au vert sur les deux versions.

Pour memoire, ce que PVE fait vraiment :

PVE::API2::Qemu (update_vm) passe la valeur par PVE::GuestHelpers::get_unique_tags,
qui TRIE, deduplique et met en MINUSCULES tant que datacenter.cfg ne porte pas
« tag-style: case-sensitive=1 ». Une comparaison faite sur la liste non
normalisee ne correspond donc jamais a ce que la configuration porte : qemu_changes
rend un changement a chaque passage, et le timer relance « qm set » toutes les
30 s sur chaque invite tague, indefiniment.

Le faux « qm set » de ce fichier normalise comme le vrai — c est ce qui permet au
banc de rougir. Celui de test_pve.py ecrit la valeur telle quelle et ne peut donc
pas voir le defaut.

    python3 tests/test_pve_tags.py
"""
import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

NUMA_DIR = Path(__file__).resolve().parent.parent


def load_script(name, path, env):
    os.environ.update(env)
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


def pve_get_unique_tags(tags, datacenter_cfg: Path):
    """Reimplementation INDEPENDANTE de PVE::GuestHelpers::get_unique_tags.

    Elle ne doit JAMAIS appeler le code teste : un faux qui emprunte la fonction
    qu il est cense eprouver passe au vert meme quand cette fonction est cassee.
    """
    txt = datacenter_cfg.read_text() if datacenter_cfg.exists() else ""
    style = {}
    for ligne in txt.splitlines():
        if ligne.startswith("tag-style:"):
            for kv in ligne.split(":", 1)[1].replace(",", " ").split():
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    style[k] = v
    sensible = style.get("case-sensitive") in ("1", "true")
    items = list(tags)
    if style.get("ordering") != "config":
        items.sort(key=lambda t: t if sensible else t.lower())
    out, vus = [], set()
    for t in items:
        t = t if sensible else t.lower()
        if t in vus:
            continue
        vus.add(t)
        out.append(t)
    return out


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class TagsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for n in range(4):
            d = self.root / f"sys/devices/system/node/node{n}"
            write(d / "cpulist", f"{n*8}-{n*8+7}\n")
            write(d / "meminfo", f"Node {n} MemTotal:       {32 * 1024 * 1024} kB\n")
        for c in range(32):
            topo = self.root / f"sys/devices/system/cpu/cpu{c}/topology"
            write(topo / "physical_package_id", "0\n")
            write(topo / "core_id", f"{c}\n")
        self.m = load_script("pnp_tags", NUMA_DIR / "pve/pve-numa-place", dict(
            PNP_SYSFS=str(self.root / "sys"), PNP_PROC=str(self.root / "proc"),
            PNP_PVE_DIR=str(self.root / "pve"), PNP_RUN_DIR=str(self.root / "run"),
            PNP_NODENAME="pve1"))

    def tearDown(self):
        self.tmp.cleanup()

    def style(self, ligne: str | None) -> None:
        write(self.root / "pve/datacenter.cfg", (f"tag-style: {ligne}\n" if ligne else "keyboard: fr\n"))

    def vm(self, vmid: int, body: str) -> Path:
        p = self.root / f"pve/qemu-server/{vmid}.conf"
        write(p, body)
        return p

    def qm_pve(self, cmd):
        """Faux « qm set » QUI NORMALISE, comme PVE::API2::Qemu le fait."""
        assert cmd[:2] == ["qm", "set"], cmd
        path = self.root / f"pve/qemu-server/{cmd[2]}.conf"
        main, _, rest = path.read_text().partition("\n[")
        lines = main.splitlines()
        for flag, val in zip(cmd[3::2], cmd[4::2]):
            keys = val.split(",") if flag == "--delete" else [flag[2:]]
            lines = [l for l in lines if l.split(":", 1)[0] not in keys]
            if flag == "--delete":
                continue
            if flag == "--tags":
                val = ";".join(pve_get_unique_tags(
                    [t for t in val.replace(",", ";").split(";") if t],
                    (self.root / "pve/datacenter.cfg")))
            lines.append(f"{flag[2:]}: {val}")
        path.write_text("\n".join(lines) + "\n" + (f"\n[{rest}" if rest else ""))
        return True

    def passe(self):
        nodes = self.m.read_topology()
        pairs = self.m.build_plan(nodes, self.m.load_guests())
        touches = []
        for g, p in pairs:
            if not p.nodes:
                continue
            chg = self.m.qemu_changes(p, g)
            if chg:
                touches.append((g.vmid, chg))
                self.m._write_conf(p, g, self.qm_pve)
        return touches

    # -------------------------------------------------------------- le defaut
    def test_pas_de_boucle_sur_un_invite_tague(self):
        self.style(None)
        self.vm(100, "cores: 2\nmemory: 2048\ntags: web\n")
        self.assertTrue(self.passe(), "le premier passage doit poser affinity/numa/tags")
        self.assertEqual(self.passe(), [], "le SECOND passage ne doit plus rien changer")
        self.assertEqual(self.passe(), [], "ni le troisieme")
        self.assertIn("tags: numa-auto;web", (self.root / "pve/qemu-server/100.conf").read_text())

    def test_pas_de_boucle_avec_une_majuscule(self):
        self.style(None)
        self.vm(101, "cores: 2\nmemory: 2048\ntags: Prod\n")
        self.assertTrue(self.passe())
        self.assertEqual(self.passe(), [], "une majuscule ne doit pas relancer qm set")
        self.assertIn("tags: numa-auto;prod", (self.root / "pve/qemu-server/101.conf").read_text())

    def test_pas_de_boucle_sans_aucun_tag(self):
        self.style(None)
        self.vm(102, "cores: 1\nmemory: 1024\n")
        self.assertTrue(self.passe())
        self.assertEqual(self.passe(), [])

    # -------------------------------------------------- les deux reglages PVE
    def test_case_sensitive_garde_la_casse(self):
        self.style("case-sensitive=1")
        self.vm(103, "cores: 2\nmemory: 2048\ntags: Prod\n")
        self.assertTrue(self.passe())
        self.assertEqual(self.passe(), [])
        self.assertIn("tags: Prod;numa-auto", (self.root / "pve/qemu-server/103.conf").read_text())

    def test_ordering_config_garde_l_ordre(self):
        self.style("ordering=config")
        self.vm(104, "cores: 2\nmemory: 2048\ntags: web;api\n")
        self.assertTrue(self.passe())
        self.assertEqual(self.passe(), [])
        self.assertIn("tags: web;api;numa-auto", (self.root / "pve/qemu-server/104.conf").read_text())

if __name__ == "__main__":
    unittest.main()
