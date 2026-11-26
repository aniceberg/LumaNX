"""Publish a verified playable runtime to a mounted share."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import uuid

from core import Failure, atomic, clone, contained, read
from recovery import game_root, inventory
from runtime_archive import _copy, _space
import managed_sources



def inventory_progress(c, expected=None, offset=0):
    """Report a stage-wide checksum total instead of resetting for each file."""
    done = 0
    positions = {}
    total = None if expected is None else 2 * sum(item['size'] for item in expected['files'].values())

    def report(path, count):
        nonlocal done
        done += count - positions.get(path, 0)
        positions[path] = count
        completed = offset + done
        c.progress(min(completed, total - 1) if total else completed, total)

    return report

def share_folder(c, chosen):
    if not isinstance(chosen, str) or not chosen:
        raise Failure('Choose a mounted NAS folder.')
    share = Path(chosen).expanduser()
    if share.is_symlink() or not share.is_dir():
        raise Failure('Choose a mounted NAS folder.')
    share = share.resolve(strict=True)
    library = c.library.resolve()
    if library == share or library in share.parents or share in library.parents:
        raise Failure('Choose a NAS folder outside the local LumaNX library.')
    return share


def slot_root(c, game_id, entry=None):
    """Local folder for a game's build slot.

    The offline slot is the game folder. A NAS entry recorded with `slot` came from
    the shared online build, which also plays offline (see build.launch).
    """
    local = game_root(c, 'games', game_id)
    slot = (entry or {}).get('slot')
    if slot is None:
        return local
    from online import SHARED
    if slot != SHARED or (local / slot).is_symlink():
        raise Failure('The playable NAS runtime record is invalid. Publish the runtime again.')
    return local / slot


def publishable_slot(c, game_id):
    """The build to move: the local offline build, else the game's one online build."""
    local = game_root(c, 'games', game_id)
    if (local / 'current').is_symlink() and (local / 'current').exists():
        return local, None
    from online import SHARED, offline_runtime
    if offline_runtime(c, game_id) and not (local / SHARED).is_symlink():
        return local / SHARED, SHARED
    return local, None


def finish_publish(c, catalog, candidates, current, source):
    c.begin('Release local compiled game space', 4, 4)
    managed_sources.remove_local_copies(c, catalog, candidates)
    if current.is_symlink():
        if current.resolve() != source:
            raise Failure('The active runtime changed. The local runtime was kept.')
        current.unlink()
    if source.is_dir():
        shutil.rmtree(source)
    # Older local builds were kept for rollback. Without the active build they no longer
    # share its disk blocks, so each would hold a full copy; the NAS build replaces them.
    releases = source.parent
    if releases.is_dir() and not releases.is_symlink():
        for old in releases.iterdir():
            if old.name.startswith('.') or old.is_symlink() or not old.is_dir():
                continue  # in-progress staging folders start with a dot
            c.check()
            shutil.rmtree(old)
    for link in current.parent.glob('current*'):
        if link.is_symlink() and not link.exists():
            link.unlink()  # rollback links that pointed at the removed builds


