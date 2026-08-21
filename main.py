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

# ── РУЧНЫЕ ДАННЫЕ КЛИЕНТА (email, Instagram) — Cloudinary raw, постоянно ──
def _client_data_key(phone):
    import hashlib
    return hashlib.sha1((phone or "").strip().lower().encode("utf-8")).hexdigest()

def _client_data_public_id(key):
    return f"rjgrooming/client_data/{key}.json"

def _client_data_url(key):
    return f"https://res.cloudinary.com/{CLOUDINARY_CLOUD_NAME}/raw/upload/{_client_data_public_id(key)}"

def _load_client_data(phone):
    key = _client_data_key(phone)
    try:
        r = requests.get(_client_data_url(key), timeout=8)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}

@app.route("/api/save-client-data", methods=["POST"])
def api_save_client_data():
    import hashlib as _hashlib
    body = request.get_json(force=True) or {}
    phone = (body.get("phone") or "").strip()
    if not phone:
        return jsonify({"success": False, "error": "phone обязателен"}), 400

    name = (body.get("name") or "").strip()
    phone_override = (body.get("phone_override") or "").strip()
    email = (body.get("email") or "").strip()
    instagram = (body.get("instagram") or "").strip().lstrip("@")
    comment = (body.get("comment") or "").strip()
    payload = json.dumps({
        "name": name, "phone_override": phone_override,
        "email": email, "instagram": instagram, "comment": comment
    }, ensure_ascii=False)

    key = _client_data_key(phone)
    public_id = _client_data_public_id(key)
    timestamp = int(_time.time())
    params_to_sign = {"overwrite": "true", "public_id": public_id, "timestamp": timestamp}
    to_sign = "&".join(f"{k}={v}" for k, v in sorted(params_to_sign.items()))
    signature = _hashlib.sha1((to_sign + CLOUDINARY_API_SECRET).encode("utf-8")).hexdigest()

    try:
        resp = requests.post(
            f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/raw/upload",
            data={
                "api_key": CLOUDINARY_API_KEY,
                "timestamp": timestamp,
                "public_id": public_id,
                "overwrite": "true",
                "signature": signature,
            },
            files={"file": ("data.json", payload, "application/json")},
            timeout=20
        )
        rbody = resp.json()
        if resp.status_code != 200 or "secure_url" not in rbody:
            return jsonify({"success": False, "error": rbody.get("error", {}).get("message", "Cloudinary upload failed")}), 500
        return jsonify({"success": True, "name": name, "phone_override": phone_override, "email": email, "instagram": instagram, "comment": comment})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

def _get_reminder_dashboard_rows():
    """Общая функция: последний визит на клиента за всю историю,
    с расчётом стадии напоминания. Используется дашбордом и отчётами."""
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
                "breed": b.get("breed", ""),
                "email": b.get("clientEmail", "")
            }

    reminder_status = _load_reminder_status()
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
        client_saved = _load_client_data(phone)
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
BOOKING_HTML_B64 = "77u/PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9InJ1Ij4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idGhlbWUtY29sb3IiIGNvbnRlbnQ9IiMwYTBhMGEiPgo8bWV0YSBuYW1lPSJ2aWV3cG9ydCIgY29udGVudD0id2lkdGg9ZGV2aWNlLXdpZHRoLGluaXRpYWwtc2NhbGU9MSI+Cjx0aXRsZT5SJkogR3Jvb21pbmc8L3RpdGxlPgo8bGluayBocmVmPSJodHRwczovL2ZvbnRzLmdvb2dsZWFwaXMuY29tL2NzczI/ZmFtaWx5PUNvcm1vcmFudCtHYXJhbW9uZDp3Z2h0QDQwMDs2MDAmZmFtaWx5PVBsYXlmYWlyK0Rpc3BsYXk6aXRhbCx3Z2h0QDAsNDAwOzAsNjAwOzAsNzAwOzEsNDAwJmZhbWlseT1Nb250c2VycmF0OndnaHRAMzAwOzQwMDs1MDA7NjAwJmRpc3BsYXk9c3dhcCIgcmVsPSJzdHlsZXNoZWV0Ij4KPHN0eWxlPgoqe2JveC1zaXppbmc6Ym9yZGVyLWJveDttYXJnaW46MDtwYWRkaW5nOjB9Cmh0bWwsYm9keXttaW4taGVpZ2h0OjEwMHZoO2JhY2tncm91bmQ6IzBhMGEwYTtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXdlaWdodDo0MDB9Ci5zY3JlZW57ZGlzcGxheTpub25lO21pbi1oZWlnaHQ6MTAwdmg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2FsaWduLWl0ZW1zOmNlbnRlcjtwYWRkaW5nOjQ4cHggMCA2NHB4fQouc2NyZWVuLmFjdGl2ZXtkaXNwbGF5OmZsZXh9Ci5jb257d2lkdGg6MTAwJTttYXgtd2lkdGg6NDAwcHg7cGFkZGluZzowIDI4cHh9Ci5iYWNrLWJ0bntkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo4cHg7Y29sb3I6I2ZmZmZmZjtmb250LXNpemU6MC44cmVtO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyOm5vbmU7Y3Vyc29yOnBvaW50ZXI7cGFkZGluZzowO21hcmdpbi1ib3R0b206MzZweDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXdlaWdodDozMDA7dHJhbnNpdGlvbjpjb2xvciAuMnN9Ci5iYWNrLWJ0bjpob3Zlcntjb2xvcjojZmZmZmZmfQoubG9nby1yantmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZjtmb250LXNpemU6Mi41cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmfQoubG9nby1zdWJ7Zm9udC1zaXplOjAuNjYzcmVtO2ZvbnQtd2VpZ2h0OjYwMDtsZXR0ZXItc3BhY2luZzouNGVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO21hcmdpbi10b3A6M3B4O3BhZGRpbmctYm90dG9tOjE0cHg7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4yKTttYXJnaW4tYm90dG9tOjIwcHh9Ci5ob21lLXJqe2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmO2ZvbnQtc2l6ZTozLjI1cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjF9Ci5sb2dvLXRhZ3tmb250LXNpemU6MC43NXJlbTtsZXR0ZXItc3BhY2luZzouMTJlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjtsaW5lLWhlaWdodDoxLjV9Ci5sb2dvLXJvd3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6ZmxleC1lbmQ7Z2FwOjEycHg7bWFyZ2luLWJvdHRvbToyOHB4O3BhZGRpbmctYm90dG9tOjE4cHg7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4yKX0KLmxvZ28taW1nLXJvd3ttYXJnaW4tYm90dG9tOjI4cHg7cGFkZGluZy1ib3R0b206MThweDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjIpfQoubG9nby1pbWd7aGVpZ2h0OjkwcHg7d2lkdGg6YXV0bztkaXNwbGF5OmJsb2NrfQouaG9tZS1nc3Vie2ZvbnQtc2l6ZTowLjY2M3JlbTtmb250LXdlaWdodDo2MDA7bGV0dGVyLXNwYWNpbmc6LjRlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjttYXJnaW4tdG9wOjZweDttYXJnaW4tYm90dG9tOjIycHh9Ci5ob21lLWgxe2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6My4xMjVyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNmZmZmZmY7bGluZS1oZWlnaHQ6MS4xO21hcmdpbi1ib3R0b206NnB4fQouaG9tZS1oMSBlbXtmb250LXN0eWxlOml0YWxpYztjb2xvcjojZmZmZmZmfQouaG9tZS1zdWJ7Zm9udC1zaXplOjAuOHJlbTtsZXR0ZXItc3BhY2luZzouMThlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjI4cHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5vcHR7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTZweDtwYWRkaW5nOjE2cHggMDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNyk7dGV4dC1kZWNvcmF0aW9uOm5vbmU7Y29sb3I6I2ZmZmZmZjt0cmFuc2l0aW9uOmNvbG9yIC4ycztjdXJzb3I6cG9pbnRlcjtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyLXRvcDpub25lO2JvcmRlci1sZWZ0Om5vbmU7Ym9yZGVyLXJpZ2h0Om5vbmU7d2lkdGg6MTAwJTtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWZ9Ci5vcHQ6aG92ZXJ7Y29sb3I6I2ZmZn0KLm9wdC1pY29ue3dpZHRoOjM4cHg7aGVpZ2h0OjM4cHg7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2ZsZXgtc2hyaW5rOjB9Ci5vcHQtaWNvbi1pbWd7d2lkdGg6MzhweDtoZWlnaHQ6MzhweDtvYmplY3QtZml0OmNvbnRhaW59Ci5vcHQtdGV4dHtmbGV4OjE7dGV4dC1hbGlnbjpsZWZ0fQoub3B0LXRpdGxle2ZvbnQtc2l6ZToxLjUxMnJlbTtmb250LXdlaWdodDo2MDA7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjJweDt0cmFuc2l0aW9uOmNvbG9yIC4ycztmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWZ9Ci5vcHQ6aG92ZXIgLm9wdC10aXRsZXtjb2xvcjojZmZmfQoub3B0LWhhbmRsZXtmb250LXNpemU6MC44ODdyZW07Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXdlaWdodDozMDB9Ci5vcHQtdGl0bGUtYm9va3tmb250LXNpemU6MS4zOHJlbTt3aGl0ZS1zcGFjZTpub3dyYXB9Ci5vcHQtaGFuZGxlLWJvb2t7Zm9udC1zaXplOjAuNzhyZW19Ci5vcHQtYXJyb3d7Y29sb3I6I2ZmZmZmZjtmb250LXNpemU6MS4yMjVyZW07ZmxleC1zaHJpbms6MDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLm9wdDpob3ZlciAub3B0LWFycm93e2NvbG9yOiNmZmZmZmZ9Ci5kaXZpZGVye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7cGFkZGluZzoxMnB4IDB9Ci5kaXZpZGVyOjpiZWZvcmUsLmRpdmlkZXI6OmFmdGVye2NvbnRlbnQ6Jyc7ZmxleDoxO2hlaWdodDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNil9Ci5kaXZpZGVyIHNwYW57Zm9udC1zaXplOjAuNjg4cmVtO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQouaG9tZS1mb290e21hcmdpbi10b3A6MzZweDtwYWRkaW5nLXRvcDoyMHB4O2JvcmRlci10b3A6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA2KTtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6Y2VudGVyfQouaG9tZS1mb290IHNwYW57Zm9udC1zaXplOjAuNzc1cmVtO2xldHRlci1zcGFjaW5nOi4yZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5mZG90e3dpZHRoOjJweDtoZWlnaHQ6MnB4O2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMTYpfQoucHJvZ3Jlc3N7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjttYXJnaW4tYm90dG9tOjQwcHg7b3ZlcmZsb3c6aGlkZGVuO2NvdW50ZXItcmVzZXQ6c3RlcH0KLnBze2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjVweDtmb250LXNpemU6MC42NjNyZW07bGV0dGVyLXNwYWNpbmc6LjEyZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7d2hpdGUtc3BhY2U6bm93cmFwO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2NvdW50ZXItaW5jcmVtZW50OnN0ZXB9Ci5wcy5kb25le2NvbG9yOiNmZmZmZmZ9Ci5wcy5hY3RpdmV7Y29sb3I6I2ZmZmZmZn0KLnBkb3R7d2lkdGg6MThweDtoZWlnaHQ6MThweDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7ZmxleC1zaHJpbms6MDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEyKTtmb250LXNpemU6MC42NjNyZW07Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC13ZWlnaHQ6NjAwfQoucGRvdDo6YmVmb3Jle2NvbnRlbnQ6Y291bnRlcihzdGVwLGRlY2ltYWwtbGVhZGluZy16ZXJvKX0KLnBzLmRvbmUgLnBkb3R7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLnBzLmFjdGl2ZSAucGRvdHtib3JkZXItY29sb3I6I2ZmZmZmZjtjb2xvcjojZmZmZmZmfQoucGx7ZmxleDoxO2hlaWdodDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNyk7bWFyZ2luOjAgNXB4O21pbi13aWR0aDo2cHh9Ci5wbC5kb25le2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMTgpfQouc3RlcHtkaXNwbGF5Om5vbmV9LnN0ZXAuc2hvd3tkaXNwbGF5OmJsb2NrO2FuaW1hdGlvbjpmdSAuMzVzIGVhc2UgYm90aH0KLnNsYmx7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjkzOHJlbTtmb250LXdlaWdodDo2MDA7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjIwcHg7bGV0dGVyLXNwYWNpbmc6LjAxZW19Ci5zYm94e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEwcHg7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTYpO3BhZGRpbmc6MCAycHg7dHJhbnNpdGlvbjpib3JkZXItY29sb3IgLjJzfQouc2JveDpmb2N1cy13aXRoaW57Ym9yZGVyLWJvdHRvbS1jb2xvcjojZmZmZmZmfQouc2l7b3BhY2l0eTouMjtmb250LXNpemU6MS4yMjVyZW07ZmxleC1zaHJpbms6MH0KI2JJbnB1dHtmbGV4OjE7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtvdXRsaW5lOm5vbmU7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjUxMnJlbTtjb2xvcjojZmZmZmZmO3BhZGRpbmc6MTJweCAwfQojYklucHV0OjpwbGFjZWhvbGRlcntjb2xvcjojZmZmZmZmfQouY2xye2JhY2tncm91bmQ6bm9uZTtib3JkZXI6bm9uZTtjb2xvcjojZmZmZmZmO2N1cnNvcjpwb2ludGVyO2ZvbnQtc2l6ZToxLjE1cmVtO2Rpc3BsYXk6bm9uZTtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZn0KLmNsci5zaG93e2Rpc3BsYXk6YmxvY2t9Ci5id3JhcHtwb3NpdGlvbjpyZWxhdGl2ZTttYXJnaW4tYm90dG9tOjIwcHh9Ci5kcm9we3Bvc2l0aW9uOmFic29sdXRlO2xlZnQ6MDtyaWdodDowO2JhY2tncm91bmQ6IzBmMGYwZjtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEpO2JvcmRlci10b3A6bm9uZTttYXgtaGVpZ2h0OjIwMHB4O292ZXJmbG93LXk6YXV0bzt6LWluZGV4OjUwO2Rpc3BsYXk6bm9uZX0KLmRyb3Aub3BlbntkaXNwbGF5OmJsb2NrfQouZGl0ZW17cGFkZGluZzoxMXB4IDE0cHg7Zm9udC1zaXplOjEuMzYzcmVtO2NvbG9yOiNmZmZmZmY7Y3Vyc29yOnBvaW50ZXI7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDUpO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLmRpdGVtOmhvdmVye2NvbG9yOiNmZmZ9Ci5kaXRlbSBtYXJre2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Y29sb3I6I2ZmZjtmb250LXdlaWdodDo3MDB9Ci5ub3Jlc3twYWRkaW5nOjE0cHg7Zm9udC1zaXplOjEuMjg4cmVtO2NvbG9yOiNmZmZmZmY7Zm9udC1zdHlsZTppdGFsaWM7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmfQoubm8tYnJlZWQtYmFubmVye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7cGFkZGluZzoxNHB4IDA7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDYpO2N1cnNvcjpwb2ludGVyO3RyYW5zaXRpb246Y29sb3IgLjJzO21hcmdpbi10b3A6NHB4fQoubm8tYnJlZWQtYmFubmVyOmhvdmVyIC5uby1icmVlZC1iYW5uZXItdGl0bGV7Y29sb3I6I2ZmZmZmZn0KLm5vLWJyZWVkLWJhbm5lci1pY29ue2ZvbnQtc2l6ZToxLjU3NXJlbTtmbGV4LXNocmluazowO29wYWNpdHk6LjN9Ci5uby1icmVlZC1iYW5uZXItdGV4dHtmbGV4OjF9Ci5uby1icmVlZC1iYW5uZXItdGl0bGV7Zm9udC1zaXplOjEuNDM4cmVtO2NvbG9yOiNmZmZmZmY7Zm9udC13ZWlnaHQ6NjAwO21hcmdpbi1ib3R0b206MnB4O2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLm5vLWJyZWVkLWJhbm5lci1zdWJ7Zm9udC1zaXplOjAuODg3cmVtO2NvbG9yOiNmZmZmZmY7bGluZS1oZWlnaHQ6MS41O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQoubm8tYnJlZWQtYmFubmVyLWFycm93e2NvbG9yOiNmZmZmZmY7Zm9udC1zaXplOjEuMjI1cmVtO2ZsZXgtc2hyaW5rOjA7dHJhbnNpdGlvbjpjb2xvciAuMnN9Ci5zYmFkZ2V7ZGlzcGxheTpub25lO2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTJweDttYXJnaW4tYm90dG9tOjIwcHh9Ci5zYmFkZ2Uuc2hvd3tkaXNwbGF5OmZsZXh9Ci5ibmFtZXtib3JkZXItYm90dG9tOjFweCBzb2xpZCAjZmZmZmZmO2NvbG9yOiNmZmZmZmY7cGFkZGluZzoycHggMDtmb250LXNpemU6MS40MzhyZW07Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmfQouYmNoZ3tmb250LXNpemU6MC44cmVtO2NvbG9yOiNmZmZmZmY7Y3Vyc29yOnBvaW50ZXI7bGV0dGVyLXNwYWNpbmc6LjEyZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO3RyYW5zaXRpb246Y29sb3IgLjJzfQouYmNoZzpob3Zlcntjb2xvcjojZmZmZmZmfQouc3ZidG57ZGlzcGxheTpibG9jaztwYWRkaW5nOjA7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wOCk7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Y3Vyc29yOnBvaW50ZXI7dGV4dC1hbGlnbjpsZWZ0O3RyYW5zaXRpb246Ym9yZGVyLWNvbG9yIC4yczt3aWR0aDoxMDAlO292ZXJmbG93OmhpZGRlbjtwb3NpdGlvbjpyZWxhdGl2ZX0KLnN2YnRuOmhvdmVye2JvcmRlci1ib3R0b20tY29sb3I6I2ZmZmZmZn0KLnN2YnRuLmFjdGl2ZXtib3JkZXItYm90dG9tLWNvbG9yOiNmZmZmZmZ9Ci5zdnB7Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNmZmZmZmY7ZmxleC1zaHJpbms6MH0KLndoZWVsLXdyYXB7cG9zaXRpb246cmVsYXRpdmU7aGVpZ2h0OjIyMHB4O21hcmdpbjoyNHB4IDAgMTZweDtvdmVyZmxvdzpoaWRkZW59Ci53aGVlbC1oaWdobGlnaHR7cG9zaXRpb246YWJzb2x1dGU7dG9wOjUwJTtsZWZ0OjA7cmlnaHQ6MDtoZWlnaHQ6NDRweDt0cmFuc2Zvcm06dHJhbnNsYXRlWSgtNTAlKTtib3JkZXItdG9wOjFweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjM1KTtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjM1KTtwb2ludGVyLWV2ZW50czpub25lO3otaW5kZXg6MTtiYWNrZ3JvdW5kOnJnYmEoMjAxLDE2OCw3NiwuMDQpfQoud2hlZWx7aGVpZ2h0OjEwMCU7b3ZlcmZsb3cteTpzY3JvbGw7c2Nyb2xsLXNuYXAtdHlwZTp5IG1hbmRhdG9yeTtkaXNwbGF5OmZsZXg7ZmxleC1kaXJlY3Rpb246Y29sdW1uOy13ZWJraXQtb3ZlcmZsb3ctc2Nyb2xsaW5nOnRvdWNoO3Njcm9sbGJhci13aWR0aDpub25lfQoud2hlZWw6Oi13ZWJraXQtc2Nyb2xsYmFye2Rpc3BsYXk6bm9uZX0KLndoZWVsLXBhZHtoZWlnaHQ6ODhweDtmbGV4LXNocmluazowO29yZGVyOi0xfQoud2hlZWwtcGFkOmxhc3QtY2hpbGR7b3JkZXI6OTk5fQoubWJ0bntoZWlnaHQ6NDRweDtmbGV4LXNocmluazowO3Njcm9sbC1zbmFwLWFsaWduOmNlbnRlcjtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7Y3Vyc29yOnBvaW50ZXI7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtwYWRkaW5nOjA7dHJhbnNpdGlvbjpvcGFjaXR5IC4xNXN9Ci5tbmFtZXtmb250LXNpemU6MS4xNXJlbTtmb250LXdlaWdodDo1MDA7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuMzUpO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjt0cmFuc2l0aW9uOmNvbG9yIC4xNXMsZm9udC1zaXplIC4xNXMsZm9udC13ZWlnaHQgLjE1c30KLm1idG4ud2hlZWwtYWN0aXZlIC5tbmFtZXtmb250LXNpemU6MS42cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmfQoud2hlZWwtY29uZmlybXtkaXNwbGF5OmJsb2NrO3dpZHRoOjEwMCU7YmFja2dyb3VuZDpub25lO2JvcmRlcjoxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC40KTtjb2xvcjojYzlhODRjO3BhZGRpbmc6MTRweDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXNpemU6MC44NXJlbTtsZXR0ZXItc3BhY2luZzouMDVlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y3Vyc29yOnBvaW50ZXI7dHJhbnNpdGlvbjphbGwgLjJzO21hcmdpbi1ib3R0b206OHB4fQoud2hlZWwtY29uZmlybTphY3RpdmV7dHJhbnNmb3JtOnNjYWxlKDAuOTcpO2JhY2tncm91bmQ6cmdiYSgyMDEsMTY4LDc2LC4wOCl9Ci5tdGl0bGV7Zm9udC1zaXplOjAuOHJlbTtjb2xvcjojZmZmZmZmO21hcmdpbi10b3A6M3B4O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQouZ2J0bntkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO3BhZGRpbmc6MTRweCAwO2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOm5vbmU7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDgpO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjQzOHJlbTtjdXJzb3I6cG9pbnRlcjt3aWR0aDoxMDAlO3RyYW5zaXRpb246YWxsIC4yc30KLmdidG46aG92ZXJ7Y29sb3I6I2ZmZmZmZn0KLmdidG4uYWN0aXZle2NvbG9yOiNmZmZmZmY7Ym9yZGVyLWJvdHRvbS1jb2xvcjojZmZmZmZmfQouY2FsLWh7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2FsaWduLWl0ZW1zOmNlbnRlcjttYXJnaW4tYm90dG9tOjE2cHh9Ci5jYWwtbXtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjEuOTM4cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmfQouY2FsLW57YmFja2dyb3VuZDpub25lO2JvcmRlcjpub25lO2NvbG9yOiNmZmZmZmY7Y3Vyc29yOnBvaW50ZXI7Zm9udC1zaXplOjEuNTc1cmVtO3BhZGRpbmc6NHB4IDhweDt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLmNhbC1uOmhvdmVye2NvbG9yOiNmZmZmZmZ9Ci5jZ3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCg3LDFmcik7Z2FwOjJweDttYXJnaW4tYm90dG9tOjEycHh9Ci5jZG57dGV4dC1hbGlnbjpjZW50ZXI7Zm9udC1zaXplOjAuNjYzcmVtO2NvbG9yOiNmZmZmZmY7cGFkZGluZzo0cHggMDt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7bGV0dGVyLXNwYWNpbmc6LjFlbX0KLmNke3RleHQtYWxpZ246Y2VudGVyO2N1cnNvcjpwb2ludGVyO2NvbG9yOiNmZmZmZmY7Ym9yZGVyOjFweCBzb2xpZCB0cmFuc3BhcmVudDt0cmFuc2l0aW9uOmFsbCAuMnN9Ci5jZDpob3Zlcjpub3QoLmRpcyk6bm90KC5wYWQpIC5jZC1pbm5lcntiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA3KSFpbXBvcnRhbnQ7Y29sb3I6I2ZmZmZmZiFpbXBvcnRhbnR9Ci5jZC5zZWwgLmNkLWlubmVye2JhY2tncm91bmQ6I2ZmZmZmZiFpbXBvcnRhbnQ7Y29sb3I6IzBhMGEwYSFpbXBvcnRhbnQ7Zm9udC13ZWlnaHQ6NzAwIWltcG9ydGFudDtib3JkZXI6bm9uZSFpbXBvcnRhbnR9Ci5jZC50b2QgLmNkLWlubmVye2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMjgpO2NvbG9yOiNmZmZ9Ci5jZC5kaXN7Y29sb3I6I2ZmZmZmZjtjdXJzb3I6ZGVmYXVsdH0KLnRne2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDQsMWZyKTtnYXA6MXB4O2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDcpfQoudGJ0bntiYWNrZ3JvdW5kOiMwYTBhMGE7Ym9yZGVyOm5vbmU7cGFkZGluZzoxM3B4IDRweDt0ZXh0LWFsaWduOmNlbnRlcjtmb250LXNpemU6MS4zMjVyZW07Y29sb3I6I2ZmZmZmZjtjdXJzb3I6cG9pbnRlcjtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7dHJhbnNpdGlvbjphbGwgLjJzfQoudGJ0bjpob3Zlcntjb2xvcjojZmZmZmZmO2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDQpfQoudGJ0bi5hY3RpdmV7Y29sb3I6I2ZmZmZmZn0KLnN1bXtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjpub25lO2JvcmRlci10b3A6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA4KTtwYWRkaW5nOjIwcHggMDttYXJnaW4tYm90dG9tOjIwcHh9Ci5zcntkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47cGFkZGluZzo4cHggMDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNSk7Zm9udC1zaXplOjEuMzYzcmVtO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZn0KLnNyOmxhc3QtY2hpbGR7Ym9yZGVyLWJvdHRvbTpub25lO3BhZGRpbmctdG9wOjE0cHh9Ci5zbHtjb2xvcjojZmZmZmZmfS5zdntjb2xvcjojZmZmZmZmO3RleHQtYWxpZ246cmlnaHR9Ci5zcHtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjIuNDM4cmVtO2NvbG9yOiNmZmZmZmY7Zm9udC13ZWlnaHQ6NjAwfQouZmd7bWFyZ2luLWJvdHRvbToyMHB4fQouZmx7Zm9udC1zaXplOjAuNzEycmVtO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO21hcmdpbi1ib3R0b206OHB4O2Rpc3BsYXk6YmxvY2s7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5maXt3aWR0aDoxMDAlO2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOm5vbmU7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTQpO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjUxMnJlbTtwYWRkaW5nOjEwcHggMDtvdXRsaW5lOm5vbmU7dHJhbnNpdGlvbjpib3JkZXItY29sb3IgLjJzfQouZmk6Zm9jdXN7Ym9yZGVyLWJvdHRvbS1jb2xvcjojZmZmZmZmfQouY2J0bntkaXNwbGF5OmJsb2NrO3dpZHRoOjEwMCU7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZTowLjg2MnJlbTtmb250LXdlaWdodDo2MDA7bGV0dGVyLXNwYWNpbmc6LjI4ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO3RleHQtYWxpZ246Y2VudGVyO3BhZGRpbmc6MTZweDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjI1KTtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmFsbCAuMnN9Ci5jYnRuOmhvdmVye2JvcmRlci1jb2xvcjojZmZmZmZmO2NvbG9yOiNmZmZmZmZ9Ci5zYmxvY2t7dGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzo1MnB4IDIwcHg7ZGlzcGxheTpub25lfQouc2Jsb2NrLnNob3d7ZGlzcGxheTpibG9jazthbmltYXRpb246ZnUgLjVzIGVhc2UgYm90aH0KLnNpMntmb250LXNpemU6My42cmVtO21hcmdpbi1ib3R0b206MjBweDtvcGFjaXR5Oi40fQouc3R7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToyLjcyNXJlbTtjb2xvcjojZmZmZmZmO21hcmdpbi1ib3R0b206MTBweDtmb250LXdlaWdodDo2MDB9Ci5zc3tmb250LXNpemU6MS4wNzVyZW07Y29sb3I6I2ZmZmZmZjtsaW5lLWhlaWdodDoxLjk7bWFyZ2luLWJvdHRvbToyOHB4O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQouaGJ0bntiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTYpO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC1zaXplOjAuODYycmVtO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtwYWRkaW5nOjEzcHggMjhweDtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmFsbCAuMnN9Ci5oYnRuOmhvdmVye2JvcmRlci1jb2xvcjojZmZmZmZmO2NvbG9yOiNmZmZmZmZ9Ci5sb2FkaW5nLXNsb3Rze2NvbG9yOiNmZmZmZmY7Zm9udC1zaXplOjEuMjg4cmVtO3BhZGRpbmc6MTJweCAwO3RleHQtYWxpZ246Y2VudGVyO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXN0eWxlOml0YWxpY30KLmNke2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2FsaWduLWl0ZW1zOmNlbnRlcjtoZWlnaHQ6MzZweCFpbXBvcnRhbnQ7cGFkZGluZzowIWltcG9ydGFudH0KLmNkLWlubmVye3dpZHRoOjMycHg7aGVpZ2h0OjMycHg7Ym9yZGVyLXJhZGl1czowO2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OmNlbnRlcjtmb250LXNpemU6MS4xNXJlbTtjdXJzb3I6cG9pbnRlcjtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWZ9Ci5jZC5hdmFpbCAuY2QtaW5uZXJ7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDkwLDE4MCw5MCwuMzUpO2NvbG9yOnJnYmEoOTAsMTgwLDkwLC42NSl9Ci5jZC5idXN5IC5jZC1pbm5lcntib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA3KTtjb2xvcjojZmZmZmZmfQouY2Quc2VsIC5jZC1pbm5lcntiYWNrZ3JvdW5kOiNmZmZmZmYhaW1wb3J0YW50O2NvbG9yOiMwYTBhMGEhaW1wb3J0YW50O2ZvbnQtd2VpZ2h0OjcwMCFpbXBvcnRhbnQ7Ym9yZGVyOm5vbmUhaW1wb3J0YW50fQouY2QudG9kIC5jZC1pbm5lcntib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjI4KTtjb2xvcjojZmZmO2ZvbnQtd2VpZ2h0OjYwMH0KLmNkLmRpcyAuY2QtaW5uZXJ7Y29sb3I6I2ZmZmZmZjtjdXJzb3I6ZGVmYXVsdDtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyOm5vbmV9Ci5zdmJ0bi1yb3d7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2FsaWduLWl0ZW1zOmJhc2VsaW5lO21hcmdpbi1ib3R0b206NnB4O3BhZGRpbmc6MTZweCAwIDB9Ci5zdmJ0bi1uYW1le2ZvbnQtc2l6ZToxLjUxMnJlbTtjb2xvcjojZmZmZmZmO2ZvbnQtd2VpZ2h0OjYwMDtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWZ9Ci5zdmJ0bi5hY3RpdmUgLnN2YnRuLW5hbWV7Y29sb3I6I2ZmZmZmZn0KLnN2YnRuLXByaWNle2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6MS43MjVyZW07Y29sb3I6I2ZmZmZmZjtmb250LXdlaWdodDo2MDA7ZmxleC1zaHJpbms6MH0KLnN2YnRuLmFjdGl2ZSAuc3ZidG4tcHJpY2V7Y29sb3I6I2ZmZmZmZn0KLnN2YnRuLWRlc2N7Zm9udC1zaXplOjFyZW07Y29sb3I6I2ZmZmZmZjtsaW5lLWhlaWdodDoxLjc7ZGlzcGxheTpibG9jaztwYWRkaW5nOjAgMCAxNHB4O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO3doaXRlLXNwYWNlOnByZS1saW5lfQouc3ZidG4uYWN0aXZlIC5zdmJ0bi1kZXNje2NvbG9yOiNmZmZmZmZ9Ci5zdmJ0bi10YWd7Zm9udC1zaXplOjAuOTc1cmVtO2NvbG9yOiNmZmZmZmY7Zm9udC1zdHlsZTppdGFsaWM7ZGlzcGxheTpibG9jazttYXJnaW4tdG9wOjJweDtwYWRkaW5nOjAgMCAxNHB4O2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZn0KLnN2YnRuLmFjdGl2ZSAuc3ZidG4tdGFne2NvbG9yOiNmZmZmZmZ9CkBtZWRpYShtYXgtd2lkdGg6NDAwcHgpey5zdmJ0bi1uYW1le2ZvbnQtc2l6ZToxLjM2M3JlbX0uc3ZidG4tcHJpY2V7Zm9udC1zaXplOjEuNTEycmVtfS5zdmJ0bi1kZXNje2ZvbnQtc2l6ZTowLjkzOHJlbX0uc3ZidG4tdGFne2ZvbnQtc2l6ZTowLjg4N3JlbX19CkBrZXlmcmFtZXMgZnV7ZnJvbXtvcGFjaXR5OjA7dHJhbnNmb3JtOnRyYW5zbGF0ZVkoMTBweCl9dG97b3BhY2l0eToxO3RyYW5zZm9ybTp0cmFuc2xhdGVZKDApfX0KLmxhbmctYmFye3Bvc2l0aW9uOmZpeGVkO3RvcDoxMnB4O3JpZ2h0OjE0cHg7ei1pbmRleDo5OTk7ZGlzcGxheTpmbGV4O2dhcDo2cHh9Ci5sYW5nLWJ0bntiYWNrZ3JvdW5kOnJnYmEoMTAsMTAsMTAsLjkyKTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEpO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC1zaXplOjAuNzc1cmVtO2xldHRlci1zcGFjaW5nOi4xNWVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtwYWRkaW5nOjVweCAxMHB4O2N1cnNvcjpwb2ludGVyO3RyYW5zaXRpb246YWxsIC4yc30KLmxhbmctYnRuOmhvdmVye2JvcmRlci1jb2xvcjojZmZmZmZmO2NvbG9yOiNmZmZmZmZ9Ci5sYW5nLWJ0bi5hY3RpdmV7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLmNiay1idG57YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjE0KTtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZTowLjg2MnJlbTtsZXR0ZXItc3BhY2luZzouMTZlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7cGFkZGluZzoxMnB4IDIwcHg7Y3Vyc29yOnBvaW50ZXI7dHJhbnNpdGlvbjphbGwgLjJzO3dpZHRoOjEwMCV9Ci5jYmstYnRuOmhvdmVye2JvcmRlci1jb2xvcjojZmZmZmZmO2NvbG9yOiNmZmZmZmZ9Ci5tYnRuLC5zdmJ0biwuZ2J0biwudGJ0biwuY2J0biwuaGJ0biwuY2JrLWJ0biwubGFuZy1idG4sLmJhY2stYnRuLC5vcHQsLmRpdGVtLC5jZCwubm8tYnJlZWQtYmFubmVyLC5iY2hne3RyYW5zaXRpb246YWxsIC4xNXMgZWFzZX0KLm1idG46YWN0aXZlLC5zdmJ0bjphY3RpdmUsLmdidG46YWN0aXZlLC50YnRuOmFjdGl2ZSwuY2J0bjphY3RpdmUsLmhidG46YWN0aXZlLC5jYmstYnRuOmFjdGl2ZSwubGFuZy1idG46YWN0aXZlLC5iYWNrLWJ0bjphY3RpdmUsLm9wdDphY3RpdmUsLmRpdGVtOmFjdGl2ZSwuY2Q6YWN0aXZlLC5uby1icmVlZC1iYW5uZXI6YWN0aXZlLC5iY2hnOmFjdGl2ZXt0cmFuc2Zvcm06c2NhbGUoMC45Nil9Cjwvc3R5bGU+CjwvaGVhZD4KPGJvZHk+CjxhIGhyZWY9Ii9hZG1pbj9wYXNzPWFuemExOTg1IiBpZD0iYWRtaW5CYWNrTGluayIgc3R5bGU9ImRpc3BsYXk6bm9uZTtwb3NpdGlvbjpmaXhlZDt0b3A6MTRweDtyaWdodDoxNHB4O2ZvbnQtc2l6ZTowLjlyZW07Y29sb3I6I2M5YTA1YTt0ZXh0LWRlY29yYXRpb246bm9uZTt6LWluZGV4Ojk5OTtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtiYWNrZ3JvdW5kOnJnYmEoMTAsMTAsOSwuODUpO3BhZGRpbmc6NnB4IDEycHg7Ym9yZGVyLXJhZGl1czoyMHB4O2JvcmRlcjoxcHggc29saWQgcmdiYSgyMDEsMTYwLDkwLC4zNSkiPuKGkCDQkNC00LzQuNC9LdC/0LDQvdC10LvRjDwvYT4KPHNjcmlwdD5pZihsb2NhdGlvbi5zZWFyY2guaW5kZXhPZigncGFzcz1hbnphMTk4NScpIT09LTEpe2RvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdhZG1pbkJhY2tMaW5rJykuc3R5bGUuZGlzcGxheT0nYmxvY2snO308L3NjcmlwdD4KPGRpdiBjbGFzcz0ibGFuZy1iYXIiPgogIDxidXR0b24gY2xhc3M9ImxhbmctYnRuIiBvbmNsaWNrPSJzZXRMYW5nKCdldCcpIj5FVDwvYnV0dG9uPgogIDxidXR0b24gY2xhc3M9ImxhbmctYnRuIiBvbmNsaWNrPSJzZXRMYW5nKCdlbicpIj5FTjwvYnV0dG9uPgogIDxidXR0b24gY2xhc3M9ImxhbmctYnRuIGFjdGl2ZSIgb25jbGljaz0ic2V0TGFuZygncnUnKSI+UlU8L2J1dHRvbj4KPC9kaXY+Cgo8IS0tIEhPTUUgLS0+CjxkaXYgY2xhc3M9InNjcmVlbiBhY3RpdmUiIGlkPSJob21lU2NyZWVuIj4KPGRpdiBjbGFzcz0iY29uIj4KICA8ZGl2IGNsYXNzPSJsb2dvLWltZy1yb3ciPgogICAgPGltZyBzcmM9ImRhdGE6aW1hZ2UvcG5nO2Jhc2U2NCxpVkJPUncwS0dnb0FBQUFOU1VoRVVnQUFBVU1BQUFEckNBWUFBQUR6Qy9Rd0FBQUJXR2xEUTFCSlEwTWdVSEp2Wm1sc1pRQUFlSng5a0xGTHcxQVF4cjlXcGFCMUVCMGNIREtKUTVTU0NybzR0QlZFY1FoVndlcVV2cWFwa01aSGtpSUZOLytCZ3YrQkNzNXVGb2M2T2pnSW9wUG81dVNrNEtMbGVTK0pwQ0o2aitOK2ZPKzc0emdnT1c1d2J2Y0RxRHUrVzF6S0s1dWxMU1gxakFTOUlBem04Wnl1cjByK3JqL2ovVDcwM2s3TFdiLy8vNDNCaXVreHFwK1VHY1pkSDBpb3hQcWV6eVh2RTQrNXRCUnhTN0lWOG9ua2Nzam5nV2U5V0NDK0psWll6YWdRdnhDcjVSN2Q2dUc2M1dEUkRuTDd0T2xzck1rNWxCTll4QTQ4Y05ndzBJUUNIZGsvL0xPQnY0QmRjamZoVXArRkduenF5WkVpSjVqRXkzREFNQU9WV0VPR1VwTjNqdTUzRjkxUGpiV0RKMkNoSTRTNGlMV1ZEbkEyUnlkcng5clVQREF5QkZ5MXVlRWFnZFJIbWF4V2dkZFRZTGdFak41UXo3Wlh6V3JoOXVrOE1QQW94TnNra0RvRXVpMGhQbzZFNkI1VDh3Tnc2WHdCQTZkaUU4SFlXaE1BQUVId1NVUkJWSGljN1oxNWZGVkZzdmpyM0RYN0JvUWxRQWliS0FJK1VGQnhYMUFad0hGNUlpQlBIUmNlRGk3b3FQaFRSbEZBUWNWUlVaOFBVWEhVSitMbzRLNkFBczY0b09ER0loRENrb1JBOXZWdVo2bmZIMWhObjc3bkpqY1FJSUg2Zmo3NTNDWG5kdmZwYzdwT1ZWZDFOUURETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUV6YlFEdlNEVGhhY0xsY2dJaWdhUnBZbGdWdXR4dE0wd1JOTzdndVJzU29lZ0FBTE1zQ0FCRGwwM0dhcG9rMk1DMlB4K01Cd3pBTytycktJQ0s0WEM1eHplVDNETk5tSU9Fa28ya2FlRHllRmluZjQvR0EyKzJPV1U4c1dySU54em91bDB2MHY5dnRCcmZiM2FMbGE1b0dicmNiZkQ2ZitLNmw2MkNhaGpYREZzRG44MEVrRXJGOVJ4cWFxdGsxRi9uM1RscWcvSC9XS0E0ZGJyY2JFRkg4a1NYUUVxalgwK3YxZ3E3ckxWSTJ3eHhXVkEzTVNZczdVSHcrbjAwRGJFb2JkTGxjTFdyQ0hldDRQQjdSbjNLL3RxVFdyVjR2K3N6YUlkT21rTTBud3V2MUNzSFVrcWpDamw1bE0wNyt6RUt4WlpDdnJjdmxhbkVoSlpkSHBqSlBjVEJ0RWxVUXRqUWtCRlhoR3EvR3lCdzRkRDFwMmdOZy8vVnVpWWRkckxMNGVqSnREdldtOWZ2OUFHQTNydzYyZkxVY1dldVRKOTNwZjdIYXhod1lxaVlZeTZGMUlMaGNMcUVGVWoyeUFHWU9IOXpiTFlEUDU0T0hIbm9JTzNmdURKRklwRVhOS0RsY3h6QU0wSFVkNnVycW9LU2tCUGJ1M1F1Yk5tM1NTa3BLb0xLeUVnRHNUcFNXY09BYzY1eCsrdWs0YWRJa0FBQ0lSQ0xnOVhvaEhBNkQyKzBHeTdJT1dpZ2FoZ0VWRlJYdzhNTVBhK0Z3V0h6UHpqQ21UWktZbUFqcjE2L0hjRGlNaG1FZ0lxS3U2MmhaRnBxbWlZZ29YdW4vaEs3cmlJaG9XUlphbG1YN0gzMm1WL20zOHZ0SUpJS0dZZURhdFd2eGpqdnV3THk4UEVoTlRZM1NFdWt6dlI0T3pjT3BMbnBZMEt1VDlpc2pQMXpvMkVNeEordkV1SEhqUkQrcjExS0Zyb2w2amVWcktGOXorbjc5K3ZXWWtwSWlOTVREY1Y1TU5Ld1p0Z0ErbncrbVRwMkthV2xwb0drYUpDVWxRYnQyN1dEdzRNRnd3Z2tuZ0dWWnRtQnNYZGZCNi9VS3JZOCtrNlpCbW9mSDR4SGZtYVlwaEVJa0VnR2Z6MmZUVE9UM2htSEErdlhyNGFXWFhvSVZLMVpvQlFVRkl2UkhEcytod1BCRERZV0tPR21xOUoyc0NkRjVhNW9tMnFlR0ZjbS9QWlNjY2NZWk9ISGlSUEI0UE9EeitTQTNOeGNHRHg0TUNRa0o0aGpxUzFXZ3F5RXpkSzIvL2ZaYktDZ29BRjNYSVNFaEFZcUtpdURoaHgvV2dzSGdZVDAzeGc0THc0T0VCQXFaVFhRRGV6d2VPTzY0NCtDaWl5N0NKNTk4MGliTUFFQUlRSHF0cmEyRjFOUlUyTGx6SjJ6Y3VCSFdyVnNIUlVWRlVGMWRMY3BNU2txQ3JsMjd3cm5ubmd0bm5ubW16ZU9vYVpwTmlBYURRZkQ3L2ZEenp6L0QwcVZMWWZiczJacXU2N2I0dU1NeDJOUkJUU3M0NUw2ajcwM1RqR3MxQnMyWkhnNUJUbTBEMlBlUXljdkxnK09QUHg1bnpKZ0JKNTk4c2ppR2hKMFRkTzMzN05rRE45eHdBMnpac2tVckxDeUVTQ1FDaUdpTFU2VitvUE5tZ2NpMEtXS1pOYlI2NVBiYmIwZlROREVjRHR0TUxOa01ycXlzeEI0OWVvRFA1NFBFeEVRQTJDZElhREpkRHJsd3U5MlFrWkVCTTJiTVFFVEVRQ0JnSzlNMFRadlpIUWdFY04yNmRhaWF6b2Nqam8wRWlXeldxbUVqOG1jbkI1QzYra00xOXc4bGNoMnlvOFB2OThPTEw3NklvVkRJWmc3TDB4NnlTVjFXVm9iZHVuV0RwS1FrVWE3c0NDUEhHOE8wYVdpUXlzSkZuZzg3NFlRVFlOMjZkYlpCSXd0RjB6U3hyS3dNMjdWckozNURaWGk5WHR1OEd3MGdlczNPem9hMzMzNGJxNnVyMGJJc0RJZkRVZVZibG9XR1llRG5uMytPZmZ2MkJZQWpNL2pjYmpla3A2ZkRxYWVlaXVQR2pjUFhYMzhkdDI3ZGluVjFkVUo0UkNJUkxDc3J3dzgvL0JDdnZ2cHFIRHg0TUFMWVBlaXFrRHhjYlZmbk92djE2d2ZmZmZlZHJiOXB2cEFlU0lpSTRYQVlyNzMyMnFqekFJQ29lVUphbWtmdkdhYk40QlFmUmplMEhQN3kwVWNmb1dFWXFPdTZHRGcwV0hSZHgrcnFhdXpjdWJNb1EzNXRyQzZQeHdOcGFXa3djZUpFcksrdmp4SzRobUhZQnVxeVpjc3dLeXZyc0EwME9UN3l6My8rTTc3eHhoczI0VWRFSXBFb1I1RmxXYmhqeHc1OC9QSEhNVGMzMXlZa1dpcDBxU25rNjZnNmdkeHVOenoxMUZNWURvZWpuRjB5di83Nkt4NTMzSEUyamQvcFZkV1llUVVLMHlaeDhvaktOL05ycjcwV3BRMlNTV1dhSmxaV1ZtS0hEaDFFV1RLMG9nWEFXVU1DMkNkd1R6dnROQXlIdzFHbW1teStSU0lSWExkdTNXR2JpSEs1WE5DcFV5ZFl2SGd4VmxkWEMwMkoyb0s0ejRUOCt1dXY4ZXV2dnhiSDBQK29uOWF1WFlzbm4zd3lIb21WTmVvS0ZKbHJycm5HWmlxcm5tYkxzbkRGaWhWSWdoREFua1JEZmxqd01qem1xRUVObWdYWVAzam16WnRuRTBwcW1FWkZSUVcyYTlmT2NhVURRTFN3ZFhydjlYcGgvUGp4Mk5EUVlCdVVWQ2RwWE1GZ0VLZFBuNDZIdzh6TXpzNkd6ejc3VEFnM0VoQ2hVQWkvLy81N1BQUE1NMUVPUFBiNy9mRElJNDlnVFUyTnplUkVSQ3dvS01DY25CeFI5dUVVR3JLSkxEK1loZzRkaXJXMXRlTGM1TEFhYXZmU3BVdFJuak5WelcyQTZDeERiQ0l6UngzMDVKODJiWnBOR0toVVZGUUl6WkJvem9DZ2daYWFtZ3BMbHk2MXpWblJ3SlEvLy9EREQyTCs4R0NGaXF5NUVtNjNHeElTRXVDZi8vd25tcVpwRTRhSWlNODk5eHdtSmliR1hNbzRhZEtrS0xNL0VvbmdxbFdyc0RVNUcvTHk4cUM2dXRvbUJOWHJ1M2p4WWdTd081RlkrMk9PT1VpanUvZmVlMjJEUlIwd0J5b015YnNNQUNLczV1U1RUeFphaW1xMmtVblgwTkNBRXlaTXNHa3NWQ2Vab3MzVlRtU2hxR2thVEo4K1BhcCswelJ4eVpJbG1KMmRiZnN0ZWN2cFhESXlNbURSb2tXMk5wUEF1Zm5tbTF1TlFPemV2VHRVVlZYRkRNUkdSRnkwYUpFUWhrZkMrY1BFQjErUlF3eitudi91VU1XTVVkWmxpdGt6VFJQV3JsMnJGUllXMnNKUUtGYlA1L01CSWtKU1VoS01HemRPQkh2TFFjMTBiSFBicSt1NmlMZExUMCtITVdQR2lQb2prUWhZbGdXV1pjRVhYM3dCcGFXbHRyQWJpck9qN09EVjFkV3daczBhQ0lmRDRQZjdiY3NjeDR3WjAycnkvY25uckVMWHZxR2h3ZmFkL01xMEhsZ1lIZ1ZRRURQQS9nSDR3QU1QQ0FFanI0Q2hJR2pETU9DY2M4NFJHcG5xbUdqT1lGVmpDTDFlTHd3Yk5neTdkKzh1eXZINWZPQnl1V0RQbmozdzAwOC9nYVpwUXBDclNTY1FFUklTRXVDSEgzNkFQWHYyaUhMcDNEcDI3QWpaMmRtdFlsN05NQXpidzhRSmVXVUp3Y0t3OWNIQzhBaHdJQ1pvVTJYUktnZVh5d1UrbncrV0xGbWl5Y0tGNnFObGV4NlBCNUtUazRGaUcxWE50VGxtbkZ3K0lrSWtFb0grL2ZzRE9ZUklXQ01pMU5YVlFYVjF0WVpTdG1oYXBpaG5DdytGUXJCMTYxYXRxcXJLVm9lbWFaQ2FtaXJhZmFScFNxalJFcnpXSUxpWnhtRmgyTVlob1FLd3o4U2s3RGJCWUJBYUdocUVvQUhZci8zUkFMWXNDM0p5Y2xCTllYOGc4MWxrM3RJU09aL1BKMHgzZVJPbHpNeE02Tnk1TTFMOVRyR1VWRDlsY2FGekNJVkNBQURRME5BZ2xySWRhWnpXVEt2L2s5K3pVR3k5c0RCczQ2akpGdVRVVXNGZ1VBdyswczVJR0pJUVRVdExzNVZINXZUQnBJOVNOemFTTmNmMjdkdkRzR0hEeFA5bFFVN0pLK1NFRFhRTW1jNldaVUZEUXdPVWxKUzBDbytzVS9DM0toaGxnY2xDc2ZYQ3d2QXdvUTZRbHRKcWFGRS9RTFJnRElWQ1F2akpjMjV5OGdOZDEyMUpFV1RpRVRieTRLYkJIb2xFUU5mMUtDRk43UmcyYkJoUXZLRGNmc013UkFZWUFJQ2VQWHNpQ1dzUzBvZ0lPM2Z1aElhR2hzT1dxS0V4cUsyeHJpLzFDWDFtWWRoNllXRjRpSWwxODdma25HRXM3eStsbVNLdFVON0htWTR0TEN6VVNBTWpZU05yYTAwaEQzWjUwTmZWMVVFZ0VCQnRwSGt6eTdMZzRvc3Zodjc5K3lNSkVsa1RsUHZsekRQUEJGcWlLQXZ4Ung1NVJDUEJLdmREWXl1QTVHUGtQenBPWFNQc2xKTExpYWEyY1ZYYlJkZWdLYWNMYy9oaFlkakdJZk1SWUw5emhBWlpXbG9hR0lZaGdwa3BQWlJsV2VEeGVDQWNEa05GUllYTmhIWjZiUXpTK0tndEpJQysrZVliMkxObkR4aUdJVXhnZVQ3eG80OCtndFRVVkFEWUgyeE5tcUZwbXRDaFF3YzQ1NXh6SURFeFVXaXZpQWpUcDArSGdvSUNjYnc4eDBqdElNR21DblBTak9VL09vNk9wZmxKU2ljV3ovbXpVRHM2WUdIWXh0RTBUVGdXL0g2LzBNNHlNelBCNC9HQXJQWFI4UzZYQ3d6RGdHM2J0a0Z0YmEzNFh2WTR4d3Q1c1ZVQlZGQlFvTzNldlRzcUtGeDI0bnoyMldlWWw1Y0hjcDVGRWk1VHBrekJVYU5HaVgyRUE0RUFQUC84OC9ERUUwOW9zb0NYNXpmbCtWRjVpVitzZnFQNFRBQzdJMG8xYVJ2alVHd0F4aHdaV0JpMmNVaFlBT3p6dnRJQUhqdDJiTlRhWTFub3VGd3VlT3V0dDRTNUpzOGpOa2NZMFBFa2dBRDJtWm9ORFEyd1lNRUM0ZldWdFRUTHNzRHI5Y0pKSjUwRVU2ZE94YXlzTE51ODVZSUZDL0N2Zi8ycjBEcHJhbXBneG93WjhOQkREMmxVRGdWZGsya3RhM3B5bXhwck03VkxGb2prZ0pKTi9zYncrLzE0cUtaQUdPYW9nZ2JHM1hmZjdiZ21tVGpRNVhoMGpKb2d0YkN3VUN6SGs5Y2wweHJoNnVwcXZQamlpNk9XaWNsbHh1dEFrWmVZeVdhNjIrMkc4dkp5a2FVR01YcC9FTXV5OEo1NzdrR1h5d1VEQmd6QTlldlhvNjdyWWdsZVFVRUJwcWFtUnMwbk9xMkhsdHZzbEJBMlZzWWJ0YzFxZnpiR0thZWNnclcxdFk3WmFvakhIMzhjbmZxWGhXYnJnalhETmc1cEw2UUZlVHdlT1BmY2M3RnIxNjQySVVDYWtOZnJoVWdrQXUrLy96NTg5ZFZYR3BYaHBBM0c2NjJsY3Nsa0plZUdhWnB3NG9rbmFpVWxKV0lWaGl4Z0xjc0MwelJoenB3NXNHclZLdnpsbDErZ1g3OStVRjlmRDZ0WHI0WS8vZWxQMEtkUEh5MFlESXJ6SS9PWGxzR1JWcXhxd2ZMZU1UUlBHbXRKSkgwdngwbkdpOC9uTytDVk8wenJJcjdISDlOcUlmT1d6THFzckN5NDdiYmJBQUJzMzhzZTRsQW9CRTg5OVpSdHpTekFnVzFDUk1lcjI1TlNrUFhldlh0aHhvd1o4TlJUVDRua3BpU1laTzNyakRQT2dKS1NFbmp2dmZkZzJiSmxzSHo1Y3EyK3ZsNzhuMHhtVmFEcHVnNlptWm5RcDA4ZjFIVmRyTDJtK1VEU0tPbFBUa0toYVpxWWovenl5eTgxK2Z6akZZcWtvVHJGR3JMbTE3WmdZZGpHa2IyZlBwOFBSbzRjaVNOSGpoUWJUUUhzRDc4aHdYbjU1WmZEanovK3FLbEpIR1JoSUF1ZmVDRGhJVzlrUkVMNGpUZmUwTTQvLzN3Y04yNmMwRklwN2xFVzVqNmZEejc1NUJQNDVKTlBOSnJMbzNMcGxiNmpjalJOZ3drVEp1RDk5OTh2TmxhU25UYXk0SXdsbkRaczJBQVhYSENCbUhPbGN1UDFKak5IQjJ3bXR3TEkrYUR1b0NmUHhSRWVqOGVXR1prRVhVSkNBbHh3d1FXNGNPSENxUDFZS0V5a3Vyb2FKazJhQkY5ODhZVkdqZ0paMk1udnliUnNDcldOY2tnS0NUclROT0hCQngvVWdzR2c3UnpWZWJSMjdkckI3Tm16b1ZldlhxS3NXUE5ySkhBdHk0THE2bXJZdm4wNzdOMjdGK3JyNjhIdjkwTm1aaVprWm1aQ1ZsWVdaR1ZsUVVaR2h2Z3VQVDBkYUZ2WDJ0cGFxSyt2ajhvbUUrOURnTUthNUdrR0V2SnNNak9NUkZNT0ZISW8xTlRVWUZaV0ZnRHNFeUtxSTBBdWk5N1RYOCtlUFdIMjdObFJFL2VJKy9kQjJidDNMMDZhTkFuVDA5TmIzSHhUdzFUSS9BUUFTRTlQaDJuVHB1SFBQLzhzSERpMGc1OTgvcktENTdQUFBzUE16RXl4ck05cFh0QXBjQm9Bb0cvZnZ2RGdndzlpVFUyTktKZWNTSWo3c253SEFnR2NOV3NXL3RkLy9SY09HalFJZS9ic0tjcHRMTVcvRTJQR2pJbktMSzd1aDhJT0ZJYUIrTHpKaG1GZ1pXVWx0bS9mUHVhMm1hcTJSL05mVTZaTXdmejhmQXlGUW1oWmxranhUNElRRWJHNHVCaUhEUnVHNmtCdkNSTXYxcW9QVGROZzdOaXgrTU1QUDJBb0ZFSmQxOUd5TE55MmJSdFdWRlJFN2ROaUdJWXQ2ZTBISDN5QUNRa0pqbHQxeHRwcWxJU2ozKytINTU5L1BpcTdOeUppZVhrNW5uTEtLYWhwbWtnM0pwZEI3WmZYVnpmR0ZWZGNnWUZBd0ZZUEMwT0djYUFwWVVqZlZWUlVZS2RPbmNTa1AzbE5FeElTaENCTVNrcUN0TFEwNk5ldkg4eVpNMGVFckZCNmZIWGZrK3JxYW56c3NjZHNXYUZKQ0xRa2N1QXg3ZWs4ZS9aczJ4N09obUZnZm40K0FnRGNmLy85V0Y5Zkw4Sjg1TkFiRXBLQlFBQ2ZmZmJacUxZVHF0YW1mazVOVFFXNVQzUmR4OXJhV3J6a2trdFExbUxsMzhwbHhKdTVaOXk0Y1ZIQ1VMMjJMQXpiQnV4QU9jS1FBOFRuODhFOTk5eUQ1ZVhsWWxrYUpVUnQzNzQ5ZE96WUVYSnpjNkZ2Mzc2UWxaVmxXMUtXbkp3TUFQc0djQ0FRZ0xWcjE4TDI3ZHRoM3J4NThQUFBQMnNBK3dRV2hhTVFCK0k5ZG9LQ3VRRUFoZzBiaHZmY2N3K01IajFhcE8reUxBdFdybHdKSTBlTzFOeHVOOHlhTlV2THpzN0cyMjY3RFdRUE1QNGU5SXkvTHpHY1BIa3k3TjI3RjJmUG5xMVIyQXM1U2VRNVBUbGduRDdMQ1ZXcGp6Lzc3RFA0N3J2dk5KVG1OS2tmQU96TCtRekRFSytOMGRnS0ZCWjJEQ01SYjlDMXVvTWRhUmJxZmlueVo5bkVMQzB0eFZ0dXVRVkhqeDZOdWJtNVVmV1RXWDBvdkora2FWNTY2YVc0WmN1V3FCM2lWcXhZZ2JtNXVVTDQwTExCUng1NXhHYldxNXRHV1phRmdVQUFIMzMwVVpUcmNRcVNwbmhLaXEzTXlNZ0F1YXlxcWlyOHd4LytFQ1g1blRMMXhHc2lBd0RjZU9PTkdBd0dHOTAzbVRWRGhvSDQ1d3dSOTIzUzVDUVlkRjJQV3JraFF3S0ZBcTBKSnljTW1aMHRMUlRQT3VzczNMRmpoMmhMTUJoRTB6U3hycTVPN01Jbjk0Zlg2NFdVbEJSNDhjVVhiUUpSRnFKeXY5eDMzMzJvenBuSzI0dXE1M25paVNjQzlZMXBtcmh4NDBaVUhURzBEdHBwWlVxOFp2TGt5Wk50K3lhek1HU1lHRFFsREdsT3E3UzBGSWNQSDQ0REJnekFnUU1ING9BQkEvQ3NzODdDSjU1NEF0ZXRXeWNjRExMZ2tMWEVVQ2lFaFlXRm1KT1RZOU5zYUZBZnlvUUNLU2twc0hMbFNzZnp1dlBPTzhVYWFYbEpIYjNtNXViQ1J4OTlKSTUzMGc0Ujl6aytyci8rZW94MUxxb0Q1T1dYWHhabEJnSUI3TisvdnpoV3puUWovNVpvVGw5Tm1UTEZOdWZwcENHeU1HUVlpRTh6dEN3TEt5c3JvN2JQSlBMeTh1Q2RkOTRSeDhzQ1E5WVlEY1BBSjU5OEVsTlNVc1J2MVpnK2VlMXRjd2Rqck5DZUo1NTR3bEZ6TFNnb2lHdENjc0NBQWZqdHQ5K0tjMUNuQXVpNzB0SlN2T0dHRzRTR3FKNGJDYmpNekV3b0tTa1JRbXJldkhub2RGeEw4SmUvL0VYVTQrUlJabUhJTUw5enNNS1F0SmFoUTRlS1dEMG55R3RiVjFlSEYxNTRJYXJhVHF3a3BNMUJYc3BHZ2lndkx3L0l2Q2ROaklURHRHblQ0dmJPNU9Ua3dJWU5HMndlY1hvdkM4YXFxaXE4NVpaYmJNSk5GVFQzM1hlZk1GMDNiZHFFLy9FZi94RjFmRXNKb252dXVZZUY0VkVDcjBCcDVaQTNjODJhTmRxaVJZc0FFVzFlWVZTOHd5a3BLYkJ3NFVLUVExSjhQcDlZVVNJdjBZdG5NS3BKQ0tnK1doNTM3NzMzb2h5YVlsbVdNTlBYckZrVDF6bDZ2VjRvTFMyRmE2NjVCZ29LQ2tScUxYbkpIZFdaa1pFQnMyYk5ndXV1dXc3SllTSWYwN3QzYjdqaWlpdkE3L2REWFYwZC9NLy8vSTlZZWlqM1dVc0pJazd1ZXZUQXdyQ1ZJNXVDOCtmUDF4WXNXQ0N5UnRPYVhrcUtBTEJ2cy9adTNickJLNis4Z242L0gxd3VsOWlDMCtWeWdhN3JRb0JnTThOcVZBSGNvVU1INk5ldm4xaVNGZ3FGaEFDTFJDSWk3WDlUNTZmck91aTZEai8rK0tNMmRlcFVLQzR1dHAwM1NrdmRBUGJGRU02Y09STW1UcHlJOHJwcVRkUGdubnZ1d2Y3OSt3TWl3dHExYStIMTExL1huTW80bUEydlpHSk5ON0NBWkJpRmxwZ3psRWxLU29JVksxWkVsVUZ6ZHZRYUNBVHdnUWNlUUlCb0o0cWMyaXZlYzVEbkhNa0RlL2JaWjJOUlVaRllZU0tidG1WbFpYamFhYWMxS1cxVjc3ZW1hVEJzMkRDc3FxckNZREFvY2pMSzUwZnppSHYzN3NYSmt5Y2p0ZStaWjU3QnVybzZjVnkvZnYwY3IwVkxldEpuekpqUnFLZWZ6ZVMyQTJ1R3JSeDE3aThVQ3NITW1UT2h1cnJhWnJLcWdpMHhNUkZ1dXVrbU9QUE1NNFgyUkFIT2NuNi9lSkcxU05JcXUzZnZEaGtaR2VEMysyM2JDeUFpVUVMV3BpQnpPekV4VVp6SG1qVnJ0SFBQUFZjRWM2dkxFbW5Pc24zNzlqQno1a3k0K2VhYjhmenp6OGZKa3lkRFNrb0toTU5oR0RseUpQejIyMitPdjNYcXJ3UGxRQnhSVE91RWhXRXJoekxheUFOdTFhcFYybU9QUFNiMlBnRUFZUzVUTmhvQWdFNmRPc0hreVpOdGV5TkhJaEZibnIrbVVGZXBrUERDMzFlSkpDY25pMHcxdEdxRFlnRGpFWVowYnNGZ0VIdytuekQ5MTY5ZnIwMmNPQkZLU2tyRUNoSUErOGJ5THBjTE1qTXpZZHEwYWJCNDhXSnd1OTBRREFiaDFWZGZGWWxyNmJkMEhpMnRsWEVLcjZNSEZvYXRIRTNibjR0US9qeC8vbnh0NWNxVnRwUlljc0lDL0gwNTM1VlhYZ2wzM25tbmJRVUhDYTU0NWd5ZHdta0E5bWVjbG8raDdOWUErNFRFd0lFRG15eGZ6b3hOKzZWWWxnV0dZY0Q3NzcrdjNYampqZERRMENBY1NiSmppT3J1MGFNSFpHUmtBQURBbDE5K0NiTm56OWJvUVVHYUlKVXI1MzlzQ1lFWWF5c0JwdTNCd3ZBUWc3L250cVBCNXlTQTFNRWtDeDA2bnJROUtpY2NEc01mLy9oSExSQUlpTTJSQ0RuRHRjZmpnV25UcHNIZ3dZTlJMcjg1N2FmZnlKb1ZDUll5T1Vub3lHWGZlZWVkTnUxUURZeFd0d0JRNS9SdzN3NTYyc2tubjZ6VjFkWFpjaVU2ZWROMVhZZS8vdld2VUZSVUpQcFFiVDhKYkxtZGNodWQydFlZdE4xQlkzMm5PcXRVWnc3VE9tQmgySXFJMTdzcmF6bC8rTU1mb0txcVNndzQyVlFtZ2VEMWV1R05OOTZBamgwN0FvQTlzM1Z6MjBTL3BSM3daTE9aSENzMEo5bXRXemM0OTl4elJlZ050WWxDZkdTdFZrN25UMlZTa29jdFc3YkFoQWtUaEpBRDJDZXNaQzg2elNPKzhzb3JjTVlaWnlEMUJ3bHFlVzltOWR6azc2aU5hcUxkV0RRV290UlUvemJYbTg4Y1dsZ1l0aEpVN2FFcFNDdjc1cHR2dEVXTEZvbndHWG4vWXRuazdOdTNMMHlmUGgxcEh4Skt1OThjWktjRElrSitmajRVRlJYWkJHUTRITGJ0ai96NjY2L0RjY2NkSjlwTTU2bnJPdmo5ZmlINDVFQnVUZE9FbzRmNDVKTlB0RW1USnNHbVRac0FZSjhXU09kQTUrcDJ1MkhBZ0FFd2QrNWNPT2VjYzlEcjlZcjZhRHNBcDNPU0E4bmw2eERQUGlqTjBlNVlFMnpkc0RBOERNUWo2R0taVWszOXhySXNtRHQzcnJaMTYxWWhsR2lUZGRMVUtCWFlWVmRkQlVPSERrVktUZFdjb0d1MVBZZ0ltelp0MGpadjNpeUVpV0VZWWs2UDJ0S3VYVHU0OTk1N01UMDlQU3FISU8wNVFrS05ORVlTWHFUWkVjdVdMZFBXcmwwTEFQdFRrZ0hZVjljWWhnRkRodzZGdi8vOTczREZGVmVJM0lVdWwwdG96ZFIrMGp4bDg1L0twbkxqNlI4MU1EMWVXRE5zWGJBd2JDWEVJekNkaEpkbFdWQlJVUUVqUm96UTZ1cnFBQUNFcVVyL0oxSlRVK0dERHo2SXVhTmJySGJGYWtkTlRRMHNXYklreXV5bWVVU2FtN3YyMm12aDdydnZSam52SWptRnlMU251aWljUmphZi9YNC9aR1Zsd1R2dnZJTVRKa3dRMnFDOGQ3SmhHRFp2ZG5aMk5peFlzQUJ1dnZsbWxJV3EvSjQ4OWZJbVZyUU5LUW5LbG9EbkNCbm1kelJOZ3p2dXVLUFJ3Tnl5c2pJa2oyaGpjMUJPZjhURWlSTkY2aXpFL1FIS2lDalM3bHVXaFI5Ly9ERlNUc0htNEpRSjJ1VnlpU1FTY3BwOU5lczJJdUxDaFF2eHJMUE9RcWNOM1dVdnRWeVB6K2VEMGFOSDQ3Smx5OFQ1QklOQjNMQmhBNGJEWVZ0QXRnb2QrL0RERDJQNzl1MEJ3TzRzVVpPNzB2dm1oTXM4L2ZUVFVSdklxMUEreHBaTUVNRXdiWko0aEdGcGFTbW1wNmVMNCtYZk5nVnBVcW1wcWZENDQ0L2J5bFVUclpxbWlZRkFBTysvLy82NGJEUXl0UnRyVjNaMk5uenp6VGMyd2FkQ2dtemJ0bTM0d2dzdmlCeUg2dm1SSUVwT1RvWlJvMGJoNHNXTHNhU2tCQTNEd0Vna2dxWnA0c3laTTNISWtDSDQ3cnZ2Mm9TOUNnbG4welR4ODg4L3h6Rmp4dGdTTmxEOFljK2VQZUhxcTY5R1ZRakdJeFQvOXJlL05Tb01MY3ZDMmJOblJ3bEQxaEtaWXhZbllTZ3Z6OXU3ZDI5TVlkall3RkdkQWprNU9mRE5OOS9ZRW82U1lKRHJMQ3dzeFBQT082OUpnUmhMV05ILzVIVC9SVVZGdGwzdVpJRWtaNTR4VFJOMVhjZWRPM2ZpNDQ4L2ppTkdqTUFoUTRiZ09lZWNnM2ZjY1FkKy9mWFhXRjlmTDQ2VGhmcWdRWU5zS2J5KytPSUxXejFxLzlKNTA0Tmd4WW9WMkx0M2IvRDVmT0QzKytHdXUrN0NjRGlNNFhBWTU4eVpnN0lUSng2ZWV1cXBSalZUeTdKdzFxeFpMQXdaaGdURzlkZGY3emhvNlgxWldSbW1wS1E0Yms0VWJ6MzBldm5sbDJNNEhCWnJlSjJFb3E3citNNDc3MkJxYWlvQTJMT3ZOR2ZES0hsUWp4dzVFci8rK210SFRTbldlY3VDVHY1ZU5yZDFYY2VQUHZvSWh3d1pZaFBlSG84SDJyZHZEMHVYTHNWQUlHQ3JUMDBTSzdkSm5qNmd6OTkrK3kzbTVlV0pqRHV4aEpXYW9IYmV2SG1PMjdQSzM5MXh4eDAyWWVpVWdaeGhqaGttVEpoZ0crU1VqaDV4WC82LzB0SlNFZlpDeEN1VTFPTjhQaDlNbmp3NVNnQ29nc0EwVFh6eXlTZnhZQWVuM080ZVBYckFyRm16c0xxNjJxYlpFZkpuZFU1VEZhSzZydVA2OWV2eHBwdHV3czZkT3dPQTg3eHByMTY5NExISEhoTjlTZWNxNzVkTWZTOS9wanlFbjMzMkdRNGVQTmltY2Nyem1yR21CelJOZ3llZmZGTE14OHJJZmZ5blAvMEo1WWNOZWRBWjVwaUN0dm04K3VxclJRWVdlYkRRZ0ttcXFrS0FmY3ZORGpSRnZ4eGM3UGY3WWNHQ0JWSENrT3FtdGxpV2hiZmZmanZLQTVWUWw3NDVRZlhKcjM2L0h3WU5Hb1NmZnZxcFkzMHk2aWJ5MUQ4N2R1ekFLVk9tWUU1T2pxMGY1WE9WKzlqcjljSUpKNXdBQlFVRnFDS241WmY3d0xJc25EbHpadFIrMVU3OUdtdVYwTk5QUHgyVm5Wc1ZpQmRjY0lIUWFPa2NEdVUyREF6VGFuRzVYSER6elRjMzZsd29MUzFGZVlBY3lKeVNhb1lOR2pRSXQyelo0aWdFNkwxcG1yaDE2MVljT25Rb3FpbSttb3ZjWnZwOSsvYnRZZnIwNmJodTNUb3NLQ2pBb3FJaXJLeXN4SnFhR3F5cHFjSHk4bklzS3l2RHpaczM0NlpObS9EVlYxOUZpb1ZzN0J6bCtzaTBkYnZka0pLU0FoTW1UTUIxNjliaHRtM2JzS3lzVEFqRVVDaUVaV1ZsV0ZSVWhNdVdMY096empwTE9GUmtJUldQUjVuYThlS0xMMFk5MkZUNjlPa2pmdGVjbmZlWXd3dlA0aDVpS01ENWlTZWV3THZ1dWdzQTltZGNvVmZETUtDeXNoTHk4dkkwU25RS0FDS2hRRk5RV2VyeEhvOEhycnJxS3B3L2Z6NWtabWFDcnV1T0dvbGhHTEJxMVNxNDVwcHJ0SXFLaXFpMXp2SFU3ZlY2UmZJSGRiOWhXZytjbHBZR3h4OS9QSGJyMWcwU0V4TWhNVEVSYW1wcW9LaW9DTFpzMmFMVjFOU0FZUmdpUGxGZUJ5MURnZGwwblBwL3FqOHZMdytHRFJ1RzNicDFFOWwxZHV6WUFmLzYxNyswSFR0MkFNRCtaWVQ0K3dvV2lwR1UxM2VyeU4rLy9QTExlTzIxMXdyaHFDN2owM1VkTWpNenRWQW9GSFV0WTVYUE1FY3RMcGNMM24vL2ZhRUZxazRDUk1UNitubzg5ZFJURHpvZWpiSk9FMzYvSHg1NjZDRVJqeWVIbTZnT2x1Ky8vOTQyTXB1VHJDRFc5MDJkaXp4UHA1cnE2dnljYXJiVG5KNnNFYXZKSUdSa0FhdkdHOHAxcXRwbnJEbERsOHNGcjczMm1zMVpJNXY3MXUvN1Bxc2FPOVhQRGhUbW1PT3NzODdDalJzM1JwbFA4aHhhT0J6R1YxOTlGUS9VbTZ3S0pEV2I5U2VmZkNLRXNTcUk2ZnR3T0l6NStmbllxVk9uWmdVZWt6Q1JQYTJxbWFzS29sZ0NWQlpHc2JZNVZSMCtMcGNyNmhoVmNNb1BDVmtBcTFNVDh1b1hwLzJZNWZkcGFXbnczbnZ2T2M0RjArZlBQLzhjR3hQUURIUE1NSFRvVUZ5eVpJbHRvRGlGa3BpbWljWEZ4WGpycmJkaWN5YlhuZWJwbk9qUW9RTzgvLzc3R0FxRmJDdFUxRUVjaVVSd3c0WU5PSG55Wk96ZHUvY0JuWE5qcTJTYzJ1czBSK2YwRzZmMFdySldwem9uU0FpcDJhNmRrUGQybGw5ai9kN2o4VUNYTGwxc2NZN3lLL1hucUZHalVLMlhONUZxbmZBVmFRRzhYaTljZXVtbDJMbHpad2dHZzVDWm1RbnQycldEM054Y0dESmtDSFRyMWcwU0VoS2k4Z0hLcWExb3ptcjM3dDN3M1hmZndaWXRXNkN5c2hMcTZ1ckFzaXdvTEN5RXp6Ly9YSlBuNHVLZGM2STF3TjI2ZFlQcnJyc09IM3JvSWZGN1dzOUxoTU5oOFB2OVlCZ0dyRnUzRGdvS0NxQzh2QnpxNit2Qk5FMG9MUzJGMTE1N1RhdXZyN2ZOQ3g2dDBEeWluQkNXK3Z6TU04L0V0OTU2Q3pwMzdpeldQTXZYdGFhbUJqcDA2S0JSdWpXZUgyU09hdHh1Ti9qOWZ0aTBhUk1HQWdIaE1aYk5YOWtVZFhxdm1xMjZyb3ZsWi9SKzBhSkYyS0ZEQjFGbmN5RXZwc3ZsZ3R6Y1hQajN2Lzl0MHd4bFQ3ZnFFYVcyQllOQkxDc3J3NzU5K3g2VDgxMmtjVkpmWG5ubGxXaFpsbGp0STEvVFNDU0M4K2JOdzFpZWVkWU1XeDhjK1htUW1LWUpQcDhQTm03Y0NKV1ZsVUp6SUMyQXNwK1FWaUdua0hLNzNTTFBucXc1VUV3ZHBad3lEQU8yYmRzbXZMeHkyVTFwWjJTbVJpSVI0ZkhkdFdzWERCOCtYT3ZkdXplTUd6Y09Uejc1Wk9qUW9ZUHdCSHU5WGxFMlpYYWhuSUdrSWNwN3JSek4rSHcrc1U4MTlUK2xQeHN3WUlCdzdKQ25ucTVaWFYwZGZQVFJSNDRhb2V5c1lXMng5Y0NQcDRORWpuV1QwMEhKa0FsRi95UEJLRzlzRHJCL2Jrb053VUJFOEhxOVVGOWZiNnMzM29IazFDYTVESHBOU1VtQjFOUlVNYWRGb1RLR1lVQWdFQkJDTVJnTXh0YzVSd24wRUtNSGcyVlprSmlZQ0pzM2I4YXVYYnZhMG9MaDcza1pGeTllRERmZmZMUFcwTkJ3aEZ2UE1JY1JOUnlEdnBQL1lrMmFOMmJ5eXM0Qkp5L3pnWmlxYnJjYmtwS1NvdXFSdmFqVWZ2bTk3TlFoeit5eFlDcXJqZy9pbW11dWNkd0gyelJOTEMwdHhSNDllckFwekJ4YnlLc2ZpS2E4aFhJOEhIR29NcHBRdTN3K242MWNOZUdBM0E1WmdNdW9udUZqUVJnQzJOY1NhNW9HSFR0MkJGcVBMSzkvRGdhRFdGTlRnK2VkZHg2cTE1QUZJM05NUUpvaGFVK3FOcWNLRUNjaG8rNGlSNExIYWVjMkt1ZEFCNWphSGxVd3h4THNzYlNrb3htMW56dDI3QWovK01jL01CS0pSS1VyS3k4dng5dHZ2MTNNWGNqM2c5TzFZZ0hKSEZXb2c2VTVua04xNVVOamRjaGxOWGNReGRMZzFISWIwL1RVT0x4alJTc0UySCt1NmVucE1HL2VQS3l2cjdlWnhaWmxZV1ZsSmQ1KysrMlltcG9hbCtCcnFyOFpobUVPTzJwd3VQclo2L1dDeitlRHI3LytXb1JLeVNGSnBtbmlpQkVqc0RGTm5tRVlwbFdqYW15eXRwYVltQWpEaHcvSHUrNjZDL2ZzMldQTGsyaFpGcGFYbCtQeTVjc3hPenM3YWtya1lLWXhHSVpoamhpeUZ0ZXZYejk0N0xISGNNbVNKVmhVVkJTVmg5RTBUVnl6WmcxT21EQkJiRFJGampSTzJzb3dUSnVFekdGWmt4czdkaXhHSWhHYlNXeFpGdXE2anF0WHI4Ymh3NGRqWm1ZbUFFUTd0UURzcTMyWXRnTS94cGhqR2dwY2x3UFlkVjJIaW9vS3FLeXNoRWdrQXNYRnhiQnk1VXBZdUhDaFZsVlZKVmFhVUJDNjMrK0hjRGdzUE8rUlNNUXh6eUxETUV5YndlZnpRWmN1WFdENDhPR1lsNWNuTWwrVCtSc3JwMkpqV1hZWWhtSGFERTRtTFNWZ2Rab0hsSU93Q2RuN3pEQU0wNlp3RW1ieHhHV3F2K0VFcmd6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1FY3ZSenpIa0xvdkxVRHpOanRxS2sxU2E5bDlMRlk3VzB2N21BT2pxZXQ2cEhmQWErNzRVSTkzYXJ0OERPMm9TTHMwSHVuelBSaU91REJVb2JXZTZnNXhoNG9qTFV4Yk11ZWRVMWx5KzUyeWFqZjMvT0laTEFkVFhuTnBxdjZXYWwrc2NscjcvWEU0QkpOVEh4M3VjZHdTdEFwaDZIYTdZZFNvVVdoWkZuZzhIdEIxWFd5OUtXZithTzd1Y3ZIUTFNM1dWSDQ2cDQyYzVETFZ6YUZpTGVvLzBQcnAvQnZiV2lCVzIrSVphT3J2MU44ZmJKcXFTQ1J5VUw5dktqTk1VL3RLTnpWWW5iWnRqZlZaM2krYlBqZFYvOEcycnluaUZZWk8yWHRpbmF0NmpLN3I4TkZISDJrMFhyMWVyOWhqdWkzUktsSjRVUm9rZ0gzWmhTT1JDTGhjTHRCMVhXeExTYlMxWGNkb3NEZFhDQjBzVklkbFdRZFZ0NU5tS1g4KzFKcEhXeks1bkFRSlBheGluVWRURC9QRG1TeldTZGc1V1JGT0R3RDVvVWlDVUo3NmFnc2NjVWxDMm9WVHB6WFdtVTJaTHkzWnZzWm9xdjVEYllZZmFUTy90ZE5TMXkvV2NRZGJmbE9hOWFIT2lYZ3dVeVpPcU5aY1crS0lhNGJ5QlZDRlgyT2QybElPbHFab2lSdmtZTXBvYVdIYzNMYW9nN1V4TS9GUWNMRG5mNmdmVm8zOVA1NTdyeW5CY1RqdjM2Ym1uSjBnazFnV2dyVG5kbE5UQUsyTkk2NFprZ0FranhRbDByUXNDMHpUUE9LYVQwc0lvNE1aTUFkNmZ2RnF6b2RhODIySzFxNjVIc3pENUhCWkw0ZVNlTzRQeXZTdHdtYnlBUkNQT1V3MEZRcWdjcVJ1eENNNUVPSXhmUTYwZlMzdFRXWU9MYzE1V0IzTXRTUmxoalRFdG13dXQybGNMcGR0SWxxZGRGWTMvQ0hjYm5mVUpqN3hiT3J1VkwvYUJxZXlEbVF5UEZhN25YRGFZTDZ4RGMzalBVYzFjN05zT3FzYklqV24zNXk4MGVwbTllcnhUdlU0WlpodWJBdFFLbC91Qi9xLzdKMlA5MXpVdHNxZjVYTFVkc2ZxeDFqbkVDL3lOZ1dOZWZ4YnUvT1JhU2J5UlhjU1JrNFhuSVNYL0ptTzkzcTlqb0l0Rms2Q1NoVVFOUEFPUmlpcTU5ZFVTSTM4T3pvZnB6Q2dwbEFITnVHVURwL2E1SGE3aFdDS1I2aTQzVzd3ZUR3eEJhdkg0eEY5S05lcENqQ3F6K2tZcDNOUWYwdnZLYktodVdGRGpaMkRYQmFkRDdWSHZTN3F0V29NK2ozVnBkWXBDM3dxbC9aMVlZNVMxRUhncERtb3gzZzhIbkhqTysxYkVjOWVGalRZWlUxRHJWK0ZidlI0Ym5oMXNNZzN1Q3dRYVVBNGFXZXg0aHpqSFJBK244LzJPNmZ0TVozK1I3OVIvOVMyeWRkRTduTlY0S3NidkFPQVRVaXEvZXIzK3gzYkpRdHRPbDZ0U3czcGFneXlLcHo2UlVVV2hDcXh0T1I0aUtVdEEvQ2VMTWNNVGhxVGt6blNtRmFrL2kvV0prRHhJQWRaazZiZ1pNYkZXNzU2SHVyZ1ZzOU5QUSsxYlU0RHR6RmltYXNKQ1FtMnRxbDdCZE5ucDkvRzBxWmxUWS9hcDJwYkFJMFBibldqSnZWQklndE5xa3VsdWVhK1hJOXFxWkMxNGRTUGNoK3BKbjY4OVR1WjkxUlhZOU1aNnYrWm80UjROUjNWWktDYnp1bEdiazdkVkI3OTNtbkFxUnBVdk1qbWoxcWVrOGFubWtyeG10UHh0TVBKZkhPYTQydEtFMm1zTGJFZUh2STFVNCtscVEyMVRmVHFKSXpraDZpcVVUc0owbGc0VFFlUTFxcWVxMnc5cUJ0SU9abjh6ZFhvWWdtL1dQM05tMWZaYWZNVEIwN2VLNWZMWlZzYWxaaVlDRU9HRE1HQkF3ZENjbkl5SkNRa1FIVjFOZno4ODgrd2NlTkdyYnk4SER3ZUQ1aW1LYUxwcWJ5bVBHSXVsd3Q2OWVvRmdVQUFrcEtTSUJnTVJqMmxBNEVBR0lZQlpXVmxVUXZiNDBFT08xSUQxSDArSCtUazVFQ2ZQbjJ3VTZkT0FBQ3dkKzllMkxadG0xWlVWQVNoVUVpY0M3VkgxM1hIc2hxRFBQN1V0NzE2OVlMZXZYdGpSa1lHSkNZbXdzNmRPNkcwdEZUYnRHa1RXSllGaUNnR29WTUVnSlBuVWw3UG1waVlLSlpsQm9OQjBWYjZyUnFCUU44bkpTVkJJQkNBbEpRVXNmb25Fb2xFSFUveGNmUTd1VTJwcWFtZzZ6b2dvaWdqbHFkVnZkZjhmci90R3BPSHRiRkVCajZmRDB6VHRMV0o3c1Y0SU1GdUdBWjR2VjdvMTY4Zm5uamlpZENqUncvd2VEd1FpVVJnKy9idHNIYnRXcTJvcUFoMFhXZFA3OUdNYkZZUlBYcjBnRGZmZkJPM2I5K090YlcxV0ZaV2hwRklCQkVSNitycXNLNnVEb3VLaXZCZi8vb1hYbmJaWmVMT2E0NW1rSkdSQWJ0Mzc4YWFtaG9zTGk3R3ZYdjNZbVZsSmU3WnN3Zkx5c3F3cEtRRVMwdExjZmZ1M2JoejUwNWN2bnc1WG5ycHBSanZKTGFxTWRENXRXL2ZIbDU2NlNYY3VYTW43dDI3RjJ0cWFyQyt2aDdyNit1eHRyWVdTMHRMc2JDd0VGOTY2U1hNeXNvQ0FPZTVyYWFRemE3TXpFeFl1SEFoYnQ2OEdjdkx5N0dpb2dJRGdRRFcxZFZoVFUwTmxwV1Y0YTVkdS9EbGwxL0d6cDA3QzNQWGFaNVExY0xrNjNibGxWZGlhV2twRmhVVllXRmhvYmcyVHVhZjJrOWp4NDdGb3FJaTNMbHpKNWFVbE9DVUtWT1E1di9VK1VtNVBmVDczTnhjS0N3c3hKMDdkMkpSVVJHU2dHN3NXc21hN1BMbHk3R3dzQkFMQ3d0eDl1elo2S1RseXU5UE8rMDAvTzY3NzdDOHZCeFhybHlKSjUxMFV0UjkyQml5QmZELy90Ly93eDA3ZG1CeGNURTJORFJnS0JUQ1lEQ0k0WEFZZzhFZ0ZoY1g0L3IxNi9HQkJ4N0E1c3lKTW0wRXVwRmxjeU01T1JuR2p4K1BvVkFJVGRORXk3TFFzaXdNaDhOWVVWR0JlL2Jzd2VycWFqUU1BeEVSRGNOQXk3SXdQejhmYzNKeW1sVi9VbElTSUNLYXBpbktJWFJkUi9sL2hHVlorTU1QUDJDUEhqM2lxa1BXTkJNVEUrSFBmLzR6MXRiV29xN3JhSm9tQmdJQjNMTm5EK2JuNTJOK2ZqN3UzYnNYZzhFZ21xWXBoUCtNR1RPd2ZmdjJCN1MvYjJabUpreWRPaFV0eThKUUtJU0lhS3R6MjdadHVIdjNiZ3lIdzZLL1E2RVEvdmQvL3pmNmZMNUdrMVdvNXI3YjdZWXhZOGFJZnJRc0N5c3JLN0YzNzk3aXQzSTU4bm4wNjljUHFxdXJiZjEvenozM29OUDVra0JTcHkrNmR1MEtWQzhpWWl5bkZLRjZpWC8rK1dkeHpVM1R4RWNmZlJRek16TUJJSHBlRldDZk1Dd3NMTVJBSUlCYnQyN0ZRWU1HTlVzWWVqd2V1T2lpaS9EWFgzOFYxd1VSTVJLSllIbDVPZTdac3dkcmEydXhycTVPZkkrSVdGWldodVBIaitjZ1VZa2p2aHp2WUNFVGgweW9wS1FrZVBEQkIvSE9PKzhVWnNiR2pSdmh3dzgvaEpLU0VpZ29LSUJJSkFJZE9uU0EzcjE3UTc5Ky9lRGlpeStHdExRMCtQVFRUOFVpODNoTldicGhLZVBPdkhuendEUk5zQ3hMbUdZcEtTblFwVXNYdU95eXk4QXdEUEI0UERCbzBDQll1SEFoamhrelJnc0VBcUJwbWlqRE1JeW9QSTh1bHd2OGZqL01uRGtUcDA2ZENycXVnOGZqZ1E4Ly9CQldyMTROMzMvL1BlemF0VXZUTkEyNmQrK09nd2NQaHVIRGg4UG8wYU1oRkFyQjlPblQ0WVFUVHNBcFU2Wm81ZVhsb2t5NW5RQVE5YjVqeDQ0d2QrNWNIRHQyck9qanQ5NTZDOWF0V3dkcjFxeUI0dUppVGRNMDZOYXRHdzRaTWdUT09PTU1HRFZxRlBqOWZwZy9mejRNSERnUUgzendRYTJzckV5WWlMS1pLeS9oUWtUUkxsbHpURTFOaFJkZmZCR3Z1KzQ2cmFTa3hOWS8xS2JNekV4NDlkVlhNVDA5WGR3VDhoeW5iR0pybWlhdU05VlA5NHFxc2RKMWtmOHZ2NmZwQi9xT3pHcTZsdE9tVFlPRWhBU2NPM2V1VmxKU1lxdVRIaEkwTFNEWHF5NXpvMWM1STR6TDVZSXJycmdDLy9hM3YwR25UcDFBMTNXb3I2K0gvL3UvLzRQZmZ2c050bTNiSnFadmV2ZnVEVDE3OW9RUkkwWkFYbDRlRkJZV1FsRlJrZTFlbHU5NURwcHVnNmczN3cwMzNJQ1ZsWlhpNlR4djNqdzgvdmpqYlpxQlBNR2RscFlHSTBlT3hERmp4bUJTVWhJQU5NL0xscEtTQXFacGlxZHVSa1lHQU95ZnRQZjcvWkNRa0FBZE9uU0FvVU9IWWlBUUVFL240dUppUFB2c3M4WFRXWTJ0VTVrMWF4WmFsaVcweklrVEoyS0hEaDJpdk9IMGw1bVpDUTg4OEFBMk5EUWdJbUk0SE1aSEhubkVVUnR3TW1VQkFLWk9uWXFCUUFBdHk4S2FtaHFjTVdNR2R1alFJZW8zSkhpeXNyTGdwcHR1RWxwNUlCREFtVE5ub3F5Qk9abWQ4dWMvL3ZHUFFyc3pEQU1OdzBEVE5JV1dwenE2TkUyREYxNTRRUnhMNTJxYUprNmJOaTFLMDJwTTQrcldyUnVnaEpOVzYvU2VZaXZYclZzbjdqMjZWb0ZBQUo5OTlsbE1TVWtCZ0dnemVjZU9IWWlJbUorZmowT0dEQkh0amFYRnkxcHNWVldWMElLcnFxcHc4T0RCb2g3WksrOXl1U0FoSVFGT1BQRkVlUFRSUjdGbno1N0NvUk1ySE90Z25XN01ZVVllWkRrNU9mRFZWMStKQWZIMjIyOWpVbEpTVkRBdFFIeWU0M2lFWWxwYUdwQkpaWm9tcHFhbVJ2MVdEdFZadEdpUk1OdXJxcXBzcG9vOFNOUzYrL1RwQTcvODhvc3czLzc2MTcraUdyeXIvcFplUC9qZ0EyRStOVFEwb094OVZiM09oS1pwa0pLU0FoVVZGVUs0dlAzMjI2aitUZzFRZDd2ZGtKaVlDRE5uemhUVEJKczJiY0wrL2ZzM2VuNnhoS0ZzOXBtbWlYMzY5TEg5enVQeHdQang0N0d5c2hJTnc4QklKQ0tFUHlMaWZmZmRkMGlGb2Z6bjgvbGd3NFlOR0FnRU1CZ000dmJ0MjlHeUxORVB6enp6VE5RRDk5eHp6OFhpNG1LMExBczNiOTZNZ3djUFJ2aytwVDVWKzlubjg4R1NKVXN3RW9tZ1lSaFlYMTl2bXdlVTUwRzlYcS80TGVVSmpjV3hMQVRiZktDUmJPYWtwS1RBYWFlZEJpNlhDMEtoRUN4WnNnVEM0YkM0d09Gd1dBUzl1dDF1U0VwS2dvU0VCTWpLeW9MazVHVEl5c3FDdExRMEVTWVRqNWxBcGhJQVFEZ2NGZ05lRHJPaC8xOTQ0WVY0M25ubkNWT3RwcVlHZHU3Y0tjcVM4OENwZFo5enpqbVlsNWNIbXFaQmZuNCtyRnk1VWlTem9IYkliWkw3NXFxcnJ0S29QUWtKQ2ZDZi8vbWZLSHZlQWNDV0ZJUE12cUZEaDJKV1ZwYnd4dDV4eHgyYWJCYktwaTBOTXRNMFFkZDFXTFpzR2V6WnN3Y1FFWHIwNkFFREJ3NUU4b2lyN1cyTUgzLzhFYVpNbVNLOC9CczJiTURqano5ZVBOZ3lNakxneGh0dmhPVGtaSEM1WFBEVVUwL0J4eDkvTEs1SGMrbzZFTWc4eHQ4OXo2RlFTRmdldDl4eUM3ejY2cXZDbTN6cnJiZkNzODgrYTlQbXcrR3dNTFhwbkdTdk4vV3BuTGlFZ3VCNzkrNHR3b3FtVDU4dVRIU2Z6d2VJS014ck9vYnU3YVNrSk1qTXpJUjI3ZHBCY25LeTdYeU9WVUVJY0JUTUdaS3crVjJUUVlCOUZ6UVNpY0MvLy8xdlRSWVdkRk8rK2VhYjZQUDVSTFlOQ2lCMnU5MVFXbG9LRHp6d2dMWnIxNjY0NmcrSHd5S3NJVEV4RVdiTm1vVTBEMFJhaGR2dGhuYnQyc0hnd1lPaGE5ZXVJdVNpb2FFQmZ2MzFWNDNhaDBxSUIzMFBzTStKa1pLU0FwWmxRWEZ4TVd6ZXZGa0lKZ0I3S2lqMW5NUGhNR3pidGczNjllc0hMcGNMemp2dlBIanp6VGZGZkpWYUY1M1BpQkVqd0xJczhQbDhVRlJVQkRUblJZS0pIZ1F1bDB1RW9uaTlYakFNQTM3ODhVZXR0TFFVdTNidENuNi9IOUxTMG15L3d6akRSckt5c3VDMTExN1RUanp4Ukp3MGFSSzQzVzZZTTJjT1huLzk5Vm80SElaNTgrYmgyV2VmRFpabHdVOC8vUVF2dnZpaTl0aGpqeUgrbmszbGNBeHVFbGgrdngvOGZyOHRZY0hOTjkrc1ZWWlc0dFNwVXlFU2ljQ0VDUlBBNS9QaGZmZmRweFVWRlVGQ1FvSUlyU0ZCSjRmaHlBOHR1bDZSU0FUNjl1MHI3Z2VYeXdYNStmbWlQU1FVWFM0WFhIbmxsWGoxMVZlRDErc0ZSQlIxVVNqWjNYZmZyVzNjdUZIOE5sWjQyckZBbXhlRzhvUnZZbUtpdUdIb0J2WDVmQkNKUkd3WDlweHp6b0hPblR1RHJ1dmlKZ0hZZC9QOTl0dHZrSktTSXI1dnlvbENUM1BTdkc2NjZTWWhoRlJuQUVGQ2NzeVlNVnB0YlMwQTJEVU1Fb3IwV1c0THhRbEdJaEZ4UTFQYlNkdVFKL3hKS0ZPOEdUMEVaSUVrTzAxSW85QTBEZExUMDIwVCtqU1lJcEdJVFpCU1dTUVVxUTAwY0gwK1g1U2podnFzcWY2dHJhMkYrdnA2bUR0M3JqWnMyREFjTUdBQVhIVFJSVEIrL0hpc3FxcUM4ZVBIaS9hZWZ2cnBHbWxOY3A4ZVN1Z2NhUDR0SEE3YkF1VXR5NEo3NzcxWHk4akl3QnR1dUFFTXc0Q3JyNzRhRUJHdnUrNDZqYTRWUGFncDVJcnVBeXFEN2duNlRIR0lkSS9SOTNJNkxVU0VidDI2d1dXWFhTYmFxenJPWnMyYWhZaW9BZXdYZ01jcWJkNU1sdWZNeXN2TE5mSTBoc05odU9TU1M5QnBqNDFmZnZrRlZxOWVEZDk4OHcwc1g3NGN0bS9mYnJ1eDNHNDM2cm9lbHpkWnp1Tm1HQVpVVmxiQ25qMTdZUGZ1M1ZCV1ZnYkZ4Y1ZBWlZtV0JZV0ZoYkJ3NFVMbzNyMjd0bTNiTnNlNVBoVmQxNFc1YjFrVzlPalJBL3IyN1l0eSswaUl5WUtRU0VoSWdPN2R1NFBYNndYVE5PSExMNyswcFdwWGhRZVZzV3JWS2xGdVZsWVdaR2RuQzYyRGhDd2REN0EvZE1UajhjREFnUU94VTZkTzRQUDVRTmQxS0M4dmp6cS9lUHFYaFBEMjdkdmh5U2VmRko3a21UTm53cUpGaTBEVE5LaXJxNFBMTHJzTVFxR1EwTERVUDVtV0hQQ2tCWnFtQ2FGUVNNenZCUUlCMFhjQUFKTW1UZExlZnZ0dGNiOU9uRGdSWG4vOWRjekp5UkhYbHN4WnVYMXlzRHlaeXk2WEMycHFhaUFVQ2dudjh0Q2hRd0VBaEFjYVlOOTFMUzB0aFZXclZzRVhYM3dCcTFhdGdpMWJ0b2hycHk0cW9MTHAvYkVzR05zazhzMmVrNU1EMzM3N0xaSjNkL255NWRpMWE5ZW9ZMm1laE15YXUrKytXMHlZNStmblk2OWV2ZUplTzV5U2tpTGlERTNUeERGanh1RG8wYU54NU1pUmVPR0ZGK0lGRjF5QTc3NzdybkI4ckZtekJvODc3cmlvQ1hGcW4vcEs3MDg5OVZUY3VuV3JjQ2JNblRzWDQwMGs4ZlRUVDR2NmEydHJNVFUxTmU2QThuQTRMQndBYytiTUVVSEVhbnlkN0xSSlNVbUJ1WFBuWWpBWVJNdXk4SmRmZnNHVFRqb0paZTAxWG0veTZ0V3JVZTZudi96bEw4SlRpNGdZQ29WdzNyeDVTTmMwTlRVVjNuNzdiZkg3KysrL1A4cnAweGdINmswRzJIYy9rWk1yR0F6aVJSZGRoQUQ3SFdNSkNRbnc5Tk5QSTkwdnVxN2oxMTkvalhWMWRXaWFKdTdZc1FQUE8rODhsUHRTcm9lbVhBRDJhZk5MbGl3Um52YlMwbExNeTh1TE9oK2Z6d2VKaVlsaVR2ZW1tMjRTOGFtSWlNT0hEMGU1M01iT2oya0QwRVh6ZUR6d3dBTVBDQTliS0JUQ1YxNTVCWjNTSmRFTjZuYTc0Yjc3N2tQRWZlRUpXN1pzd2Y3OSs4ZGRkM0p5TXRCdkVSSFQwdEtpNmpyaGhCUGcxMTkvRmQ3T3YvLzk3K0k0OVZocWszcHVtcWJCUC83eEQ0eEVJcWpyT2dZQ0Fiem1tbXRFVUhBczdycnJMbHZBN2EyMzN0cmtiMlFlZnZoaE1lQnFhMnR4MHFSSllsNVdicXU4VW1iOCtQRllYVjJOcG1saU1CakVaNTk5MWlaVTFNR3VsamQ2OUdnaDdMNzg4a3ViQU5ZMERaNTU1aG5idzR1Q21qMGVEL2g4UG5qbm5YZHNYbmY1Zk5SK1ZoOUllWGw1UWhoYWxoVWxET1cycW5nOEh2anBwNThRRWJHK3ZoNUhqQmdSMWRlWm1abncvUFBQbzY3cklxcUEycnB0MnpZOC8venpvN3pmVGhsM3ZGNHY1T2JtaW52UE1Bd3NLQ2dRRDFvWk9TR0VrekJzNnJ5WU5nYmRBQWtKQ2ZEcHA1K0tBWXk0TDlwKzFLaFIyS2RQSCtqU3BRdjA3dDBidW5idENzY2RkeHljZnZycFNEY3dEUzQ1QnFzcE1qSXlnRzdzWURBb1l2QUlldXBPbmp4WmhQem91bzYzM0hKTGxBWUFFSHR4UGczS3JWdTNvbUVZR0E2SEVSSHhuWGZld1VHREJtSDM3dDBoT3pzYnNyT3pvV3ZYcnRDM2IxOVl1SENoMEZpRHdTQXVYcndZTzNic2FHdGJVK1RsNWNISEgzK01obUVJVGUrRkYxN0FQbjM2UUxkdTNTQXJLd3M2ZHV3SVhidDJoVUdEQnVHNzc3NXIwM3pXcmwzcmFHdXBRbFJtekpneG9wOCsrK3d6bS9EMWVyM1F1M2R2K09LTEwzRHo1czNZcmwwNzBjZmtrWDNublhlRTFraHhobkxZaVpNUUprSGJzV05IUUVSeHJlSVJodkxEK0tlZmZzSklKSUtCUUFBdnZ2aGlzZnBGL2sxcWFpcTgvLzc3NHY0a1NrcEs4T0tMTDBhS1dYU3FUMDJJTVhueVpBeUh3MkwxVTBOREF6Nzg4TVBZcDA4ZjZOV3JGM1RwMGdXNmQrOE9PVGs1Y1BiWlorTmJiNzBsNmpWTkU0Y05HeVlzak1hbUZwZzJnanF3MnJkdkR5Ky8vREtHdzJHMExFdkVxRFUwTkdCaFlTRnUzcnpadGs2WmJvN1MwbEo4K09HSHhRQ0xoOS9ER01UZ1RVNU9qdm90UFptZmUrNDVSRVJ4ODk1NDQ0MG9hMzdxT2NrYUk1MWpjbkl5L08vLy9pK1dscGFLUVdRWUJtN2R1aFZYcmx5SlgzMzFGZTdZc1VNSUk5TTBzYWFtQmg5NjZDR2JHZFhZRWpPVi92Mzd3L3o1ODIxTC9BekR3UHo4ZkZ5MmJCbXVYcjBhOC9QelJVeGdPQnpHN2R1MzQzUFBQU2NFb1dxR09Ra1VPdGZSbzBlTDY3SjA2VktrOEJEQzQvRkFWbFlXZE9uU1JYd20vSDQvdlAzMjIrS2FQdlRRUTFIQ21FeEdlWDA0emN2bDVPUUlZV2haRmpyMWsycEt5dGVJSHF6aGNCalBQLzk4bEkrVmowdFBUNGRGaXhhSmV4UVJzYkN3RUMrODhNS1lmZVpVZjFKU0V0eDIyMjFpQ2FhODdMTzB0QlMzYjkrT2hZV0ZHQXFGeFA4b2dIN3AwcVdZbTVzYjgrSEF0REdjQnJUZjc0ZlUxRlFZUFhvMHJsaXhJdW9KTEdzdWlQdUNncWRObTRaRGh3NFZRYkh4a3BHUllRdTZWalZEbVlTRUJOdDhWbVZsSlY1enpUVml3TWdyWTlTZ2JabTB0RFFZTm13WXpwa3pCM2Z2M2gxMWJ0U1duVHQzNG9NUFBvakRodzlIT1o2c3VhbWJTQkNOR0RFQzU4K2ZqMlZsWlRidGdnWVk0ajVUNy83Nzc4ZFRUamxGSktOUXB5Ym9zMU91UXJmYkRXUEhqaFdDOWYzMzN4Y2F0T3lnVWR0R3h5UWxKY0Y3NzcwblZxRGNkOTk5S0Nlb2NMcGZaSUdYbTVzTHRQNDZIbUZJK0h3KzhQbDhzSGJ0V3FHaFhYTEpKV0tlVkUweTRmVjZJU3NyQzE1KytXVnhINWFVbE9EbGwxOXVXMlZEOVRrOUxLbnZFaElTWU1pUUlmamNjODloVFUyTkVPVHkrbTU2V05mVjFlR0NCUXZ3dlBQT2kxcTlwSjdqc2FZZHR2bXpsY00xNUhXYkFQdERCVFJOZzZGRGgrSnBwNTBHUFhyMGdOcmFXaWd1TG9aZmZ2a0Zmdjc1WnkwU2lkZzhzZko2NEhoU2VJMGJOdzZycTZzaElTRUIvdm5QZjJweU9pYnk0Rkk1ZnI4ZnNyT3pRZGQxOFB2OUVBd0dvYXlzVEhna0FmYmQzT1NacE8vVUhjaGtFenM1T1JtNmQrK08zYnQzQjh1eVlQdjI3ZHF1WGJzZ0VBaUk0OVhZUHRscjJOVDVxV0V4THBjTDB0UFRJUzh2RDd0MjdRcVJTQVNLaW9xMHJWdTNnbUVZNGppS0FZMVZoOXdtK1gyM2J0M2c5Tk5QUjEzWFlkZXVYZkRERHo5b1ZEOGRwNGJweUcwOTlkUlRzVXVYTHVCeXVXRExsaTN3eXkrL2FHcTlGS0lrcDgyaU9jZExMNzBVNit2cklUVTFGZDU4ODAwTjQvU3ErdjErNk55NU15UWtKRUE0SElhS2lncW9yYTJOdWZZYllKL3dQdU9NTXpBNU9SbENvUkI4Ly8zM1dtMXRyUWhmb3JybE9NUEcrdEhyOVVLdlhyM2c5Tk5QRjQ3QW9xSWkyTHAxSy96NjY2OWFhV2xwMUNidk5FN2tlNUJwb3pTbDZUaVpaaFNQRnN0WkVhODNXYTI3cWZXa3BDVTRyYStOOVpTV1RVUTVqaTVXTytTSmQvSmF5OXJWZ1NTeEpkVFVUNnFKMVZqYlZBMHhWdnRsMUcwYW5PcVZIUVN4MG9iRnlscmpWSjY2aXFneG5NeEtwK3VvSm0yVlBjWDBQOWt5b04rb1puWmo1eVgzdjJ4aXErYTJXazZzZTVWcGd6aE5NTXZlWWtLZGU2THZHak9mNGlWV212dFlYbUtuLzhzM3JteGFBZGpYVmF2dGRVb05GVXZnT0hrb20wSWVQUEtEd21tL0V2bTkwendoL1ZaZUhTSVBZdms5SFN2M3JTb3dpS2F1WDZ5NXNGZ1BzMWhseHFyWHFTNTZ3TW5PSDdWOWpUblJuQjZRc29ORFhhc3NseXUzUS9aQU81V25FbXV1a21ubHlEZU0rc1NUSjZ6Vjc1c2FLTTBSaEU0M21OTU5SZDg1RFRpbjlqalZvWllYU3pPZzM4ajk0eVJvbWtKMUZqVDIzdW1oSXM4UE9yVS8xcHljK3AwcVNKeWNTM0s4bzNwK1R2M25kSy9FK24wczFQNlBwWFU3aFhjNVhRdW5wQ0pOT2J2VSswcnRsOFkwZHFmN3N6RlBQOE13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd1RDdmkvd012dE9UN1h5eDJmQUFBQUFCSlJVNUVya0pnZ2c9PSIgYWx0PSJSJmFtcDtKIEdyb29taW5nIiBjbGFzcz0ibG9nby1pbWciPgogIDwvZGl2PgoKICA8YnV0dG9uIGNsYXNzPSJvcHQiIGlkPSJib29rQnRuIj4KICAgIDxkaXYgY2xhc3M9Im9wdC1pY29uIj48c3ZnIHdpZHRoPSIzNiIgaGVpZ2h0PSIzNiIgdmlld0JveD0iMCAwIDI0IDI0IiB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciPjxyZWN0IHdpZHRoPSIyNCIgaGVpZ2h0PSIyNCIgcng9IjYiIGZpbGw9InJnYmEoMjU1LDI1NSwyNTUsLjA4KSIvPjxyZWN0IHg9IjUiIHk9IjciIHdpZHRoPSIxNCIgaGVpZ2h0PSIxMyIgcng9IjEuNSIgZmlsbD0ibm9uZSIgc3Ryb2tlPSJyZ2JhKDI1NSwyNTUsMjU1LC41NSkiIHN0cm9rZS13aWR0aD0iMS41Ii8+PHBhdGggZD0iTTggNXY0TTE2IDV2NE01IDExaDE0IiBzdHJva2U9InJnYmEoMjU1LDI1NSwyNTUsLjU1KSIgc3Ryb2tlLXdpZHRoPSIxLjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPjxjaXJjbGUgY3g9IjguNSIgY3k9IjE1IiByPSIxIiBmaWxsPSJyZ2JhKDI1NSwyNTUsMjU1LC41NSkiLz48Y2lyY2xlIGN4PSIxMiIgY3k9IjE1IiByPSIxIiBmaWxsPSJyZ2JhKDI1NSwyNTUsMjU1LC41NSkiLz48Y2lyY2xlIGN4PSIxNS41IiBjeT0iMTUiIHI9IjEiIGZpbGw9InJnYmEoMjU1LDI1NSwyNTUsLjU1KSIvPjwvc3ZnPjwvZGl2PgogICAgPGRpdiBjbGFzcz0ib3B0LXRleHQiPjxkaXYgY2xhc3M9Im9wdC10aXRsZSBvcHQtdGl0bGUtYm9vayIgZGF0YS1pMThuPSJib29rX29ubGluZSI+Qm9vayBPbmxpbmU8L2Rpdj48ZGl2IGNsYXNzPSJvcHQtaGFuZGxlIG9wdC1oYW5kbGUtYm9vayIgZGF0YS1pMThuPSJib29rX2Zsb3ciPtCf0L7RgNC+0LTQsCDihpIg0KPRgdC70YPQs9CwIOKGkiDQnNCw0YHRgtC10YAg4oaSINCS0YDQtdC80Y88L2Rpdj48L2Rpdj4KICAgIDxzcGFuIGNsYXNzPSJvcHQtYXJyb3ciPuKGkjwvc3Bhbj4KICA8L2J1dHRvbj4KICA8ZGl2IGNsYXNzPSJkaXZpZGVyIj48c3BhbiBkYXRhLWkxOG49Im9yX2NvbnRhY3QiPm9yIGNvbnRhY3QgdXM8L3NwYW4+PC9kaXY+CiAgPGEgaHJlZj0iaHR0cHM6Ly93d3cuaW5zdGFncmFtLmNvbS9yal9ncm9vbWluZz9pZ3NoPU1XeG1kSE5xY1hGa2FuTnZiUT09IiB0YXJnZXQ9Il9ibGFuayIgY2xhc3M9Im9wdCI+CiAgICA8ZGl2IGNsYXNzPSJvcHQtaWNvbiI+PGltZyBzcmM9ImRhdGE6aW1hZ2UvcG5nO2Jhc2U2NCxpVkJPUncwS0dnb0FBQUFOU1VoRVVnQUFBS0FBQUFDZ0NBWUFBQUNMejJjdEFBQ1EwRWxFUVZSNG5PeTllYnh0VjFVbFBNWmNlNTl6NzMzOVM5OFRBZ1FTa0Y2a2tRUVJhVlRBMHNRcS9FcExSZEN5S2N1MmJPQWxZT2xYamRXb1dQYVdXZ29rS2xaSnF5QUpKUWdvU0FCcDA1UDJ2ZVIxdHpubjdMM1dITjhmYzYxekh4QVFGQlg5T0Q4dUwvZmVjOC9aWisrNVp6UEdtSE1DWDNoODRmR0Z4eGNlWDNoODRmR0Z4eGNlWDNoODRmR0Z4eGNlWDNoODRmR0Z4eGNlWDNoODRmR0Z4ei8xQi8raEQrRHo3TUVET01DTDhRSCtGUTd5WW55bkFPQXlYS1pQZU40bmZIOEZnU3VXMzEyTnE5bis2MlU0eUZOeHFpN0NSYm9TVitxVC8vWUxqLysvUG5nVkxrc0hjRWtuSERCQmZ5ODNveUFLc3F0d1ZUcUFTN29ET0dCL0grLzcrZnI0LzQwSEZNQ3JjWmtCbCtFeVhPWUVQOWtUblllVjd6ejY5SXQ4b3ovcm5QNmszV2YyZTA2RjYxU2xkTGE3N1J0TDNpMVpUMHRHRUtRYjZGN0VBbURNV1pKaGMyTHBMbWMrZkMrMzdyNWxjYy9OeDdtK3ZyTGEzZm1iRjd6K2Vyd0xXNTk4YkdIOFYrRFNCRnpxVitKSy83cy9JNThmajMvU0JsaTlpMTJCSzhvbkd0d1RUcnB3MTZYRFE1NTBRVG41ekVtYWZOa011UGhrN055eEw2K2VOZkYrZGNvSk9pWTRDUU5BR0F3ZGlrVVVKUXdDVWVBZ0RCQlFGS1lrRUJJaGQ4d3hZTUdDaFEwYmg3QjE1OTFjMzVySWJoeDhmTytIZE50SDVtbnpmVmR2dnZGOUp4NGJBVGdPMkJXNHhxN0F0WVgvaE1QMlB6a0RES083eGw2S3QyUS80YnA5dy80djNuMHF6bnJxUS9NcER6MHA3M2phNmRoemJ1YzhMM1U5ZWlhWWdCSEFnajdDV0Z3aWtKUkl1Y084T0FvQkZ5QVhCSkNKa2lnSlJsRGVVeUJoSUdBR3dXZ09LUnZwVGdsOXo0NFpZYVFMampqbW0rdDM0dGp0bStub2grL094MTQzYVBhVy96SDh3WWNCTEwyZ0lGNkJTOU9WdUxiZ241Z3gvbE14UUI3QUpla0tYUE54bnU1NzluNzFGNTA3N3YvU2M5UGVTMDhxYTArWVdIL21uakpGNXdrWmptd2w1MFFWRjVTTGxUTFNjMEVCNkJBRW9JTzVkMFlsRWwwSG1xazQ2QUJoRkVsNUZoM3l6QUlVMGtjQ3BUQ0JjSFFnREduU3lUb1RtTVJrM3FlSjRBUXlKb2FPaFFWempWamtZZU1ZWnZjZW5OejdqbzhNTjcvK1hkMTFyM252NW8wSDIyYzZnQU1kZ0g4eVlmb2Z0UUVld0FHN0dCL2c1Ymk2dEo5OTQ3Nm5YUHpRNGV5bm40ODl6enNOdXk0NnVkdTVtb3NqdThNTlE1RlRZK0dZYzRLWE1MUWt4OXFVM1o0VnJwNjlFOU16OXZqSy90MU11MVl4M1RYUlpPOFVhY2NVMW5ka24wU2F5UVFYUkJBQzRDclFVRFJ1RFJqV1I4eVB6TEU0TXJmRjhabG1kMjlnNjY0WkZ2Y3NWT1lGdnVXVVFHT0hOTzFrbHBUNlRpYVdYRFRwVTQ4dUdUYktnRHQwK0o1MUhYLzc3YnpqOS81Nzk3OWVoU000Qm9SWHZCeVgyOVc0MnZHUDJDditvelRBeTNCWnVncFhiUmNTSjJIWGo1Wm5mODNGNWRULzV4enRlL3hwWmZkT3lMRkFLZW80TEhKT1BoUzZMeXpEalhzbVdEbDl0OVllZGhwMm5INnk3VHh2ajZabjdFYTNzb0kwNmNPa0hQTDVTTTh1ejA1bGdTTUFqekFNQWtnRUpJaUVKRmloa0l3Mk1YRnFTSDB5OUNZblVMSnJuQTBZam0vaDJDM3JuQjJaWStPTzR6ajZrYVBZT2poRE9lNDBVSlBKUkdtbEV5WldiRFFSYWJwU09peTB3TTA4ZE52dGZzZnZYcGMrOE1vL1hMLzI3UUJBRUYrSHIwdi9XQTN4SDVVQkhzQUJ1d0pYcUJuZTkreCt4bVBQcy8zZmZ2NjQ3MmtYK1A1ejltcUNHWHlZczNncEpZM0RqQmtqTlVsY3VXQVgxcDV3cHZiZi94emJmY0dwVEx0WGxXREFSbWJlR3VGYldTcE9kd0V1eUNrQ2hNdEZVR0lVR3c0S0VDZ29HU1hJQUxwRFhoeEdVZzdKQlpJVVVZMFZRbWRJSGNGSndtUnRBa3VkRnNQSXhlWU05OTU0TCs3OTZHSGVjOTFoYkI2YU9VYjZwT3RwVS9QZUprcERsK20rWnRiakR0Njl1RmwzL3RINzV6Zjh6TytYMS93SkFEL0JFTXVuTzRlZmI0OS9GQVlZaG5jeGljc0xBUHk3L2MvOWlrZm1zLzdOR1hudEdTZHp6VGdXeURpTXlXMmN6MU11QzJHYXVPTUJKM1BuWTgvemZSZWZwWld6OW9CR2FqTlRHMWxsUGxMWllVeDBBTFNvTlFYSzNRR0FjQWdPZWRnYklZSWlwRGh2UldvbE1TVUNwQU9pQ0ZJazRtZTFxb1VnU0hKS2tBcEloNUNJTkVuczFpYXdxU0c3WS8zUUZ1Njk4WmpmZXQzZFBITFRNWFVEYk1lT1ZhRWpWRlFNblJGZE9xUmpPT2dILy9RMnUvbm4vc2ZtNy93dWdDTElyc0FWK01lU0kzNWVHNkFBQWxkWk03d2YzUEhsVDcyRTkvdWhNM0RLVit6MUtlWVlQQ2NXWDJRT3c4eWNvMDNPMjZmOWp6OGYrNy80Zkt5ZXRwY1kzTXZSdWZKV2hvclRTQXJONEVnV3dJdkxKWVRSQU1VVlphNFEzZ3lDQkJBa291b2xBTWpoVG9TRmhZRTZTSVNQSk9IaEFSbHZKYW4rcVlIaHN3Q1pnRUxJQ1plUUVwQldlL1E3TzZpamp0NnhvVHMrZUpmZGZkMFJiQjJhWTVKNllwcEFha0R1ck9la0s5MENOK2lPdDc0M2YvaW5mbnZyRmErSmMzZFZBaTczejNjSTUvUFdBSy9DWmFrVkY5Kzcrb1RIWE5vOTVNZk9MZnVmZXpwMjRsaForTkRMUzNhTzgrUFFidVBPeDUzRDA1NThJWGJkL3d4aEJMVStNQitmaXdVT0dHUU15eWxPbENXVUFwWnFaTzZvSUI3Y25SRGhndUNCQTJZSkJFR1JEZ2xPZXFSL2xDZ25ZQUJBRW9BY0FBc0p4aWwyaTlRUkltU0FLWHlqaC9HQ3RLaTdDVWhoOERCRHY5SnJzc09zbEtLRE54N2hUZSs1VzBkdW1SRUxWNytTbktTdllPcWQ5ZE43ZlIwMzZjYlgvM24zRnovOW1tUFh2cEVBWG5uQ2VmeDhmSHplR1dCY2dnTWtydlNIN1huWXZuODlQT2o3dmlpZCtjUG5hMysvVVlaeE5DL0RxSDRjMTFuMkdFLzUyb2ZvMU1jK0dDdjc5MURIWnh6dTNaSVBEck5FQTZUc0tybGFSWUdzbExDV0lzcGRpdFE5akZJdWg2SDlUSUJZb01CYW92S1Fva1lSSXF4U1VkQXFUQXVnVVVENE9sbDRTaGhrOFJOUUZBQUxlRnVpSWxURElJb0VKTkpCRWtaenVTREJFamhaNjhWRUhEdTJ4ZHV1TzZRN1BuQVllUTdmdFhNS0NTTThwYW4xL2NIdWJuMWdjZU12L1pmTlgvZ3hBUGZXTkZXZmo5N3c4OG9BVC9SNi8ybnZjLzdsWThlelgzS2g5dDF2NXFNR2xPSlN0MWdjMTJLMzRaU3Z1QmluUCtNUm5PNWF4WGpMTWZuNkltSWRDVGlCVXFBR0doZFVReE93Y0VOeFNYR3BxemRjL2w2TXdpSzhGWUJJMlFBNmtVbDVSUEJhY2paN0REUkdOZmFhU1M2aUdFRkFWdE5MVURKUU1GRWdXR3NiQUpWeUVaYXdEc0JFRjJRa0tiQjZhYURyRTd0cDUxdWJBMjk0M3lFY3ZPRzRrcExTU3BlS2ExakRCSjJseVVmS1IyLzZvSC9nUjM1MTg1V3ZCQUk5K0h3clVqNHZEREM4M21WR1hGMHVQT21jTTM5c2VOSi9mMEk1NSt0V3M3RFY1Y0dadXNYbWhvMnJvL1k4NXlFNDQybVB3Y3JxR3ZLaGRaVDFBUm9ERzJFUjRLQVhDTzVVcVRsL1ZoaWlDeHhFbEFvek80a2l5UVVYU0JtOGVxK2E1cEV1ZDBYS1pxVld1QkpCd2xGek9ob0ZxWlhIdFBDVXloR1FaWXppQm9BU0FCQ21KTURENXhHQU0rcWNGTjlLa0JnR0c1WUsxWXFhS0VRcEx1dUlsYldlRzVzTDNYamRRUnkrZmNhdTc1VW1rTEx5bEN1VEVUTjhLSC8wVmI4eWZmbDMzM2I0dHRzUDRFQjNKYTc4dkdGVS9zRU44QUFPV0t2WWZtVFBNNzd1eWVYY24zMTBPZTMwcmJJMURCT1lablBiS3NlNWRza0RlY2EvZklKV1Q5OEh2K1U0ODlHWmFCWUU3T1lJallWZUFCWkJEdEJGTHk2Vk1FQXZsVE1iQkJUQVBVd25qRldzS1dHRVMxZkVLd2VDZlNNOXZDSGlpZUdsQ3NrS1JNc2xHQ2haNUgyU3dFeUF0WUEyeUduaDRVeU5zSU9JWUpOcm9RM1FaUkhOUFl4VFFJVnowS3dtUEwzY1ZZcTRzak9objVydXVYTVROMXgzR01ObXdjcHF3b2ppSytPazdQR1Y2UWR4dzEydkszLytiYThiL3VEVkJzT0w0SGJsQ1hUZlovZ2dQc2VHK3c5cWdDM2t2bURmby9jOE0xLzRVeGZvMU84NGJiR0tkWnNQSXZwaGZwVGptWjJmK1crL25DYy83RUtPZHh6RGVNK21rcFBJZ0JZRkdMTTBkeWs3a1F1VncwR2hpQ3FDRjVkeVZBcWxTQnpEQUN1ZzNLcFpxQVJ6TEpvZzBHczlBSTh3S2FubWhnUVEzcXA5cStiRXdXYXg0ZHdjaHZDRzhBU0o4UndSTUtRb3JBME0ySnRSVHpNOEpRQ0FSbWY5TXpId0hwSmxXWmpMU0FJR3lhWHBOTEVBdXZValIzRDN4emJSbWFsTElrWU1PN0MyY2lnZDluZVY5L3preTJhLytsSUF3K2REU1A0SE04QTM0NUx1S2JnMlg3YnZ3b2Q5eC9qWTMvNmlmTmJEMXN1ODVFVFlNSExtaDdqbjJZL1U2ZC8rZE9zV1FyN2hIbmdwUUhacDdzU2l3TWNDRFE0VUYwYUhpc0lBTTRqaTh1b041WkUvbFNLbXJEQ1FVa3NBRU40cVloY2NCbGVGbDlFQ0t5SEk0SXhMejZpb3d5WURWcEVZQlMxRklISkFGWUJtQkloU2l4RFNsaVdCeU1EVUNaQWRHOExUMGxNUWNndjNSNlFvV0l4VUNjQVAxZFV1LzlNRkdqRGRNZEh4WXd0OTlBT0hPYzZjTzFaN2pGNThxZ2tOc3ZlTzczbnpMK0xYdnZtdStkRmJEdUNTN2twY216L25ydTB6ZlB4REdDQlZxOXdmMi8zVXIzOVd1ZDh2UE14UDNudW9iSTdvdXE1c0hZR2ZBcDcyL1YrdHZVOTRDUEtOaDZCNzU4VG8wR0tVNWhsWWlENFVZQ3pRS0dpVU1CYkFSUlhKUjlDTGhPSWhpeXFRaXNNZHBDTnl3M3JkNVlTN3E4VTVDWkFZK0Y3RlNOUUtWYkZWSlBCcWxZSW9XaGl3NkRWVWhyc3E5U1ZJeUNnblZURklnOWVJYXd3L0pnSzBtdWVSTG9YcnM0WS8xbHlTUzh5dzJRdTkyWTRKTGlORjd5YUVHL0N4RzlkNTdOQWNmUjh3ejBUZFlsVXJLeC9RQis1NGJYbjk4NjZadi8zYWFvVC9JSG5oMzZzQkhnRHNDa1JFKzZuZHovcnhyL2J6WDNyV3NGS09wM0VjNVN2RC9FNU12L3pCT3ZQRlg4L3B2Q0RmZkMrUUJSd2ZvSTBGdE1qQ1BOTkhRQXVIY29GR0Z6T2tMSG91UUJZOGszREJxNkdwbU9TMW5NMU9GbFNvQkdoVzBhNm5xNmxwQXVkcjhkVXIvY2VsRVJyekVxdUpuSkExZnhNc2dPc015dUwzSU4xcGlHQk9XaURVd2U2Uk1CbmRHTDludUZWbkpWZ0FnSVphTVV0dTlGcll0MGMxL01vWVduRFRadW82OEo1RE05eDl4eGFNUUdmTVJjbzd0R1AxWTd4bGZCdisvUHRlZnZ6cW43c0tWNlhMSytELzkvbjRlelBBQTRDOUJIQmRndTZWZi9IYy8vNGxkdTYvM3JYZ3NKR2NsalBYODExcHo0dWVnOU8rOGFuUVIrNkVianBDa2JDTkJYUjhBZC9NMENKRDh3SmxWYy9uOEtFSVdWSnhvamg4Qk9RV0tISjJ1S0lTZGhmZEtmTkNGSU1LQXNKVE5jQW9MaVFFVTZJbVU0NG9iZzdXeWlRc01mNk1EYkloS2o2OUxFSktQS2s2TDdoQllvVDFDTUFKSU1OQUlSQXBYdDRpM1F2R3hHcEJITjZ4MVNxTytqSWlRMWNUckdIQWtDWm5CY0FGdVl2OWhKelBNKzY4YlV0amRrOGRrWFAyTmE1aFp1djlxeGF2ZWVudnovN1BpLzhoS3VTL0Z3TThnQVAyRWx6cDJvYzlyeHUvN3FvbkRtZDl4UWJHbkZNaVpodFc5czV4eXN0ZWdCMlBmU0R5KzI0Qmo4K0pJd3Y0NWx4Y2pOQkdwbVlaV2hUNG9raVo5TkdsMGFIUnllenV4Y0VpSzFsQ0lkeFZ3N0RnRWMvZ0RqRExJbklpcUxpcVBXZ0FYRGpEQ0hWUlpVaGcwTGFSRndJQTZWRzkxb2hzOGpDUlNOK3M0aTZsRml5R21qNmFRTWpaTEFTUTFTUVF5UkI1SXFvbkRDZHNOSVVoMXlMWkNGbncwTFJXL2JnRFpLb0ZUNURiY29oUkJRSHNnRktBdXcvT05KOFhtUkZER2JWWHErTW0xbGRlVTk3OEgzNW45b3AvVnozaDM1dXk1dS9jQUEvZ2dQMEVYdUtQMkhuNnlUK2hKNzc2Q2VPWmp6dU8rWno5WkRwdUhvWWV1S0l6ZitQN01OMjkwOHBmM1FxWFpFZTNpRU16bFBrQzNCcWh6UUtmWldEaDBsaW9rU2lqNEdNUnNzQXhKRk1NMEptbFFDcUtURjBoTHZBU1ZXOFM2QjVLdmdheXFiRFdHa2FLS05YaFZYUVpja0lHTXNKZ1JHdEdPcW5nZStVRWtpOXhRVWh1VUF2cGhKdmdWYkFnd0VpcitXTU54V0FVUEZINW9vSFNuZ2pDV3IwVU9LRjNkQWx1NWhVRWorTmhSUUNRdEtROVFxMGRIcktTTC9jY0hyaTFsWVBnS1RQdHdGb1pmWno4YVg3YmYvNjV4YS84NE4rbkovdzdOY0JsMkFWMi8rSEsxNzd4VWovN3NmZjQ1cXhmWFprTTYzZVNUenFINS96Njl5bk5Nc3BINzZER0FxNXZRVWRuMUwwTFlUNVFtd08wNWREY29VV0VYR1dqWjRmR0lzOE9LeEN5ckJTWGlsQ3kwSFJRTHBNWU9abUt3K1pPRGFWZW5HQkx2SlFxZXhGOENja0lLazFYRUtVSTNDenlyaWdtY3J2RU5BSVdJZHVNN0UyS3EwMTBpV1FuN3lpWGhhVExBS0lMUFFMRDJ6SjFSQ0xRbVdDa0I3TkNyK1dHQXVRR3pBU1ppUXdQSEo0eWloMElqZ1RGRzZCUmZRbzNDMEd3RklkMjlOaUFqWTBzODlHekZVN0xTaWJMOUMvd1oxZitwL1ZmdnVMdnl3ai96Z3p3QUdBdkJmMU03TnIvY3p1ZTh1cW5qdWM4L25EWkhOSjBwUisyYnNma0dRL0c2Yi82L2JCYjcyVTVlRlE4UHFjMjVzTEdIRG8yQjQ0dEl1eHVEdEpjMU9EUVBNTUh3Yk9nd2FIUlVUTEU0cEE3SkFNOGdkbUl4WWd5RE1pTExXa3hDc1ZaZGhTM2xVSjBob0prMEFSbUxrNG5zRjJySENjbUpZUHRYNVh0N0Uwcm5RZXJJWklHN0p3Z3JhMEFJMWhLa1ZZRm1La01oV1hoc0VKcExCeVB6SUhqbzdodyt2RUJaVVBNWlJUR1VqMHhaY2lBT2lvWjZKMUlXSjZOeXVzZzBZdm9wRWtpVjFlUUpsT2tycE42TXhsVVJMcEtGQ01NZXdNQW1RRTFONDI4TUJRMlJJSUNKeEl0U2lDbmMzT3phSE56cnB3Rk44ZXFKdGt3bTE1Yi91ekF6Mjc5eGtzK29URDVPMEZxL2s0TWNGbnRuc2ExMXg3L21qYzhVMmMvOGU2OE1lU3VtNVQ1bmQ0LzdTSTcvVmYvTFhURFhkTGhEWEEyUUlkbjhJMDVzREVITmhiQXhramZ5STVacGdiUjUwVStaR2h3K2lqNDRFSVdwRVFYbkpzWlB0K2tIenVPMUdYNHFWTm94dzcyano5YjNZTk9LOWk1MTlLNUo2Rzc4R1RheVR2QmFRZExrNkRLWUxLdWg1TEZ0eDFDTDFCVC9SUE8xU2Vlci9aN2Z0TDNnc0Voenc2VVFNYTlGTWtWMzFOU2RwUkZBUmNGa0hOK3kzSE1Qbnd2OC9HWlJOZDQ5M0ZzdlBWZWpuZHRFTVdWN3hDWU9uTG5HakNkaU5PT1BoYklpbWptbmdMSFVmWFFNZ3RjeUt1VUVZQ2ltVThlTjVXR01lUDRlb1lYR1ZCODFkTllPSisreHE4OThPdGJMMzlKODRSQ1pYbyt4N2J5ZDJHQUZzZ1p1MWV2UGZ0MVR4L1BlZW9SYmMxSzE2M001bmR5K3BRSDYvUmYvMTdZRFhjakg5NEU1d3ZoK0F3NHVxRFdaOURtUUd3TXdFYUdiNDNTUEZNTHdCZUtpbmNzOUVHQWtud1V5dEYxK1BGN3dWTVR1Z3ZQWlBlc0I2bDd3UG1ZUFBuQlRIdDNDWk1FZlB5ZFN3ZG85M1UzdTBNRTNXc0s1WTJXalpmZ1VqTlZBMXdFNWlxRHFUODVvVmFnR1JWSXpOSjdOTXpPV3BteGJiU3R3bW12SDg4dmdJOEQ4OEVOSEx2Mk5teGRmMUJIWC9jeGJ0NTZGT1Z1YWJwak43cjlxM0JHMzB1RVgxdXlOTlhpZ2c2c2pBd3JvK2oxNkJmWnNiRXhocXdiMmRkODZsdlk3UCtQdi9IN1hqRjcxWCs5REpkTnJzYlZJLzRSZU1BR011c1ZLOC84dmEvbEJWOXplTnlZWXpycEZwdDNHaDkvTHMvODdSOERiN21MZnMrNnVEbVhIOThranMzQjR5TjlZd0ZzRHREbUtHMDVzQmhadGdvd0NENElHa1pKSFh3aGxrTUhWU2JIWVk4NlJ5di83SEdjUHVPeDdCOXd4dklFQ1hFKzZWbkIrMGFSNklrRWpkV0M4SEg5d2lFeXFJQmdzQnFpWEE3U3JOSVRDQ2F1Q2hMa3JtUVdsYWMxNDNFWURGNy9qUkJPYnozRWNtZXpNQUd3V3Y5RUswQWNmYk5NTU1uUzBpRFpqbTN6K3J0eDhEVWZ3c0hmK1pEbTd6K3VaTHN3MmJjVGhXQnh0THd3U25RbEZIaXdObVF0NXFVU2gwd1hNQnN6RnZNUm5jdEhaSzJXRlIxT2gvdmZ3MnVmOTlyamIzejVDL0NDL3Bmd1N4bWZ6MXp3WCtEUi9XUHdydkhscTAvNTVhL3hDNSsvWHVaRFNkYjU0b2o4b2J0NStsVXZZcnJ0Q1AzSUJyUStFNDdPZ2ZVdGFIME9iR1pxTmtKYkkzeVd4Vm1tWmdVK2QyazkweGNBMENQZmV6ZG85eUJkL25oTXYvVXIwRDNoSVZoNnMreVFPeHZ6WUdhc0xFY0ZQYXg1bzBxWjFRTDB4Q0JiZ2I5V21IcTFMTUJodHEyWHFyUWI1SktSZEZHQ2d3elZOT0NnVlVPUGgzeUpIRHRnb1FpVVJKZHE5R2VqWGVUbWdCc0FSZnVuZ3VXSmU4YkFWRjlVam52ZTlHSGQrbk4vZ1kzWDM0RysyNGQrL3k2TmVhU0xOUzlNRkJSZTBlcDlHUXlQRTJBUnJMaGpudDN6RUhYSFdFYnM1bzU4YTNjYlhydDQzZGUrYnV2YVYvOWRjTWVmTXdOc3dvS3I5MzM1dDMzbGNNNHZEUXNmWm9aZTQ1YksyUXVjOW5zdlpYOXNKTzY2Vno0ZmdjTXo2dmpNdFRFak5rWnE3dkRaSU0wek5DdmtWb2JteFgyZXdWbG41ZWhjNWZoTjBKZWZ4NTB2K1diMGo3OElRRnhWWlplbFRwNHFYTnRpWnZ3ZkJmZjJqUVU1NjlYR1FxbnFrRnVWaENLNFl3dXJKdDBkWm5KM3MvaGhOV0NnYXFhcUtyb1dxdUV1RzdmVzJOb1FvWXBlRk0xMFZRd0RSeGlhQlRXaU9EQXduS3VyR24xOVkwcnRYZ0s4REpuV0pZWUdGamo4cHV0NXc0LzlDVGJlc2VFN1RqOFZCVTVIR0tFVTJPVVNkNmRZcW9CTUFuS0lOVGdiU25Eajdpb29lVTlhUzlmamhtTy81YTk5M0hYcjcvem9pL0ZpKzF6Mm0zeE9ETEFaMzB2M1BlNUozemhlOElaVEZ0TzB3ZEZHbFRTa3UzajZLMStzbGYxN3pUOXloNUFML09nbTB0RUZ5L3BNMmh5Z3pRd3VITDQxVWd1WHp6TnRxNkRNUjFGVCtoMTNZZHg1REtzLytRMWFlZjVYQVFBMUZzQUlKcE83aDA0RUZBeFJKaVkyanlPRHgwUURCQUMzREx1UmRWVnlMSVNtN2I4ZENDRGFJaGRVR0Jab0VSM0RPQ05pR29CaWpoUXZLSy9UT2t5TnZ3UENOcmxkMUJnYVVndzRhaGNUQUZJdXRSNnArQnVLVmhtVlpvSE5ienNBNVBpZzFobktMT09HbjNvemJ2MlA3OGJPMWRPa3RRbmQ0NDBMSWpJMGJqdFg1TjBobElvdnpjZWlvU2drYWNqRldNWTl0bnZsTFhybm4vK0hZLy90RWtGenRnLzBPWGo4clNjekhRRHNNbHpsLzNMWG1TZDk1WERXVlNjdkppdnJ0cEFzMlh5OEhmdXUvQlpNempvRjVmbzdoTmtBSFY0bkQyL0NqMjFLeHhmQytnTFlXTUEzUjJCV29Oa0liSTRxVzZOUWVvNDMzaUE5dXNmdXYvaXZXSG4rVnhIakNPVUNkQWJhOXVHTEViR3FhTGdLM2NScWZKUkVlZ0ZMa1pkQ2VJYVBHY2hGTElMbjRKWlJITXBPeXc2NkUyT0Jja0Z5Z0hJcXV6d0RLSUxIQUpqSXJqSlFja0hKVHJqVHhzQWs0UUt5b094Q0tVQXBSQ25BV09MN29ZUjZ1d2hlRk85ZFhDb2V5dTZBRTlXS0YvTGpyM3NRZElFdGxxR0FuZUZCTDNrYUh2cTdYODM1Nm1Ia1l4dGdTbkF2cldKU3E1N3ErQWZSMitzS0V5TzZTQWRDOUVQWmhtL01uNEJIUGZiNXU1LzNTd1IxQUFmUzM5WnUydU52NndINVpseVNub0pyeTJ2MlBQTVZUNStmZHZuaFBCdHNPa21McmRzNS9aWXY1MG5mOVJ5TTc3a2UzTXJnMFJtMU1SUFdGOUR4QWRvWWdGbUc1a1VhUU44YW9ZV1RXNE5zWVJ6dStoRHMrVS9TMnMvOEc5anFoQm95MktkbERicThCd1Y1VlRsRm91T29TaGdLRVFEVHBJOW5mdkpuNW4zOGZPbGRERTJYaXByQ2IxZXBIbjJYSHdkTnBQcDgyNjVzNy9PMTcrUG5ud1R4ZUFHVVBTSXZnWlJTVkUxTGhwaTE1RUdqbENzWTcrcW1IZFp2T016cnZ2S1Y0cTFtM2U2ZHlPTUk3enNvMGQxRnI3eXlTeXdJRGFRZ0RTNE1JMWhVaXBuTFhiYWl2cmh0OXE4ZC92aTcvK2ZzRDM3dXFzOVJzOVBmeWdDYnB1OC83WG44OTMvbjRrSC9lVDdPeDJ6c05CNFZIbkVtVC8yNWY0M3k0ZHVGelFWeGZFYXR6NFdOQVZwZkVNZEhhS3ZsZkE0dEFOOGNvY0hKc1JQdnVvN3B4NTZEeVUrOE1Fd3RPOUZaRlNuVkUrOHVzb3J6WUlLWGdEYTZ0TFFKQjJDTGhTOCtkRGZMclllSWNndytacVJqTTVUYk4xbnUzUUR1V0hjY1cxakpHUnJuMEFnblNsd1VtTE5Bcm1Jd0VTWlhTUVNNbVNXQW1rckh5YnFxcFVra0U0VXVHSktVaUs2SDBEdFRKM2FkYVJlQVUxYlJuN3NiM1JtNzBLMU5pTDZIVGRkZzNRUnA3MFFyRHo4RHRqclpQdUdPS0xLTXFLMHJ0ZXNZN2g0dHAxNWRtUzh5dW1uSDJjMUg4WjR2KzAzZ3ppblRqalVNeWVGR2VRM0RMc3FETXFkQUZna09hQ3pnd290QzRBTVVaTzdMYStWanV0bC9YNi81MGorZXZlT2RuNHQ4c1B1Yi9xRUFNN3dsLzh1MUJ6enlxeFpuWGRrUGVUaVdjdGNKR0hjVW5QenZMcGZmZkpBNnZnbHNEdUw2Z3R5WXc5Y0hZbk1RdGpLMU9VS0xMRzA1TlFjeEh3Vk81WGQ5R04zM1BnM1RuM2doTUdRaEdaRmF3c1lsTEdkV2M2NmNCUU90U3lDZ2N0ZHhMZDcrTHROZmZOUVhiL3dnOHZwUnBvOXRnZXVqUmhRS2hoNkpRcEtRUUhRa0VvU2tHR0Znakw2MUtwcUNWUjJMSVp4TllDV0VWU3d2MVI3MndrZ2FDNEFNWUFDQUtwMGlGTE04NEVBMDIwSGNnaXVIc2N0QWVzVnl1aFdqUFdDbitwUDNZL2V6TDhES3c4N2xya3N2Z0hVUi9WUTh2REVKdDZoN3ZBcHZTYU5OTytSRjF1cjk5dktMWHYvUCthNHYvWFdralFUdW5vZ1FEVlJseEVrQXBnb2F4ZGxsUjZDa3dNd2REcE5oblJzNkgrZE9MdUVUWHZaSGVQc1RMOFlIQ3Y2V0RNbmYxQU5TMFVTa04reDQ1bHUvWW5iU2w5eUxyZEc3dnN2RFFheDl6MWRqejVjL1F1TUhiaWZHRVR3MkZ6Y0grdFpJYkM2azlSSGFjaXJnRnZoV2dTOUVxSWZmZFNmc3VXZGk3ZmYvWDJBc1lLckFTZTJ6aU5LMDZ1OWNjTG1zUzNISlgvczI1dCsrUnZrdDc0WGZ0ZzVpVmNSdU9qcDBOb0g2Sk5Db0VqV0hvM2FWZXpPTHlCcWphQ1pncVNvQTZ2OUM1WUxXSUNRU1ZZRktJTUZUQ25rWExZZ0h5VndHWnhSTGdlNUJ6aGp6NFI1aUNLZUZHVlMxalZoSm5xM0NyRG1nbVFBaFBXUVg5L3lMUjJMLzF6OEtxdzg2T1o0N0ZuaWZ4Rm8xR1F3bFVHNGFvVElVcEVuaVBXLytNTjczOU4vRDZ0NXpOS1lTb3RZSXdaUkNyRkFrdUJNQ1dWd29uWHlRVUxJRGN2UXFNTGRoQmQzS3EvMVBYdklMcy85MTRHK3JJL3diR1dDTC96KzM0N0ZYUGw4UGZIRWU1a014NjRaaEU3am9aT3gveWZOUXJyOExPTDRnNXdPME9RQmJHWmlQME1ZSWJCVm9ub0Y1a1crTzFNekJ1VEZ2emVXbkhzUE9kL3dQcGxOMlIxYVRiSG1NeTZSSkFISUIrdWlybVAzUC8rUGxaMStKOHU2N1NleUZZUzlzdWhxcTRpbzRnR0w2UWFyd3N6Y2NCTkVyQ1VUL0wwUkZNOUR5OTJnQWJoMjNJYS9pNlRCanVwTVdLcGY0R3pjQ2J2Rkxoc0N2RUlKWjlCalRRcHdZVVMrS1lEZXF5aGhESjBnUFJYUWlMUUZPK05ZY1k5NmtuelJxNTNNdXdKay8rSlZjZmZCcEtEa0ROTEcyb1BneWNmVTQ4T0t3UHVtR2wxekwydys4QTZ0bm51NTV5Q2d3bEVpYjZRSUxhdm9jdHpWQmVhWTBMMjRxSGpKYnVxOXBpa04rVjM3NStIdGYvS2J4WGU5N01XQlhmdllOVGdEK0JsWHdBY0MrSGxlWGI5MS8za08rek03OC9qNlhrcTEwVkNZbUEzWi95MVBoZHh5QjF1Zms1Z3phbkFOYkF6QWJnYzBCbUkzU2ZBQVdHVDRiaUNHRFl3YWM4c1d0V1AzRjcwVTZiVzlVYkJGMnRXMSt0V0UzakEvNSt0dXg4ZHgvaS9rMy8yZnczWTYrZnlEUzVDUXdBUm8yeFhHTEtpTllpdE5qQ2dZZGhMdE1TMldwVEY1NSt2Z0Jhek80VU9FWDFBWVFWRGlYallDTEVzY1VnREpWMnoxYnQzQWdidkdtQkFNS2RxOXFWYkNTZUZaMTFsR3FSd0pBaVhRSFNwR0dnY29MWURWaHNtOC9WcmRPNXVhdlhZK1BmTWwvMDkwL2Z5MVMxMFdxSUtjYkNBZHBIc29aQWt3R0x3WDMvOUVuWWZMRk81WHZQVWFyZ2pEQXE0N1JsV3FJTVVWSWhzUUVzWThHQTNoTVc3Sk56WFYyT212bHNkTkgvNzhDN0dKY2RWODgrZCtOQVY2TXl5Z0F6MWljK3g4ZXNsamJzYTY1WUIySDhiQ216M29NdTcxcnhOM0hZRU9Xenhid3pRVjhhd0MyUm1BclE3T1JXaFJnbnNsRm9SWVpVZzgvY2h2cytWK0t5ZE1mQnl3R0lDWGd4TUhocW9NeDhpajBTY00xZjY3TkovNHJwUDk5aTArbUR5VnRqY3d6Y0J5a05seEljb3ZPZEVwdDBxMTdjQnhGZFM0SGdSQ3BhbWswQWErMGJpV0c5Rm0xamE3RlpBbVNvVFp3VnBDT2F2MlZZWVRSdlFRUkFjWkZSM3VVNTVITmlncTVLa016M1pEcmhpQ0tqR09JeW40UmZhVnJPMC9EeXVJVTNQR2RyOVpOMy81eUlsbmNEclc3TDFnVUJNQm9ZUi9za2g3MDM1K2xoUjJGbFcwS1NGWkJjNEJKYUpncUNDZ1I2QWltNkI1QS9XM2EwR0o4dUYvNHJLL1o4YlRMdng2WGw2dHcyZDhJMHZ1cy91Z3FYSmErSGxlWG45bjFtR2Mrc1p6eTFiT3lHRVVrWDJ4SVorN2o2bE1lQnYvWVlkQmx2ckVnTmtkd2xzSFpJRzBOMER3VGkwek1NM3lScFlVN1I0R2JNd3g3Wmx4NTBiY0dqR0l0N0M2dlkveG56bFRmYy82YXQyRDJyTy9ENnNIVGFaUFRvY1dtb0V4dmhGUHJRa2MxcExCR0tqeFVlOUVvbnVGTDZVcHRNVnJxU2JoOGMxWFVvODRYOG9ZMFJuZWs0RnpxRVNKWmpaK3E4ZytSWkNHVXJTRnBiYW5FMGxQV1IzUjZ4Q3l0UU95cXg2MkdIVDRMeUhtZzZOaXgrendjLzhYcmROUHpmbGxOd0ErNFpEVzVyUUNpSllObngvNHZ1Wi90Ly9vSFluYjRIcVZFV0VoMllmV3pBQjd5aGFab2RLQUQxWE41NG1Ba0ZocTRSenZ6bC9oRERnaFkreXRjZEY4UTErZlVBSGtaTHBMT3c4cERzZm9menlDMHdOd29SOVlHZHp6bjBlQ3drQi9maEI5Zmx6YTJ3RVVtNXlNeEg2aHhwSVpCV2d6Q2ZFRnRGV0F4UXVyZ1d4L1R5amRmcW5UMnFZNlNnYjZMYmlKSG13WVFCVW5Yb2J6bDNacGYvdVBzNWc5a1RoT000d2FOVHFySTNBazRyWDdWZmtZUzBiRk9GaGhFMGhHa2lVaVVTTC9sTkJRa1pJWlJaaElacEFQSTRTMVIyK3JnOVQwRndoV3R1MUg1VWxtTXBtUWtiVDhIeWhWT0xtUzA2UkhLa0RMb0RxcUFLS0xIK3poR3VvK2hKNWdrYUdKQW44Q1VhcXU3aTNMNnVJV1ZQYWRoNCtVZnhFMy8ramVSdWdTNDF5cWlqcUNwTndjalBPdjhIN3JFZmZXNE9CdGhYdEFWZVNvRm5YdE1IWWJYR1JHRnFncUpEa0owS1F2Rm5YVGFISXY4aU83Q0J6OS83OWNjdUJKWCt0L0VDMzdHZjNBVkxqUGlTdi9QeHkvKzVrZGkzMFBuUG81TVpubmNoRjF3R2ljUFBvZCsrMkZ5R0tDTkdXdzJTTXV2RVpqVlBIQ2VVZk0rZWk3VTFvQzhhN1RwOS94ekdFQ1BLVmJSTW1FZVByQTRrQXo1SXgvRDhIVS93TW5zbkdqQUtJTk1yQzA1Y1ZHaWdhTGV6VFdYYXptY1NhZ250QnBOSlhiUkJzZDQ5WHcxNzJzd3M3Zm5pcFNIQWF0ZTJPWHJoQkFxbEFwcS95SzQ0UkxxTExtaXpTTTAyZ1NRWWpwUkdJaHE2N2s3alVDU0lXOGR4M2owRG83SDc4UjQvQTZONjRlVnNtREpnRkprRG1vK3c5cmFPVGo2UzMvQjIzN3F0U0ZlclJoTGVOeDJ0WWxTQ25ZOTVBdzcrU3Nmd0hIak9GSXlJRmppT1A1QXB0RzhJRlhicnlEMk1MQmxEZ2FPekowVmxFZmxoM3pudVhzZWRQNWx1S294azU5YkExVDFmdWZ0d2Q0djlwTi9lUGRvUG1lRzNEVml6cFduUGxTNGQwT1lEY0RXZ3RnWW9hMlJtZzNRMWdETlJtSnJCR2FaR0FxVVhjcWpBRU5aSElTZThoRG4rV2RUSlJNcGJkT25sWU9QaUZvd2Z2T1B3ZzVOeExRQ2xiRStyWVhiMWtmajhrajIyNTBzQld0OFFqR0RxRGZocWpHUlMvS3VHbUo5NHpCc3F4TFBoZ010cTZIUUhsZGdCcldybUtsZGRWL21nU0E4aXVuMk10R1dqb1lFMWRjbjVXSkswbXlMVzNZcko4ODltYWYrM0ROeDFxOWN4ak4vNGJuYys5MFB3N2gyR0huekNLMmJBRjdpRml4ejdsZzVSd2V2ZkJNMjNuY0hVNThvRjR4VWhhMmFpb2NRZU1vM1BnTEYxbUdLWVlWV3p4M2hTSWh4WHdsTnZ4cWVQSUhxWUdoQ25BN2twcy9MUlg3T2ptZm1SLzRvUVYwVkJjbm4xZ0JSdmQrUDQ1RXZlSVR2UFcvbVF6WmpLbm1ML2ZtbllIck95ZlNEeDZERkNHd013R3lnNWlPNE5STHpEQ3l5c0JpRkhJSlNqRG04aW9qTXcraSs0ZGxWbzFGQnFSTU1YMTdBUGlILzZxdWd0OTBBVHMraDhpeWd3U282cnhWeURaM2JCaFNCdHVaVTBSWlVjN3I2TjNJS1pmbGFZSXdocXMrcHIxK2FjWUVNbzZZS2llelJpVmtVY0VmMWRoQmM5WGxMekh4WncxZjBVSzBGdm5ydVp0UU9wTVN5ZFFSNk5IWEJuLzRnTDNqVjkrR1U3M3lxOW4zcjQ3VC9oVS9FT1Q5ek9lLy83aC9BNUd0T3hUQzdrNWI2YVBkekJ3eVlMbFowMjQrL0doQ1lXTHVNNjRPa0VIYXBrNTU2SVNZWDdLUnZ6cW95Tmtab0dyWTdTQ2pCUEZ4ZXZGQmh4eWhNV3BkME1hWFJTM21jTG5qZXhhYzg0SUxMY0psL050dWZQcE1uRXJqYXo5MnpaOTlqRjN1L1o5ZGlWTkVpSVdjRHRyRGppODhYRDY4RFd6TmdjeTV0TFZ5TGhUQ2ZBNHNSSEVaeUdJR2NxVHdDT1F0am5RbzUzNFNkdVV0cnozcFNnTHBtYW1xVXFPUUVNaWtmM2NEc3BiOGxzN05SeHZVNk1DREQ0bzYxYWpScUZ6Z0ZGMXR6UEptaHlKaFZEUXFHQWxRRUxHRGlySnBIMGhDemMxdk9aOGdnUnFIbWhNYmMvcDdtMVhNb1BJY3BBejZLcFFnMVZNZlBCY3JOVU1UUXBJVHpWSUhKRWZvWHA3cWtNbXhLRDBvNC8vVS9naDJQdnIvN2tPV0xFV1VvS0VPUmNzSEsvVTdXQmIvL3ZWcDU5amxhTE82RmRZWmtFSE5tdjdxWFczLzRmcXkvL2FQT2xPUk41bEtoSWpPRFNsYTNPc1crNXo0SWk5bFJkQVpIcWJtdEhDeFpTUVdHd29TQ1RsS1NhTzVJWHRnTFNuSzZNM1hlYzFPakx1RFphMCtiUGVaN0NlcGlYUHdaZThHLzFnRGZqRXNTQWYxZ1B2dWJIbVFyWjgwOUY4Sk00d0oyMG03MnArNm43dGtFaGtKc0RlWmJDMkkyZ1BNeEZNeGpkZ3lGekI3NFhYYlFSV09TNTd2UWZkVVgwM2F1QWptREJNM01HMlZBTDRBQitYZGVoKzcyUTZTdHdkd3JmaFVXMnFwVVF3TktnNEtJVWxYYm8vbHFTaFNhditvVjJiemhOallTelpFbGNNSVlOTWhvOVM2eVJJQkdzdzZwbTVCZEo3UEVaSW5zRXBnU2FFYXI0UnZ5NmptcVp3NEVPOFlkb2MxYWlPbzJlZEJkWTNjSVovN0t2MkovOGs3bXhVQW1BL3VPMWlXbWprQktVSTZHNDdQKzQ3K0FKbHZPTEFnZWFsWTZldTNBWFQvL3Btb0UxZjh5WW43YzM4Rmg3bi82UmZMVkJaakY1UEpXTkJrUTV6Z0tkS3NobWdZaHhXZGJhc29scDdNa0t5Z1B3WG5mZE5xZTArNTNHUzZ2Vk5MZjNnQjVLYTUxbkkzVmgydnZDMWRMOGdXeWdVRFdYSk9MemhFWEpZRG1yVUdZalVxTFRNeEgrRHhEWTRFV21UNW1hWFJnZEtvVXVJcUtCT2VjZk93aldvU3FKVnNMWndDU1FXTWhmdTBQa0hneXFFVkYyNVlRaUNuWVNxR0dXeW1yaGxyVzVWbU9Oc01LZ2ZLekZpemN6dnNpMFVTMFp4SXlzbFNMblFqOVJIS1h4bldVY2pjVzVXWmZqRGRwTWQ2TWViNVZpM3lyajR2YjRlTlJJQzlBa3V3bVlPb0QvVmFwWGxKcXVXbTdjYUsxUFlaamxjVXhUcDl3Rm5aODZZT2hYR0RUU2ZqeWFQTllxZ1RaR1ZRY0t4ZWVpcDNQUEo5bFdBY3N4U2NaUjZ5a2ZWei8zeC9HMWswSFlWM1gwdHRBZm9oQXVnSHNldVFaN0hjSVBndzBPT2kxZGI0K1VoamM4bnhGMTNRUWhyRytMRWpMUk9LWXR2ekNmT2F1WitCeDMwWkFWK09xenlnTWYxb3h3bFYxYU9UTDFpOSs1a080OXVCNW1XZEFxWlFNOVluZFdYdmhoemRpU05EbUdQVGFJa3REQmtkSm80Z1NZd29VYUVmazR1N3dNb2V0ZFQ1OXlpUHJsUHJsZ0o5YW1FYmxXOTcxZnBSMzNZQ0puUS8zZ1VCU2JHaERPNnZWV2tzdFhsb25iL3hheUt6eTQ5b3M1VFhycDhXY1ZHK2tXeFRMSkpDbWthcVZvd0tPc1pRUjVkU1RwQWVlRE80OUQvMzlUNmVkdXcvRm5WclA4bGt4LzlqZHdzRWpISTl1QVIrK1Raak5rYkFMaHBPcGZnZElWWVdzdzRVWWxCb0ljUnZNQm1HdWJ1L3BnZWJKZzlPek5qSXBhcDJtaDVZN2FPRGtvak8xOVFjM29lT3VvUEVnWUFxa2RjYzlyM3FuenYyK3I0SzcwMnlwNGFjZ2xleFkyYlVUTzU5MHBtYi8rekQ3bmJzSkw3TGFWRlZxWDc0TFZhQkFGYzhraUM0QTBSQzB4aDNFRVpsRU56N0t6NzM4Ti9iaFAxNTI1TExqK0F5RUNwL1dBQytyNE9LanRPZmZuQ3pYT3NjSUUyWHUvZjFQUW1jcCtmRU5zQWljRjJvczBwakJYSVFpc3pxQm5uS3dpUEoyeHh2NlBPUGlwQWw0eW1sUm1aR0l1YmJWSEhLaGttRjg3VFdSTXlVQW95bytCNlI2b3dxeGpvMlZ5b1ZJQ3c1VVdzb3NHelhTQnRwN2hmK3JHclBpUGt5cllDa2M4czBxV01BZWZUYW1UM3lXVnA3OUpQQUJEN0QrdkpQYXFXa0d2dlRlN2QrU1hlVUR0NXNmdW8yTFY3OUg4emUvVjM3ZFIxbXdFNzJkb3RKUGFHV000MURjREJMQkpIUnc5TjFxSlVJQ3cwa2czSTFtOWVaYWp2UlZCWVduTmYwSVU0NUFYTlJqaXVHYUQwSGYrNVdrTVdUUGhtaTZra2R3bUFJckQ5MkgyUi9jcWc2N0xMTG8ydWNTUXdkRFpBTlVnWGVVKzY1U1cxS0VFWTRpc0RPbWpIRjJJVTUvd0dYK0ZWOUw4TmMra3g2U1QybUFWd0dKdUxLOGFQZlpqejNUSjE4NnFvaUFsUWdacWIvZlNjTG1BQ3lxdW5jb3dPREVrT05uZ2JIR1FKTGNJUHhndTJDZyt5YnNTeTRrZDYyNHhneDJxYW5UWTVaRUY3N01QblNEZ0RYQ005am9zb3FxMUEwS3k1S01RTXpZYTU2Z0luZHNwUTNWWUFpMUJpS3FBS2tEMUttVW16bXVEZWllODBTc3ZPQjU2Si80YUtRK0xyZ0RydXdmMTgzWlpuU293VEV1cE5RamZkRTVBczdCeWxNZnoxMXp4L0IvMzQxanYvUTZESy83TUczVFpPbDBvTytJY1FEWlVSS1NGd2dUbE9PSFNSSVdveWNSTStXcUNEK0dYcUZPOHdnYlh0OWdoeDZRWkRHYm44cFp2ZTNtNXR0dTFXTGp1RloyNzJrME0walNrT1RtQnNEM1BQcitkaVM5aThqQitUVjViYlFDQm54cWlESGFyTGRBOWNlSW9FK1VrSUJoYmtNNjJWZjlZVHJ2RzY0R2Z2TXFYT1IvWFNMNEtlUDBLYmlFQXZpSTdwU3ZQeGM5TThybzBXbE43cHdvN2RtaGNtd0xHRWY0WW9qWmZUbUx1U2lLalZJbDdvSzdROFdscXBvTGlmazZlUDZwWVNVZVVBaXNwc2R5SVNYNDVnejVyUjhHc0JQeXVpZXJmaW1HTlFmU1dnSFRodHFIVllJeGpTV3l6U1VJRXZrWFF3aFFnSDRGcGF4ejV0Y0IzL2hZN2Y3TFYyTG43L3cwVnk1OU5KZ0t5ampJYzQ1eEhSM0JMZ1ZQblZLSUpWSkNTaDFrUm5WOVRQRXRUaDh6TkJSd2FsaDUybU4wMnRVdjBpbC8rWk9ZZnNlam1kTk53UHlnckorR0Zoa09Md1dwVzhQNDlwczF2L2tPc0VzeGFLYTFIWnk0K3RNQmRFU2VEOXg4emZ2VmNZZVFNd0N2azVoR3NqTmdNNk44OUY3VWt4SHUyaEU0YWMxNGRqN2lkUGhraExsa2RZSVRhOEVSNldMa3JVQ29KMDJodms2QTZGSGhJN3dpak9DOERPVUMzL2VrTTA4Ky8vNE1zZXFuelFVLzFTLzVaVEUxTTUzai9YT0VqSUtjU01FMUtwMjhpOHhPelJiQ01JRGpTT1ZNamdFVDBFV1ZFcFNRbCtpYmlLcVBnYUVCam9WODV5azFvU1ZPMEFJMG1oU2Fid0liR3lRbklFYW9UazFXalh5cEdsSURsZXVaVmdPa2F6TEY2dXJRT3MxUlZRUHNwOUw0WWZqRjFJN1gvangyL3NaUEl6M29QR2tZb0Z3aWIwb2RRVU5LeXhiTHRnTXBYQU1jb3BoQ3ZDckFZY2xnZlFkT1VvQit4WmtYbWYwRHo4WEpQLzl2ZU1wYmZ3TDhpcE14ekQ4Q1NraUpJQXVZd0hRY1BIemdxcWlQNVNoRGpnSFU4dmhJeGVIakNKcnA4TS8vRWZ5R0kwalRGVUVGMngrVW9nR2NPN2IrL05ZNDZCaVpGZTBqWmt0QTNkSUtqRW11VEZVUmx0VUtQVVU4RGp3d1BHeUZvd0t1U1pRU0hWMzREQUdHQVo3UDEvN0pOd3lQdXh4WTdteis3QXp3cXBwMy9xZnB1VTgrdC9UM0p3WlBoa1NQV1YvZC9sWHAyQmFZUjZKNlBKWU1sZ3dVaDVkUnRkaVFwRG9leUJuSklnUVVHb1QrOUxOcUtXaENUR3IwU3FDQmdQejJ3OEk0WHliR1JnZVkxUURueXFleDdkZUlBZ01XZksrVzR1bjYzTGdRVU9DTkpQTDRIdmJmOVV6c2VPZXJNSDNtSlVUTzlGS0F5VVJMQlRZQVkwdkQ2d2xTM0FTcTQ5MUVoUVpQaFpJQ2U2dnFxa2lnREdtU3lESExGOWxYSG5NaFRuL0RUMkxQTHp3UDQrUjZZRmhINmliZ3NPV1Q3aFF0ZnZNOU9QU1MzNFoxWFVtVHJxR2FoQ2gyQnB2Mk9QeUthM0RrUmEvQ2FuOGFsUmVSM3BsSUUxUElXMlJJR0EvR3BsY3ZVYzNHbk1SdGR6clpzMXZUOC9aSWVRdEpZWHdtdWFHQUt1d0VUNUppZGJlWUFDVVVVSm1CRTBwZFVJc3NSVmFndEtLRWh3NzdudzBnWFlFcm1tcjZNemZBVTNBSkNlQkMyLzNVMHppeFFWNmlOOXFsUHFGYm13RHpJYnE1aGt6bUFzc09GU2R5RmowVUZqRUZzbmhBRUVCcjR4R2dRb0JuN2lHQUFEeUFxQ3hzeVlaUU45ekJiamJXRHAvR2NxZ0N4YlU0UkdqclJLK2NiSUZIc2luU0dUUEhtLzZ1TUZtQytSeU9ENm43bFJlci85bWZnSzFPR1oxMkhXZ3BTdUpRQlRMU2IyeGZ0Q2dEcWtGR3N0NmtUNGtKaVlraDRLOUZUeDBFR0FDYkFYMEhIOFBROTd6d09UenRqMTRLUFdBZFpYRUlObGtseWh3cjNSbFlQL0E2M3Y1Vkw3V05QL3dMbFBVTjVzVUNaWnhoODgzdjAyM2YrdDl3K0htL3dSMkxzOEtpcXFvbkJsT3JwalhSZUxDNHNZYmdqa0YzeEdjenBvUlNNdnZkVTA0dTNzM2lJMUpJaEVCdGgyRElrMVd0WkZYdUxIazlodTZiTWZjcjB0QUVwUzBNWmIrdFBQcHB1NzdvTWRGRjk2a044RDZMa0tmaUxWa0FUdW5TTXdCSHBwdUI3cVhBOXF5U2h1amxIVDJtRVl3ZVE3OXp5TkRsSHRQbFZhOFIydHc2RW5MUkM2dzM4TXo5OVdJRHRkY0xGbVZjM0xVSEQ0TndSbGRNblh0UytkYWFTMFpPeDFhS0NDQ2RCYXhRVEsxU3F3ekVrcndjWjlsOWlQMXZ2UXpUWjE4Q2pEbnd4aTVWWDFiZHJRTUdyM0N5eGRoYmQ3R1VLRUJyNEFkS3ZIck5OSkVTYUNSSmwwY3pTYzI5cWlkRG5VVnBMUE1SMHlkZWpGUGUvTk00K1BRWHEzemdMcWJKS2ZKeHdkWHUvaGhmY3hDSFh2TXp3T2s3cExRQ1lBRy9hMlJYVnJEVzNVL3VZL05rdFJLS0RqZWdRQTUwNk1VUEhvMGFLUm05NW10Z0lLNjVPSkNBZnMrRUEwYkJSS3R6NGFLQmZ0bTRUb2lLSWpFRXJDSmlrUFVKK1dLZFQ0WUNqYWVYZlN0ZnBIT2UrY2Q0N3p1QVN3eTQ5ajRWMDUva0FROEE1aEQrL2Y3VEx6ckYrb3ZkUjdGMnpRUE9ibWNQelFjaWoxREprdWZ3YkNyMGtCY0ZzNkFNcUpBb2pIRkFWUmxzSHQwWVZtQjdkMGlJNWlJeGRzdUVJWVlGY3VOd3ZKWTVJbnpuR0dwTEYxa0FsdHBQM2w0L0dBRWtGNjAyVTRZS2xHNmlzVWlyZDZGNzFjc3dlZllseERBQVhRSXRSbmEwdks2NXI1aU1JWExNNUppakNhcnZhSk9lN0R0eDBwR1RucGgwdEdrUG0vU3laSEJSZWNoTDhKdmJwNXFvazg4OVFaejBLSXNSL2RtbjRMUS8raW1VQjV2RzRURFJUeVJ0cU85M1lOcWRwWlc3OTNKNng0VDk3WHV3Wm1kaXNySWZybGw0OU5vbndDWm9OVmRJelNLUldSdzhRb3dGMFlVU2VKZTFsTEFpZk5aUDVaZzMxUzJBVUVYREc1TWpTQVZnQ1lFQ3hSUk4wZWdRWFhPcGtuMGVrYWJyUzQ4SDhPU240VEtrSzNETnA0UmlQc2tEWG9wTDdFcGM2K2VYWFUrOG4wK244RHhTNnR6akluZHJpVmpNWFdNaFMxZjdieXNjMHVaRXNRVGhRNFBneSs0ZnhZZ1N5QXJrQzNCbEVocVE1Z0REeVZRNUVHQjVyb0lSWUNhdHpaRW9hRHZZSXE2ZDRONmpRZ2t3a0t3bkV5U0trRlpSRnU5Qi8zUC9BZE12ZTRJd2pzSmtFczF1amxhcXFQcXAwSUZsQjYwRCt5cWl2L04ybGplK1IvN0JEeUhQTjVnbFdYWVpPMnAxQmQzcDU2Ri94cU5rOTdzZnU5VWVBS0F4aDFjazZwZ2pxMUVkZElQWTkvQmhaSGZXWHA3Nnh6L2hoNTcwQThCdE03THJIT05DQkUwcDFkTlhKTTJKOFFTMHlaMmtoZWJmd25sWU9ISVJ4cnpZUUNrWkNXbUpGb1gyMXBmMFVMOXpGNGdCbEJsUjRub2k1c0dxYXRzaUJBYlRHVVlXclZVQ2xBZzZCWk5Zb3BlQWp1d25qVHNlL3VnL3ZmOVpCdDRLSEREY1J3dm5mUmpncFE1Y2l6Tjk1WkxreElJNXhyTkx3TVRBanVBUUU0L3BKYVovMTMwY0ZoOE1qUmxUdlNmQ09FeldFTTRtU0twVVFQTVNCT2htcWhOYllCb3Rhb3hsOXM5NlltUExRU01JYWxvV09HRHRMRVBOVXlDaFgwT2Vmd0RwaDc0Smt4ZGNUaDhHb0pzc2h4cTVuSWpLTU9DOHlqZW5ybFBabW1QMnl0OWp1ZXIxS20vNUVMQWxFcDJFRGd4Z2dnVmt3YUFSV2JOL1I5cER6a2IzdFpkaTladitPYnB6VDRxa29XVEVEcS9vQ0M4QVV1WEV2T3Voc1dCeTltbDI2bS8rc0E1OTJaVXlQemZrdEJETVM5VDh6VXVyK1hWNU5RSVdPUkRUNWVxcGlzbzh0L2FDbHVSSW9GakhEVWVnNlU5WkZaR1oxQVFkSU9rb0FNS3U2aHVTMnhLemRwWnJuN1pSTUJxTEQwUXl6eXpqZWJaLzdVRmJwejMxWGJqeDF3L2dHcnZ5UGhxWFBpa0VXMWdwVnpJZnR3MHlLSURKbFJSWm5ZZW9RRlZZRURScXUzTWFPVkR6amFCckVHRVNCamhrQlNubWVDOEQwekk3Y1RTVUl6SXdHR1dDSjQrMGtvN0F6aVMzcFlSZW9PQW1nZTVPRjFpcjdpN0o1M2ZJSG4yQitwZThpTWc1Z09mdFQwNGFXYmxOZUNreUMzeHYvSjNmNTliam42bnhXLzQ5OFBwYjJXK2RnOTdPUjllZmk3NC9VNVB1Vk91Ny9laTdrelR0ejhha3V6Lzc0WHp5dWtINXhhL0ExbU11OThWUC9qdzBMb0N1RTBwcFJDT1NCYWl4dkFwOWdzYU0vc2tQNVk1L2Z6bUc4U2F3NndFV0xJdWVLT0hVcFBNQmFSWUZveEszZkd1b1FpMzNUSEZ0cWsxS2lPNll1dllKQUpSTzJWbHhtYmp4TEU2K3VNVDhLTnV1UHBCWWgva0xqVnZ5MEFzR0JGc2dPb1UxNy9sRk92T2hBSEF4VHQzR01UK1ZBYW9tQmY5Kzc2a1BPN3V6TXh3THR5b1FnVHRUVjRtWk9vQkZLaFYvS2dROWdGMzNlanlGUWlITks2VlI2c2xVZEZ4NVJwTVZOSUtwcWtQWitNNmlnZ0t2STY4Y3hycHdPdkxKS3J0M0NKbGd5S2pjUEJpUDJqSU9aZU9PUSt4LzlhZXRtNlpJRTVPZDhNRXJQQ05RWTlYakhUbk96Y3Rmd1BrM3ZBajllOEhKOUdLa3lUNHlMVWh0QVhsRzVBVlJCaWhub0VUWG1zcWM0QUpJVTZiSitiQjdUckhaai8wNmpqeitNdVMzdlJkcDBoTmpzVmdjSjZORk82OTU5S01nSmFnVTdQbmh5OUE5NjBLTmk0TmlkUGVTMWM4MmhRNmh1dGdoV2c0TUJWYjFqbEdodWd5aWx6bDh2Z0NBQ29jQlMwZFVid2ZiUFhHdkM1QWp4Y2xvaFVXVVdvVUs3Qy8wa2lFaGcxRktJWEZqa2lzMWdhOFhPb1pra1BhWEhVOEdZUDhjdjN1ZmVlREhHZUExOWZ2VHhzbEYrenZiNGN4ZUsvUElLYVlHY3lIUTlpeVRWN3E2b0U1WE5DMmxUWUNaUjVWcXRaT1dMcGtMVm1vaFl1SDRGRUcrS3FTaThRZUl5VUlZNmdrTk0vRXE5QXdEYzRHaXBhb2JOOEhvcUVzVWhEUWxGeDlGK281L0NYdjRROXdYQTlSMVJCdWIxYUk0NmNoVitQcXU5K1A0WTc1YXV2cWQ2bFllUkUxWHFYSk04RVc3NDlVRXE2MVlVcVZZVGFIQU5zL0FzQUZYQWJ1THhIYzdqanp0bTdIMWgzOHNtL2F1UEFhSVVZSGVJTkFxMXh1Z1BFLzY2ZTlVM3JFdWxraDk2NTRsTjVUQVF4VW5LdFFiVlVmUGNBVHQrQXdPK0lBeTFNOWJ2UzhiVjFTdnZ1MHlkYWpOVHpHSHBNRm9sY0tqVXFYaHpGdkNMNW9LV3hPVFFVd2VuWWVxUHl0eTdoeW1aNTkwMGtrN0dqdjFhUTN3VWx3bUFMaG91dk5CSzdJd1BkVEtrb0IxSmcrR0k4VERxdFRhc3BSb1ZZaHE1UnVHMFppTFNENEVtSm9rcUJZVGdHckxkalFtaEhrbHlUclV5aXdwZWtRc1FqcE5rUG15d1NncWsycm9nYnhBd3pwMThpN1pEMzlQQkk5azljSllIUkFZZUo5S01mWko0NSs5RzR1blhvN1ZHNFhKNUg1QTNsQncwT1JTcHM5WW1jbGxNaFZpMkNwOXFDM3RhczA5UUY1SG4zWmhaZjVBYkg3dEQyUGpGYTlGbXZaQXlUWEJzTlo1RVBvSU02Z1VUQjk4ZHRyNXdrdHQ4TnRocVZkZGJGeDd3OWx1QUVCZS9aaU1VcDB3MUc3WWV0aGpycjdlVmVvSk40dFJtZzZRMDVTaWl6NGNRT2daYTMrTFJLb3c1am5FdGEwdDh5QVlQVGdWSTB3a1RNQVFvZHVLeG5JYWQreS9wRnowU0FHNDdENmFsajdoQnhjRnN1VzZoSXcxRzZVV0RFaU1odThTeXdGcTBsYUJzNkM1c08yZHRIUnMxanhmeTJRRm9JQU0zSXpMWTJncE51cGRWTm1kU2xwVzl4T2V6eUJuc3lESnpRbVRvdEJ6RlJTcDc1RExMY0x6THdOT1Bva1lSNkl1RGd3anJ6aGg4Ump6ZHRNZDhHZS9FSlBqSnlOTmRsSGo4Vm9ZMWVNV0VMTk1WWFUyVlFyQmV0T3A1bVhOamFNNUdkTExnQVJnTlYrSStUZjlLT2R2ZVFlczc2aWM2NmUzK21lc1JrNUpqdDNmLy9YMHZSa29KZWFNMS9PMzNEZ1N5R2lORng0NjNBaWJhRGtndkVEanVEekZjVDVaRzB6ajc5Rkh1OVlKN1poRS9WMUMyeHE3YkY1RzVIK05HR2hvVjZTbUNkRVVXdTFoUEtsTUovME1Ed1NBaTNEdzAzdEF3NVYreVNYb2R0Tk9ENEZBU1lFQkJXREpUa1Jkc0t0b2RTUlFvcTBRSWkzbVNjbThlYWNLRHNjQWl0YS9FZXVKUnRiYU9vZ2poRU50K1hPWUZyeWcwUG9SdEFLa1FpWW5MWk5XWUpaQjh6b2lxUFowSktIdkFSdG5TQ2V0SVgzWE41T2hLQWtQellyR3hqV2d3K21sWU9zN2ZvQzZKd05yYTNCdFNhbE9GOUMyUnlWRnMxSkh2emc2eTdTR1J5WmZZcExiRjZySitnSDNBY1VLKy9IK1BQcnM3OWJpL1I5RjZqb2dCMzlWWFVva3BDbkJpL3YwekZPMTlnMlBSL1pia2F6VFV0UktaMHgyOWhweVM4WDR4RVRKNkxCb1FVWEtnemhyY0JSRFJsWExuMFl0eG1RRk5TUWlRbWlVdjZocTdnaGFDcVlwVk9JRkZuUWNJZzkwR2x3SmtnVVJKVE5oSjRqSHBIMlBCWUFyY09tbnJvSVAxQUxrOFIvZGRmKzkxcDBSM1ZZMXIzV0g5U0FUYXFJYWZialI1bDlCNExCNG9sYWdGcUVLVnRzUVdYa2JtSWRiV0FvRG1yM1YvMVpNY1NIcTJqV1UramN1V2t3d29UbG9YbWRJT0pHS2tPcjdwa0pNRWpTL1hmeWFwNkk3NjJ4Z3pFSkkvWU9nUXkya2lvT3B3L0N6dnlLOTRmL0sxazREOHhicWJJb1dZZ1BVclVWT0ZFbGVmWEkxdUZibFEyenFFZU4ydzN2MEZ3djBFVXhUbnh6YnplTXZPQUJYcWRtTUx4ZFNOeXlTa2VuNXJtLzlLcmpOcVZ3WUJWOXRBMVhGUXhvYVdsTWMxVVo0ODJqTXA1d2FjM3RDaUVJcTFLOGFvV2tKaGx3OVlqTzBTTDBvMFZ3aWkwVWhvam9kSjlDS0pJbGUyUkJWblFZekhhTEwweXFKcWRMajR0TmQ4VW1WOE5JQUw2NzVHQmQyemlxNUQwU1dvcDRWQmVzckRlYWxJcElWWDFMdEhvdmNMampaSUJRRFJvZlR6VlVOaUxUSVpPTzBWWXA0cVJXSUplaXByamN6dUNWRVpLRUZGNlRrelVBSWN6RjVKZUdkMVNBbFpLaWZXYnJzT1dFV2JiWnp1OGdlZzkxb1NicjdFTXBQdlF4OWZ6N01aNkFoUUZaenNIcFVtQytyd2pyQ3AzcGRnZWEwV2d4VnVJbVZScWplNG9US0ZKQjhocjQvSGZ5ejkyTCtpeStuVFJLUTI1NGoxSDhWc3pEY2JYTFJBNUVlYzM5QW0yUktrQngxN3pxRGM2bXBTY3NKZmZzYWh5ZmVSbkVFeWVwZk44WWJBTm01R2FMcHZ0YVBVV1dyalhSd3R2YTZsaVFSV0Y2endPSUNuN1N3RnBYNE5BWUlPelJkd3dGWWFzUGY3c3NBMitQVU5OMlJTQlNydWp0R25jVWVkWlJoSlB4b0hpelVQeUZrQ1c5WElaZWdoQmpDc2ZBWENTMWhRSU9McTg0RFlzV2ZWZVY2OGU2UkRScGFBUkl0UjFhVnpLYTJLU2lhR1pMVFRjSTRFMDdaQ3ozNkVYSG5tMUdWZ3dJVS9SV2xFQVlNdi9nclNJZU9rNU1KaEFKRkx0bWdYdFJHQ2RhMElwUTVsYkZIaXpXaEJuR3JDVlRNMHE4cEFiWnZVRUl3QjByWndvcWRqY1YvK1UzNXhwenNZd1ZNcmVWcXE0cWc3T1MwNC9SeEQwVEJ2UjVUOXgzZWNzREtzemNQU0xoek93K3RzN3NLdkpRbDN1OW9HbEZid3E5Z0I2Qm5lRENndFdWaTIzZ0JORG5Xc3JVVnBwZ2N2QTNYaEpGdWovbVFSaTg0SFN2N3YvaFhIblptVldwK1hCNjROTURMNnI5ZnFwMW43WlZRV0JTcTlUQWtkSUpRakV0ZVZoQnpOWXpnZU1GQ1dJSE1xZVNHTGhPcE1IV0ZUSVcwRVV3amtFb29Ca1J0NzRGaURiOVVFd3NwUUFveGxUQTRSczZIbEdGSk5TOTBJSTFBeXJFeHFDZTVjYS94MHNlcDI3L2ZiQnhJQmZBYkE1L00zUjNzZStIb2NmSlhmeGRjT3gza0ZwbWl1SWpQVkJqdkVTbUdtY05TUnZETW1TZDhRWllseTR6anl5QnpRRThoajZxdmtZbklYYzB3U3QwZWxSc09hdXQxYjNJbGszSm8zY0xMS2hMUU92bG8rcFdQOUlLdHVuR3d3RmhZVzBWQlpBaGo1SnZSajB4RHJJNEg0ankzTHNKZ0xOamFieUt3QUtDS1MrMDFzcVJjaTR6STI5dHJRNFVtUjN3VkNXN0oyLzFhYWo1WVlKN3BYcXhYWWVHaW5GYlMvZ3VPcFFzQTRQSlBxSVJQK0NaTU1Fc1BNU0N3N0twdEJGM3M0bTRnU3lUQUxDMnNBaEZpQXhwSjRTRVlIa2xJc1pVbHBlM25JVlU0QmN1WVV6MmRSN0xmMEptNjU3SGlpS2p2RldjdGxYZ3ZLMmhobmNscEhTbHNDZWVmQnd0SlZkUTM3a2l4SGRQYVNQanh0WDhNZmV5UTBzcUt4Q0trQXFZU2MxK3NiZFB3YUY4S2NVT0lLOHdCaTBxZWtlK0doS1BkbkZiRFdjMkVhdlV2UXBLNWFDS1IyV0VYOHUrK3BoYkN3U2w2aEFKc2V5ckFIbksrWWRMQmxDdGVYMnBzaUthcUNnWXJadFpzandwcGVoMjFDTjgwa1U1emxVWUxnd2l4WndSVnI4V3RReWlzT3ZMbDJEZzA5WXZhQ20wdEkxbGxVc0l0UTZBWE0zamVyYzdXQnB3S2ZISWwvRWtoZUNhZERhaVZaYldua1UzOUc0R2lGUlVWTWtFejBxUW9UTXloRkdvNkdDZ3JLcXdLZzRCbXhKQ05CdXVEeXB3MW9pcHlEQmpBVk1Fc0pRK0RxMTVKRGRJSm82Y2EzS01pcmt5UXZ2UkpTeWxDSkY5MUFrck4xd1RRM3ZrT0dYb2c1ZkR3akNSSzlhWlNOZloyUVFHUFlZQldKU0pXVkZNcXhBekFNTktsVUM0dUpPUGkxWnUxd2lUVVFoMzJNbDk3SGZMbU9wanFsdk1BQUJCY1RaeXcvdVJUa1I1eU1vcHZRcXdmTzlvN0k4OWo0M0Jqd1VjTG4wRWE2MFRnUFJTRGtJaEVCcVVHc2ZWYTExeVhWYjRZa3hJUVJ0azhZbVFkMWFZVk9zUmF2S3M1ay9pUlJTWERxUXdYYU4vS0o5cmFmUnJnaE54Ujg3UzRrK0xUUnU1Vjk1ZGE1R0pTWXpZcU1CeS9RelMyQlVvWlhpQ0Z3cEVtV2hKaU9GMFJZNWhuTlpLUUQxbk5RK0p5S3c0eElRYlhHU0wvQ3lPb01pMFBqNXRFSmtFbUZZN1EyV2ZHbFF6anM5WkpiUUtaT21qTXlHOTZLMjNIR29HUk5NRlMyWDVkaTBKRE1ka3lDb3ZVYml5djNoZ0EzV21GeG5pTzBhT3JPOUNBYUEzY2xvbzF6eWlnQUgwSEhKMXplT2NIMEN3a2NKQnFESW4wTWJOYm04SWVzQi9Bck9Jb3BSWVlOVTl2amU1b2VHemM2TlhWVkdNSloyR28weEdJcHJza1kyVlBnT2UxRWw2aTJWcmlnNkhBaVZxS0taSWxWZkZxWEUvSkNXZXF4NE1xRVZ0UndsbmRqbDEvalFFR0NJMk9xeEZlQ21ERlNCZTZES1pDc2hCZGdTeTdSMFViMkY5WGdCVC9yUWk3b2ZlendNY3NpZWlLcWMveVNSRjZsMkpvTGJ3ZVJHeDI5TmlQVWxGVUF1NDJRbjJWMmxocEJVNlVXK2JSWUpzS1lVVktEbllMNjZla1ZsZHJrV1FSMzczS1JUeWdEbTF0QVVlT2dCTURMRDRma3BNcG9CZVpRNm5RdWdKMEdlaHk1TGtSa2drTG1JbUc0S2JOMWY0dUlKdVc5OVpjRlJIYXJYcEpRYlJld21JRDQ0YytGSmZBcTZLbFRleXJhRGNBZFpQZGNveUttOHFCeXYyR2g5Mm1KbUtYVG9ZaHhvTVlSMWhUSjBTQnErS2xHaTZpN3MxeTE4Z293Z2kwMlRrZXc1SXFLRzJ4UUNmR2k2RE8wREcyVVc1UmFrWWU2M1F2akEwdEdSTUJlemc1R1FDdStBUlJ3Z2x5ckNzQkFDc1cyNTBOb3RjMmhNcGkxTUFZektYUndSU0htaW9Yb3FDc0pFVEJxdVV0YlRWczFoczhSV3hkSXFGc2NVdk5oZGNlRHpDU2VzWFNxYXFUTVFsdElKc2hNTUNBYmNJWXRHdW5iTHBXN3pBaWRwZWJKTi91TExycm9GSWV5WDZsRmpqVjdKMklPRmk1cUNqc0ZCL2ZscmRzM0QwTTVTSGJPaVZyaW5nYVE4VVR3bTNCYUtHTVJKMEFZb0d6ZFpqQXRvN1ZGeFVjc1pOdWFaQXRWVjdiQldHb0FMY0hFb2x0VVVWWUtobm9hYzNCVk9xUVMyNkRMZ2pjcncwenFpNFFxVFZ4TWJTK1RRc1lXb0R3c1ZVckhOcVJpRmlzV1dhbE5hTnRvNzFtWkc4RlNSMHl5a2xvb2UyK0RUQWVPMlVXMlNhcmoxZWdKS2E2QXFBbHBTR0pYTTQ5VzlicmJteUtOZFdyQkZYanF3TEhaWk4rbkJsWG5jYkpJQlByK0QxV1loV0FmNXpCeG1jV1ViZW54dnFzMW5iazhyNkhkUlhLWWN3aEFNRm9FSWxkSXRoWUI0WVp1TGFyemcyVXFEb0ZueTZMQUZ3VFhGVGFNRkwyRUhNeTdxSUlsL1VEMXFzakFGWkVKNVVJS2Z4SG01a1pWQ1FGSzBZazZmaEd0YkxZdDZCYUx4QUdqeDN0NnFZVE9ncVdrQXRCcXNTU010aXlPU3B1WUlzc3ZwN3c2a1VqckN5WElMWUVIaEJ5elZCRVUwRmRRbElmYXB3d3lNYWhlMFYvS3JiWmlwN0lHR3NiZlAwQWNkM2hHdmZFNjEzMHFUeGdQRklGWHBzd1VhR2ZZc04xMms5RjByekpCbXJtb2RvOVZqbUNxaVp0cFc1emdKSEhWSGZITURTRkdJRWZkNGU0Z0pRTTZnWGtkaEpxelJYM1h6eTlaYitFVU1KMDFWNm9BZEJXd3h1clJIWjFTblVHc2RRTDF4cW1vdkRUQ2ZtWTJndGE5Y3BlczZPNFU4U2wvVzlyWmVwc2NqUkVzN1dYeDhVSzYvQ0txN21hV0VEMTNGVkRxaG1zQUtMRWJHaFlnVXIxVm9paXZuNzJTa0Y3dTNtalhtQUR4dHRKYlo3S0lrVFh0dzNDc0xrM0xRRzdxa3hpYkFWMTFQRVR5L3dSalhPdW5LVTFNd3gzVUNGTElkTlhQdEg3ZmFJQkNnQ0t4ZzRKd1hZWVlRbXhiZEc4RllTbzBnODJLQ1ZPUTNRRG9kYkdyZkFQeWlIdSt0alRvbGk5MER1b1ZEOGRBUkx1VVpVdHdYeDNhQVZFSCtQVUl2Y3h3VU1PWHQrYjBaakUwQTJaeE0xTllZaUxhb3lNSlRCMVJCRU9RR3M3aFpVSnhSR2l3VnlNOTZob25OcWRBWVFhdlU1YksxVlByN3E5dWxKQU5kVFZjMnoxL05SU3dNTVZDQVFzTWpKNTdScEFSamRkcWJkb25JNFQ0bnlvM3dHVTJWR3dkbjRaU3IwSUZnYXJRb3VJRmZkelJFMFhSUXM4TUY1T0lHbndSaWlveWxLejNEWFFKQlk0M0VPVUdad2VKSS9pMGgxTTBZa1VkR1lrVEdURkZrZ3Z3VWxuR2pJZ3NHZUJxV0JUaXgyZmJINzM0UUczcUZJUmdKQ0UyOUthSTA2NjZncU5tamNCbGNVSWcyUE5lRnNPRjJQazZ5QUplcmpGSktIN0JIZUhPTzFlNVZnQTBCa1YxRnRnaHpVOHhwVmRKa2UxSXE1WFNzbWcrVG80bjhXdjBRQTFRelFzUmZ6bi92M2hUYjJJZlh6Q2NPNE9ENXFYSU52MDJucXpSVzVZNzVyNFB0UkFMUzlCZFdCVldoWm5nUlZOQzYvaTFYRVYwSWlFTGRydU5RSnRUVTBOK2RYT3ZSNlhoZ1dKcEtaa3F5czhFVG1mTFZXRkVYTGFMZHhLdkdXdHVkeERISkNBTnpaTlhhV2dtbHp0aElyY2xxRndLYzlidmx5OTdNdlFGQ2Nrbm1SUkNBU1NNSmp1YzhIaGlRWllYNnVNVlcvVGxJNkdWdTRUQWZyV1VLVWw2RmtEdlVVOUVaazJhd0NMMTBvMVFnSFZTN0g1KzdDaXFPN2FrcjhJT3g3QUttU0UydnEzU0NqaWcxVVBVRlBPZU4rKzFncmpFQ2ZjblRoaGQxeEVCU05XcDhxN2RxSS90azVQQ2FLM2p3TzIxQ1pROGFESHhFYS9zVExYTlVKRi9IVVBxNnZwVUEwVXRXSjExSHpFcXNTczNxQ2h4UUgyblZJdHoxcE9WZTI5OFpPQStSeEVXcXFSYTRxanN1U2FXUk12Z1pGa0I4UzNqSTYxTTZUbWY2R01wQkpBYUxUR1E4V3A5SHBaV25SRlBla3RJa0FOb0E1YmE1Q2RxWTVCOGFwS3FNV1AwSHRyRHZyNHg0azRJQUZnVE16eFIvSCtiZzBJaUN1enhPRlNBTW93QVowSERFTkphVW5nQjVEYk9kbUppdWNJbmNBT3dTYkV4MlA5cktwOWI5dFpJeXBTMmpuWUNlamErelpRdW1HTThicm9CRGVCSGVFMzNkalNtT1hvbVdVeVZES3M3MkdYUEFtYUh3ZDZpM2JSbU5LSUNpNnppaXNDMXpNSEtzNW9MRHhCalNPdzFBVWJwZUtEd1k4M1NLYTlyWnBhdTZxM2ZWakFkKy9BNUVzZVZTTTNsOWRCQk9TaTlaM0tZbFM1K1pBVEU4UnE2Y3JTVkF3dVRxUEg4TWdHU2xkZ25DbExWWGtRMlZCd0FHNXRyZ3NVR1hBVWRBRTR4ejNIcG5Ha2FucXdiWEJMeXdScWtWS2xkdUUvR09zUVJ6Y1V1VHQ2WXV2VEcyQWxnKzlNSmRVVFc1QnFUMGNLY0lxZDB6cW51bTFzRUoyRHlTdFc1c1lrV1hMSjR2a0lYRkJJb3ZvQzlCNDdBZnNhcEFNUUNNZFQ1ZDdMQ2lLWkZ4dWwzb0hPWWFrSVhRRjZoM1dDNHIySnZwaDM4VHZybkJ3MjRlOTZCNkxvaTFpZ3dOamdiZzNoQmg3NUNPUmhDK3hjeGdKYVptemNLazBBRjRaZmFUNVlJY3pwblJEaitRTUxSVXNUcXNISkNpeDVkQnlFUkFSTW1ZRTFDa29rMVJIakp1M1VuYkp6endzUFVEV0xMUWRWZElOaU9IU1EvcjZiUVpzSXF2aWo0djFSNVY2aFMzUWFBMUUxdWdNWk1GOHVkWXdySE1PaUtpUWF6cmxMTHVhS2FRcDFZSktxSXhQcTRLRVFQemlzcmpCa05iNElmQ0JWNVZvb0JBbzdaSUFaQ1k1T09ocnYvd0YrS2c4SUFGaTREMGlFT2hrN05TNHMvRkhnZ3RIQjJNSnhxanV3REtqaU1Ib1lIMkJhRG14bW85RUl4QkFTTWkxVmNKRlNrUldMcjc0N1Fld3NoQWV3NnQzb01iNDlPWkpKWmg0RGpKTmdDVEVMYjgyUWJyb3BybDRBUFVnV29Cd055end3UGZlNTRDbW5pT09NU3VHQnJIUEc4WVVPcUJwUTFFdTJuWUt3R1JjcmYyMEt2YVFCVnVWbjhmdndoREhlUlNKZGlZWHNFendmWXYvY1p6Sk5Kakc0UFM0M1NRU3c0aEV2OVZjZlJab3RZbHh2YXhoaUNab0JwZVpFQWxFRXhLRHhDaDRIWTF0eFJWVW9pcTBKc0VaSWViRVVUSWFDM0NGU3BSQmJEdGdXNW15bitNMm1YWFVmZmJ0N0F1emNQaVdLWVNtMjhlazk0Tlh4ejZwNEdBQUNOMFh6dTNWQVhJZ01WRSttcC9qWlVzQVoyMDREczZ0R0NZWkFJWUQ5bXA2a2NOTEZhN1hmOGlLZVdNKzByRE9nTEhTQ0pjQTdWdEVENENaNnBlZ1lOSjNrZy96VXZTanYvci9BNFh1QnZvOUFVdFZqY3NWWXRaeUJrMDhHbi9ybDFPRjd3QlVMVDA3QXF0ZVBQaFFwUkErSU9xYXJSbWlDSjBWbEh5ck5SbHVHOWJRV2hHcUVTTzcxM0ZHV1hUNmdyRUwyRFpkRjBFcmJsNkp4cThzSytMVnZVb2NFZGxWanFDb3FaVk5mTjFHc1VKc29ETmltNTJyM2RVaGxQYkNvR0RYU2dJR3dTZGJDSmlpOTFvYXBLSndxTFJmV1Y5K3o1dXBjbXNsU3h3U0xHd0dtbUtTYVlmZWVZR2IzWVlEMWNjckVQb0JVUU1vc3drL2tNM0FnZVJRUVNhdzVZQTB4WGdXakFydGFzWnFFem9tK0VKUElBZGtKbkJTaUsyUlhTQ2djRDFETDJlMzdxcDEvVFFUMm9YeFJKMW9YM28vSmdkNkJIbUVvS1F4ZGRHaHRBanQyTi9YT3R4TUFXVW9keTIyeHNCSVZaUUNnNy9qMm1nbDVqUG51QkcvY2MrY0lqdGtKODBaSDFuTVN4K0FwOUlKTWtwSUNYb3MyQVNKNUdJMHBhblVUd1F4TWUrajRyZWlmL1JSMEQzOFlrQXRvMjc2RnJOTjh1d1FmUnVwUC8xS0plNkF5cXo3THd3Z0RSR0hsWGNIcS9RTGJyTHE5MEtEVml4MkZmVnZyRUxoZFZGRGVObDFYdUV2TkNLdVY4Z1NJTzQ1U3FGc0dFTlZ3K0NrTHZTSVRDeExjVFZKbXhucVozUUVBZi9VcDFUQTFCOXlhNGtNNXhRc3p5VkxuWUZmaXdPb0ZVUTB4UWNDRHJVcGxWd0tITlJjNnA3b0NkQkpUQVh1bnV0RHNvWE5nNGdBU0d0a2tnTVdqMGFmZWFHQlA5Mmw5YmhkZnNzZ0IyVHN3RWRRNTJWWCt1Uk01aVhsN05pSExxMTlWOTFKQ1hxcFg5YnBZSVNVeFozV1BlQXp4RGQvaWZzdWQ0bVNLWUYwa2RVMFJVd0FycnM1YkVTWXR3MjhWTURTcEdUTEVYQXVQSnVsU1ZYRm5RRmxtRW9lQjJGRXdlY21QeFRWZjRycExSNElha24xODU3dWtkMytFbU93QUZWS3YrdGdHd1ZUYllpTmFXTlh3eWVHZ1ozSWNveGFHcWtJRU5XVEZXMkdveFVZMUxHenZVeWRZYTI3V1lnUGk5bTZXMnY0YS8yMEpSU0dVS09nd29rZEdSOGVBQVI4b3h6OTlFWEoxOVkwZlBqcGIzd1JpaUdHRTFBQzNjazJTUTg5Vzg3THdBaTBrd1FBa0oxS3dIVXhTQ0RnVkVFWVNMTEhsZ0VGaWI2ZVlTSFZDYWxCcmdDWFJ1bkR5Yk5xc0RqRVFQdElBV0lxd0JsUE52d0Q0Q0o2eEIvYm0xOVB2UFN4T2VpM1Z5ZGFnSGdCbVpDbmdqLzg0ZWVhWjREMzNnS3RkZUpobHJvcFFjYktsRWFHNldYNVZNQkFXS0VrN1RxVHFxU0NZTFZzV2lFbVBjcytIZ1pmK01QQ2dCeGpISE8yaWJKUk40QmZHU0RtSFgvc3RKRXhvcWJWRnhzNjdHTTRVWWREQ2pkV1E2OHZFdW8xOVYrcGFlUjFHU2JOcVhPRlBoMFVNcGF5Z0F3RXVKeUZvV2VVMm5DNGdJQ3hkWWZ1cEFwc0lPbVlpSUtuUUJNNHM0eTdtR1FCYzg2a01zRDF1blErTEJSUVVBbVBmY2ozeWdNUnI4NmRpYVVTZ2hIVlVrZ3hxRnlieXA4aW1QVldZSmdGSW9xV293SXJuaWtmVis3T2RPdFZ1NDhoaW9RNWdWME5zY25tSENzblVSS1BtWXFvNUZrM1VyaFhZL0U3Wi8vbzFBR0NUNE5jdVkxWUZUb2diOXA4Q3ZQSlZVa25DYkFaTSs2cGlBZFZKeTN3d1psUlUzQjlTRXJ5TEFrZ3RGMDIxN3FuNUJic0t5VkRDeW9yeXh6NU0vT3R2eFBSN3Z3Y1lSOEVTbzBpbzRqa2hkT0o5eC9HMjI2RGZlNk5zZWhxZ21VS2w1R2lwdWFGb08veWllcU5ReUVUK1VtQ2R3R2wzUXVFQXNPMm5xQWFnbkJHaHpDdWVIakJMRURtVlJhbnNzQzFkdENvY1hsT0FTRmk5ZWtWMHFHTjhxZTRveStZZEs3d1JBQzc5aERGdFN3UDhxeVdvb3lQWnhnR3BKSFNGTm5XeEUySU1Xd1o2ai9EVTFVUzlFNnhyVmE4dmN6RlVEQkNka3gxcW1BTGFuQmYyb0Z4Y3ptdGdDME10RHRUK2F3TG9DalFSMEF2c1NYWU9kVUlvb0QwTXZuZWhjNFZ3RlVBZXdmUDNzN3ppRitoSERrZERqMGZYbW0yblRJREY0aGMrOUdHR3E2NkdEbS9JTmphbExzSGdTa2xnQWttSlhZRjFSZGFKN0wzaG9KSDdtb0JVUXFuZFpjaHlmQ1o2cEMrVG5yanByOGl2ZlJhNi8vcGZZMTZMSllCQktaOTRSZHdsSjMzKzBwK0dIUmN4VFJWMnI5R0lwYVhNRnVyckFxTzcxZkFZTlliSHBJWSt3WFpOV1U4c2x4ZThCbVVBc2xFcTBkVnpJdVVmSmJsUU5Ta09JcXRhZGhocXRHR0dvUzR4UWdBUUo0RW1sUVN6bzV3Zi9Jc3o4a2NKNE1ydEZQL2pEZkNLV2tmMy9kYUhObEZ1d2RUSnJpaE5SZXNMUEJVVUNUWnhzbE5RWkgwWVJjdlAyRVhWVHd2RFhGYS9CakF0dzdjamdaaEVwY1IyUU1HTHlhc0VpUURZVzlUSlBlSnJVckcvM21GZGZZOU9yQUIxZk5Wak1icXdldzFwNHhiZ2wvK2J3MHdvUmFocE0wTkpGV0hGa2pobThRbFBKbDcxT3BSakJUeDRHTDdhQTUwa0s5VWJGNmt2VkZlZ0ZQcEFzNW92VVdDWGhSUnJoNUFFS0VkYmdTWGdwdXZnMy9pMTZGL3h2OVJQSm5WZVZZVkgxQnE4Q0I4ejBIVll2T3M5OU4vNFhkbk9zNFd5V0ZKZVJDQmJiYTBsS3k2SWlrbHkyVEJXQklqSkNGdGJpYnl2Zm1nUHNuY1pxalZtQUxrQ3M5dkFkdk5tMVRSc0c0QUdZcWxQTlhpMDZDMlVHcFI2dHZVN3hLRzAyTUwxMXkrczhRdjNaWUFFNUFkZ1Y5NkpyVHNXK1ZpZzhsWUhQVW9rVWVhUmhJTTFONHZSUzB0TUxJek9XZlYrVVlOWG5IQUpRWFF3NnlqMmtDMDJLdG1GR0RCWGs3akc0Nkd1NlY3bW1FbGdyOEFCdDhPeXpCUnVyWVY1ODFqbHVCakFCNXdrdnZ4bmlacytTdllUSVdkWUpSZFJFUTh6d0x1T3pCbDg3SmVRYi93VDZINFhRUis2R2JBTTI5R0RYUjFRWWdBcTdsaXh2c0FOVTRreXMrYWoxaGs1blVMSGpzQVAzUUs5NkVmVi84WnZ4T3VVd3JhVW15SFdQakU4QnZUeUF6K095YmlMVE5GRFpHSHNMVVNpOW45RUljVEFJMk0renhLckExSENYS2VUV3NoR2hWM2wvblg4RzRqMU9UdkVvS0ZVUFJ3YjB5RlpJSEtsR215RGQrTENCUjdnOVRZU1ZZSHFTUVdxQmFGSDkwRUF5SGp4SjZWOEgvK0REOFNIVDBrZkJobk93Z0RyZ3VNdWl5Sm5oS1IyVDNCWkVOUWF2YXVBZEVBVEZmZFQyL3NVdEZZWE4xaFp6TGNWMFhDeWhZaEdoZlFXZjI4MS9BWUdKNFVCU2wzOHNYY2dFN2R6UWpORm51cUM5ZUN1T2ZUaU9oL0dTNDB3YUtjenVzTUlJU1V3RDhLREx3TGUvR2I0ajd3VTVaam90OTVHbEJtNGFzQ09DV3lhZ042Z1NhSjZFM3BLdlFFcm5iaHpCVnBOeUxNTmxkdHVSSG5FQTRFM3ZocmRTMTRDNWhGeTBsS1NzYVYrMmlhMXhneDJIZkovL0ZuYU5YOE83ajBEOGtWZ0dWWmJBVnFMS3lIVmRnU0VJVUFvYW4zTHdXbDRGR1dCTWFycVJkbk9lWk5YNW8wQmRaZVVBbHFKT0Z5YnJ0VHlsYXJuRWV2NnM2cDBWQnVPRkxMendtb0M2TUd5Z0hDN2I3MFZBSzdCTlovZUFLODVXUG5nTXI3WnU0eXVkN0YzZFZPSEphZThoRUs3aTd5UUxmeEYrRFdrZ0I2aUVnNGRHbE53djBwT2RnNFBIcGZxTTdWMVpCdFJiMG9TVkUwSkFLeXRBYXVBVmlxMkdHRTI4TFdrZUsrSkV6VWNvOUZ6bllnZXdKUkFHZWhublVKYzkzcVduNzFDZVRJRnhySE5GNm9xNnNaMEUrbzZzaFJTaHZUOVB3ejk4ZHZoTDN5eFNqb051djBvZU1NdDByM0hxTmtHc05pTXIvazZzSGtjZnRlZExCLzVhS3d2Tys4ODhOZCtFZjJiM296MGhDK0ZqVG51NU9idEFvb0RXOXZjbU1HKzAvaG5iOGY0MHArVTdYMFE1UFBnYnFQVXJneE0rSlZhQVFjZUY1aGJWT2dXR3o2TkxsWklwUForUmRkWExSM3FlUTlQZVd5bWdqWkl4UkhqM3dJM0QrVmREZDVSOURBMmxDNmwrUTBiakpJRVJUc3dvcS9VL2thMzhQZDF4NjhIZ0orL2p4bUJIeWZIT25SdHZOSmZMY1piSDRtc3ZSMjdnR3dkbG9rOEVsb1VjSTJSZm9UcU1mSzNKU29WL3pKVlhLSFJWcUZUQ290UEVDYUZuQjlwQjBSVWRBbE9SVThUcEowN3dHbUtnaWVIQ0NMQ1Q0cXl2TktFUWtSRGI2cXBDazJSd2U1cG1Jc1BPWTM0dForaVAvd0pzQ2QvaFRpTXhLUnZrQUlxcEI5UUtxTkF4amlhblg2MjhFTS9JbnovRDFIdmVDdnd6bmRCYi84VCtQVWZnU2xCcE54RVRSemRGMStxOHZndlZYcjhFNWtlOGZBVEdLd0M3MUl3MG9wQkg2Um9paGtUTEhFc2Z2MU5HTDc2ZWVqSzZjQ0t3TEUwOFZWOFRJWktuaXcxd0RXTm1Fc1dLV0FWMWxaeGRJSFpTS2F1M3VWaHRxR2M4YXFzZyt6ZWpaajlIQ0tsbW11eTZtbXBTcGxVU1VXdVowcE5oYXRXZUxUZnI3TFVRSmVtZDJoMno0ZE92dk90dUJXNEdsZC9raUxtNHd6d3Nwb2EzVE11L3Z6SXNIYlgzclYwaGtxUkpUSWxvR1J3M0JENmt3eVJnVlkySk56M3NvOGdWQ3h4bGdPOFpiQW5BbVFoUG1PWGtiYU9CS2J2bGJCSE5aaEt4M0hYYnNoU25PYysyS0FRdGFLbUowSnRYS2phSDhXMUNtRld4ZFcweEhlNysrOGkvKzNYVTcvNngvSkhQRVkyamtUZlI2OWlKRi9iVTBzQnF1OWw3dkJjcUs0WG4vQms2QWxQcHZCdkZZTWpSbkFzMWx0U21mWVF3TDdkRSs2aDNPOE1VSUxSMWVqQUpkdEIwSVlSbVBRcTl4eGgvbWZmd1A1WUwrN2JCZmtjc2hnalcxVXI5V0s3RElsbW9Dby95Z292eGFZb1cxWllSSmFtSzJMcTRuS1JiVkZWOWFsaGxPUHNLQXdwdkdWekNGQTdXQUxiVUhtRmUrcUhwQm95R09ZdWRCQlhhaFV6SmRNZE5ydnJUMjg5ZHF6dUtmaEUrL3Y0RUV4RUE4aVZ0K0Rvb2JFY1hPb0NJLytTS0pWNTNRYVZHQlBwSWlrSExTYS9XNnBYSVlHaGVrRW9aaWl4MXV3QjJqcDhkaXhPYkwxMzZ0RTBnQlRZZHhvd1hhbHlMd0E5b2I3UlpZcmNwcE1DcjR1OHNPV0lTSUFueVpQWDNMSEFWbnJwbkVIOHRxZlQzdjRXb3UvQm5PUCtZVVVmS3BpdGdMN0NSZlZkYUNwS0JzZEJxY1FlWTNRZHNUcVJwbjFNanMwWkdrZkNTM2lQbExaRFFvTURFdEE2MGpBTTBLUW5icjBOL3ZUbndENTBwMnpmcVZCWmhKaVZjUTlYOWlXdWQ0Z2I0dnR3aXBHWHQxbU1Wc0VRQXdwR3BGTk9CYnF1VVdYdHM5VVhyZ0RFc0JsOFE0eS9SWVZhR3VZWCtzUWw0MUh6UTlUWmtIRFZLYXB5dUthUVZrSUk0U01kbXlwdkJPQnZ3cE03NEpNdDhCT1RRcFluaDFmTUxHOUFsOUZOdk1nY2FlcTBxYUtEWWU3Q3FpdkdwYlhxVjBTSGFoaVJHN0lMZUNMVUs1VS9UclhabTRCbVc4dGJHNmo0RnlyMURBRDd6Z0U0Q1Jxclo2VUNBWFlPNnl0SDNBVVBXeHVLcVNSYVY0V3BDVVFIb0JjeElZb0tiUGNPOEx5RjlOM1BVdm1qbDZOMEhlZ1NTcTVOaTVYK3JPYzduRkNjS2FWRTlCTW94V0pGTnR4WDBaL0d6c0MrZDBjQ0c4WlRING9XUjFoVk9wVmhFQ1lUNVBlOVg3T25QQlg4NkszZzZhY0QyQ0piRytvMmsxUlZPQTR6TVZUaGxYK25hcnRuaEE4akFoNUtFakVYTGo0dm9uZ1RIa2dNRWpuQWRBZmc4M25VWjU1YmdsclZCYTM0OEFhNW9JNUxvOURHZGdpQW1PRjBqTmpKS2cxRDBRWkd2S2ZjL1NIZ2t4bVFUMldBdXFMK3gwZm40OXNIUUVpS2hvbXB5M3FSSFRodWxpZ3Fsb3dIcWxyRmlVNlExUzEyQVozRUtJb085WVNpTWlVQTFtTjJjWTJTWUozZjBzYmZhTHBMVEpYL2JQUlc4a2djT2dGZHNDQkt0ZUc5dlc0WWZNVW93Ylo1bVpNNlJtblhEdkRCVS9MRjN3RDg3QlZ3Q2toZHpIdU8weDZEclpza25DeHQ0U3VXNHFkdFdKZGtyT3J5QnU4NGNLTDF4YWtGSlBrNHlyb0VUQ2Nvdi9WeStKYy9BK21ZZ0ZOT0E4dU1xZ0lRV0V5VXEyTFlaZU03NkZIVXhvMHRxMWhyRzlaRVZueVZCTEZKbkg5cVJNdlN1RjRBSHROSjFCazlBN3JqdUloRTh4UEhla1R2cjFBTThMclRMNHl3VnNwZ3E4Y2hsSUJmc0lvQ3FjREF5VzFhWDl6ZUxmNGtQdisxbjVULzNaY0I0b3BybzRQbG5uc24vL2ZPUWNmUVdYSlNGbEozSVFGNVMwQ20yTE5CTUcwdWpDb25TMHVRVXZDL2RmUzZ3bWdoMEIwVGdCdkhDUUJtVk56TWJteGNzQURzWHFOT09pbk9SUmdUdERSRWhxZnRBWFlONG9IWVFlaWkyaU1oSkNwV2dJV0VDeDFBRC80MVBleFUyTzljQ1gzVDArQWZmQy9VOVFwUU1CczhRd29CdTBlYlJOdkNzalNubG1xcC9wU1ZtS2hQVmdQVUFRZExpWlhqS3ozendidVFYL2lkNEF0ZnlINTFEMjNmTG1LTXVZUlJtZ3RNb1VjTXoxZHY1Z2pCaW52akJGRUFhMTRYd3h0UlpmRVVaa2g3OXdDUkZRTllLcjFpd29sMThDUEhWUDdxWTVZd2xhbFU1RzNiRU91RE5kZFRKUlpEK2dLcHdGRnFPRjZCY3dKQW9LOGkyUkdXOTcvOE9iZmZLSWlmeUlCOFNnTWtJQjJBL2NDZEcvZXNiNDF2UXg4bGxabGdmZVdBWFJqV25lZ3JlQkdTSk1rSVN4UTZCdkVlZ0RUZEVHTFNaSFVmWENaMnJFSUgzdzJWa1dDaU16cHZyYzB4OFF4T1ZvR3pId3ZNTjRCSkY0Vk5OVFowQXRwTjBSSHFRRXlpUUhhRDBLdks2NE1pWHhxb1FTSEJWeWhPSG5vNmVPeHR3RGMvR1Rqd1hkUXRIeGE3THVBWXlqeVhHRHBldkRhWkNpR0ZCNlNZUm9OVzNhTjF6OVFUV3pMa0hybGYzMU9MQVhqWnowRmY4bmowdi9zSzRJRVhTREV2R2pYbnEzeERVT3VWdW1UanVxeng4azIwdHpTK0drYWdKbnFBY2xUVy9hVlBxamQ1cWhkY2F0T3hEQkJteDRERFIySHNnUmdKaVZibFdlM0lZTTBGdHdIdXFoR3NJWG9KdjlBMWdkQlJ4U25jeGMwL3d0VW92NFRIM0dmK2Q1OEdDQURYWEJNLy8xaldtNUVLK2w3dTVwcnNkRnB5YU9JWTEzT0F6dzFvN3NEUXh3bld0ZDRLTEhGQTYwQWtsMXRObkZZSkhyMUozTm9FVWdBdjdsVW9LVk03WHAxNVByREl3QVJONk1yZ1hPdkY2c1NLUTBxOTErL0RFTm1EN0pZaWlQaWJEbURuUWhjbEc4WUJkdG9lOEdFOStjNWZBTDd0UzhrWFBWL2xiVytFUzlLa3AzZGRIS01BWm9HbEFEa1RlVVFxQlN3Rm5uUHNBeWtaRU9CbVZQMDd2K00yREwvNDMrSFBmREw4aWg4QTF5VGQvNnlvZEx0Qm1HYWxFK2pFMWdhQkxzUUhNUXFrQUJTdGpqMWhjaks1TWNZa3M5a3ZnUGpBSmJ1ZHNrdDJ3ZjBVUDZ5OU5sVWQ3ZlZHSDk5N0U2d1V4VVoyNS9icVc2ZWpOUGtHMjFjVlFMQmhoYkVjc3FCSHhpNE9VaHJWMGRNdDNRYit2RC82YWdCNEFkNzFtYS9xQW9CTEw0WGpXdUJ0UjhjM2ZWRzI4YXdlWFhHZ213QnBFcjNmZVY0MHpwUDFPeERIVXVWUklPcE0vYUFibGJCc0hLZ3lvM2hTSXFDUldyOFgyclUzbWxpWVZDZEd0UHhLZk9TWENkZitad015bHUxS3JaUnJVU2U4UmFDU0ppQzJwa1lZcHpWUXJEWGhiQ01JeWVLNFN3WkE4TnlUZ01VQWY5Zi9oRjM3bThKSkZ4SlArR2ZTWTc5WTlvZ25HUGJ1Z3h0bDI5MUR5N3ZhQUhuWE5WUVV1dW1qMHR2ZVFieitEOEhyM2drN2NqZHcyajdob3ZPQm9SQ0xlVXpFWXUzdGkwM2RsVzFZYm9LcTAwUnFucTBpeVVJaTVvS0htQ2UwVTgzTUVrajE4TmxkNEpkZnltN25UcUJrS1hWeFhzUHVDQS9BYWZpemR5S3B4TTN0cWdWRzg1QkVSb2wrdmxvYlJqR0Nhb1JHaDVUaDJBbm5sRTdCZllxdXZ3M3pELzZYczI5NEY2K3Z1TTFuWTRDOEVuVWg1SGpkMTE0NGZjOVorK3l4bUJXbkFkMGFNR3dJSkxrNE9LcC9VRi94UFFBSjBTOVNPUTFuVkpGeHFLbzVaRTNodXltTVI1bHZmSnY4ekF0azdyRWcyaEFlVWhXOWYvQmppWlV6Z1hJdjBQVVYyUVVhdmdjTGdicFZBSSsrUFdObW1iYUU1b3Zld2s1THg5V090RHFQWEFJN092TWtFcFN2M3lKNy9iOFhYa1dXSGVlSnUzZVNGejZjZnM3RHBaVzFtRzdWR3ptTzhua0JGeU4xL1h1ZzkvdzU3TWh4OHQ3YmdEMjd3ZDI3Z2RQT2dXWVptaThJR0NxUUI5UUNKc1FFWk1EQjFhNkxSWnRNbmRoRUN6RnpzcnE1cXNaZlFoR3FuVEgzMmdoeUhmenlMMnRYZEttQlhCWkpYU0ljOHIvOElCTDJNdFpSMU5vS2tUWVA4S0RvVGpobFRTdk5pZzBXaUVMQkxvWUszSnhsTk92ZmIvUFg4M29zL2dTWGRFL0J0Zm16TWtBQXVPWlNKQUw1N3NQNGZaMkV4L1k5Y25GMDNScG9XNFE2SVI5MWxvV1Urb0R1bHEzSEZYUTJBWW9kd0pHbUIxRWZweUtaMER2U2JkZVQ0Zk9KNkFSa2syclFNN0JqdC9DWVp4UHYvUm5nckZPRWhkZG5Wa091bnJBbUUwSlpRcTV4cXB1NDErcnpseVYzK3g2MUFSMGhLaFhJVWlBSGJXMEgvUHhkNFJrMkRzTm1CNEczZjBoODQyOUNJOGdDZVludVN3alV0RWRhV1FuUnc4bHIwaGtYVUlOREMwbXpzYzQzWXZUWGdFU2RoVjJ2ZXh5UGhVQzZqWWlKem1HMnVRNW1rRXBOSzFGUSsyQ3FrN1Q2NmViSG9MTlBadi9WVDNlNFU1WlFXNzNVdHM1YVNzaDNIeUxlY3AwNm5BMzMzRTVBNkpMaUxUeUVwcUdrUkdWSW1rbUdUajVyQlJrN1VDQ2hUR0hwbzV6ckdoNjdXcmh2K3Uwek04QnJneG43czl2SHF5NDh5MTUwdnpXdWFuUlpML1E3WFlzNXFkNjF1SHZnMm9NbTBpZ2lWZDFzOVZDQmhRbWt0ZjZSV2k4YTRDT3dtcVFiLzFRQ1lOYWh5RVZZNDNuZ05ZM1VNNzROdk82WEFJMlJ5WFJhMmxJNDFqcDNxb0hZWHVzMEdjMXFmMFZyc1lwRDIvNlhxbWhEOVlJaDloRE5nc3pQRHM4aStna3dtUUI3ZGlOR01vWHJoV0oxQ21XS2Vjd3VqaFJHRVhrZWQxOFBCdDRlamd3bHlMQ3d0R0JzMktFZWgrcVlEVll2WjVIZmVMaHpDL2NHVURLU3JwRE5xMG9FMkUzcHc4MndGM3l2ZDN0MkFjTUFUbnBVeFY0MHVtVUhKa21MUDNrcmZXc0dkcjJ6TEt3eFIxSHMxK3hrbSswSXJTeWdPREpaQVpCUnRCdk9UdEFvRkRCTlA4Smo3N3Y2dEJ2K1VyZUF3TlhibzE3djQzR2ZSUWdBWEFtNExrTzY4dGJGamJkdCtKOUVFV0FPQ3RNOVpnYW5yUm9XUjF4bGNIS0NxdUFLNlowbE5BbVdXZzVZVlVCaDloS3daMHJjZEIzOXlKMTBvMUtZUkcxR1JHd1JLdGx4M2tPQmgvNEw0UEJSWWFXdnhRUzN2MXB4MFpRelZicS8xQWlhWkxIWU1zYkRSeFhObGpaRTV4NWFNU1d2alU5S2dwdkRlZ2hkV0xWOEpNWUJuTStCWVE0dTVyVEZBaGhtd0RpRzBGVGxCR2tZVUtFbHdNQTZCSjExNENZVGEwbmQxL1FsamlsR1JrWmVyYlp2TDJiTmFMblRPT2pPNXNjVnVlRjhremh0bDlLM2Ywdk1MSzh0cU9EMmRhZ3VndjRuZjRwT080Q2w4TDB4RzBIclJadVdwMWJ4dHVkVU80WUhNTTNkRlJmc0lSNDM4YSt3L2o5NUMrYlg0SkpHbkg3MkJuamk0OE9IODI5dUxzQ1VCRG5SVGFBMGpkd0VCczV2eThJa2VOK1E0VWQ5N3FFSlBLRzlFY3UyVENRU0t5c3dIaGIrOG5VVmIxdHFvQ09reFdZalVvSXUvM0ZnM0VkZ0RObFVhLzlNQWNNRXZFT3g0WS9HMnFFbldUVkd0ZWtLRGJ0Y0dqRlk1OVcwM2w4eTBjMFlVd1hhalZQZk0zcGRGRnRoNGlzNFRMT1FyaVN6SlpoV0owbVlMWEhEaVA0QkpndFdKN1lwSkd4MTRyZGFyM1VVYjl3V2RMQXBWRUpkdGV4ZlVZSDZxYlIxayt6QTk1TW43NmNYaDZlNm1pRFFWM2h4c085UTFqZUlWNzlOSFU0QmZNN1l0QlRpMDJhcmxRV3BoVkZRUGtJTUpzcDFJOVVPRkt6Q1ZWalVzK3ZlYSt2clA1OXV1Vm9BTDhXMW43TDYvWXdNa0ZlalNPQXJieW12dmZHWXJrY0hVNHhQNTNRMzQwWmZBWVo3Q3NldEFxd3lkblltbUJLc0F0SUkyVG9pTTR1Y2tPaHFsblpxeis3dHZ4MUFxUm5NMEhvVHdnNVRvcndBSjk4ZmV1NVBBVGNmQTFZcTVkKzhXZldBRFg2Snh2cjZld005a2Q0YlUxOC9jWWVnNkpKLy9Pc3N4YVVOeDR4R0o2OVJFQjBxbUZHeE9hdHd6M0o4TUtLOTFxSW9kYUxoanloVi9wbHFLaExjT0pkaTNaanNWUTIycm9Fd1F4dC9wOXFMSFRWSjdURUo4Sm1nQ2pGZGd4KzZDZVZwVHhhLzdmbmdtR0VXU3cyVzE1T2lsZEM0RFAvNzlTcDNIeVA3VlN3M1NiRXljTFhRYUhsZkRVd3RoMUdNMGN3b0tOaFh0eU5SS0lOWitqQm52M0g3YkhhYmNKbjlkZDd2cnpWQUFNRGxzRCsrRzVzZjNoeC9mcHpDdWg3S2xOSU9ZYklXVHBwVFlYN0xRRXhxTjFVS1VRRGpGcXBGaUFJVVhxcW5IYzRDbkxRRDVmYTMwcTkvUjJCVWVZeXdzcTF0UVpQaTZNdGVLRDMybTRuYjd3VFdKb0F5WWxSRlhLUWxMVmZYSTRpc1JaRERySjY4RU1ucWhNS281cWFxUmlhcXZrN3R1aE9TeTlxQ25BN0FCTnNpaXhpUUxrc2lVb2FTTzVLVWFzOUtiZEVFazFPcE1OYkpBdXBMNEpsZHFXSUxYN0lmWnJKSUVZcTNrUi9MYlFGMURJaHFnenFVWVNzVDJOSGJZQmZ1UXYrYkwyTXlGeER0bElvR3BScUNvK0oyUU9Ndi94YVM5Z00rb0k1emErM3FyTk9mQUpSSWlHdjNvbEFzY0Q5eFFNRk9ETnpCZ2dGdXEraDRJemJtYnlpMy9DSUFYSUdyLzFyais4d004T3FvVUgvbjdmbTNiempzaDdCR0V5QW1ZcktYVW9Gc0JjaEhwZUZ3QVZaamJLUlgzcmJ1OGdTN01JWks1eEVkNjhCeVE5by9JdjNoU3dHRUx1REVnNnZnU3BSNHBVRGY5TFBRT2M4Q2JyOHJqTkJxaUt5S2FLWmFqVGNRUEZIV2hXZnhDSzFMYnhXaFcxWGt5cVVuWk9Xc0szY2RFMjJEbFdqcFJIaXVoaWNhcVNTbEJGblU4dlNrUmlkdjQ1ZFVwQ2UxaFJRV21qRzJJZWlvTjFMMWlHMzRaNGdPb3RRT3VLQVNJWlE0WFlIdXV0SDlmbE9sTjc0V09QMjBTR1ZTWW9nbnQwc0FsVUxyT3BVL2VTdjRaeDlVU3Z1Qk1sWW9zVTA2Y0d4REM2akpRaHVVRmhNVXh4Z3dqNVBxYVNhOG9PdjZEMkgyNmxlTlI5NHZITEFyUHczMjkxa1pJQUZkY3duU3F6Wng4S01mODU4UmFIMzAxTERiWVp5c2dNb0FKOERzeGdGWUVkRmJlQW9TVm1lNm9NcVBsK0czVVhiS3d1bDd3UnRlQS83bHEySDlKRm9vbDViWFVCT0RHY2kwQTNyQksrSDMreHJnNWtQdzNxWHBTbmhQa3VocVUyMHpvSzdtbXlsK1Y1UDh5UDZYdkRLMk9lYjJ0eEY2VVRQdmFPR0lteWo0cWNqOVd0aXNLMkxpK2RaVk5OcXdwQUZoOHVEQnczZ1J5SkRZVmo0QURjaXZxdE1LVEZuZ21oV1hWeXdKQXRqM2xCbks3UitFdnVZSnRHdmViSGIyT2VBd3hnSEVDMGJhUXl3blFBSGc4SlAvQlRidXM4WVF0M1FWYUVLRTluMmRnM2RDU0M1d2pDamFDZGNhcEN6SENqdDhFSnQ2UTNmM1R3UEE1Ymp5MHhXK241MEJBc0NsMTBZdWVQVmJoLzl4L1QyNkU2dXdRaFdac0xJM0RvOFRJQitUNXJkazJLNzZZWmVpQVd5clpscXVaV3FKZmFRS1ozVGlIL3c0TkM3cU5kaVdMMFl3VmdXL0hKcnNCRjd3KzhDWHZSUjI5eFE4ZENTNjBLYXBKVlQxRDl1RVNBREdXcVVIaW5JaWN3T3pOanlvL3F3YWtxbDZQaXdIZFlhVU9TWTUxL2szSWI1ZyszeFZDb0JhZ0RWVWlyQ1lXd0FUdkJKY1FJVlVvcmlBWUhUVitaaExOWlRpSGhaU0lpZlRVQk1ldmxQSWg2Q2YrQkhZNy8wZWVNWnB3cGhoWFY4bkRGV3pybjZJSlpOZHd0WXJmZzk2MDd1UXVsT2xQQWgxdUZDcmFsWGxWekZWdFEycENHTXNjQXdoeGVkZVpOVHRtSU94Nzk3cFcvL25WMmEzdjEwNFlGZTNrYXlmS3dNa0lGd08reTNnM3ZjZThwZU5Ec1BFWFgxQjJpUDBhNGhjWTQ5eDg0WlJlUTVnRXNhbERyU29OZ05ucVI0RmFFYUl5T1gyN1NUbTE4bGY4NVB5MUVFbG83WlpvUVdkRmhWTWdoVVhudm5qOGg5OE8vemgzeU1jM0NuY2ZnaVlIU002QkE5cnpiMEJiYkVpU3QwRVZBdkk3ZndhTGNkdTVoT0hHTkNpbkdqTG9LTXFyMk5VMFJxR1RFQzlsRm9PaU1ZU09tbmVwVUo4bGExeHlWMHFEcUpFZTFFUlVoWlFESW9tRndBVHc1Q01oNDZEdDl4SzRpajFMNzVTOXFkL3pNbVAvUkFzWjFncFF0Y1JGcVBUVFN4V1o5Z2pPOWdsbEkvZHFmSmRQd0hhL1ZWOElTQ2FuT0tEZTl2M3dmZ1VwWjJMbWt1U0E5eEdaTzVBNFJyRVF2Y081RWU2amZLMkhYZi9GUEhaZVQ4MFUvaE1uNnNENE9PdXhNNWYrSC82OXoveURKeGRCaWwxWXQ0RU51OTJhcEpRWmtYOXZoNjd2MlFLckRzOUJab1NnM2tRaXBnVHdPcmExa1ZtajJMZzVneDl5eHZJQno4WnlJT1VKcEZMVmNLZ2NxZEJIbmdXVWtjSFhFZHVBOS8xdTdUM3Z3cTQ1NzFBT1JxVFVWY1RNRmtCVW8rbHkzSEZDankzYUlmTmdXTFhIcHVXZDdlZUd6V0ppOXpqZWhVdTk0M0FxOVl6Mm1ObFRyakhEQ09NOWFhcDZKTExnajZRUmNORmpubnp6S1NOaHVEK1RjaWtieXlBalpsc25vR3RnYjY2QTNySWhkSS9ldzdUczc4U2VNQURRVUNlTTh5NnVKRmo4bFdkdzI0bzVrZ09vampSZHhxZThWeVdOOXdJcExPQk1xaEo2b0hvcFF0NG5IVkpnWmhCeFRaZ1lJQnpYcFhQcHlOempVQldHVmE3NmZTMytudGYvbzJ6OXo1UE9HREVsWjlSN3ZjM01VRG9NaVJlamZMYmw5anp2L1pSM1M5UGs0L0s2dGxCVzNjSjgwMmpyUWpsbUd2dDRpbFdIOUFER3c3dll1bEI2L09Gc1RVQnRYQXBqREtrQkN3RzRXTWQ4RjF2aE01N0RPRVpzRzdKU05hT3VUb0hsREozUVlWSS9YYXY3NTN2cDkzOFh2cEgvaERwNEIyTzR6ZkNaN2RGMndsVEdGV3ExWE9KVjQ3S0lzckVxQkE2cUVUMVFwRHlGSTBVRWxocTBwZFNqSkIweXVydVg1Ullxc0hSWWZPUTlHc3NNWUVxaXhnQjVBSUxRUWxZd0RJdlN2TkVGYUZzTFdUc3lITWZCSngwT3NvRjV5STk3Um5RQlE4U3YraWhySndZbVhOdFB6WGk0NFN3aHJoTktYZzFuNjduOEsrK0hmcU4xNkJNdmtnY1pvaTUwdTJjSXFRUGFCdnVJaFlVeE1Ed0FuR09VUXVBZTFGMENnb3loUlZRMTAzejVvdDA4eVAreitLdW02OEErWmtXSDM4akF3UUFIWUR4U3ZpMVg1UCs5TWtQdGlkaTdnVkpxVGgxL05iNjBYdlNqenAyUDJVTi9WN0taeFp0bldUZDl4RXZGWTNvd3JMakR4UlNSMnpOb01NN29HOTVQWERPbzhBeWhBY1QwS2FxTlJJTExYaTZHMVZRUURGMWJHWWxRT1h3TGNEUnUyR2I2N0Q1SWZEd3pjU1JtOENqaDRCakdSZ0JqQTdrQXBVRWprTnR1VnlYai9OZ09NWTVNTTVnNDhEYTB3SFBCY3dWNCtpVDJFMEFkb3g1d1NzZ1Y0aHVUZWgyVURhVnBtc0EwM1pMZ3B2Z1RxMU1nYlBQSngvd1VQaVo1MEY3ZHFxNzMvMkl2ZnVsZXJvTWlKNFRGNW1Ta0F5Vk1ZdnNBdEg3QmRFZGJxR2lwWmN1MGIvcngxaGU5bXZnOUdIeWNWZzI0ZFQ5U3FyOEJwdThvUUk4RU9nWnhBRG5GakpXVUhSYTNJVk1VaDY3YnZKYmt6dC81SVZiMS8rL1YrR3lkRG11L294enY3K3hBVjUyR2RMVnY0dnlQUS9Cay83ZFUvbzNuN0hQVldaS2FRVzJPR1phdjh1UlZnVU5nSlhFdlYrMUptUkNZeDMzV3NrTk1DcFB0bDVOSTFHM0I4Q01XTjlDT2J4YitGZHZnSjM3S0xJTWtEb2d0VmFMNm84VTNXRUdVeVhYSUM5YU5pd1NnblV0a3diQzVGdG0ySExnMkwvUzdBS1F5a0NPTXlEUGhMRklaVERMQTFXeVRIVnQ0NURyOG01VXpXQVhjdnZVQTExUGRoTnFNaFVtTzBKU0h1TnV2SlpmUE9HWVlEV0lBcTMxQ0xLYzNZdFhkSjZvYWlFQ1liZFZMMVI3WXdNc2NBY3dMR0NUcWR5QTRZVS9CUCtsVjdMckh3Z3ZpM3JTWGZLbG9wV1Y1MkN6aDJhQURtS0VmQlBGQ01lcHpKb1k1SVN2cXUvZTBLKy82eGxuWC9la3E2Ni9MRjhlTFplZkVmWjM0dU96TmtBQXVPb3lwTXV2UnZtRFo2YWZlYzRUK04zWThDeXlZMjlhdjZWd1dFamRDcFdQTy90OVBYWS9ZeWR3T0lkNHo2eWQvZ29nV3dXRUd4aEJSYUZnd21JR0hkcEpmT1hQZzQvNitqakxlUlJURnk4Z1NBWUtYamZCMUoybmtTaGlHUTNjVmJ4Qkh6UkdZeC9OWXFRM0ZYdkk2MXJyMksyVXVxWHdxNTFZT3lGenJTZXZsVGZMODFnOWxxd2FVbGdMQU9VWVRjTkFacFpyUzlBc3Z5bzNWS3Z1MkhjUlo4U01aSXhXTTBmRmhPSjNiVmlxb3haWkxsamZJOTk1RDhyMy9CdndkNjhWSmhjYWh3WHFXa0g1ZG50STlYNGhCcXJBSVhLMW94SFFETUtBZ3BOUXNOT2tZdFNxSjcrbEgvdWY3Rzc1c2wvYXZQdk5Wd0hwOHMraThqM3g4VGN5UUFIRUFmRHhWMkx2TC95cjd0MFBmNURPSzBma2Fack1zK1BZVFVWS0J2WlF1ZGU1ZXRHS2RqeHV3dit2dkRNUHN1eXU3dnZubk4rOTczWDNUTTlvdEF0SkZraGpJU1FMZ1FVT01tQ0JaTGFFSmNTV29JUWtCOHBseXNSVUdST2J1QkxYU0lBcmpnUEVqbTBjRjFVQmJ3UkxFRnNoRGlZb0NCQTJGRUoyU0VBb2xOQTJXbWZUVEhlL2ZzdjkvYzdKSCtkM1g3ZFViRm9ad2FtYW1xbDUzZS9kOTk2NXY3Tjl6L2ZMSGhQYWFNdTQ5b3U4MHNlWTJvOGdhRWl5TzQwS3N4bnNIc1BPTitBdjI0VWN1OU1keEV2bm9xazZvcnVKeUthYVdienlEeGJ6VFQyU2pmZXMvZnVJTE1qbmo1ckZqbkdGZVBZSmpVTG90VlFxakxxWk1UOTU2aWRUZmE1ZVJFeHdQVWpDaTJQaXhha1VKTG9KMldpa1lFZ05kWmo2Ym1MWUZxUzBjVVcxeFBBNUppbzZkS1Z1cHpYUi81dGU4OWY0MjM4ZC9lWUloazlEWm1NSlIrc1BWemFRYlBWZTZqczJBVFJ3endnVHNzL0FGekE1UmtyZFo5RGNKaDE4b05uendUZVB2L0dtWFp6ZlhQa2Q4SDdmemI2bk5zeERUY0N2dmduNUloejR6QTM1bC9hdVlHbFpTekV4WFJRV1QxQnNabTVGcERsS1dmL3FsUFdiT3ppdWtqZzBHODNlalcwM24wUDdhUVVHQ0dLd01JRFRkOEMrUDBjKytCejQ1RHRnZlMra3RwNFNobGx4RlRldHA1NnBSenZMVlFKL2gwREJyY3kxY3cxNkpscmlDNmtua3ZaVXRkSHcwOHA4YmdTbkM5ckVHRWhUSmRzVUVVMmlLZUVpb2lsUmI0eUtGMVZ4UTh5Q2Z6OEptM1RZaUkyLy9qL0U2VkZhMEhjQTQrSjZ4clRhajRxa29jc1JWSnFFTlEzZGpYL1A1SFdYZVhudG01QTdXcEV0cDdqbjlmaTFpb1J6VEtvUU4xN2g5eldob083M1FqMzFaaGlKem84STFud3lKUThhYmE1UEIyLzV3M1RQTzV4ZCt1MjIzUjZHTHoxeXUrNThtaGQvbG56TmEzamZxODl2MzhhcXowcWhrUllaM1ZHWXJpRTZySi9wSG1mN3E1Y1luTkRDQ3NFUGcvZUFsd3BpcmRWb2RONll1MGNtQUhObDR1eGRCMytxY001Ym5IOTBLYlo4QXZRaDBvcTRsMmo5U3pLUkhvV3Z4ZW8ydjRZZ3ExRWpYOS9qQnVpSCt4V05LZk1RYTBGU20xUTM2c3pxQ09FbDBlK3pVdEFxSTZtRVlPYThRbFh3VWlrZ29oZFV6ekIxTUJGUkZjSE1UQ1IweDJwZjJFUWtsQlBEOGFLZDVVMkRnSmZpVW03NEV2NmYvOFRsdzlmZ293R3lkSko3eVhncGlLVmV1VFFHN0RCbkFvd1BUc1hyMFIxSHVVcUgrUW9lWUFNcHZxelJNUnlTWnQ5WTdJYS9iN3RmOW52cmUvL25JeTA4TnR1amNrQUg0U3Iwakl0Wit0TTNONTk3N3BueXJMTFBzaVJKcUhQb0Z2T3V1S1NCQ0ROM0RnbmJMOTRxemJiR2ZTMkxOTXdUc3pvTGRhcFdXOVgxaVJ1elNzV0I0Tm9nbzFYWU00UG1hUHlVMXlCbi9yVGJxUzhWMlhKazMwSUcrdFpZY2N6ZEtocFRvSTQ5Wkw3WjBROExIUGNVNmhzMUd4V3hVTVowcjZsWU9HeXNjTlFZNmYyWjZrQlNEWFVtSlhJcWswcmNIZS9HNTZXOHp4ZnlvZWFBODgwT0Y0K0RLVUsvcWtyellPeXczWDhmNWI5KzNPVWpINk44K1NiU09qU0x4NHRwNDducktyUkcrblpQamIzeEljeHZIbkNyNHBaR3ZldTBrWldTdmNOWkRINGd3WnpXNlBLd0hmeEJjKzg3ZjJWMHg2NC80dHoyemR6WVBSci9pVS8vVVZwZmtQejJpVHp6OWE5cmJqajVLYVN5Z3FRQjVJbkxBOThzU0EvWm40RE9ZUHZybGttTHdCaW5xZGVRUk9pQjlQMTB0RSsxTzZqTitzajRzd1k0WVRxRGc2dE9FV2lPZ2VOZlFEbjZiUGlSODVCano4S0hDOGppZGp5MTNqK1ZiSHJQM3k3LzZLdlFRRmxiYkJxWTB5UGhOd01tSUJyajBjaWx3cjhsd3BzVG9Ob1lpWVJ1U1pvUENqZmxqaHZYdGVteEIxMlBIRHhvSE5pdjl0ZWZ4Szcva3R2L3VsN2t3SXFySHVteXRFMWRrNU5uWG9xS296M3JxcnVKYXBrTGZOYlNKMjRvZDdmYTRsZHIzZFAySkd2N2piRTdROHgzYUltajNhME1HTGIvZlhEb2hsZWQ5UFVYK2kwWDVZcDBmdGhWNzBQdFVUc2diSVRpcXk3UW4zLzVTK1VEeXlhemJrWnFCNTRtcTg3cWJlWnBRTHo1S2FLbWJILzlNanJBR1JQd3B0UlBROWxJN1d0SlVUT2lqZitibjRqZ2xldVhQTVBXUnVpNmd3MEZYY0FIVzVBdHA3dHRQUmtXajNCZjNPN2FISkg4cU5PZDVWTmhZUUcwRlZJYkR0M0VKWGk3aUtSV3BJa3Q5OTVUNmgvWmRHLzBYMEFmeGpaL29ITWNpZ0RGTzVqTm5Pa1V6MEd4Nitab3RpckRGaVBZTkI2N0hkeVA3ZHZ0OW4rL0x0ei9BSDdQSHRHLyt5STZIcnNjSEVGYUZsODYya1VHNGxid2twMGlNYjN4UURmRXpTTWhUV3hTWlFieFdtMVZ6VHB4OGVBR1RjY1BkSDAxKzJqTmFUQjJZRFRxMWdtelJZYkRMOHJvcnZmbHUxLzRNUTdlK1J1NFB0eUc4N2V6eDhRQkFhNjZpblR4eFpSUFhTcnZ1ZUJaK25hbTB1SGU2a0I4ZlkvSjJtNkxtYkhpdG80a1U3WmZ1dFcxVVh3YWtQNWVyVFErbGpvVG4rK09zZUdJUGFUWW95RkdoL2NvQXhFQnl4NUp1c04wQXBQT2JJWjRBWm1nT20wY0gyQmEwZFpUWEgyQVN5dWVGTklScnJxTU5RdGlNblJOQTBlSGdnNUZ2SzNsb2xEU0lBWXlKV1p6UGpQSENqcWJRUloxVmFTYm9yTXBaYlRxak5iZFZsZEVKbVAzWWpET3dpekhyQllKUWxOUElDcG1uYk0yZGp5SkRKYWhYVWFrZFdrR0toMzRyTGpYbWJHSVFGRVB3VGZCU3dncHUwdmx0SXlVMHlyb3dZUiszY2hSbGZiRW9hOGZjbGxmbVZGQXRtTytCTXkwbE1WbW9mdUtUcHIzY3Q4RmZ6clorM21ueHV6SHlCNHpCM1FRTGtMbGF0SzEvMXcrZWVHejVVV3NNU3NtYlJvS2t6M0c2QzV6WFlvTlhsdDExTldYZjI0YnpaTGdxMWxrR0tDRGVZTXMvcTZycUVaZEdBb0g3RCtDQUc1WVFOUzBOclA3MGxZRnhLejBuVGxRSzBwblVVV1cranhkZ2J4cE1XbGFpT1ZuZDg5RnBDdlF4U1NhS2RHcGpZMGMzRUJ6VGVJTEhpeTZpc2VOQVhVUHdacFUwYTBxNGdrdEdwdDFKQ2NEbHVMblRRUFVpRUpKZUJhWVJxVG94ZDAwTnhMWUNuVXBWYld0bjFlWGhQYzlGY1NsaUlwdHdqZXJ1cFdDcXVQSE5LVGpsM3gySU10bzk4UmRSQllwYkNWd0Y0Mm02ZjBMTW55ZjNmbXJ2ejNaKzU3cm9Ia3hQT0tXeTdleXg4d0JBWGFCdmt1eGx4ckh2T3N0K3Rubi9CalBLQS9RNFRTcGhmRzl6dWgrcDFrRU04RkhzZHE0N2JKdE5NZUwrSDVEQm4xdzAzcjRJVDIrYWVNRUpJRFFFRnpaWmVNTG9IWXVBcy9wOWZIQUVXQTlPeFc5cmtzNFliYWEyTlVsNWpuclZhcGMzTWpHNjNqOGJoYkhWU3pFSzZPYlcxS3NVaFp6c3FCZGpCZzlnMnZmNHpTOGdPUUVIVWgyTVZQWHJFSjJKT1BtQ3FKQ3AxRkZ6eEowVGJDNkZqRnlxdUxaNHQ0N3ZIc0ZTV3ZmblJjYzl5cnVWSGY0M2IyNEhKa2tQVzNnZWx4TGQ2djU2czNyS3FpM0ZOa3Fqb25RbXVmWm9HM2ZMM3ZlKzdicDduOTVYZXozVnY3Zng4NGVVd2VFalZIZHoyM2pxVzk3WTdyK25KMXlVcmRDVmtwS2l6RGFqWXp2ZDljaDRpTE94TEUxMlBhelcyVDRqQUcreDVEQi9OSnFTNmErNTM3QnordHBXTzkwaDBoazZqREllNm1Mb0N3UkszVnZPd1NlNGhleXlGelhPVk9STUZIb2hOaGpmUzREbVR1ZlJSZ3ZRSzdRelJ4RFZDbXh1b0loWkRFTjJKeDRGaVFIb2hvWEo0T1gvalUwbkxFRERiUTcwdEZIMG5pTnJHaVJhRVVWamV2TEdxZG1scXBkSENncU40R2E3MkZTTnpWTVZFU2tjNHFaeWFtTG9xY01rUVlmNzU2eGZuT3NqcmJpc3BRY00zem9kS1Z0aHgvVy9lKy9mSExudjNpOG5HLytMVC9XMWxmR2x4L0xNMy9sRGUybnp6bk5qK3JXckxUSmxRV1IwYTNHK3YyZ1MvVU5PWlQ3WVBtQ0pWbThZQkVPRkNkN0lKajdxM1NsSDVIVGg1ejZRWnYwZkkxYXNaVGVPNmoza0Nweml5L1NYVGZnVmhLOTJBelVVWFVVT0RVY2g0TWJ1ZlpxK3VjdUVwcUI5UVNWUXV3ZVYza1FLeUlhVGh3L0Z5cFRidWJ1V1VXS1JGcVJCYytRQ3ZoTVJFeXhMbmJ5blZoM0lJdElFYUVFZ05tTHVuU0laNG1id3FzVFduMU9FN0ZNaFRCSFhhNmpMTDR0b2MvZUJndkp5YzdrdGpHald5Y3VLSzBpaTdFSzZtMW1WdG8wL0hCYStiUEwxMisvekFOUy9Jam12TitMUGFKSnlIZXppNittN0RxZjVrLzI4SDgrOGxmZEsyN2FMZnZhSTBuRk1KL0FscWNKVzUraVhrYjFGd1NhcDRpc1hiZk8ya2RHc0UyRXJjU1hYVmNUZzZSYzRvOUlENSt2a0hlSitXamREWm52K0dwZFFFb2VJYTNmYnF1N3c3RVpSOTJtNitzWU5sWkpCV2ZPOUY4MytwSkVXN0R1dWxUVktFRUNHVVdTZmhPUTBBKzJDdStQZzBvVXJJcDlWeUpKS1QxQ205aXBsb3FzcnJ2QklmK2xkUzFjWXFrcFdDYWdybmJXNU5NdzkxaWRheUsxMEhHSG5MNUlldEVSeUhLOHp1ek9DZE5ieDRna0dTaXkyTVlVcERYcEdEYkRqemFIL3R2bDY3ZS8wVGNBRW8rTDg5V3Yvdkd6WGVmVFhQbFo4cjg2ZzNQZitFLzFFNmZ2NUpoeXdMSTZqUXpWeC9lNHI5N3RraFlKOXV4R3NMM3VhVmxsK2ZWYmFFNXM4SDFXYWQxOG93anhpR2FoMVNLKzZXTUtKSDhCNkxsUzNDbmk1aUY1VXZ1SnZmaFBmUTZ2Snh2MTlKTmU1OFhuNlBRZUlKd2R6NGprMkFIeDdKNU0rbE0wVUEwRjE0NmV2Z012NExIS2hoZEVTbWhmV3gvcU15TFRCTVVKc3BVNnBNbkJHS1pGaEZ5VHN5eXVXYkFzdFNpUjZLMVVnY3pnVVZaWTY4UjJKRS9QM2k3cGlJSDR1RGpxakwreVJuZDc1MDZpRWZlRk5qWlFXclJ6MWNGZnlxRnJmbVp5eDhVS005dm94RDV1OXJnNklHejBDSC90T002NjVETDltM1BPOEpQS1FYS0N4QUkrMmU4eXVyWE9RQWJFOHZvYXpnalo4cEl0TFB6VUFvdzhXaXFwZHVKSzRVRWlQaHJ6Q25WNkpZSDViTW1LdS9ZTEdBWldUTFIzNUFlRlhkK29pbXVGQzRTRFo2aExZN1ZvaWI4ZHdRdXV0UjJDNFZaY3lJSmtFVHBIaWdUcUdyeWd2UU9qUmZ2bmNzK2dXWlFjenhjTlJvV3VDbUVXUllvUW9WajZuTlVEbVMzUUJaMkptK0NySlNBWno5aUtQM3M3c3BZbFRiS1lDYXRmUE9SbGYwWTFlYXZPc0hGRHhKcVFlRis4Mmc1ZWMvSGs3cDlWeUw4QmoxbXY3enZaNCs2QXNIRVN2bjRMWjczOXpmSlh6M21tN0xSRFpESkpCNURIeHVyTndSZWVGcGlQSXV3K0dQem9nQzJYTEtQTENudkxSdEpRYWtIc0Rxa0hrTVR2ZXFaSzZsWm9sc2RZeTRyMDJpc0JMTW4xQmkrK0FjRjNvS1BtZ0ZISWVIN1E2c1M4YURFWDEwS29HR1RGQytZNVZuZnBSS0pRcU5lalFpa0ViMHpOL3lndTNvbExCaTBpM29FWDhaQmJFQ3ZGNHhxTGlCZXRVcjRpRWp6bzNrdUZlRzdjMTdKN01aVlRsdEN6dHB0dlRjS2hRbXFFZlBkRTFtOVk4VEp4a2liYVpMUXRBTmFZVHIxTlN4L2gwRFdYdlB5ZW4vR3JzU3ZnWVNPYkg2azlJUTRJRzRYSnkrR2tkLzl5K2kvbm5pc3Y0RkRweXBRbURRT3hzbnF6TVZzQldZeUlTd04rQUFSaDhaVmJHSjYzQ0d2dXZsS2tIeG1MMVIzZUtEcGtEdktZczBsZzBUcUJCeFVTL1g2SWJ3cS96cWFLbU5qRE5vdVFudXZQbTlVVHNvcWhSbmgyQzBkQmkyT0YrUWxJUnhRcUVoby8zdldhMTRKbnhEc0p1dVBPZzdTbzlESVZjZnA1RnBlczR0bWRJbUltTkoyNnU0aVp1Nnk0MjB6RlR4b2daMjlGZHl6QVhnTnhiTG1oL01NcTNZMkh4R2hRRVcvYnluSlgzQnVWcnJUTjhDOXQ5Uzh1V3IzclVzZkxGWThBVnY5bzdBbHpRS2hPK0ZFS3p1TGZ2a2svOXBNL3FhL0FTMWRXVUIxSWtvRXp2czFaM3cwNkpIaFlGSHdLdGcvYVUxc1cvOWsybXBNVjltWjhBalFTb2pnZXhMWjE0U2phemlZUmdxRTZYKzlvUGZxNkFsN3lwc2Y2dkt4ZlVLb2hNVUt5U0lSZklsY1VDZlNtZ1hXZ2RSem1CZkVPbHc2UjR0QUoxclA3VnFsZmNoK0M0N1ZrS2lKVkY3eG5ZNUZLL0VBbmZYb1FtbWtUaGRVaVZuQk9Xa0pPMjRJY09SQWRGN2QxZDVZRzR1Tk0vc0tLNTl1bkFSM0RKWWxiR2dZQll5TWk0NlRwT2x0L3p6OVoyLzFyQ3Y1RWhkM045b1E2SU1DdVhlZzczNFc1b1IrL1NIN25oUmZJVzdkdmRiY0hLSWdrSFRqZFBoamQ0VkxHMEF6cjRaYkFEdUsrRHNQekZtWExQOTZDTGlnY05NaFlzTHFKekFWaVBKcko0b0VqVUFjcnhBbG1ncGozRkUveHB6cWhlKzM3NWRxRzhkcHpLL1ZVN1I4ck5lQVhpV1dqQ00xT1JyUkl0SkVLZ1VEdHFxQkJ4bU1McjM3d0dmZGMyVHRtRWJLdFFHQzNwRUlZRWJLaXB0QWhyRG1leGVYNG9YRDZGbVM1eGRjNkdCVjBJWGtaTkhRM1QzejJ4Vlh4VHExWlVCbElxYXRLZ2c1a3BvbUZiM2FkZmNHbWI3bHNiYzhmMWZGYTMrcC9RdTBKZDBDb1k3dmFZdjd6VitqbDU1M1ArNTkycW14aG4zV1dwZEVGaCt5TWJrT205K0xhSXQ2VFBEbjRYcENCc1BpU3JTdzhmd0ZwMWV5QWllYjZjMTU2SjZQTy9CMExsalBINXVGWStrTEdMRUpzMUtsQ3JoT0V2c3J1UTdTSlJGVk41SGVpdGJIczRrVmNzN2gxTHBpNDVORGs4Z3hTbFZBbGk1VWFnc1BCUEY2M0ErbnF2RFlIZlkyUnNPemdqazZTTUlwRFVZNGZ3b2xMeUZJTEk4TW14UmtLT2hEeTNaMTBYMTZudTg5TUJna1hHSWpUTkFFR2F0RENVTnNiYkh6Yk5ldWpYL2pONmFGcjYzanRjV2t5ZnkvMmZYSEEvclg5S2xRdXB2eTcwL2p4QzErVC91emM1L296R0pQTG1tdEtyZ3loMndmcnQrQjVIZEVsQWxDWEVLWmcrM0ZkVnBaZXNNVENUeTNDVWhMYm41MUpRU3V5V2F6S2I4MkhvYUJCYlJLRmk3TlJoQmh4ZXBhK2lBbU1nZGJRN0NZaXhlcmNPR3FBZWRzbW0ydFJwNHM5SmEyT1NVZFV3cTV1V1NxQUlJcUpQcGNrdTJoWHcyME4vZTdKNmNCbmpwUWtIRGxFajIvZEdoV2RwSGd2QTNVcllQZDJkRjlidzNZYjdvMzdNQ0Zta3BKNG85SGVURWw5SkNWZFg5WS90V3QxNzV1K0JIYzlIclBkaDJ2ZlR3Y0VOdG8wWjhCUnYvdDYrUS9QZXdHWGJkc0tqRHk3MGNoQ1NJeE9kOFBvdHBqVjZ5RFFNWjRFSnU2eUI5ZHRLb1B6bHhnOGY5RjFHNktyUlh3bEhFNVNjTXFwQjBpbTlnUEZiU05NQjNBQU5sb3R4SWtZWVZxODVvWFNGeWxSbElUT1hSR1JMc0t4RjlTenUrWTZQck5OT1dBaGRsMnlPRmFuTmxFTkl6T1pNMkpZa1pCaFYxR1dtOGpwWEZ4TlJGb0pzTUtxVTI2YllyZE1mYlkvY05yU3BnRHVDelJCaldkSlBHdlREdS93am8rUDEzYTlkZjNBT3dFZXpTTFJZMm5mZHdlRUtFNWU5ekdLRzd6dmhiemhGUmZLNzV6eGREK2FFVjBaYTBwdG5JYmxvRE8rSFdiN2lIYk1vRDZCaXR2RThZTWdRMkhocktFc1BHK1I1dVFCREIxV2NVWmU5eUdMR09Mem5tR3BSVWp4ZXZKc252MEN1Sk5GdkZRT2gwNmlMVk5IY1pLSkdYQlV3RzRaQ0ljVXFXMllXdmg0c1JxT2N5WFhLb3BWc0lOYWp3UVhOeFJTcXR3NUVpaXpoUGpFdmR4ZHBOemVZZmNWYkFyZXFuc0Rna2dTOTJhb3JsRnhsNEdJNUliMmM5UHBMWi91cHIvNG00Y09YZXVnVnhBTXVFL29sL3h0N0xCd1FLaDVZUTNKdjNRVVoxenlCbjdyMmMva05RdExBcXQwTnFYUmhlRHd5NGVFOGUxTzkwQUZhVFc0SkVSUzVHYitRQlFEelFrTjdZOHRlSHRtNjgxVG1tRFFHcHY0eUpDdW42d1ljMlJNWDRUa0dwNER6TERSKzZ1b2w3bFRsZG9vdGpsQXdhd2pKaTZaS0JwcTQ5cTlOcG1MdUJ1U1FnRkxyT2VGdGlET2NRVnBVakR3QzI3aklyYW5VTzRvNUhzNkdBTXA0WU00NnNvc1NGemJSU1F0aUh0UmJ6dWRrWFRoL3BUNTlIVDZvVi9Zcys5WDEyQmZiZVYvMzArOXpYYllPR0J2ZmI4UTRFTXY0ZEx6enVQZHA1OGhwNUNCTlRMbWlRVVJFdVNETUxuRG1lNkpxbHFHb0FtWEZBV0VqZDFaSlJqUmptMmtPV1BnZzZjUHBUbTVnU1VSWmppcldSaDd4ZmI1eG5RRjZIUEJ3TmtCZGJXNWQwS3ZqaWxGM0l1TEZKa0RGN3hEcEdPejgxcmY0aWtGU2JsMjFHdmtWVzFpU2FVenlncVUrL0Z5YjhiM09iNXU4ZGdnbUkwd3BIVEI4dGEwUXJzbFZxeTl1RFdTVWlaeGcwMi8vcW54K2p0MkhWejl1QUIvY1ppRTNJZmFZZWVBRUxqQ0s2Skt0dGZDc1cvL2VkNTIyazc1NWVOL2hBVkdYbXdpa0ZBZElpaWVSODUwTjB6dlFXd21yZzJpQS9xcFNZVFRpWXV2dVdtSHBLMHErdFFCNmZUV202ZUtwQjBORERUQzhOUmhBb3pkWTViclhrZGtVYm5QeDNZQnJBa0hqTHhOcTVOYXpmczAxOGExRVhKL2xlUkxGYWNrckVOMDVIUXJCVG1vWW5zTGZvOVJSa1pSY1pGZ3ZDUnBCYzRpbmdNc01XaUVkbGlwYmt4eUFJZFNlOWZNMXE0djAvOTB5WjREN3dKV3JvSjBFWE5FNVdGbmg2VUQ5cmFwY2MzdlBaMnp6MzhWLy9ya1UrWGlJNDV5WVUwSzB3QUk2OUNkUnNSSEx0MCttTjBMNVZDQURhekJwVWU5dUxoYW9KcHRCTXlDOXlodFVkSXhMZW1rQmpsTzBLTWJkRW1RVmlwcmd6Z1pwVVRMaEZuTkczUDA2ank3U0ZHZlY5Tk9CVFRVOGQ3VTNNYmdJOGNPdXRnK3QzSW9paVE3WkY3R1NGT0NMc0pWM0Z2QlUvaXVUMEZ6Z0crYlJxUmRFaG9STjFPVDdKWlVoUzAwdDgzTXZ6SHBQdktGbGZWM1gzbGdkdFBoZk9wdHRzUGFBU0Z5dzgvc0lyMzR5bWdYZlBUVi9QU0paOHF2bjNFYUZ4eHhwTURJQ2hPS1pXbTBSV2tjREM4clNIYy96UGJpZVMwNGZVU0RKTHhYelZScU1URURuNENNNDk4QXNxam9vcm91Q0xJczZHSVNhVUNIQXNNNkJpeFZ0a1hCVGFMSlBIRVl1OWpJbllNdXRtcjRxdUZqb1V4cUcwYTF3ckdRWGljdkppTkFLRDNFTks2NGF3dURCWlhCUUpCR3NLS3VVeWxxQ0cxS2QzV0YvMjNsRTE5YW4vM2JkOTI3ZGozTUs5ekhGVWIxV05saDc0QzkrUzZVc3hDNU9PN29EMTdJeTg5OGpyN3R4T1A5cFNjZVc1RnJxNVF5RXhGMzBhR0V4R3NSNzFhUnZBK21leDFmQlI5VGszMUl3VnNkbkQyQmNJNmlvZUF5VmFTNFMzRXRYYlJCMU9vK3JRV2tDaU9VNkJ5a1ZDaThWV1pIUWFzdWlwc212Q2Q0OHlDZ2tReWVGWnZoT2xPbE9GcWR2RmtFWFJCUFE0azlsTEVVTlJ6WFJFcHlkelp1R3BkUC8rMkI2WHV2ZkdEOWZ3QTRwQ3ZBRDVjSzkzdXhKNDBEOW5iVlJhU0xycUtLVThIdm5zM3p6ajVYTHQvNWRIL2x5U2R3TW9zQzYxNXNFcWhuUnpTMUFpM2c3aloxc1lOSTNvOTNCOERXb3FuZDd6eUZ1S0pBZEQ4cUtqU2NyR2RXa1JJZ0hDOUJJQlJPRjl6a0dLRXlZYUMxUXZaQ2xTQVdKenVXZTNhdVVKQlNGWm9rTkZzZ0xWWUtKQlAzRE41aGpZdUR0bVRodG5XZjNsWGttbXZXcG4vdzNydldQZ2R4YzE1OEpmSndxSEVQRjN2U09XQnYxUkhuOU8xdmhXTmVkU212Ty9rTXVmejRvK1c1Unh4WngyY1RNak12WnBJd1FuazhvU2dldklCSVdjUExBYVFjZ2p3Qkc0TjFtM3FCbGE5TWNGRVBzRURzbmtpUE0rd2ROTGhzaXpnNVNQcTlCQ0dyTkpCYWNSb2t0ZXBwRVVuREFGdGdTaHByREsybm9jK3FqY1RNQjBsN3A4WTNKK1VyWDM4Z2YvSVRoL3lQcno0d3V3bkNnNjhHT2R6enZPOWtUMW9IN08yaG9SbG8vdmhGemZOUDNGa3VPZmxwZnVGeFIrdHAyNCtxUmNPRXd0U2RLVzVWTW1lRHVMeUNEekxZRFBjTzhURXd3MjBtbEJrd1JnaWtjK3hKWmNHRFdCT25vdUlCQ2FJNFQrSkNFblRnMEdnMGR6VG9NcXdUZk9wSUZxZWo2TFFSSUNFcUpHRjlWdmpteU5mdm5IRHRqUWU2RCsyNmEvWTNSQmNRdjRoMDlkWHdaSGE4M3A3MER0aWJnM0FScWgrbDlFdDBwOEFSLy81RnpUbExPLzJTblNmWlR5d3R5Yk5PM0c3QjRsK0FkYUREbUZId0lKcXFmSHVoeDlZalpWTEZHbGFHbjRvYjhlQjAzclFHWUxXUXFPTThuMFdSWXdZK0VhTWpLRmdNMTZKYTIrZENWcGdrN2xqTDNEMnlXLy9meVAvT0pWMy9oN2VWYTc4OG5kNEtOU3BmUkxyaTZpZFhqdmZkN0FmR0FUZVpYSFVSZXN5WnlJdmZPZWRhQkJqKy9qbnR6ck4rdER0dngzSDh4STdqT0xjb1ovL0lNYlJwa2FnWDY5STV1WWJuRXUzajJrNkovWkN1eWtOWEJGYUVhQUhIZXp4Z3hRMEc3M1ZJQ0ZaZXVycFdPWVhaREc0OUpEWmV0N3RYVnVUenQ2ekxIZmZ0bjE3OWIvWnhLM0N3ditoTnA5MlRvcXA5dVBhRDZJQnpjNUNydjdVekFneC9jUWZIdnZhVm5MMmVlZkhwSjhxTzVVWE9IaHVuTmkzYmpsaWsyVEowSGZUS25BbFlkNWpWZnp1eHAxSnArekJxZ0NRU3V6SE1Wb3o5NjlqS2hDNmJUdHBXdjNwZ2phL2Vzby9WUEMwMy84T0tmZTQvM3NvKzRJSE5GM2JkK1RSN2o4Vy85Z04yMm4wcis0RjJ3TTNtSUZmc1FxNjRDYmx4Qi9yY0R3VFp4a09zZVI1c2U5bFJMRno0SW81ZWdaMHJhK3hVWmR2MlpacW1CQ0hEMWtVR1N3M3FVMXhVa3F1ejNrblpzOHJzNEFTYlRMMlVLYk1kUTcxejZPbnZyNzI5Mi8rMXU1aDlIdmJ6TFJ6S0x5SjlaZy95bWM5aVYvVExCVDhrOWtQamdBKzEzaUhQdWdrNWRRZDY3Z200dnBQOExaenlNVE1SdU9ISGFXOEVUdCtLL3pBNjNFUHQvd01RazN6MjZRTDFSUUFBQUFCSlJVNUVya0pnZ2c9PSIgYWx0PSJJbnN0YWdyYW0iIGNsYXNzPSJvcHQtaWNvbi1pbWciPjwvZGl2PgogICAgPGRpdiBjbGFzcz0ib3B0LXRleHQiPjxkaXYgY2xhc3M9Im9wdC10aXRsZSI+SW5zdGFncmFtPC9kaXY+PGRpdiBjbGFzcz0ib3B0LWhhbmRsZSI+QHJqX2dyb29taW5nPC9kaXY+PC9kaXY+CiAgICA8c3BhbiBjbGFzcz0ib3B0LWFycm93Ij7ihpI8L3NwYW4+CiAgPC9hPgogIDxhIGhyZWY9Imh0dHBzOi8vd2EubWUvMzcyNTg3MzU0NTYiIHRhcmdldD0iX2JsYW5rIiBjbGFzcz0ib3B0Ij4KICAgIDxkaXYgY2xhc3M9Im9wdC1pY29uIj48aW1nIHNyYz0iZGF0YTppbWFnZS9wbmc7YmFzZTY0LGlWQk9SdzBLR2dvQUFBQU5TVWhFVWdBQUFLQUFBQUNnQ0FZQUFBQ0x6MmN0QUFDT2lFbEVRVlI0bk95OWQ0QWxSM1UxZm01VjkzdHYwczVzRHRxVlZscmxnQkxJZ0FBSklSQTVha1dPeHNZMjhBSEdCaHVEaGZEbkJOaGdrakVZYkpJQkxXQnlNRkZFZ1FnU1FrSloydFhtT1BHOTE5MVY5L3orcUtydUhrbllZSUx4OTZOaHRETXZWbGVkdXVIY1VNQnZydDljdjdsK2MvM20rczMxbStzMzEyK3UzMXkvdVg1ei9lYjZ6ZldiNnpmWGI2NWZ3U1gvMHdQNE5iNEVCSEFSVFAzSTNqaGZxOEM3L0x0OW5RamlFaEM0aStkK2MvM21pcGRnTXl6T1FZWkxZWEhwWmd0ZWJNQmY0TVlrQktTQUZ4dGN1amw4MTJiWVgraDMvQysrL3Y4MUNSZkQ0Q3N3ZUM2SXpSY1Q1aEw5U2ZMcEhDQzc1UWtiVmkwVWV2akNnaHR4ZmUzQ21Cd0Nva2RqTzNrR01RN2VBeDcwVHRUMlBiM1JNcCsweGVoNFovK3lXK2QyM1BTZGc3TS9jVHlrQUFKc2djR2JJYmdNSHY4L2s1ai9id09RRUp3TGkzT2grQXRSNk9LMVhic1dvN01uTGo5TGwzQ2xuanExakxtNUp3Wnl2SFJ0ejA5SUQ2TjJ5b2haelk0SUlLQVZVQVFpaEFGQUNWOENDT0FBZWdVOFlWVkFkZlBxdUE4RG5URkRKWTI1T1JOOHBiaCtkcjlNdTJGbnQvdnU0UEtET3hZTlNBQm9hOHlYUUg5Rk0vVS9kdjIvQjhDTFlRQVkvQVhjSFpkdjh1RnJ6eWhHZUNKT21ieVhveitISy9NeEdjczJZc0tDSFFFeUFjVUNDbVF3b0NQZ3FDSkNBYWdnSUFGMTFvTTBDa0lrUUZHOFFJUUNNVXBERVV0TFVBVG9DSlNFcVFoVUNsTjZjTHJhcTN1cmZhYmtIclBnUHFXM0RHOHVQN3JyQ3dBVzZnRUxnQXRoQVFCYjRIOUZNL2dydmY1ZkFhRGdZdGc3Z203a09lc1BLM1lXNTNlT0dyMDNsM2Z1cHhzNlIyQkpQaUlqRnJRR3FnSlRRU25HczFMUU9WRjFBbGNSVldtZ0RsQVZxQkJpVFB3bWhMOEJnSUFoQS81RVlBUXdJQ0JSTUZyQ1pyVGRqR0p5TVZsT1pDVERJRHZNQ1lGQ0tvTFREdGhiYmN0bS9QWEY3c0hIZFZCK0ZlL2VleldTU2pZQVhuRk9CbHoyLzVSay9OOE53SXRoY05KbXdVVmJhdWt3ZHQ1aGR5dU96czdYRGZsRlBMNTdESzFaWm1oZ0pqSW9GUWFtZEtVWGxKVkJNUlM0Z1lGNlFBd3l5ZG1URE10N1UzTDQrQ291SFpsQVQwYlE3WXhJdHp1Q2tVNFhIWk1UQUR3ZFBBbWxGNjlFcFk0RFgyREJENFMrNUxBcXVXOStXdmIycHpIcit5aGRnUUplNER4QkEzUnlJdTlCT2gxYUMxVUxBNEVWQ0xUd3dMd2ZZcisvdGJPMy9JUy9kdUhUN3FPN3Z3NUVLY2lMRGM2OXhPQXl1UCtaaWYvRlhmOGJBU2pZREJOcERnV0FTV0NxK3UzREw2eU82ajNWcit5ZWhWV2RIbklDVG1Fa3E1U0FGbjJMcWkrbytvVE5zUVFqNW9qSnRUaDVhaVBYTFRrTWF5ZFh5cG9sU3puYUhVZVdkNWgzY2dFQUR5OGVuZ0lScjBxbkhpcE9GQjZrS0VrakJqQVFWVUNNQ0dnQWdaQ2VHRmFGS1gzRm9pcTRkKzZRYkQyNEMzUHowM0x6N0c3ZU9yMGI4MVZmSUo0d1BVR240MDIzQTJNelQrODd6QUVMQzkwOUJQY09yelk3cTM4ejMxLzRTUG4xL1RjQUNGTHh2c2orTnpzdi83c0F1QmtXSDRKUFU1MDlmTTA5N04ybm5xSXI3VVZtUTNlTjYxcWc4TEJxU3ZVVVYvVXRCdE1HSGx6ZW1jTHBhNC9GeWN1T3hTbnJUdUNHOFpXU2pZeUlXRUdGQWVkUVlOYjNwWFFsblMrMUVscFBoWHBQZ3FDQnFCSUFxVlNvcUlBQ1VoVUNFU09FQWlRQmdTZ0FDNE5NUk1RWVdqRXlZam9jTVYzWVRpNm95S0lZeXY3NWc3eDU3M2JjZEhDclhMUC9kaDRZekFZN014K0I3WFRWZXFPYXc3SUhZeXBDZGd4bXNiMzhqTDltNFlQKzMzZDlDa0FKQUxqNG5BeVhYUGEvRG9qL093QjRNUXhlZVRFZ2x5Z0FhNSswNnY3NWt0SGY1eWtURDNkSDV4MXhnTlZzeUZLenNsb3dLT1lGYW5qVXlDcTUvL3JUNWN6MVorR2sxWnZJTHNTandxRnlubk1Zb3VCUXZDdmhCWENpSkZRSWtxcFVvVkFnWGowOUZUUXEzaXRJalRpTFpoZ0pXRXNERlRKNHhBSUJvNzhpQWxBbFVDNEVhUUFRc0NMU3NSbDZ0c3R4MnhNakZzT2k1TzJIZHVMYVBUZksxVHUyY3Z1aC9hS2dvdE5EMXVtUkhmSG9xUldJTlFjYzVOckJEM0ZUK2RiaWcxdmZBMkErek5YL0xpRCtlZ053TXl3MmIwYXk4VWFmZU5oRGgvZGM4bkpkYWUrRjBRd21OeFFWNTUzUFVNNEN3eGxaTjdvUzU2Ky9OODVkZnphUFdyVVI3RUJtTU0vcFloWnoxUUljZlBBUlNGRW9IRDFVUER3SXBZZW5oeWRSMFlIVW9OdW9JQWhGL05lVFFjY3FBRkFrT2lFUmdRSWdPTVFRQ0JDd0hJa2JFVWo4cFVaSWtLd3dZcm1rTzRiUnJBdDR4YzdwL1hMOXJsdnh2ZHR2NU42NWc0Q3h3T2dZalRGS0F5QXptZkVHY3V2Z1Z2T2p3VDlNdlAyV2R4NEE1bXJ2ZVFzVXYrWkEvSFVGWVBCcUx3bEdkdmRocTg4MXAwMittRWVQUFh5NERqQjlxSkhjdTNJaFEzRUlvNXJyZmRiY3pWeHc5SGs0N2JDVFlMc2RUT3MwOTFZSHBIQlZBSTRFUjBHcEtId3BhbFFVd2FiejlLS2k4TW9BUi9WUWtFNDlHQ0FuQklMdUo2RWdoQXh3UzZCaUU5c3dJc0lnOXdRUU1XTGlhd0dSZ0VDQlVBZ3hZZ05Bay9BRVNZSkdySXgxT2pMUzZkSjVZdHZlM2JqNnRodHc0NTRkNkxzQzZQVEU1QjJWM0RoMjBFRUJ5RzJEVyt5MUMyOWUrcmF0LzdRSFdBQWhlQ1hrMTlsci92VUQ0R2JZeEhuMUxsaDFMNTQrOVdJY00vbzRQYXdMbGw3aFJGMnhZREYvQ0llTkxNZkRqNzRmSDNUMC9iRnlhcFhNbTc3c0t2YXg4aVVnWkNHVmNYU292R1BGQ2dXOWVEbzRlbnA0S0x3NDlmQ3E5RUx4VUhwVjhmQlVwU2dKMHBPZ0JGRkNVRUVhRmROb1lNSkE0dE1DZ01hSWtNM0RFSU1BT1ZCRVdzSlFZQ0VRdFFHUUVzQnJEQUFhZ0tSVGhiR0NpWHdFdmF5THdXQ0E2MjYvVGE3Y2VqUDJ6YzRRblM1TXQ2UE1vTktUM0hnRC9IaitPdjNPN04vb2xwM3ZBYUM0R0JrdStmVjBWSDU5QUhneERGNEpRS0NyZ2JHRGY3RHVyN083cmZqOWNtTW5NNlU2MFk1VzFURGovRTZ6M3F6QnMwOTdITTg1OXQ3UW51Vys0b0JNbDdNb3hZc0k0Y1NqOG81RERPSFVpYU5Ed1FxVk9ucDZjVlE2ZWhBcVhoVWVIbzRLRGJRS1ZRaXZLZ3FDOFRHSWlGTFRFaVlWU2lnQkUwTnFRU0xTaU5UekdzU2RRR2pTQXpCR0FCVVlRd2dNaEFtcUFndWhFUW4ySTBNK2hNVHZWbERIcENzVEl5UGlQSEg3bnQyOCt0WmJ1SFgvQVVIWE11dDJWSzJTWXpZM0J4eXk3eFRmY0Z2blh1ay9zdk1MY1k2enBGVitYYTVmRHdDMnZOdk9NdzdmN0U0ZmZSV1BIVDBlamhTdlRpRVdNM3RrdVl6Z21YZDdOQjkrL0lQaHV3WmJCenN4NytmaFFYR3NVTEtTU2gwcWVGUytvaGVGbzVkS0t6aDRWRm9pZ1UrcG9nUThGWjRhN0Q5SklBelduaWNKVWhpVFdsUmorSTBhWFl3bzlBaElVSjBRRTZnWVJLV2FzQ2h4cXNXZ0JseDhuS1JJaERNelk1RkVaSWpNTVlJNGZJYUZvU2NoSWpMVzZTRTNSbmNlT0NoWDMzZ1Q5ODNOQ3JvOWlCaGxSbVRqdWVXZUV2TEQvai8zWHIzN1QrY3h2ejk5SEg1TnBPSC9QQUQvQ1RtZWcyb0NXTzVlZnRUcnFwTW5udW9uRGV5Q2NWWXlVeFQ3VEtmZjkwODgrc0h5dE5NdkVydWtpOXVHTzduZysxVFE5SFdBUWt0V1dvcURvbEtIa2c2Vk9uZ29uSWJmblRpcHZLZFM0WUxMSVE0SzlZU0hnZ2hBWkJCcjhGUjZJamdJd1RhRHNoWC9KYUdCY2tGQ1NqTHpyQmdJaEVyQ0JBaEcvemdnVWhCanlpQU1ESVFTTUFqU0dpT2dSQ294T001UVNjWWtNcHJ3UFVZRURQeGpKOHZFWkJZN2R1M0R0VnUzY1dGUUNMb2R5YTMxMmdIUXNWYXVIOXd1MzUrK3VQcVg3ZjhDQUxnVUZoZjl6NGYzL2ljQmFLSUE0ZGdEVjU5WFhURDVEajE1eVVaWGV0Y3BMYXFxdEp6ZnkvdXNPbGxlOUZ2UDV2clY2N0YxdUVQMjZReW9oSmRLRjNRb0MzNGdsYTlRc2tJbEtxV3Y0TlFITHhZQmJGNDlTblYwOU9KVm9WUTZxQUFLVlFhd0dSVVNVZVVxd3Y4RVVGK3pLMHBBSWdnWjhTYUFrSXp3RExaZkpqWUpTQmdJS0pGN2liSXVTcmdBU0FoQUE0bE9zVFVSb3ZIVGlTUTFRUlBnR3A4SStEU1NBU1FFUkNmdndIdkYxcDI3Y2RQMlhlSWxvODB5ME5CelRETGI5OEFWYysvdnZtUDNpeGIyTHV6NWRWREovek1BYktuYzdBK1ArRE81Mi9nbGJrM1htbGwxMW1hMjdPL0dLaitHbDV6K2ROejNidmZqN3VxQTdCc2NoRnBLWDRZYytnRktkZExYSVlkK2lESkt1VW9jS25WU3FZTlRoUmNGTmRoM0pSMmNPbnBTQ0sxdHZrUzFKTWd4aU1BZ3pNUUtFTlFnVE1oMllWRFpWUHJ3ZnFFRWVzL0Mwc0tJUldhc01JSTFDRCtGSndWVVFJVEdHSUVISmFHSkZnR2RrRXlNR29SbmhJRlRoSW5TazBJREF3YmhpU0JMZ3dFcWpCTFhpUFI2WGZUN1E5eDh5eTd1bTU2RjlISVIwbkVDVG1CNzVrZnoyM2pON0xQOXYrNzZQTGpaUXJZUStKL3hsSC8xQUR6bm5BeVhYZWJHTWI3Uy9kbjZmeXp2UGZJNFZVOVRHS280ZzdtZGVQajZzL25pZXowWGRyUXJQeDdlek1wN0tEeUdMR1NvQllhK1lLR2xGRnF4OUE2bFZxamdVRkhoR0FCWWVnY0s0ZFRScVVjRkZWVlBoYUtDZ3hMaVZFRXFJVVlBZ1FOSjllanJBQXUrTDVYcjB5aEI5YWd3RkFBWWx6R09TSVlPZTlKQnpvN2tVcGdLTTM2ZXM1eG5SUzhkNW1MVVVITUQwK21pSXlPY01DTXdrZ2ZwUjRoblJZQVFNWklGQ0lHa1dqRWlZa0lhdG9qWVZrSTJSQ0FheVd4QURBeE1ORDBCd0lnTkJKR3FkUEljRTcwZWJ0OTdnTGZkdGdzVlZhVGJBYUVWUmt5ZUhYRGVmSDNoVmVVLzN2SXFDSUEvaC9tZm9HdCtsUUFVWEh5T3hTV1h1YW1UVjkxdCtLamxIOVJ6bGh6ditzUEthR2JkY0ZyR0IzMzh5VDJmaDRlYytBRGNWTjR1aDl3TXZIajAzVUFIZm9pS3BSbWk0ckFxZ2xmTFNncDFXbmhIVDJjY2lkSlg4RlJVOEtJQmtQQlFWYW80SHgwTjFlaUtHamdvWjZwWnpMZ0RzSlduYzBOemJIZWpIdFhkZ0JHTW1yc3RPWjFualo2QU1Uc21XVGFHSmRrU2RFeEcwS0JqTXVUSTRhR1kxejc2Zmg1RHY4Q3k2c3VOZzIyOFl1RXE3QzIyeTdRT2NPWEM5VmhBbnpRV05KbVo2aTVIYnJ2b01nUFZRMVZCQTFnWWlnRkVvN0lXaytKN2tkOU9mb21SaU1QbzdFVExFWkhySnRDeEdicmRMb2RsaGEzYmRzdkJRM09VWGg3RWZVNHhzRWErTm51cGUrMU56d1d3djAyQi9lcEE4YXY2bm1CSGNmUkJxNS9KQzFhOTBaODhPc1laWDVrc3Q4WEJXK1RFa1ExNC9ZUCtBbU1ySm5CMS96b0lDUS9sdEorVG9RNWs2QW90VWNwQVBVcGZTRVhISWpnY2RONUxSWWVLS3FVNktFbFBKMTRVSU9GSk9IaXFxaGhqb1FJY0hCN2lUSGxBc3FMQTNickg4OGplNFhqUXNnZmc5Q1dueUxyZWVyOHNtMnFKSGhDSWh0dC9mU25DdktiWDB3R3lkWEE3OXBjNytlbjlsK0c2K2F2TTVYTlhZbGMxUTNZNk10VmRnWkZzSEFZQ3BZTUlVWU1KZ1M4TVRqS1NlWkQ0N0pTT0tCQXllZGNTWFoxdWxrTkpXbU1sTTRZSERzemc5cDM3NEswaFZJQk1uWXptSGJsODdocjlsejBQeDlhWjIzQU9zbDlsbHMydkFvQUdBajJIeUw3NWY0NTZaWGJtNUorVkt3V3k0TDJSM0pRSGJzRmpqanhmWG5IZUgyS3ZUdVBXYWpzOEt4WmFZYWdGNXQwOGhscGk2Q29wcEVDaERwVldxTlNqcEF2T2hYTndvbExGVUpvTHpnVXJlQ0U5REN5OEdNNlgwNWdwOWpIM2xKUHRNWGpBaXZ2Slk1Yy9Bc2VPSDRlT3lSY051bElYRXYyaXlSWFl1VEJqMU1EMVVVaWhDTWtBQVFnb0llMDZSdW1pbnlYSUYzKys3Q24yNE9yWkgrTmpleitCYjAzL2dOZjBiMFE1bW1GRmI3MTBUQWRlSzZoNldyRXBuQmQyZ1JnQ0ZGSmdER2dJYU1CakpINU1VTWNFT2pZUElqTmtVVWl2azNGWWxOaTJiWjhVQXdmSmpSTHdNcFhsNW9yWlc4MWxDeGRXbjl2MS9WK2xjL0xMQmVERk1IZ2xlYUpJZnN1TGp2bWdPMy9wb3oxS0ovUGVxaVd3Ynp2KytCN1B3dVBPZkN5dUwyNWhYd2RTc3BKNVArRFFGekx3UXd4OGlXRlZzSWc4WHhrSTVmQkR6ekw4QzZjZUVJZ242ZURGdVlvS0E0cVZROVYrenZmMzREQXU1Nk9XUDRoUFdmTUVuREoxcXNsaDYvdXZXTVhGRFRHSkZOcVZRSVVrNzVPUjhnTkZ3dEtuRk9ua3Q1SlJOQVZ3cGhrbVNOWGd0QWlBVExKNm1oWmN3ZThlK2g3K2RmdDcrSWtEbjVNRGRzQmVaNjJNZHBiQWFpVktKVUkrck9RaTBmME83a2p3eWlWYWtZSWdLQVAzWTBNRWhvZ2tqd0F3eGdBQzd0NTVTUHF6UTBpZWdWNjlXWmxiM05pZjBRL3ZleG8rZCtEak1hbmhsdzdDWHlZQURYZ3hJSmRrdlpjYy9URjMzcklIbys4S3E2WlQrRGtabVp2RnE4LzlVNTYxNlF4OGIzQ05pQm90ZENoejdNdUNHNkR2Q2hRc1VQZ1NBMWV3cEdORlowbzZWdDRoY1g2VmVqajRHTCtBVk40UllrUnNqZ1BGSHN6MWQrUHUzZVA1ekRWUDV1TldQQlpUbmNsQUpVTkZxUktJRFVtc1Nad1NJcVd6SkdXWURQMDBZUnFDWlhXU2Z2cGJHWElURU9XVUNXeVRSQ3NrOEhzcHdod3BuendtdVFMQTdzRnV2bS9IKy9IT25lL0h0ZFYyR1IxWkplUFpGQ3N0QlNCeUV3R0ltdUlPUXRaSURUVVFJVGxiVENJdUFRR3NDS2lFQVNnMnc0RzljNWlmNlFPWkZVTTZMTXN5dWJsUGZuVC8wL1hUKzk2RDMwV090Nkg2SldMa2x3YkFaUFBaN29zMmZjUTllTmtqc09ETERyTnNNSmlXVmM3amRRKytSTmF2WEtmWHpOOG9QaU9HMVFBRExURG4rMWpRQVFwZm92Q0ZETFZpNlN1VVdxR0VrNUlLcng1T1Bhc1F5NlVHNGxnb0FpTTVadDBNOXM5dnhUM3k0L2o4ZGMrUng2NTdESFBrQkNDVmxpSDhaUXdNak56UnJJdllBVnIvMWxFTUJIK0FBVktVRkFaQndsZjkra2l2SUZHRFFsa00yQmpSUzBFSk9sWEFDTE1nbGFYdiszcnAxa3ZsdGR2ZUlOY1UyekUyc1FHOWZBendWU0J6YWk1UldyR05JQWtadjhIQ05pa1FJRXlRbEhGL0NZek5aSDZtajVrRDg1RE1ra0FsUzYzRjFxR1ZkKzE5a2w2MjkvMzRwek56UE9kN3Z6UVEvaklBbUNRZjhwY2U5V0djcy9MUmJqZ3NySFM2cnIrZlIraVl2TzZSbDdBem5zdTJ1VjJveEhFQkF3ejlFQU5YeUFLSEhMZ2hocjZRb2E4d1pNbFNnOXAxOEZKcGs4SGl5QkJXVXc4eHVmUTV3Tzc1VzdoSlZzc2ZIL1k4ZmRhR1p3V0hFaDZPSGlJWkRVS2lxQ0xSYTlFN2FpQVJGMDFqeWhhZ1ZBcUMzNW1KRFMrNzgvd1JBQnlEOXgyNFFSRXJGcFNZcFFvVGtoV2kyRXB3ai9paEFsRDE4UERvbWd5QTRaenJtN2ZlK2xhOGJ1dmJzTXRQYytuazBRQWh6bGNVWThWSzNCQ1JHYTlqZndDdG1HQXVCSmpTbUJnK2liZE5DbXhtMFo4Yll1N0FBcENiUUZndXlXQnVMY1JmZHZCcGVQK3U5K0Yzejh6eHRsOE9DSCt4QUF3SkJRS0I3Nzc0cUEvNEM1WS9YdWZMeXNCa2JuQ0l4K1pMekQ4ODdDOHczUmxpNzNBZmxNb0YzOGVDSDhyQUR6blFvUXkxd3NBVkxId2xCU3NNdFVUcEsxYjBjUERpVlZNdWkxUktLRVhGWk5nejJDNVpPWS9ucjNrbVhuekVpekdWVFZBQjhWckJtaHdwVzRBUXBhcFlZeVF0VFZLdkhrcXF3b1BvM2NFcFNkZUNMekZ3ZlNuY2dBNFZoQlFqQnNia0hMWGptT3FNdDdSMGRFb0JEbjBCQ2NRSERHT0FycEdzVEc4d29LZ0dMZXVnNkpoTUFIRFhZQTllZGUxZjRKMTd0cUFjbWRMSmtkVkdmU0dnSnJVYm5DYVJoR3hZWXhBU2VJSjltRVhBTTZHZmhpVEVXb055VUdIKzBBQXdRbmdGbG1lS1BaWGdBL3VmZ0kvdjN2TExja3gra1FBVWZQa2NpL3RmNW5vdjJmaDI5NkNWeitiQnFzb2t6NHZpQURibEUzekRJMThsTTlMbnptS3ZLSlVESGJMdkI3TGdCekwwaFE2MU5BT3RVTGlDdytCY1NLRUZDblYwNnFDaTRtTHNWdUVKNUJoNmgxM3oxL05CRS9mQTN4LzlOM0xpMklrQWdGSXJac1ltU2pmYVlZQW9xU1lFeUFCQW9TUkRVTUtJVFl1bnM5VzhIQ2oyeVEyRDIvalZROS9FZExrTHUveXMzRnh0NDk1aXR5eFVDNmpFRWZBUUo5SXpZMXpaVzRsamVrZHd2YXcwbWUzeXpLbTc0K3pKTXpEVldjbFZ2ZVV0N1U1VHFZTVJDeVBCeDI2bVVTTWJISndNQmVEVTFjVlEzejV3T2Y3MG1sZnh5L1BmbHBFbFI2RWptVGd0QUZpeWxuMUJMMXRKZG9FZzFxMkVQSW82aThJRTJhK0NqczA0SEpSWW1CMEVNZXJoWmFrRmRoU1E5KzEralAvOGdVLzhNaWlhWHh3QXYzeE9odnRmNWpxL3YrR05mUFNxNS9rNWxwbmFyQnpNWXEyMThnK1BmaVVLVytEQVlCb0xNc1RBRDJYb0J0clh3Z3kwRE5FTlgyQklMNFZXS0gzRmtrNkdyRUtXc25wMVVPTkplblhTTVYzdUsvZWpXcGpCeGV0ZmdEOCs2b1hSeHF0Q3hOUllDbWhxUXgxSndzU3dHMVdzMk5wOEFvQmJGbTdobHc5OEM1ZnQrenkrVzEyUEc0WTNpNWVja0JMSWpNQ09RMHdYdWMyWVNWWkhjMDMwbUN0Zm9uSjl3QTBFdmdDUUkvT0tWV1lsNzljN2s2Y3VPVVVldXVZQ25yejBGQk1IQmtjSFFKQ0pFTUUwU0phanREMXRKYUhxbU5zT0FNZ2JybnNMWDNiVFg2TS9NbzRsSTh1bGRBTWlBWTRpSUdHTXJSbHNnclJpaEFnMFVrMXNoMFFIQ0N3eksrajNDeWxtQzhBWXdrR3gyb2o1MFh4cC92SFFBOTIxKzc3K2l5YXJmekVBak9KNS9QY1BmNmw3eUpxL0tjcXlraElacGVEeW1TRmUvZmlYQzBjTkRnNm1wVlNIR1QrTFVpdjBYWW1CTDFpZ2tNSlZLTFQ1cWJSQ3FZcUtqazY5S0JqK1ZzZXVIY0hPK1J0eFFyNGU3enpoTFRoajRtNnN0SkxnV05oQTNVRkNwQlpwZ2hIQ2NPclFOWjM2L3I5MzZBZDQvNzR0dUhwd0RiODFmNlVzK0FLYUs3dmRGZEt4SXpBQXJHUVFBM2l2UUxUdmdvL0x0aThLQUJSalE1cXBBbDRJaU1KVkZlYUhCNENxUkY0Wm5yWGtOSG5FeXZONTRackh5S2F4VFdrc3JPakVpbEVESTRwUTlJUklOd1p3S1ZVZEFKSE01UGpoekxWNHpuZWZ5OHNIMTJCczhoaFFDL0ZTcTFnRU13TkozdFZXU0hDaElyY1laYTlGU0lnUWdRem1DL3FoQnpKRGVLVk1HQ3RYRHc3cHg2ZnZoNi91K1ZGTTkvK0ZnUERuQjJEY0VaT1BQT0wrNWVabFh5aVhHTldGMHNLQzNZUDc1UzhmL1VjeXNYU0N1K2IzQVZaazN2V3g0QW9VdnNEQWxTeFpTc0VTUSs4UUloc2xDbC9CcTBkRkgrSzdxaUcxUUNHZU9YZk4vSmdYTGpzUDd6anByYklrbjJEaFMrbmF6cUpoYWYxZlEwS2hxcEtid0wzTnVsbCtmTStuelR1My93dStzdkJkc0pzQjNURk1kcGNpZ3dFVVVMcElvUUVoNEJCNkRJVXJ5aGttaDdjMjdPdUVMU2dadEJ3RkZCaVRBd0o0VmN3TVpvRmlsbU11bC91UDN3TlBPZXdwZk9SaEQ1VVJPd0lBckxTaU1SWkNHSWloUWJMaktBSlJWUldQY0Q4TGJzZy8vTUhMK0xiYjM0WHU4bzFpSUhCVW9RQ1oxTTVOQWgrQW1MZ1krYzZRS2tGa1lnQUZSUUxIWFM1VXBOTm93YXJIaWp3elg1Ly9udjdwRGZjRk1VeCs5ODhMbjU4UGdKRm83aHczY1p6OHdSRmY5OGVNTHNQQlNqbGl4Ty9aSVM4KzcxazRidVBSMkRxL1F3Qnc0SWN5MUNJQTBCVVlxbU9KRXFWV012UVZTMVpTSm02UEhvNkVpNVFMUktpaVp1KytHL2pINi84UC92ckVWeEFBS25YSVRVWlZHREVoWXRXT29YbDZFYkZxQUV5WGgrU2RXOStEZCs1OUw2OHBieEdNVHNwNGJ4bXNDQUFYdkdsRk1Oa2wvcXNoU3lYNHJ0cXVQYXE1d3FUNFFwbG00SUZUdjRRa2p1cTFWMEJvUmF5RmR4NEx4VjVnTU9UcG5lUGttZXNlenlkdmZDcVdkWmNpM0Z1RkxEZ2hMVHN4MVFLSU9EcG1SZ0JrK0plYjNxUFArZUVMeFV5dUY1aU9lRGhreHRRT1NQSkdRclNHc2F3RmdFcE1aQkRHTkJzUkFrNkZmbGlselFRQm5GbVNaZmpZd1MzK2piZGVoRXMzVzF5MDVlY3VldnA1QUNqZ3hTSnlpWFplZWZ5MzNUMG56OEsrd3RtUmppbDMzeTRYbmY1Z1BQaU0rL0tXZzl1a3lvaStEcVQwSmZxdXdDQ0J6VmNzNkZDd2xOSlhET0UxRDBjbm5pRmxxbEpQUVdnSnNQL2dqL0gyNDE2SFp4NytkSGhVaEJvUll3a29ndG9LbHdIRVE2bXF5RTJHZ1MvdzFxM3Y1TnR1Znh1dTh6dEV4bGRqUEI4SHhJbnpnVjJ3Qm9rVlpHU2RKZEVrSkNqR0NEVE9kOXVEclpFV0x0Yi9BWkorWStqWGdlU3Fla1dvS1pGZ3F5cEYrdFVNMFQ4b3gva04vdmNQZjZiOHpqRy9MYVA1YUlqd0dKRU1KdHF4bWhoQUVhZzZFRjZkNlpxZWZtTGJaN0Q1bTA4VnQzeWwyR3djSXI1bUdqWEJLNW9ueXVpcEpLYVF3UVpGVXMrdzlKV0hsbFVBc0FNNUlwVjAwTUg3ZHY4OTM3M3J4YitJYU1sL0Y0QzF4enY2MHFQL29iei8xUDl4Qnd1WFpWM3JwdmZoSHV1T2tkOTl3RVc4K2RBMk9BTHpIS0JrSllXdk1QUUZDcDlzUFZkTHZkSlhBWEFCZ0ZTb09CK3lraFdDbVlQWDRqMG52VlVlditIeEtIM0IzSFNFZFNVdTZrVUpaVVFxdVdRS1FMNjQ3eXY0NCt0ZkxqOFlYbzE4YWkzSHNrbFFuU2djRURKTkVPbTU5RTlOeml5S2pDeWVxWVNuUmNSMWpINGdXcHhSR0JwcHk0aFlIZ3lKOXFPU2dESlVodGd1K29NRlZETzdjVkxuQ1B6Zmt5N0Jvdzk3aEFCZ3BSNlpzWFhBcHNtYlVoQUdsU3ZReTdyOHdxNnZ5ZVp2UEJYVDR4bEdlaXZFK1NFWnZYdUdHV053YXFJSlFUUVNIUkpEMlJJNGRpV3BDamlHL2pqT0EwdkVteGxtOHU4ekYva1BidHZ5OHpvbC96MEFSbmU4OCtUREw4VGpWbTBwVGVWTUgxYmRVTlpKemo5NjVHL0w3c0ZCRHFzU0JVcnBhNEhTdThiSjhDR3BJQ1dTT3ZXczZNV3B3aUhFZUlNUkRuaTEyTGZ2R3YzQWFXK1h4NisvVUVvdGtaa3NoVlNoSk1VWVFsV01NU2pVb1dzeUhDeG4rT2ZYdkVMZXR1Kzk0cGFzNEpMdVVnakxVRmdFVUV4TUowbjYwUUMxTFFmV0JucXEvRWl4dXZoL0NXRzNXRHdFeHM1dktWOGFVWENHV28vd0hjbGNUT2tzY2VHWnFEd2pwRUpvSVpMcmZERWpuTnVMeDR3L0VQOXd4bXV4WWVKd3REejhKSjdETjJ1SThaYStRTWQyZWNXQnErVlJYMzRzZG8xVzZJNnVvWGRGeUtVSTRwM3hucU5BaGtEamVMVGVVakcxZ1lSQ29CVDRPRm1scDZ6dTBsNVRUTHUvM25vdjdKaTc4ZWNwL1RULzlVdnU0ajFmZ1k2Y3Zud2Q3am4rOXpwaW5Ka1hBVlR5aFFVODQzNlBrK2xpbmpQRGVWbndBOHlYQS9hckFuMDNSTjhOTWZCRDlIMkp3bFVvbkdlcGpxVldVdm1Lemp0V3F2Q3FVQVhWR083YmV4WGVkZkkveU9QWFg4aFNoK3lZVGxqcVFGVFVib0Vhd0Vmd2ZYSFBGM0d2YjV5Tk4wOS9RTHJMajhaWU5nbm5obEFOYVN1RWllbjNxS2xpeGxnSUdheDJEY3NRL3FZSmNkc0lVVWJ3SWI1ZW16aEtFcHdNN0lZMmJ3QkFUZllnd1VDaXg3OGw1dEFZS2tubkNoblB4bVY4K1RIeTc4WFhjY1pYenBIMzNQd0I1Q2FITlVEd2hPTm1pSGtLQ2tWbU01UmE0aDdMVCtGbnp2c0VqK3VQbzVqWkk1bDBnVkRTbDdqcUZGR1UycUpRU1ZScHNJTkpRTVVnMXVmRk9nUWdzOEw5cGVwSjNXWDJXV3ZlQmdGdzBuL2ZsUHZaQVhncEJBTDFEMTN5ZDNyTStBYlpwMnBzSm5yb0FCNSsrcmxZdG1RSzIyZjN5WUFWRnFxaDlLc0NBMWRHeXFYRXdGVVllbDlMd01LN2FQZFJIRlFxOVNBVnh1YXlkL2ZWOHBZVFg2TlAyL2cwbEZvd014azhZckNURk1BQXhsRFZTd1lEYXpLODVzYlh5QU4vc0JrMzk5UXNtVHdLNmlzb25LZ3hFbnpETU9OaGpnbjZXTzFHQ2ZaU2FQRVN3UlpmaDVBUEUrTmRVa3ZOR0FOVGxTakpBc2hVUmFDQ1ZHT0MrRklnZ0JzME5mMVJDeU1TU2tpazZNVFJvM0pEakkwZWh1a2w0M2phVmMvQjh5NS9rUXk5SWpPWmVIVmg4WXhBR1J4a0EyRm1NaW0wTXFjdU94NWZmUEIvWUdPUllUallMMFpzR3BBd09oNXBjNlQ3aVNwRkdpNFJqUjFpSkZHSEFDWFRlVmZvZmNmT3laNTZ4RXR4RVR3Mnh6NkdQK1Axc3lFMzZ2dVJKeDIrdVhqTXNrdlZvc29MbTFXRFEzTGsrRkk4OC96SFlmdjBIczZ4a0tFdldBVnFSUXBVcUx4RHhRcVZLandVRlIwcURhU3lpM2w4Q3FCU0Q3RTlIanh3TlY1LzVDdnhndU5mZ01JWDZOaU9zQlV4aUpRRUt6amt5TGpnQi9MY3E1NlBkeDNZZ3JHcG8wUlVnTXdCRVRqQmJ3aldWMURmck9jNDFxa2hha3FRaEFtNUxVaUJmVW54VXpRQjVQaHA2YmU0aU1uZmpVdWNnck9NSUE5aXE0NENJM0ROUWVkUmFvb3VMWTU2d2tKZ2JJNzVRN2Z3UHAzVCtMNnovdFVjdnVSd2xMNWlaaTFVR1lqM2FJWW9nTXFYNk5xT1hMbi9HajNuUHg2TWhhVVRJdEtEaDJjMFBjSVVwcHVLYWlBYUVjSG1TOCsxdkdYNEZHR2tZc3FvM0ZvTStZKzd6c2FWMHo4TXUvUm5VOFUvaXdRVVhIb3hKNCtZbkhKbmpMd0dvNWJTVitOUkluZkVnKzkrTG5jdDdFZmZGeGk0SVFlK2xMNHZaUkJyT0lhKzVNQzdsRVNLMG50VXpvbFREK2M5dkpMT080cnQ0T0NCNi9EU3RYK0FGeHovQWxSYUliYzVCYUlHRU5ONGdheTBRbzRNQjR1RGVOQTNMK0M3RG41WXBxYU9rMkFXVldBVDh3OGFSK3NPVmx6VVhwZFJ6dFUyWVJJSkVXcFJWU2FwcVFqNXg0djJiNTBURUlRa1F2V0dKS3NyNUFSRU53bkpMdFJhQWlsTVkzL0ZiMDJaK0o1a3BRWEdsbTZTcjd2cmNjOHZQSUJmMlAxMWRHd3VsZmVFc1NCVUdGV3hrTksxdVpTKzVHa3JUcEovdXRkYjRRL3VKa3dZR1d1eDNwUk9KV25IZWlmR2dTVlBPVkhhaWZjME1EaW80TEc5Y1huRTByZUNFRnlhREpwZkJnQTN3MEF1MGZrTEpsN3FUcGs0UXFlZG10d2FuWm5GT2NmZkhkMlJqaHlZbjJPcFhnYSt3c0JYd2R2MURrTmZvVkFubFhxVUd0UnY1VDBjbEM1VXFOR3JoN1VkT1RpOWxlZVAzUmQvZGRwZndxR2lEYkVCVVZWQmJERUpCRHNvTnptMjkzZmdnZDk0c0h5enVsR1dUQjZId2c4aXdBeVFtclpRb3VaQjZDUVVheUJaSzlob3gybWE5RkI0a2RRa2tGUnAyUDFNWG9XR3l2VmdFOGJQQ1FzbUZCcENLSm9rWFdnbkV4T2xBejdUNzZoWjdqU3VNT0FXQ29VQ1Z3MHhNcnBlZGkvSjhMQ3ZQd3FmdVAyVDdOb2MzcFZSL2hwQ1U0bW5vR016Rks3QUU0NThtUHoxeVg5T3QvTjZaQ1lMTm9RYWllQ1NSY3dwcFVWYXB3M0lhQ05HTUZMQzMwWXlUS3VUZTB6ZHl6eHA3Zk54RVR3dS9kbk11cC91eFJmRDRGSm81OWp4NDNHUHBjK0Q4OTVReEErR3NtSEpDcm5icHFPeC9kQitlS2dNcW9KRFY2R29TZ3g5eWFKeUxMMmlWQi9TNkoyajh4clQ1aFdWRDgwd0lFYm15eGtlNVNmNXJydi9Jd0ZsYUVBQXdpaU1NV0JJc1lTams4eGt1R1hoZHB6M3pZZnkrOWlCMGQ2UktOd2dWdWZFalY1djRtQlhKejBaVXF3Q3ZaUHN0RG96RDRCS3MvR1RvSWhzU2EwKzYvV3FrL0JSVy9IcDllRzU4T0dxSVZzVUd2OEplQU5ia2xlVHBLeURLUUdjbE9RVUdYcGZzSmVOQ2xjY2hzZCs0MG40eE5aUG9wdUZGSDZCQWlZa3hTS0txVHpMVVdncGYzTHFDK1c1bTU3RGN0K3RrcUVuRWljWHllRmdCRit5RXhyd3RiZGg4MjlrSVRDazFZNm91Y2ZTVitEMDVldndCUGlmR2xjLzlRdFBna0JBZmNMYVYrdjYzamdPZVRMM0l0VkF6enp4Qk80Wkh1UkFobGpnRUFzWXl0QVhVdmlLUWZLVmNON0ZKa0NoSlFhcG9jWldWVlFobFJlV0NnNzM3WkQzbnZsT1dUZTZTdFFyck9tWTJMS25uZ1NuRHBsa3ZHMStCeS80MWlONFkrZVFMQms3VElnNUdCTTBiTkNxc1pkZlhGbEU1NEVoRFptMTZwTUlPTFp2dUxITWczb0VZT3EwZk5SYUxBUU9tblZCUzRrcUlnUkZtbHhEZ3FLdEJZN1BhUHF1SUdhb01WOEFDY3pSZ1NWQnE4WkpTV3Nzc09vSVBPNGJUNVJQYnYwME82WnJLbCtaZ0NOU1E2eGFBRU1MU3cvRnE4LytXNXc2ZG9KV3M3dG9JbEpyZXlEWkYvWE5wSEhHTzZYR0hkanFpK2cwdkhlMlVuZEtaNFc5OS9qZlFBRnMvdW5WOEg4TndNMndlRHo4MkNNUHV6K1BHWHNFcHlzSEdNdUZnUnkrZWgzR3B5WjRjREFucFhjWVZoV0dWWW5DbFJ4cUphVjNVcXFYa3A0dUZoSDVPc1NXVks5alpuTE1IN2dGTHo3ODkzaXZWV2VGRUpUTllqUk1xSUdXQVZXUm1Zd3p4UXdmODQxSHkwMW1yMHoyMXNEcGdCQVRUT2dJTkVuMk5SdExPcEt3RUlhdVZEWnllTkpRZDhDaUg5Um1ZdlFWb3FSc0xFZ0ZJdjBYdEdXVWVHRW9URTgwQWJRR3E2eXRQWktrU2ozMGlJcmEzV2EwRHFKc2p4TFV3WWdWcnRqSUN5OTdvdnpIOWkrd2E3dnFmQW5iMFBJRWdNeFlVR2xHVFVjK2NNNDdaSExvUUZhUXRoTWlVZHRRd3JjbE1YNG5MREV4LzNIM1VPQnBVTkhqekxHblptZXV2QTgrOU5ON3hmOFZBQVVuaGk4cWYydkp5N2t5Z3hsR1phREFjWWNmTG5PREJWU3FnV0lKMVdzbzFLUDA0YWZ5bnFGQmtJY3FVU21EMDRFWXI0ZVZoWVY5Y3B5c3g1K2Y5dWZpMWNPYUhLclJqQS9CQkFPanBBazAvWk8vOFNTNTBtN2wyT2g2bEg0WVJSNEZNWDh1dWh6cER1Smp3bzd0aUtmSWdnNWszZzlRMGlNem5iREJHWTRSU2VZZElWRUZoclRvS0tUcXlFZTlQdUVWU041T011d1VrTFFMMGg1b3FlWm1mTkZFaU81blNsZ09PVEFwM0pjWWtrWmxDaUZRNzJrbGgxKzVRUzc4eXVQeG5iMVhvR3M3TEVONE1XWkRxQUFRWXl4TFgvTDRaY2ViVjV6K0N1citiYlJaSnhHYXlmRUlmRkxnL21KTmFJcEgxczUvMkYxR0JNWUkxQUF3Qm5OS3Y3RUwvNER4UzBDRW84cCtiZ0J1RHRYeVk0ODc3RnpkMUR0UFo3d2lzeG43UTY1ZHNRS2pveU9ZSHd5RHVxMnFRQzZyUStHY1ZNNnpDalplY0RnWUNHYXZHcnZMSzN3OGFjTk43K1diN3Y0NlRHUWo4TW5OTUtITU5Tb3FldS9GSXNPekwvOGRmbXJ3ZFJrWjNTVE9EWUp3Q3FmSDFQNS9jdWVVQW5wQ2tJa0NNbnZ3Vm1iemgzZ3NsdUFZTjg1VmZXQnUvODF3Y0JUYStFMHRjQkZRQmthbnRyMmprNUpRbHJ4bVJnYVJBbEUwU2ZhS2xxM0h4UFlJcUNFNVZCUEVLQTBrbWJEZUFubllFTFZGRm9JV0Jxb08xblk0djJ5VlBQVHpGK0ttdVZ1UldTc2hUOXF3L3NCSVZGZGE4c1duL29IY2MrcGV4czNzaGpWNWl4RlBOd29ra2Job011b3Iyb21wOHRtRDhHSXdxNDUzSHowUEQxLzNDRndDL1dtazRIOEZRQUJBY2ZMSUgrbTRCUW9mYVVvdkc5YXV4T3h3RG4wV012QmxTSi8zSllZK3RNbHdkT0s5RCtueklhRTBSRGhJVkY3aG5ZZlFZdUhRclhqNllVL0ErZXZPUmFVVk96WmtmNWhhTVFLVjk4aHR6cmZlK0sveWp2My9ac2FYSFF0MWM0Q0IrR2pVYWV4c1pZSTRBVDNCeWdPK3k2SS9vMmIzVHI1d3hUUDVqVE0vcWRmZjgzSzk0ZXdyOFAxN2ZBVi91ZnJQS0R0M295cUhFTTBKRDZpM1RDRVJrZ2o2UDNreVNKSXlERERaY2JYSEVvQkgxZHBraXFJSU1YdExrbmRVMi9nYVhaMllLWnBjNjRUMUZDOU9LR1ZzWjAxRFlVWnhwa1NuTjRvRG8xNmU5ZVhmUlJwVnFQb0RDQ0UwRkdGSnNLZmxUZmY2YTNRTzlRa0hTT2pqaE1iVGlvQnJ1bjVFM0xINTBUZ3hFdTFBZ2FEdmdHVTU1RzRqTHdRZ0NULy9QUUJ1RHUyN3h1NjE5R1E1Y3VSQm5IY1VNUm1IQlZZc1dZTFI4VkhNRFFhQlZ2RU9aZVZRdUtCMm5mTjBEUGw4SGlwT0dicVF0aG9Da1VEcEN5d3RsK0NTMDE0ZWJvK0pHV0R0VVZicTBMVTVyOXgvRlY3eXd6L215TlJSY0s0SXVjNWhwMGFUUytyNHJVWTMxa3FYeFhBZk5nNXpmUEhzeitKMXA3OFdkNXM4UlNvVktBeFc5VmJ5WmFmOEVUNSt4cVd3Qi9lQldnSnFhd2NsQUNZQkxMWmphNkVOYk95NGNNVlhNeHBWdGU1TlM5bVNhSktBR1ZBY0hOZEltZ2ZJTUgxdkVraDFDQ2JZYmxGa0JuWHBmWUh1MkJwOGJlNEtlZEhYWDRaY01uaDZwaVVXRStiVG1Fd3FYK0hNTldmZ2FjYyttWDcvVnRpOFV3OGFrWUZDUXdNa0JDNVdxUUpBYVdCRHJ4dDRBakFXODBvY00zci83TzRyNzRYSC85ZTI0RThHNEIrY0V3Qnd6dFR6ZEUwM1Iwa1BFQ2djTnF4ZWlmbGlpSUlxdzhweDZDc01RMklCSy9Xb1NLbEMyek40WlZJMW9uSGplRlZZMjhId3dGYit3Y1puOElqeDlTaDlpY3htMFFpSk5BU1VCZ0t2SHMvOCtoOWdycGREVEJaaXVqV0RHaVpKWTV2bGNLaUNRbXpPNGZDZ0hOWHZ5VmZ1KzNuZWEvbVpMSHdsbFRveHhnQ3FJQ2lGTDNEQitnZHd5NW52UVRXOVhVU01TRHlCcTlhdXRYcXM3Y0phT3k2aWF3S1VvRFJSSlNOVmtTY2FONGI5MG5MR0RSTWoyMkNJSmNlOWxNaktoUFg0WFpIS2psL0cwS0lMaExDcVNuU1dINE0zWFA5UDh0RmJQc3ZjNUNpOWk4SGNWaFJKRER3VUw3L25TODNTY2d4YUZNSERaOVRyQ1hOTnVXZmtyRm9xdWs3R2phL1N5Q2t1cU1lUnVaaXpKMThBQXRqOG40dkJud1JBZy9NdWN5UEhqQnpHSThZdlFnR0toMlhoTURZeWlwR0pJUDBxSDZSZjRSeEs1MUF5eEhJckRaRU5LRVExYU1QVTNWWlZJUlFNaTNtdXo5YmcrU2YvSGhRS2E2MElhR28yQUNGNzJCckxsLzN3RWx4WlhDMjk4YlZTK1NxWXlkcWlyQWdKamtJUU9JYUE5MzBzSHlvK2ZiOVBjTVBZQmx2NFFybzJaMjRNTENER2hJTDBydTJpMHNvOFl1M0QrTXpWVDhIdzBGWmF5UVcrOW9rbFphM1VlcFloZ1FSQTdGb1o3UGRvSDlZcEJtbHdkVlFyZ2c0dHFkbW1hTUkvSWtsdHh3YXQ5YUl3YVc0bXk3Z1ZXNGtKUE9wS21KVXI4Znh2dlZCMkxPeEJaZzI5aHFTMmNDa3lZK0c4a3lQRzErR1B6bmdCZE4vdFJrdzNnS2lWV1FGdEFXN1JSVGFQUnkyZE9sQXJNczU3NzA3cFBUSS9hZVdwOFlTRG55am83dnFKaTg4eElHRHV0K1lKV050Wnl2bkt3MEJRT2t3dUhjT2NMekIwSmZ1dVpPRktsRDcwYXFsY3lHWU9ObzZHd0RvcFpEeERJMHBEb29OcWVoZGVmT3h6c0hwa0pid3FER3gwdXlBVVVlODljNVBKOS9aZEthKy80VTNJMXg0RlJabE0rS2lxb2dtZm1nVW93NG1Wa3NQdHVRMS9lK0pmOHJqSlkxbTRrcm50Qmltc0puVEhRcjJ5WWswR0Q4WHJUL3BiUFVvTzAySXdFOXIrZVVSRHIyVUh0ZE5rdENVUzR2MUZ4Z1phSnlNSTRHTXdMcmdTZ2FPRXRxS21qQkhCOEsvVVFsVmFueDFVU05ySXRWUGFvcTloYUp6eE1OMEpiRGY3OGRKdlhod2FlR2dveHdyL0RWUmhMaGs5bE0rOSsrOXdmVzg5ZFRnVE1oZlQzTlM3b09rTEM2VEhFSkp6VlFtWDdCSWxTaDhRTlVQUEl6bzlQWC9zcVRXZWZpWUF2dklySG9EeFIvV2VvZ1loRzlhSGRQU3hKU01ZREF1NFNMT1U2cVR5UHVYMVFUVzRCQjdSMjBYcXV4eHRLQmk0YWg1cnVVS2V0dWxwVkNpc01aQ21pb0pVaU5pZ2dQNzRCeTlET1RZQ1VST29tY0NhQmNJNUdJdWlXbnVsek5CbE1idWRqMXI1V1B6MnhxZElwWlhKVFFZVGIxYUVRUVczUUdYQ3A4cDRaeHd2Ty9iRlJ2dTdJS1lERUl5ZkhhYzQySGJKZDQzTFVRc014RzU5cksyM0FOU1VOY2FXS20rQ3JsRXlhdE5sUm1PK2F1QVN0WmErQkNJbnJ6R2h0VFlDRWNONUJBVGVsY2lXYnNEN2Jua2Z2M1RMWmNpelhMeXZVcWFBK0tnQm5QT1l6Q2ZrSldlK2dKemRRMG45YXRxWXF3dmQwMk5OaG4rTHFnbDlQN3lrY0pGbFFlZ1J2Y2NCR01XckxuT0xKdncvQmVCbVdJaHcyVU5YMzhPdjdaNmlDK29BSXlnOWVxTmQyRHpEc0hRb1NLbThsMUtWRlFPLzU1UGRSOEJyZ0lwcUNncUVoVEdTUWFkMzgxbEhQSUhMZWt2aHZFUFRqaEV4VE9aZ1llVmpXei9ETDg5ZWpzN1lXdkZWQ1JHRGtONmVRQ0dwYTFuaytvdzRMV1Z5bU10cnp2Z3JobkNsaFFraEVxWjFRck9jNmFJUlN3OG5UOTM0Skp6WU93RmxlVWhFYk9BK2ZPT0FSRThwOFlWTmFDUUFLdVgxUVdycEdNMGpUUlZDalNQU3FPSXdOOXArUGxHYmtNU3ZCNW1uamZrUnBTd0R4dE5PaUhuaW5wQ2xLL2lTYjc5QzFJTkdET29JZVFBaXJCRjRLSjU0N0lXeXhod0dYL1hqdDlUQm05cWFRUHI4Wk9oS2loazM5Q0FFNGVBd2hjaThyN2ltc3hFWEhQWmdFTUE1ZCsyTTNCVUFBUUJ6SjA0ODBpMjNGa05WQWdicU1UclJpekZkaFZQUHdPdUZUcVBleDRKcUlvVGJsTkRRRHBkUXFiMzd5cGZvdVE2ZWV2eVRJeHphUXhBUUNpTUNyNHFMdi8rWEJxTkxnTGdIeUJSZUNyZkxHZ2hCL1ZteDhOTTc4SHNibjRsanhqZUtWMGNidURCREUrekxlcnE0YUVPS0lZUUs2WmdjTHp6eUJkQzVhU0swMVlndUI1Q2NrY2FKQ0FQUWV1MGJSeVJvUjlTYkJXaldEMms5YTRja0ppQnJSRnNFb2lTdXA3RVhVMVl6eVBhbjFkWkVUQThEbks5b1IxYkk5Mlovb08rL2JndXNzWERxNm04TEJ5MFplTyt3WW5RcGZ2ZW9Kd2dPN29ZMUhkVGh0dWFHVU51dUVpMkpkUC8xN0FoZ0JmQlIySlFBbGhySWFTT1BCd0I4NWE3VHRPNE13TWZEbjNqaWlSMi9McjhRNFJ5RGpGb1o2UmhrSXprV3FpRzhldEFScXFScVNCb3dRT0MrSW1jV2lKUVlFWWhJc1NhSFg5aURCNjArRzhkTkhjUFNWOHl0aWJvcTNKQ3FpalVXSDc3cDQ3eHE0V3AyeHBlSjB4SVNWVTlVdnZVQ2hVVU1oMkU1clRCUkx1SHZibm8yQWRDS0ZVWElyaGJVVUtlcTFzcXdnU0JnakNGSmVlcVJGOG1SZGdQSzRad0tiTEFNYXJzN3B1cW5KWktnZGlTcUxVa09RcjFHOVM2cG5TdTJIcXNmWitzRjhYekU5QkNUN1p4ZTFpb25DbUNQdm5uZ0FzSm5HQkVWSjFpK2luLzlnOWVqOUU1eWF4bWxiTXkrTkxDaDF3MmVmdHBUT0Y2TTBic3FkRnB0YkF2VVRuUjd6OWJidnlhV0dLMFRnakNBWmhnb3VOWThFTWV2Vzk2MDQvblBBTGdaRmdSdVBtN1BmYkFxUDBibTFNTUtVSkUyczRBRnFvcDBKRHpqSVMrKzdqUWY3VDhnR0dtaFNTVHFtUXdhaFVXSnB4NzFwTENYSXVYYzNKZ0VPa0FoZjMvOW13UlRTd0huSVVicVRWa1hVMGMvSU9wQWlzbmcrOU84NThTWk9HckpSam9OL2Y1aVF5QmdrYmd3dGZac1BRNERBMCtQbnVuZy8yejhYV0IyajFqbU1RZ2N4eEFKSUNKRS9LRjFUVk5TMGtsVjEyeEorSktrSDV0dlRCZ1VJQ1I4Q2lTNG5zbXdXbXgyMVdWL05RUVpka0FpRlpPdWptU1dPc2VzdDFTdVdiZ1dsOTd3WVFMR2VPOEV4akEyYUJJeEZrNjlITFgwU0RuL3NITUVNL3ZGbU5SVml3bFlVYUpFc1JjNHdKaWtHcW1ZY0VoQUNEKzZ1RUFGSFZaMXA4eFI4bEFRRWsrei84OEFHUFh2MFZPUHdtZ21vV2NZQVNXN25SemVFOTZIOHpSY01JMmdDQnRXVytVUFNTQUlFVUJJaUJFalZUWEV1bXd0ejExL2Z4Sk1EYmpqVmdzSEIxcGorYzA5bC9PN0MxZlM5cGJTcXlQaW9ScTFhbXZXdWFGaGpBSDZmWG5zK29janJGWHdVUVNJdlIyUlRBUXhiY3hINlp1QVlrd0lYejE5NDVObE5WWklXY3hpVVlkQVRhcXpjWS9yRkg1dFpFSXlrTVNuQkM0SnVHRllyMFp1Qk15azNGbXdGUWl1ODdEUVdBQmNST29GUlpNeXhLSkFiYVNYQVNzdm1GeUtOMTd6endDUStyelcrbEJDV0kwQThOU1Rud0RPTGdDdzZmMVN4NmZqMk9LYkNaaW1xMG5MUklVQXFJSTJRQUdSa1V6TXlSTVBCRUM4OHM1cWVMRUI5dmdQK1lzQnd4WFplVFFHOUxUd0lRbFp1aFl1bkxFaFhnbXZkUnV5aG95cldRa2lGclZKckFRa2Flajcwemg3K1JsWTBadUM5NDZac2MzRTFZRkY0TjIzL2h0OTF3WnZTSkpHRjlOUUQ0M2dCeUR3Z0tvVGNjQVpxMDRGQURHMTVJc3JYZ3NhYllSU2VxWTFCd1lHVGgyV2RpZjVsQTFQVk00ZkNGME40bjJ4bGdaaDRxazF4eG1iOXNaMUN0UUpOWW5xMmx4cUJzOVVpNkpzb2JneDcrNlVpbGZiQUlpcFZFM0paeXVNMUREam9DZ3JNYjFKK2U2K0svSEY3VjhUYXl5OVZwRUhDR2kxeG9wWHhYMFB2dzhPRzFsUFg4ekJ0RG5Bc0FqaEFZMUVVZnVHSWpFS0lwS3dCS2poQkFrU1hHM094ZEZITDRHNVF6TGxJZ0J1aGdHSjE1eXg0blJkblIrRHdxdkFTanJiZ3BtQmM0VDNEQjNkSS9HclBqR1hhYjFyMnlGTlpEajB6eGlpUDhQTmh6OGlnTGIyeFFLajdsV1JTY2JwL3JSODZ2YlBRMGFXaXZlK3VjOFlha2hHZTdSQjZ1NWtaVFhFNGIwamNlVGtwckJjeGxCaU1LSU5jVFNqVEQ1bytzUzB6aUt4ei9MdkgvazdrcnNPcTZxSTh5OHh1U1FtRmRkdVlMaU54V0ZVWVVvNkZTWkpWejlYUHg1SXc2VGNHRHhhalJZbmExQW0wU3ExY285OFZTM3lLUFhFb3pad0NGcW9DS2dqQm0vNHpsdlQxM2pFN0c0VGU4SjRlcXdjWGNyN3I3NG5aSFlXWW13RUVsaVhDWHF3TlpiRXJhSDJ3dEtzQ29BeWJIeVczbkdWM1dEWHpOOGJCSEEvTkwySjd3QkFBSUMvZTM0NmwzUzZVbEFoQ25xcXpZS3lESjV1VUxlaFd4TWxNZ0FNSWJZay9RVWhyUnpKTUlEM3BabVVwVGh0K2VuQndoR0RsR2dxOVowQVg5cjVGZXdvZG9pMW8vRHdNY1lMa05GeG9OVGhweHJITUlCM1hKVXQ0Y3FSS1hwVkptb0hpNEdXdW1NMW9uVHhSUUZoeFZCVmNlVGtCdHh2OGg2QzRoQXQwb2xFclUyV2hHdVFTSTJkbmlSVlFtWDg1QnFGZHdBazZsaWZRZnlTSm5ZWHkzT3hpQkJuQ24yeC92NmdybG4vTHd5TlVCSDFUbVRKU254NXoxZHg2L1EyNmRnT2ZISjlFaENpMEhqTThZOFF6cytKbUF4SW5wZEs4OVdMNWkyMmlHZ3JtNWpKaFNxc1Bad1NTekx3dExFeldqZHdGd0NNQ05TSjdzUFFNV0FSYzNJOVlIT2pQcG9LM3FOeE5JSUthbml4c0d1anlvMlNRa2tqRmpxWXhnbmp4K09ZcVUzdzZtbU1MQjVNWExEM2IvMndjbVFpR3JsR3RCWWhLZDdSeUs0b0RXdHVKSFl2RllhQzFnWUtpMjdjMUQwRHcydGJ0bjE4U0FGVThEQXdlT2lhQndIREJVRHNZc2N3ZmlqcnJZUDZ2NDBkR0FhY1V1NVFVOGQzVU1tMTlraWtjL3drTlEzSTY4ZUNhWmxHd1BvRGtyYUlhRXgxQjFGQ0dOUGxuQ3pvNTdkK0VRREVlNjFuZ2RFWkFXRE9QdkkrWERtNlh0MXdMaUVycXZ3SXFFUnZwTnFSd0p3MW9qZHRNYzhRMXF2RTBCcklxTDBBQVBFVkx1cWlrTVlnMkx4RlY2L0dHS2Z5NDFFQUlFMFUveUtaTVNtVEpaVlZOWVk0dzU1Tk1WRWcxUmJXTWdJd1FML2crYXZ2bTBZWnZZcncvUjRHbVRYWVBUaUViKzYvU21Sa0hQUXhEcVlNMFlpUXB4RStQMFMwUW4xRmJYU0xsQnE2SzJTbXFRNWtJenBFa1ZqeFdLMFVuMmo1Sk5IbnJCMGtQT3J3aDZIcnVsSzVRVnd0SnJZOHBrV0ZZWVl5K1NUUTRqY3piUmVpemgyc0pSYWF6Mk5ialNHOVZwSXRIVU44S1htMDdYQklqV0pLQ09rZzFDUXZGczhrdkRjeU9tSStmTk1ubU5ZQVNJb1lzTWJRZVlmVm84dmx6R1duR3ZUblFzTWlUOVlEcjlVYkY5c1ZOZGdaY2d0dGxPZ1ZnL2dvUFBRd2UrVDQwVWV2Uk9nWVYrK29NTXViUXpScXVHcjBlRE9WSDhPRmtGc2pQcnB1VmdDTjZwZUpjR1pZek9nSzF3NUlxdENKQ0NQaklRSmxuMmV0T0RYTVhlMVloWmxUSCtwM3I5OTNEWGVXMjhUbW8xRHhpYlVQL3ErUFdGTEVWS1ZvcmNmYUQ1R2N1NFlIc1hkd1NBQURGMUlBRXRCSWhwTXBZNThnYlU5Q3VsSklWUkRQY3dPNGVuUUQxNDF0RXJnaEJDMHBtQUFRMTVmMVF0UmlOY1hmSWtjSUJHWSs3U0lrUkNicGt1eXJWSkxYK09mSlVZbDMwaVFNMUlJbkpSd3lKa1NpVmhNeFlLNnNnTzRrdnIzbmg3eHRlZ2M3dGdPdE4ya2NSbWlmeWd1T2ZnQlFERUp0ZE8wSkJlMVdHNjcxeG8vb2I2UnY0cVVJRnplTTh4V1daaHNHazdPbmdSRnZpd0NZQm5EaTFGcU9kU3c5UENnQzFkQisyQWpVb3laRVF5cFRjZ01DR0VKWm96WmpqVGwvQXNDNUNpT3lRdFpNYkFqNFN4NjdTRWk4ajFQdzNiMVhVMHczdVBRcU5SeVFvby94RHNQMEN0Ti9WSUVzNjJGWHNZMjNUOThTWmtIdmdEQlo5TnVkd0Fja056Yjg3a1BVd0d5ZnZVbDI5MjlWc1NNaDBhRlduVzJoR2U0cUd2OWhSdUtZSlNFay90S0UxK0o3R2Q4Y0VoSFNxMUNIdU5KWEpadTNMVUpyQ1ptMHZVanRLSVNQWmZKYVNWQ3lIbWJjUWZQRGZUOWlBQnlUeFlaSVJBb0EyYlRpS0VobDB4U213WVoxU1VKWW83SkF6UVdHc2RXV2xRaDhNS0RnQmVnSnpNbTlOWGVjOHlnQmcvMG5LM3Juc3BjRllnOElFWTdRSG9SZUk2QWk2eEo3RGRmUUNFK2FoTVpvZU1VRFg2cUNSK1NyY2NxeTQrQVJFOWJqT0xXeGlIRFo3VjhDc3dCTENKcHM0alR4VEpWdFlUNXFqeE1VMElBVzJObmZYaThoMGpCYUtwZXhvanl0VWJycXYwV2dWQkVqOUFDZitmWG5ZMkE4ak9Tb0V3UFNKN2V6VmVKampXSVBTMVlMaVhhOVFOMThKcjNXb0Y3Y3hEd24yNnNWaW90QURQZWtpNzRud0VPMXVhMmtMcHZjbWlBY3V4ay9kdU9uVFp5TCt1VUtBNU9GU01sOTE5NExhN0sxOE5Vd09IUDFna2t6aG5xWTdVdVNWQStTMkNFQXRRS2xsNEVUMlFWdHZBRjNrSUJGVDQ1aVJoaTJaaWVES0wwaENmcHcyazhUbFVtMlFab0VEYU9LNnBsVWdvYW9TdWxsRnIyc0o0aW45c1FKTWtJVmE2MDRWUnpVT1lQZUtEVG12aUpLL0pqRUtWR1hCQ0Q2a0FNSUJxNFJDa0dleThlMmZ5N094Q0xYSW9ZdVVudXlaRGpkY2VwQ0hibFR6d3daL3V6N0w4Zmx3Mi9CVHE0VFg1WE5QVVlKbjFSdlhaU2tDTFVlWVBBSmxNMmkxSE9FaGpPcmdkSjJZNUtuWEEreEZmdExYOVlDWGxzYXBsdXB0eDZEZ3BGZ1JOSjVzTnZCTFRPM3dwTml4WUIxdVJVZ0NxRXFwa2JHY05qRVNxQW9ZN29oUXh5cnZnL0diSWlvcEZKVlB1Tk1hd3hBS1FuSFZLc0lQOHFOQVlBbjFvTU5BTHhtU3hCdUsreXlvRXBqNU02RGFtTTBRd0ZTaGJHdW83NXRqVWxxOWZjbk5TekNrSHNxMEFwSEx6MnBoZGc0bjlHSnN6QzRlZVoyL0dEK09rcmVDd0NNVWZWNmp0UGVaNE9vZE1NS1FMVUNPbVA4L3Y0Zll1Q0dsTkJuT2I1Yld6SXZiUDYySlo4V21oQjZyZEN4T2Q1Kzgvdk4zMTcvV21UTGo0YXliQUN6bURCbW9sOFFsVnJhTk9FUmFWS2JnbGtVdnF6Tk9pS3FYcWJuRVROYjBkaUFTZHkzWUZyYmVFa01CMjNZcVBER3hXVlNsWjRPNkU3aDhuM2Z3KzNUMjJuRU1HV3J4OFBuMGpueDhxQWp6Z0VHY3pRbW5vdFMwMGhwQWU0azlwdmxUWDk2QUY2RlJNYVNrQlhaT2t3ZU1RVzVwTGJCUTVyY0pkQ2p4bGF0UW1hT2s2SVdGcWsyVDV1eXc3U2owN3F5L3M3V0RvaHpGWjR6RUtJc2NWWUFJRFFkZ2hIaHBMRmJ4Y0pnTC92bGZvcmtiTjlubUw3d1hhMGxZTHpQWUcwcG9hbzBuU1c0ZXZaNmZIdlhENUNaREUyNHpNUlZrUlNXU3pxay9wVWduSytRbTF5K3VmZEtQTytLRjlFczN5VHFIT3FzNUtUMmdoWU1Tb2dDSkpzRHBwRStpNnpPZWxLazRTNkpSWFJrbU5vRW9CalJxTmU0bFR1UkpGR2NaQVZhVkJSckowVmJkbG45aFFLeFZvWTY1Rnh4Q0swWFFJUXg0QmdBZmRqb1dxRHNtOVJsTHI2eVplZ21lelRDcE40ZzljWUlydzltb0tHbllyUnpaSmJOSHcwQXVEZ0I4T0x3MGZ2V3VXVXdabVhJNENQQmVNU2lRVktwV054REJLaHp3bElpWDNzeTY5OHRVQlZZbVMrcDMwbUFyUjBxQUxCUXpndk1hTFRXcEcxZXhFQkw4aElscVRIV2FJaW52aGdWUWNmZ0F6ZStId0RTYnE3QndOajhyQmtra0h4TkpkR3h1ZXpzNzhHVFA3dFozZGdJQkIxcUUrSUtid2tTTFRWdVdZeTF4aXRydlphTjFHejdQcnlMMzRuR0l3WWFnQzRTbUMzUWh1OXUva29jWWVoamdSZ3liRlM2RW9aR2tIWGtCd2V1Qy9mZExJTjRxRWhrVUZjc1BSeVFjVkdIR0E5dURhU2RzQnJHMUVqbGRMOEo5dzRVcFlxbjU1Z0YxMmNUQUlCckV3RGpMM3FDNmVtb3NWSUp4Q01MMno2VzRmc20zbHZ2UG8rVUNOZXcvVWtTSmpjOTZUZHhrbzJNaFAxaHhMVDZTcG8wOW1zV3RnSVpSSHdkL2ExM2Z3QUpGNmN4TDdJOXd6OWVIVEMrREIvZThSblowOTh2dWVrZ2RVUU55WmloOUlaczhZQUNLRU8yOTRIQkFUN3NreGZ3dHRGNW1PNGtQSWRoT3llSlh5ODBwSTdma29oZGp4QTkyWHF4d3p2Tkl1M1FrbDROb0pJWmxyd0NUUVhxdGJwRHk4YUw4eHhaSnNiM3BlOUdDeERCSUcwK1A2T0lJWUVodnJmanU4R2tWcDgwQ1MyUTJvL0lHYXRPd0xpWjhCNHVPdlF0V3pZT0lIS1VpWWR0bms5Q0VpU2NobjNnS2VnUlBDVmYzYnFSVmtoZzNaS2xzQVp3REZYdkdvejhHTmJnb2gzZFVFUHRXc1dXU282aXFWNkREaWU3eTVycFM5VlVOZEtBYXcvZFFOQWptcnd0M2lzdWVXMTdNUm5GNGNQclJqeUJ0ckg1S1BhNy9YekhqOThOQUtIRGZ2U05rT0pWb2ErVlJHU0cxM25IQ3ovK0JGN3BibU8yWkkwNFgwUmpYMXZxcFMzaVcxZWFrK1JjSloxZVc3bFlEREMyM2xjN0txYmhBUmM1SWkzZUw4d0JhMXV4K2E0MG5SSVRCOXNxTytuWUdNVWcwYkVjRHVmQ2s1S0FFRW9lRW9yWGRLY3dwZ2JRcEVYU0JEUUNPdWdQb3ZiQ1dsQ29MMDhBTktJa2VvQ09tV01CMUtGZlUxTXd1UnlGM0tEbTkydkRPVW1pdEV2amJxODVDTnpabHRFbWFxYXFNRExHeVh4SjJFWXhZMUlCYWlDUkNRRFQ1VDdBSnJsWmkvZTBZQUpRazlrRlFPSml0M3lKdUNiZWlVeXV3UnV2ZXp1bnEybkpUU2FKUW1laVBBQ0pIZHJpTkJMV1dvd3VXU2JRVE9oU2lSZWFIYzEwbnkxOVdIOHZFbEFhSUFuYTltQjhUV3N1YXpVV3M0Q0NGSkhtaFJFTTlXY1pMSHF1WG85bWQ5VzJXWHZNZGZaTWF5NGxseXE4SXNiYVFwVElHTVBVVDlPWUhDYkxZdjZLWWNSREdFY3J0QmhzcGtXeUpBbUdjQU5xZ3Bua0FlWUdjQnBQNXRrY0FYak4zaUFKSERjaHQ0Q1hHT2FLRlM2SlZVODdNTjE4c1BrTTZpS0x0QkNOYldBRVVDVnlaQnpOUm1xeGwvU3BNWTFOV2JGQ3lqK1ROUGxwbHdmcVFacEZqNHEwbGpScDhaVEJHUm1SM2RVZTg1YXIzaEV6UFJRaEJLSUVRK0VyRTZzYmN5Z3REUDdoM3EvQmlxb0hsSVVJc2dZNnZoNFBtcVMvdGcwRTFJbEdxb3VUcmVzb1NOeTBpZ2c2UVVxRmlmZlRVTmMxY0NORzZ0QmEzTlpKd2xFUVRraVBjNVFja25wY2JZc2xUQ3BKZ2Ntd3Y1b2xBSVRXdmJVRWxDUnhqYzB3MXBzaXZJZlVuWjdpT0VJV1hwdDZhU2FvUFhaQmJhYUFzVlZPaDhFR2pMaXJFVkJaVG5rSi9aa2oyeXcxcTVBbWxRd3RXbFZhS0UrekxYZjRkaVFEWHd5Tk1iRlBpZFFHUlMzODQ4TDVXb3h5OFgrQWtGOGFTd0daTmtEYlMwUThEMGhnS2NvaFpHb0ZYL09qTjNMbi9HN2sxdENqQW1CUVZ6YTJKTFlSZzBvOWpwNDhISzgvK3gvZ2Q5eE93d3loazVLa01FK3o4ZXBjT1RZVExDMndKUnM1ZlVkNFQxTm5tKzRoWmZScS9Ub3VXbHhOQUkyL0w0cDZJRFU2Yktub0ZsZVhiRUpCK0NOd2Q2UjZ3RnJjMXQ4bEExOHhnMDJKM1JFTkJqQ0tQTTg1T2JvTThDVkNzRXBpNW5GYzdKZ1ExcFE4MXVNSnlFL3VqMWN5Y0tLQ0NwQ0o3Q2VrWXdsNmRaQ3NTUzJMWkhJYlcxSFNCNGE5SVNGcnVaOFFqM3JIQ0lsMDBFcjcxUkdwOFp1TWh1TG0xaUxHMkcrZGVwUkNRbTFQc1A2ZTJoWWlDWnA4Rk5PY3c4dSs5UmNBREZobmZ5aGlmZWVpS3pkV1NsL2h5Y2MrRGsvZjlFejQvYmNpc3lOcFVhUHF3VjEvTDJwQUxwNkxOalBRZnZNZFZUT1F3QmsrTERVc2JNOTVjZ0xTQXRSWiswbGJwN2xKd0pjMEh0YmJQdUNUUUlaRGZoWjlQMERTWWJGc05lUWhLMkVCV1dHWEFPb2hTZE5JYzZOeEpkTVlwSG11aFlIb0tFcmRXcEZnTnhDTE9PbXlJSUhyS2JCaTB1b3lMclkwT3JobFlNYUpYU1JuNDkrTGdKZEdFYmpvTk5RMHA2Sk5xK0w2ZzFKQkRldXBSRzJJcHVkYW1LMTNJeGpiRDhTL0tmQ3VGTHQwQTk1MzgzdmxDN2Q5V1RxMlM2ZGVqREVJQnpjM1psWWNQS3dZT0hYeWhnZjhIWTdOam9PYjN5K1pkSmd5VWhhWklHa2RnRFlmR2ovTk5KTVFoVmk2LzhYQWkvWlU3VWd4VFU2elBkb2dsOVpjTjVNWGFaSjY5V1hSKzVvYWtUVFJBcE5oNElad2RXWVVXM1Z3Tkdtc1N6cTl5SENZTzBSQ1dnb3NwZVluWnlxTWpXQjl2eEpVWVhpdldBbUgrbTFHQk9DdWVZbHowZVQ0QXdnbi9WQkFOUFJIa2tpTFBLOWs1RW9EdkhxVGhKdDNWSmJxRnRtQUVJaEpXUlFBT3ZtWXdFUHFnU1M0aGdWczMxQkxNcGdFVlZPL2xtbWdBcWpDVFV6aGVWOStvUXpLdmdpRWQ4d0FpWmNBUW1zc0NISkozc09ISHZodUxKMHU2SXUrV0dSdDhDMjJmMm9icnpVdXZmTVhMQnAzUFYvMWRveHpGVjlaUzdEMmU5TEN0bDdMdGcyczdiWWE2Yk1iY1pRS0FlTmNlaGZmRTZaTFFxWmFtUGpVbnRVaVI5QjBJbzNaTEdrc1VsTXZHb2l0TzkxcjgzdjRhQ1drVHRVTWw4SGFjUUtBRDZkRmhkVkk5ZTZKUkdnS1h5UElvN0pQMlFJdE9yT1doTkhlTVJRcHE4ck11MEY0c3JFQmcyQ0xpYWxMdXlzRlRrTGJDaDhuckxhTm91M1RybE1JUTVBN0FhQTFCczlLc3ZFcFhLKzM4amxmK3lOYVkrQzlSM0lqMHF2akpRQWtON2s0ZGVhVWxjZmpZeGQ4bUwzdGU4aXlIMUtUS2dKZUFFZUVLdnk0NEQ1dXpQU3ZhckR2a2ttaGpEMzAwdTlzYkRxUDhIa05yeGo1TlRRYnZxRy9ZdFBES05FQ0J5czFNRnM5QzJvSFFFRzRCTzRJUWhFVXcyazRWOFlGWjYwTFNUQzFJdloxT3k0WHBYVDhVeVJvSk5aejBNUzMwejBrS1oxMmJFcG85LzR1RTFMVHpraVdRL3ZtV1FmTGEvQUphaHN3NWEvZEVmbHhjeGdqSUllY0syYkRyYVpRSEpKbUN0Y2twd2duY2NWYVlHdFMybHZ4T1VRR2dDMHAwTElGa3IxbUJLNHFrYTA0Q3UrNS90MW15M1VmWVc1elZGb3hKYUhld1JRRUFHUW1RK1ZMM1BlSSsrS0RELzAzY05kT3BTb01Nd1JTSHJJNDdFYkV1V2hKb0JoL2E2VGhZZ2tPb2o3OE1JNjZSVXJIejlIMDJkR0xUb1lVRzVXWTdNRTdYczFVTlZ1KzFsNUVyenNHWTV0bUJRWUk0d25Hb0FCQTZZcndoZ2lrNW9PVHRwSGF3b3JqV3Z6OXJPa0NrWlN6dUxnVFFlc1BYOXRmemMzVWtzKzBiS0RJVzdXVFRTU0ZhbG9Jakg4TERlRUx6QThPdGRDVkZLZ3ljYk9uclR3V2NLR3QyV0lSSG0zQVZzcDFMSVJwUVQxTkFCcnZPSDJMTWZET2lWbStUcC85K2VmTHRRZHVScFJ5cmRtNjQwWGtOcGZLVitZUnh6NmM3ei92WFREYmJrZm9xMnpiRXkyTEFTYUxwVkVkbzAydmFkbHpjZjRzTEV4akIyTFJ2bWlyY21sOUNOUGZyYzhtR20rNHJTWFM5MUpxOWdlcUhKVlI1TGJibXVOQWkwbjZmQUN6WmQ4QU5tbnFNS0ZKTU5YQjlDZ0FOQkhIYVN6SlBKT1VhaHZlWjh5aWVXOHlVeW56Z1pLS0h4alNYbUljOUU3MUcwbUpCWGE5dG44RUNLZWxvRllqUG9qVjBnM2pnTmpNc3pGSXVTTnJSbFlDMVFCc2YxNVNZUUdEMG5pLzhlWnFPaWhPQ0ZzRFRCS0ZDQ1oyTm1abXg0aG5mdnkzUVZVSmtublJYTFJFajFBaGFtM09TaXZ6K0pNMnk1Ynozd2R6K3phSWQySm9Rd1dXbW1ieDA4YW8xUkhSa05OSWFyamV5RUtCUVE3ZlB3alZBamJyaHJpa2ozUk5Vc0gxME5CK0xCNGVtTzQxcXVlVzNkMXM0RGlXdW53QUFsZkowczVTakdXallXS2IxU1dvTU1heVZNV2hoVVB4Q0ZGdDFoZnh2bW9NY1BGR1NXTk1FamV4bG1udmxGZzA2VFVBcmRQYlROK0hUYUlBMUFqRUlMYW5iOTZVZmsxdWY3M0swaHBVTTJuaHdWd0cxWkR4YnBNWUNGSXcycVFqblNVRU9vRHo3WUthOE9OaXZsOEtVTlEvYkUxQytqMFp5c25BRGdoV0hTS2JXb3Z2VkQvZ016L3piQnBZZUhYdDFMSjZSek9nM1FncHVjbForZ3FQT2ZGUjJQTGdmMVBac1lNNkdNQ3lDMVErOElTT3JUSEhId2ZBUnpCNG9PNGM1Ulh3QWpxRjd0dU8zNUxqc0g1NmhIN2J6ZFM1V1ZqSllWeEdPQVF3cHZlNkNNejBlQzNsR05LM2dtVWxhSmRKeGdtSU55a1JnSVFydVdGa0JVWk1EcTh4alowVVZaVjRKMUs1UXZZdDNLN0lMV25pUmtvOFlOSXlvU1FqWlYybk1zNW1NMmdqYldKcGVBellBOWdTcElqQnRZR1BzWTdicFl6N0lXZ3drVVF3cHExVXh6TGpoemZpT01DKzN2bnAzcU5xeVR2OHdZRWIyOEN0TjR1SisyL0QxSG9jTlg0MFdBNWl5a0RMNFVnU0xhaXVoaEpxdnlhZStGMDdRRkpqUEh5aEdMcHlnR3o1TWVZOU43MGZyL3ptcXlXek9Ud2RrejVwS1c2MEdZM2NabEw1aW84NS9qSHk4VWQvRk12bkNEKzNYekxUQTV3Q2tMaklMV0FrVERPT1BaNXNMVFN3VU5vRCsvaTIrNzRGbHovaEsvenVreTdEMisvekpweGhqMVMvZXl1MU9BaHJNcGc2dVRkdXdDQ2NJeU1RZ1Y1N0VQR1lpdWFPazEzUFdCbUxvQk1zVUZWWTJac0NBSGhWaVluVGJkMEVxRmRYcWtBczRnbVJ3ZE50aDlzQWdaRVVGbzJQdHFWMW5FU0plVndDb1BUUitFaWh1SGk1ZWU1aHFtSktJVVZGRTRaaVhXZWJGcWN4Z2hjQkpRMHdTRWhWSmJKY2ZyRC8rNHYwQ1VOSURFWU1uVG9zNjQ3TE1lTXJpY0VDUkd3emVZaTNFSFpXcXk2aFhvSFdwN2IrcVArT1g2c2lFQ3UrS3BHdFB4NlhmT3RWZVBmVkgwWnVjamhYdGU5Z1VhWk1sSWJJYlM3T08zbm9rZWZ6YTQvL0VzN0FFWFM3YjBXV2pVQ2FLaXRwMlZ6U2VMWlM1L2VackV1LysxYTg5cDUvSmI5ei9CTlIrUktyUjVmajJhYzlBOTk1eWxmbDM4NzdGNXdseDRuZmRodDBmbFp0bHNNWTIreUx1bG9PelU5ZE5kKzIzOU5yQTlzVk4wRjRpUy9SelRvQXdscWJ1TlNwb3prQWxsVUZuMlZCRTk3aEhtcGh3UFE5YVd3dDdqTnR2bVFaSmszUVo1Q0Fkd3pGbWQwTE0wWTFjakRSb2lJak40ZEd2YldsRHFXNTZjWFAxY1d5d2JQcWNMNC9UNmMrOU54RG1vendmTUxLQ2N2UEVCbldrWmRVZWhsdHdYaTJXVzFUcFFscC9kVEFZKzNWMTJPTGxTQ1VjRlNFV1hNNGZ1L1R6K0lWTzYrUVBPdWc4dFdpR3lQQWxIS1kvTExNWm5TK01pY3NQUktYUGZsTGVQckdKNGk3OVRwUWxkYmtFcG9rSXFqRE9pd1ZQOVlwTXR1QjMzTVRublBzNzhrTFQvODlWTDZFTmJsNHFsVGVBZXJsaVNjOEJ0OTh5aGU1NVdIdndUM3RzZkRiYnFmMnAyRnNEdU5ONjdQQnhXc0JObHFwWmI2QTBZeEtONktBTjdKeTZSRmh4NFQxRU1SMHRlUVYzakM3VFdhMEx5SlprclJ0N2JZNDN0ME9Vb1M5d0hxVENFS0JiNHk0U0lGWkFNQkpxN2dZZ050OGFTckNHTkJRUkd5TFZWNTBReWsyaVVBSEpFZWhsbjV4Sk1sZVZVTHNLTFl0N09FUEQxeFBLMEtsQmlLSmxGcmxBM2pRcHJQQmNoaU9DbTN6ZTR2VWJoeFVLT3hoL2JQSTRFL3R4Umhiem1xa2JPTHhPRm9DTnVOd3pVcDUyQWNlaVd2MlhTdTV6VkY1UnlKV0VoQlNFNlBOWkV0bWN6cjFNcHAxOEs4UC95ZDk2emx2d3JKZGZmZ0RPMkdrUStOTVVNc2VDcFhBL1RtbGxZNjZnN3Q1ajlHN3k1c3ZlRjBvemhkTEk0SDF5RzJHMEtuS1FWUjU0ZkdQNHRlZS9nWDU4QVh2d1ZrOFd2VzJiYXI5R1pwc2hFWTYyaUtCSS9qaVJrdlowQ2tMT3pnMU5VVVV1azEwOUg2cnp3QUFtblRxbUVpa0x3UGFmcnozT2xUbHRGcGp0SlY5M3piQkdodXdlVHkxUnd2MnVwT1VyaEoxR0lIY2JnMVR1U1hnTHAxb1kwby9rSUVyWWs4TDFsbk9yaTFKMmlCTFVReHQ3Y1JvcnpRdEtJUWdUSlp6b2RndjIvYmVCQ0FTN1VSb0RTLzFlU0I2N0tyanVEUmZwYjRxSUNydDhCcWF4RWNnWlNVdkJtVHQvY1h4R3FrenRqVVJBU2FxbUhEQWk4bkhzRzl5QkJlODZ5Rzg4Y0FONk5oY3ZLc0lLRnJ0YjhOeGJ5Mk5uNWx3VHBYekZaNXoxbS96VzgvNEtoNjk3SUhVWFRlTCtnV3hXVGZVa2tYcExiQkFXY3JVQXZBdkQzczc0NGtKWW8wTmV6VjhXV29hS1RCQTVaMVlWWG5zaVkvQTE1LzVCWG5QQlc4elo5bGpvZHR2RSsxUHc5bzhucUFURnp4SnFDUUlXa0dROEs4a3UxMVlEckZ4YkYxMHArdmtUQnFqcWIwZ2g3T0hHT1BBYldPOFh2cTBJZVBUc2xnS29xSHBiTWczZ2hHUm9VSm05VWN0L01IZ2t2QzJEVHZtZGxHeE5UUTJwRElkRXBBYTFOVFV3QjBHMEZhOTZXN2JxaGdnVkFXOUNmbkc5aXZTKytJTFRXaEtaQTByWDhtbXlTTnc0dWdtd1hCT2pOajZuaHRObUNSU0JCYWpjWjNPZjY1ZnFXMFZrRDRqVEVrNitkc1k4ZDdCams1aXg1U1Y4LzcxZk54eTRHYm1XUzZsY3dEcXZNVjRROGtFQ210a0lUVFdTdWxLT1hacUkvNzl3ZzlneTRQZWcrTVdWbmkvWXh1MEdJcXhWdks4aTh4MnhlL2NKbTkrMEQvaXBPWEh3Rk1oZFE4K1JJTXFqaDNoUUpsUXNRWlc2bUJCUE9YVWkzalowejRySDM3SU8vRmI3Z2p4VzdlQ3JDZ1NPM2NsTlp0V24zR1RJcHIveWVLcXZDekwxNWp4aVpWSTg1SENrMXFYWUFLM3pld1dkTWNONmNQV1lBdGRTZUxXREYrVDNWWUxJUUhxNmpoQWtBSHNLK1RhL2g2MHJsaUhBdmt4TU05NXYwTnN0QUZ0K054SWZ3VENxNjdMaUdLWFFGMkpsUVJHdlJ1WTVrTGdsZWgyK0xVZGx3dnFKRWpXRTZacUJJR3ZOQTgvNWdMQi9EekZ4TTVjVFRhd2dhK0xieEJkL1RRUkVuZTQxSDhuK0NzRDZjWm9ZdGZHZVZnYjd3cllpVWxzbndBZStKNEw4TU05MTZDWGRWQzVDaUJUdXlDa1Q0Mkp4ZFFvc1RwWkp4eEQ2NTFjZU5MamNNVnZmOE84N3F5L3hFbkZLdXEyM2F5MjM0VHExaC93MldjOGwwODY0YkVvZkFtYnV0UEUrMjk1ZGdueVlveUlpRWh1TW9UZTExNXlJM2pzU1kvRzE1L3paWG5yZVgrSHFaMTljSDQya3YxeFJScnl1OUZja1RxeHhvSUwwN2pYcWhPeGJtSWx2RG94RkRFd0Vrb1dFRFkrZ00vZCtpV2kxNk5XS2pXZHREakczUW9MSmhVdGpacjJVVVZia0lRVEVUR3pWTE5uTUhkSEFBSmJOb2Z6bXc2NVdSR0J1TG8wSUh5UTg0M3VUMStldkNGcTJIMHBycGxzZ01ZWUZ2VmVwTGNVMXh5OGpqZnV1d25XV1BFTS9iT01TR3dZR1pJUUhuSGNRekJXalZMcDBtb0htekMxSDZ0M1h0eVJtallFbTBtcUhSYzBoSGpneHdJbG12cWRTQWc5K1hJSU83NFV0NHlYdU45N0g0U1AzL3dWeWJNY3BhdnFaZy94SWdDRzJwSlFscXdBTW1PbFl6TTY5WnpvOVBEQ3M1K0w3ejc3cS9yUlI3NlBmM3pVYzNuSm1YL09mM3JnYTZsZW1kc01NYjlUYW1jK3FvL3dFNkVaaXExQVFJd1l6WXlsTlpsVTNrRWcrcHpmK20xKzhlbi9nWlg5akNncWlrYTZwTFlGSmZ5UzFrbURVU3ZPNmZGclQ2UWduUGNUZWo0UkRJZUcweHJEL1hQN3NXTityeUR2Z3FFZlg5dm1penhnc3JzMXJIY0syd2FPc0drdllsTUlSd3dLN3FpR1psdkFYQk5YcS9WeDV2RWpHU3JFVWdWR2tDSFVpTVRHMHdqQjc1Wk5pT1FJSUNRbXhOV3FuWWpHUURNMng3eGJ3QmR1KzNJOTl0QjZNR3pSekZpcUtrOWFjd0pPWEg2QzZPd01yZGo0Z1hHYkVXaHlGUVVOTFZQditQaWJOTlJOeUl4cTl1MWlWUkszb2NEN0VyYTNWR2FXanNwanRqd2FIL2p4eDlETk8rSlJ0VVJKWkJUQzZZZ2ExR1dRNWhHSVVDZ3FyZEN6WFhuVThRK1dWei82MWZqekIxNEM5VjRvb0lIUnlHTFVIOXNNcnE1M2JKNnBBeGhCTWVVMnlJYkNEZVNNZFNmSjVtTWZBODVNaTVYVTdMT2VwNVQ5a3NaTjcwSDJLM25VOFErTHVzdkVBdkF3QnhvOTRNdTNmZ2Y3QnJ0aDdValRxVFh0RWNaQk1RS3hUbDFxRzRBSW04R0lrUndpRkNOZEl6eFk3Y0Q4L0Q1Y2ZMRkpiNGhlY0VDZzdodGVnWUVIYlJhMlRCNUZxaGZDMVV1UVVvR2l6eTMxRS9GeEpsZ2hKU3hBQUZYQjJMaDg0b2JQSXZGc0ZtbmJoODlLdHNqbTR4OE56RTVEYUJ0amRuRUtVdlA3SWt1eFpaRFdubGthb3JSTUJnbGhOS0RGcFJsNjUyaXlFWER0V2o3eHcwL0ZtNy94WnVRbUI4aVkxcC91U3FCbzV4V0YrMUhBR0Jqa0pxZXFGNmNPbFN0UXVpSXVrekhKM21DZHdGUWJhR20wYkFBbmFmalNRaVVnTUxucG92Q08xeHk0QmVpTWhDelNSQTdYOXlXMWlXUmd3R0lCUjQ1dnhFbXJUaFFQaFpIa2c0ZEdsU21BZS8yKzI0ak1CdTgxQ2Voa1lrR2tQdVZiNnZWSTRBdy9vV0pTSUVLRVBueGg4dVoxTndEZ3BHdnJHdzV6R0QxaGUwdi9CZ3lxUHJQUUhSZTVDV01yMk9iZldvc1dVVkNyMndRT2pSSXBTazJCcUs4ZzQxUDQ5dmJ2WTl2MDdaTFpjSXAzRWtna3hjVE9wRTg0WlRNbVpKSys3TWRvakRRR2RzSkJzdVVTU1pwS0dldFFYQVBMSnFzWnFGUEdRK2ZCeFQrV1VEakFXakdIYlpEbmZlVlArRHNmZmE1NGhIemR5cnY0MVlSUVlyRlRiZDdIeVF4SFNSaGprWmtNZWRaRmJqb1NwaFJTbHdRbVdYY1hsOVRHZEpNL1VNTVRJWXZTR01PWi9pSDU5czRyQkNNam9mdzB6VWY2aWNJS25oUXh3TnkwUE9DSSsySFp5Q1NkY3pBaGxoQjBwRklNZzNuK2lSOTlVdER0QmZXYjl2Z2lTd0Uxc2RMNEEwa0FFWFhjT3hmUWdqUkdNU0JOSVo4SEFHelpVdDlybUxOTHdweWVlZVA4alRKZFhTODlHbEVHZDhGS0U4TnNCaEMrS1NVT2dNMGVEU2ZUcE1tbytUekMwWFp5SE5SRCtNQ1ZId0VBOGZRMGxEcGZ4OENnMG9vYnB0YmdzUnNmSWp5MEI5WmtyUjRrelZmWGwwOUdjQnhjRy9qdHVHaGJFbXB0RzBSdkNJQ294QXdCVUIzVUV0bkdvL0hQTjc4WDkvdm5jM0RWM2g4anR4a3E3Nkw0V2xRK0w4M0k0cU10Z1FrVHFNbUFxaVkxcjNYRjdWMTdVSnFrYXR5Y1NDWDFDb1Z6b1ZmZGYxejNlUlJ1RmpidmdpbUx1dTE4QklGQmhFNjJJcFhIazA5L1BJRFFyRHhhYndRQ2xacVpEUHZtOXVHRy9UY0NveU9pM2pkbVZacDMxc09OZGxSdFd6ZlBLd1FPUkRjS1NVTWpmUlYrZDNvcjduQTFXbVFMekdXQWswUFZyV0d4aExBRWpMQkp1SXpPUmxyZzVPcTNqZi9hbVl1RGtlUXdpR2psSVpOTDhhNnJQb2hoVmNHS2xacDhEMlZIdGJYMm92cytGL2xDekI2c2pkcVd4NVdVckJBMVgxaG5UZC9CTEVnMlNwTFFpd0xTbEx0OFBRV3VMSkd0MllUTHE1dHg3aitmanc5ZDlXSGtOcU1SUTY5ZUFtS2pjYXJCTm95ZlUzZCtTRmlRSnMwdEVSYnRkWTJDemlUMHhMaE5lbk1LcGdKQ2tVeUMybnovdFI4akozb0M1eGQvWUVKL2xQakdkc0NGYVo0OGVUTHZlZmc5b1BDd1lnTXFnejBidWtnWThLczNmaDI3RnJiRGRzZEkralFuZHdaMzBqeDF2VWs5MkRxTFNibzJPSkNaeWN6K2N0cjI3UTBSYS9XcjJ3QU1iNThwUDQ0RkJYT3JFS0gwSXJGY29hVjY3ekNOS1ZJaHdLS1FYVE5RZ2dMMUh0SmJ5bXNQWFlOdmJ2czZyREhxdkJPbXFoaFF4Rm80WCtIVXcwN2xnNDkrdU9pQlhiUlowNlcrMXBmTmdyWkFsOGJVdHU0bDdjcEdnZ3VhRFpJaW9lMFVwcUQ4Q0xGd3hSQjJZZzJtVjB4Zzg3OC9BOC8vMEFzd001d0xKdy81U2hRKzJMUjFxRHdZdmJHY29iSHpJdkhUZ0xTNVd0c2h4YUZyMjR0UnVpYzcwVk5wcmNWVjI2L2g1Mjc3b3NqRUtxcExIUkRRekgyeUFRRVlhOEJEMDNqMkdjOWd6M2JnblFxTVNSWFdxTTBvUUQ3eXcwOENJK1BoekxHNkZya1JJdEhHVzJ6MzFYbUljUTRkZ2N3QVhRRWNhRE1SM2VsdUwyL1pmMk9NRjlkaWRWRjJLZ0RrMXkvY1lCY3FqeXkwNkdYSEJQUEdhMHhNYU96Y2VqL0lIU2MxelI0YU96RXV2QUZGbG96aGRkLzZ4L1Q5clJMeFlISUdoZzN5VitlL1hEcERnTjVUYWhzdnF0czdrdUtwakxRdTIyeUJzRmEvU1UzWGNxa0Y2allpRUZaUHZNQVllRitJNUJuTXhpUHhwbHZlTFdmKzQ5bjQ1aTNmUUdaekdsaFVyaEpTSlpGRjBhU280OTFFWUN1a05tR0luM0RWcjBpVkdNQWlnSWF5U2dBdisreXI0RWRDTWdwTm94WVdIVlpDaFNHbzgzTmNsNjNEMDg5OG9paEFtMlZHWWdXOXhDNGplWlpoNTZGZC9QeU5YNllzbXpLK3FzTG5CWTIzV01VbU5WdFRXbWdjWVRJd0p6a0VSc2pRZ0F0bUFkOERBR3k1NkNka1JHOEo4WVRWYzZ1L2IyZktXeVdYUUNGbUFMTG9CYnRvNHdHbzZaaDJEcHpHTGR1RTRocXZPWm9OVGl0Z2NoVStkZE1YZWZsdDM1SE1abEQ2c01ralZJeXhySHlGazllY0lFODU0VW5RM2R0aGJCNmI2ZGVzZStJZG84aE5pam5GUHRQMklIREgwdExrTzlSaHFqdUFJbkdFUXNCb01NT01oMnJGYk8wUnVMbDdTQjd3M2tmeUR6LzZFdXllMjRjOHl5RktWbHBCWXllVG1xOUVLTFlKdWJlZ3lKMzJmQTI4T0xYU2FyWVRWelowUW5UZVNaNTErSm5ydjh6UDN2d3BzVXZYd3ZzS3RYZmFCa2hnSG1CTUJ0Mi9HNzk5K3BOa3Nqc083eXN4MGVBTk9XSUliZHNBZk9ISFgrYSthaTlOcDRlbVJWbmM2V0Y5RzZCSGZxZzJhRlAvR2M5UXR0Q0xpY201cUpRTzVzYmhsd0FBYjk2eWFMZTNaNFA0NEdaNzAwMDNGVGlrWDRsdWRzQjVWeUloelZod3pRWlliZEhyZ2Fad0tQMmJvQ0RCWVVBOHYyUzBJMy8xcGRlbU8yeHM4eUQ0SlZJZGZNVzVMOEVLWFFZLzZIdEpOdHppUStVQlV1djJFY0ZlRkxUNzVBYzF6UHBkU1M4dkVrUkphcmVOTTRsZ1JTMldYRlhRakV4aXVHNGRYbmZOMjNIbW04L0c2Ny8yWmc1Rm1XYzVBSVRUblJianJGWjJpS3FnRGZkNk1Vemlpa3hyNk9HL1NoVWpCZ3ZsZ3J6b295OFFybGdlRzVraE1Zck5PS01sYlVUQVlsNVdkbGJMYzgvNWZaSVVhNnlvQmh0VFlvYWZpUzFKMzNINSs0MU1UQmlVbnJYY2xSZ0lDSGVSaEVrZDY2L2o3VUVZQlZNTkF1bkdDY2drTjRlcU9idkgvUUFBc0dyeHJDK2Vwb2hPZitQQzV6QndnSTIyY1MrVzhJUU1oeVlkUEMxbUxlMmlEVmEzODJJTFhsRk9LK0hMRW1icGFuNWk2K2Y1alZzdkYyc3NYYVE0RWlLdFdLbWN3OFpsNi9VeEp6MGNtTnN2RnAzMmdqYi9wdTlMQ2JPMUxWb3ZTKzBDTjgvWHFoc05kV01RczcrNUtMTW52U25lcjZwUzZHSFhIWTZkU3hVditzcWY0TXczM1Fmdi90NmxnREhJYkM2a2w4cTdaQzAwUjkzZFNkOHZJb0lndGFKbWJUOTZxaWdWeGhnODVkTGZ4L1hEVzJGR2xrS2RKMnFia2MzbUJ3UmVJYllMdjN1WHZQanNGMkgxMkVweDZnRXhySU10SUp4NldHUHh2VnQvd0cvdC9DWmxZaG04eHJZS2d0WUdsR1krNjFtVVpBNkYxNmdJU2lVeVlaQ0FwSFNzeU0zVmJRdTM3UGtSRElBdCtBbFZjUUJ3V2NqKzYrM1k5eVU1TU54cmVqWXpJS1ZqZ0o0QUZjSUpPRUgwdHRwSXRQSEFwTnFZVm0zUjlBT0FDQXdobU9qSmE3LzgrdmplUUNiVjFXb2lxVmhCY3RpUTFXdGFkbDQ2WHI3bXZXZ2FtWmpNQVRRcXQra1l4ZFpDTlJPYVBPeWt6dEp6WUdQakpwZ1lDQzNGYXducGRKRWZzVW11TlR2TjB6LytESnozbHZ2TFo2LzdENGpZa0dJRmdYTVYxUHRZVDVnS1IrNGtmb0U0VXhvSHJBQzlkN0Jpa0ptTUwvblV4ZmpvVFIrQlBleEllSlFDSzdJb0poN21RZUJJQTBzL2Y0Z25MenRkbjN2dlo5TnBSU3NaRVZJTzBnbFQ5TEhjNG8xZmVwTlVvN0ZpUWlDZ1NsMCtTcmJxV1poNDFiQlpOWVZCUVZRS1ZCVDBRaUsxV09Pc2VuQ3YreWdBd1NzV241SjBad0FDaE1MTVhvdUQ5b0QvbHVRQ2dDb1ppSzVoVTlPZ3FEZW9Sb0MxMjBra2FSRzhwWVljRGJ1VUVJRnpGZTNrYW56MHVrL2dVOWY4aDJRMmgxUEhLSWNBaEJocjVaMWN1K3MySUJzanRRN0hKRUdaSkJzV0VhS3BzVUJTQzR0TUZ6VGh4TnBXd21LbnBiWjFBdHBxSUxieFVqK2xVaFVEbU40RTdKSEh5R1hWOWZLUTl6NEdaNy9wL3ZxKzcxMktnU3ZReVhKbTFrSzlZK1c5T0I4T1JJZ0NtN0VzZzZvQkhLb01YQis5WkRialhOWG4wejcwQjNqTnQvK09kdjFHZUZjQk5wa01DRHJJbU9EK0J2dFhqQmhnL3pUZStPaS9rL0h1Q0FCcEg5cElZd3dxZGV4YUs5ZnN2cDRmdlA0VGtLVnI0U3ZYS1BONkF6TXNYbkk2a3dCcXo2dW5vSW9hYU5RRUhyQkR5LzJsNTFVelh3QkFYTHQ0MTkwVkFJRnpnMmZhdVhtNHhRd1Z0RWFoRUhTTndDTFlnQldTbEl0aU9JRXZxck5tZ1pza2dxU09VN2s3UmVpSmJIUVNOc3VDNWRMNGljSmc4L0RnM01IQTl2ZEdxTDZtUytxRGFob1N1aldPUUpMWERFZ3psamloNlpTbTJ2bG9LY2FXa202VlFyYitYZlRDd0Z5S2daTDBaVWt6dW96MmlFMzhabm1kUE9XVHo4SUpyenVWTC9yb3krU3JOMzliakdUSWJjN00ybENQSDRkaGpJR3FyK3RqTW1QWnlYSllzZnpvTlovQnZkOThqcnpueCsrRFdYZWsrTXFqOFVxUkNIclRuSldyeURwZHVCM2I1SVgzZUNIT1BmcmVVbnBIRXgwT2c5ajNIZ0NwQWhqOHphZitqbVhIaFg3UXlWSko5bDN6ZDV6M05NOUF6UU5DZ24vZ0tNZ0Zac3hTbEpRUmE3bkQzZVJ1blA3YVhhbGZBSGNXaVZFTk03dHMxNmVMTThiM215UEdWN0JmT2VRbVExZUFQZ1FWQVNmaDNZYU44Vi9qVzFJaVFBQkpIVGFXdXJKS2pJaTZnc3ZNbU54NzQxbEFuSndvMXFBTVFaaHQwenRaaUJmWUhMRlJVbU5EU1FRR0k5QWFyQWNBS1FLRGE2S2hIbm1ITzBqcUJsL3R6NnM1NWZoN2VrNFJHbmkyN1NOSml3R29lZ0pPek1ReXlPUUtiQzM2ZVAyMS84dzNmUGVkT0dYcUtIbmNpUS9uS1VlZGd2dHR1QmZIT3hPd1dTNVdESTJ4VUlDVks3QnpkcmQ4NG9lZjRiOWQ5WDU4KytCVmdxa2x5Rlp1RkZjVlJOWUNRTExCMHYxVGFDU0htejJJRTNxYmVQR0QvMWljZDdSTlRWQ1VZSXFLaW83TjVlb2QxL0tEVjM5SXpCSHJ4RmNsYTN2ZEJJWWZyT2V1dGNIdllCZDZoaXJBU2lETExjVUlJUER3TUx4NStDRUF4SDJSNGJJNm8rQS9BU0JBZkJCMjVpSWM2dTR1UCswMzZkTjhTcXZwV2NHQ0E0WUM1Q1F5aER5N3RzR2ZDR21Rb2JSVG0rZGlBakEwOEU5Y0dNaHhTMC9HZUhjY3Fsb2YzeERPSS9Td01QTEZXNzlFbFQ0enNlS0NHNDFHK2lBcUNta25MWVJlVERYRkVpY3FwaUhYc1JaaHkzbHFicjFXUDhrZlNHY0ZwNGZyRWdLbVpZOURxcVdvQUFKVkY3S2hPMTNZdGVQaXZlTlZnKzI4NnNxL0ZmekFZRVYzRlRxa2pIYkhjZGpvV3VubFk5eGZITVR1aFIyWUtTdk05L2NJcHFiRXJ0OEFxb2ZUSVdCak1YTUNYLzJOUWVJSHg3K1EzblRCRHozLy9aZ2FYWUxLVjZIMHRkRjFWQU9CRHdOKzJjY3ZZVFZsNjBxZHV1Rmw2bFZkMS9aSUUyWk5HVEN4eFQ0cUJZbzRwK01aNEtHY01OYnNHTHJzeHd1WFZnQndMaFNYM1Jsc2R3WEFKajNyeXJuMytPUEdueXJqWWxnQ0dCRWdsL0NGQXdFNmxzamlnTk95Z1dHUkc0a1ViaTJCSXRLVzFtU2lDOU04OTlpN3d3QW8xV3VXNVhWaEZpTTNkV0QvSHNERy9nOGVnRTA3a2xKTEp4T1Jrd3FYMnJIaWhKeW04akpncDUwUVJHMTV4bXplb3lic0NGRnBvanZKMFpKMEwyZ2VCSkFpSFdKaTRwS0tjd1ZBd1BRbVlNYW1vS3JjN3l1Qjk0QTdoSnVtOXpNVXp4aEJOeWU2SFdUTGpqTHFQWHhWeHVabWtyelNlcExyelJJdGw2eVRTM1hkemZxNkM5OGhKNjQ5bms0cjVEWm5nQ2dUWFNPZWpybnR5T2V1K2h3L2VjT25ZSS9hS0w1d1NkYlYrcllHZWxOekhiNVhrYUtHVFdTa0pERnBhWHBHcUtSMHJUVzNEYjVlM2pCOWRWejlWbEM1dWU2U0ZjVVdlQkN5Y1BuQkw1bmIrdDh6ZVdhaEpLd0FJekU5MjZsZzJMYTkwTmlBUUdwZ2ZZZm5nSlM0cWdxZ0JJNWNkMHk0VFZrOGhDekw0S2o0N3I3cmdlNms4YzQxa2k1VW04V2E1YmJLYk5tYXRZUnNBU29WdHBvVzY5Z1NuZlhGZGp3THFEc2dKT01iN2NHeUNZR0ZvRzBpd0tVR1NYeE9xWEJGUmEyOENBd2s2OERrWTdBalUyTEhsNG9aWFFLeFhTT0FjVVZCOVk1TkM3ZGFnVFlObVZMcGdSTmtlUS9WOWRmcC8zM0F4Znk5czUrR3loVmlUQjZ5R2tMeURtRkNyWlNSREdWVjhBOC8vQ2VRNVZQQ0twbFJjU1hxVG1mU2FJbDJROUJZS1FOR29WQ0ZOWmVwVEtpQVpFYnNySWU1cnZ4bkFJeCt4VjFlUC9FSm5Bc0xRTE50L2JlWXdnTlpuUGh4RS9oQkZXQ29ncXBldExhbVlzMVpzQTJTOERvQjRIMkpiamJPY3pmZEp3eEVqQWdoR3F2WURBem0rM1A4M3E0cmlkNG9XTVhFdFdUajFZVTRhWElTMkNMaTJ2SEpaaUxiNGZUVzVtaUJyTDZIT2plMGtiVGhQcHRGU1NJeFpRS2xKSTMyZlNmYkYzSFJZaUYzRTFIMDRYL2VpMUtGcVM3U3RBamVaSU1tNnFzMkE4SW16MGQ2NG02K0hpLytyUmZ5eng3K2NxbDhDV3V6T0VjaG9wTk9KdkRxWVkyUmwzejBMM0J0LzBiWThTbG81UkEzVWtKaFErdlVhMWRyczJUSGg1bXNRQXc5TVNxUXNReHdWSXhiaTV2Nk8wWXUyLzF4RUlMTDd1eDgvTmNBdkN4SXdlNi83L2tJYmgzY2pwNDFVSHBrTmtoQnhKTER2a3BkLzRyMklrWHBGNW44eHQ2aXdCSndReTYxRTdKbWZKWFVBQlJBakpGVUFYREQzbHN3VUcrUVpRajlMT3FGandCTGNVOXQydmY2S0NHU3ZWSlRSNjBkM0J4QjJoS0FiTFZOYTk4REVyRGI5OWNnTXRGTDdRNVZwdlY2cWI4azBkanBUZUZIQ0JnUFdFK0lUNmRkc240dUFUdmNuMFRiTlhxL1JEN1NRN1gxZXYvc1U1NkYxejcrTmNiNVNveGtFTVJrQTQzMmlnZ3I1OURKY243aFIxL0FXNzcrQnJIckRqZStxQklyMEc3dUZ1Y0RyUG0vTkpna0FMd1BkZENGRWs2QjVYbVlyaHlLRHNEcmkzY2VBbWJ3eW5OUzN2SFBDRUNBZUNYc0lXREcvSGp1WFNZUkdrSmdpVVYwTHNJQWl0WmkxcEtRRVNBeGxKT2VBMkRGRUhOenVQK0crMkNzTnlwZXZjVEcrbUYyby8zM3Jkc3ZoM09IbUlrTis3QTU1aUJLdVpTM0tZekhpS1RuSTFqcW5kbzhIcWdFcWNGVWU1SUpFQzJRdFZWNytyZGxCallTVlpxbENlTks0R3ZGb0xYbFFMQ3g1Mm9LcS83Z1ZnK2NwTWJaUkdiaWtmVWlsTHpYaytyRzYvQ2NvNThxYjMvcW0wTk5zUmhZWXhLZlRHTXNEVXlNZUJoc1A3UkxudmkrWjdOYU01V1dwK1hNMWRxcVNWR3I5MTFiaXlITXRhT2c5SUtKREdZaWg1UktNNUZiYzlPd1gzMXU3enNoQUM2NTdDNXR2M1Q5WndBRUxnbFNNUHZXbmpkeDYyQWZ4b3hGaFJCbW1iU0JLWWNBQzBxNHVHTnJlMFVDUjFZVHdiWDBDVnR0T0pBalZ4d2Vsb2FwcnBJU2o1Y1RBTmh6WUk4aXl4dGp1NTZBOXFRSkY0V2dDTlFOd1RYQ3BabGdxUmVmU0FSckM3RHA4VFExRVZnMFRERzFSVkdVU1A2RXpZYjBJWW1nVFJSR1E0eEhxaVFDY3ZIQzFoTFZOSFV1algwZE15VU5vSVF4VmtSeVZEZGN6OTg3OWRsODZ6UGZKazRkaFlBMU51YmJKdjVmNnhRd1l3eCsrNTNQd3Y3T1BHeHZDVlVqMHB1TkxZc2RNWWI3cTlWeHN1MUJlQWpLS0dCV2RjUFlEZFZZQ0w0Ly8xNFV4VzM0WUREai9qT0kvZWNBQklndG04M0NYdXd4VjgrL3pSZ1JaRlI0QWhNVzhVQ2IwRGwwSVNWRnRpVUk2M210bjFPQlV4WFJIdTl6ek5rQVFCTkJKeEJBRlZZc1BZQXZiNzFDTURKTzlUNjUvdEtrVzBVd0tWQlhabm1rREcwUnBWaXhzTWFHNTFMSlpocFArL0NYaUlPMjMxRnZIQ1JxSXQ0VHRDV0JXeXFwU1ZGdkFKMDBnYkQxZDNwT0drMjh5SmFOQitNa0NWbm5XcXJBSzdPc0ErMFBZVysrblgveGdGZmhINS95Wm9TWU04WGFMSjRDRlQ3WGhQT3h4YWxEWml5ZTljNW44ejkyWHdhN2RKMzRxb2lBandCZm5FaVNQTnltUTBVVUhyV0pVa1RxWlRLampCbEY0WW5Kek9pUCsvM3FQMmIrSG9UZ29wK3NldE4xMXpSTSs3cG9pNEtRY2RuMWQvMWpSNStxSjQrdHgzNGxPbEVLN25lQ1RJQUJnREVDWFNKbVJFU1p3eEJCcWFlZW9DbzZsY0dwYTA0TUhvSXhDR1hCUVE5WmE5a2Y5bkh6dm1zTWx1ZHBGbHFlYTV3Z2pkSkhBZERUR0F1eE9XQUJQK3pUeng4S21uQjBXY2h6MTZoaVRZeGZKaXJHUzdEYkpJSWg2ZHdrT1dPYUlpSXJneVJWZ1NiSkltMkd4WVI0ZURMOUxZSjAzT3NpVlp4QUdNN1NhWWhmaVVpTnh3YlpQSWZiczVPcmlpVjQvN00valBOT09sOHFWOEprbHFKR0ZDRjZGTWtCRVpKZUhUcFpCLy9uMGorUmY3bnlQY2lPT2g2dUdBQmlHM29sakVHaXdkZ0VGSUxFaitJMDNwOEg0VFJJUHlHeElndG4zRm80NlNESDlZTi93ZHpjOWRnUzQyYi94ZlZmU2NBd1N4ZkJ6QUNIc20vT3ZzWVdhdEFURHcvQkVrdDBwQ25iblBhb095bWtRLzVxVDQ1Qi9Sb0RMUFJ4Mm9wVHNYVEpjb1F1bUdIUjQ1b1JBSDZ3ODhlWWRoVWw3eUVTbm90M1kxU0psZ2JXWmpCNUYxb080US9zaHIvNUZvNGRVTjVuOUc2NDE4ako0STZkMFA1QlpIa2U4Z3BjU3pVdkNyZWhrVVFCWDlFbVNqdS9CYmFrT3BONnJRdkQ0Nkl0R210UzUyVHRrZGZmbFNSeVFtajQ0cnEzaTVJMjc1RE93ZDE2RXg2KzlMN21pai81SnM0NzZYeHh2b0xOT2hCWUNmVjRKcHhNRlFxWFVER0E3K0pQdjFiZWVObnJtQjE1bkxpaWlCdUJXRFNPT29jempyMGRRNC9qcUpNZHFpaGtsblZnUnF3UkIyTFNHbnZEY0NiLzVMNVgvN1RTRC9ocEpDQVFrbFY1c1JuSUpXL1BUdW45UHM1ZWVTTDJGNHJNQ0paYVlKY0xVWTdTQS9OQ2pKbGdIMG9FWTVBOEFoQ0dPWFIyaGljZWU2U001QjB0WFNGNTFnblJ1VmorYUdGeDdiYXJVRldITkplVnR0SmhqVkdoaUJFcnlDdzluZmk1V1dCaEFNd1BzR25GaVhLM3crNkRwOTc5OFhMaWhydnh1R1dIQTRCODVyb3Y4RTgvY1RHdjJuYWx3YXJEWUxzalZLM0FsRVFoY1hjM0VtNnhUUWxwMldWQTQ4Q2c5blhxQjFOWXNoYW5TZnJGVjlkcU4zNUxPNFNaeklBZ1RjVm1PWlZlL0s3dHNyU2N3bDg5N0EzeW5Qdi9MZ1NReWp2bWtvZE9YNkdKUW9DNk1WRFNlUFhJYlFldi9zSWI4YXJQL0RteUk0K2tLOHZGZG1ndHFhVTFnaFF4U3ZmRDVoQWFTb3g2RU9nSVpHVWVHc3ZuVUdOTkx0K2RmKzF3T055R2kzNDY2UWY4dEFBRWlGZGVZaUVvT2xkTS94RVBILytrTHMvSUJUV1l5SWdaVHd4VWtGbGdsb0tjUUNZdFlTRzFDVWdvNEExTzJYUjYyUEppR2t2WGlNQ0YxYnp0MEZhZzF6WDBqa0tLdFIyb0pkVlg5SE1IZ2RsU29PQzkxcHlCZTJ3NkZSZWU4a2ljY3RRWm5PcU9KekJJNlVzWUVUemsrUE54enRIM2t6ZDg2WTE4L1dWdnhoNjVEVmk1a25aa1ZPZ2MxR3ZrTnRrQXFoMkNFMm0zM1kyUHhiOFhlZE9vSGZGQWttdFRUWlFjdHVDd0pHQ21tSEswK1lLNHlmSmNOQlA0L2J0Z0I4VHZuUG9VL09rRkwrUGh5OWFGQnFNaWt0c3M1Q1VhUTlYWVRDa3dUNkpVN2RqTS9NV24vZ1ovL3JsTFlBOC9Fcjd5eWVSb29rSmtNRDhhYWljMUpJK21TYkp4VTdvVmdWS0Jpb29OUFpGTVJFcDZMczh5WERGM1hmbkpuYS9EeFRDNDVEOTNQTnFYL05jdmFWMlh3dUlpZUh2aFlSL1VSNis4Q0xPdUFveEY0WVRiSzZsYitob0FLelBXQkcxZE4wV0JGZURXN2JqMkpkL2hDZXVQcC9OZU1wdWNKUVBuSzJRMjEvdTg1ZUh5N1prZkNpYkd4UTJHaXJsRGdvRml6RTd3Zm12dm9mYys5aHo3eUpQUDU5MDJuTHpvUGxKaUt5RElqQlVJNkx4SFprTzBjOWZNYm4zamw5K0tkMS8xQWJPajNDbFl0Z3htZER4Z3pMdlF1aXRrRnJiU2ttc2dOdVNzU2FvejJvL3QxellxTFhXUFlzT0oxaEttTmpxZ2dMV1dFQU5QRHh6YUQvU0pSeDV6QVY3K2tKZmlIdXRQSndCVTZveU5CdzFGZ2R6WUR3b2hIYXhrZ0FGZS92NC9rNys4N0crUkhYTWNYUkZQb0UvZjNNUjNJOFVUNHBtdExLWW04em5sLzNrQ0pZZ0ZMNWpLWVRhTmhHTzRKcXczQjZyTS8rdjJoK09hdVUvalF0aTd5bnI1U2RmUEJzQ0xZZkJLRXV1WEg1WTlZOFVQOWFUUkpkaGZnVjB4M0ZjSjltbUlGVHRQakJoZ0tvdTdUZ0VJUlVTb0RsUDdpV3RmOWkyc25WcWhIZ29MSTZxQU1TRXlRQVUydkhRVGQ4a09vSmcweTN2TDlmeE45OFpEVG42STNPZVlzN0JwNWNaYWlYbnZSYUVLaGJGWkZ1SXBHZ0liTFFPWEJJM3pucmtOaHhEdW50OHJIL3oyQitXZnYvMSsvR2oreDhSRUpwaFlRV3V0Q0VDdkxnWnQ3bWpLcEUzVjdLbVdHbTdSTVd5eWNXcVFTUEJtNDl1TnlRQVJlRmNRYy9QQTNCQWplUmVQMm5RQm5uZi81K1BzSSs4aEFGQTZwOVlZc1RHaGoweE1GYUhSYzNQT281UGxtS3VHZU42L1BFL2VmZFc3TkR2cWVPUExtS1NYa21xREhkcWN3eEtlYkhtOGJETURVcGRnbEJRTW9vZzljUlNtWXdKVnZUVEwrTUU5Vy9peFhSY2xBZld6UU9wbkF5QUFiQTRJeng2NDZqbll2T2F0bXFIaVFETm1GR3d0Z1FVRU5sOFpxSm9sa1FZQllLMkZuOTNQQjQrZHpVKy81TVBHTzZjbU02bFhWQXdUV1Y1KysxWHlyTGM4bmVlY2RwNCs4dVJIMjFNUFB4N3JKbFlGRVlsUW5FTlNJVERHNXBRb2RWUEtaY2g4Uzl4SGJZS0ZUb3crOUdQSWJTWUFaTUVQOEtVZlg0YjNmMzhMUG43VDU3SGdGNEFSSVpZc0UrUjVPT09PRUZXUGNPcWVCRE9pWGJKV3g1c2pFbjBEM1BBcUF4akNpSUhBd29zSFN3L016d0w5QlpncXczbnI3NE1IbmZnQVhuakdvM0hreW8wQUFPY3JVb3drcVpjMkZFRkJ3R0RJbW5aTzhpeVRiZnR1NStQLzhZbTRmUG9IWXRkdHBDOGRXanVsRnBzQWtyUnJId2JVOExXK05YUklTRFRvZTJEZ2dTTkhZRlowQkE0cUs2emdtN056L3MyN1RnUUhPMXNHeFU5OS9ld0FCSUJMTjF0Y3RBWDVrdy83ckgvMGl2TjFuM01ReWVBSjNGcUdzcnkwNHlZellnUUFSYkpPRHJmemRqNzd1Q2ZqN2IvekpwU3VaQ2ZyMUdNZ0tVcmh6SEFhTUFiTGVrdlNHRm02VW1DQ0hyRml4WWlFUUp5cWlBR0ZZaHBLQXdnOXQrTzVJNGpsaHdqVFl4Q0M4b1JLRnB0RUFzRE82VjM0MkRXZnd1VTNYc0hQYmY4eTloYXpJQmFBM0FwR2x3QzlMaUFHWWpKYWE4TzcwcEZWb28xTnFDbWxURVhwd3BkV1Fnd1dCUE96QU1jNWJuTjV3T3I3NGU1SG40YUxUbjhjamwyekNYSDBVamxIWTBSU0Q4RlFaSk1DSXBIdFp6aU0yMEpvclpXUFh2bHBQTzlkeitlT2ZFYXlsV3ZoaGdWZ0plaDl4a1l1d2M1TWRoMGFRcCtoazJxZ1c1b09GSUlRd2x6d3hNQUxsdVdRSTBjaEZZa2x4bU9vbWI1dCsxUHh3K24zL3F5cU4xMy9QUUJlRElPL2dDNWQwOXN3LzR4VlYxZW5UeTJSWGFvY01SYlREdGcyQkd6bytBWUJNU21BRlRHOUR2VFc3ZnpRMDk2RngvM1dJMUU1eDh4bUp1NWtHRWFDTDNRK2s5SlhZaVYwZjdkaWhOR09JVVFCRmFHSUJ1NHN0UHRGNEh5ekxJUDNuaEFSYXd4VWxaRFFlRVpFVkZVbGhKNWpMWTk2UUVJVUlZSUFCd2FIWk52K2JmclpHejRqdCsvWmllOGV1Z0hYSGZxUmxFb1djSUQyQVJ0VHFDQkI0b1lqTFlDcUVuZ3JrRkYyVEM3V095NGRXWVZ6RHI4M0RwdGNoUWVjZUFGT1gzTWNWaTlaVTArcFY0VlNhWTB4RUtOUUZSTTZzZFlGeGtJS0dRNFdwQ3J5TEVQZlZmeXpELzA1WHYrbE53RHJWNGp0VHNCWFJiSlJvMnBOcXgzOG91RDBSRWxOQkJzdlJaUVNGMmtsU01KNUR4UU95RVhNOFJQQnpiSjBabW1leWIvdjMrSSt0UDBpbklNc0poejhWTlRMenc5QUFQRkwzY2laazV2THA2KzcxRS9sRHJOcWtSdkIvb0xZVlFJMjlxekxGSmd3SXFOZDhLYXR2UExQdm9aVGp6d05wWFBvWkJrQWlpcVpEckQyVVZTSk1UQ2txRkJqQSsxd2dnUFNLYktFcXRLSVFXYXorbjZtRitZd05SYk9SZmErRW9pbE5TYnh1VFRCRWpJMTZ4YUZRcWpwRGF1VkJSVmRUMmdGWUdiaElPWUcwL2pobmh2dzR3TzN5UHpDSG5wWGtTcWhtRTRoTmplRVFrWW5sdUdJbFp0dzF1cVRaS296em01dkZGTWpTOXE2R3Q0NWVLRllzUXc1bzdGWm1ub1lZMG5RSk1rZGFrVUE1MHQwOHREaC9xcy8vZ2IvNUFNdjViZjJmMWZNa1J1SkNxTE9JZG9qeVVoRXRCT1NrOVFBMGtjQm16SzdmYlJPczVoV051dUFJUVZPYVk0YkRjbW1wZmV5cG12c04yYTNqcno1NWpObWlKbUlvcDlKOWFicnZ3OUFvQWFoZmRDcU4rRnA2NTdyaDc1Q0pUbEF5bzRCY0VDRnVTVjhKZExKd0JIbEVmMUpmT2RWWDhDcWlXWDB6b2ZEQ28yQmliT3N6YmpxeFUrMU5GNjlDZlZDaWp5clN6UUpBTnNQYk9kbnIvbUsrZmF0Vi9BalAveW9uSHY0Yi9IbGozMkZuTDdoRkFCZzZhdWt1aE5uQm9EUlhFeEI3UEI0N0hwRk9vcW5oNEV3eTdKVXNySEk0R3VOUWU3aTkvYkYwbGVpU2dSblFtQ3RqWlJ4WTZwcUdGU2RXME1JaFJUUDBFTVJBQTcwRCtML2Z1UXYrUS9mZUlkdytSaXp5V1hpcW1GcVJtNmlEMVRmRDlvZ0MrUnlqSGd3eFhrREtJMEpIZEVNZ0JsSERGVlFlTWo2SHV5Nm5yRHZ5ZVdaeWkwRGl3L3VlcWkvY2U0ei8xM1ZtNjZmRDRDQTRHSllYSUlSKzd2clA0c0hycmkzN25HZVZxeDRFTnNMY01ZRG1jS0tGVDg4Z1BPWDNadWYvK3RQaW5NVk01TzNRUWVoeGtSNVEwSWxWZ3dLVlpGWmkzWlhnVU1MaDNqOTdodnhpYXMveTh0ditqNHUzL1ZkOUxVdjZBa3h0ZFJnK2lBN2ZjRWZudnQ4K2VNTFhveGxveE1Bd01KWHlJd1ZRV2hmM0hSZENWZGdJY0lYUjZrckREa1NKckd6bnRTWXU0ZmFPSlBZYWhSSjI0ZUhURHBQTi8yZXVvNGx5bEZxQmhFQVJNTUxJRkVycUhySnNod0NjS0hvNHoyWHZSdC8rY1hYWUh1eG0zYkRSb0VIZlZWSzNWbFZZMXBOMmdicDZJYjBGY0h6RlhncVNBTWFvUEpBTnlPNlZzUVk4bUFKOUQxUWVNanFycGlOUGJKUVlOSTZ6R3VPZjkzK1BQMys5SnVUQVBwNUFQVFRFdEUvNlFwc2tjRmM5eVBiTDZ5V2RhN2dxV1BydUY4OTg4emk4QjV4Y3o4a0tveGFZTUZqOWFhVllidUxpQWRvb1NGZFY4Sko1MVJLcFJXRVJMZlRDZDhSQ25aazI5NnQrTncxWDhaM2J2cTJmdXFXejh2dXdReVFsY0NTU2VDd2NiRjJLVUFZZFo1MjlUcVVyc1RmZlAzditONXZmNEF2dU8vdm1XZWQrd3dzRzVzS25KcXJRQkVSTVpCd2dGNXpWRkJNWm1yK0RnVnJpYisxWWt3R20zeWFWR1hHOUZjQ3NhbXB4T0FIMENnbE5HeU13WWVXb0l0d0VZcng2dFhUbzV0MVlJM2hYREhIOTM5akMvNytNMitRNitkdUF0YXVnSjA2MHZqQk1IQlhrcEpwYWU0c2s1TXRLSW1iVEdFMWdScWlWRUV2aDR6bWdvNGg5aGJBckF0OXZTY3ptaU5HZ0VMQm5pMGxNMTErWWQvYitQM3BOMGZLNWVjQ1h6MjZuL3VLTzZGNzFOZ0QrSXkxbjZ1T0hnUDNxMEZIUkNxU3Q4Nko5UUxkdXB2dmZmWTc4S1NIUFZHcXlzSG1tVkpEbWFFbjJjbnlSY0pveC9RZTNMWm5xOWx5MVVmeGc5dXUwbS9jL2kzeG1RcTZRaXhkQm5TNnlKQ0I2cUdoa1Z1c0YyaDJ2TTF6K0dKQTdObUxJMFlPTjA4LzY0bDgycjJlZ2sycmppQ2kySERlQlN5S3dJaEpoSjVJcXZhcHRlU2QxVyt5endCUUF5c05RNGEzQnRaSU5LckNDQk5oYlpTRnoxSFZaTThpRDNOQUFOaDVjRGZlL1kzMzRWM2ZmcDljZCtnbXhjcEpzU01Ub3FVamxRS2JtbXRFQzZJaHRoT0ZFak5wSkpXd2hzUmRTbkJBNWozeWxUMll5UzZZQzkyT1BuVDNNRVJIUmczc3llTmkxRURoSzZ6S2MzeDAzL2VtUHJqOS9nY3VQbWVBU3k3N2J6a2RkN3grTVFBRUJKdVJZd3ZLN3FrVHp5eC9aLzA3dWFMcnNOOFp5YXlZaFFyWVg4Si81eVo4NWRWZnhYMVBPeHVscTZSbmN3M3RQOEkxMTUrVDIzYmN5cy9jOGxWOCs1cXY0YkxicjhDQjZxQWc4OERFbUdKOHlwZzhIRy9xNFVodFFpM1JqREsxb2QyU0xXSU1UR2JwNTJlQlE0ZWtoekUrN01oejhKZ3pIeTMzUGU3ZU9Ielpobm91bkhkeHBRS0ZZWTJSUlJDTWxFNzRqVEdVQUJHRWM2WkNTekVqUWVBQjRRak0wQXpTeExlMit6Y0JvVEVrNG1MT1YwTjg3K2J2NG4zZmVCLy8vY3JQWWIvdUU2eGFKblowVkxYMFlNVW05Y3RLRXNxQytzU0ExbFpweDN4alpyTVlFUTRjVVNyR0Q1K0NYZG9WTVlMK2JYTW9iMXRnNkxZQTJqTW5JVlpFS3U5MFZTZkR0Nlp2OEcrNjViNHcyQnM2MHYzODRHc0crNHU2TGo0bnd5V1hPWFBPMVBPekp4MytocXFuRldhUkdXdmc1K1p4K0MwOXVlYjEzOEY0YjZ4V0ZGZmQ5Q081OXJZZjRlUGYvQlIrZU9BR3ViWi9MVENSRVYwTFRDMkhkRE5ZYjBCNjhTR0pzMUdNZFoxWDBqV1JTRzJjaGFhd0dncHhvT25rOEhDQy9mdUJ2c2RrWndudWYvZzljT0daajhWWlI1NkZZMVp0YWsrc0tBbW5MdEJBaSs4MlNSY1lpVVUvalI4WUQzS0lQSlFTYWdKMWJGRFRQZlcxWjJFL2J0eDJQZDc3dlEveWE5ZC9COWZ1dnphMFFsbTVVbXluUzFhVnFHOTliODA5SW9RQlU4NGdrTGk5bE5vV1h0K1dWVE1GT2lNZFdYSENLbXBIeEJQbzN6S0xoUnRuZ0c0SXgyV25MQkVkc1dSWk9sblJ5K1g2L3UzWkcyNjdmekZYM0F6KzlJa0dQODMxaXdVZ1VJT3djOTZLUDlNbnIvbS9hcVhDMEZpZFBvaXpEbTdFTzU3OUZyTnp6M1orNUp1ZmxDdHYrcEYrKzZidkNqcjgvOG83OTFqTDdxcU9mOWJ2dC9jKzU5eDc3cDM3bkR0elp6cVZ6akNVVW13Tll0SW0ycWFnYUsycGhFd1ZSUTJhRUJMOWcyaVFCS01FalNTU3FIOG9pYURpQXcwQ0dtTjhWQ0RoVmFDQUxhL09sQXFWVHR1Wnp1dk96SDJlZS9icnQvemo5L3Z0dmUra0dBek1kS2FzNU03Yzg5cDNuMzIrWnoyL2F5MjRvUSs3ZDRrTUIycXM3d1J6cmhMdERzcHA1bDAzRGxRb053UjNYaldzcWZKaFlOdlo3eFZROEZoVkFKc2s0aEJjVWNMYWVSZ1ZESktoM3I3M0J6aTRmSWpYM0h3M2V4YjI4OEtGZ3pKSWVyRFQvSVpqQXAwR3owc2ttdGh1dEN3QVh6dnpkVDEvOGF6ZS85akh6RmUvK1VYOTBzcmpuRng5eXZ2Snd5RXlNWVhCaUtzcXo5aHB1Sld1WmJMUTVLaUpYOE0yd28zZ2syWSt0RlpPV0JzenUzOUdkNzk0dDI1dlZWS2x5TVkzMXRnNHVvTDBFMVJRKzVJaFpzS0txMXlsYzZtMVI3Y3Y2dCtlZWxWMWV2MmhXQVg3emdDeVU3NzdBQVI0dzh0UzN2TndhWDkwL3ZmbHRjdHZyUk5iNkVhVnBxZTNxWjVZVWIyNExTUXBUS2RxSm1mQXFUSFdxSnRQY1ZQaUM5L1dFQ2dlSVowUUVxU3h0Q1J0NEJkY0wybmlnT2FqRDcrMC9BLy9uaU14MVBuZzFwZ0VVcUYydGJKeFVSZ1h5cmhBc2tsZU9udFFGck9oN3A3ZEx6KzQ3K1c4ZVBGNnpkS01aTkJqMEIrU0pST2FDbUtOUVVXb2F1ZnljbXhHMjV2VStaaTZxdVQ0eG1rZU92a29UNTQ1cHF2YlcvclZjLzl0UnVNVlpXSUNNZ3U5YVRHOW5yZk90WXFySzMvT05yUTFkTW00a1hPNG84KzVHMlJvMjg5QmlIODNLNUlhMmZ2U0paM2FNeVhGWnVHMGIyWGxrUlhXanE2SXBDazZBUHZpb1pxSlJOeTRybVZ2WXUyWHh5UGV2WEpQdm5iKzQ1Y0RmSEM1QUFqQzIrNnd2UDJUVmZwVEMrK3NYcmY4WnMxZHlkZ2tGb05zbHVqVG0yaGgvSGpab2c3Wlo0SERRMkhDK1BwamRQRTZWcGRvVmx1cjZ6T3NNZk1mS1ZVK2hkYThpTGpCc2VucWg1YWRyR0RERkErYmhFRFdhRjA1b2RnUXhpTmZEU2dGcWh6cXdwR21ncG5ReFBUSUV1TnBaU3FtckN2TkpSZktMYVVxQlpzcXB1ZkxZaGxLMm9QQmxKaVFXcUVDVjlZUzZzeVJVQjhCMXFieS9HVm9sd0YyS1lkK0c1UmZwK0hCS2lKR3ZkYkxtVm1jNXJwYjlta3RVQmFsbUo3VjA1ODl4ZHJqRjBVeWk4NGthbTZlOHUzU2hYTzZsRnB6Ykd0ay8zcmwxZm1wQ3gvNWJxUmJ2alZRTHFkRSt0Wjl1LzlVN2w3OFZXZWwxRFZKVkJUV0N1SFJzWjkxWXRWUFY2cndRRG8wcWN3bHdyaW1JYlYyejdWTEFKQWRqN1YrWDZUZlI1OVFORkR2Q1NFamdZMnN6MzRWd2hZZGcvVlRUZU5rQnhHUGRoOTU0d2ZRYXR2U3FENFBhTVQ2ZGZjTnhUNVMvQjExWFVYZFJPeVZiNzVuRXZOMnRGclBuMnZRZHRwcXZ6Z0Z3b1VIL0hJWW4xWmZ6N0VpN0gvSlhsMjRmb0hSeGphdWI2aTJLM25tSTAreWZXRmJKVFdpTXhubWxtbi8rbEpyZHFlSlBMYTl3ZCtmK3VuNm0yc2Z1NXpnODIveThrcE1WRmZwWGZOL3FQZnQrWFUzSWM2dE9HRXlFUzRVY0d6TG4wVWFraFNWODVPV0R2VGh1Z0ZVdFc5MkZ1T2ZvRHQ4UUNCUVU2U1RmaEFDUUJ1Tm9IRXBodGN5Qkw2ZnRrQm9Ya2NMS0JlYTB4dkNaa0NLZDcrYS9nMXB2VkhmWHhabjlrRndEVVNiY3dIL1phTUQvc2dYVkkyTlhocWUzdlpYYTNpclhSUGNtbDEvT1JSMHUxYTJDek8zYjViOU4rOVZrMWp5VVU0MjNaUFZKMWIxeE1lZm9zWXB6Z3BMR2ZhV2FTRjNnQ3VaeXhMenlOYXF2di9zdmRYVHF3OWNidkRGUzM2NVJmZ2dodnVva3pzWDMrTHVuWCtIVyt3WnpwVTFBMk1wSFJ6YlVEWWQ5Qk9mQUZXRXNZT1pWRGs0RUNhTmtxdFNxNGxhcGpHNWpTNklKamdBdERIZm5XQWwwQko5NktpZUFtR2sxWlpld2lmZTBybjh2UzErUWxDdFFZTkZVTWFEQktDR2ZGdjBPZEgyZHRObEY4M3RqbDRSM1RFNHFZbXN3ekdjaG1wSCtKWjUwclZvVWNOR3BSUFRmWlp2MnN2MDRsQ0tyVnhkZ3FacElpYy84elFyUjFlRXFSUktWVGt3Z1QwOEtlUTF6bERKWEpMWS8xbzdsN3pyeEUrTXl2TGhLd0crK0s2dWhEUWd0SGZOSGRGWExQeU4zakF4MEpXeVF2QkZoY2UybGJPRjBMY2h5aE1oci8xdzlCZjBZWC9QMC95MzFZUFVtSTRwMWc3enVER0hyWThVTmFKRC9aU3NtSHQwbmZoVU9pWVE4TW1WYVBMWUdjdzJ4NDhSYVRTMFRXS29JYWdpc3VPbFVjTjI2ZnpSRDRWWU52UHZKMVlzR245WFc0WXlYdWRwcWJCVlNIK1E2ZExCQldhV1o4U1ZOYTVXc21HbUc4K3NjK0tCRXpKZUd5c1RLVklvOXZ1SHNLOEg2MDYwTHhXN2trUS90LzZWNUUvKzUrY0tlUFJLZ1ErdUhBQzl2STJFdDFNbFM0TWZzciswOTMzdXRzbkQxZG1xb2pDVzFNQ1RZOUdueC82RFRZSkdxSjB5ZHBBWllYL1BYemdyK01GSWpRTVhUSERIWDFUQ1JDNW96R1h6U1p0b0ptbDZOZ2lISTRLNnRaSkVhOTQ0L0VKVHVHdm1GVXJUMDBIa2VYVmlvT2E4SElweHNtT0thL1JuZldOVUo2RWMrMGVJSlRUQitiVXFXaWhzbDVJT3JDNWNOOHZjOG93ZlVwVlhaSk9wYUY3cnVTK2NrUXVQclNqOTFHZHdCb2k1ZFZyTmRBS2J0V1BlT2kwMGxVOWQrT2ZxNzA3K0FzTFdkMG91K1AvS2xRVWdOSUhKRUJiS1g5djNWL1Z0cy9mVWhTbDFyVGJTczRhdFV2UWIyNTRLMURlaGRPU2pSYlpWNkFNdjZDdkxmYzhtMkhadGlLSXhDcGJvalVVaWdQT2tUTk1tcjd1bUVHTFZMTnpvZ0Z1RER4ZzFVRk1JNllTblFwemMzOVcyaEw4U0FOUnhDWWhmRm1tMWVIUXRBTVQ0SmQvK2hzUlZGS0tDbGs0WitRRkRjd2QyTVhkZzJvbUtWS1dLNlZzVmxJdkh6c3ZLdytmVXFRcURWTWxyWkc5UHpFMitBT0FLVjV1Rk5ESG5TdHhITC81Ui9aK25mc1B6dmI2N1NlWnZSNjQ4QU1IWGpqOUZoWUs5ZCs4N3VXdnV6YnFVb21mTFNoUGp0MGlkSEN0UDVaRFhRaWFnT0VRTXBjTEkrUjZPZlQxWTZpazlVUW9WQ3RmR3hCTE5zZmpJTlhaMkdjUjN2TGx1dGFEckEwTG5JTTNOSnZpSXYzUjhVSWhkNzNRQ200Ny8yR2pUQVA1bXIxdjNwMjFLaituUDBKU080SE9qb3dxRDBabmxvY3pzMlVYYXM1UmxyY2tnRVlQUnRhOWZZT1dMWnltM1NtRTZnekZLaXRpYkpqRkxQZFh0R3NtazFtbWI2Tkd0RGZPcGpUZVZENTU1YjhkV2RIeU1LeVBQRFFDOW1HaVV6TTB6UDYvM3p2MnhlY24wWXIxWlYydzdLeE1pdXVXVTR5TTRsWHUxMWpkUjB3aVYrb245bVhIczdRbkxHUXlNYjVvdXRFM1BSUCtwdGE4UWkvVE5zeFRQeDQ2cEZ1S2RNWmhvejdveHZKMFdPWTBPSkVUZWZJdFRwYUVoZUNDMzZSTm9UMG5WUjkweHVkNTBwaW1VZm5qNTlOeFFweGFIcFAyUTMwbEVwWUxONCt0eTRkaUtGcXM1VEtZSUlwbzc3SFY5TlljblJLM2d4czdKdkZWVFkrdVBubi9JdmYvc3IwRDUxV0NSZG5ZclgwRjVMZ0hvLzM0SVRvRHIrNjlaK3JQcTd0MC9YdlV0WnJXcVFLenJHK0ZDQ1krUFlLV0VWQ0Uxa2F3Q3BTcEZ1SGJ6cVhDZ0J6T0JyT29IWjlQNGM5Q0pPTFhGV0FSZHAzOW5SMURqejVUZzUwVUtZZXQvYW91dUptaHdRUlh1NEwxd1NlOXg1MzYvZVZSQ1MyaThUMUFsR3lRTUZ5ZkpKc0w3S210MTV3dkdUMjdLNW9sTmRWdWxCNTRWMGJIQ25NVWNuc0RPcHFxRnEyc2p0WmxKZXZMRUp0Vm5MdndCOTEvNFhZUVJQM0xsZ28xdkpjODFBTDBjd2ZLUG5oQ2UzTG40bTd4aTEyL0xDNmVHOVlaek9xclExQW9KeUxsQzlQZzJYQ3o5Zk9yRU5JUUFuUHFKcmFXRDZRU1crckNVd21TWU1GcmlINE13Z0Z1bDBVN09Fd2NhUDdDTlVEcytINWNtcm5jR0lhNERuSmg3akVodUFCa2ZhOTNIMWw4TmdSSFJGeFJJakdTN01uclRQWklhNnJOanlwTmJXcHpacHQ0b0JXdGdNdkZQemdVbVJlWGdBTm5URTZrVktWM0YwUHA1YlorNytBM3pMMnR2ek0rc2ZTejRlLzdmNTFpdURnQjZhVXh5TnBVZGRxL2I4M1p6NC9CbnE0VU10MW1Wak5WSTMxZzF3RXFwUEJrMG91SlpISkhlSEJQWlpXaTZtVXRndWE4c1pHR3daakRmM2VXTENxRnFFZjAxZnlhdHp4Y2dKNTNiMHI3WWw4YmlhMExrNnRyVVRqU3pUVlRiYVR1N2RKOXhZcFMrSUltSVZLSjJ2Ulk5bDFPZkhhT2pHaXgrV0h4cWZmZGhyckRMaXIxaG9DeG1mblJnWGp0NnhzbXNUVGkralh4Ky9TK3FmejM5VzhEWjc2U0I2SExJMVFSQUw1MmlkL2J5bVh2S1Y4NytuamswZWF0TEVoaFZsZFpxeUl4ZmRyRmFvay9seXFuY2ovUklqSkEwcnA2RzVTbCtmSndWWlRhQmhWU1l6WlRKMEtnYjlVQ2MvaFQvbHc0Z2Q0QXA0Sy94MVNTbVN0aXg3Q2JtRUdNd0U4MnZnbCtFR0k1aEpLeEVVOThVTkhLd1hncXJKYXhXZmd5YUZlaFo3M3FJK3RISVJZM3NTdFVjR2lMTG1lQlVkVlE3bHhrMVE1dkl1RUsvdFBFVjdsOTlTMzFpL2NNSVhPa1V5N2NqVng4QXZSZytlRVM0NzBNMWtDUS9OdmRHdVhYNnJkeXlhMjhwQmxrckt5bkZhRStNV29HUmd4TmpPRDJHclNxWVV4UFhRbmlKUUl6YmZGTHhHMzEycFRDYitpYjZnZlY3YnB0QWdXYjVzdis5NHg4YTdlZ1FqYWE3RTRCMG4wc0FiRlNjemg5cjI4SFl3VmFGckRsMHJWVHljTHhFZlBTZmh1VXhoWG93cHFMTXA4aUJnY2hDaWlRNEhUdEhZcHhNU2tMaGpCN2RQTWtYTjk3aFBuMytMNEU4ZkttZnMwRGovNUtyRllCZWptRDVKMm9jVE1KU2NXVDNtL1NXbWRlN1E1TkwxS0FiWlVXT3FCRWpQUkd0Z1FzRm5NNlZjNFVuTTFqeEZSVFBBZlVPb3lNRUtNNHZXQWxzR0hyR2MvSW1FNWd3bmhUYU14NnNpUTFnRXEvZG9yL1lEaU9pTWIvcTJ1VXRkUWhReGc3R3FveWNzRkg2S1FObGZJN3pmZFNaQ0lsNHlxY2lmdmNhWHN0T1daR2xEUGIybEtuRTkvWGxycFlNeDFTU1VpcnkrUGE1NUpHTmQ0My80L1M3Z0JVTThKcXJUK3QxNWVvR1lKUk9rREtBL2VYcmwzOVpEMCsvZ2VWc244c0VOclRTc1FOSDRxTmtDN2xUenVmQ21Sd3UxRXBlZTZDbFJyQW1kTGxGQ1NtUUNwLzRybFdwUTZSc05HaFQ4YXlkeFBxc1dSeE9GSmsyYlNRTHprbndNZHZtNzdxeHkzNWdweUZNRU90OEJFNjEyYmNyS0lORW1PL0JVb2JNSi80Y1JrNnB0WmFlRlhaWnEyV0ZlWHo3dUg1NTg5M3BSODU4SUljbnJsWnorMnh5YlFEUWkwL1ovSXdINGdUc0dkKzMveGZkVGYzWG1ybmtWcGxOcVkzQW1xc1pPejhRdVNkK3NjM0lLV3VWY3I0UVZrcGg3SlF5ak1LMWhyQ1V1L1g3SW5VTFdsUHFnUlZ1aDhsSGpwakNhY0xtSmhLT2VXd1JEeHdpMktKdnFONjgxODZ2UFhQT244TXdWWmxQUlhkbk1KVXFXVWhDRitxdzFESzAxbVRXbVBNVjd1em9VWGx3NjYrclQ1NTVMM0MrQTd5cjB0dyttMXhMQUl5eUE0aEFtdDAyK0VsM2NQaDZEazIveWgwYzlqUXhzRm5YV2prVlgxKzFCS3RGcGJCWktadVZzRjdEZWcyYmxSK3VHU05pcGRWNjBxYjdkaEFXOFBuZW9EMGpXdHVQM2NWYWNlUWUwb0k0K29XSjlXWitNb0c1RkdaU0dGcHZnaXRWS1ZDMVR1a1prVjVpYmFYdzlGYk5pZndUOHZYeGU4clByUHdiTU9xWTJtc0dlRkd1UlFCR0VZNWdvbWtHeU1nT3Uzdm1qdWpCL3QzczZkM3VsdnZRQXdwQnhzNjVVZTBiMXl5Q1JackF3Tys1OVhOUU5pc2Z5R3c2UDR3eEpyT2IrcTZBaG9Ic2tXZllSTGNTYU4zQmhCcUIxSGN0a1Jub1crOWJUbGdZQmw4ejdRQzdWQVZYQzZqMHhXcG1qQURtYklXZUxoNHhqMjM5dTN6NHd2c0tpa2ZERmJqbU5ONmxjaTBETUlvSDRoSFlNWnR1S2IzWjNEcjdhbk5vOG5hMzI5eWxTLzFNZDZWSUFWcXFrdGZPcjZaSFVESGV4elBTYUQwblBraXAxUWNDVmUwRGhsamhNTVRaS3NIZ1NveTYvZTVlcTUwQUtPUXBiU2pieFVIZnFDTzJ6eVhZdUJKWGN2VVIvZW5pYStiNCtNSDYxUGdmT0xyK2Fmd29lRkFNOXlIWE12Q2lQQjhBMkJYREhaaElkT2pJQzh3ckYrOTBOMDdlenJUK3NIWG1SYkovUUQyVG9sVW9ZRlFDMndyT1ZTaE93amhxYUpwOFl0VGh3ZFJOdy9nMFhtUkJ4MkdQWGQ1TjJNRWxpbFhCQnJDbDR2ZE1GdzVkS1dGenZNVks5UUJmS3g3U0IwY2ZvTng2QXRpS2Z6YVV6cUl4ZjE3STh3MkFVWVMzSVh3Q3d5ZjhFSXJPWTVNWkxNc2RNNi9LWjVJYjVlRGtDNU5lY3B0YU05RDVOR0UreFZqakI0TldFVDgrc0tWMm5sZW5JV2tjR1EyQ3FBbnBhU3ZOZmtFaGFNREFwaGJuVXorNmxpTVhxMEp5OXpnWDh5KzRFOFZGczFyZVgzOSsvUmp3VE9kZGVORHRScDhQMnU3WjVQa0t3RXZGY0FSaDltV0dQMys0ZkphUGNSR3d5ZDJMTjVEWHI5QzVaSW9EZ3hsU2U2TTRibERSSHBsSk5NT3FzUVlqaWZRa2krT0twRlM4T2FXaXFDc1pBNlZ6T3RZUzBYV0RlWXE4Zm9KbjhyT3lXcTJyczUrdEh6ano2UFd3K2lTTW03UHdSUmZoVG16UWRERWtldDdLOXdvQXUrSnpJbmRnZU5ITGhIYy9YR0VhWTNtcEdHQUtzRU13bTVEMjk4eGs5ZmNsODVLeHB4Yk5zQmFiUzEyUGF5ZnF6bGVuaXRYZWhUTFA4M3dNRk9GbmswdUJGSy84NzVCd0N1RWlqZzgxQ1ovdkdmbGZCdThySTdFOC9LRUFBQUFBU1VWT1JLNUNZSUk9IiBhbHQ9IldoYXRzQXBwIiBjbGFzcz0ib3B0LWljb24taW1nIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im9wdC10ZXh0Ij48ZGl2IGNsYXNzPSJvcHQtdGl0bGUiPldoYXRzQXBwPC9kaXY+PGRpdiBjbGFzcz0ib3B0LWhhbmRsZSI+KzM3MiA1ODcgMzU0NTY8L2Rpdj48L2Rpdj4KICAgIDxzcGFuIGNsYXNzPSJvcHQtYXJyb3ciPuKGkjwvc3Bhbj4KICA8L2E+CiAgPGEgaHJlZj0iaHR0cHM6Ly93d3cuZmFjZWJvb2suY29tL3NoYXJlLzFFTFA2S0M2clYvP21pYmV4dGlkPXd3WElmciIgdGFyZ2V0PSJfYmxhbmsiIGNsYXNzPSJvcHQiPgogICAgPGRpdiBjbGFzcz0ib3B0LWljb24iPjxpbWcgc3JjPSJkYXRhOmltYWdlL3BuZztiYXNlNjQsaVZCT1J3MEtHZ29BQUFBTlNVaEVVZ0FBQUtBQUFBQ2dDQVlBQUFDTHoyY3RBQUIyb0VsRVFWUjRuTzI5YWFCbDExRWUrdFhhKzV4emg3NDlEMnJOdHVSSk1qWllKZ1lUV3pJMkpBd0dZOXlDZ0JtU0FJOGtFRUo0anp3Z2NhdEpJSUVRRXZMQzhFZ2VTY0Rnb0RhRThRVmlYckJNd0FHUDJGanlwTllzdFhydXZzTTVaKys5Nm5zL3FtcnRmZHFTTEJ2SmxzRGJ2dXA3ejdEM1dyVnFWWDMxVmEyMWdNOWNuN2srYzMzbStzejFtZXN6MTJldXoxeWZ1VDV6ZmViNnpQV1o2elBYWjY3UFhKK0NTejdkRFhqNlhCVGdGZ0Z1RitDRUFEY0J1SjQ0OUNnZlArci9IanJxdjE5SGUrRVdBc0ludTdWUGwrc3pDcmh3VVlDYkUyNjhUckQvZXVLNlE4UXRJQ1FSZUFKMWhoVGNBc0h0UndVblBpQzQ3WGFhZ2g3UkorNGhUNC9yTDdrQ0hrNjRFUWszM2FLbWFJOXVtZlpkZDJoYlUxMnpmL1BzdzFkUXV6VmdGUmlOUjlVNEk4dUVGWFBLMVFSSXl3bmM2akRkMGtvU2Eyd1F6VmJHYVBUZ2FGS2QzM244N25QM25ML3QzQ00rUkFSNGd5YTg5WlprU25rMFAxazlmNnBjZjhrVWNLQndQMWdwdUdod0RoNjhjZStaK3ZJWDZkTGFidGwxN1pXU0pqZDFhWHlKcHJWYXF1VlZxVWE3S05VdTFpc0FLbE1ZTnZZN0NFZ0NVb0tRSUFGQmhpQURYUXVpbTZWYXR0ak5McURadkpBNE8xdFgrQk9ldStlOVBIZi9hVXo0b2ZtSC92UGRpKzBWNExBbUhMa2xBZEMvaUJieUw3NENIanBVQVllQU4zOU5CbnNESndEcWE3L2hjL0x5cFMrc2R6enpKZ0F2MHJVcmRuTzg3VEp5QkZSanlHZ0NRb0N1QTZoQUVraHVNM0pXRlFHcXBGQUZTUkVrUllJQUN2dFNCUkVWaUFDU0NNMGpRUkltQWRJWW9na1FnSGtLNldhUWJub0swd2RPMXUzbTNadysvQnY1N0VmdjZPNSsweDhDNlBwR0MvQzZYNjV3OUNqK29sakh2NkFLNkpidWJUL1lEWlZ1K2ZLdi85eDI1NTRiMHVxVnI4ampIWitOMVN1dTVHVGZFc2Nya0NwQjJBSElMYldsdGptaDdRVHpER1FWWkJDQ0JMZ1JFZ29nQ2xXRGh5SUVLRWdKUUlLOW1HaFdrZ0pKQ21pQ1VDRVUxRFV4SGpGVkZVV1FpTHF1cEFiWmdlZ2cwNGRWTm80ZnkxdnI3OFg1WTIrcHVvMTN0UGY4M0o4aUdpQUN2UHdOTlc1N2Vsdkd2MGdLS01DaGhNTzNFa2RFQWVCR29QN2o1MzNYUzl2dDEzNDVKM3UrZ2l2N25vbWxIU1BJQkJoTmtOaUFtbHZ0V3NHOEU4eXlvR1dDQ2pHdXNWUW4yYjU5eEV2MkxzbWVYU09zck5UY3ZuT0UxZVdhUzFXU3FrcW9rd2dCVmFXb0NsSUZ0QzNSS0RCdE1xYU5jbk9ydy9xRkRubmV5blRlOHZTWnVadzkyOGkwemRTY0FZV2dUc1J5eWhnbEpCRWlqU3BVbzZSdEE4eTNnRzVqTHUzWkI5TFcyZCtvejl6eDIvTzcvdDFiRWRieE1CT08zQ3hQUjZ2NGRGZEFBUW5jZEV1RnQvM1RMakRkeXRVM3Z6QWZlUEZyOC9KbFg4TnRCNSt0cXdlRVdTQ2NVVVE2elZrd2F3UlRUY2hKMGtpNFk4Y0V6emk0SXBmdlcrYmVBeXM0dUdlQ2Jhc1ZScE5LNnFvR0ZPaXlvczFFemtCVzg3NEtrS1NRb0VETUNST2FnQVFRQkZnbFFFV1lCQUlRbW9INXJNUFdQTXZ4TXcwZlBqNlZFeWMyY2Z6c0Z0Y3Z6TUdjZ1FURjhnZ1kxMHhTa2VDSUdBR3NJYlBqU05OakgwcnJEeDFORDczajUrWVAvK1pkQU16UzNueHp3dEdqaWljMGJIL3lycWV6QWdwdVBGemh0aU1kQU53QWpONS8xZGU5T2wzMjRtL29kajM3aS9QcTFTdW1Bek5OWUp1YnRzWldKMmhWUnFNYUIvYXQ0cmxYYlpjckxsL2w1ZnVYc1gzN1dPb0tCSVh6dHBPdE9hVHRGRTFMekR0QWxSUVJpeS9vaWtSU2toQ0VLRWxSQ0VWQVFraFNTYWdJSUNReVJhelZFTmlkSnVNa2RWMWhNa29nd0tacGNXR2p3ME1QYitIaEJ5N2c3aE5UbkR2ZkNEUURZeEtqcEtsS1N0WVZScE1FdGtqcjk2MWovZGl2MVBlOTlaZm1ELzNtVzB3eWdSVnZmc29yNHROUkFSY1VEN3V2M1Y1ZittVi9BMnZQL25idWZkNW42L0pCQ0RzVnRFMXVtaEcyY2tKTHJxNVc4c3dydDhzTG5yMkR6N3A2cCt6WU5pWUVNcHNyTnVjWjA3bWk2MGdGSVNtSkVrZ2dXZ1ZiVFFJU2RHekhUQ2dKS2lsQ3FCSVFFRXhpY0JBSXBWUUN5VjlnK1M5RUlDQUpwUUJVQ0lCS3lGR2RzRFNwSUhXQ3RDcG56ODF3NTcwWGNOZjlaM0g2MUpTNVVXQ2NnTlZSbHFycVNGbVNORUk5dnkvejdCMXYxUWYrOUdmMXZsLzhEUUF6U0FKZTkxK2Uwb3I0OUZMQVE0Y3EzSHFyUW9UWEFwTzdQLy9IdnpPdlhmTVB1ZTN5ZzVCbFNNbzVvVUhlN0FUbnN0U1RDdGRkc3gyZmMvMTJmTmF6MWpoYW1zaHNuckUrVmRtYUVXMm5VQWdvQkJVa2tpZ1ZBSmtKNUV6SkZHWlhMRldBekRBclNKajZBWUFwR2dpcEJLQllXS0lxVUNVdDhTRVFDNGpFb3hiL3NUc2tFWkJLSlFHbHFNY3p5K1BFOGFRV1NjRDZlcVAzM1hzQjk5eDFRVTZkM2JCdkxnbWtUaG1URldFMXFyQjFEdW5jZlI4WXJiL3p4K2J2LzdIL0FtQm1nZEFiMGxNeFdIbWFLT0RoaEVPM0NJNUtCakNldlBENy9tWjM4Q1YvUjdjOTQ0V1FCR0hYUWpUcDVpemhBbVQzbmhXODdFVjc4Ymt2M01tOWU4YmMyc3B5K2tMSHJibENGVW1ScUJSa3BXUWxsTXFPZ0txSXVuSXBCVXBLcG9BVVpCSUV3UXlxeTQxdTR1aktBeUpWS1lGUWdrbnNPd0JJV3FRaEFNeVBteHA2dEV5em1nSWxTWWhBUkpLcnJsbmNxaEtNUjhCa1VoTUMyVnh2NWU2UG51V2RkNTJUMmF3aDFoTFNhSnlaUmtSS28wcTNnQXNmK3RQNi9uZisyT3pELys2L0FPaHc2TmFubkRWODZpdmdqWWZyY0xmTDE3NysxZDFWZiswTnV1K3pYcHl4akpTbm5hQkplVXJnWE1iVmw2MmtWNzNzQUQvN3VsMlFsSEQ2UWljYld5MWJCYUVRczJyS05pZHBWU1RUWEdsV01JT2lHY2dremRLSkVFUm1ncEpRdHgyRzdTQ3VweUJOWndpb0NGSmxjWkVBZGk5WE5VS05wd2JNY2lhU1NDSlVpUVNNS2FsWlNZcXJvU3VuMldBQnFJUUlPS21yTkJsWG1EZUsrKzQ5eDJQM244UEcrVGt3RWNpSUhRakZlRzFTNlFiazRYZjhpWDcwTjkrUVQvN2U3MTRzMDAvMzlSUld3TU1KaDI4QmpvaE9Mbi8xdGZteWwvMFFML3Y4bS9QeTVaRG1iSk5FcTd5aENYUGxzNjlleFpkL3dVRzU3am5iWlQ0bkhqNDc1MVpyU0Y4VmFMcU10aU02RmVTc2FKbWt6V0xLbHdrRm1HazBDcFZVZ2VSTTBJd1BWSktRTUt4blFhOG8xYlRPOUE4VUVncEpJaElPVmhrR0xYU1NJa0tqQmFrUUpJUlRGcVgveCtNVUNDRWlVQ0lsSUlVYkIxQkJxWUF3QytvYW1Fd3FNaEhISDlyQW5SODlLeHZudGhSTG9GUUFNYUpNSm5YYXVoODQ4WjZmWG5yUEcyN1pCRTRZZFlOUGUySEVVMUVCQlRkOFc0MTMvV3dMb0twZjlNUGZxZnRlOVAyNjY1cDkwS2FyTUpYY29NSkRNenpqNm0wNDlPV1g0N09ldFFPYlc1bkh6elNZWlJFRjBXV2c2WlJOaHVSTzJaRnNzMGluUU50Qk1nV3FSS2RFQnFuK3QwSUVPVk9SbU1GRUJaUUpGRk5BTjJsUXMzN0djd3NabG8wVWlKanBnMXMvSWNBRU1OTTBpQkZSQ3dUdXd4Tmc2ZzRMcFEwcFVqeW1TV0tmVFlnb0dramlFYmtTZFVVdUxWVUFranh3MzNrYys5Qkp6cnRNTEZjQ3FHSzhUV1M4a25EaWp4K3NIM2piRDdUSC9zTi9NbkhmV2dFM2Y5cjR3NmVZQWxKd0dJSWpvdU9yditJNTNWVmYrWDl4end1L2lOVUtFbVlkUkN0OXVNWHUxYkY4L1ZkY3lodi95ajVabjJZOGVMTGx0S01vd1hrTGFUcGlucFZ0cDlKMG9DclFVZEYxUUtlUVRoTTZWWFlVVVJwK3k4NlV1T3BRRmFJd3VHYWZvUmt1bWowamFhd2pGWUJFdUd0OENnang3OEE4TENFUWpheU12MjRLYU1FSnhiZ2JpR05DV0VaUGhDSVVKQUhGY2FRVjV5UkpaaFNaUUVsSkNTVWd3cFhsU3JxT3VPdllHVDUwejJuaFVpVXlGckJMSFNiYjYxb2ZCbzYvNDllN2QveGYzd0U4Y0Q5dS9QMGF0NzBpNDlPQURaOUNDbmlvQ2laLzh2eHYrZmIyMGxmOXFPNytuRFcwczY2dXU5UnRac0c1aks5NitRRzgvcXN1aFZZMTducHdqbG1yNkNDWXpSVk5DNW0xR1UwRzVobnNNcVROeWdnbW1telVTYXNpYlU1UUN4ZEFrbG5OQWRJMU1Lc0loRkFJVkMyc3BmdFZBRUkxZk1laVRNZ0NnM1lzY3FXSEY0Ull3QzAwZytqS1pxNWRKS3E5d2dWSGJDMWlzYkpBUkZtWlgwWUNLUUlSdFRZbFVRcUVDWVFrMCsrVUtPT2xNZGJQVG5uc295ZlNiTm9DU3pXaG1SaXR0REpPRXpuMXZ1UFZuZi85SDdYM3ZmSG5QMTJSOGxOREFSMFViOXQyN2I3bStkLzJFL25BaS85R0h1K0RzTzFFdGRMakxhNjVhb25mOVkzUGxPYytZMDJPM1QvRHFjME1oVm04YWFlWXpjbW1wVFFkMEdSaXJrRFhHWStuU21RcU9rMmlKTG9zNkZTZzZOMXFkbXhIc2I4dEVMSFNBa0pFTlVTbExOU2RrVEY5dWxrTXZubnc2K1FmeldjNkhIUURTeUhFbUVaNmJFeC9vTHRuZjY0OVZXRVo1b2gxQmdOSElvbmRSR2lsaXluWjc4ekVlRndoMXlJbjd6MkhoKzg3UnlZQUV3R1FHb3hYSjJuclBsVEgzL0ZEN2Z1Ty9CQ0E2YWZhSlgvNkZkRE1mMWZ2KytLWDhyTys0WTM1NEV1ZmdXN1dWWmluZklFSjV6Sys1YXN1d2V0ZmV5bk9YS0RlZGJ5UldVdHBPc1cwSldZdE1XMGM4MlZpbGdWZFp5bXpOZ090bXBLcHVWeGtRTHBXcUdKUmNLWUF6RlRhOENrOHBQVm9ReWtnVW5DRmNGTkZHRlVqR1lKRVUwcGhrYWZaTmRORUFoYUpxRktTcWFkNDNReEVyWWhCQUNSejgrWmZYYTlUZ2tUOUE2Z3FJaUtnRWVXaGRBWU1KR3ltMEtaUkJVQWhNaEppYWFubSt2b2M5MzM0SkpxMmhheU1TR1lpTFNFdGpTbzg4TWR2Vy9wZi8vcHJ0L0RCaDV5dStaUW80YWRUQVFXSGFYanZHYS85YWo3MzBCdmIzUzlad3V4Q1c0MVI1UWRuY3RuYVJQN0pkMStERnp4bkRYY2NhM2h1TTJQYUtqYm5XV1lOTUdzVXMwN1J0RUNiQlcxV3pMT2dJOUIyTUp5WHphaGtRRHNGVkNWMUJEc2FCZ3VMWk5ZUUhsVFl2eFlpR2czRFFHb1VjNUVrVlFSVTBCV0NBdk9NNWw4aFZKU1l4TWdVMDJCeExqQTdWaXhCTDVXOU8wYmhCd0VSaWNaWlF0bmduaWRna2l1NDNRTXFvZ2tFUlVpb3BDb0JnS0t1S3hLUWt3K2U1Ym5UbTBSZEFVa0ZkZDJreWRvRUo5OTM3K2oyWC9xVytZbmZmTXVuQ2hkK3FoU3dwL3pqNzBOTU9DcDU5TUsvZXdUWGZNVWIycFZyczdRYkJLVG0vUmY0Rlg5MVAvN3UzN3hHbUFWM1A5eGluak12YkNrMlpvcFprMlhXQWRPR0NKZmJkWUpHZ1U2QlZoVk5Cbk1XNmFpTTZEWXJxQkNhRXJxcnBWaFdGNEpzeGdTRW96RXgzK1paanZpL0VFNDhpMEN6Rm1kcndXa3hhQ1VlUWR5VUlxV2FpbDQyNkIrRUtBSkFKbjh6YkMwaWI4Zkk4c1hzNkl1NHhhZU9pRkpjK3lGSkJFUktDVlVpNkNuRGVpUllQNytGMHcrZU40OC9rZ3hxeHRMdWNUci93VnpmODEvLzd2d2piL3habzJxRWVCS1Y4Tk5oQVl2bG03endCMytpZTk2ci9uNldQVjNLRjBSblZhclB6L0Mvdi81eWZOa3JyOEJISG1obFk1clJFYml3MVhKOXBwZzJLdE5HTVd0UmxLL0podXRhdDM2ZEt0b01ab3BrV3EyVWtjb2l5b1NzNW5xVmlNQVJsb0FMK0s4ZW5FUWtHekVvNlFLelVXRlJCZ2FWTEdFZ0pYU21LSWRybkdNOUFGWTNxTzZMZ2NvU2VnRFU4b05HQkJycWN5OGZDQUFFVXVGNkNFbkdBL25mQ21xeTdJcmRSZnh6VWJvekdndG04eXhuSGppSHJtc1Vvd3JJTFdWNUIxSStWYVdQL0xjZmFlLzQ4Zi9UYkhqcDhwT2dESi9hU3h4YU03M2t5TS9vRlYvNXY3RWV6eXRNUjNtZHNudEwrVVBmK3l4NTdyVTc4ZjQ3dDlCbXlEUUQwM25HNWp4amM2YWNkY1NzcGN4YUdNYkx4RHliSXVVTXRKWmlRNnVFS3RoWnhBcDRRWXNpU2F2MDlKb1kwV2JFTGtCeDYyWm1SK0ZoSmhJb1Jsd251cE4yUXBtOTZzSEhGcjJlaEMyTmVNU3dXWEpqNWdHMFdkaEVWTXhNOFRVNnBnTWdJaEljdUJIU2xyVVR6eUFEUkMwQkhBQXZFZ3RHMjFod2M5bEtVQ29oU0dXVlJCUWlaMCtzbzl1YUswWkkwRXhNZG5SSnVsSDZ5TkZmNkc3L2tXOXlnL0drV01KUG9RTGE3THdCVXIvMzgvN2x6K1dydnV6MTBMYXQ2cTdPcHpPdTNsN2hSNy8zT2s0bUk3bjcrSnl0U3RxY1pVd2JHdTZiWlc2MXhLeWgwU3lkUmJ4enRZSmxWU0lyMlZJa1U5QmxCU25zaXZGUlpBWElKQjNOVXBidWUySk5CNjR6Rk1SZmNsVmcvN3VROXYrQWQzMXFnOUZkaHJ0MXRheUlWTkVjb3dDdENycnMzMmRtcFliN0xaQUFhN0Y0dUlLSVFtbjRnVUF1bkRYRVRWUVNBZ2tpbHBQMmRETU5NamgzNlN5bWlDdXlab1ZJa2l6ZzlQd20ydldwb0JaQVdpSnRhNldlaktzUC9UKy8yTjN4RTYvSElWWTRLazk0SHJsK0ltLzJxTmZodzhsWG5kWHZmdW0vZVNNdmU5WE5hT2R0dllUVVBkVGdzeTRmNDRlLzUzcXV6d1gzUFRDRlFyQysyWEt6czNLcHJRYVl0WXBwQjVsM1FOT1I4dzVvMUN5Z3VWbkRmQ29Ja2hsWmpTb0RLWlFFSlFrcU0ydkQ5QkxGQXZEQ3ZoNWZtVmVVS0Vwd1hVMlJxeTA0VDN1eloyT3RodDlTRXFTUjhTRk5CdGdTdXE2Q3JRN1lVcUJWcEpTNG5ESnFGV0ZVYUdrR0V0RXFaVXVUNlc0Q1VFTXdTc0JTUWxvU1ZLTUtvOG9VVHp0RjE1bXVwUklpaGRjTnIrOVBBQ3c2cHZVSHpsOVAxbGFFU09pMnBrQWFFM2s2b3JDVjUzN2QxeTlYc2pVOUt0LzJaQ2pocDBBQkR5ZmNmb3ZjSUZLOTkvTi8raGZ5bFY5NE0yWmJYVFhSVVhkL295KzRjb0ovOXI4L0h5Zk9aUncvM3dnSlRKdWNwbzF5MWdxMkdtTFdBdE1HTW0zSVJpRnRSOHpWY3J1dEFwMFNVRWhISWlQQlNHVUcwUngwaUZYdkNaQ3BUakFqTUJyQ1NCaU5iTlpQRmRCa2hFZDRXcUdsdzlRSE4xYUltRDlXcGdvaUk4RjgzaEYzdDhDRmpGMnJTZXBsd2ZNUEpuN3U4MWRrZFFYY2Uwa3R6end3a21jZHFER3BCVlVGTUlHdFcvWUxjK0xCRGVMNHlZNG5qemZTYlhVNGV6N2pQZmZQK2RHemtPYk1GS2ZQWnlvbzZkSXg2dVZFWkJVbkl5T0tJWkpuKzl3Q3dyTTloWGMwclJjb1VDMlBrYW5nZkM2UUJEUmJkYXRMalY3enRkKzZWRzNMczZQeWQ1N29pcG9uVndFSndaZThaSVNqTXYremwvN1RmOFVyYi9vYXpHWnRWZWM2bitodzNjRUpEbi9QOC9XK2s2MmMyOGd5N1JUenVXTGFLWnBPWk40UzA4NTR2dWxjT2VzVXJRb2JoV2cybk5mQk1CL1Y4cmtOM0MwU1VKQVo0Z01BcUhFVHlPcS9leE9GQkJYS1pMaWVDaUI1K1pXRzNhQXJxQWc5WVVjb0ZJcWsxRlNMYUNMYVV4M3djTXRMdGdsZS9jSXh2dUN6dDhrclAzZUZ1M2JXV0YwMXR1OWpwTlJEb1FWSWRNTUNJZE5mRzNPaTJlcjRKeCtlNHQxL3VvbGYrY01MZU84RlJiVXRNWGRrRWlUakJnbmtnbE9CVE1BTHk4VEVZLzJOL3hDU1JqVnl6b0ttc2JSS25vNnpyRFI2OVd1L2ZVUm9lL1RtditlVzhBbmhDWjljREhqRHQ0M3dycDl0bDU3LzkvL1A5dnB2L09lNUd6V0o4NUZlVUhubXpocTNmTTluNmVsenJaemI3TkJrY25PZXBla2dzMDdadEVsbXJibGVXK0JqaFFXTmdwM1JMWkl6MFpueEF4V21qSVZHTWNGblEwUVFRTElhdHN2QjdUSnFROTBDcXNSZmd5eUVyZTd0YS9pSXBMNEVCS1JVQ3FtQTdrUkxPVDFQTjE2NXhHLzYwalc4NXBYYlpPZTJDaGdvRDBscHN5V1JSWUNxU2xJR3dDeVNNeVhKNlVaNm1RMUxWRk5WeVJiZURjYndUYjkvbGwvM3d3K2d1bnBaZEtacWR0N0M4RENKMWdDbEVWSHd1Sm9BMVpKM0Z1UzQvMWJvYkc1a3FwQ1FES1RsTG9tTTVNTS84OC96aC8vajkwY0M0YytySWsrZUJUeDBxTUxSbjIyWHJubmROK3F6di9LZlU1Ym1JcHNqM2FEc1h5Sys2MXVmaFlmT3REaHpvVUdyeEt4VnpGdHczbEdhckpnMWlubVhNTStBRlJkQTJpeG05ZFJ3WDZZWS9vT1J5QW9nVzRCb3I0V3BFaThtTUdJUEtxYWd2Vzc0dW80U04zcDFsTVNZbE9RRlRCZk1qNldsaFBaY0J6d3dsUzk1VnNWLzlCMlg4TWJQMndhNHFyYXRjU2QxTXFlZFVtSmRRVkFMUlNtU1NsaGo5MDJoT1NxVkpxQ0tndXNJWjcwNElpc2xKV2xhc0VyRW1RdmVlV3FRNVo2UWxrQzVYaHdCenlTcnhmK00wakREdzRXdWxVUVpqNFRaSkFwSlFMYzEwbXBiTjNyMjEzNWYxWjA1MGR6MmluL3pSTlFWUGprS2VPUGhHbS8rd1c2eS8xdGYxVDMzTmYrK1c3NnlTL056RlRZcFM3T01iLytXNnpHZEp6NXdha3N5QklaN0ZOT09hTE9nNlNqekREUlpqZXRUUWRNUmphclY5QkZpNUxKNWxleFl6akNmODN0UTBHaFovNENWbWxncFhrU3pDSjdDS0JHeEFvVWhtd0YyUkVyOVlDR1RDU0lUa2ZhRFczakJkdkNIZjJBL3YreGxhd0lBdWFOa0NPdEtaRlFKTkxucUlJbXlGMUhvRkdBbWlnQ29hblg0VEtWVWNHQ0s2ZFBEc25FQ1ZoV2xyZ1MxWm9GMmdFeThJc2FxV1RIUUtYVnFobGF5UXgxNDlTZ3JzNm95RDdpU0FKTVIwTUptdHdqUWJsVHQ2cjV1OUt6WC8vaHkzbmJQOUxZai85VU16U2UvSFBSSlVNRERDVy83d1c0SHVXdjlzMjc4R2QzKzNER21wMXNrcWVWc2c5ZC8wL09RNmhIdWZtZ0xMWW1teStneU9WZEIweEt0S3VlWlp1MXlRcE9CVm9rbUt6b2xYUUdwcm9SWitreEZsbkM5OXJlS0ZaZENuVlFqUmZzMG01ZmpTZEFUemdaSytGZWp6b3dMUmdrbHgxWUJrOSs5aVcvL3doWDh5RC9ZaSsyck5ick9xbEZUN2FTdk9YTVJ0ekJPMEpnUlZZMWlaL2lkelNVakNSUkVVbVlrVlA1NktKSUNnSUNKQy9VSVRLcTJjazdqR2ZSeUI2TWRlK0liQURPakxNZTc2WGVOQ2tmdnIyZDJVRmVDVmdqdGdLb1NUTTlLdC9Rc3lqVmY4blBqTSsvNlFQUG1veDhHRG4vU1ZUVHA0My9rRTdvRU55SmRSUzV0dmZTZi9Db1BmdTQxTWp2YnBsU045TlJNdi9UVlYyUHYzaVhjK2NBR3BpMWxzMUZ1elJXYkRiQTFwMnkxTk1xbEFRUC96YlB4Zm0xR2xOTERlRHhCU3hnR0pORFJKcjlaUnFJenFxeFB2d2trdzdsaUs5dVRJV2RuRUtnM0F0cW53c0s4SWxVUXRvckpuMjdKRy8vQlh2bnBIN2dFMjVjcmFUdEZYU2MxOWk2NER4T0hWUm1ZRWVtTlRnclUxV3RSUk9BSlVDU0thcnp0czhjcSt0TUEwOFU5MUJFY0dIWVQ4T29IajBCQ1gvdEVDWUl0dHhTUVRSY1hnazA5R0ZaQlJhUUt0cjBJaVpRU3A2Zlo3bmpCVG4zTzMveFZjRzBQRGwzdnZmekVyeWRXQVgyNTVQRWIvdTRiZU9XcmI4SjBjeVlWS3owM3hZdWZ2MHV1ZmNZYTdueGdFL09PV0o5bGJzMVZ0aHFScllhWU5jcFpCNWwxa0hrV21YWEFMTHZyelVTcklxMFZFYUNsSWx0K0Y1bEU1MW1Oam9JczludEdGSnNLRlRCcUJrZ2E5RU5ZU3NBeEh0emM5TENzYUI4QnFjd2xMMzEwamwvL29RUDQrbGR2NTNUYW9hTm9WWU5aTlNHcFJUQytQRmpqenFRZzlUR1AyVlh6ck5FR3g1dFIzcHJpN1NMYm9vcDk2L3d6RW1RNVFxVnBPUTkzMk5IRitENzd2ZzhVMDFOL0FPQVl4dXNna20zM1VGV0NWQW1RZ0ZRTHArc05MNzNwK3NtenYrWmZXZVhNNFU5S2w1NUFGM3lvd20xSHV1cmFiM2xkYzlsci93OXl0VWw2WWFTYkhmYXVqZm1pejk3UGV4L1lGR1ZDMTlrYW5WYVZuWkp0QmxvQ1RTYTdMTklSZlY2WFRwdlFhUkVIY2hwY1hwOUlHMHhvbUE4MGIyb2tuZ0VzcStNa2UvM3FuVEVENlNQc1kvWjBXTExBc3ZyQU9uNzlSeTdERjc5a2hmTldaWG01SnNta0NsWVJtZ3FLNGlTdkZJalNLUThIb3BDaEQ0QmhqVTRZZ0QyL3lON2llVjBza2hmUlJJUWxFSUhHQ3J1Q0xsM1pJTDBlTTRvamZOWmwyRnFCbUdnRFZ3MUhpV1VOSUR5YWN4bmwrVWc3NmZJenZ1cWI2cTU3ZTNmc3lQLzl5WlJ4UFZFV01JRzM2dXFCRit6SFZhLzZLYTVjWFZmTmVnVVZxVnJpSlM4NXlEUHJjNW42SXZDdE5zUFNiSm5UeGtxc3R1YVVhUVBNTzNMV0tlYWR1ZDdPQ3c1YXorOW1WV1lsc3dvNmRiSlk2U3ZYR0p3V2tHa0VLeFgwOG1XU0RCZGRtQWVUclFXUDlJb1JqWUV6RzFMVmd2U0JMYno1SHgvQUY3OWtoVTBIVEtvVVZvaFZTbDdLUlNsQ3RjQkc3RFVkcktvRGs0dTlETGRibzBqN2t4NFhXSGtmQlpHUjltdW9ZckNsSnVWT0loYlBGK0t5NEl4WXlJS2lXZlRLeDhqYWhSSUt2QmdpN3Vvb05Wa0k1QlVjd21hV3VxVkxWQzU3K2IvZVB2bWNhL0htci9tRUxlRVRvWUNDUTRjRUl0SmMvdGYrZ3g3NG5IMHlPOW1pRnRHTkZzOTUzbDVNSmxVNmY2RmxvOFM4elppMWlsbEh3M2tkWk40Uzg0NDY3MndiaktZVE5DcGViQUJ6dHlRemszU2FrQUZrZUFFQ0FaV0VLQXlnNWQ3NjZoYU5KSUQ0d2tZVUF4RHUxWEwxdE1MaUNBa0pnSUswWEtINzhDYit4VGR2eDFlOGFqdm1MV1ZjUFNKL2FrdmVqT2hCZ0RJcmJ2YmNDUUNMejlIbnl0d0RxaFVUTWtZbEpSdnJVT3B3NVY3YTU3TW5tZnlMT1pkUUZudDlvSmNEWis2WXp3em5JTmJ1OFc3OE40SVIrQzFGck9SYWJHa1VVa3FZYldqZTlmemwyWE8rN1A4QldUa2VmTno4OGhPZ2dJY1NqaDdOYTgvNjZtK1FaLzcxVjNNK2JRV284aXhqeCs0SkxydDhHODZjbmFITFRETXZvNXAzUU5PcXpETmwzb0V6NC9xa1VjL3ZLcnlpeGZDZVZiaEFjbkI5RVNnZ0dkL25NcVhELy9CQ3luQ3JrVElieUNhR3U0QXc3NDcwNjNHckVaRWZuT01Weng3anU3OTVENXFHR05YaEIrM2pxcUNxZ2xCeE15cjBiWWpnUmRRcFFWSWFyR2NUaXFURk1VcDluQnRCT1ZEVXhwYkp4ZTR5bEtGeWhib2x1S0dVb3YwYy9ONzNEVDRGRTRMWUdkN0orVVlBZ3VRZUhzbnVMMGtjRThZSEFiRFNwbXZ6WlRlOWZQeU0xLzk5YzhHSEhyZGUvWGtWVUlEcnVCM1lQYi9zbGYraXJTOVhOSnVKa2lCTmxtZGR2UXRiMDViVGhybzVWODZiRE52d1J6SFA1THhSek51TXBsVzBxbUNtcUxISFVGWERId3FJRWptVHFwbHFaay9JRERCYjRXWE80Yk5NSTl5VmxHaTBWSlRhQ25VV1YrU29VZFZYcFB0WWFVZGhSOVVPazFOei91UjM3Mk9Dc0s1dGRFVThxTFVWUW9LVVVLWGswRkdDcTRhSWlOcytLbWxjc0VnNDJJdEVTUUNKdzJDNVZPS3pVSHBtbDN2eXhONEd2WnpiTVVnUCtRSkllR0VNbzBEUmFzMmNkU252STRJd241bVJJSTlwWDhKa2RZdnBQM2xXNTdSVHVmOExmbWpiK1BuUEJXL1Z4K3VLLzN3S2VPUGhDamlpMHhkOHgvZDIrei8vRWs0dnFOUlY0clRCSlpkc3c5SlNKWnViclRTZFl0NHFacjZHbyttVTgxWmw3cnRQdFVxMEdkS3BzbE5sbTVXZFc4R09SS2ZxZW1pS0dadiswSU9USHU2WUVoVThUU1hWYWxsc2o0c3lPRWE0Q01LTm9ZUXdyci9WSklIM05QaWVyOXd1ejd0NlNkb09TRW1pVGlHSkt4U0FXQXNTTHRqdDhpQ1VVS3QyVGh4WU9iazRKeXdXaDBDUnhMb2hpUFZKcmhvUmF4VzdibCtzS2l3YXNwaDRRbFBLOG1IMnE3QlNDYm9XbThGRjdTNzMxWWpLdmN4VklzS21XY1haT3ZPQnY3SThmOTRYL1V1STBGM3h4NzMrSEFwNE9PRzJXL0o0NytjK094LzRndS9VTG1YUlJwakpVVjNKd1FQYnVMSFpzYU40aFllbDB3em5pVmpFQzdZRVdvVjBDbTFwbFMxWnJlU3QwMkFFYlA1bFc0bUk0UThMaGpOeDBZc3RMWEhSZXpMNjRvdGlIclM0cDdoaytFYWV0N0s3eXZqdWI5Z05FcWhTM0FsREE5VHpIK29XMFFHVlEwZ1RzdSthcWlBU2t2MnRBMjN5Wm9nSEtLb3BXQnQ3VG9sQ0VjRnljYzBBVUNWRUJXQzRaamRuanRXY2RocjJFbUNLMXBadURYbXBDSWVpYWlPZUdZRzNBVndVdTV4WWFUTnY5Y0NOWHo3ZTk0clh1aXV1SGwxL1hEWWY3d09QZWgwR0FLRSs2elgvakR1ZnY0SjJrNmhTd3J6RC9uM2JBSVhNTzBYYlpXazZvczJVTmlzYlQ3RzFDclJLR1ZnNnlSbmlOWHlpU29sZ2d3d1BZc1VwWnN4NmdVWW9VYnBVZkpnRVd0S0N1ME9jWllZUE5OaCtwQnFMOE1FRzMzampDdmJ1ckpuVmdvTDRqUHF1R1FpWFQxS2gwaTg3Z2xGQWFyeWVBa0JTUUlUcVFRaTgvakRhNGVHeldiN2trOFVZb2I3Y01LR3NwaXR4QitETDQ2cmU5WllZcXhUbEwvNklXQWJJRW9LaGJpWUZHWnBBNlRFZ1lETWgrVHlXRlB0Zjl3cmViaWFkWEVxNS9BdVBBRmpENFZ1TFgzKzA2NU5Ud0VPSEtoeTVoVXVYdnVLbHV1dUcxekhuTElLS3JXSXlTYkpqeDVqVGVXUDdzR1JobThsV3liWlR0TmwvV3BVdUUxa1Z1WFBYcWlRTjFSc0d6QVkzVkxOdGI2QzJlbHdJVHowNTFndHJRc0tTNTdCS1VpaEJsUUtpREI5S2ovMzgzNFhJbDhna3BWRisvWmZ1TWlFbEh4Q1BWQ094RHdpWVBHUkZzbXlGbFF6YUZocUNpRnFoVnN3cW9vWWdGN1RGNFprcUJObmw0QlNNRXlLaGI2UnR6MUhXcEFCQTIxSnM2VEJqcGIyN1MrTUlpcjh3NUdyVkdnV0hzbTlFa1EyZGVIVXNvemx3c2xmbkR2QmtOQ0puQUpJNG43YmRKVGMrZjN6TjMvNE9IQkhGb2NjT1NDNTZrNCtwcmVXNjdsWUtoUG1xbTQ3b3Rpc0Y3YVlsN0RPd2M5ZXFCUmtkWVpaUHBWVkttNEV1QTYxWDczWktaZ1Z5dGdIUEduU0xNbnZSVUFaTFpVcy9XQnpzSDFEMHBoU01GcjhzQ0lFR2p4RWZESjlITHdNWnVDR2lxa0NleS9pcjE0N3gyYzllUnM1R2ZaVG5ET2dORlZDaUZDZjUvUVNRSk03L0dYRnVDOFcxbUxvb0ZJaDRkQ2gwcDQ4a0s5bDFpcTZGZXdkSzd0UzJCODYyd1VHMlpRandoWHAybFRUeEFDeHc4Rjd3ZTMwQmVOUkNSSWd6VkFOdnN4ak9FL2FmdTFoWENwL1VWbG1XTk8rNzRYdFhWdlpkWXRzRlAzcEFjdEViajJQbDA2RkRGWTZJcmx6ejEyN1NmUzkrQmRxbUExZ3hLMGRMTlVhVG1qT3I2ek1DT1Z0Wldac1JPVjEzdjVhL3pVeStjaTJ3WGdKVlNpQXhTRnNLU3dNNVhLTGg4dmFvTFRCUStkc2pTSEhuVW54WHNQb1lLaXVrRnVCQ2gxZTlhRVhxRktzcU1IVGFvWWswZ0dQKzJUeTBFRkRib3NqK1U3ZzlFUXRCZk4xVHVSSGRQblhaaW5icUtrbGRDZXNxb2E2VDFDT2dyZ1JWSlJqVlNVWlZRbFVKNjBxd05FbW9rbUJ0emJkQ1FESjNHWjZYMHY4OXdCZ3UzQjdWTWtMZ0FTenBrV2NQYjRZbFBIRE9NNUxkZ1hwRmtqUWJtYnRmdUxPOS9EWGZDWUM0OGRFOTdTZWVpcnZ1VmdLQzl1QVhIdGFseXlwc2JYU1FTcEFWeTJzamRsbXQzSWtDcG1SVks5NkgxdXYzZk5oOERUWVpaUnZGTUJBa1ZVcHVQVUJ3NGExWVhoeGMvWHZ4dVZBd1JndEVGZ3RKK3B2SElHUUJvQ3AvN2ZQWDRuM0tnaWtwZzFUc1FRS29IaHdNSXA4ZXVNT216QUx0NXByWmRjWXRwbG9BUUk3ZFA4Zld1VTVPYlNqUGJ3TE5QTnRXSTYyU2hLUktVZFdWSkJJVkUzYnNFUHpwaDJmQThzZ1dLb2w2ZjBxYjNjcWxtTTNXWWxFQms2ZmwyUGZQQ0drcGZVMFVaQzhXaktERlYwMFZzVVBzbmhITDVLNG1WeWk3bnZ2M3NIekZUK0syV3g0QzhJZ1ZNNStZQWg2NnRjSVIwY2x6dnZLbWJzZjFMMmZiZENBcklDTlZDWFV0eUsydDk0ZEF0TXRKSmRGWFFVTFZkaG93RWxsc0s0cWtVQ2FGYmVibnV6VkdRYm1qZjQwb2xzYkNHNDlsczA1VE1XaGd2c2dGcVNVNE5NS000cnNIQ28weU1DSUNiblk0c0ExODVoVmpBQlJaS0QrT0piS21hRkV5cmRiTXdHMFlHR3F4d0NPTWtFbkdpNXdGcWh6VlNSNThjTTZmK1pYVDhxNzNYdUQvT05aaTFrRlF3WEtOUVhua1hHNEFnZFdlSlFHUWdVa2xPTEFFN2FMaTBIUGJMSUp4dkt4d3BRc1FEUGpHQ29odDRrSnBJMFdudlVtQTBFL1hpVW9IbUtVUk43NnFSSWFOMld4RHNlZHpkMHl1ZXUxM3pUOG8vd2lIYmszbEFNZFBXZ0d2TzBRQTFGMHYvaTVkdVRSaHZ0RWhWVFUwbzE0YVI0eFFTdWlzRzBaYlVZU1pZS1lXTTZBVkRDTzdTTndndWcyenhlQUxBREFvaFpoMXNUK0Z5U3pJMFVHV1FNUjVzSktad0xEVWVXQ3NJRmFxbWJjb24zdlpFdmF0MVd3elVWY0xmaWNzc29RK2l4dFZWVVdLRW1jenFFVkJVNEdQZmkvdlhRUElqLzdrZy95M2J6NlBrd0N4cXdhdXFnVXB1YWYyVUZ2RWxLZXNPZktXUkJ6aFc3d1dKQ2VNb29HTDNHdDVMWG9TZlRmbEs3SUUrc04yQ0lodGFkT0hRZ09RV1FoMWQ4WGxjMW15VnF6M1hQMU5hMnRyUDdwKzlHdE85dzNvcjA4Z0NqNmNjRVIwMjFVdmY1N3VmdTZYc05NT2tHUllLN0ZLRmJ0T05SUHNiTVdhZGlxMHNubVJUUFBNVnJGc3V3elFBd3pHenJRTDNrRFJaeXRDRm9VU1lCbnBJWW9YaUJYVGUzaXNSV2crVXVycHBjaVFGR2tEdEcyWk1RTjJyRll4TFBnWXJPMy9LV2dxQ2hCU0d0Q1JwcGhRWWRrVEVLNzM1Z1p3ZmwzbHk3N2pYdnlUVzgvajVOVXJVbDI3aW1wSGJSeFBTM0FPNkp6UU9VUm4vbThMMFRtaFUwSm5DcDBEYkVpMjhNUmJ6Qlh4QUN1NkYrTXUxcXVGVUlyd3dJUjl2WmgzMWFwMmZCQmtDQ2tXbEdnSURFdEhwUkowczFhM1hYK2cyL3ZsZndPZ0p5NFdyOGV2Z001c3p3Kzg0dS9veXRVVGRGdm0yRlNSS3FQVlk5UHZUbFZ5MXBTcFVLdUtJMG14YlZUQ0ZFa3AvdlM1SEVSdWVDc3U5ZzFSTG1wVG1VQ2tORjNRYm5hZHZJaFp2ekRuUXVidTFtS1F3aXFJQ0xyTWZWZU1ZZ1FvZ28vQkxTWmpNVjlxQnlWQm9SSzVlek4zRkNlL2ZXVGhxL0VFSFlEWC9NTjc4WHNmVkl5djN5YlNLdkxVSWx3UEdnUXBpVmNrT0o4cy9VK0MyVlZ4aTJpd3cxbm5vYldLUUNLVUw0Zzg4V0pUU2M3anBXSXhpNXhZdm03eWlnTFpoZG5YeTVmeGViY3doQ0IzVlpZVjVOMHYvQmJndWpIZWVzdkhsR285WGdVVUhEMmtBTmE2cGF0Zncxd1RWS1BzYlhFOXFKazU2SUpNRWhrZXpub1ZsRUpzTTF6WEkxKzV5d0hlSUgySnBIY2dyS0F2OXU0Sk0zK3Q1T09DbW1XVXc3aG1SLzRTZzNJakFIRE9wMWcvUXprS0FFMG5MNzFtWk1SWmJLa1Nud3BrMTZ0OFNiNEJWcWRuemxrWi9peUowT3Vqa0R1aXJvUS8vZ3NQNDYxL3RvN1J0VFdhQzYxUHFZSDVLUVBzbGp6Q2NPUGVCaE9DTElXa3hoR0V5K2pkU01tYm1HQTlySmZ5ZDVIZFVEZTh0S2pJTGZmOXA3SnZJRXBHSng0SGlIRkVtaUZrWXRlMFdMdm1oZVBMcnYwaVU5QWJGMkRmNDFQQVE0Y1NJRng5M3F1L3BOcDErUlZvcDUwUm40bndBZ2t2aDZjWEJZanh4a0pWSmxVbTQ0UE5CVm1pTEFJTkZuMk1KRm81UlpVc2ZZZHZPVkdFaVlFY3l1VFZ4YjhsQnMxbnArK1c3NjRvMWo1Z2NCY0E1TUVkbFFPYUFhTVl5U21nRUNrRGxnY1JGSm1xOUtWZllZMnpBcU94NEw0VERYNzRUV2RSWGJ2TWJwcFJyQndHUCtFU3kydGx4NWplNGxFOFB6c1VnbWx5QVFyaUdGcWtmTFBJRWh4UTNQR3NvVXhMejh4cXF0V0c5Wk15OHMyQkZkR0xNUFFiQVBJY3VuUVFzdWVGWHdlQU9QeldCWS95K0JUd3Vsc0pJT1Vkei8xYk90bFA1TVpOT1d5T3NCRG45TDMwM0VEWmEyYlkxQTFZR0xveVdTOGlIOFZ3V2ovMmZkOFdySmhmSEVRaVpTTVhEZmNxYmgxY0NkSC9YclMwdDc1aGNTZTE0Y3pVaTdUWGtYaXNLMkh3bDBnR0x1a29JVDRhVmN4VU02aHYvdi9PeTRXT0t1T3FSd29jS0U3ZjdZSFBEZmVYb21wd1VKeWZITk80dGFjdE9uVWw4NmdncUJrTWNSd2cydmVvM0RFOENVTFdqaXFHdytIdExjc2JodU1oaUFJUG4wWXBrOVRsZzErMGlnUDcvU0RKY3FmSG80QUpSMFNYbG5CNVhyMzJaZHJNNEFBQ1VDMUM4Z09HYkxta3JRQUhMTWlnc214OEZsYkIvdXNtM1NuTjNzTFpkeU5seHFJbmZWV1NTVFBBZEVRdTdHOWVIQ1k0U0o0T3RHaW9nd0hiWElCVlZjUWl1aWplSXY4ZW0xT1NhYUhWMTVReEF5aGlTbXdSdHdEUTMzajdPbVhIeUZ6SFFpVk9yK3Q5eXdaNTdUNnF4VkFTcmlRQ1ZNUFpTcURRVCtpelBiM2g4b3hINFpYNmpETmNxUmtkbFNKWUJjQVVXdGRUWVVQTENZaFhMSUpRSTJtNmFlYmFNL2ZsSzc3NFMreW1mWHJ1NHl2Z29WdXRtOC80NWxkMTI1NjlnbVp1WkJzN0FTd3BIN1Y0R25WMkxpZzdqaWlMUlBsVVNjMHJRQU12QmhXVmlPV0RROXhuRGkvd1d6eklYYW4wcysvaTdMd0NaV3VFcUdaUzBPdm1neU5pY2RNTFpuWGdsOFEyWm9zN3h4QzZPYUNJUkFqY1U5dytIOTM4dUxVWHBnUzBXZVg4ZVNVbkZZeXoxTUdkL2ZjZ3BBU0lwZkw5RkhBVkR4bEx0TDhQOTEwalVoRmdJWlZqTXFzTXVoRkdRQWJJTnRvVWc5aTNVVHg2S1N2dFFubkZrWUkvd3lhUHUwSUl1c3hjYldQZXR2dDFBSWhEaDRwNmZYd0ZQUEVCQVFTNmN2QkxVZThFMkxsRkVyWHpBb1QwM0hrL3U3eG5uaElsSVZEeG91RmVsaGdDYWdzdVhKTzlZOUdmM3I2bG50ZUt2Z0xGRlpUSkdnTURLWll6cWJzcXNFL3F1ZEJqOGE1L05wVXpCM3VMT2JDZGhld1lncG5Zd3M4K1d6WWF0UEliV2pCeXo1bFc3cnd3RjR5c2xtTHc3ZWl2RlBkVzZLZm9mMWc2TENabUNKU3Q4YzFkOTgyVUFSYUl6d2VwM0F2T2xmN2lEeUtlWi9kbGdLMWhjTUxDZUxuSmp4bllENkFwUnFKbXlQWm5mZDRLY05BWEx0bmN4V05mZ3R1T2REdkFuVnk3OHFWc1p3QUxOTExxekhCeDZ2WEVhb1l2WEpGM1J5ZzYyS01rQUIwdkdtRUp6SVlpcE5MblVCWjRNV1hCTmIzSkRaZFFySWg5ZU9BZWt1YzVVZlJJaWt5bHlIcXdZZzE5SHdZdk1ZWlAvTGhNWlBYWlpXQUN0aktTSEg3eDVObk1DMXUyeXBGQkJRWFBIWk9zdCtURCtyeStNVVhwUW55REIzaHNFZHhYd1lmaGh1MWZqNUdLUC9iWkdBbGpiMEM1ZDU5TWg4K3RRdVVNeEdiM1ZwUUpqcklXaGdRVDJrN3o4dVY3ZWNVWHZkZ2FhMjc0NHlpZ2ZXajZuSy83SWwyNTRpQnltd0ZXbHNsRjMxYXIvSEM3WXVGWjZBMmozRW5CNGw2TE14dU01dERVUTFCY1VocTRoamdaVUNNQ2cwbVdBMFZtQ01yQmR3bHh3cTFJNzI0R1l6MVFVcStoTWp1dUJYU1ZHL2lZbUhVVDIzVVZ5ZFpia0lNdGJjWFNKTVdrNTRZV1RaWmlnV0hUVUtaMTZjc3dNMFovcjFodWx4RkRFbTd4N0VnbHR3QytlSHJRY3BkUjlOQ2VGZmxSK3NSbmxNUVUvUXlyNlIvU2dieUtuZzM3MDMvZnVWL1JCaGp0WmJkNjNaY0RBRzY4N25GWVFQOVEzbjNORjNGeUNhR05XdDFBU0NGb2o2QkZCbTZqMzRuSnJaa08wallLK0pKSlYwb09ZQWY3WEdPeHJzYktBYkJrWHhGVy83eFEyZ2o2UWdtcElSd3VWTllOUVk5UEJGZmpQaXFYb2IvclZjSUFWTlJlaWZQRUhEaWdVbW95MEJFL2NGQ2pYSUVMZm4xd2hjazFVQjNCZ1ZtWXZ0TmxJaS84YnZWWjlQNUdNMk55bXhFSVE2Qk91cUxjSkxic04xbVo0RFFQWksybVhKN1Q2cVBna0pqMDkwS3dEZEdXREtxSVRQYStFRUNOMjh4ZlBaWUNDbTc3d1E3QUVsWXUveXZzT2dFMXVTWHlvTGJxRlNlRVpoTXROQ0xrMG90Wnkrd0plOU5Ic1FvdFp5VUVMeHZ3UkZ6TEN2QnlIRjN5NHA1OUhnUno2T1hpVWFoS1QrNnlqN2pkQzRrVFJrRXc5YlRYUlpjdmoyVDhYeWpRVkpoazJ1WlpJa0tXU21yWTJxbGVEc01HTHR6YlpCT1FwT0RoSW9pK3pRTUJ1MVhzRzVzZ3ZnOFJQL1l4d1FNeUtCWlg2ckI0SG13b3hIRytOYUlZRytreDFtQWVSSDFUd1JiQkNXajIvRTFETE8rL1lZeTFaMXBsek9ISFVrQzc4OUtsMSs5alduNHV0QVZpcDI0enUxSndRUURvS0VndnRNZ0NHUE5HcHFFQWZaYkdlN1FVRkFoYkZUSnNESHM5SnRuYjd1RXNHeGduRFB3OE5TWkcyTGVCRHlRSzZVb0NTQklhbUNRU0F2RHYrTlBMR0FOV0FDMVVMMzFXc096WmE0a2lEY2JVRGVyZ1ZrUERZYzB4V1lXRlhHaXpEeXk5anlud2IrZ0gzQ3I1aDIyck1EcGZ1MGlkQnlpTS94VklFbDBiVnNZTVh1N3hIa3R2U21rdDRheW5vK0NDSWVIMS82Qm1jdWxnUGI3Mk5TOEVBQnk2L2pFVThORFJCQUROenB0ZXh2SEJNWExuOUlzR1p3Y3c5OWlobUtxb0pvMzMzZW9VdDFuY3J2am1pUDU3cE45aUJnMEdxTkNJYmc3RWxUMTRXdnBuakdHUUVwa1ZLRElBckZpSTFvdTgzZXFhT0UzZzlOVHlndjBJakZmTWtWdERvYVdGeE90amZBbXVKQ1Q0YkJFUG9LUnNkeEZpNjJWb3lzVUErLzY2bHRFUExCWnd4dVJpMi8xRnB3U1FGS3JSVDdDaDJaVHdPQ2phWDRLeHVJVXV1bStRbnVNZHBFYjl3d3RMRVh2SEVMTDJiUXNoN1Z4WmJVZUQ3YThFQUp6NHdHTW80TEhmczNUQTB1NFhzTm9sMEp4alJZcTNod3VLaFNLUXZrRTlkR2R4bDMxam5YSWh5cDRhcW9ENlFwRFFrS0swTVRqK3pGSkdIWjhCSEhUN09IbzdJbFdoNWZPUk1SZ0lTajBqV1BCUkVZTU1vWVNQcFF3VHQxN2kzaGN0V0N3U3FvNStwR21uWElZUzlYbkEzdEpMcnpUaEhvZVppN0p3R2IwTEYvUnp2N1RUKzFBQ0ZpMjZiV1N0MG1zbnlqU0REaFJuQVhjTUprSlVQWk1SbUF4ZUQ4T3FmUi85WmRzaTJaYVlxUkk2UG5nRkFPQ21XL1RSRmZETC8yL2phclpmOG16dlZGcVlnUTdNK2dERW53UXdKbXNSRm1CNXh6THJlNSs3WU1aaUVQdE5Lb29XQUZEZjJ0VHVlekhuNXdGRTc2NkcySWU5VUl1MUhsZ0llMFlxbXVhUjlJSVpYb1JmNFNTUmtwYnQxd1FPT215ZkEycm9aT2dadmRPQjdjcGRRbDYrQ05VVWIvaEVIMmdSQ0dJRlBJYlNSVG5rSmxwWFZEZWUyZytiS1kyQUV2dFVPZ1BhdTVPWWNIMnI0NGxrc2FnQ0xOQmMwYWVleHVxeHJIMitFblRnMHRvTkFQYmhpRHlxQWdwK3NGSUF5MmlibC9nc1NmNFFManlrSjdSQ1VJRzNCalBRTVVjTVpDaUVVSHBMNU9Bb2xYcS9Qa0lySWlDY1p1aWxYL0FTaCtvKzZJbjA3OXZ3eVNLK2lSOS92NVJZQlVnYXpnVC9TdWtKb1pxb3FyNHFncVVDMm9iRzlyR0o5b2luZFljdzBONG9sc2YrazBwN0JvMlVzR0tEQmtub1IwelI4RkJhREVXZjVuUzVoOHVGSUNxY0E5Ykl4OXlUQmJLd2ZBL0Y1SnIzNlB1d3lCOUkrUlVDK2w1SXFrcXBsdzlNc0dQTlpQU0kxMkZ6WDllOGJBK3IxVjNvcHFab1d2Z2hkNmx4a0M1UjhFRlp4QnV1VE9FbjhhSjN2eTdUeU5xVUZ5SElzWXd5QXBtaCtXQlkxb2pJRmwrTEc3c2Q3dCtUSHJzb01NQTE4ZDMrQnhSZmwydGU2YUpkN1h1enBKSHNzUlZKQ1dHOFloWGR4WG9teWRjTWxjZzkyakpnbldCK0lFWU5oZk1NQ3huN01SUnJHZmZCQUQ4V2RScGtWT0puVU1NVFNoODdPNVVQRnRBODhCTEJUZ1FKWGVUVzIxQ05lN21iRDEwUUVGbTlGamdUbzkzZ2xWLzlPWSttZ0lKRHR3c0FqTWM3YmtoTGU1ZVFXMTFRQnZ1ZEM3OExDbmt3MUlJZUkrcEZ2dyt3VmcvRXZlR0szazM3SUpVQUpiQ0hEMlQ4Z0wwUXltRy9nVkVMVm1TUjc4ZFl6R2diV1hBZEVNNHVSaUc2YSt2ZUxOd2dQTm9OdzFMd1hleG01VityYXJNU1F6cXY5TFBncTBGZU55UmFiS3EvR21sREVBUEk0UzBOSlMwWWpRaE95OFRyeHNCbjRVTDk1RkJaaWY2UDhvc2dFZ3ZsMlFNbEhYand3WXoyY2ZNY05MTnl0SVkyei84SzhNZ0tTQnpiNWFSQ2ZqYkd1d1NNekdWWURwVWlPUzVxNWFEV0ROQkJiVnFZY0pjellnL2pJUVpjdUZjOHlyNiswRmtKYlJ0MGZLancwWmJvdlBGNy9veVlDQ0VZb0xjWWpMck1jR1hESXBIaVlJYkpDc0pyRWhRUUtkcmdIeFFPdm9BQ3dDU3c3RkN4aHBZNDRBY0hiWXlsWFhBbEtwL3R2WjMyalJxMG9aL0p5UkNreVNOU25pNDBHZHd2SHJyd08yRGtOSUdlQkJ0bzdNRGFtaWpkQXJ0MFl3QkZtYVJHUGQ2eEhYaTBSVW5iRGhJQWRQbmdjdVIxU3k5alFnMnNOR0tnNWVLT3F4OEpYanByMzAvUkhpZEtZN2hKS1d0Ym9RUXE2WE9lL2FOUXRFNzZrU01rVlhDUGFUZjBYVzdkTnZ0SVNUelIybEI2SVY0Qm53Q3AraTZVSGc3K1pYRkN0RVVmQ1V5U29GQXZpZ2xJRmpPdmdFRFVkWkkwQWpRSkZ2UFVneWNwbklSMFYxY3N0QURCQm9ER0ZTdjcxWEN4MXFYTXdRRTJGNWRWUHo5RDl0S1AwYUF0OFRxQ0RBMGpJWVAwS0h2RElwN1pNdGtQK2t6RVlpVXFqWmNDZ2VYOWx6MjZBdTYvM3BxeGROMmw0QWpRTGF2Y1pZZ2ZkbkpwakY5WlcrR0tGYUc0d0I1dXh3blpERTJWNlpZZElXV0xBaW1wRUtZa2JEdHhMQXljV2Yrd1pPaHIwUWlQZkJXNm9mMW5FNlJYT3ZaV0k0bEowT2FIRk5kRlpSNkw0RnlEYm1ZRFNrSlNHcG9ZQUZEYnhpMzVlS2ZrL2JLK2VnUUFrV1FkTUFOTWc3Y1ozZG1PbUV4dFlVaE1iaHRiWXhpTU1yUmRZcktkYUdjbDUxRUdoVVhGWEVyQUtnVmRHaWlZSzFEeEFzQmdsM1NmTzFTSDh6YU9xdjFuQ1NtVmtNRm9GTXNEK0U3d1V1WS9PTUN4akI4aWF1Wm9TL3FZTTZoSldCRlNiM3MyZ05WSFVFQUszbHlaSTJKNmdYTU1UdnBMYUppRk5RSlRIQ1U5YlJEenhoWEY4VkIwQ3U1NndxcjExU0FEd0NwVzZ5ZURBc3NpVk1KWGFwbGtxR2F5RktnM0ZKOTl5UmlyOEVPa3F6QVlZQzBNVzRxRWhKa0NtZ1dKQXFsQXFZQVJhdFFqUVg0ZVpXVVpnOW03a0FzMjFVcHVrdHk5dUxwUVlzdFM5dzV1ZjR1LzJyYzZ4amU4YkEzMU9MSHJiTTIzencrVFhSVXpVQW1ON2R6S3p0SE9SWWhLbDVObTVYaFU0OTBYV3J6Ny9pbGt0UUpMa1JPZHJhQlhDQlhYSFA5QkdZUitJc2VnR082Z2Z5OFV2WlRGMGI0UWk1Ujg4bUlRcy9kZXFmd3JNVjZTQkZCQWtRNEEyUFlJQ3VpdUFhaUZ6VUVPOFZjMGdqSEQwa0NUSk55Q0taOVVnVVZzVHpMNitKT0FER2ZyWUFGcVFLdFlvK3BlMmswOGJUVXRBMnNRRUtscUlKOXM4QVhYci9JdC8rcFo1Y3ZzZllFRzErQVBZNGR5L2c4cTlQUlZWVWJIRW1waXh6a01KWXYrM3BTRjdxc0pkK2hOdzNaR2hmVzFsMDM0OHovNnpMZ2xzWWpCMlgvell3YUVqL0MzQU9BUC9lY1Q4dTUvdjhsNmgyMTdNbXhvc2Y3bFc4VU5FMEtEUjFCNEtaMHNOS1BVL3hVY0hlTUpsRzBRRUlheGh3V2w0RFVNeTBWWWtCUWlJMVZMZFFhcVIxMllmaDJRUHVpTHpnTkcybzNVaGlyMkp3djhVSGFhOTk3YVJKRVNhRVRsaFZUUlFhQWl2SW9ycEJaNHpXWVBLM0hnTUlpMEZnWUNJWVNkMjRRakl0bEdrcFNFc2gwUVNEQ0pIY3luc09MMXFvZTBKUkdjYlpNTnBEcEpHQUs1cUxDQzR0dmxCaEFVS3BYR0x3NGpBaUtDRXRBblJWWkkxMlg0UkpLcXA3N2RBSVdOTVo5Y2lRelZwK2dIWVZ0NmpFY2lKKy9iQk1heG8zM01QYmRTeFNxVnUwZ1pHL2JQUSt6REJhQjRMR3VDUXhTQnJVZlJRU3Y4SGdWK0ZUelJGM29VVk9udHN2b1hxeGVYVVRXWnZIanBrVnd3Z0lUYkFSR01FdGlXVVN4R2hhSzlwcWZBVUVDQVVTU2lERDhFTktiV2xDMTJSb2hzUUZqQ1B0UHRiZkMvMFhkY2ZDejdYdHZITTlIT2MvSXNhT2lyK0FGYlZyNUtzcEpFRlR0b3lHdUlJMDRoUWFRS0V1dDQvRHp6aTYxUkVaSk5QeEVJSlNYN25RajM2M0YzNEFzVndwQWNKdVBLcXF3OCtJOEhXRnRZMWxFNkdLVFQ4Z3VNT0FHcEVpbVY0TU9uR3NHb3N0bFdTdFhjVS9TbFFsakFnVXFCd01kUUFpLzJ4QUlLSmtTdmxFT002UGNZWm5wNlRsZEs4VWZoSnJNWWU1UEJxa2JTRGlKVlZlKzRidHNqRTlFRTlnQmpxZXE2VDhzT2FJRElIZEpMZFlweUJrQU9kWEhUalNDV3VmaVFraGVOWnl3U1NLVno5dDRBTEE4NkhrQ3ZZOStTb2RxRUwwYUtCZDVSMlRDSVYreDlNY1d5T2lybitTNjJ1VVZsaWxkSUZ1eEx2OFRNcUJrZkRRSzJlRG9LVlJ5d3hIcVN3YzBKaWIwREdkejJ3Z2Y4M2dCUnBTUWIwNHgzZkhnS0xOZlVjdmlic0dRbDRvR2hOSU9QdUdTR3VXOTNvV2JXd3phaWhMbnh5bUM4eXRzaFpFSC92U0FxZmUyb2l6MmlNcVM2eWl2MXprZkpoQkN6L2Z0WHdGVDNabGVzUXdzUDlFcUkyS2k1SjdPa3h3dmx5MmFLaCszdkpSLy9oZFVjTXNyeWUrRzVZWW5SNjV2SzhBNUY5L3dYVndCZlBKUW8wRUNVVXA3ckJ0Q3QyUEJzajk3NUZLSDAveWt2QyttNnJUSGdoUThUdTZORnl2Q1ZxSDBrdVNnRUh5V2daT0lHZzQ2Qk9IM1FCZENOTE0yR0FwTUVPNndhZ2tXOVRuM1QyYmZQZXBBdzVISUxoMGVnbEZVNXhMSjEzTEZtUjRZd295OThBQVlCSjZQVi9UUVBOYkFraEtSVWFjTGFveFlqYkk0UDFtWGxXY3dDRXYxdUE3RW5ocytPeU5MMUhmR0l1TmpzL3MzUzRPSmxCMjdXWjlGUTZWemJ2UTlTckthVXR4aUUwb0x4Y3pGYm9ZQ1FxVkJIZmN0RGpoYlBlaE1rZHJNdkgwWGY0Z0hhSVNpazl1ZTZsVDdRNDkvZTV2UWJqc2REaHZKbS83SUFhZmhaZjI1dmpZTlJPbmE2d1ZRVXFNVE1Yc2d5eE1hRlc3Z29nKytqeVRMeUFIM1MzSnBUMUtaZ0JTbmp0WmkyOCtFZGRLWXdJQ3dhQUFlNVJnMmJybFJNeTQrZ2dMY0lBQ3hwbDFoTnFuNzYwWE8vWlJKNUtaWFpWREFMaGp0TEdwVmduL1ZqYkFlWkZCZFErZGR6bUpUSXNRNEVHUGpFWnlYY1VRNnNvMUxxMUN0QTJHZzZiakdxemdkUmlvakxxQ2NLcmF4ME1INmVXaHRZUXd6dVh6SzlDUW45RnIweFVVdFpEaExFUTB6cDI4QmVKOG80MGlMMHlLYVF3eVFNeTNQWkR6L2Y4OUV0dGpORlpUTDNMT0RRbXJrdEdxN0ZLUlhudEFQZnk2MkgvM29xVDkzRHNTeDBkK0dVeEMrS040eHhIejZMZzdHR0F0cjZZUG9wRVRLckg4VUNDbWF6OHlvY2JDVmZTTzRZK0dBcVdkeEV0SDBnZ0FLM1BGd3YrcmNBZUdQaWxmemxFQUI3UjN4Y2VuQWR6dGIvR1c1RDVZMUtLVUVSeHpSUWJJTkM5cjQzMnB3QXBCVFF4eFhIVi9mMHptTkJRSU13Q0hIOFFnUjl2bFl3dkVhUlRBSWRYQUxGb3Z2TlpTREZTTUxKVUtZb2lMTjQrQlAzemdVcE1jVkdSV0FrQlJoT0hWd1VKY3E2RHVtRkY0b3psT2VnYlc0eEI4WkhJa1hJOHZsUWhjQ1hjWkpLTUJ2UmpwS1dJNUQ1U0FXcFIrek5hcDZSVlFkQmhUY3VMSnB2OHc4ZDRnZGVoUDFDL1Z3WVd0cFU5S2VNYkNuTDhpNFBoRmRvbVVGcWFlQXRVQ1hrUVZjQ3FCSys2TVdkRjZXbkNHVndHNlBDVU1LQ2tOMndJNE43OTY5TFNWVlROYlpuaXdOSXZFKytPNEt2SEdURU5ld1ZzN1NqTCtQcXlWRWREdkpGVjdQWkFFdUNmaHhnOHJQZUU3RzJvTFMzUkFrRW1JeXhpSW1OT044c3VpbGx3dmR3MUx0ZmhxaGZveU1oeU9FUStiZ0ZKMW5NV1Zrck9IdFVDNGlUSjZmUXR1MVJUMGhrb083RnVvbVVNdTBJQmhscGpxaWV3RURoWEJuRGhRZmlDQUVOSXZ3Q0g0ZWdOc3FIeXN0RW5DNXE2S09ZRHBocnRFM3hmQ0wzdHNiYlkwdzFpOHNWWDlEdC9QdEZRKzl6WTJBdCs0cUpRWnRodGhBaGNET0c0ZUllVWVTV0lRaVpscGs0S0UwMUMxbzUrbjdIM1ROZ3FSSXRiTXZBNmczckloZ1BHQ2dPQVBSTW1mUkNDdzhXUUtIQUhmcVk5MzBZdXRyaDVnd3NYakVVM0NlRS9XcEZrNWtDbm5zRUJXVDRnNW5JUnFibGJrMDB6UDFuRnNxbVNsMTh6M3lYZWpEMHVLVGdPamYzRXZqQ0c2ZUViWFJjeXBsNnR4SFZJNzBySDB3S08ra0dRRThyd3ZnSU5RckVsa0pGK2RTQ21nQmVXMlhERkhPZzRNVkZTMldHRkhSbTNWZGpwb2czTGRqdy9ZUTlDak96SzI0SWJPZnlpLzBpUUNJbElNVjg5YnIrb2YveCt6Q0pvRkhpamxNdFVJdnRDRXBHSUdkZ3llVEZYZ0hVWkt2bGIvWXowUCtPclVJaUJUZFVzbGdrWnVPaC9ldGx5dlF3S2RCckdBNldtaTlVVktZRWlMWTV6YzV2UG9JQ21yeHZBRlR5UE50cDJURmM0YjRaeHRnNk1ZQVRpT0NrLzB4ZjZsUTZXNTRseGJvV2ZEZDRWdndlZnhJc2doTFFUN0h4WmcwOWxUWEV6blZNcnNJcWZxaE1BZmZGSHZrQk1xNjZGSlhZL29HRDVSRkZYekh3czZHNHh1S1UrZ3N4RzlrNzQ3NjR4SW9KTDVaMytkY2lFWGN4QzdwUFJER0pBSGtyWS9OY0Mwd1NtUWtVU3VEaUt6b2J2dzQ4RXRDekZNV3hEY1lueHBPUWZwZUZZbGd1bWtRczN5Z0tVNkNUVzNRUk1Ja1FGVVRiTG04OWZQNVJhWmgzQVdSdTUvNkFNdnhtbkxLRU4wU0FsejQ2dHAvZ2owSjlPSkJ5SDh2MUpHT3ZaSTlnNHVOVFlaNkdBTTM4VGZhejF5NG05YTFNejdZTVVJa2kwb1ZwWXgraVV4a0FLTzZ6ZTZUZXR6MjZXbDdTd3VtWk1oWXpnSjRSZGlHSzBJcjNQMVpSaHFqRHN4OFhqNDI0c1FjQUhqL2ZZYk9qbmVFUU5YNG1xOUQwWHE3REJVS0ZuSFlqSFRJT0JTMEtPS0J6VU5ERVlJd1kyR2NJbDNxdlZHWWNZT2hIZXdRaUF1WjVWNjIvODVFc1lCbmFEUEFDY3RmTFhzcnNvUkU2bWdZVnZGSTZiRDFKd1g0WHZGWXNtN3ZRNFZrVmhVajBXL1ZSay92VmtFNjhpRWpUQ2tESkFRL2lMWmdmdEswenpQYWlaRFlHZFE5bVBFd3h5OTRJNGxPdW1PNWVPUFRrc1JUMTgva1hXVE9LUjB1RHFzMmUzQmI3L1JHTlZTall3cnNYOFpicWJYekhQVk5zeklBcTBVNnNUb1YwampVejBvc3IwdDBoSVVFNVlzdnQrQ0RtRGxXUFgveUsvUWZMaDRESUJoWHJpcmpuNENnd0V3QWkvUWozMExuUktheUk3T0pMb05uR1JzWVBHWXNGMzEwUy9heGhWcFNqNXduRXBvZzkxdlBjQkQzUThNOE15L0hMNTdYM25PcERHcDAxdlFzc0dkOFZ4RkZlTGh0cGRXR2dFSnFHc2x6REt5SDdKb0tJV2duMlEyM2ZTeEZXRG1pVUVMSXJOQTNhaUtTVTNHMUc0T0IzOTUxaUJXS24xOUNPVWpSbjNROWEvRjRCVE1rYktJTi9mU1FsanFVRGNPN2VUYUFqVTJ6S1hwYkpxaUxuUGhkTXVsYTdwUzRZMENPVk1xOWQ0TVhYa1lYWHRiVW45RFhoZ2w2QzZOUHlROGhVVnZmMTNvNWhSRldGR1JUZEFOQThzZ1c4MlJhbFMzUHVkdk1pcVZjUWdSVkhxbXZYY0ZiRlA4VWErdnZGRXNaTjJJZnpKWmh3QWNMckFVc3NnMkx0Z1lGRlJhOFhwaFVERU9pRzBSTzZGcUtHaFZMeFU0eFFiSklwcUlZTHBzc3hKY3VJOUpJUlozTGdjRjBTa3lQS1VxNkx3akxhUTEzQ0lXaWhTUEpOdW5vUGhZZzFreXBFeGFkOThSUitQNi9LQklDelp6cGdaR3RUZXZtRStmZVNPQ3kwUGVURmoyRXlocW00MEsyb2RvcE9DWk1Sbk1PMTRDR3RZakJZN21HNHh2LzJKUndTU0RsQlpIUUhnRWVLZ2dHYytFa0JnT3JDaDA5Sm5xSGZRc1lEaFdHUW9Nb1M1YUkwZ21INmVqTWUvNGgxcmhRYURheThPOEJpVGN1dEhCQUdGUUR2ZFBoQUFIbm9RUUkzV3NHTnhIWTA2SHZpL0tEOXg2eGpDcEozUU9UeFlyVG1JMndzak4yclh6V25pSVVuWWlBUXRsaEowVzlGcWJTVW9NR3hCY1FoQkFaNzN6blBHbGxabDBTVnpJLzlyM3RteEhqa2N6WUdmdERTNGVibElmYzBVT1J3MVNWMkdlcHJUUHp3ZHVoM2NyeDQ2N3A0WnF4OGpKUmRuN0VvRFdPcXJGMnBBdVluN29JeFlJOStwVG8vbUhRVFlPeXI1K1pVeUxMdFJtbkx3Szlad3kzWnpUeUFVZVY5NjNHSndIekdxTnVCb0FFNHVIZTVwS2g0ai9HVlVoY3hCMDhTanNFT093cjh0eEJhK2tUU0lINTYxdDJaeHZpUTMycmhiOEFDRlplaGlwRXRVWmRWM0w0WlRzczFsMTJORm5JOXhUM1pGOFF5RldYU0FIMmlOb2tRQ3R6eG9YVmdPZm1oeW9VRVdzUjZCY1lNYllCM2tVa1dHUWxlbEUzU3dhTVpWbElHdlplRjhReExiYy96M3dON21uUlRUSFhONE96a2FlL3VJMXkzM2FRQVVQR0JkNk05MFVKUzVZMHp4VktpM3doNm9TRjlGVVhKYkpURzkzUmhRZTRsTHprVVJFVFA0Wm9kdThBSjZFaFUwUTdlOFBjclIxVlp6U2hUS1RrcjR5UUMyb21qOXBheS95cGQ4VHhucDJZMy9MalZLQ3FJRVlyK29nUWkvZHVwY0l6cUI0UVlZWkljQXhwOTBkbHBvVlQ2T2NuZWxpNFR1Vk8wbmRwQlA1bkltZWd5bUpYb3NySnJGUm1VazJjYk9YOXVMcGdJQjZjSWlGYzJBN0VMR1BQQUNwV2NidUM2N0ZqUHhxM2tkSXJWOHVubjhpNEkyM2xhOXF6NUl2NXpheEtlVVFqbmo0MWFTRldTYmgwSitqN2dVWS9xT2tKQVVCOTczLzNUbldlbm1EeG5oTHdseHF3VjNJQ3lKZ091eTZXMnJ4VEtCQzF0empJeThtWGxHb3FyZEN6Qk11dEtsT1l1Tzk3ajRIc29jaE1abTE2TVIwT25XYXF6THBKVTJLaFNvTmZyaTVqL0tldCtTemFqcHczaDZNYm10NWR3U1NycDI4VVR1NHovU203YXhuV0tOZzJFZ09FamhtM3VmVFRFMWc4QVBIYW1reE5kQmFrRTJxTC91R0J3bm0vSXB4Z09sS1NCRlEvM0U5dzQ5ZDRZbENrWGhpWEd1OVFYU3Q5REJ4ZkZwTHN5RjlldlFGYUlWSUtVa0ZJU3pNNmdQZmVlZHdPUGRWYWNDTTZUMDhSMEg2UytIaEJiaEpROTh1MWRMc052bEFSbVg0NER6MW1sd2RDRmpPbGRrYUpvcHJ6R0lDbjdaWUs5TjRxYlJreHY0R1JjNGV5VWVPQ0JLZHVPa3V4WUM0YjU5K1VybnIrd2djbmVGS2ZDbVVSRktOUk1YSEp3SXFOUk9ITVp3S00rd1BDR0NFV1lWSWJPMi93OGJVdHMxd2xKSWp4elh2a3IvL09NVkxWSUJXRmRXeGN6N2JCcWFsL0xYdGNXOEpBcXlRR2pxbkJsVXVFdGYzSU9ITlZJRE9wOG9MS2xFbElIdjN1TGh4VVVRNTZiRXRwcGw0YXNoL2xRaFBBOEJTRkFiRmhPd0pabFltQ0UzR2hZOGtCUTFZUWtNRldROXNJNWJQeloyY2RTUU9MbC82VEdiVWZtMU9YYklQbDZDQldFTGVnZ0NiSE1ndSszRUlESHlNYytnUEpHVjdHTXN4ZEcxSHRvRC9sN3ZPR2RaN0padjZDZ0lXVDdYVnNBYXpYZStaRVpudkdhZHdsbjhLWEZFc3MrcFFReHB0eXdPcjBFc2JVYzlqSkJxVVQwMUFiLytOYy9oeTk2L2s1MFdhV3V5b3dwTFVkSU9QcVlZcE9Wdm1iUHEvVkZCTXdaU0JYazlydTMrRzNmODFGZ3p3UTJ3YVNubGlMZ3NJa25aUzFPa1B5MUFDbEo3SUtFZzh2UURySmd0ZnBOTjFtczM4ZWsxVVJjNFZDV1VWZ0JjSS8xL01VQUdYWnpKVlFsU09WZTg4dG5ZM2g2RHdrS3RBT29JbWxFVWViRXFnYTMzZzdnQkE3ZCt1aUxrckR4a01uNndvZE95cTdudVY3RmN4MmlKK2VGVklsU2RDY21nTENDc1pCYVBHZHFhb05GNm1BSXMyaWJMOEpQaTlZWWQvWEVoSzlCQ1l6c2JvQ3JDZTNhR20yMWtVc2pBZjNLdllESmNUNkg5Qnhta0I0VEFUOEtFYW5DTlE5MGJxQjVvTUFYalZDaXlNSDhnZ3hWMUQ5dFRGRGlhRVVrWFRsbWRmbXE1RXc3Y3JXNE9BQ0p5ZGRpdUlOWHdIaDBwekNEQW9Gb0c5OHJ0amhjS3dGNmdnQ1JCdXNWTUJURDBHTWdXUmtzclhSejdtNDAwbTVsM2EvQ2EwQ2xQSHRJZXZlUmo1OVcxRUdZS1ZWZDdDKzM3bjBZQUhIczk5S2pLK0M3emlvQXBQbEQvMU5ucHhYMVNnVnRXV1l0eE5CK1VzZHBET05jQkYrc29TY21BSWd2U2cvZnAyNmZYQnVsUDl0c2lFTUNvdEhOWUNiTFNqblhWWkEyUStQSTRaQ1BQWUpsbkVyeVhLSTh3QkVTSWEwQVhiREV2ZVAvbU10U0xBSUpEaEVnbFpXVThpZjMzSllPTGs1S0FaMHJNQ2UwMVY1NWlncVRsbDBTRHlEZ2JHVkJIajF1QytVS2RiZFlvWGVUR3B4NG4rMjBmK2hIMWlJdEpBaUtRUUFzMzR0UWwxNEtQVFdXWW80czdQRVRTaDdyazRXMEJkZzFrUkloS1VrK0Q1bHYvVjdvMkdQUU1FY1ZFT1FIZitQZE1qMnhqbXJpTkdqcFNMSW5hYmpTMHN3b2cvRFNMQmE4VVpUSjcrRzdVSm55cXQyem41R0R5UVFNSW1BVVNraWkwd2psUktUUm1NU1VWSHo1cTMrZUltUmxJa1JpbndBTVRLa29hYWt3NWhkZk1aZXMyU3pyaThzYUsvUW96TzQyd0EraFQ4VzRlbTVZUWpESjFYZXdUN0NnQjZ3aDQwSW9FVUVpK2YxOUxBb3ZWL1MzeUFveEZrVXJZMHk1NEhWNmVtWDRvYjVLdDJ3SUd2Y09CUTRmVEFFenBSb1p0UzZqSkxPSGtCLzZyKysxRHg3bFl5Z2dpTU9hQUp5cjlNTGJVaHFCbXJXWWU1Q1c4dkZRdnh5ZkVQV0FHcTB5ZDYyT0Y4cjRGQXBHeW00SDViM2lLbnJCUnBzS3hoalF1NEdCSW9mSndEV1JSaGtJTVVsL1NHRTBjY0V5QTY1RkM4aDcyQVRwSmR5aml0UlR5TkczcUhweG0yb1JRZXhsTG5EOEhJMEpmenBRbXBpMHZaWHMyMHR2WjlSaDB0Mm81dWlwMDJIaHNZb0RsSDdTaHB1RmpWdHNibFRTcmZHNFFsakZXQTZTQW00UWdtTHJ4MCtRSGY5VkkwcFdyVVJFdXd0M3RHanZqUTQ5SmhHTnQ5NlNBR2phK01CN0VxZGNPTDBlUGx1NjFwZUUrWXh6UnRkKzk0NFl3eWJGcEpYT0VZaWNjVmk0WWtZR0F1LzNsYmJQMisyNTBHazY3MVJTY294NzlacERHdWtXRHpmTEIxdG9WWEsvRjB0Qkh1MVA2NTNRVGdaMXN5RUFrQkJidDNGUWhXb0VUQTd0R2Z5RUcvT0JWbHJaazdWYUMxWFNmN0NmckZFZWg0RjNNRTdRcTVkQ2JvRUppMkxaanBIOWJYdU5Fd1dnN0lzUzhwQWZsTEltaElQbkZ6QXdtTjFXeUVKSmxkZXprOVhXUSs4RHNPNTdrSDhjQmJTekhKRE9mK2oveGZRQlFUV3BvbXpUZ0dobDUwaG9YalJmL1VibWtWQXZJSEZCK2NLaVFjTHE5SXRrQm5GSjcyc0RFMW0vaHBwUTZnQWNmSllKd2tEZUpRcVAvL2FXQUFsSWFtRkZYK3AxY1JxdXRJUmV1bW1sODJxRnBMNHlwS1JEUWJHaitncVVaR0JaRGF2RU1DS0R3Q1VzVTBUSEpWOExtNlNGR2NrZVlNUjMzV0Qzd3lDbDVENEFCZVAwVExyOEdBcGFLT1VJeUR6d2RYbEliMGhLMlpjL0tQYVd0dWYwazE4VjBJNnBHZ21TQ05OSXBEMG5NcjMzTndBQVI0L0NKZjlZMXhFQ2xPbVo5OTJPcmVOM01xMVk5Q1RKUUt3b29ObXNZUEdocm54OXgwTnhZdWlIUnNSL2kyUXRvM2lWTHVDaG0rek5FL3N4WFp4OThUaVhaNy9UZkIrbExXNzJpSjZhaUN3eEMveGE5UDZMWC9OL1JaSE1Bc1pxSWRONVZ6V2hxZ1lHSE9SVVlzeGs4SGZJSnpBV1E2UjlINkowM2hJQUE5a3hmbVhmaVpEU1FHbUt5SVhPVGx3YzhxRXNleXBJQXo2ZTVUTTlCaXdXTWZvUWtBaUFab0laVW8vTjFxWXF5ZGI5Ni9WRC8rMlA3THRIWStvLzVrWGNlRXNGWUQxdEhmdGQwVXpmU1kzRjFBTkExOWpzR0dEaGZ0QUpNTW9vdlpGbGk0aUJhelgzTGYySmxoN0crNEU5dGlTMFlEN0JvTUxBVWoxeHI1QkxFR2hBY2NVV0JNV0F1c0tXRkpXUERzVU5BaTBIcjhPUkErRUhyWHIvRWl6WG5CQ0ZyK0xaWU5zcnVrb0piakFqVUtObzRHYWZMS1VJUjQxWmlMNXA2YS8vNU42cjRDTFowZkYzZ1NzREMwK3ZYdExzM2luKzF2aGViN1VpeFdhcE5OZXk3QlpXZTFrYXZCcGFaeHVqN0M1YU94R0k0YitrdWFwcTRjYWQ3ejRQM0kzRERQTGc0eW9nY052dE5nM092ZWNvcHZjSzBsS2tkVmhtb21ZZ056Wkk3Q2VLZDFTS0VNSis5Zi9GSXI2THVyWmlydnBCS29udXdZeGYySE9hWGd2TVBpM2tDZUNCa09MOTNwcVd3c21BaThyWUpjdFNhQXZUSGNVUStPTDFzS2ZXS2pyWW9OditDRUswTjZtTTVSVGFVNVJEM0Rrb29NYkZvTFJZR2NEUHhoM0lhRENySXJBYjF1WEYyNnI5NTNueGZRZktIb2lEUXd0bkU5VEljSWNxTVQ2RnZsUUNMWkJiU2wwekpRaWtKbldLZHVQaFh3WUFITG01V055UHI0QTRxZ0JsZnZvRDc1U3RlejRpOVZJU1VpRVZJQlhOdXRHc29KVHBJNldUdlJrUEt4anNmYURFZ1lBS2h1aEp6bmd2M0lJYmtlSVFGaXM2QnE3V3NXVU0vTEJhdnBRU2xBRWMxaDBQYmpkWWV6TzQ0dE1mSTd5THZ4N01VUG1lNzZVam5vMG9PQThoajZGRlk5OS9vUENDM2k2VURJZHJUVm5jWDBKbnc4T1JYZ2NDV3BoUzJ1bWxIclFOMGE0alhKdU1iaFM4R3dYdmhYVjFZcnpJVWQwS1pnQlpaTFFNa3BRMHJtWGpubWwzOG85KzM1NXhYWkhSNDFEQTRvWTMwdGFkUjVPdUM5T0V2dE1vRUJnZ2R3THRCSko2TXBxRG53WE9LZnE4YUZUOFh2NkJRaWYwMXJCOGtYMVJaWkVwQjlrUnd0TkYwbU1lNysrQ1JzVTllbVBTMjdFaDFsa1VCNEo4VUlxamk2aWU0U0sxVXl6cHdBSVdhQnhNZWR4NG1LcDBSWE9MTTh6ckRzbm9pR0packpVL085Q0NFWXlGY0M3ZDlqSFM0VTRXWVJUWVQvS2hoUVE4T3ZZdjZFQjI1V1gvTjNkQUdsRkdFNHNaMGpqcHhwMjNBYWMvYU83M1NBSGlqMGNCZ2R2OEMyZmYrcXRvSHU2UUtnQktTL0Q3UXpVVHpieFh0Q0hnaHVNSHpTZ3VWQm5IUHZUWW93UXJORTdLbGhJT3NJcWR0bU5ZTVpTRzhOT2ZQVkliNExtZ0NNckpuRzRaK3JMMFhuRDlBRVgyd0RlaldLam9RVVJKQWpPa3liaVdGR3RDckhMU1l3NnJTYkF5UWNBalQvWFRxZ216TUYzdkZpV3diRWFQelliTEhETEwzanoydHhRc3BpcEFOdnlZczIvcEcrVll6dGN5UjNRdDVSVE1CZHhZRkRKZTh4bWpLTS90cVM5M3p4bTlHeUtRTTVCYmtkR1NKQ29oSTAzZFNWVG4zL0dmQUFCSGJsclF1Y2VuZ0lEaU1GTjcvdFI3dVhuOG5USmFybUhSc09XQVFTdEU2T2JXc1pJMGRrMTAvUnNNT0F1R01aMWd3WUNnWGxSZUh3b3pXSUdsWHVCVnZoOEQ2alBiallhU2lEVWtkSldLNEVTak1jVUsrV29abHJYMVVYN3ZIMlBmcEw0dWlZd1NmMUU0QUU1bDNXOTVoSnVvd2FvVUR0byt0TXJPM3BrcEJrby9pN2RJZzJ4RktERVFwNmdacVEvRDBYRThhOXlvckxkUlFNUVB0ZlpCaXQ0Vmd4R1ROVEMwUDJzWS9QUUlXQW9QbVZ0QXdEUmVBalZyTlo2TTA0WGI3NTZkZmVkdm0yRy9iZUdzK01lcmdNRHRSd1ZBeHNuM3ZERjFteUpwNHVtZ2lpNTN3NFB6TFJhTVU0WnNZRVBDd2trSVd5d3FLMzV3dUYxRXlFNkNXUm5TS2FIQVhHd29VVEkxS0k5bXFaenZLWngrbHB0d2srdGZZUm85dllDTGVDTmFTWWl2RjVFNGRDMzhFZWg4aUFVMklvd2d4UFdNZnV0QkZaQy9yaTZid1llUjQxUDB5ZWhINW9heTJQd2xzbXBmTU9CTmpUeDdaQzJHcWJ6WXBxbklBR1VPRjlFcHNlQXRoa0ZNWCt6UWYwZGJJamRNNDJXa1ZGUFNLRmZTaUt4LzlPY0JiT0IxdjF5RlFzVDErQlh3Nk0wS0VlakozL29GMmJqalhobXRqa1RWZ3BIb2RFcUNkZ2EwTFgxeHRqY3hUTFkvc2c4c1kvcTdLeEdVL1V6Sy9ubnNCOFBnZjBoWWVtR0hBTXJ2WVE1NlR0Sk9IRXJGZ3hwdkhRb3R0dGxtdkRmc3VBeE5wYjBBTCs0MTlXY0N5cGxYQU1DU1MwNDBZNVA4R2FaRUpkTmNoc0xsZzNqZEU4YkRDSG5vRmRDcnZEK3d2MzlKdFRHVWJKajk2SjlWU3JDR2JZaklISEMyM0NSbWdVemZ2dmpwOXhxek5yU3RBSVJNdHBubHJDYzFMM3hrcXp2eDdsOEFCRGg2ODBYRzRoTlJRSUI0K1J0cUFCZlMraC8vZE1VTmdWUVpsVVJhM0hFYmdmbjY0R3RxSURnaXZoNEh5b0JYNnBVazJQK1Nqd3plTUFZd0V1RCtYdFFsMHA4Rmp3UzkxaDRTaDJ5TDl0WENoSE5iRVUzU0lsNE85WTlScmgvYTNBdkNWOVdSVWxsK1VuUlEyakRZV3hCVlFyZ3FNeWZCTzVZOGRtQ3g0dUk4RUtEWXRxOXEySTJRQlF5b2diMjA1K1FBUDNGVUYrOFZ1RGJ1WXdvVFBCOTZ6SWZlZWtZUUdBclAwcTY0cjQ5QmhtSFBEc2d6eUdnaWtrYWtVcXQ2bkxCMTdMZm44enMvNm5VRkpmajRaQlRRZ2hGU211Ty84Wi9rd2dkT3kyaTFGZ1dSUmk1Y3QxRHRISmh2d25mS0w3MXlITmE3UFRQcEEzVGwzSlZSQXdNWDdPOFhBVVdxYnlnMDE4cnNBMW0rVTB5SVdZT0lsS05RWnlnU2ExUFE1dUlIT1VUVEMxMHNOaGxpUDVtd0lqb3NYQkN4QW1leUx4L3hKSS8ySXVHaUJYY2pQN0JnQTVxSnJqd0RXMXpXMzRRZmRDaFRtaEd5WHVCT3ZlalZiOHJlTFBiUEczSTNCUVBLWUl6UTcrRURHOE91QVFCV3l6c2diSUhSc25EOUx0WGpiL3ZYZ0N4d2Y4UHJFMU5BUUNFM0oyemhPRSsvNjZlRmJXSTF5cWhIcm15dU9DTEFiQVBRenQzdDRFaUFFSnpERWp0ZkJJWVpJMzd1RlhVNFFJN2VKSlRQWGZWQVZzUEl0M2M5RVJYakl2ZlVhelF6ZW90cnBiRDJoa3JnOUJLcWdERDRMc1UyMkxpcTczNFFhUzRBWmYxeFA4b0FrL1FwUWhRMDRTcFFsTXFRaVhzWGdlOU13QkswOUlITDBNMktLNkV6QXNVN2hkemhFMVV2VXZUZVhSZVplQUNTZFNESHdSZ1V1T3RlclpzalRiWkpTalZKWktuR1NVNzh3VnRtNng5OE8vQzZDamk2RUh6RTlZa3FJSUNqQkNudEE3Zjk2M1QrM1NlbFdxbUVJS29KakJib2JLeHlCOHpXZ1ZTc1lERWxMZzI2VitJQ2VSMmpYUUExdytWR2FJdCtPZ09sbG5CeHZ4ajdmRmxKRjE4cGExazhDSko0amhUREVuZ0toWFlaQWlXN2RYL21tb1M3UzVJa01pT0cvWWRmS3dkOXk0QW54RVh0QldMWCt1RjZEUFVKMHUvN1p6K2w4TnY3dzM1K2VDWE00TDVGWVIxbkw1eElHdjk2VThMVlp2dFNqMGdHR1JGWFVEbzh5SzM1aTZVMVVsdWlYa3Zwd2gxb2p2L3h2N0QrbkhoRTZ3ZDhVZ29JUW01T3dJVXpvN04vL0crVHpoSWxkWGI0UnVYOFVtZFV6Tlo1d1d3RFNKVU9iRVZBbDBHdW1ESHpYZkRaRWJqandkTHA0cjdUUUJBOVQxZytFd0xYUVhBTmxxTUg3RHVSVWtLNWtjQW1rU2xoaE5tTElRbENCLzF2U2FLYWZHaXRPRlhva1RMTkJyclhwUlRNVzhBWGpOY3MxZ3hnUjEvR2FEak13eWo3eWIwN0xYbmljSzlxK0ZCVnkxcnN5RGJGczhwSkJ6Skk4eEU5RjNnUjlnNytGRG1lTFRZMk9iQ21nQjNRelpDV3RrR3FHc3JVVnBJVHpyM3p0M08rODYxbS9XN3JubGdGeEhYRVlhYXRCOS96YjNIKy9mZkthUHRJcUpycVpmYUxoeWhJaWRnOFpTWmE0a3dpd3JmNGNtWHdNZGJJQTBPRCtDK1lSRnlZUlMyRytDaXNnZCs3RDU3RHd2Ykt1UEQzSUtxMGljR29RM1hiNHRodDBZT1YzZjdvUjliUWxiRlVvbENRMUd3SEFLSFF0NGdUSzJEUnNvaGthTWpMRXpqc0gzclNYTnlsWHZ6WnZnOW0zYnp3eHF1aXJUMExaZk9ESUtVdlNDQStadkl5TEN6TC9lUE5vdkFLZEhOYmNiVzBBOGd0MG1TMXJyZnVhUFQwSC94ams5M1J4MVNtVDBZQkJUaWlCaXJQWEVnbi91ZjNWOTA1UWIyaVREVXhXaVU2bjBVQzYrREdDY05HQmV1SWxFQWlmRWs1Uk1OUGNieFlFQWdoREFjQXcwRWN1SjBnVE9PZUNzU0o3eEU1WHJ6Uk9ZTndLYmVVMEtyNEtkRnd5YXdSbVJyN1M0akhWQ3diRlFpb3FkaFdSN2VGcExhWFM1MkNGbjNwRzBIN1ZpaEt5SDlZQlJPYTJHTmFHYlI0bUMvdWxVaW92YlVERmxKNlJibWlvSUg5NUJwNklzMkMzQUp0ZzdTeUV5a0ppQ3JYU1NzNSs0NmZiTGZ1ZVMvd2hocHhBTzZqWEora0JRU3NTT0ZRMVo3Kzc3K0lVMjk5aThoa0JIYVV5VEpRMVVSdVhRYkpJdUt0TTRJMEd1UXJBZEEzb1lLbnFHTGh1Z0FMYnFxdjFBaFhIVU5ocjVlZEUrSnpnQXZRQTVZQlhpcjhXeWgzK1NrWWh4SmJlNmJpZzB2Z0dhNCtDZzJxMUN0SGJBcmtybDJqcDJIekFsRmw5aHgzc1dUdU9lSTAwSENld3o0dGxHQ0ZoeGllM2F5eXdDcTRaUjlVSkxFc1Z5anlJTUNjRm1aaU9ablRsWmErMDBLQlVSNEZ0NXVVOFFxcXlTclF6Vmt0YlU4NDg3OU9idHo3NWg4QkRpL2tmQi90K21RVU1DNWFVYUVnM2YvRzcwZ1gzcldGYWsxRU0yUmx1OHU5dFFVWUtTazJ6d0RUODBTcXZRTTBnYWovQkVDbjE2R1Z4VFYwbzhYZzYrejlUT2NVRWRSQk1XN2xOWkNnSm1pMmJTc0tGMVpjRHdZMGcybEN6cVZ1encrY0REVXZodGhQVVBJK1FsSXlpcXM0d0FTSWVWcGJrUmtGaHJFYXROUkI1cUVzUXJrQzU0bXQ3d2llRUFaVHNrZXc5SHh5YjdWY2FhQUZEMnRrUjZMZlNzOEJoM1ZqZWE2Tmc1UWQyaU1ydzhpL085OEl0YUNqblFwQVNhczdDTFpndlpycjJVT3BldWovL1g0QUR3TzNDeDZCOTd2NCt2TW9JT3dCdjF3MXpmcUgwN20zL2RPSzZ3blZKSXNrWUhrN21MdkFEN2ExMS9vSm9Oc0NVdDBMMVNwV1dKUng2SEtIbUM3Y0N3Y2ZJQWF1cVNqY3dKMVJDcWNZT05KZTUwRFI0N3RtM2ZvUTBrRmNlTTk0VVF6ZktXSzMxU2ptS3U4TGJKdG9MdlN4NUlnTEN6TnNkbzlkYWNTOUxTT1RnWXMydHlnWVVDRGhGUWlITTI0eHZXTXlZQlpNRHowNEdlVGhBdzhQQzRnTFJpeU1nMmNmQWJBVmRJMmduYUZhMmNOVWo0Q3NlVlF2MWUzSnQvK1BqYlB2L1k4NGRPaFJhWmVMcnordkFnSzRXWEhvMXFwNTRQZCtWRTcrd1I5aHRHTkVwY3BvRlpoc0I5VVBPNHdoT252Y1psRGx4TGc0RUFwNmdxWHN2S2RWWkVGSXR1dFc3K0RpUFJZOEdZTlFaci9QWml1bTdBbHVvSGRwcGVBMUd0c3ZJUFdYK3hvcGd2RHp4bFhWbkhyeW5KWHB0cW1lQ0gzbmhmRGdQVVZTZnVJdFJYRzc0UVlSNzlQY2ExbjFBUFNGMmc1SndndUFVYmZYTXd2RFlLZFhPcGU3RHFKd29wY1BVVFkxSnhVcG1ZSG9HcUtiVWlhcmxPVWRncTZqalBlUVo5NjVqbnQrNlRzaEtlUG8wY0VESC90NkFoUVF4TkVQRUNUYnUvN29iNlZ6N3o0ajQ5MlFQR2RhM2tuVUs1VGNHaGNyTkhMNjFMMDJjNE84SHE0L0tETnp3UGxwOEhVRnRBellmUjhjSzJyQW9sVUx1UllsYzRWM0RpelNWZzRIWXJsdlliNEFRQytxMm1PUm1tM1RsTXpUSmNTcWVzREthSWFwWG84RXNnMThXUmVxZzM0S2dDakVLQWFZS0RRTnZaL0kxczh5TXdvWUdFeWt3Smowd0llRHllamZDMWdURHd6aXVYaWFuamFUVkZtSHVpblF6czJhcis0WHlRMVJMZVUwZTZEdVR2enU5emJZdUIzODZxQTdIdGYxUkNnZ2dDT1dJY0g3UDFROS9OdHZxUExKV2taTExUaFBzbTJ2TUZXZ3RzYmRKU0YwRHB5OHl4UGlxZEN6eUFwZnQrQkJCWG9pRms0c0IzMVFoQjkvUjFXTHYyWUNaWEVwL1ZFUnZqZWJNZ0x6c0NCMDNqRDBKVEs3M3NuaVlzc2VRb1JRWTIyY3VxTzMxQWRJaURLMk9yWHZrcFpaaURJeGlXaWRnejVGN3BjQlNWaEt5TXdyRENaZkNjNkFPRElnZ2dXTlVoWkt5YkZyWUVESGc5QmM4Q1k3LzE1Z2JRTGFRa2FWb0VwQW5oUGRUSkFicHRVRHRBUlduYXVxR3ZINGI3NjVPLzBIUDRNYkQ5ZVAxL1hHOVFRcElBQWN6Ymp4Y0QwLytkYWZyQi8rblZ0VHRUUUJSMDBsZ3JUdFFIL01xaW9nTmRITmlGUEg2TllpWm5VL1VBQ0xTekZqd0Y3NVF0RXd3RTVSbE9CYUd3UFVGektJRHlyY1phTzRuNEo5Q0dpcEJ6TW1jdUZNRDN1cVJIdGtzTkJORTIwRkk4b21sQ0hoZ2hGbGlGL2oxL0p2Ny9yS292SUJMdXZOcVc5cVZJS0hSYmNhVVROOFd1Zmh1U3MrZ1RsUWVzMDkzSW1KQVZ2amsxYTNTVFVhRTdrUnppNEltaG5UNmo1VWsxV2h0amt0N2F6bDVPL2V1L1RRcjMwclNDbUZ5NS9BOVFRcUlJRGJqbVNRVXQzenhyOG5KLy9IdldscDEwU1ZIVVpMSW11WDJQSk5jVUJiallCMlJwdzhabFFOb2xUTUdXZ1MvY253UTNvRi92c1FYY2VMR0E1RzVDM2R0TVVBdVNQdVNjbWVtblA5WW5oY29wUXpjNWppTnlWelV3ZVUvV3VUZUNXRDJ1OXVTeVVGbkl5SWhqM0gzWHZCSVMvcFdZZDRIZEYyNlZmTk1TWWZmYitYZU4ydHBFZGlZZlY3U09OZFZPZGkyVTlDbzcrQXRrVzlZNmVreVlqTXJYRHJ2S0taVWxaMklhM3NoSGFOWXJ3UDFhay96TTJ4WC96bTg1Qno1Z0Vmdit1TjY0bFZRSUNRbTlNRzVCUWYrTlhYeUptM1gwaWp0UXJkTEtmSktyRnRIOUhPZkFaMmdxb1N6QzhBSis4RTZoR2N1clYvQzNWUUxCTmNtdjJBRklFT1pyWHAyY0FTQk41akFQZlVseUVKeXV1S09OQlFTclJoaVF3NktRaEI3T0hNV0o5dnh5TFpYZnRUcnVHR3lVdi9vaVRmNTRQNE5xNjlGU3ZFZVZqbUNJalk5MHNMcXpFRWw3M2xSd1JuZEM1UUUyS3J0K0lkZlBzTjVzQy8vV1FYQW0wcm9pM0h1M2V6R28rWnVnNTY3Z1F3dXlBWUxhTmEydzlwdHpTTlZuWFUzbHZ4K08vOTdaelAvLzVqRlJ0OHZPdUpWa0JZUTk1UXQxdjN2RWVQLzk3WHBObmRxQ1k3TXZJbVpYVVBzTEtMYURadE5ISUhWR05pZWhZNDhSRkJQYUpsUkhKZmVGb0dJWWNRK3pSY3JGME5xeGR1dm5DQ2lwNzNBajBmREVTMDF3Y2dnS3F5NERLN3pQbEwwUjRDb2xFTWFBdDlYRUdkakhialNVVXNGQTY2em5RdmlVOHE1Ly9DdW9leWxRMGZBL01PK29PQ1k3V2ZiSzYwdlNYVG9zUkdNNm50WEZId24vOG83Q0JLZFV1cndHekdxa3BZT3JCZnF0RW9KVlYwcHg0U05odVVlb1I2KzZVaXpJSnFvclZzamZqUXIvL0U3TndmL21mZ3hrOFk5dzJ2SjBFQkFlQklCOXhZNTdQLzgzZlN3Nzk5Uzhyclk2bjNkc2h6WU1mbGd1WGRRRHUxRFgxVUUrb2xZUE1NOGRDSGpDTkUzVWV6REk4WjBWcjh3REZOV0RPUFhndEZNY0JhVU5oQ2o5d0Q3SDU1cU5zNlRiQUNFSmFOM3p5TFVtSVV1QzRWZ2toZ0pjU3hYdEhYSWNUaEVJN25vaVRmQTFMTHkramcyYjF5bVpVQzJMZlR1eEprZlFqbFk2eStDeXdVQ2x6c2J4UWM5QlZGZGg5VllqYkRlRzFGVmk3YkQwa1ZSSVR6NDNkRFordEFxcEYyWEFtcEVnaTA5WGhwaEFkLzYzZG5ELzdXOTFyUWNkc25yWHpBazZhQUFIQmJ4ZzNmTnBvOStGdi9UTzcvNWYrWU1GOWl2ZFloTjhEdVp3RExPNGgyYmx4WnpvSjZBc3pPQVE5OHdBUlZqeTFpRytUQUVRdHRBNWpINzFIVEZxdnMra1hUanZtQ0V3d1FIdTQ0ckF0TXFRYjFwUDZmQUpEd0ZXNEZ4cGxpc1hoNlFmTGtqVmhWZzBCczExMlVESGJzaGxHdW9nY0Erb0NqNXpMVmVjUGhCMHVHZ3NQdkdlNGJMaURTZ2pQUTQyWDA3d3NFYlpzd24yUDF3RjZzN05zRFprVXR4UHplTzZuVERZRUlxNTFYU0JwTndKeTdlckpubEU3Ky9udW45LzdTelVCcWNOdVJXTjczU1Y5UG9nS0NlTmZQZGpoMGF6Vjc0TmYrZHZYZ0x4NGQxenFHVksxMFU4ck9LeUNUYlVBelE5bWhxaDRCN1FaeDMzc0U4L09DeVJLc3RDdE1tdk5aOUxMK0VnbEgzcmRZaGVBRUNUQzVWU1R5SUFNZ1JOa3B5elpZQ3NxbkxPNzFyL1hGSy81dkxqb1BBRXoyT1JYYWdkZ0NKcS9qTk9WVzN5elNOTGhGdjgySWw2NXhvRGltUEQzajNQZUxKWDBYUzA4amtyZitKaFJ1MDVYVWFDM0RoRmx0eVdUY2IyTUxJeEhzZis2Vk10bStBdVNNS3JmWVBIWTc4dXk4Z0lwcTkxV294c3RNZWRxTWxuYlY2ZVRiN3RyODhKdStDcEFMOE4xdy9yeEs4bVFxSUFBUVIyOVdIS1pNNy8ydlg4OTdmK1YzcXRIU1NLUnVvWTNJbnFzZ3k5c1UzUmFSSkVFN29Cb0JJSEhmbndMbjdnUEdLd0FnMEp3d3hHaUY1b3FCR2xqQ1lqRjE4Y2RjamhSTUZDVmYvcjRnQTlyMVc3d0JvQ1JqNjZUWEVYdThPVllsQ2ZIMTdraE9Bbmw0eXRoRzJrMjJFQkpyWXZvMlkwQ25TQW1wN1dHUlFneUZHdmhrNy9jd0NFUDBYd1dxV3ZaMDZTY3MwYmJBNWhhMjc5dXVsMXgvRmNWMlFwWTgyK1NGajd3ZmViNEZDQ1R0ZlNabGFadWduZWMwWGh1blUzOXdFaC82cWE4R0hyNGI0Q2RFTmovVzlXUXJJQUFRUndRQXUvYStuMzlOT3Y2YmIwMlQ3V05KcXgyNkR0aHpUY0x5YnFEWnNIM09vSURVZ3RFcWNPSVk4T0FkUUJvQm80a0RacjhXS29MRExjWmdEaUNhT2FlK3NMbHNTeHRsVFlnSU8weGJlY0tRdVNqN1NNSlhtdnN1dGJiWkFsMU5ncFpKUFVzaU9pQVM1U0xkR2VnVHZEZ2d6dmdvQmFmb1UzZGhpZ2ZBZEVEYkJBVHhXZVZZTURZMUI0SHBMRldhdWY4NWwySG4xUWVrbVRkZ3F0Q2NPNlhySDdsZG1KV294cWoyWHN0cXZDS2NUelV0NzZpcmMzODh4ZDF2L0pvdG5IcVBCUjJQWFdMMWlWeVB2a2YwRTNzcGNFc0Mwcnk5KzJlK29tTCtiN2owdFYrQW5CcnB0a2F5NXhyTDZxOGZKOGFyZ2lDU1J5dUM5UlBBN0R4d3lYT0ExZDFFTzdYYjlja3hBS0RSQzhrUGhGQlh5Z0UxRlVBY1FNR1ZnTGswTDJheDRZNnN2dDNXaklxRkR5SnhJRlcvL0lndzBBZUI5SEN5dkJkN0V6akRKbUMvTEFCbFlSVDlwdlRGN2ZSR0Z1VmkvTlZYQ0lVeWk3dGp3Tncwc0pqdnJpQ1l0Y1IwTG11WDdNQ2VaMTRpdVdrNW44NEZWWTJOajN3WVd5Y2ZFb3lTWUx5TnN1c1pWbVhSZFYyOXZLK3V6dnpCT1I3NzFaczM1L2Y4UG5Db0FvNCthblh6SjNOOXFoUVFYaHVXSUdrOTMvUHZ2M2lFMlcvSXBhOTdaY2ZsdVRSYm83VHJLbVNwaGVmdkV4bXYrQmhrWURJeHJIYmZlNEU5VndCN3J4RXcwelpES3FCZUNvbGFUbHNibWcxRjJFSGJCSjEyYjZiZUI1QVVKQ0V6MUpPMkhTa0pMUHY3bFpXVUV1TXNOTTFQNkplT2VGV05MWnUxSUNhREhZQXVaMEJhU3U3S0NRL0RuR3VoVThwNlhsKzdJYTZnWVZmTG9pNE04V0pQdjVDbVJFcGlmWWJ4WkNSN1gzQVZKdHVYT1YvZkFxb0ptQnVjZmQrNzBHNWVBQ29SV2RxTGF2ZlZFRFlVMVZ3dDc2cmw1Ty9jUHovMmExL1ZkY2ZlNmNyM2hGbSt1QzVXd0VFVy8wbTVGTlFLU0ZzSDd2bUZWNS9JK2JlcXkxLzNoVXhyY3pZYm83VHJDbWcxSXM4ZUE2cXhaVXMwRzEwelhoS2N2Wi9ZUEVzY2VEYXdzaE5HYXJ1YktxMlBBUk56WldsZzdVQ1U3TXB3V1dZYXhMdXFBK3RtRnN1WklQR1JMOTZ2QkNjQ1dvSTRKVXRvS0lVV0pkc0dsaGxaZ2FTRWRObklRUVhLc3RXSTZrdmhaN2hkTjJNYUJhZTlPUzRUTEt5Zks2WWtVWFlVYnMyWWttTFAxWHV4NCtCT05MTU8wL09iVWkwdGNmT2g0emovMFR1VXVVdW9LOGl1SzFDdkhhRGttUkRqcmw1YXF1V2hYN3U3dmZPbnY2UURQdWhjM3hOcStlSzZXQUdmVE9XTEt3T2E3a2VhNHY1Zit0S3h6djg5THIvNUc3aTB1OFhzVkVwcmx5U09Wa1JQZmhEUUtWQXY5d0I2dEN6b0d1Sys5d0RiOWdQN3J3WEdFNnZRVURwRjR1NDNQSFJvaXUxR1pScG5qREJLUms0ZFYzbnBrUVM0eTdUVDVRVkkyVEtHaW9FbDlKSXNjZUtIWWlmakFBQ2RqQ2xRc2ZqM1dOMEc5SUVJTUZBOEdTaldnTWNqZ1RpTHlza2REVmVjeTdoeHF3RzZCdHYzNzVTOVYrOWhHaVZzWFpoQ1pJUTBHdlBzSGJkajYvajl4QWlDOFFUVjNtZWhXbDRqbTAyd1htdkhOVWE4NzQyM2I5N3pwaThGNUI3ZzVmVmpMU3I2ODE2ZlFoZThjQ21nQ1dUVGlIempwRDExcjF4NjZBZnl0bWNEMDFNNUxhMVd2UFJGNU1NZkZzeE9BK00xQUFuUVROUVZ3QnBZZnhqWU9BUHNmd2F3NnpLcmNNbHRKUDN0c3NGbXNTUldmZU51THFGc0xTWVJ5SkFRamMwcExIVlBrbjZHcjZoRlBzWk9LcURKQ2FKc0dSTW1oM0dFdVhPaGRsbWlHS1VjcXA1TE5VMG9IMUQ0djhoRFI0UStTTDMxN2hrRngwWmZteGJvT3E3dFhwSGRWMTNPMGFSQ3U5WElmRTVXb3lWc1BYeEN6bjdrdzZMVFRXS2NJT1B0a3ZaZXc1UnFZYlBGdExRblYrMkZVZnZSWDM5TDgvQ2JYZy9JaVNkYitZQlBud0lDc0pwaTROWnFmdkxtZjV4bXh6ODZ2dnpyZmh5N1AzOVhiczYybGVRNlgvcFp3UGw3d1hOM0FsSUQxWks1SUdSZ05MR0JPMzQ3Y09ZZTRNQnpCS3Q3QVczdGVDaHpjWXMrc3dRZmJwUEMyaGlCUzhrak1KTktJZ05zRkNtQlRKS2daRG1XVWYya2VLRUdEV3hFTkl3SDFBaGwzT3hsVldRbUNOUTI2eE5QeVJXYUpQQmpVQ2x3Vit5V0dRNDFJeW11c0hKOW9XRFdBVjBucXp1M1llK1ZPem5aTnVGc2F5N3I1elBTZU14Mi9RSTI3bnFmekU2Zk5PTThFa25iOTdQYWRZVks3Z0R0MnNuS0FVbWJIeDNONzNyVFR6Vm5iL3RPcy9GTVQ3YnlBWjllQlFRQUFqZG40TlpLMTIvK1Q3TTdmdlRkSzlmOHpWdmxrbGM5cCszcURyT3pGYlpmS1dsNUYzanFnOExaSmpCZVJqa3pEUUpNdGdGdEk3ajNQY1RhSG1EZnRZTGxIVUE3aCsxWE10d01DYjR0UmVUc1VuRnFaaWMxZ1RsQ0ZGRUZVMHAyckZaWnR0UHJIbUJ3THdtZ3lFUVVOMm80VzFyVXF4YlcyS0dmcGVMWXN6TGVGS2dIUkhTbEhQNGVRWWtydGRBcWkxckY4dlpsN0w3MEFKWjNUTWlzYkJzRlVvVjI4enkyUHZRQnprNCtiSmk0VWlBdEkrMTdEdXZsRmFaMmk2alg4bWk4UE9HWlA5aHM3anY2RDJZWGJ2OUo4eEx5U1ZXMmZETFhwMXNCL2JvNUF6ZU1nSGUvYit2T2YvZXl5ZlNobjZvdSs2clg2Y29sMEsySE0rcHhKUWRmVEp5L0J6eDNqOW11MFVUQVpPeCtQUUtxTWJCMURyanJUNGp0bHdqMlBnTlkybVlGRDEwRGsyZGxMcmxrMnJRUFJpd2tEZzZHOVBPVkMvT290dUl0b0p2bGhxMDBVRU0vd0lMV0JCQkRaa29pc2RCMkVkbXFSaEF5cVBrTFM0MzRlOUg5MnJ3Uk5CM0drNUhzdUh3TjIvYXVJbFVWbTRac1pvM016NTdsNWdQM3NUbDdVcEJTd2lnUjJrSzJYU3BwNzdWTXpFQzN4YlM4RjFWZW43VEhmdlYvZFEvK3duZDF3SjhBdDFhK2pPcFRvbnpBVTBZQkFlQmRMWENvZ3Z6S3lmbUR2M0pvdkg3L2Q2WExYdnZQMDU2WExPZlptWmJ0UnNLT3ExTmEyUStlL1NpNWNWcFFWZWFLbzdTOW1naHFHRDY4OERDeHRnL1lmWVZGekFvaU44bDNpRUt4Zm5aRnRZenBua2dxNUlabUVwV0lyOGtJMndrZ0NjWGNzZWRXeldnRllWZFlPV1NhZnlaRXFNcWs3b2FEeml4YkJEc1c5V1djZzFwQnUzRzIwcitsL1R1NSs4QjJxWk5ndnJtRjZabFQyRHArUnViblR0dkdVS09LV0tvTkY2WmxwUDNYTWEzc2t0UnVBRkozNCtYOUkxejRNOHp1L2MwZmE4Kzk5UWNBYVF6djNmeWt1OXlMcjZlUUFnTEEwUXhDY09qVzFCeTkrU2Z3d2ZmL3diWm5mZXVQNVgydmZFWER2Vm1iYzIycVVvMzl6NFB1MklLZU93Wk16NWdscVpiRFRRbnFpWTNheGtuZy9FUEEwcHBnMTJYRTJpWEVaTVYzOWZjeXBTUUFWWkVCVkJTa29JL0JUS2JrNnp3eUVrVlZDcnlERnloWVNiY3Y5RjVZUG1YS2FpQ1I1cDg3cHFSZ3pvSHAraWk0YkFlanZRc1dUeGRLTGFtZVFPcUtkVjBoc2NXWk80OHhYemlGOXV4cDRXd0tqRWFDeVlneVNtQ3pKV2dTWk1kVnJIWmRKUWtkTVR1WHE4bDJyVkk5cWsvOTdoM1R1OTcwWGUzOG9iZVl5NzM1Q1NlWUgrLzFGRk5BQUFCeDlPYnN4T2U3Tno3eUUxKzB0UDZSN3h2dGVjVXQzUDJpTVp1TmVXN09WekpaVHRXQno0RnVuUUxQM2dYTUx4RDFHRWdUSUhKejR5WHppdDFjY1B4MndjbVBFR3NIQk5zUEFzczdpY215dWI5MmFtc2lOQVBhUlhJaHVSWkpMQXlMOVNCZU1XL0ZXbDV1WlJGQlNSQktmMklZU1FXcEtLdmFMR0xXZmpQSUlLT044TEhGV2hWUVRvM3ZHbURqTExGMURzM21CY3hubTFKMnpSOVBJTnUyQzdVaG1pbVlSa3hybDR2c3VBSlZQUUs3TFVVYTUvSHEvaEUyNzZxYWg5LzJIelllL0tYdkEzREtQSTU0VmNTbjUzb3FLcUJmUnpOd09JRzM2RXprbjlYSC8vdi9TRmYrN1g5VFhmS0ZuNHR0bDZMZE90OUozcWhrWlJka2RZOXc4NVR3M0wzQS9Cd2hJOEZvS2R5YUlGV0tlaTFCTTNEaEllTE1BMEMxSk5pMkMxamJSMHkyQWFObGtmRllXU1ZKSU9hempMYk4wRlMyMlpCa0RBNTk1WWY0Y3VaQ21ERGNwWjliSFNubUxxdTBYWWVjS1dWaGVRREhWQnZobm9TUURtaGJZSGJCZGhhYmJ3aG1XMEM3QmRWV0lNbDJHeHVOaUdwaWs2ZWJnVzFEMUN0TU81NHBzdU1nVWowRzJrMGdNOWVUblhYaUxPWGpiL216Zk8rdmZWL1RmUFMzVExHLytrbkpiSHlpbDN6OGp6d0ZyaHNQMTdqdFNBZGdVcTk5M3Q5UGw3N3llNnRkTDlyYnBUWG83SHdIemlyVUt3SVprVnVuaE9mdkJXY1hERkRWWTl0R1dIM3JEenRLd1lLQXJnVzBCU29TNHpXcHQrOW5kMmJLdC8vT044dm52ZWlnekJSb1dqQXJwSmtyWnZOc2hkTWVWcXZ6aXduUzAzSzJYTXlBWkVWT3hvS3FHbUhuZHVBRHh6Yng0ci8rcTBpWDdoRnRPZ0tWTURmQTlBTFFiQURUZFdLK0lkREd5czRTQmF5QXFnSVNhSW9EYXpOYlFTYXd0QU5wKzJXc3R1MHhpNW0zVkZRMGpYZFVrOVNrOXVRN3p6U24zL0V2dTdQLy9kOEIyTUNoV3lzY3ZibVVWbnk2cjZlSEF0cFZRU1I3V3UzeXliNHYvTitxL1MvN085aHh3NTZPQW0wMnMwQUY5VVRBSk4xOENtdzhBRzZkc0FLR1ZCSFZKTmtDYTkvQ3k3ZkJCeEtST3l0QW5jN3d2T2ZzNVBPdk9jam52ZmdxZWVWTHI4THk4aTdadG5NTkIvZXRjT2RLR1Rpbm5PMXdoY3AvZEVCdnI4OFZEenkwanZWem02aDBoajk2ejBQeTNkLy9POFRTQ0d5YkFXZnAxRXBWSzFJbGlOeHlXVE1OSStHMXNYL1RLTW5hUWFUVlN5RkwyNUhZVXJpcEF0RTAycEhHRmFwODRRUG44OG0zdlhIcitPLzhHSUM3TGVKNmFsaTk0ZlYwVWtDL0RsV1FONGNpWGoyKzdEWC9NTzI0NFcvSmpoZXRxbFRVWmwyUkcyRWFKVlFUcUxiZzFtbHk0emd3TzJ2Wmttb0V5TWlVVWp3UkxGN0pKQUEyNXNCMEJsUU4wSFZBR3NudXkzZmdXVmRzdys3dEs2aGtCSkdhcVFKbFBKR1VBRUZpMTh4RWthRk5DN0xEcWZVcFAzTDNlWnc5MVJDem1Sbks3U3RnNVpzMFNRS1FpSlI4UW5oWUhEV0N1VFhDV3JNZ0pXQ3lFN0x0TXFTMWZVaXBZdXJtb3RwbXFaZDFOS3BIZGFxUkwzendBczc4OGM4M0Q5ejZVdzF3aDNYb2RaVnRKdlhVc0hyRDYybW9nQUFBQVE0bDRGYzhxNC9ybHk0NTlBK3c2NGEvSVRzL2F6WExHSGwyb1VPZWlpUkpySmJBcWhhMk04cldHZXJzYk1MV1dhQ2IyaFloYVV5a2tRY0ppU2xWU0NuWjdpNEtZVzdCTGhQelZ0QmtTL3VKRXl6bzJOY2FPcThqdFlVam96R3hNZ1pHSTBtcElyUVYxUTU5elVjVlJRYXdMWGc3TzNHSzlnQ01WcEpNZGdLcmV5SExPeUQxTW9TWmFPY1FNS2Rxa3FyUmFrSjdGbWw2N0l5ZWU5K2J1Z2ZlOUpORjhRNjlyc0xScDZiaXhmVjBWY0M0RW5CSWdEY0h1ZmZjMFlIWGZsdmFmY09oeWRwVmx6ZlZBZVE4emN5Ynl0eUlwRG9oVGFBWWlhaUM4M1BDNldsaWZnRnM1d0kyS0trd2lDa25FeUdWU0VwSWtoaEh1VXF3ZDVLTkdBY0ZrandsWnlrK2tscXFvZ2tDWFdRL2pBclMxcXA5bUlCNkNSZ3RNWTIzQWNzN1JaYldLT01WRVZTZ05oQnRWSEpXVm1PcHgydFZ4VGxrODE3a3Jmdi9zRDN6dnA5dnovN083d0U0QmdDTzgwcU81YWw4UGQwVk1LNEUzQ3FXVVFFQUhOaTE1NFd2MFQxLzlldXc3WnFYYy9WcXRMa0NjdTZJUnZPOEU2WlVTUm9McWtwVVkrT2RyY1IyRTJ4bllMTk9kQnRHN01iQ29GZ3NMbDVpUXdFa1M2eU1zM3hkUWpuc3owckRnalUwMmtURTNQOW9XVkN2UUNiYmdja095R1FiNjFRenBRcGtLOGdOeVVZRjBGU3RWcW11VTVJa3FUa0QzYmpuaEs3LzJXL2wwMy93YzdQWlEzOVlwUEEwVXJ5NG51NEtXQXFiL0RKRmxLL05VU2UzZ3BVWDRjREx2aTd0dU80bXJGeHhBMWFlaVl3bHFDWGlzK3BjdGN1UWhJUlVWNEtLS2pVZ0NZUUt0RFdhUTF0QU80SE9pUzRMdEhPWE9ZZmJQeFd4dzVRRDE0a2tzcG9JcWhHbG1naFNSYWxHZ21wQ1NaWFZ1VkxOQ3VxY3RiUktGVVUxZ1ZUamxGSlZWVkpEWncrQjA3dk9ZdmJnYlRqLzBWczN6OTcyTmdBUEFIQWx2em41QWRCUEc4V0w2K211Z0k5MkdVYmtyWXArYjVjUlJ0ZThZSG5YTlRkejlWazNvRDc0c21yMTByRXVIWVJpRExJRnRhRjJiWWJPSGZoREtFa00wMVZBcXZ4VWllUTdSUSt5WmtncUVtc3U2UmsxTHhTRVFsUXRsMHlsNU00VGRZbTJFM1dkVWoxSm95UytOdXNzTUh1WTBPbnR1djZCUDJuUDNmV3J6ZnJiL3hUQWZhVjdONzZodHJPY24xcFI3U2Q2L1VWVndPR1ZjT1BoaE50K3NMc0lpMTg5WHZ2ODUzTnk1VitYYlZmdGtORzJWNDFHNDB2UzZpWElrMzBnYXFoMjBKekJiSGlOdHZaUllSVmJodWY4VkNRQlFCR2txT2VyazFkYkpTQWxrVFFTUWxOS0lpS215SklTMERhUTdqUTRQMEZ0ejM2SXN6TnZ6NXYzdkw4NTgvYi9CbXpjQTJCcXpSWGcwQzlYT0hvVVQxZHI5MGpYWHdZRmpFdUF3NElia1hEYkxibWNPdGxmZTdZQisyVHZEVjlNMmZuTWF0ZXo5NmJSbnBkbTVmNk1lbHlOZDFZeTJwNmtXZ0prQ1pDcXIrc3JoYzRKeWZiL1FHeTJKSGtPNWN4Mmh1MDJGVHJ0Tk04M0lPbjliRS9mcFp0M25hNmFyZHVhMDI5Ny94dzRCV0Jqb2NtOXBmc0xvM1RENnkrVEFnNHZzWjlEZ2h1dkUreS9uamo2dFRrS0F3ZlhOZ0RMeThDb1hudk9tbTUvNW1XcGE2NG04eVZTalNhUWFrUkZuV1FrbWFLKythNklhS2M1ZDZwdGxqeHRrT1Zod2V3WXVvZFBibTdlZnhiQURNRHBqMjBTWE9IZUN1QW1CWTU4VElQK29sMS9XUlh3a1M1WHloc1RibmlPWU50QjRtMy8xQ3RFdkZidkNYbUt3MFROZ3B0dXFiRHhrT0JkWjlVdDNGOTRoYnY0K3Y4QnZtOGtBcGI3dFRrQUFBQUFTVVZPUks1Q1lJST0iIGFsdD0iRmFjZWJvb2siIGNsYXNzPSJvcHQtaWNvbi1pbWciPjwvZGl2PgogICAgPGRpdiBjbGFzcz0ib3B0LXRleHQiPjxkaXYgY2xhc3M9Im9wdC10aXRsZSI+RmFjZWJvb2s8L2Rpdj48ZGl2IGNsYXNzPSJvcHQtaGFuZGxlIj5SJmFtcDtKIEdyb29taW5nPC9kaXY+PC9kaXY+CiAgICA8c3BhbiBjbGFzcz0ib3B0LWFycm93Ij7ihpI8L3NwYW4+CiAgPC9hPgogIDxidXR0b24gY2xhc3M9Im9wdCIgb25jbGljaz0id2luZG93LmxvY2F0aW9uLmhyZWY9J3RlbDorMzcyNTg3MzU0NTYnIj4KICAgIDxkaXYgY2xhc3M9Im9wdC1pY29uIj48c3ZnIHdpZHRoPSIzMiIgaGVpZ2h0PSIzMiIgdmlld0JveD0iMCAwIDI0IDI0IiBmaWxsPSJub25lIiBzdHJva2U9InJnYmEoMjU1LDI1NSwyNTUsLjQ1KSIgc3Ryb2tlLXdpZHRoPSIxLjYiPjxwYXRoIGQ9Ik0yMiAxNi45MnYzYTIgMiAwIDAxLTIuMTggMiAxOS43OSAxOS43OSAwIDAxLTguNjMtMy4wN0ExOS41IDE5LjUgMCAwMTMuMDcgOS44MmExOS43OSAxOS43OSAwIDAxLTMuMDctOC42N0EyIDIgMCAwMTIgMWgzYTIgMiAwIDAxMiAxLjcyYy4xMjcuOTYuMzYxIDEuOTAzLjcgMi44MWEyIDIgMCAwMS0uNDUgMi4xMUw2LjkxIDguOTFhMTYgMTYgMCAwMDYgNmwxLjI3LTEuMjdhMiAyIDAgMDEyLjExLS40NWMuOTA3LjMzOSAxLjg1LjU3MyAyLjgxLjdBMiAyIDAgMDEyMiAxNi45MnoiLz48L3N2Zz48L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im9wdC10ZXh0Ij48ZGl2IGNsYXNzPSJvcHQtdGl0bGUiIGRhdGEtaTE4bj0iY2FsbF91cyI+Q2FsbCBVczwvZGl2PjxkaXYgY2xhc3M9Im9wdC1oYW5kbGUiPiszNzIgNTg3IDM1NDU2PC9kaXY+PC9kaXY+CiAgICA8c3BhbiBjbGFzcz0ib3B0LWFycm93Ij7ihpI8L3NwYW4+CiAgPC9idXR0b24+CiAgPGRpdiBjbGFzcz0iaG9tZS1mb290Ij4KICAgIDxzcGFuPlRhbGxpbm48L3NwYW4+PGRpdiBjbGFzcz0iZmRvdCI+PC9kaXY+PHNwYW4+RXN0b25pYTwvc3Bhbj48ZGl2IGNsYXNzPSJmZG90Ij48L2Rpdj48c3Bhbj5BbGx2ZWVsYWV2YSA0PC9zcGFuPgogIDwvZGl2Pgo8L2Rpdj4KPC9kaXY+Cgo8IS0tIEJPT0tJTkcgLS0+CjxkaXYgY2xhc3M9InNjcmVlbiIgaWQ9ImJvb2tTY3JlZW4iPgo8ZGl2IGNsYXNzPSJjb24iPgogIDxidXR0b24gY2xhc3M9ImJhY2stYnRuIiBpZD0iYmFja0J0biIgZGF0YS1pMThuPSJiYWNrIj7ihpAg0J3QsNC30LDQtDwvYnV0dG9uPgogIDxkaXYgY2xhc3M9ImxvZ28tcmoiPlImYW1wO0o8L2Rpdj4KICA8ZGl2IGNsYXNzPSJsb2dvLXN1YiIgZGF0YS1pMThuPSJsb2dvX3N1YiI+R3Jvb21pbmcgwrcg0KLQsNC70LvQuNC9PC9kaXY+CiAgPGRpdiBjbGFzcz0icHJvZ3Jlc3MiPgogICAgPGRpdiBjbGFzcz0icHMgYWN0aXZlIiBpZD0icHMxIj48ZGl2IGNsYXNzPSJwZG90Ij48L2Rpdj48c3BhbiBkYXRhLWkxOG49InBzX3NlcnZpY2UiPtCj0YHQu9GD0LPQsDwvc3Bhbj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBsIiBpZD0icGwxIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBzIiBpZD0icHMyIj48ZGl2IGNsYXNzPSJwZG90Ij48L2Rpdj48c3BhbiBkYXRhLWkxOG49InBzX21hc3RlciI+0JzQsNGB0YLQtdGAPC9zcGFuPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icGwiIGlkPSJwbDIiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icHMiIGlkPSJwczMiPjxkaXYgY2xhc3M9InBkb3QiPjwvZGl2PjxzcGFuIGRhdGEtaTE4bj0icHNfcGV0Ij7Qn9C40YLQvtC80LXRhjwvc3Bhbj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBsIiBpZD0icGwzIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBzIiBpZD0icHM0Ij48ZGl2IGNsYXNzPSJwZG90Ij48L2Rpdj48c3BhbiBkYXRhLWkxOG49InBzX2RhdGUiPtCU0LDRgtCwPC9zcGFuPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icGwiIGlkPSJwbDQiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icHMiIGlkPSJwczUiPjxkaXYgY2xhc3M9InBkb3QiPjwvZGl2PjxzcGFuIGRhdGEtaTE4bj0icHNfZGV0YWlscyI+0JTQsNC90L3Ri9C1PC9zcGFuPjwvZGl2PgogIDwvZGl2PgoKICA8IS0tIFN0ZXAgMSAtLT4KICA8ZGl2IGNsYXNzPSJzdGVwIHNob3ciIGlkPSJiazEiPgogICAgPGRpdiBjbGFzcz0ic2xibCIgZGF0YS1pMThuPSJzdGVwMV9sYmwiPjAxIMK3INCf0L7RgNC+0LTQsDwvZGl2PgogICAgPGRpdiBjbGFzcz0iYndyYXAiPgogICAgICA8ZGl2IGNsYXNzPSJzYm94Ij4KICAgICAgICA8c3BhbiBjbGFzcz0ic2kiPvCflI08L3NwYW4+CiAgICAgICAgPGlucHV0IGlkPSJiSW5wdXQiIHR5cGU9InRleHQiIHBsYWNlaG9sZGVyPSLQndCw0YfQvdC40YLQtSDQstCy0L7QtNC40YLRjCDQv9C+0YDQvtC00YMuLi4iIGRhdGEtaTE4bi1waD0iYnJlZWRfcGgiIGF1dG9jb21wbGV0ZT0ib2ZmIj4KICAgICAgICA8YnV0dG9uIGNsYXNzPSJjbHIiIGlkPSJjbHJCdG4iPuKclTwvYnV0dG9uPgogICAgICA8L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0iZHJvcCIgaWQ9ImJEcm9wIj48L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0ic2JhZGdlIiBpZD0ic0JhZGdlIj48L2Rpdj4KICAgIDxkaXYgaWQ9InN2Y1NlYyIgc3R5bGU9ImRpc3BsYXk6bm9uZTttYXJnaW4tdG9wOjE2cHgiPgogICAgICA8ZGl2IGNsYXNzPSJzbGJsIiBpZD0ic3RlcDJMYmxFbCIgZGF0YS1pMThuPSJzdGVwMl9sYmwiPjAyIMK3INCj0YHQu9GD0LPQsDwvZGl2PgogICAgICA8ZGl2IGlkPSJzdmNMaXN0Ij48L2Rpdj4KICAgIDwvZGl2PgogIDwvZGl2PgoKICA8IS0tIFN0ZXAgMiAtLT4KICA8ZGl2IGNsYXNzPSJzdGVwIiBpZD0iYmsyIj4KICAgIDxkaXYgY2xhc3M9InNsYmwiIGRhdGEtaTE4bj0ic3RlcDJfbWFzdGVyIj7QktGL0LHQtdGA0LjRgtC1INC80LDRgdGC0LXRgNCwPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJ3aGVlbC13cmFwIj4KICAgICAgPGRpdiBjbGFzcz0id2hlZWwtaGlnaGxpZ2h0Ij48L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0id2hlZWwiIGlkPSJtYXN0ZXJXaGVlbCI+CiAgICAgICAgPGRpdiBjbGFzcz0id2hlZWwtcGFkIj48L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJtYnRuIiBkYXRhLW1hc3Rlcj0i0KLQsNGC0YzRj9C90LAiPjxkaXYgY2xhc3M9Im1uYW1lIj7QotCw0YLRjNGP0L3QsDwvZGl2PjwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9Im1idG4iIGRhdGEtbWFzdGVyPSLQkNC70LXQutGB0LDQvdC00YDQsCI+PGRpdiBjbGFzcz0ibW5hbWUiPtCQ0LvQtdC60YHQsNC90LTRgNCwPC9kaXY+PC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0ibWJ0biIgZGF0YS1tYXN0ZXI9ItCa0YHQtdC90LjRjyI+PGRpdiBjbGFzcz0ibW5hbWUiPtCa0YHQtdC90LjRjzwvZGl2PjwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9Im1idG4iIGRhdGEtbWFzdGVyPSLQkNC90L3QsCI+PGRpdiBjbGFzcz0ibW5hbWUiPtCQ0L3QvdCwPC9kaXY+PC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0ibWJ0biIgZGF0YS1tYXN0ZXI9ItCQ0LvQuNGB0LAiPjxkaXYgY2xhc3M9Im1uYW1lIj7QkNC70LjRgdCwPC9kaXY+PC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0ibWJ0biIgZGF0YS1tYXN0ZXI9ItCa0YDQuNGB0YLQuNC90LAiPjxkaXYgY2xhc3M9Im1uYW1lIj7QmtGA0LjRgdGC0LjQvdCwPC9kaXY+PC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0id2hlZWwtcGFkIj48L2Rpdj4KICAgICAgPC9kaXY+CiAgICA8L2Rpdj4KICAgIDxidXR0b24gY2xhc3M9IndoZWVsLWNvbmZpcm0iIGlkPSJ3aGVlbENvbmZpcm1CdG4iPtCS0YvQsdGA0LDRgtGMIOKGkjwvYnV0dG9uPgogIDwvZGl2PgoKICA8IS0tIFN0ZXAgMyAtLT4KICA8ZGl2IGNsYXNzPSJzdGVwIiBpZD0iYmszIj4KICAgIDxkaXYgY2xhc3M9InNsYmwiIGRhdGEtaTE4bj0ic3RlcDNfbGJsIj7QmtCw0Log0LTQsNCy0L3QviDQstGLINC/0L7RgdC10YnQsNC70Lgg0LPRgNGD0LzQuNC90LM/PC9kaXY+CiAgICA8YnV0dG9uIGNsYXNzPSJnYnRuIiBkYXRhLXZhbD0i0J/QtdGA0LLRi9C5INGA0LDQtyIgZGF0YS1pMThuPSJnMSI+0J/QtdGA0LLRi9C5INGA0LDQtzwvYnV0dG9uPgogICAgPGJ1dHRvbiBjbGFzcz0iZ2J0biIgZGF0YS12YWw9ItCe0YIgMSDQtNC+IDMg0LzQtdGB0Y/RhtC10LIiIGRhdGEtaTE4bj0iZzIiPtCe0YIgMSDQtNC+IDMg0LzQtdGB0Y/RhtC10LI8L2J1dHRvbj4KICAgIDxidXR0b24gY2xhc3M9ImdidG4iIGRhdGEtdmFsPSLQntGCIDMg0LTQviA2INC80LXRgdGP0YbQtdCyIiBkYXRhLWkxOG49ImczIj7QntGCIDMg0LTQviA2INC80LXRgdGP0YbQtdCyPC9idXR0b24+CiAgICA8YnV0dG9uIGNsYXNzPSJnYnRuIiBkYXRhLXZhbD0i0JHQvtC70LXQtSA2INC80LXRgdGP0YbQtdCyIiBkYXRhLWkxOG49Imc0Ij7QkdC+0LvQtdC1IDYg0LzQtdGB0Y/RhtC10LI8L2J1dHRvbj4KICA8L2Rpdj4KCiAgPCEtLSBTdGVwIDQgLS0+CiAgPGRpdiBjbGFzcz0ic3RlcCIgaWQ9ImJrNCI+CiAgICA8ZGl2IGNsYXNzPSJzbGJsIiBkYXRhLWkxOG49InN0ZXA0X2xibCI+0JLRi9Cx0LXRgNC40YLQtSDQtNCw0YLRgzwvZGl2PgogICAgPGRpdiBjbGFzcz0iY2FsLWgiPgogICAgICA8YnV0dG9uIGNsYXNzPSJjYWwtbiIgaWQ9InByZXZNIj4mIzgyNDk7PC9idXR0b24+CiAgICAgIDxkaXYgY2xhc3M9ImNhbC1tIiBpZD0iY2FsTSI+PC9kaXY+CiAgICAgIDxidXR0b24gY2xhc3M9ImNhbC1uIiBpZD0ibmV4dE0iPiYjODI1MDs8L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0iY2ciIGlkPSJjYWxHIj48L2Rpdj4KICAgIDxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDtnYXA6MjBweDthbGlnbi1pdGVtczpjZW50ZXI7bWFyZ2luLXRvcDoxMnB4O3BhZGRpbmctdG9wOjEycHg7Ym9yZGVyLXRvcDoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMSk7ZmxleC13cmFwOndyYXA7Ij48ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo4cHg7Ij48ZGl2IHN0eWxlPSJ3aWR0aDoxNnB4O2hlaWdodDoxNnB4O2JvcmRlci1yYWRpdXM6NTAlO2JhY2tncm91bmQ6cmdiYSg5MCwxODAsOTAsLjE1KTtib3JkZXI6MXB4IHNvbGlkICM1YWI0NWE7ZmxleC1zaHJpbms6MDsiPjwvZGl2PjxzcGFuIHN0eWxlPSJmb250LXNpemU6MXJlbTtjb2xvcjojZmZmZmZmO2xldHRlci1zcGFjaW5nOi4wM2VtOyIgZGF0YS1pMThuPSJjYWxfYXZhaWwiPtCV0YHRgtGMINGB0LLQvtCx0L7QtNC90L7QtSDQstGA0LXQvNGPPC9zcGFuPjwvZGl2PjxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweDsiPjxkaXYgc3R5bGU9IndpZHRoOjE2cHg7aGVpZ2h0OjE2cHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNCk7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xKTtmbGV4LXNocmluazowOyI+PC9kaXY+PHNwYW4gc3R5bGU9ImZvbnQtc2l6ZToxcmVtO2NvbG9yOiNmZmZmZmY7bGV0dGVyLXNwYWNpbmc6LjAzZW07IiBkYXRhLWkxOG49ImNhbF9ub25lIj7QodCy0L7QsdC+0LTQvdC+0LPQviDQstGA0LXQvNC10L3QuCDQvdC10YI8L3NwYW4+PC9kaXY+PC9kaXY+CiAgICA8ZGl2IGlkPSJ0aW1lU2VjIiBzdHlsZT0iZGlzcGxheTpub25lO21hcmdpbi10b3A6MTZweCI+CiAgICAgIDxkaXYgY2xhc3M9InNsYmwiIGRhdGEtaTE4bj0ic3RlcDRfdGltZSI+0JLRi9Cx0LXRgNC40YLQtSDQstGA0LXQvNGPPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9InRnIiBpZD0idGltZUciPjwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IHN0eWxlPSJtYXJnaW4tdG9wOjIwcHg7cGFkZGluZy10b3A6MTZweDtib3JkZXItdG9wOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wOCk7dGV4dC1hbGlnbjpjZW50ZXIiPgogICAgICA8YnV0dG9uIGlkPSJjYWxsYmFja0J0biIgY2xhc3M9ImNiay1idG4iPtCd0LUg0L3QsNGI0LvQuCDRg9C00L7QsdC90L7QtSDQstGA0LXQvNGPPyDihpI8L2J1dHRvbj4KICAgIDwvZGl2PgogIDwvZGl2PgoKICA8IS0tIFN0ZXAgNSAtLT4KICA8ZGl2IGNsYXNzPSJzdGVwIiBpZD0iYms1Ij4KICAgIDxkaXYgY2xhc3M9InNsYmwiIGRhdGEtaTE4bj0ic3RlcDVfbGJsIj7QktCw0YjQuCDQtNCw0L3QvdGL0LU8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImZnIj48bGFiZWwgY2xhc3M9ImZsIiBkYXRhLWkxOG49ImxibF9uYW1lIj7QmNC80Y88L2xhYmVsPjxpbnB1dCBjbGFzcz0iZmkiIGlkPSJjTmFtZSIgdHlwZT0idGV4dCIgcGxhY2Vob2xkZXI9ItCS0LDRiNC1INC40LzRjyIgZGF0YS1pMThuLXBoPSJwaF9uYW1lIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImZnIj48bGFiZWwgY2xhc3M9ImZsIiBkYXRhLWkxOG49ImxibF9waG9uZSI+0KLQtdC70LXRhNC+0L08L2xhYmVsPjxpbnB1dCBjbGFzcz0iZmkiIGlkPSJjUGhvbmUiIHR5cGU9InRlbCIgcGxhY2Vob2xkZXI9IiszNzIgLi4uIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImZnIj48bGFiZWwgY2xhc3M9ImZsIiBkYXRhLWkxOG49ImxibF9lbWFpbCI+RW1haWw8L2xhYmVsPjxpbnB1dCBjbGFzcz0iZmkiIGlkPSJjRW1haWwiIHR5cGU9ImVtYWlsIiBwbGFjZWhvbGRlcj0iZW1haWxAZXhhbXBsZS5jb20iPjwvZGl2PgogICAgPGRpdiBjbGFzcz0iZmciPjxsYWJlbCBjbGFzcz0iZmwiIGRhdGEtaTE4bj0ibGJsX3BldCI+0JrQu9C40YfQutCwINC/0LjRgtC+0LzRhtCwPC9sYWJlbD48aW5wdXQgY2xhc3M9ImZpIiBpZD0iY1BldCIgdHlwZT0idGV4dCIgcGxhY2Vob2xkZXI9ItCd0LXQvtCx0Y/Qt9Cw0YLQtdC70YzQvdC+IiBkYXRhLWkxOG4tcGg9InBoX29wdGlvbmFsIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InN1bSIgaWQ9InN1bUJsb2NrIj48L2Rpdj4KICAgIDxidXR0b24gY2xhc3M9ImNidG4iIGlkPSJjb25maXJtQnRuIiBkYXRhLWkxOG49ImNvbmZpcm1fYnRuIj7Qn9C+0LTRgtCy0LXRgNC00LjRgtGMINC30LDQv9C40YHRjDwvYnV0dG9uPgogIDwvZGl2PgoKICA8IS0tIFN1Y2Nlc3MgLS0+CiAgPGRpdiBjbGFzcz0ic2Jsb2NrIiBpZD0ic3VjQmxvY2siPgogICAgPGRpdiBjbGFzcz0ic2kyIj7wn5C+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzdCIgZGF0YS1pMThuPSJzdWNjZXNzX3RpdGxlIj7Ql9Cw0L/QuNGB0Ywg0L/RgNC40L3Rj9GC0LAhPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzcyIgZGF0YS1pMThuPSJzdWNjZXNzX3N1YiI+0JzRiyDRgdCy0Y/QttC10LzRgdGPINGBINCy0LDQvNC4INC00LvRjyDQv9C+0LTRgtCy0LXRgNC20LTQtdC90LjRjy48YnI+0KHQv9Cw0YHQuNCx0L4sINGH0YLQviDQstGL0LHRgNCw0LvQuCBSJkogR3Jvb21pbmchPC9kaXY+CiAgICA8YnV0dG9uIGNsYXNzPSJoYnRuIiBpZD0iaG9tZUJ0biIgZGF0YS1pMThuPSJ0b19ob21lIj7ihpAg0J3QsCDQs9C70LDQstC90YPRjjwvYnV0dG9uPgogIDwvZGl2Pgo8L2Rpdj4KPC9kaXY+Cgo8ZGl2IGlkPSJjYmtNb2RhbCIgc3R5bGU9ImRpc3BsYXk6bm9uZTtwb3NpdGlvbjpmaXhlZDtpbnNldDowO2JhY2tncm91bmQ6cmdiYSgwLDAsMCwuNzUpO3otaW5kZXg6MzAwO2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO3BhZGRpbmc6MjBweCI+CiAgPGRpdiBzdHlsZT0iYmFja2dyb3VuZDojMGEwYTBhO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTIpO2JvcmRlci10b3A6MXB4IHNvbGlkICNmZmZmZmY7cGFkZGluZzoyOHB4IDI0cHg7d2lkdGg6MTAwJTttYXgtd2lkdGg6MzYwcHgiPgogICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjAuODM4cmVtO2xldHRlci1zcGFjaW5nOi4yZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbToxNnB4O2ZvbnQtd2VpZ2h0OjYwMDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZiI+0J7QsdGA0LDRgtC90YvQuSDQt9Cy0L7QvdC+0Lo8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImZnIj48bGFiZWwgY2xhc3M9ImZsIj7QmNC80Y88L2xhYmVsPjxpbnB1dCBjbGFzcz0iZmkiIGlkPSJjYmtOYW1lIiB0eXBlPSJ0ZXh0IiBwbGFjZWhvbGRlcj0i0JLQsNGI0LUg0LjQvNGPIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImZnIj4KICAgICAgPGxhYmVsIGNsYXNzPSJmbCI+0KLQtdC70LXRhNC+0L08L2xhYmVsPgogICAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6c3RyZXRjaDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xNSkiPgogICAgICAgIDxzcGFuIHN0eWxlPSJwYWRkaW5nOjEwcHggMTBweCAxMHB4IDA7Y29sb3I6I2ZmZmZmZjtmb250LXNpemU6MS4zNjNyZW07Ym9yZGVyLXJpZ2h0OjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xKTttYXJnaW4tcmlnaHQ6MTBweDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZiI+KzM3Mjwvc3Bhbj4KICAgICAgICA8aW5wdXQgaWQ9ImNia1Bob25lIiB0eXBlPSJ0ZWwiIHBsYWNlaG9sZGVyPSJYWFhYWFhYWCIgc3R5bGU9ImZsZXg6MTtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjpub25lO291dGxpbmU6bm9uZTtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjEuNDM4cmVtO2NvbG9yOiNmZmZmZmY7cGFkZGluZzoxMHB4IDAiPgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBpZD0iY2JrU3VjY2VzcyIgc3R5bGU9ImRpc3BsYXk6bm9uZTt0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjIwcHggMCI+CiAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToyLjg3NXJlbTttYXJnaW4tYm90dG9tOjEwcHg7b3BhY2l0eTouNSI+4pyTPC9kaXY+CiAgICAgIDxkaXYgc3R5bGU9ImZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6MS44NzVyZW07Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjZweCI+0JfQsNGP0LLQutCwINC/0YDQuNC90Y/RgtCwITwvZGl2PgogICAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6MS4wMzdyZW07Y29sb3I6I2ZmZmZmZjtsaW5lLWhlaWdodDoxLjY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWYiPtCc0Ysg0L/QtdGA0LXQt9Cy0L7QvdC40Lwg0LLQsNC8INCyINCx0LvQuNC20LDQudGI0LXQtSDQstGA0LXQvNGPPC9kaXY+CiAgICA8L2Rpdj4KICAgIDxidXR0b24gaWQ9ImNia1N1Ym1pdCIgY2xhc3M9ImNidG4iIHN0eWxlPSJtYXJnaW4tdG9wOjE0cHgiPtCe0YLQv9GA0LDQstC40YLRjDwvYnV0dG9uPgogICAgPGJ1dHRvbiBpZD0iY2JrQ2xvc2UiIHN0eWxlPSJkaXNwbGF5OmJsb2NrO3dpZHRoOjEwMCU7bWFyZ2luLXRvcDo4cHg7YmFja2dyb3VuZDpub25lO2JvcmRlcjpub25lO2NvbG9yOiNmZmZmZmY7Zm9udC1zaXplOjAuODM4cmVtO2xldHRlci1zcGFjaW5nOi4xMmVtO2N1cnNvcjpwb2ludGVyO3BhZGRpbmc6OHB4O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmIj7QntGC0LzQtdC90LA8L2J1dHRvbj4KICA8L2Rpdj4KPC9kaXY+Cgo8c2NyaXB0Pgp2YXIgREFUQSA9IFt7ImJyZWVkIjoi0JDQstGB0YLRgNCw0LvQuNC50YHQutCw0Y8g0L7QstGH0LDRgNC60LAgMTXigJMyNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6ODV9LCJicmVlZF9lbiI6IkF1c3RyYWxpYW4gU2hlcGhlcmQgMTXigJMyNSBrZyIsImJyZWVkX2V0IjoiQXVzdHJhYWxpYSBsYW1iYWtvZXIgMTXigJMyNSBrZyJ9LHsiYnJlZWQiOiLQkNCy0YHRgtGA0LDQu9C40LnRgdC60LDRjyDQvtCy0YfQsNGA0LrQsCAyNeKAkzM1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IkF1c3RyYWxpYW4gU2hlcGhlcmQgMjXigJMzNSBrZyIsImJyZWVkX2V0IjoiQXVzdHJhYWxpYSBsYW1iYWtvZXIgMjXigJMzNSBrZyJ9LHsiYnJlZWQiOiLQkNC60LjRgtCwLdC40L3RgyAyMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJBa2l0YSBJbnUgMjDigJM0MCBrZyIsImJyZWVkX2V0IjoiQWtpdGEgSW51IDIw4oCTNDAga2cifSx7ImJyZWVkIjoi0JDQutC40YLQsC3QuNC90YMg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkFraXRhIEludSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJBa2l0YSBJbnUgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQkNC60LjRgtCwLdC40L3RgyDRhNC70LDRhNGE0LggMjDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQWtpdGEgSW51IGZsdWZmeSAyMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBa2l0YSBJbnUgcGVobWVrYXJ2YWxpbmUgMjDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC60LjRgtCwLdC40L3RgyDRhNC70LDRhNGE0Lgg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkFraXRhIEludSBmbHVmZnkgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiQWtpdGEgSW51IHBlaG1la2FydmFsaW5lIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JDQu9Cw0LHQsNC5IDQw4oCTNjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJDZW50cmFsIEFzaWFuIFNoZXBoZXJkIDQw4oCTNjAga2ciLCJicmVlZF9ldCI6Iktlc2stQWFzaWEgbGFtYmFrb2VyIDQw4oCTNjAga2cifSx7ImJyZWVkIjoi0JDQu9Cw0LHQsNC5INCx0L7Qu9C10LUgNjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoxMDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjoxMTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMzB9LCJicmVlZF9lbiI6IkNlbnRyYWwgQXNpYW4gU2hlcGhlcmQgb3ZlciA2MCBrZyIsImJyZWVkX2V0IjoiS2Vzay1BYXNpYSBsYW1iYWtvZXIgw7xsZSA2MCBrZyJ9LHsiYnJlZWQiOiLQkNC70Y/RgdC60LjQvdGB0LrQuNC5INC80LDQu9Cw0LzRg9GCIDIw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkFsYXNrYW4gTWFsYW11dGUgMjDigJM0MCBrZyIsImJyZWVkX2V0IjoiQWxhc2thIG1hbGFtdXV0IDIw4oCTNDAga2cifSx7ImJyZWVkIjoi0JDQu9GP0YHQutC40L3RgdC60LjQuSDQvNCw0LvQsNC80YPRgiDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQWxhc2thbiBNYWxhbXV0ZSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJBbGFza2EgbWFsYW11dXQgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQkNC70Y/RgdC60LjQvdGB0LrQuNC5INC80LDQu9Cw0LzRg9GCINGE0LvQsNGE0YTQuCAyMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJBbGFza2FuIE1hbGFtdXRlIGZsdWZmeSAyMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBbGFza2EgbWFsYW11dXQgcGVobWVrYXJ2YWxpbmUgMjDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC70Y/RgdC60LjQvdGB0LrQuNC5INC80LDQu9Cw0LzRg9GCINGE0LvQsNGE0YTQuCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQWxhc2thbiBNYWxhbXV0ZSBmbHVmZnkgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiQWxhc2thIG1hbGFtdXV0IHBlaG1la2FydmFsaW5lIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JDQvNC10YDQuNC60LDQvdGB0LrQsNGPINCw0LrQuNGC0LAgMjDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQW1lcmljYW4gQWtpdGEgMjDigJM0MCBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2EgQWtpdGEgMjDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutCw0Y8g0LDQutC40YLQsCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQW1lcmljYW4gQWtpdGEgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2EgQWtpdGEgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutCw0Y8g0LDQutC40YLQsCDRhNC70LDRhNGE0LggMjDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQW1lcmljYW4gQWtpdGEgZmx1ZmZ5IDIw4oCTNDAga2ciLCJicmVlZF9ldCI6IkFtZWVyaWthIEFraXRhIHBlaG1la2FydmFsaW5lIDIw4oCTNDAga2cifSx7ImJyZWVkIjoi0JDQvNC10YDQuNC60LDQvdGB0LrQsNGPINCw0LrQuNGC0LAg0YTQu9Cw0YTRhNC4INCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBBa2l0YSBmbHVmZnkgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2EgQWtpdGEgcGVobWVrYXJ2YWxpbmUgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutC40Lkg0LrQvtC60LXRgC3RgdC/0LDQvdC40LXQu9GMIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MH0sImJyZWVkX2VuIjoiQW1lcmljYW4gQ29ja2VyIFNwYW5pZWwgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2Ega29rZXJzcGFuamVsIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0JDQvNC10YDQuNC60LDQvdGB0LrQuNC5INC60L7QutC10YAt0YHQv9Cw0L3QuNC10LvRjCAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODB9LCJicmVlZF9lbiI6IkFtZXJpY2FuIENvY2tlciBTcGFuaWVsIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IkFtZWVyaWthIGtva2Vyc3BhbmplbCAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LjQuSDRgdGC0LDRhNGE0L7RgNC00YjQuNGA0YHQutC40Lkg0YLQtdGA0YzQtdGAIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBTdGFmZm9yZHNoaXJlIFRlcnJpZXIgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2EgU3RhZmZvcmRzaGlyZSB0ZXJqZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutC40Lkg0YHRgtCw0YTRhNC+0YDQtNGI0LjRgNGB0LrQuNC5INGC0LXRgNGM0LXRgCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NX0sImJyZWVkX2VuIjoiQW1lcmljYW4gU3RhZmZvcmRzaGlyZSBUZXJyaWVyIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkFtZWVyaWthIFN0YWZmb3Jkc2hpcmUgdGVyamVyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JDQvdCz0LvQuNC50YHQutC40Lkg0LHRg9C70YzQtNC+0LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjcwfSwiYnJlZWRfZW4iOiJFbmdsaXNoIEJ1bGxkb2ciLCJicmVlZF9ldCI6IkluZ2xpc2UgYnVsZG9nIn0seyJicmVlZCI6ItCQ0L3Qs9C70LjQudGB0LrQuNC5INC60L7QutC10YAt0YHQv9Cw0L3QuNC10LvRjCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IkVuZ2xpc2ggQ29ja2VyIFNwYW5pZWwgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiSW5nbGlzZSBrb2tlcnNwYW5qZWwgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQkNC90LPQu9C40LnRgdC60LjQuSDQutC+0LrQtdGALdGB0L/QsNC90LjQtdC70YwgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwfSwiYnJlZWRfZW4iOiJFbmdsaXNoIENvY2tlciBTcGFuaWVsIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IkluZ2xpc2Uga29rZXJzcGFuamVsIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0JDRhNCz0LDQvSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTB9LCJicmVlZF9lbiI6IkFmZ2hhbiBIb3VuZCAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJBZmdhbmlzdGFuaSBrb2VyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JDRhNCz0LDQvSAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwfSwiYnJlZWRfZW4iOiJBZmdoYW4gSG91bmQgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiQWZnYW5pc3Rhbmkga29lciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCR0LDRgdGB0LXRgi3RhdCw0YPQvdC0IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJCYXNzZXQgSG91bmQgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiQmFzc2V0aG91bmQgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQkdCw0YHRgdC10YIt0YXQsNGD0L3QtCAzMOKAkzM1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NX0sImJyZWVkX2VuIjoiQmFzc2V0IEhvdW5kIDMw4oCTMzUga2ciLCJicmVlZF9ldCI6IkJhc3NldGhvdW5kIDMw4oCTMzUga2cifSx7ImJyZWVkIjoi0JHQtdGA0L3RgdC60LjQuSDQt9C10L3QvdC10L3RhdGD0L3QtCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTEwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJCZXJuZXNlIE1vdW50YWluIERvZyAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJCZXJuaSBtw6RnaWtvZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkdC10YDQvdGB0LrQuNC5INC30LXQvdC90LXQvdGF0YPQvdC0INCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMzAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkJlcm5lc2UgTW91bnRhaW4gRG9nIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkJlcm5pIG3DpGdpa29lciDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCR0LjQstC10YAt0LnQvtGA0Log0LHQvtC70LXQtSAzLDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiQmlld2VyIFlvcmtzaGlyZSBUZXJyaWVyIG92ZXIgMyw1IGtnIiwiYnJlZWRfZXQiOiJCaWV3ZXIgWW9ya3NoaXJlIFRlcnJpZXIgw7xsZSAzLDUga2cifSx7ImJyZWVkIjoi0JHQuNCy0LXRgC3QudC+0YDQuiDQtNC+IDMsNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJCaWV3ZXIgWW9ya3NoaXJlIFRlcnJpZXIgdXAgdG8gMyw1IGtnIiwiYnJlZWRfZXQiOiJCaWV3ZXIgWW9ya3NoaXJlIFRlcnJpZXIga3VuaSAzLDUga2cifSx7ImJyZWVkIjoi0JHQuNCz0LvRjCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjYwfSwiYnJlZWRfZW4iOiJCZWFnbGUgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiQmlpZ2VsIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0JHQuNCz0LvRjCAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjY1fSwiYnJlZWRfZW4iOiJCZWFnbGUgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiQmlpZ2VsIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0JHQuNGI0L7QvS3RhNGA0LjQt9C1IDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJCaWNob24gRnJpc8OpIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiQmnFoW9uIEZyaXPDqSA14oCTMTAga2cifSx7ImJyZWVkIjoi0JHQuNGI0L7QvS3RhNGA0LjQt9C1INC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJCaWNob24gRnJpc8OpIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IkJpxaFvbiBGcmlzw6kga3VuaSA1IGtnIn0seyJicmVlZCI6ItCR0L7QutGB0LXRgCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MH0sImJyZWVkX2VuIjoiQm94ZXIgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiQm9rc2VyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JHQvtC60YHQtdGAIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJCb3hlciAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJCb2tzZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkdC+0YDQtNC10YAt0LrQvtC70LvQuCAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4MH0sImJyZWVkX2VuIjoiQm9yZGVyIENvbGxpZSAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiJCb3JkZXJrb2xsIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0JHQvtGA0LTQtdGALdC60L7Qu9C70LggMjDigJMyNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJCb3JkZXIgQ29sbGllIDIw4oCTMjUga2ciLCJicmVlZF9ldCI6IkJvcmRlcmtvbGwgMjDigJMyNSBrZyJ9LHsiYnJlZWQiOiLQkdC+0YHRgtC+0L0t0YLQtdGA0YzQtdGAIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1fSwiYnJlZWRfZW4iOiJCb3N0b24gVGVycmllciAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJCb3N0b25pIHRlcmplciAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCR0L7RgdGC0L7QvS3RgtC10YDRjNC10YAgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MH0sImJyZWVkX2VuIjoiQm9zdG9uIFRlcnJpZXIgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJCb3N0b25pIHRlcmplciA14oCTMTAga2cifSx7ImJyZWVkIjoi0JHRgNCw0LHQsNC90YHQvtC9Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo1MH0sImJyZWVkX2VuIjoiR3JpZmZvbiBCcnV4ZWxsb2lzIiwiYnJlZWRfZXQiOiJCcsO8c3NlbGkgZ3JpZm9uIn0seyJicmVlZCI6ItCR0YPQu9GM0YLQtdGA0YzQtdGAIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwfSwiYnJlZWRfZW4iOiJCdWxsIFRlcnJpZXIgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiQnVsbHRlcmplciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCS0LXQu9GM0Ygt0LrQvtGA0LPQuCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjY1fSwiYnJlZWRfZW4iOiJXZWxzaCBDb3JnaSAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJXYWxlc2kga29yZ2kgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQktC10LvRjNGILdC60L7RgNCz0LggMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo3NX0sImJyZWVkX2VuIjoiV2Vsc2ggQ29yZ2kgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiV2FsZXNpIGtvcmdpIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0JLQtdGB0YIt0YXQsNC50LvQtdC90LQt0LLQsNC50YIt0YLQtdGA0YzQtdGAIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiV2VzdCBIaWdobGFuZCBXaGl0ZSBUZXJyaWVyIiwiYnJlZWRfZXQiOiJMw6TDpG5lLcWgb3RpbWFhIHZhbGdlIHRlcmplciJ9LHsiYnJlZWQiOiLQktC+0YHRgtC+0YfQvdC+0YHQuNCx0LjRgNGB0LrQsNGPINC70LDQudC60LAgMTjigJMyNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4NX0sImJyZWVkX2VuIjoiRWFzdCBTaWJlcmlhbiBMYWlrYSAxOOKAkzI1IGtnIiwiYnJlZWRfZXQiOiJJZGEtU2liZXJpIGxhaWthIDE44oCTMjUga2cifSx7ImJyZWVkIjoi0JLQvtGB0YLQvtGH0L3QvtGB0LjQsdC40YDRgdC60LDRjyDQu9Cw0LnQutCwINCx0L7Qu9C10LUgMjUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJFYXN0IFNpYmVyaWFuIExhaWthIG92ZXIgMjUga2ciLCJicmVlZF9ldCI6IklkYS1TaWJlcmkgbGFpa2Egw7xsZSAyNSBrZyJ9LHsiYnJlZWQiOiLQk9C+0LvQtNC10L0t0YDQtdGC0YDQuNCy0LXRgCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkdvbGRlbiBSZXRyaWV2ZXIgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiS3VsZG5lIHJldHJpaXZlciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCT0L7Qu9C00LXQvS3RgNC10YLRgNC40LLQtdGAIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IkdvbGRlbiBSZXRyaWV2ZXIgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiS3VsZG5lIHJldHJpaXZlciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCT0YDQuNGE0YTQvtC9Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiR3JpZmZvbiIsImJyZWVkX2V0IjoiR3JpZm9uIn0seyJicmVlZCI6ItCU0LDQu9C80LDRgtC40L0iLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjcwfSwiYnJlZWRfZW4iOiJEYWxtYXRpYW4iLCJicmVlZF9ldCI6IkRhbG1hYXRzaWEga29lciJ9LHsiYnJlZWQiOiLQlNC20LXQui3RgNCw0YHRgdC10Lst0YLQtdGA0YzQtdGAINCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdGL0LkiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjUwfSwiYnJlZWRfZW4iOiJKYWNrIFJ1c3NlbGwgVGVycmllciBzbW9vdGgiLCJicmVlZF9ldCI6IkphY2sgUnVzc2VsbGkgdGVyamVyIGzDvGhpa2FydmFsaW5lIn0seyJicmVlZCI6ItCU0LbQtdC6LdGA0LDRgdGB0LXQuy3RgtC10YDRjNC10YAg0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90YvQuSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MCwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6IkphY2sgUnVzc2VsbCBUZXJyaWVyIHdpcmUtaGFpcmVkIiwiYnJlZWRfZXQiOiJKYWNrIFJ1c3NlbGxpIHRlcmplciBrYXJ1a2FydmFsaW5lIn0seyJicmVlZCI6ItCU0L7QsdC10YDQvNCw0L0gMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjU1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4MH0sImJyZWVkX2VuIjoiRG9iZXJtYW5uIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkRvYmVybWFubiAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCU0L7QsdC10YDQvNCw0L0g0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5NX0sImJyZWVkX2VuIjoiRG9iZXJtYW5uIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkRvYmVybWFubiDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCX0LDQv9Cw0LTQvdC+0YHQuNCx0LjRgNGB0LrQsNGPINC70LDQudC60LAgMTjigJMyNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4NX0sImJyZWVkX2VuIjoiV2VzdCBTaWJlcmlhbiBMYWlrYSAxOOKAkzI1IGtnIiwiYnJlZWRfZXQiOiJMw6TDpG5lLVNpYmVyaSBsYWlrYSAxOOKAkzI1IGtnIn0seyJicmVlZCI6ItCX0L7Qu9C+0YLQuNGB0YLRi9C5INGA0LXRgtGA0LjQstC10YAgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkdvbGRlbiBSZXRyaWV2ZXIgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiS3VsZG5lIHJldHJpaXZlciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCX0L7Qu9C+0YLQuNGB0YLRi9C5INGA0LXRgtGA0LjQstC10YAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjY1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjExMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExMH0sImJyZWVkX2VuIjoiR29sZGVuIFJldHJpZXZlciAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJLdWxkbmUgcmV0cmlpdmVyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JjRgNC70LDQvdC00YHQutC40Lkg0LzRj9Cz0LrQvtGI0LXRgNGB0YLQvdGL0Lkg0L/RiNC10L3QuNGH0L3Ri9C5INGC0LXRgNGM0LXRgCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4MH0sImJyZWVkX2VuIjoiSXJpc2ggU29mdCBDb2F0ZWQgV2hlYXRlbiBUZXJyaWVyIiwiYnJlZWRfZXQiOiJJaXJpIHBlaG1la2FydmFuZSBuaXN1dsOkcnZpIHRlcmplciJ9LHsiYnJlZWQiOiLQmNGA0LvQsNC90LTRgdC60LjQuSDRgtC10YDRjNC10YAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzAsItCi0YDQuNC80LzQuNC90LMiOjc1fSwiYnJlZWRfZW4iOiJJcmlzaCBUZXJyaWVyIiwiYnJlZWRfZXQiOiJJaXJpIHRlcmplciJ9LHsiYnJlZWQiOiLQmNGB0L/QsNC90YHQutC40Lkg0LPQsNC70YzQs9C+IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NzB9LCJicmVlZF9lbiI6IlNwYW5pc2ggR2FsZ28gMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiSGlzcGFhbmlhIGdhbGdvIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JjRgdC/0LDQvdGB0LrQuNC5INCz0LDQu9GM0LPQviAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjgwfSwiYnJlZWRfZW4iOiJTcGFuaXNoIEdhbGdvIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6Ikhpc3BhYW5pYSBnYWxnbyAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCZ0L7RgNC60YjQuNGA0YHQutC40Lkg0YLQtdGA0YzQtdGAINCx0L7Qu9C10LUgMyw1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IllvcmtzaGlyZSBUZXJyaWVyIG92ZXIgMyw1IGtnIiwiYnJlZWRfZXQiOiJZb3Jrc2hpcmUgdGVyamVyIMO8bGUgMyw1IGtnIn0seyJicmVlZCI6ItCZ0L7RgNC60YjQuNGA0YHQutC40Lkg0YLQtdGA0YzQtdGAINC00L4gMyw1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IllvcmtzaGlyZSBUZXJyaWVyIHVwIHRvIDMsNSBrZyIsImJyZWVkX2V0IjoiWW9ya3NoaXJlIHRlcmplciBrdW5pIDMsNSBrZyJ9LHsiYnJlZWQiOiLQmtCw0LLQsNC70LXRgC3QutC40L3Qsy3Rh9Cw0YDQu9GM0Lct0YHQv9Cw0L3QuNC10LvRjCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IkNhdmFsaWVyIEtpbmcgQ2hhcmxlcyBTcGFuaWVsIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IkNhdmFsaWVyIEtpbmcgQ2hhcmxlcyBTcGFuaWVsIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0JrQsNCy0LDQu9C10YAt0LrQuNC90LMt0YfQsNGA0LvRjNC3LdGB0L/QsNC90LjQtdC70YwgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkNhdmFsaWVyIEtpbmcgQ2hhcmxlcyBTcGFuaWVsIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiQ2F2YWxpZXIgS2luZyBDaGFybGVzIFNwYW5pZWwgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCa0LDQvdC1LdC60L7RgNGB0L4gNDDigJM2MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODV9LCJicmVlZF9lbiI6IkNhbmUgQ29yc28gNDDigJM2MCBrZyIsImJyZWVkX2V0IjoiQ2FuZSBDb3JzbyA0MOKAkzYwIGtnIn0seyJicmVlZCI6ItCa0LDQvdC1LdC60L7RgNGB0L4g0LHQvtC70LXQtSA2MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjkwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MTA1fSwiYnJlZWRfZW4iOiJDYW5lIENvcnNvIG92ZXIgNjAga2ciLCJicmVlZF9ldCI6IkNhbmUgQ29yc28gw7xsZSA2MCBrZyJ9LHsiYnJlZWQiOiLQmtCw0YDQtdC70L4t0YTQuNC90YHQutCw0Y8g0LvQsNC50LrQsCDQtNC+IDEzINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjY1fSwiYnJlZWRfZW4iOiJLYXJlbGlhbi1GaW5uaXNoIExhaWthIHVwIHRvIDEzIGtnIiwiYnJlZWRfZXQiOiJLYXJqYWxhLVNvb21lIGxhaWthIGt1bmkgMTMga2cifSx7ImJyZWVkIjoi0JrQuNGC0LDQudGB0LrQsNGPINGF0L7RhdC70LDRgtCw0Y8g0LPQvtC70LDRjyA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMiwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQyLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiQ2hpbmVzZSBDcmVzdGVkIGhhaXJsZXNzIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiSGlpbmEgaGFyamFrb2VyIGthcnZhdHUgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCa0LjRgtCw0LnRgdC60LDRjyDRhdC+0YXQu9Cw0YLQsNGPINCz0L7Qu9Cw0Y8g0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MjgsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjozNSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkNoaW5lc2UgQ3Jlc3RlZCBoYWlybGVzcyB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJIaWluYSBoYXJqYWtvZXIga2FydmF0dSBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JrQuNGC0LDQudGB0LrQsNGPINGF0L7RhdC70LDRgtCw0Y8g0L/Rg9GF0L7QstCw0Y8gNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkNoaW5lc2UgQ3Jlc3RlZCBwb3dkZXJwdWZmIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiSGlpbmEgaGFyamFrb2VyIFBvd2RlcnB1ZmYgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCa0LjRgtCw0LnRgdC60LDRjyDRhdC+0YXQu9Cw0YLQsNGPINC/0YPRhdC+0LLQsNGPINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJDaGluZXNlIENyZXN0ZWQgcG93ZGVycHVmZiB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJIaWluYSBoYXJqYWtvZXIgUG93ZGVycHVmZiBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JrQvtC60LDQv9GDIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjc1fSwiYnJlZWRfZW4iOiJDb2NrYXBvbyA14oCTMTAga2ciLCJicmVlZF9ldCI6IkNvY2thcG9vIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQmtC+0LrQsNC/0YMg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjV9LCJicmVlZF9lbiI6IkNvY2thcG9vIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IkNvY2thcG9vIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQmtC+0LvQu9C4IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQ29sbGllIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IktvbGwgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQmtC+0LvQu9C4IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IkNvbGxpZSAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJLb2xsIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JrQvtC80L7QvdC00L7RgCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTEwfSwiYnJlZWRfZW4iOiJLb21vbmRvciAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJLb21vbmRvciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCa0L7QvNC+0L3QtNC+0YAg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEzMH0sImJyZWVkX2VuIjoiS29tb25kb3Igb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiS29tb25kb3Igw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQm9Cw0LHRgNCw0LTQvtGAINCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdGL0LkgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo3MH0sImJyZWVkX2VuIjoiTGFicmFkb3IgUmV0cmlldmVyIHNtb290aCAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvcmkgcmV0cmlpdmVyIGzDvGhpa2FydmFsaW5lIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00L7RgCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3Ri9C5IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6ODB9LCJicmVlZF9lbiI6IkxhYnJhZG9yIFJldHJpZXZlciBzbW9vdGggMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiTGFicmFkb3JpIHJldHJpaXZlciBsw7xoaWthcnZhbGluZSAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNC+0YAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjk1fSwiYnJlZWRfZW4iOiJMYWJyYWRvciBSZXRyaWV2ZXIgc21vb3RoIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9yaSByZXRyaWl2ZXIgbMO8aGlrYXJ2YWxpbmUgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQm9Cw0LHRgNCw0LTQvtGAINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdGL0LkgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJMYWJyYWRvciBSZXRyaWV2ZXIgbG9uZy1jb2F0ZWQgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiTGFicmFkb3JpIHJldHJpaXZlciBwaWtrYXJ2YWxpbmUgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQm9Cw0LHRgNCw0LTQvtGAINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdGL0LkgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjExMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiTGFicmFkb3IgUmV0cmlldmVyIGxvbmctY29hdGVkIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9yaSByZXRyaWl2ZXIgcGlra2FydmFsaW5lIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00L7RgCDQtNC70LjQvdC90L7RiNC10YDRgdGC0L3Ri9C5INCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMzAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkxhYnJhZG9yIFJldHJpZXZlciBsb25nLWNvYXRlZCBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvcmkgcmV0cmlpdmVyIHBpa2thcnZhbGluZSDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNGD0LTQtdC70YwgMTDigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJMYWJyYWRvb2RsZSAxMOKAkzIwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvb2RsZSAxMOKAkzIwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNGD0LTQtdC70YwgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwfSwiYnJlZWRfZW4iOiJMYWJyYWRvb2RsZSAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvb2RsZSAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNGD0LTQtdC70YwgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMH0sImJyZWVkX2VuIjoiTGFicmFkb29kbGUgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiTGFicmFkb29kbGUgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQm9C10LLRgNC10YLQutCwIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDB9LCJicmVlZF9lbiI6Ikl0YWxpYW4gR3JleWhvdW5kIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiSXRhYWxpYSB2aW5ka29lciA14oCTMTAga2cifSx7ImJyZWVkIjoi0JvQtdCy0YDQtdGC0LrQsCDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1fSwiYnJlZWRfZW4iOiJJdGFsaWFuIEdyZXlob3VuZCB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJJdGFhbGlhIHZpbmRrb2VyIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQm9GF0LDRgdGB0LrQuNC5INCw0L/RgdC+IDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjc1fSwiYnJlZWRfZW4iOiJMaGFzYSBBcHNvIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiTGhhc2EgQXBzbyA14oCTMTAga2cifSx7ImJyZWVkIjoi0JvRhdCw0YHRgdC60LjQuSDQsNC/0YHQviDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2NX0sImJyZWVkX2VuIjoiTGhhc2EgQXBzbyB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJMaGFzYSBBcHNvIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQnNCw0LvRjNGC0LXQt9C1Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJNYWx0ZXNlIiwiYnJlZWRfZXQiOiJNYWx0YSBib2xvbmVlcyJ9LHsiYnJlZWQiOiLQnNCw0LvRjNGC0LjQudGB0LrQsNGPINCx0L7Qu9C+0L3QutCwIDXigJM4INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjV9LCJicmVlZF9lbiI6Ik1hbHRlc2UgQm9sb2duZXNlIDXigJM4IGtnIiwiYnJlZWRfZXQiOiJNYWx0YSBib2xvbmVlcyA14oCTOCBrZyJ9LHsiYnJlZWQiOiLQnNCw0LvRjNGC0LjQudGB0LrQsNGPINCx0L7Qu9C+0L3QutCwINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJNYWx0ZXNlIEJvbG9nbmVzZSB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJNYWx0YSBib2xvbmVlcyBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JzQsNC70YzRgtC40L/RgyAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6Ik1hbHRpcG9vIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6Ik1hbHRpcHV1IDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0JzQsNC70YzRgtC40L/RgyA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiTWFsdGlwb28gNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJNYWx0aXB1dSA14oCTMTAga2cifSx7ImJyZWVkIjoi0JzQsNC70YzRgtC40L/RgyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiTWFsdGlwb28gdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiTWFsdGlwdXUga3VuaSA1IGtnIn0seyJicmVlZCI6ItCc0LXRgtC40YEg0LrRgNGD0L/QvdGL0LkgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiTWl4ZWQgYnJlZWQgbGFyZ2UgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiU2VnYXZlcmQgc3V1ciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCc0LXRgtC40YEg0LrRgNGD0L/QvdGL0Lkg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjc1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEyMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEyMH0sImJyZWVkX2VuIjoiTWl4ZWQgYnJlZWQgbGFyZ2Ugb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiU2VnYXZlcmQgc3V1ciDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCc0LXRgtC40YEg0LzQtdC70LrQuNC5IDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJNaXhlZCBicmVlZCBzbWFsbCA14oCTMTAga2ciLCJicmVlZF9ldCI6IlNlZ2F2ZXJkIHbDpGlrZSA14oCTMTAga2cifSx7ImJyZWVkIjoi0JzQtdGC0LjRgSDQvNC10LvQutC40Lkg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MjUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjozNSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTB9LCJicmVlZF9lbiI6Ik1peGVkIGJyZWVkIHNtYWxsIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IlNlZ2F2ZXJkIHbDpGlrZSBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JzQtdGC0LjRgSDRgdGA0LXQtNC90LjQuSAxMOKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo3MH0sImJyZWVkX2VuIjoiTWl4ZWQgYnJlZWQgbWVkaXVtIDEw4oCTMjAga2ciLCJicmVlZF9ldCI6IlNlZ2F2ZXJkIGtlc2ttaW5lIDEw4oCTMjAga2cifSx7ImJyZWVkIjoi0JzQtdGC0LjRgSDRgdGA0LXQtNC90LjQuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4NX0sImJyZWVkX2VuIjoiTWl4ZWQgYnJlZWQgbWVkaXVtIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IlNlZ2F2ZXJkIGtlc2ttaW5lIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JzQuNGC0YLQtdC70YzRiNC90LDRg9GG0LXRgCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzAsItCi0YDQuNC80LzQuNC90LMiOjc1fSwiYnJlZWRfZW4iOiJTdGFuZGFyZCBTY2huYXV6ZXIgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiU3RhbmRhcmTFoW5hdXRzZXIgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQnNC40YLRgtC10LvRjNGI0L3QsNGD0YbQtdGAIDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4MCwi0KLRgNC40LzQvNC40L3QsyI6ODV9LCJicmVlZF9lbiI6IlN0YW5kYXJkIFNjaG5hdXplciAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiJTdGFuZGFyZMWhbmF1dHNlciAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCc0L7Qv9GBIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo1MH0sImJyZWVkX2VuIjoiUHVnIiwiYnJlZWRfZXQiOiJNb3BzIn0seyJicmVlZCI6ItCd0LXQstGB0LrQsNGPINC+0YDRhdC40LTQtdGPIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJOZXZhIE9yY2hpZCIsImJyZWVkX2V0IjoiTmVldmEgb3JoaWRlZSJ9LHsiYnJlZWQiOiLQndC10LzQtdGG0LrQsNGPINC+0LLRh9Cw0YDQutCwIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6Ikdlcm1hbiBTaGVwaGVyZCAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJTYWtzYSBsYW1iYWtvZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQndC10LzQtdGG0LrQsNGPINC+0LLRh9Cw0YDQutCwIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJHZXJtYW4gU2hlcGhlcmQgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiU2Frc2EgbGFtYmFrb2VyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0J3QtdC80LXRhtC60LDRjyDQvtCy0YfQsNGA0LrQsCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiR2VybWFuIFNoZXBoZXJkIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IlNha3NhIGxhbWJha29lciDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCo0LLQtdC50YbQsNGA0YHQutCw0Y8g0L7QstGH0LDRgNC60LAgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiU3dpc3MgU2hlcGhlcmQgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoixaB2ZWl0c2kgbGFtYmFrb2VyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0KjQstC10LnRhtCw0YDRgdC60LDRjyDQvtCy0YfQsNGA0LrQsCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiU3dpc3MgU2hlcGhlcmQgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoixaB2ZWl0c2kgbGFtYmFrb2VyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0KjQstC10LnRhtCw0YDRgdC60LDRjyDQvtCy0YfQsNGA0LrQsCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiU3dpc3MgU2hlcGhlcmQgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoixaB2ZWl0c2kgbGFtYmFrb2VyIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0J3QvtGA0LLQuNGHLdGC0LXRgNGM0LXRgCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MCwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6Ik5vcndpY2ggVGVycmllciIsImJyZWVkX2V0IjoiTm9yd2l0xaFpIHRlcmplciJ9LHsiYnJlZWQiOiLQndC+0YDRhNC+0LvQui3RgtC10YDRjNC10YAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJOb3Jmb2xrIFRlcnJpZXIiLCJicmVlZF9ldCI6Ik5vcmZvbGtpIHRlcmplciJ9LHsiYnJlZWQiOiLQndGM0Y7RhNCw0YPQvdC00LvQtdC90LQgNDDigJM2MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEzMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiTmV3Zm91bmRsYW5kIDQw4oCTNjAga2ciLCJicmVlZF9ldCI6Ik5ld2ZvdW5kbGFuZGkga29lciA0MOKAkzYwIGtnIn0seyJicmVlZCI6ItCd0YzRjtGE0LDRg9C90LTQu9C10L3QtCDQsdC+0LvQtdC1IDYwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MTAwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MTE1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxNTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMzB9LCJicmVlZF9lbiI6Ik5ld2ZvdW5kbGFuZCBvdmVyIDYwIGtnIiwiYnJlZWRfZXQiOiJOZXdmb3VuZGxhbmRpIGtvZXIgw7xsZSA2MCBrZyJ9LHsiYnJlZWQiOiLQn9Cw0L/QuNC50L7QvSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiUGFwaWxsb24iLCJicmVlZF9ldCI6IlBhcGlsbG9uIn0seyJicmVlZCI6ItCf0LXQutC40L3QtdGBIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJQZWtpbmdlc2UgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJQZWtpbmVzaSBrb2VyIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQn9C10LrQuNC90LXRgSDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiUGVraW5nZXNlIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IlBla2luZXNpIGtvZXIga3VuaSA1IGtnIn0seyJicmVlZCI6ItCf0YPQtNC10LvRjCDQsdC+0LvRjNGI0L7QuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTB9LCJicmVlZF9lbiI6IlN0YW5kYXJkIFBvb2RsZSAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJTdGFuZGFyZHB1dWRlbCAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCf0YPQtNC10LvRjCDQsdC+0LvRjNGI0L7QuSAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwfSwiYnJlZWRfZW4iOiJTdGFuZGFyZCBQb29kbGUgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiU3RhbmRhcmRwdXVkZWwgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQn9GD0LTQtdC70Ywg0LrQsNGA0LvQuNC60L7QstGL0LkgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6Ik1pbmlhdHVyZSBQb29kbGUgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJLw6TDpGJ1c3B1dWRlbCA14oCTMTAga2cifSx7ImJyZWVkIjoi0J/Rg9C00LXQu9GMINC80LDQu9GL0LkgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJTbWFsbCBQb29kbGUgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiVsOkaWtlIHB1dWRlbCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCf0YPQtNC10LvRjCDQvNCw0LvRi9C5IDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4MH0sImJyZWVkX2VuIjoiU21hbGwgUG9vZGxlIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IlbDpGlrZSBwdXVkZWwgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQn9GD0LTQtdC70Ywg0YLQvtC5INC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJUb3kgUG9vZGxlIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6Ik3DpG5ndWFzamEgcHV1ZGVsIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQoNC40LfQtdC90YjQvdCw0YPRhtC10YAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMCwi0KLRgNC40LzQvNC40L3QsyI6MTEwfSwiYnJlZWRfZW4iOiJHaWFudCBTY2huYXV6ZXIgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiU3V1csWhbmF1dHNlciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCg0LjQt9C10L3RiNC90LDRg9GG0LXRgCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTIwLCLQotGA0LjQvNC80LjQvdCzIjoxMjV9LCJicmVlZF9lbiI6IkdpYW50IFNjaG5hdXplciBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJTdXVyxaFuYXV0c2VyIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0KDRg9GB0YHQutCw0Y8g0YbQstC10YLQvdCw0Y8g0LHQvtC70L7QvdC60LAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IlJ1c3NpYW4gQ29sb3JlZCBMYXBkb2ciLCJicmVlZF9ldCI6IlZlbmUgdsOkcnZpbGluZSBzw7xsZWtvZXIifSx7ImJyZWVkIjoi0KDRg9GB0YHQutC40Lkg0L7RhdC+0YLQvdC40YfQuNC5INGB0L/QsNC90LjQtdC70YwgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJSdXNzaWFuIFNwYW5pZWwgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiVmVuZSBqYWhpc3BhbmplbCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCg0YPRgdGB0LrQuNC5INC+0YXQvtGC0L3QuNGH0LjQuSDRgdC/0LDQvdC40LXQu9GMIDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4MH0sImJyZWVkX2VuIjoiUnVzc2lhbiBTcGFuaWVsIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IlZlbmUgamFoaXNwYW5qZWwgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQoNGD0YHRgdC60LjQuSDRgtC+0Lkg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1fSwiYnJlZWRfZW4iOiJSdXNzaWFuIFRveSBzbW9vdGgiLCJicmVlZF9ldCI6IlZlbmUgVG95IGzDvGhpa2FydmFsaW5lIn0seyJicmVlZCI6ItCg0YPRgdGB0LrQuNC5INGC0L7QuSDQtNC70LjQvdC90L7RiNC10YDRgdGC0L3Ri9C5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJSdXNzaWFuIFRveSBsb25nLWNvYXRlZCIsImJyZWVkX2V0IjoiVmVuZSBUb3kgcGlra2FydmFsaW5lIn0seyJicmVlZCI6ItCg0YPRgdGB0LrQuNC5INGH0LXRgNC90YvQuSDRgtC10YDRjNC10YAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMH0sImJyZWVkX2VuIjoiQmxhY2sgUnVzc2lhbiBUZXJyaWVyIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6Ik11c3QgVmVuZSB0ZXJqZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQoNGD0YHRgdC60LjQuSDRh9C10YDQvdGL0Lkg0YLQtdGA0YzQtdGAINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMjB9LCJicmVlZF9lbiI6IkJsYWNrIFJ1c3NpYW4gVGVycmllciBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJNdXN0IFZlbmUgdGVyamVyIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0KDRg9GB0YHQutC+LdC10LLRgNC+0L/QtdC50YHQutCw0Y8g0LvQsNC50LrQsCAyMOKAkzI4INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJSdXNzaWFuLUV1cm9wZWFuIExhaWthIDIw4oCTMjgga2ciLCJicmVlZF9ldCI6IlZlbmUtRXVyb29wYSBsYWlrYSAyMOKAkzI4IGtnIn0seyJicmVlZCI6ItCh0LDQvNC+0LXQtCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJTYW1veWVkIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IlNhbW9qZWVkIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0KHQsNC80L7QtdC0IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJTYW1veWVkIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IlNhbW9qZWVkIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0KHQtdGC0YLQtdGAINCw0L3Qs9C70LjQudGB0LrQuNC5IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5MH0sImJyZWVkX2VuIjoiRW5nbGlzaCBTZXR0ZXIgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiSW5nbGlzZSBzZXR0ZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQodC10YLRgtC10YAg0LPQvtGA0LTQvtC9IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjgwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDB9LCJicmVlZF9lbiI6IkdvcmRvbiBTZXR0ZXIgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiR29yZG9uaSBzZXR0ZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQodC10YLRgtC10YAg0LjRgNC70LDQvdC00YHQutC40LkgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwfSwiYnJlZWRfZW4iOiJJcmlzaCBTZXR0ZXIgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiSWlyaSBzZXR0ZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQodC40LHQsC3QuNC90YMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjcwfSwiYnJlZWRfZW4iOiJTaGliYSBJbnUiLCJicmVlZF9ldCI6IlNoaWJhIEludSJ9LHsiYnJlZWQiOiLQodC40LvQuNGF0LXQvC3RgtC10YDRjNC10YAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJTZWFseWhhbSBUZXJyaWVyIiwiYnJlZWRfZXQiOiJTZWFseWhhbWkgdGVyamVyIn0seyJicmVlZCI6ItCh0LrQvtGC0Yct0YLQtdGA0YzQtdGAIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiU2NvdHRpc2ggVGVycmllciIsImJyZWVkX2V0IjoixaBvdGkgdGVyamVyIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90LDRjyDQutCw0YDQu9C40LrQvtCy0LDRjyA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NTB9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCBzbW9vdGggbWluaWF0dXJlIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiVGFrc2lrb2VyIGzDvGhpa2FydmFsaW5lIGvDpMOkYnVzIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdCw0Y8g0LrRgNC+0LvQuNGH0YzRjyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NDV9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCBzbW9vdGggcmFiYml0IHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBsw7xoaWthcnZhbGluZSBrw7zDvGxpayBrdW5pIDUga2cifSx7ImJyZWVkIjoi0KLQsNC60YHQsCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3QsNGPINGB0YLQsNC90LTQsNGA0YLQvdCw0Y8gMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2MH0sImJyZWVkX2VuIjoiRGFjaHNodW5kIHNtb290aCBzdGFuZGFyZCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIgbMO8aGlrYXJ2YWxpbmUgc3RhbmRhcmQgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdCw0Y8g0LrQsNGA0LvQuNC60L7QstCw0Y8gNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCBsb25nLWNvYXRlZCBtaW5pYXR1cmUgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIgcGlra2FydmFsaW5lIGvDpMOkYnVzIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdCw0Y8g0LrRgNC+0LvQuNGH0YzRjyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiRGFjaHNodW5kIGxvbmctY29hdGVkIHJhYmJpdCB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIgcGlra2FydmFsaW5lIGvDvMO8bGlrIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdCw0Y8g0YHRgtCw0L3QtNCw0YDRgtC90LDRjyAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCBsb25nLWNvYXRlZCBzdGFuZGFyZCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIgcGlra2FydmFsaW5lIHN0YW5kYXJkIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KLQsNC60YHQsCDQttC10YHRgtC60L7RiNC10YDRgdGC0L3QsNGPINC60LDRgNC70LjQutC+0LLQsNGPIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiRGFjaHNodW5kIHdpcmUtaGFpcmVkIG1pbmlhdHVyZSA14oCTMTAga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBrYXJ1a2FydmFsaW5lIGvDpMOkYnVzIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINC20LXRgdGC0LrQvtGI0LXRgNGB0YLQvdCw0Y8g0LrRgNC+0LvQuNGH0YzRjyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NSwi0KLRgNC40LzQvNC40L3QsyI6NTV9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCB3aXJlLWhhaXJlZCByYWJiaXQgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiVGFrc2lrb2VyIGthcnVrYXJ2YWxpbmUga8O8w7xsaWsga3VuaSA1IGtnIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90LDRjyDRgdGC0LDQvdC00LDRgNGC0L3QsNGPIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MCwi0KLRgNC40LzQvNC40L3QsyI6NzV9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCB3aXJlLWhhaXJlZCBzdGFuZGFyZCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIga2FydWthcnZhbGluZSBzdGFuZGFyZCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCj0LjQv9C/0LXRgiAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NX0sImJyZWVkX2VuIjoiV2hpcHBldCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJXaGlwcGV0IDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KPQuNC/0L/QtdGCIDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwfSwiYnJlZWRfZW4iOiJXaGlwcGV0IDE14oCTMjAga2ciLCJicmVlZF9ldCI6IldoaXBwZXQgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQpNC40L3RgdC60LjQuSDQu9Cw0L/RhdGD0L3QtCAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODV9LCJicmVlZF9lbiI6IkZpbm5pc2ggTGFwcGh1bmQgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiU29vbWUgbGFtYmFrb2VyIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0KTQuNC90YHQutC40Lkg0LvQsNC/0YXRg9C90LQgMjDigJMyNCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjU1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwfSwiYnJlZWRfZW4iOiJGaW5uaXNoIExhcHBodW5kIDIw4oCTMjQga2ciLCJicmVlZF9ldCI6IlNvb21lIGxhbWJha29lciAyMOKAkzI0IGtnIn0seyJicmVlZCI6ItCk0L7QutGB0YLQtdGA0YzQtdGAINC20LXRgdGC0LrQvtGI0LXRgNGB0YLQvdGL0LkgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwLCLQotGA0LjQvNC80LjQvdCzIjo3NX0sImJyZWVkX2VuIjoiV2lyZSBGb3ggVGVycmllciAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJLYXJ1a2FydmFsaW5lIGZveHRlcmplciAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCk0L7QutGB0YLQtdGA0YzQtdGAINC20LXRgdGC0LrQvtGI0LXRgNGB0YLQvdGL0LkgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJXaXJlIEZveCBUZXJyaWVyIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiS2FydWthcnZhbGluZSBmb3h0ZXJqZXIgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCk0YDQsNC90YbRg9C30YHQutC40Lkg0LHRg9C70YzQtNC+0LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjYwfSwiYnJlZWRfZW4iOiJGcmVuY2ggQnVsbGRvZyIsImJyZWVkX2V0IjoiUHJhbnRzdXNlIGJ1bGRvZyJ9LHsiYnJlZWQiOiLQpdCw0YHQutC4IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IlNpYmVyaWFuIEh1c2t5IDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IlNpYmVyaSBodXNreSAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCl0LDRgdC60LggMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IlNpYmVyaWFuIEh1c2t5IDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IlNpYmVyaSBodXNreSAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCm0LLQtdGA0LPRiNC90LDRg9GG0LXRgCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzAsItCi0YDQuNC80LzQuNC90LMiOjc1fSwiYnJlZWRfZW4iOiJNaW5pYXR1cmUgU2NobmF1emVyIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IkvDpMOkYnVzxaFuYXV0c2VyIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KbQstC10YDQs9GI0L3QsNGD0YbQtdGAIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiTWluaWF0dXJlIFNjaG5hdXplciA14oCTMTAga2ciLCJicmVlZF9ldCI6IkvDpMOkYnVzxaFuYXV0c2VyIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQp9Cw0YMt0YfQsNGDIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQ2hvdyBDaG93IDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkNob3cgQ2hvdyAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCn0LDRgy3Rh9Cw0YMgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjExMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiQ2hvdyBDaG93IDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkNob3cgQ2hvdyAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCn0LjRhdGD0LDRhdGD0LAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1fSwiYnJlZWRfZW4iOiJDaGlodWFodWEgc21vb3RoIiwiYnJlZWRfZXQiOiJUxaFpaHVhaHVhIGzDvGhpa2FydmFsaW5lIn0seyJicmVlZCI6ItCn0LjRhdGD0LDRhdGD0LAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90YvQuSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiQ2hpaHVhaHVhIGxvbmctY29hdGVkIiwiYnJlZWRfZXQiOiJUxaFpaHVhaHVhIHBpa2thcnZhbGluZSJ9LHsiYnJlZWQiOiLQqNCw0YDQv9C10LkgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2NX0sImJyZWVkX2VuIjoiU2hhciBQZWkgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoixaBhci1QZWkgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQqNCw0YDQv9C10LkgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo3MH0sImJyZWVkX2VuIjoiU2hhciBQZWkgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoixaBhci1QZWkgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQqNC10LvRgtC4Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjB9LCJicmVlZF9lbiI6IlNoZXRsYW5kIFNoZWVwZG9nIiwiYnJlZWRfZXQiOiLFoGV0bGFuZGkgbGFtYmFrb2VyIn0seyJicmVlZCI6ItCo0Lgt0YLRhtGDIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJTaGloIFR6dSA14oCTMTAga2ciLCJicmVlZF9ldCI6IlNoaWggVHp1IDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQqNC4LdGC0YbRgyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiU2hpaCBUenUgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiU2hpaCBUenUga3VuaSA1IGtnIn0seyJicmVlZCI6ItCo0L3QsNGD0YbQtdGAINC80LjQvdC40LDRgtGO0YDQvdGL0Lkg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6Ik1pbmlhdHVyZSBTY2huYXV6ZXIgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiS8Okw6RidXPFoW5hdXRzZXIga3VuaSA1IGtnIn0seyJicmVlZCI6ItCo0L/QuNGGINC90LXQvNC10YbQutC40LkgLyDQv9C+0LzQtdGA0LDQvdGB0LrQuNC5INCx0L7Qu9C10LUgMyw1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2MH0sImJyZWVkX2VuIjoiR2VybWFuIFNwaXR6IC8gUG9tZXJhbmlhbiBvdmVyIDMsNSBrZyIsImJyZWVkX2V0IjoiU2Frc2Egc3BpdHMgLyBQb21lcmFuaWFuIMO8bGUgMyw1IGtnIn0seyJicmVlZCI6ItCo0L/QuNGGINC90LXQvNC10YbQutC40LkgLyDQv9C+0LzQtdGA0LDQvdGB0LrQuNC5INC00L4gMyw1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo1NX0sImJyZWVkX2VuIjoiR2VybWFuIFNwaXR6IC8gUG9tZXJhbmlhbiB1cCB0byAzLDUga2ciLCJicmVlZF9ldCI6IlNha3NhIHNwaXRzIC8gUG9tZXJhbmlhbiBrdW5pIDMsNSBrZyJ9LHsiYnJlZWQiOiLQqNC/0LjRhiDRj9C/0L7QvdGB0LrQuNC5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjB9LCJicmVlZF9lbiI6IkphcGFuZXNlIFNwaXR6IiwiYnJlZWRfZXQiOiJKYWFwYW5pIHNwaXRzIn0seyJicmVlZCI6ItCp0LXQvdC60LgiLCJzZXJ2aWNlcyI6eyLQktGB0Y8g0L/RgNC+0LPRgNCw0LzQvNCwIjo1NX0sImJyZWVkX2VuIjoiUHVwcGllcyIsImJyZWVkX2V0IjoiS3V0c2lrYWQifSx7ImJyZWVkIjoi0K3RgdGC0L7QvdGB0LrQsNGPINCz0L7QvdGH0LDRjyAxNeKAkzI1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MH0sImJyZWVkX2VuIjoiRXN0b25pYW4gSG91bmQgMTXigJMyNSBrZyIsImJyZWVkX2V0IjoiRWVzdGkgaGFnaWphcyAxNeKAkzI1IGtnIn0seyJicmVlZCI6ItCv0L/QvtC90YHQutC40Lkg0YXQuNC9Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJKYXBhbmVzZSBDaGluIiwiYnJlZWRfZXQiOiJKYWFwYW5pIENoaW4ifSx7ImJyZWVkIjoi0JrQvtGI0LrQsCDQutC+0YDQvtGC0LrQvtGI0LXRgNGB0YLQvdCw0Y8iLCJzZXJ2aWNlcyI6eyLQktGL0YfQtdGBIjo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkNhdCBzaG9ydC1oYWlyZWQiLCJicmVlZF9ldCI6Ikthc3MgbMO8aGlrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0JrQvtGI0LrQsCDQtNC70LjQvdC90L7RiNC10YDRgdGC0L3QsNGPIiwic2VydmljZXMiOnsi0JLRi9GH0LXRgSI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwfSwiYnJlZWRfZW4iOiJDYXQgbG9uZy1oYWlyZWQiLCJicmVlZF9ldCI6Ikthc3MgcGlra2FydmFsaW5lIn0seyJicmVlZCI6ItCa0L7RiNC60LAg0JzQtdC50L0t0LrRg9C9Iiwic2VydmljZXMiOnsi0JLRi9GH0ZHRgSI6NjAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwfSwiYnJlZWRfZW4iOiJDYXQgTWFpbmUgQ29vbiIsImJyZWVkX2V0IjoiS2FzcyBNYWluZSBDb29uIn1dOwp2YXIgUkFJTFdBWSA9ICJodHRwczovL3JqZ3Jvb21pbmcudXAucmFpbHdheS5hcHAvYm9vayI7CnZhciBHT09HTEVfU0NSSVBUID0gImh0dHBzOi8vc2NyaXB0Lmdvb2dsZS5jb20vbWFjcm9zL3MvQUtmeWNieVRTWi1lSk1kZXAtRDBMci1ueDBfVjRIQldnSUljdG5SVDJyalNEdkJ5Ymo1Q1lJM05LMk1xY0F3X2NmY3pnUkVpZmcvZXhlYyI7CnZhciBGQUxMQkFDS19USU1FUyA9IFsnMTA6MDAnLCcxMDozMCcsJzExOjAwJywnMTE6MzAnLCcxMjowMCcsJzEyOjMwJywnMTM6MDAnLCcxMzozMCcsJzE0OjAwJywnMTQ6MzAnLCcxNTowMCcsJzE1OjMwJywnMTY6MDAnLCcxNjozMCcsJzE3OjAwJywnMTc6MzAnLCcxODowMCddOwp2YXIgYm9va2luZyA9IHticmVlZDonJyxicmVlZERpc3BsYXk6Jycsc2VydmljZTonJyxwcmljZTowLG1hc3RlcjonJyxncm9vbUhpc3Rvcnk6JycsZGF0ZTonJyx0aW1lOicnLGxhbmc6J3J1J307CnZhciBzZWxCcmVlZCA9IG51bGw7CnZhciBjWSA9IG5ldyBEYXRlKCkuZ2V0RnVsbFllYXIoKTsKdmFyIGNNID0gbmV3IERhdGUoKS5nZXRNb250aCgpOwp2YXIgc3RlcCA9IDE7CnZhciBNT05USFMgPSBbJ9Cv0L3QstCw0YDRjCcsJ9Ck0LXQstGA0LDQu9GMJywn0JzQsNGA0YInLCfQkNC/0YDQtdC70YwnLCfQnNCw0LknLCfQmNGO0L3RjCcsJ9CY0Y7Qu9GMJywn0JDQstCz0YPRgdGCJywn0KHQtdC90YLRj9Cx0YDRjCcsJ9Ce0LrRgtGP0LHRgNGMJywn0J3QvtGP0LHRgNGMJywn0JTQtdC60LDQsdGA0YwnXTsKCmZ1bmN0aW9uIHNob3dTY3JlZW4oaWQpIHsKICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuc2NyZWVuJykuZm9yRWFjaChmdW5jdGlvbihzKXtzLmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpO30pOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKGlkKS5jbGFzc0xpc3QuYWRkKCdhY3RpdmUnKTsKICB3aW5kb3cuc2Nyb2xsVG8oMCwwKTsKfQoKZnVuY3Rpb24gZ29TdGVwKG4pIHsKICBbJ2JrMScsJ2JrMicsJ2JrMycsJ2JrNCcsJ2JrNSddLmZvckVhY2goZnVuY3Rpb24oaWQsaSl7CiAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZChpZCkuY2xhc3NOYW1lID0gJ3N0ZXAnICsgKGkrMT09PW4/JyBzaG93JzonJyk7CiAgfSk7CiAgZm9yKHZhciBpPTE7aTw9NTtpKyspewogICAgdmFyIHBzPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdwcycraSk7CiAgICB2YXIgcGw9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3BsJytpKTsKICAgIGlmKGk8bil7cHMuY2xhc3NOYW1lPSdwcyBkb25lJztpZihwbClwbC5jbGFzc05hbWU9J3BsIGRvbmUnO30KICAgIGVsc2UgaWYoaT09PW4pe3BzLmNsYXNzTmFtZT0ncHMgYWN0aXZlJztpZihwbClwbC5jbGFzc05hbWU9J3BsJzt9CiAgICBlbHNle3BzLmNsYXNzTmFtZT0ncHMnO2lmKHBsKXBsLmNsYXNzTmFtZT0ncGwnO30KICB9CiAgc3RlcD1uOyB3aW5kb3cuc2Nyb2xsVG8oMCwwKTsKICBpZihuPT09MikgZmlsdGVyTWFzdGVycygpOwp9Cgpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnYm9va0J0bicpLm9uY2xpY2sgPSBmdW5jdGlvbigpewogIHNob3dTY3JlZW4oJ2Jvb2tTY3JlZW4nKTsgZ29TdGVwKDEpOyBidWlsZENhbCgpOwp9Owpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnYmFja0J0bicpLm9uY2xpY2sgPSBmdW5jdGlvbigpewogIGlmKHN0ZXA+MSl7Z29TdGVwKHN0ZXAtMSk7fWVsc2V7c2hvd1NjcmVlbignaG9tZVNjcmVlbicpO30KfTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2hvbWVCdG4nKS5vbmNsaWNrID0gZnVuY3Rpb24oKXsKICBzaG93U2NyZWVuKCdob21lU2NyZWVuJyk7IHJlc2V0QWxsKCk7Cn07CgovLyBCcmVlZCBzZWFyY2gKdmFyIGlucCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdiSW5wdXQnKTsKdmFyIGRyb3AgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnYkRyb3AnKTsKdmFyIGNsciA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjbHJCdG4nKTsKdmFyIGJhZGdlID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3NCYWRnZScpOwoKaW5wLmFkZEV2ZW50TGlzdGVuZXIoJ2lucHV0JywgZnVuY3Rpb24oKXsKICB2YXIgcSA9IGlucC52YWx1ZS50cmltKCk7CiAgY2xyLmNsYXNzTGlzdC50b2dnbGUoJ3Nob3cnLCBxLmxlbmd0aD4wKTsKICBpZighcSl7ZHJvcC5jbGFzc0xpc3QucmVtb3ZlKCdvcGVuJyk7ZHJvcC5pbm5lckhUTUw9Jyc7cmV0dXJuO30KICB2YXIgc2Y9TEFORz09PSdlbic/J2JyZWVkX2VuJzpMQU5HPT09J2V0Jz8nYnJlZWRfZXQnOidicmVlZCc7CiAgdmFyIHJlcz1EQVRBLmZpbHRlcihmdW5jdGlvbihiKXtyZXR1cm4oYltzZl18fGIuYnJlZWQpLnRvTG93ZXJDYXNlKCkuaW5kZXhPZihxLnRvTG93ZXJDYXNlKCkpIT09LTE7fSkuc2xpY2UoMCwzNSk7CiAgZHJvcC5pbm5lckhUTUw9Jyc7CiAgdmFyIF9ucj1MQU5HPT09J2VuJz8nQnJlZWQgbm90IGZvdW5kJzpMQU5HPT09J2V0Jz8nVMO1dWd1IGVpIGxlaXR1ZCc6J9Cf0L7RgNC+0LTQsCDQvdC1INC90LDQudC00LXQvdCwJzsKICB2YXIgX250PUxBTkc9PT0nZW4nPyJDYW4ndCBmaW5kIHlvdXIgYnJlZWQ/IjpMQU5HPT09J2V0Jz8nRWkgbGVpYSBvbWEgdMO1dWd1Pyc6J9Cd0LUg0L3QsNGI0LvQuCDRgdCy0L7RjiDQv9C+0YDQvtC00YM/JzsKICB2YXIgX25zPUxBTkc9PT0nZW4nPydDb250YWN0IHVzIOKAlCB3ZSB3aWxsIGhlbHAgeW91IGNob29zZSBhIHNlcnZpY2UnOkxBTkc9PT0nZXQnPydWw7V0a2UgbWVpZWdhIMO8aGVuZHVzdCDigJQgYWl0YW1lIHRlZW51c2UgdmFsaWRhJzon0KHQstGP0LbQuNGC0LXRgdGMINGBINC90LDQvNC4INC70Y7QsdGL0Lwg0YPQtNC+0LHQvdGL0Lwg0YHQv9C+0YHQvtCx0L7QvCDigJQg0LzRiyDQv9C+0LzQvtC20LXQvCDQv9C+0LTQvtCx0YDQsNGC0Ywg0YPRgdC70YPQs9GDJzsKICBpZighcmVzLmxlbmd0aCl7ZHJvcC5pbm5lckhUTUw9JzxkaXYgY2xhc3M9Im5vcmVzIj4nK19ucisnPC9kaXY+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyIiBvbmNsaWNrPSJzaG93U2NyZWVuKFwnaG9tZVNjcmVlblwnKSI+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLWljb24iPvCfkL48L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItdGV4dCI+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLXRpdGxlIj4nK19udCsnPC9kaXY+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLXN1YiI+JytfbnMrJzwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci1hcnJvdyI+4oaSPC9kaXY+PC9kaXY+Jzt9CiAgZWxzZXsKICAgIHJlcy5mb3JFYWNoKGZ1bmN0aW9uKGIpewogICAgICB2YXIgZD1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdkaXYnKTsgZC5jbGFzc05hbWU9J2RpdGVtJzsKICAgICAgdmFyIGJuYW1lPWJbc2ZdfHxiLmJyZWVkOwogICAgICB2YXIgaWR4PWJuYW1lLnRvTG93ZXJDYXNlKCkuaW5kZXhPZihxLnRvTG93ZXJDYXNlKCkpOwogICAgICBkLmlubmVySFRNTD1ibmFtZS5zdWJzdHJpbmcoMCxpZHgpKyc8bWFyaz4nK2JuYW1lLnN1YnN0cmluZyhpZHgsaWR4K3EubGVuZ3RoKSsnPC9tYXJrPicrYm5hbWUuc3Vic3RyaW5nKGlkeCtxLmxlbmd0aCk7CiAgICAgIGQub25jbGljaz1mdW5jdGlvbigpe3NlbGVjdEJyZWVkKGIpO307CiAgICAgIGRyb3AuYXBwZW5kQ2hpbGQoZCk7CiAgICB9KTsKICB9CiAgZHJvcC5jbGFzc0xpc3QuYWRkKCdvcGVuJyk7Cn0pOwoKZG9jdW1lbnQuYWRkRXZlbnRMaXN0ZW5lcignY2xpY2snLGZ1bmN0aW9uKGUpewogIGlmKCFlLnRhcmdldC5jbG9zZXN0KCcuYndyYXAnKSlkcm9wLmNsYXNzTGlzdC5yZW1vdmUoJ29wZW4nKTsKfSk7CmNsci5vbmNsaWNrID0gcmVzZXRCcmVlZDsKCmZ1bmN0aW9uIHNlbGVjdEJyZWVkKGIpewogIHNlbEJyZWVkPWI7IGJvb2tpbmcuYnJlZWQ9Yi5icmVlZDsKICBpbnAudmFsdWU9Jyc7IGNsci5jbGFzc0xpc3QucmVtb3ZlKCdzaG93Jyk7CiAgZHJvcC5jbGFzc0xpc3QucmVtb3ZlKCdvcGVuJyk7IGRyb3AuaW5uZXJIVE1MPScnOwogIGJhZGdlLmlubmVySFRNTD0nJzsKICB2YXIgYkZpZWxkPUxBTkc9PT0nZW4nPydicmVlZF9lbic6TEFORz09PSdldCc/J2JyZWVkX2V0JzonYnJlZWQnOwogIHZhciBkaXNwQnJlZWQ9YltiRmllbGRdfHxiLmJyZWVkOwogIGJvb2tpbmcuYnJlZWREaXNwbGF5PWRpc3BCcmVlZDsKICB2YXIgYm49ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnc3BhbicpO2JuLmNsYXNzTmFtZT0nYm5hbWUnO2JuLnRleHRDb250ZW50PWRpc3BCcmVlZDsKICB2YXIgY2hnVHh0PUxBTkc9PT0nZW4nPydDaGFuZ2UnOkxBTkc9PT0nZXQnPydNdXVkYSc6J9CY0LfQvNC10L3QuNGC0YwnOwogIHZhciBiYz1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdzcGFuJyk7YmMuY2xhc3NOYW1lPSdiY2hnJztiYy50ZXh0Q29udGVudD1jaGdUeHQ7CiAgYmMub25jbGljaz1yZXNldEJyZWVkOwogIGJhZGdlLmFwcGVuZENoaWxkKGJuKTtiYWRnZS5hcHBlbmRDaGlsZChiYyk7CiAgYmFkZ2UuY2xhc3NMaXN0LmFkZCgnc2hvdycpOwogIHJlbmRlclN2Y3MoYik7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y1NlYycpLnN0eWxlLmRpc3BsYXk9J2Jsb2NrJzsKICAgIC8vIEFkZCBpbXBvcnRhbnQgbm90ZSBpZiBub3QgZXhpc3RzCiAgICBpZighZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y05vdGUnKSl7CiAgICAgIHZhciBub3RlPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpOwogICAgICBub3RlLmlkPSdzdmNOb3RlJzsKICAgICAgbm90ZS5zdHlsZS5jc3NUZXh0PSdib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA4KTtwYWRkaW5nOjE0cHggMTZweDtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjAyKTttYXJnaW4tdG9wOjEycHg7JzsKICAgICAgdmFyIG5vdGVUaXRsZT1MQU5HPT09J2VuJz8nUGxlYXNlIG5vdGUnOkxBTkc9PT0nZXQnPydQYW5nZSB0w6RoZWxlJzon0JLQsNC20L3QviDQt9C90LDRgtGMJzsKICAgICAgdmFyIG5vdGVCb2R5PUxBTkc9PT0nZW4nPydGaW5hbCBwcmljZSBkZXBlbmRzIG9uIGNvYXQgY29uZGl0aW9uIGFuZCBwZXQgYmVoYXZpb3VyLjxicj5EZW1hdHRpbmcgZnJvbSA1IOKCrC48YnI+QWdncmVzc2l2ZSBiZWhhdmlvdXIgc3VyY2hhcmdlIG1heSBhcHBseTogKzUwJS4nOkxBTkc9PT0nZXQnPydMw7VwbGlrIGhpbmQgc8O1bHR1YiBrYXJ2YXN0aWt1IHNlaXN1bmRpc3QgamEgbGVtbWlrbG9vbWEga8OkaXR1bWlzZXN0Ljxicj5Lb2x0c3VuaXRlIGxhaHRpaGFydXRhbWluZSBhbGF0ZXMgNSDigqwuPGJyPkFncmVzc2lpdnNlIGvDpGl0dW1pc2Uga29ycmFsIHbDtWliIGxpc2FuZHVkYSA1MCUganV1cmRlaGluZGx1cy4nOifQntC60L7QvdGH0LDRgtC10LvRjNC90LDRjyDRgdGC0L7QuNC80L7RgdGC0Ywg0LfQsNCy0LjRgdC40YIg0L7RgiDRgdC+0YHRgtC+0Y/QvdC40Y8g0YjQtdGA0YHRgtC4INC4INC/0L7QstC10LTQtdC90LjRjyDQv9C40YLQvtC80YbQsC48YnI+0KDQsNC30LHQvtGAINC60L7Qu9GC0YPQvdC+0LIg4oCUINC+0YIgNSDigqwuPGJyPtCf0YDQuCDQsNCz0YDQtdGB0YHQuNCy0L3QvtC8INC/0L7QstC10LTQtdC90LjQuCDQvNC+0LbQtdGCINC/0YDQuNC80LXQvdGP0YLRjNGB0Y8g0LTQvtC/0LvQsNGC0LAgNTAlLic7CiAgICAgIG5vdGUuaW5uZXJIVE1MPSc8ZGl2IHN0eWxlPSJmb250LXNpemU6MC44MzhyZW07bGV0dGVyLXNwYWNpbmc6LjE1ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbTo4cHg7Zm9udC13ZWlnaHQ6NjAwO2ZvbnQtZmFtaWx5OlwnTW9udHNlcnJhdFwnLHNhbnMtc2VyaWYiPicrbm90ZVRpdGxlKyc8L2Rpdj48ZGl2IHN0eWxlPSJmb250LXNpemU6MS4wMjVyZW07Y29sb3I6I2ZmZmZmZjtsaW5lLWhlaWdodDoxLjg7Zm9udC1mYW1pbHk6XCdNb250c2VycmF0XCcsc2Fucy1zZXJpZiI+Jytub3RlQm9keSsnPC9kaXY+JzsKICAgICAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y1NlYycpLmFwcGVuZENoaWxkKG5vdGUpOwogICAgfQogIGZpbHRlck1hc3RlcnMoKTsKfQoKZnVuY3Rpb24gcmVzZXRCcmVlZCgpewogIHNlbEJyZWVkPW51bGw7Ym9va2luZy5icmVlZD0nJztib29raW5nLnNlcnZpY2U9Jyc7Ym9va2luZy5wcmljZT0wOwogIGlucC52YWx1ZT0nJztjbHIuY2xhc3NMaXN0LnJlbW92ZSgnc2hvdycpOwogIGJhZGdlLmNsYXNzTGlzdC5yZW1vdmUoJ3Nob3cnKTtiYWRnZS5pbm5lckhUTUw9Jyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y1NlYycpLnN0eWxlLmRpc3BsYXk9J25vbmUnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdmNMaXN0JykuaW5uZXJIVE1MPScnOwp9CgoKdmFyIFNWQ19UUkFOU0xBVElPTlMgPSB7CiAgJ9CR0LDQt9C+0LLRi9C5INGD0YXQvtC0JzogICAgICB7ZW46J0Jhc2ljIGdyb29tJywgICAgICBldDonUMO1aGlob29sZHVzJ30sCiAgJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Jzp7ZW46J0h5Z2llbmUgZ3Jvb20nLCAgICBldDonSMO8Z2llZW5paG9vbGR1cyd9LAogICfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0JzogIHtlbjonRnVsbCBncm9vbScsICAgICAgICBldDonVMOkaWVsaWsgaG9vbGR1cyd9LAogICfQotGA0LjQvNC80LjQvdCzJzogICAgICAgICAge2VuOidUcmltbWluZycsICAgICAgICAgIGV0OidUcmltbWVyaW1pbmUnfSwKICAn0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAnOiAgIHtlbjonRXhwcmVzcyBzaGVkJywgICAgICBldDonS2lpcmthcnZhdmFoZXR1cyd9LAogICfQktGL0YfQtdGBJzogICAgICAgICAgICAge2VuOidCcnVzaC1vdXQnLCAgICAgICAgIGV0OidIYXJqYW1pbmUnfSwKICAn0JLRgdGPINC/0YDQvtCz0YDQsNC80LzQsCc6ICAgICB7ZW46J0Z1bGwgcHJvZ3JhbScsICAgICAgZXQ6J0tvZ3UgcHJvZ3JhbW0nfQp9Owp2YXIgU1ZDX1RBR0xJTkVfSTE4Tj17CiAgcnU6eyfQktGL0YfQtdGBJzon0KHRgtC+0LjQvNC+0YHRgtGMINC30LDQstC40YHQuNGCINC+0YIg0YHQvtGB0YLQvtGP0L3QuNGPINGI0LXRgNGB0YLQuCDQuCDQvtCx0YrRkdC80LAg0YDQsNCx0L7RgicsJ9CR0LDQt9C+0LLRi9C5INGD0YXQvtC0Jzon0J/QvtC00YXQvtC00LjRgiDQtNC70Y8g0L/QvtC00LTQtdGA0LbQsNC90LjRjyDRh9C40YHRgtC+0YLRiyDQvNC10LbQtNGDINC/0YDQvtGG0LXQtNGD0YDQsNC80LgnLCfQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCc6J9CU0LvRjyDQutC+0LzRhNC+0YDRgtCwINC4INCw0LrQutGD0YDQsNGC0L3QvtGB0YLQuCDQv9C40YLQvtC80YbQsCcsJ9Ca0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQnOifQn9C+0LvQvdGL0Lkg0YPRhdC+0LQg0YHQviDRgdGC0YDQuNC20LrQvtC5Jywn0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAnOifQn9C+0LzQvtCz0LDQtdGCINGD0LzQtdC90YzRiNC40YLRjCDQutC+0LvQuNGH0LXRgdGC0LLQviDQu9C40L3Rj9GO0YnQtdC5INGI0LXRgNGB0YLQuCcsJ9Ci0YDQuNC80LzQuNC90LMnOifQlNC70Y8g0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90YvRhSDQv9C+0YDQvtC0J30sCiAgZW46eyfQktGL0YfQtdGBJzonUHJpY2UgZGVwZW5kcyBvbiBjb2F0IGNvbmRpdGlvbiBhbmQgdm9sdW1lIG9mIHdvcmsnLCfQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCc6J0lkZWFsIGZvciBtYWludGFpbmluZyBjbGVhbmxpbmVzcyBiZXR3ZWVuIGZ1bGwgZ3Jvb21zJywn0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQnOidGb3IgeW91ciBwZXRcJ3MgY29tZm9ydCBhbmQgbmVhdG5lc3MnLCfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0JzonRnVsbCBncm9vbWluZyB3aXRoIGhhaXJjdXQnLCfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6J1NpZ25pZmljYW50bHkgcmVkdWNlcyBzaGVkZGluZycsJ9Ci0YDQuNC80LzQuNC90LMnOidGb3Igd2lyZS1oYWlyZWQgYnJlZWRzJ30sCiAgZXQ6eyfQktGL0YfQtdGBJzonSGluZCBzw7VsdHViIGthcnZhc3Rpa3Ugc2Vpc3VuZGlzdCBqYSB0w7bDtm1haHVzdCcsJ9CR0LDQt9C+0LLRi9C5INGD0YXQvtC0JzonU29iaWIgcHVodHVzZSBob2lkbWlzZWtzIHByb3RzZWR1dXJpZGUgdmFoZWwnLCfQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCc6J0xlbW1pa2xvb21hIG11Z2F2dXNla3MgamEga29ycmFzaG9pdWtzJywn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc6J1TDpGllbGlrIGhvb2xkdXMga29vcyBsw7Vpa3VzZWdhJywn0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAnOidWw6RoZW5kYWIgb2x1bGlzZWx0IGthcnZhZGUgbGFuZ2VtaXN0Jywn0KLRgNC40LzQvNC40L3Qsyc6J1RyYWF0a2FydmFsaXN0ZWxlIHTDtXVndWRlbGUnfQp9Owp2YXIgU1ZDX0RFU0NfSTE4Tj17CiAgcnU6eyfQktGL0YfQtdGBJzon0KfQuNGB0YLQutCwINCz0LvQsNC3LCDRg9GI0LXQuSwg0L/QvtC00YHRgtGA0LjQs9Cw0L3QuNC1INC60L7Qs9GC0LXQuSwg0LLRi9GH0ZHRgSAo0LTQu9GPINC60L7RiNC10LopJywn0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQnOifQnNGL0YLRjNGRINC/0YDQvtGE0LXRgdGB0LjQvtC90LDQu9GM0L3Ri9C80Lgg0YHRgNC10LTRgdGC0LLQsNC80LgsINC00LXQu9C40LrQsNGC0L3QsNGPINGB0YPRiNC60LAnLCfQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCc6J9Ch0YLRgNC40LbQutCwINC60L7Qs9GC0LXQuSwg0YfQuNGB0YLQutCwINGD0YjQtdC5INC4INCz0LvQsNC3LCDQutGD0L/QsNC90LjQtSwg0YHRg9GI0LrQsCwg0YPRhdC+0LQg0LfQsCDQu9Cw0L/QutCw0LzQuCDQuCDRh9GD0LLRgdGC0LLQuNGC0LXQu9GM0L3Ri9C80Lgg0LfQvtC90LDQvNC4Jywn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc6J9Ch0YLRgNC40LbQutCwINC60L7Qs9GC0LXQuSwg0YfQuNGB0YLQutCwINGD0YjQtdC5INC4INCz0LvQsNC3LCDQutGD0L/QsNC90LjQtSwg0YHRg9GI0LrQsCwg0YPRhdC+0LQg0LfQsCDQu9Cw0L/QutCw0LzQuCDQuCDRh9GD0LLRgdGC0LLQuNGC0LXQu9GM0L3Ri9C80Lgg0LfQvtC90LDQvNC4LCDQvNC+0LTQtdC70YzQvdCw0Y8g0YHRgtGA0LjQttC60LAnLCfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6J9Cc0YvRgtGM0ZEsINGB0YPRiNC60LAsINGD0YXQvtC0INC30LAg0YjQtdGA0YHRgtGM0Y4sINC80LDRgdC60LAsINC/0L7QtNGB0YLRgNC40LPQsNC90LjQtSDQutC+0LPRgtC10LksINGH0LjRgdGC0LrQsCDRg9GI0LXQuSDQuCDQs9C70LDQtywg0YPRhdC+0LQg0LfQsCDQu9Cw0L/QsNC80Lgg0Lgg0LfQvtC90LDQvNC4INGC0YDQtdCx0YPRjtGJ0LjQvNC4INC+0YHQvtCx0L7Qs9C+INCy0L3QuNC80LDQvdC40Y8nLCfQotGA0LjQvNC80LjQvdCzJzon0JLRi9GJ0LjQv9GL0LLQsNC90LjQtSDRgdGC0LDRgNC+0LPQviDRgdC70L7RjyDRiNC10YDRgdGC0LgsINC80YvRgtGM0ZEsINGB0YPRiNC60LAsINGB0YLRgNC40LbQutCwINC60L7Qs9GC0LXQuSwg0YfQuNGB0YLQutCwINGD0YjQtdC5INC4INCz0LvQsNC3LCDQvtGE0L7RgNC80LvQtdC90LjQtSDRiNC10YDRgdGC0LgnLCfQktGB0Y8g0L/RgNC+0LPRgNCw0LzQvNCwJzon0J/QldCg0JLQq9CZINCS0JjQl9CY0KIgKDIwLTMwINC80LjQvSkg4oCUIDIwIOKCrFxu4oCiINC30L3QsNC60L7QvNGB0YLQstC+INGB0L4g0YHRgtC+0LvQvtC8INC4INC40L3RgdGC0YDRg9C80LXQvdGC0LDQvNC4XG7igKIg0LvRkdCz0LrQvtC1INCy0YvRh9GR0YHRi9Cy0LDQvdC40LVcbuKAoiDQt9Cy0YPQutC4INGE0LXQvdCwINC4INC70LXQs9C60LDRjyDQv9GA0L7QtNGD0LLQutCwXG7igKIg0L7RgdCy0LXQttC10L3QuNC1INCz0LvQsNC30L7QuiDQuCDRg9GI0LXQulxu4oCiINC60L7Qs9C+0YLQutC4XG7igKIg0LLQutGD0YHQvdGP0YjQutC4INC4INGB0L/QvtC60L7QudC90LDRjyDQsNC00LDQv9GC0LDRhtC40Y9cblxu0JLQotCe0KDQntCZINCS0JjQl9CY0KIgKDQwLTYwINC80LjQvSkg4oCUIDM1IOKCrFxu4oCiINC/0LXRgNCy0L7QtSDQutGD0L/QsNC90LjQtSDQuCDRgdGD0YjQutCwXG7igKIg0LLRi9GH0ZHRgdGL0LLQsNC90LjQtVxu4oCiINCz0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0XG7igKIg0L3QtdCx0L7Qu9GM0YjQsNGPINGB0YLRgNC40LbQutCwIC8g0LrQvtGA0YDQtdC60YbQuNGPINGI0LXRgNGB0YLQuCAo0L/RgNC4INC90LXQvtCx0YXQvtC00LjQvNC+0YHRgtC4KVxu4oCiINC30LDQutGA0LXQv9C70LXQvdC40LUg0L/QvtC70L7QttC40YLQtdC70YzQvdC+0LPQviDQvtC/0YvRgtCwJ30sCiAgZW46eyfQktGL0YfQtdGBJzonRXllIGFuZCBlYXIgY2xlYW5pbmcsIG5haWwgdHJpbW1pbmcsIGJydXNoaW5nIChmb3IgY2F0cyknLCfQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCc6J1dhc2hpbmcgd2l0aCBwcm9mZXNzaW9uYWwgcHJvZHVjdHMsIGdlbnRsZSBkcnlpbmcnLCfQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCc6J05haWwgdHJpbW1pbmcsIGVhciBhbmQgZXllIGNsZWFuaW5nLCBiYXRoaW5nLCBkcnlpbmcsIHBhdyBhbmQgc2Vuc2l0aXZlIGFyZWEgY2FyZScsJ9Ca0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQnOidOYWlsIHRyaW1taW5nLCBlYXIgYW5kIGV5ZSBjbGVhbmluZywgYmF0aGluZywgZHJ5aW5nLCBwYXcgYW5kIHNlbnNpdGl2ZSBhcmVhIGNhcmUsIHN0eWxpbmcgaGFpcmN1dCcsJ9Ct0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwJzonV2FzaGluZywgZHJ5aW5nLCBjb2F0IGNhcmUsIG1hc2ssIG5haWwgdHJpbW1pbmcsIGVhciBhbmQgZXllIGNsZWFuaW5nLCBwYXcgYW5kIHNwZWNpYWwgYXJlYSBjYXJlJywn0KLRgNC40LzQvNC40L3Qsyc6J1JlbW92aW5nIG9sZCBjb2F0IGxheWVyLCB3YXNoaW5nLCBkcnlpbmcsIG5haWwgdHJpbW1pbmcsIGVhciBhbmQgZXllIGNsZWFuaW5nLCBjb2F0IHN0eWxpbmcnLCfQktGB0Y8g0L/RgNC+0LPRgNCw0LzQvNCwJzonRklSU1QgVklTSVQgKDIwLTMwIG1pbikg4oCUIOKCrDIwXG7igKIgZ2V0dGluZyB1c2VkIHRvIHRoZSB0YWJsZSBhbmQgdG9vbHNcbuKAoiBnZW50bGUgYnJ1c2hpbmdcbuKAoiBkcnllciBzb3VuZHMgYW5kIGxpZ2h0IGFpcmZsb3dcbuKAoiBleWUgYW5kIGVhciByZWZyZXNoXG7igKIgbmFpbCB0cmltXG7igKIgdHJlYXRzIGFuZCBjYWxtIGFkYXB0YXRpb25cblxuU0VDT05EIFZJU0lUICg0MC02MCBtaW4pIOKAlCDigqwzNVxu4oCiIGZpcnN0IGJhdGggYW5kIGRyeWluZ1xu4oCiIGJydXNoaW5nXG7igKIgaHlnaWVuZSBjYXJlXG7igKIgbGlnaHQgdHJpbSAvIGNvYXQgYWRqdXN0bWVudCAoaWYgbmVlZGVkKVxu4oCiIHJlaW5mb3JjaW5nIHRoZSBwb3NpdGl2ZSBleHBlcmllbmNlJ30sCiAgZXQ6eyfQktGL0YfQtdGBJzonU2lsbWFkZSBqYSBrw7VydmFkZSBwdWhhc3RhbWluZSwga8O8w7xudGUgbMO1aWthbWluZSwgaGFyamFtaW5lIChrYXNzaWRlbGUpJywn0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQnOidQZXNlbWluZSBwcm9mZXNzaW9uYWFsc2V0ZSB2YWhlbmRpdGVnYSwgw7VybiBrdWl2YXRhbWluZScsJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0JzonS8O8w7xudGUgbMO1aWthbWluZSwga8O1cnZhZGUgamEgc2lsbWFkZSBwdWhhc3RhbWluZSwgcGVzZW1pbmUsIGt1aXZhdGFtaW5lLCBrw6RwcGFkZSBqYSB0dW5kbGlrZSBwaWlya29uZGFkZSBob29sZHVzJywn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc6J0vDvMO8bnRlIGzDtWlrYW1pbmUsIGvDtXJ2YWRlIGphIHNpbG1hZGUgcHVoYXN0YW1pbmUsIHBlc2VtaW5lLCBrdWl2YXRhbWluZSwga8OkcHBhZGUgamEgdHVuZGxpa2UgcGlpcmtvbmRhZGUgaG9vbGR1cywgbW9kZWxsw7Vpa3VzJywn0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAnOidQZXNlbWluZSwga3VpdmF0YW1pbmUsIGthcnZhc3Rpa3UgaG9vbGR1cywgbWFzaywga8O8w7xudGUgbMO1aWthbWluZSwga8O1cnZhZGUgamEgc2lsbWFkZSBwdWhhc3RhbWluZSwga8OkcHBhZGUgamEgZXJpbGlzdGUgcGlpcmtvbmRhZGUgaG9vbGR1cycsJ9Ci0YDQuNC80LzQuNC90LMnOidWYW5hIGthcnZha2loaSBlZW1hbGRhbWluZSwgcGVzZW1pbmUsIGt1aXZhdGFtaW5lLCBrw7zDvG50ZSBsw7Vpa2FtaW5lLCBrw7VydmFkZSBqYSBzaWxtYWRlIHB1aGFzdGFtaW5lLCBrYXJ2YXN0aWt1IGt1anVuZGFtaW5lJywn0JLRgdGPINC/0YDQvtCz0YDQsNC80LzQsCc6J0VTSU1FTkUgS8OcTEFTVFVTICgyMC0zMCBtaW4pIOKAlCAyMCDigqxcbuKAoiB0dXR2dW1pbmUgbGF1YWdhIGphIHTDtsO2cmlpc3RhZGVnYVxu4oCiIGtlcmdlIGhhcmphbWluZVxu4oCiIGbDtsO2bmloZWxpZCBqYSBrZXJnZSDDtWh1dm9vbFxu4oCiIHNpbG1hZGUgamEga8O1cnZhZGUgdsOkcnNrZW5kdXNcbuKAoiBrw7zDvG50ZSBsw7Vpa2FtaW5lXG7igKIgbWFpdXNlZCBqYSByYWh1bGlrIGtvaGFuZW1pbmVcblxuVEVJTkUgS8OcTEFTVFVTICg0MC02MCBtaW4pIOKAlCAzNSDigqxcbuKAoiBlc2ltZW5lIHZhbm5pdGFtaW5lIGphIGt1aXZhdGFtaW5lXG7igKIgaGFyamFtaW5lXG7igKIgaMO8Z2llZW5paG9vbGR1c1xu4oCiIGtlcmdlIGzDtWlrdXMgLyBrYXJ2YSBrb3JyaWdlZXJpbWluZSAodmFqYWR1c2VsKVxu4oCiIHBvc2l0aWl2c2Uga29nZW11c2Uga2lubmlzdGFtaW5lJ30KfTsKdmFyIFNWQ19ERVNDX0NBVF9DT01QTEVYPXsKICBydTon0JzRi9GC0YzRkSwg0YHRg9GI0LrQsCwg0LLRi9GH0ZHRgdGL0LLQsNC90LjQtSwg0YHRgtGA0LjQttC60LAg0LrQvtCz0YLQtdC5LCDQsCDRgtCw0LrQttC1INC+0LHRgNCw0LHQvtGC0LrQsCDQs9C70LDQtyDQuCDRg9GI0LXQuicsCiAgZW46J1dhc2hpbmcsIGRyeWluZywgYnJ1c2hpbmcsIG5haWwgdHJpbW1pbmcsIGFuZCBleWUgYW5kIGVhciBjYXJlJywKICBldDonUGVzZW1pbmUsIGt1aXZhdGFtaW5lLCBoYXJqYW1pbmUsIGvDvMO8bnRlIGzDtWlrYW1pbmUgbmluZyBzaWxtYWRlIGphIGvDtXJ2YWRlIGhvb2xkdXMnCn07CmZ1bmN0aW9uIGdldFN2Y1RhZyhuYW1lKXtyZXR1cm4oU1ZDX1RBR0xJTkVfSTE4TltMQU5HXSYmU1ZDX1RBR0xJTkVfSTE4TltMQU5HXVtuYW1lXSl8fFNWQ19UQUdMSU5FX0kxOE4ucnVbbmFtZV18fCcnO30KZnVuY3Rpb24gZ2V0U3ZjRGVzYyhuYW1lKXsKICBpZihuYW1lPT09J9Ca0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQnICYmIGJvb2tpbmcuYnJlZWQgJiYgYm9va2luZy5icmVlZC5pbmRleE9mKCfQmtC+0YjQutCwJyk9PT0wKXsKICAgIHZhciBkPVNWQ19ERVNDX0NBVF9DT01QTEVYW0xBTkddfHxTVkNfREVTQ19DQVRfQ09NUExFWC5ydTsKICAgIHJldHVybiBkOwogIH0KICByZXR1cm4oU1ZDX0RFU0NfSTE4TltMQU5HXSYmU1ZDX0RFU0NfSTE4TltMQU5HXVtuYW1lXSl8fFNWQ19ERVNDX0kxOE4ucnVbbmFtZV18fCcnOwp9CgpmdW5jdGlvbiByZW5kZXJTdmNzKGIpewogIHZhciBsYmxFbD1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3RlcDJMYmxFbCcpOwogIGlmKGxibEVsKXsKICAgIHZhciBiYXNlTGJsPShUW0xBTkddJiZUW0xBTkddLnN0ZXAyX2xibCl8fCcwMiDCtyDQo9GB0LvRg9Cz0LAnOwogICAgbGJsRWwudGV4dENvbnRlbnQ9KGIuYnJlZWQ9PT0n0KnQtdC90LrQuCcpPyhiYXNlTGJsKycgUHVwcHkgU3RhcicpOmJhc2VMYmw7CiAgfQogIHZhciBsaXN0PWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdmNMaXN0Jyk7bGlzdC5pbm5lckhUTUw9Jyc7CiAgT2JqZWN0LmVudHJpZXMoYi5zZXJ2aWNlcykuZm9yRWFjaChmdW5jdGlvbihrdil7CiAgICB2YXIgbmFtZT1rdlswXSxwcmljZT1rdlsxXTsKCiAgICB2YXIgZGlzcGxheU5hbWU9KExBTkchPT0ncnUnJiZTVkNfVFJBTlNMQVRJT05TW25hbWVdKT9TVkNfVFJBTlNMQVRJT05TW25hbWVdW0xBTkddOm5hbWU7CiAgICB2YXIgYnRuPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2J1dHRvbicpO2J0bi5jbGFzc05hbWU9J3N2YnRuJzsKICAgIHZhciByb3c9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7cm93LmNsYXNzTmFtZT0nc3ZidG4tcm93JzsKICAgIHZhciBucz1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdzcGFuJyk7bnMuY2xhc3NOYW1lPSdzdmJ0bi1uYW1lJztucy50ZXh0Q29udGVudD1kaXNwbGF5TmFtZTsKICAgIHZhciBwcz1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdzcGFuJyk7cHMuY2xhc3NOYW1lPSdzdmJ0bi1wcmljZSc7cHMudGV4dENvbnRlbnQ9cHJpY2UrJyDigqwnOwogICAgcm93LmFwcGVuZENoaWxkKG5zKTtyb3cuYXBwZW5kQ2hpbGQocHMpOwogICAgYnRuLmFwcGVuZENoaWxkKHJvdyk7CiAgICB2YXIgZGVzYz1nZXRTdmNEZXNjKG5hbWUpOwogICAgaWYoZGVzYyl7dmFyIGRzPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NwYW4nKTtkcy5jbGFzc05hbWU9J3N2YnRuLWRlc2MnO2RzLnRleHRDb250ZW50PWRlc2M7YnRuLmFwcGVuZENoaWxkKGRzKTt9CiAgICB2YXIgdGFnPWdldFN2Y1RhZyhuYW1lKTsKICAgIGlmKHRhZyl7dmFyIHRzPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NwYW4nKTt0cy5jbGFzc05hbWU9J3N2YnRuLXRhZyc7dHMudGV4dENvbnRlbnQ9dGFnO2J0bi5hcHBlbmRDaGlsZCh0cyk7fQogICAgYnRuLm9uY2xpY2s9ZnVuY3Rpb24oKXsKICAgICAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLnN2YnRuJykuZm9yRWFjaChmdW5jdGlvbihiKXtiLmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpO30pOwogICAgICBidG4uY2xhc3NMaXN0LmFkZCgnYWN0aXZlJyk7CiAgICAgIGJvb2tpbmcuc2VydmljZT1uYW1lO2Jvb2tpbmcucHJpY2U9cHJpY2U7CiAgICAgIGZpbHRlck1hc3RlcnMoKTsKICAgICAgc2V0VGltZW91dChmdW5jdGlvbigpe2dvU3RlcCgyKTt9LDMwMCk7CiAgICB9OwogICAgbGlzdC5hcHBlbmRDaGlsZChidG4pOwogIH0pOwp9CgovLyBNYXN0ZXJzCmZ1bmN0aW9uIHNvcnRNYXN0ZXJzQnlBdmFpbGFiaWxpdHkoKXsKICB2YXIgbm93ID0gbmV3IERhdGUoKTsKICB2YXIgbW9udGggPSBub3cuZ2V0TW9udGgoKSsxLCB5ZWFyID0gbm93LmdldEZ1bGxZZWFyKCk7CiAgdmFyIGJhc2VPcmRlciA9IFsn0KLQsNGC0YzRj9C90LAnLCfQkNC70LXQutGB0LDQvdC00YDQsCcsJ9Ca0YHQtdC90LjRjycsJ9CQ0L3QvdCwJywn0JDQu9C40YHQsCcsJ9Ca0YDQuNGB0YLQuNC90LAnXTsKICB2YXIgdmlzaWJsZUJ0bnMgPSBBcnJheS5wcm90b3R5cGUuZmlsdGVyLmNhbGwoZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLm1idG4nKSwgZnVuY3Rpb24oYil7IHJldHVybiBiLnN0eWxlLmRpc3BsYXkgIT09ICdub25lJzsgfSk7CiAgaWYoIXZpc2libGVCdG5zLmxlbmd0aCkgcmV0dXJuOwogIFByb21pc2UuYWxsKHZpc2libGVCdG5zLm1hcChmdW5jdGlvbihidG4pewogICAgdmFyIG1hc3RlciA9IGJ0bi5nZXRBdHRyaWJ1dGUoJ2RhdGEtbWFzdGVyJyk7CiAgICByZXR1cm4gZmV0Y2god2luZG93LmxvY2F0aW9uLm9yaWdpbiArICcvYXBpL2F2YWlsYWJsZV9kYXlzP21vbnRoPScgKyBtb250aCArICcmeWVhcj0nICsgeWVhciArICcmbWFzdGVyPScgKyBlbmNvZGVVUklDb21wb25lbnQobWFzdGVyKSkKICAgICAgLnRoZW4oZnVuY3Rpb24ocil7IHJldHVybiByLmpzb24oKTsgfSkKICAgICAgLnRoZW4oZnVuY3Rpb24oZCl7IHJldHVybiB7YnRuOiBidG4sIG1hc3RlcjogbWFzdGVyLCBjb3VudDogKGQuYXZhaWxhYmxlfHxbXSkubGVuZ3RofTsgfSkKICAgICAgLmNhdGNoKGZ1bmN0aW9uKCl7IHJldHVybiB7YnRuOiBidG4sIG1hc3RlcjogbWFzdGVyLCBjb3VudDogLTF9OyB9KTsKICB9KSkudGhlbihmdW5jdGlvbihyZXN1bHRzKXsKICAgIHJlc3VsdHMuc29ydChmdW5jdGlvbihhLGIpewogICAgICBpZihiLmNvdW50ICE9PSBhLmNvdW50KSByZXR1cm4gYi5jb3VudCAtIGEuY291bnQ7CiAgICAgIHJldHVybiBiYXNlT3JkZXIuaW5kZXhPZihhLm1hc3RlcikgLSBiYXNlT3JkZXIuaW5kZXhPZihiLm1hc3Rlcik7CiAgICB9KTsKICAgIHJlc3VsdHMuZm9yRWFjaChmdW5jdGlvbihyLCBpKXsgci5idG4uc3R5bGUub3JkZXIgPSBpOyB9KTsKICAgIGlmKHJlc3VsdHMubGVuZ3RoKXsKICAgICAgc2V0VGltZW91dChmdW5jdGlvbigpewogICAgICAgIGNlbnRlck1hc3RlckluV2hlZWwocmVzdWx0c1swXS5idG4sIGZhbHNlKTsKICAgICAgICB1cGRhdGVXaGVlbEFjdGl2ZSgpOwogICAgICB9LCAzMCk7CiAgICB9CiAgfSk7Cn0KCmZ1bmN0aW9uIGZpbHRlck1hc3RlcnMoKXsKICB2YXIgaXNDYXQgPSBib29raW5nLmJyZWVkICYmIGJvb2tpbmcuYnJlZWQuaW5kZXhPZign0JrQvtGI0LrQsCcpID09PSAwOwogIHZhciBicmVlZCA9IGJvb2tpbmcuYnJlZWQgfHwgJyc7CiAgdmFyIGlzQ2F0Q29tcGxleCA9IGlzQ2F0ICYmIGJvb2tpbmcuc2VydmljZSA9PT0gJ9Ca0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQnOwogIHZhciBhbm5hRXhjbHVkZSA9IFsn0JzQsNC70YzRgtC40L/RgycsJ9Cf0YPQtNC10LvRjCcsJ9CZ0L7RgNC6Jywn0JHQuNGI0L7QvScsJ9CR0L7Qu9C+0L3QutCwJywn0JzQsNC70YzRgtC40LnRgdC60LDRjyddOwogIHZhciBpc0FubmFCcmVlZCA9IGJyZWVkICYmICFhbm5hRXhjbHVkZS5zb21lKGZ1bmN0aW9uKGIpeyByZXR1cm4gYnJlZWQuaW5kZXhPZihiKSAhPT0gLTE7IH0pOwogIHZhciBhbGV4YW5kcmFFeGNsdWRlID0gWyfQpNC+0LrRgdGC0LXRgNGM0LXRgCcsJ9Cm0LLQtdGA0LPRiNC90LDRg9GG0LXRgCddOwogIHZhciBpc0FsZXhhbmRyYUJyZWVkID0gIWFsZXhhbmRyYUV4Y2x1ZGUuc29tZShmdW5jdGlvbihiKXsgcmV0dXJuIGJyZWVkLmluZGV4T2YoYikgIT09IC0xOyB9KTsKICB2YXIga3NlbmlhRXhjbHVkZSA9IFsn0J/Rg9C00LXQu9GMJywn0JzQsNC70YzRgtC40L/RgycsJ9CZ0L7RgNC6Jywn0JHQvtC70L7QvdC60LAnXTsKICB2YXIgaXNLc2VuaWFCcmVlZCA9ICFrc2VuaWFFeGNsdWRlLnNvbWUoZnVuY3Rpb24oYil7IHJldHVybiBicmVlZC5pbmRleE9mKGIpICE9PSAtMTsgfSk7CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLm1idG4nKS5mb3JFYWNoKGZ1bmN0aW9uKGJ0bil7CiAgICB2YXIgbWFzdGVyID0gYnRuLmdldEF0dHJpYnV0ZSgnZGF0YS1tYXN0ZXInKTsKICAgIHZhciBpc1RyaW1taW5nID0gYm9va2luZy5zZXJ2aWNlID09PSAn0KLRgNC40LzQvNC40L3Qsyc7CiAgICBpZihpc0NhdENvbXBsZXgpewogICAgICBidG4uc3R5bGUuZGlzcGxheSA9IChtYXN0ZXIgPT09ICfQotCw0YLRjNGP0L3QsCcgfHwgbWFzdGVyID09PSAn0JrRgdC10L3QuNGPJykgPyAnJyA6ICdub25lJzsKICAgICAgcmV0dXJuOwogICAgfQogICAgaWYobWFzdGVyID09PSAn0JDQu9C40YHQsCcpewogICAgICBidG4uc3R5bGUuZGlzcGxheSA9IGlzQ2F0ID8gJycgOiAnbm9uZSc7CiAgICB9IGVsc2UgaWYobWFzdGVyID09PSAn0JDQvdC90LAnKXsKICAgICAgYnRuLnN0eWxlLmRpc3BsYXkgPSAoaXNBbm5hQnJlZWQgJiYgIWlzVHJpbW1pbmcpID8gJycgOiAnbm9uZSc7CiAgICB9IGVsc2UgaWYobWFzdGVyID09PSAn0JDQu9C10LrRgdCw0L3QtNGA0LAnKXsKICAgICAgYnRuLnN0eWxlLmRpc3BsYXkgPSAoaXNBbGV4YW5kcmFCcmVlZCAmJiAhaXNUcmltbWluZyAmJiAhaXNDYXQpID8gJycgOiAnbm9uZSc7CiAgICB9IGVsc2UgaWYobWFzdGVyID09PSAn0JrRgdC10L3QuNGPJyl7CiAgICAgIGJ0bi5zdHlsZS5kaXNwbGF5ID0gaXNLc2VuaWFCcmVlZCA/ICcnIDogJ25vbmUnOwogICAgfSBlbHNlIGlmKGlzVHJpbW1pbmcpewogICAgICBidG4uc3R5bGUuZGlzcGxheSA9ICdub25lJzsKICAgIH0gZWxzZSB7CiAgICAgIGJ0bi5zdHlsZS5kaXNwbGF5ID0gJyc7CiAgICB9CiAgfSk7CiAgc29ydE1hc3RlcnNCeUF2YWlsYWJpbGl0eSgpOwp9CgpmdW5jdGlvbiB1cGRhdGVXaGVlbEFjdGl2ZSgpewogIHZhciB3aGVlbCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdtYXN0ZXJXaGVlbCcpOwogIGlmKCF3aGVlbCkgcmV0dXJuOwogIHZhciB3aGVlbFJlY3QgPSB3aGVlbC5nZXRCb3VuZGluZ0NsaWVudFJlY3QoKTsKICB2YXIgY2VudGVyWSA9IHdoZWVsUmVjdC50b3AgKyB3aGVlbFJlY3QuaGVpZ2h0LzI7CiAgdmFyIGNsb3Nlc3QgPSBudWxsLCBjbG9zZXN0RGlzdCA9IEluZmluaXR5OwogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5tYnRuJykuZm9yRWFjaChmdW5jdGlvbihidG4pewogICAgaWYoYnRuLnN0eWxlLmRpc3BsYXkgPT09ICdub25lJykgcmV0dXJuOwogICAgdmFyIHIgPSBidG4uZ2V0Qm91bmRpbmdDbGllbnRSZWN0KCk7CiAgICB2YXIgYnRuQ2VudGVyID0gci50b3AgKyByLmhlaWdodC8yOwogICAgdmFyIGRpc3QgPSBNYXRoLmFicyhidG5DZW50ZXIgLSBjZW50ZXJZKTsKICAgIGlmKGRpc3QgPCBjbG9zZXN0RGlzdCl7IGNsb3Nlc3REaXN0ID0gZGlzdDsgY2xvc2VzdCA9IGJ0bjsgfQogIH0pOwogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5tYnRuJykuZm9yRWFjaChmdW5jdGlvbihiKXsgYi5jbGFzc0xpc3QucmVtb3ZlKCd3aGVlbC1hY3RpdmUnKTsgfSk7CiAgaWYoY2xvc2VzdCl7CiAgICBjbG9zZXN0LmNsYXNzTGlzdC5hZGQoJ3doZWVsLWFjdGl2ZScpOwogICAgYm9va2luZy5tYXN0ZXIgPSBjbG9zZXN0LmdldEF0dHJpYnV0ZSgnZGF0YS1tYXN0ZXInKTsKICB9Cn0KCmZ1bmN0aW9uIGNlbnRlck1hc3RlckluV2hlZWwoYnRuLCBzbW9vdGgpewogIHZhciB3aGVlbCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdtYXN0ZXJXaGVlbCcpOwogIGlmKCF3aGVlbCB8fCAhYnRuKSByZXR1cm47CiAgdmFyIHRhcmdldCA9IGJ0bi5vZmZzZXRUb3AgLSAod2hlZWwuY2xpZW50SGVpZ2h0IC0gYnRuLmNsaWVudEhlaWdodCkvMjsKICB3aGVlbC5zY3JvbGxUbyh7dG9wOiB0YXJnZXQsIGJlaGF2aW9yOiBzbW9vdGggPyAnc21vb3RoJyA6ICdhdXRvJ30pOwp9Cgpkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcubWJ0bicpLmZvckVhY2goZnVuY3Rpb24oYnRuKXsKICBidG4ub25jbGljaz1mdW5jdGlvbigpewogICAgaWYoYnRuLnN0eWxlLmRpc3BsYXkgPT09ICdub25lJykgcmV0dXJuOwogICAgY2VudGVyTWFzdGVySW5XaGVlbChidG4sIHRydWUpOwogIH07Cn0pOwoKdmFyIG1hc3RlcldoZWVsRWwgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbWFzdGVyV2hlZWwnKTsKaWYobWFzdGVyV2hlZWxFbCl7CiAgdmFyIHdoZWVsU2Nyb2xsVGltZW91dDsKICBtYXN0ZXJXaGVlbEVsLmFkZEV2ZW50TGlzdGVuZXIoJ3Njcm9sbCcsIGZ1bmN0aW9uKCl7CiAgICB1cGRhdGVXaGVlbEFjdGl2ZSgpOwogICAgY2xlYXJUaW1lb3V0KHdoZWVsU2Nyb2xsVGltZW91dCk7CiAgICB3aGVlbFNjcm9sbFRpbWVvdXQgPSBzZXRUaW1lb3V0KHVwZGF0ZVdoZWVsQWN0aXZlLCAxMDApOwogIH0pOwp9Cgp2YXIgd2hlZWxDb25maXJtQnRuRWwgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnd2hlZWxDb25maXJtQnRuJyk7CmlmKHdoZWVsQ29uZmlybUJ0bkVsKXsKICB3aGVlbENvbmZpcm1CdG5FbC5vbmNsaWNrID0gZnVuY3Rpb24oKXsKICAgIGlmKCFib29raW5nLm1hc3Rlcil7IHVwZGF0ZVdoZWVsQWN0aXZlKCk7IH0KICAgIGlmKGJvb2tpbmcubWFzdGVyKXsgZ29TdGVwKDMpOyB9CiAgfTsKfQoKLy8gR3Jvb20gaGlzdG9yeQpkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuZ2J0bicpLmZvckVhY2goZnVuY3Rpb24oYnRuKXsKICBidG4ub25jbGljaz1mdW5jdGlvbigpewogICAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmdidG4nKS5mb3JFYWNoKGZ1bmN0aW9uKGIpe2IuY2xhc3NMaXN0LnJlbW92ZSgnYWN0aXZlJyk7fSk7CiAgICBidG4uY2xhc3NMaXN0LmFkZCgnYWN0aXZlJyk7CiAgICBib29raW5nLmdyb29tSGlzdG9yeT1idG4uZ2V0QXR0cmlidXRlKCdkYXRhLXZhbCcpOwogICAgc2V0VGltZW91dChmdW5jdGlvbigpe2dvU3RlcCg0KTtidWlsZENhbCgpO30sMzAwKTsKICB9Owp9KTsKCi8vIENhbGVuZGFyCmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdwcmV2TScpLm9uY2xpY2s9ZnVuY3Rpb24oKXtjTS0tO2lmKGNNPDApe2NNPTExO2NZLS07fWJ1aWxkQ2FsKCk7fTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ25leHRNJykub25jbGljaz1mdW5jdGlvbigpe2NNKys7aWYoY00+MTEpe2NNPTA7Y1krKzt9YnVpbGRDYWwoKTt9OwoKdmFyIGF2YWlsYWJsZURheXMgPSBbXTsKCmZ1bmN0aW9uIGxvYWRBdmFpbGFibGVEYXlzKCkgewogIHZhciBtYXN0ZXIgPSBib29raW5nLm1hc3RlcjsKICBpZiAoIW1hc3RlcikgcmV0dXJuOwogIGF2YWlsYWJsZURheXMgPSBbXTsKICBmZXRjaCh3aW5kb3cubG9jYXRpb24ub3JpZ2luICsgJy9hcGkvYXZhaWxhYmxlX2RheXM/bW9udGg9JyArIChjTSsxKSArICcmeWVhcj0nICsgY1kgKyAnJm1hc3Rlcj0nICsgZW5jb2RlVVJJQ29tcG9uZW50KG1hc3RlcikpCiAgICAudGhlbihmdW5jdGlvbihyKXsgcmV0dXJuIHIuanNvbigpOyB9KQogICAgLnRoZW4oZnVuY3Rpb24oZGF0YSl7CiAgICAgIGF2YWlsYWJsZURheXMgPSBkYXRhLmF2YWlsYWJsZSB8fCBbXTsKICAgICAgbWFya0F2YWlsYWJsZURheXMoKTsKICAgIH0pCiAgICAuY2F0Y2goZnVuY3Rpb24oKXsgYXZhaWxhYmxlRGF5cyA9IFtdOyB9KTsKfQoKZnVuY3Rpb24gbWFya0F2YWlsYWJsZURheXMoKSB7CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmNkJykuZm9yRWFjaChmdW5jdGlvbihjKXtpZighYy5jbGFzc0xpc3QuY29udGFpbnMoJ2RpcycpKWMuY2xhc3NMaXN0LnJlbW92ZSgnc2VsJyk7fSk7CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmNkOm5vdCguZGlzKTpub3QoLmNkbik6bm90KC5wYWQpJykuZm9yRWFjaChmdW5jdGlvbihlbCkgewogICAgdmFyIGRheSA9IGVsLnRleHRDb250ZW50LnRyaW0oKTsKICAgIGlmICghZGF5IHx8IGlzTmFOKHBhcnNlSW50KGRheSkpKSByZXR1cm47CiAgICB2YXIgZGF0ZVN0ciA9IFN0cmluZyhwYXJzZUludChkYXkpKS5wYWRTdGFydCgyLCcwJykgKyAnLicgKyBTdHJpbmcoY00rMSkucGFkU3RhcnQoMiwnMCcpICsgJy4nICsgY1k7CiAgICBpZiAoYXZhaWxhYmxlRGF5cy5pbmRleE9mKGRhdGVTdHIpICE9PSAtMSkgewogICAgICBlbC5jbGFzc0xpc3QuYWRkKCdhdmFpbCcpOwogICAgICBlbC5jbGFzc0xpc3QucmVtb3ZlKCdidXN5Jyk7CiAgICB9IGVsc2UgewogICAgICBlbC5jbGFzc0xpc3QuYWRkKCdidXN5Jyk7CiAgICAgIGVsLmNsYXNzTGlzdC5yZW1vdmUoJ2F2YWlsJyk7CiAgICB9CiAgfSk7Cn0KCmZ1bmN0aW9uIGJ1aWxkQ2FsKCl7CiAgbG9hZEF2YWlsYWJsZURheXMoKTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2FsTScpLnRleHRDb250ZW50PU1PTlRIU1tjTV0rJyAnK2NZOwogIGJvb2tpbmcuZGF0ZT0nJzsgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmNkJykuZm9yRWFjaChmdW5jdGlvbihjKXtjLmNsYXNzTGlzdC5yZW1vdmUoJ3NlbCcpO2MuY2xhc3NMaXN0LnJlbW92ZSgnYXZhaWwnKTtjLmNsYXNzTGlzdC5yZW1vdmUoJ2J1c3knKTt9KTsKICB2YXIgZz1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2FsRycpO2cuaW5uZXJIVE1MPScnOwogIFsn0J/QvScsJ9CS0YInLCfQodGAJywn0KfRgicsJ9Cf0YInLCfQodCxJywn0JLRgSddLmZvckVhY2goZnVuY3Rpb24oZCl7CiAgICB2YXIgZWw9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7ZWwuY2xhc3NOYW1lPSdjZG4nO2VsLnRleHRDb250ZW50PWQ7Zy5hcHBlbmRDaGlsZChlbCk7CiAgfSk7CiAgdmFyIGZpcnN0PW5ldyBEYXRlKGNZLGNNLDEpLmdldERheSgpOwogIHZhciBkYXlzPW5ldyBEYXRlKGNZLGNNKzEsMCkuZ2V0RGF0ZSgpOwogIHZhciBzdGFydD1maXJzdD09PTA/NjpmaXJzdC0xOwogIHZhciB0b2RheT1uZXcgRGF0ZSgpOwogIGZvcih2YXIgaT0wO2k8c3RhcnQ7aSsrKXt2YXIgZWw9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7ZWwuY2xhc3NOYW1lPSdjZCBwYWQnO2cuYXBwZW5kQ2hpbGQoZWwpO30KICBmb3IodmFyIGRheT0xO2RheTw9ZGF5cztkYXkrKyl7CiAgICB2YXIgZWw9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7ZWwuY2xhc3NOYW1lPSdjZCc7CiAgICB2YXIgZGF0ZT1uZXcgRGF0ZShjWSxjTSxkYXkpOwogICAgdmFyIGlzUGFzdD1kYXRlPG5ldyBEYXRlKHRvZGF5LmdldEZ1bGxZZWFyKCksdG9kYXkuZ2V0TW9udGgoKSx0b2RheS5nZXREYXRlKCkpOwogICAgdmFyIGlubmVyPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO2lubmVyLmNsYXNzTmFtZT0nY2QtaW5uZXInO2lubmVyLnRleHRDb250ZW50PWRheTtlbC5hcHBlbmRDaGlsZChpbm5lcik7CiAgICBpZihpc1Bhc3Qpe2VsLmNsYXNzTGlzdC5hZGQoJ2RpcycpO30KICAgIGVsc2V7CiAgICAgIGlmKGRhdGUudG9EYXRlU3RyaW5nKCk9PT10b2RheS50b0RhdGVTdHJpbmcoKSllbC5jbGFzc0xpc3QuYWRkKCd0b2QnKTsKICAgICAgKGZ1bmN0aW9uKGQsIGVsUmVmKXsKICAgICAgICBlbFJlZi5vbmNsaWNrPWZ1bmN0aW9uKCl7CiAgICAgICAgICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuY2QnKS5mb3JFYWNoKGZ1bmN0aW9uKGMpe2MuY2xhc3NMaXN0LnJlbW92ZSgnc2VsJyk7fSk7CiAgICAgICAgICBlbFJlZi5jbGFzc0xpc3QuYWRkKCdzZWwnKTsKICAgICAgICAgIGJvb2tpbmcuZGF0ZT1TdHJpbmcoZCkucGFkU3RhcnQoMiwnMCcpKycuJytTdHJpbmcoY00rMSkucGFkU3RhcnQoMiwnMCcpKycuJytjWTsKICAgICAgICAgIHNob3dUaW1lcygpOwogICAgICAgIH07CiAgICAgIH0pKGRheSwgZWwpOwogICAgfQogICAgZy5hcHBlbmRDaGlsZChlbCk7CiAgfQogIC8vIGZpbGwgdHJhaWxpbmcgY2VsbHMgdG8gY29tcGxldGUgbGFzdCBncmlkIHJvdwogIHZhciB0b3RhbCA9IHN0YXJ0ICsgZGF5czsKICB2YXIgdHJhaWwgPSAoNyAtICh0b3RhbCAlIDcpKSAlIDc7CiAgZm9yKHZhciB0PTA7dDx0cmFpbDt0Kyspe3ZhciBlcD1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdkaXYnKTtlcC5jbGFzc05hbWU9J2NkIHBhZCc7Zy5hcHBlbmRDaGlsZChlcCk7fQp9CgpmdW5jdGlvbiBzaG93VGltZXMoKXsKICB2YXIgdGc9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3RpbWVHJyk7CiAgdGcuaW5uZXJIVE1MPSc8ZGl2IGNsYXNzPSJsb2FkaW5nLXNsb3RzIj7ij7Mg0JfQsNCz0YDRg9C20LDQtdC8INGA0LDRgdC/0LjRgdCw0L3QuNC1Li4uPC9kaXY+JzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgndGltZVNlYycpLnN0eWxlLmRpc3BsYXk9J2Jsb2NrJzsKCiAgdmFyIHVybCA9IHdpbmRvdy5sb2NhdGlvbi5vcmlnaW4gKyAiL2FwaS9zbG90cyIgKyAnP2FjdGlvbj1zbG90cyZkYXRlPScgKyBlbmNvZGVVUklDb21wb25lbnQoYm9va2luZy5kYXRlKSArICcmbWFzdGVyPScgKyBlbmNvZGVVUklDb21wb25lbnQoYm9va2luZy5tYXN0ZXIpOwoKICBmZXRjaCh1cmwpCiAgICAudGhlbihmdW5jdGlvbihyKXtyZXR1cm4gci5qc29uKCk7fSkKICAgIC50aGVuKGZ1bmN0aW9uKGRhdGEpewogICAgICB2YXIgc2xvdHMgPSAoZGF0YS5zbG90cyAmJiBkYXRhLnNsb3RzLmxlbmd0aCA+IDApID8gZGF0YS5zbG90cyA6IFtdOwogICAgICByZW5kZXJUaW1lU2xvdHMoc2xvdHMpOwogICAgfSkKICAgIC5jYXRjaChmdW5jdGlvbigpewogICAgICByZW5kZXJUaW1lU2xvdHMoW10pOwogICAgfSk7Cn0KCmZ1bmN0aW9uIHJlbmRlclRpbWVTbG90cyhzbG90cyl7CiAgdmFyIHRnPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCd0aW1lRycpO3RnLmlubmVySFRNTD0nJzsKICBpZihzbG90cy5sZW5ndGg9PT0wKXsKICAgIHRnLmlubmVySFRNTD0nPGRpdiBjbGFzcz0ibG9hZGluZy1zbG90cyI+0J3QtdGCINC00L7RgdGC0YPQv9C90YvRhSDRgdC70L7RgtC+0LIg0L3QsCDRjdGC0YMg0LTQsNGC0YM8L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXIiIG9uY2xpY2s9InNob3dTY3JlZW4oXCdob21lU2NyZWVuXCcpIiBzdHlsZT0ibWFyZ2luLXRvcDo4cHg7Ij48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItaWNvbiI+8J+QvjwvZGl2PjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci10ZXh0Ij48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItdGl0bGUiPtCd0LUg0L3QsNGI0LvQuCDQv9C+0LTRhdC+0LTRj9GJ0LXQtSDQstGA0LXQvNGPPzwvZGl2PjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci1zdWIiPtCh0LLRj9C20LjRgtC10YHRjCDRgSDQvdCw0LzQuCDQu9GO0LHRi9C8INGD0LTQvtCx0L3Ri9C8INGB0L/QvtGB0L7QsdC+0Lwg4oCUINC80Ysg0L/QvtC00LHQtdGA0ZHQvCDRg9C00L7QsdC90L7QtSDQstGA0LXQvNGPPC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLWFycm93Ij7ihpI8L2Rpdj48L2Rpdj4nOwogICAgcmV0dXJuOwogIH0KICBzbG90cy5mb3JFYWNoKGZ1bmN0aW9uKHQpewogICAgdmFyIGJ0bj1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdidXR0b24nKTtidG4uY2xhc3NOYW1lPSd0YnRuJztidG4udGV4dENvbnRlbnQ9dDsKICAgIGJ0bi5vbmNsaWNrPWZ1bmN0aW9uKCl7CiAgICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy50YnRuJykuZm9yRWFjaChmdW5jdGlvbihiKXtiLmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpO30pOwogICAgICBidG4uY2xhc3NMaXN0LmFkZCgnYWN0aXZlJyk7Ym9va2luZy50aW1lPXQ7CiAgICAgIHNldFRpbWVvdXQoZnVuY3Rpb24oKXtnb1N0ZXAoNSk7YnVpbGRTdW0oKTt9LDMwMCk7CiAgICB9OwogICAgdGcuYXBwZW5kQ2hpbGQoYnRuKTsKICB9KTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgndGltZVNlYycpLnNjcm9sbEludG9WaWV3KHtiZWhhdmlvcjonc21vb3RoJyxibG9jazonbmVhcmVzdCd9KTsKfQoKZnVuY3Rpb24gYnVpbGRTdW0oKXsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3VtQmxvY2snKS5pbm5lckhUTUw9CiAgICAnPGRpdiBjbGFzcz0ic3IiPjxzcGFuIGNsYXNzPSJzbCI+JytUW0xBTkddLnN1bV9icmVlZCsnPC9zcGFuPjxzcGFuIGNsYXNzPSJzdiI+JysoYm9va2luZy5icmVlZERpc3BsYXl8fGJvb2tpbmcuYnJlZWQpKyc8L3NwYW4+PC9kaXY+JysKICAgICc8ZGl2IGNsYXNzPSJzciI+PHNwYW4gY2xhc3M9InNsIj4nK1RbTEFOR10uc3VtX3NlcnZpY2UrJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrKChMQU5HIT09J3J1JyYmU1ZDX1RSQU5TTEFUSU9OU1tib29raW5nLnNlcnZpY2VdKT9TVkNfVFJBTlNMQVRJT05TW2Jvb2tpbmcuc2VydmljZV1bTEFOR106Ym9va2luZy5zZXJ2aWNlKSsnPC9zcGFuPjwvZGl2PicrCiAgICAnPGRpdiBjbGFzcz0ic3IiPjxzcGFuIGNsYXNzPSJzbCI+JytUW0xBTkddLnN1bV9tYXN0ZXIrJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrYm9va2luZy5tYXN0ZXIrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fZ3Jvb20rJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrYm9va2luZy5ncm9vbUhpc3RvcnkrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fZGF0ZSsnPC9zcGFuPjxzcGFuIGNsYXNzPSJzdiI+Jytib29raW5nLmRhdGUrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fdGltZSsnPC9zcGFuPjxzcGFuIGNsYXNzPSJzdiI+Jytib29raW5nLnRpbWUrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fcHJpY2UrJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3AiPicrYm9va2luZy5wcmljZSsnIOKCrDwvc3Bhbj48L2Rpdj4nOwp9Cgpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY29uZmlybUJ0bicpLm9uY2xpY2sgPSBmdW5jdGlvbigpewogIHZhciBuYW1lPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjTmFtZScpLnZhbHVlOwogIHZhciBwaG9uZT1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY1Bob25lJykudmFsdWU7CiAgaWYoIW5hbWV8fCFwaG9uZSl7YWxlcnQoVFtMQU5HXS5hbGVydF9maWxsKTtyZXR1cm47fQogIGlmKCEvXlwrXGR7MTAsfSQvLnRlc3QocGhvbmUudHJpbSgpKSl7YWxlcnQoVFtMQU5HXS5hbGVydF9waG9uZSk7cmV0dXJuO30KICBib29raW5nLm5hbWU9bmFtZTsgYm9va2luZy5waG9uZT1waG9uZTsgYm9va2luZy5lbWFpbD1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY0VtYWlsJykudmFsdWU7IGJvb2tpbmcucGV0PWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjUGV0JykudmFsdWU7IGJvb2tpbmcubGFuZz1MQU5HOwogIGJvb2tpbmcuZHVyYXRpb24gPSBib29raW5nLmJyZWVkID09PSAn0KnQtdC90LrQuCcgPyA2MCA6IChib29raW5nLmJyZWVkICYmIGJvb2tpbmcuYnJlZWQuaW5kZXhPZign0JrQvtGI0LrQsCcpID09PSAwID8gMTIwIDogMTgwKTsKICB2YXIgYnRuPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjb25maXJtQnRuJyk7CiAgYnRuLnRleHRDb250ZW50PVRbTEFOR10uc2VuZGluZzsgYnRuLmRpc2FibGVkPXRydWU7CiAgZmV0Y2goUkFJTFdBWSwgewogICAgbWV0aG9kOidQT1NUJywKICAgIGhlYWRlcnM6eydDb250ZW50LVR5cGUnOidhcHBsaWNhdGlvbi9qc29uJ30sCiAgICBib2R5OkpTT04uc3RyaW5naWZ5KGJvb2tpbmcpCiAgfSkudGhlbihmdW5jdGlvbigpe3Nob3dTdWNjZXNzKCk7fSkuY2F0Y2goZnVuY3Rpb24oKXtzaG93U3VjY2VzcygpO30pOwp9OwoKZnVuY3Rpb24gc2hvd1N1Y2Nlc3MoKXsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnYms1JykuY2xhc3NOYW1lPSdzdGVwJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3VjQmxvY2snKS5jbGFzc0xpc3QuYWRkKCdzaG93Jyk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3Byb2dyZXNzJykuc3R5bGUuZGlzcGxheT0nbm9uZSc7Cn0KCmZ1bmN0aW9uIHJlc2V0QWxsKCl7CiAgYm9va2luZz17YnJlZWQ6JycsYnJlZWREaXNwbGF5OicnLHNlcnZpY2U6JycscHJpY2U6MCxtYXN0ZXI6JycsZ3Jvb21IaXN0b3J5OicnLGRhdGU6JycsdGltZTonJyxsYW5nOidydSd9OwogIHNlbEJyZWVkPW51bGw7IGlucC52YWx1ZT0nJzsgY2xyLmNsYXNzTGlzdC5yZW1vdmUoJ3Nob3cnKTsKICBiYWRnZS5jbGFzc0xpc3QucmVtb3ZlKCdzaG93Jyk7IGJhZGdlLmlubmVySFRNTD0nJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjU2VjJykuc3R5bGUuZGlzcGxheT0nbm9uZSc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3RpbWVTZWMnKS5zdHlsZS5kaXNwbGF5PSdub25lJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3VjQmxvY2snKS5jbGFzc0xpc3QucmVtb3ZlKCdzaG93Jyk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3Byb2dyZXNzJykuc3R5bGUuZGlzcGxheT0nZmxleCc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NOYW1lJykudmFsdWU9Jyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NQaG9uZScpLnZhbHVlPScnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjRW1haWwnKS52YWx1ZT0nJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY1BldCcpLnZhbHVlPScnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjb25maXJtQnRuJykudGV4dENvbnRlbnQ9VFtMQU5HXS5jb25maXJtX2J0bjsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY29uZmlybUJ0bicpLmRpc2FibGVkPWZhbHNlOwogIGdvU3RlcCgxKTsKfQoKdmFyIExBTkcgPSBsb2NhbFN0b3JhZ2UuZ2V0SXRlbSgncmpsYW5nJykgfHwgJ3J1JzsKdmFyIFQgPSB7CiAgcnU6ewogICAgbG9nb190YWc6J9Cf0YDQtdC80LjQsNC70YzQvdGL0Lkg0LPRgNGD0LzQuNC90LMtPGJyPtGB0LDQu9C+0L0g0LIg0KLQsNC70LvQuNC90LUnLAogICAgY2hvb3NlX2hvdzonQ2hvb3NlIGhvdyB0byBjb25uZWN0JywKICAgIGJvb2tfb25saW5lOifQntC90LvQsNC50L0g0LHRgNC+0L3QuNGA0L7QstCw0L3QuNC1JywKICAgIGJvb2tfZmxvdzon0J/QvtGA0L7QtNCwIOKGkiDQo9GB0LvRg9Cz0LAg4oaSINCc0LDRgdGC0LXRgCDihpIg0JLRgNC10LzRjycsCiAgICBvcl9jb250YWN0OifQuNC70Lgg0YHQstGP0LbQuNGC0LXRgdGMINGBINC90LDQvNC4JywKICAgIGNhbGxfdXM6J9Cf0L7Qt9Cy0L7QvdC40YLQtSDQvdCw0LwnLAogICAgYmFjazon4oaQINCd0LDQt9Cw0LQnLAogICAgbG9nb19zdWI6J0dyb29taW5nIMK3INCi0LDQu9C70LjQvScsCiAgICBwc19zZXJ2aWNlOifQo9GB0LvRg9Cz0LAnLHBzX21hc3Rlcjon0JzQsNGB0YLQtdGAJyxwc19wZXQ6J9Cf0LjRgtC+0LzQtdGGJyxwc19kYXRlOifQlNCw0YLQsCcscHNfZGV0YWlsczon0JTQsNC90L3Ri9C1JywKICAgIHN0ZXAxX2xibDonMDEgwrcg0J/QvtGA0L7QtNCwJywKICAgIGJyZWVkX3BoOifQndCw0YfQvdC40YLQtSDQstCy0L7QtNC40YLRjCDQv9C+0YDQvtC00YMuLi4nLAogICAgc3RlcDJfbGJsOicwMiDCtyDQo9GB0LvRg9Cz0LAnLAogICAgc3RlcDJfbWFzdGVyOifQktGL0LHQtdGA0LjRgtC1INC80LDRgdGC0LXRgNCwJywKICAgIHN0ZXAzX2xibDon0JrQsNC6INC00LDQstC90L4g0LLRiyDQv9C+0YHQtdGJ0LDQu9C4INCz0YDRg9C80LjQvdCzPycsCiAgICBnMTon0J/QtdGA0LLRi9C5INGA0LDQtycsZzI6J9Ce0YIgMSDQtNC+IDMg0LzQtdGB0Y/RhtC10LInLGczOifQntGCIDMg0LTQviA2INC80LXRgdGP0YbQtdCyJyxnNDon0JHQvtC70LXQtSA2INC80LXRgdGP0YbQtdCyJywKICAgIHN0ZXA0X2xibDon0JLRi9Cx0LXRgNC40YLQtSDQtNCw0YLRgycsCiAgICBjYWxfYXZhaWw6J9CV0YHRgtGMINGB0LLQvtCx0L7QtNC90L7QtSDQstGA0LXQvNGPJyxjYWxfbm9uZTon0KHQstC+0LHQvtC00L3QvtCz0L4g0LLRgNC10LzQtdC90Lgg0L3QtdGCJywKICAgIHN0ZXA0X3RpbWU6J9CS0YvQsdC10YDQuNGC0LUg0LLRgNC10LzRjycsCiAgICBzdGVwNV9sYmw6J9CS0LDRiNC4INC00LDQvdC90YvQtScsCiAgICBsYmxfbmFtZTon0JjQvNGPJyxwaF9uYW1lOifQktCw0YjQtSDQuNC80Y8nLAogICAgbGJsX3Bob25lOifQotC10LvQtdGE0L7QvScsbGJsX2VtYWlsOidFbWFpbCcsCiAgICBsYmxfcGV0OifQmtC70LjRh9C60LAg0L/QuNGC0L7QvNGG0LAnLHBoX29wdGlvbmFsOifQndC10L7QsdGP0LfQsNGC0LXQu9GM0L3QvicsCiAgICBjb25maXJtX2J0bjon0J/QvtC00YLQstC10YDQtNC40YLRjCDQt9Cw0L/QuNGB0YwnLAogICAgc3VjY2Vzc190aXRsZTon0JfQsNC/0LjRgdGMINC/0YDQuNC90Y/RgtCwIScsCiAgICBzdWNjZXNzX3N1Yjon0JzRiyDRgdCy0Y/QttC10LzRgdGPINGBINCy0LDQvNC4INC00LvRjyDQv9C+0LTRgtCy0LXRgNC20LTQtdC90LjRjy48YnI+0KHQv9Cw0YHQuNCx0L4sINGH0YLQviDQstGL0LHRgNCw0LvQuCBSJmFtcDtKIEdyb29taW5nIScsCiAgICB0b19ob21lOifihpAg0J3QsCDQs9C70LDQstC90YPRjicsCiAgICBhbGVydF9maWxsOifQktCy0LXQtNC40YLQtSDQuNC80Y8g0Lgg0YLQtdC70LXRhNC+0L0nLGFsZXJ0X3Bob25lOifQktCy0LXQtNC40YLQtSDQvdC+0LzQtdGAINCyINGE0L7RgNC80LDRgtC1ICszNzIxMjM0NTY3OCcsCiAgICBzZW5kaW5nOifQntGC0L/RgNCw0LLQu9GP0LXQvC4uLicsCiAgICBzdW1fYnJlZWQ6J9Cf0L7RgNC+0LTQsCcsc3VtX3NlcnZpY2U6J9Cj0YHQu9GD0LPQsCcsc3VtX21hc3Rlcjon0JzQsNGB0YLQtdGAJyxzdW1fZ3Jvb206J9Cf0L7RgdC70LXQtNC90LjQuSDQs9GA0YPQvCcsc3VtX2RhdGU6J9CU0LDRgtCwJyxzdW1fdGltZTon0JLRgNC10LzRjycsc3VtX3ByaWNlOifQodGC0L7QuNC80L7RgdGC0YwnLAogICAgbW9udGhzOlsn0K/QvdCy0LDRgNGMJywn0KTQtdCy0YDQsNC70YwnLCfQnNCw0YDRgicsJ9CQ0L/RgNC10LvRjCcsJ9Cc0LDQuScsJ9CY0Y7QvdGMJywn0JjRjtC70YwnLCfQkNCy0LPRg9GB0YInLCfQodC10L3RgtGP0LHRgNGMJywn0J7QutGC0Y/QsdGA0YwnLCfQndC+0Y/QsdGA0YwnLCfQlNC10LrQsNCx0YDRjCddCiAgfSwKICBlbjp7CiAgICBsb2dvX3RhZzonUHJlbWl1bSBncm9vbWluZzxicj5zYWxvbiBpbiBUYWxsaW5uJywKICAgIGNob29zZV9ob3c6J0Nob29zZSBob3cgdG8gY29ubmVjdCcsCiAgICBib29rX29ubGluZTonQm9vayBPbmxpbmUnLAogICAgYm9va19mbG93OidCcmVlZCDihpIgU2VydmljZSDihpIgTWFzdGVyIOKGkiBUaW1lJywKICAgIG9yX2NvbnRhY3Q6J29yIGNvbnRhY3QgdXMnLAogICAgY2FsbF91czonQ2FsbCBVcycsCiAgICBiYWNrOifihpAgQmFjaycsCiAgICBsb2dvX3N1YjonR3Jvb21pbmcgwrcgVGFsbGlubicsCiAgICBwc19zZXJ2aWNlOidTZXJ2aWNlJyxwc19tYXN0ZXI6J01hc3RlcicscHNfcGV0OidQZXQnLHBzX2RhdGU6J0RhdGUnLHBzX2RldGFpbHM6J0RldGFpbHMnLAogICAgc3RlcDFfbGJsOicwMSDCtyBEb2cgYnJlZWQnLAogICAgYnJlZWRfcGg6J1N0YXJ0IHR5cGluZyBicmVlZC4uLicsCiAgICBzdGVwMl9sYmw6JzAyIMK3IFNlcnZpY2UnLAogICAgc3RlcDJfbWFzdGVyOidDaG9vc2UgbWFzdGVyJywKICAgIHN0ZXAzX2xibDonSG93IGxvbmcgYWdvIHdhcyB5b3VyIGxhc3QgZ3Jvb21pbmc/JywKICAgIGcxOidGaXJzdCB0aW1lJyxnMjonMeKAkzMgbW9udGhzIGFnbycsZzM6JzPigJM2IG1vbnRocyBhZ28nLGc0OidPdmVyIDYgbW9udGhzJywKICAgIHN0ZXA0X2xibDonQ2hvb3NlIGRhdGUnLAogICAgY2FsX2F2YWlsOidBdmFpbGFibGUnLGNhbF9ub25lOidOb3QgYXZhaWxhYmxlJywKICAgIHN0ZXA0X3RpbWU6J0Nob29zZSB0aW1lJywKICAgIHN0ZXA1X2xibDonWW91ciBkZXRhaWxzJywKICAgIGxibF9uYW1lOidOYW1lJyxwaF9uYW1lOidZb3VyIG5hbWUnLAogICAgbGJsX3Bob25lOidQaG9uZScsbGJsX2VtYWlsOidFbWFpbCcsCiAgICBsYmxfcGV0OiJQZXQncyBuYW1lIixwaF9vcHRpb25hbDonT3B0aW9uYWwnLAogICAgY29uZmlybV9idG46J0NvbmZpcm0gYm9va2luZycsCiAgICBzdWNjZXNzX3RpdGxlOidCb29raW5nIGNvbmZpcm1lZCEnLAogICAgc3VjY2Vzc19zdWI6J1dlIHdpbGwgY29udGFjdCB5b3UgdG8gY29uZmlybS48YnI+VGhhbmsgeW91IGZvciBjaG9vc2luZyBSJmFtcDtKIEdyb29taW5nIScsCiAgICB0b19ob21lOifihpAgSG9tZScsCiAgICBhbGVydF9maWxsOidQbGVhc2UgZW50ZXIgbmFtZSBhbmQgcGhvbmUnLGFsZXJ0X3Bob25lOidFbnRlciBwaG9uZSBudW1iZXIgaW4gZm9ybWF0ICszNzIxMjM0NTY3OCcsCiAgICBzZW5kaW5nOidTZW5kaW5nLi4uJywKICAgIHN1bV9icmVlZDonQnJlZWQnLHN1bV9zZXJ2aWNlOidTZXJ2aWNlJyxzdW1fbWFzdGVyOidNYXN0ZXInLHN1bV9ncm9vbTonTGFzdCBncm9vbWluZycsc3VtX2RhdGU6J0RhdGUnLHN1bV90aW1lOidUaW1lJyxzdW1fcHJpY2U6J1ByaWNlJywKICAgIG1vbnRoczpbJ0phbnVhcnknLCdGZWJydWFyeScsJ01hcmNoJywnQXByaWwnLCdNYXknLCdKdW5lJywnSnVseScsJ0F1Z3VzdCcsJ1NlcHRlbWJlcicsJ09jdG9iZXInLCdOb3ZlbWJlcicsJ0RlY2VtYmVyJ10KICB9LAogIGV0OnsKICAgIGxvZ29fdGFnOidFc21ha2xhc3NpbGluZSBob29sZHVzdGVlbnVzPGJyPlRhbGxpbm5hcycsCiAgICBjaG9vc2VfaG93OidWYWxpIMO8aGVuZHVzdmlpcycsCiAgICBib29rX29ubGluZTonQnJvbmVlcmkgdmVlYmlzJywKICAgIGJvb2tfZmxvdzonVMO1dWcg4oaSIFRlZW51cyDihpIgTWVpc3RlciDihpIgQWVnJywKICAgIG9yX2NvbnRhY3Q6J3bDtWkgdsO1dGEgw7xoZW5kdXN0JywKICAgIGNhbGxfdXM6J0hlbGlzdGEgbWVpbGUnLAogICAgYmFjazon4oaQIFRhZ2FzaScsCiAgICBsb2dvX3N1YjonR3Jvb21pbmcgwrcgVGFsbGlubicsCiAgICBwc19zZXJ2aWNlOidUZWVudXMnLHBzX21hc3RlcjonTWVpc3RlcicscHNfcGV0OidMZW1taWtsb29tJyxwc19kYXRlOidLdXVww6RldicscHNfZGV0YWlsczonQW5kbWVkJywKICAgIHN0ZXAxX2xibDonMDEgwrcgS29lcmEgdMO1dWcnLAogICAgYnJlZWRfcGg6J0FsdXN0YWdlIHTDtXUgc2lzZXN0YW1pc3QuLi4nLAogICAgc3RlcDJfbGJsOicwMiDCtyBUZWVudXMnLAogICAgc3RlcDJfbWFzdGVyOidWYWxpIG1laXN0ZXInLAogICAgc3RlcDNfbGJsOidNaWxsYWwga8OkaXNpdGUgdmlpbWF0aSBncm9vbWluZ3VzPycsCiAgICBnMTonRXNpbWVzdCBrb3JkYScsZzI6JzHigJMzIGt1dWQgdGFnYXNpJyxnMzonM+KAkzYga3V1ZCB0YWdhc2knLGc0OifDnGxlIDYga3V1JywKICAgIHN0ZXA0X2xibDonVmFsaSBrdXVww6RldicsCiAgICBjYWxfYXZhaWw6J1ZhYnUgYWVndSBvbicsY2FsX25vbmU6J1ZhYnUgYWVndSBwb2xlJywKICAgIHN0ZXA0X3RpbWU6J1ZhbGkga2VsbGFhZWcnLAogICAgc3RlcDVfbGJsOidUZWllIGFuZG1lZCcsCiAgICBsYmxfbmFtZTonTmltaScscGhfbmFtZTonVGVpZSBuaW1pJywKICAgIGxibF9waG9uZTonVGVsZWZvbicsbGJsX2VtYWlsOidFbWFpbCcsCiAgICBsYmxfcGV0OidMZW1taWtsb29tYSBuaW1pJyxwaF9vcHRpb25hbDonVmFsaWt1bGluZScsCiAgICBjb25maXJtX2J0bjonS2lubml0YSBicm9uZWVyaW5nJywKICAgIHN1Y2Nlc3NfdGl0bGU6J0Jyb25lZXJpbmcga2lubml0YXR1ZCEnLAogICAgc3VjY2Vzc19zdWI6J1bDtXRhbWUgdGVpZWdhIMO8aGVuZHVzdCBraW5uaXRhbWlzZWtzLjxicj5Uw6RuYW1lLCBldCB2YWxpc2l0ZSBSJmFtcDtKIEdyb29taW5nIScsCiAgICB0b19ob21lOifihpAgQXZhbGVoZWxlJywKICAgIGFsZXJ0X2ZpbGw6J1BhbHVuIHNpc2VzdGFnZSBuaW1pIGphIHRlbGVmb24nLGFsZXJ0X3Bob25lOidTaXNlc3RhZ2UgdGVsZWZvbmludW1iZXIgdm9ybWluZ3VzICszNzIxMjM0NTY3OCcsCiAgICBzZW5kaW5nOidTYWFkYW4uLi4nLAogICAgc3VtX2JyZWVkOidUw7V1Zycsc3VtX3NlcnZpY2U6J1RlZW51cycsc3VtX21hc3RlcjonTWVpc3Rlcicsc3VtX2dyb29tOidWaWltYW5lIGdyb29taW5nJyxzdW1fZGF0ZTonS3V1cMOkZXYnLHN1bV90aW1lOidLZWxsYWFlZycsc3VtX3ByaWNlOidIaW5kJywKICAgIG1vbnRoczpbJ0phYW51YXInLCdWZWVicnVhcicsJ03DpHJ0cycsJ0FwcmlsbCcsJ01haScsJ0p1dW5pJywnSnV1bGknLCdBdWd1c3QnLCdTZXB0ZW1iZXInLCdPa3Rvb2JlcicsJ05vdmVtYmVyJywnRGV0c2VtYmVyJ10KICB9Cn07CgpmdW5jdGlvbiBzZXRMYW5nKGwpewogIExBTkc9bDsKICBsb2NhbFN0b3JhZ2Uuc2V0SXRlbSgncmpsYW5nJyxsKTsKICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcubGFuZy1idG4nKS5mb3JFYWNoKGZ1bmN0aW9uKGIpewogICAgYi5jbGFzc0xpc3QudG9nZ2xlKCdhY3RpdmUnLCBiLnRleHRDb250ZW50LnRvTG93ZXJDYXNlKCk9PT1sKTsKICB9KTsKICB2YXIgdHI9VFtsXTsKICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCdbZGF0YS1pMThuXScpLmZvckVhY2goZnVuY3Rpb24oZWwpewogICAgdmFyIGs9ZWwuZ2V0QXR0cmlidXRlKCdkYXRhLWkxOG4nKTsKICAgIGlmKHRyW2tdIT09dW5kZWZpbmVkKSBlbC5pbm5lckhUTUw9dHJba107CiAgfSk7CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnW2RhdGEtaTE4bi1waF0nKS5mb3JFYWNoKGZ1bmN0aW9uKGVsKXsKICAgIHZhciBrPWVsLmdldEF0dHJpYnV0ZSgnZGF0YS1pMThuLXBoJyk7CiAgICBpZih0cltrXSE9PXVuZGVmaW5lZCkgZWwucGxhY2Vob2xkZXI9dHJba107CiAgfSk7CiAgTU9OVEhTPXRyLm1vbnRoczsKICBidWlsZENhbCgpOwogIC8vIFJlLXJlbmRlciBiYWRnZSBhbmQgc2VydmljZXMgaWYgYnJlZWQgYWxyZWFkeSBzZWxlY3RlZAogIGlmKHNlbEJyZWVkKXsKICAgIHZhciBiZj1sPT09J2VuJz8nYnJlZWRfZW4nOmw9PT0nZXQnPydicmVlZF9ldCc6J2JyZWVkJzsKICAgIHZhciBkYj1zZWxCcmVlZFtiZl18fHNlbEJyZWVkLmJyZWVkOwogICAgYm9va2luZy5icmVlZERpc3BsYXk9ZGI7CiAgICB2YXIgYm5FbD1kb2N1bWVudC5xdWVyeVNlbGVjdG9yKCcjc0JhZGdlIC5ibmFtZScpOwogICAgaWYoYm5FbCkgYm5FbC50ZXh0Q29udGVudD1kYjsKICAgIHZhciBiY0VsPWRvY3VtZW50LnF1ZXJ5U2VsZWN0b3IoJyNzQmFkZ2UgLmJjaGcnKTsKICAgIGlmKGJjRWwpIGJjRWwudGV4dENvbnRlbnQ9bD09PSdlbic/J0NoYW5nZSc6bD09PSdldCc/J011dWRhJzon0JjQt9C80LXQvdC40YLRjCc7CiAgICBpZihkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjU2VjJykuc3R5bGUuZGlzcGxheSE9PSdub25lJykgcmVuZGVyU3ZjcyhzZWxCcmVlZCk7CiAgICB2YXIgc249ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y05vdGUnKTsKICAgIGlmKHNuKXsKICAgICAgdmFyIG50PWw9PT0nZW4nPydQbGVhc2Ugbm90ZSc6bD09PSdldCc/J1BhbmdlIHTDpGhlbGUnOifQktCw0LbQvdC+INC30L3QsNGC0YwnOwogICAgICB2YXIgbmI9bD09PSdlbic/J0ZpbmFsIHByaWNlIGRlcGVuZHMgb24gY29hdCBjb25kaXRpb24gYW5kIHBldCBiZWhhdmlvdXIuPGJyPkRlbWF0dGluZyBmcm9tIDUg4oKsLjxicj5BZ2dyZXNzaXZlIGJlaGF2aW91ciBzdXJjaGFyZ2UgbWF5IGFwcGx5OiArNTAlLic6bD09PSdldCc/J0zDtXBsaWsgaGluZCBzw7VsdHViIGthcnZhc3Rpa3Ugc2Vpc3VuZGlzdCBqYSBsZW1taWtsb29tYSBrw6RpdHVtaXNlc3QuPGJyPktvbHRzdW5pdGUgbGFodGloYXJ1dGFtaW5lIGFsYXRlcyA1IOKCrC48YnI+QWdyZXNzaWl2c2Uga8OkaXR1bWlzZSBrb3JyYWwgdsO1aWIgbGlzYW5kdWRhIDUwJSBqdXVyZGVoaW5kbHVzLic6J9Ce0LrQvtC90YfQsNGC0LXQu9GM0L3QsNGPINGB0YLQvtC40LzQvtGB0YLRjCDQt9Cw0LLQuNGB0LjRgiDQvtGCINGB0L7RgdGC0L7Rj9C90LjRjyDRiNC10YDRgdGC0Lgg0Lgg0L/QvtCy0LXQtNC10L3QuNGPINC/0LjRgtC+0LzRhtCwLjxicj7QoNCw0LfQsdC+0YAg0LrQvtC70YLRg9C90L7QsiDigJQg0L7RgiA1IOKCrC48YnI+0J/RgNC4INCw0LPRgNC10YHRgdC40LLQvdC+0Lwg0L/QvtCy0LXQtNC10L3QuNC4INC80L7QttC10YIg0L/RgNC40LzQtdC90Y/RgtGM0YHRjyDQtNC+0L/Qu9Cw0YLQsCA1MCUuJzsKICAgICAgc24uaW5uZXJIVE1MPSc8ZGl2IHN0eWxlPSJmb250LXNpemU6MC44MzhyZW07bGV0dGVyLXNwYWNpbmc6LjE1ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbTo4cHg7Zm9udC13ZWlnaHQ6NjAwO2ZvbnQtZmFtaWx5OlwnTW9udHNlcnJhdFwnLHNhbnMtc2VyaWYiPicrbnQrJzwvZGl2PjxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxLjAyNXJlbTtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjEuODtmb250LWZhbWlseTpcJ01vbnRzZXJyYXRcJyxzYW5zLXNlcmlmIj4nK25iKyc8L2Rpdj4nOwogICAgfQogIH0KfQoKLy8gQXBwbHkgc2F2ZWQgbGFuZ3VhZ2Ugb24gbG9hZAooZnVuY3Rpb24oKXsgc2V0TGFuZyhMQU5HKTsgfSkoKTsKCi8vIENhbGxiYWNrIGZvcm0KZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NhbGxiYWNrQnRuJykub25jbGljayA9IGZ1bmN0aW9uKCl7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia01vZGFsJykuc3R5bGUuZGlzcGxheSA9ICdmbGV4JzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrTmFtZScpLnZhbHVlID0gJyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1Bob25lJykudmFsdWUgPSAnJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrU3VjY2VzcycpLnN0eWxlLmRpc3BsYXkgPSAnbm9uZSc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1N1Ym1pdCcpLnN0eWxlLmRpc3BsYXkgPSAnYmxvY2snOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtDbG9zZScpLnRleHRDb250ZW50ID0gJ9Ce0YLQvNC10L3QsCc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1N1Ym1pdCcpLnRleHRDb250ZW50ID0gJ9Ce0YLQv9GA0LDQstC40YLRjCc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1N1Ym1pdCcpLmRpc2FibGVkID0gZmFsc2U7Cn07CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtDbG9zZScpLm9uY2xpY2sgPSBmdW5jdGlvbigpewogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtNb2RhbCcpLnN0eWxlLmRpc3BsYXkgPSAnbm9uZSc7Cn07CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtTdWJtaXQnKS5vbmNsaWNrID0gZnVuY3Rpb24oKXsKICB2YXIgbmFtZSA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtOYW1lJykudmFsdWUudHJpbSgpOwogIHZhciBwaG9uZSA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtQaG9uZScpLnZhbHVlLnRyaW0oKS5yZXBsYWNlKC9cRC9nLCcnKTsKICBpZighbmFtZSB8fCAhcGhvbmUpe2FsZXJ0KCfQktCy0LXQtNC40YLQtSDQuNC80Y8g0Lgg0YLQtdC70LXRhNC+0L0nKTtyZXR1cm47fQogIHZhciBidG4gPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrU3VibWl0Jyk7CiAgYnRuLnRleHRDb250ZW50ID0gJ9Ce0YLQv9GA0LDQstC70Y/QtdC8Li4uJzsgYnRuLmRpc2FibGVkID0gdHJ1ZTsKICBmZXRjaCgnL2FwaS9jYWxsYmFjaycsewogICAgbWV0aG9kOidQT1NUJywKICAgIGhlYWRlcnM6eydDb250ZW50LVR5cGUnOidhcHBsaWNhdGlvbi9qc29uJ30sCiAgICBib2R5OkpTT04uc3RyaW5naWZ5KHtuYW1lOm5hbWUsIHBob25lOicrMzcyJytwaG9uZX0pCiAgfSkudGhlbihmdW5jdGlvbigpewogICAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1N1Y2Nlc3MnKS5zdHlsZS5kaXNwbGF5ID0gJ2Jsb2NrJzsKICAgIGJ0bi5zdHlsZS5kaXNwbGF5ID0gJ25vbmUnOwogICAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia0Nsb3NlJykudGV4dENvbnRlbnQgPSAn4oaQINCX0LDQutGA0YvRgtGMJzsKICAgIHNldFRpbWVvdXQoZnVuY3Rpb24oKXtkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrTW9kYWwnKS5zdHlsZS5kaXNwbGF5PSdub25lJzt9LDMwMDApOwogIH0pLmNhdGNoKGZ1bmN0aW9uKCl7CiAgICBidG4udGV4dENvbnRlbnQgPSAn0J7RgtC/0YDQsNCy0LjRgtGMJzsgYnRuLmRpc2FibGVkID0gZmFsc2U7CiAgICBhbGVydCgn0J7RiNC40LHQutCwLiDQn9C+0L/RgNC+0LHRg9C50YLQtSDQtdGJ0ZEg0YDQsNC3LicpOwogIH0pOwp9OwoKPC9zY3JpcHQ+CjwvYm9keT4KPC9odG1sPgo="



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
STATS_HTML_B64 = "PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9InJ1Ij4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCxpbml0aWFsLXNjYWxlPTEiPgo8bWV0YSBuYW1lPSJ0aGVtZS1jb2xvciIgY29udGVudD0iIzFjMWMxOCI+Cjx0aXRsZT5SJkogU3RhdHM8L3RpdGxlPgo8bGluayBocmVmPSJodHRwczovL2ZvbnRzLmdvb2dsZWFwaXMuY29tL2NzczI/ZmFtaWx5PUNvcm1vcmFudCtHYXJhbW9uZDppdGFsLHdnaHRAMCw2MDA7MSw0MDAmZmFtaWx5PU1vbnRzZXJyYXQ6d2dodEAzMDA7NDAwOzUwMDs2MDAmZGlzcGxheT1zd2FwIiByZWw9InN0eWxlc2hlZXQiPgo8c3R5bGU+Cip7Ym94LXNpemluZzpib3JkZXItYm94O21hcmdpbjowO3BhZGRpbmc6MH0KaHRtbCxib2R5e21pbi1oZWlnaHQ6MTAwdmg7YmFja2dyb3VuZDojMWMxYzE4O2NvbG9yOiNjOGMyYjg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC13ZWlnaHQ6MzAwfQouY29ue3dpZHRoOjEwMCU7bWF4LXdpZHRoOjUwMHB4O3BhZGRpbmc6MCAyMnB4O21hcmdpbjowIGF1dG99Ci5wYWdle3BhZGRpbmc6MzJweCAwIDYwcHh9Ci5sb2dvLXJqe2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmO2ZvbnQtc2l6ZToxLjhyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNlOGUwZDB9Ci5iYWNrLWxpbmt7ZGlzcGxheTppbmxpbmUtYmxvY2s7Zm9udC1zaXplOjAuNzRyZW07Y29sb3I6I2M5YTA1YTt0ZXh0LWRlY29yYXRpb246bm9uZTttYXJnaW4tYm90dG9tOjE0cHh9Ci5sb2dvLXN1Yntmb250LXNpemU6LjQ0cmVtO2xldHRlci1zcGFjaW5nOi40ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiM4YThhNTU7bWFyZ2luLXRvcDoycHg7cGFkZGluZy1ib3R0b206MTZweDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjIpO21hcmdpbi1ib3R0b206MjBweH0KLnRhYnMtbWFpbntkaXNwbGF5OmZsZXg7Z2FwOjA7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4yKTttYXJnaW4tYm90dG9tOjB9Ci50bWJ7cGFkZGluZzoxMnB4IDE4cHg7Zm9udC1zaXplOi41NnJlbTtsZXR0ZXItc3BhY2luZzouMThlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6IzU1NTU1MDtjdXJzb3I6cG9pbnRlcjtib3JkZXItYm90dG9tOjJweCBzb2xpZCB0cmFuc3BhcmVudDtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyLXRvcDpub25lO2JvcmRlci1sZWZ0Om5vbmU7Ym9yZGVyLXJpZ2h0Om5vbmU7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7dHJhbnNpdGlvbjphbGwgLjJzfQoudG1iLmFjdGl2ZXtjb2xvcjojYzlhODRjO2JvcmRlci1ib3R0b20tY29sb3I6I2M5YTg0Y30KLnRtYjpob3Zlcntjb2xvcjojYzhjMmI4fQoucGFuZWx7ZGlzcGxheTpub25lO3BhZGRpbmc6MjBweCAwfQoucGFuZWwuYWN0aXZle2Rpc3BsYXk6YmxvY2t9Ci5zbGJse2ZvbnQtc2l6ZTouNTJyZW07bGV0dGVyLXNwYWNpbmc6LjJlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2M5YTg0YzttYXJnaW4tYm90dG9tOjEwcHg7Zm9udC13ZWlnaHQ6NTAwfQoubWV0cmljc3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCgzLDFmcik7Z2FwOjZweDttYXJnaW4tYm90dG9tOjIycHh9Ci5tZXRyaWN7YmFja2dyb3VuZDojMTQxNDEwO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDcpO3BhZGRpbmc6MTJweCAxNHB4fQoubWV0cmljLWxhYmVse2ZvbnQtc2l6ZTouNTRyZW07bGV0dGVyLXNwYWNpbmc6LjFlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6IzU1NTU1MDttYXJnaW4tYm90dG9tOjVweH0KLm1ldHJpYy12YWx7Zm9udC1mYW1pbHk6J0Nvcm1vcmFudCBHYXJhbW9uZCcsc2VyaWY7Zm9udC1zaXplOjEuMzVyZW07Y29sb3I6I2M5YTg0Yztmb250LXdlaWdodDo2MDB9Ci5tZXRyaWMtc3Vie2ZvbnQtc2l6ZTouNThyZW07Y29sb3I6IzQ0NDQ0MDttYXJnaW4tdG9wOjJweH0KLmRpc2NvdW50LXJvd3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxMHB4O2JhY2tncm91bmQ6IzE0MTQxMDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjAxLDE2OCw3NiwuMik7cGFkZGluZzoxMnB4IDE0cHg7bWFyZ2luLWJvdHRvbToxOHB4fQouZGlzY291bnQtbGFiZWx7Zm9udC1zaXplOi41OHJlbTtsZXR0ZXItc3BhY2luZzouMWVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojOGE4YTU1O2ZsZXg6MX0KLmRpc2NvdW50LWlucHV0e2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOm5vbmU7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC40KTtjb2xvcjojYzlhODRjO2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmO2ZvbnQtc2l6ZToxLjJyZW07Zm9udC13ZWlnaHQ6NjAwO3dpZHRoOjgwcHg7dGV4dC1hbGlnbjpyaWdodDtvdXRsaW5lOm5vbmU7cGFkZGluZzoycHggNHB4fQouZGlzY291bnQtaW5wdXQ6Zm9jdXN7Ym9yZGVyLWJvdHRvbS1jb2xvcjojYzlhODRjfQouZGlzY291bnQtZXVye2NvbG9yOiM4YThhNTU7Zm9udC1zaXplOi43NXJlbTttYXJnaW4tbGVmdDoycHh9Ci5wZXJpb2Qtcm93e2Rpc3BsYXk6ZmxleDtnYXA6OHB4O21hcmdpbi1ib3R0b206MThweDthbGlnbi1pdGVtczpjZW50ZXJ9Ci5wZXJpb2Qtc2VsZWN0e2JhY2tncm91bmQ6IzE0MTQxMDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEpO2NvbG9yOiNjOGMyYjg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC1zaXplOi43MnJlbTtwYWRkaW5nOjhweCAxMnB4O291dGxpbmU6bm9uZTtmbGV4OjF9Ci5wZXJpb2Qtc2VsZWN0OmZvY3Vze2JvcmRlci1jb2xvcjojYzlhODRjfQoucmVmcmVzaC1idG57YmFja2dyb3VuZDpyZ2JhKDIwMSwxNjgsNzYsLjEyKTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjAxLDE2OCw3NiwuMyk7Y29sb3I6I2M5YTg0YztwYWRkaW5nOjhweCAxNHB4O2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZTouNThyZW07bGV0dGVyLXNwYWNpbmc6LjE0ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO3doaXRlLXNwYWNlOm5vd3JhcH0KLnJlZnJlc2gtYnRuOmhvdmVye2JhY2tncm91bmQ6cmdiYSgyMDEsMTY4LDc2LC4yMil9Ci5tYXN0ZXItY2FyZHtiYWNrZ3JvdW5kOiMxNDE0MTA7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNyk7Ym9yZGVyLWxlZnQ6M3B4IHNvbGlkIHJnYmEoMjAxLDE2OCw3NiwuMyk7bWFyZ2luLWJvdHRvbTo2cHg7b3ZlcmZsb3c6aGlkZGVufQoubWFzdGVyLWhlYWR7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2FsaWduLWl0ZW1zOmNlbnRlcjtwYWRkaW5nOjEzcHggMTVweDtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmJhY2tncm91bmQgLjJzfQoubWFzdGVyLWhlYWQ6aG92ZXJ7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wMil9Ci5tYXN0ZXItaGVhZC5vcGVue2JvcmRlci1sZWZ0LWNvbG9yOiNjOWE4NGN9Ci5tbmFtZXtmb250LXNpemU6Ljg4cmVtO2ZvbnQtd2VpZ2h0OjUwMDtjb2xvcjojZThlMGQwfQoubWNvdW50e2ZvbnQtc2l6ZTouNnJlbTtjb2xvcjojNTU1NTUwO21hcmdpbi10b3A6MnB4fQoubWVhcm5pbmdze2Rpc3BsYXk6ZmxleDtnYXA6MTRweDthbGlnbi1pdGVtczpjZW50ZXJ9Ci5lYXJuLWl0ZW17dGV4dC1hbGlnbjpyaWdodH0KLmVhcm4tbGFiZWx7Zm9udC1zaXplOi41cmVtO2NvbG9yOiM1NTU1NTA7bGV0dGVyLXNwYWNpbmc6LjFlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2V9Ci5lYXJuLXZhbHtmb250LXNpemU6Ljg4cmVtO2NvbG9yOiNjOWE4NGM7Zm9udC13ZWlnaHQ6NTAwfQouZWFybi12YWwuc2Fsb257Y29sb3I6IzhhOGE1NX0KLmNoZXZyb257Y29sb3I6IzhhOGE1NTtmb250LXNpemU6Ljc1cmVtO3RyYW5zaXRpb246dHJhbnNmb3JtIC4yNXM7bWFyZ2luLWxlZnQ6OHB4fQoubWFzdGVyLWhlYWQub3BlbiAuY2hldnJvbnt0cmFuc2Zvcm06cm90YXRlKDE4MGRlZyl9Ci5tYXN0ZXItYm9keXtkaXNwbGF5Om5vbmU7Ym9yZGVyLXRvcDoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDYpfQoubWFzdGVyLWJvZHkub3BlbntkaXNwbGF5OmJsb2NrfQoudmlzaXRzLWhlYWRlcntkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjcycHggMWZyIDg1cHggNjBweDtnYXA6NnB4O3BhZGRpbmc6OHB4IDE1cHg7Zm9udC1zaXplOi41cmVtO2NvbG9yOiM0NDQ0NDA7bGV0dGVyLXNwYWNpbmc6LjFlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wMil9Ci52aXNpdC1yb3d7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczo3MnB4IDFmciA4NXB4IDYwcHg7Z2FwOjZweDtwYWRkaW5nOjlweCAxNXB4O2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA0KTtmb250LXNpemU6LjcycmVtO2FsaWduLWl0ZW1zOnN0YXJ0fQoudmlzaXQtcm93Omxhc3QtY2hpbGR7Ym9yZGVyLWJvdHRvbTpub25lfQoudmlzaXQtZGF0ZXtjb2xvcjojNTU1NTUwfQoudmlzaXQtY2xpZW50e2NvbG9yOiNjOGMyYjg7Zm9udC1zaXplOi43NXJlbX0KLnZpc2l0LXBldHtjb2xvcjojNTU1NTUwO2ZvbnQtc2l6ZTouNjJyZW07bWFyZ2luLXRvcDoxcHh9Ci52aXNpdC1zdmN7Y29sb3I6IzY2NjY2MDtmb250LXNpemU6LjY1cmVtfQoudmlzaXQtcHJpY2V7Y29sb3I6I2M5YTg0Yzt0ZXh0LWFsaWduOnJpZ2h0O2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmO2ZvbnQtc2l6ZTouOTVyZW07Zm9udC13ZWlnaHQ6NjAwfQoubm8tdmlzaXRze3BhZGRpbmc6MTZweCAxNXB4O2ZvbnQtc2l6ZTouNzJyZW07Y29sb3I6IzQ0NDQ0MDtmb250LXN0eWxlOml0YWxpY30KLnN1Yi10YWJze2Rpc3BsYXk6ZmxleDtnYXA6NXB4O21hcmdpbi1ib3R0b206MThweH0KLnN0YntwYWRkaW5nOjdweCAxNHB4O2ZvbnQtc2l6ZTouNTRyZW07bGV0dGVyLXNwYWNpbmc6LjE0ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiM1NTU1NTA7Y3Vyc29yOnBvaW50ZXI7YmFja2dyb3VuZDojMTQxNDEwO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDcpO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO3RyYW5zaXRpb246YWxsIC4yc30KLnN0Yi5hY3RpdmV7Y29sb3I6I2M5YTg0Yztib3JkZXItY29sb3I6cmdiYSgyMDEsMTY4LDc2LC40KTtiYWNrZ3JvdW5kOnJnYmEoMjAxLDE2OCw3NiwuMDYpfQouc3RiOmhvdmVye2NvbG9yOiNjOGMyYjh9Ci5zdWItcGFuZWx7ZGlzcGxheTpub25lfQouc3ViLXBhbmVsLmFjdGl2ZXtkaXNwbGF5OmJsb2NrfQouZml7d2lkdGg6MTAwJTtiYWNrZ3JvdW5kOiMxNDE0MTA7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xKTtjb2xvcjojYzhjMmI4O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZTouODJyZW07cGFkZGluZzoxMXB4IDE0cHg7b3V0bGluZTpub25lO21hcmdpbi1ib3R0b206OHB4fQouZmk6Zm9jdXN7Ym9yZGVyLWNvbG9yOiNjOWE4NGN9CnNlbGVjdC5maXthcHBlYXJhbmNlOm5vbmU7LXdlYmtpdC1hcHBlYXJhbmNlOm5vbmV9Ci5maS1yb3d7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyO2dhcDo4cHh9Ci5jYnRue2Rpc3BsYXk6YmxvY2s7d2lkdGg6MTAwJTtiYWNrZ3JvdW5kOiM0YTRhMmU7Y29sb3I6I2M4YzJiODtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXNpemU6LjZyZW07Zm9udC13ZWlnaHQ6NjAwO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtwYWRkaW5nOjEzcHg7Ym9yZGVyOm5vbmU7Y3Vyc29yOnBvaW50ZXI7bWFyZ2luLXRvcDo0cHg7dHJhbnNpdGlvbjpiYWNrZ3JvdW5kIC4yc30KLmNidG46aG92ZXJ7YmFja2dyb3VuZDojNmI2YjQyfQouY2J0bi5naG9zdHtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjMpO2NvbG9yOiNjOWE4NGN9Ci5jYnRuLmdob3N0OmhvdmVye2JhY2tncm91bmQ6cmdiYSgyMDEsMTY4LDc2LC4wOCl9Ci5saXN0LWl0ZW17ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2FsaWduLWl0ZW1zOmNlbnRlcjtwYWRkaW5nOjExcHggMTRweDtiYWNrZ3JvdW5kOiMxNDE0MTA7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNyk7bWFyZ2luLWJvdHRvbTo1cHh9Ci5saS1uYW1le2ZvbnQtc2l6ZTouODJyZW07Y29sb3I6I2M4YzJiOH0KLmxpLXN1Yntmb250LXNpemU6LjZyZW07Y29sb3I6IzU1NTU1MDttYXJnaW4tdG9wOjJweH0KLmRlbC1idG57YmFja2dyb3VuZDpub25lO2JvcmRlcjpub25lO2NvbG9yOiM0NDQ0NDA7Y3Vyc29yOnBvaW50ZXI7Zm9udC1zaXplOi44cmVtO3BhZGRpbmc6NHB4IDhweDt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLmRlbC1idG46aG92ZXJ7Y29sb3I6I2MwNTA1MH0KLmJyZWVkLWNhcmR7YmFja2dyb3VuZDojMTQxNDEwO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDcpO2JvcmRlci1sZWZ0OjNweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjIpO21hcmdpbi1ib3R0b206NnB4fQouYnJlZWQtaGVhZHtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6Y2VudGVyO3BhZGRpbmc6MTFweCAxNHB4O2N1cnNvcjpwb2ludGVyfQouYnJlZWQtaGVhZDpob3ZlcntiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjAyKX0KLmJyZWVkLW5hbWV7Zm9udC1zaXplOi44MnJlbTtjb2xvcjojZThlMGQwfQouYnJlZWQtY291bnR7Zm9udC1zaXplOi42cmVtO2NvbG9yOiM1NTU1NTB9Ci5icmVlZC1ib2R5e2Rpc3BsYXk6bm9uZTtib3JkZXItdG9wOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNik7cGFkZGluZzoxMHB4IDE0cHh9Ci5icmVlZC1ib2R5Lm9wZW57ZGlzcGxheTpibG9ja30KLnN2Yy1yb3d7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2FsaWduLWl0ZW1zOmNlbnRlcjtwYWRkaW5nOjZweCAwO2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA0KTtmb250LXNpemU6Ljc1cmVtfQouc3ZjLXJvdzpsYXN0LWNoaWxke2JvcmRlci1ib3R0b206bm9uZX0KLnN2Yy1uYW1le2NvbG9yOiNjOGMyYjh9Ci5zdmMtcHJpY2V7Y29sb3I6I2M5YTg0Yztmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZjtmb250LXNpemU6Ljk1cmVtO2ZvbnQtd2VpZ2h0OjYwMH0KLmFkZC1zdmMtcm93e2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDcwcHggMzRweDtnYXA6NnB4O21hcmdpbi10b3A6MTBweH0KLmFkZC1zdmMtcm93IC5maXttYXJnaW4tYm90dG9tOjA7Zm9udC1zaXplOi43NXJlbTtwYWRkaW5nOjhweCAxMHB4fQouaWNvbi1idG57d2lkdGg6MzRweDtoZWlnaHQ6MzRweDtiYWNrZ3JvdW5kOnJnYmEoMjAxLDE2OCw3NiwuMTIpO2JvcmRlcjoxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4zKTtjb2xvcjojYzlhODRjO2N1cnNvcjpwb2ludGVyO2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OmNlbnRlcjtmb250LXNpemU6MS4xcmVtO2ZvbnQtd2VpZ2h0OjMwMH0KLmljb24tYnRuOmhvdmVye2JhY2tncm91bmQ6cmdiYSgyMDEsMTY4LDc2LC4yNCl9Ci5jbGllbnQtY2FyZHtiYWNrZ3JvdW5kOiMxNDE0MTA7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNyk7cGFkZGluZzoxNHB4O21hcmdpbi1ib3R0b206NnB4fQouY2wtcm93e2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpmbGV4LXN0YXJ0fQouY2wtYXZhdGFye3dpZHRoOjM2cHg7aGVpZ2h0OjM2cHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDpyZ2JhKDIwMSwxNjgsNzYsLjEyKTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjAxLDE2OCw3NiwuMjUpO2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OmNlbnRlcjtmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZjtmb250LXNpemU6Ljg4cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojYzlhODRjO2ZsZXgtc2hyaW5rOjA7bWFyZ2luLXJpZ2h0OjEwcHh9Ci5jbC1uYW1le2ZvbnQtc2l6ZTouODVyZW07Zm9udC13ZWlnaHQ6NTAwO2NvbG9yOiNlOGUwZDB9Ci5jbC1kZXRhaWx7Zm9udC1zaXplOi42MnJlbTtjb2xvcjojNTU1NTUwO21hcmdpbi10b3A6MnB4fQouY2wtc3RhdHN7ZGlzcGxheTpmbGV4O2dhcDoxNHB4O21hcmdpbi10b3A6MTBweDtwYWRkaW5nLXRvcDoxMHB4O2JvcmRlci10b3A6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA1KX0KLmNzdC12YWx7Zm9udC1mYW1pbHk6J0Nvcm1vcmFudCBHYXJhbW9uZCcsc2VyaWY7Zm9udC1zaXplOjEuMXJlbTtjb2xvcjojYzlhODRjO2ZvbnQtd2VpZ2h0OjYwMH0KLmNzdC1sYWJlbHtmb250LXNpemU6LjUycmVtO2NvbG9yOiM1NTU1NTA7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi4xZW19Ci5jbC1sYXN0e2ZvbnQtc2l6ZTouNnJlbTtjb2xvcjojNTU1NTUwO21hcmdpbi10b3A6OHB4fQouY2wtbGFzdCBzcGFue2NvbG9yOiM4YThhNTV9Ci5jbC1iYWRnZXtmb250LXNpemU6LjU4cmVtO2NvbG9yOiNjOWE4NGM7YmFja2dyb3VuZDpyZ2JhKDIwMSwxNjgsNzYsLjEpO2JvcmRlcjoxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4yKTtwYWRkaW5nOjNweCA4cHg7d2hpdGUtc3BhY2U6bm93cmFwfQoubG9hZGluZ3t0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjQwcHggMDtjb2xvcjojNDQ0NDQwO2ZvbnQtc2l6ZTouNzVyZW07bGV0dGVyLXNwYWNpbmc6LjEyZW19Ci5zZWN0e21hcmdpbi1ib3R0b206MjJweH0KLmZvcm0tYmxvY2t7YmFja2dyb3VuZDojMTQxNDEwO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDcpO3BhZGRpbmc6MTZweDttYXJnaW4tYm90dG9tOjE2cHh9Ci50YWd7Zm9udC1zaXplOi41OHJlbTtjb2xvcjojOGE4YTU1O2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDUpO3BhZGRpbmc6MnB4IDhweDttYXJnaW4tcmlnaHQ6NHB4O21hcmdpbi1ib3R0b206NHB4O2Rpc3BsYXk6aW5saW5lLWJsb2NrfQpAa2V5ZnJhbWVzIGZ1e2Zyb217b3BhY2l0eTowO3RyYW5zZm9ybTp0cmFuc2xhdGVZKDhweCl9dG97b3BhY2l0eToxO3RyYW5zZm9ybTp0cmFuc2xhdGVZKDApfX0KLmFuaW17YW5pbWF0aW9uOmZ1IC4zcyBlYXNlIGJvdGh9Cjwvc3R5bGU+CjwvaGVhZD4KPGJvZHk+CjxkaXYgY2xhc3M9ImNvbiI+CjxkaXYgY2xhc3M9InBhZ2UiPgoKPGEgaHJlZj0iL2FkbWluP3Bhc3M9YW56YTE5ODUiIGNsYXNzPSJiYWNrLWxpbmsiPuKGkCDQkNC00LzQuNC9LdC/0LDQvdC10LvRjDwvYT4KPGRpdiBjbGFzcz0ibG9nby1yaiI+UiZhbXA7SjwvZGl2Pgo8ZGl2IGNsYXNzPSJsb2dvLXN1YiI+R3Jvb21pbmcgJm1pZGRvdDsg0KHRgtCw0YLQuNGB0YLQuNC60LAgJm1pZGRvdDsg0KLQsNC70LvQuNC9PC9kaXY+Cgo8ZGl2IGNsYXNzPSJ0YWJzLW1haW4iPgogIDxidXR0b24gY2xhc3M9InRtYiBhY3RpdmUiIG9uY2xpY2s9InN3aXRjaE1haW4oJ3N0YXRzJyx0aGlzKSI+0KHRgtCw0YLQuNGB0YLQuNC60LA8L2J1dHRvbj4KICA8YnV0dG9uIGNsYXNzPSJ0bWIiIG9uY2xpY2s9InN3aXRjaE1haW4oJ21nbXQnLHRoaXMpIj7Qo9C/0YDQsNCy0LvQtdC90LjQtTwvYnV0dG9uPgo8L2Rpdj4KCjwhLS0g4pWQ4pWQ4pWQINCh0KLQkNCi0JjQodCi0JjQmtCQIOKVkOKVkOKVkCAtLT4KPGRpdiBjbGFzcz0icGFuZWwgYWN0aXZlIiBpZD0icGFuZWwtc3RhdHMiPgoKICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7Z2FwOjhweDttYXJnaW4tYm90dG9tOjEycHg7bWFyZ2luLXRvcDoyMHB4O2FsaWduLWl0ZW1zOmNlbnRlciI+CiAgICA8c2VsZWN0IGNsYXNzPSJwZXJpb2Qtc2VsZWN0IiBpZD0icGVyaW9kU2VsZWN0IiBvbmNoYW5nZT0ibG9hZFN0YXRzKCkiPgogICAgICA8b3B0aW9uIHZhbHVlPSJtb250aCI+0K3RgtC+0YIg0LzQtdGB0Y/Rhjwvb3B0aW9uPgogICAgICA8b3B0aW9uIHZhbHVlPSJsYXN0X21vbnRoIj7Qn9GA0L7RiNC70YvQuSDQvNC10YHRj9GGPC9vcHRpb24+CiAgICAgIDxvcHRpb24gdmFsdWU9IjNtb250aHMiPjMg0LzQtdGB0Y/RhtCwPC9vcHRpb24+CiAgICAgIDxvcHRpb24gdmFsdWU9ImFsbCI+0JLRgdGRINCy0YDQtdC80Y88L29wdGlvbj4KICAgIDwvc2VsZWN0PgogICAgPGJ1dHRvbiBjbGFzcz0icmVmcmVzaC1idG4iIG9uY2xpY2s9ImxvYWRTdGF0cygpIj4mIzg2MzU7INCe0LHQvdC+0LLQuNGC0Yw8L2J1dHRvbj4KICA8L2Rpdj4KCiAgPGRpdiBjbGFzcz0iZGlzY291bnQtcm93Ij4KICAgIDxkaXYgY2xhc3M9ImRpc2NvdW50LWxhYmVsIj7QodC60LjQtNC60LAg0YHQsNC70L7QvdCwICjQstGL0YfQuNGC0LDQtdGC0YHRjyDRgtC+0LvRjNC60L4g0LjQtyDQtNC+0LvQuCDRgdCw0LvQvtC90LApPC9kaXY+CiAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo0cHgiPgogICAgICA8aW5wdXQgY2xhc3M9ImRpc2NvdW50LWlucHV0IiBpZD0iZGlzY291bnRJbnB1dCIgdHlwZT0ibnVtYmVyIiBtaW49IjAiIHZhbHVlPSIwIiBvbmlucHV0PSJyZWNhbGMoKSI+CiAgICAgIDxzcGFuIGNsYXNzPSJkaXNjb3VudC1ldXIiPuKCrDwvc3Bhbj4KICAgIDwvZGl2PgogIDwvZGl2PgoKICA8ZGl2IGNsYXNzPSJtZXRyaWNzIiBpZD0ibWV0cmljc0Jsb2NrIj4KICAgIDxkaXYgY2xhc3M9Im1ldHJpYyI+PGRpdiBjbGFzcz0ibWV0cmljLWxhYmVsIj7QktGL0YDRg9GH0LrQsDwvZGl2PjxkaXYgY2xhc3M9Im1ldHJpYy12YWwiIGlkPSJtVG90YWwiPuKAlDwvZGl2PjxkaXYgY2xhc3M9Im1ldHJpYy1zdWIiPtGB0YPQvNC80LAg0YPRgdC70YPQszwvZGl2PjwvZGl2PgogICAgPGRpdiBjbGFzcz0ibWV0cmljIj48ZGl2IGNsYXNzPSJtZXRyaWMtbGFiZWwiPtCc0LDRgdGC0LXRgNCw0Lw8L2Rpdj48ZGl2IGNsYXNzPSJtZXRyaWMtdmFsIiBpZD0ibU1hc3RlcnMiPuKAlDwvZGl2PjxkaXYgY2xhc3M9Im1ldHJpYy1zdWIiPtC/0L4g0YHRgtCw0LLQutC1INC80LDRgdGC0LXRgNCwPC9kaXY+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJtZXRyaWMiPjxkaXYgY2xhc3M9Im1ldHJpYy1sYWJlbCI+0KHQsNC70L7QvdGDPC9kaXY+PGRpdiBjbGFzcz0ibWV0cmljLXZhbCIgaWQ9Im1TYWxvbiI+4oCUPC9kaXY+PGRpdiBjbGFzcz0ibWV0cmljLXN1YiI+0L7RgdGC0LDRgtC+0Log4oiSINGB0LrQuNC00LrQsDwvZGl2PjwvZGl2PgogIDwvZGl2PgogIDxkaXYgY2xhc3M9Im1ldHJpY3MiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjAiPgogICAgPGRpdiBjbGFzcz0ibWV0cmljIj48ZGl2IGNsYXNzPSJtZXRyaWMtbGFiZWwiPtCX0LDQv9C40YHQtdC5PC9kaXY+PGRpdiBjbGFzcz0ibWV0cmljLXZhbCIgaWQ9Im1Db3VudCI+4oCUPC9kaXY+PGRpdiBjbGFzcz0ibWV0cmljLXN1YiI+0LfQsCDQv9C10YDQuNC+0LQ8L2Rpdj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im1ldHJpYyI+PGRpdiBjbGFzcz0ibWV0cmljLWxhYmVsIj7QmtC70LjQtdC90YLQvtCyPC9kaXY+PGRpdiBjbGFzcz0ibWV0cmljLXZhbCIgaWQ9Im1DbGllbnRzIj7igJQ8L2Rpdj48ZGl2IGNsYXNzPSJtZXRyaWMtc3ViIj7Rg9C90LjQutCw0LvRjNC90YvRhTwvZGl2PjwvZGl2PgogICAgPGRpdiBjbGFzcz0ibWV0cmljIj48ZGl2IGNsYXNzPSJtZXRyaWMtbGFiZWwiPtCh0YAuINGH0LXQujwvZGl2PjxkaXYgY2xhc3M9Im1ldHJpYy12YWwiIGlkPSJtQXZnIj7igJQ8L2Rpdj48ZGl2IGNsYXNzPSJtZXRyaWMtc3ViIj7Qt9CwINGD0YHQu9GD0LPRgzwvZGl2PjwvZGl2PgogIDwvZGl2PgoKICA8ZGl2IHN0eWxlPSJtYXJnaW4tdG9wOjIycHgiPgogICAgPGRpdiBjbGFzcz0ic2xibCI+0J/QviDQvNCw0YHRgtC10YDQsNC8PC9kaXY+CiAgICA8ZGl2IGlkPSJtYXN0ZXJzTGlzdCI+PGRpdiBjbGFzcz0ibG9hZGluZyI+0JfQsNCz0YDRg9C30LrQsCDQtNCw0L3QvdGL0YUuLi48L2Rpdj48L2Rpdj4KICA8L2Rpdj4KPC9kaXY+Cgo8IS0tIOKVkOKVkOKVkCDQo9Cf0KDQkNCS0JvQldCd0JjQlSDilZDilZDilZAgLS0+CjxkaXYgY2xhc3M9InBhbmVsIiBpZD0icGFuZWwtbWdtdCI+CiAgPGRpdiBzdHlsZT0ibWFyZ2luLXRvcDoyMHB4IiBjbGFzcz0ic3ViLXRhYnMiPgogICAgPGJ1dHRvbiBjbGFzcz0ic3RiIGFjdGl2ZSIgb25jbGljaz0ic3dpdGNoU3ViKCdtYXN0ZXJzJyx0aGlzKSI+0JzQsNGB0YLQtdGA0LA8L2J1dHRvbj4KICAgIDxidXR0b24gY2xhc3M9InN0YiIgb25jbGljaz0ic3dpdGNoU3ViKCdicmVlZHMnLHRoaXMpIj7Qn9C+0YDQvtC00Ys8L2J1dHRvbj4KICAgIDxidXR0b24gY2xhc3M9InN0YiIgb25jbGljaz0ic3dpdGNoU3ViKCdjbGllbnRzJyx0aGlzKSI+0JrQu9C40LXQvdGC0Ys8L2J1dHRvbj4KICA8L2Rpdj4KCiAgPCEtLSDQnNCw0YHRgtC10YDQsCAtLT4KICA8ZGl2IGNsYXNzPSJzdWItcGFuZWwgYWN0aXZlIiBpZD0ic3ViLW1hc3RlcnMiPgogICAgPGRpdiBjbGFzcz0ic2VjdCI+CiAgICAgIDxkaXYgY2xhc3M9InNsYmwiPtCU0L7QsdCw0LLQuNGC0Ywg0LzQsNGB0YLQtdGA0LA8L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0iZm9ybS1ibG9jayI+CiAgICAgICAgPGRpdiBjbGFzcz0iZmktcm93Ij4KICAgICAgICAgIDxpbnB1dCBjbGFzcz0iZmkiIGlkPSJuZXdNYXN0ZXJOYW1lIiBwbGFjZWhvbGRlcj0i0JjQvNGPINC80LDRgdGC0LXRgNCwIj4KICAgICAgICAgIDxpbnB1dCBjbGFzcz0iZmkiIGlkPSJuZXdNYXN0ZXJQaG9uZSIgcGxhY2Vob2xkZXI9ItCi0LXQu9C10YTQvtC9ICjQvdC10L7QsdGP0LcuKSI+CiAgICAgICAgPC9kaXY+CiAgICAgICAgPGJ1dHRvbiBjbGFzcz0iY2J0biIgb25jbGljaz0iYWRkTWFzdGVyKCkiPisg0JTQvtCx0LDQstC40YLRjDwvYnV0dG9uPgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0ic2VjdCI+CiAgICAgIDxkaXYgY2xhc3M9InNsYmwiPtCc0LDRgdGC0LXRgNCwINGB0LDQu9C+0L3QsDwvZGl2PgogICAgICA8ZGl2IGlkPSJtYXN0ZXJMaXN0VUkiPjwvZGl2PgogICAgPC9kaXY+CiAgPC9kaXY+CgogIDwhLS0g0J/QvtGA0L7QtNGLINC4INGD0YHQu9GD0LPQuCAtLT4KICA8ZGl2IGNsYXNzPSJzdWItcGFuZWwiIGlkPSJzdWItYnJlZWRzIj4KICAgIDxkaXYgY2xhc3M9InNlY3QiPgogICAgICA8ZGl2IGNsYXNzPSJzbGJsIj7QlNC+0LHQsNCy0LjRgtGMINC/0L7RgNC+0LTRgzwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJmb3JtLWJsb2NrIj4KICAgICAgICA8aW5wdXQgY2xhc3M9ImZpIiBpZD0ibmV3QnJlZWROYW1lIiBwbGFjZWhvbGRlcj0i0J3QsNC30LLQsNC90LjQtSDQv9C+0YDQvtC00YsgKNC90LDQv9GALiDQpdCw0YHQutC4IDIw4oCTMzAg0LrQsykiPgogICAgICAgIDxidXR0b24gY2xhc3M9ImNidG4iIG9uY2xpY2s9ImFkZEJyZWVkKCkiPisg0JTQvtCx0LDQstC40YLRjCDQv9C+0YDQvtC00YM8L2J1dHRvbj4KICAgICAgPC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InNlY3QiPgogICAgICA8ZGl2IGNsYXNzPSJzbGJsIj7Qn9C+0YDQvtC00Ysg0Lgg0YPRgdC70YPQs9C4PC9kaXY+CiAgICAgIDxkaXYgaWQ9ImJyZWVkTGlzdFVJIj48L2Rpdj4KICAgIDwvZGl2PgogIDwvZGl2PgoKICA8IS0tINCa0LvQuNC10L3RgtGLIC0tPgogIDxkaXYgY2xhc3M9InN1Yi1wYW5lbCIgaWQ9InN1Yi1jbGllbnRzIj4KICAgIDxkaXYgY2xhc3M9InNlY3QiPgogICAgICA8ZGl2IGNsYXNzPSJzbGJsIj7QndC+0LLQsNGPINC60LDRgNGC0L7Rh9C60LAg0LrQu9C40LXQvdGC0LA8L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0iZm9ybS1ibG9jayI+CiAgICAgICAgPGRpdiBjbGFzcz0iZmktcm93Ij4KICAgICAgICAgIDxpbnB1dCBjbGFzcz0iZmkiIGlkPSJuZXdDbGllbnROYW1lIiBwbGFjZWhvbGRlcj0i0JjQvNGPINC60LvQuNC10L3RgtCwIj4KICAgICAgICAgIDxpbnB1dCBjbGFzcz0iZmkiIGlkPSJuZXdDbGllbnRQaG9uZSIgcGxhY2Vob2xkZXI9IiszNzIgLi4uIj4KICAgICAgICA8L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJmaS1yb3ciPgogICAgICAgICAgPGlucHV0IGNsYXNzPSJmaSIgaWQ9Im5ld0NsaWVudFBldCIgcGxhY2Vob2xkZXI9ItCa0LvQuNGH0LrQsCDQv9C40YLQvtC80YbQsCI+CiAgICAgICAgICA8c2VsZWN0IGNsYXNzPSJmaSIgaWQ9Im5ld0NsaWVudEJyZWVkIj48b3B0aW9uIHZhbHVlPSIiPtCf0L7RgNC+0LTQsC4uLjwvb3B0aW9uPjwvc2VsZWN0PgogICAgICAgIDwvZGl2PgogICAgICAgIDxpbnB1dCBjbGFzcz0iZmkiIGlkPSJuZXdDbGllbnROb3RlIiBwbGFjZWhvbGRlcj0i0JrQvtC80LzQtdC90YLQsNGA0LjQuSAo0LDQu9C70LXRgNCz0LjQuCwg0L7RgdC+0LHQtdC90L3QvtGB0YLQuC4uLikiPgogICAgICAgIDxidXR0b24gY2xhc3M9ImNidG4iIG9uY2xpY2s9ImFkZENsaWVudCgpIj4rINCh0L7Qt9C00LDRgtGMINC60LDRgNGC0L7Rh9C60YM8L2J1dHRvbj4KICAgICAgPC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InNlY3QiPgogICAgICA8ZGl2IGNsYXNzPSJzbGJsIj7QkdCw0LfQsCDQutC70LjQtdC90YLQvtCyPC9kaXY+CiAgICAgIDxkaXYgaWQ9ImNsaWVudExpc3RVSSI+PC9kaXY+CiAgICA8L2Rpdj4KICA8L2Rpdj4KPC9kaXY+Cgo8L2Rpdj4KPC9kaXY+Cgo8c2NyaXB0Pgp2YXIgR09PR0xFX1NDUklQVCA9IHdpbmRvdy5sb2NhdGlvbi5vcmlnaW4gKyAnL2FwaS9zdGF0cyc7CnZhciBkYiA9IHsKICBtYXN0ZXJzOiBKU09OLnBhcnNlKGxvY2FsU3RvcmFnZS5nZXRJdGVtKCdyal9tYXN0ZXJzJykgfHwgJ1si0KLQsNGC0YzRj9C90LAiLCLQkNC70LjRgdCwIiwi0JrRgNC40YHRgtC40L3QsCIsItCQ0L3QvdCwIl0nKSwKICBicmVlZHM6ICBKU09OLnBhcnNlKGxvY2FsU3RvcmFnZS5nZXRJdGVtKCdyal9icmVlZHMnKSAgfHwgJ1tdJyksCiAgY2xpZW50czogSlNPTi5wYXJzZShsb2NhbFN0b3JhZ2UuZ2V0SXRlbSgncmpfY2xpZW50cycpIHx8ICdbXScpLAogIGRpc2NvdW50OiBwYXJzZUZsb2F0KGxvY2FsU3RvcmFnZS5nZXRJdGVtKCdyal9kaXNjb3VudCcpIHx8ICcwJykKfTsKdmFyIGFsbEJvb2tpbmdzID0gW107CnZhciBzdGF0c0xvYWRlZCA9IGZhbHNlOwoKZnVuY3Rpb24gc2F2ZSgpIHsKICBsb2NhbFN0b3JhZ2Uuc2V0SXRlbSgncmpfbWFzdGVycycsICBKU09OLnN0cmluZ2lmeShkYi5tYXN0ZXJzKSk7CiAgbG9jYWxTdG9yYWdlLnNldEl0ZW0oJ3JqX2JyZWVkcycsICAgSlNPTi5zdHJpbmdpZnkoZGIuYnJlZWRzKSk7CiAgbG9jYWxTdG9yYWdlLnNldEl0ZW0oJ3JqX2NsaWVudHMnLCAgSlNPTi5zdHJpbmdpZnkoZGIuY2xpZW50cykpOwogIGxvY2FsU3RvcmFnZS5zZXRJdGVtKCdyal9kaXNjb3VudCcsIGRiLmRpc2NvdW50KTsKfQoKLy8g4pSA4pSAINCd0JDQktCY0JPQkNCm0JjQryDilIDilIAKZnVuY3Rpb24gc3dpdGNoTWFpbihuYW1lLCBidG4pIHsKICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcudG1iJykuZm9yRWFjaChmdW5jdGlvbih0KXt0LmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpfSk7CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLnBhbmVsJykuZm9yRWFjaChmdW5jdGlvbihwKXtwLmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpfSk7CiAgYnRuLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdwYW5lbC0nK25hbWUpLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpOwogIGlmIChuYW1lID09PSAnc3RhdHMnICYmICFzdGF0c0xvYWRlZCkgbG9hZFN0YXRzKCk7Cn0KZnVuY3Rpb24gc3dpdGNoU3ViKG5hbWUsIGJ0bikgewogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5zdGInKS5mb3JFYWNoKGZ1bmN0aW9uKHQpe3QuY2xhc3NMaXN0LnJlbW92ZSgnYWN0aXZlJyl9KTsKICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuc3ViLXBhbmVsJykuZm9yRWFjaChmdW5jdGlvbihwKXtwLmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpfSk7CiAgYnRuLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdWItJytuYW1lKS5jbGFzc0xpc3QuYWRkKCdhY3RpdmUnKTsKfQoKLy8g4pSA4pSAINCh0KLQkNCi0JjQodCi0JjQmtCQIOKUgOKUgApmdW5jdGlvbiBnZXRQZXJpb2RQYXJhbXMoKSB7CiAgdmFyIHZhbCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdwZXJpb2RTZWxlY3QnKS52YWx1ZTsKICB2YXIgbm93ID0gbmV3IERhdGUoKTsKICB2YXIgZnJvbSwgdG87CiAgaWYgKHZhbCA9PT0gJ21vbnRoJykgewogICAgZnJvbSA9IG5ldyBEYXRlKG5vdy5nZXRGdWxsWWVhcigpLCBub3cuZ2V0TW9udGgoKSwgMSk7CiAgICB0byAgID0gbmV3IERhdGUobm93LmdldEZ1bGxZZWFyKCksIG5vdy5nZXRNb250aCgpKzEsIDApOwogIH0gZWxzZSBpZiAodmFsID09PSAnbGFzdF9tb250aCcpIHsKICAgIGZyb20gPSBuZXcgRGF0ZShub3cuZ2V0RnVsbFllYXIoKSwgbm93LmdldE1vbnRoKCktMSwgMSk7CiAgICB0byAgID0gbmV3IERhdGUobm93LmdldEZ1bGxZZWFyKCksIG5vdy5nZXRNb250aCgpLCAwKTsKICB9IGVsc2UgaWYgKHZhbCA9PT0gJzNtb250aHMnKSB7CiAgICBmcm9tID0gbmV3IERhdGUobm93LmdldEZ1bGxZZWFyKCksIG5vdy5nZXRNb250aCgpLTIsIDEpOwogICAgdG8gICA9IG5ldyBEYXRlKG5vdy5nZXRGdWxsWWVhcigpLCBub3cuZ2V0TW9udGgoKSsxLCAwKTsKICB9IGVsc2UgewogICAgZnJvbSA9IG5ldyBEYXRlKDIwMjQsIDAsIDEpOwogICAgdG8gICA9IG5ldyBEYXRlKG5vdy5nZXRGdWxsWWVhcigpLCBub3cuZ2V0TW9udGgoKSsxLCAwKTsKICB9CiAgdmFyIGZtdCA9IGZ1bmN0aW9uKGQpIHsgcmV0dXJuIFN0cmluZyhkLmdldERhdGUoKSkucGFkU3RhcnQoMiwnMCcpKycuJytTdHJpbmcoZC5nZXRNb250aCgpKzEpLnBhZFN0YXJ0KDIsJzAnKSsnLicrZC5nZXRGdWxsWWVhcigpOyB9OwogIHJldHVybiB7ZnJvbTogZm10KGZyb20pLCB0bzogZm10KHRvKX07Cn0KCmZ1bmN0aW9uIGxvYWRTdGF0cygpIHsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbWFzdGVyc0xpc3QnKS5pbm5lckhUTUwgPSAnPGRpdiBjbGFzcz0ibG9hZGluZyI+0JfQsNCz0YDRg9C30LrQsCDQtNCw0L3QvdGL0YUg0LjQtyDQutCw0LvQtdC90LTQsNGA0Y8uLi48L2Rpdj4nOwogIHZhciBwID0gZ2V0UGVyaW9kUGFyYW1zKCk7CiAgZmV0Y2god2luZG93LmxvY2F0aW9uLm9yaWdpbiArICcvYXBpL3N0YXRzP2Zyb209JyArIHAuZnJvbSArICcmdG89JyArIHAudG8pCiAgICAudGhlbihmdW5jdGlvbihyKXtyZXR1cm4gci5qc29uKCk7fSkKICAgIC50aGVuKGZ1bmN0aW9uKGRhdGEpewogICAgICBpZiAoZGF0YS5zdWNjZXNzKSB7CiAgICAgICAgYWxsQm9va2luZ3MgPSBkYXRhLmJvb2tpbmdzIHx8IFtdOwogICAgICAgIHN0YXRzTG9hZGVkID0gdHJ1ZTsKICAgICAgICByZWNhbGMoKTsKICAgICAgfSBlbHNlIHsKICAgICAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbWFzdGVyc0xpc3QnKS5pbm5lckhUTUwgPSAnPGRpdiBjbGFzcz0ibG9hZGluZyI+0J7RiNC40LHQutCwOiAnICsgKGRhdGEuZXJyb3J8fCfQvdC10YIg0LTQsNC90L3Ri9GFJykgKyAnPC9kaXY+JzsKICAgICAgfQogICAgfSkKICAgIC5jYXRjaChmdW5jdGlvbihlKXsKICAgICAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ21hc3RlcnNMaXN0JykuaW5uZXJIVE1MID0gJzxkaXYgY2xhc3M9ImxvYWRpbmciPtCd0LXRgiDRgdC+0LXQtNC40L3QtdC90LjRjyDRgSDQutCw0LvQtdC90LTQsNGA0ZHQvDwvZGl2Pic7CiAgICB9KTsKfQoKZnVuY3Rpb24gZ2V0TWFzdGVyUmF0aW8obmFtZSkgewogIHZhciBtYXAgPSB7CiAgICAn0JDQu9C40YHQsCc6IDAuNDAsCiAgICAn0JrRgdC10L3QuNGPJzogMC40NSwKICAgICfQkNC90L3QsCc6IDEuMDAsCiAgICAn0JDQu9C10LrRgdCw0L3QtNGA0LAnOiAwLjUwLAogICAgJ9Ci0LDRgtGM0Y/QvdCwJzogMC41MCwKICAgICfQmtGA0LjRgdGC0LjQvdCwJzogMC41MAogIH07CiAgcmV0dXJuIG1hcC5oYXNPd25Qcm9wZXJ0eShuYW1lKSA/IG1hcFtuYW1lXSA6IDAuNTA7Cn0KCmZ1bmN0aW9uIHJlY2FsYygpIHsKICB2YXIgZGlzY291bnQgPSBwYXJzZUZsb2F0KGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdkaXNjb3VudElucHV0JykudmFsdWUpIHx8IDA7CiAgZGIuZGlzY291bnQgPSBkaXNjb3VudDsKICBzYXZlKCk7CgogIHZhciB0b3RhbCA9IDAsIGNvdW50ID0gMDsKICB2YXIgbWFzdGVyTWFwID0ge307CiAgdmFyIHBob25lcyA9IHt9OwoKICBhbGxCb29raW5ncy5mb3JFYWNoKGZ1bmN0aW9uKGIpIHsKICAgIHZhciBwcmljZSA9IHBhcnNlRmxvYXQoYi5wcmljZSkgfHwgMDsKICAgIHRvdGFsICs9IHByaWNlOwogICAgY291bnQrKzsKICAgIGlmIChiLmNsaWVudFBob25lKSBwaG9uZXNbYi5jbGllbnRQaG9uZV0gPSB0cnVlOwogICAgdmFyIG0gPSBiLm1hc3RlcjsKICAgIGlmICghbWFzdGVyTWFwW21dKSBtYXN0ZXJNYXBbbV0gPSB7Ym9va2luZ3M6W10sIHRvdGFsOjB9OwogICAgbWFzdGVyTWFwW21dLmJvb2tpbmdzLnB1c2goYik7CiAgICBtYXN0ZXJNYXBbbV0udG90YWwgKz0gcHJpY2U7CiAgfSk7CgogIHZhciBtYXN0ZXJUb3RhbCA9IDAsIHNhbG9uQmVmb3JlRGlzY291bnQgPSAwOwogIE9iamVjdC5rZXlzKG1hc3Rlck1hcCkuZm9yRWFjaChmdW5jdGlvbihtKSB7CiAgICB2YXIgcmF0aW8gPSBnZXRNYXN0ZXJSYXRpbyhtKTsKICAgIG1hc3RlclRvdGFsICs9IG1hc3Rlck1hcFttXS50b3RhbCAqIHJhdGlvOwogICAgc2Fsb25CZWZvcmVEaXNjb3VudCArPSBtYXN0ZXJNYXBbbV0udG90YWwgKiAoMSAtIHJhdGlvKTsKICB9KTsKICBtYXN0ZXJUb3RhbCA9IE1hdGgucm91bmQobWFzdGVyVG90YWwpOwogIHZhciBzYWxvblRvdGFsID0gTWF0aC5yb3VuZChzYWxvbkJlZm9yZURpc2NvdW50IC0gZGlzY291bnQpOwogIHZhciBhdmcgPSBjb3VudCA+IDAgPyBNYXRoLnJvdW5kKHRvdGFsIC8gY291bnQpIDogMDsKICB2YXIgdW5pcXVlQ2xpZW50cyA9IE9iamVjdC5rZXlzKHBob25lcykubGVuZ3RoOwoKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbVRvdGFsJykudGV4dENvbnRlbnQgICA9IHRvdGFsICsgJyDigqwnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdtTWFzdGVycycpLnRleHRDb250ZW50ID0gbWFzdGVyVG90YWwgKyAnIOKCrCc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ21TYWxvbicpLnRleHRDb250ZW50ICAgPSBzYWxvblRvdGFsICsgJyDigqwnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdtQ291bnQnKS50ZXh0Q29udGVudCAgID0gY291bnQ7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ21DbGllbnRzJykudGV4dENvbnRlbnQgPSB1bmlxdWVDbGllbnRzOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdtQXZnJykudGV4dENvbnRlbnQgICAgID0gYXZnICsgJyDigqwnOwoKICByZW5kZXJNYXN0ZXJzKG1hc3Rlck1hcCwgZGlzY291bnQsIHRvdGFsKTsKfQoKZnVuY3Rpb24gcmVuZGVyTWFzdGVycyhtYXN0ZXJNYXAsIGRpc2NvdW50LCB0b3RhbEFsbCkgewogIHZhciBlbCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdtYXN0ZXJzTGlzdCcpOwogIGlmIChPYmplY3Qua2V5cyhtYXN0ZXJNYXApLmxlbmd0aCA9PT0gMCkgewogICAgZWwuaW5uZXJIVE1MID0gJzxkaXYgY2xhc3M9ImxvYWRpbmciPtCd0LXRgiDQt9Cw0L/QuNGB0LXQuSDQt9CwINCy0YvQsdGA0LDQvdC90YvQuSDQv9C10YDQuNC+0LQ8L2Rpdj4nOwogICAgcmV0dXJuOwogIH0KICB2YXIgaHRtbCA9ICcnOwogIHZhciBtYXN0ZXJzID0gT2JqZWN0LmtleXMobWFzdGVyTWFwKS5zb3J0KGZ1bmN0aW9uKGEsYil7IHJldHVybiBtYXN0ZXJNYXBbYl0udG90YWwgLSBtYXN0ZXJNYXBbYV0udG90YWw7IH0pOwogIG1hc3RlcnMuZm9yRWFjaChmdW5jdGlvbihuYW1lKSB7CiAgICB2YXIgZCA9IG1hc3Rlck1hcFtuYW1lXTsKICAgIHZhciByYXRpbyA9IGdldE1hc3RlclJhdGlvKG5hbWUpOwogICAgdmFyIG1hc3RlckVhcm4gPSBNYXRoLnJvdW5kKGQudG90YWwgKiByYXRpbyk7CiAgICB2YXIgc2Fsb25TaGFyZSA9IE1hdGgucm91bmQoZC50b3RhbCAqICgxIC0gcmF0aW8pKTsKICAgIHZhciByYXRpbyA9IHRvdGFsQWxsID4gMCA/IGQudG90YWwgLyB0b3RhbEFsbCA6IDA7CiAgICB2YXIgc2Fsb25EaXNjb3VudCA9IE1hdGgucm91bmQoZGlzY291bnQgKiByYXRpbyk7CiAgICB2YXIgc2Fsb25FYXJuID0gc2Fsb25TaGFyZSAtIHNhbG9uRGlzY291bnQ7CiAgICB2YXIgaWQgPSAnbWNfJyArIG5hbWUucmVwbGFjZSgvXHMvZywnXycpOwogICAgaHRtbCArPSAnPGRpdiBjbGFzcz0ibWFzdGVyLWNhcmQgYW5pbSI+JzsKICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9Im1hc3Rlci1oZWFkIiBpZD0ibWhfJytpZCsnIiBvbmNsaWNrPSJ0b2dnbGVNYXN0ZXIoXCcnICsgaWQgKyAnXCcpIj4nOwogICAgaHRtbCArPSAnPGRpdj48ZGl2IGNsYXNzPSJtbmFtZSI+JyArIG5hbWUgKyAnPC9kaXY+JzsKICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9Im1jb3VudCI+JyArIGQuYm9va2luZ3MubGVuZ3RoICsgJyDQt9Cw0L/QuNGB0LXQuSDCtyAnICsgZC50b3RhbCArICcg4oKsINC+0LHRidCw0Y8g0YHRg9C80LzQsDwvZGl2PjwvZGl2Pic7CiAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJtZWFybmluZ3MiPic7CiAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJlYXJuLWl0ZW0iPjxkaXYgY2xhc3M9ImVhcm4tbGFiZWwiPtCc0LDRgdGC0LXRgDwvZGl2PjxkaXYgY2xhc3M9ImVhcm4tdmFsIj4nICsgbWFzdGVyRWFybiArICcg4oKsPC9kaXY+PC9kaXY+JzsKICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9ImVhcm4taXRlbSI+PGRpdiBjbGFzcz0iZWFybi1sYWJlbCI+0KHQsNC70L7QvTwvZGl2PjxkaXYgY2xhc3M9ImVhcm4tdmFsIHNhbG9uIj4nICsgc2Fsb25FYXJuICsgJyDigqw8L2Rpdj48L2Rpdj4nOwogICAgaHRtbCArPSAnPGRpdiBjbGFzcz0iY2hldnJvbiI+4pa8PC9kaXY+PC9kaXY+PC9kaXY+JzsKICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9Im1hc3Rlci1ib2R5IiBpZD0ibWJfJytpZCsnIj4nOwogICAgaHRtbCArPSAnPGRpdiBjbGFzcz0idmlzaXRzLWhlYWRlciI+PHNwYW4+0JTQsNGC0LA8L3NwYW4+PHNwYW4+0JrQu9C40LXQvdGCIC8g0J/QuNGC0L7QvNC10YY8L3NwYW4+PHNwYW4+0KPRgdC70YPQs9CwPC9zcGFuPjxzcGFuIHN0eWxlPSJ0ZXh0LWFsaWduOnJpZ2h0Ij7QptC10L3QsDwvc3Bhbj48L2Rpdj4nOwogICAgdmFyIHNvcnRlZCA9IGQuYm9va2luZ3Muc2xpY2UoKS5zb3J0KGZ1bmN0aW9uKGEsYil7IHJldHVybiBiLmRhdGUubG9jYWxlQ29tcGFyZShhLmRhdGUpOyB9KTsKICAgIHNvcnRlZC5mb3JFYWNoKGZ1bmN0aW9uKGIpIHsKICAgICAgaHRtbCArPSAnPGRpdiBjbGFzcz0idmlzaXQtcm93Ij4nOwogICAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJ2aXNpdC1kYXRlIj4nICsgYi5kYXRlICsgJzwvZGl2Pic7CiAgICAgIGh0bWwgKz0gJzxkaXY+PGRpdiBjbGFzcz0idmlzaXQtY2xpZW50Ij4nICsgKGIuY2xpZW50TmFtZXx8J+KAlCcpICsgJzwvZGl2Pic7CiAgICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9InZpc2l0LXBldCI+JyArIChiLnBldE5hbWUgPyBiLnBldE5hbWUgKyAnIMK3ICcgOiAnJykgKyBiLmJyZWVkICsgJzwvZGl2PjwvZGl2Pic7CiAgICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9InZpc2l0LXN2YyI+JyArIGIuc2VydmljZSArICc8L2Rpdj4nOwogICAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJ2aXNpdC1wcmljZSI+JyArIChiLnByaWNlfHwwKSArICcg4oKsPC9kaXY+PC9kaXY+JzsKICAgIH0pOwogICAgaHRtbCArPSAnPC9kaXY+PC9kaXY+JzsKICB9KTsKICBlbC5pbm5lckhUTUwgPSBodG1sOwp9CgpmdW5jdGlvbiB0b2dnbGVNYXN0ZXIoaWQpIHsKICB2YXIgaGVhZCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdtaF8nK2lkKTsKICB2YXIgYm9keSA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdtYl8nK2lkKTsKICBoZWFkLmNsYXNzTGlzdC50b2dnbGUoJ29wZW4nKTsKICBib2R5LmNsYXNzTGlzdC50b2dnbGUoJ29wZW4nKTsKfQoKLy8g4pSA4pSAINCj0J/QoNCQ0JLQm9CV0J3QmNCVOiDQnNCQ0KHQotCV0KDQkCDilIDilIAKZnVuY3Rpb24gcmVuZGVyTWFzdGVyTGlzdCgpIHsKICB2YXIgZWwgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbWFzdGVyTGlzdFVJJyk7CiAgaWYgKCFlbCkgcmV0dXJuOwogIHZhciBodG1sID0gJyc7CiAgZGIubWFzdGVycy5mb3JFYWNoKGZ1bmN0aW9uKG5hbWUsIGkpIHsKICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9Imxpc3QtaXRlbSI+PGRpdj48ZGl2IGNsYXNzPSJsaS1uYW1lIj4nK25hbWUrJzwvZGl2PjwvZGl2Pic7CiAgICBodG1sICs9ICc8YnV0dG9uIGNsYXNzPSJkZWwtYnRuIiBvbmNsaWNrPSJkZWxNYXN0ZXIoJytpKycpIj7inJU8L2J1dHRvbj48L2Rpdj4nOwogIH0pOwogIGVsLmlubmVySFRNTCA9IGh0bWwgfHwgJzxkaXYgc3R5bGU9ImNvbG9yOiM0NDQ0NDA7Zm9udC1zaXplOi43NXJlbTtwYWRkaW5nOjEycHggMCI+0J3QtdGCINC80LDRgdGC0LXRgNC+0LI8L2Rpdj4nOwp9CmZ1bmN0aW9uIGFkZE1hc3RlcigpIHsKICB2YXIgbmFtZSA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCduZXdNYXN0ZXJOYW1lJykudmFsdWUudHJpbSgpOwogIGlmICghbmFtZSkgcmV0dXJuOwogIGRiLm1hc3RlcnMucHVzaChuYW1lKTsKICBzYXZlKCk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ25ld01hc3Rlck5hbWUnKS52YWx1ZSA9ICcnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCduZXdNYXN0ZXJQaG9uZScpLnZhbHVlID0gJyc7CiAgcmVuZGVyTWFzdGVyTGlzdCgpOwp9CmZ1bmN0aW9uIGRlbE1hc3RlcihpKSB7CiAgaWYgKCFjb25maXJtKCfQo9C00LDQu9C40YLRjCDQvNCw0YHRgtC10YDQsD8nKSkgcmV0dXJuOwogIGRiLm1hc3RlcnMuc3BsaWNlKGksIDEpOwogIHNhdmUoKTsKICByZW5kZXJNYXN0ZXJMaXN0KCk7Cn0KCi8vIOKUgOKUgCDQo9Cf0KDQkNCS0JvQldCd0JjQlTog0J/QntCg0J7QlNCrIOKUgOKUgApmdW5jdGlvbiByZW5kZXJCcmVlZExpc3QoKSB7CiAgdmFyIGVsID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2JyZWVkTGlzdFVJJyk7CiAgaWYgKCFlbCkgcmV0dXJuOwogIHZhciBodG1sID0gJyc7CiAgZGIuYnJlZWRzLmZvckVhY2goZnVuY3Rpb24oYnJlZWQsIGJpKSB7CiAgICB2YXIgYmlkID0gJ2JyXycrYmk7CiAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJicmVlZC1jYXJkIj4nOwogICAgaHRtbCArPSAnPGRpdiBjbGFzcz0iYnJlZWQtaGVhZCIgb25jbGljaz0idG9nZ2xlQnJlZWQoXCcnICsgYmlkICsgJ1wnKSI+JzsKICAgIGh0bWwgKz0gJzxkaXY+PGRpdiBjbGFzcz0iYnJlZWQtbmFtZSI+JyticmVlZC5uYW1lKyc8L2Rpdj4nOwogICAgaHRtbCArPSAnPGRpdiBjbGFzcz0iYnJlZWQtY291bnQiPicrKGJyZWVkLnNlcnZpY2VzfHxbXSkubGVuZ3RoKycg0YPRgdC70YPQszwvZGl2PjwvZGl2Pic7CiAgICBodG1sICs9ICc8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxMHB4Ij4nOwogICAgaHRtbCArPSAnPGJ1dHRvbiBjbGFzcz0iZGVsLWJ0biIgb25jbGljaz0iZXZlbnQuc3RvcFByb3BhZ2F0aW9uKCk7ZGVsQnJlZWQoJytiaSsnKSI+4pyVPC9idXR0b24+JzsKICAgIGh0bWwgKz0gJzxzcGFuIHN0eWxlPSJjb2xvcjojOGE4YTU1O2ZvbnQtc2l6ZTouNzVyZW0iPuKWvDwvc3Bhbj48L2Rpdj48L2Rpdj4nOwogICAgaHRtbCArPSAnPGRpdiBjbGFzcz0iYnJlZWQtYm9keSIgaWQ9IicrYmlkKyciPic7CiAgICAoYnJlZWQuc2VydmljZXN8fFtdKS5mb3JFYWNoKGZ1bmN0aW9uKHN2Yywgc2kpIHsKICAgICAgaHRtbCArPSAnPGRpdiBjbGFzcz0ic3ZjLXJvdyI+PHNwYW4gY2xhc3M9InN2Yy1uYW1lIj4nK3N2Yy5uYW1lKyc8L3NwYW4+JzsKICAgICAgaHRtbCArPSAnPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTBweCI+JzsKICAgICAgaHRtbCArPSAnPHNwYW4gY2xhc3M9InN2Yy1wcmljZSI+JytzdmMucHJpY2UrJyDigqw8L3NwYW4+JzsKICAgICAgaHRtbCArPSAnPGJ1dHRvbiBjbGFzcz0iZGVsLWJ0biIgb25jbGljaz0iZGVsU3ZjKCcrYmkrJywnK3NpKycpIj7inJU8L2J1dHRvbj48L2Rpdj48L2Rpdj4nOwogICAgfSk7CiAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJhZGQtc3ZjLXJvdyI+JzsKICAgIGh0bWwgKz0gJzxpbnB1dCBjbGFzcz0iZmkiIGlkPSJzbl8nK2JpKyciIHBsYWNlaG9sZGVyPSLQo9GB0LvRg9Cz0LAiPic7CiAgICBodG1sICs9ICc8aW5wdXQgY2xhc3M9ImZpIiBpZD0ic3BfJytiaSsnIiBwbGFjZWhvbGRlcj0i0KbQtdC90LAg4oKsIj4nOwogICAgaHRtbCArPSAnPGJ1dHRvbiBjbGFzcz0iaWNvbi1idG4iIG9uY2xpY2s9ImFkZFN2YygnK2JpKycpIj4rPC9idXR0b24+PC9kaXY+JzsKICAgIGh0bWwgKz0gJzwvZGl2PjwvZGl2Pic7CiAgfSk7CiAgZWwuaW5uZXJIVE1MID0gaHRtbCB8fCAnPGRpdiBzdHlsZT0iY29sb3I6IzQ0NDQ0MDtmb250LXNpemU6Ljc1cmVtO3BhZGRpbmc6MTJweCAwIj7QndC10YIg0L/QvtGA0L7QtDwvZGl2Pic7CiAgcmVuZGVyQnJlZWRTZWxlY3QoKTsKfQpmdW5jdGlvbiB0b2dnbGVCcmVlZChpZCkgewogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKGlkKS5jbGFzc0xpc3QudG9nZ2xlKCdvcGVuJyk7Cn0KZnVuY3Rpb24gYWRkQnJlZWQoKSB7CiAgdmFyIG5hbWUgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbmV3QnJlZWROYW1lJykudmFsdWUudHJpbSgpOwogIGlmICghbmFtZSkgcmV0dXJuOwogIGRiLmJyZWVkcy5wdXNoKHtuYW1lOiBuYW1lLCBzZXJ2aWNlczogW119KTsKICBzYXZlKCk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ25ld0JyZWVkTmFtZScpLnZhbHVlID0gJyc7CiAgcmVuZGVyQnJlZWRMaXN0KCk7Cn0KZnVuY3Rpb24gZGVsQnJlZWQoaSkgewogIGlmICghY29uZmlybSgn0KPQtNCw0LvQuNGC0Ywg0L/QvtGA0L7QtNGDINC4INCy0YHQtSDQtdGRINGD0YHQu9GD0LPQuD8nKSkgcmV0dXJuOwogIGRiLmJyZWVkcy5zcGxpY2UoaSwgMSk7CiAgc2F2ZSgpOwogIHJlbmRlckJyZWVkTGlzdCgpOwp9CmZ1bmN0aW9uIGFkZFN2YyhiaSkgewogIHZhciBuYW1lICA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzbl8nK2JpKS52YWx1ZS50cmltKCk7CiAgdmFyIHByaWNlID0gcGFyc2VGbG9hdChkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3BfJytiaSkudmFsdWUpIHx8IDA7CiAgaWYgKCFuYW1lIHx8ICFwcmljZSkgcmV0dXJuOwogIGRiLmJyZWVkc1tiaV0uc2VydmljZXMucHVzaCh7bmFtZTogbmFtZSwgcHJpY2U6IHByaWNlfSk7CiAgc2F2ZSgpOwogIHJlbmRlckJyZWVkTGlzdCgpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdicl8nK2JpKS5jbGFzc0xpc3QuYWRkKCdvcGVuJyk7Cn0KZnVuY3Rpb24gZGVsU3ZjKGJpLCBzaSkgewogIGRiLmJyZWVkc1tiaV0uc2VydmljZXMuc3BsaWNlKHNpLCAxKTsKICBzYXZlKCk7CiAgcmVuZGVyQnJlZWRMaXN0KCk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2JyXycrYmkpLmNsYXNzTGlzdC5hZGQoJ29wZW4nKTsKfQpmdW5jdGlvbiByZW5kZXJCcmVlZFNlbGVjdCgpIHsKICB2YXIgc2VsID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ25ld0NsaWVudEJyZWVkJyk7CiAgaWYgKCFzZWwpIHJldHVybjsKICBzZWwuaW5uZXJIVE1MID0gJzxvcHRpb24gdmFsdWU9IiI+0J/QvtGA0L7QtNCwLi4uPC9vcHRpb24+JzsKICBkYi5icmVlZHMuZm9yRWFjaChmdW5jdGlvbihiKSB7CiAgICBzZWwuaW5uZXJIVE1MICs9ICc8b3B0aW9uPicrYi5uYW1lKyc8L29wdGlvbj4nOwogIH0pOwp9CgovLyDilIDilIAg0KPQn9Cg0JDQktCb0JXQndCY0JU6INCa0JvQmNCV0J3QotCrIOKUgOKUgApmdW5jdGlvbiByZW5kZXJDbGllbnRMaXN0KCkgewogIHZhciBlbCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjbGllbnRMaXN0VUknKTsKICBpZiAoIWVsKSByZXR1cm47CiAgdmFyIG1lcmdlZCA9IHt9OwogIGRiLmNsaWVudHMuZm9yRWFjaChmdW5jdGlvbihjKSB7CiAgICBtZXJnZWRbYy5waG9uZXx8Yy5uYW1lXSA9IGM7CiAgfSk7CiAgYWxsQm9va2luZ3MuZm9yRWFjaChmdW5jdGlvbihiKSB7CiAgICBpZiAoIWIuY2xpZW50UGhvbmUgJiYgIWIuY2xpZW50TmFtZSkgcmV0dXJuOwogICAgdmFyIGtleSA9IGIuY2xpZW50UGhvbmUgfHwgYi5jbGllbnROYW1lOwogICAgaWYgKCFtZXJnZWRba2V5XSkgewogICAgICBtZXJnZWRba2V5XSA9IHtuYW1lOiBiLmNsaWVudE5hbWUsIHBob25lOiBiLmNsaWVudFBob25lLCBwZXQ6IGIucGV0TmFtZSwgYnJlZWQ6IGIuYnJlZWQsIG5vdGU6ICcnLCB2aXNpdHM6IDAsIHRvdGFsOiAwfTsKICAgIH0KICAgIG1lcmdlZFtrZXldLnZpc2l0cyA9IChtZXJnZWRba2V5XS52aXNpdHN8fDApICsgMTsKICAgIG1lcmdlZFtrZXldLnRvdGFsICA9IChtZXJnZWRba2V5XS50b3RhbHx8MCkgKyAocGFyc2VGbG9hdChiLnByaWNlKXx8MCk7CiAgICBtZXJnZWRba2V5XS5sYXN0RGF0ZSAgID0gYi5kYXRlOwogICAgbWVyZ2VkW2tleV0ubGFzdE1hc3RlciA9IGIubWFzdGVyOwogIH0pOwogIHZhciBhcnIgPSBPYmplY3QudmFsdWVzKG1lcmdlZCk7CiAgYXJyLnNvcnQoZnVuY3Rpb24oYSxiKXsgcmV0dXJuIChiLnZpc2l0c3x8MCktKGEudmlzaXRzfHwwKTsgfSk7CiAgdmFyIGh0bWwgPSAnJzsKICBhcnIuZm9yRWFjaChmdW5jdGlvbihjLCBpKSB7CiAgICB2YXIgaW5pdGlhbHMgPSAoYy5uYW1lfHwnPycpLnNwbGl0KCcgJykubWFwKGZ1bmN0aW9uKHcpe3JldHVybiB3WzBdfHwnJzt9KS5qb2luKCcnKS5zdWJzdHJpbmcoMCwyKS50b1VwcGVyQ2FzZSgpOwogICAgaHRtbCArPSAnPGRpdiBjbGFzcz0iY2xpZW50LWNhcmQgYW5pbSI+JzsKICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9ImNsLXJvdyI+JzsKICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9ImNsLWF2YXRhciI+Jytpbml0aWFscysnPC9kaXY+JzsKICAgIGh0bWwgKz0gJzxkaXYgc3R5bGU9ImZsZXg6MSI+PGRpdiBjbGFzcz0iY2wtbmFtZSI+JysoYy5uYW1lfHwn4oCUJykrJzwvZGl2Pic7CiAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJjbC1kZXRhaWwiPicrKGMucGhvbmV8fCcnKSsoYy5wZXQ/JyDCtyAnK2MucGV0OicnKSsoYy5icmVlZD8nIMK3ICcrYy5icmVlZDonJykrJzwvZGl2PjwvZGl2Pic7CiAgICBpZiAoIWMuZnJvbUNhbGVuZGFyKSB7CiAgICAgIGh0bWwgKz0gJzxidXR0b24gY2xhc3M9ImRlbC1idG4iIG9uY2xpY2s9ImRlbENsaWVudCgnK2krJykiPuKclTwvYnV0dG9uPic7CiAgICB9CiAgICBodG1sICs9ICc8L2Rpdj4nOwogICAgaWYgKChjLnZpc2l0c3x8MCkgPiAwKSB7CiAgICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9ImNsLXN0YXRzIj4nOwogICAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJjc3RhdCI+PGRpdiBjbGFzcz0iY3N0LXZhbCI+JysoYy52aXNpdHN8fDApKyc8L2Rpdj48ZGl2IGNsYXNzPSJjc3QtbGFiZWwiPtCy0LjQt9C40YLQvtCyPC9kaXY+PC9kaXY+JzsKICAgICAgaHRtbCArPSAnPGRpdiBjbGFzcz0iY3N0YXQiPjxkaXYgY2xhc3M9ImNzdC12YWwiPicrKGMudG90YWx8fDApKycg4oKsPC9kaXY+PGRpdiBjbGFzcz0iY3N0LWxhYmVsIj7Qv9C+0YLRgNCw0YfQtdC90L48L2Rpdj48L2Rpdj4nOwogICAgICBodG1sICs9ICc8L2Rpdj4nOwogICAgICBpZiAoYy5sYXN0RGF0ZSkgewogICAgICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9ImNsLWxhc3QiPtCf0L7RgdC70LXQtNC90LjQuSDQstC40LfQuNGCOiA8c3Bhbj4nK2MubGFzdERhdGUrKGMubGFzdE1hc3Rlcj8nIMK3ICcrYy5sYXN0TWFzdGVyOicnKSsnPC9zcGFuPjwvZGl2Pic7CiAgICAgIH0KICAgIH0KICAgIGlmIChjLm5vdGUpIGh0bWwgKz0gJzxkaXYgc3R5bGU9ImZvbnQtc2l6ZTouNjJyZW07Y29sb3I6IzU1NTU1MDttYXJnaW4tdG9wOjZweCI+JytjLm5vdGUrJzwvZGl2Pic7CiAgICBodG1sICs9ICc8L2Rpdj4nOwogIH0pOwogIGVsLmlubmVySFRNTCA9IGh0bWwgfHwgJzxkaXYgc3R5bGU9ImNvbG9yOiM0NDQ0NDA7Zm9udC1zaXplOi43NXJlbTtwYWRkaW5nOjEycHggMCI+0J3QtdGCINC60LvQuNC10L3RgtC+0LI8L2Rpdj4nOwp9CmZ1bmN0aW9uIGFkZENsaWVudCgpIHsKICB2YXIgbmFtZSAgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbmV3Q2xpZW50TmFtZScpLnZhbHVlLnRyaW0oKTsKICB2YXIgcGhvbmUgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbmV3Q2xpZW50UGhvbmUnKS52YWx1ZS50cmltKCk7CiAgdmFyIHBldCAgID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ25ld0NsaWVudFBldCcpLnZhbHVlLnRyaW0oKTsKICB2YXIgYnJlZWQgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbmV3Q2xpZW50QnJlZWQnKS52YWx1ZTsKICB2YXIgbm90ZSAgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbmV3Q2xpZW50Tm90ZScpLnZhbHVlLnRyaW0oKTsKICBpZiAoIW5hbWUpIHsgYWxlcnQoJ9CS0LLQtdC00LjRgtC1INC40LzRjyDQutC70LjQtdC90YLQsCcpOyByZXR1cm47IH0KICBkYi5jbGllbnRzLnB1c2goe25hbWU6bmFtZSwgcGhvbmU6cGhvbmUsIHBldDpwZXQsIGJyZWVkOmJyZWVkLCBub3RlOm5vdGUsIHZpc2l0czowLCB0b3RhbDowfSk7CiAgc2F2ZSgpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCduZXdDbGllbnROYW1lJykudmFsdWUgPSAnJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbmV3Q2xpZW50UGhvbmUnKS52YWx1ZSA9ICcnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCduZXdDbGllbnRQZXQnKS52YWx1ZSA9ICcnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCduZXdDbGllbnROb3RlJykudmFsdWUgPSAnJzsKICByZW5kZXJDbGllbnRMaXN0KCk7Cn0KZnVuY3Rpb24gZGVsQ2xpZW50KGkpIHsKICBpZiAoIWNvbmZpcm0oJ9Cj0LTQsNC70LjRgtGMINC60LDRgNGC0L7Rh9C60YMg0LrQu9C40LXQvdGC0LA/JykpIHJldHVybjsKICBkYi5jbGllbnRzLnNwbGljZShpLCAxKTsKICBzYXZlKCk7CiAgcmVuZGVyQ2xpZW50TGlzdCgpOwp9CgovLyDilIDilIAgSU5JVCDilIDilIAKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Rpc2NvdW50SW5wdXQnKS52YWx1ZSA9IGRiLmRpc2NvdW50OwpyZW5kZXJNYXN0ZXJMaXN0KCk7CnJlbmRlckJyZWVkTGlzdCgpOwpyZW5kZXJDbGllbnRMaXN0KCk7CmxvYWRTdGF0cygpOwo8L3NjcmlwdD4KPC9ib2R5Pgo8L2h0bWw+"

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
    params = {"action": "stats", "from": "01.01.2020", "to": (today + timedelta(days=180)).strftime("%d.%m.%Y")}
    r = requests.get(GOOGLE_SCRIPT, params=params, timeout=30)
    data = r.json()
    bookings = data.get("bookings", []) if isinstance(data, dict) else []

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
        params = {"action": "stats", "from": "01.01.2020", "to": (today + timedelta(days=180)).strftime("%d.%m.%Y")}
        r = requests.get(GOOGLE_SCRIPT, params=params, timeout=30)
        data = r.json()
        bookings = data.get("bookings", []) if isinstance(data, dict) else []

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
        params = {"action": "stats", "from": "01.01.2020", "to": (today + timedelta(days=180)).strftime("%d.%m.%Y")}
        r = requests.get(GOOGLE_SCRIPT, params=params, timeout=30)
        data = r.json()
        bookings = data.get("bookings", []) if isinstance(data, dict) else []
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

@app.route("/admin/clients")
def admin_clients_page():
    import urllib.parse as _urlp

    error = None
    clients = []
    try:
        today = datetime.now(_REMINDER_TZ).date() if _REMINDER_TZ else datetime.utcnow().date()
        params = {"action": "stats", "from": "01.01.2020", "to": (today + timedelta(days=180)).strftime("%d.%m.%Y")}
        r = requests.get(GOOGLE_SCRIPT, params=params, timeout=30)
        data = r.json()
        bookings = data.get("bookings", []) if isinstance(data, dict) else []

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

        for phone, entry in by_phone.items():
            saved = _load_client_data(phone)
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

  <div class="stats">
    <div class="stat"><div class="n">{len(clients)}</div><div class="l">клиентов</div></div>
    <div class="stat"><div class="n">{total_pets}</div><div class="l">питомцев</div></div>
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
            params = {"action": "stats", "from": "01.01.2020", "to": (today + timedelta(days=180)).strftime("%d.%m.%Y")}
            r = requests.get(GOOGLE_SCRIPT, params=params, timeout=30)
            data = r.json()
            bookings = data.get("bookings", []) if isinstance(data, dict) else []

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
            for key, entry in by_phone.items():
                saved = _load_client_data(entry["phone"]) if entry["phone"] else {}
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
