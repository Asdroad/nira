'use strict';

const searchSourceNames = {web:'Интернет',onlinecontract:'ONLINECONTRACT',online_tender:'ТЕНДЕР — ОНЛАЙН',tender_online:'ТЕНДЕР.онлайн'};
const searchJobLabels = {queued:'В очереди',running:'Идёт поиск',completed:'Завершён',failed:'Ошибка источников',cancelled:'Отменён',blocked:'Не подключено'};
function externalLink(url,label,className='button quiet'){
  try { const parsed=new URL(url);if(!['https:','http:'].includes(parsed.protocol)||parsed.username)return ''; }
  catch {return '';}
  return `<a class="${className}" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`;
}
function searchDetails(job){
  const result=job.result||{};
  return `<p class="report-note">${esc(job.error||'')} ${job.status==='queued'?'Для выполнения должен работать локальный исполнитель.':''}</p>${['cancelled','blocked'].includes(job.status)?'<p class="report-note">Этот запуск не применяется к текущим требованиям. Запустите новый поиск.</p>':''}
    <div class="search-source-reports">${(result.sources||[]).map(s=>`<article class="search-source-report"><strong>${esc(s.id==='web'?'Интернет':s.name)}</strong>${badge(s.status==='ok'?s.count+' результатов':s.status==='error'?'Ошибка':'Нет открытого поиска',s.status==='ok'?'':'amber')}<p>${esc(s.message)}</p>${s.total!=null?`<small class="muted">Всего по запросу на площадке: ${esc(s.total)}. Показана первая порция.</small>`:''}${(result.items||[]).some(item=>item.source===s.id)?button('Показать результаты ('+(result.items||[]).filter(item=>item.source===s.id).length+')','search-results:'+esc(s.id),true):''}${s.id==='web'?'':externalLink(s.url,'Открыть источник')}</article>`).join('')}</div>
    ${result.items?.length?'<h3 class="search-results-heading">Найденные страницы и закупки</h3>':''}
    <div class="search-result-list">${(result.items||[]).map((item,index)=>`<article class="search-result-card" data-search-source="${esc(item.source)}" tabindex="-1"><div class="panel-head"><strong>${externalLink(item.url,item.title,'search-result-title')||esc(item.title)}</strong>${badge(item.kind==='tender'?'Закупка':'Веб-страница',item.kind==='tender'?'':'gray')}</div><p class="safe-text">${esc(item.description||'')}</p><p class="small-text muted">${esc(searchSourceNames[item.source])}${item.external_id?' · № '+esc(item.external_id):''}${item.status?' · '+esc(item.status):''}</p>${item.kind==='tender'?`<p>Срок: ${esc(item.deadline?new Date(item.deadline).toLocaleString('ru-RU',{timeZone:'Europe/Moscow'})+' МСК':'Не указан')} · Начальная цена: ${item.amount==null?'Не указана':esc(new Intl.NumberFormat('ru-RU').format(item.amount)+' '+(item.currency||''))}</p><p class="small-text muted">Сведения о заказчике и условия участия проверяйте на площадке.</p>`:'<p class="small-text muted">Фрагмент поисковой выдачи. Прочитанные страницы и контакты показаны отдельно, если они доступны.</p>'}${searchContacts(result,index)}${externalLink(item.url,item.kind==='tender'?'Открыть закупку':'Открыть сайт','button primary')}</article>`).join('')}</div>
    ${job.status==='completed'&&!result.items?.length?empty('По этому запросу ничего не найдено','Попробуйте другое название товара или расширьте поиск.'):''}
    ${result.checked_at?`<p class="report-note">Проверено: ${esc(new Date(result.checked_at).toLocaleString('ru-RU'))}. ${esc(result.note||'')}</p>`:''}`;
}
function searchContacts(result,index){
  const record=(result.contacts||[]).find(c=>c.source_id===String(index));
  if(!record)return '';
  if(record.status!=='read')return '<p class="small-text muted">Сайт не удалось прочитать; контакты не подтверждены.</p>';
  const pages=record.pages.filter(p=>p.emails?.length);
  return `<details class="ai-evidence"><summary>Контакты с открытых страниц (${record.emails.length})</summary>${pages.length?pages.map(p=>`<p>${externalLink(p.url,'Источник контакта')}</p><p>${p.emails.map(esc).join(', ')}</p>`).join(''):'<p>На прочитанных страницах email не найден.</p>'}<p class="small-text muted">Адреса найдены на сайте. Оценка компании и подготовка обращения доступны во вкладке «ИИ и документы».</p></details>`;
}
function showSearchResults(source){
  const dialog=document.getElementById('modal');
  const scope=dialog.open?dialog:document.getElementById('main');
  const first=Array.from(scope.querySelectorAll('.search-result-card')).find(card=>card.dataset.searchSource===source);
  if(first){first.scrollIntoView({block:'start'});first.focus({preventScroll:true});}
}
function renderSearch(task){
  const jobs=(state.taskJobs||[]).filter(j=>j.campaign_id===task.id&&j.kind==='search');const latest=jobs[0];
  return `<div class="panel"><div class="panel-head"><div><h2>Поиск в интернете и закупках</h2><p class="small-text muted">Открытые источники, без регистрации и API-ключей.</p></div>${button('Новый поиск','run-search:'+task.id,true)}</div><p class="report-note">Для продаж закупки помогают находить потребности заказчиков. Для закупок используйте интернет для поиска поставщиков. Результаты требуют проверки соответствия товару, региону и условиям.</p>${latest?`<h3>${esc(latest.payload.query||'Поиск')}</h3><p>${badge(searchJobLabels[latest.status]||latest.status)} · Требования v${esc(latest.payload.requirements_version)}</p>${searchDetails(latest)}`:empty('Поиск ещё не запускался','Утвердите требования и задайте поисковую фразу.')} ${jobs.length>1?`<details><summary>Предыдущие поиски (${jobs.length-1})</summary>${jobs.slice(1).map(j=>`<p>${esc(j.payload.query||'Поиск')} · ${esc(searchJobLabels[j.status]||j.status)} ${button('Результаты','search-job:'+j.id)}</p>`).join('')}</details>`:''}</div>`;
}
function openSearchForm(task){
  modal('Запустить поиск','В поисковик передаётся введённая фраза. ИИ затем анализирует результаты вместе с вашей задачей и документами.',form('search-form',`${field('Поисковая фраза',input('query',task.title,'text',true,'minlength="2" maxlength="300"'),true)}<p class="report-note">Добавьте регион к фразе, если он важен. Для ИИ-анализа текст загруженных документов, требования и найденные страницы передаются в OpenRouter.</p><div>${Object.entries(searchSourceNames).map(([id,name])=>`<label class="toggle-row"><span>${esc(name)}${id==='online_tender'||id==='tender_online'?' · нет открытого поиска':''}</span><input type="checkbox" name="sources" value="${id}" checked></label>`).join('')}</div><label class="toggle-row"><span>Только действующие закупки ONLINECONTRACT</span><input type="checkbox" name="active_only" checked></label>${field('Результатов от каждого доступного источника',`<select name="limit"><option value="10">10</option><option value="30">30</option><option value="50">50</option></select>`)}<p class="report-note">У «ТЕНДЕР — ОНЛАЙН» и «ТЕНДЕР.онлайн» открытый каталог поиска не найден. Их ограничения будут показаны рядом с выдачей.</p>`,'Начать поиск',`data-id="${task.id}"`));
}
