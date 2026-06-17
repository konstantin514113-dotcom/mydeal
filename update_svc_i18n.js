const fs = require('fs');

const mainPy = fs.readFileSync('main.py', 'utf8');
const b64m = mainPy.match(/BOOKING_HTML_B64\s*=\s*"([A-Za-z0-9+\/=]+)"/);
if (!b64m) { console.error('BOOKING_HTML_B64 not found'); process.exit(1); }
let html = Buffer.from(b64m[1], 'base64').toString('utf8');

// ── New i18n-aware tagline/desc objects + getter functions ─────────────────────
const replacement = `var SVC_TAGLINE_I18N={
  ru:{'Вычес':'Стоимость зависит от состояния шерсти и объёма работ','Базовый уход':'Подходит для поддержания чистоты между процедурами','Гигиенический уход':'Для комфорта и аккуратности питомца','Комплексный уход':'Полный уход со стрижкой','Экспресс-линька':'Помогает уменьшить количество линяющей шерсти','Тримминг':'Для жесткошерстных пород'},
  en:{'Вычес':'Price depends on coat condition and volume of work','Базовый уход':'Ideal for maintaining cleanliness between full grooms','Гигиенический уход':'For your pet\\'s comfort and neatness','Комплексный уход':'Full grooming with haircut','Экспресс-линька':'Significantly reduces shedding','Тримминг':'For wire-haired breeds'},
  et:{'Вычес':'Hind sõltub karvastiku seisundist ja töömahust','Базовый уход':'Sobib puhtuse hoidmiseks protseduuride vahel','Гигиенический уход':'Lemmiklooma mugavuseks ja korrashoiuks','Комплексный уход':'Täielik hooldus koos lõikusega','Экспресс-линька':'Vähendab oluliselt karvade langemist','Тримминг':'Traatkarvalistele tõugudele'}
};
var SVC_DESC_I18N={
  ru:{'Вычес':'Чистка глаз, ушей, подстригание когтей, вычёс (для кошек)','Базовый уход':'Мытьё профессиональными средствами, деликатная сушка','Гигиенический уход':'Стрижка когтей, чистка ушей и глаз, купание, сушка, уход за лапками и чувствительными зонами','Комплексный уход':'Стрижка когтей, чистка ушей и глаз, купание, сушка, уход за лапками и чувствительными зонами, модельная стрижка','Экспресс-линька':'Мытьё, сушка, уход за шерстью, маска, подстригание когтей, чистка ушей и глаз, уход за лапами и зонами требующими особого внимания','Тримминг':'Выщипывание старого слоя шерсти, мытьё, сушка, стрижка когтей, чистка ушей и глаз, оформление шерсти'},
  en:{'Вычес':'Eye and ear cleaning, nail trimming, brushing (for cats)','Базовый уход':'Washing with professional products, gentle drying','Гигиенический уход':'Nail trimming, ear and eye cleaning, bathing, drying, paw and sensitive area care','Комплексный уход':'Nail trimming, ear and eye cleaning, bathing, drying, paw and sensitive area care, styling haircut','Экспресс-линька':'Washing, drying, coat care, mask, nail trimming, ear and eye cleaning, paw and special area care','Тримминг':'Removing old coat layer, washing, drying, nail trimming, ear and eye cleaning, coat styling'},
  et:{'Вычес':'Silmade ja kõrvade puhastamine, küünte lõikamine, harjamine (kassidele)','Базовый уход':'Pesemine professionaalsete vahenditega, õrn kuivatamine','Гигиенический уход':'Küünte lõikamine, kõrvade ja silmade puhastamine, pesemine, kuivatamine, käppade ja tundlike piirkondade hooldus','Комплексный уход':'Küünte lõikamine, kõrvade ja silmade puhastamine, pesemine, kuivatamine, käppade ja tundlike piirkondade hooldus, modellõikus','Экспресс-линька':'Pesemine, kuivatamine, karvastiku hooldus, mask, küünte lõikamine, kõrvade ja silmade puhastamine, käppade ja eriliste piirkondade hooldus','Тримминг':'Vana karvakihi eemaldamine, pesemine, kuivatamine, küünte lõikamine, kõrvade ja silmade puhastamine, karvastiku kujundamine'}
};
function getSvcTag(name){return(SVC_TAGLINE_I18N[LANG]&&SVC_TAGLINE_I18N[LANG][name])||SVC_TAGLINE_I18N.ru[name]||'';}
function getSvcDesc(name){return(SVC_DESC_I18N[LANG]&&SVC_DESC_I18N[LANG][name])||SVC_DESC_I18N.ru[name]||'';}
`;

