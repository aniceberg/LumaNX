"""Opt-in Nextendo builds: separate engine, workspace and runtime; offline files are never changed.

Public service data is fetched only while preparing a Nextendo build. Account
tokens stay in Keychain and never enter requests, jobs, logs or game bundles.
"""
from __future__ import annotations
import os, plistlib, shutil, subprocess, sys
from pathlib import Path
from core import Failure, chain_of, clone, clone_tree, contained, digest, read

MODE = 'nextendo'
# An engine that shares the bundled shader chain installs beside, never over, a stand-alone one.
SHARED = 'nextendo-shared'

def enabled(c): return getattr(c,'r',{}).get('network') == MODE

def ensure_engine_tools(c):
    """Prepare the online engine's tools when an online build finds none for its fingerprint.

    The same step as "Prepare online engine" in Settings. The online engine is not
    bundled, so nothing prepares it at startup, and any change to its source or to
    the Worker bridge files gives it a new fingerprint. Call this before the build
    takes the shared toolchain lock: preparing needs the lock exclusively.
    """
    from engine import prepare, prepared_tools
    try:
        prepared_tools(c)
    except Failure:
        prepare(c)

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

def offline_runtime(c, game_id):
    """The shared-chain online build, when it can also be played offline.

    Its engine keeps every network service off unless DM_NETWORK selects one, so a
    launch without that variable plays offline. Only the shared slot qualifies: it
    runs the ported engine. The older stand-alone slot does not.
    """
    root = c.library/'games'/game_id/SHARED
    current = root/'current'
    try:
        if not current.is_symlink() or not current.is_dir(): return None
        runtime = contained(root/'releases', current.resolve(strict=True))
        if read(runtime/'manifest.json', {}).get('gameId') != game_id: return None
        if not (runtime/'dmhost').is_file() or not (runtime/'Info.plist').is_file(): return None
        return runtime
    except (Failure, OSError, ValueError, RuntimeError):
        return None

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

def policy(catalog, game_id, version=None):
    """Require an enabled title and check the exact version as soon as it is known."""
    row = next((r for r in catalog.get('games',[]) if game_id.lower() in [t.lower() for t in r['title_ids']]), None)
    if row is None or not row['enabled']: raise Failure('Nextendo 签名游戏目录尚未启用此游戏。')
    versions = row.get('versions') or {}
    capabilities = row.get('required_capabilities')
    supported = {'versions.exact.v1', 'versions.any.v1'}
    if (not isinstance(versions, dict) or not isinstance(capabilities, list)
            or any(not isinstance(capability, str) or capability not in supported for capability in capabilities)):
        raise Failure('此游戏需要当前 LumaNX 不支持的 Nextendo 功能。')
    if versions.get('policy') == 'any' and 'versions.any.v1' in capabilities and not {'accepted','recommended'} & versions.keys():
        return row
    accepted = versions.get('accepted')
    if (versions.get('policy') != 'exact' or 'versions.exact.v1' not in capabilities
            or not isinstance(accepted, list) or not accepted
            or not all(isinstance(value, str) and value for value in accepted)
            or versions.get('recommended', accepted[0]) not in accepted):
        raise Failure('此游戏需要当前 LumaNX 不支持的 Nextendo 功能。')
    if version is not None and version not in accepted: raise Failure(f'Nextendo 不接受游戏版本 {version}，可用版本：{"、".join(accepted)}')
    return row

def game_catalog(repo, root):
    _, _, patches, _ = _modules(repo)
    try: return patches.verified_catalog(repo, root/'nextendo-catalog', games=True)
    except ERRORS as error: raise Failure('无法验证 Nextendo 游戏目录，请检查网络后重试。') from error

def services(c, repo, root, gp, version, row):
    _, bcat, _, profile = _modules(repo)
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

def install(root, stage, version, name, *, patch_free=False):
    """Add online files to a staged runtime; the caller inventories and publishes it atomically."""
    source = root/'nextendo-modules'/MODE
    if not patch_free and source.is_dir(): shutil.copytree(source, stage/'modules'/MODE)
    shutil.copytree(root/'nextendo-services', stage/'online')
    # The host puts this version in the account token and refuses to make one without
    # it. Foundation reads an Info.plist beside a bare executable as its bundle's.
    with (stage/'Info.plist').open('wb') as out:
        plistlib.dump({'CFBundleShortVersionString':version,'CFBundleDisplayName':name},out)
    (stage/ROOTS).write_bytes(trust_roots())

# Games that run TLS in their own embedded OpenSSL (Mario Kart World) take their trusted
# roots from the system's built-in certificate list, so the host serves Nextendo's roots
# there. Published at https://wiki.nextendo.network/integration; pinned by SHA-256 here.
ROOTS = 'nextendo-roots.pem'
ROOT_FINGERPRINTS = (
    'd76bf8d0d7a1cd81874b20314171de9ecb64864837d684ab689831a72b6c0a01',  # NPLN root ("Nintendo Class 2 CA - G3", made by Nextendo)
    'a654c0eff8aa20a72280ea0ef2b5c2a17392922dd78ffbb113862e1ad2c66d49',  # Nextendo Network CA
)

