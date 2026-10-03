'use strict';
const $ = id => document.getElementById(id);
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
  $('launcher-guide').textContent=id==='official' ? 'Minecraft Launcher → Установки → Новая установка → Forge 1.20.1-47.4.10. Задайте отдельную «Папку игры» (gameDirectory). В дополнительных настройках укажите ту же Java 17, которую выберете в обновляторе.' : 'В TLauncher выберите Forge 1.20.1 / 47.4.10 и папку своей сборки — ту, внутри которой находится mods. Эту же папку укажите в обновляторе.';
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
