#!/usr/bin/env python3
"""Fetch the pinned NX Port Studio engine (MIT) used for Nextendo play and apply LumaNX's small, checked patches."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys

UPSTREAM = 'https://github.com/RobiiAragon/NX-Port-Studio.git'
REVISION = 'd0d59e373a1d19da6d50a154f03fa66e95575a19'
KEYCHAIN = 'org.lumanx.nextendo.account'
# (file, exact original text, replacement). Every original must occur exactly once.
PATCHES = (
    # NX Port Studio's pinned CA link returns 404; Nextendo's wiki serves the same certificate. The SHA-256 pin is unchanged.
    ('dm/net/nextendo-services.json', 'https://raw.githubusercontent.com/NextendoNetwork/Prelude-Nextendo/482c182917228d69ebbbabfea44fffa4a44051be/romfs/sd/rootCA.pem',
     'https://wiki.nextendo.network/media/certs/nextendo-network-ca.pem'),
    ('dm/tools/nextendo_services.py', "if not url.startswith('https://raw.githubusercontent.com/NextendoNetwork/Prelude-Nextendo/'):",
     "if url != 'https://wiki.nextendo.network/media/certs/nextendo-network-ca.pem':"),
    # The provider asks clients to resolve its published names. The contract's addresses remain the offline fallback.
    ('dm/tools/nextendo_services.py', "    for name in ('server', 'nat'):\n",
     "    import socket\n    for name, host in (('server', 'nex.nextendo.network'), ('nat', 'nat.nextendo.network')):\n"
     "        try: value['provider'][name] = socket.gethostbyname(host)\n        except OSError: pass\n"),
    # Keep LumaNX's credential apart from NX Port Studio's.
    ('dm/host/hle_nextendo_apple.m', '@"org.nxnative.nextendo.account"', '@"' + KEYCHAIN + '"'),
)


def apply(root):
    changed = {}
    for name, old, new in PATCHES:
        text = changed.get(name) or (root/name).read_text()
        if text.count(old) != 1: raise SystemExit('Pinned engine changed, patch does not apply: ' + name)
        changed[name] = text.replace(old, new)
    for name, text in changed.items(): (root/name).write_text(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    target = parser.parse_args().destination.expanduser().resolve()
    if target.exists(): raise SystemExit(f'{target} already exists; choose a new folder')
    partial = target.with_name(target.name + '.partial')
    if partial.exists(): shutil.rmtree(partial)
    git = lambda *a: subprocess.run(['git', '-C', str(partial), *a], check=True)
    partial.mkdir(parents=True)
    git('init', '-q'); git('fetch', '-q', '--depth', '1', UPSTREAM, REVISION); git('checkout', '-q', '--detach', 'FETCH_HEAD')
    head = subprocess.check_output(['git', '-C', str(partial), 'rev-parse', 'HEAD'], text=True).strip()
    if head != REVISION: raise SystemExit('Unexpected engine revision ' + head)
    apply(partial)
    partial.rename(target)
    print(target)


if __name__ == '__main__': sys.exit(main())
