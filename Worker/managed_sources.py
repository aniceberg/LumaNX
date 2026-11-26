"""Locate registered source packages and publish verified copies to a mounted share."""
from contextlib import contextmanager
from pathlib import Path
import os
import re
import shutil
import tempfile

from core import Failure, clone, contained, digest

RECONNECT = 'Reconnect the NAS share containing the managed source files, then retry.'
CHANGED = '托管源文件已丢失或内容发生改变，请重新导入。'


def local_path(c, package):
    return contained(c.library, c.library / package['path'])


def nas_path(package, location):
    root = Path(location['root'])
    owner = location['gameId']
    sha = package['sha256']
    if (not root.is_absolute() or root.resolve() != root
            or not re.fullmatch(r'[A-Fa-f0-9]{16}', owner)
            or not re.fullmatch(r'[A-Fa-f0-9]{64}', sha)):
        raise Failure('The registered NAS source location is invalid.')
    path = root / 'managed-sources' / owner / sha / Path(package['path']).name
    if path.resolve() != path or path.name in ('', '.', '..'):
        raise Failure('The registered NAS source location is invalid.')
    return path


def resolve(c, package):
    location = package.get('nasSource')
    if not location:
        return local_path(c, package)
    try:
        path = nas_path(package, location)
        if not path.exists(): raise Failure(RECONNECT)
    except OSError as error:
        raise Failure(RECONNECT) from error
    return path


def stamp(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def check_stamps(stamps):
    try:
        for path, previous in stamps.items():
            if stamp(path) != previous: raise Failure(CHANGED)
    except OSError as error:
        raise Failure(RECONNECT) from error


@contextmanager
def explain_disconnect(paths):
    try:
        yield
    except (Failure, OSError) as error:
        for path in paths:
            try:
                path.stat()
            except OSError:
                raise Failure(RECONNECT) from error
        raise


def verify(c, package, path, progress=None):
    if path.is_symlink():
        raise Failure(CHANGED)
    if package.get('sourceKind') == 'dump':
        from dump import inventory, package_hash
        actual = inventory(c, path)
        if package_hash(package, actual['sha256']) != package['sha256'] or actual['size'] != package['size']:
            raise Failure(CHANGED)
        return actual
    if not path.is_file() or (stamp_before := stamp(path))[2] != package['size']:
        raise Failure(CHANGED)
    if digest(path, progress or (lambda *_: c.check())) != package['sha256'] or stamp(path) != stamp_before:
        raise Failure(CHANGED)
    return None


def publish(c, game, share):
    """Verify each distinct container and return local files eligible for cleanup.

    The caller commits the updated game in one catalog write before cleanup.
    Completed destination files can be reused after interrupted transfers.
    """
    packages = {}
    for package in game['packages']:
        packages.setdefault(package['path'], []).append(package)
    cleanup = {}
    for rows in packages.values():
        package = rows[0]
        if package.get('nasSource') and package['nasSource']['root'] != str(share):
            continue
        raw = c.library / package['path']
        local = contained(c.library/'sources', raw)
        if raw != local:
            raise Failure('The managed source path is invalid.')
        location = {'root': str(share), 'gameId': game['id']}
        remote = nas_path(package, location)
        local_stamp = stamp(local) if local.exists() else None
        if remote.exists():
            verify(c, package, remote, c.progress)
        else:
            if package.get('nasSource') and local_stamp is None:
                raise Failure(RECONNECT)
            tree = verify(c, package, local, c.progress)
            if shutil.disk_usage(share).free < package['size'] + 64*2**20:
                raise Failure('Not enough NAS space for the managed source files.')
            remote.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.source-', dir=remote.parent) as scratch:
                staging = Path(scratch) / remote.name
                if tree is None:
                    clone(local, staging, c.progress)
                    with staging.open('rb') as stream: os.fsync(stream.fileno())
                    verify(c, package, staging, c.progress)
                else:
                    from dump import copy_tree
                    copy_tree(c, local, staging, tree)
                c.check()
                staging.rename(remote)
        for row in rows:
            if row['sha256'] != package['sha256']:
                raise Failure('Conflicting managed source checksums.')
            row['nasSource'] = location
        if local_stamp is not None:
            cleanup[local] = local_stamp
    return cleanup


def remove_local_copies(c, catalog, candidates):
    referenced = {local_path(c, p) for game in catalog['games'] for p in game['packages'] if not p.get('nasSource')}
    for path, old_stamp in candidates.items():
        c.check()
        if path not in referenced and path.exists() and not path.is_symlink() and stamp(path) == old_stamp:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