def publish(c):
    from build import validate_runtime

    with c.lock('execution'), c.lock():
        catalog = c.catalog()
        game = next((row for row in catalog['games'] if row['id'] == c.r.get('gameId')), None)
        if game is None: raise Failure('游戏未登记在当前游戏库中。')
        share = share_folder(c, c.r.get('archiveRoot'))
        include_sources = c.r.get('includeManagedSources') is True

        c.job['archiveRoot'] = str(share)
        c.job['includeManagedSources'] = include_sources
        atomic(c.jobpath, c.job)
        prior = game.get('nasRuntime', {})
        old_release = prior.get('localRelease')
        local, slot = publishable_slot(c, game['id'])
        if (old_release and Path(old_release).name == old_release
                and Path(prior['path']).parent.parent == share and slot_root(c, game['id'], prior) == local):
            # An interrupted publish finishes in the slot it started from.
            prior_local = slot_root(c, game['id'], prior)
            prior_current = prior_local / 'current'
            pending = contained(prior_local/'releases', prior_local/'releases'/old_release)
            if (not prior_current.exists() and not prior_current.is_symlink()) or prior_current.resolve() == pending:
                remote, _, _ = authenticated_runtime(prior)
                candidates = move_sources(c, game, share) if include_sources else {}
                c.check()
                c.save_catalog(catalog)
                finish_publish(c, catalog, candidates, prior_current, pending)
                return {'path': str(remote), 'savesKeptLocal': True}
        current = local / 'current'
        if not current.is_symlink() or not current.exists():
            raise Failure('Prepare a local runtime before publishing it to the NAS.')
        source = contained(local / 'releases', current.resolve(strict=True))
        validate_runtime(source)
        candidates = move_sources(c, game, share) if include_sources else {}
        identifier = uuid.uuid4().hex
        destination = share / game['id']
        if destination.is_symlink():
            raise Failure('The NAS game folder must not be a symbolic link.')
        destination.mkdir(exist_ok=True)
        remote = destination / identifier
        hosts = local / 'nas-hosts'
        if hosts.is_symlink():
            raise Failure('The local NAS host folder must not be a symbolic link.')
        hosts.mkdir(exist_ok=True)
        host = hosts / identifier / 'dmhost'
        c.begin('Inspect local runtime for NAS play', 1, 4)
        expected = inventory(c, source, inventory_progress(c))
        _space(destination, expected)
        with tempfile.TemporaryDirectory(prefix='.publishing-', dir=destination) as scratch:
            staging = Path(scratch) / 'runtime'
            c.begin('Copy playable runtime to NAS', 2, 4)
            _copy(c, source, staging, expected)
            c.begin('Verify playable NAS runtime', 3, 4)
            size = sum(item['size'] for item in expected['files'].values())
            if (inventory(c, staging, inventory_progress(c, expected)) != expected
                    or inventory(c, source, inventory_progress(c, expected, offset=size)) != expected):
                raise Failure('Runtime verification failed. The local runtime was kept.')
            # We validated the source and verified the destination byte for byte.
            host.parent.mkdir()
            clone(source / 'dmhost', host)
            host.chmod(0o755)
            if slot:
                # The online engine reads its bundle identity from Info.plist beside dmhost.
                clone(source / 'Info.plist', host.parent / 'Info.plist')
            c.check()
            staging.rename(remote)

        manifest = read(source / 'manifest.json')
        entry = dict(id=identifier, path=str(remote), localRelease=source.name, manifestHash=expected['files']['manifest.json']['sha256'],
                     version=manifest['version'], assetCompression=bool(manifest.get('assetCompression')),
                     inputConfig=manifest.get('inputConfig', True))
        if not manifest.get('artifacts'):
            entry['artifactInventory'] = expected
        entry.update({key: manifest[key] for key in ('internalVersion', 'retailVersion', 'retailVersionSource') if key in manifest})
        if slot:
            entry['slot'] = slot
        game['nasRuntime'] = entry
        c.check()
        c.save_catalog(catalog)
        finish_publish(c, catalog, candidates, current, source)
        return {'path': str(remote), 'savesKeptLocal': True}


def move_sources(c, game, share):
    c.begin('Copy and verify managed sources on the NAS')
    return managed_sources.publish(c, game, share)


def publish_sources(c):
    with c.lock('execution'), c.lock():
        catalog = c.catalog()
        game = next((row for row in catalog['games'] if row['id'] == c.r.get('gameId')), None)
        if not game or not game.get('nasRuntime'):
            raise Failure('Move the playable runtime to the NAS first.')
        remote, _, manifest = authenticated_runtime(game['nasRuntime'])
        if manifest.get('gameId') != game['id']:
            raise Failure('The playable NAS runtime belongs to another game.')
        share = share_folder(c, str(remote.parent.parent))
        candidates = move_sources(c, game, share)
        c.check()
        c.save_catalog(catalog)
        managed_sources.remove_local_copies(c, catalog, candidates)
        return {'path': str(share/'managed-sources'/game['id'])}


