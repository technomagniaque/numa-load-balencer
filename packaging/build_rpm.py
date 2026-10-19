#!/usr/bin/env python3
"""build_rpm.py — construit les RPM de numa-place avec rpmbuild.

    python3 packaging/build_rpm.py --target el    --version 0.3.0 --out dist/   # Rocky/Alma/RHEL 9-10 (KVM)
    python3 packaging/build_rpm.py --target xcpng --version 0.3.0 --out dist/   # XCP-ng 8.3

Cibles :
  el     kvm-numa-place     KVM + libvirt (virt-manager, Cockpit) ; Requires libvirt-client
  xcpng  xcpng-numa-place   XCP-ng 8.3 (base CentOS 7, rpm 4.11, Python 3.6) : charge
                            gzip et empreintes SHA-256, que rpm 4.11 sait lire, même
                            si le paquet est construit sur une Rocky récente.

Un seul RPM par cible (bibliothèques communes incluses dans /usr/lib/numa-place).
Le timer est installé mais PAS activé : c'est à l'administrateur de le décider.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = "https://github.com/technomagniaque/numa-load-balencer"
PACKAGER = "technomagniaque <38930713+technomagniaque@users.noreply.github.com>"

TARGETS = {
    "el": {
        "name": "kvm-numa-place",
        "tool": "kvm/kvm-numa-place",
        "units": ("kvm/kvm-numa-place.service", "kvm/kvm-numa-place.timer"),
        "modules": ("numa_core.py", "numa_state.py", "numa_linux.py", "numa_libvirt.py"),
        "requires": ("python3 >= 3.6", "libvirt-client", "systemd"),
        "release": "1%{?dist}",
        "summary": "Placement NUMA automatique des VM KVM gérées par libvirt",
        "extra_macros": "",
    },
    "xcpng": {
        "name": "xcpng-numa-place",
        "tool": "xcp-ng/xcpng-numa-place",
        "units": ("xcp-ng/xcpng-numa-place.service", "xcp-ng/xcpng-numa-place.timer"),
        "modules": ("numa_core.py", "numa_state.py", "numa_linux.py", "numa_xen.py"),
        "requires": ("python3 >= 3.6", "xen-dom0-tools", "xapi-xe", "systemd"),  # xl, xe
        "release": "1.xcpng8.3",
        # rpm 4.11 (CentOS 7) : pas de zstd, empreintes SHA-256 (algorithme 8)
        "extra_macros": ("%define _binary_payload w9.gzdio\n"
                         "%define _binary_filedigest_algorithm 8\n"),
        "summary": "Placement NUMA automatique des VM XCP-ng",
    },
}

SPEC = """\
%global debug_package %{{nil}}
%global __os_install_post %{{nil}}
%global __brp_mangle_shebangs %{{nil}}
{extra_macros}
Name:           {name}
Version:        {version}
Release:        {release}
Summary:        {summary}
License:        BSD-2-Clause
URL:            {url}
Packager:       {packager}
BuildArch:      noarch
{requires}

%description
Place vCPU et mémoire des VM sur le(s) même(s) nœud(s) NUMA : nœuds contigus,
VM avec passthrough PCI/SR-IOV figées sur le nœud de leur carte, ballooning,
jamais deux threads d'un même cœur dans une VM, évacuation à chaud des petites
VM des nœuds saturés et retour près de leur RAM. Timer systemd fourni, non activé.

%install
rm -rf %{{buildroot}}
install -d %{{buildroot}}/usr/lib/numa-place %{{buildroot}}/usr/sbin %{{buildroot}}/usr/lib/systemd/system \\
           %{{buildroot}}%{{_sharedstatedir}}/numa-place
{install_modules}
install -m 0755 {src}/{tool} %{{buildroot}}/usr/lib/numa-place/{name}
ln -s ../lib/numa-place/{name} %{{buildroot}}/usr/sbin/{name}
{install_units}
install -D -m 0644 {src}/README.md %{{buildroot}}%{{_docdir}}/{name}/README.md
install -D -m 0644 {src}/LICENSE %{{buildroot}}%{{_docdir}}/{name}/LICENSE

