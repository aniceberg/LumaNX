"""Synthetic compiled payloads only; no games, keys, external drives or NAS IO."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'Worker'))
from core import Context, Failure, Cancelled, atomic
import library
import runtime_archive

GAME='040074A01BF12000'


class RuntimeArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.library=self.root/'local';self.library.mkdir()
        self.archive=self.root/'nas-simulation'
        self.c=Context(dict(operation='archiveGame',privateRoot=str(self.root/'private'),library=str(self.library),gameId=GAME,archiveRoot=str(self.archive)))
        self.c.save_catalog(dict(schema=1,games=[dict(id=GAME,name='Synthetic game',sourceKind='nup',display='v458752',packages=[],hidden=False)]))
        self.local=self.library/'games'/GAME;runtime=self.local/'releases'/'fixture';runtime.mkdir(parents=True)
        (self.local/'current').symlink_to('releases/fixture',target_is_directory=True)
        for folder in ['romfs','modules','shaders','profile']:(runtime/folder).mkdir()
        (runtime/'dmhost').write_bytes(b'synthetic host');(runtime/'dmhost').chmod(0o755)
        (runtime/'romfs/assets').write_bytes(b'A'*100000)
        for ext in ('patched.bin','fixups.tsv','gates.tsv'):(runtime/'modules'/('main.'+ext)).write_bytes(b'synthetic module')
        shaders=runtime/'shaders/b-abc';shaders.mkdir()
        (shaders/'key_b.json').write_text(json.dumps(dict(chain='abc')))
        (shaders/'key_b.metallib').write_bytes(b'synthetic shader')
        (shaders/'key_b.metal').write_bytes(b'generated source'*10000)
        (runtime/'shaders/cas-abc.db').write_bytes(b'build cache'*10000)
        atomic(runtime/'manifest.json',dict(schema=1,gameId=GAME,chain='abc',graphics='nvn',version='v458752',internalVersion='v458752',retailVersion='3.2.0',preparationMode='registered'))
        saves=self.library/'saves'/GAME;saves.mkdir(parents=True);(saves/'save.dat').write_bytes(b'keep my save')
        self.runtime=runtime

    def tearDown(self):self.tmp.cleanup()
    def quiet(self,fn,*args):
        with contextlib.redirect_stdout(io.StringIO()):return fn(*args)
    def saved(self):return (self.library/'saves'/GAME/'save.dat').read_bytes()

    def managed_nup(self):
        from core import digest
        source=self.library/'sources'/GAME/'combined.nup'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'combined base and update')
        common=dict(path=str(source.relative_to(self.library)),sha256=digest(source),
                    sourceKind='nup',size=source.stat().st_size)
        catalog=self.c.catalog()
        catalog['games'][0]['packages']=[common|dict(type='base',version=0),common|dict(type='update',version=458752)]
        self.c.save_catalog(catalog)
        return source

    def test_publish_with_sources_copies_combined_nup_once_and_restores_runtime_only(self):
        import managed_sources, nas_play
        source=self.managed_nup()
        self.archive.mkdir()
        self.c.r['includeManagedSources']=True
        with patch('managed_sources.clone',wraps=managed_sources.clone) as copy:
            self.quiet(nas_play.publish,self.c)
        self.assertEqual(copy.call_count,1)
        self.assertFalse(source.exists())
        packages=self.c.game()['packages']
        destinations={managed_sources.resolve(self.c,p) for p in packages}
        self.assertEqual(len(destinations),1)
        remote=destinations.pop()
        self.assertEqual(remote.read_bytes(),b'combined base and update')
        self.quiet(nas_play.restore_local,self.c)
        self.assertTrue(remote.is_file())
        self.assertEqual(self.c.game()['packages'],packages)
        self.assertFalse(source.exists())
        self.assertEqual(self.saved(),b'keep my save')

    def test_source_only_move_keeps_playable_runtime_and_hidden_shared_source(self):
        import managed_sources, nas_play
        source=self.managed_nup()
        catalog=self.c.catalog()
        catalog['games'].append(dict(id='0400000000000001',hidden=True,packages=[catalog['games'][0]['packages'][0].copy()]))
        self.c.save_catalog(catalog)
        self.archive.mkdir()
        self.quiet(nas_play.publish,self.c)
        active=self.c.game()['nasRuntime']
        self.assertTrue(source.is_file())
        self.quiet(nas_play.publish_sources,self.c)
        self.assertEqual(self.c.game()['nasRuntime'],active)
        self.assertTrue(source.is_file())
        self.assertTrue(managed_sources.resolve(self.c,self.c.game()['packages'][0]).is_relative_to(self.archive.resolve()))
        self.c.r['kind']='sources'
        self.quiet(library.clean,self.c)
        self.assertEqual(len(self.c.game()['packages']),2)
        self.assertTrue(source.is_file())

    def test_source_verification_failure_and_cancelled_retry_keep_original_assets(self):
        import managed_sources, nas_play
        source=self.managed_nup()
        self.archive.mkdir()
        self.c.r['includeManagedSources']=True
        package=self.c.game()['packages'][0]
        remote=managed_sources.nas_path(package,{'root':str(self.archive.resolve()),'gameId':GAME})
        remote.parent.mkdir(parents=True)
        remote.write_bytes(b'bad destination')
        with self.assertRaises(Failure):
            self.quiet(nas_play.publish,self.c)
        self.assertTrue(source.is_file())
        self.assertTrue((self.local/'current/dmhost').is_file())
        self.assertNotIn('nasSource',self.c.game()['packages'][0])
        remote.write_bytes(source.read_bytes())
        with patch('nas_play.finish_publish',side_effect=Cancelled):
            with self.assertRaises(Cancelled):
                self.quiet(nas_play.publish,self.c)
        self.assertTrue(source.is_file())
        self.assertTrue((self.local/'current/dmhost').is_file())
        published=self.c.game()['nasRuntime']['path']
        with patch('managed_sources.clone') as copy:
            result=self.quiet(nas_play.publish,self.c)
        copy.assert_not_called()
        self.assertEqual(result['path'],published)
        self.assertFalse(source.exists())
        self.assertFalse((self.local/'current').exists())

    def test_publish_nas_launch_uses_remote_assets_and_local_host_and_saves(self):
        import build
        import nas_play
        self.archive.mkdir()
        published = self.quiet(nas_play.publish, self.c)
        remote = Path(published['path'])
        self.assertFalse((self.local / 'current').exists())
        self.assertEqual(self.saved(), b'keep my save')
        self.assertTrue((remote / 'romfs/assets').is_file())
        self.assertFalse((remote / 'runtime.zip').exists())
        captured = {}

        def run(args, log, env, **kwargs):
            captured.update(host=args[0], modules=args[1], env=env)

        with patch('toolchain.require_launch_environment'), patch.object(self.c, 'run', side_effect=run):
            self.quiet(build.launch, self.c)
        self.assertTrue(captured['host'].is_relative_to(self.local.resolve()))
        self.assertEqual(captured['modules'], remote / 'modules')
        self.assertEqual(captured['env']['DM_ROMFS'], str(remote / 'romfs'))
        self.assertEqual(captured['env']['DM_SAVE'], str(self.library.resolve() / 'saves' / GAME))
        remote.rename(remote.with_name('offline'))
        with self.assertRaisesRegex(Failure, 'offline'):
            nas_play.launch_paths(self.c, self.c.game(), self.local / 'current')
        new_release = self.local / 'releases' / 'new-local-build'
        new_release.mkdir()
        current = self.local / 'current'
        current.symlink_to(new_release.relative_to(self.local), target_is_directory=True)
        self.assertEqual(nas_play.launch_paths(self.c, self.c.game(), current), (current, current / 'dmhost'))

    def test_failed_nas_publish_retains_active_game(self):
        import nas_play
        self.archive.mkdir()
        with patch('nas_play._copy', side_effect=OSError('SMB write failed')):
            with self.assertRaises(OSError):
                self.quiet(nas_play.publish, self.c)
        self.assertTrue((self.local / 'current/dmhost').is_file())
        self.assertNotIn('nasRuntime', self.c.game())
        self.assertEqual(self.saved(), b'keep my save')

    def test_move_back_verifies_existing_nas_snapshot_before_local_activation(self):
        import nas_play
        from incremental import runtime_inventory

        manifest_path = self.runtime / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['artifacts'] = runtime_inventory(self.c, self.runtime)
        atomic(manifest_path, manifest)
        self.archive.mkdir()
        published = self.quiet(nas_play.publish, self.c)
        remote = Path(published['path'])
        assets = remote / 'romfs/assets'
        original = assets.read_bytes()
        assets.write_bytes(b'Z' * len(original))
        with self.assertRaisesRegex(Failure, 'failed verification'):
            self.quiet(nas_play.restore_local, self.c)
        self.assertFalse((self.local / 'current').exists())
        self.assertEqual(self.saved(), b'keep my save')
        assets.write_bytes(original)
        result = self.quiet(nas_play.restore_local, self.c)
        self.assertTrue(result['nasCopyKept'])
        self.assertTrue(remote.is_dir())
        self.assertEqual((self.local / 'current/romfs/assets').read_bytes(), original)
        self.assertEqual(self.saved(), b'keep my save')

    def test_archive_restore_round_trip_without_sources_or_build_cache(self):
        result=self.quiet(runtime_archive.archive,self.c)
        entry=result['archive'];archived=Path(entry['path'])
        self.assertFalse((self.local/'current').exists());self.assertTrue(archived.is_dir())
        self.assertEqual(self.saved(),b'keep my save')
        self.assertTrue((archived/'runtime.zip').is_file())
        self.assertTrue(entry['compressed']);self.assertLess(entry['size'],entry['runtimeSize'])
        self.assertEqual((entry['retailVersion'], entry['internalVersion']), ('3.2.0', 'v458752'))
        self.assertEqual(self.c.catalog()['games'][0]['runtimeArchive']['path'],str(archived))
        restored=self.quiet(runtime_archive.restore,self.c)
        self.assertTrue((self.local/'current/dmhost').is_file());self.assertTrue(restored['archiveKept'])
        self.assertEqual(self.saved(),b'keep my save');self.assertTrue(archived.exists())
        self.assertFalse((self.local/'current/shaders/cas-abc.db').exists())
        import os
        self.assertTrue(os.access(self.local/'current/dmhost',os.X_OK))
        import runtime_payload
        view=runtime_payload.shader_tools().view(self.local/'current/shaders/b-abc')
        self.assertTrue((view/'key_b.metallib').is_file());self.assertFalse((view/'key_b.metal').exists())
        self.assertFalse((self.library/'sources').exists());self.assertFalse((self.library/'cache').exists())

    def test_failed_copy_keeps_local_game_and_saves(self):
        with patch.object(runtime_archive,'_zip',side_effect=OSError('simulated SMB failure')):
            with self.assertRaises(OSError):self.quiet(runtime_archive.archive,self.c)
        self.assertTrue((self.local/'current/dmhost').is_file());self.assertEqual(self.saved(),b'keep my save')
        self.assertNotIn('runtimeArchive',self.c.catalog()['games'][0])

    def test_cancellation_before_verified_copy_keeps_local_game(self):
        with patch.object(runtime_archive,'_zip',side_effect=Cancelled):
            with self.assertRaises(Cancelled):self.quiet(runtime_archive.archive,self.c)
        self.assertTrue((self.local/'current/dmhost').is_file());self.assertEqual(self.saved(),b'keep my save')

    def test_corrupt_archive_cannot_replace_local_game_or_saves(self):
        result=self.quiet(runtime_archive.archive,self.c)
        (Path(result['archive']['path'])/'runtime.zip').write_bytes(b'corrupted')
        with self.assertRaises(Failure):self.quiet(runtime_archive.restore,self.c)
        self.assertFalse((self.local/'current').exists());self.assertEqual(self.saved(),b'keep my save')

    def test_archive_manifest_is_pinned_in_local_catalog(self):
        result=self.quiet(runtime_archive.archive,self.c)
        manifest=Path(result['archive']['path'])/'manifest.json'
        manifest.write_text(manifest.read_text()+' ')
        with self.assertRaisesRegex(Failure,'manifest changed'):self.quiet(runtime_archive.restore,self.c)
        self.assertEqual(self.saved(),b'keep my save')

    def test_restore_uses_only_the_authenticated_manifest_read(self):
        entry = self.quiet(runtime_archive.archive, self.c)['archive']
        manifest_path = Path(entry['path']) / 'manifest.json'
        original_read = Path.read_bytes
        reads = []

        def replace_after_read(path):
            data = original_read(path)
            if path == manifest_path:
                reads.append(path)
                path.write_bytes(b'{}')
            return data

        with patch.object(Path, 'read_bytes', replace_after_read):
            self.quiet(runtime_archive.restore, self.c)
        self.assertEqual(reads, [manifest_path])
        self.assertTrue((self.local / 'current').exists())

    def test_requirements_distinguish_playing_from_rebuilding(self):
        profile=dict(id=GAME,name='Synthetic game',package='v458752',builds={'v458752':{'display':'2.0'}},compatibility='verified')
        self.c.engine=self.root/'engine'
        from incremental import pipeline_version
        manifest=json.loads((self.runtime/'manifest.json').read_text())
        manifest.update(engine='new-engine',pipeline=pipeline_version())
        atomic(self.runtime/'manifest.json',manifest)
        with patch('engine.inspect_source',return_value=dict(fingerprint='new-engine',derivedProfiles=False,inputConfig=False)),patch.object(library,'refresh_container_metadata'),patch.object(library,'profiles',return_value={GAME:profile}):
            game=self.quiet(library.catalog,self.c)['games'][0]
            self.assertTrue(game['ready']);self.assertEqual(game['missing'],[]);self.assertTrue(game['buildMissing'])
            self.assertFalse(game['needsPreparation'])
            self.quiet(runtime_archive.archive,self.c)
            game=self.quiet(library.catalog,self.c)['games'][0]
            self.assertFalse(game['ready']);self.assertTrue(game['archived']);self.assertEqual(game['missing'],[])
            self.assertTrue(game['buildMissing'])

    def test_library_cannot_be_its_own_archive_destination(self):
        self.c.r['archiveRoot']=str(self.library/'archive')
        with self.assertRaises(Failure):self.quiet(runtime_archive.archive,self.c)
        self.assertTrue((self.local/'current/dmhost').is_file())
