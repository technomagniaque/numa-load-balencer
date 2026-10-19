#!/usr/bin/env python3
"""build_deb.py — construit les paquets .deb de numa-place sans dpkg ni debhelper.

    python3 packaging/build_deb.py --version 0.2.0 --out dist/

Produit (Architecture: all, Debian 13 / Proxmox VE 8-9) :
  numa-place-common  numa_core, numa_state, numa_linux, numa_libvirt (/usr/lib/numa-place)
  pve-numa-place     outil Proxmox (KVM + LXC) + timer systemd
  xen-numa-place     outil Xen + libvirt (dom0 Debian 13) + numa_xen.py + timer
  kvm-numa-place     outil KVM + libvirt (virt-manager, Debian/Ubuntu) + timer

Un .deb est une archive « ar » : debian-binary, control.tar.gz, data.tar.gz.
Ce script l'écrit lui-même, avec des fichiers root:root et des dates fixes
(SOURCE_DATE_EPOCH). Le gzip est écrit à la main en blocs « stockés » : la sortie
d'un vrai compresseur dépend de la bibliothèque (zlib, zlib-ng…), celle-ci non.
Même commit -> mêmes .deb au bit près, sur toute machine (vérifié CI / CachyOS).
Les timers sont installés mais PAS activés : appliquer un placement modifie
les VM, c'est à l'administrateur de le décider (systemctl enable --now …).
"""
import argparse
import hashlib
import io
import os
import struct
import subprocess
import sys
import tarfile
import time
import zlib
from typing import Dict, List, NamedTuple, Tuple

NUMA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# dépôt autonome (code à la racine) ou ancien emplacement numa/ dans un autre dépôt
REPO = NUMA if os.path.exists(os.path.join(NUMA, "LICENSE")) else os.path.dirname(NUMA)
MAINTAINER = "technomagniaque <38930713+technomagniaque@users.noreply.github.com>"
HOMEPAGE = "https://github.com/technomagniaque/numa-load-balencer"
LIB = "usr/lib/numa-place"
COMMON_MODULES = ("numa_core.py", "numa_state.py", "numa_linux.py", "numa_libvirt.py")
UNITS = "usr/lib/systemd/system"


class File(NamedTuple):
    path: str        # chemin dans le paquet, sans « / » initial
    data: bytes
    mode: int = 0o644
    link: str = ""   # cible si lien symbolique


class Package(NamedTuple):
    name: str
    depends: str
    recommends: str
    description: str
    files: Tuple[File, ...]
    scripts: Dict[str, str]   # postinst, prerm, postrm


def read(*parts: str) -> bytes:
    with open(os.path.join(NUMA, *parts), "rb") as fh:
        return fh.read()


def unit(*parts: str) -> bytes:
    """Les unités du dépôt visent /usr/local/sbin (installation manuelle) : paquet -> /usr/sbin."""
    return read(*parts).replace(b"/usr/local/sbin/", b"/usr/sbin/")


def doc(name: str) -> Tuple[File, ...]:
    with open(os.path.join(REPO, "LICENSE"), "rb") as fh:
        licence = fh.read()
    copyright_ = (b"Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
                  b"Upstream-Name: numa-load-balencer\nSource: " + HOMEPAGE.encode() + b"\n\n"
                  b"Files: *\nCopyright: 2025-2026 technomagniaque\nLicense: BSD-2-Clause\n " +
                  b"\n ".join(line or b"." for line in licence.strip().splitlines()) + b"\n")
    return (File("usr/share/doc/{}/copyright".format(name), copyright_),
            File("usr/share/doc/{}/README.md".format(name), read("README.md")))


def tool_scripts(tool: str) -> Dict[str, str]:
    timer = tool + ".timer"
    return {
        "postinst": """#!/bin/sh
set -e
if [ "$1" = configure ]; then
    mkdir -p /var/lib/numa-place
    if [ -d /run/systemd/system ]; then
        systemctl daemon-reload || true
        # mise à jour : relancer le timer s'il était déjà actif, sans jamais l'activer
        systemctl try-restart {timer} >/dev/null 2>&1 || true
    fi
    if [ -z "$2" ]; then
        echo "{tool} installé. Relire le plan, puis activer l'équilibrage automatique :"
        echo "  {tool} plan"
        echo "  systemctl enable --now {timer} {tool}.service"
    fi
fi
exit 0
""".format(tool=tool, timer=timer),
        "prerm": """#!/bin/sh
set -e
if [ "$1" = remove ] && [ -d /run/systemd/system ]; then
    systemctl disable --now {timer} {tool}.service >/dev/null 2>&1 || true
fi
exit 0
""".format(tool=tool, timer=timer),
        "postrm": """#!/bin/sh
set -e
if [ -d /run/systemd/system ]; then
    systemctl daemon-reload || true
fi
if [ "$1" = purge ]; then
    rm -f /var/lib/numa-place/{state}
    rmdir /var/lib/numa-place 2>/dev/null || true
fi
exit 0
""".format(state=tool.split("-")[0] + "-state.json"),
    }


