"""Nextendo mode isolation: engine selection, workspaces, runtime paths and policy."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import sys, tempfile, unittest
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'Worker'));sys.path.insert(0,str(ROOT/'scripts'))
from core import Context, Failure, atomic
import build, fetch_online_engine, online
from incremental import workspace
GAME='0100C2500FC20000'

class OnlineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.engine=self.root/'engine';(self.engine/'dm/tools').mkdir(parents=True)
        for n in ('patches','services'):(self.engine/f'dm/tools/nextendo_{n}.py').write_text('')
    def tearDown(self):self.temp.cleanup()
    def context(self,**kw):
        (self.root/'resources/Engine/nxnative').mkdir(parents=True,exist_ok=True)
        return Context(dict(operation='build',privateRoot=str(self.root/'private'),resources=str(self.root/'resources'),**kw))
    def test_bundled_engine_stays_authoritative_unless_online_is_requested(self):
        path=str(self.engine)
        self.assertEqual(self.context(onlineEngineDir=path).engine,self.root/'resources/Engine/nxnative')
        self.assertEqual(self.context(onlineEngineDir=path,network='nextendo').engine,self.engine)
    def test_missing_online_engine_never_falls_back(self):
        c=self.context(network='nextendo');self.assertIsNone(c.engine)
        self.assertRegex(str(c.engineError),'尚未选择 Nextendo')
    def test_workspaces_and_runtimes_are_separate(self):
        off=self.context();on=self.context(network='nextendo',onlineEngineDir=str(self.engine));g={'id':GAME}
        a=workspace(off,g,'macos','k');b=workspace(on,g,'macos','k')
        self.assertNotEqual(a,b);self.assertEqual(workspace(off,g,'macos','k'),a)
        self.assertNotEqual(online.game_root(off,GAME),online.game_root(on,GAME))
    def test_exact_version_policy(self):
        catalog={'games':[{'title_ids':[GAME.lower()],'enabled':True,'protocol':'npln',
                           'required_capabilities':['versions.exact.v1'],
                           'versions':{'policy':'exact','accepted':['12.0.0']}}]}
        self.assertEqual(online.policy(catalog,GAME,'12.0.0')['protocol'],'npln')
        unknown={'games':[catalog['games'][0]|{'required_capabilities':['future.capability']}]}
        for value,version in ((catalog,'11.2.0'),({'games':[]},'12.0.0'),(unknown,'12.0.0')):
            with self.assertRaises(Failure):online.policy(value,GAME,version)
    def test_preparation_preview_follows_registered_and_derived_profiles(self):
        from library import online_preparation
        game={'id':GAME,'packages':[{'type':'base','version':0},{'type':'update','version':2}]}
        profile={'package':'v0','builds':{'v0':{'display':'1.0.0'},'v2':{'display':'12.0.0'}}}
        self.assertEqual(online_preparation(game,profile,False),{'version':'1.0.0','issues':[]})
        self.assertEqual(online_preparation(game,profile,True),{'version':'12.0.0','issues':[]})
        self.assertIsNone(online_preparation(game,profile|{'builds':{'v0':{'display':'1.0.0'}}},True)['version'])
        self.assertTrue(online_preparation(game|{'packages':[]},profile,True)['issues'])
    def test_unlisted_online_game_is_rejected_before_reading_sources(self):
        title = '0400C3F00006E000'
        c = self.context(network='nextendo', onlineEngineDir=str(self.engine))
        c.library = self.root / 'library'
        c.game = lambda: {'id': title}
        profile = SimpleNamespace(id=title, get=lambda _: 'v0', builds=lambda: {'v0': {'display': 'v0'}})
        prepared = (self.root, self.engine, {}, SimpleNamespace(), profile, 'fixture')
        with redirect_stdout(StringIO()), patch('build.prepare', return_value=prepared), \
             patch('online.game_catalog', return_value={'games': []}), patch('build.ms.plan') as plan, \
             patch('build.digest') as verify:
            with self.assertRaisesRegex(Failure, '签名游戏目录尚未启用'):
                build.build(c)
        plan.assert_not_called()
        verify.assert_not_called()
    def test_runtime_environment_requires_routes_and_contains_no_credentials(self):
        c=self.context(network='nextendo',onlineEngineDir=str(self.engine));rt=self.root/'rt'
        with self.assertRaises(Failure):online.runtime_env(c,rt,GAME)
        (rt/'online').mkdir(parents=True);(rt/'online/routes.tsv').write_text('x');self.plist(rt);atomic(rt/'manifest.json',{'version':'12.0.0'})
        self.assertEqual(set(online.runtime_env(c,rt,GAME)),{'DM_NETWORK','DM_NEXTENDO_ROUTES'})
    def shared(self):
        from core import digest
        (self.engine/'dm/gfx/nvn').mkdir(parents=True);(self.engine/'dm/gfx/nvn/msl_image.h').write_text('')
        c=self.context(network='nextendo',onlineEngineDir=str(self.engine));c.library=self.root/'library'
        c.game=lambda:{'id':GAME,'packages':[{'sha256':'a'},{'sha256':'b'}]}
        rt=c.library/'games'/GAME/'current';(rt/'shaders').mkdir(parents=True);(rt/'profile').mkdir()
        (rt/'shaders/b-abc.img').write_text('shaders');head='# chain b-abc inputs 123 \n'
        for n,t in (('shader_map.tsv','map'),('pso_manifest.tsv','p'),('c1_full.tsv',head),('cb_extent.tsv',head)):(rt/'profile'/n).write_text(t)
        files={str(p.relative_to(rt)):[p.stat().st_size,digest(p)] for p in rt.rglob('*') if p.is_file()}
        manifest=dict(gameId=GAME,chain='abc',graphics='nvn',sourceHashes=['b','a'],
                      version='12.0.0',internalVersion='v2818048',artifacts=dict(files=files))
        atomic(rt/'manifest.json',manifest)
        table=self.root/'table.tsv';table.write_text('map')
        return c,rt,SimpleNamespace(id=GAME,table=lambda n:table,
                    get=lambda k,d='': 'v2818048' if k=='package' else d,
                    builds=lambda: {'v2818048': {'display':'12.0.0'}})
    def reusable(self,c,gp,chain='abc',inputs='123',game=None):
        with patch('online.chain_of',return_value=chain), patch('online.profile_inputs',return_value=inputs):
            return online.reusable_shaders(c,self.engine,game or c.game(),gp,{})
    def test_matching_offline_shaders_are_reused(self):
        c,rt,gp=self.shared()
        self.assertEqual(online.slot(c),online.SHARED)
        shared=self.reusable(c,gp)
        self.assertEqual(shared[0],rt.resolve())
        stage=self.root/'stage';stage.mkdir();online.copy_shared(c,shared,stage)
        self.assertEqual((stage/'shaders/b-abc.img').read_text(),'shaders');self.assertEqual(len(list((stage/'profile').iterdir())),4)
    def test_offline_build_can_reuse_online_shaders(self):
        c,rt,gp=self.shared()
        remote_mode = rt.parent/online.SHARED/'current'
        remote_mode.parent.mkdir()
        rt.rename(remote_mode)
        c.r.pop('network')
        self.assertEqual(self.reusable(c,gp)[0],remote_mode)
    def test_online_reprepare_can_reuse_its_installed_shaders(self):
        c,rt,gp=self.shared()
        online_runtime = rt.parent/online.SHARED/'current'
        online_runtime.parent.mkdir()
        rt.rename(online_runtime)
        self.assertEqual(self.reusable(c,gp)[0],online_runtime)
    def test_online_build_reuses_authenticated_nas_shaders(self):
        from core import digest
        c,rt,gp=self.shared()
        nas=self.root/'nas'
        rt.rename(nas)
        game=c.game()
        game['nasRuntime']={'path':str(nas),'manifestHash':digest(nas/'manifest.json')}
        self.assertEqual(self.reusable(c,gp,game=game)[0],nas)
        game['nasRuntime']['manifestHash']='wrong'
        self.assertIsNone(self.reusable(c,gp,game=game))
    def test_any_shader_difference_falls_back_to_a_build(self):
        c,rt,gp=self.shared();check=lambda chain='abc',inputs='123':self.reusable(c,gp,chain,inputs)
        self.assertIsNone(check('other'));self.assertIsNone(check(inputs='999'));self.assertIsNone(check(inputs=''))
        self.assertIsNone(self.reusable(c,gp,game={'id':GAME,'packages':[{'sha256':'a'}]}))
        self.assertIsNotNone(check())
        (rt/'shaders/b-abc.img').write_text('changed');self.assertIsNone(check())
    def test_only_the_iphone_compiler_may_be_missing_from_the_shipped_lock(self):
        c,rt,gp=self.shared();repo=self.root/'repo';name='dm/gfx/tools/dmsh_chain.lock'
        for p in (self.engine,repo):(p/name).parent.mkdir(parents=True,exist_ok=True)
        (repo/'dm/gfx/nvn').mkdir(parents=True);(repo/'dm/gfx/nvn/msl_image.h').write_text('')
        shipped='# x\nxlate\t1\nmetal_macosx\t2\nmetal_iphoneos\t3\n';(self.engine/name).write_text(shipped)
        for text,kept in (('xlate\t1\nmetal_macosx\t2\n',True),('xlate\t9\nmetal_macosx\t2\n',False),('metal_macosx\t2\n',False),('xlate\t1\n',False)):
            (repo/name).write_text(text);self.assertEqual(online.keep_lock(c,repo),kept)
            self.assertEqual((repo/name).read_text(),shipped if kept else text)
    def test_pipeline_cache_is_copied_once_and_stays_separate(self):
        c,rt,gp=self.shared();src=c.library/'games'/GAME/'pso_user';src.mkdir();(src/'pipelines.tsv').write_text('x')
        on=online.game_root(c,GAME);(on/'current').mkdir(parents=True);atomic(on/'current/manifest.json',{'chain':'abc'})
        online.seed_pipelines(c,GAME,on/'pso_user');(on/'pso_user/pipelines.tsv').write_text('y')
        online.seed_pipelines(c,GAME,on/'pso_user')
        self.assertEqual(((on/'pso_user/pipelines.tsv').read_text(),(src/'pipelines.tsv').read_text()),('y','x'))
    def plist(self,rt):
        import plistlib
        (rt/'Info.plist').write_bytes(plistlib.dumps({'CFBundleShortVersionString':'12.0.0'}))
    def test_install_gives_the_bare_host_its_bundle_version(self):
        import plistlib
        (self.root/'nextendo-services').mkdir();stage=self.root/'stage';stage.mkdir()
        online.install(self.root,stage,'12.0.0','Splatoon 3')
        self.assertEqual(plistlib.loads((stage/'Info.plist').read_bytes())['CFBundleShortVersionString'],'12.0.0')
        c=self.context(network='nextendo',onlineEngineDir=str(self.engine));rt=self.root/'rt';(rt/'online').mkdir(parents=True);(rt/'online/routes.tsv').write_text('x')
        with self.assertRaises(Failure):online.runtime_env(c,rt,GAME)
    def test_runtime_with_packaged_shaders_is_refused(self):
        c=self.context(network='nextendo',onlineEngineDir=str(self.engine));rt=self.root/'rt'
        (rt/'online').mkdir(parents=True);(rt/'online/routes.tsv').write_text('x');self.plist(rt);atomic(rt/'manifest.json',{'version':'12.0.0','chain':'abc'})
        (rt/'shaders').mkdir();(rt/'shaders/b-abc.img').write_text('x')
        with self.assertRaises(Failure):online.runtime_env(c,rt,GAME)
        (rt/'shaders/b-abc').mkdir();online.runtime_env(c,rt,GAME)
    def test_event_data_is_found_by_every_app_that_opens_the_library(self):
        c=self.context(network='nextendo',onlineEngineDir=str(self.engine));rt=self.root/'rt'
        (rt/'online').mkdir(parents=True);(rt/'online/routes.tsv').write_text('x');self.plist(rt);atomic(rt/'manifest.json',{'version':'12.0.0'})
        self.assertNotIn('DM_NEXTENDO_BCAT_DIR',online.runtime_env(c,rt,GAME))
        data=online.schedule(c,GAME,'12.0.0');data.mkdir(parents=True);(data/'setup.json').write_text('{}')
        self.assertTrue(data.is_relative_to(c.library))
        c.private=self.root/'another-app'
        self.assertEqual(online.runtime_env(c,rt,GAME)['DM_NEXTENDO_BCAT_DIR'],str(data))
    def test_shaders_are_installed_as_files_for_hosts_without_an_image_reader(self):
        c=self.context(network='nextendo',onlineEngineDir=str(self.engine));src=self.root/'cache';lib=src/'b-abc';lib.mkdir(parents=True);(src/'cas').mkdir()
        for n in ('k_b.metallib','k_ba.metallib','k_b.json','k_b.metal','inputs.tsv','_b_build.json'):(lib/n).write_text(n)
        self.assertFalse(online.reads_images(self.engine))
        online.copy_shaders(c,src,self.root/'out','abc')
        self.assertEqual({p.name for p in self.root.glob('out/*/*')},{'k_b.metallib','k_ba.metallib','k_b.json','inputs.tsv'})
        self.assertEqual([p.name for p in (self.root/'out').iterdir()],['b-abc'])
    def test_npln_build_without_a_signed_patch_is_refused(self):
        up=SimpleNamespace(nextendo_variant=lambda *a:None,BuildError=RuntimeError)
        with patch('online._modules'):
            with self.assertRaisesRegex(Failure,'签名补丁'):online.variant(None,up,self.engine,self.root,self.root,{},None,'npln')
    def test_engine_patches_refuse_changed_sources(self):
        for name,_,_ in fetch_online_engine.PATCHES:
            p=self.engine/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('changed')
        with self.assertRaises(SystemExit):fetch_online_engine.apply(self.engine)

if __name__=='__main__':unittest.main()
