"""Build on Windows. No Minecraft JARs, credentials, or personal profiles are packaged."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

from release_tools import source_paths, verify_downloads, write_public_inventory, write_zip
from split_download import split


def main():
    if sys.platform != 'win32':
        raise SystemExit('A Windows runner is required to build the Windows EXE.')
    root = Path(__file__).resolve().parent
    downloads = root / 'docs' / 'downloads'
    downloads.mkdir(exist_ok=True)
    subprocess.run([sys.executable, '-m', 'unittest', '-v'], cwd=root, check=True)
    subprocess.run([sys.executable, 'updater.py', '--self-test'], cwd=root, check=True)
    assert (root / 'manifest.json').read_bytes() == (root / 'docs' / 'manifest.json').read_bytes()
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
                    '--name', 'Minecraft-Modpack-Updater', '--add-data', 'manifest.json:.',
                    '--add-data', 'presets:presets', 'updater.py'], cwd=root, check=True)
    exe = root / 'dist' / 'Minecraft-Modpack-Updater.exe'
    from PyInstaller.archive.readers import CArchiveReader
    bundled = CArchiveReader(str(exe))
    assert bundled.extract('manifest.json') == (root / 'manifest.json').read_bytes()
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    for config in manifest['configs']:
        name = 'presets/' + config['bundle']
        # PyInstaller may use OS separators in CArchive names.
        key = name if name in bundled.toc else name.replace('/', '\\')
        assert hashlib.sha256(bundled.extract(key)).hexdigest() == config['sha256']
    subprocess.run([str(exe), '--self-test'], cwd=root, check=True, timeout=90)
    subprocess.run([str(exe), '--self-test-download'], cwd=root, check=True, timeout=120)
    with zipfile.ZipFile(downloads / 'Minecraft-Modpack-Updater-Windows.zip', 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for item in [exe, root / 'README.md', root / 'LICENSE', root / 'manifest.json']:
            archive.write(item, item.name)
    write_zip(downloads / 'Minecraft-Modpack-Updater-Source.zip', root, source_paths(root))
    spec = split(downloads)
    lines = []
    for path in sorted(p for p in downloads.iterdir() if p.suffix == '.zip' or p.suffix in ('.part1', '.part2')):
        lines.append(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name)
    (downloads / 'SHA256SUMS.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    verify_downloads(root, expected_exe=exe)
    report = {
        'release': 'r9', 'pack_release': manifest['release'],
        'commit': os.environ.get('GITHUB_SHA', 'local-build'),
        'run_url': ('https://github.com/' + os.environ['GITHUB_REPOSITORY'] + '/actions/runs/' + os.environ['GITHUB_RUN_ID']) if os.environ.get('GITHUB_RUN_ID') else None,
        'python': sys.version.split()[0], 'pyinstaller': '6.22.3',
        'unit_tests': 'PASS', 'source_self_test': 'PASS', 'frozen_self_test': 'PASS',
        'frozen_manifest_matches_source': True, 'frozen_presets_match_manifest': True,
        'frozen_official_https_download': 'PASS', 'zip_parts_and_full_sha256': 'PASS',
        'source_archive_matches_source': True,
        'manifest_sha256': hashlib.sha256((root / 'manifest.json').read_bytes()).hexdigest(),
        'exe_sha256': hashlib.sha256(exe.read_bytes()).hexdigest(), 'windows_zip_sha256': spec['sha256'],
        'mods': len(manifest['mods']), 'configs': len(manifest['configs']),
        'game_launch': 'NOT RUN', 'NAS_install': 'NOT PERFORMED', 'live_profile_writes': 0,
    }
    (downloads / 'BUILD-VERIFICATION.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    write_public_inventory(root)
    print(json.dumps(report, indent=2))
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