// Replace old flat objects with new i18n versions
const oldTagline = `var SVC_TAGLINE={'Вычес':'Стоимость зависит от состояния шерсти и объёма работ','Базовый уход':'Подходит для поддержания чистоты между процедурами','Гигиенический уход':'Для комфорта и аккуратности питомца','Комплексный уход':'Полный уход со стрижкой','Экспресс-линька':'Помогает уменьшить количество линяющей шерсти','Тримминг':'Для жесткошерстных пород'};`;
const oldDesc   = `var SVC_DESC={'Вычес':'Чистка глаз, ушей, подстригание когтей, вычёс (для кошек)','Базовый уход':'Мытьё профессиональными средствами, деликатная сушка','Гигиенический уход':'Стрижка когтей, чистка ушей и глаз, купание, сушка, уход за лапками и чувствительными зонами','Комплексный уход':'Стрижка когтей, чистка ушей и глаз, купание, сушка, уход за лапками и чувствительными зонами, модельная стрижка','Экспресс-линька':'Мытьё, сушка, уход за шерстью, маска, подстригание когтей, чистка ушей и глаз, уход за лапами и зонами требующими особого внимания','Тримминг':'Выщипывание старого слоя шерсти, мытьё, сушка, стрижка когтей, чистка ушей и глаз, оформление шерсти'};`;

if (!html.includes(oldTagline)) { console.error('SVC_TAGLINE not found as expected'); process.exit(1); }
html = html.replace(oldTagline + '\n' + oldDesc, replacement);

// ── Update renderSvcs to use getSvcDesc / getSvcTag ────────────────────────────
html = html.replace(
  `var desc=SVC_DESC[name];
    if(desc){var ds=document.createElement('span');ds.className='svbtn-desc';ds.textContent=desc;btn.appendChild(ds);}
    var tag=SVC_TAGLINE[name];
    if(tag){var ts=document.createElement('span');ts.className='svbtn-tag';ts.textContent=tag;btn.appendChild(ts);}`,
  `var desc=getSvcDesc(name);
    if(desc){var ds=document.createElement('span');ds.className='svbtn-desc';ds.textContent=desc;btn.appendChild(ds);}
    var tag=getSvcTag(name);
    if(tag){var ts=document.createElement('span');ts.className='svbtn-tag';ts.textContent=tag;btn.appendChild(ts);}`
);

// ── Also re-render services when lang changes (add to setLang re-render block) ─
// Already handled: setLang calls renderSvcs(selBreed) if breed selected

// ── Verify ─────────────────────────────────────────────────────────────────────
const checks = [
  'SVC_TAGLINE_I18N',
  'SVC_DESC_I18N',
  'getSvcTag',
  'getSvcDesc',
  'Täielik hooldus koos lõikusega',
  'Vähendab oluliselt karvade langemist',
  'Traatkarvalistele tõugudele',
  'Ideal for maintaining cleanliness',
  'getSvcDesc(name)',
  'getSvcTag(name)',
];
let ok = true;
checks.forEach(c => {
  const found = html.includes(c);
  console.log((found ? '✓' : '✗') + ' ' + c);
  if (!found) ok = false;
});
if (!ok) { console.error('Abort: missing patches'); process.exit(1); }

// ── Re-encode & save ───────────────────────────────────────────────────────────
const newB64 = Buffer.from(html, 'utf8').toString('base64');
const newMain = mainPy.replace(
  /BOOKING_HTML_B64\s*=\s*"[A-Za-z0-9+\/=]+"/,
  `BOOKING_HTML_B64 = "${newB64}"`
);
fs.writeFileSync('main.py', newMain, 'utf8');
fs.writeFileSync('booking.html', html, 'utf8');
console.log(`Done. HTML: ${html.length} | B64: ${newB64.length}`);
