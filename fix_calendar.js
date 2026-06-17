const fs = require('fs');

const mainPy = fs.readFileSync('main.py', 'utf8');
const b64m = mainPy.match(/BOOKING_HTML_B64\s*=\s*"([A-Za-z0-9+\/=]+)"/);
if (!b64m) { console.error('BOOKING_HTML_B64 not found'); process.exit(1); }
let html = Buffer.from(b64m[1], 'base64').toString('utf8');

// ── Bug 1: el closure fix ──────────────────────────────────────────────────────
// Problem: IIFE captures `d` but NOT `el` — by the time any onclick fires,
// `el` (var-scoped) points to the LAST element of the loop, so sel is always
// added to the last day regardless of which day was clicked.
// Fix: pass `el` into the IIFE as `elRef`.
const OLD_IIFE =
`      (function(d){
        el.onclick=function(){
          document.querySelectorAll('.cd').forEach(function(c){c.classList.remove('sel');});
          el.classList.add('sel');
          booking.date=String(d).padStart(2,'0')+'.'+String(cM+1).padStart(2,'0')+'.'+cY;
          showTimes();
        };
      })(day);`;

const NEW_IIFE =
`      (function(d, elRef){
        elRef.onclick=function(){
          document.querySelectorAll('.cd').forEach(function(c){c.classList.remove('sel');});
          elRef.classList.add('sel');
          booking.date=String(d).padStart(2,'0')+'.'+String(cM+1).padStart(2,'0')+'.'+cY;
          showTimes();
        };
      })(day, el);`;

if (!html.includes(OLD_IIFE)) { console.error('Bug1: IIFE block not found'); process.exit(1); }
html = html.replace(OLD_IIFE, NEW_IIFE);
console.log('✓ Bug 1 fixed: el captured in IIFE as elRef');

// ── Bug 2: trailing empty cell after last day ──────────────────────────────────
// The loop for(day=1;day<=days;day++) is correct — no extra div is appended
// after it. However, empty start-padding .cd cells have no inner div but still
// participate in grid hover (CSS .cd:hover:not(.dis) applies to them), which
// can render a ghost circle. Fix:
//  a) Add class 'pad' to start-padding cells so hover selector can exclude them.
//  b) Add trailing padding cells to complete the last row (avoids visual gap).
//  c) Update the hover CSS to exclude .pad cells.

// Fix the padding cells to have class 'cd pad'
const OLD_PAD = `for(var i=0;i<start;i++){var el=document.createElement('div');el.className='cd';g.appendChild(el);}`;
const NEW_PAD = `for(var i=0;i<start;i++){var el=document.createElement('div');el.className='cd pad';g.appendChild(el);}`;
if (!html.includes(OLD_PAD)) { console.error('Bug2: padding loop not found'); process.exit(1); }
html = html.replace(OLD_PAD, NEW_PAD);

// Add trailing cells after the days loop to complete the last row
const OLD_END = `    g.appendChild(el);
  }
}

function showTimes(){`;
const NEW_END = `    g.appendChild(el);
  }
  // fill trailing cells to complete last grid row
  var total = start + days;
  var trail = (7 - (total % 7)) % 7;
  for(var t=0;t<trail;t++){var ep=document.createElement('div');ep.className='cd pad';g.appendChild(ep);}
}

function showTimes(){`;
if (!html.includes(OLD_END)) { console.error('Bug2: end-of-buildCal not found'); process.exit(1); }
html = html.replace(OLD_END, NEW_END);
console.log('✓ Bug 2 fixed: padding cells classed .pad, trailing cells added');

// Fix CSS: exclude .pad from hover circle
const OLD_HOVER = `.cd:hover:not(.dis),.cd.sel .cd-inner{background:#c9a84c!important;color:#1c1c18!important;font-weight:700!important;border:none!important;border-radius:50%!important;}`;
const NEW_HOVER = `.cd:hover:not(.dis):not(.pad) .cd-inner,.cd.sel .cd-inner{background:#c9a84c!important;color:#1c1c18!important;font-weight:700!important;border:none!important;border-radius:50%!important;}`;
if (!html.includes(OLD_HOVER)) { console.error('Bug2: hover CSS not found'); process.exit(1); }
html = html.replace(OLD_HOVER, NEW_HOVER);
console.log('✓ Bug 2 CSS: .pad excluded from hover');

// Also exclude .pad from markAvailableDays processing
const OLD_MARK = `document.querySelectorAll('.cd:not(.dis):not(.cdn)').forEach(function(el) {`;
const NEW_MARK = `document.querySelectorAll('.cd:not(.dis):not(.cdn):not(.pad)').forEach(function(el) {`;
if (!html.includes(OLD_MARK)) { console.error('Bug2: markAvailableDays selector not found'); process.exit(1); }
html = html.replace(OLD_MARK, NEW_MARK);
console.log('✓ Bug 2: .pad excluded from markAvailableDays');

// ── Verify ─────────────────────────────────────────────────────────────────────
const checks = [
  'elRef.classList.add(\'sel\')',
  '(day, el)',
  'cd pad',
  'not(.pad)',
  'trail',
];
let ok = true;
checks.forEach(c => {
  const f = html.includes(c);
  console.log((f ? '✓' : '✗') + ' ' + c);
  if (!f) ok = false;
});
if (!ok) process.exit(1);

// ── Re-encode ──────────────────────────────────────────────────────────────────
const newB64 = Buffer.from(html, 'utf8').toString('base64');
fs.writeFileSync('main.py', mainPy.replace(
  /BOOKING_HTML_B64\s*=\s*"[A-Za-z0-9+\/=]+"/,
  `BOOKING_HTML_B64 = "${newB64}"`
), 'utf8');
fs.writeFileSync('booking.html', html, 'utf8');
console.log(`Done. HTML: ${html.length} | B64: ${newB64.length}`);
