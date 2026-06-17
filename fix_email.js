const fs = require('fs');

const mainPy = fs.readFileSync('main.py', 'utf8');
const b64m = mainPy.match(/BOOKING_HTML_B64\s*=\s*"([A-Za-z0-9+\/=]+)"/);
if (!b64m) { console.error('BOOKING_HTML_B64 not found'); process.exit(1); }
let html = Buffer.from(b64m[1], 'base64').toString('utf8');

// 1. Add Email field between Phone and Pet in Step 5
html = html.replace(
  `<div class="fg"><label class="fl" data-i18n="lbl_phone">Телефон</label><input class="fi" id="cPhone" type="tel" placeholder="+372 ..."></div>
    <div class="fg"><label class="fl" data-i18n="lbl_pet">Кличка питомца</label>`,
  `<div class="fg"><label class="fl" data-i18n="lbl_phone">Телефон</label><input class="fi" id="cPhone" type="tel" placeholder="+372 ..."></div>
    <div class="fg"><label class="fl" data-i18n="lbl_email">Email</label><input class="fi" id="cEmail" type="email" placeholder="email@example.com"></div>
    <div class="fg"><label class="fl" data-i18n="lbl_pet">Кличка питомца</label>`
);

// 2. Add lbl_email to T translations (ru/en/et)
html = html.replace(
  `lbl_phone:'Телефон',`,
  `lbl_phone:'Телефон',lbl_email:'Email',`
);
html = html.replace(
  `lbl_phone:'Phone',`,
  `lbl_phone:'Phone',lbl_email:'Email',`
);
html = html.replace(
  `lbl_phone:'Telefon',`,
  `lbl_phone:'Telefon',lbl_email:'Email',`
);

// 3. Read email value in confirmBtn onclick and pass to booking
html = html.replace(
  `if(!name||!phone){alert(T[LANG].alert_fill);return;}
  booking.name=name; booking.phone=phone; booking.pet=document.getElementById('cPet').value;`,
  `if(!name||!phone){alert(T[LANG].alert_fill);return;}
  booking.name=name; booking.phone=phone; booking.email=document.getElementById('cEmail').value; booking.pet=document.getElementById('cPet').value;`
);

// 4. Clear email in resetAll
html = html.replace(
  `document.getElementById('cPhone').value='';
  document.getElementById('cPet').value='';`,
  `document.getElementById('cPhone').value='';
  document.getElementById('cEmail').value='';
  document.getElementById('cPet').value='';`
);

// Verify
const checks = ['cEmail', 'lbl_email', 'booking.email', 'id="cEmail"'];
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
