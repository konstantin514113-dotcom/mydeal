const fs = require('fs');

// ── Decode ────────────────────────────────────────────────────────────────────
const mainPy = fs.readFileSync('main.py', 'utf8');
const b64m = mainPy.match(/BOOKING_HTML_B64\s*=\s*"([A-Za-z0-9+\/=]+)"/);
if (!b64m) { console.error('BOOKING_HTML_B64 not found'); process.exit(1); }
let html = Buffer.from(b64m[1], 'base64').toString('utf8');

// ── Breed name map: Russian base → {en, et} ───────────────────────────────────
const BREED_MAP = {
  'Австралийская овчарка':              {en:'Australian Shepherd',                   et:'Australian Shepherd'},
  'Акита-ину флаффи':                   {en:'Akita Inu fluffy',                      et:'Akita Inu pehmekarvaline'},
  'Акита-ину':                          {en:'Akita Inu',                             et:'Akita Inu'},
  'Алабай':                             {en:'Central Asian Shepherd',                et:'Kesk-Aasia lambakoer'},
  'Аляскинский маламут флаффи':         {en:'Alaskan Malamute fluffy',               et:'Alaskan Malamute pehmekarvaline'},
  'Аляскинский маламут':                {en:'Alaskan Malamute',                      et:'Alaskan Malamute'},
  'Американская акита флаффи':          {en:'American Akita fluffy',                 et:'Ameerika Akita pehmekarvaline'},
  'Американская акита':                 {en:'American Akita',                        et:'Ameerika Akita'},
  'Американский кокер-спаниель':        {en:'American Cocker Spaniel',               et:'Ameerika kokkerspanjel'},
  'Американский стаффордширский терьер':{en:'American Staffordshire Terrier',        et:'Ameerika Staffordshire terjer'},
  'Английский бульдог':                 {en:'English Bulldog',                       et:'Inglise buldog'},
  'Английский кокер-спаниель':          {en:'English Cocker Spaniel',                et:'Inglise kokkerspanjel'},
  'Афган':                              {en:'Afghan Hound',                          et:'Afgaani koer'},
  'Бассет-хаунд':                       {en:'Basset Hound',                          et:'Bassethound'},
  'Бернский зенненхунд':                {en:'Bernese Mountain Dog',                  et:'Berni mägikoer'},
  'Бивер-йорк':                         {en:'Biewer Yorkshire Terrier',              et:'Biewer Yorkshire Terrier'},
  'Бигль':                              {en:'Beagle',                                et:'Beagle'},
  'Бишон-фризе':                        {en:'Bichon Frisé',                          et:'Bichon Frisé'},
  'Боксер':                             {en:'Boxer',                                 et:'Bokser'},
  'Бордер-колли':                       {en:'Border Collie',                         et:'Border Collie'},
  'Бостон-терьер':                      {en:'Boston Terrier',                        et:'Bostoni terjer'},
  'Брабансон':                          {en:'Griffon Bruxellois',                    et:'Griffon Bruxellois'},
  'Бультерьер':                         {en:'Bull Terrier',                          et:'Bullterjer'},
  'Вельш-корги':                        {en:'Welsh Corgi',                           et:'Walesi corgi'},
  'Вест-хайленд-вайт-терьер':           {en:'West Highland White Terrier',           et:'West Highland White Terrier'},
  'Восточносибирская лайка':            {en:'East Siberian Laika',                   et:'Ida-Siberi laika'},
  'Голден-ретривер':                    {en:'Golden Retriever',                      et:'Golden Retriever'},
  'Гриффон':                            {en:'Griffon',                               et:'Griffon'},
  'Далматин':                           {en:'Dalmatian',                             et:'Dalmaatsia koer'},
  'Джек-рассел-терьер гладкошерстный':  {en:'Jack Russell Terrier smooth',           et:'Jack Russelli terjer lühikarvaline'},
  'Джек-рассел-терьер жесткошерстный':  {en:'Jack Russell Terrier wire-haired',      et:'Jack Russelli terjer karukarvaline'},
  'Доберман':                           {en:'Dobermann',                             et:'Dobermann'},
  'Западносибирская лайка':             {en:'West Siberian Laika',                   et:'Lääne-Siberi laika'},
  'Золотистый ретривер':                {en:'Golden Retriever',                      et:'Golden Retriever'},
  'Ирландский мягкошерстный пшеничный терьер':{en:'Irish Soft Coated Wheaten Terrier',et:'Iiri pehmekarvaline nisuvärvi terjer'},
  'Ирландский терьер':                  {en:'Irish Terrier',                         et:'Iiri terjer'},
  'Испанский гальго':                   {en:'Spanish Galgo',                         et:'Hispaania galgo'},
  'Йоркширский терьер':                 {en:'Yorkshire Terrier',                     et:'Yorkshire Terrier'},
  'Кавалер-кинг-чарльз-спаниель':       {en:'Cavalier King Charles Spaniel',         et:'Cavalier King Charles Spaniel'},
  'Кане-корсо':                         {en:'Cane Corso',                            et:'Cane Corso'},
  'Карело-финская лайка':               {en:'Karelian-Finnish Laika',                et:'Karjala-Soome laika'},
  'Китайская хохлатая голая':           {en:'Chinese Crested hairless',              et:'Hiina harjakoer karvatu'},
  'Китайская хохлатая пуховая':         {en:'Chinese Crested powderpuff',            et:'Hiina harjakoer Powderpuff'},
  'Кокапу':                             {en:'Cockapoo',                              et:'Cockapoo'},
  'Колли':                              {en:'Collie',                                et:'Collie'},
  'Комондор':                           {en:'Komondor',                              et:'Komondor'},
  'Лабрадор гладкошерстный':            {en:'Labrador Retriever smooth',             et:'Labrador Retriever lühikarvaline'},
  'Лабрадор длинношерстный':            {en:'Labrador Retriever long-coated',        et:'Labrador Retriever pikkarvaline'},
  'Лабрадудель':                        {en:'Labradoodle',                           et:'Labradoodle'},
  'Левретка':                           {en:'Italian Greyhound',                     et:'Itaalia vindkoer'},
  'Лхасский апсо':                      {en:'Lhasa Apso',                            et:'Lhasa Apso'},
  'Мальтезе':                           {en:'Maltese',                               et:'Maltese'},
  'Мальтийская болонка':                {en:'Maltese Bolognese',                     et:'Malta boloneese'},
  'Мальтипу':                           {en:'Maltipoo',                              et:'Maltipoo'},
  'Метис крупный':                      {en:'Mixed breed large',                     et:'Segaverd suur'},
  'Метис мелкий':                       {en:'Mixed breed small',                     et:'Segaverd väike'},
  'Метис средний':                      {en:'Mixed breed medium',                    et:'Segaverd keskmine'},
  'Миттельшнауцер':                     {en:'Standard Schnauzer',                    et:'Standardšnautser'},
  'Мопс':                               {en:'Pug',                                   et:'Mops'},
  'Невская орхидея':                    {en:'Neva Orchid',                           et:'Neeva orhidee'},
  'Немецкая овчарка':                   {en:'German Shepherd',                       et:'Saksa lambakoer'},
  'Норвич-терьер':                      {en:'Norwich Terrier',                       et:'Norwitši terjer'},
  'Норфолк-терьер':                     {en:'Norfolk Terrier',                       et:'Norfolki terjer'},
  'Ньюфаундленд':                       {en:'Newfoundland',                          et:'Newfoundlandi koer'},
  'Папийон':                            {en:'Papillon',                              et:'Papillon'},
  'Пекинес':                            {en:'Pekingese',                             et:'Pekingese'},
  'Пудель большой':                     {en:'Standard Poodle',                       et:'Standardpuudel'},
  'Пудель карликовый':                  {en:'Miniature Poodle',                      et:'Miniatuurpuudel'},
  'Пудель малый':                       {en:'Small Poodle',                          et:'Väike puudel'},
  'Пудель той':                         {en:'Toy Poodle',                            et:'Toy puudel'},
  'Ризеншнауцер':                       {en:'Giant Schnauzer',                       et:'Jäätišnautser'},
  'Русская цветная болонка':            {en:'Russian Colored Lapdog',                et:'Vene värviline boloneese'},
  'Русский охотничий спаниель':         {en:'Russian Spaniel',                       et:'Vene spaniel'},
  'Русский той гладкошерстный':         {en:'Russian Toy smooth',                    et:'Vene Toy lühikarvaline'},
  'Русский той длинношерстный':         {en:'Russian Toy long-coated',               et:'Vene Toy pikkarvaline'},
  'Русский черный терьер':              {en:'Black Russian Terrier',                 et:'Must Vene terjer'},
  'Русско-европейская лайка':           {en:'Russian-European Laika',                et:'Vene-Euroopa laika'},
  'Самоед':                             {en:'Samoyed',                               et:'Samojeed'},
  'Сеттер английский':                  {en:'English Setter',                        et:'Inglise setter'},
  'Сеттер гордон':                      {en:'Gordon Setter',                         et:'Gordoni setter'},
  'Сеттер ирландский':                  {en:'Irish Setter',                          et:'Iiri setter'},
  'Сиба-ину':                           {en:'Shiba Inu',                             et:'Shiba Inu'},
  'Силихем-терьер':                     {en:'Sealyham Terrier',                      et:'Sealyhami terjer'},
  'Скотч-терьер':                       {en:'Scottish Terrier',                      et:'Šoti terjer'},
  'Такса гладкошерстная карликовая':    {en:'Dachshund smooth miniature',            et:'Taksikoer lühikarvaline miniatuurne'},
  'Такса гладкошерстная кроличья':      {en:'Dachshund smooth rabbit',               et:'Taksikoer lühikarvaline küülik'},
  'Такса гладкошерстная стандартная':   {en:'Dachshund smooth standard',             et:'Taksikoer lühikarvaline standard'},
  'Такса длинношерстная карликовая':    {en:'Dachshund long-coated miniature',       et:'Taksikoer pikkarvaline miniatuurne'},
  'Такса длинношерстная кроличья':      {en:'Dachshund long-coated rabbit',          et:'Taksikoer pikkarvaline küülik'},
  'Такса длинношерстная стандартная':   {en:'Dachshund long-coated standard',        et:'Taksikoer pikkarvaline standard'},
  'Такса жесткошерстная карликовая':    {en:'Dachshund wire-haired miniature',       et:'Taksikoer karukarvaline miniatuurne'},
  'Такса жесткошерстная кроличья':      {en:'Dachshund wire-haired rabbit',          et:'Taksikoer karukarvaline küülik'},
  'Такса жесткошерстная стандартная':   {en:'Dachshund wire-haired standard',        et:'Taksikoer karukarvaline standard'},
  'Уиппет':                             {en:'Whippet',                               et:'Whippet'},
  'Фокстерьер жесткошерстный':          {en:'Wire Fox Terrier',                      et:'Karukarvaline foxterjer'},
  'Французский бульдог':                {en:'French Bulldog',                        et:'Prantsuse buldog'},
  'Хаски':                              {en:'Siberian Husky',                        et:'Siberi husky'},
  'Цвергшнауцер':                       {en:'Miniature Schnauzer',                   et:'Miniatuuršnautser'},
  'Чау-чау':                            {en:'Chow Chow',                             et:'Chow Chow'},
  'Чихуахуа гладкошерстный':            {en:'Chihuahua smooth',                      et:'Chihuahua lühikarvaline'},
  'Чихуахуа длинношерстный':            {en:'Chihuahua long-coated',                 et:'Chihuahua pikkarvaline'},
  'Шарпей':                             {en:'Shar Pei',                              et:'Shar-Pei'},
  'Шелти':                              {en:'Shetland Sheepdog',                     et:'Shetlandi lambakoer'},
  'Ши-тцу':                             {en:'Shih Tzu',                              et:'Shih Tzu'},
  'Шнауцер миниатюрный':                {en:'Miniature Schnauzer',                   et:'Miniatuuršnautser'},
  'Шпиц немецкий / померанский':        {en:'German Spitz / Pomeranian',             et:'Saksa spits / Pomeranian'},
  'Шпиц японский':                      {en:'Japanese Spitz',                        et:'Jaapani spits'},
  'Эстонская гончая':                   {en:'Estonian Hound',                        et:'Eesti hagijas'},
  'Японский хин':                       {en:'Japanese Chin',                         et:'Jaapani Chin'},
  'Кошка короткошерстная':              {en:'Cat short-haired',                      et:'Kass lühikarvaline'},
  'Кошка длинношерстная':               {en:'Cat long-haired',                       et:'Kass pikkarvaline'},
  'Мейн-кун':                           {en:'Maine Coon',                            et:'Maine Coon'},
};

