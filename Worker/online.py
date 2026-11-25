"""Opt-in Nextendo builds: separate engine, workspace and runtime; offline files are never changed.

Public service data is fetched only while preparing a Nextendo build. Account
tokens stay in Keychain and never enter requests, jobs, logs or game bundles.
"""
from __future__ import annotations
import os, plistlib, shutil, subprocess, sys
from pathlib import Path
from core import Failure, clone, clone_tree, digest, read

MODE = 'nextendo'
# An engine that shares the bundled shader chain installs beside, never over, a stand-alone one.
SHARED = 'nextendo-shared'

def enabled(c): return getattr(c,'r',{}).get('network') == MODE

def select_engine(request):
    selected = str(request.get('onlineEngineDir') or '').strip()
    if not selected: raise Failure('尚未选择 Nextendo 联机引擎目录，请先在设置中指定。')
    root = Path(selected).expanduser().resolve()
    if not (root/'dm/tools/nextendo_patches.py').is_file() or not (root/'dm/tools/nextendo_services.py').is_file():
        raise Failure('所选目录不包含 Nextendo 联机引擎，请使用 scripts/fetch_online_engine.py 获取。')
    return root

def game_root(c, game_id):
    root = c.library/'games'/game_id
    return root/slot(c) if enabled(c) else root

def schedule(c, game_id, version):
    """Event data lives beside the online runtime, so every app that opens the library finds it."""
    return game_root(c,game_id)/'bcat'/version

def slot(c):
    engine = getattr(c,'engine',None)
    return SHARED if engine and reads_images(engine) else MODE

def _modules(repo):
    tools = str(Path(repo)/'dm/tools')
    if tools not in sys.path: sys.path.insert(0, tools)
    # The bundled Python has no certificate store; macOS ships one.
    if Path('/etc/ssl/cert.pem').is_file(): os.environ.setdefault('SSL_CERT_FILE','/etc/ssl/cert.pem')
    import apple_crypto, nextendo_bcat, nextendo_patches, nextendo_services
    return apple_crypto, nextendo_bcat, nextendo_patches, nextendo_services

ERRORS = (OSError, ValueError, KeyError, subprocess.SubprocessError)

def toolchain(c, repo, env):
    os.environ['DEVELOPER_DIR'] = env['DEVELOPER_DIR']
    try: env['DM_OPENSSL_ROOT'] = str(_modules(repo)[0].prepare('macos'))
    except ERRORS as error: raise Failure('无法准备 Nextendo 加密组件，请检查网络后重试。') from error

def policy(catalog, game_id, version):
    """The server accepts exact versions only; reject a doomed build before compiling it."""
    row = next((r for r in catalog.get('games',[]) if game_id.lower() in [t.lower() for t in r['title_ids']]), None)
    if row is None or not row['enabled']: raise Failure('Nextendo 目前未开放此游戏。')
    accepted = (row.get('versions') or {}).get('accepted') or []
    if version not in accepted: raise Failure(f'Nextendo 不接受游戏版本 {version}，可用版本：{"、".join(accepted)}')
    return row

def services(c, repo, root, gp, version):
    _, bcat, patches, profile = _modules(repo)
    try: catalog = patches.verified_catalog(repo, root/'nextendo-catalog', games=True)
    except ERRORS as error: raise Failure('无法验证 Nextendo 游戏目录，请检查网络后重试。') from error
    row = policy(catalog, gp.id, version)
    try: profile.prepare(repo, gp.id.lower(), root/'nextendo-services', row.get('protocol'))
    except ERRORS as error: raise Failure('无法准备 Nextendo 服务配置，请检查网络后重试。') from error
    data = None
    if gp.id.lower() in bcat.TITLES:
        data = schedule(c,gp.id,version)
        data.parent.mkdir(parents=True,exist_ok=True)
        try: bcat.prepare(repo, gp.id.lower(), version, root/'nextendo-catalog/game-catalog.json', data)
        except ERRORS:
            c.emit('message','Nextendo 日程数据暂不可用；游戏启动时会再次尝试。')
    return row, data

