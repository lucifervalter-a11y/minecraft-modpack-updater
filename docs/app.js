'use strict';
const $ = id => document.getElementById(id);
async function sha256(bytes) {
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),x=>x.toString(16).padStart(2,'0')).join('');
}
$('download-windows').addEventListener('click',async()=>{
  const button=$('download-windows'), status=$('download-status');
  button.disabled=true;
  try {
    if (!window.isSecureContext || !crypto.subtle) throw Error('Откройте страницу по HTTPS в современном браузере.');
    status.textContent='Подготавливаем загрузку…';
    const response=await fetch('downloads/windows-download.json',{cache:'no-store'});
    if (!response.ok) throw Error('Не удалось получить описание загрузки. Попробуйте ещё раз.');
    const spec=await response.json();
    if(spec.schema!==1 || spec.file!=='Minecraft-Modpack-Updater-Windows.zip' || !Number.isInteger(spec.bytes) || spec.bytes<1 || spec.bytes>16000000 || !/^[a-f0-9]{64}$/.test(spec.sha256) || !Array.isArray(spec.parts) || spec.parts.length!==2) throw Error('Некорректное описание загрузки.');
    let received=0; const chunks=[];
    for(let i=0;i<2;i++) {
      const part=spec.parts[i];
      if(part.file!==spec.file+'.part'+(i+1) || !Number.isInteger(part.bytes) || part.bytes<1 || part.bytes>=8000000 || !/^[a-f0-9]{64}$/.test(part.sha256)) throw Error('Некорректное описание части архива.');
      status.textContent=`Скачиваем ${i+1}/2… ${Math.round(received/spec.bytes*100)}%`;
      const r=await fetch('downloads/'+part.file,{cache:'no-store'});
      if(!r.ok) throw Error(`Не удалось скачать часть ${i+1}. Попробуйте ещё раз.`);
      const chunk=new Uint8Array(await r.arrayBuffer());
      if(chunk.length!==part.bytes || await sha256(chunk)!==part.sha256) throw Error(`Проверка SHA256 части ${i+1} не пройдена. ZIP не сохранён; повторите загрузку.`);
      chunks.push(chunk); received+=chunk.length;
    }
    if(received!==spec.bytes) throw Error('Размер архива не совпадает. ZIP не сохранён.');
    status.textContent='Проверяем SHA256 готового ZIP…';
    const bytes=new Uint8Array(received);let offset=0;
    for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
    if(await sha256(bytes)!==spec.sha256) throw Error('SHA256 готового ZIP не совпадает. ZIP не сохранён.');
    const url=URL.createObjectURL(new Blob([bytes],{type:'application/zip'}));
    const link=document.createElement('a');link.href=url;link.download=spec.file;document.body.append(link);link.click();link.remove();
    setTimeout(()=>URL.revokeObjectURL(url),60000);
    status.textContent='ZIP готов, SHA256 подтверждён. Если браузер спросит, выберите «Сохранить».';
  } catch(e) {status.textContent='Загрузка остановлена: '+e.message;}
  finally {button.disabled=false;}
});
let manifest;
fetch('manifest.json').then(r => { if (!r.ok) throw Error('Манифест недоступен'); return r.json(); }).then(m => {
  manifest=m;
  for (const mod of [...m.mods,...m.extras]) {
    const row=document.createElement('tr');
    for (const value of [mod.name,mod.version]) { const cell=document.createElement('td'); cell.textContent=value; row.append(cell); }
    const cell=document.createElement('td'), link=document.createElement('a'); link.href=mod.official_page; link.textContent='Modrinth ↗'; link.rel='noreferrer'; cell.append(link); row.append(cell); $('mod-list').append(row);
  }
}).catch(() => {$('check-status').textContent='Не удалось загрузить манифест. Обновите страницу или используйте локальный установщик.';});
function selectLauncher(id) {
  for (const name of ['tlauncher','official']) {$(name).classList.toggle('active',name===id); $(name).setAttribute('aria-pressed',String(name===id));}
  $('launcher-guide').textContent=id==='official' ? 'Официальный Minecraft Launcher: стандартная папка %APPDATA%\\.minecraft поддерживается и предлагается автоматически. Если у установки Forge задана своя «Папка игры» (gameDirectory), выберите её. Переносить игру не нужно; Java 17 и Forge найдутся автоматически.' : 'В TLauncher выберите Forge 1.20.1 / 47.4.10 и папку своей конкретной сборки — ту, внутри которой находятся её mods. Не выбирайте versions целиком. Java 17 и Forge найдутся автоматически.';
}
$('tlauncher').addEventListener('click',()=>selectLauncher('tlauncher'));
$('official').addEventListener('click',()=>selectLauncher('official'));
$('choose-folder').addEventListener('click',async()=>{
  if (!window.showDirectoryPicker) {$('check-status').textContent='Для выбора папки используйте Chrome/Edge на компьютере или скачайте локальный обновлятор.'; return;}
  if (!manifest) {$('check-status').textContent='Сначала дождитесь загрузки манифеста.'; return;}
  $('choose-folder').disabled=true;
  try {
    const directory=await window.showDirectoryPicker({mode:'read'});
    $('check-results').replaceChildren(); $('check-results').hidden=false;
    let mods;
    try {mods=await directory.getDirectoryHandle('mods');}
    catch(e) {if(e.name==='NotFoundError') { $('check-status').textContent='В выбранной папке нет mods. Это может быть новая папка игры: установите набор локальным обновлятором.'; return; } throw e;}
    const hashes=new Map(); let count=0;
    for await (const [name,handle] of mods.entries()) {
      if(handle.kind!=='file'||!name.toLowerCase().endsWith('.jar')) continue;
      $('check-status').textContent=`Проверяем JAR ${++count}… Файлы остаются на компьютере.`;
      const file=await handle.getFile(); if(file.size>300000000) throw Error('Слишком большой JAR');
      const bytes=await crypto.subtle.digest('SHA-256',await file.arrayBuffer());
      const hash=Array.from(new Uint8Array(bytes),x=>x.toString(16).padStart(2,'0')).join('');
      hashes.set(hash,(hashes.get(hash)||0)+1);
    }
    let ok=0;
    for (const mod of manifest.mods) {
      const n=hashes.get(mod.sha256)||0; if(n===1) ok++;
      const row=document.createElement('li'); row.textContent=`${n===1?'✓':n>1?'⚠':'○'} ${mod.name} · ${mod.version} — ${n===1?'SHA256 совпадает':n>1?'дубликат JAR':'нужна локальная проверка / установка'}`; $('check-results').append(row);
    }
    $('check-status').textContent=`Совпало ${ok} из ${manifest.mods.length} модов. Записей на диск: 0. Java, Forge, конфликты и дополнения проверит локальный обновлятор.`;
  } catch(e) {
    $('check-status').textContent=e.name==='AbortError'?'Выбор отменён. Ничего не изменено.':'Проверка остановлена: '+e.message;
  } finally {$('choose-folder').disabled=false;}
});
