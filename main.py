from flask import Flask, request, jsonify, session
import anthropic
import os
import requests
from datetime import datetime, timedelta
from functools import wraps
import json, re, uuid, time as _time, threading
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "rjgrooming-secret-2024")
client_ai = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

WHATSAPP_TOKEN    = os.environ.get("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID")
VERIFY_TOKEN      = os.environ.get("WHATSAPP_VERIFY_TOKEN")
INSTAGRAM_VERIFY_TOKEN = os.environ.get("INSTAGRAM_VERIFY_TOKEN", "mydeal13")
INSTAGRAM_TOKEN   = os.environ.get("INSTAGRAM_TOKEN", os.environ.get("WHATSAPP_TOKEN"))

conversation_history = {}
MAX_HISTORY = 20
_msg_log = []          # recent messages: {ts, phone, channel, direction, text}
_channel_counts = {"whatsapp": 0, "instagram": 0, "facebook": 0}

def _log_msg(phone, channel, direction, text):
    _msg_log.append({"ts": datetime.now().isoformat(), "phone": str(phone),
                     "channel": channel, "direction": direction, "text": str(text)[:300]})
    if len(_msg_log) > 100:
        del _msg_log[:-100]
jarvis_enabled    = True
instagram_enabled = True
pause_until       = None
manual_mode       = False
clients           = {}

SYSTEM_PROMPT = """You are Jarvis, the AI administrator of R&J Grooming in Tallinn, Estonia.
- Respond in the client language (Russian, Estonian, English)
- Keep answers short: 1-3 sentences
- Greet only on first message
- Booking: https://n1425894.alteg.io"""

# ── WA funnel SYSTEM_PROMPT (loaded from whatsapp_bot.js) ────────────────────
def _load_wa_prompt():
    try:
        p = os.path.join(os.path.dirname(__file__), "whatsapp_bot.js")
        src = open(p, encoding="utf-8").read()
        m = re.search(r"const SYSTEM_PROMPT = `([\s\S]*?)`;", src)
        return m.group(1) if m else ""
    except Exception:
        return ""

WA_SYSTEM_PROMPT = _load_wa_prompt()

_FUNNEL_RULES = """

═══ ВОРОНКА /test-chat — СТРОГО ПО ШАГАМ, ПРОПУСКИ ЗАПРЕЩЕНЫ ═══

Шаг 1 → Анна: «Здравствуйте! Я Анна, администратор R&J Grooming 🐾 Чем могу помочь?»
Шаг 2 → Клиент написал запрос: ответь тепло, спроси ТОЛЬКО породу.
         Пример: «Замечательно! Подскажите, пожалуйста, какая у вас порода?»
         ❌ НЕ спрашивай вес на этом шаге.
Шаг 2б → ВЫПОЛНЯЕТСЯ КОДОМ: если порода требует уточнения подвида (такса / пудель /
          шпиц / чихуахуа / йорк) — код спрашивает подвид автоматически.
          ❌ НЕ называй услуги и цену до уточнения подвида.
Шаг 3 → ВЫПОЛНЯЕТСЯ КОДОМ: порода (с подвидом если нужен) известна, вес не известен →
         код автоматически делает комплимент породе и спрашивает вес.
         ❌ НЕ спрашивай сам — дождись кода. ❌ НЕ называй услуги.
Шаг 4 → ВЫПОЛНЯЕТСЯ КОДОМ: порода + вес известны →
         код показывает услуги для породы от простого к дорогому + рекомендует комплексный.
         ❌ НЕ перечисляй услуги самостоятельно.
Шаг 5 → Клиент выбрал услугу: жди подтверждения («да», «подходит», «хочу»).
Шаг 6 → ВЫПОЛНЯЕТСЯ КОДОМ: цена «от N€» + дисклеймер + «Хотите записаться?»
         ❌ НЕ называй цену самостоятельно.
Шаг 7 → Клиент «да» на «Хотите записаться?»: ответь ТОЛЬКО «Одну минуту, проверяю расписание 🐾»
Шаг 8 → После расписания: жди конкретную дату и время.
Шаг 9 → Спроси имя владельца и кличку питомца (можно в одном сообщении).
Шаг 9б → Спроси номер телефона для SMS-подтверждения:
          «Укажите, пожалуйста, номер телефона в формате +372XXXXXXX 🤍»
          Если клиент дал номер без +372 или в другом формате — уточни:
          «Пожалуйста, укажите полный номер в формате +372XXXXXXX (например, +37253112233) 🤍»
          Код проверяет формат автоматически и попросит исправить если нужно.
Шаг 10 → Карточка: Порода | Услуга | Дата | Время | Владелец | Питомец | Телефон.
          «Всё верно?» → «да» → «Запись принята! Ждём вас 🐾»

ЕСЛИ порода не известна → спроси породу (шаг 2).
ЕСЛИ порода с разновидностями (такса/пудель/шпиц/чихуахуа/йорк) без подвида → жди кода (шаг 2б).
ЕСЛИ порода (с подвидом) известна, вес НЕ известен → жди кода (шаг 3). Сам не действуй.
ЕСЛИ порода и вес известны → жди кода (шаг 4). Сам не действуй.
НЕЛЬЗЯ переходить к услугам пока не известен вес и подвид (если порода требует).
НЕЛЬЗЯ называть цену пока клиент не подтвердил услугу.
НЕЛЬЗЯ показывать слоты пока клиент не ответил «да» на «Хотите записаться?».

ОТМЕНА ЗАПИСИ:
Клиент: «отмените», «хочу отменить», «не приду» →
  1. Если телефон неизвестен — спроси (+372XXXXXXX).
  2. Уточни: «Вы хотите полностью отменить запись?»
  3. После «да» — ответь: «Хорошо, отменяю 🤍 [[ACTION:cancel:+372XXXXXXX]]»

ПЕРЕНОС ЗАПИСИ:
Клиент: «хочу перенести», «другое время», «изменить дату» →
  1. Если телефон неизвестен — спроси его.
  2. Покажи свободные слоты, спроси удобное время.
  3. После выбора — ответь: «Переношу на [дата] в [время] 🐾 [[ACTION:reschedule:+372XXXXXXX:DD.MM.YYYY:HH:MM]]»

Маркер [[ACTION:...]] оставь в ответе — система исполняет его и удаляет перед отправкой.

ЗАПРЕЩЕНО: пропускать шаги | спрашивать породу и вес вместе | два вопроса в одном сообщении | слово «малыш» | бронирование через сайт."""

TEST_CHAT_SYSTEM_PROMPT = WA_SYSTEM_PROMPT + _FUNNEL_RULES

# ── Test-chat in-memory sessions ─────────────────────────────────────────────
_chat_sessions = {}  # sid -> {history, state}

def _blank_state():
    return {"breed": None, "weight": None, "service": None, "date": None, "time": None,
            "ownerName": None, "petName": None, "master": None,
            "clientPhone": None, "confirmed": False,
            "service_confirmed": False, "booking_intent": False,
            "price_shown": False}

def _state_context(state):
    labels = {"breed": "Порода", "weight": "Вес", "service": "Услуга", "date": "Дата",
              "time": "Время", "ownerName": "Имя владельца", "petName": "Кличка",
              "master": "Мастер", "clientPhone": "Телефон для SMS"}
    filled = [f"{labels[k]}: {state[k]}" for k in labels if state.get(k)]
    ctx = ("\n\nТЕКУЩИЕ ДАННЫЕ КЛИЕНТА (уже известны, НЕ переспрашивай):\n"
           + "\n".join(filled)) if filled else ""
    breed = state.get("breed")
    if breed and not state.get("service"):
        top_svc, top_price = _lookup_most_expensive_service(breed)
        if top_svc and top_price:
            ctx += (f"\n\nСАМАЯ ДОРОГАЯ УСЛУГА ДЛЯ ЭТОЙ ПОРОДЫ: {top_svc} — от {top_price}€"
                    " (предлагай именно её, если клиент не знает что нужно)")
    return ctx

_SVC_KW = {
    'базовый':       'Базовый уход',
    'гигиенический': 'Гигиенический уход',
    'комплексный':   'Комплексный уход',
    'spa':           'SPA-уход',
    'тримминг':      'Тримминг',
    'экспресс':      'Экспресс-линька',
    'линька':        'Экспресс-линька',
    'вычес':         'Вычес',
}

# Order for listing services simple → complex
_SVC_ORDER = ['Базовый уход', 'Гигиенический уход', 'Комплексный уход',
              'Тримминг', 'Вычес', 'Экспресс-линька', 'SPA-уход']

_SVC_DESCRIPTIONS = {
    'Базовый уход':       'мытьё профессиональными средствами + сушка',
    'Гигиенический уход': 'купание, сушка, стрижка когтей, чистка ушей, глаз и лапок',
    'Комплексный уход':   'всё из гигиенического + модельная стрижка ✨',
    'SPA-уход':           'глубокое питание и восстановление шерсти',
    'Тримминг':           'выщипывание старого слоя + оформление силуэта',
    'Экспресс-линька':    'мытьё, маска, сушка — эффективное удаление подшёрстка',
    'Вычес':              'вычёсывание + стрижка когтей + уход за глазами и ушами',
}

import random as _random

_BREED_COMPLIMENTS = [
    "{breed} — отличная порода! 🤍",
    "О, {breed}! Такие красивые собаки 🐾",
    "{breed} — просто замечательная порода! 🤍",
    "Как здорово, {breed}! Одна из моих любимых пород 🐾",
    "{breed} — такие умные и красивые! 🤍",
]

def _breed_compliment(breed):
    name = (breed or '').strip()
    if not name:
        return "Отличный питомец! 🤍"
    name = name[0].upper() + name[1:]
    return _random.choice(_BREED_COMPLIMENTS).format(breed=name)

def _lookup_price(breed, service):
    """Look up price from WA_SYSTEM_PROMPT price list by breed + service keywords."""
    if not breed or not service or not WA_SYSTEM_PROMPT:
        return 0
    service_lower = service.lower()
    canonical = next((name for kw, name in _SVC_KW.items() if kw in service_lower), None)
    if not canonical:
        return 0
    # Breed words longer than 3 chars, skip digits/weight tokens
    breed_words = [w.strip(',.()кгКГ') for w in breed.lower().split()
                   if len(w) > 3 and not re.match(r'^\d', w)]
    if not breed_words:
        return 0
    best_line, best_score = "", 0
    for line in WA_SYSTEM_PROMPT.split('\n'):
        if ':' not in line:
            continue
        ll = line.lower()
        score = sum(1 for w in breed_words if w in ll)
        if score > best_score:
            best_score, best_line = score, line
    if not best_line or best_score == 0:
        return 0
    m = re.search(rf'{re.escape(canonical)}\s+(\d+)', best_line)
    return int(m.group(1)) if m else 0

def _lookup_most_expensive_service(breed):
    """Return (canonical_service_name, price) with the highest price for breed."""
    if not breed or not WA_SYSTEM_PROMPT:
        return None, 0
    breed_words = [w.strip(',.()кгКГ') for w in breed.lower().split()
                   if len(w) > 3 and not re.match(r'^\d', w)]
    if not breed_words:
        return None, 0
    best_line, best_score = "", 0
    for line in WA_SYSTEM_PROMPT.split('\n'):
        if ':' not in line:
            continue
        ll = line.lower()
        score = sum(1 for w in breed_words if w in ll)
        if score > best_score:
            best_score, best_line = score, line
    if not best_line or best_score == 0:
        return None, 0
    best_service, best_price = None, 0
    for canonical in set(_SVC_KW.values()):
        m = re.search(rf'{re.escape(canonical)}\s+(\d+)', best_line)
        if m:
            p = int(m.group(1))
            if p > best_price:
                best_price, best_service = p, canonical
    return best_service, best_price

def _get_all_services_for_breed(breed):
    """Return ordered list of (service, price) for breed, simple→complex."""
    if not breed or not WA_SYSTEM_PROMPT:
        return []
    breed_words = [w.strip(',.()кгКГ') for w in breed.lower().split()
                   if len(w) > 3 and not re.match(r'^\d', w)]
    if not breed_words:
        return []
    best_line, best_score = "", 0
    for line in WA_SYSTEM_PROMPT.split('\n'):
        if ':' not in line:
            continue
        ll = line.lower()
        score = sum(1 for w in breed_words if w in ll)
        if score > best_score:
            best_score, best_line = score, line
    if not best_line or best_score == 0:
        return []
    result = []
    for canonical in _SVC_ORDER:
        m = re.search(rf'{re.escape(canonical)}\s+(\d+)', best_line)
        if m:
            result.append((canonical, int(m.group(1))))
    return result

# Breeds that require subtype clarification before pricing/services.
# 'coat'+'size' means BOTH must be present; 'any' means at least one must be present.
_BREED_CLARIFICATION = [
    {
        'base': ['такса'],
        'coat': ['гладкошерстн', 'длинношерстн', 'жёсткошерстн', 'жесткошерстн'],
        'size': ['стандартн', 'миниатюрн', 'кролич', 'карликов'],
        'question': (
            "Уточните, пожалуйста, подвид таксы:\n"
            "• Тип шерсти: гладкошерстная / длинношерстная / жёсткошерстная\n"
            "• Размер: стандартная / миниатюрная / кроличья 🤍"
        ),
    },
    {
        'base': ['пудель'],
        'any': ['той', 'миниатюрн', 'средн', 'большой', 'королевск', 'карликов'],
        'question': "Уточните, пожалуйста, размер пуделя: той / миниатюрный / средний / большой (королевский)? 🤍",
    },
    {
        'base': ['шпиц'],
        'any': ['немецк', 'японск', 'той', 'миниатюрн', 'средн', 'большой'],
        'question': (
            "Уточните, пожалуйста, подвид шпица:\n"
            "• Тип: немецкий / японский\n"
            "• Размер: той / миниатюрный / средний / большой 🤍"
        ),
    },
    {
        'base': ['чихуахуа', 'чихуа'],
        'any': ['гладкошерстн', 'длинношерстн'],
        'question': "Уточните, пожалуйста: чихуахуа гладкошерстная или длинношерстная? 🤍",
    },
    {
        'base': ['йоркширский', 'йорк'],
        'any': ['стандартн', 'мини', 'той'],
        'question': "Уточните, пожалуйста: йоркширский терьер стандартный или мини? 🤍",
    },
]

def _check_breed_needs_clarification(breed):
    """Return clarification question if breed requires subtype info, else None."""
    if not breed:
        return None
    bl = breed.lower()
    for entry in _BREED_CLARIFICATION:
        if not any(b in bl for b in entry['base']):
            continue
        if 'coat' in entry and 'size' in entry:
            has_coat = any(c in bl for c in entry['coat'])
            has_size = any(s in bl for s in entry['size'])
            if not (has_coat and has_size):
                return entry['question']
        elif 'any' in entry:
            if not any(a in bl for a in entry['any']):
                return entry['question']
    return None

def _extract_price_from_history(history):
    """Find last 'от N€' price mentioned by Anna in conversation."""
    price_re = re.compile(r'от\s+(\d+)\s*€', re.IGNORECASE)
    for msg in reversed(history):
        if msg.get('role') == 'assistant':
            m = price_re.search(msg.get('content', ''))
            if m:
                return int(m.group(1))
    return 0

def _extract_state(history, state):
    recent = "\n".join(
        ("Клиент" if m["role"] == "user" else "Анна") + ": " + m["content"]
        for m in history[-8:]
    )
    prompt = (
        "Извлеки данные бронирования. Верни ТОЛЬКО JSON без пояснений.\n"
        "Поля: breed (название породы включая подвид если указан — например "
        "'такса гладкошерстная стандартная', 'пудель той', 'шпиц немецкий миниатюрный'; без веса), "
        "weight (вес питомца — например '30 кг'; null если не назван), "
        "service, date, time, ownerName, petName, "
        "master (null если клиент не называл мастера), "
        "clientPhone (null если клиент не называл другой номер), "
        "service_confirmed (boolean — true если клиент явно согласился с рекомендованной услугой: "
        "«да», «подходит», «хорошо», «давайте», «хочу», «окей» — это ДО вопроса о записи).\n"
        "booking_intent (boolean — true если клиент ответил «да»/«хочу»/«записывайте»/«yes» "
        "именно на вопрос «Хотите записаться?» — НЕ путать с подтверждением услуги).\n"
        "confirmed (boolean — true только если клиент явно подтвердил итоговую карточку записи: "
        "\"да\", \"подтверждаю\", \"всё верно\", \"yes\", \"jah\").\n"
        "ВАЖНО: если поле уже есть в «Текущие» и в диалоге не изменилось — "
        "оставь его значение, не возвращай null.\n"
        f"Текущие: {json.dumps(state, ensure_ascii=False)}\nДиалог:\n{recent}"
    )
    try:
        r = client_ai.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        text = r.content[0].text.strip()
        print(f"[extract_state] raw: {text[:400]}", flush=True)
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            extracted = json.loads(m.group())
            # Merge: only overwrite with non-null values so existing fields aren't cleared
            merged = dict(state)
            for k, v in extracted.items():
                if v is not None:
                    merged[k] = v
            return merged
        else:
            print(f"[extract_state] no JSON found in response", flush=True)
    except Exception as e:
        print(f"[extract_state] error: {e}", flush=True)
    return state

# ── Availability helpers ──────────────────────────────────────────────────────
_RU_MONTHS = {
    'января': '01', 'февраля': '02', 'марта': '03', 'апреля': '04',
    'мая': '05', 'июня': '06', 'июля': '07', 'августа': '08',
    'сентября': '09', 'октября': '10', 'ноября': '11', 'декабря': '12',
}

def _parse_date_to_iso(date_str):
    if not date_str:
        return None
    if re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return date_str
    m = re.match(r'^(\d{1,2})[./](\d{1,2})[./](\d{4})$', date_str)
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    dl = date_str.lower()
    for ru, num in _RU_MONTHS.items():
        if ru in dl:
            day_m = re.search(r'(\d{1,2})', dl)
            year_m = re.search(r'(\d{4})', dl)
            day = day_m.group(1).zfill(2) if day_m else '01'
            year = int(year_m.group(1)) if year_m else datetime.now().year
            month_int = int(num)
            now = datetime.now()
            if year == now.year and month_int < now.month:
                year += 1
            return f"{year}-{num}-{day}"
    return None

def _to_booking_date(date_str):
    """Convert any date string to DD.MM.YYYY (format Google Script expects)."""
    iso = _parse_date_to_iso(date_str)
    if iso and re.match(r'^\d{4}-\d{2}-\d{2}$', iso):
        y, mo, d = iso.split('-')
        return f"{d}.{mo}.{y}"
    return date_str  # fallback: send as-is, log will show it

# TTL cache for Google Script availability data (avoids hammering GS on every message)
_avail_cache: dict = {}
_AVAIL_TTL = 300  # 5 minutes

def _cache_get(key):
    entry = _avail_cache.get(key)
    if entry and (_time.time() - entry[0]) < _AVAIL_TTL:
        return entry[1]
    return None

def _cache_set(key, value):
    _avail_cache[key] = (_time.time(), value)

def _gs_url(params: dict) -> str:
    """Build full GS URL with params for logging."""
    import urllib.parse
    return GOOGLE_SCRIPT + "?" + urllib.parse.urlencode(params)

def _gs_cancel(phone):
    """Cancel booking by phone via GAS action=cancel."""
    if not GOOGLE_SCRIPT:
        return {"success": False, "error": "GOOGLE_SCRIPT not set"}
    try:
        r = requests.get(GOOGLE_SCRIPT, params={"action": "cancel", "phone": phone}, timeout=15)
        return r.json()
    except Exception as e:
        print(f"[gs_cancel] {e}", flush=True)
        return {"success": False, "error": str(e)}

def _gs_reschedule(phone, new_date, new_time):
    """Reschedule booking by phone via GAS action=reschedule."""
    if not GOOGLE_SCRIPT:
        return {"success": False, "error": "GOOGLE_SCRIPT not set"}
    try:
        r = requests.get(GOOGLE_SCRIPT, params={
            "action": "reschedule", "phone": phone,
            "newDate": new_date, "newTime": new_time,
        }, timeout=15)
        return r.json()
    except Exception as e:
        print(f"[gs_reschedule] {e}", flush=True)
        return {"success": False, "error": str(e)}

# Marker: [[ACTION:cancel:+372XXXXXXX]] or [[ACTION:reschedule:+372XX…:DD.MM.YYYY:HH:MM]]
_ACTION_RE = re.compile(r'\[\[ACTION:(cancel|reschedule):([^\]]+)\]\]')

def _process_action_markers(reply):
    """Execute GAS actions embedded in a Claude reply and strip the markers."""
    m = _ACTION_RE.search(reply)
    if not m:
        return reply
    action, args_str = m.group(1), m.group(2)
    args = args_str.split(':')
    if action == 'cancel' and args:
        res = _gs_cancel(args[0])
        print(f"[action-marker] cancel {args[0]!r} → {res}", flush=True)
    elif action == 'reschedule' and len(args) >= 3:
        # args = [phone, DD, MM, YYYY, HH, MM] after split on ':'
        # Reassemble date=DD.MM.YYYY and time=HH:MM
        phone    = args[0]
        new_date = args[1] if '.' in args[1] else '.'.join(args[1:4])
        new_time = args[-2] + ':' + args[-1] if len(args) >= 3 else ''
        # simpler: rejoin from index 1, split last two as time
        parts = args_str.split(':')
        phone     = parts[0]
        new_time  = parts[-2] + ':' + parts[-1]
        new_date  = ':'.join(parts[1:-2])   # DD.MM.YYYY — no colons inside
        res = _gs_reschedule(phone, new_date, new_time)
        print(f"[action-marker] reschedule {phone!r} → {new_date} {new_time} → {res}", flush=True)
    return _ACTION_RE.sub('', reply).strip()

_MASTERS = ["татьяна", "алиса", "кристина", "анна", "александра", "ксения"]

def _fetch_slots_for_date(date_iso, master=""):
    """Fetch slots from GS for date_iso (YYYY-MM-DD) and a specific master (lowercase).
    GS receives date as DD.MM.YYYY — same format as booking form."""
    cache_key = f"slots:{date_iso}:{master}"
    cached = _cache_get(cache_key)
    if cached is not None:
        print(f"[slots] cache hit {date_iso} master={master!r}: {cached}", flush=True)
        return cached
    if not GOOGLE_SCRIPT:
        print("[slots] GOOGLE_SCRIPT env var not set", flush=True)
        return []
    date_gs = _to_booking_date(date_iso)
    action = "slots"
    params = {"action": action, "date": date_gs, "master": master}
    print(f"[slots] action={action!r} master={master!r} GET {_gs_url(params)}", flush=True)
    try:
        r = requests.get(GOOGLE_SCRIPT, params=params, timeout=20)
        print(f"[slots] → {r.status_code}: {r.text[:300]}", flush=True)
        slots = [str(s) for s in r.json().get("slots", [])]
        _cache_set(cache_key, slots)
        return slots
    except Exception as e:
        print(f"[slots] error {date_iso} master={master!r}: {e}", flush=True)
        return []

def _fetch_available_days(master=""):
    """Fetch available days from GS for a specific master (lowercase)."""
    now = datetime.now()
    cache_key = f"days:{now.year}-{now.month}:{master}"
    cached = _cache_get(cache_key)
    if cached is not None:
        print(f"[days] cache hit master={master!r}: {cached[:5]}", flush=True)
        return cached
    if not GOOGLE_SCRIPT:
        print("[days] GOOGLE_SCRIPT env var not set", flush=True)
        return []
    action = "available_days"
    params = {"action": action, "month": now.month, "year": now.year, "master": master}
    print(f"[days] action={action!r} master={master!r} GET {_gs_url(params)}", flush=True)
    try:
        r = requests.get(GOOGLE_SCRIPT, params=params, timeout=20)
        print(f"[days] → {r.status_code}: {r.text[:300]}", flush=True)
        days = [str(d) for d in r.json().get("available", [])]
        _cache_set(cache_key, days)
        return days
    except Exception as e:
        print(f"[days] error master={master!r}: {e}", flush=True)
        return []

_NUM_TO_RU = ['', 'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
              'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря']

def _iso_to_ru_date(iso):
    """'2026-06-03' → '3 июня'"""
    try:
        _, m, d = iso.split('-')
        return f"{int(d)} {_NUM_TO_RU[int(m)]}"
    except Exception:
        return iso

# Keywords that indicate the client is asking about availability/schedule
_SCHEDULE_KW = re.compile(
    r'когда|свободн|занят|место[сть]?|дни|дат[ыею]|расписани|'
    r'слот|ближайш|доступн|прийти|записа|выбрать|в какое|какие',
    re.IGNORECASE
)

def _build_schedule_for_days():
    """Synchronous: Татьяна + Алиса, remaining days of current month, timeout=20s per GS request.
    Returns formatted string with dates and time slots."""
    _SCHED_MASTERS = ["татьяна", "алиса"]
    now = datetime.now()
    last_day = 31 if now.month == 12 else (datetime(now.year, now.month + 1, 1) - timedelta(days=1)).day
    month_remaining = {
        f"{day:02d}.{now.month:02d}.{now.year}"
        for day in range(now.day, last_day + 1)
    }

    print(f"[schedule] step 1 — available_days for {_SCHED_MASTERS}, {now.day:02d}.{now.month:02d}–{last_day:02d}.{now.month:02d}.{now.year}", flush=True)
    avail_days: set = set()
    for master in _SCHED_MASTERS:
        days = _fetch_available_days(master=master)
        matched = [d for d in days if d in month_remaining]
        print(f"[schedule] {master}: {matched}", flush=True)
        avail_days.update(matched)

    if not avail_days:
        print("[schedule] no available days", flush=True)
        return None

    print(f"[schedule] step 2 — slots for {len(avail_days)} days × {len(_SCHED_MASTERS)} masters", flush=True)
    day_slots: dict = {}
    sorted_dates = sorted(avail_days, key=lambda d: _parse_date_to_iso(d) or d)
    for day_str in sorted_dates:
        iso = _parse_date_to_iso(day_str)
        if not iso:
            continue
        for master in _SCHED_MASTERS:
            slots = _fetch_slots_for_date(iso, master=master)
            if slots:
                day_slots.setdefault(day_str, set()).update(slots)

    if not day_slots:
        print("[schedule] no slots found", flush=True)
        return None

    lines = [
        f"• {_iso_to_ru_date(_parse_date_to_iso(d))}: {', '.join(s for s in sorted(day_slots[d]) if s.endswith(':00'))}"
        for d in sorted_dates if d in day_slots
        and any(s.endswith(':00') for s in day_slots[d])
    ]
    result = "Свободные места:\n" + "\n".join(lines)
    print(f"[schedule] ready:\n{result}", flush=True)
    return result

def _fetch_schedule_bg(sid):
    """Background thread: fetch schedule and store result in session avail dict."""
    print(f"[schedule-bg] starting for sid={sid[:8]}", flush=True)
    try:
        schedule = _build_schedule_for_days()
    except Exception as e:
        print(f"[schedule-bg] error: {e}", flush=True)
        schedule = None
    if sid in _chat_sessions:
        avail = _chat_sessions[sid].get("avail", {})
        avail["full_schedule"] = schedule   # None if failed/empty
        avail["schedule_loading"] = False   # signal completion (set AFTER data)
        print(f"[schedule-bg] done: {'ok — ' + schedule[:60] if schedule else 'empty'}", flush=True)
    else:
        print(f"[schedule-bg] session {sid[:8]} gone before fetch completed", flush=True)

def _fetch_full_schedule(max_days=5):
    """Fetch available days + slots across all masters, merge results."""
    # Collect available days from each master (union)
    all_days: set = set()
    for master in _MASTERS:
        for d in _fetch_available_days(master=master):
            all_days.add(d)

    if not all_days:
        print("[schedule] no available days across any master", flush=True)
        return None

    # Sort by ISO date so earliest days come first
    sorted_days = sorted(all_days, key=lambda d: _parse_date_to_iso(d) or d)
    print(f"[schedule] available days (all masters): {sorted_days[:max_days]}", flush=True)

    lines = []
    for day_str in sorted_days[:max_days]:
        iso = _parse_date_to_iso(day_str)
        if not iso:
            continue
        # Collect slots from each master for this date (union, sorted)
        all_slots: set = set()
        for master in _MASTERS:
            for s in _fetch_slots_for_date(iso, master=master):
                all_slots.add(s)
        if all_slots:
            sorted_slots = sorted(all_slots)
            lines.append(f"• {_iso_to_ru_date(iso)}: {', '.join(sorted_slots)}")

    if not lines:
        return None
    return "Свободные места:\n" + "\n".join(lines)

def _avail_context(avail):
    if not avail:
        return ""
    lines = []
    # Full schedule (days + slots) shown after client confirms booking intent
    if avail.get("full_schedule"):
        lines.append(avail["full_schedule"])
    elif avail.get("available_days"):
        days = avail["available_days"]
        lines.append(f"Ближайшие свободные дни: {', '.join(str(d) for d in days[:10])}.")
    # Slot check for a specific date (when client named one)
    if "slots" in avail:
        label = avail.get("date_label", "")
        slots = avail["slots"]
        if slots is None:
            pass  # fetch failed — say nothing
        elif not slots:
            lines.append(f"На {label} свободных слотов нет — предложи другой день.")
        else:
            slot_list = ", ".join(slots)
            req_t = avail.get("requested_time", "")
            if req_t and not any(str(s).strip()[:5] == req_t.strip()[:5] for s in slots):
                lines.append(f"Слот {req_t} на {label} ЗАНЯТ — не подтверждай его, предложи свободный.")
            lines.append(f"Свободные слоты на {label}: {slot_list}.")
    return ("\n\n📅 РАСПИСАНИЕ (актуально):\n" + "\n".join(lines)) if lines else ""

# ── Price disclaimer (code-level, breed-aware) ────────────────────────────────
_SMOOTH_COAT_KW = [
    'такса гладк', 'джек-рассел гладк', 'джек рассел гладк',
    'французский бульдог', 'чихуахуа гладк', 'доберман',
    'боксер', 'боксёр', 'далматин', 'бигль', 'мопс',
    'бульмастиф', 'немецкий дог', 'американский стаффордш', 'питбуль',
]
_PRICE_RE = re.compile(r'\d+\s*€')
_DISCLAIMER_SMOOTH = ("Окончательная стоимость будет озвучена мастером после осмотра "
                      "при приёмке — может варьироваться в зависимости от состояния шерсти 🤍")
_DISCLAIMER_OTHER  = ("Окончательная стоимость будет озвучена мастером после осмотра "
                      "при приёмке — может варьироваться в зависимости от состояния шерсти "
                      "и наличия колтунов 🤍")

def _is_smooth_coat(breed):
    if not breed:
        return False
    b = breed.lower()
    return any(kw in b for kw in _SMOOTH_COAT_KW)

def _add_price_disclaimer(reply, breed):
    if not _PRICE_RE.search(reply):
        return reply
    if "Окончательная стоимость" in reply:
        return reply
    disclaimer = _DISCLAIMER_SMOOTH if _is_smooth_coat(breed) else _DISCLAIMER_OTHER
    return reply + "\n\n" + disclaimer

def track_client(phone, channel, text):
    if phone not in clients:
        clients[phone] = {"channel": channel, "timestamps": [], "last_seen": None, "last_text": "", "mode": "jarvis"}
    now = datetime.now()
    clients[phone]["timestamps"].append(now)
    clients[phone]["last_seen"] = now
    clients[phone]["last_text"] = text
    _log_msg(phone, channel, "in", text)
    _channel_counts[channel] = _channel_counts.get(channel, 0) + 1

def should_reply(phone, channel="whatsapp"):
    if manual_mode: return False
    if pause_until and datetime.now() < pause_until: return False
    if not jarvis_enabled: return False
    if channel == "instagram" and not instagram_enabled: return False
    if clients.get(phone, {}).get("mode") == "manual": return False
    return True

def get_ai_reply(sender_id, text):
    if sender_id not in conversation_history:
        conversation_history[sender_id] = []
    history = conversation_history[sender_id]
    history.append({"role": "user", "content": text})
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
        conversation_history[sender_id] = history
    response = client_ai.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=history
    )
    reply = _process_action_markers(response.content[0].text)
    history.append({"role": "assistant", "content": reply})
    return reply

def send_whatsapp(to, text):
    url = "https://graph.facebook.com/v18.0/" + (WHATSAPP_PHONE_ID or "") + "/messages"
    headers = {"Authorization": "Bearer " + (WHATSAPP_TOKEN or ""), "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=data)

INSTAGRAM_ACCOUNT_ID = "17841479914115449"

def send_instagram(to, text):
    url = "https://graph.facebook.com/v18.0/" + INSTAGRAM_ACCOUNT_ID + "/messages"
    headers = {"Authorization": "Bearer " + (INSTAGRAM_TOKEN or ""), "Content-Type": "application/json"}
    data = {"recipient": {"id": to}, "message": {"text": text}}
    requests.post(url, headers=headers, json=data)

def handle_message(sender_id, text, channel):
    track_client(sender_id, channel, text)
    if not should_reply(sender_id, channel):
        return None
    reply = get_ai_reply(sender_id, text)
    _log_msg(sender_id, channel, "out", reply)
    if channel == "instagram":
        send_instagram(sender_id, reply)
    else:
        send_whatsapp(sender_id, reply)
    return reply
# ── EMAIL CONFIRMATION ──────────────────────
@app.route("/confirm")
def confirm():
    import requests as req_lib
    import urllib.parse

    booking_id = request.args.get("id", "")

    if booking_id:
        try:
            r = req_lib.get(GOOGLE_SCRIPT, params={"action": "get", "id": booking_id}, timeout=10, allow_redirects=True)
            print(f"DEBUG confirm id={booking_id} status={r.status_code} body={r.text[:200]}", flush=True)
            data = r.json()
            email   = data.get("email", "")
            name    = data.get("name", "")
            date    = data.get("date", "")
            time    = data.get("time", "")
            service = data.get("service", "")
            master  = data.get("master", "")
            breed   = data.get("breed", "")
            pet     = data.get("pet", "")
            phone   = data.get("phone", "")
            lang    = data.get("lang", "ru")
        except Exception as e:
            return f"Ошибка получения данных: {str(e)}", 500
    else:
        email   = urllib.parse.unquote(request.args.get("email", ""))
        name    = request.args.get("name", "")
        date    = request.args.get("date", "")
        time    = request.args.get("time", "")
        service = request.args.get("service", "")
        master  = request.args.get("master", "")
        breed   = request.args.get("breed", "")
        pet     = request.args.get("pet", "")
        phone   = request.args.get("phone", "")
        lang    = request.args.get("lang", "ru")

    if lang not in ("ru", "en", "et"):
        lang = "ru"

    # ── Email (optional — only sent when email address is provided) ────────────
    email_result = "Email не указан — пропущено"
    if email:
        resend_api_key = os.environ.get("RESEND_API_KEY")
        if not resend_api_key:
            email_result = "RESEND_API_KEY не настроен — email пропущен"
        else:
            td = "padding:8px;border-bottom:1px solid #eee"
            tl = f"{td};color:#666"
            if lang == "en":
                heading  = f"Thank you for booking, {name}!"
                _ms      = f" Your groomer: <b>{master}</b>." if master else ""
                subhead  = f"Your appointment at R&amp;J Grooming is confirmed.{_ms}"
                labels   = ["Date", "Time", "Groomer", "Service", "Breed", "Pet's name"]
                footer_t = "We look forward to seeing you and your pet!"
                address  = "Address: Allveelaeva 4, Tallinn<br>Phone: +372 587 35456"
            elif lang == "et":
                heading  = f"Aitäh broneeringu eest, {name}!"
                _ms      = f" Teie meister: <b>{master}</b>." if master else ""
                subhead  = f"Teie broneering R&amp;J Groomingus on kinnitatud.{_ms}"
                labels   = ["Kuupäev", "Kellaaeg", "Meister", "Teenus", "Tõug", "Lemmiklooma nimi"]
                footer_t = "Ootame teid ja teie lemmikut!"
                address  = "Aadress: Allveelaeva 4, Tallinn<br>Telefon: +372 587 35456"
            else:
                heading  = f"Спасибо за запись, {name}!"
                _ms      = f" Ваш мастер: <b>{master}</b>." if master else ""
                subhead  = f"Ваша запись в R&amp;J Grooming подтверждена.{_ms}"
                labels   = ["Дата", "Время", "Мастер", "Услуга", "Порода", "Кличка"]
                footer_t = "Ждём вас и вашего питомца!"
                address  = "Адрес: Allveelaeva 4, Tallinn<br>Телефон: +372 587 35456"

            values = [date, time, master, service, breed, pet]
            rows = "".join(
                f'<tr><td style="{tl}">{l}</td>'
                f'<td style="{td}"><b>{v}</b></td></tr>'
                for l, v in zip(labels, values) if v  # skip rows with empty values
            )
            body_html = (
                f'<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;color:#222;'
                f'max-width:600px;margin:0 auto;padding:20px;">'
                f'<h2 style="color:#c9a84c;font-family:Georgia,serif;">{heading}</h2>'
                f'<p>{subhead}</p>'
                f'<table style="border-collapse:collapse;width:100%;margin:16px 0;">{rows}</table>'
                f'<p style="color:#666;font-size:14px;">{address}</p>'
                f'<p style="color:#c9a84c;font-style:italic;">{footer_t}</p>'
                f'</body></html>'
            )
            if lang == "en":
                subject = f"R&J Grooming booking confirmed - {date} at {time}"
            elif lang == "et":
                subject = f"R&J Grooming broneering kinnitatud - {date} kell {time}"
            else:
                subject = f"Запись в R&J Grooming подтверждена - {date} в {time}"

            try:
                r = req_lib.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {resend_api_key}", "Content-Type": "application/json"},
                    json={
                        "from": "R&J Grooming <booking@rjgrooming.salon>",
                        "to": [email.strip()],
                        "subject": subject,
                        "html": body_html,
                    },
                    timeout=10
                )
                if r.status_code == 200:
                    email_result = f"Email отправлен на {email}"
                else:
                    email_result = f"Ошибка email: {r.status_code} {r.text}"
            except Exception as e:
                email_result = f"Ошибка email: {str(e)}"

    # Отправка SMS через Twilio
    sms_result = "SMS не отправлен"
    twilio_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    twilio_token = os.environ.get("TWILIO_AUTH_TOKEN")
    twilio_phone = os.environ.get("TWILIO_PHONE", "+37266922128")
    if phone and twilio_sid and twilio_token:
        try:
            # Форматируем номер (убираем двойное кодирование %2B от Safari)
            import urllib.parse as _ul
            p = _ul.unquote(_ul.unquote(phone)).strip().replace(" ", "").replace("-", "")
            if not p.startswith("+"):
                p = "+" + p
            print(f"PHONE RAW: {phone!r} → DECODED: {p!r}", flush=True)
            if lang == "en":
                _m = f"Groomer: {master}. " if master else ""
                sms_body = f"R&J Grooming: booking confirmed! {_m}{date} at {time}. Address: Allveelaeva 4, Tallinn"
            elif lang == "et":
                _m = f"Meister: {master}. " if master else ""
                sms_body = f"R&J Grooming: broneering kinnitatud! {_m}{date} kell {time}. Aadress: Allveelaeva 4, Tallinn"
            else:
                _m = f"Мастер: {master}. " if master else ""
                sms_body = f"R&J Grooming: запись подтверждена! {_m}Дата: {date} в {time}. Адрес: Allveelaeva 4, Tallinn"
            sms_url = f"https://api.twilio.com/2010-04-01/Accounts/{twilio_sid}/Messages.json"
            sms_resp = req_lib.post(
                sms_url,
                auth=(twilio_sid, twilio_token),
                data={"From": twilio_phone, "To": p, "Body": sms_body},
                timeout=10
            )
            if sms_resp.status_code == 201:
                sms_result = f"SMS отправлен на {p}"
            else:
                sms_result = f"Ошибка SMS: {sms_resp.status_code} {sms_resp.text}"
        except Exception as e:
            sms_result = f"Ошибка SMS: {str(e)}"

    return f"{email_result}<br>{sms_result}", 200

# ── BOOKING → GOOGLE CALENDAR ──────────────────────────────────────────────
GOOGLE_SCRIPT = os.environ.get("GOOGLE_SCRIPT", "")

# ── Кеш полной истории календаря (общий для всех страниц админки) ───────
_full_history_cache = {"data": None, "fetched_at": 0, "to_date": None}
_full_history_cache_lock = threading.Lock()
_FULL_HISTORY_CACHE_TTL = 180  # секунд

def _fetch_full_history_bookings():
    """Общая история броней с 01.01.2020 по (сегодня+180дн), с кешем на
    несколько минут — избавляет от повторного тяжёлого запроса к календарю
    при каждом открытии Клиенты/Напоминания/Поиск/Экспорт."""
    today = datetime.now(_REMINDER_TZ).date() if _REMINDER_TZ else datetime.utcnow().date()
    to_date = (today + timedelta(days=180)).strftime("%d.%m.%Y")
    now_ts = _time.time()

    with _full_history_cache_lock:
        if (_full_history_cache["data"] is not None
                and _full_history_cache["to_date"] == to_date
                and now_ts - _full_history_cache["fetched_at"] < _FULL_HISTORY_CACHE_TTL):
            return _full_history_cache["data"]

    params = {"action": "stats", "from": "01.01.2020", "to": to_date}
    r = requests.get(GOOGLE_SCRIPT, params=params, timeout=30)
    data = r.json()
    bookings = data.get("bookings", []) if isinstance(data, dict) else []

    with _full_history_cache_lock:
        _full_history_cache["data"] = bookings
        _full_history_cache["fetched_at"] = now_ts
        _full_history_cache["to_date"] = to_date

    return bookings

# ── 35-ДНЕВНОЕ НАПОМИНАНИЕ (первый визит → напоминание админу в Telegram) ──
try:
    from zoneinfo import ZoneInfo
    _REMINDER_TZ = ZoneInfo("Europe/Tallinn")
except Exception:
    _REMINDER_TZ = None

def _send_reminder_telegram(text):
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (tg_token and tg_chat_id):
        msg = "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы"
        print(f"REMINDER: {msg}", flush=True)
        return {"ok": False, "error": msg}
    # Telegram режет сообщения длиннее 4096 символов — бьём по границам "\n\n"
    chunks = []
    remaining = text
    LIMIT = 3800
    while len(remaining) > LIMIT:
        cut = remaining.rfind("\n\n", 0, LIMIT)
        if cut <= 0:
            cut = LIMIT
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)

    results = []
    for i, chunk in enumerate(chunks):
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json={"chat_id": tg_chat_id, "text": chunk, "parse_mode": "HTML"},
                timeout=10
            )
            body = resp.json()
            results.append({"chunk": i + 1, "http_status": resp.status_code, "telegram_response": body})
            if not body.get("ok"):
                print(f"REMINDER TELEGRAM API ERROR (chunk {i+1}/{len(chunks)}): {body}", flush=True)
            if len(chunks) > 1:
                _time.sleep(0.5)
        except Exception as e:
            results.append({"chunk": i + 1, "error": str(e)})
            print(f"REMINDER TELEGRAM ERROR (chunk {i+1}/{len(chunks)}): {e}", flush=True)
    return {"ok": all(r.get("telegram_response", {}).get("ok") for r in results if "telegram_response" in r), "chunks": len(chunks), "results": results}

def check_35day_reminders(target_date=None, send_telegram=True):
    """Два напоминания на клиента после его последнего визита:
    #1 — на 35-й день, #2 — на 42-й день (35+7), если он не записался повторно.
    Если между визитом и сегодня есть ЛЮБАЯ более поздняя запись (прошлая или
    будущая) — значит клиент уже перезаписался, и напоминания не шлём.
    target_date позволяет посчитать на любую дату (например, на завтра) без
    реальной отправки — для этого передать send_telegram=False."""
    try:
        today = target_date or (datetime.now(_REMINDER_TZ).date() if _REMINDER_TZ else datetime.utcnow().date())
        from_date = "01.01.2020"
        to_date = (today + timedelta(days=120)).strftime("%d.%m.%Y")
        r = requests.get(GOOGLE_SCRIPT, params={"action": "stats", "from": from_date, "to": to_date}, timeout=30)
        data = r.json()
        bookings = data.get("bookings", []) if isinstance(data, dict) else []

        by_phone = {}
        for b in bookings:
            phone = (b.get("clientPhone") or "").strip()
            if not phone:
                continue
            try:
                d = datetime.strptime(b.get("date", ""), "%d.%m.%Y").date()
            except Exception:
                continue
            by_phone.setdefault(phone, []).append({
                "date": d,
                "name": b.get("clientName", ""),
                "pet": b.get("petName", ""),
                "master": b.get("master", ""),
                "breed": b.get("breed", "")
            })

        due = []
        for phone, visits in by_phone.items():
            past_visits = [v for v in visits if v["date"] <= today]
            if not past_visits:
                continue
            last_visit = max(past_visits, key=lambda v: v["date"])
            has_rebooked = any(v["date"] > last_visit["date"] for v in visits)
            if has_rebooked:
                continue
            days_since = (today - last_visit["date"]).days
            stage = {35: 1, 42: 2}.get(days_since)
            if stage:
                info = dict(last_visit)
                info["stage"] = stage
                info["days_since"] = days_since
                due.append((phone, info))

        if due and send_telegram:
            lines = ["🔔 <b>Напоминания клиентам</b>", ""]
            for phone, info in due:
                lines.append(
                    f"{'1️⃣' if info['stage']==1 else '2️⃣'} <b>Напоминание #{info['stage']}</b> ({info['days_since']} дн. без визита)\n"
                    f"👤 {info['name']} ({phone})\n"
                    f"🐾 {info['pet']} — {info['breed']}\n"
                    f"Последний визит: {info['date'].strftime('%d.%m.%Y')} у {info['master']}\n"
                )
            _send_reminder_telegram("\n".join(lines))

        print(f"REMINDER CHECK ({today}): найдено {len(due)} клиентов", flush=True)
        return due
    except Exception as e:
        print(f"REMINDER CHECK ERROR: {e}", flush=True)
        return []

def _reminder_scheduler_loop():
    while True:
        try:
            now = datetime.now(_REMINDER_TZ) if _REMINDER_TZ else datetime.utcnow()
            next_run = now.replace(hour=10, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            sleep_seconds = (next_run - now).total_seconds()
            _time.sleep(max(sleep_seconds, 60))
            check_35day_reminders()
        except Exception as e:
            print(f"REMINDER SCHEDULER ERROR: {e}", flush=True)
            _time.sleep(3600)

threading.Thread(target=_reminder_scheduler_loop, daemon=True).start()

_REMINDER_STATUS_PUBLIC_ID = "rjgrooming/reminder_status_index.json"

def _reminder_status_url():
    return f"https://res.cloudinary.com/{CLOUDINARY_CLOUD_NAME}/raw/upload/{_REMINDER_STATUS_PUBLIC_ID}"

def _load_reminder_status():
    try:
        r = requests.get(_reminder_status_url(), timeout=8)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}

def _save_reminder_status(data):
    import hashlib as _hashlib
    timestamp = int(_time.time())
    params_to_sign = {"overwrite": "true", "public_id": _REMINDER_STATUS_PUBLIC_ID, "timestamp": timestamp}
    to_sign = "&".join(f"{k}={v}" for k, v in sorted(params_to_sign.items()))
    signature = _hashlib.sha1((to_sign + CLOUDINARY_API_SECRET).encode("utf-8")).hexdigest()
    payload = json.dumps(data, ensure_ascii=False)
    try:
        resp = requests.post(
            f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/raw/upload",
            data={
                "api_key": CLOUDINARY_API_KEY,
                "timestamp": timestamp,
                "public_id": _REMINDER_STATUS_PUBLIC_ID,
                "overwrite": "true",
                "signature": signature,
            },
            files={"file": ("data.json", payload, "application/json")},
            timeout=15
        )
        if resp.status_code != 200:
            print(f"REMINDER STATUS SAVE ERROR: {resp.status_code} {resp.text[:200]}", flush=True)
        return resp.status_code == 200
    except Exception as e:
        print(f"REMINDER STATUS SAVE ERROR: {e}", flush=True)
        return False

# ── АБОНЕМЕНТЫ (цифровая карта посещений) ────────────────────────────────
_MEMBERSHIP_INDEX_PUBLIC_ID = "rjgrooming/membership_index.json"

def _membership_index_url():
    return f"https://res.cloudinary.com/{CLOUDINARY_CLOUD_NAME}/raw/upload/{_MEMBERSHIP_INDEX_PUBLIC_ID}"

def _load_memberships():
    import base64 as _b64_mod
    try:
        auth = _b64_mod.b64encode(f"{CLOUDINARY_API_KEY}:{CLOUDINARY_API_SECRET}".encode()).decode()
        meta_r = requests.get(
            f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/resources/raw/upload/{_MEMBERSHIP_INDEX_PUBLIC_ID}",
            headers={"Authorization": f"Basic {auth}"},
            timeout=8
        )
        if meta_r.status_code == 200:
            version = meta_r.json().get("version")
            if version:
                versioned_url = f"https://res.cloudinary.com/{CLOUDINARY_CLOUD_NAME}/raw/upload/v{version}/{_MEMBERSHIP_INDEX_PUBLIC_ID}"
                r = requests.get(versioned_url, timeout=8)
                if r.status_code == 200:
                    return r.json()
    except Exception as e:
        print(f"MEMBERSHIP LOAD (versioned) ERROR: {e}", flush=True)

    # запасной вариант — обычный запрос без версии
    try:
        r = requests.get(_membership_index_url(), params={"_": int(_time.time() * 1000)}, timeout=8)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}

def _save_memberships(data):
    import hashlib as _hashlib
    timestamp = int(_time.time())
    params_to_sign = {"invalidate": "true", "overwrite": "true", "public_id": _MEMBERSHIP_INDEX_PUBLIC_ID, "timestamp": timestamp}
    to_sign = "&".join(f"{k}={v}" for k, v in sorted(params_to_sign.items()))
    signature = _hashlib.sha1((to_sign + CLOUDINARY_API_SECRET).encode("utf-8")).hexdigest()
    payload = json.dumps(data, ensure_ascii=False)
    try:
        resp = requests.post(
            f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/raw/upload",
            data={
                "api_key": CLOUDINARY_API_KEY,
                "timestamp": timestamp,
                "public_id": _MEMBERSHIP_INDEX_PUBLIC_ID,
                "overwrite": "true",
                "invalidate": "true",
                "signature": signature,
            },
            files={"file": ("data.json", payload, "application/json")},
            timeout=15
        )
        if resp.status_code != 200:
            print(f"MEMBERSHIP SAVE ERROR: {resp.status_code} {resp.text[:200]}", flush=True)
        return resp.status_code == 200
    except Exception as e:
        print(f"MEMBERSHIP SAVE ERROR: {e}", flush=True)
        return False

_LOGO_B64_CACHE = None
def _get_logo_b64():
    """Извлекает base64 логотипа R&J из уже закоммиченного BOOKING_HTML_B64 (первая data:image/png в разметке)."""
    global _LOGO_B64_CACHE
    if _LOGO_B64_CACHE:
        return _LOGO_B64_CACHE
    try:
        import base64 as _b64_mod
        html = _b64_mod.b64decode(BOOKING_HTML_B64).decode("utf-8")
        m = re.search(r"data:image/png;base64,([A-Za-z0-9+/=]+)", html)
        if m:
            _LOGO_B64_CACHE = m.group(1)
            return _LOGO_B64_CACHE
    except Exception as e:
        print(f"LOGO EXTRACT ERROR: {e}", flush=True)
    return ""

def _next_membership_id(memberships):
    nums = []
    for k in memberships.keys():
        try:
            nums.append(int(k.replace("RJ-", "")))
        except Exception:
            pass
    n = (max(nums) + 1) if nums else 1
    return f"RJ-{n:03d}"

@app.route("/api/mark-reminder", methods=["POST"])
def api_mark_reminder():
    body = request.get_json(force=True) or {}
    phone = (body.get("phone") or "").strip()
    date_str = body.get("date") or ""
    stage = body.get("stage")
    done = bool(body.get("done"))
    if not phone or stage not in (1, 2):
        return jsonify({"success": False, "error": "phone и stage (1 или 2) обязательны"}), 400

    status = _load_reminder_status()
    entry = status.get(phone, {})
    if entry.get("last_date") != date_str:
        entry = {"last_date": date_str}
    entry[f"stage{stage}_done"] = done
    status[phone] = entry
    ok = _save_reminder_status(status)
    return jsonify({"success": ok})

# ── ФОТО АНКЕТ ПИТОМЦЕВ (Cloudinary, постоянное хранение) ───────────────
CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "u35xfusf")
CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY", "555146516498372")
CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "AQTPeJ2sfE0XhjtlCwz_PcM2ZrQ")

def _normalize_phone(phone):
    """Добавляет +372, если у номера нет кода страны."""
    p = (phone or "").strip()
    if not p:
        return p
    if p.startswith("+"):
        return p
    digits = re.sub(r"[^\d]", "", p)
    if not digits:
        return p
    if digits.startswith("372"):
        return "+" + digits
    return "+372" + digits

def _pet_photo_key(phone, pet):
    raw = f"{(phone or '').strip()}|{(pet or '').strip()}".lower()
    import hashlib
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()

def _pet_photo_public_id(key):
    return f"rjgrooming/pet_{key}"

def _pet_photo_url(key):
    return f"https://res.cloudinary.com/{CLOUDINARY_CLOUD_NAME}/image/upload/f_auto,q_auto/{_pet_photo_public_id(key)}"

@app.route("/api/upload-pet-photo", methods=["POST"])
def api_upload_pet_photo():
    import hashlib as _hashlib
    phone = (request.form.get("phone") or "").strip()
    pet = (request.form.get("pet") or "").strip()
    file = request.files.get("photo")
    if not phone or not pet or not file:
        return jsonify({"success": False, "error": "phone, pet и photo обязательны"}), 400

    key = _pet_photo_key(phone, pet)
    public_id = _pet_photo_public_id(key)
    timestamp = int(_time.time())

    params_to_sign = {"overwrite": "true", "public_id": public_id, "timestamp": timestamp}
    to_sign = "&".join(f"{k}={v}" for k, v in sorted(params_to_sign.items()))
    signature = _hashlib.sha1((to_sign + CLOUDINARY_API_SECRET).encode("utf-8")).hexdigest()

    try:
        resp = requests.post(
            f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/image/upload",
            data={
                "api_key": CLOUDINARY_API_KEY,
                "timestamp": timestamp,
                "public_id": public_id,
                "overwrite": "true",
                "signature": signature,
            },
            files={"file": (file.filename or "photo.jpg", file.stream, file.mimetype)},
            timeout=30
        )
        body = resp.json()
        if resp.status_code != 200 or "secure_url" not in body:
            return jsonify({"success": False, "error": body.get("error", {}).get("message", "Cloudinary upload failed")}), 500
        return jsonify({"success": True, "url": _pet_photo_url(key)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# ── РУЧНЫЕ ДАННЫЕ КЛИЕНТА (email, Instagram) — Cloudinary raw, единый индекс ──
_CLIENT_DATA_INDEX_PUBLIC_ID = "rjgrooming/client_data_index.json"

def _client_data_key(phone):
    import hashlib
    return hashlib.sha1((phone or "").strip().lower().encode("utf-8")).hexdigest()

def _client_data_index_url():
    return f"https://res.cloudinary.com/{CLOUDINARY_CLOUD_NAME}/raw/upload/{_CLIENT_DATA_INDEX_PUBLIC_ID}"

def _load_all_client_data():
    """Один запрос — все сохранённые правки клиентов разом (ключ = хэш телефона)."""
    try:
        r = requests.get(_client_data_index_url(), timeout=8)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}

def _save_all_client_data(data):
    import hashlib as _hashlib
    timestamp = int(_time.time())
    params_to_sign = {"overwrite": "true", "public_id": _CLIENT_DATA_INDEX_PUBLIC_ID, "timestamp": timestamp}
    to_sign = "&".join(f"{k}={v}" for k, v in sorted(params_to_sign.items()))
    signature = _hashlib.sha1((to_sign + CLOUDINARY_API_SECRET).encode("utf-8")).hexdigest()
    payload = json.dumps(data, ensure_ascii=False)
    try:
        resp = requests.post(
            f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/raw/upload",
            data={
                "api_key": CLOUDINARY_API_KEY,
                "timestamp": timestamp,
                "public_id": _CLIENT_DATA_INDEX_PUBLIC_ID,
                "overwrite": "true",
                "signature": signature,
            },
            files={"file": ("data.json", payload, "application/json")},
            timeout=20
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"CLIENT DATA SAVE ERROR: {e}", flush=True)
        return False

def _load_client_data(phone):
    """Совместимость: одна запись из общего индекса."""
    key = _client_data_key(phone)
    return _load_all_client_data().get(key, {})

@app.route("/api/save-client-data", methods=["POST"])
def api_save_client_data():
    body = request.get_json(force=True) or {}
    phone = (body.get("phone") or "").strip()
    if not phone:
        return jsonify({"success": False, "error": "phone обязателен"}), 400

    name = (body.get("name") or "").strip()
    phone_override = (body.get("phone_override") or "").strip()
    email = (body.get("email") or "").strip()
    instagram = (body.get("instagram") or "").strip().lstrip("@")
    comment = (body.get("comment") or "").strip()

    key = _client_data_key(phone)
    all_data = _load_all_client_data()
    all_data[key] = {
        "name": name, "phone_override": phone_override,
        "email": email, "instagram": instagram, "comment": comment
    }
    ok = _save_all_client_data(all_data)
    if not ok:
        return jsonify({"success": False, "error": "Cloudinary upload failed"}), 500
    return jsonify({"success": True, "name": name, "phone_override": phone_override, "email": email, "instagram": instagram, "comment": comment})

def _get_reminder_dashboard_rows():
    """Общая функция: последний визит на клиента за всю историю,
    с расчётом стадии напоминания. Используется дашбордом и отчётами."""
    today = datetime.now(_REMINDER_TZ).date() if _REMINDER_TZ else datetime.utcnow().date()
    bookings = _fetch_full_history_bookings()

    by_phone = {}
    for b in bookings:
        phone = (b.get("clientPhone") or "").strip()
        if not phone:
            continue
        try:
            d = datetime.strptime(b.get("date", ""), "%d.%m.%Y").date()
        except Exception:
            continue
        if d > today:
            continue
        if phone not in by_phone or d > by_phone[phone]["date"]:
            by_phone[phone] = {
                "date": d,
                "name": b.get("clientName", ""),
                "pet": b.get("petName", ""),
                "master": b.get("master", ""),
                "breed": b.get("breed", ""),
                "email": b.get("clientEmail", "")
            }

    reminder_status = _load_reminder_status()
    all_client_data = _load_all_client_data()
    rows = []
    for phone, info in by_phone.items():
        days_since = (today - info["date"]).days
        if days_since >= 42:
            stage_status = "done"
        elif days_since >= 35:
            stage_status = "stage1"
        else:
            stage_status = "pending"
        date_str = info["date"].strftime("%d.%m.%Y")
        saved = reminder_status.get(phone, {})
        stage1_done = bool(saved.get("stage1_done")) if saved.get("last_date") == date_str else False
        stage2_done = bool(saved.get("stage2_done")) if saved.get("last_date") == date_str else False
        client_saved = all_client_data.get(_client_data_key(phone), {})
        display_name = client_saved.get("name") or info["name"]
        display_phone = _normalize_phone(client_saved.get("phone_override") or phone)
        display_email = client_saved.get("email") or info["email"]
        rows.append({
            "phone": phone, "display_phone": display_phone, "name": display_name, "pet": info["pet"],
            "breed": info["breed"], "master": info["master"], "email": display_email,
            "date": info["date"], "days_since": days_since, "status": stage_status,
            "stage1_done": stage1_done, "stage2_done": stage2_done
        })
    rows.sort(key=lambda x: -x["days_since"])
    return rows, today

def send_full_40day_report():
    """Полный список клиентов за всю историю (по последнему визиту)
    со статусом по каждому — сколько дней прошло и на какой они стадии."""
    try:
        today = datetime.now(_REMINDER_TZ).date() if _REMINDER_TZ else datetime.utcnow().date()
        from_date = "01.01.2020"
        to_date = today.strftime("%d.%m.%Y")
        r = requests.get(GOOGLE_SCRIPT, params={"action": "stats", "from": from_date, "to": to_date}, timeout=30)
        data = r.json()
        bookings = data.get("bookings", []) if isinstance(data, dict) else []

        by_phone = {}
        for b in bookings:
            phone = (b.get("clientPhone") or "").strip()
            if not phone:
                continue
            try:
                d = datetime.strptime(b.get("date", ""), "%d.%m.%Y").date()
            except Exception:
                continue
            if d > today:
                continue
            if phone not in by_phone or d > by_phone[phone]["date"]:
                by_phone[phone] = {
                    "date": d,
                    "name": b.get("clientName", ""),
                    "pet": b.get("petName", ""),
                    "master": b.get("master", ""),
                    "breed": b.get("breed", "")
                }

        rows = sorted(by_phone.items(), key=lambda kv: kv[1]["date"])
        if not rows:
            tg_result = _send_reminder_telegram("🔔 <b>Отчёт за 40 дней</b>\n\nНет визитов за последние 40 дней.")
            print("FULL REPORT: клиентов не найдено", flush=True)
            return [], tg_result

        lines = [f"🔔 <b>Отчёт по клиентам за 40 дней</b> ({len(rows)} чел.)", ""]
        for phone, info in rows:
            days_since = (today - info["date"]).days
            if days_since >= 42:
                status = "✅ напоминание #2 отправлено"
            elif days_since >= 35:
                status = "1️⃣ напоминание #1 отправлено"
            else:
                status = f"⏳ ещё {35 - days_since} дн. до напоминания"
            lines.append(
                f"👤 {info['name']} ({phone}) — {days_since} дн.\n"
                f"🐾 {info['pet']} — {info['breed']}\n"
                f"Визит: {info['date'].strftime('%d.%m.%Y')} у {info['master']}\n"
                f"{status}\n"
            )
        tg_result = _send_reminder_telegram("\n".join(lines))
        print(f"FULL REPORT: {len(rows)} клиентов отправлено", flush=True)
        return rows, tg_result
    except Exception as e:
        print(f"FULL REPORT ERROR: {e}", flush=True)
        return [], {"ok": False, "error": str(e)}

@app.route("/api/send-full-report")
def api_send_full_report():
    rows, tg_result = send_full_40day_report()
    return jsonify({"success": True, "count": len(rows), "telegram": tg_result})

@app.route("/api/check-reminders")
def api_check_reminders():
    due = check_35day_reminders()
    return jsonify({"success": True, "due_count": len(due), "clients": [{"phone": p, **{k: (v.isoformat() if k == "date" else v) for k, v in i.items()}} for p, i in due]})

@app.route("/api/check-reminders-tomorrow")
def api_check_reminders_tomorrow():
    today = datetime.now(_REMINDER_TZ).date() if _REMINDER_TZ else datetime.utcnow().date()
    tomorrow = today + timedelta(days=1)
    due = check_35day_reminders(target_date=tomorrow, send_telegram=False)
    return jsonify({"success": True, "date": tomorrow.isoformat(), "due_count": len(due), "clients": [{"phone": p, **{k: (v.isoformat() if k == "date" else v) for k, v in i.items()}} for p, i in due]})

@app.route("/book", methods=["POST", "OPTIONS"])
def book():
    if request.method == "OPTIONS":
        resp = app.make_default_options_response()
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "POST"
        return resp
    data = request.get_json()
    try:
        print(f"BOOK DATA: {data}", flush=True)
        r = requests.get(GOOGLE_SCRIPT, params=data, timeout=10)
        print(f"GOOGLE SCRIPT RESPONSE: {r.text[:500]}", flush=True)
        resp = jsonify({"success": True})

        # Email уведомление о новой онлайн-записи
        resend_key = os.environ.get("RESEND_API_KEY")
        if resend_key:
            try:
                requests.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
                    json={
                        "from": "booking@rjgrooming.salon",
                        "to": ["myrnj1@gmail.com"],
                        "subject": "Новая онлайн-запись — R&J Grooming",
                        "html": (
                            "<h2>Новая онлайн-запись</h2>"
                            f"<p><b>Клиент:</b> {data.get('name','')}</p>"
                            f"<p><b>Телефон:</b> {data.get('phone','')}</p>"
                            f"<p><b>Email:</b> {data.get('email','')}</p>"
                            f"<p><b>Порода:</b> {data.get('breedDisplay') or data.get('breed','')}</p>"
                            f"<p><b>Услуга:</b> {data.get('service','')}</p>"
                            f"<p><b>Мастер:</b> {data.get('master','')}</p>"
                            f"<p><b>Дата:</b> {data.get('date','')} в {data.get('time','')}</p>"
                            f"<p><b>Стоимость:</b> {data.get('price','')} EUR</p>"
                            f"<p><b>Кличка питомца:</b> {data.get('pet','')}</p>"
                        )
                    },
                    timeout=10
                )
            except Exception as e:
                print(f"BOOK EMAIL ERROR: {e}", flush=True)

        # Telegram уведомление о новой онлайн-записи
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if tg_token and tg_chat_id:
            try:
                tg_text = (
                    "🐾 <b>Новая онлайн-запись</b>\n\n"
                    f"<b>Клиент:</b> {data.get('name','')}\n"
                    f"<b>Телефон:</b> {data.get('phone','')}\n"
                    f"<b>Порода:</b> {data.get('breedDisplay') or data.get('breed','')}\n"
                    f"<b>Услуга:</b> {data.get('service','')}\n"
                    f"<b>Мастер:</b> {data.get('master','')}\n"
                    f"<b>Дата:</b> {data.get('date','')} в {data.get('time','')}\n"
                    f"<b>Стоимость:</b> {data.get('price','')} EUR\n"
                    f"<b>Кличка питомца:</b> {data.get('pet','')}"
                )
                requests.post(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    json={"chat_id": tg_chat_id, "text": tg_text, "parse_mode": "HTML"},
                    timeout=10
                )
            except Exception as e:
                print(f"BOOK TELEGRAM ERROR: {e}", flush=True)
    except Exception as e:
        print(f"BOOK ERROR: {e}", flush=True)
        resp = jsonify({"success": False, "error": str(e)})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp

# ── BOOKING APP PAGE ───────────────────────────────────────────────────────
BOOKING_HTML_B64 = "77u/PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9InJ1Ij4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idGhlbWUtY29sb3IiIGNvbnRlbnQ9IiMwYTBhMGEiPgo8bWV0YSBuYW1lPSJ2aWV3cG9ydCIgY29udGVudD0id2lkdGg9ZGV2aWNlLXdpZHRoLGluaXRpYWwtc2NhbGU9MSI+Cjx0aXRsZT5SJkogR3Jvb21pbmc8L3RpdGxlPgo8bGluayBocmVmPSJodHRwczovL2ZvbnRzLmdvb2dsZWFwaXMuY29tL2NzczI/ZmFtaWx5PUNvcm1vcmFudCtHYXJhbW9uZDp3Z2h0QDQwMDs2MDAmZmFtaWx5PVBsYXlmYWlyK0Rpc3BsYXk6aXRhbCx3Z2h0QDAsNDAwOzAsNjAwOzAsNzAwOzEsNDAwJmZhbWlseT1Nb250c2VycmF0OndnaHRAMzAwOzQwMDs1MDA7NjAwJmRpc3BsYXk9c3dhcCIgcmVsPSJzdHlsZXNoZWV0Ij4KPHN0eWxlPgoqe2JveC1zaXppbmc6Ym9yZGVyLWJveDttYXJnaW46MDtwYWRkaW5nOjB9Cmh0bWwsYm9keXttaW4taGVpZ2h0OjEwMHZoO2JhY2tncm91bmQ6IzBhMGEwYTtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXdlaWdodDo0MDB9Ci5zY3JlZW57ZGlzcGxheTpub25lO21pbi1oZWlnaHQ6MTAwdmg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2FsaWduLWl0ZW1zOmNlbnRlcjtwYWRkaW5nOjQ4cHggMCA2NHB4fQouc2NyZWVuLmFjdGl2ZXtkaXNwbGF5OmZsZXh9Ci5jb257d2lkdGg6MTAwJTttYXgtd2lkdGg6NDAwcHg7cGFkZGluZzowIDI4cHh9Ci5iYWNrLWJ0bntkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo4cHg7Y29sb3I6I2ZmZmZmZjtmb250LXNpemU6MC44cmVtO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyOm5vbmU7Y3Vyc29yOnBvaW50ZXI7cGFkZGluZzowO21hcmdpbi1ib3R0b206MzZweDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXdlaWdodDozMDA7dHJhbnNpdGlvbjpjb2xvciAuMnN9Ci5iYWNrLWJ0bjpob3Zlcntjb2xvcjojZmZmZmZmfQoubG9nby1yantmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZjtmb250LXNpemU6Mi41cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmfQoubG9nby1zdWJ7Zm9udC1zaXplOjAuNjYzcmVtO2ZvbnQtd2VpZ2h0OjYwMDtsZXR0ZXItc3BhY2luZzouNGVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO21hcmdpbi10b3A6M3B4O3BhZGRpbmctYm90dG9tOjE0cHg7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4yKTttYXJnaW4tYm90dG9tOjIwcHh9Ci5ob21lLXJqe2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmO2ZvbnQtc2l6ZTozLjI1cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjF9Ci5sb2dvLXRhZ3tmb250LXNpemU6MC43NXJlbTtsZXR0ZXItc3BhY2luZzouMTJlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjtsaW5lLWhlaWdodDoxLjV9Ci5sb2dvLXJvd3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6ZmxleC1lbmQ7Z2FwOjEycHg7bWFyZ2luLWJvdHRvbToyOHB4O3BhZGRpbmctYm90dG9tOjE4cHg7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4yKX0KLmxvZ28taW1nLXJvd3ttYXJnaW4tYm90dG9tOjI4cHg7cGFkZGluZy1ib3R0b206MThweDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjIpfQoubG9nby1pbWd7aGVpZ2h0OjkwcHg7d2lkdGg6YXV0bztkaXNwbGF5OmJsb2NrfQouaG9tZS1nc3Vie2ZvbnQtc2l6ZTowLjY2M3JlbTtmb250LXdlaWdodDo2MDA7bGV0dGVyLXNwYWNpbmc6LjRlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjttYXJnaW4tdG9wOjZweDttYXJnaW4tYm90dG9tOjIycHh9Ci5ob21lLWgxe2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6My4xMjVyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNmZmZmZmY7bGluZS1oZWlnaHQ6MS4xO21hcmdpbi1ib3R0b206NnB4fQouaG9tZS1oMSBlbXtmb250LXN0eWxlOml0YWxpYztjb2xvcjojZmZmZmZmfQouaG9tZS1zdWJ7Zm9udC1zaXplOjAuOHJlbTtsZXR0ZXItc3BhY2luZzouMThlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjI4cHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5vcHR7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTZweDtwYWRkaW5nOjE2cHggMDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNyk7dGV4dC1kZWNvcmF0aW9uOm5vbmU7Y29sb3I6I2ZmZmZmZjt0cmFuc2l0aW9uOmNvbG9yIC4ycztjdXJzb3I6cG9pbnRlcjtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyLXRvcDpub25lO2JvcmRlci1sZWZ0Om5vbmU7Ym9yZGVyLXJpZ2h0Om5vbmU7d2lkdGg6MTAwJTtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWZ9Ci5vcHQ6aG92ZXJ7Y29sb3I6I2ZmZn0KLm9wdC1pY29ue3dpZHRoOjM4cHg7aGVpZ2h0OjM4cHg7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2ZsZXgtc2hyaW5rOjB9Ci5vcHQtaWNvbi1pbWd7d2lkdGg6MzhweDtoZWlnaHQ6MzhweDtvYmplY3QtZml0OmNvbnRhaW59Ci5vcHQtdGV4dHtmbGV4OjE7dGV4dC1hbGlnbjpsZWZ0fQoub3B0LXRpdGxle2ZvbnQtc2l6ZToxLjUxMnJlbTtmb250LXdlaWdodDo2MDA7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjJweDt0cmFuc2l0aW9uOmNvbG9yIC4ycztmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWZ9Ci5vcHQ6aG92ZXIgLm9wdC10aXRsZXtjb2xvcjojZmZmfQoub3B0LWhhbmRsZXtmb250LXNpemU6MC44ODdyZW07Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXdlaWdodDozMDB9Ci5vcHQtdGl0bGUtYm9va3tmb250LXNpemU6MS4zOHJlbTt3aGl0ZS1zcGFjZTpub3dyYXB9Ci5vcHQtaGFuZGxlLWJvb2t7Zm9udC1zaXplOjAuNzhyZW19Ci5vcHQtYXJyb3d7Y29sb3I6I2ZmZmZmZjtmb250LXNpemU6MS4yMjVyZW07ZmxleC1zaHJpbms6MDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLm9wdDpob3ZlciAub3B0LWFycm93e2NvbG9yOiNmZmZmZmZ9Ci5kaXZpZGVye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7cGFkZGluZzoxMnB4IDB9Ci5kaXZpZGVyOjpiZWZvcmUsLmRpdmlkZXI6OmFmdGVye2NvbnRlbnQ6Jyc7ZmxleDoxO2hlaWdodDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNil9Ci5kaXZpZGVyIHNwYW57Zm9udC1zaXplOjAuNjg4cmVtO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQouaG9tZS1mb290e21hcmdpbi10b3A6MzZweDtwYWRkaW5nLXRvcDoyMHB4O2JvcmRlci10b3A6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA2KTtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6Y2VudGVyfQouaG9tZS1mb290IHNwYW57Zm9udC1zaXplOjAuNzc1cmVtO2xldHRlci1zcGFjaW5nOi4yZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5mZG90e3dpZHRoOjJweDtoZWlnaHQ6MnB4O2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMTYpfQoucHJvZ3Jlc3N7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjttYXJnaW4tYm90dG9tOjQwcHg7b3ZlcmZsb3c6aGlkZGVuO2NvdW50ZXItcmVzZXQ6c3RlcH0KLnBze2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjVweDtmb250LXNpemU6MC42NjNyZW07bGV0dGVyLXNwYWNpbmc6LjEyZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7d2hpdGUtc3BhY2U6bm93cmFwO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2NvdW50ZXItaW5jcmVtZW50OnN0ZXB9Ci5wcy5kb25le2NvbG9yOiNmZmZmZmZ9Ci5wcy5hY3RpdmV7Y29sb3I6I2ZmZmZmZn0KLnBkb3R7d2lkdGg6MThweDtoZWlnaHQ6MThweDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7ZmxleC1zaHJpbms6MDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEyKTtmb250LXNpemU6MC42NjNyZW07Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC13ZWlnaHQ6NjAwfQoucGRvdDo6YmVmb3Jle2NvbnRlbnQ6Y291bnRlcihzdGVwLGRlY2ltYWwtbGVhZGluZy16ZXJvKX0KLnBzLmRvbmUgLnBkb3R7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLnBzLmFjdGl2ZSAucGRvdHtib3JkZXItY29sb3I6I2ZmZmZmZjtjb2xvcjojZmZmZmZmfQoucGx7ZmxleDoxO2hlaWdodDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNyk7bWFyZ2luOjAgNXB4O21pbi13aWR0aDo2cHh9Ci5wbC5kb25le2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMTgpfQouc3RlcHtkaXNwbGF5Om5vbmV9LnN0ZXAuc2hvd3tkaXNwbGF5OmJsb2NrO2FuaW1hdGlvbjpmdSAuMzVzIGVhc2UgYm90aH0KLnNsYmx7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjkzOHJlbTtmb250LXdlaWdodDo2MDA7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjIwcHg7bGV0dGVyLXNwYWNpbmc6LjAxZW19Ci5zYm94e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEwcHg7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTYpO3BhZGRpbmc6MCAycHg7dHJhbnNpdGlvbjpib3JkZXItY29sb3IgLjJzfQouc2JveDpmb2N1cy13aXRoaW57Ym9yZGVyLWJvdHRvbS1jb2xvcjojZmZmZmZmfQouc2l7b3BhY2l0eTouMjtmb250LXNpemU6MS4yMjVyZW07ZmxleC1zaHJpbms6MH0KI2JJbnB1dHtmbGV4OjE7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtvdXRsaW5lOm5vbmU7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjUxMnJlbTtjb2xvcjojZmZmZmZmO3BhZGRpbmc6MTJweCAwfQojYklucHV0OjpwbGFjZWhvbGRlcntjb2xvcjojZmZmZmZmfQouY2xye2JhY2tncm91bmQ6bm9uZTtib3JkZXI6bm9uZTtjb2xvcjojZmZmZmZmO2N1cnNvcjpwb2ludGVyO2ZvbnQtc2l6ZToxLjE1cmVtO2Rpc3BsYXk6bm9uZTtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZn0KLmNsci5zaG93e2Rpc3BsYXk6YmxvY2t9Ci5id3JhcHtwb3NpdGlvbjpyZWxhdGl2ZTttYXJnaW4tYm90dG9tOjIwcHh9Ci5kcm9we3Bvc2l0aW9uOmFic29sdXRlO2xlZnQ6MDtyaWdodDowO2JhY2tncm91bmQ6IzBmMGYwZjtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEpO2JvcmRlci10b3A6bm9uZTttYXgtaGVpZ2h0OjIwMHB4O292ZXJmbG93LXk6YXV0bzt6LWluZGV4OjUwO2Rpc3BsYXk6bm9uZX0KLmRyb3Aub3BlbntkaXNwbGF5OmJsb2NrfQouZGl0ZW17cGFkZGluZzoxMXB4IDE0cHg7Zm9udC1zaXplOjEuMzYzcmVtO2NvbG9yOiNmZmZmZmY7Y3Vyc29yOnBvaW50ZXI7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDUpO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLmRpdGVtOmhvdmVye2NvbG9yOiNmZmZ9Ci5kaXRlbSBtYXJre2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Y29sb3I6I2ZmZjtmb250LXdlaWdodDo3MDB9Ci5ub3Jlc3twYWRkaW5nOjE0cHg7Zm9udC1zaXplOjEuMjg4cmVtO2NvbG9yOiNmZmZmZmY7Zm9udC1zdHlsZTppdGFsaWM7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmfQoubm8tc2xvdHMtcGFuZWx7dGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzozMnB4IDIycHggMjZweDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA4KTtib3JkZXItcmFkaXVzOjE2cHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wMik7bWFyZ2luLXRvcDo0cHh9Ci5uby1zbG90cy1pY29ue2ZvbnQtc2l6ZToxLjhyZW07b3BhY2l0eTouMzI7bWFyZ2luLWJvdHRvbToxMnB4fQoubm8tc2xvdHMtdGl0bGV7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc3R5bGU6aXRhbGljO2ZvbnQtc2l6ZToxLjJyZW07Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjIwcHg7bGluZS1oZWlnaHQ6MS4zNX0KLm5vLXNsb3RzLWRpdmlkZXJ7d2lkdGg6MzZweDtoZWlnaHQ6MXB4O2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMTUpO21hcmdpbjowIGF1dG8gMjBweH0KLm5vLXNsb3RzLWN0YXtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOm9wYWNpdHkgLjJzfQoubm8tc2xvdHMtY3RhOmhvdmVye29wYWNpdHk6Ljc1fQoubm8tc2xvdHMtY3RhLXRpdGxle2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6MS4wNXJlbTtjb2xvcjojZmZmZmZmO2ZvbnQtd2VpZ2h0OjYwMDttYXJnaW4tYm90dG9tOjhweH0KLm5vLXNsb3RzLWN0YS1zdWJ7Zm9udC1zaXplOjAuODVyZW07Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuNTUpO2xpbmUtaGVpZ2h0OjEuNTU7bWFyZ2luLWJvdHRvbToxNHB4fQoubm8tc2xvdHMtY3RhLWFycm93e2ZvbnQtc2l6ZTowLjgycmVtO2NvbG9yOiNmZmZmZmY7bGV0dGVyLXNwYWNpbmc6LjA4ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQoubm8tYnJlZWQtYmFubmVye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7cGFkZGluZzoxNHB4IDA7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDYpO2N1cnNvcjpwb2ludGVyO3RyYW5zaXRpb246Y29sb3IgLjJzO21hcmdpbi10b3A6NHB4fQoubm8tYnJlZWQtYmFubmVyOmhvdmVyIC5uby1icmVlZC1iYW5uZXItdGl0bGV7Y29sb3I6I2ZmZmZmZn0KLm5vLWJyZWVkLWJhbm5lci1pY29ue2ZvbnQtc2l6ZToxLjU3NXJlbTtmbGV4LXNocmluazowO29wYWNpdHk6LjN9Ci5uby1icmVlZC1iYW5uZXItdGV4dHtmbGV4OjF9Ci5uby1icmVlZC1iYW5uZXItdGl0bGV7Zm9udC1zaXplOjEuNDM4cmVtO2NvbG9yOiNmZmZmZmY7Zm9udC13ZWlnaHQ6NjAwO21hcmdpbi1ib3R0b206MnB4O2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLm5vLWJyZWVkLWJhbm5lci1zdWJ7Zm9udC1zaXplOjAuODg3cmVtO2NvbG9yOiNmZmZmZmY7bGluZS1oZWlnaHQ6MS41O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQoubm8tYnJlZWQtYmFubmVyLWFycm93e2NvbG9yOiNmZmZmZmY7Zm9udC1zaXplOjEuMjI1cmVtO2ZsZXgtc2hyaW5rOjA7dHJhbnNpdGlvbjpjb2xvciAuMnN9Ci5zYmFkZ2V7ZGlzcGxheTpub25lO2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTJweDttYXJnaW4tYm90dG9tOjIwcHh9Ci5zYmFkZ2Uuc2hvd3tkaXNwbGF5OmZsZXh9Ci5ibmFtZXtib3JkZXItYm90dG9tOjFweCBzb2xpZCAjZmZmZmZmO2NvbG9yOiNmZmZmZmY7cGFkZGluZzoycHggMDtmb250LXNpemU6MS40MzhyZW07Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmfQouYmNoZ3tmb250LXNpemU6MC44cmVtO2NvbG9yOiNmZmZmZmY7Y3Vyc29yOnBvaW50ZXI7bGV0dGVyLXNwYWNpbmc6LjEyZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO3RyYW5zaXRpb246Y29sb3IgLjJzfQouYmNoZzpob3Zlcntjb2xvcjojZmZmZmZmfQouc3ZidG57ZGlzcGxheTpibG9jaztwYWRkaW5nOjA7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wOCk7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Y3Vyc29yOnBvaW50ZXI7dGV4dC1hbGlnbjpsZWZ0O3RyYW5zaXRpb246Ym9yZGVyLWNvbG9yIC4yczt3aWR0aDoxMDAlO292ZXJmbG93OmhpZGRlbjtwb3NpdGlvbjpyZWxhdGl2ZX0KLnN2YnRuOmhvdmVye2JvcmRlci1ib3R0b20tY29sb3I6I2ZmZmZmZn0KLnN2YnRuLmFjdGl2ZXtib3JkZXItYm90dG9tLWNvbG9yOiNmZmZmZmZ9Ci5zdnB7Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNmZmZmZmY7ZmxleC1zaHJpbms6MH0KLm1hc3RlcnN7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyO2dhcDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNyl9Ci5tYnRue2JhY2tncm91bmQ6IzBhMGEwYTtwYWRkaW5nOjIycHggMTJweDt0ZXh0LWFsaWduOmNlbnRlcjtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmJhY2tncm91bmQgLjJzO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtib3JkZXI6bm9uZX0KLm1idG4uYWN0aXZle2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDYpfQoubW5hbWV7Zm9udC1zaXplOjEuMTVyZW07Zm9udC13ZWlnaHQ6NTAwO2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjg1KTtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7dHJhbnNpdGlvbjpjb2xvciAuMTVzfQoubWJ0bi5hY3RpdmUgLm1uYW1le2NvbG9yOiNmZmZmZmY7Zm9udC13ZWlnaHQ6NjAwfQoubXRpdGxle2ZvbnQtc2l6ZTowLjhyZW07Y29sb3I6I2ZmZmZmZjttYXJnaW4tdG9wOjNweDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZn0KLmdidG57ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtwYWRkaW5nOjE0cHggMDtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjpub25lO2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA4KTtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6MS40MzhyZW07Y3Vyc29yOnBvaW50ZXI7d2lkdGg6MTAwJTt0cmFuc2l0aW9uOmFsbCAuMnN9Ci5nYnRuOmhvdmVye2NvbG9yOiNmZmZmZmZ9Ci5nYnRuLmFjdGl2ZXtjb2xvcjojZmZmZmZmO2JvcmRlci1ib3R0b20tY29sb3I6I2ZmZmZmZn0KLmNhbC1oe2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpjZW50ZXI7bWFyZ2luLWJvdHRvbToxNnB4fQouY2FsLW17Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjkzOHJlbTtmb250LXdlaWdodDo2MDA7Y29sb3I6I2ZmZmZmZn0KLmNhbC1ue2JhY2tncm91bmQ6bm9uZTtib3JkZXI6bm9uZTtjb2xvcjojZmZmZmZmO2N1cnNvcjpwb2ludGVyO2ZvbnQtc2l6ZToxLjU3NXJlbTtwYWRkaW5nOjRweCA4cHg7dHJhbnNpdGlvbjpjb2xvciAuMnN9Ci5jYWwtbjpob3Zlcntjb2xvcjojZmZmZmZmfQouY2d7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoNywxZnIpO2dhcDoycHg7bWFyZ2luLWJvdHRvbToxMnB4fQouY2FsLXdyYXB7cG9zaXRpb246cmVsYXRpdmV9Ci5jYWwtbG9hZGluZ3twb3NpdGlvbjphYnNvbHV0ZTt0b3A6MDtsZWZ0OjA7cmlnaHQ6MDtib3R0b206MDtkaXNwbGF5Om5vbmU7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2dhcDoxMnB4O2JhY2tncm91bmQ6cmdiYSgxMCwxMCw5LC44NSk7ei1pbmRleDo1fQouY2FsLWxvYWRpbmcuc2hvd3tkaXNwbGF5OmZsZXh9Ci5jYWwtc3Bpbm5lcnt3aWR0aDoyOHB4O2hlaWdodDoyOHB4O2JvcmRlcjoycHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTUpO2JvcmRlci10b3AtY29sb3I6I2ZmZmZmZjtib3JkZXItcmFkaXVzOjUwJTthbmltYXRpb246Y2Fsc3BpbiAuN3MgbGluZWFyIGluZmluaXRlfQpAa2V5ZnJhbWVzIGNhbHNwaW57dG97dHJhbnNmb3JtOnJvdGF0ZSgzNjBkZWcpfX0KLmNhbC1sb2FkaW5nLXRleHR7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuNyk7Zm9udC1zaXplOjAuODVyZW07bGV0dGVyLXNwYWNpbmc6LjA1ZW07Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5jZG57dGV4dC1hbGlnbjpjZW50ZXI7Zm9udC1zaXplOjAuNjYzcmVtO2NvbG9yOiNmZmZmZmY7cGFkZGluZzo0cHggMDt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7bGV0dGVyLXNwYWNpbmc6LjFlbX0KLmNke3RleHQtYWxpZ246Y2VudGVyO2N1cnNvcjpwb2ludGVyO2NvbG9yOiNmZmZmZmY7Ym9yZGVyOjFweCBzb2xpZCB0cmFuc3BhcmVudDt0cmFuc2l0aW9uOmFsbCAuMnN9Ci5jZDpob3Zlcjpub3QoLmRpcyk6bm90KC5wYWQpIC5jZC1pbm5lcntiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA3KSFpbXBvcnRhbnQ7Y29sb3I6I2ZmZmZmZiFpbXBvcnRhbnR9Ci5jZC5zZWwgLmNkLWlubmVye2JhY2tncm91bmQ6I2ZmZmZmZiFpbXBvcnRhbnQ7Y29sb3I6IzBhMGEwYSFpbXBvcnRhbnQ7Zm9udC13ZWlnaHQ6NzAwIWltcG9ydGFudDtib3JkZXI6bm9uZSFpbXBvcnRhbnR9Ci5jZC50b2QgLmNkLWlubmVye2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMjgpO2NvbG9yOiNmZmZ9Ci5jZC5kaXN7Y29sb3I6I2ZmZmZmZjtjdXJzb3I6ZGVmYXVsdH0KLnRne2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDQsMWZyKTtnYXA6MXB4O2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDcpfQoudGJ0bntiYWNrZ3JvdW5kOiMwYTBhMGE7Ym9yZGVyOm5vbmU7cGFkZGluZzoxM3B4IDRweDt0ZXh0LWFsaWduOmNlbnRlcjtmb250LXNpemU6MS4zMjVyZW07Y29sb3I6I2ZmZmZmZjtjdXJzb3I6cG9pbnRlcjtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7dHJhbnNpdGlvbjphbGwgLjJzfQoudGJ0bjpob3Zlcntjb2xvcjojZmZmZmZmO2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDQpfQoudGJ0bi5hY3RpdmV7Y29sb3I6I2ZmZmZmZn0KLnN1bXtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjpub25lO2JvcmRlci10b3A6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA4KTtwYWRkaW5nOjIwcHggMDttYXJnaW4tYm90dG9tOjIwcHh9Ci5zcntkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47cGFkZGluZzo4cHggMDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNSk7Zm9udC1zaXplOjEuMzYzcmVtO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZn0KLnNyOmxhc3QtY2hpbGR7Ym9yZGVyLWJvdHRvbTpub25lO3BhZGRpbmctdG9wOjE0cHh9Ci5zbHtjb2xvcjojZmZmZmZmfS5zdntjb2xvcjojZmZmZmZmO3RleHQtYWxpZ246cmlnaHR9Ci5zcHtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjIuNDM4cmVtO2NvbG9yOiNmZmZmZmY7Zm9udC13ZWlnaHQ6NjAwfQouZmd7bWFyZ2luLWJvdHRvbToyMHB4fQouZmx7Zm9udC1zaXplOjAuNzEycmVtO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO21hcmdpbi1ib3R0b206OHB4O2Rpc3BsYXk6YmxvY2s7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5maXt3aWR0aDoxMDAlO2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOm5vbmU7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTQpO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjUxMnJlbTtwYWRkaW5nOjEwcHggMDtvdXRsaW5lOm5vbmU7dHJhbnNpdGlvbjpib3JkZXItY29sb3IgLjJzfQouZmk6Zm9jdXN7Ym9yZGVyLWJvdHRvbS1jb2xvcjojZmZmZmZmfQouY2J0bntkaXNwbGF5OmJsb2NrO3dpZHRoOjEwMCU7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZTowLjg2MnJlbTtmb250LXdlaWdodDo2MDA7bGV0dGVyLXNwYWNpbmc6LjI4ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO3RleHQtYWxpZ246Y2VudGVyO3BhZGRpbmc6MTZweDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjI1KTtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmFsbCAuMnN9Ci5jYnRuOmhvdmVye2JvcmRlci1jb2xvcjojZmZmZmZmO2NvbG9yOiNmZmZmZmZ9Ci5zYmxvY2t7dGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzo1MnB4IDIwcHg7ZGlzcGxheTpub25lfQouc2Jsb2NrLnNob3d7ZGlzcGxheTpibG9jazthbmltYXRpb246ZnUgLjVzIGVhc2UgYm90aH0KLnNpMntmb250LXNpemU6My42cmVtO21hcmdpbi1ib3R0b206MjBweDtvcGFjaXR5Oi40fQouc3R7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToyLjcyNXJlbTtjb2xvcjojZmZmZmZmO21hcmdpbi1ib3R0b206MTBweDtmb250LXdlaWdodDo2MDB9Ci5zc3tmb250LXNpemU6MS4wNzVyZW07Y29sb3I6I2ZmZmZmZjtsaW5lLWhlaWdodDoxLjk7bWFyZ2luLWJvdHRvbToyOHB4O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQouaGJ0bntiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTYpO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC1zaXplOjAuODYycmVtO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtwYWRkaW5nOjEzcHggMjhweDtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmFsbCAuMnN9Ci5oYnRuOmhvdmVye2JvcmRlci1jb2xvcjojZmZmZmZmO2NvbG9yOiNmZmZmZmZ9Ci5sb2FkaW5nLXNsb3Rze2NvbG9yOiNmZmZmZmY7Zm9udC1zaXplOjEuMjg4cmVtO3BhZGRpbmc6MTJweCAwO3RleHQtYWxpZ246Y2VudGVyO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXN0eWxlOml0YWxpY30KLmNke2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2FsaWduLWl0ZW1zOmNlbnRlcjtoZWlnaHQ6MzZweCFpbXBvcnRhbnQ7cGFkZGluZzowIWltcG9ydGFudH0KLmNkLWlubmVye3dpZHRoOjMycHg7aGVpZ2h0OjMycHg7Ym9yZGVyLXJhZGl1czowO2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OmNlbnRlcjtmb250LXNpemU6MS4xNXJlbTtjdXJzb3I6cG9pbnRlcjtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWZ9Ci5jZC5hdmFpbCAuY2QtaW5uZXJ7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDkwLDE4MCw5MCwuMzUpO2NvbG9yOnJnYmEoOTAsMTgwLDkwLC42NSl9Ci5jZC5idXN5IC5jZC1pbm5lcntib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA3KTtjb2xvcjpyZ2JhKDI1NSwyNTUsMjU1LC4zMik7dGV4dC1kZWNvcmF0aW9uOmxpbmUtdGhyb3VnaDt0ZXh0LWRlY29yYXRpb24tY29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuMzIpO3RleHQtZGVjb3JhdGlvbi10aGlja25lc3M6MXB4fQouY2Quc2VsIC5jZC1pbm5lcntiYWNrZ3JvdW5kOiNmZmZmZmYhaW1wb3J0YW50O2NvbG9yOiMwYTBhMGEhaW1wb3J0YW50O2ZvbnQtd2VpZ2h0OjcwMCFpbXBvcnRhbnQ7Ym9yZGVyOm5vbmUhaW1wb3J0YW50O3RleHQtZGVjb3JhdGlvbjpub25lIWltcG9ydGFudH0KLmNkLnRvZCAuY2QtaW5uZXJ7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4yOCk7Y29sb3I6I2ZmZjtmb250LXdlaWdodDo2MDB9Ci5jZC5kaXMgLmNkLWlubmVye2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjIyKTtjdXJzb3I6ZGVmYXVsdDtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyOm5vbmU7dGV4dC1kZWNvcmF0aW9uOmxpbmUtdGhyb3VnaDt0ZXh0LWRlY29yYXRpb24tY29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuMjIpO3RleHQtZGVjb3JhdGlvbi10aGlja25lc3M6MXB4fQouc3ZidG4tcm93e2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpiYXNlbGluZTttYXJnaW4tYm90dG9tOjZweDtwYWRkaW5nOjE2cHggMCAwfQouc3ZidG4tbmFtZXtmb250LXNpemU6MS41MTJyZW07Y29sb3I6I2ZmZmZmZjtmb250LXdlaWdodDo2MDA7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmfQouc3ZidG4uYWN0aXZlIC5zdmJ0bi1uYW1le2NvbG9yOiNmZmZmZmZ9Ci5zdmJ0bi1wcmljZXtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjEuNzI1cmVtO2NvbG9yOiNmZmZmZmY7Zm9udC13ZWlnaHQ6NjAwO2ZsZXgtc2hyaW5rOjB9Ci5zdmJ0bi5hY3RpdmUgLnN2YnRuLXByaWNle2NvbG9yOiNmZmZmZmZ9Ci5zdmJ0bi1kZXNje2ZvbnQtc2l6ZToxcmVtO2NvbG9yOiNmZmZmZmY7bGluZS1oZWlnaHQ6MS43O2Rpc3BsYXk6YmxvY2s7cGFkZGluZzowIDAgMTRweDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjt3aGl0ZS1zcGFjZTpwcmUtbGluZX0KLnN2YnRuLmFjdGl2ZSAuc3ZidG4tZGVzY3tjb2xvcjojZmZmZmZmfQouc3ZidG4tdGFne2ZvbnQtc2l6ZTowLjk3NXJlbTtjb2xvcjojZmZmZmZmO2ZvbnQtc3R5bGU6aXRhbGljO2Rpc3BsYXk6YmxvY2s7bWFyZ2luLXRvcDoycHg7cGFkZGluZzowIDAgMTRweDtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWZ9Ci5zdmJ0bi5hY3RpdmUgLnN2YnRuLXRhZ3tjb2xvcjojZmZmZmZmfQpAbWVkaWEobWF4LXdpZHRoOjQwMHB4KXsuc3ZidG4tbmFtZXtmb250LXNpemU6MS4zNjNyZW19LnN2YnRuLXByaWNle2ZvbnQtc2l6ZToxLjUxMnJlbX0uc3ZidG4tZGVzY3tmb250LXNpemU6MC45MzhyZW19LnN2YnRuLXRhZ3tmb250LXNpemU6MC44ODdyZW19fQpAa2V5ZnJhbWVzIGZ1e2Zyb217b3BhY2l0eTowO3RyYW5zZm9ybTp0cmFuc2xhdGVZKDEwcHgpfXRve29wYWNpdHk6MTt0cmFuc2Zvcm06dHJhbnNsYXRlWSgwKX19Ci5sYW5nLWJhcntwb3NpdGlvbjpmaXhlZDt0b3A6MTJweDtyaWdodDoxNHB4O3otaW5kZXg6OTk5O2Rpc3BsYXk6ZmxleDtnYXA6NnB4fQoubGFuZy1idG57YmFja2dyb3VuZDpyZ2JhKDEwLDEwLDEwLC45Mik7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xKTtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZTowLjc3NXJlbTtsZXR0ZXItc3BhY2luZzouMTVlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7cGFkZGluZzo1cHggMTBweDtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmFsbCAuMnN9Ci5sYW5nLWJ0bjpob3Zlcntib3JkZXItY29sb3I6I2ZmZmZmZjtjb2xvcjojZmZmZmZmfQoubGFuZy1idG4uYWN0aXZle2JvcmRlci1jb2xvcjojZmZmZmZmO2NvbG9yOiNmZmZmZmZ9Ci5jYmstYnRue2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xNCk7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXNpemU6MC44NjJyZW07bGV0dGVyLXNwYWNpbmc6LjE2ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO3BhZGRpbmc6MTJweCAyMHB4O2N1cnNvcjpwb2ludGVyO3RyYW5zaXRpb246YWxsIC4yczt3aWR0aDoxMDAlfQouY2JrLWJ0bjpob3Zlcntib3JkZXItY29sb3I6I2ZmZmZmZjtjb2xvcjojZmZmZmZmfQoubWJ0biwuc3ZidG4sLmdidG4sLnRidG4sLmNidG4sLmhidG4sLmNiay1idG4sLmxhbmctYnRuLC5iYWNrLWJ0biwub3B0LC5kaXRlbSwuY2QsLm5vLWJyZWVkLWJhbm5lciwuYmNoZ3t0cmFuc2l0aW9uOmFsbCAuMTVzIGVhc2V9Ci5tYnRuOmFjdGl2ZSwuc3ZidG46YWN0aXZlLC5nYnRuOmFjdGl2ZSwudGJ0bjphY3RpdmUsLmNidG46YWN0aXZlLC5oYnRuOmFjdGl2ZSwuY2JrLWJ0bjphY3RpdmUsLmxhbmctYnRuOmFjdGl2ZSwuYmFjay1idG46YWN0aXZlLC5vcHQ6YWN0aXZlLC5kaXRlbTphY3RpdmUsLmNkOmFjdGl2ZSwubm8tYnJlZWQtYmFubmVyOmFjdGl2ZSwuYmNoZzphY3RpdmV7dHJhbnNmb3JtOnNjYWxlKDAuOTYpfQo8L3N0eWxlPgo8L2hlYWQ+Cjxib2R5Pgo8YSBocmVmPSIvYWRtaW4/cGFzcz1hbnphMTk4NSIgaWQ9ImFkbWluQmFja0xpbmsiIHN0eWxlPSJkaXNwbGF5Om5vbmU7cG9zaXRpb246Zml4ZWQ7dG9wOjE0cHg7cmlnaHQ6MTRweDtmb250LXNpemU6MC45cmVtO2NvbG9yOiNjOWEwNWE7dGV4dC1kZWNvcmF0aW9uOm5vbmU7ei1pbmRleDo5OTk7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7YmFja2dyb3VuZDpyZ2JhKDEwLDEwLDksLjg1KTtwYWRkaW5nOjZweCAxMnB4O2JvcmRlci1yYWRpdXM6MjBweDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjAxLDE2MCw5MCwuMzUpIj7ihpAg0JDQtNC80LjQvS3Qv9Cw0L3QtdC70Yw8L2E+CjxzY3JpcHQ+aWYobG9jYXRpb24uc2VhcmNoLmluZGV4T2YoJ3Bhc3M9YW56YTE5ODUnKSE9PS0xKXtkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnYWRtaW5CYWNrTGluaycpLnN0eWxlLmRpc3BsYXk9J2Jsb2NrJzt9PC9zY3JpcHQ+CjxkaXYgY2xhc3M9ImxhbmctYmFyIj4KICA8YnV0dG9uIGNsYXNzPSJsYW5nLWJ0biIgb25jbGljaz0ic2V0TGFuZygnZXQnKSI+RVQ8L2J1dHRvbj4KICA8YnV0dG9uIGNsYXNzPSJsYW5nLWJ0biIgb25jbGljaz0ic2V0TGFuZygnZW4nKSI+RU48L2J1dHRvbj4KICA8YnV0dG9uIGNsYXNzPSJsYW5nLWJ0biBhY3RpdmUiIG9uY2xpY2s9InNldExhbmcoJ3J1JykiPlJVPC9idXR0b24+CjwvZGl2PgoKPCEtLSBIT01FIC0tPgo8ZGl2IGNsYXNzPSJzY3JlZW4gYWN0aXZlIiBpZD0iaG9tZVNjcmVlbiI+CjxkaXYgY2xhc3M9ImNvbiI+CiAgPGRpdiBjbGFzcz0ibG9nby1pbWctcm93Ij4KICAgIDxpbWcgc3JjPSJkYXRhOmltYWdlL3BuZztiYXNlNjQsaVZCT1J3MEtHZ29BQUFBTlNVaEVVZ0FBQVVNQUFBRHJDQVlBQUFEekMvUXdBQUFCV0dsRFExQkpRME1nVUhKdlptbHNaUUFBZUp4OWtMRkx3MUFReHI5V3BhQjFFQjBjSERLSlE1U1NDcm80dEJWRWNRaFZ3ZXFVdnFhcGtNWkhraUlGTi8rQmd2K0JDczV1Rm9jNk9qZ0lvcFBvNXVTazRLTGxlUytKcENKNmorTitmTys3NHpnZ09XNXdidmNEcUR1K1cxektLNXVsTFNYMWpBUzlJQXptOFp5dXIwcityai9qL1Q3MDNrN0xXYi8vLzQzQml1a3hxcCtVR2NaZEgwaW94UHFlenlYdkU0KzV0QlJ4UzdJVjhvbmtjc2puZ1dlOVdDQytKbFpZemFnUXZ4Q3I1UjdkNnVHNjNXRFJEbkw3dE9sc3JNazVsQk5ZeEE0OGNOZ3cwSVFDSGRrLy9MT0J2NEJkY2pmaFVwK0ZHbnpxeVpFaUo1akV5M0RBTUFPVldFT0dVcE4zanU1M0Y5MVBqYldESjJDaEk0UzRpTFdWRG5BMlJ5ZHJ4OXJVUERBeUJGeTF1ZUVhZ2RSSG1heFdnZGRUWUxnRWpONVF6N1pYeldyaDl1azhNUEFveE5za2tEb0V1aTBoUG82RTZCNVQ4d053Nlh3QkE2ZGlFOEhZV2hNQUFFSHdTVVJCVkhpYzdaMTVmRlZGc3ZqcjNEWDdCb1FsUUFpYktBSStVRkJ4WDFBWndIRjVJaUJQSFJjZURpN29xUGhUUmxGQVFjVlJVWjhQVVhIVUorTG80SzZBQXM2NG9PREdJaERDa29SQTl2VnVaNm5mSDFoTm43N25KamNRSUlINmZqNzUzQ1huZHZmcGM3cE9WVmQxTlFERE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1FemJRRHZTRFRoYWNMbGNnSWlnYVJwWWxnVnV0eHRNMHdSTk83Z3VSc1NvZWdBQUxNc0NBQkRsMDNHYXBvazJNQzJQeCtNQnd6QU8rcnJLSUNLNFhDNXh6ZVQzRE5ObUlPRWtvMmthZUR5ZUZpbmY0L0dBMisyT1dVOHNXcklOeHpvdWwwdjB2OXZ0QnJmYjNhTGxhNW9HYnJjYmZENmYrSzZsNjJDYWhqWERGc0RuODBFa0VyRjlSeHFhcXRrMUYvbjNUbHFnL0gvV0tBNGRicmNiRUZIOGtTWFFFcWpYMCt2MWdxN3JMVkkyd3h4V1ZBM01TWXM3VUh3K24wMERiRW9iZExsY0xXckNIZXQ0UEI3Um4zSy90cVRXclY0ditzemFJZE9ta00wbnd1djFDc0hVa3FqQ2psNWxNMDcrekVLeFpaQ3ZyY3ZsYW5FaEpaZEhwakpQY1RCdEVsVVF0alFrQkZYaEdxL0d5Qnc0ZEQxcDJnTmcvL1Z1aVlkZHJMTDRlakp0RHZXbTlmdjlBR0Ezcnc2MmZMVWNXZXVUSjkzcGY3SGF4aHdZcWlZWXk2RjFJTGhjTHFFRlVqMnlBR1lPSDl6YkxZRFA1NE9ISG5vSU8zZnVESkZJcEVYTktEbGN4ekFNMEhVZDZ1cnFvS1NrQlBidTNRdWJObTNTU2twS29MS3lFZ0RzVHBTV2NPQWM2NXgrK3VrNGFkSWtBQUNJUkNMZzlYb2hIQTZEMiswR3k3SU9XaWdhaGdFVkZSWHc4TU1QYStGd1dIelB6akNtVFpLWW1BanIxNi9IY0RpTWhtRWdJcUt1NjJoWkZwcW1pWWdvWHVuL2hLN3JpSWhvV1JaYWxtWDdIMzJtVi9tMzh2dElKSUtHWWVEYXRXdnhqanZ1d0x5OFBFaE5UWTNTRXVrenZSNE96Y09wTG5wWTBLdVQ5aXNqUDF6bzJFTXhKK3ZFdUhIalJEK3IxMUtGcm9sNmplVnJLRjl6K243OSt2V1lrcElpTk1URGNWNU1OS3dadGdBK253K21UcDJLYVdscG9Ha2FKQ1VsUWJ0MjdXRHc0TUZ3d2drbmdHVlp0bUJzWGRmQjYvVUtyWTgrazZaQm1vZkg0eEhmbWFZcGhFSWtFZ0dmejJmVFRPVDNobUhBK3ZYcjRhV1hYb0lWSzFab0JRVUZJdlJIRHMraHdQQkREWVdLT0dtcTlKMnNDZEY1YTVvbTJxZUdGY20vUFpTY2NjWVpPSEhpUlBCNFBPRHorU0EzTnhjR0R4NE1DUWtKNGhqcVMxV2dxeUV6ZEsyLy9mWmJLQ2dvQUYzWElTRWhBWXFLaXVEaGh4L1dnc0hnWVQwM3hnNEx3NE9FQkFxWlRYUURlendlT082NDQrQ2lpeTdDSjU5ODBpYk1BRUFJUUhxdHJhMkYxTlJVMkxsekoyemN1QkhXclZzSFJVVkZVRjFkTGNwTVNrcUNybDI3d3Jubm5ndG5ubm1temVPb2FacE5pQWFEUWZENy9mRHp6ei9EMHFWTFlmYnMyWnF1NjdiNHVNTXgyTlJCVFNzNDVMNmo3MDNUakdzMUJzMlpIZzVCVG0wRDJQZVF5Y3ZMZytPUFB4NW56SmdCSjU5OHNqaUdoSjBUZE8zMzdOa0ROOXh3QTJ6WnNrVXJMQ3lFU0NRQ2lHaUxVNlYrb1BObWdjaTBLV0taTmJSNjVQYmJiMGZUTkRFY0R0dE1MTmtNcnF5c3hCNDllb0RQNTRQRXhFUUEyQ2RJYURKZERybHd1OTJRa1pFQk0yYk1RRVRFUUNCZ0s5TTBUWnZaSFFnRWNOMjZkYWlhem9jampvMEVpV3pXcW1FajhtY25CNUM2K2tNMTl3OGxjaDJ5bzhQdjk4T0xMNzZJb1ZESVpnN0wweDZ5U1YxV1ZvYmR1bldEcEtRa1VhN3NDQ1BIRzhPMGFXaVF5c0pGbmc4NzRZUVRZTjI2ZGJaQkl3dEYwelN4ckt3TTI3VnJKMzVEWlhpOVh0dThHdzBnZXMzT3pvYTMzMzRicTZ1cjBiSXNESWZEVWVWYmxvV0dZZURubjMrT2ZmdjJCWUFqTS9qY2JqZWtwNmZEcWFlZWl1UEdqY1BYWDM4ZHQyN2RpblYxZFVKNFJDSVJMQ3Nyd3c4Ly9CQ3Z2dnBxSER4NE1BTFlQZWlxa0R4Y2JWZm5PdnYxNndmZmZmZWRyYjlwdnBBZVNJaUk0WEFZcjczMjJxanpBSUNvZVVKYW1rZnZHYWJONEJRZlJqZTBIUDd5MFVjZm9XRVlxT3U2R0RnMFdIUmR4K3JxYXV6Y3ViTW9RMzV0ckM2UHh3TnBhV2t3Y2VKRXJLK3ZqeEs0aG1IWUJ1cXlaY3N3S3l2cnNBMDBPVDd5ejMvK003N3h4aHMyNFVkRUlwRW9SNUZsV2Joanh3NTgvUEhITVRjMzF5WWtXaXAwcVNuazY2ZzZnZHh1Tnp6MTFGTVlEb2VqbkYweXYvNzZLeDUzM0hFMmpkL3BWZFdZZVFVSzB5Wng4b2pLTi9OcnI3MFdwUTJTU1dXYUpsWldWbUtIRGgxRVdUSzBvZ1hBV1VNQzJDZHdUenZ0TkF5SHcxR21tbXkrUlNJUlhMZHUzV0diaUhLNVhOQ3BVeWRZdkhneFZsZFhDMDJKMm9LNHo0VDgrdXV2OGV1dnZ4YkgwUCtvbjlhdVhZc25uM3d5SG9tVk5lb0tGSmxycnJuR1ppcXJubWJMc25ERmloVklnaERBbmtSRGZsandNanptcUVFTm1nWFlQM2ptelp0bkUwcHFtRVpGUlFXMmE5Zk9jYVVEUUxTd2RYcnY5WHBoL1BqeDJORFFZQnVVVkNkcFhNRmdFS2RQbjQ2SHc4ek16czZHeno3N1RBZzNFaENoVUFpLy8vNTdQUFBNTTFFT1BQYjcvZkRJSTQ5Z1RVMk56ZVJFUkN3b0tNQ2NuQnhSOXVFVUdyS0pMRCtZaGc0ZGlyVzF0ZUxjNUxBYWF2ZlNwVXRSbmpOVnpXMkE2Q3hEYkNJelJ4MzA1SjgyYlpwTkdLaFVWRlFJelpCb3pvQ2dnWmFhbWdwTGx5NjF6Vm5Sd0pRLy8vREREMkwrOEdDRmlxeTVFbTYzR3hJU0V1Q2YvL3dubXFacEU0YUlpTTg5OXh3bUppYkdYTW80YWRLa0tMTS9Fb25ncWxXcnNEVTVHL0x5OHFDNnV0b21CTlhydTNqeFlnU3dPNUZZKzJPT09VaWp1L2ZlZTIyRFJSMHdCeW9NeWJzTUFDS3M1dVNUVHhaYWltcTJrVW5YME5DQUV5Wk1zR2tzVkNlWm9zM1ZUbVNocUdrYVRKOCtQYXArMHpSeHlaSWxtSjJkYmZzdGVjdnBYREl5TW1EUm9rVzJOcFBBdWZubW0xdU5RT3pldlR0VVZWWEZETVJHUkZ5MGFKRVFoa2ZDK2NQRUIxK1JRd3orbnYvdVVNV01VZFpsaXRrelRSUFdybDJyRlJZVzJzSlFLRmJQNS9NQklrSlNVaEtNR3pkT0JIdkxRYzEwYkhQYnErdTZpTGRMVDArSE1XUEdpUG9qa1FoWWxnV1daY0VYWDN3QnBhV2x0ckFiaXJPajdPRFYxZFd3WnMwYUNJZkQ0UGY3YmNzY3g0d1owMnJ5L2NubnJFTFh2cUdod2ZhZC9NcTBIbGdZSGdWUUVEUEEvZ0g0d0FNUENBRWpyNENoSUdqRE1PQ2NjODRSR3BucW1Hak9ZRlZqQ0wxZUx3d2JOZ3k3ZCs4dXl2SDVmT0J5dVdEUG5qM3cwMDgvZ2FacFFwQ3JTU2NRRVJJU0V1Q0hIMzZBUFh2MmlITHAzRHAyN0FqWjJkbXRZbDdOTUF6Ync4UUplV1VKd2NLdzljSEM4QWh3SUNab1UyWFJLZ2VYeXdVK253K1dMRm1peWNLRjZxTmxleDZQQjVLVGs0RmlHMVhOdFRsbW5GdytJa0lrRW9IKy9mc0RPWVJJV0NNaTFOWFZRWFYxdFlaU3RtaGFwaWhuQ3crRlFyQjE2MWF0cXFyS1ZvZW1hWkNhbWlyYWZhUnBTcWpSRXJ6V0lMaVp4bUZoMk1ZaG9RS3d6OFNrN0RiQllCQWFHaHFFb0FIWXIvM1JBTFlzQzNKeWNsQk5ZWDhnODFsazN0SVNPWi9QSjB4M2VST2x6TXhNNk55NU0xTDlUckdVVkQ5bGNhRnpDSVZDQUFEUTBOQWdscklkYVp6V1RLdi9rOSt6VUd5OXNEQnM0NmpKRnVUVVVzRmdVQXcrMHM1SUdKSVFUVXRMczVWSDV2VEJwSTlTTnphU05jZjI3ZHZEc0dIRHhQOWxRVTdKSytTRURYUU1tYzZXWlVGRFF3T1VsSlMwQ28rc1UvQzNLaGhsZ2NsQ3NmWEN3dkF3b1E2UWx0SnFhRkUvUUxSZ0RJVkNRdmpKYzI1eThnTmQxMjFKRVdUaUVUYnk0S2JCSG9sRVFOZjFLQ0ZON1JnMmJCaFF2S0RjZnNNd1JBWVlBSUNlUFhzaUNXc1Mwb2dJTzNmdWhJYUdoc09XcUtFeHFLMnhyaS8xQ1gxbVlkaDZZV0Y0aUlsMTg3ZmtuR0VzN3krbG1TS3RVTjdIbVk0dExDelVTQU1qWVNOcmEwMGhEM1o1ME5mVjFVRWdFQkJ0cEhrenk3TGc0b3N2aHY3OSt5TUpFbGtUbFB2bHpEUFBCRnFpS0F2eFJ4NTVSQ1BCS3ZkRFl5dUE1R1BrUHpwT1hTUHNsSkxMaWFhMmNWWGJSZGVnS2FjTGMvaGhZZGpHSWZNUllMOXpoQVpaV2xvYUdJWWhncGtwUFpSbFdlRHhlQ0FjRGtORlJZWE5oSFo2YlF6UytLZ3RKSUMrK2VZYjJMTm5EeGlHSVV4Z2VUN3hvNDgrZ3RUVVZBRFlIMnhObXFGcG10Q2hRd2M0NTV4eklERXhVV2l2aUFqVHAwK0hnb0lDY2J3OHgwanRJTUdtQ25QU2pPVS9PbzZPcGZsSlNpY1d6L216VURzNllHSFl4dEUwVFRnVy9INi8wTTR5TXpQQjQvR0FyUFhSOFM2WEN3ekRnRzNidGtGdGJhMzRYdlk0eHd0NXNWVUJWRkJRb08zZXZUc3FLRngyNG56MjJXZVlsNWNIY3A1RkVpNVRwa3pCVWFOR2lYMkVBNEVBUFAvODgvREVFMDlvc29DWDV6ZmwrVkY1aVYrc2ZxUDRUQUM3STBvMWFSdmpVR3dBeGh3WldCaTJjVWhZQU96enZ0SUFIanQyYk5UYVkxbm91Rnd1ZU91dHQ0UzVKczhqTmtjWTBQRWtnQUQybVpvTkRRMndZTUVDNGZXVnRUVExzc0RyOWNKSko1MEVVNmRPeGF5c0xOdTg1WUlGQy9DdmYvMnIwRHByYW1wZ3hvd1o4TkJERDJsVURnVmRrMmt0YTNweW14cHJNN1ZMRm9qa2dKSk4vc2J3Ky8xNHFLWkFHT2FvZ2diRzNYZmY3YmdtbVRqUTVYaDBqSm9ndGJDd1VDekhrOWNsMHhyaDZ1cHF2UGppaTZPV2ljbGx4dXRBa1plWXlXYTYyKzJHOHZKeWthVUdNWHAvRU11eThKNTc3a0dYeXdVREJnekE5ZXZYbzY3cllnbGVRVUVCcHFhbVJzMG5PcTJIbHR2c2xCQTJWc1lidGMxcWZ6YkdLYWVjZ3JXMXRZN1phb2pISDM4Y25mcVhoV2JyZ2pYRE5nNXBMNlFGZVR3ZU9QZmNjN0ZyMTY0MklVQ2FrTmZyaFVna0F1Ky8vejU4OWRWWEdwWGhwQTNHNjYybGNzbGtKZWVHYVpwdzRva25haVVsSldJVmhpeGdMY3NDMHpSaHpwdzVzR3JWS3Z6bGwxK2dYNzkrVUY5ZkQ2dFhyNFkvL2VsUDBLZFBIeTBZRElyekkvT1hsc0dSVnF4cXdmTGVNVFJQR210SkpIMHZ4MG5HaTgvbk8rQ1ZPMHpySXI3SEg5TnFJZk9Xekxxc3JDeTQ3YmJiQUFCczM4c2U0bEFvQkU4OTlaUnR6U3pBZ1cxQ1JNZXIyNU5Ta1BYZXZYdGh4b3daOE5SVFQ0bmtwaVNZWk8zcmpEUE9nSktTRW5qdnZmZGcyYkpsc0h6NWNxMit2bDc4bjB4bVZhRHB1ZzZabVpuUXAwOGYxSFZkckwybStVRFNLT2xQVGtLaGFacVlqL3p5eXk4MStmempGWXFrb1RyRkdyTG0xN1pnWWRqR2tiMmZQcDhQUm80Y2lTTkhqaFFiVFFIc0Q3OGh3WG41NVpmRGp6LytxS2xKSEdSaElBdWZlQ0RoSVc5a1JFTDRqVGZlME00Ly8zd2NOMjZjMEZJcDdsRVc1ajZmRHo3NTVCUDQ1Sk5QTkpyTG8zTHBsYjZqY2pSTmd3a1RKdUQ5OTk4dk5sYVNuVGF5NEl3bG5EWnMyQUFYWEhDQm1IT2xjdVAxSmpOSEIyd210d0xJK2FEdW9DZlB4UkVlajhlV0daa0VYVUpDQWx4d3dRVzRjT0hDcVAxWUtFeWt1cm9hSmsyYUJGOTg4WVZHamdKWjJNbnZ5YlJzQ3JXTmNrZ0tDVHJUTk9IQkJ4L1Vnc0dnN1J6VmViUjI3ZHJCN05tem9WZXZYcUtzV1BOckpIQXR5NExxNm1yWXZuMDc3TjI3RitycjY4SHY5ME5tWmlaa1ptWkNWbFlXWkdWbFFVWkdodmd1UFQwZGFGdlgydHBhcUsrdmo4b21FKzlEZ01LYTVHa0dFdkpzTWpPTVJGTU9GSElvMU5UVVlGWldGZ0RzRXlLcUkwQXVpOTdUWDgrZVBXSDI3TmxSRS9lSSsvZEIyYnQzTDA2YU5BblQwOU5iM0h4VHcxVEkvQVFBU0U5UGgyblRwdUhQUC84c0hEaTBnNTk4L3JLRDU3UFBQc1BNekV5eHJNOXBYdEFwY0JvQW9HL2Z2dkRnZ3c5aVRVMk5LSmVjU0lqN3Nud0hBZ0djTldzVy90ZC8vUmNPR2pRSWUvYnNLY3B0TE1XL0UyUEdqSW5LTEs3dWg4SU9GSWFCK0x6SmhtRmdaV1VsdG0vZlB1YTJtYXEyUi9OZlU2Wk13Zno4ZkF5RlFtaFpsa2p4VDRJUUViRzR1QmlIRFJ1RzZrQnZDUk12MXFvUFRkTmc3Tml4K01NUFAyQW9GRUpkMTlHeUxOeTJiUnRXVkZSRTdkTmlHSVl0NmUwSEgzeUFDUWtKamx0MXh0cHFsSVNqMysrSDU1OS9QaXE3TnlKaWVYazVubkxLS2FocG1rZzNKcGRCN1pmWFZ6ZkdGVmRjZ1lGQXdGWVBDME9HY2FBcFlVamZWVlJVWUtkT25jU2tQM2xORXhJU2hDQk1Ta3FDdExRMDZOZXZIOHlaTTBlRXJGQjZmSFhmaytycWFuenNzY2RzV2FGSkNMUWtjdUF4N2VrOGUvWnMyeDdPaG1GZ2ZuNCtBZ0RjZi8vOVdGOWZMOEo4NU5BYkVwS0JRQUNmZmZiWnFMWVRxdGFtZms1TlRRVzVUM1JkeDlyYVdyemtra3RRMW1MbDM4cGx4SnU1Wjl5NGNWSENVTDIyTEF6YkJ1eEFPY0tRQThUbjg4RTk5OXlENWVYbFlsa2FKVVJ0Mzc0OWRPellFWEp6YzZGdjM3NlFsWlZsVzFLV25Kd01BUHNHY0NBUWdMVnIxOEwyN2R0aDNyeDU4UFBQUDJzQSt3UVdoYU1RQitJOWRvS0N1UUVBaGcwYmh2ZmNjdytNSGoxYXBPK3lMQXRXcmx3SkkwZU8xTnh1Tjh5YU5Vdkx6czdHMjI2N0RXUVBNUDRlOUl5L0x6R2NQSGt5N04yN0YyZlBucTFSMkFzNVNlUTVQVGxnbkQ3TENWV3Bqei83N0RQNDdydnZOSlRtTktrZkFPekwrUXpERUsrTjBkZ0tGQloyRENNUmI5QzF1b01kYVJicWZpbnlaOW5FTEMwdHhWdHV1UVZIang2TnVibTVVZldUV1gwb3ZKK2thVjU2NmFXNFpjdVdxQjNpVnF4WWdibTV1VUw0MExMQlJ4NTV4R2JXcTV0R1daYUZnVUFBSDMzMFVaVHJjUXFTcG5oS2lxM015TWdBdWF5cXFpcjh3eC8rRUNYNW5UTDF4R3NpQXdEY2VPT05HQXdHRzkwM21UVkRob0g0NXd3UjkyM1M1Q1FZZEYyUFdya2hRd0tGQXEwSkp5Y01tWjB0TFJUUE91c3MzTEZqaDJoTE1CaEUwelN4cnE1TzdNSW45NGZYNjRXVWxCUjQ4Y1VYYlFKUkZxSnl2OXgzMzMyb3pwbksyNHVxNTNuaWlTY0M5WTFwbXJoeDQwWlVIVEcwRHRwcFpVcThadkxreVpOdCt5YXpNR1NZR0RRbERHbE9xN1MwRkljUEg0NERCZ3pBZ1FNSDRvQUJBL0Nzczg3Q0o1NTRBdGV0V3ljY0RMTGdrTFhFVUNpRWhZV0ZtSk9UWTlOc2FGQWZ5b1FDS1NrcHNITGxTc2Z6dXZQT084VWFhWGxKSGIzbTV1YkNSeDk5Skk1MzBnNFI5emsrcnIvK2VveDFMcW9ENU9XWFh4WmxCZ0lCN04rL3Z6aFd6blFqLzVab1RsOU5tVExGTnVmcHBDR3lNR1FZaUU4enRDd0xLeXNybzdiUEpQTHk4dUNkZDk0Ung4c0NROVlZRGNQQUo1OThFbE5TVXNSdjFaZytlZTF0Y3dkanJOQ2VKNTU0d2xGekxTZ29pR3RDY3NDQUFmanR0OStLYzFDbkF1aTcwdEpTdk9HR0c0U0dxSjRiQ2JqTXpFd29LU2tSUW1yZXZIbm9kRnhMOEplLy9FWFU0K1JSWm1ISU1MOXpzTUtRdEphaFE0ZUtXRDBueUd0YlYxZUhGMTU0SWFyYVRxd2twTTFCWHNwR2dpZ3ZMdy9JdkNkTmpJVER0R25UNHZiTzVPVGt3SVlORzJ3ZWNYb3ZDOGFxcWlxODVaWmJiTUpORlRUMzNYZWZNRjAzYmRxRS8vRWYveEYxZkVzSm9udnV1WWVGNFZFQ3IwQnA1WkEzYzgyYU5kcWlSWXNBRVcxZVlWUzh3eWtwS2JCdzRVS1FRMUo4UHA5WVVTSXYwWXRuTUtwSkNLZytXaDUzNzczM29oeWFZbG1XTU5QWHJGa1Qxemw2dlY0b0xTMkZhNjY1QmdvS0NrUnFMWG5KSGRXWmtaRUJzMmJOZ3V1dXV3N0pZU0lmMDd0M2I3amlpaXZBNy9kRFhWMGQvTS8vL0k5WWVpajNXVXNKSWs3dWV2VEF3ckNWSTV1QzgrZlAxeFlzV0NDeVJ0T2FYa3FLQUxCdnMvWnUzYnJCSzYrOGduNi9IMXd1bDlpQzArVnlnYTdyUW9CZ004TnFWQUhjb1VNSDZOZXZuMWlTRmdxRmhBQ0xSQ0lpN1g5VDU2ZnJPdWk2RGovKytLTTJkZXBVS0M0dXRwMDNTa3ZkQVBiRkVNNmNPUk1tVHB5SThycHFUZFBnbm52dXdmNzkrd01pd3RxMWErSDExMS9Ybk1vNG1BMnZaR0pOTjdDQVpCaUZscGd6bEVsS1NvSVZLMVpFbFVGemR2UWFDQVR3Z1FjZVFJQm9KNHFjMml2ZWM1RG5ITWtEZS9iWloyTlJVWkZZWVNLYnRtVmxaWGphYWFjMUtXMVY3N2VtYVRCczJEQ3NxcXJDWURBb2NqTEs1MGZ6aUh2MzdzWEpreWNqdGUrWlo1N0J1cm82Y1Z5L2Z2MGNyMFZMZXRKbnpKalJxS2VmemVTMkEydUdyUngxN2k4VUNzSE1tVE9odXJyYVpyS3FnaTB4TVJGdXV1a21PUFBNTTRYMlJBSE9jbjYvZUpHMVNOSXF1M2Z2RGhrWkdlRDMrMjNiQ3lBaVVFTFdwaUJ6T3pFeFVaekhtalZydEhQUFBWY0VjNnZMRW1uT3NuMzc5akJ6NWt5NCtlYWI4Znp6ejhmSmt5ZERTa29LaE1OaEdEbHlKUHoyMjIrT3YzWHFyd1BsUUJ4UlRPdUVoV0VyaHpMYXlBTnUxYXBWMm1PUFBTYjJQZ0VBWVM1VE5ob0FnRTZkT3NIa3laTnRleU5ISWhGYm5yK21VRmVwa1BEQzMxZUpKQ2NuaTB3MXRHcURZZ0RqRVlaMGJzRmdFSHcrbnpEOTE2OWZyMDJjT0JGS1NrckVDaElBKzhieUxwY0xNak16WWRxMGFiQjQ4V0p3dTkwUURBYmgxVmRmRllscjZiZDBIaTJ0bFhFS3I2TUhGb2F0SEUzYm40dFEvangvL254dDVjcVZ0cFJZY3NJQy9IMDUzNVZYWGdsMzNubW5iUVVIQ2E1NDVneWR3bWtBOW1lY2xvK2g3TllBKzRURXdJRURteXhmem94Tis2VllsZ1dHWWNENzc3K3YzWGpqamREUTBDQWNTYkpqaU9ydTBhTUhaR1JrQUFEQWwxOStDYk5uejlib1FVR2FJSlVyNTM5c0NZRVlheXNCcHUzQnd2QVFnNy9udHFQQjV5U0ExTUVrQ3gwNm5yUTlLaWNjRHNNZi8vaEhMUkFJaU0yUkNEbkR0Y2ZqZ1duVHBzSGd3WU5STHI4NTdhZmZ5Sm9WQ1JZeU9Vbm95R1hmZWVlZE51MVFEWXhXdHdCUTUvUnczdzU2MnNrbm42elYxZFhaY2lVNmVkTjFYWWUvL3ZXdlVGUlVKUHBRYlQ4SmJMbWRjaHVkMnRZWXROMUJZMzJuT3F0VVp3N1RPbUJoMklxSTE3c3JhemwvK01NZm9LcXFTZ3c0MlZRbWdlRDFldUdOTjk2QWpoMDdBb0E5czNWejIwUy9wUjN3WkxPWkhDczBKOW10V3pjNDk5eHpSZWdOdFlsQ2ZHU3RWazduVDJWU2tvY3RXN2JBaEFrVGhKQUQyQ2VzWkM4NnpTTys4c29yY01ZWlp5RDFCd2xxZVc5bTlkems3NmlOYXFMZFdEUVdvdFJVL3piWG04OGNXbGdZdGhKVTdhRXBTQ3Y3NXB0dnRFV0xGb253R1huL1l0bms3TnUzTDB5ZlBoMXBIeEpLdTk4Y1pLY0RJa0orZmo0VUZSWFpCR1E0SExidGovejY2Ni9EY2NjZEo5cE01Nm5yT3ZqOWZpSDQ1RUJ1VGRPRW80ZjQ1Sk5QdEVtVEpzR21UWnNBWUo4V1NPZEE1K3AydTJIQWdBRXdkKzVjT09lY2M5RHI5WXI2YURzQXAzT1NBOG5sNnhEUFBpak4wZTVZRTJ6ZHNEQThETVFqNkdLWlVrMzl4cklzbUR0M3JyWjE2MVlobEdpVGRkTFVLQlhZVlZkZEJVT0hEa1ZLVGRXY29HdTFQWWdJbXpadDBqWnYzaXlFaVdFWVlrNlAydEt1WFR1NDk5NTdNVDA5UFNxSElPMDVRa0tOTkVZU1hxVFpFY3VXTGRQV3JsMExBUHRUa2dIWVY5Y1loZ0ZEaHc2RnYvLzk3M0RGRlZlSTNJVXVsMHRvemRSKzBqeGw4NS9LcG5MajZSODFNRDFlV0ROc1hiQXdiQ1hFSXpDZGhKZGxXVkJSVVFFalJvelE2dXJxQUFDRXFVci9KMUpUVStHRER6Nkl1YU5ickhiRmFrZE5UUTBzV2JJa3l1eW1lVVNhbTd2MjJtdmg3cnZ2UmpudklqbUZ5TFNudWlpY1JqYWYvWDQvWkdWbHdUdnZ2SU1USmt3UTJxQzhkN0poR0RadmRuWjJOaXhZc0FCdXZ2bG1sSVdxL0o0ODlmSW1WclFOS1FuS2xvRG5DQm5tZHpSTmd6dnV1S1BSd055eXNqSWtqMmhqYzFCT2Y4VEVpUk5GNml6RS9RSEtpQ2pTN2x1V2hSOS8vREZTVHNIbTRKUUoydVZ5aVNRU2NwcDlOZXMySXVMQ2hRdnhyTFBPUXFjTjNXVXZ0VnlQeitlRDBhTkg0N0pseThUNUJJTkIzTEJoQTRiRFlWdEF0Z29kKy9EREQyUDc5dTBCd080c1VaTzcwdnZtaE1zOC9mVFRVUnZJcTFBK3hwWk1FTUV3YlpKNGhHRnBhU21tcDZlTDQrWGZOZ1ZwVXFtcHFmRDQ0NC9ieWxVVHJacW1pWUZBQU8rLy8vNjRiRFF5dFJ0clYzWjJObnp6elRjMndhZENnbXpidG0zNHdnc3ZpQnlINnZtUklFcE9Ub1pSbzBiaDRzV0xzYVNrQkEzRHdFZ2tncVpwNHN5Wk0zSElrQ0g0N3J2djJvUzlDZ2xuMHpUeDg4OC94ekZqeHRnU05sRDhZYytlUGVIcXE2OUdWUWpHSXhULzlyZS9OU29NTGN2QzJiTm5Sd2xEMWhLWll4WW5ZU2d2ejl1N2QyOU1ZZGpZd0ZHZEFqazVPZkROTjkvWUVvNlNZSkRyTEN3c3hQUE9PNjlKZ1JoTFdOSC81SFQvUlVWRnRsM3VaSUVrWjU0eFRSTjFYY2VkTzNmaTQ0OC9qaU5Hak1BaFE0YmdPZWVjZzNmY2NRZCsvZlhYV0Y5Zkw0NlRoZnFnUVlOc0tieSsrT0lMV3oxcS85SjUwNE5neFlvVjJMdDNiL0Q1Zk9EMysrR3V1KzdDY0RpTTRYQVk1OHlaZzdJVEp4NmVldXFwUmpWVHk3SncxcXhaTEF3WmhnVEc5ZGRmN3pobzZYMVpXUm1tcEtRNGJrNFViejMwZXZubGwyTTRIQlpyZUoyRW9xN3IrTTQ3NzJCcWFpb0EyTE92TkdmREtIbFFqeHc1RXIvKyttdEhUU25XZWN1Q1R2NWVOcmQxWGNlUFB2b0lod3daWWhQZUhvOEgycmR2RDB1WExzVkFJR0NyVDAwU0s3ZEpuajZnejk5Kyt5M201ZVdKakR1eGhKV2FvSGJldkhtTzI3UEszOTF4eHgwMlllaVVnWnhoamhrbVRKaGdHK1NVamg1eFgvNi8wdEpTRWZaQ3hDdVUxT044UGg5TW5qdzVTZ0NvZ3NBMFRYenl5U2Z4WUFlbjNPNGVQWHJBckZtenNMcTYycWJaRWZKbmRVNVRGYUs2cnVQNjlldnhwcHR1d3M2ZE93T0E4N3hwcjE2OTRMSEhIaE45U2VjcTc1ZE1mUzkvcGp5RW4zMzJHUTRlUE5pbWNjcnptckdtQnpSTmd5ZWZmRkxNeDhySWZmeW5QLzBKNVljTmVkQVo1cGlDdHZtOCt1cXJSUVlXZWJEUWdLbXFxa0tBZmN2TkRqUkZ2eHhjN1BmN1ljR0NCVkhDa09xbXRsaVdoYmZmZmp2S0E1VlFsNzQ1UWZYSnIzNi9Id1lOR29TZmZ2cXBZMzB5NmlieTFEODdkdXpBS1ZPbVlFNU9qcTBmNVhPVis5anI5Y0lKSjV3QUJRVUZxQ0tuNVpmN3dMSXNuRGx6WnRSKzFVNzlHbXVWME5OUFB4MlZuVnNWaUJkY2NJSFFhT2tjRHVVMkRBelRhbkc1WEhEenpUYzM2bHdvTFMxRmVZQWN5SnlTYW9ZTkdqUUl0MnpaNGlnRTZMMXBtcmgxNjFZY09uUW9xaW0rbW92Y1p2cDkrL2J0WWZyMDZiaHUzVG9zS0NqQW9xSWlyS3lzeEpxYUdxeXBxY0h5OG5Jc0t5dkR6WnMzNDZaTm0vRFZWMTlGaW9WczdCemwrc2kwZGJ2ZGtKS1NBaE1tVE1CMTY5Ymh0bTNic0t5c1RBakVVQ2lFWldWbFdGUlVoTXVXTGNPenpqcExPRlJrSVJXUFI1bmE4ZUtMTDBZOTJGVDY5T2tqZnRlY25mZVl3d3ZQNGg1aUtNRDVpU2Vld0x2dXVnc0E5bWRjb1ZmRE1LQ3lzaEx5OHZJMFNuUUtBQ0toUUZOUVdlcnhIbzhIcnJycUtwdy9mejVrWm1hQ3J1dU9Hb2xoR0xCcTFTcTQ1cHBydElxS2lxaTF6dkhVN2ZWNlJmSUhkYjloV2crY2xwWUd4eDkvUEhicjFnMFNFeE1oTVRFUmFtcHFvS2lvQ0xaczJhTFYxTlNBWVJnaVBsRmVCeTFEZ2RsMG5QcC9xajh2THcrR0RSdUczYnAxRTlsMWR1ellBZi82MTcrMEhUdDJBTUQrWllUNCt3b1dpcEdVMTNlcnlOKy8vUExMZU8yMTF3cmhxQzdqMDNVZE1qTXp0VkFvRkhVdFk1WFBNRWN0THBjTDNuLy9mYUVGcWs0Q1JNVDYrbm84OWRSVER6b2VqYkpPRTM2L0h4NTY2Q0VSanllSG02Z09sdSsvLzk0Mk1wdVRyQ0RXOTAyZGl6eFBwNXJxNnZ5Y2FyYlRuSjZzRWF2SklHUmtBYXZHRzhwMXF0cG5yRGxEbDhzRnI3MzJtczFaSTV2NzF1LzdQcXNhTzlYUERoVG1tT09zczg3Q2pSczNScGxQOGh4YU9CekdWMTk5RlEvVW02d0tKRFdiOVNlZmZDS0VzU3FJNmZ0d09JejUrZm5ZcVZPblpnVWVrekNSUGEycW1hc0tvbGdDVkJaR3NiWTVWUjArTHBjcjZoaFZjTW9QQ1ZrQXExTVQ4dW9YcC8yWTVmZHBhV253M252dk9jNEYwK2ZQUC84Y0d4UFFESFBNTUhUb1VGeXlaSWx0b0RpRmtwaW1pY1hGeFhqcnJiZGljeWJYbmVicG5PalFvUU84Ly83N0dBcUZiQ3RVMUVFY2lVUnd3NFlOT0hueVpPemR1L2NCblhOanEyU2MydXMwUitmMEc2ZjBXckpXcHpvblNBaXAyYTZka1BkMmxsOWovZDdqOFVDWExsMXNjWTd5Sy9YbnFGR2pVSzJYTjVGcW5mQVZhUUc4WGk5Y2V1bWwyTGx6WndnR2c1Q1ptUW50MnJXRDNOeGNHREprQ0hUcjFnMFNFaEtpOGdIS3FhMW96bXIzN3QzdzNYZmZ3Wll0VzZDeXNoTHE2dXJBc2l3b0xDeUV6ei8vWEpQbjR1S2RjNkkxd04yNmRZUHJycnNPSDNyb0lmRjdXczlMaE1OaDhQdjlZQmdHckZ1M0Rnb0tDcUM4dkJ6cTYrdkJORTBvTFMyRjExNTdUYXV2cjdmTkN4NnQwRHlpbkJDVyt2ek1NOC9FdDk1NkN6cDM3aXpXUE12WHRhYW1CanAwNktCUnVqV2VIMlNPYXR4dU4vajlmdGkwYVJNR0FnSGhNWmJOWDlrVWRYcXZtcTI2cm92bFovUiswYUpGMktGREIxRm5jeUV2cHN2bGd0emNYUGozdi85dDB3eGxUN2ZxRWFXMkJZTkJMQ3Nydzc1OSt4NlQ4MTJrY1ZKZlhubmxsV2habGxqdEkxL1RTQ1NDOCtiTncxaWVlZFlNV3g4YytYbVFtS1lKUHA4UE5tN2NDSldWbFVKeklDMkFzcCtRVmlHbmtISzczU0xQbnF3NVVFd2RwWnd5REFPMmJkc212THh5MlUxcFoyU21SaUlSNGZIZHRXc1hEQjgrWE92ZHV6ZU1HemNPVHo3NVpPalFvWVB3Qkh1OVhsRTJaWGFobklHa0ljcDdyUnpOK0h3K3NVODE5VCtsUHhzd1lJQnc3SkNubnE1WlhWMGRmUFRSUjQ0YW9leXNZVzJ4OWNDUHA0TkVqbldUMDBISmtBbEYveVBCS0c5c0RyQi9ia29Od1VCRThIcTlVRjlmYjZzMzNvSGsxQ2E1REhwTlNVbUIxTlJVTWFkRm9US0dZVUFnRUJCQ01SZ014dGM1UnduMEVLTUhnMlZaa0ppWUNKczNiOGF1WGJ2YTBvTGg3M2taRnk5ZUREZmZmTFBXME5Cd2hGdlBNSWNSTlJ5RHZwUC9ZazJhTjJieXlzNEJKeS96Z1ppcWJyY2JrcEtTb3VxUnZhalVmdm05N05RaHoreXhZQ3FyamcvaW1tdXVjZHdIMnpSTkxDMHR4UjQ5ZXJBcHpCeGJ5S3NmaUthOGhYSThISEdvTXBwUXUzdytuNjFjTmVHQTNBNVpnTXVvbnVGalFSZ0MyTmNTYTVvR0hUdDJCRnFQTEs5L0RnYURXRk5UZytlZGR4NnExNUFGSTNOTVFKb2hhVStxTnFjS0VDY2hvKzRpUjRMSGFlYzJLdWRBQjVqYUhsVXd4eExzc2JTa294bTFuenQyN0FqLytNYy9NQktKUktVckt5OHZ4OXR2djEzTVhjajNnOU8xWWdISkhGV29nNlU1bmtOMTVVTmpkY2hsTlhjUXhkTGcxSEliMC9UVU9MeGpSU3NFMkgrdTZlbnBNRy9lUEt5dnI3ZVp4WlpsWVdWbEpkNSsrKzJZbXBvYWwrQnJxcjhaaG1FT08ycHd1UHJaNi9XQ3orZURyNy8rV29SS3lTRkpwbW5paUJFanNERk5ubUVZcGxXamFteXl0cGFZbUFqRGh3L0h1KzY2Qy9mczJXUExrMmhaRnBhWGwrUHk1Y3N4T3pzN2FrcmtZS1l4R0laaGpoaXlGdGV2WHo5NDdMSEhjTW1TSlZoVVZCU1ZoOUUwVFZ5elpnMU9tREJCYkRSRmpqUk8yc293VEp1RXpHRlpreHM3ZGl4R0loR2JTV3haRnVxNmpxdFhyOGJodzRkalptWW1BRVE3dFFEc3EzMll0Z00veHBoakdncGNsd1BZZFYySGlvb0txS3lzaEVna0FzWEZ4YkJ5NVVwWXVIQ2hWbFZWSlZhYVVCQzYzKytIY0Rnc1BPK1JTTVF4enlMRE1FeWJ3ZWZ6UVpjdVhXRDQ4T0dZbDVjbk1sK1QrUnNycDJKaldYWVlobUhhREU0bUxTVmdkWm9IbElPd0Nkbjd6REFNMDZad0VtYnh4R1dxditFRXJnekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNRWN2Unp6SGtMb3ZMVUR6Tmp0cUtrMVNhOWw5TEZZN1cwdjdtQU9qcWV0NnBIZkFhKzc0VUk5M2FydDhETzJvU0xzMEh1bnpQUmlPdURCVW9iV2U2ZzV4aDRvakxVeGJNdWVkVTFseSs1MnlhamYzL09JWkxBZFRYbk5wcXY2V2FsK3NjbHI3L1hFNEJKTlRIeDN1Y2R3U3RBcGg2SGE3WWRTb1VXaFpGbmc4SHRCMVhXeTlLV2YrYU83dWN2SFExTTNXVkg0NnA0MmM1RExWemFGaUxlby8wUHJwL0J2YldpQlcyK0laYU9ydjFOOGZiSnFxU0NSeVVMOXZLak5NVS90S056VlluYlp0amZWWjNpK2JQamRWLzhHMnJ5bmlGWVpPMlh0aW5hdDZqSzdyOE5GSEgyazBYcjFlcjloanVpM1JLbEo0VVJva2dIM1poU09SQ0xoY0x0QjFYV3hMU2JTMVhjZG9zRGRYQ0Iwc1ZJZGxXUWRWdDVObUtYOCsxSnBIV3pLNW5BUUpQYXhpblVkVEQvUERtU3pXU2RnNVdSRk9Ed0Q1b1VpQ1VKNzZhZ3NjY1VsQzJvVlRwelhXbVUyWkx5M1p2c1pvcXY1RGJZWWZhVE8vdGROUzF5L1djUWRiZmxPYTlhSE9pWGd3VXlaT3FOWmNXK0tJYTRieUJWQ0ZYMk9kMmxJT2xxWm9pUnZrWU1wb2FXSGMzTGFvZzdVeE0vRlFjTERuZjZnZlZvMzlQNTU3cnluQmNUanYzNmJtbkowZ2sxZ1dnclRuZGxOVEFLMk5JNjRaa2dBa2p4UWwwclFzQzB6VFBPS2FUMHNJbzRNWk1BZDZmdkZxem9kYTgyMksxcTY1SHN6RDVIQlpMNGVTZU80UHl2U3R3bWJ5QVJDUE9VdzBGUXFnY3FSdXhDTTVFT0l4ZlE2MGZTM3RUV1lPTGMxNVdCM010U1JsaGpURXRtd3V0MmxjTHBkdElscWRkRlkzL0NIY2JuZlVKajd4Yk9ydVZML2FCcWV5RG1ReVBGYTduWERhWUw2eERjM2pQVWMxYzdOc09xc2JJalduMzV5ODBlcG05ZXJ4VHZVNFpaaHViQXRRS2wvdUIvcS83SjJQOTF6VXRzcWY1WExVZHNmcXgxam5FQy95TmdXTmVmeGJ1L09SYVNieVJYY1NSazRYbklTWC9KbU85M3E5am9JdEZrNkNTaFVRTlBBT1JpaXE1OWRVU0kzOE96b2ZwekNncGxBSE51R1VEcC9hNUhhN2hXQ0tSNmk0M1c3d2VEd3hCYXZINHhGOUtOZXBDakNxeitrWXAzTlFmMHZ2S2JLaHVXRkRqWjJEWEJhZEQ3Vkh2UzdxdFdvTStqM1ZwZFlwQzN3cWwvWjFZWTVTMUVIZ3BEbW94M2c4SG5Iak8rMWJFYzllRmpUWVpVMURyVitGYnZSNGJuaDFzTWczdUN3UWFVQTRhV2V4NGh6akhSQStuOC8yTzZmdE1aMytSNzlSLzlTMnlkZEU3bk5WNEtzYnZBT0FUVWlxL2VyMyt4M2JKUXR0T2w2dFN3M3BhZ3l5S3B6NlJVVVdoQ3F4dE9SNGlLVXRBL0NlTE1jTVRocVRrem5TbUZhay9pL1dKa0R4SUFkWms2YmdaTWJGVzc1Nkh1cmdWczlOUFErMWJVNER0ekZpbWFzSkNRbTJ0cWw3QmRObnA5L0cwcVpsVFkvYXAycGJBSTBQYm5Xakp2VkJJZ3ROcWt1bHVlYStYSTlxcVpDMTRkU1BjaCtwSm42ODlUdVo5MVJYWTlNWjZ2K1pvNFI0TlIzVlpLQ2J6dWxHYms3ZFZCNzkzbW5BcVJwVXZNam1qMXFlazhhbm1rcnhtdFB4dE1QSmZIT2E0MnRLRTJtc0xiRWVIdkkxVTQrbHFRMjFUZlRxSkl6a2g2aXFVVHNKMGxnNFRRZVExcXFlcTJ3OXFCdElPWm44emRYb1lnbS9XUDNObTFmWmFmTVRCMDdlSzVmTFpWc2FsWmlZQ0VPR0RNR0JBd2RDY25JeUpDUWtRSFYxTmZ6ODg4K3djZU5HcmJ5OEhEd2VENWltS2FMcHFieW1QR0l1bHd0Njllb0ZnVUFBa3BLU0lCZ01SajJsQTRFQUdJWUJaV1ZsVVF2YjQwRU9PMUlEMUgwK0grVGs1RUNmUG4yd1U2ZE9BQUN3ZCs5ZTJMWnRtMVpVVkFTaFVFaWNDN1ZIMTNYSHNocURQUDdVdDcxNjlZTGV2WHRqUmtZR0pDWW13czZkTzZHMHRGVGJ0R2tUV0pZRmlDZ0dvVk1FZ0pQblVsN1BtcGlZS0pabEJvTkIwVmI2clJxQlFOOG5KU1ZCSUJDQWxKUVVzZm9uRW9sRUhVL3hjZlE3dVUycHFhbWc2em9nb2lnamxxZFZ2ZGY4ZnIvdEdwT0h0YkZFQmo2ZkQwelR0TFdKN3NWNElNRnVHQVo0dlY3bzE2OGZubmppaWRDalJ3L3dlRHdRaVVSZysvYnRzSGJ0V3Eyb3FBaDBYV2RQNzlHTWJGWVJQWHIwZ0RmZmZCTzNiOStPdGJXMVdGWldocEZJQkJFUjYrcnFzSzZ1RG91S2l2QmYvL29YWG5iWlplTE9hNDVta0pHUkFidDM3OGFhbWhvc0xpN0d2WHYzWW1WbEplN1pzd2ZMeXNxd3BLUUVTMHRMY2ZmdTNiaHo1MDVjdm53NVhucnBwUmp2SkxhcU1kRDV0Vy9mSGw1NjZTWGN1WE1uN3QyN0YydHFhckMrdmg3cjYrdXh0cllXUzB0THNiQ3dFRjk2NlNYTXlzb0NBT2U1cmFhUXphN016RXhZdUhBaGJ0NjhHY3ZMeTdHaW9nSURnUURXMWRWaFRVME5scFdWNGE1ZHUvRGxsMS9HenAwN0MzUFhhWjVRMWNMazYzYmxsVmRpYVdrcEZoVVZZV0Zob2JnMlR1YWYyazlqeDQ3Rm9xSWkzTGx6SjVhVWxPQ1VLVk9RNXYvVStVbTVQZlQ3M054Y0tDd3N4SjA3ZDJKUlVSR1NnRzdzV3NtYTdQTGx5N0d3c0JBTEN3dHg5dXpaNktUbHl1OVBPKzAwL082Nzc3Qzh2QnhYcmx5Sko1MTBVdFI5MkJpeUJmRC8vdC8vd3gwN2RtQnhjVEUyTkRSZ0tCVENZRENJNFhBWWc4RWdGaGNYNC9yMTYvR0JCeDdBNXN5Sk1tMEV1cEZsY3lNNU9SbkdqeCtQb1ZBSVRkTkV5N0xRc2l3TWg4TllVVkdCZS9ic3dlcnFhalFNQXhFUkRjTkF5N0l3UHo4ZmMzSnltbFYvVWxJU0lDS2FwaW5LSVhSZFIvbC9oR1ZaK01NUFAyQ1BIajNpcWtQV05CTVRFK0hQZi80ejF0YldvcTdyYUpvbUJnSUIzTE5uRCtibjUyTitmajd1M2JzWGc4RWdtcVlwaFArTUdUT3dmZnYyQjdTL2IyWm1Ka3lkT2hVdHk4SlFLSVNJYUt0ejI3WnR1SHYzYmd5SHc2Sy9RNkVRL3ZkLy96ZjZmTDVHazFXbzVyN2I3WVl4WThhSWZyUXNDeXNySzdGMzc5N2l0M0k1OG5uMDY5Y1BxcXVyYmYxL3p6MzNvTlA1a2tCU3B5KzZkdTBLVkM4aVlpeW5GS0Y2aVgvKytXZHh6VTNUeEVjZmZSUXpNek1CSUhwZUZXQ2ZNQ3dzTE1SQUlJQmJ0MjdGUVlNR05Vc1llandldU9paWkvRFhYMzhWMXdVUk1SS0pZSGw1T2U3WnN3ZHJhMnV4cnE1T2ZJK0lXRlpXaHVQSGorY2dVWWtqdmh6dllDRVRoMHlvcEtRa2VQREJCL0hPTys4VVpzYkdqUnZod3c4L2hKS1NFaWdvS0lCSUpBSWRPblNBM3IxN1E3OSsvZURpaXkrR3RMUTArUFRUVDhVaTgzaE5XYnBoS2VQT3ZIbnp3RFJOc0N4TG1HWXBLU25RcFVzWHVPeXl5OEF3RFBCNFBEQm8wQ0JZdUhBaGpoa3pSZ3NFQXFCcG1pakRNSXlvUEk4dWx3djhmai9NbkRrVHAwNmRDcnF1ZzhmamdROC8vQkJXcjE0TjMzLy9QZXphdFV2VE5BMjZkKytPZ3djUGh1SERoOFBvMGFNaEZBckI5T25UNFlRVFRzQXBVNlpvNWVYbG9reTVuUUFROWI1ang0NHdkKzVjSER0MnJPamp0OTU2QzlhdFd3ZHIxcXlCNHVKaVRkTTA2TmF0R3c0Wk1nVE9PT01NR0RWcUZQajlmcGcvZno0TUhEZ1FIM3p3UWEyc3JFeVlpTEtaS3kvaFFrVFJMbGx6VEUxTmhSZGZmQkd2dSs0NnJhU2t4TlkvMUtiTXpFeDQ5ZFZYTVQwOVhkd1Q4aHluYkdKcm1pYXVNOVZQOTRxcXNkSjFrZjh2djZmcEIvcU96R3E2bHRPbVRZT0VoQVNjTzNldVZsSlNZcXVUSGhJMExTRFhxeTV6bzFjNUk0ekw1WUlycnJnQy8vYTN2MEduVHAxQTEzV29yNitILy91Ly80UGZmdnNOdG0zYkpxWnZldmZ1RFQxNzlvUVJJMFpBWGw0ZUZCWVdRbEZSa2UxZWx1OTVEcHB1ZzZnMzd3MDMzSUNWbFpYaTZUeHYzanc4L3ZqamJacUJQTUdkbHBZR0kwZU94REZqeG1CU1VoSUFOTS9MbHBLU0FxWnBpcWR1UmtZR0FPeWZ0UGY3L1pDUWtBQWRPblNBb1VPSFlpQVFFRS9uNHVKaVBQdnNzOFhUV1kydFU1azFheFphbGlXMHpJa1RKMktIRGgyaXZPSDBsNW1aQ1E4ODhBQTJORFFnSW1JNEhNWkhIbm5FVVJ0d01tVUJBS1pPbllxQlFBQXR5OEthbWhxY01XTUdkdWpRSWVvM0pIaXlzckxncHB0dUVscDVJQkRBbVRObm9xeUJPWm1kOHVjLy92R1BRcnN6REFNTncwRFROSVdXcHpxNk5FMkRGMTU0UVJ4TDUycWFKazZiTmkxSzAycE00K3JXclJ1Z2hKTlc2L1NlWWl2WHJWc243ajI2Vm9GQUFKOTk5bGxNU1VrQmdHZ3plY2VPSFlpSW1KK2ZqME9HREJIdGphWEZ5MXBzVlZXVjBJS3JxcXB3OE9EQm9oN1pLKzl5dVNBaElRRk9QUEZFZVBUUlI3Rm56NTdDb1JNckhPdGduVzdNWVVZZVpEazVPZkRWVjErSkFmSDIyMjlqVWxKU1ZEQXRRSHllNDNpRVlscGFHcEJKWlpvbXBxYW1SdjFXRHRWWnRHaVJNTnVycXFwc3BvbzhTTlM2Ky9UcEE3Lzg4b3N3My83NjE3K2lHcnlyL3BaZVAvamdBMkUrTlRRMG9PeDlWYjNPaEtacGtKS1NBaFVWRlVLNHZQMzIyNmorVGcxUWQ3dmRrSmlZQ0RObnpoVFRCSnMyYmNMKy9mczNlbjZ4aEtGczlwbW1pWDM2OUxIOXp1UHh3UGp4NDdHeXNoSU53OEJJSkNLRVB5TGlmZmZkZDBpRm9mem44L2xndzRZTkdBZ0VNQmdNNHZidDI5R3lMTkVQenp6elROUUQ5OXh6ejhYaTRtSzBMQXMzYjk2TWd3Y1BSdmsrcFQ1Vis5bm44OEdTSlVzd0VvbWdZUmhZWDE5dm13ZVU1MEc5WHEvNExlVUpqY1d4TEFUYmZLQ1JiT2FrcEtUQWFhZWRCaTZYQzBLaEVDeFpzZ1RDNGJDNHdPRndXQVM5dXQxdVNFcEtnb1NFQk1qS3lvTGs1R1RJeXNxQ3RMUTBFU1lUajVsQXBoSUFRRGdjRmdOZURyT2gvMTk0NFlWNDNubm5DVk90cHFZR2R1N2NLY3FTODhDcGRaOXp6am1ZbDVjSG1xWkJmbjQrckZ5NVVpU3pvSGJJYlpMNzVxcXJydEtvUFFrSkNmQ2YvL21mS0h2ZUFjQ1dGSVBNdnFGRGgySldWcGJ3eHQ1eHh4MmFiQmJLcGkwTk10TTBRZGQxV0xac0dlelpzd2NRRVhyMDZBRURCdzVFOG9pcjdXMk1IMy84RWFaTW1TSzgvQnMyYk1Eamp6OWVQTmd5TWpMZ3hodHZoT1RrWkhDNVhQRFVVMC9CeHg5L0xLNUhjK282RU1nOHh0ODl6NkZRU0ZnZXQ5eHlDN3o2NnF2Q20zenJyYmZDczg4K2E5UG13K0d3TUxYcG5HU3ZOL1dwbkxpRWd1Qjc5KzR0d29xbVQ1OHVUSFNmendlSUtNeHJPb2J1N2FTa0pNak16SVIyN2RwQmNuS3k3WHlPVlVFSWNCVE1HWkt3K1YyVFFZQjlGelFTaWNDLy8vMXZUUllXZEZPKytlYWI2UFA1UkxZTkNpQjJ1OTFRV2xvS0R6endnTFpyMTY2NDZnK0h3eUtzSVRFeEVXYk5tb1UwRDBSYWhkdnRobmJ0MnNIZ3dZT2hhOWV1SXVTaW9hRUJmdjMxVjQzYWgwcUlCMzBQc00rSmtaS1NBcFpsUVhGeE1XemV2RmtJSmdCN0tpajFuTVBoTUd6YnRnMzY5ZXNITHBjTHpqdnZQSGp6elRmRmZKVmFGNTNQaUJFandMSXM4UGw4VUZSVUJEVG5SWUtKSGdRdWwwdUVvbmk5WGpBTUEzNzg4VWV0dExRVXUzYnRDbjYvSDlMUzBteS93empEUnJLeXN1QzExMTdUVGp6eFJKdzBhUks0M1c2WU0yY09Ybi85OVZvNEhJWjU4K2JoMldlZkRaWmx3VTgvL1FRdnZ2aWk5dGhqanlIK25rM2xjQXh1RWxoK3Z4Lzhmcjh0WWNITk45K3NWVlpXNHRTcFV5RVNpY0NFQ1JQQTUvUGhmZmZkcHhVVkZVRkNRb0lJclNGQko0Zmh5QTh0dWw2UlNBVDY5dTByN2dlWHl3WDUrZm1pUFNRVVhTNFhYSG5sbFhqMTFWZUQxK3NGUkJSMVVTalozWGZmclczY3VGSDhObFo0MnJGQW14ZUc4b1J2WW1LaXVHSG9Cdlg1ZkJDSlJHd1g5cHh6em9IT25UdURydXZpSmdIWWQvUDk5dHR2a0pLU0lyNXZ5b2xDVDNQU3ZHNjY2U1loaEZSbkFFRkNjc3lZTVZwdGJTMEEyRFVNRW9yMFdXNEx4UWxHSWhGeFExUGJTZHVRSi94SktGTzhHVDBFWklFa08wMUlvOUEwRGRMVDAyMFQralNZSXBHSVRaQlNXU1FVcVEwMGNIMCtYNVNqaHZxc3FmNnRyYTJGK3ZwNm1EdDNyalpzMkRBY01HQUFYSFRSUlRCKy9IaXNxcXFDOGVQSGkvYWVmdnJwR21sTmNwOGVTdWdjYVA0dEhBN2JBdVV0eTRKNzc3MVh5OGpJd0J0dXVBRU13NENycjc0YUVCR3Z1KzQ2amE0VlBhZ3A1SXJ1QXlxRDdnbjZUSEdJZEkvUjkzSTZMVVNFYnQyNndXV1hYU2JhcXpyT1pzMmFoWWlvQWV3WGdNY3FiZDVNbHVmTXlzdkxOZkkwaHNOaHVPU1NTOUJwajQxZmZ2a0ZWcTllRGQ5ODh3MHNYNzRjdG0vZmJydXgzRzQzNnJvZWx6ZFp6dU5tR0FaVVZsYkNuajE3WVBmdTNWQldWZ2JGeGNWQVpWbVdCWVdGaGJCdzRVTG8zcjI3dG0zYk5zZTVQaFZkMTRXNWIxa1c5T2pSQS9yMjdZdHkrMGlJeVlLUVNFaElnTzdkdTRQWDZ3WFROT0hMTDcrMHBXcFhoUWVWc1dyVktsRnVWbFlXWkdkbkM2MkRoQ3dkRDdBL2RNVGo4Y0RBZ1FPeFU2ZE80UFA1UU5kMUtDOHZqenEvZVBxWGhQRDI3ZHZoeVNlZkZKN2ttVE5ud3FKRmkwRFROS2lycTRQTExyc01RcUdRMExEVVA1bVdIUENrQlpxbUNhRlFTTXp2QlFJQjBYY0FBSk1tVGRMZWZ2dHRjYjlPbkRnUlhuLzlkY3pKeVJIWGxzeFp1WDF5c0R5Wnl5NlhDMnBxYWlBVUNnbnY4dENoUXdFQWhBY2FZTjkxTFMwdGhWV3JWc0VYWDN3QnExYXRnaTFidG9ocnB5NHFvTExwL2JFc0dOc2s4czJlazVNRDMzNzdMWkozZC9ueTVkaTFhOWVvWTJtZWhNeWF1KysrVzB5WTUrZm5ZNjlldmVKZU81eVNraUxpREUzVHhERmp4dURvMGFOeDVNaVJlT0dGRitJRkYxeUE3Nzc3cm5COHJGbXpCbzg3N3Jpb0NYRnFuL3BLNzA4OTlWVGN1bldyY0NiTW5Uc1g0MDBrOGZUVFQ0djZhMnRyTVRVMU5lNkE4bkE0TEJ3QWMrYk1FVUhFYW55ZDdMUkpTVW1CdVhQbllqQVlSTXV5OEpkZmZzR1RUam9KWmUwMVhtL3k2dFdyVWU2bnYvemxMOEpUaTRnWUNvVnczcng1U05jME5UVVYzbjc3YmZINysrKy9QOHJwMHhnSDZrMEcySGMva1pNckdBemlSUmRkaEFEN0hXTUpDUW53OU5OUEk5MHZ1cTdqMTE5L2pYVjFkV2lhSnU3WXNRUFBPKzg4bFB0U3JvZW1YQUQyYWZOTGxpd1JudmJTMGxMTXk4dUxPaCtmendlSmlZbGlUdmVtbTI0UzhhbUlpTU9IRDBlNTNNYk9qMmtEMEVYemVEend3QU1QQ0E5YktCVENWMTU1QlozU0pkRU42bmE3NGI3NzdrUEVmZUVKVzdac3dmNzkrOGRkZDNKeU10QnZFUkhUMHRLaTZqcmhoQlBnMTE5L0ZkN092Ly85NytJNDlWaHFrM3B1bXFiQlAvN3hENHhFSXFqck9nWUNBYnptbW10RVVIQXM3cnJyTGx2QTdhMjMzdHJrYjJRZWZ2aGhNZUJxYTJ0eDBxUkpZbDVXYnF1OFVtYjgrUEZZWFYyTnBtbGlNQmpFWjU5OTFpWlUxTUd1bGpkNjlHZ2g3TDc4OGt1YkFOWTBEWjU1NWhuYnc0dUNtajBlRC9oOFBuam5uWGRzWG5mNWZOUitWaDlJZVhsNVFoaGFsaFVsRE9XMnFuZzhIdmpwcDU4UUViRyt2aDVIakJnUjFkZVptWm53L1BQUG82N3JJcXFBMnJwdDJ6WTgvL3p6bzd6ZlRobDN2RjR2NU9ibWludlBNQXdzS0NnUUQxb1pPU0dFa3pCczZyeVlOZ2JkQUFrSkNmRHBwNStLQVl5NEw5cCsxS2hSMktkUEgralNwUXYwN3QwYnVuYnRDc2NkZHh5Y2Z2cnBTRGN3RFM0NUJxc3BNakl5Z0c3c1lEQW9ZdkFJZXVwT25qeFpoUHpvdW82MzNISkxsQVlBRUh0eFBnM0tyVnUzb21FWUdBNkhFUkh4blhmZXdVR0RCbUgzN3QwaE96c2Jzck96b1d2WHJ0QzNiMTlZdUhDaDBGaUR3U0F1WHJ3WU8zYnNhR3RiVStUbDVjSEhIMytNaG1FSVRlK0ZGMTdBUG4zNlFMZHUzU0FyS3dzNmR1d0lYYnQyaFVHREJ1Rzc3NzVyMDN6V3JsM3JhR3VwUWxSbXpKZ3hvcDgrKyt3em0vRDFlcjNRdTNkditPS0xMM0R6NXMzWXJsMDcwY2Zra1gzbm5YZUUxa2h4aG5MWWlaTVFKa0hic1dOSFFFUnhyZUlSaHZMRCtLZWZmc0pJSklLQlFBQXZ2dmhpc2ZwRi9rMXFhaXE4Ly83NzR2NGtTa3BLOE9LTEwwYUtXWFNxVDAySU1YbnlaQXlIdzJMMVUwTkRBejc4OE1QWXAwOGY2TldyRjNUcDBnVzZkKzhPT1RrNWNQYlpaK05iYjcwbDZqVk5FNGNOR3lZc2pNYW1GcGcyZ2pxdzJyZHZEeSsvL0RLR3cyRzBMRXZFcURVME5HQmhZU0Z1M3J6WnRrNlpibzdTMGxKOCtPR0h4UUNMaDkvREdNVGdUVTVPanZvdFBabWZlKzQ1UkVSeDg5NTQ0NDBvYTM3cU9ja2FJNTFqY25JeS9PLy8vaStXbHBhS1FXUVlCbTdkdWhWWHJseUpYMzMxRmU3WXNVTUlJOU0wc2FhbUJoOTY2Q0diR2RYWUVqT1YvdjM3dy96NTgyMUwvQXpEd1B6OGZGeTJiQm11WHIwYTgvUHpSVXhnT0J6RzdkdTM0M1BQUFNjRW9XcUdPUWtVT3RmUm8wZUw2N0owNlZLazhCREM0L0ZBVmxZV2RPblNSWHdtL0g0L3ZQMzIyK0thUHZUUVExSENtRXhHZVgwNHpjdmw1T1FJWVdoWkZqcjFrMnBLeXRlSUhxemhjQmpQUC85OGxJK1ZqMHRQVDRkRml4YUpleFFSc2JDd0VDKzg4TUtZZmVaVWYxSlNFdHgyMjIxaUNhYTg3TE8wdEJTM2I5K09oWVdGR0FxRnhQOG9nSDdwMHFXWW01c2I4K0hBdERHY0JyVGY3NGZVMUZRWVBYbzBybGl4SXVvSkxHc3VpUHVDZ3FkTm00WkRodzRWUWJIeGtwR1JZUXU2VmpWRG1ZU0VCTnQ4Vm1WbEpWNXp6VFZpd01nclk5U2diWm0wdERRWU5td1l6cGt6QjNmdjNoMTFidFNXblR0MzRvTVBQb2pEaHc5SE9aNnN1YW1iU0JDTkdERUM1OCtmajJWbFpUYnRnZ1lZNGo1VDcvNzc3OGRUVGpsRkpLTlFweWJvczFPdVFyZmJEV1BIamhXQzlmMzMzeGNhdE95Z1VkdEd4eVFsSmNGNzc3MG5WcURjZDk5OUtDZW9jTHBmWklHWG01c0x0UDQ2SG1GSStIdys4UGw4c0hidFdxR2hYWExKSldLZVZFMHk0ZlY2SVNzckMxNSsrV1Z4SDVhVWxPRGxsMTl1VzJWRDlUazlMS252RWhJU1lNaVFJZmpjYzg5aFRVMk5FT1R5K201NldOZlYxZUdDQlF2d3ZQUE9pMXE5cEo3anNhWWR0dm16bGNNMTVIV2JBUHREQlRSTmc2RkRoK0pwcDUwR1BYcjBnTnJhV2lndUxvWmZmdmtGZnY3NVp5MFNpZGc4c2ZKNjRIaFNlSTBiTnc2cnE2c2hJU0VCL3ZuUGYycHlPaWJ5NEZJNWZyOGZzck96UWRkMThQdjlFQXdHb2F5c1RIZ2tBZmJkM09TWnBPL1VIY2hrRXpzNU9SbTZkKytPM2J0M0I4dXlZUHYyN2RxdVhic2dFQWlJNDlYWVB0bHIyTlQ1cVdFeExwY0wwdFBUSVM4dkQ3dDI3UXFSU0FTS2lvcTByVnUzZ21FWTRqaUtBWTFWaDl3bStYMjNidDNnOU5OUFIxM1hZZGV1WGZERER6OW9WRDhkcDRicHlHMDk5ZFJUc1V1WEx1Qnl1V0RMbGkzd3l5Ky9hR3E5RktJa3A4MmlPY2RMTDcwVTYrdnJJVFUxRmQ1ODgwME40L1NxK3YxKzZOeTVNeVFrSkVBNEhJYUtpZ3FvcmEyTnVmWWJZSi93UHVPTU16QTVPUmxDb1JCOC8vMzNXbTF0clFoZm9ycmxPTVBHK3RIcjlVS3ZYcjNnOU5OUEY0N0FvcUlpMkxwMUsvejY2NjlhYVdscDFDYnZORTdrZTVCcG96U2w2VGlaWmhTUEZzdFpFYTgzV2EyN3FmV2twQ1U0cmErTjlaU1dUVVE1amk1V08rU0pkL0pheTlyVmdTU3hKZFRVVDZxSjFWamJWQTB4VnZ0bDFHMGFuT3FWSFFTeDBvYkZ5bHJqVko2NmlxZ3huTXhLcCt1b0ptMlZQY1gwUDlreW9OK29ablpqNXlYM3YyeGlxK2EyV2s2c2U1VnBnemhOTU12ZVlrS2RlNkx2R2pPZjRpVldtdnRZWG1Lbi84czNybXhhQWRqWFZhdnRkVW9ORlV2Z09Ia29tMEllUFBLRHdtbS9Fdm05MHp3aC9WWmVIU0lQWXZrOUhTdjNyU293aUthdVg2eTVzRmdQczFobHhxclhxUzU2d01uT0g3VjlqVG5SbkI2UXNvTkRYYXNzbHl1M1EvWkFPNVduRW11dWttbmx5RGVNK3NTVEo2elY3NXNhS00wUmhFNDNtTk1OUmQ4NURUaW45ampWb1pZWFN6T2czOGo5NHlSb21rSjFGalQyM3VtaElzOFBPclUvMXB5YytwMHFTSnljUzNLOG8zcCtUdjNuZEsvRStuMHMxUDZQcFhVN2hYYzVYUXVucENKTk9idlUrMHJ0bDhZMGRxZjdzekZQUDhNd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdUQ3ZpL3dNdnRPVDdYeXgyZkFBQUFBQkpSVTVFcmtKZ2dnPT0iIGFsdD0iUiZhbXA7SiBHcm9vbWluZyIgY2xhc3M9ImxvZ28taW1nIj4KICA8L2Rpdj4KCiAgPGJ1dHRvbiBjbGFzcz0ib3B0IiBpZD0iYm9va0J0biI+CiAgICA8ZGl2IGNsYXNzPSJvcHQtaWNvbiI+PHN2ZyB3aWR0aD0iMzYiIGhlaWdodD0iMzYiIHZpZXdCb3g9IjAgMCAyNCAyNCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48cmVjdCB3aWR0aD0iMjQiIGhlaWdodD0iMjQiIHJ4PSI2IiBmaWxsPSJyZ2JhKDI1NSwyNTUsMjU1LC4wOCkiLz48cmVjdCB4PSI1IiB5PSI3IiB3aWR0aD0iMTQiIGhlaWdodD0iMTMiIHJ4PSIxLjUiIGZpbGw9Im5vbmUiIHN0cm9rZT0icmdiYSgyNTUsMjU1LDI1NSwuNTUpIiBzdHJva2Utd2lkdGg9IjEuNSIvPjxwYXRoIGQ9Ik04IDV2NE0xNiA1djRNNSAxMWgxNCIgc3Ryb2tlPSJyZ2JhKDI1NSwyNTUsMjU1LC41NSkiIHN0cm9rZS13aWR0aD0iMS41IiBzdHJva2UtbGluZWNhcD0icm91bmQiLz48Y2lyY2xlIGN4PSI4LjUiIGN5PSIxNSIgcj0iMSIgZmlsbD0icmdiYSgyNTUsMjU1LDI1NSwuNTUpIi8+PGNpcmNsZSBjeD0iMTIiIGN5PSIxNSIgcj0iMSIgZmlsbD0icmdiYSgyNTUsMjU1LDI1NSwuNTUpIi8+PGNpcmNsZSBjeD0iMTUuNSIgY3k9IjE1IiByPSIxIiBmaWxsPSJyZ2JhKDI1NSwyNTUsMjU1LC41NSkiLz48L3N2Zz48L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im9wdC10ZXh0Ij48ZGl2IGNsYXNzPSJvcHQtdGl0bGUgb3B0LXRpdGxlLWJvb2siIGRhdGEtaTE4bj0iYm9va19vbmxpbmUiPkJvb2sgT25saW5lPC9kaXY+PGRpdiBjbGFzcz0ib3B0LWhhbmRsZSBvcHQtaGFuZGxlLWJvb2siIGRhdGEtaTE4bj0iYm9va19mbG93Ij7Qn9C+0YDQvtC00LAg4oaSINCj0YHQu9GD0LPQsCDihpIg0JzQsNGB0YLQtdGAIOKGkiDQktGA0LXQvNGPPC9kaXY+PC9kaXY+CiAgICA8c3BhbiBjbGFzcz0ib3B0LWFycm93Ij7ihpI8L3NwYW4+CiAgPC9idXR0b24+CiAgPGRpdiBjbGFzcz0iZGl2aWRlciI+PHNwYW4gZGF0YS1pMThuPSJvcl9jb250YWN0Ij5vciBjb250YWN0IHVzPC9zcGFuPjwvZGl2PgogIDxhIGhyZWY9Imh0dHBzOi8vd3d3Lmluc3RhZ3JhbS5jb20vcmpfZ3Jvb21pbmc/aWdzaD1NV3htZEhOcWNYRmthbk52YlE9PSIgdGFyZ2V0PSJfYmxhbmsiIGNsYXNzPSJvcHQiPgogICAgPGRpdiBjbGFzcz0ib3B0LWljb24iPjxpbWcgc3JjPSJkYXRhOmltYWdlL3BuZztiYXNlNjQsaVZCT1J3MEtHZ29BQUFBTlNVaEVVZ0FBQUtBQUFBQ2dDQVlBQUFDTHoyY3RBQUNRMEVsRVFWUjRuT3k5ZWJ4dFYxVWxQTVpjZTU5ejczMzlTOThUQWdRU2tGNmtrUVFSYVZUQTBzUXEvRXBMUmRDeUtjdTJiT0FsWU9sWGpkV29XUGFXV2dva0tsWkpxeUFKSlFnb1NBQnAwNVAydmVSMXR6bm43TDNXSE44ZmM2MXpIeEFRRkJYOU9EOHVML2ZlYzgvWlorKzVaelBHbUhNQ1gzaDg0ZkdGeHhjZVgzaDg0ZkdGeHhjZVgzaDg0ZkdGeHhjZVgzaDg0ZkdGeHhjZVgzaDg0ZkdGeHovMUIvK2hEK0R6N01FRE9NQ0w4UUgrRlE3eVlueW5BT0F5WEtaUGVONG5mSDhGZ1N1VzMxMk5xOW4rNjJVNHlGTnhxaTdDUmJvU1YrcVQvL1lMai8rL1BuZ1ZMa3NIY0VrbkhEQkJmeTgzb3lBS3NxdHdWVHFBUzdvRE9HQi9IKy83K2ZyNC80MEhGTUNyY1prQmwrRXlYT1lFUDlrVG5ZZVY3eno2OUl0OG96L3JuUDZrM1dmMmUwNkY2MVNsZExhNzdSdEwzaTFaVDB0R0VLUWI2RjdFQW1ETVdaSmhjMkxwTG1jK2ZDKzM3cjVsY2MvTng3bSt2ckxhM2ZtYkY3eitlcndMVzU5OGJHSDhWK0RTQkZ6cVYrSksvN3MvSTU4ZmozL1NCbGk5aTEyQks4b25HdHdUVHJwdzE2WERRNTUwUVRuNXpFbWFmTmtNdVBoazdOeXhMNitlTmZGK2Rjb0pPaVk0Q1FOQUdBd2Rpa1VVSlF3Q1VlQWdEQkJRRktZa0VCSWhkOHd4WU1HQ2hRMGJoN0IxNTkxYzM1ckliaHg4Zk8rSGROdEg1bW56ZlZkdnZ2RjlKeDRiQVRnTzJCVzR4cTdBdFlYL2hNUDJQemtEREtPN3hsNkt0MlEvNGJwOXcvNHYzbjBxem5ycVEvTXBEejBwNzNqYTZkaHpidWM4TDNVOWVpYVlnQkhBZ2o3Q1dGd2lrSlJJdWNPOE9Bb0JGeUFYQkpDSmtpZ0pSbERlVXlCaElHQUd3V2dPS1J2cFRnbDl6NDRaWWFRTGpqam1tK3QzNHRqdG0rbm9oKy9PeDE0M2FQYVcvekg4d1ljQkxMMmdJRjZCUzlPVnVMYmduNWd4L2xNeFFCN0FKZWtLWFBOeG51NTc5bjcxRjUwNzd2L1NjOVBlUzA4cWEwK1lXSC9tbmpKRjV3a1pqbXdsNTBRVkY1U0xsVExTYzBFQjZCQUVvSU81ZDBZbEVsMEhtcWs0NkFCaEZFbDVGaDN5ekFJVTBrY0NwVENCY0hRZ0RHblN5VG9UbU1SazNxZUo0QVF5Sm9hT2hRVnpqVmprWWVNWVp2Y2VuTno3am84TU43LytYZDExcjNudjVvMEgyMmM2Z0FNZGdIOHlZZm9mdFFFZXdBRzdHQi9nNWJpNnRKOTk0NzZuWFB6UTRleW5uNDg5enpzTnV5NDZ1ZHU1bW9zanU4TU5RNUZUWStHWWM0S1hNTFFreDlxVTNaNFZycDY5RTlNejl2aksvdDFNdTFZeDNUWFJaTzhVYWNjVTFuZGtuMFNheVFRWFJCQUM0Q3JRVURSdURSaldSOHlQekxFNE1yZkY4WmxtZDI5ZzY2NFpGdmNzVk9ZRnZ1V1VRR09ITk8xa2xwVDZUaWFXWERUcFU0OHVHVGJLZ0R0MCtKNTFIWC83N2J6ajkvNTc5NzllaFNNNEJvUlh2QnlYMjlXNDJ2R1AyQ3Yrb3pUQXkzQlp1Z3BYYlJjU0oySFhqNVpuZjgzRjVkVC81eHp0ZS94cFpmZE95TEZBS2VvNExISk9QaFM2THl6RGpYc21XRGw5dDlZZWRocDJuSDZ5N1R4dmo2Wm43RWEzc29JMDZjT2tIUEw1U004dXowNWxnU01BanpBTUFrZ0VKSWlFSkZpaGtJdzJNWEZxU0gweTlDWW5VTEpybkEwWWptL2gyQzNybkIyWlkrT080emo2a2FQWU9qaERPZTQwVUpQSlJHbWxFeVpXYkRRUmFicFNPaXkwd00wOGROdnRmc2Z2WHBjKzhNby9YTC8yN1FCQUVGK0hyMHYvV0EzeEg1VUJIc0FCdXdKWHFCbmU5K3greG1QUHMvM2ZmdjY0NzJrWCtQNXo5bXFDR1h5WXMzZ3BKWTNEakJrak5VbGN1V0FYMXA1d3B2YmYveHpiZmNHcFRMdFhsV0RBUm1iZUd1RmJXU3BPZHdFdXlDa0NoTXRGVUdJVUd3NEtFQ2dvR1NYSUFMcERYaHhHVWc3SkJaSVVVWTBWUW1kSUhjRkp3bVJ0QWt1ZEZzUEl4ZVlNOTk1NEwrNzk2R0hlYzkxaGJCNmFPVWI2cE90cFUvUGVKa3BEbCttK1p0YmpEdDY5dUZsMy90SDc1emY4ek8rWDEvd0pBRC9CRU11bk80ZWZiNDkvRkFZWWhuY3hpY3NMQVB5Ny9jLzlpa2Ztcy83TkdYbnRHU2R6elRnV3lEaU15VzJjejFNdUMyR2F1T01CSjNQblk4L3pmUmVmcFpXejlvQkdhak5URzFsbFBsTFpZVXgwQUxTb05RWEszUUdBY0FnT2VkZ2JJWUlpcERodlJXb2xNU1VDcEFPaUNGSWs0bWUxcW9VZ1NISktrQXBJaDVDSU5FbnMxaWF3cVNHN1kvM1FGdTY5OFpqZmV0M2RQSExUTVhVRGJNZU9WYUVqVkZRTW5SRmRPcVJqT09nSC8vUTJ1L25uL3NmbTcvd3VnQ0xJcnNBVitNZVNJMzVlRzZBQUFsZFpNN3dmM1BIbFQ3MkU5L3VoTTNES1YrejFLZVlZUENjV1gyUU93OHljbzAzTzI2ZjlqejhmKzcvNGZLeWV0cGNZM012UnVmSldob3JUU0FyTjRFZ1d3SXZMSllUUkFNVVZaYTRRM2d5Q0JCQWtvdW9sQU1qaFRvU0ZoWUU2U0lTUEpPSGhBUmx2SmFuK3FZSGhzd0NaZ0VMSUNaZVFFcEJXZS9RN082aWpqdDZ4b1RzK2VKZmRmZDBSYkIyYVk1SjZZcHBBYWtEdXJPZWtLOTBDTitpT3Q3NDNmL2luZm52ckZhK0pjM2RWQWk3M3ozY0k1L1BXQUsvQ1pha1ZGOSs3K29USFhObzk1TWZPTGZ1ZmV6cDI0bGhaK05ETFMzYU84K1BRYnVQT3g1M0QwNTU4SVhiZC93eGhCTFUrTUIrZml3VU9HR1FNeXlsT2xDV1VBcFpxWk82b0lCN2NuUkRoZ3VDQkEyWUpCRUdSRGdsT2VxUi9sQ2duWUFCQUVvQWNBQXNKeGlsMmk5UVJJbVNBS1h5amgvR0N0S2k3Q1VoaDhEQkR2OUpyc3NPc2xLS0ROeDdoVGUrNVcwZHVtUkVMVjcrU25LU3ZZT3FkOWRON2ZSMDM2Y2JYLzNuM0Z6LzltbVBYdnBFQVhubkNlZng4Zkh6ZUdXQmNnZ01rcnZTSDdYbll2bjg5UE9qN3ZpaWQrY1BuYTMrL1VZWnhOQy9EcUg0YzExbjJHRS81Mm9mbzFNYytHQ3Y3OTFESFp4enUzWklQRHJORUE2VHNLcmxhUllHc2xMQ1dJc3BkaXRROWpGSXVoNkg5VElCWW9NQmFvdktRb2tZUklxeFNVZEFxVEF1Z1VVRDRPbGw0U2hoazhSTlFGQUFMZUZ1aUlsVERJSW9FSk5KQkVrWnp1U0RCRWpoWjY4VkVIRHUyeGR1dU82UTdQbkFZZVE3ZnRYTUtDU004cGFuMS9jSHVibjFnY2VNdi9aZk5YL2d4QVBmV05GV2ZqOTd3ODhvQVQvUjYvMm52Yy83bFk4ZXpYM0toOXQxdjVxTUdsT0pTdDFnYzEySzM0WlN2dUJpblArTVJuTzVheFhqTE1mbjZJbUlkQ1RpQlVxQUdHaGRVUXhPd2NFTnhTWEdwcXpkYy9sNk13aUs4RllCSTJRQTZrVWw1UlBCYWNqWjdERFJHTmZhYVNTNmlHRUZBVnROTFVESlFNRkVnV0dzYkFKVnlFWmF3RHNCRUYyUWtLYkI2YWFEckU3dHA1MXViQTI5NDN5RWN2T0c0a3BMU1NwZUthMWpEQkoybHlVZktSMi82b0gvZ1IzNTE4NVd2QkFJOStId3JVajR2RERDODNtVkdYRjB1UE9tY00zOXNlTkovZjBJNTUrdFdzN0RWNWNHWnVzWG1obzJyby9ZODV5RTQ0Mm1Qd2NycUd2S2hkWlQxQVJvREcyRVI0S0FYQ081VXFUbC9WaGlpQ3h4RWxBb3pPNGtpeVFVWFNCbThlcSthNXBFdWQwWEtacVZXdUJKQndsRnpPaG9GcVpYSHRQQ1V5aEdRWll6aUJvQVNBQkNtSk1ERDV4R0FNK3FjRk45S2tCZ0dHNVlLMVlxYUtFUXBMdXVJbGJXZUc1c0wzWGpkUVJ5K2ZjYXU3NVVta0xMeWxDdVRFVE44S0gvMFZiOHlmZmwzMzNiNHR0c1A0RUIzSmE3OHZHRlUvc0VOOEFBT1dLdllmbVRQTTc3dXllWGNuMzEwT2UzMHJiSTFEQk9ZWm5QYktzZTVkc2tEZWNhL2ZJSldUOThIditVNDg5R1phQllFN09ZSWpZVmVBQlpCRHRCRkx5NlZNRUF2bFRNYkJCVEFQVXduakZXc0tXR0VTMWZFS3dlQ2ZTTTl2Q0hpaWVHbENza0tSTXNsR0NoWjVIMlN3RXlBdFlBMnlHbmg0VXlOc0lPSVlKTnJvUTNRWlJITlBZeFRRSVZ6MEt3bVBMM2NWWXE0c2pPaG41cnV1WE1UTjF4M0dNTm13Y3Bxd29qaUsrT2s3UEdWNlFkeHcxMnZLMy8rYmE4Yi91RFZCc09MNEhibENYVGZaL2dnUHNlRyt3OXFnQzNrdm1EZm8vYzhNMS80VXhmbzFPODRiYkdLZFpzUEl2cGhmcFRqbVoyZitXKy9uQ2MvN0VLT2R4ekRlTStta3BQSWdCWUZHTE0wZHlrN2tRdVZ3MEdoaUNxQ0Y1ZHlWQXFsU0J6REFDdWczS3BacUFSekxKb2cwR3M5QUk4d0thbm1oZ1FRM3FwOXErYkV3V2F4NGR3Y2h2Q0c4QVNKOFJ3Uk1LUW9yQTBNMkp0UlR6TThKUUNBUm1mOU16SHdIcEpsV1pqTFNBSUd5YVhwTkxFQXV2VWpSM0QzeHpiUm1hbExJa1lNTzdDMmNpZ2Q5bmVWOS96a3kyYS8rbElBdytkRFNQNEhNOEEzNDVMdUtiZzJYN2J2d29kOXgvalkzLzZpZk5iRDFzdTg1RVRZTUhMbWg3am4yWS9VNmQvK2RPc1dRcjdoSG5ncFFIWnA3c1Npd01jQ0RRNFVGMGFIaXNJQU00amk4dW9ONVpFL2xTS21yRENRVWtzQUVONHFZaGNjQmxlRmw5RUNLeUhJNEl4THo2aW93eVlEVnBFWUJTMUZJSEpBRllCbUJJaFNpeERTbGlXQnlNRFVDWkFkRzhMVDBsTVFjZ3YzUjZRb1dJeFVDY0FQMWRVdS85TUZHakRkTWRIeFl3dDk5QU9IT2M2Y08xWjdqRjU4cWdrTnN2ZU83M256TCtMWHZ2bXUrZEZiRHVDUzdrcGNtei9ucnUwemZQeERHQ0JWcTl3ZjIvM1VyMzlXdWQ4dlBNeFAzbnVvYkk3b3VxNXNIWUdmQXA3Mi9WK3R2VTk0Q1BLTmg2Qjc1OFRvMEdLVTVobFlpRDRVWUN6UUtHaVVNQmJBUlJYSlI5Q0xoT0loaXlxUWlzTWRwQ055dzNyZDVZUzdxOFU1Q1pBWStGN0ZTTlFLVmJGVkpQQnFsWUlvV2hpdzZEVlVocnNxOVNWSXlDZ25WVEZJZzllSWF3dy9KZ0swbXVlUkxvWHJzNFkvMWx5U1M4eXcyUXU5Mlk0SkxpTkY3eWFFRy9DeEc5ZDU3TkFjZlI4d3owVGRZbFVyS3gvUUIrNTRiWG45ODY2WnYvM2Fhb1QvSUhuaDM2c0JIZ0RzQ2tSRSs2bmR6L3J4ci9ielgzcldzRktPcDNFYzVTdkQvRTVNdi96Qk92UEZYOC9wdkNEZmZDK1FCUndmb0kwRnRNakNQTk5IUUF1SGNvRkdGek9rTEhvdVFCWThrM0RCcTZHcG1PUzFuTTFPRmxTb0JHaFcwYTZucTZscEF1ZHI4ZFVyL2NlbEVScnpFcXVKbkpBMWZ4TXNnT3NNeXVMM0lOMXBpR0JPV2lEVXdlNlJNQm5kR0w5bnVGVm5KVmdBZ0laYU1VdHU5RnJZdDBjMS9Nb1lXbkRUWnVvNjhKNURNOXg5eHhhTVFHZk1SY283dEdQMVk3eGxmQnYrL1B0ZWZ2enFuN3NLVjZYTEsrRC85L240ZXpQQUE0QzlCSEJkZ3U2VmYvSGMvLzRsZHU2LzNyWGdzSkdjbGpQWDgxMXB6NHVlZzlPKzhhblFSKzZFYmpwQ2tiQ05CWFI4QWQvTTBDSkQ4d0psVmMvbjhLRUlXVkp4b2poOEJPUVdLSEoydUtJU2RoZmRLZk5DRklNS0FzSlROY0FvTGlRRVU2SW1VNDRvYmc3V3lpUXNNZjZNRGJJaEtqNjlMRUpLUEtrNkw3aEJZb1QxQ01BSklNTkFJUkFwWHQ0aTNRdkd4R3BCSE42eDFTcU8raklpUTFjVHJHSEFrQ1puQmNBRnVZdjloSnpQTSs2OGJVdGpkazhka1hQMk5hNWhadXY5cXhhdmVlbnZ6LzdQaS84aEt1Uy9Gd004Z0FQMkVsenAyb2M5cnh1Lzdxb25EbWQ5eFFiR25GTWlaaHRXOXM1eHlzdGVnQjJQZlNEeSsyNEJqOCtKSXd2NDVseGNqTkJHcG1ZWldoVDRva2laOU5HbDBhSFJ5ZXp1eGNFaUsxbENJZHhWdzdEZ0VjL2dEakRMSW5JaXFMaXFQV2dBWERqRENIVlJaVWhnMExhUkZ3SUE2Vkc5MW9oczhqQ1JTTitzNGk2bEZpeUdtajZhUU1qWkxBU1ExU1FReVJCNUlxb25EQ2RzTklVaDF5TFpDRm53MExSVy9iZ0RaS29GVDVEYmNvaFJCUUhzZ0ZLQXV3L09OSjhYbVJGREdiVlhxK01tMWxkZVU5NzhIMzVuOW9wL1Z6M2gzNXV5NXUvY0FBL2dnUDBFWHVLUDJIbjZ5VCtoSjc3NkNlT1pqenVPK1p6OVpEcHVIb1lldUtJemYrUDdNTjI5MDhwZjNRcVhaRWUzaUVNemxQa0MzQnFoelFLZlpXRGgwbGlva1NpajRHTVJzc0F4SkZNTTBKbWxRQ3FLVEYwaEx2QVNWVzhTNkI1S3ZnYXlxYkRXR2thS0tOWGhWWFFaY2tJR01zSmdSR3RHT3FuZ2UrVUVraTl4UVVodVVBdnBoSnZnVmJBZ3dFaXIrV01OeFdBVVBGSDVvb0hTbmdqQ1dyMFVPS0YzZEFsdTVoVUVqK05oUlFDUXRLUTlRcTBkSHJLU0wvY2NIcmkxbFlQZ0tUUHR3Rm9aZlp6OGFYN2JmLzY1eGEvODROK25KL3c3TmNCbDJBVjIvK0hLMTc3eFVqLzdzZmY0NXF4Zlhaa002M2VTVHpxSDUvejY5eW5OTXNwSDc2REdBcTV2UVVkbjFMMExZVDVRbXdPMDVkRGNvVVdFWEdXalo0ZkdJczhPS3hDeXJCU1hpbEN5MEhSUUxwTVlPWm1LdytaT0RhVmVuR0JMdkpRcWV4RjhDY2tJS2sxWEVLVUkzQ3p5cmlnbWNydkVOQUlXSWR1TTdFMktxMDEwaVdRbjd5aVhoYVRMQUtJTFBRTEQyekoxUkNMUW1XQ2tCN05DcitXR0F1UUd6QVNaaVF3UEhKNHlpaDBJamdURkc2QlJmUW8zQzBHd0ZJZDI5TmlBalkwczg5R3pGVTdMU2liTDlDL3daMWYrcC9WZnZ1THZ5d2ovemd6d0FHQXZCZjFNN05yL2N6dWU4dXFuanVjOC9uRFpITkowcFIrMmJzZmtHUS9HNmIvNi9iQmI3MlU1ZUZROFBxYzI1c0xHSERvMkI0NHRJdXh1RHRKYzFPRFFQTU1Id2JPZ3dhSFJVVExFNHBBN0pBTThnZG1JeFlneURNaUxMV2t4Q3NWWmRoUzNsVUowaG9KazBBUm1MazRuc0YyckhDY21KWVB0WDVYdDdFMHJuUWVySVpJRzdKd2dyYTBBSTFoS2tWWUZtS2tNaFdYaHNFSnBMQnlQeklIam83aHcrdkVCWlVQTVpSVEdVajB4WmNpQU9pb1o2SjFJV0o2Tnl1c2cwWXZvcEVraVYxZVFKbE9rcnBONk14bFVSTHBLRkNNTWV3TUFtUUUxTjQyOE1CUTJSSUlDSnhJdFNpQ25jM096YUhOenJwd0ZOOGVxSnRrd20xNWIvdXpBejI3OXhrcytvVEQ1TzBGcS9rNE1jRm50bnNhMTF4Ny9tamM4VTJjLzhlNjhNZVN1bTVUNW5kNC83U0k3L1ZmL0xYVERYZExoRFhBMlFJZG44STA1c0RFSE5oYkF4a2pmeUk1WnBnYlI1MFUrWkdodytpajQ0RUlXcEVRWG5Kc1pQdCtrSHp1TzFHWDRxVk5veHc3Mmp6OWIzWU5PSzlpNTE5SzVKNkc3OEdUYXlUdkJhUWRMazZES1lMS3VoNUxGdHgxQ0wxQlQvUlBPMVNlZXIvWjdmdEwzZ3NFaHp3NlVRTWE5Rk1rVjMxTlNkcFJGQVJjRmtITit5M0hNUG53djgvR1pSTmQ0OTNGc3ZQVmVqbmR0RU1XVjd4Q1lPbkxuR2pDZGlOT09QaGJJaW1qbW5nTEhVZlhRTWd0Y3lLdVVFWUNpbVU4ZU41V0dNZVA0ZW9ZWEdWQjgxZE5ZT0orK3hxODk4T3RiTDM5Sjg0UkNaWG8reDdieWQyR0FGc2dadTFldlBmdDFUeC9QZWVvUmJjMUsxNjNNNW5keStwUUg2L1JmLzE3WURYY2pIOTRFNXd2aCtBdzR1cURXWjlEbVFHd013RWFHYjQzU1BGTUx3QmVLaW5jczlFR0FrbndVeXRGMStQRjd3Vk1UdWd2UFpQZXNCNmw3d1BtWVBQbkJUSHQzQ1pNRWZQeWRTd2RvOTNVM3UwTUUzV3NLNVkyV2paZmdVak5WQTF3RTVpcURxVDg1b1ZhZ0dSVkl6Tko3Tk16T1dwbXhiYlN0d21tdkg4OHZnSThEODhFTkhMdjJObXhkZjFCSFgvY3hidDU2Rk9WdWFicGpON3I5cTNCRzMwdUVYMXV5Tk5YaWdnNnNqQXdybytqMTZCZlpzYkV4aHF3YjJkZDg2bHZZN1ArUHYvSDdYakY3MVgrOURKZE5yc2JWSS80UmVNQUdNdXNWSzgvOHZhL2xCVjl6ZU55WVl6cnBGcHQzR2g5L0xzLzg3UjhEYjdtTGZzKzZ1RG1YSDk4a2pzM0I0eU45WXdGc0R0RG1LRzA1c0JoWnRnb3dDRDRJR2taSkhYd2hsa01IVlNiSFlZODZSeXYvN0hHY1B1T3g3Qjl3eHZJRUNYRSs2Vm5CKzBhUjZJa0VqZFdDOEhIOXdpRXlxSUJnc0JxaVhBN1NyTklUQ0NhdUNoTGtybVFXbGFjMTQzRVlERjcvalJCT2J6M0VjbWV6TUFHd1d2OUVLMEFjZmJOTU1NblMwaURaam0zeitydHg4RFVmd3NIZitaRG03eit1WkxzdzJiY1RoV0J4dEx3d1NuUWxGSGl3Tm1RdDVxVVNoMHdYTUJzekZ2TVJuY3RIWksyV0ZSMU9oL3ZmdzJ1Zjk5cmpiM3o1Qy9DQy9wZndTeG1mejF6d1grRFIvV1B3cnZIbHEwLzU1YS94QzUrL1h1WkRTZGI1NG9qOG9idDUrbFV2WXJydENQM0lCclErRTQ3T2dmVXRhSDBPYkdacU5rSmJJM3lXeFZtbVpnVStkMms5MHhjQTBDUGZlemRvOXlCZC9uaE12L1VyMEQzaElWaDZzK3lRT3h2ellHYXNMRWNGUGF4NW8wcVoxUUwweENCYmdiOVdtSHExTE1CaHRxMlhxclFiNUpLUmRGR0Nnd3pWTk9DZ1ZVT1BoM3lKSER0Z29RaVVSSmRxOUdlalhlVG1nQnNBUmZ1bmd1V0plOGJBVkY5VWpudmU5R0hkK25OL2dZM1gzNEcrMjRkKy95Nk5lYVNMTlM5TUZCUmUwZXA5R1F5UEUyQVJyTGhqbnQzekVIWEhXRWJzNW81OGEzY2JYcnQ0M2RlK2J1dmFWLzlkY01lZk13TnN3b0tyOTMzNXQzM2xjTTR2RFFzZlpvWmU0NWJLMlF1Yzluc3ZaWDlzSk82NlZ6NGZnY016NnZqTXRURWpOa1pxN3ZEWklNMHpOQ3ZrVm9ibXhYMmV3VmxuNWVoYzVmaE4wSmVmeDUwditXYjBqNzhJUUZ4VlpaZWxUcDRxWE50aVp2d2ZCZmYyalFVNTY5WEdRcW5xa0Z1VmhDSzRZd3VySnQwZFpuSjNzL2hoTldDZ2FxYXFLcm9XcXVFdUc3ZlcyTm9Rb1lwZUZNMTBWUXdEUnhpYUJUV2lPREF3bkt1ckduMTlZMHJ0WGdLOERKbldKWVlHRmpqOHB1dDV3NC85Q1RiZXNlRTdUajhWQlU1SEdLRVUyT1VTZDZkWXFvQk1BbktJTlRnYlNuRGo3aW9vZVU5YVM5ZmpobU8vNWE5OTNIWHI3L3pvaS9GaSsxejJtM3hPRExBWjMwdjNQZTVKM3poZThJWlRGdE8wd2RGR2xUU2t1M2o2SzErc2xmMTd6VDl5aDVBTC9PZ20wdEVGeS9wTTJoeWd6UXd1SEw0MVVndVh6ek50cTZETVIxRlQraDEzWWR4NURLcy8rUTFhZWY1WEFRQTFGc0FJSnBPN2gwNEVGQXhSSmlZMmp5T0R4MFFEQkFDM0RMdVJkVlZ5TElTbTdiOGRDQ0RhSWhkVUdCWm9FUjNET0NOaUdvQmlqaFF2S0svVE9reU52d1BDTnJsZDFCZ2FVZ3c0YWhjVEFGSXV0UjZwK0J1S1ZobVZab0hOYnpzQTVQaWcxaG5LTE9PR24zb3pidjJQNzhiTzFkT2t0UW5kNDQwTElqSTBianRYNU4waGxJb3Z6Y2Vpb1Nna2FjakZXTVk5dG52bExYcm5uLytIWS8vdEVrRnp0Zy8wT1hqOHJTY3pIUURzTWx6bC8zTFhtU2Q5NVhEV1ZTY3ZKaXZydHBBczJYeThIZnV1L0JaTXpqb0Y1Zm83aE5rQUhWNG5EMi9DajIxS3h4ZkMrZ0xZV01BM1IyQldvTmtJYkk0cVc2TlFlbzQzM2lBOXVzZnV2L2l2V0huK1Z4SGpDT1VDZEFiYTl1R0xFYkdxYUxnSzNjUnFmSlJFZWdGTGtaZENlSWFQR2NoRkxJTG40SlpSSE1wT3l3NjZFMk9CY2tGeWdISXF1endES0lMSEFKaklyakpRY2tISlRyalR4c0FrNFFLeW9PeENLVUFwUkNuQVdPTDdvWVI2dXdoZUZPOWRYQ29leXU2QUU5V0tGL0xqcjNzUWRJRXRscUdBbmVGQkwza2FIdnE3WDgzNTZtSGtZeHRnU25BdnJXSlNxNTdxK0FmUjIrc0tFeU82U0FkQzlFUFpobS9NbjRCSFBmYjV1NS8zU3dSMUFBZlMzOVp1MnVOdjZ3SDVabHlTbm9KcnkydjJQUE1WVDUrZmR2bmhQQnRzT2ttTHJkczUvWll2NTBuZjlSeU03N2tlM01yZzBSbTFNUlBXRjlEeEFkb1lnRm1HNWtVYVFOOGFvWVdUVzROc1lSenUraERzK1UvUzJzLzhHOWpxaEJveTJLZGxEYnE4QndWNVZUbEZvdU9vU2hnS0VRRFRwSTluZnZKbjVuMzhmT2xkREUyWGlwckNiMWVwSG4yWEh3ZE5wUHA4MjY1czcvTzE3K1BubndUeGVBR1VQU0l2Z1pSU1ZFMUxocGkxNUVHamxDc1k3K3FtSGRadk9NenJ2dktWNHExbTNlNmR5T01JN3pzbzBkMUZyN3l5U3l3SURhUWdEUzRNSTFoVWlwbkxYYmFpdnJodDlxOGQvdmk3Lytmc0QzN3VxczlSczlQZnlnQ2JwdTgvN1huODkzL240a0gvZVQ3T3gyenNOQjRWSG5FbVQvMjVmNDN5NGR1RnpRVnhmRWF0ejRXTkFWcGZFTWRIYUt2bGZBNHRBTjhjb2NISnNSUHZ1bzdweDU2RHlVKzhNRXd0TzlGWkZTblZFKzh1c29yellJS1hnRGE2dExRSkIyQ0xoUzgrZERmTHJZZUljZ3crWnFSak01VGJOMW51M1FEdVdIY2NXMWpKR1JybjBBZ25TbHdVbUxOQXJtSXdFU1pYU1FTTW1TV0Fta3JIeWJxcXBVa2tFNFV1R0pLVWlLNkgwRHRUSjNhZGFSZUFVMWJSbjdzYjNSbTcwSzFOaUw2SFRkZGczUVJwNzBRckR6OER0anJaUHVHT0tMS01xSzBydGVzWTdoNHRwMTVkbVM4eXVtbkgyYzFIOFo0diswM2d6aW5UampVTXllRkdlUTNETHNxRE1xZEFGZ2tPYUN6Z3dvdEM0QU1VWk83TGErVmp1dGwvWDYvNTBqK2V2ZU9kbjR0OHNQdWIvcUVBTTd3bC84dTFCenp5cXhablhka1BlVGlXY3RjSkdIY1VuUHp2THBmZmZKQTZ2Z2xzRHVMNmd0eVl3OWNIWW5NUXRqSzFPVUtMTEcwNU5RY3hId1ZPNVhkOUdOMzNQZzNUbjNnaE1HUWhHWkZhd3NZbExHZFdjNjZjQlFPdFN5Q2djdGR4TGQ3K0x0TmZmTlFYYi93Zzh2cFJwbzl0Z2V1alJoUUtoaDZKUXBLUVFIUWtFb1NrR0dGZ2pMNjFLcHFDVlIyTElaeE5ZQ1dFVlN3djFSNzJ3a2dhQzRBTVlBQ0FLcDBpRkxNODRFQTAyMEhjZ2l1SHNjdEFlc1Z5dWhXalBXQ24rcFAzWS9lekw4REt3ODdscmtzdmdIVVIvVlE4dkRFSnQ2aDd2QXB2U2FOTk8rUkYxdXI5OXZLTFh2L1ArYTR2L1hXa2pRVHVub2dRRFZSbHhFa0FwZ29heGRsbFI2Q2t3TXdkRHBOaG5SczZIK2RPTHVFVFh2WkhlUHNUTDhZSEN2NldETW5mMUFOUzBVU2tOK3g0NWx1L1luYlNsOXlMcmRHN3ZzdkRRYXg5ejFkano1Yy9RdU1IYmlmR0VUdzJGemNIK3RaSWJDNms5UkhhY2lyZ0Z2aFdnUzlFcUlmZmRTZnN1V2RpN2ZmL1gyQXNZS3JBU2UyemlOSzA2dTljY0xtc1MzSEpYL3MyNXQrK1J2a3Q3NFhmdGc1aVZjUnVPanAwTm9INkpOQ29FaldIbzNhVmV6T0x5QnFqYUNaZ3FTb0E2djlDNVlMV0lDUVNWWUZLSU1GVENua1hMWWdIeVZ3R1p4UkxnZTVCemhqejRSNWlDS2VGR1ZTMWpWaEpucTNDckRtZ21RQWhQV1FYOS95TFIyTC8xejhLcXc4Nk9aNDdGbmlmeEZvMUdRd2xVRzRhb1RJVXBFbmlQVy8rTU43MzlOL0Q2dDV6TktZU290WUl3WlJDckZBa3VCTUNXVndvblh5UVVMSURjdlFxTUxkaEJkM0txLzFQWHZJTHMvOTE0RytySS93YkdXQ0wveiszNDdGWFBsOFBmSEVlNWtNeDY0WmhFN2pvWk94L3lmTlFycjhMT0w0ZzV3TzBPUUJiR1ppUDBNWUliQlZvbm9GNWtXK08xTXpCdVRGdnplV25Ic1BPZC93UHBsTjJSMWFUYkhtTXk2UkpBSElCK3Vpcm1QM1AvK1BsWjErSjh1NjdTZXlGWVM5c3VocXE0aW80Z0dMNlFhcndzemNjQk5FckNVVC9MMFJGTTlEeTkyZ0FiaDIzSWEvaTZUQmp1cE1XS3BmNEd6Y0NidkZMaHNDdkVJSlo5QmpUUXB3WVVTK0tZRGVxeWhoREowZ1BSWFFpTFFGTytOWWNZOTZrbnpScTUzTXV3SmsvK0pWY2ZmQnBLRGtETkxHMm9QZ3ljZlU0OE9Ld1B1bUdsMXpMMncrOEE2dG5udTU1eUNnd2xFaWI2UUlMYXZvY3R6VkJlYVkwTDI0cUhqSmJ1cTlwaWtOK1YzNzUrSHRmL0tieFhlOTdNV0JYZnZZTlRnRCtCbFh3QWNDK0hsZVhiOTEvM2tPK3pNNzgvajZYa3ExMFZDWW1BM1oveTFQaGR4eUIxdWZrNWd6YW5BTmJBekFiZ2MwQm1JM1NmQUFXR1Q0YmlDR0RZd2FjOHNXdFdQM0Y3MFU2Ylc5VWJCRjJ0VzErdFdFM2pBLzUrdHV4OGR4L2kvazMvMmZ3M1k2K2Z5RFM1Q1F3QVJvMnhYR0xLaU5ZaXROakNnWWRoTHRNUzJXcFRGNTUrdmdCYXpPNFVPRVgxQVlRVkRpWGpZQ0xFc2NVZ0RKVjJ6MWJ0M0FnYnZHbUJBTUtkcTlxVmJDU2VGWjExbEdxUndKQWlYUUhTcEdHZ2NvTFlEVmhzbTgvVnJkTzV1YXZYWStQZk1sLzA5MC9meTFTMTBXcUlLY2JDQWRwSHNvWkFrd0dMd1gzLzlFbllmTEZPNVh2UFVhcmdqREFxNDdSbFdxSU1VVkloc1FFc1k4R0EzaE1XN0pOelhWMk9tdmxzZE5ILzc4QzdHSmNkVjg4K2QrTkFWNk15eWdBejFpYyt4OGVzbGpic2E2NVlCMkg4YkNtejNvTXU3MXJ4TjNIWUVPV3p4Ynd6UVY4YXdDMlJtQXJRN09SV2hSZ25zbEZvUllaVWc4L2NodnMrVitLeWRNZkJ5d0dJQ1hneE1IaHFvTXg4aWowU2NNMWY2N05KLzRycFA5OWkwK21EeVZ0amN3emNCeWtObHhJY292T2RFcHQwcTE3Y0J4RmRTNEhnUkNwYW1rMEFhKzBiaVdHOUZtMWphN0ZaQW1Tb1Rad1ZwQ09hdjJWWVlUUnZRUVJBY1pGUjN1VTU1SE5pZ3E1S2tNejNaRHJoaUNLakdPSXluNFJmYVZyTzAvRHl1SVUzUEdkcjlaTjMvNXlJbG5jRHJXN0wxZ1VCTUJvWVIvc2toNzAzNStsaFIyRmxXMEtTRlpCYzRCSmFKZ3FDQ2dSNkFpbTZCNUEvVzNhMEdKOHVGLzRySy9aOGJUTHZ4NlhsNnR3MmQ4STB2dXMvdWdxWEphK0hsZVhuOW4xbUdjK3NaenkxYk95R0VVa1gyeElaKzdqNmxNZUJ2L1lZZEJsdnJFZ05rZHdsc0haSUcwTjBEd1RpMHpNTTN5UnBZVTdSNEdiTXd4N1pseDUwYmNHakdJdDdDNnZZL3huemxUZmMvNmF0MkQyck8vRDZzSFRhWlBUb2NXbW9FeHZoRlByUWtjMXBMQkdLanhVZTlFb251Rkw2VXB0TVZycVNiaDhjMVhVbzg0WDhvWTBSbmVrNEZ6cUVTSlpqWitxOGcrUlpDR1VyU0ZwYmFuRTBsUFdSM1I2eEN5dFFPeXF4NjJHSFQ0THlIbWc2Tml4K3p3Yy84WHJkTlB6ZmxsTndBKzRaRFc1clFDaUpZTm54LzR2dVovdC8vb0hZbmI0SHFWRVdFaDJZZld6QUI3eWhhWm9kS0FEMVhONTRtQWtGaHE0Unp2emwvaEREZ2hZK3l0Y2RGOFExK2ZVQUhrWkxwTE93OHBEc2ZvZnp5QzB3TndvUjlZR2R6em4wZUN3a0IvZmhCOWZsemEyd0VVbTV5TXhINmh4cElaQldnekNmRUZ0RldBeFF1cmdXeC9UeWpkZnFuVDJxWTZTZ2I2TGJpSkhtd1lRQlVuWG9iemwzWnBmL3VQczVnOWtUaE9NNHdhTlRxckkzQWs0clg3VmZrWVMwYkZPRmhoRTBoR2tpVWlVU0wvbE5CUWtaSVpSWmhJWnBBUEk0UzFSMityZzlUMEZ3aFd0dTFINVVsbU1wbVFrYlQ4SHloVk9MbVMwNlJIS2tETG9EcXFBS0tMSCt6aEd1bytoSjVna2FHSkFuOENVYXF1N2kzTDZ1SVdWUGFkaDQrVWZ4RTMvK2plUnVnUzQxeXFpanFDcE53Y2pQT3Y4SDdyRWZmVzRPQnRoWHRBVmVTb0ZuWHRNSFliWEdSR0ZxZ3FKRGtKMEtRdkZuWFRhSEl2OGlPN0NCejkvNzljY3VCSlgrdC9FQzM3R2YzQVZMalBpU3YvUHh5Lys1a2RpMzBQblBvNU1abm5jaEYxd0dpY1BQb2QrKzJGeUdLQ05HV3cyU011dkVaalZQSENlVWZNK2VpN1Uxb0M4YTdUcDkveHpHRUNQS1ZiUk1tRWVQckE0a0F6NUl4L0Q4SFUvd01uc25HakFLSU5NckMwNWNWR2lnYUxlelRXWGF6bWNTYWdudEJwTkpYYlJCc2Q0OVh3MTcyc3dzN2ZuaXBTSEFhdGUyT1hyaEJBcWxBcHEveUs0NFJMcUxMbWl6U00wMmdTUVlqcFJHSWhxNjdrN2pVQ1NJVzhkeDNqMERvN0g3OFI0L0E2TjY0ZVZzbURKZ0ZKa0Rtbyt3OXJhT1RqNlMzL0IyMzdxdFNGZXJSaExlTngydFlsU0NuWTk1QXc3K1NzZndISGpPRkl5SUZqaU9QNUFwdEc4SUZYYnJ5RDJNTEJsRGdhT3pKMFZsRWZsaDN6bnVYc2VkUDVsdUtveGs1OWJBMVQxZnVmdHdkNHY5cE4vZVBkb1BtZUczRFZpenBXblBsUzRkME9ZRGNEV2d0Z1lvYTJSbWczUTFnRE5SbUpyQkdhWkdBcVVYY3FqQUVOWkhJU2U4aERuK1dkVEpSTXBiZE9ubFlPUGlGb3dmdk9Qd2c1TnhMUUNsYkUrcllYYjFrZmo4a2oyMjUwc0JXdDhRakdEcURmaHFqR1JTL0t1R21KOTR6QnNxeExQaGdNdHE2SFFIbGRnQnJXcm1LbGRkVi9tZ1NBOGl1bjJNdEdXam9ZRTFkY241V0pLMG15TFczWXJKODg5bWFmKzNETngxcTljeGpOLzRibmMrOTBQdzdoMkdIbnpDSzJiQUY3aUZpeHo3bGc1UndldmZCTTIzbmNIVTU4b0Y0eFVoYTJhaW9jUWVNbzNQZ0xGMW1HS1lZVld6eDNoU0loeFh3bE52eHFlUElIcVlHaENuQTdrcHMvTFJYN09qbWZtUi80b1FWMFZCY25uMWdCUnZkK1A0NUV2ZUlUdlBXL21RelpqS25tTC9mbW5ZSHJPeWZTRHg2REZDR3dNd0d5ZzVpTzROUkx6REN5eXNCaUZISUpTakRtOGlvak13K2krNGRsVm8xRkJxUk1NWDE3QVBpSC82cXVndDkwQVRzK2g4aXlnd1NvNnJ4VnlEWjNiQmhTQnR1WlUwUlpVYzdyNk4zSUtaZmxhWUl3aHFzK3ByMSthY1lFTW82WUtpZXpSaVZrVWNFZjFkaEJjOVhsTHpIeFp3MWYwVUswRnZucnVadFFPcE1TeWRRUjZOSFhCbi80Z0wzalY5K0dVNzN5cTluM3I0N1QvaFUvRU9UOXpPZS8vN2gvQTVHdE94VEM3azViNmFQZHpCd3lZTGxaMDI0Ky9HaENZV0x1TTY0T2tFSGFwazU1NklTWVg3S1J2enFveU5rWm9Hclk3U0NqQlBGeGV2RkJoeHloTVdwZDBNYVhSUzNtY0xuamV4YWM4NElMTGNKbC9OdHVmUHBNbkVyamF6OTJ6Wjk5akYzdS9aOWRpVk5FaUlXY0R0ckRqaTg4WEQ2OERXek5nY3k1dExWeUxoVENmQTRzUkhFWnlHSUdjcVR3Q09RdGpuUW81MzRTZHVVdHJ6M3BTZ0xwbWFtcVVxT1FFTWlrZjNjRHNwYjhsczdOUnh2VTZNQ0RENG82MWFqUnFGemdGRjF0elBKbWh5SmhWRFFxR0FsUUVMR0RpckpwSDBoQ3pjMXZPWjhnZ1JxSG1oTWJjL3A3bTFYTW9QSWNwQXo2S3BRZzFWTWZQQmNyTlVNVFFwSVR6VklISkVmb1hwN3FrTW14S0QwbzQvL1UvZ2gyUHZyLzdrT1dMRVdVb0tFT1Jjc0hLL1U3V0JiLy92VnA1OWpsYUxPNkZkWVprRUhObXY3cVhXMy80ZnF5Ly9hUE9sT1JONWxLaElqT0RTbGEzT3NXKzV6NElpOWxSZEFaSHFibXRIQ3haU1FXR3dvU0NUbEtTYU81SVh0Z0xTbks2TTNYZWMxT2pMdURaYTArYlBlWjdDZXBpWFB3WmU4Ry8xZ0RmakVzU0FmMWdQdnViSG1Rclo4MDlGOEpNNHdKMjBtNzJwKzZuN3RrRWhrSnNEZVpiQzJJMmdQTXhGTXhqZGd5RnpCNzRYWGJRUldPUzU3dlFmZFVYMDNhdUFqbURCTTNNRzJWQUw0QUIrWGRlaCs3MlE2U3R3ZHdyZmhVVzJxcFVRd05LZzRLSVVsWGJvL2xxU2hTYXYrb1YyYnpoTmpZU3paRWxjTUlZTk1obzlTNnlSSUJHc3c2cG01QmRKN1BFWkluc0VwZ1NhRWFyNFJ2eTZqbXFadzRFTzhZZG9jMWFpT28yZWRCZFkzY0laLzdLdjJKLzhrN214VUFtQS91TzFpV21qa0JLVUk2RzQ3UCs0NytBSmx2T0xBZ2VhbFk2ZXUzQVhULy9wbW9FMWY4eVluN2MzOEZoN24vNlJmTFZCWmpGNVBKV05Ca1E1emdLZEtzaG1nWWh4V2RiYXNvbHA3TWtLeWdQd1huZmROcWUwKzUzR1M2dlZOTGYzZ0I1S2E1MW5JM1ZoMnZ2QzFkTDhnV3lnVURXWEpPTHpoRVhKWURtclVHWWpVcUxUTXhIK0R4RFk0RVdtVDVtYVhSZ2RLb1V1SXFLQk9lY2ZPd2pXb1NxSlZzTFp3Q1NRV01oZnUwUGtIZ3lxRVZGMjVZUWlDbllTcUdHV3ltcmhsclc1Vm1PTnNNS2dmS3pGaXpjenZzaTBVUzBaeEl5c2xTTG5RajlSSEtYeG5XVWNqY1c1V1pmakRkcE1kNk1lYjVWaTN5cmo0dmI0ZU5SSUM5QWt1d21ZT29EL1ZhcFhsSnF1V203Y2FLMVBZWmpsY1V4VHA5d0ZuWjg2WU9oWEdEVFNmanlhUE5ZcWdUWkdWUWNLeGVlaXAzUFBKOWxXQWNzeFNjWlI2eWtmVnovM3gvRzFrMEhZVjNYMHR0QWZvaEF1Z0hzZXVRWjdIY0lQZ3cwT09pMWRiNCtVaGpjOG54RjEzUVFockcrTEVqTFJPS1l0dnpDZk9hdVorQngzMFpBVitPcXp5Z01mMW94d2xWMWFPVEwxaTkrNWtPNDl1QjVtV2RBcVpRTTlZbmRXWHZoaHpkaVNORG1HUFRhSWt0REJrZEpvNGdTWXdvVWFFZms0dTd3TW9ldGRUNTl5aVBybFBybGdKOWFtRWJsVzk3MWZwUjMzWUNKblEvM2dVQlNiR2hETzZ2Vldrc3RYbG9uYi94YXlLenk0OW9zNVRYcnA4V2NWRytrV3hUTEpKQ21rYXFWb3dLT3NaUVI1ZFNUcEFlZURPNDlELzM5VDZlZHV3L0ZuVnJQOGxreC85amR3c0VqSEk5dUFSKytUWmpOa2JBTGhwT3BmZ2RJVllXc3c0VVlsQm9JY1J2TUJtR3VidS9wZ2ViSmc5T3pOaklwYXAybWg1WTdhT0Rrb2pPMTlRYzNvZU91b1BFZ1lBcWtkY2M5cjNxbnp2MityNEs3MDJ5cDRhY2dsZXhZMmJVVE81OTBwbWIvK3pEN25ic0pMN0xhVkZWcVg3NExWYUJBRmM4a2lDNEEwUkMweGgzRUVabEVOejdLejczOE4vYmhQMTUyNUxMaitBeUVDcC9XQUMrcjRPS2p0T2ZmbkN6WE9zY0lFMlh1L2YxUFFtY3ArZkVOc0FpY0Yyb3MwcGpCWElRaXN6cUJubkt3aVBKMnh4djZQT1BpcEFsNHltbFJtWkdJdWJiVkhIS2hrbUY4N1RXUk15VUFveW8rQjZSNm93cXhqbzJWeW9WSUN3NVVXc29zR3pYU0J0cDdoZityR3JQaVBreXJZQ2tjOHMwcVdNQWVmVGFtVDN5V1ZwNzlKUEFCRDdEK3ZKUGFxV2tHdnZUZTdkK1NYZVVEdDVzZnVvMkxWNzlIOHplL1YzN2RSMW13RTcyZG90SlBhR1dNNDFEY0RCTEJKSFJ3OU4xcUpVSUN3MGtnM0kxbTllWmFqdlJWQllXbk5mMElVNDVBWE5Saml1R2FEMEhmKzVXa01XVFBobWk2a2tkd21BSXJEOTJIMlIvY3FnNjdMTExvMnVjU1F3ZERaQU5VZ1hlVSs2NVNXMUtFRVk0aXNET21qSEYySVU1L3dHWCtGVjlMOE5jK2t4NlNUMm1BVndHSnVMSzhhUGZaanozVEoxODZxb2lBbFFnWnFiL2ZTY0xtQUN5cXVuY293T0RFa09ObmdiSEdRSkxjSVB4Z3UyQ2creWJzU3k0a2Q2MjR4Z3gycWFuVFk1WkVGNzdNUG5TRGdEWENNOWpvc29xcTFBMEt5NUtNUU16WWE1NmdJbmRzcFEzVllBaTFCaUtxQUtrRDFLbVVtem11RGVpZTgwU3N2T0I1NkovNGFLUStMcmdEcnV3ZjE4M1pablNvd1RFdXBOUWpmZEU1QXM3QnlsTWZ6MTF6eC9CLzM0MWp2L1E2REsvN01HM1RaT2wwb08rSWNRRFpVUktTRndnVGxPT0hTUklXb3ljUk0rV3FDRCtHWHFGTzh3Z2JYdDlnaHg2UVpER2JuOHBadmUzbTV0dHUxV0xqdUZaMjcyazBNMGpTa09UbUJzRDNQUHIrZGlTOWk4akIrVFY1YmJRQ0JueHFpREhhckxkQTljZUlvRStVa0lCaGJrTTYyVmY5WVRydkc2NEdmdk1xWE9SL1hTTDRLZVAwS2JpRUF2aUk3cFN2UHhjOU04cm8wV2xON3B3bzdkbWhjbXdMR0VmNFlvalpmVG1MdVNpS2pWSWw3b0s3UThXbHFwb0xpZms2ZVA2cFlTVWVVQWlzcHNkeUlTWDQ1Z3o1clI4R3NCUHl1aWVyZmltR05RZlNXZ0hUaHRxSFZZSXhqU1d5elNVSUV2a1hRd2hRZ0g0RnBheHo1dGNCMy9oWTdmN0xWMkxuNy93MFZ5NTlOSmdLeWpqSWM0NXhIUjNCTGdWUG5WS0lKVkpDU2gxa1JuVjlUUEV0VGg4ek5CUndhbGg1Mm1OMDJ0VXYwaWwvK1pPWWZzZWptZE5Od1B5Z3JKK0dGaGtPTHdXcFc4UDQ5cHMxdi9rT3NFc3hhS2ExSFp5NCt0TUJkRVNlRDl4OHpmdlZjWWVRTXdDdms1aEdzak5nTTZOODlGN1VreEh1MmhFNGFjMTRkajdpZFBoa2hMbGtkWUlUYThFUjZXTGtyVUNvSjAyaHZrNkE2RkhoSTd3aWpPQzhET1VDMy9la00wOCsvLzRNc2VxbnpRVS8xUy81WlRFMU01M2ovWE9FaklLY1NNRTFLcDI4aTh4T3pSYkNNSURqU09WTWpnRVQwRVdWRXBTUWwraWJpS3FQZ2FFQmpvVjg1eWsxb1NWTzBBSTBtaFNhYndJYkd5UW5JRWFvVGsxV2pYeXBHbElEbGV1WlZnT2thekxGNnVyUU9zMVJWUVBzcDlMNFlmakYxSTdYL2p4Mi9zWlBJejNvUEdrWW9Gd2liMG9kUVVOS3l4Ykx0Z01wWEFNY29waEN2Q3JBWWNsZ2ZRZE9Vb0IreFprWG1mMER6OFhKUC85dmVNcGJmd0w4aXBNeHpEOENTa2lKSUF1WXdIUWNQSHpncXFpUDVTaERqZ0hVOHZoSXhlSGpDSnJwOE0vL0VmeUdJMGpURlVFRjJ4K1VvZ0djTzdiKy9OWTQ2QmlaRmUwalprdEEzZElLakVtdVRGVVJsdFVLUFVVOERqd3dQR3lGb3dLdVNaUVNIVjM0REFHR0FaN1AxLzdKTnd5UHV4eFk3bXorN0F6d3FwcDMvcWZwdVU4K3QvVDNKd1pQaGtTUFdWL2QvbFhwMkJhWVI2SjZQSllNbGd3VWg1ZFJ0ZGlRcERvZXlCbkpJZ1FVR29UKzlMTnFLV2hDVEdyMFNxQ0JnUHoydzhJNFh5YkdSZ2VZMVFEbnlxZXg3ZGVJQWdNV2ZLK1c0dW42M0xnUVVPQ05KUEw0SHZiZjlVenNlT2VyTUgzbUpVVE85RktBeVVSTEJUWUFZMHZENndsUzNBU3E0OTFFaFFaUGhaSUNlNnZxcWtpZ0RHbVN5REhMRjlsWEhuTWhUbi9EVDJMUEx6d1A0K1I2WUZoSDZpYmdzT1dUN2hRdGZ2TTlPUFNTMzRaMVhVbVRycUdhaENoMkJwdjJPUHlLYTNEa1JhL0NhbjhhbFJlUjNwbElFMVBJVzJSSUdBL0dwbGN2VWMzR25NUnRkenJaczF2VDgvWkllUXRKWVh3bXVhR0FLdXdFVDVKaWRiZVlBQ1VVVUptQkUwcGRVSXNzUlZhZ3RLS0Vodzc3bncwZ1hZRXJtbXI2TXpmQVUzQUpDZUJDMi8zVTB6aXhRVjZpTjlxbFBxRmJtd0R6SWJxNWhrem1Bc3NPRlNkeUZqMFVGakVGc25oQUVFQnI0eEdnUW9CbjdpR0FBRHlBcUN4c3lZWlFOOXpCYmpiV0RwL0djcWdDeGJVNFJHanJSSytjYklGSHNpblNHVFBIbS82dU1GbUMrUnlPRDZuN2xSZXIvOW1mZ0sxT0daMTJIV2dwU3VKUUJUTFNiMnhmdENnRHFrRkdzdDZrVDRrSmlZa2g0SzlGVHgwRUdBQ2JBWDBISDhQUTk3endPVHp0ajE0S1BXQWRaWEVJTmxrbHlod3IzUmxZUC9BNjN2NVZMN1dOUC93TGxQVU41c1VDWlp4aDg4M3YwMjNmK3Q5dytIbS93UjJMczhLaXFxb25CbE9ycGpYUmVMQzRzWWJnamtGM3hHY3pwb1JTTXZ2ZFUwNHUzczNpSTFKSWhFQnRoMkRJazFXdFpGWHVMSGs5aHU2Yk1mY3IwdEFFcFMwTVpiK3RQUHBwdTc3b01kRkY5NmtOOEQ2TGtLZmlMVmtBVHVuU013QkhwcHVCN3FYQTlxeVNodWpsSFQybUVZd2VRNzl6eU5EbEh0UGxWYThSMnR3NkVuTFJDNnczOE16OTlXSUR0ZGNMRm1WYzNMVUhENE53UmxkTW5YdFMrZGFhUzBaT3gxYUtDQ0NkQmF4UVRLMVNxd3pFa3J3Y1o5bDlpUDF2dlF6VFoxOENqRG53eGk1VlgxYmRyUU1HcjNDeXhkaGJkN0dVS0VCcjRBZEt2SHJOTkpFU2FDUkpsMGN6U2MyOXFpZERuVVZwTFBNUjB5ZGVqRlBlL05NNCtQUVhxM3pnTHFiSktmSnh3ZFh1L2hoZmN4Q0hYdk16d09rN3BMUUNZQUcvYTJSWFZyRFczVS91WS9Oa3RSS0tEamVnUUE1MDZNVVBIbzBhS1JtOTVtdGdJSzY1T0pDQWZzK0VBMGJCUkt0ejRhS0JmdG00VG9pS0lqRUVyQ0ppa1BVSitXS2RUNFlDamFlWGZTdGZwSE9lK2NkNDd6dUFTd3k0OWo0VjA1L2tBUThBNWhEKy9mN1RMenJGK292ZFI3RjJ6UVBPYm1jUHpRY2lqMURKa3Vmd2JDcjBrQmNGczZBTXFKQW9qSEZBVlJsc0h0MFlWbUI3ZDBpSTVpSXhkc3VFSVlZRmN1Tnd2Slk1SW56bkdHcExGMWtBbHRwUDNsNC9HQUVrRjYwMlU0WUtsRzZpc1VpcmQ2RjcxY3N3ZWZZbHhEQUFYUUl0Um5hMHZLNjVyNWlNSVhMTTVKaWpDYXJ2YUpPZTdEdHgwcEdUbnBoMHRHa1BtL1N5WkhCUmVjaEw4SnZicDVxb2s4ODlRWnowS0lzUi9kbW40TFEvK2ltVUI1dkc0VERSVHlSdHFPOTNZTnFkcFpXNzkzSjZ4NFQ5N1h1d1ptZGlzcklmcmxsNDlOb253Q1pvTlZkSXpTS1JXUnc4UW93RjBZVVNlSmUxbExBaWZOWlA1WmczMVMyQVVFWERHNU1qU0FWZ0NZRUN4UlJOMGVnUVhYT3BrbjBla2FiclM0OEg4T1NuNFRLa0szRE5wNFJpUHNrRFhvcEw3RXBjNitlWFhVKzhuMCtuOER4UzZ0empJbmRyaVZqTVhXTWhTMWY3YnlzYzB1WkVzUVRoUTRQZ3krNGZ4WWdTeUFya0MzQmxFaHFRNWdERHlWUTVFR0I1cm9JUllDYXR6WkVvYUR2WUlxNmQ0TjZqUWdrd2tLd25FeVNLa0ZaUkZ1OUIvM1AvQWRNdmU0SXdqc0prRXMxdWpsYXFxUHFwMElGbEI2MEQreXFpdi9OMmxqZStSLzdCRHlIUE41Z2xXWFlaTzJwMUJkM3A1NkYveHFOazk3c2Z1OVVlQUtBeGgxY2s2cGdqcTFFZGRJUFk5L0JoWkhmV1hwNzZ4ei9oaDU3MEE4QnRNN0xySE9OQ0JFMHAxZE5YSk0ySjhRUzB5WjJraGViZndubFlPSElSeHJ6WVFDa1pDV21KRm9YMjFwZjBVTDl6RjRnQmxCbFI0bm9pNXNHcWF0c2lCQWJUR1VZV3JWVUNsQWc2QlpOWW9wZUFqdXdualRzZS91Zy92ZjlaQnQ0S0hERGNSd3ZuZlJqZ3BRNWNpek45NVpMa3hJSTV4ck5Md01UQWp1QVFFNC9wSmFaLzEzMGNGaDhNalJsVHZTZkNPRXpXRU00bVNLcFVRUE1TQk9obXFoTmJZQm90YW94bDlzOTZZbVBMUVNNSWFsb1dPR0R0TEVQTlV5Q2hYME9lZndEcGg3NEpreGRjVGg4R29Kc3NoeHE1bklqS01PQzh5amVucmxQWm1tUDJ5dDlqdWVyMUttLzVFTEFsRXAyRURneGdnZ1Zrd2FBUldiTi9SOXBEemtiM3RaZGk5WnYrT2JwelQ0cWtvV1RFRHEvb0NDOEFVdVhFdk91aHNXQnk5bWwyNm0vK3NBNTkyWlV5UHpma3RCRE1TOVQ4elV1citYVjVOUUlXT1JEVDVlcXBpc284dC9hQ2x1UklvRmpIRFVlZzZVOVpGWkdaMUFRZElPa29BTUt1Nmh1UzJ4S3pkcFpybjdaUk1CcUxEMFF5enl6amViWi83VUZicHozMVhiangxdy9nR3J2eVBocVhQaWtFVzFncFZ6SWZ0dzB5S0lESmxSUlpuWWVvUUZWWUVEUnF1M01hT1ZEemphQnJFR0VTQmpoa0JTbm1lQzhEMHpJN2NUU1VJekl3R0dXQ0o0KzBrbzdBemlTM3BZUmVvT0FtZ2U1T0YxaXI3aTdKNTNmSUhuMkIrcGU4aU1nNWdPZnRUMDRhV2JsTmVDa3lDM3h2L0ozZjU5YmpuNm54Vy80OThQcGIyVytkZzk3T1I5ZWZpNzQvVTVQdVZPdTcvZWk3a3pUdHo4YWt1ei83NFh6eXVrSDV4YS9BMW1NdTk4VlAvancwTG9DdUUwcHBSQ09TQmFpeHZBcDlnc2FNL3NrUDVZNS9mem1HOFNhdzZ3RVdMSXVlS09IVXBQTUJhUllGb3hLM2ZHdW9RaTMzVEhGdHFrMUtpTzZZdXZZSkFKUk8yVmx4bWJqeExFNit1TVQ4S051dVBwQlloL2tMalZ2eTBBc0dCRnNnT29VMTcvbEZPdk9oQUhBeFR0M0dNVCtWQWFvbUJmOSs3NmtQTzd1ek14d0x0eW9RZ1R0VFY0bVpPb0JGS2hWL0tnUTlnRjMzZWp5RlFpSE5LNlZSNnNsVWRGeDVScE1WTklLcHFrUForTTZpZ2dLdkk2OGN4cnB3T3ZMSktydDNDSmxneUtqY1BCaVAyaklPWmVPT1EreC85YWV0bTZaSUU1T2Q4TUVyUENOUVk5WGpIVG5PemN0ZndQazN2QWo5ZThISjlHS2t5VDR5TFVodEFYbEc1QVZSQmlobm9FVFhtc3FjNEFKSVU2YkorYkI3VHJIWmovMDZqanorTXVTM3ZSZHAwaE5qc1ZnY0o2TkZPNjk1OUtNZ0phZ1U3UG5oeTlBOTYwS05pNE5pZFBlUzFjODJoUTZodXRnaFdnNE1CVmIxamxHaHVneWlsemw4dmdDQUNvY0JTMGRVYndmYlBYR3ZDNUFqeGNsb2hVV1VXb1VLN0MvMGtpRWhnMUZLSVhGamtpczFnYThYT29aa2tQYVhIVThHWVA4Y3YzdWZlZURIR2VBMTlmdlR4c2xGK3p2YjRjeGVLL1BJS2FZR2N5SFE5aXlUVjdxNm9FNVhOQzJsVFlDWlI1VnF0Wk9XTHBrTFZtb2hZdUg0RkVHK0txU2k4UWVJeVVJWTZna05NL0VxOUF3RGM0R2lwYW9iTjhIb3FFc1VoRFFsRng5RitvNS9DWHY0UTl3WEE5UjFSQnViMWFJNDZjaFYrUHF1OStQNFk3NWF1dnFkNmxZZVJFMVhxWEpNOEVXNzQ5VUVxNjFZVXFWWVRhSEFOcy9Bc0FGWEFidUx4SGM3amp6dG03SDFoMzhzbS9hdVBBYUlVWUhlSU5BcTF4dWdQRS82NmU5VTNyRXVsa2g5NjU0bE41VEFReFVuS3RRYlZVZlBjQVR0K0F3TytJQXkxTTlidlM4YlYxU3Z2dTB5ZGFqTlR6R0hwTUZvbGNLalVxWGh6RnZDTDVvS1d4T1RRVXdlblllcVB5dHk3aHltWjU5MDBrazdHanYxYVEzd1Vsd21BTGhvdXZOQks3SXdQZFRLa29CMUpnK0dJOFREcXRUYXNwUm9WWWhxNVJ1RzBaaUxTRDRFbUpva3FCWVRnR3JMZGpRbWhIa2x5VHJVeWl3cGVrUXNRanBOa1BteXdTZ3FrMnJvZ2J4QXd6cDE4aTdaRDM5UEJJOWs5Y0pZSFJBWWVKOUtNZlpKNDUrOUc0dW5YbzdWRzRYSjVINUEzbEJ3ME9SU3BzOVltY2xsTWhWaTJDcDlxQzN0YXMwOVFGNUhuM1poWmY1QWJIN3REMlBqRmE5Rm12WkF5VFhCc05aNUVQb0lNNmdVVEI5OGR0cjV3a3R0OE50aHFWZGRiRng3dzlsdUFFQmUvWmlNVXAwdzFHN1lldGhqcnI3ZVZlb0pONHRSbWc2UTA1U2lpejRjUU9nWmEzK0xSS293NWpuRXRhMHQ4eUFZUFRnVkkwd2tUTUFRb2R1S3huSWFkK3kvcEZ6MFNBRzQ3RDZhbGo3aEJ4Y0ZzdVc2aEl3MUc2VVdERWlNaHU4U3l3RnEwbGFCczZDNXNPMmR0SFJzMWp4ZnkyUUZvSUFNM0l6TFkyZ3BOdXBkVk5tZFNscFc5eE9lenlCbnN5REp6UW1Ub3RCekZSU3A3NURMTGNMekx3Tk9Qb2tZUjZJdURnd2pyemhoOFJqemR0TWQ4R2UvRUpQakp5Tk5kbEhqOFZvWTFlTVdFTE5NVlhVMlZRckJldE9wNW1YTmphTTVHZExMZ0FSZ05WK0krVGY5S09kdmVRZXM3NmljNjZlMyttZXNSazVKanQzZi8vWDB2UmtvSmVhTTEvTzMzRGdTeUdpTkZ4NDYzQWliYURrZ3ZFRGp1RHpGY1Q1Wkcwemo3OUZIdTlZSjdaaEUvVjFDMnhxN2JGNUc1SCtOR0dob1Y2U21DZEVVV3UxaFBLbE1KLzBNRHdTQWkzRHcwM3RBdzVWK3lTWG9kdE5PRDRGQVNZRUJCV0RKVGtSZHNLdG9kU1JRb3EwUUlpM21TY204ZWFjS0RzY0FpdGEvRWV1SlJ0YmFPb2dqaEVOdCtYT1lGcnlnMFBvUnRBS2tRaVluTFpOV1lKWkI4em9pcVBaMEpLSHZBUnRuU0NldElYM1hONU9oS0FrUHpZckd4aldndyttbFlPczdmb0M2SndOcmEzQnRTYWxPRjlDMlJ5VkZzMUpIdnpnNnk3U0dSeVpmWXBMYkY2ckorZ0gzQWNVSysvSCtQUHJzNzliaS9SOUY2am9nQjM5VlhVb2twQ25CaS92MHpGTzE5ZzJQUi9aYmthelRVdFJLWjB4MjlocHlTOFg0eEVUSjZMQm9RVVhLZ3pocmNCUkRSbFhMbjBZdHhtUUZOU1FpUW1pVXY2aHE3Z2hhQ3FZcFZPSUZGblFjSWc5MEdsd0prZ1VSSlROaEo0akhwSDJQQllBcmNPbW5yb0lQMUFMazhSL2RkZis5MXAwUjNWWTFyM1dIOVNBVGFxSWFmYmpSNWw5QjRMQjRvbGFnRnFFS1Z0c1FXWGtibUlkYldBb0RtcjNWLzFaTWNTSHEyaldVK2pjdVdrd3dvVGxvWG1kSU9KR0trT3I3cGtKTUVqUy9YZnlhcDZJNzYyeGd6RUpJL1lPZ1F5Mmtpb09wdy9DenZ5Szk0Zi9LMWs0RDh4YnFiSW9XWWdQVXJVVk9GRWxlZlhJMXVGYmxRMnpxRWVOMnczdjBGd3YwRVV4VG54emJ6ZU12T0FCWHFkbU1MeGRTTnl5U2tlbjVybS85S3JqTnFWd1lCVjl0QTFYRlF4b2FXbE1jMVVaNDgyak1wNXdhYzN0Q2lFSXExSzhhb1drSmhsdzlZak8wU0wwbzBWd2lpMFVob2pvZEo5Q0tKSWxlMlJCVm5RWXpIYUxMMHlxSnFkTGo0dE5kOFVtVjhOSUFMNjc1R0JkMnppcTVEMFNXb3A0VkJlc3JEZWFsSXBJVlgxTHRIb3ZjTGpqWklCUURSb2ZUelZVTmlMVElaT08wVllwNHFSV0lKZWlwcmpjenVDVkVaS0VGRjZUa3pVQUljekY1SmVHZDFTQWxaS2lmV2Jyc09XRVdiYlp6dThnZWc5MW9TYnI3RU1wUHZReDlmejdNWjZBaFFGWnpzSHBVbUMrcndqckNwM3BkZ2VhMFdneFZ1SW1WUnFqZTRvVEtGSkI4aHI0L0hmeXo5MkwraXkrblRSS1EyNTRqMUg4VnN6RGNiWExSQTVFZWMzOUFtMlJLa0J4MTd6cURjNm1wU2NzSmZmc2FoeWZlUm5FRXllcGZOOFliQU5tNUdhTHB2dGFQVVdXcmpYUnd0dmE2bGlRUldGNnp3T0lDbjdTd0ZwWDROQVlJT3pSZHd3Rllhc1BmN3NzQTIrUFVOTjJSU0JTcnVqdEduY1VlZFpSaEpQeG9IaXpVUHlGa0NXOVhJWmVnaEJqQ3NmQVhDUzFoUUlPTHE4NERZc1dmVmVWNjhlNlJEUnBhQVJJdFIxYVZ6S2EyS1NpYUdaTFRUY0k0RTA3WkN6MzZFWEhubTFHVmd3SVUvUldsRUFZTXYvZ3JTSWVPazVNSmhBSkZMdG1nWHRSR0NkYTBJcFE1bGJGSGl6V2hCbkdyQ1ZUTTBxOHBBYlp2VUVJd0Iwclp3b3FkamNWLytVMzV4cHpzWXdWTXJlVnFxNHFnN09TMDQvUnhEMFRCdlI1VDl4M2Vjc0RLc3pjUFNMaHpPdyt0czdzS3ZKUWwzdTlvR2xGYndxOWdCNkJuZURDZ3RXVmkyM2dCTkRuV3NyVVZwcGdjdkEzWGhKRnVqL21RUmk4NEhTdjd2L2hYSG5abVZXcCtYQjY0Tk1ETDZyOWZxcDFuN1pWUVdCU3E5VEFrZElKUWpFdGVWaEJ6Tll6Z2VNRkNXSUhNcWVTR0xoT3BNSFdGVElXMEVVd2prRW9vQmtSdDc0RmlEYjlVRXdzcFFBb3hsVEE0UnM2SGxHRkpOUzkwSUkxQXlyRXhxQ2U1Y2EveDBzZXAyNy9mYkJ4SUJmQWJBNS9NM1Izc2UrSG9jZkpYZnhkY094M2tGcG1pdUlqUFZCanZFU21HbWNOU1J2RE1tU2Q4UVpZbHk0emp5eUJ6UUU4aGo2cXZrWW5JWGMwd1N0MGVsUnNPYXV0MWIzSWxrM0pvM2NMTEtoTFFPdmxvK3BXUDlJS3R1bkd3d0ZoWVcwVkJaQWhqNUp2UmoweERySTRINGp5M0xzSmdMTmphYnlLd0FLQ0tTKzAxc3FSY2k0ekkyOXRyUTRVbVIzd1ZDVzdKMi8xYWFqNVlZSjdwWHF4WFllR2luRmJTL2d1T3BRc0E0UEpQcUlSUCtDWk1NRXNQTVNDdzdLcHRCRjNzNG00Z1N5VEFMQzJzQWhGaUF4cEo0U0VZSGtsSXNaVWxwZTNuSVZVNEJjdVlVejJkUjdMZjBKbTY1N0hpaUtqdkZXY3RsWGd2SzJoaG5jbHBIU2xzQ2VlZkJ3dEpWZFEzN2tpeEhkUGFTUGp4dFg4TWZleVEwc3FLeENLa0FxWVNjMStzYmRQd2FGOEtjVU9JSzh3QmkwcWVrZStHaEtQZG5GYkRXYzJFYXZVdlFwSzVhQ0tSMldFWDh1KytwaGJDd1NsNmhBSnNleXJBSG5LK1lkTEJsQ3RlWDJwc2lLYXFDZ1lyWnRac2p3cHBlaDIxQ044MGtVNXpsVVlMZ3dpeFp3UlZyOFd0UXlpc092TGwyRGcwOVl2YUNtMHRJMWxsVXNJdFE2QVhNM2plcmM3V0Jwd0tmSElsL0VraGVDYWREYWlWWmJXbmtVMzlHNEdpRlJVVk1rRXowcVFvVE15aEZHbzZHQ2dyS3F3S2c0Qm14SkNOQnV1RHlwdzFvaXB5REJqQVZNRXNKUStEcTE1SkRkSUpvNmNhM0tNaXJreVF2dlJKU3lsQ0pGOTFBa3JOMXdUUTN2a09HWG9nNWZEd2pDUks5YVpTTmZaMlFRR1BZWUJXSlNKV1ZGTXF4QXpBTU5LbFVDNHVKT1BpMVp1MXdpVFVRaDMyTWw5N0hmTG1PcGpxbHZNQUFCQmNUWnl3L3VSVGtSNXlNb3B2UXF3Zk85bzdJODlqNDNCandVY0xuMEVhNjBUZ1BSU0RrSWhFQnFVR3NmVmExMXlYVmI0WWt4SVFSdGs4WW1RZDFhWVZPc1JhdktzNWsvaVJSU1hEcVF3WGFOL0tKOXJhZlJyZ2hOeFI4N1M0aytMVFJ1NVY5NWRhNUdKU1l6WXFNQnkvUXpTMkJVb1pYaUNGd3BFbVdoSmlPRjBSWTVobk5aS1FEMW5OUStKeUt3NHhJUWJYR1NML0N5T29NaTBQajV0RUprRW1GWTdRMldmR2xRempzOVpKYlFLWk9tak15Rzk2SzIzSEdvR1JOTUZTMlg1ZGkwSkRNZGt5Q292VWJpeXYzaGdBM1dtRnhuaU8wYU9yTzlDQWFBM2Nsb28xenlpZ0FIMEhISjF6ZU9jSDBDd2tjSkJxREluME1iTmJtOEllc0IvQXJPSW9wUllZTlU5dmplNW9lR3pjNk5YVlZHTUpaMkdvMHhHSXByc2tZMlZQZ09lMUVsNmkyVnJpZzZIQWlWcUtLWklsVmZGcVhFL0pDV2VxeDRNcUVWdFJ3bG5kamwxL2pRRUdDSTJPcXhGZUNtREZTQmU2REtaQ3NoQmRnU3k3UjBVYjJGOVhnQlQvclFpN29mZXp3TWNzaWVpS3FjL3lTUkY2bDJKb0xid2VSR3gyOU5pUFVsRlVBdTQyUW4yVjJsaHBCVTZVVytiUllKc0tZVVZLRG5ZTDY2ZWtWbGRya1dRUjM3M0tSVHlnRG0xdEFVZU9nQk1ETEQ0ZmtwTXBvQmVaUTZuUXVnSjBHZWh5NUxrUmtna0xtSW1HNEtiTjFmNHVJSnVXOTlaY0ZSSGFyWHBKUWJSZXdtSUQ0NGMrRkpmQXE2S2xUZXlyYURjQWRaUGRjb3lLbThxQnl2MkdoOTJtSm1LWFRvWWh4b01ZUjFoVEowU0JxK0tsR2k2aTdzMXkxOGdvd2dpMDJUa2V3NUlxS0cyeFFDZkdpNkRPMERHMlVXNVJha1llNjNRdmpBMHRHUk1CZXpnNUdRQ3UrQVJSd2dseXJDc0JBQ3NXMjUwTm90YzJoTXBpMU1BWXpLWFJ3UlNIbWlvWG9xQ3NKRVRCcXVVdGJUVnMxaHM4Uld4ZElxRnNjVXZOaGRjZUR6Q1Nlc1hTcWFxVE1RbHRJSnNoTU1DQWJjSVl0R3VuYkxwVzd6QWlkcGViSk4vdUxMcnJvRklleVg2bEZqalY3SjJJT0ZpNXFDanNGQi9mbHJkczNEME01U0hiT2lWcmluZ2FROFVUd20zQmFLR01SSjBBWW9HemRaakF0bzdWRnhVY3NaTnVhWkF0VlY3YkJXR29BTGNIRW9sdFVVVllLaG5vYWMzQlZPcVFTMjZETGdqY3J3MHpxaTRRcVRWeE1iUytUUXNZV29Ed3NWVXJITnFSaUZpc1dXYWxOYU50bzcxbVpHOEZTUjB5eWtsb29lMitEVEFlTzJVVzJTYXJqMWVnSkthNkFxQWxwU0dKWE00OVc5YnJibXlLTmRXckJGWGpxd0xIWlpOK25CbFhuY2JKSUJQcitEMVdZaFdBZjV6QnhtY1dVYmVueHZxczFuYms4cjZIZFJYS1ljd2hBTUZvRUlsZEl0aFlCNFladUxhcnpnMlVxRG9Gbnk2TEFGd1RYRlRhTUZMMkVITXk3cUlJbC9VRDFxc2pBRlpFSjVVSUtmeEhtNWtaVkNRRkswWWs2ZmhHdGJMWXQ2QmFMeEFHangzdDZxWVRPZ3FXa0F0QnFzU1NNdGl5T1NwdVlJc3N2cDd3NmtVanJDeVhJTFlFSGhCeXpWQkVVMEZkUWxJZmFwd3d5TWFoZTBWL0tyYlppcDdJR0dzYmZQMEFjZDNoR3ZmRTYxMzBxVHhnUEZJRlhwc3dVYUdmWXNOMTJrOUYwcnpKQm1ybW9kbzlWam1DcWladHBXNXpnSkhIVkhmSE1EU0ZHSUVmZDRlNGdKUU02Z1hrZGhKcXpSWDNYenk5WmIrRVVNSjAxVjZvQWRCV3d4dXJSSFoxU25VR3NkUUwxeHFtb3ZEVENmbVkyZ3RhOWNwZXM2TzRVOFNsL1c5clplcHNjalJFczdXWHg4VUs2L0NLcTdtYVdFRDEzRlZEcWhtc0FLTEViR2hZZ1VyMVZvaWl2bjcyU2tGN3UzbWpYbUFEeHR0SmJaN0tJa1RYdHczQ3NMazNMUUc3cWt4aWJBVjExUEVUeS93UmpYT3VuS1UxTXd4M1VDRkxJZE5YUHRIN2ZhSUJDZ0NLeGc0SndYWVlZUW14YmRHOEZZU28wZzgyS0NWT1EzUURvZGJHcmZBUHlpSHUrdGpUb2xpOTBEdW9WRDhkQVJMdVVaVXR3WHgzYUFWRUgrUFVJdmN4d1VNT1h0K2IwWmpFMEEyWnhNMU5ZWWlMYW95TUpUQjFSQkVPUUdzN2haVUp4Ukdpd1Z5TTk2aG9uTnFkQVlRYXZVNWJLMVZQcjdxOXVsSkFOZFRWYzJ6MS9OUlN3TU1WQ0FRc01qSjU3UnBBUmpkZHFiZG9uSTRUNG55bzN3R1UyVkd3ZG40WlNyMElGZ2FyUW91SUZmZHpSRTBYUlFzOE1GNU9JR253Umlpb3lsS3ozRFhRSkJZNDNFT1VHWndlSkkvaTBoMU0wWWtVZEdZa1RHVEZGa2d2d1VsbkdqSWdzR2VCcVdCVGl4MmZiSDczNFFHM3FGSVJnSkNFMjlLYUkwNjY2Z3FObWpjQmxjVUlnMlBOZUZzT0YyUGs2eUFKZXJqRkpLSDdCSGVIT08xZTVWZ0EwQmtWMUZ0Z2h6VTh4cFZkSmtlMUlxNVhTc21nK1RvNG44V3YwUUExUXpRc1Jmem4vdjNoVGIySWZYekNjTzRPRDVxWElOdjAybnF6Ulc1WTc1cjRQdFJBTFM5QmRXQlZXaFpuZ1JWTkM2L2kxWEVWMElpRUxkcnVOUUp0VFUwTitkWE92UjZYaGdXSnBLWmtxeXM4RVRtZkxWV0ZFWExhTGR4S3ZHV3R1ZHhESEpDQU56Wk5YYVdnbWx6dGhJcmNscUZ3S2M5YnZseTk3TXZRRkNja25tUlJDQVNTTUpqdWM4SGhpUVpZWDZ1TVZXL1RsSTZHVnU0VEFmcldVS1VsNkZrRHZVVTlFWmsyYXdDTDEwbzFRZ0hWUzdINSs3Q2lxTzdha3I4SU94N0FLbVNFMnZxM1NDamlnMVVQVUZQT2VOKysxZ3JqRUNmY25UaGhkMXhFQlNOV3A4cTdkcUkvdGs1UENhSzNqd08yMUNaUThhREh4RWEvc1RMWE5VSkYvSFVQcTZ2cFVBMFV0V0oxMUh6RXFzU3MzcUNoeFFIMm5WSXR6MXBPVmUyOThaT0ErUnhFV3FxUmE0cWpzdVNhV1JNdmdaRmtCOFMzakk2MU02VG1mNkdNcEJKQWFMVEdROFdwOUhwWlduUkZQZWt0SWtBTm9BNWJhNUNkcVk1QjhhcEtxTVdQMEh0ckR2cjR4NGs0SUFGZ1RNenhSL0grYmcwSWlDdXp4T0ZTQU1vd0FaMEhERU5KYVVuZ0I1RGJPZG1KaXVjSW5jQU93U2JFeDJQOXJLcDliOXRaSXlwUzJqbllDZWphK3paUXVtR004YnJvQkRlQkhlRTMzZGpTbU9Yb21XVXlWREtzNzJHWFBBbWFId2Q2aTNiUm1OS0lDaTZ6aWlzQzF6TUhLczVvTER4QmpTT3cxQVVicGVLRHdZODNTS2E5clpwYXU2cTNmVmpBZCsvQTVFc2VWU00zbDlkQkJPU2k5WjNLWWxTNStaQVRFOFJxNmNyU1ZBd3VUcVBIOE1nR1NsZGduQ2xMVlhrUTJWQndBRzV0cmdzVUdYQVVkQUU0eHozSHBuR2thbnF3YlhCTHl3UnFrVktsZHVFL0dPc1FSemNVdVR0Nll1dlRHMkFsZys5TUpkVVRXNUJxVDBjS2NJcWQwenFudW0xc0VKMkR5U3RXNXNZa1dYTEo0dmtJWEZCSW92b0M5QjQ3QWZzYXBBTVFDTWRUNWQ3TENpS1pGeHVsM29IT1lha0lYUUY2aDNXQzRyMkp2cGgzOFR2cm5CdzI0ZTk2QjZMb2kxaWd3TmpnYmczaEJoNzVDT1JoQyt4Y3hnSmFabXpjS2swQUY0WmZhVDVZSWN6cG5SRGorUU1MUlVzVHFzSEpDaXg1ZEJ5RVJBUk1tWUUxQ2tvazFSSGpKdTNVbmJKenp3c1BVRFdMTFFkVmRJTmlPSFNRL3I2YlFac0lxdmlqNHYxUjVWNmhTM1FhQTFFMXVnTVpNRjh1ZFl3ckhNT2lLaVFhenJsTEx1YUthUXAxWUpLcUl4UHE0S0VRUHppc3JqQmtOYjRJZkNCVjVWb29CQW83WklBWkNZNU9PaHJ2L3dGK0tnOElBRmk0RDBpRU9oazdOUzRzL0ZIZ2d0SEIyTUp4cWp1d0RLamlNSG9ZSDJCYURteG1vOUVJeEJBU01pMVZjSkZTa1JXTHI3NDdRZXdzaEFldzZ0M29NYjQ5T1pKSlpoNERqSk5nQ1RFTGI4MlFicm9wcmw0QVBVZ1dvQndOeXp3d1BmZTU0Q21uaU9PTVN1R0JySFBHOFlVT3FCcFExRXUybllLd0dSY3JmMjBLdmFRQlZ1Vm44ZnZ3aERIZVJTSmRpWVhzRXp3Zll2L2NaekpOSmpHNFBTNDNTUVN3NGhFdjlWY2ZSWm90WWx4dmF4aGlDWm9CcGVaRUFsRUV4S0R4Q2g0SFkxdHhSVlVvaXEwSnNFWkllYkVVVElhQzNDRlNwUkJiRHRnVzVteW4rTTJtWFhVZmZidDdBdXpjUGlXS1lTbTI4ZWs5NE5YeHo2cDRHQUFDTjBYenUzVkFYSWdNVkUrbXAvalpVc0FaMjA0RHM2dEdDWVpBSVlEOW1wNmtjTkxGYTdYZjhpS2VXTSswckRPZ0xIU0NKY0E3VnRFRDRDWjZwZWdZTkoza2cvelV2U2p2L3IvQTRYdUJ2bzlBVXRWamNzVll0WnlCazA4R24vcmwxT0Y3d0JVTFQwN0FxdGVQUGhRcFJBK0lPcWFyUm1pQ0owVmxIeXJOUmx1RzliUVdoR3FFU083MTNGR1dYVDZnckVMMkRaZEYwRXJibDZKeHE4c0srTFZ2VW9jRWRsVmpxQ29xWlZOZk4xR3NVSnNvRE5pbTUycjNkVWhsUGJDb0dEWFNnSUd3U2RiQ0ppaTkxb2FwS0p3cUxSZldWOSt6NXVwY21zbFN4d1NMR3dHbW1LU2FZZmVlWUdiM1lZRDFjY3JFUG9CVVFNb3N3ay9rTTNBZ2VSUVFTYXc1WUEweFhnV2pBcnRhc1pxRXpvbStFSlBJQWRrSm5CU2lLMlJYU0NnY0QxREwyZTM3cXAxL1RRVDJvWHhSSjFvWDNvL0pnZDZCSG1Fb0tReGRkR2h0QWp0Mk4vWE90eE1BV1VvZHkyMnhzQklWWlFDZzcvajJtZ2w1alBudUJHL2NjK2NJanRrSjgwWkgxbk1TeCtBcDlJSk1rcElDWG9zMkFTSjVHSTBwYW5VVHdReE1lK2o0cmVpZi9SUjBEMzhZa0F0bzI3NkZyTk44dXdRZlJ1cFAvMUtKZTZBeXF6N0x3d2dEUkdIbFhjSHEvUUxickxxOTBLRFZpeDJGZlZ2ckVMaGRWRkRlTmwxWHVFdk5DS3VWOGdTSU80NVNxRnNHRU5WdytDa0x2U0lUQ3hMY1RWSm14bnFaM1FFQWYvVXAxVEExQjl5YTRrTTV4UXN6eVZMbllGZml3T29GVVEweFFjQ0RyVXBsVndLSE5SYzZwN29DZEJKVEFYdW51dERzb1hOZzRnQVNHdGtrZ01XajBhZmVhR0JQOTJsOWJoZGZzc2dCMlRzd0VkUTUyVlgrdVJNNWlYbDdOaUhMcTE5VjkxSkNYcXBYOWJwWUlTVXhaM1dQZUF6eERkL2lmc3VkNG1TS1lGMGtkVTBSVXdBcnJzNWJFU1l0dzI4Vk1EU3BHVExFWEF1UEp1bFNWWEZuUUZsbUVvZUIyRkV3ZWNtUHhUVmY0cnBMUjRJYWtuMTg1N3VrZDMrRW1Pd0FGVkt2K3RnR3dWVGJZaU5hV05Yd3llR2daM0ljb3hhR3FrSUVOV1RGVzJHb3hVWTFMR3p2VXlkWWEyN1dZZ1BpOW02VzJ2NGEvMjBKUlNHVUtPZ3dva2RHUjhlQUFSOG94ejk5RVhKMTlZMGZQanBiM3dSaWlHR0UxQUMzY2syU1E4OVc4N0x3QWkwa3dRQWtKMUt3SFV4U0NEZ1ZFRVlTTExIbGdFRmliNmVZU0hWQ2FsQnJnQ1hSdW5EeWJOcXNEakVRUHRJQVdJcXdCbFBOdndENENKNnhCL2JtMTlQdlBTeE9laTNWeWRhZ0hnQm1aQ25nai84NGVlYVo0RDMzZ0t0ZGVKaGxyb3BRY2JLbEVhRzZXWDVWTUJBV0tFazdUcVRxcVNDWUxWc1dpRW1QY3MrSGdaZitNUENnQnhqSEhPMmliSlJONEJmR1NEbUhYL3N0SkV4b3FiVkZ4czY3R000VVlkRENqZFdRNjh2RXVvMTlWK3BhZVIxR1NiTnFYT0ZQaDBVTXBheWdBd0V1SnlGb1dlVTJuQzRnSUN4ZFlmdXBBcHNJT21ZaUlLblFCTTRzNHk3bUdRQmM4NmtNc0QxdW5RK0xCUlFVQW1QZmNqM3lnTVJyODZkaWFVU2doSFZVa2d4cUZ5YnlwOGltUFZXWUpnRklvcVdvd0lybmlrZlYrN09kT3RWdTQ4aGlvUTVnVjBOc2NubUhDc25VUktQbVlxbzVGazNVcmhYWS9FN1ovL28xQUdDVDROY3VZMVlGVG9nYjlwOEN2UEpWVWtuQ2JBWk0rNnBpQWRWSnkzd3dabFJVM0I5U0VyeUxBa2d0RjAyMTdxbjVCYnNLeVZEQ3lvcnl4ejVNL090dnhQUjd2d2NZUjhFU28waW80amtoZE9KOXgvRzIyNkRmZTZOc2VocWdtVUtsNUdpcHVhRm9PL3lpZXFOUXlFVCtVbUNkd0dsM1F1RUFzTzJucUFhZ25CR2h6Q3VlSGpCTEVEbVZSYW5zc0MxZHRDb2NYbE9BU0ZpOWVrVjBxR044cWU0b3krWWRLN3dSQUM3OWhERnRTd1A4cXlXb295UFp4Z0dwSkhTRk5uV3hFMklNV3daNmovRFUxVVM5RTZ4clZhOHZjekZVREJDZGt4MXFtQUxhbkJmMm9GeGN6bXRnQzBNdER0VCthd0xvQ2pRUjBBdnNTWFlPZFVJb29EME12bmVoYzRWd0ZVQWV3ZlAzczd6aUYraEhEa2REajBmWG1tMm5USURGNGhjKzlHR0dxNjZHRG0vSU5qYWxMc0hnU2tsZ0FrbUpYWUYxUmRhSjdMM2hvSkg3bW9CVVFxbmRaY2h5ZkNaNnBDK1RucmpwcjhpdmZSYTYvL3BmWTE2TEpZQkJLWjk0UmR3bEozMyswcCtHSFJjeFRSVjJyOUdJcGFYTUZ1cnJBcU83MWZBWU5ZYkhwSVkrd1haTldVOHNseGU4Qm1VQXNsRXEwZFZ6SXVVZkpibFFOU2tPSXF0YWRoaHF0R0dHb1M0eFFnQVFKNEVtbFFTem81d2YvSXN6OGtjSjRNcnRGUC9qRGZDS1drZjMvZGFITmxGdXdkVEpyaWhOUmVzTFBCVVVDVFp4c2xOUVpIMFlSY3ZQMkVYVlR3dkRYRmEvQmpBdHc3Y2pnWmhFcGNSMlFNR0x5YXNFaVFEWVc5VEpQZUpyVXJHLzNtRmRmWTlPckFCMWZOVmpNYnF3ZXcxcDR4YmdsLytidzB3b1JhaHBNME5KRldIRmtqaG04UWxQSmw3MU9wUmpCVHg0R0w3YUE1MGtLOVViRjZrdlZGZWdGUHBBczVvdlVXQ1hoUlJyaDVBRUtFZGJnU1hncHV2ZzMvaTE2Ri94djlSUEpuVmVWWVZIMUJxOENCOHowSFZZdk9zOTlOLzRYZG5PczRXeVdGSmVSQ0JiYmEwbEt5Nklpa2x5MlRCV0JJakpDRnRiaWJ5dmZtZ1BzbmNacWpWbUFMa0NzOXZBZHZObTFUUnNHNEFHWXFsUE5YaTA2QzJVR3BSNnR2VTd4S0cwMk1MMTF5K3M4UXYzWllBRTVBZGdWOTZKclRzVytWaWc4bFlIUFVva1VlYVJoSU0xTjR2UlMwdE1MSXpPV2ZWK1VZTlhuSEFKUVhRdzZ5ajJrQzAyS3RtRkdEQlhrN2pHNDZHdTZWN21tRWxncjhBQnQ4T3l6QlJ1cllWNTgxamx1QmpBQjV3a3Z2eG5pWnMrU3ZZVElXZFlKUmRSRVE4endMdU96Qmw4N0plUWIvd1Q2SDRYUVIrNkdiQU0yOUdEWFIxUVlnQXE3bGl4dnNBTlU0a3lzK2FqMWhrNW5VTEhqc0FQM1FLOTZFZlYvOFp2eE91VXdyYVVteUhXUGpFOEJ2VHlBeitPeWJpTFRORkRaR0hzTFVTaTluOUVJY1RBSTJNK3p4S3JBMUhDWEtlVFdzaEdoVjNsL25YOEc0ajFPVHZFb0tGVVBSd2IweUZaSUhLbEdteURkK0xDQlI3ZzlUWVNWWUhxU1FXcUJhRkg5MEVBeUhqeEo2VjhILytERDhTSFQwa2ZCaG5Pd2dEcmd1TXVpeUpuaEtSMlQzQlpFTlFhdmF1QWRFQVRGZmRUMi9zVXRGWVhOMWhaekxjVjBYQ3loWWhHaGZRV2YyODEvQVlHSjRVQlNsMzhzWGNnRTdkelFqTkZudXFDOWVDdU9mVGlPaC9HUzQwd2FLY3p1c01JSVNVd0Q4S0RMd0xlL0diNGo3d1U1WmpvdDk1R2xCbTRhc0NPQ1d5YWdONmdTYUo2RTNwS3ZRRXJuYmh6QlZwTnlMTU5sZHR1UkhuRUE0RTN2aHJkUzE0QzVoRnkwbEtTc2FWKzJpYTF4Z3gySGZKLy9GbmFOWDhPN2owRDhrVmdHVlpiQVZxTEt5SFZkZ1NFSVVBb2FuM0x3V2w0RkdXQk1hcnFSZG5PZVpOWDVvMEJkWmVVQWxxSk9GeWJydFR5bGFybkVldjZzNnAwVkJ1T0ZMTHp3bW9DNk1HeWdIQzdiNzBWQUs3Qk5aL2VBSzg1V1BuZ01yN1p1NHl1ZDdGM2RWT0hKYWU4aEVLN2k3eVFMZnhGK0RXa2dCNmlFZzRkR2xOd3YwcE9kZzRQSHBmcU03VjFaQnRSYjBvU1ZFMEpBS3l0QWF1QVZpcTJHR0UyOExXa2VLK0pFelVjbzlGem5ZZ2V3SlJBR2Vobm5VSmM5M3FXbjcxQ2VUSUZ4ckhORjZvcTZzWjBFK282c2hSU2h2VDlQd3o5OGR2aEwzeXhTam9OdXYwb2VNTXQwcjNIcU5rR3NOaU1yL2s2c0hrY2Z0ZWRMQi81YUt3dk8rODg4TmQrRWYyYjNvejBoQytGalRudTVPYnRBb29EVzl2Y21NRyswL2huYjhmNDBwK1U3WDBRNVBQZ2JxUFVyZ3hNK0pWYUFRY2VGNWhiVk9nV0d6Nk5MbFpJcFBaK1JkZFhMUjNxZVE5UGVXeW1nalpJeFJIajN3STNEK1ZkRGQ1UjlEQTJsQzZsK1EwYmpKSUVSVHN3b3EvVS9rYTM4UGQxeDY4SGdKKy9qeG1CSHlmSE9uUnR2TkpmTGNaYkg0bXN2UjI3Z0d3ZGxvazhFbG9VY0kyUmZvVHFNZkszSlNvVi96SlZYS0hSVnFGVENvdFBFQ2FGbkI5cEIwUlVkQWxPUlU4VHBKMDd3R21LZ2llSENDTENUNHF5dk5LRVFrUkRiNnFwQ2syUndlNXBtSXNQT1kzNHRaK2lQL3dKc0NkL2hUaU14S1J2a0FJcXBCOVFLcU5BeGppYW5YNjI4RU0vSW56L0QxSHZlQ3Z3em5kQmIvOFQrUFVmZ1NsQnBOeEVUUnpkRjErcTh2Z3ZWWHI4RTVrZThmQVRHS3dDNzFJdzBvcEJINlJvaWhrVExIRXNmdjFOR0w3NmVlaks2Y0NLd0xFMDhWVjhUSVpLbml3MXdEV05tRXNXS1dBVjFsWnhkSUhaU0thdTN1Vmh0cUdjOGFxc2cremVqWmo5SENLbG1tdXk2bW1wU3BsVVNVV3VaMHBOaGF0V2VMVGZyN0xVUUplbWQyaDJ6NGRPdnZPdHVCVzRHbGQva2lMbTR3endzcG9hM1RNdS92eklzSGJYM3JWMGhrcVJKVElsb0dSdzNCRDZrd3lSZ1ZZMkpOejNzbzhnVkN4eGxnTzhaYkFuQW1RaFBtT1hrYmFPQktidmxiQkhOWmhLeDNIWGJzaFNuT2MrMktBUXRhS21KMEp0WEtqYUg4VzFDbUZXeGRXMHhIZTcrKzhpLyszWFU3LzZ4L0pIUEVZMmprVGZSNjlpSkYvYlUwc0JxdTlsN3ZCY3FLNFhuL0JrNkFsUHB2QnZGWU1qUm5BczFsdFNtZllRd0w3ZEUrNmgzTzhNVUlMUjFlakFKZHRCMElZUm1QUXE5eHhoL21mZndQNVlMKzdiQmZrY3NoZ2pXMVVyOVdLN0RJbG1vQ28veWdvdnhhWW9XMVpZUkphbUsyTHE0bktSYlZGVjlhbGhsT1BzS0F3cHZHVnpDRkE3V0FMYlVIbUZlK3FIcEJveUdPWXVkQkJYYWhVekpkTWROcnZyVDI4OWRxenVLZmhFKy92NEVFeEVBOGlWdCtEb29iRWNYT29DSS8rU0tKVjUzUWFWR0JQcElpa0hMU2EvVzZwWElZR2hla0VvWmlpeDF1d0IyanA4ZGl4T2JMMTM2dEUwZ0JUWWR4b3dYYWx5THdBOW9iN1JaWXJjcHBNQ3I0dThzT1dJU0lBbnlaUFgzTEhBVm5ycG5FSDh0cWZUM3Y0V291L0JuT1ArWVVVZktwaXRnTDdDUmZWZGFDcEtCc2RCcWNRZVkzUWRzVHFScG4xTWpzMFpHa2ZDUzNpUGxMWkRRb01ERXRBNjBqQU0wS1FuYnIwTi92VG53RDUwcDJ6ZnFWQlpoSmlWY1E5WDlpV3VkNGdiNHZ0d2lwR1h0MW1NVnNFUUF3cEdwRk5PQmJxdVVXWHRzOVVYcmdERXNCbDhRNHkvUllWYUd1WVgrc1FsNDFIelE5VFprSERWS2FweXVLYVFWa0lJNFNNZG15cHZCT0J2d3BNNzRKTXQ4Qk9UUXBZbmgxZk1MRzlBbDlGTnZNZ2NhZXEwcWFLRFllN0NxaXZHcGJYcVYwU0hhaGlSRzdJTGVDTFVLNVUvVHJYWm00Qm1XOHRiRzZqNEZ5cjFEQUQ3emdFNENScXJaNlVDQVhZTzZ5dEgzQVVQV3h1S3FTUmFWNFdwQ1VRSG9CY3hJWW9LYlBjTzhMeUY5TjNQVXZtamw2TjBIZWdTU3E1Tmk1WCtyT2M3bkZDY0thVkU5Qk1veFdKRk50eFgwWi9HenNDK2QwY0NHOFpUSDRvV1IxaFZPcFZoRUNZVDVQZTlYN09uUEJYODZLM2c2YWNEMkNKYkcrbzJrMVJWT0E0ek1WVGhsWCtuYXJ0bmhBOGpBaDVLRWpFWExqNHZvbmdUSGtnTUVqbkFkQWZnODNuVVo1NWJnbHJWQmEzNDhBYTVvSTVMbzlER2RnaUFtT0Ywak5qSktnMUQwUVpHdktmYy9TSGdreG1RVDJXQXVxTCt4MGZuNDlzSFFFaUtob21weTNxUkhUaHVsaWdxbG93SHFsckZpVTZRMVMxMkFaM0VLSW9POVlTaU1pVUExbU4yY1kyU1lKM2Ywc2JmYUxwTFRKWC9iUFJXOGtnY09nRmRzQ0JLdGVHOXZXNFlmTVVvd2JaNW1aTTZSbW5YRHZEQlUvTEYzd0Q4N0JWd0NraGR6SHVPMHg2RHJac2tuQ3h0NFN1VzRxZHRXSmRrck9yeUJ1ODRjS0wxeGFrRkpQazR5cm9FVENjb3YvVnkrSmMvQSttWWdGTk9BOHVNcWdJUVdFeVVxMkxZWmVNNzZGSFV4bzB0cTFockc5WkVWbnlWQkxGSm5IOXFSTXZTdUY0QUh0TkoxQms5QTdyanVJaEU4eFBIZWtUdnIxQU04THJUTDR5d1ZzcGdxOGNobElCZnNJb0NxY0RBeVcxYVg5emVMZjRrUHYrMW41VC8zWmNCNG9wcm80UGxubnNuLy9mT1FjZlFXWEpTRmxKM0lRRjVTMENtMkxOQk1HMHVqQ29uUzB1UVV2Qy9kZlM2d21naDBCMFRnQnZIQ1FCbVZOek1ibXhjc0FEc1hxTk9PaW5PUlJnVHREUkVocWZ0QVhZTjRvSFlRZWlpMmlNaEpDcFdnSVdFQ3gxQUQvNDFQZXhVMk85Y0NYM1QwK0FmZkMvVTlRcFFNQnM4UXdvQnUwZWJSTnZDc2pTbmxtcXAvcFNWbUtoUFZnUFVBUWRMaVpYakt6M3p3YnVRWC9pZDRBdGZ5SDUxRDIzZkxtS011WVJSbWd0TW9VY016MWR2NWdqQmludmpCRkVBYTE0WHd4dFJaZkVVWmtoNzl3Q1JGUU5ZS3IxaXdvbDE4Q1BIVlA3cVk1WXdsYWxVNUczYkVPdUROZGRUSlJaRCtnS3B3RkZxT0Y2QmN3SkFvSzhpMlJHVzk3LzhPYmZmS0lpZnlJQjhTZ01rSUIyQS9jQ2RHL2VzYjQxdlF4OGxsWmxnZmVXQVhSalduZWdyZUJHU0pNa0lTeFE2QnZFZWdEVGRFR0xTWkhVZlhDWjJyRUlIM3cyVmtXQ2lNenB2cmMweDhReE9Wb0d6SHd2TU40QkpGNFZOTlRaMEF0cE4wUkhxUUV5aVFIYUQwS3ZLNjRNaVh4cW9RU0hCVnloT0hubzZlT3h0d0RjL0dUandYZFF0SHhhN0x1QVl5anlYR0RwZXZEYVpDaUdGQjZTWVJvTlczYU4xejlRVFd6TGtIcmxmMzFPTEFYalp6MEZmOG5qMHYvc0s0SUVYU0RFdkdqWG5xM3hEVU91VnVtVGp1cXp4OGsyMHR6UytHa2FnSm5xQWNsVFcvYVZQcWpkNXFoZGNhdE94REJCbXg0RERSMkhzZ1JnSmlWYmxXZTNJWU0wRnR3SHVxaEdzSVhvSnY5QTFnZEJSeFNuY3hjMC93dFVvdjRUSDNHZitkNThHQ0FEWFhCTS8vMWpXbTVFSytsN3U1cHJzZEZweWFPSVkxM09BencxbzdzRFF4d25XdGQ0S0xIRkE2MEFrbDF0Tm5GWUpIcjFKM05vRVVnQXY3bFVvS1ZNN1hwMTVQckRJd0FSTjZNcmdYT3ZGNnNTS1EwcTkxKy9ERU5tRDdKWWlpUGliRG1EblFoY2xHOFlCZHRvZThHRTkrYzVmQUw3dFM4a1hQVi9sYlcrRVM5S2twM2RkSEtNQVpvR2xBRGtUZVVRcUJTd0ZublBzQXlrWkVPQm1WUDA3ditNMkRMLzQzK0hQZkRMOGloOEExeVRkLzZ5b2RMdEJtR2FsRStqRTFnYUJMc1FITVFxa0FCU3RqajFoY2pLNU1jWWtzOWt2Z1BqQUpidWRza3Qyd2YwVVA2eTlObFVkN2ZWR0g5OTdFNndVeFVaMjUvYnFXNmVqTlBrRzIxY1ZRTEJoaGJFY3NxQkh4aTRPVWhyVjBkTXQzUWIrdkQvNmFnQjRBZDcxbWEvcUFvQkxMNFhqV3VCdFI4YzNmVkcyOGF3ZVhYR2dtd0JwRXIzZmVWNDB6cFAxT3hESFV1VlJJT3BNL2FBYmxiQnNIS2d5bzNoU0lxQ1JXcjhYMnJVM21saVlWQ2RHdFB4S2ZPU1hDZGYrWndNeWx1MUtyWlJyVVNlOFJhQ1NKaUMycGtZWXB6VlFyRFhoYkNNSXllSzRTd1pBOE55VGdNVUFmOWYvaEYzN204SkpGeEpQK0dmU1k3OVk5b2duR1BidWd4dGwyOTFEeTd2YUFIblhOVlFVdXVtajB0dmVRYnorRDhIcjNnazdjamR3Mmo3aG92T0JvUkNMZVV6RVl1M3RpMDNkbFcxWWJvS3EwMFJxbnEwaXlVSWk1b0tIbUNlMFU4M01Fa2oxOE5sZDRKZGZ5bTduVHFCa0tYVnhYc1B1Q0EvQWFmaXpkeUtweE0zdHFnVkc4NUJFUm9sK3Zsb2JSakdDYW9SR2g1VGgyQW5ubEU3QmZZcXV2dzN6RC82WHMyOTRGNit2dU0xblk0QzhFblVoNUhqZDExNDRmYzlaKyt5eG1CV25BZDBhTUd3SUpMazRPS3AvVUYveFBRQUowUzlTT1ExblZKRnhxS281WkUzaHV5bU1SNWx2Zkp2OHpBdGs3ckVnMmhBZVVoVzlmL0JqaVpVemdYSXYwUFVWMlFVYXZnY0xnYnBWQUkrK1BXTm1tYmFFNW92ZXdrNUx4OVdPdERxUFhBSTdPdk1rRXBTdjN5SjcvYjhYWGtXV0hlZUp1M2VTRno2Y2ZzN0RwWlcxbUc3Vkd6bU84bmtCRnlOMS9YdWc5L3c1N01oeDh0N2JnRDI3d2QyN2dkUE9nV1labWk4SUdDcVFCOVFDSnNRRVpNREIxYTZMUlp0TW5kaEVDekZ6c3JxNXFzWmZRaEdxblRIMzJnaHlIZnp5TDJ0WGRLbUJYQlpKWFNJYzhyLzhJQkwyTXRaUjFOb0trVFlQOEtEb1RqaGxUU3ZOaWcwV2lFTEJMb1lLM0p4bE5PdmZiL1BYODNvcy9nU1hkRS9CdGZtek1rQUF1T1pTSkFMNTdzUDRmWjJFeC9ZOWNuRjAzUnBvVzRRNklSOTFsb1dVK29EdWxxM0hGWFEyQVlvZHdKR21CMUVmcHlLWjBEdlNiZGVUNGZPSjZBUmtrMnJRTTdCanQvQ1laeFB2L1JuZ3JGT0VoZGRuVmtPdW5yQW1FMEpaUXE1eHFwdTQxK3J6bHlWMyt4NjFBUjBoS2hYSVVpQUhiVzBIL1B4ZDRSazJEc05tQjRHM2YwaDg0MjlDSThnQ2VZbnVTd2pVdEVkYVdRblJ3OGxyMGhrWFVJTkRDMG16c2M0M1l2VFhnRVNkaFYydmV4eVBoVUM2allpSnptRzJ1UTVta0VwTksxRlErMkNxazdUNjZlYkhvTE5QWnYvVlQzZTRVNVpRVzczVXRzNWFTc2gzSHlMZWNwMDZuQTMzM0U1QTZKTGlMVHlFcHFHa1JHVklta21HVGo1ckJSazdVQ0NoVEdIcG81enJHaDY3V3Jodit1MHpNOEJyZ3huN3M5dkhxeTQ4eTE1MHZ6V3VhblJaTC9RN1hZczVxZDYxdUh2ZzJvTW0waWdpVmQxczlWQ0JoUW1rdGY2UldpOGE0Q093bXFRYi8xUUNZTmFoeUVWWTQzbmdOWTNVTTc0TnZPNlhBSTJSeVhSYTJsSTQxanAzcW9IWVh1czBHYzFxZjBWcnNZcEQyLzZYcW1oRDlZSWg5aEROZ3N6UERzOGkrZ2t3bVFCN2RpTkdNb1hyaFdKMUNtV0tlY3d1amhSR0VYa2VkMThQQnQ0ZWpnd2x5TEN3dEdCczJLRWVoK3FZRFZZdlo1SGZlTGh6Qy9jR1VES1NycEROcTBvRTJFM3B3ODJ3RjN5dmQzdDJBY01BVG5wVXhWNDB1bVVISmttTFAza3JmV3NHZHIyekxLd3hSMUhzMSt4a20rMElyU3lnT0RKWkFaQlJ0QnZPVHRBb0ZEQk5QOEpqNzd2NnRCditVcmVBd05YYm8xN3Y0M0dmUlFnQVhBbTRMa082OHRiRmpiZHQrSjlFRVdBT0N0TTlaZ2FuclJvV1IxeGxjSEtDcXVBSzZaMGxOQW1XV2c1WVZVQmg5aEt3WjByY2RCMzl5SjEwbzFLWVJHMUdSR3dSS3RseDNrT0JoLzRMNFBCUllhV3Z4UVMzdjFweDBaUXpWYnEvMUFpYVpMSFlNc2JEUnhYTmxqWkU1eDVhTVNXdmpVOUtncHZEZWdoZFdMVjhKTVlCbk0rQllRNHU1clRGQWhobXdEaUcwRlRsQkdrWVVLRWx3TUE2QkoxMTRDWVRhMG5kMS9RbGppbEdSa1plcmJadkwyYk5hTG5UT09qTzVzY1Z1ZUY4a3podGw5SzNmMHZNTEs4dHFPRDJkYWd1Z3Y0bmY0cE9PNENsOEwweEcwSHJSWnVXcDFieHR1ZFVPNFlITU0zZEZSZnNJUjQzOGErdy9qOTVDK2JYNEpKR25INzJCbmppNDhPSDgyOXVMc0NVQkRuUlRhQTBqZHdFQnM1dnk4SWtlTitRNFVkOTdxRUpQS0c5RWN1MlRDUVNLeXN3SGhiKzhuVVZiMXRxb0NPa3hXWWpVb0l1LzNGZzNFZGdETmxVYS85TUFjTUV2RU94NFkvRzJxRW5XVFZHdGVrS0RidGNHakZZNTlXMDNsOHkwYzBZVXdYYWpWUGZNM3BkRkZ0aDRpczRUTE9RcmlTekpaaFdKMG1ZTFhIRGlQNEJKZ3RXSjdZcEpHeDE0cmRhcjNVVWI5d1dkTEFwVkVKZHRleGZVWUg2cWJSMWsrekE5NU1uNzZjWGg2ZTZtaURRVjNoeHNPOVExamVJVjc5TkhVNEJmTTdZdEJUaTAyYXJsUVdwaFZGUVBrSU1Kc3AxSTlVT0ZLekNWVmpVcyt2ZWErdnJQNTl1dVZvQUw4VzFuN0w2L1l3TWtGZWpTT0FyYnltdnZmR1lya2NIVTR4UDUzUTM0MFpmQVlaN0NzZXRBcXd5ZG5ZbW1CS3NBdElJMlRvaU00dWNrT2hxbG5acXorN3R2eDFBcVJuTTBIb1R3ZzVUb3J3QUo5OGZldTVQQVRjZkExWXE1ZCs4V2ZXQURYNkp4dnI2ZXdNOWtkNGJVMTgvY1llZzZKSi8vT3NzeGFVTng0eEdKNjlSRUIwcW1GR3hPYXR3ejNKOE1LSzkxcUlvZGFMaGp5aFYvcGxxS2hMY09KZGkzWmpzVlEyMnJvRXdReHQvcDlxTEhUVko3VEVKOEptZ0NqRmRneCs2Q2VWcFR4YS83Zm5nbUdFV1N3MlcxNU9pbGRDNERQLzc5U3AzSHlQN1ZTdzNTYkV5Y0xYUWFIbGZEVXd0aDFHTTBjd29LTmhYdHlOUktJTlorakJudjNIN2JIYWJjSm45ZGQ3dnJ6VkFBTURsc0QrK0c1c2YzaHgvZnB6Q3VoN0tsTklPWWJJV1RwcFRZWDdMUUV4cU4xVUtVUURqRnFwRmlBSVVYcXFuSGM0Q25MUUQ1ZmEzMHE5L1IyQlVlWXl3c3ExdFFaUGk2TXRlS0QzMm00bmI3d1RXSm9BeVlsUkZYS1FsTFZmWEk0aXNSWkREcko2OEVNbnFoTUtvNXFhcVJpYXF2azd0dWhPU3k5cUNuQTdBQk5zaWl4aVFMa3NpVW9hU081S1VhczlLYmRFRWsxT3BNTmJKQXVwTDRKbGRxV0lMWDdJZlpySklFWXEza1IvTGJRRjFESWhxZ3pxVVlTc1QyTkhiWUJmdVF2K2JMMk15RnhEdGxJb0dwUnFDbytKMlFPTXYveGFTOWdNK29JNXphKzNxck5PZkFKUklpR3Yzb2xBc2NEOXhRTUZPRE56QmdnRnVxK2g0SXpibWJ5aTMvQ0lBWElHci8xcmorOHdNOE9xb1VIL243Zm0zYnpqc2g3QkdFeUFtWXJLWFVvRnNCY2hIcGVGd0FWWmpiS1JYM3JidThnUzdNSVpLNXhFZDY4QnlROW8vSXYzaFN3R0VMdURFZzZ2Z1NwUjRwVURmOUxQUU9jOENicjhyak5CcWlLeUthS1phalRjUVBGSFdoV2Z4Q0sxTGJ4V2hXMVhreXFVblpPV3NLM2NkRTIyRGxXanBSSGl1aGljYXFTU2xCRm5VOHZTa1JpZHY0NWRVcENlMWhSUVdtakcySWVpb04xTDFpRzM0WjRnT290UU91S0FTSVpRNFhZSHV1dEg5ZmxPbE43NFdPUDIwU0dWU1lvZ250MHNBbFVMck9wVS9lU3Y0Wng5VVN2dUJNbFlvc1UwNmNHeERDNmpKUWh1VUZoTVV4eGd3ajVQcWFTYThvT3Y2RDJIMjZsZU5SOTR2SExBclB3MzI5MWtaSUFGZGN3blNxelp4OEtNZjg1OFJhSDMwMUxEYllaeXNnTW9BSjhEc3hnRllFZEZiZUFvU1ZtZTZvTXFQbCtHM1VYYkt3dWw3d1J0ZUEvN2xxMkg5SkZvb2w1YlhVQk9ER2NpMEEzckJLK0gzK3hyZzVrUHczcVhwU25oUGt1aHFVMjB6b0s3bW15bCtWNVA4eVA2WHZESzJPZWIydHhGNlVUUHZhT0dJbXlqNHFjajlXdGlzSzJMaStkWlZOTnF3cEFGaDh1REJ3M2dSeUpEWVZqNEFEY2l2cXRNS1RGbmdtaFdYVnl3SkF0ajNsQm5LN1IrRXZ1WUp0R3ZlYkhiMk9lQXd4Z0hFQzBiYVF5d25RQUhnOEpQL0JUYnVzOFlRdDNRVmFFS0U5bjJkZzNkQ1NDNXdqQ2phQ2RjYXBDekhDanQ4RUp0NlEzZjNUd1BBNWJqeTB4VytuNTBCQXNDbDEwWXVlUFZiaC85eC9UMjZFNnV3UWhXWnNMSTNEbzhUSUIrVDVyZGsySzc2WVplaUFXeXJabHF1WldxSmZhUUtaM1RpSC93NE5DN3FOZGlXTDBZd1ZnVy9ISnJzQkY3dys4Q1h2UlIyOXhROGRDUzYwS2FwSlZUMUQ5dUVTQURHV3FVSGluSWljd096Tmp5by9xd2FrcWw2UGl3SGRZYVVPU1k1MS9rM0liNWcrM3hWQ29CYWdEVlVpckNZV3dBVHZCSmNRSVZVb3JpQVlIVFYrWmhMTlpUaUhoWlNJaWZUVUJNZXZsUEloNkNmK0JIWTcvMGVlTVpwd3BoaFhWOG5ERld6cm42SUpaTmR3dFlyZmc5NjA3dVF1bE9sUEFoMXVGQ3JhbFhsVnpGVnRRMnBDR01zY0F3aHhlZGVaTlR0bUlPeDc5N3BXLy9uVjJhM3YxMDRZRmUza2F5Zkt3TWtJRndPK3kzZzN2Y2U4cGVORHNQRVhYMUIyaVAwYTRoY1k0OXg4NFpSZVE1Z0VzYWxEclNvTmdObnFSNEZhRWFJeU9YMjdTVG0xOGxmODVQeTFFRWxvN1pab1FXZEZoVk1naFVYbnZuajhoOThPL3poM3lNYzNDbmNmZ2lZSFNNNkJBOXJ6YjBCYmJFaVN0MEVWQXZJN2Z3YUxjZHU1aE9IR05DaW5HakxvS01xcjJOVTBScUdURUM5bEZvT2lNWVNPbW5lcFVKOGxhMXh5VjBxRHFKRWUxRVJVaFpRRElvbUZ3QVR3NUNNaDQ2RHQ5eEs0aWoxTDc1UzlxZC96TW1QL1JBc1oxZ3BRdGNSRnFQVFRTeFdaOWdqTzlnbGxJL2RxZkpkUHdIYS9WVjhJU0Nhbk9LRGU5djN3ZmdVcFoyTG1rdVNBOXhHWk81QTRSckVRdmNPNUVlNmpmSzJIWGYvRlBIWmVUODBVL2hNbjZzRDRPT3V4TTVmK0gvNjl6L3lESnhkQmlsMVl0NEVOdTkyYXBKUVprWDl2aDY3djJRS3JEczlCWm9TZzNrUWlwZ1R3T3JhMWtWbWoyTGc1Z3g5eXh2SUJ6OFp5SU9VSnBGTFZjS2djcWRCSG5nV1VrY0hYRWR1QTkvMXU3VDN2d3E0NTcxQU9ScVRVVmNUTUZrQlVvK2x5M0hGQ2p5M2FJZk5nV0xYSHB1V2Q3ZWVHeldKaTl6amVoVXU5NDNBcTlZejJtTmxUcmpIRENPTTlhYXA2SkxMZ2o2UVJjTkZqbm56ektTTmh1RCtUY2lrYnl5QWpabHNub0d0Z2I2NkEzckloZEkvZXc3VHM3OFNlTUFEUVVDZU04eTZ1SkZqOGxXZHcyNG81a2dPb2pqUmR4cWU4VnlXTjl3SXBMT0JNcWhKNm9Ib3BRdDRuSFZKZ1poQnhUWmdZSUJ6WHBYUHB5TnpqVUJXR1ZhNzZmUzMrbnRmL28yejl6NVBPR0RFbFo5Ujd2YzNNVURvTWlSZWpmTGJsOWp6di9aUjNTOVBrNC9LNnRsQlczY0o4MDJqclFqbG1HdnQ0aWxXSDlBREd3N3ZZdWxCNi9PRnNUVUJ0WEFwakRLa0JDd0c0V01kOEYxdmhNNTdET0Vac0c3SlNOYU91VG9IbERKM1FZVkkvWGF2NzUzdnA5MzhYdnBIL2hEcDRCMk80emZDWjdkRjJ3bFRHRldxMVhPSlY0N0tJc3JFcUJBNnFFVDFRcER5RkkwVUVsaHEwcGRTakpCMHl1cnVYNVJZcXNIUllmT1E5R3NzTVlFcWl4Z0I1QUlMUVFsWXdESXZTdk5FRmFGc0xXVHN5SE1mQkp4ME9zb0Y1eUk5N1JuUUJROFN2K2lockp3WW1YTnRQelhpNDRTd2hyaE5LWGcxbjY3bjhLKytIZnFOMTZCTXZrZ2Nab2k1MHUyY0lxUVBhQnZ1SWhZVXhNRHdBbkdPVVF1QWUxRjBDZ295aFJWUTEwM3o1b3QwOHlQK3orS3VtNjhBK1prV0gzOGpBd1FBSFlEeFN2aTFYNVArOU1rUHRpZGk3Z1ZKcVRoMS9OYjYwWHZTanpwMlAyVU4vVjdLWnhadG5XVGQ5eEV2Rlkzb3dyTGpEeFJTUjJ6Tm9NTTdvRzk1UFhET284QXloQWNUMEthcU5SSUxMWGk2RzFWUVFERjFiR1lsUU9Yd0xjRFJ1MkdiNjdENUlmRHd6Y1NSbThDamg0QmpHUmdCakE3a0FwVUVqa050dVZ5WGovTmdPTVk1TU01ZzQ4RGEwd0hQQmN3VjQraVQyRTBBZG94NXdTc2dWNGh1VGVoMlVEYVZwbXNBMDNaTGdwdmdUcTFNZ2JQUEp4L3dVUGlaNTBGN2RxcTczLzJJdmZ1bGVyb01pSjRURjVtU2tBeVZNWXZzQXRIN0JkRWRicUdpcFpjdTBiL3J4MWhlOW12ZzlHSHljVmcyNGRUOVNxcjhCcHU4b1FJOEVPZ1p4QURuRmpKV1VIUmEzSVZNVWg2N2J2SmJrenQvNUlWYjEvKy9WK0d5ZERtdS9veHp2Nyt4QVY1MkdkTFZ2NHZ5UFEvQmsvN2RVL28zbjdIUFZXWkthUVcyT0daYXY4dVJWZ1VOZ0pYRXZWKzFKbVJDWXgzM1dza05NQ3BQdGw1TkkxRzNCOENNV045Q09ieGIrRmR2Z0ozN0tMSU1rRG9ndFZhTDZvOFUzV0VHVXlYWElDOWFOaXdTZ25VdGt3YkM1RnRtMkhMZzJML1M3QUtReWtDT015RFBoTEZJWlRETEExV3lUSFZ0NDVEcjhtNVV6V0FYY3Z2VUExMVBkaE5xTWhVbU8wSlNIdU51dkpaZlBPR1lZRFdJQXEzMUNMS2MzWXRYZEo2b2FpRUNZYmRWTDFSN1l3TXNjQWN3TEdDVHFkeUE0WVUvQlArbFY3THJId2d2aTNyU1hmS2xvcFdWNTJDemgyYUFEbUtFZkJQRkNNZXB6Sm9ZNUlTdnF1L2UwSysvNnhsblgvZWtxNjYvTEY4ZUxaZWZFZlozNHVPek5rQUF1T295cE11dlJ2bURaNmFmZWM0VCtOM1k4Q3l5WTI5YXY2VndXRWpkQ3BXUE8vdDlQWFkvWXlkd09JZDR6NnlkL2dvZ1d3V0VHeGhCUmFGZ3dtSUdIZHBKZk9YUGc0LzYrampMZVJSVEZ5OGdTQVlLWGpmQjFKMm5rU2hpR1EzY1ZieEJIelJHWXgvTllxUTNGWHZJNjFycjJLMlV1cVh3cTUxWU95RnpyU2V2bFRmTDgxZzlscXdhVWxnTEFPVVlUY05BWnBaclM5QXN2eW8zVkt2dTJIY1JaOFNNWkl4V00wZkZoT0ozYlZpcW94WlpMbGpmSTk5NUQ4cjMvQnZ3ZDY4VkpoY2Fod1hxV2tINWRudEk5WDRoQnFyQUlYSzFveEhRRE1LQWdwTlFzTk9rWXRTcUo3K2xIL3VmN0c3NXNsL2F2UHZOVndIcDhzK2k4ajN4OFRjeVFBSEVBZkR4VjJMdkwveXI3dDBQZjVET0swZmthWnJNcytQWVRVVktCdlpRdWRlNWV0R0tkanh1d3YrdnZETVBzdXl1N3Z2bm5OKzk3M1gzVE05b3RBdEpGa2hqSVNRTGdRVU9NbUNCWkxhRUpjU1dvSVFrQjhwbHlzUlVHUk9idUJMWFNJQXJqZ1BFam0wY0YxVUJid1JMRUZzaERpWW9DQkEyRkVKMlNFQW9sTkEyV21mVFRIZS9mc3Y5L2M3SkgrZDNYN2RVYkZvWndhbWFtcWw1M2UvZDk5NjV2N045ei9mTEhoUGFhTXU0OW91ODBzZVkybzhnYUVpeU80MEtzeG5zSHNQT04rQXYyNFVjdTlNZHhFdm5vcWs2b3J1SnlLYWFXYnp5RHhielRUMlNqZmVzL2Z1SUxNam5qNXJGam5HRmVQWUpqVUxvdFZRcWpMcVpNVDk1NmlkVGZhNWVSRXh3UFVqQ2kyUGl4YWtVSkxvSjJXaWtZRWdOZFpqNmJtTFlGcVMwY1VXMXhQQTVKaW82ZEtWdXB6WFIvNXRlODlmNDIzOGQvZVlJaGs5RFptTUpSK3NQVnphUWJQVmU2anMyQVRSd3p3Z1Rzcy9BRnpBNVJrcmRaOURjSmgxOG9Obnp3VGVQdi9HbVhaemZYUGtkOEg3ZnpiNm5Oc3hEVGNDdnZnbjVJaHo0ekEzNWwvYXVZR2xaU3pFeFhSUVdUMUJzWm01RnBEbEtXZi9xbFBXYk96aXVramcwRzgzZWpXMDNuMFA3YVFVR0NHS3dNSURUZDhDK1AwYysrQno0NUR0Z2ZTK2t0cDRTaGxseEZUZXRwNTZwUnp2TFZRSi9oMERCcmN5MWN3MTZKbHJpQzZrbmt2WlV0ZEh3MDhwOGJnU25DOXJFR0VoVEpkc1VFVTJpS2VFaW9pbFJiNHlLRjFWeFE4eUNmejhKbTNUWWlJMi8vai9FNlZGYTBIY0E0K0o2eHJUYWo0cWtvY3NSVkpxRU5RM2RqWC9QNUhXWGVYbnRtNUE3V3BFdHA3am45ZmkxaW9SelRLb1FOMTdoOXpXaG9PNzNRajMxWmhpSnpvOEkxbnd5SlE4YWJhNVBCMi81dzNUUE81eGQrdTIyM1I2R0x6MXl1KzU4bWhkL2xuek5hM2pmcTg5djM4YXF6MHFoa1JZWjNWR1lyaUU2ckovcEhtZjdxNWNZbk5EQ0NzRVBnL2VBbHdwaXJkVm9kTjZZdTBjbUFITmw0dXhkQjMrcWNNNWJuSDkwS2JaOEF2UWgwb3E0bDJqOVN6S1JIb1d2eGVvMnY0WWdxMUVqWDkvakJ1aUgreFdOS2ZNUWEwRlNtMVEzNnN6cUNPRWwwZSt6VXRBcUk2bUVZT2E4UWxYd1Vpa2dvaGRVenpCMU1CRlJGY0hNVENSMHgycGYyRVFrbEJQRDhhS2Q1VTJEZ0pmaVVtNzRFdjZmLzhUbHc5Zmdvd0d5ZEpKN3lYZ3BpS1ZldVRRRzdEQm5Bb3dQVHNYcjBSMUh1VXFIK1FvZVlBTXB2cXpSTVJ5U1p0OVk3SWEvYjd0ZjludnJlLy9uSXkwOE50dWpja0FINFNyMGpJdFordE0zTjU5NzdwbnlyTExQc2lSSnFIUG9Gdk91dUtTQkNETjNEZ25iTDk0cXpiYkdmUzJMTk13VHN6b0xkYXBXVzlYMWlSdXpTc1dCNE5vZ28xWFlNNFBtYVB5VTF5Qm4vclRicVM4VjJYSmszMElHK3RaWWNjemRLaHBUb0k0OVpMN1owUThMSFBjVTZoczFHeFd4VU1aMHI2bFlPR3lzY05RWTZmMlo2a0JTRFhVbUpYSXFrMHJjSGUvRzU2Vzh6eGZ5b2VhQTg4ME9GNCtES1VLL3FrcnpZT3l3M1g4ZjViOSszT1VqSDZOOCtTYlNPalNMeDR0cDQ3bnJLclJHK25aUGpiM3hJY3h2SG5DcjRwWkd2ZXUwa1pXU3ZjTlpESDRnd1p6VzZQS3dIZnhCYys4N2YyVjB4NjQvNHR6MnpkellQUnIvaVUvL1VWcGZrUHoyaVR6ejlhOXJiamo1S2FTeWdxUUI1SW5MQTk4c1NBL1puNERPWVB2cmxrbUx3QmlucWRlUVJPaUI5UDEwdEUrMU82ak4rc2o0c3dZNFlUcURnNnRPRVdpT2dlTmZRRG42YlBpUjg1Qmp6OEtIQzhqaWRqeTEzaitWYkhyUDN5Ny82S3ZRUUZsYmJCcVkweVBoTndNbUlCcmowY2lsd3I4bHdwc1RvTm9ZaVlSdVNab1BDamZsamh2WHRlbXhCMTJQSER4b0hOaXY5dGVmeEs3L2t0di91bDdrd0lxckh1bXl0RTFkazVOblhvcUtvejNycXJ1SmFwa0xmTmJTSjI0b2Q3ZmE0bGRyM2RQMkpHdjdqYkU3UTh4M2FJbWozYTBNR0xiL2ZYRG9obGVkOVBVWCtpMFg1WXAwZnRoVjcwUHRVVHNnYklUaXF5N1FuMy81UytVRHl5YXpia1pxQjU0bXE4N3FiZVpwUUx6NUthS21iSC85TWpyQUdSUHdwdFJQUTlsSTdXdEpVVE9pamYrYm40amdsZXVYUE1QV1J1aTZndzBGWGNBSFc1QXRwN3R0UFJrV2ozQmYzTzdhSEpIOHFOT2Q1Vk5oWVFHMEZWSWJEdDNFSlhpN2lLUldwSWt0OTk1VDZoL1pkRy8wWDBBZnhqWi9vSE1jaWdERk81ak5uT2tVejBHeDYrWm90aXJERmlQWU5CNjdIZHlQN2R2dDluKy9MdHovQUg3UEh0Ry8reUk2SHJzY0hFRmFGbDg2MmtVRzRsYndrcDBpTWIzeFFEZkV6U01oVFd4U1pRYnhXbTFWelRweDhlQUdUY2NQZEgwMSsyak5hVEIyWURUcTFnbXpSWWJETDhyb3J2Zmx1MS80TVE3ZStSdTRQdHlHODdleng4UUJBYTY2aW5UeHhaUlBYU3J2dWVCWituYW0wdUhlNmtCOGZZL0oybTZMbWJIaXRvNGtVN1pmdXRXMVVYd2FrUDVlclRRK2xqb1RuKytPc2VHSVBhVFlveUZHaC9jb0F4RUJ5eDVKdXNOMEFwUE9iSVo0QVptZ09tMGNIMkJhMGRaVFhIMkFTeXVlRk5JUnJycU1OUXRpTW5STkEwZUhnZzVGdkszbG9sRFNJQVl5SldaelBqUEhDanFiUVJaMVZhU2Jvck1wWmJUcWpOYmRWbGRFSm1QM1lqRE93aXpIckJZSlFsTlBJQ3BtbmJNMmRqeUpESmFoWFVha2RXa0dLaDM0ckxqWG1iR0lRRkVQd1RmQlN3Z3B1MHZsdEl5VTB5cm93WVIrM2NoUmxmYkVvYThmY2xsZm1WRkF0bU8rQk15MGxNVm1vZnVLVHByM2N0OEZmenJaKzNtbnh1ekh5QjR6QjNRUUxrTGxhdEsxLzF3K2VlR3o1VVdzTVNzbWJSb0trejNHNkM1elhZb05YbHQxMU5XWGYyNGJ6WkxncTFsa0dLQ0RlWU1zL3E2cnFFWmRHQW9IN0QrQ0FHNVlRTlMwTnJQNzBsWUZ4S3owblRsUUswcG5VVVdXK2p4ZGdieHBNV2xhaU9WbmQ4OUZwQ3ZReFNTYUtkR3BqWTBjM0VCelRlSUxIaXk2aXNlTkFYVVB3WnBVMGEwcTRna3RHcHQxSkNjRGx1TG5UUVBVaUVKSmVCYVlScVRveGQwME54TFlDblVwVmJXdG4xZVhoUGM5RmNTbGlJcHR3amVydXBXQ3F1UEhOS1RqbDN4MklNdG85OFJkUkJZcGJDVndGNDJtNmYwTE1ueWYzZm1ydnozWis1N3JvSGt4UE9LV3k3ZXl4OHdCQVhhQnZrdXhseHJIdk9zdCt0bm4vQmpQS0EvUTRUU3BoZkc5enVoK3Axa0VNOEZIc2RxNDdiSnROTWVMK0g1REJuMXcwM3I0SVQyK2FlTUVKSURRRUZ6WlplTUxvSFl1QXMvcDlmSEFFV0E5T3hXOXJrczRZYmFhMk5VbDVqbnJWYXBjM01qRzYzajhiaGJIVlN6RUs2T2JXMUtzVWhaenNxQmRqQmc5ZzJ2ZjR6UzhnT1FFSFVoMk1WUFhyRUoySk9QbUNxSkNwMUZGenhKMFRiQzZGakZ5cXVMWjR0NDd2SHNGU1d2Zm5SY2M5eXJ1VkhmNDNiMjRISmtrUFczZ2VseExkNnY1NnMzcktxaTNGTmtxam9uUW11ZlpvRzNmTDN2ZSs3YnA3bjk1WGV6M1Z2N2Z4ODRlVXdlRWpWSGR6MjNqcVc5N1k3cituSjF5VXJkQ1ZrcEtpekRhall6dmQ5Y2g0aUxPeExFMTJQYXpXMlQ0akFHK3g1REIvTkpxUzZhKzUzN0J6K3RwV085MGgwaGs2akRJZTZtTG9Dd1JLM1Z2T3dTZTRoZXl5RnpYT1ZPUk1GSG9oTmhqZlM0RG1UdWZSUmd2UUs3UXpSeERWQ214dW9JaFpERU4ySng0RmlRSG9ob1hKNE9YL2pVMG5MRUREYlE3MHRGSDBuaU5yR2lSYUVVVmpldkxHcWRtbHFwZEhDZ3FONEdhNzJGU056Vk1WRVNrYzRxWnlhbUxvcWNNa1FZZjc1Nnhmbk9zanJiaXNwUWNNM3pvZEtWdGh4L1cvZSsvZkhMbnYzaThuRy8rTFQvVzFsZkdseC9MTTMvbERlMm56em5OaityV3JMVEpsUVdSMGEzRyt2MmdTL1VOT1pUN1lQbUNKVm04WUJFT0ZDZDdJSmo3cTNTbEg1SFRoNXo2UVp2MGZJMWFzWlRlTzZqM2tDcHppeS9TWFRmZ1ZoSzkyQXpVVVhVVU9EVWNoNE1idWZacSt1Y3VFcHFCOVFTVlF1d2VWM2tRS3lJYVRody9GeXBUYnVidVdVV0tSRnFSQmMrUUN2aE1SRXl4TG5ieW5WaDNJSXRJRWFFRWdObUx1blNJWjRtYndxc1RXbjFPRTdGTWhUQkhYYTZqTEw0dG9jL2VCZ3ZKeWM3a3RqR2pXeWN1S0swaWk3RUs2bTFtVnRvMC9IQmErYlBMMTIrL3pBTlMvSWptdk4rTFBhSkp5SGV6aTYrbTdEcWY1ay8yOEg4KzhsZmRLMjdhTGZ2YUkwbkZNSi9BbHFjSlc1K2lYa2IxRndTYXA0aXNYYmZPMmtkR3NFMkVyY1NYWFZjVGc2UmM0bzlJRDUrdmtIZUorV2pkRFpuditHcGRRRW9lSWEzZmJxdTd3N0VaUjkybTYrc1lObFpKQldmTzlGODMrcEpFVzdEdXVsVFZLRUVDR1VXU2ZoT1EwQSsyQ3UrUGcwb1VySXA5VnlKSktUMUNtOWlwbG9xc3JydkJJZitsZFMxY1lxa3BXQ2Fncm5iVzVOTXc5MWlkYXlLMTBIR0huTDVJZXRFUnlISzh6dXpPQ2ROYng0Z2tHU2l5Mk1ZVXBEWHBHRGJEanphSC90dmw2N2UvMFRjQUVvK0w4OVd2L3ZHelhlZlRYUGxaOHI4NmczUGYrRS8xRTZmdjVKaHl3TEk2alF6VngvZTRyOTd0a2hZSjl1eEdzTDN1YVZsbCtmVmJhRTVzOEgxV2FkMThvd2p4aUdhaDFTSys2V01LSkg4QjZMbFMzQ25pNWlGNVV2dUp2ZmhQZlE2dkp4djE5Sk5lNThYbjZQUWVJSndkejRqazJBSHg3SjVNK2xNMFVBMEYxNDZldmdNdjRMSEtoaGRFU21oZld4L3FNeUxUQk1VSnNwVTZwTW5CR0taRmhGeVRzeXl1V2JBc3RTaVI2SzFVZ2N6Z1VWWlk2OFIySkUvUDNpN3BpSUg0dURqcWpMK3lSbmQ3NTA2aUVmZUZOalpRV3JSejFjRmZ5cUZyZm1aeXg4VUtNOXZveEQ1dTlyZzZJR3owQ0gvdE9NNjY1REw5bTNQTzhKUEtRWEtDeEFJKzJlOHl1clhPUUFiRTh2b2F6Z2paOHBJdExQelVBb3c4V2lxcGR1Sks0VUVpUGhyekNuVjZKWUg1Yk1tS3UvWUxHQVpXVExSMzVBZUZYZCtvaW11RkM0U0RaNmhMWTdWb2liOGR3UXV1dFIyQzRWWmN5SUprRVRwSGlnVHFHcnlndlFPalJmdm5jcytnV1pRY3p4Y05Sb1d1Q21FV1JZb1FvVmo2bk5VRG1TM1FCWjJKbStDckpTQVp6OWlLUDNzN3NwWWxUYktZQ2F0ZlBPUmxmMFkxZWF2T3NIRkR4SnFRZUYrODJnNWVjL0hrN3A5VnlMOEJqMW12N3p2WjQrNkFzSEVTdm40TFo3Mzl6ZkpYejNtbTdMUkRaREpKQjVESHh1ck53UmVlRnBpUEl1dytHUHpvZ0MyWExLUExDbnZMUnRKUWFrSHNEcWtIa01UdmVxWks2bFpvbHNkWXk0cjAyaXNCTE1uMUJpKytBY0Yzb0tQbWdGSEllSDdRNnNTOGFERVgxMEtvR0dURkMrWTVWbmZwUktKUXFOZWpRaWtFYjB6Ti95Z3Uzb2xMQmkwaTNvRVg4WkJiRUN2RjR4cUxpQmV0VXI0aUVqem8za3VGZUc3YzE3SjdNWlZUbHRDenRwdHZUY0toUW1xRWZQZEUxbTlZOFRKeGtpYmFaTFF0QU5hWVRyMU5TeC9oMERXWHZQeWVuL0dyc1N2Z1lTT2JINms5SVE0SUc0WEp5K0drZC85eStpL25uaXN2NEZEcHlwUW1EUU94c25xek1Wc0JXWXlJU3dOK0FBUmg4WlZiR0o2M0NHdnV2bEtrSHhtTDFSM2VLRHBrRHZLWXMwbGcwVHFCQnhVUy9YNklid3EvenFhS21OakROb3VRbnV2UG05VVRzb3FoUm5oMkMwZEJpMk9GK1FsSVJ4UXFFaG8vM3ZXYTE0Sm54RHNKdXVQT2c3U285RElWY2ZwNUZwZXM0dG1kSW1JbU5KMjZ1NGladTZ5NDIwekZUeG9nWjI5RmR5ekFYZ054YkxtaC9NTXEzWTJIeEdoUUVXL2J5bkpYM0J1VnJyVE44Qzl0OVM4dVdyM3JVc2ZMRlk4QVZ2OW83QWx6UUtoTytGRUt6dUxmdmtrLzlwTS9xYS9BUzFkV1VCMUlrb0V6dnMxWjN3MDZKSGhZRkh3S3RnL2FVMXNXLzlrMm1wTVY5bVo4QWpRU29qZ2V4TFoxNFNqYXppWVJncUU2WCs5b1BmcTZBbDd5cHNmNnZLeGZVS29oTVVLeVNJUmZJbGNVQ2ZTbWdYV2dkUnptQmZFT2x3NlI0dEFKMXJQN1ZxbGZjaCtDNDdWa0tpSlZGN3huWTVGSy9FQW5mWG9RbW1rVGhkVWlWbkJPV2tKTzI0SWNPUkFkRjdkMWQ1WUc0dU5NL3NLSzU5dW5BUjNESllsYkdnWUJZeU1pNDZUcE9sdC96ejlaMi8xckN2NUVoZDNOOW9RNklNQ3VYZWc3MzRXNW9SKy9TSDduaFJmSVc3ZHZkYmNIS0lna0hUamRQaGpkNFZMRzBBenI0WmJBRHVLK0RzUHpGbVhMUDk2Q0xpZ2NOTWhZc0xxSnpBVmlQSnJKNG9FalVBY3J4QWxtZ3BqM0ZFL3hwenFoZSszNzVkcUc4ZHB6Sy9WVTdSOHJOZUFYaVdXakNNMU9SclJJdEpFS2dVRHRxcUJCeG1NTHIzN3dHZmRjMlR0bUViS3RRR0MzcEVJWUViS2lwdEFockRtZXhlWDRvWEQ2Rm1TNXhkYzZHQlYwSVhrWk5IUTNUM3oyeFZYeFRxMVpVQmxJcWF0S2dnNWtwb21GYjNhZGZjR21iN2xzYmM4ZjFmRmEzK3AvUXUwSmQwQ29ZN3ZhWXY3elYramw1NTNQKzU5MnFteGhuM1dXcGRFRmgreU1ia09tOStMYUl0NlRQRG40WHBDQnNQaVNyU3c4ZndGcDFleUFpZWI2YzE1Nko2UE8vQjBMbGpQSDV1Rlkra0xHTEVKczFLbENyaE9FdnNydVE3U0pSRlZONUhlaXRiSHM0a1ZjczdoMUxwaTQ1TkRrOGd4U2xWQWxpNVVhZ3NQQlBGNjNBK25xdkRZSGZZMlJzT3pnams2U01JcERVWTRmd29sTHlGSUxJOE1teFJrS09oRHkzWjEwWDE2bnU4OU1CZ2tYR0lqVE5BRUdhdERDVU5zYmJIemJOZXVqWC9qTjZhRnI2M2p0Y1dreWZ5LzJmWEhBL3JYOUtsUXVwdnk3MC9qeEMxK1QvdXpjNS9vekdKUExtbXRLcmd5aDJ3ZnJ0K0I1SGRFbEFsQ1hFS1pnKzNGZFZwWmVzTVRDVHkzQ1VoTGJuNTFKUVN1eVdhektiODJIb2FCQmJSS0ZpN05SaEJoeGVwYStpQW1NZ2RiUTdDWWl4ZXJjT0dxQWVkc21tMnRScDRzOUphMk9TVWRVd3E1dVdTcUFJSXFKUHBja3UyaFh3MjBOL2U3SjZjQm5qcFFrSERsRWoyL2RHaFdkcEhndkEzVXJZUGQyZEY5YnczWWI3bzM3TUNGbWtwSjRvOUhlVEVsOUpDVmRYOVkvdFd0MTc1dStCSGM5SHJQZGgydmZUd2NFTnRvMFo4QlJ2L3Q2K1EvUGV3R1hiZHNLakR5NzBjaENTSXhPZDhQb3RwalY2eURRTVo0RUp1NnlCOWR0S29Qemx4ZzhmOUYxRzZLclJYd2xIRTVTY01xcEIwaW05Z1BGYlNOTUIzQUFObG90eElrWVlWcTg1b1hTRnlsUmxJVE9YUkdSTHNLeEY5U3p1K1k2UHJOTk9XQWhkbDJ5T0Zhbk5sRU5Jek9aTTJKWWtaQmhWMUdXbThqcFhGeE5SRm9Kc01LcVUyNmJZcmRNZmJZL2NOclNwZ0R1Q3pSQmpXZEpQR3ZURHUvd2pvK1AxM2E5ZGYzQU93RWV6U0xSWTJuZmR3ZUVLRTVlOXpHS0c3enZoYnpoRlJmSzc1enhkRCthRVYwWmEwcHRuSWJsb0RPK0hXYjdpSGJNb0Q2Qml0dkU4WU1nUTJIaHJLRXNQRytSNXVRQkRCMVdjVVplOXlHTEdPTHpubUdwUlVqeGV2SnNudjBDdUpORnZGUU9oMDZpTFZOSGNaS0pHWEJVd0c0WkNJY1VxVzJZV3ZoNHNScU9jeVhYS29wVnNJTmFqd1FYTnhSU3F0dzVFaWl6aFBqRXZkeGRwTnplWWZjVmJBcmVxbnNEZ2tnUzkyYW9ybEZ4bDRHSTVJYjJjOVBwTFovdXByLzRtNGNPWGV1Z1Z4QU11RS9vbC94dDdMQndRS2g1WVEzSnYzUVVaMXp5Qm43cjJjL2tOUXRMQXF0ME5xWFJoZUR3eTRlRThlMU85MEFGYVRXNEpFUlM1R2IrUUJRRHpRa043WTh0ZUh0bTY4MVRtbURRR3B2NHlKQ3VuNndZYzJSTVg0VGtHcDREekxEUis2dW9sN2xUbGRvb3RqbEF3YXdqSmk2WktCcHE0OXE5TnBtTHVCdVNRZ0ZMck9lRnRpRE9jUVZwVWpEd0MyN2pJcmFuVU80bzVIczZHQU1wNFlNNDZzb3NTRnpiUlNRdGlIdFJienVka1hUaC9wVDU5SFQ2b1YvWXMrOVgxMkJmYmVWLzMwKzl6WGJZT0dCdmZiOFE0RU12NGRMenp1UGRwNThocDVDQk5UTG1pUVVSRXVTRE1MbkRtZTZKcWxxR29BbVhGQVdFamQxWkpSalJqbTJrT1dQZ2c2Y1BwVG01Z1NVUlpqaXJXUmg3eGZiNXhuUUY2SFBCd05rQmRiVzVkMEt2amlsRjNJdUxGSmtERjd4RHBHT3o4MXJmNGlrRlNibDIxR3ZrVlcxaVNhVXp5Z3FVKy9GeWI4YjNPYjV1OGRnZ21JMHdwSFRCOHRhMFFyc2xWcXk5dURXU1VpWnhnMDIvL3FueCtqdDJIVno5dUFCL2NaaUUzSWZhWWVlQUVMakNLNkpLdHRmQ3NXLy9lZDUyMms3NTVlTi9oQVZHWG13aWtGQWRJaWllUjg1ME4wenZRV3dtcmcyaUEvcXBTWVRUaVl1dnVXbUhwSzBxK3RRQjZmVFdtNmVLcEIwTkREVEM4TlJoQW96ZFk1YnJYa2RrVWJuUHgzWUJyQWtIakx4TnE1TmF6ZnMwMThhMUVYSi9sZVJMRmFja3JFTjA1SFFyQlRtb1luc0xmbzlSUmtaUmNaRmd2Q1JwQmM0aW5nTXNNV2lFZGxpcGJreHlBSWRTZTlmTTFxNHYwLzkweVo0RDd3Sldyb0owRVhORTVXRm5oNlVEOXJhcGNjM3ZQWjJ6ejM4Vi8vcmtVK1hpSTQ1eVlVMEswd0FJNjlDZFJzUkhMdDArbU4wTDVWQ0FEYXpCcFVlOXVMaGFvSnB0Qk15Qzl5aHRVZEl4TGVta0JqbE8wS01iZEVtUVZpcHJnemdacFVUTGhGbk5HM1AwNmp5N1NGR2ZWOU5PQlRUVThkN1UzTWJnSThjT3V0Zyt0M0lvaWlRN1pGN0dTRk9DTHNKVjNGdkJVL2l1VDBGemdHK2JScVJkRWhvUk4xT1Q3SlpVaFMwMHQ4M012ekhwUHZLRmxmVjNYM2xnZHRQaGZPcHR0c1BhQVNGeXc4L3NJcjM0eW1nWGZQVFYvUFNKWjhxdm4zRWFGeHh4cE1ESUNoT0taV20wUldrY0RDOHJTSGMvelBiaWVTMDRmVVNESkx4WHpWUnFNVEVEbjRDTTQ5OEFzcWpvb3JvdUNMSXM2R0lTYVVDSEFzTTZCaXhWdGtYQlRhTEpQSEVZdTlqSW5ZTXV0bXI0cXVGam9VeHFHMGExd3JHUVhpY3ZKaU5BS0QzRU5LNjRhd3VEQlpYQlFKQkdzS0t1VXlscUNHMUtkM1dGLzIzbEUxOWFuLzNiZDkyN2RqM01LOXpIRlViMVdObGg3NEM5K1M2VXN4QzVPTzdvRDE3SXk4OThqcjd0eE9QOXBTY2VXNUZycTVReUV4RjMwYUdFeEdzUjcxYVJ2QSttZXgxZkJSOVRrMzFJd1ZzZG5EMkJjSTZpb2VBeVZhUzRTM0V0WGJSQjFPbytyUVdrQ2lPVTZCeWtWQ2k4VldaSFFhc3VpcHNtdkNkNDh5Q2drUXllRlp2aE9sT2xPRnFkdkZrRVhSQlBRNGs5bExFVU5SelhSRXB5ZHpadUdwZFAvKzJCNlh1dmZHRDlmd0E0cEN2QUQ1Y0s5M3V4SjQwRDluYlZSYVNMcnFLS1U4SHZuczN6emo1WEx0LzVkSC9seVNkd01vc0M2MTVzRXFoblJ6UzFBaTNnN2paMXNZTkkzbzkzQjhEV29xbmQ3enlGdUtKQWREOHFLalNjckdkV2tSSWdIQzlCSUJST0Y5emtHS0V5WWFDMVF2WkNsU0FXSnp1V2UzYXVVSkJTRlpva05Gc2dMVllLSkJQM0RONWhqWXVEdG1UaHRuV2YzbFhrbW12V3BuL3czcnZXUGdkeGMxNThKZkp3cUhFUEYzdlNPV0J2MVJIbjlPMXZoV05lZFNtdk8va011Zno0bytXNVJ4eFp4MmNUTWpNdlpwSXdRbms4b1NnZXZJQklXY1BMQWFRY2dqd0JHNE4xbTNxQmxhOU1jRkVQc0VEc25raVBNK3dkTkxoc2l6ZzVTUHE5QkNHck5KQmFjUm9rdGVwcEVVbkRBRnRnU2hwckRLMm5vYytxamNUTUIwbDdwOFkzSitVclgzOGdmL0lUaC95UHJ6NHd1d25DZzY4R09kenp2TzlrVDFvSDdPMmhvUmxvL3ZoRnpmTlAzRmt1T2ZscGZ1RnhSK3RwMjQrcVJjT0V3dFNkS1c1Vk1tZUR1THlDRHpMWURQY084VEV3dzIwbWxCa3dSZ2lrYyt4SlpjR0RXQk9ub3VJQkNhSTRUK0pDRW5UZzBHZzBkelRvTXF3VGZPcElGcWVqNkxRUklDRXFKR0Y5VnZqbXlOZnZuSER0alFlNkQrMjZhL1kzUkJjUXY0aDA5ZFh3WkhhODNwNzBEdGliZzNBUnFoK2w5RXQwcDhBUi8vNUZ6VGxMTy8yU25TZlpUeXd0eWJOTzNHN0I0bCtBZGFERG1GSHdJSnFxZkh1aHg5WWpaVkxGR2xhR240b2I4ZUIwM3JRR1lMV1FxT004bjBXUll3WStFYU1qS0ZnTTE2SmEyK2RDVnBnazdsakwzRDJ5Vy8vZnlQL09KVjMvaDdlVmE3ODhuZDRLTlNwZlJMcmk2aWRYanZmZDdBZkdBVGVaWEhVUmVzeVp5SXZmT2VkYUJCaisvam50enJOK3REdHZ4M0g4eEk3ak9MY29aLy9JTWJScGthZ1g2OUk1dVlibkV1M2oyazZKL1pDdXlrTlhCRmFFYUFISGV6eGd4UTBHNzNWSUNGWmV1cnBXT1lYWkRHNDlKRFpldDd0WFZ1VHp0NnpMSGZmdG4xNzliL1p4SzNDd3YraE5wOTJUb3FwOXVQYUQ2SUJ6YzVDcnY3VXpBZ3gvY1FmSHZ2YVZuTDJlZWZIcEo4cU81VVhPSGh1bk5pM2JqbGlrMlRKMEhmVEtuQWxZZDVqVmZ6dXhwMUpwK3pCcWdDUVN1ekhNVm96OTY5aktoQzZiVHRwV3YzcGdqYS9lc28vVlBDMDMvOE9LZmU0LzNzbys0SUhORjNiZCtUUjdqOFcvOWdOMjJuMHIrNEYyd00zbUlGZnNRcTY0Q2JseEIvcmNEd1RaeGtPc2VSNXNlOWxSTEZ6NElvNWVnWjByYSt4VVpkdjJaWnFtQkNIRDFrVUdTdzNxVTF4VWtxdXoza25aczhyczRBU2JUTDJVS2JNZFE3MXo2T252cjcyOTIvKzF1NWg5SHZiekxSektMeUo5WmcveW1jOWlWL1RMQlQ4azlrUGpnQSsxM2lIUHVnazVkUWQ2N2dtNHZwUDhMWnp5TVRNUnVPSEhhVzhFVHQrSy96QTYzRVB0L3dNUWszejI2UUwxUlFBQUFBQkpSVTVFcmtKZ2dnPT0iIGFsdD0iSW5zdGFncmFtIiBjbGFzcz0ib3B0LWljb24taW1nIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im9wdC10ZXh0Ij48ZGl2IGNsYXNzPSJvcHQtdGl0bGUiPkluc3RhZ3JhbTwvZGl2PjxkaXYgY2xhc3M9Im9wdC1oYW5kbGUiPkByal9ncm9vbWluZzwvZGl2PjwvZGl2PgogICAgPHNwYW4gY2xhc3M9Im9wdC1hcnJvdyI+4oaSPC9zcGFuPgogIDwvYT4KICA8YSBocmVmPSJodHRwczovL3dhLm1lLzM3MjU4NzM1NDU2IiB0YXJnZXQ9Il9ibGFuayIgY2xhc3M9Im9wdCI+CiAgICA8ZGl2IGNsYXNzPSJvcHQtaWNvbiI+PGltZyBzcmM9ImRhdGE6aW1hZ2UvcG5nO2Jhc2U2NCxpVkJPUncwS0dnb0FBQUFOU1VoRVVnQUFBS0FBQUFDZ0NBWUFBQUNMejJjdEFBQ09pRWxFUVZSNG5PeTlkNEFsUjNVMWZtNVY5M3R2MHM1c0R0cVZWbHJsZ0JMSWdBQUpJUkE1YWtXT3hzWTI4QUhHQmh1RGhmRG5CTmhna2pFWWJKSUJMV0J5TUZGRWdRZ1NRa0paMnRYbU9QRzkxOTFWOS96K3FLcnVIa25ZWUlMeDk2Tmh0RE12VmxlZHV1SGNVTUJ2cnQ5Y3Y3bCtjLzNtK3MzMW0rczMxMit1MzF5L3VYNXovZWI2emZXYjZ6ZlhiNjVmd1NYLzB3UDROYjRFQkhBUlRQM0kzamhmcThDNy9MdDluUWppRWhDNGkrZCtjLzNtaXBkZ015ek9RWVpMWVhIcFpndGViTUJmNE1Za0JLU0FGeHRjdWpsODEyYllYK2gzL0MrKy92ODFDUmZENENzd2VDNkl6UmNUNWhMOVNmTHBIQ0M3NVFrYlZpMFVldmpDZ2h0eGZlM0NtQndDb2tkak8za0dNUTdlQXg3MFR0VDJQYjNSTXArMHhlaDRaLyt5VytkMjNQU2RnN00vY1R5a0FBSnNnY0diSWJnTUh2OC9rNWovYndPUUVKd0xpM09oK0F0UjZPSzFYYnNXbzdNbkxqOUxsM0NsbmpxMWpMbTVKd1p5dkhSdHowOUlENk4yeW9oWnpZNElJS0FWVUFRaWhBRkFDVjhDQ09BQWVnVThZVlZBZGZQcXVBOERuVEZESlkyNU9STjhwYmgrZHI5TXUyRm50L3Z1NFBLRE94WU5TQUJvYTh5WFFIOUZNL1UvZHYyL0I4Q0xZUUFZL0FYY0haZHY4dUZyenloR2VDSk9tYnlYb3orSEsvTXhHY3MyWXNLQ0hRRXlBY1VDQ21Rd29DUGdxQ0pDQWFnZ0lBRjExb00wQ2tJa1FGRzhRSVFDTVVwREVVdExVQVRvQ0pTRXFRaFVDbE42Y0xyYXEzdXJmYWJrSHJQZ1BxVzNERzh1UDdyckN3QVc2Z0VMZ0F0aEFRQmI0SDlGTS9ncnZmNWZBYURnWXRnN2dtN2tPZXNQSzNZVzUzZU9HcjAzbDNmdXB4czZSMkJKUGlJakZyUUdxZ0pUUVNuR3MxTFFPVkYxQWxjUlZXbWdEbEFWcUJCaVRQd21oTDhCZ0lBaEEvNUVZQVF3SUNCUk1GckNaclRkakdKeU1WbE9aQ1RESUR2TUNZRkNLb0xURHRoYmJjdG0vUFhGN3NISGRWQitGZS9lZXpXU1NqWUFYbkZPQmx6Mi81UmsvTjhOd0l0aGNOSm13VVZiYXVrd2R0NWhkeXVPenM3WERmbEZQTDU3REsxWlptaGdKaklvRlFhbWRLVVhsSlZCTVJTNGdZRjZRQXd5eWRtVERNdDdVM0w0K0NvdUhabEFUMGJRN1l4SXR6dUNrVTRYSFpNVEFEd2RQQW1sRjY5RXBZNERYMkRCRDRTKzVMQXF1VzkrV3ZiMnB6SHIreWhkZ1FKZTREeEJBM1J5SXU5Qk9oMWFDMVVMQTRFVkNMVHd3THdmWXIrL3RiTzMvSVMvZHVIVDdxTzd2dzVFS2NpTERjNjl4T0F5dVArWmlmL0ZYZjhiQVNqWURCTnBEZ1dBU1dDcSt1M0RMNnlPNmozVnIreWVoVldkSG5JQ1RtRWtxNVNBRm4yTHFpK28rb1ROc1FRajVvakp0VGg1YWlQWExUa01heWRYeXBvbFN6bmFIVWVXZDVoM2NnRUFEeThlbmdJUnIwcW5IaXBPRkI2a0tFa2pCakFRVlVDTUNHZ0FnWkNlR0ZhRktYM0ZvaXE0ZCs2UWJEMjRDM1B6MDNMejdHN2VPcjBiODFWZklKNHdQVUduNDAyM0EyTXpUKzg3ekFFTEM5MDlCUGNPcnpZN3EzOHozMS80U1BuMS9UY0FDRkx4dnNqK056c3YvN3NBdUJrV0g0SlBVNTA5Zk0wOTdOMm5ucUlyN1VWbVEzZU42MXFnOExCcVN2VVVWL1V0QnRNR0hsemVtY0xwYTQvRnljdU94U25yVHVDRzhaV1NqWXlJV0VHRkFlZFFZTmIzcFhRbG5TKzFFbHBQaFhwUGdxQ0JxQklBcVZTb3FJQUNVaFVDRVNPRUFpUUJnU2dBQzROTVJNUVlXakV5WWpvY01WM1lUaTZveUtJWXl2NzVnN3g1NzNiY2RIQ3JYTFAvZGg0WXpBWTdNeCtCN1hUVmVxT2F3N0lIWXlwQ2RneG1zYjM4akw5bTRZUCszM2Q5Q2tBSkFMajRuQXlYWFBhL0Rvai9Pd0I0TVF4ZWVURWdseWdBYTUrMDZ2NzVrdEhmNXlrVEQzZEg1eDF4Z05Wc3lGS3pzbG93S09ZRmFualV5Q3E1Ly9yVDVjejFaK0drMVp2SUxzU2p3cUZ5bm5NWW91QlF2Q3ZoQlhDaUpGUUlrcXBVb1ZBZ1hqMDlGVFFxM2l0SWpUaUxaaGdKV0VzREZUSjR4QUlCbzc4aUFsQWxVQzRFYVFBUXNDTFNzUmw2dHN0eDJ4TWpGc09pNU8ySGR1TGFQVGZLMVR1MmN2dWgvYUtnb3RORDF1bVJIZkhvcVJXSU5RY2M1TnJCRDNGVCtkYmlnMXZmQTJBK3pOWC9MaUQrZWdOd015dzJiMGF5OFVhZmVOaERoL2RjOG5KZGFlK0YwUXdtTnhRVjU1M1BVTTRDd3hsWk43b1M1NisvTjg1ZGZ6YVBXclVSN0VCbU1NL3BZaFp6MVFJY2ZQQVJTRkVvSEQxVVBEd0lwWWVuaHlkUjBZSFVvTnVvSUFoRi9OZVRRY2NxQUZBa09pRVJnUUlnT01RUUNCQ3dISWtiRVVqOHBVWklrS3d3WXJta080YlJyQXQ0eGM3cC9YTDlybHZ4dmR0djVONjVnNEN4d09nWWpURktBeUF6bWZFR2N1dmdWdk9qd1Q5TXZQMldkeDRBNW1ydmVRc1V2K1pBL0hVRllQQnFMd2xHZHZkaHE4ODFwMDIrbUVlUFBYeTREakI5cUpIY3UzSWhRM0VJbzVycmZkYmN6Vnh3OUhrNDdiQ1RZTHNkVE9zMDkxWUhwSEJWQUk0RVIwR3BLSHdwYWxRVXdhYno5S0tpOE1vQVIvVlFrRTQ5R0NBbkJJTHVKNkVnaEF4d1M2QmlFOXN3SXNJZzl3UVFNV0xpYXdHUmdFQ0JVQWd4WWdOQWsvQUVTWUpHckl4MU9qTFM2ZEo1WXR2ZTNiajZ0aHR3NDU0ZDZMc0M2UFRFNUIyVjNEaDIwRUVCeUcyRFcreTFDMjllK3JhdC83UUhXQUFoZUNYazE5bHIvdlVENEdiWXhIbjFMbGgxTDU0KzlXSWNNL280UGF3TGxsN2hSRjJ4WURGL0NJZU5MTWZEajc0ZkgzVDAvYkZ5YXBYTW03N3NLdmF4OGlVZ1pDR1ZjWFNvdkdQRkNnVzllRG80ZW5wNEtMdzQ5ZkNxOUVMeFVIcFY4ZkJVcFNnSjBwT2dCRkZDVUVFYUZkTm9ZTUpBNHRNQ2dNYUlrTTNERUlNQU9WQkVXc0pRWUNFUXRRR1FFc0JyREFBYWdLUlRoYkdDaVh3RXZheUx3V0NBNjI2L1RhN2NlalAyemM0UW5TNU10NlBNb05LVDNIZ0QvSGorT3YzTzdOL29scDN2QWFDNEdCa3UrZlYwVkg1OUFIZ3hERjRKUUtDcmdiR0RmN0R1cjdPN3JmajljbU1uTTZVNjBZNVcxVERqL0U2ejNxekJzMDk3SE04NTl0N1FudVcrNG9CTWw3TW94WXNJNGNTajhvNURET0hVaWFORHdRcVZPbnA2Y1ZRNmVoQXFYaFVlSG80S0RiUUtWUWl2S2dxQzhUR0lpRkxURWlZVlNpZ0JFME5xUVNMU2lOVHpHc1NkUUdqU0F6QkdBQlVZUXdnTWhBbXFBZ3VoRVFuMkkwTStoTVR2VmxESHBDc1RJeVBpUEhIN250MjgrdFpidUhYL0FVSFhNdXQyVksyU1l6WTNCeHl5N3hUZmNGdm5YdWsvc3ZNTGNZNnpwRlYrWGE1ZkR3QzJ2TnZPTXc3ZjdFNGZmUldQSFQwZWpoU3ZUaUVXTTN0a3VZemdtWGQ3TkI5Ky9JUGh1d1piQnpzeDcrZmhRWEdzVUxLU1NoMHFlRlMrb2hlRm81ZEtLemg0VkZvaWdVK3BvZ1E4Rlo0YTdEOUpJQXpXbmljSlVoaVRXbFJqK0kwYVhZd285QWhJVUowUUU2Z1lSS1dhc0NoeHFzV2dCbHg4bktSSWhETXpZNUZFWklqTU1ZSTRmSWFGb1NjaElqTFc2U0UzUm5jZU9DaFgzM2dUOTgzTkNybzlpQmhsUm1UanVlV2VFdkxEL2ovM1hyMzdUK2N4dno5OUhINU5wT0gvUEFEL0NUbWVnMm9DV081ZWZ0VHJxcE1ubnVvbkRleUNjVll5VXhUN1RLZmY5MDg4K3NIeXROTXZFcnVraTl1R083bmcrMVRROUhXQVFrdFdXb3FEb2xLSGtnNlZPbmdvbkliZm5UaXB2S2RTNFlMTElRNEs5WVNIZ2doQVpCQnI4RlI2SWpnSXdUYURzaFgvSmFHQmNrRkNTakx6ckJnSWhFckNCQWhHL3pnZ1VoQmp5aUFNRElRU01BalNHaU9nUkNveE9NNVFTY1lrTXByd1BVWUVEUHhqSjh2RVpCWTdkdTNEdFZ1M2NXRlFDTG9keWEzMTJnSFFzVmF1SDl3dTM1Kyt1UHFYN2Y4Q0FMZ1VGaGY5ejRmMy9pY0JhS0lBNGRnRFY1OVhYVEQ1RGoxNXlVWlhldGNwTGFxcXRKemZ5L3VzT2xsZTlGdlA1dnJWNjdGMXVFUDI2UXlvaEpkS0YzUW9DMzRnbGE5UXNrSWxLcVd2NE5RSEx4WUJiRjQ5U25WMDlPSlZvVlE2cUFBS1ZRYXdHUlVTVWVVcXd2OEVVRit6SzBwQUlnZ1o4U2FBa0l6d0RMWmZKallKU0JnSUtKRjdpYkl1U3JnQVNBaEFBNGxPc1RVUm92SFRpU1ExUVJQZ0dwOEkrRFNTQVNRRVJDZnZ3SHZGMXAyN2NkUDJYZUlsbzgweTBOQnpURExiOThBVmMrL3Z2bVAzaXhiMkx1ejVkVkRKL3pNQWJLbmM3QStQK0RPNTIvZ2xiazNYbWxsMTFtYTI3Ty9HS2orR2w1eitkTnozYnZmajd1cUE3QnNjaEZwS1g0WWMrZ0ZLZGRMWElZZCtpREpLdVVvY0tuVlNxWU5UaFJjRk5kaDNKUjJjT25wU0NLMXR2a1MxSk1neGlNQWd6TVFLRU5RZ1RNaDJZVkRaVlByd2ZxRUVlcy9DMHNLSVJXYXNNSUkxQ0QrRkp3VlVRSVRHR0lFSEphR0pGZ0dka0V5TUdvUm5oSUZUaEluU2swSURBd2JoaVNCTGd3RXFqQkxYaVBSNlhmVDdROXg4eXk3dW01NkY5SElSMG5FQ1RtQjc1a2Z6MjNqTjdMUDl2Kzc2UExqWlFyWVErSi94bEgvMUFEem5uQXlYWGViR01iN1MvZG42Znl6dlBmSTRWVTlUR0tvNGc3bWRlUGo2cy9uaWV6MFhkclFyUHg3ZXpNcDdLRHlHTEdTb0JZYStZS0dsRkZxeDlBNmxWcWpnVUZIaEdBQlllZ2NLNGRUUnFVY0ZGVlZQaGFLQ2d4TGlWRUVxSVVZQWdRTko5ZWpyQUF1K0w1WHIweWhCOWFnd0ZBQVlsekdPU0lZT2U5SkJ6bzdrVXBnS00zNmVzNXhuUlM4ZDVtTFVVSE1EMCttaUl5T2NNQ013a2dmcFI0aG5SWUFRTVpJRkNJR2tXakVpWWtJYXRvallWa0kyUkNBYXlXeEFEQXhNTkQwQndJZ05CSkdxZFBJY0U3MGVidDk3Z0xmZHRnc1ZWYVRiQWFFVlJreWVIWERlZkgzaFZlVS8zdklxQ0lBL2gvbWZvR3QrbFFBVVhIeU94U1dYdWFtVFY5MXQrS2psSDlSemxoenYrc1BLYUdiZGNGckdCMzM4eVQyZmg0ZWMrQURjVk40dWg5d012SGowM1VBSGZvaUtwUm1pNHJBcWdsZkxTZ3AxV25oSFQyY2NpZEpYOEZSVThLSUJrUEJRVmFvNEh4ME4xZWlLR2pnb1o2cFp6TGdEc0pXbmMwTnpiSGVqSHRYZGdCR01tcnN0T1oxbmpaNkFNVHNtV1RhR0pka1NkRXhHMEtCak11VEk0YUdZMXo3NmZoNUR2OEN5NnN1TmcyMjhZdUVxN0MyMnk3UU9jT1hDOVZoQW56UVdOSm1aNmk1SGJydm9NZ1BWUTFWQkExZ1lpZ0ZFbzdJV2srSjdrZDlPZm9tUmlNUG83RVRMRVpIckp0Q3hHYnJkTG9kbGhhM2Jkc3ZCUTNPVVhoN0VmVTR4c0VhK05udXBlKzFOendXd3YwMkIvZXBBOGF2Nm5tQkhjZlJCcTUvSkMxYTkwWjg4T3NZWlg1a3N0OFhCVytURWtRMTQvWVArQW1NckpuQjEvem9JQ1EvbHRKK1RvUTVrNkFvdFVjcEFQVXBmU0VYSElqZ2NkTjVMUlllS0txVTZLRWxQSjE0VUlPRkpPSGlxcWhoam9RSWNIQjdpVEhsQXNxTEEzYnJIODhqZTRYalFzZ2ZnOUNXbnlMcmVlcjhzbTJxSkhoQ0lodHQvZlNuQ3ZLYlgwd0d5ZFhBNzlwYzcrZW45bCtHNithdk01WE5YWWxjMVEzWTZNdFZkZ1pGc0hBWUNwWU1JVVlNSmdTOE1UaktTZVpENDdKU09LQkF5ZWRjU1haMXVsa05KV21NbE00WUhEc3pnOXAzNzRLMGhWSUJNbll6bUhibDg3aHI5bHowUHg5YVoyM0FPc2w5bGxzMnZBb0FHQWoySHlMNzVmNDU2WlhibTVKK1ZLd1d5NEwyUjNKUUhic0Zqamp4ZlhuSGVIMkt2VHVQV2FqczhLeFphWWFnRjV0MDhobHBpNkNvcHBFQ2hEcFZXcU5TanBBdk9oWE53b2xMRlVKb0x6Z1VyZUNFOURDeThHTTZYMDVncDlqSDNsSlB0TVhqQWl2dkpZNWMvQXNlT0g0ZU95UmNOdWxJWEV2Mml5UlhZdVRCajFNRDFVVWloQ01rQUFRZ29JZTA2UnVtaW55WElGMysrN0NuMjRPclpIK05qZXorQmIwMy9nTmYwYjBRNW1tRkZiNzEwVEFkZUs2aDZXckVwbkJkMmdSZ0NGRkpnREdnSWFNQmpKSDVNVU1jRU9qWVBJak5rVVVpdmszRllsTmkyYlo4VUF3ZkpqUkx3TXBYbDVvclpXODFsQ3hkV245djEvVitsYy9MTEJlREZNSGdsZWFKSWZzdUxqdm1nTzMvcG96MUtKL1BlcWlXd2J6disrQjdQd3VQT2ZDeXVMMjVoWHdkU3NwSjVQK0RRRnpMd1F3eDhpV0ZWc0lnOFh4a0k1ZkJEenpMOEM2Y2VFSWduNmVERnVZb0tBNHFWUTlWK3p2ZjM0REF1NTZPV1A0aFBXZk1FbkRKMXFzbGg2L3V2V01YRkRUR0pGTnFWUUlVazc1T1I4Z05Gd3RLbkZPbmt0NUpSTkFWd3Boa21TTlhndEFpQVRMSjZtaFpjd2U4ZStoNytkZnQ3K0lrRG41TURkc0JlWjYyTWRwYkFhaVZLSlVJK3JPUWkwZjBPN2tqd3lpVmFrWUlnS0FQM1kwTUVob2drandBd3hnQUM3dDU1U1BxelEwaWVnVjY5V1psYjNOaWYwUS92ZXhvK2QrRGpNYW5obHc3Q1h5WUFEWGd4SUpka3ZaY2MvVEYzM3JJSG8rOEtxNlpUK0RrWm1adkZxOC85VTU2MTZReDhiM0NOaUJvdGRDaHo3TXVDRzZEdkNoUXNVUGdTQTFld3BHTkZaMG82VnQ0aGNYNlZlamo0R0wrQVZONFJZa1JzamdQRkhzejFkK1B1M2VQNXpEVlA1dU5XUEJaVG5jbEFKVU5GcVJLSURVbXNTWndTSXFXekpHV1lEUDAwWVJxQ1pYV1NmdnBiR1hJVEVPV1VDV3lUUkNzazhIc3B3aHdwbnp3bXVRTEE3c0Z1dm0vSCsvSE9uZS9IdGRWMkdSMVpKZVBaRkNzdEJTQnlFd0dJbXVJT1F0WklEVFVRSVRsYlRDSXVBUUdzQ0tpRUFTZzJ3NEc5YzVpZjZRT1pGVU02TE1zeXVibFBmblQvMC9YVCs5NkQzMFdPdDZINkpXTGtsd2JBWlBQWjdvczJmY1E5ZU5ranNPRExEck5zTUppV1ZjN2pkUSsrUk5hdlhLZlh6TjhvUGlPRzFRQURMVERuKzFqUUFRcGZvdkNGRExWaTZTdVVXcUdFazVJS3J4NU9QYXNReTZVRzRsZ29BaU01WnQwTTlzOXZ4VDN5NC9qOGRjK1J4NjU3REhQa0JDQ1ZsaUg4WlF3TWpOelJySXZZQVZyLzFsRU1CSCtBQVZLVUZBWkJ3bGY5K2tpdklGR0RRbGtNMkJqUlMwRUpPbFhBQ0xNZ2xhWHYrM3JwMWt2bHRkdmVJTmNVMnpFMnNRRzlmQXp3VlNCemFpNVJXckdOSUFrWnY4SENOaWtRSUV5UWxIRi9DWXpOWkg2bWo1a0Q4NURNa2tBbFM2M0YxcUdWZCsxOWtsNjI5LzM0cHpOelBPZDd2elFRL2pJQW1DUWY4cGNlOVdHY3MvTFJiamdzckhTNnJyK2ZSK2lZdk82Umw3QXpuc3UydVYyb3hIRUJBd3o5RUFOWHlBS0hITGdoaHI2UW9hOHdaTWxTZzlwMThGSnBrOEhpeUJCV1V3OHh1ZlE1d083NVc3aEpWc3NmSC9ZOGZkYUdad1dIRWg2T0hpSVpEVUtpcUNMUmE5RTdhaUFSRjAxanloYWdWQXFDMzVtSkRTKzc4L3dSQUJ5RDl4MjRRUkVyRnBTWXBRb1RraFdpMkVwd2ovaWhBbEQxOFBEb21neUE0WnpybTdmZStsYThidXZic010UGMrbmswUUFoemxjVVk4VkszQkNSR2E5amZ3Q3RtR0F1QkpqU21CZytpYmROQ214bTBaOGJZdTdBQXBDYlFGZ3V5V0J1TGNSZmR2QnBlUCt1OStGM3o4enh0bDhPQ0greEFBd0pCUUtCNzc3NHFBLzRDNVkvWHVmTHlzQmtibkNJeCtaTHpEODg3Qzh3M1JsaTczQWZsTW9GMzhlQ0g4ckFEem5Rb1F5MXdzQVZMSHdsQlNzTXRVVHBLMWIwY1BEaVZWTXVpMVJLS0VYRlpOZ3oyQzVaT1kvbnIza21YbnpFaXpHVlRWQUI4VnJCbWh3cFc0QVFwYXBZWXlRdFRWS3ZIa3Fxd29QbzNjRXBTZGVDTHpGd2ZTbmNnQTRWaEJRakJzYmtITFhqbU9xTXQ3UjBkRW9CRG4wQkNjUUhER09BcnBHc1RHOHdvS2dHTGV1ZzZKaE1BSERYWUE5ZWRlMWY0SjE3dHFBY21kTEprZFZHZlNHZ0pyVWJuQ2FSaEd4WVl4QVNlSUo5bUVYQU02R2ZoaVRFV29OeVVHSCswQUF3UW5nRmxtZUtQWlhnQS91ZmdJL3YzdkxMY2t4K2tRQVVmUGtjaS90ZjVub3YyZmgyOTZDVnorYkJxc29rejR2aUFEYmxFM3pESTE4bE05TG56bUt2S0pVREhiTHZCN0xnQnpMMGhRNjFOQU90VUxpQ3crQmNTS0VGQ25WMDZxQ2k0bUxzVnVFSjVCaDZoMTN6MS9OQkUvZkEzeC85TjNMaTJJa0FnRklyWnNZbVNqZmFZWUFvcVNZRXlBQkFvU1JEVU1LSVRZdW5zOVc4SENqMnlRMkQyL2pWUTkvRWRMa0x1L3lzM0Z4dDQ5NWl0eXhVQzZqRUVmQVFKOUl6WTF6Wlc0bGpla2R3dmF3MG1lM3l6S203NCt6Sk16RFZXY2xWdmVVdDdVNVRxWU1SQ3lQQngyNm1VU01iSEp3TUJlRFUxY1ZRM3o1d09mNzBtbGZ4eS9QZmxwRWxSNkVqbVRndEFGaXlsbjFCTDF0SmRvRWcxcTJFUElvNmk4SUUyYStDanMwNEhKUlltQjBFTWVyaFpha0ZkaFNROSsxK2pQLzhnVS84TWlpYVh4d0F2M3hPaHZ0ZjVqcS92K0dOZlBTcTUvazVscG5hckJ6TVlxMjE4ZytQZmlVS1crREFZQm9MTXNUQUQyWG9CdHJYd2d5MERORU5YMkJJTDRWV0tIM0ZrazZHckVLV3NucDFVT05KZW5YU01WM3VLL2VqV3BqQnhldGZnRDgrNm9YUnhxdEN4TlJZQ21ocVF4MUp3c1N3RzFXczJOcDhBb0JiRm03aGx3OThDNWZ0K3p5K1cxMlBHNFkzaTVlY2tCTElqTUNPUTB3WHVjMllTVlpIYzAzMG1DdGZvbko5d0EwRXZnQ1FJL09LVldZbDc5YzdrNmN1T1VVZXV1WUNucnowRkJNSEJrY0hRSkNKRU1FMFNKYWp0RDF0SmFIcW1Oc09BTWdicm5zTFgzYlRYNk0vTW80bEk4dWxkQU1pQVk0aUlHR01yUmxzZ3JSaWhBZzBVazFzaDBRSENDd3pLK2ozQ3lsbUM4QVl3a0d4Mm9qNTBYeHAvdkhRQTkyMSs3NytpeWFyZnpFQWpPSjUvUGNQZjZsN3lKcS9LY3F5a2hJWnBlRHltU0ZlL2ZpWEMwY05EZzZtcFZTSEdUK0xVaXYwWFltQkwxaWdrTUpWS0xUNXFiUkNxWXFLams2OUtCaitWc2V1SGNITytSdHhRcjRlN3p6aExUaGo0bTZzdEpMZ1dOaEEzVUZDcEJacGdoSENjT3JRTlozNi9yOTM2QWQ0Lzc0dHVIcHdEYjgxZjZVcytBS2FLN3ZkRmRLeEl6QUFyR1FRQTNpdlFMVHZnby9MdGk4S0FCUmpRNXFwQWw0SWlNSlZGZWFIQjRDcVJGNFpuclhrTkhuRXl2TjU0WnJIeUtheFRXa3NyT2pFaWxFREk0cFE5SVJJTndad0tWVWRBSkhNNVBqaHpMVjR6bmVmeThzSDEyQnM4aGhRQy9GU3ExZ0VNd05KM3RWV1NIQ2hJcmNZWmE5RlNJZ1FnUXptQy9xaEJ6SkRlS1ZNR0N0WER3N3B4NmZ2aDYvdStWRk05LytGZ1BEbkIyRGNFWk9QUE9MKzVlWmxYeWlYR05XRjBzS0MzWVA3NVM4Zi9VY3lzWFNDdStiM0FWWmszdld4NEFvVXZzREFsU3haU3NFU1ErOFFJaHNsQ2wvQnEwZEZIK0s3cWlHMVFDR2VPWGZOL0pnWExqc1A3empwcmJJa24yRGhTK25henFKaGFmMWZRMEtocXBLYndMM051bGwrZk0rbnpUdTMvd3Urc3ZCZHNKc0IzVEZNZHBjaWd3RVVVTHBJb1FFaDRCQjZESVVyeWhrbWg3YzI3T3VFTFNnWnRCd0ZGQmlUQXdKNFZjd01ab0ZpbG1NdWwvdVAzd05QT2V3cGZPUmhENVVST3dJQXJMU2lNUlpDR0lpaFFiTGpLQUpSVlJXUGNEOExic2cvL01ITCtMYmIzNFh1OG8xaUlIQlVvUUNaMU01TkFoK0FtTGdZK2M2UUtrRmtZZ0FGUlFMSFhTNVVwTk5vd2FySGlqd3pYNS8vbnY3cERmY0ZNVXgrOTg4TG41OFBnSkZvN2h3M2Naejh3UkZmOThlTUxzUEJTamxpeE8vWklTOCs3MWs0YnVQUjJEcS9Rd0J3NEljeTFDSUEwQlVZcW1PSkVxVldNdlFWUzFaU0ptNlBIbzZFaTVRTFJLaWladSsrRy9qSDYvOFAvdnJFVnhBQUtuWElUVVpWR0RFaFl0V09vWGw2RWJGcUFFeVhoK1NkVzkrRGQrNTlMNjhwYnhHTVRzcDRieG1zQ0FBWHZHbEZNTmtsL3FzaFN5WDRydHF1UGFxNXdxVDRRcGxtNElGVHY0UWtqdXExVjBCb1JheUZkeDRMeFY1Z01PVHBuZVBrbWVzZXp5ZHZmQ3FXZFpjaTNGdUZMRGdoTFRzeDFRS0lPRHBtUmdCaytKZWIzcVBQK2VFTHhVeXVGNWlPZURoa3h0UU9TUEpHUXJTR3Nhd0ZnRXBNWkJER05Cc1JBazZGZmxpbHpRUUJuRm1TWmZqWXdTMytqYmRlaEVzM1cxeTA1ZWN1ZXZwNUFDamd4U0p5aVhaZWVmeTMzVDBuejhLK3d0bVJqaWwzM3k0WG5mNWdQUGlNKy9LV2c5dWt5b2krRHFUMEpmcXV3Q0NCelZjczZGQ3dsTkpYRE9FMUQwY25uaUZscWxKUFFXZ0pzUC9nai9IMjQxNkhaeDcrZEhoVWhCb1JZd2tvZ3RvS2x3SEVRNm1xeUUyR2dTL3cxcTN2NU50dWZ4dXU4enRFeGxkalBCOEh4SW56Z1Yyd0Jva1ZaR1NkSmRFa0pDakdDRFRPZDl1RHJaRVdMdGIvQVpKK1kralhnZVNxZWtXb0taRmdxeXBGK3RVTTBUOG94L2tOL3ZjUGY2Yjh6akcvTGFQNWFJandHSkVNSnRxeG1oaEFFYWc2RUY2ZDZacWVmbUxiWjdENW0wOFZ0M3lsMkd3Y0lyNW1HalhCSzVvbnl1aXBKS2FRd1FaRlVzK3c5SldIbGxVQXNBTTVJcFYwME1IN2R2ODkzNzNyeGIrSWFNbC9GNEMxeHp2NjBxUC9vYnovMVA5eEJ3dVhaVjNycHZmaEh1dU9rZDk5d0VXOCtkQTJPQUx6SEtCa0pZV3ZNUFFGQ3A5c1BWZEx2ZEpYQVhBQmdGU29PQit5a2hXQ21ZUFg0ajBudlZVZXYrSHhLSDNCM0hTRWRTVXU2a1VKWlVRcXVXUUtRTDY0N3l2NDQrdGZMajhZWG8xOGFpM0hza2xRblNnY0VESk5FT201OUU5TnppeUtqQ3llcVlTblJjUjFqSDRnV3B4UkdCcHB5NGhZSGd5SjlxT1NnREpVaHRndStvTUZWRE83Y1ZMbkNQemZreTdCb3c5N2hBQmdwUjZac1hYQXBzbWJVaEFHbFN2UXk3cjh3cTZ2eWVadlBCWFQ0eGxHZWl2RStTRVp2WHVHR1dOd2FxSUpRVFFTSFJKRDJSSTRkaVdwQ2ppRy9qak9BMHZFbXhsbTh1OHpGL2tQYnR2eTh6b2wvejBBUm5lODgrVERMOFRqVm0wcFRlVk1IMWJkVU5aSnpqOTY1Ry9MN3NGQkRxc1NCVXJwYTRIU3U4Yko4Q0dwSUNXU092V3M2TVdwd2lIRWVJTVJEbmkxMkxmdkd2M0FhVytYeDYrL1VFb3RrWmtzaFZTaEpNVVlRbFdNTVNqVW9Xc3lIQ3huK09mWHZFTGV0dSs5NHBhczRKTHVVZ2pMVUZnRVVFeE1KMG42MFFDMUxRZldCbnFxL0VpeHV2aC9DV0czV0R3RXhzNXZLVjhhVVhDR1dvL3dIY2xjVE9rc2NlR1pxRHdqcEVKb0laTHJmREVqbk51THg0dy9FUDl3eG11eFllSnd0RHo4Sko3RE4ydUk4WmErUU1kMmVjV0JxK1ZSWDM0c2RvMVc2STZ1b1hkRnlLVUk0cDN4bnFOQWhrRGplTFRlVWpHMWdZUkNvQlQ0T0ZtbHA2enUwbDVUVEx1LzNub3Y3Smk3OGVjcC9UVC85VXZ1NGoxZmdZNmN2bndkN2puKzl6cGluSmtYQVZUeWhRVTg0MzZQaytsaW5qUERlVm53QTh5WEEvYXJBbjAzUk44Tk1mQkQ5SDJKd2xVb25HZXBqcVZXVXZtS3pqdFdxdkNxVUFYVkdPN2JleFhlZGZJL3lPUFhYOGhTaCt5WVRsanFRRlRVYm9FYXdFZndmWEhQRjNHdmI1eU5OMDkvUUxyTGo4WllOZ25uaGxBTmFTdUVpZW4zcUtsaXhsZ0lHYXgyRGNzUS9xWUpjZHNJVVVid0liNWVtemhLRXB3TTdJWTJid0JBVGZZZ3dVQ2l4NzhsNXRBWUtrbm5DaG5QeG1WOCtUSHk3OFhYY2NaWHpwSDMzUHdCNUNhSE5VRHdoT05taUhrS0NrVm1NNVJhNGg3TFQrRm56dnNFait1UG81alpJNWwwZ1ZEU2w3anFGRkdVMnFKUVNWUnBzSU5KUU1VZzF1ZkZPZ1FnczhMOXBlcEozV1gyV1d2ZUJnRncwbi9mbFB2WkFYZ3BCQUwxRDEzeWQzck0rQWJacDJwc0pucm9BQjUrK3JsWXRtUUsyMmYzeVlBVkZxcWg5S3NDQTFkR3lxWEV3RlVZZWw5THdNSzdhUGRSSEZRcTlTQVZ4dWF5ZC9mVjhwWVRYNk5QMi9nMGxGb3dNeGs4WXJDVEZNQUF4bERWU3dZRGF6Szg1c2JYeUFOL3NCazM5OVFzbVR3SzZpc29uS2d4RW56RE1PTmhqZ242V08xR0NmWlNhUEVTd1JaZmg1QVBFK05kVWt2TkdBTlRsU2pKQXNoVVJhQ0NWR09DK0ZJZ2dCczBOZjFSQ3lNU1NraWs2TVRSbzNKRGpJMGVodWtsNDNqYVZjL0I4eTUva1F5OUlqT1plSFZoOFl4QUdSeGtBMkZtTWltME1xY3VPeDVmZlBCL1lHT1JZVGpZTDBac0dwQXdPaDVwYzZUN2lTcEZHaTRSalIxaUpGR0hBQ1hUZVZmb2ZjZk95WjU2eEV0eEVUdzJ4ejZHUCtQMXN5RTM2dnVSSngyK3VYak1za3ZWb3NvTG0xV0RRM0xrK0ZJODgvekhZZnYwSHM2eGtLRXZXQVZxUlFwVXFMeER4UXFWS2p3VUZSMHFEYVN5aTNsOENxQlNEN0U5SGp4d05WNS81Q3Z4Z3VOZmdNSVg2TmlPc0JVeGlKUUVLempreUxqZ0IvTGNxNTZQZHgzWWdyR3BvMFJVZ013QkVUakJid2pXVjFEZnJPYzQxcWtoYWtxUWhBbTVMVWlCZlVueFV6UUI1UGhwNmJlNGlNbmZqVXVjZ3JPTUlBOWlxNDRDSTNETlFlZFJhb291TFk1NndrSmdiSTc1UTdmd1BwM1QrTDZ6L3RVY3Z1UndsTDVpWmkxVUdZajNhSVlvZ01xWDZOcU9YTG4vR2ozblB4Nk1oYVVUSXRLRGgyYzBQY0lVcHB1S2FpQWFFY0htUzgrMXZHWDRGR0drWXNxbzNGb00rWSs3enNhVjB6OE11L1JuVThVL2l3UVVYSG94SjQrWW5ISm5qTHdHbzViU1YrTlJJbmZFZys5K0xuY3Q3RWZmRnhpNElRZStsTDR2WlJCck9JYSs1TUM3bEVTSzBudFV6b2xURCtjOXZKTE9PNHJ0NE9DQjYvRFN0WCtBRnh6L0FsUmFJYmM1QmFJR0VOTjRnYXkwUW80TUI0dURlTkEzTCtDN0RuNVlwcWFPazJBV1ZXQVQ4dzhhUitzT1ZselVYcGRSenRVMllSSUpFV3BSVlNhcHFRajV4NHYyYjUwVEVJUWtRdldHSktzcjVBUkVOd25KTHRSYUFpbE1ZMy9GYjAyWitKNWtwUVhHbG02U3I3dnJjYzh2UElCZjJQMTFkR3d1bGZlRXNTQlVHRld4a05LMXVaUys1R2tyVHBKL3V0ZGI0US91Smt3WUdXdXgzcFJPSlduSGVpZkdnU1ZQT1ZIYWlmYzBNRGlvNExHOWNYbkUwcmVDRUZ5YURKcGZCZ0EzdzBBdTBma0xKbDdxVHBrNFFxZWRtdHdhblpuRk9jZmZIZDJSamh5WW4yT3BYZ2Erd3NCWHdkdjFEa05mb1ZBbmxYcVVHdFJ2NVQwY2xDNVVxTkdyaDdVZE9UaTlsZWVQM1JkL2RkcGZ3cUdpRGJFQlVWVkJiREVKQkRzb056bTI5M2ZnZ2Q5NHNIeXp1bEdXVEI2SHdnOGl3QXlRbXJaUW91WkI2Q1FVYXlCWks5aG94Mm1hOUZCNGtkUWtrRlJwMlAxTVhvV0d5dlZnRThiUENRc21GQnBDS0pva1hXZ25FeE9sQXo3VDc2aFo3alN1TU9BV0NvVUNWdzB4TXJwZWRpL0o4TEN2UHdxZnVQMlQ3Tm9jM3BWUi9ocENVNG1ub0dNekZLN0FFNDU4bVB6MXlYOU90L042WkNZTE5vUWFpZUNTUmN3cHBVVmFwdzNJYUNOR01GTEMzMFl5VEt1VGUwemR5enhwN2ZOeEVUd3UvZG5NdXAvdXhSZkQ0RkpvNTlqeDQzR1BwYytEODk1UXhBK0dzbUhKQ3JuYnBxT3gvZEIrZUtnTXFvSkRWNkdvU2d4OXlhSnlMTDJpVkIvUzZKMmo4eHJUNWhXVkQ4MHdJRWJteXhrZTVTZjVycnYvSXdGbGFFQUF3aWlNTVdCSXNZU2prOHhrdUdYaGRwejN6WWZ5KzlpQjBkNlJLTndnVnVmRWpWNXY0bUJYSnowWlVxd0N2WlBzdERvekQ0QktzL0dUb0loc1NhMCs2L1dxay9CUlcvSHA5ZUc1OE9HcUlWc1VHdjhKZUFOYmtsZVRwS3lES1FHY2xPUVVHWHBmc0plTkNsY2Noc2QrNDBuNHhOWlBvcHVGRkg2QkFpWWt4U0tLcVR6TFVXZ3BmM0xxQytXNW01N0RjdCt0a3FFbkVpY1h5ZUZnQkYreUV4cnd0YmRoODI5a0lUQ2sxWTZvdWNmU1YrRDA1ZXZ3QlBpZkdsYy85UXRQZ2tCQWZjTGFWK3Y2M2pnT2VUTDNJdFZBenp6eEJPNFpIdVJBaGxqZ0VBc1l5dEFYVXZpS1FmS1ZjTjdGSmtDaEpRYXBvY1pXVlZRaGxSZVdDZzczN1pEM252bE9XVGU2U3RRcnJPbVkyTEtubmdTbkRwbGt2RzErQnkvNDFpTjRZK2VRTEJrN1RJZzVHQk0wYk5DcXNaZGZYRmxFNTRFaERabTE2cE1JT0xadnVMSE1nM29FWU9xMGZOUmFMQVFPbW5WQlM0a3FJZ1JGbWx4RGdxS3RCWTdQYVBxdUlHYW9NVjhBQ2N6UmdTVkJxOFpKU1dzc3NPb0lQTzRiVDVSUGJ2MDBPNlpyS2wrWmdDTlNRNnhhQUVNTFN3L0ZxOC8rVzV3NmRvSldzN3RvSWxKcmV5RFpGL1hOcEhIR082WEdIZGpxaStnMHZIZTJVbmRLWjRXOTkvamZRQUZzL3VuVjhIOE53TTJ3ZUR6ODJDTVB1eitQR1hzRXB5c0hHTXVGZ1J5K2VoM0dweVo0Y0RBbnBYY1lWaFdHVlluQ2xSeHFKYVYzVXFxWGtwNHVGaEg1T3NTV1ZLOWpabkxNSDdnRkx6Nzg5M2l2VldlRkVKVE5ZalJNcUlHV0FWV1JtWXd6eFF3Zjg0MUh5MDFtcjB6MjFzRHBnQkFUVE9nSU5FbjJOUnRMT3BLd0VJYXVWRFp5ZU5KUWQ4Q2lIOVJtWXZRVm9xUnNMRWdGSXYwWHRHV1VlR0VvVEU4MEFiUUdxNnl0UFpLa1NqMzBpSXJhM1dhMERxSnNqeExVd1lnVnJ0aklDeTk3b3Z6SDlpK3dhN3ZxZkFuYjBQSUVnTXhZVUdsR1RVYytjTTQ3WkhMb1FGYVF0aE1pVWR0UXdyY2xNWDRuTERFeC8zSDNVT0JwVU5IanpMR25abWV1dkE4KzlOTjd4ZjhWQUFVbmhpOHFmMnZKeTdreWd4bEdaYURBY1ljZkxuT0RCVlNxZ1dJSjFXc28xS1AwNGFmeW5xRkJrSWNxVVNtRDA0RVlyNGVWaFlWOWNweXN4NStmOXVmaTFjT2FIS3JSakEvQkJBT2pwQWswL1pPLzhTUzUwbTdsMk9oNmxINFlSUjRGTVg4dXVoenBEdUpqd283dGlLZklnZzVrM2c5UTBpTXpuYkRCR1k0UlNlWWRJVkVGaHJUb0tLVHF5RWU5UHVFVlNONU9NdXdVa0xRTDBoNW9xZVptZk5GRWlPNW5TbGdPT1RBcDNKY1lra1psQ2lGUTcya2xoMSs1UVM3OHl1UHhuYjFYb0dzN0xFTjRNV1pEcUFBUVl5eExYL0w0WmNlYlY1eitDdXIrYmJSWkp4R2F5ZkVJZkZMZy9tSk5hSXBIMXM1LzJGMUdCTVlJMUFBd0JuTkt2N0VMLzREeFMwQ0VvOHArYmdCdUR0WHlZNDg3N0Z6ZDFEdFBaN3dpc3huN1E2NWRzUUtqb3lPWUh3eUR1cTJxUUM2clErR2NWTTZ6Q2paZWNEZ1lDR2F2R3J2TEszdzhhY05ONytXYjd2NDZUR1FqOE1uTk1LSE1OU29xZXUvRklzT3pMLzhkZm1yd2RSa1ozU1RPRFlKd0NxZkgxUDUvY3VlVUFucENrSWtDTW52d1ZtYnpoM2dzbHVBWU44NVZmV0J1Lzgxd2NCVGErRTB0Y0JGUUJrYW50cjJqazVKUWxyeG1SZ2FSQWxFMFNmYUtscTNIeFBZSXFDRTVWQlBFS0Ewa21iRGVBbm5ZRUxWRkZvSVdCcW9PMW5ZNHYyeVZQUFR6RitLbXVWdVJXU3NoVDlxdy9zQklWRmRhOHNXbi9vSGNjK3BleHMzc2hqVjVpeEZQTndva2tiaG9NdW9yMm9tcDh0bUQ4R0l3cTQ1M0h6MFBEMS8zQ0Z3Qy9XbWs0SDhGUUFCQWNmTElIK200QlFvZmFVb3ZHOWF1eE94d0RuMFdNdkJsU0ovM0pZWSt0TWx3ZE9LOUQrbnpJYUUwUkRoSVZGN2huWWZRWXVIUXJYajZZVS9BK2V2T1JhVVZPelprZjVoYU1RS1Y5OGh0enJmZStLL3lqdjMvWnNhWEhRdDFjNENCK0dqVWFleHNaWUk0QVQzQnlnTyt5NkkvbzJiM1RyNXd4VFA1alRNL3FkZmY4M0s5NGV3cjhQMTdmQVYvdWZyUEtEdDNveXFIRU0wSkQ2aTNUQ0VSa2dqNlAza3lTSkl5REREWmNiWEhFb0JIMWRwa2lxSUlNWHRMa25kVTIvZ2FYWjJZS1pwYzY0VDFGQzlPS0dWc1owMURZVVp4cGtTbk40b0RvMTZlOWVYZlJScFZxUG9EQ0NFMEZHRkpzS2ZsVGZmNmEzUU85UWtIU09qamhNYlRpb0JydW41RTNMSDUwVGd4RXUxQWdhRHZnR1U1NUc0akx3UWdDVC8vUFFCdUR1Mjd4dTYxOUdRNWN1UkJuSGNVTVJtSEJWWXNXWUxSOFZITURRYUJWdkVPWmVWUXVLQjJuZk4wRFBsOEhpcE9HYnFRdGhvQ2tVRHBDeXd0bCtDUzAxNGVibytKR1dEdFVWYnEwTFU1cjl4L0ZWN3l3ei9teU5SUmNLNEl1YzVocDBhVFMrcjRyVVkzMWtxWHhYQWZOZzV6ZlBIc3orSjFwNzhXZDVzOFJTb1ZLQXhXOVZieVphZjhFVDUreHFXd0IvZUJXZ0pxYXdjbEFDWUJMTFpqYTZFTmJPeTRjTVZYTXhwVnRlNU5TOW1TYUpLQUdWQWNITmRJbWdmSU1IMXZFa2gxQ0NiWWJsRmtCblhwZllIdTJCcDhiZTRLZWRIWFg0WmNNbmg2cGlVV0UrYlRtRXdxWCtITU5XZmdhY2MrbVg3L1Z0aThVdzhha1lGQ1F3TWtCQzVXcVFKQWFXQkRyeHQ0QWpBVzgwb2NNM3IvN080cjc0WEgvOWUyNEU4RzRCK2NFd0J3enRUemRFMDNSMGtQRUNnY05xeGVpZmxpaUlJcXc4cHg2Q3NNUTJJQksvV29TS2xDMnpONFpWSTFvbkhqZUZWWTI4SHd3RmIrd2NabjhJang5U2g5aWN4bTBRaUpOQVNVQmdLdkhzLzgraDlncnBkRFRCWml1aldER2laSlk1dmxjS2lDUW16TzRmQ2dITlh2eVZmdSszbmVhL21aTEh3bGxUb3h4Z0NxSUNpRkwzREIrZ2R3eTVudlFUVzlYVVNNU0R5QnE5YXV0WHFzN2NKYU95Nmlhd0tVb0RSUkpTTlZrU2NhTjRiOTBuTEdEUk1qMjJDSUpjZTlsTWpLaFBYNFhaSEtqbC9HMEtJTGhMQ3FTblNXSDRNM1hQOVA4dEZiUHN2YzVDaTlpOEhjVmhSSkREd1VMNy9uUzgzU2NneGFGTUhEWjlUckNYTk51V2ZrckZvcXVrN0dqYS9TeUNrdXFNZVJ1Wml6SjE4QUF0ajhuNHZCbndSQWcvTXVjeVBIakJ6R0k4WXZRZ0dLaDJYaE1EWXlpcEdKSVAwcUg2UmY0UnhLNTFBeXhISXJEWkVOS0VRMWFNUFUzVlpWSVJRTWkzbXV6OWJnK1NmL0hoUUthNjBJYUdvMkFDRjcyQnJMbC8zd0VseFpYQzI5OGJWUytTcVl5ZHFpckFnSmprSVFPSWFBOTMwc0h5bytmYjlQY01QWUJsdjRRcm8yWjI0TUxDREdoSUwwcnUyaTBzbzhZdTNEK016VlQ4SHcwRlpheVFXKzlva2xaYTNVZXBZaGdRUkE3Rm9aN1Bkb0g5WXBCbWx3ZFZRcmdnNHRxZG1tYU1JL0lrbHR4d2F0OWFJd2FXNG15N2dWVzRrSlBPcEttSlVyOGZ4dnZWQjJMT3hCWmcyOWhxUzJjQ2t5WStHOGt5UEcxK0dQem5nQmROL3RSa3czZ0tpVldRRnRBVzdSUlRhUFJ5MmRPbEFyTXM1NzcwN3BQVEkvYWVXcDhZU0RueWpvN3ZxSmk4OHhJR0R1dCtZSldOdFp5dm5LdzBCUU9rd3VIY09jTHpCMEpmdXVaT0ZLbEQ3MGFxbGN5R1lPTm82R3dEb3BaRHhESTBwRG9vTnFlaGRlZk94enNIcGtKYndxREd4MHV5QVVVZTg5YzVQSjkvWmRLYSsvNFUzSTF4NEZSWmxNK0tpcW9nbWZtZ1VvdzRtVmtzUHR1UTEvZStKZjhyakpZMW00a3JudEJpbXNKblRIUXIyeVlrMEdEOFhyVC9wYlBVb08wMkl3RTlyK2VVUkRyMlVIdGROa3RDVVM0djFGeGdaYUp5TUk0R013THJnU2dhT0V0cUttakJIQjhLL1VRbFZhbngxVVNOckl0VlBhb3E5aGFKenhNTjBKYkRmNzhkSnZYaHdhZUdnb3h3ci9EVlJoTGhrOWxNKzkrKzl3Zlc4OWRUZ1RNaGZUM05TN29Pa0xDNlRIRUpKelZRbVg3QklsU2g4UU5VUFBJem85UFgvc3FUV2VmaVlBdnZJckhvRHhSL1dlb2dZaEc5YUhkUFN4SlNNWURBdTRTTE9VNnFUeVB1WDFRVFc0QkI3UjIwWHF1eHh0S0JpNGFoNXJ1VUtldHVscFZDaXNNWkNtaW9KVWlOaWdnUDc0Qnk5RE9UWUNVUk9vbWNDYUJjSTVHSXVpV251bHpOQmxNYnVkajFyNVdQejJ4cWRJcFpYSlRRWVRiMWFFUVFXM1FHWENwOHA0Wnh3dk8vYkZSdnU3SUtZREVJeWZIYWM0MkhiSmQ0M0xVUXNNeEc1OXJLMjNBTlNVTmNhV0ttK0NybEV5YXRObFJtTythdUFTdFphK0JDSW5yekdodFRZQ0VjTjVCQVRlbGNpV2JzRDdibmtmdjNUTFpjaXpYTHl2VXFhQStLZ0JuUE9ZekNma0pXZStnSnpkUTBuOWF0cVlxd3ZkMDJOTmhuK0xxZ2w5UDd5a2NKRmxRZWdSdmNjQkdNV3JMbk9MSnZ3L0JlQm1XSWh3MlVOWDM4T3Y3WjZpQytvQUl5ZzllcU5kMkR6RHNIUW9TS204bDFLVkZRTy81NVBkUjhCcmdJcHFDZ3FFaFRHU1FhZDM4MWxIUElITGVrdmh2RVBUamhFeFRPWmdZZVZqV3ovREw4OWVqczdZV3ZGVkNSR0RrTjZlUUNHcGExbmsrb3c0TFdWeW1NdHJ6dmdyaG5DbGhRa2hFcVoxUXJPYzZhSVJTdzhuVDkzNEpKellPd0ZsZVVoRWJPQStmT09BUkU4cDhZVk5hQ1FBS3VYMVFXcnBHTTBqVFJWQ2pTUFNxT0l3TjlwK1BsR2JrTVN2QjVtbmpma1JwU3dEeHROT2lIbmlucENsSy9pU2I3OUMxSU5HRE9vSWVRQWlyQkY0S0o1NDdJV3l4aHdHWC9YanQ5VEJtOXFhUVByOFpPaEtpaGszOUNBRTRlQXdoY2k4cjdpbXN4RVhIUFpnRU1BNWQrMk0zQlVBQVFCekowNDgwaTIzRmtOVkFnYnFNVHJSaXpGZGhWUFB3T3VGVHFQZXg0SnFJb1RibE5EUURwZFFxYjM3eXBmb3VRNmVldnlUSXh6YVF4QVFDaU1DcjRxTHYvK1hCcU5MZ0xnSHlCUmVDcmZMR2doQi9WbXg4Tk03OEhzYm40bGp4amVLVjBjYnVEQkRFK3pMZXJxNGFFT0tJWVFLNlpnY0x6enlCZEM1YVNLMDFZZ3VCNUNja2NhSkNBUFFldTBiUnlSb1I5U2JCV2pXRDJrOWE0Y2tKaUJyUkZzRW9pU3VwN0VYVTFZenlQYW4xZFpFVEE4RG5LOW9SMWJJOTJaL29PKy9iZ3Vzc1hEcTZtOExCeTBaZU8rd1luUXBmdmVvSndnTzdvWTFIZFRodHVhR1VOdXVFaTJKZFAvMTdBaGdCZkJSMkpRQWxocklhU09QQndCODVhN1R0TzRNd01mRG4zamlpUjIvTHI4UTRSeURqRm9aNlJoa0l6a1dxaUc4ZXRBUnFxUnFTQm93UU9DK0ltY1dpSlFZRVloSXNTYUhYOWlEQjYwK0c4ZE5IY1BTVjh5dGlib3EzSkNxaWpVV0g3N3A0N3hxNFdwMnhwZUoweElTVlU5VXZ2VUNoVVVNaDJFNXJUQlJMdUh2Ym5vMkFkQ0tGVVhJcmhiVVVLZXExc3F3Z1NCZ2pDRkplZXFSRjhtUmRnUEs0WndLYkxBTWFyczdwdXFuSlpLZ2RpU3FMVWtPUXIxRzlTNnBuU3UySHFzZlorc0Y4WHpFOUJDVDdaeGUxaW9uQ21DUHZubmdBc0puR0JFVkoxaStpbi85ZzllajlFNXlheG1sYk15K05MQ2gxdzJlZnRwVE9GNk0wYnNxZEZwdGJBdlVUblI3ejliYnZ5YVdHSzBUZ2pDQVpoZ291Tlk4RU1ldlc5NjA0L25QQUxnWkZnUnVQbTdQZmJBcVAwYm0xTU1LVUpFMnM0QUZxb3AwSkR6aklTKys3alFmN1Q4Z0dHbWhTU1RxbVF3YWhVV0pweDcxcExDWEl1WGMzSmdFT2tBaGYzLzltd1JUU3dIbklVYnFUVmtYVTBjL0lPcEFpc25nKzlPODU4U1pPR3JKUmpvTi9mNWlReUJna2Jnd3RmWnNQUTREQTArUG51bmcvMno4WFdCMmoxam1NUWdjeHhBSklDSkUvS0YxVFZOUzBrbFYxMnhKK0pLa0g1dHZUQmdVSUNSOENpUzRuc213V214MjFXVi9OUVFaZGtBaUZaT3VqbVNXT3Nlc3QxU3VXYmdXbDk3d1lRTEdlTzhFeGpBMmFCSXhGazY5SExYMFNEbi9zSE1FTS92Rm1OUlZpd2xZVWFKRXNSYzR3SmlrR3FtWWNFaEFDRCs2dUVBRkhWWjFwOHhSOGxBUUVrK3ovODhBR1BYdjBWT1B3bWdtb1djWUFTVzduUnplRTk2SDh6UmNNSTJnQ0J0V1crVVBTU0FJRVVCSWlCRWpWVFhFdW13dHoxMS9meEpNRGJqalZnc0hCMXBqK2MwOWwvTzdDMWZTOXBiU3F5UGlvUnExYW12V3VhRmhqQUg2ZlhucytvY2pyRlh3VVFTSXZSMlJUQVF4YmN4SDZadUFZa3dJWHoxOTQ1TmxOVlpJV2N4aVVZZEFUYXF6Y1kvckZINXRaRUl5a01TbkJDNEp1R0ZZcjBadUJNeWszRm13RlFpdTg3RFFXQUJjUk9vRlJaTXl4S0pBYmFTWEFTc3ZtRnlLTjE3enp3Q1ErcnpXK2xCQ1dJMEE4TlNUbndET0xnQ3c2ZjFTeDZmajJPS2JDWmltcTBuTFJJVUFxSUkyUUFHUmtVek15Uk1QQkVDODhzNXFlTEVCOXZnUCtZc0J3eFhaZVRRRzlMVHdJUWxadWhZdW5MRWhYZ212ZFJ1eWhveXJXUWtpRnJWSnJBUWthZWo3MHpoNytSbFkwWnVDOTQ2WnNjM0UxWUZGNE4yMy9odDkxd1p2U0pKR0Y5TlFENDNnQnlEd2dLb1RjY0FacTA0RkFERzE1SXNyWGdzYWJZUlNlcVkxQndZR1RoMldkaWY1bEExUFZNNGZDRjBONG4yeGxnWmg0cWsxeHhtYjlzWjFDdFFKTllucTJseHFCczlVaTZKc29iZ3g3KzZVaWxmYkFJaXBWRTNKWnl1TTFERGpvQ2dyTWIxSitlNitLL0hGN1Y4VGF5eTlWcEVIQ0dpMXhvcFh4WDBQdnc4T0cxbFBYOHpCdERuQXNBamhBWTFFVWZ1R0lqRUtJcEt3QktqaEJBa1NYRzNPeGRGSEw0RzVRekxsSWdCdWhnR0oxNXl4NG5SZG5SK0R3cXZBU2pyYmdwbUJjNFQzREIzZEkvR3JQakdYYWIxcjJ5Rk5aRGowenhpaVA4UE5oejhpZ0xiMnhRS2o3bFdSU2NicC9yUjg2dmJQUTBhV2l2ZSt1YzhZYWtoR2U3UkI2dTVrWlRYRTRiMGpjZVRrcHJCY3hsQmlNS0lOY1RTalRENW8rc1MwemlLeHovTHZIL2s3a3JzT3E2cUk4eTh4dVNRbUZkZHVZTGlOeFdGVVlVbzZGU1pKVno5WFB4NUl3NlRjR0R4YWpSWW5hMUFtMFNxMWNvOThWUzN5S1BYRW96WndDRnFvQ0tnakJtLzR6bHZUMTNqRTdHNFRlOEo0ZXF3Y1hjcjdyNzRuWkhZV1ltd0VFbGlYQ1hxd05aYkVyYUgyd3RLc0NvQXliSHlXM25HVjNXRFh6TjhiQkhBL05MMko3d0JBQUlDL2UzNDZsM1M2VWxBaENucXF6WUt5REo1dVVMZWhXeE1sTWdBTUliWWsvUVVoclJ6Sk1JRDNwWm1VcFRodCtlbkJ3aEdEbEdncTlaMEFYOXI1RmV3b2RvaTFvL0R3TWNZTGtORnhvTlRocHhySE1JQjNYSlV0NGNxUktYcFZKbW9IaTRHV3VtTTFvblR4UlFGaHhWQlZjZVRrQnR4djhoNkM0aEF0MG9sRXJVMldoR3VRU0kyZG5pUlZRbVg4NUJxRmR3QWs2bGlmUWZ5U0puWVh5M094aUJCbkNuMngvdjZncmxuL0x3eU5VQkgxVG1USlNueDV6MWR4Ni9RMjZkZ09mSEo5RWhDaTBIak04WThRenMrSm1BeElucGRLODlXTDVpMjJpR2dybTVqSmhTcXNQWndTU3pMd3RMRXpXamR3RndDTUNOU0o3c1BRTVdBUmMzSTlZSE9qUHBvSzNxTnhOSUlLYW5peHNHdWp5bzJTUWtrakZqcVl4Z25qeCtPWXFVM3c2bW1NTEI1TVhMRDNiLzJ3Y21RaUdybEd0QlloS2Q3UnlLNG9EV3R1SkhZdkZZYUMxZ1lLaTI3YzFEMER3MnRidG4xOFNBRlU4REF3ZU9pYUJ3SERCVURzWXNjd2ZpanJyWVA2djQwZEdBYWNVdTVRVThkM1VNbTE5a2lrYy93a05RM0k2OGVDYVpsR3dQb0RrcmFJYUV4MUIxRkNHTlBsbkN6bzU3ZCtFUURFZTYxbmdkRVpBV0RPUHZJK1hEbTZYdDF3TGlFcnF2d0lxRVJ2cE5xUndKdzFvamR0TWM4UTFxdkUwQnJJcUwwQUFQRVZMdXFpa01ZZzJMeEZWNi9HR0tmeTQxRUFJRTBVL3lLWk1TbVRKWlZWTllZNHc1NU5NVkVnMVJiV01nSXdRTC9nK2F2dm0wWVp2WXJ3L1I0R21UWFlQVGlFYis2L1NtUmtIUFF4RHFZTTBZaVFweEUrUDBTMFFuMUZiWFNMbEJxNksyU21xUTVrSXpwRWtWanhXSzBVbjJqNUpOSG5yQjBrUE9yd2g2SHJ1bEs1UVZ3dEpyWThwa1dGWVlZeStTVFE0amN6YlJlaXpoMnNKUmFhejJOYmpTRzlWcEl0SFVOOEtYbTA3WEJJaldKS0NPa2cxQ1F2RnM4a3ZEY3lPbUkrZk5Nbm1OWUFTSW9Zc01iUWVZZlZvOHZsekdXbkd2VG5Rc01pVDlZRHI5VWJGOXNWTmRnWmNndHRsT2dWZy9nb1BQUXdlK1Q0MFVldlJPZ1lWKytvTU11YlF6UnF1R3IwZURPVkg4T0ZrRnNqUHJwdVZnQ042cGVKY0daWXpPZ0sxdzVJcXRDSkNDUGpJUUpsbjJldE9EWE1YZTFZaFpsVEgrcDNyOTkzRFhlVzI4VG1vMUR4aWJVUC9xK1BXRkxFVktWb3JjZmFENUdjdTRZSHNYZHdTQUFERjFJQUV0QklocE1wWTU4Z2JVOUN1bEpJVlJEUGN3TzRlblFEMTQxdEVyZ2hCQzBwbUFBUTE1ZjFRdFJpTmNYZklrY0lCR1krN1NJa1JDYnBrdXlyVkpMWCtPZkpVWWwzMGlRTTFJSW5KUnd5SmtTaVZoTXhZSzZzZ080a3ZyM25oN3h0ZWdjN3RnT3ROMmtjUm1pZnlndU9mZ0JRREVKdGRPMEpCZTFXRzY3MXhvL29iNlJ2NHFVSUZ6ZU04eFdXWmhzR2s3T25nUkZ2aXdDWUJuRGkxRnFPZFN3OVBDZ0MxZEIrMkFqVW95WkVReXBUY2dNQ0dFSlpvelpqalRsL0FzQzVDaU95UXRaTWJBajRTeDY3U0VpOGoxUHczYjFYVTB3M3VQUXFOUnlRb28veERzUDBDdE4vVklFczYyRlhzWTIzVDk4U1prSHZnREJaOU51ZHdBY2tOemI4N2tQVXdHeWZ2VWwyOTI5VnNTTWgwYUZXblcyaEdlNHFHdjloUnVLWUpTRWsvdEtFMStKN0dkOGNFaEhTcTFDSHVOSlhKWnUzTFVKckNabTB2VWp0S0lTUFpmSmFTVkN5SG1iY1FmUERmVDlpQUJ5VHhZWklSQW9BMmJUaUtFaGwweFNtd1laMVNVSllvN0pBelFXR3NkV1dsUWg4TUtEZ0JlZ0p6TW05TlhlYzh5Z0JnLzBuSzNybnNwY0ZZZzhJRVk3UUhvUmVJNkFpNnhKN0RkZlFDRSthaE1ab2VNVURYNnFDUitTcmNjcXk0K0FSRTliak9MV3hpSERaN1Y4Q3N3QkxDSnBzNGpUeFRKVnRZVDVxanhNVTBJQVcyTm5mWGk4aDBqQmFLcGV4b2p5dFVicnF2MFdnVkJFajlBQ2YrZlhuWTJBOGpPU29Fd1BTSjdlelZlSmpqV0lQUzFZTGlYYTlRTjE4SnIzV29GN2N4RHduMjZzVmlvdEFEUGVraTc0bndFTzF1YTJrTHB2Y21pQWN1eGsvZHVPblRaeUwrdVVLQTVPRlNNbDkxOTRMYTdLMThOVXdPSFAxZ2tremhucVk3VXVTVkErUzJDRUF0UUtsbDRFVDJRVnR2QUYza0lCRlQ0NWlSaGkyWmllREtMMGhDZnB3Mms4VGxVbTJRWm9FRGFPSzZwbFVnb2FvU3VsbEZyMnNKNGluOXNRSk1rSVZhNjA0VlJ6VU9ZUGVLRFRtdmlKSy9KakVLVkdYQkNENmtBTUlCcTRSQ2tHZXk4ZTJmeTdPeENMWElvWXVVbnV5WkRqZGNlcENIYmxUend3Wi91ejdMOGZsdzIvQlRxNFRYNVhOUFVZSm4xUnZYWlNrQ0xVZVlQQUpsTTJpMUhPRWhqT3JnZEoyWTVLblhBK3hGZnRMWDlZQ1hsc2FwbHVwdHg2RGdwRmdSTko1c052QkxUTzN3cE5peFlCMXVSVWdDcUVxcGtiR2NOakVTcUFvWTdvaFF4eXJ2Zy9HYklpb3BGSlZQdU5NYXd4QUtRbkhWS3NJUDhxTkFZQW4xb01OQUx4bVN4QnVLK3l5b0VwajVNNkRhbU0wUXdGU2hiR3VvNzV0alVscTlmY25OU3pDa0hzcTBBcEhMejJwaGRnNG45R0pzekM0ZWVaMi9HRCtPa3JlQ3dDTVVmVjZqdFBlWjRPb2RNTUtRTFVDT21QOC92NGZZdUNHbE5Cbk9iNWJXekl2YlA2MkpaOFdtaEI2cmRDeE9kNSs4L3ZOMzE3L1dtVExqNGF5YkFDem1EQm1vbDhRbFZyYU5PRVJhVktiZ2xrVXZxek5PaUtxWHFibkVUTmIwZGlBU2R5M1lGcmJlRWtNQjIzWXFQREd4V1ZTbFo0TzZFN2g4bjNmdyszVDIybkVNR1dyeDhQbjBqbng4cUFqemdFR2N6UW1ub3RTMDBocEFlNGs5cHZsVFg5NkFGNkZSTWFTa0JYWk9rd2VNUVc1cExiQlE1cmNKZENqeGxhdFFtYU9rNklXRnFrMlQ1dXl3N1NqMDdxeS9zN1dEb2h6Rlo0ekVLSXNjVllBSURRZGdoSGhwTEZieGNKZ0wvdmxmb3JrYk45bm1MN3dYYTBsWUx6UFlHMHBvYW8wblNXNGV2WjZmSHZYRDVDWkRFMjR6TVJWa1JTV1N6cWsvcFVnbksrUW0xeSt1ZmRLUE8rS0Y5RXMzeVRxSE9xczVLVDJnaFlNU29nQ0pKc0RwcEUraTZ6T2VsS2s0UzZKUlhSa21Ob0VvQmpScU5lNGxUdVJKRkdjWkFWYVZCUnJKMFZiZGxuOWhRS3hWb1k2NUZ4eENLMFhRSVF4NEJnQWZkam9XcURzbTlSbExyNnlaZWdtZXpUQ3BONGc5Y1lJcnc5bW9LR25ZclJ6WkpiTkh3MEF1RGdCOE9MdzBmdld1V1V3Wm1YSTRDUEJlTVNpUVZLcFdOeERCS2h6d2xJaVgzc3k2OTh0VUJWWW1TK3AzMG1BclIwcUFMQlF6Z3ZNYUxUV3BHMWV4RUJMOGhJbHFUSFdhSWludmhnVlFjZmdBemUrSHdEU2JxN0J3Tmo4ckJra2tIeE5KZEd4dWV6czc4R1RQN3RaM2RnSUJCMXFFK0lLYndrU0xUVnVXWXkxeGl0cnZaYU4xR3o3UHJ5TDM0bkdJd1lhZ0M0U21DM1FodTl1L2tvY1llaGpnUmd5YkZTNkVvWkdrSFhrQndldUMvZmRMSU40cUVoa1VGY3NQUnlRY1ZHSEdBOXVEYVNkc0JyRzFFamxkTDhKOXc0VXBZcW41NWdGMTJjVEFJQnJFd0RqTDNxQzZlbW9zVklKeENNTDJ6Nlc0ZnNtM2x2dlBvK1VDTmV3L1VrU0pqYzk2VGR4a28yTWhQMWh4TFQ2U3BvMDltc1d0Z0laUkh3ZC9hMTNmd0FKRjZjeEw3STl3ejllSFRDK0RCL2U4Um5aMDk4dnVla2dkVVFOeVppaDlJWnM4WUFDS0VPMjk0SEJBVDdza3hmd3R0RjVtTzRrUElkaE95ZUpYeTgwcEk3ZmtvaGRqeEE5MlhxeHd6dk5JdTNRa2w0Tm9KSVpscndDVFFYcXRicER5OGFMOHh4WkpzYjNwZTlHQ3hEQklHMCtQNk9JSVlFaHZyZmp1OEdrVnA4MENTMlEyby9JR2F0T3dMaVo4QjR1T3ZRdFd6WU9JSEtVaVlkdG5rOUNFaVNjaG4zZ0tlZ1JQQ1ZmM2JxUlZraGczWktsc0Fad0RGWHZHb3o4R05iZ29oM2RVRVB0V3NXV1NvNmlxVjZERGllN3k1cnBTOVZVTmRLQWF3L2RRTkFqbXJ3dDNpc3VlVzE3TVJuRjRjUHJSanlCdHJINUtQYTcvWHpIajk4TkFLSERmdlNOa09KVm9hK1ZSR1NHMTNuSEN6LytCRjdwYm1PMlpJMDRYMFJqWDF2cXBTM2lXMWVhaytSY0paMWVXN2xZRERDMjNsYzdLcWJoQVJjNUlpM2VMOHdCYTF1eCthNDBuUklUQjlzcU8rbllHTVVnMGJFY0R1ZkNrNUtBRUVvZUVvclhkS2N3cGdiUXBFWFNCRFFDT3VnUG92YkNXbENvTDA4QU5LSWtlb0NPbVdNQjFLRmZVMU13dVJ5RjNLRG05MnZET1VtaXRFdmpicTg1Q056Wmx0RW1hcWFxTURMR3lYeEoyRVl4WTFJQmFpQ1JDUURUNVQ3QUpybFppL2UwWUFKUWs5a0ZRT0ppdDN5SnVDYmVpVXl1d1J1dmV6dW5xMm5KVFNhSlFtZWlQQUNKSGRyaU5CTFdXb3d1V1NiUVRPaFNpUmVhSGMxMG55MTlXSDh2RWxBYUlBbmE5bUI4VFdzdWF6VVdzNENDRkpIbWhSRU05V2NaTEhxdVhvOW1kOVcyV1h2TWRmWk1heTRsbHlxOElzYmFRcFRJR01QVVQ5T1lIQ2JMWXY2S1ljUkRHRWNydEJoc3BrV3lKQW1HY0FOcWdwbmtBZVlHY0JwUDV0a2NBWGpOM2lBSkhEY2h0NENYR09hS0ZTNkpWVTg3TU4xOHNQa002aUtMdEJDTmJXQUVVQ1Z5WkJ6TlJtcXhsL1NwTVkxTldiRkN5aitUTlBscGx3ZnFRWnBGajRxMGxqUnA4WlRCR1JtUjNkVWU4NWFyM2hFelBSUWhCS0lFUStFckU2c2JjeWd0RFA3aDNxL0JpcW9IbElVSXNnWTZ2aDRQbXFTL3RnMEUxSWxHcW91VHJlc29TTnkwaWdnNlFVcUZpZmZUVU5jMWNDTkc2dEJhM05aSndsRVFUa2lQYzVRY2tucGNiWXNsVENwSmdjbXd2NW9sQUlUV3ZiVUVsQ1J4amMwdzFwc2l2SWZVblo3aU9FSVdYcHQ2YVNhb1BYWkJiYWFBc1ZWT2g4RUdqTGlyRVZCWlRua0ovWmtqMnl3MXE1QW1sUXd0V2xWYUtFK3pMWGY0ZGlRRFh3eU5NYkZQaWRRR1JTMzg0OEw1V294eThYK0FrRjhhU3dHWk5rRGJTMFE4RDBoZ0tjb2haR29GWC9Pak4zTG4vRzdrMXRDakFtQlFWemEySkxZUmcwbzlqcDQ4SEs4Lyt4L2dkOXhPd3d5aGs1S2tNRSt6OGVwY09UWVRMQzJ3SlJzNWZVZDRUMU5ubSs0aFpmUnEvVG91V2x4TkFJMi9MNHA2SURVNmJLbm9GbGVYYkVKQitDTndkNlI2d0ZyYzF0OGxBMTh4ZzAySjNSRU5CakNLUE04NU9ib004Q1ZDc0VwaTVuRmM3SmdRMXBRODF1TUp5RS91ajFjeWNLS0NDcENKN0Nla1l3bDZkWkNzU1MyTFpISWJXMUhTQjRhOUlTRnJ1WjhRajNySENJbDAwRXI3MVJHcDhadU1odUxtMWlMRzJHK2RlcFJDUW0xUHNQNmUyaFlpQ1pwOEZOT2N3OHUrOVJjQURGaG5meWhpZmVlaUt6ZFdTbC9oeWNjK0RrL2Y5RXo0L2JjaXN5TnBVYVBxd1YxL0wycEFMcDZMTmpQUWZ2TWRWVE9Rd0JrK0xEVXNiTTk1Y2dMU0F0UlorMGxicDdsSndKYzBIdGJiUHVDVFFJWkRmaFo5UDBEU1liRnNOZVFoSzJFQldXR1hBT29oU2ROSWM2TnhKZE1ZcEhtdWhZSG9LRXJkV3BGZ054Q0xPT215SUlIcktiQmkwdW95THJZME9yaGxZTWFKWFNSbjQ5K0xnSmRHRWJqb05OUTBwNkpOcStMNmcxSkJEZXVwUkcySXB1ZGFtSzEzSXhqYkQ4Uy9LZkN1Rkx0MEE5NTM4M3ZsQzdkOVdUcTJTNmRlakRFSUJ6YzNabFljUEt3WU9IWHloZ2Y4SFk3TmpvT2IzeStaZEpneVVoYVpJR2tkZ0RZZkdqL05OSk1RaFZpNi84WEFpL1pVN1VneFRVNnpQZG9nbDlaY041TVhhWko2OVdYUis1b2FrVFRSQXBOaDRJWndkV1lVVzNWd05HbXNTenE5eUhDWU8wUkNXZ29zcGVZblp5cU1qV0I5dnhKVVlYaXZXQW1IK20xR0JPQ3VlWWx6MGVUNEF3Z24vVkJBTlBSSGtraUxQSzlrNUVvRHZIcVRoSnQzVkpicUZ0bUFFSWhKV1JRQU92bVl3RVBxZ1NTNGhnVnMzMUJMTXBnRVZWTy9sbW1nQXFqQ1RVemhlVjkrb1F6S3ZnaUVkOHdBaVpjQVFtc3NDSEpKM3NPSEh2aHVMSjB1Nkl1K1dHUnQ4QzIyZjJvYnJ6VXV2Zk1YTEJwM1BWLzFkb3h6RlY5WlM3RDJlOUxDdGw3THRnMnM3YllhNmJNYmNaUUtBZU5jZWhmZkU2WkxRcVphbVBqVW50VWlSOUIwSW8zWkxHa3NVbE12R29pdE85MXI4M3Y0YUNXa1R0VU1sOEhhY1FLQUQ2ZEZoZFZJOWU2SlJHZ0tYeVBJbzdKUDJRSXRPck9XaE5IZU1SUXBxOHJNdTBGNHNyRUJnMkNMaWFsTHV5c0ZUa0xiQ2g4bnJMYU5vdTNUcmxNSVE1QTdBYUExQnM5S3N2RXBYSyszOGpsZit5TmFZK0M5UjNJajBxdmpKUUFrTjdrNGRlYVVsY2ZqWXhkOG1MM3RlOGl5SDFLVEtnSmVBRWVFS3Z5NDRENXV6UFN2YXJEdmtrbWhqRDMwMHU5c2JEcVA4SGtOcnhqNU5UUWJ2cUcvWXRQREtORUNCeXMxTUZzOUMyb0hRRUc0Qk80SVFoRVV3Mms0VjhZRlo2MExTVEMxSXZaMU95NFhwWFQ4VXlSb0pOWnowTVMzMHowa0taMTJiRXBvOS80dUUxTFR6a2lXUS92bVdRZkxhL0FKYWhzdzVhL2RFZmx4Y3hnaklJZWNLMmJEcmFaUUhKSm1DdGNrcHdnbmNjVmFZR3RTMmx2eE9VUUdnQzBwMExJRmtyMW1CSzRxa2EwNEN1KzUvdDFteTNVZllXNXpWRm94SmFIZXdSUUVBR1FtUStWTDNQZUkrK0tERC8wM2NOZE9wU29NTXdSU0hySTQ3RWJFdVdoSm9CaC9hNlRoWWdrT29qNzhNSTY2UlVySHo5SDAyZEdMVG9ZVUc1V1k3TUU3WHMxVU5WdSsxbDVFcnpzR1k1dG1CUVlJNHduR29BQkE2WXJ3aGdpazVvT1R0cEhhd29yald2ejlyT2tDa1pTenVMZ1RRZXNQWDl0ZnpjM1VrcyswYktESVc3V1RUU1NGYWxvSWpIOExEZUVMekE4T3RkQ1ZGS2d5Y2JPbnJUd1djS0d0MldJUkhtM0FWc3AxTElScFFUMU5BQnJ2T0gyTE1mRE9pVm0rVHAvOStlZkx0UWR1UnBSeXJkbTY0MFhrTnBmS1YrWVJ4ejZjN3ovdlhURGJia2ZvcTJ6YkV5MkxBU2FMcFZFZG8wMnZhZGx6Y2Y0c0xFeGpCMkxSdm1pcmNtbDlDTlBmcmM4bUdtKzRyU1hTOTFKcTlnZXFISlZSNUxiYm11TkFpMG42ZkFDelpkOEFObW5xTUtGSk1OWEI5Q2dBTkJISGFTekpQSk9VYWh2ZVo4eWllVzh5VXluemdaS0tIeGpTWG1JYzlFNzFHMG1KQlhhOXRuOEVDS2Vsb0ZZalBvalYwZzNqZ05qTXN6Rkl1U05yUmxZQzFRQnNmMTVTWVFHRDBuaS84ZVpxT2loT0NGc0RUQktGQ0NaMk5tWm14NGhuZnZ5M1FWVUprbm5SWExSRWoxQWhhbTNPU2l2eitKTTJ5NWJ6M3dkeit6YUlkMkpvUXdXV21tYngwOGFvMVJIUmtOTklhcmpleUVLQlFRN2ZQd2pWQWpicmhyaWtqM1JOVXNIMTBOQitMQjRlbU80MXF1ZVczZDFzNERpV3Vud0FBbGZKMHM1U2pHV2pZV0tiMVNXb01NYXlWTVdoaFVQeENGRnQxaGZ4dm1vTWNQRkdTV05NRWpleGxtbnZsRmcwNlRVQXJkUGJUTitIVGFJQTFBakVJTGFuYjk2VWZrMXVmNzNLMGhwVU0ybmh3VndHMVpEeGJwTVlDRkl3MnFRam5TVUVPb0R6N1lLYThPTml2bDhLVU5RL2JFMUMrajBaeXNuQURnaFdIU0tiV292dlZEL2dNei96YkJwWWVIWHQxTEo2UnpPZzNRZ3B1Y2xaK2dxUE9mRlIyUExnZjFQWnNZTTZHTUN5QzFRKzhJU09yVEhISHdmQVJ6QjRvTzRjNVJYd0FqcUY3dHVPMzVManNINTZoSDdiemRTNVdWakpZVnhHT0FRd3B2ZTZDTXowZUMzbEdOSzNnbVVsYUpkSnhnbUlOeWtSZ0lRcnVXRmtCVVpNRHE4eGpaMFVWWlY0SjFLNVF2WXQzSzdJTFduaVJrbzhZTkl5b1NRalpWMm5NczVtTTJnamJXSnBlQXpZQTlnU3BJakJ0WUdQc1k3YnBZejdJV2d3a1VRd3BxMVV4ekxqaHpmaU9NQyszdm5wM3FOcXlUdjh3WUViMjhDdE40dUorMi9EMUhvY05YNDBXQTVpeWtETDRVZ1NMYWl1aGhKcXZ5YWUrRjA3UUZKalBIeWhHTHB5Z0d6NU1lWTlONzBmci96bXF5V3pPVHdka3o1cEtXNjBHWTNjWmxMNWlvODUvakh5OFVkL0ZNdm5DRCszWHpMVEE1d0NrTGpJTFdBa1RET09QWjVzTFRTd1VOb0QrL2kyKzc0Rmx6L2hLL3p1a3k3RDIrL3pKcHhoajFTL2V5dTFPQWhyTXBnNnVUZHV3Q0NjSXlNUWdWNTdFUEdZaXVhT2sxM1BXQm1Mb0JNc1VGVlkyWnNDQUhoVmlZblRiZDBFcUZkWHFrQXM0Z21Sd2ROdGg5c0FnWkVVRm8yUHRxVjFuRVNKZVZ3Q29QVFIrRWlodUhpNWVlNWhxbUpLSVVWRkU0WmlYV2ViRnFjeGdoY0JKUTB3U0VoVkpiSmNmckQvKzR2MENVTklERVlNblRvczY0N0xNZU1yaWNFQ1JHd3plWWkzRUhaV3F5NmhYb0hXcDdiK3FQK09YNnNpRUN1K0twR3RQeDZYZk90VmVQZlZIMFp1Y2poWHRlOWdVYVpNbEliSWJTN09PM25va2VmemE0Ly9FczdBRVhTN2IwV1dqVUNhS2l0cDJWelNlTFpTNS9lWnJFdS8rMWE4OXA1L0piOXovQk5SK1JLclI1ZmoyYWM5QTk5NXlsZmwzODc3RjV3bHg0bmZkaHQwZmxadGxzTVkyK3lMdWxvT3pVOWROZCsyMzlOckE5c1ZOMEY0aVMvUnpUb0F3bHFidU5TcG96a0FsbFVGbjJWQkU5N2hIbXBod1BROWFXd3Q3ak50dm1RWkprM1FaNUNBZHd6Rm1kMExNMFkxY2pEUm9pSWpONGRHdmJXbERxVzU2Y1hQMWNXeXdiUHFjTDQvVDZjKzlOeERtb3p3Zk1MS0NjdlBFQm5Xa1pkVWVobHR3WGkyV1cxVHBRbHAvZFRBWSszVjEyT0xsU0NVY0ZTRVdYTTRmdS9UeitJVk82K1FQT3VnOHRXaUd5UEFsSEtZL0xMTVpuUytNaWNzUFJLWFBmbExlUHJHSjRpNzlUcFFsZGJrRXBva0lxakRPaXdWUDlZcE10dUIzM01Ubm5Qczc4a0xULzg5Vkw2RU5ibDRxbFRlQWVybGlTYzhCdDk4eWhlNTVXSHZ3VDN0c2ZEYmJxZjJwMkZzRHVOTjY3UEJ4V3NCTmxxcFpiNkEwWXhLTjZLQU43Snk2UkZoeDRUMUVNUjB0ZVFWM2pDN1RXYTBMeUpaa3JSdDdiWTQzdDBPVW9TOXdIcVRDRUtCYjR5NFNJRlpBTUJKcTdnWWdOdDhhU3JDR05CUVJHeUxWVjUwUXlrMmlVQUhKRWVobG41eEpNbGVWVUxzS0xZdDdPRVBEMXhQSzBLbEJpS0psRnJsQTNqUXByUEJjaGlPQ20zemU0dlViaHhVS094aC9iUEk0RS90eFJoYnptcWtiT0x4T0ZvQ051Tnd6VXA1MkFjZWlXdjJYU3U1elZGNVJ5SldFaEJTRTZQTlpFdG1jenIxTXBwMThLOFAveWQ5NnpsdndySmRmZmdETzJHa1ErTk1VTXNlQ3BYQS9UbWxsWTY2Zzd0NWo5Rzd5NXN2ZUYwb3poZExJNEgxeUcyRzBLbktRVlI1NGZHUDR0ZWUvZ1g1OEFYdndWazhXdlcyYmFyOUdacHNoRVk2MmlLQkkvamlSa3ZaMENrTE96ZzFOVVVVdWsxMDlINnJ6d0FBbW5UcW1FaWtMd1BhZnJ6M09sVGx0RnBqdEpWOTN6YkJHaHV3ZVR5MVJ3djJ1cE9VcmhKMUdJSGNiZzFUdVNYZ0xwMW9ZMG8va0lFcllrOEwxbG5PcmkxSjJpQkxVUXh0N2NSb3J6UXRLSVFnVEpaem9kZ3YyL2JlQkNBUzdVUm9EUy8xZVNCNjdLcmp1RFJmcGI0cUlDcnQ4QnFheEVjZ1pTVXZCbVR0L2NYeEdxa3p0alVSQVNhcW1IREFpOG5Ic0c5eUJCZTg2eUc4OGNBTjZOaGN2S3NJS0ZydGI4TnhieTJObjVsd1RwWHpGWjV6MW0velc4LzRLaDY5N0lIVVhUZUwrZ1d4V1RmVWtrWHBMYkJBV2NyVUF2QXZEM3M3NDRrSllvME5lelY4V1dvYUtUQkE1WjFZVlhuc2lZL0ExNS81QlhuUEJXOHpaOWxqb2R0dkUrMVB3OW84bnFBVEZ6eEpxQ1FJV2tHUThLOGt1MTFZRHJGeGJGMTBwK3ZrVEJxanFiMGdoN09IR09QQWJXTzhYdnEwSWVQVHNsZ0tvcUhwYk1nM2doR1JvVUptOVVjdC9NSGdrdkMyRFR2bWRsR3hOVFEycERJZEVwQWExTlRVd0IwRzBGYTk2VzdicWhnZ1ZBVzlDZm5HOWl2UysrSUxUV2hLWkEwclg4bW15U053NHVnbXdYQk9qTmo2bmh0Tm1DUlNCQmFqY1ozT2Y2NWZxVzBWa0Q0alRFazYrZHNZOGQ3QmprNWl4NVNWOC83MWZOeHk0R2JtV1M2bGN3RHF2TVY0UThrRUNtdGtJVFRXU3VsS09YWnFJLzc5d2c5Z3k0UGVnK01XVm5pL1l4dTBHSXF4VnZLOGk4eDJ4ZS9jSm05KzBEL2lwT1hId0ZNaGRROCtSSU1xamgzaFFKbFFzUVpXNm1CQlBPWFVpM2paMHo0ckgzN0lPL0ZiN2dqeFc3ZUNyQ2dTTzNjbE5adFduM0dUSXByL3llS3F2Q3pMMTVqeGlaVkk4NUhDazFxWFlBSzN6ZXdXZE1jTjZjUFdZQXRkU2VMV0RGK1QzVllMSVFIcTZqaEFrQUhzSytUYS9oNjBybGlIQXZreE1NOTV2ME5zdEFGdCtOeElmd1RDcTY3TGlHS1hRRjJKbFFSR3ZSdVk1a0xnbGVoMitMVWRsd3ZxSkVqV0U2WnFCSUd2TkE4LzVnTEIvRHpGeE01Y1RUYXdnYStMYnhCZC9UUVJFbmU0MUg4bitDc0Q2Y1pvWXRmR2VWZ2I3d3JZaVVsc253QWUrSjRMOE1NOTE2Q1hkVkM1Q2lCVHV5Q2tUNDJKeGRRb3NUcFpKeHhENjUxY2VOTGpjTVZ2ZjhPODdxeS94RW5GS3VxMjNheTIzNFRxMWgvdzJXYzhsMDg2NGJFb2ZBbWJ1dFBFKzI5NWRnbnlZb3lJaUVodU1vVGUxMTV5STNqc1NZL0cxNS96WlhucmVYK0hxWjE5Y0g0Mmt2MXhSUnJ5dTlGY2tUcXh4b0lMMDdqWHFoT3hibUlsdkRveEZERXdFa29XRURZK2dNL2QraVdpMTZOV0tqV2R0RGpHM1FvTEpoVXRqWnIyVVVWYmtJUVRFVEd6VkxObk1IZEhBQUpiTm9mem13NjVXUkdCdUxvMElIeVE4NDN1VDErZXZDRnEySDBwcnBsc2dNWVlGdlZlcExjVTF4eThqamZ1dXduV1dQRU0vYk9NU0d3WUdaSVFIbkhjUXpCV2pWTHAwbW9IbXpDMUg2dDNYdHlSbWpZRW0wbXFIUmMwaEhqZ3h3SWxtdnFkU0FnOStYSUlPNzRVdDR5WHVOOTdINFNQMy93VnliTWNwYXZxWmcveElnQ0cycEpRbHF3QU1tT2xZek02OVp6bzlQRENzNStMN3o3N3EvclJSNzZQZjN6VWMzbkptWC9PZjNyZ2E2bGVtZHNNTWI5VGFtYytxby93RTZFWmlxMUFRSXdZell5bE5abFUza0VnK3B6ZittMSs4ZW4vZ1pYOWpDZ3Fpa2E2cExZRkpmeVMxa21EVVN2TzZmRnJUNlFnblBjVGVqNFJESWVHMHhyRC9YUDdzV04rcnlEdmdxRWZYOXZtaXp4Z3NyczFySGNLMndhT3NHa3ZZbE1JUnd3SzdxaUdabHZBWEJOWHEvVng1dkVqR1NyRVVnVkdrQ0hVaU1URzB3akI3NVpOaU9RSUlDUW14TldxbllqR1FETTJ4N3hid0JkdSszSTk5dEI2TUd6UnpGaXFLazlhY3dKT1hINkM2T3dNcmRqNGdYR2JFV2h5RlFVTkxWUHYrUGliTk5STnlJeHE5dTFpVlJLM29jRDdFcmEzVkdhV2pzcGp0andhSC9qeHg5RE5PK0pSdFVSSlpCVEM2WWdhMUdXUTVoR0lVQ2dxcmRDelhYblU4UStXVnovNjFmanpCMTRDOVY0b29JSFJ5R0xVSDlzTXJxNTNiSjZwQXhoQk1lVTJ5SWJDRGVTTWRTZko1bU1mQTg1TWk1WFU3TE9lcDVUOWtzWk43MEgySzNuVThRK0x1c3ZFQXZBd0J4bzk0TXUzZmdmN0JydGg3VWpUcVRYdEVjWkJNUUt4VGwxcUc0QUltOEdJa1J3aUZDTmRJenhZN2NEOC9ENWNmTEZKYjRoZWNFQ2c3aHRlZ1lFSGJSYTJUQjVGcWhmQzFVdVFVb0dpenkzMUUvRnhKbGdoSlN4QUFGWEIyTGg4NG9iUEl2RnNGbW5iaDg5S3Rzam00eDhOekU1RGFCdGpkbkVLVXZQN0lrdXhaWkRXbmxrYW9yUk1CZ2xoTktERnBSbDY1Mml5RVhEdFdqN3h3MC9GbTcveFp1UW1COGlZMXAvdVNxQm81eFdGKzFIQUdCamtKcWVxRjZjT2xTdFF1aUl1a3pISjNtQ2R3RlFiYUdtMGJBQW5hZmpTUWlVZ01MbnBvdkNPMXh5NEJlaU1oQ3pTUkE3WDl5VzFpV1Jnd0dJQlI0NXZ4RW1yVGhRUGhaSGtnNGRHbFNtQWUvMisyNGpNQnU4MUNlaGtZa0drUHVWYjZ2Vkk0QXcvb1dKU0lFS0VQbnhoOHVaMU53RGdwR3ZyR3c1ekdEMWhlMHYvQmd5cVByUFFIUmU1Q1dNcjJPYmZXb3NXVVZDcjJ3UU9qUklwU2syQnFLOGc0MVA0OXZidlk5djA3WkxaY0lwM0VrZ2t4Y1RPcEU4NFpUTW1aSksrN01kb2pEUUdkc0pCc3VVU1NacEtHZXRRWEFQTEpxc1pxRlBHUStmQnhUK1dVRGpBV2pHSGJaRG5mZVZQK0RzZmZhNTRoSHpkeXJ2NDFZUlFZckZUYmQ3SHlReEhTUmhqa1prTWVkWkZiam9TcGhSU2x3UW1XWGNYbDlUR2RKTS9VTU1USVl2U0dNT1ovaUg1OXM0ckJDTWpvZncwelVmNmljSUtuaFF4d055MFBPQ0krMkhaeUNTZGN6QWhsaEIwcEZJTWczbitpUjk5VXREdEJmV2I5dmdpU3dFMXNkTDRBMGtBRVhYY094ZlFnalJHTVNCTklaOEhBR3paVXQ5cm1MTkx3cHllZWVQOGpUSmRYUzg5R2xFR2Q4RktFOE5zQmhDK0tTVU9nTTBlRFNmVHBNbW8rVHpDMFhaeUhOUkQrTUNWSHdFQThmUTBsRHBmeDhDZzBvb2JwdGJnc1JzZklqeTBCOVprclI0a3pWZlhsMDlHY0J4Y0cvanR1R2hiRW1wdEcwUnZDSUNveEF3QlVCM1VFdG5Hby9IUE43OFg5L3ZuYzNEVjNoOGp0eGtxNzZMNFdsUStMODNJNHFNdGdRa1RxTW1BcWlZMXIzWEY3VjE3VUpxa2F0eWNTQ1gxQ29Wem9WZmRmMXozZVJSdUZqYnZnaW1MdXUxOEJJRkJoRTYySXBYSGswOS9QSURRckR4YWJ3UUNsWnFaRFB2bTl1R0cvVGNDb3lPaTNqZG1WWnAzMXNPTmRsUnRXemZQS3dRT1JEY0tTVU1qZlJWK2Qzb3I3bkExV21RTHpHV0FrMFBWcldHeGhMQUVqTEJKdUl6T1Jscmc1T3EzamYvYW1ZdURrZVF3aUdqbElaTkw4YTZyUG9oaFZjR0tsWnA4RDJWSHRiWDJvdnMrRi9sQ3pCNnNqZHFXeDVXVXJCQTFYMWhuVGQvQkxFZzJTcExRaXdMU2xMdDhQUVd1TEpHdDJZVExxNXR4N2orZmp3OWQ5V0hrTnFNUlE2OWVBbUtqY2FyQk5veWZVM2QrU0ZpUUpzMHRFUmJ0ZFkyQ3ppVDB4TGhOZW5NS3BnSkNrVXlDMm56L3RSOGpKM29DNXhkL1lFSi9sUGpHZHNDRmFaNDhlVEx2ZWZnOW9QQ3dZZ01xZ3owYnVrZ1k4S3MzZmgyN0ZyYkRkc2RJK2pRbmR3WjMwangxdlVrOTJEcUxTYm8yT0pDWnljeitjdHIyN1EwUmEvV3Iyd0FNYjU4cFA0NEZCWE9yRUtIMElyRmNvYVY2N3pDTktWSWh3S0tRWFROUWdnTDFIdEpieW1zUFhZTnZidnM2ckRIcXZCT21xaGhReEZvNFgrSFV3MDdsZzQ5K3VPaUJYYlJaMDZXKzFwZk5nclpBbDhiVXR1NGw3Y3BHZ2d1YURaSWlvZTBVcHFEOENMRnd4UkIyWWcybVYweGc4NzgvQTgvLzBBc3dNNXdMSncvNVNoUSsyTFIxcUR3WXZiR2NvYkh6SXZIVGdMUzVXdHNoeGFGcjI0dFJ1aWM3MFZOcHJjVlYyNi9oNTI3N29zakVLcXBMSFJEUXpIMnlBUUVZYThCRDAzajJHYzlnejNiZ25RcU1TUlhXcU0wb1FEN3l3MDhDSStQaHpMRzZGcmtSSXRIR1cyejMxWG1JY1E0ZGdjd0FYUUVjYURNUjNlbHVMMi9aZjJPTUY5ZGlkVkYyS2dEazF5L2NZQmNxanl5MDZHWEhCUFBHYTB4TWFPemNlai9JSFNjMXpSNGFPekV1dkFGRmxvemhkZC82eC9UOXJSTHhZSElHaGczeVYrZS9YRHBEZ041VGFoc3ZxdHM3a3VLcGpMUXUyMnlCc0ZhL1NVM1hjcWtGNmpZaUVGWlB2TUFZZUYrSTVCbk14aVB4cGx2ZUxXZis0OW40NWkzZlFHWnpHbGhVcmhKU0paRkYwYVNvNDkxRVlDdWtObUdJbjNEVnIwaVZHTUFpZ0lheVNnQXYrK3lyNEVkQ01ncE5veFlXSFZaQ2hTR284M05jbDYzRDA4OThvaWhBbTJWR1lnVzl4QzRqZVpaaDU2RmQvUHlOWDZZc216Sytxc0xuQlkyM1dNVW1OVnRUV21nY1lUSXdKemtFUnNqUWdBdG1BZDhEQUd5NTZDZGtSRzhKOFlUVmM2dS9iMmZLV3lXWFFDRm1BTExvQmJ0bzR3R282WmgyRHB6R0xkdUU0aHF2T1pvTlRpdGdjaFUrZGRNWGVmbHQzNUhNWmxENnNNa2pWSXl4ckh5Rms5ZWNJRTg1NFVuUTNkdGhiQjZiNmRlc2UrSWRvOGhOaWpuRlB0UDJJSERIMHRMa085UmhxanVBSW5HRVFzQm9NTU9NaDJyRmJPMFJ1TGw3U0I3dzNrZnlEei82RXV5ZTI0Yzh5eUZLVmxwQll5ZVRtcTlFS0xZSnViZWd5SjMyZkEyOE9MWFNhcllUVnpaMFFuVGVTWjUxK0pucnY4elAzdndwc1V2WHd2c0t0WGZhQmtoZ0htQk1CdDIvRzc5OStwTmtzanNPN3lzeDBlQU5PV0lJYmRzQWZPSEhYK2ErYWk5TnA0ZW1SVm5jNldGOUc2QkhmcWcyYUZQL0djOVF0dENMaWNtNXFKUU81c2JobHdBQWI5NnlhTGUzWjRQNDRHWjcwMDAzRlRpa1g0bHVkc0I1VnlJaHpWaHd6UVpZYmRIcmdhWndLUDJib0NEQllVQTh2MlMwSTMvMXBkZW1PMnhzOHlENEpWSWRmTVc1TDhFS1hRWS82SHRKTnR6aVErVUJVdXYyRWNGZUZMVDc1QWMxelBwZFNTOHZFa1JKYXJlTk00bGdSUzJXWEZYUWpFeGl1RzRkWG5mTjIzSG1tOC9HNjcvMlpnNUZtV2M1QUlUVG5SYmpyRloyaUtxZ0RmZDZNVXppaWt4cjZPRy9TaFVqQmd2bGdyem9veThRcmxnZUc1a2hNWXJOT0tNbGJVVEFZbDVXZGxiTGM4LzVmWklVYTZ5b0JodFRZb2FmaVMxSjMzSDUrNDFNVEJpVW5yWGNsUmdJQ0hlUmhFa2Q2Ni9qN1VFWUJWTU5BdW5HQ2Nna040ZXFPYnZIL1FBQXNHcnhyQytlcG9oT2YrUEM1ekJ3Z0kyMmNTK1c4SVFNaHlZZFBDMW1MZTJpRFZhMzgySUxYbEZPSytITEVtYnBhbjVpNitmNWpWc3ZGMnNzWGFRNEVpS3RXS21jdzhabDYvVXhKejBjbU5zdkZwMzJnamIvcHU5TENiTzFMVm92UyswQ044L1hxaHNOZFdNUXM3KzVLTE1udlNuZXI2cFM2R0hYSFk2ZFN4VXYrc3FmNE13MzNRZnYvdDZsZ0RISWJDNmtsOHE3WkMwMFI5M2RTZDh2SW9JZ3RhSm1iVDk2cWlnVnhoZzg1ZExmeC9YRFcyRkdsa0tkSjJxYmtjM21Cd1JlSWJZTHYzdVh2UGpzRjJIMTJFcHg2Z0V4cklNdElKeDZXR1B4dlZ0L3dHL3QvQ1psWWhtOHhyWUtndFlHbEdZKzYxbVVaQTZGMTZnSVNpVXlZWkNBcEhTc3lNM1ZiUXUzN1BrUkRJQXQrQWxWY1FCd1djais2KzNZOXlVNU1OeHJlall6SUtWamdKNEFGY0lKT0VIMHR0cEl0UEhBcE5xWVZtM1I5QU9BQ0F3aG1PakphNy84K3ZqZVFDYlYxV29pcVZoQmN0aVExV3RhZGw0NlhyN212V2dhbVpqTUFUUXF0K2tZeGRaQ05ST2FQT3lrenRKellHUGpKcGdZQ0MzRmF3bnBkSkVmc1VtdU5Udk4wei8rREp6M2x2dkxaNi83RDRqWWtHSUZnWE1WMVB0WVQ1Z0tSKzRrZm9FNFV4b0hyQUM5ZDdCaWtKbU1ML25VeGZqb1RSK0JQZXhJZUpRQ0s3SW9KaDdtUWVCSUEwcy9mNGduTHp0ZG4zdnZaOU5wUlNzWkVWSU8wZ2xUOUxIYzRvMWZlcE5VbzdGaVFpQ2dTbDArU3JicVdaaDQxYkJaTllWQlFWUUtWQlQwUWlLMVdPT3NlbkN2K3lnQXdTc1duNUowWndBQ2hNTE1Yb3VEOW9EL2x1UUNnQ29aaUs1aFU5T2dxRGVvUm9DMTIwa2thUkc4cFlZY0RidVVFSUZ6RmUza2FuejB1ay9nVTlmOGgyUTJoMVBIS0ljQWhCaHI1WjFjdStzMklCc2p0UTdISkVHWkpCc1dFYUtwc1VCU0M0dE1GelRoeE5wV3dtS25wYloxQXRwcUlMYnhVaitsVWhVRG1ONEU3SkhIeUdYVjlmS1E5ejRHWjcvcC92cSs3MTJLZ1N2UXlYSm0xa0s5WStXOU9COE9SSWdDbTdFc2c2b0JIS29NWEIrOVpEYmpYTlhuMHo3MEIzak50LytPZHYxR2VGY0JOcGtNQ0RySW1PRCtCdnRYakJoZy96VGUrT2kvay9IdUNBQnBIOXBJWXd3cWRleGFLOWZzdnA0ZnZQNFRrS1ZyNFN2WEtQTjZBek1zWG5JNmt3QnF6NnVub0lvYWFOUUVIckJEeS8ybDUxVXpYd0JBWEx0NDE5MFZBSUZ6ZzJmYXVYbTR4UXdWdEVhaEVIU053Q0xZZ0JXU2xJdGlPSUV2cXJObWdac2tncVNPVTdrN1JlaUpiSFFTTnN1QzVkTDRpY0pnOC9EZzNNSEE5dmRHcUw2bVMrcURhaG9TdWpXT1FKTFhERWd6bGppaDZaU20ydmxvS2NhV2ttNlZRcmIrWGZUQ3dGeUtnWkwwWlVrenVvejJpRTM4Wm5tZFBPV1R6OElKcnp1Vkwvcm95K1NyTjM5YmpHVEliYzdNMmxDUEg0ZGhqSUdxcit0ak1tUFp5WEpZc2Z6b05aL0J2ZDk4anJ6bngrK0RXWGVrK01xajhVcVJDSHJUbkpXcnlEcGR1QjNiNUlYM2VDSE9QZnJlVW5wSEV4ME9nOWozSGdDcEFoajh6YWYram1YSGhYN1F5VkpKOWwzemQ1ejNOTTlBelFOQ2duL2dLTWdGWnN4U2xKUVJhN25EM2VSdW5QN2FYYWxmQUhjV2lWRU5NN3RzMTZlTE04YjNteVBHVjdCZk9lUW1RMWVBUGdRVkFTZmgzWWFOOFYvalcxSWlRQUJKSFRhV3VySktqSWk2Z3N2TW1OeDc0MWxBbkp3bzFxQU1RWmh0MHp0WmlCZllITEZSVW1ORFNRUUdJOUFhckFjQUtRS0RhNktoSG5tSE8wanFCbC90ejZzNTVmaDdlazRSR25pMjdTTkppd0dvZWdKT3pNUXl5T1FLYkMzNmVQMjEvOHczZlBlZE9HWHFLSG5jaVEvbktVZWRndnR0dUJmSE94T3dXUzVXREkyeFVJQ1ZLN0J6ZHJkODRvZWY0YjlkOVg1OCsrQlZncWtseUZadUZGY1ZSTllDUUxMQjB2MVRhQ1NIbXoySUUzcWJlUEdELzFpY2Q3Uk5UVkNVWUlxS2lvN041ZW9kMS9LRFYzOUl6QkhyeEZjbGEzdmRCSVlmck9ldXRjSHZZQmQ2aGlyQVNpRExMY1VJSVBEd01MeDUrQ0VBeEgyUjRiSTZvK0EvQVNCQWZCQjI1aUljNnU0dVArMDM2ZE44U3F2cFdjR0NBNFlDNUNReWhEeTd0c0dmQ0dtUW9iUlRtK2RpQWpBMDhFOWNHTWh4UzAvR2VIY2NxbG9mM3hET0kvU3dNUExGVzc5RWxUNHpzZUtDRzQxRytpQXFDbWtuTFlSZVREWEZFaWNxcGlIWHNSWmh5M2xxYnIxV1A4a2ZTR2NGcDRmckVnS21aWTlEcXFXb0FBSlZGN0toTzEzWXRlUGl2ZU5WZysyODZzcS9GZnpBWUVWM0ZUcWtqSGJIY2Rqb1d1bmxZOXhmSE1UdWhSMllLU3ZNOS9jSXBxYkVydDhBcW9mVElXQmpNWE1DWC8yTlFlSUh4NytRM25UQkR6My8vWmdhWFlMS1Y2SDB0ZEYxVkFPQkR3TisyY2N2WVRWbDYwcWR1dUZsNmxWZDEvWklFMlpOR1RDeHhUNHFCWW80cCtNWjRLR2NNTmJzR0xyc3h3dVhWZ0J3TGhTWDNSbHNkd1hBSmozcnlybjMrT1BHbnlyallsZ0NHQkVnbC9DRkF3RTZsc2ppZ05PeWdXR1JHNGtVYmkyQkl0S1cxbVNpQzlNODk5aTd3d0FvMVd1VzVYVmhGaU0zZFdEL0hzREcvZzhlZ0UwN2tsSkxKeE9Sa3dxWDJySGloSnltOGpKZ3A1MFFSRzE1eG16ZW95YnNDRkZwb2p2SjBaSjBMMmdlQkpBaUhXSmk0cEtLY3dWQXdQUW1ZTWFtb0tyYzd5dUI5NEE3aEp1bTl6TVV6eGhCTnllNkhXVExqakxxUFh4Vnh1Wm1rcnpTZXBMcnpSSXRsNnlUUzNYZHpmcTZDOThoSjY0OW5rNHI1RFpuZ0NnVFhTT2Vqcm50eU9ldStody9lY09uWUkvYUtMNXdTZGJWK3JZR2VsTnpIYjVYa2FLR1RXU2tKREZwYVhwR3FLUjByVFczRGI1ZTNqQjlkVno5VmxDNXVlNlNGY1VXZUJDeWNQbkJMNW5iK3Q4emVXYWhKS3dBSXpFOTI2bGcyTGE5ME5pQVFHcGdmWWZuZ0pTNHFncWdCSTVjZDB5NFRWazhoQ3pMNEtqNDdyN3JnZTZrOGM0MWtpNVVtOFdhNWJiS2JObWF0WVJzQVNvVnRwb1c2OWdTbmZYRmRqd0xxRHNnSk9NYjdjR3lDWUdGb0cwaXdLVUdTWHhPcVhCRlJhMjhDQXdrNjhEa1k3QWpVMkxIbDRvWlhRS3hYU09BY1VWQjlZNU5DN2RhZ1RZTm1WTHBnUk5rZVEvVjlkZnAvMzNBeGZ5OXM1K0d5aFZpVEI2eUdrTHlEbUZDclpTUkRHVlY4QTgvL0NlUTVWUENLcGxSY1NYcVRtZlNhSWwyUTlCWUtRTkdvVkNGTlplcFRLaUFaRWJzckllNXJ2eG5BSXgreFYxZVAvRUpuQXNMUUxOdC9iZVl3Z05ablBoeEUvaEJGV0NvZ3FwZXRMYW1ZczFac0EyUzhEb0I0SDJKYmpiT2N6ZmRKd3hFakFnaEdxdllEQXptKzNQODNxNHJpZDRvV01YRXRXVGoxWVU0YVhJUzJDTGkydkhKWmlMYjRmVFc1bWlCckw2SE9qZTBrYlRoUHB0RlNTSXhaUUtsSkkzMmZTZmJGM0hSWWlGM0UxSDA0WC9laTFLRnFTN1N0QWplWklNbTZxczJBOEltejBkNjRtNitIaS8rclJmeXp4NytjcWw4Q1d1ek9FY2hvcE5PSnZEcVlZMlJsM3owTDNCdC8wYlk4U2xvNVJBM1VrSmhRK3ZVYTFkcnMyVEhoNW1zUUF3OU1TcVFzUXh3Vkl4Ymk1djZPMFl1Mi8xeEVJTEw3dXg4L05jQXZDeEl3ZTYvNy9rSWJoM2NqcDQxVUhwa05raEJ4SkxEdmtwZC80cjJJa1hwRjVuOHh0Nml3Qkp3UXk2MUU3Sm1mSlhVQUJSQWpKRlVBWEREM2xzd1VHK1FaUWo5TE9xRmp3QkxjVTl0MnZmNktDR1N2VkpUUjYwZDNCeEIyaEtBYkxWTmE5OERFckRiOTljZ010Rkw3UTVWcHZWNnFiOGswZGpwVGVGSENCZ1BXRStJVDZkZHNuNHVBVHZjbjBUYk5YcS9SRDdTUTdYMWV2L3NVNTZGMXo3K05jYjVTb3hrRU1Sa0E0MzJpZ2dyNTlESmNuN2hSMS9BVzc3K0JySHJEamUrcUJJcjBHN3VGdWNEclBtL05KZ2tBTHdQZGRDRkVrNkI1WG1Zcmh5S0RzRHJpM2NlQW1id3luTlMzdkhQQ0VDQWVDWHNJV0RHL0hqdVhTWVJHa0pnaVVWMExzSUFpdFppMXBLUUVTQXhsSk9lQTJERkVITnp1UCtHKzJDc055cGV2Y1RHK21GMm8vMzNyZHN2aDNPSG1Ja04rN0E1NWlCS3VaUzNLWXpIaUtUbkkxanFuZG84SHFnRXFjRlVlNUlKRUMyUXRWVjcrcmRsQmpZU1ZacWxDZU5LNEd2Rm9MWGxRTEN4NTJvS3EvN2dWZytjcE1iWlJHYmlrZlVpbEx6WGsrckc2L0NjbzU4cWIzL3FtME5Oc1JoWVl4S2ZUR01zRFV5TWVCaHNQN1JMbnZpK1o3TmFNNVdXcCtYTTFkcXFTVkdyOTExYml5SE10YU9nOUlLSkRHWWloNVJLTTVGYmM5T3dYMzF1N3pzaEFDNjU3QzV0djNUOVp3QUVMZ2xTTVB2V25qZHg2MkFmeG94RmhSQm1tYlNCS1ljQUMwcTR1R05yZTBVQ1IxWVR3YlgwQ1Z0dE9KQWpWeHdlbG9hcHJwSVNqNWNUQU5oellJOGl5eHRqdTU2QTlxUUpGNFdnQ05RTndUWENwWmxncVJlZlNBUnJDN0RwOFRRMUVWZzBUREcxUlZHVVNQNkV6WWIwSVltZ1RSUkdRNHhIcWlRQ2N2SEMxaExWTkhVdWpYMGRNeVVOb0lReFZrUnlWRGRjejk4NzlkbDg2elBmSms0ZGhZQTFOdWJiSnY1ZjZ4UXdZd3grKzUzUHd2N09QR3h2Q1ZVajBwdU5MWXNkTVliN3E5VnhzdTFCZUFqS0tHQldkY1BZRGRWWUNMNC8vMTRVeFczNFlERGovak9JL2VjQUJJZ3RtODNDWHV3eFY4Ky96UmdSWkZSNEFoTVc4VUNiMERsMElTVkZ0aVVJNjNtdG4xT0JVeFhSSHU5enpOa0FRQk5CSnhCQUZWWXNQWUF2YjcxQ01ESk85VDY1L3RLa1cwVXdLVkJYWm5ta0RHMFJwVml4c01hRzUxTEpaaHBQKy9DWGlJTzIzMUZ2SENScUl0NFR0Q1dCV3lxcFNWRnZBSjAwZ2JEMWQzcE9HazI4eUphTkIrTWtDVm5uV3FyQUs3T3NBKzBQWVcrK25YL3hnRmZoSDUveVpvU1lNOFhhTEo0Q0ZUN1hoUE94eGFsRFppeWU5YzVuOHo5Mlh3YTdkSjM0cW9pQWp3QmZuRWlTUE55bVEwVVVIcldKVWtUcVpUS2pqQmxGNFluSnpPaVArLzNxUDJiK0hvVGdvcCtzZXROMTF6Uk0rN3BvaTRLUWNkbjFkLzFqUjUrcUo0K3R4MzRsT2xFSzduZUNUSUFCZ0RFQ1hTSm1SRVNad3hCQnFhZWVvQ282bGNHcGEwNE1Ib0l4Q0dYQlFROVphOWtmOW5IenZtc01sdWRwRmxxZWE1d2dqZEpIQWREVEdBdXhPV0FCUCt6VHp4OEttbkIwV2NoejE2aGlUWXhmSmlyR1M3RGJKSUloNmR3a09XT2FJaUlyZ3lSVmdTYkpJbTJHeFlSNGVETDlMWUowM09zaVZaeEFHTTdTYVloZmlVaU54d2JaUElmYnM1T3JpaVY0LzdNL2pQTk9PbDhxVjhKa2xxSkdGQ0Y2Rk1rQkVaSmVIVHBaQi8vbjBqK1JmN255UGNpT09oNnVHQUJpRzNvbGpFR2l3ZGdFRklMRWorSTAzcDhINFRSSVB5R3hJZ3RuM0ZvNDZTREg5WU4vd2R6YzlkZ1M0MmIveGZWZlNjQXdTeGZCekFDSHNtL092c1lXYXRBVER3L0JFa3QwcENuYm5QYW9PeW1rUS81cVQ0NUIvUm9ETFBSeDJvcFRzWFRKY29RdW1HSFI0NW9SQUg2dzg4ZVlkaFVsN3lFU25vdDNZMVNKbGdiV1pqQjVGMW9PNFEvc2hyLzVGbzRkVU41bjlHNjQxOGpKNEk2ZDBQNUJaSGtlOGdwY1N6VXZDcmVoa1VRQlg5RW1TanUvQmJha09wTjZyUXZENDZJdEdtdFM1MlR0a2RmZmxTUnlRbWo0NHJxM2k1STI3NURPd2QxNkV4Nis5TDdtaWovNUpzNDc2WHh4dm9MTk9oQllDZlY0SnB4TUZRcVhVREdBNytKUHYxYmVlTm5ybUIxNW5MaWlpQnVCV0RTT09vY3pqcjBkUTQvanFKTWRxaWhrbG5WZ1Jxd1JCMkxTR252RGNDYi81TDVYLzdUU0QvaHBKQ0FRa2xWNXNSbklKVy9QVHVuOVBzNWVlU0wyRjRyTUNKWmFZSmNMVVk3U0EvTkNqSmxnSDBvRVk1QThBaENHT1hSMmhpY2VlNlNNNUIwdFhTRjUxZ25SdVZqK2FHRng3YmFyVUZXSE5KZVZ0dEpoalZHaGlCRXJ5Q3c5bmZpNVdXQmhBTXdQc0duRmlYSzN3KzZEcDk3OThYTGlocnZ4dUdXSEE0Qjg1cm92OEU4L2NUR3YybmFsd2FyRFlMc2pWSzNBbEVRaGNYYzNFbTZ4VFFscDJXVkE0OENnOW5YcUIxTllzaGFuU2ZyRlY5ZHFOMzVMTzRTWnpJQWdUY1ZtT1pWZS9LN3RzclNjd2w4OTdBM3luUHYvTGdTUXlqdm1rb2RPWDZHSlFvQzZNVkRTZVBYSWJRZXYvc0liOGFyUC9EbXlJNCtrSzh2RmRtZ3RxYVUxZ2hReFN2ZkQ1aEFhU294NkVPZ0laR1VlR3N2blVHTk5MdCtkZisxd09OeUdpMzQ2NlFmOHRBQUVpRmRlWWlFb09sZE0veEVQSC8ra0xzL0lCVFdZeUlnWlR3eFVrRmxnbG9LY1FDWXRZU0cxQ1VnbzRBMU8yWFI2MlBKaUdrdlhpTUNGMWJ6dDBGYWcxelgwamtLS3RSMm9KZFZYOUhNSGdkbFNvT0M5MXB5QmUydzZGUmVlOGtpY2N0UVpuT3FPSnpCSTZVc1lFVHprK1BOeHp0SDNremQ4NlkxOC9XVnZ4aDY1RFZpNWtuWmtWT2djMUd2a050a0FxaDJDRTJtMzNZMlB4YjhYZWRPb0hmRkFrbXRUVFpRY3R1Q3dKR0NtbUhLMCtZSzR5ZkpjTkJQNC9idGdCOFR2blBvVS9Pa0ZMK1BoeTlhRkJxTWlrdHNzNUNVYVE5WFlUQ2t3VDZKVTdkak0vTVduL2daLy9ybExZQTgvRXI3eXllUm9va0prTUQ4YWFpYzFKSSttU2JKeFU3b1ZnVktCaW9vTlBaRk1SRXA2THM4eVhERjNYZm5KbmEvRHhUQzQ1RDkzUE5xWC9OY3ZhVjJYd3VJaWVIdmhZUi9VUjYrOENMT3VBb3hGNFlUYks2bGIraG9BS3pQV0JHMWROMFdCRmVEVzdiajJKZC9oQ2V1UHAvTmVNcHVjSlFQbksyUTIxL3U4NWVIeTdaa2ZDaWJHeFEyR2lybERnb0ZpekU3d2ZtdnZvZmMrOWh6N3lKUFA1OTAybkx6b1BsSmlLeURJakJVSTZMeEhaa08wYzlmTWJuM2psOStLZDEvMUFiT2ozQ2xZdGd4bWREeGd6THZRdWl0a0ZyYlNrbXNnTnVTc1Nhb3oyby90MXpZcUxYV1BZc09KMWhLbU5qcWdnTFdXRUFOUER4emFEL1NKUng1ekFWNytrSmZpSHV0UEp3QlU2b3lOQncxRmdkellEd29oSGF4a2dBRmUvdjQvazcrODdHK1JIWE1jWFJGUG9FL2YzTVIzSThVVDRwbXRMS1ltOHpubC8za0NKWWdGTDVqS1lUYU5oR080SnF3M0I2ck0vK3YyaCtPYXVVL2pRdGk3eW5yNVNkZlBCc0NMWWZCS0V1dVhINVk5WThVUDlhVFJKZGhmZ1YweDNGY0o5bW1JRlR0UGpCaGdLb3U3VGdFSVJVU29EbFA3aVd0ZjlpMnNuVnFoSGdvTEk2cUFNU0V5UUFVMnZIUVRkOGtPb0pnMHkzdkw5ZnhOOThaRFRuNkkzT2VZczdCcDVjWmFpWG52UmFFS2hiRlpGdUlwR2dJYkxRT1hCSTN6bnJrTmh4RHVudDhySC96MkIrV2Z2LzErL0dqK3g4UkVKcGhZUVd1dENFQ3ZMZ1p0N21qS3BFM1Y3S21XR203Uk1XeXljV3FRU1BCbTQ5dU55UUFSZUZjUWMvUEEzQkFqZVJlUDJuUUJubmYvNStQc0krOGhBRkE2cDlZWXNUR2hqMHhNRmFIUmMzUE9vNVBsbUt1R2VONi9QRS9lZmRXN05EdnFlT1BMbUtTWGttcURIZHFjd3hLZWJIbThiRE1EVXBkZ2xCUU1vb2c5Y1JTbVl3SlZ2VFRMK01FOVcvaXhYUmNsQWZXelFPcG5BeUFBYkE0SXp4NjQ2am5Zdk9hdG1xSGlRRE5tRkd3dGdRVUVObDhacUpvbGtRWUJZSzJGbjkzUEI0K2R6VSsvNU1QR082Y21NNmxYVkF3VFdWNSsrMVh5ckxjOG5lZWNkcDQrOHVSSDIxTVBQeDdySmxZRkVZbFFuRU5TSVRERzVwUW9kVlBLWmNoOFM5eEhiWUtGVG93KzlHUEliU1lBWk1FUDhLVWZYNGIzZjM4TFBuN1Q1N0hnRjRBUklaWXNFK1I1T09PT0VGV1BjT3FlQkRPaVhiSld4NXNqRW4wRDNQQXFBeGpDaUlIQXdvc0hTdy9NendMOUJaZ3F3M25yNzRNSG5mZ0FYbmpHbzNIa3lvMEFBT2NyVW93a3FaYzJGRUZCd0dESW1uWk84aXlUYmZ0dTUrUC84WW00ZlBvSFl0ZHRwQzhkV2p1bEZwc0FrclJySHdiVThMVytOWFJJU0RUb2UyRGdnU05IWUZaMEJBNHFLNnpnbTdOei9zMjdUZ1FITzFzR3hVOTkvZXdBQklCTE4xdGN0QVg1a3cvN3JILzBpdk4xbjNNUXllQUozRnFHc3J5MDR5WXpZZ1FBUmJKT0RyZnpkajc3dUNmajdiL3pKcFN1WkNmcjFHTWdLVXJoekhBYU1BYkxla3ZTR0ZtNlVtQ0NIckZpeFlpRVFKeXFpQUdGWWhwS0F3Zzl0K081STRqbGh3alRZeENDOG9SS0ZwdEVBc0RPNlYzNDJEV2Z3dVUzWHNIUGJmOHk5aGF6SUJhQTNBcEdsd0M5TGlBR1lqSmFhOE83MHBGVm9vMU5xQ21sVEVYcHdwZFdRZ3dXQlBPekFNYzVibk41d09yNzRlNUhuNGFMVG44Y2psMnpDWEgwVWpsSFkwUlNEOEZRWkpNQ0lwSHRaemlNMjBKb3JaV1BYdmxwUE85ZHorZU9mRWF5bFd2aGhnVmdKZWg5eGtZdXdjNU1kaDBhUXAraGsycWdXNW9PRklJUXdsend4TUFMbHVXUUkwY2hGWWtseG1Pb21iNXQrMVB4dytuMy9xeXFOMTMvUFFCZURJTy9nQzVkMDlzdy80eFZWMWVuVHkyUlhhb2NNUmJURHRnMkJHem8rQVlCTVNtQUZURzlEdlRXN2Z6UTA5NkZ4LzNXSTFFNXg4eG1KdTVrR0VhQ0wzUStrOUpYWWlWMGY3ZGloTkdPSVVRQkZhR0lCdTRzdFB0RjRIeXpMSVAzbmhBUmF3eFVsWkRRZUVaRVZGVWxoSjVqTFk5NlFFSVVJWUlBQndhSFpOditiZnJaR3o0anQrL1ppZThldWdIWEhmcVJsRW9XY0lEMkFSdFRxQ0JCNG9ZakxZQ3FFbmdya0ZGMlRDN1dPeTRkV1lWekRyODNEcHRjaFFlY2VBRk9YM01jVmk5WlUwK3BWNFZTYVkweEVLTlFGUk02c2RZRnhrSUtHUTRXcENyeUxFUGZWZnl6RC8wNVh2K2xOd0RyVjRqdFRzQlhSYkpSbzJwTnF4MzhvdUQwUkVsTkJCc3ZSWlFTRjJrbFNNSjVEeFFPeUVYTThSUEJ6YkowWm1tZXliL3YzK0krdFAwaW5JTXNKaHo4Vk5UTHp3OUFBUEZMM2NpWms1dkxwNis3MUUvbERyTnFrUnZCL29MWVZRSTI5cXpMRkpnd0lxTmQ4S2F0dlBMUHZvWlRqendOcFhQb1pCa0FpaXFaRHJEMlVWU0pNVENrcUZCakErMXdnZ1BTS2JLRXF0S0lRV2F6K242bUYrWXdOUmJPUmZhK0VvaWxOU2J4dVRUQkVqSTE2eGFGUXFqcERhdVZCUlZkVDJnRllHYmhJT1lHMC9qaG5odnc0d08zeVB6Q0hucFhrU3FobUU0aE5qZUVRa1lubHVHSWxadHcxdXFUWktvenptNXZGRk1qUzlxNkd0NDVlS0ZZc1F3NW83Rlptbm9ZWTBuUUpNa2Rha1VBNTB0MDh0RGgvcXMvL2diLzVBTXY1YmYyZjFmTWtSdUpDcUxPSWRvanlVaEV0Qk9TazlRQTBrY0Jteks3ZmJST3M1aFdOdXVBSVFWT2FZNGJEY21tcGZleXBtdnNOMmEzanJ6NTVqTm1pSm1Jb3A5SjlhYnJ2dzlBb0FhaGZkQ3FOK0ZwNjU3cmg3NUNKVGxBeW80QmNFQ0Z1U1Y4SmRMSndCSGxFZjFKZk9kVlg4Q3FpV1gwem9mRENvMkJpYk9zemJqcXhVKzFORjY5Q2ZWQ2lqeXJTelFKQU5zUGJPZG5yL21LK2ZhdFYvQWpQL3lvbkh2NGIvSGxqMzJGbkw3aEZBQmc2YXVrdWhObkJvRFJYRXhCN1BCNDdIcEZPb3FuaDRFd3k3SlVzckhJNEd1TlFlN2k5L2JGMGxlaVNnUm5RbUN0alpSeFk2cHFHRlNkVzBNSWhSVFAwRU1SQUE3MEQrTC9mdVF2K1EvZmVJZHcrUml6eVdYaXFtRnFSbTZpRDFUZkQ5b2dDK1J5akhnd3hYa0RLSTBKSGRFTWdCbEhERlZRZU1qNkh1eTZuckR2eWVXWnlpMERpdy91ZXFpL2NlNHovMTNWbTY2ZkQ0Q0E0R0pZWElJUis3dnJQNHNIcnJpMzduR2VWcXg0RU5zTGNNWURtY0tLRlQ4OGdQT1gzWnVmLyt0UGluTVZNNU8zUVFlaHhrUjVRMElsVmd3S1ZaRlppM1pYZ1VNTGgzajk3aHZ4aWFzL3k4dHYrajR1My9WZDlMVXY2QWt4dGRSZytpQTdmY0VmbnZ0OCtlTUxYb3hsb3hNQXdNSlh5SXdWUVdoZjNIUmRDVmRnSWNJWFI2a3JERGtTSnJHem50U1l1NGZhT0pQWWFoUkoyNGVIVERwUE4vMmV1bzRseWxGcUJoRUFSTU1MSUZFcnFIckpzaHdDY0tIbzR6Mlh2UnQvK2NYWFlIdXhtM2JEUm9FSGZWVkszVmxWWTFwTjJnYnA2SWIwRmNIekZYZ3FTQU1hb1BKQU55TzZWc1FZOG1BSjlEMVFlTWpxcnBpTlBiSlFZTkk2ekd1T2Y5MytQUDMrOUp1VEFQcDVBUFRURXRFLzZRcHNrY0ZjOXlQYkw2eVdkYTdncVdQcnVGODk4OHppOEI1eGN6OGtLb3hhWU1GajlhYVZZYnVMaUFkb29TRmRWOEpKNTFSS3BSV0VSTGZUQ2Q4UkNuWmsyOTZ0K053MVg4WjNidnEyZnVxV3o4dnV3UXlRbGNDU1NlQ3djYkYyS1VBWWRaNTI5VHFVcnNUZmZQM3YrTjV2ZjRBdnVPL3ZtV2VkK3d3c0c1c0tuSnFyUUJFUk1aQndnRjV6VkZCTVptcitEZ1ZyaWIrMVlrd0dtM3lhVkdYRzlGY0NzYW1weE9BSDBDZ2xOR3lNd1llV29JdHdFWXJ4NnRYVG81dDFZSTNoWERISDkzOWpDLzcrTTIrUTYrZHVBdGF1Z0owNjB2akJNSEJYa3BKcGFlNHNrNU10S0ltYlRHRTFnUnFpVkVFdmg0em1nbzRoOWhiQXJBdDl2U2N6bWlOR2dFTEJuaTBsTTExK1lkL2IrUDNwTjBmSzVlY0NYejI2bi91S082RjcxTmdEK0l5MW42dU9IZ1AzcTBGSFJDcVN0ODZKOVFMZHVwdnZmZlk3OEtTSFBWR3F5c0htbVZKRG1hRW4yY255UmNKb3gvUWUzTFpucTlseTFVZnhnOXV1MG0vYy9pM3htUXE2UWl4ZEJuUzZ5SkNCNnFHaGtWdXNGMmgydk0xeitHSkE3Tm1MSTBZT04wOC82NGw4MnIyZWdrMnJqaUNpMkhEZUJTeUt3SWhKaEo1SXF2YXB0ZVNkMVcreXp3QlFBeXNOUTRhM0J0WklOS3JDQ0JOaGJaU0Z6MUhWWk04aUQzTkFBTmg1Y0RmZS9ZMzM0VjNmZnA5Y2QrZ214Y3BKc1NNVG9xVWpsUUtibW10RUM2SWh0aE9GRWpOcEpKV3doc1JkU25CQTVqM3lsVDJZeVM2WUM5Mk9QblQzTUVSSFJnM3N5ZU5pMUVEaEs2ektjM3gwMy9lbVByajkvZ2N1UG1lQVN5NzdiemtkZDd4K01RQUVCSnVSWXd2Szdxa1R6eXgvWi8wN3VhTHJzTjhaeWF5WWhRcllYOEovNXlaODVkVmZ4WDFQT3h1bHE2Um5jdzN0UDhJMTE1K1QyM2JjeXMvYzhsVjgrNXF2NGJMYnI4Q0I2cUFnODhERW1HSjh5cGc4SEcvcTRVaHRRaTNSakRLMW9kMlNMV0lNVEdicDUyZUJRNGVraHpFKzdNaHo4Smd6SHkzM1BlN2VPSHpaaG5vdW5IZHhwUUtGWVkyUlJSQ01sRTc0alRHVUFCR0VjNlpDU3pFalFlQUI0UWpNMEF6U3hMZTIremNCb1RFazRtTE9WME44NytidjRuM2ZlQi8vL2NyUFliL3VFNnhhSm5aMFZMWDBZTVVtOWN0S0VzcUMrc1NBMWxacHgzeGpack1ZRVE0Y1VTckdENStDWGRvVk1ZTCtiWE1vYjF0ZzZMWUEyak1uSVZaRUt1OTBWU2ZEdDZadjhHKzY1YjR3MkJzNjB2Mzg0R3NHKzR1NkxqNG53eVdYT1hQTzFQT3pKeDMraHFxbkZXYVJHV3ZnNStaeCtDMDl1ZWIxMzhGNGI2eFdGRmZkOUNPNTlyWWY0ZVBmL0JSK2VPQUd1YlovTFRDUkVWMExUQzJIZEROWWIwQjY4U0dKczFHTWRaMVgwaldSU0cyY2hhYXdHZ3B4b09uazhIQ0MvZnVCdnNka1p3bnVmL2c5Y09HWmo4VlpSNTZGWTFadGFrK3NLQW1uTHRCQWkrODJTUmNZaVVVL2pSOFlEM0tJUEpRU2FnSjFiRkRUUGZXMVoyRS9idHgyUGQ3N3ZRL3lhOWQvQjlmdXZ6YTBRbG01VW15blMxYVZxRzk5YjgwOUlvUUJVODRna0xpOWxOb1dYdCtXVlRNRk9pTWRXWEhDS21wSHhCUG8zektMaFJ0bmdHNEl4MlduTEJFZHNXUlpPbG5SeStYNi91M1pHMjY3ZnpGWDNBeis5SWtHUDgzMWl3VWdVSU93Yzk2S1A5TW5yL20vYXFYQzBGaWRQb2l6RG03RU81NzlGck56ejNaKzVKdWZsQ3R2K3BGKys2YnZDanI4LzhvNzkxakw3cXFPZjlidnQvYys1OXg3N3AzN25EdHpaenFWempDVVVtd05ZdEltMnFhZ2FLMnBoRXdWUlEyYUVCTDlnMmlRQktNRWpTU1NxSDhvaWFEaUF3MENHbU44VkNEaFZhQ0FMYS9PbEFxVlR0dVp6dXZPekgyZWUvYnJ0L3pqOS92dHZlK2tHQXpNZEthczVNN2M4OXAzbjMyK1p6Mi9heTI0b1ErN2Q0a01CMnFzN3dSenJoTHREc3BwNWwwM0RsUW9Od1IzWGpXc3FmSmhZTnZaN3hWUThGaFZBSnNrNGhCY1VjTGFlUmdWREpLaDNyNzNCemk0ZklqWDNIdzNleGIyODhLRmd6SkllckRUL0laakFwMEd6MHNrbXRodXRDd0FYenZ6ZFQxLzhhemUvOWpIekZlLytVWDkwc3JqbkZ4OXl2dkp3eUV5TVlYQmlLc3F6OWhwdUpXdVpiTFE1S2lKWDhNMndvM2drMlkrdEZaT1dCc3p1MzlHZDc5NHQyNXZWVktseU1ZMzF0ZzR1b0wwRTFSUSs1SWhac0tLcTF5bGM2bTFSN2N2NnQrZWVsVjFldjJoV0FYN3pnQ3lVNzc3QUFSNHc4dFMzdk53YVg5MC92Zmx0Y3R2clJOYjZFYVZwcWUzcVo1WVViMjRMU1FwVEtkcUptZkFxVEhXcUp0UGNWUGlDOS9XRUNnZUlaMFFFcVN4dENSdDRCZGNMMm5pZ09hakQ3KzAvQS8vbmlNeDFQbmcxcGdFVXFGMnRiSnhVUmdYeXJoQXNrbGVPbnRRRnJPaDdwN2RMeis0NytXOGVQRjZ6ZEtNWk5CajBCK1NKUk9hQ21LTlFVV29hdWZ5Y214RzI1dlUrWmk2cXVUNHhta2VPdmtvVDU0NXBxdmJXL3JWYy85dFJ1TVZaV0lDTWd1OWFURzlucmZPdFlxckszL09OclExZE1tNGtYTzRvOCs1RzJSbzI4OUJpSDgzSzVJYTJmdlNKWjNhTXlYRlp1RzBiMlhsa1JYV2pxNklwQ2s2QVB2aW9acUpSTnk0cm1Wdll1Mlh4eVBldlhKUHZuYis0NWNEZkhDNUFBakMyKzZ3dlAyVFZmcFRDKytzWHJmOFpzMWR5ZGdrRm9Oc2x1alRtMmhoL0hqWm9nN1paNEhEUTJIQytQcGpkUEU2VnBkb1ZsdXI2ek9zTWZNZktWVStoZGE4aUxqQnNlbnFoNWFkckdEREZBK2JoRURXYUYwNW9kZ1F4aU5mRFNnRnFoenF3cEdtZ3BuUXhQVElFdU5wWlNxbXJDdk5KUmZLTGFVcUJac3FwdWZMWWhsSzJvUEJsSmlRV3FFQ1Y5WVM2c3lSVUI4QjFxYnkvR1ZvbHdGMktZZCtHNVJmcCtIQktpSkd2ZGJMbVZtYzVycGI5bWt0VUJhbG1KN1YwNTg5eGRyakYwVXlpODRrYW02ZTh1M1NoWE82bEZwemJHdGsvM3JsMWZtcEN4LzVicVJidmpWUUxxZEUrdFo5dS85VTdsNzhWV2VsMURWSlZCVFdDdUhSc1o5MVl0VlBWNnJ3UURvMHFjd2x3cmltSWJWMno3VkxBSkFkajdWK1g2VGZSNTlRTkZEdkNTRWpnWTJzejM0VndoWWRnL1ZUVGVOa0J4R1BkaDk1NHdmUWF0dlNxRDRQYU1UNmRmY054VDVTL0IxMVhVWGRST3lWYjc1bkV2TjJ0RnJQbjJ2UWR0cHF2emdGd29VSC9ISVluMVpmejdFaTdIL0pYbDI0Zm9IUnhqYXViNmkySzNubUkwK3lmV0ZiSlRXaU14bm1sbW4vK2xKcmRxZUpQTGE5d2QrZit1bjZtMnNmdTV6ZzgyL3k4a3BNVkZmcFhmTi9xUGZ0K1hVM0ljNnRPR0V5RVM0VWNHekxuMFVha2hTVjg1T1dEdlRodWdGVXRXOTJGdU9mb0R0OFFDQlFVNlNUZmhBQ1FCdU5vSEVwaHRjeUJMNmZ0a0JvWGtjTEtCZWEweHZDWmtDS2Q3K2EvZzFwdlZIZlh4Wm45a0Z3RFVTYmN3SC9aYU1EL3NnWFZJMk5YaHFlM3ZaWGEzaXJYUlBjbWwxL09SUjB1MWEyQ3pPM2I1YjlOKzlWazFqeVVVNDIzWlBWSjFiMXhNZWZvc1lwemdwTEdmYVdhU0YzZ0N1Wnl4THp5TmFxdnYvc3ZkWFRxdzljYnZERlMzNjVSZmdnaHZ1b2t6c1gzK0x1blgrSFcrd1p6cFUxQTJNcEhSemJVRFlkOUJPZkFGV0VzWU9aVkRrNEVDYU5rcXRTcTRsYXBqRzVqUzZJSmpnQXRESGZuV0FsMEJKOTZLaWVBbUdrMVpaZXdpZmUwcm44dlMxK1FsQ3RRWU5GVU1hREJLQ0dmRnYwT2RIMmR0TmxGODN0amw0UjNURTRxWW1zd3pHY2htcEgrSlo1MHJWb1VjTkdwUlBUZlpadjJzdjA0bENLclZ4ZGdxWnBJaWMvOHpRclIxZUVxUlJLVlRrd2dUMDhLZVExemxESlhKTFkvMW83bDd6cnhFK015dkxoS3dHKytLNnVoRFFndEhmTkhkRlhMUHlOM2pBeDBKV3lRdkJGaGNlMmxiT0YwTGNoeWhNaHIvMXc5QmYwWVgvUDAveTMxWVBVbUk0cDFnN3p1REdIclk4VU5hSkQvWlNzbUh0MG5maFVPaVlROE1tVmFQTFlHY3cyeDQ4UmFUUzBUV0tvSWFnaXN1T2xVY04yNmZ6UkQ0VllOdlB2SjFZc0duOVhXNFl5WHVkcHFiQlZTSCtRNmRMQkJXYVdaOFNWTmE1V3NtR21HOCtzYytLQkV6SmVHeXNUS1ZJbzl2dUhzSzhINjA2MEx4Vzdra1EvdC82VjVFLys1K2NLZVBSS2dRK3VIQUM5dkkyRXQxTWxTNE1mc3IrMDkzM3V0c25EMWRtcW9qQ1cxTUNUWTlHbngvNkRUWUpHcUoweWRwQVpZWC9QWHpncitNRklqUU1YVEhESFgxVENSQzVvekdYelNadG9KbWw2TmdpSEk0SzZ0WkpFYTk0NC9FSlR1R3ZtRlVyVDAwSGtlWFZpb09hOEhJcHhzbU9LYS9SbmZXTlVKNkVjKzBlSUpUVEIrYlVxV2loc2w1SU9yQzVjTjh2Yzhvd2ZVcFZYWkpPcGFGN3J1Uytja1F1UHJTajkxR2R3Qm9pNWRWck5kQUtidFdQZU9pMDBsVTlkK09mcTcwNytBc0xXZDBvdStQL0tsUVVnTklISkVCYktYOXYzVi9WdHMvZlVoU2wxclRiU3M0YXRVdlFiMjU0SzFEZWhkT1NqUmJaVjZBTXY2Q3ZMZmM4bTJIWnRpS0l4Q3Bib2pVVWlnUE9rVE5NbXI3dW1FR0xWTE56b2dGdUREeGcxVUZNSTZZU25RcHpjMzlXMmhMOFNBTlJ4Q1loZkZtbTFlSFF0QU1UNEpkLytoc1JWRktLQ2xrNForUUZEY3dkMk1YZGcyb21LVktXSzZWc1ZsSXZIenN2S3crZlVxUXFEVk1sclpHOVB6RTIrQU9BS1Y1dUZOREhuU3R4SEwvNVIvWituZnNQenZiNjdTZVp2UjY0OEFNSFhqajlGaFlLOWQrODd1V3Z1emJxVW9tZkxTaFBqdDBpZEhDdFA1WkRYUWlhZ09FUU1wY0xJK1I2T2ZUMVk2aWs5VVFvVkN0Zkd4QkxOc2ZqSU5YWjJHY1IzdkxsdXRhRHJBMExuSU0zTkp2aUl2M1I4VUloZDczUUNtNDcvMkdqVEFQNW1yMXYzcDIxS2orblAwSlNPNEhPam93cUQwWm5sb2N6czJVWGFzNVJscmNrZ0VZUFJ0YTlmWU9XTFp5bTNTbUU2Z3pGS2l0aWJKakZMUGRYdEdzbWsxbW1iNk5HdERmT3BqVGVWRDU1NWI4ZFdkSHlNS3lQUERRQzltR2lVek0welA2LzN6djJ4ZWNuMFlyMVpWMnc3S3hNaXV1V1U0eU00bFh1MTFqZFIwd2lWK29uOW1YSHM3UW5MR1F5TWI1b3V0RTNQUlArcHRhOFFpL1ROc3hUUHg0NnBGdUtkTVpob3o3b3h2SjBXT1kwT0pFVGVmSXRUcGFFaGVDQzM2Uk5vVDBuVlI5MHh1ZDUwcGltVWZuajU5TnhRcHhhSHBQMlEzMGxFcFlMTjQrdHk0ZGlLRnFzNVRLWUlJcG83N0hWOU5ZY25SSzNneHM3SnZGVlRZK3VQbm4vSXZmL3NyMEQ1MVdDUmRuWXJYMEY1TGdIby8zNElUb0RyKzY5WityUHE3dDAvWHZVdFpyV3FRS3pyRytGQ0NZK1BZS1dFVkNFMWthd0NwU3BGdUhienFYQ2dCek9Cck9vSFo5UDRjOUNKT0xYRldBUmRwMzluUjFEano1VGc1MFVLWWV0L2FvdXVKbWh3UVJYdTRMMXdTZTl4NTM2L2VWUkNTMmk4VDFBbEd5UU1GeWZKSnNMN0ttdDE1d3ZHVDI3SzVvbE5kVnVsQjU0VjBiSENuTVVjbnNET3BxcUZxMnNqdFpsSmV2TEVKdFZuTHZ3QjkxLzRYWVFSUDNMbGdvMXZKYzgxQUwwY3dmS1BuaENlM0xuNG03eGkxMi9MQzZlRzlZWnpPcXJRMUFvSnlMbEM5UGcyWEN6OWZPckVOSVFBblBxSnJhV0Q2UVNXK3JDVXdtU1lNRnJpSDRNd2dGdWwwVTdPRXdjYVA3Q05VRHMrSDVjbXJuY0dJYTREbkpoN2pFaHVBQmtmYTkzSDFsOE5nUkhSRnhSSWpHUzdNbnJUUFpJYTZyTmp5cE5iV3B6WnB0NG9CV3RnTXZGUHpnVW1SZVhnQU5uVEU2a1ZLVjNGMFBwNWJaKzcrQTN6TDJ0dnpNK3NmU3o0ZS83ZjUxaXVEZ0I2YVV4eU5wVWRkcS9iODNaejQvQm5xNFVNdDFtVmpOVkkzMWcxd0VxcFBCazBvdUpaSEpIZUhCUFpaV2k2bVV0Z3VhOHNaR0d3WmpEZjNlV0xDcUZxRWYwMWZ5YXR6eGNnSjUzYjByN1lsOGJpYTBMazZ0clVUalN6VFZUYmFUdTdkSjl4WXBTK0lJbUlWS0oydlJZOWwxT2ZIYU9qR2l4K1dIeHFmZmRocnJETGlyMWhvQ3htZm5SZ1hqdDZ4c21zVFRpK2pYeCsvUytxZnozOVc4RFo3NlNCNkhMSTFRUkFMNTJpZC9ieW1YdktWODcrbmprMGVhdExFaGhWbGRacXlJeGZkckZhb2svbHlxbmNqL1JJakpBMHJwNkc1U2wrZkp3VlpUYUJoVlNZelpUSjBLZ2I5VUNjL2hUL2x3NGdkNEFwNEsveDFTU21TdGl4N0NibUVHTXdFODJ2Z2wrRUdJNWhKS3hFVTk4VU5IS3dYZ3FySmF4V2ZneWFGZWhaNzNxSSt0SElSWTNzU3RVY0dpTExtZUJVZFZRN2x4azFRNXZJdUVLL3RQRVY3bDk5UzMxaS9jTUlYT2tVeTdjalZ4OEF2UmcrZUVTNDcwTTFrQ1EvTnZkR3VYWDZyZHl5YTI4cEJsa3JLeW5GYUUrTVdvR1JneE5qT0QyR3JTcVlVeFBYUW5pSlFJemJmRkx4RzMxMnBUQ2IraWI2Z2ZWN2JwdEFnV2I1c3YrOTR4OGE3ZWdRamFhN0U0QjBuMHNBYkZTY3poOXIyOEhZd1ZhRnJEbDByVlR5Y0x4RWZQU2ZodVV4aFhvd3BxTE1wOGlCZ2NoQ2lpUTRIVHRIWXB4TVNrTGhqQjdkUE1rWE45N2hQbjMrTDRFOGZLbWZzMERqLzVLckZZQmVqbUQ1SjJvY1RNSlNjV1QzbS9TV21kZTdRNU5MMUtBYlpVV09xQkVqUFJHdGdRc0ZuTTZWYzRVbk0xanhGUlRQQWZVT295TUVLTTR2V0Fsc0dIckdjL0ltRTVnd25oVGFNeDZzaVExZ0VxL2Rvci9ZRGlPaU1iL3EydVV0ZFFoUXhnN0dxb3ljc0ZINktRTmxmSTd6ZmRTWkNJbDR5cWNpZnZjYVhzdE9XWkdsRFBiMmxLbkU5L1hscnBZTXgxU1NVaXJ5K1BhNTVKR05kNDMvNC9TN2dCVU04SnFyVCt0MTVlb0dZSlJPa0RLQS9lWHJsMzlaRDArL2dlVnNuOHNFTnJUU3NRTkg0cU5rQzdsVHp1ZkNtUnd1MUVwZWU2Q2xSckFtZExsRkNTbVFDcC80cmxXcFE2UnNOR2hUOGF5ZHhQcXNXUnhPRkprMmJTUUx6a253TWR2bTc3cXh5MzVncHlGTUVPdDhCRTYxMmJjcktJTkVtTy9CVW9iTUovNGNSazZwdFphZUZYWlpxMldGZVh6N3VINTU4OTNwUjg1OElJY25ybFp6KzJ4eWJRRFFpMC9aL0l3SDRnVHNHZCszL3hmZFRmM1htcm5rVnBsTnFZM0FtcXNaT3o4UXVTZCtzYzNJS1d1VmNyNFFWa3BoN0pReWpNSzFockNVdS9YN0luVUxXbFBxZ1JWdWg4bEhqcGpDYWNMbUpoS09lV3dSRHh3aTJLSnZxTjY4MTg2dlBYUE9uOE13VlpsUFJYZG5NSlVxV1VoQ0YrcXcxREswMW1UV21QTVY3dXpvVVhsdzY2K3JUNTU1TDNDK0E3eXIwdHcrbTF4TEFJeXlBNGhBbXQwMitFbDNjUGg2RGsyL3loMGM5alF4c0ZuWFdqa1ZYMSsxQkt0RnBiQlpLWnVWc0Y3RGVnMmJsUit1R1NOaXBkVjYwcWI3ZGhBVzhQbmVvRDBqV3R1UDNjVmFjZVFlMG9JNCtvV0o5V1orTW9HNUZHWlNHRnB2Z2l0VktWQzFUdWtaa1Y1aWJhWHc5RmJOaWZ3VDh2WHhlOHJQclB3Yk1PcVkybXNHZUZHdVJRQkdFWTVnb21rR3lNZ091M3ZtanVqQi90M3M2ZDN1bHZ2UUF3cEJ4czY1VWUwYjF5eUNSWnJBd08rNTlYTlFOaXNmeUd3NlA0d3hKck9iK3E2QWhvSHNrV2ZZUkxjU2FOM0JoQnFCMUhjdGtSbm9XKzliVGxnWUJsOHo3UUM3VkFWWEM2ajB4V3BtakFEbWJJV2VMaDR4ajIzOXUzejR3dnNLaWtmREZiam1OTjZsY2kwRE1Jb0g0aEhZTVp0dUtiM1ozRHI3YW5ObzhuYTMyOXlsUy8xTWQ2VklBVnFxa3RmT3I2WkhVREhleHpQU2FEMG5Qa2lwMVFjQ1ZlMERobGpoTU1UWktzSGdTb3k2L2U1ZXE1MEFLT1FwYlNqYnhVSGZxQ08yenlYWXVCSlhjdlVSL2VuaWErYjQrTUg2MVBnZk9McithZndvZUZBTTl5SFhNdkNpUEI4QTJCWERIWmhJZE9qSUM4d3JGKzkwTjA3ZXpyVCtzSFhtUmJKL1FEMlRvbFVvWUZRQzJ3ck9WU2hPd2pocWFKcDhZdFRod2RSTncvZzBYbVJCeDJHUFhkNU4yTUVsaWxYQkJyQ2w0dmRNRnc1ZEtXRnp2TVZLOVFCZkt4N1NCMGNmb054NkF0aUtmemFVenFJeGYxN0k4dzJBVVlTM0lYd0N3eWY4RUlyT1k1TVpMTXNkTTYvS1o1SWI1ZURrQzVOZWNwdGFNOUQ1TkdFK3hWampCNE5XRVQ4K3NLVjJubGVuSVdrY0dRMkNxQW5wYVN2TmZrRWhhTURBcGhiblV6KzZsaU1YcTBKeTl6Z1g4eSs0RThWRnMxcmVYMzkrL1Jqd1RPZGRlTkR0UnA4UDJ1N1o1UGtLd0V2RmNBUmg5bVdHUDMrNGZKYVBjUkd3eWQyTE41RFhyOUM1WklvRGd4bFNlNk00YmxEUkhwbEpOTU9xc1FZamlmUWtpK09LcEZTOE9hV2lxQ3NaQTZWek90WVMwWFdEZVlxOGZvSm44ck95V3EycnM1K3RIemp6NlBXdytpU01tN1B3UlJmaFRtelFkREVrZXQ3Szl3b0F1K0p6SW5kZ2VOSExoSGMvWEdFYVkzbXBHR0FLc0VNd201RDI5OHhrOWZjbDg1S3hweGJOc0JhYlMxMlBheWZxemxlbml0WGVoVExQODN3TUZPRm5rMHVCRksvODc1QndDdUVpamc4MUNaL3ZHZmxmQnU4ckk3RTgvS0VBQUFBQVNVVk9SSzVDWUlJPSIgYWx0PSJXaGF0c0FwcCIgY2xhc3M9Im9wdC1pY29uLWltZyI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJvcHQtdGV4dCI+PGRpdiBjbGFzcz0ib3B0LXRpdGxlIj5XaGF0c0FwcDwvZGl2PjxkaXYgY2xhc3M9Im9wdC1oYW5kbGUiPiszNzIgNTg3IDM1NDU2PC9kaXY+PC9kaXY+CiAgICA8c3BhbiBjbGFzcz0ib3B0LWFycm93Ij7ihpI8L3NwYW4+CiAgPC9hPgogIDxhIGhyZWY9Imh0dHBzOi8vd3d3LmZhY2Vib29rLmNvbS9zaGFyZS8xRUxQNktDNnJWLz9taWJleHRpZD13d1hJZnIiIHRhcmdldD0iX2JsYW5rIiBjbGFzcz0ib3B0Ij4KICAgIDxkaXYgY2xhc3M9Im9wdC1pY29uIj48aW1nIHNyYz0iZGF0YTppbWFnZS9wbmc7YmFzZTY0LGlWQk9SdzBLR2dvQUFBQU5TVWhFVWdBQUFLQUFBQUNnQ0FZQUFBQ0x6MmN0QUFCMm9FbEVRVlI0bk8yOWFhQmwxMUVlK3RYYSs1eHpoNzQ5RDJyTnR1UkpNalpZSmdZVFd6STJKQXdHWTl5Q2dCbVNBSThrRUVKNGp6d2djYXRKSUlFUUV2TEM4RWdlU2NEZ29EYUU4UVZpWHJCTXdBR1AyRmp5cE5Zc3RYcnV2c001WisrOTZucy9xbXJ0ZmRxU0xCdkpsc0RidnVwN3o3RDNXclZxVlgzMVZhMjFnTTljbjdrK2MzM20rc3oxbWVzejEyZXV6MXlmdVQ1emZlYjZ6UFdaNnpQWFo2N1BYSitDU3o3ZERYajZYQlRnRmdGdUYrQ0VBRGNCdUo0NDlDZ2ZQK3IvSGpycXYxOUhlK0VXQXNJbnU3VlBsK3N6Q3Jod1VZQ2JFMjY4VHJEL2V1SzZROFF0SUNRUmVBSjFoaFRjQXNIdFJ3VW5QaUM0N1hhYWdoN1JKKzRoVDQvckw3a0NIazY0RVFrMzNhS21hSTl1bWZaZGQyaGJVMTJ6Zi9Qc3cxZFF1elZnRlJpTlI5VTRJOHVFRlhQSzFRUkl5d25jNmpEZDBrb1NhMndRelZiR2FQVGdhRktkMzNuODduUDNuTC90M0NNK1JBUjRneWE4OVpaa1NuazBQMWs5ZjZwY2Y4a1VjS0J3UDFncHVHaHdEaDY4Y2UrWit2SVg2ZExhYnRsMTdaV1NKamQxYVh5SnByVmFxdVZWcVVhN0tOVXUxaXNBS2xNWU52WTdDRWdDVW9LUUlBRkJoaUFEWFF1aW02VmF0dGpOTHFEWnZKQTRPMXRYK0JPZXUrZTlQSGYvYVV6NG9mbUgvdlBkaSswVjRMQW1ITGtsQWRDL2lCYnlMNzRDSGpwVUFZZUFOMzlOQm5zREp3RHFhNy9oYy9MeXBTK3Nkenp6SmdBdjByVXJkbk84N1RKeUJGUmp5R2dDUW9DdUE2aEFFa2h1TTNKV0ZRR3FwRkFGU1JFa1JZSUFDdnRTQlJFVmlBQ1NDTTBqUVJJbUFkSVlvZ2tRZ0hrSzZXYVFibm9LMHdkTzF1M20zWncrL0J2NTdFZnY2TzUrMHg4QzZQcEdDL0M2WDY1dzlDaitvbGpIdjZBSzZKYnViVC9ZRFpWdStmS3YvOXgyNTU0YjB1cVZyOGpqSForTjFTdXU1R1RmRXNjcmtDcEIyQUhJTGJXbHRqbWg3UVR6REdRVlpCQ0NCTGdSRWdvZ0NsV0RoeUlFS0VnSlFJSzltR2hXa2dKSkNtaUNVQ0VVMURVeEhqRlZGVVdRaUxxdXBBYlpnZWdnMDRkVk5vNGZ5MXZyNzhYNVkyK3B1bzEzdFBmODNKOGlHaUFDdlB3Tk5XNTdlbHZHdjBnS0tNQ2hoTU8zRWtkRUFlQkdvUDdqNTMzWFM5dnQxMzQ1SjN1K2dpdjdub21sSFNQSUJCaE5rTmlBbWx2dFdzRzhFOHl5b0dXQ0NqR3VzVlFuMmI1OXhFdjJMc21lWFNPc3JOVGN2bk9FMWVXYVMxV1Nxa3Fva3dnQlZhV29DbElGdEMzUktEQnRNcWFOY25PcncvcUZEbm5leW5UZTh2U1p1Wnc5MjhpMHpkU2NBWVdnVHNSeXloZ2xKQkVpalNwVW82UnRBOHkzZ0c1akx1M1pCOUxXMmQrb3o5engyL083L3QxYkVkYnhNQk9PM0N4UFI2djRkRmRBQVFuY2RFdUZ0LzNUTGpEZHl0VTN2ekFmZVBGcjgvSmxYOE50QjUrdHF3ZUVXU0NjVVVRNnpWa3dhd1JUVGNoSjBraTRZOGNFenppNElwZnZXK2JlQXlzNHVHZUNiYXNWUnBOSzZxb0dGT2l5b3MxRXprQlc4NzRLa0tTUW9FRE1DUk9hZ0FRUUJGZ2xRRVdZQkFJUW1vSDVyTVBXUE12eE13MGZQajZWRXljMmNmenNGdGN2ek1HY2dRVEY4Z2dZMTB4U2tlQ0lHQUdzSWJQalNOTmpIMHJyRHgxTkQ3M2o1K1lQLytaZEFNelMzbnh6d3RHamlpYzBiSC95cnFlekFncHVQRnpodGlNZEFOd0FqTjUvMWRlOU9sMzI0bS9vZGozN2kvUHExU3VtQXpOTllKdWJ0c1pXSjJoVlJxTWFCL2F0NHJsWGJaY3JMbC9sNWZ1WHNYMzdXT29LQklYenRwT3RPYVR0RkUxTHpEdEFsUlFSaXkvb2lrUlNraENFS0VsUkNFVkFRa2hTU2FnSUlDUXlSYXpWRU5pZEp1TWtkVjFoTWtvZ3dLWnBjV0dqdzBNUGIrSGhCeTdnN2hOVG5EdmZDRFFEWXhLanBLbEtTdFlWUnBNRXRranI5NjFqL2RpdjFQZTk5WmZtRC8zbVcwd3lnUlZ2ZnNvcjR0TlJBUmNVRDd1djNWNWYrbVYvQTJ2UC9uYnVmZDVuNi9KQkNEc1Z0RTF1bWhHMmNrSkxycTVXOHN3cnQ4c0xucjJEejdwNnArellOaVlFTXBzck51Y1owN21pNjBnRklTbUpFa2dnV2dWYlRRSVNkR3pIVENnSktpbENxQklRRUV4aWNCQUlwVlFDeVY5ZytTOUVJQ0FKcFFCVUNJQkt5Rkdkc0RTcElIV0N0Q3BuejgxdzU3MFhjTmY5WjNINjFKUzVVV0NjZ05WUmxxcnFTRm1TTkVJOXZ5L3o3QjF2MVFmKzlHZjF2bC84RFFBelNBSmU5MStlMG9yNDlGTEFRNGNxM0hxclFvVFhBcE83UC8vSHZ6T3ZYZk1QdWUzeWc1QmxTTW81b1VIZTdBVG5zdFNUQ3RkZHN4MmZjLzEyZk5hejFqaGFtc2hzbnJFK1ZkbWFFVzJuVUFnb0JCVWtraWdWQUprSjVFekpGR1pYTEZXQXpEQXJTSmo2QVlBcEdnaXBCS0JZV0tJcVVDVXQ4U0VRQzRqRW94Yi9zVHNrRVpCS0pRR2xxTWN6eStQRThhUVdTY0Q2ZXFQMzNYc0I5OXgxUVU2ZDNiQnZMZ21rVGhtVEZXRTFxckIxRHVuY2ZSOFlyYi96eCtidi83SC9BbUJtZ2RBYjBsTXhXSG1hS09EaGhFTzNDSTVLQmpDZXZQRDcvbVozOENWL1I3Yzk0NFdRQkdIWFFqVHA1aXpoQW1UM25oVzg3RVY3OGJrdjNNbTllOGJjMnNweStrTEhyYmxDRlVtUnFCUmtwV1FsbE1xT2dLcUl1bklwQlVwS3BvQVVaQklFd1F5cXk0MXU0dWpLQXlKVktZRlFna25zT3dCSVdxUWhBTXlQbXhwNnRFeXptZ0lsU1loQVJKS3JybG5jcWhLTVI4QmtVaE1DMlZ4djVlNlBudVdkZDUyVDJhd2gxaExTYUp5WlJrUktvMHEzZ0FzZit0UDYvbmYrMk96RC8rNi9BT2h3Nk5hbm5EVjg2aXZnallmcmNMZkwxNzcrMWQxVmYrME51dSt6WHB5eGpKU25uYUJKZVVyZ1hNYlZsNjJrVjczc0FELzd1bDJRbEhENlFpY2JXeTFiQmFFUXMycktOaWRwVlNUVFhHbFdNSU9pR2Nna3pkS0pFRVJtZ3BKUXR4Mkc3U0N1cHlCTlp3aW9DRkpsY1pFQWRpOVhOVUtOcHdiTWNpYVNTQ0pVaVFTTUthbFpTWXFyb1N1bjJXQUJxSVFJT0ttck5CbFhtRGVLKys0OXgyUDNuOFBHK1Rrd0VjaUlIUWpGZUcxUzZRYms0WGY4aVg3ME45K1FULzdlNzE0czAwLzM5UlJXd01NSmgyOEJqb2hPTG4vMXRmbXlsLzBRTC92OG0vUHk1WkRtYkpORXE3eWhDWFBsczY5ZXhaZC93VUc1N2puYlpUNG5IajQ3NTFaclNGOFZhTHFNdGlNNkZlU3NhSm1reldMS2x3a0ZtR2swQ3BWVWdlUk0wSXdQVkpLUU1LeG5RYThvMWJUTzlBOFVFZ3BKSWhJT1Zoa0dMWFNTSWtLakJha1FKSVJURnFYL3grTVVDQ0VpVUNJbElJVWJCMUJCcVlBd0Mrb2FtRXdxTWhISEg5ckFuUjg5S3h2bnRoUkxvRlFBTWFKTUpuWGF1aDg0OFo2ZlhuclBHMjdaQkU0WWRZTlBlMkhFVTFFQkJUZDhXNDEzL1d3TG9LcGY5TVBmcWZ0ZTlQMjY2NXA5MEthck1KWGNvTUpETXp6ajZtMDQ5T1dYNDdPZXRRT2JXNW5IenpTWVpSRUYwV1dnNlpSTmh1Uk8yWkZzczBpblFOdEJNZ1dxUktkRUJxbit0MElFT1ZPUm1NRkVCWlFKRkZOQU4ybFFzMzdHY3dzWmxvMFVpSmpwZzFzL0ljQUVNTk0waUJGUkN3VHV3eE5nNmc0THBRMHBVanltU1dLZlRZZ29Ha2ppRWJrU2RVVXVMVlVBa2p4dzMza2MrOUJKenJ0TUxGY0NxR0s4VFdTOGtuRGlqeCtzSDNqYkQ3VEgvc04vTW5IZldnRTNmOXI0dzZlWUFsSndHSUlqb3VPcnYrSTUzVlZmK1g5eHp3dS9pTlVLRW1ZZFJDdDl1TVh1MWJGOC9WZGN5aHYveWo1Wm4yWThlTExsdEtNb3dYa0xhVHBpbnBWdHA5SjBvQ3JRVWRGMVFLZVFUaE02VlhZVVVScCt5ODZVdU9wUUZhSXd1R2Fmb1JrdW1qMGphYXdqRllCRXVHdDhDZ2p4NzhBOExDRVFqYXlNdjI0S2FNRUp4YmdiaUdOQ1dFWlBoQ0lVSkFIRmNhUVY1eVJKWmhTWlFFbEpDU1Vnd3BYbFNycU91T3ZZR1Q1MHoybmhVaVV5RnJCTEhTYmI2MW9mQm82LzQ5ZTdkL3hmM3dFOGNEOXUvUDBhdDcwaTQ5T0FEWjlDQ25pb0NpWi84dnh2K2ZiMjBsZjlxTzcrbkRXMHM2NnV1OVJ0WnNHNWpLOTYrUUc4L3FzdWhWWTE3bnB3amxtcjZDQ1l6UlZOQzVtMUdVMEc1aG5zTXFUTnlnZ21tbXpVU2FzaWJVNVFDeGRBa2xuTkFkSTFNS3NJaEZBSVZDMnNwZnRWQUVJMWZNZWlUTWdDZzNZc2NxV0hGNFJZd0MwMGcraktacTVkSktxOXdnVkhiQzFpc2JKQVJGbVpYMFlDS1FJUnRUWWxVUXFFQ1lRazArK1VLT09sTWRiUFRubnNveWZTYk5vQ1N6V2htUml0dERKT0V6bjF2dVBWbmYvOUg3WDN2ZkhuUDEyUjhsTkRBUjBVYjl0MjdiN20rZC8yRS9uQWkvOUdIdStEc08xRXRkTGpMYTY1YW9uZjlZM1BsT2MrWTAyTzNUL0RxYzBNaFZtOGFhZVl6Y21tcFRRZDBHUmlya0RYR1krblNtUXFPazJpSkxvczZGU2c2TjFxZG14SHNiOHRFTEhTQWtKRU5VU2xMTlNka1RGOXVsa012bm53NitRZnpXYzZISFFEU3lIRW1FWjZiRXgvb0x0bmY2NDlWV0VaNW9oMUJnTkhJb25kUkdpbGl5blo3OHpFZUZ3aDF5SW43ejJIaCs4N1J5WUFFd0dRR294WEoybnJQbFRIMy9GRDdmdU8vQkNBNmFmYUpYLzZGZERNZjFmdisrS1g4ck8rNFkzNTRFdWZnVzdXVlppbmZJRUo1eksrNWFzdXdldGZleW5PWEtEZWRieVJXVXRwT3NXMEpXWXRNVzBjODJWaWxnVmRaeW16TmdPdG1wS3B1VnhrUUxwV3FHSlJjS1lBekZUYThDazhwUFZvUXlrZ1VuQ0ZjRk5GR0ZVakdZSkVVMHBoa2FmWk5kTkVBaGFKcUZLU3FhZDQzUXhFclloQkFDUno4K1pmWGE5VGdrVDlBNmdxSWlLZ0VlV2hkQVlNSkd5bTBLWlJCVUFoTWhKaWFhbm0rdm9jOTMzNEpKcTJoYXlNU0dZaUxTRXRqU284OE1kdlcvcGYvL3BydC9EQmg1eXUrWlFvNGFkVEFRV0hhWGp2R2EvOWFqNzMwQnZiM1M5Wnd1eENXNDFSNVFkbmN0bmFSUDdKZDErREZ6eG5EWGNjYTNodU0yUGFLamJuV1dZTk1Hc1VzMDdSdEVDYkJXMVd6TE9nSTlCMk1KeVh6YWhrUURzRlZDVjFCRHNhQmd1TFpOWVFIbFRZdnhZaUdnM0RRR29VYzVFa1ZRUlUwQldDQXZPTTVsOGhWSlNZeE1nVTAyQnhMakE3Vml4Qkw1VzlPMGJoQndFUmljWlpRdG5nbmlkZ2tpdTQzUU1xb2drRVJVaW9wQ29CZ0tLdUt4S1FrdytlNWJuVG0wUmRBVWtGZGQya3lkb0VKOTkzNytqMlgvcVcrWW5mZk11bkNoZCtxaFN3cC96ajcwTk1PQ3A1OU1LL2V3VFhmTVViMnBWcnM3UWJCS1RtL1JmNEZYOTFQLzd1Mzd4R21BVjNQOXhpbmpNdmJDazJab3BaazJYV0FkT0dDSmZiZFlKR2dVNkJWaFZOQm5NVzZhaU02RFlycUJDYUVycXJwVmhXRjRKc3hnU0VvekV4MytaWmp2aS9FRTQ4aTBDekZtZHJ3V2t4YUNVZVFkeVVJcVdhaWw0MjZCK0VLQUpBSm44emJDMGliOGZJOHNYczZJdTR4YWVPaUZKYyt5RkpCRVJLQ1ZVaTZDbkRlaVJZUDcrRjB3K2VONDgva2d4cXh0THVjVHIvd1Z6ZjgxLy83dndqYi94Wm8ycUVlQktWOE5OaEFZdmxtN3p3QjMraWU5NnIvbjZXUFYzS0YwUm5WYXJQei9DL3YvNXlmTmtycjhCSEhtaGxZNXJSRWJpdzFYSjlwcGcyS3ROR01XdFJsSy9KaHV0YXQzNmRLdG9NWm9wa1dxMlVrY29peW9TczVucVZpTUFSbG9BTCtLOGVuRVFrR3pFbzZRS3pVV0ZSQmdhVkxHRWdKWFNtS0lkcm5HTTlBRlkzcU82TGdjb1NlZ0RVOG9OR0JCcnFjeThmQ0FBRVV1RjZDRW5HQS9uZkNtcXk3SXJkUmZ4elVib3pHZ3RtOHl4bkhqaUhybXNVb3dySUxXVjVCMUkrVmFXUC9MY2ZhZS80OGYvVGJIanA4cE9nREovYVN4eGFNNzNreU0vb0ZWLzV2N0Vlenl0TVIzbWRzbnRMK1VQZit5eDU3clU3OGY0N3Q5Qm15RFFEMDNuRzVqeGpjNmFjZGNTc3BjeGFHTWJMeER5Ykl1VU10SlppUTZ1RUt0aFp4QXA0UVlzaVNhdjA5Sm9ZMFdiRUxrQng2MlptUitGaEpoSW9SbHdudXBOMlFwbTk2c0hIRnIyZWhDMk5lTVN3V1hKajVnRzBXZGhFVk14TThUVTZwZ01nSWhJY3VCSFNsclVUenlBRFJDMEJIQUF2RWd0RzIxaHdjOWxLVUNvaFNHV1ZSQlFpWjArc285dWFLMFpJMEV4TWRuUkp1bEg2eU5GZjZHNy9rVzl5Zy9Ha1dNSlBvUUxhN0x3QlVyLzM4LzdseitXcnZ1ejEwTGF0NnE3T3B6T3UzbDdoUjcvM09rNG1JN243K0p5dFN0cWNaVXdiR3U2YlpXNjF4S3loMFN5ZFJieHp0WUpsVlNJcjJWSWtVOUJsQlNuc2l2RlJaQVhJSkIzTlVwYnVlMkpOQjY0ekZNUmZjbFZnLzd1UTl2K0FkMzFxZzlGZGhydDF0YXlJVk5FY293Q3RDcnJzMzJkbXBZYjdMWkFBYTdGNHVJS0lRbW40Z1VBdW5EWEVUVlFTQWdraWxwUDJkRE1OTWpoMzZTeW1pQ3V5Wm9WSWtpemc5UHdtMnZXcG9CWkFXaUp0YTZXZWpLc1AvVCsvMk4zeEU2L0hJVlk0S2s5NEhybCtJbS8ycU5maHc4bFhuZFh2ZnVtL2VTTXZlOVhOYU9kdHZZVFVQZFRnc3k0ZjQ0ZS81M3F1endYM1BUQ0ZRckMrMlhLenMzS3ByUWFZdFlwcEI1bDNRTk9SOHc1bzFDeWd1Vm5EZkNvSWtobFpqU29ES1pRRUpRa3FNMnZEOUJMRkF2REN2aDVmbVZlVUtFcHdYVTJScXkwNFQzdXpaMk90aHQ5U0VxU1I4U0ZOQnRnU3VxNkNyUTdZVXFCVnBKUzRuREpxRldGVWFHa0dFdEVxWlV1VDZXNENVRU13U3NCU1Fsb1NWS01LbzhvVVR6dEYxNW11cFJJaWhkY05yKzlQQUN3NnB2VUh6bDlQMWxhRVNPaTJwa0FhRTNrNm9yQ1Y1MzdkMXk5WHNqVTlLdC8yWkNqaHAwQUJEeWZjZm92Y0lGSzk5L04vK2hmeWxWOTRNMlpiWFRYUlVYZC9veSs0Y29KLzlyOC9IeWZPWlJ3LzN3Z0pUSnVjcG8xeTFncTJHbUxXQXRNR01tM0lSaUZ0Ujh6VmNydXRBcDBTVUVoSElpUEJTR1VHMFJ4MGlGWHZDWkNwVGpBak1CckNTQmlOYk5aUEZkQmtoRWQ0V3FHbHc5UUhOMWFJbUQ5V3Bnb2lJOEY4M2hGM3Q4Q0ZqRjJyU2VwbHdmTVBKbjd1ODFka2RRWGNlMGt0enp3d2ttY2RxREdwQlZVRk1JR3RXL1lMYytMQkRlTDR5WTRuanpmU2JYVTRlejdqUGZmUCtkR3prT2JNRktmUFp5b282ZEl4NnVWRVpCVW5JeU9LSVpKbis5d0N3ck05aFhjMHJSY29VQzJQa2FuZ2ZDNlFCRFJiZGF0TGpWN3p0ZCs2VkczTHM2UHlkNTdvaXBvblZ3RUp3WmU4WklTak12K3psLzdUZjhVcmIvb2F6R1p0VmVjNm4raHczY0VKRG4vUDgvVytrNjJjMjhneTdSVHp1V0xhS1pwT1pONFMwODU0dnVsY09lc1VyUW9iaFdnMm5OZkJNQi9WOHJrTjNDMFNVSkFaNGdNQXFIRVR5T3EvZXhPRkJCWEtaTGllQ2lCNStaV0czYUFycUFnOVlVY29GSXFrMUZTTGFDTGFVeDN3Y010THRnbGUvY0l4dnVDenQ4a3JQM2VGdTNiV1dGMDF0dTlqcE5SRG9RVklkTU1DSWROZkczT2kyZXI0SngrZTR0MS91b2xmK2NNTGVPOEZSYlV0TVhka0VpVGpCZ25rZ2xPQlRNQUx5OFRFWS8yTi94Q1NSalZ5em9LbXNiUktubzZ6ckRSNjlXdS9mVVJvZS9UbXYrZVc4QW5oQ1o5Y0RIakR0NDN3cnA5dGw1Ny85Ly9QOXZwdi9PZTVHeldKODVGZVVIbm16aHEzZk05bjZlbHpyWnpiN05Ca2NuT2VwZWtnczA3WnRFbG1yYmxlVytCamhRV05ncDNSTFpJejBabnhBeFdtaklWR01jRm5RMFFRUUxJYXRzdkI3VEpxUTkwQ3FzUmZneXlFcmU3dGEvaUlwTDRFQktSVUNxbUE3a1JMT1QxUE4xNjV4Ry82MGpXODVwWGJaT2UyQ2hnb0QwbHBzeVdSUllDcVNsSUd3Q3lTTXlYSjZVWjZtUTFMVkZOVnlSYmVEY2J3VGI5L2xsLzN3dytndW5wWmRLWnFkdDdDOERDSjFnQ2xFVkh3dUpvQTFaSjNGdVM0LzFib2JHNWtxcENRREtUbExvbU01TU0vODgvemgvL2o5MGNDNGMrcklrK2VCVHgwcU1MUm4yMlhybm5kTitxenYvS2ZVNWJtSXBzajNhRHNYeUsrNjF1ZmhZZk90RGh6b1VHcnhLeFZ6RnR3M2xHYXJKZzFpbm1YTU0rQUZSZEEyaXhtOWRSd1g2WVkvb09SeUFvZ1c0Qm9yNFdwRWk4bU1HSVBLcWFndlc3NHVvNFNOM3AxbE1TWWxPUUZUQmZNajZXbGhQWmNCend3bFM5NVZzVi85QjJYOE1iUDJ3YTRxcmF0Y1NkMU1xZWRVbUpkUVZBTFJTbVNTbGhqOTAyaE9TcVZKcUNLZ3VzSVo3MDRJaXNsSldsYXNFckVtUXZlZVdxUTVaNlFsa0M1WGh3Qnp5U3J4ZitNMGpERHc0V3VsVVFaajRUWkpBcEpRTGMxMG1wYk4zcjIxMzVmMVowNTBkejJpbi96Uk5RVlBqa0tlT1BoR20vK3dXNnkvMXRmMVQzM05mKytXNzZ5Uy9OekZUWXBTN09NYi8rVzZ6R2RKejV3YWtzeUJJWjdGTk9PYUxPZzZTanpERFJaamV0VFFkTVJqYXJWOUJGaTVMSjVsZXhZempDZjgzdFEwR2haLzRDVm1sZ3BYa1N6Q0o3Q0tCR3hBb1VobXdGMlJFcjlZQ0dUQ1NJVGtmYURXM2pCZHZDSGYyQS92K3hsYXdJQXVhTmtDT3RLWkZRSk5MbnFJSW15RjFIb0ZHQW1pZ0NvYW5YNFRLVlVjR0NLNmRQRHNuRUNWaFdscmdTMVpvRjJnRXk4SXNhcVdUSFFLWFZxaGxheVF4MTQ5U2dyczZveUQ3aVNBSk1SME1KbXR3alFibFR0NnI1dTlLelgvL2h5M25iUDlMWWovOVVNelNlL0hQUkpVTUREQ1cvN3dXNEh1V3Y5czI3OEdkMyszREdtcDFza3FlVnNnOWQvMC9PUTZoSHVmbWdMTFltbXkrZ3lPVmRCMHhLdEt1ZVpadTF5UXBPQlZva21Lem9sWFFHcHJvUlora3hGbG5DOTlyZUtGWmRDblZRalJmczBtNWZqU2RBVHpnWksrRmVqem93TFJna2x4MVlCazkrOWlXLy93aFg4eUQvWWkrMnJOYnJPcWxGVDdhU3ZPWE1SdHpCTzBKZ1JWWTFpWi9pZHpTVWpDUlJFVW1Za1ZQNTZLSklDZ0lDSkMvVUlUS3EyY2s3akdmUnlCNk1kZStJYkFET2pMTWU3NlhlTkNrZnZyMmQyVUZlQ1ZnanRnS29TVE05S3QvUXN5alZmOG5Qak0rLzZRUFBtb3g4R0RuL1NWVFRwNDMva0U3b0VOeUpkUlM1dHZmU2YvQ29QZnU0MU1qdmJwbFNOOU5STXYvVFZWMlB2M2lYYytjQUdwaTFsczFGdXpSV2JEYkExcDJ5MU5NcWxBUVAvemJQeGZtMUdsTkxEZUR4QlN4Z0dKTkRSSnI5WlJxSXpxcXhQdndra3c3bGlLOXVUSVdkbkVLZzNBdHFud3NLOElsVVF0b3JKbjI3SkcvL0JYdm5wSDdnRTI1Y3JhVHRGWFNjMTlpNjREeE9IVlJtWUVlbU5UZ3JVMVd0UlJPQUpVQ1NLYXJ6dHM4Y3ErdE1BMDhVOTFCRWNHSFlUOE9vSGowQkNYL3RFQ1lJdHR4U1FUUmNYZ2swOUdGWkJSYVFLdHIwSWlaUVNwNmZaN25qQlRuM08zL3hWY0cwUERsM3Z2ZnpFcnlkV0FYMjU1UEViL3U0YmVPV3JiOEowY3lZVkt6MDN4WXVmdjB1dWZjWWE3bnhnRS9PT1dKOWxiczFWdGhxUnJZYVlOY3BaQjVsMWtIa1dtWFhBTEx2cnpVU3JJcTBWRWFDbElsdCtGNWxFNTFtTmpvSXM5bnRHRkpzS0ZUQnFCa2dhOUVOWVNzQXhIdHpjOUxDc2FCOEJxY3dsTDMxMGpsLy9vUVA0K2xkdjUzVGFvYU5vVllOWk5TR3BSVEMrUEZqanpxUWc5VEdQMlZYenJORUd4NXRSM3ByaTdTTGJvb3A5Ni93ekVtUTVRcVZwT1E5MzJOSEYrRDc3dmc4VTAxTi9BT0FZeHVzZ2ttMzNVRldDVkFtUWdGUUxwK3NOTDczcCtzbXp2K1pmV2VYTTRVOUtsNTVBRjN5b3dtMUh1dXJhYjNsZGM5bHIvdzl5dFVsNllhU2JIZmF1amZtaXo5N1BleC9ZRkdWQzE5a2FuVmFWblpKdEJsb0NUU2E3TE5JUmZWNlhUcHZRYVJFSGNocGNYcDlJRzB4b21BODBiMm9rbmdFc3ErTWtlLzNxblRFRDZTUHNZL1owV0xMQXN2ckFPbjc5Unk3REY3OWtoZk5XWlhtNUpzbWtDbFlSbWdxSzRpU3ZGSWpTS1E4SG9wQ2hENEJoalU0WWdEMi95TjdpZVYwc2toZlJSSVFsRUlIR0NydUNMbDNaSUwwZU00b2pmTlpsMkZxQm1HZ0RWdzFIaVdVTklEeWFjeG5sK1VnNzZmSXp2dXFiNnE1N2UzZnN5UC85eVpSeFBWRVdNSUczNnVxQkYrekhWYS82S2E1Y1hWZk5lZ1VWcVZyaUpTODV5RFByYzVuNkl2Q3ROc1BTYkpuVHhrcXN0dWFVYVFQTU8zTFdLZWFkdWQ3T0N3NWF6KzltVldZbHN3bzZkYkpZNlN2WEdKd1drR2tFS3hYMDhtV1NEQmRkbUFlVHJRV1A5SW9SallFekcxTFZndlNCTGJ6NUh4L0FGNzlraFUwSFRLb1VWb2hWU2w3S1JTbEN0Y0JHN0RVZHJLb0RrNHU5RExkYm8wajdreDRYV0hrZkJaR1I5bXVvWXJDbEp1Vk9JaGJQRitLeTRJeFl5SUtpV2ZUS3g4amFoUklLdkJnaTd1b29OVmtJNUJVY3dtYVd1cVZMVkM1NytiL2VQdm1jYS9IbXIvbUVMZUVUb1lDQ1E0Y0VJdEpjL3RmK2d4NzRuSDB5TzltaUZ0R05Gczk1M2w1TUpsVTZmNkZsbzhTOHpaaTFpbGxIdzNrZFpONFM4NDQ2NzJ3YmpLWVROQ3BlYkFCenR5UXprM1Nha0FGa2VBRUNBWldFS0F5ZzVkNzY2aGFOSklENHdrWVVBeER1MVhMMXRNTGlDQWtKZ0lLMFhLSDc4Q2IreFRkdngxZThhanZtTFdWY1BTSi9ha3Zlak9oQmdESXJidmJjQ1FDTHo5SG55dHdEcWhVVE1rWWxKUnZyVU9wdzVWN2E1N01ubWZ5TE9aZFFGbnQ5b0pjRForNll6d3puSU5idThXNzhONElSK0MxRnJPUmFiR2tVVWtxWWJXamU5ZnpsMlhPKzdQOEJXVGtlZk56ODhoT2dnSWNTamg3TmE4LzY2bStRWi83MVYzTStiUVdvOGl4angrNEpMcnQ4Rzg2Y25hSExURE12bzVwM1FOT3F6RE5sM29FejQvcWtVYy92S3J5aXhmQ2VWYmhBY25COUVTZ2dHZC9uTXFYRC8vQkN5bkNya1RJYnlDYUd1NEF3NzQ3MDYzR3JFWkVmbk9NVnp4N2p1Nzk1RDVxR0dOWGhCKzNqcXFDcWdsQnhNeXIwYllqZ1JkUXBRVklhckdjVGlxVEZNVXA5bkJ0Qk9WRFV4cGJKeGU0eWxLRnloYm9sdUtHVW92MGMvTjczRFQ0RkU0TFlHZDdKK1VZQWd1UWVIc251TDBrY0U4WUhBYkRTcG12elpUZTlmUHlNMS85OWM4R0hIcmRlL1hrVlVJRHJ1QjNZUGIvc2xmK2lyUzlYTkp1SmtpQk5sbWRkdlF0YjA1YlRocm81Vjg2YkROdndSekhQNUx4UnpOdU1wbFcwcW1DbXFMSEhVRlhESHdxSUVqbVRxcGxxWmsvSUREQmI0V1hPNGJOTUk5eVZsR2kwVkpUYUNuVVdWK1NvVWRWWHBQdFlhVWRoUjlVT2sxTnovdVIzNzJPQ3NLNXRkRVU4cUxVVlFvS1VVS1hrMEZHQ3E0YUlpTnMrS21sY3NFZzQySXRFU1FDSncyQzVWT0t6VUhwbWwzdnl4TjRHdlp6Yk1VZ1ArUUpJZUdFTW8wRFJhczJjZFNudkk0SXduNW1SSUk5cFg4SmtkWXZwUDNsVzU3UlR1ZjhMZm1qYitQblBCVy9WeCt1Sy8zd0tlT1BoQ2ppaTB4ZDh4L2QyK3ovL0VrNHZxTlJWNHJUQkpaZHN3OUpTSlp1YnJUU2RZdDRxWnI2R28rbVU4MVpsN3J0UHRVcTBHZEtwc2xObG01V2RXOEdPUktmcWVtaUtHWnYrMElPVEh1NllFaFU4VFNYVmFsbHNqNHN5T0VhNENNS05vWVF3cnIvVkpJSDNOUGllcjl3dXo3dDZTZG9PU0VtaVRpR0pLeFNBV0FzU0x0anQ4aUNVVUt0MlRoeFlPYms0Snl3V2gwQ1J4TG9oaVBWSnJob1JheFc3Ymwrc0tpd2FzcGg0UWxQSzhtSDJxN0JTQ2JvV204RkY3UzczMVlqS3ZjeFZJc0ttV2NYWk92T0J2N0k4Zjk0WC9VdUkwRjN4eDczK0hBcDRPT0cyVy9KNDcrYytPeC80Z3UvVUxtWFJScGpKVVYzSndRUGJ1TEhac2FONGhZZWwwd3puaVZqRUM3WUVXb1YwQ20xcGxTMVpyZVN0MDJBRWJQNWxXNG1JNFE4TGhqTngwWXN0TFhIUmV6TDY0b3RpSHJTNHA3aGsrRWFldDdLN3l2anViOWdORXFoUzNBbERBOVR6SCtvVzBRR1ZRMGdUc3UrYXFpQVNrdjJ0QTIzeVpvZ0hLS29wV0J0N1RvbENFY0Z5Y2MwQVVDVkVCV0M0Wmpkbmp0V2NkaHIyRW1DSzFwWnVEWG1wQ0llaWFpT2VHWUczQVZ3VXU1eFlhVE52OWNDTlh6N2U5NHJYdWl1dUhsMS9YRFlmN3dPUGVoMEdBS0UrNnpYL2pEdWZ2NEoyazZoU3dyekQvbjNiQUlYTU8wWGJaV2s2b3MyVU5pc2JUN0cxQ3JSS0dWZzZ5Um5pTlh5aVNvbGdnd3dQWXNVcFpzeDZnVVlvVWJwVWZKZ0VXdEtDdTBPY1pZWVBOTmgrcEJxTDhNRUczM2pqQ3ZidXJKblZnb0w0alBxdUdRaVhUMUtoMGk4N2dsRkFhcnllQWtCU1FJVHFRUWk4L2pEYTRlR3pXYjdrazhVWW9iN2NNS0dzcGl0eEIrREw0NnJlOVpZWXF4VGxMLzZJV0FiSUVvS2hiaVlGR1pwQTZURWdZRE1oK1R5V0ZQdGY5d3JlYmlhZFhFcTUvQXVQQUZqRDRWdUxYMyswNjVOVHdFT0hLaHk1aFV1WHZ1S2x1dXVHMXpIbkxJS0tyV0l5U2JKang1alRlV1A3c0dSaG04bFd5YlpUdE5sL1dwVXVFMWtWdVhQWHFpUU4xUnNHekFZM1ZMTnRiNkMyZWx3SVR6MDUxZ3RyUXNLUzU3QktVaWhCbFFLaURCOUtqLzM4MzRYSWw4Z2twVkYrL1pmdU1pRWxIeENQVkNPeER3aVlQR1JGc215RmxRemFGaHFDaUZxaFZzd3Fvb1lnRjdURjRaa3FCTm5sNEJTTUV5S2hiNlJ0ejFIV3BBQkEyMUpzNlRCanBiMjdTK01JaXI4dzVHclZHZ1dIc205RWtRMmRlSFVzb3psd3NsZm5EdkJrTkNKbkFKSTRuN2JkSlRjK2Yzek4zLzRPSEJIRm9jY09TQzU2azQrcHJlVzY3bFlLaFBtcW00N290aXNGN2FZbDdET3djOWVxQlJrZFlaWlBwVlZLbTRFdUE2MVg3M1pLWmdWeXRnSFBHblNMTW52UlVBWkxaVXMvV0J6c0gxRDBwaFNNRnI4c0NJRUdqeEVmREo5SEx3TVp1Q0dpcWtDZXkvaXIxNDd4MmM5ZVJzNUdmWlRuRE9nTkZWQ2lGQ2Y1L1FTUUpNNy9HWEZ1QzhXMW1Mb29GSWg0ZENoMHA0OGtLOWwxaXE2RmV3ZEs3dFMyQjg2MndVRzJaUWp3aFhwMmxUVHhBQ3h3OEY3d2UzMEJlTlJDUklnelZBTnZzeGpPRS9hZnUxaFhDcC9VVmxtV05PKzc0WHRYVnZaZFl0c0ZQM3BBY3RFYmoyUGwwNkZERlk2SXJsenoxMjdTZlM5K0JkcW1BMWd4SzBkTE5VYVRtak9yNnpNQ09WdFpXWnNST1YxM3Y1YS96VXkrY2kyd1hnSlZTaUF4U0ZzS1N3TTVYS0xoOHZhb0xUQlErZHNqU0hIblVueFhzUG9ZS2l1a0Z1QkNoMWU5YUVYcUZLc3FNSFRhb1lrMGdHUCsyVHkwRUZEYm9zaitVN2c5RVF0QmZOMVR1UkhkUG5YWmluYnFLa2xkQ2VzcW9hNlQxQ09ncmdSVkpSalZTVVpWUWxVSjYwcXdORW1va21CdHpiZENRREozR1o2WDB2ODl3Qmd1M0I3Vk1rTGdBU3pwa1djUGI0WWxQSERPTTVMZGdYcEZralFibWJ0ZnVMTzkvRFhmQ1lDNDhkRTk3U2VlaXJ2dVZnS0M5dUFYSHRhbHl5cHNiWFNRU3BBVnkyc2pkbG10M0lrQ3BtUlZLOTZIMXV2M2ZOaDhEVFlaWlJ2Rk1CQWtWVXB1UFVCdzRhMVlYaHhjL1h2eHVWQXdSZ3RFRmd0SitwdkhJR1FCb0NwLzdmUFg0bjNLZ2lrcGcxVHNRUUtvSGh3TUlwOGV1TU9tekFMdDVwclpkY1l0cGxvQVFJN2RQOGZXdVU1T2JTalBid0xOUE50V0k2MlNoS1JLVWRXVkpCSVZFM2JzRVB6cGgyZkE4c2dXS29sNmYwcWIzY3FsbU0zV1lsRUJrNmZsMlBmUENHa3BmVTBVWkM4V2pLREZWMDBWc1VQc25oSEw1SzRtVnlpN252djNzSHpGVCtLMld4NEM4SWdWTTUrWUFoNjZ0Y0lSMGNsenZ2S21ic2YxTDJmYmRDQXJJQ05WQ1hVdHlLMnQ5NGRBdE10SkpkRlhRVUxWZGhvd0VsbHNLNHFrVUNhRmJlYm51elZHUWJtamY0MG9sc2JDRzQ5bHMwNVRNV2hndnNnRnFTVTROTUtNNHJzSENvMHlNQ0lDYm5ZNHNBMTg1aFZqQUJSWktEK09KYkttYUZFeXJkYk13RzBZR0dxeHdDT01rRW5HaTV3RnFoelZTUjU4Y002ZitaWFQ4cTczWHVEL09OWmkxa0ZRd1hLTlFYbmtYRzRBZ2RXZUpRR1FnVWtsT0xBRTdhTGkwSFBiTElKeHZLeHdwUXNRRFBqR0NvaHQ0a0pwSTBXbnZVbUEwRS9YaVVvSG1LVVJONzZxUklhTjJXeERzZWR6ZDB5dWV1MTN6VDhvL3dpSGJrM2xBTWRQV2dHdk8wUUExRjB2L2k1ZHVUUmh2dEVoVlRVMG8xNGFSNHhRU3Vpc0cwWmJVWVNaWUtZV002QVZEQ083U053Z3VnMnp4ZUFMQURBb2haaDFzVCtGeVN6STBVR1dRTVI1c0pLWndMRFVlV0NzSUZhcW1iY29uM3ZaRXZhdDFXd3pVVmNMZmljc3NvUStpeHRWVlVXS0VtY3pxRVZCVTRHUGZpL3ZYUVBJai83a2cveTNiejZQa3dDeHF3YXVxZ1VwdWFmMlVGdkVsS2VzT2ZLV1JCemhXN3dXSkNlTW9vR0wzR3Q1TFhvU2ZUZmxLN0lFK3NOMkNJaHRhZE9IUWdPUVdRaDFkOFhsYzFteVZxejNYUDFOYTJ0clA3cCs5R3RPOXczb3IwOGdDajZjY0VSMDIxVXZmNTd1ZnU2WHNOTU9rR1JZSzdGS0ZidE9OUlBzYk1XYWRpcTBzbm1SVFBQTVZyRnN1d3pRQXd6R3pyUUwza0RSWnl0Q0ZvVVNZQm5wSVlvWGlCWFRlM2lzUldnK1V1cnBwY2lRRkdrRHRHMlpNUU4yckZZeExQZ1lyTzMvS1dncUNoQlNHdENScHBoUVlka1RFSzczNWdad2ZsM2x5NzdqWHZ5VFc4L2o1TlVyVWwyN2ltcEhiUnhQUzNBTzZKelFPVVJuL204TDBUbWhVMEpuQ3AwRGJFaTI4TVJiekJYeEFDdTZGK011MXF1RlVJcnd3SVI5dlpoMzFhcDJmQkJrQ0NrV2xHZ0lERXRIcFJKMHMxYTNYWCtnMi92bGZ3T2dKeTRXcjhldmdNNXN6dys4NHUvb3l0VVRkRnZtMkZTUktxUFZZOVB2VGxWeTFwU3BVS3VLSTBteGJWVENGRWtwL3ZTNUhFUnVlQ3N1OWcxUkxtcFRtVUNrTkYzUWJuYWR2SWhadnpEblF1YnUxbUtRd2lxSUNMck1mVmVNWWdRb2dvL0JMU1pqTVY5cUJ5VkJvUks1ZXpOM0ZDZS9mV1RocS9FRUhZRFgvTU43OFhzZlZJeXYzeWJTS3ZMVUlsd1BHZ1FwaVZja09KOHMvVStDMlZWeGkyaXd3MW5ub2JXS1FDS1VMNGc4OFdKVFNjN2pwV0l4aTV4WXZtN3lpZ0xaaGRuWHk1ZnhlYmN3aENCM1ZaWVY1TjB2L0JiZ3VqSGVlc3ZIbEdvOVhnVVVIRDJrQU5hNnBhdGZ3MXdUVktQc2JYRTlxSms1NklKTUVoa2V6bm9WbEVKc00xelhJMSs1eXdIZUlIMkpwSGNncktBdjl1NEpNMyt0NU9PQ21tV1V3N2htUi80U2czSWpBSERPcDFnL1F6a0tBRTBuTDcxbVpNUlpiS2tTbndwazE2dDhTYjRCVnFkbnpsa1ovaXlKME91amtEdWlyb1EvL2dzUDQ2MS90bzdSdFRXYUM2MVBxWUg1S1FQc2xqekNjT1BlQmhPQ0xJV2t4aEdFeStqZFNNbWJtR0E5ckpmeWQ1SGRVRGU4dEtqSUxmZjlwN0p2SUVwR0p4NEhpSEZFbWlGa1l0ZTBXTHZtaGVQTHJ2MGlVOUFiRjJEZjQxUEFRNGNTSUZ4OTNxdS9wTnAxK1JWb3A1MFJuNG53QWdrdmg2Y1hCWWp4eGtKVkpsVW00NFBOQlZtaUxBSU5GbjJNSkZvNVJaVXNmWWR2T1ZHRWlZRWN5dVRWeGI4bEJzMW5wKytXNzY0bzFqNWdjQmNBNU1FZGxRT2FBYU1ZeVNtZ0VDa0RsZ2NSRkptcTlLVmZZWTJ6QXFPeDRMNFREWDc0VFdkUlhidk1icHBSckJ3R1ArRVN5MnRseDVqZTRsRThQenNVZ21seUFRcmlHRnFrZkxQSUVoeFEzUEdzb1V4THo4eHFxdFdHOVpNeThzMkJGZEdMTVBRYkFQSWN1blFRc3VlRlh3ZUFPUHpXQlkveStCVHd1bHNKSU9VZHovMWJPdGxQNU1aTk9XeU9zQkRuOUwzMDNFRFphMmJZMUExWUdMb3lXUzhpSDhWd1dqLzJmZDhXckpoZkhFUWlaU01YRGZjcWJoMWNDZEgvWHJTMHQ3NWhjU2UxNGN6VWk3VFhrWGlzSzJId2wwZ0dMdWtvSVQ0YVZjeFVNNmh2L3YvT3k0V09LdU9xUndvY0tFN2Y3WUhQRGZlWG9tcHdVSnlmSE5PNHRhY3RPblVsODZnZ3FCa01jUndnMnZlbzNERThDVUxXamlxR3crSHRMY3NiaHVNaGlBSVBuMFlwazlUbGcxKzBpZ1A3L1NESmNxZkhvNEFKUjBTWGxuQjVYcjMyWmRyTTRBQUNVQzFDOGdPR2JMbWtyUUFITE1pZ3NteDhGbGJCL3VzbTNTbk4zc0xaZHlObHhxSW5mVldTU1RQQWRFUXU3RzllSENZNFNKNE90R2lvZ3dIYlhJQlZWY1FpdWlqZUl2OGVtMU9TYWFIVjE1UXhBeWhpU213UnR3RFEzM2o3T21YSHlGekhRaVZPcit0OXl3WjU3VDZxeFZBU3JpUUNWTVBaU3FEUVQraXpQYjNoOG94SDRaWDZqRE5jcVJrZGxTSllCY0FVV3RkVFlVUExDWWhYTElKUUkybTZhZWJhTS9mbEs3NzRTK3ltZlhydTR5dmdvVnV0bTgvNDVsZDEyNTY5Z21adVpCczdBU3dwSDdWNEduVjJMaWc3amlpTFJQbFVTYzByUUFNdkJoV1ZpT1dEUTl4bkRpL3dXenpJWGFuMHMrL2k3THdDWld1RXFHWlMwT3ZtZ3lOaWNkTUxablhnbDhRMlpvczd4eEM2T2FDSVJBamNVOXcrSDkzOHVMVVhwZ1MwV2VYOGVTVW5GWXl6MU1HZC9mY2dwQVNJcGZMOUZIQVZEeGxMdEw4UDkxMGpVaEZnSVpWak1xc011aEZHUUFiSU50b1VnOWkzVVR4NktTdnRRbm5Ga1lJL3d5YVB1MElJdXN4Y2JXUGV0dnQxQUloRGg0cDZmWHdGUFBFQkFRUzZjdkJMVWU4RTJMbEZFclh6QW9UMDNIay91N3hubmhJbElWRHhvdUZlbGhnQ2Fnc3VYSk85WTlHZjNyNmxudGVLdmdMRkZaVEpHZ01ES1pZenFic3FzRS9xdWRCajhhNS9OcFV6QjN1TE9iQ2RoZXdZZ3BuWXdzOCtXellhdFBJYldqQnl6NWxXN3J3d0Y0eXNsbUx3N2VpdkZQZFc2S2ZvZjFnNkxDWm1DSlN0OGMxZDk4MlVBUmFJendlcDNBdk9sZjdpRHlLZVovZGxnSzFoY01MQ2VMbkpqeG5ZRDZBcFJxSm15UFpuZmQ0S2NOQVhMdG5jeFdOZmd0dU9kRHZBblZ5NzhxVnNad0FMTkxMcXpIQng2dlhFYW9ZdlhKRjNSeWc2MktNa0FCMHZHbUVKeklZaXBOTG5VQlo0TVdYQk5iM0pEWmRRckloOWVPQWVrdWM1VWZSSWlreWx5SHF3WWcxOUh3WXZNWVpQL0xoTVpQWFpaV0FDdGpLU0hIN3g1Tm5NQzF1MnlwRkJCUVhQSFpPc3QrVEQrcnkrTVVYcFFueURCM2hzRWR4WHdZZmhodTFmajVHS1AvYlpHQWxqYjBDNWQ1OU1oOCt0UXVVTXhHYjNWcFFKanJJV2hnUVQyazd6OHVWN2VjVVh2ZGdhYTI3NDR5aWdmV2o2bksvN0lsMjU0aUJ5bXdGV2xzbEYzMWFyL0hDN1l1Rlo2QTJqM0VuQjRsNkxNeHVNNXREVVExQmNVaHE0aGpnWlVDTUNnMG1XQTBWbUNNckJkd2x4d3ExSTcyNEdZejFRVXEraE1qdXVCWFNWRy9pWW1IVVQyM1VWeWRaYmtJTXRiY1hTSk1XazU0WVdUWlppZ1dIVFVLWjE2Y3N3TTBaL3IxaHVseEZERW03eDdFZ2x0d0MrZUhyUWNwZFI5TkNlRmZsUitzUm5sTVFVL1F5cjZSL1NnYnlLbmczNzAzL2Z1Vi9SQmhqdFpiZDYzWmNEQUc2ODduRllRUDlRM24zTkYzRnlDYUdOV3QxQVNDRm9qNkJGQm02ajM0bkpyWmtPMGpZSytKSkpWMG9PWUFmN1hHT3hyc2JLQWJCa1h4RlcvN3hRMmdqNlFnbXBJUnd1Vk5ZTlFZOVBCRmZqUGlxWG9iL3JWY0lBVk5SZWlmUEVIRGlnVW1veTBCRS9jRkNqWElFTGZuMXdoY2sxVUIzQmdWbVl2dE5sSWkvOGJ2Vlo5UDVHTTJOeW14RUlRNkJPdXFMY0pMYnNOMW1aNERRUFpLMm1YSjdUNnFQZ2tKajA5MEt3RGRHV0RLcUlUUGErRUVDTjI4eGZQWllDQ203N3dRN0FFbFl1L3l2c09nRTF1U1h5b0xicUZTZUVaaE10TkNMazBvdFp5K3dKZTlOSHNRb3RaeVVFTHh2d1JGekxDdkJ5SEYzeTRwNTlIZ1J6Nk9YaVVhaEtUKzZ5ajdqZEM0a1RSa0V3OWJUWFJaY3ZqMlQ4WHlqUVZKaGsydVpaSWtLV1NtclkycWxlRHNNR0x0emJaQk9RcE9EaElvaSt6UU1CdTFYc0c1c2d2ZzhSUC9ZeHdRTXlLQlpYNnJCNEhtd294SEcrTmFJWUcra3gxbUFlUkgxVHdSYkJDV2oyL0UxRExPKy9ZWXkxWjFwbHpPSEhVa0M3ODlLbDErOWpXbjR1dEFWaXAyNHp1MUp3UVFEb0tFZ3Z0TWdDR1BOR3BxRUFmWmJHZTdRVUZBaGJGVEpzREhzOUp0bmI3dUVzR3hnbkRQdzhOU1pHMkxlQkR5UUs2VW9DU0JJYW1DUVNBdkR2K05QTEdBTldBQzFVTDMxV3NPelphNGtpRGNiVURlcmdWa1BEWWMweFdZV0ZYR2l6RHl5OWp5bndiK2dIM0NyNWgyMnJNRHBmdTBpZEJ5aU0veFZJRWwwYlZzWU1YdTd4SGt0dlNta3Q0YXlubytDQ0llSDEvNkJtY3VsZ1BiNzJOUzhFQUJ5Ni9qRVU4TkRSQkFETnpwdGV4dkhCTVhMbjlJc0dad2N3OTlpaG1LcW9KbzMzM2VvVXQxbmNydmptaVA1N3BOOWlCZzBHcU5DSWJnN0VsVDE0V3ZwbmpHR1FFcGtWS0RJQXJGaUkxb3U4M2VxYU9FM2c5TlR5Z3YwSWpGZk1rVnREb2FXRnhPdGpmQW11SkNUNGJCRVBvS1JzZHhGaTYyVm95c1VBKy82Nmx0RVBMQlp3eHVSaTIvMUZwd1NRRktyUlQ3Q2gyWlR3T0NqYVg0S3h1SVV1dW0rUW51TWRwRWI5d3d0TEVYdkhFTEwyYlFzaDdWeFpiVWVEN2E4RUFKejR3R01vNExIZnMzVEEwdTRYc05vbDBKeGpSWXEzaHd1S2hTS1F2a0U5ZEdkeGwzMWpuWEloeXA0YXFvRDZRcERRa0tLME1Uait6RkpHSFo4QkhIVDdPSG83SWxXaDVmT1JNUmdJU2owaldQQlJFWU1Nb1lTUHBRd1R0MTdpM2hjdFdDd1NxbzUrcEdtblhJWVM5WG5BM3RKTHJ6VGhIb2VaaTdKd0diMExGL1J6djdUVCsxQUNGaTI2YldTdDBtc255alNERGhSbkFYY01Ka0pVUFpNUm1BeGVEOE9xZlIvOVpkc2kyWmFZcVJJNlBuZ0ZBT0NtVy9UUkZmREwvMi9qYXJaZjhtenZWRnFZZ1E3TStnREVud1F3Sm1zUkZtQjV4ekxyZTUrN1lNWmlFUHROS29vV0FGRGYydFR1ZXpIbjV3RkU3NjZHMkllOVVJdTFIbGdJZTBZcW11YVI5SUlaWG9SZjRTU1JrcGJ0MXdRT09teWZBMnJvWk9nWnZkT0I3Y3BkUWw2K0NOVVViL2hFSDJnUkNHSUZQSWJTUlRua0pscFhWRGVlMmcrYktZMkFFdnRVT2dQYXU1T1ljSDJyNDRsa3NhZ0NMTkJjMGFlZXh1cXhySDIrRW5UZzB0b05BUGJoaUR5cUFncCtzRklBeTJpYmwvZ3NTZjRRTGp5a0o3UkNVSUczQmpQUU1VY01aQ2lFVUhwTDVPQW9sWHEvUGtJcklpQ2NadWlsWC9BU2grbys2SW4wNzl2d3lTSytpUjkvdjVSWUJVZ2F6Z1QvU3VrSm9acW9xcjRxZ3FVQzJvYkc5ckdKOW9pbmRZY3cwTjRvbHNmK2swcDdCbzJVc0dLREJrbm9SMHpSOEZCYURFV2Y1blM1aDh1RklDcWNBOWJJeDl5VEJiS3dmQS9GNUpyMzZQdXd5QjlJK1JVQytsNUlxa3FwbHc5TXNHUE5aUFNJMTJGelg5ZThiQStyMVYzb3BxWm9XdmdoZDZseGtDNVI4RUZaeEJ1dVRPRW44YUozdnk3VHlOcVVGeUhJc1l3eUFwbWgrV0JZMW9qSUZsK0xHN3NkN3QrVEhyc29NTUExOGQzK0J4UmZsMnRlNmFKZDdYdXpwSkhzc1JWSkNXRzhZaFhkeFhvbXlkY01sY2c5MmpKZ25XQitJRVlOaGZNTUN4bjdNUlJyR2ZmQkFEOFdkUnBrVk9KblVNTVRTaDg3TzVVUEZ0QTg4QkxCVGdRSlhlVFcyMUNOZTdtYkQxMFFFRm05RmpnVG85M2dsVi85T1krbWdJSkR0d3NBak1jN2JraExlNWVRVzExUUJ2dWRDNzhMQ25rdzFJSWVJK3BGdncrd1ZnL0V2ZUdLM2szN0lKVUFKYkNIRDJUOGdMMFF5bUcvZ1ZFTFZtU1I3OGRZekdnYldYQWRFTTR1UmlHNmErdmVMTndnUE5vTncxTHdYZXhtNVYrcmFyTVNRenF2OUxQZ3EwRmVOeVJhYktxL0dtbERFQVBJNFMwTkpTMFlqUWhPeThUcnhzQm40VUw5NUZCWmlmNlA4b3NnRWd2bDJRTWxIWGp3d1l6MmNmTWNOTE55dElZMnovOEs4TWdLU0J6YjVhUkNmamJHdXdTTXpHVllEcFVpT1M1cTVhRFdETkJCYlZxWWNKY3pZZy9qSVFaY3VGYzh5cjYrMEZrSmJSdDBmS2p3MFpib3ZQRjcvb3lZQ0NFWW9MY1lqTHJNY0dYRElwSGlZSWJKQ3NKckVoUVFLZHJnSHhRT3ZvQUN3Q1N3N0ZDeGhwWTQ0QWNIYll5bFhYQWxLcC90dlozMmpScTBvWi9KeVJDa3lTTlNuaTQwR2R3dkhycndPMkRrTklHZUJCdG83TURhbWlqZEFydDBZd0JGbWFSR1BkNnhIWGkwUlVuYkRoSUFkUG5nY3VSMVN5OWpRZzJzTkdLZzVlS09xeDhKWGpwcjMwL1JIaWRLWTdoSktXdGJvUVFxNlhPZS9hTlF0RTc2a1NNa1ZYQ1BhVGYwWFc3ZE52dElTVHpSMmxCNklWNEJud0NwK2k2VUhnNytaWEZDdEVVZkNVeVNvRkF2aWdsSUZqT3ZnRURVZFpJMEFqUUpGdlBVZ3ljcG5JUjBWMWNzdEFEQkJvREdGU3Y3MVhDeDFxWE13UUUyRjVkVlB6OUQ5dEtQMGFBdDhUcUNEQTBqSVlQMEtIdkRJcDdaTXRrUCtrekVZaVVxalpjQ2dlWDlsejI2QXU2LzNwcXhkTjJsNEFqUUxhdmNaWWdmZG5KcGpGOVpXK0dLRmFHNHdCNXV4d25aREUyVjZaWWRJV1dMQWltcEVLWWtiRHR4TEF5Y1dmK3daT2hyMFFpUGZCVzZvZjFuRTZSWE92WldJNGxKME9hSEZOZEZaUjZMNEZ5RGJtWURTa0pTR3BvWUFGRGJ4aTM1ZUtmay9iSytlZ1FBa1dRZE1BTk1nN2NaM2RtT21FeHRZVWhNYmh0Yll4aU1NclJkWXJLZGFHY2w1MUVHaFVYRlhFckFLZ1ZkR2lpWUsxRHhBc0JnbDNTZk8xU0g4emFPcXYxbkNTbVZrTUZvRk1zRCtFN3dVdVkvT01DeGpCOGlhdVpvUy9xWU02aEpXQkZTYjNzMmdOVkhVRUFLM2x5WkkySjZnWE1NVHZwTGFKaUZOUUpUSENVOWJSRHp4aFhGOFZCMEN1NTZ3cXIxMVNBRHdDcFc2eWVEQXNzaVZNSlhhcGxrcUdheUZLZzNGSjk5eVJpcjhFT2txekFZWUMwTVc0cUVoSmtDbWdXSkFxbEFxWUFSYXRRalFYNGVaV1VaZzltN2tBczIxVXB1a3R5OXVMcFFZc3RTOXc1dWY0dS8ycmM2eGplOGJBMzFPTEhyYk0yM3p3K1RYUlV6VUFtTjdkekt6dEhPUlloS2w1Tm01WGhVNDkwWFdyejcvaWxrdFFKTGtST2RyYUJYQ0JYWEhQOUJHWVIrSXNlZ0dPNmdmeThVdlpURjBiNFFpNVI4OG1JUXMvZGVxZndyTVY2U0JGQkFrUTRBMlBZSUN1aXVBYWlGelVFTzhWYzBnakhEMGtDVEpOeUNLWjlVZ1VWc1R6TDYrSk9BREdmcllBRnFRS3RZbytwZTJrMDhiVFV0QTJzUUVLbHFJSjlzOEFYWHIvSXQvK3BaNWN2c2ZZRUcxK0FQWTRkeS9nOHE5UFJWVlViSEVtcGl4emtNSll2KzNwU0Y3cXNKZCtoTnczWkdoZlcxbDAzNDh6LzZ6TGdsc1lqQjJYL3pZd2FFai9DM0FPQVAvZWNUOHU1L3Y4bDZoMjE3TW14b3NmN2xXOFVORTBLRFIxQjRLWjBzTktQVS94VWNIZU1KbEcwUUVJYXhod1dsNERVTXkwVllrQlFpSTFWTGRRYXFSMTJZZmgyUVB1aUx6Z05HMm8zVWhpcjJKd3Y4VUhhYTk5N2FSSkVTYUVUbGhWVFJRYUFpdklvcnBCWjR6V1lQSzNIZ01JaTBGZ1lDSVlTZDI0UWpJdGxHa3BTRXNoMFFTRENKSGN5bnNPTDFxb2UwSlJHY2JaTU5wRHBKR0FLNXFMQ0M0dHZsQmhBVUtwWEdMdzRqQWlLQ0V0QW5SVlpJMTJYNFJKS3FwNzdkQUlXTk1aOWNpUXpWcCtnSFlWdDZqRWNpSisvYkJNYXhvMzNNUGJkU3hTcVZ1MGdaRy9iUFErekRCYUI0TEd1Q1F4U0JyVWZSUVN2OEhnVitGVHpSRjNvVVZPbnRzdm9YcXhlWFVUV1p2SGpwa1Z3d2dJVGJBUkdNRXRpV1VTeEdoYUs5cHFmQVVFQ0FVU1NpREQ4RU5LYldsQzEyUm9oc1FGakNQdFB0YmZDLzBYZGNmQ3o3WHR2SE05SE9jL0lzYU9pcitBRmJWcjVLc3BKRUZUdG95R3VJSTA0aFFhUUtFdXQ0L0R6emk2MVJFWkpOUHhFSUpTWDduUWozNjNGMzRBc1Z3cEFjSnVQS3FxdzgrSThIV0Z0WTFsRTZHS1RUOGd1TU9BR3BFaW1WNE1PbkdzR29zdGxXU3RYY1UvU2xRbGpBZ1VxQndNZFFBaS8yeEFJS0prU3ZsRU9NNlBjWVpucDZUbGRLOFVmaEpyTVllNVBCcWtiU0RpSlZWZSs0YnRzakU5RUU5Z0JqcWVxNlQ4c09hSURJSGRKTGRZcHlCa0FPZFhIVGpTQ1d1ZmlRa2hlTlp5d1NTS1Z6OXQ0QUxBODZIa0N2WTkrU29kcUVMMGFLQmQ1UjJUQ0lWK3g5TWNXeU9pcm4rUzYydVVWbGlsZElGdXhMdjhUTXFCa2ZEUUsyZURvS1ZSeXd4SHFTd2MwSmliMERHZHoyd2dmODNnQlJwU1FiMDR4M2ZIZ0tMTmZVY3ZpYnNHUWw0b0doTklPUHVHU0d1Vzkzb1diV3d6YWloTG54eW1DOHl0c2haRUgvdlNBcWZlMm9pejJpTXFTNnlpdjF6a2ZKaEJDei9mdFh3RlQzWmxlc1F3c1A5RXFJMktpNUo3T2t4d3ZseTJhS2grM3ZKUi8vaGRVY01zcnllK0c1WVluUjY1dks4QTVGOS93WFZ3QmZQSlFvMEVDVVVwN3JCdEN0MlBCc2o5NzVGS0gwL3lrdkMrbTZyVEhnaFE4VHU2TkZ5dkNWcUgwa3VTZ0VIeVdnWk9JR2c0NkJPSDNRQmRDTkxNMkdBcE1FTzZ3YWdrVzlUbjNUMmJmUGVwQXc1SElMaDBlZ2xGVTV4TEoxM0xGbVI0WXdveTk4QUFZQko2UFYvVFFQTmJBa2hLUlVhY0xhb3hZamJJNFAxbVhsV2N3Q0V2MXVBN0VuaHMrT3lOTDFIZkdJdU5qcy9zM1M0T0psQjI3V1o5RlE2VnpidlE5U3JLYVV0eGlFMG9MeGN6RmJvWUNRcVZCSGZjdERqaGJQZWhNa2RyTXZIMFhmNGdIYUlTaWs5dWU2bFQ3UTQ5L2U1dlFianNkRGh2Sm0vN0lBYWZoWmYyNXZqWU5ST25hNndWUVVxTVRNWHNneXhNYUZXN2dvZysranlUTHlBSDNTM0pwVDFLWmdCU25qdFppMjgrRWRkS1l3SUN3YUFBZTVSZzJicmxSTXk0K2dnTGNJQUN4cGwxaE5xbjc2MFhPL1pSSjVLWlhaVkRBTGhqdExHcFZnbi9WamJBZVpGQmRRK2Rkem1KVElzUTRFR1BqRVp5WGNVUTZzbzFMcTFDdEEyR2c2YmpHcXpnZFJpb2pMcUNjS3JheDBNSDZlV2h0WVF3enVYeks5Q1FuOUZyMHhVVXRaRGhMRVEwenAyOEJlSjhvNDBpTDB5S2FRd3lRTXkzUFpEei9mODlFdHRqTkZaVEwzTE9EUW1ya3RHcTdGS1JYbnRBUGZ5NjJILzNvcVQ5M0RzU3gwZCtHVXhDK0tONHh4SHo2TGc3R0dBdHI2WVBvcEVUS3JIOFVDQ21hejh5b2NiQ1ZmU080WStHQXFXZHhFdEgwZ2dBSzNQRnd2K3JjQWVHUGlsZnpsRUFCN1IzeGNlbkFkenRiL0dXNUQ1WTFLS1VFUnh6UlFiSU5DOXI0MzJwd0FwQlRReHhYSFYvZjB6bU5CUUlNd0NISDhRZ1I5dmxZd3ZFYVJUQUlkWEFMRm92dk5aU0RGU01MSlVLWW9pTE40K0JQM3pnVXBNY1ZHUldBa0JSaE9IVndVSmNxNkR1bUZGNG96bE9lZ2JXNHhCOFpISWtYSTh2bFFoY0NYY1pKS01CdlJqcEtXSTVENVNBV3BSK3pOYXA2UlZRZEJoVGN1TEpwdjh3OGQ0Z2RlaFAxQy9Wd1lXdHBVOUtlTWJDbkw4aTRQaEZkb21VRnFhZUF0VUNYa1FWY0NxQksrNk1XZEY2V25DR1Z3RzZQQ1VNS0NrTjJ3STRONzk2OUxTVlZUTmJabml3Tkl2RSsrTzRLdkhHVEVOZXdWczdTakwrUHF5VkVkRHZKRlY3UFpBRXVDZmh4ZzhyUGVFN0cyb0xTM1JBa0VtSXl4aUltTk9OOHN1aWxsd3ZkdzFMdGZocWhmb3lNaHlPRVErYmdGSjFuTVdWa3JPSHRVQzRpVEo2ZlF0dTFSVDBoa29PN0Z1b21VTXUwSUJobHBqcWlld0VEaFhCbkRoUWZpQ0FFTkl2d0NINGVnTnNxSHlzdEVuQzVxNktPWURwaHJ0RTN4ZkNMM3RzYmJZMHcxaThzVlg5RHQvUHRGUSs5elkyQXQrNHFKUVp0aHRoQWhjRE9HNGVJZVVlU1dJUWlabHBrNEtFMDFDMW81K243SDNUTmdxUkl0Yk12QTZnM3JJaGdQR0NnT0FQUk1tZlJDQ3c4V1FLSEFIZnFZOTMwWXV0cmg1Z3dzWGpFVTNDZUUvV3BGazVrQ25uc0VCV1Q0ZzVuSVJxYmxiazAwelAxbkZzcW1TbDE4ejN5WGVqRDB1S1RnT2pmM0V2akNHNmVFYlhSY3lwbDZ0eEhWSTcwckgwd0tPK2tHUUU4cnd2Z0lOUXJFbGtKRitkU0NtZ0JlVzJYREZIT2c0TVZGUzJXR0ZIUm0zVmRqcG9nM0xkancvWVE5Q2pPeksyNEliT2Z5aS8waVFDSWxJTVY4OWJyK29mL3grekNKb0ZIaWpsTXRVSXZ0Q0VwR0lHZGd5ZVRGWGdIVVpLdmxiL1l6MFArT3JVSWlCVGRVc2xna1p1T2gvZXRseXZRd0tkQnJHQTZXbWk5VVZLWUVpTFk1emM1dlBvSUNtcnh2QUZUeVBOdHAyVEZjNGI0Wnh0ZzZNWUFUaU9Day8weGY2bFE2VzU0bHhib1dmRGQ0VnZ3ZWZ4SXNnaExRVDdIeFpnMDlsVFhFem5WTXJzSXFmcWhNQWZmRkh2a0JNcTY2RkpYWS9vR0Q1UkZGWHpId3M2RzR4dUtVK2dzeEc5azc0NzY0eElvSkw1WjMrZGNpRVhjeEM3cFBSREdKQUhrclkvTmNDMHdTbVFrVVN1RGlLem9idnc0OEV0Q3pGTVd4RGNZbnhwT1FmcGVGWWxndW1rUXMzeWdLVTZDVFczUVJNSWtRRlVUYkxtODlmUDVSYVpoM0FXUnU1LzZBTXZ4bW5MS0VOMFNBbHo0NnRwL2dqMEo5T0pCeUg4djFKR092Wkk5ZzR1TlRZWjZHQU0zOFRmYXoxeTRtOWExTXo3WU1VSWtpMG9WcFl4K2lVeGtBS082emU2VGV0ejI2V2w3U3d1bVpNaFl6Z0o0UmRpR0swSXIzUDFaUmhxakRzeDhYajQyNHNRY0FIai9mWWJPam5lRVFOWDRtcTlEMFhxN0RCVUtGbkhZakhUSU9CUzBLT0tCelVOREVZSXdZMkdjSWwzcXZWR1ljWU9oSGV3UWlBdVo1VjYyLzg1RXNZQm5hRFBBQ2N0ZkxYc3Jzb1JFNm1nWVZ2Rkk2YkQxSndYNFh2RllzbTd2UTRWa1ZoVWowVy9WUmsvdlZrRTY4aUVqVENrREpBUS9pTFpnZnRLMHp6UGFpWkRZR2RROW1QRXd4eTk0STRsT3VtTzVlT1BUa3NSVDE4L2tYV1RPS1IwdURxczJlM0JiNy9SR05WU2pZd3JzWDhaYnFiWHpIUFZOc3pJQXEwVTZzVG9WMGpqVXowb3NyMHQwaElVRTVZc3Z0K0NEbURsV1BYL3lLL1FmTGg0RElCaFhyaXJqbjRDZ3dFd0FpL1FqMzBMblJLYXlJN09KTG9ObkdSc1lQR1lzRjMxMFMvYXhoVnBTajV3bkVwb2c5MXZQY0JEM1E4TThNeS9ITDU3WDNuT3BER3AwMXZRc3NHZDhWeEZGZUxodHBkV0dnRUpxR3NsekRLeUg3Sm9LSVdnbjJRMjNmU3hGV0RtaVVFTElyTkEzYWlLU1UzRzFHNE9CMzk1MWlCV0tuMTlDT1VqUm4zUTlhL0Y0QlRNa2JLSU4vZlNRbGpxVURjTzdlVGFBalUyektYcGJKcWlMblBoZE11bGE3cFM0WTBDT1ZNcTlkNE1YWGtZWFh0YlVuOURYaGdsNkM2TlB5UThoVVZ2ZjEzbzVoUkZXRkdSVGRBTkE4c2dXODJSYWxTM1B1ZHZNaXFWY1FnUlZIcW12WGNGYkZQOFVhK3Z2RkVzWk4ySWZ6Slpod0FjTHJBVXNzZzJMdGdZRkZSYThYcGhVREVPaUcwUk82RnFLR2hWTHhVNHhRYkpJcHFJWUxwc3N4SmN1STlKSVJaM0xnY0YwU2t5UEtVcTZMd2pMYVExM0NJV2loU1BKTnVub1BoWWcxa3lwRXhhZDk4UlIrUDYvS0JJQ3paenBnWkd0VGV2bUUrZmVTT0N5MFBlVEZqMkV5aHFtNDBLMm9kb3BPQ1pNUm5NTzE0Q0d0WWpCWTdtRzR4di8ySlJ3U1NEbEJaSFFIZ0VlS2dnR2MrRWtCZ09yQ2gwOUpucUhmUXNZRGhXR1FvTW9TNWFJMGdtSDZlak1lLzRoMXJoUWFEYXk4TzhCaVRjdXRIQkFHRlFEdmRQaEFBSG5vUVFJM1dzR054SFkwNkh2aS9LRDl4NnhqQ3BKM1FPVHhZclRtSTJ3c2pOMnJYelduaUlVbllpQVF0bGhKMFc5RnFiU1VvTUd4QmNRaEJBWjczem5QR2xsWmwwU1Z6SS85cjN0bXhIamtjellHZnREUzRlYmxJZmMwVU9SdzFTVjJHZXByVFB6d2R1aDNjcng0NjdwNFpxeDhqSlJkbjdFb0RXT3FyRjJwQXVZbjdvSXhZSTkrcFRvL21IUVRZT3lyNStaVXlMTHRSbW5Md0s5Wnd5M1p6VHlBVWVWOTYzR0p3SHpHcU51Qm9BRTR1SGU1cEtoNGovR1ZVaGN4QjA4U2pzRU9Pd3I4dHhCYStrVFNJSDU2MXQyWnh2aVEzMnJoYjhBQ0ZaZWhpcEV0VVpkVjNMNFpUc3MxbDEyTkZuSTl4VDNaRjhReUZXWFNBSDJpTm9rUUN0enhvWFZnT2ZtaHlvVUVXc1I2QmNZTWJZQjNrVWtXR1FsZWxFM1N3YU1aVmxJR3ZaZUY4UXhMYmMvejN3TjdtblJUVEhYTjRPemthZS91STF5MzNhUUFVUEdCZDZNOTBVSlM1WTB6eFZLaTN3aDZvU0Y5RlVYSmJKVEc5M1JoUWU0bEx6a1VSRVRQNFpvZHU4QUo2RWhVMFE3ZThQY3JSMVZaelNoVEtUa3I0eVFDMm9tajlwYXkveXBkOFR4bnAyWTMvTGpWS0NxSUVZcitvZ1FpL2R1cGNJenFCNFFZWVpJY0F4cDkwZGxwb1ZUNk9jbmVsaTRUdVZPMG5kcEJQNW5JbWVneW1KWG9zckpyRlJtVWsyY2JPWDl1THBnSUI2Y0lpRmMyQTdFTEdQUEFDcFdjYnVDNjdGalB4cTNrZElyVjh1bm44aTRJMjNsYTlxejVJdjV6YXhLZVVRam5qNDFhU0ZXU2JoMEorajdnVVkvcU9rSkFVQjk3My8zVG5XZW5tRHhuaEx3bHhxd1YzSUN5SmdPdXk2VzJyeFRLQkMxdHpqSXk4bVhsR29xcmRDekJNdXRLbE9ZdU85N2o0SHNvY2hNWm0xNk1SME9uV2FxekxwSlUyS2hTb05mcmk1ai9LZXQrU3phanB3M2g2TWJtdDVkd1NTcnAyOFVUdTR6L1NtN2F4bldLTmcyRWdPRWpobTN1ZlRURTFnOEFQSGFta3hOZEJha0UycUwvdUdCd25tL0lweGdPbEtTQkZRLzNFOXc0OWQ0WWxDa1hoaVhHdTlRWFN0OURCeGZGcExzeUY5ZXZRRmFJVklLVWtGSVN6TTZnUGZlZWR3T1BkVmFjQ002VDA4UjBINlMrSGhCYmhKUTk4dTFkTHNOdmxBUm1YNDREejFtbHdkQ0ZqT2xka2FKb3ByekdJQ243WllLOU40cWJSa3h2NEdSYzRleVVlT0NCS2R1T2t1eFlDNGI1OStVcm5yK3dnY25lRktmQ21VUkZLTlJNWEhKd0lxTlJPSE1ad0tNK3dQQ0dDRVdZVkliTzIvdzhiVXRzMXdsSklqeHpYdmtyLy9PTVZMVklCV0ZkV3hjejdiQnFhbC9MWHRjVzhKQXF5UUdqcW5CbFV1RXRmM0lPSE5WSURPcDhvTEtsRWxJSHYzdUxoeFVVUTU2YkV0cHBsNGFzaC9sUWhQQThCU0ZBYkZoT3dKWmxZbUNFM0doWThrQlExWVFrTUZXUTlzSTViUHpaMmNkU1FPTGwvNlRHYlVmbTFPWGJJUGw2Q0JXRUxlZ2dDYkhNZ3UrM0VJREh5TWMrZ1BKR1Y3R01zeGRHMUh0b0QvbDd2T0dkWjdKWnY2Q2dJV1Q3WFZzQWF6WGUrWkVabnZHYWR3bG44S1hGRXNzK3BRUXhwdHl3T3IwRXNiVWM5akpCcVVUMDFBYi8rTmMvaHk5Ni9rNTBXYVd1eW93cExVZElPUHFZWXBPVnZtYlBxL1ZGQk13WlNCWGs5cnUzK0czZjgxRmd6d1Eyd2FTbmxpTGdzSWtuWlMxT2tQeTFBQ2xKN0lLRWc4dlFEckpndGZwTk4xbXMzOGVrMVVSYzRWQ1dVVmdCY0kvMS9NVUFHWFp6SlZRbFNPVmU4OHRuWTNoNkR3a0t0QU9vSW1sRVVlYkVxZ2EzM2c3Z0JBN2QrdWlMa3JEeGtNbjZ3b2RPeXE3bnVWN0ZjeDJpSitlRlZJbFNkQ2NtZ0xDQ3NaQmFQR2RxYW9ORjZtQUlzMmliTDhKUGk5WVlkL1hFaEs5QkNZenNib0NyQ2UzYUdtMjFrVXNqQWYzS3ZZREpjVDZIOUJ4bWtCNFRBVDhLRWFuQ05ROTBicUI1b01BWGpWQ2l5TUg4Z2d4VjFEOXRURkRpYUVVa1hUbG1kZm1xNUV3N2NyVzRPQUNKeWRkaXVJTlh3SGgwcHpDREFvRm9HOThydGpoY0t3RjZnZ0NSQnVzVk1CVEQwR01nV1Jrc3JYUno3bTQwMG01bDNhL0NhMENsUEh0SWV2ZVJqNTlXMUVHWUtWVmQ3QyszN24wWUFISHM5OUtqSytDN3ppb0FwUGxELzFObnB4WDFTZ1Z0V1dZdHhOQitVc2RwRE9OY0JGK3NvU2NtQUlndlNnL2ZwMjZmWEJ1bFA5dHNpRU1Db3RITllDYkxTam5YVlpBMlErUEk0WkNQUFlKbG5FcnlYS0k4d0JFU0lhMEFYYkRFdmVQL21NdFNMQUlKRGhFZ2xaV1U4aWYzM0pZT0xrNUtBWjByTUNlMDFWNTVpZ3FUbGwwU0R5RGdiR1ZCSGoxdUMrVUtkYmRZb1hlVEdweDRuKzIwZitoSDFpSXRKQWlLUVFBczM0dFFsMTRLUFRXV1lvNHM3UEVUU2g3cms0VzBCZGcxa1JJaEtVaytENWx2L1Y3bzJHUFFNRWNWRU9RSGYrUGRNajJ4am1yaU5HanBTTEluYWJqUzBzd29nL0RTTEJhOFVaVEo3K0c3VUpueXF0MnpuNUdEeVFRTUltQVVTa2lpMHdqbFJLVFJtTVNVVkh6NXEzK2VJbVJsSWtSaW53QU1US2tvYWFrdzVoZGZNWmVzMlN6cmk4c2FLL1Fvek80MndBK2hUOFc0ZW01WVFqREoxWGV3VDdDZ0I2d2g0MElvRVVFaStmMTlMQW92Vi9TM3lBb3hGa1VyWTB5NTRIVjZlbVg0b2I1S3Qyd0lHdmNPQlE0ZlRBRXpwUm9adFM2akpMT0hrQi82cisrMUR4N2xZeWdnaU1PYUFKeXI5TUxiVWhxQm1yV1llNUNXOHZGUXZ4eWZFUFdBR3EweWQ2Mk9GOHI0RkFwR3ltNEg1YjNpS25yQlJwc0t4aGpRdTRHQklvZkp3RFdSUmhrSU1VbC9TR0UwY2NFeUE2NUZDOGg3MkFUcEpkeWppdFJUeU5HM3FIcHhtMm9SUWV4bExuRDhISTBKZnpwUW1waTB2WlhzMjB0dlo5UmgwdDJvNXVpcDAySGhzWW9EbEg3U2hwdUZqVnRzYmxUU3JmRzRRbGpGV0E2U0FtNFFnbUxyeDArUUhmOVZJMHBXclVSRXV3dDN0R2p2alE0OUpoR050OTZTQUdqYStNQjdFcWRjT0wwZVBsdTYxcGVFK1l4elJ0ZCs5NDRZd3liRnBKWE9FWWljY1ZpNFlrWUdBdS8zbGJiUDIrMjUwR2s2NzFSU2NveDc5WnBER3VrV0R6ZkxCMXRvVlhLL0YwdEJIdTFQNjUzUVRnWjFzeUVBa0JCYnQzRlFoV29FVEE3dEdmeUVHL09CVmxyWms3VmFDMVhTZjdDZnJGRWVoNEYzTUU3UXE1ZENib0VKaTJMWmpwSDliWHVORXdXZzdJc1M4cEFmbExJbWhJUG5GekF3bU4xV3lFSkpsZGV6azlYV1ErOERzTzU3a0g4Y0JiU3pISkRPZitqL3hmUUJRVFdwb216VGdHaGw1MGhvWGpSZi9VYm1rVkF2SUhGQitjS2lRY0xxOUl0a0JuRko3MnNERTFtL2hwcFE2Z0FjZkpZSndrRGVKUXFQLy9hV0FBbElhbUZGWCtwMWNScXV0SVJldW1tbDgycUZwTDR5cEtSRFFiR2orZ3FVWkdCWkRhdkVNQ0tEd0NVc1UwVEhKVjhMbTZTRkdja2VZTVIzM1dEM3d5Q2w1RDRBQmVQMFRMcjhHQXBhS09VSXlEendkWGxJYjBoSzJaYy9LUGFXdHVmMGsxOFYwSTZwR2dtU0NOTklwRDBuTXIzM053QUFSNC9DSmY5WTF4RUNsT21aOTkyT3JlTjNNcTFZOUNUSlFLd29vTm1zWVBHaHJueDl4ME54WXVpSFJzUi9pMlF0bzNpVkx1Q2htK3pORS9zeFhaeDk4VGlYWjcvVGZCK2xMVzcyaUo2YWlDd3hDL3hhOVA2TFgvTi9SWkhNQXNacUlkTjVWeldocWdZR0hPUlVZc3hrOEhmSUp6QVdRNlI5SDZKMDNoSUFBOWt4Zm1YZmlaRFNRR21LeUlYT1Rsd2M4cUVzZXlwSUF6NmU1VE05Qml3V01mb1FrQWlBWm9JWlVvL04xcVlxeWRiOTYvVkQvKzJQN0x0SFkrby81a1hjZUVzRllEMXRIZnRkMFV6ZlNZM0YxQU5BMTlqc0dHRGhmdEFKTU1vb3ZaRmxpNGlCYXpYM0xmMkpsaDdHKzRFOXRpUzBZRDdCb01MQVVqMXhyNUJMRUdoQWNjVVdCTVdBdXNLV0ZKV1BEc1VOQWkwSHI4T1JBK0VIclhyL0VpelhuQkNGcitMWllOc3J1a29KYmpBalVLTm80R2FmTEtVSVI0MVppTDVwNmEvLzVONnI0Q0xaMGZGM2dTc0RDMCt2WHRMczNpbisxdmhlYjdVaXhXYXBOTmV5N0JaV2Uxa2F2QnBhWnh1ajdDNWFPeEdJNGIra3VhcHE0Y2FkN3o0UDNJM0REUExnNHlvZ2NOdnROZzNPdmVjb3B2Y0swbEtrZFZobW9tWWdOelpJN0NlS2QxU0tFTUorOWYvRklyNkx1clppcnZwQktvbnV3WXhmMkhPYVhndk1QaTNrQ2VDQmtPTDkzcHFXd3NtQWk4cllKY3RTYUF2VEhjVVErT0wxc0tmV0tqcllvTnYrQ0VLME42bU01UlRhVTVSRDNEa29vTWJGb0xSWUdjRFB4aDNJYURDcklyQWIxdVhGMjZyOTUzbnhmUWZLSG9pRFF3dG5FOVRJY0ljcU1UNkZ2bFFDTFpCYlNsMHpKUWlrSm5XS2R1UGhYd1lBSExtNVdOeVByNEE0cWdCbGZ2b0Q3NVN0ZXo0aTlWSVNVaUVWSUJYTnV0R3NvSlRwSTZXVHZSa1BLeGpzZmFERWdZQUtodWhKem5ndjNJSWJrZUlRRmlzNkJxN1dzV1VNL0xCYXZwUVNsQUVjMWgwUGJqZFllek80NHRNZkk3eUx2eDdNVVBtZTc2VWpubzBvT0E4aGo2RkZZOTkvb1BDQzNpNlVESWRyVFZuY1gwSm53OE9SWGdjQ1dwaFMydW1sSHJRTjBhNGpYSnVNYmhTOEd3WHZoWFYxWXJ6SVVkMEtaZ0JaWkxRTWtwUTBybVhqbm1sMzhvOSszNTV4WFpIUjQxREE0b1kzMHRhZFI1T3VDOU9FdnRNb0VCZ2dkd0x0QkpKNk1wcURud1hPS2ZxOGFGVDhYdjZCUWlmMDFyQjhrWDFSWlpFcEI5a1J3dE5GMG1NZTcrK0NSc1U5ZW1QUzI3RWgxbGtVQjRKOFVJcWppNmllNFNLMVV5enB3QUlXYUJ4TWVkeDRtS3AwUlhPTE04enJEc25vaUdKWnJKVS9POUNDRVl5RmNDN2Q5akhTNFU0V1lSVFlUL0toaFFROE92WXY2RUIyNVdYL04zZEFHbEZHRTRzWjBqanB4cDIzQWFjL2FPNzNTQUhpajBjQmdkdjhDMmZmK3F0b0h1NlFLZ0JLUy9EN1F6VVR6YnhYdENIZ2h1TUh6U2d1VkJuSFB2VFlvd1FyTkU3S2xoSU9zSXFkdG1OWU1aU0c4Tk9mUFZJYjRMbWdDTXJKbkc0WityTDBYbkQ5QUVYMndEZWpXS2pvUVVSSkFqT2t5YmlXRkd0Q3JITFNZdzZyU2JBeVFjQWpUL1hUcWdtek1GM3ZGaVd3YkVhUHpZYkxIRExMM2p6MnR4UXNwaXBBTnZ5WXMyL3BHK1ZZenRjeVIzUXQ1UlRNQmR4WUZESmU4eG1qS00vdHFTOTN6eG05R3lLUU01QmJrZEdTSkNvaEkwM2RTVlRuMy9HZkFBQkhibHJRdWNlbmdJRGlNRk43L3RSN3VYbjhuVEphcm1IUnNPV0FRU3RFNk9iV3NaSTBkazEwL1JzTU9BdUdNWjFnd1lDZ1hsUmVId296V0lHbFh1QlZ2aDhENmpQYmpZYVNpRFVrZEpXSzRFU2pNY1VLK1dvWmxyWDFVWDd2SDJQZnBMNHVpWXdTZjFFNEFFNWwzVzk1aEp1b3dhb1VEdG8rdE1yTzNwa3BCa28vaTdkSWcyeEZLREVRcDZnWnFRL0QwWEU4YTl5b3JMZFJRTVFQdGZaQml0NFZneEdUTlRDMFAyc1kvUFFJV0FvUG1WdEF3RFJlQWpWck5aNk0wNFhiNzU2ZGZlZHZtMkcvYmVHcytNZXJnTUR0UndWQXhzbjN2REYxbXlKcDR1bWdpaTUzdzRQekxSYU1VNFpzWUVQQ3dra0lXeXdxSzM1d3VGMUV5RTZDV1JuU0thSEFYR3dvVVRJMUtJOW1xWnp2S1p4K2xwdHdrK3RmWVJvOXZZQ0xlQ05hU1lpdkY1RTRkQzM4RWVoOGlBVTJJb3dneFBXTWZ1dEJGWkMvcmk2YndZZVI0MVAweWVoSDVvYXkyUHdsc21wZk1PQk5qVHg3WkMyR3FiellwcW5JQUdVT0Y5RXBzZUF0aGtGTVgrelFmMGRiSWpkTTQyV2tWRlBTS0ZmU2lLeC85T2NCYk9CMXYxeUZRc1QxK0JYdzZNMEtFZWpKMy9vRjJiampYaG10amtUVmdwSG9kRXFDZGdhMExYMXh0amN4VExZL3NnOHNZL3E3S3hHVS9Veksvbm5zQjhQZ2YwaFllbUdIQU1ydllRNTZUdEpPSEVyRmd4cHZIUW90dHRsbXZEZnN1QXhOcGIwQUwrNDE5V2NDeXBsWEFNQ1NTMDQwWTVQOEdhWkVKZE5jaHNMbGczamRFOGJEQ0hub0ZkQ3J2RCt3djM5SnRUR1ViSmo5Nko5VlNyQ0diWWpJSEhDMjNDUm1nVXpmdnZqcDl4cXpOclN0QUlSTXRwbmxyQ2MxTDN4a3F6dng3bDhBQkRoNjgwWEc0aE5SUUlCNCtSdHFBQmZTK2gvL2RNVU5nVlFabFVSYTNIRWJnZm42NEd0cUlEZ2l2aDRIeW9CWDZwVWsyUCtTand6ZU1BWXdFdUQrWHRRbDBwOEZqd1M5MWg0U2gyeUw5dFhDaEhOYkVVM1NJbDRPOVk5UnJoL2EzQXZDVjlXUlVsbCtVblJRMmpEWVd4QlZRcmdxTXlmQk81WThkbUN4NHVJOEVLRFl0cTlxMkkyUUJReW9nYjIwNStRQVAzRlVGKzhWdURidVl3b1RQQjk2eklmZWVrWVFHQXJQMHE2NHI0OUJobUhQRHNnenlHZ2lra2FrVXF0Nm5MQjE3TGZuOHpzLzZuVUZKZmo0WkJUUWdoRlNtdU8vOFova3dnZE95MmkxRmdXUlJpNWN0MUR0SEpodnduZktMNzF5SE5hN1BUUHBBM1RsM0pWUkF3TVg3TzhYQVVXcWJ5ZzAxOHJzQTFtK1UweUlXWU9JbEtOUVp5Z1NhMVBRNXVJSE9VVFRDMTBzTmhsaVA1bXdJam9zWEJDeEFtZXlMeC94SkkvMkl1R2lCWGNqUDdCZ0E1cUpyandEVzF6VzM0UWZkQ2hUbWhHeVh1Qk92ZWpWYjhyZUxQYlBHM0kzQlFQS1lJelE3K0VERzhPdUFRQld5enNnYklIUnNuRDlMdFhqYi92WGdDeHdmOFByRTFOQVFDRTNKMnpoT0UrLzY2ZUZiV0kxeXFoSHJteXVPQ0xBYkFQUXp0M3Q0RWlBRUp6REVqdGZCSVlaSTM3dUZYVTRRSTdlSkpUUFhmVkFWc1BJdDNjOUVSWGpJdmZVYXpRemVvdHJwYkQyaGtyZzlCS3FnREQ0THNVMjJMaXE3MzRRYVM0QVpmMXhQOG9Bay9RcFFoUTA0U3BRbE1xUWlYc1hnZTlNd0JLMDlJSEwwTTJLSzZFekFzVTdoZHpoRTFVdlV2VGVYUmVaZUFDU2RTREh3UmdVdU90ZXJac2pUYlpKU2pWSlpLbkdTVTc4d1Z0bTZ4OThPL0M2Q2ppNkVIekU5WWtxSUlDakJDbnRBN2Y5NjNUKzNTZWxXcW1FSUtvSmpCYm9iS3h5Qjh6V2dWU3NZREVsTGcyNlYrSUNlUjJqWFFBMXcrVkdhSXQrT2dPbGxuQnh2eGo3ZkZsSkYxOHBhMWs4Q0pKNGpoVERFbmdLaFhZWkFpVzdkWC9tbW9TN1M1SWtNaU9HL1lkZkt3ZDl5NEFueEVYdEJXTFgrdUY2RFBVSjB1LzdaeitsOE52N3czNStlQ1hNNEw1RllSMW5MNXhJR3Y5NlU4TFZadnRTajBnR0dSRlhVRG84eUszNWk2VTFVbHVpWGt2cHdoMW9qdi94djdEK25IaEU2d2Q4VWdvSVFtNU93SVV6bzdOLy9HK1R6aElsZFhiNFJ1WDhVbWRVek5aNXdXd0RTSlVPYkVWQWwwR3VtREh6WGZEWkViamp3ZExwNHI3VFFCQTlUMWcrRXdMWFFYQU5scU1IN0R1UlVrSzVrY0Fta1NsaGhObUxJUWxDQi8xdlNhS2FmR2l0T0ZYb2tUTE5CcnJYcFJUTVc4QVhqTmNzMWd4Z1IxL0dhRGpNd3lqN3liMDdMWG5pY0s5cStGQlZ5MXJzeURiRnM4cEpCekpJOHhFOUYzZ1I5ZzcrRkRtZUxUWTJPYkNtZ0IzUXpaQ1d0a0dxR3NyVVZwSVR6cjN6dDNPKzg2MW0vVzdybmxnRnhIWEVZYWF0QjkvemIzSCsvZmZLYVB0SXFKcnFaZmFMaHloSWlkZzhaU1phNGt3aXdyZjRjbVh3TWRiSUEwT0QrQytZUkZ5WVJTMkcrQ2lzZ2QrN0Q1N0R3dmJLdVBEM0lLcTBpY0dvUTNYYjR0aHQwWU9WM2Y3b1I5YlFsYkZVb2xDUTFHd0hBS0hRdDRnVEsyRFJzb2hrYU1qTEV6anNIM3JTWE55bFh2elp2ZzltM2J6d3hxdWlyVDBMWmZPRElLVXZTQ0ErWnZJeUxDekwvZVBOb3ZBS2RITmJjYlcwQThndDBtUzFycmZ1YVBUMEgveGprOTNSeDFTbVQwWUJCVGlpQmlyUFhFZ24vdWYzVjkwNVFiMmlURFV4V2lVNm4wVUM2K0RHQ2NOR0JldUlsRUFpZkVrNVJNTlBjYnhZRUFnaERBY0F3MEVjdUowZ1RPT2VDc1NKN3hFNVhyelJPWU53S2JlVTBLcjRLZEZ3eWF3Um1ScjdTNGpIVkN3YkZRaW9xZGhXUjdlRnBMYVhTNTJDRm4zcEcwSDdWaWhLeUg5WUJST2EyR05hR2JSNG1DL3VsVWlvdmJVREZsSjZSYm1pb0lIOTVCcDZJczJDM0FKdGc3U3lFeWtKaUNyWFNTczUrNDZmYkxmdWVTL3doaHB4QU82alhKK2tCUVNzU09GUTFaNys3NytJVTI5OWk4aGtCSGFVeVRKUTFVUnVYUWJKSXVLdE00STBHdVFyQWRBM29ZS25xR0xodWdBTGJxcXYxQWhYSFVOaHI1ZWRFK0p6Z0F2UUE1WUJYaXI4V3loMytTa1loeEpiZTZiaWcwdmdHYTQrQ2cycTFDdEhiQXJrcmwyanAySHpBbEZsOWh4M3NXVHVPZUkwMEhDZXd6NHRsR0NGaHhpZTNheXl3Q3E0WlI5VUpMRXNWeWp5SU1DY0ZtWmlPWm5UbFphKzAwS0JVUjRGdDV1VThRcXF5U3JRelZrdGJVODQ4NzlPYnR6NzVoOEJEaS9rZkIvdCttUVVNQzVhVWFFZzNmL0c3MGdYM3JXRmFrMUVNMlJsdTh1OXRRVVlLU2syendEVDgwU3F2UU0wZ2FqL0JFQ24xNkdWeFRWMG84WGc2K3o5VE9jVUVkUkJNVzdsTlpDZ0ptaTJiU3NLRjFaY0R3WTBnMmxDenFWdXp3K2NERFV2aHRoUFVQSStRbEl5aXFzNHdBU0llVnBia1JrRmhyRWF0TlJCNXFFc1Fya0M1NG10N3dpZUVBWlRza2V3OUh4eWI3VmNhYUFGRDJ0a1I2TGZTczhCaDNWamVhNk5nNVFkMmlNcnc4aS9POThJdGFDam5RcEFTYXM3Q0xaZ3ZacnIyVU9wZXVqLy9YNEFEd08zQ3g2Qjk3djQrdk1vSU93QnYxdzF6ZnFIMDdtMy9kT0s2d25WSklza1lIazdtTHZBRDdhMTEvb0pvTnNDVXQwTDFTcFdXSlJ4NkhLSG1DN2NDd2NmSUFhdXFTamN3SjFSQ3FjWU9OSmU1MERSNDd0bTNmb1Ewa0ZjZU05NFVRemZLV0szMVNqbUt1OExiSnRvTHZTeDVJZ0xDek5zZG85ZGFjUzlMU09UZ1lzMnR5Z1lVQ0RoRlFpSE0yNHh2V015WUJaTUR6MDRHZVRoQXc4UEM0Z0xSaXlNZzJjZkFiQVZkSTJnbmFGYTJjTlVqNENzZVZRdjFlM0p0LytQamJQdi9ZODRkT2hSYVplTHJ6K3ZBZ0s0V1hIbzFxcDU0UGQrVkU3K3dSOWh0R05FcGNwb0ZaaHNCOVVQTzR3aE9udmNabERseExnNEVBcDZncVhzdktkVlpFRkl0dXRXNytEaVBSWThHWU5RWnIvUFppdW03QWx1b0hkcHBlQTFHdHN2SVBXWCt4b3BndkR6eGxYVm5IcnluSlhwdHFtZUNIM25oZkRnUFVWU2Z1SXRSWEc3NFFZUjc5UGNhMW4xQVBTRjJnNUp3Z3VBVWJmWE13dkRZS2RYT3BlN0RxSndvcGNQVVRZMUp4VXBtWUhvR3FLYlVpYXJsT1VkZ3E2ampQZVFaOTY1am50KzZUc2hLZVBvMGNFREgvdDZBaFFReE5FUEVDVGJ1LzdvYjZWejd6NGo0OTJRUEdkYTNrblVLNVRjR2hjck5ITDYxTDAyYzRPOEhxNC9LRE56d1BscDhIVUZ0QXpZZlI4Y0syckFvbFVMdVJZbGM0VjNEaXpTVmc0SFlybHZZYjRBUUMrcTJtT1JtbTNUbE16VEpjU3Flc0RLYUlhcFhvOEVzZzE4V1JlcWczNEtnQ2pFS0FhWUtEUU52Wi9JMXM4eU13b1lHRXlrd0pqMHdJZUR5ZWpmQzFnVER3eml1WGlhbmphVFZGbUh1aW5RenMyYXIrNFh5UTFSTGVVMGU2RHVUdnp1OXpiWXVCMzg2cUE3SHRmMVJDZ2dnQ09XSWNIN1AxUTkvTnR2cVBMSldrWkxMVGhQc20ydk1GV2d0c2JkSlNGMERweTh5eFBpcWRDenlBcGZ0K0JCQlhvaUZrNHNCMzFRaEI5L1IxV0x2MllDWlhFcC9WRVJ2amViTWdMenNDQjAzakQwSlRLNzNzbmlZc3NlUW9SUVkyMmN1cU8zMUFkSWlESzJPclh2a3BaWmlESXhpV2lkZ3o1RjdwY0JTVmhLeU13ckRDWmZDYzZBT0RJZ2dnV05VaFpLeWJGcllFREhnOUJjOENZNy8xNWdiUUxhUWthVm9FcEFuaFBkVEpBYnB0VUR0QVJXbmF1cUd2SDRiNzY1Ty8wSFA0TWJEOWVQMS9YRzlRUXBJQUFjemJqeGNEMC8rZGFmckIvK25WdFR0VFFCUjAwbGdyVHRRSC9NcWlvZ05kSE5pRlBINk5ZaVpuVS9VQUNMU3pGandGNzVRdEV3d0U1UmxPQmFHd1BVRnpLSUR5cmNaYU80bjRKOUNHaXBCek1tY3VGTUQzdXFSSHRrc05CTkUyMEZJOG9tbENIaGdoRmxpRi9qMS9KdjcvcktvdklCTHV2TnFXOXFWSUtIUmJjYVVUTjhXdWZodVNzK2dUbFFlczA5M0ltSkFWdmprMWEzU1RVYUU3a1J6aTRJbWhuVDZqNVVrMVdodGprdDdhemw1Ty9ldS9UUXIzMHJTQ21GeTUvQTlRUXFJSURiam1TUVV0M3p4cjhuSi8vSHZXbHAxMFNWSFVaTEltdVgyUEpOY1VCYmpZQjJScHc4WmxRTm9sVE1HV2dTL2Nud1Ezb0YvdnNRWGNlTEdBNUc1QzNkdE1VQXVTUHVTY21lbW5QOVluaGNvcFF6YzVqaU55VnpVd2VVL1d1VGVDV0QydTl1U3lVRm5JeUloajNIM1h2QklTL3BXWWQ0SGRGMjZWZk5NU1lmZmIrWGVOMnRwRWRpWWZWN1NPTmRWT2RpMlU5Q283K0F0a1c5WTZla3lZak1yWERydktLWlVsWjJJYTNzaEhhTllyd1AxYWsvek0yeFgvem04NUJ6NWdFZnYrdU42NGxWUUlDUW05TUc1QlFmK05YWHlKbTNYMGlqdFFyZExLZkpLckZ0SDlIT2ZBWjJncW9TekM4QUorOEU2aEdjdXJWL0MzVlFMQk5jbXYyQUZJRU9aclhwMmNBU0JONWpBUGZVbHlFSnl1dUtPTkJRU3JSaGlRdzZLUWhCN09ITVdKOXZ4eUxaWGZ0VHJ1R0d5VXYvb2lUZjU0UDROcTY5RlN2RWVWam1DSWpZOTBzTHF6RUVsNzNsUndSbmRDNVFFMktydCtJZGZQc041c0MvL1dRWEFtMHJvaTNIdTNlekdvK1p1ZzU2N2dRd3V5QVlMYU5hMnc5cHR6U05WblhVM2x2eCtPLzk3WnpQLy81akZSdDh2T3VKVmtCWVE5NVF0MXYzdkVlUC85N1hwTm5kcUNZN012SW1aWFVQc0xLTGFEWnROSElIVkdOaWVoWTQ4UkZCUGFKbFJISmZlRm9HSVljUSt6UmNyRjBOcXhkdXZuQ0NpcDczQWowZkRFUzAxd2NnZ0txeTRESzd6UGxMMFI0Q29sRU1hQXQ5WEVHZGpIYmpTVVVzRkE2NnpuUXZpVThxNS8vQ3VvZXlsUTBmQS9NTytvT0NZN1dmYks2MHZTWFRvc1JHTTZudFhGSHduLzhvN0NCS2RVdXJ3R3pHcWtwWU9yQmZxdEVvSlZWMHB4NFNOaHVVZW9SNis2VWl6SUpxb3JWc2pmalFyLy9FN053Zi9tZmd4azhZOXcydkowRUJBZUJJQjl4WTU3UC84M2ZTdzc5OVM4cnJZNm4zZHNoellNZmxndVhkUUR1MURYMVVFK29sWVBNTThkQ0hqQ05FM1VlekRJOFowVnI4d0RGTldET1BYZ3RGTWNCYVVOaENqOXdEN0g1NXFOczZUYkFDRUphTjN6eUxVbUlVdUM0VmdraGdKY1N4WHRIWEljVGhFSTdub2lUZkExTEx5K2pnMmIxeW1aVUMyTGZUdXhKa2ZRamxZNnkrQ3l3VUNsenNieFFjOUJWRmRoOVZZamJEZUcxRlZpN2JEMGtWUklUejQzZERaK3RBcXBGMlhBbXBFZ2kwOVhocGhBZC82M2RuRC83VzkxclFjZHNuclh6QWs2YUFBSEJieGczZk5wbzkrRnYvVE83LzVmK1lNRjlpdmRZaE44RHVad0RMTzRoMmJseFp6b0o2QXN6T0FROTh3QVJWankxaUcrVEFFUXR0QTVqSDcxSFRGcXZzK2tYVGp2bUNFd3dRSHU0NHJBdE1xUWIxcFA2ZkFKRHdGVzRGeHBsaXNYaDZRZkxralZoVmcwQnMxMTJVREhic2hsR3VvZ2NBK29DajV6TFZlY1BoQjB1R2dzUHZHZTRiTGlEU2dqUFE0MlgwN3dzRWJac3duMlAxd0Y2czdOc0Raa1V0eFB6ZU82blREWUVJcTUxWFNCcE53Snk3ZXJKbmxFNysvbnVuOS83U3pVQnFjTnVSV043M1NWOVBvZ0tDZU5mUGRqaDBhelY3NE5mK2R2WGdMeDRkMXpxR1ZLMTBVOHJPS3lDVGJVQXpROW1ocWg0QjdRWngzM3NFOC9PQ3lSS3N0Q3RNbXZOWjlMTCtFZ2xIM3JkWWhlQUVDVEM1VlNUeUlBTWdSTmtweXpaWUNzcW5MTzcxci9YRksvNXZMam9QQUV6Mk9SWGFnZGdDSnEvak5PVlczeXpTTkxoRnY4MklsNjV4b0RpbVBEM2ozUGVMSlgwWFMwOGprcmYrSmhSdTA1WFVhQzNEaEZsdHlXVGNiMk1MSXhIc2YrNlZNdG0rQXVTTUtyZllQSFk3OHV5OGdJcHE5MVdveHN0TWVkcU1sbmJWNmVUYjd0cjg4SnUrQ3BBTDhOMXcvcnhLOG1RcUlBQVFSMjlXSEtaTTcvMnZYODk3ZitWM3F0SFNTS1J1b1kzSW5xc2d5OXNVM1JhUkpFRTdvQm9CSUhIZm53TG43Z1BHS3dBZzBKd3d4R2lGNW9xQkdsakNZakYxOGNkY2poUk1GQ1ZmL3I0Z0E5cjFXN3dCb0NSajY2VFhFWHU4T1ZZbENmSDE3a2hPQW5sNHl0aEcyazIyRUJKcll2bzJZMENuU0FtcDdXR1JRZ3lGR3ZoazcvY3dDRVAwWHdXcVd2WjA2U2NzMGJiQTVoYTI3OXV1bDF4L0ZjVjJRcFk4MitTRmo3d2ZlYjRGQ0NUdGZTWmxhWnVnbmVjMFhodW5VMzl3RWgvNnFhOEdIcjRiNENkRU5qL1c5V1FySUFBUVJ3UUF1L2ErbjM5Tk92NmJiMDJUN1dOSnF4MjZEdGh6VGNMeWJxRFpzSDNPb0lEVWd0RXFjT0lZOE9BZFFCb0JvNGtEWnI4V0tvTERMY1pnRGlDYU9hZStzTGxzU3h0bFRZZ0lPMHhiZWNLUXVTajdTTUpYbXZzdXRiYlpBbDFOZ3BaSlBVc2lPaUFTNVNMZEdlZ1R2RGdnenZnb0JhZm9VM2RoaWdmQWRFRGJCQVR4V2VWWU1EWTFCNEhwTEZXYXVmODVsMkhuMVFla21UZGdxdENjTzZYckg3bGRtSldveHFqMlhzdHF2Q0tjVHpVdDc2aXJjMzg4eGQxdi9Kb3RuSHFQQlIyUFhXTDFpVnlQdmtmMEUzc3BjRXNDMHJ5OSsyZStvbUwrYjdqMHRWK0FuQnJwdGtheTV4ckw2cThmSjhhcmdpQ1NSeXVDOVJQQTdEeHd5WE9BMWQxRU83WGI5Y2t4QUtEUkM4a1BoRkJYeWdFMUZVQWNRTUdWZ0xrMEwyYXg0WTZzdnQzV2pJcUZEeUp4SUZXLy9JZ3cwQWVCOUhDeXZCZDdFempESm1DL0xBQmxZUlQ5cHZURjdmUkdGdVZpL05WWENJVXlpN3Rqd053MHNKanZyaUNZdGNSMExtdVg3TUNlWjE0aXVXazVuODRGVlkyTmozd1lXeWNmRW95U1lMeU5zdXNaVm1YUmRWMjl2Syt1enZ6Qk9SNzcxWnMzNS9mOFBuQ29BbzQrYW5YekozTjlxaFFRWGh1V0lHazkzL1B2djNpRTJXL0lwYTk3WmNmbHVUUmJvN1RyS21TcGhlZnZFeG12K0Joa1lESXhySGJmZTRFOVZ3QjdyeEV3MHpaREtxQmVDb2xhVGxzYm1nMUYyRUhiQkoxMmI2YmVCNUFVSkNFejFKTzJIU2tKTFB2N2xaV1VFdU1zTk0xUDZKZU9lRldOTFp1MUlDYURIWUF1WjBCYVN1N0tDUS9Ebkd1aFU4cDZYbCs3SWE2Z1lWZkxvaTRNOFdKUHY1Q21SRXBpZllieFpDUjdYM0FWSnR1WE9WL2ZBcW9KbUJ1Y2ZkKzcwRzVlQUNvUldkcUxhdmZWRURZVTFWd3Q3NnJsNU8vY1B6LzJhMS9WZGNmZTZjcjNoRm0rdUM1V3dFRVcvMG01Rk5RS1NGc0g3dm1GVjUvSStiZXF5MS8zaFV4cmN6WWJvN1RyQ21nMUlzOGVBNnF4WlVzMEcxMHpYaEtjdlovWVBFc2NlRGF3c2hOR2FydWJLcTJQQVJOelpXbGc3VUNVN01wd1dXWWF4THVxQSt0bUZzdVpJUEdSTDk2dkJDY0NXb0k0SlV0b0tJVVdKZHNHbGhsWmdhU0VkTm5JUVFYS3N0V0k2a3ZoWjdoZE4yTWFCYWU5T1M0VExLeWZLNllrVVhZVWJzMllrbUxQMVh1eDQrQk9OTE1PMC9PYlVpMHRjZk9oNHpqLzBUdVV1VXVvSzhpdUsxQ3ZIYURrbVJEanJsNWFxdVdoWDd1N3ZmT252NlFEUHVoYzN4TnErZUs2V0FHZlRPV0xLd09hN2tlYTR2NWYrdEt4enY4OUxyLzVHN2kwdThYc1ZFcHJseVNPVmtSUGZoRFFLVkF2OXdCNnRDem9HdUsrOXdEYjlnUDdyd1hHRTZ2UVVEcEY0dTQzUEhSb2l1MUdaUnBuakRCS1JrNGRWM25wa1FTNHk3VFQ1UVZJMlRLR2lvRWw5SklzY2VLSFlpZmpBQUNkakNsUXNmajNXTjBHOUlFSU1GQThHU2pXZ01jamdUaUx5c2tkRFZlY3k3aHhxd0c2QnR2Mzc1UzlWKzloR2lWc1haaENaSVEwR3ZQc0hiZGo2L2o5eEFpQzhRVFYzbWVoV2w0am0wMndYbXZITlVhODc0MjNiOTd6cGk4RjVCN2c1ZlZqTFNyNjgxNmZRaGU4Y0NtZ0NXVFRpSHpqcEQxMXIxeDY2QWZ5dG1jRDAxTTVMYTFXdlBSRjVNTWZGc3hPQStNMUFBblFUTlFWd0JwWWZ4allPQVBzZndhdzZ6S3JjTWx0SlAzdHNzRm1zU1JXZmVOdUxxRnNMU1lSeUpBUWpjMHBMSFZQa242R3I2aEZQc1pPS3FESkNhSnNHUk1taDNHRXVYT2hkbG1pR0tVY3FwNUxOVTBvSDFENHY4aERSNFErU0wzMTdoa0Z4MFpmbXhib09xN3RYcEhkVjEzTzBhUkN1OVhJZkU1V295VnNQWHhDem43a3c2TFRUV0tjSU9QdGt2WmV3NVJxWWJQRnRMUW5WKzJGVWZ2UlgzOUw4L0NiWGcvSWlTZGIrWUJQbndJQ3NKcGk0TlpxZnZMbWY1eG14ejg2dnZ6cmZoeTdQMzlYYnM2MmxlUTZYL3Bad1BsN3dYTjNBbElEMVpLNUlHUmdOTEdCTzM0N2NPWWU0TUJ6Qkt0N0FXM3RlQ2h6Y1lzK3N3UWZicFBDMmhpQlM4a2pNSk5LSWdOc0ZDbUJUSktnWkRtV1VmMmtlS0VHRFd4RU5Jd0gxQWhsM094bFZXUW1DTlEyNnhOUHlSV2FKUEJqVUNsd1YreVdHUTQxSXltdXNISjlvV0RXQVYwbnF6dTNZZStWT3puWk51RnNheTdyNXpQU2VNeDIvUUkyN25xZnpFNmZOT004RWtuYjk3UGFkWVZLN2dEdDJzbktBVW1iSHgzTjczclRUelZuYi90T3MvRk1UN2J5QVo5ZUJRUUFBamRuNE5aSzEyLytUN003ZnZUZEs5Zjh6VnZsa2xjOXArM3FEck96RmJaZktXbDVGM2pxZzhMWkpqQmVSamt6RFFKTXRnRnRJN2ozUGNUYUhtRGZ0WUxsSFVBN2grMVhNdHdNQ2I0dFJlVHNVbkZxWmljMWdUbENGRkVGVTBwMnJGWlp0dFBySG1Cd0x3bWd5RVFVTjJvNFcxclVxeGJXMktHZnBlTFlzekxlRktnSFJIU2xIUDRlUVlrcnRkQXFpMXJGOHZabDdMNzBBSlozVE1pc2JCc0ZVb1YyOHp5MlB2UUJ6azQrYkppNFVpQXRJKzE3RHV2bEZhWjJpNmpYOG1pOFBPR1pQOWhzN2p2NkQyWVhidjlKOHhMeVNWVzJmRExYcDFzQi9ibzVBemVNZ0hlL2Irdk9mL2V5eWZTaG42b3UrNnJYNmNvbDBLMkhNK3B4SlFkZlRKeS9CengzajltdTBVVEFaT3grUFFLcU1iQjFEcmpyVDRqdGx3ajJQZ05ZMm1ZRkQxMERrMmRsTHJsazJyUVBSaXdrRGc2RzlQT1ZDL09vdHVJdG9KdmxocTAwVUVNL3dJTFdCQkJEWmtvaXNkQjJFZG1xUmhBeXFQa0xTNDM0ZTlIOTJyd1JOQjNHazVIc3VId04yL2F1SWxVVm00WnNabzNNejU3bDVnUDNzVGw3VXBCU3dpZ1Iya0syWFNwcDc3Vk16RUMzeGJTOEYxVmVuN1RIZnZWL2RRLyt3bmQxd0o4QXQxYStqT3BUb256QVUwWUJBZUJkTFhDb2d2ekt5Zm1EdjNKb3ZINy9kNlhMWHZ2UDA1NlhMT2ZabVpidFJzS09xMU5hMlErZS9TaTVjVnBRVmVhS283UzltZ2hxR0Q2ODhEQ3h0Zy9ZZllWRnpBb2lOOGwzaUVLeGZuWkZ0WXpwbmtncTVJWm1FcFdJcjhrSTJ3a2dDY1hjc2VkV3pXZ0ZZVmRZT1dTYWZ5WkVxTXFrN29hRHppeGJCRHNXOVdXY2cxcEJ1M0cyMHIrbC9UdTUrOEIycVpOZ3ZybUY2WmxUMkRwK1J1Ym5UdHZHVUtPS1dLb05GNlpscFAzWE1hM3NrdFJ1QUZKMzQrWDlJMXo0TTh6dS9jMGZhOCs5OVFjQWFRenYzZnlrdTl5THI2ZVFBZ0xBMFF4Q2NPalcxQnk5K1Nmd3dmZi93YlpuZmV1UDVYMnZmRVhEdlZtYmMyMnFVbzM5ejRQdTJJS2VPd1pNejVnbHFaYkRUUW5xaVkzYXhrbmcvRVBBMHBwZzEyWEUyaVhFWk1WMzlmY3lwU1FBVlpFQlZCU2tvSS9CVEtiazZ6d3lFa1ZWQ3J5REZ5aFlTYmN2OUY1WVBtWEthaUNSNXA4N3BxUmd6b0hwK2lpNGJBZWp2UXNXVHhkS0xhbWVRT3FLZFYwaHNjV1pPNDh4WHppRjl1eHA0V3dLakVhQ3lZZ3lTbUN6SldnU1pNZFZySFpkSlFrZE1UdVhxOGwyclZJOXFrLzk3aDNUdTk3MFhlMzhvYmVZeTczNUNTZVlIKy8xRkZOQUFBQng5T2JzeE9lN056N3lFMSswdFA2Ujd4dnRlY1V0M1AyaU1adU5lVzdPVnpKWlR0V0J6NEZ1blFMUDNnWE1MeEQxR0VnVElISno0eVh6aXQxY2NQeDJ3Y21QRUdzSEJOc1BBc3M3aWNteXViOTJhbXNpTkFQYVJYSWh1UlpKTEF5TDlTQmVNVy9GV2w1dVpSRkJTUkJLZjJJWVNRV3BLS3ZhTEdMV2ZqUElJS09OOExIRldoVlFUbzN2R21EakxMRjFEczNtQmN4bm0xSjJ6UjlQSU51MkM3VWhtaW1ZUmt4cmw0dnN1QUpWUFFLN0xVVWE1L0hxL2hFMjc2cWFoOS8ySHpZZS9LWHZBM0RLUEk1NFZjU241M29xS3FCZlJ6TndPSUczNkV6a245WEgvL3YvU0ZmKzdYOVRYZktGbjR0dGw2TGRPdDlKM3Foa1pSZGtkWTl3ODVUdzNMM0EvQndoSThGb0tkeWFJRldLZWkxQk0zRGhJZUxNQTBDMUpOaTJDMWpiUjB5MkFhTmxrZkZZV1NWSklPYXpqTGJOMEZTMjJaQmtEQTU5NVlmNGN1WkNtRERjcFo5YkhTbm1McXUwWFllY0tXVmhlUURIVkJ2aG5vU1FEbWhiWUhiQmRoYWJid2htVzBDN0JkVldJTWwyR3h1TmlHcGlrNmViZ1cxRDFDdE1PNTRwc3VNZ1VqMEcyazBnTTllVG5YWGlMT1hqYi9temZPK3ZmVi9UZlBTM1RMRy8ra25KYkh5aWwzejhqendGcmhzUDE3anRTQWRnVXE5OTN0OVBsNzd5ZTZ0ZEw5cmJwVFhvN0h3SHppclVLd0laa1Z1bmhPZnZCV2NYREZEVlk5dEdXSDNyRHp0S3dZS0FyZ1cwQlNvUzR6V3B0KzluZDJiS3QvL09OOHZudmVpZ3pCUm9XakFycEprclp2TnNoZE1lVnF2eml3blMwM0syWE15QVpFVk94b0txR21IbmR1QUR4emJ4NHIvK3EwaVg3aEZ0T2dLVk1EZkE5QUxRYkFEVGRXSytJZERHeXM0U0JheUFxZ0lTYUlvRGF6TmJRU2F3dEFOcCsyV3N0dTB4aTVtM1ZGUTBqWGRVazlTazl1UTd6elNuMy9FdnU3UC8vZDhCMk1DaFd5c2N2Ym1VVm55NnI2ZUhBdHBWUVNSN1d1M3l5YjR2L04rcS9TLzdPOWh4dzU2T0FtMDJzMEFGOVVUQUpOMThDbXc4QUc2ZHNBS0dWQkhWSk5rQ2E5L0N5N2ZCQnhLUk95dEFuYzd3dk9mczVQT3ZPY2pudmZncWVlVkxyOEx5OGk3WnRuTU5CL2V0Y09kS0dUaW5uTzF3aGNwL2RFQnZyODhWRHp5MGp2VnptNmgwaGo5NnowUHkzZC8vTzhUU0NHeWJBV2ZwMUVwVksxSWxpTnh5V1RNTkkrRzFzWC9US01uYVFhVFZTeUZMMjVIWVVyaXBBdEUwMnBIR0ZhcDg0UVBuODhtM3ZYSHIrTy84R0lDN0xlSjZhbGk5NGZWMFVrQy9EbFdRTjRjaVhqMis3RFgvTU8yNDRXL0pqaGV0cWxUVVpsMlJHMkVhSlZRVHFMYmcxbWx5NHpnd08ydlprbW9FeU1pVVVqd1JMRjdKSkFBMjVzQjBCbFFOMEhWQUdzbnV5M2ZnV1Zkc3crN3RLNmhrQkpHYXFRSmxQSkdVQUVGaTE4eEVrYUZOQzdMRHFmVXBQM0wzZVp3OTFSQ3ptUm5LN1N0ZzVaczBTUUtRaUpSOFFuaFlIRFdDdVRYQ1dyTWdKV0N5RTdMdE1xUzFmVWlwWXVybW90cG1xWmQxTktwSGRhcVJMM3p3QXM3ODhjODNEOXo2VXcxd2gzWG9kWlZ0SnZYVXNIckQ2Mm1vZ0FBQUFRNGw0RmM4cTQvcmx5NDU5QSt3NjRhL0lUcy9helhMR0hsMm9VT2VpaVJKckpiQXFoYTJNOHJXR2Vyc2JNTFdXYUNiMmhZaGFVeWtrUWNKaVNsVlNDblo3aTRLWVc3QkxoUHpWdEJrUy91SkV5em8yTmNhT3E4anRZVWpvekd4TWdaR0kwbXBJclFWMVE1OXpVY1ZSUWF3TFhnN08zR0s5Z0NNVnBKTWRnS3JleUhMT3lEMU1vU1phT2NRTUtkcWtxclJha0o3Rm1sNjdJeWVlOStidWdmZTlKTkY4UTY5cnNMUnA2Yml4ZlYwVmNDNEVuQklnRGNIdWZmYzBZSFhmbHZhZmNPaHlkcFZsemZWQWVROHpjeWJ5dHlJcERvaFRhQVlpYWlDODNQQzZXbGlmZ0ZzNXdJMktLa3dpQ2tuRXlHVlNFcElraGhIdVVxd2Q1S05HQWNGa2p3bFp5aytrbHFxb2drQ1hXUS9qQXJTMXFwOW1JQjZDUmd0TVkyM0FjczdSWmJXS09NVkVWU2dOaEJ0VkhKV1ZtT3B4MnRWeFRsazgxN2tyZnYvc0QzenZwOXZ6LzdPN3dFNEJnQ084MHFPNWFsOFBkMFZNSzRFM0NxV1VRRUFITmkxNTRXdjBUMS85ZXV3N1pxWGMvVnF0TGtDY3U2SVJ2TzhFNlpVU1JvTHFrcFVZK09kcmNSMkUyeG5ZTE5PZEJ0RzdNYkNvRmdzTGw1aVF3RWtTNnlNczN4ZFFqbnN6MHJEZ2pVMDJrVEUzUDlvV1ZDdlFDYmJnY2tPeUdRYjYxUXpwUXBrSzhnTnlVWUYwRlN0VnFtdVU1SWtxVGtEM2JqbmhLNy8yVy9sMDMvd2M3UFpRMzlZcFBBMFVyeTRudTRLV0FxYi9ESkZsSy9OVVNlM2dwVVg0Y0RMdmk3dHVPNG1yRnh4QTFhZWlZd2xxQ1hpcytwY3RjdVFoSVJVVjRLS0tqVWdDWVFLdERXYVExdEFPNEhPaVM0THRIT1hPWWZiUHhXeHc1UUQxNGtrc3BvSXFoR2xtZ2hTUmFsR2dtcENTWlhWdVZMTkN1cWN0YlJLRlVVMWdWVGpsRkpWVlZKRFp3K0IwN3ZPWXZiZ2JUai8wVnMzejk3Mk5nQVBBSEFsdnpuNUFkQlBHOFdMNittdWdJOTJHVWJrcllwK2I1Y1JSdGU4WUhuWE5UZHo5Vmszb0Q3NHNtcjEwckV1SFlSaURMSUZ0YUYyYlliT0hmaERLRWtNMDFWQXF2eFVpZVE3UlEreVprZ3FFbXN1NlJrMUx4U0VRbFF0bDB5bDVNNFRkWW0yRTNXZFVqMUpveVMrTnVzc01IdVkwT250dXY2QlAyblAzZldyemZyYi94VEFmYVY3Tjc2aHRyT2NuMXBSN1NkNi9VVlZ3T0dWY09QaGhOdCtzTHNJaTE4OVh2djg1M055NVYrWGJWZnRrTkcyVjQxRzQwdlM2aVhJazMwZ2FxaDIwSnpCYkhpTnR2WlJZUlZiaHVmOFZDUUJRQkdrcU9lcmsxZGJKU0Fsa1RRU1FsTktJaUtteUpJUzBEYVE3alE0UDBGdHozNklzek52ejV2M3ZMODU4L2IvQm16Y0EyQnF6UlhnMEM5WE9Ib1VUMWRyOTBqWFh3WUZqRXVBdzRJYmtYRGJMYm1jT3RsZmU3WUIrMlR2RFY5TTJmbk1hdGV6OTZiUm5wZG01ZjZNZWx5TmQxWXkycDZrV2dKa0NaQ3FyK3NyaGM0SnlmYi9RR3kySkhrTzVjeDJodTAyRlRydE5NODNJT245YkUvZnBadDNuYTZhcmR1YTAyOTcveHc0QldCam9jbTlwZnNMbzNURDZ5K1RBZzR2c1o5RGdodXZFK3kvbmpqNnRUa0tBd2ZYTmdETHk4Q29YbnZPbW01LzVtV3BhNjRtOHlWU2pTYVFha1JGbldRa21hSysrYTZJYUtjNWQ2cHRsanh0a09WaHdld1l1b2RQYm03ZWZ4YkFETURwajIwU1hPSGVDdUFtQlk1OFRJUCtvbDEvV1JYd2tTNVh5aHNUYm5pT1lOdEI0bTMvMUN0RXZGYnZDWG1LdzBUTmdwdHVxYkR4a09CZFo5VXQzRjk0aGJ2NCt2OEJ2bThrQXBiN3RUa0FBQUFBU1VWT1JLNUNZSUk9IiBhbHQ9IkZhY2Vib29rIiBjbGFzcz0ib3B0LWljb24taW1nIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im9wdC10ZXh0Ij48ZGl2IGNsYXNzPSJvcHQtdGl0bGUiPkZhY2Vib29rPC9kaXY+PGRpdiBjbGFzcz0ib3B0LWhhbmRsZSI+UiZhbXA7SiBHcm9vbWluZzwvZGl2PjwvZGl2PgogICAgPHNwYW4gY2xhc3M9Im9wdC1hcnJvdyI+4oaSPC9zcGFuPgogIDwvYT4KICA8YnV0dG9uIGNsYXNzPSJvcHQiIG9uY2xpY2s9IndpbmRvdy5sb2NhdGlvbi5ocmVmPSd0ZWw6KzM3MjU4NzM1NDU2JyI+CiAgICA8ZGl2IGNsYXNzPSJvcHQtaWNvbiI+PHN2ZyB3aWR0aD0iMzIiIGhlaWdodD0iMzIiIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSJyZ2JhKDI1NSwyNTUsMjU1LC40NSkiIHN0cm9rZS13aWR0aD0iMS42Ij48cGF0aCBkPSJNMjIgMTYuOTJ2M2EyIDIgMCAwMS0yLjE4IDIgMTkuNzkgMTkuNzkgMCAwMS04LjYzLTMuMDdBMTkuNSAxOS41IDAgMDEzLjA3IDkuODJhMTkuNzkgMTkuNzkgMCAwMS0zLjA3LTguNjdBMiAyIDAgMDEyIDFoM2EyIDIgMCAwMTIgMS43MmMuMTI3Ljk2LjM2MSAxLjkwMy43IDIuODFhMiAyIDAgMDEtLjQ1IDIuMTFMNi45MSA4LjkxYTE2IDE2IDAgMDA2IDZsMS4yNy0xLjI3YTIgMiAwIDAxMi4xMS0uNDVjLjkwNy4zMzkgMS44NS41NzMgMi44MS43QTIgMiAwIDAxMjIgMTYuOTJ6Ii8+PC9zdmc+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJvcHQtdGV4dCI+PGRpdiBjbGFzcz0ib3B0LXRpdGxlIiBkYXRhLWkxOG49ImNhbGxfdXMiPkNhbGwgVXM8L2Rpdj48ZGl2IGNsYXNzPSJvcHQtaGFuZGxlIj4rMzcyIDU4NyAzNTQ1NjwvZGl2PjwvZGl2PgogICAgPHNwYW4gY2xhc3M9Im9wdC1hcnJvdyI+4oaSPC9zcGFuPgogIDwvYnV0dG9uPgogIDxkaXYgY2xhc3M9ImhvbWUtZm9vdCI+CiAgICA8c3Bhbj5UYWxsaW5uPC9zcGFuPjxkaXYgY2xhc3M9ImZkb3QiPjwvZGl2PjxzcGFuPkVzdG9uaWE8L3NwYW4+PGRpdiBjbGFzcz0iZmRvdCI+PC9kaXY+PHNwYW4+QWxsdmVlbGFldmEgNDwvc3Bhbj4KICA8L2Rpdj4KPC9kaXY+CjwvZGl2PgoKPCEtLSBCT09LSU5HIC0tPgo8ZGl2IGNsYXNzPSJzY3JlZW4iIGlkPSJib29rU2NyZWVuIj4KPGRpdiBjbGFzcz0iY29uIj4KICA8YnV0dG9uIGNsYXNzPSJiYWNrLWJ0biIgaWQ9ImJhY2tCdG4iIGRhdGEtaTE4bj0iYmFjayI+4oaQINCd0LDQt9Cw0LQ8L2J1dHRvbj4KICA8ZGl2IGNsYXNzPSJsb2dvLXJqIj5SJmFtcDtKPC9kaXY+CiAgPGRpdiBjbGFzcz0ibG9nby1zdWIiIGRhdGEtaTE4bj0ibG9nb19zdWIiPkdyb29taW5nIMK3INCi0LDQu9C70LjQvTwvZGl2PgogIDxkaXYgY2xhc3M9InByb2dyZXNzIj4KICAgIDxkaXYgY2xhc3M9InBzIGFjdGl2ZSIgaWQ9InBzMSI+PGRpdiBjbGFzcz0icGRvdCI+PC9kaXY+PHNwYW4gZGF0YS1pMThuPSJwc19zZXJ2aWNlIj7Qo9GB0LvRg9Cz0LA8L3NwYW4+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwbCIgaWQ9InBsMSI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwcyIgaWQ9InBzMiI+PGRpdiBjbGFzcz0icGRvdCI+PC9kaXY+PHNwYW4gZGF0YS1pMThuPSJwc19wZXQiPtCf0LjRgtC+0LzQtdGGPC9zcGFuPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icGwiIGlkPSJwbDIiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icHMiIGlkPSJwczMiPjxkaXYgY2xhc3M9InBkb3QiPjwvZGl2PjxzcGFuIGRhdGEtaTE4bj0icHNfbWFzdGVyIj7QnNCw0YHRgtC10YA8L3NwYW4+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwbCIgaWQ9InBsMyI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwcyIgaWQ9InBzNCI+PGRpdiBjbGFzcz0icGRvdCI+PC9kaXY+PHNwYW4gZGF0YS1pMThuPSJwc19kYXRlIj7QlNCw0YLQsDwvc3Bhbj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBsIiBpZD0icGw0Ij48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBzIiBpZD0icHM1Ij48ZGl2IGNsYXNzPSJwZG90Ij48L2Rpdj48c3BhbiBkYXRhLWkxOG49InBzX2RldGFpbHMiPtCU0LDQvdC90YvQtTwvc3Bhbj48L2Rpdj4KICA8L2Rpdj4KCiAgPCEtLSBTdGVwIDEgLS0+CiAgPGRpdiBjbGFzcz0ic3RlcCBzaG93IiBpZD0iYmsxIj4KICAgIDxkaXYgY2xhc3M9InNsYmwiIGRhdGEtaTE4bj0ic3RlcDFfbGJsIj4wMSDCtyDQn9C+0YDQvtC00LA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImJ3cmFwIj4KICAgICAgPGRpdiBjbGFzcz0ic2JveCI+CiAgICAgICAgPHNwYW4gY2xhc3M9InNpIj7wn5SNPC9zcGFuPgogICAgICAgIDxpbnB1dCBpZD0iYklucHV0IiB0eXBlPSJ0ZXh0IiBwbGFjZWhvbGRlcj0i0J3QsNGH0L3QuNGC0LUg0LLQstC+0LTQuNGC0Ywg0L/QvtGA0L7QtNGDLi4uIiBkYXRhLWkxOG4tcGg9ImJyZWVkX3BoIiBhdXRvY29tcGxldGU9Im9mZiI+CiAgICAgICAgPGJ1dHRvbiBjbGFzcz0iY2xyIiBpZD0iY2xyQnRuIj7inJU8L2J1dHRvbj4KICAgICAgPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImRyb3AiIGlkPSJiRHJvcCI+PC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InNiYWRnZSIgaWQ9InNCYWRnZSI+PC9kaXY+CiAgICA8ZGl2IGlkPSJzdmNTZWMiIHN0eWxlPSJkaXNwbGF5Om5vbmU7bWFyZ2luLXRvcDoxNnB4Ij4KICAgICAgPGRpdiBjbGFzcz0ic2xibCIgaWQ9InN0ZXAyTGJsRWwiIGRhdGEtaTE4bj0ic3RlcDJfbGJsIj4wMiDCtyDQo9GB0LvRg9Cz0LA8L2Rpdj4KICAgICAgPGRpdiBpZD0ic3ZjTGlzdCI+PC9kaXY+CiAgICA8L2Rpdj4KICA8L2Rpdj4KCiAgPCEtLSBTdGVwIDIgLS0+CiAgPGRpdiBjbGFzcz0ic3RlcCIgaWQ9ImJrMiI+CiAgICA8ZGl2IGNsYXNzPSJzbGJsIiBkYXRhLWkxOG49InN0ZXAzX2xibCI+0JrQsNC6INC00LDQstC90L4g0LLRiyDQv9C+0YHQtdGJ0LDQu9C4INCz0YDRg9C80LjQvdCzPzwvZGl2PgogICAgPGJ1dHRvbiBjbGFzcz0iZ2J0biIgZGF0YS12YWw9ItCf0LXRgNCy0YvQuSDRgNCw0LciIGRhdGEtaTE4bj0iZzEiPtCf0LXRgNCy0YvQuSDRgNCw0Lc8L2J1dHRvbj4KICAgIDxidXR0b24gY2xhc3M9ImdidG4iIGRhdGEtdmFsPSLQntGCIDEg0LTQviAzINC80LXRgdGP0YbQtdCyIiBkYXRhLWkxOG49ImcyIj7QntGCIDEg0LTQviAzINC80LXRgdGP0YbQtdCyPC9idXR0b24+CiAgICA8YnV0dG9uIGNsYXNzPSJnYnRuIiBkYXRhLXZhbD0i0J7RgiAzINC00L4gNiDQvNC10YHRj9GG0LXQsiIgZGF0YS1pMThuPSJnMyI+0J7RgiAzINC00L4gNiDQvNC10YHRj9GG0LXQsjwvYnV0dG9uPgogICAgPGJ1dHRvbiBjbGFzcz0iZ2J0biIgZGF0YS12YWw9ItCR0L7Qu9C10LUgNiDQvNC10YHRj9GG0LXQsiIgZGF0YS1pMThuPSJnNCI+0JHQvtC70LXQtSA2INC80LXRgdGP0YbQtdCyPC9idXR0b24+CiAgPC9kaXY+CgogIDwhLS0gU3RlcCAzIC0tPgogIDxkaXYgY2xhc3M9InN0ZXAiIGlkPSJiazMiPgogICAgPGRpdiBjbGFzcz0ic2xibCIgZGF0YS1pMThuPSJzdGVwMl9tYXN0ZXIiPtCS0YvQsdC10YDQuNGC0LUg0LzQsNGB0YLQtdGA0LA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im1hc3RlcnMiPgogICAgICA8ZGl2IGNsYXNzPSJtYnRuIiBkYXRhLW1hc3Rlcj0i0KLQsNGC0YzRj9C90LAiPjxkaXYgY2xhc3M9Im1uYW1lIj7QotCw0YLRjNGP0L3QsDwvZGl2PjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJtYnRuIiBkYXRhLW1hc3Rlcj0i0JDQu9C10LrRgdCw0L3QtNGA0LAiPjxkaXYgY2xhc3M9Im1uYW1lIj7QkNC70LXQutGB0LDQvdC00YDQsDwvZGl2PjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJtYnRuIiBkYXRhLW1hc3Rlcj0i0JrRgdC10L3QuNGPIj48ZGl2IGNsYXNzPSJtbmFtZSI+0JrRgdC10L3QuNGPPC9kaXY+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9Im1idG4iIGRhdGEtbWFzdGVyPSLQkNC90L3QsCI+PGRpdiBjbGFzcz0ibW5hbWUiPtCQ0L3QvdCwPC9kaXY+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9Im1idG4iIGRhdGEtbWFzdGVyPSLQkNC70LjRgdCwIj48ZGl2IGNsYXNzPSJtbmFtZSI+0JDQu9C40YHQsDwvZGl2PjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJtYnRuIiBkYXRhLW1hc3Rlcj0i0JrRgNC40YHRgtC40L3QsCI+PGRpdiBjbGFzcz0ibW5hbWUiPtCa0YDQuNGB0YLQuNC90LA8L2Rpdj48L2Rpdj4KICAgIDwvZGl2PgogIDwvZGl2PgoKICA8IS0tIFN0ZXAgNCAtLT4KICA8ZGl2IGNsYXNzPSJzdGVwIiBpZD0iYms0Ij4KICAgIDxkaXYgY2xhc3M9InNsYmwiIGRhdGEtaTE4bj0ic3RlcDRfbGJsIj7QktGL0LHQtdGA0LjRgtC1INC00LDRgtGDPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjYWwtaCI+CiAgICAgIDxidXR0b24gY2xhc3M9ImNhbC1uIiBpZD0icHJldk0iPiYjODI0OTs8L2J1dHRvbj4KICAgICAgPGRpdiBjbGFzcz0iY2FsLW0iIGlkPSJjYWxNIj48L2Rpdj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iY2FsLW4iIGlkPSJuZXh0TSI+JiM4MjUwOzwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjYWwtd3JhcCIgaWQ9ImNhbFdyYXAiPgogICAgICA8ZGl2IGNsYXNzPSJjZyIgaWQ9ImNhbEciPjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJjYWwtbG9hZGluZyIgaWQ9ImNhbExvYWRpbmciPgogICAgICAgIDxkaXYgY2xhc3M9ImNhbC1zcGlubmVyIj48L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJjYWwtbG9hZGluZy10ZXh0IiBkYXRhLWkxOG49ImNhbF9sb2FkaW5nIj7Ql9Cw0LPRgNGD0LbQsNC10Lwg0YHQstC+0LHQvtC00L3Ri9C1INC00L3QuC4uLjwvZGl2PgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2dhcDoyMHB4O2FsaWduLWl0ZW1zOmNlbnRlcjttYXJnaW4tdG9wOjEycHg7cGFkZGluZy10b3A6MTJweDtib3JkZXItdG9wOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xKTtmbGV4LXdyYXA6d3JhcDsiPjxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweDsiPjxkaXYgc3R5bGU9IndpZHRoOjE2cHg7aGVpZ2h0OjE2cHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDpyZ2JhKDkwLDE4MCw5MCwuMTUpO2JvcmRlcjoxcHggc29saWQgIzVhYjQ1YTtmbGV4LXNocmluazowOyI+PC9kaXY+PHNwYW4gc3R5bGU9ImZvbnQtc2l6ZToxcmVtO2NvbG9yOiNmZmZmZmY7bGV0dGVyLXNwYWNpbmc6LjAzZW07IiBkYXRhLWkxOG49ImNhbF9hdmFpbCI+0JXRgdGC0Ywg0YHQstC+0LHQvtC00L3QvtC1INCy0YDQtdC80Y88L3NwYW4+PC9kaXY+PGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OHB4OyI+PGRpdiBzdHlsZT0id2lkdGg6MTZweDtoZWlnaHQ6MTZweDtib3JkZXItcmFkaXVzOjUwJTtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA0KTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEpO2ZsZXgtc2hyaW5rOjA7Ij48L2Rpdj48c3BhbiBzdHlsZT0iZm9udC1zaXplOjFyZW07Y29sb3I6I2ZmZmZmZjtsZXR0ZXItc3BhY2luZzouMDNlbTsiIGRhdGEtaTE4bj0iY2FsX25vbmUiPtCh0LLQvtCx0L7QtNC90L7Qs9C+INCy0YDQtdC80LXQvdC4INC90LXRgjwvc3Bhbj48L2Rpdj48L2Rpdj4KICAgIDxkaXYgaWQ9InRpbWVTZWMiIHN0eWxlPSJkaXNwbGF5Om5vbmU7bWFyZ2luLXRvcDoxNnB4Ij4KICAgICAgPGRpdiBjbGFzcz0ic2xibCIgZGF0YS1pMThuPSJzdGVwNF90aW1lIj7QktGL0LHQtdGA0LjRgtC1INCy0YDQtdC80Y88L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0idGciIGlkPSJ0aW1lRyI+PC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgc3R5bGU9Im1hcmdpbi10b3A6MjBweDtwYWRkaW5nLXRvcDoxNnB4O2JvcmRlci10b3A6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA4KTt0ZXh0LWFsaWduOmNlbnRlciI+CiAgICAgIDxidXR0b24gaWQ9ImNhbGxiYWNrQnRuIiBjbGFzcz0iY2JrLWJ0biI+0J3QtSDQvdCw0YjQu9C4INGD0LTQvtCx0L3QvtC1INCy0YDQtdC80Y8/IOKGkjwvYnV0dG9uPgogICAgPC9kaXY+CiAgPC9kaXY+CgogIDwhLS0gU3RlcCA1IC0tPgogIDxkaXYgY2xhc3M9InN0ZXAiIGlkPSJiazUiPgogICAgPGRpdiBjbGFzcz0ic2xibCIgZGF0YS1pMThuPSJzdGVwNV9sYmwiPtCS0LDRiNC4INC00LDQvdC90YvQtTwvZGl2PgogICAgPGRpdiBjbGFzcz0iZmciPjxsYWJlbCBjbGFzcz0iZmwiIGRhdGEtaTE4bj0ibGJsX25hbWUiPtCY0LzRjzwvbGFiZWw+PGlucHV0IGNsYXNzPSJmaSIgaWQ9ImNOYW1lIiB0eXBlPSJ0ZXh0IiBwbGFjZWhvbGRlcj0i0JLQsNGI0LUg0LjQvNGPIiBkYXRhLWkxOG4tcGg9InBoX25hbWUiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0iZmciPjxsYWJlbCBjbGFzcz0iZmwiIGRhdGEtaTE4bj0ibGJsX3Bob25lIj7QotC10LvQtdGE0L7QvTwvbGFiZWw+PGlucHV0IGNsYXNzPSJmaSIgaWQ9ImNQaG9uZSIgdHlwZT0idGVsIiBwbGFjZWhvbGRlcj0iKzM3MiAuLi4iPjwvZGl2PgogICAgPGRpdiBjbGFzcz0iZmciPjxsYWJlbCBjbGFzcz0iZmwiIGRhdGEtaTE4bj0ibGJsX2VtYWlsIj5FbWFpbDwvbGFiZWw+PGlucHV0IGNsYXNzPSJmaSIgaWQ9ImNFbWFpbCIgdHlwZT0iZW1haWwiIHBsYWNlaG9sZGVyPSJlbWFpbEBleGFtcGxlLmNvbSI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJmZyI+PGxhYmVsIGNsYXNzPSJmbCIgZGF0YS1pMThuPSJsYmxfcGV0Ij7QmtC70LjRh9C60LAg0L/QuNGC0L7QvNGG0LA8L2xhYmVsPjxpbnB1dCBjbGFzcz0iZmkiIGlkPSJjUGV0IiB0eXBlPSJ0ZXh0IiBwbGFjZWhvbGRlcj0i0J3QtdC+0LHRj9C30LDRgtC10LvRjNC90L4iIGRhdGEtaTE4bi1waD0icGhfb3B0aW9uYWwiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0ic3VtIiBpZD0ic3VtQmxvY2siPjwvZGl2PgogICAgPGJ1dHRvbiBjbGFzcz0iY2J0biIgaWQ9ImNvbmZpcm1CdG4iIGRhdGEtaTE4bj0iY29uZmlybV9idG4iPtCf0L7QtNGC0LLQtdGA0LTQuNGC0Ywg0LfQsNC/0LjRgdGMPC9idXR0b24+CiAgPC9kaXY+CgogIDwhLS0gU3VjY2VzcyAtLT4KICA8ZGl2IGNsYXNzPSJzYmxvY2siIGlkPSJzdWNCbG9jayI+CiAgICA8ZGl2IGNsYXNzPSJzaTIiPvCfkL48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InN0IiBkYXRhLWkxOG49InN1Y2Nlc3NfdGl0bGUiPtCX0LDQv9C40YHRjCDQv9GA0LjQvdGP0YLQsCE8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InNzIiBkYXRhLWkxOG49InN1Y2Nlc3Nfc3ViIj7QnNGLINGB0LLRj9C20LXQvNGB0Y8g0YEg0LLQsNC80Lgg0LTQu9GPINC/0L7QtNGC0LLQtdGA0LbQtNC10L3QuNGPLjxicj7QodC/0LDRgdC40LHQviwg0YfRgtC+INCy0YvQsdGA0LDQu9C4IFImSiBHcm9vbWluZyE8L2Rpdj4KICAgIDxidXR0b24gY2xhc3M9ImhidG4iIGlkPSJob21lQnRuIiBkYXRhLWkxOG49InRvX2hvbWUiPuKGkCDQndCwINCz0LvQsNCy0L3Rg9GOPC9idXR0b24+CiAgPC9kaXY+CjwvZGl2Pgo8L2Rpdj4KCjxkaXYgaWQ9ImNia01vZGFsIiBzdHlsZT0iZGlzcGxheTpub25lO3Bvc2l0aW9uOmZpeGVkO2luc2V0OjA7YmFja2dyb3VuZDpyZ2JhKDAsMCwwLC43NSk7ei1pbmRleDozMDA7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7cGFkZGluZzoyMHB4Ij4KICA8ZGl2IHN0eWxlPSJiYWNrZ3JvdW5kOiMwYTBhMGE7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xMik7Ym9yZGVyLXRvcDoxcHggc29saWQgI2ZmZmZmZjtwYWRkaW5nOjI4cHggMjRweDt3aWR0aDoxMDAlO21heC13aWR0aDozNjBweCI+CiAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6MC44MzhyZW07bGV0dGVyLXNwYWNpbmc6LjJlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjE2cHg7Zm9udC13ZWlnaHQ6NjAwO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmIj7QntCx0YDQsNGC0L3Ri9C5INC30LLQvtC90L7QujwvZGl2PgogICAgPGRpdiBjbGFzcz0iZmciPjxsYWJlbCBjbGFzcz0iZmwiPtCY0LzRjzwvbGFiZWw+PGlucHV0IGNsYXNzPSJmaSIgaWQ9ImNia05hbWUiIHR5cGU9InRleHQiIHBsYWNlaG9sZGVyPSLQktCw0YjQtSDQuNC80Y8iPjwvZGl2PgogICAgPGRpdiBjbGFzcz0iZmciPgogICAgICA8bGFiZWwgY2xhc3M9ImZsIj7QotC10LvQtdGE0L7QvTwvbGFiZWw+CiAgICAgIDxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpzdHJldGNoO2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjE1KSI+CiAgICAgICAgPHNwYW4gc3R5bGU9InBhZGRpbmc6MTBweCAxMHB4IDEwcHggMDtjb2xvcjojZmZmZmZmO2ZvbnQtc2l6ZToxLjM2M3JlbTtib3JkZXItcmlnaHQ6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEpO21hcmdpbi1yaWdodDoxMHB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmIj4rMzcyPC9zcGFuPgogICAgICAgIDxpbnB1dCBpZD0iY2JrUGhvbmUiIHR5cGU9InRlbCIgcGxhY2Vob2xkZXI9IlhYWFhYWFhYIiBzdHlsZT0iZmxleDoxO2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOm5vbmU7b3V0bGluZTpub25lO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6MS40MzhyZW07Y29sb3I6I2ZmZmZmZjtwYWRkaW5nOjEwcHggMCI+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IGlkPSJjYmtTdWNjZXNzIiBzdHlsZT0iZGlzcGxheTpub25lO3RleHQtYWxpZ246Y2VudGVyO3BhZGRpbmc6MjBweCAwIj4KICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjIuODc1cmVtO21hcmdpbi1ib3R0b206MTBweDtvcGFjaXR5Oi41Ij7inJM8L2Rpdj4KICAgICAgPGRpdiBzdHlsZT0iZm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjg3NXJlbTtjb2xvcjojZmZmZmZmO21hcmdpbi1ib3R0b206NnB4Ij7Ql9Cw0Y/QstC60LAg0L/RgNC40L3Rj9GC0LAhPC9kaXY+CiAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxLjAzN3JlbTtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjEuNjtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZiI+0JzRiyDQv9C10YDQtdC30LLQvtC90LjQvCDQstCw0Lwg0LIg0LHQu9C40LbQsNC50YjQtdC1INCy0YDQtdC80Y88L2Rpdj4KICAgIDwvZGl2PgogICAgPGJ1dHRvbiBpZD0iY2JrU3VibWl0IiBjbGFzcz0iY2J0biIgc3R5bGU9Im1hcmdpbi10b3A6MTRweCI+0J7RgtC/0YDQsNCy0LjRgtGMPC9idXR0b24+CiAgICA8YnV0dG9uIGlkPSJjYmtDbG9zZSIgc3R5bGU9ImRpc3BsYXk6YmxvY2s7d2lkdGg6MTAwJTttYXJnaW4tdG9wOjhweDtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyOm5vbmU7Y29sb3I6I2ZmZmZmZjtmb250LXNpemU6MC44MzhyZW07bGV0dGVyLXNwYWNpbmc6LjEyZW07Y3Vyc29yOnBvaW50ZXI7cGFkZGluZzo4cHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWYiPtCe0YLQvNC10L3QsDwvYnV0dG9uPgogIDwvZGl2Pgo8L2Rpdj4KCjxzY3JpcHQ+CnZhciBEQVRBID0gW3siYnJlZWQiOiLQkNCy0YHRgtGA0LDQu9C40LnRgdC60LDRjyDQvtCy0YfQsNGA0LrQsCAxNeKAkzI1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4NX0sImJyZWVkX2VuIjoiQXVzdHJhbGlhbiBTaGVwaGVyZCAxNeKAkzI1IGtnIiwiYnJlZWRfZXQiOiJBdXN0cmFhbGlhIGxhbWJha29lciAxNeKAkzI1IGtnIn0seyJicmVlZCI6ItCQ0LLRgdGC0YDQsNC70LjQudGB0LrQsNGPINC+0LLRh9Cw0YDQutCwIDI14oCTMzUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiQXVzdHJhbGlhbiBTaGVwaGVyZCAyNeKAkzM1IGtnIiwiYnJlZWRfZXQiOiJBdXN0cmFhbGlhIGxhbWJha29lciAyNeKAkzM1IGtnIn0seyJicmVlZCI6ItCQ0LrQuNGC0LAt0LjQvdGDIDIw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkFraXRhIEludSAyMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBa2l0YSBJbnUgMjDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC60LjRgtCwLdC40L3RgyDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQWtpdGEgSW51IG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkFraXRhIEludSDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCQ0LrQuNGC0LAt0LjQvdGDINGE0LvQsNGE0YTQuCAyMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJBa2l0YSBJbnUgZmx1ZmZ5IDIw4oCTNDAga2ciLCJicmVlZF9ldCI6IkFraXRhIEludSBwZWhtZWthcnZhbGluZSAyMOKAkzQwIGtnIn0seyJicmVlZCI6ItCQ0LrQuNGC0LAt0LjQvdGDINGE0LvQsNGE0YTQuCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQWtpdGEgSW51IGZsdWZmeSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJBa2l0YSBJbnUgcGVobWVrYXJ2YWxpbmUgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQkNC70LDQsdCw0LkgNDDigJM2MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkNlbnRyYWwgQXNpYW4gU2hlcGhlcmQgNDDigJM2MCBrZyIsImJyZWVkX2V0IjoiS2Vzay1BYXNpYSBsYW1iYWtvZXIgNDDigJM2MCBrZyJ9LHsiYnJlZWQiOiLQkNC70LDQsdCw0Lkg0LHQvtC70LXQtSA2MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjEwMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjExNSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEzMH0sImJyZWVkX2VuIjoiQ2VudHJhbCBBc2lhbiBTaGVwaGVyZCBvdmVyIDYwIGtnIiwiYnJlZWRfZXQiOiJLZXNrLUFhc2lhIGxhbWJha29lciDDvGxlIDYwIGtnIn0seyJicmVlZCI6ItCQ0LvRj9GB0LrQuNC90YHQutC40Lkg0LzQsNC70LDQvNGD0YIgMjDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQWxhc2thbiBNYWxhbXV0ZSAyMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBbGFza2EgbWFsYW11dXQgMjDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC70Y/RgdC60LjQvdGB0LrQuNC5INC80LDQu9Cw0LzRg9GCINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJBbGFza2FuIE1hbGFtdXRlIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkFsYXNrYSBtYWxhbXV1dCDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCQ0LvRj9GB0LrQuNC90YHQutC40Lkg0LzQsNC70LDQvNGD0YIg0YTQu9Cw0YTRhNC4IDIw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkFsYXNrYW4gTWFsYW11dGUgZmx1ZmZ5IDIw4oCTNDAga2ciLCJicmVlZF9ldCI6IkFsYXNrYSBtYWxhbXV1dCBwZWhtZWthcnZhbGluZSAyMOKAkzQwIGtnIn0seyJicmVlZCI6ItCQ0LvRj9GB0LrQuNC90YHQutC40Lkg0LzQsNC70LDQvNGD0YIg0YTQu9Cw0YTRhNC4INCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJBbGFza2FuIE1hbGFtdXRlIGZsdWZmeSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJBbGFza2EgbWFsYW11dXQgcGVobWVrYXJ2YWxpbmUgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutCw0Y8g0LDQutC40YLQsCAyMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBBa2l0YSAyMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBBa2l0YSAyMOKAkzQwIGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LDRjyDQsNC60LjRgtCwINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBBa2l0YSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBBa2l0YSDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LDRjyDQsNC60LjRgtCwINGE0LvQsNGE0YTQuCAyMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBBa2l0YSBmbHVmZnkgMjDigJM0MCBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2EgQWtpdGEgcGVobWVrYXJ2YWxpbmUgMjDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutCw0Y8g0LDQutC40YLQsCDRhNC70LDRhNGE0Lgg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkFtZXJpY2FuIEFraXRhIGZsdWZmeSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBBa2l0YSBwZWhtZWthcnZhbGluZSDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LjQuSDQutC+0LrQtdGALdGB0L/QsNC90LjQtdC70YwgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBDb2NrZXIgU3BhbmllbCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBrb2tlcnNwYW5qZWwgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutC40Lkg0LrQvtC60LXRgC3RgdC/0LDQvdC40LXQu9GMIDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4MH0sImJyZWVkX2VuIjoiQW1lcmljYW4gQ29ja2VyIFNwYW5pZWwgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2Ega29rZXJzcGFuamVsIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0JDQvNC10YDQuNC60LDQvdGB0LrQuNC5INGB0YLQsNGE0YTQvtGA0LTRiNC40YDRgdC60LjQuSDRgtC10YDRjNC10YAgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkFtZXJpY2FuIFN0YWZmb3Jkc2hpcmUgVGVycmllciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBTdGFmZm9yZHNoaXJlIHRlcmplciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LjQuSDRgdGC0LDRhNGE0L7RgNC00YjQuNGA0YHQutC40Lkg0YLQtdGA0YzQtdGAIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1fSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBTdGFmZm9yZHNoaXJlIFRlcnJpZXIgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2EgU3RhZmZvcmRzaGlyZSB0ZXJqZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC90LPQu9C40LnRgdC60LjQuSDQsdGD0LvRjNC00L7QsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NzB9LCJicmVlZF9lbiI6IkVuZ2xpc2ggQnVsbGRvZyIsImJyZWVkX2V0IjoiSW5nbGlzZSBidWxkb2cifSx7ImJyZWVkIjoi0JDQvdCz0LvQuNC50YHQutC40Lkg0LrQvtC60LXRgC3RgdC/0LDQvdC40LXQu9GMIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MH0sImJyZWVkX2VuIjoiRW5nbGlzaCBDb2NrZXIgU3BhbmllbCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJJbmdsaXNlIGtva2Vyc3BhbmplbCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCQ0L3Qs9C70LjQudGB0LrQuNC5INC60L7QutC10YAt0YHQv9Cw0L3QuNC10LvRjCAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODB9LCJicmVlZF9lbiI6IkVuZ2xpc2ggQ29ja2VyIFNwYW5pZWwgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiSW5nbGlzZSBrb2tlcnNwYW5qZWwgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQkNGE0LPQsNC9IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5MH0sImJyZWVkX2VuIjoiQWZnaGFuIEhvdW5kIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkFmZ2FuaXN0YW5pIGtvZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQkNGE0LPQsNC9IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjgwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDB9LCJicmVlZF9lbiI6IkFmZ2hhbiBIb3VuZCAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBZmdhbmlzdGFuaSBrb2VyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JHQsNGB0YHQtdGCLdGF0LDRg9C90LQgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkJhc3NldCBIb3VuZCAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJCYXNzZXRob3VuZCAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCR0LDRgdGB0LXRgi3RhdCw0YPQvdC0IDMw4oCTMzUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1fSwiYnJlZWRfZW4iOiJCYXNzZXQgSG91bmQgMzDigJMzNSBrZyIsImJyZWVkX2V0IjoiQmFzc2V0aG91bmQgMzDigJMzNSBrZyJ9LHsiYnJlZWQiOiLQkdC10YDQvdGB0LrQuNC5INC30LXQvdC90LXQvdGF0YPQvdC0IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IkJlcm5lc2UgTW91bnRhaW4gRG9nIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkJlcm5pIG3DpGdpa29lciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCR0LXRgNC90YHQutC40Lkg0LfQtdC90L3QtdC90YXRg9C90LQg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEzMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQmVybmVzZSBNb3VudGFpbiBEb2cgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiQmVybmkgbcOkZ2lrb2VyIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JHQuNCy0LXRgC3QudC+0YDQuiDQsdC+0LvQtdC1IDMsNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJCaWV3ZXIgWW9ya3NoaXJlIFRlcnJpZXIgb3ZlciAzLDUga2ciLCJicmVlZF9ldCI6IkJpZXdlciBZb3Jrc2hpcmUgVGVycmllciDDvGxlIDMsNSBrZyJ9LHsiYnJlZWQiOiLQkdC40LLQtdGALdC50L7RgNC6INC00L4gMyw1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkJpZXdlciBZb3Jrc2hpcmUgVGVycmllciB1cCB0byAzLDUga2ciLCJicmVlZF9ldCI6IkJpZXdlciBZb3Jrc2hpcmUgVGVycmllciBrdW5pIDMsNSBrZyJ9LHsiYnJlZWQiOiLQkdC40LPQu9GMIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjB9LCJicmVlZF9lbiI6IkJlYWdsZSAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJCaWlnZWwgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQkdC40LPQu9GMIDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjV9LCJicmVlZF9lbiI6IkJlYWdsZSAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiJCaWlnZWwgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQkdC40YjQvtC9LdGE0YDQuNC30LUgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkJpY2hvbiBGcmlzw6kgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJCacWhb24gRnJpc8OpIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQkdC40YjQvtC9LdGE0YDQuNC30LUg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkJpY2hvbiBGcmlzw6kgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiQmnFoW9uIEZyaXPDqSBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JHQvtC60YHQtdGAIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwfSwiYnJlZWRfZW4iOiJCb3hlciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJCb2tzZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQkdC+0LrRgdC10YAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkJveGVyIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkJva3NlciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCR0L7RgNC00LXRgC3QutC+0LvQu9C4IDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjgwfSwiYnJlZWRfZW4iOiJCb3JkZXIgQ29sbGllIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IkJvcmRlcmtvbGwgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQkdC+0YDQtNC10YAt0LrQvtC70LvQuCAyMOKAkzI1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkJvcmRlciBDb2xsaWUgMjDigJMyNSBrZyIsImJyZWVkX2V0IjoiQm9yZGVya29sbCAyMOKAkzI1IGtnIn0seyJicmVlZCI6ItCR0L7RgdGC0L7QvS3RgtC10YDRjNC10YAgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDV9LCJicmVlZF9lbiI6IkJvc3RvbiBUZXJyaWVyIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IkJvc3RvbmkgdGVyamVyIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0JHQvtGB0YLQvtC9LdGC0LXRgNGM0LXRgCA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwfSwiYnJlZWRfZW4iOiJCb3N0b24gVGVycmllciA14oCTMTAga2ciLCJicmVlZF9ldCI6IkJvc3RvbmkgdGVyamVyIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQkdGA0LDQsdCw0L3RgdC+0L0iLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjUwfSwiYnJlZWRfZW4iOiJHcmlmZm9uIEJydXhlbGxvaXMiLCJicmVlZF9ldCI6IkJyw7xzc2VsaSBncmlmb24ifSx7ImJyZWVkIjoi0JHRg9C70YzRgtC10YDRjNC10YAgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTB9LCJicmVlZF9lbiI6IkJ1bGwgVGVycmllciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJCdWxsdGVyamVyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JLQtdC70YzRiC3QutC+0YDQs9C4IDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjV9LCJicmVlZF9lbiI6IldlbHNoIENvcmdpIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IldhbGVzaSBrb3JnaSAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCS0LXQu9GM0Ygt0LrQvtGA0LPQuCAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjc1fSwiYnJlZWRfZW4iOiJXZWxzaCBDb3JnaSAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiJXYWxlc2kga29yZ2kgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQktC10YHRgi3RhdCw0LnQu9C10L3QtC3QstCw0LnRgi3RgtC10YDRjNC10YAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJXZXN0IEhpZ2hsYW5kIFdoaXRlIFRlcnJpZXIiLCJicmVlZF9ldCI6IkzDpMOkbmUtxaBvdGltYWEgdmFsZ2UgdGVyamVyIn0seyJicmVlZCI6ItCS0L7RgdGC0L7Rh9C90L7RgdC40LHQuNGA0YHQutCw0Y8g0LvQsNC50LrQsCAxOOKAkzI1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjg1fSwiYnJlZWRfZW4iOiJFYXN0IFNpYmVyaWFuIExhaWthIDE44oCTMjUga2ciLCJicmVlZF9ldCI6IklkYS1TaWJlcmkgbGFpa2EgMTjigJMyNSBrZyJ9LHsiYnJlZWQiOiLQktC+0YHRgtC+0YfQvdC+0YHQuNCx0LjRgNGB0LrQsNGPINC70LDQudC60LAg0LHQvtC70LXQtSAyNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjU1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IkVhc3QgU2liZXJpYW4gTGFpa2Egb3ZlciAyNSBrZyIsImJyZWVkX2V0IjoiSWRhLVNpYmVyaSBsYWlrYSDDvGxlIDI1IGtnIn0seyJicmVlZCI6ItCT0L7Qu9C00LXQvS3RgNC10YLRgNC40LLQtdGAIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiR29sZGVuIFJldHJpZXZlciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJLdWxkbmUgcmV0cmlpdmVyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JPQvtC70LTQtdC9LdGA0LXRgtGA0LjQstC10YAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjExMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiR29sZGVuIFJldHJpZXZlciAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJLdWxkbmUgcmV0cmlpdmVyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JPRgNC40YTRhNC+0L0iLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJHcmlmZm9uIiwiYnJlZWRfZXQiOiJHcmlmb24ifSx7ImJyZWVkIjoi0JTQsNC70LzQsNGC0LjQvSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NzB9LCJicmVlZF9lbiI6IkRhbG1hdGlhbiIsImJyZWVkX2V0IjoiRGFsbWFhdHNpYSBrb2VyIn0seyJicmVlZCI6ItCU0LbQtdC6LdGA0LDRgdGB0LXQuy3RgtC10YDRjNC10YAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NTB9LCJicmVlZF9lbiI6IkphY2sgUnVzc2VsbCBUZXJyaWVyIHNtb290aCIsImJyZWVkX2V0IjoiSmFjayBSdXNzZWxsaSB0ZXJqZXIgbMO8aGlrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0JTQttC10Lot0YDQsNGB0YHQtdC7LdGC0LXRgNGM0LXRgCDQttC10YHRgtC60L7RiNC10YDRgdGC0L3Ri9C5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiSmFjayBSdXNzZWxsIFRlcnJpZXIgd2lyZS1oYWlyZWQiLCJicmVlZF9ldCI6IkphY2sgUnVzc2VsbGkgdGVyamVyIGthcnVrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0JTQvtCx0LXRgNC80LDQvSAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjgwfSwiYnJlZWRfZW4iOiJEb2Jlcm1hbm4gMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiRG9iZXJtYW5uIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JTQvtCx0LXRgNC80LDQvSDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjk1fSwiYnJlZWRfZW4iOiJEb2Jlcm1hbm4gb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiRG9iZXJtYW5uIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JfQsNC/0LDQtNC90L7RgdC40LHQuNGA0YHQutCw0Y8g0LvQsNC50LrQsCAxOOKAkzI1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjg1fSwiYnJlZWRfZW4iOiJXZXN0IFNpYmVyaWFuIExhaWthIDE44oCTMjUga2ciLCJicmVlZF9ldCI6IkzDpMOkbmUtU2liZXJpIGxhaWthIDE44oCTMjUga2cifSx7ImJyZWVkIjoi0JfQvtC70L7RgtC40YHRgtGL0Lkg0YDQtdGC0YDQuNCy0LXRgCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiR29sZGVuIFJldHJpZXZlciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJLdWxkbmUgcmV0cmlpdmVyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JfQvtC70L7RgtC40YHRgtGL0Lkg0YDQtdGC0YDQuNCy0LXRgCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTEwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTEwfSwiYnJlZWRfZW4iOiJHb2xkZW4gUmV0cmlldmVyIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6Ikt1bGRuZSByZXRyaWl2ZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQmNGA0LvQsNC90LTRgdC60LjQuSDQvNGP0LPQutC+0YjQtdGA0YHRgtC90YvQuSDQv9GI0LXQvdC40YfQvdGL0Lkg0YLQtdGA0YzQtdGAIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwfSwiYnJlZWRfZW4iOiJJcmlzaCBTb2Z0IENvYXRlZCBXaGVhdGVuIFRlcnJpZXIiLCJicmVlZF9ldCI6IklpcmkgcGVobWVrYXJ2YW5lIG5pc3V2w6RydmkgdGVyamVyIn0seyJicmVlZCI6ItCY0YDQu9Cw0L3QtNGB0LrQuNC5INGC0LXRgNGM0LXRgCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MCwi0KLRgNC40LzQvNC40L3QsyI6NzV9LCJicmVlZF9lbiI6IklyaXNoIFRlcnJpZXIiLCJicmVlZF9ldCI6IklpcmkgdGVyamVyIn0seyJicmVlZCI6ItCY0YHQv9Cw0L3RgdC60LjQuSDQs9Cw0LvRjNCz0L4gMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo3MH0sImJyZWVkX2VuIjoiU3BhbmlzaCBHYWxnbyAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJIaXNwYWFuaWEgZ2FsZ28gMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQmNGB0L/QsNC90YHQutC40Lkg0LPQsNC70YzQs9C+IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6ODB9LCJicmVlZF9lbiI6IlNwYW5pc2ggR2FsZ28gMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiSGlzcGFhbmlhIGdhbGdvIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JnQvtGA0LrRiNC40YDRgdC60LjQuSDRgtC10YDRjNC10YAg0LHQvtC70LXQtSAzLDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiWW9ya3NoaXJlIFRlcnJpZXIgb3ZlciAzLDUga2ciLCJicmVlZF9ldCI6IllvcmtzaGlyZSB0ZXJqZXIgw7xsZSAzLDUga2cifSx7ImJyZWVkIjoi0JnQvtGA0LrRiNC40YDRgdC60LjQuSDRgtC10YDRjNC10YAg0LTQviAzLDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiWW9ya3NoaXJlIFRlcnJpZXIgdXAgdG8gMyw1IGtnIiwiYnJlZWRfZXQiOiJZb3Jrc2hpcmUgdGVyamVyIGt1bmkgMyw1IGtnIn0seyJicmVlZCI6ItCa0LDQstCw0LvQtdGALdC60LjQvdCzLdGH0LDRgNC70YzQty3RgdC/0LDQvdC40LXQu9GMIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MH0sImJyZWVkX2VuIjoiQ2F2YWxpZXIgS2luZyBDaGFybGVzIFNwYW5pZWwgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiQ2F2YWxpZXIgS2luZyBDaGFybGVzIFNwYW5pZWwgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQmtCw0LLQsNC70LXRgC3QutC40L3Qsy3Rh9Cw0YDQu9GM0Lct0YHQv9Cw0L3QuNC10LvRjCA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiQ2F2YWxpZXIgS2luZyBDaGFybGVzIFNwYW5pZWwgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJDYXZhbGllciBLaW5nIENoYXJsZXMgU3BhbmllbCA14oCTMTAga2cifSx7ImJyZWVkIjoi0JrQsNC90LUt0LrQvtGA0YHQviA0MOKAkzYwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NX0sImJyZWVkX2VuIjoiQ2FuZSBDb3JzbyA0MOKAkzYwIGtnIiwiYnJlZWRfZXQiOiJDYW5lIENvcnNvIDQw4oCTNjAga2cifSx7ImJyZWVkIjoi0JrQsNC90LUt0LrQvtGA0YHQviDQsdC+0LvQtdC1IDYwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6OTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjoxMDV9LCJicmVlZF9lbiI6IkNhbmUgQ29yc28gb3ZlciA2MCBrZyIsImJyZWVkX2V0IjoiQ2FuZSBDb3JzbyDDvGxlIDYwIGtnIn0seyJicmVlZCI6ItCa0LDRgNC10LvQvi3RhNC40L3RgdC60LDRjyDQu9Cw0LnQutCwINC00L4gMTMg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjV9LCJicmVlZF9lbiI6IkthcmVsaWFuLUZpbm5pc2ggTGFpa2EgdXAgdG8gMTMga2ciLCJicmVlZF9ldCI6IkthcmphbGEtU29vbWUgbGFpa2Ega3VuaSAxMyBrZyJ9LHsiYnJlZWQiOiLQmtC40YLQsNC50YHQutCw0Y8g0YXQvtGF0LvQsNGC0LDRjyDQs9C+0LvQsNGPIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMyLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDIsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJDaGluZXNlIENyZXN0ZWQgaGFpcmxlc3MgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJIaWluYSBoYXJqYWtvZXIga2FydmF0dSA14oCTMTAga2cifSx7ImJyZWVkIjoi0JrQuNGC0LDQudGB0LrQsNGPINGF0L7RhdC70LDRgtCw0Y8g0LPQvtC70LDRjyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyOCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiQ2hpbmVzZSBDcmVzdGVkIGhhaXJsZXNzIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IkhpaW5hIGhhcmpha29lciBrYXJ2YXR1IGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQmtC40YLQsNC50YHQutCw0Y8g0YXQvtGF0LvQsNGC0LDRjyDQv9GD0YXQvtCy0LDRjyA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiQ2hpbmVzZSBDcmVzdGVkIHBvd2RlcnB1ZmYgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJIaWluYSBoYXJqYWtvZXIgUG93ZGVycHVmZiA14oCTMTAga2cifSx7ImJyZWVkIjoi0JrQuNGC0LDQudGB0LrQsNGPINGF0L7RhdC70LDRgtCw0Y8g0L/Rg9GF0L7QstCw0Y8g0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkNoaW5lc2UgQ3Jlc3RlZCBwb3dkZXJwdWZmIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IkhpaW5hIGhhcmpha29lciBQb3dkZXJwdWZmIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQmtC+0LrQsNC/0YMgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzV9LCJicmVlZF9lbiI6IkNvY2thcG9vIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiQ29ja2Fwb28gNeKAkzEwIGtnIn0seyJicmVlZCI6ItCa0L7QutCw0L/RgyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2NX0sImJyZWVkX2VuIjoiQ29ja2Fwb28gdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiQ29ja2Fwb28ga3VuaSA1IGtnIn0seyJicmVlZCI6ItCa0L7Qu9C70LggMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJDb2xsaWUgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiS29sbCAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCa0L7Qu9C70LggMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjExMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiQ29sbGllIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IktvbGwgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQmtC+0LzQvtC90LTQvtGAIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMTB9LCJicmVlZF9lbiI6IktvbW9uZG9yIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IktvbW9uZG9yIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JrQvtC80L7QvdC00L7RgCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTMwfSwiYnJlZWRfZW4iOiJLb21vbmRvciBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJLb21vbmRvciDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNC+0YAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjcwfSwiYnJlZWRfZW4iOiJMYWJyYWRvciBSZXRyaWV2ZXIgc21vb3RoIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9yaSByZXRyaWl2ZXIgbMO8aGlrYXJ2YWxpbmUgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQm9Cw0LHRgNCw0LTQvtGAINCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdGL0LkgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjU1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4MH0sImJyZWVkX2VuIjoiTGFicmFkb3IgUmV0cmlldmVyIHNtb290aCAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvcmkgcmV0cmlpdmVyIGzDvGhpa2FydmFsaW5lIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00L7RgCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3Ri9C5INCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjgwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTV9LCJicmVlZF9lbiI6IkxhYnJhZG9yIFJldHJpZXZlciBzbW9vdGggb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiTGFicmFkb3JpIHJldHJpaXZlciBsw7xoaWthcnZhbGluZSDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNC+0YAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90YvQuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkxhYnJhZG9yIFJldHJpZXZlciBsb25nLWNvYXRlZCAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvcmkgcmV0cmlpdmVyIHBpa2thcnZhbGluZSAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNC+0YAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90YvQuSAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTEwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJMYWJyYWRvciBSZXRyaWV2ZXIgbG9uZy1jb2F0ZWQgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiTGFicmFkb3JpIHJldHJpaXZlciBwaWtrYXJ2YWxpbmUgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQm9Cw0LHRgNCw0LTQvtGAINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdGL0Lkg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEzMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiTGFicmFkb3IgUmV0cmlldmVyIGxvbmctY29hdGVkIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9yaSByZXRyaWl2ZXIgcGlra2FydmFsaW5lIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00YPQtNC10LvRjCAxMOKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IkxhYnJhZG9vZGxlIDEw4oCTMjAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9vZGxlIDEw4oCTMjAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00YPQtNC10LvRjCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTB9LCJicmVlZF9lbiI6IkxhYnJhZG9vZGxlIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9vZGxlIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00YPQtNC10LvRjCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwfSwiYnJlZWRfZW4iOiJMYWJyYWRvb2RsZSAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvb2RsZSAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCb0LXQstGA0LXRgtC60LAgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MH0sImJyZWVkX2VuIjoiSXRhbGlhbiBHcmV5aG91bmQgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJJdGFhbGlhIHZpbmRrb2VyIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQm9C10LLRgNC10YLQutCwINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjI1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MzV9LCJicmVlZF9lbiI6Ikl0YWxpYW4gR3JleWhvdW5kIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6Ikl0YWFsaWEgdmluZGtvZXIga3VuaSA1IGtnIn0seyJicmVlZCI6ItCb0YXQsNGB0YHQutC40Lkg0LDQv9GB0L4gNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzV9LCJicmVlZF9lbiI6IkxoYXNhIEFwc28gNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJMaGFzYSBBcHNvIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQm9GF0LDRgdGB0LrQuNC5INCw0L/RgdC+INC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjY1fSwiYnJlZWRfZW4iOiJMaGFzYSBBcHNvIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IkxoYXNhIEFwc28ga3VuaSA1IGtnIn0seyJicmVlZCI6ItCc0LDQu9GM0YLQtdC30LUiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6Ik1hbHRlc2UiLCJicmVlZF9ldCI6Ik1hbHRhIGJvbG9uZWVzIn0seyJicmVlZCI6ItCc0LDQu9GM0YLQuNC50YHQutCw0Y8g0LHQvtC70L7QvdC60LAgNeKAkzgg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2NX0sImJyZWVkX2VuIjoiTWFsdGVzZSBCb2xvZ25lc2UgNeKAkzgga2ciLCJicmVlZF9ldCI6Ik1hbHRhIGJvbG9uZWVzIDXigJM4IGtnIn0seyJicmVlZCI6ItCc0LDQu9GM0YLQuNC50YHQutCw0Y8g0LHQvtC70L7QvdC60LAg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6Ik1hbHRlc2UgQm9sb2duZXNlIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6Ik1hbHRhIGJvbG9uZWVzIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQnNCw0LvRjNGC0LjQv9GDIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MH0sImJyZWVkX2VuIjoiTWFsdGlwb28gMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiTWFsdGlwdXUgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQnNCw0LvRjNGC0LjQv9GDIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJNYWx0aXBvbyA14oCTMTAga2ciLCJicmVlZF9ldCI6Ik1hbHRpcHV1IDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQnNCw0LvRjNGC0LjQv9GDINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJNYWx0aXBvbyB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJNYWx0aXB1dSBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JzQtdGC0LjRgSDQutGA0YPQv9C90YvQuSAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJNaXhlZCBicmVlZCBsYXJnZSAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJTZWdhdmVyZCBzdXVyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JzQtdGC0LjRgSDQutGA0YPQv9C90YvQuSDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTIwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTIwfSwiYnJlZWRfZW4iOiJNaXhlZCBicmVlZCBsYXJnZSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJTZWdhdmVyZCBzdXVyIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JzQtdGC0LjRgSDQvNC10LvQutC40LkgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6Ik1peGVkIGJyZWVkIHNtYWxsIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiU2VnYXZlcmQgdsOkaWtlIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQnNC10YLQuNGBINC80LXQu9C60LjQuSDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1MH0sImJyZWVkX2VuIjoiTWl4ZWQgYnJlZWQgc21hbGwgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiU2VnYXZlcmQgdsOkaWtlIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQnNC10YLQuNGBINGB0YDQtdC00L3QuNC5IDEw4oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjcwfSwiYnJlZWRfZW4iOiJNaXhlZCBicmVlZCBtZWRpdW0gMTDigJMyMCBrZyIsImJyZWVkX2V0IjoiU2VnYXZlcmQga2Vza21pbmUgMTDigJMyMCBrZyJ9LHsiYnJlZWQiOiLQnNC10YLQuNGBINGB0YDQtdC00L3QuNC5IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjYwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjg1fSwiYnJlZWRfZW4iOiJNaXhlZCBicmVlZCBtZWRpdW0gMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiU2VnYXZlcmQga2Vza21pbmUgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQnNC40YLRgtC10LvRjNGI0L3QsNGD0YbQtdGAIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MCwi0KLRgNC40LzQvNC40L3QsyI6NzV9LCJicmVlZF9lbiI6IlN0YW5kYXJkIFNjaG5hdXplciAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJTdGFuZGFyZMWhbmF1dHNlciAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCc0LjRgtGC0LXQu9GM0YjQvdCw0YPRhtC10YAgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwLCLQotGA0LjQvNC80LjQvdCzIjo4NX0sImJyZWVkX2VuIjoiU3RhbmRhcmQgU2NobmF1emVyIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IlN0YW5kYXJkxaFuYXV0c2VyIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0JzQvtC/0YEiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjUwfSwiYnJlZWRfZW4iOiJQdWciLCJicmVlZF9ldCI6Ik1vcHMifSx7ImJyZWVkIjoi0J3QtdCy0YHQutCw0Y8g0L7RgNGF0LjQtNC10Y8iLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6Ik5ldmEgT3JjaGlkIiwiYnJlZWRfZXQiOiJOZWV2YSBvcmhpZGVlIn0seyJicmVlZCI6ItCd0LXQvNC10YbQutCw0Y8g0L7QstGH0LDRgNC60LAgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiR2VybWFuIFNoZXBoZXJkIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IlNha3NhIGxhbWJha29lciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCd0LXQvNC10YbQutCw0Y8g0L7QstGH0LDRgNC60LAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6Ikdlcm1hbiBTaGVwaGVyZCAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJTYWtzYSBsYW1iYWtvZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQndC10LzQtdGG0LrQsNGPINC+0LLRh9Cw0YDQutCwINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJHZXJtYW4gU2hlcGhlcmQgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiU2Frc2EgbGFtYmFrb2VyIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0KjQstC10LnRhtCw0YDRgdC60LDRjyDQvtCy0YfQsNGA0LrQsCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJTd2lzcyBTaGVwaGVyZCAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiLFoHZlaXRzaSBsYW1iYWtvZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQqNCy0LXQudGG0LDRgNGB0LrQsNGPINC+0LLRh9Cw0YDQutCwIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJTd2lzcyBTaGVwaGVyZCAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiLFoHZlaXRzaSBsYW1iYWtvZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQqNCy0LXQudGG0LDRgNGB0LrQsNGPINC+0LLRh9Cw0YDQutCwINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJTd2lzcyBTaGVwaGVyZCBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiLFoHZlaXRzaSBsYW1iYWtvZXIgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQndC+0YDQstC40Yct0YLQtdGA0YzQtdGAIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiTm9yd2ljaCBUZXJyaWVyIiwiYnJlZWRfZXQiOiJOb3J3aXTFoWkgdGVyamVyIn0seyJicmVlZCI6ItCd0L7RgNGE0L7Qu9C6LdGC0LXRgNGM0LXRgCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MCwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6Ik5vcmZvbGsgVGVycmllciIsImJyZWVkX2V0IjoiTm9yZm9sa2kgdGVyamVyIn0seyJicmVlZCI6ItCd0YzRjtGE0LDRg9C90LTQu9C10L3QtCA0MOKAkzYwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTMwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJOZXdmb3VuZGxhbmQgNDDigJM2MCBrZyIsImJyZWVkX2V0IjoiTmV3Zm91bmRsYW5kaSBrb2VyIDQw4oCTNjAga2cifSx7ImJyZWVkIjoi0J3RjNGO0YTQsNGD0L3QtNC70LXQvdC0INCx0L7Qu9C10LUgNjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoxMDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjoxMTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjE1MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEzMH0sImJyZWVkX2VuIjoiTmV3Zm91bmRsYW5kIG92ZXIgNjAga2ciLCJicmVlZF9ldCI6Ik5ld2ZvdW5kbGFuZGkga29lciDDvGxlIDYwIGtnIn0seyJicmVlZCI6ItCf0LDQv9C40LnQvtC9Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJQYXBpbGxvbiIsImJyZWVkX2V0IjoiUGFwaWxsb24ifSx7ImJyZWVkIjoi0J/QtdC60LjQvdC10YEgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IlBla2luZ2VzZSA14oCTMTAga2ciLCJicmVlZF9ldCI6IlBla2luZXNpIGtvZXIgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCf0LXQutC40L3QtdGBINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJQZWtpbmdlc2UgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiUGVraW5lc2kga29lciBrdW5pIDUga2cifSx7ImJyZWVkIjoi0J/Rg9C00LXQu9GMINCx0L7Qu9GM0YjQvtC5IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5MH0sImJyZWVkX2VuIjoiU3RhbmRhcmQgUG9vZGxlIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IlN0YW5kYXJkcHV1ZGVsIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0J/Rg9C00LXQu9GMINCx0L7Qu9GM0YjQvtC5IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjgwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDB9LCJicmVlZF9lbiI6IlN0YW5kYXJkIFBvb2RsZSAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJTdGFuZGFyZHB1dWRlbCAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCf0YPQtNC10LvRjCDQutCw0YDQu9C40LrQvtCy0YvQuSA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiTWluaWF0dXJlIFBvb2RsZSA14oCTMTAga2ciLCJicmVlZF9ldCI6IkvDpMOkYnVzcHV1ZGVsIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQn9GD0LTQtdC70Ywg0LzQsNC70YvQuSAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IlNtYWxsIFBvb2RsZSAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJWw6Rpa2UgcHV1ZGVsIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0J/Rg9C00LXQu9GMINC80LDQu9GL0LkgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwfSwiYnJlZWRfZW4iOiJTbWFsbCBQb29kbGUgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiVsOkaWtlIHB1dWRlbCAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCf0YPQtNC10LvRjCDRgtC+0Lkg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IlRveSBQb29kbGUgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiTcOkbmd1YXNqYSBwdXVkZWwga3VuaSA1IGtnIn0seyJicmVlZCI6ItCg0LjQt9C10L3RiNC90LDRg9GG0LXRgCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwLCLQotGA0LjQvNC80LjQvdCzIjoxMTB9LCJicmVlZF9lbiI6IkdpYW50IFNjaG5hdXplciAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJTdXVyxaFuYXV0c2VyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0KDQuNC30LXQvdGI0L3QsNGD0YbQtdGAINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMjAsItCi0YDQuNC80LzQuNC90LMiOjEyNX0sImJyZWVkX2VuIjoiR2lhbnQgU2NobmF1emVyIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IlN1dXLFoW5hdXRzZXIgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQoNGD0YHRgdC60LDRjyDRhtCy0LXRgtC90LDRjyDQsdC+0LvQvtC90LrQsCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiUnVzc2lhbiBDb2xvcmVkIExhcGRvZyIsImJyZWVkX2V0IjoiVmVuZSB2w6RydmlsaW5lIHPDvGxla29lciJ9LHsiYnJlZWQiOiLQoNGD0YHRgdC60LjQuSDQvtGF0L7RgtC90LjRh9C40Lkg0YHQv9Cw0L3QuNC10LvRjCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IlJ1c3NpYW4gU3BhbmllbCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJWZW5lIGphaGlzcGFuamVsIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KDRg9GB0YHQutC40Lkg0L7RhdC+0YLQvdC40YfQuNC5INGB0L/QsNC90LjQtdC70YwgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwfSwiYnJlZWRfZW4iOiJSdXNzaWFuIFNwYW5pZWwgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiVmVuZSBqYWhpc3BhbmplbCAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCg0YPRgdGB0LrQuNC5INGC0L7QuSDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3Ri9C5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjI1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MzV9LCJicmVlZF9lbiI6IlJ1c3NpYW4gVG95IHNtb290aCIsImJyZWVkX2V0IjoiVmVuZSBUb3kgbMO8aGlrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0KDRg9GB0YHQutC40Lkg0YLQvtC5INC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdGL0LkiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IlJ1c3NpYW4gVG95IGxvbmctY29hdGVkIiwiYnJlZWRfZXQiOiJWZW5lIFRveSBwaWtrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0KDRg9GB0YHQutC40Lkg0YfQtdGA0L3Ri9C5INGC0LXRgNGM0LXRgCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwfSwiYnJlZWRfZW4iOiJCbGFjayBSdXNzaWFuIFRlcnJpZXIgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiTXVzdCBWZW5lIHRlcmplciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCg0YPRgdGB0LrQuNC5INGH0LXRgNC90YvQuSDRgtC10YDRjNC10YAg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjc1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEyMH0sImJyZWVkX2VuIjoiQmxhY2sgUnVzc2lhbiBUZXJyaWVyIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6Ik11c3QgVmVuZSB0ZXJqZXIgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQoNGD0YHRgdC60L4t0LXQstGA0L7Qv9C10LnRgdC60LDRjyDQu9Cw0LnQutCwIDIw4oCTMjgg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IlJ1c3NpYW4tRXVyb3BlYW4gTGFpa2EgMjDigJMyOCBrZyIsImJyZWVkX2V0IjoiVmVuZS1FdXJvb3BhIGxhaWthIDIw4oCTMjgga2cifSx7ImJyZWVkIjoi0KHQsNC80L7QtdC0IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IlNhbW95ZWQgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiU2Ftb2plZWQgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQodCw0LzQvtC10LQgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IlNhbW95ZWQgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiU2Ftb2plZWQgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQodC10YLRgtC10YAg0LDQvdCz0LvQuNC50YHQutC40LkgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwfSwiYnJlZWRfZW4iOiJFbmdsaXNoIFNldHRlciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJJbmdsaXNlIHNldHRlciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCh0LXRgtGC0LXRgCDQs9C+0YDQtNC+0L0gMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMH0sImJyZWVkX2VuIjoiR29yZG9uIFNldHRlciAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJHb3Jkb25pIHNldHRlciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCh0LXRgtGC0LXRgCDQuNGA0LvQsNC90LTRgdC60LjQuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTB9LCJicmVlZF9lbiI6IklyaXNoIFNldHRlciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJJaXJpIHNldHRlciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCh0LjQsdCwLdC40L3RgyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjYwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NzB9LCJicmVlZF9lbiI6IlNoaWJhIEludSIsImJyZWVkX2V0IjoiU2hpYmEgSW51In0seyJicmVlZCI6ItCh0LjQu9C40YXQtdC8LdGC0LXRgNGM0LXRgCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MCwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6IlNlYWx5aGFtIFRlcnJpZXIiLCJicmVlZF9ldCI6IlNlYWx5aGFtaSB0ZXJqZXIifSx7ImJyZWVkIjoi0KHQutC+0YLRhy3RgtC10YDRjNC10YAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJTY290dGlzaCBUZXJyaWVyIiwiYnJlZWRfZXQiOiLFoG90aSB0ZXJqZXIifSx7ImJyZWVkIjoi0KLQsNC60YHQsCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3QsNGPINC60LDRgNC70LjQutC+0LLQsNGPIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo1MH0sImJyZWVkX2VuIjoiRGFjaHNodW5kIHNtb290aCBtaW5pYXR1cmUgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIgbMO8aGlrYXJ2YWxpbmUga8Okw6RidXMgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90LDRjyDQutGA0L7Qu9C40YfRjNGPINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjI1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo0NX0sImJyZWVkX2VuIjoiRGFjaHNodW5kIHNtb290aCByYWJiaXQgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiVGFrc2lrb2VyIGzDvGhpa2FydmFsaW5lIGvDvMO8bGlrIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdCw0Y8g0YHRgtCw0L3QtNCw0YDRgtC90LDRjyAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjYwfSwiYnJlZWRfZW4iOiJEYWNoc2h1bmQgc21vb3RoIHN0YW5kYXJkIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBsw7xoaWthcnZhbGluZSBzdGFuZGFyZCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90LDRjyDQutCw0YDQu9C40LrQvtCy0LDRjyA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiRGFjaHNodW5kIGxvbmctY29hdGVkIG1pbmlhdHVyZSA14oCTMTAga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBwaWtrYXJ2YWxpbmUga8Okw6RidXMgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90LDRjyDQutGA0L7Qu9C40YfRjNGPINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJEYWNoc2h1bmQgbG9uZy1jb2F0ZWQgcmFiYml0IHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBwaWtrYXJ2YWxpbmUga8O8w7xsaWsga3VuaSA1IGtnIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90LDRjyDRgdGC0LDQvdC00LDRgNGC0L3QsNGPIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MH0sImJyZWVkX2VuIjoiRGFjaHNodW5kIGxvbmctY29hdGVkIHN0YW5kYXJkIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBwaWtrYXJ2YWxpbmUgc3RhbmRhcmQgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINC20LXRgdGC0LrQvtGI0LXRgNGB0YLQvdCw0Y8g0LrQsNGA0LvQuNC60L7QstCw0Y8gNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJEYWNoc2h1bmQgd2lyZS1oYWlyZWQgbWluaWF0dXJlIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiVGFrc2lrb2VyIGthcnVrYXJ2YWxpbmUga8Okw6RidXMgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90LDRjyDQutGA0L7Qu9C40YfRjNGPINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1LCLQotGA0LjQvNC80LjQvdCzIjo1NX0sImJyZWVkX2VuIjoiRGFjaHNodW5kIHdpcmUtaGFpcmVkIHJhYmJpdCB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIga2FydWthcnZhbGluZSBrw7zDvGxpayBrdW5pIDUga2cifSx7ImJyZWVkIjoi0KLQsNC60YHQsCDQttC10YHRgtC60L7RiNC10YDRgdGC0L3QsNGPINGB0YLQsNC90LTQsNGA0YLQvdCw0Y8gMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwLCLQotGA0LjQvNC80LjQvdCzIjo3NX0sImJyZWVkX2VuIjoiRGFjaHNodW5kIHdpcmUtaGFpcmVkIHN0YW5kYXJkIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBrYXJ1a2FydmFsaW5lIHN0YW5kYXJkIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KPQuNC/0L/QtdGCIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1fSwiYnJlZWRfZW4iOiJXaGlwcGV0IDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IldoaXBwZXQgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQo9C40L/Qv9C10YIgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTB9LCJicmVlZF9lbiI6IldoaXBwZXQgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiV2hpcHBldCAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCk0LjQvdGB0LrQuNC5INC70LDQv9GF0YPQvdC0IDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4NX0sImJyZWVkX2VuIjoiRmlubmlzaCBMYXBwaHVuZCAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiJTb29tZSBsYW1iYWtvZXIgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQpNC40L3RgdC60LjQuSDQu9Cw0L/RhdGD0L3QtCAyMOKAkzI0INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTB9LCJicmVlZF9lbiI6IkZpbm5pc2ggTGFwcGh1bmQgMjDigJMyNCBrZyIsImJyZWVkX2V0IjoiU29vbWUgbGFtYmFrb2VyIDIw4oCTMjQga2cifSx7ImJyZWVkIjoi0KTQvtC60YHRgtC10YDRjNC10YAg0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90YvQuSAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzAsItCi0YDQuNC80LzQuNC90LMiOjc1fSwiYnJlZWRfZW4iOiJXaXJlIEZveCBUZXJyaWVyIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IkthcnVrYXJ2YWxpbmUgZm94dGVyamVyIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KTQvtC60YHRgtC10YDRjNC10YAg0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90YvQuSA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MCwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6IldpcmUgRm94IFRlcnJpZXIgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJLYXJ1a2FydmFsaW5lIGZveHRlcmplciA14oCTMTAga2cifSx7ImJyZWVkIjoi0KTRgNCw0L3RhtGD0LfRgdC60LjQuSDQsdGD0LvRjNC00L7QsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjB9LCJicmVlZF9lbiI6IkZyZW5jaCBCdWxsZG9nIiwiYnJlZWRfZXQiOiJQcmFudHN1c2UgYnVsZG9nIn0seyJicmVlZCI6ItCl0LDRgdC60LggMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiU2liZXJpYW4gSHVza3kgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiU2liZXJpIGh1c2t5IDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0KXQsNGB0LrQuCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiU2liZXJpYW4gSHVza3kgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiU2liZXJpIGh1c2t5IDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0KbQstC10YDQs9GI0L3QsNGD0YbQtdGAIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MCwi0KLRgNC40LzQvNC40L3QsyI6NzV9LCJicmVlZF9lbiI6Ik1pbmlhdHVyZSBTY2huYXV6ZXIgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiS8Okw6RidXPFoW5hdXRzZXIgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQptCy0LXRgNCz0YjQvdCw0YPRhtC10YAgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJNaW5pYXR1cmUgU2NobmF1emVyIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiS8Okw6RidXPFoW5hdXRzZXIgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCn0LDRgy3Rh9Cw0YMgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJDaG93IENob3cgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiQ2hvdyBDaG93IDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0KfQsNGDLdGH0LDRgyAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTEwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJDaG93IENob3cgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiQ2hvdyBDaG93IDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0KfQuNGF0YPQsNGF0YPQsCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3Ri9C5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjI1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MzV9LCJicmVlZF9lbiI6IkNoaWh1YWh1YSBzbW9vdGgiLCJicmVlZF9ldCI6IlTFoWlodWFodWEgbMO8aGlrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0KfQuNGF0YPQsNGF0YPQsCDQtNC70LjQvdC90L7RiNC10YDRgdGC0L3Ri9C5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJDaGlodWFodWEgbG9uZy1jb2F0ZWQiLCJicmVlZF9ldCI6IlTFoWlodWFodWEgcGlra2FydmFsaW5lIn0seyJicmVlZCI6ItCo0LDRgNC/0LXQuSAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjY1fSwiYnJlZWRfZW4iOiJTaGFyIFBlaSAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiLFoGFyLVBlaSAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCo0LDRgNC/0LXQuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjcwfSwiYnJlZWRfZW4iOiJTaGFyIFBlaSAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiLFoGFyLVBlaSAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCo0LXQu9GC0LgiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2MH0sImJyZWVkX2VuIjoiU2hldGxhbmQgU2hlZXBkb2ciLCJicmVlZF9ldCI6IsWgZXRsYW5kaSBsYW1iYWtvZXIifSx7ImJyZWVkIjoi0KjQuC3RgtGG0YMgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IlNoaWggVHp1IDXigJMxMCBrZyIsImJyZWVkX2V0IjoiU2hpaCBUenUgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCo0Lgt0YLRhtGDINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJTaGloIFR6dSB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJTaGloIFR6dSBrdW5pIDUga2cifSx7ImJyZWVkIjoi0KjQvdCw0YPRhtC10YAg0LzQuNC90LjQsNGC0Y7RgNC90YvQuSDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiTWluaWF0dXJlIFNjaG5hdXplciB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJLw6TDpGJ1c8WhbmF1dHNlciBrdW5pIDUga2cifSx7ImJyZWVkIjoi0KjQv9C40YYg0L3QtdC80LXRhtC60LjQuSAvINC/0L7QvNC10YDQsNC90YHQutC40Lkg0LHQvtC70LXQtSAzLDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjYwfSwiYnJlZWRfZW4iOiJHZXJtYW4gU3BpdHogLyBQb21lcmFuaWFuIG92ZXIgMyw1IGtnIiwiYnJlZWRfZXQiOiJTYWtzYSBzcGl0cyAvIFBvbWVyYW5pYW4gw7xsZSAzLDUga2cifSx7ImJyZWVkIjoi0KjQv9C40YYg0L3QtdC80LXRhtC60LjQuSAvINC/0L7QvNC10YDQsNC90YHQutC40Lkg0LTQviAzLDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjU1fSwiYnJlZWRfZW4iOiJHZXJtYW4gU3BpdHogLyBQb21lcmFuaWFuIHVwIHRvIDMsNSBrZyIsImJyZWVkX2V0IjoiU2Frc2Egc3BpdHMgLyBQb21lcmFuaWFuIGt1bmkgMyw1IGtnIn0seyJicmVlZCI6ItCo0L/QuNGGINGP0L/QvtC90YHQutC40LkiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2MH0sImJyZWVkX2VuIjoiSmFwYW5lc2UgU3BpdHoiLCJicmVlZF9ldCI6IkphYXBhbmkgc3BpdHMifSx7ImJyZWVkIjoi0KnQtdC90LrQuCIsInNlcnZpY2VzIjp7ItCS0YHRjyDQv9GA0L7Qs9GA0LDQvNC80LAiOjU1fSwiYnJlZWRfZW4iOiJQdXBwaWVzIiwiYnJlZWRfZXQiOiJLdXRzaWthZCJ9LHsiYnJlZWQiOiLQrdGB0YLQvtC90YHQutCw0Y8g0LPQvtC90YfQsNGPIDE14oCTMjUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwfSwiYnJlZWRfZW4iOiJFc3RvbmlhbiBIb3VuZCAxNeKAkzI1IGtnIiwiYnJlZWRfZXQiOiJFZXN0aSBoYWdpamFzIDE14oCTMjUga2cifSx7ImJyZWVkIjoi0K/Qv9C+0L3RgdC60LjQuSDRhdC40L0iLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkphcGFuZXNlIENoaW4iLCJicmVlZF9ldCI6IkphYXBhbmkgQ2hpbiJ9LHsiYnJlZWQiOiLQmtC+0YjQutCwINC60L7RgNC+0YLQutC+0YjQtdGA0YHRgtC90LDRjyIsInNlcnZpY2VzIjp7ItCS0YvRh9C10YEiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiQ2F0IHNob3J0LWhhaXJlZCIsImJyZWVkX2V0IjoiS2FzcyBsw7xoaWthcnZhbGluZSJ9LHsiYnJlZWQiOiLQmtC+0YjQutCwINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdCw0Y8iLCJzZXJ2aWNlcyI6eyLQktGL0YfQtdGBIjo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODB9LCJicmVlZF9lbiI6IkNhdCBsb25nLWhhaXJlZCIsImJyZWVkX2V0IjoiS2FzcyBwaWtrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0JrQvtGI0LrQsCDQnNC10LnQvS3QutGD0L0iLCJzZXJ2aWNlcyI6eyLQktGL0YfRkdGBIjo2MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTB9LCJicmVlZF9lbiI6IkNhdCBNYWluZSBDb29uIiwiYnJlZWRfZXQiOiJLYXNzIE1haW5lIENvb24ifV07CnZhciBSQUlMV0FZID0gImh0dHBzOi8vcmpncm9vbWluZy51cC5yYWlsd2F5LmFwcC9ib29rIjsKdmFyIEdPT0dMRV9TQ1JJUFQgPSAiaHR0cHM6Ly9zY3JpcHQuZ29vZ2xlLmNvbS9tYWNyb3Mvcy9BS2Z5Y2J5VFNaLWVKTWRlcC1EMExyLW54MF9WNEhCV2dJSWN0blJUMnJqU0R2QnliajVDWUkzTksyTXFjQXdfY2ZjemdSRWlmZy9leGVjIjsKdmFyIEZBTExCQUNLX1RJTUVTID0gWycxMDowMCcsJzEwOjMwJywnMTE6MDAnLCcxMTozMCcsJzEyOjAwJywnMTI6MzAnLCcxMzowMCcsJzEzOjMwJywnMTQ6MDAnLCcxNDozMCcsJzE1OjAwJywnMTU6MzAnLCcxNjowMCcsJzE2OjMwJywnMTc6MDAnLCcxNzozMCcsJzE4OjAwJ107CnZhciBib29raW5nID0ge2JyZWVkOicnLGJyZWVkRGlzcGxheTonJyxzZXJ2aWNlOicnLHByaWNlOjAsbWFzdGVyOicnLGdyb29tSGlzdG9yeTonJyxkYXRlOicnLHRpbWU6JycsbGFuZzoncnUnfTsKdmFyIHNlbEJyZWVkID0gbnVsbDsKdmFyIGNZID0gbmV3IERhdGUoKS5nZXRGdWxsWWVhcigpOwp2YXIgY00gPSBuZXcgRGF0ZSgpLmdldE1vbnRoKCk7CnZhciBzdGVwID0gMTsKdmFyIE1PTlRIUyA9IFsn0K/QvdCy0LDRgNGMJywn0KTQtdCy0YDQsNC70YwnLCfQnNCw0YDRgicsJ9CQ0L/RgNC10LvRjCcsJ9Cc0LDQuScsJ9CY0Y7QvdGMJywn0JjRjtC70YwnLCfQkNCy0LPRg9GB0YInLCfQodC10L3RgtGP0LHRgNGMJywn0J7QutGC0Y/QsdGA0YwnLCfQndC+0Y/QsdGA0YwnLCfQlNC10LrQsNCx0YDRjCddOwoKZnVuY3Rpb24gc2hvd1NjcmVlbihpZCkgewogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5zY3JlZW4nKS5mb3JFYWNoKGZ1bmN0aW9uKHMpe3MuY2xhc3NMaXN0LnJlbW92ZSgnYWN0aXZlJyk7fSk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoaWQpLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpOwogIHdpbmRvdy5zY3JvbGxUbygwLDApOwp9CgpmdW5jdGlvbiBnb1N0ZXAobikgewogIFsnYmsxJywnYmsyJywnYmszJywnYms0JywnYms1J10uZm9yRWFjaChmdW5jdGlvbihpZCxpKXsKICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKGlkKS5jbGFzc05hbWUgPSAnc3RlcCcgKyAoaSsxPT09bj8nIHNob3cnOicnKTsKICB9KTsKICBmb3IodmFyIGk9MTtpPD01O2krKyl7CiAgICB2YXIgcHM9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3BzJytpKTsKICAgIHZhciBwbD1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgncGwnK2kpOwogICAgaWYoaTxuKXtwcy5jbGFzc05hbWU9J3BzIGRvbmUnO2lmKHBsKXBsLmNsYXNzTmFtZT0ncGwgZG9uZSc7fQogICAgZWxzZSBpZihpPT09bil7cHMuY2xhc3NOYW1lPSdwcyBhY3RpdmUnO2lmKHBsKXBsLmNsYXNzTmFtZT0ncGwnO30KICAgIGVsc2V7cHMuY2xhc3NOYW1lPSdwcyc7aWYocGwpcGwuY2xhc3NOYW1lPSdwbCc7fQogIH0KICBzdGVwPW47IHdpbmRvdy5zY3JvbGxUbygwLDApOwogIGlmKG49PT0zKSBmaWx0ZXJNYXN0ZXJzKCk7Cn0KCmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdib29rQnRuJykub25jbGljayA9IGZ1bmN0aW9uKCl7CiAgc2hvd1NjcmVlbignYm9va1NjcmVlbicpOyBnb1N0ZXAoMSk7IGJ1aWxkQ2FsKCk7Cn07CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdiYWNrQnRuJykub25jbGljayA9IGZ1bmN0aW9uKCl7CiAgaWYoc3RlcD4xKXtnb1N0ZXAoc3RlcC0xKTt9ZWxzZXtzaG93U2NyZWVuKCdob21lU2NyZWVuJyk7fQp9Owpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnaG9tZUJ0bicpLm9uY2xpY2sgPSBmdW5jdGlvbigpewogIHNob3dTY3JlZW4oJ2hvbWVTY3JlZW4nKTsgcmVzZXRBbGwoKTsKfTsKCi8vIEJyZWVkIHNlYXJjaAp2YXIgaW5wID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2JJbnB1dCcpOwp2YXIgZHJvcCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdiRHJvcCcpOwp2YXIgY2xyID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NsckJ0bicpOwp2YXIgYmFkZ2UgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc0JhZGdlJyk7CgppbnAuYWRkRXZlbnRMaXN0ZW5lcignaW5wdXQnLCBmdW5jdGlvbigpewogIHZhciBxID0gaW5wLnZhbHVlLnRyaW0oKTsKICBjbHIuY2xhc3NMaXN0LnRvZ2dsZSgnc2hvdycsIHEubGVuZ3RoPjApOwogIGlmKCFxKXtkcm9wLmNsYXNzTGlzdC5yZW1vdmUoJ29wZW4nKTtkcm9wLmlubmVySFRNTD0nJztyZXR1cm47fQogIHZhciBzZj1MQU5HPT09J2VuJz8nYnJlZWRfZW4nOkxBTkc9PT0nZXQnPydicmVlZF9ldCc6J2JyZWVkJzsKICB2YXIgcmVzPURBVEEuZmlsdGVyKGZ1bmN0aW9uKGIpe3JldHVybihiW3NmXXx8Yi5icmVlZCkudG9Mb3dlckNhc2UoKS5pbmRleE9mKHEudG9Mb3dlckNhc2UoKSkhPT0tMTt9KS5zbGljZSgwLDM1KTsKICBkcm9wLmlubmVySFRNTD0nJzsKICB2YXIgX25yPUxBTkc9PT0nZW4nPydCcmVlZCBub3QgZm91bmQnOkxBTkc9PT0nZXQnPydUw7V1Z3UgZWkgbGVpdHVkJzon0J/QvtGA0L7QtNCwINC90LUg0L3QsNC50LTQtdC90LAnOwogIHZhciBfbnQ9TEFORz09PSdlbic/IkNhbid0IGZpbmQgeW91ciBicmVlZD8iOkxBTkc9PT0nZXQnPydFaSBsZWlhIG9tYSB0w7V1Z3U/Jzon0J3QtSDQvdCw0YjQu9C4INGB0LLQvtGOINC/0L7RgNC+0LTRgz8nOwogIHZhciBfbnM9TEFORz09PSdlbic/J0NvbnRhY3QgdXMg4oCUIHdlIHdpbGwgaGVscCB5b3UgY2hvb3NlIGEgc2VydmljZSc6TEFORz09PSdldCc/J1bDtXRrZSBtZWllZ2Egw7xoZW5kdXN0IOKAlCBhaXRhbWUgdGVlbnVzZSB2YWxpZGEnOifQodCy0Y/QttC40YLQtdGB0Ywg0YEg0L3QsNC80Lgg0LvRjtCx0YvQvCDRg9C00L7QsdC90YvQvCDRgdC/0L7RgdC+0LHQvtC8IOKAlCDQvNGLINC/0L7QvNC+0LbQtdC8INC/0L7QtNC+0LHRgNCw0YLRjCDRg9GB0LvRg9Cz0YMnOwogIGlmKCFyZXMubGVuZ3RoKXtkcm9wLmlubmVySFRNTD0nPGRpdiBjbGFzcz0ibm9yZXMiPicrX25yKyc8L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXIiIG9uY2xpY2s9InNob3dTY3JlZW4oXCdob21lU2NyZWVuXCcpIj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItaWNvbiI+8J+QvjwvZGl2PjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci10ZXh0Ij48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItdGl0bGUiPicrX250Kyc8L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItc3ViIj4nK19ucysnPC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLWFycm93Ij7ihpI8L2Rpdj48L2Rpdj4nO30KICBlbHNlewogICAgcmVzLmZvckVhY2goZnVuY3Rpb24oYil7CiAgICAgIHZhciBkPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpOyBkLmNsYXNzTmFtZT0nZGl0ZW0nOwogICAgICB2YXIgYm5hbWU9YltzZl18fGIuYnJlZWQ7CiAgICAgIHZhciBpZHg9Ym5hbWUudG9Mb3dlckNhc2UoKS5pbmRleE9mKHEudG9Mb3dlckNhc2UoKSk7CiAgICAgIGQuaW5uZXJIVE1MPWJuYW1lLnN1YnN0cmluZygwLGlkeCkrJzxtYXJrPicrYm5hbWUuc3Vic3RyaW5nKGlkeCxpZHgrcS5sZW5ndGgpKyc8L21hcms+JytibmFtZS5zdWJzdHJpbmcoaWR4K3EubGVuZ3RoKTsKICAgICAgZC5vbmNsaWNrPWZ1bmN0aW9uKCl7c2VsZWN0QnJlZWQoYik7fTsKICAgICAgZHJvcC5hcHBlbmRDaGlsZChkKTsKICAgIH0pOwogIH0KICBkcm9wLmNsYXNzTGlzdC5hZGQoJ29wZW4nKTsKfSk7Cgpkb2N1bWVudC5hZGRFdmVudExpc3RlbmVyKCdjbGljaycsZnVuY3Rpb24oZSl7CiAgaWYoIWUudGFyZ2V0LmNsb3Nlc3QoJy5id3JhcCcpKWRyb3AuY2xhc3NMaXN0LnJlbW92ZSgnb3BlbicpOwp9KTsKY2xyLm9uY2xpY2sgPSByZXNldEJyZWVkOwoKZnVuY3Rpb24gc2VsZWN0QnJlZWQoYil7CiAgc2VsQnJlZWQ9YjsgYm9va2luZy5icmVlZD1iLmJyZWVkOwogIGlucC52YWx1ZT0nJzsgY2xyLmNsYXNzTGlzdC5yZW1vdmUoJ3Nob3cnKTsKICBkcm9wLmNsYXNzTGlzdC5yZW1vdmUoJ29wZW4nKTsgZHJvcC5pbm5lckhUTUw9Jyc7CiAgYmFkZ2UuaW5uZXJIVE1MPScnOwogIHZhciBiRmllbGQ9TEFORz09PSdlbic/J2JyZWVkX2VuJzpMQU5HPT09J2V0Jz8nYnJlZWRfZXQnOidicmVlZCc7CiAgdmFyIGRpc3BCcmVlZD1iW2JGaWVsZF18fGIuYnJlZWQ7CiAgYm9va2luZy5icmVlZERpc3BsYXk9ZGlzcEJyZWVkOwogIHZhciBibj1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdzcGFuJyk7Ym4uY2xhc3NOYW1lPSdibmFtZSc7Ym4udGV4dENvbnRlbnQ9ZGlzcEJyZWVkOwogIHZhciBjaGdUeHQ9TEFORz09PSdlbic/J0NoYW5nZSc6TEFORz09PSdldCc/J011dWRhJzon0JjQt9C80LXQvdC40YLRjCc7CiAgdmFyIGJjPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NwYW4nKTtiYy5jbGFzc05hbWU9J2JjaGcnO2JjLnRleHRDb250ZW50PWNoZ1R4dDsKICBiYy5vbmNsaWNrPXJlc2V0QnJlZWQ7CiAgYmFkZ2UuYXBwZW5kQ2hpbGQoYm4pO2JhZGdlLmFwcGVuZENoaWxkKGJjKTsKICBiYWRnZS5jbGFzc0xpc3QuYWRkKCdzaG93Jyk7CiAgcmVuZGVyU3ZjcyhiKTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjU2VjJykuc3R5bGUuZGlzcGxheT0nYmxvY2snOwogICAgLy8gQWRkIGltcG9ydGFudCBub3RlIGlmIG5vdCBleGlzdHMKICAgIGlmKCFkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjTm90ZScpKXsKICAgICAgdmFyIG5vdGU9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7CiAgICAgIG5vdGUuaWQ9J3N2Y05vdGUnOwogICAgICBub3RlLnN0eWxlLmNzc1RleHQ9J2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDgpO3BhZGRpbmc6MTRweCAxNnB4O2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDIpO21hcmdpbi10b3A6MTJweDsnOwogICAgICB2YXIgbm90ZVRpdGxlPUxBTkc9PT0nZW4nPydQbGVhc2Ugbm90ZSc6TEFORz09PSdldCc/J1BhbmdlIHTDpGhlbGUnOifQktCw0LbQvdC+INC30L3QsNGC0YwnOwogICAgICB2YXIgbm90ZUJvZHk9TEFORz09PSdlbic/J0ZpbmFsIHByaWNlIGRlcGVuZHMgb24gY29hdCBjb25kaXRpb24gYW5kIHBldCBiZWhhdmlvdXIuPGJyPkRlbWF0dGluZyBmcm9tIDUg4oKsLjxicj5BZ2dyZXNzaXZlIGJlaGF2aW91ciBzdXJjaGFyZ2UgbWF5IGFwcGx5OiArNTAlLic6TEFORz09PSdldCc/J0zDtXBsaWsgaGluZCBzw7VsdHViIGthcnZhc3Rpa3Ugc2Vpc3VuZGlzdCBqYSBsZW1taWtsb29tYSBrw6RpdHVtaXNlc3QuPGJyPktvbHRzdW5pdGUgbGFodGloYXJ1dGFtaW5lIGFsYXRlcyA1IOKCrC48YnI+QWdyZXNzaWl2c2Uga8OkaXR1bWlzZSBrb3JyYWwgdsO1aWIgbGlzYW5kdWRhIDUwJSBqdXVyZGVoaW5kbHVzLic6J9Ce0LrQvtC90YfQsNGC0LXQu9GM0L3QsNGPINGB0YLQvtC40LzQvtGB0YLRjCDQt9Cw0LLQuNGB0LjRgiDQvtGCINGB0L7RgdGC0L7Rj9C90LjRjyDRiNC10YDRgdGC0Lgg0Lgg0L/QvtCy0LXQtNC10L3QuNGPINC/0LjRgtC+0LzRhtCwLjxicj7QoNCw0LfQsdC+0YAg0LrQvtC70YLRg9C90L7QsiDigJQg0L7RgiA1IOKCrC48YnI+0J/RgNC4INCw0LPRgNC10YHRgdC40LLQvdC+0Lwg0L/QvtCy0LXQtNC10L3QuNC4INC80L7QttC10YIg0L/RgNC40LzQtdC90Y/RgtGM0YHRjyDQtNC+0L/Qu9Cw0YLQsCA1MCUuJzsKICAgICAgbm90ZS5pbm5lckhUTUw9JzxkaXYgc3R5bGU9ImZvbnQtc2l6ZTowLjgzOHJlbTtsZXR0ZXItc3BhY2luZzouMTVlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjhweDtmb250LXdlaWdodDo2MDA7Zm9udC1mYW1pbHk6XCdNb250c2VycmF0XCcsc2Fucy1zZXJpZiI+Jytub3RlVGl0bGUrJzwvZGl2PjxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxLjAyNXJlbTtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjEuODtmb250LWZhbWlseTpcJ01vbnRzZXJyYXRcJyxzYW5zLXNlcmlmIj4nK25vdGVCb2R5Kyc8L2Rpdj4nOwogICAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjU2VjJykuYXBwZW5kQ2hpbGQobm90ZSk7CiAgICB9CiAgZmlsdGVyTWFzdGVycygpOwp9CgpmdW5jdGlvbiByZXNldEJyZWVkKCl7CiAgc2VsQnJlZWQ9bnVsbDtib29raW5nLmJyZWVkPScnO2Jvb2tpbmcuc2VydmljZT0nJztib29raW5nLnByaWNlPTA7CiAgaW5wLnZhbHVlPScnO2Nsci5jbGFzc0xpc3QucmVtb3ZlKCdzaG93Jyk7CiAgYmFkZ2UuY2xhc3NMaXN0LnJlbW92ZSgnc2hvdycpO2JhZGdlLmlubmVySFRNTD0nJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjU2VjJykuc3R5bGUuZGlzcGxheT0nbm9uZSc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y0xpc3QnKS5pbm5lckhUTUw9Jyc7Cn0KCgp2YXIgU1ZDX1RSQU5TTEFUSU9OUyA9IHsKICAn0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQnOiAgICAgIHtlbjonQmFzaWMgZ3Jvb20nLCAgICAgIGV0OidQw7VoaWhvb2xkdXMnfSwKICAn0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQnOntlbjonSHlnaWVuZSBncm9vbScsICAgIGV0OidIw7xnaWVlbmlob29sZHVzJ30sCiAgJ9Ca0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQnOiAge2VuOidGdWxsIGdyb29tJywgICAgICAgIGV0OidUw6RpZWxpayBob29sZHVzJ30sCiAgJ9Ci0YDQuNC80LzQuNC90LMnOiAgICAgICAgICB7ZW46J1RyaW1taW5nJywgICAgICAgICAgZXQ6J1RyaW1tZXJpbWluZSd9LAogICfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6ICAge2VuOidFeHByZXNzIHNoZWQnLCAgICAgIGV0OidLaWlya2FydmF2YWhldHVzJ30sCiAgJ9CS0YvRh9C10YEnOiAgICAgICAgICAgICB7ZW46J0JydXNoLW91dCcsICAgICAgICAgZXQ6J0hhcmphbWluZSd9LAogICfQktGB0Y8g0L/RgNC+0LPRgNCw0LzQvNCwJzogICAgIHtlbjonRnVsbCBwcm9ncmFtJywgICAgICBldDonS29ndSBwcm9ncmFtbSd9Cn07CnZhciBTVkNfVEFHTElORV9JMThOPXsKICBydTp7J9CS0YvRh9C10YEnOifQodGC0L7QuNC80L7RgdGC0Ywg0LfQsNCy0LjRgdC40YIg0L7RgiDRgdC+0YHRgtC+0Y/QvdC40Y8g0YjQtdGA0YHRgtC4INC4INC+0LHRitGR0LzQsCDRgNCw0LHQvtGCJywn0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQnOifQn9C+0LTRhdC+0LTQuNGCINC00LvRjyDQv9C+0LTQtNC10YDQttCw0L3QuNGPINGH0LjRgdGC0L7RgtGLINC80LXQttC00YMg0L/RgNC+0YbQtdC00YPRgNCw0LzQuCcsJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Jzon0JTQu9GPINC60L7QvNGE0L7RgNGC0LAg0Lgg0LDQutC60YPRgNCw0YLQvdC+0YHRgtC4INC/0LjRgtC+0LzRhtCwJywn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc6J9Cf0L7Qu9C90YvQuSDRg9GF0L7QtCDRgdC+INGB0YLRgNC40LbQutC+0LknLCfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6J9Cf0L7QvNC+0LPQsNC10YIg0YPQvNC10L3RjNGI0LjRgtGMINC60L7Qu9C40YfQtdGB0YLQstC+INC70LjQvdGP0Y7RidC10Lkg0YjQtdGA0YHRgtC4Jywn0KLRgNC40LzQvNC40L3Qsyc6J9CU0LvRjyDQttC10YHRgtC60L7RiNC10YDRgdGC0L3Ri9GFINC/0L7RgNC+0LQnfSwKICBlbjp7J9CS0YvRh9C10YEnOidQcmljZSBkZXBlbmRzIG9uIGNvYXQgY29uZGl0aW9uIGFuZCB2b2x1bWUgb2Ygd29yaycsJ9CR0LDQt9C+0LLRi9C5INGD0YXQvtC0JzonSWRlYWwgZm9yIG1haW50YWluaW5nIGNsZWFubGluZXNzIGJldHdlZW4gZnVsbCBncm9vbXMnLCfQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCc6J0ZvciB5b3VyIHBldFwncyBjb21mb3J0IGFuZCBuZWF0bmVzcycsJ9Ca0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQnOidGdWxsIGdyb29taW5nIHdpdGggaGFpcmN1dCcsJ9Ct0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwJzonU2lnbmlmaWNhbnRseSByZWR1Y2VzIHNoZWRkaW5nJywn0KLRgNC40LzQvNC40L3Qsyc6J0ZvciB3aXJlLWhhaXJlZCBicmVlZHMnfSwKICBldDp7J9CS0YvRh9C10YEnOidIaW5kIHPDtWx0dWIga2FydmFzdGlrdSBzZWlzdW5kaXN0IGphIHTDtsO2bWFodXN0Jywn0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQnOidTb2JpYiBwdWh0dXNlIGhvaWRtaXNla3MgcHJvdHNlZHV1cmlkZSB2YWhlbCcsJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0JzonTGVtbWlrbG9vbWEgbXVnYXZ1c2VrcyBqYSBrb3JyYXNob2l1a3MnLCfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0JzonVMOkaWVsaWsgaG9vbGR1cyBrb29zIGzDtWlrdXNlZ2EnLCfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6J1bDpGhlbmRhYiBvbHVsaXNlbHQga2FydmFkZSBsYW5nZW1pc3QnLCfQotGA0LjQvNC80LjQvdCzJzonVHJhYXRrYXJ2YWxpc3RlbGUgdMO1dWd1ZGVsZSd9Cn07CnZhciBTVkNfREVTQ19JMThOPXsKICBydTp7J9CS0YvRh9C10YEnOifQp9C40YHRgtC60LAg0LPQu9Cw0LcsINGD0YjQtdC5LCDQv9C+0LTRgdGC0YDQuNCz0LDQvdC40LUg0LrQvtCz0YLQtdC5LCDQstGL0YfRkdGBICjQtNC70Y8g0LrQvtGI0LXQuiknLCfQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCc6J9Cc0YvRgtGM0ZEg0L/RgNC+0YTQtdGB0YHQuNC+0L3QsNC70YzQvdGL0LzQuCDRgdGA0LXQtNGB0YLQstCw0LzQuCwg0LTQtdC70LjQutCw0YLQvdCw0Y8g0YHRg9GI0LrQsCcsJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Jzon0KHRgtGA0LjQttC60LAg0LrQvtCz0YLQtdC5LCDRh9C40YHRgtC60LAg0YPRiNC10Lkg0Lgg0LPQu9Cw0LcsINC60YPQv9Cw0L3QuNC1LCDRgdGD0YjQutCwLCDRg9GF0L7QtCDQt9CwINC70LDQv9C60LDQvNC4INC4INGH0YPQstGB0YLQstC40YLQtdC70YzQvdGL0LzQuCDQt9C+0L3QsNC80LgnLCfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Jzon0KHRgtGA0LjQttC60LAg0LrQvtCz0YLQtdC5LCDRh9C40YHRgtC60LAg0YPRiNC10Lkg0Lgg0LPQu9Cw0LcsINC60YPQv9Cw0L3QuNC1LCDRgdGD0YjQutCwLCDRg9GF0L7QtCDQt9CwINC70LDQv9C60LDQvNC4INC4INGH0YPQstGB0YLQstC40YLQtdC70YzQvdGL0LzQuCDQt9C+0L3QsNC80LgsINC80L7QtNC10LvRjNC90LDRjyDRgdGC0YDQuNC20LrQsCcsJ9Ct0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwJzon0JzRi9GC0YzRkSwg0YHRg9GI0LrQsCwg0YPRhdC+0LQg0LfQsCDRiNC10YDRgdGC0YzRjiwg0LzQsNGB0LrQsCwg0L/QvtC00YHRgtGA0LjQs9Cw0L3QuNC1INC60L7Qs9GC0LXQuSwg0YfQuNGB0YLQutCwINGD0YjQtdC5INC4INCz0LvQsNC3LCDRg9GF0L7QtCDQt9CwINC70LDQv9Cw0LzQuCDQuCDQt9C+0L3QsNC80Lgg0YLRgNC10LHRg9GO0YnQuNC80Lgg0L7RgdC+0LHQvtCz0L4g0LLQvdC40LzQsNC90LjRjycsJ9Ci0YDQuNC80LzQuNC90LMnOifQktGL0YnQuNC/0YvQstCw0L3QuNC1INGB0YLQsNGA0L7Qs9C+INGB0LvQvtGPINGI0LXRgNGB0YLQuCwg0LzRi9GC0YzRkSwg0YHRg9GI0LrQsCwg0YHRgtGA0LjQttC60LAg0LrQvtCz0YLQtdC5LCDRh9C40YHRgtC60LAg0YPRiNC10Lkg0Lgg0LPQu9Cw0LcsINC+0YTQvtGA0LzQu9C10L3QuNC1INGI0LXRgNGB0YLQuCcsJ9CS0YHRjyDQv9GA0L7Qs9GA0LDQvNC80LAnOifQn9CV0KDQktCr0Jkg0JLQmNCX0JjQoiAoMjAtMzAg0LzQuNC9KSDigJQgMjAg4oKsXG7igKIg0LfQvdCw0LrQvtC80YHRgtCy0L4g0YHQviDRgdGC0L7Qu9C+0Lwg0Lgg0LjQvdGB0YLRgNGD0LzQtdC90YLQsNC80LhcbuKAoiDQu9GR0LPQutC+0LUg0LLRi9GH0ZHRgdGL0LLQsNC90LjQtVxu4oCiINC30LLRg9C60Lgg0YTQtdC90LAg0Lgg0LvQtdCz0LrQsNGPINC/0YDQvtC00YPQstC60LBcbuKAoiDQvtGB0LLQtdC20LXQvdC40LUg0LPQu9Cw0LfQvtC6INC4INGD0YjQtdC6XG7igKIg0LrQvtCz0L7RgtC60LhcbuKAoiDQstC60YPRgdC90Y/RiNC60Lgg0Lgg0YHQv9C+0LrQvtC50L3QsNGPINCw0LTQsNC/0YLQsNGG0LjRj1xuXG7QktCi0J7QoNCe0Jkg0JLQmNCX0JjQoiAoNDAtNjAg0LzQuNC9KSDigJQgMzUg4oKsXG7igKIg0L/QtdGA0LLQvtC1INC60YPQv9Cw0L3QuNC1INC4INGB0YPRiNC60LBcbuKAoiDQstGL0YfRkdGB0YvQstCw0L3QuNC1XG7igKIg0LPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LRcbuKAoiDQvdC10LHQvtC70YzRiNCw0Y8g0YHRgtGA0LjQttC60LAgLyDQutC+0YDRgNC10LrRhtC40Y8g0YjQtdGA0YHRgtC4ICjQv9GA0Lgg0L3QtdC+0LHRhdC+0LTQuNC80L7RgdGC0LgpXG7igKIg0LfQsNC60YDQtdC/0LvQtdC90LjQtSDQv9C+0LvQvtC20LjRgtC10LvRjNC90L7Qs9C+INC+0L/Ri9GC0LAnfSwKICBlbjp7J9CS0YvRh9C10YEnOidFeWUgYW5kIGVhciBjbGVhbmluZywgbmFpbCB0cmltbWluZywgYnJ1c2hpbmcgKGZvciBjYXRzKScsJ9CR0LDQt9C+0LLRi9C5INGD0YXQvtC0JzonV2FzaGluZyB3aXRoIHByb2Zlc3Npb25hbCBwcm9kdWN0cywgZ2VudGxlIGRyeWluZycsJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0JzonTmFpbCB0cmltbWluZywgZWFyIGFuZCBleWUgY2xlYW5pbmcsIGJhdGhpbmcsIGRyeWluZywgcGF3IGFuZCBzZW5zaXRpdmUgYXJlYSBjYXJlJywn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc6J05haWwgdHJpbW1pbmcsIGVhciBhbmQgZXllIGNsZWFuaW5nLCBiYXRoaW5nLCBkcnlpbmcsIHBhdyBhbmQgc2Vuc2l0aXZlIGFyZWEgY2FyZSwgc3R5bGluZyBoYWlyY3V0Jywn0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAnOidXYXNoaW5nLCBkcnlpbmcsIGNvYXQgY2FyZSwgbWFzaywgbmFpbCB0cmltbWluZywgZWFyIGFuZCBleWUgY2xlYW5pbmcsIHBhdyBhbmQgc3BlY2lhbCBhcmVhIGNhcmUnLCfQotGA0LjQvNC80LjQvdCzJzonUmVtb3Zpbmcgb2xkIGNvYXQgbGF5ZXIsIHdhc2hpbmcsIGRyeWluZywgbmFpbCB0cmltbWluZywgZWFyIGFuZCBleWUgY2xlYW5pbmcsIGNvYXQgc3R5bGluZycsJ9CS0YHRjyDQv9GA0L7Qs9GA0LDQvNC80LAnOidGSVJTVCBWSVNJVCAoMjAtMzAgbWluKSDigJQg4oKsMjBcbuKAoiBnZXR0aW5nIHVzZWQgdG8gdGhlIHRhYmxlIGFuZCB0b29sc1xu4oCiIGdlbnRsZSBicnVzaGluZ1xu4oCiIGRyeWVyIHNvdW5kcyBhbmQgbGlnaHQgYWlyZmxvd1xu4oCiIGV5ZSBhbmQgZWFyIHJlZnJlc2hcbuKAoiBuYWlsIHRyaW1cbuKAoiB0cmVhdHMgYW5kIGNhbG0gYWRhcHRhdGlvblxuXG5TRUNPTkQgVklTSVQgKDQwLTYwIG1pbikg4oCUIOKCrDM1XG7igKIgZmlyc3QgYmF0aCBhbmQgZHJ5aW5nXG7igKIgYnJ1c2hpbmdcbuKAoiBoeWdpZW5lIGNhcmVcbuKAoiBsaWdodCB0cmltIC8gY29hdCBhZGp1c3RtZW50IChpZiBuZWVkZWQpXG7igKIgcmVpbmZvcmNpbmcgdGhlIHBvc2l0aXZlIGV4cGVyaWVuY2UnfSwKICBldDp7J9CS0YvRh9C10YEnOidTaWxtYWRlIGphIGvDtXJ2YWRlIHB1aGFzdGFtaW5lLCBrw7zDvG50ZSBsw7Vpa2FtaW5lLCBoYXJqYW1pbmUgKGthc3NpZGVsZSknLCfQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCc6J1Blc2VtaW5lIHByb2Zlc3Npb25hYWxzZXRlIHZhaGVuZGl0ZWdhLCDDtXJuIGt1aXZhdGFtaW5lJywn0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQnOidLw7zDvG50ZSBsw7Vpa2FtaW5lLCBrw7VydmFkZSBqYSBzaWxtYWRlIHB1aGFzdGFtaW5lLCBwZXNlbWluZSwga3VpdmF0YW1pbmUsIGvDpHBwYWRlIGphIHR1bmRsaWtlIHBpaXJrb25kYWRlIGhvb2xkdXMnLCfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0JzonS8O8w7xudGUgbMO1aWthbWluZSwga8O1cnZhZGUgamEgc2lsbWFkZSBwdWhhc3RhbWluZSwgcGVzZW1pbmUsIGt1aXZhdGFtaW5lLCBrw6RwcGFkZSBqYSB0dW5kbGlrZSBwaWlya29uZGFkZSBob29sZHVzLCBtb2RlbGzDtWlrdXMnLCfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6J1Blc2VtaW5lLCBrdWl2YXRhbWluZSwga2FydmFzdGlrdSBob29sZHVzLCBtYXNrLCBrw7zDvG50ZSBsw7Vpa2FtaW5lLCBrw7VydmFkZSBqYSBzaWxtYWRlIHB1aGFzdGFtaW5lLCBrw6RwcGFkZSBqYSBlcmlsaXN0ZSBwaWlya29uZGFkZSBob29sZHVzJywn0KLRgNC40LzQvNC40L3Qsyc6J1ZhbmEga2FydmFraWhpIGVlbWFsZGFtaW5lLCBwZXNlbWluZSwga3VpdmF0YW1pbmUsIGvDvMO8bnRlIGzDtWlrYW1pbmUsIGvDtXJ2YWRlIGphIHNpbG1hZGUgcHVoYXN0YW1pbmUsIGthcnZhc3Rpa3Uga3VqdW5kYW1pbmUnLCfQktGB0Y8g0L/RgNC+0LPRgNCw0LzQvNCwJzonRVNJTUVORSBLw5xMQVNUVVMgKDIwLTMwIG1pbikg4oCUIDIwIOKCrFxu4oCiIHR1dHZ1bWluZSBsYXVhZ2EgamEgdMO2w7ZyaWlzdGFkZWdhXG7igKIga2VyZ2UgaGFyamFtaW5lXG7igKIgZsO2w7ZuaWhlbGlkIGphIGtlcmdlIMO1aHV2b29sXG7igKIgc2lsbWFkZSBqYSBrw7VydmFkZSB2w6Ryc2tlbmR1c1xu4oCiIGvDvMO8bnRlIGzDtWlrYW1pbmVcbuKAoiBtYWl1c2VkIGphIHJhaHVsaWsga29oYW5lbWluZVxuXG5URUlORSBLw5xMQVNUVVMgKDQwLTYwIG1pbikg4oCUIDM1IOKCrFxu4oCiIGVzaW1lbmUgdmFubml0YW1pbmUgamEga3VpdmF0YW1pbmVcbuKAoiBoYXJqYW1pbmVcbuKAoiBow7xnaWVlbmlob29sZHVzXG7igKIga2VyZ2UgbMO1aWt1cyAvIGthcnZhIGtvcnJpZ2VlcmltaW5lICh2YWphZHVzZWwpXG7igKIgcG9zaXRpaXZzZSBrb2dlbXVzZSBraW5uaXN0YW1pbmUnfQp9Owp2YXIgU1ZDX0RFU0NfQ0FUX0NPTVBMRVg9ewogIHJ1OifQnNGL0YLRjNGRLCDRgdGD0YjQutCwLCDQstGL0YfRkdGB0YvQstCw0L3QuNC1LCDRgdGC0YDQuNC20LrQsCDQutC+0LPRgtC10LksINCwINGC0LDQutC20LUg0L7QsdGA0LDQsdC+0YLQutCwINCz0LvQsNC3INC4INGD0YjQtdC6JywKICBlbjonV2FzaGluZywgZHJ5aW5nLCBicnVzaGluZywgbmFpbCB0cmltbWluZywgYW5kIGV5ZSBhbmQgZWFyIGNhcmUnLAogIGV0OidQZXNlbWluZSwga3VpdmF0YW1pbmUsIGhhcmphbWluZSwga8O8w7xudGUgbMO1aWthbWluZSBuaW5nIHNpbG1hZGUgamEga8O1cnZhZGUgaG9vbGR1cycKfTsKZnVuY3Rpb24gZ2V0U3ZjVGFnKG5hbWUpe3JldHVybihTVkNfVEFHTElORV9JMThOW0xBTkddJiZTVkNfVEFHTElORV9JMThOW0xBTkddW25hbWVdKXx8U1ZDX1RBR0xJTkVfSTE4Ti5ydVtuYW1lXXx8Jyc7fQpmdW5jdGlvbiBnZXRTdmNEZXNjKG5hbWUpewogIGlmKG5hbWU9PT0n0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCcgJiYgYm9va2luZy5icmVlZCAmJiBib29raW5nLmJyZWVkLmluZGV4T2YoJ9Ca0L7RiNC60LAnKT09PTApewogICAgdmFyIGQ9U1ZDX0RFU0NfQ0FUX0NPTVBMRVhbTEFOR118fFNWQ19ERVNDX0NBVF9DT01QTEVYLnJ1OwogICAgcmV0dXJuIGQ7CiAgfQogIHJldHVybihTVkNfREVTQ19JMThOW0xBTkddJiZTVkNfREVTQ19JMThOW0xBTkddW25hbWVdKXx8U1ZDX0RFU0NfSTE4Ti5ydVtuYW1lXXx8Jyc7Cn0KCmZ1bmN0aW9uIHJlbmRlclN2Y3MoYil7CiAgdmFyIGxibEVsPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdGVwMkxibEVsJyk7CiAgaWYobGJsRWwpewogICAgdmFyIGJhc2VMYmw9KFRbTEFOR10mJlRbTEFOR10uc3RlcDJfbGJsKXx8JzAyIMK3INCj0YHQu9GD0LPQsCc7CiAgICBsYmxFbC50ZXh0Q29udGVudD0oYi5icmVlZD09PSfQqdC10L3QutC4Jyk/KGJhc2VMYmwrJyBQdXBweSBTdGFyJyk6YmFzZUxibDsKICB9CiAgdmFyIGxpc3Q9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y0xpc3QnKTtsaXN0LmlubmVySFRNTD0nJzsKICBPYmplY3QuZW50cmllcyhiLnNlcnZpY2VzKS5mb3JFYWNoKGZ1bmN0aW9uKGt2KXsKICAgIHZhciBuYW1lPWt2WzBdLHByaWNlPWt2WzFdOwoKICAgIHZhciBkaXNwbGF5TmFtZT0oTEFORyE9PSdydScmJlNWQ19UUkFOU0xBVElPTlNbbmFtZV0pP1NWQ19UUkFOU0xBVElPTlNbbmFtZV1bTEFOR106bmFtZTsKICAgIHZhciBidG49ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnYnV0dG9uJyk7YnRuLmNsYXNzTmFtZT0nc3ZidG4nOwogICAgdmFyIHJvdz1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdkaXYnKTtyb3cuY2xhc3NOYW1lPSdzdmJ0bi1yb3cnOwogICAgdmFyIG5zPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NwYW4nKTtucy5jbGFzc05hbWU9J3N2YnRuLW5hbWUnO25zLnRleHRDb250ZW50PWRpc3BsYXlOYW1lOwogICAgdmFyIHBzPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NwYW4nKTtwcy5jbGFzc05hbWU9J3N2YnRuLXByaWNlJztwcy50ZXh0Q29udGVudD1wcmljZSsnIOKCrCc7CiAgICByb3cuYXBwZW5kQ2hpbGQobnMpO3Jvdy5hcHBlbmRDaGlsZChwcyk7CiAgICBidG4uYXBwZW5kQ2hpbGQocm93KTsKICAgIHZhciBkZXNjPWdldFN2Y0Rlc2MobmFtZSk7CiAgICBpZihkZXNjKXt2YXIgZHM9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnc3BhbicpO2RzLmNsYXNzTmFtZT0nc3ZidG4tZGVzYyc7ZHMudGV4dENvbnRlbnQ9ZGVzYztidG4uYXBwZW5kQ2hpbGQoZHMpO30KICAgIHZhciB0YWc9Z2V0U3ZjVGFnKG5hbWUpOwogICAgaWYodGFnKXt2YXIgdHM9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnc3BhbicpO3RzLmNsYXNzTmFtZT0nc3ZidG4tdGFnJzt0cy50ZXh0Q29udGVudD10YWc7YnRuLmFwcGVuZENoaWxkKHRzKTt9CiAgICBidG4ub25jbGljaz1mdW5jdGlvbigpewogICAgICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuc3ZidG4nKS5mb3JFYWNoKGZ1bmN0aW9uKGIpe2IuY2xhc3NMaXN0LnJlbW92ZSgnYWN0aXZlJyk7fSk7CiAgICAgIGJ0bi5jbGFzc0xpc3QuYWRkKCdhY3RpdmUnKTsKICAgICAgYm9va2luZy5zZXJ2aWNlPW5hbWU7Ym9va2luZy5wcmljZT1wcmljZTsKICAgICAgZmlsdGVyTWFzdGVycygpOwogICAgICBzZXRUaW1lb3V0KGZ1bmN0aW9uKCl7Z29TdGVwKDIpO30sMzAwKTsKICAgIH07CiAgICBsaXN0LmFwcGVuZENoaWxkKGJ0bik7CiAgfSk7Cn0KCi8vIE1hc3RlcnMKZnVuY3Rpb24gc29ydE1hc3RlcnNCeUF2YWlsYWJpbGl0eSgpewogIHZhciBub3cgPSBuZXcgRGF0ZSgpOwogIHZhciBtb250aCA9IG5vdy5nZXRNb250aCgpKzEsIHllYXIgPSBub3cuZ2V0RnVsbFllYXIoKTsKICB2YXIgYmFzZU9yZGVyID0gWyfQotCw0YLRjNGP0L3QsCcsJ9CQ0LvQtdC60YHQsNC90LTRgNCwJywn0JrRgdC10L3QuNGPJywn0JDQvdC90LAnLCfQkNC70LjRgdCwJywn0JrRgNC40YHRgtC40L3QsCddOwogIHZhciB2aXNpYmxlQnRucyA9IEFycmF5LnByb3RvdHlwZS5maWx0ZXIuY2FsbChkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcubWJ0bicpLCBmdW5jdGlvbihiKXsgcmV0dXJuIGIuc3R5bGUuZGlzcGxheSAhPT0gJ25vbmUnOyB9KTsKICBpZighdmlzaWJsZUJ0bnMubGVuZ3RoKSByZXR1cm47CiAgUHJvbWlzZS5hbGwodmlzaWJsZUJ0bnMubWFwKGZ1bmN0aW9uKGJ0bil7CiAgICB2YXIgbWFzdGVyID0gYnRuLmdldEF0dHJpYnV0ZSgnZGF0YS1tYXN0ZXInKTsKICAgIHJldHVybiBmZXRjaCh3aW5kb3cubG9jYXRpb24ub3JpZ2luICsgJy9hcGkvYXZhaWxhYmxlX2RheXM/bW9udGg9JyArIG1vbnRoICsgJyZ5ZWFyPScgKyB5ZWFyICsgJyZtYXN0ZXI9JyArIGVuY29kZVVSSUNvbXBvbmVudChtYXN0ZXIpKQogICAgICAudGhlbihmdW5jdGlvbihyKXsgcmV0dXJuIHIuanNvbigpOyB9KQogICAgICAudGhlbihmdW5jdGlvbihkKXsgcmV0dXJuIHtidG46IGJ0biwgbWFzdGVyOiBtYXN0ZXIsIGNvdW50OiAoZC5hdmFpbGFibGV8fFtdKS5sZW5ndGh9OyB9KQogICAgICAuY2F0Y2goZnVuY3Rpb24oKXsgcmV0dXJuIHtidG46IGJ0biwgbWFzdGVyOiBtYXN0ZXIsIGNvdW50OiAtMX07IH0pOwogIH0pKS50aGVuKGZ1bmN0aW9uKHJlc3VsdHMpewogICAgcmVzdWx0cy5zb3J0KGZ1bmN0aW9uKGEsYil7CiAgICAgIGlmKGIuY291bnQgIT09IGEuY291bnQpIHJldHVybiBiLmNvdW50IC0gYS5jb3VudDsKICAgICAgcmV0dXJuIGJhc2VPcmRlci5pbmRleE9mKGEubWFzdGVyKSAtIGJhc2VPcmRlci5pbmRleE9mKGIubWFzdGVyKTsKICAgIH0pOwogICAgcmVzdWx0cy5mb3JFYWNoKGZ1bmN0aW9uKHIsIGkpeyByLmJ0bi5zdHlsZS5vcmRlciA9IGk7IH0pOwogIH0pOwp9CgpmdW5jdGlvbiBmaWx0ZXJNYXN0ZXJzKCl7CiAgdmFyIGlzQ2F0ID0gYm9va2luZy5icmVlZCAmJiBib29raW5nLmJyZWVkLmluZGV4T2YoJ9Ca0L7RiNC60LAnKSA9PT0gMDsKICB2YXIgYnJlZWQgPSBib29raW5nLmJyZWVkIHx8ICcnOwogIHZhciBpc0NhdENvbXBsZXggPSBpc0NhdCAmJiBib29raW5nLnNlcnZpY2UgPT09ICfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0JzsKICB2YXIgYW5uYUV4Y2x1ZGUgPSBbJ9Cc0LDQu9GM0YLQuNC/0YMnLCfQn9GD0LTQtdC70YwnLCfQmdC+0YDQuicsJ9CR0LjRiNC+0L0nLCfQkdC+0LvQvtC90LrQsCcsJ9Cc0LDQu9GM0YLQuNC50YHQutCw0Y8nXTsKICB2YXIgaXNBbm5hQnJlZWQgPSBicmVlZCAmJiAhYW5uYUV4Y2x1ZGUuc29tZShmdW5jdGlvbihiKXsgcmV0dXJuIGJyZWVkLmluZGV4T2YoYikgIT09IC0xOyB9KTsKICB2YXIgYWxleGFuZHJhRXhjbHVkZSA9IFsn0KTQvtC60YHRgtC10YDRjNC10YAnLCfQptCy0LXRgNCz0YjQvdCw0YPRhtC10YAnXTsKICB2YXIgaXNBbGV4YW5kcmFCcmVlZCA9ICFhbGV4YW5kcmFFeGNsdWRlLnNvbWUoZnVuY3Rpb24oYil7IHJldHVybiBicmVlZC5pbmRleE9mKGIpICE9PSAtMTsgfSk7CiAgdmFyIGtzZW5pYUV4Y2x1ZGUgPSBbJ9Cf0YPQtNC10LvRjCcsJ9Cc0LDQu9GM0YLQuNC/0YMnLCfQmdC+0YDQuicsJ9CR0L7Qu9C+0L3QutCwJ107CiAgdmFyIGlzS3NlbmlhQnJlZWQgPSAha3NlbmlhRXhjbHVkZS5zb21lKGZ1bmN0aW9uKGIpeyByZXR1cm4gYnJlZWQuaW5kZXhPZihiKSAhPT0gLTE7IH0pOwogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5tYnRuJykuZm9yRWFjaChmdW5jdGlvbihidG4pewogICAgdmFyIG1hc3RlciA9IGJ0bi5nZXRBdHRyaWJ1dGUoJ2RhdGEtbWFzdGVyJyk7CiAgICB2YXIgaXNUcmltbWluZyA9IGJvb2tpbmcuc2VydmljZSA9PT0gJ9Ci0YDQuNC80LzQuNC90LMnOwogICAgaWYoaXNDYXRDb21wbGV4KXsKICAgICAgYnRuLnN0eWxlLmRpc3BsYXkgPSAobWFzdGVyID09PSAn0KLQsNGC0YzRj9C90LAnIHx8IG1hc3RlciA9PT0gJ9Ca0YHQtdC90LjRjycpID8gJycgOiAnbm9uZSc7CiAgICAgIHJldHVybjsKICAgIH0KICAgIGlmKG1hc3RlciA9PT0gJ9CQ0LvQuNGB0LAnKXsKICAgICAgYnRuLnN0eWxlLmRpc3BsYXkgPSBpc0NhdCA/ICcnIDogJ25vbmUnOwogICAgfSBlbHNlIGlmKG1hc3RlciA9PT0gJ9CQ0L3QvdCwJyl7CiAgICAgIGJ0bi5zdHlsZS5kaXNwbGF5ID0gKGlzQW5uYUJyZWVkICYmICFpc1RyaW1taW5nKSA/ICcnIDogJ25vbmUnOwogICAgfSBlbHNlIGlmKG1hc3RlciA9PT0gJ9CQ0LvQtdC60YHQsNC90LTRgNCwJyl7CiAgICAgIGJ0bi5zdHlsZS5kaXNwbGF5ID0gKGlzQWxleGFuZHJhQnJlZWQgJiYgIWlzVHJpbW1pbmcgJiYgIWlzQ2F0KSA/ICcnIDogJ25vbmUnOwogICAgfSBlbHNlIGlmKG1hc3RlciA9PT0gJ9Ca0YHQtdC90LjRjycpewogICAgICBidG4uc3R5bGUuZGlzcGxheSA9IGlzS3NlbmlhQnJlZWQgPyAnJyA6ICdub25lJzsKICAgIH0gZWxzZSBpZihpc1RyaW1taW5nKXsKICAgICAgYnRuLnN0eWxlLmRpc3BsYXkgPSAnbm9uZSc7CiAgICB9IGVsc2UgewogICAgICBidG4uc3R5bGUuZGlzcGxheSA9ICcnOwogICAgfQogIH0pOwogIHNvcnRNYXN0ZXJzQnlBdmFpbGFiaWxpdHkoKTsKfQoKZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLm1idG4nKS5mb3JFYWNoKGZ1bmN0aW9uKGJ0bil7CiAgYnRuLm9uY2xpY2s9ZnVuY3Rpb24oKXsKICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5tYnRuJykuZm9yRWFjaChmdW5jdGlvbihiKXtiLmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpO30pOwogICAgYnRuLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpOwogICAgYm9va2luZy5tYXN0ZXI9YnRuLmdldEF0dHJpYnV0ZSgnZGF0YS1tYXN0ZXInKTsKICAgIHNldFRpbWVvdXQoZnVuY3Rpb24oKXtnb1N0ZXAoNCk7YnVpbGRDYWwoKTt9LDMwMCk7CiAgfTsKfSk7CgovLyBHcm9vbSBoaXN0b3J5CmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5nYnRuJykuZm9yRWFjaChmdW5jdGlvbihidG4pewogIGJ0bi5vbmNsaWNrPWZ1bmN0aW9uKCl7CiAgICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuZ2J0bicpLmZvckVhY2goZnVuY3Rpb24oYil7Yi5jbGFzc0xpc3QucmVtb3ZlKCdhY3RpdmUnKTt9KTsKICAgIGJ0bi5jbGFzc0xpc3QuYWRkKCdhY3RpdmUnKTsKICAgIGJvb2tpbmcuZ3Jvb21IaXN0b3J5PWJ0bi5nZXRBdHRyaWJ1dGUoJ2RhdGEtdmFsJyk7CiAgICBmaWx0ZXJNYXN0ZXJzKCk7CiAgICBzZXRUaW1lb3V0KGZ1bmN0aW9uKCl7Z29TdGVwKDMpO30sMzAwKTsKICB9Owp9KTsKCi8vIENhbGVuZGFyCmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdwcmV2TScpLm9uY2xpY2s9ZnVuY3Rpb24oKXtjTS0tO2lmKGNNPDApe2NNPTExO2NZLS07fWJ1aWxkQ2FsKCk7fTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ25leHRNJykub25jbGljaz1mdW5jdGlvbigpe2NNKys7aWYoY00+MTEpe2NNPTA7Y1krKzt9YnVpbGRDYWwoKTt9OwoKdmFyIGF2YWlsYWJsZURheXMgPSBbXTsKCmZ1bmN0aW9uIGxvYWRBdmFpbGFibGVEYXlzKCkgewogIHZhciBtYXN0ZXIgPSBib29raW5nLm1hc3RlcjsKICBpZiAoIW1hc3RlcikgcmV0dXJuOwogIGF2YWlsYWJsZURheXMgPSBbXTsKICB2YXIgbG9hZGluZ0VsID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NhbExvYWRpbmcnKTsKICBpZihsb2FkaW5nRWwpIGxvYWRpbmdFbC5jbGFzc0xpc3QuYWRkKCdzaG93Jyk7CiAgZmV0Y2god2luZG93LmxvY2F0aW9uLm9yaWdpbiArICcvYXBpL2F2YWlsYWJsZV9kYXlzP21vbnRoPScgKyAoY00rMSkgKyAnJnllYXI9JyArIGNZICsgJyZtYXN0ZXI9JyArIGVuY29kZVVSSUNvbXBvbmVudChtYXN0ZXIpKQogICAgLnRoZW4oZnVuY3Rpb24ocil7IHJldHVybiByLmpzb24oKTsgfSkKICAgIC50aGVuKGZ1bmN0aW9uKGRhdGEpewogICAgICBhdmFpbGFibGVEYXlzID0gZGF0YS5hdmFpbGFibGUgfHwgW107CiAgICAgIG1hcmtBdmFpbGFibGVEYXlzKCk7CiAgICB9KQogICAgLmNhdGNoKGZ1bmN0aW9uKCl7IGF2YWlsYWJsZURheXMgPSBbXTsgfSkKICAgIC5maW5hbGx5KGZ1bmN0aW9uKCl7CiAgICAgIGlmKGxvYWRpbmdFbCkgbG9hZGluZ0VsLmNsYXNzTGlzdC5yZW1vdmUoJ3Nob3cnKTsKICAgIH0pOwp9CgpmdW5jdGlvbiBtYXJrQXZhaWxhYmxlRGF5cygpIHsKICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuY2QnKS5mb3JFYWNoKGZ1bmN0aW9uKGMpe2lmKCFjLmNsYXNzTGlzdC5jb250YWlucygnZGlzJykpYy5jbGFzc0xpc3QucmVtb3ZlKCdzZWwnKTt9KTsKICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuY2Q6bm90KC5kaXMpOm5vdCguY2RuKTpub3QoLnBhZCknKS5mb3JFYWNoKGZ1bmN0aW9uKGVsKSB7CiAgICB2YXIgZGF5ID0gZWwudGV4dENvbnRlbnQudHJpbSgpOwogICAgaWYgKCFkYXkgfHwgaXNOYU4ocGFyc2VJbnQoZGF5KSkpIHJldHVybjsKICAgIHZhciBkYXRlU3RyID0gU3RyaW5nKHBhcnNlSW50KGRheSkpLnBhZFN0YXJ0KDIsJzAnKSArICcuJyArIFN0cmluZyhjTSsxKS5wYWRTdGFydCgyLCcwJykgKyAnLicgKyBjWTsKICAgIGlmIChhdmFpbGFibGVEYXlzLmluZGV4T2YoZGF0ZVN0cikgIT09IC0xKSB7CiAgICAgIGVsLmNsYXNzTGlzdC5hZGQoJ2F2YWlsJyk7CiAgICAgIGVsLmNsYXNzTGlzdC5yZW1vdmUoJ2J1c3knKTsKICAgIH0gZWxzZSB7CiAgICAgIGVsLmNsYXNzTGlzdC5hZGQoJ2J1c3knKTsKICAgICAgZWwuY2xhc3NMaXN0LnJlbW92ZSgnYXZhaWwnKTsKICAgIH0KICB9KTsKfQoKZnVuY3Rpb24gYnVpbGRDYWwoKXsKICBsb2FkQXZhaWxhYmxlRGF5cygpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYWxNJykudGV4dENvbnRlbnQ9TU9OVEhTW2NNXSsnICcrY1k7CiAgYm9va2luZy5kYXRlPScnOyBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuY2QnKS5mb3JFYWNoKGZ1bmN0aW9uKGMpe2MuY2xhc3NMaXN0LnJlbW92ZSgnc2VsJyk7Yy5jbGFzc0xpc3QucmVtb3ZlKCdhdmFpbCcpO2MuY2xhc3NMaXN0LnJlbW92ZSgnYnVzeScpO30pOwogIHZhciBnPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYWxHJyk7Zy5pbm5lckhUTUw9Jyc7CiAgWyfQn9C9Jywn0JLRgicsJ9Ch0YAnLCfQp9GCJywn0J/RgicsJ9Ch0LEnLCfQktGBJ10uZm9yRWFjaChmdW5jdGlvbihkKXsKICAgIHZhciBlbD1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdkaXYnKTtlbC5jbGFzc05hbWU9J2Nkbic7ZWwudGV4dENvbnRlbnQ9ZDtnLmFwcGVuZENoaWxkKGVsKTsKICB9KTsKICB2YXIgZmlyc3Q9bmV3IERhdGUoY1ksY00sMSkuZ2V0RGF5KCk7CiAgdmFyIGRheXM9bmV3IERhdGUoY1ksY00rMSwwKS5nZXREYXRlKCk7CiAgdmFyIHN0YXJ0PWZpcnN0PT09MD82OmZpcnN0LTE7CiAgdmFyIHRvZGF5PW5ldyBEYXRlKCk7CiAgZm9yKHZhciBpPTA7aTxzdGFydDtpKyspe3ZhciBlbD1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdkaXYnKTtlbC5jbGFzc05hbWU9J2NkIHBhZCc7Zy5hcHBlbmRDaGlsZChlbCk7fQogIGZvcih2YXIgZGF5PTE7ZGF5PD1kYXlzO2RheSsrKXsKICAgIHZhciBlbD1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdkaXYnKTtlbC5jbGFzc05hbWU9J2NkJzsKICAgIHZhciBkYXRlPW5ldyBEYXRlKGNZLGNNLGRheSk7CiAgICB2YXIgaXNQYXN0PWRhdGU8bmV3IERhdGUodG9kYXkuZ2V0RnVsbFllYXIoKSx0b2RheS5nZXRNb250aCgpLHRvZGF5LmdldERhdGUoKSk7CiAgICB2YXIgaW5uZXI9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7aW5uZXIuY2xhc3NOYW1lPSdjZC1pbm5lcic7aW5uZXIudGV4dENvbnRlbnQ9ZGF5O2VsLmFwcGVuZENoaWxkKGlubmVyKTsKICAgIGlmKGlzUGFzdCl7ZWwuY2xhc3NMaXN0LmFkZCgnZGlzJyk7fQogICAgZWxzZXsKICAgICAgaWYoZGF0ZS50b0RhdGVTdHJpbmcoKT09PXRvZGF5LnRvRGF0ZVN0cmluZygpKWVsLmNsYXNzTGlzdC5hZGQoJ3RvZCcpOwogICAgICAoZnVuY3Rpb24oZCwgZWxSZWYpewogICAgICAgIGVsUmVmLm9uY2xpY2s9ZnVuY3Rpb24oKXsKICAgICAgICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5jZCcpLmZvckVhY2goZnVuY3Rpb24oYyl7Yy5jbGFzc0xpc3QucmVtb3ZlKCdzZWwnKTt9KTsKICAgICAgICAgIGVsUmVmLmNsYXNzTGlzdC5hZGQoJ3NlbCcpOwogICAgICAgICAgYm9va2luZy5kYXRlPVN0cmluZyhkKS5wYWRTdGFydCgyLCcwJykrJy4nK1N0cmluZyhjTSsxKS5wYWRTdGFydCgyLCcwJykrJy4nK2NZOwogICAgICAgICAgc2hvd1RpbWVzKCk7CiAgICAgICAgfTsKICAgICAgfSkoZGF5LCBlbCk7CiAgICB9CiAgICBnLmFwcGVuZENoaWxkKGVsKTsKICB9CiAgLy8gZmlsbCB0cmFpbGluZyBjZWxscyB0byBjb21wbGV0ZSBsYXN0IGdyaWQgcm93CiAgdmFyIHRvdGFsID0gc3RhcnQgKyBkYXlzOwogIHZhciB0cmFpbCA9ICg3IC0gKHRvdGFsICUgNykpICUgNzsKICBmb3IodmFyIHQ9MDt0PHRyYWlsO3QrKyl7dmFyIGVwPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO2VwLmNsYXNzTmFtZT0nY2QgcGFkJztnLmFwcGVuZENoaWxkKGVwKTt9Cn0KCmZ1bmN0aW9uIHNob3dUaW1lcygpewogIHZhciB0Zz1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgndGltZUcnKTsKICB0Zy5pbm5lckhUTUw9JzxkaXYgY2xhc3M9ImxvYWRpbmctc2xvdHMiPuKPsyDQl9Cw0LPRgNGD0LbQsNC10Lwg0YDQsNGB0L/QuNGB0LDQvdC40LUuLi48L2Rpdj4nOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCd0aW1lU2VjJykuc3R5bGUuZGlzcGxheT0nYmxvY2snOwoKICB2YXIgdXJsID0gd2luZG93LmxvY2F0aW9uLm9yaWdpbiArICIvYXBpL3Nsb3RzIiArICc/YWN0aW9uPXNsb3RzJmRhdGU9JyArIGVuY29kZVVSSUNvbXBvbmVudChib29raW5nLmRhdGUpICsgJyZtYXN0ZXI9JyArIGVuY29kZVVSSUNvbXBvbmVudChib29raW5nLm1hc3Rlcik7CgogIGZldGNoKHVybCkKICAgIC50aGVuKGZ1bmN0aW9uKHIpe3JldHVybiByLmpzb24oKTt9KQogICAgLnRoZW4oZnVuY3Rpb24oZGF0YSl7CiAgICAgIHZhciBzbG90cyA9IChkYXRhLnNsb3RzICYmIGRhdGEuc2xvdHMubGVuZ3RoID4gMCkgPyBkYXRhLnNsb3RzIDogW107CiAgICAgIHJlbmRlclRpbWVTbG90cyhzbG90cyk7CiAgICB9KQogICAgLmNhdGNoKGZ1bmN0aW9uKCl7CiAgICAgIHJlbmRlclRpbWVTbG90cyhbXSk7CiAgICB9KTsKfQoKZnVuY3Rpb24gcmVuZGVyVGltZVNsb3RzKHNsb3RzKXsKICB2YXIgdGc9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3RpbWVHJyk7dGcuaW5uZXJIVE1MPScnOwogIGlmKHNsb3RzLmxlbmd0aD09PTApewogICAgdGcuaW5uZXJIVE1MPSc8ZGl2IGNsYXNzPSJuby1zbG90cy1wYW5lbCI+PGRpdiBjbGFzcz0ibm8tc2xvdHMtaWNvbiI+8J+QvjwvZGl2PjxkaXYgY2xhc3M9Im5vLXNsb3RzLXRpdGxlIj7QndC10YIg0LTQvtGB0YLRg9C/0L3Ri9GFINGB0LvQvtGC0L7Qsjxicj7QvdCwINGN0YLRgyDQtNCw0YLRgzwvZGl2PjxkaXYgY2xhc3M9Im5vLXNsb3RzLWRpdmlkZXIiPjwvZGl2PjxkaXYgY2xhc3M9Im5vLXNsb3RzLWN0YSIgb25jbGljaz0ic2hvd1NjcmVlbihcJ2hvbWVTY3JlZW5cJykiPjxkaXYgY2xhc3M9Im5vLXNsb3RzLWN0YS10aXRsZSI+0J3QtSDQvdCw0YjQu9C4INC/0L7QtNGF0L7QtNGP0YnQtdC1INCy0YDQtdC80Y8/PC9kaXY+PGRpdiBjbGFzcz0ibm8tc2xvdHMtY3RhLXN1YiI+0KHQstGP0LbQuNGC0LXRgdGMINGBINC90LDQvNC4INC70Y7QsdGL0Lwg0YPQtNC+0LHQvdGL0Lwg0YHQv9C+0YHQvtCx0L7QvCDigJQ8YnI+0LzRiyDQv9C+0LTQsdC10YDRkdC8INGD0LTQvtCx0L3QvtC1INCy0YDQtdC80Y88L2Rpdj48ZGl2IGNsYXNzPSJuby1zbG90cy1jdGEtYXJyb3ciPtCh0LLRj9C30LDRgtGM0YHRjyDihpI8L2Rpdj48L2Rpdj48L2Rpdj4nOwogICAgcmV0dXJuOwogIH0KICBzbG90cy5mb3JFYWNoKGZ1bmN0aW9uKHQpewogICAgdmFyIGJ0bj1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdidXR0b24nKTtidG4uY2xhc3NOYW1lPSd0YnRuJztidG4udGV4dENvbnRlbnQ9dDsKICAgIGJ0bi5vbmNsaWNrPWZ1bmN0aW9uKCl7CiAgICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy50YnRuJykuZm9yRWFjaChmdW5jdGlvbihiKXtiLmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpO30pOwogICAgICBidG4uY2xhc3NMaXN0LmFkZCgnYWN0aXZlJyk7Ym9va2luZy50aW1lPXQ7CiAgICAgIHNldFRpbWVvdXQoZnVuY3Rpb24oKXtnb1N0ZXAoNSk7YnVpbGRTdW0oKTt9LDMwMCk7CiAgICB9OwogICAgdGcuYXBwZW5kQ2hpbGQoYnRuKTsKICB9KTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgndGltZVNlYycpLnNjcm9sbEludG9WaWV3KHtiZWhhdmlvcjonc21vb3RoJyxibG9jazonbmVhcmVzdCd9KTsKfQoKZnVuY3Rpb24gYnVpbGRTdW0oKXsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3VtQmxvY2snKS5pbm5lckhUTUw9CiAgICAnPGRpdiBjbGFzcz0ic3IiPjxzcGFuIGNsYXNzPSJzbCI+JytUW0xBTkddLnN1bV9icmVlZCsnPC9zcGFuPjxzcGFuIGNsYXNzPSJzdiI+JysoYm9va2luZy5icmVlZERpc3BsYXl8fGJvb2tpbmcuYnJlZWQpKyc8L3NwYW4+PC9kaXY+JysKICAgICc8ZGl2IGNsYXNzPSJzciI+PHNwYW4gY2xhc3M9InNsIj4nK1RbTEFOR10uc3VtX3NlcnZpY2UrJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrKChMQU5HIT09J3J1JyYmU1ZDX1RSQU5TTEFUSU9OU1tib29raW5nLnNlcnZpY2VdKT9TVkNfVFJBTlNMQVRJT05TW2Jvb2tpbmcuc2VydmljZV1bTEFOR106Ym9va2luZy5zZXJ2aWNlKSsnPC9zcGFuPjwvZGl2PicrCiAgICAnPGRpdiBjbGFzcz0ic3IiPjxzcGFuIGNsYXNzPSJzbCI+JytUW0xBTkddLnN1bV9tYXN0ZXIrJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrYm9va2luZy5tYXN0ZXIrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fZ3Jvb20rJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrYm9va2luZy5ncm9vbUhpc3RvcnkrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fZGF0ZSsnPC9zcGFuPjxzcGFuIGNsYXNzPSJzdiI+Jytib29raW5nLmRhdGUrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fdGltZSsnPC9zcGFuPjxzcGFuIGNsYXNzPSJzdiI+Jytib29raW5nLnRpbWUrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fcHJpY2UrJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3AiPicrYm9va2luZy5wcmljZSsnIOKCrDwvc3Bhbj48L2Rpdj4nOwp9Cgpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY29uZmlybUJ0bicpLm9uY2xpY2sgPSBmdW5jdGlvbigpewogIHZhciBuYW1lPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjTmFtZScpLnZhbHVlOwogIHZhciBwaG9uZT1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY1Bob25lJykudmFsdWU7CiAgaWYoIW5hbWV8fCFwaG9uZSl7YWxlcnQoVFtMQU5HXS5hbGVydF9maWxsKTtyZXR1cm47fQogIGlmKCEvXlwrXGR7MTAsfSQvLnRlc3QocGhvbmUudHJpbSgpKSl7YWxlcnQoVFtMQU5HXS5hbGVydF9waG9uZSk7cmV0dXJuO30KICBib29raW5nLm5hbWU9bmFtZTsgYm9va2luZy5waG9uZT1waG9uZTsgYm9va2luZy5lbWFpbD1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY0VtYWlsJykudmFsdWU7IGJvb2tpbmcucGV0PWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjUGV0JykudmFsdWU7IGJvb2tpbmcubGFuZz1MQU5HOwogIGJvb2tpbmcuZHVyYXRpb24gPSBib29raW5nLmJyZWVkID09PSAn0KnQtdC90LrQuCcgPyA2MCA6IChib29raW5nLmJyZWVkICYmIGJvb2tpbmcuYnJlZWQuaW5kZXhPZign0JrQvtGI0LrQsCcpID09PSAwID8gMTIwIDogMTgwKTsKICB2YXIgYnRuPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjb25maXJtQnRuJyk7CiAgYnRuLnRleHRDb250ZW50PVRbTEFOR10uc2VuZGluZzsgYnRuLmRpc2FibGVkPXRydWU7CiAgZmV0Y2goUkFJTFdBWSwgewogICAgbWV0aG9kOidQT1NUJywKICAgIGhlYWRlcnM6eydDb250ZW50LVR5cGUnOidhcHBsaWNhdGlvbi9qc29uJ30sCiAgICBib2R5OkpTT04uc3RyaW5naWZ5KGJvb2tpbmcpCiAgfSkudGhlbihmdW5jdGlvbigpe3Nob3dTdWNjZXNzKCk7fSkuY2F0Y2goZnVuY3Rpb24oKXtzaG93U3VjY2VzcygpO30pOwp9OwoKZnVuY3Rpb24gc2hvd1N1Y2Nlc3MoKXsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnYms1JykuY2xhc3NOYW1lPSdzdGVwJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3VjQmxvY2snKS5jbGFzc0xpc3QuYWRkKCdzaG93Jyk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3Byb2dyZXNzJykuc3R5bGUuZGlzcGxheT0nbm9uZSc7Cn0KCmZ1bmN0aW9uIHJlc2V0QWxsKCl7CiAgYm9va2luZz17YnJlZWQ6JycsYnJlZWREaXNwbGF5OicnLHNlcnZpY2U6JycscHJpY2U6MCxtYXN0ZXI6JycsZ3Jvb21IaXN0b3J5OicnLGRhdGU6JycsdGltZTonJyxsYW5nOidydSd9OwogIHNlbEJyZWVkPW51bGw7IGlucC52YWx1ZT0nJzsgY2xyLmNsYXNzTGlzdC5yZW1vdmUoJ3Nob3cnKTsKICBiYWRnZS5jbGFzc0xpc3QucmVtb3ZlKCdzaG93Jyk7IGJhZGdlLmlubmVySFRNTD0nJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjU2VjJykuc3R5bGUuZGlzcGxheT0nbm9uZSc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3RpbWVTZWMnKS5zdHlsZS5kaXNwbGF5PSdub25lJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3VjQmxvY2snKS5jbGFzc0xpc3QucmVtb3ZlKCdzaG93Jyk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3Byb2dyZXNzJykuc3R5bGUuZGlzcGxheT0nZmxleCc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NOYW1lJykudmFsdWU9Jyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NQaG9uZScpLnZhbHVlPScnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjRW1haWwnKS52YWx1ZT0nJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY1BldCcpLnZhbHVlPScnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjb25maXJtQnRuJykudGV4dENvbnRlbnQ9VFtMQU5HXS5jb25maXJtX2J0bjsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY29uZmlybUJ0bicpLmRpc2FibGVkPWZhbHNlOwogIGdvU3RlcCgxKTsKfQoKdmFyIExBTkcgPSBsb2NhbFN0b3JhZ2UuZ2V0SXRlbSgncmpsYW5nJykgfHwgJ3J1JzsKdmFyIFQgPSB7CiAgcnU6ewogICAgbG9nb190YWc6J9Cf0YDQtdC80LjQsNC70YzQvdGL0Lkg0LPRgNGD0LzQuNC90LMtPGJyPtGB0LDQu9C+0L0g0LIg0KLQsNC70LvQuNC90LUnLAogICAgY2hvb3NlX2hvdzonQ2hvb3NlIGhvdyB0byBjb25uZWN0JywKICAgIGJvb2tfb25saW5lOifQntC90LvQsNC50L0g0LHRgNC+0L3QuNGA0L7QstCw0L3QuNC1JywKICAgIGJvb2tfZmxvdzon0J/QvtGA0L7QtNCwIOKGkiDQo9GB0LvRg9Cz0LAg4oaSINCc0LDRgdGC0LXRgCDihpIg0JLRgNC10LzRjycsCiAgICBvcl9jb250YWN0OifQuNC70Lgg0YHQstGP0LbQuNGC0LXRgdGMINGBINC90LDQvNC4JywKICAgIGNhbGxfdXM6J9Cf0L7Qt9Cy0L7QvdC40YLQtSDQvdCw0LwnLAogICAgYmFjazon4oaQINCd0LDQt9Cw0LQnLAogICAgbG9nb19zdWI6J0dyb29taW5nIMK3INCi0LDQu9C70LjQvScsCiAgICBwc19zZXJ2aWNlOifQo9GB0LvRg9Cz0LAnLHBzX21hc3Rlcjon0JzQsNGB0YLQtdGAJyxwc19wZXQ6J9Cf0LjRgtC+0LzQtdGGJyxwc19kYXRlOifQlNCw0YLQsCcscHNfZGV0YWlsczon0JTQsNC90L3Ri9C1JywKICAgIHN0ZXAxX2xibDonMDEgwrcg0J/QvtGA0L7QtNCwJywKICAgIGJyZWVkX3BoOifQndCw0YfQvdC40YLQtSDQstCy0L7QtNC40YLRjCDQv9C+0YDQvtC00YMuLi4nLAogICAgc3RlcDJfbGJsOicwMiDCtyDQo9GB0LvRg9Cz0LAnLAogICAgc3RlcDJfbWFzdGVyOifQktGL0LHQtdGA0LjRgtC1INC80LDRgdGC0LXRgNCwJywKICAgIHN0ZXAzX2xibDon0JrQsNC6INC00LDQstC90L4g0LLRiyDQv9C+0YHQtdGJ0LDQu9C4INCz0YDRg9C80LjQvdCzPycsCiAgICBnMTon0J/QtdGA0LLRi9C5INGA0LDQtycsZzI6J9Ce0YIgMSDQtNC+IDMg0LzQtdGB0Y/RhtC10LInLGczOifQntGCIDMg0LTQviA2INC80LXRgdGP0YbQtdCyJyxnNDon0JHQvtC70LXQtSA2INC80LXRgdGP0YbQtdCyJywKICAgIHN0ZXA0X2xibDon0JLRi9Cx0LXRgNC40YLQtSDQtNCw0YLRgycsCiAgICBjYWxfYXZhaWw6J9CV0YHRgtGMINGB0LLQvtCx0L7QtNC90L7QtSDQstGA0LXQvNGPJyxjYWxfbm9uZTon0KHQstC+0LHQvtC00L3QvtCz0L4g0LLRgNC10LzQtdC90Lgg0L3QtdGCJywKICAgIHN0ZXA0X3RpbWU6J9CS0YvQsdC10YDQuNGC0LUg0LLRgNC10LzRjycsCiAgICBzdGVwNV9sYmw6J9CS0LDRiNC4INC00LDQvdC90YvQtScsCiAgICBsYmxfbmFtZTon0JjQvNGPJyxwaF9uYW1lOifQktCw0YjQtSDQuNC80Y8nLAogICAgbGJsX3Bob25lOifQotC10LvQtdGE0L7QvScsbGJsX2VtYWlsOidFbWFpbCcsCiAgICBsYmxfcGV0OifQmtC70LjRh9C60LAg0L/QuNGC0L7QvNGG0LAnLHBoX29wdGlvbmFsOifQndC10L7QsdGP0LfQsNGC0LXQu9GM0L3QvicsCiAgICBjb25maXJtX2J0bjon0J/QvtC00YLQstC10YDQtNC40YLRjCDQt9Cw0L/QuNGB0YwnLAogICAgc3VjY2Vzc190aXRsZTon0JfQsNC/0LjRgdGMINC/0YDQuNC90Y/RgtCwIScsCiAgICBzdWNjZXNzX3N1Yjon0JzRiyDRgdCy0Y/QttC10LzRgdGPINGBINCy0LDQvNC4INC00LvRjyDQv9C+0LTRgtCy0LXRgNC20LTQtdC90LjRjy48YnI+0KHQv9Cw0YHQuNCx0L4sINGH0YLQviDQstGL0LHRgNCw0LvQuCBSJmFtcDtKIEdyb29taW5nIScsCiAgICB0b19ob21lOifihpAg0J3QsCDQs9C70LDQstC90YPRjicsCiAgICBhbGVydF9maWxsOifQktCy0LXQtNC40YLQtSDQuNC80Y8g0Lgg0YLQtdC70LXRhNC+0L0nLGFsZXJ0X3Bob25lOifQktCy0LXQtNC40YLQtSDQvdC+0LzQtdGAINCyINGE0L7RgNC80LDRgtC1ICszNzIxMjM0NTY3OCcsCiAgICBzZW5kaW5nOifQntGC0L/RgNCw0LLQu9GP0LXQvC4uLicsCiAgICBzdW1fYnJlZWQ6J9Cf0L7RgNC+0LTQsCcsc3VtX3NlcnZpY2U6J9Cj0YHQu9GD0LPQsCcsc3VtX21hc3Rlcjon0JzQsNGB0YLQtdGAJyxzdW1fZ3Jvb206J9Cf0L7RgdC70LXQtNC90LjQuSDQs9GA0YPQvCcsc3VtX2RhdGU6J9CU0LDRgtCwJyxzdW1fdGltZTon0JLRgNC10LzRjycsc3VtX3ByaWNlOifQodGC0L7QuNC80L7RgdGC0YwnLAogICAgbW9udGhzOlsn0K/QvdCy0LDRgNGMJywn0KTQtdCy0YDQsNC70YwnLCfQnNCw0YDRgicsJ9CQ0L/RgNC10LvRjCcsJ9Cc0LDQuScsJ9CY0Y7QvdGMJywn0JjRjtC70YwnLCfQkNCy0LPRg9GB0YInLCfQodC10L3RgtGP0LHRgNGMJywn0J7QutGC0Y/QsdGA0YwnLCfQndC+0Y/QsdGA0YwnLCfQlNC10LrQsNCx0YDRjCddCiAgfSwKICBlbjp7CiAgICBsb2dvX3RhZzonUHJlbWl1bSBncm9vbWluZzxicj5zYWxvbiBpbiBUYWxsaW5uJywKICAgIGNob29zZV9ob3c6J0Nob29zZSBob3cgdG8gY29ubmVjdCcsCiAgICBib29rX29ubGluZTonQm9vayBPbmxpbmUnLAogICAgYm9va19mbG93OidCcmVlZCDihpIgU2VydmljZSDihpIgTWFzdGVyIOKGkiBUaW1lJywKICAgIG9yX2NvbnRhY3Q6J29yIGNvbnRhY3QgdXMnLAogICAgY2FsbF91czonQ2FsbCBVcycsCiAgICBiYWNrOifihpAgQmFjaycsCiAgICBsb2dvX3N1YjonR3Jvb21pbmcgwrcgVGFsbGlubicsCiAgICBwc19zZXJ2aWNlOidTZXJ2aWNlJyxwc19tYXN0ZXI6J01hc3RlcicscHNfcGV0OidQZXQnLHBzX2RhdGU6J0RhdGUnLHBzX2RldGFpbHM6J0RldGFpbHMnLAogICAgc3RlcDFfbGJsOicwMSDCtyBEb2cgYnJlZWQnLAogICAgYnJlZWRfcGg6J1N0YXJ0IHR5cGluZyBicmVlZC4uLicsCiAgICBzdGVwMl9sYmw6JzAyIMK3IFNlcnZpY2UnLAogICAgc3RlcDJfbWFzdGVyOidDaG9vc2UgbWFzdGVyJywKICAgIHN0ZXAzX2xibDonSG93IGxvbmcgYWdvIHdhcyB5b3VyIGxhc3QgZ3Jvb21pbmc/JywKICAgIGcxOidGaXJzdCB0aW1lJyxnMjonMeKAkzMgbW9udGhzIGFnbycsZzM6JzPigJM2IG1vbnRocyBhZ28nLGc0OidPdmVyIDYgbW9udGhzJywKICAgIHN0ZXA0X2xibDonQ2hvb3NlIGRhdGUnLAogICAgY2FsX2F2YWlsOidBdmFpbGFibGUnLGNhbF9ub25lOidOb3QgYXZhaWxhYmxlJywKICAgIHN0ZXA0X3RpbWU6J0Nob29zZSB0aW1lJywKICAgIHN0ZXA1X2xibDonWW91ciBkZXRhaWxzJywKICAgIGxibF9uYW1lOidOYW1lJyxwaF9uYW1lOidZb3VyIG5hbWUnLAogICAgbGJsX3Bob25lOidQaG9uZScsbGJsX2VtYWlsOidFbWFpbCcsCiAgICBsYmxfcGV0OiJQZXQncyBuYW1lIixwaF9vcHRpb25hbDonT3B0aW9uYWwnLAogICAgY29uZmlybV9idG46J0NvbmZpcm0gYm9va2luZycsCiAgICBzdWNjZXNzX3RpdGxlOidCb29raW5nIGNvbmZpcm1lZCEnLAogICAgc3VjY2Vzc19zdWI6J1dlIHdpbGwgY29udGFjdCB5b3UgdG8gY29uZmlybS48YnI+VGhhbmsgeW91IGZvciBjaG9vc2luZyBSJmFtcDtKIEdyb29taW5nIScsCiAgICB0b19ob21lOifihpAgSG9tZScsCiAgICBhbGVydF9maWxsOidQbGVhc2UgZW50ZXIgbmFtZSBhbmQgcGhvbmUnLGFsZXJ0X3Bob25lOidFbnRlciBwaG9uZSBudW1iZXIgaW4gZm9ybWF0ICszNzIxMjM0NTY3OCcsCiAgICBzZW5kaW5nOidTZW5kaW5nLi4uJywKICAgIHN1bV9icmVlZDonQnJlZWQnLHN1bV9zZXJ2aWNlOidTZXJ2aWNlJyxzdW1fbWFzdGVyOidNYXN0ZXInLHN1bV9ncm9vbTonTGFzdCBncm9vbWluZycsc3VtX2RhdGU6J0RhdGUnLHN1bV90aW1lOidUaW1lJyxzdW1fcHJpY2U6J1ByaWNlJywKICAgIG1vbnRoczpbJ0phbnVhcnknLCdGZWJydWFyeScsJ01hcmNoJywnQXByaWwnLCdNYXknLCdKdW5lJywnSnVseScsJ0F1Z3VzdCcsJ1NlcHRlbWJlcicsJ09jdG9iZXInLCdOb3ZlbWJlcicsJ0RlY2VtYmVyJ10KICB9LAogIGV0OnsKICAgIGxvZ29fdGFnOidFc21ha2xhc3NpbGluZSBob29sZHVzdGVlbnVzPGJyPlRhbGxpbm5hcycsCiAgICBjaG9vc2VfaG93OidWYWxpIMO8aGVuZHVzdmlpcycsCiAgICBib29rX29ubGluZTonQnJvbmVlcmkgdmVlYmlzJywKICAgIGJvb2tfZmxvdzonVMO1dWcg4oaSIFRlZW51cyDihpIgTWVpc3RlciDihpIgQWVnJywKICAgIG9yX2NvbnRhY3Q6J3bDtWkgdsO1dGEgw7xoZW5kdXN0JywKICAgIGNhbGxfdXM6J0hlbGlzdGEgbWVpbGUnLAogICAgYmFjazon4oaQIFRhZ2FzaScsCiAgICBsb2dvX3N1YjonR3Jvb21pbmcgwrcgVGFsbGlubicsCiAgICBwc19zZXJ2aWNlOidUZWVudXMnLHBzX21hc3RlcjonTWVpc3RlcicscHNfcGV0OidMZW1taWtsb29tJyxwc19kYXRlOidLdXVww6RldicscHNfZGV0YWlsczonQW5kbWVkJywKICAgIHN0ZXAxX2xibDonMDEgwrcgS29lcmEgdMO1dWcnLAogICAgYnJlZWRfcGg6J0FsdXN0YWdlIHTDtXUgc2lzZXN0YW1pc3QuLi4nLAogICAgc3RlcDJfbGJsOicwMiDCtyBUZWVudXMnLAogICAgc3RlcDJfbWFzdGVyOidWYWxpIG1laXN0ZXInLAogICAgc3RlcDNfbGJsOidNaWxsYWwga8OkaXNpdGUgdmlpbWF0aSBncm9vbWluZ3VzPycsCiAgICBnMTonRXNpbWVzdCBrb3JkYScsZzI6JzHigJMzIGt1dWQgdGFnYXNpJyxnMzonM+KAkzYga3V1ZCB0YWdhc2knLGc0OifDnGxlIDYga3V1JywKICAgIHN0ZXA0X2xibDonVmFsaSBrdXVww6RldicsCiAgICBjYWxfYXZhaWw6J1ZhYnUgYWVndSBvbicsY2FsX25vbmU6J1ZhYnUgYWVndSBwb2xlJywKICAgIHN0ZXA0X3RpbWU6J1ZhbGkga2VsbGFhZWcnLAogICAgc3RlcDVfbGJsOidUZWllIGFuZG1lZCcsCiAgICBsYmxfbmFtZTonTmltaScscGhfbmFtZTonVGVpZSBuaW1pJywKICAgIGxibF9waG9uZTonVGVsZWZvbicsbGJsX2VtYWlsOidFbWFpbCcsCiAgICBsYmxfcGV0OidMZW1taWtsb29tYSBuaW1pJyxwaF9vcHRpb25hbDonVmFsaWt1bGluZScsCiAgICBjb25maXJtX2J0bjonS2lubml0YSBicm9uZWVyaW5nJywKICAgIHN1Y2Nlc3NfdGl0bGU6J0Jyb25lZXJpbmcga2lubml0YXR1ZCEnLAogICAgc3VjY2Vzc19zdWI6J1bDtXRhbWUgdGVpZWdhIMO8aGVuZHVzdCBraW5uaXRhbWlzZWtzLjxicj5Uw6RuYW1lLCBldCB2YWxpc2l0ZSBSJmFtcDtKIEdyb29taW5nIScsCiAgICB0b19ob21lOifihpAgQXZhbGVoZWxlJywKICAgIGFsZXJ0X2ZpbGw6J1BhbHVuIHNpc2VzdGFnZSBuaW1pIGphIHRlbGVmb24nLGFsZXJ0X3Bob25lOidTaXNlc3RhZ2UgdGVsZWZvbmludW1iZXIgdm9ybWluZ3VzICszNzIxMjM0NTY3OCcsCiAgICBzZW5kaW5nOidTYWFkYW4uLi4nLAogICAgc3VtX2JyZWVkOidUw7V1Zycsc3VtX3NlcnZpY2U6J1RlZW51cycsc3VtX21hc3RlcjonTWVpc3Rlcicsc3VtX2dyb29tOidWaWltYW5lIGdyb29taW5nJyxzdW1fZGF0ZTonS3V1cMOkZXYnLHN1bV90aW1lOidLZWxsYWFlZycsc3VtX3ByaWNlOidIaW5kJywKICAgIG1vbnRoczpbJ0phYW51YXInLCdWZWVicnVhcicsJ03DpHJ0cycsJ0FwcmlsbCcsJ01haScsJ0p1dW5pJywnSnV1bGknLCdBdWd1c3QnLCdTZXB0ZW1iZXInLCdPa3Rvb2JlcicsJ05vdmVtYmVyJywnRGV0c2VtYmVyJ10KICB9Cn07CgpmdW5jdGlvbiBzZXRMYW5nKGwpewogIExBTkc9bDsKICBsb2NhbFN0b3JhZ2Uuc2V0SXRlbSgncmpsYW5nJyxsKTsKICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcubGFuZy1idG4nKS5mb3JFYWNoKGZ1bmN0aW9uKGIpewogICAgYi5jbGFzc0xpc3QudG9nZ2xlKCdhY3RpdmUnLCBiLnRleHRDb250ZW50LnRvTG93ZXJDYXNlKCk9PT1sKTsKICB9KTsKICB2YXIgdHI9VFtsXTsKICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCdbZGF0YS1pMThuXScpLmZvckVhY2goZnVuY3Rpb24oZWwpewogICAgdmFyIGs9ZWwuZ2V0QXR0cmlidXRlKCdkYXRhLWkxOG4nKTsKICAgIGlmKHRyW2tdIT09dW5kZWZpbmVkKSBlbC5pbm5lckhUTUw9dHJba107CiAgfSk7CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnW2RhdGEtaTE4bi1waF0nKS5mb3JFYWNoKGZ1bmN0aW9uKGVsKXsKICAgIHZhciBrPWVsLmdldEF0dHJpYnV0ZSgnZGF0YS1pMThuLXBoJyk7CiAgICBpZih0cltrXSE9PXVuZGVmaW5lZCkgZWwucGxhY2Vob2xkZXI9dHJba107CiAgfSk7CiAgTU9OVEhTPXRyLm1vbnRoczsKICBidWlsZENhbCgpOwogIC8vIFJlLXJlbmRlciBiYWRnZSBhbmQgc2VydmljZXMgaWYgYnJlZWQgYWxyZWFkeSBzZWxlY3RlZAogIGlmKHNlbEJyZWVkKXsKICAgIHZhciBiZj1sPT09J2VuJz8nYnJlZWRfZW4nOmw9PT0nZXQnPydicmVlZF9ldCc6J2JyZWVkJzsKICAgIHZhciBkYj1zZWxCcmVlZFtiZl18fHNlbEJyZWVkLmJyZWVkOwogICAgYm9va2luZy5icmVlZERpc3BsYXk9ZGI7CiAgICB2YXIgYm5FbD1kb2N1bWVudC5xdWVyeVNlbGVjdG9yKCcjc0JhZGdlIC5ibmFtZScpOwogICAgaWYoYm5FbCkgYm5FbC50ZXh0Q29udGVudD1kYjsKICAgIHZhciBiY0VsPWRvY3VtZW50LnF1ZXJ5U2VsZWN0b3IoJyNzQmFkZ2UgLmJjaGcnKTsKICAgIGlmKGJjRWwpIGJjRWwudGV4dENvbnRlbnQ9bD09PSdlbic/J0NoYW5nZSc6bD09PSdldCc/J011dWRhJzon0JjQt9C80LXQvdC40YLRjCc7CiAgICBpZihkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjU2VjJykuc3R5bGUuZGlzcGxheSE9PSdub25lJykgcmVuZGVyU3ZjcyhzZWxCcmVlZCk7CiAgICB2YXIgc249ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y05vdGUnKTsKICAgIGlmKHNuKXsKICAgICAgdmFyIG50PWw9PT0nZW4nPydQbGVhc2Ugbm90ZSc6bD09PSdldCc/J1BhbmdlIHTDpGhlbGUnOifQktCw0LbQvdC+INC30L3QsNGC0YwnOwogICAgICB2YXIgbmI9bD09PSdlbic/J0ZpbmFsIHByaWNlIGRlcGVuZHMgb24gY29hdCBjb25kaXRpb24gYW5kIHBldCBiZWhhdmlvdXIuPGJyPkRlbWF0dGluZyBmcm9tIDUg4oKsLjxicj5BZ2dyZXNzaXZlIGJlaGF2aW91ciBzdXJjaGFyZ2UgbWF5IGFwcGx5OiArNTAlLic6bD09PSdldCc/J0zDtXBsaWsgaGluZCBzw7VsdHViIGthcnZhc3Rpa3Ugc2Vpc3VuZGlzdCBqYSBsZW1taWtsb29tYSBrw6RpdHVtaXNlc3QuPGJyPktvbHRzdW5pdGUgbGFodGloYXJ1dGFtaW5lIGFsYXRlcyA1IOKCrC48YnI+QWdyZXNzaWl2c2Uga8OkaXR1bWlzZSBrb3JyYWwgdsO1aWIgbGlzYW5kdWRhIDUwJSBqdXVyZGVoaW5kbHVzLic6J9Ce0LrQvtC90YfQsNGC0LXQu9GM0L3QsNGPINGB0YLQvtC40LzQvtGB0YLRjCDQt9Cw0LLQuNGB0LjRgiDQvtGCINGB0L7RgdGC0L7Rj9C90LjRjyDRiNC10YDRgdGC0Lgg0Lgg0L/QvtCy0LXQtNC10L3QuNGPINC/0LjRgtC+0LzRhtCwLjxicj7QoNCw0LfQsdC+0YAg0LrQvtC70YLRg9C90L7QsiDigJQg0L7RgiA1IOKCrC48YnI+0J/RgNC4INCw0LPRgNC10YHRgdC40LLQvdC+0Lwg0L/QvtCy0LXQtNC10L3QuNC4INC80L7QttC10YIg0L/RgNC40LzQtdC90Y/RgtGM0YHRjyDQtNC+0L/Qu9Cw0YLQsCA1MCUuJzsKICAgICAgc24uaW5uZXJIVE1MPSc8ZGl2IHN0eWxlPSJmb250LXNpemU6MC44MzhyZW07bGV0dGVyLXNwYWNpbmc6LjE1ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbTo4cHg7Zm9udC13ZWlnaHQ6NjAwO2ZvbnQtZmFtaWx5OlwnTW9udHNlcnJhdFwnLHNhbnMtc2VyaWYiPicrbnQrJzwvZGl2PjxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxLjAyNXJlbTtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjEuODtmb250LWZhbWlseTpcJ01vbnRzZXJyYXRcJyxzYW5zLXNlcmlmIj4nK25iKyc8L2Rpdj4nOwogICAgfQogIH0KfQoKLy8gQXBwbHkgc2F2ZWQgbGFuZ3VhZ2Ugb24gbG9hZAooZnVuY3Rpb24oKXsgc2V0TGFuZyhMQU5HKTsgfSkoKTsKCi8vIENhbGxiYWNrIGZvcm0KZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NhbGxiYWNrQnRuJykub25jbGljayA9IGZ1bmN0aW9uKCl7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia01vZGFsJykuc3R5bGUuZGlzcGxheSA9ICdmbGV4JzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrTmFtZScpLnZhbHVlID0gJyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1Bob25lJykudmFsdWUgPSAnJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrU3VjY2VzcycpLnN0eWxlLmRpc3BsYXkgPSAnbm9uZSc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1N1Ym1pdCcpLnN0eWxlLmRpc3BsYXkgPSAnYmxvY2snOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtDbG9zZScpLnRleHRDb250ZW50ID0gJ9Ce0YLQvNC10L3QsCc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1N1Ym1pdCcpLnRleHRDb250ZW50ID0gJ9Ce0YLQv9GA0LDQstC40YLRjCc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1N1Ym1pdCcpLmRpc2FibGVkID0gZmFsc2U7Cn07CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtDbG9zZScpLm9uY2xpY2sgPSBmdW5jdGlvbigpewogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtNb2RhbCcpLnN0eWxlLmRpc3BsYXkgPSAnbm9uZSc7Cn07CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtTdWJtaXQnKS5vbmNsaWNrID0gZnVuY3Rpb24oKXsKICB2YXIgbmFtZSA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtOYW1lJykudmFsdWUudHJpbSgpOwogIHZhciBwaG9uZSA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtQaG9uZScpLnZhbHVlLnRyaW0oKS5yZXBsYWNlKC9cRC9nLCcnKTsKICBpZighbmFtZSB8fCAhcGhvbmUpe2FsZXJ0KCfQktCy0LXQtNC40YLQtSDQuNC80Y8g0Lgg0YLQtdC70LXRhNC+0L0nKTtyZXR1cm47fQogIHZhciBidG4gPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrU3VibWl0Jyk7CiAgYnRuLnRleHRDb250ZW50ID0gJ9Ce0YLQv9GA0LDQstC70Y/QtdC8Li4uJzsgYnRuLmRpc2FibGVkID0gdHJ1ZTsKICBmZXRjaCgnL2FwaS9jYWxsYmFjaycsewogICAgbWV0aG9kOidQT1NUJywKICAgIGhlYWRlcnM6eydDb250ZW50LVR5cGUnOidhcHBsaWNhdGlvbi9qc29uJ30sCiAgICBib2R5OkpTT04uc3RyaW5naWZ5KHtuYW1lOm5hbWUsIHBob25lOicrMzcyJytwaG9uZX0pCiAgfSkudGhlbihmdW5jdGlvbigpewogICAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1N1Y2Nlc3MnKS5zdHlsZS5kaXNwbGF5ID0gJ2Jsb2NrJzsKICAgIGJ0bi5zdHlsZS5kaXNwbGF5ID0gJ25vbmUnOwogICAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia0Nsb3NlJykudGV4dENvbnRlbnQgPSAn4oaQINCX0LDQutGA0YvRgtGMJzsKICAgIHNldFRpbWVvdXQoZnVuY3Rpb24oKXtkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrTW9kYWwnKS5zdHlsZS5kaXNwbGF5PSdub25lJzt9LDMwMDApOwogIH0pLmNhdGNoKGZ1bmN0aW9uKCl7CiAgICBidG4udGV4dENvbnRlbnQgPSAn0J7RgtC/0YDQsNCy0LjRgtGMJzsgYnRuLmRpc2FibGVkID0gZmFsc2U7CiAgICBhbGVydCgn0J7RiNC40LHQutCwLiDQn9C+0L/RgNC+0LHRg9C50YLQtSDQtdGJ0ZEg0YDQsNC3LicpOwogIH0pOwp9OwoKPC9zY3JpcHQ+CjwvYm9keT4KPC9odG1sPgo="



@app.route("/api/available_days")
def api_available_days():
    month = request.args.get("month", "")
    year = request.args.get("year", "")
    master = request.args.get("master", "")
    gs_params = {"action": "available_days", "month": month, "year": year, "master": master}
    print(f"[api/available_days] GET {_gs_url(gs_params)}", flush=True)
    if not GOOGLE_SCRIPT:
        print("[api/available_days] GOOGLE_SCRIPT not configured", flush=True)
        resp = jsonify({"available": [], "error": "GOOGLE_SCRIPT not configured"})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp
    try:
        r = requests.get(GOOGLE_SCRIPT, params=gs_params, timeout=25)
        print(f"[api/available_days] → {r.status_code}: {r.text[:300]}", flush=True)
        resp = jsonify(r.json())
    except Exception as e:
        print(f"[api/available_days] error: {e}", flush=True)
        resp = jsonify({"available": [], "error": str(e)})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp

@app.route("/api/slots")
def api_slots():
    date = request.args.get("date", "")
    master = request.args.get("master", "")
    gs_params = {"action": "slots", "date": date, "master": master}
    print(f"[api/slots] GET {_gs_url(gs_params)}", flush=True)
    if not GOOGLE_SCRIPT:
        print("[api/slots] GOOGLE_SCRIPT not configured", flush=True)
        resp = jsonify({"slots": [], "error": "GOOGLE_SCRIPT not configured"})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp
    try:
        r = requests.get(GOOGLE_SCRIPT, params=gs_params, timeout=25)
        print(f"[api/slots] → {r.status_code}: {r.text[:300]}", flush=True)
        resp = jsonify(r.json())
    except Exception as e:
        print(f"[api/slots] error: {e}", flush=True)
        resp = jsonify({"slots": [], "error": str(e)})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp
@app.route("/app")
def booking_app():
    import base64
    html = base64.b64decode(BOOKING_HTML_B64).decode("utf-8")
    html = html.replace("https://dynamic-cooperation-production-dd95.up.railway.app/book", "/book")
    html = html.replace(
        '<div class="fg"><label class="fl">Кличка питомца</label>',
        '<div class="fg"><label class="fl">Email</label><input class="fi" id="cEmail" type="email" placeholder="email@example.com"></div><div class="fg"><label class="fl">Кличка питомца</label>'
    )
    html = html.replace(
        "booking.pet=document.getElementById('cPet').value;",
        "booking.pet=document.getElementById('cPet').value; booking.email=document.getElementById('cEmail').value;"
    )
    # ── Phone validation: must start with + and have at least 10 digits ──────
    html = html.replace(
        "if(!name||!phone){alert(T[LANG].alert_fill);return;}",
        "if(!name||!phone){alert(T[LANG].alert_fill);return;}"
        "if(!/^\\+\\d{10,}$/.test(phone.trim())){alert(T[LANG].alert_phone);return;}"
    )
    html = html.replace(
        "alert_fill:'Введите имя и телефон',",
        "alert_fill:'Введите имя и телефон',alert_phone:'Введите номер в формате +37212345678',"
    )
    html = html.replace(
        "alert_fill:'Please enter name and phone',",
        "alert_fill:'Please enter name and phone',alert_phone:'Enter phone number in format +37212345678',"
    )
    html = html.replace(
        "alert_fill:'Palun sisestage nimi ja telefon',",
        "alert_fill:'Palun sisestage nimi ja telefon',alert_phone:'Sisestage telefoninumber vormingus +37212345678',"
    )
    anna_filter_script = """
<script>
(function(){
  var ANNA_KEYS = ["француз","такса","джек","рассел","самоед","шелти","корги","лабрадор","чихуа"];
  function annaCanGroom(breed){
    if(!breed) return true;
    var b = String(breed).toLowerCase();
    if(b.indexOf("такса") !== -1){
      if(b.indexOf("жёстк") !== -1 || b.indexOf("жестк") !== -1) return false;
      if(b.indexOf("кроличь") !== -1) return false;
      return true;
    }
    if(b.indexOf("джек") !== -1 || b.indexOf("рассел") !== -1){
      if(b.indexOf("жёстк") !== -1 || b.indexOf("жестк") !== -1) return false;
      if(b.indexOf("брокен") !== -1) return false;
      return true;
    }
    for(var i=0; i<ANNA_KEYS.length; i++){
      if(b.indexOf(ANNA_KEYS[i]) !== -1) return true;
    }
    return false;
  }
  function updateAnnaVisibility(){
    var annaBtn = document.querySelector('.mbtn[data-master="Анна"]');
    if(!annaBtn) return;
    var b = (window.booking && window.booking.breed) ? window.booking.breed : "";
    if(annaCanGroom(b)){
      annaBtn.style.display = "";
    } else {
      annaBtn.style.display = "none";
      if(window.booking && window.booking.master === "Анна"){
        window.booking.master = "";
        annaBtn.classList.remove("active");
      }
    }
  }
  function install(){
    if(!window.booking){ setTimeout(install, 100); return; }
    updateAnnaVisibility();
    try{
      var _breed = window.booking.breed;
      Object.defineProperty(window.booking, "breed", {
        get: function(){ return _breed; },
        set: function(v){ _breed = v; updateAnnaVisibility(); },
        configurable: true
      });
    }catch(e){
      var last = window.booking.breed;
      setInterval(function(){
        if(window.booking.breed !== last){
          last = window.booking.breed;
          updateAnnaVisibility();
        }
      }, 300);
    }
  }
  if(document.readyState === "loading"){
    document.addEventListener("DOMContentLoaded", install);
  } else {
    install();
  }
})();
</script>
"""
    html = html.replace("</body>", anna_filter_script + "</body>")
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


# ── WEBHOOKS ───────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token in (VERIFY_TOKEN, INSTAGRAM_VERIFY_TOKEN):
        return challenge, 200
    return "Forbidden", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    try:
        obj = data.get("object", "")
        entry = data.get("entry", [{}])[0]
        if obj == "instagram" or "messaging" in entry:
            for msg_event in entry.get("messaging", []):
                sender_id = msg_event.get("sender", {}).get("id", "")
                recipient_id = msg_event.get("recipient", {}).get("id", "")
                if sender_id == recipient_id: continue
                msg = msg_event.get("message", {})
                if msg.get("is_echo"): continue
                text = msg.get("text", "")
                if sender_id and text:
                    handle_message(sender_id, text, "instagram")
            return "ok", 200
        value = entry.get("changes", [{}])[0].get("value", {})
        messages = value.get("messages", [])
        if not messages: return "ok", 200
        msg = messages[0]
        phone = msg["from"]
        text = msg.get("text", {}).get("body", "")
        if text:
            handle_message(phone, text, "whatsapp")
    except Exception as e:
        print("Error:", str(e))
    return "ok", 200

# ── ADMIN API ──────────────────────────────────────────────────────────────
@app.route("/admin/api/toggle", methods=["POST"])
def api_toggle():
    global jarvis_enabled, manual_mode, pause_until
    data = request.get_json()
    jarvis_enabled = bool(data.get("enabled", True))
    manual_mode = False; pause_until = None
    return jsonify({"ok": True, "message": "Jarvis " + ("включён ✅" if jarvis_enabled else "выключен ❌")})

@app.route("/admin/api/toggle-instagram", methods=["POST"])
def api_toggle_instagram():
    global instagram_enabled
    instagram_enabled = bool(request.get_json().get("enabled", True))
    return jsonify({"ok": True, "message": "Instagram " + ("включён ✅" if instagram_enabled else "выключен ❌")})

@app.route("/admin/api/messages")
def api_messages():
    result = []
    for phone, info in clients.items():
        last = info.get("last_seen")
        result.append({"phone": phone, "ts": last.isoformat() if isinstance(last, datetime) else "", "last_text": info.get("last_text", ""), "channel": info.get("channel", "whatsapp")})
    return jsonify(result)

@app.route("/admin/api/message-log")
def api_message_log():
    return jsonify({"log": list(reversed(_msg_log[-20:])), "counts": _channel_counts})

@app.route("/admin/api/send", methods=["POST"])
def api_send():
    data = request.get_json() or {}
    phone = data.get("phone", "").strip()
    text = data.get("text", "").strip()
    if not phone or not text:
        return jsonify({"ok": False, "error": "phone and text required"}), 400
    try:
        send_whatsapp(phone, text)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/cancel-booking", methods=["POST"])
def api_cancel_booking():
    data = request.get_json() or {}
    phone = data.get("phone", "").strip()
    if not phone:
        return jsonify({"success": False, "error": "phone required"}), 400
    return jsonify(_gs_cancel(phone))

@app.route("/api/reschedule-booking", methods=["POST"])
def api_reschedule_booking():
    data = request.get_json() or {}
    phone    = data.get("phone", "").strip()
    new_date = data.get("newDate", "").strip()
    new_time = data.get("newTime", "").strip()
    if not all([phone, new_date, new_time]):
        return jsonify({"success": False, "error": "phone, newDate, newTime required"}), 400
    return jsonify(_gs_reschedule(phone, new_date, new_time))

# ── ElevenLabs voice agent endpoints ─────────────────────────────────────────

@app.route("/api/elevenlabs/book", methods=["POST"])
def elevenlabs_book():
    """Accept booking from ElevenLabs voice agent and forward to Google Script.
    Required JSON fields: breed, service, date (DD.MM.YYYY), time (HH:MM), name, phone.
    Optional: pet, master, price, lang (ru/en/et), breedDisplay.
    """
    data = request.get_json() or {}
    missing = [f for f in ("breed", "service", "date", "time", "name", "phone")
               if not data.get(f)]
    if missing:
        return jsonify({"success": False,
                        "error": f"Missing required fields: {', '.join(missing)}"}), 400

    payload = {
        "breed":        data.get("breed", ""),
        "breedDisplay": data.get("breedDisplay") or data.get("breed", ""),
        "service":      data.get("service", ""),
        "price":        data.get("price", 0),
        "date":         data.get("date", ""),
        "time":         data.get("time", ""),
        "name":         data.get("name", ""),
        "pet":          data.get("pet", ""),
        "master":       data.get("master", ""),
        "phone":        data.get("phone", ""),
        "lang":         data.get("lang", "ru"),
        "source":       "elevenlabs",
    }
    print(f"[elevenlabs/book] {payload}", flush=True)

    if not GOOGLE_SCRIPT:
        return jsonify({"success": False, "error": "GOOGLE_SCRIPT not configured"}), 500
    try:
        r = requests.get(GOOGLE_SCRIPT, params=payload, timeout=15)
        print(f"[elevenlabs/book] GS → {r.status_code}: {r.text[:300]}", flush=True)
        return jsonify({"success": True, "booking": payload})
    except Exception as e:
        print(f"[elevenlabs/book] error: {e}", flush=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/elevenlabs/slots")
def elevenlabs_slots():
    """Return available time slots for a given date.
    Query params: date=DD.MM.YYYY (required), master= (optional).
    Returns: {success, date, slots: ["10:00", "11:00", ...]}
    """
    date   = request.args.get("date", "").strip()
    master = request.args.get("master", "").strip()

    if not date:
        return jsonify({"success": False, "slots": [],
                        "error": "date parameter required (DD.MM.YYYY)"}), 400
    if not GOOGLE_SCRIPT:
        return jsonify({"success": False, "slots": [],
                        "error": "GOOGLE_SCRIPT not configured"}), 500
    try:
        r = requests.get(GOOGLE_SCRIPT,
                         params={"action": "slots", "date": date, "master": master},
                         timeout=25)
        slots = [str(s) for s in r.json().get("slots", []) if str(s).endswith(":00")]
        print(f"[elevenlabs/slots] {date} master={master!r} → {slots}", flush=True)
        return jsonify({"success": True, "date": date, "slots": slots})
    except Exception as e:
        print(f"[elevenlabs/slots] error: {e}", flush=True)
        return jsonify({"success": False, "slots": [], "error": str(e)}), 500


@app.route("/api/elevenlabs/available_days")
def elevenlabs_available_days():
    """Return available days for the current (or specified) month.
    Query params: month= (1-12), year= (YYYY), master= (optional).
    Returns: {success, month, year, available_days: ["01.06.2026", ...]}
    """
    import datetime as _dt
    _now   = _dt.date.today()
    month  = request.args.get("month",  str(_now.month)).strip()
    year   = request.args.get("year",   str(_now.year)).strip()
    master = request.args.get("master", "").strip()

    if not GOOGLE_SCRIPT:
        return jsonify({"success": False, "available_days": [],
                        "error": "GOOGLE_SCRIPT not configured"}), 500
    try:
        r = requests.get(GOOGLE_SCRIPT,
                         params={"action": "available_days",
                                 "month": month, "year": year, "master": master},
                         timeout=25)
        available = r.json().get("available", [])
        print(f"[elevenlabs/available_days] {month}/{year} master={master!r} → {available}",
              flush=True)
        return jsonify({"success": True, "month": month, "year": year,
                        "available_days": available})
    except Exception as e:
        print(f"[elevenlabs/available_days] error: {e}", flush=True)
        return jsonify({"success": False, "available_days": [], "error": str(e)}), 500


@app.route("/api/elevenlabs/notify", methods=["POST"])
def elevenlabs_notify():
    """Send SMS to admin when voice agent captures a new lead.
    Required JSON fields: breed, service, phone.
    """
    data = request.get_json() or {}
    breed   = data.get("breed", "").strip()
    service = data.get("service", "").strip()
    phone   = data.get("phone", "").strip()

    missing = [f for f in ("breed", "service", "phone") if not data.get(f)]
    if missing:
        return jsonify({"success": False,
                        "error": f"Missing required fields: {', '.join(missing)}"}), 400

    print(f"[elevenlabs/notify] breed={breed!r} service={service!r} phone={phone!r}", flush=True)

    twilio_sid   = os.environ.get("TWILIO_ACCOUNT_SID")
    twilio_token = os.environ.get("TWILIO_AUTH_TOKEN")
    twilio_from  = os.environ.get("TWILIO_PHONE", "+37266922128")
    admin_phone  = "+37266922128"

    if not (twilio_sid and twilio_token):
        return jsonify({"success": False, "error": "Twilio not configured"}), 500

    sms_body = f"Новый клиент с голосового агента: {breed}, {service}, тел: {phone}"
    sms_url  = f"https://api.twilio.com/2010-04-01/Accounts/{twilio_sid}/Messages.json"
    try:
        resp = requests.post(
            sms_url,
            auth=(twilio_sid, twilio_token),
            data={"From": twilio_from, "To": admin_phone, "Body": sms_body},
            timeout=10
        )
        print(f"[elevenlabs/notify] SMS → {resp.status_code}: {resp.text[:200]}", flush=True)
        if resp.status_code == 201:
            return jsonify({"success": True, "message": "Admin notified"})
        return jsonify({"success": False, "error": f"SMS error {resp.status_code}"}), 500
    except Exception as e:
        print(f"[elevenlabs/notify] error: {e}", flush=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/cron/reminders")
def cron_reminders():
    """Send SMS reminders for tomorrow's bookings.
    Railway cron: 0 10 * * *
    Command:      curl https://rjgrooming.up.railway.app/cron/reminders
    Requires GAS action=bookings (see gas_cancel_reschedule.gs).
    """
    import datetime as _dt

    secret = os.environ.get("CRON_SECRET", "")
    if secret and request.args.get("secret") != secret:
        return "Unauthorized", 401

    if not GOOGLE_SCRIPT:
        return "GOOGLE_SCRIPT not configured", 500

    twilio_sid   = os.environ.get("TWILIO_ACCOUNT_SID")
    twilio_token = os.environ.get("TWILIO_AUTH_TOKEN")
    twilio_from  = os.environ.get("TWILIO_PHONE", "+37266922128")

    tomorrow  = _dt.date.today() + _dt.timedelta(days=1)
    date_gs   = tomorrow.strftime("%d.%m.%Y")   # DD.MM.YYYY for GAS

    # ── Fetch tomorrow's bookings from Google Script ──────────────────────
    try:
        r = requests.get(GOOGLE_SCRIPT,
                         params={"action": "bookings", "date": date_gs},
                         timeout=30)
        data = r.json()
    except Exception as e:
        print(f"[cron/reminders] GAS error: {e}", flush=True)
        return f"GAS error: {e}", 500

    bookings = data.get("bookings", [])
    print(f"[cron/reminders] {date_gs}: {len(bookings)} bookings", flush=True)

    sent, failed, skipped = [], [], []

    for b in bookings:
        phone  = re.sub(r'[\s\-()]', '', str(b.get("phone", "")).strip())
        time_  = b.get("time", "")
        master = b.get("master", "")
        lang   = b.get("lang", "ru")

        if not phone:
            skipped.append(f"no phone — {b.get('title', '?')}")
            continue
        if not phone.startswith("+"):
            phone = "+" + phone

        master_part = f"Мастер: {master}. " if master else ""

        if lang == "en":
            body = (f"R&J Grooming reminder: your appointment is tomorrow "
                    f"{date_gs} at {time_}. Groomer: {master}. "
                    "Address: Allveelaeva 4, Noblessner. See you! 🐾")
        elif lang == "et":
            body = (f"R&J Grooming meeldetuletus: teie broneering on homme "
                    f"{date_gs} kell {time_}. Meister: {master}. "
                    "Aadress: Allveelaeva 4, Noblessner. Ootame teid! 🐾")
        else:
            body = (f"Напоминаем о вашей записи в R&J Grooming завтра "
                    f"{date_gs} в {time_}. {master_part}"
                    "Адрес: Allveelaeva 4, Noblessner. Ждём вас! 🐾")

        if twilio_sid and twilio_token:
            try:
                resp = requests.post(
                    f"https://api.twilio.com/2010-04-01/Accounts/{twilio_sid}/Messages.json",
                    auth=(twilio_sid, twilio_token),
                    data={"From": twilio_from, "To": phone, "Body": body},
                    timeout=10,
                )
                if resp.status_code == 201:
                    sent.append(phone)
                    print(f"[cron/reminders] ✓ {phone}", flush=True)
                else:
                    failed.append(f"{phone}: {resp.status_code} {resp.text[:80]}")
                    print(f"[cron/reminders] ✗ {phone}: {resp.status_code}", flush=True)
            except Exception as e:
                failed.append(f"{phone}: {e}")
                print(f"[cron/reminders] ✗ {phone}: {e}", flush=True)
        else:
            skipped.append(f"Twilio not configured — would send to {phone}")
            print(f"[cron/reminders] Twilio not set — skipping {phone}", flush=True)

    summary = (f"Reminders {date_gs}: {len(bookings)} bookings | "
               f"sent={len(sent)} failed={len(failed)} skipped={len(skipped)}")
    print(f"[cron/reminders] {summary}", flush=True)
    lines = ([summary]
             + [f"✓ {p}" for p in sent]
             + [f"✗ {e}" for e in failed]
             + [f"- {s}" for s in skipped])
    return "\n".join(lines), 200

@app.route("/admin/whatsapp")
def admin_whatsapp():
    global jarvis_enabled, instagram_enabled
    html = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>R&J Admin</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{background:#1c1c18;color:#c8c2b8;font-family:'Montserrat',sans-serif;min-height:100vh;padding:20px 14px;font-size:16px;line-height:1.5}
h1{color:#c9a84c;font-size:1.3rem;font-weight:700;margin-bottom:20px;letter-spacing:.04em}
h2{color:#c9a84c;font-size:.95rem;font-weight:700;margin-bottom:14px;letter-spacing:.03em;text-transform:uppercase}
.card{background:#26261f;border-radius:14px;padding:18px 16px;margin-bottom:16px}
.bot-row{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.bot-row:last-of-type{margin-bottom:0}
.bot-label{font-size:.9rem;min-width:80px}
.badge{display:inline-block;padding:5px 12px;border-radius:20px;font-size:.75rem;font-weight:700;min-width:52px;text-align:center}
.on{background:#2a4a2a;color:#6fcf6f}
.off{background:#4a2a2a;color:#cf6f6f}
.btn{display:block;width:100%;min-height:48px;border:none;border-radius:10px;font-family:inherit;font-size:1rem;font-weight:700;cursor:pointer;transition:.15s;letter-spacing:.02em;padding:0 16px}
.btn-gold{background:#c9a84c;color:#1c1c18}
.btn-gold:active{background:#b89640}
.btn-row{margin-top:14px}
#msg{font-size:.85rem;color:#c9a84c;margin-top:10px;min-height:1.2em;text-align:center}
.client-item{padding:12px 0;border-bottom:1px solid #2e2e26;display:flex;flex-direction:column;gap:3px}
.client-item:last-child{border-bottom:none}
.c-phone{font-size:1rem;font-weight:600;color:#e8e0d0}
.c-text{font-size:.85rem;color:#a09880;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.c-meta{display:flex;gap:8px;align-items:center}
.c-time{font-size:.75rem;color:#666}
.c-ch{font-size:.7rem;color:#888;text-transform:uppercase;background:#2e2e26;padding:2px 7px;border-radius:10px}
#clients-empty{font-size:.85rem;color:#666;text-align:center;padding:12px 0}
select{width:100%;background:#1c1c18;border:1px solid #3a3a30;border-radius:10px;color:#c8c2b8;font-family:inherit;font-size:1rem;padding:14px 12px;margin-bottom:12px;appearance:none;-webkit-appearance:none}
select:focus{outline:none;border-color:#c9a84c}
textarea{width:100%;background:#1c1c18;border:1px solid #3a3a30;border-radius:10px;color:#c8c2b8;font-family:inherit;font-size:1rem;padding:14px 12px;margin-bottom:12px;resize:vertical;min-height:100px;line-height:1.5}
textarea:focus{outline:none;border-color:#c9a84c}
#sendMsg{font-size:.85rem;color:#c9a84c;margin-top:10px;min-height:1.2em;text-align:center}
.counter-row{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:4px}
.counter-item{background:#1c1c18;border-radius:10px;padding:10px 12px;flex:1;min-width:72px;text-align:center}
.counter-num{font-size:1.5rem;font-weight:700;color:#c9a84c;line-height:1.1}
.counter-lbl{font-size:.65rem;color:#888;text-transform:uppercase;margin-top:3px}
.msg-item{padding:8px 0;border-bottom:1px solid #2e2e26}
.msg-item:last-child{border-bottom:none}
.msg-head{display:flex;gap:6px;align-items:center;margin-bottom:2px}
.msg-dir{font-size:.7rem;font-weight:700}
.msg-in .msg-dir{color:#6fcf6f}
.msg-out .msg-dir{color:#c9a84c}
.msg-ph{font-size:.73rem;color:#a09880;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.msg-t{font-size:.68rem;color:#555;white-space:nowrap}
.msg-body{font-size:.8rem;color:#c8c2b8;line-height:1.35;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
</style>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600;700&display=swap" rel="stylesheet">
</head>
<body>
<h1>R&J Grooming — Admin</h1>

<div class="card">
  <h2>Боты</h2>
  <div class="bot-row">
    <span class="bot-label">Jarvis</span>
    <span class="badge" id="jarvisBadge"></span>
  </div>
  <div class="btn-row"><button class="btn btn-gold" id="jarvisBtn" onclick="toggleJarvis()"></button></div>
  <div class="bot-row" style="margin-top:14px">
    <span class="bot-label">Instagram</span>
    <span class="badge" id="igBadge"></span>
  </div>
  <div class="btn-row"><button class="btn btn-gold" id="igBtn" onclick="toggleIg()"></button></div>
  <div class="bot-row" style="margin-top:14px">
    <span class="bot-label">Jarvis WA</span>
    <span class="badge" id="waJarvisBadge"></span>
  </div>
  <div class="btn-row"><button class="btn btn-gold" id="waJarvisBtn" onclick="toggleWaJarvis()"></button></div>
  <div id="msg"></div>
</div>

<div class="card">
  <h2>Клиенты</h2>
  <div id="clientsList"><div id="clients-empty">Нет данных</div></div>
</div>

<div class="card">
  <h2>Статистика сообщений</h2>
  <div class="counter-row">
    <div class="counter-item"><div class="counter-num" id="cntWa">0</div><div class="counter-lbl">WhatsApp</div></div>
    <div class="counter-item"><div class="counter-num" id="cntIg">0</div><div class="counter-lbl">Instagram</div></div>
    <div class="counter-item"><div class="counter-num" id="cntFb">0</div><div class="counter-lbl">Facebook</div></div>
  </div>
</div>

<div class="card">
  <h2>Последние сообщения</h2>
  <div id="msgLog"><div style="font-size:.85rem;color:#666;text-align:center;padding:12px 0">Нет данных</div></div>
</div>

<div class="card">
  <h2>Отправить сообщение</h2>
  <select id="selPhone"><option value="">— выбрать клиента —</option></select>
  <textarea id="txtMsg" placeholder="Текст сообщения..."></textarea>
  <button class="btn btn-gold" onclick="sendManual()">Отправить в WhatsApp</button>
  <div id="sendMsg"></div>
</div>

<script>
var jarvisOn = """ + ("true" if jarvis_enabled else "false") + """;
var igOn = """ + ("true" if instagram_enabled else "false") + """;
var waJarvisOn = true;

function setBadge(id, on){
  var el=document.getElementById(id);
  el.className='badge '+(on?'on':'off');
  el.textContent=on?'ВКЛ':'ВЫКЛ';
}
function updateBadges(){
  setBadge('jarvisBadge', jarvisOn);
  document.getElementById('jarvisBtn').textContent=jarvisOn?'Выключить Jarvis':'Включить Jarvis';
  setBadge('igBadge', igOn);
  document.getElementById('igBtn').textContent=igOn?'Выключить Instagram':'Включить Instagram';
  setBadge('waJarvisBadge', waJarvisOn);
  document.getElementById('waJarvisBtn').textContent=waJarvisOn?'Выключить Jarvis WA':'Включить Jarvis WA';
}
updateBadges();

function toggleJarvis(){
  fetch('/admin/api/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!jarvisOn})})
    .then(r=>r.json()).then(d=>{jarvisOn=!jarvisOn;updateBadges();document.getElementById('msg').textContent=d.message||'';});
}
function toggleIg(){
  fetch('/admin/api/toggle-instagram',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!igOn})})
    .then(r=>r.json()).then(d=>{igOn=!igOn;updateBadges();document.getElementById('msg').textContent=d.message||'';});
}
function toggleWaJarvis(){
  var msgEl=document.getElementById('msg');
  msgEl.textContent='...';
  fetch('http://mydeal.railway.internal/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!waJarvisOn})})
    .then(r=>r.json()).then(d=>{
      waJarvisOn=!waJarvisOn;updateBadges();
      msgEl.textContent='Jarvis WA '+(waJarvisOn?'включён ✅':'выключен ❌');
    }).catch(function(){msgEl.textContent='Ошибка: недоступен Jarvis WA сервис';});
}

function loadClients(){
  fetch('/admin/api/messages').then(r=>r.json()).then(data=>{
    var list=document.getElementById('clientsList');
    var sel=document.getElementById('selPhone');
    var cur=sel.value;
    sel.innerHTML='<option value="">— выбрать клиента —</option>';
    data.sort(function(a,b){return b.ts.localeCompare(a.ts);});
    if(!data.length){list.innerHTML='<div id="clients-empty">Нет клиентов</div>';return;}
    list.innerHTML='';
    data.forEach(function(c){
      var ts=c.ts?new Date(c.ts).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'';
      var div=document.createElement('div');
      div.className='client-item';
      div.innerHTML='<div class="c-phone">'+esc(c.phone)+'</div>'
        +'<div class="c-text">'+esc(c.last_text||'—')+'</div>'
        +'<div class="c-meta"><span class="c-ch">'+esc(c.channel||'wa')+'</span><span class="c-time">'+ts+'</span></div>';
      list.appendChild(div);
      var opt=document.createElement('option');
      opt.value=c.phone; opt.textContent=c.phone+(c.last_text?' — '+c.last_text.substring(0,28):'');
      sel.appendChild(opt);
    });
    if(cur) sel.value=cur;
  });
}

function esc(s){var d=document.createElement('div');d.appendChild(document.createTextNode(String(s)));return d.innerHTML;}

function sendManual(){
  var phone=document.getElementById('selPhone').value;
  var text=document.getElementById('txtMsg').value.trim();
  var msgEl=document.getElementById('sendMsg');
  if(!phone){msgEl.textContent='Выберите клиента';return;}
  if(!text){msgEl.textContent='Введите текст';return;}
  msgEl.textContent='Отправка...';
  fetch('/admin/api/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({phone:phone,text:text})})
    .then(r=>r.json()).then(d=>{
      if(d.ok){msgEl.textContent='✓ Отправлено';document.getElementById('txtMsg').value='';}
      else{msgEl.textContent='Ошибка: '+(d.error||'неизвестно');}
    });
}

loadClients();
setInterval(loadClients, 15000);

function loadMsgLog(){
  fetch('/admin/api/message-log').then(r=>r.json()).then(data=>{
    document.getElementById('cntWa').textContent=data.counts.whatsapp||0;
    document.getElementById('cntIg').textContent=data.counts.instagram||0;
    document.getElementById('cntFb').textContent=data.counts.facebook||0;
    var log=document.getElementById('msgLog');
    if(!data.log||!data.log.length){log.innerHTML='<div style="font-size:.85rem;color:#666;text-align:center;padding:12px 0">Нет сообщений</div>';return;}
    log.innerHTML='';
    data.log.forEach(function(m){
      var ts=m.ts?new Date(m.ts).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'';
      var div=document.createElement('div');
      div.className='msg-item msg-'+(m.direction==='in'?'in':'out');
      div.innerHTML='<div class="msg-head"><span class="msg-dir">'+(m.direction==='in'?'←':'→')+'</span>'
        +'<span class="c-ch">'+esc(m.channel||'wa')+'</span>'
        +'<span class="msg-ph">'+esc(m.phone||'')+'</span>'
        +'<span class="msg-t">'+ts+'</span></div>'
        +'<div class="msg-body">'+esc(m.text||'')+'</div>';
      log.appendChild(div);
    });
  }).catch(function(){});
}
loadMsgLog();
setInterval(loadMsgLog, 15000);
</script>
</body>
</html>"""
    return html

# ── PUBLIC ─────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return "MyDeal Jarvis rabotaet!"

@app.route("/privacy")
def privacy():
    return "<h1>Privacy Policy</h1><p>R&J Grooming, Tallinn, Estonia</p>"

@app.route("/terms")
def terms():
    return "<h1>Terms of Service</h1><p>R&J Grooming, Tallinn, Estonia</p>"


# ── STATS PAGE ─────────────────────────────────────────────────────────────
STATS_HTML_B64 = "PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9InJ1Ij4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCxpbml0aWFsLXNjYWxlPTEiPgo8bWV0YSBuYW1lPSJ0aGVtZS1jb2xvciIgY29udGVudD0iIzFjMWMxOCI+Cjx0aXRsZT5SJkogU3RhdHM8L3RpdGxlPgo8bGluayBocmVmPSJodHRwczovL2ZvbnRzLmdvb2dsZWFwaXMuY29tL2NzczI/ZmFtaWx5PUNvcm1vcmFudCtHYXJhbW9uZDppdGFsLHdnaHRAMCw2MDA7MSw0MDAmZmFtaWx5PU1vbnRzZXJyYXQ6d2dodEAzMDA7NDAwOzUwMDs2MDAmZGlzcGxheT1zd2FwIiByZWw9InN0eWxlc2hlZXQiPgo8c3R5bGU+Cip7Ym94LXNpemluZzpib3JkZXItYm94O21hcmdpbjowO3BhZGRpbmc6MH0KaHRtbCxib2R5e21pbi1oZWlnaHQ6MTAwdmg7YmFja2dyb3VuZDojMWMxYzE4O2NvbG9yOiNjOGMyYjg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC13ZWlnaHQ6MzAwfQouY29ue3dpZHRoOjEwMCU7bWF4LXdpZHRoOjUwMHB4O3BhZGRpbmc6MCAyMnB4O21hcmdpbjowIGF1dG99Ci5wYWdle3BhZGRpbmc6MzJweCAwIDYwcHh9Ci5sb2dvLXJqe2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmO2ZvbnQtc2l6ZToxLjhyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNlOGUwZDB9Ci5iYWNrLWxpbmt7ZGlzcGxheTppbmxpbmUtYmxvY2s7Zm9udC1zaXplOjAuNzRyZW07Y29sb3I6I2M5YTA1YTt0ZXh0LWRlY29yYXRpb246bm9uZTttYXJnaW4tYm90dG9tOjE0cHh9Ci5sb2dvLXN1Yntmb250LXNpemU6LjQ0cmVtO2xldHRlci1zcGFjaW5nOi40ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiM4YThhNTU7bWFyZ2luLXRvcDoycHg7cGFkZGluZy1ib3R0b206MTZweDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjIpO21hcmdpbi1ib3R0b206MjBweH0KLnRhYnMtbWFpbntkaXNwbGF5OmZsZXg7Z2FwOjA7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4yKTttYXJnaW4tYm90dG9tOjB9Ci50bWJ7cGFkZGluZzoxMnB4IDE4cHg7Zm9udC1zaXplOi41NnJlbTtsZXR0ZXItc3BhY2luZzouMThlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6IzU1NTU1MDtjdXJzb3I6cG9pbnRlcjtib3JkZXItYm90dG9tOjJweCBzb2xpZCB0cmFuc3BhcmVudDtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyLXRvcDpub25lO2JvcmRlci1sZWZ0Om5vbmU7Ym9yZGVyLXJpZ2h0Om5vbmU7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7dHJhbnNpdGlvbjphbGwgLjJzfQoudG1iLmFjdGl2ZXtjb2xvcjojYzlhODRjO2JvcmRlci1ib3R0b20tY29sb3I6I2M5YTg0Y30KLnRtYjpob3Zlcntjb2xvcjojYzhjMmI4fQoucGFuZWx7ZGlzcGxheTpub25lO3BhZGRpbmc6MjBweCAwfQoucGFuZWwuYWN0aXZle2Rpc3BsYXk6YmxvY2t9Ci5zbGJse2ZvbnQtc2l6ZTouNTJyZW07bGV0dGVyLXNwYWNpbmc6LjJlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2M5YTg0YzttYXJnaW4tYm90dG9tOjEwcHg7Zm9udC13ZWlnaHQ6NTAwfQoubWV0cmljc3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCgzLDFmcik7Z2FwOjZweDttYXJnaW4tYm90dG9tOjIycHh9Ci5tZXRyaWN7YmFja2dyb3VuZDojMTQxNDEwO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDcpO3BhZGRpbmc6MTJweCAxNHB4fQoubWV0cmljLWxhYmVse2ZvbnQtc2l6ZTouNTRyZW07bGV0dGVyLXNwYWNpbmc6LjFlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6IzU1NTU1MDttYXJnaW4tYm90dG9tOjVweH0KLm1ldHJpYy12YWx7Zm9udC1mYW1pbHk6J0Nvcm1vcmFudCBHYXJhbW9uZCcsc2VyaWY7Zm9udC1zaXplOjEuMzVyZW07Y29sb3I6I2M5YTg0Yztmb250LXdlaWdodDo2MDB9Ci5tZXRyaWMtc3Vie2ZvbnQtc2l6ZTouNThyZW07Y29sb3I6IzQ0NDQ0MDttYXJnaW4tdG9wOjJweH0KLmRpc2NvdW50LXJvd3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxMHB4O2JhY2tncm91bmQ6IzE0MTQxMDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjAxLDE2OCw3NiwuMik7cGFkZGluZzoxMnB4IDE0cHg7bWFyZ2luLWJvdHRvbToxOHB4fQouZGlzY291bnQtbGFiZWx7Zm9udC1zaXplOi41OHJlbTtsZXR0ZXItc3BhY2luZzouMWVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojOGE4YTU1O2ZsZXg6MX0KLmRpc2NvdW50LWlucHV0e2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOm5vbmU7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC40KTtjb2xvcjojYzlhODRjO2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmO2ZvbnQtc2l6ZToxLjJyZW07Zm9udC13ZWlnaHQ6NjAwO3dpZHRoOjgwcHg7dGV4dC1hbGlnbjpyaWdodDtvdXRsaW5lOm5vbmU7cGFkZGluZzoycHggNHB4fQouZGlzY291bnQtaW5wdXQ6Zm9jdXN7Ym9yZGVyLWJvdHRvbS1jb2xvcjojYzlhODRjfQouZGlzY291bnQtZXVye2NvbG9yOiM4YThhNTU7Zm9udC1zaXplOi43NXJlbTttYXJnaW4tbGVmdDoycHh9Ci5wZXJpb2Qtcm93e2Rpc3BsYXk6ZmxleDtnYXA6OHB4O21hcmdpbi1ib3R0b206MThweDthbGlnbi1pdGVtczpjZW50ZXJ9Ci5wZXJpb2Qtc2VsZWN0e2JhY2tncm91bmQ6IzE0MTQxMDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEpO2NvbG9yOiNjOGMyYjg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC1zaXplOi43MnJlbTtwYWRkaW5nOjhweCAxMnB4O291dGxpbmU6bm9uZTtmbGV4OjF9Ci5wZXJpb2Qtc2VsZWN0OmZvY3Vze2JvcmRlci1jb2xvcjojYzlhODRjfQoucmVmcmVzaC1idG57YmFja2dyb3VuZDpyZ2JhKDIwMSwxNjgsNzYsLjEyKTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjAxLDE2OCw3NiwuMyk7Y29sb3I6I2M5YTg0YztwYWRkaW5nOjhweCAxNHB4O2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZTouNThyZW07bGV0dGVyLXNwYWNpbmc6LjE0ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO3doaXRlLXNwYWNlOm5vd3JhcH0KLnJlZnJlc2gtYnRuOmhvdmVye2JhY2tncm91bmQ6cmdiYSgyMDEsMTY4LDc2LC4yMil9Ci5tYXN0ZXItY2FyZHtiYWNrZ3JvdW5kOiMxNDE0MTA7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNyk7Ym9yZGVyLWxlZnQ6M3B4IHNvbGlkIHJnYmEoMjAxLDE2OCw3NiwuMyk7bWFyZ2luLWJvdHRvbTo2cHg7b3ZlcmZsb3c6aGlkZGVufQoubWFzdGVyLWhlYWR7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2FsaWduLWl0ZW1zOmNlbnRlcjtwYWRkaW5nOjEzcHggMTVweDtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmJhY2tncm91bmQgLjJzfQoubWFzdGVyLWhlYWQ6aG92ZXJ7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wMil9Ci5tYXN0ZXItaGVhZC5vcGVue2JvcmRlci1sZWZ0LWNvbG9yOiNjOWE4NGN9Ci5tbmFtZXtmb250LXNpemU6Ljg4cmVtO2ZvbnQtd2VpZ2h0OjUwMDtjb2xvcjojZThlMGQwfQoubWNvdW50e2ZvbnQtc2l6ZTouNnJlbTtjb2xvcjojNTU1NTUwO21hcmdpbi10b3A6MnB4fQoubWVhcm5pbmdze2Rpc3BsYXk6ZmxleDtnYXA6MTRweDthbGlnbi1pdGVtczpjZW50ZXJ9Ci5lYXJuLWl0ZW17dGV4dC1hbGlnbjpyaWdodH0KLmVhcm4tbGFiZWx7Zm9udC1zaXplOi41cmVtO2NvbG9yOiM1NTU1NTA7bGV0dGVyLXNwYWNpbmc6LjFlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2V9Ci5lYXJuLXZhbHtmb250LXNpemU6Ljg4cmVtO2NvbG9yOiNjOWE4NGM7Zm9udC13ZWlnaHQ6NTAwfQouZWFybi12YWwuc2Fsb257Y29sb3I6IzhhOGE1NX0KLmNoZXZyb257Y29sb3I6IzhhOGE1NTtmb250LXNpemU6Ljc1cmVtO3RyYW5zaXRpb246dHJhbnNmb3JtIC4yNXM7bWFyZ2luLWxlZnQ6OHB4fQoubWFzdGVyLWhlYWQub3BlbiAuY2hldnJvbnt0cmFuc2Zvcm06cm90YXRlKDE4MGRlZyl9Ci5tYXN0ZXItYm9keXtkaXNwbGF5Om5vbmU7Ym9yZGVyLXRvcDoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDYpfQoubWFzdGVyLWJvZHkub3BlbntkaXNwbGF5OmJsb2NrfQoudmlzaXRzLWhlYWRlcntkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjcycHggMWZyIDg1cHggNjBweDtnYXA6NnB4O3BhZGRpbmc6OHB4IDE1cHg7Zm9udC1zaXplOi41cmVtO2NvbG9yOiM0NDQ0NDA7bGV0dGVyLXNwYWNpbmc6LjFlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wMil9Ci52aXNpdC1yb3d7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczo3MnB4IDFmciA4NXB4IDYwcHg7Z2FwOjZweDtwYWRkaW5nOjlweCAxNXB4O2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA0KTtmb250LXNpemU6LjcycmVtO2FsaWduLWl0ZW1zOnN0YXJ0fQoudmlzaXQtcm93Omxhc3QtY2hpbGR7Ym9yZGVyLWJvdHRvbTpub25lfQoudmlzaXQtZGF0ZXtjb2xvcjojNTU1NTUwfQoudmlzaXQtY2xpZW50e2NvbG9yOiNjOGMyYjg7Zm9udC1zaXplOi43NXJlbX0KLnZpc2l0LXBldHtjb2xvcjojNTU1NTUwO2ZvbnQtc2l6ZTouNjJyZW07bWFyZ2luLXRvcDoxcHh9Ci52aXNpdC1zdmN7Y29sb3I6IzY2NjY2MDtmb250LXNpemU6LjY1cmVtfQoudmlzaXQtcHJpY2V7Y29sb3I6I2M5YTg0Yzt0ZXh0LWFsaWduOnJpZ2h0O2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmO2ZvbnQtc2l6ZTouOTVyZW07Zm9udC13ZWlnaHQ6NjAwfQoubm8tdmlzaXRze3BhZGRpbmc6MTZweCAxNXB4O2ZvbnQtc2l6ZTouNzJyZW07Y29sb3I6IzQ0NDQ0MDtmb250LXN0eWxlOml0YWxpY30KLnN1Yi10YWJze2Rpc3BsYXk6ZmxleDtnYXA6NXB4O21hcmdpbi1ib3R0b206MThweH0KLnN0YntwYWRkaW5nOjdweCAxNHB4O2ZvbnQtc2l6ZTouNTRyZW07bGV0dGVyLXNwYWNpbmc6LjE0ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiM1NTU1NTA7Y3Vyc29yOnBvaW50ZXI7YmFja2dyb3VuZDojMTQxNDEwO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDcpO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO3RyYW5zaXRpb246YWxsIC4yc30KLnN0Yi5hY3RpdmV7Y29sb3I6I2M5YTg0Yztib3JkZXItY29sb3I6cmdiYSgyMDEsMTY4LDc2LC40KTtiYWNrZ3JvdW5kOnJnYmEoMjAxLDE2OCw3NiwuMDYpfQouc3RiOmhvdmVye2NvbG9yOiNjOGMyYjh9Ci5zdWItcGFuZWx7ZGlzcGxheTpub25lfQouc3ViLXBhbmVsLmFjdGl2ZXtkaXNwbGF5OmJsb2NrfQouZml7d2lkdGg6MTAwJTtiYWNrZ3JvdW5kOiMxNDE0MTA7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xKTtjb2xvcjojYzhjMmI4O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZTouODJyZW07cGFkZGluZzoxMXB4IDE0cHg7b3V0bGluZTpub25lO21hcmdpbi1ib3R0b206OHB4fQouZmk6Zm9jdXN7Ym9yZGVyLWNvbG9yOiNjOWE4NGN9CnNlbGVjdC5maXthcHBlYXJhbmNlOm5vbmU7LXdlYmtpdC1hcHBlYXJhbmNlOm5vbmV9Ci5maS1yb3d7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyO2dhcDo4cHh9Ci5jYnRue2Rpc3BsYXk6YmxvY2s7d2lkdGg6MTAwJTtiYWNrZ3JvdW5kOiM0YTRhMmU7Y29sb3I6I2M4YzJiODtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXNpemU6LjZyZW07Zm9udC13ZWlnaHQ6NjAwO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtwYWRkaW5nOjEzcHg7Ym9yZGVyOm5vbmU7Y3Vyc29yOnBvaW50ZXI7bWFyZ2luLXRvcDo0cHg7dHJhbnNpdGlvbjpiYWNrZ3JvdW5kIC4yc30KLmNidG46aG92ZXJ7YmFja2dyb3VuZDojNmI2YjQyfQouY2J0bi5naG9zdHtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjMpO2NvbG9yOiNjOWE4NGN9Ci5jYnRuLmdob3N0OmhvdmVye2JhY2tncm91bmQ6cmdiYSgyMDEsMTY4LDc2LC4wOCl9Ci5saXN0LWl0ZW17ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2FsaWduLWl0ZW1zOmNlbnRlcjtwYWRkaW5nOjExcHggMTRweDtiYWNrZ3JvdW5kOiMxNDE0MTA7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNyk7bWFyZ2luLWJvdHRvbTo1cHh9Ci5saS1uYW1le2ZvbnQtc2l6ZTouODJyZW07Y29sb3I6I2M4YzJiOH0KLmxpLXN1Yntmb250LXNpemU6LjZyZW07Y29sb3I6IzU1NTU1MDttYXJnaW4tdG9wOjJweH0KLmRlbC1idG57YmFja2dyb3VuZDpub25lO2JvcmRlcjpub25lO2NvbG9yOiM0NDQ0NDA7Y3Vyc29yOnBvaW50ZXI7Zm9udC1zaXplOi44cmVtO3BhZGRpbmc6NHB4IDhweDt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLmRlbC1idG46aG92ZXJ7Y29sb3I6I2MwNTA1MH0KLmJyZWVkLWNhcmR7YmFja2dyb3VuZDojMTQxNDEwO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDcpO2JvcmRlci1sZWZ0OjNweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjIpO21hcmdpbi1ib3R0b206NnB4fQouYnJlZWQtaGVhZHtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6Y2VudGVyO3BhZGRpbmc6MTFweCAxNHB4O2N1cnNvcjpwb2ludGVyfQouYnJlZWQtaGVhZDpob3ZlcntiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjAyKX0KLmJyZWVkLW5hbWV7Zm9udC1zaXplOi44MnJlbTtjb2xvcjojZThlMGQwfQouYnJlZWQtY291bnR7Zm9udC1zaXplOi42cmVtO2NvbG9yOiM1NTU1NTB9Ci5icmVlZC1ib2R5e2Rpc3BsYXk6bm9uZTtib3JkZXItdG9wOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNik7cGFkZGluZzoxMHB4IDE0cHh9Ci5icmVlZC1ib2R5Lm9wZW57ZGlzcGxheTpibG9ja30KLnN2Yy1yb3d7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2FsaWduLWl0ZW1zOmNlbnRlcjtwYWRkaW5nOjZweCAwO2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA0KTtmb250LXNpemU6Ljc1cmVtfQouc3ZjLXJvdzpsYXN0LWNoaWxke2JvcmRlci1ib3R0b206bm9uZX0KLnN2Yy1uYW1le2NvbG9yOiNjOGMyYjh9Ci5zdmMtcHJpY2V7Y29sb3I6I2M5YTg0Yztmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZjtmb250LXNpemU6Ljk1cmVtO2ZvbnQtd2VpZ2h0OjYwMH0KLmFkZC1zdmMtcm93e2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDcwcHggMzRweDtnYXA6NnB4O21hcmdpbi10b3A6MTBweH0KLmFkZC1zdmMtcm93IC5maXttYXJnaW4tYm90dG9tOjA7Zm9udC1zaXplOi43NXJlbTtwYWRkaW5nOjhweCAxMHB4fQouaWNvbi1idG57d2lkdGg6MzRweDtoZWlnaHQ6MzRweDtiYWNrZ3JvdW5kOnJnYmEoMjAxLDE2OCw3NiwuMTIpO2JvcmRlcjoxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4zKTtjb2xvcjojYzlhODRjO2N1cnNvcjpwb2ludGVyO2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OmNlbnRlcjtmb250LXNpemU6MS4xcmVtO2ZvbnQtd2VpZ2h0OjMwMH0KLmljb24tYnRuOmhvdmVye2JhY2tncm91bmQ6cmdiYSgyMDEsMTY4LDc2LC4yNCl9Ci5jbGllbnQtY2FyZHtiYWNrZ3JvdW5kOiMxNDE0MTA7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNyk7cGFkZGluZzoxNHB4O21hcmdpbi1ib3R0b206NnB4fQouY2wtcm93e2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpmbGV4LXN0YXJ0fQouY2wtYXZhdGFye3dpZHRoOjM2cHg7aGVpZ2h0OjM2cHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDpyZ2JhKDIwMSwxNjgsNzYsLjEyKTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjAxLDE2OCw3NiwuMjUpO2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OmNlbnRlcjtmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZjtmb250LXNpemU6Ljg4cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojYzlhODRjO2ZsZXgtc2hyaW5rOjA7bWFyZ2luLXJpZ2h0OjEwcHh9Ci5jbC1uYW1le2ZvbnQtc2l6ZTouODVyZW07Zm9udC13ZWlnaHQ6NTAwO2NvbG9yOiNlOGUwZDB9Ci5jbC1kZXRhaWx7Zm9udC1zaXplOi42MnJlbTtjb2xvcjojNTU1NTUwO21hcmdpbi10b3A6MnB4fQouY2wtc3RhdHN7ZGlzcGxheTpmbGV4O2dhcDoxNHB4O21hcmdpbi10b3A6MTBweDtwYWRkaW5nLXRvcDoxMHB4O2JvcmRlci10b3A6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA1KX0KLmNzdC12YWx7Zm9udC1mYW1pbHk6J0Nvcm1vcmFudCBHYXJhbW9uZCcsc2VyaWY7Zm9udC1zaXplOjEuMXJlbTtjb2xvcjojYzlhODRjO2ZvbnQtd2VpZ2h0OjYwMH0KLmNzdC1sYWJlbHtmb250LXNpemU6LjUycmVtO2NvbG9yOiM1NTU1NTA7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi4xZW19Ci5jbC1sYXN0e2ZvbnQtc2l6ZTouNnJlbTtjb2xvcjojNTU1NTUwO21hcmdpbi10b3A6OHB4fQouY2wtbGFzdCBzcGFue2NvbG9yOiM4YThhNTV9Ci5jbC1iYWRnZXtmb250LXNpemU6LjU4cmVtO2NvbG9yOiNjOWE4NGM7YmFja2dyb3VuZDpyZ2JhKDIwMSwxNjgsNzYsLjEpO2JvcmRlcjoxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4yKTtwYWRkaW5nOjNweCA4cHg7d2hpdGUtc3BhY2U6bm93cmFwfQoubG9hZGluZ3t0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjQwcHggMDtjb2xvcjojNDQ0NDQwO2ZvbnQtc2l6ZTouNzVyZW07bGV0dGVyLXNwYWNpbmc6LjEyZW19Ci5ncm93dGgtdGFic3tkaXNwbGF5OmZsZXg7Z2FwOjhweDttYXJnaW4tYm90dG9tOjE0cHh9Ci5ndGFie2JhY2tncm91bmQ6bm9uZTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjAxLDE2OCw3NiwuMjUpO2NvbG9yOiM4YTg1Nzg7cGFkZGluZzo3cHggMTZweDtmb250LXNpemU6LjY1cmVtO2xldHRlci1zcGFjaW5nOi4xZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO3RyYW5zaXRpb246YWxsIC4yc30KLmd0YWIuYWN0aXZle2NvbG9yOiNjOWE4NGM7Ym9yZGVyLWNvbG9yOiNjOWE4NGM7YmFja2dyb3VuZDpyZ2JhKDIwMSwxNjgsNzYsLjA4KX0KLmdyb3d0aC1zdmd7d2lkdGg6MTAwJTtoZWlnaHQ6YXV0bztkaXNwbGF5OmJsb2NrfQouZ3Jvd3RoLWJhci1sYWJlbHtmb250LXNpemU6OHB4O2ZpbGw6IzhhODU3ODtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZn0KLmdyb3d0aC1iYXItdmFse2ZvbnQtc2l6ZTo5cHg7ZmlsbDojYzlhODRjO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2ZvbnQtd2VpZ2h0OjYwMH0KLnNlY3R7bWFyZ2luLWJvdHRvbToyMnB4fQouZm9ybS1ibG9ja3tiYWNrZ3JvdW5kOiMxNDE0MTA7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNyk7cGFkZGluZzoxNnB4O21hcmdpbi1ib3R0b206MTZweH0KLnRhZ3tmb250LXNpemU6LjU4cmVtO2NvbG9yOiM4YThhNTU7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNSk7cGFkZGluZzoycHggOHB4O21hcmdpbi1yaWdodDo0cHg7bWFyZ2luLWJvdHRvbTo0cHg7ZGlzcGxheTppbmxpbmUtYmxvY2t9CkBrZXlmcmFtZXMgZnV7ZnJvbXtvcGFjaXR5OjA7dHJhbnNmb3JtOnRyYW5zbGF0ZVkoOHB4KX10b3tvcGFjaXR5OjE7dHJhbnNmb3JtOnRyYW5zbGF0ZVkoMCl9fQouYW5pbXthbmltYXRpb246ZnUgLjNzIGVhc2UgYm90aH0KPC9zdHlsZT4KPC9oZWFkPgo8Ym9keT4KPGRpdiBjbGFzcz0iY29uIj4KPGRpdiBjbGFzcz0icGFnZSI+Cgo8YSBocmVmPSIvYWRtaW4/cGFzcz1hbnphMTk4NSIgY2xhc3M9ImJhY2stbGluayI+4oaQINCQ0LTQvNC40L0t0L/QsNC90LXQu9GMPC9hPgo8ZGl2IGNsYXNzPSJsb2dvLXJqIj5SJmFtcDtKPC9kaXY+CjxkaXYgY2xhc3M9ImxvZ28tc3ViIj5Hcm9vbWluZyAmbWlkZG90OyDQodGC0LDRgtC40YHRgtC40LrQsCAmbWlkZG90OyDQotCw0LvQu9C40L08L2Rpdj4KCjxkaXYgY2xhc3M9InRhYnMtbWFpbiI+CiAgPGJ1dHRvbiBjbGFzcz0idG1iIGFjdGl2ZSIgb25jbGljaz0ic3dpdGNoTWFpbignc3RhdHMnLHRoaXMpIj7QodGC0LDRgtC40YHRgtC40LrQsDwvYnV0dG9uPgogIDxidXR0b24gY2xhc3M9InRtYiIgb25jbGljaz0ic3dpdGNoTWFpbignbWdtdCcsdGhpcykiPtCj0L/RgNCw0LLQu9C10L3QuNC1PC9idXR0b24+CjwvZGl2PgoKPCEtLSDilZDilZDilZAg0KHQotCQ0KLQmNCh0KLQmNCa0JAg4pWQ4pWQ4pWQIC0tPgo8ZGl2IGNsYXNzPSJwYW5lbCBhY3RpdmUiIGlkPSJwYW5lbC1zdGF0cyI+CgogIDxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDtnYXA6OHB4O21hcmdpbi1ib3R0b206MTJweDttYXJnaW4tdG9wOjIwcHg7YWxpZ24taXRlbXM6Y2VudGVyIj4KICAgIDxzZWxlY3QgY2xhc3M9InBlcmlvZC1zZWxlY3QiIGlkPSJwZXJpb2RTZWxlY3QiIG9uY2hhbmdlPSJsb2FkU3RhdHMoKSI+CiAgICAgIDxvcHRpb24gdmFsdWU9Im1vbnRoIj7QrdGC0L7RgiDQvNC10YHRj9GGPC9vcHRpb24+CiAgICAgIDxvcHRpb24gdmFsdWU9Imxhc3RfbW9udGgiPtCf0YDQvtGI0LvRi9C5INC80LXRgdGP0YY8L29wdGlvbj4KICAgICAgPG9wdGlvbiB2YWx1ZT0iM21vbnRocyI+MyDQvNC10YHRj9GG0LA8L29wdGlvbj4KICAgICAgPG9wdGlvbiB2YWx1ZT0iYWxsIj7QktGB0ZEg0LLRgNC10LzRjzwvb3B0aW9uPgogICAgPC9zZWxlY3Q+CiAgICA8YnV0dG9uIGNsYXNzPSJyZWZyZXNoLWJ0biIgb25jbGljaz0ibG9hZFN0YXRzKCkiPiYjODYzNTsg0J7QsdC90L7QstC40YLRjDwvYnV0dG9uPgogIDwvZGl2PgoKICA8ZGl2IGNsYXNzPSJkaXNjb3VudC1yb3ciPgogICAgPGRpdiBjbGFzcz0iZGlzY291bnQtbGFiZWwiPtCh0LrQuNC00LrQsCDRgdCw0LvQvtC90LAgKNCy0YvRh9C40YLQsNC10YLRgdGPINGC0L7Qu9GM0LrQviDQuNC3INC00L7Qu9C4INGB0LDQu9C+0L3QsCk8L2Rpdj4KICAgIDxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjRweCI+CiAgICAgIDxpbnB1dCBjbGFzcz0iZGlzY291bnQtaW5wdXQiIGlkPSJkaXNjb3VudElucHV0IiB0eXBlPSJudW1iZXIiIG1pbj0iMCIgdmFsdWU9IjAiIG9uaW5wdXQ9InJlY2FsYygpIj4KICAgICAgPHNwYW4gY2xhc3M9ImRpc2NvdW50LWV1ciI+4oKsPC9zcGFuPgogICAgPC9kaXY+CiAgPC9kaXY+CgogIDxkaXYgY2xhc3M9Im1ldHJpY3MiIGlkPSJtZXRyaWNzQmxvY2siPgogICAgPGRpdiBjbGFzcz0ibWV0cmljIj48ZGl2IGNsYXNzPSJtZXRyaWMtbGFiZWwiPtCS0YvRgNGD0YfQutCwPC9kaXY+PGRpdiBjbGFzcz0ibWV0cmljLXZhbCIgaWQ9Im1Ub3RhbCI+4oCUPC9kaXY+PGRpdiBjbGFzcz0ibWV0cmljLXN1YiI+0YHRg9C80LzQsCDRg9GB0LvRg9CzPC9kaXY+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJtZXRyaWMiPjxkaXYgY2xhc3M9Im1ldHJpYy1sYWJlbCI+0JzQsNGB0YLQtdGA0LDQvDwvZGl2PjxkaXYgY2xhc3M9Im1ldHJpYy12YWwiIGlkPSJtTWFzdGVycyI+4oCUPC9kaXY+PGRpdiBjbGFzcz0ibWV0cmljLXN1YiI+0L/QviDRgdGC0LDQstC60LUg0LzQsNGB0YLQtdGA0LA8L2Rpdj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im1ldHJpYyI+PGRpdiBjbGFzcz0ibWV0cmljLWxhYmVsIj7QodCw0LvQvtC90YM8L2Rpdj48ZGl2IGNsYXNzPSJtZXRyaWMtdmFsIiBpZD0ibVNhbG9uIj7igJQ8L2Rpdj48ZGl2IGNsYXNzPSJtZXRyaWMtc3ViIj7QvtGB0YLQsNGC0L7QuiDiiJIg0YHQutC40LTQutCwPC9kaXY+PC9kaXY+CiAgPC9kaXY+CiAgPGRpdiBjbGFzcz0ibWV0cmljcyIgc3R5bGU9Im1hcmdpbi1ib3R0b206MCI+CiAgICA8ZGl2IGNsYXNzPSJtZXRyaWMiPjxkaXYgY2xhc3M9Im1ldHJpYy1sYWJlbCI+0JfQsNC/0LjRgdC10Lk8L2Rpdj48ZGl2IGNsYXNzPSJtZXRyaWMtdmFsIiBpZD0ibUNvdW50Ij7igJQ8L2Rpdj48ZGl2IGNsYXNzPSJtZXRyaWMtc3ViIj7Qt9CwINC/0LXRgNC40L7QtDwvZGl2PjwvZGl2PgogICAgPGRpdiBjbGFzcz0ibWV0cmljIj48ZGl2IGNsYXNzPSJtZXRyaWMtbGFiZWwiPtCa0LvQuNC10L3RgtC+0LI8L2Rpdj48ZGl2IGNsYXNzPSJtZXRyaWMtdmFsIiBpZD0ibUNsaWVudHMiPuKAlDwvZGl2PjxkaXYgY2xhc3M9Im1ldHJpYy1zdWIiPtGD0L3QuNC60LDQu9GM0L3Ri9GFPC9kaXY+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJtZXRyaWMiPjxkaXYgY2xhc3M9Im1ldHJpYy1sYWJlbCI+0KHRgC4g0YfQtdC6PC9kaXY+PGRpdiBjbGFzcz0ibWV0cmljLXZhbCIgaWQ9Im1BdmciPuKAlDwvZGl2PjxkaXYgY2xhc3M9Im1ldHJpYy1zdWIiPtC30LAg0YPRgdC70YPQs9GDPC9kaXY+PC9kaXY+CiAgPC9kaXY+CiAgPGRpdiBjbGFzcz0ibWV0cmljcyIgc3R5bGU9Im1hcmdpbi1ib3R0b206MDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIj4KICAgIDxkaXYgY2xhc3M9Im1ldHJpYyI+PGRpdiBjbGFzcz0ibWV0cmljLWxhYmVsIj7QndC+0LLRi9GFINC60LvQuNC10L3RgtC+0LI8L2Rpdj48ZGl2IGNsYXNzPSJtZXRyaWMtdmFsIiBpZD0ibU5ld0NsaWVudHMiPuKAlDwvZGl2PjxkaXYgY2xhc3M9Im1ldHJpYy1zdWIiPtC/0LXRgNCy0YvQuSDQstC40LfQuNGCINCyINGN0YLQvtC8INC/0LXRgNC40L7QtNC1PC9kaXY+PC9kaXY+CiAgPC9kaXY+CgogIDxkaXYgc3R5bGU9Im1hcmdpbi10b3A6MjJweCI+CiAgICA8ZGl2IGNsYXNzPSJzbGJsIj7Qn9GA0LjRgNC+0YHRgiDQutC70LjQtdC90YLQvtCyPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJncm93dGgtdGFicyI+CiAgICAgIDxidXR0b24gY2xhc3M9Imd0YWIgYWN0aXZlIiBpZD0iZ3RhYkRheSIgb25jbGljaz0ic3dpdGNoR3Jvd3RoVmlldygnZGF5JykiPtCf0L4g0LTQvdGP0Lw8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iZ3RhYiIgaWQ9Imd0YWJNb250aCIgb25jbGljaz0ic3dpdGNoR3Jvd3RoVmlldygnbW9udGgnKSI+0J/QviDQvNC10YHRj9GG0LDQvDwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8ZGl2IGlkPSJncm93dGhDaGFydCI+PGRpdiBjbGFzcz0ibG9hZGluZyI+0JfQsNCz0YDRg9C30LrQsCDQtNCw0L3QvdGL0YUuLi48L2Rpdj48L2Rpdj4KICA8L2Rpdj4KCiAgPGRpdiBzdHlsZT0ibWFyZ2luLXRvcDoyMnB4Ij4KICAgIDxkaXYgY2xhc3M9InNsYmwiPtCf0L4g0LzQsNGB0YLQtdGA0LDQvDwvZGl2PgogICAgPGRpdiBpZD0ibWFzdGVyc0xpc3QiPjxkaXYgY2xhc3M9ImxvYWRpbmciPtCX0LDQs9GA0YPQt9C60LAg0LTQsNC90L3Ri9GFLi4uPC9kaXY+PC9kaXY+CiAgPC9kaXY+CjwvZGl2PgoKPCEtLSDilZDilZDilZAg0KPQn9Cg0JDQktCb0JXQndCY0JUg4pWQ4pWQ4pWQIC0tPgo8ZGl2IGNsYXNzPSJwYW5lbCIgaWQ9InBhbmVsLW1nbXQiPgogIDxkaXYgc3R5bGU9Im1hcmdpbi10b3A6MjBweCIgY2xhc3M9InN1Yi10YWJzIj4KICAgIDxidXR0b24gY2xhc3M9InN0YiBhY3RpdmUiIG9uY2xpY2s9InN3aXRjaFN1YignbWFzdGVycycsdGhpcykiPtCc0LDRgdGC0LXRgNCwPC9idXR0b24+CiAgICA8YnV0dG9uIGNsYXNzPSJzdGIiIG9uY2xpY2s9InN3aXRjaFN1YignYnJlZWRzJyx0aGlzKSI+0J/QvtGA0L7QtNGLPC9idXR0b24+CiAgICA8YnV0dG9uIGNsYXNzPSJzdGIiIG9uY2xpY2s9InN3aXRjaFN1YignY2xpZW50cycsdGhpcykiPtCa0LvQuNC10L3RgtGLPC9idXR0b24+CiAgPC9kaXY+CgogIDwhLS0g0JzQsNGB0YLQtdGA0LAgLS0+CiAgPGRpdiBjbGFzcz0ic3ViLXBhbmVsIGFjdGl2ZSIgaWQ9InN1Yi1tYXN0ZXJzIj4KICAgIDxkaXYgY2xhc3M9InNlY3QiPgogICAgICA8ZGl2IGNsYXNzPSJzbGJsIj7QlNC+0LHQsNCy0LjRgtGMINC80LDRgdGC0LXRgNCwPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImZvcm0tYmxvY2siPgogICAgICAgIDxkaXYgY2xhc3M9ImZpLXJvdyI+CiAgICAgICAgICA8aW5wdXQgY2xhc3M9ImZpIiBpZD0ibmV3TWFzdGVyTmFtZSIgcGxhY2Vob2xkZXI9ItCY0LzRjyDQvNCw0YHRgtC10YDQsCI+CiAgICAgICAgICA8aW5wdXQgY2xhc3M9ImZpIiBpZD0ibmV3TWFzdGVyUGhvbmUiIHBsYWNlaG9sZGVyPSLQotC10LvQtdGE0L7QvSAo0L3QtdC+0LHRj9C3LikiPgogICAgICAgIDwvZGl2PgogICAgICAgIDxidXR0b24gY2xhc3M9ImNidG4iIG9uY2xpY2s9ImFkZE1hc3RlcigpIj4rINCU0L7QsdCw0LLQuNGC0Yw8L2J1dHRvbj4KICAgICAgPC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InNlY3QiPgogICAgICA8ZGl2IGNsYXNzPSJzbGJsIj7QnNCw0YHRgtC10YDQsCDRgdCw0LvQvtC90LA8L2Rpdj4KICAgICAgPGRpdiBpZD0ibWFzdGVyTGlzdFVJIj48L2Rpdj4KICAgIDwvZGl2PgogIDwvZGl2PgoKICA8IS0tINCf0L7RgNC+0LTRiyDQuCDRg9GB0LvRg9Cz0LggLS0+CiAgPGRpdiBjbGFzcz0ic3ViLXBhbmVsIiBpZD0ic3ViLWJyZWVkcyI+CiAgICA8ZGl2IGNsYXNzPSJzZWN0Ij4KICAgICAgPGRpdiBjbGFzcz0ic2xibCI+0JTQvtCx0LDQstC40YLRjCDQv9C+0YDQvtC00YM8L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0iZm9ybS1ibG9jayI+CiAgICAgICAgPGlucHV0IGNsYXNzPSJmaSIgaWQ9Im5ld0JyZWVkTmFtZSIgcGxhY2Vob2xkZXI9ItCd0LDQt9Cy0LDQvdC40LUg0L/QvtGA0L7QtNGLICjQvdCw0L/RgC4g0KXQsNGB0LrQuCAyMOKAkzMwINC60LMpIj4KICAgICAgICA8YnV0dG9uIGNsYXNzPSJjYnRuIiBvbmNsaWNrPSJhZGRCcmVlZCgpIj4rINCU0L7QsdCw0LLQuNGC0Ywg0L/QvtGA0L7QtNGDPC9idXR0b24+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzZWN0Ij4KICAgICAgPGRpdiBjbGFzcz0ic2xibCI+0J/QvtGA0L7QtNGLINC4INGD0YHQu9GD0LPQuDwvZGl2PgogICAgICA8ZGl2IGlkPSJicmVlZExpc3RVSSI+PC9kaXY+CiAgICA8L2Rpdj4KICA8L2Rpdj4KCiAgPCEtLSDQmtC70LjQtdC90YLRiyAtLT4KICA8ZGl2IGNsYXNzPSJzdWItcGFuZWwiIGlkPSJzdWItY2xpZW50cyI+CiAgICA8ZGl2IGNsYXNzPSJzZWN0Ij4KICAgICAgPGRpdiBjbGFzcz0ic2xibCI+0J3QvtCy0LDRjyDQutCw0YDRgtC+0YfQutCwINC60LvQuNC10L3RgtCwPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImZvcm0tYmxvY2siPgogICAgICAgIDxkaXYgY2xhc3M9ImZpLXJvdyI+CiAgICAgICAgICA8aW5wdXQgY2xhc3M9ImZpIiBpZD0ibmV3Q2xpZW50TmFtZSIgcGxhY2Vob2xkZXI9ItCY0LzRjyDQutC70LjQtdC90YLQsCI+CiAgICAgICAgICA8aW5wdXQgY2xhc3M9ImZpIiBpZD0ibmV3Q2xpZW50UGhvbmUiIHBsYWNlaG9sZGVyPSIrMzcyIC4uLiI+CiAgICAgICAgPC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0iZmktcm93Ij4KICAgICAgICAgIDxpbnB1dCBjbGFzcz0iZmkiIGlkPSJuZXdDbGllbnRQZXQiIHBsYWNlaG9sZGVyPSLQmtC70LjRh9C60LAg0L/QuNGC0L7QvNGG0LAiPgogICAgICAgICAgPHNlbGVjdCBjbGFzcz0iZmkiIGlkPSJuZXdDbGllbnRCcmVlZCI+PG9wdGlvbiB2YWx1ZT0iIj7Qn9C+0YDQvtC00LAuLi48L29wdGlvbj48L3NlbGVjdD4KICAgICAgICA8L2Rpdj4KICAgICAgICA8aW5wdXQgY2xhc3M9ImZpIiBpZD0ibmV3Q2xpZW50Tm90ZSIgcGxhY2Vob2xkZXI9ItCa0L7QvNC80LXQvdGC0LDRgNC40LkgKNCw0LvQu9C10YDQs9C40LgsINC+0YHQvtCx0LXQvdC90L7RgdGC0LguLi4pIj4KICAgICAgICA8YnV0dG9uIGNsYXNzPSJjYnRuIiBvbmNsaWNrPSJhZGRDbGllbnQoKSI+KyDQodC+0LfQtNCw0YLRjCDQutCw0YDRgtC+0YfQutGDPC9idXR0b24+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzZWN0Ij4KICAgICAgPGRpdiBjbGFzcz0ic2xibCI+0JHQsNC30LAg0LrQu9C40LXQvdGC0L7QsjwvZGl2PgogICAgICA8ZGl2IGlkPSJjbGllbnRMaXN0VUkiPjwvZGl2PgogICAgPC9kaXY+CiAgPC9kaXY+CjwvZGl2PgoKPC9kaXY+CjwvZGl2PgoKPHNjcmlwdD4KdmFyIEdPT0dMRV9TQ1JJUFQgPSB3aW5kb3cubG9jYXRpb24ub3JpZ2luICsgJy9hcGkvc3RhdHMnOwp2YXIgZGIgPSB7CiAgbWFzdGVyczogSlNPTi5wYXJzZShsb2NhbFN0b3JhZ2UuZ2V0SXRlbSgncmpfbWFzdGVycycpIHx8ICdbItCi0LDRgtGM0Y/QvdCwIiwi0JDQu9C40YHQsCIsItCa0YDQuNGB0YLQuNC90LAiLCLQkNC90L3QsCJdJyksCiAgYnJlZWRzOiAgSlNPTi5wYXJzZShsb2NhbFN0b3JhZ2UuZ2V0SXRlbSgncmpfYnJlZWRzJykgIHx8ICdbXScpLAogIGNsaWVudHM6IEpTT04ucGFyc2UobG9jYWxTdG9yYWdlLmdldEl0ZW0oJ3JqX2NsaWVudHMnKSB8fCAnW10nKSwKICBkaXNjb3VudDogcGFyc2VGbG9hdChsb2NhbFN0b3JhZ2UuZ2V0SXRlbSgncmpfZGlzY291bnQnKSB8fCAnMCcpCn07CnZhciBhbGxCb29raW5ncyA9IFtdOwp2YXIgc3RhdHNMb2FkZWQgPSBmYWxzZTsKCmZ1bmN0aW9uIHNhdmUoKSB7CiAgbG9jYWxTdG9yYWdlLnNldEl0ZW0oJ3JqX21hc3RlcnMnLCAgSlNPTi5zdHJpbmdpZnkoZGIubWFzdGVycykpOwogIGxvY2FsU3RvcmFnZS5zZXRJdGVtKCdyal9icmVlZHMnLCAgIEpTT04uc3RyaW5naWZ5KGRiLmJyZWVkcykpOwogIGxvY2FsU3RvcmFnZS5zZXRJdGVtKCdyal9jbGllbnRzJywgIEpTT04uc3RyaW5naWZ5KGRiLmNsaWVudHMpKTsKICBsb2NhbFN0b3JhZ2Uuc2V0SXRlbSgncmpfZGlzY291bnQnLCBkYi5kaXNjb3VudCk7Cn0KCi8vIOKUgOKUgCDQndCQ0JLQmNCT0JDQptCY0K8g4pSA4pSACmZ1bmN0aW9uIHN3aXRjaE1haW4obmFtZSwgYnRuKSB7CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLnRtYicpLmZvckVhY2goZnVuY3Rpb24odCl7dC5jbGFzc0xpc3QucmVtb3ZlKCdhY3RpdmUnKX0pOwogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5wYW5lbCcpLmZvckVhY2goZnVuY3Rpb24ocCl7cC5jbGFzc0xpc3QucmVtb3ZlKCdhY3RpdmUnKX0pOwogIGJ0bi5jbGFzc0xpc3QuYWRkKCdhY3RpdmUnKTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgncGFuZWwtJytuYW1lKS5jbGFzc0xpc3QuYWRkKCdhY3RpdmUnKTsKICBpZiAobmFtZSA9PT0gJ3N0YXRzJyAmJiAhc3RhdHNMb2FkZWQpIGxvYWRTdGF0cygpOwp9CmZ1bmN0aW9uIHN3aXRjaFN1YihuYW1lLCBidG4pIHsKICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuc3RiJykuZm9yRWFjaChmdW5jdGlvbih0KXt0LmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpfSk7CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLnN1Yi1wYW5lbCcpLmZvckVhY2goZnVuY3Rpb24ocCl7cC5jbGFzc0xpc3QucmVtb3ZlKCdhY3RpdmUnKX0pOwogIGJ0bi5jbGFzc0xpc3QuYWRkKCdhY3RpdmUnKTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ViLScrbmFtZSkuY2xhc3NMaXN0LmFkZCgnYWN0aXZlJyk7Cn0KCi8vIOKUgOKUgCDQodCi0JDQotCY0KHQotCY0JrQkCDilIDilIAKZnVuY3Rpb24gZ2V0UGVyaW9kUGFyYW1zKCkgewogIHZhciB2YWwgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgncGVyaW9kU2VsZWN0JykudmFsdWU7CiAgdmFyIG5vdyA9IG5ldyBEYXRlKCk7CiAgdmFyIGZyb20sIHRvOwogIGlmICh2YWwgPT09ICdtb250aCcpIHsKICAgIGZyb20gPSBuZXcgRGF0ZShub3cuZ2V0RnVsbFllYXIoKSwgbm93LmdldE1vbnRoKCksIDEpOwogICAgdG8gICA9IG5ldyBEYXRlKG5vdy5nZXRGdWxsWWVhcigpLCBub3cuZ2V0TW9udGgoKSsxLCAwKTsKICB9IGVsc2UgaWYgKHZhbCA9PT0gJ2xhc3RfbW9udGgnKSB7CiAgICBmcm9tID0gbmV3IERhdGUobm93LmdldEZ1bGxZZWFyKCksIG5vdy5nZXRNb250aCgpLTEsIDEpOwogICAgdG8gICA9IG5ldyBEYXRlKG5vdy5nZXRGdWxsWWVhcigpLCBub3cuZ2V0TW9udGgoKSwgMCk7CiAgfSBlbHNlIGlmICh2YWwgPT09ICczbW9udGhzJykgewogICAgZnJvbSA9IG5ldyBEYXRlKG5vdy5nZXRGdWxsWWVhcigpLCBub3cuZ2V0TW9udGgoKS0yLCAxKTsKICAgIHRvICAgPSBuZXcgRGF0ZShub3cuZ2V0RnVsbFllYXIoKSwgbm93LmdldE1vbnRoKCkrMSwgMCk7CiAgfSBlbHNlIHsKICAgIGZyb20gPSBuZXcgRGF0ZSgyMDI0LCAwLCAxKTsKICAgIHRvICAgPSBuZXcgRGF0ZShub3cuZ2V0RnVsbFllYXIoKSwgbm93LmdldE1vbnRoKCkrMSwgMCk7CiAgfQogIHZhciBmbXQgPSBmdW5jdGlvbihkKSB7IHJldHVybiBTdHJpbmcoZC5nZXREYXRlKCkpLnBhZFN0YXJ0KDIsJzAnKSsnLicrU3RyaW5nKGQuZ2V0TW9udGgoKSsxKS5wYWRTdGFydCgyLCcwJykrJy4nK2QuZ2V0RnVsbFllYXIoKTsgfTsKICByZXR1cm4ge2Zyb206IGZtdChmcm9tKSwgdG86IGZtdCh0byl9Owp9Cgp2YXIgYWxsVGltZUJvb2tpbmdzID0gW107CnZhciBncm93dGhWaWV3ID0gJ2RheSc7CgpmdW5jdGlvbiBsb2FkU3RhdHMoKSB7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ21hc3RlcnNMaXN0JykuaW5uZXJIVE1MID0gJzxkaXYgY2xhc3M9ImxvYWRpbmciPtCX0LDQs9GA0YPQt9C60LAg0LTQsNC90L3Ri9GFINC40Lcg0LrQsNC70LXQvdC00LDRgNGPLi4uPC9kaXY+JzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnZ3Jvd3RoQ2hhcnQnKS5pbm5lckhUTUwgPSAnPGRpdiBjbGFzcz0ibG9hZGluZyI+0JfQsNCz0YDRg9C30LrQsCDQtNCw0L3QvdGL0YUuLi48L2Rpdj4nOwogIHZhciBwID0gZ2V0UGVyaW9kUGFyYW1zKCk7CiAgUHJvbWlzZS5hbGwoWwogICAgZmV0Y2god2luZG93LmxvY2F0aW9uLm9yaWdpbiArICcvYXBpL3N0YXRzP2Zyb209JyArIHAuZnJvbSArICcmdG89JyArIHAudG8pLnRoZW4oZnVuY3Rpb24ocil7cmV0dXJuIHIuanNvbigpO30pLAogICAgZmV0Y2god2luZG93LmxvY2F0aW9uLm9yaWdpbiArICcvYXBpL3N0YXRzP2Zyb209MDEuMDEuMjAyMCZ0bz0nICsgcC50bykudGhlbihmdW5jdGlvbihyKXtyZXR1cm4gci5qc29uKCk7fSkKICBdKS50aGVuKGZ1bmN0aW9uKHJlc3VsdHMpewogICAgdmFyIHBlcmlvZERhdGEgPSByZXN1bHRzWzBdLCBhbGxEYXRhID0gcmVzdWx0c1sxXTsKICAgIGlmIChwZXJpb2REYXRhLnN1Y2Nlc3MpIHsKICAgICAgYWxsQm9va2luZ3MgPSBwZXJpb2REYXRhLmJvb2tpbmdzIHx8IFtdOwogICAgICBzdGF0c0xvYWRlZCA9IHRydWU7CiAgICB9IGVsc2UgewogICAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbWFzdGVyc0xpc3QnKS5pbm5lckhUTUwgPSAnPGRpdiBjbGFzcz0ibG9hZGluZyI+0J7RiNC40LHQutCwOiAnICsgKHBlcmlvZERhdGEuZXJyb3J8fCfQvdC10YIg0LTQsNC90L3Ri9GFJykgKyAnPC9kaXY+JzsKICAgIH0KICAgIGlmIChhbGxEYXRhLnN1Y2Nlc3MpIHsKICAgICAgYWxsVGltZUJvb2tpbmdzID0gYWxsRGF0YS5ib29raW5ncyB8fCBbXTsKICAgIH0KICAgIHJlY2FsYygpOwogICAgcmVuZGVyR3Jvd3RoQ2hhcnQoKTsKICB9KS5jYXRjaChmdW5jdGlvbihlKXsKICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdtYXN0ZXJzTGlzdCcpLmlubmVySFRNTCA9ICc8ZGl2IGNsYXNzPSJsb2FkaW5nIj7QndC10YIg0YHQvtC10LTQuNC90LXQvdC40Y8g0YEg0LrQsNC70LXQvdC00LDRgNGR0Lw8L2Rpdj4nOwogICAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2dyb3d0aENoYXJ0JykuaW5uZXJIVE1MID0gJzxkaXYgY2xhc3M9ImxvYWRpbmciPtCd0LXRgiDRgdC+0LXQtNC40L3QtdC90LjRjyDRgSDQutCw0LvQtdC90LTQsNGA0ZHQvDwvZGl2Pic7CiAgfSk7Cn0KCmZ1bmN0aW9uIHBhcnNlRE1ZKHMpewogIGlmKCFzKSByZXR1cm4gbnVsbDsKICB2YXIgcGFydHMgPSBzLnNwbGl0KCcuJyk7CiAgaWYocGFydHMubGVuZ3RoIT09MykgcmV0dXJuIG51bGw7CiAgdmFyIGQgPSBuZXcgRGF0ZShwYXJzZUludChwYXJ0c1syXSwxMCksIHBhcnNlSW50KHBhcnRzWzFdLDEwKS0xLCBwYXJzZUludChwYXJ0c1swXSwxMCkpOwogIHJldHVybiBpc05hTihkLmdldFRpbWUoKSkgPyBudWxsIDogZDsKfQoKZnVuY3Rpb24gZ2V0Q2xpZW50Rmlyc3RWaXNpdE1hcCgpewogIHZhciBtYXAgPSB7fTsKICBhbGxUaW1lQm9va2luZ3MuZm9yRWFjaChmdW5jdGlvbihiKXsKICAgIHZhciBrZXkgPSBiLmNsaWVudFBob25lIHx8IGIuY2xpZW50TmFtZTsKICAgIGlmKCFrZXkpIHJldHVybjsKICAgIHZhciBkID0gcGFyc2VETVkoYi5kYXRlKTsKICAgIGlmKCFkKSByZXR1cm47CiAgICBpZighbWFwW2tleV0gfHwgZCA8IG1hcFtrZXldKSBtYXBba2V5XSA9IGQ7CiAgfSk7CiAgcmV0dXJuIG1hcDsKfQoKZnVuY3Rpb24gc3dpdGNoR3Jvd3RoVmlldyh2aWV3KXsKICBncm93dGhWaWV3ID0gdmlldzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnZ3RhYkRheScpLmNsYXNzTGlzdC50b2dnbGUoJ2FjdGl2ZScsIHZpZXc9PT0nZGF5Jyk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2d0YWJNb250aCcpLmNsYXNzTGlzdC50b2dnbGUoJ2FjdGl2ZScsIHZpZXc9PT0nbW9udGgnKTsKICByZW5kZXJHcm93dGhDaGFydCgpOwp9CgpmdW5jdGlvbiByZW5kZXJHcm93dGhDaGFydCgpewogIHZhciBjb250YWluZXIgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnZ3Jvd3RoQ2hhcnQnKTsKICBpZighYWxsVGltZUJvb2tpbmdzLmxlbmd0aCl7IGNvbnRhaW5lci5pbm5lckhUTUwgPSAnPGRpdiBjbGFzcz0ibG9hZGluZyI+0J3QtdGCINC00LDQvdC90YvRhTwvZGl2Pic7IHJldHVybjsgfQogIHZhciBmaXJzdFZpc2l0cyA9IGdldENsaWVudEZpcnN0VmlzaXRNYXAoKTsKICB2YXIgbGFiZWxzID0gW107CgogIGlmKGdyb3d0aFZpZXcgPT09ICdkYXknKXsKICAgIHZhciBwID0gZ2V0UGVyaW9kUGFyYW1zKCk7CiAgICB2YXIgZnJvbUQgPSBwYXJzZURNWShwLmZyb20pLCB0b0QgPSBwYXJzZURNWShwLnRvKTsKICAgIHZhciBidWNrZXRzID0ge307CiAgICBPYmplY3Qua2V5cyhmaXJzdFZpc2l0cykuZm9yRWFjaChmdW5jdGlvbihrKXsKICAgICAgdmFyIGQgPSBmaXJzdFZpc2l0c1trXTsKICAgICAgaWYoZCA8IGZyb21EIHx8IGQgPiB0b0QpIHJldHVybjsKICAgICAgdmFyIGtleSA9IGQuZ2V0RnVsbFllYXIoKSsnLScrKGQuZ2V0TW9udGgoKSsxKSsnLScrZC5nZXREYXRlKCk7CiAgICAgIGJ1Y2tldHNba2V5XSA9IChidWNrZXRzW2tleV18fDApKzE7CiAgICB9KTsKICAgIHZhciBjdXIgPSBuZXcgRGF0ZShmcm9tRCk7CiAgICB3aGlsZShjdXIgPD0gdG9EKXsKICAgICAgdmFyIGtleSA9IGN1ci5nZXRGdWxsWWVhcigpKyctJysoY3VyLmdldE1vbnRoKCkrMSkrJy0nK2N1ci5nZXREYXRlKCk7CiAgICAgIGxhYmVscy5wdXNoKHtrZXk6IFN0cmluZyhjdXIuZ2V0RGF0ZSgpKS5wYWRTdGFydCgyLCcwJykrJy4nK1N0cmluZyhjdXIuZ2V0TW9udGgoKSsxKS5wYWRTdGFydCgyLCcwJyksIHZhbDogYnVja2V0c1trZXldfHwwfSk7CiAgICAgIGN1ci5zZXREYXRlKGN1ci5nZXREYXRlKCkrMSk7CiAgICB9CiAgfSBlbHNlIHsKICAgIHZhciBtb250aE5hbWVzID0gWyfQr9C90LInLCfQpNC10LInLCfQnNCw0YAnLCfQkNC/0YAnLCfQnNCw0LknLCfQmNGO0L0nLCfQmNGO0LsnLCfQkNCy0LMnLCfQodC10L0nLCfQntC60YInLCfQndC+0Y8nLCfQlNC10LonXTsKICAgIHZhciBidWNrZXRzID0ge307CiAgICBPYmplY3Qua2V5cyhmaXJzdFZpc2l0cykuZm9yRWFjaChmdW5jdGlvbihrKXsKICAgICAgdmFyIGQgPSBmaXJzdFZpc2l0c1trXTsKICAgICAgdmFyIGtleSA9IGQuZ2V0RnVsbFllYXIoKSsnLScrU3RyaW5nKGQuZ2V0TW9udGgoKSsxKS5wYWRTdGFydCgyLCcwJyk7CiAgICAgIGJ1Y2tldHNba2V5XSA9IChidWNrZXRzW2tleV18fDApKzE7CiAgICB9KTsKICAgIHZhciBrZXlzID0gT2JqZWN0LmtleXMoYnVja2V0cykuc29ydCgpOwogICAgbGFiZWxzID0ga2V5cy5tYXAoZnVuY3Rpb24oayl7CiAgICAgIHZhciBwYXJ0cyA9IGsuc3BsaXQoJy0nKTsKICAgICAgcmV0dXJuIHtrZXk6IG1vbnRoTmFtZXNbcGFyc2VJbnQocGFydHNbMV0sMTApLTFdICsgJyAnICsgcGFydHNbMF0uc2xpY2UoMiksIHZhbDogYnVja2V0c1trXX07CiAgICB9KTsKICB9CiAgcmVuZGVyR3Jvd3RoU1ZHKGNvbnRhaW5lciwgbGFiZWxzKTsKfQoKZnVuY3Rpb24gcmVuZGVyR3Jvd3RoU1ZHKGNvbnRhaW5lciwgbGFiZWxzKXsKICBpZighbGFiZWxzLmxlbmd0aCl7IGNvbnRhaW5lci5pbm5lckhUTUwgPSAnPGRpdiBjbGFzcz0ibG9hZGluZyI+0J3QtdGCINC00LDQvdC90YvRhTwvZGl2Pic7IHJldHVybjsgfQogIHZhciBtYXhWYWwgPSBNYXRoLm1heC5hcHBseShudWxsLCBsYWJlbHMubWFwKGZ1bmN0aW9uKGwpe3JldHVybiBsLnZhbDt9KS5jb25jYXQoWzFdKSk7CiAgdmFyIHcgPSAzNDAsIGggPSAxNDA7CiAgdmFyIGJhckdhcCA9IDM7CiAgdmFyIGJhclcgPSBNYXRoLm1heCgyLCAodyAtIChsYWJlbHMubGVuZ3RoLTEpKmJhckdhcCkgLyBsYWJlbHMubGVuZ3RoKTsKICB2YXIgc3ZnID0gJzxzdmcgY2xhc3M9Imdyb3d0aC1zdmciIHZpZXdCb3g9IjAgMCAnICsgdyArICcgJyArIChoKzIwKSArICciIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyI+JzsKICBsYWJlbHMuZm9yRWFjaChmdW5jdGlvbihsLCBpKXsKICAgIHZhciBiYXJIID0gbWF4VmFsID4gMCA/IChsLnZhbCAvIG1heFZhbCkgKiBoIDogMDsKICAgIHZhciB4ID0gaSAqIChiYXJXICsgYmFyR2FwKTsKICAgIHZhciB5ID0gaCAtIGJhckg7CiAgICBzdmcgKz0gJzxyZWN0IHg9IicreC50b0ZpeGVkKDEpKyciIHk9IicreS50b0ZpeGVkKDEpKyciIHdpZHRoPSInK2JhclcudG9GaXhlZCgxKSsnIiBoZWlnaHQ9IicrYmFySC50b0ZpeGVkKDEpKyciIGZpbGw9InJnYmEoMjAxLDE2OCw3NiwnKyhsLnZhbD4wPzAuNzU6MC4xMikrJykiPjwvcmVjdD4nOwogICAgaWYobGFiZWxzLmxlbmd0aCA8PSAyMCAmJiBsLnZhbCA+IDApewogICAgICBzdmcgKz0gJzx0ZXh0IHg9IicrKHgrYmFyVy8yKS50b0ZpeGVkKDEpKyciIHk9IicrTWF0aC5tYXgoOSx5LTQpLnRvRml4ZWQoMSkrJyIgdGV4dC1hbmNob3I9Im1pZGRsZSIgY2xhc3M9Imdyb3d0aC1iYXItdmFsIj4nK2wudmFsKyc8L3RleHQ+JzsKICAgIH0KICB9KTsKICB2YXIgbGFiZWxTdGVwID0gTWF0aC5tYXgoMSwgTWF0aC5jZWlsKGxhYmVscy5sZW5ndGggLyA4KSk7CiAgbGFiZWxzLmZvckVhY2goZnVuY3Rpb24obCwgaSl7CiAgICBpZihpICUgbGFiZWxTdGVwICE9PSAwICYmIGkgIT09IGxhYmVscy5sZW5ndGgtMSkgcmV0dXJuOwogICAgdmFyIHggPSBpICogKGJhclcgKyBiYXJHYXApICsgYmFyVy8yOwogICAgc3ZnICs9ICc8dGV4dCB4PSInK3gudG9GaXhlZCgxKSsnIiB5PSInKyhoKzE0KSsnIiB0ZXh0LWFuY2hvcj0ibWlkZGxlIiBjbGFzcz0iZ3Jvd3RoLWJhci1sYWJlbCI+JytsLmtleSsnPC90ZXh0Pic7CiAgfSk7CiAgc3ZnICs9ICc8L3N2Zz4nOwogIGNvbnRhaW5lci5pbm5lckhUTUwgPSBzdmc7Cn0KCmZ1bmN0aW9uIGdldE1hc3RlclJhdGlvKG5hbWUpIHsKICB2YXIgbWFwID0gewogICAgJ9CQ0LvQuNGB0LAnOiAwLjQwLAogICAgJ9Ca0YHQtdC90LjRjyc6IDAuNDUsCiAgICAn0JDQvdC90LAnOiAxLjAwLAogICAgJ9CQ0LvQtdC60YHQsNC90LTRgNCwJzogMC41MCwKICAgICfQotCw0YLRjNGP0L3QsCc6IDAuNTAsCiAgICAn0JrRgNC40YHRgtC40L3QsCc6IDAuNTAKICB9OwogIHJldHVybiBtYXAuaGFzT3duUHJvcGVydHkobmFtZSkgPyBtYXBbbmFtZV0gOiAwLjUwOwp9CgpmdW5jdGlvbiByZWNhbGMoKSB7CiAgdmFyIGRpc2NvdW50ID0gcGFyc2VGbG9hdChkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnZGlzY291bnRJbnB1dCcpLnZhbHVlKSB8fCAwOwogIGRiLmRpc2NvdW50ID0gZGlzY291bnQ7CiAgc2F2ZSgpOwoKICB2YXIgdG90YWwgPSAwLCBjb3VudCA9IDA7CiAgdmFyIG1hc3Rlck1hcCA9IHt9OwogIHZhciBwaG9uZXMgPSB7fTsKCiAgYWxsQm9va2luZ3MuZm9yRWFjaChmdW5jdGlvbihiKSB7CiAgICB2YXIgcHJpY2UgPSBwYXJzZUZsb2F0KGIucHJpY2UpIHx8IDA7CiAgICB0b3RhbCArPSBwcmljZTsKICAgIGNvdW50Kys7CiAgICBpZiAoYi5jbGllbnRQaG9uZSkgcGhvbmVzW2IuY2xpZW50UGhvbmVdID0gdHJ1ZTsKICAgIHZhciBtID0gYi5tYXN0ZXI7CiAgICBpZiAoIW1hc3Rlck1hcFttXSkgbWFzdGVyTWFwW21dID0ge2Jvb2tpbmdzOltdLCB0b3RhbDowfTsKICAgIG1hc3Rlck1hcFttXS5ib29raW5ncy5wdXNoKGIpOwogICAgbWFzdGVyTWFwW21dLnRvdGFsICs9IHByaWNlOwogIH0pOwoKICB2YXIgbWFzdGVyVG90YWwgPSAwLCBzYWxvbkJlZm9yZURpc2NvdW50ID0gMDsKICBPYmplY3Qua2V5cyhtYXN0ZXJNYXApLmZvckVhY2goZnVuY3Rpb24obSkgewogICAgdmFyIHJhdGlvID0gZ2V0TWFzdGVyUmF0aW8obSk7CiAgICBtYXN0ZXJUb3RhbCArPSBtYXN0ZXJNYXBbbV0udG90YWwgKiByYXRpbzsKICAgIHNhbG9uQmVmb3JlRGlzY291bnQgKz0gbWFzdGVyTWFwW21dLnRvdGFsICogKDEgLSByYXRpbyk7CiAgfSk7CiAgbWFzdGVyVG90YWwgPSBNYXRoLnJvdW5kKG1hc3RlclRvdGFsKTsKICB2YXIgc2Fsb25Ub3RhbCA9IE1hdGgucm91bmQoc2Fsb25CZWZvcmVEaXNjb3VudCAtIGRpc2NvdW50KTsKICB2YXIgYXZnID0gY291bnQgPiAwID8gTWF0aC5yb3VuZCh0b3RhbCAvIGNvdW50KSA6IDA7CiAgdmFyIHVuaXF1ZUNsaWVudHMgPSBPYmplY3Qua2V5cyhwaG9uZXMpLmxlbmd0aDsKCiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ21Ub3RhbCcpLnRleHRDb250ZW50ICAgPSB0b3RhbCArICcg4oKsJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbU1hc3RlcnMnKS50ZXh0Q29udGVudCA9IG1hc3RlclRvdGFsICsgJyDigqwnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdtU2Fsb24nKS50ZXh0Q29udGVudCAgID0gc2Fsb25Ub3RhbCArICcg4oKsJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbUNvdW50JykudGV4dENvbnRlbnQgICA9IGNvdW50OwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdtQ2xpZW50cycpLnRleHRDb250ZW50ID0gdW5pcXVlQ2xpZW50czsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbUF2ZycpLnRleHRDb250ZW50ICAgICA9IGF2ZyArICcg4oKsJzsKCiAgdmFyIG5ld0NsaWVudHNFbCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdtTmV3Q2xpZW50cycpOwogIGlmIChuZXdDbGllbnRzRWwpIHsKICAgIGlmIChhbGxUaW1lQm9va2luZ3MubGVuZ3RoKSB7CiAgICAgIHZhciBwID0gZ2V0UGVyaW9kUGFyYW1zKCk7CiAgICAgIHZhciBmcm9tRCA9IHBhcnNlRE1ZKHAuZnJvbSksIHRvRCA9IHBhcnNlRE1ZKHAudG8pOwogICAgICB2YXIgZmlyc3RWaXNpdHMgPSBnZXRDbGllbnRGaXJzdFZpc2l0TWFwKCk7CiAgICAgIHZhciBuZXdDb3VudCA9IDA7CiAgICAgIE9iamVjdC5rZXlzKGZpcnN0VmlzaXRzKS5mb3JFYWNoKGZ1bmN0aW9uKGspewogICAgICAgIHZhciBkID0gZmlyc3RWaXNpdHNba107CiAgICAgICAgaWYgKGQgPj0gZnJvbUQgJiYgZCA8PSB0b0QpIG5ld0NvdW50Kys7CiAgICAgIH0pOwogICAgICBuZXdDbGllbnRzRWwudGV4dENvbnRlbnQgPSBuZXdDb3VudDsKICAgIH0gZWxzZSB7CiAgICAgIG5ld0NsaWVudHNFbC50ZXh0Q29udGVudCA9ICfigJQnOwogICAgfQogIH0KCiAgcmVuZGVyTWFzdGVycyhtYXN0ZXJNYXAsIGRpc2NvdW50LCB0b3RhbCk7Cn0KCmZ1bmN0aW9uIHJlbmRlck1hc3RlcnMobWFzdGVyTWFwLCBkaXNjb3VudCwgdG90YWxBbGwpIHsKICB2YXIgZWwgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbWFzdGVyc0xpc3QnKTsKICBpZiAoT2JqZWN0LmtleXMobWFzdGVyTWFwKS5sZW5ndGggPT09IDApIHsKICAgIGVsLmlubmVySFRNTCA9ICc8ZGl2IGNsYXNzPSJsb2FkaW5nIj7QndC10YIg0LfQsNC/0LjRgdC10Lkg0LfQsCDQstGL0LHRgNCw0L3QvdGL0Lkg0L/QtdGA0LjQvtC0PC9kaXY+JzsKICAgIHJldHVybjsKICB9CiAgdmFyIGh0bWwgPSAnJzsKICB2YXIgbWFzdGVycyA9IE9iamVjdC5rZXlzKG1hc3Rlck1hcCkuc29ydChmdW5jdGlvbihhLGIpeyByZXR1cm4gbWFzdGVyTWFwW2JdLnRvdGFsIC0gbWFzdGVyTWFwW2FdLnRvdGFsOyB9KTsKICBtYXN0ZXJzLmZvckVhY2goZnVuY3Rpb24obmFtZSkgewogICAgdmFyIGQgPSBtYXN0ZXJNYXBbbmFtZV07CiAgICB2YXIgcmF0aW8gPSBnZXRNYXN0ZXJSYXRpbyhuYW1lKTsKICAgIHZhciBtYXN0ZXJFYXJuID0gTWF0aC5yb3VuZChkLnRvdGFsICogcmF0aW8pOwogICAgdmFyIHNhbG9uU2hhcmUgPSBNYXRoLnJvdW5kKGQudG90YWwgKiAoMSAtIHJhdGlvKSk7CiAgICB2YXIgcmF0aW8gPSB0b3RhbEFsbCA+IDAgPyBkLnRvdGFsIC8gdG90YWxBbGwgOiAwOwogICAgdmFyIHNhbG9uRGlzY291bnQgPSBNYXRoLnJvdW5kKGRpc2NvdW50ICogcmF0aW8pOwogICAgdmFyIHNhbG9uRWFybiA9IHNhbG9uU2hhcmUgLSBzYWxvbkRpc2NvdW50OwogICAgdmFyIGlkID0gJ21jXycgKyBuYW1lLnJlcGxhY2UoL1xzL2csJ18nKTsKICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9Im1hc3Rlci1jYXJkIGFuaW0iPic7CiAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJtYXN0ZXItaGVhZCIgaWQ9Im1oXycraWQrJyIgb25jbGljaz0idG9nZ2xlTWFzdGVyKFwnJyArIGlkICsgJ1wnKSI+JzsKICAgIGh0bWwgKz0gJzxkaXY+PGRpdiBjbGFzcz0ibW5hbWUiPicgKyBuYW1lICsgJzwvZGl2Pic7CiAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJtY291bnQiPicgKyBkLmJvb2tpbmdzLmxlbmd0aCArICcg0LfQsNC/0LjRgdC10LkgwrcgJyArIGQudG90YWwgKyAnIOKCrCDQvtCx0YnQsNGPINGB0YPQvNC80LA8L2Rpdj48L2Rpdj4nOwogICAgaHRtbCArPSAnPGRpdiBjbGFzcz0ibWVhcm5pbmdzIj4nOwogICAgaHRtbCArPSAnPGRpdiBjbGFzcz0iZWFybi1pdGVtIj48ZGl2IGNsYXNzPSJlYXJuLWxhYmVsIj7QnNCw0YHRgtC10YA8L2Rpdj48ZGl2IGNsYXNzPSJlYXJuLXZhbCI+JyArIG1hc3RlckVhcm4gKyAnIOKCrDwvZGl2PjwvZGl2Pic7CiAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJlYXJuLWl0ZW0iPjxkaXYgY2xhc3M9ImVhcm4tbGFiZWwiPtCh0LDQu9C+0L08L2Rpdj48ZGl2IGNsYXNzPSJlYXJuLXZhbCBzYWxvbiI+JyArIHNhbG9uRWFybiArICcg4oKsPC9kaXY+PC9kaXY+JzsKICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9ImNoZXZyb24iPuKWvDwvZGl2PjwvZGl2PjwvZGl2Pic7CiAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJtYXN0ZXItYm9keSIgaWQ9Im1iXycraWQrJyI+JzsKICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9InZpc2l0cy1oZWFkZXIiPjxzcGFuPtCU0LDRgtCwPC9zcGFuPjxzcGFuPtCa0LvQuNC10L3RgiAvINCf0LjRgtC+0LzQtdGGPC9zcGFuPjxzcGFuPtCj0YHQu9GD0LPQsDwvc3Bhbj48c3BhbiBzdHlsZT0idGV4dC1hbGlnbjpyaWdodCI+0KbQtdC90LA8L3NwYW4+PC9kaXY+JzsKICAgIHZhciBzb3J0ZWQgPSBkLmJvb2tpbmdzLnNsaWNlKCkuc29ydChmdW5jdGlvbihhLGIpeyByZXR1cm4gYi5kYXRlLmxvY2FsZUNvbXBhcmUoYS5kYXRlKTsgfSk7CiAgICBzb3J0ZWQuZm9yRWFjaChmdW5jdGlvbihiKSB7CiAgICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9InZpc2l0LXJvdyI+JzsKICAgICAgaHRtbCArPSAnPGRpdiBjbGFzcz0idmlzaXQtZGF0ZSI+JyArIGIuZGF0ZSArICc8L2Rpdj4nOwogICAgICBodG1sICs9ICc8ZGl2PjxkaXYgY2xhc3M9InZpc2l0LWNsaWVudCI+JyArIChiLmNsaWVudE5hbWV8fCfigJQnKSArICc8L2Rpdj4nOwogICAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJ2aXNpdC1wZXQiPicgKyAoYi5wZXROYW1lID8gYi5wZXROYW1lICsgJyDCtyAnIDogJycpICsgYi5icmVlZCArICc8L2Rpdj48L2Rpdj4nOwogICAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJ2aXNpdC1zdmMiPicgKyBiLnNlcnZpY2UgKyAnPC9kaXY+JzsKICAgICAgaHRtbCArPSAnPGRpdiBjbGFzcz0idmlzaXQtcHJpY2UiPicgKyAoYi5wcmljZXx8MCkgKyAnIOKCrDwvZGl2PjwvZGl2Pic7CiAgICB9KTsKICAgIGh0bWwgKz0gJzwvZGl2PjwvZGl2Pic7CiAgfSk7CiAgZWwuaW5uZXJIVE1MID0gaHRtbDsKfQoKZnVuY3Rpb24gdG9nZ2xlTWFzdGVyKGlkKSB7CiAgdmFyIGhlYWQgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbWhfJytpZCk7CiAgdmFyIGJvZHkgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbWJfJytpZCk7CiAgaGVhZC5jbGFzc0xpc3QudG9nZ2xlKCdvcGVuJyk7CiAgYm9keS5jbGFzc0xpc3QudG9nZ2xlKCdvcGVuJyk7Cn0KCi8vIOKUgOKUgCDQo9Cf0KDQkNCS0JvQldCd0JjQlTog0JzQkNCh0KLQldCg0JAg4pSA4pSACmZ1bmN0aW9uIHJlbmRlck1hc3Rlckxpc3QoKSB7CiAgdmFyIGVsID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ21hc3Rlckxpc3RVSScpOwogIGlmICghZWwpIHJldHVybjsKICB2YXIgaHRtbCA9ICcnOwogIGRiLm1hc3RlcnMuZm9yRWFjaChmdW5jdGlvbihuYW1lLCBpKSB7CiAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJsaXN0LWl0ZW0iPjxkaXY+PGRpdiBjbGFzcz0ibGktbmFtZSI+JytuYW1lKyc8L2Rpdj48L2Rpdj4nOwogICAgaHRtbCArPSAnPGJ1dHRvbiBjbGFzcz0iZGVsLWJ0biIgb25jbGljaz0iZGVsTWFzdGVyKCcraSsnKSI+4pyVPC9idXR0b24+PC9kaXY+JzsKICB9KTsKICBlbC5pbm5lckhUTUwgPSBodG1sIHx8ICc8ZGl2IHN0eWxlPSJjb2xvcjojNDQ0NDQwO2ZvbnQtc2l6ZTouNzVyZW07cGFkZGluZzoxMnB4IDAiPtCd0LXRgiDQvNCw0YHRgtC10YDQvtCyPC9kaXY+JzsKfQpmdW5jdGlvbiBhZGRNYXN0ZXIoKSB7CiAgdmFyIG5hbWUgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbmV3TWFzdGVyTmFtZScpLnZhbHVlLnRyaW0oKTsKICBpZiAoIW5hbWUpIHJldHVybjsKICBkYi5tYXN0ZXJzLnB1c2gobmFtZSk7CiAgc2F2ZSgpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCduZXdNYXN0ZXJOYW1lJykudmFsdWUgPSAnJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbmV3TWFzdGVyUGhvbmUnKS52YWx1ZSA9ICcnOwogIHJlbmRlck1hc3Rlckxpc3QoKTsKfQpmdW5jdGlvbiBkZWxNYXN0ZXIoaSkgewogIGlmICghY29uZmlybSgn0KPQtNCw0LvQuNGC0Ywg0LzQsNGB0YLQtdGA0LA/JykpIHJldHVybjsKICBkYi5tYXN0ZXJzLnNwbGljZShpLCAxKTsKICBzYXZlKCk7CiAgcmVuZGVyTWFzdGVyTGlzdCgpOwp9CgovLyDilIDilIAg0KPQn9Cg0JDQktCb0JXQndCY0JU6INCf0J7QoNCe0JTQqyDilIDilIAKZnVuY3Rpb24gcmVuZGVyQnJlZWRMaXN0KCkgewogIHZhciBlbCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdicmVlZExpc3RVSScpOwogIGlmICghZWwpIHJldHVybjsKICB2YXIgaHRtbCA9ICcnOwogIGRiLmJyZWVkcy5mb3JFYWNoKGZ1bmN0aW9uKGJyZWVkLCBiaSkgewogICAgdmFyIGJpZCA9ICdicl8nK2JpOwogICAgaHRtbCArPSAnPGRpdiBjbGFzcz0iYnJlZWQtY2FyZCI+JzsKICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9ImJyZWVkLWhlYWQiIG9uY2xpY2s9InRvZ2dsZUJyZWVkKFwnJyArIGJpZCArICdcJykiPic7CiAgICBodG1sICs9ICc8ZGl2PjxkaXYgY2xhc3M9ImJyZWVkLW5hbWUiPicrYnJlZWQubmFtZSsnPC9kaXY+JzsKICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9ImJyZWVkLWNvdW50Ij4nKyhicmVlZC5zZXJ2aWNlc3x8W10pLmxlbmd0aCsnINGD0YHQu9GD0LM8L2Rpdj48L2Rpdj4nOwogICAgaHRtbCArPSAnPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTBweCI+JzsKICAgIGh0bWwgKz0gJzxidXR0b24gY2xhc3M9ImRlbC1idG4iIG9uY2xpY2s9ImV2ZW50LnN0b3BQcm9wYWdhdGlvbigpO2RlbEJyZWVkKCcrYmkrJykiPuKclTwvYnV0dG9uPic7CiAgICBodG1sICs9ICc8c3BhbiBzdHlsZT0iY29sb3I6IzhhOGE1NTtmb250LXNpemU6Ljc1cmVtIj7ilrw8L3NwYW4+PC9kaXY+PC9kaXY+JzsKICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9ImJyZWVkLWJvZHkiIGlkPSInK2JpZCsnIj4nOwogICAgKGJyZWVkLnNlcnZpY2VzfHxbXSkuZm9yRWFjaChmdW5jdGlvbihzdmMsIHNpKSB7CiAgICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9InN2Yy1yb3ciPjxzcGFuIGNsYXNzPSJzdmMtbmFtZSI+JytzdmMubmFtZSsnPC9zcGFuPic7CiAgICAgIGh0bWwgKz0gJzxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEwcHgiPic7CiAgICAgIGh0bWwgKz0gJzxzcGFuIGNsYXNzPSJzdmMtcHJpY2UiPicrc3ZjLnByaWNlKycg4oKsPC9zcGFuPic7CiAgICAgIGh0bWwgKz0gJzxidXR0b24gY2xhc3M9ImRlbC1idG4iIG9uY2xpY2s9ImRlbFN2YygnK2JpKycsJytzaSsnKSI+4pyVPC9idXR0b24+PC9kaXY+PC9kaXY+JzsKICAgIH0pOwogICAgaHRtbCArPSAnPGRpdiBjbGFzcz0iYWRkLXN2Yy1yb3ciPic7CiAgICBodG1sICs9ICc8aW5wdXQgY2xhc3M9ImZpIiBpZD0ic25fJytiaSsnIiBwbGFjZWhvbGRlcj0i0KPRgdC70YPQs9CwIj4nOwogICAgaHRtbCArPSAnPGlucHV0IGNsYXNzPSJmaSIgaWQ9InNwXycrYmkrJyIgcGxhY2Vob2xkZXI9ItCm0LXQvdCwIOKCrCI+JzsKICAgIGh0bWwgKz0gJzxidXR0b24gY2xhc3M9Imljb24tYnRuIiBvbmNsaWNrPSJhZGRTdmMoJytiaSsnKSI+KzwvYnV0dG9uPjwvZGl2Pic7CiAgICBodG1sICs9ICc8L2Rpdj48L2Rpdj4nOwogIH0pOwogIGVsLmlubmVySFRNTCA9IGh0bWwgfHwgJzxkaXYgc3R5bGU9ImNvbG9yOiM0NDQ0NDA7Zm9udC1zaXplOi43NXJlbTtwYWRkaW5nOjEycHggMCI+0J3QtdGCINC/0L7RgNC+0LQ8L2Rpdj4nOwogIHJlbmRlckJyZWVkU2VsZWN0KCk7Cn0KZnVuY3Rpb24gdG9nZ2xlQnJlZWQoaWQpIHsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZChpZCkuY2xhc3NMaXN0LnRvZ2dsZSgnb3BlbicpOwp9CmZ1bmN0aW9uIGFkZEJyZWVkKCkgewogIHZhciBuYW1lID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ25ld0JyZWVkTmFtZScpLnZhbHVlLnRyaW0oKTsKICBpZiAoIW5hbWUpIHJldHVybjsKICBkYi5icmVlZHMucHVzaCh7bmFtZTogbmFtZSwgc2VydmljZXM6IFtdfSk7CiAgc2F2ZSgpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCduZXdCcmVlZE5hbWUnKS52YWx1ZSA9ICcnOwogIHJlbmRlckJyZWVkTGlzdCgpOwp9CmZ1bmN0aW9uIGRlbEJyZWVkKGkpIHsKICBpZiAoIWNvbmZpcm0oJ9Cj0LTQsNC70LjRgtGMINC/0L7RgNC+0LTRgyDQuCDQstGB0LUg0LXRkSDRg9GB0LvRg9Cz0Lg/JykpIHJldHVybjsKICBkYi5icmVlZHMuc3BsaWNlKGksIDEpOwogIHNhdmUoKTsKICByZW5kZXJCcmVlZExpc3QoKTsKfQpmdW5jdGlvbiBhZGRTdmMoYmkpIHsKICB2YXIgbmFtZSAgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc25fJytiaSkudmFsdWUudHJpbSgpOwogIHZhciBwcmljZSA9IHBhcnNlRmxvYXQoZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3NwXycrYmkpLnZhbHVlKSB8fCAwOwogIGlmICghbmFtZSB8fCAhcHJpY2UpIHJldHVybjsKICBkYi5icmVlZHNbYmldLnNlcnZpY2VzLnB1c2goe25hbWU6IG5hbWUsIHByaWNlOiBwcmljZX0pOwogIHNhdmUoKTsKICByZW5kZXJCcmVlZExpc3QoKTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnYnJfJytiaSkuY2xhc3NMaXN0LmFkZCgnb3BlbicpOwp9CmZ1bmN0aW9uIGRlbFN2YyhiaSwgc2kpIHsKICBkYi5icmVlZHNbYmldLnNlcnZpY2VzLnNwbGljZShzaSwgMSk7CiAgc2F2ZSgpOwogIHJlbmRlckJyZWVkTGlzdCgpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdicl8nK2JpKS5jbGFzc0xpc3QuYWRkKCdvcGVuJyk7Cn0KZnVuY3Rpb24gcmVuZGVyQnJlZWRTZWxlY3QoKSB7CiAgdmFyIHNlbCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCduZXdDbGllbnRCcmVlZCcpOwogIGlmICghc2VsKSByZXR1cm47CiAgc2VsLmlubmVySFRNTCA9ICc8b3B0aW9uIHZhbHVlPSIiPtCf0L7RgNC+0LTQsC4uLjwvb3B0aW9uPic7CiAgZGIuYnJlZWRzLmZvckVhY2goZnVuY3Rpb24oYikgewogICAgc2VsLmlubmVySFRNTCArPSAnPG9wdGlvbj4nK2IubmFtZSsnPC9vcHRpb24+JzsKICB9KTsKfQoKLy8g4pSA4pSAINCj0J/QoNCQ0JLQm9CV0J3QmNCVOiDQmtCb0JjQldCd0KLQqyDilIDilIAKZnVuY3Rpb24gcmVuZGVyQ2xpZW50TGlzdCgpIHsKICB2YXIgZWwgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2xpZW50TGlzdFVJJyk7CiAgaWYgKCFlbCkgcmV0dXJuOwogIHZhciBtZXJnZWQgPSB7fTsKICBkYi5jbGllbnRzLmZvckVhY2goZnVuY3Rpb24oYykgewogICAgbWVyZ2VkW2MucGhvbmV8fGMubmFtZV0gPSBjOwogIH0pOwogIGFsbEJvb2tpbmdzLmZvckVhY2goZnVuY3Rpb24oYikgewogICAgaWYgKCFiLmNsaWVudFBob25lICYmICFiLmNsaWVudE5hbWUpIHJldHVybjsKICAgIHZhciBrZXkgPSBiLmNsaWVudFBob25lIHx8IGIuY2xpZW50TmFtZTsKICAgIGlmICghbWVyZ2VkW2tleV0pIHsKICAgICAgbWVyZ2VkW2tleV0gPSB7bmFtZTogYi5jbGllbnROYW1lLCBwaG9uZTogYi5jbGllbnRQaG9uZSwgcGV0OiBiLnBldE5hbWUsIGJyZWVkOiBiLmJyZWVkLCBub3RlOiAnJywgdmlzaXRzOiAwLCB0b3RhbDogMH07CiAgICB9CiAgICBtZXJnZWRba2V5XS52aXNpdHMgPSAobWVyZ2VkW2tleV0udmlzaXRzfHwwKSArIDE7CiAgICBtZXJnZWRba2V5XS50b3RhbCAgPSAobWVyZ2VkW2tleV0udG90YWx8fDApICsgKHBhcnNlRmxvYXQoYi5wcmljZSl8fDApOwogICAgbWVyZ2VkW2tleV0ubGFzdERhdGUgICA9IGIuZGF0ZTsKICAgIG1lcmdlZFtrZXldLmxhc3RNYXN0ZXIgPSBiLm1hc3RlcjsKICB9KTsKICB2YXIgYXJyID0gT2JqZWN0LnZhbHVlcyhtZXJnZWQpOwogIGFyci5zb3J0KGZ1bmN0aW9uKGEsYil7IHJldHVybiAoYi52aXNpdHN8fDApLShhLnZpc2l0c3x8MCk7IH0pOwogIHZhciBodG1sID0gJyc7CiAgYXJyLmZvckVhY2goZnVuY3Rpb24oYywgaSkgewogICAgdmFyIGluaXRpYWxzID0gKGMubmFtZXx8Jz8nKS5zcGxpdCgnICcpLm1hcChmdW5jdGlvbih3KXtyZXR1cm4gd1swXXx8Jyc7fSkuam9pbignJykuc3Vic3RyaW5nKDAsMikudG9VcHBlckNhc2UoKTsKICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9ImNsaWVudC1jYXJkIGFuaW0iPic7CiAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJjbC1yb3ciPic7CiAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJjbC1hdmF0YXIiPicraW5pdGlhbHMrJzwvZGl2Pic7CiAgICBodG1sICs9ICc8ZGl2IHN0eWxlPSJmbGV4OjEiPjxkaXYgY2xhc3M9ImNsLW5hbWUiPicrKGMubmFtZXx8J+KAlCcpKyc8L2Rpdj4nOwogICAgaHRtbCArPSAnPGRpdiBjbGFzcz0iY2wtZGV0YWlsIj4nKyhjLnBob25lfHwnJykrKGMucGV0PycgwrcgJytjLnBldDonJykrKGMuYnJlZWQ/JyDCtyAnK2MuYnJlZWQ6JycpKyc8L2Rpdj48L2Rpdj4nOwogICAgaWYgKCFjLmZyb21DYWxlbmRhcikgewogICAgICBodG1sICs9ICc8YnV0dG9uIGNsYXNzPSJkZWwtYnRuIiBvbmNsaWNrPSJkZWxDbGllbnQoJytpKycpIj7inJU8L2J1dHRvbj4nOwogICAgfQogICAgaHRtbCArPSAnPC9kaXY+JzsKICAgIGlmICgoYy52aXNpdHN8fDApID4gMCkgewogICAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJjbC1zdGF0cyI+JzsKICAgICAgaHRtbCArPSAnPGRpdiBjbGFzcz0iY3N0YXQiPjxkaXYgY2xhc3M9ImNzdC12YWwiPicrKGMudmlzaXRzfHwwKSsnPC9kaXY+PGRpdiBjbGFzcz0iY3N0LWxhYmVsIj7QstC40LfQuNGC0L7QsjwvZGl2PjwvZGl2Pic7CiAgICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9ImNzdGF0Ij48ZGl2IGNsYXNzPSJjc3QtdmFsIj4nKyhjLnRvdGFsfHwwKSsnIOKCrDwvZGl2PjxkaXYgY2xhc3M9ImNzdC1sYWJlbCI+0L/QvtGC0YDQsNGH0LXQvdC+PC9kaXY+PC9kaXY+JzsKICAgICAgaHRtbCArPSAnPC9kaXY+JzsKICAgICAgaWYgKGMubGFzdERhdGUpIHsKICAgICAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJjbC1sYXN0Ij7Qn9C+0YHQu9C10LTQvdC40Lkg0LLQuNC30LjRgjogPHNwYW4+JytjLmxhc3REYXRlKyhjLmxhc3RNYXN0ZXI/JyDCtyAnK2MubGFzdE1hc3RlcjonJykrJzwvc3Bhbj48L2Rpdj4nOwogICAgICB9CiAgICB9CiAgICBpZiAoYy5ub3RlKSBodG1sICs9ICc8ZGl2IHN0eWxlPSJmb250LXNpemU6LjYycmVtO2NvbG9yOiM1NTU1NTA7bWFyZ2luLXRvcDo2cHgiPicrYy5ub3RlKyc8L2Rpdj4nOwogICAgaHRtbCArPSAnPC9kaXY+JzsKICB9KTsKICBlbC5pbm5lckhUTUwgPSBodG1sIHx8ICc8ZGl2IHN0eWxlPSJjb2xvcjojNDQ0NDQwO2ZvbnQtc2l6ZTouNzVyZW07cGFkZGluZzoxMnB4IDAiPtCd0LXRgiDQutC70LjQtdC90YLQvtCyPC9kaXY+JzsKfQpmdW5jdGlvbiBhZGRDbGllbnQoKSB7CiAgdmFyIG5hbWUgID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ25ld0NsaWVudE5hbWUnKS52YWx1ZS50cmltKCk7CiAgdmFyIHBob25lID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ25ld0NsaWVudFBob25lJykudmFsdWUudHJpbSgpOwogIHZhciBwZXQgICA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCduZXdDbGllbnRQZXQnKS52YWx1ZS50cmltKCk7CiAgdmFyIGJyZWVkID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ25ld0NsaWVudEJyZWVkJykudmFsdWU7CiAgdmFyIG5vdGUgID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ25ld0NsaWVudE5vdGUnKS52YWx1ZS50cmltKCk7CiAgaWYgKCFuYW1lKSB7IGFsZXJ0KCfQktCy0LXQtNC40YLQtSDQuNC80Y8g0LrQu9C40LXQvdGC0LAnKTsgcmV0dXJuOyB9CiAgZGIuY2xpZW50cy5wdXNoKHtuYW1lOm5hbWUsIHBob25lOnBob25lLCBwZXQ6cGV0LCBicmVlZDpicmVlZCwgbm90ZTpub3RlLCB2aXNpdHM6MCwgdG90YWw6MH0pOwogIHNhdmUoKTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbmV3Q2xpZW50TmFtZScpLnZhbHVlID0gJyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ25ld0NsaWVudFBob25lJykudmFsdWUgPSAnJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbmV3Q2xpZW50UGV0JykudmFsdWUgPSAnJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbmV3Q2xpZW50Tm90ZScpLnZhbHVlID0gJyc7CiAgcmVuZGVyQ2xpZW50TGlzdCgpOwp9CmZ1bmN0aW9uIGRlbENsaWVudChpKSB7CiAgaWYgKCFjb25maXJtKCfQo9C00LDQu9C40YLRjCDQutCw0YDRgtC+0YfQutGDINC60LvQuNC10L3RgtCwPycpKSByZXR1cm47CiAgZGIuY2xpZW50cy5zcGxpY2UoaSwgMSk7CiAgc2F2ZSgpOwogIHJlbmRlckNsaWVudExpc3QoKTsKfQoKLy8g4pSA4pSAIElOSVQg4pSA4pSACmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdkaXNjb3VudElucHV0JykudmFsdWUgPSBkYi5kaXNjb3VudDsKcmVuZGVyTWFzdGVyTGlzdCgpOwpyZW5kZXJCcmVlZExpc3QoKTsKcmVuZGVyQ2xpZW50TGlzdCgpOwpsb2FkU3RhdHMoKTsKPC9zY3JpcHQ+CjwvYm9keT4KPC9odG1sPg=="

@app.route("/stats")
def stats_page():
    import base64
    html = base64.b64decode(STATS_HTML_B64).decode("utf-8")
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/api/stats")
def api_stats():
    from_date = request.args.get("from", "")
    to_date   = request.args.get("to", "")
    try:
        params = {"action": "stats"}
        if from_date: params["from"] = from_date
        if to_date:   params["to"]   = to_date
        r = requests.get(GOOGLE_SCRIPT, params=params, timeout=20)
        resp = jsonify(r.json())
    except Exception as e:
        resp = jsonify({"success": False, "error": str(e), "bookings": []})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp

# ── /test-chat ────────────────────────────────────────────────────────────────
_TEST_CHAT_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Анна — тест чата</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#0b141a;height:100vh;display:flex;flex-direction:column;overflow:hidden}
.header{background:#202c33;padding:10px 16px;display:flex;align-items:center;
        gap:12px;border-bottom:1px solid #111b21;flex-shrink:0}
.avatar{width:40px;height:40px;border-radius:50%;background:#00a884;
        display:flex;align-items:center;justify-content:center;font-size:20px;flex-shrink:0}
.hinfo{flex:1}
.hname{color:#e9edef;font-size:15px;font-weight:600}
.hstatus{color:#8696a0;font-size:12px;margin-top:1px}
.reset{background:none;border:1px solid #8696a0;color:#8696a0;padding:5px 12px;
       border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap}
.reset:hover{border-color:#e9edef;color:#e9edef}
.messages{flex:1;overflow-y:auto;padding:16px 5vw;display:flex;flex-direction:column;gap:2px}
.messages::-webkit-scrollbar{width:5px}
.messages::-webkit-scrollbar-thumb{background:#374045;border-radius:3px}
.bubble{max-width:72%;padding:8px 12px 4px;border-radius:8px;
        font-size:14px;line-height:1.5;word-break:break-word;margin:1px 0}
.bubble .ts{font-size:11px;color:#8696a0;text-align:right;margin-top:3px}
.jarvis{background:#202c33;color:#e9edef;align-self:flex-start;border-top-left-radius:0}
.client{background:#005c4b;color:#e9edef;align-self:flex-end;border-top-right-radius:0}
.typing-row{display:none;align-self:flex-start;padding:2px 0}
.dots{display:flex;gap:5px;padding:10px 14px;background:#202c33;border-radius:8px;
      border-top-left-radius:0}
.dots span{width:8px;height:8px;border-radius:50%;background:#8696a0;
           animation:blink 1.2s infinite}
.dots span:nth-child(2){animation-delay:.2s}
.dots span:nth-child(3){animation-delay:.4s}
@keyframes blink{0%,60%,100%{opacity:.3}30%{opacity:1}}
.input-bar{background:#202c33;padding:10px 12px;display:flex;align-items:center;
           gap:8px;flex-shrink:0}
.input-bar input{flex:1;background:#2a3942;border:none;border-radius:24px;
                 padding:10px 16px;color:#e9edef;font-size:15px;outline:none}
.input-bar input::placeholder{color:#8696a0}
.send{width:44px;height:44px;border-radius:50%;background:#00a884;border:none;
      cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.send:disabled{background:#3a4a52;cursor:default}
.send svg{fill:#fff}
</style>
</head>
<body>
<div class="header">
  <div class="avatar">🐾</div>
  <div class="hinfo">
    <div class="hname">Анна — R&amp;J Grooming</div>
    <div class="hstatus" id="st">онлайн</div>
  </div>
  <button class="reset" onclick="resetChat()">Сбросить</button>
</div>
<div class="messages" id="msgs"></div>
<div class="typing-row" id="typing"><div class="dots"><span></span><span></span><span></span></div></div>
<div class="input-bar">
  <input id="inp" type="text" placeholder="Сообщение" onkeydown="if(event.key==='Enter')send()">
  <button class="send" id="sb" onclick="send()">
    <svg viewBox="0 0 24 24" width="22" height="22"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
  </button>
</div>
<script>
const msgs=document.getElementById('msgs'),inp=document.getElementById('inp'),
      typing=document.getElementById('typing'),sb=document.getElementById('sb'),
      st=document.getElementById('st');
function ts(){return new Date().toLocaleTimeString('ru',{hour:'2-digit',minute:'2-digit'})}
function addBubble(text,role){
  const d=document.createElement('div');
  d.className='bubble '+role;
  const html=text.replace(/\\*\\*(.*?)\\*\\*/g,'<b>$1</b>').replace(/\\n/g,'<br>');
  d.innerHTML=html+'<div class="ts">'+ts()+'</div>';
  msgs.appendChild(d);
  msgs.scrollTop=msgs.scrollHeight;
}
async function send(){
  const text=inp.value.trim();if(!text)return;
  inp.value='';sb.disabled=true;
  addBubble(text,'client');
  typing.style.display='flex';msgs.scrollTop=msgs.scrollHeight;
  st.textContent='печатает…';
  try{
    const r=await fetch('/test-chat/send',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});
    const d=await r.json();
    typing.style.display='none';st.textContent='онлайн';
    if(d.reply)addBubble(d.reply,'jarvis');
    if(d.second_reply){
      await new Promise(r=>setTimeout(r,900));
      typing.style.display='flex';msgs.scrollTop=msgs.scrollHeight;
      await new Promise(r=>setTimeout(r,700));
      typing.style.display='none';
      addBubble(d.second_reply,'jarvis');
    }
    if(d.booked){st.textContent='запись принята ✓';setTimeout(()=>st.textContent='онлайн',4000);}
  }catch(e){
    typing.style.display='none';st.textContent='ошибка';
    addBubble('Ошибка соединения. Попробуйте ещё раз.','jarvis');
  }
  sb.disabled=false;inp.focus();
}
async function resetChat(){
  await fetch('/test-chat/reset',{method:'POST'});
  msgs.innerHTML='';st.textContent='онлайн';
}
inp.focus();
</script>
</body>
</html>"""

@app.route("/test-chat")
def test_chat():
    return _TEST_CHAT_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/test-chat/send", methods=["POST"])
def test_chat_send():
    try:
        return _test_chat_send()
    except Exception:
        import traceback
        print(f"[test-chat] UNHANDLED ERROR:\n{traceback.format_exc()}", flush=True)
        return jsonify({"reply": "Произошла ошибка сервера. Попробуйте ещё раз 🤍",
                        "state": {}, "booked": False}), 200

def _test_chat_send():
    data = request.get_json() or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "empty"}), 400

    sid = session.get("chat_sid")
    if not sid or sid not in _chat_sessions:
        sid = str(uuid.uuid4())
        session["chat_sid"] = sid
        _chat_sessions[sid] = {"history": [], "state": _blank_state(), "avail": {}}

    sess = _chat_sessions[sid]
    history, state = sess["history"], sess["state"]
    avail = sess.setdefault("avail", {})

    history.append({"role": "user", "content": text})
    if len(history) > MAX_HISTORY:
        history[:] = history[-MAX_HISTORY:]

    # Save flags before extraction to detect transitions
    prev_service_confirmed = bool(state.get("service_confirmed"))
    prev_breed  = state.get("breed")
    prev_phone  = state.get("clientPhone")

    # Extract state BEFORE generating reply so bot knows about slot availability
    new_state = _extract_state(history, state)

    booking_intent = bool(new_state.get("booking_intent"))

    # Shared flags for step guards
    _services_shown = sess.get("services_shown", False)
    _breed_known    = bool(new_state.get("breed"))
    _weight_known   = bool(new_state.get("weight"))
    _had_greeting   = any(m["role"] == "assistant" for m in history)

    # ── Subtype clarification: breed with varieties → ask before weight/services ──
    if (_breed_known and not _services_shown
            and not new_state.get("service_confirmed")
            and not booking_intent
            and _had_greeting):
        _clarify_q = _check_breed_needs_clarification(new_state.get("breed"))
        if _clarify_q:
            last_asst = next((m["content"] for m in reversed(history)
                              if m["role"] == "assistant"), "")
            if "уточните" not in last_asst.lower():
                history.append({"role": "assistant", "content": _clarify_q})
                sess["state"] = new_state
                print(f"[test-chat] subtype-clarify: breed={new_state.get('breed')!r}", flush=True)
                return jsonify({"reply": _clarify_q, "state": new_state, "booked": False})

    # ── Step 3 (strict): breed known, weight not yet → inject weight question ──
    # Ensures Claude never skips asking for weight before showing services.
    if (_breed_known and not _weight_known and not _services_shown
            and not new_state.get("service_confirmed")
            and not booking_intent
            and _had_greeting):
        last_asst = next((m["content"] for m in reversed(history)
                          if m["role"] == "assistant"), "")
        _weight_already_asked = any(w in last_asst.lower()
                                    for w in ["вес", "весит", "килограмм", "сколько кг"])
        if not _weight_already_asked:
            reply = (_breed_compliment(new_state["breed"])
                     + "\n\nПодскажите, пожалуйста, примерный вес питомца?")
            history.append({"role": "assistant", "content": reply})
            sess["state"] = new_state
            print(f"[test-chat] step3-weight: breed={new_state['breed']!r}", flush=True)
            return jsonify({"reply": reply, "state": new_state, "booked": False})

    # ── Step 4 (strict): breed + weight both known → show services ──────────────
    if (_breed_known and _weight_known and not _services_shown
            and not new_state.get("service_confirmed")
            and not booking_intent
            and _had_greeting):
        services = _get_all_services_for_breed(new_state["breed"])
        sess["services_shown"] = True  # mark regardless so we don't retry on empty
        if services:
            lines = [f"• {svc} — {_SVC_DESCRIPTIONS.get(svc, '')}" for svc, _ in services]
            comp = next((svc for svc, _ in services if 'комплексный' in svc.lower()), None)
            gig  = next((svc for svc, _ in services if 'гигиенический' in svc.lower()), None)
            recommend = comp or gig or services[-1][0]
            compliment = _breed_compliment(new_state["breed"])
            msg = (compliment + "\n\nДля вашей породы доступны следующие услуги:\n"
                   + "\n".join(lines))
            if comp:
                msg += (f"\n\nКомплексный уход — самый полный вариант: питомец выходит "
                        f"полностью ухоженным — шерсть, когти, уши, глаза и стрижка 🤍 "
                        f"Многие клиенты выбирают именно его.\n\n"
                        f"Я бы посоветовала {recommend}. Вам подходит?")
            else:
                msg += (f"\n\nЯ бы посоветовала {recommend} — для вашей породы это "
                        "наиболее полный вариант ухода. Вам подходит?")
            history.append({"role": "assistant", "content": msg})
            sess["state"] = new_state
            print(f"[test-chat] auto-services: breed={new_state['breed']!r} prev={prev_breed!r} "
                  f"services={[s for s,_ in services]} recommend={recommend!r}", flush=True)
            return jsonify({"reply": msg, "state": new_state, "booked": False})

    # ── Phone validation: must be +372XXXXXXX format ─────────────────────────
    _new_phone = new_state.get("clientPhone")
    if _new_phone and _new_phone != prev_phone:
        _clean = re.sub(r'[\s\-()]', '', str(_new_phone))
        if not re.match(r'^\+372\d{6,8}$', _clean):
            new_state["clientPhone"] = None  # reset invalid phone
            reply = ("Пожалуйста, укажите номер телефона в формате +372XXXXXXX "
                     "(например, +37253112233) 🤍")
            history.append({"role": "assistant", "content": reply})
            sess["state"] = new_state
            print(f"[test-chat] phone-invalid: {_new_phone!r}", flush=True)
            return jsonify({"reply": reply, "state": new_state, "booked": False})

    # ── Step 6 (strict): service just confirmed → inject price from Python ─────
    just_confirmed_service = (not prev_service_confirmed) and bool(new_state.get("service_confirmed"))
    if just_confirmed_service:
        breed   = new_state.get("breed")
        service = new_state.get("service")
        price   = _lookup_price(breed, service)
        service_label = service or "Услуга"
        if price:
            disclaimer = _DISCLAIMER_SMOOTH if _is_smooth_coat(breed) else _DISCLAIMER_OTHER
            reply = f"{service_label} — от {price}€.\n\n{disclaimer}\n\nХотите записаться?"
        else:
            disclaimer = _DISCLAIMER_SMOOTH if _is_smooth_coat(breed) else _DISCLAIMER_OTHER
            reply = f"{service_label} отлично подойдёт вашему питомцу 🤍\n\n{disclaimer}\n\nХотите записаться?"
        history.append({"role": "assistant", "content": reply})
        new_state["booking_intent"] = False  # reset: next "да" must be answer to "Хотите записаться?"
        new_state["price_shown"]    = True   # persist in state so it survives across requests
        sess["state"]       = new_state
        sess["price_shown"] = True           # also in session for same-request reads
        print(f"[test-chat] price-inject: breed={breed!r} service={service!r} price={price}", flush=True)
        return jsonify({"reply": reply, "state": new_state, "booked": False})

    # ── Schedule trigger ──────────────────────────────────────────────────────
    _no_date_yet     = not new_state.get("date")
    _schedule_cached = "full_schedule" in avail
    # Read price_shown from state (persists via extraction merge) OR session fallback
    _price_shown     = bool(new_state.get("price_shown")) or sess.get("price_shown", False)
    _needs_schedule  = booking_intent and _price_shown and _no_date_yet and not _schedule_cached

    print(
        f"[schedule-trigger] service={new_state.get('service')!r} "
        f"booking_intent={booking_intent} price_shown={_price_shown} "
        f"(state={new_state.get('price_shown')} sess={sess.get('price_shown', False)}) "
        f"date={new_state.get('date')!r} cached={_schedule_cached} → needs_schedule={_needs_schedule}",
        flush=True,
    )

    if _needs_schedule:
        schedule = _build_schedule_for_days()
        if schedule:
            avail["full_schedule"] = schedule
            reply = f"{schedule}\n\nКакое время вам удобно? 🤍"
        else:
            reply = "Расписание сейчас недоступно. Назовите удобную дату — я проверю наличие слотов 🤍"
        history.append({"role": "assistant", "content": reply})
        sess["state"] = new_state
        return jsonify({"reply": reply, "state": new_state, "booked": False})

    # ── Fetch slots for a specific date when client names one ─────────────────
    curr_date = new_state.get("date")
    curr_time = new_state.get("time")
    if curr_date and curr_date != avail.get("cached_date"):
        date_iso = _parse_date_to_iso(curr_date)
        if date_iso:
            slots = _fetch_slots_for_date(date_iso)
            avail.update({
                "cached_date": curr_date,
                "date_label": curr_date,
                "slots": slots,
                "requested_time": curr_time,
            })
    elif curr_time and curr_time != avail.get("requested_time"):
        avail["requested_time"] = curr_time

    # ── Normal single-reply flow ──────────────────────────────────────────────
    try:
        response = client_ai.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=TEST_CHAT_SYSTEM_PROMPT + _state_context(new_state) + _avail_context(avail),
            messages=history,
        )
        reply = _process_action_markers(response.content[0].text.strip())
        reply = _add_price_disclaimer(reply, new_state.get("breed"))
    except Exception as e:
        print(f"[test-chat] claude error: {e}", flush=True)
        reply = "Произошла ошибка. Пожалуйста, попробуйте ещё раз 🤍"
    history.append({"role": "assistant", "content": reply})

    sess["state"] = new_state

    # ── State logging ─────────────────────────────────────────────────────────
    _required = ("breed", "service", "date", "time", "ownerName", "petName")
    _missing = [k for k in _required if not new_state.get(k)]
    if "date" not in _missing and new_state.get("date"):
        if _parse_date_to_iso(new_state["date"]) is None:
            _missing.append("date")
            print(f"[test-chat] date {new_state['date']!r} not parseable", flush=True)
    print(
        f"[test-chat] state: confirmed={new_state.get('confirmed')} missing={_missing} | "
        f"breed={new_state.get('breed')!r} service={new_state.get('service')!r} "
        f"date={new_state.get('date')!r} time={new_state.get('time')!r} "
        f"owner={new_state.get('ownerName')!r} pet={new_state.get('petName')!r}",
        flush=True,
    )

    # ── Booking ───────────────────────────────────────────────────────────────
    booked = new_state.get("confirmed") and not _missing
    if booked:
        booking_date = _to_booking_date(new_state["date"])
        price = _extract_price_from_history(history)
        payload = {
            "breed":        new_state["breed"],
            "breedDisplay": " ".join(filter(None, [new_state["breed"], new_state.get("weight")])),
            "service":      new_state["service"],
            "price":        price,
            "date":         booking_date,
            "time":         new_state["time"],
            "name":         new_state["ownerName"],
            "pet":          new_state["petName"] or "",
            "master":       new_state.get("master") or "",
            "phone":        new_state.get("clientPhone") or "test-chat",
            "lang":         "ru",
            "source":       "test-chat",
        }
        # Validate all required fields before sending
        _required_booking = {"breed": payload["breed"], "service": payload["service"],
                             "date": payload["date"], "time": payload["time"],
                             "name": payload["name"]}
        _empty = [k for k, v in _required_booking.items() if not v]
        if _empty:
            print(f"[test-chat] ⚠️ booking missing required fields: {_empty}", flush=True)
        if not price:
            print("[test-chat] ⚠️ price=0 — Anna may not have mentioned price in conversation", flush=True)
        print(f"[test-chat] ✅ booking payload: {payload}", flush=True)
        if GOOGLE_SCRIPT:
            try:
                gs_resp = requests.get(GOOGLE_SCRIPT, params=payload, timeout=15)
                print(f"[test-chat] GS → {gs_resp.status_code}: {gs_resp.text[:300]}", flush=True)
            except Exception as e:
                print(f"[test-chat] GS call failed: {e}", flush=True)
        else:
            print("[test-chat] ⚠️ GOOGLE_SCRIPT not set", flush=True)
        del _chat_sessions[sid]
        session.pop("chat_sid", None)

    return jsonify({"reply": reply, "state": new_state, "booked": bool(booked)})

@app.route("/test-chat/reset", methods=["POST"])
def test_chat_reset():
    sid = session.pop("chat_sid", None)
    if sid:
        _chat_sessions.pop(sid, None)
    return jsonify({"ok": True})

@app.route("/api/callback", methods=["POST"])
def api_callback():
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    phone = data.get("phone", "").strip()
    msg = f"Заявка: {name}, тел {phone}"
    twilio_sid   = os.environ.get("TWILIO_ACCOUNT_SID")
    twilio_token = os.environ.get("TWILIO_AUTH_TOKEN")
    twilio_from  = os.environ.get("TWILIO_ADMIN_PHONE", "+37266922128")
    admin_to     = "+37258243141"
    print(f"[callback] name={name!r} phone={phone!r} sid_set={bool(twilio_sid)} from={twilio_from}", flush=True)

    # Telegram (не зависит от статуса email-домена)
    try:
        _send_reminder_telegram(
            f"📞 <b>Заявка на обратный звонок</b>\n\n"
            f"👤 Имя: {name or '—'}\n"
            f"📱 Телефон: {phone or '—'}\n\n"
            f"Клиент не нашёл удобное время в виджете бронирования."
        )
    except Exception as e:
        print(f"[callback] Telegram error: {e}", flush=True)

    # SMS через Twilio
    if twilio_sid and twilio_token:
        try:
            sms_url = f"https://api.twilio.com/2010-04-01/Accounts/{twilio_sid}/Messages.json"
            resp = requests.post(
                sms_url,
                auth=(twilio_sid, twilio_token),
                data={"From": twilio_from, "To": admin_to, "Body": msg},
                timeout=10
            )
            print(f"[callback] Twilio SMS response: {resp.status_code} {resp.text[:200]}", flush=True)
        except Exception as e:
            print(f"[callback] Twilio error: {e}", flush=True)
    else:
        print("[callback] Twilio creds not set — skipping", flush=True)

    # Email через Resend
    resend_key = os.environ.get("RESEND_API_KEY")
    if resend_key:
        try:
            email_resp = requests.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
                json={
                    "from": "booking@rjgrooming.salon",
                    "to": ["myrnj1@gmail.com"],
                    "subject": "Новая заявка на обратный звонок",
                    "html": f"<h2>Заявка с сайта</h2><p><b>Имя:</b> {name}</p><p><b>Телефон:</b> {phone}</p>"
                },
                timeout=10
            )
            print(f"[callback] Resend response: {email_resp.status_code}", flush=True)
        except Exception as e:
            print(f"[callback] Resend error: {e}", flush=True)

    return jsonify({"ok": True}), 200

def _build_client_export_workbook():
    """Собирает Excel-книгу (2 листа: Клиенты, Напоминания) и возвращает
    (bytes, filename). Используется и веб-роутом, и еженедельной рассылкой."""
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill
    from io import BytesIO

    today = datetime.now(_REMINDER_TZ).date() if _REMINDER_TZ else datetime.utcnow().date()
    bookings = _fetch_full_history_bookings()

    def parse_date(s):
        try:
            return datetime.strptime(s, "%d.%m.%Y")
        except Exception:
            return datetime.max

    bookings_sorted = sorted(bookings, key=lambda b: parse_date(b.get("date", "")))

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Клиенты"

    headers = ["Дата", "Время", "Клиент", "Телефон", "Email", "Питомец", "Порода", "Услуга", "Мастер", "Цена (EUR)"]
    ws.append(headers)
    header_font = Font(name="Arial", bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="333333", end_color="333333", fill_type="solid")
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    for b in bookings_sorted:
        ws.append([
            b.get("date", ""),
            b.get("time", ""),
            b.get("clientName", ""),
            b.get("clientPhone", ""),
            b.get("clientEmail", ""),
            b.get("petName", ""),
            b.get("breed", ""),
            b.get("service", ""),
            b.get("master", ""),
            b.get("price", "")
        ])

    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = Font(name="Arial")

    for col in ws.columns:
        max_len = max((len(str(c.value)) for c in col if c.value is not None), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 3, 40)

    ws.freeze_panes = "A2"

    # ── Лист 2: Напоминания (последний визит на клиента, начиная с 20 мая) ──
    cutoff = datetime(2026, 5, 20)
    by_phone = {}
    for b in bookings:
        phone = (b.get("clientPhone") or "").strip()
        if not phone:
            continue
        d = parse_date(b.get("date", ""))
        if d == datetime.max or d < cutoff:
            continue
        if phone not in by_phone or d > by_phone[phone]["_d"]:
            by_phone[phone] = {
                "_d": d,
                "date": b.get("date", ""),
                "name": b.get("clientName", ""),
                "pet": b.get("petName", ""),
                "breed": b.get("breed", ""),
                "master": b.get("master", ""),
                "email": b.get("clientEmail", ""),
            }

    ws2 = wb.create_sheet("Напоминания")
    headers2 = ["Клиент", "Телефон", "Email", "Питомец", "Порода", "Последний визит", "Мастер", "Напоминание #1 (+35 дн.)", "Напоминание #2 (+42 дн.)"]
    ws2.append(headers2)
    for col_idx in range(1, len(headers2) + 1):
        cell = ws2.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    rows2 = sorted(by_phone.items(), key=lambda kv: kv[1]["_d"])
    for phone, info in rows2:
        r1 = info["_d"] + timedelta(days=35)
        r2 = info["_d"] + timedelta(days=42)
        ws2.append([
            info["name"], phone, info["email"], info["pet"], info["breed"],
            info["date"], info["master"],
            r1.strftime("%d.%m.%Y"), r2.strftime("%d.%m.%Y")
        ])

    for row in ws2.iter_rows(min_row=2):
        for cell in row:
            cell.font = Font(name="Arial")
    for col in ws2.columns:
        max_len = max((len(str(c.value)) for c in col if c.value is not None), default=10)
        ws2.column_dimensions[col[0].column_letter].width = min(max_len + 3, 40)
    ws2.freeze_panes = "A2"

    # ── Лист 3: Дубли (по формату номера и по совпадению имени) ──
    by_raw_phone = {}
    for b in bookings:
        raw_phone = (b.get("clientPhone") or "").strip()
        if not raw_phone:
            continue
        norm = _normalize_phone(raw_phone)
        entry = by_raw_phone.setdefault(raw_phone, {"norm": norm, "names": set(), "pets": set(), "count": 0})
        if b.get("clientName"):
            entry["names"].add(b.get("clientName"))
        if b.get("petName"):
            entry["pets"].add(b.get("petName"))
        entry["count"] += 1

    by_norm = {}
    for raw_phone, info in by_raw_phone.items():
        by_norm.setdefault(info["norm"], []).append({"raw": raw_phone, **info})

    by_name = {}
    for raw_phone, info in by_raw_phone.items():
        for nm in info["names"]:
            nk = nm.strip().lower()
            if nk:
                by_name.setdefault(nk, set()).add(info["norm"])

    ws3 = wb.create_sheet("Дубли")
    ws3.append(["Тип дубля", "Телефон / Имя", "Детали"])
    for col_idx in range(1, 4):
        cell = ws3.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    for norm, variants in by_norm.items():
        if len(variants) > 1:
            for v in variants:
                ws3.append([
                    "Один номер, разный формат",
                    norm,
                    f"как записано: {v['raw']} | имена: {', '.join(sorted(v['names']))} | питомцы: {', '.join(sorted(v['pets']))} | броней: {v['count']}"
                ])

    for name_key, phones in by_name.items():
        if len(phones) > 1:
            ws3.append([
                "Одно имя, разные номера",
                name_key,
                f"телефоны: {', '.join(sorted(phones))}"
            ])

    for row in ws3.iter_rows(min_row=2):
        for cell in row:
            cell.font = Font(name="Arial")
    for col in ws3.columns:
        max_len = max((len(str(c.value)) for c in col if c.value is not None), default=10)
        ws3.column_dimensions[col[0].column_letter].width = min(max_len + 3, 60)
    ws3.freeze_panes = "A2"

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"RJ_Grooming_clients_{datetime.now().strftime('%Y%m%d')}.xlsx"
    return buf.getvalue(), filename

@app.route("/admin/client")
def admin_client_detail():
    import urllib.parse as _urlp

    phone = (request.args.get("phone") or "").strip()
    if not phone:
        return "Не указан телефон клиента.", 400

    error = None
    name, email, instagram = "", "", ""
    visits = []
    pets = {}
    total = 0.0

    try:
        today = datetime.now(_REMINDER_TZ).date() if _REMINDER_TZ else datetime.utcnow().date()
        bookings = _fetch_full_history_bookings()

        def parse_date(s):
            try:
                return datetime.strptime(s, "%d.%m.%Y").date()
            except Exception:
                return None

        for b in bookings:
            if (b.get("clientPhone") or "").strip() != phone:
                continue
            if b.get("clientName"):
                name = b.get("clientName")
            if b.get("clientEmail"):
                email = b.get("clientEmail")
            if b.get("clientInstagram"):
                instagram = b.get("clientInstagram")
            pet = b.get("petName", "")
            breed = b.get("breed", "")
            if pet:
                if breed or pet not in pets:
                    pets[pet] = breed

            try:
                price = float(b.get("price") or 0)
            except Exception:
                price = 0
            total += price
            visits.append({
                "date": parse_date(b.get("date", "")),
                "date_str": b.get("date", ""),
                "service": b.get("service", ""),
                "master": b.get("master", ""),
                "price": price,
                "pet": pet,
                "breed": breed
            })
        visits.sort(key=lambda v: v["date"] or datetime.min.date(), reverse=True)
    except Exception as e:
        error = str(e)

    saved_data = _load_client_data(phone)
    if saved_data.get("email"):
        email = saved_data["email"]
    if saved_data.get("instagram"):
        instagram = saved_data["instagram"]
    if saved_data.get("name"):
        name = saved_data["name"]
    comment = saved_data.get("comment", "")
    display_phone = _normalize_phone(saved_data.get("phone_override") or phone)

    wa_digits = re.sub(r"[^\d]", "", display_phone)
    pets_str = ", ".join(f"{p} ({b})" if b else p for p, b in pets.items()) or "—"
    last_visit_str = visits[0]["date_str"] if visits else "—"
    first_visit_str = visits[-1]["date_str"] if visits else "—"

    email_html = f'<a class="contact-val link" href="mailto:{email}">{email}</a>' if email else '<span class="contact-val empty">не указан</span>'
    ig_html = f'<a class="contact-val link" href="https://instagram.com/{instagram.lstrip("@")}" target="_blank" rel="noopener">@{instagram.lstrip("@")}</a>' if instagram else '<span class="contact-val empty">не указан</span>'

    def pet_photo_card(pet_name, breed):
        key = _pet_photo_key(phone, pet_name)
        photo_url = _pet_photo_url(key)
        return f"""
        <div class="pet-photo-card">
          <div class="pet-photo-name">{pet_name}{f' · {breed}' if breed else ''}</div>
          <img src="{photo_url}" class="pet-photo-img" id="img-{key}"
               onerror="this.outerHTML='<div class=&quot;pet-photo-placeholder&quot; id=&quot;img-{key}&quot;>Нет фото анкеты</div>'">
          <label class="pet-photo-btn">
            📷 Загрузить / обновить фото анкеты
            <input type="file" accept="image/*" capture="environment" style="display:none" onchange="uploadPetPhoto(this, '{key}', '{_urlp.quote(phone)}', '{_urlp.quote(pet_name)}')">
          </label>
        </div>"""

    pet_photos_html = "".join(pet_photo_card(p, b) for p, b in pets.items()) if pets else '<div class="empty">Питомцы не найдены</div>'

    all_memberships = _load_memberships()
    client_memberships = [m for m in all_memberships.values() if (m.get("client_phone") or "").strip() == phone]
    client_memberships.sort(key=lambda m: m.get("id", ""), reverse=True)

    def membership_row_html(m):
        pct = round((m["used_visits"] / m["total_visits"]) * 100) if m["total_visits"] else 0
        status_label = "Завершён" if m["status"] == "completed" else "Активен"
        status_color = "#8a8578" if m["status"] == "completed" else "#4ade80"
        return f"""
        <a class="membership-row" href="/membership/{m['id']}" target="_blank">
          <div class="membership-row-top">
            <span class="membership-row-id">{m['id']}</span>
            <span class="membership-row-status" style="color:{status_color};border-color:{status_color}55">{status_label}</span>
          </div>
          <div class="membership-row-meta">{m.get('plan_name','')} · {m.get('pet_name','')} · до {m.get('expiry_date','—')}</div>
          <div class="membership-row-progress-bar"><div class="membership-row-progress-fill" style="width:{pct}%"></div></div>
          <div class="membership-row-count">{m['used_visits']}/{m['total_visits']} посещений</div>
        </a>"""

    memberships_html = "".join(membership_row_html(m) for m in client_memberships) if client_memberships else '<div class="empty">Абонементов нет</div>'
    first_pet_name = next(iter(pets.keys()), "")
    first_pet_type = pets.get(first_pet_name, "") if first_pet_name else ""
    new_membership_link = (
        f"/admin/memberships?client_name={_urlp.quote(name)}&client_phone={_urlp.quote(phone)}"
        f"&pet_name={_urlp.quote(first_pet_name)}&pet_type={_urlp.quote(first_pet_type)}"
        f"&client_email={_urlp.quote(email)}"
    )

    def visit_row(v):
        return f"""
        <div class="vrow">
          <div class="vrow-date">{v['date_str']}</div>
          <div class="vrow-mid">
            <span class="vrow-service">{v['service'] or '—'}</span>
            <span class="vrow-sub">{v['pet'] or '—'} · {v['master'] or '—'}</span>
          </div>
          <div class="vrow-price">{v['price']:.0f}€</div>
        </div>"""

    visits_html = "".join(visit_row(v) for v in visits) if visits else '<div class="empty">Визитов не найдено</div>'
    error_html = f'<div class="empty" style="color:#e0824a">Ошибка загрузки данных: {error}</div>' if error else ""

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{name or 'Клиент'} — R&J Grooming</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,500;0,600;0,700;1,500&family=Montserrat:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0a0a09;color:#f2ede2;font-family:'Montserrat',sans-serif;padding:36px 20px 80px}}
  .wrap{{max-width:640px;margin:0 auto}}
  .back-link{{display:inline-block;font-size:0.74rem;color:rgba(201,160,90,.75);text-decoration:none;margin-bottom:20px}}
  .eyebrow{{font-size:0.68rem;letter-spacing:.3em;text-transform:uppercase;color:rgba(242,237,226,.4);margin-bottom:10px}}
  h1{{font-family:'Playfair Display',serif;font-weight:700;font-size:2.4rem;margin-bottom:4px}}
  .pets-sub{{font-size:0.85rem;color:rgba(242,237,226,.55);margin-bottom:28px}}

  .actions{{display:flex;gap:10px;margin-bottom:28px}}
  .abtn{{flex:1;text-align:center;padding:14px;border-radius:12px;text-decoration:none;font-size:0.85rem;font-weight:600}}
  .abtn.call{{background:rgba(201,160,90,.12);border:1px solid rgba(201,160,90,.4);color:#c9a05a}}
  .abtn.wa{{background:rgba(74,222,128,.1);border:1px solid rgba(74,222,128,.4);color:#4ade80}}

  .card{{background:#141310;border:1px solid rgba(255,255,255,.07);border-radius:14px;padding:20px;margin-bottom:16px}}
  .card-title{{font-size:0.66rem;letter-spacing:.15em;text-transform:uppercase;color:#c9a05a;margin-bottom:14px}}
  .card-title-row{{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}}
  .card-title-row .card-title{{margin-bottom:0}}
  .edit-btn{{background:none;border:1px solid rgba(201,160,90,.35);color:#c9a05a;font-size:0.68rem;padding:5px 12px;border-radius:20px;cursor:pointer;font-family:'Montserrat',sans-serif}}
  .edit-field{{margin-bottom:14px}}
  .edit-field label{{display:block;font-size:0.72rem;color:rgba(242,237,226,.5);margin-bottom:6px}}
  .edit-field input{{width:100%;background:#0e0d0b;border:1px solid rgba(201,160,90,.3);border-radius:8px;padding:10px 12px;color:#f2ede2;font-family:'Montserrat',sans-serif;font-size:0.88rem}}
  .edit-field input:focus{{outline:none;border-color:#c9a05a}}
  .edit-field textarea{{width:100%;background:#0e0d0b;border:1px solid rgba(201,160,90,.3);border-radius:8px;padding:10px 12px;color:#f2ede2;font-family:'Montserrat',sans-serif;font-size:0.88rem;min-height:80px;resize:vertical}}
  .edit-field textarea:focus{{outline:none;border-color:#c9a05a}}
  .edit-actions{{display:flex;gap:8px;margin-top:4px}}
  .edit-save{{flex:1;background:#c9a05a;color:#0a0a09;border:none;border-radius:8px;padding:11px;font-weight:600;font-size:0.85rem;cursor:pointer}}
  .edit-cancel{{flex:1;background:none;border:1px solid rgba(255,255,255,.15);color:rgba(242,237,226,.6);border-radius:8px;padding:11px;font-size:0.85rem;cursor:pointer}}
  .contact-line{{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid rgba(255,255,255,.05)}}
  .contact-line:last-child{{border-bottom:none}}
  .contact-label{{font-size:0.78rem;color:rgba(242,237,226,.5)}}
  .contact-val{{font-size:0.85rem;color:#f2ede2;text-decoration:none}}
  .contact-val.link{{color:#c9a05a}}
  .contact-val.empty{{color:rgba(242,237,226,.3);font-style:italic}}

  .stats-mini{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px}}
  .stat{{background:#141310;border:1px solid rgba(201,160,90,.18);border-radius:12px;padding:16px 10px;text-align:center}}
  .stat .n{{font-family:'Playfair Display',serif;font-size:1.5rem;font-weight:600;color:#c9a05a}}
  .stat .l{{font-size:0.6rem;letter-spacing:.06em;text-transform:uppercase;color:rgba(242,237,226,.5);margin-top:3px}}

  .list-label{{font-size:0.68rem;letter-spacing:.2em;text-transform:uppercase;color:#c9a05a;margin:24px 0 12px}}
  .vrow{{display:flex;align-items:center;gap:14px;background:#131210;border:1px solid rgba(255,255,255,.06);border-radius:10px;padding:12px 16px;margin-bottom:8px}}
  .vrow-date{{font-size:0.72rem;color:rgba(242,237,226,.45);width:78px;flex-shrink:0}}
  .vrow-mid{{flex:1;display:flex;flex-direction:column}}
  .vrow-service{{font-size:0.85rem;font-weight:500}}
  .vrow-sub{{font-size:0.72rem;color:rgba(242,237,226,.5);margin-top:2px}}
  .vrow-price{{font-size:0.85rem;font-weight:600;color:#c9a05a}}
  .empty{{text-align:center;padding:30px 0;color:rgba(242,237,226,.4);font-size:0.85rem}}

  .pet-photos-grid{{display:flex;flex-direction:column;gap:12px;margin-bottom:8px}}
  .pet-photo-card{{background:#141310;border:1px solid rgba(255,255,255,.07);border-radius:14px;padding:16px}}
  .pet-photo-name{{font-family:'Playfair Display',serif;font-size:1rem;font-weight:600;margin-bottom:10px}}
  .pet-photo-img{{width:100%;max-height:340px;object-fit:contain;border-radius:10px;background:#0a0a09;display:block;margin-bottom:10px}}
  .pet-photo-placeholder{{width:100%;height:80px;border:1px dashed rgba(242,237,226,.2);border-radius:10px;display:flex;align-items:center;justify-content:center;color:rgba(242,237,226,.35);font-size:0.78rem;margin-bottom:10px}}
  .pet-photo-btn{{display:block;text-align:center;background:rgba(201,160,90,.1);border:1px solid rgba(201,160,90,.4);color:#c9a05a;border-radius:10px;padding:11px;font-size:0.82rem;font-weight:600;cursor:pointer}}
  .membership-row{{display:block;background:#141310;border:1px solid rgba(255,255,255,.07);border-radius:12px;padding:14px 16px;margin-bottom:8px;text-decoration:none;color:inherit}}
  .membership-row-top{{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}}
  .membership-row-id{{font-family:'Playfair Display',serif;font-size:1rem;font-weight:600;color:#c9a05a}}
  .membership-row-status{{font-size:0.6rem;text-transform:uppercase;letter-spacing:.05em;padding:3px 9px;border-radius:20px;border:1px solid}}
  .membership-row-meta{{font-size:0.74rem;color:rgba(242,237,226,.55);margin-bottom:8px}}
  .membership-row-progress-bar{{height:5px;background:rgba(255,255,255,.08);border-radius:4px;overflow:hidden;margin-bottom:6px}}
  .membership-row-progress-fill{{height:100%;background:#c9a05a}}
  .membership-row-count{{font-size:0.72rem;color:rgba(242,237,226,.6)}}
  .new-membership-link{{display:block;text-align:center;background:rgba(201,160,90,.08);border:1px dashed rgba(201,160,90,.4);color:#c9a05a;border-radius:10px;padding:11px;font-size:0.8rem;text-decoration:none;margin-bottom:24px}}
  .upload-toast{{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(20px);background:#151310;border:1px solid rgba(201,160,90,.4);color:#f2ede2;padding:10px 20px;border-radius:20px;font-size:0.8rem;opacity:0;pointer-events:none;transition:all .25s;z-index:999}}
  .upload-toast.show{{opacity:1;transform:translateX(-50%) translateY(0)}}
</style>
</head>
<body>
<div class="wrap">
  <a href="/admin/clients?pass=anza1985" class="back-link">← Все клиенты</a>
  <div class="eyebrow">R&J Grooming · Клиент</div>
  <h1>{name or '—'}</h1>
  <div class="pets-sub">{pets_str}</div>

  <div class="actions">
    <a class="abtn call" href="tel:{display_phone}">📞 Позвонить</a>
    <a class="abtn wa" href="https://wa.me/{wa_digits}" target="_blank" rel="noopener">💬 WhatsApp</a>
  </div>

  <div class="stats-mini">
    <div class="stat"><div class="n">{len(visits)}</div><div class="l">визитов</div></div>
    <div class="stat"><div class="n">{total:.0f}€</div><div class="l">всего</div></div>
    <div class="stat"><div class="n" style="font-size:1.05rem">{last_visit_str}</div><div class="l">последний</div></div>
  </div>

  <div class="card">
    <div class="card-title-row">
      <div class="card-title">Контакты</div>
      <button class="edit-btn" onclick="toggleEditContacts()" id="editBtnLabel">✏️ Изменить</button>
    </div>
    <div id="contactsView">
      <div class="contact-line"><span class="contact-label">Имя</span><span class="contact-val">{name or '—'}</span></div>
      <div class="contact-line"><span class="contact-label">Телефон</span><a class="contact-val link" href="tel:{display_phone}">{display_phone}</a></div>
      <div class="contact-line"><span class="contact-label">Email</span>{email_html}</div>
      <div class="contact-line"><span class="contact-label">Instagram</span>{ig_html}</div>
      <div class="contact-line"><span class="contact-label">Первый визит</span><span class="contact-val">{first_visit_str}</span></div>
      <div class="contact-line" style="border-bottom:none;flex-direction:column;align-items:flex-start;gap:4px">
        <span class="contact-label">Комментарий</span>
        <span class="contact-val" style="white-space:pre-wrap">{comment or 'нет комментария'}</span>
      </div>
    </div>
    <div id="contactsEdit" style="display:none">
      <div class="edit-field">
        <label>Имя</label>
        <input type="text" id="editName" value="{name}" placeholder="Имя клиента">
      </div>
      <div class="edit-field">
        <label>Телефон</label>
        <input type="text" id="editPhone" value="{display_phone}" placeholder="+372...">
      </div>
      <div class="edit-field">
        <label>Email</label>
        <input type="email" id="editEmail" value="{email}" placeholder="client@example.com">
      </div>
      <div class="edit-field">
        <label>Instagram (без @)</label>
        <input type="text" id="editInstagram" value="{instagram}" placeholder="username">
      </div>
      <div class="edit-field">
        <label>Комментарий</label>
        <textarea id="editComment" placeholder="Заметки о клиенте...">{comment}</textarea>
      </div>
      <div class="edit-actions">
        <button class="edit-save" onclick="saveContacts('{_urlp.quote(phone)}')">Сохранить</button>
        <button class="edit-cancel" onclick="toggleEditContacts()">Отмена</button>
      </div>
    </div>
  </div>

  <div class="list-label">Абонементы</div>
  <div class="memberships-list">{memberships_html}</div>
  <a class="new-membership-link" href="{new_membership_link}">+ Новый абонемент</a>

  <div class="list-label">Анкеты питомцев</div>
  <div class="pet-photos-grid">{pet_photos_html}</div>

  <div class="list-label">История визитов ({len(visits)})</div>
  {error_html}
  {visits_html}
</div>
<div id="uploadToast" class="upload-toast"></div>
<script>
function toggleEditContacts(){{
  var view = document.getElementById('contactsView');
  var edit = document.getElementById('contactsEdit');
  var isEditing = edit.style.display !== 'none';
  view.style.display = isEditing ? '' : 'none';
  edit.style.display = isEditing ? 'none' : '';
}}
function saveContacts(phoneEncoded){{
  var name = document.getElementById('editName').value.trim();
  var phoneOverride = document.getElementById('editPhone').value.trim();
  var email = document.getElementById('editEmail').value.trim();
  var instagram = document.getElementById('editInstagram').value.trim();
  var comment = document.getElementById('editComment').value.trim();
  var toast = document.getElementById('uploadToast');
  toast.textContent = 'Сохраняю...';
  toast.classList.add('show');
  fetch('/api/save-client-data', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{
      phone: decodeURIComponent(phoneEncoded),
      name: name, phone_override: phoneOverride,
      email: email, instagram: instagram, comment: comment
    }})
  }})
  .then(function(r){{ return r.json(); }})
  .then(function(data){{
    if(data.success){{
      toast.textContent = 'Сохранено ✓';
      setTimeout(function(){{ location.reload(); }}, 600);
    }} else {{
      toast.textContent = 'Ошибка: ' + (data.error || 'не удалось сохранить');
      setTimeout(function(){{ toast.classList.remove('show'); }}, 2500);
    }}
  }})
  .catch(function(){{
    toast.textContent = 'Ошибка сохранения';
    setTimeout(function(){{ toast.classList.remove('show'); }}, 2500);
  }});
}}
function uploadPetPhoto(input, key, phone, pet){{
  var file = input.files[0];
  if(!file) return;
  var toast = document.getElementById('uploadToast');
  toast.textContent = 'Загружаю...';
  toast.classList.add('show');
  var fd = new FormData();
  fd.append('phone', decodeURIComponent(phone));
  fd.append('pet', decodeURIComponent(pet));
  fd.append('photo', file);
  fetch('/api/upload-pet-photo', {{method: 'POST', body: fd}})
    .then(function(r){{ return r.json(); }})
    .then(function(data){{
      if(data.success){{
        toast.textContent = 'Фото сохранено ✓';
        var el = document.getElementById('img-' + key);
        if(el){{
          var img = document.createElement('img');
          img.src = data.url + '?v=' + Date.now();
          img.className = 'pet-photo-img';
          img.id = 'img-' + key;
          el.replaceWith(img);
        }}
      }} else {{
        toast.textContent = 'Ошибка: ' + (data.error || 'не удалось загрузить');
      }}
      setTimeout(function(){{ toast.classList.remove('show'); }}, 2500);
    }})
    .catch(function(err){{
      toast.textContent = 'Ошибка загрузки';
      setTimeout(function(){{ toast.classList.remove('show'); }}, 2500);
    }});
}}
</script>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/api/find-duplicates")
def api_find_duplicates():
    try:
        today = datetime.now(_REMINDER_TZ).date() if _REMINDER_TZ else datetime.utcnow().date()
        bookings = _fetch_full_history_bookings()
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    by_raw_phone = {}
    for b in bookings:
        raw_phone = (b.get("clientPhone") or "").strip()
        if not raw_phone:
            continue
        norm = _normalize_phone(raw_phone)
        entry = by_raw_phone.setdefault(raw_phone, {"norm": norm, "names": set(), "pets": set(), "count": 0})
        if b.get("clientName"):
            entry["names"].add(b.get("clientName"))
        if b.get("petName"):
            entry["pets"].add(b.get("petName"))
        entry["count"] += 1

    # Группировка по нормализованному телефону — ловим разные форматы одного номера
    by_norm = {}
    for raw_phone, info in by_raw_phone.items():
        by_norm.setdefault(info["norm"], []).append({"raw": raw_phone, **info})

    phone_dupes = []
    for norm, variants in by_norm.items():
        if len(variants) > 1:
            phone_dupes.append({
                "normalized_phone": norm,
                "variants": [
                    {"raw_phone": v["raw"], "names": sorted(v["names"]), "pets": sorted(v["pets"]), "bookings_count": v["count"]}
                    for v in variants
                ]
            })

    # Совпадение имени при разных телефонах — возможный дубль под другим номером
    by_name = {}
    for raw_phone, info in by_raw_phone.items():
        for name in info["names"]:
            name_key = name.strip().lower()
            if not name_key:
                continue
            by_name.setdefault(name_key, set()).add(info["norm"])

    name_dupes = []
    for name_key, phones in by_name.items():
        if len(phones) > 1:
            name_dupes.append({"name": name_key, "phones": sorted(phones)})

    return jsonify({
        "success": True,
        "phone_format_duplicates": phone_dupes,
        "same_name_different_phone": name_dupes,
        "phone_dupes_count": len(phone_dupes),
        "name_dupes_count": len(name_dupes)
    })

@app.route("/admin/memberships")
def admin_memberships_page():
    import urllib.parse as _urlp
    prefill_name = request.args.get("client_name", "")
    prefill_phone = request.args.get("client_phone", "")
    prefill_pet = request.args.get("pet_name", "")
    prefill_pet_type = request.args.get("pet_type", "")
    prefill_email = request.args.get("client_email", "")
    memberships = _load_memberships()
    items = sorted(memberships.values(), key=lambda m: m.get("id", ""), reverse=True)

    def card_html(m):
        pct = round((m["used_visits"] / m["total_visits"]) * 100) if m["total_visits"] else 0
        status_label = "Завершён" if m["status"] == "completed" else "Активен"
        status_color = "#8a8578" if m["status"] == "completed" else "#4ade80"
        actions = ""
        if m["status"] != "completed" and m["used_visits"] < m["total_visits"]:
            actions += f'<button class="mem-btn mem-btn-mark" onclick="markVisit(\'{m["id"]}\')">Отметить визит</button>'
        if m["used_visits"] > 0:
            actions += f'<button class="mem-btn mem-btn-undo" onclick="undoVisit(\'{m["id"]}\')">Отменить отметку</button>'

        pricing_html = ""
        single_price = m.get("single_visit_price") or 0
        total_price = m.get("total_price") or 0
        total_visits = m.get("total_visits") or 0
        discount_pct = m.get("discount_percent") or 0
        if single_price and total_price and total_visits:
            per_visit = m.get("per_visit_price") or (total_price / total_visits)
            without_total = single_price * total_visits
            total_saving = without_total - total_price
            service_line = f'{m.get("service_type","")} · ' if m.get("service_type") else ""
            discount_line = f' · скидка {discount_pct:.0f}%' if discount_pct else ""
            pricing_html = f"""
          <div class="mem-pricing">
            <div class="mem-pricing-row">{service_line}разово {single_price:.0f}€ → по абонементу {per_visit:.1f}€/визит{discount_line}</div>
            <div class="mem-pricing-row">Без абонемента: {without_total:.0f}€ · С абонементом: {total_price:.0f}€</div>
            <div class="mem-pricing-row mem-pricing-highlight">Выгода клиента: {total_saving:.0f}€</div>
          </div>"""

        wa_digits = _normalize_phone(m.get("client_phone", "")).lstrip("+")
        wa_msg = _urlp.quote(f"Здравствуйте, {m.get('client_name','')}! Ваш абонемент {m['id']} ({m.get('plan_name','')}) готов: https://rjgrooming.up.railway.app/membership/{m['id']}")
        has_email = bool((m.get("client_email") or "").strip())

        return f"""
        <div class="mem-card" id="card-{m['id']}">
          <div class="mem-top">
            <div>
              <div class="mem-id">{m['id']}</div>
              <div class="mem-client">{m['client_name']} · {m['pet_name']}</div>
            </div>
            <div class="mem-status" style="color:{status_color};border-color:{status_color}55">{status_label}</div>
          </div>
          <div class="mem-progress-row">
            <div class="mem-progress-bar"><div class="mem-progress-fill" style="width:{pct}%"></div></div>
            <span class="mem-progress-text">{m['used_visits']}/{m['total_visits']}</span>
          </div>
          <div class="mem-meta">{m.get('plan_name','')} · до {m.get('expiry_date','—')}</div>
          {pricing_html}
          <div class="mem-actions">
            {actions}
            <a class="mem-btn mem-btn-link" href="/membership/{m['id']}" target="_blank">Открыть карточку</a>
            <a class="mem-btn mem-btn-wa" href="https://wa.me/{wa_digits}?text={wa_msg}" target="_blank" rel="noopener">WhatsApp</a>
            <button class="mem-btn mem-btn-mail" onclick="sendMembershipEmail('{m['id']}')" {'disabled title="У клиента нет email"' if not has_email else ''}>Отправить на почту</button>
            <button class="mem-btn mem-btn-delete" onclick="deleteMembership('{m['id']}')">Удалить</button>
          </div>
        </div>"""

    cards_html = "".join(card_html(m) for m in items) if items else '<div class="empty">Абонементов пока нет</div>'

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>R&J Grooming — Абонементы</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,500;0,600;0,700;1,500&family=Montserrat:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0a0a09;color:#f2ede2;font-family:'Montserrat',sans-serif;padding:36px 20px 80px}}
  .wrap{{max-width:640px;margin:0 auto}}
  .back-link{{display:inline-block;font-size:0.74rem;color:rgba(201,160,90,.75);text-decoration:none;margin-bottom:16px}}
  .eyebrow{{font-size:0.68rem;letter-spacing:.3em;text-transform:uppercase;color:rgba(242,237,226,.4);margin-bottom:10px}}
  h1{{font-family:'Playfair Display',serif;font-weight:600;font-size:2.1rem;margin-bottom:24px}}
  .form-card{{background:#141310;border:1px solid rgba(201,160,90,.25);border-radius:14px;padding:20px;margin-bottom:28px}}
  .form-title{{font-size:0.66rem;letter-spacing:.15em;text-transform:uppercase;color:#c9a05a;margin-bottom:14px}}
  .form-row{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}}
  .form-field{{margin-bottom:10px}}
  .form-field label{{display:block;font-size:0.72rem;color:rgba(242,237,226,.5);margin-bottom:5px}}
  .form-field input{{width:100%;background:#0e0d0b;border:1px solid rgba(201,160,90,.3);border-radius:8px;padding:10px 12px;color:#f2ede2;font-family:'Montserrat',sans-serif;font-size:0.85rem}}
  .form-field input:focus{{outline:none;border-color:#c9a05a}}
  .form-field select{{width:100%;background:#0e0d0b;border:1px solid rgba(201,160,90,.3);border-radius:8px;padding:10px 12px;color:#f2ede2;font-family:'Montserrat',sans-serif;font-size:0.85rem}}
  .form-field input[readonly]{{color:rgba(242,237,226,.5);cursor:not-allowed}}
  .breed-search-wrap{{position:relative}}
  .breed-drop{{display:none;position:absolute;top:100%;left:0;right:0;background:#141310;border:1px solid rgba(201,160,90,.35);border-radius:8px;margin-top:4px;max-height:220px;overflow-y:auto;z-index:20}}
  .breed-drop.open{{display:block}}
  .breed-drop-item{{padding:9px 12px;font-size:0.82rem;color:#f2ede2;cursor:pointer;border-bottom:1px solid rgba(255,255,255,.05)}}
  .breed-drop-item:last-child{{border-bottom:none}}
  .breed-drop-item:hover{{background:rgba(201,160,90,.1)}}
  .breed-drop-item mark{{background:none;color:#c9a05a;font-weight:600}}
  .breed-drop-empty{{padding:12px;font-size:0.78rem;color:rgba(242,237,226,.4);text-align:center}}
  .form-field select:focus{{outline:none;border-color:#c9a05a}}
  .savings-preview{{background:rgba(74,222,128,.06);border:1px solid rgba(74,222,128,.25);border-radius:10px;padding:12px 14px;margin-bottom:14px}}
  .savings-row{{display:flex;justify-content:space-between;font-size:0.78rem;color:rgba(242,237,226,.7);padding:4px 0}}
  .savings-row span:last-child{{font-weight:600;color:#4ade80}}
  .savings-total{{border-top:1px solid rgba(74,222,128,.2);margin-top:4px;padding-top:8px}}
  .savings-total span{{font-size:0.85rem}}
  .create-btn{{width:100%;background:#c9a05a;color:#0a0a09;border:none;border-radius:8px;padding:12px;font-weight:600;font-size:0.88rem;cursor:pointer;margin-top:6px}}
  .list-label{{font-size:0.68rem;letter-spacing:.2em;text-transform:uppercase;color:#c9a05a;margin-bottom:14px}}
  .mem-card{{background:#131210;border:1px solid rgba(255,255,255,.06);border-radius:12px;padding:16px 18px;margin-bottom:10px}}
  .mem-top{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px}}
  .mem-id{{font-family:'Playfair Display',serif;font-size:1.1rem;font-weight:600;color:#c9a05a}}
  .mem-client{{font-size:0.85rem;color:rgba(242,237,226,.75);margin-top:2px}}
  .mem-status{{font-size:0.62rem;text-transform:uppercase;letter-spacing:.05em;padding:4px 10px;border-radius:20px;border:1px solid;white-space:nowrap}}
  .mem-progress-row{{display:flex;align-items:center;gap:10px;margin-bottom:8px}}
  .mem-progress-bar{{flex:1;height:6px;background:rgba(255,255,255,.08);border-radius:4px;overflow:hidden}}
  .mem-progress-fill{{height:100%;background:#c9a05a}}
  .mem-progress-text{{font-size:0.75rem;color:rgba(242,237,226,.7);font-weight:600}}
  .mem-meta{{font-size:0.72rem;color:rgba(242,237,226,.5);margin-bottom:12px}}
  .mem-pricing{{background:rgba(74,222,128,.05);border:1px solid rgba(74,222,128,.2);border-radius:8px;padding:8px 12px;margin-bottom:12px}}
  .mem-pricing-row{{font-size:0.72rem;color:rgba(242,237,226,.6)}}
  .mem-pricing-highlight{{color:#4ade80;font-weight:600;margin-top:2px}}
  .mem-actions{{display:flex;gap:8px;flex-wrap:wrap}}
  .mem-btn{{font-size:0.72rem;padding:8px 12px;border-radius:8px;border:1px solid;cursor:pointer;text-decoration:none;font-family:'Montserrat',sans-serif}}
  .mem-btn-mark{{background:rgba(74,222,128,.1);border-color:rgba(74,222,128,.4);color:#4ade80}}
  .mem-btn-undo{{background:none;border-color:rgba(224,82,74,.4);color:#e0524a}}
  .mem-btn-link{{background:rgba(201,160,90,.1);border-color:rgba(201,160,90,.4);color:#c9a05a}}
  .mem-btn-wa{{background:rgba(74,222,128,.08);border-color:rgba(74,222,128,.3);color:#4ade80}}
  .mem-btn-mail{{background:rgba(120,170,230,.1);border-color:rgba(120,170,230,.35);color:#78aae6}}
  .mem-btn-mail:disabled{{opacity:.35;cursor:not-allowed}}
  .mem-btn-delete{{background:none;border-color:rgba(224,82,74,.25);color:rgba(224,82,74,.6)}}
  .empty{{text-align:center;padding:40px 0;color:rgba(242,237,226,.4);font-size:0.85rem}}
  .toast{{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(20px);background:#151310;border:1px solid rgba(201,160,90,.4);color:#f2ede2;padding:10px 20px;border-radius:20px;font-size:0.8rem;opacity:0;pointer-events:none;transition:all .25s;z-index:999}}
  .toast.show{{opacity:1;transform:translateX(-50%) translateY(0)}}
</style>
</head>
<body>
<div class="wrap">
  <a href="/admin" class="back-link">← Админ-панель</a>
  <div class="eyebrow">R&J Grooming · Абонементы</div>
  <h1>Абонементы</h1>

  <div class="form-card">
    <div class="form-title">Новый абонемент</div>
    <div class="form-row">
      <div class="form-field"><label>Имя клиента</label><input type="text" id="fClientName" value="{prefill_name}"></div>
      <div class="form-field"><label>Телефон</label><input type="text" id="fClientPhone" value="{prefill_phone}"></div>
    </div>
    <div class="form-row">
      <div class="form-field"><label>Кличка питомца</label><input type="text" id="fPetName" value="{prefill_pet}"></div>
      <div class="form-field"><label>Email</label><input type="email" id="fClientEmail" value="{prefill_email}" placeholder="client@example.com"></div>
    </div>
    <div class="form-row">
      <div class="form-field">
        <label>Порода</label>
        <div class="breed-search-wrap">
          <input type="text" id="fBreedSearch" placeholder="Начните вводить породу..." autocomplete="off" oninput="onBreedSearchInput()">
          <div class="breed-drop" id="breedDrop"></div>
        </div>
      </div>
      <div class="form-field">
        <label>Тип услуги</label>
        <select id="fServiceType" onchange="onServiceChange()">
          <option value="">— сначала выбери породу —</option>
        </select>
      </div>
    </div>
    <div class="form-row">
      <div class="form-field"><label>Разовая цена, €</label><input type="number" id="fSingleVisitPrice" min="0" step="0.5" placeholder="—" oninput="updateSavingsPreview()"></div>
      <div class="form-field"></div>
    </div>
    <div class="form-row">
      <div class="form-field"><label>Название абонемента</label><input type="text" id="fPlanName" placeholder="сформируется автоматически" readonly></div>
      <div class="form-field"><label>Кол-во посещений</label><input type="number" id="fTotalVisits" min="1" value="5" oninput="updatePlanName();updateSavingsPreview()"></div>
    </div>
    <div class="form-field">
      <label>Выгода в процентах от обычного посещения, %</label>
      <input type="number" id="fDiscountPercent" min="0" max="100" step="1" placeholder="20" oninput="updateSavingsPreview()">
    </div>
    <div class="form-row">
      <div class="form-field"><label>Дата покупки</label><input type="text" id="fPurchaseDate" placeholder="24.08.2026"></div>
      <div class="form-field"><label>Действителен до</label><input type="text" id="fExpiryDate" placeholder="24.02.2027"></div>
    </div>
    <div class="savings-preview" id="savingsPreview" style="display:none">
      <div class="savings-row"><span>Цена за визит по абонементу</span><span id="spPerVisit">—</span></div>
      <div class="savings-row"><span>Стоимость абонемента</span><span id="spTotalPrice">—</span></div>
      <div class="savings-row"><span>Без абонемента за визиты</span><span id="spWithoutTotal">—</span></div>
      <div class="savings-row savings-total"><span>Общая выгода клиента</span><span id="spTotalSaving">—</span></div>
    </div>
    <button class="create-btn" onclick="createMembership()">Создать абонемент</button>
  </div>

  <div class="list-label" id="memListLabel">Все абонементы ({len(items)})</div>
  {cards_html}
</div>
<div id="toast" class="toast"></div>
<script>
function showToast(msg){{
  var t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(function(){{ t.classList.remove('show'); }}, 2500);
}}

var breedData = [];
var selectedBreed = null;
var PREFILL_BREED = {json.dumps(prefill_pet_type)};

function loadBreeds(){{
  fetch('/api/breed-prices').then(function(r){{ return r.json(); }}).then(function(res){{
    if(res.success){{
      breedData = res.breeds;
      if(PREFILL_BREED){{
        var match = breedData.find(function(b){{ return b.breed === PREFILL_BREED; }});
        if(!match){{
          match = breedData.find(function(b){{ return b.breed.indexOf(PREFILL_BREED) === 0 || PREFILL_BREED.indexOf(b.breed) === 0; }});
        }}
        if(match){{ pickBreed(match); }}
        else {{ document.getElementById('fBreedSearch').value = PREFILL_BREED; }}
      }}
    }}
  }}).catch(function(){{}});
}}
loadBreeds();

function onBreedSearchInput(){{
  var q = document.getElementById('fBreedSearch').value.trim();
  var drop = document.getElementById('breedDrop');
  if(!q){{ drop.classList.remove('open'); drop.innerHTML=''; return; }}
  var qLower = q.toLowerCase();
  var res = breedData.filter(function(b){{ return b.breed.toLowerCase().indexOf(qLower) !== -1; }}).slice(0, 30);
  drop.innerHTML = '';
  if(!res.length){{
    drop.innerHTML = '<div class="breed-drop-empty">Порода не найдена</div>';
  }} else {{
    res.forEach(function(b){{
      var idx = b.breed.toLowerCase().indexOf(qLower);
      var item = document.createElement('div');
      item.className = 'breed-drop-item';
      item.innerHTML = b.breed.substring(0, idx) + '<mark>' + b.breed.substring(idx, idx + q.length) + '</mark>' + b.breed.substring(idx + q.length);
      item.onclick = function(){{ pickBreed(b); }};
      drop.appendChild(item);
    }});
  }}
  drop.classList.add('open');
}}

function pickBreed(b){{
  selectedBreed = b;
  document.getElementById('fBreedSearch').value = b.breed;
  document.getElementById('breedDrop').classList.remove('open');
  document.getElementById('breedDrop').innerHTML = '';

  var serviceSel = document.getElementById('fServiceType');
  serviceSel.innerHTML = '<option value="">— выбрать услугу —</option>' +
    Object.keys(b.services).map(function(s){{ return '<option value="' + s + '">' + s + ' — ' + b.services[s] + '€</option>'; }}).join('');
  document.getElementById('fSingleVisitPrice').value = '';
  updateSavingsPreview();
}}

document.addEventListener('click', function(e){{
  if(!e.target.closest('.breed-search-wrap')) document.getElementById('breedDrop').classList.remove('open');
}});

function onServiceChange(){{
  var serviceName = document.getElementById('fServiceType').value;
  if(selectedBreed && selectedBreed.services[serviceName] !== undefined){{
    document.getElementById('fSingleVisitPrice').value = selectedBreed.services[serviceName];
  }}
  updatePlanName();
  updateSavingsPreview();
}}

function updatePlanName(){{
  var visits = document.getElementById('fTotalVisits').value;
  var service = document.getElementById('fServiceType').value;
  document.getElementById('fPlanName').value = (visits && service) ? (visits + ' посещений — ' + service) : '';
}}

function createMembership(){{
  var data = {{
    client_name: document.getElementById('fClientName').value.trim(),
    client_phone: document.getElementById('fClientPhone').value.trim(),
    client_email: document.getElementById('fClientEmail').value.trim(),
    pet_name: document.getElementById('fPetName').value.trim(),
    pet_type: selectedBreed ? selectedBreed.breed : document.getElementById('fBreedSearch').value.trim(),
    plan_name: document.getElementById('fPlanName').value.trim(),
    total_visits: document.getElementById('fTotalVisits').value,
    purchase_date: document.getElementById('fPurchaseDate').value.trim(),
    expiry_date: document.getElementById('fExpiryDate').value.trim(),
    service_type: document.getElementById('fServiceType').value,
    single_visit_price: document.getElementById('fSingleVisitPrice').value,
    discount_percent: document.getElementById('fDiscountPercent').value
  }};
  if(!data.client_name || !data.pet_name || !data.total_visits){{
    showToast('Заполни имя клиента, кличку и число посещений');
    return;
  }}
  fetch('/api/membership/create', {{
    method: 'POST', headers: {{'Content-Type':'application/json'}}, body: JSON.stringify(data)
  }}).then(function(r){{ return r.json(); }}).then(function(res){{
    if(res.success){{
      showToast('Создан: ' + res.id);
      setTimeout(function(){{ location.reload(); }}, 700);
    }} else {{
      showToast('Ошибка: ' + (res.error||'не удалось создать'));
    }}
  }}).catch(function(){{ showToast('Ошибка сети'); }});
}}

function updateSavingsPreview(){{
  var totalVisits = parseFloat(document.getElementById('fTotalVisits').value) || 0;
  var singlePrice = parseFloat(document.getElementById('fSingleVisitPrice').value) || 0;
  var discountPct = parseFloat(document.getElementById('fDiscountPercent').value) || 0;
  var block = document.getElementById('savingsPreview');
  if(!totalVisits || !singlePrice){{
    block.style.display = 'none';
    return;
  }}
  var perVisit = singlePrice * (1 - discountPct / 100);
  var totalPrice = perVisit * totalVisits;
  var withoutTotal = singlePrice * totalVisits;
  var totalSaving = withoutTotal - totalPrice;
  document.getElementById('spPerVisit').textContent = perVisit.toFixed(2) + ' €';
  document.getElementById('spTotalPrice').textContent = totalPrice.toFixed(2) + ' €';
  document.getElementById('spWithoutTotal').textContent = totalVisits + ' × ' + singlePrice.toFixed(2) + ' € = ' + withoutTotal.toFixed(2) + ' €';
  document.getElementById('spTotalSaving').textContent = totalSaving.toFixed(2) + ' €';
  block.style.display = 'block';
}}

function markVisit(id){{
  fetch('/api/membership/mark-visit', {{
    method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{id:id}})
  }}).then(function(r){{return r.json();}}).then(function(res){{
    if(res.success){{ showToast('Визит отмечен ✓'); setTimeout(function(){{location.reload();}}, 600); }}
    else{{ showToast('Ошибка: ' + (res.error||'')); }}
  }}).catch(function(){{ showToast('Ошибка сети'); }});
}}

function undoVisit(id){{
  fetch('/api/membership/undo-visit', {{
    method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{id:id}})
  }}).then(function(r){{return r.json();}}).then(function(res){{
    if(res.success){{ showToast('Отметка отменена'); setTimeout(function(){{location.reload();}}, 600); }}
    else{{ showToast('Ошибка: ' + (res.error||'')); }}
  }}).catch(function(){{ showToast('Ошибка сети'); }});
}}

function sendMembershipEmail(id){{
  var btn = event && event.target;
  if(btn){{ btn.disabled = true; var oldText = btn.textContent; btn.textContent = 'Отправка...'; }}
  fetch('/api/membership/send-email', {{
    method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{id:id}})
  }}).then(function(r){{return r.json();}}).then(function(res){{
    if(res.success){{ showToast('Письмо отправлено'); }}
    else {{ showToast('Ошибка: ' + (res.error||'')); }}
  }}).catch(function(){{
    showToast('Ошибка сети');
  }}).finally(function(){{
    if(btn){{ btn.disabled = false; btn.textContent = oldText; }}
  }});
}}

function deleteMembership(id){{
  if(!confirm('Удалить абонемент ' + id + '? Это действие необратимо.')) return;
  var card = document.getElementById('card-' + id);
  if(card){{ card.style.opacity = '0.4'; card.style.pointerEvents = 'none'; }}
  fetch('/api/membership/delete', {{
    method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{id:id}})
  }}).then(function(r){{return r.json();}}).then(function(res){{
    if(res.success){{
      showToast('Удалён');
      if(card){{ card.remove(); }}
      var label = document.getElementById('memListLabel');
      if(label){{
        var n = document.querySelectorAll('.mem-card').length;
        label.textContent = 'Все абонементы (' + n + ')';
      }}
    }} else {{
      if(card){{ card.style.opacity = '1'; card.style.pointerEvents = 'auto'; }}
      showToast('Ошибка: ' + (res.error||''));
    }}
  }}).catch(function(){{
    if(card){{ card.style.opacity = '1'; card.style.pointerEvents = 'auto'; }}
    showToast('Ошибка сети');
  }});
}}
</script>
</body>
</html>"""
    return html, 200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache"
    }

@app.route("/admin/clients")
def admin_clients_page():
    import urllib.parse as _urlp

    error = None
    clients = []
    try:
        today = datetime.now(_REMINDER_TZ).date() if _REMINDER_TZ else datetime.utcnow().date()
        bookings = _fetch_full_history_bookings()

        def parse_date(s):
            try:
                return datetime.strptime(s, "%d.%m.%Y").date()
            except Exception:
                return None

        by_phone = {}
        for b in bookings:
            phone = (b.get("clientPhone") or "").strip()
            if not phone:
                continue
            d = parse_date(b.get("date", ""))
            entry = by_phone.setdefault(phone, {
                "name": b.get("clientName", ""), "phone": phone, "pets": {},
                "visits": 0, "total": 0.0, "last_date": None, "last_master": ""
            })
            if b.get("clientName"):
                entry["name"] = b.get("clientName")
            pet = b.get("petName", "")
            breed = b.get("breed", "")
            if pet:
                entry["pets"][pet] = breed
            entry["visits"] += 1
            try:
                entry["total"] += float(b.get("price") or 0)
            except Exception:
                pass
            if d and (not entry["last_date"] or d > entry["last_date"]):
                entry["last_date"] = d
                entry["last_master"] = b.get("master", "")

        all_client_data = _load_all_client_data()
        for phone, entry in by_phone.items():
            saved = all_client_data.get(_client_data_key(phone), {})
            if saved.get("name"):
                entry["name"] = saved["name"]
            entry["display_phone"] = _normalize_phone(saved.get("phone_override") or phone)

        clients = sorted(by_phone.values(), key=lambda c: c["last_date"] or datetime.min.date(), reverse=True)
    except Exception as e:
        error = str(e)

    def client_html(c):
        pets_str = ", ".join(f"{p} ({b})" if b else p for p, b in c["pets"].items()) or "—"
        last_str = c["last_date"].strftime("%d.%m.%Y") if c["last_date"] else "—"
        phone_encoded = _urlp.quote(c["phone"] or "")
        return f"""
        <a class="row" href="/admin/client?phone={phone_encoded}&pass=anza1985">
          <div class="row-top">
            <div class="who">
              <span class="name">{c['name'] or '—'}</span>
              <span class="pet">{pets_str}</span>
            </div>
            <div class="visits-badge">{c['visits']} {'визит' if c['visits']==1 else 'визита' if 2<=c['visits']<=4 else 'визитов'}</div>
          </div>
          <div class="row-bottom">
            <span>Последний визит: {last_str} · {c['last_master'] or '—'}</span>
            <span class="days">{c['total']:.0f}€</span>
          </div>
          <div class="row-bottom" style="margin-top:6px">
            <span class="contacts">{c.get('display_phone', c['phone'])}</span>
            <span class="row-arrow">→</span>
          </div>
        </a>"""

    rows_html = "".join(client_html(c) for c in clients) if clients else '<div class="empty">Клиентов не найдено</div>'
    error_html = f'<div class="empty" style="color:#e0824a">Ошибка загрузки данных: {error}</div>' if error else ""
    total_revenue = sum(c["total"] for c in clients)
    total_pets = sum(len(c["pets"]) for c in clients)
    returning_clients = sum(1 for c in clients if c["visits"] >= 2)

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>R&J Grooming — Клиенты</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,500;0,600;0,700;1,500&family=Montserrat:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0a0a09;color:#f2ede2;font-family:'Montserrat',sans-serif;padding:36px 20px 80px}}
  .wrap{{max-width:720px;margin:0 auto}}
  .eyebrow{{font-size:0.68rem;letter-spacing:.3em;text-transform:uppercase;color:rgba(242,237,226,.4);margin-bottom:10px}}
  .back-link{{display:inline-block;font-size:0.74rem;color:rgba(201,160,90,.75);text-decoration:none;margin-bottom:16px}}
  h1{{font-family:'Playfair Display',serif;font-weight:600;font-size:2.1rem;margin-bottom:6px}}
  .sub{{font-size:0.78rem;color:rgba(242,237,226,.5);margin-bottom:20px}}
  .quick-links{{display:flex;gap:10px;margin-bottom:24px}}
  .search-link{{flex:1;display:block;text-align:center;background:rgba(201,160,90,.1);border:1px solid rgba(201,160,90,.4);color:#c9a05a;border-radius:10px;padding:12px;font-size:0.85rem;font-weight:600;text-decoration:none}}
  .stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:32px}}
  .stat{{background:#151310;border:1px solid rgba(201,160,90,.18);border-radius:12px;padding:18px 14px}}
  .stat .n{{font-family:'Playfair Display',serif;font-size:1.9rem;font-weight:600;color:#c9a05a}}
  .stat .l{{font-size:0.66rem;letter-spacing:.08em;text-transform:uppercase;color:rgba(242,237,226,.5);margin-top:4px}}
  .list-label{{font-size:0.68rem;letter-spacing:.2em;text-transform:uppercase;color:#c9a05a;margin-bottom:14px}}
  .row{{display:block;background:#131210;border:1px solid rgba(255,255,255,.06);border-radius:12px;padding:16px 18px;margin-bottom:10px;text-decoration:none;color:inherit;transition:border-color .15s}}
  .row:active{{border-color:rgba(201,160,90,.4)}}
  .row-top{{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:10px}}
  .who{{display:flex;flex-direction:column}}
  .name{{font-family:'Playfair Display',serif;font-size:1.15rem;font-weight:600}}
  .pet{{font-size:0.76rem;color:rgba(242,237,226,.5);margin-top:2px}}
  .visits-badge{{font-size:0.62rem;letter-spacing:.04em;color:#c9a05a;border:1px solid rgba(201,160,90,.35);background:rgba(201,160,90,.08);padding:5px 10px;border-radius:20px;white-space:nowrap;flex-shrink:0}}
  .row-bottom{{display:flex;justify-content:space-between;align-items:center;font-size:0.74rem;color:rgba(242,237,226,.55)}}
  .row-bottom .days{{font-weight:600;color:#f2ede2}}
  .contacts{{color:rgba(242,237,226,.6)}}
  .row-arrow{{color:rgba(201,160,90,.6)}}
  .wa{{color:#4ade80;text-decoration:none;font-size:0.72rem;font-weight:600;border:1px solid rgba(74,222,128,.35);border-radius:20px;padding:3px 10px}}
  .empty{{text-align:center;padding:40px 0;color:rgba(242,237,226,.4);font-size:0.85rem}}
</style>
</head>
<body>
<div class="wrap">
  <a href="/admin?pass=anza1985" class="back-link">← Админ-панель</a>
  <div class="eyebrow">R&J Grooming · Клиенты</div>
  <h1>Все клиенты</h1>
  <div class="sub">Полная база — вся история бронирований</div>

  <div class="quick-links">
    <a href="/admin/search?pass=anza1985" class="search-link">🔍 Поиск клиента</a>
    <a href="/admin/export-clients?pass=anza1985" class="search-link">⬇️ Excel-выгрузка</a>
  </div>

  <div class="stats" style="grid-template-columns:repeat(2,1fr)">
    <div class="stat"><div class="n">{len(clients)}</div><div class="l">клиентов</div></div>
    <div class="stat"><div class="n">{total_pets}</div><div class="l">питомцев</div></div>
    <div class="stat"><div class="n">{returning_clients}</div><div class="l">повторных (2+ визита)</div></div>
    <div class="stat"><div class="n">{total_revenue:.0f}€</div><div class="l">выручка всего</div></div>
  </div>

  <div class="list-label">Список ({len(clients)})</div>
  {error_html}
  {rows_html}
</div>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/admin/dns-records")
def admin_dns_records():
    resend_key = request.args.get("key") or os.environ.get("RESEND_API_KEY")
    if not resend_key:
        return "RESEND_API_KEY не настроен на Railway.", 500

    try:
        r = requests.get(
            "https://api.resend.com/domains",
            headers={"Authorization": f"Bearer {resend_key}"},
            timeout=15
        )
        domains = r.json().get("data", [])
        domain = next((d for d in domains if "rjgrooming" in d.get("name", "")), None)
        if not domain:
            return f"Домен не найден. Ответ API: {r.text}", 404

        r2 = requests.get(
            f"https://api.resend.com/domains/{domain['id']}",
            headers={"Authorization": f"Bearer {resend_key}"},
            timeout=15
        )
        detail = r2.json()
    except Exception as e:
        return f"Ошибка запроса к Resend API: {e}", 500

    records = detail.get("records", [])
    rows_html = ""
    for rec in records:
        rows_html += f"""
        <div class="card">
          <div class="row"><b>Type:</b> {rec.get('type','')}</div>
          <div class="row"><b>Name:</b> <span class="copyable">{rec.get('name','')}</span></div>
          <div class="row"><b>Value:</b></div>
          <textarea readonly onclick="this.select()">{rec.get('value','')}</textarea>
          <div class="row"><b>Priority:</b> {rec.get('priority','—')}</div>
          <div class="row"><b>Status:</b> {rec.get('status','')}</div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DNS Records — {domain.get('name','')}</title>
<style>
  body{{background:#0a0a09;color:#f2ede2;font-family:-apple-system,sans-serif;padding:20px;margin:0}}
  h1{{font-size:1.3rem}}
  .status{{color:#e0824a;font-weight:600;margin-bottom:20px}}
  .card{{background:#151310;border:1px solid rgba(201,160,90,.25);border-radius:12px;padding:16px;margin-bottom:14px}}
  .row{{font-size:0.85rem;margin-bottom:6px;color:rgba(242,237,226,.8)}}
  textarea{{width:100%;background:#0a0a09;color:#c9a05a;border:1px solid rgba(201,160,90,.3);border-radius:8px;padding:10px;font-family:monospace;font-size:0.78rem;min-height:70px;margin-bottom:8px;word-break:break-all}}
</style>
</head>
<body>
  <h1>{domain.get('name','')}</h1>
  <div class="status">Статус: {domain.get('status','')}</div>
  {rows_html}
  <p style="font-size:0.75rem;color:rgba(242,237,226,.5)">Нажми на поле Value, выбери всё (Select All) и скопируй — там полное значение без обрезки.</p>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/api/membership/create", methods=["POST"])
def api_membership_create():
    body = request.get_json(force=True) or {}
    client_name = (body.get("client_name") or "").strip()
    client_phone = (body.get("client_phone") or "").strip()
    client_email = (body.get("client_email") or "").strip()
    pet_name = (body.get("pet_name") or "").strip()
    pet_type = (body.get("pet_type") or "").strip()
    plan_name = (body.get("plan_name") or "").strip()
    total_visits = body.get("total_visits")
    purchase_date = (body.get("purchase_date") or "").strip()
    expiry_date = (body.get("expiry_date") or "").strip()
    service_type = (body.get("service_type") or "").strip()
    single_visit_price = body.get("single_visit_price")
    discount_percent = body.get("discount_percent")

    if not client_name or not pet_name or not total_visits:
        return jsonify({"success": False, "error": "client_name, pet_name и total_visits обязательны"}), 400
    try:
        total_visits = int(total_visits)
    except Exception:
        return jsonify({"success": False, "error": "total_visits должно быть числом"}), 400

    try:
        single_visit_price = float(single_visit_price) if single_visit_price not in (None, "") else 0.0
    except Exception:
        single_visit_price = 0.0
    try:
        discount_percent = float(discount_percent) if discount_percent not in (None, "") else 0.0
    except Exception:
        discount_percent = 0.0
    discount_percent = max(0.0, min(100.0, discount_percent))

    per_visit_price = round(single_visit_price * (1 - discount_percent / 100), 2)
    total_price = round(per_visit_price * total_visits, 2)

    memberships = _load_memberships()
    mid = _next_membership_id(memberships)
    memberships[mid] = {
        "id": mid,
        "client_name": client_name,
        "client_phone": client_phone,
        "client_email": client_email,
        "pet_name": pet_name,
        "pet_type": pet_type,
        "plan_name": plan_name or f"{total_visits} посещений",
        "service_type": service_type,
        "total_visits": total_visits,
        "used_visits": 0,
        "purchase_date": purchase_date,
        "expiry_date": expiry_date,
        "single_visit_price": single_visit_price,
        "discount_percent": discount_percent,
        "per_visit_price": per_visit_price,
        "total_price": total_price,
        "visit_history": [],
        "status": "active",
        "created_at": datetime.now(_REMINDER_TZ).isoformat() if _REMINDER_TZ else datetime.utcnow().isoformat()
    }
    ok = _save_memberships(memberships)
    return jsonify({"success": ok, "id": mid})

@app.route("/api/membership/mark-visit", methods=["POST"])
def api_membership_mark_visit():
    body = request.get_json(force=True) or {}
    mid = (body.get("id") or "").strip()
    note = (body.get("note") or "уход").strip()
    memberships = _load_memberships()
    m = memberships.get(mid)
    if not m:
        return jsonify({"success": False, "error": "Абонемент не найден"}), 404
    if m["used_visits"] >= m["total_visits"]:
        return jsonify({"success": False, "error": "Все посещения уже использованы"}), 400

    today = datetime.now(_REMINDER_TZ).date() if _REMINDER_TZ else datetime.utcnow().date()
    m["used_visits"] += 1
    m["visit_history"].append({"date": today.strftime("%d.%m.%Y"), "note": note})
    if m["used_visits"] >= m["total_visits"]:
        m["status"] = "completed"
    ok = _save_memberships(memberships)
    return jsonify({"success": ok, "used_visits": m["used_visits"], "status": m["status"]})

@app.route("/api/membership/undo-visit", methods=["POST"])
def api_membership_undo_visit():
    body = request.get_json(force=True) or {}
    mid = (body.get("id") or "").strip()
    memberships = _load_memberships()
    m = memberships.get(mid)
    if not m:
        return jsonify({"success": False, "error": "Абонемент не найден"}), 404
    if m["used_visits"] <= 0:
        return jsonify({"success": False, "error": "Нет отметок для отмены"}), 400

    m["used_visits"] -= 1
    if m["visit_history"]:
        m["visit_history"].pop()
    m["status"] = "active"
    ok = _save_memberships(memberships)
    return jsonify({"success": ok, "used_visits": m["used_visits"], "status": m["status"]})

@app.route("/api/membership/delete", methods=["POST"])
def api_membership_delete():
    body = request.get_json(force=True) or {}
    mid = (body.get("id") or "").strip()
    memberships = _load_memberships()
    if mid not in memberships:
        return jsonify({"success": False, "error": "Абонемент не найден"}), 404
    del memberships[mid]
    ok = _save_memberships(memberships)
    return jsonify({"success": ok})

@app.route("/api/membership/send-email", methods=["POST"])
def api_membership_send_email():
    body = request.get_json(force=True) or {}
    mid = (body.get("id") or "").strip()
    memberships = _load_memberships()
    m = memberships.get(mid)
    if not m:
        return jsonify({"success": False, "error": "Абонемент не найден"}), 404
    to_email = (body.get("email") or m.get("client_email") or "").strip()
    if not to_email or "@" not in to_email:
        return jsonify({"success": False, "error": "У клиента не указан email"}), 400
    resend_key = os.environ.get("RESEND_API_KEY")
    if not resend_key:
        return jsonify({"success": False, "error": "RESEND_API_KEY не настроен"}), 500
    card_url = f"https://rjgrooming.up.railway.app/membership/{mid}"
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
            json={
                "from": "R&J Grooming <booking@rjgrooming.salon>",
                "to": [to_email],
                "subject": f"Ваш абонемент {mid} — R&J Grooming",
                "html": (
                    "<div style='background:#0a0a09;padding:32px 24px;font-family:Arial,sans-serif;color:#f2ede2'>"
                    "<h2 style='margin:0 0 6px'>R&amp;J Grooming</h2>"
                    f"<p style='color:#cfc9ba'>Здравствуйте, {m.get('client_name','')}!</p>"
                    f"<p style='color:#cfc9ba'>Ваш абонемент <b>{m.get('plan_name','')}</b> для {m.get('pet_name','')} готов. "
                    "Откройте карточку по кнопке ниже — там всегда видно, сколько посещений использовано и сколько осталось.</p>"
                    f"<p style='margin:24px 0'><a href='{card_url}' style='background:#e6e1d5;color:#0a0a09;text-decoration:none;padding:12px 22px;border-radius:8px;font-weight:bold;display:inline-block'>Открыть абонемент</a></p>"
                    f"<p style='color:#8a8578;font-size:13px'>{card_url}</p>"
                    "</div>"
                )
            },
            timeout=10
        )
        if r.status_code >= 300:
            return jsonify({"success": False, "error": f"Resend error {r.status_code}: {r.text[:200]}"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True})

@app.route("/membership/<mid>")
def public_membership_card(mid):
    try:
        return _render_membership_card(mid)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"MEMBERSHIP CARD ERROR: {tb}", flush=True)
        return f"<pre>{tb}</pre>", 500

def _render_membership_card(mid):
    import urllib.parse as _urlp_q
    memberships = _load_memberships()
    m = memberships.get(mid)
    if not m:
        return "Абонемент не найден. Проверьте ссылку.", 404

    total = m.get("total_visits", 0)
    used = m.get("used_visits", 0)
    remaining = max(0, total - used)
    is_completed = m.get("status") == "completed" or used >= total

    def circle_html(i):
        filled = i <= used
        cls = "visit-circle filled" if filled else "visit-circle"
        mark = "\u2713" if filled else str(i)
        return f'<div class="{cls}">{mark}</div>'

    circles_html = "".join(circle_html(i) for i in range(1, total + 1))

    history = m.get("visit_history", [])
    if history:
        history_html = "".join(
            f'<div class="hist-row"><span class="hist-date">{h.get("date","")}</span><span class="hist-note">{h.get("note","уход")} \u2713</span></div>'
            for h in reversed(history)
        )
    else:
        history_html = '<div class="hist-empty">Визитов пока не было</div>'

    completed_banner = ""
    if is_completed:
        completed_banner = """
        <div class="completed-banner">
          <div class="completed-title">Абонемент завершён \U0001F90D</div>
          <a class="new-membership-btn" href="/app">Приобрести новый абонемент</a>
        </div>"""

    icon_person = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="8" r="4" stroke="rgba(230,225,215,.55)" stroke-width="1.5"/><path d="M4 20c0-4.4 3.6-7 8-7s8 2.6 8 7" stroke="rgba(230,225,215,.55)" stroke-width="1.5" stroke-linecap="round"/></svg>'
    icon_paw = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none"><ellipse cx="12" cy="16" rx="5.5" ry="4.5" stroke="rgba(230,225,215,.55)" stroke-width="1.5"/><circle cx="5.5" cy="9" r="2.1" stroke="rgba(230,225,215,.55)" stroke-width="1.5"/><circle cx="10" cy="5.5" r="2.1" stroke="rgba(230,225,215,.55)" stroke-width="1.5"/><circle cx="14.5" cy="5.5" r="2.1" stroke="rgba(230,225,215,.55)" stroke-width="1.5"/><circle cx="18.5" cy="9" r="2.1" stroke="rgba(230,225,215,.55)" stroke-width="1.5"/></svg>'
    icon_tag = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none"><path d="M11 3H5a2 2 0 00-2 2v6l10.6 10.6a2 2 0 002.8 0l5.2-5.2a2 2 0 000-2.8L11 3z" stroke="rgba(230,225,215,.55)" stroke-width="1.5" stroke-linejoin="round"/><circle cx="7.5" cy="7.5" r="1.4" stroke="rgba(230,225,215,.55)" stroke-width="1.3"/></svg>'
    icon_cal = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none"><rect x="4" y="6" width="16" height="14" rx="2" stroke="rgba(230,225,215,.55)" stroke-width="1.5"/><path d="M8 3.5v4M16 3.5v4M4 10.5h16" stroke="rgba(230,225,215,.55)" stroke-width="1.5" stroke-linecap="round"/></svg>'

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{m.get('id','')} — R&J Grooming Membership</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,500;0,600;0,700;1,500&family=Montserrat:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0a0a09;color:#f2ede2;font-family:'Montserrat',sans-serif;padding:28px 18px 50px;-webkit-font-smoothing:antialiased;min-height:100vh}}
  .wrap{{max-width:420px;margin:0 auto}}

  .flip-wrap{{perspective:1800px;margin-bottom:26px}}
  .flip-card{{position:relative;width:100%;height:88vh;min-height:520px;max-height:640px;transform-style:preserve-3d;transition:transform .75s cubic-bezier(.42,.15,.16,1);cursor:pointer}}
  .flip-card.flipped{{transform:rotateY(180deg)}}
  .face{{position:absolute;inset:0;backface-visibility:hidden;-webkit-backface-visibility:hidden;border-radius:20px;overflow:hidden;background:#0a0a09;border:1px solid rgba(200,195,180,.16)}}

  .face-front{{display:flex;align-items:center;justify-content:center}}
  .brand-frame{{position:absolute;inset:14px;border:1px solid rgba(200,195,180,.4);border-radius:12px}}
  .brand-inner{{position:absolute;inset:26px;border:1px solid rgba(200,195,180,.22);border-radius:6px;display:flex;flex-direction:column;align-items:center;justify-content:center}}
  .brand-logo-img{{width:58%;max-width:230px;height:auto;display:block;filter:drop-shadow(0 0 18px rgba(255,255,255,.06))}}
  .tap-hint{{position:absolute;bottom:22px;left:0;right:0;text-align:center;font-size:0.62rem;letter-spacing:.18em;text-transform:uppercase;color:rgba(205,199,181,.35)}}

  .face-back{{transform:rotateY(180deg);padding:24px 22px;overflow-y:auto;display:flex;flex-direction:column}}
  .back-head{{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}}
  .flip-back-btn{{background:none;border:1px solid rgba(255,255,255,.18);color:rgba(230,225,215,.7);font-size:0.62rem;letter-spacing:.12em;text-transform:uppercase;padding:6px 12px;border-radius:20px;cursor:pointer;font-family:'Montserrat',sans-serif}}
  .mem-id-label{{font-size:0.62rem;letter-spacing:.15em;text-transform:uppercase;color:rgba(230,225,215,.4)}}
  .mem-id{{font-family:'Playfair Display',serif;font-size:1.5rem;font-weight:600;color:#f2ede2}}

  .info-rows{{border-top:1px solid rgba(255,255,255,.08);margin-bottom:6px}}
  .info-row{{display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid rgba(255,255,255,.08)}}
  .info-row .ico{{flex-shrink:0;width:30px;height:30px;border-radius:50%;background:rgba(255,255,255,.05);display:flex;align-items:center;justify-content:center}}
  .info-row .info-label{{font-size:0.6rem;letter-spacing:.1em;text-transform:uppercase;color:rgba(230,225,215,.42);margin-bottom:2px}}
  .info-row .info-val{{font-size:0.92rem;color:#f2ede2;font-weight:500}}
  .info-row.two-col{{display:grid;grid-template-columns:1fr 1fr;gap:12px;align-items:start}}
  .info-row.two-col .info-sub{{display:flex;align-items:center;gap:10px}}

  .counter-card{{background:#131210;border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:18px 16px;margin:16px 0}}
  .counter-label{{font-size:0.6rem;letter-spacing:.2em;text-transform:uppercase;color:rgba(200,195,180,.6);margin-bottom:14px;text-align:center}}
  .visit-circles{{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-bottom:16px}}
  .visit-circle{{width:32px;height:32px;border-radius:50%;border:1.5px solid rgba(200,195,180,.35);display:flex;align-items:center;justify-content:center;font-size:0.78rem;color:rgba(230,225,215,.4);font-family:'Playfair Display',serif}}
  .visit-circle.filled{{background:#e6e1d5;border-color:#e6e1d5;color:#0a0a09;font-weight:700}}
  .counter-stats{{display:flex;justify-content:space-around;text-align:center;padding-top:12px;border-top:1px solid rgba(255,255,255,.07)}}
  .counter-stat .n{{font-family:'Playfair Display',serif;font-size:1.25rem;font-weight:600;color:#f2ede2}}
  .counter-stat .l{{font-size:0.58rem;letter-spacing:.06em;text-transform:uppercase;color:rgba(230,225,215,.45);margin-top:2px}}

  .list-label{{font-size:0.62rem;letter-spacing:.18em;text-transform:uppercase;color:rgba(230,225,215,.5);margin:6px 0 10px}}
  .hist-row{{display:flex;justify-content:space-between;padding:9px 0;border-bottom:1px solid rgba(255,255,255,.06);font-size:0.82rem}}
  .hist-date{{color:rgba(230,225,215,.5)}}
  .hist-note{{color:#f2ede2}}
  .hist-empty{{text-align:center;padding:16px 0;color:rgba(230,225,215,.35);font-size:0.8rem}}

  .completed-banner{{text-align:center;background:#131210;border:1px solid rgba(255,255,255,.1);border-radius:14px;padding:20px 18px;margin:16px 0}}
  .completed-title{{font-family:'Playfair Display',serif;font-size:1.05rem;margin-bottom:12px}}
  .new-membership-btn{{display:inline-block;background:#e6e1d5;color:#0a0a09;text-decoration:none;padding:11px 22px;border-radius:8px;font-size:0.82rem;font-weight:600}}

  .qr-section{{text-align:center;margin-top:auto;padding-top:18px}}
  .qr-section img{{border-radius:10px;border:1px solid rgba(255,255,255,.1)}}
  .qr-caption{{font-size:0.64rem;color:rgba(230,225,215,.4);margin-top:8px}}
</style>
</head>
<body>
<div class="wrap">

  <div class="flip-wrap">
    <div class="flip-card" id="flipCard">

      <div class="face face-front" id="cardFront">
        <div class="brand-frame"></div>
        <div class="brand-inner">
          <img class="brand-logo-img" src="data:image/png;base64,{_get_logo_b64()}" alt="R&amp;J Grooming">
        </div>
        <div class="tap-hint">Нажмите, чтобы открыть абонемент</div>
      </div>

      <div class="face face-back">
        <div class="back-head">
          <div>
            <div class="mem-id-label">Абонемент</div>
            <div class="mem-id">{m.get('id','')}</div>
          </div>
          <button class="flip-back-btn" id="flipBackBtn">← Назад</button>
        </div>

        <div class="info-rows">
          <div class="info-row"><div class="ico">{icon_person}</div><div><div class="info-label">Владелец</div><div class="info-val">{m.get('client_name','—')}</div></div></div>
          <div class="info-row"><div class="ico">{icon_paw}</div><div><div class="info-label">Питомец</div><div class="info-val">{m.get('pet_name','—')} · {m.get('pet_type','—')}</div></div></div>
          <div class="info-row"><div class="ico">{icon_tag}</div><div><div class="info-label">Абонемент</div><div class="info-val">{m.get('plan_name','—')}</div></div></div>
          <div class="info-row two-col">
            <div class="info-sub"><div class="ico">{icon_cal}</div><div><div class="info-label">Приобретён</div><div class="info-val">{m.get('purchase_date','—')}</div></div></div>
            <div class="info-sub"><div class="ico">{icon_cal}</div><div><div class="info-label">До</div><div class="info-val">{m.get('expiry_date','—')}</div></div></div>
          </div>
        </div>

        <div class="counter-card">
          <div class="counter-label">Посещения</div>
          <div class="visit-circles">{circles_html}</div>
          <div class="counter-stats">
            <div class="counter-stat"><div class="n">{used}</div><div class="l">использовано</div></div>
            <div class="counter-stat"><div class="n">{remaining}</div><div class="l">осталось</div></div>
            <div class="counter-stat"><div class="n">{total}</div><div class="l">всего</div></div>
          </div>
        </div>

        {completed_banner}

        <div class="list-label">История посещений</div>
        {history_html}

        <div class="qr-section">
          <div class="qr-caption">{m.get('id','')} · rjgrooming.salon</div>
        </div>
      </div>

    </div>
  </div>

</div>
<script>
var flipCard = document.getElementById('flipCard');
var flipBackBtn = document.getElementById('flipBackBtn');
document.getElementById('cardFront').addEventListener('click', function(){{
  flipCard.classList.add('flipped');
}});
flipBackBtn.addEventListener('click', function(e){{
  e.stopPropagation();
  flipCard.classList.remove('flipped');
}});
</script>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/admin/migrate-client-data")
def admin_migrate_client_data():
    """Разовая миграция: старые отдельные файлы rjgrooming/client_data/*.json
    переносятся в единый rjgrooming/client_data_index.json."""
    import base64 as _b64
    auth = _b64.b64encode(f"{CLOUDINARY_API_KEY}:{CLOUDINARY_API_SECRET}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}

    try:
        r = requests.get(
            f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/resources/raw",
            headers=headers,
            params={"prefix": "rjgrooming/client_data/", "type": "upload", "max_results": 500},
            timeout=20
        )
        listing = r.json()
    except Exception as e:
        return jsonify({"success": False, "error": f"list failed: {e}"}), 500

    resources = listing.get("resources", [])
    all_data = _load_all_client_data()
    migrated = []
    errors = []

    for res in resources:
        public_id = res.get("public_id", "")
        key = public_id.split("/")[-1]
        secure_url = res.get("secure_url")
        if not secure_url:
            continue
        try:
            fr = requests.get(secure_url, timeout=10)
            if fr.status_code == 200:
                all_data[key] = fr.json()
                migrated.append(key)
        except Exception as e:
            errors.append(f"{key}: {e}")

    ok = _save_all_client_data(all_data)
    return jsonify({
        "success": ok,
        "found_old_files": len(resources),
        "migrated_count": len(migrated),
        "migrated_keys": migrated,
        "errors": errors,
        "total_in_new_index": len(all_data)
    })

@app.route("/api/breed-prices")
def api_breed_prices():
    import base64 as _b64_mod
    try:
        html = _b64_mod.b64decode(BOOKING_HTML_B64).decode("utf-8")
        m = re.search(r"var DATA = (\[.*?\]);", html, re.DOTALL)
        if not m:
            return jsonify({"success": False, "error": "Не удалось найти данные о ценах"}), 500
        data = json.loads(m.group(1))
        result = [{"breed": d["breed"], "services": d["services"]} for d in data]
        return jsonify({"success": True, "breeds": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/admin/debug-memberships")
def admin_debug_memberships():
    import base64 as _b64_mod
    debug = {}

    debug["env_check"] = {
        "cloud_name": CLOUDINARY_CLOUD_NAME,
        "api_key_set": bool(CLOUDINARY_API_KEY),
        "api_secret_set": bool(CLOUDINARY_API_SECRET),
    }

    try:
        auth = _b64_mod.b64encode(f"{CLOUDINARY_API_KEY}:{CLOUDINARY_API_SECRET}".encode()).decode()
        meta_r = requests.get(
            f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/resources/raw/upload/{_MEMBERSHIP_INDEX_PUBLIC_ID}",
            headers={"Authorization": f"Basic {auth}"},
            timeout=8
        )
        debug["admin_api_status"] = meta_r.status_code
        debug["admin_api_response"] = meta_r.json()
    except Exception as e:
        debug["admin_api_error"] = str(e)

    debug["load_memberships_result"] = _load_memberships()
    debug["load_memberships_keys"] = list(debug["load_memberships_result"].keys())

    return jsonify(debug)

@app.route("/admin")
def admin_hub():

    due_badge = ""
    try:
        rows, _ = _get_reminder_dashboard_rows()
        due_now = sum(1 for r in rows if (r["status"] == "stage1" and not r["stage1_done"]) or (r["status"] == "done" and not r["stage2_done"]))
        if due_now:
            due_badge = f'<span class="card-badge">{due_now}</span>'
    except Exception:
        pass

    P = "?pass=anza1985"
    import urllib.parse as _urlp_hub
    _calendar_id = "50218a0d445e1be8f510d21128b82c0d33b6a78c57bb5a4ea9d14eb2fbcfeaa6@group.calendar.google.com"
    gcal_link = f"https://calendar.google.com/calendar/u/0/r?cid={_urlp_hub.quote(_calendar_id)}"
    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>R&J Grooming — Панель администратора</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,500;0,600;0,700;1,500&family=Montserrat:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0a0a09;color:#f2ede2;font-family:'Montserrat',sans-serif;padding:44px 20px 80px}}
  .wrap{{max-width:680px;margin:0 auto}}
  .eyebrow{{font-size:0.68rem;letter-spacing:.3em;text-transform:uppercase;color:rgba(242,237,226,.4);margin-bottom:10px}}
  h1{{font-family:'Playfair Display',serif;font-weight:600;font-size:2.3rem;margin-bottom:4px}}
  .sub{{font-size:0.8rem;color:rgba(242,237,226,.5);margin-bottom:40px}}
  .section-label{{font-size:0.66rem;letter-spacing:.2em;text-transform:uppercase;color:#c9a05a;margin:28px 0 14px}}
  .cards{{display:flex;flex-direction:column;gap:12px}}
  .card{{position:relative;display:flex;align-items:center;gap:16px;background:#141310;border:1px solid rgba(201,160,90,.18);border-radius:14px;padding:20px 22px;text-decoration:none;color:#f2ede2;transition:border-color .15s}}
  .card:active{{border-color:rgba(201,160,90,.5)}}
  .card-icon{{font-size:1.6rem;width:48px;height:48px;border-radius:12px;background:rgba(201,160,90,.12);display:flex;align-items:center;justify-content:center;flex-shrink:0}}
  .card-txt{{flex:1}}
  .card-name{{font-family:'Playfair Display',serif;font-size:1.25rem;font-weight:600}}
  .card-desc{{font-size:0.78rem;color:rgba(242,237,226,.5);margin-top:3px}}
  .card-arrow{{color:rgba(201,160,90,.6);font-size:1.2rem}}
  .card-badge{{position:absolute;top:-8px;right:-8px;background:#e0824a;color:#0a0a09;font-size:0.72rem;font-weight:700;min-width:22px;height:22px;border-radius:12px;display:flex;align-items:center;justify-content:center;padding:0 6px}}
  .secondary{{display:flex;gap:10px;flex-wrap:wrap}}
  .schip{{flex:1;min-width:140px;text-align:center;background:#141310;border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:12px 10px;text-decoration:none;color:rgba(242,237,226,.75);font-size:0.78rem}}
</style>
</head>
<body>
<div class="wrap">
  <div class="eyebrow">R&J Grooming</div>
  <h1>Панель администратора</h1>
  <div class="sub">Бронирование, статистика и напоминания — в одном месте</div>

  <div class="section-label">Модули</div>
  <div class="cards">
    <a class="card" href="/app{P}">
      <div class="card-icon">📅</div>
      <div class="card-txt">
        <div class="card-name">Бронирование</div>
        <div class="card-desc">Виджет онлайн-записи для клиентов</div>
      </div>
      <div class="card-arrow">→</div>
    </a>
    <a class="card" href="/stats{P}">
      <div class="card-icon">📊</div>
      <div class="card-txt">
        <div class="card-name">Статистика</div>
        <div class="card-desc">Выручка, записи, доли по мастерам</div>
      </div>
      <div class="card-arrow">→</div>
    </a>
    <a class="card" href="/admin/reminders{P}">
      {due_badge}
      <div class="card-icon">🔔</div>
      <div class="card-txt">
        <div class="card-name">Напоминания</div>
        <div class="card-desc">Клиенты без визита 35+ дней</div>
      </div>
      <div class="card-arrow">→</div>
    </a>
    <a class="card" href="/admin/clients{P}">
      <div class="card-icon">👥</div>
      <div class="card-txt">
        <div class="card-name">Клиенты</div>
        <div class="card-desc">Вся база — история визитов и контакты</div>
      </div>
      <div class="card-arrow">→</div>
    </a>
    <a class="card" href="/admin/memberships{P}">
      <div class="card-icon">🎫</div>
      <div class="card-txt">
        <div class="card-name">Абонементы</div>
        <div class="card-desc">Цифровые карты посещений</div>
      </div>
      <div class="card-arrow">→</div>
    </a>
    <a class="card" href="{gcal_link}" target="_blank" rel="noopener">
      <div class="card-icon">🗓️</div>
      <div class="card-txt">
        <div class="card-name">Google Calendar</div>
        <div class="card-desc">Сам календарь салона — все брони</div>
      </div>
      <div class="card-arrow">→</div>
    </a>
  </div>
</div>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/admin/search")
def admin_client_search():

    query = (request.args.get("q") or "").strip()
    results = []
    error = None

    if query:
        try:
            today = datetime.now(_REMINDER_TZ).date() if _REMINDER_TZ else datetime.utcnow().date()
            bookings = _fetch_full_history_bookings()

            q_lower = query.lower()
            q_digits = re.sub(r"[^\d]", "", query)

            by_phone = {}
            for b in bookings:
                name = (b.get("clientName") or "")
                phone = (b.get("clientPhone") or "")
                pet = (b.get("petName") or "")
                phone_digits = re.sub(r"[^\d]", "", phone)
                match = (
                    (q_lower and (q_lower in name.lower() or q_lower in pet.lower())) or
                    (q_digits and q_digits in phone_digits)
                )
                if not match:
                    continue
                key = phone or name
                try:
                    d = datetime.strptime(b.get("date", ""), "%d.%m.%Y").date()
                except Exception:
                    d = None
                if key not in by_phone or (d and (not by_phone[key]["_d"] or d > by_phone[key]["_d"])):
                    by_phone[key] = {
                        "_d": d, "name": name, "phone": phone, "pet": pet,
                        "breed": b.get("breed", ""), "master": b.get("master", ""),
                        "date": b.get("date", ""), "service": b.get("service", "")
                    }
            all_client_data = _load_all_client_data()
            for key, entry in by_phone.items():
                saved = all_client_data.get(_client_data_key(entry["phone"]), {}) if entry["phone"] else {}
                if saved.get("name"):
                    entry["name"] = saved["name"]
                entry["display_phone"] = _normalize_phone(saved.get("phone_override") or entry["phone"])

            results = sorted(by_phone.values(), key=lambda x: x["_d"] or datetime.min.date(), reverse=True)
        except Exception as e:
            error = str(e)

    def result_html(r):
        import urllib.parse as _urlp_s
        display_phone = r.get("display_phone") or r["phone"]
        wa_digits = re.sub(r"[^\d]", "", display_phone or "")
        phone_encoded = _urlp_s.quote(r["phone"] or "")
        return f"""
        <a class="row" href="/admin/client?phone={phone_encoded}&pass=anza1985">
          <div class="row-top">
            <div class="who">
              <span class="name">{r['name'] or '—'}</span>
              <span class="pet">{r['pet'] or '—'} · {r['breed'] or '—'}</span>
            </div>
            <span class="row-arrow">→</span>
          </div>
          <div class="row-bottom">
            <span>Последний визит: {r['date'] or '—'} · {r['master'] or '—'}</span>
          </div>
          <div class="row-bottom" style="margin-top:6px">
            <span class="contacts">
              <span onclick="event.stopPropagation();location.href='tel:{display_phone}'" style="color:#c9a05a">{display_phone or '—'}</span>
              <span onclick="event.stopPropagation();window.open('https://wa.me/{wa_digits}','_blank')" class="wa">WhatsApp</span>
            </span>
          </div>
          <div class="row-hint">Скопируй имя выше и вставь в поиск Instagram Direct, чтобы найти переписку</div>
        </a>"""

    if query and not results and not error:
        results_html = '<div class="empty">Ничего не найдено — проверь написание или попробуй телефон/кличку</div>'
    elif error:
        results_html = f'<div class="empty" style="color:#e0824a">Ошибка: {error}</div>'
    elif not query:
        results_html = '<div class="empty">Введи имя, телефон или кличку питомца</div>'
    else:
        results_html = "".join(result_html(r) for r in results)

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>R&J Grooming — Поиск клиента</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,500;0,600;0,700;1,500&family=Montserrat:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0a0a09;color:#f2ede2;font-family:'Montserrat',sans-serif;padding:36px 20px 80px}}
  .wrap{{max-width:640px;margin:0 auto}}
  .eyebrow{{font-size:0.68rem;letter-spacing:.3em;text-transform:uppercase;color:rgba(242,237,226,.4);margin-bottom:10px}}
  .back-link{{display:inline-block;font-size:0.74rem;color:rgba(201,160,90,.75);text-decoration:none;margin-bottom:16px}}
  h1{{font-family:'Playfair Display',serif;font-weight:600;font-size:2.1rem;margin-bottom:20px}}
  form{{display:flex;gap:8px;margin-bottom:28px}}
  input[type=text]{{flex:1;background:#151310;border:1px solid rgba(201,160,90,.3);border-radius:10px;padding:14px 16px;color:#f2ede2;font-family:'Montserrat',sans-serif;font-size:0.95rem}}
  input[type=text]:focus{{outline:none;border-color:#c9a05a}}
  button{{background:#c9a05a;color:#0a0a09;border:none;border-radius:10px;padding:0 22px;font-weight:600;font-size:0.9rem;cursor:pointer}}
  .row{{display:block;background:#131210;border:1px solid rgba(255,255,255,.06);border-radius:12px;padding:16px 18px;margin-bottom:10px;text-decoration:none;color:inherit}}
  .row-top{{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:10px}}
  .who{{display:flex;flex-direction:column}}
  .name{{font-family:'Playfair Display',serif;font-size:1.2rem;font-weight:600}}
  .pet{{font-size:0.76rem;color:rgba(242,237,226,.5);margin-top:2px}}
  .row-arrow{{color:rgba(201,160,90,.6)}}
  .row-bottom{{font-size:0.78rem;color:rgba(242,237,226,.55)}}
  .contacts{{display:flex;align-items:center;gap:10px}}
  .wa{{color:#4ade80;font-size:0.72rem;font-weight:600;border:1px solid rgba(74,222,128,.35);border-radius:20px;padding:3px 10px;cursor:pointer}}
  .row-hint{{margin-top:10px;padding-top:10px;border-top:1px solid rgba(255,255,255,.06);font-size:0.7rem;color:rgba(201,160,90,.7);font-style:italic}}
  .empty{{text-align:center;padding:40px 0;color:rgba(242,237,226,.4);font-size:0.85rem}}
</style>
</head>
<body>
<div class="wrap">
  <a href="/admin?pass=anza1985" class="back-link">← Админ-панель</a>
  <div class="eyebrow">R&J Grooming · Поиск</div>
  <h1>Найти клиента</h1>
  <form method="get">
    <input type="hidden" name="pass" value="anza1985">
    <input type="text" name="q" placeholder="Имя, телефон или кличка питомца" value="{query}" autofocus>
    <button type="submit">Искать</button>
  </form>
  {results_html}
</div>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/admin/reminders")
def admin_reminders_dashboard():

    try:
        rows, today = _get_reminder_dashboard_rows()
        error = None
    except Exception as e:
        rows, today, error = [], datetime.now(_REMINDER_TZ).date() if _REMINDER_TZ else datetime.utcnow().date(), str(e)

    stage1_count = sum(1 for r in rows if r["status"] == "stage1" and not r["stage1_done"])
    stage2_count = sum(1 for r in rows if r["status"] == "done" and not r["stage2_done"])
    pending_count = sum(1 for r in rows if r["status"] == "pending")

    STATUS_META = {
        "pending": {"color": "#8a8578", "label": "ожидание"},
        "stage1":  {"color": "#c9a05a", "label": "напоминание #1"},
        "done":    {"color": "#e0824a", "label": "напоминание #2"},
    }

    def bar_color(days_since):
        if days_since < 35:
            return "#8a8578"
        elif days_since == 35:
            return "#4ade80"
        else:
            return "#e0524a"

    def row_html(r):
        import urllib.parse as _urlp
        pct = min(100, round(r["days_since"] / 42 * 100))
        meta = STATUS_META[r["status"]]
        bar = bar_color(r["days_since"])
        email_html = f'<a class="email" href="mailto:{r["email"]}">{r["email"]}</a>' if r.get("email") else '<span class="email-empty">email не указан</span>'
        wa_digits = re.sub(r"[^\d]", "", r.get("display_phone") or r["phone"] or "")
        date_str = r["date"].strftime("%d.%m.%Y")

        first_name = (r["name"] or "").split()[0] if r["name"] else ""
        pet = r["pet"] or "питомец"
        greeting = f"Здравствуйте, {first_name}! 🐾" if first_name else "Здравствуйте! 🐾"
        booking_link = "https://rjgrooming.up.railway.app/app"

        def _days_word(n):
            n10, n100 = n % 10, n % 100
            if 11 <= n100 <= 14:
                return "дней"
            if n10 == 1:
                return "день"
            if 2 <= n10 <= 4:
                return "дня"
            return "дней"

        days_word = _days_word(r["days_since"])
        if r["days_since"] >= 42:
            msg = (f"{greeting} {pet} давно не был{'а' if pet.endswith(('а','я')) else ''} у нас — "
                   f"прошло уже {r['days_since']} {days_word} с последнего визита. "
                   f"Будем рады снова вас видеть в R&J Grooming! Записаться можно здесь: {booking_link}")
        elif r["days_since"] >= 35:
            msg = (f"{greeting} Прошло {r['days_since']} {days_word} с последнего визита {pet} к нам — "
                   f"самое время подумать о следующем груминге. Будем рады видеть вас снова! "
                   f"Записаться можно здесь: {booking_link}")
        else:
            msg = ""
        wa_href = f"https://wa.me/{wa_digits}?text={_urlp.quote(msg)}" if msg else f"https://wa.me/{wa_digits}"

        checks = ""
        if r["days_since"] >= 35:
            c1 = "checked" if r["stage1_done"] else ""
            checks += f'<label class="chk"><input type="checkbox" data-phone="{r["phone"]}" data-date="{date_str}" data-stage="1" {c1}> Напоминание #1 отправлено</label>'
        if r["days_since"] >= 42:
            c2 = "checked" if r["stage2_done"] else ""
            checks += f'<label class="chk"><input type="checkbox" data-phone="{r["phone"]}" data-date="{date_str}" data-stage="2" {c2}> Напоминание #2 отправлено</label>'
        checks_html = f'<div class="row-checks">{checks}</div>' if checks else ""

        if r["days_since"] >= 42:
            needs_action = not r["stage2_done"]
        elif r["days_since"] >= 35:
            needs_action = not r["stage1_done"]
        else:
            needs_action = False
        pulse_class = " pulse" if needs_action else ""

        return f"""
        <div class="row{pulse_class}">
          <div class="row-top">
            <div class="who">
              <span class="name">{r['name'] or '—'}</span>
              <span class="pet">{r['pet'] or '—'} · {r['breed'] or '—'}</span>
            </div>
            <div class="badge" style="color:{meta['color']};border-color:{meta['color']}55;background:{meta['color']}14">{meta['label']}</div>
          </div>
          <div class="track">
            <div class="fill" style="width:{pct}%;background:{bar}"></div>
            <div class="tick" style="left:{35/42*100:.2f}%"></div>
            <div class="tick" style="left:100%"></div>
          </div>
          <div class="row-bottom">
            <span>Визит {date_str} · {r['master'] or '—'}</span>
            <span class="days">{r['days_since']} дн.</span>
            <span class="contacts">
              <a class="phone" href="tel:{r.get('display_phone') or r['phone']}">{r.get('display_phone') or r['phone']}</a>
              <a class="wa" href="{wa_href}" target="_blank" rel="noopener">WhatsApp{' с текстом' if msg else ''}</a>
            </span>
          </div>
          <div class="row-contact">{email_html}</div>
          {checks_html}
        </div>"""

    rows_html = "".join(row_html(r) for r in rows) if rows else '<div class="empty">За последние 40 дней визитов не найдено</div>'
    error_html = f'<div class="empty" style="color:#e0824a">Ошибка загрузки данных: {error}</div>' if error else ""

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>R&J Grooming — Напоминания</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,500;0,600;0,700;1,500&family=Montserrat:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0a0a09;color:#f2ede2;font-family:'Montserrat',sans-serif;padding:36px 20px 80px}}
  .wrap{{max-width:720px;margin:0 auto}}
  .eyebrow{{font-size:0.68rem;letter-spacing:.3em;text-transform:uppercase;color:rgba(242,237,226,.4);margin-bottom:10px}}
  .back-link{{display:inline-block;font-size:0.74rem;color:rgba(201,160,90,.75);text-decoration:none;margin-bottom:16px}}
  h1{{font-family:'Playfair Display',serif;font-weight:600;font-size:2.1rem;margin-bottom:6px}}
  .sub{{font-size:0.78rem;color:rgba(242,237,226,.5);margin-bottom:32px}}
  .stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:36px}}
  .stat{{background:#151310;border:1px solid rgba(201,160,90,.18);border-radius:12px;padding:18px 14px}}
  .stat .n{{font-family:'Playfair Display',serif;font-size:1.9rem;font-weight:600;color:#c9a05a}}
  .stat .l{{font-size:0.66rem;letter-spacing:.08em;text-transform:uppercase;color:rgba(242,237,226,.5);margin-top:4px}}
  .list-label{{font-size:0.68rem;letter-spacing:.2em;text-transform:uppercase;color:#c9a05a;margin-bottom:14px}}
  .row{{background:#131210;border:1px solid rgba(255,255,255,.06);border-radius:12px;padding:16px 18px;margin-bottom:10px}}
  .row.pulse{{animation:rowPulse 1.8s ease-in-out infinite}}
  @keyframes rowPulse{{
    0%,100%{{border-color:rgba(224,82,74,.25);box-shadow:0 0 0 0 rgba(224,82,74,.15)}}
    50%{{border-color:rgba(224,82,74,.75);box-shadow:0 0 0 4px rgba(224,82,74,.08)}}
  }}
  .row-top{{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:12px}}
  .who{{display:flex;flex-direction:column}}
  .name{{font-family:'Playfair Display',serif;font-size:1.15rem;font-weight:600}}
  .pet{{font-size:0.76rem;color:rgba(242,237,226,.5);margin-top:2px}}
  .badge{{font-size:0.62rem;letter-spacing:.06em;text-transform:uppercase;padding:5px 10px;border-radius:20px;border:1px solid;white-space:nowrap;flex-shrink:0}}
  .track{{position:relative;height:6px;background:rgba(255,255,255,.08);border-radius:4px;margin-bottom:10px}}
  .fill{{position:absolute;top:0;left:0;height:100%;border-radius:4px;transition:width .3s}}
  .tick{{position:absolute;top:-3px;width:2px;height:12px;background:rgba(242,237,226,.25)}}
  .row-bottom{{display:flex;justify-content:space-between;align-items:center;font-size:0.74rem;color:rgba(242,237,226,.55)}}
  .row-bottom .days{{font-weight:600;color:#f2ede2}}
  .phone{{color:#c9a05a;text-decoration:none}}
  .contacts{{display:flex;align-items:center;gap:10px}}
  .wa{{color:#4ade80;text-decoration:none;font-size:0.72rem;font-weight:600;border:1px solid rgba(74,222,128,.35);border-radius:20px;padding:3px 10px}}
  .row-contact{{margin-top:6px;font-size:0.74rem}}
  .row-contact .email{{color:rgba(242,237,226,.65);text-decoration:none}}
  .row-contact .email-empty{{color:rgba(242,237,226,.3);font-style:italic}}
  .row-checks{{margin-top:12px;padding-top:10px;border-top:1px solid rgba(255,255,255,.06);display:flex;flex-direction:column;gap:8px}}
  .chk{{display:flex;align-items:center;gap:8px;font-size:0.78rem;color:rgba(242,237,226,.75);cursor:pointer;user-select:none}}
  .chk input{{width:16px;height:16px;accent-color:#c9a05a;cursor:pointer}}
  .empty{{text-align:center;padding:40px 0;color:rgba(242,237,226,.4);font-size:0.85rem}}
  @media(max-width:480px){{
    .row-bottom{{flex-direction:column;align-items:flex-start;gap:4px}}
  }}
</style>
</head>
<body>
<div class="wrap">
  <a href="/admin?pass=anza1985" class="back-link">← Админ-панель</a>
  <div class="eyebrow">R&J Grooming · Напоминания</div>
  <h1>Клиенты без визита</h1>
  <div class="sub">Окно 40 дней от последнего визита · {today.strftime('%d.%m.%Y')}</div>

  <div class="stats">
    <div class="stat"><div class="n" id="statStage1">{stage1_count}</div><div class="l">напоминание #1</div></div>
    <div class="stat"><div class="n" id="statStage2">{stage2_count}</div><div class="l">напоминание #2</div></div>
    <div class="stat"><div class="n">{pending_count}</div><div class="l">ожидают</div></div>
  </div>

  <div class="list-label">Все клиенты ({len(rows)})</div>
  {error_html}
  {rows_html}
</div>
<script>
document.addEventListener('change', function(e){{
  if(e.target && e.target.matches('.chk input[type="checkbox"]')){{
    var el = e.target;
    var stage = parseInt(el.getAttribute('data-stage'), 10);
    var counterEl = document.getElementById(stage === 1 ? 'statStage1' : 'statStage2');
    if(counterEl){{
      var n = parseInt(counterEl.textContent, 10) || 0;
      counterEl.textContent = el.checked ? Math.max(0, n - 1) : n + 1;
    }}
    fetch('/api/mark-reminder', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        phone: el.getAttribute('data-phone'),
        date: el.getAttribute('data-date'),
        stage: stage,
        done: el.checked
      }})
    }}).catch(function(err){{ console.error('mark-reminder failed', err); }});
  }}
}});
</script>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

@app.route("/admin/export-clients")
def admin_export_clients():
    from io import BytesIO
    from flask import send_file
    try:
        content, filename = _build_client_export_workbook()
    except Exception as e:
        return f"Ошибка получения данных: {e}", 500
    return send_file(
        BytesIO(content),
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

def send_weekly_client_export_email():
    resend_key = os.environ.get("RESEND_API_KEY")
    if not resend_key:
        print("WEEKLY EXPORT: RESEND_API_KEY не задан", flush=True)
        return
    try:
        import base64 as _b64
        content, filename = _build_client_export_workbook()
        requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
            json={
                "from": "booking@rjgrooming.salon",
                "to": ["myrnj1@gmail.com"],
                "subject": f"R&J Grooming — еженедельная выгрузка клиентов ({datetime.now().strftime('%d.%m.%Y')})",
                "html": "<p>Во вложении — актуальная выгрузка всех клиентов и лист с напоминаниями (+35/+42 дня).</p>",
                "attachments": [{
                    "filename": filename,
                    "content": _b64.b64encode(content).decode("ascii")
                }]
            },
            timeout=30
        )
        print("WEEKLY EXPORT: письмо отправлено", flush=True)
    except Exception as e:
        print(f"WEEKLY EXPORT ERROR: {e}", flush=True)

def _weekly_export_scheduler_loop():
    while True:
        try:
            now = datetime.now(_REMINDER_TZ) if _REMINDER_TZ else datetime.utcnow()
            next_run = now.replace(hour=9, minute=0, second=0, microsecond=0)
            days_ahead = (0 - next_run.weekday()) % 7  # 0 = понедельник
            if days_ahead == 0 and next_run <= now:
                days_ahead = 7
            next_run += timedelta(days=days_ahead)
            sleep_seconds = (next_run - now).total_seconds()
            _time.sleep(max(sleep_seconds, 60))
            send_weekly_client_export_email()
        except Exception as e:
            print(f"WEEKLY EXPORT SCHEDULER ERROR: {e}", flush=True)
            _time.sleep(3600)

threading.Thread(target=_weekly_export_scheduler_loop, daemon=True).start()

@app.route("/api/send-weekly-export")
def api_send_weekly_export():
    send_weekly_client_export_email()
    return jsonify({"success": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