// Sort keys by length descending for greedy matching
const BREED_KEYS = Object.keys(BREED_MAP).sort((a, b) => b.length - a.length);

function translateBreed(ruName, lang) {
  for (const key of BREED_KEYS) {
    if (ruName.startsWith(key)) {
      let suffix = ruName.slice(key.length).trim();
      const base = BREED_MAP[key][lang];
      if (!suffix) return base;
      // Translate weight suffix words
      if (lang === 'en') {
        suffix = suffix.replace(/более/g,'over').replace(/до/g,'up to').replace(/кг/g,'kg');
      } else {
        suffix = suffix.replace(/более/g,'üle').replace(/до/g,'kuni').replace(/кг/g,'kg');
      }
      return base + ' ' + suffix;
    }
  }
  return ruName; // fallback: return as-is
}

// ── Patch DATA array: add breed_en and breed_et ───────────────────────────────
// Extract the DATA JSON string
const dataMatch = html.match(/var DATA = (\[.*?\]);/s);
if (!dataMatch) { console.error('DATA not found'); process.exit(1); }

let data;
try { data = JSON.parse(dataMatch[1]); }
catch(e) { console.error('Failed to parse DATA:', e.message); process.exit(1); }

data.forEach(function(item) {
  item.breed_en = translateBreed(item.breed, 'en');
  item.breed_et = translateBreed(item.breed, 'et');
});

