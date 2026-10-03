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
        (self.root/'launcher_profiles.json').write_bytes(b'not to read')
        with self.assertRaises(u.SafetyError):u.profile_path(self.root)
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

if __name__=='__main__':unittest.main(verbosity=2)
