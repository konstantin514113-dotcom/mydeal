// WhatsApp bot for R&J Grooming — powered by whatsapp-web.js + Anthropic
//
// Railway deployment: create a SEPARATE Railway service pointing to this repo,
// set Start Command to "node whatsapp_bot.js" and add env vars:
//   ANTHROPIC_API_KEY  — your Anthropic key
//   JARVIS_WA_ENABLED  — set to "false" to disable without stopping the process
//   PORT               — Railway sets this automatically
//
// First run: open the service URL in browser to see the QR code, then scan in
// WhatsApp > Linked Devices. Session is saved in .wwebjs_auth/.

'use strict';

const http = require('http');
const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcodeTerminal = require('qrcode-terminal');
const QRCode = require('qrcode');
const Anthropic = require('@anthropic-ai/sdk');

const ENABLED = process.env.JARVIS_WA_ENABLED !== 'false';
const ANTHROPIC_API_KEY = process.env.ANTHROPIC_API_KEY;
const PORT = process.env.PORT || 3000;

if (!ENABLED) {
  console.log('[Jarvis WA] Bot disabled (JARVIS_WA_ENABLED=false). Exiting.');
  process.exit(0);
}
if (!ANTHROPIC_API_KEY) {
  console.error('[Jarvis WA] ANTHROPIC_API_KEY not set. Exiting.');
  process.exit(1);
}

const anthropic = new Anthropic({ apiKey: ANTHROPIC_API_KEY });

const SYSTEM_PROMPT = `You are Jarvis, the AI assistant of R&J Grooming salon in Tallinn, Estonia.

ВАЖНЫЕ ПРАВИЛА:
- Отвечай на языке клиента (русский, эстонский, английский)
- Держи ответы короткими и по делу
- Используй эмодзи 🐾 и 🤍 как в примерах
- НЕ отправляй клиента на бронирование через приложение - только на запись через переписку

ПРИВЕТСТВИЕ И ПЕРВЫЙ ШАГ:
Когда клиент спрашивает о стоимости - сначала спроси породу и примерный вес питомца. Потом называй цену из прайса.

УСЛУГИ R&J GROOMING:

БАЗОВЫЙ УХОД:
— Мытьё профессиональными средствами
— Деликатная сушка

ГИГИЕНИЧЕСКИЙ УХОД:
— Стрижка когтей
— Чистка ушей
— Обработка глаз
— Купание
— Сушка шерсти
— Уход за лапками и чувствительными зонами

КОМПЛЕКСНЫЙ УХОД:
— Стрижка когтей
— Чистка ушей
— Обработка глаз
— Уход за шерстью
— Купание и сушка
— Уход за лапками и чувствительными зонами
— Модельная стрижка

SPA-УХОД (дополнительная процедура):
— Глубокое питание и увлажнение шерсти
— Восстановление структуры шерсти
Эффект: мягкая и блестящая шерсть, меньше сухости, легче расчёсывать.
Подходит для тусклой, повреждённой, требующей дополнительного питания шерсти.

ТРИММИНГ (для жесткошерстных пород):
— Выщипывание старого слоя шерсти
— Мытьё и сушка
— Стрижка когтей
— Чистка ушей и глаз
— Оформление шерсти

ЭКСПРЕСС-ЛИНЬКА:
— Мытьё и сушка
— Уход за шерстью + маска
— Стрижка когтей
— Чистка ушей и глаз
— Уход за лапами и особыми зонами
Противопоказания: возраст до 6 мес. и старше 8 лет; сердечно-сосудистые заболевания, коллапс трахеи, дерматиты, грибковые заболевания, покраснения, опухоли, шишки, эрозии, язвы, ранки, одышка, хрипы, кашель, учащённое дыхание.

АДРЕС: Район Noblessner, Allveelaeva 4 🐾

ПРИМЕР ОТВЕТА О ЦЕНЕ:
Здравствуйте 🐾
Для той-пуделя (3 кг) комплексный уход будет стоить от 55€ 🤍
В уход входит:
— мытьё и сушка
— уход за глазками и ушками
— гигиеническая обработка деликатных зон
— стрижка когтей
— модельная стрижка 🐾
Точная стоимость может немного варьироваться в зависимости от состояния шерсти и наличия колтунов — перед процедурой мастер обязательно всё осмотрит и согласует с вами 🤍

ВАЖНО по стоимости:
Стоимость зависит от породы и веса. Если клиент не назвал породу/вес — спроси сначала. Используй прайс из базы данных для расчёта цены. Точная стоимость может варьироваться от состояния шерсти, колтунов, объёма и поведения питомца.

ВЫЧЁСЫВАНИЕ КОШЕК включает:
— вычёсывание
— стрижка когтей
— уход за глазками и ушками
Стоимость может варьироваться от состояния шерсти, колтунов и поведения питомца 🤍`;

// ── Runtime state ───────────────────────────────────────────────────────────
let currentQR = null;   // raw QR string from whatsapp-web.js
let isReady = false;
let waEnabled = true;   // toggled at runtime via POST /toggle

