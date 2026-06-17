const fs = require('fs');

// ── Decode ─────────────────────────────────────────────────────────────────────
const mainPy = fs.readFileSync('main.py', 'utf8');
const b64m = mainPy.match(/BOOKING_HTML_B64\s*=\s*"([A-Za-z0-9+\/=]+)"/);
if (!b64m) { console.error('BOOKING_HTML_B64 not found'); process.exit(1); }
let html = Buffer.from(b64m[1], 'base64').toString('utf8');

// ── Mapping: Russian base name → {en, et} ──────────────────────────────────────
// Provided by user + filled remainder with proper Estonian breed names
const BREED_TRANSLATIONS = {
  'Австралийская овчарка':              {en:'Australian Shepherd',                    et:'Austraalia lambakoer'},
  'Акита-ину флаффи':                   {en:'Akita Inu fluffy',                       et:'Akita Inu pehmekarvaline'},
  'Акита-ину':                          {en:'Akita Inu',                              et:'Akita Inu'},
  'Алабай':                             {en:'Central Asian Shepherd',                 et:'Kesk-Aasia lambakoer'},
  'Аляскинский маламут флаффи':         {en:'Alaskan Malamute fluffy',                et:'Alaska malamuut pehmekarvaline'},
  'Аляскинский маламут':                {en:'Alaskan Malamute',                       et:'Alaska malamuut'},
  'Американская акита флаффи':          {en:'American Akita fluffy',                  et:'Ameerika Akita pehmekarvaline'},
  'Американская акита':                 {en:'American Akita',                         et:'Ameerika Akita'},
  'Американский кокер-спаниель':        {en:'American Cocker Spaniel',                et:'Ameerika kokerspanjel'},
  'Американский стаффордширский терьер':{en:'American Staffordshire Terrier',         et:'Ameerika Staffordshire terjer'},
  'Английский бульдог':                 {en:'English Bulldog',                        et:'Inglise buldog'},
  'Английский кокер-спаниель':          {en:'English Cocker Spaniel',                 et:'Inglise kokerspanjel'},
  'Афган':                              {en:'Afghan Hound',                           et:'Afganistani koer'},
  'Бассет-хаунд':                       {en:'Basset Hound',                           et:'Bassethound'},
  'Бернский зенненхунд':                {en:'Bernese Mountain Dog',                   et:'Berni mägikoer'},
  'Бивер-йорк':                         {en:'Biewer Yorkshire Terrier',               et:'Biewer Yorkshire Terrier'},
  'Бигль':                              {en:'Beagle',                                 et:'Biigel'},
  'Бишон-фризе':                        {en:'Bichon Frisé',                           et:'Bišon Frisé'},
  'Боксер':                             {en:'Boxer',                                  et:'Bokser'},
  'Бордер-колли':                       {en:'Border Collie',                          et:'Borderkoll'},
  'Бостон-терьер':                      {en:'Boston Terrier',                         et:'Bostoni terjer'},
  'Брабансон':                          {en:'Griffon Bruxellois',                     et:'Brüsseli grifon'},
  'Бультерьер':                         {en:'Bull Terrier',                           et:'Bullterjer'},
  'Вельш-корги':                        {en:'Welsh Corgi',                            et:'Walesi korgi'},
  'Вест-хайленд-вайт-терьер':           {en:'West Highland White Terrier',            et:'Lääne-Šotimaa valge terjer'},
  'Восточносибирская лайка':            {en:'East Siberian Laika',                    et:'Ida-Siberi laika'},
  'Голден-ретривер':                    {en:'Golden Retriever',                       et:'Kuldne retriiver'},
  'Гриффон':                            {en:'Griffon',                                et:'Grifon'},
  'Далматин':                           {en:'Dalmatian',                              et:'Dalmaatsia koer'},
  'Джек-рассел-терьер гладкошерстный':  {en:'Jack Russell Terrier smooth',            et:'Jack Russelli terjer lühikarvaline'},
  'Джек-рассел-терьер жесткошерстный':  {en:'Jack Russell Terrier wire-haired',       et:'Jack Russelli terjer karukarvaline'},
  'Доберман':                           {en:'Dobermann',                              et:'Dobermann'},
  'Западносибирская лайка':             {en:'West Siberian Laika',                    et:'Lääne-Siberi laika'},
  'Золотистый ретривер':                {en:'Golden Retriever',                       et:'Kuldne retriiver'},
  'Ирландский мягкошерстный пшеничный терьер':{en:'Irish Soft Coated Wheaten Terrier',et:'Iiri pehmekarvane nisuvärvi terjer'},
  'Ирландский терьер':                  {en:'Irish Terrier',                          et:'Iiri terjer'},
  'Испанский гальго':                   {en:'Spanish Galgo',                          et:'Hispaania galgo'},
  'Йоркширский терьер':                 {en:'Yorkshire Terrier',                      et:'Yorkshire terjer'},
  'Кавалер-кинг-чарльз-спаниель':       {en:'Cavalier King Charles Spaniel',          et:'Cavalier King Charles Spaniel'},
  'Кане-корсо':                         {en:'Cane Corso',                             et:'Cane Corso'},
  'Карело-финская лайка':               {en:'Karelian-Finnish Laika',                 et:'Karjala-Soome laika'},
  'Китайская хохлатая голая':           {en:'Chinese Crested hairless',               et:'Hiina harjakoer karvatu'},
  'Китайская хохлатая пуховая':         {en:'Chinese Crested powderpuff',             et:'Hiina harjakoer Powderpuff'},
  'Кокапу':                             {en:'Cockapoo',                               et:'Cockapoo'},
  'Колли':                              {en:'Collie',                                 et:'Koll'},
  'Комондор':                           {en:'Komondor',                               et:'Komondor'},
  'Лабрадор гладкошерстный':            {en:'Labrador Retriever smooth',              et:'Labradori retriiver lühikarvaline'},
  'Лабрадор длинношерстный':            {en:'Labrador Retriever long-coated',         et:'Labradori retriiver pikkarvaline'},
  'Лабрадудель':                        {en:'Labradoodle',                            et:'Labradoodle'},
  'Левретка':                           {en:'Italian Greyhound',                      et:'Itaalia vindkoer'},
  'Лхасский апсо':                      {en:'Lhasa Apso',                             et:'Lhasa Apso'},
  'Мальтезе':                           {en:'Maltese',                                et:'Malta bolonees'},
  'Мальтийская болонка':                {en:'Maltese Bolognese',                      et:'Malta bolonees'},
  'Мальтипу':                           {en:'Maltipoo',                               et:'Maltipuu'},
  'Метис крупный':                      {en:'Mixed breed large',                      et:'Segaverd suur'},
  'Метис мелкий':                       {en:'Mixed breed small',                      et:'Segaverd väike'},
  'Метис средний':                      {en:'Mixed breed medium',                     et:'Segaverd keskmine'},
  'Миттельшнауцер':                     {en:'Standard Schnauzer',                     et:'Standardšnautser'},
  'Мопс':                               {en:'Pug',                                    et:'Mops'},
  'Невская орхидея':                    {en:'Neva Orchid',                            et:'Neeva orhidee'},
  'Немецкая овчарка':                   {en:'German Shepherd',                        et:'Saksa lambakoer'},
  'Норвич-терьер':                      {en:'Norwich Terrier',                        et:'Norwitši terjer'},
  'Норфолк-терьер':                     {en:'Norfolk Terrier',                        et:'Norfolki terjer'},
  'Ньюфаундленд':                       {en:'Newfoundland',                           et:'Newfoundlandi koer'},
  'Папийон':                            {en:'Papillon',                               et:'Papillon'},
  'Пекинес':                            {en:'Pekingese',                              et:'Pekinesi koer'},
  'Пудель большой':                     {en:'Standard Poodle',                        et:'Standardpuudel'},
  'Пудель карликовый':                  {en:'Miniature Poodle',                       et:'Kääbuspuudel'},
  'Пудель малый':                       {en:'Small Poodle',                           et:'Väike puudel'},
  'Пудель той':                         {en:'Toy Poodle',                             et:'Mänguasja puudel'},
  'Ризеншнауцер':                       {en:'Giant Schnauzer',                        et:'Suuršnautser'},
  'Русская цветная болонка':            {en:'Russian Colored Lapdog',                 et:'Vene värviline sülekoer'},
  'Русский охотничий спаниель':         {en:'Russian Spaniel',                        et:'Vene jahispanjel'},
  'Русский той гладкошерстный':         {en:'Russian Toy smooth',                     et:'Vene Toy lühikarvaline'},
  'Русский той длинношерстный':         {en:'Russian Toy long-coated',                et:'Vene Toy pikkarvaline'},
  'Русский черный терьер':              {en:'Black Russian Terrier',                  et:'Must Vene terjer'},
  'Русско-европейская лайка':           {en:'Russian-European Laika',                 et:'Vene-Euroopa laika'},
  'Самоед':                             {en:'Samoyed',                                et:'Samojeed'},
  'Сеттер английский':                  {en:'English Setter',                         et:'Inglise setter'},
  'Сеттер гордон':                      {en:'Gordon Setter',                          et:'Gordoni setter'},
  'Сеттер ирландский':                  {en:'Irish Setter',                           et:'Iiri setter'},
  'Сиба-ину':                           {en:'Shiba Inu',                              et:'Shiba Inu'},
  'Силихем-терьер':                     {en:'Sealyham Terrier',                       et:'Sealyhami terjer'},
  'Скотч-терьер':                       {en:'Scottish Terrier',                       et:'Šoti terjer'},
  'Такса гладкошерстная карликовая':    {en:'Dachshund smooth miniature',             et:'Taksikoer lühikarvaline kääbus'},
  'Такса гладкошерстная кроличья':      {en:'Dachshund smooth rabbit',                et:'Taksikoer lühikarvaline küülik'},
  'Такса гладкошерстная стандартная':   {en:'Dachshund smooth standard',              et:'Taksikoer lühikarvaline standard'},
  'Такса длинношерстная карликовая':    {en:'Dachshund long-coated miniature',        et:'Taksikoer pikkarvaline kääbus'},
  'Такса длинношерстная кроличья':      {en:'Dachshund long-coated rabbit',           et:'Taksikoer pikkarvaline küülik'},
  'Такса длинношерстная стандартная':   {en:'Dachshund long-coated standard',         et:'Taksikoer pikkarvaline standard'},
  'Такса жесткошерстная карликовая':    {en:'Dachshund wire-haired miniature',        et:'Taksikoer karukarvaline kääbus'},
  'Такса жесткошерстная кроличья':      {en:'Dachshund wire-haired rabbit',           et:'Taksikoer karukarvaline küülik'},
  'Такса жесткошерстная стандартная':   {en:'Dachshund wire-haired standard',         et:'Taksikoer karukarvaline standard'},
  'Уиппет':                             {en:'Whippet',                                et:'Whippet'},
  'Фокстерьер жесткошерстный':          {en:'Wire Fox Terrier',                       et:'Karukarvaline foxterjer'},
  'Французский бульдог':                {en:'French Bulldog',                         et:'Prantsuse buldog'},
  'Хаски':                              {en:'Siberian Husky',                         et:'Siberi husky'},
  'Цвергшнауцер':                       {en:'Miniature Schnauzer',                    et:'Kääbusšnautser'},
  'Чау-чау':                            {en:'Chow Chow',                              et:'Chow Chow'},
  'Чихуахуа гладкошерстный':            {en:'Chihuahua smooth',                       et:'Tšihuahua lühikarvaline'},
  'Чихуахуа длинношерстный':            {en:'Chihuahua long-coated',                  et:'Tšihuahua pikkarvaline'},
  'Шарпей':                             {en:'Shar Pei',                               et:'Šar-Pei'},
  'Шелти':                              {en:'Shetland Sheepdog',                      et:'Šetlandi lambakoer'},
  'Ши-тцу':                             {en:'Shih Tzu',                               et:'Shih Tzu'},
  'Шнауцер миниатюрный':                {en:'Miniature Schnauzer',                    et:'Kääbusšnautser'},
  'Шпиц немецкий / померанский':        {en:'German Spitz / Pomeranian',              et:'Saksa spits / Pomeranian'},
  'Шпиц японский':                      {en:'Japanese Spitz',                         et:'Jaapani spits'},
  'Эстонская гончая':                   {en:'Estonian Hound',                         et:'Eesti hagijas'},
  'Японский хин':                       {en:'Japanese Chin',                          et:'Jaapani Chin'},
  'Кошка короткошерстная':              {en:'Cat short-haired',                       et:'Kass lühikarvaline'},
  'Кошка длинношерстная':               {en:'Cat long-haired',                        et:'Kass pikkarvaline'},
  'Мейн-кун':                           {en:'Maine Coon',                             et:'Maine Cooni kass'},
};