def variant(c, up, repo, root, selected, env, gp, protocol):
    """Compile the signed, exact-build adapter beside (never into) the normal modules."""
    out = root/'nextendo-modules'; out.mkdir(exist_ok=True)
    _modules(repo)
    try: up.nextendo_variant(sys.executable, repo, selected/'ExeFS', out, root, env, root/'logs', gp)
    except up.BuildError as error: raise Failure('Nextendo 签名补丁未通过验证，未修改任何游戏代码。') from error
    if protocol == 'npln' and not (out/MODE/'main.fixups.tsv').is_file():
        raise Failure('Nextendo 没有适用于此游戏构建的签名补丁。')
    return out

def install(root, stage, version, name):
    """Add online files to a staged runtime; the caller inventories and publishes it atomically."""
    source = root/'nextendo-modules'/MODE
    if source.is_dir(): shutil.copytree(source, stage/'modules'/MODE)
    shutil.copytree(root/'nextendo-services', stage/'online')
    # The host puts this version in the account token and refuses to make one without
    # it. Foundation reads an Info.plist beside a bare executable as its bundle's.
    with (stage/'Info.plist').open('wb') as out:
        plistlib.dump({'CFBundleShortVersionString':version,'CFBundleDisplayName':name},out)

def reads_images(repo):
    """Whether the engine's host reads b-<chain>.img; older ones open each shader as a file."""
    return (Path(repo)/'dm/gfx/nvn/msl_image.h').is_file()

def copy_shaders(c, source, destination, chain, graphics='nvn'):
    if graphics in ('vulkan','gl') or not chain:
        clone_tree(source,destination,c.progress); return
    # What the host opens; translated sources, build reports and the shader cache stay out.
    library=Path(source)/('b-'+chain); out=Path(destination)/library.name; out.mkdir(parents=True)
    files=[p for p in sorted(library.iterdir()) if p.suffix in ('.metallib','.json','.tsv') and not p.name.endswith('_b_build.json')]
    if not any(p.suffix=='.metallib' for p in files): raise Failure('Metal 着色器缓存缺失。')
    for index,path in enumerate(files,1):
        if path.is_symlink() or not path.is_file(): raise Failure('不接受指向游戏库之外的符号链接。')
        clone(path,out/path.name)
        if not index%500 or index==len(files): c.check(); c.progress(index,len(files))

def keep_lock(c, repo):
    """Keep the engine's own lock when this Mac's tools are the ones it names.

    A Mac build leaves the iPhone Metal compiler out of its private lock. That changes the
    chain version and no Mac shader, and would hide the offline libraries built from the same tools.
    """
    if not reads_images(repo): return False
    name = 'dm/gfx/tools/dmsh_chain.lock'
    rows = lambda path: dict(l.split('\t',1) for l in path.read_text().splitlines() if '\t' in l and not l.startswith('#'))
    try: shipped, actual = rows(Path(c.engine)/name), rows(Path(repo)/name)
    except (OSError,ValueError): return False
    if 'metal_macosx' not in actual or any(shipped.get(k) != v for k,v in actual.items()) or set(shipped)-set(actual)-{'metal_iphoneos'}: return False
    shutil.copyfile(Path(c.engine)/name, Path(repo)/name)
    return True

def profile_inputs(repo, gp, env):
    """The engine's own digest of the profile a shader library is built from."""
    result = subprocess.run([sys.executable,str(Path(repo)/'dm/gfx/tools/dmsh_chain.py'),'--inputs'],
        env=dict(env,DM_GAME=gp.id),capture_output=True,text=True,timeout=120)
    return result.stdout.strip() if result.returncode == 0 else ''