def trust_roots():
    import base64, hashlib, re
    data = (Path(__file__).parent/ROOTS).read_bytes()
    blocks = re.findall(rb'-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----', data, re.S)
    found = tuple(hashlib.sha256(base64.b64decode(b''.join(block.split()))).hexdigest() for block in blocks)
    if found != ROOT_FINGERPRINTS: raise Failure('Nextendo 根证书校验失败，请重新安装 LumaNX。')
    return data

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
    if 'metal_macosx' not in actual or 'missing' in actual.values(): return False
    if any(shipped.get(k) != v for k,v in actual.items()) or set(shipped)-set(actual)-{'metal_iphoneos'}: return False
    shutil.copyfile(Path(c.engine)/name, Path(repo)/name)
    return True

def profile_inputs(repo, gp, env):
    """The engine's own digest of the profile a shader library is built from."""
    result = subprocess.run([sys.executable,str(Path(repo)/'dm/gfx/tools/dmsh_chain.py'),'--inputs'],
        env=dict(env,DM_GAME=gp.id),capture_output=True,text=True,timeout=120)
    return result.stdout.strip() if result.returncode == 0 else ''

SHADER_TABLES = ('shader_map.tsv','pso_manifest.tsv','c1_full.tsv','cb_extent.tsv')

def shader_sources(c, game):
    root = c.library/'games'/game['id']
    slots = (game_root(c,game['id']), root) if enabled(c) else (root, root/SHARED, root/MODE)
    for slot_root in slots:
        try:
            current = slot_root/'current'
            if current.is_dir():
                runtime = contained(c.library, current)
                if current.is_symlink():
                    contained(slot_root/'releases', runtime)
                yield runtime, read(runtime/'manifest.json', {})
        except (Failure, OSError, ValueError, RuntimeError):
            continue
    if game.get('nasRuntime'):
        from nas_play import authenticated_runtime
        try:
            runtime, _, manifest = authenticated_runtime(game['nasRuntime'])
            yield runtime, manifest
        except (Failure, OSError, ValueError, KeyError, TypeError):
            return

def reusable_shaders(c, repo, game, gp, env):
    """Find a matching installed macOS shader image in either play mode."""
    if not reads_images(repo) or gp.get('graphics','nvn') != 'nvn': return None
    chain = chain_of(repo,gp)
    source_hashes = sorted(p['sha256'] for p in game['packages'])
    if not chain or not source_hashes: return None
    package = gp.get('package')
    display = gp.builds()[package]['display']
    names = [f'shaders/b-{chain}.img', *('profile/'+n for n in SHADER_TABLES)]
    inputs = None
    for runtime, manifest in shader_sources(c, game):
        try:
            if manifest.get('gameId') != gp.id or manifest.get('chain') != chain or manifest.get('graphics','nvn') != 'nvn': continue
            if sorted(manifest['sourceHashes']) != source_hashes: continue
            if 'internalVersion' in manifest:
                if manifest['internalVersion'] != package: continue
            elif manifest.get('version') != display: continue
            files = {name: manifest['artifacts']['files'][name] for name in names}
            if any((runtime/name).resolve() != runtime/name or (runtime/name).stat().st_size != files[name][0] for name in names): continue
            if digest(gp.table('shader_map.tsv')) != files['profile/shader_map.tsv'][1]: continue
            if inputs is None: inputs = profile_inputs(repo, gp, env)
            if not inputs: return None
            from shader_artifacts import table_head_ok
            if not all(table_head_ok((runtime/'profile'/n).read_text(), chain, inputs) for n in ('c1_full.tsv','cb_extent.tsv')): continue
            if all(digest(runtime/name, lambda *_: c.check()) == files[name][1] for name in names):
                return runtime, files
        except (OSError, AttributeError, KeyError, IndexError, TypeError, ValueError, RuntimeError):
            continue
        except subprocess.TimeoutExpired:
            return None
    return None

def copy_shared(c, shared, stage):
    runtime, files = shared
    for name in files:
        clone(runtime/name, stage/name, c.progress)

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
    if (Path(runtime)/ROOTS).is_file(): env['DM_NEXTENDO_BUILTIN_CA'] = str(Path(runtime)/ROOTS)
    # Opt-in diagnostics: result codes and counters only, never names, tokens or traffic.
    if (c.private/'online/trace').is_file(): env.update(DM_ACCOUNT_TRACE='1', DM_SSL_TRACE='1', DM_NET_TRACE='1')
    version = read(Path(runtime)/'manifest.json',{}).get('version','')
    data = schedule(c,game_id,version)
    if (data/'setup.json').is_file(): env['DM_NEXTENDO_BCAT_DIR'] = str(data)
    return env