const newDataStr = 'var DATA = ' + JSON.stringify(data) + ';';
html = html.replace(/var DATA = \[.*?\];/s, newDataStr);
console.log('DATA patched,', data.length, 'breeds');

// ── Inject SVC_TRANSLATIONS and update SVC logic ─────────────────────────────
const svcTransJs = `
var SVC_TRANSLATIONS = {
  'Базовый уход':      {en:'Basic groom',      et:'Põhihooldus'},
  'Гигиенический уход':{en:'Hygiene groom',    et:'Hügieenihooldus'},
  'Комплексный уход':  {en:'Full groom',        et:'Täielik hooldus'},
  'Тримминг':          {en:'Trimming',          et:'Trimmerimine'},
  'Экспресс-линька':   {en:'Express shed',      et:'Kiirkarvavahetus'},
  'Вычес':             {en:'Brush-out',         et:'Harjamine'}
};
`;

// Insert SVC_TRANSLATIONS right before existing SVC_TAGLINE definition
html = html.replace(
  'var SVC_TAGLINE=',
  svcTransJs + 'var SVC_TAGLINE='
);

// ── Update renderSvcs to use translated service names ─────────────────────────
html = html.replace(
  `var name=kv[0],price=kv[1];
    var btn=document.createElement('button');btn.className='svbtn';
    var row=document.createElement('div');row.className='svbtn-row';
    var ns=document.createElement('span');ns.className='svbtn-name';ns.textContent=name;`,
  `var name=kv[0],price=kv[1];
    var displayName=(LANG!=='ru'&&SVC_TRANSLATIONS[name])?SVC_TRANSLATIONS[name][LANG]:name;
    var btn=document.createElement('button');btn.className='svbtn';
    var row=document.createElement('div');row.className='svbtn-row';
    var ns=document.createElement('span');ns.className='svbtn-name';ns.textContent=displayName;`
);