def offline_shaders(c, repo, gp, chain, inputs):
    """The installed offline runtime whose shader libraries are exactly what this build would make, or None.

    Same game files, translator chain and shader profile, and an unmodified library:
    anything else falls through to a normal build."""
    if not enabled(c) or not reads_images(repo) or not chain or not inputs or gp.get('graphics','nvn') in ('vulkan','gl'): return None
    runtime = (c.library/'games'/gp.id/'current').resolve()
    manifest = read(runtime/'manifest.json',{})
    if not isinstance(manifest,dict) or manifest.get('gameId') != gp.id or manifest.get('chain') != chain: return None
    if manifest.get('graphics','nvn') in ('vulkan','gl') or not manifest.get('sourceHashes'): return None
    if sorted(manifest['sourceHashes']) != sorted(p['sha256'] for p in c.game()['packages']): return None
    image = f'shaders/b-{chain}.img'; files = (manifest.get('artifacts') or {}).get('files') or {}
    names = [image]+['profile/'+n for n in SHADER_TABLES]
    try:
        if digest(gp.table('shader_map.tsv')) != files['profile/shader_map.tsv'][1]: return None
        head = f'# chain b-{chain} inputs {inputs} '
        if not all((runtime/'profile'/n).read_text().startswith(head) for n in ('c1_full.tsv','cb_extent.tsv')): return None
        for name in names:
            path = runtime/name
            if path.is_symlink() or [path.stat().st_size, digest(path)] != list(files[name]): return None
    except (OSError,KeyError,IndexError,TypeError,ValueError): return None
    return runtime

SHADER_TABLES = ('shader_map.tsv','pso_manifest.tsv','c1_full.tsv','cb_extent.tsv')

def copy_shared(c, runtime, stage, chain):
    (stage/'shaders').mkdir(); (stage/'profile').mkdir(exist_ok=True)
    clone(runtime/f'shaders/b-{chain}.img', stage/f'shaders/b-{chain}.img', c.progress)
    for name in SHADER_TABLES: clone(runtime/'profile'/name, stage/'profile'/name)

def seed_pipelines(c, game_id, folder):
    """Start a shared-chain runtime from a copy of the offline pipeline cache; the two stay separate."""
    source = c.library/'games'/game_id/'pso_user'
    if slot(c) != SHARED or Path(folder).exists() or not source.is_dir(): return
    if read(game_root(c,game_id)/'current/manifest.json',{}).get('chain') != read(c.library/'games'/game_id/'current/manifest.json',{}).get('chain'): return
    partial = Path(str(folder)+'.partial'); shutil.rmtree(partial,ignore_errors=True)
    clone_tree(source,partial); partial.rename(folder)

def runtime_env(c, runtime, game_id):
    # The online engine cannot open packaged libraries, so such a runtime would render nothing.
    chain=read(Path(runtime)/'manifest.json',{}).get('chain') or ''
    if chain and list(Path(runtime).glob('shaders/*.img')) and not (Path(runtime)/'shaders'/('b-'+chain)).is_dir() \
            and not read(Path(runtime)/'manifest.json',{}).get('onlineImageReader'):
        raise Failure('Nextendo 运行文件不完整，请重新准备联机版本。')
    routes = Path(runtime)/'online/routes.tsv'
    if not routes.is_file(): raise Failure('Nextendo 运行文件不完整，请重新准备联机版本。')
    try:
        with (Path(runtime)/'Info.plist').open('rb') as source: bundle = plistlib.load(source)
    except (OSError, ValueError, plistlib.InvalidFileException): bundle = {}
    if not bundle.get('CFBundleShortVersionString'): raise Failure('Nextendo 运行文件不完整，请重新准备联机版本。')
    env = {'DM_NETWORK':MODE, 'DM_NEXTENDO_ROUTES':str(routes)}
    # Opt-in diagnostics: result codes and counters only, never names, tokens or traffic.
    if (c.private/'online/trace').is_file(): env.update(DM_ACCOUNT_TRACE='1', DM_SSL_TRACE='1', DM_NET_TRACE='1')
    version = read(Path(runtime)/'manifest.json',{}).get('version','')
    data = schedule(c,game_id,version)
    if (data/'setup.json').is_file(): env['DM_NEXTENDO_BCAT_DIR'] = str(data)
    return env
