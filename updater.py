"""Local, explicit-folder Minecraft client updater. Python 3.11+; no services."""
from __future__ import annotations
import contextlib
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.parse
import urllib.request
import uuid
import zipfile

BASE = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
MANIFEST_SHA256 = 'c7242af5d845af5a9f9a940501d857a7d1d5f4b94810f2bb81f411dae11ac173'
MANAGER = '.modpack-updater'
STATE = MANAGER + '/state.json'
SERVICE = 'META-INF/services/net.minecraftforge.forgespi.language.IModLanguageProvider'

class SafetyError(Exception):
    pass

def digest(path):
    safe_path(path)
    if not path.is_file():
        raise SafetyError('Ожидался обычный файл: ' + path.name)
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def safe_name(name):
    if not isinstance(name, str) or not name or name in ('.', '..') or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name[-1] in ' .':
        raise SafetyError('Небезопасное имя файла')
    if re.match(r'(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)', name):
        raise SafetyError('Зарезервированное имя файла')
    return name

def safe_path(path):
    path = Path(path)
    if not path.is_absolute() or str(path).startswith(('\\\\', '//')) or '..' in path.parts:
        raise SafetyError('Нужен абсолютный путь к локальной папке, без переходов ..')
    if os.name == 'nt':
        if ctypes.windll.kernel32.GetDriveTypeW(path.anchor) != 3:
            raise SafetyError('Выберите папку на локальном фиксированном диске. Сетевые диски не поддерживаются.')
        for part in path.parts[1:]:
            safe_name(part)
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise SafetyError('Ссылки и junction не поддерживаются: ' + item.name)
    return path

def profile_path(value):
    p = safe_path(Path(value))
    mods = safe_path(p/'mods')
    # A TLauncher game directory may itself be named Mods. Its child mods/
    # distinguishes that game directory from the actual JAR directory.
    named_mods_profile = p.name.lower()=='mods' and mods.is_dir()
    if not p.is_dir() or p == Path(p.anchor) or (p.name.lower() in ('mods','config','saves','versions') and not named_mods_profile):
        raise SafetyError('Выберите папку игры, внутри которой находится mods. Не выбирайте саму mods, versions или корень диска. Для официального Launcher обычно подходит .minecraft.')
    safe_path(p/MANAGER)
    return p

def create_profile(parent):
    """User-chosen parent; create exactly one dedicated child, never launcher root."""
    parent=safe_path(Path(parent))
    if not parent.is_dir():raise SafetyError('Выберите существующую папку, внутри которой создать сборку.')
    for suffix in ('', '-2', '-3', '-4', '-5', '-6', '-7', '-8', '-9'):
        folder=safe_path(parent/('Minecraft-AEM'+suffix))
        try:folder.mkdir()
        except FileExistsError:continue
        return profile_path(folder)
    raise SafetyError('Здесь уже есть несколько папок Minecraft-AEM. Выберите другую папку для новой сборки.')

def target(root, relative):
    if relative == STATE:
        return safe_path(root / relative)
    parts = relative.split('/')
    if len(parts) != 2 or parts[0] not in ('mods','config','shaderpacks','resourcepacks'):
        if relative == 'options.txt':
            return safe_path(root/relative)
        raise SafetyError('Путь вне управляемого набора')
    safe_name(parts[1])
    if parts[0]=='mods' and not parts[1].lower().endswith('.jar'):
        raise SafetyError('Ожидался JAR')
    return safe_path(root / relative)

def read_json(path, limit=2_000_000):
    safe_path(path)
    if path.stat().st_size > limit:
        raise SafetyError('Слишком большой JSON: '+path.name)
    return json.loads(path.read_text('utf-8-sig'))

def load_manifest():
    path = BASE/'manifest.json'
    if digest(path) != MANIFEST_SHA256:
        raise SafetyError('Манифест изменён. Скачайте целый официальный выпуск установщика.')
    m = read_json(path)
    if m.get('schema') != 1 or (m['minecraft'], m['forge'], m['java']) != ('1.20.1','47.4.10',17):
        raise SafetyError('Неподдерживаемый манифест')
    seen=set()
    for spec in m['mods']+m.get('extras',[]):
        safe_name(spec['file'])
        if spec['project_id'] in seen:
            raise SafetyError('Повтор проекта в манифесте')
        seen.add(spec['project_id'])
        if not re.fullmatch('[0-9a-f]{64}',spec['sha256']) or not 0 < spec['bytes'] < 300_000_000:
            raise SafetyError('Некорректная контрольная сумма/размер')
        valid_url(spec['download_url'])
    return m

def valid_url(url):
    u=urllib.parse.urlsplit(url)
    if u.scheme!='https' or u.hostname!='cdn.modrinth.com' or u.port not in (None,443) or u.username or u.password or u.query or u.fragment or not u.path.startswith('/data/'):
        raise SafetyError('Разрешены только прямые HTTPS URL официального CDN Modrinth')

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SafetyError('Неожиданное перенаправление при скачивании')