// ── HTTP server ──────────────────────────────────────────────────────────────
const server = http.createServer(async (req, res) => {
  // POST /toggle — enable/disable bot at runtime
  if (req.method === 'POST' && req.url === '/toggle') {
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => {
      try {
        const data = JSON.parse(body);
        waEnabled = data.enabled !== false;
        console.log(`[Jarvis WA] toggled via HTTP: ${waEnabled ? 'enabled' : 'disabled'}`);
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true, enabled: waEnabled }));
      } catch (e) {
        res.writeHead(400); res.end(JSON.stringify({ ok: false, error: 'invalid json' }));
      }
    });
    return;
  }

  if (req.method !== 'GET' || req.url !== '/') {
    res.writeHead(404); res.end('Not found'); return;
  }

  if (isReady) {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(`<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jarvis WA</title>
<style>body{background:#1c1c18;color:#c8c2b8;font-family:sans-serif;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0;text-align:center}
h2{color:#6fcf6f;font-size:1.4rem}</style></head>
<body><div><h2>✅ WhatsApp подключён</h2><p>Jarvis активен и принимает сообщения.</p></div></body></html>`);
    return;
  }

  if (!currentQR) {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(`<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="5">
<title>Jarvis WA — ожидание</title>
<style>body{background:#1c1c18;color:#c8c2b8;font-family:sans-serif;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0;text-align:center}
p{color:#a09880}</style></head>
<body><div><p>⏳ QR код ещё не готов, страница обновится автоматически…</p></div></body></html>`);
    return;
  }

  try {
    const pngDataUrl = await QRCode.toDataURL(currentQR, { width: 320, margin: 2 });
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(`<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>Jarvis WA — QR код</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#1c1c18;color:#c8c2b8;font-family:'Montserrat',sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px}
.box{background:#26261f;border-radius:16px;padding:32px 24px;text-align:center;max-width:400px;width:100%}
h2{color:#c9a84c;font-size:1.2rem;margin-bottom:8px}
p{color:#a09880;font-size:.85rem;margin-bottom:20px;line-height:1.5}
img{border-radius:12px;background:#fff;padding:12px;width:100%;max-width:280px}
.note{margin-top:16px;font-size:.75rem;color:#666}
</style></head>
<body><div class="box">
  <h2>Сканируй QR в WhatsApp</h2>
  <p>WhatsApp → Связанные устройства → Привязать устройство</p>
  <img src="${pngDataUrl}" alt="QR Code">
  <div class="note">Страница обновляется каждые 30 секунд</div>
</div></body></html>`);
  } catch (err) {
    res.writeHead(500); res.end('QR render error: ' + err.message);
  }
});

server.listen(PORT, () => {
  console.log(`[Jarvis WA] HTTP server listening on port ${PORT}`);
});

// ── WhatsApp client ──────────────────────────────────────────────────────────
const client = new Client({
  authStrategy: new LocalAuth({ dataPath: '.wwebjs_auth' }),
  puppeteer: {
    headless: true,
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-dev-shm-usage',
      '--disable-accelerated-2d-canvas',
      '--no-first-run',
      '--no-zygote',
      '--disable-gpu',
    ],
  },
});

client.on('qr', (qr) => {
  currentQR = qr;
  isReady = false;
  console.log('[Jarvis WA] QR ready — open the service URL to scan.');
  qrcodeTerminal.generate(qr, { small: true });
});

client.on('authenticated', () => {
  console.log('[Jarvis WA] Authenticated — session saved.');
  currentQR = null;
});

client.on('auth_failure', (msg) => {
  console.error('[Jarvis WA] Auth failure:', msg);
  currentQR = null;
});

client.on('ready', () => {
  isReady = true;
  currentQR = null;
  console.log('[Jarvis WA] Bot ready and listening.');
});

client.on('disconnected', (reason) => {
  isReady = false;
  console.warn('[Jarvis WA] Disconnected:', reason);
});

client.on('message', async (msg) => {
  if (msg.fromMe) return;

  const chat = await msg.getChat();
  if (chat.isGroup) return;

  const text = (msg.body || '').trim();
  if (!text) return;

  const from = msg.from.replace('@c.us', '');
  console.log(`[Jarvis WA] ${from}: ${text.substring(0, 100)}`);

  if (!waEnabled) {
    console.log('[Jarvis WA] Skipped (disabled).');
    return;
  }

  try {
    const response = await anthropic.messages.create({
      model: 'claude-haiku-4-5-20251001',
      max_tokens: 300,
      system: SYSTEM_PROMPT,
      messages: [{ role: 'user', content: text }],
    });
    const reply = response.content[0]?.text?.trim();
    if (reply) {
      await msg.reply(reply);
      console.log(`[Jarvis WA] → ${reply.substring(0, 80)}`);
    }
  } catch (err) {
    console.error('[Jarvis WA] Anthropic error:', err.message);
  }
});

client.initialize();