// Keys sorted longest-first for greedy matching
const KEYS = Object.keys(BREED_TRANSLATIONS).sort((a, b) => b.length - a.length);

function translateSuffix(suffix, lang) {
  if (!suffix) return '';
  let s = suffix.trim().replace(/кг/g, 'kg');
  if (lang === 'en') s = s.replace(/более/g, 'over').replace(/до/g, 'up to');
  else                s = s.replace(/более/g, 'üle').replace(/до/g,  'kuni');
  return ' ' + s;
}

function getTranslation(ruBreed, lang) {
  for (const key of KEYS) {
    if (ruBreed.startsWith(key)) {
      const base = BREED_TRANSLATIONS[key][lang];
      const suffix = ruBreed.slice(key.length);
      return base + translateSuffix(suffix, lang);
    }
  }
  return ruBreed; // fallback
}

// ── Patch DATA ─────────────────────────────────────────────────────────────────
const dataMatch = html.match(/var DATA = (\[[\s\S]*?\]);/);
if (!dataMatch) { console.error('DATA not found'); process.exit(1); }

let data;
try { data = JSON.parse(dataMatch[1]); }
catch(e) { console.error('JSON parse error:', e.message); process.exit(1); }

let misses = [];
data.forEach(item => {
  item.breed_en = getTranslation(item.breed, 'en');
  item.breed_et = getTranslation(item.breed, 'et');
  if (item.breed_en === item.breed) misses.push(item.breed);
});

