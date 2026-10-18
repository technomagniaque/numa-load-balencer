# numa-place — placement NUMA automatique (Proxmox, XCP-ng, Xen + libvirt)

Hôte cible : EPYC 7551P (4 nœuds de 8 cœurs / 16 threads), P40 et Chelsio T6225-CR en passthrough.

Place les vCPU **et** la mémoire de chaque VM/CT sur le(s) même(s) nœud(s) NUMA :

- invité avec passthrough (GPU, VF SR-IOV) → nœud de la carte ;
- invité sans passthrough → nœud le moins chargé (RAM + vCPU), puis conservé ;
- invité plus grand qu'un nœud → plusieurs nœuds **contigus**, alignés (0-1, 2-3) ;
- `--cores-only` : un nœud contient ses cœurs physiques (8), pas ses threads (16) ;
  une VM à 16 vCPU prend alors 2 nœuds contigus ;
- invités épinglés à la main, ou marqués `numa-manual` → non modifiés, mais comptés dans la charge.

| Fichier | Rôle | Python |
|---|---|---|
| `numa_core.py` | calcul du placement, commun aux trois outils | 3.6+ |
| `numa_xen.py` | topologie et nœud des cartes PCI via `xl info -n` | 3.6+ |
| `pve/pve-numa-place` | Proxmox VE 8/9 : KVM (`affinity`, `numaN hostnodes`) et LXC (`cpuset`) | 3.9+ |
| `xcp-ng/xcpng-numa-place` | XCP-ng 8.3 : `VCPUs-params:mask` + tag `numa-auto` | 3.6+ |
| `xen-libvirt/xen-numa-place` | Debian 13 dom0 + libvirt (libxl) : `<vcpu cpuset>` + `<metadata>` | 3.6+ |

## Installation (sur l'hôte, en root)

Copier le dossier `numa/` sur l'hôte, puis, selon la plateforme (`pve`, `xcp-ng` ou `xen-libvirt`) :

```bash
install -d /usr/local/lib/numa-place && install -m644 numa_core.py numa_xen.py /usr/local/lib/numa-place/
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
- Xen : le dom0 PV ne voit pas NUMA (`numa_node = -1`) ; le nœud des cartes vient de `xl info -n`.

## Tests

```bash
python3 -m unittest discover -s numa/tests -v
```
