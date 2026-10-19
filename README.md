# numa-load-balencer — placement et équilibrage NUMA des VM (Proxmox, XCP-ng, Xen + libvirt)

Développé et testé sur : EPYC 7551P (4 nœuds de 8 cœurs / 16 threads), P40 et Chelsio T6225-CR en passthrough.
Sur `xcp-ng-epyc` (XCP-ng 8.3, Xen 4.17, Python 3.6.8), le SMT est désactivé : 8 pCPU par nœud,
la P40 (`0000:61:00.0`) est sur le nœud 3. Sortie réelle de `xl info -n` : `tests/fixtures/`.

Place les vCPU **et** la mémoire de chaque VM/CT sur le(s) même(s) nœud(s) NUMA :

- invité avec passthrough (GPU, VF SR-IOV) → nœud de la carte ;
- invité sans passthrough → nœud le moins chargé (RAM + vCPU), puis conservé ;
- invité plus grand qu'un nœud → plusieurs nœuds **contigus**, alignés (0-1, 2-3) ;
- `--cores-only` : un nœud contient ses cœurs physiques (8), pas ses threads (16) ;
  une VM à 16 vCPU prend alors 2 nœuds contigus ;
- invités épinglés à la main, ou marqués `numa-manual` → non modifiés, mais comptés dans la charge.
- **équilibrage sur la charge réelle** : à chaque passage (toutes les 30 s), la consommation CPU
  de chaque invité est mesurée (`--sample 10` s) et lissée dans `/var/lib/numa-place/*-state.json`.
  Une VM « chaude » (≥ 50 % de ses vCPU et ≥ 2 pCPU) garde ses nœuds ; une petite VM dont les nœuds
  dépassent 85 % est **évacuée à chaud** vers les nœuds les moins chargés, si le gain dépasse 1 pCPU,
  puis ne rebouge pas pendant 10 min. `--sample 0` revient au calcul sur les vCPU nominaux.
- **retour près de la RAM** : sous Xen, l'évacuation ne déplace que les vCPU ; la RAM reste sur le
  nœud d'origine (retenu dans le fichier d'état). Quand ce nœud redescend sous 60 % (seuil plus bas
  que les 85 % d'évacuation), la VM y revient. Si elle a redémarré entre-temps (domid ou PID
  différent), sa RAM a été réallouée et l'origine est oubliée.
- **ballooning** : seule la mémoire garantie compte (`memory-dynamic-min`, `<currentMemory>`,
  `balloon:`) ; une VM à ballooning n'est jamais liée strictement (`policy=preferred`).

