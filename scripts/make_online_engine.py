#!/usr/bin/env python3
"""Make the online engine from the bundled nxnative snapshot and LumaNX's Nextendo patch.

The patch ports NX Port Studio's networking (MIT) and its stale float
storage fix. It changes nothing the shader chain covers, so every shader already built for
offline play stays valid; that is checked below.
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BASE, PATCH = ROOT/'Engine/nxnative', ROOT/'scripts/patches/nextendo/engine.patch'


def chain(root):
    return subprocess.check_output([sys.executable, '-B', str(root/'dm/gfx/tools/dmsh_chain.py')], text=True).strip()


def make(target, profiles=()):
    if target.exists(): raise SystemExit(f'{target} already exists; choose a new folder')
    partial = target.with_name(target.name + '.partial')
    shutil.rmtree(partial, ignore_errors=True)
    shutil.copytree(BASE, partial)
    subprocess.run(['git', 'init', '-q'], cwd=partial, check=True)   # apply relative to this folder, not an enclosing checkout
    subprocess.run(['git', 'apply', '--whitespace=nowarn', str(PATCH)], cwd=partial, check=True)
    shutil.rmtree(partial/'.git')
    (partial/'manifest.json').unlink()   # the bundle's inventory no longer describes these files
    for profile in profiles:
        if not (profile/'game.tsv').is_file(): raise SystemExit(f'{profile} is not a game profile')
        shutil.copytree(profile, partial/'dm/games'/profile.name, dirs_exist_ok=True, ignore=shutil.ignore_patterns('c1_full.tsv', 'cb_extent.tsv', '__pycache__'))
    if chain(partial) != chain(BASE): raise SystemExit('The patch changed the shader chain; offline shaders would not be reusable')
    partial.rename(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--profile', type=Path, action='append', default=[], help='a locally generated dm/games/<title id> folder to include')
    args = parser.parse_args()
    target = args.destination.expanduser().resolve()
    make(target, args.profile)
    print(target)


if __name__ == '__main__': sys.exit(main())