// ── Update breed search: filter and display by language ───────────────────────
// Replace the input event handler
html = html.replace(
  `var res = DATA.filter(function(b){return b.breed.toLowerCase().indexOf(q.toLowerCase())!==-1;}).slice(0,35);
  drop.innerHTML='';
  if(!res.length){drop.innerHTML='<div class="nores">Порода не найдена</div><div class="no-breed-banner" onclick="showScreen(\'homeScreen\')"><div class="no-breed-banner-icon">🐾</div><div class="no-breed-banner-text"><div class="no-breed-banner-title">Не нашли свою породу?</div><div class="no-breed-banner-sub">Свяжитесь с нами любым удобным способом — мы поможем подобрать услугу</div></div><div class="no-breed-banner-arrow">→</div></div>';}
  else{
    res.forEach(function(b){
      var d=document.createElement('div'); d.className='ditem';
      var idx=b.breed.toLowerCase().indexOf(q.toLowerCase());
      d.innerHTML=b.breed.substring(0,idx)+'<mark>'+b.breed.substring(idx,idx+q.length)+'</mark>'+b.breed.substring(idx+q.length);
      d.onclick=function(){selectBreed(b);};
      drop.appendChild(d);
    });
  }`,
  `var bField=LANG==='en'?'breed_en':LANG==='et'?'breed_et':'breed';
  var res=DATA.filter(function(b){return (b[bField]||b.breed).toLowerCase().indexOf(q.toLowerCase())!==-1;}).slice(0,35);
  drop.innerHTML='';
  var noBreed=LANG==='en'?'Breed not found':LANG==='et'?'Tõugu ei leitud':'Порода не найдена';
  var noBreedTitle=LANG==='en'?"Can't find your breed?":LANG==='et'?'Ei leia oma tõugu?':'Не нашли свою породу?';
  var noBreedSub=LANG==='en'?'Contact us in any convenient way — we will help you choose a service':LANG==='et'?'Võtke meiega ühendust — aitame teenuse valida':'Свяжитесь с нами любым удобным способом — мы поможем подобрать услугу';
  if(!res.length){drop.innerHTML='<div class="nores">'+noBreed+'</div><div class="no-breed-banner" onclick="showScreen(\'homeScreen\')"><div class="no-breed-banner-icon">🐾</div><div class="no-breed-banner-text"><div class="no-breed-banner-title">'+noBreedTitle+'</div><div class="no-breed-banner-sub">'+noBreedSub+'</div></div><div class="no-breed-banner-arrow">→</div></div>';}
  else{
    res.forEach(function(b){
      var d=document.createElement('div'); d.className='ditem';
      var dname=b[bField]||b.breed;
      var idx=dname.toLowerCase().indexOf(q.toLowerCase());
      d.innerHTML=dname.substring(0,idx)+'<mark>'+dname.substring(idx,idx+q.length)+'</mark>'+dname.substring(idx+q.length);
      d.onclick=function(){selectBreed(b);};
      drop.appendChild(d);
    });
  }`
);