def download(spec, dest):
    valid_url(spec['download_url'])
    safe_path(dest)
    if os.name=='nt':
        import native_http
        try:
            with dest.open('xb') as output:size=native_http.stream(spec['download_url'],output,spec['bytes'])
        except native_http.WindowsDownloadError as error:
            host=urllib.parse.urlsplit(spec['download_url']).hostname
            details={'utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'transport':'Windows WinHTTP/SChannel','domain':host,'winhttp_code':error.code,'tls_flags':error.flags,'http_status':error.status,'stage':error.stage,'reason':error.reason,'certificate_verification':'required','profile_files_changed':False}
            try:save_json(dest.parent/'download-error.json',details)
            except OSError:pass
            raise SafetyError(f'Загрузка с {host} остановлена. WinHTTP {error.code}: {error.reason}. Проверка сертификатов сохранена. Проверьте дату Windows, обновления сертификатов и доступ к этому домену через свою сеть. Диагностика: download-error.json в папке backup этой операции. Отключать TLS-проверку не нужно.') from error
    else:
        request=urllib.request.Request(spec['download_url'],headers={'User-Agent':'MinecraftModpackUpdater/r8'})
        opener=urllib.request.build_opener(NoRedirect());size=0
        with opener.open(request,timeout=45) as response, dest.open('xb') as output:
            while block:=response.read(1024*1024):
                size+=len(block)
                if size>spec['bytes']:raise SafetyError('Размер скачанного файла больше ожидаемого')
                output.write(block)
    if size!=spec['bytes'] or digest(dest)!=spec['sha256']:
        raise SafetyError('SHA256 скачанного файла не совпадает: '+spec['file'])

def metadata(path):
    """Only metadata inside a directly selected mods directory; never extract."""
    with zipfile.ZipFile(path) as z:
        def entry(name):
            try: info=z.getinfo(name)
            except KeyError: return ''
            if info.file_size > 2_000_000:
                raise SafetyError('Слишком большие метаданные мода: '+path.name)
            return z.read(info).decode('utf-8-sig')
        text=entry('META-INF/mods.toml')
        data=tomllib.loads(text) if text else {}
        return {m['modId'] for m in data.get('mods',[])}, {s.strip() for s in entry(SERVICE).splitlines() if s.strip() and not s.startswith('#')}

def inspect_profile(root, manifest):
    root=profile_path(root)
    state_path=target(root,STATE)
    state=read_json(state_path) if state_path.exists() else {'schema':1,'managed':{}}
    if state.get('schema')!=1 or not isinstance(state.get('managed'),dict):
        raise SafetyError('Неподдерживаемое состояние обновлятора')
    previous=state['managed']
    for old in previous.values():
        target(root,old['path'])
        if old['path'].split('/')[0] not in ('mods','shaderpacks','resourcepacks'):
            raise SafetyError('Недопустимый путь в состоянии модов')
        if not re.fullmatch('[0-9a-f]{64}',old['sha256']):
            raise SafetyError('Повреждено состояние обновлятора')
    inventory={}
    modsdir=root/'mods'
    if modsdir.exists():
        for p in modsdir.iterdir():
            if p.suffix.lower()!='.jar': continue
            safe_path(p)
            if not p.is_file() or p.stat().st_size>300_000_000:
                raise SafetyError('Некорректный JAR: '+p.name)
            inventory[p.name]=digest(p)
    expected_ids=set().union(*(set(m['mod_ids']) for m in manifest['mods']))
    expected_providers=set().union(*(set(m.get('providers',[])) for m in manifest['mods']))
    desired_hashes={s['sha256'] for s in manifest['mods']}
    owned={v['path']:v['sha256'] for v in previous.values()}
    conflicts=[]
    outsiders=[]
    for name,sha in inventory.items():
        if sha in desired_hashes or owned.get('mods/'+name)==sha: continue
        outsiders.append(name)
        try:
            ids,providers=metadata(modsdir/name)
        except (ValueError,KeyError,zipfile.BadZipFile,UnicodeError) as e:
            conflicts.append(name+': не удалось безопасно прочитать метаданные JAR')
            continue
        overlap=ids & expected_ids or providers & expected_providers
        if overlap:
            conflicts.append(name+': другая неуправляемая версия '+', '.join(sorted(overlap)))
    ops={}
    managed={}
    rows=[]
    for spec in manifest['mods']+manifest.get('extras',[]):
        key=spec['project_id']
        rel=spec['category']+'/'+spec['file']
        dest=target(root,rel)
        matches=[n for n,h in inventory.items() if h==spec['sha256']] if spec['category']=='mods' else []
        if len(matches)>1:
            conflicts.append(spec['name']+': несколько одинаковых JAR; уберите дубликат вручную')
        if matches:
            rel='mods/'+matches[0]
            dest=target(root,rel)
        actual=digest(dest) if dest.exists() else None
        old=previous.get(key)
        if actual==spec['sha256']:
            status='OK'
        else:
            status='Скачать' if actual is None else 'Обновить'
            if actual and (not old or old['path']!=rel or old['sha256']!=actual):
                conflicts.append(rel+': файл не принадлежит обновлятору, перезапись запрещена')
            ops[rel]={'path':rel,'before':actual,'after':spec['sha256'],'spec':spec}
        if old and old['path']!=rel:
            oldpath=target(root,old['path'])
            if oldpath.exists():
                if digest(oldpath)!=old['sha256']:
                    conflicts.append(old['path']+': управляемый файл изменён вручную')
                else:
                    ops[old['path']]={'path':old['path'],'before':old['sha256'],'after':None}
        managed[key]={'path':rel,'sha256':spec['sha256']}
        rows.append({'name':spec['name'],'version':spec['version'],'status':status})
    # Removed projects are deliberately left in place; no broad deletion policy.
    for key,old in previous.items():
        if key not in managed: managed[key]=old
    for spec in manifest.get('configs',[]):
        dest=target(root,spec['path'])
        if not dest.exists():
            ops[spec['path']]={'path':spec['path'],'before':None,'after':spec['sha256'],'bundled':spec['bundle']}
    return {'root':root,'rows':rows,'ops':list(ops.values()),'managed':managed,'conflicts':conflicts,'outsiders':outsiders,'jar_count':len(inventory)}

def verification_summary(plan, manifest):
    count=len(manifest['mods']);mods=plan['rows'][:count];extras=plan['rows'][count:]
    matched=sum(row['status']=='OK' for row in mods)
    extra_matched=sum(row['status']=='OK' for row in extras)
    complete=not plan['conflicts'] and all(row['status']=='OK' for row in plan['rows'])
    label='Файлы набора проверены' if complete else 'Сборка НЕ готова'
    summary=f"{label}: моды {matched}/{count}, дополнения {extra_matched}/{len(extras)}."
    if plan['conflicts']:summary+=f" Конфликтов: {len(plan['conflicts'])}."
    elif plan['ops']:summary+=f" Файлов для установки/обновления: {len(plan['ops'])}."
    return summary

def verification_receipt(plan, manifest, action, runtime, operation=''):
    lines=['Minecraft Modpack Updater r8',time.strftime('Проверено: %Y-%m-%d %H:%M:%S UTC',time.gmtime()),
        'Действие: '+action,'Набор: '+manifest['release'],'Папка игры: '+str(plan['root']),
        'Папка модов: '+str(plan['root']/'mods'),f"JAR в выбранной папке: {plan['jar_count']}",
        verification_summary(plan,manifest),'Java / Forge: '+runtime]
    if operation:lines.append('Операция: '+operation)
    lines.append('Проверка относится только к указанной папке; активная папка запущенного Minecraft автоматически не определялась.')
    lines.extend(f"[{row['status']}] {row['name']} {row['version']}" for row in plan['rows'])
    lines.extend('КОНФЛИКТ: '+entry for entry in plan['conflicts'])
    return '\n'.join(lines)

def java_version(java_path, timeout=15):
    java=safe_path(Path(java_path))
    if java.name.lower() not in ('java.exe','java') or not java.is_file():
        raise SafetyError('Выберите исполняемый файл bin/java.exe из Java 17')
    result=subprocess.run([str(java),'-version'],capture_output=True,text=True,errors='replace',timeout=timeout,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    match=re.search(r'(?:openjdk|java) version "([^"\s]+)',result.stderr+'\n'+result.stdout)
    if result.returncode!=0 or not match:
        raise SafetyError('Не удалось определить версию Java')
    return match[1]

def forge_metadata(version_path):
    version=safe_path(Path(version_path))
    if version.suffix.lower()!='.json' or version.stem!=version.parent.name or re.search(r'(?i)account|token|launcher_profile',version.name):
        raise SafetyError('Выберите versions/<версия>/<версия>.json, а не файл настроек лаунчера')
    data=read_json(version)
    libraries={item.get('name','') for item in data.get('libraries',[]) if isinstance(item,dict)}
    pins={'net.minecraftforge:forge:1.20.1-47.4.10','net.minecraftforge:fmlloader:1.20.1-47.4.10'}
    if not libraries & pins:
        raise SafetyError('В выбранном JSON не найден Forge 47.4.10 для Minecraft 1.20.1')
    if data.get('javaVersion',{}).get('majorVersion',17)!=17:
        raise SafetyError('Версия требует другую Java')
    return True

def check_runtime(java_path, version_path):
    actual=java_version(java_path)
    if actual.split('.')[0]!='17':
        raise SafetyError(f'Выбрана Java {actual}, а для этой сборки нужна Java 17. Нажмите «Найти папки, Java и Forge». Если Java 17 нет, кнопка «Java 17…» откроет официальный Temurin: установите Java 17 для Windows x64 и повторите поиск.')
    forge_metadata(version_path)
    return 'Minecraft 1.20.1 / Forge 47.4.10: метаданные подтверждены. Выбранная Java: 17.'

def discovery_roots(selected=None):
    """Fixed launcher subdirectories only. Never enumerate the home/drive root."""
    roots=[]; direct=[]
    if selected:
        p=profile_path(selected)
        roots.append(('Выбранная папка',p))
        direct.append(p/(p.name+'.json'))
        if p.parent.name.lower()=='versions':
            roots.append(('Лаунчер выбранной сборки',safe_path(p.parent.parent)))
    appdata=os.environ.get('APPDATA')
    if appdata:
        roots.extend([('Minecraft',Path(appdata)/'.minecraft'),('TLauncher',Path(appdata)/'.tlauncher')])
    runtime=[];versions=[]
    for label,base in roots:
        for name in ('runtime','jvms','jre','java'):
            runtime.append((label,base/name))
        versions.append((label,base/'versions'))
    # Known JDK installation locations, only version-17 directory names.
    # This is not a Program Files scan: only these four vendor directories.
    program_files=os.environ.get('ProgramFiles')
    if program_files:
        for vendor in ('Eclipse Adoptium','Microsoft','Java','Zulu'):
            vendor_root=Path(program_files)/vendor
            try:
                safe_path(vendor_root)
                if not vendor_root.is_dir():continue
                for i,child in enumerate(vendor_root.iterdir()):
                    if i>=40:break
                    if re.match(r'(?i)^(?:jdk|jre|zulu)[-_.]?17(?:[._+\-]|$)',child.name):
                        safe_path(child)
                        if child.is_dir():runtime.append((vendor,child))
            except (OSError,SafetyError):continue
    return runtime,versions,direct

def game_version_info(path):
    """Read only an explicitly located version JSON, never launcher profiles."""
    safe_path(path)
    if path.suffix.lower()!='.json' or path.stem!=path.parent.name or re.search(r'(?i)account|token|launcher_profile',path.name):
        raise SafetyError('Не файл метаданных версии')
    data=read_json(path)
    for item in data.get('libraries',[]):
        if not isinstance(item,dict):continue
        match=re.fullmatch(r'net\.minecraftforge:(?:forge|fmlloader):(\d+\.\d+(?:\.\d+)?)-([\d.]+)(?::.*)?',item.get('name',''))
        if match:return {'minecraft':match[1],'loader':'Forge '+match[2],'target':match[1]=='1.20.1' and match[2]=='47.4.10','path':str(path)}
    mc=str(data.get('inheritsFrom',''))
    if not re.fullmatch(r'\d+\.\d+(?:\.\d+)?',mc):
        mc=str(data.get('id',''))
        if not re.fullmatch(r'\d+\.\d+(?:\.\d+)?',mc):mc='версия не определена'
    fabric=any(isinstance(x,dict) and x.get('name','').startswith('net.fabricmc:fabric-loader:') for x in data.get('libraries',[]))
    return {'minecraft':mc,'loader':'Fabric' if fabric else 'без подтверждения Forge','target':False,'path':str(path)}

def discover_game_folders(selected=None):
    """Bounded directory discovery; counts filenames, never reads JARs or accounts."""
    roots=[];explicit=None
    appdata=os.environ.get('APPDATA')
    if appdata:
        roots=[(Path(appdata)/'.minecraft','shared'),(Path(appdata)/'.tlauncher','tlauncher')]
    if selected:
        explicit=safe_path(Path(selected))
        if not explicit.is_dir():raise SafetyError('Выберите существующую локальную папку')
        if explicit.name.lower()=='versions':roots.insert(0,(explicit.parent,'custom'))
        else:
            profile_path(explicit)
            roots.insert(0,(explicit,'manual'))
            if explicit.parent.name.lower()=='versions':roots.insert(1,(explicit.parent.parent,'custom'))
    found=[];seen=set();visited=set();truncated=False
    def add(folder,kind,info=None,available=False):
        nonlocal truncated
        key=str(folder).lower()
        if key in seen:return
        try:
            profile_path(folder)
            mods=safe_path(folder/'mods');count=0;entries=0
            if mods.is_dir():
                for p in mods.iterdir():
                    entries+=1
                    if entries>2000:truncated=True;break
                    if p.suffix.lower()=='.jar':
                        safe_path(p)
                        if p.is_file():count+=1
            if info is None:
                version_file=folder/(folder.name+'.json')
                if version_file.is_file():
                    try:info=game_version_info(version_file)
                    except (OSError,ValueError,TypeError,AttributeError,SafetyError):pass
            version=(info['minecraft']+' / '+info['loader']) if info else 'версия не определена'
            if info and available:version+=' (доступна)'
            launcher={'shared':'Official / TLauncher','tlauncher':'TLauncher','version':'TLauncher / профиль','manual':'Выбранная папка','custom':'Своя папка'}.get(kind,kind)
            name=folder.name
            short_name=name if len(name)<=30 else name[:29]+'…'
            found.append({'path':str(folder),'kind':kind,'launcher':launcher,'name':name,'version':version,'mods':count,'count_limited':entries>2000,'version_path':info['path'] if info else None,'target_metadata':bool(info and info['target']),'label':f'{launcher} · {short_name} · {version} · {count}{"+" if entries>2000 else ""} модов'})
            seen.add(key)
        except (OSError,SafetyError):return
    for base,kind in roots:
        key=str(base).lower()
        if key in visited:continue
        visited.add(key)
        try:
            safe_path(base)
            if not base.is_dir():continue
            version_root=safe_path(base/'versions')
            profile_rows=[];metadata=[]
            if version_root.is_dir():
                for i,folder in enumerate(version_root.iterdir()):
                    if i>=120:truncated=True;break
                    try:
                        safe_path(folder)
                        if not folder.is_dir() or 'backup' in folder.name.lower():continue
                        info=None;version_file=folder/(folder.name+'.json')
                        if version_file.is_file():
                            try:info=game_version_info(version_file);metadata.append(info)
                            except (OSError,ValueError,TypeError,AttributeError,SafetyError):pass
                        # Version metadata alone is NOT evidence that its directory
                        # is a gameDirectory. TLauncher profiles have their own mods.
                        if safe_path(folder/'mods').is_dir():profile_rows.append((folder,info))
                    except (OSError,SafetyError):continue
            best=next((x for x in metadata if x['target']),metadata[0] if metadata else None)
            if kind=='shared' or kind=='manual' or safe_path(base/'mods').is_dir():add(base,kind,best,available=True)
            for folder,info in profile_rows:add(folder,'version',info)
            # An explicitly selected container may also hold custom game folders.
            # Only its immediate children and their .minecraft/ are considered.
            if explicit==base and explicit.name.lower()!='versions':
                ignored={'mods','versions','saves','config','logs','assets','libraries','runtime','java','jre','jvms','screenshots','resourcepacks','shaderpacks','.modpack-updater'}
                for i,child in enumerate(base.iterdir()):
                    if i>=120:truncated=True;break
                    if child.name.lower() in ignored or 'backup' in child.name.lower():continue
                    try:
                        safe_path(child)
                        if not child.is_dir():continue
                        if safe_path(child/'mods').is_dir():add(child,'custom')
                        nested=safe_path(child/'.minecraft')
                        if nested.is_dir() and safe_path(nested/'mods').is_dir():add(nested,'custom')
                    except (OSError,SafetyError):continue
        except (OSError,SafetyError):continue
    if explicit and explicit.name.lower() not in ('.minecraft','versions'):
        try:
            if not safe_path(explicit/'mods').is_dir() and any(Path(c['path'])!=explicit and Path(c['path']).is_relative_to(explicit) for c in found):
                # A selected container of profiles is a search boundary, not an
                # extra empty game directory. Empty explicit game dirs still work.
                found=[c for c in found if Path(c['path'])!=explicit]
        except (OSError,SafetyError):pass
    return {'candidates':found,'truncated':truncated,'bounded':True}

def game_folder_candidates(found, launcher='Все лаунчеры'):
    # Launcher name is a hint, never a restriction on a valid gameDirectory.
    return found['candidates']

def choose_game_folder(candidates, preferred=''):
    """Only an explicit previous choice or a single result can be selected."""
    if preferred and any(x['path']==preferred for x in candidates):return preferred
    return candidates[0]['path'] if len(candidates)==1 else ''

def runtime_help(found):
    missing=[]
    if not found['java']:
        versions=', '.join(sorted({x['version'] for x in found.get('other_java',[])}))
        actual=(' Найдены другие версии: '+versions+'.') if versions else ''
        missing.append('Java 17 не найдена.'+actual+' Нажмите «Java 17…», скачайте Temurin 17 для Windows x64 (MSI) с официальной страницы и установите. Затем повторите поиск. Запуск Minecraft сам по себе не гарантирует установку Java 17.')
    if not found['forge']:
        missing.append('Forge 47.4.10 не найден. Установите Forge 47.4.10 для Minecraft 1.20.1, затем повторите поиск. Если лаунчер хранит игру в другой папке, выберите папку своей сборки или используйте «Указать…».')
    return '\n'.join(missing)

def discover_runtime(selected=None, progress=lambda message:None):
    runtimes,versions,direct=discovery_roots(selected)
    found={'java':[],'forge':[],'other_java':[],'checked_java':0,'skipped_java':0,'bounded':True}
    seen_dirs=set();seen_java=set();seen_json=set();budget=160
    # Traverse only the runtime subtrees, with depth/count limits. Once bin/java
    # is found, do not descend into that runtime's lib, legal or other content.
    for label,root in runtimes:
        queue=[(root,0)]
        while queue and budget>0 and found['checked_java']<12:
            folder,depth=queue.pop(0)
            key=str(folder).lower()
            if key in seen_dirs:continue
            seen_dirs.add(key);budget-=1
            try:
                safe_path(folder)
                if not folder.is_dir():continue
                candidate=folder/'bin/java.exe'
                if candidate.is_file():
                    safe_path(candidate)
                    java_key=str(candidate).lower()
                    if java_key not in seen_java:
                        seen_java.add(java_key);found['checked_java']+=1
                        progress('Проверяем Java в стандартной папке лаунчера…')
                        ver=java_version(candidate,timeout=4)
                        if ver.split('.')[0]=='17':
                            found['java'].append({'path':str(candidate),'version':ver,'label':f'Java {ver} · {label} / {folder.name}'})
                        else:
                            found['skipped_java']+=1
                            found['other_java'].append({'version':ver,'path':str(candidate)})
                    continue
                if depth<5:
                    children=[]
                    for child in folder.iterdir():
                        if child.is_dir() and not child.is_symlink():children.append(child)
                        if len(children)>=40:break
                    children.sort(key=lambda p:('gamma' not in p.name.lower(),p.name.lower()))
                    queue.extend((child,depth+1) for child in children)
            except (OSError,SafetyError,subprocess.TimeoutExpired,ValueError):continue
    candidates=[('Выбранная сборка',p) for p in direct]
    for label,parent in versions:
        try:
            safe_path(parent)
            if not parent.is_dir():continue
            count=0
            for folder in parent.iterdir():
                count+=1
                if count>120:break
                if folder.is_dir() and not folder.is_symlink():
                    # Exactly one version JSON per child; never recurse into mods,
                    # configs, backup folders, accounts or launcher settings.
                    candidates.append((label,folder/(folder.name+'.json')))
        except (OSError,SafetyError):continue
    for label,path in candidates:
        key=str(path).lower()
        if key in seen_json:continue
        seen_json.add(key)
        try:
            safe_path(path)
            if not path.is_file():continue
            forge_metadata(path)
            found['forge'].append({'path':str(path),'label':f'Forge 47.4.10 · {label} / {path.parent.name}'})
        except (OSError,SafetyError,ValueError,TypeError,AttributeError):continue
    return found

def require_closed():
    if os.name!='nt': return
    # Process names only: no command lines, usernames, tokens, or environments.
    import csv,io
    result=subprocess.run(['tasklist','/FO','CSV','/NH'],capture_output=True,text=True,errors='replace',creationflags=subprocess.CREATE_NO_WINDOW,timeout=15)
    if result.returncode: raise SafetyError('Не удалось проверить закрытие игры')
    names={r[0].lower() for r in csv.reader(io.StringIO(result.stdout)) if r}
    if names & {'java.exe','javaw.exe','minecraft.exe','minecraftlauncher.exe','tlauncher.exe','tlauncher-2.0.exe','tl.exe'}:
        raise SafetyError('Закройте Minecraft, оба лаунчера и Java-приложения перед изменениями.')

@contextlib.contextmanager
def profile_lock(root):
    directory=safe_path(root/MANAGER)
    directory.mkdir(exist_ok=True)
    lock=safe_path(directory/'lock')
    with lock.open('a+b') as stream:
        if stream.tell()==0: stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as e:
            raise SafetyError('Другой обновлятор уже работает с этой папкой') from e
        try: yield
        finally:
            stream.seek(0)
            if os.name=='nt': msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else: fcntl.flock(stream,fcntl.LOCK_UN)

def atomic_write(path, data):
    safe_path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    safe_path(path.parent)
    fd,temp=tempfile.mkstemp(prefix='.updater-',dir=path.parent)
    temp=Path(temp)
    try:
        with os.fdopen(fd,'wb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
        safe_path(path)
        os.replace(temp,path)
    finally:
        if temp.exists(): temp.unlink()

def save_json(path,data):
    atomic_write(path,(json.dumps(data,ensure_ascii=False,indent=2)+'\n').encode('utf-8'))

def actual_hash(path):
    return digest(path) if path.exists() else None

def backup_list(root):
    parent=safe_path(root/MANAGER/'backups')
    if not parent.exists(): return []
    result=[]
    for p in parent.iterdir():
        safe_path(p)
        if p.is_dir() and re.fullmatch(r'\d{8}T\d{6}-\d{20}-[a-f0-9]{32}',p.name):
            j=p/'journal.json'
            if j.exists():
                data=read_json(j)
                if data.get('status') in ('pending','complete','rollback_pending'):
                    result.append((p,data))
    return sorted(result,key=lambda pair:pair[0].name,reverse=True)

def restore_transaction(root, directory, journal):
    """Preflight every path and backup before restoring anything."""
    allowed_config={x['path'] for x in load_manifest().get('configs',[])}
    for i,op in enumerate(journal['ops']):
        dest=target(root,op['path'])
        if op['path'] not in (STATE,'options.txt') and not op['path'].startswith(('mods/','shaderpacks/','resourcepacks/')) and op['path'] not in allowed_config:
            raise SafetyError('Неизвестная настройка в backup')
        if actual_hash(dest) not in (op['before'],op['after']):
            raise SafetyError('Откат остановлен: файл изменён после установки: '+op['path'])
        for suffix,sha in [('before',op['before']),('after',op['after'])]:
            if sha is not None and digest(directory/(str(i)+'.'+suffix))!=sha:
                raise SafetyError('Повреждён backup: '+op['path'])
    journal['status']='rollback_pending'
    save_json(directory/'journal.json',journal)
    for i in range(len(journal['ops'])-1,-1,-1):
        op=journal['ops'][i]
        dest=target(root,op['path'])
        if actual_hash(dest)==op['before']: continue
        if actual_hash(dest)!=op['after']:
            raise SafetyError('Файл изменился во время отката: '+op['path'])
        if op['before'] is None:
            dest.unlink()
        else:
            atomic_write(dest,(directory/(str(i)+'.before')).read_bytes())
    journal['status']='rolled_back'
    save_json(directory/'journal.json',journal)

def rollback(root):
    root=profile_path(root)
    require_closed()
    with profile_lock(root):
        items=backup_list(root)
        if not items: raise SafetyError('Нет доступного backup для отката')
        directory,journal=items[0]
        restore_transaction(root,directory,journal)
        return directory.name

def apply(root, manifest, progress=lambda message:None, fetch=download):
    root=profile_path(root)
    require_closed()
    with profile_lock(root):
        pending=[b for b in backup_list(root) if b[1]['status']!='complete']
        if pending: raise SafetyError('Предыдущая операция прервана. Сначала нажмите «Откатить».')
        plan=inspect_profile(root,manifest)
        if plan['conflicts']: raise SafetyError('\n'.join(plan['conflicts']))
        state={'schema':1,'release':manifest['release'],'managed':plan['managed']}
        state_bytes=(json.dumps(state,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
        old_state=actual_hash(target(root,STATE))
        new_state=hashlib.sha256(state_bytes).hexdigest()
        ops=plan['ops']
        if old_state!=new_state:
            ops.append({'path':STATE,'before':old_state,'after':new_state,'data':state_bytes})
        if not ops: return 'Все файлы уже совпадают. Изменений нет.'
        parent=safe_path(root/MANAGER/'backups')
        parent.mkdir(exist_ok=True)
        directory=parent/(time.strftime('%Y%m%dT%H%M%S',time.gmtime())+'-'+str(time.time_ns()).zfill(20)+'-'+uuid.uuid4().hex)
        directory.mkdir()
        # Stage everything and verify it before touching profile files.
        for i,op in enumerate(ops):
            progress('Подготовка: '+op['path'])
            if op['after'] is not None:
                staged=directory/(str(i)+'.after')
                if 'spec' in op: fetch(op['spec'],staged)
                elif 'bundled' in op:
                    source=safe_path(BASE/'presets'/safe_name(op['bundled']))
                    shutil.copyfile(source,staged)
                else: staged.write_bytes(op['data'])
                if digest(staged)!=op['after']: raise SafetyError('Ошибка SHA256 при подготовке')
            dest=target(root,op['path'])
            if actual_hash(dest)!=op['before']: raise SafetyError('Профиль изменился. Повторите проверку.')
            if op['before'] is not None:
                shutil.copyfile(dest,directory/(str(i)+'.before'))
                if digest(directory/(str(i)+'.before'))!=op['before']: raise SafetyError('Ошибка backup')
        require_closed()
        fresh=inspect_profile(root,manifest)
        if fresh['conflicts'] or fresh['managed']!=plan['managed'] or fresh['ops']!=[o for o in ops if o['path']!=STATE]:
            raise SafetyError('Профиль изменился во время скачивания. Повторите проверку.')
        journal={'schema':1,'status':'pending','release':manifest['release'],'ops':[{k:op[k] for k in ('path','before','after')} for op in ops]}
        save_json(directory/'journal.json',journal)
        try:
            for i,op in enumerate(ops):
                dest=target(root,op['path'])
                if actual_hash(dest)!=op['before']: raise SafetyError('Файл изменился перед записью: '+op['path'])
                if op['after'] is None: dest.unlink()
                else: atomic_write(dest,(directory/(str(i)+'.after')).read_bytes())
                if actual_hash(dest)!=op['after']: raise SafetyError('Ошибка проверки установленного файла')
            verified=inspect_profile(root,manifest)
            if verified['conflicts'] or verified['ops'] or any(row['status']!='OK' for row in verified['rows']):
                raise SafetyError('Итоговая проверка не пройдена: после записи набор неполон или изменён. '+verification_summary(verified,manifest))
            journal['status']='complete'
            save_json(directory/'journal.json',journal)
        except Exception:
            restore_transaction(root,directory,journal)
            raise
        return 'Готово. SHA256 проверены. Backup: '+directory.name

def gui():
    import tkinter as tk
    from tkinter import ttk,filedialog,messagebox
    import threading,queue
    m=load_manifest()
    window=tk.Tk(); window.title('Minecraft • Обновлятор r8 • Проверка сборки'); window.geometry('990x870'); window.minsize(850,790)
    style=ttk.Style(); style.theme_use('clam'); style.configure('.',font=('Segoe UI',10)); style.configure('Title.TLabel',font=('Segoe UI',21,'bold'))
    frame=ttk.Frame(window,padding=18); frame.pack(fill='both',expand=True)
    ttk.Label(frame,text='Проверка и обновление сборки',style='Title.TLabel').pack(anchor='w')
    ttk.Label(frame,text='Minecraft 1.20.1  •  Forge 47.4.10  •  Java 17  •  26 модов',padding=(0,8,0,16)).pack(anchor='w')
    launcher=tk.StringVar(value='Все лаунчеры'); folder=tk.StringVar(); java=tk.StringVar(); version=tk.StringVar()
    folder_view=tk.StringVar(value='Ищем установленные сборки…');folder_scope=[None]
    folder_note=tk.StringVar(value='Папки будут найдены автоматически. Запись начнётся только после подтверждения.')
    folder_choices={};folder_details={};last_receipt=['']
    java_view=tk.StringVar(value='Найдём автоматически'); forge_view=tk.StringVar(value='Найдём автоматически')
    java_choices={};forge_choices={}
    selectors=ttk.Frame(frame); selectors.pack(fill='x')
    ttk.Label(selectors,text='Лаунчер').grid(row=0,column=0,sticky='w',pady=5)
    launcher_combo=ttk.Combobox(selectors,textvariable=launcher,values=['Все лаунчеры','TLauncher','Официальный Minecraft Launcher'],state='readonly',width=38)
    launcher_combo.grid(row=0,column=1,sticky='w')
    def pick_game(event=None):
        selected=folder_choices.get(folder_view.get())
        if not selected:return
        clear_result()
        folder.set(selected)
        detail=folder_details[selected]
        folder_note.set(f"Папка игры: {selected}\n{detail['launcher']} · {detail['name']} · {detail['version']} · модов: {detail['mods']}")
        version.set(detail['version_path'] if detail['target_metadata'] else '')
        window.after(50,lambda:task('runtime'))
    def choose(var,kind):
        value=filedialog.askdirectory(title='Папка игры с mods. Для официального Launcher обычно .minecraft.') if kind=='folder' else filedialog.askopenfilename(title=('Java 17: bin/java.exe' if kind=='java' else 'versions/версия/версия.json'),filetypes=([('Java','java.exe')] if kind=='java' else [('Метаданные версии','*.json')]))
        if value:
            clear_result()
            if kind=='folder':
                folder_scope[0]=value
                folder.set(value if Path(value).name.lower()!='versions' else '')
                window.after(50,lambda:task('detect'))
            else:var.set(value)
            if kind=='folder':pass
            elif kind=='java':java_view.set('Указано вручную: '+str(Path(value).parent.parent.name))
            else:forge_view.set('Указано вручную: '+Path(value).parent.name)
    def new_profile():
        parent=filedialog.askdirectory(title='Где создать отдельную сборку? Внутри появится папка Minecraft-AEM.',mustexist=True)
        if not parent:return
        clear_result()
        try:
            created=create_profile(parent);folder.set(str(created));folder_scope[0]=str(created)
        except Exception as e:messagebox.showerror('Не удалось создать папку',str(e));return
        window.clipboard_clear();window.clipboard_append(str(created))
        if launcher.get()=='TLauncher':
            text='В TLauncher укажите эту же папку в настройках папки игры для Forge 1.20.1 / 47.4.10.'
        else:
            text='В Minecraft Launcher откройте «Установки» → свою установку Forge → «Изменить» → «Папка игры» и вставьте этот путь. Сохраните установку.'
        messagebox.showinfo('Отдельная сборка создана',f'Создана папка:\n{created}\n\nПуть уже скопирован в буфер обмена.\n\n{text}\n\nПапка в обновляторе и лаунчере должна совпадать. Аккаунты, миры и старые профили не переносились.')
        window.after(50,lambda:task('detect'))
    for row,label,var,kind in [(1,'Папка игры',folder,'folder'),(2,'Java 17',java,'java'),(3,'Forge 47.4.10',version,'version')]:
        ttk.Label(selectors,text=label).grid(row=row,column=0,sticky='w',padx=(0,12),pady=4)
        if kind=='folder':
            folder_combo=ttk.Combobox(selectors,textvariable=folder_view,state='readonly')
            folder_combo.grid(row=row,column=1,sticky='ew')
            folder_combo.bind('<<ComboboxSelected>>',pick_game)
        elif kind=='java':
            java_combo=ttk.Combobox(selectors,textvariable=java_view,state='readonly')
            java_combo.grid(row=row,column=1,sticky='ew')
            java_combo.bind('<<ComboboxSelected>>',lambda e:(java.set(java_choices[java_view.get()]),clear_result()))
        else:
            forge_combo=ttk.Combobox(selectors,textvariable=forge_view,state='readonly')
            forge_combo.grid(row=row,column=1,sticky='ew')
            forge_combo.bind('<<ComboboxSelected>>',lambda e:(version.set(forge_choices[forge_view.get()]),clear_result()))
        ttk.Button(selectors,text='Другая папка…' if kind=='folder' else 'Указать…',command=lambda v=var,k=kind:choose(v,k)).grid(row=row,column=2,padx=(8,0))
    selectors.columnconfigure(1,weight=1)
    create_button=ttk.Button(selectors,text='Создать отдельную сборку',command=new_profile)
    create_button.grid(row=4,column=1,sticky='w',pady=(7,0))
    detection_note=tk.StringVar(value='Проверяем стандартные папки лаунчеров. Аккаунты и настройки входа не читаются.')
    auto_button=ttk.Button(selectors,text='Найти папки, Java и Forge',command=lambda:task('detect'))
    auto_button.grid(row=5,column=1,sticky='w',pady=(7,0))
    def java_download():
        import webbrowser
        webbrowser.open('https://adoptium.net/temurin/releases/?version=17&os=windows&arch=x64&package=jdk')
    ttk.Button(selectors,text='Java 17…',command=java_download).grid(row=5,column=2,padx=(8,0),pady=(7,0))
    ttk.Label(frame,textvariable=folder_note,wraplength=930,padding=(0,10,0,0)).pack(fill='x')
    ttk.Label(frame,textvariable=detection_note,wraplength=840,padding=(0,10,0,0)).pack(fill='x')
    guide=tk.StringVar()
    def help_text(*args):
        official=launcher.get()=='Официальный Minecraft Launcher'
        guide.set('Подтвердите папку, которую использует ваш лаунчер. И Official Launcher, и TLauncher могут использовать .minecraft или свою папку. В списке показаны доступные метаданные версий, а не активная установка лаунчера.')
    launcher.trace_add('write',help_text); help_text()
    ttk.Label(frame,textvariable=guide,wraplength=930,padding=(0,6)).pack(fill='x')
    table=ttk.Treeview(frame,columns=('version','status'),height=4); table.heading('#0',text='Мод / дополнение'); table.heading('version',text='Версия'); table.heading('status',text='Результат'); table.column('#0',width=360); table.column('version',width=220); table.column('status',width=120)
    table.pack(fill='both',expand=True,pady=8)
    status=tk.StringVar(value='Выберите папку игры и нажмите «Проверить». Java и Forge подберём автоматически.')
    ttk.Label(frame,textvariable=status,wraplength=840).pack(anchor='w')
    controls=ttk.Frame(frame); controls.pack(fill='x',pady=(15,0))
    events=queue.Queue(); buttons=[w for w in selectors.winfo_children() if isinstance(w,ttk.Button)];working=[False]
    combos=[launcher_combo,folder_combo,java_combo,forge_combo]
    def clear_result():
        last_receipt[0]=''
        for child in table.get_children():table.delete(child)
        status.set('Моды в этой папке ещё не проверены. Нажмите «Проверить».')
    def use_game_folders(found,chosen):
        folder_choices.clear();folder_details.clear()
        for i,c in enumerate(found['candidates'],1):
            folder_choices[f"{i}. {c['label']}"]=c['path'];folder_details[c['path']]=c
        folder_combo.configure(values=list(folder_choices))
        folder.set(chosen)
        if chosen:
            folder_view.set(next(label for label,path in folder_choices.items() if path==chosen))
            detail=folder_details[chosen]
            folder_note.set(f"Папка игры: {chosen}\n{detail['launcher']} · {detail['name']} · {detail['version']} · модов: {detail['mods']}")
        elif folder_choices:
            folder_view.set(f'Найдено сборок: {len(folder_choices)} — выберите одну')
            folder_note.set('Найдено несколько папок. Выберите нужную сборку из списка — автоматически записывать в первую папку программа не будет.')
        else:
            folder_view.set('Папки не найдены — «Другая папка…»')
            folder_note.set('В стандартных местах сборка не найдена. Выберите свою папку; она будет добавлена к поиску.')
        if found['truncated']:folder_note.set(folder_note.get()+' Достигнут предел поиска: укажите нужную папку вручную.')
    def use_found(found):
        java_choices.clear();forge_choices.clear()
        for i,c in enumerate(found['java'],1):java_choices[f"{i}. {c['label']}"]=c['path']
        for i,c in enumerate(found['forge'],1):forge_choices[f"{i}. {c['label']}"]=c['path']
        java_combo.configure(values=list(java_choices));forge_combo.configure(values=list(forge_choices))
        for choices,var,view in [(java_choices,java,java_view),(forge_choices,version,forge_view)]:
            if choices:
                label=next((label for label,path in choices.items() if path==var.get()),next(iter(choices)))
                var.set(choices[label]);view.set(label)
            else:var.set('');view.set('Не найдено — см. подсказку ниже')
        if found['java'] and found['forge']:
            detection_note.set(f"Найдена Java {found['java'][0]['version']} и Forge 47.4.10. Ручной поиск файлов не нужен.")
        else:detection_note.set(runtime_help(found))
    def task(kind):
        if working[0]:return
        selected=folder.get(); selected_java=java.get(); selected_version=version.get();selected_launcher=launcher.get()
        if not selected and kind not in ('detect','runtime'): messagebox.showerror('Выберите сборку','Подтвердите одну из найденных папок в списке «Папка игры» или нажмите «Другая папка…».'); return
        if kind=='apply':
            try:preview=inspect_profile(selected,m)
            except Exception as e:messagebox.showerror('Проверка папки',str(e));return
            if preview['conflicts']:messagebox.showerror('Нужна проверка конфликтов','\n'.join(preview['conflicts']));return
            changes='\n'.join(op['path'] for op in preview['ops'][:12]) or 'Файлы набора уже совпадают.'
            if len(preview['ops'])>12:changes+=f"\n… ещё {len(preview['ops'])-12} файлов"
            explanation=f'Папка игры:\n{selected}\n\nМоды будут записаны сюда:\n{Path(selected)/"mods"}\n\nМетаданные Forge подтверждают версию, а не активную папку игры. Сверьте путь выше с папкой, открытой кнопкой «Открыть папку модов» в Minecraft.\n\nПлан изменений:\n{changes}\n\nПеред записью будут проверены загрузки и создан backup. Посторонние моды и существующие настройки сохраняются. Аккаунты и миры не затрагиваются.\n\nЗакройте игру и лаунчер. Установить?'
            if not messagebox.askyesno('Проверка перед установкой',explanation):return
        if kind=='rollback' and not messagebox.askyesno('Откат', 'Вернуть файлы последней операции? Файлы, изменённые после установки, не перезаписываются.'): return
        clear_result()
        for b in buttons: b.configure(state='disabled')
        for combo in combos:combo.configure(state='disabled')
        working[0]=True
        status.set('Выполняется…')
        def worker():
            try:
                chosen_java=selected_java;chosen_version=selected_version
                chosen_folder=selected
                if kind=='detect':
                    games=discover_game_folders(folder_scope[0])
                    games['candidates']=game_folder_candidates(games,selected_launcher)
                    chosen_folder=choose_game_folder(games['candidates'],selected)
                    events.put(('games',(games,chosen_folder)))
                if kind in ('detect','runtime') or (kind in ('apply','check') and (not chosen_java or not chosen_version)):
                    found=discover_runtime(chosen_folder or None,lambda s:events.put(('status',s)))
                    events.put(('found',found))
                    chosen_java=found['java'][0]['path'] if found['java'] else ''
                    chosen_version=found['forge'][0]['path'] if found['forge'] else ''
                    if kind in ('detect','runtime'):
                        events.put(('detected','Поиск папок и версий завершён. Моды ещё не проверены. Выберите сборку и нажмите «Проверить».'));return
                    if kind=='apply' and (not chosen_java or not chosen_version):raise SafetyError(runtime_help(found))
                runtime='не проверены';operation=''
                if kind=='rollback': operation='Откат выполнен: '+rollback(selected)
                elif kind=='apply':
                    runtime=check_runtime(chosen_java,chosen_version)
                    operation=apply(selected,m,lambda s:events.put(('status',s)))
                else:
                    if chosen_java and chosen_version:
                        try:runtime=check_runtime(chosen_java,chosen_version)
                        except (SafetyError,OSError,subprocess.TimeoutExpired) as error:runtime='НЕ подтверждены: '+str(error)
                    else:runtime='Java или Forge не найдены: следуйте подсказке над списком.'
                plan=inspect_profile(selected,m)
                result=verification_summary(plan,m)
                if kind=='check':result+=' Проверка ничего не устанавливает.'
                elif kind=='apply':result+=' Установка завершена.' if not plan['conflicts'] and not plan['ops'] else ' После установки обнаружены изменения: повторите проверку.'
                elif kind=='rollback':result+=' Откат выполнен.'
                if plan['conflicts']:result+=' Подробности: «Скопировать результат».'
                receipt=verification_receipt(plan,m,{'check':'Проверка без записи','apply':'Установка / обновление','rollback':'Откат'}[kind],runtime,operation)
                events.put(('done',(result,plan,receipt)))
            except Exception as e: events.put(('error',str(e)))
        threading.Thread(target=worker,daemon=True).start()
    for label,kind in [('1. Проверить','check'),('2. Установить / обновить','apply'),('Откатить','rollback')]:
        b=ttk.Button(controls,text=label,command=lambda k=kind:task(k)); b.pack(side='left',padx=(0,10)); buttons.append(b)
    def copy_result():
        if not last_receipt[0]:messagebox.showinfo('Результата пока нет','Сначала выберите папку и нажмите «Проверить».');return
        window.clipboard_clear();window.clipboard_append(last_receipt[0])
        messagebox.showinfo('Результат скопирован','В буфере путь выбранной папки и результаты проверки всех модов. Можно отправить этот текст для диагностики. Аккаунты и токены в него не входят.')
    copy_button=ttk.Button(controls,text='Скопировать результат',command=copy_result);copy_button.pack(side='left');buttons.append(copy_button)
    ttk.Label(frame,text='Аккаунты, saves, метки Xaero и общие профили лаунчеров не читаются и не изменяются.',wraplength=840,padding=(0,15,0,0)).pack(anchor='w')
    def poll():
        while not events.empty():
            event,data=events.get()
            if event=='status': status.set(data)
            elif event=='games':use_game_folders(*data)
            elif event=='found':use_found(data)
            else:
                working[0]=False
                for b in buttons: b.configure(state='normal')
                for combo in combos:combo.configure(state='readonly')
                if event=='error': status.set('Остановлено: '+data); messagebox.showerror('Операция остановлена',data)
                elif event=='detected':status.set(data)
                else:
                    message,plan,receipt=data; status.set(message);last_receipt[0]=receipt
                    folder_note.set(f"Проверенная папка игры: {plan['root']}\nПапка модов: {plan['root']/'mods'} · JAR сейчас: {plan['jar_count']}")
                    for child in table.get_children(): table.delete(child)
                    for r in plan['rows']: table.insert('', 'end',text=r['name'],values=(r['version'],r['status']))
        window.after(100,poll)
    def close():
        if any(str(b['state'])=='disabled' for b in buttons):
            messagebox.showinfo('Операция выполняется','Дождитесь завершения скачивания или записи файлов.'); return
        window.destroy()
    window.protocol('WM_DELETE_WINDOW',close); poll();window.after(150,lambda:task('detect')); window.mainloop()

if __name__=='__main__':
    if '--self-test-download' in sys.argv:
        spec=next(x for x in load_manifest()['mods'] if x['project_id']=='UQgc9Wcb')
        with tempfile.TemporaryDirectory(prefix='mc-updater-https-test-') as temp:
            download(spec,Path(temp)/spec['file'])
        sys.exit(0)
    if '--self-test-folders' in sys.argv:
        sys.exit(0 if discover_game_folders()['candidates'] else 2)
    if '--self-test-java17' in sys.argv:
        found=discover_runtime()
        sys.exit(0 if found['java'] and found['forge'] and check_runtime(found['java'][0]['path'],found['forge'][0]['path']) else 2)
    if '--self-test' in sys.argv:
        m=load_manifest()
        for c in m['configs']:
            assert digest(BASE/'presets'/c['bundle'])==c['sha256']
        sys.exit(0)
    try: gui()
    except Exception as exc:
        import tkinter.messagebox
        tkinter.messagebox.showerror('Minecraft Updater',str(exc))
        sys.exit(1)