| Fichier | Rôle | Python |
|---|---|---|
| `numa_core.py` | calcul du placement, commun aux trois outils | 3.6+ |
| `numa_xen.py` | topologie et nœud des cartes PCI via `xl info -n` | 3.6+ |
| `numa_state.py` | consommation lissée et délai après déplacement (fichier d'état) | 3.6+ |
| `pve/pve-numa-place` | Proxmox VE 8/9 : KVM (`affinity`, `numaN hostnodes`) et LXC (`cpuset`) | 3.9+ |
| `xcp-ng/xcpng-numa-place` | XCP-ng 8.3 : `VCPUs-params:mask` + tag `numa-auto` | 3.6+ |
| `xen-libvirt/xen-numa-place` | Debian 13 dom0 + libvirt (libxl) : `<vcpu cpuset>` + `<metadata>` | 3.6+ |

## Paquets .deb (Debian 13 et Proxmox VE)

| Paquet | Pour | Contenu |
|---|---|---|
| `numa-place-common` | tous | `numa_core.py`, `numa_state.py` dans `/usr/lib/numa-place` |
| `pve-numa-place` | Proxmox VE 8/9 | outil KVM + LXC, `/usr/sbin/pve-numa-place`, service + timer |
| `xen-numa-place` | Debian 13 dom0 Xen + libvirt | outil Xen, `numa_xen.py`, service + timer |

Les timers sont installés **sans être activés** : relire `… plan`, puis
`systemctl enable --now pve-numa-place.timer pve-numa-place.service` (ou `xen-…`).
XCP-ng (RPM, Python 3.6) garde l'installation manuelle ci-dessous.

```bash
apt install ./numa-place-common_*_all.deb ./pve-numa-place_*_all.deb
```

**Releases GitHub** : pousser un tag `numa-place-vX.Y.Z` lance `.github/workflows/numa-place-deb.yml`
(Debian 13 : tests, construction, `dpkg-deb`/`lintian`, essai d'installation) et publie la
Release `numa-place X.Y.Z` avec les trois `.deb` et `SHA256SUMS`.
Construction locale, sans dpkg (paquets identiques au bit près à ceux de la CI) :

```bash
python3 packaging/build_deb.py --out dist
```

## Installation (sur l'hôte, en root)

Copier le dépôt sur l'hôte, puis, selon la plateforme (`pve`, `xcp-ng` ou `xen-libvirt`) :

```bash
install -d /usr/local/lib/numa-place && install -m644 numa_core.py numa_state.py numa_xen.py /usr/local/lib/numa-place/
```

```bash
install -m755 pve/pve-numa-place /usr/local/lib/numa-place/ && ln -sf /usr/local/lib/numa-place/pve-numa-place /usr/local/sbin/
```

Toujours relire le plan avant d'appliquer :

```bash
pve-numa-place plan
```

```bash
pve-numa-place apply
```

Automatique (au démarrage, avant les invités, puis toutes les 10 min) :

```bash
cp pve/pve-numa-place.service pve/pve-numa-place.timer /etc/systemd/system/ && systemctl enable --now pve-numa-place.timer pve-numa-place.service
```

Même chose avec `xcpng-numa-place` (dossier `xcp-ng/`) ou `xen-numa-place` (dossier `xen-libvirt/`).

## Vérifier

```bash
pve-numa-place status
```

- Proxmox : CPU autorisés (`/proc/PID/status`) et RAM par nœud (`numa_maps`, `memory.numa_stat`).
- Xen / XCP-ng : affinité et RAM par nœud du domaine (`xl debug-keys u` + `xl dmesg`).

## À savoir

- La mémoire n'est placée qu'au **démarrage** de la VM. `--live` déplace les vCPU tout de suite,
  et la RAM des conteneurs LXC (cgroup v2) ; la RAM d'une VM avec passthrough est verrouillée par VFIO.
- Xen / XCP-ng : la mémoire suit l'affinité dure des vCPU (affinité de nœud déduite par Xen).
  Sur XCP-ng, si la politique NUMA de l'hôte est `best_effort`, vérifier avec `status` que la RAM
  est bien sur les nœuds du mask.
- Réserve mémoire : sous Xen, la RAM du dom0 par nœud (cache 1 h) ; sous Proxmox, la RAM de
  l'hôte hors invités (services, ARC ZFS) = utilisée − cache de pages − RAM des invités (cache 5 min).
- Proxmox : les changements en attente (`[PENDING]`, VM en marche) comptent comme placement actuel.
- Xen : le dom0 PV ne voit pas NUMA (`numa_node = -1`) ; le nœud des cartes vient de `xl info -n`.
  Une VF SR-IOV créée après le démarrage n'y figure pas : son nœud est celui de sa PF (lien sysfs `physfn`).
- Chelsio T6 : les VF sont sous les PF 0-3 (`41:00.0`…), pas sous la PF4 qui porte le réseau.
  Un pool libvirt `<forward mode='hostdev'><pf dev=…/>` ne les trouve donc pas : lister les VF
  par `<address type='pci' …/>` dans le pool (lu par `xen-numa-place`).
- Passthrough et SR-IOV (Xen 4.17 à 4.21) : invité HVM ou PV seulement, pas PVH ; dom0 PV obligatoire.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