// ── Update selectBreed to store & show translated breed name ──────────────────
html = html.replace(
  `function selectBreed(b){
  selBreed=b; booking.breed=b.breed;
  inp.value=''; clr.classList.remove('show');
  drop.classList.remove('open'); drop.innerHTML='';
  badge.innerHTML='';
  var bn=document.createElement('span');bn.className='bname';bn.textContent=b.breed;
  var bc=document.createElement('span');bc.className='bchg';bc.textContent='Изменить';`,
  `function selectBreed(b){
  selBreed=b; booking.breed=b.breed;
  inp.value=''; clr.classList.remove('show');
  drop.classList.remove('open'); drop.innerHTML='';
  badge.innerHTML='';
  var bField=LANG==='en'?'breed_en':LANG==='et'?'breed_et':'breed';
  var dispBreed=b[bField]||b.breed;
  booking.breedDisplay=dispBreed;
  var bn=document.createElement('span');bn.className='bname';bn.textContent=dispBreed;
  var chgTxt=LANG==='en'?'Change':LANG==='et'?'Muuda':'Изменить';
  var bc=document.createElement('span');bc.className='bchg';bc.textContent=chgTxt;`
);

// ── Update svcNote translations ───────────────────────────────────────────────
html = html.replace(
  `note.innerHTML='<div style="font-size:.58rem;letter-spacing:.15em;text-transform:uppercase;color:#8a8a55;margin-bottom:8px;font-weight:600;">Важно знать</div><div style="font-size:.71rem;color:#777770;line-height:1.8;">Окончательная стоимость зависит от состояния шерсти и поведения питомца.<br>Разбор колтунов — от 5 €.<br>При агрессивном поведении может применяться доплата 50%.</div>';`,
  `var noteTitle=LANG==='en'?'Please note':LANG==='et'?'Pange tähele':'Важно знать';
      var noteBody=LANG==='en'?'Final price depends on coat condition and pet behaviour.<br>Dematting from 5 €.<br>Aggressive behaviour surcharge may apply: +50%.':LANG==='et'?'Lõplik hind sõltub karvastiku seisundist ja lemmiklooma käitumisest.<br>Koltsunite lahtiharutamine alates 5 €.<br>Agressiivse käitumise korral võib lisanduda 50% juurdehindlus.':'Окончательная стоимость зависит от состояния шерсти и поведения питомца.<br>Разбор колтунов — от 5 €.<br>При агрессивном поведении может применяться доплата 50%.';
      note.innerHTML='<div style="font-size:.58rem;letter-spacing:.15em;text-transform:uppercase;color:#8a8a55;margin-bottom:8px;font-weight:600;">'+noteTitle+'</div><div style="font-size:.71rem;color:#777770;line-height:1.8;">'+noteBody+'</div>';`
);

