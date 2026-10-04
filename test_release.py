"""r9 contract and upgrade regression tests; all profile operations use temp fixtures."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import updater as u
from release_tools import SOURCE_FILES, source_paths, write_zip

ROOT = Path(__file__).resolve().parent


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def fake_jar(ids, identity):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.comment = identity.encode()
        archive.writestr('META-INF/mods.toml', 'modLoader="javafml"\nloaderVersion="[47,)"\nlicense="MIT"\n' + ''.join('[[mods]]\nmodId="' + name + '"\nversion="1"\n' for name in ids))
    return output.getvalue()


class ReleaseTests(unittest.TestCase):
    def test_manifest_28_mods_and_pinned_additions(self):
        manifest = u.load_manifest()
        self.assertEqual(manifest['release'], 'client-aem-ae2-2026-10-04-r9')
        self.assertEqual(len(manifest['mods']), 28)
        actual = {item['mod_ids'][0]: item for item in manifest['mods'][-2:]}
        expected = {
            'ae2': ('15.4.11', 'appliedenergistics2-forge-15.4.11.jar', 8501309, 'f2ff9ee5122b037d4e93537425ab9fbe49860fc9c3977db13b20a50ca7d30f9e'),
            'guideme': ('20.1.15', 'guideme-20.1.15.jar', 9414827, 'cf8052ab3da121012efc59a20e731c24bfe7488dfa556c9b88809f42dea3b24a'),
        }
        self.assertEqual(set(actual), set(expected))
        for name, values in expected.items():
            self.assertEqual(tuple(actual[name][key] for key in ('version', 'file', 'bytes', 'sha256')), values)
            u.valid_url(actual[name]['download_url'])

    def test_all_previous_manifest_entries_preserved_exactly(self):
        manifest = u.load_manifest()
        # Canonical hashes of the verified GitHub r8 commit, including every entry field.
        self.assertEqual(canonical_hash(manifest['mods'][:26]), '5e543c26e12abd969b8f0b7e9fa8f6de9be34dbfe38e3e78e7c505390ce42807')
        self.assertEqual(canonical_hash(manifest['extras']), '51dea6b794ba91a2e10b68702c6b144eb1fb23fca4b0e0aa3abf895aa5c52531')
        self.assertEqual(canonical_hash(manifest['configs']), 'f8471714987ce94c2bf8e30e564eca492a26e01596ddc6e676096298686005f8')
        self.assertEqual(len(manifest['configs']), 20)
        for item in manifest['configs']:
            self.assertEqual(hashlib.sha256((ROOT / 'presets' / item['bundle']).read_bytes()).hexdigest(), item['sha256'])

    def test_site_manifest_equals_embedded_source_manifest(self):
        self.assertEqual((ROOT / 'manifest.json').read_bytes(), (ROOT / 'docs/manifest.json').read_bytes())

    def test_r8_fixture_upgrade_adds_only_two_jars_and_preserves_everything(self):
        manifest = copy.deepcopy(u.load_manifest())
        payloads = {}
        for spec in manifest['mods'] + manifest['extras']:
            data = fake_jar(spec['mod_ids'], spec['project_id']) if spec['category'] == 'mods' else b'extra fixture ' + spec['project_id'].encode()
            payloads[spec['project_id']] = data
            spec['sha256'] = hashlib.sha256(data).hexdigest()
            spec['bytes'] = len(data)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'game'
            root.mkdir()
            original = {}
            for spec in manifest['mods'][:26] + manifest['extras']:
                target = root / spec['category'] / spec['file']
                target.parent.mkdir(exist_ok=True)
                target.write_bytes(payloads[spec['project_id']])
                original[target.relative_to(root).as_posix()] = target.read_bytes()
            sentinels = {'saves/world/level.dat': b'world sentinel', 'XaeroWaypoints/private.txt': b'markers',
                         'XaeroWorldMap/private.txt': b'map', 'accounts.json': b'private fixture',
                         'launcher_profiles.json': b'private launcher fixture'}
            for spec in manifest['configs']:
                sentinels[spec['path']] = b'custom setting retained'
            for name, value in sentinels.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(value)
            original.update(sentinels)
            requested = []

            def fetch(spec, path):
                requested.append(spec['mod_ids'][0])
                path.write_bytes(payloads[spec['project_id']])

            with patch.object(u, 'require_closed'), patch.object(u, 'load_manifest', lambda: manifest):
                before = u.inspect_profile(root, manifest)
                self.assertEqual(len(before['ops']), 2)
                self.assertIn('26/28', u.verification_summary(before, manifest))
                u.apply(root, manifest, fetch=fetch)
                self.assertEqual(requested, ['ae2', 'guideme'])
                self.assertIn('28/28', u.verification_summary(u.inspect_profile(root, manifest), manifest))
                for name, value in original.items():
                    self.assertEqual((root / name).read_bytes(), value, name)
                u.rollback(root)
                for spec in manifest['mods'][-2:]:
                    self.assertFalse((root / 'mods' / spec['file']).exists())
                for name, value in original.items():
                    self.assertEqual((root / name).read_bytes(), value, name)

    def test_source_archive_is_reproducible_and_contains_no_jars(self):
        with tempfile.TemporaryDirectory() as directory:
            a = Path(directory) / 'a.zip'
            b = Path(directory) / 'b.zip'
            write_zip(a, ROOT, source_paths(ROOT))
            write_zip(b, ROOT, source_paths(ROOT))
            self.assertEqual(a.read_bytes(), b.read_bytes())
            with zipfile.ZipFile(a) as archive:
                self.assertIn('test_release.py', archive.namelist())
                self.assertIn('release_tools.py', archive.namelist())
                self.assertFalse(any(name.endswith(('.jar', '.exe')) for name in archive.namelist()))


if __name__ == '__main__':
    unittest.main()