if (misses.length) console.warn('⚠ No translation for:', misses.join(', '));
console.log(`✓ ${data.length} breeds translated (${data.length - misses.length} matched)`);

html = html.replace(/var DATA = \[[\s\S]*?\];/, 'var DATA = ' + JSON.stringify(data) + ';');

// ── Ensure search uses language fields ─────────────────────────────────────────
// Only patch if the previous version's bField logic isn't present
if (!html.includes("var bField=LANG==='en'?'breed_en'")) {
  html = html.replace(
    `var res = DATA.filter(function(b){return b.breed.toLowerCase().indexOf(q.toLowerCase())!==-1;}).slice(0,35);`,
    `var bField=LANG==='en'?'breed_en':LANG==='et'?'breed_et':'breed';
  var res=DATA.filter(function(b){return (b[bField]||b.breed).toLowerCase().indexOf(q.toLowerCase())!==-1;}).slice(0,35);`
  );
  html = html.replace(
    `var idx=b.breed.toLowerCase().indexOf(q.toLowerCase());
      d.innerHTML=b.breed.substring(0,idx)+'<mark>'+b.breed.substring(idx,idx+q.length)+'</mark>'+b.breed.substring(idx+q.length);`,
    `var bField2=LANG==='en'?'breed_en':LANG==='et'?'breed_et':'breed';
      var bname=b[bField2]||b.breed;
      var idx=bname.toLowerCase().indexOf(q.toLowerCase());
      d.innerHTML=bname.substring(0,idx)+'<mark>'+bname.substring(idx,idx+q.length)+'</mark>'+bname.substring(idx+q.length);`
  );
  console.log('✓ search patched');
} else {
  console.log('✓ search already patched — skipped');
}

// ── Verify ─────────────────────────────────────────────────────────────────────
const checks = ['breed_en','breed_et','bField','Austraalia lambakoer','Eesti hagijas','Siberi husky','Kuldne retriiver'];
let ok = true;
checks.forEach(c => {
  const found = html.includes(c);
  console.log((found?'✓':'✗') + ' ' + c);
  if (!found) ok = false;
});
if (!ok) { console.error('Abort: missing patches'); process.exit(1); }

// ── Re-encode & save ───────────────────────────────────────────────────────────
const newB64 = Buffer.from(html, 'utf8').toString('base64');
const newMain = mainPy.replace(/BOOKING_HTML_B64\s*=\s*"[A-Za-z0-9+\/=]+"/, `BOOKING_HTML_B64 = "${newB64}"`);
fs.writeFileSync('main.py', newMain, 'utf8');
fs.writeFileSync('booking.html', html, 'utf8');
console.log(`Done. HTML: ${html.length} chars | B64: ${newB64.length} chars`);
