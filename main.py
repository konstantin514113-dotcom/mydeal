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
BOOKING_HTML_B64 = "77u/PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9InJ1Ij4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idGhlbWUtY29sb3IiIGNvbnRlbnQ9IiMwYTBhMGEiPgo8bWV0YSBuYW1lPSJ2aWV3cG9ydCIgY29udGVudD0id2lkdGg9ZGV2aWNlLXdpZHRoLGluaXRpYWwtc2NhbGU9MSI+Cjx0aXRsZT5SJkogR3Jvb21pbmc8L3RpdGxlPgo8bGluayBocmVmPSJodHRwczovL2ZvbnRzLmdvb2dsZWFwaXMuY29tL2NzczI/ZmFtaWx5PUNvcm1vcmFudCtHYXJhbW9uZDp3Z2h0QDQwMDs2MDAmZmFtaWx5PVBsYXlmYWlyK0Rpc3BsYXk6aXRhbCx3Z2h0QDAsNDAwOzAsNjAwOzAsNzAwOzEsNDAwJmZhbWlseT1Nb250c2VycmF0OndnaHRAMzAwOzQwMDs1MDA7NjAwJmRpc3BsYXk9c3dhcCIgcmVsPSJzdHlsZXNoZWV0Ij4KPHN0eWxlPgoqe2JveC1zaXppbmc6Ym9yZGVyLWJveDttYXJnaW46MDtwYWRkaW5nOjB9Cmh0bWwsYm9keXttaW4taGVpZ2h0OjEwMHZoO2JhY2tncm91bmQ6IzBhMGEwYTtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXdlaWdodDo0MDB9Ci5zY3JlZW57ZGlzcGxheTpub25lO21pbi1oZWlnaHQ6MTAwdmg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2FsaWduLWl0ZW1zOmNlbnRlcjtwYWRkaW5nOjQ4cHggMCA2NHB4fQouc2NyZWVuLmFjdGl2ZXtkaXNwbGF5OmZsZXh9Ci5jb257d2lkdGg6MTAwJTttYXgtd2lkdGg6NDAwcHg7cGFkZGluZzowIDI4cHh9Ci5iYWNrLWJ0bntkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo4cHg7Y29sb3I6I2ZmZmZmZjtmb250LXNpemU6MC44cmVtO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyOm5vbmU7Y3Vyc29yOnBvaW50ZXI7cGFkZGluZzowO21hcmdpbi1ib3R0b206MzZweDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXdlaWdodDozMDA7dHJhbnNpdGlvbjpjb2xvciAuMnN9Ci5iYWNrLWJ0bjpob3Zlcntjb2xvcjojZmZmZmZmfQoubG9nby1yantmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZjtmb250LXNpemU6Mi41cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmfQoubG9nby1zdWJ7Zm9udC1zaXplOjAuNjYzcmVtO2ZvbnQtd2VpZ2h0OjYwMDtsZXR0ZXItc3BhY2luZzouNGVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO21hcmdpbi10b3A6M3B4O3BhZGRpbmctYm90dG9tOjE0cHg7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4yKTttYXJnaW4tYm90dG9tOjIwcHh9Ci5ob21lLXJqe2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmO2ZvbnQtc2l6ZTozLjI1cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjF9Ci5sb2dvLXRhZ3tmb250LXNpemU6MC43NXJlbTtsZXR0ZXItc3BhY2luZzouMTJlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjtsaW5lLWhlaWdodDoxLjV9Ci5sb2dvLXJvd3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6ZmxleC1lbmQ7Z2FwOjEycHg7bWFyZ2luLWJvdHRvbToyOHB4O3BhZGRpbmctYm90dG9tOjE4cHg7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4yKX0KLmxvZ28taW1nLXJvd3ttYXJnaW4tYm90dG9tOjI4cHg7cGFkZGluZy1ib3R0b206MThweDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjIpfQoubG9nby1pbWd7aGVpZ2h0OjkwcHg7d2lkdGg6YXV0bztkaXNwbGF5OmJsb2NrfQouaG9tZS1nc3Vie2ZvbnQtc2l6ZTowLjY2M3JlbTtmb250LXdlaWdodDo2MDA7bGV0dGVyLXNwYWNpbmc6LjRlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjttYXJnaW4tdG9wOjZweDttYXJnaW4tYm90dG9tOjIycHh9Ci5ob21lLWgxe2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6My4xMjVyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNmZmZmZmY7bGluZS1oZWlnaHQ6MS4xO21hcmdpbi1ib3R0b206NnB4fQouaG9tZS1oMSBlbXtmb250LXN0eWxlOml0YWxpYztjb2xvcjojZmZmZmZmfQouaG9tZS1zdWJ7Zm9udC1zaXplOjAuOHJlbTtsZXR0ZXItc3BhY2luZzouMThlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjI4cHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5vcHR7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTZweDtwYWRkaW5nOjE2cHggMDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNyk7dGV4dC1kZWNvcmF0aW9uOm5vbmU7Y29sb3I6I2ZmZmZmZjt0cmFuc2l0aW9uOmNvbG9yIC4ycztjdXJzb3I6cG9pbnRlcjtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyLXRvcDpub25lO2JvcmRlci1sZWZ0Om5vbmU7Ym9yZGVyLXJpZ2h0Om5vbmU7d2lkdGg6MTAwJTtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWZ9Ci5vcHQ6aG92ZXJ7Y29sb3I6I2ZmZn0KLm9wdC1pY29ue3dpZHRoOjM4cHg7aGVpZ2h0OjM4cHg7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2ZsZXgtc2hyaW5rOjB9Ci5vcHQtaWNvbi1pbWd7d2lkdGg6MzhweDtoZWlnaHQ6MzhweDtib3JkZXItcmFkaXVzOjlweDtvYmplY3QtZml0OmNvdmVyfQoub3B0LXRleHR7ZmxleDoxO3RleHQtYWxpZ246bGVmdH0KLm9wdC10aXRsZXtmb250LXNpemU6MS41MTJyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbToycHg7dHJhbnNpdGlvbjpjb2xvciAuMnM7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmfQoub3B0OmhvdmVyIC5vcHQtdGl0bGV7Y29sb3I6I2ZmZn0KLm9wdC1oYW5kbGV7Zm9udC1zaXplOjAuODg3cmVtO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC13ZWlnaHQ6MzAwfQoub3B0LWFycm93e2NvbG9yOiNmZmZmZmY7Zm9udC1zaXplOjEuMjI1cmVtO2ZsZXgtc2hyaW5rOjA7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7dHJhbnNpdGlvbjpjb2xvciAuMnN9Ci5vcHQ6aG92ZXIgLm9wdC1hcnJvd3tjb2xvcjojZmZmZmZmfQouZGl2aWRlcntkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxMnB4O3BhZGRpbmc6MTJweCAwfQouZGl2aWRlcjo6YmVmb3JlLC5kaXZpZGVyOjphZnRlcntjb250ZW50OicnO2ZsZXg6MTtoZWlnaHQ6MXB4O2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDYpfQouZGl2aWRlciBzcGFue2ZvbnQtc2l6ZTowLjY4OHJlbTtsZXR0ZXItc3BhY2luZzouMjJlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZn0KLmhvbWUtZm9vdHttYXJnaW4tdG9wOjM2cHg7cGFkZGluZy10b3A6MjBweDtib3JkZXItdG9wOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNik7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2FsaWduLWl0ZW1zOmNlbnRlcn0KLmhvbWUtZm9vdCBzcGFue2ZvbnQtc2l6ZTowLjc3NXJlbTtsZXR0ZXItc3BhY2luZzouMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQouZmRvdHt3aWR0aDoycHg7aGVpZ2h0OjJweDtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjE2KX0KLnByb2dyZXNze2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7bWFyZ2luLWJvdHRvbTo0MHB4O292ZXJmbG93OmhpZGRlbjtjb3VudGVyLXJlc2V0OnN0ZXB9Ci5wc3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo1cHg7Zm9udC1zaXplOjAuNjYzcmVtO2xldHRlci1zcGFjaW5nOi4xMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO3doaXRlLXNwYWNlOm5vd3JhcDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtjb3VudGVyLWluY3JlbWVudDpzdGVwfQoucHMuZG9uZXtjb2xvcjojZmZmZmZmfQoucHMuYWN0aXZle2NvbG9yOiNmZmZmZmZ9Ci5wZG90e3dpZHRoOjE4cHg7aGVpZ2h0OjE4cHg7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2ZsZXgtc2hyaW5rOjA7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xMik7Zm9udC1zaXplOjAuNjYzcmVtO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2ZvbnQtd2VpZ2h0OjYwMH0KLnBkb3Q6OmJlZm9yZXtjb250ZW50OmNvdW50ZXIoc3RlcCxkZWNpbWFsLWxlYWRpbmctemVybyl9Ci5wcy5kb25lIC5wZG90e2JvcmRlci1jb2xvcjojZmZmZmZmO2NvbG9yOiNmZmZmZmZ9Ci5wcy5hY3RpdmUgLnBkb3R7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLnBse2ZsZXg6MTtoZWlnaHQ6MXB4O2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDcpO21hcmdpbjowIDVweDttaW4td2lkdGg6NnB4fQoucGwuZG9uZXtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjE4KX0KLnN0ZXB7ZGlzcGxheTpub25lfS5zdGVwLnNob3d7ZGlzcGxheTpibG9jazthbmltYXRpb246ZnUgLjM1cyBlYXNlIGJvdGh9Ci5zbGJse2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6MS45MzhyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbToyMHB4O2xldHRlci1zcGFjaW5nOi4wMWVtfQouc2JveHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxMHB4O2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjE2KTtwYWRkaW5nOjAgMnB4O3RyYW5zaXRpb246Ym9yZGVyLWNvbG9yIC4yc30KLnNib3g6Zm9jdXMtd2l0aGlue2JvcmRlci1ib3R0b20tY29sb3I6I2ZmZmZmZn0KLnNpe29wYWNpdHk6LjI7Zm9udC1zaXplOjEuMjI1cmVtO2ZsZXgtc2hyaW5rOjB9CiNiSW5wdXR7ZmxleDoxO2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOm5vbmU7b3V0bGluZTpub25lO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6MS41MTJyZW07Y29sb3I6I2ZmZmZmZjtwYWRkaW5nOjEycHggMH0KI2JJbnB1dDo6cGxhY2Vob2xkZXJ7Y29sb3I6I2ZmZmZmZn0KLmNscntiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyOm5vbmU7Y29sb3I6I2ZmZmZmZjtjdXJzb3I6cG9pbnRlcjtmb250LXNpemU6MS4xNXJlbTtkaXNwbGF5Om5vbmU7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5jbHIuc2hvd3tkaXNwbGF5OmJsb2NrfQouYndyYXB7cG9zaXRpb246cmVsYXRpdmU7bWFyZ2luLWJvdHRvbToyMHB4fQouZHJvcHtwb3NpdGlvbjphYnNvbHV0ZTtsZWZ0OjA7cmlnaHQ6MDtiYWNrZ3JvdW5kOiMwZjBmMGY7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xKTtib3JkZXItdG9wOm5vbmU7bWF4LWhlaWdodDoyMDBweDtvdmVyZmxvdy15OmF1dG87ei1pbmRleDo1MDtkaXNwbGF5Om5vbmV9Ci5kcm9wLm9wZW57ZGlzcGxheTpibG9ja30KLmRpdGVte3BhZGRpbmc6MTFweCAxNHB4O2ZvbnQtc2l6ZToxLjM2M3JlbTtjb2xvcjojZmZmZmZmO2N1cnNvcjpwb2ludGVyO2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA1KTtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7dHJhbnNpdGlvbjpjb2xvciAuMnN9Ci5kaXRlbTpob3Zlcntjb2xvcjojZmZmfQouZGl0ZW0gbWFya3tiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2NvbG9yOiNmZmY7Zm9udC13ZWlnaHQ6NzAwfQoubm9yZXN7cGFkZGluZzoxNHB4O2ZvbnQtc2l6ZToxLjI4OHJlbTtjb2xvcjojZmZmZmZmO2ZvbnQtc3R5bGU6aXRhbGljO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZn0KLm5vLWJyZWVkLWJhbm5lcntkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxMnB4O3BhZGRpbmc6MTRweCAwO2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA2KTtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmNvbG9yIC4yczttYXJnaW4tdG9wOjRweH0KLm5vLWJyZWVkLWJhbm5lcjpob3ZlciAubm8tYnJlZWQtYmFubmVyLXRpdGxle2NvbG9yOiNmZmZmZmZ9Ci5uby1icmVlZC1iYW5uZXItaWNvbntmb250LXNpemU6MS41NzVyZW07ZmxleC1zaHJpbms6MDtvcGFjaXR5Oi4zfQoubm8tYnJlZWQtYmFubmVyLXRleHR7ZmxleDoxfQoubm8tYnJlZWQtYmFubmVyLXRpdGxle2ZvbnQtc2l6ZToxLjQzOHJlbTtjb2xvcjojZmZmZmZmO2ZvbnQtd2VpZ2h0OjYwMDttYXJnaW4tYm90dG9tOjJweDtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7dHJhbnNpdGlvbjpjb2xvciAuMnN9Ci5uby1icmVlZC1iYW5uZXItc3Vie2ZvbnQtc2l6ZTowLjg4N3JlbTtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjEuNTtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZn0KLm5vLWJyZWVkLWJhbm5lci1hcnJvd3tjb2xvcjojZmZmZmZmO2ZvbnQtc2l6ZToxLjIyNXJlbTtmbGV4LXNocmluazowO3RyYW5zaXRpb246Y29sb3IgLjJzfQouc2JhZGdle2Rpc3BsYXk6bm9uZTthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7bWFyZ2luLWJvdHRvbToyMHB4fQouc2JhZGdlLnNob3d7ZGlzcGxheTpmbGV4fQouYm5hbWV7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgI2ZmZmZmZjtjb2xvcjojZmZmZmZmO3BhZGRpbmc6MnB4IDA7Zm9udC1zaXplOjEuNDM4cmVtO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZn0KLmJjaGd7Zm9udC1zaXplOjAuOHJlbTtjb2xvcjojZmZmZmZmO2N1cnNvcjpwb2ludGVyO2xldHRlci1zcGFjaW5nOi4xMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLmJjaGc6aG92ZXJ7Y29sb3I6I2ZmZmZmZn0KLnN2YnRue2Rpc3BsYXk6YmxvY2s7cGFkZGluZzowO2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOm5vbmU7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDgpO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2N1cnNvcjpwb2ludGVyO3RleHQtYWxpZ246bGVmdDt0cmFuc2l0aW9uOmJvcmRlci1jb2xvciAuMnM7d2lkdGg6MTAwJTtvdmVyZmxvdzpoaWRkZW47cG9zaXRpb246cmVsYXRpdmV9Ci5zdmJ0bjpob3Zlcntib3JkZXItYm90dG9tLWNvbG9yOiNmZmZmZmZ9Ci5zdmJ0bi5hY3RpdmV7Ym9yZGVyLWJvdHRvbS1jb2xvcjojZmZmZmZmfQouc3Zwe2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmO2ZsZXgtc2hyaW5rOjB9Ci5tYXN0ZXJze2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDFmcjtnYXA6MXB4O2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDcpfQoubWJ0bntiYWNrZ3JvdW5kOiMwYTBhMGE7cGFkZGluZzoyMnB4IDEycHg7dGV4dC1hbGlnbjpjZW50ZXI7Y3Vyc29yOnBvaW50ZXI7dHJhbnNpdGlvbjpiYWNrZ3JvdW5kIC4ycztmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Ym9yZGVyOm5vbmV9Ci5tYnRuOmhvdmVye2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDMpfQoubWJ0bi5hY3RpdmV7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNSl9Ci5tYXZ7d2lkdGg6NDBweDtoZWlnaHQ6NDBweDtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTQpO2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OmNlbnRlcjttYXJnaW46MCBhdXRvIDEwcHg7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjQzOHJlbTtmb250LXdlaWdodDo2MDA7Y29sb3I6I2ZmZmZmZn0KLm1idG4uYWN0aXZlIC5tYXZ7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLm1uYW1le2ZvbnQtc2l6ZToxLjQzOHJlbTtmb250LXdlaWdodDo2MDA7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWZ9Ci5tYnRuOmhvdmVyIC5tbmFtZXtjb2xvcjojZmZmZmZmfQoubWJ0bi5hY3RpdmUgLm1uYW1le2NvbG9yOiNmZmZmZmZ9Ci5tdGl0bGV7Zm9udC1zaXplOjAuOHJlbTtjb2xvcjojZmZmZmZmO21hcmdpbi10b3A6M3B4O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQouZ2J0bntkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO3BhZGRpbmc6MTRweCAwO2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOm5vbmU7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDgpO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjQzOHJlbTtjdXJzb3I6cG9pbnRlcjt3aWR0aDoxMDAlO3RyYW5zaXRpb246YWxsIC4yc30KLmdidG46aG92ZXJ7Y29sb3I6I2ZmZmZmZn0KLmdidG4uYWN0aXZle2NvbG9yOiNmZmZmZmY7Ym9yZGVyLWJvdHRvbS1jb2xvcjojZmZmZmZmfQouY2FsLWh7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2FsaWduLWl0ZW1zOmNlbnRlcjttYXJnaW4tYm90dG9tOjE2cHh9Ci5jYWwtbXtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjEuOTM4cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmfQouY2FsLW57YmFja2dyb3VuZDpub25lO2JvcmRlcjpub25lO2NvbG9yOiNmZmZmZmY7Y3Vyc29yOnBvaW50ZXI7Zm9udC1zaXplOjEuNTc1cmVtO3BhZGRpbmc6NHB4IDhweDt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLmNhbC1uOmhvdmVye2NvbG9yOiNmZmZmZmZ9Ci5jZ3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCg3LDFmcik7Z2FwOjJweDttYXJnaW4tYm90dG9tOjEycHh9Ci5jZG57dGV4dC1hbGlnbjpjZW50ZXI7Zm9udC1zaXplOjAuNjYzcmVtO2NvbG9yOiNmZmZmZmY7cGFkZGluZzo0cHggMDt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7bGV0dGVyLXNwYWNpbmc6LjFlbX0KLmNke3RleHQtYWxpZ246Y2VudGVyO2N1cnNvcjpwb2ludGVyO2NvbG9yOiNmZmZmZmY7Ym9yZGVyOjFweCBzb2xpZCB0cmFuc3BhcmVudDt0cmFuc2l0aW9uOmFsbCAuMnN9Ci5jZDpob3Zlcjpub3QoLmRpcyk6bm90KC5wYWQpIC5jZC1pbm5lcntiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA3KSFpbXBvcnRhbnQ7Y29sb3I6I2ZmZmZmZiFpbXBvcnRhbnR9Ci5jZC5zZWwgLmNkLWlubmVye2JhY2tncm91bmQ6I2ZmZmZmZiFpbXBvcnRhbnQ7Y29sb3I6IzBhMGEwYSFpbXBvcnRhbnQ7Zm9udC13ZWlnaHQ6NzAwIWltcG9ydGFudDtib3JkZXI6bm9uZSFpbXBvcnRhbnR9Ci5jZC50b2QgLmNkLWlubmVye2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMjgpO2NvbG9yOiNmZmZ9Ci5jZC5kaXN7Y29sb3I6I2ZmZmZmZjtjdXJzb3I6ZGVmYXVsdH0KLnRne2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDQsMWZyKTtnYXA6MXB4O2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDcpfQoudGJ0bntiYWNrZ3JvdW5kOiMwYTBhMGE7Ym9yZGVyOm5vbmU7cGFkZGluZzoxM3B4IDRweDt0ZXh0LWFsaWduOmNlbnRlcjtmb250LXNpemU6MS4zMjVyZW07Y29sb3I6I2ZmZmZmZjtjdXJzb3I6cG9pbnRlcjtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7dHJhbnNpdGlvbjphbGwgLjJzfQoudGJ0bjpob3Zlcntjb2xvcjojZmZmZmZmO2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDQpfQoudGJ0bi5hY3RpdmV7Y29sb3I6I2ZmZmZmZn0KLnN1bXtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjpub25lO2JvcmRlci10b3A6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA4KTtwYWRkaW5nOjIwcHggMDttYXJnaW4tYm90dG9tOjIwcHh9Ci5zcntkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47cGFkZGluZzo4cHggMDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNSk7Zm9udC1zaXplOjEuMzYzcmVtO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZn0KLnNyOmxhc3QtY2hpbGR7Ym9yZGVyLWJvdHRvbTpub25lO3BhZGRpbmctdG9wOjE0cHh9Ci5zbHtjb2xvcjojZmZmZmZmfS5zdntjb2xvcjojZmZmZmZmO3RleHQtYWxpZ246cmlnaHR9Ci5zcHtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjIuNDM4cmVtO2NvbG9yOiNmZmZmZmY7Zm9udC13ZWlnaHQ6NjAwfQouZmd7bWFyZ2luLWJvdHRvbToyMHB4fQouZmx7Zm9udC1zaXplOjAuNzEycmVtO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO21hcmdpbi1ib3R0b206OHB4O2Rpc3BsYXk6YmxvY2s7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5maXt3aWR0aDoxMDAlO2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOm5vbmU7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTQpO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjUxMnJlbTtwYWRkaW5nOjEwcHggMDtvdXRsaW5lOm5vbmU7dHJhbnNpdGlvbjpib3JkZXItY29sb3IgLjJzfQouZmk6Zm9jdXN7Ym9yZGVyLWJvdHRvbS1jb2xvcjojZmZmZmZmfQouY2J0bntkaXNwbGF5OmJsb2NrO3dpZHRoOjEwMCU7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZTowLjg2MnJlbTtmb250LXdlaWdodDo2MDA7bGV0dGVyLXNwYWNpbmc6LjI4ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO3RleHQtYWxpZ246Y2VudGVyO3BhZGRpbmc6MTZweDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjI1KTtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmFsbCAuMnN9Ci5jYnRuOmhvdmVye2JvcmRlci1jb2xvcjojZmZmZmZmO2NvbG9yOiNmZmZmZmZ9Ci5zYmxvY2t7dGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzo1MnB4IDIwcHg7ZGlzcGxheTpub25lfQouc2Jsb2NrLnNob3d7ZGlzcGxheTpibG9jazthbmltYXRpb246ZnUgLjVzIGVhc2UgYm90aH0KLnNpMntmb250LXNpemU6My42cmVtO21hcmdpbi1ib3R0b206MjBweDtvcGFjaXR5Oi40fQouc3R7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToyLjcyNXJlbTtjb2xvcjojZmZmZmZmO21hcmdpbi1ib3R0b206MTBweDtmb250LXdlaWdodDo2MDB9Ci5zc3tmb250LXNpemU6MS4wNzVyZW07Y29sb3I6I2ZmZmZmZjtsaW5lLWhlaWdodDoxLjk7bWFyZ2luLWJvdHRvbToyOHB4O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQouaGJ0bntiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTYpO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC1zaXplOjAuODYycmVtO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtwYWRkaW5nOjEzcHggMjhweDtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmFsbCAuMnN9Ci5oYnRuOmhvdmVye2JvcmRlci1jb2xvcjojZmZmZmZmO2NvbG9yOiNmZmZmZmZ9Ci5sb2FkaW5nLXNsb3Rze2NvbG9yOiNmZmZmZmY7Zm9udC1zaXplOjEuMjg4cmVtO3BhZGRpbmc6MTJweCAwO3RleHQtYWxpZ246Y2VudGVyO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXN0eWxlOml0YWxpY30KLmNke2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2FsaWduLWl0ZW1zOmNlbnRlcjtoZWlnaHQ6MzZweCFpbXBvcnRhbnQ7cGFkZGluZzowIWltcG9ydGFudH0KLmNkLWlubmVye3dpZHRoOjMycHg7aGVpZ2h0OjMycHg7Ym9yZGVyLXJhZGl1czowO2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OmNlbnRlcjtmb250LXNpemU6MS4xNXJlbTtjdXJzb3I6cG9pbnRlcjtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWZ9Ci5jZC5hdmFpbCAuY2QtaW5uZXJ7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDkwLDE4MCw5MCwuMzUpO2NvbG9yOnJnYmEoOTAsMTgwLDkwLC42NSl9Ci5jZC5idXN5IC5jZC1pbm5lcntib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA3KTtjb2xvcjojZmZmZmZmfQouY2Quc2VsIC5jZC1pbm5lcntiYWNrZ3JvdW5kOiNmZmZmZmYhaW1wb3J0YW50O2NvbG9yOiMwYTBhMGEhaW1wb3J0YW50O2ZvbnQtd2VpZ2h0OjcwMCFpbXBvcnRhbnQ7Ym9yZGVyOm5vbmUhaW1wb3J0YW50fQouY2QudG9kIC5jZC1pbm5lcntib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjI4KTtjb2xvcjojZmZmO2ZvbnQtd2VpZ2h0OjYwMH0KLmNkLmRpcyAuY2QtaW5uZXJ7Y29sb3I6I2ZmZmZmZjtjdXJzb3I6ZGVmYXVsdDtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyOm5vbmV9Ci5zdmJ0bi1yb3d7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2FsaWduLWl0ZW1zOmJhc2VsaW5lO21hcmdpbi1ib3R0b206NnB4O3BhZGRpbmc6MTZweCAwIDB9Ci5zdmJ0bi1uYW1le2ZvbnQtc2l6ZToxLjUxMnJlbTtjb2xvcjojZmZmZmZmO2ZvbnQtd2VpZ2h0OjYwMDtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWZ9Ci5zdmJ0bi5hY3RpdmUgLnN2YnRuLW5hbWV7Y29sb3I6I2ZmZmZmZn0KLnN2YnRuLXByaWNle2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6MS43MjVyZW07Y29sb3I6I2ZmZmZmZjtmb250LXdlaWdodDo2MDA7ZmxleC1zaHJpbms6MH0KLnN2YnRuLmFjdGl2ZSAuc3ZidG4tcHJpY2V7Y29sb3I6I2ZmZmZmZn0KLnN2YnRuLWRlc2N7Zm9udC1zaXplOjFyZW07Y29sb3I6I2ZmZmZmZjtsaW5lLWhlaWdodDoxLjc7ZGlzcGxheTpibG9jaztwYWRkaW5nOjAgMCAxNHB4O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO3doaXRlLXNwYWNlOnByZS1saW5lfQouc3ZidG4uYWN0aXZlIC5zdmJ0bi1kZXNje2NvbG9yOiNmZmZmZmZ9Ci5zdmJ0bi10YWd7Zm9udC1zaXplOjAuOTc1cmVtO2NvbG9yOiNmZmZmZmY7Zm9udC1zdHlsZTppdGFsaWM7ZGlzcGxheTpibG9jazttYXJnaW4tdG9wOjJweDtwYWRkaW5nOjAgMCAxNHB4O2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZn0KLnN2YnRuLmFjdGl2ZSAuc3ZidG4tdGFne2NvbG9yOiNmZmZmZmZ9CkBtZWRpYShtYXgtd2lkdGg6NDAwcHgpey5zdmJ0bi1uYW1le2ZvbnQtc2l6ZToxLjM2M3JlbX0uc3ZidG4tcHJpY2V7Zm9udC1zaXplOjEuNTEycmVtfS5zdmJ0bi1kZXNje2ZvbnQtc2l6ZTowLjkzOHJlbX0uc3ZidG4tdGFne2ZvbnQtc2l6ZTowLjg4N3JlbX19CkBrZXlmcmFtZXMgZnV7ZnJvbXtvcGFjaXR5OjA7dHJhbnNmb3JtOnRyYW5zbGF0ZVkoMTBweCl9dG97b3BhY2l0eToxO3RyYW5zZm9ybTp0cmFuc2xhdGVZKDApfX0KLmxhbmctYmFye3Bvc2l0aW9uOmZpeGVkO3RvcDoxMnB4O3JpZ2h0OjE0cHg7ei1pbmRleDo5OTk7ZGlzcGxheTpmbGV4O2dhcDo2cHh9Ci5sYW5nLWJ0bntiYWNrZ3JvdW5kOnJnYmEoMTAsMTAsMTAsLjkyKTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEpO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC1zaXplOjAuNzc1cmVtO2xldHRlci1zcGFjaW5nOi4xNWVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtwYWRkaW5nOjVweCAxMHB4O2N1cnNvcjpwb2ludGVyO3RyYW5zaXRpb246YWxsIC4yc30KLmxhbmctYnRuOmhvdmVye2JvcmRlci1jb2xvcjojZmZmZmZmO2NvbG9yOiNmZmZmZmZ9Ci5sYW5nLWJ0bi5hY3RpdmV7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLmNiay1idG57YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjE0KTtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZTowLjg2MnJlbTtsZXR0ZXItc3BhY2luZzouMTZlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7cGFkZGluZzoxMnB4IDIwcHg7Y3Vyc29yOnBvaW50ZXI7dHJhbnNpdGlvbjphbGwgLjJzO3dpZHRoOjEwMCV9Ci5jYmstYnRuOmhvdmVye2JvcmRlci1jb2xvcjojZmZmZmZmO2NvbG9yOiNmZmZmZmZ9Ci5tYnRuLC5zdmJ0biwuZ2J0biwudGJ0biwuY2J0biwuaGJ0biwuY2JrLWJ0biwubGFuZy1idG4sLmJhY2stYnRuLC5vcHQsLmRpdGVtLC5jZCwubm8tYnJlZWQtYmFubmVyLC5iY2hne3RyYW5zaXRpb246YWxsIC4xNXMgZWFzZX0KLm1idG46YWN0aXZlLC5zdmJ0bjphY3RpdmUsLmdidG46YWN0aXZlLC50YnRuOmFjdGl2ZSwuY2J0bjphY3RpdmUsLmhidG46YWN0aXZlLC5jYmstYnRuOmFjdGl2ZSwubGFuZy1idG46YWN0aXZlLC5iYWNrLWJ0bjphY3RpdmUsLm9wdDphY3RpdmUsLmRpdGVtOmFjdGl2ZSwuY2Q6YWN0aXZlLC5uby1icmVlZC1iYW5uZXI6YWN0aXZlLC5iY2hnOmFjdGl2ZXt0cmFuc2Zvcm06c2NhbGUoMC45Nil9Cjwvc3R5bGU+CjwvaGVhZD4KPGJvZHk+CjxhIGhyZWY9Ii9hZG1pbj9wYXNzPWFuemExOTg1IiBpZD0iYWRtaW5CYWNrTGluayIgc3R5bGU9ImRpc3BsYXk6bm9uZTtwb3NpdGlvbjpmaXhlZDt0b3A6MTRweDtyaWdodDoxNHB4O2ZvbnQtc2l6ZTowLjlyZW07Y29sb3I6I2M5YTA1YTt0ZXh0LWRlY29yYXRpb246bm9uZTt6LWluZGV4Ojk5OTtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtiYWNrZ3JvdW5kOnJnYmEoMTAsMTAsOSwuODUpO3BhZGRpbmc6NnB4IDEycHg7Ym9yZGVyLXJhZGl1czoyMHB4O2JvcmRlcjoxcHggc29saWQgcmdiYSgyMDEsMTYwLDkwLC4zNSkiPuKGkCDQkNC00LzQuNC9LdC/0LDQvdC10LvRjDwvYT4KPHNjcmlwdD5pZihsb2NhdGlvbi5zZWFyY2guaW5kZXhPZigncGFzcz1hbnphMTk4NScpIT09LTEpe2RvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdhZG1pbkJhY2tMaW5rJykuc3R5bGUuZGlzcGxheT0nYmxvY2snO308L3NjcmlwdD4KPGRpdiBjbGFzcz0ibGFuZy1iYXIiPgogIDxidXR0b24gY2xhc3M9ImxhbmctYnRuIGFjdGl2ZSIgb25jbGljaz0ic2V0TGFuZygncnUnKSI+UlU8L2J1dHRvbj4KICA8YnV0dG9uIGNsYXNzPSJsYW5nLWJ0biIgb25jbGljaz0ic2V0TGFuZygnZW4nKSI+RU48L2J1dHRvbj4KICA8YnV0dG9uIGNsYXNzPSJsYW5nLWJ0biIgb25jbGljaz0ic2V0TGFuZygnZXQnKSI+RVQ8L2J1dHRvbj4KPC9kaXY+Cgo8IS0tIEhPTUUgLS0+CjxkaXYgY2xhc3M9InNjcmVlbiBhY3RpdmUiIGlkPSJob21lU2NyZWVuIj4KPGRpdiBjbGFzcz0iY29uIj4KICA8ZGl2IGNsYXNzPSJsb2dvLWltZy1yb3ciPgogICAgPGltZyBzcmM9ImRhdGE6aW1hZ2UvcG5nO2Jhc2U2NCxpVkJPUncwS0dnb0FBQUFOU1VoRVVnQUFBVU1BQUFEckNBWUFBQUR6Qy9Rd0FBQUJXR2xEUTFCSlEwTWdVSEp2Wm1sc1pRQUFlSng5a0xGTHcxQVF4cjlXcGFCMUVCMGNIREtKUTVTU0NybzR0QlZFY1FoVndlcVV2cWFwa01aSGtpSUZOLytCZ3YrQkNzNXVGb2M2T2pnSW9wUG81dVNrNEtMbGVTK0pwQ0o2aitOK2ZPKzc0emdnT1c1d2J2Y0RxRHUrVzF6S0s1dWxMU1gxakFTOUlBem04Wnl1cjByK3JqL2ovVDcwM2s3TFdiLy8vNDNCaXVreHFwK1VHY1pkSDBpb3hQcWV6eVh2RTQrNXRCUnhTN0lWOG9ua2Nzam5nV2U5V0NDK0psWll6YWdRdnhDcjVSN2Q2dUc2M1dEUkRuTDd0T2xzck1rNWxCTll4QTQ4Y05ndzBJUUNIZGsvL0xPQnY0QmRjamZoVXArRkduenF5WkVpSjVqRXkzREFNQU9WV0VPR1VwTjNqdTUzRjkxUGpiV0RKMkNoSTRTNGlMV1ZEbkEyUnlkcng5clVQREF5QkZ5MXVlRWFnZFJIbWF4V2dkZFRZTGdFak41UXo3Wlh6V3JoOXVrOE1QQW94TnNra0RvRXVpMGhQbzZFNkI1VDh3Tnc2WHdCQTZkaUU4SFlXaE1BQUVId1NVUkJWSGljN1oxNWZGVkZzdmpyM0RYN0JvUWxRQWliS0FJK1VGQnhYMUFad0hGNUlpQlBIUmNlRGk3b3FQaFRSbEZBUWNWUlVaOFBVWEhVSitMbzRLNkFBczY0b09ER0loRENrb1JBOXZWdVo2bmZIMWhObjc3bkpqY1FJSUg2Zmo3NTNDWG5kdmZwYzdwT1ZWZDFOUURETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUV6YlFEdlNEVGhhY0xsY2dJaWdhUnBZbGdWdXR4dE0wd1JOTzdndVJzU29lZ0FBTE1zQ0FCRGwwM0dhcG9rMk1DMlB4K01Cd3pBTytycktJQ0s0WEM1eHplVDNETk5tSU9Fa28ya2FlRHllRmluZjQvR0EyKzJPV1U4c1dySU54em91bDB2MHY5dnRCcmZiM2FMbGE1b0dicmNiZkQ2ZitLNmw2MkNhaGpYREZzRG44MEVrRXJGOVJ4cWFxdGsxRi9uM1RscWcvSC9XS0E0ZGJyY2JFRkg4a1NYUUVxalgwK3YxZ3E3ckxWSTJ3eHhXVkEzTVNZczdVSHcrbjAwRGJFb2JkTGxjTFdyQ0hldDRQQjdSbjNLL3RxVFdyVjR2K3N6YUlkT21rTTBud3V2MUNzSFVrcWpDamw1bE0wNyt6RUt4WlpDdnJjdmxhbkVoSlpkSHBqSlBjVEJ0RWxVUXRqUWtCRlhoR3EvR3lCdzRkRDFwMmdOZy8vVnVpWWRkckxMNGVqSnREdldtOWZ2OUFHQTNydzYyZkxVY1dldVRKOTNwZjdIYXhod1lxaVlZeTZGMUlMaGNMcUVGVWoyeUFHWU9IOXpiTFlEUDU0T0hIbm9JTzNmdURKRklwRVhOS0RsY3h6QU0wSFVkNnVycW9LU2tCUGJ1M1F1Yk5tM1NTa3BLb0xLeUVnRHNUcFNXY09BYzY1eCsrdWs0YWRJa0FBQ0lSQ0xnOVhvaEhBNkQyKzBHeTdJT1dpZ2FoZ0VWRlJYdzhNTVBhK0Z3V0h6UHpqQ21UWktZbUFqcjE2L0hjRGlNaG1FZ0lxS3U2MmhaRnBxbWlZZ29YdW4vaEs3cmlJaG9XUlphbG1YN0gzMm1WL20zOHZ0SUpJS0dZZURhdFd2eGpqdnV3THk4UEVoTlRZM1NFdWt6dlI0T3pjT3BMbnBZMEt1VDlpc2pQMXpvMkVNeEordkV1SEhqUkQrcjExS0Zyb2w2amVWcktGOXorbjc5K3ZXWWtwSWlOTVREY1Y1TU5Ld1p0Z0ErbncrbVRwMkthV2xwb0drYUpDVWxRYnQyN1dEdzRNRnd3Z2tuZ0dWWnRtQnNYZGZCNi9VS3JZOCtrNlpCbW9mSDR4SGZtYVlwaEVJa0VnR2Z6MmZUVE9UM2htSEErdlhyNGFXWFhvSVZLMVpvQlFVRkl2UkhEcytod1BCRERZV0tPR21xOUoyc0NkRjVhNW9tMnFlR0ZjbS9QWlNjY2NZWk9ISGlSUEI0UE9EeitTQTNOeGNHRHg0TUNRa0o0aGpxUzFXZ3F5RXpkSzIvL2ZaYktDZ29BRjNYSVNFaEFZcUtpdURoaHgvV2dzSGdZVDAzeGc0THc0T0VCQXFaVFhRRGV6d2VPTzY0NCtDaWl5N0NKNTk4MGliTUFFQUlRSHF0cmEyRjFOUlUyTGx6SjJ6Y3VCSFdyVnNIUlVWRlVGMWRMY3BNU2txQ3JsMjd3cm5ubmd0bm5ubW16ZU9vYVpwTmlBYURRZkQ3L2ZEenp6L0QwcVZMWWZiczJacXU2N2I0dU1NeDJOUkJUU3M0NUw2ajcwM1RqR3MxQnMyWkhnNUJUbTBEMlBlUXljdkxnK09QUHg1bnpKZ0JKNTk4c2ppR2hKMFRkTzMzN05rRE45eHdBMnpac2tVckxDeUVTQ1FDaUdpTFU2VitvUE5tZ2NpMEtXS1pOYlI2NVBiYmIwZlROREVjRHR0TUxOa01ycXlzeEI0OWVvRFA1NFBFeEVRQTJDZElhREpkRHJsd3U5MlFrWkVCTTJiTVFFVEVRQ0JnSzlNMFRadlpIUWdFY04yNmRhaWF6b2Nqam8wRWlXeldxbUVqOG1jbkI1QzYra00xOXc4bGNoMnlvOFB2OThPTEw3NklvVkRJWmc3TDB4NnlTVjFXVm9iZHVuV0RwS1FrVWE3c0NDUEhHOE8wYVdpUXlzSkZuZzg3NFlRVFlOMjZkYlpCSXd0RjB6U3hyS3dNMjdWckozNURaWGk5WHR1OEd3MGdlczNPem9hMzMzNGJxNnVyMGJJc0RJZkRVZVZibG9XR1llRG5uMytPZmZ2MkJZQWpNL2pjYmpla3A2ZkRxYWVlaXVQR2pjUFhYMzhkdDI3ZGluVjFkVUo0UkNJUkxDc3J3dzgvL0JDdnZ2cHFIRHg0TUFMWVBlaXFrRHhjYlZmbk92djE2d2ZmZmZlZHJiOXB2cEFlU0lpSTRYQVlyNzMyMnFqekFJQ29lVUphbWtmdkdhYk40QlFmUmplMEhQN3kwVWNmb1dFWXFPdTZHRGcwV0hSZHgrcnFhdXpjdWJNb1EzNXRyQzZQeHdOcGFXa3djZUpFcksrdmp4SzRobUhZQnVxeVpjc3dLeXZyc0EwME9UN3l6My8rTTc3eHhoczI0VWRFSXBFb1I1RmxXYmhqeHc1OC9QSEhNVGMzMXlZa1dpcDBxU25rNjZnNmdkeHVOenoxMUZNWURvZWpuRjB5di83Nkt4NTMzSEUyamQvcFZkV1llUVVLMHlaeDhvaktOL05ycjcwV3BRMlNTV1dhSmxaV1ZtS0hEaDFFV1RLMG9nWEFXVU1DMkNkd1R6dnROQXlIdzFHbW1teStSU0lSWExkdTNXR2JpSEs1WE5DcFV5ZFl2SGd4VmxkWEMwMkoyb0s0ejRUOCt1dXY4ZXV2dnhiSDBQK29uOWF1WFlzbm4zd3lIb21WTmVvS0ZKbHJycm5HWmlxcm5tYkxzbkRGaWhWSWdoREFua1JEZmxqd01qem1xRUVObWdYWVAzam16WnRuRTBwcW1FWkZSUVcyYTlmT2NhVURRTFN3ZFhydjlYcGgvUGp4Mk5EUVlCdVVWQ2RwWE1GZ0VLZFBuNDZIdzh6TXpzNkd6ejc3VEFnM0VoQ2hVQWkvLy81N1BQUE1NMUVPUFBiNy9mRElJNDlnVFUyTnplUkVSQ3dvS01DY25CeFI5dUVVR3JLSkxEK1loZzRkaXJXMXRlTGM1TEFhYXZmU3BVdFJuak5WelcyQTZDeERiQ0l6UngzMDVKODJiWnBOR0toVVZGUUl6WkJvem9DZ2daYWFtZ3BMbHk2MXpWblJ3SlEvLy9EREQyTCs4R0NGaXF5NUVtNjNHeElTRXVDZi8vd25tcVpwRTRhSWlNODk5eHdtSmliR1hNbzRhZEtrS0xNL0VvbmdxbFdyc0RVNUcvTHk4cUM2dXRvbUJOWHJ1M2p4WWdTd081RlkrMk9PT1VpanUvZmVlMjJEUlIwd0J5b015YnNNQUNLczV1U1RUeFphaW1xMmtVblgwTkNBRXlaTXNHa3NWQ2Vab3MzVlRtU2hxR2thVEo4K1BhcCswelJ4eVpJbG1KMmRiZnN0ZWN2cFhESXlNbURSb2tXMk5wUEF1Zm5tbTF1TlFPemV2VHRVVlZYRkRNUkdSRnkwYUpFUWhrZkMrY1BFQjErUlF3eitudi91VU1XTVVkWmxpdGt6VFJQV3JsMnJGUllXMnNKUUtGYlA1L01CSWtKU1VoS01HemRPQkh2TFFjMTBiSFBicSt1NmlMZExUMCtITVdQR2lQb2prUWhZbGdXV1pjRVhYM3dCcGFXbHRyQWJpck9qN09EVjFkV3daczBhQ0lmRDRQZjdiY3NjeDR3WjAycnkvY25uckVMWHZxR2h3ZmFkL01xMEhsZ1lIZ1ZRRURQQS9nSDR3QU1QQ0FFanI0Q2hJR2pETU9DY2M4NFJHcG5xbUdqT1lGVmpDTDFlTHd3Yk5neTdkKzh1eXZINWZPQnl1V0RQbmozdzAwOC9nYVpwUXBDclNTY1FFUklTRXVDSEgzNkFQWHYyaUhMcDNEcDI3QWpaMmRtdFlsN05NQXpidzhRSmVXVUp3Y0t3OWNIQzhBaHdJQ1pvVTJYUktnZVh5d1UrbncrV0xGbWl5Y0tGNnFObGV4NlBCNUtUazRGaUcxWE50VGxtbkZ3K0lrSWtFb0grL2ZzRE9ZUklXQ01pMU5YVlFYVjF0WVpTdG1oYXBpaG5DdytGUXJCMTYxYXRxcXJLVm9lbWFaQ2FtaXJhZmFScFNxalJFcnpXSUxpWnhtRmgyTVlob1FLd3o4U2s3RGJCWUJBYUdocUVvQUhZci8zUkFMWXNDM0p5Y2xCTllYOGc4MWxrM3RJU09aL1BKMHgzZVJPbHpNeE02Tnk1TTFMOVRyR1VWRDlsY2FGekNJVkNBQURRME5BZ2xySWRhWnpXVEt2L2s5K3pVR3k5c0RCczQ2akpGdVRVVXNGZ1VBdyswczVJR0pJUVRVdExzNVZINXZUQnBJOVNOemFTTmNmMjdkdkRzR0hEeFA5bFFVN0pLK1NFRFhRTW1jNldaVUZEUXdPVWxKUzBDbytzVS9DM0toaGxnY2xDc2ZYQ3d2QXdvUTZRbHRKcWFGRS9RTFJnRElWQ1F2akpjMjV5OGdOZDEyMUpFV1RpRVRieTRLYkJIb2xFUU5mMUtDRk43UmcyYkJoUXZLRGNmc013UkFZWUFJQ2VQWHNpQ1dzUzBvZ0lPM2Z1aElhR2hzT1dxS0V4cUsyeHJpLzFDWDFtWWRoNllXRjRpSWwxODdma25HRXM3eStsbVNLdFVON0htWTR0TEN6VVNBTWpZU05yYTAwaEQzWjUwTmZWMVVFZ0VCQnRwSGt6eTdMZzRvc3Zodjc5K3lNSkVsa1RsUHZsekRQUEJGcWlLQXZ4Ung1NVJDUEJLdmREWXl1QTVHUGtQenBPWFNQc2xKTExpYWEyY1ZYYlJkZWdLYWNMYy9oaFlkakdJZk1SWUw5emhBWlpXbG9hR0lZaGdwa3BQWlJsV2VEeGVDQWNEa05GUllYTmhIWjZiUXpTK0tndEpJQysrZVliMkxObkR4aUdJVXhnZVQ3eG80OCtndFRVVkFEWUgyeE5tcUZwbXRDaFF3YzQ1NXh6SURFeFVXaXZpQWpUcDArSGdvSUNjYnc4eDBqdElNR21DblBTak9VL09vNk9wZmxKU2ljV3ovbXpVRHM2WUdIWXh0RTBUVGdXL0g2LzBNNHlNelBCNC9HQXJQWFI4UzZYQ3d6RGdHM2J0a0Z0YmEzNFh2WTR4d3Q1c1ZVQlZGQlFvTzNldlRzcUtGeDI0bnoyMldlWWw1Y0hjcDVGRWk1VHBrekJVYU5HaVgyRUE0RUFQUC84OC9ERUUwOW9zb0NYNXpmbCtWRjVpVitzZnFQNFRBQzdJMG8xYVJ2alVHd0F4aHdaV0JpMmNVaFlBT3p6dnRJQUhqdDJiTlRhWTFub3VGd3VlT3V0dDRTNUpzOGpOa2NZMFBFa2dBRDJtWm9ORFEyd1lNRUM0ZldWdFRUTHNzRHI5Y0pKSjUwRVU2ZE94YXlzTE51ODVZSUZDL0N2Zi8ycjBEcHJhbXBneG93WjhOQkREMmxVRGdWZGsya3RhM3B5bXhwck03VkxGb2prZ0pKTi9zYncrLzE0cUtaQUdPYW9nZ2JHM1hmZjdiZ21tVGpRNVhoMGpKb2d0YkN3VUN6SGs5Y2wweHJoNnVwcXZQamlpNk9XaWNsbHh1dEFrWmVZeVdhNjIrMkc4dkp5a2FVR01YcC9FTXV5OEo1NzdrR1h5d1VEQmd6QTlldlhvNjdyWWdsZVFVRUJwcWFtUnMwbk9xMkhsdHZzbEJBMlZzWWJ0YzFxZnpiR0thZWNnclcxdFk3WmFvakhIMzhjbmZxWGhXYnJnalhETmc1cEw2UUZlVHdlT1BmY2M3RnIxNjQySVVDYWtOZnJoVWdrQXUrLy96NTg5ZFZYR3BYaHBBM0c2NjJsY3Nsa0plZUdhWnB3NG9rbmFpVWxKV0lWaGl4Z0xjc0MwelJoenB3NXNHclZLdnpsbDErZ1g3OStVRjlmRDZ0WHI0WS8vZWxQMEtkUEh5MFlESXJ6SS9PWGxzR1JWcXhxd2ZMZU1UUlBHbXRKSkgwdngwbkdpOC9uTytDVk8wenJJcjdISDlOcUlmT1d6THFzckN5NDdiYmJBQUJzMzhzZTRsQW9CRTg5OVpSdHpTekFnVzFDUk1lcjI1TlNrUFhldlh0aHhvd1o4TlJUVDRua3BpU1laTzNyakRQT2dKS1NFbmp2dmZkZzJiSmxzSHo1Y3EyK3ZsNzhuMHhtVmFEcHVnNlptWm5RcDA4ZjFIVmRyTDJtK1VEU0tPbFBUa0toYVpxWWovenl5eTgxK2Z6akZZcWtvVHJGR3JMbTE3WmdZZGpHa2IyZlBwOFBSbzRjaVNOSGpoUWJUUUhzRDc4aHdYbjU1WmZEanovK3FLbEpIR1JoSUF1ZmVDRGhJVzlrUkVMNGpUZmUwTTQvLzN3Y04yNmMwRklwN2xFVzVqNmZEejc1NUJQNDVKTlBOSnJMbzNMcGxiNmpjalJOZ3drVEp1RDk5OTh2TmxhU25UYXk0SXdsbkRaczJBQVhYSENCbUhPbGN1UDFKak5IQjJ3bXR3TEkrYUR1b0NmUHhSRWVqOGVXR1prRVhVSkNBbHh3d1FXNGNPSENxUDFZS0V5a3Vyb2FKazJhQkY5ODhZVkdqZ0paMk1udnliUnNDcldOY2tnS0NUclROT0hCQngvVWdzR2c3UnpWZWJSMjdkckI3Tm16b1ZldlhxS3NXUE5ySkhBdHk0THE2bXJZdm4wNzdOMjdGK3JyNjhIdjkwTm1aaVprWm1aQ1ZsWVdaR1ZsUVVaR2h2Z3VQVDBkYUZ2WDJ0cGFxSyt2ajhvbUUrOURnTUthNUdrR0V2SnNNak9NUkZNT0ZISW8xTlRVWUZaV0ZnRHNFeUtxSTBBdWk5N1RYOCtlUFdIMjdObFJFL2VJKy9kQjJidDNMMDZhTkFuVDA5TmIzSHhUdzFUSS9BUUFTRTlQaDJuVHB1SFBQLzhzSERpMGc1OTgvcktENTdQUFBzUE16RXl4ck05cFh0QXBjQm9Bb0cvZnZ2RGdndzlpVFUyTktKZWNTSWo3c253SEFnR2NOV3NXL3RkLy9SY09HalFJZS9ic0tjcHRMTVcvRTJQR2pJbktMSzd1aDhJT0ZJYUIrTHpKaG1GZ1pXVWx0bS9mUHVhMm1hcTJSL05mVTZaTXdmejhmQXlGUW1oWmxranhUNElRRWJHNHVCaUhEUnVHNmtCdkNSTXYxcW9QVGROZzdOaXgrTU1QUDJBb0ZFSmQxOUd5TE55MmJSdFdWRlJFN2ROaUdJWXQ2ZTBISDN5QUNRa0pqbHQxeHRwcWxJU2ozKytINTU5L1BpcTdOeUppZVhrNW5uTEtLYWhwbWtnM0pwZEI3WmZYVnpmR0ZWZGNnWUZBd0ZZUEMwT0djYUFwWVVqZlZWUlVZS2RPbmNTa1AzbE5FeElTaENCTVNrcUN0TFEwNk5ldkg4eVpNMGVFckZCNmZIWGZrK3JxYW56c3NjZHNXYUZKQ0xRa2N1QXg3ZWs4ZS9aczJ4N09obUZnZm40K0FnRGNmLy85V0Y5Zkw4Sjg1TkFiRXBLQlFBQ2ZmZmJacUxZVHF0YW1mazVOVFFXNVQzUmR4OXJhV3J6a2trdFExbUxsMzhwbHhKdTVaOXk0Y1ZIQ1VMMjJMQXpiQnV4QU9jS1FBOFRuODhFOTk5eUQ1ZVhsWWxrYUpVUnQzNzQ5ZE96WUVYSnpjNkZ2Mzc2UWxaVmxXMUtXbkp3TUFQc0djQ0FRZ0xWcjE4TDI3ZHRoM3J4NThQUFBQMnNBK3dRV2hhTVFCK0k5ZG9LQ3VRRUFoZzBiaHZmY2N3K01IajFhcE8reUxBdFdybHdKSTBlTzFOeHVOOHlhTlV2THpzN0cyMjY3RFdRUE1QNGU5SXkvTHpHY1BIa3k3TjI3RjJmUG5xMVIyQXM1U2VRNVBUbGduRDdMQ1ZXcGp6Lzc3RFA0N3J2dk5KVG1OS2tmQU96TCtRekRFSytOMGRnS0ZCWjJEQ01SYjlDMXVvTWRhUmJxZmlueVo5bkVMQzB0eFZ0dXVRVkhqeDZOdWJtNVVmV1RXWDBvdkora2FWNTY2YVc0WmN1V3FCM2lWcXhZZ2JtNXVVTDQwTExCUng1NXhHYldxNXRHV1phRmdVQUFIMzMwVVpUcmNRcVNwbmhLaXEzTXlNZ0F1YXlxcWlyOHd4LytFQ1g1blRMMXhHc2lBd0RjZU9PTkdBd0dHOTAzbVRWRGhvSDQ1d3dSOTIzUzVDUVlkRjJQV3JraFF3S0ZBcTBKSnljTW1aMHRMUlRQT3VzczNMRmpoMmhMTUJoRTB6U3hycTVPN01Jbjk0Zlg2NFdVbEJSNDhjVVhiUUpSRnFKeXY5eDMzMzJvenBuSzI0dXE1M25paVNjQzlZMXBtcmh4NDBaVUhURzBEdHBwWlVxOFp2TGt5Wk50K3lhek1HU1lHRFFsREdsT3E3UzBGSWNQSDQ0REJnekFnUU1ING9BQkEvQ3NzODdDSjU1NEF0ZXRXeWNjRExMZ2tMWEVVQ2lFaFlXRm1KT1RZOU5zYUZBZnlvUUNLU2twc0hMbFNzZnp1dlBPTzhVYWFYbEpIYjNtNXViQ1J4OTlKSTUzMGc0Ujl6aytyci8rZW94MUxxb0Q1T1dYWHhabEJnSUI3TisvdnpoV3puUWovNVpvVGw5Tm1UTEZOdWZwcENHeU1HUVlpRTh6dEN3TEt5c3JvN2JQSlBMeTh1Q2RkOTRSeDhzQ1E5WVlEY1BBSjU5OEVsTlNVc1J2MVpnK2VlMXRjd2Rqck5DZUo1NTR3bEZ6TFNnb2lHdENjc0NBQWZqdHQ5K0tjMUNuQXVpNzB0SlN2T0dHRzRTR3FKNGJDYmpNekV3b0tTa1JRbXJldkhub2RGeEw4SmUvL0VYVTQrUlJabUhJTUw5enNNS1F0SmFoUTRlS1dEMG55R3RiVjFlSEYxNTRJYXJhVHF3a3BNMUJYc3BHZ2lndkx3L0l2Q2ROaklURHRHblQ0dmJPNU9Ua3dJWU5HMndlY1hvdkM4YXFxaXE4NVpaYmJNSk5GVFQzM1hlZk1GMDNiZHFFLy9FZi94RjFmRXNKb252dXVZZUY0VkVDcjBCcDVaQTNjODJhTmRxaVJZc0FFVzFlWVZTOHd5a3BLYkJ3NFVLUVExSjhQcDlZVVNJdjBZdG5NS3BKQ0tnK1doNTM3NzMzb2h5YVlsbVdNTlBYckZrVDF6bDZ2VjRvTFMyRmE2NjVCZ29LQ2tScUxYbkpIZFdaa1pFQnMyYk5ndXV1dXc3SllTSWYwN3QzYjdqaWlpdkE3L2REWFYwZC9NLy8vSTlZZWlqM1dVc0pJazd1ZXZUQXdyQ1ZJNXVDOCtmUDF4WXNXQ0N5UnRPYVhrcUtBTEJ2cy9adTNickJLNis4Z242L0gxd3VsOWlDMCtWeWdhN3JRb0JnTThOcVZBSGNvVU1INk5ldm4xaVNGZ3FGaEFDTFJDSWk3WDlUNTZmck91aTZEai8rK0tNMmRlcFVLQzR1dHAwM1NrdmRBUGJGRU02Y09STW1UcHlJOHJwcVRkUGdubnZ1d2Y3OSt3TWl3dHExYStIMTExL1huTW80bUEydlpHSk5ON0NBWkJpRmxwZ3psRWxLU29JVksxWkVsVUZ6ZHZRYUNBVHdnUWNlUUlCb0o0cWMyaXZlYzVEbkhNa0RlL2JaWjJOUlVaRllZU0tidG1WbFpYamFhYWMxS1cxVjc3ZW1hVEJzMkRDc3FxckNZREFvY2pMSzUwZnppSHYzN3NYSmt5Y2p0ZStaWjU3QnVybzZjVnkvZnYwY3IwVkxldEpuekpqUnFLZWZ6ZVMyQTJ1R3JSeDE3aThVQ3NITW1UT2h1cnJhWnJLcWdpMHhNUkZ1dXVrbU9QUE1NNFgyUkFIT2NuNi9lSkcxU05JcXUzZnZEaGtaR2VEMysyM2JDeUFpVUVMV3BpQnpPekV4VVp6SG1qVnJ0SFBQUFZjRWM2dkxFbW5Pc24zNzlqQno1a3k0K2VhYjhmenp6OGZKa3lkRFNrb0toTU5oR0RseUpQejIyMitPdjNYcXJ3UGxRQnhSVE91RWhXRXJoekxheUFOdTFhcFYybU9QUFNiMlBnRUFZUzVUTmhvQWdFNmRPc0hreVpOdGV5TkhJaEZibnIrbVVGZXBrUERDMzFlSkpDY25pMHcxdEdxRFlnRGpFWVowYnNGZ0VIdytuekQ5MTY5ZnIwMmNPQkZLU2tyRUNoSUErOGJ5THBjTE1qTXpZZHEwYWJCNDhXSnd1OTBRREFiaDFWZGZGWWxyNmJkMEhpMnRsWEVLcjZNSEZvYXRIRTNibjR0US9qeC8vbnh0NWNxVnRwUlljc0lDL0gwNTM1VlhYZ2wzM25tbmJRVUhDYTU0NWd5ZHdta0E5bWVjbG8raDdOWUErNFRFd0lFRG15eGZ6b3hOKzZWWWxnV0dZY0Q3NzcrdjNYampqZERRMENBY1NiSmppT3J1MGFNSFpHUmtBQURBbDE5K0NiTm56OWJvUVVHYUlKVXI1MzlzQ1lFWWF5c0JwdTNCd3ZBUWc3L250cVBCNXlTQTFNRWtDeDA2bnJROUtpY2NEc01mLy9oSExSQUlpTTJSQ0RuRHRjZmpnV25UcHNIZ3dZTlJMcjg1N2FmZnlKb1ZDUll5T1Vub3lHWGZlZWVkTnUxUURZeFd0d0JRNS9SdzN3NTYyc2tubjZ6VjFkWFpjaVU2ZWROMVhZZS8vdld2VUZSVUpQcFFiVDhKYkxtZGNodWQydFlZdE4xQlkzMm5PcXRVWnc3VE9tQmgySXFJMTdzcmF6bC8rTU1mb0txcVNndzQyVlFtZ2VEMWV1R05OOTZBamgwN0FvQTlzM1Z6MjBTL3BSM3daTE9aSENzMEo5bXRXemM0OTl4elJlZ050WWxDZkdTdFZrN25UMlZTa29jdFc3YkFoQWtUaEpBRDJDZXNaQzg2elNPKzhzb3JjTVlaWnlEMUJ3bHFlVzltOWR6azc2aU5hcUxkV0RRV290UlUvemJYbTg4Y1dsZ1l0aEpVN2FFcFNDdjc1cHR2dEVXTEZvbndHWG4vWXRuazdOdTNMMHlmUGgxcEh4Skt1OThjWktjRElrSitmajRVRlJYWkJHUTRITGJ0ai96NjY2L0RjY2NkSjlwTTU2bnJPdmo5ZmlINDVFQnVUZE9FbzRmNDVKTlB0RW1USnNHbVRac0FZSjhXU09kQTUrcDJ1MkhBZ0FFd2QrNWNPT2VjYzlEcjlZcjZhRHNBcDNPU0E4bmw2eERQUGlqTjBlNVlFMnpkc0RBOERNUWo2R0taVWszOXhySXNtRHQzcnJaMTYxWWhsR2lUZGRMVUtCWFlWVmRkQlVPSERrVktUZFdjb0d1MVBZZ0ltelp0MGpadjNpeUVpV0VZWWs2UDJ0S3VYVHU0OTk1N01UMDlQU3FISU8wNVFrS05ORVlTWHFUWkVjdVdMZFBXcmwwTEFQdFRrZ0hZVjljWWhnRkRodzZGdi8vOTczREZGVmVJM0lVdWwwdG96ZFIrMGp4bDg1L0twbkxqNlI4MU1EMWVXRE5zWGJBd2JDWEVJekNkaEpkbFdWQlJVUUVqUm96UTZ1cnFBQUNFcVVyL0oxSlRVK0dERHo2SXVhTmJySGJGYWtkTlRRMHNXYklreXV5bWVVU2FtN3YyMm12aDdydnZSam52SWptRnlMU251aWljUmphZi9YNC9aR1Zsd1R2dnZJTVRKa3dRMnFDOGQ3SmhHRFp2ZG5aMk5peFlzQUJ1dnZsbWxJV3EvSjQ4OWZJbVZyUU5LUW5LbG9EbkNCbm1kelJOZ3p2dXVLUFJ3Tnl5c2pJa2oyaGpjMUJPZjhURWlSTkY2aXpFL1FIS2lDalM3bHVXaFI5Ly9ERlNUc0htNEpRSjJ1VnlpU1FTY3BwOU5lczJJdUxDaFF2eHJMUE9RcWNOM1dVdnRWeVB6K2VEMGFOSDQ3Smx5OFQ1QklOQjNMQmhBNGJEWVZ0QXRnb2QrL0RERDJQNzl1MEJ3TzRzVVpPNzB2dm1oTXM4L2ZUVFVSdklxMUEreHBaTUVNRXdiWko0aEdGcGFTbW1wNmVMNCtYZk5nVnBVcW1wcWZENDQ0L2J5bFVUclpxbWlZRkFBTysvLy82NGJEUXl0UnRyVjNaMk5uenp6VGMyd2FkQ2dtemJ0bTM0d2dzdmlCeUg2dm1SSUVwT1RvWlJvMGJoNHNXTHNhU2tCQTNEd0Vna2dxWnA0c3laTTNISWtDSDQ3cnZ2Mm9TOUNnbG4welR4ODg4L3h6Rmp4dGdTTmxEOFljK2VQZUhxcTY5R1ZRakdJeFQvOXJlL05Tb01MY3ZDMmJOblJ3bEQxaEtaWXhZbllTZ3Z6OXU3ZDI5TVlkall3RkdkQWprNU9mRE5OOS9ZRW82U1lKRHJMQ3dzeFBQT082OUpnUmhMV05ILzVIVC9SVVZGdGwzdVpJRWtaNTR4VFJOMVhjZWRPM2ZpNDQ4L2ppTkdqTUFoUTRiZ09lZWNnM2ZjY1FkKy9mWFhXRjlmTDQ2VGhmcWdRWU5zS2J5KytPSUxXejFxLzlKNTA0Tmd4WW9WMkx0M2IvRDVmT0QzKytHdXUrN0NjRGlNNFhBWTU4eVpnN0lUSng2ZWV1cXBSalZUeTdKdzFxeFpMQXdaaGdURzlkZGY3emhvNlgxWldSbW1wS1E0Yms0VWJ6MzBldm5sbDJNNEhCWnJlSjJFb3E3citNNDc3MkJxYWlvQTJMT3ZOR2ZES0hsUWp4dzVFci8rK210SFRTbldlY3VDVHY1ZU5yZDFYY2VQUHZvSWh3d1pZaFBlSG84SDJyZHZEMHVYTHNWQUlHQ3JUMDBTSzdkSm5qNmd6OTkrK3kzbTVlV0pqRHV4aEpXYW9IYmV2SG1PMjdQSzM5MXh4eDAyWWVpVWdaeGhqaGttVEpoZ0crU1VqaDV4WC82LzB0SlNFZlpDeEN1VTFPTjhQaDlNbmp3NVNnQ29nc0EwVFh6eXlTZnhZQWVuM080ZVBYckFyRm16c0xxNjJxYlpFZkpuZFU1VEZhSzZydVA2OWV2eHBwdHV3czZkT3dPQTg3eHByMTY5NExISEhoTjlTZWNxNzVkTWZTOS9wanlFbjMzMkdRNGVQTmltY2Nyem1yR21CelJOZ3llZmZGTE14OHJJZmZ5blAvMEo1WWNOZWRBWjVwaUN0dm04K3VxclJRWVdlYkRRZ0ttcXFrS0FmY3ZORGpSRnZ4eGM3UGY3WWNHQ0JWSENrT3FtdGxpV2hiZmZmanZLQTVWUWw3NDVRZlhKcjM2L0h3WU5Hb1NmZnZxcFkzMHk2aWJ5MUQ4N2R1ekFLVk9tWUU1T2pxMGY1WE9WKzlqcjljSUpKNXdBQlFVRnFDS241WmY3d0xJc25EbHpadFIrMVU3OUdtdVYwTk5QUHgyVm5Wc1ZpQmRjY0lIUWFPa2NEdVUyREF6VGFuRzVYSER6elRjMzZsd29MUzFGZVlBY3lKeVNhb1lOR2pRSXQyelo0aWdFNkwxcG1yaDE2MVljT25Rb3FpbSttb3ZjWnZwOSsvYnRZZnIwNmJodTNUb3NLQ2pBb3FJaXJLeXN4SnFhR3F5cHFjSHk4bklzS3l2RHpaczM0NlpObS9EVlYxOUZpb1ZzN0J6bCtzaTBkYnZka0pLU0FoTW1UTUIxNjliaHRtM2JzS3lzVEFqRVVDaUVaV1ZsV0ZSVWhNdVdMY096empwTE9GUmtJUldQUjVuYThlS0xMMFk5MkZUNjlPa2pmdGVjbmZlWXd3dlA0aDVpS01ENWlTZWV3THZ1dWdzQTltZGNvVmZETUtDeXNoTHk4dkkwU25RS0FDS2hRRk5RV2VyeEhvOEhycnJxS3B3L2Z6NWtabWFDcnV1T0dvbGhHTEJxMVNxNDVwcHJ0SXFLaXFpMXp2SFU3ZlY2UmZJSGRiOWhXZytjbHBZR3h4OS9QSGJyMWcwU0V4TWhNVEVSYW1wcW9LaW9DTFpzMmFMVjFOU0FZUmdpUGxGZUJ5MURnZGwwblBwL3FqOHZMdytHRFJ1RzNicDFFOWwxZHV6WUFmLzYxNyswSFR0MkFNRCtaWVQ0K3dvV2lwR1UxM2VyeU4rLy9QTExlTzIxMXdyaHFDN2owM1VkTWpNenRWQW9GSFV0WTVYUE1FY3RMcGNMM24vL2ZhRUZxazRDUk1UNitubzg5ZFJURHpvZWpiSk9FMzYvSHg1NjZDRVJqeWVIbTZnT2x1Ky8vOTQyTXB1VHJDRFc5MDJkaXp4UHA1cnE2dnljYXJiVG5KNnNFYXZKSUdSa0FhdkdHOHAxcXRwbnJEbERsOHNGcjczMm1zMVpJNXY3MXUvN1Bxc2FPOVhQRGhUbW1PT3NzODdDalJzM1JwbFA4aHhhT0J6R1YxOTlGUS9VbTZ3S0pEV2I5U2VmZkNLRXNTcUk2ZnR3T0l6NStmbllxVk9uWmdVZWt6Q1JQYTJxbWFzS29sZ0NWQlpHc2JZNVZSMCtMcGNyNmhoVmNNb1BDVmtBcTFNVDh1b1hwLzJZNWZkcGFXbnczbnZ2T2M0RjArZlBQLzhjR3hQUURIUE1NSFRvVUZ5eVpJbHRvRGlGa3BpbWljWEZ4WGpycmJkaWN5YlhuZWJwbk9qUW9RTzgvLzc3R0FxRmJDdFUxRUVjaVVSd3c0WU5PSG55Wk96ZHUvY0JuWE5qcTJTYzJ1czBSK2YwRzZmMFdySldwem9uU0FpcDJhNmRrUGQybGw5ai9kN2o4VUNYTGwxc2NZN3lLL1hucUZHalVLMlhONUZxbmZBVmFRRzhYaTljZXVtbDJMbHpad2dHZzVDWm1RbnQycldEM054Y0dESmtDSFRyMWcwU0VoS2k4Z0hLcWExb3ptcjM3dDN3M1hmZndaWXRXNkN5c2hMcTZ1ckFzaXdvTEN5RXp6Ly9YSlBuNHVLZGM2STF3TjI2ZFlQcnJyc09IM3JvSWZGN1dzOUxoTU5oOFB2OVlCZ0dyRnUzRGdvS0NxQzh2QnpxNit2Qk5FMG9MUzJGMTE1N1RhdXZyN2ZOQ3g2dDBEeWluQkNXK3Z6TU04L0V0OTU2Q3pwMzdpeldQTXZYdGFhbUJqcDA2S0JSdWpXZUgyU09hdHh1Ti9qOWZ0aTBhUk1HQWdIaE1aYk5YOWtVZFhxdm1xMjZyb3ZsWi9SKzBhSkYyS0ZEQjFGbmN5RXZwc3ZsZ3R6Y1hQajN2Lzl0MHd4bFQ3ZnFFYVcyQllOQkxDc3J3NzU5K3g2VDgxMmtjVkpmWG5ubGxXaFpsbGp0STEvVFNDU0M4K2JOdzFpZWVkWU1XeDhjK1htUW1LWUpQcDhQTm03Y0NKV1ZsVUp6SUMyQXNwK1FWaUdua0hLNzNTTFBucXc1VUV3ZHBad3lEQU8yYmRzbXZMeHkyVTFwWjJTbVJpSVI0ZkhkdFdzWERCOCtYT3ZkdXplTUd6Y09Uejc1Wk9qUW9ZUHdCSHU5WGxFMlpYYWhuSUdrSWNwN3JSek4rSHcrc1U4MTlUK2xQeHN3WUlCdzdKQ25ucTVaWFYwZGZQVFJSNDRhb2V5c1lXMng5Y0NQcDRORWpuV1QwMEhKa0FsRi95UEJLRzlzRHJCL2Jrb053VUJFOEhxOVVGOWZiNnMzM29IazFDYTVESHBOU1VtQjFOUlVNYWRGb1RLR1lVQWdFQkJDTVJnTXh0YzVSd24wRUtNSGcyVlprSmlZQ0pzM2I4YXVYYnZhMG9MaDcza1pGeTllRERmZmZMUFcwTkJ3aEZ2UE1JY1JOUnlEdnBQL1lrMmFOMmJ5eXM0Qkp5L3pnWmlxYnJjYmtwS1NvdXFSdmFqVWZ2bTk3TlFoeit5eFlDcXJqZy9pbW11dWNkd0gyelJOTEMwdHhSNDllckFwekJ4YnlLc2ZpS2E4aFhJOEhIR29NcHBRdTN3K242MWNOZUdBM0E1WmdNdW9udUZqUVJnQzJOY1NhNW9HSFR0MkJGcVBMSzkvRGdhRFdGTlRnK2VkZHg2cTE1QUZJM05NUUpvaGFVK3FOcWNLRUNjaG8rNGlSNExIYWVjMkt1ZEFCNWphSGxVd3h4THNzYlNrb3htMW56dDI3QWovK01jL01CS0pSS1VyS3k4dng5dHZ2MTNNWGNqM2c5TzFZZ0hKSEZXb2c2VTVua04xNVVOamRjaGxOWGNReGRMZzFISWIwL1RVT0x4alJTc0UySCt1NmVucE1HL2VQS3l2cjdlWnhaWmxZV1ZsSmQ1KysrMlltcG9hbCtCcnFyOFpobUVPTzJwd3VQclo2L1dDeitlRHI3LytXb1JLeVNGSnBtbmlpQkVqc0RGTm5tRVlwbFdqYW15eXRwYVltQWpEaHcvSHUrNjZDL2ZzMldQTGsyaFpGcGFYbCtQeTVjc3hPenM3YWtya1lLWXhHSVpoamhpeUZ0ZXZYejk0N0xISGNNbVNKVmhVVkJTVmg5RTBUVnl6WmcxT21EQkJiRFJGampSTzJzb3dUSnVFekdGWmt4czdkaXhHSWhHYlNXeFpGdXE2anF0WHI4Ymh3NGRqWm1ZbUFFUTd0UURzcTMyWXRnTS94cGhqR2dwY2x3UFlkVjJIaW9vS3FLeXNoRWdrQXNYRnhiQnk1VXBZdUhDaFZsVlZKVmFhVUJDNjMrK0hjRGdzUE8rUlNNUXh6eUxETUV5YndlZnpRWmN1WFdENDhPR1lsNWNuTWwrVCtSc3JwMkpqV1hZWWhtSGFERTRtTFNWZ2Rab0hsSU93Q2RuN3pEQU0wNlp3RW1ieHhHV3F2K0VFcmd6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1FY3ZSenpIa0xvdkxVRHpOanRxS2sxU2E5bDlMRlk3VzB2N21BT2pxZXQ2cEhmQWErNzRVSTkzYXJ0OERPMm9TTHMwSHVuelBSaU91REJVb2JXZTZnNXhoNG9qTFV4Yk11ZWRVMWx5KzUyeWFqZjMvT0laTEFkVFhuTnBxdjZXYWwrc2NscjcvWEU0QkpOVEh4M3VjZHdTdEFwaDZIYTdZZFNvVVdoWkZuZzhIdEIxWFd5OUtXZithTzd1Y3ZIUTFNM1dWSDQ2cDQyYzVETFZ6YUZpTGVvLzBQcnAvQnZiV2lCVzIrSVphT3J2MU44ZmJKcXFTQ1J5VUw5dktqTk1VL3RLTnpWWW5iWnRqZlZaM2krYlBqZFYvOEcycnluaUZZWk8yWHRpbmF0NmpLN3I4TkZISDJrMFhyMWVyOWhqdWkzUktsSjRVUm9rZ0gzWmhTT1JDTGhjTHRCMVhXeExTYlMxWGNkb3NEZFhDQjBzVklkbFdRZFZ0NU5tS1g4KzFKcEhXeks1bkFRSlBheGluVWRURC9QRG1TeldTZGc1V1JGT0R3RDVvVWlDVUo3NmFnc2NjVWxDMm9WVHB6WFdtVTJaTHkzWnZzWm9xdjVEYllZZmFUTy90ZE5TMXkvV2NRZGJmbE9hOWFIT2lYZ3dVeVpPcU5aY1crS0lhNGJ5QlZDRlgyT2QybElPbHFab2lSdmtZTXBvYVdIYzNMYW9nN1V4TS9GUWNMRG5mNmdmVm8zOVA1NTdyeW5CY1RqdjM2Ym1uSjBnazFnV2dyVG5kbE5UQUsyTkk2NFprZ0FranhRbDByUXNDMHpUUE9LYVQwc0lvNE1aTUFkNmZ2RnF6b2RhODIySzFxNjVIc3pENUhCWkw0ZVNlTzRQeXZTdHdtYnlBUkNQT1V3MEZRcWdjcVJ1eENNNUVPSXhmUTYwZlMzdFRXWU9MYzE1V0IzTXRTUmxoalRFdG13dXQybGNMcGR0SWxxZGRGWTMvQ0hjYm5mVUpqN3hiT3J1VkwvYUJxZXlEbVF5UEZhN25YRGFZTDZ4RGMzalBVYzFjN05zT3FzYklqV24zNXk4MGVwbTllcnhUdlU0WlpodWJBdFFLbC91Qi9xLzdKMlA5MXpVdHNxZjVYTFVkc2ZxeDFqbkVDL3lOZ1dOZWZ4YnUvT1JhU2J5UlhjU1JrNFhuSVNYL0ptTzkzcTlqb0l0Rms2Q1NoVVFOUEFPUmlpcTU5ZFVTSTM4T3pvZnB6Q2dwbEFITnVHVURwL2E1SGE3aFdDS1I2aTQzVzd3ZUR3eEJhdkg0eEY5S05lcENqQ3F6K2tZcDNOUWYwdnZLYktodVdGRGpaMkRYQmFkRDdWSHZTN3F0V29NK2ozVnBkWXBDM3dxbC9aMVlZNVMxRUhncERtb3gzZzhIbkhqTysxYkVjOWVGalRZWlUxRHJWK0ZidlI0Ym5oMXNNZzN1Q3dRYVVBNGFXZXg0aHpqSFJBK244LzJPNmZ0TVozK1I3OVIvOVMyeWRkRTduTlY0S3NidkFPQVRVaXEvZXIzK3gzYkpRdHRPbDZ0U3czcGFneXlLcHo2UlVVV2hDcXh0T1I0aUtVdEEvQ2VMTWNNVGhxVGt6blNtRmFrL2kvV0prRHhJQWRaazZiZ1pNYkZXNzU2SHVyZ1ZzOU5QUSsxYlU0RHR6RmltYXNKQ1FtMnRxbDdCZE5ucDkvRzBxWmxUWS9hcDJwYkFJMFBibldqSnZWQklndE5xa3VsdWVhK1hJOXFxWkMxNGRTUGNoK3BKbjY4OVR1WjkxUlhZOU1aNnYrWm80UjROUjNWWktDYnp1bEdiazdkVkI3OTNtbkFxUnBVdk1qbWoxcWVrOGFubWtyeG10UHh0TVBKZkhPYTQydEtFMm1zTGJFZUh2STFVNCtscVEyMVRmVHFKSXpraDZpcVVUc0owbGc0VFFlUTFxcWVxMnc5cUJ0SU9abjh6ZFhvWWdtL1dQM05tMWZaYWZNVEIwN2VLNWZMWlZzYWxaaVlDRU9HRE1HQkF3ZENjbkl5SkNRa1FIVjFOZno4ODgrd2NlTkdyYnk4SER3ZUQ1aW1LYUxwcWJ5bVBHSXVsd3Q2OWVvRmdVQUFrcEtTSUJnTVJqMmxBNEVBR0lZQlpXVmxVUXZiNDBFT08xSUQxSDArSCtUazVFQ2ZQbjJ3VTZkT0FBQ3dkKzllMkxadG0xWlVWQVNoVUVpY0M3VkgxM1hIc2hxRFBQN1V0NzE2OVlMZXZYdGpSa1lHSkNZbXdzNmRPNkcwdEZUYnRHa1RXSllGaUNnR29WTUVnSlBuVWw3UG1waVlLSlpsQm9OQjBWYjZyUnFCUU44bkpTVkJJQkNBbEpRVXNmb25Fb2xFSFUveGNmUTd1VTJwcWFtZzZ6b2dvaWdqbHFkVnZkZjhmci90R3BPSHRiRkVCajZmRDB6VHRMV0o3c1Y0SU1GdUdBWjR2VjdvMTY4Zm5uamlpZENqUncvd2VEd1FpVVJnKy9idHNIYnRXcTJvcUFoMFhXZFA3OUdNYkZZUlBYcjBnRGZmZkJPM2I5K090YlcxV0ZaV2hwRklCQkVSNitycXNLNnVEb3VLaXZCZi8vb1hYbmJaWmVMT2E0NW1rSkdSQWJ0Mzc4YWFtaG9zTGk3R3ZYdjNZbVZsSmU3WnN3Zkx5c3F3cEtRRVMwdExjZmZ1M2JoejUwNWN2bnc1WG5ycHBSanZKTGFxTWRENXRXL2ZIbDU2NlNYY3VYTW43dDI3RjJ0cWFyQyt2aDdyNit1eHRyWVdTMHRMc2JDd0VGOTY2U1hNeXNvQ0FPZTVyYWFRemE3TXpFeFl1SEFoYnQ2OEdjdkx5N0dpb2dJRGdRRFcxZFZoVFUwTmxwV1Y0YTVkdS9EbGwxL0d6cDA3QzNQWGFaNVExY0xrNjNibGxWZGlhV2twRmhVVllXRmhvYmcyVHVhZjJrOWp4NDdGb3FJaTNMbHpKNWFVbE9DVUtWT1E1di9VK1VtNVBmVDczTnhjS0N3c3hKMDdkMkpSVVJHU2dHN3NXc21hN1BMbHk3R3dzQkFMQ3d0eDl1elo2S1RseXU5UE8rMDAvTzY3NzdDOHZCeFhybHlKSjUxMFV0UjkyQml5QmZELy90Ly93eDA3ZG1CeGNURTJORFJnS0JUQ1lEQ0k0WEFZZzhFZ0ZoY1g0L3IxNi9HQkJ4N0E1c3lKTW0wRXVwRmxjeU01T1JuR2p4K1BvVkFJVGRORXk3TFFzaXdNaDhOWVVWR0JlL2Jzd2VycWFqUU1BeEVSRGNOQXk3SXdQejhmYzNKeW1sVi9VbElTSUNLYXBpbktJWFJkUi9sL2hHVlorTU1QUDJDUEhqM2lxa1BXTkJNVEUrSFBmLzR6MXRiV29xN3JhSm9tQmdJQjNMTm5EK2JuNTJOK2ZqN3UzYnNYZzhFZ21xWXBoUCtNR1RPd2ZmdjJCN1MvYjJabUpreWRPaFV0eThKUUtJU0lhS3R6MjdadHVIdjNiZ3lIdzZLL1E2RVEvdmQvL3pmNmZMNUdrMVdvNXI3YjdZWXhZOGFJZnJRc0N5c3JLN0YzNzk3aXQzSTU4bm4wNjljUHFxdXJiZjEvenozM29OUDVra0JTcHkrNmR1MEtWQzhpWWl5bkZLRjZpWC8rK1dkeHpVM1R4RWNmZlJRek16TUJJSHBlRldDZk1Dd3NMTVJBSUlCYnQyN0ZRWU1HTlVzWWVqd2V1T2lpaS9EWFgzOFYxd1VSTVJLSllIbDVPZTdac3dkcmEydXhycTVPZkkrSVdGWldodVBIaitjZ1VZa2p2aHp2WUNFVGgweW9wS1FrZVBEQkIvSE9PKzhVWnNiR2pSdmh3dzgvaEpLU0VpZ29LSUJJSkFJZE9uU0EzcjE3UTc5Ky9lRGlpeStHdExRMCtQVFRUOFVpODNoTldicGhLZVBPdkhuendEUk5zQ3hMbUdZcEtTblFwVXNYdU95eXk4QXdEUEI0UERCbzBDQll1SEFoamhrelJnc0VBcUJwbWlqRE1JeW9QSTh1bHd2OGZqL01uRGtUcDA2ZENycXVnOGZqZ1E4Ly9CQldyMTROMzMvL1BlemF0VXZUTkEyNmQrK09nd2NQaHVIRGg4UG8wYU1oRkFyQjlPblQ0WVFUVHNBcFU2Wm81ZVhsb2t5NW5RQVE5YjVqeDQ0d2QrNWNIRHQyck9qanQ5NTZDOWF0V3dkcjFxeUI0dUppVGRNMDZOYXRHdzRaTWdUT09PTU1HRFZxRlBqOWZwZy9mejRNSERnUUgzendRYTJzckV5WWlMS1pLeS9oUWtUUkxsbHpURTFOaFJkZmZCR3Z1KzQ2cmFTa3hOWS8xS2JNekV4NDlkVlhNVDA5WGR3VDhoeW5iR0pybWlhdU05VlA5NHFxc2RKMWtmOHZ2NmZwQi9xT3pHcTZsdE9tVFlPRWhBU2NPM2V1VmxKU1lxdVRIaEkwTFNEWHF5NXpvMWM1STR6TDVZSXJycmdDLy9hM3YwR25UcDFBMTNXb3I2K0gvL3UvLzRQZmZ2c050bTNiSnFadmV2ZnVEVDE3OW9RUkkwWkFYbDRlRkJZV1FsRlJrZTFlbHU5NURwcHVnNmczN3cwMzNJQ1ZsWlhpNlR4djNqdzgvdmpqYlpxQlBNR2RscFlHSTBlT3hERmp4bUJTVWhJQU5NL0xscEtTQXFacGlxZHVSa1lHQU95ZnRQZjcvWkNRa0FBZE9uU0FvVU9IWWlBUUVFL240dUppUFB2c3M4WFRXWTJ0VTVrMWF4WmFsaVcweklrVEoyS0hEaDJpdk9IMGw1bVpDUTg4OEFBMk5EUWdJbUk0SE1aSEhubkVVUnR3TW1VQkFLWk9uWXFCUUFBdHk4S2FtaHFjTVdNR2R1alFJZW8zSkhpeXNyTGdwcHR1RWxwNUlCREFtVE5ub3F5Qk9abWQ4dWMvL3ZHUFFyc3pEQU1OdzBEVE5JV1dwenE2TkUyREYxNTRRUnhMNTJxYUprNmJOaTFLMDJwTTQrcldyUnVnaEpOVzYvU2VZaXZYclZzbjdqMjZWb0ZBQUo5OTlsbE1TVWtCZ0dnemVjZU9IWWlJbUorZmowT0dEQkh0amFYRnkxcHNWVldWMElLcnFxcHc4T0RCb2g3WksrOXl1U0FoSVFGT1BQRkVlUFRSUjdGbno1N0NvUk1ySE90Z25XN01ZVVllWkRrNU9mRFZWMStKQWZIMjIyOWpVbEpTVkRBdFFIeWU0M2lFWWxwYUdwQkpaWm9tcHFhbVJ2MVdEdFZadEdpUk1OdXJxcXBzcG9vOFNOUzYrL1RwQTcvODhvc3czLzc2MTcraUdyeXIvcFplUC9qZ0EyRStOVFEwb094OVZiM09oS1pwa0pLU0FoVVZGVUs0dlAzMjI2aitUZzFRZDd2ZGtKaVlDRE5uemhUVEJKczJiY0wrL2ZzM2VuNnhoS0ZzOXBtbWlYMzY5TEg5enVQeHdQang0N0d5c2hJTnc4QklKQ0tFUHlMaWZmZmRkMGlGb2Z6bjgvbGd3NFlOR0FnRU1CZ000dmJ0MjlHeUxORVB6enp6VE5RRDk5eHp6OFhpNG1LMExBczNiOTZNZ3djUFJ2aytwVDVWKzlubjg4R1NKVXN3RW9tZ1lSaFlYMTl2bXdlVTUwRzlYcS80TGVVSmpjV3hMQVRiZktDUmJPYWtwS1RBYWFlZEJpNlhDMEtoRUN4WnNnVEM0YkM0d09Gd1dBUzl1dDF1U0VwS2dvU0VCTWpLeW9MazVHVEl5c3FDdExRMEVTWVRqNWxBcGhJQVFEZ2NGZ05lRHJPaC8xOTQ0WVY0M25ubkNWT3RwcVlHZHU3Y0tjcVM4OENwZFo5enpqbVlsNWNIbXFaQmZuNCtyRnk1VWlTem9IYkliWkw3NXFxcnJ0S29QUWtKQ2ZDZi8vbWZLSHZlQWNDV0ZJUE12cUZEaDJKV1ZwYnd4dDV4eHgyYWJCYktwaTBOTXRNMFFkZDFXTFpzR2V6WnN3Y1FFWHIwNkFFREJ3NUU4b2lyN1cyTUgzLzhFYVpNbVNLOC9CczJiTURqano5ZVBOZ3lNakxneGh0dmhPVGtaSEM1WFBEVVUwL0J4eDkvTEs1SGMrbzZFTWc4eHQ4OXo2RlFTRmdldDl4eUM3ejY2cXZDbTN6cnJiZkNzODgrYTlQbXcrR3dNTFhwbkdTdk4vV3BuTGlFZ3VCNzkrNHR3b3FtVDU4dVRIU2Z6d2VJS014ck9vYnU3YVNrSk1qTXpJUjI3ZHBCY25LeTdYeU9WVUVJY0JUTUdaS3crVjJUUVlCOUZ6UVNpY0MvLy8xdlRSWVdkRk8rK2VhYjZQUDVSTFlOQ2lCMnU5MVFXbG9LRHp6d2dMWnIxNjY0NmcrSHd5S3NJVEV4RVdiTm1vVTBEMFJhaGR2dGhuYnQyc0hnd1lPaGE5ZXVJdVNpb2FFQmZ2MzFWNDNhaDBxSUIzMFBzTStKa1pLU0FwWmxRWEZ4TVd6ZXZGa0lKZ0I3S2lqMW5NUGhNR3pidGczNjllc0hMcGNMemp2dlBIanp6VGZGZkpWYUY1M1BpQkVqd0xJczhQbDhVRlJVQkRUblJZS0pIZ1F1bDB1RW9uaTlYakFNQTM3ODhVZXR0TFFVdTNidENuNi9IOUxTMG15L3d6akRSckt5c3VDMTExN1RUanp4Ukp3MGFSSzQzVzZZTTJjT1huLzk5Vm80SElaNTgrYmgyV2VmRFpabHdVOC8vUVF2dnZpaTl0aGpqeUgrbmszbGNBeHVFbGgrdngvOGZyOHRZY0hOTjkrc1ZWWlc0dFNwVXlFU2ljQ0VDUlBBNS9QaGZmZmRweFVWRlVGQ1FvSUlyU0ZCSjRmaHlBOHR1bDZSU0FUNjl1MHI3Z2VYeXdYNStmbWlQU1FVWFM0WFhIbmxsWGoxMVZlRDErc0ZSQlIxVVNqWjNYZmZyVzNjdUZIOE5sWjQyckZBbXhlRzhvUnZZbUtpdUdIb0J2WDVmQkNKUkd3WDlweHp6b0hPblR1RHJ1dmlKZ0hZZC9QOTl0dHZrSktTSXI1dnlvbENUM1BTdkc2NjZTWWhoRlJuQUVGQ2NzeVlNVnB0YlMwQTJEVU1Fb3IwV1c0THhRbEdJaEZ4UTFQYlNkdVFKL3hKS0ZPOEdUMEVaSUVrTzAxSW85QTBEZExUMDIwVCtqU1lJcEdJVFpCU1dTUVVxUTAwY0gwK1g1U2podnFzcWY2dHJhMkYrdnA2bUR0M3JqWnMyREFjTUdBQVhIVFJSVEIrL0hpc3FxcUM4ZVBIaS9hZWZ2cnBHbWxOY3A4ZVN1Z2NhUDR0SEE3YkF1VXR5NEo3NzcxWHk4akl3QnR1dUFFTXc0Q3JyNzRhRUJHdnUrNDZqYTRWUGFncDVJcnVBeXFEN2duNlRIR0lkSS9SOTNJNkxVU0VidDI2d1dXWFhTYmFxenJPWnMyYWhZaW9BZXdYZ01jcWJkNU1sdWZNeXN2TE5mSTBoc05odU9TU1M5QnBqNDFmZnZrRlZxOWVEZDk4OHcwc1g3NGN0bS9mYnJ1eDNHNDM2cm9lbHpkWnp1Tm1HQVpVVmxiQ25qMTdZUGZ1M1ZCV1ZnYkZ4Y1ZBWlZtV0JZV0ZoYkJ3NFVMbzNyMjd0bTNiTnNlNVBoVmQxNFc1YjFrVzlPalJBL3IyN1l0eSswaUl5WUtRU0VoSWdPN2R1NFBYNndYVE5PSExMNyswcFdwWGhRZVZzV3JWS2xGdVZsWVdaR2RuQzYyRGhDd2REN0EvZE1UajhjREFnUU94VTZkTzRQUDVRTmQxS0M4dmp6cS9lUHFYaFBEMjdkdmh5U2VmRko3a21UTm53cUpGaTBEVE5LaXJxNFBMTHJzTVFxR1EwTERVUDVtV0hQQ2tCWnFtQ2FGUVNNenZCUUlCMFhjQUFKTW1UZExlZnZ0dGNiOU9uRGdSWG4vOWRjekp5UkhYbHN4WnVYMXlzRHlaeXk2WEMycHFhaUFVQ2dudjh0Q2hRd0VBaEFjYVlOOTFMUzB0aFZXclZzRVhYM3dCcTFhdGdpMWJ0b2hycHk0cW9MTHAvYkVzR05zazhzMmVrNU1EMzM3N0xaSjNkL255NWRpMWE5ZW9ZMm1laE15YXUrKytXMHlZNStmblk2OWV2ZUplTzV5U2tpTGlERTNUeERGanh1RG8wYU54NU1pUmVPR0ZGK0lGRjF5QTc3NzdybkI4ckZtekJvODc3cmlvQ1hGcW4vcEs3MDg5OVZUY3VuV3JjQ2JNblRzWDQwMGs4ZlRUVDR2NmEydHJNVFUxTmU2QThuQTRMQndBYytiTUVVSEVhbnlkN0xSSlNVbUJ1WFBuWWpBWVJNdXk4SmRmZnNHVFRqb0paZTAxWG0veTZ0V3JVZTZudi96bEw4SlRpNGdZQ29WdzNyeDVTTmMwTlRVVjNuNzdiZkg3KysrL1A4cnAweGdINmswRzJIYy9rWk1yR0F6aVJSZGRoQUQ3SFdNSkNRbnc5Tk5QSTkwdnVxN2oxMTkvalhWMWRXaWFKdTdZc1FQUE8rODhsUHRTcm9lbVhBRDJhZk5MbGl3Um52YlMwbExNeTh1TE9oK2Z6d2VKaVlsaVR2ZW1tMjRTOGFtSWlNT0hEMGU1M01iT2oya0QwRVh6ZUR6d3dBTVBDQTliS0JUQ1YxNTVCWjNTSmRFTjZuYTc0Yjc3N2tQRWZlRUpXN1pzd2Y3OSs4ZGRkM0p5TXRCdkVSSFQwdEtpNmpyaGhCUGcxMTkvRmQ3T3YvLzk3K0k0OVZocWszcHVtcWJCUC83eEQ0eEVJcWpyT2dZQ0Fiem1tbXRFVUhBczdycnJMbHZBN2EyMzN0cmtiMlFlZnZoaE1lQnFhMnR4MHFSSllsNVdicXU4VW1iOCtQRllYVjJOcG1saU1CakVaNTk5MWlaVTFNR3VsamQ2OUdnaDdMNzg4a3ViQU5ZMERaNTU1aG5idzR1Q21qMGVEL2g4UG5qbm5YZHNYbmY1Zk5SK1ZoOUllWGw1UWhoYWxoVWxET1cycW5nOEh2anBwNThRRWJHK3ZoNUhqQmdSMWRlWm1abncvUFBQbzY3cklxcUEycnB0MnpZOC8venpvN3pmVGhsM3ZGNHY1T2JtaW52UE1Bd3NLQ2dRRDFvWk9TR0VrekJzNnJ5WU5nYmRBQWtKQ2ZEcHA1K0tBWXk0TDlwKzFLaFIyS2RQSCtqU3BRdjA3dDBidW5idENzY2RkeHljZnZycFNEY3dEUzQ1QnFzcE1qSXlnRzdzWURBb1l2QUlldXBPbmp4WmhQem91bzYzM0hKTGxBWUFFSHR4UGczS3JWdTNvbUVZR0E2SEVSSHhuWGZld1VHREJtSDM3dDBoT3pzYnNyT3pvV3ZYcnRDM2IxOVl1SENoMEZpRHdTQXVYcndZTzNic2FHdGJVK1RsNWNISEgzK01obUVJVGUrRkYxN0FQbjM2UUxkdTNTQXJLd3M2ZHV3SVhidDJoVUdEQnVHNzc3NXIwM3pXcmwzcmFHdXBRbFJtekpneG9wOCsrK3d6bS9EMWVyM1F1M2R2K09LTEwzRHo1czNZcmwwNzBjZmtrWDNublhlRTFraHhobkxZaVpNUUprSGJzV05IUUVSeHJlSVJodkxEK0tlZmZzSklKSUtCUUFBdnZ2aGlzZnBGL2sxcWFpcTgvLzc3NHY0a1NrcEs4T0tMTDBhS1dYU3FUMDJJTVhueVpBeUh3MkwxVTBOREF6Nzg4TVBZcDA4ZjZOV3JGM1RwMGdXNmQrOE9PVGs1Y1BiWlorTmJiNzBsNmpWTkU0Y05HeVlzak1hbUZwZzJnanF3MnJkdkR5Ky8vREtHdzJHMExFdkVxRFUwTkdCaFlTRnUzcnpadGs2WmJvN1MwbEo4K09HSHhRQ0xoOS9ER01UZ1RVNU9qdm90UFptZmUrNDVSRVJ4ODk1NDQ0MG9hMzdxT2NrYUk1MWpjbkl5L08vLy9pK1dscGFLUVdRWUJtN2R1aFZYcmx5SlgzMzFGZTdZc1VNSUk5TTBzYWFtQmg5NjZDR2JHZFhZRWpPVi92Mzd3L3o1ODIxTC9BekR3UHo4ZkZ5MmJCbXVYcjBhOC9QelJVeGdPQnpHN2R1MzQzUFBQU2NFb1dxR09Ra1VPdGZSbzBlTDY3SjA2VktrOEJEQzQvRkFWbFlXZE9uU1JYd20vSDQvdlAzMjIrS2FQdlRRUTFIQ21FeEdlWDA0emN2bDVPUUlZV2haRmpyMWsycEt5dGVJSHF6aGNCalBQLzk4bEkrVmowdFBUNGRGaXhhSmV4UVJzYkN3RUMrODhNS1lmZVpVZjFKU0V0eDIyMjFpQ2FhODdMTzB0QlMzYjkrT2hZV0ZHQXFGeFA4b2dIN3AwcVdZbTVzYjgrSEF0REdjQnJUZjc0ZlUxRlFZUFhvMHJsaXhJdW9KTEdzdWlQdUNncWRObTRaRGh3NFZRYkh4a3BHUllRdTZWalZEbVlTRUJOdDhWbVZsSlY1enpUVml3TWdyWTlTZ2JabTB0RFFZTm13WXpwa3pCM2Z2M2gxMWJ0U1duVHQzNG9NUFBvakRodzlIT1o2c3VhbWJTQkNOR0RFQzU4K2ZqMlZsWlRidGdnWVk0ajVUNy83Nzc4ZFRUamxGSktOUXB5Ym9zMU91UXJmYkRXUEhqaFdDOWYzMzN4Y2F0T3lnVWR0R3h5UWxKY0Y3NzcwblZxRGNkOTk5S0Nlb2NMcGZaSUdYbTVzTHRQNDZIbUZJK0h3KzhQbDhzSGJ0V3FHaFhYTEpKV0tlVkUweTRmVjZJU3NyQzE1KytXVnhINWFVbE9EbGwxOXVXMlZEOVRrOUxLbnZFaElTWU1pUUlmamNjODloVFUyTkVPVHkrbTU2V05mVjFlR0NCUXZ3dlBQT2kxcTlwSjdqc2FZZHR2bXpsY00xNUhXYkFQdERCVFJOZzZGRGgrSnBwNTBHUFhyMGdOcmFXaWd1TG9aZmZ2a0Zmdjc1WnkwU2lkZzhzZko2NEhoU2VJMGJOdzZycTZzaElTRUIvdm5QZjJweU9pYnk0Rkk1ZnI4ZnNyT3pRZGQxOFB2OUVBd0dvYXlzVEhna0FmYmQzT1NacE8vVUhjaGtFenM1T1JtNmQrK08zYnQzQjh1eVlQdjI3ZHF1WGJzZ0VBaUk0OVhZUHRscjJOVDVxV0V4THBjTDB0UFRJUzh2RDd0MjdRcVJTQVNLaW9xMHJWdTNnbUVZNGppS0FZMVZoOXdtK1gyM2J0M2c5Tk5QUjEzWFlkZXVYZkRERHo5b1ZEOGRwNGJweUcwOTlkUlRzVXVYTHVCeXVXRExsaTN3eXkrL2FHcTlGS0lrcDgyaU9jZExMNzBVNit2cklUVTFGZDU4ODAwTjQvU3ErdjErNk55NU15UWtKRUE0SElhS2lncW9yYTJOdWZZYllKL3dQdU9NTXpBNU9SbENvUkI4Ly8zM1dtMXRyUWhmb3JybE9NUEcrdEhyOVVLdlhyM2c5Tk5QRjQ3QW9xSWkyTHAxSy96NjY2OWFhV2xwMUNidk5FN2tlNUJwb3pTbDZUaVpaaFNQRnN0WkVhODNXYTI3cWZXa3BDVTRyYStOOVpTV1RVUTVqaTVXTytTSmQvSmF5OXJWZ1NTeEpkVFVUNnFKMVZqYlZBMHhWdnRsMUcwYW5PcVZIUVN4MG9iRnlscmpWSjY2aXFneG5NeEtwK3VvSm0yVlBjWDBQOWt5b04rb1puWmo1eVgzdjJ4aXErYTJXazZzZTVWcGd6aE5NTXZlWWtLZGU2THZHak9mNGlWV212dFlYbUtuLzhzM3JteGFBZGpYVmF2dGRVb05GVXZnT0hrb20wSWVQUEtEd21tL0V2bTkwendoL1ZaZUhTSVBZdms5SFN2M3JTb3dpS2F1WDZ5NXNGZ1BzMWhseHFyWHFTNTZ3TW5PSDdWOWpUblJuQjZRc29ORFhhc3NseXUzUS9aQU81V25FbXV1a21ubHlEZU0rc1NUSjZ6Vjc1c2FLTTBSaEU0M21OTU5SZDg1RFRpbjlqalZvWllYU3pPZzM4ajk0eVJvbWtKMUZqVDIzdW1oSXM4UE9yVS8xcHljK3AwcVNKeWNTM0s4bzNwK1R2M25kSy9FK24wczFQNlBwWFU3aFhjNVhRdW5wQ0pOT2J2VSswcnRsOFkwZHFmN3N6RlBQOE13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd1RDdmkvd012dE9UN1h5eDJmQUFBQUFCSlJVNUVya0pnZ2c9PSIgYWx0PSJSJmFtcDtKIEdyb29taW5nIiBjbGFzcz0ibG9nby1pbWciPgogIDwvZGl2PgoKICA8YnV0dG9uIGNsYXNzPSJvcHQiIGlkPSJib29rQnRuIj4KICAgIDxkaXYgY2xhc3M9Im9wdC1pY29uIj48c3ZnIHdpZHRoPSIzNiIgaGVpZ2h0PSIzNiIgdmlld0JveD0iMCAwIDI0IDI0IiB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciPjxyZWN0IHdpZHRoPSIyNCIgaGVpZ2h0PSIyNCIgcng9IjYiIGZpbGw9InJnYmEoMjU1LDI1NSwyNTUsLjA4KSIvPjxyZWN0IHg9IjUiIHk9IjciIHdpZHRoPSIxNCIgaGVpZ2h0PSIxMyIgcng9IjEuNSIgZmlsbD0ibm9uZSIgc3Ryb2tlPSJyZ2JhKDI1NSwyNTUsMjU1LC41NSkiIHN0cm9rZS13aWR0aD0iMS41Ii8+PHBhdGggZD0iTTggNXY0TTE2IDV2NE01IDExaDE0IiBzdHJva2U9InJnYmEoMjU1LDI1NSwyNTUsLjU1KSIgc3Ryb2tlLXdpZHRoPSIxLjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPjxjaXJjbGUgY3g9IjguNSIgY3k9IjE1IiByPSIxIiBmaWxsPSJyZ2JhKDI1NSwyNTUsMjU1LC41NSkiLz48Y2lyY2xlIGN4PSIxMiIgY3k9IjE1IiByPSIxIiBmaWxsPSJyZ2JhKDI1NSwyNTUsMjU1LC41NSkiLz48Y2lyY2xlIGN4PSIxNS41IiBjeT0iMTUiIHI9IjEiIGZpbGw9InJnYmEoMjU1LDI1NSwyNTUsLjU1KSIvPjwvc3ZnPjwvZGl2PgogICAgPGRpdiBjbGFzcz0ib3B0LXRleHQiPjxkaXYgY2xhc3M9Im9wdC10aXRsZSIgZGF0YS1pMThuPSJib29rX29ubGluZSI+Qm9vayBPbmxpbmU8L2Rpdj48ZGl2IGNsYXNzPSJvcHQtaGFuZGxlIiBkYXRhLWkxOG49ImJvb2tfZmxvdyI+0J/QvtGA0L7QtNCwIOKGkiDQo9GB0LvRg9Cz0LAg4oaSINCc0LDRgdGC0LXRgCDihpIg0JLRgNC10LzRjzwvZGl2PjwvZGl2PgogICAgPHNwYW4gY2xhc3M9Im9wdC1hcnJvdyI+4oaSPC9zcGFuPgogIDwvYnV0dG9uPgogIDxkaXYgY2xhc3M9ImRpdmlkZXIiPjxzcGFuIGRhdGEtaTE4bj0ib3JfY29udGFjdCI+b3IgY29udGFjdCB1czwvc3Bhbj48L2Rpdj4KICA8YSBocmVmPSJodHRwczovL3d3dy5pbnN0YWdyYW0uY29tL3JqX2dyb29taW5nP2lnc2g9TVd4bWRITnFjWEZrYW5OdmJRPT0iIHRhcmdldD0iX2JsYW5rIiBjbGFzcz0ib3B0Ij4KICAgIDxkaXYgY2xhc3M9Im9wdC1pY29uIj48aW1nIHNyYz0iZGF0YTppbWFnZS9qcGVnO2Jhc2U2NCwvOWovNEFBUVNrWkpSZ0FCQVFBQUFRQUJBQUQvMndCREFBSUJBUUVCQVFJQkFRRUNBZ0lDQWdRREFnSUNBZ1VFQkFNRUJnVUdCZ1lGQmdZR0J3a0lCZ2NKQndZR0NBc0lDUW9LQ2dvS0JnZ0xEQXNLREFrS0Nnci8yd0JEQVFJQ0FnSUNBZ1VEQXdVS0J3WUhDZ29LQ2dvS0Nnb0tDZ29LQ2dvS0Nnb0tDZ29LQ2dvS0Nnb0tDZ29LQ2dvS0Nnb0tDZ29LQ2dvS0Nnb0tDZ29LQ2dyL3dBQVJDQURYQVFjREFTSUFBaEVCQXhFQi84UUFId0FBQVFVQkFRRUJBUUVBQUFBQUFBQUFBQUVDQXdRRkJnY0lDUW9MLzhRQXRSQUFBZ0VEQXdJRUF3VUZCQVFBQUFGOUFRSURBQVFSQlJJaE1VRUdFMUZoQnlKeEZES0JrYUVJSTBLeHdSVlMwZkFrTTJKeWdna0tGaGNZR1JvbEppY29LU28wTlRZM09EazZRMFJGUmtkSVNVcFRWRlZXVjFoWldtTmtaV1puYUdscWMzUjFkbmQ0ZVhxRGhJV0doNGlKaXBLVGxKV1dsNWlabXFLanBLV21wNmlwcXJLenRMVzJ0N2k1dXNMRHhNWEd4OGpKeXRMVDFOWFcxOWpaMnVIaTQrVGw1dWZvNmVyeDh2UDA5ZmIzK1BuNi84UUFId0VBQXdFQkFRRUJBUUVCQVFBQUFBQUFBQUVDQXdRRkJnY0lDUW9MLzhRQXRSRUFBZ0VDQkFRREJBY0ZCQVFBQVFKM0FBRUNBeEVFQlNFeEJoSkJVUWRoY1JNaU1vRUlGRUtSb2JIQkNTTXpVdkFWWW5MUkNoWWtOT0VsOFJjWUdSb21KeWdwS2pVMk56ZzVPa05FUlVaSFNFbEtVMVJWVmxkWVdWcGpaR1ZtWjJocGFuTjBkWFozZUhsNmdvT0VoWWFIaUltS2twT1VsWmFYbUptYW9xT2twYWFucUttcXNyTzB0YmEzdUxtNndzUEV4Y2JIeU1uSzB0UFUxZGJYMk5uYTR1UGs1ZWJuNk9ucTh2UDA5ZmIzK1BuNi85b0FEQU1CQUFJUkF4RUFQd0Q0K1ZkdFNLdTJoVjIwdGN6ZGp6V3hWWGRUMVhzS1JQdWluYkc5S2drU25wOTBVS3Uybko5NFVBSlRrWHVmd29SZTUvQ3JlajZMckhpRFVvZEgwSFNycSt2TGg5c0ZyWlF0TEpJMzkwSW5MVUpUcVQ1WUlFbXl0VDArNks5ZjhPL3NXL0U2YTFHcGZFRFdORzhKMngydHMxYTgzVHNQYUtMY1ZQOEFzeUZEV2kzd1ovWlI4SHN5K05QanhxbXFNdjhBRHBkbkJaL3JJWnQzNVY5cGxQaDF4cG5jVkxDNFNiWGQ2Zm1lbGg4b3pIRS93NE04UlZkMU9UN29yMk5yejloblJXL2RwNGoxTGIvejlhcXYvdEtGS0I4VFAySjdWZHNmd2Z1TGh2Nzh1c1gzL3NrZ3I2K2g0QytJTmFOM1JTOVgvd0FBOWFqd2puTmJaTDd6eDdZM3BUbFhiWHJML0ZMOWo5bS8wZjRGZitWalVQOEE0L1QvQVBoWkg3Sk1uK3IrQnUzSC9VVjFELzQvV3o4QWVPNDdxbXYrM24vOGllbFQ0QXp5cHR5L2UvOEFJOG1qNzA2dldGOGVmc3J5RGF2d1F4LzNGYjcvQU9QVTVmRjM3TVVqZkw4SE1mOEFjU3ZmL2oxYzc4Q2VOSTd1bi80RS93RDVFN2FmaG54RlUyNWZ2ZjhBa2VUeDk2ZFhxNThRZnM0eWY2bjRQNC83aXQ3L0FQSHFkL2FYN1BzcTRpK0ZmL2xTdS84QTQ5V2I4RU9MdXRTbC93Q0JTLzhBa1R0cCtGUEUxVHJINzMvOGllVFVWNnNicjRGdi9xL2hqai90L3UvL0FJNVNMSDhGV0RmOFc2LzhuN3IvQU9PVXYrSUo4Vi84L2FYL0FJRkwvd0NST3FIaER4UEw3VlA3NWY4QXlKNVZReTlqWHFmOW5mQ052dStBRVgvdC91UC9BSTVUZjdHK0ZaKzc0TXgvMitYSC93QVhSL3hCTGluL0FKKzB2dmwvOGdkVVBCdmllWC9MeW45OHYva1R5M1l2cFMxNlhQNFYrR3R6L3F0RWxpLzNibC82NXF2TjhPZkFML05EYzM4VGY5ZGtaZjFTdVN0NE04V1VkblRmcEovcWtLcDRPOFd4K0QyYjlKUDlVanpwMTdqOGFiWGRYWHdqdDVGWnRKOFNJVy9nU2VIYitvei9BQ3JCMXI0ZitLTkZWcHBOTjg2RmYrV3RyODYvajNYOGErVXpUZ1RpbkowNVY4TzdkMWFTL3dESlc3Zk0rZHpUZ0hpM0thYm5Yd3I1VjFWcEwveVc5dm1ZVEx1cFBMOTZmc2IwcEsrU2FhZG1mSmFwMmExSW1YZFRXWGJVekx1NzAxbDIwaHAySVdYZFViUjRQcFZobDNVeGw3R3FUTEt6THVvcVY0KzVvcWdNK25JdmMvaFFpYnFlcTdxMFBEQlB2Q25iRjlLV2lnaHU0VkpVZGVxZnMxL0FXeitLRjlkZU5QSFZ5OWo0UDBOMWJWTHBIdzkxSjFGckdmVTlXYitBZTdMbnN5M0w4Wm0rT2hoTUxHODV1eVJ0aDZGYkZWVlRwcTdaSDhEL0FObS9XdmlwYXkrTXZFbXBmMkg0VXMyMjNXc3pwODB6RHJGYm9mdnY2bm9uZm5hcmRUNHMvYWs4QS9CblQ1L0FmN00vaFNLelhiNVY3ck12ejNOMWovbnBMMWJubllNSU93RmN4KzBsKzBkZmZFYTZYd1A0SGhUUy9EZW14ZVJaV0ZsOGtTeHIwVUFkdjUvejhpaHNSdTVOZjNoNFllQ1dUY05ZV0dNek9IdE1RKyt5UDAvS09HcU9FaXBWRmVScGVLdmlYOFF2SEY0MTE0ZzhUM1UyOXZtVGV3V3NaYkg1V1lGZitCVnBRMmV4ZmxTcGxzMWszQUluK2Z3cjk1VHcrR3A4bEtQS3ZJK3p3dUI5NHkxczVHL2dUYlU5dnBiTjk1SzE0YlA3dVUvK3hxNWE2YTBpamFtRnJ6c1pqbzA5ajZqQlpaS1hReWJmU2ZhdEMxMFJ0dTRKV3hhNldWWGJzclF0ZEo0cjQzSDVuNzI1OVZoTXE4akp0ZEgzTDl6NWEwclhSVi91VnEydW04ZktsWGJmVDFyNVBGWnIvZVBwY05sVWV4bFE2YXEvd1ZiajB2ZDl4SzFZN05WK1ZVcXpIWjdxOFdybW5NZXpTeTJKbHg2WC9zZlNyRWVtK2lmZHJVanMxcVpiRlZPU0t5K3Z4bDFPMk9BNWVoa3BwK0J6VWkyUDhKclUreGhUOXlrK3lwNm1qNjgrNXFzRVovMk0rMUt0cjNyUyt5cW9vK3pEMFA1MUVzYVhIQ0dldHV5dDh0VFc5MWVRSE1iOUtzTkR0NjVwUEpQdlhGV3hpZWpMK3JKS3pNcnhCNEo4UCtMb3pKc1N6dmYrZThTZkt4LzJ4L0YvdmRhODUxN3c3cW5oclVEcCtxUTdISHpLeS9ka1grOEQvZHIxaGx4OTM1V3BOUzBmVHZGbWxObytzSnovQU11ODYvZWpQOTRmMVh2WDVUeGp3ZGwrYzBuWHd5VUszM0tYcjUrZjMzUHkzanZ3cXdIRWVHbmlzREJVOFQ5eWw1Uzgvd0M5OTU0MTVmdlNNdTJyK3ZhSHFIaDNWSk5KMUNQYkxGL0gvQ3dQUmg3R3FkZnoxV3BWTU5YbFNxS3pXalIvSWVLd21Jd0dLbmg4UkRrbkIyYWZSb2hkZTQvR21zdlpoVSt4ZlNtTXZZMW1ZcmNnYVBQdlJVakx0b3AzYUh6R1dxN3FmUlJXemRqd203aFNwOTRVcUwzUDRVNm9FYVhndndkclhqN3hkcHZnbnczYmViZTZyZVJXOXFyL0FIZDd0amNUL0NCMUxkaHpYdWY3VkhqTFNmaHo0WjAzOW03NFozT05QMGlEYmUzQ2NHNmtQK3NsZjNkOXpldzRIQ2lxL3dDd240YnQ5UDFEeFg4Yk5RUmRuaGJSdklzSGYrRzd1dDZieDlJVW1CLzY2clhtSGkyOHZQRkhpUzgxNjhaM2U1bkw3djhBWjdWL1dmMGMrRXNQVTlwbmVJamRyU0g5ZXY1SDZqd0prbnRLVHhrMXZvamxJOVBxeGI2ZW8rNmxhc2VtcW9xYit6MWpIenZYOWV6eEorblVzdmxLUm5mWTFWZjlxcG9iV1BkdGpSVnEyc0tzMjdZbGZVLzdCdjhBd1N4K0wvN1lzc1BqTFVtZnc3NExXVWg5Wm1oek5mYmVvdDBidDI4eHZrejBEa09COHZ4RnhQbFhEdUJsaThkVlVJcnEvd0FrdXI4ajE1ckJaVGhuaU1WSlFpdTU4czZmcHJYRTZRMnNMUEs3S0VSRTNNekgrRWYzcTl3K0Z2OEF3VDIvYkMrTEVjTjE0UitBMnRpQ2JhVm4xRkVzazJIK1AvU0NwWUh0Z0hQYXYyZC9aay80SjUvc3kvc3Y2YkcvdysrSGRyL2FaaUMzT3IzaE54ZFNzUnorOWZuYWY3b3d2b29yM0Mzc3JPMWo4dTJ0VWlYKzdHZ0EvU3Y1ZTRrK2tGVnIxcFF5dkRXai9OTi9vdjhBTStIeDNpbFR3OHVYQVViK2N2OEFKZjVuNHMrSFArQ0l2N2JPcHFHdTdEd3pZSDB1OVdtM2YrUTRHcnFiRC9naEorMWxOaGJyeFQ0VWlIK3hjM0RmemlGZnNBREFPZHlDbXRQYmhzR1JjL1d2enJFZUxuRnVJbGU4Vi8yNy93QUU4bi9pTEhGUC9MdmxYL2J2L0JQeWF0ditDRC83U0FWZk0rSW5odFQzMnBLYXVSLzhFSXYyZzE1YjRtYUIvd0NBOHY4QThWWDZ0RzdnUC9MWWZuU2ZhN2MvOHRSK2RlWFB4SDRwcWJ6WDNEWGk3eHRIYWNmL0FBQkg1V3IvQU1FSy93Qm9CUjgzeE4wRC93QUI1ZjhBNHFwRi93Q0NHUHg5WDczeEwwTC9BTUJwUDhhL1U0M2R1ZjhBbHFQem8rMDIvd0R6MEZZZjYvOEFFdjhBTXZ1TkY0eGNkUi81ZVIvOEFSK1doLzRJYy90QmZ3L0V2UVB3czMvK0twa3YvQkVEOW90ZVl2aUZvRGZXS1ZhL1U3N1RiLzhBUFVmblFMdTNIL0xZZm5UWEgvRXNmdEw3aTE0eThkci9BSmVSL3dEQUVmazVmZjhBQkZYOXFtTEsybmlmd3pLUjBEM0Z3by9NUkd1YzhSLzhFaWYyMGRDdG11TFR3N28ycTdGeUlkUDFOZ3h4L3dCZG80eCt0ZnNPczFzN2ZMSW1hY2ZLZm80elhSRHhINGdoOFNpL2tkVkh4dTQxcHl2UGtsNngvd0FtajhGZmlYK3pIKzBKOEhmTW0rSTN3ajFtd2hoR1pieFlWbmdqK3NzUmFOZnhOY0dMZ09LL29idjlFMGZVb3pIcUduUXloaC9HblA1OWErWVAycVArQ1Yvd0UrTzBGM3IvQUlYMGRQRHZpQ1l0SWRVMHRGUXlTSEp6Skh3azJXNjV3NTdTTFgwdVcrSmRLdEpReGxQazgxdDl4K2djTytQR0ZyMVZTeldqN08vMjQ2cjducitMOUQ4aGl4YW1zdTd2WGRmdEdmczEvRlQ5bC94czNnLzRrYVVmSm0zblR0VWdHWUx4QWNIQjdPUDRvenlQbFBRZ25nVm0zTHpYM0VjenBZbW1xbE9WMHorZzh2eDJDelRDUnhHR21wd2V6UXJSci9FS2piZDk1ZUtrcGpOdTdWNTFmR0hlb2R6SytJbmgyUHhKNGJiVVlVLzB6VDFMZTdSZDEvOEFaaC85bFhsdmwrOWUyYWZjTEhjS3JEaC9sYXZLUEYyanJvZmlLODB1Tk5xUlM3b3Y5dzhqOUdyOGU0N3dkTjFvNHlDMzBmcjBmM2ZrZnk1NCtjTFU4TGlLT2RVSTI5cDdzL1ZLOFg2dEpyNUl5ZGplbEpVdXh2U21zdTZ2ejVTUDV2VHNST3ZjQ2ludjk0MFZSVjBZMVBWZHRDcnRweXJ1clY3bmlDVTd5L2VuVVVpVytoOU5mRFcyL3dDRUovWVRrdlpFMnplSnZFZDFPc3Zkb2tWTGNMK0RwSi8zMVhrQzI4TGRVcjJYeEpieWFmOEFzaC9EdlFaUHV2WlhFKzMvQUs2M2M4by9ScThzZXhqVWZLbGYzLzRUUXA1YndUaG9yZVN2OTUvV3ZCUEQ4WThNNGVYZEovZnFaRWkvS3A4bEZZLzM2cm0xYVQrQmEzV3NZVys5RDkyclBoZndicS9qSHhOcDNnM3c3YWlXL3dCVnZvYkt4akxxdSthVjFqVVo5eTFmb1dJeldsaDZNcWs1Y3NVZllmMmJUdzhIT1gyVDZMLzRKWi84RTk1UDJ1L2lUL3dtUGp1ME1YZ2pRTHBmdHcyY2FsY2pEL1o4OFlRQXF6dDFPNEtQdnM2ZnRwNFo4TmFINE4wSzI4UGVIdE5pdExLemlXS0NDR01JaUlvd0JnZWdGZWZmc2Y4QXdBOE0vczQvQWJ3OThNUER0b3FpeTArUDdYY0ltMDNFeEc1NVc5M2NzNUhZc1IwcWY5cG45b2J3Yit6aDhMZFcrSjNqUy9NVm5wbHF6dEZGdE1rcjlFUkFTTnpzNVZBTWdaYms0NUg4SzhkOFdabHgzeEk0d2JkTlM1YWNQMTlYL3dBQS9tcmlYTjhaeFZuZjFlaGR3dmFLL1gxWm8vR3I5b0w0WmZBWHdoYytOL2lSNHR0ZExzTFJjdlBjdmpjZXlnZFdZOWxBSlBZVjhKZkc3L2d2YjRldEwrYlNQZ1Q4TTd6VmtUY3E2cnFWeDlsaVkvM2tqMnM3ai9lOHMrMWZDWDdUMzdVWHhVL2E3K0pNM2puNGc2cEl0bkZLNjZKb2l6Ym9OUGhadnVqcHVjamJ1a0l5KzMrRUtpcnhlbjZUOTNCcjdESi9EL0s4RGhsVXgvN3lwMjZML00vYStFdkIvTGFWR0ZUTWw3U3AyNkw3dC95UHEvWHYrQ3ozN2FIaUYzR25udzNwaU45MDIybXl1Ni9Vdkt3L1FWeTkxL3dWQi9iaXZIMy9BUEMyNDR5MzMvSzBXMC9yR2E4T2hzNDRWeXFVOTRWUDhGZDFmTDhtbzZVNk1WOGtmcnVENEE0WHc4ZE1KVC84QlQvTTlray80S1dmdHp0OHkvSE9ZZjhBY0NzZi9qRk5iL2dwViszT1Y1K08wMy9naXNQL0FJeFhqbjJkUFg5S0ZoalBhdkxuaHNCL3o2WDNJOU5jR2NOZjlBVkwvd0FBai9rZXcvOEFEeVQ5dVVmODEydUYvd0IzUmJIL0FPTVVmOFBKUDI1ZitpNjNmL2dvc2Y4QTR4WGp5d3JqTktJVkhXcWhoTXZmL0xwZmNnLzFQNGIvQU9nU2wvNEJIL0k5aVgvZ3BKKzNJcmJoOGRiai93QUV0ai84WXA2LzhGS1AyNU92L0M4NTIvN2dkaC84WXJ4enlsL3VmcFI1QS91Q3V1R0F5LzhBNTlSKzVFdmc3aHIvQUtCS1gvZ0VmOGozSFMvK0NuLzdiMm0zQ3ovOExYanVHSDhNdWkydy93RFFJeFhjK0RmK0MwUDdXdmgyVkY4U2FiNGUxaUpXRzd6TFNXR1ZoOVZrMnIvM3pYeXFZVjZiSzZId0w4Ri9pbjhWQzQrSFB3ODFqV2xpZlpKTFkyTHZIR3g1MnZKamFEOVRXZGZMY29jYjFLVUxlaVI1bVpjRjhGcWk1WXJEVW93NzJVZnhWajlJZjJlZitDMmZ3YjhmWDBHZ2ZHTFFicnduZHpQdFM2bmw4K3pKTFlHWlFGS2Y3em9xRHU5ZmJQaHZ4UG92aXpTNGRhMFRVSTdxMnVJdzhNOEVnWlhVaklJSTlSWDgrSGpmNGQrTnZoN3FLNlI0KzhIYW5vOTFJclBIQnFsZzhEU0oweW04RGNQY2NWOUUvd0RCT2ovZ29MNHUvWmM4YVdudzY4YzZ4TGRlQTlTbldKNHAzei9aRHUvK3VRbjdzT1crZE8zek9CbklmNHpPK0Y4SEtpNnVCMzdkUGtmanZHbmczbDliTDVZL2grVjJ0ZVM5MDEvZGZmeTF1ZnFUKzB6K3paNEIvYUsrSGVvK0RQR09oSk9sMUYrN2tRQVBGS0FRa3FOL0RJdWNxZndPUVNLL0ZQOEFhTitCUGkzOW1mNHVhajhLL0ZibWJ5UDN0aHFBaWFOYjIxWWtMSUJ6dFBESXd5UUhWZ0NkdWEvZkRTdFRzZFkweUc5czUwbGl1SVE4YnJ5R1U5RFh4Si93V00vWnNUNGgvQnFiNG9hUHArL1ZmQ3BlOVY0azVhMUEvd0JKUSt3VDk3LzJ5LzJxOGpodk42MkRyZlY2ajkxbnlYaEJ4cGlzanpwWmJpWi91YWp0cjBsc24rai9BT0FmbHVzMjVmdjBqTnVxcGEzRzVhc2VaN1Y5alh4eC9aOU9QTU9XUXF3WmE1RDR3V3ZsNjViWHlwZ1RXdTEvOXBsWnY2TXRkWnUybjVhd1BpMUcwMm5hYmNmd284cS9tcS8vQUJOZkg4UjFvNGpMcHJ0Wi9pZmwvalRnbzRqdzh4TXJhd2NHdi9Ba3Z5Yk9GcEdYZFQvTDk2ZFg1dW5ZL2hLNks3TDJJb3FUYnU0eFJWV1FYUmlLdTZucXZZVWlmZEZQUmU1L0N0VHgyN0NiY2ZlNlUraW15ZHFPcE1maVI5YmZGaXpqdC9nWDhOTEZVNitFckNYL0FMN3QxZjhBOW1yeTJTMWp4WHEveFkvMGo0VS9EcVArRmZCR2xmOEFwRkZYbThscXE3Ulg5dDhMNXRUd25EMkdwZG9MOGovUVBnL0wvd0RqR01MWmZZWDVHVEpabitHdm92OEE0SlFmQ21MNGxmdGw2TExkd0xKRm9GblBxbmx5RDVXY0ZJVS9FR2JlUDkydkFab2RxNWF2dW4vZ2hEb2x1L3haOGFlSlpWeTFscGxwRWg5cERjTWYvUlMxNTNHbkVrcVhEdUlkTjZ1TnZ2ME9QanpteTNoVEY0aUc2amI3OVAxUDFHaVpiU3pra1ZWRzBFTHQ2WUhBcjhuL0FQZ3VmOGM3L1gvaVQ0ZitBdGhkdjlqMCsxL3RYVVkxWlNyVE1YaWlVK2hVTEkyUCttaW4wcjlWdFd1UkJvcEtqSHlDdndwLzRLRytKSnZHL3dDMnI0MzFPUjJaTGEvaXRJbGQ4N2ZKaFNNL21WWnYrQlYvUDNCS3Awc3plSXFmWlduNUg0UjRNNVBITXVKSlZwcS9zMDM4OUYrclBIOUtzMTIvTFd6QkhIQ3Z5MVZzNFZqV3JMVFpyOUV4dWZ4bG9tZjJWaE1GN09KSjVuOFBQV210SmdlbFJsaTFOYVJldGZPVnMzNXVwNkNwRXRGZXZmczAvc0svdERmdFNTeFhuZ1R3dDlrMGRwY05ydXA1amc5L0xHQzB2MUEyWjRMaXZ1SDRTZjhBQkVYNEorSFlvN3o0c2VNTlQ4U1hTZjZ5MWlrTnRibi9BSURFUTZmOS9EWG5Wczh3OUg0bWZuL0VuaVh3aHd6VWRMRTF1YW92c1ExZno2TDV0SDVmZWN2dFQvT1d2Mmw4UC84QUJNVDlpenc3R1Z0dmdacDl6dVhEZjJnM24vOEFvemNhenZGUC9CS3Y5aTd4RXJZK0VFVm0wdWN0WVhNc0lYNkNKMHhXVlBpckNLV3FmOWZNK0VoNC9jTHpxV2RDcGJ2YVA1Y3grTm9tWHJ2cC9tZTFmb1A4Yi84QWdoN1lwYTNHb2ZBTDRrM0szS3F6eGFYcnVIalkvd0J6ZWdEb1BmRWxmRHZ4bCtCbnhhL1o5OFMvOEl4OFYvQ2R4cHM3N3ZzOXg4cndYQUhYWklPR3gzSFVkd0s5L0JaemhjWjhFejlENGQ0NzRhNHAwd05aT2Y4QUk5SmZjLzB1Uy9BMzRjbjR3L0dUdzE4TUJLNlI2M3JFVnRQSkVmblNFbjk0UjdoTnhyOTB2aGg4SmZBL3dvOEYyUGdid0w0ZXROTzAvVGJkSXJhS0dCZXd5U1NjazVKWW5QSk9Ubm12d1orRkh4SjFINFRmRXpRdmlacE1QblQ2SHEwTjJ0dnYyK1lxUGtwbitIY055MSsyL3dBQXYydWZoSjhmUEJWdjR6OEMrSnJlZUtXSlRjMjd6S3N0dEx0NVNSTTVSdlkvVVpISitkNHRlTHhIcy9aL0F2elB4bng5d21lVnF1RnFVVko0ZEozdHR6MzYvTGEvbVpYN2FuN05mZ0w0Ky9CN1VmQ2ZpTFRyWkhlM1o3SytGc045cmNCY0pNbU1mT3Bic1JrTXluaGpYNGEzTWMwTTB0bmRKaVNOeWpyL0FIU09EWDdMZnQzL0FMYy93MCtBbnd4MUFTNi9iWEd2WFZtOGVpNlBFKytXZVloZ2pGZW9qQjVaemdZWEF5eFZUK01WdkpMT3pUVFB1ZDMzTXorOUdRVk1UUndyalYyNkh0K0JXR3ptbGxGZFlwTlVtMXk4M3p2YjhEOWl2K0NQM3g2di9pMSt5eFo2QnJ0NDAyb2VFYnR0TG1kenkwU2hUQzMvQUg3ZU1jOVNyR3ZvYjQ0ZUY3RHhUNEUxRFE5UnNmdE1GMWF2Rk5BNHlyb3cyc3AvMlNHYk5mbnAvd0FFSXZGbDFaZUwvaUI0S3lYanViQ3l1NFVaL3V1cG1WamozL2QxK2tuaTBTWE9neUNQa3RFZjFGZk01aEZVYzBjb0g0ajRoWmJISVBFS3ZDam91ZFRYL2J5VXZ6YlA1N1BHL2hhNjhBZkVIWGZBTjdKdW0wVFdMbXhrNkg1NFpXalBJNDdkcXFySmtldGVrL3Q0YVRiK0gvMnhmSEduVzZiVmZVWTdodnUvTVpiZUtVbmoxTDVyekNPVDVWcjFwWTFTaWYzQncvaUo0N0o2R0lsOXFFWDk2VEpta3dQU3N2NGx4bVR3bmJ6ZWw2aS9tai8vQUJOYUVqcjBxbDhRdm04Q3hOLzFFWWYvQUVDV3ZFeCtKOXBRa3ZJK2E4VllSbDRmWTYvOHY2bzREWTNwU1ZKUlh5Si9uZ3BFRC9lTkZTTXUyaWdvdzFYZFQ2UlB1aWxyb1BJYnVGRkZGU25lU0hINGtmV254S20vNHRmOFBsYitId2JwZi9wRkZYQnRNbVBtRmRkOFNwOTN3MThCcnY2ZUQ5TS85SklxNFh6RC9lRmZ1dExpVDZyZ3FkTyt5WDVIK2wzQkdHNXVGc0gvQU5lMStSSklzZVBuK2F2di93RDRJVVdCRS94QjFCT01KYXJ1L3dCMVp2OEE0dXZ6NGFSdHRmb3Avd0FFSjlyZUh2SDh2ZjdSRVA4QXlHQi9Xdm5jNTRqbGpzSktqZmMrYjhZYUhzZUFzVTEvZC84QVNrZmYzaktVRHc4NmtmOEFMSi8vQUVDdndTL2Fjdm11L3dCcHJ4OU14M0gvQUlURFVGLzc1dUhGZnZGNDRtSzZCUDdSUC82Qlg0Ry90Q3lOSiswZjQvYnAvd0FWcHFuL0FLVnkxOGZUelA4QXMrbG85ejhxK2o3UTlwbU9KZjhBZFg1b3g0WlBsVXJUL04vMnYwcUMzKzc5eXBXa0s5Vk5ZVDRnNXBibjliUncvdWlzK3l2dUQvZ210L3dUTC80VzZsdDhkZjJoTkVhTHcyTVM2Sm90d01IVU80bWtIL1BNL3dBS0g3L1UvTGdQNHovd1RzL1pULzRhdCtQVnZwT3R4NThQYUZzdmRhREpsSmhuOTNBZlp0ckUrcVJzUGxMQnEvWnl4ZzAvU2RQaHRiR0JJYlcxaVZMZUlMaFZRTDF3TzVyYUdQcVZLZDBmenQ0MGVJdGZKZjhBaEV5eWRxc2xlY2x2RlBaTHpmZm92Tmp0SDBQdy93Q0dOTGkwclI5TmdzckdCRlNLM2hqMktpZ0FCVCtBd0I2Y1ZCclBqclJ0R1ViN3BFQis3enorVmVEZnRxZnR3K0NmMlcvQmsydWEvTjlvMUNiZkhwT2pSVGJacjJVRG92WGFpN2xMeUVFS0c2Rm1SRCtUZngrL2JHK1B2N1NlcXp6ZU12R0Z4YWFUSTU4alFkT21hTzJqUS8zd09aVDd5WjlzRGlwZ3FmTjc3UHhyZ3Z3c3puakQvYXFqOW5TL25scmYwWFgxMjg3bjdFK0kvd0J2MzltcndwY3lXZXYvQUJwOE5XODBUN0pZRzFpMzh4U08yM2ZuOUsxdkFYN1pId04rSlYrTlA4RS9GSFF0VmxLbHZLMC9WSVpYd09laU9UK2xmZ3REcC84QWVxZUcxbXRaa3VyZVpvcFVjTXJLKzBxdzZNRFhwVThKaDZoK3IxUG8vWlZLamFHSWtwOTdLMzNmOEUvb3FzZkVPbDZ4SGxKVVpUOTF5Y2o4NjVQNDNmQVg0Yi9IVHdsYytFZmlQNFp0ZFNzYmhmbkVxY2h1ekVqQkJIWndRdzdHdnlXL1pPLzRLWWZHMzlueldiVFIvSGVyM25pand5c29XZTN1NXQ5NWF4N3VXaWtibDhEL0FKWnVjZkxnRk90ZnJSOEd2akw0TCtNM2dXdzhmZUJmRU1OL3B0OUFIZ25nZklBUEhma2M4RlRncVZZRUFnaXNhbUdxWU9YUEJuNHZ4VndOeEJ3QmpvMWxMM0wrN1VqZmY4MC82UitRSDdjUDdGUGkvd0RaRDhibG92TnZ2Q2VvWExKcE9xT01zaEtseGJ5a2NiOEJzSG80VmlOcEJBOFBzTlExVFM3aGI3UjlTdUxTWmZ1eTJzekkzNWl2M1QvYWUrQmZoUDQ4ZkREVlBoLzRyc1BPdHIrMUtKcys5RS9VT2gvaGRUdGRmZGEvRWI0cGZEdlcvZzM4VE5hK0YvaWJtODBhL2VCNUNtQk1uV09RQS9kRG95dVBacStrd2VackVVYlQzUDZOOEx1Tlk4WDVZOEpqWGV2VDMvdkx2L21jOWNSM1Y5Y05kWDF6TE5JL3pQTEs3TVcvRTFOREQ1UzFJcnFQdWpOSzY5eCtOYVZjUkZIN0RUb1FnclJSOWkvOEVSTDNiKzFOcmVtYjhDYnduTS8vQUh6S2c2ZjhEcjlWZGFmT2pFNTlLL0o3L2dpUk5qOXNxOGo3SHdiZWYrajdldjFXMU9mZG93LzNVcjViSDFQYVltNS9GM2pYVDVmRUNmOEFocC9xZmlYL0FNRktJZnN2N2F2aXhqL3kxV3liL3dBbEloLzdMWGpFY255aXZjUCtDb28yZnRrNjJ5L3gyTm0zL2tOUi93Q3kxNFRESU50Y2RYRWU5WS9yUGduM3VHTUkvd0RwM0g4a1R5TTFRZVBNTjRCaWIvcUpRLzhBb3FXbFp0M2FsOGRML3dBVzVqYi9BS2lrUC9vcVd1Q3RWNTRTWGtlUjRyKzc0ZlkvL0QrcU9Cb29vcnlGc2Y1MUpoUlNQOTAwVXlqRFQ3b3BhS0szZXg1WVVVcWZlRkwvQU1zNmhiamk3VFI5TC9FQ2J6UGgvd0NDVlovdStFdE4vd0RTUks0MXQzOE5kaDQyaDNmRC93QUZTUDhBOUN2cDMvcEtsY3RIQi9kTllabnhIN0dmSW5zZjZqY0NxUDhBcWxndit2Y2Z5SzhpYlQwcjlGZitDRTVZK0VQSHpmOEFUN0gvQU9nUjErZWtrSzQzYi91MStoLy9BQVExWHkvQmZqOXMvd0RMNG4vb0VkZVhsZWZTeHVZcW42bnlmalc3OEE0ajFoLzZVajdwOGFUYnRGbC8zSC85QnI4RVBqekd6ZnRHK1BCMmJ4cHFYL3BYTFg3MGVMWk4ybFNML3NQL0FPZzErRUh4MWhMZnRGK1BQK3h5MVQvMHFscGNWNWw5VG9ROVQ4bytqMzd1WVlyL0FBcjh6R3RiVUtuemZOVXNsdnRXckZ2Yi9LS1c2alJJV1phK05wWjN6VHRjL3FtVmJsaTJmcTUvd1NMK0RjSHcyL1pWdHZHTnhhckhxUGk2NWU2bGZiODNrbGg1ZVQvZE1LeGtEMVp2WE5mUTN4UzhZV0hnL3dBTDN1dGFoY3JEQloycnpUeXVmbGpDaGlUK0FYTlpIN1BXalIrRmZnWDROOE5yQ3FHeDhQd3EyMWNEaFZYdDdKWGpIL0JVdnhqcVBoSDlrZnhmZTZmZHRGSmQyeVdYeTkwdUpZb1dIL2ZEeVYrc1FyeHcyRVVuMFg2SCtmOEFYVlhpM2o2YnF2V3RXdDhuS3krU1IrVmY3UzN4MjhTZnRNL0dmVlBpVnJVc2d0WkpURHBGdEkyZnMxb3JreHAvdmNzN2Y3Yk5YSjJ0bnRYTFZKb3VuN2hqWld4OWhYWjcxNVdEelRucVhrZjNobHVDd3VYNFNHSG93dEdDc2w1SXpJNDFwM2wrOVdyaXkvdTFCTERMSFgxbUV6S010bWQ3aEdXeEZKR3RmVlgvQUFTYi9hZzFENE9mRytMNFJhN2VNM2gveFpLVVdOMzR0N3pidzMvQXd1dys2eCtqVjhzMVA0ZTEyOThLZUpkTjhVNlpLMGR6cGwvRGR3U3IxVjQzVnd3L0ZhOVQ2MUdwVHN6NW5pcklNUG4yU1Y4SFdqcEtMK1Q2UDVNL29Ga2Z6N0dTMmtseVV5cXQvZUhVSDhSelg1Yy84Rm52aFRGb1B4QjhQZkYzU3JEWW1vUlBwMm95ckZoZDYvdllja2RTUTB3OWNSci9BSGEvUy93dHE4ZXBlSDlPMUtJN2t1TkxoY3Q2N1FWeitsZkhIL0JaalFtMUg5bXYrMU5tNyt6dGR0WnMvd0IzNW5pei93Q1JjZjhBQXE0OFBWOWpLUi9IWGhmaTYyVmNiVUl4KzFMa2Z6MC9NL011M20zTDBxVm0yMVJzNXYzZkZXR2srWHBXczhielJQN2xndmRQcjMvZ2lVNFA3YU04Yk45L3diZkQvd0FqVzlmcWhxVnhuUlYvQ3Z5by93Q0NKTW1mMjFIQVAvTW5YMy9vMjNyOVNkU3VOMmpqSCt6WERPcDdSOHgvR1BqWERtOFFKZjRLZi90eCtObi9BQVZNMi84QURZbXFONjZUYS84QW9EVjROYnQ4dTJ2ZHYrQ3BqZjhBR1lXb2ovcURXdjhBSnE4R3QyekhqUDFyeDhSVy9mTkg5VDhEci9qRjhKLzE3aitSUFUzamhDM3d4alkvOUJlSC93QkZUMVZxOTQ2WC9pMHNNZy82RE52L0FPaVo2eWhMbTV2UThmeGM5M3c4eDMrSDlVZWRiRzlLU25LeWdVMnVkYkgrY3FkZ29vb3FyTXZjdzZLS0syZXg1NHFmZUZML0FNczZJKzlOcUhvZ1h4bytudkdBMy9EbndVdy82RmZUdi9TVks1WlZMZEs3SHhOQzBudzE4Rk12L1FyNmQvNlNwV0REcG5iTmZoZWQ1MU9XUHFSajBaL3A5d1ZYcDArRk1IZi9BSjl4L0l6M3R4dDlhL1E3L2dpUEg1ZmdieDk4bTMvVEUvOEFSY2RmQmNlbjdWKzVYMzkvd1JmaDhud1g0OFFkVGRJMzVSeG4rbGE4SDQ2cFU0Z3AzZlIva3o0enhqeGNhM0JGZUs3eC93RFNrZlpYaWQ5Mm15TG4rQi8vQUVHdnc0K091bnMzN1JQanB0bjN2R1dwZitsY3RmdUZyeitacDdxdmRILzlCYXZ4VytPTmkzL0RRL2pzL3dEVTVhbC82VnkxOUx4L1dsOVdwK3JQeTN3TnJmVjh3eEwvQUxxL05ITFc5aXl4ZkxTWFZySWtiRE5iMXZwbTVkdXlpNDB6ZEdkcVYrVTA4ZFVoUGMvcGIrMFBhUmFaK3ozd0Q4UlIrS1BnZjRQOFRLKzgzbWh3dEkzdVZWLzVQWGpuL0JUbndmZWVOZjJUUEYxaFpXanl5VzFzdDc4cCs2SUpZcG1QNElqZmh1cHYvQkwvQU9LYWVQUDJXN1R3dGMzYXZmOEFoUzVlMG1UZHo1YTRLREhwNVJqL0FPL2JWN2g0ejBDeThVNkhjNkxmeEpMRGVRUERMRTQrVmd5NHgrSU9LL29uQjR5T1paTkNjSDhVUDBQNGVuN1RoampTYzVMV2xXdjkwcnI3MGZoUm9jSUVYelZxdEQ4dTNGZGorMEY4QjljL1oyK0wycGZEdlY0SlRhcEswMmozTXAvNCtiVmkzbHY5UjBiM1ZxNXBiZFdYNWErRG81blBEMVhUcWFOSDl4WmJtZUh6RENReEZGM2pOWFRNMlNFMURKYTdxMDVyVmwvZ3FGb2ZTdnBjSG04ZWpQVWpWTXFheHAyZ2VHZFE4VStKOVA4QUNtblc3U1Q2bmZ3MnNFUy9lWjVIVkFCK0xWZWtoRmZUMy9CTEQ5bDY5K0wzeHZpK0xmaUczMmVIdkNUbVFUU3B4TmQ3ZWcvM0ZLdHgvR3llOWZUNERNNVltb29JOFhpblA4THcva0ZmRzRpV2tZdTNtK2krYlAwMDBLemcwalE5TjAyM2oyQzIwdUNNcjZjYjhmaHZyNDIvNExKZUlUYWZzMS9ZR2tSVHFHdjJzU2Y3UkRQSmoveUhuL2dOZlkrcDZnWlROZWxNTkt4S0wvZEhRRDhCeFg1ai93REJaYjRzeGF6NDU4Ti9DVFRiOVNsbEZKcU4vRXJaWGMzN3FIUG9RQk1kdlhFaW4rSVo5dkVZbU5LbE9UUDVBOEw4dXJabHhqUWxiYVhPL2xyK1o4YldMTnRxMDMzTnExVnNjcXFpcDJiZDJyeWxqdmRQN2pqVDVZbjF6L3dSTmZ5LzIxWEFIL01vWDMvbzIzcjlQNzY0M2FTbi9BSy9MdjhBNElxTnMvYlJjLzhBVW8zMy9vMjNyOU5iNjQvNGxhTDlLOVBDVlBhVUxuOGErTkZQL2pQWmY5ZTZmL3R4K1F2L0FBVkpiSDdYMTk3Nk5hL3lhdkJyVS9MeFh1My9BQVZHWmorMTFldG4vbURXdi9vTFY0VGJmZHJ3c1JWLzJtU1A2ZzRIai94aStFLzY5eC9JbHJTOGVML3haeUdUL3FOd2YraUxpcytUdFd0NCtqSitDTUVuL1VlZy93RFJOeFhUaDN6Umw2SHozakErWHc5eDMrSDlVZVgwVVVWSi9uS25ZUi91bWlsb29MM01MZXZyUzAyUHZTNzE5YTZEakZvb29xSjdDai9FUjliNnBicEo4TC9CYmVYL0FNeXZwMy9wS2xaTWRxVnJvWm9kM3dyOEZNK3ova1Y5Ty84QVNWS3pZN2ZjMjFhL2wzTmF2Tm1OWC9FL3pQOEFSamhuRmN2RG1Gai9BSEkva1Zmc3Z5MTk0ZjhBQkd0Vmo4TStQSS83cmh2emovOEFzYStJNGJFc1B1VjlyLzhBQklZZVJINDRzbGJHNktGdVBlS2IvQ3ZWNE5yZjhaRlJYcitUUGt2RkNxNjNCK0lYK0gvMHBIMS9xa3U2emJqK0J2OEEwR3Z4MytPK21NbjdSdmpnYk4yZkYyb04rZHc1cjloYnJkTEQ1YS94SXkvbXRmbEorMFhvdjJUOXBQeGt1ejcydnp5ZmMvdkhOZldlSUZYL0FHZWsvTm41MTRUMXZZNWhYWDl4Zm1jVGE2ZU50U3RwdkgzTTF1MmVsL3UxcWY4QXNrTjBTdng5MS9lUDNaWXkvVTduOWh6OW9CdjJiUGpWRmU2eE5zMEhXMVMxMVptZkNRa04rN21QOTVSdVlOL3NTTWY0Ulg2YnhYRnJlV3NkNVp5YjdXNVRkYnQ3ZW4xRmZrTmVhR3NpNFpOMWZUdjdGbjdicitBWUlmaEI4YU5VeHBDS3NXazZ4UDhBOHU2amdSeW4rRkIwVnowNkg1ZVIra2NFOFdVOEYvc1dKZnV2WjluL0FKTS9JL0ViaFdlYVMvdFBDSzlSSzAxMWFXelhtdnlQby84QWFoL1pYOEJmdFErQ1g4TStKWWx0dFRoQmZTTlpWQjVsdEtlNEp4dUJ3b1pDY09QUWdFZm0zOGNmMlJQamwrenpxMDFwNHY4QUNFOTNweU1mSzFqVFlXbGdaQi9FMkJtSSt6WTlzam12MWxzOVRzdFFzMHU3RzZTYUNSUVZsVG5jRDAvL0FGMVllNEUwSnQ3cTNpdW9SOTJLNVRmdCtoKzhQd05mb1daWlBnODIvZXhmTEx1djFQaWVGT1A4ODRSL2N3WHRLWDhqMHQ2UHA2Ykg0a3JKREl2elZHYmY3Uk1sdmJ3dkxMSzIxWWtUY1dKL2hBNnRYN0QrSlAyZC93Qm0zeGRMNS9pWDREZUg3cVk1WjVUYXhaWS9WbzJQNjFmOEsvQ1g0SmVBWFdid0o4R3RCMHlhTmNMSkZhS0RqL2dDb0s4L0RjTllxbkt6ckszb3o5T2ZqcmhvMGJ3d1UrZi9BQkszMy84QUFQenIvWmQvNEpyL0FCaitQT3FRNjM0MzBxNDhNZUdJNVExemMzMGZsWE15WlhJUkcvMVdSL0cvMUNQWDZTK0RmQm5nZjRVZUNiUDRaZkRUUklyRFJ0UGlDS2tTYlRMdDUzSHZqUFBQTEhjU1NTVFY2NzFpNW5pRUZ4Y1lqVDdzRWFoVlgvZ0l3UDYxeC94VStMSGd6NFZlRkxueGQ0MzhRMnVtNmZhcCs5bnVYd0Y5RndPV0o3SUFTVHdCWDIyQmhoTXJvNlM5Vy82MFB5bmlYaW5pSGp2SFFqaVBnVDl5bkM5ci9tMy9BRWlsOGZQalI0VitEUHc1MWJ4LzRydnZKdE5OczNrWWZ4U0hvcUQvQUczTzFGWHVXcjhZdml2OFF0ZStOdnhSMXI0cGVJbE1kenJOODgza0srNFFSOUk0d2VOd1JGVkEzb3RldmZ0cC90ZStLZjJxZkdMYVpwa2s5bDRTMCtmT20yRGNQY3VPUFBsQTc0YjVSMFVjRGtrbnlDejBueUZYNUsrWnpQaVduaWEzSlNmdUw4VCtpL0MzZ3IvVnJCUEZZbVA3K3B2L0FIVjI5ZXIrWFl5bTA5bzQ5eTFFeXN2eWl0NlMxMjFuMzFqOHU1YTU2T2JIN1RUbEdXaDlTLzhBQkZkaXY3WXQxSkdjN1BCdDZmOEF5TGIxK2xONWNmOEFFdlg4Sy9Odi9nakJhdEQrMUhyV3FENVJiK0RyeGQzOTBsa1Avc2xmb3RlWEgraXF2KzdYMzJTMXZhWmZHWG16K09mR0tQTjRnVlArdmRQOVQ4bS8rQ25jaXlmdGM2a3Evd0FHazJhLytPWnJ3NjMrNVhzUC9CU0c2RjMrMkg0aENuL1ZXdGt2L2t1aC93RFpxOGV0L3dDR3ZFcTFlYkhWSStaL1R2QmxQbDRZd24vWHVQNUltazdWdGZFSmR2d0h0NVArcGd0di9TZTRyRWZyK0ZiL0FNUmwyL3MrMnpmOVRIYmYrazkxWHU0U1A3dVhvZkplTWovNDE1amZUOVVlVGIxOWFONit0TW9yRS96bFRzTzh6Mm9waFlMMW9xMXNXWWxGSXJicVd0VG5GM3Q2MHZtZTFOb3FaSzZHdmlSOWxXOGJYSHdwOEdiZnVqdzFZZjhBcEtsSlphZnUvZ3F4NFBWZFQrRGZnKzRYK0hRYlJQOEF2bUpVL3dEWmEwN0N3Kzc4bGZ5Ym05VGt6R3V2NzcvTS93QkFNZ3J4L3dCWDhNMS96N2orU0s5dnAveTlhK3MvK0NVYzMyWDRqK0o5RmtmYUwzVGJkdnhBbVgrY3RmTk52cCs1YTlrL1liOFhMNEEvYUcwMlYyYU5OVGdsc3QvdVNzaThlNWlWUi92VmZEZU9qaGM4b1RlMS93QTlQMVBENDBqTEc4TjRpakhkcjh0ZjBQdk15S1VRZW1LL09IOXMzd2pOb1A3VWZpTGREdFM4ZUM0aS93QnJkQ21mL0gxYXYwaTFhQkxlL2xqalA3dHh2ai8zR1hjUHkzVjhrZjhBQlJmNFd6dnJlaWZGdXh0OTBja1gyRy9aZjRUdUx4dCtPNlJjL3dDN1g2Vnh6U2xYeW5uWDJIZjlQMVB4M2dITUk0UE9MTjZWRTErdjZIekxZYWFQTDVTclM2U3pKalo5SzFkTHNmTWpyVGowZHV5VitFenI4c2o5c2VNc2N1MmpqK0pNVlF2dkRxekwvcTkxZHUyanNuYW81dEhaditXZEVjVHlEV044eFBnNyswajhaZmdJRjAvdzdxaTMya0xMbDlKMUVzNkxucjViZmVpK2dPelBKQnI2TzhDZjhGSXZoUHJiSmErUE5HMUh3L2NFRHpaWklXbmhWdjhBWmFJYmorS0N2bWU2OFBwSjk1S3lyN3duSE92eXcxOVZsZkdPYTVkRlJoVXV1ejFYOWVoODltWER1Ulp0TDJsU255ejdyUi81UDVvKzY5Ty9iRi9aeTFEN254YjB1UDhBNitMblovNk1DMW1lSWYyNWYyWk5DVjJ1dml0Wnk0M2Y4ZXZtemNqMmlScStENzd3UEczL0FDeHJMdVBBOGY4QUREWDA4UEVmTVpSdHlyOGY4enlxWGg1a3ZOclVuYjVmNUgwcDhXLytDcStnMmxwUHAzd2g4Q1hsN2NuNVk5UTFNZVRBcC92N0FUSTQvd0JrbFByWHgvOEFHZjR0L0Z2OW9IeEV2aUQ0bmVKWHV2TE9MV3ppVVIyMXVQOEFZakhHZlVuTG51VFhRVGVEWTQvK1dOUVNlR1ZqL3dDV05lWGpPSzh4eCtsU3BwMjZINk53N2tXUVpGTG53MUpPZmQ2djcrbnlzY0ZiK0hGajRhR2lTeDh2NVZyc0xqU2xYNWRtS3pielNjRDF4V0ZITktuYy9Sc1BqNG5LM0VPMXR0VUxxSGRYUTMybXNuM3F5cnkzYU5XYitFVjdPSHpXWFU5ekQ0bUwxUjllL3dEQkYvd25LUEZueEM4ZXVtMkt4MFNDMEJmK0pwZk5HMEgyUGwvOTlWOXJhbGRQSGIvTC9DamZvdGVNZjhFNHZoQmMvQjM5a3lMVzlidHhGcVhqYStGK1Zkdm1GdDhwVGpyOTJPTDhXYXZUL0YycjJ1a2FKZDZyZlhLUXcyMXU3eXl1KzBSb0JsaVQvQ0FGYk5mdXVSVGxoOHFwS2U5ci9mcitSL0hQR3VQcDU3eHZpc1JUMVhPb0wvdDFKZm5jL0p2OXUzVTdmV1Aydi9HVjVhVGVZaVhWdEQvd09PMmdqY2ZnVllWNXREOXhhbjhmK0xwZmlIOFQvRVB4QWxpTVoxclhMcStFUjRFZm5UTkpqOE4yS2lpWGF0ZUpoc1RHdmlwVkk5V3oreGNpd2s4SGt0Q2hQNG93aXZ1U1E5T3Y0VjBmeE1qMmZzNTJrbi9VelczL0FLVDNWYzRuWDhLNlg0dlNMYi9zODZYYXNQbWs4UXd2L3dCODI4NC85bXI3akJML0FHV2I4ajgzOGFweHArSG1NdjJYNW84YlJ1eC9DbDNyNjFGNW50UzVIcUs1RXJuK2RLZHhhS1JtMjBWWXpGVnR0TzNyNjFIdlgxcFZidUsyYUllNDd6UGFrM3Q2MGxJLzNUVWlQdEw0Qk0ycmZzNitGcnh2bXhielJmOEFmRnhLbi9zdGRqWjJQVGF0ZWQvc1Q2dC9iM3dIbjBsNUVaOUkxbWFKRjdxanFzb2I4UzhuL2ZOZXRhZlk3bXl5Vi9KSEZVWllYaURFMDMvejhsK0x1dndaL2JYQ09QamlPRk1ITmY4QVB1SythVm4rS0VzOVBicnNyVjBsNy9ROVZ0ZGUwaVh5N3F6blM0dHBOblNSQ3JxZnpXcDdIVDhmd1ZxMitsN2wyQksrVytzVHBUNW9uWGk2OEtrSlFrZmUzdzU4Y1dQeFcrR2VtZU9kTisrMXFubnhLK2RoNkZTZlZIM0lhVHhmNEg4UC9FdnduZStCZkZNSWV6djQ5cFk5WTM3RUgrRWh1UWZXdm1MOWw3NDJUL0J6eEUrZzYxTTM5aWFsTCsrM2NpMW1PQjVwSE9VSVhERC9BSFQyd2ZyV1NDM3VyZE5WMDEwa3Q1RkRvVWZkdHlNLzhDQjdIMHI5eHlQUE1ObitXY3M5WGEwbC9YUm40RG0rWFZzbXpGcUdpdmVEL3JxajRSK0pQd1M4WC9CVHhPL2g3eExaczBCbFAyRy9SUDNWMUdPaEg5MXZWZTMwd3pVYlN4alpmbCthdnZUWE5LOE0rTmRHZnczNDQwQ0xVYk4xMi9PUG5YMElQcU8zY2V0ZVQrSy8ySGZEbDVjUGQvRGp4djhBWjBMZkxZYWdtNVYvMlFTUWNmblh3R2Q4RTR5bldkVEFlL0R0MVgrZjVuMWVYY2JVZlpxR04wbDM2UDhBeVBtcE5OLzJLYTJrOXlsZTI2ait4WjhidE9jaUN3c0xzRDdqUTNoL3F0WjAvd0N5aDhlSW0ybndNSlA5eTZUK3JDdmtaNUJuOU9Wbmhxbi9BSUMzK1NQZWh4TGxWVFZWby9lang2VFIxeFZXYlJTZHpiT2xleHQreXI4ZS93RG9uTS8vQUlGUWYvRjFESit5cDhlVys3OE5ybi93S2cvK0xxWTVObmtmK1llcC93Q0F2L0k2WWNSWloveitoLzRFdjh6eFc2MFZSOHJKdXJOdk5Gang5eXZjcHYyVHZqKzdZSHd6dWY4QXdLZy8rTHFuY2ZzamZ0Q1NmZCtGOXgvNEZRZi9BQmRkRU1venYvb0hxZjhBZ0wveU95bHhMbG5YRVEvOENYK1o0TmVhS3E3djNmelZrWG1rbFZiNUsrZ0xuOWpmOXBGK1kvaGRjbi90OWcvK0xyT3VmMkpQMm1yaHVQaFpNdTcrOWV3Zi9GMTIwc296ci9vSHFmOEFnTC95UFVvY1U1UEhmRXcvOENYK1o4ODZocGFyMVNzYSswL2I5MnZwVi8yQXYybnI1dG4vQUFna2NIKzFMZkwvQUV6VjNUUCtDWWZ4eTFKeE40cDhRNkZwRUIrWGZMY083L2t3UUg4RFhyWVhJczhxT3lvUythYS9NOVdueHh3OWg0M25pb2ZmZjhqNUYxQ3pWTnhhdmJQMkl2Mkd0WitQbmk2RHg5OFF0TmF4OEM2ZEtKcm1hZE5vMUJrYi9WTG5xbWVDM2Y3ZzdsUHBqNGJmOEU4UDJlUGgxY0xyWHhJMXlmeG5mdy9kc3hENVZvcDkxNUIvNEVYSHRYc21xNjgwOWxGb2VsMmNWanB0c2dXejArMitXTkFCZ2RPcDl6WDMrUThLMUtGYU5mR3RhZlkzKy9vZk5jUStLTThWaFpZVEo3cHkwZFI2V1g5MVBXL203SkVPdDZsYjNrOGNlbjJDMjlwYlFpQ3loWCtDTmVnLzJpU1dKLzNxK1gvK0NtUHh6WDRWL0FPNzhINlZlN2RaOFY3OU90bzE2aTNaQ0xxWG9kd0ViZVQxeURNcDdHdmNmaXQ4VGZDWHdjOEQzdnhBOGNhb2xyWjJVVzRNM0xTT1FTa2FEK0tSc1lVZm5nWk5mbFIrMFg4YlBGSDdTbnhYdlBpTnI4SnQ0Q3YyZlNOUEVtNWJLMVVrcEhuamNjbG5ZNEdYWmlBQndQcnM1ejJPQndycHdmdnY4RjMvQU1qei9EWGhTZWNadkRFVkYrNXBQbWwvZWEyWG5mZCtXKzZQTTdPelpmdmNWZVZOcTdxdS93Qm5Lb3FHU0ZveHdsY1dSNHlOU3gvWDlOODFNWW8zZmRGYkg3UWtqV1B3djhLYWIwRTkxY3kvOThKRVAvWjZvV05tMGwwa1A5OXd0UDhBMnFyNXJmVXZEZmhWWHlMSFJ2dERML2RlVjJCWDhvb3pYNnhnNWY4QUNkT1hleS9FL0J2cEI0eU9GNENxMG05YWtvcGYrQkova2p5cmV2clMxSFM3MjlhNWtqK0FvanQ2K3RGTVp1NU5GVnlsR1J2YjFwZk05cWJSV3hMUkpUWk8xSnZiMXBLbHhKUG9IL2dueDQyaDAzNGthbDhQYjZiRWZpQ3czMnUvK0s0ZzNPRkgxamFRL3dEQVZyNjF0ZE5hR1pvV1Q3dGZtejRUOFRhdDRMOFRXSGk3UUp2THZOTnVrdUxWdjl0V3lNLzNnZTYrbGZwVDhNL0dtZy9HTDRmNmI4VFBDNzdvYjZML0FFaTMzNWEzbUhFa1I5MVA1amFlakN2NTI4V2NrcVlUTW81bEJlNVVWbi9pVzMzcjhtZjBONFU4UlJyWlhMTEtqOSttMjEvaGUvM1A4MGEybjJPNC9kcmJzN0ZXMnFxVkRwOXIwMm10M1Q3UGJ0NHI4U3ExRDlHeEZjckxvNnlyOHkxNko4R3ZqdDRtK0V1elI5U2hiVU5IM1orem4vVzI2bnI1WlBERHZzUEhvUmtrODFaMmE0VmMxZFhSNDVJOXJKdXA0SE5zWmx0ZFZzUE96Ujg3bUZQRDQyaTZkVlhSOU9lRGZpTDREK0kwUG1lRnRiaWFYWm1XQUZnOGZybU00SzQ5Ung2VnVmMmZPZnVmUC91L05YeUwvd0FJOHl5ZWRhNzBjZk1yTDhyTDlLNlRSL2lsOFcvRGErVnAvakM0bWpYK0c4Ukp1UHE0Skg0R3YwUEErSTlLVWJZdUd2ZVArVC96UGg4VHc3S01yMEpmZi9tZlMrMjhoWGhHVGQvc01LY05RMUNMaExxUWZ3L2ZZVjRKYi90UGZGYTN4NW1tNlc2L3gvdVpWWnZ5ZXJTL3RXL0VHTkY4N3cxWXQvdXZJdjhBVTE3VU9QTW0vbmYzSG5TeVBIZnlyN3ozTCsydFZIVFVKLzhBdjZhUnRYMWZvZFRtL3dDL3AveHJ4TC9ocmJ4b3YzdkNWci80RXZUVy9hNDhaWi81RSszL0FQQXgvd0RDdG84ZDVML3o5ZjNNbFpIalArZmErOUh0aDFuVnovekU3bjhKVC9qVWNtczZ5UDhBbUpYUC9nUWE4UmY5cnp4b3YvTW1XdjhBNEdQVmViOXNMeHNyWVh3WmEvOEFnWS8rRmFManJKZitmcis1bWtjZ3h2OEF6N1gzbzl2azFuV1cvd0NZbGM1LzYrRFZhYldOWWRkcDFHYzd2NGZPYXZDcm45c3p4MUVmbDhFV3YvZ1M5WmQ5KzIxOFE0bHpENE1zUCtCWE10YXg0M3lYL240L3VaMVUrSGN3bHRUWDNvOTh1THErbWJjMDhydC92c2FxVFc5OHpjMjB1VC9zTlh6ZnEzN2NmeGU1K3krRnRKVC9BSHZPUDhuRmNmNGgvYlYvYUF1WUdpc2wwYXpmbjk3YjZhenQrVXJzUHpGZEVlTnNwMzVtL2tldGgrRk0wcWFKSmZNK3QzczVHT0pKQUNmdXFPVCtRcnh2NDQvdG5mQmI0S1c4dG5Ecks2NXJLYmxUUzlNbTN5S3cvaGtrd1VoK2Jybkwvd0N5MWZLWHhHK0szeHMrSkVNbHQ0dytJbW8zRUVvWXkya1Uza1FOL3ZSUkJWYjI0NDdWNTFkZUZkcmZjL2pybnhIRy9ORzJHamJ6ZitSOXZrdkFWT1ZSVHgxUzY3TC9BRDMvQUFJUDJoZmpyOFRQMmsvRkM2MTQzdTFpdExYSTA3U3JQY0lMVk81NU9YYzkzT1NlbnlnQURnMTBOSVYyc2xkdk5vY2NmL0xPc3UrMDhSK3dyNTJlWVZNVlVjNXU3Wis2NUpUdzJBb3dvVUk4c0YwUnlkMWErWCs3WCtHcWMxbWpaK1N0eSt0enUrN1ZaYk9TNG1FTUtibWY1VXI2L2gvRSs4a2ZkMGNSYUhNV2ZobjRWdVBFSGlxM3Q0MDNJakt6VjVOOGFQR0VQanI0bmF0NGlzWDNXejNYa1dicjBhR0pWampiOFFxdC93QUNyM0g0b2E1Yi9BbjROeVdzY2lqeEQ0bmllQ3pUK09HQThTemZrMnhUNnRrZmROZk1hdHRyOXl3azVmVVl4K1ovRlgwaCtOS09iNXBTeWpEeTVvMHRaZXIyKzVYKzlFMjlmV2tkdXcvR21LMjZrOHoycmRLeC9OYTNIRTVPS0tqWnU1TkZNc3pVYnNmd3BkNit0UkkzWS9oVDk3ZXRiMlFEdDN6YmFXbWIyOWFON2V0VFprUGNmWHNQN0huN1VsNSt6djR6YXoxNUpibnd2cTBxcnE5cW56TmJub0xpTWYzaDNIOFk5MVhIam5tZTFPcnpNMnluQloxZ0ttRXhNYnhsdi9tdk5kRHR5N01jVGxlTGhpY083U2lmcnhvTGFONGkwZTE4V2VFOVNndjlOdjRoUGEzVnErNUpFUDhBRVA4QVB0VzNZMnUzYi9lcjgwZjJVZjIwUEgzN011cWYyWDVQOXNlRnJtWGRmNkpjVGJmTFk5WllINTJQNjluN2pvVi9SajRGZkd6NE4vdEdhUDhBMjE4Si9Gc1Z4Y0JOMTdwTTcrVmVXdjhBMTBpSnpqK0hlTW9leHIrUitOT0JjMzRYeERseXVkRDdNbCtVdXovQjlEOTh5UGpMQVo5UWlwUzVLdldQK1hkSFhXTnI4dGFkcmFyOTZpSFRicUZ2MzBMMWV0NGZtQzErYXptZXZWcURJN05XMjgxTi9aY2Jmd1lxM2J3c3k4SlZxTzM5cTU1VkRpblZNc2FLakg3bEsyaDI3ZndkSzJQSVUvZlduYkY5S2oyMGpKMVRCYlFZVy81WjFGSjRmaDIvY3JvZktYcXREUXEzV3E5dXdWVTVpNDhQd3F2M0twemFESC96eHJyWkxPcWMxcnkxYVJyeU40VlRqN3J3N0MzOEZaR3BlSFlmK2VOZDFlV2Y4TEQ5S3lkUXM5M2V1cW5Ya2QxR3NlZDZwNGRpVGQ4bGM1cTJnUXF2eUpYbytxV2YzdHY1WXJsOVdzOXJGV3IxYUdJa2ZRWVNzZWQ2cG84UUIrU3VjMVRUVkdRc2RkOXJGcjFybGRVdGZscjJhRldSOWJnYXh4V3BXb1ZXcm45U3Q4YnY5bXV3MUsxZG0yeHBsdjdxVkhwdnd2OEFGbmllWlZzZE5kUS84Ykp0NCtsZTNobTV2UStxdytNcFVJODg1Y3FQTmJ5emtrazhtR1Bjei9Mc1N1bXNkTThLL0Jud20veFMrS1Q3SWszTFlXQ2Y2eThsN1JJRCtwNkFjMWQrSW54QStDdjdOc0VzT3RYNitJUEU2ZjZyUTdPWlc4bC8rbTdqSWlIdHkvdC9GWHl0OFZ2aTU0MCtNUGlZK0p2R1YrcnNFMld0bGJwc2d0WXY3a2Fmd2ozNm5xU3hyOWg0TTRieGs1ckVZbFdqMDh6OHY4UlBHakI1VGdwWmZsVStldExTL1NQcjUrWDNqZmloOFRQRVh4YThhM1hqVHhKSXF5WEh5Mjl2Ri9xcldFZmNpUWYzUitwM0U4czFjK2pkaitGUitaN1VxNC9ocjlmU3Q3c1QrTzhUaWErTXJ6cjFuelRrN3Q5MlMwMTI3RDhhYlRmTTlxMFVURmJqeXhhaW1lWjdVVTdJc3phY2pkaitGRkZhc3FRNmlpaWdrS0tLS0RNZDVudFZ2UTlhMXp3MXJFV3RlSE5XdTlQdjdSdk1odTdPNGFLV0kvM2xaQ0NwK2hvb3JHclRoVmk0VFYwQ25Pbks4V2ZSM3dtLzRLdC90VC9EV0NMVHZGRjlwZmk2empJUUpyOW4rL1ZSNlR4RldadmVUZlh2bmdqL0FJTFYvQzdVQW8rSXY3UCt0V0FIM3BkRjFXRzkzZSsyVVFiZjkzSit0RkZmbjJlK0d2Qm1QZzZzOEtveTd4YmorQ2R2d1BvTUZ4TG5WQmNxck5yejEvTTczUVArQ3ZuN0YrcGtSM2VuK01OTlA5NjgwU0p2L1JVNzExTmovd0FGUnYyQnJsVmt1Zmk5ZFdZL3UzUGhqVUQvQU9pNFdvb3I4OHhIaEx3cE9XbnRJK2tsK3NXZXhoK0xNM244VFQrWC9CTGkvd0RCVEgvZ24xMUh4LzhBL0xZMWIvNUZwMy9EeTMvZ250LzBjRC81YkdyZi9JdEZGY1AvQUJDVGhqK2VyLzRHdi9rVFY4V1pyYjdQM2Y4QUJGWC9BSUtZZjhFODIvNXVDSC9oTGF0LzhpMUt2L0JTai9nbm4xLzRhRFA0ZUdOWC93RGtXaWloZUUzREg4MVgvd0FDWC95SkgrdGVhZjNmdS80SWY4UEtQK0NkMy9Sd2cvOEFDVjFiL3dDUmFhMy9BQVVmL3dDQ2Q3RC9BSk9GWC93bE5YLytSYUtLMmg0UzhMcjdWVC93SmY4QXlJLzliYzEvdS9jLzh5Q1gvZ290L3dBRTdXWC9BSk9IL3dETFYxYi9BT1JhcVhIL0FBVUUvd0NDZDgzL0FEY01mL0NYMWIvNUZvb3JhSGhUd3V1dFQvd0pmL0lsUTR4emxmeS9jLzhBTXk3ejl1Ny9BSUo1Uy9kL2FINDkvREdyZi9JdFpGLysybi93VDRuNUg3UVFQL2N0NnQvOGkwVVYwMGZEUGhwZnovOEFnUy8rUk95bHh6bnNkbkg3ditDYzNybjdhUDdCRnVqUGEvRSsvdmgvZHQvRDE2UC9BRVpHdGNackg3ZS83R0ZxN3JhK0d2R0YrQjBlTFNyZFZQOEEzOG5CL1NpaXZvc0Q0ZGNNdzNoSitzdjhralNmaUh4UEQ0S2lYb3YrQ2NWNG8vNEtWK0NOUDgyMytIZjdQNU1nL3dCUmVhMXFvd3YxaGlUK1VsZVAvRS85dG45b1Q0bTIwMm0zUGl4ZEUwNWp0bDA3dzdEOWtRaHVxczRKbGNIdXJTRVVVVjkzbDNDbVFaYlQ5cFFvSlM3dlg4N255ZVpjYWNTNW8rVEVZaVRqMldpL0N4NU16czR5M1drb29yNkZKSkh6YmJidXdwZDdldEZGQ1ZpbHNOM3I2MG04ZWhvb3ErVkZ4QXllZy9PaWlpblpGSC8vMlE9PSIgYWx0PSJJbnN0YWdyYW0iIGNsYXNzPSJvcHQtaWNvbi1pbWciPjwvZGl2PgogICAgPGRpdiBjbGFzcz0ib3B0LXRleHQiPjxkaXYgY2xhc3M9Im9wdC10aXRsZSI+SW5zdGFncmFtPC9kaXY+PGRpdiBjbGFzcz0ib3B0LWhhbmRsZSI+QHJqX2dyb29taW5nPC9kaXY+PC9kaXY+CiAgICA8c3BhbiBjbGFzcz0ib3B0LWFycm93Ij7ihpI8L3NwYW4+CiAgPC9hPgogIDxhIGhyZWY9Imh0dHBzOi8vd2EubWUvMzcyNTg3MzU0NTYiIHRhcmdldD0iX2JsYW5rIiBjbGFzcz0ib3B0Ij4KICAgIDxkaXYgY2xhc3M9Im9wdC1pY29uIj48aW1nIHNyYz0iZGF0YTppbWFnZS9qcGVnO2Jhc2U2NCwvOWovNEFBUVNrWkpSZ0FCQVFBQWtBQ1FBQUQvNFFEMlJYaHBaZ0FBVFUwQUtnQUFBQWdBQndFT0FBSUFBQUFMQUFBQVlnRVNBQU1BQUFBQkFBRUFBQUVhQUFVQUFBQUJBQUFBYmdFYkFBVUFBQUFCQUFBQWRnRW9BQU1BQUFBQkFBSUFBQUV5QUFJQUFBQVVBQUFBZm9kcEFBUUFBQUFCQUFBQWtnQUFBQUJUWTNKbFpXNXphRzkwQUFBQUFBQ1FBQUFBQVFBQUFKQUFBQUFCTWpBeU5qb3dPRG95TVNBd01EbzFPRG8wTUFBQUJKQURBQUlBQUFBVUFBQUF5SktHQUFjQUFBQVNBQUFBM0tBQ0FBUUFBQUFCQUFBQkJxQURBQVFBQUFBQkFBQUJEUUFBQUFBeU1ESTJPakE0T2pJeElEQXdPalU0T2pRd0FFRlRRMGxKQUFBQVUyTnlaV1Z1YzJodmRQL3RBRGhRYUc5MGIzTm9iM0FnTXk0d0FEaENTVTBFQkFBQUFBQUFBRGhDU1UwRUpRQUFBQUFBRU5RZGpObVBBTElFNllBSm1PejRRbjcvNGdJb1NVTkRYMUJTVDBaSlRFVUFBUUVBQUFJWVlYQndiQVFBQUFCdGJuUnlVa2RDSUZoWldpQUg1Z0FCQUFFQUFBQUFBQUJoWTNOd1FWQlFUQUFBQUFCQlVGQk1BQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE5dFlBQVFBQUFBRFRMV0Z3Y0d3QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBcGtaWE5qQUFBQS9BQUFBREJqY0hKMEFBQUJMQUFBQUZCM2RIQjBBQUFCZkFBQUFCUnlXRmxhQUFBQmtBQUFBQlJuV0ZsYUFBQUJwQUFBQUJSaVdGbGFBQUFCdUFBQUFCUnlWRkpEQUFBQnpBQUFBQ0JqYUdGa0FBQUI3QUFBQUN4aVZGSkRBQUFCekFBQUFDQm5WRkpEQUFBQnpBQUFBQ0J0YkhWakFBQUFBQUFBQUFFQUFBQU1aVzVWVXdBQUFCUUFBQUFjQUVRQWFRQnpBSEFBYkFCaEFIa0FJQUJRQUROdGJIVmpBQUFBQUFBQUFBRUFBQUFNWlc1VlV3QUFBRFFBQUFBY0FFTUFid0J3QUhrQWNnQnBBR2NBYUFCMEFDQUFRUUJ3QUhBQWJBQmxBQ0FBU1FCdUFHTUFMZ0FzQUNBQU1nQXdBRElBTWxoWldpQUFBQUFBQUFEMjFRQUJBQUFBQU5Nc1dGbGFJQUFBQUFBQUFJUGZBQUE5di8vLy83dFlXVm9nQUFBQUFBQUFTcjhBQUxFM0FBQUt1VmhaV2lBQUFBQUFBQUFvT0FBQUVRc0FBTWk1Y0dGeVlRQUFBQUFBQXdBQUFBSm1aZ0FBOHFjQUFBMVpBQUFUMEFBQUNsdHpaak15QUFBQUFBQUJERUlBQUFYZS8vL3pKZ0FBQjVNQUFQMlEvLy83b3YvLy9hTUFBQVBjQUFEQWJ2L0FBQkVJQVEwQkJnTUJJZ0FDRVFFREVRSC94QUFmQUFBQkJRRUJBUUVCQVFBQUFBQUFBQUFBQVFJREJBVUdCd2dKQ2d2L3hBQzFFQUFDQVFNREFnUURCUVVFQkFBQUFYMEJBZ01BQkJFRkVpRXhRUVlUVVdFSEluRVVNb0dSb1FnalFySEJGVkxSOENRelluS0NDUW9XRnhnWkdpVW1KeWdwS2pRMU5qYzRPVHBEUkVWR1IwaEpTbE5VVlZaWFdGbGFZMlJsWm1kb2FXcHpkSFYyZDNoNWVvT0VoWWFIaUltS2twT1VsWmFYbUptYW9xT2twYWFucUttcXNyTzB0YmEzdUxtNndzUEV4Y2JIeU1uSzB0UFUxZGJYMk5uYTRlTGo1T1htNStqcDZ2SHk4L1QxOXZmNCtmci94QUFmQVFBREFRRUJBUUVCQVFFQkFBQUFBQUFBQVFJREJBVUdCd2dKQ2d2L3hBQzFFUUFDQVFJRUJBTUVCd1VFQkFBQkFuY0FBUUlERVFRRklURUdFa0ZSQjJGeEV5SXlnUWdVUXBHaHNjRUpJek5TOEJWaWN0RUtGaVEwNFNYeEZ4Z1pHaVluS0NrcU5UWTNPRGs2UTBSRlJrZElTVXBUVkZWV1YxaFpXbU5rWldabmFHbHFjM1IxZG5kNGVYcUNnNFNGaG9lSWlZcVNrNVNWbHBlWW1acWlvNlNscHFlb3FhcXlzN1MxdHJlNHVickN3OFRGeHNmSXljclMwOVRWMXRmWTJkcmk0K1RsNXVmbzZlcnk4L1QxOXZmNCtmci8yd0JEQUFJQ0FnSUNBZ01DQWdNRkF3TURCUVlGQlFVRkJnZ0dCZ1lHQmdnS0NBZ0lDQWdJQ2dvS0Nnb0tDZ29NREF3TURBd09EZzRPRGc4UER3OFBEdzhQRHcvLzJ3QkRBUUlDQWdRRUJBY0VCQWNRQ3drTEVCQVFFQkFRRUJBUUVCQVFFQkFRRUJBUUVCQVFFQkFRRUJBUUVCQVFFQkFRRUJBUUVCQVFFQkFRRUJBUUVCQVFFQkQvM1FBRUFCSC8yZ0FNQXdFQUFoRURFUUEvQVBtWGRSdW8yMGJSVTZtT29ialNaTkcwMFlOR292VVNpaWlxS2ZrRklUaWxwQ00wbVFNelJSUlFob0tRbkZMU0VacGg2aktLS0tCc0tLS0tDUXB3UGFtMDREdlFNZFNqclNVbzYwQ0gxSlVkU1VBU1ZJS2pxUVVESlIxcVFkYWpIV3BCMW9BZUtsSFdvaFVvNjBBU0RyVHgxcGc2MDhkYVFFbFBIU21VOGRLVEVMUlJSVWdmLzlENW8ybWwybXB0dnRSdDlxU01rUWJUUmcxTnRwTnRBTWhwTUNwdHByMUw0ZS9CajRnZkUxOS9oclRpTEZXMnZlM0I4cTFRanI4NUh6RWQxUU13OUtVcHFLdXpERVY2ZEtMblVra2wxZWg1TnRwTUd2ME44UGZzaitBOUdqU1h4MTRnbjFPNUdDWUxJTGJ3ZzkxTHVIZHg3allhOVgwejRiL0Evd0FQbk9sK0RiVzRQcmRoN3ZQdmk0WngrUXhYaVluaUxDMDNaeXVmQzQveEl5MmkzR01uTitTL3pQeWFwTUN2Mk10VThHNmNjNlY0VjB1ekkvNTVXY0VmL29LQ3QyTHhoYzJ3MjJsdEhDdit3QXY4Z0s4NmZHT0dXeVo0ZFR4Wnc2K0dpMzgwdjBQeFYyK2xKZzErMXg4Y2FzZlQ4NmIvQU1KcnF2dCtkWi82NlVQNVdZdnhkcC85QTcvOEMvNEIrS1ZOMjErMXA4WjZyN2ZuVFQ0eDFNK241MHY5ZGFIOHIvcjVFLzhBRVhZZE1PLy9BQUwvQUlCK0ttMmphYS9haytMdFRQcCtkTVBpelVqNlZMNDFvL3lQK3ZrSitMMGYrZ2IvQU1tLysxUHhaMjA2djJoUGluVVQ2VTArSnI4OWNWTDQycGZ5Zmovd0NINHZmOVEzL2svL0FOcWZqQlRnTzlmczBmRVY4YWpPdlhqRERESTk2bDhiMC84QW4zK1AvQUovNGk5LzFDLytULzhBMnArTmc2MCt2MkJ1TGl6dkFSZDJNRStmNzhhdC9NVno5eDRYOEIzaEp2UEMrbVNrOVMxbkNUK2UzTlZIamFsMWcvdk5xZmk1VCszaDJ2U1NmNkkvS0twSy9TVFZmZ2o4SXRaRDd0Rk5oSy8vQUMwdFpwSTl2MFFsby84QXgydkdQRS83TEV5bzl6NEkxZ1hPT1JiM2dDUGdkaEtueWtuM1ZSNzE2V0c0cndsUjJiY2ZYL2dYUG9NdjhUTXNydmxxTndmOTVhZmVyL2pZK1Jxa3JaOFFlR1BFSGhTL09tZUlyR1d3dVY2TElPR0hxckRLc1BkU1JXS09SWDBjSnFTdkYzUjk5UnJRcVJVNmJUVDJhMVJMVWxSQTVxUUhOVWFFdFNkYWhCN0duZzRwTUNZSE5PQnhVUVBwVHdhVEFsb3BtY1VaTlNCLy85SDUyMjBiYXM3S1RaVTNNN2xmYlNiYXM3YSt5LzJhL2c1WlhjUStLZmpTM0VtbjJ6bit6YmVRZkxQS2h3Wm1CNm9qRENqdXdKUEM0T09JeE1hVUhPYjBSNW1iNXJTd1ZDV0lyUFJmajVFbndXL1pzczJzWVBIUHhXaVpMVndKTFRURGxXbEhWWHVPaFZUMlFjbnEyQndmcSs5MTZSNFVzTk1qU3lzb0ZDUnhSS0VSRUhRS3E0QUE5QlZQVnRXdU5WdVdtbVk3Yy9LUFNzck5mbE9jWi9VeE1tb3UwVCtaZUl1S01UbU5WeXFPMGVpNklVa3NkekhKUGVrb3pSWHpwODJGRkZHYUFDaWpORkFCUlJSbWdBb296Um1nQW9vb3pRQVVVWm9vQUtLS00wQUZLR0tuSzhHa3pSUUJUMXpSZEQ4VzZZK2krSjdOTHkxZnB1SHpJeC9pUnVxdDdnL3BYd2I4Vi9nN3FudzZ1ZnQ5bXpYMmhYRFlpdU1mTkd4NlJ5Z2NBK2pkRzlqd1B2NmxtZ3N0U3NwOUkxYUZicXl1ME1jc2JqS3NwN2Y0SHFEeU9hOTNKczlxWVNmZVBWZjVlWjlSd3h4WGlNc3EzZzcwM3ZIOVYyZjlNL0pzSEZQejNyMUg0dGZEYTUrSFBpSTIwUmFiU3IzTWxuTWVTVkhWR1A4QWVUSUI5Umc5OER5c0hGZnJ1SHhFS3NGVXB1NlovVE9YNCtsaXFNY1JSZDR5VjEvWDVrd09hZURVTk9COWEyT3dtQjlLZURtb1FjVTdjS2xvQ2JKRkdUVWVhTW1nRC8vUzhJMlViS3Q3ZmFrMlZtWjJPdCtISGdtNCtJSGpYU3ZDZHVTaTNzdjc2UWY4czRFQmVWK2VNaEFjWjZuQTcxK25ldVRXZHBIYjZCcEVTMituNmRHa01VUy9kVkl4aFFQb0JYelAreVhvQ1dzWGlieHpPbUdnampzTGRqMHpKKzhtL0VCWS93QUNhOTJra2FSMmtZOHNTVCtOZm52R1dZTzZvUmZxZmdYaWpuRHFZcU9FaS9kZ3RmVi84QVROTG1tWm96WHdYTWZsZzdORk56Um1qbUFkbWx6VE0wbTRVY3dEODBacnNmQy9nSHhONHRaWDAyMktXcE9HdUpQbGpHT3VPN2ZobXZvTHc1OEYvRE9tb2ttc00yclhJNVBWSVFmWURyK0pQMHIzY3Q0ZnhXSnRLTWJSN3ZiL0FJSjlOazNDT094MXBVb1dqL005Rjh1citTWjhvd1EzRjNNdHZheE5OSy8zVVJTekg2QWMxMk5uOE9QSFY4Z2VEUnBncDUvZWJZaitUbFRYMnZZYVhZNmJGNU9uV3NOb21BTVJvQjArZ3E4UWc1WnorZUsrdHczQTlOZnhhamZwcCtkejlGd1hoUEMxOFRXZnlWdnhkL3lQaktINFBlUHBSbHJKSXY4QWZsVCtoTlJUZkNMeC9DY0xwd2wvM0pZLzZrVjluazIzOFRBL1U1cFFiZjhBaGJIME5kditwbUQ3eSs5ZjVIb3Z3c3krMzhTWDNyLzVFK0R0UThFZU1kTEJhOTBpNFZSMUtKNWdIMUtaQXJsZDRCd2VDSy9SM3kxSStWajlNNUg2MXoycitGZEQxdENtcWFmRGM1R054VUJ4OUc2L3JYblluZ2VPOUdwOTYvVmY1SGpZL3dBS0dsZkRWZmsxK3EveVBnVGQ3MHVhK2ovRXZ3TnRaaVovQzl5Ylo4SE1FNUxLVDIydDFINDUvQ3ZBZGIwSFd2RGQxOWsxbTFlM2ZuYVNNcXdIZFdIQnI1RE1zbHhPRjFxeDA3clZmMTZuNXZtL0R1THdUdFhocDNXcSsvOEFwbWRtbHpVUWNHblpyeU9ZOFFkbWltNW96UnpBT3pTNXBtYU0wY3dITGZFVHdoRjhRUEJWN29SVU5mUXI1OW14NmlkQjhvejZPTXFmcm50WDVtTXJJeFJ3VlpUZ2c4RUVWK3NkdEtZNWxZVitlUHh0OFBKNGMrSldyMjhLYkxlOGNYY1hHQmk0RzlzRDBEN2dQcFg2RHdUajIzUER5OVYrcCt3ZUZPYnRUcVlHVDArSmZrLzBmM25sUVBZMDZvNlVIRmZvUisyRWdPS2NDS2ozQ2xvQWtwY21vcVhKb0EvLzAvSGRncE50VzlsR3lzek0vUVA0SVdDNlI4RExLZE91cjNkemN0OVEvd0JuL2xFSzZmTlZmQkZ0L1ovd2I4SlduVGRidEwvMytrZVQvd0JtcWZOZmpYRXRYbXhreitVZUxLcnFabFhsL2VmNEQ4MFpwbWFNMTRGejU2dy9OR2FabW5LcnlPc2NhbG5jZ0tvR1NTZWdBOWFZY285Rmtsa1dLRlM3dVFxcW95U1R3QUFPcE5mU25nVDROeFFMRnEzakZQTWtZQm83TWNnZHg1bnFmYnA2K2xkRjhNL2h0RDRZZ2oxbldJeExyRTY1Ukc2VzZrZE80TGVwL0FkNjloZVJMZFM4aHl4NzErbGNQY0t4Z2xYeFN1K2k3ZXZuNWRQeS9adUVPQTRSaXNWajFkN3FMMlgrTHUvTHAxN0o4VU1jTVN4aFZpaVFZVkVHRkE5T0txWFdxMnRwR3pzd1JFR1NTY0FDdkpmSG54WDB2d3VXczR6OXJ2eU1pRkR3dWVtODl2cDE5c2MxOHFlSWZGL2lMeFhNWDFXNUppSnlJVStXSmZUNWUrUFU1TmVubS9GZERDdHdqNzB1eS9WbnU1OXg5UXdsNldIWE5KZmN2NjdMOEQ2bDE3NDIrRjlNa2FDQ1o3K1JlQ0xjWlgvdnM0VS9nVFhtR29mSHJXSlNWMHpUbzRnTTRNemx5ZlRoZHVQenJ3bEljQ3B4RUsrR3hQRnVNcVAzWmNxOGwvbWZsMk80M3pDczMrOHN2TCtybnA4bnhwOGNTSEttM2ovM1kyL3F4cVdINDErTll2dnJiU2ovQUdvMi9vNHJ5L3l4UytXSzRQN2R4bDcrMWYzbmtyaUhHM3Y3YVgzczk3MHo0K1hDTXE2dHBuQTZ2QS8vQUxLMlAvUXE5YThOL0Zmd3pyenJCQmRpS2MvOHNweHNZK3dKNFA0RTE4VUdNVkUwVmVwaE9NTVhUZnZ2bVhuL0FNQTl2TCtQTXdvTmMwdVplZjhBbWo5SjRydTN1Qmc0NXJNMXJRZE8xcXlleDFHM1c2dDNIM1dISTkxUFVIM3I0cThLL0VueEY0WWxTT1NWcjZ5WGd3eU1TUVA5aHVTUHAwOXU5ZldQZy94MXBYaW16RnpwMHBKWGlTSitKRVB1UDZqSU5mZVpWeERoOGF1VFo5VS82MVAxTEp1TDhIbWNmWVYxYVQ2UHIvbWZPdmp6NFdYM2hwWk5WMFl0ZDZhdVN3UE1rSTkvVlI2OVIzOWE4a1dRR3YwWm1oaXVZeVVBWUVZSVBRajBOZktIeFIrRzM5a3RKNGowR0lpMEozVHdqL2xrVDFjZjdQcU8zWHAwK1Y0azRZVk5QRVlaYWRWMjgxNWVYVDBQZytNZUNQcTE4VGhWN25WZHZOZVg1ZW0zak82ak5WMGt5S2x6M3I0TzUrWmNvL05HYVptak5Gd3NTQnNFSDByNU4vYW0wOVUxbncvckk2M1ZyTEIvMzRjTi93QzFhK3JzMTg0L3RSMi9tYUY0YnZmK2VVMXhILzM4VkQvN0pYMG5DZFZ4eDBGM3YrVFBzZUFhcmhtMUh6dXYvSlgrdGo0Mm9wbVNLTnhyOWtQNmFIMFUzY2FOeG9BZmswWk5NM0dqY2FBUC85VHpMeTZUWlZ6WjdVYlBhc0xtWitqMWlvaCtISGcySWRQN0xzMi83NmdWdjYxblpyUWdQL0ZBK0R4LzFDYkgvd0JKMHJNcjhRenlWOFZQMVA1R3pwM3hsWi8zbitZL05HNm1ab3pYazNQTUhFMTlIL0Jqd09vUmZHbXJSN3VTdG5HdzZrY0dUK2kvbjZWNHI0TjhPeStLL0Vsbm9zZjNKVzNTbis3RXZMbjh1QjdrVjk0VzhWdGJSUndXcUNPMnRsQ1JxT2dDOGNWOXh3YmxDcXplS3FMU08zcjMrWDVuNk40ZmNQckVWbmk2cTkyRDA4NWY4RGYxc1dHa0VLdEpJY3UxZlBIeFUrS0owZnpOQzBWOTJveUREdU9rS3NPM3EzcDZkYTZ6NG9lT0I0VjBacGJkaDl1dWN4MjZubkI3c1I2S09mcmdkNitLaTgxMU05emNPWkpaV0xNekhKSlBKSit0ZXR4WHhFNksrcjBYN3ozZlpmNW4wSEhIRmNvZjdKaDNyMWY5ZFdQSmx1Wlh1TGh6SkpJU3pNeHlTVDFKUGVyS0lBS1JGeGdEa212ZVBDUHdWdjhBVmJhSFUvRXR3ZE90NWVWaFVablplM1hoYy9RbjJyOC93R1hWOFZQbG94dSt2L0JaK1c1ZmxlSXhsVDJlSGpkOWZMMWZROE9HS1hJcjZIOGZmQ25ROUo4TlRhbjRkV1VYRmwrOGs4eHR4ZU1mZTQ2REE1NHgwTmZPS1NicTB6VExLdURxS25WNnErbXc4MnlhdmdxbnNxNjF0ZlFzWm96VE0wVjV0enloK2FUSXB1YU0wWEFHQU5XdEwxVFVkQnY0OVMwcVl3VHg5eDBJN3F3N2c5eFZXa1BOVkNvNHRTaTdORjA1dUxUaTlUN1o4QWVQYlB4ZHAvbnhqeXJ1SEFuaDlDZTYrcW50K3RkL2R3eFhVTGZLSFZoaGdlUVFhL1BuUWRkdi9ET3JRNnZwejdaSWlOeTlCSW1mbVEreC93RHJqbXZ1enc1cnRwcnVsMjJxMlp6QmRLR0FPTXF4NnFjZHdlRFg2NXcxbjMxdW00VlBqanY1cnY4QTVuN3B3ZHhLc2JTZUd4SHhMOFYzL3dBejQ5K0kvZzl2Q090RnJZZjhTKzhKYUUvM0QvRW40ZHZiOGE0Ulh5Sys0ZkhuaGEzOFRhSGNhYklBSkdHK0Z5UHVTTDBQOUQ3RTE4TDdaYmVWN2VkU2trYkZXVTlRUWNFVjhMeFBsU3d0Zm1ndmRsdDVkMGZtbkYyUmZVc1MxRmU3TFZmNUZyTkc2bzg1cGMxODNjK1RINXJ3djlwZU1QOEFEL1NwdTZhaWkvOEFmVU1wL3BYdU5lSmZ0Si84azEwOC93RFVVaC85RVQxN25EY3Y5dXBlcDlKd2U3WnBoLzhBRWZERkZKa1VtNnYzQS9xTFVkUlRNbWtwMkdQM0NqY0taUlJ5aVAvVjRueTZUWlYzWlRkbGM1bWZvTENjZUJmQ0k5TktzZjhBMG5Tc3ZOYUVSLzRvandtUFRTN0wvd0JFTFdYbXZ3ck9wZjdWUDFQNUZ6Yi9BSHFyL2lmNWttYUNhanpTRThWNVhNY0ZqNm4rQm1ncGFhTGVlSnBrSDJpOWZ5SVdQVVJyOTdCOTIvOEFRUlh0OTFNSVlkZ09PUDByRjhNYVdkRThPNlRvemdDUzF0MDh6SFR6R0dXL1hOY2Y4Vk5iazBmd2pxTnhDK3lXUkJDaEJ3UVpEdHlQY0FraXYyL0RRamdzRW92N01idjEzZjRuOUE0SlJ5N0swbXRZeHUvVjZ2OEFIVDVIeXI0LzhTTjRwOFUzTjRqYnJhQStUQjZiRU9Ody93QjQ1TmMxR3VCVkdCY1lyUUhBcjhXeEdKbFZxU3FUM2J1ZmcrS3J5cTFKVko3dDNQYS9ncjRaZzFuWDU5WXY0MWt0ZEpVT0Zidk0vd0J6am9jQUUvWEZmV29Zc2ZOazVZL3A3Q3ZCZmdRMFgvQ01hcGpHL3dDMXJ1OWR1eGNmcm12ZFdiaXYySGhiRHhwNEdtNDd5MWYzL29rZnVYQXVHaFN5K0VvN3l1Mzk3Uys1TDh5SzlqV1dJN3dHVmhnZ2pJSVByWHdUNHcwSi9ESGlTNzBvZytVRzN3azk0MzVYNjQ2SDNGZmZXUVZPN2dWODUvSFBTTFNYU3JYV2d5cGNXMG5sOG5CZEg3RDF3UmtlMmE0dUw4Q3F1RzlvdDQ2L0xyL21jSEgrV3FyaC9icmVQNWRUNTFWdUtkbXFzYlpGVFpyOGw1ajhUYUpNMFpxUE5HYWZNRmlUTkdhanpSbWptQ3c1dWE5eCtCL2lNMnVxWEhoaTVmRVY0RExEbnRLZzVINHFNL2hYaG1hdGFiZnlhVnFkcHFjSkllMWxTUVk2L0tRY2ZqWG81VG1MdzJJaFdYUjYrblg4RDBjcHg4c0xpSVY0OUgrSFg4RDlEWHpOQ1Flby9tSytNL2kvb1NhUDRvRi9Bb1NIVWs4emdZSG1Mdy81OEUrNU5mWWxuZFJYVU1WMUF3YUs1UlpGSU9RUXd6bXZFZmpmcEgydnd5TlFRZlBwMHlzVDMyUDhoSDVrSDhLL1VlSzhJcXVFazF2SFZmTC9BSUIreGNhNFJZakErMVdyanIvWHlQbDlHeUtmbXFjVDVGV00xK09jeCtHdEVtYThVL2FUL3dDU1o2ZC8yRllmL1JFOWV6NXJ4ajlwTG40WTZjZitvckQvQU9pSjY5N2huWEhVdlUraDRRWC9BQXFZZi9FZkN0RkZGZnU2Ui9VRFlVaElGQk9LWlRBWGNhTW1rb3BXQzUvLzFzSHk2YnNxNzVkSVVybHVKbys1WXoveFJmaFllbW1XWC9vaEt6YzFlUnYrS1A4QURBOU5Ocy8vQUVRdFptYS9CczVsL3RNL1Uva0xOUDhBZWF2K0ovbVNaclk4T1c2M3ZpTFM3TitWbXVvVVAwWndEK2xZZWE2LzRmUkxQNDQwV051bjJsRy83NTUvcFhOZ0k4OWVuRjlXdnpNY0pUNTZzSXZxMStaOTJTTisvbFBwZ2ZrSythZmo1cURMWWFWcHc2VHpTU24vQUxaQUQvMmV2bzJSL3dCNU4vdk1LK1VmanhJVzFUU0kreXd5SDhTVi93QUsvV3VMcXpXRHFXNi9xMGZ0SEdsZHJCU1M2djhBVThWaDRGV3MxVWo2VlBtdnh2bVB4Q1I3MThDTlk4blZOUzBLUThYa1N5cDZib2pnajhRMmZ3cjZkVjl5ZzE4QStHOWRuOE42OVphMUQ4eHRuQlpmN3lIaGgrSUovR3Z1Mnl2cmUrdDRyMjBjU1c5MGdrallkQ0c1cjlXNE56QlZNTDdGdldEL0FBZXY1M1AxN2dMTTFMRHZEdDZ4ZjRQWDg3L2VqTzhTK0liTHcvcGx4cWVvTVZ0N1laYkF5V0o0QUE0NUo0RmZFbml6eGpxZmpMVXplWGhNZHZHU0lZQWZsUmZYM1k5ei9UQXI2KzhlZUhENG84UDNta0l3U1dZQjRpYzRFaUhLNXgyeU9hK0hEYnpXYzhscGRJWTVvV0tPcDRLc3B3UWE4ampiRTExS05QN0QvRm5qOGQ0cXY3UlUzOEg1di9nRm1QZ1ZQbW9CVHMxK2Y4eCtjc2t6UmtWSG1qTlBtRVM1b3pVV2FNMGN3RW1hYXg0cHVhUW1qbUN4OXZmRFhVRzFEd0xvdHczM280MmdQL2JKaWcvUmFmOEFFSzJXNzhLYXZDd3ptMWtZZjd5cVNQMUZjdjhBQmE0YWJ3SHNiZ1cxNUlxL1FoVy85bU5kOTRnQWsweTdROUdna0g2ViszNGVYdGNERG02d1g1V1AzYkN6OXJsVWVickJmbFkrQUlXNEZYUWF6SUR4VjRHdnhEbVB3dVNKczE0MyswbC95Uy9Uai8xRllmOEEwUlBYcithOGgvYVIvd0NTV2FhZitvckQvd0NpSjYraDRWZiszMHZVK2g0Ui93Q1JuaC84UjhKMGhPS1dtRTgxKzhuOVBJU2lpbWs5aFFBcElGRzRVeWlnT1kvLzE2K3lrS1ZjS1UwcFhHQjlqb2YrS1M4TmowMDYwLzhBUksxblZjamIvaWxmRHc5TEMxLzlFclZITmZnT2JTLzJtZnF6K1E4eC93QjRxZjRuK1krdWs4RnovWi9HT2l5ZWw1Q1ArK25BL3JYTVpxVzF1NUxHOGd2b3Z2MjhpeUw5VUlJL2xYTGhhL3M2c1o5bW1ZVUtuSk9NdXpQMExtYkUwdzl6K3RmTDN4NWhJdU5FdVFPQ3M2RS9UWVIvTTE5S201aHVSSGVXN0I0YnFOSlVZSElJWWNFZlVWNHQ4YXRPTjM0VEY0Z0c2d3VFYyt1MThvZjFZSDhLL1llS2FmdE1KVlM5ZnVkL3lQMlRpbUh0Y0ZQbDlmeHYrUjh3UkhpcDgxU2hiaXJXYS9GN240dTBEY2l2b2o0TWVNQkpDL2cvVUpNUEhtUzBZbnFQNG8vdzZqMno2Q3ZuYk5MRGNUMmx4SGQyc2hpbWhZT2pyMVZsT1FSWHA1VG1rOEpYVldPM1ZkMGVqbFdZend0ZFZvL1B6UitoSlBtTGc4TVAwTmVIL0ZMNGRuVzRuOFI2RkVXMUdFZnZvVUhNeWp1bzd1QitZNDY0ejFIZ0h4M2JlTWRQQWtaWXRWdGxBbmk2QngyZFBVSHY2SGc5aWUvRW1jT2h3dy96ZzErdDFxZUh4K0hzOVl5MmZWZjhGSDY5V2hoOHl3M2RQNzEvd1VmbjRyRlNVY0ZXVTRJUEJCRlM1cjZxOGNmQzZ3OFdPK282TVVzTlZQTEFqRWMzMXgwYi9heDlRYStkSlBCZmkrSFVocEQ2UmNHNlp0b1ZVTEErKzRaWEh2bkh2WDVkbWVRWW5EVHR5OHllelhYL0FDZmtmbE9aNURpTU5QbGNicDdOZGY4QWcrUml3UXozTW9ndG8ybWtib3FBc3grZ0dhaEp3U3A0STlhKzB2QW5nYXg4QTJSZHl0enJOd284NlRxc1EvdUo3ZXA2bjZZRlRlSnZCZmhieFlDK3JXbmszUUJBdWJmNUg1OWNjTi93SUgycjNGd1JXOWlwT2FVLzVYK1YrLzRlWjdxNEhyK3dVM0pLZjh2L0FBZS85WFBpak5GZXJlSnZnNzRpMFpXdTlGY2F0YUR0R01UQWU2ZC8rQWtuMkZlVE9KSVpHaG1ReHlJY01yREJCSHFEelh5ZU53RmZEeTVLMFd2NjZQWm55ZUx3RmFoTGtyUnMvd0N2dkpLUW5GTURVRWs4RHFhNHJuSWZZL3dqdGhiZkR1eGtBMm03dUpwRDc0WXAvd0N5aXVqOFZYWDJiUXRTdVQveXl0WlcvSlNhdGFKWW5SdkR1ajZRNEFlMXRZdzRIVGVRTjM2NXJpZmlocVFzUEJXcXlnL05NaXdLUFh6R0NuOUNUWDdmUC9aOEVveSt6QmZlbzYvaWZ0MDE5WHk1UWYyWUw3N2EvaWZHa0I2Vm9BOFZuUWRCVjRHdnhDNStKeTNKSzhrL2FSLzVKVnBwL3dDb3RCLzZUejE2eG12S1Aya1ArU1VhYWY4QXFMUWYrazg5ZlNjSlAvYjZmcWZRY0pmOGpPaC9pUGhHbzZrcU92M3MvcHNRbkFwbE9hbTBDQ2lrSnhTYnFBUC8wTDVTbUZLdkZLaktWeGpzZlZDTi93QVV6b0k5TEcyLzlGTFZITldZei94VHVpRDBzcmIvQU5GTFZUTmZ6MW1jdjlvbjZzL2tMSC83eFUveFA4MlB6VFc2VW1hQ2E0ZVk1TEgyUjhOTllUVi9BdW50dXpMcCtiV1FlZ1g3di9qdTJ1ZzF2VElOYTB5NzBtNE9FdkltanoxMmtqZy9VSGtWODgvQmJ4SU5PMTZYdzdkdUJhNnNNTHU3VHA5M0grOE1qM08ydnBPWGRHeFJ2dklmMUZmc21TWTJPS3dVWExXeTVYOHY4MVkvWHNreGNjVmdveGwwWEsvbC9tckh3VGNXdHhwdDdQcDkydXlhMmRvM0hveW5CcDROZXkvR1R3cThWeW5pNnhYTVUrMk81VUQ3cmdZVnZvUndmZkhyWGljYkFpdnlyTThGTERWNVVuOHZUb2ZtR1pZS1ZDcktuTHArUll6U0dtNW96WEJ6SEJZc1dPb1gra1hzV282Wk0xdmN3bkt1dkI5eDZFSG9RZUNPRFgxVjRIK0pXbmVLa1d6dkN0bnF3SE1mUkpmZENmNUhuNml2a3c4MUNWSU81VGdqa0VIR0s5aktNOHE0T1Y0NnhlNi9yWm5zWlZuRlhDU3ZEVmRWL1hVL1FNdms3VzRQK2VsVzExQzdSZGdtSUE5aG44OFpyNUw4TC9GL1Y5S1dPeThRb2RTdFFjZVpuOStvK3A0Zkh1UWZldmZ0RThXK0h2RU1hdnBOL0hJN2Y4c25PMlVlMjA4L3BpdjFETE9JcUdJWDd1Vm4yMmYvQUFUOU55M2lPalhYdXlzKzJ6LzRKMWJUWUJ3Zng3MUdzNXFFN2gxVS9oelRSbm9GSnIxK2M5bjJxN2x3eVkrWmVHUEhGZkl2eGIxSzJ2UEc4NldvSCtqUnh4T3k0K1p4bGpuSGNidHArbUs5eDhjZVA5TzhJV0xxanBjYXJJQ0lvQVFkaFA4QUZKamtLUHpQUWR5UGo4eXpYVThsMWN1WkpaV0x1emNsbVk1SlAxcjREakxOSU9LdzBYZDN1L0krQTR2ektFMHFFTzkyWFZPUm11LytHZmgxL0Uzakd4dE1mdUxadnRFeHh3STRpRGcvN3h3djQxNTZEZ1Y5aS9EUHdzL2c3d3UxemVqYnFXc2hYWlNNR0tFRDVWUHZ5U2ZyanRYZzhNWmI5WnhVZVplNUhWL292bTlENS9oeksvck9KU2t2ZGpxL1R0ODNvZWlYVnlKWjVKZ2ZsSndQb09CWHpmOEFIWFdCNU9tYUNqZk16RzVrSHBnYlUvbTM1Vjc0OHFLcGFSZ3NjWUxNVDBBSHJYdzc0dThRU2VLZkV0NXE3Y1J1MnlJZWtTY0wrWTVQdVRYMlhHT1k4bUhjTDZ6Zi9CWjlweGZtSExROW4xbC93N01pRWNWYXpWZU1ZRlM1cjhyVFB5MW9mbXZMZjJqL0FQa2t1bUgvQUtpMEgvcFBjVjZmbXZNUDJqditTUjZaL3dCaGVELzBudUsrbTRRZiszMC9VK2k0UlgvQ25ROVQ0UXFPcEtqcjk5UDZaR3RUYWMzV20wQXhoUE5KUlJRSS85SHBDbFJsS3VsYWpLMXhYTHNmUlVUWjhQYU5qdGFXNC9LTUNxK2FacDhubStHdEtiMGhWZjhBdm5qK2xKWDg3WnBwaWFpODMrWi9JbVl3dGlhcWY4MHZ6Wkptak5Nb3pYQmM0elMwYTJ1YnpXdFB0Ykp6RmNUWEVTUnVPcXN6QUJ2d1BOZmRtcXV2MjExVS9kQVVuMUk2MThzZkJYVEYxSHg1YjNFZy9kNmRGSmNOK0EyTCtyQTE5SlR6bTRuZHgxa1luOHpYNmh3WlI1TUpPcS90Uy84QVNWL3dmd1AwWGhHanlZYWRSL2FmNUwvZ25sdnhrMWdXSGhLUFQxT0pkU21DNDc3SS9tWWo4UW8vR3ZtS0p1SzlNK05Hcm0rOFdSNldqWmkwMkZVeG5nUEo4ekg2NDJnL1N2TVlqZ1Y4ZnhOaS9hNHlYYU9uM2IvamMrVzRneEh0Y1RKOXRQdS80SmJ6Um1vZzJlbk9LTndyd0xuaDJKYzBtYzB6TkxtbGNCR1VHb0dqd2NqcjYxUG1pbmNFemZzZkcvakxTMTJXZXJYQVVEQUR0NW9BOUFKTndINFZZdlBpTDQ3dm9qQlBxOHFxZitlWVNKdisrbzFVMXk1QXBOb3JyV1kxMHVWVkhiMVoxUnh0WkxsVTNiMVpXMnZKSVpKV0x1eHlXSnlTZlVrMVlWZHRPQXlRcWpKUEZlLytBUGhDMXdzZmlEeHVyV2xrQ0RIYXR4Sk54a0Z1NnI3Y0UrdzY2NWJsbGJHVlBaMFZmdStpODJ6WEFaZld4VlQyZEpmNUx6YjZFZndsK0hZMUNSZkYzaUtJcnB0cVEwRWJESG55QTVCOTFCLzc2UHNEWHYxM2V2ZFROTy9WdUFQUWRoVVY5cVAyblpGRWdodDRnRmpqWGdLQndPQlhJZUsvRmRqNE4waHRVdnZudUpNcmJRZDVIeCtnSDhSN0QzSUZmck9GdzlEQVlma2k5RnEzM2ZmL0FDWDZzL1RzTFFvNEhEOGtYb3RXKzcveTdMOVdjUDhBR0R4Zi9aT2xqd3pZeUQ3WnFDNW5JUE1jUHB4M2ZwOU0rb05mTkVDWXA5OWYzdXM2aFBxbW95R1c0dVdMT3g5ZXdIb0FPQU93NHFTTVlGZmxXY1pvOFZYZFI3YkwwUHpiTmNmTEVWWFVlM1QwSmh4VHMweWpOZVZjOHNmbXZNLzJqeUI4STlMSGM2dENmeXQ1L3dER3ZTTTE1Wiswdk1JL2h6b0Zwbi9XMzVmL0FMOXhNUDhBMmV2cStDMWZNS1o5THdkQytaMFBYOUdmRGRSMUpURDFyOStQNlRHTjFwdE9hbTBBUjBVcDROSlFCLy9TN1lwVVpTcmhXb3l0Y0Z6UTlnOFB1SlBDbGlBZVU4eFQvd0I5dC9RMWJ6V0Y0TW1FdWkzVnAvRkRMdS9CeHgrcW10bk5mei94RlNkUEhWb3Z1MzkrdjZuOHI4VDRaMHN5eEVIL0FETi9lN3I4eVROSVRUTTBoUEZlTmM4S3g5UWZCR3grdytGZFoxMWxJa3ZKbHQwSjQrV01aSkg0c2Z4RmVrMmJJOTBnZHRvSi9Xc2JRYk50RThDNkZwUlVvN1EvYUpBUmdoNWZuSVBvUnVJSXBOejVyOXJ5K2o3RERVcVBaSy9xOVgrWityWUdIc3NQU3A5a3Z2ZXIvTTgzZjRLM21zNjFkNjM0bjFxRzFGM08wdmx3Z3l0c1k1QzdtMjRJR0IwSXJzOU8rR2Z3MDBuYVpMYWZWSkZPY3pTRlY0OWwyajh4VzdIRGN5SENxZnlxTzhtc05NVU5xMS9CWmcvODlaRlVuNkFrWnJDbGxtRHBYbjdOTjk1YS9ucCtCaFR5L0MwL2U5bW0rOHRmejAvQTNiVzgwN1RvemJhWHB0dFoyN2ZlampqVlF3NzV3Qm44cTgxOFJmQ1h3MTRoZVM3OFBULzJUZVNFdDVUZk5BekhKNEhWZWZUZ2YzYXVXL2pmd05kWG8wNkRXb3pPZWhZTXNaUG9ISUMvclhUeVF6eEtIKzhoNUREa0VWdFYrcjR1SEpOS2NWMnRwNlcyTnF2c01USGtrbEpMdDA5TGJIeS9yWHczOGJhQXpmYXROa25pVThTMjQ4MUNQWDVlUitJRmNSdnh3ZUQrVmZidHZxdDdhNEVjaEFIWThqOWFmZFgybWFvdTNXZE10YjhEcDVzU3NSOU53T0srWnhIQnRDVHZScU9QazFmOFZiOGp3SzNDMUtUdlRxTmVxdjhBaXY4QUkrSU40bzMxOWl5K0dQaHZlTUh1dkQwU01PUDNUTkdQeVFxS21nMEw0ZTJBMjJuaHUyZnZtYjk2Zi9IOTM4NjRsd1JVdnJXamIvdDcvSTVGd25VNjFZMi83ZS95UGp1Mmd1cjJaYmV6aGVlVjg3VWpVc3h4eWNBYzE2aDRmK0RmalhXMlY3dUJkS3RTTW1XNU8wNDlrR1d6OVFCN2l2cEtIWEZzWXZzMmsyc0ZqQ09pUXhxb0g0REEvU3FOeHFWNWRITTByUDdFOGZsWHA0WGcvQ3dkNjAzUHlTNVY5K3IvQUNQUXcvREdIZzcxWnVYa2xaZmZxL3lNN3czNEo4R2VDV1M2dFVPcmFtZy80K0poOGluMVJPUVBZOGtldGJ0emYzRjdMNWt6RjJQVDIrZ3JOa0MyOERYZC9NbHJib010Skl3VlI5YzRyeUh4VjhZckRUZzloNFFRWGMrQ0d1bkg3dFQwK1JmNHZxY0Q2MTd1SXgrSHdkTGwwaEhzdXY2dCtiUFlyWXlqaGFmS3JSajJYOVhmcXowanhUNHQwandYWS9hdFNjUzNiak1Gc3ArZHo2bnJoZlVuajB5ZUsrU1BFSGlMVmZGbXF2cTJyU2JuUHlvZzRTTkFjaEZIWUQ4eWVUeldWZDNWOXFsNUpmNmxPOXpjekhMTzV5VC9BUFdBNEE2QWNDbnhwaXZ6WE9jL3FZdDhxMGd1bjZzK0h6VE41NGgyMmoyL3pIeHFCelZnVkdLWE5lRGM4VWt6Um1vODBacDNGWWtCTmVLL3RUekNQU1BCMWlEeWZ0c3JEL3Z5RlA4QU92YmJWRE5jUnhqK0poWHk3KzFEcXlYZmorMDBhSnNycEZoREU0OUpaUzB4L05IU3Z1L0Q3RHVXTjUreWYrWDZuMm5oL2huUE00U1gyVTMrRnYxUG1vOWFZMVBQV210MHI5eFA2QkdIcFVkU1ZIUUExcWJUeU0wbTJnTEgvOVAwWXJVTExWeGxxSmxyenpRNkR3aGZDejFZUVNIRWQydmxuL2VQS244K1B4cnZKME1VcktSWGp4eXBERGdqa2UxZXM2YnFDNjVwNno1LzBxSEN5ajFQOTc2SCtkZmwzSDJVdm1qaklMVFovby8wKzQvRmZFL0k1UnFSeDlOYVBTWHIwZnoyK1M3a21hdmFWSGFUNnJadzZnNGp0WG1RU3N4d0JIdUc0NStsWnA0NjBocjgyaEpKcDJ1Zmt5ZG5jK3A5ZCtNUGdtT1pwWWZQdjJ4aFJGSHNRWTZBbHlweDlBYTh6djhBNDQ2eTVLNlBwZHZacjZ5RnBXL01iQitsZVBGTTBnakZmUVlyaXJHVlczelc5Ri93N1BicjUvaWFqdnpXOVA2dWRWcVh4RDhjYXNTTGpWcG9rSis3QVJDQUQyK1RCUDRrMXh4amFSMmtsSmQyT1NTY2tuMXpWb0lLZUFCWGgxOFRVcU85U1RmcXp5cW1Jbk4zazdsVHlSWFhhQjQzOFUrR0FzV2wzcmZaMS81WXlmUEYrQVBUL2dPSzUya0lGS2hpSjA1YzFPVFQ4aFU2OG9QbWk3TTl6MDM0M1F5RUo0aDBuNnkyemMvOThQOEEvRjExMXQ4VFBoN2RLR2UrbHRHUDhNc1Q1SDRxR0g2MTh0RkJVWmlyMzZIRmVMZ3JTYWw2ci9LeDY5TFA4UkZXYlQ5VDY5WHhmNEZrNVhYb0FEejgyUi9QRlJ5ZU5QQVVBM1NhNUV3LzJGWmorZ05mSVpoRko1SXJyZkdkYitSZmovbWRQK3NsWCtWZmovbWZVZDM4VnZBTmx6QkpjMzU5SW9pby9PVFpYRDZuOGNkVGNOSG9HbVJXWVBBa2xKbGY2Z0RhQWZZNXJ4WVFnZHFrRVlyaHhIRk9McWFLU2o2TC9oemxyWjdpSkt5ZHZRdjZ4cjJ2ZUk1aFByZDdKZEZmdWhqaEZ6L2RVWVVmZ0t6VWh4MnFjS0JUK0JYZzFLc3B2bW03czhpZFZ5ZDI5UVZRS2t6VE0wWnFMbVJKbWtwbWFNMFhBa3pTWnBtYW50b0picVpZSWhsbU5DMTBRRzdvUzIxdTArc2FpNGhzN0NONXBYUFJVakc1aitBRmZteDR4OFJYSGkzeFJxbmlTNUJWOVJ1SG1DazUySXgrUlBvcTRVZlN2clQ5b1R4M0I0ZjBKZmhwbzB1YjI4Q1NhaXluL1Z4ZmVTTEk3dWNNdy91NEg4VmZGTFYrNmNDNU04UGgzV212ZWwrWC9CUDIvd0FPc2tsUW9TeFZSV2M5dlQvZy93Q1JHMU1QU3BEeUtaWDNaK2pFZE1QV3BEd2FhUlFBeWlpaW1JLy8xUFV5dFFsYXRzdFFzSzg1R2hWWmFuMDdVYm5TcnRicTJQSTRLbm95bnFEVEdGVjJGUldveHFRY0pxNmU1amljTlRyVTVVcXF2RjZOSHJ0dFBhYXhiZmJiQThqNzZIN3lIMy9vZTlRRUVIQjRyeTIwdnJ2VHJnWE5uSVkzSHAwSTlDTzRyMEN3OFZhWHFJRVdvZ1djL3dEZS93Q1daL0hxUHgvT3Z4N1ArQzYySGs2bUdYTkR0MVgrZnFmZ1hFM0FHSXdrblZ3cWM2ZmxySmVxNnJ6WHpOS2lyaHNuWkJMQXdsamJrTXBCQkgxRlYyaGxYcXAvS3ZoMnJPeCtmZVJIUlJ0UHBSUUFVVWxMNzBnRW9vb3BnR0JSaWlsOTZWZ0V4UlJSVEFXaWtwYVFCUlFBVDBHYWVJcFQwUmo5QlRzQXlpcjhPbDM4NXhIQ3h6V2pkYVZZNkZhZjJuNHExQ0hTN1VmeFRPRTNIMFhQTEgySE5kTkRCMWFqNWFjV3k2ZE9VNUtNRmR2c1k5dGF6M2NvaGdVc3hyRCtJdnhHMG40VGFZMWpaRkx6eFJkSis3aU9HVzJWaHhKS1AvUVY3OVR4MTg4OGJmdEQ2ZnAxdkxvL3d5Z1BtRUZXMUdkTUVlOE1iZC85cC84QXZudlh5TmUzZDFmM010N2V6UGNYRTdGNUpKR0xPN0hra2s4a212MUhobmdaeGtxK01YeS96UDFEaGZnR2NwS3ZqbFpMYVBWK3ZiMEdYOTllYW5lejZocUV6WEZ6Y3Uwa2tqbkxPN0hKSlBxVFZBMUszV296MXI5VFdtaCt4cEpLeUlxWWVEVWg2MDA5S29DTWltVkpURHdhb2tpUEZGT0lwTUdnRC8vVjljWVZFd3F3M1NvV0ZlY2FGWmhWWmhWdHFyTlRBck9LcXlDcmIxVmVxaUFsdmYzMWd4YXl1SGdKNjdHSXo5Y2RhM0lmSGZpR0QvV1NSM0FIYVNNZnpYYWYxcm1YcW80cml4V1Y0YXZyV3BxWHFsK1o1T095VEI0aDNyMG95ZmRwWCsvYzdzZkVqVWdmM3RsYnNQOEFaRGovQU5tTlRENG5oZjhBVzZTamZTVWorYW12TkhxbzllWFBoSExwYjBWOTdYNm5pVk9CY3BscTZDK1RrdnlaNnVmaXRhTDEwVFAvQUc4Zi9hNllmaTVaci96QXYvSm4vd0MxVjQrOVZIcExnM0xmK2ZYNHkvek1Id0JsUC9Qbi93QW1sLzhBSkh0SC9DNDdKZXVnWi83ZXYvdFZJZmpYWUwvekx1Ziszci83VlhocjFWZXRJOEhaYi96Ni9HWCtZdjhBVUhLZitmUC9BSk5ML3dDU1BlajhjdFBIWHcxbi90Ny9BUHROTS80WHhwcTlmREdmKzN6L0FPMDE4K3ZWUjYwWENHWGY4K3Z4ZitZLzlRc3Avd0NmUC9rMHY4ejZMUHgvMHhmK1pXei9BTnZ2L3dCb3B2OEF3MExwUTYrRTgvOEFiNy85b3I1c2VxclUvd0RWTEx2K2ZYNHYvTVQ0RHluL0FKOC8rVFMvelBwdi9ob3ZTUi96S09mKzM3LzdSVGYrR2tOS1hrZUVCbi9yKy84QXRGZkx6VkEzU3FYQ21YLzgrdnhmK1lmNmk1Vi96NS9HWCtaOU96ZnRLcUFmc3ZoV0JEL3QzTFAvQUNSYXhadjJtUEZZSkZub3VtUkE5TnlUT1IvNUZVZnBYenEvZW9XNjEwMCtHOERIYWtqYW53YmxrZHFDL0YvbXoxM1Z2ajc4VWRUUjQ0OVZGaEUvOE5yREhFUjlIMm1RZjk5VjVIcVdwNmxxOTAxN3ExM0xlM0Q5WkpwR2tjL1ZtSk5WMjZWRTFlcFF3bEtscFRpbDZJOXJDNWZRb2FVYWFqNkpJaFBwVVI2Vk1ldFJIMHJvT3NpYnBVVFZLZWxSbnBRTWpQU21WSlVkTkNJenhUV3A3ZGFhZWxOTVRHVVVVVlFqLzliMkE5NmhhcFdOUXNhODQwSVc2MVdicFZocXJOVEFydFZaNnN2VlZ6VFFGVjZxUFZwNnFPYXNtUldlcWIxYmVxajBFc3B2VlI2dHZWUjYwUkRLajFWZXJUMVZlcWlJcVBWUjZ0dlZSNm9DczlWVzYxWmVxclVFc3J0VURkS25hb0c2VUVrRDk2aGJyVXo5NmhiclZpSVc3MUMxU3QwcUpxWUVSNjFHZXRTSHJVUjlhQkVaNlZHZWxQUFNvejBvR01waDYwK282YUFhMU1QU25OMXBwNlUwU3hsRkZGVUkvOWYxeGpVTEdrTEdvV1kxNXlScFlHTlYyTk9aalZkbXBnTlkxVmtOVE1hcU9hcUlFTG1xcm1wbk5WWE5VUXlGelZOelZoelZWNkNXVm5xbzlXSE5WWE5hSWxsWnpWVnpWbDZxT2FwQ0t6bXFqbXJEMVZjMVFteXU1cXMxVFBWZHU5QkJDMVYycVp1OVFOVFFFTFZDeHFWcWdicFZvUkUxUkhyVWpWQ2VsQUREVVI2VkkzU29tcGszSTJxTnZTbmsxRlFNUW5pbVVIbW1rOXFhQWJUV1BhbEp4VEtaTERPS1RJcHBPYVNtQi8vWiIgYWx0PSJXaGF0c0FwcCIgY2xhc3M9Im9wdC1pY29uLWltZyI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJvcHQtdGV4dCI+PGRpdiBjbGFzcz0ib3B0LXRpdGxlIj5XaGF0c0FwcDwvZGl2PjxkaXYgY2xhc3M9Im9wdC1oYW5kbGUiPiszNzIgNTg3IDM1NDU2PC9kaXY+PC9kaXY+CiAgICA8c3BhbiBjbGFzcz0ib3B0LWFycm93Ij7ihpI8L3NwYW4+CiAgPC9hPgogIDxhIGhyZWY9Imh0dHBzOi8vd3d3LmZhY2Vib29rLmNvbS9zaGFyZS8xRUxQNktDNnJWLz9taWJleHRpZD13d1hJZnIiIHRhcmdldD0iX2JsYW5rIiBjbGFzcz0ib3B0Ij4KICAgIDxkaXYgY2xhc3M9Im9wdC1pY29uIj48c3ZnIHdpZHRoPSIzNiIgaGVpZ2h0PSIzNiIgdmlld0JveD0iMCAwIDI0IDI0IiB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciPjxjaXJjbGUgY3g9IjEyIiBjeT0iMTIiIHI9IjEyIiBmaWxsPSIjMTg3N0YyIi8+PHBhdGggZmlsbD0id2hpdGUiIGQ9Ik0xMyAxMC41aDJsLjUtMi41SDEzVjYuNWMwLS43LjItMS41IDEuNS0xLjVIMTZWM3MtMS0uMi0yLS4yYy0yLjEgMC0zLjUgMS4zLTMuNSAzLjVWOEg4djIuNWgyLjVWMThIMTN2LTcuNXoiLz48L3N2Zz48L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im9wdC10ZXh0Ij48ZGl2IGNsYXNzPSJvcHQtdGl0bGUiPkZhY2Vib29rPC9kaXY+PGRpdiBjbGFzcz0ib3B0LWhhbmRsZSI+UiZhbXA7SiBHcm9vbWluZzwvZGl2PjwvZGl2PgogICAgPHNwYW4gY2xhc3M9Im9wdC1hcnJvdyI+4oaSPC9zcGFuPgogIDwvYT4KICA8YnV0dG9uIGNsYXNzPSJvcHQiIG9uY2xpY2s9IndpbmRvdy5sb2NhdGlvbi5ocmVmPSd0ZWw6KzM3MjU4NzM1NDU2JyI+CiAgICA8ZGl2IGNsYXNzPSJvcHQtaWNvbiI+PHN2ZyB3aWR0aD0iMzIiIGhlaWdodD0iMzIiIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSJyZ2JhKDI1NSwyNTUsMjU1LC40NSkiIHN0cm9rZS13aWR0aD0iMS42Ij48cGF0aCBkPSJNMjIgMTYuOTJ2M2EyIDIgMCAwMS0yLjE4IDIgMTkuNzkgMTkuNzkgMCAwMS04LjYzLTMuMDdBMTkuNSAxOS41IDAgMDEzLjA3IDkuODJhMTkuNzkgMTkuNzkgMCAwMS0zLjA3LTguNjdBMiAyIDAgMDEyIDFoM2EyIDIgMCAwMTIgMS43MmMuMTI3Ljk2LjM2MSAxLjkwMy43IDIuODFhMiAyIDAgMDEtLjQ1IDIuMTFMNi45MSA4LjkxYTE2IDE2IDAgMDA2IDZsMS4yNy0xLjI3YTIgMiAwIDAxMi4xMS0uNDVjLjkwNy4zMzkgMS44NS41NzMgMi44MS43QTIgMiAwIDAxMjIgMTYuOTJ6Ii8+PC9zdmc+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJvcHQtdGV4dCI+PGRpdiBjbGFzcz0ib3B0LXRpdGxlIiBkYXRhLWkxOG49ImNhbGxfdXMiPkNhbGwgVXM8L2Rpdj48ZGl2IGNsYXNzPSJvcHQtaGFuZGxlIj4rMzcyIDU4NyAzNTQ1NjwvZGl2PjwvZGl2PgogICAgPHNwYW4gY2xhc3M9Im9wdC1hcnJvdyI+4oaSPC9zcGFuPgogIDwvYnV0dG9uPgogIDxkaXYgY2xhc3M9ImhvbWUtZm9vdCI+CiAgICA8c3Bhbj5UYWxsaW5uPC9zcGFuPjxkaXYgY2xhc3M9ImZkb3QiPjwvZGl2PjxzcGFuPkVzdG9uaWE8L3NwYW4+PGRpdiBjbGFzcz0iZmRvdCI+PC9kaXY+PHNwYW4+QWxsdmVlbGFldmEgNDwvc3Bhbj4KICA8L2Rpdj4KPC9kaXY+CjwvZGl2PgoKPCEtLSBCT09LSU5HIC0tPgo8ZGl2IGNsYXNzPSJzY3JlZW4iIGlkPSJib29rU2NyZWVuIj4KPGRpdiBjbGFzcz0iY29uIj4KICA8YnV0dG9uIGNsYXNzPSJiYWNrLWJ0biIgaWQ9ImJhY2tCdG4iIGRhdGEtaTE4bj0iYmFjayI+4oaQINCd0LDQt9Cw0LQ8L2J1dHRvbj4KICA8ZGl2IGNsYXNzPSJsb2dvLXJqIj5SJmFtcDtKPC9kaXY+CiAgPGRpdiBjbGFzcz0ibG9nby1zdWIiIGRhdGEtaTE4bj0ibG9nb19zdWIiPkdyb29taW5nIMK3INCi0LDQu9C70LjQvTwvZGl2PgogIDxkaXYgY2xhc3M9InByb2dyZXNzIj4KICAgIDxkaXYgY2xhc3M9InBzIGFjdGl2ZSIgaWQ9InBzMSI+PGRpdiBjbGFzcz0icGRvdCI+PC9kaXY+PHNwYW4gZGF0YS1pMThuPSJwc19zZXJ2aWNlIj7Qo9GB0LvRg9Cz0LA8L3NwYW4+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwbCIgaWQ9InBsMSI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwcyIgaWQ9InBzMiI+PGRpdiBjbGFzcz0icGRvdCI+PC9kaXY+PHNwYW4gZGF0YS1pMThuPSJwc19tYXN0ZXIiPtCc0LDRgdGC0LXRgDwvc3Bhbj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBsIiBpZD0icGwyIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBzIiBpZD0icHMzIj48ZGl2IGNsYXNzPSJwZG90Ij48L2Rpdj48c3BhbiBkYXRhLWkxOG49InBzX3BldCI+0J/QuNGC0L7QvNC10YY8L3NwYW4+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwbCIgaWQ9InBsMyI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwcyIgaWQ9InBzNCI+PGRpdiBjbGFzcz0icGRvdCI+PC9kaXY+PHNwYW4gZGF0YS1pMThuPSJwc19kYXRlIj7QlNCw0YLQsDwvc3Bhbj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBsIiBpZD0icGw0Ij48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBzIiBpZD0icHM1Ij48ZGl2IGNsYXNzPSJwZG90Ij48L2Rpdj48c3BhbiBkYXRhLWkxOG49InBzX2RldGFpbHMiPtCU0LDQvdC90YvQtTwvc3Bhbj48L2Rpdj4KICA8L2Rpdj4KCiAgPCEtLSBTdGVwIDEgLS0+CiAgPGRpdiBjbGFzcz0ic3RlcCBzaG93IiBpZD0iYmsxIj4KICAgIDxkaXYgY2xhc3M9InNsYmwiIGRhdGEtaTE4bj0ic3RlcDFfbGJsIj4wMSDCtyDQn9C+0YDQvtC00LA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImJ3cmFwIj4KICAgICAgPGRpdiBjbGFzcz0ic2JveCI+CiAgICAgICAgPHNwYW4gY2xhc3M9InNpIj7wn5SNPC9zcGFuPgogICAgICAgIDxpbnB1dCBpZD0iYklucHV0IiB0eXBlPSJ0ZXh0IiBwbGFjZWhvbGRlcj0i0J3QsNGH0L3QuNGC0LUg0LLQstC+0LTQuNGC0Ywg0L/QvtGA0L7QtNGDLi4uIiBkYXRhLWkxOG4tcGg9ImJyZWVkX3BoIiBhdXRvY29tcGxldGU9Im9mZiI+CiAgICAgICAgPGJ1dHRvbiBjbGFzcz0iY2xyIiBpZD0iY2xyQnRuIj7inJU8L2J1dHRvbj4KICAgICAgPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImRyb3AiIGlkPSJiRHJvcCI+PC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InNiYWRnZSIgaWQ9InNCYWRnZSI+PC9kaXY+CiAgICA8ZGl2IGlkPSJzdmNTZWMiIHN0eWxlPSJkaXNwbGF5Om5vbmU7bWFyZ2luLXRvcDoxNnB4Ij4KICAgICAgPGRpdiBjbGFzcz0ic2xibCIgaWQ9InN0ZXAyTGJsRWwiIGRhdGEtaTE4bj0ic3RlcDJfbGJsIj4wMiDCtyDQo9GB0LvRg9Cz0LA8L2Rpdj4KICAgICAgPGRpdiBpZD0ic3ZjTGlzdCI+PC9kaXY+CiAgICA8L2Rpdj4KICA8L2Rpdj4KCiAgPCEtLSBTdGVwIDIgLS0+CiAgPGRpdiBjbGFzcz0ic3RlcCIgaWQ9ImJrMiI+CiAgICA8ZGl2IGNsYXNzPSJzbGJsIiBkYXRhLWkxOG49InN0ZXAyX21hc3RlciI+0JLRi9Cx0LXRgNC40YLQtSDQvNCw0YHRgtC10YDQsDwvZGl2PgogICAgPGRpdiBjbGFzcz0ibWFzdGVycyI+CiAgICAgIDxkaXYgY2xhc3M9Im1idG4iIGRhdGEtbWFzdGVyPSLQotCw0YLRjNGP0L3QsCI+PGRpdiBjbGFzcz0ibW5hbWUiPtCi0LDRgtGM0Y/QvdCwPC9kaXY+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9Im1idG4iIGRhdGEtbWFzdGVyPSLQkNC70LjRgdCwIj48ZGl2IGNsYXNzPSJtbmFtZSI+0JDQu9C40YHQsDwvZGl2PjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJtYnRuIiBkYXRhLW1hc3Rlcj0i0JrRgNC40YHRgtC40L3QsCI+PGRpdiBjbGFzcz0ibW5hbWUiPtCa0YDQuNGB0YLQuNC90LA8L2Rpdj48L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0ibWJ0biIgZGF0YS1tYXN0ZXI9ItCQ0L3QvdCwIj48ZGl2IGNsYXNzPSJtbmFtZSI+0JDQvdC90LA8L2Rpdj48L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0ibWJ0biIgZGF0YS1tYXN0ZXI9ItCQ0LvQtdC60YHQsNC90LTRgNCwIj48ZGl2IGNsYXNzPSJtbmFtZSI+0JDQu9C10LrRgdCw0L3QtNGA0LA8L2Rpdj48L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0ibWJ0biIgZGF0YS1tYXN0ZXI9ItCa0YHQtdC90LjRjyI+PGRpdiBjbGFzcz0ibW5hbWUiPtCa0YHQtdC90LjRjzwvZGl2PjwvZGl2PgogICAgPC9kaXY+CiAgPC9kaXY+CgogIDwhLS0gU3RlcCAzIC0tPgogIDxkaXYgY2xhc3M9InN0ZXAiIGlkPSJiazMiPgogICAgPGRpdiBjbGFzcz0ic2xibCIgZGF0YS1pMThuPSJzdGVwM19sYmwiPtCa0LDQuiDQtNCw0LLQvdC+INCy0Ysg0L/QvtGB0LXRidCw0LvQuCDQs9GA0YPQvNC40L3Qsz88L2Rpdj4KICAgIDxidXR0b24gY2xhc3M9ImdidG4iIGRhdGEtdmFsPSLQn9C10YDQstGL0Lkg0YDQsNC3IiBkYXRhLWkxOG49ImcxIj7Qn9C10YDQstGL0Lkg0YDQsNC3PC9idXR0b24+CiAgICA8YnV0dG9uIGNsYXNzPSJnYnRuIiBkYXRhLXZhbD0i0J7RgiAxINC00L4gMyDQvNC10YHRj9GG0LXQsiIgZGF0YS1pMThuPSJnMiI+0J7RgiAxINC00L4gMyDQvNC10YHRj9GG0LXQsjwvYnV0dG9uPgogICAgPGJ1dHRvbiBjbGFzcz0iZ2J0biIgZGF0YS12YWw9ItCe0YIgMyDQtNC+IDYg0LzQtdGB0Y/RhtC10LIiIGRhdGEtaTE4bj0iZzMiPtCe0YIgMyDQtNC+IDYg0LzQtdGB0Y/RhtC10LI8L2J1dHRvbj4KICAgIDxidXR0b24gY2xhc3M9ImdidG4iIGRhdGEtdmFsPSLQkdC+0LvQtdC1IDYg0LzQtdGB0Y/RhtC10LIiIGRhdGEtaTE4bj0iZzQiPtCR0L7Qu9C10LUgNiDQvNC10YHRj9GG0LXQsjwvYnV0dG9uPgogIDwvZGl2PgoKICA8IS0tIFN0ZXAgNCAtLT4KICA8ZGl2IGNsYXNzPSJzdGVwIiBpZD0iYms0Ij4KICAgIDxkaXYgY2xhc3M9InNsYmwiIGRhdGEtaTE4bj0ic3RlcDRfbGJsIj7QktGL0LHQtdGA0LjRgtC1INC00LDRgtGDPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjYWwtaCI+CiAgICAgIDxidXR0b24gY2xhc3M9ImNhbC1uIiBpZD0icHJldk0iPiYjODI0OTs8L2J1dHRvbj4KICAgICAgPGRpdiBjbGFzcz0iY2FsLW0iIGlkPSJjYWxNIj48L2Rpdj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iY2FsLW4iIGlkPSJuZXh0TSI+JiM4MjUwOzwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjZyIgaWQ9ImNhbEciPjwvZGl2PgogICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2dhcDoyMHB4O2FsaWduLWl0ZW1zOmNlbnRlcjttYXJnaW4tdG9wOjEycHg7cGFkZGluZy10b3A6MTJweDtib3JkZXItdG9wOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xKTtmbGV4LXdyYXA6d3JhcDsiPjxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweDsiPjxkaXYgc3R5bGU9IndpZHRoOjE2cHg7aGVpZ2h0OjE2cHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDpyZ2JhKDkwLDE4MCw5MCwuMTUpO2JvcmRlcjoxcHggc29saWQgIzVhYjQ1YTtmbGV4LXNocmluazowOyI+PC9kaXY+PHNwYW4gc3R5bGU9ImZvbnQtc2l6ZToxcmVtO2NvbG9yOiNmZmZmZmY7bGV0dGVyLXNwYWNpbmc6LjAzZW07IiBkYXRhLWkxOG49ImNhbF9hdmFpbCI+0JXRgdGC0Ywg0YHQstC+0LHQvtC00L3QvtC1INCy0YDQtdC80Y88L3NwYW4+PC9kaXY+PGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OHB4OyI+PGRpdiBzdHlsZT0id2lkdGg6MTZweDtoZWlnaHQ6MTZweDtib3JkZXItcmFkaXVzOjUwJTtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA0KTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEpO2ZsZXgtc2hyaW5rOjA7Ij48L2Rpdj48c3BhbiBzdHlsZT0iZm9udC1zaXplOjFyZW07Y29sb3I6I2ZmZmZmZjtsZXR0ZXItc3BhY2luZzouMDNlbTsiIGRhdGEtaTE4bj0iY2FsX25vbmUiPtCh0LLQvtCx0L7QtNC90L7Qs9C+INCy0YDQtdC80LXQvdC4INC90LXRgjwvc3Bhbj48L2Rpdj48L2Rpdj4KICAgIDxkaXYgaWQ9InRpbWVTZWMiIHN0eWxlPSJkaXNwbGF5Om5vbmU7bWFyZ2luLXRvcDoxNnB4Ij4KICAgICAgPGRpdiBjbGFzcz0ic2xibCIgZGF0YS1pMThuPSJzdGVwNF90aW1lIj7QktGL0LHQtdGA0LjRgtC1INCy0YDQtdC80Y88L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0idGciIGlkPSJ0aW1lRyI+PC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgc3R5bGU9Im1hcmdpbi10b3A6MjBweDtwYWRkaW5nLXRvcDoxNnB4O2JvcmRlci10b3A6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA4KTt0ZXh0LWFsaWduOmNlbnRlciI+CiAgICAgIDxidXR0b24gaWQ9ImNhbGxiYWNrQnRuIiBjbGFzcz0iY2JrLWJ0biI+0J3QtSDQvdCw0YjQu9C4INGD0LTQvtCx0L3QvtC1INCy0YDQtdC80Y8/IOKGkjwvYnV0dG9uPgogICAgPC9kaXY+CiAgPC9kaXY+CgogIDwhLS0gU3RlcCA1IC0tPgogIDxkaXYgY2xhc3M9InN0ZXAiIGlkPSJiazUiPgogICAgPGRpdiBjbGFzcz0ic2xibCIgZGF0YS1pMThuPSJzdGVwNV9sYmwiPtCS0LDRiNC4INC00LDQvdC90YvQtTwvZGl2PgogICAgPGRpdiBjbGFzcz0iZmciPjxsYWJlbCBjbGFzcz0iZmwiIGRhdGEtaTE4bj0ibGJsX25hbWUiPtCY0LzRjzwvbGFiZWw+PGlucHV0IGNsYXNzPSJmaSIgaWQ9ImNOYW1lIiB0eXBlPSJ0ZXh0IiBwbGFjZWhvbGRlcj0i0JLQsNGI0LUg0LjQvNGPIiBkYXRhLWkxOG4tcGg9InBoX25hbWUiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0iZmciPjxsYWJlbCBjbGFzcz0iZmwiIGRhdGEtaTE4bj0ibGJsX3Bob25lIj7QotC10LvQtdGE0L7QvTwvbGFiZWw+PGlucHV0IGNsYXNzPSJmaSIgaWQ9ImNQaG9uZSIgdHlwZT0idGVsIiBwbGFjZWhvbGRlcj0iKzM3MiAuLi4iPjwvZGl2PgogICAgPGRpdiBjbGFzcz0iZmciPjxsYWJlbCBjbGFzcz0iZmwiIGRhdGEtaTE4bj0ibGJsX2VtYWlsIj5FbWFpbDwvbGFiZWw+PGlucHV0IGNsYXNzPSJmaSIgaWQ9ImNFbWFpbCIgdHlwZT0iZW1haWwiIHBsYWNlaG9sZGVyPSJlbWFpbEBleGFtcGxlLmNvbSI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJmZyI+PGxhYmVsIGNsYXNzPSJmbCIgZGF0YS1pMThuPSJsYmxfcGV0Ij7QmtC70LjRh9C60LAg0L/QuNGC0L7QvNGG0LA8L2xhYmVsPjxpbnB1dCBjbGFzcz0iZmkiIGlkPSJjUGV0IiB0eXBlPSJ0ZXh0IiBwbGFjZWhvbGRlcj0i0J3QtdC+0LHRj9C30LDRgtC10LvRjNC90L4iIGRhdGEtaTE4bi1waD0icGhfb3B0aW9uYWwiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0ic3VtIiBpZD0ic3VtQmxvY2siPjwvZGl2PgogICAgPGJ1dHRvbiBjbGFzcz0iY2J0biIgaWQ9ImNvbmZpcm1CdG4iIGRhdGEtaTE4bj0iY29uZmlybV9idG4iPtCf0L7QtNGC0LLQtdGA0LTQuNGC0Ywg0LfQsNC/0LjRgdGMPC9idXR0b24+CiAgPC9kaXY+CgogIDwhLS0gU3VjY2VzcyAtLT4KICA8ZGl2IGNsYXNzPSJzYmxvY2siIGlkPSJzdWNCbG9jayI+CiAgICA8ZGl2IGNsYXNzPSJzaTIiPvCfkL48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InN0IiBkYXRhLWkxOG49InN1Y2Nlc3NfdGl0bGUiPtCX0LDQv9C40YHRjCDQv9GA0LjQvdGP0YLQsCE8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InNzIiBkYXRhLWkxOG49InN1Y2Nlc3Nfc3ViIj7QnNGLINGB0LLRj9C20LXQvNGB0Y8g0YEg0LLQsNC80Lgg0LTQu9GPINC/0L7QtNGC0LLQtdGA0LbQtNC10L3QuNGPLjxicj7QodC/0LDRgdC40LHQviwg0YfRgtC+INCy0YvQsdGA0LDQu9C4IFImSiBHcm9vbWluZyE8L2Rpdj4KICAgIDxidXR0b24gY2xhc3M9ImhidG4iIGlkPSJob21lQnRuIiBkYXRhLWkxOG49InRvX2hvbWUiPuKGkCDQndCwINCz0LvQsNCy0L3Rg9GOPC9idXR0b24+CiAgPC9kaXY+CjwvZGl2Pgo8L2Rpdj4KCjxkaXYgaWQ9ImNia01vZGFsIiBzdHlsZT0iZGlzcGxheTpub25lO3Bvc2l0aW9uOmZpeGVkO2luc2V0OjA7YmFja2dyb3VuZDpyZ2JhKDAsMCwwLC43NSk7ei1pbmRleDozMDA7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7cGFkZGluZzoyMHB4Ij4KICA8ZGl2IHN0eWxlPSJiYWNrZ3JvdW5kOiMwYTBhMGE7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xMik7Ym9yZGVyLXRvcDoxcHggc29saWQgI2ZmZmZmZjtwYWRkaW5nOjI4cHggMjRweDt3aWR0aDoxMDAlO21heC13aWR0aDozNjBweCI+CiAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6MC44MzhyZW07bGV0dGVyLXNwYWNpbmc6LjJlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjE2cHg7Zm9udC13ZWlnaHQ6NjAwO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmIj7QntCx0YDQsNGC0L3Ri9C5INC30LLQvtC90L7QujwvZGl2PgogICAgPGRpdiBjbGFzcz0iZmciPjxsYWJlbCBjbGFzcz0iZmwiPtCY0LzRjzwvbGFiZWw+PGlucHV0IGNsYXNzPSJmaSIgaWQ9ImNia05hbWUiIHR5cGU9InRleHQiIHBsYWNlaG9sZGVyPSLQktCw0YjQtSDQuNC80Y8iPjwvZGl2PgogICAgPGRpdiBjbGFzcz0iZmciPgogICAgICA8bGFiZWwgY2xhc3M9ImZsIj7QotC10LvQtdGE0L7QvTwvbGFiZWw+CiAgICAgIDxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpzdHJldGNoO2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjE1KSI+CiAgICAgICAgPHNwYW4gc3R5bGU9InBhZGRpbmc6MTBweCAxMHB4IDEwcHggMDtjb2xvcjojZmZmZmZmO2ZvbnQtc2l6ZToxLjM2M3JlbTtib3JkZXItcmlnaHQ6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEpO21hcmdpbi1yaWdodDoxMHB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmIj4rMzcyPC9zcGFuPgogICAgICAgIDxpbnB1dCBpZD0iY2JrUGhvbmUiIHR5cGU9InRlbCIgcGxhY2Vob2xkZXI9IlhYWFhYWFhYIiBzdHlsZT0iZmxleDoxO2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOm5vbmU7b3V0bGluZTpub25lO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6MS40MzhyZW07Y29sb3I6I2ZmZmZmZjtwYWRkaW5nOjEwcHggMCI+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IGlkPSJjYmtTdWNjZXNzIiBzdHlsZT0iZGlzcGxheTpub25lO3RleHQtYWxpZ246Y2VudGVyO3BhZGRpbmc6MjBweCAwIj4KICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjIuODc1cmVtO21hcmdpbi1ib3R0b206MTBweDtvcGFjaXR5Oi41Ij7inJM8L2Rpdj4KICAgICAgPGRpdiBzdHlsZT0iZm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjg3NXJlbTtjb2xvcjojZmZmZmZmO21hcmdpbi1ib3R0b206NnB4Ij7Ql9Cw0Y/QstC60LAg0L/RgNC40L3Rj9GC0LAhPC9kaXY+CiAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxLjAzN3JlbTtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjEuNjtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZiI+0JzRiyDQv9C10YDQtdC30LLQvtC90LjQvCDQstCw0Lwg0LIg0LHQu9C40LbQsNC50YjQtdC1INCy0YDQtdC80Y88L2Rpdj4KICAgIDwvZGl2PgogICAgPGJ1dHRvbiBpZD0iY2JrU3VibWl0IiBjbGFzcz0iY2J0biIgc3R5bGU9Im1hcmdpbi10b3A6MTRweCI+0J7RgtC/0YDQsNCy0LjRgtGMPC9idXR0b24+CiAgICA8YnV0dG9uIGlkPSJjYmtDbG9zZSIgc3R5bGU9ImRpc3BsYXk6YmxvY2s7d2lkdGg6MTAwJTttYXJnaW4tdG9wOjhweDtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyOm5vbmU7Y29sb3I6I2ZmZmZmZjtmb250LXNpemU6MC44MzhyZW07bGV0dGVyLXNwYWNpbmc6LjEyZW07Y3Vyc29yOnBvaW50ZXI7cGFkZGluZzo4cHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWYiPtCe0YLQvNC10L3QsDwvYnV0dG9uPgogIDwvZGl2Pgo8L2Rpdj4KCjxzY3JpcHQ+CnZhciBEQVRBID0gW3siYnJlZWQiOiLQkNCy0YHRgtGA0LDQu9C40LnRgdC60LDRjyDQvtCy0YfQsNGA0LrQsCAxNeKAkzI1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4NX0sImJyZWVkX2VuIjoiQXVzdHJhbGlhbiBTaGVwaGVyZCAxNeKAkzI1IGtnIiwiYnJlZWRfZXQiOiJBdXN0cmFhbGlhIGxhbWJha29lciAxNeKAkzI1IGtnIn0seyJicmVlZCI6ItCQ0LLRgdGC0YDQsNC70LjQudGB0LrQsNGPINC+0LLRh9Cw0YDQutCwIDI14oCTMzUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiQXVzdHJhbGlhbiBTaGVwaGVyZCAyNeKAkzM1IGtnIiwiYnJlZWRfZXQiOiJBdXN0cmFhbGlhIGxhbWJha29lciAyNeKAkzM1IGtnIn0seyJicmVlZCI6ItCQ0LrQuNGC0LAt0LjQvdGDIDIw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkFraXRhIEludSAyMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBa2l0YSBJbnUgMjDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC60LjRgtCwLdC40L3RgyDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQWtpdGEgSW51IG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkFraXRhIEludSDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCQ0LrQuNGC0LAt0LjQvdGDINGE0LvQsNGE0YTQuCAyMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJBa2l0YSBJbnUgZmx1ZmZ5IDIw4oCTNDAga2ciLCJicmVlZF9ldCI6IkFraXRhIEludSBwZWhtZWthcnZhbGluZSAyMOKAkzQwIGtnIn0seyJicmVlZCI6ItCQ0LrQuNGC0LAt0LjQvdGDINGE0LvQsNGE0YTQuCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQWtpdGEgSW51IGZsdWZmeSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJBa2l0YSBJbnUgcGVobWVrYXJ2YWxpbmUgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQkNC70LDQsdCw0LkgNDDigJM2MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkNlbnRyYWwgQXNpYW4gU2hlcGhlcmQgNDDigJM2MCBrZyIsImJyZWVkX2V0IjoiS2Vzay1BYXNpYSBsYW1iYWtvZXIgNDDigJM2MCBrZyJ9LHsiYnJlZWQiOiLQkNC70LDQsdCw0Lkg0LHQvtC70LXQtSA2MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjEwMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjExNSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEzMH0sImJyZWVkX2VuIjoiQ2VudHJhbCBBc2lhbiBTaGVwaGVyZCBvdmVyIDYwIGtnIiwiYnJlZWRfZXQiOiJLZXNrLUFhc2lhIGxhbWJha29lciDDvGxlIDYwIGtnIn0seyJicmVlZCI6ItCQ0LvRj9GB0LrQuNC90YHQutC40Lkg0LzQsNC70LDQvNGD0YIgMjDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQWxhc2thbiBNYWxhbXV0ZSAyMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBbGFza2EgbWFsYW11dXQgMjDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC70Y/RgdC60LjQvdGB0LrQuNC5INC80LDQu9Cw0LzRg9GCINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJBbGFza2FuIE1hbGFtdXRlIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkFsYXNrYSBtYWxhbXV1dCDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCQ0LvRj9GB0LrQuNC90YHQutC40Lkg0LzQsNC70LDQvNGD0YIg0YTQu9Cw0YTRhNC4IDIw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkFsYXNrYW4gTWFsYW11dGUgZmx1ZmZ5IDIw4oCTNDAga2ciLCJicmVlZF9ldCI6IkFsYXNrYSBtYWxhbXV1dCBwZWhtZWthcnZhbGluZSAyMOKAkzQwIGtnIn0seyJicmVlZCI6ItCQ0LvRj9GB0LrQuNC90YHQutC40Lkg0LzQsNC70LDQvNGD0YIg0YTQu9Cw0YTRhNC4INCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJBbGFza2FuIE1hbGFtdXRlIGZsdWZmeSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJBbGFza2EgbWFsYW11dXQgcGVobWVrYXJ2YWxpbmUgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutCw0Y8g0LDQutC40YLQsCAyMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBBa2l0YSAyMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBBa2l0YSAyMOKAkzQwIGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LDRjyDQsNC60LjRgtCwINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBBa2l0YSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBBa2l0YSDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LDRjyDQsNC60LjRgtCwINGE0LvQsNGE0YTQuCAyMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBBa2l0YSBmbHVmZnkgMjDigJM0MCBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2EgQWtpdGEgcGVobWVrYXJ2YWxpbmUgMjDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutCw0Y8g0LDQutC40YLQsCDRhNC70LDRhNGE0Lgg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkFtZXJpY2FuIEFraXRhIGZsdWZmeSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBBa2l0YSBwZWhtZWthcnZhbGluZSDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LjQuSDQutC+0LrQtdGALdGB0L/QsNC90LjQtdC70YwgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBDb2NrZXIgU3BhbmllbCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBrb2tlcnNwYW5qZWwgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutC40Lkg0LrQvtC60LXRgC3RgdC/0LDQvdC40LXQu9GMIDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4MH0sImJyZWVkX2VuIjoiQW1lcmljYW4gQ29ja2VyIFNwYW5pZWwgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2Ega29rZXJzcGFuamVsIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0JDQvNC10YDQuNC60LDQvdGB0LrQuNC5INGB0YLQsNGE0YTQvtGA0LTRiNC40YDRgdC60LjQuSDRgtC10YDRjNC10YAgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkFtZXJpY2FuIFN0YWZmb3Jkc2hpcmUgVGVycmllciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBTdGFmZm9yZHNoaXJlIHRlcmplciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LjQuSDRgdGC0LDRhNGE0L7RgNC00YjQuNGA0YHQutC40Lkg0YLQtdGA0YzQtdGAIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1fSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBTdGFmZm9yZHNoaXJlIFRlcnJpZXIgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2EgU3RhZmZvcmRzaGlyZSB0ZXJqZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC90LPQu9C40LnRgdC60LjQuSDQsdGD0LvRjNC00L7QsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NzB9LCJicmVlZF9lbiI6IkVuZ2xpc2ggQnVsbGRvZyIsImJyZWVkX2V0IjoiSW5nbGlzZSBidWxkb2cifSx7ImJyZWVkIjoi0JDQvdCz0LvQuNC50YHQutC40Lkg0LrQvtC60LXRgC3RgdC/0LDQvdC40LXQu9GMIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MH0sImJyZWVkX2VuIjoiRW5nbGlzaCBDb2NrZXIgU3BhbmllbCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJJbmdsaXNlIGtva2Vyc3BhbmplbCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCQ0L3Qs9C70LjQudGB0LrQuNC5INC60L7QutC10YAt0YHQv9Cw0L3QuNC10LvRjCAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODB9LCJicmVlZF9lbiI6IkVuZ2xpc2ggQ29ja2VyIFNwYW5pZWwgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiSW5nbGlzZSBrb2tlcnNwYW5qZWwgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQkNGE0LPQsNC9IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5MH0sImJyZWVkX2VuIjoiQWZnaGFuIEhvdW5kIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkFmZ2FuaXN0YW5pIGtvZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQkNGE0LPQsNC9IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjgwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDB9LCJicmVlZF9lbiI6IkFmZ2hhbiBIb3VuZCAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBZmdhbmlzdGFuaSBrb2VyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JHQsNGB0YHQtdGCLdGF0LDRg9C90LQgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkJhc3NldCBIb3VuZCAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJCYXNzZXRob3VuZCAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCR0LDRgdGB0LXRgi3RhdCw0YPQvdC0IDMw4oCTMzUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1fSwiYnJlZWRfZW4iOiJCYXNzZXQgSG91bmQgMzDigJMzNSBrZyIsImJyZWVkX2V0IjoiQmFzc2V0aG91bmQgMzDigJMzNSBrZyJ9LHsiYnJlZWQiOiLQkdC10YDQvdGB0LrQuNC5INC30LXQvdC90LXQvdGF0YPQvdC0IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IkJlcm5lc2UgTW91bnRhaW4gRG9nIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkJlcm5pIG3DpGdpa29lciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCR0LXRgNC90YHQutC40Lkg0LfQtdC90L3QtdC90YXRg9C90LQg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEzMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQmVybmVzZSBNb3VudGFpbiBEb2cgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiQmVybmkgbcOkZ2lrb2VyIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JHQuNCy0LXRgC3QudC+0YDQuiDQsdC+0LvQtdC1IDMsNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJCaWV3ZXIgWW9ya3NoaXJlIFRlcnJpZXIgb3ZlciAzLDUga2ciLCJicmVlZF9ldCI6IkJpZXdlciBZb3Jrc2hpcmUgVGVycmllciDDvGxlIDMsNSBrZyJ9LHsiYnJlZWQiOiLQkdC40LLQtdGALdC50L7RgNC6INC00L4gMyw1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkJpZXdlciBZb3Jrc2hpcmUgVGVycmllciB1cCB0byAzLDUga2ciLCJicmVlZF9ldCI6IkJpZXdlciBZb3Jrc2hpcmUgVGVycmllciBrdW5pIDMsNSBrZyJ9LHsiYnJlZWQiOiLQkdC40LPQu9GMIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjB9LCJicmVlZF9lbiI6IkJlYWdsZSAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJCaWlnZWwgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQkdC40LPQu9GMIDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjV9LCJicmVlZF9lbiI6IkJlYWdsZSAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiJCaWlnZWwgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQkdC40YjQvtC9LdGE0YDQuNC30LUgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkJpY2hvbiBGcmlzw6kgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJCacWhb24gRnJpc8OpIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQkdC40YjQvtC9LdGE0YDQuNC30LUg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkJpY2hvbiBGcmlzw6kgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiQmnFoW9uIEZyaXPDqSBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JHQvtC60YHQtdGAIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwfSwiYnJlZWRfZW4iOiJCb3hlciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJCb2tzZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQkdC+0LrRgdC10YAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkJveGVyIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkJva3NlciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCR0L7RgNC00LXRgC3QutC+0LvQu9C4IDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjgwfSwiYnJlZWRfZW4iOiJCb3JkZXIgQ29sbGllIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IkJvcmRlcmtvbGwgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQkdC+0YDQtNC10YAt0LrQvtC70LvQuCAyMOKAkzI1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkJvcmRlciBDb2xsaWUgMjDigJMyNSBrZyIsImJyZWVkX2V0IjoiQm9yZGVya29sbCAyMOKAkzI1IGtnIn0seyJicmVlZCI6ItCR0L7RgdGC0L7QvS3RgtC10YDRjNC10YAgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDV9LCJicmVlZF9lbiI6IkJvc3RvbiBUZXJyaWVyIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IkJvc3RvbmkgdGVyamVyIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0JHQvtGB0YLQvtC9LdGC0LXRgNGM0LXRgCA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwfSwiYnJlZWRfZW4iOiJCb3N0b24gVGVycmllciA14oCTMTAga2ciLCJicmVlZF9ldCI6IkJvc3RvbmkgdGVyamVyIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQkdGA0LDQsdCw0L3RgdC+0L0iLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjUwfSwiYnJlZWRfZW4iOiJHcmlmZm9uIEJydXhlbGxvaXMiLCJicmVlZF9ldCI6IkJyw7xzc2VsaSBncmlmb24ifSx7ImJyZWVkIjoi0JHRg9C70YzRgtC10YDRjNC10YAgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTB9LCJicmVlZF9lbiI6IkJ1bGwgVGVycmllciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJCdWxsdGVyamVyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JLQtdC70YzRiC3QutC+0YDQs9C4IDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjV9LCJicmVlZF9lbiI6IldlbHNoIENvcmdpIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IldhbGVzaSBrb3JnaSAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCS0LXQu9GM0Ygt0LrQvtGA0LPQuCAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjc1fSwiYnJlZWRfZW4iOiJXZWxzaCBDb3JnaSAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiJXYWxlc2kga29yZ2kgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQktC10YHRgi3RhdCw0LnQu9C10L3QtC3QstCw0LnRgi3RgtC10YDRjNC10YAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJXZXN0IEhpZ2hsYW5kIFdoaXRlIFRlcnJpZXIiLCJicmVlZF9ldCI6IkzDpMOkbmUtxaBvdGltYWEgdmFsZ2UgdGVyamVyIn0seyJicmVlZCI6ItCS0L7RgdGC0L7Rh9C90L7RgdC40LHQuNGA0YHQutCw0Y8g0LvQsNC50LrQsCAxOOKAkzI1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjg1fSwiYnJlZWRfZW4iOiJFYXN0IFNpYmVyaWFuIExhaWthIDE44oCTMjUga2ciLCJicmVlZF9ldCI6IklkYS1TaWJlcmkgbGFpa2EgMTjigJMyNSBrZyJ9LHsiYnJlZWQiOiLQktC+0YHRgtC+0YfQvdC+0YHQuNCx0LjRgNGB0LrQsNGPINC70LDQudC60LAg0LHQvtC70LXQtSAyNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjU1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IkVhc3QgU2liZXJpYW4gTGFpa2Egb3ZlciAyNSBrZyIsImJyZWVkX2V0IjoiSWRhLVNpYmVyaSBsYWlrYSDDvGxlIDI1IGtnIn0seyJicmVlZCI6ItCT0L7Qu9C00LXQvS3RgNC10YLRgNC40LLQtdGAIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiR29sZGVuIFJldHJpZXZlciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJLdWxkbmUgcmV0cmlpdmVyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JPQvtC70LTQtdC9LdGA0LXRgtGA0LjQstC10YAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjExMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiR29sZGVuIFJldHJpZXZlciAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJLdWxkbmUgcmV0cmlpdmVyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JPRgNC40YTRhNC+0L0iLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJHcmlmZm9uIiwiYnJlZWRfZXQiOiJHcmlmb24ifSx7ImJyZWVkIjoi0JTQsNC70LzQsNGC0LjQvSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NzB9LCJicmVlZF9lbiI6IkRhbG1hdGlhbiIsImJyZWVkX2V0IjoiRGFsbWFhdHNpYSBrb2VyIn0seyJicmVlZCI6ItCU0LbQtdC6LdGA0LDRgdGB0LXQuy3RgtC10YDRjNC10YAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NTB9LCJicmVlZF9lbiI6IkphY2sgUnVzc2VsbCBUZXJyaWVyIHNtb290aCIsImJyZWVkX2V0IjoiSmFjayBSdXNzZWxsaSB0ZXJqZXIgbMO8aGlrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0JTQttC10Lot0YDQsNGB0YHQtdC7LdGC0LXRgNGM0LXRgCDQttC10YHRgtC60L7RiNC10YDRgdGC0L3Ri9C5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiSmFjayBSdXNzZWxsIFRlcnJpZXIgd2lyZS1oYWlyZWQiLCJicmVlZF9ldCI6IkphY2sgUnVzc2VsbGkgdGVyamVyIGthcnVrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0JTQvtCx0LXRgNC80LDQvSAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjgwfSwiYnJlZWRfZW4iOiJEb2Jlcm1hbm4gMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiRG9iZXJtYW5uIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JTQvtCx0LXRgNC80LDQvSDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjk1fSwiYnJlZWRfZW4iOiJEb2Jlcm1hbm4gb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiRG9iZXJtYW5uIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JfQsNC/0LDQtNC90L7RgdC40LHQuNGA0YHQutCw0Y8g0LvQsNC50LrQsCAxOOKAkzI1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjg1fSwiYnJlZWRfZW4iOiJXZXN0IFNpYmVyaWFuIExhaWthIDE44oCTMjUga2ciLCJicmVlZF9ldCI6IkzDpMOkbmUtU2liZXJpIGxhaWthIDE44oCTMjUga2cifSx7ImJyZWVkIjoi0JfQvtC70L7RgtC40YHRgtGL0Lkg0YDQtdGC0YDQuNCy0LXRgCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiR29sZGVuIFJldHJpZXZlciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJLdWxkbmUgcmV0cmlpdmVyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JfQvtC70L7RgtC40YHRgtGL0Lkg0YDQtdGC0YDQuNCy0LXRgCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTEwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTEwfSwiYnJlZWRfZW4iOiJHb2xkZW4gUmV0cmlldmVyIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6Ikt1bGRuZSByZXRyaWl2ZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQmNGA0LvQsNC90LTRgdC60LjQuSDQvNGP0LPQutC+0YjQtdGA0YHRgtC90YvQuSDQv9GI0LXQvdC40YfQvdGL0Lkg0YLQtdGA0YzQtdGAIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwfSwiYnJlZWRfZW4iOiJJcmlzaCBTb2Z0IENvYXRlZCBXaGVhdGVuIFRlcnJpZXIiLCJicmVlZF9ldCI6IklpcmkgcGVobWVrYXJ2YW5lIG5pc3V2w6RydmkgdGVyamVyIn0seyJicmVlZCI6ItCY0YDQu9Cw0L3QtNGB0LrQuNC5INGC0LXRgNGM0LXRgCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MCwi0KLRgNC40LzQvNC40L3QsyI6NzV9LCJicmVlZF9lbiI6IklyaXNoIFRlcnJpZXIiLCJicmVlZF9ldCI6IklpcmkgdGVyamVyIn0seyJicmVlZCI6ItCY0YHQv9Cw0L3RgdC60LjQuSDQs9Cw0LvRjNCz0L4gMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo3MH0sImJyZWVkX2VuIjoiU3BhbmlzaCBHYWxnbyAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJIaXNwYWFuaWEgZ2FsZ28gMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQmNGB0L/QsNC90YHQutC40Lkg0LPQsNC70YzQs9C+IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6ODB9LCJicmVlZF9lbiI6IlNwYW5pc2ggR2FsZ28gMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiSGlzcGFhbmlhIGdhbGdvIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JnQvtGA0LrRiNC40YDRgdC60LjQuSDRgtC10YDRjNC10YAg0LHQvtC70LXQtSAzLDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiWW9ya3NoaXJlIFRlcnJpZXIgb3ZlciAzLDUga2ciLCJicmVlZF9ldCI6IllvcmtzaGlyZSB0ZXJqZXIgw7xsZSAzLDUga2cifSx7ImJyZWVkIjoi0JnQvtGA0LrRiNC40YDRgdC60LjQuSDRgtC10YDRjNC10YAg0LTQviAzLDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiWW9ya3NoaXJlIFRlcnJpZXIgdXAgdG8gMyw1IGtnIiwiYnJlZWRfZXQiOiJZb3Jrc2hpcmUgdGVyamVyIGt1bmkgMyw1IGtnIn0seyJicmVlZCI6ItCa0LDQstCw0LvQtdGALdC60LjQvdCzLdGH0LDRgNC70YzQty3RgdC/0LDQvdC40LXQu9GMIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MH0sImJyZWVkX2VuIjoiQ2F2YWxpZXIgS2luZyBDaGFybGVzIFNwYW5pZWwgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiQ2F2YWxpZXIgS2luZyBDaGFybGVzIFNwYW5pZWwgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQmtCw0LLQsNC70LXRgC3QutC40L3Qsy3Rh9Cw0YDQu9GM0Lct0YHQv9Cw0L3QuNC10LvRjCA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiQ2F2YWxpZXIgS2luZyBDaGFybGVzIFNwYW5pZWwgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJDYXZhbGllciBLaW5nIENoYXJsZXMgU3BhbmllbCA14oCTMTAga2cifSx7ImJyZWVkIjoi0JrQsNC90LUt0LrQvtGA0YHQviA0MOKAkzYwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NX0sImJyZWVkX2VuIjoiQ2FuZSBDb3JzbyA0MOKAkzYwIGtnIiwiYnJlZWRfZXQiOiJDYW5lIENvcnNvIDQw4oCTNjAga2cifSx7ImJyZWVkIjoi0JrQsNC90LUt0LrQvtGA0YHQviDQsdC+0LvQtdC1IDYwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6OTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjoxMDV9LCJicmVlZF9lbiI6IkNhbmUgQ29yc28gb3ZlciA2MCBrZyIsImJyZWVkX2V0IjoiQ2FuZSBDb3JzbyDDvGxlIDYwIGtnIn0seyJicmVlZCI6ItCa0LDRgNC10LvQvi3RhNC40L3RgdC60LDRjyDQu9Cw0LnQutCwINC00L4gMTMg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjV9LCJicmVlZF9lbiI6IkthcmVsaWFuLUZpbm5pc2ggTGFpa2EgdXAgdG8gMTMga2ciLCJicmVlZF9ldCI6IkthcmphbGEtU29vbWUgbGFpa2Ega3VuaSAxMyBrZyJ9LHsiYnJlZWQiOiLQmtC40YLQsNC50YHQutCw0Y8g0YXQvtGF0LvQsNGC0LDRjyDQs9C+0LvQsNGPIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMyLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDIsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJDaGluZXNlIENyZXN0ZWQgaGFpcmxlc3MgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJIaWluYSBoYXJqYWtvZXIga2FydmF0dSA14oCTMTAga2cifSx7ImJyZWVkIjoi0JrQuNGC0LDQudGB0LrQsNGPINGF0L7RhdC70LDRgtCw0Y8g0LPQvtC70LDRjyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyOCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiQ2hpbmVzZSBDcmVzdGVkIGhhaXJsZXNzIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IkhpaW5hIGhhcmpha29lciBrYXJ2YXR1IGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQmtC40YLQsNC50YHQutCw0Y8g0YXQvtGF0LvQsNGC0LDRjyDQv9GD0YXQvtCy0LDRjyA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiQ2hpbmVzZSBDcmVzdGVkIHBvd2RlcnB1ZmYgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJIaWluYSBoYXJqYWtvZXIgUG93ZGVycHVmZiA14oCTMTAga2cifSx7ImJyZWVkIjoi0JrQuNGC0LDQudGB0LrQsNGPINGF0L7RhdC70LDRgtCw0Y8g0L/Rg9GF0L7QstCw0Y8g0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkNoaW5lc2UgQ3Jlc3RlZCBwb3dkZXJwdWZmIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IkhpaW5hIGhhcmpha29lciBQb3dkZXJwdWZmIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQmtC+0LrQsNC/0YMgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzV9LCJicmVlZF9lbiI6IkNvY2thcG9vIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiQ29ja2Fwb28gNeKAkzEwIGtnIn0seyJicmVlZCI6ItCa0L7QutCw0L/RgyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2NX0sImJyZWVkX2VuIjoiQ29ja2Fwb28gdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiQ29ja2Fwb28ga3VuaSA1IGtnIn0seyJicmVlZCI6ItCa0L7Qu9C70LggMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJDb2xsaWUgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiS29sbCAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCa0L7Qu9C70LggMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjExMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiQ29sbGllIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IktvbGwgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQmtC+0LzQvtC90LTQvtGAIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMTB9LCJicmVlZF9lbiI6IktvbW9uZG9yIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IktvbW9uZG9yIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JrQvtC80L7QvdC00L7RgCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTMwfSwiYnJlZWRfZW4iOiJLb21vbmRvciBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJLb21vbmRvciDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNC+0YAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjcwfSwiYnJlZWRfZW4iOiJMYWJyYWRvciBSZXRyaWV2ZXIgc21vb3RoIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9yaSByZXRyaWl2ZXIgbMO8aGlrYXJ2YWxpbmUgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQm9Cw0LHRgNCw0LTQvtGAINCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdGL0LkgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjU1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4MH0sImJyZWVkX2VuIjoiTGFicmFkb3IgUmV0cmlldmVyIHNtb290aCAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvcmkgcmV0cmlpdmVyIGzDvGhpa2FydmFsaW5lIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00L7RgCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3Ri9C5INCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjgwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTV9LCJicmVlZF9lbiI6IkxhYnJhZG9yIFJldHJpZXZlciBzbW9vdGggb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiTGFicmFkb3JpIHJldHJpaXZlciBsw7xoaWthcnZhbGluZSDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNC+0YAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90YvQuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkxhYnJhZG9yIFJldHJpZXZlciBsb25nLWNvYXRlZCAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvcmkgcmV0cmlpdmVyIHBpa2thcnZhbGluZSAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNC+0YAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90YvQuSAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTEwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJMYWJyYWRvciBSZXRyaWV2ZXIgbG9uZy1jb2F0ZWQgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiTGFicmFkb3JpIHJldHJpaXZlciBwaWtrYXJ2YWxpbmUgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQm9Cw0LHRgNCw0LTQvtGAINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdGL0Lkg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEzMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiTGFicmFkb3IgUmV0cmlldmVyIGxvbmctY29hdGVkIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9yaSByZXRyaWl2ZXIgcGlra2FydmFsaW5lIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00YPQtNC10LvRjCAxMOKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IkxhYnJhZG9vZGxlIDEw4oCTMjAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9vZGxlIDEw4oCTMjAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00YPQtNC10LvRjCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTB9LCJicmVlZF9lbiI6IkxhYnJhZG9vZGxlIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9vZGxlIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00YPQtNC10LvRjCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwfSwiYnJlZWRfZW4iOiJMYWJyYWRvb2RsZSAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvb2RsZSAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCb0LXQstGA0LXRgtC60LAgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MH0sImJyZWVkX2VuIjoiSXRhbGlhbiBHcmV5aG91bmQgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJJdGFhbGlhIHZpbmRrb2VyIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQm9C10LLRgNC10YLQutCwINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjI1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MzV9LCJicmVlZF9lbiI6Ikl0YWxpYW4gR3JleWhvdW5kIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6Ikl0YWFsaWEgdmluZGtvZXIga3VuaSA1IGtnIn0seyJicmVlZCI6ItCb0YXQsNGB0YHQutC40Lkg0LDQv9GB0L4gNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzV9LCJicmVlZF9lbiI6IkxoYXNhIEFwc28gNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJMaGFzYSBBcHNvIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQm9GF0LDRgdGB0LrQuNC5INCw0L/RgdC+INC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjY1fSwiYnJlZWRfZW4iOiJMaGFzYSBBcHNvIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IkxoYXNhIEFwc28ga3VuaSA1IGtnIn0seyJicmVlZCI6ItCc0LDQu9GM0YLQtdC30LUiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6Ik1hbHRlc2UiLCJicmVlZF9ldCI6Ik1hbHRhIGJvbG9uZWVzIn0seyJicmVlZCI6ItCc0LDQu9GM0YLQuNC50YHQutCw0Y8g0LHQvtC70L7QvdC60LAgNeKAkzgg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2NX0sImJyZWVkX2VuIjoiTWFsdGVzZSBCb2xvZ25lc2UgNeKAkzgga2ciLCJicmVlZF9ldCI6Ik1hbHRhIGJvbG9uZWVzIDXigJM4IGtnIn0seyJicmVlZCI6ItCc0LDQu9GM0YLQuNC50YHQutCw0Y8g0LHQvtC70L7QvdC60LAg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6Ik1hbHRlc2UgQm9sb2duZXNlIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6Ik1hbHRhIGJvbG9uZWVzIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQnNCw0LvRjNGC0LjQv9GDIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MH0sImJyZWVkX2VuIjoiTWFsdGlwb28gMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiTWFsdGlwdXUgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQnNCw0LvRjNGC0LjQv9GDIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJNYWx0aXBvbyA14oCTMTAga2ciLCJicmVlZF9ldCI6Ik1hbHRpcHV1IDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQnNCw0LvRjNGC0LjQv9GDINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJNYWx0aXBvbyB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJNYWx0aXB1dSBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JzQtdGC0LjRgSDQutGA0YPQv9C90YvQuSAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJNaXhlZCBicmVlZCBsYXJnZSAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJTZWdhdmVyZCBzdXVyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JzQtdGC0LjRgSDQutGA0YPQv9C90YvQuSDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTIwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTIwfSwiYnJlZWRfZW4iOiJNaXhlZCBicmVlZCBsYXJnZSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJTZWdhdmVyZCBzdXVyIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JzQtdGC0LjRgSDQvNC10LvQutC40LkgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6Ik1peGVkIGJyZWVkIHNtYWxsIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiU2VnYXZlcmQgdsOkaWtlIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQnNC10YLQuNGBINC80LXQu9C60LjQuSDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1MH0sImJyZWVkX2VuIjoiTWl4ZWQgYnJlZWQgc21hbGwgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiU2VnYXZlcmQgdsOkaWtlIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQnNC10YLQuNGBINGB0YDQtdC00L3QuNC5IDEw4oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjcwfSwiYnJlZWRfZW4iOiJNaXhlZCBicmVlZCBtZWRpdW0gMTDigJMyMCBrZyIsImJyZWVkX2V0IjoiU2VnYXZlcmQga2Vza21pbmUgMTDigJMyMCBrZyJ9LHsiYnJlZWQiOiLQnNC10YLQuNGBINGB0YDQtdC00L3QuNC5IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjYwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjg1fSwiYnJlZWRfZW4iOiJNaXhlZCBicmVlZCBtZWRpdW0gMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiU2VnYXZlcmQga2Vza21pbmUgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQnNC40YLRgtC10LvRjNGI0L3QsNGD0YbQtdGAIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MCwi0KLRgNC40LzQvNC40L3QsyI6NzV9LCJicmVlZF9lbiI6IlN0YW5kYXJkIFNjaG5hdXplciAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJTdGFuZGFyZMWhbmF1dHNlciAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCc0LjRgtGC0LXQu9GM0YjQvdCw0YPRhtC10YAgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwLCLQotGA0LjQvNC80LjQvdCzIjo4NX0sImJyZWVkX2VuIjoiU3RhbmRhcmQgU2NobmF1emVyIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IlN0YW5kYXJkxaFuYXV0c2VyIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0JzQvtC/0YEiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjUwfSwiYnJlZWRfZW4iOiJQdWciLCJicmVlZF9ldCI6Ik1vcHMifSx7ImJyZWVkIjoi0J3QtdCy0YHQutCw0Y8g0L7RgNGF0LjQtNC10Y8iLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6Ik5ldmEgT3JjaGlkIiwiYnJlZWRfZXQiOiJOZWV2YSBvcmhpZGVlIn0seyJicmVlZCI6ItCd0LXQvNC10YbQutCw0Y8g0L7QstGH0LDRgNC60LAgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiR2VybWFuIFNoZXBoZXJkIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IlNha3NhIGxhbWJha29lciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCd0LXQvNC10YbQutCw0Y8g0L7QstGH0LDRgNC60LAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6Ikdlcm1hbiBTaGVwaGVyZCAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJTYWtzYSBsYW1iYWtvZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQndC10LzQtdGG0LrQsNGPINC+0LLRh9Cw0YDQutCwINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJHZXJtYW4gU2hlcGhlcmQgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiU2Frc2EgbGFtYmFrb2VyIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0KjQstC10LnRhtCw0YDRgdC60LDRjyDQvtCy0YfQsNGA0LrQsCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJTd2lzcyBTaGVwaGVyZCAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiLFoHZlaXRzaSBsYW1iYWtvZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQqNCy0LXQudGG0LDRgNGB0LrQsNGPINC+0LLRh9Cw0YDQutCwIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJTd2lzcyBTaGVwaGVyZCAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiLFoHZlaXRzaSBsYW1iYWtvZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQqNCy0LXQudGG0LDRgNGB0LrQsNGPINC+0LLRh9Cw0YDQutCwINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJTd2lzcyBTaGVwaGVyZCBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiLFoHZlaXRzaSBsYW1iYWtvZXIgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQndC+0YDQstC40Yct0YLQtdGA0YzQtdGAIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiTm9yd2ljaCBUZXJyaWVyIiwiYnJlZWRfZXQiOiJOb3J3aXTFoWkgdGVyamVyIn0seyJicmVlZCI6ItCd0L7RgNGE0L7Qu9C6LdGC0LXRgNGM0LXRgCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MCwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6Ik5vcmZvbGsgVGVycmllciIsImJyZWVkX2V0IjoiTm9yZm9sa2kgdGVyamVyIn0seyJicmVlZCI6ItCd0YzRjtGE0LDRg9C90LTQu9C10L3QtCA0MOKAkzYwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTMwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJOZXdmb3VuZGxhbmQgNDDigJM2MCBrZyIsImJyZWVkX2V0IjoiTmV3Zm91bmRsYW5kaSBrb2VyIDQw4oCTNjAga2cifSx7ImJyZWVkIjoi0J3RjNGO0YTQsNGD0L3QtNC70LXQvdC0INCx0L7Qu9C10LUgNjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoxMDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjoxMTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjE1MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEzMH0sImJyZWVkX2VuIjoiTmV3Zm91bmRsYW5kIG92ZXIgNjAga2ciLCJicmVlZF9ldCI6Ik5ld2ZvdW5kbGFuZGkga29lciDDvGxlIDYwIGtnIn0seyJicmVlZCI6ItCf0LDQv9C40LnQvtC9Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJQYXBpbGxvbiIsImJyZWVkX2V0IjoiUGFwaWxsb24ifSx7ImJyZWVkIjoi0J/QtdC60LjQvdC10YEgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IlBla2luZ2VzZSA14oCTMTAga2ciLCJicmVlZF9ldCI6IlBla2luZXNpIGtvZXIgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCf0LXQutC40L3QtdGBINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJQZWtpbmdlc2UgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiUGVraW5lc2kga29lciBrdW5pIDUga2cifSx7ImJyZWVkIjoi0J/Rg9C00LXQu9GMINCx0L7Qu9GM0YjQvtC5IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5MH0sImJyZWVkX2VuIjoiU3RhbmRhcmQgUG9vZGxlIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IlN0YW5kYXJkcHV1ZGVsIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0J/Rg9C00LXQu9GMINCx0L7Qu9GM0YjQvtC5IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjgwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDB9LCJicmVlZF9lbiI6IlN0YW5kYXJkIFBvb2RsZSAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJTdGFuZGFyZHB1dWRlbCAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCf0YPQtNC10LvRjCDQutCw0YDQu9C40LrQvtCy0YvQuSA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiTWluaWF0dXJlIFBvb2RsZSA14oCTMTAga2ciLCJicmVlZF9ldCI6IkvDpMOkYnVzcHV1ZGVsIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQn9GD0LTQtdC70Ywg0LzQsNC70YvQuSAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IlNtYWxsIFBvb2RsZSAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJWw6Rpa2UgcHV1ZGVsIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0J/Rg9C00LXQu9GMINC80LDQu9GL0LkgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwfSwiYnJlZWRfZW4iOiJTbWFsbCBQb29kbGUgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiVsOkaWtlIHB1dWRlbCAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCf0YPQtNC10LvRjCDRgtC+0Lkg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IlRveSBQb29kbGUgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiTcOkbmd1YXNqYSBwdXVkZWwga3VuaSA1IGtnIn0seyJicmVlZCI6ItCg0LjQt9C10L3RiNC90LDRg9GG0LXRgCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwLCLQotGA0LjQvNC80LjQvdCzIjoxMTB9LCJicmVlZF9lbiI6IkdpYW50IFNjaG5hdXplciAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJTdXVyxaFuYXV0c2VyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0KDQuNC30LXQvdGI0L3QsNGD0YbQtdGAINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMjAsItCi0YDQuNC80LzQuNC90LMiOjEyNX0sImJyZWVkX2VuIjoiR2lhbnQgU2NobmF1emVyIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IlN1dXLFoW5hdXRzZXIgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQoNGD0YHRgdC60LDRjyDRhtCy0LXRgtC90LDRjyDQsdC+0LvQvtC90LrQsCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiUnVzc2lhbiBDb2xvcmVkIExhcGRvZyIsImJyZWVkX2V0IjoiVmVuZSB2w6RydmlsaW5lIHPDvGxla29lciJ9LHsiYnJlZWQiOiLQoNGD0YHRgdC60LjQuSDQvtGF0L7RgtC90LjRh9C40Lkg0YHQv9Cw0L3QuNC10LvRjCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IlJ1c3NpYW4gU3BhbmllbCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJWZW5lIGphaGlzcGFuamVsIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KDRg9GB0YHQutC40Lkg0L7RhdC+0YLQvdC40YfQuNC5INGB0L/QsNC90LjQtdC70YwgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwfSwiYnJlZWRfZW4iOiJSdXNzaWFuIFNwYW5pZWwgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiVmVuZSBqYWhpc3BhbmplbCAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCg0YPRgdGB0LrQuNC5INGC0L7QuSDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3Ri9C5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjI1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MzV9LCJicmVlZF9lbiI6IlJ1c3NpYW4gVG95IHNtb290aCIsImJyZWVkX2V0IjoiVmVuZSBUb3kgbMO8aGlrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0KDRg9GB0YHQutC40Lkg0YLQvtC5INC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdGL0LkiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IlJ1c3NpYW4gVG95IGxvbmctY29hdGVkIiwiYnJlZWRfZXQiOiJWZW5lIFRveSBwaWtrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0KDRg9GB0YHQutC40Lkg0YfQtdGA0L3Ri9C5INGC0LXRgNGM0LXRgCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwfSwiYnJlZWRfZW4iOiJCbGFjayBSdXNzaWFuIFRlcnJpZXIgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiTXVzdCBWZW5lIHRlcmplciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCg0YPRgdGB0LrQuNC5INGH0LXRgNC90YvQuSDRgtC10YDRjNC10YAg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjc1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEyMH0sImJyZWVkX2VuIjoiQmxhY2sgUnVzc2lhbiBUZXJyaWVyIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6Ik11c3QgVmVuZSB0ZXJqZXIgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQoNGD0YHRgdC60L4t0LXQstGA0L7Qv9C10LnRgdC60LDRjyDQu9Cw0LnQutCwIDIw4oCTMjgg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IlJ1c3NpYW4tRXVyb3BlYW4gTGFpa2EgMjDigJMyOCBrZyIsImJyZWVkX2V0IjoiVmVuZS1FdXJvb3BhIGxhaWthIDIw4oCTMjgga2cifSx7ImJyZWVkIjoi0KHQsNC80L7QtdC0IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IlNhbW95ZWQgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiU2Ftb2plZWQgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQodCw0LzQvtC10LQgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IlNhbW95ZWQgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiU2Ftb2plZWQgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQodC10YLRgtC10YAg0LDQvdCz0LvQuNC50YHQutC40LkgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwfSwiYnJlZWRfZW4iOiJFbmdsaXNoIFNldHRlciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJJbmdsaXNlIHNldHRlciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCh0LXRgtGC0LXRgCDQs9C+0YDQtNC+0L0gMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMH0sImJyZWVkX2VuIjoiR29yZG9uIFNldHRlciAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJHb3Jkb25pIHNldHRlciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCh0LXRgtGC0LXRgCDQuNGA0LvQsNC90LTRgdC60LjQuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTB9LCJicmVlZF9lbiI6IklyaXNoIFNldHRlciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJJaXJpIHNldHRlciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCh0LjQsdCwLdC40L3RgyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjYwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NzB9LCJicmVlZF9lbiI6IlNoaWJhIEludSIsImJyZWVkX2V0IjoiU2hpYmEgSW51In0seyJicmVlZCI6ItCh0LjQu9C40YXQtdC8LdGC0LXRgNGM0LXRgCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MCwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6IlNlYWx5aGFtIFRlcnJpZXIiLCJicmVlZF9ldCI6IlNlYWx5aGFtaSB0ZXJqZXIifSx7ImJyZWVkIjoi0KHQutC+0YLRhy3RgtC10YDRjNC10YAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJTY290dGlzaCBUZXJyaWVyIiwiYnJlZWRfZXQiOiLFoG90aSB0ZXJqZXIifSx7ImJyZWVkIjoi0KLQsNC60YHQsCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3QsNGPINC60LDRgNC70LjQutC+0LLQsNGPIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo1MH0sImJyZWVkX2VuIjoiRGFjaHNodW5kIHNtb290aCBtaW5pYXR1cmUgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIgbMO8aGlrYXJ2YWxpbmUga8Okw6RidXMgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90LDRjyDQutGA0L7Qu9C40YfRjNGPINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjI1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo0NX0sImJyZWVkX2VuIjoiRGFjaHNodW5kIHNtb290aCByYWJiaXQgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiVGFrc2lrb2VyIGzDvGhpa2FydmFsaW5lIGvDvMO8bGlrIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdCw0Y8g0YHRgtCw0L3QtNCw0YDRgtC90LDRjyAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjYwfSwiYnJlZWRfZW4iOiJEYWNoc2h1bmQgc21vb3RoIHN0YW5kYXJkIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBsw7xoaWthcnZhbGluZSBzdGFuZGFyZCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90LDRjyDQutCw0YDQu9C40LrQvtCy0LDRjyA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiRGFjaHNodW5kIGxvbmctY29hdGVkIG1pbmlhdHVyZSA14oCTMTAga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBwaWtrYXJ2YWxpbmUga8Okw6RidXMgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90LDRjyDQutGA0L7Qu9C40YfRjNGPINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJEYWNoc2h1bmQgbG9uZy1jb2F0ZWQgcmFiYml0IHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBwaWtrYXJ2YWxpbmUga8O8w7xsaWsga3VuaSA1IGtnIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90LDRjyDRgdGC0LDQvdC00LDRgNGC0L3QsNGPIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MH0sImJyZWVkX2VuIjoiRGFjaHNodW5kIGxvbmctY29hdGVkIHN0YW5kYXJkIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBwaWtrYXJ2YWxpbmUgc3RhbmRhcmQgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINC20LXRgdGC0LrQvtGI0LXRgNGB0YLQvdCw0Y8g0LrQsNGA0LvQuNC60L7QstCw0Y8gNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJEYWNoc2h1bmQgd2lyZS1oYWlyZWQgbWluaWF0dXJlIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiVGFrc2lrb2VyIGthcnVrYXJ2YWxpbmUga8Okw6RidXMgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90LDRjyDQutGA0L7Qu9C40YfRjNGPINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1LCLQotGA0LjQvNC80LjQvdCzIjo1NX0sImJyZWVkX2VuIjoiRGFjaHNodW5kIHdpcmUtaGFpcmVkIHJhYmJpdCB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIga2FydWthcnZhbGluZSBrw7zDvGxpayBrdW5pIDUga2cifSx7ImJyZWVkIjoi0KLQsNC60YHQsCDQttC10YHRgtC60L7RiNC10YDRgdGC0L3QsNGPINGB0YLQsNC90LTQsNGA0YLQvdCw0Y8gMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwLCLQotGA0LjQvNC80LjQvdCzIjo3NX0sImJyZWVkX2VuIjoiRGFjaHNodW5kIHdpcmUtaGFpcmVkIHN0YW5kYXJkIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBrYXJ1a2FydmFsaW5lIHN0YW5kYXJkIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KPQuNC/0L/QtdGCIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1fSwiYnJlZWRfZW4iOiJXaGlwcGV0IDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IldoaXBwZXQgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQo9C40L/Qv9C10YIgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTB9LCJicmVlZF9lbiI6IldoaXBwZXQgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiV2hpcHBldCAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCk0LjQvdGB0LrQuNC5INC70LDQv9GF0YPQvdC0IDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4NX0sImJyZWVkX2VuIjoiRmlubmlzaCBMYXBwaHVuZCAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiJTb29tZSBsYW1iYWtvZXIgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQpNC40L3RgdC60LjQuSDQu9Cw0L/RhdGD0L3QtCAyMOKAkzI0INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTB9LCJicmVlZF9lbiI6IkZpbm5pc2ggTGFwcGh1bmQgMjDigJMyNCBrZyIsImJyZWVkX2V0IjoiU29vbWUgbGFtYmFrb2VyIDIw4oCTMjQga2cifSx7ImJyZWVkIjoi0KTQvtC60YHRgtC10YDRjNC10YAg0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90YvQuSAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzAsItCi0YDQuNC80LzQuNC90LMiOjc1fSwiYnJlZWRfZW4iOiJXaXJlIEZveCBUZXJyaWVyIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IkthcnVrYXJ2YWxpbmUgZm94dGVyamVyIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KTQvtC60YHRgtC10YDRjNC10YAg0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90YvQuSA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MCwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6IldpcmUgRm94IFRlcnJpZXIgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJLYXJ1a2FydmFsaW5lIGZveHRlcmplciA14oCTMTAga2cifSx7ImJyZWVkIjoi0KTRgNCw0L3RhtGD0LfRgdC60LjQuSDQsdGD0LvRjNC00L7QsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjB9LCJicmVlZF9lbiI6IkZyZW5jaCBCdWxsZG9nIiwiYnJlZWRfZXQiOiJQcmFudHN1c2UgYnVsZG9nIn0seyJicmVlZCI6ItCl0LDRgdC60LggMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiU2liZXJpYW4gSHVza3kgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiU2liZXJpIGh1c2t5IDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0KXQsNGB0LrQuCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiU2liZXJpYW4gSHVza3kgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiU2liZXJpIGh1c2t5IDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0KbQstC10YDQs9GI0L3QsNGD0YbQtdGAIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MCwi0KLRgNC40LzQvNC40L3QsyI6NzV9LCJicmVlZF9lbiI6Ik1pbmlhdHVyZSBTY2huYXV6ZXIgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiS8Okw6RidXPFoW5hdXRzZXIgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQptCy0LXRgNCz0YjQvdCw0YPRhtC10YAgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJNaW5pYXR1cmUgU2NobmF1emVyIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiS8Okw6RidXPFoW5hdXRzZXIgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCn0LDRgy3Rh9Cw0YMgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJDaG93IENob3cgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiQ2hvdyBDaG93IDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0KfQsNGDLdGH0LDRgyAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTEwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJDaG93IENob3cgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiQ2hvdyBDaG93IDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0KfQuNGF0YPQsNGF0YPQsCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3Ri9C5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjI1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MzV9LCJicmVlZF9lbiI6IkNoaWh1YWh1YSBzbW9vdGgiLCJicmVlZF9ldCI6IlTFoWlodWFodWEgbMO8aGlrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0KfQuNGF0YPQsNGF0YPQsCDQtNC70LjQvdC90L7RiNC10YDRgdGC0L3Ri9C5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJDaGlodWFodWEgbG9uZy1jb2F0ZWQiLCJicmVlZF9ldCI6IlTFoWlodWFodWEgcGlra2FydmFsaW5lIn0seyJicmVlZCI6ItCo0LDRgNC/0LXQuSAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjY1fSwiYnJlZWRfZW4iOiJTaGFyIFBlaSAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiLFoGFyLVBlaSAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCo0LDRgNC/0LXQuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjcwfSwiYnJlZWRfZW4iOiJTaGFyIFBlaSAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiLFoGFyLVBlaSAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCo0LXQu9GC0LgiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2MH0sImJyZWVkX2VuIjoiU2hldGxhbmQgU2hlZXBkb2ciLCJicmVlZF9ldCI6IsWgZXRsYW5kaSBsYW1iYWtvZXIifSx7ImJyZWVkIjoi0KjQuC3RgtGG0YMgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IlNoaWggVHp1IDXigJMxMCBrZyIsImJyZWVkX2V0IjoiU2hpaCBUenUgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCo0Lgt0YLRhtGDINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJTaGloIFR6dSB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJTaGloIFR6dSBrdW5pIDUga2cifSx7ImJyZWVkIjoi0KjQvdCw0YPRhtC10YAg0LzQuNC90LjQsNGC0Y7RgNC90YvQuSDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiTWluaWF0dXJlIFNjaG5hdXplciB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJLw6TDpGJ1c8WhbmF1dHNlciBrdW5pIDUga2cifSx7ImJyZWVkIjoi0KjQv9C40YYg0L3QtdC80LXRhtC60LjQuSAvINC/0L7QvNC10YDQsNC90YHQutC40Lkg0LHQvtC70LXQtSAzLDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjYwfSwiYnJlZWRfZW4iOiJHZXJtYW4gU3BpdHogLyBQb21lcmFuaWFuIG92ZXIgMyw1IGtnIiwiYnJlZWRfZXQiOiJTYWtzYSBzcGl0cyAvIFBvbWVyYW5pYW4gw7xsZSAzLDUga2cifSx7ImJyZWVkIjoi0KjQv9C40YYg0L3QtdC80LXRhtC60LjQuSAvINC/0L7QvNC10YDQsNC90YHQutC40Lkg0LTQviAzLDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjU1fSwiYnJlZWRfZW4iOiJHZXJtYW4gU3BpdHogLyBQb21lcmFuaWFuIHVwIHRvIDMsNSBrZyIsImJyZWVkX2V0IjoiU2Frc2Egc3BpdHMgLyBQb21lcmFuaWFuIGt1bmkgMyw1IGtnIn0seyJicmVlZCI6ItCo0L/QuNGGINGP0L/QvtC90YHQutC40LkiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2MH0sImJyZWVkX2VuIjoiSmFwYW5lc2UgU3BpdHoiLCJicmVlZF9ldCI6IkphYXBhbmkgc3BpdHMifSx7ImJyZWVkIjoi0KnQtdC90LrQuCIsInNlcnZpY2VzIjp7ItCS0YHRjyDQv9GA0L7Qs9GA0LDQvNC80LAiOjU1fSwiYnJlZWRfZW4iOiJQdXBwaWVzIiwiYnJlZWRfZXQiOiJLdXRzaWthZCJ9LHsiYnJlZWQiOiLQrdGB0YLQvtC90YHQutCw0Y8g0LPQvtC90YfQsNGPIDE14oCTMjUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwfSwiYnJlZWRfZW4iOiJFc3RvbmlhbiBIb3VuZCAxNeKAkzI1IGtnIiwiYnJlZWRfZXQiOiJFZXN0aSBoYWdpamFzIDE14oCTMjUga2cifSx7ImJyZWVkIjoi0K/Qv9C+0L3RgdC60LjQuSDRhdC40L0iLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkphcGFuZXNlIENoaW4iLCJicmVlZF9ldCI6IkphYXBhbmkgQ2hpbiJ9LHsiYnJlZWQiOiLQmtC+0YjQutCwINC60L7RgNC+0YLQutC+0YjQtdGA0YHRgtC90LDRjyIsInNlcnZpY2VzIjp7ItCS0YvRh9C10YEiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiQ2F0IHNob3J0LWhhaXJlZCIsImJyZWVkX2V0IjoiS2FzcyBsw7xoaWthcnZhbGluZSJ9LHsiYnJlZWQiOiLQmtC+0YjQutCwINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdCw0Y8iLCJzZXJ2aWNlcyI6eyLQktGL0YfQtdGBIjo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODB9LCJicmVlZF9lbiI6IkNhdCBsb25nLWhhaXJlZCIsImJyZWVkX2V0IjoiS2FzcyBwaWtrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0JrQvtGI0LrQsCDQnNC10LnQvS3QutGD0L0iLCJzZXJ2aWNlcyI6eyLQktGL0YfRkdGBIjo2MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTB9LCJicmVlZF9lbiI6IkNhdCBNYWluZSBDb29uIiwiYnJlZWRfZXQiOiJLYXNzIE1haW5lIENvb24ifV07CnZhciBSQUlMV0FZID0gImh0dHBzOi8vcmpncm9vbWluZy51cC5yYWlsd2F5LmFwcC9ib29rIjsKdmFyIEdPT0dMRV9TQ1JJUFQgPSAiaHR0cHM6Ly9zY3JpcHQuZ29vZ2xlLmNvbS9tYWNyb3Mvcy9BS2Z5Y2J5VFNaLWVKTWRlcC1EMExyLW54MF9WNEhCV2dJSWN0blJUMnJqU0R2QnliajVDWUkzTksyTXFjQXdfY2ZjemdSRWlmZy9leGVjIjsKdmFyIEZBTExCQUNLX1RJTUVTID0gWycxMDowMCcsJzEwOjMwJywnMTE6MDAnLCcxMTozMCcsJzEyOjAwJywnMTI6MzAnLCcxMzowMCcsJzEzOjMwJywnMTQ6MDAnLCcxNDozMCcsJzE1OjAwJywnMTU6MzAnLCcxNjowMCcsJzE2OjMwJywnMTc6MDAnLCcxNzozMCcsJzE4OjAwJ107CnZhciBib29raW5nID0ge2JyZWVkOicnLGJyZWVkRGlzcGxheTonJyxzZXJ2aWNlOicnLHByaWNlOjAsbWFzdGVyOicnLGdyb29tSGlzdG9yeTonJyxkYXRlOicnLHRpbWU6JycsbGFuZzoncnUnfTsKdmFyIHNlbEJyZWVkID0gbnVsbDsKdmFyIGNZID0gbmV3IERhdGUoKS5nZXRGdWxsWWVhcigpOwp2YXIgY00gPSBuZXcgRGF0ZSgpLmdldE1vbnRoKCk7CnZhciBzdGVwID0gMTsKdmFyIE1PTlRIUyA9IFsn0K/QvdCy0LDRgNGMJywn0KTQtdCy0YDQsNC70YwnLCfQnNCw0YDRgicsJ9CQ0L/RgNC10LvRjCcsJ9Cc0LDQuScsJ9CY0Y7QvdGMJywn0JjRjtC70YwnLCfQkNCy0LPRg9GB0YInLCfQodC10L3RgtGP0LHRgNGMJywn0J7QutGC0Y/QsdGA0YwnLCfQndC+0Y/QsdGA0YwnLCfQlNC10LrQsNCx0YDRjCddOwoKZnVuY3Rpb24gc2hvd1NjcmVlbihpZCkgewogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5zY3JlZW4nKS5mb3JFYWNoKGZ1bmN0aW9uKHMpe3MuY2xhc3NMaXN0LnJlbW92ZSgnYWN0aXZlJyk7fSk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoaWQpLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpOwogIHdpbmRvdy5zY3JvbGxUbygwLDApOwp9CgpmdW5jdGlvbiBnb1N0ZXAobikgewogIFsnYmsxJywnYmsyJywnYmszJywnYms0JywnYms1J10uZm9yRWFjaChmdW5jdGlvbihpZCxpKXsKICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKGlkKS5jbGFzc05hbWUgPSAnc3RlcCcgKyAoaSsxPT09bj8nIHNob3cnOicnKTsKICB9KTsKICBmb3IodmFyIGk9MTtpPD01O2krKyl7CiAgICB2YXIgcHM9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3BzJytpKTsKICAgIHZhciBwbD1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgncGwnK2kpOwogICAgaWYoaTxuKXtwcy5jbGFzc05hbWU9J3BzIGRvbmUnO2lmKHBsKXBsLmNsYXNzTmFtZT0ncGwgZG9uZSc7fQogICAgZWxzZSBpZihpPT09bil7cHMuY2xhc3NOYW1lPSdwcyBhY3RpdmUnO2lmKHBsKXBsLmNsYXNzTmFtZT0ncGwnO30KICAgIGVsc2V7cHMuY2xhc3NOYW1lPSdwcyc7aWYocGwpcGwuY2xhc3NOYW1lPSdwbCc7fQogIH0KICBzdGVwPW47IHdpbmRvdy5zY3JvbGxUbygwLDApOwogIGlmKG49PT0yKSBmaWx0ZXJNYXN0ZXJzKCk7Cn0KCmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdib29rQnRuJykub25jbGljayA9IGZ1bmN0aW9uKCl7CiAgc2hvd1NjcmVlbignYm9va1NjcmVlbicpOyBnb1N0ZXAoMSk7IGJ1aWxkQ2FsKCk7Cn07CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdiYWNrQnRuJykub25jbGljayA9IGZ1bmN0aW9uKCl7CiAgaWYoc3RlcD4xKXtnb1N0ZXAoc3RlcC0xKTt9ZWxzZXtzaG93U2NyZWVuKCdob21lU2NyZWVuJyk7fQp9Owpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnaG9tZUJ0bicpLm9uY2xpY2sgPSBmdW5jdGlvbigpewogIHNob3dTY3JlZW4oJ2hvbWVTY3JlZW4nKTsgcmVzZXRBbGwoKTsKfTsKCi8vIEJyZWVkIHNlYXJjaAp2YXIgaW5wID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2JJbnB1dCcpOwp2YXIgZHJvcCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdiRHJvcCcpOwp2YXIgY2xyID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NsckJ0bicpOwp2YXIgYmFkZ2UgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc0JhZGdlJyk7CgppbnAuYWRkRXZlbnRMaXN0ZW5lcignaW5wdXQnLCBmdW5jdGlvbigpewogIHZhciBxID0gaW5wLnZhbHVlLnRyaW0oKTsKICBjbHIuY2xhc3NMaXN0LnRvZ2dsZSgnc2hvdycsIHEubGVuZ3RoPjApOwogIGlmKCFxKXtkcm9wLmNsYXNzTGlzdC5yZW1vdmUoJ29wZW4nKTtkcm9wLmlubmVySFRNTD0nJztyZXR1cm47fQogIHZhciBzZj1MQU5HPT09J2VuJz8nYnJlZWRfZW4nOkxBTkc9PT0nZXQnPydicmVlZF9ldCc6J2JyZWVkJzsKICB2YXIgcmVzPURBVEEuZmlsdGVyKGZ1bmN0aW9uKGIpe3JldHVybihiW3NmXXx8Yi5icmVlZCkudG9Mb3dlckNhc2UoKS5pbmRleE9mKHEudG9Mb3dlckNhc2UoKSkhPT0tMTt9KS5zbGljZSgwLDM1KTsKICBkcm9wLmlubmVySFRNTD0nJzsKICB2YXIgX25yPUxBTkc9PT0nZW4nPydCcmVlZCBub3QgZm91bmQnOkxBTkc9PT0nZXQnPydUw7V1Z3UgZWkgbGVpdHVkJzon0J/QvtGA0L7QtNCwINC90LUg0L3QsNC50LTQtdC90LAnOwogIHZhciBfbnQ9TEFORz09PSdlbic/IkNhbid0IGZpbmQgeW91ciBicmVlZD8iOkxBTkc9PT0nZXQnPydFaSBsZWlhIG9tYSB0w7V1Z3U/Jzon0J3QtSDQvdCw0YjQu9C4INGB0LLQvtGOINC/0L7RgNC+0LTRgz8nOwogIHZhciBfbnM9TEFORz09PSdlbic/J0NvbnRhY3QgdXMg4oCUIHdlIHdpbGwgaGVscCB5b3UgY2hvb3NlIGEgc2VydmljZSc6TEFORz09PSdldCc/J1bDtXRrZSBtZWllZ2Egw7xoZW5kdXN0IOKAlCBhaXRhbWUgdGVlbnVzZSB2YWxpZGEnOifQodCy0Y/QttC40YLQtdGB0Ywg0YEg0L3QsNC80Lgg0LvRjtCx0YvQvCDRg9C00L7QsdC90YvQvCDRgdC/0L7RgdC+0LHQvtC8IOKAlCDQvNGLINC/0L7QvNC+0LbQtdC8INC/0L7QtNC+0LHRgNCw0YLRjCDRg9GB0LvRg9Cz0YMnOwogIGlmKCFyZXMubGVuZ3RoKXtkcm9wLmlubmVySFRNTD0nPGRpdiBjbGFzcz0ibm9yZXMiPicrX25yKyc8L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXIiIG9uY2xpY2s9InNob3dTY3JlZW4oXCdob21lU2NyZWVuXCcpIj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItaWNvbiI+8J+QvjwvZGl2PjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci10ZXh0Ij48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItdGl0bGUiPicrX250Kyc8L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItc3ViIj4nK19ucysnPC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLWFycm93Ij7ihpI8L2Rpdj48L2Rpdj4nO30KICBlbHNlewogICAgcmVzLmZvckVhY2goZnVuY3Rpb24oYil7CiAgICAgIHZhciBkPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpOyBkLmNsYXNzTmFtZT0nZGl0ZW0nOwogICAgICB2YXIgYm5hbWU9YltzZl18fGIuYnJlZWQ7CiAgICAgIHZhciBpZHg9Ym5hbWUudG9Mb3dlckNhc2UoKS5pbmRleE9mKHEudG9Mb3dlckNhc2UoKSk7CiAgICAgIGQuaW5uZXJIVE1MPWJuYW1lLnN1YnN0cmluZygwLGlkeCkrJzxtYXJrPicrYm5hbWUuc3Vic3RyaW5nKGlkeCxpZHgrcS5sZW5ndGgpKyc8L21hcms+JytibmFtZS5zdWJzdHJpbmcoaWR4K3EubGVuZ3RoKTsKICAgICAgZC5vbmNsaWNrPWZ1bmN0aW9uKCl7c2VsZWN0QnJlZWQoYik7fTsKICAgICAgZHJvcC5hcHBlbmRDaGlsZChkKTsKICAgIH0pOwogIH0KICBkcm9wLmNsYXNzTGlzdC5hZGQoJ29wZW4nKTsKfSk7Cgpkb2N1bWVudC5hZGRFdmVudExpc3RlbmVyKCdjbGljaycsZnVuY3Rpb24oZSl7CiAgaWYoIWUudGFyZ2V0LmNsb3Nlc3QoJy5id3JhcCcpKWRyb3AuY2xhc3NMaXN0LnJlbW92ZSgnb3BlbicpOwp9KTsKY2xyLm9uY2xpY2sgPSByZXNldEJyZWVkOwoKZnVuY3Rpb24gc2VsZWN0QnJlZWQoYil7CiAgc2VsQnJlZWQ9YjsgYm9va2luZy5icmVlZD1iLmJyZWVkOwogIGlucC52YWx1ZT0nJzsgY2xyLmNsYXNzTGlzdC5yZW1vdmUoJ3Nob3cnKTsKICBkcm9wLmNsYXNzTGlzdC5yZW1vdmUoJ29wZW4nKTsgZHJvcC5pbm5lckhUTUw9Jyc7CiAgYmFkZ2UuaW5uZXJIVE1MPScnOwogIHZhciBiRmllbGQ9TEFORz09PSdlbic/J2JyZWVkX2VuJzpMQU5HPT09J2V0Jz8nYnJlZWRfZXQnOidicmVlZCc7CiAgdmFyIGRpc3BCcmVlZD1iW2JGaWVsZF18fGIuYnJlZWQ7CiAgYm9va2luZy5icmVlZERpc3BsYXk9ZGlzcEJyZWVkOwogIHZhciBibj1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdzcGFuJyk7Ym4uY2xhc3NOYW1lPSdibmFtZSc7Ym4udGV4dENvbnRlbnQ9ZGlzcEJyZWVkOwogIHZhciBjaGdUeHQ9TEFORz09PSdlbic/J0NoYW5nZSc6TEFORz09PSdldCc/J011dWRhJzon0JjQt9C80LXQvdC40YLRjCc7CiAgdmFyIGJjPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NwYW4nKTtiYy5jbGFzc05hbWU9J2JjaGcnO2JjLnRleHRDb250ZW50PWNoZ1R4dDsKICBiYy5vbmNsaWNrPXJlc2V0QnJlZWQ7CiAgYmFkZ2UuYXBwZW5kQ2hpbGQoYm4pO2JhZGdlLmFwcGVuZENoaWxkKGJjKTsKICBiYWRnZS5jbGFzc0xpc3QuYWRkKCdzaG93Jyk7CiAgcmVuZGVyU3ZjcyhiKTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjU2VjJykuc3R5bGUuZGlzcGxheT0nYmxvY2snOwogICAgLy8gQWRkIGltcG9ydGFudCBub3RlIGlmIG5vdCBleGlzdHMKICAgIGlmKCFkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjTm90ZScpKXsKICAgICAgdmFyIG5vdGU9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7CiAgICAgIG5vdGUuaWQ9J3N2Y05vdGUnOwogICAgICBub3RlLnN0eWxlLmNzc1RleHQ9J2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDgpO3BhZGRpbmc6MTRweCAxNnB4O2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDIpO21hcmdpbi10b3A6MTJweDsnOwogICAgICB2YXIgbm90ZVRpdGxlPUxBTkc9PT0nZW4nPydQbGVhc2Ugbm90ZSc6TEFORz09PSdldCc/J1BhbmdlIHTDpGhlbGUnOifQktCw0LbQvdC+INC30L3QsNGC0YwnOwogICAgICB2YXIgbm90ZUJvZHk9TEFORz09PSdlbic/J0ZpbmFsIHByaWNlIGRlcGVuZHMgb24gY29hdCBjb25kaXRpb24gYW5kIHBldCBiZWhhdmlvdXIuPGJyPkRlbWF0dGluZyBmcm9tIDUg4oKsLjxicj5BZ2dyZXNzaXZlIGJlaGF2aW91ciBzdXJjaGFyZ2UgbWF5IGFwcGx5OiArNTAlLic6TEFORz09PSdldCc/J0zDtXBsaWsgaGluZCBzw7VsdHViIGthcnZhc3Rpa3Ugc2Vpc3VuZGlzdCBqYSBsZW1taWtsb29tYSBrw6RpdHVtaXNlc3QuPGJyPktvbHRzdW5pdGUgbGFodGloYXJ1dGFtaW5lIGFsYXRlcyA1IOKCrC48YnI+QWdyZXNzaWl2c2Uga8OkaXR1bWlzZSBrb3JyYWwgdsO1aWIgbGlzYW5kdWRhIDUwJSBqdXVyZGVoaW5kbHVzLic6J9Ce0LrQvtC90YfQsNGC0LXQu9GM0L3QsNGPINGB0YLQvtC40LzQvtGB0YLRjCDQt9Cw0LLQuNGB0LjRgiDQvtGCINGB0L7RgdGC0L7Rj9C90LjRjyDRiNC10YDRgdGC0Lgg0Lgg0L/QvtCy0LXQtNC10L3QuNGPINC/0LjRgtC+0LzRhtCwLjxicj7QoNCw0LfQsdC+0YAg0LrQvtC70YLRg9C90L7QsiDigJQg0L7RgiA1IOKCrC48YnI+0J/RgNC4INCw0LPRgNC10YHRgdC40LLQvdC+0Lwg0L/QvtCy0LXQtNC10L3QuNC4INC80L7QttC10YIg0L/RgNC40LzQtdC90Y/RgtGM0YHRjyDQtNC+0L/Qu9Cw0YLQsCA1MCUuJzsKICAgICAgbm90ZS5pbm5lckhUTUw9JzxkaXYgc3R5bGU9ImZvbnQtc2l6ZTowLjgzOHJlbTtsZXR0ZXItc3BhY2luZzouMTVlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjhweDtmb250LXdlaWdodDo2MDA7Zm9udC1mYW1pbHk6XCdNb250c2VycmF0XCcsc2Fucy1zZXJpZiI+Jytub3RlVGl0bGUrJzwvZGl2PjxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxLjAyNXJlbTtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjEuODtmb250LWZhbWlseTpcJ01vbnRzZXJyYXRcJyxzYW5zLXNlcmlmIj4nK25vdGVCb2R5Kyc8L2Rpdj4nOwogICAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjU2VjJykuYXBwZW5kQ2hpbGQobm90ZSk7CiAgICB9CiAgZmlsdGVyTWFzdGVycygpOwp9CgpmdW5jdGlvbiByZXNldEJyZWVkKCl7CiAgc2VsQnJlZWQ9bnVsbDtib29raW5nLmJyZWVkPScnO2Jvb2tpbmcuc2VydmljZT0nJztib29raW5nLnByaWNlPTA7CiAgaW5wLnZhbHVlPScnO2Nsci5jbGFzc0xpc3QucmVtb3ZlKCdzaG93Jyk7CiAgYmFkZ2UuY2xhc3NMaXN0LnJlbW92ZSgnc2hvdycpO2JhZGdlLmlubmVySFRNTD0nJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjU2VjJykuc3R5bGUuZGlzcGxheT0nbm9uZSc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y0xpc3QnKS5pbm5lckhUTUw9Jyc7Cn0KCgp2YXIgU1ZDX1RSQU5TTEFUSU9OUyA9IHsKICAn0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQnOiAgICAgIHtlbjonQmFzaWMgZ3Jvb20nLCAgICAgIGV0OidQw7VoaWhvb2xkdXMnfSwKICAn0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQnOntlbjonSHlnaWVuZSBncm9vbScsICAgIGV0OidIw7xnaWVlbmlob29sZHVzJ30sCiAgJ9Ca0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQnOiAge2VuOidGdWxsIGdyb29tJywgICAgICAgIGV0OidUw6RpZWxpayBob29sZHVzJ30sCiAgJ9Ci0YDQuNC80LzQuNC90LMnOiAgICAgICAgICB7ZW46J1RyaW1taW5nJywgICAgICAgICAgZXQ6J1RyaW1tZXJpbWluZSd9LAogICfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6ICAge2VuOidFeHByZXNzIHNoZWQnLCAgICAgIGV0OidLaWlya2FydmF2YWhldHVzJ30sCiAgJ9CS0YvRh9C10YEnOiAgICAgICAgICAgICB7ZW46J0JydXNoLW91dCcsICAgICAgICAgZXQ6J0hhcmphbWluZSd9LAogICfQktGB0Y8g0L/RgNC+0LPRgNCw0LzQvNCwJzogICAgIHtlbjonRnVsbCBwcm9ncmFtJywgICAgICBldDonS29ndSBwcm9ncmFtbSd9Cn07CnZhciBTVkNfVEFHTElORV9JMThOPXsKICBydTp7J9CS0YvRh9C10YEnOifQodGC0L7QuNC80L7RgdGC0Ywg0LfQsNCy0LjRgdC40YIg0L7RgiDRgdC+0YHRgtC+0Y/QvdC40Y8g0YjQtdGA0YHRgtC4INC4INC+0LHRitGR0LzQsCDRgNCw0LHQvtGCJywn0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQnOifQn9C+0LTRhdC+0LTQuNGCINC00LvRjyDQv9C+0LTQtNC10YDQttCw0L3QuNGPINGH0LjRgdGC0L7RgtGLINC80LXQttC00YMg0L/RgNC+0YbQtdC00YPRgNCw0LzQuCcsJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Jzon0JTQu9GPINC60L7QvNGE0L7RgNGC0LAg0Lgg0LDQutC60YPRgNCw0YLQvdC+0YHRgtC4INC/0LjRgtC+0LzRhtCwJywn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc6J9Cf0L7Qu9C90YvQuSDRg9GF0L7QtCDRgdC+INGB0YLRgNC40LbQutC+0LknLCfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6J9Cf0L7QvNC+0LPQsNC10YIg0YPQvNC10L3RjNGI0LjRgtGMINC60L7Qu9C40YfQtdGB0YLQstC+INC70LjQvdGP0Y7RidC10Lkg0YjQtdGA0YHRgtC4Jywn0KLRgNC40LzQvNC40L3Qsyc6J9CU0LvRjyDQttC10YHRgtC60L7RiNC10YDRgdGC0L3Ri9GFINC/0L7RgNC+0LQnfSwKICBlbjp7J9CS0YvRh9C10YEnOidQcmljZSBkZXBlbmRzIG9uIGNvYXQgY29uZGl0aW9uIGFuZCB2b2x1bWUgb2Ygd29yaycsJ9CR0LDQt9C+0LLRi9C5INGD0YXQvtC0JzonSWRlYWwgZm9yIG1haW50YWluaW5nIGNsZWFubGluZXNzIGJldHdlZW4gZnVsbCBncm9vbXMnLCfQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCc6J0ZvciB5b3VyIHBldFwncyBjb21mb3J0IGFuZCBuZWF0bmVzcycsJ9Ca0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQnOidGdWxsIGdyb29taW5nIHdpdGggaGFpcmN1dCcsJ9Ct0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwJzonU2lnbmlmaWNhbnRseSByZWR1Y2VzIHNoZWRkaW5nJywn0KLRgNC40LzQvNC40L3Qsyc6J0ZvciB3aXJlLWhhaXJlZCBicmVlZHMnfSwKICBldDp7J9CS0YvRh9C10YEnOidIaW5kIHPDtWx0dWIga2FydmFzdGlrdSBzZWlzdW5kaXN0IGphIHTDtsO2bWFodXN0Jywn0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQnOidTb2JpYiBwdWh0dXNlIGhvaWRtaXNla3MgcHJvdHNlZHV1cmlkZSB2YWhlbCcsJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0JzonTGVtbWlrbG9vbWEgbXVnYXZ1c2VrcyBqYSBrb3JyYXNob2l1a3MnLCfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0JzonVMOkaWVsaWsgaG9vbGR1cyBrb29zIGzDtWlrdXNlZ2EnLCfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6J1bDpGhlbmRhYiBvbHVsaXNlbHQga2FydmFkZSBsYW5nZW1pc3QnLCfQotGA0LjQvNC80LjQvdCzJzonVHJhYXRrYXJ2YWxpc3RlbGUgdMO1dWd1ZGVsZSd9Cn07CnZhciBTVkNfREVTQ19JMThOPXsKICBydTp7J9CS0YvRh9C10YEnOifQp9C40YHRgtC60LAg0LPQu9Cw0LcsINGD0YjQtdC5LCDQv9C+0LTRgdGC0YDQuNCz0LDQvdC40LUg0LrQvtCz0YLQtdC5LCDQstGL0YfRkdGBICjQtNC70Y8g0LrQvtGI0LXQuiknLCfQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCc6J9Cc0YvRgtGM0ZEg0L/RgNC+0YTQtdGB0YHQuNC+0L3QsNC70YzQvdGL0LzQuCDRgdGA0LXQtNGB0YLQstCw0LzQuCwg0LTQtdC70LjQutCw0YLQvdCw0Y8g0YHRg9GI0LrQsCcsJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Jzon0KHRgtGA0LjQttC60LAg0LrQvtCz0YLQtdC5LCDRh9C40YHRgtC60LAg0YPRiNC10Lkg0Lgg0LPQu9Cw0LcsINC60YPQv9Cw0L3QuNC1LCDRgdGD0YjQutCwLCDRg9GF0L7QtCDQt9CwINC70LDQv9C60LDQvNC4INC4INGH0YPQstGB0YLQstC40YLQtdC70YzQvdGL0LzQuCDQt9C+0L3QsNC80LgnLCfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Jzon0KHRgtGA0LjQttC60LAg0LrQvtCz0YLQtdC5LCDRh9C40YHRgtC60LAg0YPRiNC10Lkg0Lgg0LPQu9Cw0LcsINC60YPQv9Cw0L3QuNC1LCDRgdGD0YjQutCwLCDRg9GF0L7QtCDQt9CwINC70LDQv9C60LDQvNC4INC4INGH0YPQstGB0YLQstC40YLQtdC70YzQvdGL0LzQuCDQt9C+0L3QsNC80LgsINC80L7QtNC10LvRjNC90LDRjyDRgdGC0YDQuNC20LrQsCcsJ9Ct0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwJzon0JzRi9GC0YzRkSwg0YHRg9GI0LrQsCwg0YPRhdC+0LQg0LfQsCDRiNC10YDRgdGC0YzRjiwg0LzQsNGB0LrQsCwg0L/QvtC00YHRgtGA0LjQs9Cw0L3QuNC1INC60L7Qs9GC0LXQuSwg0YfQuNGB0YLQutCwINGD0YjQtdC5INC4INCz0LvQsNC3LCDRg9GF0L7QtCDQt9CwINC70LDQv9Cw0LzQuCDQuCDQt9C+0L3QsNC80Lgg0YLRgNC10LHRg9GO0YnQuNC80Lgg0L7RgdC+0LHQvtCz0L4g0LLQvdC40LzQsNC90LjRjycsJ9Ci0YDQuNC80LzQuNC90LMnOifQktGL0YnQuNC/0YvQstCw0L3QuNC1INGB0YLQsNGA0L7Qs9C+INGB0LvQvtGPINGI0LXRgNGB0YLQuCwg0LzRi9GC0YzRkSwg0YHRg9GI0LrQsCwg0YHRgtGA0LjQttC60LAg0LrQvtCz0YLQtdC5LCDRh9C40YHRgtC60LAg0YPRiNC10Lkg0Lgg0LPQu9Cw0LcsINC+0YTQvtGA0LzQu9C10L3QuNC1INGI0LXRgNGB0YLQuCcsJ9CS0YHRjyDQv9GA0L7Qs9GA0LDQvNC80LAnOifQn9CV0KDQktCr0Jkg0JLQmNCX0JjQoiAoMjAtMzAg0LzQuNC9KSDigJQgMjAg4oKsXG7igKIg0LfQvdCw0LrQvtC80YHRgtCy0L4g0YHQviDRgdGC0L7Qu9C+0Lwg0Lgg0LjQvdGB0YLRgNGD0LzQtdC90YLQsNC80LhcbuKAoiDQu9GR0LPQutC+0LUg0LLRi9GH0ZHRgdGL0LLQsNC90LjQtVxu4oCiINC30LLRg9C60Lgg0YTQtdC90LAg0Lgg0LvQtdCz0LrQsNGPINC/0YDQvtC00YPQstC60LBcbuKAoiDQvtGB0LLQtdC20LXQvdC40LUg0LPQu9Cw0LfQvtC6INC4INGD0YjQtdC6XG7igKIg0LrQvtCz0L7RgtC60LhcbuKAoiDQstC60YPRgdC90Y/RiNC60Lgg0Lgg0YHQv9C+0LrQvtC50L3QsNGPINCw0LTQsNC/0YLQsNGG0LjRj1xuXG7QktCi0J7QoNCe0Jkg0JLQmNCX0JjQoiAoNDAtNjAg0LzQuNC9KSDigJQgMzUg4oKsXG7igKIg0L/QtdGA0LLQvtC1INC60YPQv9Cw0L3QuNC1INC4INGB0YPRiNC60LBcbuKAoiDQstGL0YfRkdGB0YvQstCw0L3QuNC1XG7igKIg0LPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LRcbuKAoiDQvdC10LHQvtC70YzRiNCw0Y8g0YHRgtGA0LjQttC60LAgLyDQutC+0YDRgNC10LrRhtC40Y8g0YjQtdGA0YHRgtC4ICjQv9GA0Lgg0L3QtdC+0LHRhdC+0LTQuNC80L7RgdGC0LgpXG7igKIg0LfQsNC60YDQtdC/0LvQtdC90LjQtSDQv9C+0LvQvtC20LjRgtC10LvRjNC90L7Qs9C+INC+0L/Ri9GC0LAnfSwKICBlbjp7J9CS0YvRh9C10YEnOidFeWUgYW5kIGVhciBjbGVhbmluZywgbmFpbCB0cmltbWluZywgYnJ1c2hpbmcgKGZvciBjYXRzKScsJ9CR0LDQt9C+0LLRi9C5INGD0YXQvtC0JzonV2FzaGluZyB3aXRoIHByb2Zlc3Npb25hbCBwcm9kdWN0cywgZ2VudGxlIGRyeWluZycsJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0JzonTmFpbCB0cmltbWluZywgZWFyIGFuZCBleWUgY2xlYW5pbmcsIGJhdGhpbmcsIGRyeWluZywgcGF3IGFuZCBzZW5zaXRpdmUgYXJlYSBjYXJlJywn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc6J05haWwgdHJpbW1pbmcsIGVhciBhbmQgZXllIGNsZWFuaW5nLCBiYXRoaW5nLCBkcnlpbmcsIHBhdyBhbmQgc2Vuc2l0aXZlIGFyZWEgY2FyZSwgc3R5bGluZyBoYWlyY3V0Jywn0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAnOidXYXNoaW5nLCBkcnlpbmcsIGNvYXQgY2FyZSwgbWFzaywgbmFpbCB0cmltbWluZywgZWFyIGFuZCBleWUgY2xlYW5pbmcsIHBhdyBhbmQgc3BlY2lhbCBhcmVhIGNhcmUnLCfQotGA0LjQvNC80LjQvdCzJzonUmVtb3Zpbmcgb2xkIGNvYXQgbGF5ZXIsIHdhc2hpbmcsIGRyeWluZywgbmFpbCB0cmltbWluZywgZWFyIGFuZCBleWUgY2xlYW5pbmcsIGNvYXQgc3R5bGluZycsJ9CS0YHRjyDQv9GA0L7Qs9GA0LDQvNC80LAnOidGSVJTVCBWSVNJVCAoMjAtMzAgbWluKSDigJQg4oKsMjBcbuKAoiBnZXR0aW5nIHVzZWQgdG8gdGhlIHRhYmxlIGFuZCB0b29sc1xu4oCiIGdlbnRsZSBicnVzaGluZ1xu4oCiIGRyeWVyIHNvdW5kcyBhbmQgbGlnaHQgYWlyZmxvd1xu4oCiIGV5ZSBhbmQgZWFyIHJlZnJlc2hcbuKAoiBuYWlsIHRyaW1cbuKAoiB0cmVhdHMgYW5kIGNhbG0gYWRhcHRhdGlvblxuXG5TRUNPTkQgVklTSVQgKDQwLTYwIG1pbikg4oCUIOKCrDM1XG7igKIgZmlyc3QgYmF0aCBhbmQgZHJ5aW5nXG7igKIgYnJ1c2hpbmdcbuKAoiBoeWdpZW5lIGNhcmVcbuKAoiBsaWdodCB0cmltIC8gY29hdCBhZGp1c3RtZW50IChpZiBuZWVkZWQpXG7igKIgcmVpbmZvcmNpbmcgdGhlIHBvc2l0aXZlIGV4cGVyaWVuY2UnfSwKICBldDp7J9CS0YvRh9C10YEnOidTaWxtYWRlIGphIGvDtXJ2YWRlIHB1aGFzdGFtaW5lLCBrw7zDvG50ZSBsw7Vpa2FtaW5lLCBoYXJqYW1pbmUgKGthc3NpZGVsZSknLCfQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCc6J1Blc2VtaW5lIHByb2Zlc3Npb25hYWxzZXRlIHZhaGVuZGl0ZWdhLCDDtXJuIGt1aXZhdGFtaW5lJywn0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQnOidLw7zDvG50ZSBsw7Vpa2FtaW5lLCBrw7VydmFkZSBqYSBzaWxtYWRlIHB1aGFzdGFtaW5lLCBwZXNlbWluZSwga3VpdmF0YW1pbmUsIGvDpHBwYWRlIGphIHR1bmRsaWtlIHBpaXJrb25kYWRlIGhvb2xkdXMnLCfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0JzonS8O8w7xudGUgbMO1aWthbWluZSwga8O1cnZhZGUgamEgc2lsbWFkZSBwdWhhc3RhbWluZSwgcGVzZW1pbmUsIGt1aXZhdGFtaW5lLCBrw6RwcGFkZSBqYSB0dW5kbGlrZSBwaWlya29uZGFkZSBob29sZHVzLCBtb2RlbGzDtWlrdXMnLCfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6J1Blc2VtaW5lLCBrdWl2YXRhbWluZSwga2FydmFzdGlrdSBob29sZHVzLCBtYXNrLCBrw7zDvG50ZSBsw7Vpa2FtaW5lLCBrw7VydmFkZSBqYSBzaWxtYWRlIHB1aGFzdGFtaW5lLCBrw6RwcGFkZSBqYSBlcmlsaXN0ZSBwaWlya29uZGFkZSBob29sZHVzJywn0KLRgNC40LzQvNC40L3Qsyc6J1ZhbmEga2FydmFraWhpIGVlbWFsZGFtaW5lLCBwZXNlbWluZSwga3VpdmF0YW1pbmUsIGvDvMO8bnRlIGzDtWlrYW1pbmUsIGvDtXJ2YWRlIGphIHNpbG1hZGUgcHVoYXN0YW1pbmUsIGthcnZhc3Rpa3Uga3VqdW5kYW1pbmUnLCfQktGB0Y8g0L/RgNC+0LPRgNCw0LzQvNCwJzonRVNJTUVORSBLw5xMQVNUVVMgKDIwLTMwIG1pbikg4oCUIDIwIOKCrFxu4oCiIHR1dHZ1bWluZSBsYXVhZ2EgamEgdMO2w7ZyaWlzdGFkZWdhXG7igKIga2VyZ2UgaGFyamFtaW5lXG7igKIgZsO2w7ZuaWhlbGlkIGphIGtlcmdlIMO1aHV2b29sXG7igKIgc2lsbWFkZSBqYSBrw7VydmFkZSB2w6Ryc2tlbmR1c1xu4oCiIGvDvMO8bnRlIGzDtWlrYW1pbmVcbuKAoiBtYWl1c2VkIGphIHJhaHVsaWsga29oYW5lbWluZVxuXG5URUlORSBLw5xMQVNUVVMgKDQwLTYwIG1pbikg4oCUIDM1IOKCrFxu4oCiIGVzaW1lbmUgdmFubml0YW1pbmUgamEga3VpdmF0YW1pbmVcbuKAoiBoYXJqYW1pbmVcbuKAoiBow7xnaWVlbmlob29sZHVzXG7igKIga2VyZ2UgbMO1aWt1cyAvIGthcnZhIGtvcnJpZ2VlcmltaW5lICh2YWphZHVzZWwpXG7igKIgcG9zaXRpaXZzZSBrb2dlbXVzZSBraW5uaXN0YW1pbmUnfQp9Owp2YXIgU1ZDX0RFU0NfQ0FUX0NPTVBMRVg9ewogIHJ1OifQnNGL0YLRjNGRLCDRgdGD0YjQutCwLCDQstGL0YfRkdGB0YvQstCw0L3QuNC1LCDRgdGC0YDQuNC20LrQsCDQutC+0LPRgtC10LksINCwINGC0LDQutC20LUg0L7QsdGA0LDQsdC+0YLQutCwINCz0LvQsNC3INC4INGD0YjQtdC6JywKICBlbjonV2FzaGluZywgZHJ5aW5nLCBicnVzaGluZywgbmFpbCB0cmltbWluZywgYW5kIGV5ZSBhbmQgZWFyIGNhcmUnLAogIGV0OidQZXNlbWluZSwga3VpdmF0YW1pbmUsIGhhcmphbWluZSwga8O8w7xudGUgbMO1aWthbWluZSBuaW5nIHNpbG1hZGUgamEga8O1cnZhZGUgaG9vbGR1cycKfTsKZnVuY3Rpb24gZ2V0U3ZjVGFnKG5hbWUpe3JldHVybihTVkNfVEFHTElORV9JMThOW0xBTkddJiZTVkNfVEFHTElORV9JMThOW0xBTkddW25hbWVdKXx8U1ZDX1RBR0xJTkVfSTE4Ti5ydVtuYW1lXXx8Jyc7fQpmdW5jdGlvbiBnZXRTdmNEZXNjKG5hbWUpewogIGlmKG5hbWU9PT0n0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCcgJiYgYm9va2luZy5icmVlZCAmJiBib29raW5nLmJyZWVkLmluZGV4T2YoJ9Ca0L7RiNC60LAnKT09PTApewogICAgdmFyIGQ9U1ZDX0RFU0NfQ0FUX0NPTVBMRVhbTEFOR118fFNWQ19ERVNDX0NBVF9DT01QTEVYLnJ1OwogICAgcmV0dXJuIGQ7CiAgfQogIHJldHVybihTVkNfREVTQ19JMThOW0xBTkddJiZTVkNfREVTQ19JMThOW0xBTkddW25hbWVdKXx8U1ZDX0RFU0NfSTE4Ti5ydVtuYW1lXXx8Jyc7Cn0KCmZ1bmN0aW9uIHJlbmRlclN2Y3MoYil7CiAgdmFyIGxibEVsPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdGVwMkxibEVsJyk7CiAgaWYobGJsRWwpewogICAgdmFyIGJhc2VMYmw9KFRbTEFOR10mJlRbTEFOR10uc3RlcDJfbGJsKXx8JzAyIMK3INCj0YHQu9GD0LPQsCc7CiAgICBsYmxFbC50ZXh0Q29udGVudD0oYi5icmVlZD09PSfQqdC10L3QutC4Jyk/KGJhc2VMYmwrJyBQdXBweSBTdGFyJyk6YmFzZUxibDsKICB9CiAgdmFyIGxpc3Q9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y0xpc3QnKTtsaXN0LmlubmVySFRNTD0nJzsKICBPYmplY3QuZW50cmllcyhiLnNlcnZpY2VzKS5mb3JFYWNoKGZ1bmN0aW9uKGt2KXsKICAgIHZhciBuYW1lPWt2WzBdLHByaWNlPWt2WzFdOwoKICAgIHZhciBkaXNwbGF5TmFtZT0oTEFORyE9PSdydScmJlNWQ19UUkFOU0xBVElPTlNbbmFtZV0pP1NWQ19UUkFOU0xBVElPTlNbbmFtZV1bTEFOR106bmFtZTsKICAgIHZhciBidG49ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnYnV0dG9uJyk7YnRuLmNsYXNzTmFtZT0nc3ZidG4nOwogICAgdmFyIHJvdz1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdkaXYnKTtyb3cuY2xhc3NOYW1lPSdzdmJ0bi1yb3cnOwogICAgdmFyIG5zPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NwYW4nKTtucy5jbGFzc05hbWU9J3N2YnRuLW5hbWUnO25zLnRleHRDb250ZW50PWRpc3BsYXlOYW1lOwogICAgdmFyIHBzPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NwYW4nKTtwcy5jbGFzc05hbWU9J3N2YnRuLXByaWNlJztwcy50ZXh0Q29udGVudD1wcmljZSsnIOKCrCc7CiAgICByb3cuYXBwZW5kQ2hpbGQobnMpO3Jvdy5hcHBlbmRDaGlsZChwcyk7CiAgICBidG4uYXBwZW5kQ2hpbGQocm93KTsKICAgIHZhciBkZXNjPWdldFN2Y0Rlc2MobmFtZSk7CiAgICBpZihkZXNjKXt2YXIgZHM9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnc3BhbicpO2RzLmNsYXNzTmFtZT0nc3ZidG4tZGVzYyc7ZHMudGV4dENvbnRlbnQ9ZGVzYztidG4uYXBwZW5kQ2hpbGQoZHMpO30KICAgIHZhciB0YWc9Z2V0U3ZjVGFnKG5hbWUpOwogICAgaWYodGFnKXt2YXIgdHM9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnc3BhbicpO3RzLmNsYXNzTmFtZT0nc3ZidG4tdGFnJzt0cy50ZXh0Q29udGVudD10YWc7YnRuLmFwcGVuZENoaWxkKHRzKTt9CiAgICBidG4ub25jbGljaz1mdW5jdGlvbigpewogICAgICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuc3ZidG4nKS5mb3JFYWNoKGZ1bmN0aW9uKGIpe2IuY2xhc3NMaXN0LnJlbW92ZSgnYWN0aXZlJyk7fSk7CiAgICAgIGJ0bi5jbGFzc0xpc3QuYWRkKCdhY3RpdmUnKTsKICAgICAgYm9va2luZy5zZXJ2aWNlPW5hbWU7Ym9va2luZy5wcmljZT1wcmljZTsKICAgICAgZmlsdGVyTWFzdGVycygpOwogICAgICBzZXRUaW1lb3V0KGZ1bmN0aW9uKCl7Z29TdGVwKDIpO30sMzAwKTsKICAgIH07CiAgICBsaXN0LmFwcGVuZENoaWxkKGJ0bik7CiAgfSk7Cn0KCi8vIE1hc3RlcnMKZnVuY3Rpb24gZmlsdGVyTWFzdGVycygpewogIHZhciBpc0NhdCA9IGJvb2tpbmcuYnJlZWQgJiYgYm9va2luZy5icmVlZC5pbmRleE9mKCfQmtC+0YjQutCwJykgPT09IDA7CiAgdmFyIGJyZWVkID0gYm9va2luZy5icmVlZCB8fCAnJzsKICB2YXIgaXNDYXRDb21wbGV4ID0gaXNDYXQgJiYgYm9va2luZy5zZXJ2aWNlID09PSAn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc7CiAgdmFyIGFubmFFeGNsdWRlID0gWyfQnNCw0LvRjNGC0LjQv9GDJywn0J/Rg9C00LXQu9GMJywn0JnQvtGA0LonLCfQkdC40YjQvtC9Jywn0JHQvtC70L7QvdC60LAnLCfQnNCw0LvRjNGC0LjQudGB0LrQsNGPJ107CiAgdmFyIGlzQW5uYUJyZWVkID0gYnJlZWQgJiYgIWFubmFFeGNsdWRlLnNvbWUoZnVuY3Rpb24oYil7IHJldHVybiBicmVlZC5pbmRleE9mKGIpICE9PSAtMTsgfSk7CiAgdmFyIGFsZXhhbmRyYUV4Y2x1ZGUgPSBbJ9Ck0L7QutGB0YLQtdGA0YzQtdGAJywn0KbQstC10YDQs9GI0L3QsNGD0YbQtdGAJ107CiAgdmFyIGlzQWxleGFuZHJhQnJlZWQgPSAhYWxleGFuZHJhRXhjbHVkZS5zb21lKGZ1bmN0aW9uKGIpeyByZXR1cm4gYnJlZWQuaW5kZXhPZihiKSAhPT0gLTE7IH0pOwogIHZhciBrc2VuaWFFeGNsdWRlID0gWyfQn9GD0LTQtdC70YwnLCfQnNCw0LvRjNGC0LjQv9GDJywn0JnQvtGA0LonXTsKICB2YXIgaXNLc2VuaWFCcmVlZCA9ICFrc2VuaWFFeGNsdWRlLnNvbWUoZnVuY3Rpb24oYil7IHJldHVybiBicmVlZC5pbmRleE9mKGIpICE9PSAtMTsgfSk7CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLm1idG4nKS5mb3JFYWNoKGZ1bmN0aW9uKGJ0bil7CiAgICB2YXIgbWFzdGVyID0gYnRuLmdldEF0dHJpYnV0ZSgnZGF0YS1tYXN0ZXInKTsKICAgIHZhciBpc1RyaW1taW5nID0gYm9va2luZy5zZXJ2aWNlID09PSAn0KLRgNC40LzQvNC40L3Qsyc7CiAgICBpZihpc0NhdENvbXBsZXgpewogICAgICBidG4uc3R5bGUuZGlzcGxheSA9IChtYXN0ZXIgPT09ICfQotCw0YLRjNGP0L3QsCcgfHwgbWFzdGVyID09PSAn0JrRgdC10L3QuNGPJykgPyAnJyA6ICdub25lJzsKICAgICAgcmV0dXJuOwogICAgfQogICAgaWYobWFzdGVyID09PSAn0JDQu9C40YHQsCcpewogICAgICBidG4uc3R5bGUuZGlzcGxheSA9IGlzQ2F0ID8gJycgOiAnbm9uZSc7CiAgICB9IGVsc2UgaWYobWFzdGVyID09PSAn0JDQvdC90LAnKXsKICAgICAgYnRuLnN0eWxlLmRpc3BsYXkgPSAoaXNBbm5hQnJlZWQgJiYgIWlzVHJpbW1pbmcpID8gJycgOiAnbm9uZSc7CiAgICB9IGVsc2UgaWYobWFzdGVyID09PSAn0JDQu9C10LrRgdCw0L3QtNGA0LAnKXsKICAgICAgYnRuLnN0eWxlLmRpc3BsYXkgPSAoaXNBbGV4YW5kcmFCcmVlZCAmJiAhaXNUcmltbWluZyAmJiAhaXNDYXQpID8gJycgOiAnbm9uZSc7CiAgICB9IGVsc2UgaWYobWFzdGVyID09PSAn0JrRgdC10L3QuNGPJyl7CiAgICAgIGJ0bi5zdHlsZS5kaXNwbGF5ID0gaXNLc2VuaWFCcmVlZCA/ICcnIDogJ25vbmUnOwogICAgfSBlbHNlIGlmKGlzVHJpbW1pbmcpewogICAgICBidG4uc3R5bGUuZGlzcGxheSA9ICdub25lJzsKICAgIH0gZWxzZSB7CiAgICAgIGJ0bi5zdHlsZS5kaXNwbGF5ID0gJyc7CiAgICB9CiAgfSk7Cn0KCmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5tYnRuJykuZm9yRWFjaChmdW5jdGlvbihidG4pewogIGJ0bi5vbmNsaWNrPWZ1bmN0aW9uKCl7CiAgICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcubWJ0bicpLmZvckVhY2goZnVuY3Rpb24oYil7Yi5jbGFzc0xpc3QucmVtb3ZlKCdhY3RpdmUnKTt9KTsKICAgIGJ0bi5jbGFzc0xpc3QuYWRkKCdhY3RpdmUnKTsKICAgIGJvb2tpbmcubWFzdGVyPWJ0bi5nZXRBdHRyaWJ1dGUoJ2RhdGEtbWFzdGVyJyk7CiAgICBzZXRUaW1lb3V0KGZ1bmN0aW9uKCl7Z29TdGVwKDMpO30sMzAwKTsKICB9Owp9KTsKCi8vIEdyb29tIGhpc3RvcnkKZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmdidG4nKS5mb3JFYWNoKGZ1bmN0aW9uKGJ0bil7CiAgYnRuLm9uY2xpY2s9ZnVuY3Rpb24oKXsKICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5nYnRuJykuZm9yRWFjaChmdW5jdGlvbihiKXtiLmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpO30pOwogICAgYnRuLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpOwogICAgYm9va2luZy5ncm9vbUhpc3Rvcnk9YnRuLmdldEF0dHJpYnV0ZSgnZGF0YS12YWwnKTsKICAgIHNldFRpbWVvdXQoZnVuY3Rpb24oKXtnb1N0ZXAoNCk7YnVpbGRDYWwoKTt9LDMwMCk7CiAgfTsKfSk7CgovLyBDYWxlbmRhcgpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgncHJldk0nKS5vbmNsaWNrPWZ1bmN0aW9uKCl7Y00tLTtpZihjTTwwKXtjTT0xMTtjWS0tO31idWlsZENhbCgpO307CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCduZXh0TScpLm9uY2xpY2s9ZnVuY3Rpb24oKXtjTSsrO2lmKGNNPjExKXtjTT0wO2NZKys7fWJ1aWxkQ2FsKCk7fTsKCnZhciBhdmFpbGFibGVEYXlzID0gW107CgpmdW5jdGlvbiBsb2FkQXZhaWxhYmxlRGF5cygpIHsKICB2YXIgbWFzdGVyID0gYm9va2luZy5tYXN0ZXI7CiAgaWYgKCFtYXN0ZXIpIHJldHVybjsKICBhdmFpbGFibGVEYXlzID0gW107CiAgZmV0Y2god2luZG93LmxvY2F0aW9uLm9yaWdpbiArICcvYXBpL2F2YWlsYWJsZV9kYXlzP21vbnRoPScgKyAoY00rMSkgKyAnJnllYXI9JyArIGNZICsgJyZtYXN0ZXI9JyArIGVuY29kZVVSSUNvbXBvbmVudChtYXN0ZXIpKQogICAgLnRoZW4oZnVuY3Rpb24ocil7IHJldHVybiByLmpzb24oKTsgfSkKICAgIC50aGVuKGZ1bmN0aW9uKGRhdGEpewogICAgICBhdmFpbGFibGVEYXlzID0gZGF0YS5hdmFpbGFibGUgfHwgW107CiAgICAgIG1hcmtBdmFpbGFibGVEYXlzKCk7CiAgICB9KQogICAgLmNhdGNoKGZ1bmN0aW9uKCl7IGF2YWlsYWJsZURheXMgPSBbXTsgfSk7Cn0KCmZ1bmN0aW9uIG1hcmtBdmFpbGFibGVEYXlzKCkgewogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5jZCcpLmZvckVhY2goZnVuY3Rpb24oYyl7aWYoIWMuY2xhc3NMaXN0LmNvbnRhaW5zKCdkaXMnKSljLmNsYXNzTGlzdC5yZW1vdmUoJ3NlbCcpO30pOwogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5jZDpub3QoLmRpcyk6bm90KC5jZG4pOm5vdCgucGFkKScpLmZvckVhY2goZnVuY3Rpb24oZWwpIHsKICAgIHZhciBkYXkgPSBlbC50ZXh0Q29udGVudC50cmltKCk7CiAgICBpZiAoIWRheSB8fCBpc05hTihwYXJzZUludChkYXkpKSkgcmV0dXJuOwogICAgdmFyIGRhdGVTdHIgPSBTdHJpbmcocGFyc2VJbnQoZGF5KSkucGFkU3RhcnQoMiwnMCcpICsgJy4nICsgU3RyaW5nKGNNKzEpLnBhZFN0YXJ0KDIsJzAnKSArICcuJyArIGNZOwogICAgaWYgKGF2YWlsYWJsZURheXMuaW5kZXhPZihkYXRlU3RyKSAhPT0gLTEpIHsKICAgICAgZWwuY2xhc3NMaXN0LmFkZCgnYXZhaWwnKTsKICAgICAgZWwuY2xhc3NMaXN0LnJlbW92ZSgnYnVzeScpOwogICAgfSBlbHNlIHsKICAgICAgZWwuY2xhc3NMaXN0LmFkZCgnYnVzeScpOwogICAgICBlbC5jbGFzc0xpc3QucmVtb3ZlKCdhdmFpbCcpOwogICAgfQogIH0pOwp9CgpmdW5jdGlvbiBidWlsZENhbCgpewogIGxvYWRBdmFpbGFibGVEYXlzKCk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NhbE0nKS50ZXh0Q29udGVudD1NT05USFNbY01dKycgJytjWTsKICBib29raW5nLmRhdGU9Jyc7IGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5jZCcpLmZvckVhY2goZnVuY3Rpb24oYyl7Yy5jbGFzc0xpc3QucmVtb3ZlKCdzZWwnKTtjLmNsYXNzTGlzdC5yZW1vdmUoJ2F2YWlsJyk7Yy5jbGFzc0xpc3QucmVtb3ZlKCdidXN5Jyk7fSk7CiAgdmFyIGc9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NhbEcnKTtnLmlubmVySFRNTD0nJzsKICBbJ9Cf0L0nLCfQktGCJywn0KHRgCcsJ9Cn0YInLCfQn9GCJywn0KHQsScsJ9CS0YEnXS5mb3JFYWNoKGZ1bmN0aW9uKGQpewogICAgdmFyIGVsPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO2VsLmNsYXNzTmFtZT0nY2RuJztlbC50ZXh0Q29udGVudD1kO2cuYXBwZW5kQ2hpbGQoZWwpOwogIH0pOwogIHZhciBmaXJzdD1uZXcgRGF0ZShjWSxjTSwxKS5nZXREYXkoKTsKICB2YXIgZGF5cz1uZXcgRGF0ZShjWSxjTSsxLDApLmdldERhdGUoKTsKICB2YXIgc3RhcnQ9Zmlyc3Q9PT0wPzY6Zmlyc3QtMTsKICB2YXIgdG9kYXk9bmV3IERhdGUoKTsKICBmb3IodmFyIGk9MDtpPHN0YXJ0O2krKyl7dmFyIGVsPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO2VsLmNsYXNzTmFtZT0nY2QgcGFkJztnLmFwcGVuZENoaWxkKGVsKTt9CiAgZm9yKHZhciBkYXk9MTtkYXk8PWRheXM7ZGF5KyspewogICAgdmFyIGVsPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO2VsLmNsYXNzTmFtZT0nY2QnOwogICAgdmFyIGRhdGU9bmV3IERhdGUoY1ksY00sZGF5KTsKICAgIHZhciBpc1Bhc3Q9ZGF0ZTxuZXcgRGF0ZSh0b2RheS5nZXRGdWxsWWVhcigpLHRvZGF5LmdldE1vbnRoKCksdG9kYXkuZ2V0RGF0ZSgpKTsKICAgIHZhciBpbm5lcj1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdkaXYnKTtpbm5lci5jbGFzc05hbWU9J2NkLWlubmVyJztpbm5lci50ZXh0Q29udGVudD1kYXk7ZWwuYXBwZW5kQ2hpbGQoaW5uZXIpOwogICAgaWYoaXNQYXN0KXtlbC5jbGFzc0xpc3QuYWRkKCdkaXMnKTt9CiAgICBlbHNlewogICAgICBpZihkYXRlLnRvRGF0ZVN0cmluZygpPT09dG9kYXkudG9EYXRlU3RyaW5nKCkpZWwuY2xhc3NMaXN0LmFkZCgndG9kJyk7CiAgICAgIChmdW5jdGlvbihkLCBlbFJlZil7CiAgICAgICAgZWxSZWYub25jbGljaz1mdW5jdGlvbigpewogICAgICAgICAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmNkJykuZm9yRWFjaChmdW5jdGlvbihjKXtjLmNsYXNzTGlzdC5yZW1vdmUoJ3NlbCcpO30pOwogICAgICAgICAgZWxSZWYuY2xhc3NMaXN0LmFkZCgnc2VsJyk7CiAgICAgICAgICBib29raW5nLmRhdGU9U3RyaW5nKGQpLnBhZFN0YXJ0KDIsJzAnKSsnLicrU3RyaW5nKGNNKzEpLnBhZFN0YXJ0KDIsJzAnKSsnLicrY1k7CiAgICAgICAgICBzaG93VGltZXMoKTsKICAgICAgICB9OwogICAgICB9KShkYXksIGVsKTsKICAgIH0KICAgIGcuYXBwZW5kQ2hpbGQoZWwpOwogIH0KICAvLyBmaWxsIHRyYWlsaW5nIGNlbGxzIHRvIGNvbXBsZXRlIGxhc3QgZ3JpZCByb3cKICB2YXIgdG90YWwgPSBzdGFydCArIGRheXM7CiAgdmFyIHRyYWlsID0gKDcgLSAodG90YWwgJSA3KSkgJSA3OwogIGZvcih2YXIgdD0wO3Q8dHJhaWw7dCsrKXt2YXIgZXA9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7ZXAuY2xhc3NOYW1lPSdjZCBwYWQnO2cuYXBwZW5kQ2hpbGQoZXApO30KfQoKZnVuY3Rpb24gc2hvd1RpbWVzKCl7CiAgdmFyIHRnPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCd0aW1lRycpOwogIHRnLmlubmVySFRNTD0nPGRpdiBjbGFzcz0ibG9hZGluZy1zbG90cyI+4o+zINCX0LDQs9GA0YPQttCw0LXQvCDRgNCw0YHQv9C40YHQsNC90LjQtS4uLjwvZGl2Pic7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3RpbWVTZWMnKS5zdHlsZS5kaXNwbGF5PSdibG9jayc7CgogIHZhciB1cmwgPSB3aW5kb3cubG9jYXRpb24ub3JpZ2luICsgIi9hcGkvc2xvdHMiICsgJz9hY3Rpb249c2xvdHMmZGF0ZT0nICsgZW5jb2RlVVJJQ29tcG9uZW50KGJvb2tpbmcuZGF0ZSkgKyAnJm1hc3Rlcj0nICsgZW5jb2RlVVJJQ29tcG9uZW50KGJvb2tpbmcubWFzdGVyKTsKCiAgZmV0Y2godXJsKQogICAgLnRoZW4oZnVuY3Rpb24ocil7cmV0dXJuIHIuanNvbigpO30pCiAgICAudGhlbihmdW5jdGlvbihkYXRhKXsKICAgICAgdmFyIHNsb3RzID0gKGRhdGEuc2xvdHMgJiYgZGF0YS5zbG90cy5sZW5ndGggPiAwKSA/IGRhdGEuc2xvdHMgOiBbXTsKICAgICAgcmVuZGVyVGltZVNsb3RzKHNsb3RzKTsKICAgIH0pCiAgICAuY2F0Y2goZnVuY3Rpb24oKXsKICAgICAgcmVuZGVyVGltZVNsb3RzKFtdKTsKICAgIH0pOwp9CgpmdW5jdGlvbiByZW5kZXJUaW1lU2xvdHMoc2xvdHMpewogIHZhciB0Zz1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgndGltZUcnKTt0Zy5pbm5lckhUTUw9Jyc7CiAgaWYoc2xvdHMubGVuZ3RoPT09MCl7CiAgICB0Zy5pbm5lckhUTUw9JzxkaXYgY2xhc3M9ImxvYWRpbmctc2xvdHMiPtCd0LXRgiDQtNC+0YHRgtGD0L/QvdGL0YUg0YHQu9C+0YLQvtCyINC90LAg0Y3RgtGDINC00LDRgtGDPC9kaXY+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyIiBvbmNsaWNrPSJzaG93U2NyZWVuKFwnaG9tZVNjcmVlblwnKSIgc3R5bGU9Im1hcmdpbi10b3A6OHB4OyI+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLWljb24iPvCfkL48L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItdGV4dCI+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLXRpdGxlIj7QndC1INC90LDRiNC70Lgg0L/QvtC00YXQvtC00Y/RidC10LUg0LLRgNC10LzRjz88L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItc3ViIj7QodCy0Y/QttC40YLQtdGB0Ywg0YEg0L3QsNC80Lgg0LvRjtCx0YvQvCDRg9C00L7QsdC90YvQvCDRgdC/0L7RgdC+0LHQvtC8IOKAlCDQvNGLINC/0L7QtNCx0LXRgNGR0Lwg0YPQtNC+0LHQvdC+0LUg0LLRgNC10LzRjzwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci1hcnJvdyI+4oaSPC9kaXY+PC9kaXY+JzsKICAgIHJldHVybjsKICB9CiAgc2xvdHMuZm9yRWFjaChmdW5jdGlvbih0KXsKICAgIHZhciBidG49ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnYnV0dG9uJyk7YnRuLmNsYXNzTmFtZT0ndGJ0bic7YnRuLnRleHRDb250ZW50PXQ7CiAgICBidG4ub25jbGljaz1mdW5jdGlvbigpewogICAgICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcudGJ0bicpLmZvckVhY2goZnVuY3Rpb24oYil7Yi5jbGFzc0xpc3QucmVtb3ZlKCdhY3RpdmUnKTt9KTsKICAgICAgYnRuLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpO2Jvb2tpbmcudGltZT10OwogICAgICBzZXRUaW1lb3V0KGZ1bmN0aW9uKCl7Z29TdGVwKDUpO2J1aWxkU3VtKCk7fSwzMDApOwogICAgfTsKICAgIHRnLmFwcGVuZENoaWxkKGJ0bik7CiAgfSk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3RpbWVTZWMnKS5zY3JvbGxJbnRvVmlldyh7YmVoYXZpb3I6J3Ntb290aCcsYmxvY2s6J25lYXJlc3QnfSk7Cn0KCmZ1bmN0aW9uIGJ1aWxkU3VtKCl7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N1bUJsb2NrJykuaW5uZXJIVE1MPQogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fYnJlZWQrJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrKGJvb2tpbmcuYnJlZWREaXNwbGF5fHxib29raW5nLmJyZWVkKSsnPC9zcGFuPjwvZGl2PicrCiAgICAnPGRpdiBjbGFzcz0ic3IiPjxzcGFuIGNsYXNzPSJzbCI+JytUW0xBTkddLnN1bV9zZXJ2aWNlKyc8L3NwYW4+PHNwYW4gY2xhc3M9InN2Ij4nKygoTEFORyE9PSdydScmJlNWQ19UUkFOU0xBVElPTlNbYm9va2luZy5zZXJ2aWNlXSk/U1ZDX1RSQU5TTEFUSU9OU1tib29raW5nLnNlcnZpY2VdW0xBTkddOmJvb2tpbmcuc2VydmljZSkrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fbWFzdGVyKyc8L3NwYW4+PHNwYW4gY2xhc3M9InN2Ij4nK2Jvb2tpbmcubWFzdGVyKyc8L3NwYW4+PC9kaXY+JysKICAgICc8ZGl2IGNsYXNzPSJzciI+PHNwYW4gY2xhc3M9InNsIj4nK1RbTEFOR10uc3VtX2dyb29tKyc8L3NwYW4+PHNwYW4gY2xhc3M9InN2Ij4nK2Jvb2tpbmcuZ3Jvb21IaXN0b3J5Kyc8L3NwYW4+PC9kaXY+JysKICAgICc8ZGl2IGNsYXNzPSJzciI+PHNwYW4gY2xhc3M9InNsIj4nK1RbTEFOR10uc3VtX2RhdGUrJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrYm9va2luZy5kYXRlKyc8L3NwYW4+PC9kaXY+JysKICAgICc8ZGl2IGNsYXNzPSJzciI+PHNwYW4gY2xhc3M9InNsIj4nK1RbTEFOR10uc3VtX3RpbWUrJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrYm9va2luZy50aW1lKyc8L3NwYW4+PC9kaXY+JysKICAgICc8ZGl2IGNsYXNzPSJzciI+PHNwYW4gY2xhc3M9InNsIj4nK1RbTEFOR10uc3VtX3ByaWNlKyc8L3NwYW4+PHNwYW4gY2xhc3M9InNwIj4nK2Jvb2tpbmcucHJpY2UrJyDigqw8L3NwYW4+PC9kaXY+JzsKfQoKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NvbmZpcm1CdG4nKS5vbmNsaWNrID0gZnVuY3Rpb24oKXsKICB2YXIgbmFtZT1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY05hbWUnKS52YWx1ZTsKICB2YXIgcGhvbmU9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NQaG9uZScpLnZhbHVlOwogIGlmKCFuYW1lfHwhcGhvbmUpe2FsZXJ0KFRbTEFOR10uYWxlcnRfZmlsbCk7cmV0dXJuO30KICBpZighL15cK1xkezEwLH0kLy50ZXN0KHBob25lLnRyaW0oKSkpe2FsZXJ0KFRbTEFOR10uYWxlcnRfcGhvbmUpO3JldHVybjt9CiAgYm9va2luZy5uYW1lPW5hbWU7IGJvb2tpbmcucGhvbmU9cGhvbmU7IGJvb2tpbmcuZW1haWw9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NFbWFpbCcpLnZhbHVlOyBib29raW5nLnBldD1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY1BldCcpLnZhbHVlOyBib29raW5nLmxhbmc9TEFORzsKICBib29raW5nLmR1cmF0aW9uID0gYm9va2luZy5icmVlZCA9PT0gJ9Cp0LXQvdC60LgnID8gNjAgOiAoYm9va2luZy5icmVlZCAmJiBib29raW5nLmJyZWVkLmluZGV4T2YoJ9Ca0L7RiNC60LAnKSA9PT0gMCA/IDEyMCA6IDE4MCk7CiAgdmFyIGJ0bj1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY29uZmlybUJ0bicpOwogIGJ0bi50ZXh0Q29udGVudD1UW0xBTkddLnNlbmRpbmc7IGJ0bi5kaXNhYmxlZD10cnVlOwogIGZldGNoKFJBSUxXQVksIHsKICAgIG1ldGhvZDonUE9TVCcsCiAgICBoZWFkZXJzOnsnQ29udGVudC1UeXBlJzonYXBwbGljYXRpb24vanNvbid9LAogICAgYm9keTpKU09OLnN0cmluZ2lmeShib29raW5nKQogIH0pLnRoZW4oZnVuY3Rpb24oKXtzaG93U3VjY2VzcygpO30pLmNhdGNoKGZ1bmN0aW9uKCl7c2hvd1N1Y2Nlc3MoKTt9KTsKfTsKCmZ1bmN0aW9uIHNob3dTdWNjZXNzKCl7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2JrNScpLmNsYXNzTmFtZT0nc3RlcCc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N1Y0Jsb2NrJykuY2xhc3NMaXN0LmFkZCgnc2hvdycpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdwcm9ncmVzcycpLnN0eWxlLmRpc3BsYXk9J25vbmUnOwp9CgpmdW5jdGlvbiByZXNldEFsbCgpewogIGJvb2tpbmc9e2JyZWVkOicnLGJyZWVkRGlzcGxheTonJyxzZXJ2aWNlOicnLHByaWNlOjAsbWFzdGVyOicnLGdyb29tSGlzdG9yeTonJyxkYXRlOicnLHRpbWU6JycsbGFuZzoncnUnfTsKICBzZWxCcmVlZD1udWxsOyBpbnAudmFsdWU9Jyc7IGNsci5jbGFzc0xpc3QucmVtb3ZlKCdzaG93Jyk7CiAgYmFkZ2UuY2xhc3NMaXN0LnJlbW92ZSgnc2hvdycpOyBiYWRnZS5pbm5lckhUTUw9Jyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y1NlYycpLnN0eWxlLmRpc3BsYXk9J25vbmUnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCd0aW1lU2VjJykuc3R5bGUuZGlzcGxheT0nbm9uZSc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N1Y0Jsb2NrJykuY2xhc3NMaXN0LnJlbW92ZSgnc2hvdycpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdwcm9ncmVzcycpLnN0eWxlLmRpc3BsYXk9J2ZsZXgnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjTmFtZScpLnZhbHVlPScnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjUGhvbmUnKS52YWx1ZT0nJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY0VtYWlsJykudmFsdWU9Jyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NQZXQnKS52YWx1ZT0nJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY29uZmlybUJ0bicpLnRleHRDb250ZW50PVRbTEFOR10uY29uZmlybV9idG47CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NvbmZpcm1CdG4nKS5kaXNhYmxlZD1mYWxzZTsKICBnb1N0ZXAoMSk7Cn0KCnZhciBMQU5HID0gbG9jYWxTdG9yYWdlLmdldEl0ZW0oJ3JqbGFuZycpIHx8ICdydSc7CnZhciBUID0gewogIHJ1OnsKICAgIGxvZ29fdGFnOifQn9GA0LXQvNC40LDQu9GM0L3Ri9C5INCz0YDRg9C80LjQvdCzLTxicj7RgdCw0LvQvtC9INCyINCi0LDQu9C70LjQvdC1JywKICAgIGNob29zZV9ob3c6J0Nob29zZSBob3cgdG8gY29ubmVjdCcsCiAgICBib29rX29ubGluZTon0J7QvdC70LDQudC9INCx0YDQvtC90LjRgNC+0LLQsNC90LjQtScsCiAgICBib29rX2Zsb3c6J9Cf0L7RgNC+0LTQsCDihpIg0KPRgdC70YPQs9CwIOKGkiDQnNCw0YHRgtC10YAg4oaSINCS0YDQtdC80Y8nLAogICAgb3JfY29udGFjdDon0LjQu9C4INGB0LLRj9C20LjRgtC10YHRjCDRgSDQvdCw0LzQuCcsCiAgICBjYWxsX3VzOidDYWxsIFVzJywKICAgIGJhY2s6J+KGkCDQndCw0LfQsNC0JywKICAgIGxvZ29fc3ViOidHcm9vbWluZyDCtyDQotCw0LvQu9C40L0nLAogICAgcHNfc2VydmljZTon0KPRgdC70YPQs9CwJyxwc19tYXN0ZXI6J9Cc0LDRgdGC0LXRgCcscHNfcGV0OifQn9C40YLQvtC80LXRhicscHNfZGF0ZTon0JTQsNGC0LAnLHBzX2RldGFpbHM6J9CU0LDQvdC90YvQtScsCiAgICBzdGVwMV9sYmw6JzAxIMK3INCf0L7RgNC+0LTQsCcsCiAgICBicmVlZF9waDon0J3QsNGH0L3QuNGC0LUg0LLQstC+0LTQuNGC0Ywg0L/QvtGA0L7QtNGDLi4uJywKICAgIHN0ZXAyX2xibDonMDIgwrcg0KPRgdC70YPQs9CwJywKICAgIHN0ZXAyX21hc3Rlcjon0JLRi9Cx0LXRgNC40YLQtSDQvNCw0YHRgtC10YDQsCcsCiAgICBzdGVwM19sYmw6J9Ca0LDQuiDQtNCw0LLQvdC+INCy0Ysg0L/QvtGB0LXRidCw0LvQuCDQs9GA0YPQvNC40L3Qsz8nLAogICAgZzE6J9Cf0LXRgNCy0YvQuSDRgNCw0LcnLGcyOifQntGCIDEg0LTQviAzINC80LXRgdGP0YbQtdCyJyxnMzon0J7RgiAzINC00L4gNiDQvNC10YHRj9GG0LXQsicsZzQ6J9CR0L7Qu9C10LUgNiDQvNC10YHRj9GG0LXQsicsCiAgICBzdGVwNF9sYmw6J9CS0YvQsdC10YDQuNGC0LUg0LTQsNGC0YMnLAogICAgY2FsX2F2YWlsOifQldGB0YLRjCDRgdCy0L7QsdC+0LTQvdC+0LUg0LLRgNC10LzRjycsY2FsX25vbmU6J9Ch0LLQvtCx0L7QtNC90L7Qs9C+INCy0YDQtdC80LXQvdC4INC90LXRgicsCiAgICBzdGVwNF90aW1lOifQktGL0LHQtdGA0LjRgtC1INCy0YDQtdC80Y8nLAogICAgc3RlcDVfbGJsOifQktCw0YjQuCDQtNCw0L3QvdGL0LUnLAogICAgbGJsX25hbWU6J9CY0LzRjycscGhfbmFtZTon0JLQsNGI0LUg0LjQvNGPJywKICAgIGxibF9waG9uZTon0KLQtdC70LXRhNC+0L0nLGxibF9lbWFpbDonRW1haWwnLAogICAgbGJsX3BldDon0JrQu9C40YfQutCwINC/0LjRgtC+0LzRhtCwJyxwaF9vcHRpb25hbDon0J3QtdC+0LHRj9C30LDRgtC10LvRjNC90L4nLAogICAgY29uZmlybV9idG46J9Cf0L7QtNGC0LLQtdGA0LTQuNGC0Ywg0LfQsNC/0LjRgdGMJywKICAgIHN1Y2Nlc3NfdGl0bGU6J9CX0LDQv9C40YHRjCDQv9GA0LjQvdGP0YLQsCEnLAogICAgc3VjY2Vzc19zdWI6J9Cc0Ysg0YHQstGP0LbQtdC80YHRjyDRgSDQstCw0LzQuCDQtNC70Y8g0L/QvtC00YLQstC10YDQttC00LXQvdC40Y8uPGJyPtCh0L/QsNGB0LjQsdC+LCDRh9GC0L4g0LLRi9Cx0YDQsNC70LggUiZhbXA7SiBHcm9vbWluZyEnLAogICAgdG9faG9tZTon4oaQINCd0LAg0LPQu9Cw0LLQvdGD0Y4nLAogICAgYWxlcnRfZmlsbDon0JLQstC10LTQuNGC0LUg0LjQvNGPINC4INGC0LXQu9C10YTQvtC9JyxhbGVydF9waG9uZTon0JLQstC10LTQuNGC0LUg0L3QvtC80LXRgCDQsiDRhNC+0YDQvNCw0YLQtSArMzcyMTIzNDU2NzgnLAogICAgc2VuZGluZzon0J7RgtC/0YDQsNCy0LvRj9C10LwuLi4nLAogICAgc3VtX2JyZWVkOifQn9C+0YDQvtC00LAnLHN1bV9zZXJ2aWNlOifQo9GB0LvRg9Cz0LAnLHN1bV9tYXN0ZXI6J9Cc0LDRgdGC0LXRgCcsc3VtX2dyb29tOifQn9C+0YHQu9C10LTQvdC40Lkg0LPRgNGD0LwnLHN1bV9kYXRlOifQlNCw0YLQsCcsc3VtX3RpbWU6J9CS0YDQtdC80Y8nLHN1bV9wcmljZTon0KHRgtC+0LjQvNC+0YHRgtGMJywKICAgIG1vbnRoczpbJ9Cv0L3QstCw0YDRjCcsJ9Ck0LXQstGA0LDQu9GMJywn0JzQsNGA0YInLCfQkNC/0YDQtdC70YwnLCfQnNCw0LknLCfQmNGO0L3RjCcsJ9CY0Y7Qu9GMJywn0JDQstCz0YPRgdGCJywn0KHQtdC90YLRj9Cx0YDRjCcsJ9Ce0LrRgtGP0LHRgNGMJywn0J3QvtGP0LHRgNGMJywn0JTQtdC60LDQsdGA0YwnXQogIH0sCiAgZW46ewogICAgbG9nb190YWc6J1ByZW1pdW0gZ3Jvb21pbmc8YnI+c2Fsb24gaW4gVGFsbGlubicsCiAgICBjaG9vc2VfaG93OidDaG9vc2UgaG93IHRvIGNvbm5lY3QnLAogICAgYm9va19vbmxpbmU6J0Jvb2sgT25saW5lJywKICAgIGJvb2tfZmxvdzonQnJlZWQg4oaSIFNlcnZpY2Ug4oaSIE1hc3RlciDihpIgVGltZScsCiAgICBvcl9jb250YWN0OidvciBjb250YWN0IHVzJywKICAgIGNhbGxfdXM6J0NhbGwgVXMnLAogICAgYmFjazon4oaQIEJhY2snLAogICAgbG9nb19zdWI6J0dyb29taW5nIMK3IFRhbGxpbm4nLAogICAgcHNfc2VydmljZTonU2VydmljZScscHNfbWFzdGVyOidNYXN0ZXInLHBzX3BldDonUGV0Jyxwc19kYXRlOidEYXRlJyxwc19kZXRhaWxzOidEZXRhaWxzJywKICAgIHN0ZXAxX2xibDonMDEgwrcgRG9nIGJyZWVkJywKICAgIGJyZWVkX3BoOidTdGFydCB0eXBpbmcgYnJlZWQuLi4nLAogICAgc3RlcDJfbGJsOicwMiDCtyBTZXJ2aWNlJywKICAgIHN0ZXAyX21hc3RlcjonQ2hvb3NlIG1hc3RlcicsCiAgICBzdGVwM19sYmw6J0hvdyBsb25nIGFnbyB3YXMgeW91ciBsYXN0IGdyb29taW5nPycsCiAgICBnMTonRmlyc3QgdGltZScsZzI6JzHigJMzIG1vbnRocyBhZ28nLGczOicz4oCTNiBtb250aHMgYWdvJyxnNDonT3ZlciA2IG1vbnRocycsCiAgICBzdGVwNF9sYmw6J0Nob29zZSBkYXRlJywKICAgIGNhbF9hdmFpbDonQXZhaWxhYmxlJyxjYWxfbm9uZTonTm90IGF2YWlsYWJsZScsCiAgICBzdGVwNF90aW1lOidDaG9vc2UgdGltZScsCiAgICBzdGVwNV9sYmw6J1lvdXIgZGV0YWlscycsCiAgICBsYmxfbmFtZTonTmFtZScscGhfbmFtZTonWW91ciBuYW1lJywKICAgIGxibF9waG9uZTonUGhvbmUnLGxibF9lbWFpbDonRW1haWwnLAogICAgbGJsX3BldDoiUGV0J3MgbmFtZSIscGhfb3B0aW9uYWw6J09wdGlvbmFsJywKICAgIGNvbmZpcm1fYnRuOidDb25maXJtIGJvb2tpbmcnLAogICAgc3VjY2Vzc190aXRsZTonQm9va2luZyBjb25maXJtZWQhJywKICAgIHN1Y2Nlc3Nfc3ViOidXZSB3aWxsIGNvbnRhY3QgeW91IHRvIGNvbmZpcm0uPGJyPlRoYW5rIHlvdSBmb3IgY2hvb3NpbmcgUiZhbXA7SiBHcm9vbWluZyEnLAogICAgdG9faG9tZTon4oaQIEhvbWUnLAogICAgYWxlcnRfZmlsbDonUGxlYXNlIGVudGVyIG5hbWUgYW5kIHBob25lJyxhbGVydF9waG9uZTonRW50ZXIgcGhvbmUgbnVtYmVyIGluIGZvcm1hdCArMzcyMTIzNDU2NzgnLAogICAgc2VuZGluZzonU2VuZGluZy4uLicsCiAgICBzdW1fYnJlZWQ6J0JyZWVkJyxzdW1fc2VydmljZTonU2VydmljZScsc3VtX21hc3RlcjonTWFzdGVyJyxzdW1fZ3Jvb206J0xhc3QgZ3Jvb21pbmcnLHN1bV9kYXRlOidEYXRlJyxzdW1fdGltZTonVGltZScsc3VtX3ByaWNlOidQcmljZScsCiAgICBtb250aHM6WydKYW51YXJ5JywnRmVicnVhcnknLCdNYXJjaCcsJ0FwcmlsJywnTWF5JywnSnVuZScsJ0p1bHknLCdBdWd1c3QnLCdTZXB0ZW1iZXInLCdPY3RvYmVyJywnTm92ZW1iZXInLCdEZWNlbWJlciddCiAgfSwKICBldDp7CiAgICBsb2dvX3RhZzonRXNtYWtsYXNzaWxpbmUgaG9vbGR1c3RlZW51czxicj5UYWxsaW5uYXMnLAogICAgY2hvb3NlX2hvdzonVmFsaSDDvGhlbmR1c3ZpaXMnLAogICAgYm9va19vbmxpbmU6J0Jyb25lZXJpIHZlZWJpcycsCiAgICBib29rX2Zsb3c6J1TDtXVnIOKGkiBUZWVudXMg4oaSIE1laXN0ZXIg4oaSIEFlZycsCiAgICBvcl9jb250YWN0Oid2w7VpIHbDtXRhIMO8aGVuZHVzdCcsCiAgICBjYWxsX3VzOidIZWxpc3RhIG1laWxlJywKICAgIGJhY2s6J+KGkCBUYWdhc2knLAogICAgbG9nb19zdWI6J0dyb29taW5nIMK3IFRhbGxpbm4nLAogICAgcHNfc2VydmljZTonVGVlbnVzJyxwc19tYXN0ZXI6J01laXN0ZXInLHBzX3BldDonTGVtbWlrbG9vbScscHNfZGF0ZTonS3V1cMOkZXYnLHBzX2RldGFpbHM6J0FuZG1lZCcsCiAgICBzdGVwMV9sYmw6JzAxIMK3IEtvZXJhIHTDtXVnJywKICAgIGJyZWVkX3BoOidBbHVzdGFnZSB0w7V1IHNpc2VzdGFtaXN0Li4uJywKICAgIHN0ZXAyX2xibDonMDIgwrcgVGVlbnVzJywKICAgIHN0ZXAyX21hc3RlcjonVmFsaSBtZWlzdGVyJywKICAgIHN0ZXAzX2xibDonTWlsbGFsIGvDpGlzaXRlIHZpaW1hdGkgZ3Jvb21pbmd1cz8nLAogICAgZzE6J0VzaW1lc3Qga29yZGEnLGcyOicx4oCTMyBrdXVkIHRhZ2FzaScsZzM6JzPigJM2IGt1dWQgdGFnYXNpJyxnNDonw5xsZSA2IGt1dScsCiAgICBzdGVwNF9sYmw6J1ZhbGkga3V1cMOkZXYnLAogICAgY2FsX2F2YWlsOidWYWJ1IGFlZ3Ugb24nLGNhbF9ub25lOidWYWJ1IGFlZ3UgcG9sZScsCiAgICBzdGVwNF90aW1lOidWYWxpIGtlbGxhYWVnJywKICAgIHN0ZXA1X2xibDonVGVpZSBhbmRtZWQnLAogICAgbGJsX25hbWU6J05pbWknLHBoX25hbWU6J1RlaWUgbmltaScsCiAgICBsYmxfcGhvbmU6J1RlbGVmb24nLGxibF9lbWFpbDonRW1haWwnLAogICAgbGJsX3BldDonTGVtbWlrbG9vbWEgbmltaScscGhfb3B0aW9uYWw6J1ZhbGlrdWxpbmUnLAogICAgY29uZmlybV9idG46J0tpbm5pdGEgYnJvbmVlcmluZycsCiAgICBzdWNjZXNzX3RpdGxlOidCcm9uZWVyaW5nIGtpbm5pdGF0dWQhJywKICAgIHN1Y2Nlc3Nfc3ViOidWw7V0YW1lIHRlaWVnYSDDvGhlbmR1c3Qga2lubml0YW1pc2Vrcy48YnI+VMOkbmFtZSwgZXQgdmFsaXNpdGUgUiZhbXA7SiBHcm9vbWluZyEnLAogICAgdG9faG9tZTon4oaQIEF2YWxlaGVsZScsCiAgICBhbGVydF9maWxsOidQYWx1biBzaXNlc3RhZ2UgbmltaSBqYSB0ZWxlZm9uJyxhbGVydF9waG9uZTonU2lzZXN0YWdlIHRlbGVmb25pbnVtYmVyIHZvcm1pbmd1cyArMzcyMTIzNDU2NzgnLAogICAgc2VuZGluZzonU2FhZGFuLi4uJywKICAgIHN1bV9icmVlZDonVMO1dWcnLHN1bV9zZXJ2aWNlOidUZWVudXMnLHN1bV9tYXN0ZXI6J01laXN0ZXInLHN1bV9ncm9vbTonVmlpbWFuZSBncm9vbWluZycsc3VtX2RhdGU6J0t1dXDDpGV2JyxzdW1fdGltZTonS2VsbGFhZWcnLHN1bV9wcmljZTonSGluZCcsCiAgICBtb250aHM6WydKYWFudWFyJywnVmVlYnJ1YXInLCdNw6RydHMnLCdBcHJpbGwnLCdNYWknLCdKdXVuaScsJ0p1dWxpJywnQXVndXN0JywnU2VwdGVtYmVyJywnT2t0b29iZXInLCdOb3ZlbWJlcicsJ0RldHNlbWJlciddCiAgfQp9OwoKZnVuY3Rpb24gc2V0TGFuZyhsKXsKICBMQU5HPWw7CiAgbG9jYWxTdG9yYWdlLnNldEl0ZW0oJ3JqbGFuZycsbCk7CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmxhbmctYnRuJykuZm9yRWFjaChmdW5jdGlvbihiKXsKICAgIGIuY2xhc3NMaXN0LnRvZ2dsZSgnYWN0aXZlJywgYi50ZXh0Q29udGVudC50b0xvd2VyQ2FzZSgpPT09bCk7CiAgfSk7CiAgdmFyIHRyPVRbbF07CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnW2RhdGEtaTE4bl0nKS5mb3JFYWNoKGZ1bmN0aW9uKGVsKXsKICAgIHZhciBrPWVsLmdldEF0dHJpYnV0ZSgnZGF0YS1pMThuJyk7CiAgICBpZih0cltrXSE9PXVuZGVmaW5lZCkgZWwuaW5uZXJIVE1MPXRyW2tdOwogIH0pOwogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tkYXRhLWkxOG4tcGhdJykuZm9yRWFjaChmdW5jdGlvbihlbCl7CiAgICB2YXIgaz1lbC5nZXRBdHRyaWJ1dGUoJ2RhdGEtaTE4bi1waCcpOwogICAgaWYodHJba10hPT11bmRlZmluZWQpIGVsLnBsYWNlaG9sZGVyPXRyW2tdOwogIH0pOwogIE1PTlRIUz10ci5tb250aHM7CiAgYnVpbGRDYWwoKTsKICAvLyBSZS1yZW5kZXIgYmFkZ2UgYW5kIHNlcnZpY2VzIGlmIGJyZWVkIGFscmVhZHkgc2VsZWN0ZWQKICBpZihzZWxCcmVlZCl7CiAgICB2YXIgYmY9bD09PSdlbic/J2JyZWVkX2VuJzpsPT09J2V0Jz8nYnJlZWRfZXQnOidicmVlZCc7CiAgICB2YXIgZGI9c2VsQnJlZWRbYmZdfHxzZWxCcmVlZC5icmVlZDsKICAgIGJvb2tpbmcuYnJlZWREaXNwbGF5PWRiOwogICAgdmFyIGJuRWw9ZG9jdW1lbnQucXVlcnlTZWxlY3RvcignI3NCYWRnZSAuYm5hbWUnKTsKICAgIGlmKGJuRWwpIGJuRWwudGV4dENvbnRlbnQ9ZGI7CiAgICB2YXIgYmNFbD1kb2N1bWVudC5xdWVyeVNlbGVjdG9yKCcjc0JhZGdlIC5iY2hnJyk7CiAgICBpZihiY0VsKSBiY0VsLnRleHRDb250ZW50PWw9PT0nZW4nPydDaGFuZ2UnOmw9PT0nZXQnPydNdXVkYSc6J9CY0LfQvNC10L3QuNGC0YwnOwogICAgaWYoZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y1NlYycpLnN0eWxlLmRpc3BsYXkhPT0nbm9uZScpIHJlbmRlclN2Y3Moc2VsQnJlZWQpOwogICAgdmFyIHNuPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdmNOb3RlJyk7CiAgICBpZihzbil7CiAgICAgIHZhciBudD1sPT09J2VuJz8nUGxlYXNlIG5vdGUnOmw9PT0nZXQnPydQYW5nZSB0w6RoZWxlJzon0JLQsNC20L3QviDQt9C90LDRgtGMJzsKICAgICAgdmFyIG5iPWw9PT0nZW4nPydGaW5hbCBwcmljZSBkZXBlbmRzIG9uIGNvYXQgY29uZGl0aW9uIGFuZCBwZXQgYmVoYXZpb3VyLjxicj5EZW1hdHRpbmcgZnJvbSA1IOKCrC48YnI+QWdncmVzc2l2ZSBiZWhhdmlvdXIgc3VyY2hhcmdlIG1heSBhcHBseTogKzUwJS4nOmw9PT0nZXQnPydMw7VwbGlrIGhpbmQgc8O1bHR1YiBrYXJ2YXN0aWt1IHNlaXN1bmRpc3QgamEgbGVtbWlrbG9vbWEga8OkaXR1bWlzZXN0Ljxicj5Lb2x0c3VuaXRlIGxhaHRpaGFydXRhbWluZSBhbGF0ZXMgNSDigqwuPGJyPkFncmVzc2lpdnNlIGvDpGl0dW1pc2Uga29ycmFsIHbDtWliIGxpc2FuZHVkYSA1MCUganV1cmRlaGluZGx1cy4nOifQntC60L7QvdGH0LDRgtC10LvRjNC90LDRjyDRgdGC0L7QuNC80L7RgdGC0Ywg0LfQsNCy0LjRgdC40YIg0L7RgiDRgdC+0YHRgtC+0Y/QvdC40Y8g0YjQtdGA0YHRgtC4INC4INC/0L7QstC10LTQtdC90LjRjyDQv9C40YLQvtC80YbQsC48YnI+0KDQsNC30LHQvtGAINC60L7Qu9GC0YPQvdC+0LIg4oCUINC+0YIgNSDigqwuPGJyPtCf0YDQuCDQsNCz0YDQtdGB0YHQuNCy0L3QvtC8INC/0L7QstC10LTQtdC90LjQuCDQvNC+0LbQtdGCINC/0YDQuNC80LXQvdGP0YLRjNGB0Y8g0LTQvtC/0LvQsNGC0LAgNTAlLic7CiAgICAgIHNuLmlubmVySFRNTD0nPGRpdiBzdHlsZT0iZm9udC1zaXplOjAuODM4cmVtO2xldHRlci1zcGFjaW5nOi4xNWVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO21hcmdpbi1ib3R0b206OHB4O2ZvbnQtd2VpZ2h0OjYwMDtmb250LWZhbWlseTpcJ01vbnRzZXJyYXRcJyxzYW5zLXNlcmlmIj4nK250Kyc8L2Rpdj48ZGl2IHN0eWxlPSJmb250LXNpemU6MS4wMjVyZW07Y29sb3I6I2ZmZmZmZjtsaW5lLWhlaWdodDoxLjg7Zm9udC1mYW1pbHk6XCdNb250c2VycmF0XCcsc2Fucy1zZXJpZiI+JytuYisnPC9kaXY+JzsKICAgIH0KICB9Cn0KCi8vIEFwcGx5IHNhdmVkIGxhbmd1YWdlIG9uIGxvYWQKKGZ1bmN0aW9uKCl7IHNldExhbmcoTEFORyk7IH0pKCk7CgovLyBDYWxsYmFjayBmb3JtCmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYWxsYmFja0J0bicpLm9uY2xpY2sgPSBmdW5jdGlvbigpewogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtNb2RhbCcpLnN0eWxlLmRpc3BsYXkgPSAnZmxleCc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia05hbWUnKS52YWx1ZSA9ICcnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtQaG9uZScpLnZhbHVlID0gJyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1N1Y2Nlc3MnKS5zdHlsZS5kaXNwbGF5ID0gJ25vbmUnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtTdWJtaXQnKS5zdHlsZS5kaXNwbGF5ID0gJ2Jsb2NrJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrQ2xvc2UnKS50ZXh0Q29udGVudCA9ICfQntGC0LzQtdC90LAnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtTdWJtaXQnKS50ZXh0Q29udGVudCA9ICfQntGC0L/RgNCw0LLQuNGC0YwnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtTdWJtaXQnKS5kaXNhYmxlZCA9IGZhbHNlOwp9Owpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrQ2xvc2UnKS5vbmNsaWNrID0gZnVuY3Rpb24oKXsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrTW9kYWwnKS5zdHlsZS5kaXNwbGF5ID0gJ25vbmUnOwp9Owpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrU3VibWl0Jykub25jbGljayA9IGZ1bmN0aW9uKCl7CiAgdmFyIG5hbWUgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrTmFtZScpLnZhbHVlLnRyaW0oKTsKICB2YXIgcGhvbmUgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrUGhvbmUnKS52YWx1ZS50cmltKCkucmVwbGFjZSgvXEQvZywnJyk7CiAgaWYoIW5hbWUgfHwgIXBob25lKXthbGVydCgn0JLQstC10LTQuNGC0LUg0LjQvNGPINC4INGC0LXQu9C10YTQvtC9Jyk7cmV0dXJuO30KICB2YXIgYnRuID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1N1Ym1pdCcpOwogIGJ0bi50ZXh0Q29udGVudCA9ICfQntGC0L/RgNCw0LLQu9GP0LXQvC4uLic7IGJ0bi5kaXNhYmxlZCA9IHRydWU7CiAgZmV0Y2goJy9hcGkvY2FsbGJhY2snLHsKICAgIG1ldGhvZDonUE9TVCcsCiAgICBoZWFkZXJzOnsnQ29udGVudC1UeXBlJzonYXBwbGljYXRpb24vanNvbid9LAogICAgYm9keTpKU09OLnN0cmluZ2lmeSh7bmFtZTpuYW1lLCBwaG9uZTonKzM3MicrcGhvbmV9KQogIH0pLnRoZW4oZnVuY3Rpb24oKXsKICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtTdWNjZXNzJykuc3R5bGUuZGlzcGxheSA9ICdibG9jayc7CiAgICBidG4uc3R5bGUuZGlzcGxheSA9ICdub25lJzsKICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtDbG9zZScpLnRleHRDb250ZW50ID0gJ+KGkCDQl9Cw0LrRgNGL0YLRjCc7CiAgICBzZXRUaW1lb3V0KGZ1bmN0aW9uKCl7ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia01vZGFsJykuc3R5bGUuZGlzcGxheT0nbm9uZSc7fSwzMDAwKTsKICB9KS5jYXRjaChmdW5jdGlvbigpewogICAgYnRuLnRleHRDb250ZW50ID0gJ9Ce0YLQv9GA0LDQstC40YLRjCc7IGJ0bi5kaXNhYmxlZCA9IGZhbHNlOwogICAgYWxlcnQoJ9Ce0YjQuNCx0LrQsC4g0J/QvtC/0YDQvtCx0YPQudGC0LUg0LXRidGRINGA0LDQty4nKTsKICB9KTsKfTsKCjwvc2NyaXB0Pgo8L2JvZHk+CjwvaHRtbD4K"



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