def packages() -> List[Package]:
    common = Package(
        "numa-place-common", "python3 (>= 3.6)", "",
        "placement NUMA des VM — calcul commun\n"
        " Calcul du placement NUMA (nœuds contigus, passthrough PCI, ballooning,\n"
        " équilibrage sur la charge CPU réelle) partagé par pve-numa-place et\n"
        " xen-numa-place.",
        tuple(File(LIB + "/" + m, read(m)) for m in COMMON_MODULES) + doc("numa-place-common"),
        {})
    pve = Package(
        "pve-numa-place", "numa-place-common (= {v}), python3 (>= 3.9)",  # taskset : util-linux, essentiel
        "numactl",
        "placement NUMA automatique des VM KVM et conteneurs LXC Proxmox VE\n"
        " Place vCPU et mémoire des invités Proxmox sur le(s) même(s) nœud(s) NUMA\n"
        " (affinity + numaN hostnodes pour KVM, cpuset pour LXC), en tenant compte\n"
        " des cartes en passthrough, et évacue à chaud les petites VM des nœuds\n"
        " saturés. Timer systemd fourni, non activé à l'installation.",
        (File(LIB + "/pve-numa-place", read("pve", "pve-numa-place"), 0o755),
         File("usr/sbin/pve-numa-place", b"", 0o777, "../lib/numa-place/pve-numa-place"),
         File(UNITS + "/pve-numa-place.service", unit("pve", "pve-numa-place.service")),
         File(UNITS + "/pve-numa-place.timer", unit("pve", "pve-numa-place.timer")))
        + doc("pve-numa-place"),
        tool_scripts("pve-numa-place"))
    xen = Package(
        "xen-numa-place", "numa-place-common (= {v}), python3 (>= 3.6), libvirt-clients, "
        "xen-utils-common", "",
        "placement NUMA automatique des VM Xen gérées par libvirt (dom0 Debian)\n"
        " Place vCPU et mémoire des VM Xen (libxl) sur le(s) même(s) nœud(s) NUMA via\n"
        " <vcpu cpuset>, lit la topologie et le nœud des cartes PCI et des VF SR-IOV\n"
        " avec « xl info -n », et évacue à chaud les petites VM des nœuds saturés.\n"
        " Timer systemd fourni, non activé à l'installation.",
        (File(LIB + "/xen-numa-place", read("xen-libvirt", "xen-numa-place"), 0o755),
         File(LIB + "/numa_xen.py", read("numa_xen.py")),
         File("usr/sbin/xen-numa-place", b"", 0o777, "../lib/numa-place/xen-numa-place"),
         File(UNITS + "/xen-numa-place.service", unit("xen-libvirt", "xen-numa-place.service")),
         File(UNITS + "/xen-numa-place.timer", unit("xen-libvirt", "xen-numa-place.timer")))
        + doc("xen-numa-place"),
        tool_scripts("xen-numa-place"))
    kvm = Package(
        "kvm-numa-place", "numa-place-common (= {v}), python3 (>= 3.6), libvirt-clients", "",
        "placement NUMA automatique des VM KVM gérées par libvirt (virt-manager)\n"
        " Place vCPU et mémoire des VM QEMU/KVM sur le(s) même(s) nœud(s) NUMA via\n"
        " <vcpu cpuset> et <numatune> (visibles dans virt-manager), garde les VM avec\n"
        " passthrough sur le nœud de leur carte, ne donne jamais deux threads d'un même\n"
        " cœur à une VM, et évacue à chaud les petites VM des nœuds saturés.\n"
        " Timer systemd fourni, non activé à l'installation.",
        (File(LIB + "/kvm-numa-place", read("kvm", "kvm-numa-place"), 0o755),
         File("usr/sbin/kvm-numa-place", b"", 0o777, "../lib/numa-place/kvm-numa-place"),
         File(UNITS + "/kvm-numa-place.service", unit("kvm", "kvm-numa-place.service")),
         File(UNITS + "/kvm-numa-place.timer", unit("kvm", "kvm-numa-place.timer")))
        + doc("kvm-numa-place"),
        tool_scripts("kvm-numa-place"))
    return [common, pve, xen, kvm]


# ------------------------------------------------------------------ archives

def _tar(entries: List[Tuple[tarfile.TarInfo, bytes]], mtime: int) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for info, data in entries:
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = mtime
            tar.addfile(info, io.BytesIO(data) if info.isreg() else None)
    return raw.getvalue()


def stored_gzip(data: bytes, mtime: int) -> bytes:
    """gzip sans compression (blocs deflate « stockés ») : octets identiques quelle que
    soit la bibliothèque zlib. Sert aux tar du paquet et au changelog."""
    out = [b"\x1f\x8b\x08\x00" + struct.pack("<I", mtime) + b"\x00\x03"]
    chunks = [data[i:i + 65535] for i in range(0, len(data), 65535)] or [b""]
    for i, chunk in enumerate(chunks):
        final = 1 if i == len(chunks) - 1 else 0
        out.append(bytes([final]) + struct.pack("<HH", len(chunk), len(chunk) ^ 0xFFFF) + chunk)
    out.append(struct.pack("<II", zlib.crc32(data) & 0xFFFFFFFF, len(data) & 0xFFFFFFFF))
    return b"".join(out)


