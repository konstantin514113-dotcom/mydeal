const fs = require('fs');

const mainPy = fs.readFileSync('main.py', 'utf8');
const b64m = mainPy.match(/BOOKING_HTML_B64\s*=\s*"([A-Za-z0-9+\/=]+)"/);
if (!b64m) { console.error('BOOKING_HTML_B64 not found'); process.exit(1); }
let html = Buffer.from(b64m[1], 'base64').toString('utf8');

// Patch 1: filter line — replace b.breed with language-aware field
const OLD1 = `  var res = DATA.filter(function(b){return b.breed.toLowerCase().indexOf(q.toLowerCase())!==-1;}).slice(0,35);
  drop.innerHTML='';`;
const NEW1 = `  var sf=LANG==='en'?'breed_en':LANG==='et'?'breed_et':'breed';
  var res=DATA.filter(function(b){return(b[sf]||b.breed).toLowerCase().indexOf(q.toLowerCase())!==-1;}).slice(0,35);
  drop.innerHTML='';`;

// Patch 2: no-results message — make language-aware
const OLD2 = `  if(!res.length){drop.innerHTML='<div class="nores">Порода не найдена</div><div class="no-breed-banner" onclick="showScreen(\\'homeScreen\\')"><div class="no-breed-banner-icon">🐾</div><div class="no-breed-banner-text"><div class="no-breed-banner-title">Не нашли свою породу?</div><div class="no-breed-banner-sub">Свяжитесь с нами любым удобным способом — мы поможем подобрать услугу</div></div><div class="no-breed-banner-arrow">→</div></div>';}`;
const NEW2 = `  var _nr=LANG==='en'?'Breed not found':LANG==='et'?'Tõugu ei leitud':'Порода не найдена';
  var _nt=LANG==='en'?"Can't find your breed?":LANG==='et'?'Ei leia oma tõugu?':'Не нашли свою породу?';
  var _ns=LANG==='en'?'Contact us — we will help you choose a service':LANG==='et'?'Võtke meiega ühendust — aitame teenuse valida':'Свяжитесь с нами любым удобным способом — мы поможем подобрать услугу';
  if(!res.length){drop.innerHTML='<div class="nores">'+_nr+'</div><div class="no-breed-banner" onclick="showScreen(\\'homeScreen\\')"><div class="no-breed-banner-icon">🐾</div><div class="no-breed-banner-text"><div class="no-breed-banner-title">'+_nt+'</div><div class="no-breed-banner-sub">'+_ns+'</div></div><div class="no-breed-banner-arrow">→</div></div>';}`;

// Patch 3: dropdown item display — use translated breed name
const OLD3 = `      var idx=b.breed.toLowerCase().indexOf(q.toLowerCase());
      d.innerHTML=b.breed.substring(0,idx)+'<mark>'+b.breed.substring(idx,idx+q.length)+'</mark>'+b.breed.substring(idx+q.length);`;
const NEW3 = `      var bname=b[sf]||b.breed;
      var idx=bname.toLowerCase().indexOf(q.toLowerCase());
      d.innerHTML=bname.substring(0,idx)+'<mark>'+bname.substring(idx,idx+q.length)+'</mark>'+bname.substring(idx+q.length);`;

[
  [OLD1, NEW1, 'filter line'],
  [OLD2, NEW2, 'no-results message'],
  [OLD3, NEW3, 'dropdown display'],
].forEach(([old, rep, label]) => {
  if (!html.includes(old)) { console.error(`NOT FOUND: ${label}`); process.exit(1); }
  html = html.replace(old, rep);
  console.log(`✓ patched: ${label}`);
});

// Verify
const checks = ["var sf=LANG==='en'", 'Tõugu ei leitud', 'Breed not found', 'b[sf]||b.breed', 'bname.substring'];
let ok = true;
checks.forEach(c => {
  const found = html.includes(c);
  console.log((found ? '✓' : '✗') + ' ' + c);
  if (!found) ok = false;
});
if (!ok) process.exit(1);

const newB64 = Buffer.from(html, 'utf8').toString('base64');
fs.writeFileSync('main.py', mainPy.replace(/BOOKING_HTML_B64\s*=\s*"[A-Za-z0-9+\/=]+"/, `BOOKING_HTML_B64 = "${newB64}"`), 'utf8');
fs.writeFileSync('booking.html', html, 'utf8');
console.log(`Done. HTML: ${html.length} | B64: ${newB64.length}`);