def launch_paths(c, game, current):
    """Prefer a new local build. A published game uses its local host on launch."""
    if current.exists() or not game.get('nasRuntime'):
        return current, current / 'dmhost'
    entry = game['nasRuntime']
    remote, _, _ = authenticated_runtime(entry)
    if not (remote / 'modules').is_dir() or not (remote / 'shaders').is_dir() or not (
        (remote / 'romfs.img').is_file() or (remote / 'romfs').is_dir()
    ):
        raise Failure('The playable NAS runtime is incomplete. Publish the runtime again.')
    hosts = slot_root(c, game['id'], entry) / 'nas-hosts'
    host = contained(hosts, hosts / entry['id'] / 'dmhost')
    if not host.is_file():
        raise Failure('The local NAS game host is missing. Publish the runtime again.')
    return remote, host


def authenticated_runtime(entry):
    remote = Path(entry['path'])
    manifest = remote / 'manifest.json'
    if not manifest.is_file() or remote.is_symlink() or manifest.is_symlink():
        raise Failure('The playable NAS runtime is offline. Reconnect the share and retry.')
    data = manifest.read_bytes()
    if hashlib.sha256(data).hexdigest() != entry['manifestHash']:
        raise Failure('The playable NAS runtime manifest changed. Publish the runtime again.')
    return remote, data, json.loads(data)


def restore_local(c):
    from build import validate_runtime

    with c.lock('execution'), c.lock():
        game = c.game()
        entry = game.get('nasRuntime')
        # An online build comes back into the online slot, where it plays in both modes.
        local = slot_root(c, game['id'], entry)
        current = local / 'current'
        if current.exists():
            validate_runtime(current)
            return {'path': str(current.resolve()), 'alreadyLocal': True}
        if not entry:
            raise Failure('No playable NAS runtime is registered for this game.')
        source, data, manifest = authenticated_runtime(entry)
        if manifest.get('gameId') != game['id']:
            raise Failure('The playable NAS runtime belongs to another game.')
        artifacts = manifest.get('artifacts')
        if artifacts:
            expected = {
                'directories': artifacts['directories'],
                'files': {name: {'size': pair[0], 'sha256': pair[1]} for name, pair in artifacts['files'].items()}
            }
            expected['files']['manifest.json'] = {'size': len(data), 'sha256': entry['manifestHash']}
        elif entry.get('artifactInventory'):
            expected = entry['artifactInventory']
        else:
            raise Failure('The playable NAS runtime has no verified file inventory.')
        for name in [*expected['directories'], *expected['files']]:
            path = PurePosixPath(name)
            if not name or name == '.' or path.is_absolute() or '..' in path.parts or path.as_posix() != name:
                raise Failure('The playable NAS runtime inventory is invalid.')
        for directory in expected['directories']:
            if (source / directory).is_symlink():
                raise Failure('The playable NAS runtime inventory is invalid.')
        releases = local / 'releases'
        if releases.is_symlink():
            raise Failure('The local release folder must not be a symbolic link.')
        releases.mkdir(exist_ok=True)
        _space(releases, expected)
        with tempfile.TemporaryDirectory(prefix='.nas-local-', dir=releases) as scratch:
            stage = Path(scratch) / 'runtime'
            c.begin('Copy playable runtime back to Mac', 1, 2)
            _copy(c, source, stage, expected)
            c.begin('Verify local playable runtime', 2, 2)
            if inventory(c, stage, inventory_progress(c)) != expected:
                raise Failure('The restored copy failed verification. The NAS archive and saves were kept.')
            (stage / 'dmhost').chmod(0o755)
            validate_runtime(stage)
            c.check()
            final = releases / ('nas-local-' + uuid.uuid4().hex)
            stage.rename(final)
            link = local / ('current.nas-local-' + uuid.uuid4().hex)
            try:
                link.symlink_to(final.relative_to(local), target_is_directory=True)
                os.replace(link, current)
            finally:
                link.unlink(missing_ok=True)
        return {'path': str(final), 'nasCopyKept': True}