%files
/usr/lib/numa-place
/usr/sbin/{name}
/usr/lib/systemd/system/{name}.service
/usr/lib/systemd/system/{name}.timer
%dir %{{_sharedstatedir}}/numa-place
%doc %{{_docdir}}/{name}/README.md
%license %{{_docdir}}/{name}/LICENSE

%post
systemctl daemon-reload >/dev/null 2>&1 || :
if [ $1 -eq 1 ]; then
    echo "{name} installé. Relire le plan, puis activer l'équilibrage automatique :"
    echo "  {name} plan"
    echo "  systemctl enable --now {name}.timer {name}.service"
else
    systemctl try-restart {name}.timer >/dev/null 2>&1 || :
fi

%preun
if [ $1 -eq 0 ]; then
    systemctl disable --now {name}.timer {name}.service >/dev/null 2>&1 || :
fi

%postun
systemctl daemon-reload >/dev/null 2>&1 || :

%changelog
* {date} {packager} - {version}-{release_plain}
- Release {version} : {url}/releases
"""


def render_spec(target: str, version: str, src: str, mtime: int) -> str:
    cfg = TARGETS[target]
    name = cfg["name"]
    install_modules = "\n".join(
        "install -m 0644 {src}/{m} %{{buildroot}}/usr/lib/numa-place/{m}".format(src=src, m=m)
        for m in cfg["modules"])
    # unités du dépôt : /usr/local/sbin (installation manuelle) -> /usr/sbin dans le paquet
    install_units = "\n".join(
        "sed 's#/usr/local/sbin/#/usr/sbin/#' {src}/{u} > %{{buildroot}}/usr/lib/systemd/system/{base}".format(
            src=src, u=u, base=os.path.basename(u)) for u in cfg["units"])
    return SPEC.format(
        name=name, version=version, release=cfg["release"],
        release_plain=cfg["release"].replace("%{?dist}", ""),
        summary=cfg["summary"], url=URL, packager=PACKAGER, src=src, tool=cfg["tool"],
        requires="\n".join("Requires:       " + r for r in cfg["requires"]),
        extra_macros=cfg["extra_macros"], install_modules=install_modules,
        install_units=install_units,
        date=time.strftime("%a %b %d %Y", time.gmtime(mtime)))


def build(target: str, version: str, out_dir: str, mtime: int) -> List[str]:
    if shutil.which("rpmbuild") is None:
        sys.exit("rpmbuild introuvable (dnf install rpm-build)")
    top = tempfile.mkdtemp(prefix="numa-rpm-")
    try:
        spec = os.path.join(top, TARGETS[target]["name"] + ".spec")
        with open(spec, "w") as fh:
            fh.write(render_spec(target, version, ROOT, mtime))
        env = dict(os.environ, SOURCE_DATE_EPOCH=str(mtime))
        subprocess.check_call(["rpmbuild", "-bb", "--define", "_topdir " + top, spec], env=env)
        os.makedirs(out_dir, exist_ok=True)
        built = []  # type: List[str]
        for dirpath, _, files in os.walk(os.path.join(top, "RPMS")):
            for f in files:
                shutil.copy(os.path.join(dirpath, f), out_dir)
                built.append(os.path.join(out_dir, f))
        return built
    finally:
        shutil.rmtree(top, ignore_errors=True)


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(description="Construit les RPM de numa-place")
    ap.add_argument("--target", choices=sorted(TARGETS), required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--spec-only", action="store_true", help="affiche le spec sans construire")
    args = ap.parse_args(argv)
    mtime = int(os.environ.get("SOURCE_DATE_EPOCH", "1700000000"))
    if args.spec_only:
        print(render_spec(args.target, args.version, ROOT, mtime))
        return 0
    for path in build(args.target, args.version, args.out, mtime):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
