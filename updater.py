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
MANIFEST_SHA256 = '69e13bb9a33809edc53c11f1696bef9b3713e0b4c66f6fec7c507c89b1c2f9b2'
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
    if not p.is_dir() or p == Path(p.anchor) or p.name.lower() in ('.minecraft','mods','config','saves','versions'):
        raise SafetyError('Выберите существующую отдельную папку игры (gameDirectory), не корень лаунчера или mods.')
    if (p/'launcher_profiles.json').exists():
        raise SafetyError('Это общая папка лаунчера. Создайте отдельную папку игры.')
    safe_path(p/'mods')
    safe_path(p/MANAGER)
    return p

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
    request=urllib.request.Request(spec['download_url'],headers={'User-Agent':'MinecraftModpackUpdater/1.0'})
    opener=urllib.request.build_opener(NoRedirect())
    size=0
    with opener.open(request,timeout=45) as response, dest.open('xb') as output:
        while block:=response.read(1024*1024):
            size+=len(block)
            if size>spec['bytes']:
                raise SafetyError('Размер скачанного файла больше ожидаемого')
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
    return {'root':root,'rows':rows,'ops':list(ops.values()),'managed':managed,'conflicts':conflicts,'outsiders':outsiders}

def check_runtime(java_path, version_path):
    java=safe_path(Path(java_path))
    if java.name.lower() not in ('java.exe','java') or not java.is_file():
        raise SafetyError('Выберите исполняемый файл bin/java.exe из Java 17')
    result=subprocess.run([str(java),'-version'],capture_output=True,text=True,timeout=15,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    match=re.search(r'(?:openjdk|java) version "(\d+)',result.stderr+'\n'+result.stdout)
    if result.returncode!=0 or not match or int(match[1])!=17:
        raise SafetyError('Выбранная Java не имеет версии 17')
    version=safe_path(Path(version_path))
    if version.suffix.lower()!='.json' or version.stem!=version.parent.name or re.search(r'(?i)account|token|launcher_profile',version.name):
        raise SafetyError('Выберите versions/<версия>/<версия>.json, а не файл настроек лаунчера')
    data=read_json(version)
    libraries={item.get('name','') for item in data.get('libraries',[])}
    pins={'net.minecraftforge:forge:1.20.1-47.4.10','net.minecraftforge:fmlloader:1.20.1-47.4.10'}
    if not libraries & pins:
        raise SafetyError('В выбранном JSON не найден Forge 47.4.10 для Minecraft 1.20.1')
    declared=data.get('javaVersion',{}).get('majorVersion',17)
    if declared!=17:
        raise SafetyError('Версия требует другую Java')
    return 'Minecraft 1.20.1 / Forge 47.4.10: метаданные подтверждены. Выбранная Java: 17.'

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
    window=tk.Tk(); window.title('Minecraft • Проверка и обновление'); window.geometry('930x760'); window.minsize(780,660)
    style=ttk.Style(); style.theme_use('clam'); style.configure('.',font=('Segoe UI',10)); style.configure('Title.TLabel',font=('Segoe UI',21,'bold'))
    frame=ttk.Frame(window,padding=24); frame.pack(fill='both',expand=True)
    ttk.Label(frame,text='Ваша сборка. Все моды на месте.',style='Title.TLabel').pack(anchor='w')
    ttk.Label(frame,text='Minecraft 1.20.1  •  Forge 47.4.10  •  Java 17  •  23 мода',padding=(0,8,0,16)).pack(anchor='w')
    launcher=tk.StringVar(value='TLauncher'); folder=tk.StringVar(); java=tk.StringVar(); version=tk.StringVar()
    selectors=ttk.Frame(frame); selectors.pack(fill='x')
    ttk.Label(selectors,text='Лаунчер').grid(row=0,column=0,sticky='w',pady=5)
    ttk.Combobox(selectors,textvariable=launcher,values=['TLauncher','Официальный Minecraft Launcher'],state='readonly',width=38).grid(row=0,column=1,sticky='w')
    def choose(var,kind):
        value=filedialog.askdirectory(title='Отдельная папка игры (gameDirectory)') if kind=='folder' else filedialog.askopenfilename(title=('Java 17: bin/java.exe' if kind=='java' else 'versions/версия/версия.json'),filetypes=([('Java','java.exe')] if kind=='java' else [('Метаданные версии','*.json')]))
        if value: var.set(value)
    for row,label,var,kind in [(1,'Папка игры',folder,'folder'),(2,'Java 17',java,'java'),(3,'JSON версии Forge',version,'version')]:
        ttk.Label(selectors,text=label).grid(row=row,column=0,sticky='w',padx=(0,12),pady=7)
        ttk.Entry(selectors,textvariable=var).grid(row=row,column=1,sticky='ew')
        ttk.Button(selectors,text='Выбрать…',command=lambda v=var,k=kind:choose(v,k)).grid(row=row,column=2,padx=(8,0))
    selectors.columnconfigure(1,weight=1)
    guide=tk.StringVar()
    def help_text(*args):
        guide.set('TLauncher: выберите папку конкретной сборки с mods. В настройках запуска укажите эту же папку и выбранную Java 17.' if launcher.get()=='TLauncher' else 'Minecraft Launcher → Установки → Новая установка → Forge 1.20.1-47.4.10 → Папка игры: выбранная папка. В дополнительных настройках задайте ту же Java 17.')
    launcher.trace_add('write',help_text); help_text()
    ttk.Label(frame,textvariable=guide,wraplength=840,padding=(0,14)).pack(fill='x')
    ttk.Label(frame,text='Выбор JSON подтверждает установленную версию, но не активную установку лаунчера.\nПроверка не скачивает файлы. Установка добавляет отсутствующие настройки; существующие сохраняет.',wraplength=840).pack(anchor='w')
    table=ttk.Treeview(frame,columns=('version','status'),height=10); table.heading('#0',text='Мод / дополнение'); table.heading('version',text='Версия'); table.heading('status',text='Результат'); table.column('#0',width=360); table.column('version',width=220); table.column('status',width=120)
    table.pack(fill='both',expand=True,pady=14)
    status=tk.StringVar(value='Выберите папку и нажмите «Проверить». Java и JSON можно указать после проверки модов.')
    ttk.Label(frame,textvariable=status,wraplength=840).pack(anchor='w')
    controls=ttk.Frame(frame); controls.pack(fill='x',pady=(15,0))
    events=queue.Queue(); buttons=[]
    def task(kind):
        selected=folder.get(); selected_java=java.get(); selected_version=version.get()
        if not selected: messagebox.showerror('Папка не выбрана','Выберите отдельную папку игры.'); return
        if kind=='apply' and not messagebox.askyesno('Установить набор?', 'Будут скачаны только отсутствующие или управляемые моды и дополнения из манифеста. Чужие файлы сохраняются. Настройки добавляются только при отсутствии.\n\nЗакройте игру и лаунчеры. Продолжить?'): return
        if kind=='rollback' and not messagebox.askyesno('Откат', 'Вернуть файлы последней операции? Файлы, изменённые после установки, не перезаписываются.'): return
        for b in buttons: b.configure(state='disabled')
        status.set('Выполняется…')
        def worker():
            try:
                if kind=='rollback': result='Откат выполнен: '+rollback(selected)
                elif kind=='apply':
                    check_runtime(selected_java,selected_version)
                    result=apply(selected,m,lambda s:events.put(('status',s)))
                else:
                    result='Проверка модов завершена.'
                    if selected_java and selected_version: result=check_runtime(selected_java,selected_version)
                    else: result+=' Java и Forge ещё не проверены.'
                plan=inspect_profile(selected,m)
                if plan['conflicts']: result+=' Конфликт: '+'; '.join(plan['conflicts'])
                elif plan['outsiders']: result+=f" Посторонних JAR сохранено: {len(plan['outsiders'])}. Их совместимость не гарантируется."
                events.put(('done',(result,plan['rows'])))
            except Exception as e: events.put(('error',str(e)))
        threading.Thread(target=worker,daemon=True).start()
    for label,kind in [('1. Проверить','check'),('2. Установить / обновить','apply'),('Откатить','rollback')]:
        b=ttk.Button(controls,text=label,command=lambda k=kind:task(k)); b.pack(side='left',padx=(0,10)); buttons.append(b)
    ttk.Label(frame,text='Аккаунты, saves, метки Xaero и общие профили лаунчеров не читаются и не изменяются.',wraplength=840,padding=(0,15,0,0)).pack(anchor='w')
    def poll():
        while not events.empty():
            event,data=events.get()
            if event=='status': status.set(data)
            else:
                for b in buttons: b.configure(state='normal')
                if event=='error': status.set('Остановлено: '+data); messagebox.showerror('Операция остановлена',data)
                else:
                    message,rows=data; status.set(message)
                    for child in table.get_children(): table.delete(child)
                    for r in rows: table.insert('', 'end',text=r['name'],values=(r['version'],r['status']))
        window.after(100,poll)
    def close():
        if any(str(b['state'])=='disabled' for b in buttons):
            messagebox.showinfo('Операция выполняется','Дождитесь завершения скачивания или записи файлов.'); return
        window.destroy()
    window.protocol('WM_DELETE_WINDOW',close); poll(); window.mainloop()

if __name__=='__main__':
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
