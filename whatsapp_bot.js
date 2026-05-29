// WhatsApp bot for R&J Grooming — powered by whatsapp-web.js + Anthropic
//
// Railway deployment: create a SEPARATE Railway service pointing to this repo,
// set Start Command to "node whatsapp_bot.js" and add env vars:
//   ANTHROPIC_API_KEY  — your Anthropic key
//   JARVIS_WA_ENABLED  — set to "false" to disable without stopping the process
//
// First run: scan the QR code printed in Railway logs via WhatsApp > Linked Devices.
// Session is saved in .wwebjs_auth/ (persists across restarts if Railway volume is mounted).

'use strict';

const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const Anthropic = require('@anthropic-ai/sdk');

const ENABLED = process.env.JARVIS_WA_ENABLED !== 'false';
const ANTHROPIC_API_KEY = process.env.ANTHROPIC_API_KEY;

if (!ENABLED) {
  console.log('[Jarvis WA] Bot disabled (JARVIS_WA_ENABLED=false). Exiting.');
  process.exit(0);
}
if (!ANTHROPIC_API_KEY) {
  console.error('[Jarvis WA] ANTHROPIC_API_KEY not set. Exiting.');
  process.exit(1);
}

const anthropic = new Anthropic({ apiKey: ANTHROPIC_API_KEY });

const SYSTEM_PROMPT =
  'You are Jarvis, AI administrator of R&J Grooming salon in Tallinn, Estonia. ' +
  'Answer in the language of the client (Russian, Estonian, English). ' +
  'Keep answers short — 1 to 3 sentences maximum. ' +
  'For booking appointments always send this link: https://rjgrooming.up.railway.app/app';

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
  console.log('\n[Jarvis WA] Scan QR code in WhatsApp > Linked Devices:\n');
  qrcode.generate(qr, { small: true });
});

client.on('authenticated', () => {
  console.log('[Jarvis WA] Authenticated — session saved.');
});

client.on('auth_failure', (msg) => {
  console.error('[Jarvis WA] Auth failure:', msg);
});

client.on('ready', () => {
  console.log('[Jarvis WA] Bot ready and listening.');
});

client.on('disconnected', (reason) => {
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

  // re-check enabled flag at message time so env changes take effect without restart
  if (process.env.JARVIS_WA_ENABLED === 'false') {
    console.log('[Jarvis WA] Skipped (disabled via env).');
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
