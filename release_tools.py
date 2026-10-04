"""Allowlisted release assembly and read-only checks. Never scan a user profile."""
from pathlib import Path
import hashlib
import io
import json
import zipfile

SOURCE_FILES = [
    'updater.py', 'native_http.py', 'manifest.json', 'Start.cmd', 'README.md',
    'LICENSE', 'test_updater.py', 'test_release.py', 'build_release.py',
    'release_tools.py', 'split_download.py', 'requirements-build.txt', '.gitignore', 'VERIFICATION.json',
]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def source_paths(root):
    root = Path(root)
    return ([root / name for name in SOURCE_FILES] + sorted((root / 'presets').iterdir())
            + [root / 'docs' / name for name in ('.nojekyll', 'app.js', 'index.html', 'manifest.json', 'style.css')]
            + sorted((root / '.github' / 'workflows').glob('*.yml')))


def write_zip(path, root, paths):
    """Fixed ZIP timestamps keep a given set of source bytes reproducible."""
    root = Path(root)
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for item in sorted(paths):
            name = item.relative_to(root).as_posix()
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 4, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, item.read_bytes())


def verify_downloads(root, expected_exe=None):
    root = Path(root)
    downloads = root / 'docs' / 'downloads'
    spec = json.loads((downloads / 'windows-download.json').read_text())
    assert spec['schema'] == 1 and spec['file'] == 'Minecraft-Modpack-Updater-Windows.zip'
    assert len(spec['parts']) == 2
    chunks = []
    for index, part in enumerate(spec['parts'], 1):
        assert part['file'] == spec['file'] + '.part' + str(index)
        data = (downloads / part['file']).read_bytes()
        assert len(data) == part['bytes'] and 0 < len(data) < 8_000_000
        assert sha(data) == part['sha256']
        chunks.append(data)
    whole = b''.join(chunks)
    assert len(whole) == spec['bytes'] and sha(whole) == spec['sha256']
    with zipfile.ZipFile(io.BytesIO(whole)) as archive:
        assert set(archive.namelist()) == {'Minecraft-Modpack-Updater.exe', 'README.md', 'LICENSE', 'manifest.json'}
        assert archive.read('manifest.json') == (root / 'manifest.json').read_bytes()
        if expected_exe is not None:
            assert archive.read('Minecraft-Modpack-Updater.exe') == Path(expected_exe).read_bytes()
    with zipfile.ZipFile(downloads / 'Minecraft-Modpack-Updater-Source.zip') as archive:
        expected = {item.relative_to(root).as_posix() for item in source_paths(root)}
        assert set(archive.namelist()) == expected
        for name in expected:
            assert archive.read(name) == (root / name).read_bytes(), name
    checksums = {}
    for line in (downloads / 'SHA256SUMS.txt').read_text().splitlines():
        value, filename = line.split('  ', 1)
        checksums[filename] = value
    assert checksums[spec['file']] == spec['sha256']
    for name, value in checksums.items():
        path = downloads / name
        assert path.name == name
        if path.exists():
            assert sha(path.read_bytes()) == value, name
    return spec


def write_public_inventory(root):
    root = Path(root)
    paths = source_paths(root) + [root / '.gitignore', root / 'VERIFICATION.json']
    paths += sorted((root / '.github' / 'workflows').glob('*.yml'))
    paths += [root / 'docs' / name for name in ('.nojekyll', 'app.js', 'index.html', 'manifest.json', 'style.css')]
    names = ('Minecraft-Modpack-Updater-Source.zip',
             'Minecraft-Modpack-Updater-Windows.zip.part1', 'Minecraft-Modpack-Updater-Windows.zip.part2',
             'SHA256SUMS.txt', 'windows-download.json', 'BUILD-VERIFICATION.json')
    paths += [root / 'docs' / 'downloads' / name for name in names]
    rows = [{'path': item.relative_to(root).as_posix(), 'bytes': item.stat().st_size,
             'sha256': sha(item.read_bytes())} for item in sorted(set(paths))]
    result = {'schema': 1, 'root': 'minecraft-modpack-updater', 'files': rows,
              'publication_note': 'Publish the two ZIP parts, not the unsplit Windows ZIP. Publish only after exact-commit build verification.',
              'excluded': ['unsplit Windows ZIP', 'build cache', 'personal profiles', 'JAR binaries', 'worlds', 'accounts', 'tokens', 'waypoints']}
    (root / 'PUBLIC-FILES.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    return result
