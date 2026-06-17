const fs = require('fs');
const mainPy = fs.readFileSync('main.py', 'utf8');
const b64m = mainPy.match(/BOOKING_HTML_B64\s*=\s*"([A-Za-z0-9+\/=]+)"/);
if (!b64m) { process.exit(1); }
let html = Buffer.from(b64m[1], 'base64').toString('utf8');

// 1. Add lang to booking object init
html = html.replace(
  `var booking = {breed:'',service:'',price:0,master:'',groomHistory:'',date:'',time:''};`,
  `var booking = {breed:'',breedDisplay:'',service:'',price:0,master:'',groomHistory:'',date:'',time:'',lang:'ru'};`
);

// 2. Set booking.lang when confirm is clicked
html = html.replace(
  `booking.name=name; booking.phone=phone; booking.email=document.getElementById('cEmail').value; booking.pet=document.getElementById('cPet').value;`,
  `booking.name=name; booking.phone=phone; booking.email=document.getElementById('cEmail').value; booking.pet=document.getElementById('cPet').value; booking.lang=LANG;`
);

// 3. Reset lang in resetAll
html = html.replace(
  `booking={breed:'',breedDisplay:'',service:'',price:0,master:'',groomHistory:'',date:'',time:''};`,
  `booking={breed:'',breedDisplay:'',service:'',price:0,master:'',groomHistory:'',date:'',time:'',lang:'ru'};`
);

const checks = ['booking.lang=LANG', "lang:'ru'"];
let ok = true;
checks.forEach(c => { const f = html.includes(c); console.log((f?'✓':'✗')+' '+c); if(!f) ok=false; });
if (!ok) process.exit(1);

const newB64 = Buffer.from(html,'utf8').toString('base64');
fs.writeFileSync('main.py', mainPy.replace(/BOOKING_HTML_B64\s*=\s*"[A-Za-z0-9+\/=]+"/, `BOOKING_HTML_B64 = "${newB64}"`), 'utf8');
console.log('Done');
