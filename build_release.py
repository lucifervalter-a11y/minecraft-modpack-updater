"""Build on Windows; do not package Minecraft JARs or personal profiles."""
import hashlib
from pathlib import Path
import subprocess
import sys
import zipfile

root=Path(__file__).parent
downloads=root/'docs'/'downloads'
downloads.mkdir(exist_ok=True)
subprocess.run([sys.executable,'-m','unittest','-v'],cwd=root,check=True)
subprocess.run([sys.executable,'updater.py','--self-test'],cwd=root,check=True)
subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onefile','--windowed','--name','Minecraft-Modpack-Updater','--add-data','manifest.json:.','--add-data','presets:presets','updater.py'],cwd=root,check=True)
exe=root/'dist'/'Minecraft-Modpack-Updater.exe'
subprocess.run([str(exe),'--self-test'],cwd=root,check=True,timeout=60)
files=['updater.py','manifest.json','Start.cmd','README.md','LICENSE']
for label,include in [('Windows',[exe,root/'README.md',root/'LICENSE',root/'manifest.json']),('Source',[root/p for p in files]+list((root/'presets').iterdir()))]:
    path=downloads/f'Minecraft-Modpack-Updater-{label}.zip'
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for p in include:
            name=p.name if p==exe else p.relative_to(root).as_posix()
            z.write(p,name)
lines=[]
for p in sorted(downloads.glob('*.zip')):
    lines.append(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name)
(downloads/'SHA256SUMS.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print('\n'.join(lines))
