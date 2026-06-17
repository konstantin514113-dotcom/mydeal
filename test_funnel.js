'use strict';
// Тест воронки Jarvis — запускать: node test_funnel.js
// (нужен ANTHROPIC_API_KEY в окружении)

const Anthropic = require('@anthropic-ai/sdk');
const fs = require('fs');

const src = fs.readFileSync('./whatsapp_bot.js', 'utf8');
const match = src.match(/const SYSTEM_PROMPT = `([\s\S]*?)`;/);
if (!match) { console.error('SYSTEM_PROMPT not found'); process.exit(1); }
const SYSTEM_PROMPT = match[1];

const anthropic = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });

const WA_PHONE = '37258812345'; // имитируем WhatsApp-номер клиента

// Два сценария: один с тем же номером, другой с новым
const SCENARIOS = {
  'Тот же номер': [
    'Привет! Сколько стоит стрижка моей собаки?',
    'Это пудель той, 3 кг',
    'Хочу комплексный уход',
    'Запишите меня на 15 июня в 11:00',
    'Меня зовут Анна, собаку зовут Чарли',
    'На этот номер, спасибо',   // ← SMS на WhatsApp-номер
    'Да, подтверждаю',
  ],
  'Другой номер': [
    'Привет! Сколько стоит стрижка?',
    'Лабрадор гладкошерстный, 30 кг',
    'Гигиенический уход',
    'На 20 июня в 14:00',
    'Меня зовут Дмитрий, пёс — Бакс',
    '+37255599900',             // ← называет другой номер
    'Да, всё верно',
  ],
};

function makeState() {
  return { breed: null, service: null, date: null, time: null, ownerName: null, petName: null, master: null, clientPhone: null, confirmed: false };
}

function stateContext(s) {
  const labels = { breed: 'Порода/вес', service: 'Услуга', date: 'Дата', time: 'Время', ownerName: 'Имя владельца', petName: 'Кличка', master: 'Мастер', clientPhone: 'Телефон для SMS' };
  const filled = Object.entries(labels).filter(([k]) => s[k]).map(([k, l]) => `${l}: ${s[k]}`);
  return filled.length ? '\n\nТЕКУЩИЕ ДАННЫЕ КЛИЕНТА:\n' + filled.join('\n') : '';
}

async function extractState(history, state) {
  const recent = history.slice(-8).map(m => (m.role === 'user' ? 'Клиент' : 'Jarvis') + ': ' + m.content).join('\n');
  const r = await anthropic.messages.create({
    model: 'claude-haiku-4-5-20251001',
    max_tokens: 200,
    messages: [{
      role: 'user',
      content: `Извлеки данные бронирования. Верни ТОЛЬКО JSON.\nПоля: breed, service, date, time, ownerName, petName, master (null если не назван), clientPhone (null если клиент не назвал другой номер), confirmed(boolean).\nТекущие: ${JSON.stringify(state)}\nДиалог:\n${recent}`,
    }],
  });
  const text = r.content[0].text.trim();
  const m = text.match(/\{[\s\S]*\}/);
  return m ? { ...state, ...JSON.parse(m[0]) } : state;
}

function isComplete(s) {
  return s.confirmed && s.breed && s.service && s.date && s.time && s.ownerName && s.petName;
}

async function runScenario(name, steps) {
  console.log(`\n${'═'.repeat(50)}`);
  console.log(`СЦЕНАРИЙ: ${name}`);
  console.log('═'.repeat(50));

  const state = makeState();
  const history = [];

  for (const userMsg of steps) {
    console.log('\n────────────────────────────────────────');
    console.log(`👤 КЛИЕНТ: ${userMsg}`);

    history.push({ role: 'user', content: userMsg });

    const res = await anthropic.messages.create({
      model: 'claude-haiku-4-5-20251001',
      max_tokens: 400,
      system: SYSTEM_PROMPT + stateContext(state),
      messages: history,
    });

    const reply = res.content[0].text.trim();
    history.push({ role: 'assistant', content: reply });
    console.log(`🤖 JARVIS: ${reply}`);

    const newState = await extractState(history, state);
    Object.assign(state, newState);

    const { breed, service, date, time, ownerName, petName, master, clientPhone, confirmed } = state;
    console.log(`📋 STATE: breed=${breed} | service=${service} | date=${date} | time=${time} | owner=${ownerName} | pet=${petName} | clientPhone=${clientPhone} | confirmed=${confirmed}`);

    if (isComplete(state)) {
      const smsPhone = state.clientPhone || WA_PHONE;
      console.log(`\n✅ POST /book → phone для SMS: ${smsPhone}`);
      console.log(JSON.stringify({ ...state, _resolvedPhone: smsPhone }, null, 2));
      break;
    }
  }
}

async function run() {
  console.log('=== ТЕСТ ВОРОНКИ JARVIS ===');
  for (const [name, steps] of Object.entries(SCENARIOS)) {
    await runScenario(name, steps);
  }
}

run().catch(err => { console.error('ERROR:', err.message); process.exit(1); });
