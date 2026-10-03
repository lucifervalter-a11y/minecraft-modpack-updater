import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import updater as u

def jar(mod='sample',version='1'):
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z:
        z.writestr('META-INF/mods.toml',f'modLoader="javafml"\nloaderVersion="[47,)"\nlicense="MIT"\n[[mods]]\nmodId="{mod}"\nversion="{version}"\n')
    return stream.getvalue()

def sha(b): return hashlib.sha256(b).hexdigest()

class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.base=Path(self.tmp.name)
        self.root=self.base/'game'; self.root.mkdir()
        self.payload=jar()
        self.m={'schema':1,'release':'fixture-1','mods':[{'project_id':'fixture','name':'Sample','version':'1','file':'sample-1.jar','sha256':sha(self.payload),'bytes':len(self.payload),'mod_ids':['sample'],'providers':[],'category':'mods','download_url':'https://cdn.modrinth.com/data/fixture/versions/v1/sample.jar'}],'configs':[],'extras':[]}
        self.patches=[patch.object(u,'require_closed'),patch.object(u,'load_manifest',lambda:self.m)]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def fetch(self,spec,dest):dest.write_bytes(self.payload)
    def install(self):return u.apply(self.root,self.m,fetch=self.fetch)
    def test_check_is_read_only(self):
        with patch.object(u,'download',side_effect=AssertionError('network')):
            result=u.inspect_profile(self.root,self.m)
        self.assertEqual(list(self.root.iterdir()),[])
        self.assertEqual(len(result['ops']),1)
    def test_install_and_idempotence(self):
        self.install(); self.assertEqual((self.root/'mods/sample-1.jar').read_bytes(),self.payload)
        with patch.object(u,'download',side_effect=AssertionError('network')):
            self.assertIn('Изменений нет',self.install())
    def test_rollback_fresh_install(self):
        self.install(); u.rollback(self.root)
        self.assertFalse((self.root/'mods/sample-1.jar').exists()); self.assertFalse((self.root/u.STATE).exists())
    def test_preserves_private_and_unmanaged(self):
        sentinels={'saves/world/level.dat':b'world','config/custom.toml':b'settings','XaeroWaypoints/private.txt':b'waypoint','accounts.json':b'secret sentinel','options.txt':b'graphics','mods/unrelated.jar':jar('other')}
        for rel,data in sentinels.items():
            p=self.root/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
        self.install();u.rollback(self.root)
        for rel,data in sentinels.items():self.assertEqual((self.root/rel).read_bytes(),data)
    def test_unmanaged_collision_blocked(self):
        (self.root/'mods').mkdir();(self.root/'mods/sample-1.jar').write_bytes(jar('sample','bad'))
        with self.assertRaises(u.SafetyError):self.install()
    def test_renamed_old_version_conflict(self):
        (self.root/'mods').mkdir();(self.root/'mods/renamed.jar').write_bytes(jar('sample','0'))
        self.assertTrue(u.inspect_profile(self.root,self.m)['conflicts'])
    def test_same_hash_alias_adopted_without_duplicate(self):
        (self.root/'mods').mkdir();(self.root/'mods/alias.jar').write_bytes(self.payload)
        self.install();self.assertFalse((self.root/'mods/sample-1.jar').exists())
        self.assertEqual(json.loads((self.root/u.STATE).read_text())['managed']['fixture']['path'],'mods/alias.jar')
    def test_duplicate_jars_blocked(self):
        (self.root/'mods').mkdir()
        for n in ('one.jar','two.jar'):(self.root/'mods'/n).write_bytes(self.payload)
        with self.assertRaises(u.SafetyError):self.install()
    def test_bad_download_does_not_commit(self):
        with self.assertRaises(u.SafetyError):u.apply(self.root,self.m,fetch=lambda s,p:p.write_bytes(b'tampered'))
        self.assertFalse((self.root/'mods').exists());self.assertFalse((self.root/u.STATE).exists())
    def test_network_failure_does_not_commit(self):
        with self.assertRaises(OSError):u.apply(self.root,self.m,fetch=lambda s,p:(_ for _ in ()).throw(OSError('network down')))
        self.assertFalse((self.root/'mods').exists())
    def test_update_owned_version_and_rollback(self):
        self.install();original=self.payload
        self.payload=jar(version='2');self.m['release']='fixture-2'
        self.m['mods'][0].update(version='2',file='sample-2.jar',sha256=sha(self.payload),bytes=len(self.payload))
        self.install();self.assertFalse((self.root/'mods/sample-1.jar').exists())
        self.assertEqual((self.root/'mods/sample-2.jar').read_bytes(),self.payload)
        u.rollback(self.root);self.assertEqual((self.root/'mods/sample-1.jar').read_bytes(),original);self.assertFalse((self.root/'mods/sample-2.jar').exists())
    def test_external_change_blocks_rollback_without_partial_restore(self):
        self.install();state=(self.root/u.STATE).read_bytes();(self.root/'mods/sample-1.jar').write_bytes(b'user change')
        with self.assertRaises(u.SafetyError):u.rollback(self.root)
        self.assertEqual((self.root/u.STATE).read_bytes(),state)
        self.assertEqual((self.root/'mods/sample-1.jar').read_bytes(),b'user change')
    def test_tampered_backup_blocked(self):
        self.install();directory,j=u.backup_list(self.root)[0];(directory/'0.after').write_bytes(b'corrupt')
        with self.assertRaises(u.SafetyError):u.rollback(self.root)
        self.assertTrue((self.root/'mods/sample-1.jar').exists())
    def test_pending_crash_recovery(self):
        self.install();directory,j=u.backup_list(self.root)[0];j['status']='pending';u.save_json(directory/'journal.json',j)
        with self.assertRaises(u.SafetyError):self.install()
        u.rollback(self.root);self.assertFalse((self.root/'mods/sample-1.jar').exists())
    def test_commit_failure_automatically_rolls_back(self):
        real=u.atomic_write
        def fail_state(path,data):
            if str(path).endswith('state.json'):raise OSError('fixture write failure')
            return real(path,data)
        with patch.object(u,'atomic_write',fail_state):
            with self.assertRaises(OSError):self.install()
        self.assertFalse((self.root/'mods/sample-1.jar').exists())
    def test_configs_add_only_missing_and_rollback(self):
        presets=self.base/'presets';presets.mkdir();(presets/'fixture.toml').write_bytes(b'new')
        self.m['configs']=[{'path':'config/fixture.toml','bundle':'fixture.toml','sha256':sha(b'new')}]
        with patch.object(u,'BASE',self.base):self.install()
        self.assertEqual((self.root/'config/fixture.toml').read_bytes(),b'new')
        u.rollback(self.root);self.assertFalse((self.root/'config/fixture.toml').exists())
        (self.root/'config/fixture.toml').write_bytes(b'custom')
        with patch.object(u,'BASE',self.base):self.install()
        self.assertEqual((self.root/'config/fixture.toml').read_bytes(),b'custom')
    def test_traversal_and_external_targets_rejected(self):
        for rel in ('../accounts.json','saves/world.jar','mods/../accounts.json','config/a:b','mods/a.exe'):
            with self.assertRaises(u.SafetyError):u.target(self.root,rel)
    def test_symlink_rejected(self):
        p=self.root/'mods'
        try:p.symlink_to(self.base,target_is_directory=True)
        except OSError:self.skipTest('symlinks unavailable')
        with self.assertRaises(u.SafetyError):u.inspect_profile(self.root,self.m)
    def test_network_and_root_profile_rejected(self):
        for path in ('//server/share/profile',str(Path(self.root.anchor))):
            with self.assertRaises(u.SafetyError):u.profile_path(path)
        invalid=self.base/'mods';invalid.mkdir()
        with self.assertRaises(u.SafetyError):u.profile_path(invalid)
    def test_url_policy(self):
        for url in ('http://cdn.modrinth.com/data/a','https://evil.test/a','https://cdn.modrinth.com.evil.test/data/a','https://x@cdn.modrinth.com/data/a','https://cdn.modrinth.com/data/a?x=1'):
            with self.assertRaises(u.SafetyError):u.valid_url(url)
    def test_profile_race_during_download(self):
        def race(s,p):
            p.write_bytes(self.payload);(self.root/'mods').mkdir();(self.root/'mods/sample-1.jar').write_bytes(b'concurrent')
        with self.assertRaises(u.SafetyError):u.apply(self.root,self.m,fetch=race)
        self.assertEqual((self.root/'mods/sample-1.jar').read_bytes(),b'concurrent')
    def test_runtime_java_17_and_forge_metadata(self):
        java=self.base/'java.exe';java.write_bytes(b'fixture')
        vdir=self.base/'1.20.1-forge-47.4.10';vdir.mkdir();version=vdir/(vdir.name+'.json')
        version.write_text(json.dumps({'libraries':[{'name':'net.minecraftforge:fmlloader:1.20.1-47.4.10'}]}))
        result=type('Result',(),{'returncode':0,'stdout':'','stderr':'openjdk version "17.0.16"'})()
        with patch.object(u.subprocess,'run',return_value=result):self.assertIn('подтверждены',u.check_runtime(java,version))
        result.stderr='openjdk version "21.0.1"'
        with patch.object(u.subprocess,'run',return_value=result):
            with self.assertRaises(u.SafetyError):u.check_runtime(java,version)
    def test_unexpected_language_provider_conflict(self):
        self.m['mods'][0]['providers']=['example.LanguageProvider']
        (self.root/'mods').mkdir()
        with zipfile.ZipFile(self.root/'mods/provider.jar','w') as z:z.writestr(u.SERVICE,'example.LanguageProvider')
        self.assertTrue(u.inspect_profile(self.root,self.m)['conflicts'])
    def test_tampered_state_cannot_own_settings(self):
        (self.root/u.MANAGER).mkdir()
        (self.root/u.STATE).write_text(json.dumps({'schema':1,'managed':{'bad':{'path':'options.txt','sha256':'a'*64}}}))
        with self.assertRaises(u.SafetyError):self.install()

    def test_java_vendor_outputs_and_actual_wrong_version_message(self):
        java=self.base/'java.exe';java.write_bytes(b'fixture')
        outputs=[('openjdk version "17.0.15" 2025-04-15\nOpenJDK Runtime Environment Microsoft-11351406','17.0.15'),
                 ('openjdk version "17.0.16" 2025-07-15\nOpenJDK Runtime Environment Temurin-17.0.16+8','17.0.16'),
                 ('java version "17.0.12" 2024-07-16 LTS\nJava(TM) SE Runtime Environment','17.0.12'),
                 ('openjdk version "17.0.10" 2024-01-16 LTS\nOpenJDK Runtime Environment Zulu17.48+15','17.0.10'),
                 ('openjdk version "21.0.7" 2025-04-15','21.0.7')]
        for text,expected in outputs:
            for channel in ('stdout','stderr'):
                result=type('Result',(),{'returncode':0,'stdout':'','stderr':'',channel:text})()
                with patch.object(u.subprocess,'run',return_value=result):self.assertEqual(u.java_version(java),expected)
        with patch.object(u,'java_version',return_value='21.0.7'):
            with self.assertRaisesRegex(u.SafetyError,'21.0.7'):u.check_runtime(java,self.base/'unused.json')

    def make_runtime_fixture(self):
        appdata=self.base/'AppData';mc=appdata/'.minecraft'
        good=mc/'runtime/java-runtime-gamma/windows/java-runtime-gamma/bin/java.exe'
        bad=mc/'runtime/java-runtime-delta/windows/java-runtime-delta/bin/java.exe'
        outside=appdata/'unrelated/bin/java.exe'
        for p in (good,bad,outside):p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'fixture')
        version=mc/'versions/1.20.1-forge-47.4.10/1.20.1-forge-47.4.10.json'
        version.parent.mkdir(parents=True);version.write_text(json.dumps({'libraries':[{'name':'net.minecraftforge:forge:1.20.1-47.4.10'}]}))
        (mc/'launcher_profiles.json').write_bytes(b'DO NOT READ')
        (mc/'accounts.json').write_bytes(b'DO NOT READ')
        return appdata,good,bad,outside,version

    def test_auto_finds_gamma_and_skips_java21_without_scanning_other_dirs(self):
        appdata,good,bad,outside,version=self.make_runtime_fixture()
        def fake_java(path,timeout=15):
            self.assertNotEqual(path,outside)
            return '17.0.15' if path==good else '21.0.7'
        before={str(p):p.read_bytes() for p in appdata.rglob('*') if p.is_file()}
        with patch.dict(os.environ,{'APPDATA':str(appdata)}),patch.object(u,'java_version',fake_java):
            found=u.discover_runtime()
        self.assertEqual([x['path'] for x in found['java']],[str(good)])
        self.assertEqual(found['forge'][0]['path'],str(version))
        self.assertEqual(found['skipped_java'],1)
        self.assertEqual(before,{str(p):p.read_bytes() for p in appdata.rglob('*') if p.is_file()})

    def test_auto_prioritizes_selected_profile_metadata(self):
        appdata,good,bad,outside,version=self.make_runtime_fixture()
        own=self.root/(self.root.name+'.json');own.write_text(version.read_text())
        with patch.dict(os.environ,{'APPDATA':str(appdata)}),patch.object(u,'java_version',return_value='17.0.15'):
            found=u.discover_runtime(self.root)
        self.assertEqual(found['forge'][0]['path'],str(own))

    def test_auto_missing_runtime_gives_official_java_install_instruction(self):
        with patch.dict(os.environ,{'APPDATA':str(self.base/'missing')}):found=u.discover_runtime(self.root)
        self.assertEqual(found['java'],[]);self.assertEqual(found['forge'],[])
        self.assertIn('Temurin 17',u.runtime_help(found));self.assertIn('не гарантирует',u.runtime_help(found))

    def test_auto_depth_limit(self):
        mc=self.base/'AppData/.minecraft';deep=mc/'runtime/a/b/c/d/e/f/g/bin/java.exe'
        deep.parent.mkdir(parents=True);deep.write_bytes(b'not executed')
        with patch.dict(os.environ,{'APPDATA':str(self.base/'AppData')}),patch.object(u,'java_version',side_effect=AssertionError('out of scope')):
            self.assertEqual(u.discover_runtime()['java'],[])

    def test_create_dedicated_profile_and_preserve_existing(self):
        old=self.base/'Minecraft-AEM';old.mkdir();(old/'accounts.json').write_bytes(b'not read or changed')
        created=u.create_profile(self.base)
        self.assertEqual(created.name,'Minecraft-AEM-2')
        self.assertEqual(list(created.iterdir()),[])
        self.assertEqual((old/'accounts.json').read_bytes(),b'not read or changed')
        self.assertEqual(u.profile_path(created),created)

    def test_create_profile_rejects_network_parent(self):
        with self.assertRaises(u.SafetyError):u.create_profile('//server/share')

    def test_official_default_minecraft_preserves_launcher_accounts_and_saves(self):
        self.root=self.base/'.minecraft';self.root.mkdir()
        sentinels={'launcher_profiles.json':b'private launcher profile','launcher_accounts.json':b'private account','accounts.json':b'private token','saves/world/level.dat':b'world','config/keep.toml':b'custom config','mods/extra.jar':jar('other')}
        for rel,data in sentinels.items():
            p=self.root/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
        original=u.read_json
        def guarded(path,*args,**kwargs):
            self.assertNotIn(path.name,('launcher_profiles.json','launcher_accounts.json','accounts.json'))
            return original(path,*args,**kwargs)
        with patch.object(u,'read_json',guarded):
            self.install();u.rollback(self.root)
        for rel,data in sentinels.items():self.assertEqual((self.root/rel).read_bytes(),data)

    def test_game_discovery_single_shared_root_selects_for_either_launcher(self):
        app=self.base/'AppData';mc=app/'.minecraft';mc.mkdir(parents=True)
        (mc/'accounts.json').write_bytes(b'NEVER READ');(mc/'launcher_profiles.json').write_bytes(b'NEVER READ')
        with patch.dict(os.environ,{'APPDATA':str(app)}),patch.object(u,'read_json',side_effect=AssertionError('no JSON expected')):
            found=u.discover_game_folders()
        self.assertEqual(len(found['candidates']),1)
        for launcher in ('TLauncher','Официальный Minecraft Launcher'):
            self.assertEqual(u.choose_game_folder(u.game_folder_candidates(found,launcher)),str(mc))
        self.assertEqual(found['candidates'][0]['mods'],0)

    def test_game_discovery_multiple_requires_choice_and_counts_without_reading_jars(self):
        app=self.base/'AppData';mc=app/'.minecraft';profile=mc/'versions/CustomPack';mods=profile/'mods';mods.mkdir(parents=True)
        for name in ('one.jar','two.jar'):(mods/name).write_bytes(b'contents must not be read')
        (profile/'CustomPack.json').write_text(json.dumps({'libraries':[{'name':'net.minecraftforge:fmlloader:1.20.1-47.4.10'}]}))
        original=u.read_json
        def guarded(path,*args):
            self.assertEqual(path.name,'CustomPack.json');return original(path,*args)
        with patch.dict(os.environ,{'APPDATA':str(app)}),patch.object(u,'read_json',guarded),patch.object(u,'metadata',side_effect=AssertionError('JAR content read')):
            found=u.discover_game_folders()
        self.assertEqual(len(found['candidates']),2);self.assertEqual(u.choose_game_folder(found['candidates']),'')
        row=next(c for c in found['candidates'] if c['path']==str(profile))
        self.assertEqual(row['mods'],2);self.assertIn('47.4.10',row['version'])
        self.assertEqual(u.choose_game_folder(found['candidates'],str(profile)),str(profile))

    def test_version_installation_without_own_mods_is_not_game_folder(self):
        app,good,bad,outside,version=self.make_runtime_fixture()
        with patch.dict(os.environ,{'APPDATA':str(app)}):found=u.discover_game_folders()
        self.assertEqual(len(found['candidates']),1)
        self.assertNotEqual(found['candidates'][0]['path'],str(version.parent))
        self.assertIn('доступна',found['candidates'][0]['version'])

    def test_selected_container_finds_custom_profile_but_not_saves(self):
        container=self.base/'Games';pack=container/'Custom';(pack/'mods').mkdir(parents=True)
        (pack/'mods/a.jar').write_bytes(b'not read')
        secret=container/'saves/private/mods';secret.mkdir(parents=True);(secret/'private.jar').write_bytes(b'not read')
        with patch.dict(os.environ,{'APPDATA':str(self.base/'missing')}):found=u.discover_game_folders(container)
        self.assertEqual([c['path'] for c in found['candidates']],[str(pack)])

    def test_game_discovery_rejects_symlink_profiles(self):
        app=self.base/'AppData';versions=app/'.minecraft/versions';versions.mkdir(parents=True)
        outside=self.base/'outside';(outside/'mods').mkdir(parents=True)
        try:(versions/'linked').symlink_to(outside,target_is_directory=True)
        except OSError:self.skipTest('symlinks unavailable')
        with patch.dict(os.environ,{'APPDATA':str(app)}):found=u.discover_game_folders()
        self.assertEqual(len(found['candidates']),1)

    def test_game_discovery_does_not_search_unrelated_disk_tree(self):
        app=self.base/'AppData';(app/'.minecraft').mkdir(parents=True)
        outside=self.base/'Unrelated/game/mods';outside.mkdir(parents=True);(outside/'a.jar').write_bytes(b'not read')
        with patch.dict(os.environ,{'APPDATA':str(app)}):found=u.discover_game_folders()
        self.assertFalse(any('Unrelated' in c['path'] for c in found['candidates']))

    def test_known_java17_vendor_folder_is_found(self):
        programs=self.base/'Program Files';java=programs/'Eclipse Adoptium/jdk-17.0.15-hotspot/bin/java.exe'
        java.parent.mkdir(parents=True);java.write_bytes(b'fixture')
        with patch.dict(os.environ,{'APPDATA':str(self.base/'missing'),'ProgramFiles':str(programs)}),patch.object(u,'java_version',return_value='17.0.15'):
            found=u.discover_runtime()
        self.assertEqual([x['path'] for x in found['java']],[str(java)])

    @unittest.skipUnless(os.name=='nt','Windows transport')
    def test_tls_failure_logged_without_secrets_and_without_commit(self):
        import native_http
        spec=self.m['mods'][0]
        with patch.object(native_http,'stream',side_effect=native_http.WindowsDownloadError(12175,'проверка TLS',8)):
            with self.assertRaisesRegex(u.SafetyError,'cdn.modrinth.com'):u.download(spec,self.base/'download.jar')
        log=json.loads((self.base/'download-error.json').read_text('utf-8'))
        self.assertEqual(log['domain'],'cdn.modrinth.com');self.assertEqual(log['tls_flags'],8)
        self.assertNotIn(str(self.base),json.dumps(log));self.assertFalse((self.root/'mods').exists())

    @unittest.skipUnless(os.name=='nt','Windows transport')
    def test_native_http_result_still_requires_sha256(self):
        import native_http
        def corrupt(url,stream,maximum):stream.write(b'x'*maximum);return maximum
        with patch.object(native_http,'stream',corrupt):
            with self.assertRaisesRegex(u.SafetyError,'SHA256'):u.download(self.m['mods'][0],self.base/'bad.jar')

if __name__=='__main__':unittest.main(verbosity=2)