def changelog(pkg: Package, version: str, mtime: int) -> File:
    date = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(mtime))
    text = ("{name} ({v}) unstable; urgency=medium\n\n"
            "  * Release {v}, see the GitHub releases page.\n\n"
            " -- {who}  {date}\n").format(name=pkg.name, v=version, home=HOMEPAGE,
                                            who=MAINTAINER, date=date)
    # paquet « natif » (version sans révision Debian) : changelog.gz, pas changelog.Debian.gz
    return File("usr/share/doc/{}/changelog.gz".format(pkg.name),
                stored_gzip(text.encode(), mtime))


def _dirs(paths: List[str]) -> List[str]:
    seen = set()
    for path in paths:
        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            seen.add("/".join(parts[:i]))
    return sorted(seen)


def data_tar(pkg: Package, mtime: int) -> bytes:
    entries = []
    for d in _dirs([f.path for f in pkg.files]):
        info = tarfile.TarInfo("./" + d + "/")
        info.type, info.mode = tarfile.DIRTYPE, 0o755
        entries.append((info, b""))
    for f in sorted(pkg.files, key=lambda f: f.path):
        info = tarfile.TarInfo("./" + f.path)
        if f.link:
            info.type, info.linkname, info.mode = tarfile.SYMTYPE, f.link, 0o777
            entries.append((info, b""))
        else:
            info.size, info.mode = len(f.data), f.mode
            entries.append((info, f.data))
    return _tar(entries, mtime)


def control_tar(pkg: Package, version: str, mtime: int) -> bytes:
    size_kb = (sum(len(f.data) for f in pkg.files) + 1023) // 1024
    fields = [("Package", pkg.name), ("Version", version), ("Architecture", "all"),
              ("Maintainer", MAINTAINER), ("Installed-Size", str(size_kb)),
              ("Depends", pkg.depends.format(v=version)), ("Recommends", pkg.recommends),
              ("Section", "admin"), ("Priority", "optional"), ("Homepage", HOMEPAGE),
              ("Description", pkg.description)]
    control = "".join("{}: {}\n".format(k, v) for k, v in fields if v).encode()
    sums = "".join("{}  {}\n".format(hashlib.md5(f.data).hexdigest(), f.path)
                   for f in sorted(pkg.files, key=lambda f: f.path) if not f.link).encode()
    entries = []
    for name, data, mode in ([("control", control, 0o644), ("md5sums", sums, 0o644)]
                             + [(n, s.encode(), 0o755) for n, s in sorted(pkg.scripts.items())]):
        info = tarfile.TarInfo("./" + name)
        info.size, info.mode = len(data), mode
        entries.append((info, data))
    return _tar(entries, mtime)


def ar_archive(members: List[Tuple[str, bytes]], mtime: int) -> bytes:
    """Format ar commun, comme dpkg-deb : en-têtes de 60 octets, membres alignés sur 2."""
    out = [b"!<arch>\n"]
    for name, data in members:
        header = "{:<16}{:<12}{:<6}{:<6}{:<8}{:<10}`\n".format(
            name, mtime, 0, 0, "100644", len(data)).encode()
        assert len(header) == 60
        out += [header, data, b"\n" if len(data) % 2 else b""]
    return b"".join(out)


def build(pkg: Package, version: str, out_dir: str, mtime: int) -> str:
    pkg = pkg._replace(files=pkg.files + (changelog(pkg, version, mtime),))
    deb = ar_archive([("debian-binary", b"2.0\n"),
                      ("control.tar.gz", stored_gzip(control_tar(pkg, version, mtime), mtime)),
                      ("data.tar.gz", stored_gzip(data_tar(pkg, mtime), mtime))], mtime)
    path = os.path.join(out_dir, "{}_{}_all.deb".format(pkg.name, version))
    with open(path, "wb") as fh:
        fh.write(deb)
    return path


def git_version() -> str:
    """numa-place-v0.2.0 -> 0.2.0 ; entre deux tags : 0.2.0+3.gabc1234."""
    try:
        desc = subprocess.check_output(
            ["git", "-C", REPO, "describe", "--tags", "--match", "numa-place-v*"],
            universal_newlines=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "0.0.0"
    parts = desc[len("numa-place-v"):].split("-")
    return parts[0] if len(parts) == 1 else "{}+{}.{}".format(parts[0], parts[1], parts[2])


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(description="Construit les .deb de numa-place")
    ap.add_argument("--version", default="", help="défaut : déduite du dernier tag numa-place-v*")
    ap.add_argument("--out", default=os.path.join(REPO, "dist"))
    args = ap.parse_args(argv)
    version = args.version or git_version()
    mtime = int(os.environ.get("SOURCE_DATE_EPOCH", "1700000000"))
    os.makedirs(args.out, exist_ok=True)
    for pkg in packages():
        print(build(pkg, version, args.out, mtime))
    return 0


if __name__ == "__main__":
    sys.exit(main())
