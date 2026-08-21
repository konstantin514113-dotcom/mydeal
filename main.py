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
BOOKING_HTML_B64 = "77u/PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9InJ1Ij4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idGhlbWUtY29sb3IiIGNvbnRlbnQ9IiMwYTBhMGEiPgo8bWV0YSBuYW1lPSJ2aWV3cG9ydCIgY29udGVudD0id2lkdGg9ZGV2aWNlLXdpZHRoLGluaXRpYWwtc2NhbGU9MSI+Cjx0aXRsZT5SJkogR3Jvb21pbmc8L3RpdGxlPgo8bGluayBocmVmPSJodHRwczovL2ZvbnRzLmdvb2dsZWFwaXMuY29tL2NzczI/ZmFtaWx5PUNvcm1vcmFudCtHYXJhbW9uZDp3Z2h0QDQwMDs2MDAmZmFtaWx5PVBsYXlmYWlyK0Rpc3BsYXk6aXRhbCx3Z2h0QDAsNDAwOzAsNjAwOzAsNzAwOzEsNDAwJmZhbWlseT1Nb250c2VycmF0OndnaHRAMzAwOzQwMDs1MDA7NjAwJmRpc3BsYXk9c3dhcCIgcmVsPSJzdHlsZXNoZWV0Ij4KPHN0eWxlPgoqe2JveC1zaXppbmc6Ym9yZGVyLWJveDttYXJnaW46MDtwYWRkaW5nOjB9Cmh0bWwsYm9keXttaW4taGVpZ2h0OjEwMHZoO2JhY2tncm91bmQ6IzBhMGEwYTtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXdlaWdodDo0MDB9Ci5zY3JlZW57ZGlzcGxheTpub25lO21pbi1oZWlnaHQ6MTAwdmg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2FsaWduLWl0ZW1zOmNlbnRlcjtwYWRkaW5nOjQ4cHggMCA2NHB4fQouc2NyZWVuLmFjdGl2ZXtkaXNwbGF5OmZsZXh9Ci5jb257d2lkdGg6MTAwJTttYXgtd2lkdGg6NDAwcHg7cGFkZGluZzowIDI4cHh9Ci5iYWNrLWJ0bntkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo4cHg7Y29sb3I6I2ZmZmZmZjtmb250LXNpemU6MC44cmVtO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyOm5vbmU7Y3Vyc29yOnBvaW50ZXI7cGFkZGluZzowO21hcmdpbi1ib3R0b206MzZweDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXdlaWdodDozMDA7dHJhbnNpdGlvbjpjb2xvciAuMnN9Ci5iYWNrLWJ0bjpob3Zlcntjb2xvcjojZmZmZmZmfQoubG9nby1yantmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZjtmb250LXNpemU6Mi41cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmfQoubG9nby1zdWJ7Zm9udC1zaXplOjAuNjYzcmVtO2ZvbnQtd2VpZ2h0OjYwMDtsZXR0ZXItc3BhY2luZzouNGVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO21hcmdpbi10b3A6M3B4O3BhZGRpbmctYm90dG9tOjE0cHg7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4yKTttYXJnaW4tYm90dG9tOjIwcHh9Ci5ob21lLXJqe2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmO2ZvbnQtc2l6ZTozLjI1cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjF9Ci5sb2dvLXRhZ3tmb250LXNpemU6MC43NXJlbTtsZXR0ZXItc3BhY2luZzouMTJlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjtsaW5lLWhlaWdodDoxLjV9Ci5sb2dvLXJvd3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6ZmxleC1lbmQ7Z2FwOjEycHg7bWFyZ2luLWJvdHRvbToyOHB4O3BhZGRpbmctYm90dG9tOjE4cHg7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4yKX0KLmxvZ28taW1nLXJvd3ttYXJnaW4tYm90dG9tOjI4cHg7cGFkZGluZy1ib3R0b206MThweDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjIpfQoubG9nby1pbWd7aGVpZ2h0OjkwcHg7d2lkdGg6YXV0bztkaXNwbGF5OmJsb2NrfQouaG9tZS1nc3Vie2ZvbnQtc2l6ZTowLjY2M3JlbTtmb250LXdlaWdodDo2MDA7bGV0dGVyLXNwYWNpbmc6LjRlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjttYXJnaW4tdG9wOjZweDttYXJnaW4tYm90dG9tOjIycHh9Ci5ob21lLWgxe2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6My4xMjVyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNmZmZmZmY7bGluZS1oZWlnaHQ6MS4xO21hcmdpbi1ib3R0b206NnB4fQouaG9tZS1oMSBlbXtmb250LXN0eWxlOml0YWxpYztjb2xvcjojZmZmZmZmfQouaG9tZS1zdWJ7Zm9udC1zaXplOjAuOHJlbTtsZXR0ZXItc3BhY2luZzouMThlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjI4cHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5vcHR7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTZweDtwYWRkaW5nOjE2cHggMDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNyk7dGV4dC1kZWNvcmF0aW9uOm5vbmU7Y29sb3I6I2ZmZmZmZjt0cmFuc2l0aW9uOmNvbG9yIC4ycztjdXJzb3I6cG9pbnRlcjtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyLXRvcDpub25lO2JvcmRlci1sZWZ0Om5vbmU7Ym9yZGVyLXJpZ2h0Om5vbmU7d2lkdGg6MTAwJTtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWZ9Ci5vcHQ6aG92ZXJ7Y29sb3I6I2ZmZn0KLm9wdC1pY29ue3dpZHRoOjM4cHg7aGVpZ2h0OjM4cHg7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2ZsZXgtc2hyaW5rOjB9Ci5vcHQtaWNvbi1pbWd7d2lkdGg6MzhweDtoZWlnaHQ6MzhweDtvYmplY3QtZml0OmNvbnRhaW59Ci5vcHQtdGV4dHtmbGV4OjE7dGV4dC1hbGlnbjpsZWZ0fQoub3B0LXRpdGxle2ZvbnQtc2l6ZToxLjUxMnJlbTtmb250LXdlaWdodDo2MDA7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjJweDt0cmFuc2l0aW9uOmNvbG9yIC4ycztmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWZ9Ci5vcHQ6aG92ZXIgLm9wdC10aXRsZXtjb2xvcjojZmZmfQoub3B0LWhhbmRsZXtmb250LXNpemU6MC44ODdyZW07Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXdlaWdodDozMDB9Ci5vcHQtdGl0bGUtYm9va3tmb250LXNpemU6MS4zOHJlbTt3aGl0ZS1zcGFjZTpub3dyYXB9Ci5vcHQtaGFuZGxlLWJvb2t7Zm9udC1zaXplOjAuNzhyZW19Ci5vcHQtYXJyb3d7Y29sb3I6I2ZmZmZmZjtmb250LXNpemU6MS4yMjVyZW07ZmxleC1zaHJpbms6MDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLm9wdDpob3ZlciAub3B0LWFycm93e2NvbG9yOiNmZmZmZmZ9Ci5kaXZpZGVye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7cGFkZGluZzoxMnB4IDB9Ci5kaXZpZGVyOjpiZWZvcmUsLmRpdmlkZXI6OmFmdGVye2NvbnRlbnQ6Jyc7ZmxleDoxO2hlaWdodDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNil9Ci5kaXZpZGVyIHNwYW57Zm9udC1zaXplOjAuNjg4cmVtO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQouaG9tZS1mb290e21hcmdpbi10b3A6MzZweDtwYWRkaW5nLXRvcDoyMHB4O2JvcmRlci10b3A6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA2KTtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6Y2VudGVyfQouaG9tZS1mb290IHNwYW57Zm9udC1zaXplOjAuNzc1cmVtO2xldHRlci1zcGFjaW5nOi4yZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5mZG90e3dpZHRoOjJweDtoZWlnaHQ6MnB4O2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMTYpfQoucHJvZ3Jlc3N7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjttYXJnaW4tYm90dG9tOjQwcHg7b3ZlcmZsb3c6aGlkZGVuO2NvdW50ZXItcmVzZXQ6c3RlcH0KLnBze2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjVweDtmb250LXNpemU6MC42NjNyZW07bGV0dGVyLXNwYWNpbmc6LjEyZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7d2hpdGUtc3BhY2U6bm93cmFwO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2NvdW50ZXItaW5jcmVtZW50OnN0ZXB9Ci5wcy5kb25le2NvbG9yOiNmZmZmZmZ9Ci5wcy5hY3RpdmV7Y29sb3I6I2ZmZmZmZn0KLnBkb3R7d2lkdGg6MThweDtoZWlnaHQ6MThweDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7ZmxleC1zaHJpbms6MDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEyKTtmb250LXNpemU6MC42NjNyZW07Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC13ZWlnaHQ6NjAwfQoucGRvdDo6YmVmb3Jle2NvbnRlbnQ6Y291bnRlcihzdGVwLGRlY2ltYWwtbGVhZGluZy16ZXJvKX0KLnBzLmRvbmUgLnBkb3R7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLnBzLmFjdGl2ZSAucGRvdHtib3JkZXItY29sb3I6I2ZmZmZmZjtjb2xvcjojZmZmZmZmfQoucGx7ZmxleDoxO2hlaWdodDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNyk7bWFyZ2luOjAgNXB4O21pbi13aWR0aDo2cHh9Ci5wbC5kb25le2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMTgpfQouc3RlcHtkaXNwbGF5Om5vbmV9LnN0ZXAuc2hvd3tkaXNwbGF5OmJsb2NrO2FuaW1hdGlvbjpmdSAuMzVzIGVhc2UgYm90aH0KLnNsYmx7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjkzOHJlbTtmb250LXdlaWdodDo2MDA7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjIwcHg7bGV0dGVyLXNwYWNpbmc6LjAxZW19Ci5zYm94e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEwcHg7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTYpO3BhZGRpbmc6MCAycHg7dHJhbnNpdGlvbjpib3JkZXItY29sb3IgLjJzfQouc2JveDpmb2N1cy13aXRoaW57Ym9yZGVyLWJvdHRvbS1jb2xvcjojZmZmZmZmfQouc2l7b3BhY2l0eTouMjtmb250LXNpemU6MS4yMjVyZW07ZmxleC1zaHJpbms6MH0KI2JJbnB1dHtmbGV4OjE7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtvdXRsaW5lOm5vbmU7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjUxMnJlbTtjb2xvcjojZmZmZmZmO3BhZGRpbmc6MTJweCAwfQojYklucHV0OjpwbGFjZWhvbGRlcntjb2xvcjojZmZmZmZmfQouY2xye2JhY2tncm91bmQ6bm9uZTtib3JkZXI6bm9uZTtjb2xvcjojZmZmZmZmO2N1cnNvcjpwb2ludGVyO2ZvbnQtc2l6ZToxLjE1cmVtO2Rpc3BsYXk6bm9uZTtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZn0KLmNsci5zaG93e2Rpc3BsYXk6YmxvY2t9Ci5id3JhcHtwb3NpdGlvbjpyZWxhdGl2ZTttYXJnaW4tYm90dG9tOjIwcHh9Ci5kcm9we3Bvc2l0aW9uOmFic29sdXRlO2xlZnQ6MDtyaWdodDowO2JhY2tncm91bmQ6IzBmMGYwZjtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEpO2JvcmRlci10b3A6bm9uZTttYXgtaGVpZ2h0OjIwMHB4O292ZXJmbG93LXk6YXV0bzt6LWluZGV4OjUwO2Rpc3BsYXk6bm9uZX0KLmRyb3Aub3BlbntkaXNwbGF5OmJsb2NrfQouZGl0ZW17cGFkZGluZzoxMXB4IDE0cHg7Zm9udC1zaXplOjEuMzYzcmVtO2NvbG9yOiNmZmZmZmY7Y3Vyc29yOnBvaW50ZXI7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDUpO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLmRpdGVtOmhvdmVye2NvbG9yOiNmZmZ9Ci5kaXRlbSBtYXJre2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Y29sb3I6I2ZmZjtmb250LXdlaWdodDo3MDB9Ci5ub3Jlc3twYWRkaW5nOjE0cHg7Zm9udC1zaXplOjEuMjg4cmVtO2NvbG9yOiNmZmZmZmY7Zm9udC1zdHlsZTppdGFsaWM7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmfQoubm8tYnJlZWQtYmFubmVye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7cGFkZGluZzoxNHB4IDA7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDYpO2N1cnNvcjpwb2ludGVyO3RyYW5zaXRpb246Y29sb3IgLjJzO21hcmdpbi10b3A6NHB4fQoubm8tYnJlZWQtYmFubmVyOmhvdmVyIC5uby1icmVlZC1iYW5uZXItdGl0bGV7Y29sb3I6I2ZmZmZmZn0KLm5vLWJyZWVkLWJhbm5lci1pY29ue2ZvbnQtc2l6ZToxLjU3NXJlbTtmbGV4LXNocmluazowO29wYWNpdHk6LjN9Ci5uby1icmVlZC1iYW5uZXItdGV4dHtmbGV4OjF9Ci5uby1icmVlZC1iYW5uZXItdGl0bGV7Zm9udC1zaXplOjEuNDM4cmVtO2NvbG9yOiNmZmZmZmY7Zm9udC13ZWlnaHQ6NjAwO21hcmdpbi1ib3R0b206MnB4O2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLm5vLWJyZWVkLWJhbm5lci1zdWJ7Zm9udC1zaXplOjAuODg3cmVtO2NvbG9yOiNmZmZmZmY7bGluZS1oZWlnaHQ6MS41O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQoubm8tYnJlZWQtYmFubmVyLWFycm93e2NvbG9yOiNmZmZmZmY7Zm9udC1zaXplOjEuMjI1cmVtO2ZsZXgtc2hyaW5rOjA7dHJhbnNpdGlvbjpjb2xvciAuMnN9Ci5zYmFkZ2V7ZGlzcGxheTpub25lO2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTJweDttYXJnaW4tYm90dG9tOjIwcHh9Ci5zYmFkZ2Uuc2hvd3tkaXNwbGF5OmZsZXh9Ci5ibmFtZXtib3JkZXItYm90dG9tOjFweCBzb2xpZCAjZmZmZmZmO2NvbG9yOiNmZmZmZmY7cGFkZGluZzoycHggMDtmb250LXNpemU6MS40MzhyZW07Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmfQouYmNoZ3tmb250LXNpemU6MC44cmVtO2NvbG9yOiNmZmZmZmY7Y3Vyc29yOnBvaW50ZXI7bGV0dGVyLXNwYWNpbmc6LjEyZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO3RyYW5zaXRpb246Y29sb3IgLjJzfQouYmNoZzpob3Zlcntjb2xvcjojZmZmZmZmfQouc3ZidG57ZGlzcGxheTpibG9jaztwYWRkaW5nOjA7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wOCk7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Y3Vyc29yOnBvaW50ZXI7dGV4dC1hbGlnbjpsZWZ0O3RyYW5zaXRpb246Ym9yZGVyLWNvbG9yIC4yczt3aWR0aDoxMDAlO292ZXJmbG93OmhpZGRlbjtwb3NpdGlvbjpyZWxhdGl2ZX0KLnN2YnRuOmhvdmVye2JvcmRlci1ib3R0b20tY29sb3I6I2ZmZmZmZn0KLnN2YnRuLmFjdGl2ZXtib3JkZXItYm90dG9tLWNvbG9yOiNmZmZmZmZ9Ci5zdnB7Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNmZmZmZmY7ZmxleC1zaHJpbms6MH0KLm1hc3RlcnN7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyO2dhcDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNyl9Ci5tYnRue2JhY2tncm91bmQ6IzBhMGEwYTtwYWRkaW5nOjIycHggMTJweDt0ZXh0LWFsaWduOmNlbnRlcjtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmJhY2tncm91bmQgLjJzO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtib3JkZXI6bm9uZX0KLm1idG46aG92ZXJ7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wMyl9Ci5tYnRuLmFjdGl2ZXtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA1KX0KLm1hdnt3aWR0aDo0MHB4O2hlaWdodDo0MHB4O2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xNCk7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO21hcmdpbjowIGF1dG8gMTBweDtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjEuNDM4cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmfQoubWJ0bi5hY3RpdmUgLm1hdntib3JkZXItY29sb3I6I2ZmZmZmZjtjb2xvcjojZmZmZmZmfQoubW5hbWV7Zm9udC1zaXplOjEuNDM4cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZn0KLm1idG46aG92ZXIgLm1uYW1le2NvbG9yOiNmZmZmZmZ9Ci5tYnRuLmFjdGl2ZSAubW5hbWV7Y29sb3I6I2ZmZmZmZn0KLm10aXRsZXtmb250LXNpemU6MC44cmVtO2NvbG9yOiNmZmZmZmY7bWFyZ2luLXRvcDozcHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5nYnRue2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7cGFkZGluZzoxNHB4IDA7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wOCk7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjEuNDM4cmVtO2N1cnNvcjpwb2ludGVyO3dpZHRoOjEwMCU7dHJhbnNpdGlvbjphbGwgLjJzfQouZ2J0bjpob3Zlcntjb2xvcjojZmZmZmZmfQouZ2J0bi5hY3RpdmV7Y29sb3I6I2ZmZmZmZjtib3JkZXItYm90dG9tLWNvbG9yOiNmZmZmZmZ9Ci5jYWwtaHtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6Y2VudGVyO21hcmdpbi1ib3R0b206MTZweH0KLmNhbC1te2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6MS45MzhyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNmZmZmZmZ9Ci5jYWwtbntiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyOm5vbmU7Y29sb3I6I2ZmZmZmZjtjdXJzb3I6cG9pbnRlcjtmb250LXNpemU6MS41NzVyZW07cGFkZGluZzo0cHggOHB4O3RyYW5zaXRpb246Y29sb3IgLjJzfQouY2FsLW46aG92ZXJ7Y29sb3I6I2ZmZmZmZn0KLmNne2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDcsMWZyKTtnYXA6MnB4O21hcmdpbi1ib3R0b206MTJweH0KLmNkbnt0ZXh0LWFsaWduOmNlbnRlcjtmb250LXNpemU6MC42NjNyZW07Y29sb3I6I2ZmZmZmZjtwYWRkaW5nOjRweCAwO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtsZXR0ZXItc3BhY2luZzouMWVtfQouY2R7dGV4dC1hbGlnbjpjZW50ZXI7Y3Vyc29yOnBvaW50ZXI7Y29sb3I6I2ZmZmZmZjtib3JkZXI6MXB4IHNvbGlkIHRyYW5zcGFyZW50O3RyYW5zaXRpb246YWxsIC4yc30KLmNkOmhvdmVyOm5vdCguZGlzKTpub3QoLnBhZCkgLmNkLWlubmVye2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDcpIWltcG9ydGFudDtjb2xvcjojZmZmZmZmIWltcG9ydGFudH0KLmNkLnNlbCAuY2QtaW5uZXJ7YmFja2dyb3VuZDojZmZmZmZmIWltcG9ydGFudDtjb2xvcjojMGEwYTBhIWltcG9ydGFudDtmb250LXdlaWdodDo3MDAhaW1wb3J0YW50O2JvcmRlcjpub25lIWltcG9ydGFudH0KLmNkLnRvZCAuY2QtaW5uZXJ7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4yOCk7Y29sb3I6I2ZmZn0KLmNkLmRpc3tjb2xvcjojZmZmZmZmO2N1cnNvcjpkZWZhdWx0fQoudGd7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoNCwxZnIpO2dhcDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNyl9Ci50YnRue2JhY2tncm91bmQ6IzBhMGEwYTtib3JkZXI6bm9uZTtwYWRkaW5nOjEzcHggNHB4O3RleHQtYWxpZ246Y2VudGVyO2ZvbnQtc2l6ZToxLjMyNXJlbTtjb2xvcjojZmZmZmZmO2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjt0cmFuc2l0aW9uOmFsbCAuMnN9Ci50YnRuOmhvdmVye2NvbG9yOiNmZmZmZmY7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNCl9Ci50YnRuLmFjdGl2ZXtjb2xvcjojZmZmZmZmfQouc3Vte2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOm5vbmU7Ym9yZGVyLXRvcDoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDgpO3BhZGRpbmc6MjBweCAwO21hcmdpbi1ib3R0b206MjBweH0KLnNye2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjtwYWRkaW5nOjhweCAwO2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA1KTtmb250LXNpemU6MS4zNjNyZW07Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmfQouc3I6bGFzdC1jaGlsZHtib3JkZXItYm90dG9tOm5vbmU7cGFkZGluZy10b3A6MTRweH0KLnNse2NvbG9yOiNmZmZmZmZ9LnN2e2NvbG9yOiNmZmZmZmY7dGV4dC1hbGlnbjpyaWdodH0KLnNwe2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6Mi40MzhyZW07Y29sb3I6I2ZmZmZmZjtmb250LXdlaWdodDo2MDB9Ci5mZ3ttYXJnaW4tYm90dG9tOjIwcHh9Ci5mbHtmb250LXNpemU6MC43MTJyZW07bGV0dGVyLXNwYWNpbmc6LjIyZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbTo4cHg7ZGlzcGxheTpibG9jaztmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZn0KLmZpe3dpZHRoOjEwMCU7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xNCk7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjEuNTEycmVtO3BhZGRpbmc6MTBweCAwO291dGxpbmU6bm9uZTt0cmFuc2l0aW9uOmJvcmRlci1jb2xvciAuMnN9Ci5maTpmb2N1c3tib3JkZXItYm90dG9tLWNvbG9yOiNmZmZmZmZ9Ci5jYnRue2Rpc3BsYXk6YmxvY2s7d2lkdGg6MTAwJTtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC1zaXplOjAuODYycmVtO2ZvbnQtd2VpZ2h0OjYwMDtsZXR0ZXItc3BhY2luZzouMjhlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7dGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzoxNnB4O2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMjUpO2N1cnNvcjpwb2ludGVyO3RyYW5zaXRpb246YWxsIC4yc30KLmNidG46aG92ZXJ7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLnNibG9ja3t0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjUycHggMjBweDtkaXNwbGF5Om5vbmV9Ci5zYmxvY2suc2hvd3tkaXNwbGF5OmJsb2NrO2FuaW1hdGlvbjpmdSAuNXMgZWFzZSBib3RofQouc2kye2ZvbnQtc2l6ZTozLjZyZW07bWFyZ2luLWJvdHRvbToyMHB4O29wYWNpdHk6LjR9Ci5zdHtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjIuNzI1cmVtO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbToxMHB4O2ZvbnQtd2VpZ2h0OjYwMH0KLnNze2ZvbnQtc2l6ZToxLjA3NXJlbTtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjEuOTttYXJnaW4tYm90dG9tOjI4cHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5oYnRue2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xNik7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXNpemU6MC44NjJyZW07bGV0dGVyLXNwYWNpbmc6LjIyZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO3BhZGRpbmc6MTNweCAyOHB4O2N1cnNvcjpwb2ludGVyO3RyYW5zaXRpb246YWxsIC4yc30KLmhidG46aG92ZXJ7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLmxvYWRpbmctc2xvdHN7Y29sb3I6I2ZmZmZmZjtmb250LXNpemU6MS4yODhyZW07cGFkZGluZzoxMnB4IDA7dGV4dC1hbGlnbjpjZW50ZXI7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc3R5bGU6aXRhbGljfQouY2R7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpjZW50ZXI7YWxpZ24taXRlbXM6Y2VudGVyO2hlaWdodDozNnB4IWltcG9ydGFudDtwYWRkaW5nOjAhaW1wb3J0YW50fQouY2QtaW5uZXJ7d2lkdGg6MzJweDtoZWlnaHQ6MzJweDtib3JkZXItcmFkaXVzOjA7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2ZvbnQtc2l6ZToxLjE1cmVtO2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZn0KLmNkLmF2YWlsIC5jZC1pbm5lcntib3JkZXI6MXB4IHNvbGlkIHJnYmEoOTAsMTgwLDkwLC4zNSk7Y29sb3I6cmdiYSg5MCwxODAsOTAsLjY1KX0KLmNkLmJ1c3kgLmNkLWlubmVye2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDcpO2NvbG9yOiNmZmZmZmZ9Ci5jZC5zZWwgLmNkLWlubmVye2JhY2tncm91bmQ6I2ZmZmZmZiFpbXBvcnRhbnQ7Y29sb3I6IzBhMGEwYSFpbXBvcnRhbnQ7Zm9udC13ZWlnaHQ6NzAwIWltcG9ydGFudDtib3JkZXI6bm9uZSFpbXBvcnRhbnR9Ci5jZC50b2QgLmNkLWlubmVye2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMjgpO2NvbG9yOiNmZmY7Zm9udC13ZWlnaHQ6NjAwfQouY2QuZGlzIC5jZC1pbm5lcntjb2xvcjojZmZmZmZmO2N1cnNvcjpkZWZhdWx0O2JhY2tncm91bmQ6bm9uZTtib3JkZXI6bm9uZX0KLnN2YnRuLXJvd3tkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6YmFzZWxpbmU7bWFyZ2luLWJvdHRvbTo2cHg7cGFkZGluZzoxNnB4IDAgMH0KLnN2YnRuLW5hbWV7Zm9udC1zaXplOjEuNTEycmVtO2NvbG9yOiNmZmZmZmY7Zm9udC13ZWlnaHQ6NjAwO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZn0KLnN2YnRuLmFjdGl2ZSAuc3ZidG4tbmFtZXtjb2xvcjojZmZmZmZmfQouc3ZidG4tcHJpY2V7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjcyNXJlbTtjb2xvcjojZmZmZmZmO2ZvbnQtd2VpZ2h0OjYwMDtmbGV4LXNocmluazowfQouc3ZidG4uYWN0aXZlIC5zdmJ0bi1wcmljZXtjb2xvcjojZmZmZmZmfQouc3ZidG4tZGVzY3tmb250LXNpemU6MXJlbTtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjEuNztkaXNwbGF5OmJsb2NrO3BhZGRpbmc6MCAwIDE0cHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7d2hpdGUtc3BhY2U6cHJlLWxpbmV9Ci5zdmJ0bi5hY3RpdmUgLnN2YnRuLWRlc2N7Y29sb3I6I2ZmZmZmZn0KLnN2YnRuLXRhZ3tmb250LXNpemU6MC45NzVyZW07Y29sb3I6I2ZmZmZmZjtmb250LXN0eWxlOml0YWxpYztkaXNwbGF5OmJsb2NrO21hcmdpbi10b3A6MnB4O3BhZGRpbmc6MCAwIDE0cHg7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmfQouc3ZidG4uYWN0aXZlIC5zdmJ0bi10YWd7Y29sb3I6I2ZmZmZmZn0KQG1lZGlhKG1heC13aWR0aDo0MDBweCl7LnN2YnRuLW5hbWV7Zm9udC1zaXplOjEuMzYzcmVtfS5zdmJ0bi1wcmljZXtmb250LXNpemU6MS41MTJyZW19LnN2YnRuLWRlc2N7Zm9udC1zaXplOjAuOTM4cmVtfS5zdmJ0bi10YWd7Zm9udC1zaXplOjAuODg3cmVtfX0KQGtleWZyYW1lcyBmdXtmcm9te29wYWNpdHk6MDt0cmFuc2Zvcm06dHJhbnNsYXRlWSgxMHB4KX10b3tvcGFjaXR5OjE7dHJhbnNmb3JtOnRyYW5zbGF0ZVkoMCl9fQoubGFuZy1iYXJ7cG9zaXRpb246Zml4ZWQ7dG9wOjEycHg7cmlnaHQ6MTRweDt6LWluZGV4Ojk5OTtkaXNwbGF5OmZsZXg7Z2FwOjZweH0KLmxhbmctYnRue2JhY2tncm91bmQ6cmdiYSgxMCwxMCwxMCwuOTIpO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMSk7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXNpemU6MC43NzVyZW07bGV0dGVyLXNwYWNpbmc6LjE1ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO3BhZGRpbmc6NXB4IDEwcHg7Y3Vyc29yOnBvaW50ZXI7dHJhbnNpdGlvbjphbGwgLjJzfQoubGFuZy1idG46aG92ZXJ7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLmxhbmctYnRuLmFjdGl2ZXtib3JkZXItY29sb3I6I2ZmZmZmZjtjb2xvcjojZmZmZmZmfQouY2JrLWJ0bntiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTQpO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC1zaXplOjAuODYycmVtO2xldHRlci1zcGFjaW5nOi4xNmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtwYWRkaW5nOjEycHggMjBweDtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmFsbCAuMnM7d2lkdGg6MTAwJX0KLmNiay1idG46aG92ZXJ7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLm1idG4sLnN2YnRuLC5nYnRuLC50YnRuLC5jYnRuLC5oYnRuLC5jYmstYnRuLC5sYW5nLWJ0biwuYmFjay1idG4sLm9wdCwuZGl0ZW0sLmNkLC5uby1icmVlZC1iYW5uZXIsLmJjaGd7dHJhbnNpdGlvbjphbGwgLjE1cyBlYXNlfQoubWJ0bjphY3RpdmUsLnN2YnRuOmFjdGl2ZSwuZ2J0bjphY3RpdmUsLnRidG46YWN0aXZlLC5jYnRuOmFjdGl2ZSwuaGJ0bjphY3RpdmUsLmNiay1idG46YWN0aXZlLC5sYW5nLWJ0bjphY3RpdmUsLmJhY2stYnRuOmFjdGl2ZSwub3B0OmFjdGl2ZSwuZGl0ZW06YWN0aXZlLC5jZDphY3RpdmUsLm5vLWJyZWVkLWJhbm5lcjphY3RpdmUsLmJjaGc6YWN0aXZle3RyYW5zZm9ybTpzY2FsZSgwLjk2KX0KPC9zdHlsZT4KPC9oZWFkPgo8Ym9keT4KPGEgaHJlZj0iL2FkbWluP3Bhc3M9YW56YTE5ODUiIGlkPSJhZG1pbkJhY2tMaW5rIiBzdHlsZT0iZGlzcGxheTpub25lO3Bvc2l0aW9uOmZpeGVkO3RvcDoxNHB4O3JpZ2h0OjE0cHg7Zm9udC1zaXplOjAuOXJlbTtjb2xvcjojYzlhMDVhO3RleHQtZGVjb3JhdGlvbjpub25lO3otaW5kZXg6OTk5O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2JhY2tncm91bmQ6cmdiYSgxMCwxMCw5LC44NSk7cGFkZGluZzo2cHggMTJweDtib3JkZXItcmFkaXVzOjIwcHg7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDIwMSwxNjAsOTAsLjM1KSI+4oaQINCQ0LTQvNC40L0t0L/QsNC90LXQu9GMPC9hPgo8c2NyaXB0PmlmKGxvY2F0aW9uLnNlYXJjaC5pbmRleE9mKCdwYXNzPWFuemExOTg1JykhPT0tMSl7ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2FkbWluQmFja0xpbmsnKS5zdHlsZS5kaXNwbGF5PSdibG9jayc7fTwvc2NyaXB0Pgo8ZGl2IGNsYXNzPSJsYW5nLWJhciI+CiAgPGJ1dHRvbiBjbGFzcz0ibGFuZy1idG4iIG9uY2xpY2s9InNldExhbmcoJ2V0JykiPkVUPC9idXR0b24+CiAgPGJ1dHRvbiBjbGFzcz0ibGFuZy1idG4iIG9uY2xpY2s9InNldExhbmcoJ2VuJykiPkVOPC9idXR0b24+CiAgPGJ1dHRvbiBjbGFzcz0ibGFuZy1idG4gYWN0aXZlIiBvbmNsaWNrPSJzZXRMYW5nKCdydScpIj5SVTwvYnV0dG9uPgo8L2Rpdj4KCjwhLS0gSE9NRSAtLT4KPGRpdiBjbGFzcz0ic2NyZWVuIGFjdGl2ZSIgaWQ9ImhvbWVTY3JlZW4iPgo8ZGl2IGNsYXNzPSJjb24iPgogIDxkaXYgY2xhc3M9ImxvZ28taW1nLXJvdyI+CiAgICA8aW1nIHNyYz0iZGF0YTppbWFnZS9wbmc7YmFzZTY0LGlWQk9SdzBLR2dvQUFBQU5TVWhFVWdBQUFVTUFBQURyQ0FZQUFBRHpDL1F3QUFBQldHbERRMUJKUTBNZ1VISnZabWxzWlFBQWVKeDlrTEZMdzFBUXhyOVdwYUIxRUIwY0hES0pRNVNTQ3JvNHRCVkVjUWhWd2VxVXZxYXBrTVpIa2lJRk4vK0JnditCQ3M1dUZvYzZPamdJb3BQbzV1U2s0S0xsZVMrSnBDSjZqK04rZk8rNzR6Z2dPVzV3YnZjRHFEdStXMXpLSzV1bExTWDFqQVM5SUF6bThaeXVyMHIrcmovai9UNzAzazdMV2IvLy80M0JpdWt4cXArVUdjWmRIMGlveFBxZXp5WHZFNCs1dEJSeFM3SVY4b25rY3NqbmdXZTlXQ0MrSmxaWXphZ1F2eENyNVI3ZDZ1RzYzV0RSRG5MN3RPbHNyTWs1bEJOWXhBNDhjTmd3MElRQ0hkay8vTE9CdjRCZGNqZmhVcCtGR256cXlaRWlKNWpFeTNEQU1BT1ZXRU9HVXBOM2p1NTNGOTFQamJXREoyQ2hJNFM0aUxXVkRuQTJSeWRyeDlyVVBEQXlCRnkxdWVFYWdkUkhtYXhXZ2RkVFlMZ0VqTjVRejdaWHpXcmg5dWs4TVBBb3hOc2trRG9FdWkwaFBvNkU2QjVUOHdOdzZYd0JBNmRpRThIWVdoTUFBRUh3U1VSQlZIaWM3WjE1ZkZWRnN2anIzRFg3Qm9RbFFBaWJLQUkrVUZCeFgxQVp3SEY1SWlCUEhSY2VEaTdvcVBoVFJsRkFRY1ZSVVo4UFVYSFVKK0xvNEs2QUFzNjRvT0RHSWhEQ2tvUkE5dlZ1WjZuZkgxaE5uNzduSmpjUUlJSDZmajc1M0NYbmR2ZnBjN3BPVlZkMU5RRERNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNRXpiUUR2U0RUaGFjTGxjZ0lpZ2FScFlsZ1Z1dHh0TTB3Uk5PN2d1UnNTb2VnQUFMTXNDQUJEbDAzR2Fwb2syTUMyUHgrTUJ3ekFPK3JyS0lDSzRYQzV4emVUM0ROTm1JT0VrbzJrYWVEeWVGaW5mNC9HQTIrMk9XVThzV3JJTnh6b3VsMHYwdjl2dEJyZmIzYUxsYTVvR2JyY2JmRDZmK0s2bDYyQ2FoalhERnNEbjgwRWtFckY5UnhxYXF0azFGL24zVGxxZy9IL1dLQTRkYnJjYkVGSDhrU1hRRXFqWDArdjFncTdyTFZJMnd4eFdWQTNNU1lzN1VIdytuMDBEYkVvYmRMbGNMV3JDSGV0NFBCN1JuM0svdHFUV3JWNHYrc3phSWRPbWtNMG53dXYxQ3NIVWtxakNqbDVsTTA3K3pFS3haWkN2cmN2bGFuRWhKWmRIcGpKUGNUQnRFbFVRdGpRa0JGWGhHcS9HeUJ3NGREMXAyZ05nLy9WdWlZZGRyTEw0ZWpKdER2V205ZnY5QUdBM3J3NjJmTFVjV2V1VEo5M3BmN0hheGh3WXFpWVl5NkYxSUxoY0xxRUZVajJ5QUdZT0g5emJMWURQNTRPSEhub0lPM2Z1REpGSXBFWE5LRGxjeHpBTTBIVWQ2dXJxb0tTa0JQYnUzUXViTm0zU1NrcEtvTEt5RWdEc1RwU1djT0FjNjV4Kyt1azRhZElrQUFDSVJDTGc5WG9oSEE2RDIrMEd5N0lPV2lnYWhnRVZGUlh3OE1NUGErRndXSHpQempDbVRaS1ltQWpyMTYvSGNEaU1obUVnSXFLdTYyaFpGcHFtaVlnb1h1bi9oSzdyaUlob1dSWmFsbVg3SDMybVYvbTM4dnRJSklLR1llRGF0V3Z4amp2dXdMeThQRWhOVFkzU0V1a3p2UjRPemNPcExucFkwS3VUOWlzalAxem8yRU14Sit2RXVISGpSRCtyMTFLRnJvbDZqZVZyS0Y5eituNzkrdldZa3BJaU5NVERjVjVNTkt3WnRnQStudyttVHAyS2FXbHBvR2thSkNVbFFidDI3V0R3NE1Gd3dna25nR1ZadG1Cc1hkZkI2L1VLclk4K2s2WkJtb2ZINHhIZm1hWXBoRUlrRWdHZnoyZlRUT1QzaG1IQSt2WHI0YVdYWG9JVksxWm9CUVVGSXZSSERzK2h3UEJERFlXS09HbXE5SjJzQ2RGNWE1b20ycWVHRmNtL1BaU2NjY1laT0hIaVJQQjRQT0R6K1NBM054Y0dEeDRNQ1FrSjRoanFTMVdncXlFemRLMi8vZlpiS0Nnb0FGM1hJU0VoQVlxS2l1RGhoeC9XZ3NIZ1lUMDN4ZzRMdzRPRUJBcVpUWFFEZXp3ZU9PNjQ0K0NpaXk3Q0o1OTgwaWJNQUVBSVFIcXRyYTJGMU5SVTJMbHpKMnpjdUJIV3JWc0hSVVZGVUYxZExjcE1Ta3FDcmwyN3dybm5uZ3Rubm5tbXplT29hWnBOaUFhRFFmRDcvZkR6enovRDBxVkxZZmJzMlpxdTY3YjR1TU14Mk5SQlRTczQ1TDZqNzAzVGpHczFCczJaSGc1QlRtMEQyUGVReWN2TGcrT1BQeDVuekpnQko1OThzamlHaEowVGRPMzM3TmtETjl4d0Eyelpza1VyTEN5RVNDUUNpR2lMVTZWK29QTm1nY2kwS1dLWk5iUjY1UGJiYjBmVE5ERWNEdHRNTE5rTXJxeXN4QjQ5ZW9EUDU0UEV4RVFBMkNkSWFESmREcmx3dTkyUWtaRUJNMmJNUUVURVFDQmdLOU0wVFp2WkhRZ0VjTjI2ZGFpYXpvY2pqbzBFaVd6V3FtRWo4bWNuQjVDNitrTTE5dzhsY2gyeW84UHY5OE9MTDc2SW9WRElaZzdMMHg2eVNWMVdWb2JkdW5XRHBLUWtVYTdzQ0NQSEc4TzBhV2lReXNKRm5nODc0WVFUWU4yNmRiWkJJd3RGMHpTeHJLd00yN1ZySjM1RFpYaTlYdHU4R3cwZ2VzM096b2EzMzM0YnE2dXIwYklzRElmRFVlVmJsb1dHWWVEbm4zK09mZnYyQllBak0vamNiamVrcDZmRHFhZWVpdVBHamNQWFgzOGR0MjdkaW5WMWRVSjRSQ0lSTENzcnd3OC8vQkN2dnZwcUhEeDRNQUxZUGVpcWtEeGNiVmZuT3Z2MTZ3ZmZmZmVkcmI5cHZwQWVTSWlJNFhBWXI3MzIycWp6QUlDb2VVSmFta2Z2R2FiTjRCUWZSamUwSFA3eTBVY2ZvV0VZcU91NkdEZzBXSFJkeCtycWF1emN1Yk1vUTM1dHJDNlB4d05wYVdrd2NlSkVySyt2anhLNGhtSFlCdXF5WmNzd0t5dnJzQTAwT1Q3eXozLytNNzd4eGhzMjRVZEVJcEVvUjVGbFdiaGp4dzU4L1BISE1UYzMxeVlrV2lwMHFTbms2Nmc2Z2R4dU56ejExRk1ZRG9lam5GMHl2Lzc2S3g1MzNIRTJqZC9wVmRXWWVRVUsweVp4OG9qS04vTnJyNzBXcFEyU1NXV2FKbFpXVm1LSERoMUVXVEswb2dYQVdVTUMyQ2R3VHp2dE5BeUh3MUdtbW15K1JTSVJYTGR1M1dHYmlISzVYTkNwVXlkWXZIZ3hWbGRYQzAySjJvSzR6NFQ4K3V1djhldXZ2eGJIMFArb245YXVYWXNubjN3eUhvbVZOZW9LRkpscnJybkdaaXFybm1iTHNuREZpaFZJZ2hEQW5rUkRmbGp3TWp6bXFFRU5tZ1hZUDNqbXpadG5FMHBxbUVaRlJRVzJhOWZPY2FVRFFMU3dkWHJ2OVhwaC9QangyTkRRWUJ1VVZDZHBYTUZnRUtkUG40Nkh3OHpNenM2R3p6NzdUQWczRWhDaFVBaS8vLzU3UFBQTU0xRU9QUGI3L2ZESUk0OWdUVTJOemVSRVJDd29LTUNjbkJ4Ujl1RVVHcktKTEQrWWhnNGRpclcxdGVMYzVMQWFhdmZTcFV0Um5qTlZ6VzJBNkN4RGJDSXpSeDMwNUo4MmJacE5HS2hVVkZRSXpaQm96b0NnZ1phYW1ncExseTYxelZuUndKUS8vL0RERDJMKzhHQ0ZpcXk1RW02M0d4SVNFdUNmLy93bm1xWnBFNGFJaU04OTl4d21KaWJHWE1vNGFkS2tLTE0vRW9uZ3FsV3JzRFU1Ry9MeThxQzZ1dG9tQk5YcnUzanhZZ1N3TzVGWSsyT09PVWlqdS9mZWUyMkRSUjB3QnlvTXlic01BQ0tzNXVTVFR4WmFpbXEya1VuWDBOQ0FFeVpNc0drc1ZDZVpvczNWVG1TaHFHa2FUSjgrUGFwKzB6Unh5WklsbUoyZGJmc3RlY3ZwWERJeU1tRFJva1cyTnBQQXVmbm1tMXVOUU96ZXZUdFVWVlhGRE1SR1JGeTBhSkVRaGtmQytjUEVCMStSUXd6K252L3VVTVdNVWRabGl0a3pUUlBXcmwyckZSWVcyc0pRS0ZiUDUvTUJJa0pTVWhLTUd6ZE9CSHZMUWMxMGJIUGJxK3U2aUxkTFQwK0hNV1BHaVBvamtRaFlsZ1dXWmNFWFgzd0JwYVdsdHJBYmlyT2o3T0RWMWRXd1pzMGFDSWZENFBmN2Jjc2N4NHdaMDJyeS9jbm5yRUxYdnFHaHdmYWQvTXEwSGxnWUhnVlFFRFBBL2dINHdBTVBDQUVqcjRDaElHakRNT0NjYzg0UkdwbnFtR2pPWUZWakNMMWVMd3diTmd5N2QrOHV5dkg1Zk9CeXVXRFBuajN3MDA4L2dhWnBRcENyU1NjUUVSSVNFdUNISDM2QVBYdjJpSExwM0RwMjdBaloyZG10WWw3Tk1BemJ3OFFKZVdVSndjS3c5Y0hDOEFod0lDWm9VMlhSS2dlWHl3VStudytXTEZtaXljS0Y2cU5sZXg2UEI1S1RrNEZpRzFYTnRUbG1uRncrSWtJa0VvSCsvZnNET1lSSVdDTWkxTlhWUVhWMXRZWlN0bWhhcGlobkN3K0ZRckIxNjFhdHFxcktWb2VtYVpDYW1pcmFmYVJwU3FqUkVyeldJTGlaeG1GaDJNWWhvUUt3ejhTazdEYkJZQkFhR2hxRW9BSFlyLzNSQUxZc0MzSnljbEJOWVg4ZzgxbGszdElTT1ovUEoweDNlUk9sek14TTZOeTVNMUw5VHJHVVZEOWxjYUZ6Q0lWQ0FBRFEwTkFnbHJJZGFaeldUS3YvazkrelVHeTlzREJzNDZqSkZ1VFVVc0ZnVUF3KzBzNUlHSklRVFV0THM1Vkg1dlRCcEk5U056YVNOY2YyN2R2RHNHSER4UDlsUVU3SksrU0VEWFFNbWM2V1pVRkRRd09VbEpTMENvK3NVL0MzS2hobGdjbENzZlhDd3ZBd29RNlFsdEpxYUZFL1FMUmdESVZDUXZqSmMyNXk4Z05kMTIxSkVXVGlFVGJ5NEtiQkhvbEVRTmYxS0NGTjdSZzJiQmhRdktEY2ZzTXdSQVlZQUlDZVBYc2lDV3NTMG9nSU8zZnVoSWFHaHNPV3FLRXhxSzJ4cmkvMUNYMW1ZZGg2WVdGNGlJbDE4N2ZrbkdFczd5K2xtU0t0VU43SG1ZNHRMQ3pVU0FNallTTnJhMDBoRDNaNTBOZlYxVUVnRUJCdHBIa3p5N0xnNG9zdmh2NzkreU1KRWxrVGxQdmx6RFBQQkZxaUtBdnhSeDU1UkNQQkt2ZERZeXVBNUdQa1B6cE9YU1BzbEpMTGlhYTJjVlhiUmRlZ0thY0xjL2hoWWRqR0lmTVJZTDl6aEFaWldsb2FHSVloZ3BrcFBaUmxXZUR4ZUNBY0RrTkZSWVhOaEhaNmJRelMrS2d0SklDKytlWWIyTE5uRHhpR0lVeGdlVDd4bzQ4K2d0VFVWQURZSDJ4Tm1xRnBtdENoUXdjNDU1eHpJREV4VVdpdmlBalRwMCtIZ29JQ2Nidzh4MGp0SU1HbUNuUFNqT1UvT282T3BmbEpTaWNXei9telVEczZZR0hZeHRFMFRUZ1cvSDYvME00eU16UEI0L0dBclBYUjhTNlhDd3pEZ0czYnRrRnRiYTM0WHZZNHh3dDVzVlVCVkZCUW9PM2V2VHNxS0Z4MjRuejIyV2VZbDVjSGNwNUZFaTVUcGt6QlVhTkdpWDJFQTRFQVBQLzg4L0RFRTA5b3NvQ1g1emZsK1ZGNWlWK3NmcVA0VEFDN0kwbzFhUnZqVUd3QXhod1pXQmkyY1VoWUFPenp2dElBSGp0MmJOVGFZMW5vdUZ3dWVPdXR0NFM1SnM4ak5rY1kwUEVrZ0FEMm1ab05EUTJ3WU1FQzRmV1Z0VFRMc3NEcjljSkpKNTBFVTZkT3hheXNMTnU4NVlJRkMvQ3ZmLzJyMERwcmFtcGd4b3daOE5CREQybFVEZ1ZkazJrdGEzcHlteHByTTdWTEZvamtnSkpOL3NidysvMTRxS1pBR09hb2dnYkczWGZmN2JnbW1UalE1WGgwakpvZ3RiQ3dVQ3pIazljbDB4cmg2dXBxdlBqaWk2T1dpY2xseHV0QWtaZVl5V2E2MisyRzh2SnlrYVVHTVhwL0VNdXk4SjU3N2tHWHl3VURCZ3pBOWV2WG82N3JZZ2xlUVVFQnBxYW1SczBuT3EySGx0dnNsQkEyVnNZYnRjMXFmemJHS2FlY2dyVzF0WTdaYW9qSEgzOGNuZnFYaFdicmdqWEROZzVwTDZRRmVUd2VPUGZjYzdGcjE2NDJJVUNha05mcmhVZ2tBdSsvL3o1ODlkVlhHcFhocEEzRzY2Mmxjc2xrSmVlR2FacHc0b2tuYWlVbEpXSVZoaXhnTGNzQzB6Umh6cHc1c0dyVkt2emxsMStnWDc5K1VGOWZENnRYcjRZLy9lbFAwS2RQSHkwWURJcnpJL09YbHNHUlZxeHF3ZkxlTVRSUEdtdEpKSDB2eDBuR2k4L25PK0NWTzB6cklyN0hIOU5xSWZPV3pMcXNyQ3k0N2JiYkFBQnMzOHNlNGxBb0JFODk5WlJ0elN6QWdXMUNSTWVyMjVOU2tQWGV2WHRoeG93WjhOUlRUNG5rcGlTWVpPM3JqRFBPZ0pLU0VuanZ2ZmRnMmJKbHNIejVjcTIrdmw3OG4weG1WYURwdWc2Wm1ablFwMDhmMUhWZHJMMm0rVURTS09sUFRrS2hhWnFZai96eXl5ODErZnpqRllxa29UckZHckxtMTdaZ1lkakdrYjJmUHA4UFJvNGNpU05IamhRYlRRSHNENzhod1huNTVaZkRqei8rcUtsSkhHUmhJQXVmZUNEaElXOWtSRUw0alRmZTBNNC8vM3djTjI2YzBGSXA3bEVXNWo2ZkR6NzU1QlA0NUpOUE5KckxvM0xwbGI2amNqUk5nd2tUSnVEOTk5OHZObGFTblRheTRJd2xuRFpzMkFBWFhIQ0JtSE9sY3VQMUpqTkhCMndtdHdMSSthRHVvQ2ZQeFJFZWo4ZVdHWmtFWFVKQ0FseHd3UVc0Y09IQ3FQMVlLRXlrdXJvYUprMmFCRjk4OFlWR2pnSloyTW52eWJSc0NyV05ja2dLQ1RyVE5PSEJCeC9VZ3NHZzdSelZlYlIyN2RyQjdObXpvVmV2WHFLc1dQTnJKSEF0eTRMcTZtcll2bjA3N04yN0YrcnI2OEh2OTBObVppWmtabVpDVmxZV1pHVmxRVVpHaHZndVBUMGRhRnZYMnRwYXFLK3ZqOG9tRSs5RGdNS2E1R2tHRXZKc01qT01SRk1PRkhJbzFOVFVZRlpXRmdEc0V5S3FJMEF1aTk3VFg4K2VQV0gyN05sUkUvZUkrL2RCMmJ0M0wwNmFOQW5UMDlOYjNIeFR3MVRJL0FRQVNFOVBoMm5UcHVIUFAvOHNIRGkwZzU5OC9yS0Q1N1BQUHNQTXpFeXhyTTlwWHRBcGNCb0FvRy9mdnZEZ2d3OWlUVTJOS0plY1NJajdzbndIQWdHY05Xc1cvdGQvL1JjT0dqUUllL2JzS2NwdExNVy9FMlBHakluS0xLN3VoOElPRklhQitMekpobUZnWldVbHRtL2ZQdWEybWFxMlIvTmZVNlpNd2Z6OGZBeUZRbWhabGtqeFQ0SVFFYkc0dUJpSERSdUc2a0J2Q1JNdjFxb1BUZE5nN05peCtNTVBQMkFvRkVKZDE5R3lMTnkyYlJ0V1ZGUkU3ZE5pR0lZdDZlMEhIM3lBQ1FrSmpsdDF4dHBxbElTajMrK0g1NTkvUGlxN055SmllWGs1bm5MS0thaHBta2czSnBkQjdaZlhWemZHRlZkY2dZRkF3RllQQzBPR2NhQXBZVWpmVlZSVVlLZE9uY1NrUDNsTkV4SVNoQ0JNU2txQ3RMUTA2TmV2SDh5Wk0wZUVyRkI2ZkhYZmsrcnFhbnpzc2Nkc1dhRkpDTFFrY3VBeDdlazhlL1pzMng3T2htRmdmbjQrQWdEY2YvLzlXRjlmTDhKODVOQWJFcEtCUUFDZmZmYlpxTFlUcXRhbWZrNU5UUVc1VDNSZHg5cmFXcnpra2t0UTFtTGwzOHBseEp1NVo5eTRjVkhDVUwyMkxBemJCdXhBT2NLUUE4VG44OEU5OTl5RDVlWGxZbGthSlVSdDM3NDlkT3pZRVhKemM2RnYzNzZRbFpWbFcxS1duSndNQVBzR2NDQVFnTFZyMThMMjdkdGgzcng1OFBQUFAyc0Erd1FXaGFNUUIrSTlkb0tDdVFFQWhnMGJodmZjY3crTUhqMWFwTyt5TEF0V3Jsd0pJMGVPMU54dU44eWFOVXZMenM3RzIyNjdEV1FQTVA0ZTlJeS9MekdjUEhreTdOMjdGMmZQbnExUjJBczVTZVE1UFRsZ25EN0xDVldwanovNzdEUDQ3cnZ2TkpUbU5La2ZBT3pMK1F6REVLK04wZGdLRkJaMkRDTVJiOUMxdW9NZGFSYnFmaW55WjluRUxDMHR4VnR1dVFWSGp4Nk51Ym01VWZXVFdYMG92SitrYVY1NjZhVzRaY3VXcUIzaVZxeFlnYm01dVVMNDBMTEJSeDU1eEdiV3E1dEdXWmFGZ1VBQUgzMzBVWlRyY1FxU3BuaEtpcTNNeU1nQXVheXFxaXI4d3gvK0VDWDVuVEwxeEdzaUF3RGNlT09OR0F3R0c5MDNtVFZEaG9INDV3d1I5MjNTNUNRWWRGMlBXcmtoUXdLRkFxMEpKeWNNbVowdExSVFBPdXNzM0xGamgyaExNQmhFMHpTeHJxNU83TUluOTRmWDY0V1VsQlI0OGNVWGJRSlJGcUp5djl4MzMzMm96cG5LMjR1cTUzbmlpU2NDOVkxcG1yaHg0MFpVSFRHMER0cHBaVXE4WnZMa3laTnQreWF6TUdTWUdEUWxER2xPcTdTMEZJY1BINDREQmd6QWdRTUg0b0FCQS9Dc3M4N0NKNTU0QXRldFd5Y2NETExna0xYRVVDaUVoWVdGbUpPVFk5TnNhRkFmeW9RQ0tTa3BzSExsU3NmenV2UE9POFVhYVhsSkhiM201dWJDUng5OUpJNTMwZzRSOXprK3JyLytlb3gxTHFvRDVPV1hYeFpsQmdJQjdOKy92emhXem5Rai81Wm9UbDlObVRMRk51ZnBwQ0d5TUdRWWlFOHp0Q3dMS3lzcm83YlBKUEx5OHVDZGQ5NFJ4OHNDUTlZWURjUEFKNTk4RWxOU1VzUnYxWmcrZWUxdGN3ZGpyTkNlSjU1NHdsRnpMU2dvaUd0Q2NzQ0FBZmp0dDkrS2MxQ25BdWk3MHRKU3ZPR0dHNFNHcUo0YkNiak16RXdvS1NrUlFtcmV2SG5vZEZ4TDhKZS8vRVhVNCtSUlptSElNTDl6c01LUXRKYWhRNGVLV0QwbnlHdGJWMWVIRjE1NElhcmFUcXdrcE0xQlhzcEdnaWd2THcvSXZDZE5qSVREdEduVDR2Yk81T1Rrd0lZTkcyd2VjWG92QzhhcXFpcTg1WlpiYk1KTkZUVDMzWGVmTUYwM2JkcUUvL0VmL3hGMWZFc0pvbnZ1dVllRjRWRUNyMEJwNVpBM2M4MmFOZHFpUllzQUVXMWVZVlM4d3lrcEtiQnc0VUtRUTFKOFBwOVlVU0l2MFl0bk1LcEpDS2crV2g1Mzc3MzNvaHlhWWxtV01OUFhyRmtUMXpsNnZWNG9MUzJGYTY2NUJnb0tDa1JxTFhuSkhkV1prWkVCczJiTmd1dXV1dzdKWVNJZjA3dDNiN2ppaWl2QTcvZERYVjBkL00vLy9JOVllaWozV1VzSklrN3VldlRBd3JDVkk1dUM4K2ZQMXhZc1dDQ3lSdE9hWGtxS0FMQnZzL1p1M2JyQks2KzhnbjYvSDF3dWw5aUMwK1Z5Z2E3clFvQmdNOE5xVkFIY29VTUg2TmV2bjFpU0ZncUZoQUNMUkNJaTdYOVQ1NmZyT3VpNkRqLysrS00yZGVwVUtDNHV0cDAzU2t2ZEFQYkZFTTZjT1JNbVRweUk4cnBxVGRQZ25udnV3Zjc5K3dNaXd0cTFhK0gxMTEvWG5NbzRtQTJ2WkdKTk43Q0FaQmlGbHBnemxFbEtTb0lWSzFaRWxVRnpkdlFhQ0FUd2dRY2VRSUJvSjRxYzJpdmVjNURuSE1rRGUvYlpaMk5SVVpGWVlTS2J0bVZsWlhqYWFhYzFLVzFWNzdlbWFUQnMyRENzcXFyQ1lEQW9jakxLNTBmemlIdjM3c1hKa3ljanRlK1paNTdCdXJvNmNWeS9mdjBjcjBWTGV0Sm56SmpScUtlZnplUzJBMnVHclJ4MTdpOFVDc0hNbVRPaHVycmFacktxZ2kweE1SRnV1dWttT1BQTU00WDJSQUhPY242L2VKRzFTTklxdTNmdkRoa1pHZUQzKzIzYkN5QWlVRUxXcGlCek96RXhVWnpIbWpWcnRIUFBQVmNFYzZ2TEVtbk9zbjM3OWpCejVreTQrZWFiOGZ6eno4ZkpreWREU2tvS2hNTmhHRGx5SlB6MjIyK092M1hxcndQbFFCeFJUT3VFaFdFcmh6TGF5QU51MWFwVjJtT1BQU2IyUGdFQVlTNVROaG9BZ0U2ZE9zSGt5Wk50ZXlOSEloRmJucittVUZlcGtQREMzMWVKSkNjbmkwdzF0R3FEWWdEakVZWjBic0ZnRUh3K256RDkxNjlmcjAyY09CRktTa3JFQ2hJQSs4YnlMcGNMTWpNellkcTBhYkI0OFdKd3U5MFFEQWJoMVZkZkZZbHI2YmQwSGkydGxYRUtyNk1IRm9hdEhFM2JuNHRRL2p4Ly9ueHQ1Y3FWdHBSWWNzSUMvSDA1MzVWWFhnbDMzbm1uYlFVSENhNTQ1Z3lkd21rQTltZWNsbytoN05ZQSs0VEV3SUVEbXl4ZnpveE4rNlZZbGdXR1ljRDc3Nyt2M1hqampkRFEwQ0FjU2JKamlPcnUwYU1IWkdSa0FBREFsMTkrQ2JObno5Ym9RVUdhSUpVcjUzOXNDWUVZYXlzQnB1M0J3dkFRZzcvbnRxUEI1eVNBMU1Fa0N4MDZuclE5S2ljY0RzTWYvL2hITFJBSWlNMlJDRG5EdGNmamdXblRwc0hnd1lOUkxyODU3YWZmeUpvVkNSWXlPVW5veUdYZmVlZWROdTFRRFl4V3R3QlE1L1J3M3c1NjJza25uNnpWMWRYWmNpVTZlZE4xWFllLy92V3ZVRlJVSlBwUWJUOEpiTG1kY2h1ZDJ0WVl0TjFCWTMybk9xdFVadzdUT21CaDJJcUkxN3NyYXpsLytNTWZvS3FxU2d3NDJWUW1nZUQxZXVHTk45NkFqaDA3QW9BOXMzVnoyMFMvcFIzd1pMT1pIQ3MwSjltdFd6YzQ5OXh6UmVnTnRZbENmR1N0Vms3blQyVlNrb2N0VzdiQWhBa1RoSkFEMkNlc1pDODZ6U08rOHNvcmNNWVpaeUQxQndscWVXOW05ZHprNzZpTmFxTGRXRFFXb3RSVS96YlhtODhjV2xnWXRoSlU3YUVwU0N2NzVwdHZ0RVdMRm9ud0dYbi9ZdG5rN051M0wweWZQaDFwSHhKS3U5OGNaS2NESWtKK2ZqNFVGUlhaQkdRNEhMYnRqL3o2NjYvRGNjY2RKOXBNNTZuck92ajlmaUg0NUVCdVRkT0VvNGY0NUpOUHRFbVRKc0dtVFpzQVlKOFdTT2RBNStwMnUySEFnQUV3ZCs1Y09PZWNjOURyOVlyNmFEc0FwM09TQThubDZ4RFBQaWpOMGU1WUUyemRzREE4RE1RajZHS1pVazM5eHJJc21EdDNycloxNjFZaGxHaVRkZExVS0JYWVZWZGRCVU9IRGtWS1RkV2NvR3UxUFlnSW16WnQwalp2M2l5RWlXRVlZazZQMnRLdVhUdTQ5OTU3TVQwOVBTcUhJTzA1UWtLTk5FWVNYcVRaRWN1V0xkUFdybDBMQVB0VGtnSFlWOWNZaGdGRGh3NkZ2Ly85NzNERkZWZUkzSVV1bDB0b3pkUiswanhsODUvS3BuTGo2UjgxTUQxZVdETnNYYkF3YkNYRUl6Q2RoSmRsV1ZCUlVRRWpSb3pRNnVycUFBQ0VxVXIvSjFKVFUrR0REejZJdWFOYnJIYkZha2ROVFEwc1diSWt5dXltZVVTYW03djIybXZoN3J2dlJqbnZJam1GeUxTbnVpaWNSamFmL1g0L1pHVmx3VHZ2dklNVEprd1EycUM4ZDdKaEdEWnZkbloyTml4WXNBQnV2dmxtbElXcS9KNDg5ZkltVnJRTktRbktsb0RuQ0JubWR6Uk5nenZ1dUtQUndOeXlzaklrajJoamMxQk9mOFRFaVJORjZpekUvUUhLaUNqUzdsdVdoUjkvL0RGU1RzSG00SlFKMnVWeWlTUVNjcHA5TmVzMkl1TENoUXZ4ckxQT1FxY04zV1V2dFZ5UHorZUQwYU5INDdKbHk4VDVCSU5CM0xCaEE0YkRZVnRBdGdvZCsvREREMlA3OXUwQndPNHNVWk83MHZ2bWhNczgvZlRUVVJ2SXExQSt4cFpNRU1Fd2JaSjRoR0ZwYVNtbXA2ZUw0K1hmTmdWcFVxbXBxZkQ0NDQvYnlsVVRyWnFtaVlGQUFPKy8vLzY0YkRReXRSdHJWM1oyTm56enpUYzJ3YWRDZ216YnRtMzR3Z3N2aUJ5SDZ2bVJJRXBPVG9aUm8wYmg0c1dMc2FTa0JBM0R3RWdrZ3FacDRzeVpNM0hJa0NINDdydnYyb1M5Q2dsbjB6VHg4ODgveHpGanh0Z1NObEQ4WWMrZVBlSHFxNjlHVlFqR0l4VC85cmUvTlNvTUxjdkMyYk5uUndsRDFoS1pZeFluWVNndno5dTdkMjlNWWRqWXdGR2RBams1T2ZETk45L1lFbzZTWUpEckxDd3N4UFBPTzY5SmdSaExXTkgvNUhUL1JVVkZ0bDN1WklFa1o1NHhUUk4xWGNlZE8zZmk0NDgvamlOR2pNQWhRNGJnT2VlY2czZmNjUWQrL2ZYWFdGOWZMNDZUaGZxZ1FZTnNLYnkrK09JTFd6MXEvOUo1MDROZ3hZb1YyTHQzYi9ENWZPRDMrK0d1dSs3Q2NEaU00WEFZNTh5Wmc3SVRKeDZlZXVxcFJqVlR5N0p3MXF4WkxBd1poZ1RHOWRkZjd6aG82WDFaV1JtbXBLUTRiazRVYnozMGV2bmxsMk00SEJacmVKMkVvcTdyK000NzcyQnFhaW9BMkxPdk5HZkRLSGxRanh3NUVyLysrbXRIVFNuV2VjdUNUdjVlTnJkMVhjZVBQdm9JaHd3WlloUGVIbzhIMnJkdkQwdVhMc1ZBSUdDclQwMFNLN2RKbmo2Z3o5OSsreTNtNWVXSmpEdXhoSldhb0hiZXZIbU8yN1BLMzkxeHh4MDJZZWlVZ1p4aGpoa21USmhnRytTVWpoNXhYLzYvMHRKU0VmWkN4Q3VVMU9OOFBoOU1uanc1U2dDb2dzQTBUWHp5eVNmeFlBZW4zTzRlUFhyQXJGbXpzTHE2MnFiWkVmSm5kVTVURmFLNnJ1UDY5ZXZ4cHB0dXdzNmRPd09BODd4cHIxNjk0TEhISGhOOVNlY3E3NWRNZlM5L3BqeUVuMzMyR1E0ZVBOaW1jY3J6bXJHbUJ6Uk5neWVmZkZMTXg4cklmZnluUC8wSjVZY05lZEFaNXBpQ3R2bTgrdXFyUlFZV2ViRFFnS21xcWtLQWZjdk5EalJGdnh4YzdQZjdZY0dDQlZIQ2tPcW10bGlXaGJmZmZqdktBNVZRbDc0NVFmWEpyMzYvSHdZTkdvU2ZmdnFwWTMweTZpYnkxRDg3ZHV6QUtWT21ZRTVPanEwZjVYT1YrOWpyOWNJSko1d0FCUVVGcUNLbjVaZjd3TElzbkRselp0UisxVTc5R211VjBOTlBQeDJWblZzVmlCZGNjSUhRYU9rY0R1VTJEQXpUYW5HNVhIRHp6VGMzNmx3b0xTMUZlWUFjeUp5U2FvWU5HalFJdDJ6WjRpZ0U2TDFwbXJoMTYxWWNPblFvcWltK21vdmNadnA5Ky9idFlmcjA2Ymh1M1Rvc0tDakFvcUlpckt5c3hKcWFHcXlwcWNIeThuSXNLeXZEelpzMzQ2Wk5tL0RWVjE5RmlvVnM3QnpsK3NpMGRidmRrSktTQWhNbVRNQjE2OWJodG0zYnNLeXNUQWpFVUNpRVpXVmxXRlJVaE11V0xjT3p6anBMT0ZSa0lSV1BSNW5hOGVLTEwwWTkyRlQ2OU9ramZ0ZWNuZmVZd3d2UDRoNWlLTUQ1aVNlZXdMdnV1Z3NBOW1kY29WZkRNS0N5c2hMeTh2STBTblFLQUNLaFFGTlFXZXJ4SG84SHJycnFLcHcvZno1a1ptYUNydXVPR29saEdMQnExU3E0NXBwcnRJcUtpcWkxenZIVTdmVjZSZklIZGI5aFdnK2NscFlHeHg5L1BIYnIxZzBTRXhNaE1URVJhbXBxb0tpb0NMWnMyYUxWMU5TQVlSZ2lQbEZlQnkxRGdkbDBuUHAvcWo4dkx3K0dEUnVHM2JwMUU5bDFkdXpZQWYvNjE3KzBIVHQyQU1EK1pZVDQrd29XaXBHVTEzZXJ5TisvL1BMTGVPMjExd3JocUM3ajAzVWRNak16dFZBb0ZIVXRZNVhQTUVjdExwY0wzbi8vZmFFRnFrNENSTVQ2K25vODlkUlREem9lamJKT0UzNi9IeDU2NkNFUmp5ZUhtNmdPbHUrLy85NDJNcHVUckNEVzkwMmRpenhQcDVycTZ2eWNhcmJUbko2c0VhdkpJR1JrQWF2R0c4cDFxdHBuckRsRGw4c0ZyNzMybXMxWkk1djcxdS83UHFzYU85WFBEaFRtbU9Pc3M4N0NqUnMzUnBsUDhoeGFPQnpHVjE5OUZRL1VtNndLSkRXYjlTZWZmQ0tFc1NxSTZmdHdPSXo1K2ZuWXFWT25aZ1Vla3pDUlBhMnFtYXNLb2xnQ1ZCWkdzYlk1VlIwK0xwY3I2aGhWY01vUENWa0FxMU1UOHVvWHAvMlk1ZmRwYVdudzNudnZPYzRGMCtmUFAvOGNHeFBRREhQTU1IVG9VRnl5WklsdG9EaUZrcGltaWNYRnhYanJyYmRpY3liWG5lYnBuT2pRb1FPOC8vNzdHQXFGYkN0VTFFRWNpVVJ3dzRZTk9IbnlaT3pkdS9jQm5YTmpxMlNjMnVzMFIrZjBHNmYwV3JKV3B6b25TQWlwMmE2ZGtQZDJsbDlqL2Q3ajhVQ1hMbDFzY1k3eUsvWG5xRkdqVUsyWE41RnFuZkFWYVFHOFhpOWNldW1sMkxselp3Z0dnNUNabVFudDJyV0QzTnhjR0RKa0NIVHIxZzBTRWhLaThnSEtxYTFvem1yMzd0M3czWGZmd1pZdFc2Q3lzaExxNnVyQXNpd29MQ3lFenovL1hKUG40dUtkYzZJMXdOMjZkWVBycnJzT0gzcm9JZkY3V3M5TGhNTmg4UHY5WUJnR3JGdTNEZ29LQ3FDOHZCenE2K3ZCTkUwb0xTMkYxMTU3VGF1dnI3Zk5DeDZ0MER5aW5CQ1crdnpNTTgvRXQ5NTZDenAzN2l6V1BNdlh0YWFtQmpwMDZLQlJ1aldlSDJTT2F0eHVOL2o5ZnRpMGFSTUdBZ0hoTVpiTlg5a1VkWHF2bXEyNnJvdmxaL1IrMGFKRjJLRkRCMUZuY3lFdnBzdmxndHpjWFBqM3YvOXQwd3hsVDdmcUVhVzJCWU5CTENzcnc3NTkreDZUODEya2NWSmZYbm5sbFdoWmxsanRJMS9UU0NTQzgrYk53MWllZWRZTVd4OGMrWG1RbUtZSlBwOFBObTdjQ0pXVmxVSnpJQzJBc3ArUVZpR25rSEs3M1NMUG5xdzVVRXdkcFp3eURBTzJiZHNtdkx4eTJVMXBaMlNtUmlJUjRmSGR0V3NYREI4K1hPdmR1emVNR3pjT1R6NzVaT2pRb1lQd0JIdTlYbEUyWlhhaG5JR2tJY3A3clJ6TitIdytzVTgxOVQrbFB4c3dZSUJ3N0pDbm5xNVpYVjBkZlBUUlI0NGFvZXlzWVcyeDljQ1BwNE5Fam5XVDAwSEprQWxGL3lQQktHOXNEckIvYmtvTndVQkU4SHE5VUY5ZmI2czMzb0hrMUNhNURIcE5TVW1CMU5SVU1hZEZvVEtHWVVBZ0VCQkNNUmdNeHRjNVJ3bjBFS01IZzJWWmtKaVlDSnMzYjhhdVhidmEwb0xoNzNrWkZ5OWVERGZmZkxQVzBOQndoRnZQTUljUk5SeUR2cFAvWWsyYU4yYnl5czRCSnkvemdaaXFicmNia3BLU291cVJ2YWpVZnZtOTdOUWh6K3l4WUNxcmpnL2ltbXV1Y2R3SDJ6Uk5MQzB0eFI0OWVyQXB6QnhieUtzZmlLYThoWEk4SEhHb01wcFF1M3crbjYxY05lR0EzQTVaZ011b251RmpRUmdDMk5jU2E1b0dIVHQyQkZxUExLOS9EZ2FEV0ZOVGcrZWRkeDZxMTVBRkkzTk1RSm9oYVUrcU5xY0tFQ2Nobys0aVI0TEhhZWMyS3VkQUI1amFIbFV3eHhMc3NiU2tveG0xbnp0MjdBai8rTWMvTUJLSlJLVXJLeTh2eDl0dnYxM01YY2ozZzlPMVlnSEpIRldvZzZVNW5rTjE1VU5qZGNobE5YY1F4ZExnMUhJYjAvVFVPTHhqUlNzRTJIK3U2ZW5wTUcvZVBLeXZyN2VaeFpabFlXVmxKZDUrKysyWW1wb2FsK0JycXI4WmhtRU9PMnB3dVByWjYvV0N6K2VEcjcvK1dvUkt5U0ZKcG1uaWlCRWpzREZObm1FWXBsV2phbXl5dHBhWW1BakRody9IdSs2NkMvZnMyV1BMazJoWkZwYVhsK1B5NWNzeE96czdha3JrWUtZeEdJWmhqaGl5RnRldlh6OTQ3TEhIY01tU0pWaFVWQlNWaDlFMFRWeXpaZzFPbURCQmJEUkZqalJPMnNvd1RKdUV6R0Zaa3hzN2RpeEdJaEdiU1d4WkZ1cTZqcXRYcjhiaHc0ZGpabVltQUVRN3RRRHNxMzJZdGdNL3hwaGpHZ3BjbHdQWWRWMkhpb29LcUt5c2hFZ2tBc1hGeGJCeTVVcFl1SENoVmxWVkpWYWFVQkM2MysrSGNEZ3NQTytSU01ReHp5TERNRXlid2VmelFaY3VYV0Q0OE9HWWw1Y25NbCtUK1JzcnAySmpXWFlZaG1IYURFNG1MU1ZnZFpvSGxJT3dDZG43ekRBTTA2WndFbWJ4eEdXcXYrRUVyZ3pETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUVjdlJ6ekhrTG92TFVEek5qdHFLazFTYTlsOUxGWTdXMHY3bUFPanFldDZwSGZBYSs3NFVJOTNhcnQ4RE8yb1NMczBIdW56UFJpT3VEQlVvYldlNmc1eGg0b2pMVXhiTXVlZFUxbHkrNTJ5YWpmMy9PSVpMQWRUWG5OcHF2NldhbCtzY2xyNy9YRTRCSk5USHgzdWNkd1N0QXBoNkhhN1lkU29VV2haRm5nOEh0QjFYV3k5S1dmK2FPN3VjdkhRMU0zV1ZINDZwNDJjNURMVnphRmlMZW8vMFBycC9CdmJXaUJXMitJWmFPcnYxTjhmYkpxcVNDUnlVTDl2S2pOTVUvdEtOelZZbmJadGpmVlozaStiUGpkVi84RzJyeW5pRllaTzJYdGluYXQ2aks3cjhORkhIMmswWHIxZXI5aGp1aTNSS2xKNFVSb2tnSDNaaFNPUkNMaGNMdEIxWFd4TFNiUzFYY2Rvc0RkWENCMHNWSWRsV1FkVnQ1Tm1LWDgrMUpwSFd6SzVuQVFKUGF4aW5VZFREL1BEbVN6V1NkZzVXUkZPRHdENW9VaUNVSjc2YWdzY2NVbEMyb1ZUcHpYV21VMlpMeTNadnNab3F2NURiWVlmYVRPL3RkTlMxeS9XY1FkYmZsT2E5YUhPaVhnd1V5Wk9xTlpjVytLSWE0YnlCVkNGWDJPZDJsSU9scVpvaVJ2a1lNcG9hV0hjM0xhb2c3VXhNL0ZRY0xEbmY2Z2ZWbzM5UDU1N3J5bkJjVGp2MzZibW5KMGdrMWdXZ3JUbmRsTlRBSzJOSTY0WmtnQWtqeFFsMHJRc0MwelRQT0thVDBzSW80TVpNQWQ2ZnZGcXpvZGE4MjJLMXE2NUhzekQ1SEJaTDRlU2VPNFB5dlN0d21ieUFSQ1BPVXcwRlFxZ2NxUnV4Q001RU9JeGZRNjBmUzN0VFdZT0xjMTVXQjNNdFNSbGhqVEV0bXd1dDJsY0xwZHRJbHFkZEZZMy9DSGNibmZVSmo3eGJPcnVWTC9hQnFleURtUXlQRmE3blhEYVlMNnhEYzNqUFVjMWM3TnNPcXNiSWpXbjM1eTgwZXBtOWVyeFR2VTRaWmh1YkF0UUtsL3VCL3EvN0oyUDkxelV0c3FmNVhMVWRzZnF4MWpuRUMveU5nV05lZnhidS9PUmFTYnlSWGNTUms0WG5JU1gvSm1POTNxOWpvSXRGazZDU2hVUU5QQU9SaWlxNTlkVVNJMzhPem9mcHpDZ3BsQUhOdUdVRHAvYTVIYTdoV0NLUjZpNDNXN3dlRHd4QmF2SDR4RjlLTmVwQ2pDcXora1lwM05RZjB2dktiS2h1V0ZEaloyRFhCYWREN1ZIdlM3cXRXb00rajNWcGRZcEMzd3FsL1oxWVk1UzFFSGdwRG1veDNnOEhuSGpPKzFiRWM5ZUZqVFlaVTFEclYrRmJ2UjRibmgxc01nM3VDd1FhVUE0YVdleDRoempIUkErbjgvMk82ZnRNWjMrUjc5Ui85UzJ5ZGRFN25OVjRLc2J2QU9BVFVpcS9lcjMreDNiSlF0dE9sNnRTdzNwYWd5eUtwejZSVVVXaENxeHRPUjRpS1V0QS9DZUxNY01UaHFUa3puU21GYWsvaS9XSmtEeElBZFprNmJnWk1iRlc3NTZIdXJnVnM5TlBRKzFiVTREdHpGaW1hc0pDUW0ydHFsN0JkTm5wOS9HMHFabFRZL2FwMnBiQUkwUGJuV2pKdlZCSWd0TnFrdWx1ZWErWEk5cXFaQzE0ZFNQY2grcEpuNjg5VHVaOTFSWFk5TVo2ditabzRSNE5SM1ZaS0NienVsR2JrN2RWQjc5M21uQXFScFV2TWptajFxZWs4YW5ta3J4bXRQeHRNUEpmSE9hNDJ0S0UybXNMYkVlSHZJMVU0K2xxUTIxVGZUcUpJemtoNmlxVVRzSjBsZzRUUWVRMXFxZXEydzlxQnRJT1puOHpkWG9ZZ20vV1AzTm0xZlphZk1UQjA3ZUs1ZkxaVnNhbFppWUNFT0dETUdCQXdkQ2NuSXlKQ1FrUUhWMU5mejg4OCt3Y2VOR3JieThIRHdlRDVpbUthTHBxYnltUEdJdWx3dDY5ZW9GZ1VBQWtwS1NJQmdNUmoybEE0RUFHSVlCWldWbFVRdmI0MEVPTzFJRDFIMCtIK1RrNUVDZlBuMndVNmRPQUFDd2QrOWUyTFp0bTFaVVZBU2hVRWljQzdWSDEzWEhzaHFEUFA3VXQ3MTY5WUxldlh0alJrWUdKQ1ltd3M2ZE82RzB0RlRidEdrVFdKWUZpQ2dHb1ZNRWdKUG5VbDdQbXBpWUtKWmxCb05CMFZiNnJScUJRTjhuSlNWQklCQ0FsSlFVc2ZvbkVvbEVIVS94Y2ZRN3VVMnBxYW1nNnpvZ29pZ2pscWRWdmRmOGZyL3RHcE9IdGJGRUJqNmZEMHpUdExXSjdzVjRJTUZ1R0FaNHZWN28xNjhmbm5qaWlkQ2pSdy93ZUR3UWlVUmcrL2J0c0hidFdxMm9xQWgwWFdkUDc5R01iRllSUFhyMGdEZmZmQk8zYjkrT3RiVzFXRlpXaHBGSUJCRVI2K3Jxc0s2dURvdUtpdkJmLy9vWFhuYlpaZUxPYTQ1bWtKR1JBYnQzNzhhYW1ob3NMaTdHdlh2M1ltVmxKZTdac3dmTHlzcXdwS1FFUzB0TGNmZnUzYmh6NTA1Y3ZudzVYbnJwcFJqdkpMYXFNZEQ1dFcvZkhsNTY2U1hjdVhNbjd0MjdGMnRxYXJDK3ZoN3I2K3V4dHJZV1MwdExzYkN3RUY5NjZTWE15c29DQU9lNXJhYVF6YTdNekV4WXVIQWhidDY4R2N2THk3R2lvZ0lEZ1FEVzFkVmhUVTBObHBXVjRhNWR1L0RsbDEvR3pwMDdDM1BYYVo1UTFjTGs2M2JsbFZkaWFXa3BGaFVWWVdGaG9iZzJUdWFmMms5ang0N0ZvcUlpM0xseko1YVVsT0NVS1ZPUTV2L1UrVW01UGZUNzNOeGNLQ3dzeEowN2QySlJVUkdTZ0c3c1dzbWE3UExseTdHd3NCQUxDd3R4OXV6WjZLVGx5dTlQTyswMC9PNjc3N0M4dkJ4WHJseUpKNTEwVXRSOTJCaXlCZkQvL3QvL3d4MDdkbUJ4Y1RFMk5EUmdLQlRDWURDSTRYQVlnOEVnRmhjWDQvcjE2L0dCQng3QTVzeUpNbTBFdXBGbGN5TTVPUm5HangrUG9WQUlUZE5FeTdMUXNpd01oOE5ZVVZHQmUvYnN3ZXJxYWpRTUF4RVJEY05BeTdJd1B6OGZjM0p5bWxWL1VsSVNJQ0thcGluS0lYUmRSL2wvaEdWWitNTVBQMkNQSGozaXFrUFdOQk1URStIUGYvNHoxdGJXb3E3cmFKb21CZ0lCM0xObkQrYm41Mk4rZmo3dTNic1hnOEVnbXFZcGhQK01HVE93ZmZ2MkI3Uy9iMlptSmt5ZE9oVXR5OEpRS0lTSWFLdHoyN1p0dUh2M2JneUh3NksvUTZFUS92ZC8vemY2Zkw1R2sxV281cjdiN1lZeFk4YUlmclFzQ3lzcks3RjM3OTdpdDNJNThubjA2OWNQcXF1cmJmMS96ejMzb05QNWtrQlNweSs2ZHUwS1ZDOGlZaXluRktGNmlYLysrV2R4elUzVHhFY2ZmUlF6TXpNQklIcGVGV0NmTUN3c0xNUkFJSUJidDI3RlFZTUdOVXNZZWp3ZXVPaWlpL0RYWDM4VjF3VVJNUktKWUhsNU9lN1pzd2RyYTJ1eHJxNU9mSStJV0ZaV2h1UEhqK2NnVVlranZoenZZQ0VUaDB5b3BLUWtlUERCQi9IT08rOFVac2JHalJ2aHd3OC9oSktTRWlnb0tJQklKQUlkT25TQTNyMTdRNzkrL2VEaWl5K0d0TFEwK1BUVFQ4VWk4M2hOV2JwaEtlUE92SG56d0RSTnNDeExtR1lwS1NuUXBVc1h1T3l5eThBd0RQQjRQREJvMENCWXVIQWhqaGt6UmdzRUFxQnBtaWpETUl5b1BJOHVsd3Y4ZmovTW5Ea1RwMDZkQ3JxdWc4ZmpnUTgvL0JCV3IxNE4zMy8vUGV6YXRVdlROQTI2ZCsrT2d3Y1BodUhEaDhQbzBhTWhGQXJCOU9uVDRZUVRUc0FwVTZabzVlWGxva3k1blFBUTliNWp4NDR3ZCs1Y0hEdDJyT2pqdDk1NkM5YXRXd2RyMXF5QjR1SmlUZE0wNk5hdEd3NFpNZ1RPT09NTUdEVnFGUGo5ZnBnL2Z6NE1IRGdRSDN6d1FhMnNyRXlZaUxLWkt5L2hRa1RSTGxselRFMU5oUmRmZkJHdnUrNDZyYVNreE5ZLzFLYk16RXg0OWRWWE1UMDlYZHdUOGh5bmJHSnJtaWF1TTlWUDk0cXFzZEoxa2Y4dnY2ZnBCL3FPekdxNmx0T21UWU9FaEFTY08zZXVWbEpTWXF1VEhoSTBMU0RYcXk1em8xYzVJNHpMNVlJcnJyZ0MvL2EzdjBHblRwMUExM1dvcjYrSC8vdS8vNFBmZnZzTnRtM2JKcVp2ZXZmdURUMTc5b1FSSTBaQVhsNGVGQllXUWxGUmtlMWVsdTk1RHBwdWc2ZzM3dzAzM0lDVmxaWGk2VHh2M2p3OC92ampiWnFCUE1HZGxwWUdJMGVPeERGanhtQlNVaElBTk0vTGxwS1NBcVpwaXFkdVJrWUdBT3lmdFBmNy9aQ1FrQUFkT25TQW9VT0hZaUFRRUUvbjR1SmlQUHZzczhYVFdZMnRVNWsxYXhaYWxpVzB6SWtUSjJLSERoMml2T0gwbDVtWkNRODg4QUEyTkRRZ0ltSTRITVpISG5uRVVSdHdNbVVCQUtaT25ZcUJRQUF0eThLYW1ocWNNV01HZHVqUUllbzNKSGl5c3JMZ3BwdHVFbHA1SUJEQW1UTm5vcXlCT1ptZDh1Yy8vdkdQUXJzekRBTU53MERUTklXV3B6cTZORTJERjE1NFFSeEw1MnFhSms2Yk5pMUswMnBNNCtyV3JSdWdoSk5XNi9TZVlpdlhyVnNuN2oyNlZvRkFBSjk5OWxsTVNVa0JnR2d6ZWNlT0hZaUltSitmajBPR0RCSHRqYVhGeTFwc1ZWV1YwSUtycXFwdzhPREJvaDdaSys5eXVTQWhJUUZPUFBGRWVQVFJSN0ZuejU3Q29STXJIT3Rnblc3TVlVWWVaRGs1T2ZEVlYxK0pBZkgyMjI5alVsSlNWREF0UUh5ZTQzaUVZbHBhR3BCSlpab21wcWFtUnYxV0R0Vlp0R2lSTU51cnFxcHNwb284U05TNisvVHBBNy84OG9zdzMvNzYxNytpR3J5ci9wWmVQL2pnQTJFK05UUTBvT3g5VmIzT2hLWnBrSktTQWhVVkZVSzR2UDMyMjZqK1RnMVFkN3Zka0ppWUNETm56aFRUQkpzMmJjTCsvZnMzZW42eGhLRnM5cG1taVgzNjlMSDl6dVB4d1BqeDQ3R3lzaElOdzhCSUpDS0VQeUxpZmZmZGQwaUZvZnpuOC9sZ3c0WU5HQWdFTUJnTTR2YnQyOUd5TE5FUHp6enpUTlFEOTl4eno4WGk0bUswTEFzM2I5Nk1nd2NQUnZrK3BUNVYrOW5uODhHU0pVc3dFb21nWVJoWVgxOXZtd2VVNTBHOVhxLzRMZVVKamNXeExBVGJmS0NSYk9ha3BLVEFhYWVkQmk2WEMwS2hFQ3hac2dUQzRiQzR3T0Z3V0FTOXV0MXVTRXBLZ29TRUJNakt5b0xrNUdUSXlzcUN0TFEwRVNZVGo1bEFwaElBUURnY0ZnTmVEck9oLzE5NDRZVjQzbm5uQ1ZPdHBxWUdkdTdjS2NxUzg4Q3BkWjl6emptWWw1Y0htcVpCZm40K3JGeTVVaVN6b0hiSWJaTDc1cXFycnRLb1BRa0pDZkNmLy9tZktIdmVBY0NXRklQTXZxRkRoMkpXVnBid3h0NXh4eDJhYkJiS3BpME5NdE0wUWRkMVdMWnNHZXpac3djUUVYcjA2QUVEQnc1RThvaXI3VzJNSDMvOEVhWk1tU0s4L0JzMmJNRGpqejllUE5neU1qTGd4aHR2aE9Ua1pIQzVYUERVVTAvQnh4OS9MSzVIYytvNkVNZzh4dDg5ejZGUVNGZ2V0OXh5Qzd6NjZxdkNtM3pycmJmQ3M4OCthOVBtdytHd01MWHBuR1N2Ti9XcG5MaUVndUI3OSs0dHdvcW1UNTh1VEhTZnp3ZUlLTXhyT29idTdhU2tKTWpNeklSMjdkcEJjbkt5N1h5T1ZVRUljQlRNR1pLdytWMlRRWUI5RnpRU2ljQy8vLzF2VFJZV2RGTysrZWFiNlBQNVJMWU5DaUIydTkxUVdsb0tEenp3Z0xacjE2NjQ2ZytId3lLc0lURXhFV2JObW9VMEQwUmFoZHZ0aG5idDJzSGd3WU9oYTlldUl1U2lvYUVCZnYzMVY0M2FoMHFJQjMwUHNNK0prWktTQXBabFFYRnhNV3pldkZrSUpnQjdLaWoxbk1QaE1HemJ0ZzM2OWVzSExwY0x6anZ2UEhqenpUZkZmSlZhRjUzUGlCRWp3TElzOFBsOFVGUlVCRFRuUllLSkhnUXVsMHVFb25pOVhqQU1BMzc4OFVldHRMUVV1M2J0Q242L0g5TFMwbXkvd3pqRFJyS3lzdUMxMTE3VFRqenhSSncwYVJLNDNXNllNMmNPWG4vOTlWbzRISVo1OCtiaDJXZWZEWlpsd1U4Ly9RUXZ2dmlpOXRoamp5SCtuazNsY0F4dUVsaCt2eC84ZnI4dFljSE5OOStzVlZaVzR0U3BVeUVTaWNDRUNSUEE1L1BoZmZmZHB4VVZGVUZDUW9JSXJTRkJKNGZoeUE4dHVsNlJTQVQ2OXUwcjdnZVh5d1g1K2ZtaVBTUVVYUzRYWEhubGxYajExVmVEMStzRlJCUjFVU2paM1hmZnJXM2N1Rkg4TmxaNDJyRkFteGVHOG9SdlltS2l1R0hvQnZYNWZCQ0pSR3dYOXB4enpvSE9uVHVEcnV2aUpnSFlkL1A5OXR0dmtKS1NJcjV2eW9sQ1QzUFN2RzY2NlNZaGhGUm5BRUZDY3N5WU1WcHRiUzBBMkRVTUVvcjBXVzRMeFFsR0loRnhRMVBiU2R1UUoveEpLRk84R1QwRVpJRWtPMDFJbzlBMERkTFQwMjBUK2pTWUlwR0lUWkJTV1NRVXFRMDBjSDArWDVTamh2cXNxZjZ0cmEyRit2cDZtRHQzcmpaczJEQWNNR0FBWEhUUlJUQisvSGlzcXFxQzhlUEhpL2FlZnZycEdtbE5jcDhlU3VnY2FQNHRIQTdiQXVVdHk0Sjc3NzFYeThqSXdCdHV1QUVNdzRDcnI3NGFFQkd2dSs0NmphNFZQYWdwNUlydUF5cUQ3Z242VEhHSWRJL1I5M0k2TFVTRWJ0MjZ3V1dYWFNiYXF6ck9aczJhaFlpb0Fld1hnTWNxYmQ1TWx1Zk15c3ZMTmZJMGhzTmh1T1NTUzlCcGo0MWZmdmtGVnE5ZURkOTg4dzBzWDc0Y3RtL2ZicnV4M0c0MzZyb2VsemRaenVObUdBWlVWbGJDbmoxN1lQZnUzVkJXVmdiRnhjVkFaVm1XQllXRmhiQnc0VUxvM3IyN3RtM2JOc2U1UGhWZDE0VzViMWtXOU9qUkEvcjI3WXR5KzBpSXlZS1FTRWhJZ083ZHU0UFg2d1hUTk9ITEw3KzBwV3BYaFFlVnNXclZLbEZ1VmxZV1pHZG5DNjJEaEN3ZEQ3QS9kTVRqOGNEQWdRT3hVNmRPNFBQNVFOZDFLQzh2anpxL2VQcVhoUEQyN2R2aHlTZWZGSjdrbVRObndxSkZpMERUTktpcnE0UExMcnNNUXFHUTBMRFVQNW1XSFBDa0JacW1DYUZRU016dkJRSUIwWGNBQUpNbVRkTGVmdnR0Y2I5T25EZ1JYbi85ZGN6SnlSSFhsc3hadVgxeXNEeVp5eTZYQzJwcWFpQVVDZ252OHRDaFF3RUFoQWNhWU45MUxTMHRoVldyVnNFWFgzd0JxMWF0Z2kxYnRvaHJweTRxb0xMcC9iRXNHTnNrOHMyZWs1TUQzMzc3TFpKM2Qvbnk1ZGkxYTllb1kybWVoTXlhdSsrK1cweVk1K2ZuWTY5ZXZlSmVPNXlTa2lMaURFM1R4REZqeHVEbzBhTng1TWlSZU9HRkYrSUZGMXlBNzc3N3JuQjhyRm16Qm84NzdyaW9DWEZxbi9wSzcwODk5VlRjdW5XcmNDYk1uVHNYNDAwazhmVFRUNHY2YTJ0ck1UVTFOZTZBOG5BNExCd0FjK2JNRVVIRWFueWQ3TFJKU1VtQnVYUG5ZakFZUk11eThKZGZmc0dUVGpvSlplMDFYbS95NnRXclVlNm52L3psTDhKVGk0Z1lDb1Z3M3J4NVNOYzBOVFVWM243N2JmSDcrKysvUDhycDB4Z0g2azBHMkhjL2taTXJHQXppUlJkZGhBRDdIV01KQ1FudzlOTlBJOTB2dXE3ajExOS9qWFYxZFdpYUp1N1lzUVBQTys4OGxQdFNyb2VtWEFEMmFmTkxsaXdSbnZiUzBsTE15OHVMT2grZnp3ZUppWWxpVHZlbW0yNFM4YW1JaU1PSEQwZTUzTWJPajJrRDBFWHplRHp3d0FNUENBOWJLQlRDVjE1NUJaM1NKZEVONm5hNzRiNzc3a1BFZmVFSlc3WnN3Zjc5KzhkZGQzSnlNdEJ2RVJIVDB0S2k2anJoaEJQZzExOS9GZDdPdi8vOTcrSTQ5VmhxazNwdW1xYkJQLzd4RDR4RUlxanJPZ1lDQWJ6bW1tdEVVSEFzN3JyckxsdkE3YTIzM3Rya2IyUWVmdmhoTWVCcWEydHgwcVJKWWw1V2JxdThVbWI4K1BGWVhWMk5wbWxpTUJqRVo1OTkxaVpVMU1HdWxqZDY5R2doN0w3ODhrdWJBTlkwRFo1NTVobmJ3NHVDbWowZUQvaDhQbmpublhkc1huZjVmTlIrVmg5SWVYbDVRaGhhbGhVbERPVzJxbmc4SHZqcHA1OFFFYkcrdmg1SGpCZ1IxZGVabVpudy9QUFBvNjdySXFxQTJycHQyelk4Ly96em83emZUaGwzdkY0djVPYm1pbnZQTUF3c0tDZ1FEMW9aT1NHRWt6QnM2cnlZTmdiZEFBa0pDZkRwcDUrS0FZeTRMOXArMUtoUjJLZFBIK2pTcFF2MDd0MGJ1bmJ0Q3NjZGR4eWNmdnJwU0Rjd0RTNDVCcXNwTWpJeWdHN3NZREFvWXZBSWV1cE9uanhaaFB6b3VvNjMzSEpMbEFZQUVIdHhQZzNLclZ1M29tRVlHQTZIRVJIeG5YZmV3VUdEQm1IMzd0MGhPenNic3JPem9XdlhydEMzYjE5WXVIQ2gwRmlEd1NBdVhyd1lPM2JzYUd0YlUrVGw1Y0hISDMrTWhtRUlUZStGRjE3QVBuMzZRTGR1M1NBckt3czZkdXdJWGJ0MmhVR0RCdUc3Nzc1cjAzeldybDNyYUd1cFFsUm16Smd4b3A4Kysrd3ptL0QxZXIzUXUzZHYrT0tMTDNEejVzM1lybDA3MGNma2tYM25uWGVFMWtoeGhuTFlpWk1RSmtIYnNXTkhRRVJ4cmVJUmh2TEQrS2VmZnNKSUpJS0JRQUF2dnZoaXNmcEYvazFxYWlxOC8vNzc0djRrU2twSzhPS0xMMGFLV1hTcVQwMklNWG55WkF5SHcyTDFVME5EQXo3ODhNUFlwMDhmNk5XckYzVHAwZ1c2ZCs4T09UazVjUGJaWitOYmI3MGw2alZORTRjTkd5WXNqTWFtRnBnMmdqcXcycmR2RHkrLy9ES0d3MkcwTEV2RXFEVTBOR0JoWVNGdTNyelp0azZaYm83UzBsSjgrT0dIeFFDTGg5L0RHTVRnVFU1T2p2b3RQWm1mZSs0NVJFUng4OTU0NDQwb2EzN3FPY2thSTUxamNuSXkvTy8vL2krV2xwYUtRV1FZQm03ZHVoVlhybHlKWDMzMUZlN1lzVU1JSTlNMHNhYW1CaDk2NkNHYkdkWFlFak9WL3YzN3cvejU4MjFML0F6RHdQejhmRnkyYkJtdVhyMGE4L1B6UlV4Z09Cekc3ZHUzNDNQUFBTY0VvV3FHT1FrVU90ZlJvMGVMNjdKMDZWS2s4QkRDNC9GQVZsWVdkT25TUlh3bS9INC92UDMyMitLYVB2VFFRMUhDbUV4R2VYMDR6Y3ZsNU9RSVlXaFpGanIxazJwS3l0ZUlIcXpoY0JqUFAvOThsSStWajB0UFQ0ZEZpeGFKZXhRUnNiQ3dFQys4OE1LWWZlWlVmMUpTRXR4MjIyMWlDYWE4N0xPMHRCUzNiOStPaFlXRkdBcUZ4UDhvZ0g3cDBxV1ltNXNiOCtIQXRER2NCclRmNzRmVTFGUVlQWG8wcmxpeEl1b0pMR3N1aVB1Q2dxZE5tNFpEaHc0VlFiSHhrcEdSWVF1NlZqVkRtWVNFQk50OFZtVmxKVjV6elRWaXdNZ3JZOVNnYlptMHREUVlObXdZenBrekIzZnYzaDExYnRTV25UdDM0b01QUG9qRGh3OUhPWjZzdWFtYlNCQ05HREVDNTgrZmoyVmxaVGJ0Z2dZWTRqNVQ3Lzc3NzhkVFRqbEZKS05RcHlib3MxT3VRcmZiRFdQSGpoV0M5ZjMzM3hjYXRPeWdVZHRHeHlRbEpjRjc3NzBuVnFEY2Q5OTlLQ2VvY0xwZlpJR1htNXNMdFA0NkhtRkkrSHcrOFBsOHNIYnRXcUdoWFhMSkpXS2VWRTB5NGZWNklTc3JDMTUrK1dWeEg1YVVsT0RsbDE5dVcyVkQ5VGs5TEtudkVoSVNZTWlRSWZqY2M4OWhUVTJORU9UeSttNTZXTmZWMWVHQ0JRdnd2UFBPaTFxOXBKN2pzYVlkdHZtemxjTTE1SFdiQVB0REJUUk5nNkZEaCtKcHA1MEdQWHIwZ05yYVdpZ3VMb1pmZnZrRmZ2NzVaeTBTaWRnOHNmSjY0SGhTZUkwYk53NnJxNnNoSVNFQi92blBmMnB5T2lieTRGSTVmcjhmc3JPelFkZDE4UHY5RUF3R29heXNUSGdrQWZiZDNPU1pwTy9VSGNoa0V6czVPUm02ZCsrTzNidDNCOHV5WVB2MjdkcXVYYnNnRUFpSTQ5WFlQdGxyMk5UNXFXRXhMcGNMMHRQVElTOHZEN3QyN1FxUlNBU0tpb3EwclZ1M2dtRVk0amlLQVkxVmg5d20rWDIzYnQzZzlOTlBSMTNYWWRldVhmREREejlvVkQ4ZHA0YnB5RzA5OWRSVHNVdVhMdUJ5dVdETGxpM3d5eSsvYUdxOUZLSWtwODJpT2NkTEw3MFU2K3ZySVRVMUZkNTg4MDBONC9TcSt2MSs2Tnk1TXlRa0pFQTRISWFLaWdxb3JhMk51ZlliWUovd1B1T01NekE1T1JsQ29SQjgvLzMzV20xdHJRaGZvcnJsT01QRyt0SHI5VUt2WHIzZzlOTlBGNDdBb3FJaTJMcDFLL3o2NjY5YWFXbHAxQ2J2TkU3a2U1QnBvelNsNlRpWlpoU1BGc3RaRWE4M1dhMjdxZldrcENVNHJhK045WlNXVFVRNWppNVdPK1NKZC9KYXk5clZnU1N4SmRUVVQ2cUoxVmpiVkEweFZ2dGwxRzBhbk9xVkhRU3gwb2JGeWxyalZKNjZpcWd4bk14S3ArdW9KbTJWUGNYMFA5a3lvTitvWm5aajV5WDN2MnhpcSthMldrNnNlNVZwZ3poTk1NdmVZa0tkZTZMdkdqT2Y0aVZXbXZ0WVhtS24vOHMzcm14YUFkalhWYXZ0ZFVvTkZVdmdPSGtvbTBJZVBQS0R3bW0vRXZtOTB6d2gvVlplSFNJUFl2azlIU3YzclNvd2lLYXVYNnk1c0ZnUHMxaGx4cXJYcVM1NndNbk9IN1Y5alRuUm5CNlFzb05EWGFzc2x5dTNRL1pBTzVXbkVtdXVrbW5seURlTStzU1RKNnpWNzVzYUtNMFJoRTQzbU5NTlJkODVEVGluOWpqVm9aWVhTek9nMzhqOTR5Um9ta0oxRmpUMjN1bWhJczhQT3JVLzFweWMrcDBxU0p5Y1MzSzhvM3ArVHYzbmRLL0UrbjBzMVA2UHBYVTdoWGM1WFF1bnBDSk5PYnZVKzBydGw4WTBkcWY3c3pGUFA4TXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13VEN2aS93TXZ0T1Q3WHl4MmZBQUFBQUJKUlU1RXJrSmdnZz09IiBhbHQ9IlImYW1wO0ogR3Jvb21pbmciIGNsYXNzPSJsb2dvLWltZyI+CiAgPC9kaXY+CgogIDxidXR0b24gY2xhc3M9Im9wdCIgaWQ9ImJvb2tCdG4iPgogICAgPGRpdiBjbGFzcz0ib3B0LWljb24iPjxzdmcgd2lkdGg9IjM2IiBoZWlnaHQ9IjM2IiB2aWV3Qm94PSIwIDAgMjQgMjQiIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyI+PHJlY3Qgd2lkdGg9IjI0IiBoZWlnaHQ9IjI0IiByeD0iNiIgZmlsbD0icmdiYSgyNTUsMjU1LDI1NSwuMDgpIi8+PHJlY3QgeD0iNSIgeT0iNyIgd2lkdGg9IjE0IiBoZWlnaHQ9IjEzIiByeD0iMS41IiBmaWxsPSJub25lIiBzdHJva2U9InJnYmEoMjU1LDI1NSwyNTUsLjU1KSIgc3Ryb2tlLXdpZHRoPSIxLjUiLz48cGF0aCBkPSJNOCA1djRNMTYgNXY0TTUgMTFoMTQiIHN0cm9rZT0icmdiYSgyNTUsMjU1LDI1NSwuNTUpIiBzdHJva2Utd2lkdGg9IjEuNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+PGNpcmNsZSBjeD0iOC41IiBjeT0iMTUiIHI9IjEiIGZpbGw9InJnYmEoMjU1LDI1NSwyNTUsLjU1KSIvPjxjaXJjbGUgY3g9IjEyIiBjeT0iMTUiIHI9IjEiIGZpbGw9InJnYmEoMjU1LDI1NSwyNTUsLjU1KSIvPjxjaXJjbGUgY3g9IjE1LjUiIGN5PSIxNSIgcj0iMSIgZmlsbD0icmdiYSgyNTUsMjU1LDI1NSwuNTUpIi8+PC9zdmc+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJvcHQtdGV4dCI+PGRpdiBjbGFzcz0ib3B0LXRpdGxlIG9wdC10aXRsZS1ib29rIiBkYXRhLWkxOG49ImJvb2tfb25saW5lIj5Cb29rIE9ubGluZTwvZGl2PjxkaXYgY2xhc3M9Im9wdC1oYW5kbGUgb3B0LWhhbmRsZS1ib29rIiBkYXRhLWkxOG49ImJvb2tfZmxvdyI+0J/QvtGA0L7QtNCwIOKGkiDQo9GB0LvRg9Cz0LAg4oaSINCc0LDRgdGC0LXRgCDihpIg0JLRgNC10LzRjzwvZGl2PjwvZGl2PgogICAgPHNwYW4gY2xhc3M9Im9wdC1hcnJvdyI+4oaSPC9zcGFuPgogIDwvYnV0dG9uPgogIDxkaXYgY2xhc3M9ImRpdmlkZXIiPjxzcGFuIGRhdGEtaTE4bj0ib3JfY29udGFjdCI+b3IgY29udGFjdCB1czwvc3Bhbj48L2Rpdj4KICA8YSBocmVmPSJodHRwczovL3d3dy5pbnN0YWdyYW0uY29tL3JqX2dyb29taW5nP2lnc2g9TVd4bWRITnFjWEZrYW5OdmJRPT0iIHRhcmdldD0iX2JsYW5rIiBjbGFzcz0ib3B0Ij4KICAgIDxkaXYgY2xhc3M9Im9wdC1pY29uIj48aW1nIHNyYz0iZGF0YTppbWFnZS9wbmc7YmFzZTY0LGlWQk9SdzBLR2dvQUFBQU5TVWhFVWdBQUFLQUFBQUNnQ0FZQUFBQ0x6MmN0QUFDUTBFbEVRVlI0bk95OWVieHRWMVVsUE1aY2U1OXo3MzM5Uzk4VEFnUVNrRjZra1FRUmFWVEEwc1FxL0VwTFJkQ3lLY3UyYk9BbFlPbFhqZFdvV1BhV1dnb2tLbFpKcXlBSkpRZ29TQUJwMDVQMnZlUjF0em5uN0wzV0hOOGZjNjF6SHhBUUZCWDlPRDh1TC9mZWM4L1paKys1WnpQR21ITUNYM2g4NGZHRnh4Y2VYM2g4NGZHRnh4Y2VYM2g4NGZHRnh4Y2VYM2g4NGZHRnh4Y2VYM2g4NGZHRnh6LzFCLytoRCtEejdNRURPTUNMOFFIK0ZRN3lZbnluQU9BeVhLWlBlTjRuZkg4RmdTdVczMTJOcTluKzYyVTR5Rk54cWk3Q1Jib1NWK3FULy9ZTGovKy9QbmdWTGtzSGNFa25IREJCZnk4M295QUtzcXR3VlRxQVM3b0RPR0IvSCsvNytmcjQvNDBIRk1DcmNaa0JsK0V5WE9ZRVA5a1RuWWVWN3p6NjlJdDhvei9yblA2azNXZjJlMDZGNjFTbGRMYTc3UnRMM2kxWlQwdEdFS1FiNkY3RUFtRE1XWkpoYzJMcExtYytmQyszN3I1bGNjL054N20rdnJMYTNmbWJGN3orZXJ3TFc1OThiR0g4VitEU0JGenFWK0pLLzdzL0k1OGZqMy9TQmxpOWkxMkJLOG9uR3R3VFRycHcxNlhEUTU1MFFUbjV6RW1hZk5rTXVQaGs3Tnl4TDYrZU5mRitkY29KT2lZNENRTkFHQXdkaWtVVUpRd0NVZUFnREJCUUZLWWtFQkloZDh3eFlNR0NoUTBiaDdCMTU5MWMzNXJJYmh4OGZPK0hkTnRINW1uemZWZHZ2dkY5Sng0YkFUZ08yQlc0eHE3QXRZWC9oTVAyUHprRERLTzd4bDZLdDJRLzRicDl3LzR2M24wcXpucnFRL01wRHowcDczamE2ZGh6YnVjOEwzVTllaWFZZ0JIQWdqN0NXRndpa0pSSXVjTzhPQW9CRnlBWEJKQ0praWdKUmxEZVV5QmhJR0FHd1dnT0tSdnBUZ2w5ejQ0WllhUUxqamptbSt0MzR0anRtK25vaCsvT3gxNDNhUGFXL3pIOHdZY0JMTDJnSUY2QlM5T1Z1TGJnbjVneC9sTXhRQjdBSmVrS1hQTnhudTU3OW43MUY1MDc3di9TYzlQZVMwOHFhMCtZV0gvbW5qSkY1d2taam13bDUwUVZGNVNMbFRMU2MwRUI2QkFFb0lPNWQwWWxFbDBIbXFrNDZBQmhGRWw1RmgzeXpBSVUwa2NDcFRDQmNIUWdER25TeVRvVG1NUmszcWVKNEFReUpvYU9oUVZ6alZqa1llTVladmNlbk56N2pvOE1ONy8rWGQxMXIzbnY1bzBIMjJjNmdBTWRnSDh5WWZvZnRRRWV3QUc3R0IvZzViaTZ0Sjk5NDc2blhQelE0ZXlubjQ4OXp6c051eTQ2dWR1NW1vc2p1OE1OUTVGVFkrR1ljNEtYTUxRa3g5cVUzWjRWcnA2OUU5TXo5dmpLL3QxTXUxWXgzVFhSWk84VWFjY1UxbmRrbjBTYXlRUVhSQkFDNENyUVVEUnVEUmpXUjh5UHpMRTRNcmZGOFpsbWQyOWc2NjRaRnZjc1ZPWUZ2dVdVUUdPSE5PMWtscFQ2VGlhV1hEVHBVNDh1R1RiS2dEdDArSjUxSFgvNzdiemo5LzU3OTc5ZWhTTTRCb1JYdkJ5WDI5VzQydkdQMkN2K296VEF5M0JadWdwWGJSY1NKMkhYajVabmY4M0Y1ZFQvNXh6dGUveHBaZmRPeUxGQUtlbzRMSEpPUGhTNkx5ekRqWHNtV0RsOXQ5WWVkaHAybkg2eTdUeHZqNlpuN0VhM3NvSTA2Y09rSFBMNVNNOHV6MDVsZ1NNQWp6QU1Ba2dFSklpRUpGaWhrSXcyTVhGcVNIMHk5Q1luVUxKcm5BMFlqbS9oMkMzcm5CMlpZK09PNHpqNmthUFlPamhET2U0MFVKUEpSR21sRXlaV2JEUVJhYnBTT2l5MHdNMDhkTnZ0ZnNmdlhwYys4TW8vWEwvMjdRQkFFRitIcjB2L1dBM3hINVVCSHNBQnV3SlhxQm5lOSt4K3htUFBzLzNmZnY2NDcya1grUDV6OW1xQ0dYeVlzM2dwSlkzRGpCa2pOVWxjdVdBWDFwNXdwdmJmL3h6YmZjR3BUTHRYbFdEQVJtYmVHdUZiV1NwT2R3RXV5Q2tDaE10RlVHSVVHdzRLRUNnb0dTWElBTHBEWGh4R1VnN0pCWklVVVkwVlFtZElIY0ZKd21SdEFrdWRGc1BJeGVZTTk5NTRMKzc5NkdIZWM5MWhiQjZhT1ViNnBPdHBVL1BlSmtwRGwrbStadGJqRHQ2OXVGbDMvdEg3NXpmOHpPK1gxL3dKQUQvQkVNdW5PNGVmYjQ5L0ZBWVlobmN4aWNzTEFQeTcvYy85aWtmbXMvN05HWG50R1NkenpUZ1d5RGlNeVcyY3oxTXVDMkdhdU9NQkozUG5ZOC96ZlJlZnBaV3o5b0JHYWpOVEcxbGxQbExaWVV4MEFMU29OUVhLM1FHQWNBZ09lZGdiSVlJaXBEaHZSV29sTVNVQ3BBT2lDRklrNG1lMXFvVWdTSEpLa0FwSWg1Q0lORW5zMWlhd3FTRzdZLzNRRnU2OThaamZldDNkUEhMVE1YVURiTWVPVmFFalZGUU1uUkZkT3FSak9PZ0gvL1EydS9ubi9zZm03L3d1Z0NMSXJzQVYrTWVTSTM1ZUc2QUFBbGRaTTd3ZjNQSGxUNzJFOS91aE0zREtWK3oxS2VZWVBDY1dYMlFPdzh5Y28wM08yNmY5ano4Zis3LzRmS3lldHBjWTNNdlJ1ZkpXaG9yVFNBck40RWdXd0l2TEpZVFJBTVVWWmE0UTNneUNCQkFrb3VvbEFNamhUb1NGaFlFNlNJU1BKT0hoQVJsdkphbitxWUhoc3dDWmdFTElDWmVRRXBCV2UvUTdPNmlqanQ2eG9UcytlSmZkZmQwUmJCMmFZNUo2WXBwQWFrRHVyT2VrSzkwQ04raU90NzQzZi9pbmZudnJGYStKYzNkVkFpNzN6M2NJNS9QV0FLL0NaYWtWRjkrNytvVEhYTm85NU1mT0xmdWZlenAyNGxoWitORExTM2FPOCtQUWJ1UE94NTNEMDU1OElYYmQvd3hoQkxVK01CK2Zpd1VPR0dRTXl5bE9sQ1dVQXBacVpPNm9JQjdjblJEaGd1Q0JBMllKQkVHUkRnbE9lcVIvbENnbllBQkFFb0FjQUFzSnhpbDJpOVFSSW1TQUtYeWpoL0dDdEtpN0NVaGg4REJEdjlKcnNzT3NsS0tETng3aFRlKzVXMGR1bVJFTFY3K1NuS1N2WU9xZDlkTjdmUjAzNmNiWC8zbjNGei85bW1QWHZwRUFYbm5DZWZ4OGZIemVHV0JjZ2dNa3J2U0g3WG5Zdm44OVBPajd2aWlkK2NQbmEzKy9VWVp4TkMvRHFINGMxMW4yR0UvNTJvZm8xTWMrR0N2NzkxREhaeHp1M1pJUERyTkVBNlRzS3JsYVJZR3NsTENXSXNwZGl0UTlqRkl1aDZIOVRJQllvTUJhb3ZLUW9rWVJJcXhTVWRBcVRBdWdVVUQ0T2xsNFNoaGs4Uk5RRkFBTGVGdWlJbFRESUlvRUpOSkJFa1p6dVNEQkVqaFo2OFZFSER1MnhkdXVPNlE3UG5BWWVRN2Z0WE1LQ1NNOHBhbjEvY0h1Ym4xZ2NlTXYvWmZOWC9neEFQZldORldmajk3dzg4b0FUL1I2LzJudmMvN2xZOGV6WDNLaDl0MXY1cU1HbE9KU3QxZ2MxMkszNFpTdnVCaW5QK01Sbk81YXhYakxNZm42SW1JZENUaUJVcUFHR2hkVVF4T3djRU54U1hHcHF6ZGMvbDZNd2lLOEZZQkkyUUE2a1VsNVJQQmFjalo3RERSR05mYWFTUzZpR0VGQVZ0TkxVREpRTUZFZ1dHc2JBSlZ5RVphd0RzQkVGMlFrS2JCNmFhRHJFN3RwNTF1YkEyOTQzeUVjdk9HNGtwTFNTcGVLYTFqREJKMmx5VWZLUjIvNm9IL2dSMzUxODVXdkJBSTkrSHdyVWo0dkREQzgzbVZHWEYwdVBPbWNNMzlzZU5KL2YwSTU1K3RXczdEVjVjR1p1c1htaG8ycm8vWTg1eUU0NDJtUHdjcnFHdktoZFpUMUFSb0RHMkVSNEtBWENPNVVxVGwvVmhpaUN4eEVsQW96TzRraXlRVVhTQm04ZXErYTVwRXVkMFhLWnFWV3VCSkJ3bEZ6T2hvRnFaWEh0UENVeWhHUVpZemlCb0FTQUJDbUpNREQ1eEdBTStxY0ZOOUtrQmdHRzVZSzFZcWFLRVFwTHV1SWxiV2VHNXNMM1hqZFFSeStmY2F1NzVVbWtMTHlsQ3VURVROOEtILzBWYjh5ZmZsMzMzYjR0dHNQNEVCM0phNzh2R0ZVL3NFTjhBQU9XS3ZZZm1UUE03N3V5ZVhjbjMxME9lMzByYkkxREJPWVpuUGJLc2U1ZHNrRGVjYS9mSUpXVDk4SHYrVTQ4OUdaYUJZRTdPWUlqWVZlQUJaQkR0QkZMeTZWTUVBdmxUTWJCQlRBUFV3bmpGV3NLV0dFUzFmRUt3ZUNmU005dkNIaWllR2xDc2tLUk1zbEdDaFo1SDJTd0V5QXRZQTJ5R25oNFV5TnNJT0lZSk5yb1EzUVpSSE5QWXhUUUlWejBLd21QTDNjVllxNHNqT2huNXJ1dVhNVE4xeDNHTU5td2NwcXdvamlLK09rN1BHVjZRZHh3MTJ2SzMvK2JhOGIvdURWQnNPTDRIYmxDWFRmWi9nZ1BzZUcrdzlxZ0Mza3ZtRGZvL2M4TTEvNFV4Zm8xTzg0YmJHS2Rac1BJdnBoZnBUam1aMmYrVysvbkNjLzdFS09keHpEZU0rbWtwUElnQllGR0xNMGR5azdrUXVWdzBHaGlDcUNGNWR5VkFxbFNCekRBQ3VnM0twWnFBUnpMSm9nMEdzOUFJOHdLYW5taGdRUTNxcDlxK2JFd1dheDRkd2NodkNHOEFTSjhSd1JNS1FvckEwTTJKdFJUek04SlFDQVJtZjlNekh3SHBKbFdaakxTQUlHeWFYcE5MRUF1dlVqUjNEM3h6YlJtYWxMSWtZTU83QzJjaWdkOW5lVjkvemt5MmEvK2xJQXcrZERTUDRITThBMzQ1THVLYmcyWDdidndvZDl4L2pZMy82aWZOYkQxc3U4NUVUWU1ITG1oN2puMlkvVTZkLytkT3NXUXI3aEhuZ3BRSFpwN3NTaXdNY0NEUTRVRjBhSGlzSUFNNGppOHVvTjVaRS9sU0ttckRDUVVrc0FFTjRxWWhjY0JsZUZsOUVDS3lISTRJeEx6Nmlvd3lZRFZwRVlCUzFGSUhKQUZZQm1CSWhTaXhEU2xpV0J5TURVQ1pBZEc4TFQwbE1RY2d2M1I2UW9XSXhVQ2NBUDFkVXUvOU1GR2pEZE1kSHhZd3Q5OUFPSE9jNmNPMVo3akY1OHFna05zdmVPNzNuekwrTFh2dm11K2RGYkR1Q1M3a3BjbXovbnJ1MHpmUHhER0NCVnE5d2YyLzNVcjM5V3VkOHZQTXhQM251b2JJN291cTVzSFlHZkFwNzIvVit0dlU5NENQS05oNkI3NThUbzBHS1U1aGxZaUQ0VVlDelFLR2lVTUJiQVJSWEpSOUNMaE9JaGl5cVFpc01kcENOeXczcmQ1WVM3cThVNUNaQVkrRjdGU05RS1ZiRlZKUEJxbFlJb1doaXc2RFZVaHJzcTlTVkl5Q2duVlRGSWc5ZUlhd3cvSmdLMG11ZVJMb1hyczRZLzFseVNTOHl3MlF1OTJZNEpMaU5GN3lhRUcvQ3hHOWQ1N05BY2ZSOHd6MFRkWWxVckt4L1FCKzU0YlhuOTg2Nlp2LzNhYW9UL0lIbmgzNnNCSGdEc0NrUkUrNm5kei9yeHIvYnpYM3JXc0ZLT3AzRWM1U3ZEL0U1TXYvekJPdlBGWDgvcHZDRGZmQytRQlJ3Zm9JMEZ0TWpDUE5OSFFBdUhjb0ZHRnpPa0xIb3VRQlk4azNEQnE2R3BtT1Mxbk0xT0ZsU29CR2hXMGE2bnE2bHBBdWRyOGRVci9jZWxFUnJ6RXF1Sm5KQTFmeE1zZ09zTXl1TDNJTjFwaUdCT1dpRFV3ZTZSTUJuZEdMOW51RlZuSlZnQWdJWmFNVXR1OUZyWXQwYzEvTW9ZV25EVFp1bzY4SjVETTl4OXh4YU1RR2ZNUmNvN3RHUDFZN3hsZkJ2Ky9QdGVmdnpxbjdzS1Y2WExLK0QvOS9uNGV6UEFBNEM5QkhCZGd1NlZmL0hjLy80bGR1Ni8zclhnc0pHY2xqUFg4MTFwejR1ZWc5Tys4YW5RUis2RWJqcENrYkNOQlhSOEFkL00wQ0pEOHdKbFZjL244S0VJV1ZKeG9qaDhCT1FXS0hKMnVLSVNkaGZkS2ZOQ0ZJTUtBc0pUTmNBb0xpUUVVNkltVTQ0b2JnN1d5aVFzTWY2TURiSWhLajY5TEVKS1BLazZMN2hCWW9UMUNNQUpJTU5BSVJBcFh0NGkzUXZHeEdwQkhONngxU3FPK2pJaVExY1RyR0hBa0NabkJjQUZ1WXY5aEp6UE0rNjhiVXRqZGs4ZGtYUDJOYTVoWnV2OXF4YXZlZW52ei83UGkvOGhLdVMvRndNOGdBUDJFbHpwMm9jOXJ4dS83cW9uRG1kOXhRYkduRk1pWmh0VzlzNXh5c3RlZ0IyUGZTRHkrMjRCajgrSkl3djQ1bHhjak5CR3BtWVpXaFQ0b2tpWjlOR2wwYUhSeWV6dXhjRWlLMWxDSWR4Vnc3RGdFYy9nRGpETEluSWlxTGlxUFdnQVhEakRDSFZSWlVoZzBMYVJGd0lBNlZHOTFvaHM4akNSU04rczRpNmxGaXlHbWo2YVFNalpMQVNRMVNRUXlSQjVJcW9uRENkc05JVWgxeUxaQ0ZudzBMUlcvYmdEWktvRlQ1RGJjb2hSQlFIc2dGS0F1dy9PTko4WG1SRkRHYlZYcStNbTFsZGVVOTc4SDM1bjlvcC9WejNoMzV1eTV1L2NBQS9nZ1AwRVh1S1AySG42eVQraEo3NzZDZU9aanp1TytaejlaRHB1SG9ZZXVLSXpmK1A3TU4yOTA4cGYzUXFYWkVlM2lFTXpsUGtDM0JxaHpRS2ZaV0RoMGxpb2tTaWo0R01Sc3NBeEpGTU0wSm1sUUNxS1RGMGhMdkFTVlc4UzZCNUt2Z2F5cWJEV0drYUtLTlhoVlhRWmNrSUdNc0pnUkd0R09xbmdlK1VFa2k5eFFVaHVVQXZwaEp2Z1ZiQWd3RWlyK1dNTnhXQVVQRkg1b29IU25nakNXcjBVT0tGM2RBbHU1aFVFaitOaFJRQ1F0S1E5UXEwZEhyS1NML2NjSHJpMWxZUGdLVFB0d0ZvWmZaejhhWDdiZi82NXhhLzg0TituSi93N05jQmwyQVYyLytISzE3N3hVai83c2ZmNDVxeGZYWmtNNjNlU1R6cUg1L3o2OXluTk1zcEg3NkRHQXE1dlFVZG4xTDBMWVQ1UW13TzA1ZERjb1VXRVhHV2paNGZHSXM4T0t4Q3lyQlNYaWxDeTBIUlFMcE1ZT1ptS3crWk9EYVZlbkdCTHZKUXFleEY4Q2NrSUtrMVhFS1VJM0N6eXJpZ21jcnZFTkFJV0lkdU03RTJLcTAxMGlXUW43eWlYaGFUTEFLSUxQUUxEMnpKMVJDTFFtV0NrQjdOQ3IrV0dBdVFHekFTWmlRd1BISjR5aWgwSWpnVEZHNkJSZlFvM0MwR3dGSWQyOU5pQWpZMHM4OUd6RlU3TFNpYkw5Qy93WjFmK3AvVmZ2dUx2eXdqL3pnendBR0F2QmYxTTdOci9jenVlOHVxbmp1YzgvbkRaSE5KMHBSKzJic2ZrR1EvRzZiLzYvYkJiNzJVNWVGUThQcWMyNXNMR0hEbzJCNDR0SXV4dUR0SmMxT0RRUE1NSHdiT2d3YUhSVVRMRTRwQTdKQU04Z2RtSXhZZ3lETWlMTFdreENzVlpkaFMzbFVKMGhvSmswQVJtTGs0bnNGMnJIQ2NtSllQdFg1WHQ3RTByblFlcklaSUc3SndncmEwQUkxaEtrVllGbUtrTWhXWGhzRUpwTEJ5UHpJSGpvN2h3K3ZFQlpVUE1aUlRHVWoweFpjaUFPaW9aNkoxSVdKNk55dXNnMFl2b3BFa2lWMWVRSmxPa3JwTjZNeGxVUkxwS0ZDTU1ld01BbVFFMU40MjhNQlEyUklJQ0p4SXRTaUNuYzNPemFITnpycHdGTjhlcUp0a3dtMTViL3V6QXoyNzl4a3Mrb1RENU8wRnEvazRNY0ZudG5zYTExeDcvbWpjOFUyYy84ZTY4TWVTdW01VDVuZDQvN1NJNy9WZi9MWFREWGRMaERYQTJRSWRuOEkwNXNERUhOaGJBeGtqZnlJNVpwZ2JSNTBVK1pHaHcraWo0NEVJV3BFUVhuSnNaUHQra0h6dU8xR1g0cVZOb3h3NzJqejliM1lOT0s5aTUxOUs1SjZHNzhHVGF5VHZCYVFkTGs2REtZTEt1aDVMRnR4MUNMMUJUL1JQTzFTZWVyL1o3ZnRMM2dzRWh6dzZVUU1hOUZNa1YzMU5TZHBSRkFSY0ZrSE4reTNITVBud3Y4L0daUk5kNDkzRnN2UFZlam5kdEVNV1Y3eENZT25MbkdqQ2RpTk9PUGhiSWltam1uZ0xIVWZYUU1ndGN5S3VVRVlDaW1VOGVONVdHTWVQNGVvWVhHVkI4MWROWU9KKyt4cTg5OE90YkwzOUo4NFJDWlhvK3g3YnlkMkdBRnNnWnUxZXZQZnQxVHgvUGVlb1JiYzFLMTYzTTVuZHkrcFFINi9SZi8xN1lEWGNqSDk0RTV3dmgrQXc0dXFEV1o5RG1RR3dNd0VhR2I0M1NQRk1Md0JlS2luY3M5RUdBa253VXl0RjErUEY3d1ZNVHVndlBaUGVzQjZsN3dQbVlQUG5CVEh0M0NaTUVmUHlkU3dkbzkzVTN1ME1FM1dzSzVZMldqWmZnVWpOVkExd0U1aXFEcVQ4NW9WYWdHUlZJek5KN05Nek9XcG14YmJTdHdtbXZIODh2Z0k4RDg4RU5ITHYyTm14ZGYxQkhYL2N4YnQ1NkZPVnVhYnBqTjdyOXEzQkczMHVFWDF1eU5OWGlnZzZzakF3cm8rajE2QmZac2JFeGhxd2IyZGQ4Nmx2WTdQK1B2L0g3WGpGNzFYKzlESmROcnNiVkkvNFJlTUFHTXVzVks4Lzh2YS9sQlY5emVOeVlZenJwRnB0M0doOS9Mcy84N1I4RGI3bUxmcys2dURtWEg5OGtqczNCNHlOOVl3RnNEdERtS0cwNXNCaFp0Z293Q0Q0SUdrWkpIWHdobGtNSFZTYkhZWTg2Unl2LzdIR2NQdU94N0I5d3h2SUVDWEUrNlZuQiswYVI2SWtFamRXQzhISDl3aUV5cUlCZ3NCcWlYQTdTck5JVENDYXVDaExrcm1RV2xhYzE0M0VZREY3L2pSQk9iejNFY21lek1BR3dXdjlFSzBBY2ZiTk1NTW5TMGlEWmptM3orcnR4OERVZndzSGYrWkRtN3ordVpMc3cyYmNUaFdCeHRMd3dTblFsRkhpd05tUXQ1cVVTaDB3WE1Cc3pGdk1SbmN0SFpLMldGUjFPaC92ZncydWY5OXJqYjN6NUMvQ0MvcGZ3U3htZnoxendYK0RSL1dQd3J2SGxxMC81NWEveEM1Ky9YdVpEU2RiNTRvajhvYnQ1K2xVdllycnRDUDNJQnJRK0U0N09nZlV0YUgwT2JHWnFOa0piSTN5V3hWbW1aZ1UrZDJrOTB4Y0EwQ1BmZXpkbzl5QmQvbmhNdi9VcjBEM2hJVmg2cyt5UU94dnpZR2FzTEVjRlBheDVvMHFaMVFMMHhDQmJnYjlXbUhxMUxNQmh0cTJYcXJRYjVKS1JkRkdDZ3d6Vk5PQ2dWVU9QaDN5SkhEdGdvUWlVUkpkcTlHZWpYZVRtZ0JzQVJmdW5ndVdKZThiQVZGOVVqbnZlOUdIZCtuTi9nWTNYMzRHKzI0ZCsveTZOZWFTTE5TOU1GQlJlMGVwOUdReVBFMkFSckxoam50M3pFSFhIV0ViczVvNThhM2NiWHJ0NDNkZStidXZhVi85ZGNNZWZNd05zd29LcjkzMzV0MzNsY000dkRRc2Zab1plNDViSzJRdWM5bnN2Wlg5c0pPNjZWejRmZ2NNejZ2ak10VEVqTmtacTd2RFpJTTB6TkN2a1ZvYm14WDJld1ZsbjVlaGM1ZmhOMEplZng1MHYrV2Iwajc4SVFGeFZaWmVsVHA0cVhOdGladndmQmZmMmpRVTU2OVhHUXFucWtGdVZoQ0s0WXd1ckp0MGRabkozcy9oaE5XQ2dhcWFxS3JvV3F1RXVHN2ZXMk5vUW9ZcGVGTTEwVlF3RFJ4aWFCVFdpT0RBd25LdXJHbjE5WTBydFhnSzhESm5XSllZR0ZqajhwdXQ1dzQvOUNUYmVzZUU3VGo4VkJVNUhHS0VVMk9VU2Q2ZFlxb0JNQW5LSU5UZ2JTbkRqN2lvb2VVOWFTOWZqaG1PLzVhOTkzSFhyNy96b2kvRmkrMXoybTN4T0RMQVozMHYzUGU1SjN6aGU4SVpURnRPMHdkRkdsVFNrdTNqNksxK3NsZjE3elQ5eWg1QUwvT2dtMHRFRnkvcE0yaHlnelF3dUhMNDFVZ3VYenpOdHE2RE1SMUZUK2gxM1lkeDVES3MvK1ExYWVmNVhBUUExRnNBSUpwTzdoMDRFRkF4UkppWTJqeU9EeDBRREJBQzNETHVSZFZWeUxJU203YjhkQ0NEYUloZFVHQlpvRVIzRE9DTmlHb0JpamhRdktLL1RPa3lOdndQQ05ybGQxQmdhVWd3NGFoY1RBRkl1dFI2cCtCdUtWaG1WWm9ITmJ6c0E1UGlnMWhuS0xPT0duM296YnYyUDc4Yk8xZE9rdFFuZDQ0MExJakkwYmp0WDVOMGhsSW92emNlaW9TZ2thY2pGV01ZOXRudmxMWHJubi8rSFkvL3RFa0Z6dGcvME9YajhyU2N6SFFEc01semwvM0xYbVNkOTVYRFdWU2N2Sml2cnRwQXMyWHk4SGZ1dS9CWk16am9GNWZvN2hOa0FIVjRuRDIvQ2oyMUt4eGZDK2dMWVdNQTNSMkJXb05rSWJJNHFXNk5RZW80MzNpQTl1c2Z1di9pdldIbitWeEhqQ09VQ2RBYmE5dUdMRWJHcWFMZ0szY1JxZkpSRWVnRkxrWmRDZUlhUEdjaEZMSUxuNEpaUkhNcE95dzY2RTJPQmNrRnlnSElxdXp3REtJTEhBSmpJcmpKUWNrSEpUcmpUeHNBazRRS3lvT3hDS1VBcFJDbkFXT0w3b1lSNnV3aGVGTzlkWENvZXl1NkFFOVdLRi9ManIzc1FkSUV0bHFHQW5lRkJMM2thSHZxN1g4MzU2bUhrWXh0Z1NuQXZyV0pTcTU3cStBZlIyK3NLRXlPNlNBZEM5RVBaaG0vTW40QkhQZmI1dTUvM1N3UjFBQWZTMzladTJ1TnY2d0g1Wmx5U25vSnJ5MnYyUFBNVlQ1K2Zkdm5oUEJ0c09rbUxyZHM1L1pZdjUwbmY5UnlNNzdrZTNNcmcwUm0xTVJQV0Y5RHhBZG9ZZ0ZtRzVrVWFRTjhhb1lXVFc0TnNZUnp1K2hEcytVL1Mycy84RzlqcWhCb3kyS2RsRGJxOEJ3VjVWVGxGb3VPb1NoZ0tFUURUcEk5bmZ2Sm41bjM4Zk9sZERFMlhpcHJDYjFlcEhuMlhId2ROcFBwODI2NXM3L08xNytQbm53VHhlQUdVUFNJdmdaUlNWRTFMaHBpMTVFR2psQ3NZNytxbUhkWnZPTXpydnZLVjRxMW0zZTZkeU9NSTd6c28wZDFGcjd5eVN5d0lEYVFnRFM0TUkxaFVpcG5MWGJhaXZyaHQ5cThkL3ZpNy8rZnNEMzd1cXM5UnM5UGZ5Z0NicHU4LzdYbjg5My9uNGtIL2VUN094MnpzTkI0VkhuRW1ULzI1ZjQzeTRkdUZ6UVZ4ZkVhdHo0V05BVnBmRU1kSGFLdmxmQTR0QU44Y29jSEpzUlB2dW83cHg1NkR5VSs4TUV3dE85RlpGU25WRSs4dXNvcnpZSUtYZ0RhNnRMUUpCMkNMaFM4K2REZkxyWWVJY2d3K1pxUmpNNVRiTjFudTNRRHVXSGNjVzFqSkdScm4wQWduU2x3VW1MTkFybUl3RVNaWFNRU01tU1dBbWtySHlicXFwVWtrRTRVdUdKS1VpSzZIMER0VEozYWRhUmVBVTFiUm43c2IzUm03MEsxTmlMNkhUZGRnM1FScDcwUXJEejhEdGpyWlB1R09LTEtNcUswcnRlc1k3aDR0cDE1ZG1TOHl1bW5IMmMxSDhaNHYrMDNnemluVGpqVU15ZUZHZVEzRExzcURNcWRBRmdrT2FDemd3b3RDNEFNVVpPN0xhK1ZqdXRsL1g2LzUwaitldmVPZG40dDhzUHViL3FFQU03d2wvOHUxQnp6eXF4Wm5YZGtQZVRpV2N0Y0pHSGNVblB6dkxwZmZmSkE2dmdsc0R1TDZndHlZdzljSFluTVF0aksxT1VLTExHMDVOUWN4SHdWTzVYZDlHTjMzUGczVG4zZ2hNR1FoR1pGYXdzWWxMR2RXYzY2Y0JRT3RTeUNnY3RkeExkNytMdE5mZk5RWGIvd2c4dnBScG85dGdldWpSaFFLaGg2SlFwS1FRSFFrRW9Ta0dHRmdqTDYxS3BxQ1ZSMkxJWnhOWUNXRVZTd3YxUjcyd2tnYUM0QU1ZQUNBS3AwaUZMTTg0RUEwMjBIY2dpdUhzY3RBZXNWeXVoV2pQV0NuK3BQM1kvZXpMOERLdzg3bHJrc3ZnSFVSL1ZROHZERUp0Nmg3dkFwdlNhTk5PK1JGMXVyOTl2S0xYdi9QK2E0di9YV2tqUVR1bm9nUURWUmx4RWtBcGdvYXhkbGxSNkNrd013ZERwTmhuUnM2SCtkT0x1RVRYdlpIZVBzVEw4WUhDdjZXRE1uZjFBTlMwVVNrTit4NDVsdS9ZbmJTbDl5THJkRzd2c3ZEUWF4OXoxZGp6NWMvUXVNSGJpZkdFVHcyRnpjSCt0WkliQzZrOVJIYWNpcmdGdmhXZ1M5RXFJZmZkU2ZzdVdkaTdmZi9YMkFzWUtyQVNlMnppTkswNnU5Y2NMbXNTM0hKWC9zMjV0KytSdmt0NzRYZnRnNWlWY1J1T2pwME5vSDZKTkNvRWpXSG8zYVZlek9MeUJxamFDWmdxU29BNnY5QzVZTFdJQ1FTVllGS0lNRlRDbmtYTFlnSHlWd0daeFJMZ2U1Qnpoano0UjVpQ0tlRkdWUzFqVmhKbnEzQ3JEbWdtUUFoUFdRWDkveUxSMkwvMXo4S3F3ODZPWjQ3Rm5pZnhGbzFHUXdsVUc0YW9USVVwRW5pUFcvK01ONzM5Ti9ENnQ1ek5LWVNvdFlJd1pSQ3JGQWt1Qk1DV1Z3b25YeVFVTElEY3ZRcU1MZGhCZDNLcS8xUFh2SUxzLzkxNEcrckkvd2JHV0NML3orMzQ3RlhQbDhQZkhFZTVrTXg2NFpoRTdqb1pPeC95Zk5RcnI4TE9MNGc1d08wT1FCYkdaaVAwTVlJYkJWb25vRjVrVytPMU16QnVURnZ6ZVduSHNQT2Qvd1BwbE4yUjFhVGJIbU15NlJKQUhJQit1aXJtUDNQLytQbFoxK0o4dTY3U2V5RllTOXN1aHFxNGlvNGdHTDZRYXJ3c3pjY0JORXJDVVQvTDBSRk05RHk5MmdBYmgyM0lhL2k2VEJqdXBNV0twZjRHemNDYnZGTGhzQ3ZFSUpaOUJqVFFwd1lVUytLWURlcXloaERKMGdQUlhRaUxRRk8rTlljWTk2a256UnE1M011d0prLytKVmNmZkJwS0RrRE5MRzJvUGd5Y2ZVNDhPS3dQdW1HbDF6TDJ3KzhBNnRubnU1NXlDZ3dsRWliNlFJTGF2b2N0elZCZWFZMEwyNHFIakpidXE5cGlrTitWMzc1K0h0Zi9LYnhYZTk3TVdCWGZ2WU5UZ0QrQmxYd0FjQytIbGVYYjkxLzNrTyt6TTc4L2o2WGtxMTBWQ1ltQTNaL3kxUGhkeHlCMXVmazVnemFuQU5iQXpBYmdjMEJtSTNTZkFBV0dUNGJpQ0dEWXdhYzhzV3RXUDNGNzBVNmJXOVViQkYydFcxK3RXRTNqQS81K3R1eDhkeC9pL2szLzJmdzNZNitmeURTNUNRd0FSbzJ4WEdMS2lOWWl0TmpDZ1lkaEx0TVMyV3BURjU1K3ZnQmF6TzRVT0VYMUFZUVZEaVhqWUNMRXNjVWdESlYyejFidDNBZ2J2R21CQU1LZHE5cVZiQ1NlRloxMWxHcVJ3SkFpWFFIU3BHR2djb0xZRFZoc204L1ZyZE81dWF2WFkrUGZNbC8wOTAvZnkxUzEwV3FJS2NiQ0FkcEhzb1pBa3dHTHdYMy85RW5ZZkxGTzVYdlBVYXJnakRBcTQ3UmxXcUlNVVZJaHNRRXNZOEdBM2hNVzdKTnpYVjJPbXZsc2ROSC83OEM3R0pjZFY4OCtkK05BVjZNeXlnQXoxaWMreDhlc2xqYnNhNjVZQjJIOGJDbXozb011NzFyeE4zSFlFT1d6eGJ3elFWOGF3QzJSbUFyUTdPUldoUmduc2xGb1JZWlVnOC9jaHZzK1YrS3lkTWZCeXdHSUNYZ3hNSGhxb014OGlqMFNjTTFmNjdOSi80cnBQOTlpMCttRHlWdGpjd3pjQnlrTmx4SWNvdk9kRXB0MHExN2NCeEZkUzRIZ1JDcGFtazBBYSswYmlXRzlGbTFqYTdGWkFtU29UWndWcENPYXYyVllZVFJ2UVFSQWNaRlIzdVU1NUhOaWdxNUtrTXozWkRyaGlDS2pHT0l5bjRSZmFWck8wL0R5dUlVM1BHZHI5Wk4zLzV5SWxuY0RyVzdMMWdVQk1Cb1lSL3NraDcwMzUrbGhSMkZsVzBLU0ZaQmM0QkphSmdxQ0NnUjZBaW02QjVBL1czYTBHSjh1Ri80cksvWjhiVEx2eDZYbDZ0dzJkOEkwdnVzL3VncVhKYStIbGVYbjluMW1HYytzWnp5MWJPeUdFVWtYMnhJWis3ajZsTWVCdi9ZWWRCbHZyRWdOa2R3bHNIWklHME4wRHdUaTB6TU0zeVJwWVU3UjRHYk13eDdabHg1MGJjR2pHSXQ3QzZ2WS94bnpsVGZjLzZhdDJEMnJPL0Q2c0hUYVpQVG9jV21vRXh2aEZQclFrYzFwTEJHS2p4VWU5RW9udUZMNlVwdE1WcnFTYmg4YzFYVW84NFg4b1kwUm5lazRGenFFU0paalorcThnK1JaQ0dVclNGcGJhbkUwbFBXUjNSNnhDeXRRT3lxeDYyR0hUNEx5SG1nNk5peCt6d2MvOFhyZE5QemZsbE53QSs0WkRXNXJRQ2lKWU5ueC80dnVaL3QvL29IWW5iNEhxVkVXRWgyWWZXekFCN3loYVpvZEtBRDFYTjU0bUFrRmhxNFJ6dnpsL2hERGdoWSt5dGNkRjhRMStmVUFIa1pMcExPdzhwRHNmb2Z6eUMwd053b1I5WUdkenpuMGVDd2tCL2ZoQjlmbHphMndFVW01eU14SDZoeHBJWkJXZ3pDZkVGdEZXQXhRdXJnV3gvVHlqZGZxblQycVk2U2diNkxiaUpIbXdZUUJVblhvYnpsM1pwZi91UHM1ZzlrVGhPTTR3YU5UcXJJM0FrNHJYN1Zma1lTMGJGT0ZoaEUwaEdraVVpVVNML2xOQlFrWklaUlpoSVpwQVBJNFMxUjIrcmc5VDBGd2hXdHUxSDVVbG1NcG1Ra2JUOEh5aFZPTG1TMDZSSEtrRExvRHFxQUtLTEgremhHdW8raEo1Z2thR0pBbjhDVWFxdTdpM0w2dUlXVlBhZGg0K1VmeEUzLytqZVJ1Z1M0MXlxaWpxQ3BOd2NqUE92OEg3ckVmZlc0T0J0aFh0QVZlU29Gblh0TUhZYlhHUkdGcWdxSkRrSjBLUXZGblhUYUhJdjhpTzdDQno5Lzc5Y2N1QkpYK3QvRUMzN0dmM0FWTGpQaVN2L1B4eS8rNWtkaTMwUG5QbzVNWm5uY2hGMXdHaWNQUG9kKysyRnlHS0NOR1d3MlNNdXZFWmpWUEhDZVVmTStlaTdVMW9DOGE3VHA5L3h6R0VDUEtWYlJNbUVlUHJBNGtBejVJeC9EOEhVL3dNbnNuR2pBS0lOTXJDMDVjVkdpZ2FMZXpUV1hhem1jU2FnbnRCcE5KWGJSQnNkNDlYdzE3MnN3czdmbmlwU0hBYXRlMk9YcmhCQXFsQXBxL3lLNDRSTHFMTG1pelNNMDJnU1FZanBSR0locTY3azdqVUNTSVc4ZHgzajBEbzdINzhSNC9BNk42NGVWc21ESmdGSmtEbW8rdzlyYU9UajZTMy9CMjM3cXRTRmVyUmhMZU54MnRZbFNDblk5NUF3NytTc2Z3SEhqT0ZJeUlGamlPUDVBcHRHOElGWGJyeUQyTUxCbERnYU96SjBWbEVmbGgzem51WHNlZFA1bHVLb3hrNTliQTFUMWZ1ZnR3ZDR2OXBOL2VQZG9QbWVHM0RWaXpwV25QbFM0ZDBPWURjRFdndGdZb2EyUm1nM1ExZ0ROUm1KckJHYVpHQXFVWGNxakFFTlpISVNlOGhEbitXZFRKUk1wYmRPbmxZT1BpRm93ZnZPUHdnNU54TFFDbGJFK3JZWGIxa2ZqOGtqMjI1MHNCV3Q4UWpHRHFEZmhxakdSUy9LdUdtSjk0ekJzcXhMUGhnTXRxNkhRSGxkZ0JyV3JtS2xkZFYvbWdTQThpdW4yTXRHV2pvWUUxZGNuNVdKSzBteUxXM1lySjg4OW1hZiszRE54MXE5Y3hqTi80Ym5jKzkwUHc3aDJHSG56Q0syYkFGN2lGaXh6N2xnNVJ3ZXZmQk0yM25jSFU1OG9GNHhVaGEyYWlvY1FlTW8zUGdMRjFtR0tZWVZXengzaFNJaHhYd2xOdnhxZVBJSHFZR2hDbkE3a3BzL0xSWDdPam1mbVIvNG9RVjBWQmNubjFnQlJ2ZCtQNDVFdmVJVHZQVy9tUXpaaktubUwvZm1uWUhyT3lmU0R4NkRGQ0d3TXdHeWc1aU80TlJMekRDeXlzQmlGSElKU2pEbThpb2pNdytpKzRkbFZvMUZCcVJNTVgxN0FQaUgvNnF1Z3Q5MEFUcytoOGl5Z3dTbzZyeFZ5RFozYkJoU0J0dVpVMFJaVWM3cjZOM0lLWmZsYVlJd2hxcytwcjErYWNZRU1vNllLaWV6UmlWa1VjRWYxZGhCYzlYbEx6SHhadzFmMFVLMEZ2bnJ1WnRRT3BNU3lkUVI2TkhYQm4vNGdMM2pWOStHVTczeXE5bjNyNDdUL2hVL0VPVDl6T2UvLzdoL0E1R3RPeFRDN2s1YjZhUGR6Qnd5WUxsWjAyNCsvR2hDWVdMdU02NE9rRUhhcGs1NTZJU1lYN0tSdnpxb3lOa1pvR3JZN1NDakJQRnhldkZCaHh5aE1XcGQwTWFYUlMzbWNMbmpleGFjODRJTExjSmwvTnR1ZlBwTW5Fcmphejkyelo5OWpGM3UvWjlkaVZORWlJV2NEdHJEamk4OFhENjhEV3pOZ2N5NXRMVnlMaFRDZkE0c1JIRVp5R0lHY3FUd0NPUXRqblFvNTM0U2R1VXRyejNwU2dMcG1hbXFVcU9RRU1pa2YzY0RzcGI4bHM3TlJ4dlU2TUNERDRvNjFhalJxRnpnRkYxdHpQSm1oeUpoVkRRcUdBbFFFTEdEaXJKcEgwaEN6YzF2T1o4Z2dScUhtaE1iYy9wN20xWE1vUEljcEF6NktwUWcxVk1mUEJjck5VTVRRcElUelZJSEpFZm9YcDdxa01teEtEMG80Ly9VL2doMlB2ci83a09XTEVXVW9LRU9SY3NISy9VN1dCYi8vdlZwNTlqbGFMTzZGZFlaa0VITm12N3FYVzMvNGZxeS8vYVBPbE9STjVsS2hJak9EU2xhM09zVys1ejRJaTlsUmRBWkhxYm10SEN4WlNRV0d3b1NDVGxLU2FPNUlYdGdMU25LNk0zWGVjMU9qTHVEWmEwK2JQZVo3Q2VwaVhQd1plOEcvMWdEZmpFc1NBZjFnUHZ1YkhtUXJaODA5RjhKTTR3SjIwbTcycCs2bjd0a0Voa0pzRGVaYkMySTJnUE14Rk14amRneUZ6Qjc0WFhiUVJXT1M1N3ZRZmRVWDAzYXVBam1EQk0zTUcyVkFMNEFCK1hkZWgrNzJRNlN0d2R3cmZoVVcycXBVUXdOS2c0S0lVbFhiby9scVNoU2F2K29WMmJ6aE5qWVN6WkVsY01JWU5NaG85UzZ5UklCR3N3NnBtNUJkSjdQRVpJbnNFcGdTYUVhcjRSdnk2am1xWnc0RU84WWRvYzFhaU9vMmVkQmRZM2NJWi83S3YySi84azdteFVBbUEvdU8xaVdtamtCS1VJNkc0N1ArNDcrQUpsdk9MQWdlYWxZNmV1M0FYVC8vcG1vRTFmOHlZbjdjMzhGaDduLzZSZkxWQlpqRjVQSldOQmtRNXpnS2RLc2htZ1loeFdkYmFzb2xwN01rS3lnUHdYbmZkTnFlMCs1M0dTNnZWTkxmM2dCNUthNTFuSTNWaDJ2dkMxZEw4Z1d5Z1VEV1hKT0x6aEVYSllEbXJVR1lqVXFMVE14SCtEeERZNEVXbVQ1bWFYUmdkS29VdUlxS0JPZWNmT3dqV29TcUpWc0xad0NTUVdNaGZ1MFBrSGd5cUVWRjI1WVFpQ25ZU3FHR1d5bXJobHJXNVZtT05zTUtnZkt6Rml6Y3p2c2kwVVMwWnhJeXNsU0xuUWo5UkhLWHhuV1VjamNXNVdaZmpEZHBNZDZNZWI1VmkzeXJqNHZiNGVOUklDOUFrdXdtWU9vRC9WYXBYbEpxdVdtN2NhSzFQWVpqbGNVeFRwOXdGblo4NllPaFhHRFRTZmp5YVBOWXFnVFpHVlFjS3hlZWlwM1BQSjlsV0Fjc3hTY1pSNnlrZlZ6LzN4L0cxazBIWVYzWDB0dEFmb2hBdWdIc2V1UVo3SGNJUGd3ME9PaTFkYjQrVWhqYzhueEYxM1FRaHJHK0xFakxST0tZdHZ6Q2ZPYXVaK0J4MzBaQVYrT3F6eWdNZjFveHdsVjFhT1RMMWk5KzVrTzQ5dUI1bVdkQXFaUU05WW5kV1h2aGh6ZGlTTkRtR1BUYUlrdERCa2RKbzRnU1l3b1VhRWZrNHU3d01vZXRkVDU5eWlQcmxQcmxnSjlhbUVibFc5NzFmcFIzM1lDSm5RLzNnVUJTYkdoRE82dlZXa3N0WGxvbmIveGF5S3p5NDlvczVUWHJwOFdjVkcra1d4VExKSkNta2FxVm93S09zWlFSNWRTVHBBZWVETzQ5RC8zOVQ2ZWR1dy9GblZyUDhsa3gvOWpkd3NFakhJOXVBUisrVFpqTmtiQUxocE9wZmdkSVZZV3N3NFVZbEJvSWNSdk1CbUd1YnUvcGdlYkpnOU96TmpJcGFwMm1oNVk3YU9Ea29qTzE5UWMzb2VPdW9QRWdZQXFrZGNjOXIzcW56djIrcjRLNzAyeXA0YWNnbGV4WTJiVVRPNTkwcG1iLyt6RDduYnNKTDdMYVZGVnFYNzRMVmFCQUZjOGtpQzRBMFJDMHhoM0VFWmxFTno3S3o3MzhOL2JoUDE1MjVMTGorQXlFQ3AvV0FDK3I0T0tqdE9mZm5DelhPc2NJRTJYdS9mMVBRbWNwK2ZFTnNBaWNGMm9zMHBqQlhJUWlzenFCbm5Ld2lQSjJ4eHY2UE9QaXBBbDR5bWxSbVpHSXViYlZISEtoa21GODdUV1JNeVVBb3lvK0I2UjZvd3F4am8yVnlvVklDdzVVV3Nvc0d6WFNCdHA3aGYrckdyUGlQa3lyWUNrYzhzMHFXTUFlZlRhbVQzeVdWcDc5SlBBQkQ3RCt2SlBhcVdrR3Z2VGU3ZCtTWGVVRHQ1c2Z1bzJMVjc5SDh6ZS9WMzdkUjFtd0U3MmRvdEpQYUdXTTQxRGNEQkxCSkhSdzlOMXFKVUlDdzBrZzNJMW05ZVphanZSVkJZV25OZjBJVTQ1QVhOUmppdUdhRDBIZis1V2tNV1RQaG1pNmtrZHdtQUlyRDkySDJSL2NxZzY3TExMbzJ1Y1NRd2REWkFOVWdYZVUrNjVTVzFLRUVZNGlzRE9takhGMklVNS93R1grRlY5TDhOYytreDZTVDJtQVZ3R0p1TEs4YVBmWmp6M1RKMTg2cW9pQWxRZ1pxYi9mU2NMbUFDeXF1bmNvd09ERWtPTm5nYkhHUUpMY0lQeGd1MkNnK3lic1N5NGtkNjI0eGd4MnFhblRZNVpFRjc3TVBuU0RnRFhDTTlqb3NvcXExQTBLeTVLTVFNellhNTZnSW5kc3BRM1ZZQWkxQmlLcUFLa0QxS21VbXptdURlaWU4MFNzdk9CNTZKLzRhS1ErTHJnRHJ1d2YxODNaWm5Tb3dURXVwTlFqZmRFNUFzN0J5bE1mejExengvQi8zNDFqdi9RNkRLLzdNRzNUWk9sMG9PK0ljUURaVVJLU0Z3Z1RsT09IU1JJV295Y1JNK1dxQ0QrR1hxRk84d2diWHQ5Z2h4NlFaREdibjhwWnZlM201dHR1MVdManVGWjI3MmswTTBqU2tPVG1Cc0QzUFByK2RpUzlpOGpCK1RWNWJiUUNCbnhxaURIYXJMZEE5Y2VJb0UrVWtJQmhia002MlZmOVlUcnZHNjRHZnZNcVhPUi9YU0w0S2VQMEtiaUVBdmlJN3BTdlB4YzlNOHJvMFdsTjdwd283ZG1oY213TEdFZjRZb2paZlRtTHVTaUtqVklsN29LN1E4V2xxcG9MaWZrNmVQNnBZU1VlVUFpc3BzZHlJU1g0NWd6NXJSOEdzQlB5dWllcmZpbUdOUWZTV2dIVGh0cUhWWUl4alNXeXpTVUlFdmtYUXdoUWdINEZwYXh6NXRjQjMvaFk3ZjdMVjJMbjcvdzBWeTU5TkpnS3lqakljNDV4SFIzQkxnVlBuVktJSlZKQ1NoMWtSblY5VFBFdFRoOHpOQlJ3YWxoNTJtTjAydFV2MGlsLytaT1lmc2VqbWROTndQeWdySitHRmhrT0x3V3BXOFA0OXBzMXYva09zRXN4YUthMUhaeTQrdE1CZEVTZUQ5eDh6ZnZWY1llUU13Q3ZrNWhHc2pOZ002Tjg5RjdVa3hIdTJoRTRhYzE0ZGo3aWRQaGtoTGxrZFlJVGE4RVI2V0xrclVDb0owMmh2azZBNkZIaEk3d2lqT0M4RE9VQzMvZWtNMDgrLy80TXNlcW56UVUvMVMvNVpURTFNNTNqL1hPRWpJS2NTTUUxS3AyOGk4eE96UmJDTUlEalNPVk1qZ0VUMEVXVkVwU1FsK2liaUtxUGdhRUJqb1Y4NXlrMW9TVk8wQUkwbWhTYWJ3SWJHeVFuSUVhb1RrMVdqWHlwR2xJRGxldVpWZ09rYXpMRjZ1clFPczFSVlFQc3A5TDRZZmpGMUk3WC9qeDIvc1pQSXozb1BHa1lvRndpYjBvZFFVTkt5eGJMdGdNcFhBTWNvcGhDdkNyQVljbGdmUWRPVW9CK3haa1htZjBEejhYSlAvOXZlTXBiZndMOGlwTXh6RDhDU2tpSklBdVl3SFFjUEh6Z3FxaVA1U2hEamdIVTh2aEl4ZUhqQ0pycDhNLy9FZnlHSTBqVEZVRUYyeCtVb2dHY083YisvTlk0NkJpWkZlMGpaa3RBM2RJS2pFbXVURlVSbHRVS1BVVThEand3UEd5Rm93S3VTWlFTSFYzNERBR0dBWjdQMS83Sk53eVB1eHhZN216KzdBendxcHAzL3FmcHVVOCt0L1QzSndaUGhrU1BXVi9kL2xYcDJCYVlSNko2UEpZTWxnd1VoNWRSdGRpUXBEb2V5Qm5KSWdRVUdvVCs5TE5xS1doQ1RHcjBTcUNCZ1B6Mnc4STRYeWJHUmdlWTFRRG55cWV4N2RlSUFnTVdmSytXNHVuNjNMZ1FVT0NOSlBMNEh2YmY5VXpzZU9lck1IM21KVVRPOUZLQXlVUkxCVFlBWTB2RDZ3bFMzQVNxNDkxRWhRWlBoWklDZTZ2cXFraWdER21TeURITEY5bFhIbk1oVG4vRFQyTFBMendQNCtSNllGaEg2aWJnc09XVDdoUXRmdk05T1BTUzM0WjFYVW1UcnFHYWhDaDJCcHYyT1B5S2EzRGtSYS9DYW44YWxSZVIzcGxJRTFQSVcyUklHQS9HcGxjdlVjM0duTVJ0ZHpyWnMxdlQ4L1pJZVF0SllYd211YUdBS3V3RVQ1SmlkYmVZQUNVVVVKbUJFMHBkVUlzc1JWYWd0S0tFaHc3N253MGdYWUVybW1yNk16ZkFVM0FKQ2VCQzIvM1Uweml4UVY2aU45cWxQcUZibXdEeklicTVoa3ptQXNzT0ZTZHlGajBVRmpFRnNuaEFFRUJyNHhHZ1FvQm43aUdBQUR5QXFDeHN5WVpRTjl6QmJqYldEcC9HY3FnQ3hiVTRSR2pyUksrY2JJRkhzaW5TR1RQSG0vNnVNRm1DK1J5T0Q2bjdsUmVyLzltZmdLMU9HWjEySFdncFN1SlFCVExTYjJ4ZnRDZ0Rxa0ZHc3Q2a1Q0a0ppWWtoNEs5RlR4MEVHQUNiQVgwSEg4UFE5N3p3T1R6dGoxNEtQV0FkWlhFSU5sa2x5aHdyM1JsWVAvQTYzdjVWTDdXTlAvd0xsUFVONXNVQ1paeGg4ODN2MDIzZit0OXcrSG0vd1IyTHM4S2lxcW9uQmxPcnBqWFJlTEM0c1liZ2prRjN4R2N6cG9SU012dmRVMDR1M3MzaUkxSkloRUJ0aDJESWsxV3RaRlh1TEhrOWh1NmJNZmNyMHRBRXBTME1aYit0UFBwcHU3N29NZEZGOTZrTjhENkxrS2ZpTFZrQVR1blNNd0JIcHB1QjdxWEE5cXlTaHVqbEhUMm1FWXdlUTc5enlORGxIdFBsVmE4UjJ0dzZFbkxSQzZ3MzhNejk5V0lEdGRjTEZtVmMzTFVIRDROd1JsZE1uWHRTK2RhYVMwWk94MWFLQ0NDZEJheFFUSzFTcXd6RWtyd2NaOWw5aVAxdnZRelRaMThDakRud3hpNVZYMWJkclFNR3IzQ3l4ZGhiZDdHVUtFQnI0QWRLdkhyTk5KRVNhQ1JKbDBjelNjMjlxaWREblVWcExQTVIweWRlakZQZS9OTTQrUFFYcTN6Z0xxYkpLZkp4d2RYdS9oaGZjeENIWHZNendPazdwTFFDWUFHL2EyUlhWckRXM1UvdVkvTmt0UktLRGplZ1FBNTA2TVVQSG8wYUtSbTk1bXRnSUs2NU9KQ0FmcytFQTBiQlJLdHo0YUtCZnRtNFRvaUtJakVFckNKaWtQVUorV0tkVDRZQ2phZVhmU3RmcEhPZStjZDQ3enVBU3d5NDlqNFYwNS9rQVE4QTVoRCsvZjdUTHpyRitvdmRSN0YyelFQT2JtY1B6UWNpajFESmt1ZndiQ3Iwa0JjRnM2QU1xSkFvakhGQVZSbHNIdDBZVm1CN2QwaUk1aUl4ZHN1RUlZWUZjdU53dkpZNUluem5HR3BMRjFrQWx0cFAzbDQvR0FFa0Y2MDJVNFlLbEc2aXNVaXJkNkY3MWNzd2VmWWx4REFBWFFJdFJuYTB2SzY1cjVpTUlYTE01SmlqQ2FydmFKT2U3RHR4MHBHVG5waDB0R2tQbS9TeVpIQlJlY2hMOEp2YnA1cW9rODg5UVp6MEtJc1IvZG1uNExRLytpbVVCNXZHNFREUlR5UnRxTzkzWU5xZHBaVzc5M0o2eDRUOTdYdXdabWRpc3JJZnJsbDQ5Tm9ud0Nab05WZEl6U0tSV1J3OFFvd0YwWVVTZUplMWxMQWlmTlpQNVpnMzFTMkFVRVhERzVNalNBVmdDWUVDeFJSTjBlZ1FYWE9wa24wZWthYnJTNDhIOE9TbjRUS2tLM0ROcDRSaVBza0RYb3BMN0VwYzYrZVhYVSs4bjArbjhEeFM2dHpqSW5kcmlWak1YV01oUzFmN2J5c2MwdVpFc1FUaFE0UGd5KzRmeFlnU3lBcmtDM0JsRWhxUTVnRER5VlE1RUdCNXJvSVJZQ2F0elpFb2FEdllJcTZkNE42alFna3drS3duRXlTS2tGWlJGdTlCLzNQL0FkTXZlNEl3anNKa0VzMXVqbGFxcVBxcDBJRmxCNjBEK3lxaXYvTjJsamUrUi83QkR5SFBONWdsV1hZWk8ycDFCZDNwNTZGL3hxTms5N3NmdTlVZUFLQXhoMWNrNnBnanExRWRkSVBZOS9CaFpIZldYcDc2eHovaGg1NzBBOEJ0TTdMckhPTkNCRTBwMWROWEpNMko4UVMweVoya2hlYmZ3bmxZT0hJUnhyellRQ2taQ1dtSkZvWDIxcGYwVUw5ekY0Z0JsQmxSNG5vaTVzR3FhdHNpQkFiVEdVWVdyVlVDbEFnNkJaTllvcGVBanV3bmpUc2UvdWcvdmY5WkJ0NEtIRERjUnd2bmZSamdwUTVjaXpOOTVaTGt4SUk1eHJOTHdNVEFqdUFRRTQvcEphWi8xMzBjRmg4TWpSbFR2U2ZDT0V6V0VNNG1TS3BVUVBNU0JPaG1xaE5iWUJvdGFveGw5czk2WW1QTFFTTUlhbG9XT0dEdExFUE5VeUNoWDBPZWZ3RHBoNzRKa3hkY1RoOEdvSnNzaHhxNW5JaktNT0M4eWplbnJsUFptbVAyeXQ5anVlcjFLbS81RUxBbEVwMkVEZ3hnZ2dWa3dhQVJXYk4vUjlwRHprYjN0WmRpOVp2K09icHpUNHFrb1dURURxL29DQzhBVXVYRXZPdWhzV0J5OW1sMjZtLytzQTU5MlpVeVB6Zmt0QkRNUzlUOHpVdXIrWFY1TlFJV09SRFQ1ZXFwaXNvOHQvYUNsdVJJb0ZqSERVZWc2VTlaRlpHWjFBUWRJT2tvQU1LdTZodVMyeEt6ZHBacm43WlJNQnFMRDBReXp5emplYlovN1VGYnB6MzFYYmp4MXcvZ0dydnlQaHFYUGlrRVcxZ3BWeklmdHcweUtJREpsUlJablllb1FGVllFRFJxdTNNYU9WRHpqYUJyRUdFU0JqaGtCU25tZUM4RDB6STdjVFNVSXpJd0dHV0NKNCswa283QXppUzNwWVJlb09BbWdlNU9GMWlyN2k3SjUzZklIbjJCK3BlOGlNZzVnT2Z0VDA0YVdibE5lQ2t5QzN4di9KM2Y1OWJqbjZueFcvNDk4UHBiMlcrZGc5N09SOWVmaTc0L1U1UHVWT3U3L2VpN2t6VHR6OGFrdXovNzRYenl1a0g1eGEvQTFtTXU5OFZQL2p3MExvQ3VFMHBwUkNPU0JhaXh2QXA5Z3NhTS9za1A1WTUvZnptRzhTYXc2d0VXTEl1ZUtPSFVwUE1CYVJZRm94SzNmR3VvUWkzM1RIRnRxazFLaU82WXV2WUpBSlJPMlZseG1ianhMRTYrdU1UOEtOdXVQcEJZaC9rTGpWdnkwQXNHQkZzZ09vVTE3L2xGT3ZPaEFIQXhUdDNHTVQrVkFhb21CZjkrNzZrUE83dXpNeHdMdHlvUWdUdFRWNG1aT29CRktoVi9LZ1E5Z0YzM2VqeUZRaUhOSzZWUjZzbFVkRng1UnBNVk5JS3Bxa1BaK002aWdnS3ZJNjhjeHJwd092TEpLcnQzQ0psZ3lLamNQQmlQMmpJT1plT09RK3gvOWFldG02WklFNU9kOE1FclBDTlFZOVhqSFRuT3pjdGZ3UGszdkFqOWU4SEo5R0treVQ0eUxVaHRBWGxHNUFWUkJpaG5vRVRYbXNxYzRBSklVNmJKK2JCN1RySFpqLzA2amp6K011UzN2UmRwMGhOanNWZ2NKNk5GTzY5NTlLTWdKYWdVN1BuaHk5QTk2MEtOaTROaWRQZVMxYzgyaFE2aHV0Z2hXZzRNQlZiMWpsR2h1Z3lpbHpsOHZnQ0FDb2NCUzBkVWJ3ZmJQWEd2QzVBanhjbG9oVVdVV29VSzdDLzBraUVoZzFGS0lYRmpraXMxZ2E4WE9vWmtrUGFYSFU4R1lQOGN2M3VmZWVESEdlQTE5ZnZUeHNsRit6dmI0Y3hlSy9QSUthWUdjeUhROWl5VFY3cTZvRTVYTkMybFRZQ1pSNVZxdFpPV0xwa0xWbW9oWXVINEZFRytLcVNpOFFlSXlVSVk2Z2tOTS9FcTlBd0RjNEdpcGFvYk44SG9xRXNVaERRbEZ4OUYrbzUvQ1h2NFE5d1hBOVIxUkJ1YjFhSTQ2Y2hWK1BxdTkrUDRZNzVhdXZxZDZsWWVSRTFYcVhKTThFVzc0OVVFcTYxWVVxVllUYUhBTnMvQXNBRlhBYnVMeEhjN2pqenRtN0gxaDM4c20vYXVQQWFJVVlIZUlOQXExeHVnUEUvNjZlOVUzckV1bGtoOTY1NGxONVRBUXhVbkt0UWJWVWZQY0FUdCtBd08rSUF5MU05YnZTOGJWMVN2dnUweWRhak5UekdIcE1Gb2xjS2pVcVhoekZ2Q0w1b0tXeE9UUVV3ZW5ZZXFQeXR5N2h5bVo1OTAwa2s3R2p2MWFRM3dVbHdtQUxob3V2TkJLN0l3UGRUS2tvQjFKZytHSThURHF0VGFzcFJvVllocTVSdUcwWmlMU0Q0RW1Kb2txQllUZ0dyTGRqUW1oSGtseVRyVXlpd3Bla1FzUWpwTmtQbXl3U2dxazJyb2dieEF3enAxOGk3WkQzOVBCSTlrOWNKWUhSQVllSjlLTWZaSjQ1KzlHNHVuWG83Vkc0WEo1SDVBM2xCdzBPUlNwczlZbWNsbE1oVmkyQ3A5cUMzdGFzMDlRRjVIbjNaaFpmNUFiSDd0RDJQakZhOUZtdlpBeVRYQnNOWjVFUG9JTTZnVVRCOThkdHI1d2t0dDhOdGhxVmRkYkZ4N3c5bHVBRUJlL1ppTVVwMHcxRzdZZXRoanJyN2VWZW9KTjR0Um1nNlEwNVNpaXo0Y1FPZ1phMytMUktvdzVqbkV0YTB0OHlBWVBUZ1ZJMHdrVE1BUW9kdUt4bklhZCt5L3BGejBTQUc0N0Q2YWxqN2hCeGNGc3VXNmhJdzFHNlVXREVpTWh1OFN5d0ZxMGxhQnM2QzVzTzJkdEhSczFqeGZ5MlFGb0lBTTNJekxZMmdwTnVwZFZObWRTbHBXOXhPZXp5Qm5zeURKelFtVG90QnpGUlNwNzVETExjTHpMd05PUG9rWVI2SXVEZ3dqcnpoaDhSanpkdE1kOEdlL0VKUGpKeU5OZGxIajhWb1kxZU1XRUxOTVZYVTJWUXJCZXRPcDVtWE5qYU01R2RMTGdBUmdOVitJK1RmOUtPZHZlUWVzNzZpYzY2ZTMrbWVzUms1Smp0M2YvL1gwdlJrb0plYU0xL08zM0RnU3lHaU5GeDQ2M0FpYmFEa2d2RURqdUR6RmNUNVpHMHpqNzlGSHU5WUo3WmhFL1YxQzJ4cTdiRjVHNUgrTkdHaG9WNlNtQ2RFVVd1MWhQS2xNSi8wTUR3U0FpM0R3MDN0QXc1Vit5U1hvZHROT0Q0RkFTWUVCQldESlRrUmRzS3RvZFNSUW9xMFFJaTNtU2NtOGVhY0tEc2NBaXRhL0VldUpSdGJhT29namhFTnQrWE9ZRnJ5ZzBQb1J0QUtrUWlZbkxaTldZSlpCOHpvaXFQWjBKS0h2QVJ0blNDZXRJWDNYTjVPaEtBa1B6WXJHeGpXZ3crbWxZT3M3Zm9DNkp3TnJhM0J0U2FsT0Y5QzJSeVZGczFKSHZ6ZzZ5N1NHUnlaZllwTGJGNnJKK2dIM0FjVUsrL0grUFByczc5YmkvUjlGNmpvZ0IzOVZYVW9rcENuQmkvdjB6Rk8xOWcyUFIvWmJrYXpUVXRSS1oweDI5aHB5UzhYNHhFVEo2TEJvUVVYS2d6aHJjQlJEUmxYTG4wWXR4bVFGTlNRaVFtaVV2NmhxN2doYUNxWXBWT0lGRm5RY0lnOTBHbHdKa2dVUkpUTmhKNGpIcEgyUEJZQXJjT21ucm9JUDFBTGs4Ui9kZGYrOTFwMFIzVlkxcjNXSDlTQVRhcUlhZmJqUjVsOUI0TEI0b2xhZ0ZxRUtWdHNRV1hrYm1JZGJXQW9EbXIzVi8xWk1jU0hxMmpXVStqY3VXa3d3b1Rsb1htZElPSkdLa09yN3BrSk1FalMvWGZ5YXA2STc2MnhnekVKSS9ZT2dReTJraW9PcHcvQ3p2eUs5NGYvSzFrNEQ4eGJxYklvV1lnUFVyVVZPRkVsZWZYSTF1RmJsUTJ6cUVlTjJ3M3YwRnd2MEVVeFRueHpiemVNdk9BQlhxZG1NTHhkU055eVNrZW41cm0vOUtyak5xVndZQlY5dEExWEZReG9hV2xNYzFVWjQ4MmpNcDV3YWMzdENpRUlxMUs4YW9Xa0pobHc5WWpPMFNMMG8wVndpaTBVaG9qb2RKOUNLSklsZTJSQlZuUVl6SGFMTDB5cUpxZExqNHROZDhVbVY4TklBTDY3NUdCZDJ6aXE1RDBTV29wNFZCZXNyRGVhbElwSVZYMUx0SG92Y0xqalpJQlFEUm9mVHpWVU5pTFRJWk9PMFZZcDRxUldJSmVpcHJqY3p1Q1ZFWktFRkY2VGt6VUFJY3pGNUplR2QxU0FsWktpZldicnNPV0VXYmJaenU4Z2VnOTFvU2JyN0VNcFB2UXg5Zno3TVo2QWhRRlp6c0hwVW1DK3J3anJDcDNwZGdlYTBXZ3hWdUltVlJxamU0b1RLRkpCOGhyNC9IZnl6OTJMK2l5K25UUktRMjU0ajFIOFZzekRjYlhMUkE1RWVjMzlBbTJSS2tCeDE3enFEYzZtcFNjc0pmZnNhaHlmZVJuRUV5ZXBmTjhZYkFObTVHYUxwdnRhUFVXV3JqWFJ3dHZhNmxpUVJXRjZ6d09JQ243U3dGcFg0TkFZSU96UmR3d0ZZYXNQZjdzc0EyK1BVTk4yUlNCU3J1anRHbmNVZWRaUmhKUHhvSGl6VVB5RmtDVzlYSVplZ2hCakNzZkFYQ1MxaFFJT0xxODREWXNXZlZlVjY4ZTZSRFJwYUFSSXRSMWFWekthMktTaWFHWkxUVGNJNEUwN1pDejM2RVhIbm0xR1Znd0lVL1JXbEVBWU12L2dyU0llT2s1TUpoQUpGTHRtZ1h0UkdDZGEwSXBRNWxiRkhpeldoQm5HckNWVE0wcThwQWJadlVFSXdCMHJad29xZGpjVi8rVTM1eHB6c1l3Vk1yZVZxcTRxZzdPUzA0L1J4RDBUQnZSNVQ5eDNlY3NES3N6Y1BTTGh6T3crdHM3c0t2SlFsM3U5b0dsRmJ3cTlnQjZCbmVEQ2d0V1ZpMjNnQk5EbldzclVWcHBnY3ZBM1hoSkZ1ai9tUVJpODRIU3Y3di9oWEhuWm1WV3ArWEI2NE5NREw2cjlmcXAxbjdaVlFXQlNxOVRBa2RJSlFqRXRlVmhCek5ZemdlTUZDV0lITXFlU0dMaE9wTUhXRlRJVzBFVXdqa0Vvb0JrUnQ3NEZpRGI5VUV3c3BRQW94bFRBNFJzNkhsR0ZKTlM5MElJMUF5ckV4cUNlNWNhL3gwc2VwMjcvZmJCeElCZkFiQTUvTTNSM3NlK0hvY2ZKWGZ4ZGNPeDNrRnBtaXVJalBWQmp2RVNtR21jTlNSdkRNbVNkOFFaWWx5NHpqeXlCelFFOGhqNnF2a1luSVhjMHdTdDBlbFJzT2F1dDFiM0lsazNKbzNjTExLaExRT3ZsbytwV1A5SUt0dW5Hd3dGaFlXMFZCWkFoajVKdlJqMHhEckk0SDRqeTNMc0pnTE5qYWJ5S3dBS0NLUyswMXNxUmNpNHpJMjl0clE0VW1SM3dWQ1c3SjIvMWFhajVZWUo3cFhxeFhZZUdpbkZiUy9ndU9wUXNBNFBKUHFJUlArQ1pNTUVzUE1TQ3c3S3B0QkYzczRtNGdTeVRBTEMyc0FoRmlBeHBKNFNFWUhrbElzWlVscGUzbklWVTRCY3VZVXoyZFI3TGYwSm02NTdIaWlLanZGV2N0bFhndksyaGhuY2xwSFNsc0NlZWZCd3RKVmRRMzdraXhIZFBhU1BqeHRYOE1mZXlRMHNxS3hDS2tBcVlTYzErc2JkUHdhRjhLY1VPSUs4d0JpMHFla2UrR2hLUGRuRmJEV2MyRWF2VXZRcEs1YUNLUjJXRVg4dSsrcGhiQ3dTbDZoQUpzZXlyQUhuSytZZExCbEN0ZVgycHNpS2FxQ2dZclp0WnNqd3BwZWgyMUNOODBrVTV6bFVZTGd3aXhad1JWcjhXdFF5aXNPdkxsMkRnMDlZdmFDbTB0STFsbFVzSXRRNkFYTTNqZXJjN1dCcHdLZkhJbC9Fa2hlQ2FkRGFpVlpiV25rVTM5RzRHaUZSVVZNa0V6MHFRb1RNeWhGR282R0Nncktxd0tnNEJteEpDTkJ1dUR5cHcxb2lweURCakFWTUVzSlErRHExNUpEZElKbzZjYTNLTWlya3lRdnZSSlN5bENKRjkxQWtyTjF3VFEzdmtPR1hvZzVmRHdqQ1JLOWFaU05mWjJRUUdQWVlCV0pTSldWRk1xeEF6QU1OS2xVQzR1Sk9QaTFadTF3aVRVUWgzMk1sOTdIZkxtT3BqcWx2TUFBQkJjVFp5dy91UlRrUjV5TW9wdlFxd2ZPOW83STg5ajQzQmp3VWNMbjBFYTYwVGdQUlNEa0loRUJxVUdzZlZhMTF5WFZiNFlreElRUnRrOFltUWQxYVlWT3NSYXZLczVrL2lSUlNYRHFRd1hhTi9LSjlyYWZScmdoTnhSODdTNGsrTFRSdTVWOTVkYTVHSlNZellxTUJ5L1F6UzJCVW9aWGlDRndwRW1XaEppT0YwUlk1aG5OWktRRDFuTlErSnlLdzR4SVFiWEdTTC9DeU9vTWkwUGo1dEVKa0VtRlk3UTJXZkdsUXpqczlaSmJRS1pPbWpNeUc5NksyM0hHb0dSTk1GUzJYNWRpMEpETWRreUNvdlViaXl2M2hnQTNXbUZ4bmlPMGFPck85Q0FhQTNjbG9vMXp5aWdBSDBISEoxemVPY0gwQ3drY0pCcURJbjBNYk5ibThJZXNCL0FyT0lvcFJZWU5VOXZqZTVvZUd6YzZOWFZWR01KWjJHbzB4R0lwcnNrWTJWUGdPZTFFbDZpMlZyaWc2SEFpVnFLS1pJbFZmRnFYRS9KQ1dlcXg0TXFFVnRSd2xuZGpsMS9qUUVHQ0kyT3F4RmVDbURGU0JlNkRLWkNzaEJkZ1N5N1IwVWIyRjlYZ0JUL3JRaTdvZmV6d01jc2llaUtxYy95U1JGNmwySm9MYndlUkd4MjlOaVBVbEZVQXU0MlFuMlYybGhwQlU2VVcrYlJZSnNLWVVWS0RuWUw2NmVrVmxkcmtXUVIzNzNLUlR5Z0RtMXRBVWVPZ0JNRExENGZrcE1wb0JlWlE2blF1Z0owR2VoeTVMa1JrZ2tMbUltRzRLYk4xZjR1SUp1Vzk5WmNGUkhhclhwSlFiUmV3bUlENDRjK0ZKZkFxNktsVGV5cmFEY0FkWlBkY295S204cUJ5djJHaDkybUptS1hUb1loeG9NWVIxaFRKMFNCcStLbEdpNmk3czF5MThnb3dnaTAyVGtldzVJcUtHMnhRQ2ZHaTZETzBERzJVVzVSYWtZZTYzUXZqQTB0R1JNQmV6ZzVHUUN1K0FSUndnbHlyQ3NCQUNzVzI1ME5vdGMyaE1waTFNQVl6S1hSd1JTSG1pb1hvcUNzSkVUQnF1VXRiVFZzMWhzOFJXeGRJcUZzY1V2TmhkY2VEekNTZXNYU3FhcVRNUWx0SUpzaE1NQ0FiY0lZdEd1bmJMcFc3ekFpZHBlYkpOL3VMTHJyb0ZJZXlYNmxGampWN0oySU9GaTVxQ2pzRkIvZmxyZHMzRDBNNVNIYk9pVnJpbmdhUThVVHdtM0JhS0dNUkowQVlvR3pkWmpBdG83VkZ4VWNzWk51YVpBdFZWN2JCV0dvQUxjSEVvbHRVVVZZS2hub2FjM0JWT3FRUzI2RExnamNydzB6cWk0UXFUVnhNYlMrVFFzWVdvRHdzVlVySE5xUmlGaXNXV2FsTmFOdG83MW1aRzhGU1IweXlrbG9vZTIrRFRBZU8yVVcyU2FyajFlZ0pLYTZBcUFscFNHSlhNNDlXOWJyYm15S05kV3JCRlhqcXdMSFpaTituQmxYbmNiSklCUHIrRDFXWWhXQWY1ekJ4bWNXVWJlbnh2cXMxbmJrOHI2SGRSWEtZY3doQU1Gb0VJbGRJdGhZQjRZWnVMYXJ6ZzJVcURvRm55NkxBRndUWEZUYU1GTDJFSE15N3FJSWwvVUQxcXNqQUZaRUo1VUlLZnhIbTVrWlZDUUZLMFlrNmZoR3RiTFl0NkJhTHhBR2p4M3Q2cVlUT2dxV2tBdEJxc1NTTXRpeU9TcHVZSXNzdnA3dzZrVWpyQ3lYSUxZRUhoQnl6VkJFVTBGZFFsSWZhcHd3eU1haGUwVi9LcmJaaXA3SUdHc2JmUDBBY2QzaEd2ZkU2MTMwcVR4Z1BGSUZYcHN3VWFHZllzTjEyazlGMHJ6SkJtcm1vZG85VmptQ3FpWnRwVzV6Z0pISFZIZkhNRFNGR0lFZmQ0ZTRnSlFNNmdYa2RoSnF6UlgzWHp5OVpiK0VVTUowMVY2b0FkQld3eHVyUkhaMVNuVUdzZFFMMXhxbW92RFRDZm1ZMmd0YTljcGVzNk80VThTbC9XOXJaZXBzY2pSRXM3V1h4OFVLNi9DS3E3bWFXRUQxM0ZWRHFobXNBS0xFYkdoWWdVcjFWb2lpdm43MlNrRjd1M21qWG1BRHh0dEpiWjdLSWtUWHR3M0NzTGszTFFHN3FreGliQVYxMVBFVHkvd1JqWE91bktVMU13eDNVQ0ZMSWROWFB0SDdmYUlCQ2dDS3hnNEp3WFlZWVFteGJkRzhGWVNvMGc4MktDVk9RM1FEb2RiR3JmQVB5aUh1K3RqVG9saTkwRHVvVkQ4ZEFSTHVVWlV0d1h4M2FBVkVIK1BVSXZjeHdVTU9YdCtiMFpqRTBBMlp4TTFOWVlpTGFveU1KVEIxUkJFT1FHczdoWlVKeFJHaXdWeU05Nmhvbk5xZEFZUWF2VTViSzFWUHI3cTl1bEpBTmRUVmMyejEvTlJTd01NVkNBUXNNako1N1JwQVJqZGRxYmRvbkk0VDRueW8zd0dVMlZHd2RuNFpTcjBJRmdhclFvdUlGZmR6UkUwWFJRczhNRjVPSUdud1JpaW95bEt6M0RYUUpCWTQzRU9VR1p3ZUpJL2kwaDFNMFlrVWRHWWtUR1RGRmtndndVbG5Haklnc0dlQnFXQlRpeDJmYkg3MzRRRzNxRklSZ0pDRTI5S2FJMDY2NmdxTm1qY0JsY1VJZzJQTmVGc09GMlBrNnlBSmVyakZKS0g3QkhlSE9PMWU1VmdBMEJrVjFGdGdoelU4eHBWZEprZTFJcTVYU3NtZytUbzRuOFd2MFFBMVF6UXNSZnpuL3YzaFRiMklmWHpDY080T0Q1cVhJTnYwMm5xelJXNVk3NXI0UHRSQUxTOUJkV0JWV2habmdSVk5DNi9pMVhFVjBJaUVMZHJ1TlFKdFRVME4rZFhPdlI2WGhnV0pwS1prcXlzOEVUbWZMVldGRVhMYUxkeEt2R1d0dWR4REhKQ0FOelpOWGFXZ21senRoSXJjbHFGd0tjOWJ2bHk5N012UUZDY2tubVJSQ0FTU01KanVjOEhoaVFaWVg2dU1WVy9UbEk2R1Z1NFRBZnJXVUtVbDZGa0R2VVU5RVprMmF3Q0wxMG8xUWdIVlM3SDUrN0NpcU83YWtyOElPeDdBS21TRTJ2cTNTQ2ppZzFVUFVGUE9lTisrMWdyakVDZmNuVGhoZDF4RUJTTldwOHE3ZHFJL3RrNVBDYUszandPMjFDWlE4YURIeEVhL3NUTFhOVUpGL0hVUHE2dnBVQTBVdFdKMTFIekVxc1NzM3FDaHhRSDJuVkl0ejFwT1ZlMjk4Wk9BK1J4RVdxcVJhNHFqc3VTYVdSTXZnWkZrQjhTM2pJNjFNNlRtZjZHTXBCSkFhTFRHUThXcDlIcFpXblJGUGVrdElrQU5vQTViYTVDZHFZNUI4YXBLcU1XUDBIdHJEdnI0eDRrNElBRmdUTXp4Ui9IK2JnMElpQ3V6eE9GU0FNb3dBWjBIREVOSmFVbmdCNURiT2RtSml1Y0luY0FPd1NiRXgyUDlyS3A5Yjl0Wkl5cFMyam5ZQ2VqYSt6WlF1bUdNOGJyb0JEZUJIZUUzM2RqU21PWG9tV1V5VkRLczcyR1hQQW1hSHdkNmkzYlJtTktJQ2k2emlpc0Mxek1IS3M1b0xEeEJqU093MUFVYnBlS0R3WTgzU0thOXJacGF1NnEzZlZqQWQrL0E1RXNlVlNNM2w5ZEJCT1NpOVozS1lsUzUrWkFURThScTZjclNWQXd1VHFQSDhNZ0dTbGRnbkNsTFZYa1EyVkJ3QUc1dHJnc1VHWEFVZEFFNHh6M0hwbkdrYW5xd2JYQkx5d1Jxa1ZLbGR1RS9HT3NRUnpjVXVUdDZZdXZURzJBbGcrOU1KZFVUVzVCcVQwY0tjSXFkMHpxbnVtMXNFSjJEeVN0VzVzWWtXWExKNHZrSVhGQklvdm9DOUI0N0Fmc2FwQU1RQ01kVDVkN0xDaUtaRnh1bDNvSE9ZYWtJWFFGNmgzV0M0cjJKdnBoMzhUdnJuQncyNGU5NkI2TG9pMWlnd05qZ2JnM2hCaDc1Q09SaEMreGN4Z0phWm16Y0trMEFGNFpmYVQ1WUljenBuUkRqK1FNTFJVc1Rxc0hKQ2l4NWRCeUVSQVJNbVlFMUNrb2sxUkhqSnUzVW5iSnp6d3NQVURXTExRZFZkSU5pT0hTUS9yNmJRWnNJcXZpajR2MVI1VjZoUzNRYUExRTF1Z01aTUY4dWRZd3JITU9pS2lRYXpybExMdWFLYVFwMVlKS3FJeFBxNEtFUVB6aXNyakJrTmI0SWZDQlY1Vm9vQkFvN1pJQVpDWTVPT2hydi93RitLZzhJQUZpNEQwaUVPaGs3TlM0cy9GSGdndEhCMk1KeHFqdXdES2ppTUhvWUgyQmFEbXhtbzlFSXhCQVNNaTFWY0pGU2tSV0xyNzQ3UWV3c2hBZXc2dDNvTWI0OU9aSkpaaDREakpOZ0NURUxiODJRYnJvcHJsNEFQVWdXb0J3Tnl6d3dQZmU1NENtbmlPT01TdUdCckhQRzhZVU9xQnBRMUV1Mm5ZS3dHUmNyZjIwS3ZhUUJWdVZuOGZ2d2hESGVSU0pkaVlYc0V6d2ZZdi9jWnpKTkpqRzRQUzQzU1FTdzRoRXY5VmNmUlpvdFlseHZheGhpQ1pvQnBlWkVBbEVFeEtEeENoNEhZMXR4UlZVb2lxMEpzRVpJZWJFVVRJYUMzQ0ZTcFJCYkR0Z1c1bXluK00ybVhYVWZmYnQ3QXV6Y1BpV0tZU20yOGVrOTROWHh6NnA0R0FBQ04wWHp1M1ZBWElnTVZFK21wL2paVXNBWjIwNERzNnRHQ1laQUlZRDltcDZrY05MRmE3WGY4aUtlV00rMHJET2dMSFNDSmNBN1Z0RUQ0Q1o2cGVnWU5KM2tnL3pVdlNqdi9yL0E0WHVCdm85QVV0Vmpjc1ZZdFp5QmswOEduL3JsMU9GN3dCVUxUMDdBcXRlUFBoUXBSQStJT3FhclJtaUNKMFZsSHlyTlJsdUc5YlFXaEdxRVNPNzEzRkdXWFQ2Z3JFTDJEWmRGMEVyYmw2SnhxOHNLK0xWdlVvY0VkbFZqcUNvcVpWTmZOMUdzVUpzb0ROaW01MnIzZFVobFBiQ29HRFhTZ0lHd1NkYkNKaWk5MW9hcEtKd3FMUmZXVjkrejV1cGNtc2xTeHdTTEd3R21tS1NhWWZlZVlHYjNZWUQxY2NyRVBvQlVRTW9zd2sva00zQWdlUlFRU2F3NVlBMHhYZ1dqQXJ0YXNacUV6b20rRUpQSUFka0puQlNpSzJSWFNDZ2NEMURMMmUzN3FwMS9UUVQyb1h4Ukoxb1gzby9KZ2Q2QkhtRW9LUXhkZEdodEFqdDJOL1hPdHhNQVdVb2R5MjJ4c0JJVlpRQ2c3L2oybWdsNWpQbnVCRy9jYytjSWp0a0o4MFpIMW5NU3grQXA5SUpNa3BJQ1hvczJBU0o1R0kwcGFuVVR3UXhNZStqNHJlaWYvUlIwRDM4WWtBdG8yNzZGck5OOHV3UWZSdXBQLzFLSmU2QXlxejdMd3dnRFJHSGxYY0hxL1FMYnJMcTkwS0RWaXgyRmZWdnJFTGhkVkZEZU5sMVh1RXZOQ0t1VjhnU0lPNDVTcUZzR0VOVncrQ2tMdlNJVEN4TGNUVkpteG5xWjNRRUFmL1VwMVRBMUI5eWE0a001eFFzenlWTG5ZRmZpd09vRlVRMHhRY0NEclVwbFZ3S0hOUmM2cDdvQ2RCSlRBWHVudXREc29YTmc0Z0FTR3Rra2dNV2owYWZlYUdCUDkybDliaGRmc3NnQjJUc3dFZFE1MlZYK3VSTTVpWGw3TmlITHExOVY5MUpDWHFwWDlicFlJU1V4WjNXUGVBenhEZC9pZnN1ZDRtU0tZRjBrZFUwUlV3QXJyczViRVNZdHcyOFZNRFNwR1RMRVhBdVBKdWxTVlhGblFGbG1Fb2VCMkZFd2VjbVB4VFZmNHJwTFI0SWFrbjE4NTd1a2QzK0VtT3dBRlZLdit0Z0d3VlRiWWlOYVdOWHd5ZUdnWjNJY294YUdxa0lFTldURlcyR294VVkxTEd6dlV5ZFlhMjdXWWdQaTltNlcydjRhLzIwSlJTR1VLT2d3b2tkR1I4ZUFBUjhveHo5OUVYSjE5WTBmUGpwYjN3UmlpR0dFMUFDM2NrMlNRODlXODdMd0FpMGt3UUFrSjFLd0hVeFNDRGdWRUVZU0xMSGxnRUZpYjZlWVNIVkNhbEJyZ0NYUnVuRHliTnFzRGpFUVB0SUFXSXF3QmxQTnZ3RDRDSjZ4Qi9ibTE5UHZQU3hPZWkzVnlkYWdIZ0JtWkNuZ2ovODRlZWFaNEQzM2dLdGRlSmhscm9wUWNiS2xFYUc2V1g1Vk1CQVdLRWs3VHFUcXFTQ1lMVnNXaUVtUGNzK0hnWmYrTVBDZ0J4akhITzJpYkpSTjRCZkdTRG1IWC9zdEpFeG9xYlZGeHM2N0dNNFVZZERDamRXUTY4dkV1bzE5VitwYWVSMUdTYk5xWE9GUGgwVU1wYXlnQXdFdUp5Rm9XZVUybkM0Z0lDeGRZZnVwQXBzSU9tWWlJS25RQk00czR5N21HUUJjODZrTXNEMXVuUStMQlJRVUFtUGZjajN5Z01Scjg2ZGlhVVNnaEhWVWtneHFGeWJ5cDhpbVBWV1lKZ0ZJb3FXb3dJcm5pa2ZWKzdPZE90VnU0OGhpb1E1Z1YwTnNjbm1IQ3NuVVJLUG1ZcW81RmszVXJoWFkvRTdaLy9vMUFHQ1Q0TmN1WTFZRlRvZ2I5cDhDdlBKVlVrbkNiQVpNKzZwaUFkVkp5M3d3WmxSVTNCOVNFcnlMQWtndEYwMjE3cW41QmJzS3lWREN5b3J5eHo1TS9PdHZ4UFI3dndjWVI4RVNvMGlvNGpraGRPSjl4L0cyMjZEZmU2TnNlaHFnbVVLbDVHaXB1YUZvTy95aWVxTlF5RVQrVW1DZHdHbDNRdUVBc08ybnFBYWduQkdoekN1ZUhqQkxFRG1WUmFuc3NDMWR0Q29jWGxPQVNGaTlla1YwcUdOOHFlNG95K1lkSzd3UkFDNzloREZ0U3dQOHF5V29veVBaeGdHcEpIU0ZObld4RTJJTVd3WjZqL0RVMVVTOUU2eHJWYTh2Y3pGVURCQ2RreDFxbUFMYW5CZjJvRnhjem10Z0MwTXREdFQrYXdMb0NqUVIwQXZzU1hZT2RVSW9vRDBNdm5laGM0VndGVUFld2ZQM3M3emlGK2hIRGtkRGowZlhtbTJuVElERjRoYys5R0dHcTY2R0RtL0lOamFsTHNIZ1NrbGdBa21KWFlGMVJkYUo3TDNob0pIN21vQlVRcW5kWmNoeWZDWjZwQytUbnJqcHI4aXZmUmE2Ly9wZlkxNkxKWUJCS1o5NFJkd2xKMzMrMHArR0hSY3hUUlYycjlHSXBhWE1GdXJyQXFPNzFmQVlOWWJIcElZK3dYWk5XVThzbHhlOEJtVUFzbEVxMGRWekl1VWZKYmxRTlNrT0lxdGFkaGhxdEdHR29TNHhRZ0FRSjRFbWxRU3pvNXdmL0lzejhrY0o0TXJ0RlAvakRmQ0tXa2YzL2RhSE5sRnV3ZFRKcmloTlJlc0xQQlVVQ1RaeHNsTlFaSDBZUmN2UDJFWFZUd3ZEWEZhL0JqQXR3N2NqZ1poRXBjUjJRTUdMeWFzRWlRRFlXOVRKUGVKclVyRy8zbUZkZlk5T3JBQjFmTlZqTWJxd2V3MXA0eGJnbC8rYncwd29SYWhwTTBOSkZXSEZramhtOFFsUEpsNzFPcFJqQlR4NEdMN2FBNTBrSzlVYkY2a3ZWRmVnRlBwQXM1b3ZVV0NYaFJScmg1QUVLRWRiZ1NYZ3B1dmczL2kxNkYveHY5UlBKblZlVllWSDFCcThDQjh6MEhWWXZPczk5Ti80WGRuT3M0V3lXRkplUkNCYmJhMGxLeTZJaWtseTJUQldCSWpKQ0Z0YmlieXZmbWdQc25jWnFqVm1BTGtDczl2QWR2Tm0xVFJzRzRBR1lxbFBOWGkwNkMyVUdwUjZ0dlU3eEtHMDJNTDExeStzOFF2M1pZQUU1QWRnVjk2SnJUc1crVmlnOGxZSFBVb2tVZWFSaElNMU40dlJTMHRNTEl6T1dmVitVWU5YbkhBSlFYUXc2eWoya0MwMkt0bUZHREJYazdqRzQ2R3U2VjdtbUVsZ3I4QUJ0OE95ekJSdXJZVjU4MWpsdUJqQUI1d2t2dnhuaVpzK1N2WVRJV2RZSlJkUkVROHp3THVPekJsODdKZVFiL3dUNkg0WFFSKzZHYkFNMjlHRFhSMVFZZ0FxN2xpeHZzQU5VNGt5cythajFoazVuVUxIanNBUDNRSzk2RWZWLzhadnhPdVV3cmFVbXlIV1BqRThCdlR5QXorT3liaUxUTkZEWkdIc0xVU2k5bjlFSWNUQUkyTSt6eEtyQTFIQ1hLZVRXc2hHaFYzbC9uWDhHNGoxT1R2RW9LRlVQUndiMHlGWklIS2xHbXlEZCtMQ0JSN2c5VFlTVllIcVNRV3FCYUZIOTBFQXlIanhKNlY4SC8rREQ4U0hUMGtmQmhuT3dnRHJndU11aXlKbmhLUjJUM0JaRU5RYXZhdUFkRUFURmZkVDIvc1V0RllYTjFoWnpMY1YwWEN5aFloR2hmUVdmMjgxL0FZR0o0VUJTbDM4c1hjZ0U3ZHpRak5GbnVxQzllQ3VPZlRpT2gvR1M0MHdhS2N6dXNNSUlTVXdEOEtETHdMZS9HYjRqN3dVNVpqb3Q5NUdsQm00YXNDT0NXeWFnTjZnU2FKNkUzcEt2UUVybmJoekJWcE55TE1ObGR0dVJIbkVBNEUzdmhyZFMxNEM1aEZ5MGxLU3NhVisyaWExeGd4MkhmSi8vRm5hTlg4TzdqMEQ4a1ZnR1ZaYkFWcUxLeUhWZGdTRUlVQW9hbjNMd1dsNEZHV0JNYXJxUmRuT2VaTlg1bzBCZFplVUFscUpPRnlicnRUeWxhcm5FZXY2czZwMFZCdU9GTEx6d21vQzZNR3lnSEM3YjcwVkFLN0JOWi9lQUs4NVdQbmdNcjdadTR5dWQ3RjNkVk9ISmFlOGhFSzdpN3lRTGZ4RitEV2tnQjZpRWc0ZEdsTnd2MHBPZGc0UEhwZnFNN1YxWkJ0UmIwb1NWRTBKQUt5dEFhdUFWaXEyR0dFMjhMV2tlSytKRXpVY285RnpuWWdld0pSQUdlaG5uVUpjOTNxV243MUNlVElGeHJITkY2b3E2c1owRStvNnNoUlNodlQ5UHd6OThkdmhMM3l4U2pvTnV2MG9lTU10MHIzSHFOa0dzTmlNci9rNnNIa2NmdGVkTEIvNWFLd3ZPKzg4OE5kK0VmMmIzb3owaEMrRmpUbnU1T2J0QW9vRFc5dmNtTUcrMC9obmI4ZjQwcCtVN1gwUTVQUGdicVBVcmd4TStKVmFBUWNlRjVoYlZPZ1dHejZOTGxaSXBQWitSZGRYTFIzcWVROVBlV3ltZ2paSXhSSGozd0kzRCtWZERkNVI5REEybEM2bCtRMGJqSklFUlRzd29xL1Uva2EzOFBkMXg2OEhnSisvanhtQkh5ZkhPblJ0dk5KZkxjWmJING1zdlIyN2dHd2Rsb2s4RWxvVWNJMlJmb1RxTWZLM0pTb1YvekpWWEtIUlZxRlRDb3RQRUNhRm5COXBCMFJVZEFsT1JVOFRwSjA3d0dtS2dpZUhDQ0xDVDRxeXZOS0VRa1JEYjZxcENrMlJ3ZTVwbUlzUE9ZMzR0WitpUC93SnNDZC9oVGlNeEtSdmtBSXFwQjlRS3FOQXhqaWFuWDYyOEVNL0luei9EMUh2ZUN2d3puZEJiLzhUK1BVZmdTbEJwTnhFVFJ6ZEYxK3E4dmd2VlhyOEU1a2U4ZkFUR0t3QzcxSXcwb3BCSDZSb2loa1RMSEVzZnYxTkdMNzZlZWpLNmNDS3dMRTA4VlY4VElaS25pdzF3RFdObUVzV0tXQVYxbFp4ZElIWlNLYXUzdVZodHFHYzhhcXNnK3plalpqOUhDS2xtbXV5Nm1tcFNwbFVTVVd1WjBwTmhhdFdlTFRmcjdMVVFKZW1kMmgyejRkT3Z2T3R1Qlc0R2xkL2tpTG00d3p3c3BvYTNUTXUvdnpJc0hiWDNyVjBoa3FSSlRJbG9HUnczQkQ2a3d5UmdWWTJKTnozc284Z1ZDeHhsZ084WmJBbkFtUWhQbU9Ya2JhT0JLYnZsYkJITlpoS3gzSFhic2hTbk9jKzJLQVF0YUttSjBKdFhLamFIOFcxQ21GV3hkVzB4SGU3Kys4aS8rM1hVNy82eC9KSFBFWTJqa1RmUjY5aUpGL2JVMHNCcXU5bDd2QmNxSzRYbi9CazZBbFBwdkJ2RllNalJuQXMxbHRTbWZZUXdMN2RFKzZoM084TVVJTFIxZWpBSmR0QjBJWVJtUFFxOXh4aC9tZmZ3UDVZTCs3YkJma2NzaGdqVzFVcjlXSzdESWxtb0NvL3lnb3Z4YVlvVzFaWVJKYW1LMkxxNG5LUmJWRlY5YWxobE9Qc0tBd3B2R1Z6Q0ZBN1dBTGJVSG1GZStxSHBCb3lHT1l1ZEJCWGFoVXpKZE1kTnJ2clQyODlkcXp1S2ZoRSsvdjRFRXhFQThpVnQrRG9vYkVjWE9vQ0kvK1NLSlY1M1FhVkdCUHBJaWtITFNhL1c2cFhJWUdoZWtFb1ppaXgxdXdCMmpwOGRpeE9iTDEzNnRFMGdCVFlkeG93WGFseUx3QTlvYjdSWllyY3BwTUNyNHU4c09XSVNJQW55WlBYM0xIQVZucnBuRUg4dHFmVDN2NFdvdS9Cbk9QK1lVVWZLcGl0Z0w3Q1JmVmRhQ3BLQnNkQnFjUWVZM1Fkc1RxUnBuMU1qczBaR2tmQ1MzaVBsTFpEUW9NREV0QTYwakFNMEtRbmJyME4vdlRud0Q1MHAyemZxVkJaaEppVmNROVg5aVd1ZDRnYjR2dHdpcEdYdDFtTVZzRVFBd3BHcEZOT0JicXVVV1h0czlVWHJnREVzQmw4UTR5L1JZVmFHdVlYK3NRbDQxSHpROVRaa0hEVkthcHl1S2FRVmtJSTRTTWRteXB2Qk9CdndwTTc0Sk10OEJPVFFwWW5oMWZNTEc5QWw5Rk52TWdjYWVxMHFhS0RZZTdDcWl2R3BiWHFWMFNIYWhpUkc3SUxlQ0xVSzVVL1RyWFptNEJtVzh0Ykc2ajRGeXIxREFEN3pnRTRDUnFyWjZVQ0FYWU82eXRIM0FVUFd4dUtxU1JhVjRXcENVUUhvQmN4SVlvS2JQY084THlGOU4zUFV2bWpsNk4wSGVnU1NxNU5pNVgrck9jN25GQ2NLYVZFOUJNb3hXSkZOdHhYMFovR3pzQytkMGNDRzhaVEg0b1dSMWhWT3BWaEVDWVQ1UGU5WDdPblBCWDg2SzNnNmFjRDJDSmJHK28yazFSVk9BNHpNVlRobFgrbmFydG5oQThqQWg1S0VqRVhMajR2b25nVEhrZ01Fam5BZEFmZzgzblVaNTViZ2xyVkJhMzQ4QWE1b0k1TG85REdkZ2lBbU9GMGpOakpLZzFEMFFaR3ZLZmMvU0hna3htUVQyV0F1cUwreDBmbjQ5c0hRRWlLaG9tcHkzcVJIVGh1bGlncWxvd0hxbHJGaVU2UTFTMTJBWjNFS0lvTzlZU2lNaVVBMW1OMmNZMlNZSjNmMHNiZmFMcExUSlgvYlBSVzhrZ2NPZ0Zkc0NCS3RlRzl2VzRZZk1Vb3diWjVtWk02Um1uWER2REJVL0xGM3dEODdCVndDa2hkekh1TzB4NkRyWnNrbkN4dDRTdVc0cWR0V0pka3JPcnlCdTg0Y0tMMXhha0ZKUGs0eXJvRVRDY292L1Z5K0pjL0ErbVlnRk5PQTh1TXFnSVFXRXlVcTJMWVplTTc2RkhVeG8wdHExaHJHOVpFVm55VkJMRkpuSDlxUk12U3VGNEFIdE5KMUJrOUE3cmp1SWhFOHhQSGVrVHZyMUFNOExyVEw0eXdWc3BncThjaGxJQmZzSW9DcWNEQXlXMWFYOXplTGY0a1B2KzFuNVQvM1pjQjRvcHJvNFBsbm5zbi8vZk9RY2ZRV1hKU0ZsSjNJUUY1UzBDbTJMTkJNRzB1akNvblMwdVFVdkMvZGZTNndtZ2gwQjBUZ0J2SENRQm1WTnpNYm14Y3NBRHNYcU5PT2luT1JSZ1R0RFJFaHFmdEFYWU40b0hZUWVpaTJpTWhKQ3BXZ0lXRUN4MUFELzQxUGV4VTJPOWNDWDNUMCtBZmZDL1U5UXBRTUJzOFF3b0J1MGViUk52Q3NqU25sbXFwL3BTVm1LaFBWZ1BVQVFkTGlaWGpLejN6d2J1UVgvaWQ0QXRmeUg1MUQyM2ZMbUtNdVlSUm1ndE1vVWNNejFkdjVnakJpbnZqQkZFQWExNFh3eHRSWmZFVVpraDc5d0NSRlFOWUtyMWl3b2wxOENQSFZQN3FZNVl3bGFsVTVHM2JFT3VETmRkVEpSWkQrZ0twd0ZGcU9GNkJjd0pBb0s4aTJSR1c5Ny84T2JmZktJaWZ5SUI4U2dNa0lCMkEvY0NkRy9lc2I0MXZReDhsbFpsZ2ZlV0FYUmpXbmVncmVCR1NKTWtJU3hRNkJ2RWVnRFRkRUdMU1pIVWZYQ1oyckVJSDN3MlZrV0NpTXpwdnJjMHg4UXhPVm9Hekh3dk1ONEJKRjRWTk5UWjBBdHBOMFJIcVFFeWlRSGFEMEt2SzY0TWlYeHFvUVNIQlZ5aE9Ibm82ZU94dHdEYy9HVGp3WGRRdEh4YTdMdUFZeWp5WEdEcGV2RGFaQ2lHRkI2U1lSb05XM2FOMXo5UVRXekxrSHJsZjMxT0xBWGpaejBGZjhuajB2L3NLNElFWFNERXZHalhucTN4RFVPdVZ1bVRqdXF6eDhrMjB0elMrR2thZ0pucUFjbFRXL2FWUHFqZDVxaGRjYXRPeERCQm14NEREUjJIc2dSZ0ppVmJsV2UzSVlNMEZ0d0h1cWhHc0lYb0p2OUExZ2RCUnhTbmN4YzAvd3RVb3Y0VEgzR2YrZDU4R0NBRFhYQk0vLzFqV201RUsrbDd1NXByc2RGcHlhT0lZMTNPQXp3MW83c0RReHduV3RkNEtMSEZBNjBBa2wxdE5uRllKSHIxSjNOb0VVZ0F2N2xVb0tWTTdYcDE1UHJESXdBUk42TXJnWE92RjZzU0tRMHE5MSsvREVObUQ3SllpaVBpYkRtRG5RaGNsRzhZQmR0b2U4R0U5K2M1ZkFMN3RTOGtYUFYvbGJXK0VTOUtrcDNkZEhLTUFab0dsQURrVGVVUXFCU3dGbm5Qc0F5a1pFT0JtVlAwN3YrTTJETC80MytIUGZETDhpaDhBMXlUZC82eW9kTHRCbUdhbEUrakUxZ2FCTHNRSE1RcWtBQlN0amoxaGNqSzVNY1lrczlrdmdQakFKYnVkc2t0MndmMFVQNnk5TmxVZDdmVkdIOTk3RTZ3VXhVWjI1L2JxVzZlak5Qa0cyMWNWUUxCaGhiRWNzcUJIeGk0T1VoclYwZE10M1FiK3ZELzZhZ0I0QWQ3MW1hL3FBb0JMTDRYald1QnRSOGMzZlZHMjhhd2VYWEdnbXdCcEVyM2ZlVjQwenBQMU94REhVdVZSSU9wTS9hQWJsYkJzSEtneW8zaFNJcUNSV3I4WDJyVTNtbGlZVkNkR3RQeEtmT1NYQ2RmK1p3TXlsdTFLclpSclVTZThSYUNTSmlDMnBrWVlwelZRckRYaGJDTUl5ZUs0U3daQThOeVRnTVVBZjlmL2hGMzdtOEpKRnhKUCtHZlNZNzlZOW9nbkdQYnVneHRsMjkxRHk3dmFBSG5YTlZRVXV1bWowdHZlUWJ6K0Q4SHIzZ2s3Y2pkdzJqN2hvdk9Cb1JDTGVVekVZdTN0aTAzZGxXMVlib0txMDBScW5xMGl5VUlpNW9LSG1DZTBVODNNRWtqMThObGQ0SmRmeW03blRxQmtLWFZ4WHNQdUNBL0FhZml6ZHlLcHhNM3RxZ1ZHODVCRVJvbCt2bG9iUmpHQ2FvUkdoNVRoMkFubmxFN0JmWXF1dnczekQvNlhzMjk0RjYrdnVNMW5ZNEM4RW5VaDVIamQxMTQ0ZmM5WisreXhtQlduQWQwYU1Hd0lKTGs0T0twL1VGL3hQUUFKMFM5U09RMW5WSkZ4cUtvNVpFM2h1eW1NUjVsdmZKdjh6QXRrN3JFZzJoQWVVaFc5Zi9CamlaVXpnWEl2MFBVVjJRVWF2Z2NMZ2JwVkFJKytQV05tbWJhRTVvdmV3azVMeDlXT3REcVBYQUk3T3ZNa0VwU3YzeUo3L2I4WFhrV1dIZWVKdTNlU0Z6NmNmczdEcFpXMW1HN1ZHem1POG5rQkZ5TjEvWHVnOS93NTdNaHg4dDdiZ0QyN3dkMjdnZFBPZ1dZWm1pOElHQ3FRQjlRQ0pzUUVaTURCMWE2TFJadE1uZGhFQ3pGenNycTVxc1pmUWhHcW5USDMyZ2h5SGZ6eUwydFhkS21CWEJaSlhTSWM4ci84SUJMMk10WlIxTm9La1RZUDhLRG9UamhsVFN2TmlnMFdpRUxCTG9ZSzNKeGxOT3ZmYi9QWDgzb3MvZ1NYZEUvQnRmbXpNa0FBdU9aU0pBTDU3c1A0ZloyRXgvWTljbkYwM1Jwb1c0UTZJUjkxbG9XVStvRHVscTNIRlhRMkFZb2R3SkdtQjFFZnB5S1owRHZTYmRlVDRmT0o2QVJrazJyUU03Qmp0L0NZWnhQdi9SbmdyRk9FaGRkblZrT3VuckFtRTBKWlFxNXhxcHU0MStyemx5VjMreDYxQVIwaEtoWElVaUFIYlcwSC9QeGQ0UmsyRHNObUI0RzNmMGg4NDI5Q0k4Z0NlWW51U3dqVXRFZGFXUW5SdzhscjBoa1hVSU5EQzBtenNjNDNZdlRYZ0VTZGhWMnZleHlQaFVDNmpZaUp6bUcydVE1bWtFcE5LMUZRKzJDcWs3VDY2ZWJIb0xOUFp2L1ZUM2U0VTVaUVc3M1V0czVhU3NoM0h5TGVjcDA2bkEzMzNFNUE2SkxpTFR5RXBxR2tSR1ZJbWttR1RqNXJCUms3VUNDaFRHSHBvNXpyR2g2N1dyaHYrdTB6TThCcmd4bjdzOXZIcXk0OHkxNTB2eld1YW5SWkwvUTdYWXM1cWQ2MXVIdmcyb01tMGlnaVZkMXM5VkNCaFFta3RmNlJXaThhNENPd21xUWIvMVFDWU5haHlFVlk0M25nTlkzVU03NE52TzZYQUkyUnlYUmEybEk0MWpwM3FvSFlYdXMwR2MxcWYwVnJzWXBEMi82WHFtaEQ5WUloOWhETmdzelBEczhpK2drd21RQjdkaU5HTW9YcmhXSjFDbVdLZWN3dWpoUkdFWGtlZDE4UEJ0NGVqZ3dseUxDd3RHQnMyS0VlaCtxWURWWXZaNUhmZUxoekMvY0dVREtTcnBETnEwb0UyRTNwdzgyd0YzeXZkM3QyQWNNQVRucFV4VjQwdW1VSEprbUxQM2tyZldzR2RyMnpMS3d4UjFIczEreGttKzBJclN5Z09ESlpBWkJSdEJ2T1R0QW9GREJOUDhKajc3djZ0QnYrVXJlQXdOWGJvMTd2NDNHZlJRZ0FYQW00TGtPNjh0YkZqYmR0K0o5RUVXQU9DdE05WmdhbnJSb1dSMXhsY0hLQ3F1QUs2WjBsTkFtV1dnNVlWVUJoOWhLd1owcmNkQjM5eUoxMG8xS1lSRzFHUkd3Ukt0bHgza09CaC80TDRQQlJZYVd2eFFTM3YxcHgwWlF6VmJxLzFBaWFaTEhZTXNiRFJ4WE5salpFNXg1YU1TV3ZqVTlLZ3B2RGVnaGRXTFY4Sk1ZQm5NK0JZUTR1NXJURkFoaG13RGlHMEZUbEJHa1lVS0Vsd01BNkJKMTE0Q1lUYTBuZDEvUWxqaWxHUmtaZXJiWnZMMmJOYUxuVE9Pak81c2NWdWVGOGt6aHRsOUszZjB2TUxLOHRxT0QyZGFndWd2NG5mNHBPTzRDbDhMMHhHMEhyUlp1V3AxYnh0dWRVTzRZSE1NM2RGUmZzSVI0MzhhK3cvajk1QytiWDRKSkduSDcyQm5qaTQ4T0g4Mjl1THNDVUJEblJUYUEwamR3RUJzNXZ5OElrZU4rUTRVZDk3cUVKUEtHOUVjdTJUQ1FTS3lzd0hoYis4blVWYjF0cW9DT2t4V1lqVW9JdS8zRmczRWRnRE5sVWEvOU1BY01FdkVPeDRZL0cycUVuV1RWR3Rla0tEYnRjR2pGWTU5VzAzbDh5MGMwWVV3WGFqVlBmTTNwZEZGdGg0aXM0VExPUXJpU3pKWmhXSjBtWUxYSERpUDRCSmd0V0o3WXBKR3gxNHJkYXIzVVViOXdXZExBcFZFSmR0ZXhmVVlINnFiUjFrK3pBOTVNbjc2Y1hoNmU2bWlEUVYzaHhzTzlRMWplSVY3OU5IVTRCZk03WXRCVGkwMmFybFFXcGhWRlFQa0lNSnNwMUk5VU9GS3pDVlZqVXMrdmVhK3ZyUDU5dXVWb0FMOFcxbjdMNi9Zd01rRmVqU09BcmJ5bXZ2ZkdZcmtjSFU0eFA1M1EzNDBaZkFZWjdDc2V0QXF3eWRuWW1tQktzQXRJSTJUb2lNNHVja09ocWxuWnF6Kzd0dngxQXFSbk0wSG9Ud2c1VG9yd0FKOThmZXU1UEFUY2ZBMVlxNWQrOFdmV0FEWDZKeHZyNmV3TTlrZDRiVTE4L2NZZWc2SkovL09zc3hhVU54NHhHSjY5UkVCMHFtRkd4T2F0d3ozSjhNS0s5MXFJb2RhTGhqeWhWL3BscUtoTGNPSmRpM1pqc1ZRMjJyb0V3UXh0L3A5cUxIVFZKN1RFSjhKbWdDakZkZ3grNkNlVnBUeGEvN2ZuZ21HRVdTdzJXMTVPaWxkQzREUC83OVNwM0h5UDdWU3czU2JFeWNMWFFhSGxmRFV3dGgxR00wY3dvS05oWHR5TlJLSU5aK2pCbnYzSDdiSGFiY0puOWRkN3ZyelZBQU1EbHNEKytHNXNmM2h4L2ZwekN1aDdLbE5JT1liSVdUcHBUWVg3TFFFeHFOMVVLVVFEakZxcEZpQUlVWHFxbkhjNENuTFFENWZhMzBxOS9SMkJVZVl5d3NxMXRRWlBpNk10ZUtEMzJtNG5iN3dUV0pvQXlZbFJGWEtRbExWZlhJNGlzUlpERHJKNjhFTW5xaE1LbzVxYXFSaWFxdms3dHVoT1N5OXFDbkE3QUJOc2lpeGlRTGtzaVVvYVNPNUtVYXM5S2JkRUVrMU9wTU5iSkF1cEw0SmxkcVdJTFg3SWZackpJRVlxM2tSL0xiUUYxRElocWd6cVVZU3NUMk5IYllCZnVRditiTDJNeUZ4RHRsSW9HcFJxQ28rSjJRT012L3hhUzlnTStvSTV6YSszcXJOT2ZBSlJJaUd2M29sQXNjRDl4UU1GT0ROekJnZ0Z1cStoNEl6Ym1ieWkzL0NJQVhJR3IvMXJqKzh3TThPcW9VSC9uN2ZtM2J6anNoN0JHRXlBbVlyS1hVb0ZzQmNoSHBlRndBVlpqYktSWDNyYnU4Z1M3TUlaSzV4RWQ2OEJ5UTlvL0l2M2hTd0dFTHVERWc2dmdTcFI0cFVEZjlMUFFPYzhDYnI4cmpOQnFpS3lLYUtaYWpUY1FQRkhXaFdmeENLMUxieFdoVzFYa3lxVW5aT1dzSzNjZEUyMkRsV2pwUkhpdWhpY2FxU1NsQkZuVTh2U2tSaWR2NDVkVXBDZTFoUlFXbWpHMkllaW9OMUwxaUczNFo0Z09vdFFPdUtBU0laUTRYWUh1dXRIOWZsT2xONzRXT1AyMFNHVlNZb2dudDBzQWxVTHJPcFUvZVN2NFp4OVVTdnVCTWxZb3NVMDZjR3hEQzZqSlFodVVGaE1VeHhnd2o1UHFhU2E4b092NkQySDI2bGVOUjk0dkhMQXJQdzMyOTFrWklBRmRjd25TcXpaeDhLTWY4NThSYUgzMDFMRGJZWnlzZ01vQUo4RHN4Z0ZZRWRGYmVBb1NWbWU2b01xUGwrRzNVWGJLd3VsN3dSdGVBLzdscTJIOUpGb29sNWJYVUJPREdjaTBBM3JCSytIMyt4cmc1a1B3M3FYcFNuaFBrdWhxVTIwem9LN21teWwrVjVQOHlQNlh2REsyT2ViMnR4RjZVVFB2YU9HSW15ajRxY2o5V3Rpc0syTGkrZFpWTk5xd3BBRmg4dURCdzNnUnlKRFlWajRBRGNpdnF0TUtURm5nbWhXWFZ5d0pBdGozbEJuSzdSK0V2dVlKdEd2ZWJIYjJPZUF3eGdIRUMwYmFReXduUUFIZzhKUC9CVGJ1czhZUXQzUVZhRUtFOW4yZGczZENTQzV3akNqYUNkY2FwQ3pIQ2p0OEVKdDZRM2YzVHdQQTViankweFcrbjUwQkFzQ2wxMFl1ZVBWYmgvOXgvVDI2RTZ1d1FoV1pzTEkzRG84VElCK1Q1cmRrMks3NllaZWlBV3lyWmxxdVpXcUpmYVFLWjNUaUgvdzROQzdxTmRpV0wwWXdWZ1cvSEpyc0JGN3crOENYdlJSMjl4UThkQ1M2MEthcEpWVDFEOXVFU0FER1dxVUhpbklpY3dPek5qeW8vcXdha3FsNlBpd0hkWWFVT1NZNTEvazNJYjVnKzN4VkNvQmFnRFZVaXJDWVd3QVR2QkpjUUlWVW9yaUFZSFRWK1poTE5aVGlIaFpTSWlmVFVCTWV2bFBJaDZDZitCSFk3LzBlZU1acHdwaGhYVjhuREZXenJuNklKWk5kd3RZcmZnOTYwN3VRdWxPbFBBaDF1RkNyYWxYbFZ6RlZ0UTJwQ0dNc2NBd2h4ZWRlWk5UdG1JT3g3OTdwVy8vblYyYTN2MTA0WUZlM2theWZLd01rSUZ3Tyt5M2czdmNlOHBlTkRzUEVYWDFCMmlQMGE0aGNZNDl4ODRaUmVRNWdFc2FsRHJTb05nTm5xUjRGYUVhSXlPWDI3U1RtMThsZjg1UHkxRUVsbzdaWm9RV2RGaFZNZ2hVWG52bmo4aDk4Ty96aDN5TWMzQ25jZmdpWUhTTTZCQTlyemIwQmJiRWlTdDBFVkF2STdmd2FMY2R1NWhPSEdOQ2luR2pMb0tNcXIyTlUwUnFHVEVDOWxGb09pTVlTT21uZXBVSjhsYTF4eVYwcURxSkVlMUVSVWhaUURJb21Gd0FUdzVDTWg0NkR0OXhLNGlqMUw3NVM5cWQvek1tUC9SQXNaMWdwUXRjUkZxUFRUU3hXWjlnak85Z2xsSS9kcWZKZFB3SGEvVlY4SVNDYW5PS0RlOXYzd2ZnVXBaMkxta3VTQTl4R1pPNUE0UnJFUXZjTzVFZTZqZksySFhmL0ZQSFplVDgwVS9oTW42c0Q0T091eE01ZitILzY5ei95REp4ZEJpbDFZdDRFTnU5MmFwSlFaa1g5dmg2N3YyUUtyRHM5QlpvU2cza1FpcGdUd09yYTFrVm1qMkxnNWd4OXl4dklCejhaeUlPVUpwRkxWY0tnY3FkQkhuZ1dVa2NIWEVkdUE5LzF1N1QzdndxNDU3MUFPUnFUVVZjVE1Ga0JVbytseTNIRkNqeTNhSWZOZ1dMWEhwdVdkN2VlR3pXSmk5emplaFV1OTQzQXE5WXoybU5sVHJqSERDT005YWFwNkpMTGdqNlFSY05Gam5uenpLU05odUQrVGNpa2J5eUFqWmxzbm9HdGdiNjZBM3JJaGRJL2V3N1RzNzhTZU1BRFFVQ2VNOHk2dUpGajhsV2R3MjRvNWtnT29qalJkeHFlOFZ5V045d0lwTE9CTXFoSjZvSG9wUXQ0bkhWSmdaaEJ4VFpnWUlCelhwWFBweU56alVCV0dWYTc2ZlMzK250Zi9vMno5ejVQT0dERWxaOVI3dmMzTVVEb01pUmVqZkxibDlqenYvWlIzUzlQazQvSzZ0bEJXM2NKODAyanJRamxtR3Z0NGlsV0g5QURHdzd2WXVsQjYvT0ZzVFVCdFhBcGpES2tCQ3dHNFdNZDhGMXZoTTU3RE9FWnNHN0pTTmFPdVRvSGxESjNRWVZJL1hhdjc1M3ZwOTM4WHZwSC9oRHA0QjJPNHpmQ1o3ZEYyd2xUR0ZXcTFYT0pWNDdLSXNyRXFCQTZxRVQxUXBEeUZJMFVFbGhxMHBkU2pKQjB5dXJ1WDVSWXFzSFJZZk9ROUdzc01ZRXFpeGdCNUFJTFFRbFl3REl2U3ZORUZhRnNMV1RzeUhNZkJKeDBPc29GNXlJOTdSblFCUThTditpaHJKd1ltWE50UHpYaTQ0U3docmhOS1hnMW42N244SysrSGZxTjE2Qk12a2djWm9pNTB1MmNJcVFQYUJ2dUloWVV4TUR3QW5HT1VRdUFlMUYwQ2dveWhSVlExMDN6NW90MDh5UCt6K0t1bTY4QStaa1dIMzhqQXdRQUhZRHhTdmkxWDVQKzlNa1B0aWRpN2dWSnFUaDEvTmI2MFh2U2p6cDJQMlVOL1Y3S1p4WnRuV1RkOXhFdkZZM293ckxqRHhSU1Iyek5vTU03b0c5NVBYRE9vOEF5aEFjVDBLYXFOUklMTFhpNkcxVlFRREYxYkdZbFFPWHdMY0RSdTJHYjY3RDVJZkR3emNTUm04Q2poNEJqR1JnQmpBN2tBcFVFamtOdHVWeVhqL05nT01ZNU1NNWc0OERhMHdIUEJjd1Y0K2lUMkUwQWRveDV3U3NnVjRodVRlaDJVRGFWcG1zQTAzWkxncHZnVHExTWdiUFBKeC93VVBpWjUwRjdkcXE3My8ySXZmdWxlcm9NaUo0VEY1bVNrQXlWTVl2c0F0SDdCZEVkYnFHaXBaY3UwYi9yeDFoZTltdmc5R0h5Y1ZnMjRkVDlTcXI4QnB1OG9RSThFT2daeEFEbkZqSldVSFJhM0lWTVVoNjdidkpia3p0LzVJVmIxLysvVitHeWREbXUvb3h6djcreEFWNTJHZExWdjR2eVBRL0JrLzdkVS9vM243SFBWV1pLYVFXMk9HWmF2OHVSVmdVTmdKWEV2VisxSm1SQ1l4MzNXc2tOTUNwUHRsNU5JMUczQjhDTVdOOUNPYnhiK0ZkdmdKMzdLTElNa0RvZ3RWYUw2bzhVM1dFR1V5WFhJQzlhTml3U2duVXRrd2JDNUZ0bTJITGcyTC9TN0FLUXlrQ09NeURQaExGSVpURExBMVd5VEhWdDQ1RHI4bTVVeldBWGN2dlVBMTFQZGhOcU1oVW1PMEpTSHVOdXZKWmZQT0dZWURXSUFxMzFDTEtjM1l0WGRKNm9haUVDWWJkVkwxUjdZd01zY0Fjd0xHQ1RxZHlBNFlVL0JQK2xWN0xySHdndmkzclNYZktsb3BXVjUyQ3poMmFBRG1LRWZCUEZDTWVwekpvWTVJU3ZxdS9lMEsrLzZ4bG5YL2VrcTY2L0xGOGVMWmVmRWZaMzR1T3pOa0FBdU9veXBNdXZSdm1EWjZhZmVjNFQrTjNZOEN5eVkyOWF2NlZ3V0VqZENwV1BPL3Q5UFhZL1l5ZHdPSWQ0ejZ5ZC9nb2dXd1dFR3hoQlJhRmd3bUlHSGRwSmZPWFBnNC82K2pqTGVSUlRGeThnU0FZS1hqZkIxSjJua1NoaUdRM2NWYnhCSHpSR1l4L05ZcVEzRlh2STYxcnIySzJVdXFYd3E1MVlPeUZ6clNldmxUZkw4MWc5bHF3YVVsZ0xBT1VZVGNOQVpwWnJTOUFzdnlvM1ZLdnUySGNSWjhTTVpJeFdNMGZGaE9KM2JWaXFveFpaTGxqZkk5OTVEOHIzL0J2d2Q2OFZKaGNhaHdYcVdrSDVkbnRJOVg0aEJxckFJWEsxb3hIUURNS0FncE5Rc05Pa1l0U3FKNytsSC91ZjdHNzVzbC9hdlB2TlZ3SHA4cytpOGozeDhUY3lRQUhFQWZEeFYyTHZML3lyN3QwUGY1RE9LMGZrYVpyTXMrUFlUVVZLQnZaUXVkZTVldEdLZGp4dXd2K3Z2RE1Qc3V5dTd2dm5uTis5NzNYM1RNOW90QXRKRmtoaklTUUxnUVVPTW1DQlpMYUVKY1NXb0lRa0I4cGx5c1JVR1JPYnVCTFhTSUFyamdQRWptMGNGMVVCYndSTEVGc2hEaVlvQ0JBMkZFSjJTRUFvbE5BMldtZlRUSGUvZnN2OS9jN0pIK2QzWDdkVWJGb1p3YW1hbXFsNTNlL2Q5OTY1djdOOXovZkxIaFBhYU11NDlvdTgwc2VZMm84Z2FFaXlPNDBLc3huc0hzUE9OK0F2MjRVY3U5TWR4RXZub3FrNm9ydUp5S2FhV2J6eUR4YnpUVDJTamZlcy9mdUlMTWpuajVyRmpuR0ZlUFlKalVMb3RWUXFqTHFaTVQ5NTZpZFRmYTVlUkV4d1BVakNpMlBpeGFrVUpMb0oyV2lrWUVnTmRaajZibUxZRnFTMGNVVzF4UEE1SmlvNmRLVnVwelhSLzV0ZTg5ZjQyMzhkL2VZSWhrOURabU1KUitzUFZ6YVFiUFZlNmpzMkFUUnd6d2dUc3MvQUZ6QTVSa3JkWjlEY0poMThvTm56d1RlUHYvR21YWnpmWFBrZDhIN2Z6YjZuTnN4RFRjQ3Z2Z241SWh6NHpBMzVsL2F1WUdsWlN6RXhYUlFXVDFCc1ptNUZwRGxLV2YvcWxQV2JPeml1a2pnMEc4M2VqVzAzbjBQN2FRVUdDR0t3TUlEVGQ4QytQMGMrK0J6NDVEdGdmUytrdHA0U2hsbHhGVGV0cDU2cFJ6dkxWUUovaDBEQnJjeTFjdzE2SmxyaUM2a25rdlpVdGRIdzA4cDhiZ1NuQzlyRUdFaFRKZHNVRVUyaUtlRWlvaWxSYjR5S0YxVnhROHlDZno4Sm0zVFlpSTIvL2ovRTZWRmEwSGNBNCtKNnhyVGFqNHFrb2NzUlZKcUVOUTNkalgvUDVIV1hlWG50bTVBN1dwRXRwN2puOWZpMWlvUnpUS29RTjE3aDl6V2hvTzczUWozMVpoaUp6bzhJMW53eUpROGFiYTVQQjIvNXczVFBPNXhkK3UyMjNSNkdMejF5dSs1OG1oZC9sbnpOYTNqZnE4OXYzOGFxejBxaGtSWVozVkdZcmlFNnJKL3BIbWY3cTVjWW5ORENDc0VQZy9lQWx3cGlyZFZvZE42WXUwY21BSE5sNHV4ZEIzK3FjTTVibkg5MEtiWjhBdlFoMG9xNGwyajlTektSSG9XdnhlbzJ2NFlncTFFalg5L2pCdWlIK3hXTktmTVFhMEZTbTFRMzZzenFDT0VsMGUrelV0QXFJNm1FWU9hOFFsWHdVaWtnb2hkVXp6QjFNQkZSRmNITVRDUjB4MnBmMkVRa2xCUEQ4YUtkNVUyRGdKZmlVbTc0RXY2Zi84VGx3OWZnb3dHeWRKSjd5WGdwaUtWZXVUUUc3REJuQW93UFRzWHIwUjFIdVVxSCtRb2VZQU1wdnF6Uk1SeVNadDlZN0lhL2I3dGY5bnZyZS8vbkl5MDhOdHVqY2tBSDRTcjBqSXRaK3RNM041OTc3cG55ckxMUHNpUkpxSFBvRnZPdXVLU0JDRE4zRGduYkw5NHF6YmJHZlMyTE5Nd1Rzem9MZGFwV1c5WDFpUnV6U3NXQjROb2dvMVhZTTRQbWFQeVUxeUJuL3JUYnFTOFYyWEprMzBJRyt0WlljY3pkS2hwVG9JNDlaTDdaMFE4TEhQY1U2aHMxR3hXeFVNWjByNmxZT0d5c2NOUVk2ZjJaNmtCU0RYVW1KWElxazByY0hlL0c1Nlc4enhmeW9lYUE4ODBPRjQrREtVSy9xa3J6WU95dzNYOGY1YjkrM09Vakg2TjgrU2JTT2pTTHg0dHA0N25yS3JSRytuWlBqYjN4SWN4dkhuQ3I0cFpHdmV1MGtaV1N2Y05aREg0Z3daelc2UEt3SGZ4QmMrODdmMlYweDY0LzR0ejJ6ZHpZUFJyL2lVLy9VVnBma1B6MmlUeno5YTlyYmpqNUthU3lncVFCNUluTEE5OHNTQS9abjRET1lQdnJsa21Md0JpbnFkZVFST2lCOVAxMHRFKzFPNmpOK3NqNHN3WTRZVHFEZzZ0T0VXaU9nZU5mUURuNmJQaVI4NUJqejhLSEM4amlkankxM2orVmJIclAzeTcvNkt2UVFGbGJiQnFZMHlQaE53TW1JQnJqMGNpbHdyOGx3cHNUb05vWWlZUnVTWm9QQ2pmbGpodlh0ZW14QjEyUEhEeG9ITml2OXRlZnhLNy9rdHYvdWw3a3dJcXJIdW15dEUxZGs1Tm5Yb3FLb3ozcnFydUphcGtMZk5iU0oyNG9kN2ZhNGxkcjNkUDJKR3Y3amJFN1E4eDNhSW1qM2EwTUdMYi9mWERvaGxlZDlQVVgraTBYNVlwMGZ0aFY3MFB0VVRzZ2JJVGlxeTdRbjMvNVMrVUR5eWF6YmtacUI1NG1xODdxYmVacFFMejVLYUttYkgvOU1qckFHUlB3cHRSUFE5bEk3V3RKVVRPaWpmK2JuNGpnbGV1WFBNUFdSdWk2Z3cwRlhjQUhXNUF0cDd0dFBSa1dqM0JmM083YUhKSDhxTk9kNVZOaFlRRzBGVkliRHQzRUpYaTdpS1JXcElrdDk5NVQ2aC9aZEcvMFgwQWZ4alovb0hNY2lnREZPNWpObk9rVXowR3g2K1pvdGlyREZpUFlOQjY3SGR5UDdkdnQ5bisvTHR6L0FIN1BIdEcvK3lJNkhyc2NIRUZhRmw4NjJrVUc0bGJ3a3AwaU1iM3hRRGZFelNNaFRXeFNaUWJ4V20xVnpUcHg4ZUFHVGNjUGRIMDErMmpOYVRCMllEVHExZ216UlliREw4cm9ydmZsdTEvNE1RN2UrUnU0UHR5Rzg3ZXp4OFFCQWE2NmluVHh4WlJQWFNydnVlQlorbmFtMHVIZTZrQjhmWS9KMm02TG1iSGl0bzRrVTdaZnV0VzFVWHdha1A1ZXJUUStsam9UbisrT3NlR0lQYVRZb3lGR2gvY29BeEVCeXg1SnVzTjBBcFBPYklaNEFabWdPbTBjSDJCYTBkWlRYSDJBU3l1ZUZOSVJycnFNTlF0aU1uUk5BMGVIZ2c1RnZLM2xvbERTSUFZeUpXWnpQalBIQ2pxYlFSWjFWYVNib3JNcFpiVHFqTmJkVmxkRUptUDNZakRPd2l6SHJCWUpRbE5QSUNwbW5iTTJkanlKREphaFhVYWtkV2tHS2gzNHJMalhtYkdJUUZFUHdUZkJTd2dwdTB2bHRJeVUweXJvd1lSKzNjaFJsZmJFb2E4ZmNsbGZtVkZBdG1PK0JNeTBsTVZtb2Z1S1RwcjNjdDhGZnpyWiszbW54dXpIeUI0ekIzUVFMa0xsYXRLMS8xdytlZUd6NVVXc01Tc21iUm9La3ozRzZDNXpYWW9OWGx0MTFOV1hmMjRielpMZ3ExbGtHS0NEZVlNcy9xNnJxRVpkR0FvSDdEK0NBRzVZUU5TME5yUDcwbFlGeEt6MG5UbFFLMHBuVVVXVytqeGRnYnhwTVdsYWlPVm5kODlGcEN2UXhTU2FLZEdwalkwYzNFQnpUZUlMSGl5NmlzZU5BWFVQd1pwVTBhMHE0Z2t0R3B0MUpDY0RsdUxuVFFQVWlFSkplQmFZUnFUb3hkMDBOeExZQ25VcFZiV3RuMWVYaFBjOUZjU2xpSXB0d2plcnVwV0NxdVBITktUamwzeDJJTXRvOThSZFJCWXBiQ1Z3RjQybTZmMExNbnlmM2ZtcnZ6M1orNTdyb0hreFBPS1d5N2V5eDh3QkFYYUJ2a3V4bHhySHZPc3QrdG5uL0JqUEtBL1E0VFNwaGZHOXp1aCtwMWtFTThGSHNkcTQ3Ykp0Tk1lTCtINURCbjF3MDNyNElUMithZU1FSklEUUVGelpaZU1Mb0hZdUFzL3A5ZkhBRVdBOU94Vzlya3M0WWJhYTJOVWw1am5yVmFwYzNNakc2M2o4YmhiSFZTekVLNk9iVzFLc1VoWnpzcUJkakJnOWcydmY0elM4Z09RRUhVaDJNVlBYckVKMkpPUG1DcUpDcDFGRnp4SjBUYkM2RmpGeXF1TFo0dDQ3dkhzRlNXdmZuUmNjOXlydVZIZjQzYjI0SEpra1BXM2dlbHhMZDZ2NTZzM3JLcWkzRk5rcWpvblFtdWZab0czZkwzdmUrN2JwN245NVhlejNWdjdmeDg0ZVV3ZUVqVkhkejIzanFXOTdZN3IrbkoxeVVyZENWa3BLaXpEYWpZenZkOWNoNGlMT3hMRTEyUGF6VzJUNGpBRyt4NURCL05KcVM2YSs1MzdCeit0cFdPOTBoMGhrNmpESWU2bUxvQ3dSSzNWdk93U2U0aGV5eUZ6WE9WT1JNRkhvaE5oamZTNERtVHVmUlJndlFLN1F6UnhEVkNteHVvSWhaREVOMkp4NEZpUUhvaG9YSjRPWC9qVTBuTEVERGJRNzB0RkgwbmlOckdpUmFFVVZqZXZMR3FkbWxxcGRIQ2dxTjRHYTcyRlNOelZNVkVTa2M0cVp5YW1Mb3FjTWtRWWY3NTZ4Zm5Pc2pyYmlzcFFjTTN6b2RLVnRoeC9XL2UrL2ZITG52M2k4bkcvK0xUL1cxbGZHbHgvTE0zL2xEZTJuenpuTmorcldyTFRKbFFXUjBhM0crdjJnUy9VTk9aVDdZUG1DSlZtOFlCRU9GQ2Q3SUpqN3EzU2xINUhUaDV6NlFadjBmSTFhc1pUZU82ajNrQ3B6aXkvU1hUZmdWaEs5MkF6VVVYVVVPRFVjaDRNYnVmWnErdWN1RXBxQjlRU1ZRdXdlVjNrUUt5SWFUaHcvRnlwVGJ1YnVXVVdLUkZxUkJjK1FDdmhNUkV5eExuYnluVmgzSUl0SUVhRUVnTm1MdW5TSVo0bWJ3cXNUV24xT0U3Rk1oVEJIWGE2akxMNHRvYy9lQmd2SnljN2t0akdqV3ljdUtLMGlpN0VLNm0xbVZ0bzAvSEJhK2JQTDEyKy96QU5TL0lqbXZOK0xQYUpKeUhlemk2K203RHFmNWsvMjhIOCs4bGZkSzI3YUxmdmFJMG5GTUovQWxxY0pXNStpWGtiMUZ3U2FwNGlzWGJmTzJrZEdzRTJFcmNTWFhWY1RnNlJjNG85SUQ1K3ZrSGVKK1dqZERabnYrR3BkUUVvZUlhM2ZicXU3dzdFWlI5Mm02K3NZTmxaSkJXZk85RjgzK3BKRVc3RHV1bFRWS0VFQ0dVV1NmaE9RMEErMkN1K1BnMG9VcklwOVZ5SkpLVDFDbTlpcGxvcXNycnZCSWYrbGRTMWNZcWtwV0NhZ3JuYlc1Tk13OTFpZGF5SzEwSEdIbkw1SWV0RVJ5SEs4enV6T0NkTmJ4NGdrR1NpeTJNWVVwRFhwR0RiRGp6YUgvdHZsNjdlLzBUY0FFbytMODlXdi92R3pYZWZUWFBsWjhyODZnM1BmK0UvMUU2ZnY1Smh5d0xJNmpRelZ4L2U0cjk3dGtoWUo5dXhHc0wzdWFWbGwrZlZiYUU1czhIMVdhZDE4b3dqeGlHYWgxU0srNldNS0pIOEI2TGxTM0NuaTVpRjVVdnVKdmZoUGZRNnZKeHYxOUpOZTU4WG42UFFlSUp3ZHo0amsyQUh4N0o1TStsTTBVQTBGMTQ2ZXZnTXY0TEhLaGhkRVNtaGZXeC9xTXlMVEJNVUpzcFU2cE1uQkdLWkZoRnlUc3l5dVdiQXN0U2lSNksxVWdjemdVVlpZNjhSMkpFL1AzaTdwaUlINHVEanFqTCt5Um5kNzUwNmlFZmVGTmpaUVdyUnoxY0ZmeXFGcmZtWnl4OFVLTTl2b3hENXU5cmc2SUd6MENIL3RPTTY2NURMOW0zUE84SlBLUVhLQ3hBSSsyZTh5dXJYT1FBYkU4dm9hemdqWjhwSXRMUHpVQW93OFdpcXBkdUpLNFVFaVBocnpDblY2SllINWJNbUt1L1lMR0FaV1RMUjM1QWVGWGQrb2ltdUZDNFNEWjZoTFk3Vm9pYjhkd1F1dXRSMkM0VlpjeUlKa0VUcEhpZ1RxR3J5Z3ZRT2pSZnZuY3MrZ1daUWN6eGNOUm9XdUNtRVdSWW9Rb1ZqNm5OVURtUzNRQloySm0rQ3JKU0FaejlpS1AzczdzcFlsVGJLWUNhdGZQT1JsZjBZMWVhdk9zSEZEeEpxUWVGKzgyZzVlYy9IazdwOVZ5TDhCajFtdjd6dlo0KzZBc0hFU3ZuNExaNzM5emZKWHozbW03TFJEWkRKSkI1REh4dXJOd1JlZUZwaVBJdXcrR1B6b2dDMlhMS1BMQ252TFJ0SlFha0hzRHFrSGtNVHZlcVpLNmxab2xzZFl5NHIwMmlzQkxNbjFCaSsrQWNGM29LUG1nRkhJZUg3UTZzUzhhREVYMTBLb0dHVEZDK1k1Vm5mcFJLSlFxTmVqUWlrRWIwek4veWd1M29sTEJpMGkzb0VYOFpCYkVDdkY0eHFMaUJldFVyNGlFanpvM2t1RmVHN2MxN0o3TVpWVGx0Q3p0cHR2VGNLaFFtcUVmUGRFMW05WThUSnhraWJhWkxRdEFOYVlUcjFOU3gvaDBEV1h2UHllbi9HcnNTdmdZU09iSDZrOUlRNElHNFhKeStHa2QvOXkraS9ubmlzdjRGRHB5cFFtRFFPeHNucXpNVnNCV1l5SVN3TitBQVJoOFpWYkdKNjNDR3Z1dmxLa0h4bUwxUjNlS0Rwa0R2S1lzMGxnMFRxQkJ4VVMvWDZJYndxL3pxYUttTmpETm91UW51dlBtOVVUc29xaFJuaDJDMGRCaTJPRitRbElSeFFxRWhvLzN2V2ExNEpueERzSnV1UE9nN1NvOURJVmNmcDVGcGVzNHRtZEltSW1OSjI2dTRpWnU2eTQyMHpGVHhvZ1oyOUZkeXpBWGdOeGJMbWgvTU1xM1kySHhHaFFFVy9ieW5KWDNCdVZyclROOEM5dDlTOHVXcjNyVXNmTEZZOEFWdjlvN0FselFLaE8rRkVLenVMZnZray85cE0vcWEvQVMxZFdVQjFJa29FenZzMVozdzA2SkhoWUZId0t0Zy9hVTFzVy85azJtcE1WOW1aOEFqUVNvamdleExaMTRTamF6aVlSZ3FFNlgrOW9QZnE2QWw3eXBzZjZ2S3hmVUtvaE1VS3lTSVJmSWxjVUNmU21nWFdnZFJ6bUJmRU9sdzZSNHRBSjFyUDdWcWxmY2grQzQ3VmtLaUpWRjd4blk1RksvRUFuZlhvUW1ta1RoZFVpVm5CT1drSk8yNEljT1JBZEY3ZDFkNVlHNHVOTS9zS0s1OXVuQVIzREpZbGJHZ1lCWXlNaTQ2VHBPbHQveno5WjIvMXJDdjVFaGQzTjlvUTZJTUN1WGVnNzM0VzVvUisvU0g3bmhSZklXN2R2ZGJjSEtJZ2tIVGpkUGhqZDRWTEcwQXpyNFpiQUR1SytEc1B6Rm1YTFA5NkNMaWdjTk1oWXNMcUp6QVZpUEpySjRvRWpVQWNyeEFsbWdwajNGRS94cHpxaGUrMzc1ZHFHOGRweksvVlU3UjhyTmVBWGlXV2pDTTFPUnJSSXRKRUtnVUR0cXFCQnhtTUxyMzd3R2ZkYzJUdG1FYkt0UUdDM3BFSVlFYktpcHRBaHJEbWV4ZVg0b1hENkZtUzV4ZGM2R0JWMElYa1pOSFEzVDN6MnhWWHhUcTFaVUJsSXFhdEtnZzVrcG9tRmIzYWRmY0dtYjdsc2JjOGYxZkZhMytwL1F1MEpkMENvWTd2YVl2N3pWK2psNTUzUCs1OTJxbXhobjNXV3BkRUZoK3lNYmtPbTkrTGFJdDZUUERuNFhwQ0JzUGlTclN3OGZ3RnAxZXlBaWViNmMxNTZKNlBPL0IwTGxqUEg1dUZZK2tMR0xFSnMxS2xDcmhPRXZzcnVRN1NKUkZWTjVIZWl0YkhzNGtWY3M3aDFMcGk0NU5EazhneFNsVkFsaTVVYWdzUEJQRjYzQStucXZEWUhmWTJSc096Z2prNlNNSXBEVVk0ZndvbEx5RklMSThNbXhSa0tPaER5M1oxMFgxNm51ODlNQmdrWEdJalROQUVHYXREQ1VOc2JiSHpiTmV1algvak42YUZyNjNqdGNXa3lmeS8yZlhIQS9yWDlLbFF1cHZ5NzAvanhDMStUL3V6YzUvb3pHSlBMbW10S3JneWgyd2ZydCtCNUhkRWxBbENYRUtaZyszRmRWcFplc01UQ1R5M0NVaExibjUxSlFTdXlXYXpLYjgySG9hQkJiUktGaTdOUmhCaHhlcGEraUFtTWdkYlE3Q1lpeGVyY09HcUFlZHNtbTJ0UnA0czlKYTJPU1VkVXdxNXVXU3FBSUlxSlBwY2t1MmhYdzIwTi9lN0o2Y0JuanBRa0hEbEVqMi9kR2hXZHBIZ3ZBM1VyWVBkMmRGOWJ3M1liN28zN01DRm1rcEo0bzlIZVRFbDlKQ1ZkWDlZL3RXdDE3NXUrQkhjOUhyUGRoMnZmVHdjRU50bzBaOEJSdi90NitRL1Bld0dYYmRzS2pEeTcwY2hDU0l4T2Q4UG90cGpWNnlEUU1aNEVKdTZ5QjlkdEtvUHpseGc4ZjlGMUc2S3JSWHdsSEU1U2NNcXBCMGltOWdQRmJTTk1CM0FBTmxvdHhJa1lZVnE4NW9YU0Z5bFJsSVRPWFJHUkxzS3hGOVN6dStZNlByTk5PV0FoZGwyeU9GYW5ObEVOSXpPWk0ySllrWkJoVjFHV204anBYRnhOUkZvSnNNS3FVMjZiWXJkTWZiWS9jTnJTcGdEdUN6UkJqV2RKUEd2VER1L3dqbytQMTNhOWRmM0FPd0VlelNMUlkybmZkd2VFS0U1ZTl6R0tHN3p2aGJ6aEZSZks3NXp4ZEQrYUVWMFphMHB0bklibG9ETytIV2I3aUhiTW9ENkJpdHZFOFlNZ1EySGhyS0VzUEcrUjV1UUJEQjFXY1VaZTl5R0xHT0x6bm1HcFJVanhldkpzbnYwQ3VKTkZ2RlFPaDA2aUxWTkhjWktKR1hCVXdHNFpDSWNVcVcyWVd2aDRzUnFPY3lYWEtvcFZzSU5handRWE54UlNxdHc1RWlpemhQakV2ZHhkcE56ZVlmY1ZiQXJlcW5zRGdrZ1M5MmFvcmxGeGw0R0k1SWIyYzlQcExaL3Vwci80bTRjT1hldWdWeEFNdUUvb2wveHQ3TEJ3UUtoNVlRM0p2M1FVWjF6eUJuN3IyYy9rTlF0TEFxdDBOcVhSaGVEd3k0ZUU4ZTFPOTBBRmFUVzRKRVJTNUdiK1FCUUR6UWtON1k4dGVIdG02ODFUbW1EUUdwdjR5SkN1bjZ3WWMyUk1YNFRrR3A0RHpMRFIrNnVvbDdsVGxkb290amxBd2F3akppNlpLQnBxNDlxOU5wbUx1QnVTUWdGTHJPZUZ0aURPY1FWcFVqRHdDMjdqSXJhblVPNG81SHM2R0FNcDRZTTQ2c29zU0Z6YlJTUXRpSHRSYnp1ZGtYVGgvcFQ1OUhUNm9WL1lzKzlYMTJCZmJlVi8zMCs5elhiWU9HQnZmYjhRNEVNdjRkTHp6dVBkcDU4aHA1Q0JOVExtaVFVUkV1U0RNTG5EbWU2SnFscUdvQW1YRkFXRWpkMVpKUmpSam0ya09XUGdnNmNQcFRtNWdTVVJaamlyV1JoN3hmYjV4blFGNkhQQndOa0JkYlc1ZDBLdmppbEYzSXVMRkprREY3eERwR096ODFyZjRpa0ZTYmwyMUd2a1ZXMWlTYVV6eWdxVSsvRnliOGIzT2I1dThkZ2dtSTB3cEhUQjh0YTBRcnNsVnF5OXVEV1NVaVp4ZzAyLy9xbngranQySFZ6OXVBQi9jWmlFM0lmYVllZUFFTGpDSzZKS3R0ZkNzVy8vZWQ1MjJrNzU1ZU4vaEFWR1htd2lrRkFkSWlpZVI4NTBOMHp2UVd3bXJnMmlBL3FwU1lUVGlZdXZ1V21IcEswcSt0UUI2ZlRXbTZlS3BCME5ERFRDOE5SaEFvemRZNWJyWGtka1ViblB4M1lCckFrSGpMeE5xNU5hemZzMDE4YTFFWEovbGVSTEZhY2tyRU4wNUhRckJUbW9ZbnNMZm85UlJrWlJjWkZndkNScEJjNGluZ01zTVdpRWRsaXBia3h5QUlkU2U5Zk0xcTR2MC85MHlaNEQ3d0pXcm9KMEVYTkU1V0ZuaDZVRDlyYXBjYzN2UFoyenozOFYvL3JrVStYaUk0NXlZVTBLMHdBSTY5Q2RSc1JITHQwK21OMEw1VkNBRGF6QnBVZTl1TGhhb0pwdEJNeUM5eWh0VWRJeExlbWtCamxPMEtNYmRFbVFWaXByZ3pnWnBVVExoRm5ORzNQMDZqeTdTRkdmVjlOT0JUVFU4ZDdVM01iZ0k4Y091dGcrdDNJb2lpUTdaRjdHU0ZPQ0xzSlYzRnZCVS9pdVQwRnpnRytiUnFSZEVob1JOMU9UN0paVWhTMDB0ODNNdnpIcFB2S0ZsZlYzWDNsZ2R0UGhmT3B0dHNQYUFTRnl3OC9zSXIzNHltZ1hmUFRWL1BTSlo4cXZuM0VhRnh4eHBNRElDaE9LWldtMFJXa2NEQzhyU0hjL3pQYmllUzA0ZlVTREpMeFh6VlJxTVRFRG40Q000OThBc3Fqb29yb3VDTElzNkdJU2FVQ0hBc002Qml4VnRrWEJUYUxKUEhFWXU5akluWU11dG1yNHF1RmpvVXhxRzBhMXdyR1FYaWN2SmlOQUtEM0VOSzY0YXd1REJaWEJRSkJHc0tLdVV5bHFDRzFLZDNXRi8yM2xFMTlhbi8zYmQ5MjdkajNNSzl6SEZVYjFXTmxoNzRDOStTNlVzeEM1T083b0QxN0l5ODk4anI3dHhPUDlwU2NlVzVGcnE1UXlFeEYzMGFHRXhHc1I3MWFSdkErbWV4MWZCUjlUazMxSXdWc2RuRDJCY0k2aW9lQXlWYVM0UzNFdFhiUkIxT28rclFXa0NpT1U2QnlrVkNpOFZXWkhRYXN1aXBzbXZDZDQ4eUNna1F5ZUZadmhPbE9sT0ZxZHZGa0VYUkJQUTRrOWxMRVVOUnpYUkVweWR6WnVHcGRQLysyQjZYdXZmR0Q5ZndBNHBDdkFENWNLOTN1eEo0MEQ5bmJWUmFTTHJxS0tVOEh2bnMzenpqNVhMdC81ZEgvbHlTZHdNb3NDNjE1c0VxaG5SelMxQWkzZzdqWjFzWU5JM285M0I4RFdvcW5kN3p5RnVLSkFkRDhxS2pTY3JHZFdrUklnSEM5QklCUk9GOXprR0tFeVlhQzFRdlpDbFNBV0p6dVdlM2F1VUpCU0Zab2tORnNnTFZZS0pCUDNETjVoall1RHRtVGh0bldmM2xYa21tdldwbi93M3J2V1BnZHhjMTU4SmZKd3FIRVBGM3ZTT1dCdjFSSG45TzF2aFdOZWRTbXZPL2tNdWZ6NG8rVzVSeHhaeDJjVE1qTXZacEl3UW5rOG9TZ2V2SUJJV2NQTEFhUWNnandCRzROMW0zcUJsYTlNY0ZFUHNFRHNua2lQTSt3ZE5MaHNpemc1U1BxOUJDR3JOSkJhY1Jva3RlcHBFVW5EQUZ0Z1NocHJESzJub2MrcWpjVE1CMGw3cDhZM0orVXJYMzhnZi9JVGgveVByejR3dXduQ2c2OEdPZHp6dk85a1Qxb0g3TzJob1Jsby92aEZ6Zk5QM0ZrdU9mbHBmdUZ4Uit0cDI0K3FSY09Fd3RTZEtXNVZNbWVEdUx5Q0R6TFlEUGNPOFRFd3cyMG1sQmt3Umdpa2MreEpaY0dEV0JPbm91SUJDYUk0VCtKQ0VuVGcwR2cwZHpUb01xd1RmT3BJRnFlajZMUVJJQ0VxSkdGOVZ2am15TmZ2bkhEdGpRZTZEKzI2YS9ZM1JCY1F2NGgwOWRYd1pIYTgzcDcwRHRpYmczQVJxaCtsOUV0MHA4QVIvLzVGelRsTE8vMlNuU2ZaVHl3dHliTk8zRzdCNGwrQWRhRERtRkh3SUpxcWZIdWh4OVlqWlZMRkdsYUduNG9iOGVCMDNyUUdZTFdRcU9NOG4wV1JZd1krRWFNaktGZ00xNkphMitkQ1ZwZ2s3bGpMM0QyeVcvL2Z5UC9PSlYzL2g3ZVZhNzg4bmQ0S05TcGZSTHJpNmlkWGp2ZmQ3QWZHQVRlWlhIVVJlc3laeUl2Zk9lZGFCQmorL2pudHpyTit0RHR2eDNIOHhJN2pPTGNvWi8vSU1iUnBrYWdYNjlJNXVZYm5FdTNqMms2Si9aQ3V5a05YQkZhRWFBSEhlenhneFEwRzczVklDRlpldXJwV09ZWFpERzQ5SkRaZXQ3dFhWdVR6dDZ6TEhmZnRuMTc5Yi9aeEszQ3d2K2hOcDkyVG9xcDl1UGFENklCemM1Q3J2N1V6QWd4L2NRZkh2dmFWbkwyZWVmSHBKOHFPNVVYT0hodW5OaTNiamxpazJUSjBIZlRLbkFsWWQ1alZmenV4cDFKcCt6QnFnQ1FTdXpITVZvejk2OWpLaEM2YlR0cFd2M3BnamEvZXNvL1ZQQzAzLzhPS2ZlNC8zc28rNElITkYzYmQrVFI3ajhXLzlnTjIybjByKzRGMndNM21JRmZzUXE2NENibHhCL3JjRHdUWnhrT3NlUjVzZTlsUkxGejRJbzVlZ1owcmEreFVaZHYyWlpxbUJDSEQxa1VHU3czcVUxeFVrcXV6M2tuWnM4cnM0QVNiVEwyVUtiTWRRNzF6Nk9udnI3MjkyLysxdTVoOUh2YnpMUnpLTHlKOVpnL3ltYzlpVi9UTEJUOGs5a1BqZ0ErMTNpSFB1Z2s1ZFFkNjdnbTR2cFA4TFp6eU1UTVJ1T0hIYVc4RVR0K0svekE2M0VQdC93TVFrM3oyNlFMMVJRQUFBQUJKUlU1RXJrSmdnZz09IiBhbHQ9Ikluc3RhZ3JhbSIgY2xhc3M9Im9wdC1pY29uLWltZyI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJvcHQtdGV4dCI+PGRpdiBjbGFzcz0ib3B0LXRpdGxlIj5JbnN0YWdyYW08L2Rpdj48ZGl2IGNsYXNzPSJvcHQtaGFuZGxlIj5AcmpfZ3Jvb21pbmc8L2Rpdj48L2Rpdj4KICAgIDxzcGFuIGNsYXNzPSJvcHQtYXJyb3ciPuKGkjwvc3Bhbj4KICA8L2E+CiAgPGEgaHJlZj0iaHR0cHM6Ly93YS5tZS8zNzI1ODczNTQ1NiIgdGFyZ2V0PSJfYmxhbmsiIGNsYXNzPSJvcHQiPgogICAgPGRpdiBjbGFzcz0ib3B0LWljb24iPjxpbWcgc3JjPSJkYXRhOmltYWdlL3BuZztiYXNlNjQsaVZCT1J3MEtHZ29BQUFBTlNVaEVVZ0FBQUtBQUFBQ2dDQVlBQUFDTHoyY3RBQUNPaUVsRVFWUjRuT3k5ZDRBbFIzVTFmbTVWOTN0djBzNXNEdHFWVmxybGdCTElnQUFKSVJBNWFrV094c1kyOEFIR0JodURoZkRuQk5oZ2tqRVliSklCTFdCeU1GRkVnUWdTUWtKWjJ0WG1PUEc5MTkxVjkveitxS3J1SGtuWVlJTHg5Nk5odERNdlZsZWR1dUhjVU1CdnJ0OWN2N2wrYy8zbStzMzFtK3MzMTIrdTMxeS91WDV6L2ViNnpmV2I2emZYYjY1ZndTWC8wd1A0TmI0RUJIQVJUUDNJM2poZnE4QzcvTHQ5blFqaUVoQzRpK2QrYy8zbWlwZGdNeXpPUVlaTFlYSHBaZ3RlYk1CZjRNWWtCS1NBRnh0Y3VqbDgxMmJZWCtoMy9DKysvdjgxQ1JmRDRDc3dlQzZJelJjVDVoTDlTZkxwSENDNzVRa2JWaTBVZXZqQ2dodHhmZTNDbUJ3Q29rZGpPM2tHTVE3ZUF4NzBUdFQyUGIzUk1wKzB4ZWg0Wi8reVcrZDIzUFNkZzdNL2NUeWtBQUpzZ2NHYkliZ01IdjgvazVqL2J3T1FFSndMaTNPaCtBdFI2T0sxWGJzV283TW5MajlMbDNDbG5qcTFqTG01SndaeXZIUnR6MDlJRDZOMnlvaFp6WTRJSUtBVlVBUWloQUZBQ1Y4Q0NPQUFlZ1U4WVZWQWRmUHF1QThEblRGREpZMjVPUk44cGJoK2RyOU11MkZudC92dTRQS0RPeFlOU0FCb2E4eVhRSDlGTS9VL2R2Mi9COENMWVFBWS9BWGNIWmR2OHVGcnp5aEdlQ0pPbWJ5WG96K0hLL014R2NzMllzS0NIUUV5QWNVQ0NtUXdvQ1BncUNKQ0FhZ2dJQUYxMW9NMENrSWtRRkc4UUlRQ01VcERFVXRMVUFUb0NKU0VxUWhVQ2xONmNMcmFxM3VyZmFia0hyUGdQcVczREc4dVA3cnJDd0FXNmdFTGdBdGhBUUJiNEg5Rk0vZ3J2ZjVmQWFEZ1l0ZzdnbTdrT2VzUEszWVc1M2VPR3IwM2wzZnVweHM2UjJCSlBpSWpGclFHcWdKVFFTbkdzMUxRT1ZGMUFsY1JWV21nRGxBVnFCQmlUUHdtaEw4QmdJQWhBLzVFWUFRd0lDQlJNRnJDWnJUZGpHSnlNVmxPWkNURElEdk1DWUZDS29MVER0aGJiY3RtL1BYRjdzSEhkVkIrRmUvZWV6V1NTallBWG5GT0JsejIvNVJrL044TndJdGhjTkptd1VWYmF1a3dkdDVoZHl1T3pzN1hEZmxGUEw1N0RLMVpabWhnSmpJb0ZRYW1kS1VYbEpWQk1SUzRnWUY2UUF3eXlkbVRETXQ3VTNMNCtDb3VIWmxBVDBiUTdZeEl0enVDa1U0WEhaTVRBRHdkUEFtbEY2OUVwWTREWDJEQkQ0Uys1TEFxdVc5K1d2YjJwekhyK3loZGdRSmU0RHhCQTNSeUl1OUJPaDFhQzFVTEE0RVZDTFR3d0x3ZllyKy90Yk8zL0lTL2R1SFQ3cU83dnc1RUtjaUxEYzY5eE9BeXVQK1ppZi9GWGY4YkFTallEQk5wRGdXQVNXQ3ErdTNETDZ5TzZqM1ZyK3llaFZXZEhuSUNUbUVrcTVTQUZuMkxxaStvK29UTnNRUWo1b2pKdFRoNWFpUFhMVGtNYXlkWHlwb2xTem5hSFVlV2Q1aDNjZ0VBRHk4ZW5nSVJyMHFuSGlwT0ZCNmtLRWtqQmpBUVZVQ01DR2dBZ1pDZUdGYUZLWDNGb2lxNGQrNlFiRDI0QzNQejAzTHo3RzdlT3IwYjgxVmZJSjR3UFVHbjQwMjNBMk16VCs4N3pBRUxDOTA5QlBjT3J6WTdxMzh6MzEvNFNQbjEvVGNBQ0ZMeHZzaitOenN2LzdzQXVCa1dINEpQVTUwOWZNMDk3TjJubnFJcjdVVm1RM2VONjFxZzhMQnFTdlVVVi9VdEJ0TUdIbHplbWNMcGE0L0Z5Y3VPeFNuclR1Q0c4WldTall5SVdFR0ZBZWRRWU5iM3BYUWxuUysxRWxwUGhYcFBncUNCcUJJQXFWU29xSUFDVWhVQ0VTT0VBaVFCZ1NnQUM0Tk1STVFZV2pFeVlqb2NNVjNZVGk2b3lLSVl5djc1Zzd4NTczYmNkSENyWExQL2RoNFl6QVk3TXgrQjdYVFZlcU9hdzdJSFl5cENkZ3htc2IzOGpMOW00WVArMzNkOUNrQUpBTGo0bkF5WFhQYS9Eb2ovT3dCNE1ReGVlVEVnbHlnQWE1KzA2djc1a3RIZjV5a1REM2RINXgxeGdOVnN5Rkt6c2xvd0tPWUZhbmpVeUNxNS8vclQ1Y3oxWitHazFadklMc1Nqd3FGeW5uTVlvdUJRdkN2aEJYQ2lKRlFJa3FwVW9WQWdYajA5RlRRcTNpdElqVGlMWmhnSldFc0RGVEo0eEFJQm83OGlBbEFsVUM0RWFRQVFzQ0xTc1JsNnRzdHgyeE1qRnNPaTVPMkhkdUxhUFRmSzFUdTJjdnVoL2FLZ290TkQxdW1SSGZIb3FSV0lOUWNjNU5yQkQzRlQrZGJpZzF2ZkEyQSt6TlgvTGlEK2VnTndNeXcyYjBheThVYWZlTmhEaC9kYzhuSmRhZStGMFF3bU54UVY1NTNQVU00Q3d4bFpON29TNTYrL044NWRmemFQV3JVUjdFQm1NTS9wWWhaejFRSWNmUEFSU0ZFb0hEMVVQRHdJcFllbmh5ZFIwWUhVb051b0lBaEYvTmVUUWNjcUFGQWtPaUVSZ1FJZ09NUVFDQkN3SElrYkVVajhwVVpJa0t3d1lybWtPNGJSckF0NHhjN3AvWEw5cmx2eHZkdHY1TjY1ZzRDeHdPZ1lqVEZLQXlBem1mRUdjdXZnVnZPandUOU12UDJXZHg0QTVtcnZlUXNVditaQS9IVUZZUEJxTHdsR2R2ZGhxODgxcDAyK21FZVBQWHk0RGpCOXFKSGN1M0loUTNFSW81cnJmZGJjelZ4dzlIazQ3YkNUWUxzZFRPczA5MVlIcEhCVkFJNEVSMEdwS0h3cGFsUVV3YWJ6OUtLaThNb0FSL1ZRa0U0OUdDQW5CSUx1SjZFZ2hBeHdTNkJpRTlzd0lzSWc5d1FRTVdMaWF3R1JnRUNCVUFneFlnTkFrL0FFU1lKR3JJeDFPakxTNmRKNVl0dmUzYmo2dGh0dzQ1NGQ2THNDNlBURTVCMlYzRGgyMEVFQnlHMkRXK3kxQzI5ZStyYXQvN1FIV0FBaGVDWGsxOWxyL3ZVRDRHYll4SG4xTGxoMUw1NCs5V0ljTS9vNFBhd0xsbDdoUkYyeFlERi9DSWVOTE1mRGo3NGZIM1QwL2JGeWFwWE1tNzdzS3ZheDhpVWdaQ0dWY1hTb3ZHUEZDZ1c5ZURvNGVucDRLTHc0OWZDcTlFTHhVSHBWOGZCVXBTZ0owcE9nQkZGQ1VFRWFGZE5vWU1KQTR0TUNnTWFJa00zREVJTUFPVkJFV3NKUVlDRVF0UUdRRXNCckRBQWFnS1JUaGJHQ2lYd0V2YXlMd1dDQTYyNi9UYTdjZWpQMnpjNFFuUzVNdDZQTW9OS1QzSGdEL0hqK092M083Ti9vbHAzdkFhQzRHQmt1K2ZWMFZINTlBSGd4REY0SlFLQ3JnYkdEZjdEdXI3TzdyZmo5Y21Nbk02VTYwWTVXMVREai9FNnozcXpCczA5N0hNODU5dDdRbnVXKzRvQk1sN01veFlzSTRjU2o4bzVERE9IVWlhTkR3UXFWT25wNmNWUTZlaEFxWGhVZUhvNEtEYlFLVlFpdktncUM4VEdJaUZMVEVpWVZTaWdCRTBOcVFTTFNpTlR6R3NTZFFHalNBekJHQUJVWVF3Z01oQW1xQWd1aEVRbjJJME0raE1UdlZsREhwQ3NUSXlQaVBISDdudDI4K3RaYnVIWC9BVUhYTXV0MlZLMlNZelkzQnh5eTd4VGZjRnZuWHVrL3N2TUxjWTZ6cEZWK1hhNWZEd0Mydk52T013N2Y3RTRmZlJXUEhUMGVqaFN2VGlFV00zdGt1WXpnbVhkN05COSsvSVBodXdaYkJ6c3g3K2ZoUVhHc1VMS1NTaDBxZUZTK29oZUZvNWRLS3poNFZGb2lnVStwb2dROEZaNGE3RDlKSUF6V25pY0pVaGlUV2xSaitJMGFYWXdvOUFoSVVKMFFFNmdZUktXYXNDaHhxc1dnQmx4OG5LUkloRE16WTVGRVpJak1NWUk0ZklhRm9TY2hJakxXNlNFM1JuY2VPQ2hYMzNnVDk4M05Dcm85aUJobFJtVGp1ZVdlRXZMRC9qLzNYcjM3VCtjeHZ6OTlISDVOcE9IL1BBRC9DVG1lZzJvQ1dPNWVmdFRycXBNbm51b25EZXlDY1ZZeVV4VDdUS2ZmOTA4OCtzSHl0Tk12RXJ1a2k5dUdPN25nKzFUUTlIV0FRa3RXV29xRG9sS0hrZzZWT25nb25JYmZuVGlwdktkUzRZTExJUTRLOVlTSGdnaEFaQkJyOEZSNklqZ0l3VGFEc2hYL0phR0Jja0ZDU2pMenJCZ0loRXJDQkFoRy96Z2dVaEJqeWlBTURJUVNNQWpTR2lPZ1JDb3hPTTVRU2NZa01wcndQVVlFRFB4ako4dkVaQlk3ZHUzRHRWdTNjV0ZRQ0xvZHlhMzEyZ0hRc1ZhdUg5d3UzNSsrdVBxWDdmOENBTGdVRmhmOXo0ZjMvaWNCYUtJQTRkZ0RWNTlYWFRENURqMTV5VVpYZXRjcExhcXF0SnpmeS91c09sbGU5RnZQNXZyVjY3RjF1RVAyNlF5b2hKZEtGM1FvQzM0Z2xhOVFza0lsS3FXdjROUUhMeFlCYkY0OVNuVjA5T0pWb1ZRNnFBQUtWUWF3R1JVU1VlVXF3djhFVUYrekswcEFJZ2daOFNhQWtJendETFpmSmpZSlNCZ0lLSkY3aWJJdVNyZ0FTQWhBQTRsT3NUVVJvdkhUaVNRMVFSUGdHcDhJK0RTU0FTUUVSQ2Z2d0h2RjFwMjdjZFAyWGVJbG84MHkwTkJ6VERMYjk4QVZjKy92dm1QM2l4YjJMdXo1ZFZESi96TUFiS25jN0ErUCtETzUyL2dsYmszWG1sbDExbWEyN08vR0tqK0dsNXorZE56M2J2Zmo3dXFBN0JzY2hGcEtYNFljK2dGS2RkTFhJWWQraURKS3VVb2NLblZTcVlOVGhSY0ZOZGgzSlIyY09ucFNDSzF0dmtTMUpNZ3hpTUFnek1RS0VOUWdUTWgyWVZEWlZQcndmcUVFZXMvQzBzS0lSV2FzTUlJMUNEK0ZKd1ZVUUlUR0dJRUhKYUdKRmdHZGtFeU1Hb1JuaElGVGhJblNrMElEQXdiaGlTQkxnd0VxakJMWGlQUjZYZlQ3UTl4OHl5N3VtNTZGOUhJUjBuRUNUbUI3NWtmejIzak43TFA5dis3NlBMalpRcllRK0oveGxILzFBRHpubkF5WFhlYkdNYjdTL2RuNmZ5enZQZkk0VlU5VEdLbzRnN21kZVBqNnMvbmllejBYZHJRclB4N2V6TXA3S0R5R0xHU29CWWErWUtHbEZGcXg5QTZsVnFqZ1VGSGhHQUJZZWdjSzRkVFJxVWNGRlZWUGhhS0NneExpVkVFcUlVWUFnUU5KOWVqckFBdStMNVhyMHloQjlhZ3dGQUFZbHpHT1NJWU9lOUpCem83a1VwZ0tNMzZlczV4blJTOGQ1bUxVVUhNRDArbWlJeU9jTUNNd2tnZnBSNGhuUllBUU1aSUZDSUdrV2pFaVlrSWF0b2pZVmtJMlJDQWF5V3hBREF4TU5EMEJ3SWdOQkpHcWRQSWNFNzBlYnQ5N2dMZmR0Z3NWVmFUYkFhRVZSa3llSFhEZWZIM2hWZVUvM3ZJcUNJQS9oL21mb0d0K2xRQVVYSHlPeFNXWHVhbVRWOTF0K0tqbEg5UnpsaHp2K3NQS2FHYmRjRnJHQjMzOHlUMmZoNGVjK0FEY1ZONHVoOXdNdkhqMDNVQUhmb2lLcFJtaTRyQXFnbGZMU2dwMVduaEhUMmNjaWRKWDhGUlU4S0lCa1BCUVZhbzRIeDBOMWVpS0dqZ29aNnBaekxnRHNKV25jME56Ykhlakh0WGRnQkdNbXJzdE9aMW5qWjZBTVRzbVdUYUdKZGtTZEV4RzBLQmpNdVRJNGFHWTF6NzZmaDVEdjhDeTZzdU5nMjI4WXVFcTdDMjJ5N1FPY09YQzlWaEFuelFXTkptWjZpNUhicnZvTWdQVlExVkJBMWdZaWdGRW83SVdrK0o3a2Q5T2ZvbVJpTVBvN0VUTEVaSHJKdEN4R2JyZExvZGxoYTNiZHN2QlEzT1VYaDdFZlU0eHNFYStObnVwZSsxTnp3V3d2MDJCL2VwQThhdjZubUJIY2ZSQnE1L0pDMWE5MFo4OE9zWVpYNWtzdDhYQlcrVEVrUTE0L1lQK0FtTXJKbkIxL3pvSUNRL2x0SitUb1E1azZBb3RVY3BBUFVwZlNFWEhJamdjZE41TFJZZUtLcVU2S0VsUEoxNFVJT0ZKT0hpcXFoaGpvUUljSEI3aVRIbEFzcUxBM2JySDg4amU0WGpRc2dmZzlDV255THJlZXI4c20ycUpIaENJaHR0L2ZTbkN2S2JYMHdHeWRYQTc5cGM3K2VuOWwrRzYrYXZNNVhOWFlsYzFRM1k2TXRWZGdaRnNIQVlDcFlNSVVZTUpnUzhNVGpLU2VaRDQ3SlNPS0JBeWVkY1NYWjF1bGtOSldtTWxNNFlIRHN6ZzlwMzc0SzBoVklCTW5Zem1IYmw4N2hyOWx6MFB4OWFaMjNBT3NsOWxsczJ2QW9BR0FqMkh5TDc1ZjQ1NlpYYm01SitWS3dXeTRMMlIzSlFIYnNGampqeGZYbkhlSDJLdlR1UFdhanM4S3haYVlhZ0Y1dDA4aGxwaTZDb3BwRUNoRHBWV3FOU2pwQXZPaFhOd29sTEZVSm9MemdVcmVDRTlEQ3k4R002WDA1Z3A5akgzbEpQdE1YakFpdnZKWTVjL0FzZU9INGVPeVJjTnVsSVhFdjJpeVJYWXVUQmoxTUQxVVVpaENNa0FBUWdvSWUwNlJ1bWlueVhJRjMrKzdDbjI0T3JaSCtOamV6K0JiMDMvZ05mMGIwUTVtbUZGYjcxMFRBZGVLNmg2V3JFcG5CZDJnUmdDRkZKZ0RHZ0lhTUJqSkg1TVVNY0VPallQSWpOa1VVaXZrM0ZZbE5pMmJaOFVBd2ZKalJMd01wWGw1b3JaVzgxbEN4ZFduOXYxL1YrbGMvTExCZURGTUhnbGVhSklmc3VManZtZ08zL3BvejFLSi9QZXFpV3dienYrK0I3UHd1UE9mQ3l1TDI1aFh3ZFNzcEo1UCtEUUZ6THdRd3g4aVdGVnNJZzhYeGtJNWZCRHp6TDhDNmNlRUlnbjZlREZ1WW9LQTRxVlE5Vit6dmYzNERBdTU2T1dQNGhQV2ZNRW5ESjFxc2xoNi91dldNWEZEVEdKRk5xVlFJVWs3NU9SOGdORnd0S25GT25rdDVKUk5BVndwaGttU05YZ3RBaUFUTEo2bWhaY3dlOGUraDcrZGZ0NytJa0RuNU1EZHNCZVo2Mk1kcGJBYWlWS0pVSStyT1FpMGYwTzdrand5aVZha1lJZ0tBUDNZME1FaG9na2p3QXd4Z0FDN3Q1NVNQcXpRMGllZ1Y2OVdabGIzTmlmMFEvdmV4bytkK0RqTWFuaGx3N0NYeVlBRFhneElKZGt2WmNjL1RGMzNySUhvKzhLcTZaVCtEa1ptWnZGcTgvOVU1NjE2UXg4YjNDTmlCb3RkQ2h6N011Q0c2RHZDaFFzVVBnU0ExZXdwR05GWjBvNlZ0NGhjWDZWZWpqNEdMK0FWTjRSWWtSc2pnUEZIc3oxZCtQdTNlUDV6RFZQNXVOV1BCWlRuY2xBSlVORnFSS0lEVW1zU1p3U0lxV3pKR1dZRFAwMFlScUNaWFdTZnZwYkdYSVRFT1dVQ1d5VFJDc2s4SHNwd2h3cG56d211UUxBN3NGdXZtL0grL0hPbmUvSHRkVjJHUjFaSmVQWkZDc3RCU0J5RXdHSW11SU9RdFpJRFRVUUlUbGJUQ0l1QVFHc0NLaUVBU2cydzRHOWM1aWY2UU9aRlVNNkxNc3l1YmxQZm5ULzAvWFQrOTZEMzBXT3Q2SDZKV0xrbHdiQVpQUFo3b3MyZmNROWVOa2pzT0RMRHJOc01KaVdWYzdqZFErK1JOYXZYS2ZYek44b1BpT0cxUUFETFREbisxalFBUXBmb3ZDRkRMVmk2U3VVV3FHRWs1SUtyeDVPUGFzUXk2VUc0bGdvQWlNNVp0ME05czl2eFQzeTQvajhkYytSeDY1N0RIUGtCQ0NWbGlIOFpRd01qTnpSckl2WUFWci8xbEVNQkgrQUFWS1VGQVpCd2xmOStraXZJRkdEUWxrTTJCalJTMEVKT2xYQUNMTWdsYVh2KzNycDFrdmx0ZHZlSU5jVTJ6RTJzUUc5ZkF6d1ZTQnphaTVSV3JHTklBa1p2OEhDTmlrUUlFeVFsSEYvQ1l6TlpINm1qNWtEODVETWtrQWxTNjNGMXFHVmQrMTlrbDYyOS8zNHB6TnpQT2Q3dnpRUS9qSUFtQ1FmOHBjZTlXR2NzL0xSYmpnc3JIUzZycitmUitpWXZPNlJsN0F6bnN1MnVWMm94SEVCQXd6OUVBTlh5QUtISExnaGhyNlFvYTh3Wk1sU2c5cDE4RkpwazhIaXlCQldVdzh4dWZRNXdPNzVXN2hKVnNzZkgvWThmZGFHWndXSEVoNk9IaUlaRFVLaXFDTFJhOUU3YWlBUkYwMWp5aGFnVkFxQzM1bUpEUys3OC93UkFCeUQ5eDI0UVJFckZwU1lwUW9Ua2hXaTJFcHdqL2loQWxEMThQRG9tZ3lBNFp6cm03ZmUrbGE4YnV2YnNNdFBjK25rMFFBaHpsY1VZOFZLM0JDUkdhOWpmd0N0bUdBdUJKalNtQmcraWJkTkNteG0wWjhiWXU3QUFwQ2JRRmd1eVdCdUxjUmZkdkJwZVArdTkrRjN6OHp4dGw4T0NIK3hBQXdKQlFLQjc3NzRxQS80QzVZL1h1Zkx5c0JrYm5DSXgrWkx6RDg4N0M4dzNSbGk3M0FmbE1vRjM4ZUNIOHJBRHpuUW9ReTF3c0FWTEh3bEJTc010VVRwSzFiMGNQRGlWVk11aTFSS0tFWEZaTmd6MkM1Wk9ZL25yM2ttWG56RWl6R1ZUVkFCOFZyQm1od3BXNEFRcGFwWVl5UXRUVkt2SGtxcXdvUG8zY0VwU2RlQ0x6RndmU25jZ0E0VmhCUWpCc2JrSExYam1PcU10N1IwZEVvQkRuMEJDY1FIREdPQXJwR3NURzh3b0tnR0xldWc2SmhNQUhEWFlBOWVkZTFmNEoxN3RxQWNtZExKa2RWR2ZTR2dKclVibkNhUmhHeFlZeEFTZUlKOW1FWEFNNkdmaGlURVdvTnlVR0grMEFBd1FuZ0ZsbWVLUFpYZ0EvdWZnSS92M3ZMTGNreCtrUUFVZlBrY2kvdGY1bm92MmZoMjk2Q1Z6K2JCcXNva3o0dmlBRGJsRTN6REkxOGxNOUxuem1LdktKVURIYkx2QjdMZ0J6TDBoUTYxTkFPdFVMaUN3K0JjU0tFRkNuVjA2cUNpNG1Mc1Z1RUo1Qmg2aDEzejEvTkJFL2ZBM3gvOU4zTGkySWtBZ0ZJclpzWW1TamZhWVlBb3FTWUV5QUJBb1NSRFVNS0lUWXVuczlXOEhDajJ5UTJEMi9qVlE5L0VkTGtMdS95czNGeHQ0OTVpdHl4VUM2akVFZkFRSjlJelkxelpXNGxqZWtkd3ZhdzBtZTN5ekttNzQrekpNekRWV2NsVnZlVXQ3VTVUcVlNUkN5UEJ4MjZtVVNNYkhKd01CZURVMWNWUTN6NXdPZjcwbWxmeHkvUGZscEVsUjZFam1UZ3RBRml5bG4xQkwxdEpkb0VnMXEyRVBJbzZpOElFMmErQ2pzMDRISlJZbUIwRU1lcmhaYWtGZGhTUTkrMStqUC84Z1UvOE1paWFYeHdBdjN4T2h2dGY1anEvditHTmZQU3E1L2s1bHBuYXJCek1ZcTIxOGcrUGZpVUtXK0RBWUJvTE1zVEFEMlhvQnRyWHdneTBETkVOWDJCSUw0VldLSDNGa2s2R3JFS1dzbnAxVU9OSmVuWFNNVjN1Sy9laldwakJ4ZXRmZ0Q4KzZvWFJ4cXRDeE5SWUNtaHFReDFKd3NTd0cxV3MyTnA4QW9CYkZtN2hsdzk4QzVmdCt6eStXMTJQRzRZM2k1ZWNrQkxJak1DT1Ewd1h1YzJZU1ZaSGMwMzBtQ3Rmb25KOXdBMEV2Z0NRSS9PS1ZXWWw3OWM3azZjdU9VVWV1dVlDbnJ6MEZCTUhCa2NIUUpDSkVNRTBTSmFqdEQxdEphSHFtTnNPQU1nYnJuc0xYM2JUWDZNL01vNGxJOHVsZEFNaUFZNGlJR0dNclJsc2dyUmloQWcwVWsxc2gwUUhDQ3d6SytqM0N5bG1DOEFZd2tHeDJvajUwWHhwL3ZIUUE5MjErNzcraXlhcmZ6RUFqT0o1L1BjUGY2bDd5SnEvS2NxeWtoSVpwZUR5bVNGZS9maVhDMGNORGc2bXBWU0hHVCtMVWl2MFhZbUJMMWlna01KVktMVDVxYlJDcVlxS2prNjlLQmorVnNldUhjSE8rUnR4UXI0ZTd6emhMVGhqNG02c3RKTGdXTmhBM1VGQ3BCWnBnaEhDY09yUU5aMzYvcjkzNkFkNC83NHR1SHB3RGI4MWY2VXMrQUthSzd2ZEZkS3hJekFBckdRUUEzaXZRTFR2Z28vTHRpOEtBQlJqUTVxcEFsNElpTUpWRmVhSEI0Q3FSRjRabnJYa05IbkV5dk41NFpySHlLYXhUV2tzck9qRWlsRURJNHBROUlSSU53WndLVlVkQUpITTVQamh6TFY0em5lZnk4c0gxMkJzOGhoUUMvRlNxMWdFTXdOSjN0VldTSENoSXJjWVphOUZTSWdRZ1F6bUMvcWhCekpEZUtWTUdDdFhEdzdweDZmdmg2L3UrVkZNOS8rRmdQRG5CMkRjRVpPUFBPTCs1ZVpsWHlpWEdOV0Ywc0tDM1lQNzVTOGYvVWN5c1hTQ3UrYjNBVlprM3ZXeDRBb1V2c0RBbFN4WlNzRVNRKzhRSWhzbENsL0JxMGRGSCtLN3FpRzFRQ0dlT1hmTi9KZ1hManNQN3pqcHJiSWtuMkRoUytuYXpxSmhhZjFmUTBLaHFwS2J3TDNOdWxsK2ZNK256VHUzL3d1K3N2QmRzSnNCM1RGTWRwY2lnd0VVVUxwSW9RRWg0QkI2RElVcnloa21oN2MyN091RUxTZ1p0QndGRkJpVEF3SjRWY3dNWm9GaWxtTXVsL3VQM3dOUE9ld3BmT1JoRDVVUk93SUFyTFNpTVJaQ0dJaWhRYkxqS0FKUlZSV1BjRDhMYnNnLy9NSEwrTGJiMzRYdThvMWlJSEJVb1FDWjFNNU5BaCtBbUxnWStjNlFLa0ZrWWdBRlJRTEhYUzVVcE5Ob3dhckhpand6WDUvL252N3BEZmNGTVV4Kzk4OExuNThQZ0pGbzdodzNjWno4d1JGZjk4ZU1Mc1BCU2psaXhPL1pJUzgrNzFrNGJ1UFIyRHEvUXdCdzRJY3kxQ0lBMEJVWXFtT0pFcVZXTXZRVlMxWlNKbTZQSG82RWk1UUxSS2lpWnUrK0cvakg2LzhQL3ZyRVZ4QUFLblhJVFVaVkdERWhZdFdPb1hsNkViRnFBRXlYaCtTZFc5K0RkKzU5TDY4cGJ4R01Uc3A0Ynhtc0NBQVh2R2xGTU5rbC9xc2hTeVg0cnRxdVBhcTV3cVQ0UXBsbTRJRlR2NFFranVxMVYwQm9SYXlGZHg0THhWNWdNT1RwbmVQa21lc2V6eWR2ZkNxV2RaY2kzRnVGTERnaExUc3gxUUtJT0RwbVJnQmsrSmViM3FQUCtlRUx4VXl1RjVpT2VEaGt4dFFPU1BKR1FyU0dzYXdGZ0VwTVpCREdOQnNSQWs2RmZsaWx6UVFCbkZtU1pmall3UzMramJkZWhFczNXMXkwNWVjdWV2cDVBQ2pneFNKeWlYWmVlZnkzM1Qwbno4Syt3dG1SamlsMzN5NFhuZjVnUFBpTSsvS1dnOXVreW9pK0RxVDBKZnF1d0NDQnpWY3M2RkN3bE5KWERPRTFEMGNubmlGbHFsSlBRV2dKc1AvZ2ovSDI0MTZIWng3K2RIaFVoQm9SWXdrb2d0b0tsd0hFUTZtcXlFMkdnUy93MXEzdjVOdHVmeHV1OHp0RXhsZGpQQjhIeEluemdWMndCb2tWWkdTZEpkRWtKQ2pHQ0RUT2Q5dURyWkVXTHRiL0FaSitZK2pYZ2VTcWVrV29LWkZncXlwRit0VU0wVDhveC9rTi92Y1BmNmI4empHL0xhUDVhSWp3R0pFTUp0cXhtaGhBRWFnNkVGNmQ2WnFlZm1MYlo3RDVtMDhWdDN5bDJHd2NJcjVtR2pYQks1b255dWlwSkthUXdRWkZVcyt3OUpXSGxsVUFzQU01SXBWMDBNSDdkdjg5MzczcnhiK0lhTWwvRjRDMXh6djYwcVAvb2J6LzFQOXhCd3VYWlYzcnB2ZmhIdXVPa2Q5OXdFVzgrZEEyT0FMekhLQmtKWVd2TVBRRkNwOXNQVmRMdmRKWEFYQUJnRlNvT0IreWtoV0NtWVBYNGowbnZWVWV2K0h4S0gzQjNIU0VkU1V1NmtVSlpVUXF1V1FLUUw2NDd5djQ0K3RmTGo4WVhvMThhaTNIc2tsUW5TZ2NFREpORU9tNTlFOU56aXlLakN5ZXFZU25SY1Ixakg0Z1dweFJHQnBweTRoWUhneUo5cU9TZ0RKVWh0Z3Urb01GVkRPN2NWTG5DUHpma3k3Qm93OTdoQUJncFI2WnNYWEFwc21iVWhBR2xTdlF5N3I4d3E2dnllWnZQQlhUNHhsR2VpdkUrU0Vadlh1R0dXTndhcUlKUVRRU0hSSkQyUkk0ZGlXcENqaUcvampPQTB2RW14bG04dTh6Ri9rUGJ0dnk4em9sL3owQVJuZTg4K1RETDhUalZtMHBUZVZNSDFiZFVOWkp6ajk2NUcvTDdzRkJEcXNTQlVycGE0SFN1OGJKOENHcElDV1NPdldzNk1XcHdpSEVlSU1SRG5pMTJMZnZHdjNBYVcrWHg2Ky9VRW90a1prc2hWU2hKTVVZUWxXTU1TalVvV3N5SEN4bitPZlh2RUxldHUrOTRwYXM0Skx1VWdqTFVGZ0VVRXhNSjBuNjBRQzFMUWZXQm5xcS9FaXh1dmgvQ1dHM1dEd0V4czV2S1Y4YVVYQ0dXby93SGNsY1RPa3NjZUdacUR3anBFSm9JWkxyZkRFam5OdUx4NHcvRVA5d3htdXhZZUp3dER6OEpKN0ROMnVJOFphK1FNZDJlY1dCcStWUlgzNHNkbzFXNkk2dW9YZEZ5S1VJNHAzeG5xTkFoa0RqZUxUZVVqRzFnWVJDb0JUNE9GbWxwNnp1MGw1VFRMdS8zbm92N0ppNzhlY3AvVFQvOVV2dTRqMWZnWTZjdm53ZDdqbis5enBpbkprWEFWVHloUVU4NDM2UGsrbGlualBEZVZud0E4eVhBL2FyQW4wM1JOOE5NZkJEOUgySndsVW9uR2VwanFWV1V2bUt6anRXcXZDcVVBWFZHTzdiZXhYZWRmSS95T1BYWDhoU2greVlUbGpxUUZUVWJvRWF3RWZ3ZlhIUEYzR3ZiNXlOTjA5L1FMckxqOFpZTmdubmhsQU5hU3VFaWVuM3FLbGl4bGdJR2F4MkRjc1EvcVlKY2RzSVVVYndJYjVlbXpoS0Vwd003SVkyYndCQVRmWWd3VUNpeDc4bDV0QVlLa25uQ2huUHhtVjgrVEh5NzhYWGNjWlh6cEgzM1B3QjVDYUhOVUR3aE9ObWlIa0tDa1ZtTTVSYTRoN0xUK0ZuenZzRWordVBvNWpaSTVsMGdWRFNsN2pxRkZHVTJxSlFTVlJwc0lOSlFNVWcxdWZGT2dRZ3M4TDlwZXBKM1dYMldXdmVCZ0Z3MG4vZmxQdlpBWGdwQkFMMUQxM3lkM3JNK0FiWnAycHNKbnJvQUI1KytybFl0bVFLMjJmM3lZQVZGcXFoOUtzQ0ExZEd5cVhFd0ZVWWVsOUx3TUs3YVBkUkhGUXE5U0FWeHVheWQvZlY4cFlUWDZOUDIvZzBsRm93TXhrOFlyQ1RGTUFBeGxEVlN3WURheks4NXNiWHlBTi9zQmszOTlRc21Ud0s2aXNvbktneEVuekRNT05oamduNldPMUdDZlpTYVBFU3dSWmZoNUFQRStOZFVrdk5HQU5UbFNqSkFzaFVSYUNDVkdPQytGSWdnQnMwTmYxUkN5TVNTa2lrNk1UUm8zSkRqSTBlaHVrbDQzamFWYy9COHk1L2tReTlJak9aZUhWaDhZeEFHUnhrQTJGbU1pbTBNcWN1T3g1ZmZQQi9ZR09SWVRqWUwwWnNHcEF3T2g1cGM2VDdpU3BGR2k0UmpSMWlKRkdIQUNYVGVWZm9mY2ZPeVo1NnhFdHhFVHcyeHo2R1ArUDFzeUUzNnZ1Ukp4Mit1WGpNc2t2Vm9zb0xtMVdEUTNMaytGSTg4L3pIWWZ2MEhzNnhrS0V2V0FWcVJRcFVxTHhEeFFxVktqd1VGUjBxRGFTeWkzbDhDcUJTRDdFOUhqeHdOVjUvNUN2eGd1TmZnTUlYNk5pT3NCVXhpSlFFS3pqa3lMamdCL0xjcTU2UGR4M1lnckdwbzBSVWdNd0JFVGpCYndqV1YxRGZyT2M0MXFraGFrcVFoQW01TFVpQmZVbnhVelFCNVBocDZiZTRpTW5malV1Y2dyT01JQTlpcTQ0Q0kzRE5RZWRSYW9vdUxZNTZ3a0pnYkk3NVE3ZndQcDNUK0w2ei90VWN2dVJ3bEw1aVppMVVHWWozYUlZb2dNcVg2TnFPWExuL0dqM25QeDZNaGFVVEl0S0RoMmMwUGNJVXBwdUthaUFhRWNIbVM4KzF2R1g0RkdHa1lzcW8zRm9NK1krN3pzYVYwejhNdS9SblU4VS9pd1FVWEhveEo0K1luSEpuakx3R281YlNWK05SSW5mRWcrOStMbmN0N0VmZkZ4aTRJUWUrbEw0dlpSQnJPSWErNU1DN2xFU0swbnRVem9sVEQrYzl2SkxPTzRydDRPQ0I2L0RTdFgrQUZ4ei9BbFJhSWJjNUJhSUdFTk40Z2F5MFFvNE1CNHVEZU5BM0wrQzdEbjVZcHFhT2syQVdWV0FUOHc4YVIrc09WbHpVWHBkUnp0VTJZUklKRVdwUlZTYXBxUWo1eDR2MmI1MFRFSVFrUXZXR0pLc3I1QVJFTnduSkx0UmFBaWxNWTMvRmIwMlorSjVrcFFYR2xtNlNyN3ZyY2M4dlBJQmYyUDExZEd3dWxmZUVzU0JVR0ZXeGtOSzF1WlMrNUdrclRwSi91dGRiNFEvdUprd1lHV3V4M3BST0pXbkhlaWZHZ1NWUE9WSGFpZmMwTURpbzRMRzljWG5FMHJlQ0VGeWFESnBmQmdBM3cwQXUwZmtMSmw3cVRwazRRcWVkbXR3YW5abkZPY2ZmSGQyUmpoeVluMk9wWGdhK3dzQlh3ZHYxRGtOZm9WQW5sWHFVR3RSdjVUMGNsQzVVcU5Hcmg3VWRPVGk5bGVlUDNSZC9kZHBmd3FHaURiRUJVVlZCYkRFSkJEc29Oem0yOTNmZ2dkOTRzSHl6dWxHV1RCNkh3Zzhpd0F5UW1yWlFvdVpCNkNRVWF5QlpLOWhveDJtYTlGQjRrZFFra0ZScDJQMU1Yb1dHeXZWZ0U4YlBDUXNtRkJwQ0tKb2tYV2duRXhPbEF6N1Q3NmhaN2pTdU1PQVdDb1VDVncweE1ycGVkaS9KOExDdlB3cWZ1UDJUN05vYzNwVlIvaHBDVTRtbm9HTXpGSzdBRTQ1OG1QejF5WDlPdC9ONlpDWUxOb1FhaWVDU1Jjd3BwVVZhcHczSWFDTkdNRkxDMzBZeVRLdVRlMHpkeXp4cDdmTnhFVHd1L2RuTXVwL3V4UmZENEZKbzU5ang0M0dQcGMrRDg5NVF4QStHc21ISkNybmJwcU94L2RCK2VLZ01xb0pEVjZHb1NneDl5YUp5TEwyaVZCL1M2SjJqOHhyVDVoV1ZEODB3SUVibXl4a2U1U2Y1cnJ2L0l3RmxhRUFBd2lpTU1XQklzWVNqazh4a3VHWGhkcHozellmeSs5aUIwZDZSS053Z1Z1ZkVqVjV2NG1CWEp6MFpVcXdDdlpQc3REb3pENEJLcy9HVG9JaHNTYTArNi9XcWsvQlJXL0hwOWVHNThPR3FJVnNVR3Y4SmVBTmJrbGVUcEt5REtRR2NsT1FVR1hwZnNKZU5DbGNjaHNkKzQwbjR4TlpQb3B1RkZINkJBaVlreFNLS3FUekxVV2dwZjNMcUMrVzVtNTdEY3QrdGtxRW5FaWNYeWVGZ0JGK3lFeHJ3dGJkaDgyOWtJVENrMVk2b3VjZlNWK0QwNWV2d0JQaWZHbGMvOVF0UGdrQkFmY0xhVit2NjNqZ09lVEwzSXRWQXp6enhCTzRaSHVSQWhsamdFQXNZeXRBWFV2aUtRZktWY043RkprQ2hKUWFwb2NaV1ZWUWhsUmVXQ2c3MzdaRDNudmxPV1RlNlN0UXJyT21ZMkxLbm5nU25EcGxrdkcxK0J5LzQxaU40WStlUUxCazdUSWc1R0JNMGJOQ3FzWmRmWEZsRTU0RWhEWm0xNnBNSU9MWnZ1TEhNZzNvRVlPcTBmTlJhTEFRT21uVkJTNGtxSWdSRm1seERncUt0Qlk3UGFQcXVJR2FvTVY4QUNjelJnU1ZCcThaSlNXc3NzT29JUE80YlQ1UlBidjAwTzZacktsK1pnQ05TUTZ4YUFFTUxTdy9GcTgvK1c1dzZkb0pXczd0b0lsSnJleURaRi9YTnBISEdPNlhHSGRqcWkrZzB2SGUyVW5kS1o0Vzk5L2pmUUFGcy91blY4SDhOd00yd2VEejgyQ01QdXorUEdYc0VweXNIR011RmdSeStlaDNHcHlaNGNEQW5wWGNZVmhXR1ZZbkNsUnhxSmFWM1VxcVhrcDR1RmhINU9zU1dWSzlqWm5MTUg3Z0ZMejc4OTNpdlZXZUZFSlROWWpSTXFJR1dBVldSbVl3enhRd2Y4NDFIeTAxbXIwejIxc0RwZ0JBVFRPZ0lORW4yTlJ0TE9wS3dFSWF1VkRaeWVOSlFkOENpSDlSbVl2UVZvcVJzTEVnRkl2MFh0R1dVZUdFb1RFODBBYlFHcTZ5dFBaS2tTajMwaUlyYTNXYTBEcUpzanhMVXdZZ1ZydGpJQ3k5N292ekg5aSt3YTd2cWZBbmIwUElFZ014WVVHbEdUVWMrY000N1pITG9RRmFRdGhNaVVkdFF3cmNsTVg0bkxERXgvM0gzVU9CcFVOSGp6TEduWm1ldXZBOCs5Tk43eGY4VkFBVW5oaThxZjJ2Snk3a3lneGxHWmFEQWNZY2ZMbk9EQlZTcWdXSUoxV3NvMUtQMDRhZnlucUZCa0ljcVVTbUQwNEVZcjRlVmhZVjljcHlzeDUrZjl1ZmkxY09hSEtyUmpBL0JCQU9qcEFrMC9aTy84U1M1MG03bDJPaDZsSDRZUlI0Rk1YOHV1aHpwRHVKandvN3RpS2ZJZ2c1azNnOVEwaU16bmJEQkdZNFJTZVlkSVZFRmhyVG9LS1RxeUVlOVB1RVZTTjVPTXV3VWtMUUwwaDVvcWVabWZORkVpTzVuU2xnT09UQXAzSmNZa2tabENpRlE3MmtsaDErNVFTNzh5dVB4bmIxWG9HczdMRU40TVdaRHFBQVFZeXhMWC9MNFpjZWJWNXorQ3VyK2JiUlpKeEdheWZFSWZGTGcvbUpOYUlwSDFzNS8yRjFHQk1ZSTFBQXdCbk5LdjdFTC80RHhTMENFbzhwK2JnQnVEdFh5WTQ4NzdGemQxRHRQWjd3aXN4bjdRNjVkc1FLam95T1lId3lEdXEycVFDNnJRK0djVk02ekNqWmVjRGdZQ0dhdkdydkxLM3c4YWNOTjcrV2I3djQ2VEdRajhNbk5NS0hNTlNvcWV1L0ZJc096TC84ZGZtcndkUmtaM1NUT0RZSndDcWZIMVA1L2N1ZVVBbnBDa0lrQ01udndWbWJ6aDNnc2x1QVlOODVWZldCdS84MXdjQlRhK0UwdGNCRlFCa2FudHIyams1SlFscnhtUmdhUkFsRTBTZmFLbHEzSHhQWUlxQ0U1VkJQRUtBMGttYkRlQW5uWUVMVkZGb0lXQnFvTzFuWTR2MnlWUFBUekYrS211VnVSV1NzaFQ5cXcvc0JJVkZkYThzV24vb0hjYytwZXhzM3NoalY1aXhGUE53b2trYmhvTXVvcjJvbXA4dG1EOEdJd3E0NTNIejBQRDEvM0NGd0MvV21rNEg4RlFBQkFjZkxJSCttNEJRb2ZhVW92RzlhdXhPeHdEbjBXTXZCbFNKLzNKWVkrdE1sd2RPSzlEK256SWFFMFJEaElWRjdobllmUVl1SFFyWGo2WVUvQStldk9SYVVWT3paa2Y1aGFNUUtWOThodHpyZmUrSy95anYzL1pzYVhIUXQxYzRDQitHalVhZXhzWllJNEFUM0J5Z08reTZJL28yYjNUcjV3eFRQNWpUTS9xZGZmODNLOTRld3I4UDE3ZkFWL3VmclBLRHQzb3lxSEVNMEpENmkzVENFUmtnajZQM2t5U0pJeURERFpjYlhIRW9CSDFkcGtpcUlJTVh0TGtuZFUyL2dhWFoyWUtacGM2NFQxRkM5T0tHVnNaMDFEWVVaeHBrU25ONG9EbzE2ZTllWGZSUnBWcVBvRENDRTBGR0ZKc0tmbFRmZjZhM1FPOVFrSFNPampoTWJUaW9CcnVuNUUzTEg1MFRneEV1MUFnYUR2Z0dVNTVHNGpMd1FnQ1QvL1BRQnVEdTI3eHU2MTlHUTVjdVJCbkhjVU1SbUhCVllzV1lMUjhWSE1EUWFCVnZFT1plVlF1S0IybmZOMERQbDhIaXBPR2JxUXRob0NrVURwQ3l3dGwrQ1MwMTRlYm8rSkdXRHRVVmJxMExVNXI5eC9GVjd5d3ovbXlOUlJjSzRJdWM1aHAwYVRTK3I0clVZMzFrcVh4WEFmTmc1emZQSHN6K0oxcDc4V2Q1czhSU29WS0F4VzlWYnlaYWY4RVQ1K3hxV3dCL2VCV2dKcWF3Y2xBQ1lCTExaamE2RU5iT3k0Y01WWE14cFZ0ZTVOUzltU2FKS0FHVkFjSE5kSW1nZklNSDF2RWtoMUNDYllibEZrQm5YcGZZSHUyQnA4YmU0S2VkSFhYNFpjTW5oNnBpVVdFK2JUbUV3cVgrSE1OV2ZnYWNjK21YNy9WdGk4VXc4YWtZRkNRd01rQkM1V3FRSkFhV0JEcnh0NEFqQVc4MG9jTTNyLzdPNHI3NFhILzllMjRFOEc0QitjRXdCd3p0VHpkRTAzUjBrUEVDZ2NOcXhlaWZsaWlJSXF3OHB4NkNzTVEySUJLL1dvU0tsQzJ6TjRaVkkxb25IamVGVlkyOEh3d0ZiK3djWm44SWp4OVNoOWljeG0wUWlKTkFTVUJnS3ZIcy84K2g5Z3JwZERUQlppdWpXREdpWkpZNXZsY0tpQ1Ftek80ZkNnSE5YdnlWZnUrM25lYS9tWkxId2xsVG94eGdDcUlDaUZMM0RCK2dkd3k1bnZRVFc5WFVTTVNEeUJxOWF1dFhxczdjSmFPeTZpYXdLVW9EUlJKU05Wa1NjYU40YjkwbkxHRFJNajIyQ0lKY2U5bE1qS2hQWDRYWkhLamwvRzBLSUxoTENxU25TV0g0TTNYUDlQOHRGYlBzdmM1Q2k5aThIY1ZoUkpERHdVTDcvblM4M1NjZ3hhRk1IRFo5VHJDWE5OdVdma3JGb3F1azdHamEvU3lDa3VxTWVSdVppekoxOEFBdGo4bjR2Qm53UkFnL011Y3lQSGpCekdJOFl2UWdHS2gyWGhNRFl5aXBHSklQMHFINlJmNFJ4SzUxQXl4SElyRFpFTktFUTFhTVBVM1ZaVklSUU1pM211ejliZytTZi9IaFFLYTYwSWFHbzJBQ0Y3MkJyTGwvM3dFbHhaWEMyOThiVlMrU3FZeWRxaXJBZ0pqa0lRT0lhQTkzMHNIeW8rZmI5UGNNUFlCbHY0UXJvMloyNE1MQ0RHaElMMHJ1Mmkwc284WXUzRCtNelZUOEh3MEZaYXlRVys5b2tsWmEzVWVwWWhnUVJBN0ZvWjdQZG9IOVlwQm1sd2RWUXJnZzR0cWRtbWFNSS9Ja2x0eHdhdDlhSXdhVzRteTdnVlc0a0pQT3BLbUpVcjhmeHZ2VkIyTE94QlpnMjlocVMyY0NreVkrRzhreVBHMStHUHpuZ0JkTi90Umt3M2dLaVZXUUZ0QVc3UlJUYVBSeTJkT2xBck1zNTc3MDdwUFRJL2FlV3A4WVNEbnlqbzd2cUppODh4SUdEdXQrWUpXTnRaeXZuS3cwQlFPa3d1SGNPY0x6QjBKZnV1Wk9GS2xENzBhcWxjeUdZT05vNkd3RG9wWkR4REkwcERvb05xZWhkZWZPeHpzSHBrSmJ3cURHeDB1eUFVVWU4OWM1UEo5L1pkS2ErLzRVM0kxeDRGUlpsTStLaXFvZ21mbWdVb3c0bVZrc1B0dVExL2UrSmY4cmpKWTFtNGtybnRCaW1zSm5USFFyMnlZazBHRDhYclQvcGJQVW9PMDJJd0U5citlVVJEcjJVSHRkTmt0Q1VTNHYxRnhnWmFKeU1JNEdNd0xyZ1NnYU9FdHFLbWpCSEI4Sy9VUWxWYW54MVVTTnJJdFZQYW9xOWhhSnp4TU4wSmJEZjc4ZEp2WGh3YWVHZ294d3IvRFZSaExoazlsTSs5Kys5d2ZXODlkVGdUTWhmVDNOUzdvT2tMQzZUSEVKSnpWUW1YN0JJbFNoOFFOVVBQSXpvOVBYL3NxVFdlZmlZQXZ2SXJIb0R4Ui9XZW9nWWhHOWFIZFBTeEpTTVlEQXU0U0xPVTZxVHlQdVgxUVRXNEJCN1IyMFhxdXh4dEtCaTRhaDVydVVLZXR1bHBWQ2lzTVpDbWlvSlVpTmlnZ1A3NEJ5OURPVFlDVVJPb21jQ2FCY0k1R0l1aVdudWx6TkJsTWJ1ZGoxcjVXUHoyeHFkSXBaWEpUUVlUYjFhRVFRVzNRR1hDcDhwNFp4d3ZPL2JGUnZ1N0lLWURFSXlmSGFjNDJIYkpkNDNMVVFzTXhHNTlySzIzQU5TVU5jYVdLbStDcmxFeWF0TmxSbU8rYXVBU3RaYStCQ0lucnpHaHRUWUNFY041QkFUZWxjaVdic0Q3Ym5rZnYzVExaY2l6WEx5dlVxYUErS2dCblBPWXpDZmtKV2UrZ0p6ZFEwbjlhdHFZcXd2ZDAyTk5obitMcWdsOVA3eWtjSkZsUWVnUnZjY0JHTVdyTG5PTEp2dy9CZUJtV0lodzJVTlgzOE92N1o2aUMrb0FJeWc5ZXFOZDJEekRzSFFvU0ttOGwxS1ZGUU8vNTVQZFI4QnJnSXBxQ2dxRWhUR1NRYWQzODFsSFBJSExla3ZodkVQVGpoRXhUT1pnWWVWald6L0RMODllanM3WVd2RlZDUkdEa042ZVFDR3BhMW5rK293NExXVnltTXRyenZncmhuQ2xoUWtoRXFaMVFyT2M2YUlSU3c4blQ5MzRKSnpZT3dGbGVVaEViT0ErZk9PQVJFOHA4WVZOYUNRQUt1WDFRV3JwR00walRSVkNqU1BTcU9Jd045cCtQbEdia01TdkI1bW5qZmtScFN3RHh0Tk9pSG5pbnBDbEsvaVNiNzlDMUlOR0RPb0llUUFpckJGNEtKNTQ3SVd5eGh3R1gvWGp0OVRCbTlxYVFQcjhaT2hLaWhrMzlDQUU0ZUF3aGNpOHI3aW1zeEVYSFBaZ0VNQTVkKzJNM0JVQUFRQnpKMDQ4MGkyM0ZrTlZBZ2JxTVRyUml6RmRoVlBQd091RlRxUGV4NEpxSW9UYmxORFFEcGRRcWIzN3lwZm91UTZlZXZ5VEl4emFReEFRQ2lNQ3I0cUx2LytYQnFOTGdMZ0h5QlJlQ3JmTEdnaEIvVm14OE5NNzhIc2JuNGxqeGplS1YwY2J1REJERSt6TGVycTRhRU9LSVlRSzZaZ2NMenp5QmRDNWFTSzAxWWd1QjVDY2tjYUpDQVBRZXUwYlJ5Um9SOVNiQldqV0QyazlhNGNrSmlCclJGc0VvaVN1cDdFWFUxWXp5UGFuMWRaRVRBOERuSzlvUjFiSTkyWi9vTysvYmd1c3NYRHE2bThMQnkwWmVPK3dZblFwZnZlb0p3Z083b1kxSGRUaHR1YUdVTnV1RWkySmRQLzE3QWhnQmZCUjJKUUFsaHJJYVNPUEJ3Qjg1YTdUdE80TXdNZkRuM2ppaVIyL0xyOFE0UnlEakZvWjZSaGtJemtXcWlHOGV0QVJxcVJxU0Jvd1FPQytJbWNXaUpRWUVZaElzU2FIWDlpREI2MCtHOGROSGNQU1Y4eXRpYm9xM0pDcWlqVVdINzdwNDd4cTRXcDJ4cGVKMHhJU1ZVOVV2dlVDaFVVTWgyRTVyVEJSTHVIdmJubzJBZENLRlVYSXJoYlVVS2VxMXNxd2dTQmdqQ0ZKZWVxUkY4bVJkZ1BLNFp3S2JMQU1hcnM3cHVxbkpaS2dkaVNxTFVrT1FyMUc5UzZwblN1Mkhxc2ZaK3NGOFh6RTlCQ1Q3WnhlMWlvbkNtQ1B2bm5nQXNKbkdCRVZKMWkraW4vOWc5ZWo5RTV5YXhtbGJNeStOTENoMXcyZWZ0cFRPRjZNMGJzcWRGcHRiQXZVVG5SN3o5YmJ2eWFXR0swVGdqQ0FaaGdvdU5ZOEVNZXZXOTYwNC9uUEFMZ1pGZ1J1UG03UGZiQXFQMGJtMU1NS1VKRTJzNEFGcW9wMEpEempJUysrN2pRZjdUOGdHR21oU1NUcW1Rd2FoVVdKcHg3MXBMQ1hJdVhjM0pnRU9rQWhmMy85bXdSVFN3SG5JVWJxVFZrWFUwYy9JT3BBaXNuZys5Tzg1OFNaT0dySlJqb04vZjVpUXlCZ2tiZ3d0ZlpzUFE0REEwK1BudW5nLzJ6OFhXQjJqMWptTVFnY3h4QUpJQ0pFL0tGMVRWTlMwa2xWMTJ4SitKS2tINXR2VEJnVUlDUjhDaVM0bnNtd1dteDIxV1YvTlFRWmRrQWlGWk91am1TV09zZXN0MVN1V2JnV2w5N3dZUUxHZU84RXhqQTJhQkl4Rms2OUhMWDBTRG4vc0hNRU0vdkZtTlJWaXdsWVVhSkVzUmM0d0ppa0dxbVljRWhBQ0QrNnVFQUZIVloxcDh4UjhsQVFFayt6Lzg4QUdQWHYwVk9Qd21nbW9XY1lBU1c3blJ6ZUU5Nkg4elJjTUkyZ0NCdFdXK1VQU1NBSUVVQklpQkVqVlRYRXVtd3R6MTEvZnhKTURiampWZ3NIQjFwaitjMDlsL083QzFmUzlwYlNxeVBpb1JxMWFtdld1YUZoakFINmZYbnMrb2NqckZYd1VRU0l2UjJSVEFReGJjeEg2WnVBWWt3SVh6MTk0NU5sTlZaSVdjeGlVWWRBVGFxemNZL3JGSDV0WkVJeWtNU25CQzRKdUdGWXIwWnVCTXlrM0Ztd0ZRaXU4N0RRV0FCY1JPb0ZSWk15eEtKQWJhU1hBU3N2bUZ5S04xN3p6d0NRK3J6VytsQkNXSTBBOE5TVG53RE9MZ0N3NmYxU3g2ZmoyT0tiQ1ppbXEwbkxSSVVBcUlJMlFBR1JrVXpNeVJNUEJFQzg4czVxZUxFQjl2Z1ArWXNCd3hYWmVUUUc5TFR3SVFsWnVoWXVuTEVoWGdtdmRSdXlob3lyV1FraUZyVkpyQVFrYWVqNzB6aDcrUmxZMFp1Qzk0NlpzYzNFMVlGRjROMjMvaHQ5MXdadlNKSkdGOU5RRDQzZ0J5RHdnS29UY2NBWnEwNEZBREcxNUlzclhnc2FiWVJTZXFZMUJ3WUdUaDJXZGlmNWxBMVBWTTRmQ0YwTjRuMnhsZ1poNHFrMXh4bWI5c1oxQ3RRSk5ZbnEybHhxQnM5VWk2SnNvYmd4Nys2VWlsZmJBSWlwVkUzSlp5dU0xRERqb0Nnck1iMUorZTYrSy9IRjdWOFRheXk5VnBFSENHaTF4b3BYeFgwUHZ3OE9HMWxQWDh6QnREbkFzQWpoQVkxRVVmdUdJakVLSXBLd0JLamhCQWtTWEczT3hkRkhMNEc1UXpMbElnQnVoZ0dKMTV5eDRuUmRuUitEd3F2QVNqcmJncG1CYzRUM0RCM2RJL0dyUGpHWGFiMXIyeUZOWkRqMHp4aWlQOFBOaHo4aWdMYjJ4UUtqN2xXUlNjYnAvclI4NnZiUFEwYVdpdmUrdWM4WWFraEdlN1JCNnU1a1pUWEU0YjBqY2VUa3ByQmN4bEJpTUtJTmNUU2pURDVvK3NTMHppS3h6L0x2SC9rN2tyc09xNnFJOHk4eHVTUW1GZGR1WUxpTnhXRlVZVW82RlNaSlZ6OVhQeDVJdzZUY0dEeGFqUlluYTFBbTBTcTFjbzk4VlMzeUtQWEVvelp3Q0Zxb0NLZ2pCbS80emx2VDEzakU3RzRUZThKNGVxd2NYY3I3cjc0blpIWVdZbXdFRWxpWENYcXdOWmJFcmFIMnd0S3NDb0F5Ykh5VzNuR1YzV0RYek44YkJIQS9OTDJKN3dCQUFJQy9lMzQ2bDNTNlVsQWhDbnFxellLeURKNXVVTGVoV3hNbE1nQU1JYllrL1FVaHJSekpNSUQzcFptVXBUaHQrZW5Cd2hHRGxHZ3E5WjBBWDlyNUZld29kb2kxby9Ed01jWUxrTkZ4b05UaHB4ckhNSUIzWEpVdDRjcVJLWHBWSm1vSGk0R1d1bU0xb25UeFJRRmh4VkJWY2VUa0J0eHY4aDZDNGhBdDBvbEVyVTJXaEd1UVNJMmRuaVJWUW1YODVCcUZkd0FrNmxpZlFmeVNKbllYeTNPeGlCQm5DbjJ4L3Y2Z3Jsbi9Md3lOVUJIMVRtVEpTbng1ejFkeDYvUTI2ZGdPZkhKOUVoQ2kwSGpNOFk4UXpzK0ptQXhJbnBkSzg5V0w1aTIyaUdncm01akpoU3FzUFp3U1N6THd0TEV6V2pkd0Z3Q01DTlNKN3NQUU1XQVJjM0k5WUhPalBwb0szcU54TklJS2FuaXhzR3VqeW8yU1Fra2pGanFZeGduangrT1lxVTN3Nm1tTUxCNU1YTEQzYi8yd2NtUWlHcmxHdEJZaEtkN1J5SzRvRFd0dUpIWXZGWWFDMWdZS2kyN2MxRDBEdzJ0YnRuMThTQUZVOERBd2VPaWFCd0hEQlVEc1lzY3dmaWpycllQNnY0MGRHQWFjVXU1UVU4ZDNVTW0xOWtpa2Mvd2tOUTNJNjhlQ2FabEd3UG9Ea3JhSWFFeDFCMUZDR05QbG5Dem81N2QrRVFERWU2MW5nZEVaQVdET1B2SStYRG02WHQxd0xpRXJxdndJcUVSdnBOcVJ3Sncxb2pkdE1jOFExcXZFMEJySXFMMEFBUEVWTHVxaWtNWWcyTHhGVjYvR0dLZnk0MUVBSUUwVS95S1pNU21USlpWVk5ZWTR3NTVOTVZFZzFSYldNZ0l3UUwvZythdnZtMFladllydy9SNEdtVFhZUFRpRWIrNi9TbVJrSFBReERxWU0wWWlRcHhFK1AwUzBRbjFGYlhTTGxCcTZLMlNtcVE1a0l6cEVrVmp4V0swVW4yajVKTkhuckIwa1BPcndoNkhydWxLNVFWd3RKclk4cGtXRllZWXkrU1RRNGpjemJSZWl6aDJzSlJhYXoyTmJqU0c5VnBJdEhVTjhLWG0wN1hCSWpXSktDT2tnMUNRdkZzOGt2RGN5T21JK2ZOTW5tTllBU0lvWXNNYlFlWWZWbzh2bHpHV25HdlRuUXNNaVQ5WURyOVViRjlzVk5kZ1pjZ3R0bE9nVmcvZ29QUFF3ZStUNDBVZXZST2dZVisrb01NdWJRelJxdUdyMGVET1ZIOE9Ga0ZzalBycHVWZ0NONnBlSmNHWll6T2dLMXc1SXF0Q0pDQ1BqSVFKbG4yZXRPRFhNWGUxWWhabFRIK3Azcjk5M0RYZVcyOFRtbzFEeGliVVAvcStQV0ZMRVZLVm9yY2ZhRDVHY3U0WUhzWGR3U0FBREYxSUFFdEJJaHBNcFk1OGdiVTlDdWxKSVZSRFBjd080ZW5RRDE0MXRFcmdoQkMwcG1BQVExNWYxUXRSaU5jWGZJa2NJQkdZKzdTSWtSQ2Jwa3V5clZKTFgrT2ZKVVlsMzBpUU0xSUluSlJ3eUprU2lWaE14WUs2c2dPNGt2cjNuaDd4dGVnYzd0Z090TjJrY1JtaWZ5Z3VPZmdCUURFSnRkTzBKQmUxV0c2NzF4by9vYjZSdjRxVUlGemVNOHhXV1poc0drN09uZ1JGdml3Q1lCbkRpMUZxT2RTdzlQQ2dDMWRCKzJBalVveVpFUXlwVGNnTUNHRUpab3paampUbC9Bc0M1Q2lPeVF0Wk1iQWo0U3g2N1NFaThqMVB3M2IxWFUwdzN1UFFxTlJ5UW9vL3hEc1AwQ3ROL1ZJRXM2MkZYc1kyM1Q5OFNaa0h2Z0RCWjlOdWR3QWNrTnpiODdrUFV3R3lmdlVsMjkyOVZzU01oMGFGV25XMmhHZTRxR3Y5aFJ1S1lKU0VrL3RLRTErSjdHZDhjRWhIU3ExQ0h1TkpYSlp1M0xVSnJDWm0wdlVqdEtJU1BaZkphU1ZDeUhtYmNRZlBEZlQ5aUFCeVR4WVpJUkFvQTJiVGlLRWhsMHhTbXdZWjFTVUpZbzdKQXpRV0dzZFdXbFFoOE1LRGdCZWdKek1tOU5YZWM4eWdCZy8wbkszcm5zcGNGWWc4SUVZN1FIb1JlSTZBaTZ4SjdEZGZRQ0UrYWhNWm9lTVVEWDZxQ1IrU3JjY3F5NCtBUkU5YmpPTFd4aUhEWjdWOENzd0JMQ0pwczRqVHhUSlZ0WVQ1cWp4TVUwSUFXMk5uZlhpOGgwakJhS3BleG9qeXRVYnJxdjBXZ1ZCRWo5QUNmK2ZYblkyQThqT1NvRXdQU0o3ZXpWZUpqaldJUFMxWUxpWGE5UU4xOEpyM1dvRjdjeER3bjI2c1Zpb3RBRFBla2k3NG53RU8xdWEya0xwdmNtaUFjdXhrL2R1T25UWnlMK3VVS0E1T0ZTTWw5MTk0TGE3SzE4TlV3T0hQMWdra3pobnFZN1V1U1ZBK1MyQ0VBdFFLbGw0RVQyUVZ0dkFGM2tJQkZUNDVpUmhpMlppZURLTDBoQ2ZwdzJrOFRsVW0yUVpvRURhT0s2cGxVZ29hb1N1bGxGcjJzSjRpbjlzUUpNa0lWYTYwNFZSelVPWVBlS0RUbXZpSksvSmpFS1ZHWEJDRDZrQU1JQnE0UkNrR2V5OGUyZnk3T3hDTFhJb1l1VW51eVpEamRjZXBDSGJsVHp3d1ovdXo3TDhmbHcyL0JUcTRUWDVYTlBVWUpuMVJ2WFpTa0NMVWVZUEFKbE0yaTFIT0Voak9yZ2RKMlk1S25YQSt4RmZ0TFg5WUNYbHNhcGx1cHR4NkRncEZnUk5KNXNOdkJMVE8zd3BOaXhZQjF1UlVnQ3FFcXBrYkdjTmpFU3FBb1k3b2hReHlydmcvR2JJaW9wRkpWUHVOTWF3eEFLUW5IVktzSVA4cU5BWUFuMW9NTkFMeG1TeEJ1Syt5eW9FcGo1TTZEYW1NMFF3RlNoYkd1bzc1dGpVbHE5ZmNuTlN6Q2tIc3EwQXBITHoycGhkZzRuOUdKc3pDNGVlWjIvR0QrT2tyZUN3Q01VZlY2anRQZVo0T29kTU1LUUxVQ09tUDgvdjRmWXVDR2xOQm5PYjViV3pJdmJQNjJKWjhXbWhCNnJkQ3hPZDUrOC92TjMxNy9XbVRMajRheWJBQ3ptREJtb2w4UWxWcmFOT0VSYVZLYmdsa1V2cXpOT2lLcVhxYm5FVE5iMGRpQVNkeTNZRnJiZUVrTUIyM1lxUERHeFdWU2xaNE82RTdoOG4zZncrM1QyMm5FTUdXcng4UG4wam54OHFBanpnRUdjelFtbm90UzAwaHBBZTRrOXB2bFRYOTZBRjZGUk1hU2tCWFpPa3dlTVFXNXBMYkJRNXJjSmRDanhsYXRRbWFPazZJV0ZxazJUNXV5dzdTajA3cXkvczdXRG9oekZaNHpFS0lzY1ZZQUlEUWRnaEhocExGYnhjSmdML3ZsZm9ya2JOOW5tTDd3WGEwbFlMelBZRzBwb2FvMG5TVzRldlo2Zkh2WEQ1Q1pERTI0ek1SVmtSU1dTenFrL3BVZ25LK1FtMXkrdWZkS1BPK0tGOUVzM3lUcUhPcXM1S1QyZ2hZTVNvZ0NKSnNEcHBFK2k2ek9lbEtrNFM2SlJYUmttTm9Fb0JqUnFOZTRsVHVSSkZHY1pBVmFWQlJySjBWYmRsbjloUUt4Vm9ZNjVGeHhDSzBYUUlReDRCZ0FmZGpvV3FEc205UmxMcjZ5WmVnbWV6VENwTjRnOWNZSXJ3OW1vS0duWXJSelpKYk5IdzBBdURnQjhPTHcwZnZXdVdVd1ptWEk0Q1BCZU1TaVFWS3BXTnhEQktoendsSWlYM3N5Njk4dFVCVlltUytwMzBtQXJSMHFBTEJRemd2TWFMVFdwRzFleEVCTDhoSWxxVEhXYUlpbnZoZ1ZRY2ZnQXplK0h3RFNicTdCd05qOHJCa2trSHhOSmRHeHVlenM3OEdUUDd0WjNkZ0lCQjFxRStJS2J3a1NMVFZ1V1l5MXhpdHJ2WmFOMUd6N1ByeUwzNG5HSXdZYWdDNFNtQzNRaHU5dS9rb2NZZWhqZ1JneWJGUzZFb1pHa0hYa0J3ZXVDL2ZkTElONHFFaGtVRmNzUFJ5UWNWR0hHQTl1RGFTZHNCckcxRWpsZEw4Sjl3NFVwWXFuNTVnRjEyY1RBSUJyRXdEakwzcUM2ZW1vc1ZJSnhDTUwyejZXNGZzbTNsdnZQbytVQ05ldy9Va1NKamM5NlRkeGtvMk1oUDFoeExUNlNwbzA5bXNXdGdJWlJId2QvYTEzZndBSkY2Y3hMN0k5d3o5ZUhUQytEQi9lOFJuWjA5OHZ1ZWtnZFVRTnlaaWg5SVpzOFlBQ0tFTzI5NEhCQVQ3c2t4Znd0dEY1bU80a1BJZGhPeWVKWHk4MHBJN2Zrb2hkanhBOTJYcXh3enZOSXUzUWtsNE5vSklabHJ3Q1RRWHF0YnBEeThhTDh4eFpKc2IzcGU5R0N4REJJRzArUDZPSUlZRWh2cmZqdThHa1ZwODBDUzJRMm8vSUdhdE93TGlaOEI0dU92UXRXellPSUhLVWlZZHRuazlDRWlTY2huM2dLZWdSUENWZjNicVJWa2hnM1pLbHNBWndERlh2R296OEdOYmdvaDNkVUVQdFdzV1dTbzZpcVY2RERpZTd5NXJwUzlWVU5kS0Fhdy9kUU5Bam1yd3QzaXN1ZVcxN01SbkY0Y1ByUmp5QnRySDVLUGE3L1h6SGo5OE5BS0hEZnZTTmtPSlZvYStWUkdTRzEzbkhDei8rQkY3cGJtTzJaSTA0WDBSalgxdnFwUzNpVzFlYWsrUmNKWjFlVzdsWUREQzIzbGM3S3FiaEFSYzVJaTNlTDh3QmExdXgrYTQwblJJVEI5c3FPK25ZR01VZzBiRWNEdWZDazVLQUVFb2VFb3JYZEtjd3BnYlFwRVhTQkRRQ091Z1BvdmJDV2xDb0wwOEFOS0lrZW9DT21XTUIxS0ZmVTFNd3VSeUYzS0RtOTJ2RE9VbWl0RXZqYnE4NUNOelpsdEVtYXFhcU1ETEd5WHhKMkVZeFkxSUJhaUNSQ1FEVDVUN0FKcmxaaS9lMFlBSlFrOWtGUU9KaXQzeUp1Q2JlaVV5dXdSdXZlenVucTJuSlRTYUpRbWVpUEFDSkhkcmlOQkxXV293dVdTYlFUT2hTaVJlYUhjMTBueTE5V0g4dkVsQWFJQW5hOW1COFRXc3VhelVXczRDQ0ZKSG1oUkVNOVdjWkxIcXVYbzltZDlXMldYdk1kZlpNYXk0bGx5cThJc2JhUXBUSUdNUFVUOU9ZSENiTFl2NktZY1JER0VjcnRCaHNwa1d5SkFtR2NBTnFncG5rQWVZR2NCcFA1dGtjQVhqTjNpQUpIRGNodDRDWEdPYUtGUzZKVlU4N01OMThzUGtNNmlLTHRCQ05iV0FFVUNWeVpCek5SbXF4bC9TcE1ZMU5XYkZDeWorVE5QbHBsd2ZxUVpwRmo0cTBsalJwOFpUQkdSbVIzZFVlODVhcjNoRXpQUlFoQktJRVErRXJFNnNiY3lndERQN2gzcS9CaXFvSGxJVUlzZ1k2dmg0UG1xUy90ZzBFMUlsR3FvdVRyZXNvU055MGlnZzZRVXFGaWZmVFVOYzFjQ05HNnRCYTNOWkp3bEVRVGtpUGM1UWNrbnBjYllzbFRDcEpnY213djVvbEFJVFd2YlVFbENSeGpjMHcxcHNpdklmVW5aN2lPRUlXWHB0NmFTYW9QWFpCYmFhQXNWVk9oOEVHakxpckVWQlpUbmtKL1prajJ5dzFxNUFtbFF3dFdsVmFLRSt6TFhmNGRpUURYd3lOTWJGUGlkUUdSUzM4NDhMNVdveHk4WCtBa0Y4YVN3R1pOa0RiUzBROEQwaGdLY29oWkdvRlgvT2pOM0xuL0c3azF0Q2pBbUJRVnphMkpMWVJnMG85anA0OEhLOC8reC9nZDl4T3d3eWhrNUtrTUUrejhlcGNPVFlUTEMyd0pSczVmVWQ0VDFObm0rNGhaZlJxL1RvdVdseE5BSTIvTDRwNklEVTZiS25vRmxlWGJFSkIrQ053ZDZSNndGcmMxdDhsQTE4eGcwMkozUkVOQmpDS1BNODVPYm9NOENWQ3NFcGk1bkZjN0pnUTFwUTgxdU1KeUUvdWoxY3ljS0tDQ3BDSjdDZWtZd2w2ZFpDc1NTMkxaSEliVzFIU0I0YTlJU0ZydVo4UWozckhDSWwwMEVyNzFSR3A4WnVNaHVMbTFpTEcyRytkZXBSQ1FtMVBzUDZlMmhZaUNacDhGTk9jdzh1KzlSY0FERmhuZnloaWZlZWlLemRXU2wvaHljYytEay9mOUV6NC9iY2lzeU5wVWFQcXdWMS9MMnBBTHA2TE5qUFFmdk1kVlRPUXdCaytMRFVzYk05NWNnTFNBdFJaKzBsYnA3bEp3SmMwSHRiYlB1Q1RRSVpEZmhaOVAwRFNZYkZzTmVRaEsyRUJXV0dYQU9vaFNkTkljNk54SmRNWXBIbXVoWUhvS0VyZFdwRmdOeENMT09teUlJSHJLYkJpMHVveUxyWTBPcmhsWU1hSlhTUm40OStMZ0pkR0Viam9OTlEwcDZKTnErTDZnMUpCRGV1cFJHMklwdWRhbUsxM0l4amJEOFMvS2ZDdUZMdDBBOTUzODN2bEM3ZDlXVHEyUzZkZWpERUlCemMzWmxZY1BLd1lPSFh5aGdmOEhZN05qb09iM3krWmRKZ3lVaGFaSUdrZGdEWWZHai9OTkpNUWhWaTYvOFhBaS9aVTdVZ3hUVTZ6UGRvZ2w5WmNONU1YYVpKNjlXWFIrNW9ha1RUUkFwTmg0SVp3ZFdZVVczVndOR21zU3pxOXlIQ1lPMFJDV2dvc3BlWW5aeXFNaldCOXZ4SlVZWGl2V0FtSCttMUdCT0N1ZVlsejBlVDRBd2duL1ZCQU5QUkhra2lMUEs5azVFb0R2SHFUaEp0M1ZKYnFGdG1BRUloSldSUUFPdm1Zd0VQcWdTUzRoZ1ZzMzFCTE1wZ0VWVk8vbG1tZ0FxakNUVXpoZVY5K29Rekt2Z2lFZDh3QWlaY0FRbXNzQ0hKSjNzT0hIdmh1TEowdTZJdStXR1J0OEMyMmYyb2JyelV1dmZNWExCcDNQVi8xZG94ekZWOVpTN0QyZTlMQ3RsN0x0ZzJzN2JZYTZiTWJjWlFLQWVOY2VoZmZFNlpMUXFaYW1QalVudFVpUjlCMElvM1pMR2tzVWxNdkdvaXRPOTFyODN2NGFDV2tUdFVNbDhIYWNRS0FENmRGaGRWSTllNkpSR2dLWHlQSW83SlAyUUl0T3JPV2hOSGVNUlFwcThyTXUwRjRzckVCZzJDTGlhbEx1eXNGVGtMYkNoOG5yTGFOb3UzVHJsTUlRNUE3QWFBMUJzOUtzdkVwWEsrMzhqbGYreU5hWStDOVIzSWowcXZqSlFBa043azRkZWFVbGNmall4ZDhtTDN0ZThpeUgxS1RLZ0plQUVlRUt2eTQ0RDV1elBTdmFyRHZra21oakQzMDB1OXNiRHFQOEhrTnJ4ajVOVFFidnFHL1l0UERLTkVDQnlzMU1GczlDMm9IUUVHNEJPNElRaEVVdzJrNFY4WUZaNjBMU1RDMUl2WjFPeTRYcFhUOFV5Um9KTlp6ME1TMzB6MGtLWjEyYkVwbzkvNHVFMUxUemtpV1Evdm1XUWZMYS9BSmFoc3c1YS9kRWZseGN4Z2pJSWVjSzJiRHJhWlFISkptQ3Rja3B3Z25jY1ZhWUd0UzJsdnhPVVFHZ0MwcDBMSUZrcjFtQks0cWthMDRDdSs1L3QxbXkzVWZZVzV6VkZveEphSGV3UlFFQUdRbVErVkwzUGVJKytLREQvMDNjTmRPcFNvTU13UlNIckk0N0ViRXVXaEpvQmgvYTZUaFlna09vajc4TUk2NlJVckh6OUgwMmRHTFRvWVVHNVdZN01FN1hzMVVOVnUrMWw1RXJ6c0dZNXRtQlFZSTR3bkdvQUJBNllyd2hnaWs1b09UdHBIYXdvcmpXdno5ck9rQ2taU3p1TGdUUWVzUFg5dGZ6YzNVa3MrMGJLRElXN1dUVFNTRmFsb0lqSDhMRGVFTHpBOE90ZENWRktneWNiT25yVHdXY0tHdDJXSVJIbTNBVnNwMUxJUnBRVDFOQUJydk9IMkxNZkRPaVZtK1RwLzkrZWZMdFFkdVJwUnlyZG02NDBYa05wZktWK1lSeHo2Yzd6L3ZYVERiYmtmb3EyemJFeTJMQVNhTHBWRWRvMDJ2YWRsemNmNHNMRXhqQjJMUnZtaXJjbWw5Q05QZnJjOG1HbSs0clNYUzkxSnE5Z2VxSEpWUjVMYmJtdU5BaTBuNmZBQ3paZDhBTm1ucU1LRkpNTlhCOUNnQU5CSEhhU3pKUEpPVWFodmVaOHlpZVc4eVV5bnpnWktLSHhqU1htSWM5RTcxRzBtSkJYYTl0bjhFQ0tlbG9GWWpQb2pWMGczamdOak1zekZJdVNOclJsWUMxUUJzZjE1U1lRR0QwbmkvOGVacU9paE9DRnNEVEJLRkNDWjJObVpteDRobmZ2eTNRVlVKa25uUlhMUkVqMUFoYW0zT1NpdnorSk0yeTViejN3ZHoremFJZDJKb1F3V1dtbWJ4MDhhbzFSSFJrTk5JYXJqZXlFS0JRUTdmUHdqVkFqYnJocmlrajNSTlVzSDEwTkIrTEI0ZW1PNDFxdWVXM2QxczREaVd1bndBQWxmSjBzNVNqR1dqWVdLYjFTV29NTWF5Vk1XaGhVUHhDRkZ0MWhmeHZtb01jUEZHU1dOTUVqZXhsbW52bEZnMDZUVUFyZFBiVE4rSFRhSUExQWpFSUxhbmI5NlVmazF1ZjczSzBocFVNMm5od1Z3RzFaRHhicE1ZQ0ZJdzJxUWpuU1VFT29EejdZS2E4T05pdmw4S1VOUS9iRTFDK2owWnlzbkFEZ2hXSFNLYldvdnZWRC9nTXovemJCcFllSFh0MUxKNlJ6T2czUWdwdWNsWitncVBPZkZSMlBMZ2YxUFpzWU02R01DeUMxUSs4SVNPclRISEh3ZkFSekI0b080YzVSWHdBanFGN3R1TzM1TGpzSDU2aEg3YnpkUzVXVmpKWVZ4R09BUXdwdmU2Q016MGVDM2xHTkszZ21VbGFKZEp4Z21JTnlrUmdJUXJ1V0ZrQlVaTURxOHhqWjBVVlpWNEoxSzVRdll0M0s3SUxXbmlSa284WU5JeW9TUWpaVjJuTXM1bU0yZ2piV0pwZUF6WUE5Z1NwSWpCdFlHUHNZN2JwWXo3SVdnd2tVUXdwcTFVeHpMamh6ZmlPTUMrM3ZucDNxTnF5VHY4d1lFYjI4Q3RONHVKKzIvRDFIb2NOWDQwV0E1aXlrREw0VWdTTGFpdWhoSnF2eWFlK0YwN1FGSmpQSHloR0xweWdHejVNZVk5TjcwZnIvem1xeVd6T1R3ZGt6NXBLVzYwR1kzY1psTDVpbzg1L2pIeThVZC9GTXZuQ0QrM1h6TFRBNXdDa0xqSUxXQWtURE9PUFo1c0xUU3dVTm9EKy9pMis3NEZsei9oSy96dWt5N0QyKy96SnB4aGoxUy9leXUxT0Fock1wZzZ1VGR1d0NDY0l5TVFnVjU3RVBHWWl1YU9rMTNQV0JtTG9CTXNVRlZZMlpzQ0FIaFZpWW5UYmQwRXFGZFhxa0FzNGdtUndkTnRoOXNBZ1pFVUZvMlB0cVYxbkVTSmVWd0NvUFRSK0VpaHVIaTVlZTVocW1KS0lVVkZFNFppWFdlYkZxY3hnaGNCSlEwd1NFaFZKYkpjZnJELys0djBDVU5JREVZTW5Ub3M2NDdMTWVNcmljRUNSR3d6ZVlpM0VIWldxeTZoWG9IV3A3YitxUCtPWDZzaUVDdStLcEd0UHg2WGZPdFZlUGZWSDBadWNqaFh0ZTlnVWFaTWxJYkliUzdPTzNub2tlZnphNC8vRXM3QUVYUzdiMFdXalVDYUtpdHAyVnpTZUxaUzUvZVpyRXUvKzFhODlwNS9KYjl6L0JOUitSS3JSNWZqMmFjOUE5OTV5bGZsMzg3N0Y1d2x4NG5mZGh0MGZsWnRsc01ZMit5THVsb096VTlkTmQrMjM5TnJBOXNWTjBGNGlTL1J6VG9Bd2xxYnVOU3BvemtBbGxVRm4yVkJFOTdoSG1waHdQUTlhV3d0N2pOdHZtUVpKazNRWjVDQWR3ekZtZDBMTTBZMWNqRFJvaUlqTjRkR3ZiV2xEcVc1NmNYUDFjV3l3YlBxY0w0L1Q2Yys5TnhEbW96d2ZNTEtDY3ZQRUJuV2taZFVlaGx0d1hpMldXMVRwUWxwL2RUQVkrM1YxMk9MbFNDVWNGU0VXWE00ZnUvVHorSVZPNitRUE91Zzh0V2lHeVBBbEhLWS9MTE1ablMrTWljc1BSS1hQZmxMZVByR0o0aTc5VHBRbGRia0Vwb2tJcWpET2l3VlA5WXBNdHVCMzNNVG5uUHM3OGtMVC84OVZMNkVOYmw0cWxUZUFlcmxpU2M4QnQ5OHloZTU1V0h2d1QzdHNmRGJicWYycDJGc0R1Tk42N1BCeFdzQk5scXBaYjZBMFl4S042S0FON0p5NlJGaHg0VDFFTVIwdGVRVjNqQzdUV2EwTHlKWmtyUnQ3Ylk0M3QwT1VvUzl3SHFUQ0VLQmI0eTRTSUZaQU1CSnE3Z1lnTnQ4YVNyQ0dOQlFSR3lMVlY1MFF5azJpVUFISkVlaGxuNXhKTWxlVlVMc0tMWXQ3T0VQRDF4UEswS2xCaUtKbEZybEEzalFwclBCY2hpT0NtM3plNHZVYmh4VUtPeGgvYlBJNEUvdHhSaGJ6bXFrYk9MeE9Gb0NOdU53elVwNTJBY2VpV3YyWFN1NXpWRjVSeUpXRWhCU0U2UE5aRXRtY3pyMU1wcDE4SzhQL3lkOTZ6bHZ3ckpkZmZnRE8yR2tRK05NVU1zZUNwWEEvVG1sbFk2Nmc3dDVqOUc3eTVzdmVGMG96aGRMSTRIMXlHMkcwS25LUVZSNTRmR1A0dGVlL2dYNThBWHZ3Vms4V3ZXMmJhcjlHWnBzaEVZNjJpS0JJL2ppUmt2WjBDa0xPemcxTlVVVXVrMTA5SDZyendBQW1uVHFtRWlrTHdQYWZyejNPbFRsdEZwanRKVjkzemJCR2h1d2VUeTFSd3YydXBPVXJoSjFHSUhjYmcxVHVTWGdMcDFvWTBvL2tJRXJZazhMMWxuT3JpMUoyaUJMVVF4dDdjUm9yelF0S0lRZ1RKWnpvZGd2Mi9iZUJDQVM3VVJvRFMvMWVTQjY3S3JqdURSZnBiNHFJQ3J0OEJxYXhFY2daU1V2Qm1UdC9jWHhHcWt6dGpVUkFTYXFtSERBaThuSHNHOXlCQmU4NnlHODhjQU42TmhjdktzSUtGcnRiOE54YnkyTm41bHdUcFh6Rlo1ejFtL3pXOC80S2g2OTdJSFVYVGVMK2dXeFdUZlVra1hwTGJCQVdjclVBdkF2RDNzNzQ0a0pZbzBOZXpWOFdXb2FLVEJBNVoxWVZYbnNpWS9BMTUvNUJYblBCVzh6Wjlsam9kdHZFKzFQdzlvOG5xQVRGenhKcUNRSVdrR1E4SzhrdTExWURyRnhiRjEwcCt2a1RCcWpxYjBnaDdPSEdPUEFiV084WHZxMEllUFRzbGdLb3FIcGJNZzNnaEdSb1VKbTlVY3QvTUhna3ZDMkRUdm1kbEd4TlRRMnBESWRFcEFhMU5UVXdCMEcwRmE5Nlc3YnFoZ2dWQVc5Q2ZuRzlpdlMrK0lMVFdoS1pBMHJYOG1teVNOdzR1Z213WEJPak5qNm5odE5tQ1JTQkJhamNaM09mNjVmcVcwVmtENGpURWs2K2RzWThkN0JqazVpeDVTVjgvNzFmTnh5NEdibVdTNmxjd0Rxdk1WNFE4a0VDbXRrSVRUV1N1bEtPWFpxSS83OXdnOWd5NFBlZytNV1ZuaS9ZeHUwR0lxeFZ2SzhpOHgyeGUvY0ptOSswRC9pcE9YSHdGTWhkUTgrUklNcWpoM2hRSmxRc1FaVzZtQkJQT1hVaTNqWjB6NHJIMzdJTy9GYjdnanhXN2VDckNnU08zY2xOWnRXbjNHVElwci95ZUtxdkN6TDE1anhpWlZJODVIQ2sxcVhZQUszemV3V2RNY042Y1BXWUF0ZFNlTFdERitUM1ZZTElRSHE2amhBa0FIc0srVGEvaDYwcmxpSEF2a3hNTTk1djBOc3RBRnQrTnhJZndUQ3E2N0xpR0tYUUYySmxRUkd2UnVZNWtMZ2xlaDIrTFVkbHd2cUpFaldFNlpxQklHdk5BOC81Z0xCL0R6RnhNNWNUVGF3Z2ErTGJ4QmQvVFFSRW5lNDFIOG4rQ3NENmNab1l0ZkdlVmdiN3dyWWlVbHNud0FlK0o0TDhNTTkxNkNYZFZDNUNpQlR1eUNrVDQySnhkUW9zVHBaSnh4RDY1MWNlTkxqY01WdmY4Tzg3cXkveEVuRkt1cTIzYXkyMzRUcTFoL3cyV2M4bDA4NjRiRW9mQW1idXRQRSsyOTVkZ255WW95SWlFaHVNb1RlMTE1eUkzanNTWS9HMTUvelpYbnJlWCtIcVoxOWNINDJrdjF4UlJyeXU5RmNrVHF4eG9JTDA3alhxaE94Ym1JbHZEb3hGREV3RWtvV0VEWStnTS9kK2lXaTE2TldLaldkdERqRzNRb0xKaFV0alpyMlVVVmJrSVFURVRHelZMTm5NSGRIQUFKYk5vZnptdzY1V1JHQnVMbzBJSHlRODQzdVQxK2V2Q0ZxMkgwcHJwbHNnTVlZRnZWZXBMY1UxeHk4ampmdXV3bldXUEVNL2JPTVNHd1lHWklRSG5IY1F6QldqVkxwMG1vSG16QzFINnQzWHR5Um1qWUVtMG1xSFJjMGhIamd4d0lsbXZxZFNBZzkrWElJTzc0VXQ0eVh1Tjk3SDRTUDMvd1Z5Yk1jcGF2cVpnL3hJZ0NHMnBKUWxxd0FNbU9sWXpNNjlaem85UERDczUrTDd6NzdxL3JSUjc2UGYzelVjM25KbVgvT2YzcmdhNmxlbWRzTU1iOVRhbWMrcW8vd0U2RVppcTFBUUl3WXpZeWxOWmxVM2tFZytwemYrbTErOGVuL2daWDlqQ2dxaWthNnBMWUZKZnlTMWttRFVTdk82ZkZyVDZRZ25QY1RlajRSREllRzB4ckQvWFA3c1dOK3J5RHZncUVmWDl2bWl6eGdzcnMxckhjSzJ3YU9zR2t2WWxNSVJ3d0s3cWlHWmx2QVhCTlhxL1Z4NXZFakdTckVVZ1ZHa0NIVWlNVEcwd2pCNzVaTmlPUUlJQ1FteE5XcW5ZakdRRE0yeDd4YndCZHUrM0k5OXRCNk1HelJ6RmlxS2s5YWN3Sk9YSDZDNk93TXJkajRnWEdiRVdoeUZRVU5MVlB2K1BpYk5OUk55SXhxOXUxaVZSSzNvY0Q3RXJhM1ZHYVdqc3BqdGp3YUgvanh4OUROTytKUnRVUkpaQlRDNllnYTFHV1E1aEdJVUNncXJkQ3pYWG5VOFErV1Z6LzYxZmp6QjE0QzlWNG9vSUhSeUdMVUg5c01ycTUzYko2cEF4aEJNZVUyeUliQ0RlU01kU2ZKNW1NZkE4NU1pNVhVN0xPZXA1VDlrc1pONzBIMkszblU4UStMdXN2RUF2QXdCeG85NE11M2ZnZjdCcnRoN1VqVHFUWHRFY1pCTVFLeFRsMXFHNEFJbThHSWtSd2lGQ05kSXp4WTdjRDgvRDVjZkxGSmI0aGVjRUNnN2h0ZWdZRUhiUmEyVEI1RnFoZkMxVXVRVW9HaXp5MzFFL0Z4SmxnaEpTeEFBRlhCMkxoODRvYlBJdkZzRm1uYmg4OUt0c2ptNHg4TnpFNURhQnRqZG5FS1V2UDdJa3V4WlpEV25sa2FvclJNQmdsaE5LREZwUmw2NTJpeUVYRHRXajd4dzAvRm03L3hadVFtQjhpWTFwL3VTcUJvNXhXRisxSEFHQmprSnFlcUY2Y09sU3RRdWlJdWt6SEozbUNkd0ZRYmFHbTBiQUFuYWZqU1FpVWdNTG5wb3ZDTzF4eTRCZWlNaEN6U1JBN1g5eVcxaVdSZ3dHSUJSNDV2eEVtclRoUVBoWkhrZzRkR2xTbUFlLzIrMjRqTUJ1ODFDZWhrWWtHa1B1VmI2dlZJNEF3L29XSlNJRUtFUG54aDh1WjFOd0RncEd2ckd3NXpHRDFoZTB2L0JneXFQclBRSFJlNUNXTXIyT2JmV29zV1VWQ3Iyd1FPalJJcFNrMkJxSzhnNDFQNDl2YnZZOXYwN1pMWmNJcDNFa2dreGNUT3BFODRaVE1tWkpLKzdNZG9qRFFHZHNKQnN1VVNTWnBLR2V0UVhBUExKcXNacUZQR1ErZkJ4VCtXVURqQVdqR0hiWkRuZmVWUCtEc2ZmYTU0aEh6ZHlydjQxWVJRWXJGVGJkN0h5UXhIU1Joamtaa01lZFpGYmpvU3BoUlNsd1FtV1hjWGw5VEdkSk0vVU1NVElZdlNHTU9aL2lINTlzNHJCQ01qb2Z3MHpVZjZpY0lLbmhReHdOeTBQT0NJKzJIWnlDU2RjekFobGhCMHBGSU1nM24raVI5OVV0RHRCZldiOXZnaVN3RTFzZEw0QTBrQUVYWGNPeGZRZ2pSR01TQk5JWjhIQUd6WlV0OXJtTE5Md3B5ZWVlUDhqVEpkWFM4OUdsRUdkOEZLRThOc0JoQytLU1VPZ00wZURTZlRwTW1vK1R6QzBYWnlITlJEK01DVkh3RUE4ZlEwbERwZng4Q2cwb29icHRiZ3NSc2ZJankwQjlaa3JSNGt6VmZYbDA5R2NCeGNHL2p0dUdoYkVtcHRHMFJ2Q0lDb3hBd0JVQjNVRXRuR28vSFBONzhYOS92bmMzRFYzaDhqdHhrcTc2TDRXbFErTDgzSTRxTXRnUWtUcU1tQXFpWTFyM1hGN1YxN1VKcWthdHljU0NYMUNvVnpvVmZkZjF6M2VSUnVGamJ2Z2ltTHV1MThCSUZCaEU2MklwWEhrMDkvUElEUXJEeGFid1FDbFpxWkRQdm05dUdHL1RjQ295T2kzamRtVlpwMzFzT05kbFJ0V3pmUEt3UU9SRGNLU1VNamZSVitkM29yN25BMVdtUUx6R1dBazBQVnJXR3hoTEFFakxCSnVJek9SbHJnNU9xM2pmL2FtWXVEa2VRd2lHamxJWk5MOGE2clBvaGhWY0dLbFpwOEQyVkh0Ylgyb3ZzK0YvbEN6QjZzamRxV3g1V1VyQkExWDFoblRkL0JMRWcyU3BMUWl3TFNsTHQ4UFFXdUxKR3QyWVRMcTV0eDdqK2ZqdzlkOVdIa05xTVJRNjllQW1LamNhckJOb3lmVTNkK1NGaVFKczB0RVJidGRZMkN6aVQweExoTmVuTUtwZ0pDa1V5QzJuei90UjhqSjNvQzV4ZC9ZRUovbFBqR2RzQ0ZhWjQ4ZVRMdmVmZzlvUEN3WWdNcWd6MGJ1a2dZOEtzM2ZoMjdGcmJEZHNkSStqUW5kd1ozMGp4MXZVazkyRHFMU2JvMk9KQ1p5Y3orY3RyMjdRMFJhL1dyMndBTWI1OHBQNDRGQlhPckVLSDBJckZjb2FWNjd6Q05LVklod0tLUVhUTlFnZ0wxSHRKYnltc1BYWU52YnZzNnJESHF2Qk9tcWhoUXhGbzRYK0hVdzA3bGc0OSt1T2lCWGJSWjA2VysxcGZOZ3JaQWw4YlV0dTRsN2NwR2dndWFEWklpb2UwVXBxRDhDTEZ3eFJCMllnMm1WMHhnODc4L0E4Ly8wQXN3TTV3TEp3LzVTaFErMkxSMXFEd1l2Ykdjb2JIekl2SFRnTFM1V3RzaHhhRnIyNHRSdWljNzBWTnByY1ZWMjYvaDUyNzdvc2pFS3FwTEhSRFF6SDJ5QVFFWWE4QkQwM2oyR2M5Z3ozYmduUXFNU1JYV3FNMG9RRDd5dzA4Q0krUGh6TEc2RnJrUkl0SEdXMnozMVhtSWNRNGRnY3dBWFFFY2FETVIzZWx1TDIvWmYyT01GOWRpZFZGMktnRGsxeS9jWUJjcWp5eTA2R1hIQlBQR2EweE1hT3pjZWovSUhTYzF6UjRhT3pFdXZBRkZsb3poZGQvNngvVDlyUkx4WUhJR2hnM3lWK2UvWERwRGdONVRhaHN2cXRzN2t1S3BqTFF1MjJ5QnNGYS9TVTNYY3FrRjZqWWlFRlpQdk1BWWVGK0k1Qm5NeGlQeHBsdmVMV2YrNDluNDVpM2ZRR1p6R2xoVXJoSlNKWkZGMGFTbzQ5MUVZQ3VrTm1HSW4zRFZyMGlWR01BaWdJYXlTZ0F2Kyt5cjRFZENNZ3BOb3hZV0hWWkNoU0dvODNOY2w2M0QwODk4b2loQW0yVkdZZ1c5eEM0amVaWmg1NkZkL1B5Tlg2WXNteksrcXNMbkJZMjNXTVVtTlZ0VFdtZ2NZVEl3SnprRVJzalFnQXRtQWQ4REFHeTU2Q2RrUkc4SjhZVFZjNnUvYjJmS1d5V1hRQ0ZtQUxMb0JidG80d0dvNlpoMkRwekdMZHVFNGhxdk9ab05UaXRnY2hVK2RkTVhlZmx0MzVITVpsRDZzTWtqVkl5eHJIeUZrOWVjSUU4NTRVblEzZHRoYkI2YjZkZXNlK0lkbzhoTmlqbkZQdFAySUhESDB0TGtPOVJocWp1QUluR0VRc0JvTU1PTWgyckZiTzBSdUxsN1NCN3cza2Z5RHovNkV1eWUyNGM4eXlGS1ZscEJZeWVUbXE5RUtMWUp1YmVneUozMmZBMjhPTFhTYXJZVFZ6WjBRblRlU1o1MStKbnJ2OHpQM3Z3cHNVdlh3dnNLdFhmYUJraGdIbUJNQnQyL0c3OTkrcE5rc2pzTzd5c3gwZUFOT1dJSWJkc0FmT0hIWCthK2FpOU5wNGVtUlZuYzZXRjlHNkJIZnFnMmFGUC9HYzlRdHRDTGljbTVxSlFPNXNiaGx3QUFiOTZ5YUxlM1o0UDQ0R1o3MDAwM0ZUaWtYNGx1ZHNCNVZ5SWh6Vmh3elFaWWJkSHJnYVp3S1AyYm9DREJZVUE4djJTMEkzLzFwZGVtTzJ4czh5RDRKVklkZk1XNUw4RUtYUVkvNkh0Sk50emlRK1VCVXV2MkVjRmVGTFQ3NUFjMXpQcGRTUzh2RWtSSmFyZU5NNGxnUlMyV1hGWFFqRXhpdUc0ZFhuZk4yM0htbTgvRzY3LzJaZzVGbVdjNUFJVFRuUmJqckZaMmlLcWdEZmQ2TVV6aWlreHI2T0cvU2hVakJndmxncnpvb3k4UXJsZ2VHNWtoTVlyTk9LTWxiVVRBWWw1V2RsYkxjOC81ZlpJVWE2eW9CaHRUWW9hZmlTMUozM0g1KzQxTVRCaVVuclhjbFJnSUNIZVJoRWtkNjYvajdVRVlCVk1OQXVuR0NjZ2tONGVxT2J2SC9RQUFzR3J4ckMrZXBvaE9mK1BDNXpCd2dJMjJjUytXOElRTWh5WWRQQzFtTGUyaURWYTM4MklMWGxGT0srSExFbWJwYW41aTYrZjVqVnN2RjJzc1hhUTRFaUt0V0ttY3c4Wmw2L1V4SnowY21Oc3ZGcDMyZ2piL3B1OUxDYk8xTFZvdlMrMENOOC9YcWhzTmRXTVFzNys1S0xNbnZTbmVyNnBTNkdIWEhZNmRTeFV2K3NxZjRNdzMzUWZ2L3Q2bGdESEliQzZrbDhxN1pDMDBSOTNkU2Q4dklvSWd0YUptYlQ5NnFpZ1Z4aGc4NWRMZngvWERXMkZHbGtLZEoycWJrYzNtQndSZUliWUx2M3VYdlBqc0YySDEyRXB4NmdFeHJJTXRJSng2V0dQeHZWdC93Ry90L0NabFlobTh4cllLZ3RZR2xHWSs2MW1VWkE2RjE2Z0lTaVV5WVpDQXBIU3N5TTNWYlF1MzdQa1JESUF0K0FsVmNRQndXY2orNiszWTl5VTVNTnhyZWpZeklLVmpnSjRBRmNJSk9FSDB0dHBJdFBIQXBOcVlWbTNSOUFPQUNBd2htT2pKYTcvOCt2amVRQ2JWMVdvaXFWaEJjdGlRMVd0YWRsNDZYcjdtdldnYW1aak1BVFFxdCtrWXhkWkNOUk9hUE95a3p0SnpZR1BqSnBnWUNDM0Zhd25wZEpFZnNVbXVOVHZOMHovK0RKejNsdnZMWjYvN0Q0allrR0lGZ1hNVjFQdFlUNWdLUis0a2ZvRTRVeG9IckFDOWQ3QmlrSm1NTC9uVXhmam9UUitCUGV4SWVKUUNLN0lvSmg3bVFlQklBMHMvZjRnbkx6dGRuM3Z2WjlOcFJTc1pFVklPMGdsVDlMSGM0bzFmZXBOVW83RmlRaUNnU2wwK1NyYnFXWmg0MWJCWk5ZVkJRVlFLVkJUMFFpSzFXT09zZW5Ddit5Z0F3U3NXbjVKMFp3QUNoTUxNWG91RDlvRC9sdVFDZ0NvWmlLNWhVOU9ncURlb1JvQzEyMGtrYVJHOHBZWWNEYnVVRUlGekZlM2thbnowdWsvZ1U5ZjhoMlEyaDFQSEtJY0FoQmhyNVoxY3UrczJJQnNqdFE3SEpFR1pKQnNXRWFLcHNVQlNDNHRNRnpUaHhOcFd3bUtucGJaMUF0cHFJTGJ4VWorbFVoVURtTjRFN0pISHlHWFY5ZktROXo0R1o3L3AvdnErNzEyS2dTdlF5WEptMWtLOVkrVzlPQjhPUklnQ203RXNnNm9CSEtvTVhCKzlaRGJqWE5YbjB6NzBCM2pOdC8rT2R2MUdlRmNCTnBrTUNEckltT0QrQnZ0WGpCaGcvelRlK09pL2svSHVDQUJwSDlwSVl3d3FkZXhhSzlmc3ZwNGZ2UDRUa0tWcjRTdlhLUE42QXpNc1huSTZrd0JxejZ1bm9Jb2FhTlFFSHJCRHkvMmw1MVV6WHdCQVhMdDQxOTBWQUlGemcyZmF1WG00eFF3VnRFYWhFSFNOd0NMWWdCV1NsSXRpT0lFdnFyTm1nWnNrZ3FTT1U3azdSZWlKYkhRU05zdUM1ZEw0aWNKZzgvRGczTUhBOXZkR3FMNm1TK3FEYWhvU3VqV09RSkxYREVnemxqaWg2WlNtMnZsb0tjYVdrbTZWUXJiK1hmVEN3RnlLZ1pMMFpVa3p1b3oyaUUzOFpubWRQT1dUejhJSnJ6dVZML3JveStTck4zOWJqR1RJYmM3TTJsQ1BINGRoaklHcXIrdGpNbVBaeVhKWXNmem9OWi9CdmQ5OGpyem54KytEV1hlaytNcWo4VXFSQ0hyVG5KV3J5RHBkdUIzYjVJWDNlQ0hPUGZyZVVucEhFeDBPZzlqM0hnQ3BBaGo4emFmK2ptWEhoWDdReVZKSjlsM3pkNXozTk05QXpRTkNnbi9nS01nRlpzeFNsSlFSYTduRDNlUnVuUDdhWGFsZkFIY1dpVkVOTTd0czE2ZUxNOGIzbXlQR1Y3QmZPZVFtUTFlQVBnUVZBU2ZoM1lhTjhWL2pXMUlpUUFCSkhUYVd1ckpLaklpNmdzdk1tTng3NDFsQW5Kd28xcUFNUVpodDB6dFppQmZZSExGUlVtTkRTUVFHSTlBYXJBY0FLUUtEYTZLaEhubUhPMGpxQmwvdHo2czU1Zmg3ZWs0UkduaTI3U05KaXdHb2VnSk96TVF5eU9RS2JDMzZlUDIxLzh3M2ZQZWRPR1hxS0huY2lRL25LVWVkZ3Z0dHVCZkhPeE93V1M1V0RJMnhVSUNWSzdCemRyZDg0b2VmNGI5ZDlYNTgrK0JWZ3FrbHlGWnVGRmNWUk5ZQ1FMTEIwdjFUYUNTSG16MklFM3FiZVBHRC8xaWNkN1JOVFZDVVlJcUtpbzdONWVvZDEvS0RWMzlJekJIcnhGY2xhM3ZkQklZZnJPZXV0Y0h2WUJkNmhpckFTaURMTGNVSUlQRHdNTHg1K0NFQXhIMlI0Ykk2bytBL0FTQkFmQkIyNWlJYzZ1NHVQKzAzNmROOFNxdnBXY0dDQTRZQzVDUXloRHk3dHNHZkNHbVFvYlJUbStkaUFqQTA4RTljR01oeFMwL0dlSGNjcWxvZjN4RE9JL1N3TVBMRlc3OUVsVDR6c2VLQ0c0MUcraUFxQ21rbkxZUmVURFhGRWljcXBpSFhzUlpoeTNscWJyMVdQOGtmU0djRnA0ZnJFZ0ttWlk5RHFxV29BQUpWRjdLaE8xM1l0ZVBpdmVOVmcrMjg2c3EvRmZ6QVlFVjNGVHFrakhiSGNkam9XdW5sWTl4ZkhNVHVoUjJZS1N2TTkvY0lwcWJFcnQ4QXFvZlRJV0JqTVhNQ1gvMk5RZUlIeDcrUTNuVEJEejMvL1pnYVhZTEtWNkgwdGRGMVZBT0JEd04rMmNjdllUVmw2MHFkdXVGbDZsVmQxL1pJRTJaTkdUQ3h4VDRxQllvNHArTVo0S0djTU5ic0dMcnN4d3VYVmdCd0xoU1gzUmxzZHdYQUpqM3J5cm4zK09QR255cmpZbGdDR0JFZ2wvQ0ZBd0U2bHNqaWdOT3lnV0dSRzRrVWJpMkJJdEtXMW1TaUM5TTg5OWk3d3dBbzFXdVc1WFZoRmlNM2RXRC9Ic0RHL2c4ZWdFMDdrbEpMSnhPUmt3cVgyckhpaEp5bThqSmdwNTBRUkcxNXhtemVveWJzQ0ZGcG9qdkowWkowTDJnZUJKQWlIV0ppNHBLS2N3VkF3UFFtWU1hbW9LcmM3eXVCOTRBN2hKdW05ek1VenhoQk55ZTZIV1RMampMcVBYeFZ4dVpta3J6U2VwTHJ6Ukl0bDZ5VFMzWGR6ZnE2Qzk4aEo2NDluazRyNURabmdDZ1RYU09lanJudHlPZXUraHcvZWNPbllJL2FLTDV3U2RiVityWUdlbE56SGI1WGthS0dUV1NrSkRGcGFYcEdxS1IwclRXM0RiNWUzakI5ZFZ6OVZsQzV1ZTZTRmNVV2VCQ3ljUG5CTDVuYit0OHplV2FoSkt3QUl6RTkyNmxnMkxhOTBOaUFRR3BnZllmbmdKUzRxZ3FnQkk1Y2QweTRUVms4aEN6TDRLajQ3cjdyZ2U2azhjNDFraTVVbThXYTViYktiTm1hdFlSc0FTb1Z0cG9XNjlnU25mWEZkandMcURzZ0pPTWI3Y0d5Q1lHRm9HMGl3S1VHU1h4T3FYQkZSYTI4Q0F3azY4RGtZN0FqVTJMSGw0b1pYUUt4WFNPQWNVVkI5WTVOQzdkYWdUWU5tVkxwZ1JOa2VRL1Y5ZGZwLzMzQXhmeTlzNStHeWhWaVRCNnlHa0x5RG1GQ3JaU1JER1ZWOEE4Ly9DZVE1VlBDS3BsUmNTWHFUbWZTYUlsMlE5QllLUU5Hb1ZDRk5aZXBUS2lBWkVic3JJZTVydnhuQUl4K3hWMWVQL0VKbkFzTFFMTnQvYmVZd2dOWm5QaHhFL2hCRldDb2dxcGV0TGFtWXMxWnNBMlM4RG9CNEgySmJqYk9jemZkSnd4RWpBZ2hHcXZZREF6bSszUDgzcTRyaWQ0b1dNWEV0V1RqMVlVNGFYSVMyQ0xpMnZISlppTGI0ZlRXNW1pQnJMNkhPamUwa2JUaFBwdEZTU0l4WlFLbEpJMzJmU2ZiRjNIUllpRjNFMUgwNFgvZWkxS0ZxUzdTdEFqZVpJTW02cXMyQThJbXowZDY0bTYrSGkvK3JSZnl6eDcrY3FsOENXdXpPRWNob3BOT0p2RHFZWTJSbDN6MEwzQnQvMGJZOFNsbzVSQTNVa0poUSt2VWExZHJzMlRIaDVtc1FBdzlNU3FRc1F4d1ZJeGJpNXY2TzBZdTIvMXhFSUxMN3V4OC9OY0F2Q3hJd2U2Lzcva0liaDNjanA0MVVIcGtOa2hCeEpMRHZrcGQvNHIySWtYcEY1bjh4dDZpd0JKd1F5NjFFN0ptZkpYVUFCUkFqSkZVQVhERDNsc3dVRytRWlFqOUxPcUZqd0JMY1U5dDJ2ZjZLQ0dTdlZKVFI2MGQzQnhCMmhLQWJMVk5hOThERXJEYjk5Y2dNdEZMN1E1VnB2VjZxYjhrMGRqcFRlRkhDQmdQV0UrSVQ2ZGRzbjR1QVR2Y24wVGJOWHEvUkQ3U1E3WDFldi9zVTU2RjF6NytOY2I1U294a0VNUmtBNDMyaWdncjU5REpjbjdoUjEvQVc3NytCckhyRGplK3FCSXIwRzd1RnVjRHJQbS9OSmdrQUx3UGRkQ0ZFazZCNVhtWXJoeUtEc0RyaTNjZUFtYnd5bk5TM3ZIUENFQ0FlQ1hzSVdERy9IanVYU1lSR2tKZ2lVVjBMc0lBaXRaaTFwS1FFU0F4bEpPZUEyREZFSE56dVArRysyQ3NOeXBldmNURyttRjJvLzMzcmRzdmgzT0htSWtOKzdBNTVpQkt1WlMzS1l6SGlLVG5JMWpxbmRvOEhxZ0VxY0ZVZTVJSkVDMlF0VlY3K3JkbEJqWVNWWnFsQ2VOSzRHdkZvTFhsUUxDeDUyb0txLzdnVmcrY3BNYlpSR2Jpa2ZVaWxMelhrK3JHNi9DY281OHFiMy9xbTBOTnNSaFlZeEtmVEdNc0RVeU1lQmhzUDdSTG52aStaN05hTTVXV3ArWE0xZHFxU1ZHcjkxMWJpeUhNdGFPZzlJS0pER1lpaDVSS001RmJjOU93WDMxdTd6c2hBQzY1N0M1dHYzVDlad0FFTGdsU01QdlduamR4NjJBZnhveEZoUkJtbWJTQktZY0FDMHE0dUdOcmUwVUNSMVlUd2JYMENWdHRPSkFqVnh3ZWxvYXBycElTajVjVEFOaHpZSThpeXh0anU1NkE5cVFKRjRXZ0NOUU53VFhDcFpsZ3FSZWZTQVJyQzdEcDhUUTFFVmcwVERHMVJWR1VTUDZFelliMElZbWdUUlJHUTR4SHFpUUNjdkhDMWhMVk5IVXVqWDBkTXlVTm9JUXhWa1J5VkRkY3o5ODc5ZGw4NnpQZkprNGRoWUExTnViYkp2NWY2eFF3WXd4Kys1M1B3djdPUEd4dkNWVWowcHVOTFlzZE1ZYjdxOVZ4c3UxQmVBaktLR0JXZGNQWURkVllDTDQvLzE0VXhXMzRZRERqL2pPSS9lY0FCSWd0bTgzQ1h1d3hWOCsvelJnUlpGUjRBaE1XOFVDYjBEbDBJU1ZGdGlVSTYzbXRuMU9CVXhYUkh1OXp6TmtBUUJOQkp4QkFGVllzUFlBdmI3MUNNREpPOVQ2NS90S2tXMFV3S1ZCWFpubWtERzBScFZpeHNNYUc1MUxKWmhwUCsvQ1hpSU8yMzFGdkhDUnFJdDRUdENXQld5cXBTVkZ2QUowMGdiRDFkM3BPR2syOHlKYU5CK01rQ1ZubldxckFLN09zQSswUFlXKytuWC94Z0ZmaEg1L3lab1NZTThYYUxKNENGVDdYaFBPeHhhbERaaXllOWM1bjh6OTJYd2E3ZEozNHFvaUFqd0JmbkVpU1BOeW1RMFVVSHJXSlVrVHFaVEtqakJsRjRZbkp6T2lQKy8zcVAyYitIb1Rnb3Arc2V0TjExelJNKzdwb2k0S1FjZG4xZC8xalI1K3FKNCt0eDM0bE9sRUs3bmVDVElBQmdERUNYU0ptUkVTWnd4QkJxYWVlb0NvNmxjR3BhMDRNSG9JeENHWEJRUTlaYTlrZjluSHp2bXNNbHVkcEZscWVhNXdnamRKSEFkRFRHQXV4T1dBQlArelR6eDhLbW5CMFdjaHoxNmhpVFl4ZkppckdTN0RiSklJaDZkd2tPV09hSWlJcmd5UlZnU2JKSW0yR3hZUjRlREw5TFlKMDNPc2lWWnhBR003U2FZaGZpVWlOeHdiWlBJZmJzNU9yaWlWNC83TS9qUE5PT2w4cVY4SmtscUpHRkNGNkZNa0JFWkplSFRwWkIvL24waitSZjdueVBjaU9PaDZ1R0FCaUczb2xqRUdpd2RnRUZJTEVqK0kwM3A4SDRUUklQeUd4SWd0bjNGbzQ2U0RIOVlOL3dkemM5ZGdTNDJiL3hmVmZTY0F3U3hmQnpBQ0hzbS9PdnNZV2F0QVREdy9CRWt0MHBDbmJuUGFvT3lta1EvNXFUNDVCL1JvRExQUngyb3BUc1hUSmNvUXVtR0hSNDVvUkFINnc4OGVZZGhVbDd5RVNub3QzWTFTSmxnYldaakI1RjFvTzRRL3Noci81Rm80ZFVONW45RzY0MThqSjRJNmQwUDVCWkhrZThncGNTelV2Q3JlaGtVUUJYOUVtU2p1L0JiYWtPcE42clF2RDQ2SXRHbXRTNTJUdGtkZmZsU1J5UW1qNDRycTNpNUkyNzVET3dkMTZFeDYrOUw3bWlqLzVKczQ3Nlh4eHZvTE5PaEJZQ2ZWNEpweE1GUXFYVURHQTcrSlB2MWJlZU5ucm1CMTVuTGlpaUJ1QldEU09Pb2N6anIwZFE0L2pxSk1kcWloa2xuVmdScXdSQjJMU0dudkRjQ2IvNUw1WC83VFNEL2hwSkNBUWtsVjVzUm5JSlcvUFR1bjlQczVlZVNMMkY0ck1DSlphWUpjTFVZN1NBL05DakpsZ0gwb0VZNUE4QWhDR09YUjJoaWNlZTZTTTVCMHRYU0Y1MWduUnVWaithR0Z4N2JhclVGV0hOSmVWdHRKaGpWR2hpQkVyeUN3OW5maTVXV0JoQU13UHNHbkZpWEszdys2RHA5Nzk4WExpaHJ2eHVHV0hBNEI4NXJvdjhFOC9jVEd2Mm5hbHdhckRZTHNqVkszQWxFUWhjWGMzRW02eFRRbHAyV1ZBNDhDZzluWHFCMU5Zc2hhblNmckZWOWRxTjM1TE80U1p6SUFnVGNWbU9aVmUvSzd0c3JTY3dsODk3QTN5blB2L0xnU1F5anZta29kT1g2R0pRb0M2TVZEU2VQWEliUWV2L3NJYjhhclAvRG15STQra0s4dkZkbWd0cWFVMWdoUXhTdmZENWhBYVNveDZFT2dJWkdVZUdzdm5VR05OTHQrZGYrMXdPTnlHaTM0NjZRZjh0QUFFaUZkZVlpRW9PbGRNL3hFUEgvK2tMcy9JQlRXWXlJZ1pUd3hVa0ZsZ2xvS2NRQ1l0WVNHMUNVZ280QTFPMlhSNjJQSmlHa3ZYaU1DRjFienQwRmFnMXpYMGprS0t0UjJvSmRWWDlITUhnZGxTb09DOTFweUJlMnc2RlJlZThraWNjdFFabk9xT0p6Qkk2VXNZRVR6aytQTnh6dEgza3pkODZZMTgvV1Z2eGg2NURWaTVrblprVk9nYzFHdmtOdGtBcWgyQ0UybTMzWTJQeGI4WGVkT29IZkZBa210VFRaUWN0dUN3SkdDbW1ISzArWUs0eWZKY05CUDQvYnRnQjhUdm5Qb1UvT2tGTCtQaHk5YUZCcU1pa3RzczVDVWFROVhZVENrd1Q2SlU3ZGpNL01Xbi9nWi8vcmxMWUE4L0VyN3l5ZVJvb2tKa01EOGFhaWMxSkkrbVNiSnhVN29WZ1ZLQmlvb05QWkZNUkVwNkxzOHlYREYzWGZuSm5hL0R4VEM0NUQ5M1BOcVgvTmN2YVYyWHd1SWllSHZoWVIvVVI2KzhDTE91QW94RjRZVGJLNmxiK2hvQUt6UFdCRzFkTjBXQkZlRFc3YmoySmQvaENldVBwL05lTXB1Y0pRUG5LMlEyMS91ODVlSHk3WmtmQ2liR3hRMkdpcmxEZ29GaXpFN3dmbXZ2b2ZjKzloejd5SlBQNTkwMm5Mem9QbEppS3lESWpCVUk2THhIWmtPMGM5Zk1ibjNqbDkrS2QxLzFBYk9qM0NsWXRneG1kRHhnekx2UXVpdGtGcmJTa21zZ051U3NTYW96Mm8vdDF6WXFMWFdQWXNPSjFoS21OanFnZ0xXV0VBTlBEeHphRC9TSlJ4NXpBVjcra0pmaUh1dFBKd0JVNm95TkJ3MUZnZHpZRHdvaEhheGtnQUZlL3Y0L2s3Kzg3RytSSFhNY1hSRlBvRS9mM01SM0k4VVQ0cG10TEtZbTh6bmwvM2tDSllnRkw1aktZVGFOaEdPNEpxdzNCNnJNLyt2MmgrT2F1VS9qUXRpN3lucjVTZGZQQnNDTFlmQktFdXVYSDVZOVk4VVA5YVRSSmRoZmdWMHgzRmNKOW1tSUZUdFBqQmhnS291N1RnRUlSVVNvRGxQN2lXdGY5aTJzblZxaEhnb0xJNnFBTVNFeVFBVTJ2SFFUZDhrT29KZzB5M3ZMOWZ4Tjk4WkRUbjZJM09lWXM3QnA1Y1phaVhudlJhRUtoYkZaRnVJcEdnSWJMUU9YQkkzem5ya05oeER1bnQ4ckgvejJCK1dmdi8xKy9Hait4OFJFSnBoWVFXdXRDRUN2TGdadDdtaktwRTNWN0ttV0dtN1JNV3l5Y1dxUVNQQm00OXVOeVFBUmVGY1FjL1BBM0JBamVSZVAyblFCbm5mLzUrUHNJKzhoQUZBNnA5WVlzVEdoajB4TUZhSFJjM1BPbzVQbG1LdUdlTjYvUEUvZWZkVzdORHZxZU9QTG1LU1hrbXFESGRxY3d4S2ViSG04YkRNRFVwZGdsQlFNb29nOWNSU21Zd0pWdlRUTCtNRTlXL2l4WFJjbEFmV3pRT3BuQXlBQWJBNEl6eDY0NmpuWXZPYXRtcUhpUURObUZHd3RnUVVFTmw4WnFKb2xrUVlCWUsyRm45M1BCNCtkelUrLzVNUEdPNmNtTTZsWFZBd1RXVjUrKzFYeXJMYzhuZWVjZHA0Kzh1UkgyMU1QUHg3ckpsWUZFWWxRbkVOU0lUREc1cFFvZFZQS1pjaDhTOXhIYllLRlRvdys5R1BJYlNZQVpNRVA4S1VmWDRiM2YzOExQbjdUNTdIZ0Y0QVJJWllzRStSNU9PT09FRldQY09xZUJET2lYYkpXeDVzakVuMEQzUEFxQXhqQ2lJSEF3b3NIU3cvTXp3TDlCWmdxdzNucjc0TUhuZmdBWG5qR28zSGt5bzBBQU9jclVvd2txWmMyRkVGQndHREltblpPOGl5VGJmdHU1K1AvOFltNGZQb0hZdGR0cEM4ZFdqdWxGcHNBa3JSckh3YlU4TFcrTlhSSVNEVG9lMkRnZ1NOSFlGWjBCQTRxSzZ6Z203TnovczI3VGdRSE8xc0d4VTk5L2V3QUJJQkxOMXRjdEFYNWt3LzdySC8waXZOMW4zTVF5ZUFKM0ZxR3NyeTA0eVl6WWdRQVJiSk9EcmZ6ZGo3N3VDZmo3Yi96SnBTdVpDZnIxR01nS1VyaHpIQWFNQWJMZWt2U0dGbTZVbUNDSHJGaXhZaUVRSnlxaUFHRllocEtBd2c5dCtPNUk0amxod2pUWXhDQzhvUktGcHRFQXNETzZWMzQyRFdmd3VVM1hzSFBiZjh5OWhheklCYUEzQXBHbHdDOUxpQUdZakphYThPNzBwRlZvbzFOcUNtbFRFWHB3cGRXUWd3V0JQT3pBTWM1Ym5ONXdPcjc0ZTVIbjRhTFRuOGNqbDJ6Q1hIMFVqbEhZMFJTRDhGUVpKTUNJcEh0WnppTTIwSm9yWldQWHZscFBPOWR6K2VPZkVheWxXdmhoZ1ZnSmVoOXhrWXV3YzVNZGgwYVFwK2hrMnFnVzVvT0ZJSVF3bHp3eE1BTGx1V1FJMGNoRllrbHhtT29tYjV0KzFQeHcrbjMvcXlxTjEzL1BRQmVESU8vZ0M1ZDA5c3cvNHhWVjFlblR5MlJYYW9jTVJiVER0ZzJCR3pvK0FZQk1TbUFGVEc5RHZUVzdmelEwOTZGeC8zV0kxRTV4OHhtSnU1a0dFYUNMM1ErazlKWFlpVjBmN2RpaE5HT0lVUUJGYUdJQnU0c3RQdEY0SHl6TElQM25oQVJhd3hVbFpEUWVFWkVWRlVsaEo1akxZOTZRRUlVSVlJQUJ3YUhaTnYrYmZyWkd6NGp0Ky9aaWU4ZXVnSFhIZnFSbEVvV2NJRDJBUnRUcUNCQjRvWWpMWUNxRW5ncmtGRjJUQzdXT3k0ZFdZVnpEcjgzRHB0Y2hRZWNlQUZPWDNNY1ZpOVpVMCtwVjRWU2FZMHhFS05RRlJNNnNkWUZ4a0lLR1E0V3BDcnlMRVBmVmZ5ekQvMDVYditsTndEclY0anRUc0JYUmJKUm8ycE5xeDM4b3VEMFJFbE5CQnN2UlpRU0Yya2xTTUo1RHhRT3lFWE04UlBCemJKMFptbWV5Yi92MytJK3RQMGluSU1zSmh6OFZOVEx6dzlBQVBGTDNjaVprNXZMcDYrNzFFL2xEck5xa1J2Qi9vTFlWUUkyOXF6TEZKZ3dJcU5kOEthdHZQTFB2b1pUanp3TnBYUG9aQmtBaWlxWkRyRDJVVlNKTVRDa3FGQmpBKzF3Z2dQU0tiS0VxdEtJUVdheituNm1GK1l3TlJiT1JmYStFb2lsTlNieHVUVEJFakkxNnhhRlFxanBEYXVWQlJWZFQyZ0ZZR2JoSU9ZRzAvamhuaHZ3NHdPM3lQekNIbnBYa1NxaG1FNGhOamVFUWtZbmx1R0lsWnR3MXVxVFpLb3p6bTV2RkZNalM5cTZHdDQ1ZUtGWXNRdzVvN0ZabW5vWVkwblFKTWtkYWtVQTUwdDA4dERoL3FzLy9nYi81QU12NWJmMmYxZk1rUnVKQ3FMT0lkb2p5VWhFdEJPU2s5UUEwa2NCbXpLN2ZiUk9zNWhXTnV1QUlRVk9hWTRiRGNtbXBmZXlwbXZzTjJhM2pyejU1ak5taUptSW9wOUo5YWJydnc5QW9BYWhmZENxTitGcDY1N3JoNzVDSlRsQXlvNEJjRUNGdVNWOEpkTEp3QkhsRWYxSmZPZFZYOENxaVdYMHpvZkRDbzJCaWJPc3pianF4VSsxTkY2OUNmVkNpanlyU3pRSkFOc1BiT2Ruci9tSytmYXRWL0FqUC95b25IdjRiL0hsajMyRm5MN2hGQUJnNmF1a3VoTm5Cb0RSWEV4QjdQQjQ3SHBGT29xbmg0RXd5N0pVc3JISTRHdU5RZTdpOS9iRjBsZWlTZ1JuUW1DdGpaUnhZNnBxR0ZTZFcwTUloUlRQMEVNUkFBNzBEK0wvZnVRditRL2ZlSWR3K1JpenlXWGlxbUZxUm02aUQxVGZEOW9nQytSeWpIZ3d4WGtES0kwSkhkRU1nQmxIREZWUWVNajZIdXk2bnJEdnllV1p5aTBEaXcvdWVxaS9jZTR6LzEzVm02NmZENENBNEdKWVhJSVIrN3ZyUDRzSHJyaTM3bkdlVnF4NEVOc0xjTVlEbWNLS0ZUODhnUE9YM1p1Zi8rdFBpbk1WTTVPM1FRZWh4a1I1UTBJbFZnd0tWWkZaaTNaWGdVTUxoM2o5N2h2eGlhcy95OHR2K2o0dTMvVmQ5TFV2NkFreHRkUmcraUE3ZmNFZm52dDgrZU1MWG94bG94TUF3TUpYeUl3VlFXaGYzSFJkQ1ZkZ0ljSVhSNmtyRERrU0pyR3pudFNZdTRmYU9KUFlhaFJKMjRlSFREcFBOLzJldW80bHlsRnFCaEVBUk1NTElGRXJxSHJKc2h3Q2NLSG80ejJYdlJ0LytjWFhZSHV4bTNiRFJvRUhmVlZLM1ZsVlkxcE4yZ2JwNkliMEZjSHpGWGdxU0FNYW9QSkFOeU82VnNRWThtQUo5RDFRZU1qcXJwaU5QYkpRWU5JNnpHdU9mOTMrUFAzKzlKdVRBUHA1QVBUVEV0RS82UXBza2NGYzl5UGJMNnlXZGE3Z3FXUHJ1Rjg5ODh6aThCNXhjejhrS294YVlNRmo5YWFWWWJ1TGlBZG9vU0ZkVjhKSjUxUktwUldFUkxmVENkOFJDblprMjk2dCtOdzFYOFozYnZxMmZ1cVd6OHZ1d1F5UWxjQ1NTZUN3Y2JGMktVQVlkWjUyOVRxVXJzVGZmUDN2K041dmY0QXZ1Ty92bVdlZCt3d3NHNXNLbkpxclFCRVJNWkJ3Z0Y1elZGQk1abXIrRGdWcmliKzFZa3dHbTN5YVZHWEc5RmNDc2FtcHhPQUgwQ2dsTkd5TXdZZVdvSXR3RVlyeDZ0WFRvNXQxWUkzaFhESEg5MzlqQy83K00yK1E2K2R1QXRhdWdKMDYwdmpCTUhCWGtwSnBhZTRzazVNdEtJbWJUR0UxZ1JxaVZFRXZoNHptZ280aDloYkFyQXQ5dlNjem1pTkdnRUxCbmkwbE0xMStZZC9iK1AzcE4wZks1ZWNDWHoyNm4vdUtPNkY3MU5nRCtJeTFuNnVPSGdQM3EwRkhSQ3FTdDg2SjlRTGR1cHZ2ZmZZNzhLU0hQVkdxeXNIbW1WSkRtYUVuMmNueVJjSm94L1FlM0xabnE5bHkxVWZ4Zzl1dTBtL2MvaTN4bVFxNlFpeGRCblM2eUpDQjZxR2hrVnVzRjJoMnZNMXorR0pBN05tTEkwWU9OMDgvNjRsODJyMmVnazJyamlDaTJIRGVCU3lLd0loSmhKNUlxdmFwdGVTZDFXK3l6d0JRQXlzTlE0YTNCdFpJTktyQ0NCTmhiWlNGejFIVlpNOGlEM05BQU5oNWNEZmUvWTMzNFYzZmZwOWNkK2dteGNwSnNTTVRvcVVqbFFLYm1tdEVDNklodGhPRkVqTnBKSld3aHNSZFNuQkE1ajN5bFQyWXlTNllDOTJPUG5UM01FUkhSZzNzeWVOaTFFRGhLNnpLYzN4MDMvZW1Qcmo5L2djdVBtZUFTeTc3YnprZGQ3eCtNUUFFQkp1Ull3dks3cWtUenl4L1ovMDd1YUxyc044WnlheVloUXJZWDhKLzV5Wjg1ZFZmeFgxUE94dWxxNlJuY3czdFA4STExNStUMjNiY3lzL2M4bFY4KzVxdjRiTGJyOENCNnFBZzg4REVtR0o4eXBnOEhHL3E0VWh0UWkzUmpESzFvZDJTTFdJTVRHYnA1MmVCUTRla2h6RSs3TWh6OEpnekh5MzNQZTdlT0h6Wmhub3VuSGR4cFFLRllZMlJSUkNNbEU3NGpUR1VBQkdFYzZaQ1N6RWpRZUFCNFFqTTBBelN4TGUyK3pjQm9URWs0bUxPVjBOODcrYnY0bjNmZUIvLy9jclBZYi91RTZ4YUpuWjBWTFgwWU1VbTljdEtFc3FDK3NTQTFsWnB4M3hqWnJNWUVRNGNVU3JHRDUrQ1hkb1ZNWUwrYlhNb2IxdGc2TFlBMmpNbklWWkVLdTkwVlNmRHQ2WnY4Rys2NWI0dzJCczYwdjM4NEdzRys0dTZMajRud3lXWE9YUE8xUE96SngzK2hxcW5GV2FSR1d2ZzUrWngrQzA5dWViMTM4RjRiNnhXRkZmZDlDTzU5cllmNGVQZi9CUitlT0FHdWJaL0xUQ1JFVjBMVEMySGRETlliMEI2OFNHSnMxR01kWjFYMGpXUlNHMmNoYWF3R2dweG9Pbms4SENDL2Z1QnZzZGtad251Zi9nOWNPR1pqOFZaUjU2RlkxWnRhaytzS0Ftbkx0QkFpKzgyU1JjWWlVVS9qUjhZRDNLSVBKUVNhZ0oxYkZEVFBmVzFaMkUvYnR4MlBkNzd2US95YTlkL0I5ZnV2emEwUWxtNVVteW5TMWFWcUc5OWI4MDlJb1FCVTg0Z2tMaTlsTm9XWHQrV1ZUTUZPaU1kV1hIQ0ttcEh4QlBvM3pLTGhSdG5nRzRJeDJXbkxCRWRzV1JaT2xuUnkrWDYvdTNaRzI2N2Z6RlgzQXorOUlrR1A4MzFpd1VnVUlPd2M5NktQOU1uci9tL2FxWEMwRmlkUG9pekRtN0VPNTc5RnJOenozWis1SnVmbEN0ditwRisrNmJ2Q2pyOC84bzc5MWpMN3FxT2Y5YnZ0L2MrNTl4NzdwMzduRHR6WnpxVnpqQ1VVbXdOWXRJbTJxYWdhSzJwaEV3VlJRMmFFQkw5ZzJpUUJLTUVqU1NTcUg4b2lhRGlBdzBDR21OOFZDRGhWYUNBTGEvT2xBcVZUdHVaenV2T3pIMmVlL2JydC96ajkvdnR2ZStrR0F6TWRLYXM1TTdjODlwM24zMitaejIvYXkyNG9RKzdkNGtNQjJxczd3UnpyaEx0RHNwcDVsMDNEbFFvTndSM1hqV3NxZkpoWU52Wjd4VlE4RmhWQUpzazRoQmNVY0xhZVJnVkRKS2gzcjczQnppNGZJalgzSHczZXhiMjg4S0ZnekpJZXJEVC9JWmpBcDBHejBza210aHV0Q3dBWHp2emRUMS84YXplLzlqSHpGZS8rVVg5MHNyam5GeDl5dnZKd3lFeU1ZWEJpS3NxejlocHVKV3VaYkxRNUtpSlg4TTJ3bzNnazJZK3RGWk9XQnN6dTM5R2Q3OTR0MjV2VlZLbHlNWTMxdGc0dW9MMEUxUlErNUloWnNLS3ExeWxjNm0xUjdjdjZ0K2VlbFYxZXYyaFdBWDd6Z0N5VTc3N0FBUjR3OHRTM3ZOd2FYOTAvdmZsdGN0dnJSTmI2RWFWcHFlM3FaNVlVYjI0TFNRcFRLZHFKbWZBcVRIV3FKdFBjVlBpQzkvV0VDZ2VJWjBRRXFTeHRDUnQ0QmRjTDJuaWdPYWpENyswL0EvL25pTXgxUG5nMXBnRVVxRjJ0Ykp4VVJnWHlyaEFza2xlT250UUZyT2g3cDdkTHorNDcrVzhlUEY2emRLTVpOQmowQitTSlJPYUNtS05RVVdvYXVmeWNteEcyNXZVK1ppNnF1VDR4bWtlT3Zrb1Q1NDVwcXZiVy9yVmMvOXRSdU1WWldJQ01ndTlhVEc5bnJmT3RZcXJLMy9PTnJRMWRNbTRrWE80bzgrNUcyUm8yODlCaUg4M0s1SWEyZnZTSlozYU15WEZadUcwYjJYbGtSWFdqcTZJcENrNkFQdmlvWnFKUk55NHJtVnZZdTJYeHlQZXZYSlB2bmIrNDVjRGZIQzVBQWpDMis2d3ZQMlRWZnBUQysrc1hyZjhaczFkeWRna0ZvTnNsdWpUbTJoaC9IalpvZzdaWjRIRFEySEMrUHBqZFBFNlZwZG9WbHVyNnpPc01mTWZLVlUraGRhOGlMakJzZW5xaDVhZHJHRERGQStiaEVEV2FGMDVvZGdReGlOZkRTZ0ZxaHpxd3BHbWdwblF4UFRJRXVOcFpTcW1yQ3ZOSlJmS0xhVXFCWnNxcHVmTFlobEsyb1BCbEppUVdxRUNWOVlTNnN5UlVCOEIxcWJ5L0dWb2x3RjJLWWQrRzVSZnArSEJLaUpHdmRiTG1WbWM1cnBiOW1rdFVCYWxtSjdWMDU4OXhkcmpGMFV5aTg0a2FtNmU4dTNTaFhPNmxGcHpiR3RrLzNybDFmbXBDeC81YnFSYnZqVlFMcWRFK3RaOXUvOVU3bDc4VldlbDFEVkpWQlRXQ3VIUnNaOTFZdFZQVjZyd1FEbzBxY3dsd3JpbUliVjJ6N1ZMQUpBZGo3VitYNlRmUjU5UU5GRHZDU0VqZ1kyc3ozNFZ3aFlkZy9WVFRlTmtCeEdQZGg5NTR3ZlFhdHZTcUQ0UGFNVDZkZmNOeFQ1Uy9CMTFYVVhkUk95VmI3NW5Fdk4ydEZyUG4ydlFkdHBxdnpnRndvVUgvSElZbjFaZno3RWk3SC9KWGwyNGZvSFJ4amF1YjZpMkszbm1JMCt5ZldGYkpUV2lNeG5tbG1uLytsSnJkcWVKUExhOXdkK2YrdW42bTJzZnU1emc4Mi95OGtwTVZGZnBYZk4vcVBmdCtYVTNJYzZ0T0dFeUVTNFVjR3pMbjBVYWtoU1Y4NU9XRHZUaHVnRlV0VzkyRnVPZm9EdDhRQ0JRVTZTVGZoQUNRQnVOb0hFcGh0Y3lCTDZmdGtCb1hrY0xLQmVhMHh2Q1prQ0tkNythL2cxcHZWSGZYeFpuOWtGd0RVU2Jjd0gvWmFNRC9zZ1hWSTJOWGhxZTN2WlhhM2lyWFJQY21sMS9PUlIwdTFhMkN6TzNiNWI5Tis5VmsxanlVVTQyM1pQVkoxYjF4TWVmb3NZcHpncExHZmFXYVNGM2dDdVp5eEx6eU5hcXZ2L3N2ZFhUcXc5Y2J2REZTMzY1UmZnZ2h2dW9renNYMytMdW5YK0hXK3daenBVMUEyTXBIUnpiVURZZDlCT2ZBRldFc1lPWlZEazRFQ2FOa3F0U3E0bGFwakc1alM2SUpqZ0F0REhmbldBbDBCSjk2S2llQW1HazFaWmV3aWZlMHJuOHZTMStRbEN0UVlORlVNYURCS0NHZkZ2ME9kSDJkdE5sRjgzdGpsNFIzVEU0cVltc3d6R2NobXBIK0paNTByVm9VY05HcFJQVGZaWnYyc3YwNGxDS3JWeGRncVpwSWljLzh6UXJSMWVFcVJSS1ZUa3dnVDA4S2VRMXpsREpYSkxZLzFvN2w3enJ4RStNeXZMaEt3RysrSzZ1aERRZ3RIZk5IZEZYTFB5TjNqQXgwSld5UXZCRmhjZTJsYk9GMExjaHloTWhyLzF3OUJmMFlYL1AwL3kzMVlQVW1JNHAxZzd6dURHSHJZOFVOYUpEL1pTc21IdDBuZmhVT2lZUThNbVZhUExZR2N3Mng0OFJhVFMwVFdLb0lhZ2lzdU9sVWNOMjZmelJENFZZTnZQdkoxWXNHbjlYVzRZeVh1ZHBxYkJWU0grUTZkTEJCV2FXWjhTVk5hNVdzbUdtRzgrc2MrS0JFekplR3lzVEtWSW85dnVIc0s4SDYwNjBMeFc3a2tRL3QvNlY1RS8rNStjS2VQUktnUSt1SEFDOXZJMkV0MU1sUzRNZnNyKzA5MzN1dHNuRDFkbXFvakNXMU1DVFk5R254LzZEVFlKR3FKMHlkcEFaWVgvUFh6Z3IrTUZJalFNWFRIREhYMVRDUkM1b3pHWHpTWnRvSm1sNk5naUhJNEs2dFpKRWE5NDQvRUpUdUd2bUZVclQwMEhrZVhWaW9PYThISXB4c21PS2EvUm5mV05VSjZFYyswZUlKVFRCK2JVcVdpaHNsNUlPckM1Y044dmM4b3dmVXBWWFpKT3BhRjdydVMrY2tRdVByU2o5MUdkd0JvaTVkVnJOZEFLYnRXUGVPaTAwbFU5ZCtPZnE3MDcrQXNMV2Qwb3UrUC9LbFFVZ05JSEpFQmJLWDl2M1YvVnRzL2ZVaFNsMXJUYlNzNGF0VXZRYjI1NEsxRGVoZE9TalJiWlY2QU12NkN2TGZjOG0ySFp0aUtJeENwYm9qVVVpZ1BPa1ROTW1yN3VtRUdMVkxOem9nRnVERHhnMVVGTUk2WVNuUXB6YzM5VzJoTDhTQU5SeENZaGZGbW0xZUhRdEFNVDRKZC8raHNSVkZLS0NsazRaK1FGRGN3ZDJNWGRnMm9tS1ZLV0s2VnNWbEl2SHpzdkt3K2ZVcVFxRFZNbHJaRzlQekUyK0FPQUtWNXVGTkRIblN0eEhMLzVSL1orbmZzUHp2YjY3U2VadlI2NDhBTUhYamo5RmhZSzlkKzg3dVd2dXpicVVvbWZMU2hQanQwaWRIQ3RQNVpEWFFpYWdPRVFNcGNMSStSNk9mVDFZNmlrOVVRb1ZDdGZHeEJMTnNmaklOWFoyR2NSM3ZMbHV0YURyQTBMbklNM05KdmlJdjNSOFVJaGQ3M1FDbTQ3LzJHalRBUDVtcjF2M3AyMUtqK25QMEpTTzRIT2pvd3FEMFpubG9jenMyVVhhczVSbHJja2dFWVBSdGE5ZllPV0xaeW0zU21FNmd6RktpdGliSmpGTFBkWHRHc21rMW1tYjZOR3REZk9walRlVkQ1NTViOGRXZEh5TUt5UFBEUUM5bUdpVXpNMHpQNi8zenYyeGVjbjBZcjFaVjJ3N0t4TWl1dVdVNHlNNGxYdTExamRSMHdpVitvbjltWEhzN1FuTEdReU1iNW91dEUzUFJQK3B0YThRaS9UTnN4VFB4NDZwRnVLZE1aaG96N294dkowV09ZME9KRVRlZkl0VHBhRWhlQ0MzNlJOb1QwblZSOTB4dWQ1MHBpbVVmbmo1OU54UXB4YUhwUDJRMzBsRXBZTE40K3R5NGRpS0ZxczVUS1lJSXBvNzdIVjlOWWNuUkszZ3hzN0p2RlZUWSt1UG5uL0l2Zi9zcjBENTFXQ1JkbllyWDBGNUxnSG8vMzRJVG9Ecis2OVorclBxN3QwL1h2VXRacldxUUt6ckcrRkNDWStQWUtXRVZDRTFrYXdDcFNwRnVIYnpxWENnQnpPQnJPb0haOVA0YzlDSk9MWEZXQVJkcDM5blIxRGp6NVRnNTBVS1lldC9hb3V1Sm1od1FSWHU0TDF3U2U5eDUzNi9lVlJDUzJpOFQxQWxHeVFNRnlmSkpzTDdLbXQxNXd2R1QyN0s1b2xOZFZ1bEI1NFYwYkhDbk1VY25zRE9wcXFGcTJzanRabEpldkxFSnRWbkx2d0I5MS80WFlRUlAzTGxnbzF2SmM4MUFMMGN3ZktQbmhDZTNMbjRtN3hpMTIvTEM2ZUc5WVp6T3FyUTFBb0p5TGxDOVBnMlhDejlmT3JFTklRQW5QcUpyYVdENlFTVytyQ1V3bVNZTUZyaUg0TXdnRnVsMFU3T0V3Y2FQN0NOVURzK0g1Y21ybmNHSWE0RG5KaDdqRWh1QUJrZmE5M0gxbDhOZ1JIUkZ4UklqR1M3TW5yVFBaSWE2ck5qeXBOYldwelpwdDRvQld0Z012RlB6Z1VtUmVYZ0FOblRFNmtWS1YzRjBQcDViWis3K0EzekwydHZ6TStzZlN6NGUvN2Y1MWl1RGdCNmFVeHlOcFVkZHEvYjgzWno0L0JucTRVTXQxbVZqTlZJMzFnMXdFcXBQQmswb3VKWkhKSGVIQlBaWldpNm1VdGd1YThzWkdHd1pqRGYzZVdMQ3FGcUVmMDFmeWF0enhjZ0o1M2IwcjdZbDhiaWEwTGs2dHJVVGpTelRWVGJhVHU3ZEo5eFlwUytJSW1JVktKMnZSWTlsMU9mSGFPakdpeCtXSHhxZmZkaHJyRExpcjFob0N4bWZuUmdYanQ2eHNtc1RUaStqWHgrL1MrcWZ6MzlXOERaNzZTQjZITEkxUVJBTDUyaWQvYnltWHZLVjg3K25qazBlYXRMRWhoVmxkWnF5SXhmZHJGYW9rL2x5cW5jai9SSWpKQTBycDZHNVNsK2ZKd1ZaVGFCaFZTWXpaVEowS2diOVVDYy9oVC9sdzRnZDRBcDRLL3gxU1NtU3RpeDdDYm1FR013RTgydmdsK0VHSTVoSkt4RVU5OFVOSEt3WGdxckpheFdmZ3lhRmVoWjczcUkrdEhJUlkzc1N0VWNHaUxMbWVCVWRWUTdseGsxUTV2SXVFSy90UEVWN2w5OVMzMWkvY01JWE9rVXk3Y2pWeDhBdlJnK2VFUzQ3ME0xa0NRL052ZEd1WFg2cmR5eWEyOHBCbGtyS3luRmFFK01Xb0dSZ3hOak9EMkdyU3FZVXhQWFFuaUpRSXpiZkZMeEczMTJwVENiK2liNmdmVjdicHRBZ1diNXN2Kzk0eDhhN2VnUWphYTdFNEIwbjBzQWJGU2N6aDlyMjhIWXdWYUZyRGwwclZUeWNMeEVmUFNmaHVVeGhYb3dwcUxNcDhpQmdjaENpaVE0SFR0SFlweE1Ta0xoakI3ZFBNa1hOOTdoUG4zK0w0RThmS21mczBEai81S3JGWUJlam1ENUoyb2NUTUpTY1dUM20vU1dtZGU3UTVOTDFLQWJaVVdPcUJFalBSR3RnUXNGbk02VmM0VW5NMWp4RlJUUEFmVU9veU1FS000dldBbHNHSHJHYy9JbUU1Z3duaFRhTXg2c2lRMWdFcS9kb3IvWURpT2lNYi9xMnVVdGRRaFF4ZzdHcW95Y3NGSDZLUU5sZkk3emZkU1pDSWw0eXFjaWZ2Y2FYc3RPV1pHbERQYjJsS25FOS9YbHJwWU14MVNTVWlyeStQYTU1SkdOZDQzLzQvUzdnQlVNOEpxclQrdDE1ZW9HWUpST2tES0EvZVhybDM5WkQwKy9nZVZzbjhzRU5yVFNzUU5INHFOa0M3bFR6dWZDbVJ3dTFFcGVlNkNsUnJBbWRMbEZDU21RQ3AvNHJsV3BRNlJzTkdoVDhheWR4UHFzV1J4T0ZKazJiU1FMemtud01kdm03N3F4eTM1Z3B5Rk1FT3Q4QkU2MTJiY3JLSU5FbU8vQlVvYk1KLzRjUms2cHRaYWVGWFpacTJXRmVYejd1SDU1ODkzcFI4NThJSWNucmxaeisyeHliUURRaTAvWi9Jd0g0Z1RzR2QrMy94ZmRUZjNYbXJua1ZwbE5xWTNBbXFzWk96OFF1U2Qrc2MzSUtXdVZjcjRRVmtwaDdKUXlqTUsxaHJDVXUvWDdJblVMV2xQcWdSVnVoOGxIanBqQ2FjTG1KaEtPZVd3UkR4d2kyS0p2cU42ODE4NnZQWFBPbjhNd1ZabFBSWGRuTUpVcVdVaENGK3F3MURLMDFtVFdtUE1WN3V6b1VYbHc2NityVDU1NUwzQytBN3lyMHR3K20xeExBSXl5QTRoQW10MDIrRWwzY1BoNkRrMi95aDBjOWpReHNGblhXamtWWDErMUJLdEZwYkJaS1p1VnNGN0RlZzJibFIrdUdTTmlwZFY2MHFiN2RoQVc4UG5lb0Qwald0dVAzY1ZhY2VRZTBvSTQrb1dKOVdaK01vRzVGR1pTR0ZwdmdpdFZLVkMxVHVrWmtWNWliYVh3OUZiTmlmd1Q4dlh4ZThyUHJQd2JNT3FZMm1zR2VGR3VSUUJHRVk1Z29ta0d5TWdPdTN2bWp1akIvdDNzNmQzdWx2dlFBd3BCeHM2NVVlMGIxeXlDUlpyQXdPKzU5WE5RTmlzZnlHdzZQNHd4SnJPYitxNkFob0hza1dmWVJMY1NhTjNCaEJxQjFIY3RrUm5vVys5YlRsZ1lCbDh6N1FDN1ZBVlhDNmoweFdwbWpBRG1iSVdlTGg0eGoyMzl1M3o0d3ZzS2lrZkRGYmptTk42bGNpMERNSW9INGhIWU1adHVLYjNaM0RyN2FuTm84bmEzMjl5bFMvMU1kNlZJQVZxcWt0Zk9yNlpIVURIZXh6UFNhRDBuUGtpcDFRY0NWZTBEaGxqaE1NVFpLc0hnU295Ni9lNWVxNTBBS09RcGJTamJ4VUhmcUNPMnp5WFl1QkpYY3ZVUi9lbmlhK2I0K01INjFQZ2ZPTHIrYWZ3b2VGQU05eUhYTXZDaVBCOEEyQlhESFpoSWRPaklDOHdyRis5ME4wN2V6clQrc0hYbVJiSi9RRDJUb2xVb1lGUUMyd3JPVlNoT3dqaHFhSnA4WXRUaHdkUk53L2cwWG1SQngyR1BYZDVOMk1FbGlsWEJCckNsNHZkTUZ3NWRLV0Z6dk1WSzlRQmZLeDdTQjBjZm9OeDZBdGlLZnphVXpxSXhmMTdJOHcyQVVZUzNJWHdDd3lmOEVJck9ZNU1aTE1zZE02L0taNUliNWVEa0M1TmVjcHRhTTlENU5HRSt4VmpqQjROV0VUOCtzS1YybmxlbklXa2NHUTJDcUFucGFTdk5ma0VoYU1EQXBoYm5Veis2bGlNWHEwSnk5emdYOHkrNEU4VkZzMXJlWDM5Ky9SandUT2RkZU5EdFJwOFAydTdaNVBrS3dFdkZjQVJoOW1XR1AzKzRmSmFQY1JHd3lkMkxONURYcjlDNVpJb0RneGxTZTZNNGJsRFJIcGxKTk1PcXNRWWppZlFraStPS3BGUzhPYVdpcUNzWkE2VnpPdFlTMFhXRGVZcThmb0puOHJPeVdxMnJzNSt0SHpqejZQV3craVNNbTdQd1JSZmhUbXpRZERFa2V0N0s5d29BdStKekluZGdlTkhMaEhjL1hHRWFZM21wR0dBS3NFTXdtNUQyOTh4azlmY2w4NUt4cHhiTnNCYWJTMTJQYXlmcXpsZW5pdFhlaFRMUDgzd01GT0ZuazB1QkZLLzg3NUJ3Q3VFaWpnODFDWi92R2ZsZkJ1OHJJN0U4L0tFQUFBQUFTVVZPUks1Q1lJST0iIGFsdD0iV2hhdHNBcHAiIGNsYXNzPSJvcHQtaWNvbi1pbWciPjwvZGl2PgogICAgPGRpdiBjbGFzcz0ib3B0LXRleHQiPjxkaXYgY2xhc3M9Im9wdC10aXRsZSI+V2hhdHNBcHA8L2Rpdj48ZGl2IGNsYXNzPSJvcHQtaGFuZGxlIj4rMzcyIDU4NyAzNTQ1NjwvZGl2PjwvZGl2PgogICAgPHNwYW4gY2xhc3M9Im9wdC1hcnJvdyI+4oaSPC9zcGFuPgogIDwvYT4KICA8YSBocmVmPSJodHRwczovL3d3dy5mYWNlYm9vay5jb20vc2hhcmUvMUVMUDZLQzZyVi8/bWliZXh0aWQ9d3dYSWZyIiB0YXJnZXQ9Il9ibGFuayIgY2xhc3M9Im9wdCI+CiAgICA8ZGl2IGNsYXNzPSJvcHQtaWNvbiI+PGltZyBzcmM9ImRhdGE6aW1hZ2UvcG5nO2Jhc2U2NCxpVkJPUncwS0dnb0FBQUFOU1VoRVVnQUFBS0FBQUFDZ0NBWUFBQUNMejJjdEFBQjJvRWxFUVZSNG5PMjlhYUJsMTFFZSt0WGErNXh6aDc0OUQyck50dVJKTWpaWUpnWVRXekkySkF3R1k5eUNnQm1TQUk4a0VFSjRqendnY2F0SklJRVFFdkxDOEVnZVNjRGdvRGFFOFFWaVhyQk13QUdQMkZqeXBOWXN0WHJ1dnNNNVorKzk2bnMvcW1ydGZkcVNMQnZKbHNEYnZ1cDd6N0QzV3JWcVZYMzFWYTIxZ005Y243aytjMzNtK3N6MW1lc3oxMmV1ejF5ZnVUNXpmZWI2elBXWjZ6UFhaNjdQWEorQ1N6N2REWGo2WEJUZ0ZnRnVGK0NFQURjQnVKNDQ5Q2dmUCtyL0hqcnF2MTlIZStFV0FzSW51N1ZQbCtzekNyaHdVWUNiRTI2OFRyRC9ldUs2UThRdElDUVJlQUoxaGhUY0FzSHRSd1VuUGlDNDdYYWFnaDdSSis0aFQ0L3JMN2tDSGs2NEVRazMzYUttYUk5dW1mWmRkMmhiVTEyemYvUHN3MWRRdXpWZ0ZSaU5SOVU0STh1RUZYUEsxUVJJeXduYzZqRGQwa29TYTJ3UXpWYkdhUFRnYUZLZDMzbjg3blAzbkwvdDNDTStSQVI0Z3lhODlaWmtTbmswUDFrOWY2cGNmOGtVY0tCd1AxZ3B1R2h3RGg2OGNlK1ordklYNmRMYWJ0bDE3WldTSmpkMWFYeUpwclZhcXVWVnFVYTdLTlV1MWlzQUtsTVlOdlk3Q0VnQ1VvS1FJQUZCaGlBRFhRdWltNlZhdHRqTkxxRFp2SkE0TzF0WCtCT2V1K2U5UEhmL2FVejRvZm1IL3ZQZGkrMFY0TEFtSExrbEFkQy9pQmJ5TDc0Q0hqcFVBWWVBTjM5TkJuc0RKd0RxYTcvaGMvTHlwUytzZHp6ekpnQXYwclVyZG5PODdUSnlCRlJqeUdnQ1FvQ3VBNmhBRWtodU0zSldGUUdxcEZBRlNSRWtSWUlBQ3Z0U0JSRVZpQUNTQ00walFSSW1BZElZb2drUWdIa0s2V2FRYm5vSzB3ZE8xdTNtM1p3Ky9CdjU3RWZ2Nk81KzB4OEM2UHBHQy9DNlg2NXc5Q2orb2xqSHY2QUs2SmJ1YlQvWURaVnUrZkt2Lzl4MjU1NGIwdXFWcjhqakhaK04xU3V1NUdUZkVzY3JrQ3BCMkFISUxiV2x0am1oN1FUekRHUVZaQkNDQkxnUkVnb2dDbFdEaHlJRUtFZ0pRSUs5bUdoV2tnSkpDbWlDVUNFVTFEVXhIakZWRlVXUWlMcXVwQWJaZ2VnZzA0ZFZObzRmeTF2cjc4WDVZMitwdW8xM3RQZjgzSjhpR2lBQ3ZQd05OVzU3ZWx2R3YwZ0tLTUNoaE1PM0VrZEVBZUJHb1A3ajUzM1hTOXZ0MTM0NUozdStnaXY3bm9tbEhTUElCQmhOa05pQW1sdnRXc0c4RTh5eW9HV0NDakd1c1ZRbjJiNTl4RXYyTHNtZVhTT3NyTlRjdm5PRTFlV2FTMVdTcWtxb2t3Z0JWYVdvQ2xJRnRDM1JLREJ0TXFhTmNuT3J3L3FGRG5uZXluVGU4dlNadVp3OTI4aTB6ZFNjQVlXZ1RzUnl5aGdsSkJFaWpTcFVvNlJ0QTh5M2dHNWpMdTNaQjlMVzJkK296OXp4Mi9PNy90MWJFZGJ4TUJPTzNDeFBSNnY0ZEZkQUFRbmNkRXVGdC8zVExqRGR5dFUzdnpBZmVQRnI4L0psWDhOdEI1K3Rxd2VFV1NDY1VVUTZ6Vmt3YXdSVFRjaEowa2k0WThjRXp6aTRJcGZ2VytiZUF5czR1R2VDYmFzVlJwTks2cW9HRk9peW9zMUV6a0JXODc0S2tLU1FvRURNQ1JPYWdBUVFCRmdsUUVXWUJBSVFtb0g1ck1QV1BNdnhNdzBmUGo2VkV5YzJjZnpzRnRjdnpNR2NnUVRGOGdnWTEweFNrZUNJR0FHc0liUGpTTk5qSDByckR4MU5ENzNqNStZUC8rWmRBTXpTM254end0R2ppaWMwYkgveXJxZXpBZ3B1UEZ6aHRpTWRBTndBak41LzFkZTlPbDMyNG0vb2RqMzdpL1BxMVN1bUF6Tk5ZSnVidHNaV0oyaFZScU1hQi9hdDRybFhiWmNyTGwvbDVmdVhzWDM3V09vS0JJWHp0cE90T2FUdEZFMUx6RHRBbFJRUml5L29pa1JTa2hDRUtFbFJDRVZBUWtoU1NhZ0lJQ1F5UmF6VkVOaWRKdU1rZFYxaE1rb2d3S1pwY1dHancwTVBiK0hoQnk3ZzdoTlRuRHZmQ0RRRFl4S2pwS2xLU3RZVlJwTUV0a2pyOTYxai9kaXYxUGU5OVpmbUQvM21XMHd5Z1JWdmZzb3I0dE5SQVJjVUQ3dXYzVjVmK21WL0EydlAvbmJ1ZmQ1bjYvSkJDRHNWdEUxdW1oRzJja0pMcnE1Vzhzd3J0OHNMbnIyRHo3cDZwK3pZTmlZRU1wc3JOdWNaMDdtaTYwZ0ZJU21KRWtnZ1dnVmJUUUlTZEd6SFRDZ0pLaWxDcUJJUUVFeGljQkFJcFZRQ3lWOWcrUzlFSUNBSnBRQlVDSUJLeUZHZHNEU3BJSFdDdENwbno4MXc1NzBYY05mOVozSDYxSlM1VVdDY2dOVlJscXJxU0ZtU05FSTl2eS96N0IxdjFRZis5R2YxdmwvOERRQXpTQUplOTErZTBvcjQ5RkxBUTRjcTNIcXJRb1RYQXBPN1AvL0h2ek92WGZNUHVlM3lnNUJsU01vNW9VSGU3QVRuc3RTVEN0ZGRzeDJmYy8xMmZOYXoxamhhbXNoc25yRStWZG1hRVcyblVBZ29CQlVra2lnVkFKa0o1RXpKRkdaWExGV0F6REFyU0pqNkFZQXBHZ2lwQktCWVdLSXFVQ1V0OFNFUUM0akVveGIvc1Rza0VaQktKUUdscU1jenkrUEU4YVFXU2NENmVxUDMzWHNCOTl4MVFVNmQzYkJ2TGdta1RobVRGV0UxcXJCMUR1bmNmUjhZcmIvengrYnYvN0gvQW1CbWdkQWIwbE14V0htYUtPRGhoRU8zQ0k1S0JqQ2V2UEQ3L21aMzhDVi9SN2M5NDRXUUJHSFhRalRwNWl6aEFtVDNuaFc4N0VWNzhia3YzTW05ZThiYzJzcHkra0xIcmJsQ0ZVbVJxQlJrcFdRbGxNcU9nS3FJdW5JcEJVcEtwb0FVWkJJRXdReXF5NDF1NHVqS0F5SlZLWUZRZ2tuc093QklXcVFoQU15UG14cDZ0RXl6bWdJbFNZaEFSSktycmxuY3FoS01SOEJrVWhNQzJWeHY1ZTZQbnVXZGQ1MlQyYXdoMWhMU2FKeVpSa1JLbzBxM2dBc2YrdFA2L25mKzJPekQvKzYvQU9odzZOYW5uRFY4Nml2Z2pZZnJjTGZMMTc3KzFkMVZmKzBOdXUrelhweXhqSlNubmFCSmVVcmdYTWJWbDYya1Y3M3NBRC83dWwyUWxIRDZRaWNiV3kxYkJhRVFzMnJLTmlkcFZTVFRYR2xXTUlPaUdjZ2t6ZEtKRUVSbWdwSlF0eDJHN1NDdXB5Qk5ad2lvQ0ZKbGNaRUFkaTlYTlVLTnB3Yk1jaWFTU0NKVWlRU01LYWxaU1lxcm9TdW4yV0FCcUlRSU9LbXJOQmxYbURlSysrNDl4MlAzbjhQRytUa3dFY2lJSFFqRmVHMVM2UWJrNFhmOGlYNzBOOStRVC83ZTcxNHMwMC8zOVJSV3dNTUpoMjhCam9oT0xuLzF0Zm15bC8wUUwvdjhtL1B5NVpEbWJKTkVxN3loQ1hQbHM2OWV4WmQvd1VHNTdqbmJaVDRuSGo0NzUxWnJTRjhWYUxxTXRpTTZGZVNzYUpta3pXTEtsd2tGbUdrMENwVlVnZVJNMEl3UFZKS1FNS3huUWE4bzFiVE85QThVRWdwSkloSU9WaGtHTFhTU0lrS2pCYWtRSklSVEZxWC94K01VQ0NFaVVDSWxJSVViQjFCQnFZQXdDK29hbUV3cU1oSEhIOXJBblI4OUt4dm50aFJMb0ZRQU1hSk1KblhhdWg4NDhaNmZYbnJQRzI3WkJFNFlkWU5QZTJIRVUxRUJCVGQ4VzQxMy9Xd0xvS3BmOU1QZnFmdGU5UDI2NjVwOTBLYXJNSlhjb01KRE16emo2bTA0OU9XWDQ3T2V0UU9iVzVuSHp6U1laUkVGMFdXZzZaUk5odVJPMlpGc3MwaW5RTnRCTWdXcVJLZEVCcW4rdDBJRU9WT1JtTUZFQlpRSkZGTkFOMmxRczM3R2N3c1psbzBVaUpqcGcxcy9JY0FFTU5NMGlCRlJDd1R1d3hOZzZnNExwUTBwVWp5bVNXS2ZUWWdvR2tqaUVia1NkVVV1TFZVQWtqeHczM2tjKzlCSnpydE1MRmNDcUdLOFRXUzhrbkRpangrc0gzamJEN1RIL3NOL01uSGZXZ0UzZjlyNHc2ZVlBbEp3R0lJam91T3J2K0k1M1ZWZitYOXh6d3UvaU5VS0VtWWRSQ3Q5dU1YdTFiRjgvVmRjeWh2L3lqNVpuMlk4ZUxMbHRLTW93WGtMYVRwaW5wVnRwOUowb0NyUVVkRjFRS2VRVGhNNlZYWVVVUnAreTg2VXVPcFFGYUl3dUdhZm9Sa3VtajBqYWF3akZZQkV1R3Q4Q2dqeDc4QThMQ0VRamF5TXYyNEthTUVKeGJnYmlHTkNXRVpQaENJVUpBSEZjYVFWNXlSSlpoU1pRRWxKQ1NVZ3dwWGxTcnFPdU92WUdUNTB6Mm5oVWlVeUZyQkxIU2JiNjFvZkJvNi80OWU3ZC94ZjN3RThjRDl1L1AwYXQ3MGk0OU9BRFo5Q0NuaW9DaVovOHZ4ditmYjIwbGY5cU83K25EVzBzNjZ1dTlSdFpzRzVqSzk2K1FHOC9xc3VoVlkxN25wd2psbXI2Q0NZelJWTkM1bTFHVTBHNWhuc01xVE55Z2dtbW16VVNhc2liVTVRQ3hkQWtsbk5BZEkxTUtzSWhGQUlWQzJzcGZ0VkFFSTFmTWVpVE1nQ2czWXNjcVdIRjRSWXdDMDBnK2pLWnE1ZEpLcTl3Z1ZIYkMxaXNiSkFSRm1aWDBZQ0tRSVJ0VFlsVVFxRUNZUWswKytVS09PbE1kYlBUbm5zb3lmU2JOb0NTeldobVJpdHRESk9Fem4xdnVQVm5mLzlIN1gzdmZIblAxMlI4bE5EQVIwVWI5dDI3YjdtK2QvMkUvbkFpLzlHSHUrRHNPMUV0ZExqTGE2NWFvbmY5WTNQbE9jK1kwMk8zVC9EcWMwTWhWbThhYWVZemNtbXBUUWQwR1JpcmtEWEdZK25TbVFxT2syaUpMb3M2RlNnNk4xcWRteEhzYjh0RUxIU0FrSkVOVVNsTE5TZGtURjl1bGtNdm5udzYrUWZ6V2M2SEhRRFN5SEVtRVo2YkV4L29MdG5mNjQ5VldFWjVvaDFCZ05ISW9uZFJHaWxpeW5aNzh6RWVGd2gxeUluN3oySGgrODdSeVlBRXdHUUdveFhKMm5yUGxUSDMvRkQ3ZnVPL0JDQTZhZmFKWC82RmRETWYxZnYrK0tYOHJPKzRZMzU0RXVmZ1c3V1ZaaW5mSUVKNXpLKzVhc3V3ZXRmZXluT1hLRGVkYnlSV1V0cE9zVzBKV1l0TVcwYzgyVmlsZ1ZkWnltek5nT3RtcEtwdVZ4a1FMcFdxR0pSY0tZQXpGVGE4Q2s4cFBWb1F5a2dVbkNGY0ZORkdGVWpHWUpFVTBwaGthZlpOZE5FQWhhSnFGS1NxYWQ0M1F4RXJZaEJBQ1J6OCtaZlhhOVRna1Q5QTZncUlpS2dFZVdoZEFZTUpHeW0wS1pSQlVBaE1oSmlhYW5tK3ZvYzkzMzRKSnEyaGF5TVNHWWlMU0V0alNvODhNZHZXL3BmLy9wcnQvREJoNXl1K1pRbzRhZFRBUVdIYVhqdkdhLzlhajczMEJ2YjNTOVp3dXhDVzQxUjVRZG5jdG5hUlA3SmQxK0RGenhuRFhjY2EzaHVNMlBhS2pibldXWU5NR3NVczA3UnRFQ2JCVzFXekxPZ0k5QjJNSnlYemFoa1FEc0ZWQ1YxQkRzYUJndUxaTllRSGxUWXZ4WWlHZzNEUUdvVWM1RWtWUVJVMEJXQ0F2T001bDhoVkpTWXhNZ1UwMkJ4TGpBN1ZpeEJMNVc5TzBiaEJ3RVJpY1paUXRuZ25pZGdraXU0M1FNcW9na0VSVWlvcENvQmdLS3VLeEtRa3crZTViblRtMFJkQVVrRmRkMmt5ZG9FSjk5MzcrajJYL3FXK1luZmZNdW5DaGQrcWhTd3Avemo3ME5NT0NwNTlNSy9ld1RYZk1VYjJwVnJzN1FiQktUbS9SZjRGWDkxUC83dTM3eEdtQVYzUDl4aW5qTXZiQ2syWm9wWmsyWFdBZE9HQ0pmYmRZSkdnVTZCVmhWTkJuTVc2YWlNNkRZcnFCQ2FFcnFycFZoV0Y0SnN4Z1NFb3pFeDMrWlpqdmkvRUU0OGkwQ3pGbWRyd1dreGFDVWVRZHlVSXFXYWlsNDI2QitFS0FKQUpuOHpiQzBpYjhmSThzWHM2SXU0eGFlT2lGSmMreUZKQkVSS0NWVWk2Q25EZWlSWVA3K0YwdytlTjQ4L2tneHF4dEx1Y1RyL3dWemY4MS8vN3Z3amIveFpvMnFFZUJLVjhOTmhBWXZsbTd6d0IzK2llOTZyL242V1BWM0tGMFJuVmFyUHovQy92LzV5Zk5rcnI4QkhIbWhsWTVyUkViaXcxWEo5cHBnMkt0TkdNV3RSbEsvSmh1dGF0MzZkS3RvTVpvcGtXcTJVa2NvaXlvU3M1bnFWaU1BUmxvQUwrSzhlbkVRa0d6RW82UUt6VVdGUkJnYVZMR0VnSlhTbUtJZHJuR005QUZZM3FPNkxnY29TZWdEVThvTkdCQnJxY3k4ZkNBQUVVdUY2Q0VuR0EvbmZDbXF5N0lyZFJmeHpVYm96R2d0bTh5eG5IamlIcm1zVW93cklMV1Y1QjFJK1ZhV1AvTGNmYWUvNDhmL1RiSGpwOHBPZ0RKL2FTeHhhTTcza3lNL29GVi81djdFZXp5dE1SM21kc250TCtVUGYreXg1N3JVNzhmNDd0OUJteURRRDAzbkc1anhqYzZhY2RjU3NwY3hhR01iTHhEeWJJdVVNdEpaaVE2dUVLdGhaeEFwNFFZc2lTYXYwOUpvWTBXYkVMa0J4NjJabVIrRmhKaElvUmx3bnVwTjJRcG05NnNISEZyMmVoQzJOZU1Td1dYSmo1Z0cwV2RoRVZNeE04VFU2cGdNZ0loSWN1QkhTbHJVVHp5QURSQzBCSEFBdkVndEcyMWh3YzlsS1VDb2hTR1dWUkJRaVowK3NvOXVhSzBaSTBFeE1kblJKdWxINnlORmY2Rzcva1c5eWcvR2tXTUpQb1FMYTdMd0JVci8zOC83bHorV3J2dXoxMExhdDZxN09wek91M2w3aFI3LzNPazRtSTduNytKeXRTdHFjWlV3Ykd1NmJaVzYxeEt5aDBTeWRSYnh6dFlKbFZTSXIyVklrVTlCbEJTbnNpdkZSWkFYSUpCM05VcGJ1ZTJKTkI2NHpGTVJmY2xWZy83dVE5ditBZDMxcWc5RmRocnQxdGF5SVZORWNvd0N0Q3JyczMyZG1wWWI3TFpBQWE3RjR1SUtJUW1uNGdVQXVuRFhFVFZRU0Fna2lscFAyZERNTk1qaDM2U3ltaUN1eVpvVklraXpnOVB3bTJ2V3BvQlpBV2lKdGE2V2VqS3NQL1QrLzJOM3hFNi9ISVZZNEtrOTRIcmwrSW0vMnFOZmh3OGxYbmRYdmZ1bS9lU012ZTlYTmFPZHR2WVRVUGRUZ3N5NGY0NGUvNTNxdXp3WDNQVENGUXJDKzJYS3pzM0twclFhWXRZcHBCNWwzUU5PUjh3NW8xQ3lndVZuRGZDb0lraGxaalNvREtaUUVKUWtxTTJ2RDlCTEZBdkRDdmg1Zm1WZVVLRXB3WFUyUnF5MDRUM3V6WjJPdGh0OVNFcVNSOFNGTkJ0Z1N1cTZDclE3WVVxQlZwSlM0bkRKcUZXRlVhR2tHRXRFcVpVdVQ2VzRDVUVNd1NzQlNRbG9TVktNS284b1VUenRGMTVtdXBSSWloZGNOcis5UEFDdzZwdlVIemw5UDFsYUVTT2kycGtBYUUzazZvckNWNTM3ZDF5OVhzalU5S3QvMlpDamhwMEFCRHlmY2ZvdmNJRks5OS9OLytoZnlsVjk0TTJaYlhUWFJVWGQvb3krNGNvSi85cjgvSHlmT1pSdy8zd2dKVEp1Y3BvMXkxZ3EyR21MV0F0TUdNbTNJUmlGdFI4elZjcnV0QXAwU1VFaEhJaVBCU0dVRzBSeDBpRlh2Q1pDcFRqQWpNQnJDU0JpTmJOWlBGZEJraEVkNFdxR2x3OVFITjFhSW1EOVdwZ29pSThGODNoRjN0OENGakYyclNlcGx3Zk1QSm43dTgxZGtkUVhjZTBrdHp6d3drbWNkcURHcEJWVUZNSUd0Vy9ZTGMrTEJEZUw0eVk0bmp6ZlNiWFU0ZXo3alBmZlArZEd6a09iTUZLZlBaeW9vNmRJeDZ1VkVaQlVuSXlPS0laSm4rOXdDd3JNOWhYYzByUmNvVUMyUGthbmdmQzZRQkRSYmRhdExqVjd6dGQrNlZHM0xzNlB5ZDU3b2lwb25Wd0VKd1plOFpJU2pNdit6bC83VGY4VXJiL29hekdadFZlYzZuK2h3M2NFSkRuL1A4L1crazYyYzI4Z3k3UlR6dVdMYUtacE9aTjRTMDg1NHZ1bGNPZXNVclFvYmhXZzJuTmZCTUIvVjhya04zQzBTVUpBWjRnTUFxSEVUeU9xL2V4T0ZCQlhLWkxpZUNpQjUrWldHM2FBcnFBZzlZVWNvRklxazFGU0xhQ0xhVXgzd2NNdEx0Z2xlL2NJeHZ1Q3p0OGtyUDNlRnUzYldXRjAxdHU5anBOUkRvUVZJZE1NQ0lkTmZHM09pMmVyNEp4K2U0dDEvdW9sZitjTUxlTzhGUmJVdE1YZGtFaVRqQmdua2dsT0JUTUFMeThURVkvMk4veENTUmpWeXpvS21zYlJLbm82enJEUjY5V3UvZlVSb2UvVG12K2VXOEFuaENaOWNESGpEdDQzd3JwOXRsNTcvOS8vUDl2cHYvT2U1R3pXSjg1RmVVSG5temhxM2ZNOW42ZWx6clp6YjdOQmtjbk9lcGVrZ3MwN1p0RWxtcmJsZVcrQmpoUVdOZ3AzUkxaSXowWm54QXhXbWpJVkdNY0ZuUTBRUVFMSWF0c3ZCN1RKcVE5MENxc1JmZ3l5RXJlN3RhL2lJcEw0RUJLUlVDcW1BN2tSTE9UMVBOMTY1eEcvNjBqVzg1cFhiWk9lMkNoZ29EMGxwc3lXUlJZQ3FTbElHd0N5U015WEo2VVo2bVExTFZGTlZ5UmJlRGNid1RiOS9sbC8zd3crZ3VucFpkS1pxZHQ3QzhEQ0oxZ0NsRVZId3VKb0ExWkozRnVTNC8xYm9iRzVrcXBDUURLVGxMb21NNU1NLzg4L3poLy9qOTBjQzRjK3JJaytlQlR4MHFNTFJuMjJYcm5uZE4rcXp2L0tmVTVibUlwc2ozYURzWHlLKzYxdWZoWWZPdERoem9VR3J4S3hWekZ0dzNsR2FySmcxaW5tWE1NK0FGUmRBMml4bTlkUndYNllZL29PUnlBb2dXNEJvcjRXcEVpOG1NR0lQS3FhZ3ZXNzR1bzRTTjNwMWxNU1lsT1FGVEJmTWo2V2xoUFpjQnp3d2xTOTVWc1YvOUIyWDhNYlAyd2E0cXJhdGNTZDFNcWVkVW1KZFFWQUxSU21TU2xoajkwMmhPU3FWSnFDS2d1c0laNzA0SWlzbEpXbGFzRXJFbVF2ZWVXcVE1WjZRbGtDNVhod0J6eVNyeGYrTTBqRER3NFd1bFVRWmo0VFpKQXBKUUxjMTBtcGJOM3IyMTM1ZjFaMDUwZHoyaW4velJOUVZQamtLZU9QaEdtLyt3VzZ5LzF0ZjFUMzNOZisrVzc2eVMvTnpGVFlwUzdPTWIvK1c2ekdkSno1d2Frc3lCSVo3Rk5PT2FMT2c2U2p6RERSWmpldFRRZE1SamFyVjlCRmk1TEo1bGV4WXpqQ2Y4M3RRMEdoWi80Q1ZtbGdwWGtTekNKN0NLQkd4QW9VaG13RjJSRXI5WUNHVENTSVRrZmFEVzNqQmR2Q0hmMkEvdit4bGF3SUF1YU5rQ090S1pGUUpOTG5xSUlteUYxSG9GR0FtaWdDb2FuWDRUS1ZVY0dDSzZkUERzbkVDVmhXbHJnUzFab0YyZ0V5OElzYXFXVEhRS1hWcWhsYXlReDE0OVNncnM2b3lEN2lTQUpNUjBNSm10d2pRYmxUdDZyNXU5S3pYLy9oeTNuYlA5TFlqLzlVTXpTZS9IUFJKVU1ERENXLzd3VzRIdVd2OXMyNzhHZDMrM0RHbXAxc2txZVZzZzlkLzAvT1E2aEh1Zm1nTExZbW15K2d5T1ZkQjB4S3RLdWVaWnUxeVFwT0JWb2ttS3pvbFhRR3Byb1JaK2t4RmxuQzk5cmVLRlpkQ25WUWpSZnMwbTVmalNkQVR6Z1pLK0Zlanpvd0xSZ2tseDFZQms5KzlpVy8vd2hYOHlEL1lpKzJyTmJyT3FsRlQ3YVN2T1hNUnR6Qk8wSmdSVlkxaVovaWR6U1VqQ1JSRVVtWWtWUDU2S0pJQ2dJQ0pDL1VJVEtxMmNrN2pHZlJ5QjZNZGUrSWJBRE9qTE1lNzZYZU5Da2Z2cjJkMlVGZUNWZ2p0Z0tvU1RNOUt0L1FzeWpWZjhuUGpNKy82UVBQbW94OEdEbi9TVlRUcDQzL2tFN29FTnlKZFJTNXR2ZlNmL0NvUGZ1NDFNanZicGxTTjlOUk12L1RWVjJQdjNpWGMrY0FHcGkxbHMxRnV6UldiRGJBMXAyeTFOTXFsQVFQL3piUHhmbTFHbE5MRGVEeEJTeGdHSk5EUkpyOVpScUl6cXF4UHZ3a2t3N2xpSzl1VElXZG5FS2czQXRxbndzSzhJbFVRdG9ySm4yN0pHLy9CWHZucEg3Z0UyNWNyYVR0RlhTYzE5aTY0RHhPSFZSbVlFZW1OVGdyVTFXdFJST0FKVUNTS2FyenRzOGNxK3RNQTA4VTkxQkVjR0hZVDhPb0hqMEJDWC90RUNZSXR0eFNRVFJjWGdrMDlHRlpCUmFRS3RyMElpWlFTcDZmWjduakJUbjNPMy94VmNHMFBEbDN2dmZ6RXJ5ZFdBWDI1NVBFYi91NGJlT1dyYjhKMGN5WVZLejAzeFl1ZnYwdXVmY1lhN254Z0UvT09XSjlsYnMxVnRocVJyWWFZTmNwWkI1bDFrSGtXbVhYQUxMdnJ6VVNySXEwVkVhQ2xJbHQrRjVsRTUxbU5qb0lzOW50R0ZKc0tGVEJxQmtnYTlFTllTc0F4SHR6YzlMQ3NhQjhCcWN3bEwzMTBqbC8vb1FQNCtsZHY1M1Rhb2FOb1ZZTlpOU0dwUlRDK1BGamp6cVFnOVRHUDJWWHpyTkVHeDV0UjNwcmk3U0xib29wOTYvd3pFbVE1UXFWcE9ROTMyTkhGK0Q3N3ZnOFUwMU4vQU9BWXh1c2drbTMzVUZXQ1ZBbVFnRlFMcCtzTkw3M3Arc216ditaZldlWE00VTlLbDU1QUYzeW93bTFIdXVyYWIzbGRjOWxyL3c5eXRVbDZZYVNiSGZhdWpmbWl6OTdQZXgvWUZHVkMxOWthblZhVm5aSnRCbG9DVFNhN0xOSVJmVjZYVHB2UWFSRUhjaHBjWHA5SUcweG9tQTgwYjJva25nRXNxK01rZS8zcW5URUQ2U1BzWS9aMFdMTEFzdnJBT243OVJ5N0RGNzlraGZOV1pYbTVKc21rQ2xZUm1ncUs0aVN2RklqU0tROEhvcENoRDRCaGpVNFlnRDIveU43aWVWMHNraGZSUklRbEVJSEdDcnVDTGwzWklMMGVNNG9qZk5abDJGcUJtR2dEVncxSGlXVU5JRHlhY3hubCtVZzc2Zkl6dnVxYjZxNTdlM2ZzeVAvOXlaUnhQVkVXTUlHMzZ1cUJGK3pIVmEvNkthNWNYVmZOZWdVVnFWcmlKUzg1eURQcmM1bjZJdkN0TnNQU2JKblR4a3FzdHVhVWFRUE1PM0xXS2VhZHVkN09DdzVheis5bVZXWWxzd282ZGJKWTZTdlhHSndXa0drRUt4WDA4bVdTREJkZG1BZVRyUVdQOUlvUmpZRXpHMUxWZ3ZTQkxiejVIeC9BRjc5a2hVMEhUS29VVm9oVlNsN0tSU2xDdGNCRzdEVWRyS29EazR1OURMZGJvMGo3a3g0WFdIa2ZCWkdSOW11b1lyQ2xKdVZPSWhiUEYrS3k0SXhZeUlLaVdmVEt4OGphaFJJS3ZCZ2k3dW9vTlZrSTVCVWN3bWFXdXFWTFZDNTcrYi9lUHZtY2EvSG1yL21FTGVFVG9ZQ0NRNGNFSXRKYy90ZitneDc0bkgweU85bWlGdEdORnM5NTNsNU1KbFU2ZjZGbG84Uzh6WmkxaWxsSHcza2RaTjRTODQ0Njcyd2JqS1lUTkNwZWJBQnp0eVF6azNTYWtBRmtlQUVDQVpXRUtBeWc1ZDc2NmhhTkpJRDR3a1lVQXhEdTFYTDF0TUxpQ0FrSmdJSzBYS0g3OENiK3hUZHZ4MWU4YWp2bUxXVmNQU0ovYWt2ZWpPaEJnRElyYnZiY0NRQ0x6OUhueXR3RHFoVVRNa1lsSlJ2clVPcHc1VjdhNTdNbm1meUxPWmRRRm50OW9KY0RaKzZZend6bklOYnU4Vzc4TjRJUitDMUZyT1JhYkdrVVVrcVliV2plOWZ6bDJYTys3UDhCV1RrZWZOejg4aE9nZ0ljU2poN05hOC82Nm0rUVovNzFWM00rYlFXbzhpeGp4KzRKTHJ0OEc4NmNuYUhMVERNdm81cDNRTk9xekRObDNvRXo0L3FrVWMvdktyeWl4ZkNlVmJoQWNuQjlFU2dnR2Qvbk1xWEQvL0JDeW5DcmtUSWJ5Q2FHdTRBdzc0NzA2M0dyRVpFZm5PTVZ6eDdqdTc5NUQ1cUdHTlhoQiszanFxQ3FnbEJ4TXlyMGJZamdSZFFwUVZJYXJHY1RpcVRGTVVwOW5CdEJPVkRVeHBiSnhlNHlsS0Z5aGJvbHVLR1VvdjBjL043M0RUNEZFNExZR2Q3SitVWUFndVFlSHNudUwwa2NFOFlIQWJEU3BtdnpaVGU5ZlB5TTEvOTljOEdISHJkZS9Ya1ZVSURydUIzWVBiL3NsZitpclM5WE5KdUpraUJObG1kZHZRdGIwNWJUaHJvNVY4NmJETnZ3UnpIUDVMeFJ6TnVNcGxXMHFtQ21xTEhIVUZYREh3cUlFam1UcXBscVprL0lEREJiNFdYTzRiTk1JOXlWbEdpMFZKVGFDblVXVitTb1VkVlhwUHRZYVVkaFI5VU9rMU56L3VSMzcyT0NzSzV0ZEVVOHFMVVZRb0tVVUtYazBGR0NxNGFJaU5zK0ttbGNzRWc0Mkl0RVNRQ0p3MkM1Vk9LelVIcG1sM3Z5eE40R3ZaemJNVWdQK1FKSWVHRU1vMERSYXMyY2RTbnZJNEl3bjVtUklJOXBYOEprZFl2cFAzbFc1N1JUdWY4TGZtamIrUG5QQlcvVngrdUsvM3dLZU9QaENqaWkweGQ4eC9kMit6Ly9FazR2cU5SVjRyVEJKWmRzdzlKU0padWJyVFNkWXQ0cVpyNkdvK21VODFabDdydFB0VXEwR2RLcHNsTmxtNVdkVzhHT1JLZnFlbWlLR1p2KzBJT1RIdTZZRWhVOFRTWFZhbGxzajRzeU9FYTRDTUtOb1lRd3JyL1ZKSUgzTlBpZXI5d3V6N3Q2U2RvT1NFbWlUaUdKS3hTQVdBc1NMdGp0OGlDVVVLdDJUaHhZT2JrNEp5d1doMENSeExvaGlQVkpyaG9SYXhXN2JsK3NLaXdhc3BoNFFsUEs4bUgycTdCU0Nib1dtOEZGN1M3MzFZakt2Y3hWSXNLbVdjWFpPdk9CdjdJOGY5NFgvVXVJMEYzeHg3MytIQXA0T09HMlcvSjQ3K2MrT3gvNGd1L1VMbVhSUnBqSlVWM0p3UVBidUxIWnNhTjRoWWVsMHd6bmlWakVDN1lFV29WMENtMXBsUzFacmVTdDAyQUViUDVsVzRtSTRROExoak54MFlzdExYSFJlekw2NG90aUhyUzRwN2hrK0VhZXQ3Szd5dmp1YjlnTkVxaFMzQWxEQTlUekgrb1cwUUdWUTBnVHN1K2FxaUFTa3YydEEyM3lab2dIS0tvcFdCdDdUb2xDRWNGeWNjMEFVQ1ZFQldDNFpqZG5qdFdjZGhyMkVtQ0sxcFp1RFhtcENJZWlhaU9lR1lHM0FWd1V1NXhZYVROdjljQ05YejdlOTRyWHVpdXVIbDEvWERZZjd3T1BlaDBHQUtFKzZ6WC9qRHVmdjRKMms2aFN3cnpEL24zYkFJWE1PMFhiWldrNm9zMlVOaXNiVDdHMUNyUktHVmc2eVJuaU5YeWlTb2xnZ3d3UFlzVXBac3g2Z1VZb1VicFVmSmdFV3RLQ3UwT2NaWVlQTk5oK3BCcUw4TUVHMzNqakN2YnVySm5WZ29MNGpQcXVHUWlYVDFLaDBpODdnbEZBYXJ5ZUFrQlNRSVRxUVFpOC9qRGE0ZUd6V2I3a2s4VVlvYjdjTUtHc3BpdHhCK0RMNDZyZTlaWVlxeFRsTC82SVdBYklFb0toYmlZRkdacEE2VEVnWURNaCtUeVdGUHRmOXdyZWJpYWRYRXE1L0F1UEFGakQ0VnVMWDMrMDY1TlR3RU9IS2h5NWhVdVh2dUtsdXV1RzF6SG5MSUtLcldJeVNiSmp4NWpUZVdQN3NHUmhtOGxXeWJaVHRObC9XcFV1RTFrVnVYUFhxaVFOMVJzR3pBWTNWTE50YjZDMmVsd0lUejA1MWd0clFzS1M1N0JLVWloQmxRS2lEQjlLai8zODM0WElsOGdrcFZGKy9aZnVNaUVsSHhDUFZDT3hEd2lZUEdSRnNteUZsUXphRmhxQ2lGcWhWc3dxb29ZZ0Y3VEY0WmtxQk5ubDRCU01FeUtoYjZSdHoxSFdwQUJBMjFKczZUQmpwYjI3UytNSWlyOHc1R3JWR2dXSHNtOUVrUTJkZUhVc296bHdzbGZuRHZCa05DSm5BSkk0bjdiZEpUYytmM3pOMy80T0hCSEZvY2NPU0M1Nms0K3ByZVc2N2xZS2hQbXFtNDdvdGlzRjdhWWw3RE93YzllcUJSa2RZWlpQcFZWS200RXVBNjFYNzNaS1pnVnl0Z0hQR25TTE1udlJVQVpMWlVzL1dCenNIMUQwcGhTTUZyOHNDSUVHanhFZkRKOUhMd01adUNHaXFrQ2V5L2lyMTQ3eDJjOWVSczVHZlpUbkRPZ05GVkNpRkNmNS9RU1FKTTcvR1hGdUM4VzFtTG9vRkloNGRDaDBwNDhrSzlsMWlxNkZld2RLN3RTMkI4NjJ3VUcyWlFqd2hYcDJsVFR4QUN4dzhGN3dlMzBCZU5SQ1JJZ3pWQU52c3hqT0UvYWZ1MWhYQ3AvVVZsbVdOTys3NFh0WFZ2WmRZdHNGUDNwQWN0RWJqMlBsMDZGREZZNklybHp6MTI3U2ZTOStCZHFtQTFneEswZExOVWFUbWpPcjZ6TUNPVnRaV1pzUk9WMTN2NWEvelV5K2NpMndYZ0pWU2lBeFNGc0tTd001WEtMaDh2YW9MVEJRK2RzalNISG5VbnhYc1BvWUtpdWtGdUJDaDFlOWFFWHFGS3NxTUhUYW9ZazBnR1ArMlR5MEVGRGJvc2orVTdnOUVRdEJmTjFUdVJIZFBuWFppbmJxS2tsZENlc3FvYTZUMUNPZ3JnUlZKUmpWU1VaVlFsVUo2MHF3TkVtb2ttQnR6YmRDUURKM0daNlgwdjg5d0JndTNCN1ZNa0xnQVN6cGtXY1BiNFlsUEhET001TGRnWHBGa2pRYm1idGZ1TE85L0RYZkNZQzQ4ZEU5N1NlZWlydnVWZ0tDOXVBWEh0YWx5eXBzYlhTUVNwQVZ5MnNqZGxtdDNJa0NwbVJWSzk2SDF1djNmTmg4RFRZWlpSdkZNQkFrVlVwdVBVQnc0YTFZWGh4Yy9Ydnh1VkF3Umd0RUZndEorcHZISUdRQm9DcC83ZlBYNG4zS2dpa3BnMVRzUVFLb0hod01JcDhldU1PbXpBTHQ1cHJaZGNZdHBsb0FRSTdkUDhmV3VVNU9iU2pQYndMTlBOdFdJNjJTaEtSS1VkV1ZKQklWRTNic0VQenBoMmZBOHNnV0tvbDZmMHFiM2NxbG1NM1dZbEVCazZmbDJQZlBDR2twZlUwVVpDOFdqS0RGVjAwVnNVUHNuaEhMNUs0bVZ5aTdudnYzc0h6RlQrSzJXeDRDOElnVk01K1lBaDY2dGNJUjBjbHp2dkttYnNmMUwyZmJkQ0FySUNOVkNYVXR5SzJ0OTRkQXRNdEpKZEZYUVVMVmRob3dFbGxzSzRxa1VDYUZiZWJudXpWR1FibWpmNDBvbHNiQ0c0OWxzMDVUTVdoZ3ZzZ0ZxU1U0Tk1LTTRyc0hDbzB5TUNJQ2JuWTRzQTE4NWhWakFCUlpLRCtPSmJLbWFGRXlyZGJNd0cwWUdHcXh3Q09Na0VuR2k1d0ZxaHpWU1I1OGNNNmYrWlhUOHE3M1h1RC9PTlppMWtGUXdYS05RWG5rWEc0QWdkV2VKUUdRZ1VrbE9MQUU3YUxpMEhQYkxJSnh2S3h3cFFzUURQakdDb2h0NGtKcEkwV252VW1BMEUvWGlVb0htS1VSTjc2cVJJYU4yV3hEc2VkemQweXVldTEzelQ4by93aUhiazNsQU1kUFdnR3ZPMFFBMUYwdi9pNWR1VFJodnRFaFZUVTBvMTRhUjR4UVN1aXNHMFpiVVlTWllLWVdNNkFWRENPN1NOd2d1ZzJ6eGVBTEFEQW9oWmgxc1QrRnlTekkwVUdXUU1SNXNKS1p3TERVZVdDc0lGYXFtYmNvbjN2WkV2YXQxV3d6VVZjTGZpY3Nzb1EraXh0VlZVV0tFbWN6cUVWQlU0R1BmaS92WFFQSWovN2tnL3kzYno2UGt3Q3hxd2F1cWdVcHVhZjJVRnZFbEtlc09mS1dSQnpoVzd3V0pDZU1vb0dMM0d0NUxYb1NmVGZsSzdJRStzTjJDSWh0YWRPSFFnT1FXUWgxZDhYbGMxbXlWcXozWFAxTmEydHJQN3ArOUd0Tzl3M29yMDhnQ2o2Y2NFUjAyMVV2ZjU3dWZ1NlhzTk1Pa0dSWUs3RktGYnRPTlJQc2JNV2FkaXEwc25tUlRQUE1WckZzdXd6UUF3ekd6clFMM2tEUlp5dENGb1VTWUJucElZb1hpQlhUZTNpc1JXZytVdXJwcGNpUUZHa0R0RzJaTVFOMnJGWXhMUGdZck8zL0tXZ3FDaEJTR3RDUnBwaFFZZGtURUs3MzVnWndmbDNseTc3alh2eVRXOC9qNU5VclVsMjdpbXBIYlJ4UFMzQU82SnpRT1VSbi9tOEwwVG1oVTBKbkNwMERiRWkyOE1SYnpCWHhBQ3U2RitNdTFxdUZVSXJ3d0lSOXZaaDMxYXAyZkJCa0NDa1dsR2dJREV0SHBSSjBzMWEzWFgrZzIvdmxmd09nSnk0V3I4ZXZnTTVzencrODR1L295dFVUZEZ2bTJGU1JLcVBWWTlQdlRsVnkxcFNwVUt1S0kwbXhiVlRDRkVrcC92UzVIRVJ1ZUNzdTlnMVJMbXBUbVVDa05GM1FibmFkdkloWnZ6RG5RdWJ1MW1LUXdpcUlDTHJNZlZlTVlnUW9nby9CTFNaak1WOXFCeVZCb1JLNWV6TjNGQ2UvZldUaHEvRUVIWURYL01ONzhYc2ZWSXl2M3liU0t2TFVJbHdQR2dRcGlWY2tPSjhzL1UrQzJWVnhpMml3dzFubm9iV0tRQ0tVTDRnODhXSlRTYzdqcFdJeGk1eFl2bTd5aWdMWmhkblh5NWZ4ZWJjd2hDQjNWWllWNU4wdi9CYmd1akhlZXN2SGxHbzlYZ1VVSEQya0FOYTZwYXRmdzF3VFZLUHNiWEU5cUprNTZJSk1FaGtlem5vVmxFSnNNMXpYSTErNXl3SGVJSDJKcEhjZ3JLQXY5dTRKTTMrdDVPT0NtbVdVdzdobVIvNFNnM0lqQUhET3AxZy9RemtLQUUwbkw3MW1aTVJaYktrU253cGsxNnQ4U2I0QlZxZG56bGtaL2l5SjBPdWprRHVpcm9RLy9nc1A0NjEvdG83UnRUV2FDNjFQcVlINUtRUHNsanpDY09QZUJoT0NMSVdreGhHRXkramRTTW1ibUdBOXJKZnlkNUhkVURlOHRLaklMZmY5cDdKdklFcEdKeDRIaUhGRW1pRmtZdGUwV0x2bWhlUExydjBpVTlBYkYyRGY0MVBBUTRjU0lGeDkzcXUvcE5wMStSVm9wNTBSbjRud0Fna3ZoNmNYQllqeHhrSlZKbFVtNDRQTkJWbWlMQUlORm4yTUpGbzVSWlVzZllkdk9WR0VpWUVjeXVUVnhiOGxCczFucCsrVzc2NG8xajVnY0JjQTVNRWRsUU9hQWFNWXlTbWdFQ2tEbGdjUkZKbXE5S1ZmWVkyekFxT3g0TDRURFg3NFRXZFJYYnZNYnBwUnJCd0dQK0VTeTJ0bHg1amU0bEU4UHpzVWdtbHlBUXJpR0Zxa2ZMUElFaHhRM1BHc29VeEx6OHhxcXRXRzlaTXk4czJCRmRHTE1QUWJBUEljdW5RUXN1ZUZYd2VBT1B6V0JZL3krQlR3dWxzSklPVWR6LzFiT3RsUDVNWk5PV3lPc0JEbjlMMzAzRURaYTJiWTFBMVlHTG95V1M4aUg4VndXai8yZmQ4V3JKaGZIRVFpWlNNWERmY3FiaDFjQ2RIL1hyUzB0NzVoY1NlMTRjelVpN1RYa1hpc0sySHdsMGdHTHVrb0lUNGFWY3hVTTZodi92L095NFdPS3VPcVJ3b2NLRTdmN1lIUERmZVhvbXB3VUp5ZkhOTzR0YWN0T25VbDg2Z2dxQmtNY1J3ZzJ2ZW8zREU4Q1VMV2ppcUd3K0h0TGNzYmh1TWhpQUlQbjBZcGs5VGxnMSswaWdQNy9TREpjcWZIbzRBSlIwU1hsbkI1WHIzMlpkck00QUFDVUMxQzhnT0diTG1rclFBSExNaWdzbXg4RmxiQi91c20zU25OM3NMWmR5Tmx4cUluZlZXU1NUUEFkRVF1N0c5ZUhDWTRTSjRPdEdpb2d3SGJYSUJWVmNRaXVpamVJdjhlbTFPU2FhSFYxNVF4QXloaVNtd1J0d0RRMzNqN09tWEh5RnpIUWlWT3IrdDl5d1o1N1Q2cXhWQVNyaVFDVk1QWlNxRFFUK2l6UGIzaDhveEg0Wlg2akROY3FSa2RsU0pZQmNBVVd0ZFRZVVBMQ1loWExJSlFJMm02YWViYU0vZmxLNzc0Uyt5bWZYcnU0eXZnb1Z1dG04LzQ1bGQxMjU2OWdtWnVaQnM3QVN3cEg3VjRHblYyTGlnN2ppaUxSUGxVU2MwclFBTXZCaFdWaU9XRFE5eG5EaS93V3p6SVhhbjBzKy9pN0x3Q1pXdUVxR1pTME92bWd5TmljZE1MWm5YZ2w4UTJab3M3eHhDNk9hQ0lSQWpjVTl3K0g5Mzh1TFVYcGdTMFdlWDhlU1VuRll5ejFNR2QvZmNncEFTSXBmTDlGSEFWRHhsTHRMOFA5MTBqVWhGZ0laVmpNcXNNdWhGR1FBYklOdG9VZzlpM1VUeDZLU3Z0UW5uRmtZSS93eWFQdTBJSXVzeGNiV1BldHZ0MUFJaERoNHA2Zlh3RlBQRUJBUVM2Y3ZCTFVlOEUyTGxGRXJYekFvVDAzSGsvdTd4bm5oSWxJVkR4b3VGZWxoZ0NhZ3N1WEpPOVk5R2YzcjZsbnRlS3ZnTEZGWlRKR2dNREtaWXpxYnNxc0UvcXVkQmo4YTUvTnBVekIzdUxPYkNkaGV3WWdwbll3czgrV3pZYXRQSWJXakJ5ejVsVzdyd3dGNHlzbG1MdzdlaXZGUGRXNktmb2YxZzZMQ1ptQ0pTdDhjMWQ5ODJVQVJhSXp3ZXAzQXZPbGY3aUR5S2VaL2RsZ0sxaGNNTENlTG5KanhuWUQ2QXBScUpteVBabmZkNEtjTkFYTHRuY3hXTmZndHVPZER2QW5WeTc4cVZzWndBTE5MTHF6SEJ4NnZYRWFvWXZYSkYzUnlnNjJLTWtBQjB2R21FSnpJWWlwTkxuVUJaNE1XWEJOYjNKRFpkUXJJaDllT0Fla3VjNVVmUklpa3lseUhxd1lnMTlId1l2TVlaUC9MaE1aUFhaWldBQ3RqS1NISDd4NU5uTUMxdTJ5cEZCQlFYUEhaT3N0K1REK3J5K01VWHBRbnlEQjNoc0VkeFh3WWZoaHUxZmo1R0tQL2JaR0FsamIwQzVkNTlNaDgrdFF1VU14R2IzVnBRSmpySVdoZ1FUMms3ejh1VjdlY1VYdmRnYWEyNzQ0eWlnZldqNm5LLzdJbDI1NGlCeW13Rldsc2xGMzFhci9IQzdZdUZaNkEyajNFbkI0bDZMTXh1TTV0RFVRMUJjVWhxNGhqZ1pVQ01DZzBtV0EwVm1DTXJCZHdseHdxMUk3MjRHWXoxUVVxK2hNanV1QlhTVkcvaVltSFVUMjNVVnlkWmJrSU10YmNYU0pNV2s1NFlXVFpaaWdXSFRVS1oxNmNzd00wWi9yMWh1bHhGREVtN3g3RWdsdHdDK2VIclFjcGRSOU5DZUZmbFIrc1JubE1RVS9ReXI2Ui9TZ2J5S25nMzcwMy9mdVYvUkJoanRaYmQ2M1pjREFHNjg3bkZZUVA5UTNuM05GM0Z5Q2FHTld0MUFTQ0ZvajZCRkJtNmozNG5KclprTzBqWUsrSkpKVjBvT1lBZjdYR094cnNiS0FiQmtYeEZXLzd4UTJnajZRZ21wSVJ3dVZOWU5RWTlQQkZmalBpcVhvYi9yVmNJQVZOUmVpZlBFSERpZ1Vtb3kwQkUvY0ZDalhJRUxmbjF3aGNrMVVCM0JnVm1ZdnRObElpLzhidlZaOVA1R00yTnlteEVJUTZCT3VxTGNKTGJzTjFtWjREUVBaSzJtWEo3VDZxUGdrSmowOTBLd0RkR1dES3FJVFBhK0VFQ04yOHhmUFpZQ0NtNzd3UTdBRWxZdS95dnNPZ0UxdVNYeW9MYnFGU2VFWmhNdE5DTGswb3RaeSt3SmU5TkhzUW90WnlVRUx4dndSRnpMQ3ZCeUhGM3k0cDU5SGdSejZPWGlVYWhLVCs2eWo3amRDNGtUUmtFdzliVFhSWmN2ajJUOFh5alFWSmhrMnVaWklrS1dTbXJZMnFsZURzTUdMdHpiWkJPUXBPRGhJb2krelFNQnUxWHNHNXNndmc4UlAvWXh3UU15S0JaWDZyQjRIbXdveEhHK05hSVlHK2t4MW1BZVJIMVR3UmJCQ1dqMi9FMURMTysvWVl5MVoxcGx6T0hIVWtDNzg5S2wxKzlqV240dXRBVmlwMjR6dTFKd1FRRG9LRWd2dE1nQ0dQTkdwcUVBZlpiR2U3UVVGQWhiRlRKc0RIczlKdG5iN3VFc0d4Z25EUHc4TlNaRzJMZUJEeVFLNlVvQ1NCSWFtQ1FTQXZEditOUExHQU5XQUMxVUwzMVdzT3paYTRraURjYlVEZXJnVmtQRFljMHhXWVdGWEdpekR5eTlqeW53YitnSDNDcjVoMjJyTURwZnUwaWRCeWlNL3hWSUVsMGJWc1lNWHU3eEhrdHZTbWt0NGF5bm8rQ0NJZUgxLzZCbWN1bGdQYjcyTlM4RUFCeTYvakVVOE5EUkJBRE56cHRleHZIQk1YTG45SXNHWndjdzk5aWhtS3FvSm8zMzNlb1V0MW5jcnZqbWlQNTdwTjlpQmcwR3FOQ0liZzdFbFQxNFd2cG5qR0dRRXBrVktESUFyRmlJMW91ODNlcWFPRTNnOU5UeWd2MElqRmZNa1Z0RG9hV0Z4T3RqZkFtdUpDVDRiQkVQb0tSc2R4Rmk2MlZveXNVQSsvNjZsdEVQTEJad3h1UmkyLzFGcHdTUUZLclJUN0NoMlpUd09DamFYNEt4dUlVdXVtK1FudU1kcEViOXd3dExFWHZIRUxMMmJRc2g3VnhaYlVlRDdhOEVBSno0d0dNbzRMSGZzM1RBMHU0WHNOb2wwSnhqUllxM2h3dUtoU0tRdmtFOWRHZHhsMzFqblhJaHlwNGFxb0Q2UXBEUWtLSzBNVGorekZKR0haOEJISFQ3T0hvN0lsV2g1Zk9STVJnSVNqMGpXUEJSRVlNTW9ZU1BwUXdUdDE3aTNoY3RXQ3dTcW81K3BHbW5YSVlTOVhuQTN0SkxyelRoSG9lWmk3SndHYjBMRi9SenY3VFQrMUFDRmkyNmJXU3QwbXNueWpTRERoUm5BWGNNSmtKVVBaTVJtQXhlRDhPcWZSLzlaZHNpMlphWXFSSTZQbmdGQU9DbVcvVFJGZkRMLzIvamFyWmY4bXp2VkZxWWdRN00rZ0RFbndRd0ptc1JGbUI1eHpMcmU1KzdZTVppRVB0Tktvb1dBRkRmMnRUdWV6SG41d0ZFNzY2RzJJZTlVSXUxSGxnSWUwWXFtdWFSOUlJWlhvUmY0U1NSa3BidDF3UU9PbXlmQTJyb1pPZ1p2ZE9CN2NwZFFsNitDTlVVYi9oRUgyZ1JDR0lGUEliU1JUbmtKbHBYVkRlZTJnK2JLWTJBRXZ0VU9nUGF1NU9ZY0gycjQ0bGtzYWdDTE5CYzBhZWV4dXF4ckgyK0VuVGcwdG9OQVBiaGlEeXFBZ3Arc0ZJQXkyaWJsL2dzU2Y0UUxqeWtKN1JDVUlHM0JqUFFNVWNNWkNpRVVIcEw1T0FvbFhxL1BrSXJJaUNjWnVpbFgvQVNoK28rNkluMDc5dnd5U0sraVI5L3Y1UllCVWdhemdUL1N1a0pvWnFvcXI0cWdxVUMyb2JHOXJHSjlvaW5kWWN3ME40b2xzZitrMHA3Qm8yVXNHS0RCa25vUjB6UjhGQmFERVdmNW5TNWg4dUZJQ3FjQTliSXg5eVRCYkt3ZkEvRjVKcjM2UHV3eUI5SStSVUMrbDVJcWtxcGx3OU1zR1BOWlBTSTEyRnpYOWU4YkErcjFWM29wcVpvV3ZnaGQ2bHhrQzVSOEVGWnhCdXVUT0VuOGFKM3Z5N1R5TnFVRnlISXNZd3lBcG1oK1dCWTFvaklGbCtMRzdzZDd0K1RIcnNvTU1BMThkMytCeFJmbDJ0ZTZhSmQ3WHV6cEpIc3NSVkpDV0c4WWhYZHhYb215ZGNNbGNnOTJqSmduV0IrSUVZTmhmTU1DeG43TVJSckdmZkJBRDhXZFJwa1ZPSm5VTU1UU2g4N081VVBGdEE4OEJMQlRnUUpYZVRXMjFDTmU3bWJEMTBRRUZtOUZqZ1RvOTNnbFYvOU9ZK21nSUpEdHdzQWpNYzdia2hMZTVlUVcxMVFCdnVkQzc4TENua3cxSUllSStwRnZ3K3dWZy9FdmVHSzNrMzdJSlVBSmJDSEQyVDhnTDBReW1HL2dWRUxWbVNSNzhkWXpHZ2JXWEFkRU00dVJpRzZhK3ZlTE53Z1BOb053MUx3WGV4bTVWK3Jhck1TUXpxdjlMUGdxMEZlTnlSYWJLcS9HbWxERUFQSTRTME5KUzBZalFoT3k4VHJ4c0JuNFVMOTVGQlppZjZQOG9zZ0VndmwyUU1sSFhqd3dZejJjZk1jTkxOeXRJWTJ6LzhLOE1nS1NCemI1YVJDZmpiR3V3U016R1ZZRHBVaU9TNXE1YURXRE5CQmJWcVljSmN6WWcvaklRWmN1RmM4eXI2KzBGa0piUnQwZktqdzBaYm92UEY3L295WUNDRVlvTGNZakxyTWNHWERJcEhpWUliSkNzSnJFaFFRS2RyZ0h4UU92b0FDd0NTdzdGQ3hocFk0NEFjSGJZeWxYWEFsS3AvdHZaMzJqUnEwb1ovSnlSQ2t5U05Tbmk0MEdkd3ZIcnJ3TzJEa05JR2VCQnRvN01EYW1pamRBcnQwWXdCRm1hUkdQZDZ4SFhpMFJVbmJEaElBZFBuZ2N1UjFTeTlqUWcyc05HS2c1ZUtPcXg4SlhqcHIzMC9SSGlkS1k3aEpLV3Rib1FRcTZYT2UvYU5RdEU3NmtTTWtWWENQYVRmMFhXN2ROdnRJU1R6UjJsQjZJVjRCbndDcCtpNlVIZzcrWlhGQ3RFVWZDVXlTb0ZBdmlnbElGak92Z0VEVWRaSTBBalFKRnZQVWd5Y3BuSVIwVjFjc3RBREJCb0RHRlN2NzFYQ3gxcVhNd1FFMkY1ZFZQejlEOXRLUDBhQXQ4VHFDREEwaklZUDBLSHZESXA3Wk10a1Ara3pFWWlVcWpaY0NnZVg5bHoyNkF1Ni8zcHF4ZE4ybDRBalFMYXZjWllnZmRuSnBqRjlaVytHS0ZhRzR3QjV1eHduWkRFMlY2WllkSVdXTEFpbXBFS1lrYkR0eExBeWNXZit3Wk9ocjBRaVBmQlc2b2YxbkU2UlhPdlpXSTRsSjBPYUhGTmRGWlI2TDRGeURibVlEU2tKU0dwb1lBRkRieGkzNWVLZmsvYksrZWdRQWtXUWRNQU5NZzdjWjNkbU9tRXh0WVVoTWJodGJZeGlNTXJSZFlyS2RhR2NsNTFFR2hVWEZYRXJBS2dWZEdpaVlLMUR4QXNCZ2wzU2ZPMVNIOHphT3F2MW5DU21Wa01Gb0ZNc0QrRTd3VXVZL09NQ3hqQjhpYXVab1MvcVlNNmhKV0JGU2IzczJnTlZIVUVBSzNseVpJMko2Z1hNTVR2cExhSmlGTlFKVEhDVTliUkR6eGhYRjhWQjBDdTU2d3FyMTFTQUR3Q3BXNnllREFzc2lWTUpYYXBsa3FHYXlGS2czRko5OXlSaXI4RU9rcXpBWVlDME1XNHFFaEprQ21nV0pBcWxBcVlBUmF0UWpRWDRlWldVWmc5bTdrQXMyMVVwdWt0eTl1THBRWXN0Uzl3NXVmNHUvMnJjNnhqZThiQTMxT0xIcmJNMjN6dytUWFJVelVBbU43ZHpLenRIT1JZaEtsNU5tNVhoVTQ5MFhXcno3L2lsa3RRSkxrUk9kcmFCWENCWFhIUDlCR1lSK0lzZWdHTzZnZnk4VXZaVEYwYjRRaTVSODhtSVFzL2RlcWZ3ck1WNlNCRkJBa1E0QTJQWUlDdWl1QWFpRnpVRU84VmMwZ2pIRDBrQ1RKTnlDS1o5VWdVVnNUekw2K0pPQURHZnJZQUZxUUt0WW8rcGUyazA4YlRVdEEyc1FFS2xxSUo5czhBWFhyL0l0LytwWjVjdnNmWUVHMStBUFk0ZHkvZzhxOVBSVlZVYkhFbXBpeHprTUpZdiszcFNGN3FzSmQraE53M1pHaGZXMWwwMzQ4ei82ekxnbHNZakIyWC96WXdhRWovQzNBT0FQL2VjVDh1NS92OGw2aDIxN01teG9zZjdsVzhVTkUwS0RSMUI0S1owc05LUFUveFVjSGVNSmxHMFFFSWF4aHdXbDREVU15MFZZa0JRaUkxVkxkUWFxUjEyWWZoMlFQdWlMemdORzJvM1VoaXIySnd2OFVIYWE5OTdhUkpFU2FFVGxoVlRSUWFBaXZJb3JwQlo0eldZUEszSGdNSWkwRmdZQ0lZU2QyNFFqSXRsR2twU0VzaDBRU0RDSkhjeW5zT0wxcW9lMEpSR2NiWk1OcERwSkdBSzVxTENDNHR2bEJoQVVLcFhHTHc0akFpS0NFdEFuUlZaSTEyWDRSSktxcDc3ZEFJV05NWjljaVF6VnArZ0hZVnQ2akVjaUorL2JCTWF4bzMzTVBiZFN4U3FWdTBnWkcvYlBRK3pEQmFCNExHdUNReFNCclVmUlFTdjhIZ1YrRlR6UkYzb1VWT250c3ZvWHF4ZVhVVFdadkhqcGtWd3dnSVRiQVJHTUV0aVdVU3hHaGFLOXBxZkFVRUNBVVNTaUREOEVOS2JXbEMxMlJvaHNRRmpDUHRQdGJmQy8wWGRjZkN6N1h0dkhNOUhPYy9Jc2FPaXIrQUZiVnI1S3NwSkVGVHRveUd1SUkwNGhRYVFLRXV0NC9EenppNjFSRVpKTlB4RUlKU1g3blFqMzYzRjM0QXNWd3BBY0p1UEtxcXc4K0k4SFdGdFkxbEU2R0tUVDhndU1PQUdwRWltVjRNT25Hc0dvc3RsV1N0WGNVL1NsUWxqQWdVcUJ3TWRRQWkvMnhBSUtKa1N2bEVPTTZQY1labnA2VGxkSzhVZmhKck1ZZTVQQnFrYlNEaUpWVmUrNGJ0c2pFOUVFOWdCanFlcTZUOHNPYUlESUhkSkxkWXB5QmtBT2RYSFRqU0NXdWZpUWtoZU5aeXdTU0tWejl0NEFMQTg2SGtDdlk5K1NvZHFFTDBhS0JkNVIyVENJVit4OU1jV3lPaXJuK1M2MnVVVmxpbGRJRnV4THY4VE1xQmtmRFFLMmVEb0tWUnl3eEhxU3djMEppYjBER2R6MndnZjgzZ0JScFNRYjA0eDNmSGdLTE5mVWN2aWJzR1FsNG9HaE5JT1B1R1NHdVc5M29XYld3emFpaExueHltQzh5dHNoWkVIL3ZTQXFmZTJvaXoyaU1xUzZ5aXYxemtmSmhCQ3ovZnRYd0ZUM1psZXNRd3NQOUVxSTJLaTVKN09reHd2bHkyYUtoKzN2SlIvL2hkVWNNc3J5ZStHNVlZblI2NXZLOEE1Rjkvd1hWd0JmUEpRbzBFQ1VVcDdyQnRDdDJQQnNqOTc1RktIMC95a3ZDK202clRIZ2hROFR1Nk5GeXZDVnFIMGt1U2dFSHlXZ1pPSUdnNDZCT0gzUUJkQ05MTTJHQXBNRU82d2Fna1c5VG4zVDJiZlBlcEF3NUhJTGgwZWdsRlU1eExKMTNMRm1SNFl3b3k5OEFBWUJKNlBWL1RRUE5iQWtoS1JVYWNMYW94WWpiSTRQMW1YbFdjd0NFdjF1QTdFbmhzK095TkwxSGZHSXVOanMvczNTNE9KbEIyN1daOUZRNlZ6YnZROVNyS2FVdHhpRTBvTHhjekZib1lDUXFWQkhmY3REamhiUGVoTWtkck12SDBYZjRnSGFJU2lrOXVlNmxUN1E0OS9lNXZRYmpzZERodkptLzdJQWFmaFpmMjV2allOUk9uYTZ3VlFVcU1UTVhzZ3l4TWFGVzdnb2crK2p5VEx5QUgzUzNKcFQxS1pnQlNuanRaaTI4K0VkZEtZd0lDd2FBQWU1UmcyYnJsUk15NCtnZ0xjSUFDeHBsMWhOcW43NjBYTy9aUko1S1pYWlZEQUxoanRMR3BWZ24vVmpiQWVaRkJkUStkZHptSlRJc1E0RUdQakVaeVhjVVE2c28xTHExQ3RBMkdnNmJqR3F6Z2RSaW9qTHFDY0tyYXgwTUg2ZVdodFlRd3p1WHpLOUNRbjlGcjB4VVV0WkRoTEVRMHpwMjhCZUo4bzQwaUwweUthUXd5UU15M1BaRHovZjg5RXR0ak5GWlRMM0xPRFFtcmt0R3E3RktSWG50QVBmeTYySC8zb3FUOTNEc1N4MGQrR1V4QytLTjR4eEh6NkxnN0dHQXRyNllQb3BFVEtySDhVQ0NtYXo4eW9jYkNWZlNPNFkrR0FxV2R4RXRIMGdnQUszUEZ3dityY0FlR1BpbGZ6bEVBQjdSM3hjZW5BZHp0Yi9HVzVENVkxS0tVRVJ4elJRYklOQzlyNDMycHdBcEJUUXh4WEhWL2Ywem1OQlFJTXdDSEg4UWdSOXZsWXd2RWFSVEFJZFhBTEZvdnZOWlNERlNNTEpVS1lvaUxONCtCUDN6Z1VwTWNWR1JXQWtCUmhPSFZ3VUpjcTZEdW1GRjRvemxPZWdiVzR4QjhaSElrWEk4dmxRaGNDWGNaSktNQnZSanBLV0k1RDVTQVdwUit6TmFwNlJWUWRCaFRjdUxKcHY4dzhkNGdkZWhQMUMvVndZV3RwVTlLZU1iQ25MOGk0UGhGZG9tVUZxYWVBdFVDWGtRVmNDcUJLKzZNV2RGNlduQ0dWd0c2UENVTUtDa04yd0k0Tjc5NjlMU1ZWVE5iWm5pd05JdkUrK080S3ZIR1RFTmV3VnM3U2pMK1BxeVZFZER2SkZWN1BaQUV1Q2ZoeGc4clBlRTdHMm9MUzNSQWtFbUl5eGlJbU5PTjhzdWlsbHd2ZHcxTHRmaHFoZm95TWh5T0VRK2JnRkoxbk1XVmtyT0h0VUM0aVRKNmZRdHUxUlQwaGtvTzdGdW9tVU11MElCaGxwanFpZXdFRGhYQm5EaFFmaUNBRU5JdndDSDRlZ05zcUh5c3RFbkM1cTZLT1lEcGhydEUzeGZDTDN0c2JiWTB3MWk4c1ZYOUR0L1B0RlErOXpZMkF0KzRxSlFadGh0aEFoY0RPRzRlSWVVZVNXSVFpWmxwazRLRTAxQzFvNStuN0gzVE5ncVJJdGJNdkE2ZzNySWhnUEdDZ09BUFJNbWZSQ0N3OFdRS0hBSGZxWTkzMFl1dHJoNWd3c1hqRVUzQ2VFL1dwRms1a0NubnNFQldUNGc1bklScWJsYmswMHpQMW5Gc3FtU2wxOHozeVhlakQwdUtUZ09qZjNFdmpDRzZlRWJYUmN5cGw2dHhIVkk3MHJIMHdLTytrR1FFOHJ3dmdJTlFyRWxrSkYrZFNDbWdCZVcyWERGSE9nNE1WRlMyV0dGSFJtM1ZkanBvZzNMZGp3L1lROUNqT3pLMjRJYk9meWkvMGlRQ0lsSU1WODlicitvZi94K3pDSm9GSGlqbE10VUl2dENFcEdJR2RneWVURlhnSFVaS3ZsYi9ZejBQK09yVUlpQlRkVXNsZ2tadU9oL2V0bHl2UXdLZEJyR0E2V21pOVVWS1lFaUxZNXpjNXZQb0lDbXJ4dkFGVHlQTnRwMlRGYzRiNFp4dGc2TVlBVGlPQ2svMHhmNmxRNlc1NGx4Ym9XZkRkNFZ2d2VmeElzZ2hMUVQ3SHhaZzA5bFRYRXpuVk1yc0lxZnFoTUFmZkZIdmtCTXE2NkZKWFkvb0dENVJGRlh6SHdzNkc0eHVLVStnc3hHOWs3NDc2NHhJb0pMNVozK2RjaUVYY3hDN3BQUkRHSkFIa3JZL05jQzB3U21Ra1VTdURpS3pvYnZ3NDhFdEN6Rk1XeERjWW54cE9RZnBlRllsZ3Vta1FzM3lnS1U2Q1RXM1FSTUlrUUZVVGJMbTg5ZlA1UmFaaDNBV1J1NS82QU12eG1uTEtFTjBTQWx6NDZ0cC9najBKOU9KQnlIOHYxSkdPdlpJOWc0dU5UWVo2R0FNMzhUZmF6MXk0bTlhMU16N1lNVUlraTBvVnBZeCtpVXhrQUtPNnplNlRldHoyNldsN1N3dW1aTWhZemdKNFJkaUdLMElyM1AxWlJocWpEc3g4WGo0MjRzUWNBSGovZlliT2puZUVRTlg0bXE5RDBYcTdEQlVLRm5IWWpIVElPQlMwS09LQnpVTkRFWUl3WTJHY0lsM3F2VkdZY1lPaEhld1FpQXVaNVY2Mi84NUVzWUJuYURQQUNjdGZMWHNyc29SRTZtZ1lWdkZJNmJEMUp3WDRYdkZZc203dlE0VmtWaFVqMFcvVlJrL3ZWa0U2OGlFalRDa0RKQVEvaUxaZ2Z0SzB6elBhaVpEWUdkUTltUEV3eHk5NEk0bE91bU81ZU9QVGtzUlQxOC9rWFdUT0tSMHVEcXMyZTNCYjcvUkdOVlNqWXdyc1g4WmJxYlh6SFBWTnN6SUFxMFU2c1RvVjBqalV6MG9zcjB0MGhJVUU1WXN2dCtDRG1EbFdQWC95Sy9RZkxoNERJQmhYcmlyam40Q2d3RXdBaS9RajMwTG5SS2F5STdPSkxvTm5HUnNZUEdZc0YzMTBTL2F4aFZwU2o1d25FcG9nOTF2UGNCRDNROE04TXkvSEw1N1gzbk9wREdwMDF2UXNzR2Q4VnhGRmVMaHRwZFdHZ0VKcUdzbHpES3lIN0pvS0lXZ24yUTIzZlN4RldEbWlVRUxJck5BM2FpS1NVM0cxRzRPQjM5NTFpQldLbjE5Q09ValJuM1E5YS9GNEJUTWtiS0lOL2ZTUWxqcVVEY083ZVRhQWpVMnpLWHBiSnFpTG5QaGRNdWxhN3BTNFkwQ09WTXE5ZDRNWFhrWVhYdGJVbjlEWGhnbDZDNk5QeVE4aFVWdmYxM281aFJGV0ZHUlRkQU5BOHNnVzgyUmFsUzNQdWR2TWlxVmNRZ1JWSHFtdlhjRmJGUDhVYSt2dkZFc1pOMklmekpaaHdBY0xyQVVzc2cyTHRnWUZGUmE4WHBoVURFT2lHMFJPNkZxS0doVkx4VTR4UWJKSXBxSVlMcHNzeEpjdUk5SklSWjNMZ2NGMFNreVBLVXE2THdqTGFRMTNDSVdpaFNQSk51bm9QaFlnMWt5cEV4YWQ5OFJSK1A2L0tCSUN6WnpwZ1pHdFRldm1FK2ZlU09DeTBQZVRGajJFeWhxbTQwSzJvZG9wT0NaTVJuTU8xNENHdFlqQlk3bUc0eHYvMkpSd1NTRGxCWkhRSGdFZUtnZ0djK0VrQmdPckNoMDlKbnFIZlFzWURoV0dRb01vUzVhSTBnbUg2ZWpNZS80aDFyaFFhRGF5OE84QmlUY3V0SEJBR0ZRRHZkUGhBQUhub1FRSTNXc0dOeEhZMDZIdmkvS0Q5eDZ4akNwSjNRT1R4WXJUbUkyd3NqTjJyWHpXbmlJVW5ZaUFRdGxoSjBXOUZxYlNVb01HeEJjUWhCQVo3M3puUEdsbFpsMFNWekkvOXIzdG14SGprY3pZR2Z0RFM0ZWJsSWZjMFVPUncxU1YyR2VwclRQendkdWgzY3J4NDY3cDRacXg4akpSZG43RW9EV09xckYycEF1WW43b0l4WUk5K3BUby9tSFFUWU95cjUrWlV5TEx0Um1uTHdLOVp3eTNaelR5QVVlVjk2M0dKd0h6R3FOdUJvQUU0dUhlNXBLaDRqL0dWVWhjeEIwOFNqc0VPT3dyOHR4QmEra1RTSUg1NjF0Mlp4dmlRMzJyaGI4QUNGWmVoaXBFdFVaZFYzTDRaVHNzMWwxMk5Gbkk5eFQzWkY4UXlGV1hTQUgyaU5va1FDdHp4b1hWZ09mbWh5b1VFV3NSNkJjWU1iWUIza1VrV0dRbGVsRTNTd2FNWlZsSUd2WmVGOFF4TGJjL3ozd043bW5SVFRIWE40T3prYWUvdUkxeTMzYVFBVVBHQmQ2TTkwVUpTNVkwenhWS2kzd2g2b1NGOUZVWEpiSlRHOTNSaFFlNGxMemtVUkVUUDRab2R1OEFKNkVoVTBRN2U4UGNyUjFWWnpTaFRLVGtyNHlRQzJvbWo5cGF5L3lwZDhUeG5wMlkzL0xqVktDcUlFWXIrb2dRaS9kdXBjSXpxQjRRWVlaSWNBeHA5MGRscG9WVDZPY25lbGk0VHVWTzBuZHBCUDVuSW1lZ3ltSlhvc3JKckZSbVVrMmNiT1g5dUxwZ0lCNmNJaUZjMkE3RUxHUFBBQ3BXY2J1QzY3RmpQeHEza2RJclY4dW5uOGk0STIzbGE5cXo1SXY1emF4S2VVUWpuajQxYVNGV1NiaDBKK2o3Z1VZL3FPa0pBVUI5NzMvM1RuV2VubUR4bmhMd2x4cXdWM0lDeUpnT3V5NlcycnhUS0JDMXR6akl5OG1YbEdvcXJkQ3pCTXV0S2xPWXVPOTdqNEhzb2NoTVptMTZNUjBPbldhcXpMcEpVMktoU29OZnJpNWovS2V0K1N6YWpwdzNoNk1ibXQ1ZHdTU3JwMjhVVHU0ei9TbTdheG5XS05nMkVnT0VqaG0zdWZUVEUxZzhBUEhhbWt4TmRCYWtFMnFML3VHQndubS9JcHhnT2xLU0JGUS8zRTl3NDlkNFlsQ2tYaGlYR3U5UVhTdDlEQnhmRnBMc3lGOWV2UUZhSVZJS1VrRklTek02Z1BmZWVkd09QZFZhY0NNNlQwOFIwSDZTK0hoQmJoSlE5OHUxZExzTnZsQVJtWDQ0RHoxbWx3ZENGak9sZGthSm9wcnpHSUNuN1pZSzlONHFiUmt4djRHUmM0ZXlVZU9DQktkdU9rdXhZQzRiNTkrVXJucit3Z2NuZUZLZkNtVVJGS05STVhISndJcU5ST0hNWndLTSt3UENHQ0VXWVZJYk8yL3c4YlV0czF3bEpJanh6WHZrci8vT01WTFZJQldGZFd4Y3o3YkJxYWwvTFh0Y1c4SkFxeVFHanFuQmxVdUV0ZjNJT0hOVklET3A4b0xLbEVsSUh2M3VMaHhVVVE1NmJFdHBwbDRhc2gvbFFoUEE4QlNGQWJGaE93SlpsWW1DRTNHaFk4a0JRMVlRa01GV1E5c0k1YlB6WjJjZFNRT0xsLzZUR2JVZm0xT1hiSVBsNkNCV0VMZWdnQ2JITWd1KzNFSURIeU1jK2dQSkdWN0dNc3hkRzFIdG9EL2w3dk9HZFo3Slp2NkNnSVdUN1hWc0FhelhlK1pFWm52R2Fkd2xuOEtYRkVzcytwUVF4cHR5d09yMEVzYlVjOWpKQnFVVDAxQWIvK05jL2h5OTYvazUwV2FXdXlvd3BMVWRJT1BxWVlwT1Z2bWJQcS9WRkJNd1pTQlhrOXJ1MytHM2Y4MUZnendRMndhU25saUxnc0lrblpTMU9rUHkxQUNsSjdJS0VnOHZRRHJKZ3RmcE5OMW1zMzhlazFVUmM0VkNXVVZnQmNJLzEvTVVBR1haekpWUWxTT1ZlODh0blkzaDZEd2tLdEFPb0ltbEVVZWJFcWdhMzNnN2dCQTdkK3VpTGtyRHhrTW42d29kT3lxN251VjdGY3gyaUorZUZWSWxTZENjbWdMQ0NzWkJhUEdkcWFvTkY2bUFJczJpYkw4SlBpOVlZZC9YRWhLOUJDWXpzYm9DckNlM2FHbTIxa1VzakFmM0t2WURKY1Q2SDlCeG1rQjRUQVQ4S0VhbkNOUTkwYnFCNW9NQVhqVkNpeU1IOGdneFYxRDl0VEZEaWFFVWtYVGxtZGZtcTVFdzdjclc0T0FDSnlkZGl1SU5Yd0hoMHB6Q0RBb0ZvRzk4cnRqaGNLd0Y2Z2dDUkJ1c1ZNQlREMEdNZ1dSa3NyWFJ6N200MDBtNWwzYS9DYTBDbFBIdElldmVSajU5VzFFR1lLVlZkN0MrMzduMFlBSEhzOTlLaksrQzd6aW9BcFBsRC8xTm5weFgxU2dWdFdXWXR4TkIrVXNkcERPTmNCRitzb1NjbUFJZ3ZTZy9mcDI2ZlhCdWxQOXRzaUVNQ290SE5ZQ2JMU2puWFZaQTJRK1BJNFpDUFBZSmxuRXJ5WEtJOHdCRVNJYTBBWGJERXZlUC9tTXRTTEFJSkRoRWdsWldVOGlmMzNKWU9MazVLQVowck1DZTAxVjU1aWdxVGxsMFNEeURnYkdWQkhqMXVDK1VLZGJkWW9YZVRHcHg0bisyMGYraEgxaUl0SkFpS1FRQXMzNHRRbDE0S1BUV1dZbzRzN1BFVFNoN3JrNFcwQmRnMWtSSWhLVWsrRDVsdi9WN28yR1BRTUVjVkVPUUhmK1BkTWoyeGptcmlOR2pwU0xJbmFialMwc3dvZy9EU0xCYThVWlRKNytHN1VKbnlxdDJ6bjVHRHlRUU1JbUFVU2tpaTB3amxSS1RSbU1TVVZIejVxMytlSW1SbElrUmlud0FNVEtrb2Fha3c1aGRmTVplczJTenJpOHNhSy9Rb3pPNDJ3QStoVDhXNGVtNVlRakRKMVhld1Q3Q2dCNndoNDBJb0VVRWkrZjE5TEFvdlYvUzN5QW94RmtVclkweTU0SFY2ZW1YNG9iNUt0MndJR3ZjT0JRNGZUQUV6cFJvWnRTNmpKTE9Ia0IvNnIrKzFEeDdsWXlnZ2lNT2FBSnlyOU1MYlVocUJtcldZZTVDVzh2RlF2eHlmRVBXQUdxMHlkNjJPRjhyNEZBcEd5bTRINWIzaUtuckJScHNLeGhqUXU0R0JJb2ZKd0RXUlJoa0lNVWwvU0dFMGNjRXlBNjVGQzhoNzJBVHBKZHlqaXRSVHlORzNxSHB4bTJvUlFleGxMbkQ4SEkwSmZ6cFFtcGkwdlpYczIwdHZaOVJoMHQybzV1aXAwMkhoc1lvRGxIN1NocHVGalZ0c2JsVFNyZkc0UWxqRldBNlNBbTRRZ21McngwK1FIZjlWSTBwV3JVUkV1d3QzdEdqdmpRNDlKaEdOdDk2U0FHamErTUI3RXFkY09MMGVQbHU2MXBlRStZeHpSdGQrOTQ0WXd5YkZwSlhPRVlpY2NWaTRZa1lHQXUvM2xiYlAyKzI1MEdrNjcxUlNjb3g3OVpwREd1a1dEemZMQjF0b1ZYSy9GMHRCSHUxUDY1M1FUZ1oxc3lFQWtCQmJ0M0ZRaFdvRVRBN3RHZnlFRy9PQlZsclprN1ZhQzFYU2Y3Q2ZyRkVlaDRGM01FN1FxNWRDYm9FSmkyTFpqcEg5Ylh1TkV3V2c3SXNTOHBBZmxMSW1oSVBuRnpBd21OMVd5RUpKbGRlems5WFdRKzhEc081N2tIOGNCYlN6SEpET2Yrai94ZlFCUVRXcG9telRnR2hsNTBob1hqUmYvVWJta1ZBdklIRkIrY0tpUWNMcTlJdGtCbkZKNzJzREUxbS9ocHBRNmdBY2ZKWUp3a0RlSlFxUC8vYVdBQWxJYW1GRlgrcDFjUnF1dElSZXVtbWw4MnFGcEw0eXBLUkRRYkdqK2dxVVpHQlpEYXZFTUNLRHdDVXNVMFRISlY4TG02U0ZHY2tlWU1SMzNXRDN3eUNsNUQ0QUJlUDBUTHI4R0FwYUtPVUl5RHp3ZFhsSWIwaEsyWmMvS1BhV3R1ZjBrMThWMEk2cEdnbVNDTk5JcEQwbk1yMzNOd0FBUjQvQ0pmOVkxeEVDbE9tWjk5Mk9yZU4zTXExWTlDVEpRS3dvb05tc1lQR2hybng5eDBOeFl1aUhSc1IvaTJRdG8zaVZMdUNobSt6TkUvc3hYWng5OFRpWFo3L1RmQitsTFc3MmlKNmFpQ3d4Qy94YTlQNkxYL04vUlpITUFzWnFJZE41VnpXaHFnWUdIT1JVWXN4azhIZklKekFXUTZSOUg2SjAzaElBQTlreGZtWGZpWkRTUUdtS3lJWE9UbHdjOHFFc2V5cElBejZlNVRNOUJpd1dNZm9Ra0FpQVpvSVpVby9OMXFZcXlkYjk2L1ZELysyUDdMdEhZK28vNWtYY2VFc0ZZRDF0SGZ0ZDBVemZTWTNGMUFOQTE5anNHR0RoZnRBSk1Nb292WkZsaTRpQmF6WDNMZjJKbGg3Rys0RTl0aVMwWUQ3Qm9NTEFVajF4cjVCTEVHaEFjY1VXQk1XQXVzS1dGSldQRHNVTkFpMEhyOE9SQStFSHJYci9FaXpYbkJDRnIrTFpZTnNydWtvSmJqQWpVS05vNEdhZkxLVUlSNDFaaUw1cDZhLy81TjZyNENMWjBmRjNnU3NEQzArdlh0THMzaW4rMXZoZWI3VWl4V2FwTk5leTdCWldlMWthdkJwYVp4dWo3QzVhT3hHSTRiK2t1YXBxNGNhZDd6NFAzSTNERFBMZzR5b2djTnZ0TmczT3ZlY29wdmNLMGxLa2RWaG1vbVlnTnpaSTdDZUtkMVNLRU1KKzlmL0ZJcjZMdXJaaXJ2cEJLb251d1l4ZjJIT2FYZ3ZNUGkza0NlQ0JrT0w5M3BxV3dzbUFpOHJZSmN0U2FBdlRIY1VRK09MMXNLZldLanJZb052K0NFSzBONm1NNVJUYVU1UkQzRGtvb01iRm9MUllHY0RQeGgzSWFEQ3JJckFiMXVYRjI2cjk1M254ZlFmS0hvaURRd3RuRTlUSWNJY3FNVDZGdmxRQ0xaQmJTbDB6SlFpa0puV0tkdVBoWHdZQUhMbTVXTnlQcjRBNHFnQmxmdm9ENzVTdGV6NGk5VklTVWlFVklCWE51dEdzb0pUcEk2V1R2UmtQS3hqc2ZhREVnWUFLaHVoSnpuZ3YzSUlia2VJUUZpczZCcTdXc1dVTS9MQmF2cFFTbEFFYzFoMFBiamRZZXpPNDR0TWZJN3lMdng3TVVQbWU3NlVqbm8wb09BOGhqNkZGWTk5L29QQ0MzaTZVRElkclRWbmNYMEpudzhPUlhnY0NXcGhTMnVtbEhyUU4wYTRqWEp1TWJoUzhHd1h2aFhWMVlyeklVZDBLWmdCWlpMUU1rcFEwcm1Yam5tbDM4bzkrMzU1eFhaSFI0MURBNG9ZMzB0YWRSNU91QzlPRXZ0TW9FQmdnZHdMdEJKSjZNcHFEbndYT0tmcThhRlQ4WHY2QlFpZjAxckI4a1gxUlpaRXBCOWtSd3RORjBtTWU3KytDUnNVOWVtUFMyN0VoMWxrVUI0SjhVSXFqaTZpZTRTSzFVeXpwd0FJV2FCeE1lZHg0bUtwMFJYT0xNOHpyRHNub2lHSlpySlUvTzlDQ0VZeUZjQzdkOWpIUzRVNFdZUlRZVC9LaGhRUThPdll2NkVCMjVXWC9OM2RBR2xGR0U0c1owampweHAyM0FhYy9hTzczU0FIaWowY0JnZHY4QzJmZitxdG9IdTZRS2dCS1MvRDdRelVUemJ4WHRDSGdodU1IelNndVZCbkhQdlRZb3dRck5FN0tsaElPc0lxZHRtTllNWlNHOE5PZlBWSWI0TG1nQ01ySm5HNForckwwWG5EOUFFWDJ3RGVqV0tqb1FVUkpBak9reWJpV0ZHdENySExTWXc2clNiQXlRY0FqVC9YVHFnbXpNRjN2RmlXd2JFYVB6WWJMSERMTDNqejJ0eFFzcGlwQU52eVlzMi9wRytWWXp0Y3lSM1F0NVJUTUJkeFlGREplOHhtaktNL3RxUzkzenhtOUd5S1FNNUJia2RHU0pDb2hJMDNkU1ZUbjMvR2ZBQUJIYmxyUXVjZW5nSURpTUZONy90Ujd1WG44blRKYXJtSFJzT1dBUVN0RTZPYldzWkkwZGsxMC9Sc01PQXVHTVoxZ3dZQ2dYbFJlSHdveldJR2xYdUJWdmg4RDZqUGJqWWFTaURVa2RKV0s0RVNqTWNVSytXb1psclgxVVg3dkgyUGZwTDR1aVl3U2YxRTRBRTVsM1c5NWhKdW93YW9VRHRvK3RNck8zcGtwQmtvL2k3ZElnMnhGS0RFUXA2Z1pxUS9EMFhFOGE5eW9yTGRSUU1RUHRmWkJpdDRWZ3hHVE5UQzBQMnNZL1BRSVdBb1BtVnRBd0RSZUFqVnJOWjZNMDRYYjc1NmRmZWR2bTJHL2JlR3MrTWVyZ01EdFJ3VkF4c24zdkRGMW15SnA0dW1naWk1M3c0UHpMUmFNVTRac1lFUEN3a2tJV3l3cUszNXd1RjFFeUU2Q1dSblNLYUhBWEd3b1VUSTFLSTltcVp6dktaeCtscHR3ayt0ZllSbzl2WUNMZUNOYVNZaXZGNUU0ZEMzOEVlaDhpQVUySW93Z3hQV01mdXRCRlpDL3JpNmJ3WWVSNDFQMHllaEg1b2F5MlB3bHNtcGZNT0JOalR4N1pDMkdxYnpZcHFuSUFHVU9GOUVwc2VBdGhrRk1YK3pRZjBkYklqZE00MldrVkZQU0tGZlNpS3gvOU9jQmJPQjF2MXlGUXNUMStCWHc2TTBLRWVqSjMvb0YyYmpqWGhtdGprVFZncEhvZEVxQ2RnYTBMWDF4dGpjeFRMWS9zZzhzWS9xN0t4R1UvVXpLL25uc0I4UGdmMGhZZW1HSEFNcnZZUTU2VHRKT0hFckZneHB2SFFvdHR0bG12RGZzdUF4TnBiMEFMKzQxOVdjQ3lwbFhBTUNTUzA0MFk1UDhHYVpFSmROY2hzTGxnM2pkRThiRENIbm9GZENydkQrd3YzOUp0VEdVYkpqOTZKOVZTckNHYllqSUhIQzIzQ1JtZ1V6ZnZ2anA5eHF6TnJTdEFJUk10cG5sckNjMUwzeGtxenZ4N2w4QUJEaDY4MFhHNGhOUlFJQjQrUnRxQUJmUytoLy9kTVVOZ1ZRWmxVUmEzSEViZ2ZuNjRHdHFJRGdpdmg0SHlvQlg2cFVrMlArU2p3emVNQVl3RXVEK1h0UWwwcDhGandTOTFoNFNoMnlMOXRYQ2hITmJFVTNTSWw0TzlZOVJyaC9hM0F2Q1Y5V1JVbGwrVW5SUTJqRFlXeEJWUXJncU15ZkJPNVk4ZG1DeDR1SThFS0RZdHE5cTJJMlFCUXlvZ2IyMDUrUUFQM0ZVRis4VnVEYnVZd29UUEI5NnpJZmVla1lRR0FyUDBxNjRyNDlCaG1IUERzZ3p5R2dpa2tha1VxdDZuTEIxN0xmbjh6cy82blVGSmZqNFpCVFFnaEZTbXVPLzhaL2t3Z2RPeTJpMUZnV1JSaTVjdDFEdEhKaHZ3bmZLTDcxeUhOYTdQVFBwQTNUbDNKVlJBd01YN084WEFVV3FieWcwMThyc0ExbStVMHlJV1lPSWxLTlFaeWdTYTFQUTV1SUhPVVRUQzEwc05obGlQNW13SWpvc1hCQ3hBbWV5THgveEpJLzJJdUdpQlhjalA3QmdBNXFKcmp3RFcxelczNFFmZENoVG1oR3lYdUJPdmVqVmI4cmVMUGJQRzNJM0JRUEtZSXpRNytFREc4T3VBUUJXeXpzZ2JJSFJzbkQ5THRYamIvdlhnQ3h3ZjhQckUxTkFRQ0UzSjJ6aE9FKy82NmVGYldJMXlxaEhybXl1T0NMQWJBUFF6dDN0NEVpQUVKekRFanRmQklZWkkzN3VGWFU0UUk3ZUpKVFBYZlZBVnNQSXQzYzlFUlhqSXZmVWF6UXplb3RycGJEMmhrcmc5QktxZ0RENExzVTIyTGlxNzM0UWFTNEFaZjF4UDhvQWsvUXBRaFEwNFNwUWxNcVFpWHNYZ2U5TXdCSzA5SUhMME0yS0s2RXpBc1U3aGR6aEUxVXZVdlRlWFJlWmVBQ1NkU0RId1JnVXVPdGVyWnNqVGJaSlNqVkpaS25HU1U3OHdWdG02eDk4Ty9DNkNqaTZFSHpFOVlrcUlJQ2pCQ250QTdmOTYzVCszU2VsV3FtRUlLb0pqQmJvYkt4eUI4eldnVlNzWURFbExnMjZWK0lDZVIyalhRQTF3K1ZHYUl0K09nT2xsbkJ4dnhqN2ZGbEpGMThwYTFrOENKSjRqaFRERW5nS2hYWVpBaVc3ZFgvbW1vUzdTNUlrTWlPRy9ZZGZLd2Q5eTRBbnhFWHRCV0xYK3VGNkRQVUowdS83WnorbDhOdjd3MzUrZUNYTTRMNUZZUjFuTDV4SUd2OTZVOExWWnZ0U2owZ0dHUkZYVURvOHlLMzVpNlUxVWx1aVhrdnB3aDFvanYveHY3RCtuSGhFNndkOFVnb0lRbTVPd0lVem83Ti8vRytUemhJbGRYYjRSdVg4VW1kVXpOWjV3V3dEU0pVT2JFVkFsMEd1bURIelhmRFpFYmpqd2RMcDRyN1RRQkE5VDFnK0V3TFhRWEFObHFNSDdEdVJVa0s1a2NBbWtTbGhoTm1MSVFsQ0IvMXZTYUthZkdpdE9GWG9rVExOQnJyWHBSVE1XOEFYak5jczFneGdSMS9HYURqTXd5ajd5YjA3TFhuaWNLOXErRkJWeTFyc3lEYkZzOHBKQnpKSTh4RTlGM2dSOWc3K0ZEbWVMVFkyT2JDbWdCM1F6WkNXdGtHcUdzclVWcElUenIzenQzTys4NjFtL1c3cm5sZ0Z4SFhFWWFhdEI5L3piM0grL2ZmS2FQdElxSnJxWmZhTGh5aElpZGc4WlNaYTRrd2l3cmY0Y21Yd01kYklBME9EK0MrWVJGeVlSUzJHK0Npc2dkKzdENTdEd3ZiS3VQRDNJS3EwaWNHb1EzWGI0dGh0MFlPVjNmN29SOWJRbGJGVW9sQ1ExR3dIQUtIUXQ0Z1RLMkRSc29oa2FNakxFempzSDNyU1hOeWxYdnpadmc5bTNiend4cXVpclQwTFpmT0RJS1V2U0NBK1p2SXlMQ3pML2VQTm92QUtkSE5iY2JXMEE4Z3QwbVMxcnJmdWFQVDBIL3hqazkzUngxU21UMFlCQlRpaUJpclBYRWduL3VmM1Y5MDVRYjJpVERVeFdpVTZuMFVDNitER0NjTkdCZXVJbEVBaWZFazVSTU5QY2J4WUVBZ2hEQWNBdzBFY3VKMGdUT09lQ3NTSjd4RTVYcnpST1lOd0tiZVUwS3I0S2RGd3lhd1JtUnI3UzRqSFZDd2JGUWlvcWRoV1I3ZUZwTGFYUzUyQ0ZuM3BHMEg3VmloS3lIOVlCUk9hMkdOYUdiUjRtQy91bFVpb3ZiVURGbEo2UmJtaW9JSDk1QnA2SXMyQzNBSnRnN1N5RXlrSmlDclhTU3M1KzQ2ZmJMZnVlUy93aGhweEFPNmpYSitrQlFTc1NPRlExWjcrNzcrSVUyOTlpOGhrQkhhVXlUSlExVVJ1WFFiSkl1S3RNNEkwR3VRckFkQTNvWUtucUdMaHVnQUxicXF2MUFoWEhVTmhyNWVkRStKemdBdlFBNVlCWGlyOFd5aDMrU2tZaHhKYmU2YmlnMHZnR2E0K0NnMnExQ3RIYkFya3JsMmpwMkh6QWxGbDloeDNzV1R1T2VJMDBIQ2V3ejR0bEdDRmh4aWUzYXl5d0NxNFpSOVVKTEVzVnlqeUlNQ2NGbVppT1puVGxaYSswMEtCVVI0RnQ1dVU4UXFxeVNyUXpWa3RiVTg0ODc5T2J0ejc1aDhCRGkva2ZCL3QrbVFVTUM1YVVhRWczZi9HNzBnWDNyV0ZhazFFTTJSbHU4dTl0UVVZS1NrMnp3RFQ4MFNxdlFNMGdhai9CRUNuMTZHVnhUVjBvOFhnNit6OVRPY1VFZFJCTVc3bE5aQ2dKbWkyYlNzS0YxWmNEd1kwZzJsQ3pxVnV6dytjRERVdmh0aFBVUEkrUWxJeWlxczR3QVNJZVZwYmtSa0ZockVhdE5SQjVxRXNRcmtDNTRtdDd3aWVFQVpUc2tldzlIeHliN1ZjYWFBRkQydGtSNkxmU3M4QmgzVmplYTZOZzVRZDJpTXJ3OGkvTzk4SXRhQ2puUXBBU2FzN0NMWmd2WnJyMlVPcGV1ai8vWDRBRHdPM0N4NkI5N3Y0K3ZNb0lPd0J2MXcxemZxSDA3bTMvZE9LNnduVkpJc2tZSGs3bUx2QUQ3YTExL29Kb05zQ1V0MEwxU3BXV0pSeDZIS0htQzdjQ3djZklBYXVxU2pjd0oxUkNxY1lPTkplNTBEUjQ3dG0zZm9RMGtGY2VNOTRVUXpmS1dLMzFTam1LdThMYkp0b0x2U3g1SWdMQ3pOc2RvOWRhY1M5TFNPVGdZczJ0eWdZVUNEaEZRaUhNMjR4dldNeVlCWk1EejA0R2VUaEF3OFBDNGdMUml5TWcyY2ZBYkFWZEkyZ25hRmEyY05VajRDc2VWUXYxZTNKdC8rUGpiUHYvWTg0ZE9oUmFaZUxyeit2QWdLNFdYSG8xcXA1NFBkK1ZFNyt3UjlodEdORXBjcG9GWmhzQjlVUE80d2hPbnZjWmxEbHhMZzRFQXA2Z3FYc3ZLZFZaRUZJdHV0VzcrRGlQUlk4R1lOUVpyL1BaaXVtN0FsdW9IZHBwZUExR3RzdklQV1greG9wZ3ZEenhsWFZuSHJ5bkpYcHRxbWVDSDNuaGZEZ1BVVlNmdUl0UlhHNzRRWVI3OVBjYTFuMUFQU0YyZzVKd2d1QVViZlhNd3ZEWUtkWE9wZTdEcUp3b3BjUFVUWTFKeFVwbVlIb0dxS2JVaWFybE9VZGdxNmpqUGVRWjk2NWpudCs2VHNoS2VQbzBjRURIL3Q2QWhRUXhORVBFQ1RidS83b2I2Vno3ejRqNDkyUVBHZGEza25VSzVUY0doY3JOSEw2MUwwMmM0TzhIcTQvS0ROendQbHA4SFVGdEF6WWZSOGNLMnJBb2xVTHVSWWxjNFYzRGl6U1ZnNEhZcmx2WWI0QVFDK3EybU9SbW0zVGxNelRKY1NxZXNES2FJYXBYbzhFc2cxOFdSZXFnMzRLZ0NqRUtBYVlLRFFOdlovSTFzOHlNd29ZR0V5a3dKajB3SWVEeWVqZkMxZ1REd3ppdVhpYW5qYVRWRm1IdWluUXpzMmFyKzRYeVExUkxlVTBlNkR1VHZ6dTl6Yll1QjM4NnFBN0h0ZjFSQ2dnZ0NPV0ljSDdQMVE5L050dnFQTEpXa1pMTFRoUHNtMnZNRldndHNiZEpTRjBEcHk4eXhQaXFkQ3p5QXBmdCtCQkJYb2lGazRzQjMxUWhCOS9SMVdMdjJZQ1pYRXAvVkVSdmplYk1nTHpzQ0IwM2pEMEpUSzczc25pWXNzZVFvUlFZMjJjdXFPMzFBZElpREsyT3JYdmtwWlppREl4aVdpZGd6NUY3cGNCU1ZoS3lNd3JEQ1pmQ2M2QU9ESWdnZ1dOVWhaS3liRnJZRURIZzlCYzhDWTcvMTVnYlFMYVFrYVZvRXBBbmhQZFRKQWJwdFVEdEFSV25hdXFHdkg0Yjc2NU8vMEhQNE1iRDllUDEvWEc5UVFwSUFBY3pianhjRDAvK2RhZnJCLytuVnRUdFRRQlIwMGxnclR0UUgvTXFpb2dOZEhOaUZQSDZOWWlablUvVUFDTFN6Rmp3Rjc1UXRFd3dFNVJsT0JhR3dQVUZ6S0lEeXJjWmFPNG40SjlDR2lwQnpNbWN1Rk1EM3VxUkh0a3NOQk5FMjBGSThvbWxDSGhnaEZsaUYvajEvSnY3L3JLb3ZJQkx1dk5xVzlxVklLSFJiY2FVVE44V3VmaHVTcytnVGxRZXMwOTNJbUpBVnZqazFhM1NUVWFFN2tSemk0SW1oblQ2ajVVazFXaHRqa3Q3YXpsNU8vZXUvVFFyMzByU0NtRnk1L0E5UVFxSUlEYmptU1FVdDN6eHI4bkovL0h2V2xwMTBTVkhVWkxJbXVYMlBKTmNVQmJqWUIyUnB3OFpsUU5vbFRNR1dnUy9jbndRM29GL3ZzUVhjZUxHQTVHNUMzZHRNVUF1U1B1U2NtZW1uUDlZbmhjb3BRemM1amlOeVZ6VXdlVS9XdVRlQ1dEMnU5dVN5VUZuSXlJaGozSDNYdkJJUy9wV1lkNEhkRjI2VmZOTVNZZmZiK1hlTjJ0cEVkaVlmVjdTT05kVk9kaTJVOUNvNytBdGtXOVk2ZWt5WWpNclhEcnZLS1pVbFoySWEzc2hIYU5ZcndQMWFrL3pNMnhYL3ptODVCejVnRWZ2K3VONjRsVlFJQ1FtOU1HNUJRZitOWFh5Sm0zWDBpanRRcmRMS2ZKS3JGdEg5SE9mQVoyZ3FvU3pDOEFKKzhFNmhHY3VyVi9DM1ZRTEJOY212MkFGSUVPWnJYcDJjQVNCTjVqQVBmVWx5RUp5dXVLT05CUVNyUmhpUXc2S1FoQjdPSE1XSjl2eHlMWlhmdFRydUdHeVV2L29pVGY1NFA0TnE2OUZTdkVlVmptQ0lqWTkwc0xxekVFbDczbFJ3Um5kQzVRRTJLcnQrSWRmUHNONXNDLy9XUVhBbTByb2kzSHUzZXpHbytadWc1NjdnUXd1eUFZTGFOYTJ3OXB0elNOVm5YVTNsdngrTy85N1p6UC8vNWpGUnQ4dk91SlZrQllROTVRdDF2M3ZFZVAvOTdYcE5uZHFDWTdNdkltWlhVUHNMS0xhRFp0TkhJSFZHTmllaFk0OFJGQlBhSmxSSEpmZUZvR0lZY1ErelJjckYwTnF4ZHV2bkNDaXA3M0FqMGZERVMwMXdjZ2dLcXk0REs3elBsTDBSNENvbEVNYUF0OVhFR2RqSGJqU1VVc0ZBNjZ6blF2aVU4cTUvL0N1b2V5bFEwZkEvTU8rb09DWTdXZmJLNjB2U1hUb3NSR002bnRYRkh3bi84bzdDQktkVXVyd0d6R3FrcFlPckJmcXRFb0pWVjBweDRTTmh1VWVvUjYrNlVpeklKcW9yVnNqZmpRci8vRTdOd2YvbWZneGs4WTl3MnZKMEVCQWVCSUI5eFk1N1AvODNmU3c3OTlTOHJyWTZuM2RzaHpZTWZsZ3VYZFFEdTFEWDFVRStvbFlQTU04ZENIakNORTNVZXpESThaMFZyOHdERk5XRE9QWGd0Rk1jQmFVTmhDajl3RDdINTVxTnM2VGJBQ0VKYU4zenlMVW1JVXVDNFZna2hnSmNTeFh0SFhJY1RoRUk3bm9pVGZBMUxMeStqZzJiMXltWlVDMkxmVHV4SmtmUWpsWTZ5K0N5d1VDbHpzYnhRYzlCVkZkaDlWWWpiRGVHMUZWaTdiRDBrVlJJVHo0M2REWit0QXFwRjJYQW1wRWdpMDlYaHBoQWQvNjNkbkQvN1c5MXJRY2RzbnJYekFrNmFBQUhCYnhnM2ZOcG85K0Z2L1RPNy81ZitZTUY5aXZkWWhOOER1WndETE80aDJibHhaem9KNkFzek9BUTk4d0FSVmp5MWlHK1RBRVF0dEE1akg3MUhURnF2cytrWFRqdm1DRXd3UUh1NDRyQXRNcVFiMXBQNmZBSkR3Rlc0RnhwbGlzWGg2UWZMa2pWaFZnMEJzMTEyVURIYnNobEd1b2djQStvQ2o1ekxWZWNQaEIwdUdnc1B2R2U0YkxpRFNnalBRNDJYMDd3c0ViWnN3bjJQMXdGNnM3TnNEWmtVdHhQemVPNm5URFlFSXE1MVhTQnBOd0p5N2VySm5sRTcrL251bjkvN1N6VUJxY051UldONzNTVjlQb2dLQ2VOZlBkamgwYXpWNzROZitkdlhnTHg0ZDF6cUdWSzEwVThyT0t5Q1RiVUF6UTltaHFoNEI3UVp4MzNzRTgvT0N5UktzdEN0TW12Tlo5TEwrRWdsSDNyZFloZUFFQ1RDNVZTVHlJQU1nUk5rcHl6WllDc3FuTE83MXIvWEZLLzV2TGpvUEFFejJPUlhhZ2RnQ0pxL2pOT1ZXM3l6U05MaEZ2ODJJbDY1eG9EaW1QRDNqM1BlTEpYMFhTMDhqa3JmK0poUnUwNVhVYUMzRGhGbHR5V1RjYjJNTEl4SHNmKzZWTXRtK0F1U01LcmZZUEhZNzh1eThnSXBxOTFXb3hzdE1lZHFNbG5iVjZlVGI3dHI4OEp1K0NwQUw4TjF3L3J4SzhtUXFJQUFRUjI5V0hLWk03LzJ2WDg5N2YrVjNxdEhTU0tSdW9ZM0lucXNneTlzVTNSYVJKRUU3b0JvQklISGZud0xuN2dQR0t3QWcwSnd3eEdpRjVvcUJHbGpDWWpGMThjZGNqaFJNRkNWZi9yNGdBOXIxVzd3Qm9DUmo2NlRYRVh1OE9WWWxDZkgxN2toT0FubDR5dGhHMmsyMkVCSnJZdm8yWTBDblNBbXA3V0dSUWd5Rkd2aGs3L2N3Q0VQMFh3V3FXdlowNlNjczBiYkE1aGEyNzl1dWwxeC9GY1YyUXBZODIrU0ZqN3dmZWI0RkNDVHRmU1psYVp1Z25lYzBYaHVuVTM5d0VoLzZxYThHSHI0YjRDZEVOai9XOVdRcklBQVFSd1FBdS9hK24zOU5PdjZiYjAyVDdXTkpxeDI2RHRoelRjTHlicURac0gzT29JRFVndEVxY09JWThPQWRRQm9CbzRrRFpyOFdLb0xETGNaZ0RpQ2FPYWUrc0xsc1N4dGxUWWdJTzB4YmVjS1F1U2o3U01KWG12c3V0YmJaQWwxTmdwWkpQVXNpT2lBUzVTTGRHZWdUdkRnZ3p2Z29CYWZvVTNkaGlnZkFkRURiQkFUeFdlVllNRFkxQjRIcExGV2F1Zjg1bDJIbjFRZWttVGRncXRDY082WHJIN2xkbUpXb3hxajJYc3RxdkNLY1R6VXQ3NmlyYzM4OHhkMXYvSm90bkhxUEJSMlBYV0wxaVZ5UHZrZjBFM3NwY0VzQzByeTkrMmUrb21MK2I3ajB0VitBbkJycHRrYXk1eHJMNnE4Zko4YXJnaUNTUnl1QzlSUEE3RHh3eVhPQTFkMUVPN1hiOWNreEFLRFJDOGtQaEZCWHlnRTFGVUFjUU1HVmdMazBMMmF4NFk2c3Z0M1dqSXFGRHlKeElGVy8vSWd3MEFlQjlIQ3l2QmQ3RXpqREptQy9MQUJsWVJUOXB2VEY3ZlJHRnVWaS9OVlhDSVV5aTd0andOdzBzSmp2cmlDWXRjUjBMbXVYN01DZVoxNGl1V2s1bjg0RlZZMk5qM3dZV3ljZkVveVNZTHlOc3VzWlZtWFJkVjI5dksrdXp2ekJPUjc3MVpzMzUvZjhQbkNvQW80K2FuWHpKM045cWhRUVhodVdJR2s5My9QdnYzaUUyVy9JcGE5N1pjZmx1VFJibzdUckttU3BoZWZ2RXhtditCaGtZREl4ckhiZmU0RTlWd0I3cnhFdzB6WkRLcUJlQ29sYVRsc2JtZzFGMkVIYkJKMTJiNmJlQjVBVUpDRXoxSk8ySFNrSkxQdjdsWldVRXVNc05NMVA2SmVPZUZXTkxadTFJQ2FESFlBdVowQmFTdTdLQ1EvRG5HdWhVOHA2WGwrN0lhNmdZVmZMb2k0TThXSlB2NUNtUkVwaWZZYnhaQ1I3WDNBVkp0dVhPVi9mQXFvSm1CdWNmZCs3MEc1ZUFDb1JXZHFMYXZmVkVEWVUxVnd0NzZybDVPL2NQei8yYTEvVmRjZmU2Y3IzaEZtK3VDNVd3RUVXLzBtNUZOUUtTRnNIN3ZtRlY1L0krYmVxeTEvM2hVeHJjellibzdUckNtZzFJczhlQTZxeFpVczBHMTB6WGhLY3ZaL1lQRXNjZURhd3NoTkdhcnViS3EyUEFSTnpaV2xnN1VDVTdNcHdXV1lheEx1cUErdG1Gc3VaSVBHUkw5NnZCQ2NDV29JNEpVdG9LSVVXSmRzR2xobFpnYVNFZE5uSVFRWEtzdFdJNmt2aFo3aGROMk1hQmFlOU9TNFRMS3lmSzZZa1VYWVViczJZa21MUDFYdXg0K0JPTkxNTzAvT2JVaTB0Y2ZPaDR6ai8wVHVVdVV1b0s4aXVLMUN2SGFEa21SRGpybDVhcXVXaFg3dTd2Zk9udjZRRFB1aGMzeE5xK2VLNldBR2ZUT1dMS3dPYTdrZWE0djVmK3RLeHp2ODlMci81RzdpMHU4WHNWRXBybHlTT1ZrUlBmaERRS1ZBdjl3QjZ0Q3pvR3VLKzl3RGI5Z1A3cndYR0U2dlFVRHBGNHU0M1BIUm9pdTFHWlJwbmpEQktSazRkVjNucGtRUzR5N1RUNVFWSTJUS0dpb0VsOUpJc2NlS0hZaWZqQUFDZGpDbFFzZmozV04wRzlJRUlNRkE4R1NqV2dNY2pnVGlMeXNrZERWZWN5N2h4cXdHNkJ0djM3NVM5Vis5aEdpVnNYWmhDWklRMEd2UHNIYmRqNi9qOXhBaUM4UVRWM21laFdsNGptMDJ3WG12SE5VYTg3NDIzYjk3enBpOEY1QjdnNWZWakxTcjY4MTZmUWhlOGNDbWdDV1RUaUh6anBEMTFyMXg2NkFmeXRtY0QwMU01TGExV3ZQUkY1TU1mRnN4T0ErTTFBQW5RVE5RVndCcFlmeGpZT0FQc2Z3YXc2ektyY01sdEpQM3Rzc0Ztc1NSV2ZlTnVMcUZzTFNZUnlKQVFqYzBwTEhWUGtuNkdyNmhGUHNaT0txREpDYUpzR1JNbWgzR0V1WE9oZGxtaUdLVWNxcDVMTlUwb0gxRDR2OGhEUjRRK1NMMzE3aGtGeDBaZm14Ym9PcTd0WHBIZFYxM08wYVJDdTlYSWZFNVdveVZzUFh4Q3puN2t3NkxUVFdLY0lPUHRrdlpldzVScVliUEZ0TFFuVisyRlVmdlJYMzlMOC9DYlhnL0lpU2RiK1lCUG53SUNzSnBpNE5acWZ2TG1mNXhteHo4NnZ2enJmaHk3UDM5WGJzNjJsZVE2WC9wWndQbDd3WE4zQWxJRDFaSzVJR1JnTkxHQk8zNDdjT1llNE1CekJLdDdBVzN0ZUNoemNZcytzd1FmYnBQQzJoaUJTOGtqTUpOS0lnTnNGQ21CVEpLZ1pEbVdVZjJrZUtFR0RXeEVOSXdIMUFobDNPeGxWV1FtQ05RMjZ4TlB5UldhSlBCalVDbHdWK3lXR1E0MUl5bXVzSEo5b1dEV0FWMG5xenUzWWUrVk96blpOdUZzYXk3cjV6UFNlTXgyL1FJMjducWZ6RTZmTk9NOEVrbmI5N1BhZFlWSzdnRHQyc25LQVVtYkh4M043M3JUVHpWbmIvdE9zL0ZNVDdieUFaOWVCUVFBQWpkbjROWksxMi8rVDdNN2Z2VGRLOWY4elZ2bGtsYzlwKzNxRHJPekZiWmZLV2w1RjNqcWc4TFpKakJlUmprekRRSk10Z0Z0STdqM1BjVGFIbURmdFlMbEhVQTdoKzFYTXR3TUNiNHRSZVRzVW5GcVppYzFnVGxDRkZFRlUwcDJyRlpadHRQckhtQndMd21neUVRVU4ybzRXMXJVcXhiVzJLR2ZwZUxZc3pMZUZLZ0hSSFNsSFA0ZVFZa3J0ZEFxaTFyRjh2Wmw3TDcwQUpaM1RNaXNiQnNGVW9WMjh6eTJQdlFCems0K2JKaTRVaUF0SSsxN0R1dmxGYVoyaTZqWDhtaThQT0daUDloczdqdjZEMllYYnY5Sjh4THlTVlcyZkRMWHAxc0IvYm81QXplTWdIZS9iK3ZPZi9leXlmU2huNm91KzZyWDZjb2wwSzJITStweEpRZGZUSnkvQnp4M2o5bXUwVVRBWk94K1BRS3FNYkIxRHJqclQ0anRsd2oyUGdOWTJtWUZEMTBEazJkbExybGsyclFQUml3a0RnNkc5UE9WQy9Pb3R1SXRvSnZsaHEwMFVFTS93SUxXQkJCRFprb2lzZEIyRWRtcVJoQXlxUGtMUzQzNGU5SDkycndSTkIzR2s1SHN1SHdOMi9hdUlsVVZtNFpzWm8zTXo1N2w1Z1Azc1RsN1VwQlN3aWdSMmtLMlhTcHA3N1ZNekVDM3hiUzhGMVZlbjdUSGZ2Vi9kUS8rd25kMXdKOEF0MWErak9wVG9uekFVMFlCQWVCZExYQ29ndnpLeWZtRHYzSm92SDcvZDZYTFh2dlAwNTZYTE9mWm1aYnRSc0tPcTFOYTJRK2UvU2k1Y1ZwUVZlYUtvN1M5bWdocUdENjg4REN4dGcvWWZZVkZ6QW9pTjhsM2lFS3hmblpGdFl6cG5rZ3E1SVptRXBXSXI4a0kyd2tnQ2NYY3NlZFd6V2dGWVZkWU9XU2FmeVpFcU1xazdvYUR6aXhiQkRzVzlXV2NnMXBCdTNHMjByK2wvVHU1KzhCMnFaTmd2cm1GNlpsVDJEcCtSdWJuVHR2R1VLT0tXS29ORjZabHBQM1hNYTNza3RSdUFGSjM0K1g5STF6NE04enUvYzBmYTgrOTlRY0FhUXp2M2Z5a3U5eUxyNmVRQWdMQTBReENjT2pXMUJ5OStTZnd3ZmYvd2JabmZldVA1WDJ2ZkVYRHZWbWJjMjJxVW8zOXo0UHUySUtlT3daTXo1Z2xxWmJEVFFucWlZM2F4a25nL0VQQTBwcGcxMlhFMmlYRVpNVjM5ZmN5cFNRQVZaRUJWQlNrb0kvQlRLYms2end5RWtWVkNyeURGeWhZU2JjdjlGNVlQbVhLYWlDUjVwODdwcVJnem9IcCtpaTRiQWVqdlFzV1R4ZEtMYW1lUU9xS2RWMGhzY1daTzQ4eFh6aUY5dXhwNFd3S2pFYUN5WWd5U21DekpXZ1NaTWRWckhaZEpRa2RNVHVYcThsMnJWSTlxay85N2gzVHU5NzBYZTM4b2JlWXk3MzVDU2VZSCsvMUZGTkFBQUJ4OU9ic3hPZTdOejd5RTErMHRQNlI3eHZ0ZWNVdDNQMmlNWnVOZVc3T1Z6SlpUdFdCejRGdW5RTFAzZ1hNTHhEMUdFZ1RJSEp6NHlYeml0MWNjUHgyd2NtUEVHc0hCTnNQQXNzN2ljbXl1YjkyYW1zaU5BUGFSWElodVJaSkxBeUw5U0JlTVcvRldsNXVaUkZCU1JCS2YySVlTUVdwS0t2YUxHTFdmalBJSUtPTjhMSEZXaFZRVG8zdkdtRGpMTEYxRHMzbUJjeG5tMUoyelI5UElOdTJDN1VobWltWVJreHJsNHZzdUFKVlBRSzdMVVVhNS9IcS9oRTI3NnFhaDkvMkh6WWUvS1h2QTNES1BJNTRWY1NuNTNvcUtxQmZSek53T0lHMzZFemtuOVhILy92L1NGZis3WDlUWGZLRm40dHRsNkxkT3Q5SjNxaGtaUmRrZFk5dzg1VHczTDNBL0J3aEk4Rm9LZHlhSUZXS2VpMUJNM0RoSWVMTUEwQzFKTmkyQzFqYlIweTJBYU5sa2ZGWVdTVkpJT2F6akxiTjBGUzIyWkJrREE1OTVZZjRjdVpDbUREY3BaOWJIU25tTHF1MFhZZWNLV1ZoZVFESFZCdmhub1NRRG1oYllIYkJkaGFiYndobVcwQzdCZFZXSU1sMkd4dU5pR3BpazZlYmdXMUQxQ3RNTzU0cHN1TWdVajBHMmswZ005ZVRuWFhpTE9YamIvbXpmTyt2ZlYvVGZQUzNUTEcvK2tuSmJIeWlsM3o4anp3RnJoc1AxN2p0U0FkZ1VxOTkzdDlQbDc3eWU2dGRMOXJicFRYbzdId0h6aXJVS3dJWmtWdW5oT2Z2QldjWERGRFZZOXRHV0gzckR6dEt3WUtBcmdXMEJTb1M0eldwdCs5bmQyYkt0Ly9PTjh2bnZlaWd6QlJvV2pBcnBKa3Jadk5zaGRNZVZxdnppd25TMDNLMlhNeUFaRVZPeG9LcUdtSG5kdUFEeHpieDRyLytxMGlYN2hGdE9nS1ZNRGZBOUFMUWJBRFRkV0srSWRER3lzNFNCYXlBcWdJU2FJb0Rhek5iUVNhd3RBTnArMldzdHUweGk1bTNWRlEwalhkVWs5U2s5dVE3enpTbjMvRXZ1N1AvL2Q4QjJNQ2hXeXNjdmJtVVZueTZyNmVIQXRwVlFTUjdXdTN5eWI0di9OK3EvUy83TzloeHc1Nk9BbTAyczBBRjlVVEFKTjE4Q213OEFHNmRzQUtHVkJIVkpOa0NhOS9DeTdmQkJ4S1JPeXRBbmM3d3ZPZnM1UE92T2NqbnZmZ3FlZVZMcjhMeThpN1p0bk1OQi9ldGNPZEtHVGlubk8xd2hjcC9kRUJ2cjg4VkR6eTBqdlZ6bTZoMGhqOTZ6MFB5M2QvL084VFNDR3liQVdmcDFFcFZLMUlsaU54eVdUTU5JK0cxc1gvVEtNbmFRYVRWU3lGTDI1SFlVcmlwQXRFMDJwSEdGYXA4NFFQbjg4bTN2WEhyK08vOEdJQzdMZUo2YWxpOTRmVjBVa0MvRGxXUU40Y2lYajIrN0RYL01PMjQ0Vy9KamhldHFsVFVabDJSRzJFYUpWUVRxTGJnMW1seTR6Z3dPMnZaa21vRXlNaVVVandSTEY3SkpBQTI1c0IwQmxRTjBIVkFHc251eTNmZ1dWZHN3Kzd0SzZoa0JKR2FxUUpsUEpHVUFFRmkxOHhFa2FGTkM3TERxZlVwUDNMM2VadzkxUkN6bVJuSzdTdGc1WnMwU1FLUWlKUjhRbmhZSERXQ3VUWENXck1nSldDeUU3THRNcVMxZlVpcFl1cm1vdHBtcVpkMU5LcEhkYXFSTDN6d0FzNzg4YzgzRDl6NlV3MXdoM1hvZFpWdEp2WFVzSHJENjJtb2dBQUFBUTRsNEZjOHE0L3JseTQ1OUErdzY0YS9JVHMvYXpYTEdIbDJvVU9laWlSSnJKYkFxaGEyTThyV0dlcnNiTUxXV2FDYjJoWWhhVXlra1FjSmlTbFZTQ25aN2k0S1lXN0JMaFB6VnRCa1MvdUpFeXpvMk5jYU9xOGp0WVVqb3pHeE1nWkdJMG1wSXJRVjFRNTl6VWNWUlFhd0xYZzdPM0dLOWdDTVZwSk1kZ0tyZXlITE95RDFNb1NaYU9jUU1LZHFrcXJSYWtKN0ZtbDY3SXllZTkrYnVnZmU5Sk5GOFE2OXJzTFJwNmJpeGZWMFZjQzRFbkJJZ0RjSHVmZmMwWUhYZmx2YWZjT2h5ZHBWbHpmVkFlUTh6Y3lieXR5SXBEb2hUYUFZaWFpQzgzUEM2V2xpZmdGczV3STJLS2t3aUNrbkV5R1ZTRXBJa2hoSHVVcXdkNUtOR0FjRmtqd2xaeWsra2xxcW9na0NYV1EvakFyUzFxcDltSUI2Q1JndE1ZMjNBY3M3UlpiV0tPTVZFVlNnTmhCdFZISldWbU9weDJ0VnhUbGs4MTdrcmZ2L3NEM3p2cDl2ei83Tzd3RTRCZ0NPODBxTzVhbDhQZDBWTUs0RTNDcVdVUUVBSE5pMTU0V3YwVDEvOWV1dzdacVhjL1ZxdExrQ2N1NklSdk84RTZaVVNSb0xxa3BVWStPZHJjUjJFMnhuWUxOT2RCdEc3TWJDb0Znc0xsNWlRd0VrUzZ5TXMzeGRRam5zejByRGdqVTAya1RFM1A5b1dWQ3ZRQ2JiZ2NrT3lHUWI2MVF6cFFwa0s4Z055VVlGMEZTdFZxbXVVNUlrcVRrRDNiam5oSzcvMlcvbDAzL3djN1BaUTM5WXBQQTBVcnk0bnU0S1dBcWIvREpGbEsvTlVTZTNncFVYNGNETHZpN3R1TzRtckZ4eEExYWVpWXdscUNYaXMrcGN0Y3VRaElSVVY0S0tLalVnQ1lRS3REV2FRMXRBTzRIT2lTNEx0SE9YT1lmYlB4V3h3NVFEMTRra3Nwb0lxaEdsbWdoU1JhbEdnbXBDU1pYVnVWTE5DdXFjdGJSS0ZVVTFnVlRqbEZKVlZWSkRadytCMDd2T1l2YmdiVGovMFZzM3o5NzJOZ0FQQUhBbHZ6bjVBZEJQRzhXTDYrbXVnSTkyR1Via3JZcCtiNWNSUnRlOFlIblhOVGR6OVZrM29ENzRzbXIxMHJFdUhZUmlETElGdGFGMmJZYk9IZmhES0VrTTAxVkFxdnhVaWVRN1JRK3laa2dxRW1zdTZSazFMeFNFUWxRdGwweWw1TTRUZFltMkUzV2RVajFKb3lTK051c3NNSHVZME9udHV2NkJQMm5QM2ZXcnpmcmIveFRBZmFWN043Nmh0ck9jbjFwUjdTZDYvVVZWd09HVmNPUGhoTnQrc0xzSWkxODlYdnY4NTNOeTVWK1hiVmZ0a05HMlY0MUc0MHZTNmlYSWszMGdhcWgyMEp6QmJIaU50dlpSWVJWYmh1ZjhWQ1FCUUJHa3FPZXJrMWRiSlNBbGtUUVNRbE5LSWlLbXlKSVMwRGFRN2pRNFAwRnR6MzZJc3pOdno1djN2TDg1OC9iL0JtemNBMkJxelJYZzBDOVhPSG9VVDFkcjkwalhYd1lGakV1QXc0SWJrWERiTGJtY090bGZlN1lCKzJUdkRWOU0yZm5NYXRlejk2YlJucGRtNWY2TWVseU5kMVl5MnA2a1dnSmtDWkNxcitzcmhjNEp5ZmIvUUd5MkpIa081Y3gyaHUwMkZUcnROTTgzSU9uOWJFL2ZwWnQzbmE2YXJkdWEwMjk3L3h3NEJXQmpvY205cGZzTG8zVEQ2eStUQWc0dnNaOURnaHV2RSt5L25qajZ0VGtLQXdmWE5nREx5OENvWG52T21tNS81bVdwYTY0bTh5VlNqU2FRYWtSRm5XUWttYUsrK2E2SWFLYzVkNnB0bGp4dGtPVmh3ZXdZdW9kUGJtN2VmeGJBRE1EcGoyMFNYT0hlQ3VBbUJZNThUSVArb2wxL1dSWHdrUzVYeWhzVGJuaU9ZTnRCNG0zLzFDdEV2RmJ2Q1htS3cwVE5ncHR1cWJEeGtPQmRaOVV0M0Y5NGhidjQrdjhCdm04a0FwYjd0VGtBQUFBQVNVVk9SSzVDWUlJPSIgYWx0PSJGYWNlYm9vayIgY2xhc3M9Im9wdC1pY29uLWltZyI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJvcHQtdGV4dCI+PGRpdiBjbGFzcz0ib3B0LXRpdGxlIj5GYWNlYm9vazwvZGl2PjxkaXYgY2xhc3M9Im9wdC1oYW5kbGUiPlImYW1wO0ogR3Jvb21pbmc8L2Rpdj48L2Rpdj4KICAgIDxzcGFuIGNsYXNzPSJvcHQtYXJyb3ciPuKGkjwvc3Bhbj4KICA8L2E+CiAgPGJ1dHRvbiBjbGFzcz0ib3B0IiBvbmNsaWNrPSJ3aW5kb3cubG9jYXRpb24uaHJlZj0ndGVsOiszNzI1ODczNTQ1NiciPgogICAgPGRpdiBjbGFzcz0ib3B0LWljb24iPjxzdmcgd2lkdGg9IjMyIiBoZWlnaHQ9IjMyIiB2aWV3Qm94PSIwIDAgMjQgMjQiIGZpbGw9Im5vbmUiIHN0cm9rZT0icmdiYSgyNTUsMjU1LDI1NSwuNDUpIiBzdHJva2Utd2lkdGg9IjEuNiI+PHBhdGggZD0iTTIyIDE2LjkydjNhMiAyIDAgMDEtMi4xOCAyIDE5Ljc5IDE5Ljc5IDAgMDEtOC42My0zLjA3QTE5LjUgMTkuNSAwIDAxMy4wNyA5LjgyYTE5Ljc5IDE5Ljc5IDAgMDEtMy4wNy04LjY3QTIgMiAwIDAxMiAxaDNhMiAyIDAgMDEyIDEuNzJjLjEyNy45Ni4zNjEgMS45MDMuNyAyLjgxYTIgMiAwIDAxLS40NSAyLjExTDYuOTEgOC45MWExNiAxNiAwIDAwNiA2bDEuMjctMS4yN2EyIDIgMCAwMTIuMTEtLjQ1Yy45MDcuMzM5IDEuODUuNTczIDIuODEuN0EyIDIgMCAwMTIyIDE2LjkyeiIvPjwvc3ZnPjwvZGl2PgogICAgPGRpdiBjbGFzcz0ib3B0LXRleHQiPjxkaXYgY2xhc3M9Im9wdC10aXRsZSIgZGF0YS1pMThuPSJjYWxsX3VzIj5DYWxsIFVzPC9kaXY+PGRpdiBjbGFzcz0ib3B0LWhhbmRsZSI+KzM3MiA1ODcgMzU0NTY8L2Rpdj48L2Rpdj4KICAgIDxzcGFuIGNsYXNzPSJvcHQtYXJyb3ciPuKGkjwvc3Bhbj4KICA8L2J1dHRvbj4KICA8ZGl2IGNsYXNzPSJob21lLWZvb3QiPgogICAgPHNwYW4+VGFsbGlubjwvc3Bhbj48ZGl2IGNsYXNzPSJmZG90Ij48L2Rpdj48c3Bhbj5Fc3RvbmlhPC9zcGFuPjxkaXYgY2xhc3M9ImZkb3QiPjwvZGl2PjxzcGFuPkFsbHZlZWxhZXZhIDQ8L3NwYW4+CiAgPC9kaXY+CjwvZGl2Pgo8L2Rpdj4KCjwhLS0gQk9PS0lORyAtLT4KPGRpdiBjbGFzcz0ic2NyZWVuIiBpZD0iYm9va1NjcmVlbiI+CjxkaXYgY2xhc3M9ImNvbiI+CiAgPGJ1dHRvbiBjbGFzcz0iYmFjay1idG4iIGlkPSJiYWNrQnRuIiBkYXRhLWkxOG49ImJhY2siPuKGkCDQndCw0LfQsNC0PC9idXR0b24+CiAgPGRpdiBjbGFzcz0ibG9nby1yaiI+UiZhbXA7SjwvZGl2PgogIDxkaXYgY2xhc3M9ImxvZ28tc3ViIiBkYXRhLWkxOG49ImxvZ29fc3ViIj5Hcm9vbWluZyDCtyDQotCw0LvQu9C40L08L2Rpdj4KICA8ZGl2IGNsYXNzPSJwcm9ncmVzcyI+CiAgICA8ZGl2IGNsYXNzPSJwcyBhY3RpdmUiIGlkPSJwczEiPjxkaXYgY2xhc3M9InBkb3QiPjwvZGl2PjxzcGFuIGRhdGEtaTE4bj0icHNfc2VydmljZSI+0KPRgdC70YPQs9CwPC9zcGFuPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icGwiIGlkPSJwbDEiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icHMiIGlkPSJwczIiPjxkaXYgY2xhc3M9InBkb3QiPjwvZGl2PjxzcGFuIGRhdGEtaTE4bj0icHNfbWFzdGVyIj7QnNCw0YHRgtC10YA8L3NwYW4+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwbCIgaWQ9InBsMiI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwcyIgaWQ9InBzMyI+PGRpdiBjbGFzcz0icGRvdCI+PC9kaXY+PHNwYW4gZGF0YS1pMThuPSJwc19wZXQiPtCf0LjRgtC+0LzQtdGGPC9zcGFuPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icGwiIGlkPSJwbDMiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icHMiIGlkPSJwczQiPjxkaXYgY2xhc3M9InBkb3QiPjwvZGl2PjxzcGFuIGRhdGEtaTE4bj0icHNfZGF0ZSI+0JTQsNGC0LA8L3NwYW4+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwbCIgaWQ9InBsNCI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwcyIgaWQ9InBzNSI+PGRpdiBjbGFzcz0icGRvdCI+PC9kaXY+PHNwYW4gZGF0YS1pMThuPSJwc19kZXRhaWxzIj7QlNCw0L3QvdGL0LU8L3NwYW4+PC9kaXY+CiAgPC9kaXY+CgogIDwhLS0gU3RlcCAxIC0tPgogIDxkaXYgY2xhc3M9InN0ZXAgc2hvdyIgaWQ9ImJrMSI+CiAgICA8ZGl2IGNsYXNzPSJzbGJsIiBkYXRhLWkxOG49InN0ZXAxX2xibCI+MDEgwrcg0J/QvtGA0L7QtNCwPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJid3JhcCI+CiAgICAgIDxkaXYgY2xhc3M9InNib3giPgogICAgICAgIDxzcGFuIGNsYXNzPSJzaSI+8J+UjTwvc3Bhbj4KICAgICAgICA8aW5wdXQgaWQ9ImJJbnB1dCIgdHlwZT0idGV4dCIgcGxhY2Vob2xkZXI9ItCd0LDRh9C90LjRgtC1INCy0LLQvtC00LjRgtGMINC/0L7RgNC+0LTRgy4uLiIgZGF0YS1pMThuLXBoPSJicmVlZF9waCIgYXV0b2NvbXBsZXRlPSJvZmYiPgogICAgICAgIDxidXR0b24gY2xhc3M9ImNsciIgaWQ9ImNsckJ0biI+4pyVPC9idXR0b24+CiAgICAgIDwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJkcm9wIiBpZD0iYkRyb3AiPjwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzYmFkZ2UiIGlkPSJzQmFkZ2UiPjwvZGl2PgogICAgPGRpdiBpZD0ic3ZjU2VjIiBzdHlsZT0iZGlzcGxheTpub25lO21hcmdpbi10b3A6MTZweCI+CiAgICAgIDxkaXYgY2xhc3M9InNsYmwiIGlkPSJzdGVwMkxibEVsIiBkYXRhLWkxOG49InN0ZXAyX2xibCI+MDIgwrcg0KPRgdC70YPQs9CwPC9kaXY+CiAgICAgIDxkaXYgaWQ9InN2Y0xpc3QiPjwvZGl2PgogICAgPC9kaXY+CiAgPC9kaXY+CgogIDwhLS0gU3RlcCAyIC0tPgogIDxkaXYgY2xhc3M9InN0ZXAiIGlkPSJiazIiPgogICAgPGRpdiBjbGFzcz0ic2xibCIgZGF0YS1pMThuPSJzdGVwMl9tYXN0ZXIiPtCS0YvQsdC10YDQuNGC0LUg0LzQsNGB0YLQtdGA0LA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im1hc3RlcnMiPgogICAgICA8ZGl2IGNsYXNzPSJtYnRuIiBkYXRhLW1hc3Rlcj0i0KLQsNGC0YzRj9C90LAiPjxkaXYgY2xhc3M9Im1uYW1lIj7QotCw0YLRjNGP0L3QsDwvZGl2PjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJtYnRuIiBkYXRhLW1hc3Rlcj0i0JDQu9C10LrRgdCw0L3QtNGA0LAiPjxkaXYgY2xhc3M9Im1uYW1lIj7QkNC70LXQutGB0LDQvdC00YDQsDwvZGl2PjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJtYnRuIiBkYXRhLW1hc3Rlcj0i0JrRgdC10L3QuNGPIj48ZGl2IGNsYXNzPSJtbmFtZSI+0JrRgdC10L3QuNGPPC9kaXY+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9Im1idG4iIGRhdGEtbWFzdGVyPSLQkNC90L3QsCI+PGRpdiBjbGFzcz0ibW5hbWUiPtCQ0L3QvdCwPC9kaXY+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9Im1idG4iIGRhdGEtbWFzdGVyPSLQkNC70LjRgdCwIj48ZGl2IGNsYXNzPSJtbmFtZSI+0JDQu9C40YHQsDwvZGl2PjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJtYnRuIiBkYXRhLW1hc3Rlcj0i0JrRgNC40YHRgtC40L3QsCI+PGRpdiBjbGFzcz0ibW5hbWUiPtCa0YDQuNGB0YLQuNC90LA8L2Rpdj48L2Rpdj4KICAgIDwvZGl2PgogIDwvZGl2PgoKICA8IS0tIFN0ZXAgMyAtLT4KICA8ZGl2IGNsYXNzPSJzdGVwIiBpZD0iYmszIj4KICAgIDxkaXYgY2xhc3M9InNsYmwiIGRhdGEtaTE4bj0ic3RlcDNfbGJsIj7QmtCw0Log0LTQsNCy0L3QviDQstGLINC/0L7RgdC10YnQsNC70Lgg0LPRgNGD0LzQuNC90LM/PC9kaXY+CiAgICA8YnV0dG9uIGNsYXNzPSJnYnRuIiBkYXRhLXZhbD0i0J/QtdGA0LLRi9C5INGA0LDQtyIgZGF0YS1pMThuPSJnMSI+0J/QtdGA0LLRi9C5INGA0LDQtzwvYnV0dG9uPgogICAgPGJ1dHRvbiBjbGFzcz0iZ2J0biIgZGF0YS12YWw9ItCe0YIgMSDQtNC+IDMg0LzQtdGB0Y/RhtC10LIiIGRhdGEtaTE4bj0iZzIiPtCe0YIgMSDQtNC+IDMg0LzQtdGB0Y/RhtC10LI8L2J1dHRvbj4KICAgIDxidXR0b24gY2xhc3M9ImdidG4iIGRhdGEtdmFsPSLQntGCIDMg0LTQviA2INC80LXRgdGP0YbQtdCyIiBkYXRhLWkxOG49ImczIj7QntGCIDMg0LTQviA2INC80LXRgdGP0YbQtdCyPC9idXR0b24+CiAgICA8YnV0dG9uIGNsYXNzPSJnYnRuIiBkYXRhLXZhbD0i0JHQvtC70LXQtSA2INC80LXRgdGP0YbQtdCyIiBkYXRhLWkxOG49Imc0Ij7QkdC+0LvQtdC1IDYg0LzQtdGB0Y/RhtC10LI8L2J1dHRvbj4KICA8L2Rpdj4KCiAgPCEtLSBTdGVwIDQgLS0+CiAgPGRpdiBjbGFzcz0ic3RlcCIgaWQ9ImJrNCI+CiAgICA8ZGl2IGNsYXNzPSJzbGJsIiBkYXRhLWkxOG49InN0ZXA0X2xibCI+0JLRi9Cx0LXRgNC40YLQtSDQtNCw0YLRgzwvZGl2PgogICAgPGRpdiBjbGFzcz0iY2FsLWgiPgogICAgICA8YnV0dG9uIGNsYXNzPSJjYWwtbiIgaWQ9InByZXZNIj4mIzgyNDk7PC9idXR0b24+CiAgICAgIDxkaXYgY2xhc3M9ImNhbC1tIiBpZD0iY2FsTSI+PC9kaXY+CiAgICAgIDxidXR0b24gY2xhc3M9ImNhbC1uIiBpZD0ibmV4dE0iPiYjODI1MDs8L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0iY2ciIGlkPSJjYWxHIj48L2Rpdj4KICAgIDxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDtnYXA6MjBweDthbGlnbi1pdGVtczpjZW50ZXI7bWFyZ2luLXRvcDoxMnB4O3BhZGRpbmctdG9wOjEycHg7Ym9yZGVyLXRvcDoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMSk7ZmxleC13cmFwOndyYXA7Ij48ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo4cHg7Ij48ZGl2IHN0eWxlPSJ3aWR0aDoxNnB4O2hlaWdodDoxNnB4O2JvcmRlci1yYWRpdXM6NTAlO2JhY2tncm91bmQ6cmdiYSg5MCwxODAsOTAsLjE1KTtib3JkZXI6MXB4IHNvbGlkICM1YWI0NWE7ZmxleC1zaHJpbms6MDsiPjwvZGl2PjxzcGFuIHN0eWxlPSJmb250LXNpemU6MXJlbTtjb2xvcjojZmZmZmZmO2xldHRlci1zcGFjaW5nOi4wM2VtOyIgZGF0YS1pMThuPSJjYWxfYXZhaWwiPtCV0YHRgtGMINGB0LLQvtCx0L7QtNC90L7QtSDQstGA0LXQvNGPPC9zcGFuPjwvZGl2PjxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweDsiPjxkaXYgc3R5bGU9IndpZHRoOjE2cHg7aGVpZ2h0OjE2cHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNCk7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xKTtmbGV4LXNocmluazowOyI+PC9kaXY+PHNwYW4gc3R5bGU9ImZvbnQtc2l6ZToxcmVtO2NvbG9yOiNmZmZmZmY7bGV0dGVyLXNwYWNpbmc6LjAzZW07IiBkYXRhLWkxOG49ImNhbF9ub25lIj7QodCy0L7QsdC+0LTQvdC+0LPQviDQstGA0LXQvNC10L3QuCDQvdC10YI8L3NwYW4+PC9kaXY+PC9kaXY+CiAgICA8ZGl2IGlkPSJ0aW1lU2VjIiBzdHlsZT0iZGlzcGxheTpub25lO21hcmdpbi10b3A6MTZweCI+CiAgICAgIDxkaXYgY2xhc3M9InNsYmwiIGRhdGEtaTE4bj0ic3RlcDRfdGltZSI+0JLRi9Cx0LXRgNC40YLQtSDQstGA0LXQvNGPPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9InRnIiBpZD0idGltZUciPjwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IHN0eWxlPSJtYXJnaW4tdG9wOjIwcHg7cGFkZGluZy10b3A6MTZweDtib3JkZXItdG9wOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wOCk7dGV4dC1hbGlnbjpjZW50ZXIiPgogICAgICA8YnV0dG9uIGlkPSJjYWxsYmFja0J0biIgY2xhc3M9ImNiay1idG4iPtCd0LUg0L3QsNGI0LvQuCDRg9C00L7QsdC90L7QtSDQstGA0LXQvNGPPyDihpI8L2J1dHRvbj4KICAgIDwvZGl2PgogIDwvZGl2PgoKICA8IS0tIFN0ZXAgNSAtLT4KICA8ZGl2IGNsYXNzPSJzdGVwIiBpZD0iYms1Ij4KICAgIDxkaXYgY2xhc3M9InNsYmwiIGRhdGEtaTE4bj0ic3RlcDVfbGJsIj7QktCw0YjQuCDQtNCw0L3QvdGL0LU8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImZnIj48bGFiZWwgY2xhc3M9ImZsIiBkYXRhLWkxOG49ImxibF9uYW1lIj7QmNC80Y88L2xhYmVsPjxpbnB1dCBjbGFzcz0iZmkiIGlkPSJjTmFtZSIgdHlwZT0idGV4dCIgcGxhY2Vob2xkZXI9ItCS0LDRiNC1INC40LzRjyIgZGF0YS1pMThuLXBoPSJwaF9uYW1lIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImZnIj48bGFiZWwgY2xhc3M9ImZsIiBkYXRhLWkxOG49ImxibF9waG9uZSI+0KLQtdC70LXRhNC+0L08L2xhYmVsPjxpbnB1dCBjbGFzcz0iZmkiIGlkPSJjUGhvbmUiIHR5cGU9InRlbCIgcGxhY2Vob2xkZXI9IiszNzIgLi4uIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImZnIj48bGFiZWwgY2xhc3M9ImZsIiBkYXRhLWkxOG49ImxibF9lbWFpbCI+RW1haWw8L2xhYmVsPjxpbnB1dCBjbGFzcz0iZmkiIGlkPSJjRW1haWwiIHR5cGU9ImVtYWlsIiBwbGFjZWhvbGRlcj0iZW1haWxAZXhhbXBsZS5jb20iPjwvZGl2PgogICAgPGRpdiBjbGFzcz0iZmciPjxsYWJlbCBjbGFzcz0iZmwiIGRhdGEtaTE4bj0ibGJsX3BldCI+0JrQu9C40YfQutCwINC/0LjRgtC+0LzRhtCwPC9sYWJlbD48aW5wdXQgY2xhc3M9ImZpIiBpZD0iY1BldCIgdHlwZT0idGV4dCIgcGxhY2Vob2xkZXI9ItCd0LXQvtCx0Y/Qt9Cw0YLQtdC70YzQvdC+IiBkYXRhLWkxOG4tcGg9InBoX29wdGlvbmFsIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InN1bSIgaWQ9InN1bUJsb2NrIj48L2Rpdj4KICAgIDxidXR0b24gY2xhc3M9ImNidG4iIGlkPSJjb25maXJtQnRuIiBkYXRhLWkxOG49ImNvbmZpcm1fYnRuIj7Qn9C+0LTRgtCy0LXRgNC00LjRgtGMINC30LDQv9C40YHRjDwvYnV0dG9uPgogIDwvZGl2PgoKICA8IS0tIFN1Y2Nlc3MgLS0+CiAgPGRpdiBjbGFzcz0ic2Jsb2NrIiBpZD0ic3VjQmxvY2siPgogICAgPGRpdiBjbGFzcz0ic2kyIj7wn5C+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzdCIgZGF0YS1pMThuPSJzdWNjZXNzX3RpdGxlIj7Ql9Cw0L/QuNGB0Ywg0L/RgNC40L3Rj9GC0LAhPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzcyIgZGF0YS1pMThuPSJzdWNjZXNzX3N1YiI+0JzRiyDRgdCy0Y/QttC10LzRgdGPINGBINCy0LDQvNC4INC00LvRjyDQv9C+0LTRgtCy0LXRgNC20LTQtdC90LjRjy48YnI+0KHQv9Cw0YHQuNCx0L4sINGH0YLQviDQstGL0LHRgNCw0LvQuCBSJkogR3Jvb21pbmchPC9kaXY+CiAgICA8YnV0dG9uIGNsYXNzPSJoYnRuIiBpZD0iaG9tZUJ0biIgZGF0YS1pMThuPSJ0b19ob21lIj7ihpAg0J3QsCDQs9C70LDQstC90YPRjjwvYnV0dG9uPgogIDwvZGl2Pgo8L2Rpdj4KPC9kaXY+Cgo8ZGl2IGlkPSJjYmtNb2RhbCIgc3R5bGU9ImRpc3BsYXk6bm9uZTtwb3NpdGlvbjpmaXhlZDtpbnNldDowO2JhY2tncm91bmQ6cmdiYSgwLDAsMCwuNzUpO3otaW5kZXg6MzAwO2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO3BhZGRpbmc6MjBweCI+CiAgPGRpdiBzdHlsZT0iYmFja2dyb3VuZDojMGEwYTBhO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTIpO2JvcmRlci10b3A6MXB4IHNvbGlkICNmZmZmZmY7cGFkZGluZzoyOHB4IDI0cHg7d2lkdGg6MTAwJTttYXgtd2lkdGg6MzYwcHgiPgogICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjAuODM4cmVtO2xldHRlci1zcGFjaW5nOi4yZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbToxNnB4O2ZvbnQtd2VpZ2h0OjYwMDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZiI+0J7QsdGA0LDRgtC90YvQuSDQt9Cy0L7QvdC+0Lo8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImZnIj48bGFiZWwgY2xhc3M9ImZsIj7QmNC80Y88L2xhYmVsPjxpbnB1dCBjbGFzcz0iZmkiIGlkPSJjYmtOYW1lIiB0eXBlPSJ0ZXh0IiBwbGFjZWhvbGRlcj0i0JLQsNGI0LUg0LjQvNGPIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImZnIj4KICAgICAgPGxhYmVsIGNsYXNzPSJmbCI+0KLQtdC70LXRhNC+0L08L2xhYmVsPgogICAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6c3RyZXRjaDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xNSkiPgogICAgICAgIDxzcGFuIHN0eWxlPSJwYWRkaW5nOjEwcHggMTBweCAxMHB4IDA7Y29sb3I6I2ZmZmZmZjtmb250LXNpemU6MS4zNjNyZW07Ym9yZGVyLXJpZ2h0OjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xKTttYXJnaW4tcmlnaHQ6MTBweDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZiI+KzM3Mjwvc3Bhbj4KICAgICAgICA8aW5wdXQgaWQ9ImNia1Bob25lIiB0eXBlPSJ0ZWwiIHBsYWNlaG9sZGVyPSJYWFhYWFhYWCIgc3R5bGU9ImZsZXg6MTtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjpub25lO291dGxpbmU6bm9uZTtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjEuNDM4cmVtO2NvbG9yOiNmZmZmZmY7cGFkZGluZzoxMHB4IDAiPgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBpZD0iY2JrU3VjY2VzcyIgc3R5bGU9ImRpc3BsYXk6bm9uZTt0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjIwcHggMCI+CiAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToyLjg3NXJlbTttYXJnaW4tYm90dG9tOjEwcHg7b3BhY2l0eTouNSI+4pyTPC9kaXY+CiAgICAgIDxkaXYgc3R5bGU9ImZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6MS44NzVyZW07Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjZweCI+0JfQsNGP0LLQutCwINC/0YDQuNC90Y/RgtCwITwvZGl2PgogICAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6MS4wMzdyZW07Y29sb3I6I2ZmZmZmZjtsaW5lLWhlaWdodDoxLjY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWYiPtCc0Ysg0L/QtdGA0LXQt9Cy0L7QvdC40Lwg0LLQsNC8INCyINCx0LvQuNC20LDQudGI0LXQtSDQstGA0LXQvNGPPC9kaXY+CiAgICA8L2Rpdj4KICAgIDxidXR0b24gaWQ9ImNia1N1Ym1pdCIgY2xhc3M9ImNidG4iIHN0eWxlPSJtYXJnaW4tdG9wOjE0cHgiPtCe0YLQv9GA0LDQstC40YLRjDwvYnV0dG9uPgogICAgPGJ1dHRvbiBpZD0iY2JrQ2xvc2UiIHN0eWxlPSJkaXNwbGF5OmJsb2NrO3dpZHRoOjEwMCU7bWFyZ2luLXRvcDo4cHg7YmFja2dyb3VuZDpub25lO2JvcmRlcjpub25lO2NvbG9yOiNmZmZmZmY7Zm9udC1zaXplOjAuODM4cmVtO2xldHRlci1zcGFjaW5nOi4xMmVtO2N1cnNvcjpwb2ludGVyO3BhZGRpbmc6OHB4O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmIj7QntGC0LzQtdC90LA8L2J1dHRvbj4KICA8L2Rpdj4KPC9kaXY+Cgo8c2NyaXB0Pgp2YXIgREFUQSA9IFt7ImJyZWVkIjoi0JDQstGB0YLRgNCw0LvQuNC50YHQutCw0Y8g0L7QstGH0LDRgNC60LAgMTXigJMyNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6ODV9LCJicmVlZF9lbiI6IkF1c3RyYWxpYW4gU2hlcGhlcmQgMTXigJMyNSBrZyIsImJyZWVkX2V0IjoiQXVzdHJhYWxpYSBsYW1iYWtvZXIgMTXigJMyNSBrZyJ9LHsiYnJlZWQiOiLQkNCy0YHRgtGA0LDQu9C40LnRgdC60LDRjyDQvtCy0YfQsNGA0LrQsCAyNeKAkzM1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IkF1c3RyYWxpYW4gU2hlcGhlcmQgMjXigJMzNSBrZyIsImJyZWVkX2V0IjoiQXVzdHJhYWxpYSBsYW1iYWtvZXIgMjXigJMzNSBrZyJ9LHsiYnJlZWQiOiLQkNC60LjRgtCwLdC40L3RgyAyMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJBa2l0YSBJbnUgMjDigJM0MCBrZyIsImJyZWVkX2V0IjoiQWtpdGEgSW51IDIw4oCTNDAga2cifSx7ImJyZWVkIjoi0JDQutC40YLQsC3QuNC90YMg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkFraXRhIEludSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJBa2l0YSBJbnUgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQkNC60LjRgtCwLdC40L3RgyDRhNC70LDRhNGE0LggMjDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQWtpdGEgSW51IGZsdWZmeSAyMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBa2l0YSBJbnUgcGVobWVrYXJ2YWxpbmUgMjDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC60LjRgtCwLdC40L3RgyDRhNC70LDRhNGE0Lgg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkFraXRhIEludSBmbHVmZnkgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiQWtpdGEgSW51IHBlaG1la2FydmFsaW5lIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JDQu9Cw0LHQsNC5IDQw4oCTNjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJDZW50cmFsIEFzaWFuIFNoZXBoZXJkIDQw4oCTNjAga2ciLCJicmVlZF9ldCI6Iktlc2stQWFzaWEgbGFtYmFrb2VyIDQw4oCTNjAga2cifSx7ImJyZWVkIjoi0JDQu9Cw0LHQsNC5INCx0L7Qu9C10LUgNjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoxMDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjoxMTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMzB9LCJicmVlZF9lbiI6IkNlbnRyYWwgQXNpYW4gU2hlcGhlcmQgb3ZlciA2MCBrZyIsImJyZWVkX2V0IjoiS2Vzay1BYXNpYSBsYW1iYWtvZXIgw7xsZSA2MCBrZyJ9LHsiYnJlZWQiOiLQkNC70Y/RgdC60LjQvdGB0LrQuNC5INC80LDQu9Cw0LzRg9GCIDIw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkFsYXNrYW4gTWFsYW11dGUgMjDigJM0MCBrZyIsImJyZWVkX2V0IjoiQWxhc2thIG1hbGFtdXV0IDIw4oCTNDAga2cifSx7ImJyZWVkIjoi0JDQu9GP0YHQutC40L3RgdC60LjQuSDQvNCw0LvQsNC80YPRgiDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQWxhc2thbiBNYWxhbXV0ZSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJBbGFza2EgbWFsYW11dXQgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQkNC70Y/RgdC60LjQvdGB0LrQuNC5INC80LDQu9Cw0LzRg9GCINGE0LvQsNGE0YTQuCAyMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJBbGFza2FuIE1hbGFtdXRlIGZsdWZmeSAyMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBbGFza2EgbWFsYW11dXQgcGVobWVrYXJ2YWxpbmUgMjDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC70Y/RgdC60LjQvdGB0LrQuNC5INC80LDQu9Cw0LzRg9GCINGE0LvQsNGE0YTQuCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQWxhc2thbiBNYWxhbXV0ZSBmbHVmZnkgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiQWxhc2thIG1hbGFtdXV0IHBlaG1la2FydmFsaW5lIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JDQvNC10YDQuNC60LDQvdGB0LrQsNGPINCw0LrQuNGC0LAgMjDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQW1lcmljYW4gQWtpdGEgMjDigJM0MCBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2EgQWtpdGEgMjDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutCw0Y8g0LDQutC40YLQsCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQW1lcmljYW4gQWtpdGEgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2EgQWtpdGEgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutCw0Y8g0LDQutC40YLQsCDRhNC70LDRhNGE0LggMjDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQW1lcmljYW4gQWtpdGEgZmx1ZmZ5IDIw4oCTNDAga2ciLCJicmVlZF9ldCI6IkFtZWVyaWthIEFraXRhIHBlaG1la2FydmFsaW5lIDIw4oCTNDAga2cifSx7ImJyZWVkIjoi0JDQvNC10YDQuNC60LDQvdGB0LrQsNGPINCw0LrQuNGC0LAg0YTQu9Cw0YTRhNC4INCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBBa2l0YSBmbHVmZnkgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2EgQWtpdGEgcGVobWVrYXJ2YWxpbmUgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutC40Lkg0LrQvtC60LXRgC3RgdC/0LDQvdC40LXQu9GMIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MH0sImJyZWVkX2VuIjoiQW1lcmljYW4gQ29ja2VyIFNwYW5pZWwgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2Ega29rZXJzcGFuamVsIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0JDQvNC10YDQuNC60LDQvdGB0LrQuNC5INC60L7QutC10YAt0YHQv9Cw0L3QuNC10LvRjCAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODB9LCJicmVlZF9lbiI6IkFtZXJpY2FuIENvY2tlciBTcGFuaWVsIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IkFtZWVyaWthIGtva2Vyc3BhbmplbCAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LjQuSDRgdGC0LDRhNGE0L7RgNC00YjQuNGA0YHQutC40Lkg0YLQtdGA0YzQtdGAIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBTdGFmZm9yZHNoaXJlIFRlcnJpZXIgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2EgU3RhZmZvcmRzaGlyZSB0ZXJqZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutC40Lkg0YHRgtCw0YTRhNC+0YDQtNGI0LjRgNGB0LrQuNC5INGC0LXRgNGM0LXRgCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NX0sImJyZWVkX2VuIjoiQW1lcmljYW4gU3RhZmZvcmRzaGlyZSBUZXJyaWVyIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkFtZWVyaWthIFN0YWZmb3Jkc2hpcmUgdGVyamVyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JDQvdCz0LvQuNC50YHQutC40Lkg0LHRg9C70YzQtNC+0LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjcwfSwiYnJlZWRfZW4iOiJFbmdsaXNoIEJ1bGxkb2ciLCJicmVlZF9ldCI6IkluZ2xpc2UgYnVsZG9nIn0seyJicmVlZCI6ItCQ0L3Qs9C70LjQudGB0LrQuNC5INC60L7QutC10YAt0YHQv9Cw0L3QuNC10LvRjCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IkVuZ2xpc2ggQ29ja2VyIFNwYW5pZWwgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiSW5nbGlzZSBrb2tlcnNwYW5qZWwgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQkNC90LPQu9C40LnRgdC60LjQuSDQutC+0LrQtdGALdGB0L/QsNC90LjQtdC70YwgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwfSwiYnJlZWRfZW4iOiJFbmdsaXNoIENvY2tlciBTcGFuaWVsIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IkluZ2xpc2Uga29rZXJzcGFuamVsIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0JDRhNCz0LDQvSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTB9LCJicmVlZF9lbiI6IkFmZ2hhbiBIb3VuZCAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJBZmdhbmlzdGFuaSBrb2VyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JDRhNCz0LDQvSAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwfSwiYnJlZWRfZW4iOiJBZmdoYW4gSG91bmQgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiQWZnYW5pc3Rhbmkga29lciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCR0LDRgdGB0LXRgi3RhdCw0YPQvdC0IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJCYXNzZXQgSG91bmQgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiQmFzc2V0aG91bmQgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQkdCw0YHRgdC10YIt0YXQsNGD0L3QtCAzMOKAkzM1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NX0sImJyZWVkX2VuIjoiQmFzc2V0IEhvdW5kIDMw4oCTMzUga2ciLCJicmVlZF9ldCI6IkJhc3NldGhvdW5kIDMw4oCTMzUga2cifSx7ImJyZWVkIjoi0JHQtdGA0L3RgdC60LjQuSDQt9C10L3QvdC10L3RhdGD0L3QtCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTEwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJCZXJuZXNlIE1vdW50YWluIERvZyAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJCZXJuaSBtw6RnaWtvZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkdC10YDQvdGB0LrQuNC5INC30LXQvdC90LXQvdGF0YPQvdC0INCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMzAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkJlcm5lc2UgTW91bnRhaW4gRG9nIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkJlcm5pIG3DpGdpa29lciDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCR0LjQstC10YAt0LnQvtGA0Log0LHQvtC70LXQtSAzLDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiQmlld2VyIFlvcmtzaGlyZSBUZXJyaWVyIG92ZXIgMyw1IGtnIiwiYnJlZWRfZXQiOiJCaWV3ZXIgWW9ya3NoaXJlIFRlcnJpZXIgw7xsZSAzLDUga2cifSx7ImJyZWVkIjoi0JHQuNCy0LXRgC3QudC+0YDQuiDQtNC+IDMsNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJCaWV3ZXIgWW9ya3NoaXJlIFRlcnJpZXIgdXAgdG8gMyw1IGtnIiwiYnJlZWRfZXQiOiJCaWV3ZXIgWW9ya3NoaXJlIFRlcnJpZXIga3VuaSAzLDUga2cifSx7ImJyZWVkIjoi0JHQuNCz0LvRjCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjYwfSwiYnJlZWRfZW4iOiJCZWFnbGUgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiQmlpZ2VsIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0JHQuNCz0LvRjCAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjY1fSwiYnJlZWRfZW4iOiJCZWFnbGUgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiQmlpZ2VsIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0JHQuNGI0L7QvS3RhNGA0LjQt9C1IDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJCaWNob24gRnJpc8OpIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiQmnFoW9uIEZyaXPDqSA14oCTMTAga2cifSx7ImJyZWVkIjoi0JHQuNGI0L7QvS3RhNGA0LjQt9C1INC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJCaWNob24gRnJpc8OpIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IkJpxaFvbiBGcmlzw6kga3VuaSA1IGtnIn0seyJicmVlZCI6ItCR0L7QutGB0LXRgCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MH0sImJyZWVkX2VuIjoiQm94ZXIgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiQm9rc2VyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JHQvtC60YHQtdGAIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJCb3hlciAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJCb2tzZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkdC+0YDQtNC10YAt0LrQvtC70LvQuCAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4MH0sImJyZWVkX2VuIjoiQm9yZGVyIENvbGxpZSAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiJCb3JkZXJrb2xsIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0JHQvtGA0LTQtdGALdC60L7Qu9C70LggMjDigJMyNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJCb3JkZXIgQ29sbGllIDIw4oCTMjUga2ciLCJicmVlZF9ldCI6IkJvcmRlcmtvbGwgMjDigJMyNSBrZyJ9LHsiYnJlZWQiOiLQkdC+0YHRgtC+0L0t0YLQtdGA0YzQtdGAIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1fSwiYnJlZWRfZW4iOiJCb3N0b24gVGVycmllciAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJCb3N0b25pIHRlcmplciAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCR0L7RgdGC0L7QvS3RgtC10YDRjNC10YAgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MH0sImJyZWVkX2VuIjoiQm9zdG9uIFRlcnJpZXIgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJCb3N0b25pIHRlcmplciA14oCTMTAga2cifSx7ImJyZWVkIjoi0JHRgNCw0LHQsNC90YHQvtC9Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo1MH0sImJyZWVkX2VuIjoiR3JpZmZvbiBCcnV4ZWxsb2lzIiwiYnJlZWRfZXQiOiJCcsO8c3NlbGkgZ3JpZm9uIn0seyJicmVlZCI6ItCR0YPQu9GM0YLQtdGA0YzQtdGAIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwfSwiYnJlZWRfZW4iOiJCdWxsIFRlcnJpZXIgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiQnVsbHRlcmplciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCS0LXQu9GM0Ygt0LrQvtGA0LPQuCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjY1fSwiYnJlZWRfZW4iOiJXZWxzaCBDb3JnaSAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJXYWxlc2kga29yZ2kgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQktC10LvRjNGILdC60L7RgNCz0LggMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo3NX0sImJyZWVkX2VuIjoiV2Vsc2ggQ29yZ2kgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiV2FsZXNpIGtvcmdpIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0JLQtdGB0YIt0YXQsNC50LvQtdC90LQt0LLQsNC50YIt0YLQtdGA0YzQtdGAIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiV2VzdCBIaWdobGFuZCBXaGl0ZSBUZXJyaWVyIiwiYnJlZWRfZXQiOiJMw6TDpG5lLcWgb3RpbWFhIHZhbGdlIHRlcmplciJ9LHsiYnJlZWQiOiLQktC+0YHRgtC+0YfQvdC+0YHQuNCx0LjRgNGB0LrQsNGPINC70LDQudC60LAgMTjigJMyNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4NX0sImJyZWVkX2VuIjoiRWFzdCBTaWJlcmlhbiBMYWlrYSAxOOKAkzI1IGtnIiwiYnJlZWRfZXQiOiJJZGEtU2liZXJpIGxhaWthIDE44oCTMjUga2cifSx7ImJyZWVkIjoi0JLQvtGB0YLQvtGH0L3QvtGB0LjQsdC40YDRgdC60LDRjyDQu9Cw0LnQutCwINCx0L7Qu9C10LUgMjUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJFYXN0IFNpYmVyaWFuIExhaWthIG92ZXIgMjUga2ciLCJicmVlZF9ldCI6IklkYS1TaWJlcmkgbGFpa2Egw7xsZSAyNSBrZyJ9LHsiYnJlZWQiOiLQk9C+0LvQtNC10L0t0YDQtdGC0YDQuNCy0LXRgCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkdvbGRlbiBSZXRyaWV2ZXIgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiS3VsZG5lIHJldHJpaXZlciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCT0L7Qu9C00LXQvS3RgNC10YLRgNC40LLQtdGAIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IkdvbGRlbiBSZXRyaWV2ZXIgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiS3VsZG5lIHJldHJpaXZlciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCT0YDQuNGE0YTQvtC9Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiR3JpZmZvbiIsImJyZWVkX2V0IjoiR3JpZm9uIn0seyJicmVlZCI6ItCU0LDQu9C80LDRgtC40L0iLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjcwfSwiYnJlZWRfZW4iOiJEYWxtYXRpYW4iLCJicmVlZF9ldCI6IkRhbG1hYXRzaWEga29lciJ9LHsiYnJlZWQiOiLQlNC20LXQui3RgNCw0YHRgdC10Lst0YLQtdGA0YzQtdGAINCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdGL0LkiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjUwfSwiYnJlZWRfZW4iOiJKYWNrIFJ1c3NlbGwgVGVycmllciBzbW9vdGgiLCJicmVlZF9ldCI6IkphY2sgUnVzc2VsbGkgdGVyamVyIGzDvGhpa2FydmFsaW5lIn0seyJicmVlZCI6ItCU0LbQtdC6LdGA0LDRgdGB0LXQuy3RgtC10YDRjNC10YAg0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90YvQuSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MCwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6IkphY2sgUnVzc2VsbCBUZXJyaWVyIHdpcmUtaGFpcmVkIiwiYnJlZWRfZXQiOiJKYWNrIFJ1c3NlbGxpIHRlcmplciBrYXJ1a2FydmFsaW5lIn0seyJicmVlZCI6ItCU0L7QsdC10YDQvNCw0L0gMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjU1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4MH0sImJyZWVkX2VuIjoiRG9iZXJtYW5uIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkRvYmVybWFubiAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCU0L7QsdC10YDQvNCw0L0g0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5NX0sImJyZWVkX2VuIjoiRG9iZXJtYW5uIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkRvYmVybWFubiDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCX0LDQv9Cw0LTQvdC+0YHQuNCx0LjRgNGB0LrQsNGPINC70LDQudC60LAgMTjigJMyNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4NX0sImJyZWVkX2VuIjoiV2VzdCBTaWJlcmlhbiBMYWlrYSAxOOKAkzI1IGtnIiwiYnJlZWRfZXQiOiJMw6TDpG5lLVNpYmVyaSBsYWlrYSAxOOKAkzI1IGtnIn0seyJicmVlZCI6ItCX0L7Qu9C+0YLQuNGB0YLRi9C5INGA0LXRgtGA0LjQstC10YAgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkdvbGRlbiBSZXRyaWV2ZXIgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiS3VsZG5lIHJldHJpaXZlciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCX0L7Qu9C+0YLQuNGB0YLRi9C5INGA0LXRgtGA0LjQstC10YAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjY1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjExMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExMH0sImJyZWVkX2VuIjoiR29sZGVuIFJldHJpZXZlciAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJLdWxkbmUgcmV0cmlpdmVyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JjRgNC70LDQvdC00YHQutC40Lkg0LzRj9Cz0LrQvtGI0LXRgNGB0YLQvdGL0Lkg0L/RiNC10L3QuNGH0L3Ri9C5INGC0LXRgNGM0LXRgCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4MH0sImJyZWVkX2VuIjoiSXJpc2ggU29mdCBDb2F0ZWQgV2hlYXRlbiBUZXJyaWVyIiwiYnJlZWRfZXQiOiJJaXJpIHBlaG1la2FydmFuZSBuaXN1dsOkcnZpIHRlcmplciJ9LHsiYnJlZWQiOiLQmNGA0LvQsNC90LTRgdC60LjQuSDRgtC10YDRjNC10YAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzAsItCi0YDQuNC80LzQuNC90LMiOjc1fSwiYnJlZWRfZW4iOiJJcmlzaCBUZXJyaWVyIiwiYnJlZWRfZXQiOiJJaXJpIHRlcmplciJ9LHsiYnJlZWQiOiLQmNGB0L/QsNC90YHQutC40Lkg0LPQsNC70YzQs9C+IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NzB9LCJicmVlZF9lbiI6IlNwYW5pc2ggR2FsZ28gMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiSGlzcGFhbmlhIGdhbGdvIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JjRgdC/0LDQvdGB0LrQuNC5INCz0LDQu9GM0LPQviAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjgwfSwiYnJlZWRfZW4iOiJTcGFuaXNoIEdhbGdvIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6Ikhpc3BhYW5pYSBnYWxnbyAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCZ0L7RgNC60YjQuNGA0YHQutC40Lkg0YLQtdGA0YzQtdGAINCx0L7Qu9C10LUgMyw1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IllvcmtzaGlyZSBUZXJyaWVyIG92ZXIgMyw1IGtnIiwiYnJlZWRfZXQiOiJZb3Jrc2hpcmUgdGVyamVyIMO8bGUgMyw1IGtnIn0seyJicmVlZCI6ItCZ0L7RgNC60YjQuNGA0YHQutC40Lkg0YLQtdGA0YzQtdGAINC00L4gMyw1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IllvcmtzaGlyZSBUZXJyaWVyIHVwIHRvIDMsNSBrZyIsImJyZWVkX2V0IjoiWW9ya3NoaXJlIHRlcmplciBrdW5pIDMsNSBrZyJ9LHsiYnJlZWQiOiLQmtCw0LLQsNC70LXRgC3QutC40L3Qsy3Rh9Cw0YDQu9GM0Lct0YHQv9Cw0L3QuNC10LvRjCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IkNhdmFsaWVyIEtpbmcgQ2hhcmxlcyBTcGFuaWVsIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IkNhdmFsaWVyIEtpbmcgQ2hhcmxlcyBTcGFuaWVsIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0JrQsNCy0LDQu9C10YAt0LrQuNC90LMt0YfQsNGA0LvRjNC3LdGB0L/QsNC90LjQtdC70YwgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkNhdmFsaWVyIEtpbmcgQ2hhcmxlcyBTcGFuaWVsIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiQ2F2YWxpZXIgS2luZyBDaGFybGVzIFNwYW5pZWwgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCa0LDQvdC1LdC60L7RgNGB0L4gNDDigJM2MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODV9LCJicmVlZF9lbiI6IkNhbmUgQ29yc28gNDDigJM2MCBrZyIsImJyZWVkX2V0IjoiQ2FuZSBDb3JzbyA0MOKAkzYwIGtnIn0seyJicmVlZCI6ItCa0LDQvdC1LdC60L7RgNGB0L4g0LHQvtC70LXQtSA2MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjkwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MTA1fSwiYnJlZWRfZW4iOiJDYW5lIENvcnNvIG92ZXIgNjAga2ciLCJicmVlZF9ldCI6IkNhbmUgQ29yc28gw7xsZSA2MCBrZyJ9LHsiYnJlZWQiOiLQmtCw0YDQtdC70L4t0YTQuNC90YHQutCw0Y8g0LvQsNC50LrQsCDQtNC+IDEzINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjY1fSwiYnJlZWRfZW4iOiJLYXJlbGlhbi1GaW5uaXNoIExhaWthIHVwIHRvIDEzIGtnIiwiYnJlZWRfZXQiOiJLYXJqYWxhLVNvb21lIGxhaWthIGt1bmkgMTMga2cifSx7ImJyZWVkIjoi0JrQuNGC0LDQudGB0LrQsNGPINGF0L7RhdC70LDRgtCw0Y8g0LPQvtC70LDRjyA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMiwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQyLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiQ2hpbmVzZSBDcmVzdGVkIGhhaXJsZXNzIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiSGlpbmEgaGFyamFrb2VyIGthcnZhdHUgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCa0LjRgtCw0LnRgdC60LDRjyDRhdC+0YXQu9Cw0YLQsNGPINCz0L7Qu9Cw0Y8g0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MjgsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjozNSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkNoaW5lc2UgQ3Jlc3RlZCBoYWlybGVzcyB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJIaWluYSBoYXJqYWtvZXIga2FydmF0dSBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JrQuNGC0LDQudGB0LrQsNGPINGF0L7RhdC70LDRgtCw0Y8g0L/Rg9GF0L7QstCw0Y8gNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkNoaW5lc2UgQ3Jlc3RlZCBwb3dkZXJwdWZmIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiSGlpbmEgaGFyamFrb2VyIFBvd2RlcnB1ZmYgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCa0LjRgtCw0LnRgdC60LDRjyDRhdC+0YXQu9Cw0YLQsNGPINC/0YPRhdC+0LLQsNGPINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJDaGluZXNlIENyZXN0ZWQgcG93ZGVycHVmZiB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJIaWluYSBoYXJqYWtvZXIgUG93ZGVycHVmZiBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JrQvtC60LDQv9GDIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjc1fSwiYnJlZWRfZW4iOiJDb2NrYXBvbyA14oCTMTAga2ciLCJicmVlZF9ldCI6IkNvY2thcG9vIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQmtC+0LrQsNC/0YMg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjV9LCJicmVlZF9lbiI6IkNvY2thcG9vIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IkNvY2thcG9vIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQmtC+0LvQu9C4IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQ29sbGllIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IktvbGwgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQmtC+0LvQu9C4IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IkNvbGxpZSAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJLb2xsIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JrQvtC80L7QvdC00L7RgCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTEwfSwiYnJlZWRfZW4iOiJLb21vbmRvciAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJLb21vbmRvciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCa0L7QvNC+0L3QtNC+0YAg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEzMH0sImJyZWVkX2VuIjoiS29tb25kb3Igb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiS29tb25kb3Igw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQm9Cw0LHRgNCw0LTQvtGAINCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdGL0LkgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo3MH0sImJyZWVkX2VuIjoiTGFicmFkb3IgUmV0cmlldmVyIHNtb290aCAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvcmkgcmV0cmlpdmVyIGzDvGhpa2FydmFsaW5lIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00L7RgCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3Ri9C5IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6ODB9LCJicmVlZF9lbiI6IkxhYnJhZG9yIFJldHJpZXZlciBzbW9vdGggMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiTGFicmFkb3JpIHJldHJpaXZlciBsw7xoaWthcnZhbGluZSAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNC+0YAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjk1fSwiYnJlZWRfZW4iOiJMYWJyYWRvciBSZXRyaWV2ZXIgc21vb3RoIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9yaSByZXRyaWl2ZXIgbMO8aGlrYXJ2YWxpbmUgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQm9Cw0LHRgNCw0LTQvtGAINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdGL0LkgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJMYWJyYWRvciBSZXRyaWV2ZXIgbG9uZy1jb2F0ZWQgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiTGFicmFkb3JpIHJldHJpaXZlciBwaWtrYXJ2YWxpbmUgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQm9Cw0LHRgNCw0LTQvtGAINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdGL0LkgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjExMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiTGFicmFkb3IgUmV0cmlldmVyIGxvbmctY29hdGVkIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9yaSByZXRyaWl2ZXIgcGlra2FydmFsaW5lIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00L7RgCDQtNC70LjQvdC90L7RiNC10YDRgdGC0L3Ri9C5INCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMzAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkxhYnJhZG9yIFJldHJpZXZlciBsb25nLWNvYXRlZCBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvcmkgcmV0cmlpdmVyIHBpa2thcnZhbGluZSDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNGD0LTQtdC70YwgMTDigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJMYWJyYWRvb2RsZSAxMOKAkzIwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvb2RsZSAxMOKAkzIwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNGD0LTQtdC70YwgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwfSwiYnJlZWRfZW4iOiJMYWJyYWRvb2RsZSAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvb2RsZSAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNGD0LTQtdC70YwgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMH0sImJyZWVkX2VuIjoiTGFicmFkb29kbGUgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiTGFicmFkb29kbGUgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQm9C10LLRgNC10YLQutCwIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDB9LCJicmVlZF9lbiI6Ikl0YWxpYW4gR3JleWhvdW5kIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiSXRhYWxpYSB2aW5ka29lciA14oCTMTAga2cifSx7ImJyZWVkIjoi0JvQtdCy0YDQtdGC0LrQsCDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1fSwiYnJlZWRfZW4iOiJJdGFsaWFuIEdyZXlob3VuZCB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJJdGFhbGlhIHZpbmRrb2VyIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQm9GF0LDRgdGB0LrQuNC5INCw0L/RgdC+IDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjc1fSwiYnJlZWRfZW4iOiJMaGFzYSBBcHNvIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiTGhhc2EgQXBzbyA14oCTMTAga2cifSx7ImJyZWVkIjoi0JvRhdCw0YHRgdC60LjQuSDQsNC/0YHQviDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2NX0sImJyZWVkX2VuIjoiTGhhc2EgQXBzbyB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJMaGFzYSBBcHNvIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQnNCw0LvRjNGC0LXQt9C1Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJNYWx0ZXNlIiwiYnJlZWRfZXQiOiJNYWx0YSBib2xvbmVlcyJ9LHsiYnJlZWQiOiLQnNCw0LvRjNGC0LjQudGB0LrQsNGPINCx0L7Qu9C+0L3QutCwIDXigJM4INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjV9LCJicmVlZF9lbiI6Ik1hbHRlc2UgQm9sb2duZXNlIDXigJM4IGtnIiwiYnJlZWRfZXQiOiJNYWx0YSBib2xvbmVlcyA14oCTOCBrZyJ9LHsiYnJlZWQiOiLQnNCw0LvRjNGC0LjQudGB0LrQsNGPINCx0L7Qu9C+0L3QutCwINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJNYWx0ZXNlIEJvbG9nbmVzZSB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJNYWx0YSBib2xvbmVlcyBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JzQsNC70YzRgtC40L/RgyAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6Ik1hbHRpcG9vIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6Ik1hbHRpcHV1IDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0JzQsNC70YzRgtC40L/RgyA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiTWFsdGlwb28gNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJNYWx0aXB1dSA14oCTMTAga2cifSx7ImJyZWVkIjoi0JzQsNC70YzRgtC40L/RgyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiTWFsdGlwb28gdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiTWFsdGlwdXUga3VuaSA1IGtnIn0seyJicmVlZCI6ItCc0LXRgtC40YEg0LrRgNGD0L/QvdGL0LkgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiTWl4ZWQgYnJlZWQgbGFyZ2UgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiU2VnYXZlcmQgc3V1ciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCc0LXRgtC40YEg0LrRgNGD0L/QvdGL0Lkg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjc1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEyMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEyMH0sImJyZWVkX2VuIjoiTWl4ZWQgYnJlZWQgbGFyZ2Ugb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiU2VnYXZlcmQgc3V1ciDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCc0LXRgtC40YEg0LzQtdC70LrQuNC5IDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJNaXhlZCBicmVlZCBzbWFsbCA14oCTMTAga2ciLCJicmVlZF9ldCI6IlNlZ2F2ZXJkIHbDpGlrZSA14oCTMTAga2cifSx7ImJyZWVkIjoi0JzQtdGC0LjRgSDQvNC10LvQutC40Lkg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MjUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjozNSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTB9LCJicmVlZF9lbiI6Ik1peGVkIGJyZWVkIHNtYWxsIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IlNlZ2F2ZXJkIHbDpGlrZSBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JzQtdGC0LjRgSDRgdGA0LXQtNC90LjQuSAxMOKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo3MH0sImJyZWVkX2VuIjoiTWl4ZWQgYnJlZWQgbWVkaXVtIDEw4oCTMjAga2ciLCJicmVlZF9ldCI6IlNlZ2F2ZXJkIGtlc2ttaW5lIDEw4oCTMjAga2cifSx7ImJyZWVkIjoi0JzQtdGC0LjRgSDRgdGA0LXQtNC90LjQuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4NX0sImJyZWVkX2VuIjoiTWl4ZWQgYnJlZWQgbWVkaXVtIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IlNlZ2F2ZXJkIGtlc2ttaW5lIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JzQuNGC0YLQtdC70YzRiNC90LDRg9GG0LXRgCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzAsItCi0YDQuNC80LzQuNC90LMiOjc1fSwiYnJlZWRfZW4iOiJTdGFuZGFyZCBTY2huYXV6ZXIgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiU3RhbmRhcmTFoW5hdXRzZXIgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQnNC40YLRgtC10LvRjNGI0L3QsNGD0YbQtdGAIDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4MCwi0KLRgNC40LzQvNC40L3QsyI6ODV9LCJicmVlZF9lbiI6IlN0YW5kYXJkIFNjaG5hdXplciAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiJTdGFuZGFyZMWhbmF1dHNlciAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCc0L7Qv9GBIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo1MH0sImJyZWVkX2VuIjoiUHVnIiwiYnJlZWRfZXQiOiJNb3BzIn0seyJicmVlZCI6ItCd0LXQstGB0LrQsNGPINC+0YDRhdC40LTQtdGPIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJOZXZhIE9yY2hpZCIsImJyZWVkX2V0IjoiTmVldmEgb3JoaWRlZSJ9LHsiYnJlZWQiOiLQndC10LzQtdGG0LrQsNGPINC+0LLRh9Cw0YDQutCwIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6Ikdlcm1hbiBTaGVwaGVyZCAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJTYWtzYSBsYW1iYWtvZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQndC10LzQtdGG0LrQsNGPINC+0LLRh9Cw0YDQutCwIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJHZXJtYW4gU2hlcGhlcmQgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiU2Frc2EgbGFtYmFrb2VyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0J3QtdC80LXRhtC60LDRjyDQvtCy0YfQsNGA0LrQsCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiR2VybWFuIFNoZXBoZXJkIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IlNha3NhIGxhbWJha29lciDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCo0LLQtdC50YbQsNGA0YHQutCw0Y8g0L7QstGH0LDRgNC60LAgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiU3dpc3MgU2hlcGhlcmQgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoixaB2ZWl0c2kgbGFtYmFrb2VyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0KjQstC10LnRhtCw0YDRgdC60LDRjyDQvtCy0YfQsNGA0LrQsCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiU3dpc3MgU2hlcGhlcmQgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoixaB2ZWl0c2kgbGFtYmFrb2VyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0KjQstC10LnRhtCw0YDRgdC60LDRjyDQvtCy0YfQsNGA0LrQsCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiU3dpc3MgU2hlcGhlcmQgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoixaB2ZWl0c2kgbGFtYmFrb2VyIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0J3QvtGA0LLQuNGHLdGC0LXRgNGM0LXRgCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MCwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6Ik5vcndpY2ggVGVycmllciIsImJyZWVkX2V0IjoiTm9yd2l0xaFpIHRlcmplciJ9LHsiYnJlZWQiOiLQndC+0YDRhNC+0LvQui3RgtC10YDRjNC10YAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJOb3Jmb2xrIFRlcnJpZXIiLCJicmVlZF9ldCI6Ik5vcmZvbGtpIHRlcmplciJ9LHsiYnJlZWQiOiLQndGM0Y7RhNCw0YPQvdC00LvQtdC90LQgNDDigJM2MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEzMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiTmV3Zm91bmRsYW5kIDQw4oCTNjAga2ciLCJicmVlZF9ldCI6Ik5ld2ZvdW5kbGFuZGkga29lciA0MOKAkzYwIGtnIn0seyJicmVlZCI6ItCd0YzRjtGE0LDRg9C90LTQu9C10L3QtCDQsdC+0LvQtdC1IDYwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MTAwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MTE1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxNTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMzB9LCJicmVlZF9lbiI6Ik5ld2ZvdW5kbGFuZCBvdmVyIDYwIGtnIiwiYnJlZWRfZXQiOiJOZXdmb3VuZGxhbmRpIGtvZXIgw7xsZSA2MCBrZyJ9LHsiYnJlZWQiOiLQn9Cw0L/QuNC50L7QvSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiUGFwaWxsb24iLCJicmVlZF9ldCI6IlBhcGlsbG9uIn0seyJicmVlZCI6ItCf0LXQutC40L3QtdGBIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJQZWtpbmdlc2UgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJQZWtpbmVzaSBrb2VyIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQn9C10LrQuNC90LXRgSDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiUGVraW5nZXNlIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IlBla2luZXNpIGtvZXIga3VuaSA1IGtnIn0seyJicmVlZCI6ItCf0YPQtNC10LvRjCDQsdC+0LvRjNGI0L7QuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTB9LCJicmVlZF9lbiI6IlN0YW5kYXJkIFBvb2RsZSAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJTdGFuZGFyZHB1dWRlbCAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCf0YPQtNC10LvRjCDQsdC+0LvRjNGI0L7QuSAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwfSwiYnJlZWRfZW4iOiJTdGFuZGFyZCBQb29kbGUgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiU3RhbmRhcmRwdXVkZWwgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQn9GD0LTQtdC70Ywg0LrQsNGA0LvQuNC60L7QstGL0LkgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6Ik1pbmlhdHVyZSBQb29kbGUgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJLw6TDpGJ1c3B1dWRlbCA14oCTMTAga2cifSx7ImJyZWVkIjoi0J/Rg9C00LXQu9GMINC80LDQu9GL0LkgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJTbWFsbCBQb29kbGUgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiVsOkaWtlIHB1dWRlbCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCf0YPQtNC10LvRjCDQvNCw0LvRi9C5IDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4MH0sImJyZWVkX2VuIjoiU21hbGwgUG9vZGxlIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IlbDpGlrZSBwdXVkZWwgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQn9GD0LTQtdC70Ywg0YLQvtC5INC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJUb3kgUG9vZGxlIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6Ik3DpG5ndWFzamEgcHV1ZGVsIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQoNC40LfQtdC90YjQvdCw0YPRhtC10YAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMCwi0KLRgNC40LzQvNC40L3QsyI6MTEwfSwiYnJlZWRfZW4iOiJHaWFudCBTY2huYXV6ZXIgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiU3V1csWhbmF1dHNlciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCg0LjQt9C10L3RiNC90LDRg9GG0LXRgCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTIwLCLQotGA0LjQvNC80LjQvdCzIjoxMjV9LCJicmVlZF9lbiI6IkdpYW50IFNjaG5hdXplciBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJTdXVyxaFuYXV0c2VyIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0KDRg9GB0YHQutCw0Y8g0YbQstC10YLQvdCw0Y8g0LHQvtC70L7QvdC60LAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IlJ1c3NpYW4gQ29sb3JlZCBMYXBkb2ciLCJicmVlZF9ldCI6IlZlbmUgdsOkcnZpbGluZSBzw7xsZWtvZXIifSx7ImJyZWVkIjoi0KDRg9GB0YHQutC40Lkg0L7RhdC+0YLQvdC40YfQuNC5INGB0L/QsNC90LjQtdC70YwgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJSdXNzaWFuIFNwYW5pZWwgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiVmVuZSBqYWhpc3BhbmplbCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCg0YPRgdGB0LrQuNC5INC+0YXQvtGC0L3QuNGH0LjQuSDRgdC/0LDQvdC40LXQu9GMIDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4MH0sImJyZWVkX2VuIjoiUnVzc2lhbiBTcGFuaWVsIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IlZlbmUgamFoaXNwYW5qZWwgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQoNGD0YHRgdC60LjQuSDRgtC+0Lkg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1fSwiYnJlZWRfZW4iOiJSdXNzaWFuIFRveSBzbW9vdGgiLCJicmVlZF9ldCI6IlZlbmUgVG95IGzDvGhpa2FydmFsaW5lIn0seyJicmVlZCI6ItCg0YPRgdGB0LrQuNC5INGC0L7QuSDQtNC70LjQvdC90L7RiNC10YDRgdGC0L3Ri9C5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJSdXNzaWFuIFRveSBsb25nLWNvYXRlZCIsImJyZWVkX2V0IjoiVmVuZSBUb3kgcGlra2FydmFsaW5lIn0seyJicmVlZCI6ItCg0YPRgdGB0LrQuNC5INGH0LXRgNC90YvQuSDRgtC10YDRjNC10YAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMH0sImJyZWVkX2VuIjoiQmxhY2sgUnVzc2lhbiBUZXJyaWVyIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6Ik11c3QgVmVuZSB0ZXJqZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQoNGD0YHRgdC60LjQuSDRh9C10YDQvdGL0Lkg0YLQtdGA0YzQtdGAINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMjB9LCJicmVlZF9lbiI6IkJsYWNrIFJ1c3NpYW4gVGVycmllciBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJNdXN0IFZlbmUgdGVyamVyIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0KDRg9GB0YHQutC+LdC10LLRgNC+0L/QtdC50YHQutCw0Y8g0LvQsNC50LrQsCAyMOKAkzI4INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJSdXNzaWFuLUV1cm9wZWFuIExhaWthIDIw4oCTMjgga2ciLCJicmVlZF9ldCI6IlZlbmUtRXVyb29wYSBsYWlrYSAyMOKAkzI4IGtnIn0seyJicmVlZCI6ItCh0LDQvNC+0LXQtCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJTYW1veWVkIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IlNhbW9qZWVkIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0KHQsNC80L7QtdC0IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJTYW1veWVkIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IlNhbW9qZWVkIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0KHQtdGC0YLQtdGAINCw0L3Qs9C70LjQudGB0LrQuNC5IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5MH0sImJyZWVkX2VuIjoiRW5nbGlzaCBTZXR0ZXIgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiSW5nbGlzZSBzZXR0ZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQodC10YLRgtC10YAg0LPQvtGA0LTQvtC9IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjgwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDB9LCJicmVlZF9lbiI6IkdvcmRvbiBTZXR0ZXIgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiR29yZG9uaSBzZXR0ZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQodC10YLRgtC10YAg0LjRgNC70LDQvdC00YHQutC40LkgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwfSwiYnJlZWRfZW4iOiJJcmlzaCBTZXR0ZXIgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiSWlyaSBzZXR0ZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQodC40LHQsC3QuNC90YMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjcwfSwiYnJlZWRfZW4iOiJTaGliYSBJbnUiLCJicmVlZF9ldCI6IlNoaWJhIEludSJ9LHsiYnJlZWQiOiLQodC40LvQuNGF0LXQvC3RgtC10YDRjNC10YAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJTZWFseWhhbSBUZXJyaWVyIiwiYnJlZWRfZXQiOiJTZWFseWhhbWkgdGVyamVyIn0seyJicmVlZCI6ItCh0LrQvtGC0Yct0YLQtdGA0YzQtdGAIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiU2NvdHRpc2ggVGVycmllciIsImJyZWVkX2V0IjoixaBvdGkgdGVyamVyIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90LDRjyDQutCw0YDQu9C40LrQvtCy0LDRjyA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NTB9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCBzbW9vdGggbWluaWF0dXJlIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiVGFrc2lrb2VyIGzDvGhpa2FydmFsaW5lIGvDpMOkYnVzIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdCw0Y8g0LrRgNC+0LvQuNGH0YzRjyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NDV9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCBzbW9vdGggcmFiYml0IHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBsw7xoaWthcnZhbGluZSBrw7zDvGxpayBrdW5pIDUga2cifSx7ImJyZWVkIjoi0KLQsNC60YHQsCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3QsNGPINGB0YLQsNC90LTQsNGA0YLQvdCw0Y8gMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2MH0sImJyZWVkX2VuIjoiRGFjaHNodW5kIHNtb290aCBzdGFuZGFyZCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIgbMO8aGlrYXJ2YWxpbmUgc3RhbmRhcmQgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdCw0Y8g0LrQsNGA0LvQuNC60L7QstCw0Y8gNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCBsb25nLWNvYXRlZCBtaW5pYXR1cmUgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIgcGlra2FydmFsaW5lIGvDpMOkYnVzIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdCw0Y8g0LrRgNC+0LvQuNGH0YzRjyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiRGFjaHNodW5kIGxvbmctY29hdGVkIHJhYmJpdCB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIgcGlra2FydmFsaW5lIGvDvMO8bGlrIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdCw0Y8g0YHRgtCw0L3QtNCw0YDRgtC90LDRjyAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCBsb25nLWNvYXRlZCBzdGFuZGFyZCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIgcGlra2FydmFsaW5lIHN0YW5kYXJkIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KLQsNC60YHQsCDQttC10YHRgtC60L7RiNC10YDRgdGC0L3QsNGPINC60LDRgNC70LjQutC+0LLQsNGPIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiRGFjaHNodW5kIHdpcmUtaGFpcmVkIG1pbmlhdHVyZSA14oCTMTAga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBrYXJ1a2FydmFsaW5lIGvDpMOkYnVzIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINC20LXRgdGC0LrQvtGI0LXRgNGB0YLQvdCw0Y8g0LrRgNC+0LvQuNGH0YzRjyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NSwi0KLRgNC40LzQvNC40L3QsyI6NTV9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCB3aXJlLWhhaXJlZCByYWJiaXQgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiVGFrc2lrb2VyIGthcnVrYXJ2YWxpbmUga8O8w7xsaWsga3VuaSA1IGtnIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90LDRjyDRgdGC0LDQvdC00LDRgNGC0L3QsNGPIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MCwi0KLRgNC40LzQvNC40L3QsyI6NzV9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCB3aXJlLWhhaXJlZCBzdGFuZGFyZCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIga2FydWthcnZhbGluZSBzdGFuZGFyZCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCj0LjQv9C/0LXRgiAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NX0sImJyZWVkX2VuIjoiV2hpcHBldCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJXaGlwcGV0IDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KPQuNC/0L/QtdGCIDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwfSwiYnJlZWRfZW4iOiJXaGlwcGV0IDE14oCTMjAga2ciLCJicmVlZF9ldCI6IldoaXBwZXQgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQpNC40L3RgdC60LjQuSDQu9Cw0L/RhdGD0L3QtCAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODV9LCJicmVlZF9lbiI6IkZpbm5pc2ggTGFwcGh1bmQgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiU29vbWUgbGFtYmFrb2VyIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0KTQuNC90YHQutC40Lkg0LvQsNC/0YXRg9C90LQgMjDigJMyNCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjU1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwfSwiYnJlZWRfZW4iOiJGaW5uaXNoIExhcHBodW5kIDIw4oCTMjQga2ciLCJicmVlZF9ldCI6IlNvb21lIGxhbWJha29lciAyMOKAkzI0IGtnIn0seyJicmVlZCI6ItCk0L7QutGB0YLQtdGA0YzQtdGAINC20LXRgdGC0LrQvtGI0LXRgNGB0YLQvdGL0LkgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwLCLQotGA0LjQvNC80LjQvdCzIjo3NX0sImJyZWVkX2VuIjoiV2lyZSBGb3ggVGVycmllciAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJLYXJ1a2FydmFsaW5lIGZveHRlcmplciAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCk0L7QutGB0YLQtdGA0YzQtdGAINC20LXRgdGC0LrQvtGI0LXRgNGB0YLQvdGL0LkgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJXaXJlIEZveCBUZXJyaWVyIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiS2FydWthcnZhbGluZSBmb3h0ZXJqZXIgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCk0YDQsNC90YbRg9C30YHQutC40Lkg0LHRg9C70YzQtNC+0LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjYwfSwiYnJlZWRfZW4iOiJGcmVuY2ggQnVsbGRvZyIsImJyZWVkX2V0IjoiUHJhbnRzdXNlIGJ1bGRvZyJ9LHsiYnJlZWQiOiLQpdCw0YHQutC4IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IlNpYmVyaWFuIEh1c2t5IDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IlNpYmVyaSBodXNreSAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCl0LDRgdC60LggMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IlNpYmVyaWFuIEh1c2t5IDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IlNpYmVyaSBodXNreSAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCm0LLQtdGA0LPRiNC90LDRg9GG0LXRgCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzAsItCi0YDQuNC80LzQuNC90LMiOjc1fSwiYnJlZWRfZW4iOiJNaW5pYXR1cmUgU2NobmF1emVyIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IkvDpMOkYnVzxaFuYXV0c2VyIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KbQstC10YDQs9GI0L3QsNGD0YbQtdGAIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiTWluaWF0dXJlIFNjaG5hdXplciA14oCTMTAga2ciLCJicmVlZF9ldCI6IkvDpMOkYnVzxaFuYXV0c2VyIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQp9Cw0YMt0YfQsNGDIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQ2hvdyBDaG93IDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkNob3cgQ2hvdyAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCn0LDRgy3Rh9Cw0YMgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjExMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiQ2hvdyBDaG93IDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkNob3cgQ2hvdyAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCn0LjRhdGD0LDRhdGD0LAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1fSwiYnJlZWRfZW4iOiJDaGlodWFodWEgc21vb3RoIiwiYnJlZWRfZXQiOiJUxaFpaHVhaHVhIGzDvGhpa2FydmFsaW5lIn0seyJicmVlZCI6ItCn0LjRhdGD0LDRhdGD0LAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90YvQuSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiQ2hpaHVhaHVhIGxvbmctY29hdGVkIiwiYnJlZWRfZXQiOiJUxaFpaHVhaHVhIHBpa2thcnZhbGluZSJ9LHsiYnJlZWQiOiLQqNCw0YDQv9C10LkgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2NX0sImJyZWVkX2VuIjoiU2hhciBQZWkgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoixaBhci1QZWkgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQqNCw0YDQv9C10LkgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo3MH0sImJyZWVkX2VuIjoiU2hhciBQZWkgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoixaBhci1QZWkgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQqNC10LvRgtC4Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjB9LCJicmVlZF9lbiI6IlNoZXRsYW5kIFNoZWVwZG9nIiwiYnJlZWRfZXQiOiLFoGV0bGFuZGkgbGFtYmFrb2VyIn0seyJicmVlZCI6ItCo0Lgt0YLRhtGDIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJTaGloIFR6dSA14oCTMTAga2ciLCJicmVlZF9ldCI6IlNoaWggVHp1IDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQqNC4LdGC0YbRgyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiU2hpaCBUenUgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiU2hpaCBUenUga3VuaSA1IGtnIn0seyJicmVlZCI6ItCo0L3QsNGD0YbQtdGAINC80LjQvdC40LDRgtGO0YDQvdGL0Lkg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6Ik1pbmlhdHVyZSBTY2huYXV6ZXIgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiS8Okw6RidXPFoW5hdXRzZXIga3VuaSA1IGtnIn0seyJicmVlZCI6ItCo0L/QuNGGINC90LXQvNC10YbQutC40LkgLyDQv9C+0LzQtdGA0LDQvdGB0LrQuNC5INCx0L7Qu9C10LUgMyw1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2MH0sImJyZWVkX2VuIjoiR2VybWFuIFNwaXR6IC8gUG9tZXJhbmlhbiBvdmVyIDMsNSBrZyIsImJyZWVkX2V0IjoiU2Frc2Egc3BpdHMgLyBQb21lcmFuaWFuIMO8bGUgMyw1IGtnIn0seyJicmVlZCI6ItCo0L/QuNGGINC90LXQvNC10YbQutC40LkgLyDQv9C+0LzQtdGA0LDQvdGB0LrQuNC5INC00L4gMyw1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo1NX0sImJyZWVkX2VuIjoiR2VybWFuIFNwaXR6IC8gUG9tZXJhbmlhbiB1cCB0byAzLDUga2ciLCJicmVlZF9ldCI6IlNha3NhIHNwaXRzIC8gUG9tZXJhbmlhbiBrdW5pIDMsNSBrZyJ9LHsiYnJlZWQiOiLQqNC/0LjRhiDRj9C/0L7QvdGB0LrQuNC5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjB9LCJicmVlZF9lbiI6IkphcGFuZXNlIFNwaXR6IiwiYnJlZWRfZXQiOiJKYWFwYW5pIHNwaXRzIn0seyJicmVlZCI6ItCp0LXQvdC60LgiLCJzZXJ2aWNlcyI6eyLQktGB0Y8g0L/RgNC+0LPRgNCw0LzQvNCwIjo1NX0sImJyZWVkX2VuIjoiUHVwcGllcyIsImJyZWVkX2V0IjoiS3V0c2lrYWQifSx7ImJyZWVkIjoi0K3RgdGC0L7QvdGB0LrQsNGPINCz0L7QvdGH0LDRjyAxNeKAkzI1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MH0sImJyZWVkX2VuIjoiRXN0b25pYW4gSG91bmQgMTXigJMyNSBrZyIsImJyZWVkX2V0IjoiRWVzdGkgaGFnaWphcyAxNeKAkzI1IGtnIn0seyJicmVlZCI6ItCv0L/QvtC90YHQutC40Lkg0YXQuNC9Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJKYXBhbmVzZSBDaGluIiwiYnJlZWRfZXQiOiJKYWFwYW5pIENoaW4ifSx7ImJyZWVkIjoi0JrQvtGI0LrQsCDQutC+0YDQvtGC0LrQvtGI0LXRgNGB0YLQvdCw0Y8iLCJzZXJ2aWNlcyI6eyLQktGL0YfQtdGBIjo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkNhdCBzaG9ydC1oYWlyZWQiLCJicmVlZF9ldCI6Ikthc3MgbMO8aGlrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0JrQvtGI0LrQsCDQtNC70LjQvdC90L7RiNC10YDRgdGC0L3QsNGPIiwic2VydmljZXMiOnsi0JLRi9GH0LXRgSI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwfSwiYnJlZWRfZW4iOiJDYXQgbG9uZy1oYWlyZWQiLCJicmVlZF9ldCI6Ikthc3MgcGlra2FydmFsaW5lIn0seyJicmVlZCI6ItCa0L7RiNC60LAg0JzQtdC50L0t0LrRg9C9Iiwic2VydmljZXMiOnsi0JLRi9GH0ZHRgSI6NjAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwfSwiYnJlZWRfZW4iOiJDYXQgTWFpbmUgQ29vbiIsImJyZWVkX2V0IjoiS2FzcyBNYWluZSBDb29uIn1dOwp2YXIgUkFJTFdBWSA9ICJodHRwczovL3JqZ3Jvb21pbmcudXAucmFpbHdheS5hcHAvYm9vayI7CnZhciBHT09HTEVfU0NSSVBUID0gImh0dHBzOi8vc2NyaXB0Lmdvb2dsZS5jb20vbWFjcm9zL3MvQUtmeWNieVRTWi1lSk1kZXAtRDBMci1ueDBfVjRIQldnSUljdG5SVDJyalNEdkJ5Ymo1Q1lJM05LMk1xY0F3X2NmY3pnUkVpZmcvZXhlYyI7CnZhciBGQUxMQkFDS19USU1FUyA9IFsnMTA6MDAnLCcxMDozMCcsJzExOjAwJywnMTE6MzAnLCcxMjowMCcsJzEyOjMwJywnMTM6MDAnLCcxMzozMCcsJzE0OjAwJywnMTQ6MzAnLCcxNTowMCcsJzE1OjMwJywnMTY6MDAnLCcxNjozMCcsJzE3OjAwJywnMTc6MzAnLCcxODowMCddOwp2YXIgYm9va2luZyA9IHticmVlZDonJyxicmVlZERpc3BsYXk6Jycsc2VydmljZTonJyxwcmljZTowLG1hc3RlcjonJyxncm9vbUhpc3Rvcnk6JycsZGF0ZTonJyx0aW1lOicnLGxhbmc6J3J1J307CnZhciBzZWxCcmVlZCA9IG51bGw7CnZhciBjWSA9IG5ldyBEYXRlKCkuZ2V0RnVsbFllYXIoKTsKdmFyIGNNID0gbmV3IERhdGUoKS5nZXRNb250aCgpOwp2YXIgc3RlcCA9IDE7CnZhciBNT05USFMgPSBbJ9Cv0L3QstCw0YDRjCcsJ9Ck0LXQstGA0LDQu9GMJywn0JzQsNGA0YInLCfQkNC/0YDQtdC70YwnLCfQnNCw0LknLCfQmNGO0L3RjCcsJ9CY0Y7Qu9GMJywn0JDQstCz0YPRgdGCJywn0KHQtdC90YLRj9Cx0YDRjCcsJ9Ce0LrRgtGP0LHRgNGMJywn0J3QvtGP0LHRgNGMJywn0JTQtdC60LDQsdGA0YwnXTsKCmZ1bmN0aW9uIHNob3dTY3JlZW4oaWQpIHsKICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuc2NyZWVuJykuZm9yRWFjaChmdW5jdGlvbihzKXtzLmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpO30pOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKGlkKS5jbGFzc0xpc3QuYWRkKCdhY3RpdmUnKTsKICB3aW5kb3cuc2Nyb2xsVG8oMCwwKTsKfQoKZnVuY3Rpb24gZ29TdGVwKG4pIHsKICBbJ2JrMScsJ2JrMicsJ2JrMycsJ2JrNCcsJ2JrNSddLmZvckVhY2goZnVuY3Rpb24oaWQsaSl7CiAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZChpZCkuY2xhc3NOYW1lID0gJ3N0ZXAnICsgKGkrMT09PW4/JyBzaG93JzonJyk7CiAgfSk7CiAgZm9yKHZhciBpPTE7aTw9NTtpKyspewogICAgdmFyIHBzPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdwcycraSk7CiAgICB2YXIgcGw9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3BsJytpKTsKICAgIGlmKGk8bil7cHMuY2xhc3NOYW1lPSdwcyBkb25lJztpZihwbClwbC5jbGFzc05hbWU9J3BsIGRvbmUnO30KICAgIGVsc2UgaWYoaT09PW4pe3BzLmNsYXNzTmFtZT0ncHMgYWN0aXZlJztpZihwbClwbC5jbGFzc05hbWU9J3BsJzt9CiAgICBlbHNle3BzLmNsYXNzTmFtZT0ncHMnO2lmKHBsKXBsLmNsYXNzTmFtZT0ncGwnO30KICB9CiAgc3RlcD1uOyB3aW5kb3cuc2Nyb2xsVG8oMCwwKTsKICBpZihuPT09MikgZmlsdGVyTWFzdGVycygpOwp9Cgpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnYm9va0J0bicpLm9uY2xpY2sgPSBmdW5jdGlvbigpewogIHNob3dTY3JlZW4oJ2Jvb2tTY3JlZW4nKTsgZ29TdGVwKDEpOyBidWlsZENhbCgpOwp9Owpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnYmFja0J0bicpLm9uY2xpY2sgPSBmdW5jdGlvbigpewogIGlmKHN0ZXA+MSl7Z29TdGVwKHN0ZXAtMSk7fWVsc2V7c2hvd1NjcmVlbignaG9tZVNjcmVlbicpO30KfTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2hvbWVCdG4nKS5vbmNsaWNrID0gZnVuY3Rpb24oKXsKICBzaG93U2NyZWVuKCdob21lU2NyZWVuJyk7IHJlc2V0QWxsKCk7Cn07CgovLyBCcmVlZCBzZWFyY2gKdmFyIGlucCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdiSW5wdXQnKTsKdmFyIGRyb3AgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnYkRyb3AnKTsKdmFyIGNsciA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjbHJCdG4nKTsKdmFyIGJhZGdlID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3NCYWRnZScpOwoKaW5wLmFkZEV2ZW50TGlzdGVuZXIoJ2lucHV0JywgZnVuY3Rpb24oKXsKICB2YXIgcSA9IGlucC52YWx1ZS50cmltKCk7CiAgY2xyLmNsYXNzTGlzdC50b2dnbGUoJ3Nob3cnLCBxLmxlbmd0aD4wKTsKICBpZighcSl7ZHJvcC5jbGFzc0xpc3QucmVtb3ZlKCdvcGVuJyk7ZHJvcC5pbm5lckhUTUw9Jyc7cmV0dXJuO30KICB2YXIgc2Y9TEFORz09PSdlbic/J2JyZWVkX2VuJzpMQU5HPT09J2V0Jz8nYnJlZWRfZXQnOidicmVlZCc7CiAgdmFyIHJlcz1EQVRBLmZpbHRlcihmdW5jdGlvbihiKXtyZXR1cm4oYltzZl18fGIuYnJlZWQpLnRvTG93ZXJDYXNlKCkuaW5kZXhPZihxLnRvTG93ZXJDYXNlKCkpIT09LTE7fSkuc2xpY2UoMCwzNSk7CiAgZHJvcC5pbm5lckhUTUw9Jyc7CiAgdmFyIF9ucj1MQU5HPT09J2VuJz8nQnJlZWQgbm90IGZvdW5kJzpMQU5HPT09J2V0Jz8nVMO1dWd1IGVpIGxlaXR1ZCc6J9Cf0L7RgNC+0LTQsCDQvdC1INC90LDQudC00LXQvdCwJzsKICB2YXIgX250PUxBTkc9PT0nZW4nPyJDYW4ndCBmaW5kIHlvdXIgYnJlZWQ/IjpMQU5HPT09J2V0Jz8nRWkgbGVpYSBvbWEgdMO1dWd1Pyc6J9Cd0LUg0L3QsNGI0LvQuCDRgdCy0L7RjiDQv9C+0YDQvtC00YM/JzsKICB2YXIgX25zPUxBTkc9PT0nZW4nPydDb250YWN0IHVzIOKAlCB3ZSB3aWxsIGhlbHAgeW91IGNob29zZSBhIHNlcnZpY2UnOkxBTkc9PT0nZXQnPydWw7V0a2UgbWVpZWdhIMO8aGVuZHVzdCDigJQgYWl0YW1lIHRlZW51c2UgdmFsaWRhJzon0KHQstGP0LbQuNGC0LXRgdGMINGBINC90LDQvNC4INC70Y7QsdGL0Lwg0YPQtNC+0LHQvdGL0Lwg0YHQv9C+0YHQvtCx0L7QvCDigJQg0LzRiyDQv9C+0LzQvtC20LXQvCDQv9C+0LTQvtCx0YDQsNGC0Ywg0YPRgdC70YPQs9GDJzsKICBpZighcmVzLmxlbmd0aCl7ZHJvcC5pbm5lckhUTUw9JzxkaXYgY2xhc3M9Im5vcmVzIj4nK19ucisnPC9kaXY+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyIiBvbmNsaWNrPSJzaG93U2NyZWVuKFwnaG9tZVNjcmVlblwnKSI+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLWljb24iPvCfkL48L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItdGV4dCI+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLXRpdGxlIj4nK19udCsnPC9kaXY+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLXN1YiI+JytfbnMrJzwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci1hcnJvdyI+4oaSPC9kaXY+PC9kaXY+Jzt9CiAgZWxzZXsKICAgIHJlcy5mb3JFYWNoKGZ1bmN0aW9uKGIpewogICAgICB2YXIgZD1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdkaXYnKTsgZC5jbGFzc05hbWU9J2RpdGVtJzsKICAgICAgdmFyIGJuYW1lPWJbc2ZdfHxiLmJyZWVkOwogICAgICB2YXIgaWR4PWJuYW1lLnRvTG93ZXJDYXNlKCkuaW5kZXhPZihxLnRvTG93ZXJDYXNlKCkpOwogICAgICBkLmlubmVySFRNTD1ibmFtZS5zdWJzdHJpbmcoMCxpZHgpKyc8bWFyaz4nK2JuYW1lLnN1YnN0cmluZyhpZHgsaWR4K3EubGVuZ3RoKSsnPC9tYXJrPicrYm5hbWUuc3Vic3RyaW5nKGlkeCtxLmxlbmd0aCk7CiAgICAgIGQub25jbGljaz1mdW5jdGlvbigpe3NlbGVjdEJyZWVkKGIpO307CiAgICAgIGRyb3AuYXBwZW5kQ2hpbGQoZCk7CiAgICB9KTsKICB9CiAgZHJvcC5jbGFzc0xpc3QuYWRkKCdvcGVuJyk7Cn0pOwoKZG9jdW1lbnQuYWRkRXZlbnRMaXN0ZW5lcignY2xpY2snLGZ1bmN0aW9uKGUpewogIGlmKCFlLnRhcmdldC5jbG9zZXN0KCcuYndyYXAnKSlkcm9wLmNsYXNzTGlzdC5yZW1vdmUoJ29wZW4nKTsKfSk7CmNsci5vbmNsaWNrID0gcmVzZXRCcmVlZDsKCmZ1bmN0aW9uIHNlbGVjdEJyZWVkKGIpewogIHNlbEJyZWVkPWI7IGJvb2tpbmcuYnJlZWQ9Yi5icmVlZDsKICBpbnAudmFsdWU9Jyc7IGNsci5jbGFzc0xpc3QucmVtb3ZlKCdzaG93Jyk7CiAgZHJvcC5jbGFzc0xpc3QucmVtb3ZlKCdvcGVuJyk7IGRyb3AuaW5uZXJIVE1MPScnOwogIGJhZGdlLmlubmVySFRNTD0nJzsKICB2YXIgYkZpZWxkPUxBTkc9PT0nZW4nPydicmVlZF9lbic6TEFORz09PSdldCc/J2JyZWVkX2V0JzonYnJlZWQnOwogIHZhciBkaXNwQnJlZWQ9YltiRmllbGRdfHxiLmJyZWVkOwogIGJvb2tpbmcuYnJlZWREaXNwbGF5PWRpc3BCcmVlZDsKICB2YXIgYm49ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnc3BhbicpO2JuLmNsYXNzTmFtZT0nYm5hbWUnO2JuLnRleHRDb250ZW50PWRpc3BCcmVlZDsKICB2YXIgY2hnVHh0PUxBTkc9PT0nZW4nPydDaGFuZ2UnOkxBTkc9PT0nZXQnPydNdXVkYSc6J9CY0LfQvNC10L3QuNGC0YwnOwogIHZhciBiYz1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdzcGFuJyk7YmMuY2xhc3NOYW1lPSdiY2hnJztiYy50ZXh0Q29udGVudD1jaGdUeHQ7CiAgYmMub25jbGljaz1yZXNldEJyZWVkOwogIGJhZGdlLmFwcGVuZENoaWxkKGJuKTtiYWRnZS5hcHBlbmRDaGlsZChiYyk7CiAgYmFkZ2UuY2xhc3NMaXN0LmFkZCgnc2hvdycpOwogIHJlbmRlclN2Y3MoYik7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y1NlYycpLnN0eWxlLmRpc3BsYXk9J2Jsb2NrJzsKICAgIC8vIEFkZCBpbXBvcnRhbnQgbm90ZSBpZiBub3QgZXhpc3RzCiAgICBpZighZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y05vdGUnKSl7CiAgICAgIHZhciBub3RlPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpOwogICAgICBub3RlLmlkPSdzdmNOb3RlJzsKICAgICAgbm90ZS5zdHlsZS5jc3NUZXh0PSdib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA4KTtwYWRkaW5nOjE0cHggMTZweDtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjAyKTttYXJnaW4tdG9wOjEycHg7JzsKICAgICAgdmFyIG5vdGVUaXRsZT1MQU5HPT09J2VuJz8nUGxlYXNlIG5vdGUnOkxBTkc9PT0nZXQnPydQYW5nZSB0w6RoZWxlJzon0JLQsNC20L3QviDQt9C90LDRgtGMJzsKICAgICAgdmFyIG5vdGVCb2R5PUxBTkc9PT0nZW4nPydGaW5hbCBwcmljZSBkZXBlbmRzIG9uIGNvYXQgY29uZGl0aW9uIGFuZCBwZXQgYmVoYXZpb3VyLjxicj5EZW1hdHRpbmcgZnJvbSA1IOKCrC48YnI+QWdncmVzc2l2ZSBiZWhhdmlvdXIgc3VyY2hhcmdlIG1heSBhcHBseTogKzUwJS4nOkxBTkc9PT0nZXQnPydMw7VwbGlrIGhpbmQgc8O1bHR1YiBrYXJ2YXN0aWt1IHNlaXN1bmRpc3QgamEgbGVtbWlrbG9vbWEga8OkaXR1bWlzZXN0Ljxicj5Lb2x0c3VuaXRlIGxhaHRpaGFydXRhbWluZSBhbGF0ZXMgNSDigqwuPGJyPkFncmVzc2lpdnNlIGvDpGl0dW1pc2Uga29ycmFsIHbDtWliIGxpc2FuZHVkYSA1MCUganV1cmRlaGluZGx1cy4nOifQntC60L7QvdGH0LDRgtC10LvRjNC90LDRjyDRgdGC0L7QuNC80L7RgdGC0Ywg0LfQsNCy0LjRgdC40YIg0L7RgiDRgdC+0YHRgtC+0Y/QvdC40Y8g0YjQtdGA0YHRgtC4INC4INC/0L7QstC10LTQtdC90LjRjyDQv9C40YLQvtC80YbQsC48YnI+0KDQsNC30LHQvtGAINC60L7Qu9GC0YPQvdC+0LIg4oCUINC+0YIgNSDigqwuPGJyPtCf0YDQuCDQsNCz0YDQtdGB0YHQuNCy0L3QvtC8INC/0L7QstC10LTQtdC90LjQuCDQvNC+0LbQtdGCINC/0YDQuNC80LXQvdGP0YLRjNGB0Y8g0LTQvtC/0LvQsNGC0LAgNTAlLic7CiAgICAgIG5vdGUuaW5uZXJIVE1MPSc8ZGl2IHN0eWxlPSJmb250LXNpemU6MC44MzhyZW07bGV0dGVyLXNwYWNpbmc6LjE1ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbTo4cHg7Zm9udC13ZWlnaHQ6NjAwO2ZvbnQtZmFtaWx5OlwnTW9udHNlcnJhdFwnLHNhbnMtc2VyaWYiPicrbm90ZVRpdGxlKyc8L2Rpdj48ZGl2IHN0eWxlPSJmb250LXNpemU6MS4wMjVyZW07Y29sb3I6I2ZmZmZmZjtsaW5lLWhlaWdodDoxLjg7Zm9udC1mYW1pbHk6XCdNb250c2VycmF0XCcsc2Fucy1zZXJpZiI+Jytub3RlQm9keSsnPC9kaXY+JzsKICAgICAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y1NlYycpLmFwcGVuZENoaWxkKG5vdGUpOwogICAgfQogIGZpbHRlck1hc3RlcnMoKTsKfQoKZnVuY3Rpb24gcmVzZXRCcmVlZCgpewogIHNlbEJyZWVkPW51bGw7Ym9va2luZy5icmVlZD0nJztib29raW5nLnNlcnZpY2U9Jyc7Ym9va2luZy5wcmljZT0wOwogIGlucC52YWx1ZT0nJztjbHIuY2xhc3NMaXN0LnJlbW92ZSgnc2hvdycpOwogIGJhZGdlLmNsYXNzTGlzdC5yZW1vdmUoJ3Nob3cnKTtiYWRnZS5pbm5lckhUTUw9Jyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y1NlYycpLnN0eWxlLmRpc3BsYXk9J25vbmUnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdmNMaXN0JykuaW5uZXJIVE1MPScnOwp9CgoKdmFyIFNWQ19UUkFOU0xBVElPTlMgPSB7CiAgJ9CR0LDQt9C+0LLRi9C5INGD0YXQvtC0JzogICAgICB7ZW46J0Jhc2ljIGdyb29tJywgICAgICBldDonUMO1aGlob29sZHVzJ30sCiAgJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Jzp7ZW46J0h5Z2llbmUgZ3Jvb20nLCAgICBldDonSMO8Z2llZW5paG9vbGR1cyd9LAogICfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0JzogIHtlbjonRnVsbCBncm9vbScsICAgICAgICBldDonVMOkaWVsaWsgaG9vbGR1cyd9LAogICfQotGA0LjQvNC80LjQvdCzJzogICAgICAgICAge2VuOidUcmltbWluZycsICAgICAgICAgIGV0OidUcmltbWVyaW1pbmUnfSwKICAn0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAnOiAgIHtlbjonRXhwcmVzcyBzaGVkJywgICAgICBldDonS2lpcmthcnZhdmFoZXR1cyd9LAogICfQktGL0YfQtdGBJzogICAgICAgICAgICAge2VuOidCcnVzaC1vdXQnLCAgICAgICAgIGV0OidIYXJqYW1pbmUnfSwKICAn0JLRgdGPINC/0YDQvtCz0YDQsNC80LzQsCc6ICAgICB7ZW46J0Z1bGwgcHJvZ3JhbScsICAgICAgZXQ6J0tvZ3UgcHJvZ3JhbW0nfQp9Owp2YXIgU1ZDX1RBR0xJTkVfSTE4Tj17CiAgcnU6eyfQktGL0YfQtdGBJzon0KHRgtC+0LjQvNC+0YHRgtGMINC30LDQstC40YHQuNGCINC+0YIg0YHQvtGB0YLQvtGP0L3QuNGPINGI0LXRgNGB0YLQuCDQuCDQvtCx0YrRkdC80LAg0YDQsNCx0L7RgicsJ9CR0LDQt9C+0LLRi9C5INGD0YXQvtC0Jzon0J/QvtC00YXQvtC00LjRgiDQtNC70Y8g0L/QvtC00LTQtdGA0LbQsNC90LjRjyDRh9C40YHRgtC+0YLRiyDQvNC10LbQtNGDINC/0YDQvtGG0LXQtNGD0YDQsNC80LgnLCfQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCc6J9CU0LvRjyDQutC+0LzRhNC+0YDRgtCwINC4INCw0LrQutGD0YDQsNGC0L3QvtGB0YLQuCDQv9C40YLQvtC80YbQsCcsJ9Ca0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQnOifQn9C+0LvQvdGL0Lkg0YPRhdC+0LQg0YHQviDRgdGC0YDQuNC20LrQvtC5Jywn0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAnOifQn9C+0LzQvtCz0LDQtdGCINGD0LzQtdC90YzRiNC40YLRjCDQutC+0LvQuNGH0LXRgdGC0LLQviDQu9C40L3Rj9GO0YnQtdC5INGI0LXRgNGB0YLQuCcsJ9Ci0YDQuNC80LzQuNC90LMnOifQlNC70Y8g0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90YvRhSDQv9C+0YDQvtC0J30sCiAgZW46eyfQktGL0YfQtdGBJzonUHJpY2UgZGVwZW5kcyBvbiBjb2F0IGNvbmRpdGlvbiBhbmQgdm9sdW1lIG9mIHdvcmsnLCfQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCc6J0lkZWFsIGZvciBtYWludGFpbmluZyBjbGVhbmxpbmVzcyBiZXR3ZWVuIGZ1bGwgZ3Jvb21zJywn0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQnOidGb3IgeW91ciBwZXRcJ3MgY29tZm9ydCBhbmQgbmVhdG5lc3MnLCfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0JzonRnVsbCBncm9vbWluZyB3aXRoIGhhaXJjdXQnLCfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6J1NpZ25pZmljYW50bHkgcmVkdWNlcyBzaGVkZGluZycsJ9Ci0YDQuNC80LzQuNC90LMnOidGb3Igd2lyZS1oYWlyZWQgYnJlZWRzJ30sCiAgZXQ6eyfQktGL0YfQtdGBJzonSGluZCBzw7VsdHViIGthcnZhc3Rpa3Ugc2Vpc3VuZGlzdCBqYSB0w7bDtm1haHVzdCcsJ9CR0LDQt9C+0LLRi9C5INGD0YXQvtC0JzonU29iaWIgcHVodHVzZSBob2lkbWlzZWtzIHByb3RzZWR1dXJpZGUgdmFoZWwnLCfQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCc6J0xlbW1pa2xvb21hIG11Z2F2dXNla3MgamEga29ycmFzaG9pdWtzJywn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc6J1TDpGllbGlrIGhvb2xkdXMga29vcyBsw7Vpa3VzZWdhJywn0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAnOidWw6RoZW5kYWIgb2x1bGlzZWx0IGthcnZhZGUgbGFuZ2VtaXN0Jywn0KLRgNC40LzQvNC40L3Qsyc6J1RyYWF0a2FydmFsaXN0ZWxlIHTDtXVndWRlbGUnfQp9Owp2YXIgU1ZDX0RFU0NfSTE4Tj17CiAgcnU6eyfQktGL0YfQtdGBJzon0KfQuNGB0YLQutCwINCz0LvQsNC3LCDRg9GI0LXQuSwg0L/QvtC00YHRgtGA0LjQs9Cw0L3QuNC1INC60L7Qs9GC0LXQuSwg0LLRi9GH0ZHRgSAo0LTQu9GPINC60L7RiNC10LopJywn0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQnOifQnNGL0YLRjNGRINC/0YDQvtGE0LXRgdGB0LjQvtC90LDQu9GM0L3Ri9C80Lgg0YHRgNC10LTRgdGC0LLQsNC80LgsINC00LXQu9C40LrQsNGC0L3QsNGPINGB0YPRiNC60LAnLCfQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCc6J9Ch0YLRgNC40LbQutCwINC60L7Qs9GC0LXQuSwg0YfQuNGB0YLQutCwINGD0YjQtdC5INC4INCz0LvQsNC3LCDQutGD0L/QsNC90LjQtSwg0YHRg9GI0LrQsCwg0YPRhdC+0LQg0LfQsCDQu9Cw0L/QutCw0LzQuCDQuCDRh9GD0LLRgdGC0LLQuNGC0LXQu9GM0L3Ri9C80Lgg0LfQvtC90LDQvNC4Jywn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc6J9Ch0YLRgNC40LbQutCwINC60L7Qs9GC0LXQuSwg0YfQuNGB0YLQutCwINGD0YjQtdC5INC4INCz0LvQsNC3LCDQutGD0L/QsNC90LjQtSwg0YHRg9GI0LrQsCwg0YPRhdC+0LQg0LfQsCDQu9Cw0L/QutCw0LzQuCDQuCDRh9GD0LLRgdGC0LLQuNGC0LXQu9GM0L3Ri9C80Lgg0LfQvtC90LDQvNC4LCDQvNC+0LTQtdC70YzQvdCw0Y8g0YHRgtGA0LjQttC60LAnLCfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6J9Cc0YvRgtGM0ZEsINGB0YPRiNC60LAsINGD0YXQvtC0INC30LAg0YjQtdGA0YHRgtGM0Y4sINC80LDRgdC60LAsINC/0L7QtNGB0YLRgNC40LPQsNC90LjQtSDQutC+0LPRgtC10LksINGH0LjRgdGC0LrQsCDRg9GI0LXQuSDQuCDQs9C70LDQtywg0YPRhdC+0LQg0LfQsCDQu9Cw0L/QsNC80Lgg0Lgg0LfQvtC90LDQvNC4INGC0YDQtdCx0YPRjtGJ0LjQvNC4INC+0YHQvtCx0L7Qs9C+INCy0L3QuNC80LDQvdC40Y8nLCfQotGA0LjQvNC80LjQvdCzJzon0JLRi9GJ0LjQv9GL0LLQsNC90LjQtSDRgdGC0LDRgNC+0LPQviDRgdC70L7RjyDRiNC10YDRgdGC0LgsINC80YvRgtGM0ZEsINGB0YPRiNC60LAsINGB0YLRgNC40LbQutCwINC60L7Qs9GC0LXQuSwg0YfQuNGB0YLQutCwINGD0YjQtdC5INC4INCz0LvQsNC3LCDQvtGE0L7RgNC80LvQtdC90LjQtSDRiNC10YDRgdGC0LgnLCfQktGB0Y8g0L/RgNC+0LPRgNCw0LzQvNCwJzon0J/QldCg0JLQq9CZINCS0JjQl9CY0KIgKDIwLTMwINC80LjQvSkg4oCUIDIwIOKCrFxu4oCiINC30L3QsNC60L7QvNGB0YLQstC+INGB0L4g0YHRgtC+0LvQvtC8INC4INC40L3RgdGC0YDRg9C80LXQvdGC0LDQvNC4XG7igKIg0LvRkdCz0LrQvtC1INCy0YvRh9GR0YHRi9Cy0LDQvdC40LVcbuKAoiDQt9Cy0YPQutC4INGE0LXQvdCwINC4INC70LXQs9C60LDRjyDQv9GA0L7QtNGD0LLQutCwXG7igKIg0L7RgdCy0LXQttC10L3QuNC1INCz0LvQsNC30L7QuiDQuCDRg9GI0LXQulxu4oCiINC60L7Qs9C+0YLQutC4XG7igKIg0LLQutGD0YHQvdGP0YjQutC4INC4INGB0L/QvtC60L7QudC90LDRjyDQsNC00LDQv9GC0LDRhtC40Y9cblxu0JLQotCe0KDQntCZINCS0JjQl9CY0KIgKDQwLTYwINC80LjQvSkg4oCUIDM1IOKCrFxu4oCiINC/0LXRgNCy0L7QtSDQutGD0L/QsNC90LjQtSDQuCDRgdGD0YjQutCwXG7igKIg0LLRi9GH0ZHRgdGL0LLQsNC90LjQtVxu4oCiINCz0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0XG7igKIg0L3QtdCx0L7Qu9GM0YjQsNGPINGB0YLRgNC40LbQutCwIC8g0LrQvtGA0YDQtdC60YbQuNGPINGI0LXRgNGB0YLQuCAo0L/RgNC4INC90LXQvtCx0YXQvtC00LjQvNC+0YHRgtC4KVxu4oCiINC30LDQutGA0LXQv9C70LXQvdC40LUg0L/QvtC70L7QttC40YLQtdC70YzQvdC+0LPQviDQvtC/0YvRgtCwJ30sCiAgZW46eyfQktGL0YfQtdGBJzonRXllIGFuZCBlYXIgY2xlYW5pbmcsIG5haWwgdHJpbW1pbmcsIGJydXNoaW5nIChmb3IgY2F0cyknLCfQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCc6J1dhc2hpbmcgd2l0aCBwcm9mZXNzaW9uYWwgcHJvZHVjdHMsIGdlbnRsZSBkcnlpbmcnLCfQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCc6J05haWwgdHJpbW1pbmcsIGVhciBhbmQgZXllIGNsZWFuaW5nLCBiYXRoaW5nLCBkcnlpbmcsIHBhdyBhbmQgc2Vuc2l0aXZlIGFyZWEgY2FyZScsJ9Ca0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQnOidOYWlsIHRyaW1taW5nLCBlYXIgYW5kIGV5ZSBjbGVhbmluZywgYmF0aGluZywgZHJ5aW5nLCBwYXcgYW5kIHNlbnNpdGl2ZSBhcmVhIGNhcmUsIHN0eWxpbmcgaGFpcmN1dCcsJ9Ct0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwJzonV2FzaGluZywgZHJ5aW5nLCBjb2F0IGNhcmUsIG1hc2ssIG5haWwgdHJpbW1pbmcsIGVhciBhbmQgZXllIGNsZWFuaW5nLCBwYXcgYW5kIHNwZWNpYWwgYXJlYSBjYXJlJywn0KLRgNC40LzQvNC40L3Qsyc6J1JlbW92aW5nIG9sZCBjb2F0IGxheWVyLCB3YXNoaW5nLCBkcnlpbmcsIG5haWwgdHJpbW1pbmcsIGVhciBhbmQgZXllIGNsZWFuaW5nLCBjb2F0IHN0eWxpbmcnLCfQktGB0Y8g0L/RgNC+0LPRgNCw0LzQvNCwJzonRklSU1QgVklTSVQgKDIwLTMwIG1pbikg4oCUIOKCrDIwXG7igKIgZ2V0dGluZyB1c2VkIHRvIHRoZSB0YWJsZSBhbmQgdG9vbHNcbuKAoiBnZW50bGUgYnJ1c2hpbmdcbuKAoiBkcnllciBzb3VuZHMgYW5kIGxpZ2h0IGFpcmZsb3dcbuKAoiBleWUgYW5kIGVhciByZWZyZXNoXG7igKIgbmFpbCB0cmltXG7igKIgdHJlYXRzIGFuZCBjYWxtIGFkYXB0YXRpb25cblxuU0VDT05EIFZJU0lUICg0MC02MCBtaW4pIOKAlCDigqwzNVxu4oCiIGZpcnN0IGJhdGggYW5kIGRyeWluZ1xu4oCiIGJydXNoaW5nXG7igKIgaHlnaWVuZSBjYXJlXG7igKIgbGlnaHQgdHJpbSAvIGNvYXQgYWRqdXN0bWVudCAoaWYgbmVlZGVkKVxu4oCiIHJlaW5mb3JjaW5nIHRoZSBwb3NpdGl2ZSBleHBlcmllbmNlJ30sCiAgZXQ6eyfQktGL0YfQtdGBJzonU2lsbWFkZSBqYSBrw7VydmFkZSBwdWhhc3RhbWluZSwga8O8w7xudGUgbMO1aWthbWluZSwgaGFyamFtaW5lIChrYXNzaWRlbGUpJywn0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQnOidQZXNlbWluZSBwcm9mZXNzaW9uYWFsc2V0ZSB2YWhlbmRpdGVnYSwgw7VybiBrdWl2YXRhbWluZScsJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0JzonS8O8w7xudGUgbMO1aWthbWluZSwga8O1cnZhZGUgamEgc2lsbWFkZSBwdWhhc3RhbWluZSwgcGVzZW1pbmUsIGt1aXZhdGFtaW5lLCBrw6RwcGFkZSBqYSB0dW5kbGlrZSBwaWlya29uZGFkZSBob29sZHVzJywn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc6J0vDvMO8bnRlIGzDtWlrYW1pbmUsIGvDtXJ2YWRlIGphIHNpbG1hZGUgcHVoYXN0YW1pbmUsIHBlc2VtaW5lLCBrdWl2YXRhbWluZSwga8OkcHBhZGUgamEgdHVuZGxpa2UgcGlpcmtvbmRhZGUgaG9vbGR1cywgbW9kZWxsw7Vpa3VzJywn0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAnOidQZXNlbWluZSwga3VpdmF0YW1pbmUsIGthcnZhc3Rpa3UgaG9vbGR1cywgbWFzaywga8O8w7xudGUgbMO1aWthbWluZSwga8O1cnZhZGUgamEgc2lsbWFkZSBwdWhhc3RhbWluZSwga8OkcHBhZGUgamEgZXJpbGlzdGUgcGlpcmtvbmRhZGUgaG9vbGR1cycsJ9Ci0YDQuNC80LzQuNC90LMnOidWYW5hIGthcnZha2loaSBlZW1hbGRhbWluZSwgcGVzZW1pbmUsIGt1aXZhdGFtaW5lLCBrw7zDvG50ZSBsw7Vpa2FtaW5lLCBrw7VydmFkZSBqYSBzaWxtYWRlIHB1aGFzdGFtaW5lLCBrYXJ2YXN0aWt1IGt1anVuZGFtaW5lJywn0JLRgdGPINC/0YDQvtCz0YDQsNC80LzQsCc6J0VTSU1FTkUgS8OcTEFTVFVTICgyMC0zMCBtaW4pIOKAlCAyMCDigqxcbuKAoiB0dXR2dW1pbmUgbGF1YWdhIGphIHTDtsO2cmlpc3RhZGVnYVxu4oCiIGtlcmdlIGhhcmphbWluZVxu4oCiIGbDtsO2bmloZWxpZCBqYSBrZXJnZSDDtWh1dm9vbFxu4oCiIHNpbG1hZGUgamEga8O1cnZhZGUgdsOkcnNrZW5kdXNcbuKAoiBrw7zDvG50ZSBsw7Vpa2FtaW5lXG7igKIgbWFpdXNlZCBqYSByYWh1bGlrIGtvaGFuZW1pbmVcblxuVEVJTkUgS8OcTEFTVFVTICg0MC02MCBtaW4pIOKAlCAzNSDigqxcbuKAoiBlc2ltZW5lIHZhbm5pdGFtaW5lIGphIGt1aXZhdGFtaW5lXG7igKIgaGFyamFtaW5lXG7igKIgaMO8Z2llZW5paG9vbGR1c1xu4oCiIGtlcmdlIGzDtWlrdXMgLyBrYXJ2YSBrb3JyaWdlZXJpbWluZSAodmFqYWR1c2VsKVxu4oCiIHBvc2l0aWl2c2Uga29nZW11c2Uga2lubmlzdGFtaW5lJ30KfTsKdmFyIFNWQ19ERVNDX0NBVF9DT01QTEVYPXsKICBydTon0JzRi9GC0YzRkSwg0YHRg9GI0LrQsCwg0LLRi9GH0ZHRgdGL0LLQsNC90LjQtSwg0YHRgtGA0LjQttC60LAg0LrQvtCz0YLQtdC5LCDQsCDRgtCw0LrQttC1INC+0LHRgNCw0LHQvtGC0LrQsCDQs9C70LDQtyDQuCDRg9GI0LXQuicsCiAgZW46J1dhc2hpbmcsIGRyeWluZywgYnJ1c2hpbmcsIG5haWwgdHJpbW1pbmcsIGFuZCBleWUgYW5kIGVhciBjYXJlJywKICBldDonUGVzZW1pbmUsIGt1aXZhdGFtaW5lLCBoYXJqYW1pbmUsIGvDvMO8bnRlIGzDtWlrYW1pbmUgbmluZyBzaWxtYWRlIGphIGvDtXJ2YWRlIGhvb2xkdXMnCn07CmZ1bmN0aW9uIGdldFN2Y1RhZyhuYW1lKXtyZXR1cm4oU1ZDX1RBR0xJTkVfSTE4TltMQU5HXSYmU1ZDX1RBR0xJTkVfSTE4TltMQU5HXVtuYW1lXSl8fFNWQ19UQUdMSU5FX0kxOE4ucnVbbmFtZV18fCcnO30KZnVuY3Rpb24gZ2V0U3ZjRGVzYyhuYW1lKXsKICBpZihuYW1lPT09J9Ca0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQnICYmIGJvb2tpbmcuYnJlZWQgJiYgYm9va2luZy5icmVlZC5pbmRleE9mKCfQmtC+0YjQutCwJyk9PT0wKXsKICAgIHZhciBkPVNWQ19ERVNDX0NBVF9DT01QTEVYW0xBTkddfHxTVkNfREVTQ19DQVRfQ09NUExFWC5ydTsKICAgIHJldHVybiBkOwogIH0KICByZXR1cm4oU1ZDX0RFU0NfSTE4TltMQU5HXSYmU1ZDX0RFU0NfSTE4TltMQU5HXVtuYW1lXSl8fFNWQ19ERVNDX0kxOE4ucnVbbmFtZV18fCcnOwp9CgpmdW5jdGlvbiByZW5kZXJTdmNzKGIpewogIHZhciBsYmxFbD1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3RlcDJMYmxFbCcpOwogIGlmKGxibEVsKXsKICAgIHZhciBiYXNlTGJsPShUW0xBTkddJiZUW0xBTkddLnN0ZXAyX2xibCl8fCcwMiDCtyDQo9GB0LvRg9Cz0LAnOwogICAgbGJsRWwudGV4dENvbnRlbnQ9KGIuYnJlZWQ9PT0n0KnQtdC90LrQuCcpPyhiYXNlTGJsKycgUHVwcHkgU3RhcicpOmJhc2VMYmw7CiAgfQogIHZhciBsaXN0PWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdmNMaXN0Jyk7bGlzdC5pbm5lckhUTUw9Jyc7CiAgT2JqZWN0LmVudHJpZXMoYi5zZXJ2aWNlcykuZm9yRWFjaChmdW5jdGlvbihrdil7CiAgICB2YXIgbmFtZT1rdlswXSxwcmljZT1rdlsxXTsKCiAgICB2YXIgZGlzcGxheU5hbWU9KExBTkchPT0ncnUnJiZTVkNfVFJBTlNMQVRJT05TW25hbWVdKT9TVkNfVFJBTlNMQVRJT05TW25hbWVdW0xBTkddOm5hbWU7CiAgICB2YXIgYnRuPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2J1dHRvbicpO2J0bi5jbGFzc05hbWU9J3N2YnRuJzsKICAgIHZhciByb3c9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7cm93LmNsYXNzTmFtZT0nc3ZidG4tcm93JzsKICAgIHZhciBucz1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdzcGFuJyk7bnMuY2xhc3NOYW1lPSdzdmJ0bi1uYW1lJztucy50ZXh0Q29udGVudD1kaXNwbGF5TmFtZTsKICAgIHZhciBwcz1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdzcGFuJyk7cHMuY2xhc3NOYW1lPSdzdmJ0bi1wcmljZSc7cHMudGV4dENvbnRlbnQ9cHJpY2UrJyDigqwnOwogICAgcm93LmFwcGVuZENoaWxkKG5zKTtyb3cuYXBwZW5kQ2hpbGQocHMpOwogICAgYnRuLmFwcGVuZENoaWxkKHJvdyk7CiAgICB2YXIgZGVzYz1nZXRTdmNEZXNjKG5hbWUpOwogICAgaWYoZGVzYyl7dmFyIGRzPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NwYW4nKTtkcy5jbGFzc05hbWU9J3N2YnRuLWRlc2MnO2RzLnRleHRDb250ZW50PWRlc2M7YnRuLmFwcGVuZENoaWxkKGRzKTt9CiAgICB2YXIgdGFnPWdldFN2Y1RhZyhuYW1lKTsKICAgIGlmKHRhZyl7dmFyIHRzPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NwYW4nKTt0cy5jbGFzc05hbWU9J3N2YnRuLXRhZyc7dHMudGV4dENvbnRlbnQ9dGFnO2J0bi5hcHBlbmRDaGlsZCh0cyk7fQogICAgYnRuLm9uY2xpY2s9ZnVuY3Rpb24oKXsKICAgICAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLnN2YnRuJykuZm9yRWFjaChmdW5jdGlvbihiKXtiLmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpO30pOwogICAgICBidG4uY2xhc3NMaXN0LmFkZCgnYWN0aXZlJyk7CiAgICAgIGJvb2tpbmcuc2VydmljZT1uYW1lO2Jvb2tpbmcucHJpY2U9cHJpY2U7CiAgICAgIGZpbHRlck1hc3RlcnMoKTsKICAgICAgc2V0VGltZW91dChmdW5jdGlvbigpe2dvU3RlcCgyKTt9LDMwMCk7CiAgICB9OwogICAgbGlzdC5hcHBlbmRDaGlsZChidG4pOwogIH0pOwp9CgovLyBNYXN0ZXJzCmZ1bmN0aW9uIHNvcnRNYXN0ZXJzQnlBdmFpbGFiaWxpdHkoKXsKICB2YXIgbm93ID0gbmV3IERhdGUoKTsKICB2YXIgbW9udGggPSBub3cuZ2V0TW9udGgoKSsxLCB5ZWFyID0gbm93LmdldEZ1bGxZZWFyKCk7CiAgdmFyIGJhc2VPcmRlciA9IFsn0KLQsNGC0YzRj9C90LAnLCfQkNC70LXQutGB0LDQvdC00YDQsCcsJ9Ca0YHQtdC90LjRjycsJ9CQ0L3QvdCwJywn0JDQu9C40YHQsCcsJ9Ca0YDQuNGB0YLQuNC90LAnXTsKICB2YXIgdmlzaWJsZUJ0bnMgPSBBcnJheS5wcm90b3R5cGUuZmlsdGVyLmNhbGwoZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLm1idG4nKSwgZnVuY3Rpb24oYil7IHJldHVybiBiLnN0eWxlLmRpc3BsYXkgIT09ICdub25lJzsgfSk7CiAgaWYoIXZpc2libGVCdG5zLmxlbmd0aCkgcmV0dXJuOwogIFByb21pc2UuYWxsKHZpc2libGVCdG5zLm1hcChmdW5jdGlvbihidG4pewogICAgdmFyIG1hc3RlciA9IGJ0bi5nZXRBdHRyaWJ1dGUoJ2RhdGEtbWFzdGVyJyk7CiAgICByZXR1cm4gZmV0Y2god2luZG93LmxvY2F0aW9uLm9yaWdpbiArICcvYXBpL2F2YWlsYWJsZV9kYXlzP21vbnRoPScgKyBtb250aCArICcmeWVhcj0nICsgeWVhciArICcmbWFzdGVyPScgKyBlbmNvZGVVUklDb21wb25lbnQobWFzdGVyKSkKICAgICAgLnRoZW4oZnVuY3Rpb24ocil7IHJldHVybiByLmpzb24oKTsgfSkKICAgICAgLnRoZW4oZnVuY3Rpb24oZCl7IHJldHVybiB7YnRuOiBidG4sIG1hc3RlcjogbWFzdGVyLCBjb3VudDogKGQuYXZhaWxhYmxlfHxbXSkubGVuZ3RofTsgfSkKICAgICAgLmNhdGNoKGZ1bmN0aW9uKCl7IHJldHVybiB7YnRuOiBidG4sIG1hc3RlcjogbWFzdGVyLCBjb3VudDogLTF9OyB9KTsKICB9KSkudGhlbihmdW5jdGlvbihyZXN1bHRzKXsKICAgIHJlc3VsdHMuc29ydChmdW5jdGlvbihhLGIpewogICAgICBpZihiLmNvdW50ICE9PSBhLmNvdW50KSByZXR1cm4gYi5jb3VudCAtIGEuY291bnQ7CiAgICAgIHJldHVybiBiYXNlT3JkZXIuaW5kZXhPZihhLm1hc3RlcikgLSBiYXNlT3JkZXIuaW5kZXhPZihiLm1hc3Rlcik7CiAgICB9KTsKICAgIHJlc3VsdHMuZm9yRWFjaChmdW5jdGlvbihyLCBpKXsgci5idG4uc3R5bGUub3JkZXIgPSBpOyB9KTsKICB9KTsKfQoKZnVuY3Rpb24gZmlsdGVyTWFzdGVycygpewogIHZhciBpc0NhdCA9IGJvb2tpbmcuYnJlZWQgJiYgYm9va2luZy5icmVlZC5pbmRleE9mKCfQmtC+0YjQutCwJykgPT09IDA7CiAgdmFyIGJyZWVkID0gYm9va2luZy5icmVlZCB8fCAnJzsKICB2YXIgaXNDYXRDb21wbGV4ID0gaXNDYXQgJiYgYm9va2luZy5zZXJ2aWNlID09PSAn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc7CiAgdmFyIGFubmFFeGNsdWRlID0gWyfQnNCw0LvRjNGC0LjQv9GDJywn0J/Rg9C00LXQu9GMJywn0JnQvtGA0LonLCfQkdC40YjQvtC9Jywn0JHQvtC70L7QvdC60LAnLCfQnNCw0LvRjNGC0LjQudGB0LrQsNGPJ107CiAgdmFyIGlzQW5uYUJyZWVkID0gYnJlZWQgJiYgIWFubmFFeGNsdWRlLnNvbWUoZnVuY3Rpb24oYil7IHJldHVybiBicmVlZC5pbmRleE9mKGIpICE9PSAtMTsgfSk7CiAgdmFyIGFsZXhhbmRyYUV4Y2x1ZGUgPSBbJ9Ck0L7QutGB0YLQtdGA0YzQtdGAJywn0KbQstC10YDQs9GI0L3QsNGD0YbQtdGAJ107CiAgdmFyIGlzQWxleGFuZHJhQnJlZWQgPSAhYWxleGFuZHJhRXhjbHVkZS5zb21lKGZ1bmN0aW9uKGIpeyByZXR1cm4gYnJlZWQuaW5kZXhPZihiKSAhPT0gLTE7IH0pOwogIHZhciBrc2VuaWFFeGNsdWRlID0gWyfQn9GD0LTQtdC70YwnLCfQnNCw0LvRjNGC0LjQv9GDJywn0JnQvtGA0LonLCfQkdC+0LvQvtC90LrQsCddOwogIHZhciBpc0tzZW5pYUJyZWVkID0gIWtzZW5pYUV4Y2x1ZGUuc29tZShmdW5jdGlvbihiKXsgcmV0dXJuIGJyZWVkLmluZGV4T2YoYikgIT09IC0xOyB9KTsKICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcubWJ0bicpLmZvckVhY2goZnVuY3Rpb24oYnRuKXsKICAgIHZhciBtYXN0ZXIgPSBidG4uZ2V0QXR0cmlidXRlKCdkYXRhLW1hc3RlcicpOwogICAgdmFyIGlzVHJpbW1pbmcgPSBib29raW5nLnNlcnZpY2UgPT09ICfQotGA0LjQvNC80LjQvdCzJzsKICAgIGlmKGlzQ2F0Q29tcGxleCl7CiAgICAgIGJ0bi5zdHlsZS5kaXNwbGF5ID0gKG1hc3RlciA9PT0gJ9Ci0LDRgtGM0Y/QvdCwJyB8fCBtYXN0ZXIgPT09ICfQmtGB0LXQvdC40Y8nKSA/ICcnIDogJ25vbmUnOwogICAgICByZXR1cm47CiAgICB9CiAgICBpZihtYXN0ZXIgPT09ICfQkNC70LjRgdCwJyl7CiAgICAgIGJ0bi5zdHlsZS5kaXNwbGF5ID0gaXNDYXQgPyAnJyA6ICdub25lJzsKICAgIH0gZWxzZSBpZihtYXN0ZXIgPT09ICfQkNC90L3QsCcpewogICAgICBidG4uc3R5bGUuZGlzcGxheSA9IChpc0FubmFCcmVlZCAmJiAhaXNUcmltbWluZykgPyAnJyA6ICdub25lJzsKICAgIH0gZWxzZSBpZihtYXN0ZXIgPT09ICfQkNC70LXQutGB0LDQvdC00YDQsCcpewogICAgICBidG4uc3R5bGUuZGlzcGxheSA9IChpc0FsZXhhbmRyYUJyZWVkICYmICFpc1RyaW1taW5nICYmICFpc0NhdCkgPyAnJyA6ICdub25lJzsKICAgIH0gZWxzZSBpZihtYXN0ZXIgPT09ICfQmtGB0LXQvdC40Y8nKXsKICAgICAgYnRuLnN0eWxlLmRpc3BsYXkgPSBpc0tzZW5pYUJyZWVkID8gJycgOiAnbm9uZSc7CiAgICB9IGVsc2UgaWYoaXNUcmltbWluZyl7CiAgICAgIGJ0bi5zdHlsZS5kaXNwbGF5ID0gJ25vbmUnOwogICAgfSBlbHNlIHsKICAgICAgYnRuLnN0eWxlLmRpc3BsYXkgPSAnJzsKICAgIH0KICB9KTsKICBzb3J0TWFzdGVyc0J5QXZhaWxhYmlsaXR5KCk7Cn0KCmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5tYnRuJykuZm9yRWFjaChmdW5jdGlvbihidG4pewogIGJ0bi5vbmNsaWNrPWZ1bmN0aW9uKCl7CiAgICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcubWJ0bicpLmZvckVhY2goZnVuY3Rpb24oYil7Yi5jbGFzc0xpc3QucmVtb3ZlKCdhY3RpdmUnKTt9KTsKICAgIGJ0bi5jbGFzc0xpc3QuYWRkKCdhY3RpdmUnKTsKICAgIGJvb2tpbmcubWFzdGVyPWJ0bi5nZXRBdHRyaWJ1dGUoJ2RhdGEtbWFzdGVyJyk7CiAgICBzZXRUaW1lb3V0KGZ1bmN0aW9uKCl7Z29TdGVwKDMpO30sMzAwKTsKICB9Owp9KTsKCi8vIEdyb29tIGhpc3RvcnkKZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmdidG4nKS5mb3JFYWNoKGZ1bmN0aW9uKGJ0bil7CiAgYnRuLm9uY2xpY2s9ZnVuY3Rpb24oKXsKICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5nYnRuJykuZm9yRWFjaChmdW5jdGlvbihiKXtiLmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpO30pOwogICAgYnRuLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpOwogICAgYm9va2luZy5ncm9vbUhpc3Rvcnk9YnRuLmdldEF0dHJpYnV0ZSgnZGF0YS12YWwnKTsKICAgIHNldFRpbWVvdXQoZnVuY3Rpb24oKXtnb1N0ZXAoNCk7YnVpbGRDYWwoKTt9LDMwMCk7CiAgfTsKfSk7CgovLyBDYWxlbmRhcgpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgncHJldk0nKS5vbmNsaWNrPWZ1bmN0aW9uKCl7Y00tLTtpZihjTTwwKXtjTT0xMTtjWS0tO31idWlsZENhbCgpO307CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCduZXh0TScpLm9uY2xpY2s9ZnVuY3Rpb24oKXtjTSsrO2lmKGNNPjExKXtjTT0wO2NZKys7fWJ1aWxkQ2FsKCk7fTsKCnZhciBhdmFpbGFibGVEYXlzID0gW107CgpmdW5jdGlvbiBsb2FkQXZhaWxhYmxlRGF5cygpIHsKICB2YXIgbWFzdGVyID0gYm9va2luZy5tYXN0ZXI7CiAgaWYgKCFtYXN0ZXIpIHJldHVybjsKICBhdmFpbGFibGVEYXlzID0gW107CiAgZmV0Y2god2luZG93LmxvY2F0aW9uLm9yaWdpbiArICcvYXBpL2F2YWlsYWJsZV9kYXlzP21vbnRoPScgKyAoY00rMSkgKyAnJnllYXI9JyArIGNZICsgJyZtYXN0ZXI9JyArIGVuY29kZVVSSUNvbXBvbmVudChtYXN0ZXIpKQogICAgLnRoZW4oZnVuY3Rpb24ocil7IHJldHVybiByLmpzb24oKTsgfSkKICAgIC50aGVuKGZ1bmN0aW9uKGRhdGEpewogICAgICBhdmFpbGFibGVEYXlzID0gZGF0YS5hdmFpbGFibGUgfHwgW107CiAgICAgIG1hcmtBdmFpbGFibGVEYXlzKCk7CiAgICB9KQogICAgLmNhdGNoKGZ1bmN0aW9uKCl7IGF2YWlsYWJsZURheXMgPSBbXTsgfSk7Cn0KCmZ1bmN0aW9uIG1hcmtBdmFpbGFibGVEYXlzKCkgewogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5jZCcpLmZvckVhY2goZnVuY3Rpb24oYyl7aWYoIWMuY2xhc3NMaXN0LmNvbnRhaW5zKCdkaXMnKSljLmNsYXNzTGlzdC5yZW1vdmUoJ3NlbCcpO30pOwogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5jZDpub3QoLmRpcyk6bm90KC5jZG4pOm5vdCgucGFkKScpLmZvckVhY2goZnVuY3Rpb24oZWwpIHsKICAgIHZhciBkYXkgPSBlbC50ZXh0Q29udGVudC50cmltKCk7CiAgICBpZiAoIWRheSB8fCBpc05hTihwYXJzZUludChkYXkpKSkgcmV0dXJuOwogICAgdmFyIGRhdGVTdHIgPSBTdHJpbmcocGFyc2VJbnQoZGF5KSkucGFkU3RhcnQoMiwnMCcpICsgJy4nICsgU3RyaW5nKGNNKzEpLnBhZFN0YXJ0KDIsJzAnKSArICcuJyArIGNZOwogICAgaWYgKGF2YWlsYWJsZURheXMuaW5kZXhPZihkYXRlU3RyKSAhPT0gLTEpIHsKICAgICAgZWwuY2xhc3NMaXN0LmFkZCgnYXZhaWwnKTsKICAgICAgZWwuY2xhc3NMaXN0LnJlbW92ZSgnYnVzeScpOwogICAgfSBlbHNlIHsKICAgICAgZWwuY2xhc3NMaXN0LmFkZCgnYnVzeScpOwogICAgICBlbC5jbGFzc0xpc3QucmVtb3ZlKCdhdmFpbCcpOwogICAgfQogIH0pOwp9CgpmdW5jdGlvbiBidWlsZENhbCgpewogIGxvYWRBdmFpbGFibGVEYXlzKCk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NhbE0nKS50ZXh0Q29udGVudD1NT05USFNbY01dKycgJytjWTsKICBib29raW5nLmRhdGU9Jyc7IGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5jZCcpLmZvckVhY2goZnVuY3Rpb24oYyl7Yy5jbGFzc0xpc3QucmVtb3ZlKCdzZWwnKTtjLmNsYXNzTGlzdC5yZW1vdmUoJ2F2YWlsJyk7Yy5jbGFzc0xpc3QucmVtb3ZlKCdidXN5Jyk7fSk7CiAgdmFyIGc9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NhbEcnKTtnLmlubmVySFRNTD0nJzsKICBbJ9Cf0L0nLCfQktGCJywn0KHRgCcsJ9Cn0YInLCfQn9GCJywn0KHQsScsJ9CS0YEnXS5mb3JFYWNoKGZ1bmN0aW9uKGQpewogICAgdmFyIGVsPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO2VsLmNsYXNzTmFtZT0nY2RuJztlbC50ZXh0Q29udGVudD1kO2cuYXBwZW5kQ2hpbGQoZWwpOwogIH0pOwogIHZhciBmaXJzdD1uZXcgRGF0ZShjWSxjTSwxKS5nZXREYXkoKTsKICB2YXIgZGF5cz1uZXcgRGF0ZShjWSxjTSsxLDApLmdldERhdGUoKTsKICB2YXIgc3RhcnQ9Zmlyc3Q9PT0wPzY6Zmlyc3QtMTsKICB2YXIgdG9kYXk9bmV3IERhdGUoKTsKICBmb3IodmFyIGk9MDtpPHN0YXJ0O2krKyl7dmFyIGVsPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO2VsLmNsYXNzTmFtZT0nY2QgcGFkJztnLmFwcGVuZENoaWxkKGVsKTt9CiAgZm9yKHZhciBkYXk9MTtkYXk8PWRheXM7ZGF5KyspewogICAgdmFyIGVsPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO2VsLmNsYXNzTmFtZT0nY2QnOwogICAgdmFyIGRhdGU9bmV3IERhdGUoY1ksY00sZGF5KTsKICAgIHZhciBpc1Bhc3Q9ZGF0ZTxuZXcgRGF0ZSh0b2RheS5nZXRGdWxsWWVhcigpLHRvZGF5LmdldE1vbnRoKCksdG9kYXkuZ2V0RGF0ZSgpKTsKICAgIHZhciBpbm5lcj1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdkaXYnKTtpbm5lci5jbGFzc05hbWU9J2NkLWlubmVyJztpbm5lci50ZXh0Q29udGVudD1kYXk7ZWwuYXBwZW5kQ2hpbGQoaW5uZXIpOwogICAgaWYoaXNQYXN0KXtlbC5jbGFzc0xpc3QuYWRkKCdkaXMnKTt9CiAgICBlbHNlewogICAgICBpZihkYXRlLnRvRGF0ZVN0cmluZygpPT09dG9kYXkudG9EYXRlU3RyaW5nKCkpZWwuY2xhc3NMaXN0LmFkZCgndG9kJyk7CiAgICAgIChmdW5jdGlvbihkLCBlbFJlZil7CiAgICAgICAgZWxSZWYub25jbGljaz1mdW5jdGlvbigpewogICAgICAgICAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmNkJykuZm9yRWFjaChmdW5jdGlvbihjKXtjLmNsYXNzTGlzdC5yZW1vdmUoJ3NlbCcpO30pOwogICAgICAgICAgZWxSZWYuY2xhc3NMaXN0LmFkZCgnc2VsJyk7CiAgICAgICAgICBib29raW5nLmRhdGU9U3RyaW5nKGQpLnBhZFN0YXJ0KDIsJzAnKSsnLicrU3RyaW5nKGNNKzEpLnBhZFN0YXJ0KDIsJzAnKSsnLicrY1k7CiAgICAgICAgICBzaG93VGltZXMoKTsKICAgICAgICB9OwogICAgICB9KShkYXksIGVsKTsKICAgIH0KICAgIGcuYXBwZW5kQ2hpbGQoZWwpOwogIH0KICAvLyBmaWxsIHRyYWlsaW5nIGNlbGxzIHRvIGNvbXBsZXRlIGxhc3QgZ3JpZCByb3cKICB2YXIgdG90YWwgPSBzdGFydCArIGRheXM7CiAgdmFyIHRyYWlsID0gKDcgLSAodG90YWwgJSA3KSkgJSA3OwogIGZvcih2YXIgdD0wO3Q8dHJhaWw7dCsrKXt2YXIgZXA9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7ZXAuY2xhc3NOYW1lPSdjZCBwYWQnO2cuYXBwZW5kQ2hpbGQoZXApO30KfQoKZnVuY3Rpb24gc2hvd1RpbWVzKCl7CiAgdmFyIHRnPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCd0aW1lRycpOwogIHRnLmlubmVySFRNTD0nPGRpdiBjbGFzcz0ibG9hZGluZy1zbG90cyI+4o+zINCX0LDQs9GA0YPQttCw0LXQvCDRgNCw0YHQv9C40YHQsNC90LjQtS4uLjwvZGl2Pic7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3RpbWVTZWMnKS5zdHlsZS5kaXNwbGF5PSdibG9jayc7CgogIHZhciB1cmwgPSB3aW5kb3cubG9jYXRpb24ub3JpZ2luICsgIi9hcGkvc2xvdHMiICsgJz9hY3Rpb249c2xvdHMmZGF0ZT0nICsgZW5jb2RlVVJJQ29tcG9uZW50KGJvb2tpbmcuZGF0ZSkgKyAnJm1hc3Rlcj0nICsgZW5jb2RlVVJJQ29tcG9uZW50KGJvb2tpbmcubWFzdGVyKTsKCiAgZmV0Y2godXJsKQogICAgLnRoZW4oZnVuY3Rpb24ocil7cmV0dXJuIHIuanNvbigpO30pCiAgICAudGhlbihmdW5jdGlvbihkYXRhKXsKICAgICAgdmFyIHNsb3RzID0gKGRhdGEuc2xvdHMgJiYgZGF0YS5zbG90cy5sZW5ndGggPiAwKSA/IGRhdGEuc2xvdHMgOiBbXTsKICAgICAgcmVuZGVyVGltZVNsb3RzKHNsb3RzKTsKICAgIH0pCiAgICAuY2F0Y2goZnVuY3Rpb24oKXsKICAgICAgcmVuZGVyVGltZVNsb3RzKFtdKTsKICAgIH0pOwp9CgpmdW5jdGlvbiByZW5kZXJUaW1lU2xvdHMoc2xvdHMpewogIHZhciB0Zz1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgndGltZUcnKTt0Zy5pbm5lckhUTUw9Jyc7CiAgaWYoc2xvdHMubGVuZ3RoPT09MCl7CiAgICB0Zy5pbm5lckhUTUw9JzxkaXYgY2xhc3M9ImxvYWRpbmctc2xvdHMiPtCd0LXRgiDQtNC+0YHRgtGD0L/QvdGL0YUg0YHQu9C+0YLQvtCyINC90LAg0Y3RgtGDINC00LDRgtGDPC9kaXY+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyIiBvbmNsaWNrPSJzaG93U2NyZWVuKFwnaG9tZVNjcmVlblwnKSIgc3R5bGU9Im1hcmdpbi10b3A6OHB4OyI+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLWljb24iPvCfkL48L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItdGV4dCI+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLXRpdGxlIj7QndC1INC90LDRiNC70Lgg0L/QvtC00YXQvtC00Y/RidC10LUg0LLRgNC10LzRjz88L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItc3ViIj7QodCy0Y/QttC40YLQtdGB0Ywg0YEg0L3QsNC80Lgg0LvRjtCx0YvQvCDRg9C00L7QsdC90YvQvCDRgdC/0L7RgdC+0LHQvtC8IOKAlCDQvNGLINC/0L7QtNCx0LXRgNGR0Lwg0YPQtNC+0LHQvdC+0LUg0LLRgNC10LzRjzwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci1hcnJvdyI+4oaSPC9kaXY+PC9kaXY+JzsKICAgIHJldHVybjsKICB9CiAgc2xvdHMuZm9yRWFjaChmdW5jdGlvbih0KXsKICAgIHZhciBidG49ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnYnV0dG9uJyk7YnRuLmNsYXNzTmFtZT0ndGJ0bic7YnRuLnRleHRDb250ZW50PXQ7CiAgICBidG4ub25jbGljaz1mdW5jdGlvbigpewogICAgICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcudGJ0bicpLmZvckVhY2goZnVuY3Rpb24oYil7Yi5jbGFzc0xpc3QucmVtb3ZlKCdhY3RpdmUnKTt9KTsKICAgICAgYnRuLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpO2Jvb2tpbmcudGltZT10OwogICAgICBzZXRUaW1lb3V0KGZ1bmN0aW9uKCl7Z29TdGVwKDUpO2J1aWxkU3VtKCk7fSwzMDApOwogICAgfTsKICAgIHRnLmFwcGVuZENoaWxkKGJ0bik7CiAgfSk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3RpbWVTZWMnKS5zY3JvbGxJbnRvVmlldyh7YmVoYXZpb3I6J3Ntb290aCcsYmxvY2s6J25lYXJlc3QnfSk7Cn0KCmZ1bmN0aW9uIGJ1aWxkU3VtKCl7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N1bUJsb2NrJykuaW5uZXJIVE1MPQogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fYnJlZWQrJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrKGJvb2tpbmcuYnJlZWREaXNwbGF5fHxib29raW5nLmJyZWVkKSsnPC9zcGFuPjwvZGl2PicrCiAgICAnPGRpdiBjbGFzcz0ic3IiPjxzcGFuIGNsYXNzPSJzbCI+JytUW0xBTkddLnN1bV9zZXJ2aWNlKyc8L3NwYW4+PHNwYW4gY2xhc3M9InN2Ij4nKygoTEFORyE9PSdydScmJlNWQ19UUkFOU0xBVElPTlNbYm9va2luZy5zZXJ2aWNlXSk/U1ZDX1RSQU5TTEFUSU9OU1tib29raW5nLnNlcnZpY2VdW0xBTkddOmJvb2tpbmcuc2VydmljZSkrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fbWFzdGVyKyc8L3NwYW4+PHNwYW4gY2xhc3M9InN2Ij4nK2Jvb2tpbmcubWFzdGVyKyc8L3NwYW4+PC9kaXY+JysKICAgICc8ZGl2IGNsYXNzPSJzciI+PHNwYW4gY2xhc3M9InNsIj4nK1RbTEFOR10uc3VtX2dyb29tKyc8L3NwYW4+PHNwYW4gY2xhc3M9InN2Ij4nK2Jvb2tpbmcuZ3Jvb21IaXN0b3J5Kyc8L3NwYW4+PC9kaXY+JysKICAgICc8ZGl2IGNsYXNzPSJzciI+PHNwYW4gY2xhc3M9InNsIj4nK1RbTEFOR10uc3VtX2RhdGUrJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrYm9va2luZy5kYXRlKyc8L3NwYW4+PC9kaXY+JysKICAgICc8ZGl2IGNsYXNzPSJzciI+PHNwYW4gY2xhc3M9InNsIj4nK1RbTEFOR10uc3VtX3RpbWUrJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrYm9va2luZy50aW1lKyc8L3NwYW4+PC9kaXY+JysKICAgICc8ZGl2IGNsYXNzPSJzciI+PHNwYW4gY2xhc3M9InNsIj4nK1RbTEFOR10uc3VtX3ByaWNlKyc8L3NwYW4+PHNwYW4gY2xhc3M9InNwIj4nK2Jvb2tpbmcucHJpY2UrJyDigqw8L3NwYW4+PC9kaXY+JzsKfQoKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NvbmZpcm1CdG4nKS5vbmNsaWNrID0gZnVuY3Rpb24oKXsKICB2YXIgbmFtZT1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY05hbWUnKS52YWx1ZTsKICB2YXIgcGhvbmU9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NQaG9uZScpLnZhbHVlOwogIGlmKCFuYW1lfHwhcGhvbmUpe2FsZXJ0KFRbTEFOR10uYWxlcnRfZmlsbCk7cmV0dXJuO30KICBpZighL15cK1xkezEwLH0kLy50ZXN0KHBob25lLnRyaW0oKSkpe2FsZXJ0KFRbTEFOR10uYWxlcnRfcGhvbmUpO3JldHVybjt9CiAgYm9va2luZy5uYW1lPW5hbWU7IGJvb2tpbmcucGhvbmU9cGhvbmU7IGJvb2tpbmcuZW1haWw9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NFbWFpbCcpLnZhbHVlOyBib29raW5nLnBldD1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY1BldCcpLnZhbHVlOyBib29raW5nLmxhbmc9TEFORzsKICBib29raW5nLmR1cmF0aW9uID0gYm9va2luZy5icmVlZCA9PT0gJ9Cp0LXQvdC60LgnID8gNjAgOiAoYm9va2luZy5icmVlZCAmJiBib29raW5nLmJyZWVkLmluZGV4T2YoJ9Ca0L7RiNC60LAnKSA9PT0gMCA/IDEyMCA6IDE4MCk7CiAgdmFyIGJ0bj1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY29uZmlybUJ0bicpOwogIGJ0bi50ZXh0Q29udGVudD1UW0xBTkddLnNlbmRpbmc7IGJ0bi5kaXNhYmxlZD10cnVlOwogIGZldGNoKFJBSUxXQVksIHsKICAgIG1ldGhvZDonUE9TVCcsCiAgICBoZWFkZXJzOnsnQ29udGVudC1UeXBlJzonYXBwbGljYXRpb24vanNvbid9LAogICAgYm9keTpKU09OLnN0cmluZ2lmeShib29raW5nKQogIH0pLnRoZW4oZnVuY3Rpb24oKXtzaG93U3VjY2VzcygpO30pLmNhdGNoKGZ1bmN0aW9uKCl7c2hvd1N1Y2Nlc3MoKTt9KTsKfTsKCmZ1bmN0aW9uIHNob3dTdWNjZXNzKCl7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2JrNScpLmNsYXNzTmFtZT0nc3RlcCc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N1Y0Jsb2NrJykuY2xhc3NMaXN0LmFkZCgnc2hvdycpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdwcm9ncmVzcycpLnN0eWxlLmRpc3BsYXk9J25vbmUnOwp9CgpmdW5jdGlvbiByZXNldEFsbCgpewogIGJvb2tpbmc9e2JyZWVkOicnLGJyZWVkRGlzcGxheTonJyxzZXJ2aWNlOicnLHByaWNlOjAsbWFzdGVyOicnLGdyb29tSGlzdG9yeTonJyxkYXRlOicnLHRpbWU6JycsbGFuZzoncnUnfTsKICBzZWxCcmVlZD1udWxsOyBpbnAudmFsdWU9Jyc7IGNsci5jbGFzc0xpc3QucmVtb3ZlKCdzaG93Jyk7CiAgYmFkZ2UuY2xhc3NMaXN0LnJlbW92ZSgnc2hvdycpOyBiYWRnZS5pbm5lckhUTUw9Jyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y1NlYycpLnN0eWxlLmRpc3BsYXk9J25vbmUnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCd0aW1lU2VjJykuc3R5bGUuZGlzcGxheT0nbm9uZSc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N1Y0Jsb2NrJykuY2xhc3NMaXN0LnJlbW92ZSgnc2hvdycpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdwcm9ncmVzcycpLnN0eWxlLmRpc3BsYXk9J2ZsZXgnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjTmFtZScpLnZhbHVlPScnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjUGhvbmUnKS52YWx1ZT0nJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY0VtYWlsJykudmFsdWU9Jyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NQZXQnKS52YWx1ZT0nJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY29uZmlybUJ0bicpLnRleHRDb250ZW50PVRbTEFOR10uY29uZmlybV9idG47CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NvbmZpcm1CdG4nKS5kaXNhYmxlZD1mYWxzZTsKICBnb1N0ZXAoMSk7Cn0KCnZhciBMQU5HID0gbG9jYWxTdG9yYWdlLmdldEl0ZW0oJ3JqbGFuZycpIHx8ICdydSc7CnZhciBUID0gewogIHJ1OnsKICAgIGxvZ29fdGFnOifQn9GA0LXQvNC40LDQu9GM0L3Ri9C5INCz0YDRg9C80LjQvdCzLTxicj7RgdCw0LvQvtC9INCyINCi0LDQu9C70LjQvdC1JywKICAgIGNob29zZV9ob3c6J0Nob29zZSBob3cgdG8gY29ubmVjdCcsCiAgICBib29rX29ubGluZTon0J7QvdC70LDQudC9INCx0YDQvtC90LjRgNC+0LLQsNC90LjQtScsCiAgICBib29rX2Zsb3c6J9Cf0L7RgNC+0LTQsCDihpIg0KPRgdC70YPQs9CwIOKGkiDQnNCw0YHRgtC10YAg4oaSINCS0YDQtdC80Y8nLAogICAgb3JfY29udGFjdDon0LjQu9C4INGB0LLRj9C20LjRgtC10YHRjCDRgSDQvdCw0LzQuCcsCiAgICBjYWxsX3VzOifQn9C+0LfQstC+0L3QuNGC0LUg0L3QsNC8JywKICAgIGJhY2s6J+KGkCDQndCw0LfQsNC0JywKICAgIGxvZ29fc3ViOidHcm9vbWluZyDCtyDQotCw0LvQu9C40L0nLAogICAgcHNfc2VydmljZTon0KPRgdC70YPQs9CwJyxwc19tYXN0ZXI6J9Cc0LDRgdGC0LXRgCcscHNfcGV0OifQn9C40YLQvtC80LXRhicscHNfZGF0ZTon0JTQsNGC0LAnLHBzX2RldGFpbHM6J9CU0LDQvdC90YvQtScsCiAgICBzdGVwMV9sYmw6JzAxIMK3INCf0L7RgNC+0LTQsCcsCiAgICBicmVlZF9waDon0J3QsNGH0L3QuNGC0LUg0LLQstC+0LTQuNGC0Ywg0L/QvtGA0L7QtNGDLi4uJywKICAgIHN0ZXAyX2xibDonMDIgwrcg0KPRgdC70YPQs9CwJywKICAgIHN0ZXAyX21hc3Rlcjon0JLRi9Cx0LXRgNC40YLQtSDQvNCw0YHRgtC10YDQsCcsCiAgICBzdGVwM19sYmw6J9Ca0LDQuiDQtNCw0LLQvdC+INCy0Ysg0L/QvtGB0LXRidCw0LvQuCDQs9GA0YPQvNC40L3Qsz8nLAogICAgZzE6J9Cf0LXRgNCy0YvQuSDRgNCw0LcnLGcyOifQntGCIDEg0LTQviAzINC80LXRgdGP0YbQtdCyJyxnMzon0J7RgiAzINC00L4gNiDQvNC10YHRj9GG0LXQsicsZzQ6J9CR0L7Qu9C10LUgNiDQvNC10YHRj9GG0LXQsicsCiAgICBzdGVwNF9sYmw6J9CS0YvQsdC10YDQuNGC0LUg0LTQsNGC0YMnLAogICAgY2FsX2F2YWlsOifQldGB0YLRjCDRgdCy0L7QsdC+0LTQvdC+0LUg0LLRgNC10LzRjycsY2FsX25vbmU6J9Ch0LLQvtCx0L7QtNC90L7Qs9C+INCy0YDQtdC80LXQvdC4INC90LXRgicsCiAgICBzdGVwNF90aW1lOifQktGL0LHQtdGA0LjRgtC1INCy0YDQtdC80Y8nLAogICAgc3RlcDVfbGJsOifQktCw0YjQuCDQtNCw0L3QvdGL0LUnLAogICAgbGJsX25hbWU6J9CY0LzRjycscGhfbmFtZTon0JLQsNGI0LUg0LjQvNGPJywKICAgIGxibF9waG9uZTon0KLQtdC70LXRhNC+0L0nLGxibF9lbWFpbDonRW1haWwnLAogICAgbGJsX3BldDon0JrQu9C40YfQutCwINC/0LjRgtC+0LzRhtCwJyxwaF9vcHRpb25hbDon0J3QtdC+0LHRj9C30LDRgtC10LvRjNC90L4nLAogICAgY29uZmlybV9idG46J9Cf0L7QtNGC0LLQtdGA0LTQuNGC0Ywg0LfQsNC/0LjRgdGMJywKICAgIHN1Y2Nlc3NfdGl0bGU6J9CX0LDQv9C40YHRjCDQv9GA0LjQvdGP0YLQsCEnLAogICAgc3VjY2Vzc19zdWI6J9Cc0Ysg0YHQstGP0LbQtdC80YHRjyDRgSDQstCw0LzQuCDQtNC70Y8g0L/QvtC00YLQstC10YDQttC00LXQvdC40Y8uPGJyPtCh0L/QsNGB0LjQsdC+LCDRh9GC0L4g0LLRi9Cx0YDQsNC70LggUiZhbXA7SiBHcm9vbWluZyEnLAogICAgdG9faG9tZTon4oaQINCd0LAg0LPQu9Cw0LLQvdGD0Y4nLAogICAgYWxlcnRfZmlsbDon0JLQstC10LTQuNGC0LUg0LjQvNGPINC4INGC0LXQu9C10YTQvtC9JyxhbGVydF9waG9uZTon0JLQstC10LTQuNGC0LUg0L3QvtC80LXRgCDQsiDRhNC+0YDQvNCw0YLQtSArMzcyMTIzNDU2NzgnLAogICAgc2VuZGluZzon0J7RgtC/0YDQsNCy0LvRj9C10LwuLi4nLAogICAgc3VtX2JyZWVkOifQn9C+0YDQvtC00LAnLHN1bV9zZXJ2aWNlOifQo9GB0LvRg9Cz0LAnLHN1bV9tYXN0ZXI6J9Cc0LDRgdGC0LXRgCcsc3VtX2dyb29tOifQn9C+0YHQu9C10LTQvdC40Lkg0LPRgNGD0LwnLHN1bV9kYXRlOifQlNCw0YLQsCcsc3VtX3RpbWU6J9CS0YDQtdC80Y8nLHN1bV9wcmljZTon0KHRgtC+0LjQvNC+0YHRgtGMJywKICAgIG1vbnRoczpbJ9Cv0L3QstCw0YDRjCcsJ9Ck0LXQstGA0LDQu9GMJywn0JzQsNGA0YInLCfQkNC/0YDQtdC70YwnLCfQnNCw0LknLCfQmNGO0L3RjCcsJ9CY0Y7Qu9GMJywn0JDQstCz0YPRgdGCJywn0KHQtdC90YLRj9Cx0YDRjCcsJ9Ce0LrRgtGP0LHRgNGMJywn0J3QvtGP0LHRgNGMJywn0JTQtdC60LDQsdGA0YwnXQogIH0sCiAgZW46ewogICAgbG9nb190YWc6J1ByZW1pdW0gZ3Jvb21pbmc8YnI+c2Fsb24gaW4gVGFsbGlubicsCiAgICBjaG9vc2VfaG93OidDaG9vc2UgaG93IHRvIGNvbm5lY3QnLAogICAgYm9va19vbmxpbmU6J0Jvb2sgT25saW5lJywKICAgIGJvb2tfZmxvdzonQnJlZWQg4oaSIFNlcnZpY2Ug4oaSIE1hc3RlciDihpIgVGltZScsCiAgICBvcl9jb250YWN0OidvciBjb250YWN0IHVzJywKICAgIGNhbGxfdXM6J0NhbGwgVXMnLAogICAgYmFjazon4oaQIEJhY2snLAogICAgbG9nb19zdWI6J0dyb29taW5nIMK3IFRhbGxpbm4nLAogICAgcHNfc2VydmljZTonU2VydmljZScscHNfbWFzdGVyOidNYXN0ZXInLHBzX3BldDonUGV0Jyxwc19kYXRlOidEYXRlJyxwc19kZXRhaWxzOidEZXRhaWxzJywKICAgIHN0ZXAxX2xibDonMDEgwrcgRG9nIGJyZWVkJywKICAgIGJyZWVkX3BoOidTdGFydCB0eXBpbmcgYnJlZWQuLi4nLAogICAgc3RlcDJfbGJsOicwMiDCtyBTZXJ2aWNlJywKICAgIHN0ZXAyX21hc3RlcjonQ2hvb3NlIG1hc3RlcicsCiAgICBzdGVwM19sYmw6J0hvdyBsb25nIGFnbyB3YXMgeW91ciBsYXN0IGdyb29taW5nPycsCiAgICBnMTonRmlyc3QgdGltZScsZzI6JzHigJMzIG1vbnRocyBhZ28nLGczOicz4oCTNiBtb250aHMgYWdvJyxnNDonT3ZlciA2IG1vbnRocycsCiAgICBzdGVwNF9sYmw6J0Nob29zZSBkYXRlJywKICAgIGNhbF9hdmFpbDonQXZhaWxhYmxlJyxjYWxfbm9uZTonTm90IGF2YWlsYWJsZScsCiAgICBzdGVwNF90aW1lOidDaG9vc2UgdGltZScsCiAgICBzdGVwNV9sYmw6J1lvdXIgZGV0YWlscycsCiAgICBsYmxfbmFtZTonTmFtZScscGhfbmFtZTonWW91ciBuYW1lJywKICAgIGxibF9waG9uZTonUGhvbmUnLGxibF9lbWFpbDonRW1haWwnLAogICAgbGJsX3BldDoiUGV0J3MgbmFtZSIscGhfb3B0aW9uYWw6J09wdGlvbmFsJywKICAgIGNvbmZpcm1fYnRuOidDb25maXJtIGJvb2tpbmcnLAogICAgc3VjY2Vzc190aXRsZTonQm9va2luZyBjb25maXJtZWQhJywKICAgIHN1Y2Nlc3Nfc3ViOidXZSB3aWxsIGNvbnRhY3QgeW91IHRvIGNvbmZpcm0uPGJyPlRoYW5rIHlvdSBmb3IgY2hvb3NpbmcgUiZhbXA7SiBHcm9vbWluZyEnLAogICAgdG9faG9tZTon4oaQIEhvbWUnLAogICAgYWxlcnRfZmlsbDonUGxlYXNlIGVudGVyIG5hbWUgYW5kIHBob25lJyxhbGVydF9waG9uZTonRW50ZXIgcGhvbmUgbnVtYmVyIGluIGZvcm1hdCArMzcyMTIzNDU2NzgnLAogICAgc2VuZGluZzonU2VuZGluZy4uLicsCiAgICBzdW1fYnJlZWQ6J0JyZWVkJyxzdW1fc2VydmljZTonU2VydmljZScsc3VtX21hc3RlcjonTWFzdGVyJyxzdW1fZ3Jvb206J0xhc3QgZ3Jvb21pbmcnLHN1bV9kYXRlOidEYXRlJyxzdW1fdGltZTonVGltZScsc3VtX3ByaWNlOidQcmljZScsCiAgICBtb250aHM6WydKYW51YXJ5JywnRmVicnVhcnknLCdNYXJjaCcsJ0FwcmlsJywnTWF5JywnSnVuZScsJ0p1bHknLCdBdWd1c3QnLCdTZXB0ZW1iZXInLCdPY3RvYmVyJywnTm92ZW1iZXInLCdEZWNlbWJlciddCiAgfSwKICBldDp7CiAgICBsb2dvX3RhZzonRXNtYWtsYXNzaWxpbmUgaG9vbGR1c3RlZW51czxicj5UYWxsaW5uYXMnLAogICAgY2hvb3NlX2hvdzonVmFsaSDDvGhlbmR1c3ZpaXMnLAogICAgYm9va19vbmxpbmU6J0Jyb25lZXJpIHZlZWJpcycsCiAgICBib29rX2Zsb3c6J1TDtXVnIOKGkiBUZWVudXMg4oaSIE1laXN0ZXIg4oaSIEFlZycsCiAgICBvcl9jb250YWN0Oid2w7VpIHbDtXRhIMO8aGVuZHVzdCcsCiAgICBjYWxsX3VzOidIZWxpc3RhIG1laWxlJywKICAgIGJhY2s6J+KGkCBUYWdhc2knLAogICAgbG9nb19zdWI6J0dyb29taW5nIMK3IFRhbGxpbm4nLAogICAgcHNfc2VydmljZTonVGVlbnVzJyxwc19tYXN0ZXI6J01laXN0ZXInLHBzX3BldDonTGVtbWlrbG9vbScscHNfZGF0ZTonS3V1cMOkZXYnLHBzX2RldGFpbHM6J0FuZG1lZCcsCiAgICBzdGVwMV9sYmw6JzAxIMK3IEtvZXJhIHTDtXVnJywKICAgIGJyZWVkX3BoOidBbHVzdGFnZSB0w7V1IHNpc2VzdGFtaXN0Li4uJywKICAgIHN0ZXAyX2xibDonMDIgwrcgVGVlbnVzJywKICAgIHN0ZXAyX21hc3RlcjonVmFsaSBtZWlzdGVyJywKICAgIHN0ZXAzX2xibDonTWlsbGFsIGvDpGlzaXRlIHZpaW1hdGkgZ3Jvb21pbmd1cz8nLAogICAgZzE6J0VzaW1lc3Qga29yZGEnLGcyOicx4oCTMyBrdXVkIHRhZ2FzaScsZzM6JzPigJM2IGt1dWQgdGFnYXNpJyxnNDonw5xsZSA2IGt1dScsCiAgICBzdGVwNF9sYmw6J1ZhbGkga3V1cMOkZXYnLAogICAgY2FsX2F2YWlsOidWYWJ1IGFlZ3Ugb24nLGNhbF9ub25lOidWYWJ1IGFlZ3UgcG9sZScsCiAgICBzdGVwNF90aW1lOidWYWxpIGtlbGxhYWVnJywKICAgIHN0ZXA1X2xibDonVGVpZSBhbmRtZWQnLAogICAgbGJsX25hbWU6J05pbWknLHBoX25hbWU6J1RlaWUgbmltaScsCiAgICBsYmxfcGhvbmU6J1RlbGVmb24nLGxibF9lbWFpbDonRW1haWwnLAogICAgbGJsX3BldDonTGVtbWlrbG9vbWEgbmltaScscGhfb3B0aW9uYWw6J1ZhbGlrdWxpbmUnLAogICAgY29uZmlybV9idG46J0tpbm5pdGEgYnJvbmVlcmluZycsCiAgICBzdWNjZXNzX3RpdGxlOidCcm9uZWVyaW5nIGtpbm5pdGF0dWQhJywKICAgIHN1Y2Nlc3Nfc3ViOidWw7V0YW1lIHRlaWVnYSDDvGhlbmR1c3Qga2lubml0YW1pc2Vrcy48YnI+VMOkbmFtZSwgZXQgdmFsaXNpdGUgUiZhbXA7SiBHcm9vbWluZyEnLAogICAgdG9faG9tZTon4oaQIEF2YWxlaGVsZScsCiAgICBhbGVydF9maWxsOidQYWx1biBzaXNlc3RhZ2UgbmltaSBqYSB0ZWxlZm9uJyxhbGVydF9waG9uZTonU2lzZXN0YWdlIHRlbGVmb25pbnVtYmVyIHZvcm1pbmd1cyArMzcyMTIzNDU2NzgnLAogICAgc2VuZGluZzonU2FhZGFuLi4uJywKICAgIHN1bV9icmVlZDonVMO1dWcnLHN1bV9zZXJ2aWNlOidUZWVudXMnLHN1bV9tYXN0ZXI6J01laXN0ZXInLHN1bV9ncm9vbTonVmlpbWFuZSBncm9vbWluZycsc3VtX2RhdGU6J0t1dXDDpGV2JyxzdW1fdGltZTonS2VsbGFhZWcnLHN1bV9wcmljZTonSGluZCcsCiAgICBtb250aHM6WydKYWFudWFyJywnVmVlYnJ1YXInLCdNw6RydHMnLCdBcHJpbGwnLCdNYWknLCdKdXVuaScsJ0p1dWxpJywnQXVndXN0JywnU2VwdGVtYmVyJywnT2t0b29iZXInLCdOb3ZlbWJlcicsJ0RldHNlbWJlciddCiAgfQp9OwoKZnVuY3Rpb24gc2V0TGFuZyhsKXsKICBMQU5HPWw7CiAgbG9jYWxTdG9yYWdlLnNldEl0ZW0oJ3JqbGFuZycsbCk7CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmxhbmctYnRuJykuZm9yRWFjaChmdW5jdGlvbihiKXsKICAgIGIuY2xhc3NMaXN0LnRvZ2dsZSgnYWN0aXZlJywgYi50ZXh0Q29udGVudC50b0xvd2VyQ2FzZSgpPT09bCk7CiAgfSk7CiAgdmFyIHRyPVRbbF07CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnW2RhdGEtaTE4bl0nKS5mb3JFYWNoKGZ1bmN0aW9uKGVsKXsKICAgIHZhciBrPWVsLmdldEF0dHJpYnV0ZSgnZGF0YS1pMThuJyk7CiAgICBpZih0cltrXSE9PXVuZGVmaW5lZCkgZWwuaW5uZXJIVE1MPXRyW2tdOwogIH0pOwogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tkYXRhLWkxOG4tcGhdJykuZm9yRWFjaChmdW5jdGlvbihlbCl7CiAgICB2YXIgaz1lbC5nZXRBdHRyaWJ1dGUoJ2RhdGEtaTE4bi1waCcpOwogICAgaWYodHJba10hPT11bmRlZmluZWQpIGVsLnBsYWNlaG9sZGVyPXRyW2tdOwogIH0pOwogIE1PTlRIUz10ci5tb250aHM7CiAgYnVpbGRDYWwoKTsKICAvLyBSZS1yZW5kZXIgYmFkZ2UgYW5kIHNlcnZpY2VzIGlmIGJyZWVkIGFscmVhZHkgc2VsZWN0ZWQKICBpZihzZWxCcmVlZCl7CiAgICB2YXIgYmY9bD09PSdlbic/J2JyZWVkX2VuJzpsPT09J2V0Jz8nYnJlZWRfZXQnOidicmVlZCc7CiAgICB2YXIgZGI9c2VsQnJlZWRbYmZdfHxzZWxCcmVlZC5icmVlZDsKICAgIGJvb2tpbmcuYnJlZWREaXNwbGF5PWRiOwogICAgdmFyIGJuRWw9ZG9jdW1lbnQucXVlcnlTZWxlY3RvcignI3NCYWRnZSAuYm5hbWUnKTsKICAgIGlmKGJuRWwpIGJuRWwudGV4dENvbnRlbnQ9ZGI7CiAgICB2YXIgYmNFbD1kb2N1bWVudC5xdWVyeVNlbGVjdG9yKCcjc0JhZGdlIC5iY2hnJyk7CiAgICBpZihiY0VsKSBiY0VsLnRleHRDb250ZW50PWw9PT0nZW4nPydDaGFuZ2UnOmw9PT0nZXQnPydNdXVkYSc6J9CY0LfQvNC10L3QuNGC0YwnOwogICAgaWYoZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y1NlYycpLnN0eWxlLmRpc3BsYXkhPT0nbm9uZScpIHJlbmRlclN2Y3Moc2VsQnJlZWQpOwogICAgdmFyIHNuPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdmNOb3RlJyk7CiAgICBpZihzbil7CiAgICAgIHZhciBudD1sPT09J2VuJz8nUGxlYXNlIG5vdGUnOmw9PT0nZXQnPydQYW5nZSB0w6RoZWxlJzon0JLQsNC20L3QviDQt9C90LDRgtGMJzsKICAgICAgdmFyIG5iPWw9PT0nZW4nPydGaW5hbCBwcmljZSBkZXBlbmRzIG9uIGNvYXQgY29uZGl0aW9uIGFuZCBwZXQgYmVoYXZpb3VyLjxicj5EZW1hdHRpbmcgZnJvbSA1IOKCrC48YnI+QWdncmVzc2l2ZSBiZWhhdmlvdXIgc3VyY2hhcmdlIG1heSBhcHBseTogKzUwJS4nOmw9PT0nZXQnPydMw7VwbGlrIGhpbmQgc8O1bHR1YiBrYXJ2YXN0aWt1IHNlaXN1bmRpc3QgamEgbGVtbWlrbG9vbWEga8OkaXR1bWlzZXN0Ljxicj5Lb2x0c3VuaXRlIGxhaHRpaGFydXRhbWluZSBhbGF0ZXMgNSDigqwuPGJyPkFncmVzc2lpdnNlIGvDpGl0dW1pc2Uga29ycmFsIHbDtWliIGxpc2FuZHVkYSA1MCUganV1cmRlaGluZGx1cy4nOifQntC60L7QvdGH0LDRgtC10LvRjNC90LDRjyDRgdGC0L7QuNC80L7RgdGC0Ywg0LfQsNCy0LjRgdC40YIg0L7RgiDRgdC+0YHRgtC+0Y/QvdC40Y8g0YjQtdGA0YHRgtC4INC4INC/0L7QstC10LTQtdC90LjRjyDQv9C40YLQvtC80YbQsC48YnI+0KDQsNC30LHQvtGAINC60L7Qu9GC0YPQvdC+0LIg4oCUINC+0YIgNSDigqwuPGJyPtCf0YDQuCDQsNCz0YDQtdGB0YHQuNCy0L3QvtC8INC/0L7QstC10LTQtdC90LjQuCDQvNC+0LbQtdGCINC/0YDQuNC80LXQvdGP0YLRjNGB0Y8g0LTQvtC/0LvQsNGC0LAgNTAlLic7CiAgICAgIHNuLmlubmVySFRNTD0nPGRpdiBzdHlsZT0iZm9udC1zaXplOjAuODM4cmVtO2xldHRlci1zcGFjaW5nOi4xNWVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO21hcmdpbi1ib3R0b206OHB4O2ZvbnQtd2VpZ2h0OjYwMDtmb250LWZhbWlseTpcJ01vbnRzZXJyYXRcJyxzYW5zLXNlcmlmIj4nK250Kyc8L2Rpdj48ZGl2IHN0eWxlPSJmb250LXNpemU6MS4wMjVyZW07Y29sb3I6I2ZmZmZmZjtsaW5lLWhlaWdodDoxLjg7Zm9udC1mYW1pbHk6XCdNb250c2VycmF0XCcsc2Fucy1zZXJpZiI+JytuYisnPC9kaXY+JzsKICAgIH0KICB9Cn0KCi8vIEFwcGx5IHNhdmVkIGxhbmd1YWdlIG9uIGxvYWQKKGZ1bmN0aW9uKCl7IHNldExhbmcoTEFORyk7IH0pKCk7CgovLyBDYWxsYmFjayBmb3JtCmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYWxsYmFja0J0bicpLm9uY2xpY2sgPSBmdW5jdGlvbigpewogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtNb2RhbCcpLnN0eWxlLmRpc3BsYXkgPSAnZmxleCc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia05hbWUnKS52YWx1ZSA9ICcnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtQaG9uZScpLnZhbHVlID0gJyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1N1Y2Nlc3MnKS5zdHlsZS5kaXNwbGF5ID0gJ25vbmUnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtTdWJtaXQnKS5zdHlsZS5kaXNwbGF5ID0gJ2Jsb2NrJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrQ2xvc2UnKS50ZXh0Q29udGVudCA9ICfQntGC0LzQtdC90LAnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtTdWJtaXQnKS50ZXh0Q29udGVudCA9ICfQntGC0L/RgNCw0LLQuNGC0YwnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtTdWJtaXQnKS5kaXNhYmxlZCA9IGZhbHNlOwp9Owpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrQ2xvc2UnKS5vbmNsaWNrID0gZnVuY3Rpb24oKXsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrTW9kYWwnKS5zdHlsZS5kaXNwbGF5ID0gJ25vbmUnOwp9Owpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrU3VibWl0Jykub25jbGljayA9IGZ1bmN0aW9uKCl7CiAgdmFyIG5hbWUgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrTmFtZScpLnZhbHVlLnRyaW0oKTsKICB2YXIgcGhvbmUgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrUGhvbmUnKS52YWx1ZS50cmltKCkucmVwbGFjZSgvXEQvZywnJyk7CiAgaWYoIW5hbWUgfHwgIXBob25lKXthbGVydCgn0JLQstC10LTQuNGC0LUg0LjQvNGPINC4INGC0LXQu9C10YTQvtC9Jyk7cmV0dXJuO30KICB2YXIgYnRuID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1N1Ym1pdCcpOwogIGJ0bi50ZXh0Q29udGVudCA9ICfQntGC0L/RgNCw0LLQu9GP0LXQvC4uLic7IGJ0bi5kaXNhYmxlZCA9IHRydWU7CiAgZmV0Y2goJy9hcGkvY2FsbGJhY2snLHsKICAgIG1ldGhvZDonUE9TVCcsCiAgICBoZWFkZXJzOnsnQ29udGVudC1UeXBlJzonYXBwbGljYXRpb24vanNvbid9LAogICAgYm9keTpKU09OLnN0cmluZ2lmeSh7bmFtZTpuYW1lLCBwaG9uZTonKzM3MicrcGhvbmV9KQogIH0pLnRoZW4oZnVuY3Rpb24oKXsKICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtTdWNjZXNzJykuc3R5bGUuZGlzcGxheSA9ICdibG9jayc7CiAgICBidG4uc3R5bGUuZGlzcGxheSA9ICdub25lJzsKICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtDbG9zZScpLnRleHRDb250ZW50ID0gJ+KGkCDQl9Cw0LrRgNGL0YLRjCc7CiAgICBzZXRUaW1lb3V0KGZ1bmN0aW9uKCl7ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia01vZGFsJykuc3R5bGUuZGlzcGxheT0nbm9uZSc7fSwzMDAwKTsKICB9KS5jYXRjaChmdW5jdGlvbigpewogICAgYnRuLnRleHRDb250ZW50ID0gJ9Ce0YLQv9GA0LDQstC40YLRjCc7IGJ0bi5kaXNhYmxlZCA9IGZhbHNlOwogICAgYWxlcnQoJ9Ce0YjQuNCx0LrQsC4g0J/QvtC/0YDQvtCx0YPQudGC0LUg0LXRidGRINGA0LDQty4nKTsKICB9KTsKfTsKCjwvc2NyaXB0Pgo8L2JvZHk+CjwvaHRtbD4K"



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