// ── Update buildSum to use breedDisplay and translated service name ────────────
html = html.replace(
  `'<div class="sr"><span class="sl">'+T[LANG].sum_breed+'</span><span class="sv">'+booking.breed+'</span></div>'+
    '<div class="sr"><span class="sl">'+T[LANG].sum_service+'</span><span class="sv">'+booking.service+'</span></div>'+`,
  `'<div class="sr"><span class="sl">'+T[LANG].sum_breed+'</span><span class="sv">'+(booking.breedDisplay||booking.breed)+'</span></div>'+
    '<div class="sr"><span class="sl">'+T[LANG].sum_service+'</span><span class="sv">'+((LANG!=='ru'&&SVC_TRANSLATIONS[booking.service])?SVC_TRANSLATIONS[booking.service][LANG]:booking.service)+'</span></div>'+`
);

// ── Update resetAll to clear breedDisplay ─────────────────────────────────────
html = html.replace(
  `booking={breed:'',service:'',price:0,master:'',groomHistory:'',date:'',time:''};`,
  `booking={breed:'',breedDisplay:'',service:'',price:0,master:'',groomHistory:'',date:'',time:''};`
);

// ── Update setLang: re-render badge + services if breed already selected ──────
html = html.replace(
  `  MONTHS=tr.months;
  renderCal();
}`,
  `  MONTHS=tr.months;
  renderCal();
  // Re-render badge and services if breed already selected
  if(selBreed){
    var bf=l==='en'?'breed_en':l==='et'?'breed_et':'breed';
    var db=selBreed[bf]||selBreed.breed;
    booking.breedDisplay=db;
    var bnEl=document.querySelector('#sBadge .bname');
    if(bnEl) bnEl.textContent=db;
    var bcEl=document.querySelector('#sBadge .bchg');
    if(bcEl) bcEl.textContent=l==='en'?'Change':l==='et'?'Muuda':'Изменить';
    if(document.getElementById('svcSec').style.display!=='none') renderSvcs(selBreed);
    var sn=document.getElementById('svcNote');
    if(sn){
      var nt=l==='en'?'Please note':l==='et'?'Pange tähele':'Важно знать';
      var nb=l==='en'?'Final price depends on coat condition and pet behaviour.<br>Dematting from 5 €.<br>Aggressive behaviour surcharge may apply: +50%.':l==='et'?'Lõplik hind sõltub karvastiku seisundist ja lemmiklooma käitumisest.<br>Koltsunite lahtiharutamine alates 5 €.<br>Agressiivse käitumise korral võib lisanduda 50% juurdehindlus.':'Окончательная стоимость зависит от состояния шерсти и поведения питомца.<br>Разбор колтунов — от 5 €.<br>При агрессивном поведении может применяться доплата 50%.';
      sn.innerHTML='<div style="font-size:.58rem;letter-spacing:.15em;text-transform:uppercase;color:#8a8a55;margin-bottom:8px;font-weight:600;">'+nt+'</div><div style="font-size:.71rem;color:#777770;line-height:1.8;">'+nb+'</div>';
    }
  }
}`
);

// ── Verify patches ────────────────────────────────────────────────────────────
const checks = [
  'SVC_TRANSLATIONS',
  'breed_en',
  'breed_et',
  'bField',
  'breedDisplay',
  'displayName',
  'Põhihooldus',
  'Australian Shepherd',
  'Eesti hagijas',
];
let ok = true;
checks.forEach(c => {
  const found = html.includes(c);
  console.log((found ? '✓' : '✗') + ' ' + c);
  if (!found) ok = false;
});

if (!ok) { console.error('Some patches missing — aborting'); process.exit(1); }

// ── Re-encode and write ───────────────────────────────────────────────────────
const newB64 = Buffer.from(html, 'utf8').toString('base64');
const newMain = mainPy.replace(
  /BOOKING_HTML_B64\s*=\s*"[A-Za-z0-9+\/=]+"/,
  'BOOKING_HTML_B64 = "' + newB64 + '"'
);
fs.writeFileSync('main.py', newMain, 'utf8');
fs.writeFileSync('booking.html', html, 'utf8');
console.log('Done. HTML:', html.length, 'chars | Base64:', newB64.length, 'chars');
