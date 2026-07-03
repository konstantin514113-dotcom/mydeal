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

_MASTERS = ["татьяна", "алиса", "кристина", "анна"]

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
    except Exception as e:
        print(f"BOOK ERROR: {e}", flush=True)
        resp = jsonify({"success": False, "error": str(e)})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp

# ── BOOKING APP PAGE ───────────────────────────────────────────────────────
BOOKING_HTML_B64 = "77u/PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9InJ1Ij4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idGhlbWUtY29sb3IiIGNvbnRlbnQ9IiMwYTBhMGEiPgo8bWV0YSBuYW1lPSJ2aWV3cG9ydCIgY29udGVudD0id2lkdGg9ZGV2aWNlLXdpZHRoLGluaXRpYWwtc2NhbGU9MSI+Cjx0aXRsZT5SJkogR3Jvb21pbmc8L3RpdGxlPgo8bGluayBocmVmPSJodHRwczovL2ZvbnRzLmdvb2dsZWFwaXMuY29tL2NzczI/ZmFtaWx5PUNvcm1vcmFudCtHYXJhbW9uZDppdGFsLHdnaHRAMCw2MDA7MSw0MDAmZmFtaWx5PU1vbnRzZXJyYXQ6d2dodEAzMDA7NDAwOzUwMDs2MDAmZGlzcGxheT1zd2FwIiByZWw9InN0eWxlc2hlZXQiPgo8c3R5bGU+Cip7Ym94LXNpemluZzpib3JkZXItYm94O21hcmdpbjowO3BhZGRpbmc6MH0KaHRtbCxib2R5e21pbi1oZWlnaHQ6MTAwdmg7YmFja2dyb3VuZDojMGEwYTBhO2NvbG9yOiNmMGVjZTQ7Zm9udC1mYW1pbHk6J0Nvcm1vcmFudCBHYXJhbW9uZCcsc2VyaWY7Zm9udC13ZWlnaHQ6NDAwfQouc2NyZWVue2Rpc3BsYXk6bm9uZTttaW4taGVpZ2h0OjEwMHZoO2ZsZXgtZGlyZWN0aW9uOmNvbHVtbjthbGlnbi1pdGVtczpjZW50ZXI7cGFkZGluZzo0OHB4IDAgNjRweH0KLnNjcmVlbi5hY3RpdmV7ZGlzcGxheTpmbGV4fQouY29ue3dpZHRoOjEwMCU7bWF4LXdpZHRoOjQwMHB4O3BhZGRpbmc6MCAyOHB4fQouYmFjay1idG57ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OHB4O2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjI4KTtmb250LXNpemU6LjU2cmVtO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyOm5vbmU7Y3Vyc29yOnBvaW50ZXI7cGFkZGluZzowO21hcmdpbi1ib3R0b206MzZweDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXdlaWdodDozMDA7dHJhbnNpdGlvbjpjb2xvciAuMnN9Ci5iYWNrLWJ0bjpob3Zlcntjb2xvcjojZjBlY2U0fQoubG9nby1yantmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZjtmb250LXNpemU6Mi4ycmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojYzlhODRjO2xldHRlci1zcGFjaW5nOi4wMmVtfQoubG9nby1zdWJ7Zm9udC1zaXplOi40NHJlbTtmb250LXdlaWdodDo0MDA7bGV0dGVyLXNwYWNpbmc6LjQ1ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjIpO21hcmdpbi10b3A6NHB4O3BhZGRpbmctYm90dG9tOjIwcHg7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDgpO21hcmdpbi1ib3R0b206MjhweDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZn0KLmhvbWUtcmp7Zm9udC1mYW1pbHk6J0Nvcm1vcmFudCBHYXJhbW9uZCcsc2VyaWY7Zm9udC1zaXplOjNyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNjOWE4NGM7bGluZS1oZWlnaHQ6MX0KLmxvZ28tdGFne2ZvbnQtc2l6ZTouNXJlbTtsZXR0ZXItc3BhY2luZzouMTRlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuMjUpO2xpbmUtaGVpZ2h0OjEuNjtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZn0KLmxvZ28tcm93e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpmbGV4LWVuZDtnYXA6MTRweDttYXJnaW4tYm90dG9tOjZweDtwYWRkaW5nLWJvdHRvbToyMHB4O2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA4KX0KLmhvbWUtZ3N1Yntmb250LXNpemU6LjQ0cmVtO2ZvbnQtd2VpZ2h0OjQwMDtsZXR0ZXItc3BhY2luZzouNDVlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuMik7bWFyZ2luLXRvcDo4cHg7bWFyZ2luLWJvdHRvbToyOHB4O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQouaG9tZS1oMXtmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZjtmb250LXNpemU6Mi41cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZjBlY2U0O2xpbmUtaGVpZ2h0OjEuMTttYXJnaW4tYm90dG9tOjZweH0KLmhvbWUtaDEgZW17Zm9udC1zdHlsZTppdGFsaWM7Y29sb3I6I2YwZWNlNH0KLmhvbWUtc3Vie2ZvbnQtc2l6ZTouNTZyZW07bGV0dGVyLXNwYWNpbmc6LjE4ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjIpO21hcmdpbi1ib3R0b206MjhweDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZn0KLm9wdHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxNnB4O3BhZGRpbmc6MTZweCAwO2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA3KTt0ZXh0LWRlY29yYXRpb246bm9uZTtjb2xvcjojZjBlY2U0O3RyYW5zaXRpb246Y29sb3IgLjJzO2N1cnNvcjpwb2ludGVyO2JhY2tncm91bmQ6bm9uZTtib3JkZXItdG9wOm5vbmU7Ym9yZGVyLWxlZnQ6bm9uZTtib3JkZXItcmlnaHQ6bm9uZTt3aWR0aDoxMDAlO2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmfQoub3B0OmhvdmVye2NvbG9yOiNmZmZ9Ci5vcHQtaWNvbnt3aWR0aDozOHB4O2hlaWdodDozOHB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OmNlbnRlcjtmbGV4LXNocmluazowfQoub3B0LXRleHR7ZmxleDoxO3RleHQtYWxpZ246bGVmdH0KLm9wdC10aXRsZXtmb250LXNpemU6MS4wNXJlbTtmb250LXdlaWdodDo2MDA7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuNTUpO21hcmdpbi1ib3R0b206MnB4O3RyYW5zaXRpb246Y29sb3IgLjJzO2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmfQoub3B0OmhvdmVyIC5vcHQtdGl0bGV7Y29sb3I6I2ZmZn0KLm9wdC1oYW5kbGV7Zm9udC1zaXplOi42MnJlbTtjb2xvcjpyZ2JhKDI1NSwyNTUsMjU1LC4yKTtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXdlaWdodDozMDB9Ci5vcHQtYXJyb3d7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuMTYpO2ZvbnQtc2l6ZTouODVyZW07ZmxleC1zaHJpbms6MDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLm9wdDpob3ZlciAub3B0LWFycm93e2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjU1KX0KLmRpdmlkZXJ7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTJweDtwYWRkaW5nOjEycHggMH0KLmRpdmlkZXI6OmJlZm9yZSwuZGl2aWRlcjo6YWZ0ZXJ7Y29udGVudDonJztmbGV4OjE7aGVpZ2h0OjFweDtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA2KX0KLmRpdmlkZXIgc3Bhbntmb250LXNpemU6LjQ4cmVtO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjpyZ2JhKDI1NSwyNTUsMjU1LC4xNik7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5ob21lLWZvb3R7bWFyZ2luLXRvcDozNnB4O3BhZGRpbmctdG9wOjIwcHg7Ym9yZGVyLXRvcDoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDYpO2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpjZW50ZXJ9Ci5ob21lLWZvb3Qgc3Bhbntmb250LXNpemU6LjU0cmVtO2xldHRlci1zcGFjaW5nOi4yZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjE2KTtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZn0KLmZkb3R7d2lkdGg6MnB4O2hlaWdodDoycHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4xNil9Ci5wcm9ncmVzc3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO21hcmdpbi1ib3R0b206NDBweDtvdmVyZmxvdzpoaWRkZW47Y291bnRlci1yZXNldDpzdGVwfQoucHN7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6NXB4O2ZvbnQtc2l6ZTouNDZyZW07bGV0dGVyLXNwYWNpbmc6LjEyZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjE4KTt3aGl0ZS1zcGFjZTpub3dyYXA7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Y291bnRlci1pbmNyZW1lbnQ6c3RlcH0KLnBzLmRvbmV7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuMjgpfQoucHMuYWN0aXZle2NvbG9yOiNjOWE4NGN9Ci5wZG90e3dpZHRoOjE4cHg7aGVpZ2h0OjE4cHg7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2ZsZXgtc2hyaW5rOjA7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xMik7Zm9udC1zaXplOi40NnJlbTtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXdlaWdodDo2MDB9Ci5wZG90OjpiZWZvcmV7Y29udGVudDpjb3VudGVyKHN0ZXAsZGVjaW1hbC1sZWFkaW5nLXplcm8pfQoucHMuZG9uZSAucGRvdHtib3JkZXItY29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuMjIpO2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjI4KX0KLnBzLmFjdGl2ZSAucGRvdHtib3JkZXItY29sb3I6I2M5YTg0Yztjb2xvcjojYzlhODRjfQoucGx7ZmxleDoxO2hlaWdodDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNyk7bWFyZ2luOjAgNXB4O21pbi13aWR0aDo2cHh9Ci5wbC5kb25le2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMTgpfQouc3RlcHtkaXNwbGF5Om5vbmV9LnN0ZXAuc2hvd3tkaXNwbGF5OmJsb2NrO2FuaW1hdGlvbjpmdSAuMzVzIGVhc2UgYm90aH0KLnNsYmx7Zm9udC1mYW1pbHk6J0Nvcm1vcmFudCBHYXJhbW9uZCcsc2VyaWY7Zm9udC1zaXplOjEuMzVyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNmMGVjZTQ7bWFyZ2luLWJvdHRvbToyMHB4O2xldHRlci1zcGFjaW5nOi4wMWVtfQouc2JveHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxMHB4O2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjE2KTtwYWRkaW5nOjAgMnB4O3RyYW5zaXRpb246Ym9yZGVyLWNvbG9yIC4yc30KLnNib3g6Zm9jdXMtd2l0aGlue2JvcmRlci1ib3R0b20tY29sb3I6I2M5YTg0Y30KLnNpe29wYWNpdHk6LjI7Zm9udC1zaXplOi44NXJlbTtmbGV4LXNocmluazowfQojYklucHV0e2ZsZXg6MTtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjpub25lO291dGxpbmU6bm9uZTtmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZjtmb250LXNpemU6MS4wNXJlbTtjb2xvcjojZjBlY2U0O3BhZGRpbmc6MTJweCAwfQojYklucHV0OjpwbGFjZWhvbGRlcntjb2xvcjpyZ2JhKDI1NSwyNTUsMjU1LC4xOCl9Ci5jbHJ7YmFja2dyb3VuZDpub25lO2JvcmRlcjpub25lO2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjIpO2N1cnNvcjpwb2ludGVyO2ZvbnQtc2l6ZTouOHJlbTtkaXNwbGF5Om5vbmU7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5jbHIuc2hvd3tkaXNwbGF5OmJsb2NrfQouYndyYXB7cG9zaXRpb246cmVsYXRpdmU7bWFyZ2luLWJvdHRvbToyMHB4fQouZHJvcHtwb3NpdGlvbjphYnNvbHV0ZTtsZWZ0OjA7cmlnaHQ6MDtiYWNrZ3JvdW5kOiMwZjBmMGY7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xKTtib3JkZXItdG9wOm5vbmU7bWF4LWhlaWdodDoyMDBweDtvdmVyZmxvdy15OmF1dG87ei1pbmRleDo1MDtkaXNwbGF5Om5vbmV9Ci5kcm9wLm9wZW57ZGlzcGxheTpibG9ja30KLmRpdGVte3BhZGRpbmc6MTFweCAxNHB4O2ZvbnQtc2l6ZTouOTVyZW07Y29sb3I6I2YwZWNlNDtjdXJzb3I6cG9pbnRlcjtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNSk7Zm9udC1mYW1pbHk6J0Nvcm1vcmFudCBHYXJhbW9uZCcsc2VyaWY7dHJhbnNpdGlvbjpjb2xvciAuMnN9Ci5kaXRlbTpob3Zlcntjb2xvcjojZmZmfQouZGl0ZW0gbWFya3tiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2NvbG9yOiNjOWE4NGM7Zm9udC13ZWlnaHQ6NjAwfQoubm9yZXN7cGFkZGluZzoxNHB4O2ZvbnQtc2l6ZTouOXJlbTtjb2xvcjpyZ2JhKDI1NSwyNTUsMjU1LC4yMik7Zm9udC1zdHlsZTppdGFsaWM7Zm9udC1mYW1pbHk6J0Nvcm1vcmFudCBHYXJhbW9uZCcsc2VyaWZ9Ci5uby1icmVlZC1iYW5uZXJ7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTJweDtwYWRkaW5nOjE0cHggMDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNik7Y3Vyc29yOnBvaW50ZXI7dHJhbnNpdGlvbjpjb2xvciAuMnM7bWFyZ2luLXRvcDo0cHh9Ci5uby1icmVlZC1iYW5uZXI6aG92ZXIgLm5vLWJyZWVkLWJhbm5lci10aXRsZXtjb2xvcjojZjBlY2U0fQoubm8tYnJlZWQtYmFubmVyLWljb257Zm9udC1zaXplOjEuMXJlbTtmbGV4LXNocmluazowO29wYWNpdHk6LjN9Ci5uby1icmVlZC1iYW5uZXItdGV4dHtmbGV4OjF9Ci5uby1icmVlZC1iYW5uZXItdGl0bGV7Zm9udC1zaXplOjFyZW07Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuMzgpO2ZvbnQtd2VpZ2h0OjYwMDttYXJnaW4tYm90dG9tOjJweDtmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZjt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLm5vLWJyZWVkLWJhbm5lci1zdWJ7Zm9udC1zaXplOi42MnJlbTtjb2xvcjpyZ2JhKDI1NSwyNTUsMjU1LC4xNik7bGluZS1oZWlnaHQ6MS41O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQoubm8tYnJlZWQtYmFubmVyLWFycm93e2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjE2KTtmb250LXNpemU6Ljg1cmVtO2ZsZXgtc2hyaW5rOjA7dHJhbnNpdGlvbjpjb2xvciAuMnN9Ci5zYmFkZ2V7ZGlzcGxheTpub25lO2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTJweDttYXJnaW4tYm90dG9tOjIwcHh9Ci5zYmFkZ2Uuc2hvd3tkaXNwbGF5OmZsZXh9Ci5ibmFtZXtib3JkZXItYm90dG9tOjFweCBzb2xpZCAjYzlhODRjO2NvbG9yOiNjOWE4NGM7cGFkZGluZzoycHggMDtmb250LXNpemU6MXJlbTtmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZn0KLmJjaGd7Zm9udC1zaXplOi41NnJlbTtjb2xvcjpyZ2JhKDI1NSwyNTUsMjU1LC4yKTtjdXJzb3I6cG9pbnRlcjtsZXR0ZXItc3BhY2luZzouMTJlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7dHJhbnNpdGlvbjpjb2xvciAuMnN9Ci5iY2hnOmhvdmVye2NvbG9yOiNmMGVjZTR9Ci5zdmJ0bntkaXNwbGF5OmJsb2NrO3BhZGRpbmc6MDtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjpub25lO2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA4KTtjb2xvcjojZjBlY2U0O2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmO2N1cnNvcjpwb2ludGVyO3RleHQtYWxpZ246bGVmdDt0cmFuc2l0aW9uOmJvcmRlci1jb2xvciAuMnM7d2lkdGg6MTAwJTtvdmVyZmxvdzpoaWRkZW47cG9zaXRpb246cmVsYXRpdmV9Ci5zdmJ0bjpob3Zlcntib3JkZXItYm90dG9tLWNvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjI4KX0KLnN2YnRuLmFjdGl2ZXtib3JkZXItYm90dG9tLWNvbG9yOiNjOWE4NGN9Ci5zdnB7Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNjOWE4NGM7ZmxleC1zaHJpbms6MH0KLm1hc3RlcnN7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyO2dhcDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNyl9Ci5tYnRue2JhY2tncm91bmQ6IzBhMGEwYTtwYWRkaW5nOjIycHggMTJweDt0ZXh0LWFsaWduOmNlbnRlcjtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmJhY2tncm91bmQgLjJzO2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmO2JvcmRlcjpub25lfQoubWJ0bjpob3ZlcntiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjAzKX0KLm1idG4uYWN0aXZle2JhY2tncm91bmQ6cmdiYSgyMDEsMTY4LDc2LC4wNSl9Ci5tYXZ7d2lkdGg6NDBweDtoZWlnaHQ6NDBweDtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTQpO2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OmNlbnRlcjttYXJnaW46MCBhdXRvIDEwcHg7Zm9udC1mYW1pbHk6J0Nvcm1vcmFudCBHYXJhbW9uZCcsc2VyaWY7Zm9udC1zaXplOjFyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjMpfQoubWJ0bi5hY3RpdmUgLm1hdntib3JkZXItY29sb3I6I2M5YTg0Yztjb2xvcjojYzlhODRjfQoubW5hbWV7Zm9udC1zaXplOjFyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjQpO2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmfQoubWJ0bjpob3ZlciAubW5hbWV7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuNyl9Ci5tYnRuLmFjdGl2ZSAubW5hbWV7Y29sb3I6I2M5YTg0Y30KLm10aXRsZXtmb250LXNpemU6LjU2cmVtO2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjE2KTttYXJnaW4tdG9wOjNweDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZn0KLmdidG57ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtwYWRkaW5nOjE0cHggMDtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjpub25lO2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA4KTtjb2xvcjpyZ2JhKDI1NSwyNTUsMjU1LC4zOCk7Zm9udC1mYW1pbHk6J0Nvcm1vcmFudCBHYXJhbW9uZCcsc2VyaWY7Zm9udC1zaXplOjFyZW07Y3Vyc29yOnBvaW50ZXI7d2lkdGg6MTAwJTt0cmFuc2l0aW9uOmFsbCAuMnN9Ci5nYnRuOmhvdmVye2NvbG9yOiNmMGVjZTR9Ci5nYnRuLmFjdGl2ZXtjb2xvcjojYzlhODRjO2JvcmRlci1ib3R0b20tY29sb3I6I2M5YTg0Y30KLmNhbC1oe2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpjZW50ZXI7bWFyZ2luLWJvdHRvbToxNnB4fQouY2FsLW17Zm9udC1mYW1pbHk6J0Nvcm1vcmFudCBHYXJhbW9uZCcsc2VyaWY7Zm9udC1zaXplOjEuMzVyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNmMGVjZTR9Ci5jYWwtbntiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyOm5vbmU7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuMjUpO2N1cnNvcjpwb2ludGVyO2ZvbnQtc2l6ZToxLjFyZW07cGFkZGluZzo0cHggOHB4O3RyYW5zaXRpb246Y29sb3IgLjJzfQouY2FsLW46aG92ZXJ7Y29sb3I6I2YwZWNlNH0KLmNne2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDcsMWZyKTtnYXA6MnB4O21hcmdpbi1ib3R0b206MTJweH0KLmNkbnt0ZXh0LWFsaWduOmNlbnRlcjtmb250LXNpemU6LjQ2cmVtO2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjE4KTtwYWRkaW5nOjRweCAwO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtsZXR0ZXItc3BhY2luZzouMWVtfQouY2R7dGV4dC1hbGlnbjpjZW50ZXI7Y3Vyc29yOnBvaW50ZXI7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuMyk7Ym9yZGVyOjFweCBzb2xpZCB0cmFuc3BhcmVudDt0cmFuc2l0aW9uOmFsbCAuMnN9Ci5jZDpob3Zlcjpub3QoLmRpcyk6bm90KC5wYWQpIC5jZC1pbm5lcntiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA3KSFpbXBvcnRhbnQ7Y29sb3I6I2YwZWNlNCFpbXBvcnRhbnR9Ci5jZC5zZWwgLmNkLWlubmVye2JhY2tncm91bmQ6I2M5YTg0YyFpbXBvcnRhbnQ7Y29sb3I6IzBhMGEwYSFpbXBvcnRhbnQ7Zm9udC13ZWlnaHQ6NzAwIWltcG9ydGFudDtib3JkZXI6bm9uZSFpbXBvcnRhbnR9Ci5jZC50b2QgLmNkLWlubmVye2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMjgpO2NvbG9yOiNmZmZ9Ci5jZC5kaXN7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuMDgpO2N1cnNvcjpkZWZhdWx0fQoudGd7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoNCwxZnIpO2dhcDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNyl9Ci50YnRue2JhY2tncm91bmQ6IzBhMGEwYTtib3JkZXI6bm9uZTtwYWRkaW5nOjEzcHggNHB4O3RleHQtYWxpZ246Y2VudGVyO2ZvbnQtc2l6ZTouOTJyZW07Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuMzUpO2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmO3RyYW5zaXRpb246YWxsIC4yc30KLnRidG46aG92ZXJ7Y29sb3I6I2YwZWNlNDtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA0KX0KLnRidG4uYWN0aXZle2NvbG9yOiNjOWE4NGN9Ci5zdW17YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtib3JkZXItdG9wOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wOCk7cGFkZGluZzoyMHB4IDA7bWFyZ2luLWJvdHRvbToyMHB4fQouc3J7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO3BhZGRpbmc6OHB4IDA7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDUpO2ZvbnQtc2l6ZTouOTVyZW07Zm9udC1mYW1pbHk6J0Nvcm1vcmFudCBHYXJhbW9uZCcsc2VyaWZ9Ci5zcjpsYXN0LWNoaWxke2JvcmRlci1ib3R0b206bm9uZTtwYWRkaW5nLXRvcDoxNHB4fQouc2x7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuMjgpfS5zdntjb2xvcjojZjBlY2U0O3RleHQtYWxpZ246cmlnaHR9Ci5zcHtmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZjtmb250LXNpemU6MS43cmVtO2NvbG9yOiNmMGVjZTQ7Zm9udC13ZWlnaHQ6NjAwfQouZmd7bWFyZ2luLWJvdHRvbToyMHB4fQouZmx7Zm9udC1zaXplOi41cmVtO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjpyZ2JhKDI1NSwyNTUsMjU1LC4yNSk7bWFyZ2luLWJvdHRvbTo4cHg7ZGlzcGxheTpibG9jaztmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZn0KLmZpe3dpZHRoOjEwMCU7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xNCk7Y29sb3I6I2YwZWNlNDtmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZjtmb250LXNpemU6MS4wNXJlbTtwYWRkaW5nOjEwcHggMDtvdXRsaW5lOm5vbmU7dHJhbnNpdGlvbjpib3JkZXItY29sb3IgLjJzfQouZmk6Zm9jdXN7Ym9yZGVyLWJvdHRvbS1jb2xvcjojYzlhODRjfQouY2J0bntkaXNwbGF5OmJsb2NrO3dpZHRoOjEwMCU7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtjb2xvcjojZjBlY2U0O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZTouNnJlbTtmb250LXdlaWdodDo2MDA7bGV0dGVyLXNwYWNpbmc6LjI4ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO3RleHQtYWxpZ246Y2VudGVyO3BhZGRpbmc6MTZweDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjI1KTtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmFsbCAuMnN9Ci5jYnRuOmhvdmVye2JvcmRlci1jb2xvcjojZjBlY2U0O2NvbG9yOiNmMGVjZTR9Ci5zYmxvY2t7dGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzo1MnB4IDIwcHg7ZGlzcGxheTpub25lfQouc2Jsb2NrLnNob3d7ZGlzcGxheTpibG9jazthbmltYXRpb246ZnUgLjVzIGVhc2UgYm90aH0KLnNpMntmb250LXNpemU6Mi41cmVtO21hcmdpbi1ib3R0b206MjBweDtvcGFjaXR5Oi40fQouc3R7Zm9udC1mYW1pbHk6J0Nvcm1vcmFudCBHYXJhbW9uZCcsc2VyaWY7Zm9udC1zaXplOjEuOXJlbTtjb2xvcjojZjBlY2U0O21hcmdpbi1ib3R0b206MTBweDtmb250LXdlaWdodDo2MDB9Ci5zc3tmb250LXNpemU6Ljc1cmVtO2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjMpO2xpbmUtaGVpZ2h0OjEuOTttYXJnaW4tYm90dG9tOjI4cHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5oYnRue2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xNik7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuNCk7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC1zaXplOi42cmVtO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtwYWRkaW5nOjEzcHggMjhweDtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmFsbCAuMnN9Ci5oYnRuOmhvdmVye2JvcmRlci1jb2xvcjojZjBlY2U0O2NvbG9yOiNmMGVjZTR9Ci5sb2FkaW5nLXNsb3Rze2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjIpO2ZvbnQtc2l6ZTouOXJlbTtwYWRkaW5nOjEycHggMDt0ZXh0LWFsaWduOmNlbnRlcjtmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZjtmb250LXN0eWxlOml0YWxpY30KLmNke2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2FsaWduLWl0ZW1zOmNlbnRlcjtoZWlnaHQ6MzZweCFpbXBvcnRhbnQ7cGFkZGluZzowIWltcG9ydGFudH0KLmNkLWlubmVye3dpZHRoOjMycHg7aGVpZ2h0OjMycHg7Ym9yZGVyLXJhZGl1czowO2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OmNlbnRlcjtmb250LXNpemU6LjhyZW07Y3Vyc29yOnBvaW50ZXI7Zm9udC1mYW1pbHk6J0Nvcm1vcmFudCBHYXJhbW9uZCcsc2VyaWZ9Ci5jZC5hdmFpbCAuY2QtaW5uZXJ7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDkwLDE4MCw5MCwuMzUpO2NvbG9yOnJnYmEoOTAsMTgwLDkwLC42NSl9Ci5jZC5idXN5IC5jZC1pbm5lcntib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA3KTtjb2xvcjpyZ2JhKDI1NSwyNTUsMjU1LC4xMil9Ci5jZC5zZWwgLmNkLWlubmVye2JhY2tncm91bmQ6I2M5YTg0YyFpbXBvcnRhbnQ7Y29sb3I6IzBhMGEwYSFpbXBvcnRhbnQ7Zm9udC13ZWlnaHQ6NzAwIWltcG9ydGFudDtib3JkZXI6bm9uZSFpbXBvcnRhbnR9Ci5jZC50b2QgLmNkLWlubmVye2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMjgpO2NvbG9yOiNmZmY7Zm9udC13ZWlnaHQ6NjAwfQouY2QuZGlzIC5jZC1pbm5lcntjb2xvcjpyZ2JhKDI1NSwyNTUsMjU1LC4wNyk7Y3Vyc29yOmRlZmF1bHQ7YmFja2dyb3VuZDpub25lO2JvcmRlcjpub25lfQouc3ZidG4tcm93e2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpiYXNlbGluZTttYXJnaW4tYm90dG9tOjZweDtwYWRkaW5nOjE2cHggMCAwfQouc3ZidG4tbmFtZXtmb250LXNpemU6MS4wNXJlbTtjb2xvcjojZjBlY2U0O2ZvbnQtd2VpZ2h0OjYwMDtmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZn0KLnN2YnRuLmFjdGl2ZSAuc3ZidG4tbmFtZXtjb2xvcjojYzlhODRjfQouc3ZidG4tcHJpY2V7Zm9udC1mYW1pbHk6J0Nvcm1vcmFudCBHYXJhbW9uZCcsc2VyaWY7Zm9udC1zaXplOjEuMnJlbTtjb2xvcjpyZ2JhKDI1NSwyNTUsMjU1LC4zOCk7Zm9udC13ZWlnaHQ6NjAwO2ZsZXgtc2hyaW5rOjB9Ci5zdmJ0bi5hY3RpdmUgLnN2YnRuLXByaWNle2NvbG9yOiNjOWE4NGN9Ci5zdmJ0bi1kZXNje2ZvbnQtc2l6ZTouN3JlbTtjb2xvcjpyZ2JhKDI1NSwyNTUsMjU1LC4yKTtsaW5lLWhlaWdodDoxLjc7ZGlzcGxheTpibG9jaztwYWRkaW5nOjAgMCAxNHB4O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQouc3ZidG4uYWN0aXZlIC5zdmJ0bi1kZXNje2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjMpfQouc3ZidG4tdGFne2ZvbnQtc2l6ZTouNjhyZW07Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuMTYpO2ZvbnQtc3R5bGU6aXRhbGljO2Rpc3BsYXk6YmxvY2s7bWFyZ2luLXRvcDoycHg7cGFkZGluZzowIDAgMTRweDtmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZn0KLnN2YnRuLmFjdGl2ZSAuc3ZidG4tdGFne2NvbG9yOnJnYmEoMjAxLDE2OCw3NiwuNSl9CkBtZWRpYShtYXgtd2lkdGg6NDAwcHgpey5zdmJ0bi1uYW1le2ZvbnQtc2l6ZTouOTVyZW19LnN2YnRuLXByaWNle2ZvbnQtc2l6ZToxLjA1cmVtfS5zdmJ0bi1kZXNje2ZvbnQtc2l6ZTouNjVyZW19LnN2YnRuLXRhZ3tmb250LXNpemU6LjYycmVtfX0KQGtleWZyYW1lcyBmdXtmcm9te29wYWNpdHk6MDt0cmFuc2Zvcm06dHJhbnNsYXRlWSgxMHB4KX10b3tvcGFjaXR5OjE7dHJhbnNmb3JtOnRyYW5zbGF0ZVkoMCl9fQoubGFuZy1iYXJ7cG9zaXRpb246Zml4ZWQ7dG9wOjEycHg7cmlnaHQ6MTRweDt6LWluZGV4Ojk5OTtkaXNwbGF5OmZsZXg7Z2FwOjZweH0KLmxhbmctYnRue2JhY2tncm91bmQ6cmdiYSgxMCwxMCwxMCwuOTIpO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMSk7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuMjUpO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZTouNTRyZW07bGV0dGVyLXNwYWNpbmc6LjE1ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO3BhZGRpbmc6NXB4IDEwcHg7Y3Vyc29yOnBvaW50ZXI7dHJhbnNpdGlvbjphbGwgLjJzfQoubGFuZy1idG46aG92ZXJ7Ym9yZGVyLWNvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjI1KTtjb2xvcjpyZ2JhKDI1NSwyNTUsMjU1LC42KX0KLmxhbmctYnRuLmFjdGl2ZXtib3JkZXItY29sb3I6I2M5YTg0Yztjb2xvcjojYzlhODRjfQouY2JrLWJ0bntiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTQpO2NvbG9yOnJnYmEoMjU1LDI1NSwyNTUsLjI4KTtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXNpemU6LjZyZW07bGV0dGVyLXNwYWNpbmc6LjE2ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO3BhZGRpbmc6MTJweCAyMHB4O2N1cnNvcjpwb2ludGVyO3RyYW5zaXRpb246YWxsIC4yczt3aWR0aDoxMDAlfQouY2JrLWJ0bjpob3Zlcntib3JkZXItY29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuNCk7Y29sb3I6cmdiYSgyNTUsMjU1LDI1NSwuNil9Cjwvc3R5bGU+CjwvaGVhZD4KPGJvZHk+CjxkaXYgY2xhc3M9ImxhbmctYmFyIj4KICA8YnV0dG9uIGNsYXNzPSJsYW5nLWJ0biBhY3RpdmUiIG9uY2xpY2s9InNldExhbmcoJ3J1JykiPlJVPC9idXR0b24+CiAgPGJ1dHRvbiBjbGFzcz0ibGFuZy1idG4iIG9uY2xpY2s9InNldExhbmcoJ2VuJykiPkVOPC9idXR0b24+CiAgPGJ1dHRvbiBjbGFzcz0ibGFuZy1idG4iIG9uY2xpY2s9InNldExhbmcoJ2V0JykiPkVUPC9idXR0b24+CjwvZGl2PgoKPCEtLSBIT01FIC0tPgo8ZGl2IGNsYXNzPSJzY3JlZW4gYWN0aXZlIiBpZD0iaG9tZVNjcmVlbiI+CjxkaXYgY2xhc3M9ImNvbiI+CiAgPGRpdiBjbGFzcz0ibG9nby1yb3ciPgogICAgPGRpdiBjbGFzcz0iaG9tZS1yaiI+UiZhbXA7SjwvZGl2PgogICAgPGRpdiBjbGFzcz0ibG9nby10YWciIGRhdGEtaTE4bj0ibG9nb190YWciPtCf0YDQtdC80LjQsNC70YzQvdGL0Lkg0LPRgNGD0LzQuNC90LMtPGJyPtGB0LDQu9C+0L0g0LIg0KLQsNC70LvQuNC90LU8L2Rpdj4KICA8L2Rpdj4KICA8ZGl2IGNsYXNzPSJob21lLWdzdWIiPkdyb29taW5nPC9kaXY+CiAgPGRpdiBjbGFzcz0iaG9tZS1oMSI+Qm9vayB0aGUgd2F5IDxlbT55b3UgbGlrZTwvZW0+PC9kaXY+CiAgPGRpdiBjbGFzcz0iaG9tZS1zdWIiIGRhdGEtaTE4bj0iY2hvb3NlX2hvdyI+Q2hvb3NlIGhvdyB0byBjb25uZWN0PC9kaXY+CgogIDxidXR0b24gY2xhc3M9Im9wdCIgaWQ9ImJvb2tCdG4iPgogICAgPGRpdiBjbGFzcz0ib3B0LWljb24iPjxzdmcgd2lkdGg9IjM2IiBoZWlnaHQ9IjM2IiB2aWV3Qm94PSIwIDAgMjQgMjQiIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyI+PHJlY3Qgd2lkdGg9IjI0IiBoZWlnaHQ9IjI0IiByeD0iNiIgZmlsbD0iI2M5YTg0YyIvPjxyZWN0IHg9IjUiIHk9IjciIHdpZHRoPSIxNCIgaGVpZ2h0PSIxMyIgcng9IjEuNSIgZmlsbD0ibm9uZSIgc3Ryb2tlPSJ3aGl0ZSIgc3Ryb2tlLXdpZHRoPSIxLjUiLz48cGF0aCBkPSJNOCA1djRNMTYgNXY0TTUgMTFoMTQiIHN0cm9rZT0id2hpdGUiIHN0cm9rZS13aWR0aD0iMS41IiBzdHJva2UtbGluZWNhcD0icm91bmQiLz48Y2lyY2xlIGN4PSI4LjUiIGN5PSIxNSIgcj0iMSIgZmlsbD0id2hpdGUiLz48Y2lyY2xlIGN4PSIxMiIgY3k9IjE1IiByPSIxIiBmaWxsPSJ3aGl0ZSIvPjxjaXJjbGUgY3g9IjE1LjUiIGN5PSIxNSIgcj0iMSIgZmlsbD0id2hpdGUiLz48L3N2Zz48L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im9wdC10ZXh0Ij48ZGl2IGNsYXNzPSJvcHQtdGl0bGUiIGRhdGEtaTE4bj0iYm9va19vbmxpbmUiPkJvb2sgT25saW5lPC9kaXY+PGRpdiBjbGFzcz0ib3B0LWhhbmRsZSIgZGF0YS1pMThuPSJib29rX2Zsb3ciPtCf0L7RgNC+0LTQsCDihpIg0KPRgdC70YPQs9CwIOKGkiDQnNCw0YHRgtC10YAg4oaSINCS0YDQtdC80Y88L2Rpdj48L2Rpdj4KICAgIDxzcGFuIGNsYXNzPSJvcHQtYXJyb3ciPuKGkjwvc3Bhbj4KICA8L2J1dHRvbj4KICA8ZGl2IGNsYXNzPSJkaXZpZGVyIj48c3BhbiBkYXRhLWkxOG49Im9yX2NvbnRhY3QiPm9yIGNvbnRhY3QgdXM8L3NwYW4+PC9kaXY+CiAgPGEgaHJlZj0iaHR0cHM6Ly93d3cuaW5zdGFncmFtLmNvbS9yal9ncm9vbWluZz9pZ3NoPU1XeG1kSE5xY1hGa2FuTnZiUT09IiB0YXJnZXQ9Il9ibGFuayIgY2xhc3M9Im9wdCI+CiAgICA8ZGl2IGNsYXNzPSJvcHQtaWNvbiI+PHN2ZyB3aWR0aD0iMzYiIGhlaWdodD0iMzYiIHZpZXdCb3g9IjAgMCAyNCAyNCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48ZGVmcz48bGluZWFyR3JhZGllbnQgaWQ9ImlnIiB4MT0iMCUiIHkxPSIxMDAlIiB4Mj0iMTAwJSIgeTI9IjAlIj48c3RvcCBvZmZzZXQ9IjAlIiBzdG9wLWNvbG9yPSIjZjA5NDMzIi8+PHN0b3Agb2Zmc2V0PSI1MCUiIHN0b3AtY29sb3I9IiNkYzI3NDMiLz48c3RvcCBvZmZzZXQ9IjEwMCUiIHN0b3AtY29sb3I9IiNiYzE4ODgiLz48L2xpbmVhckdyYWRpZW50PjwvZGVmcz48cmVjdCB3aWR0aD0iMjQiIGhlaWdodD0iMjQiIHJ4PSI2IiBmaWxsPSJ1cmwoI2lnKSIvPjxyZWN0IHg9IjYiIHk9IjYiIHdpZHRoPSIxMiIgaGVpZ2h0PSIxMiIgcng9IjMiIGZpbGw9Im5vbmUiIHN0cm9rZT0id2hpdGUiIHN0cm9rZS13aWR0aD0iMS41Ii8+PGNpcmNsZSBjeD0iMTIiIGN5PSIxMiIgcj0iMyIgZmlsbD0ibm9uZSIgc3Ryb2tlPSJ3aGl0ZSIgc3Ryb2tlLXdpZHRoPSIxLjUiLz48Y2lyY2xlIGN4PSIxNi41IiBjeT0iNy41IiByPSIxIiBmaWxsPSJ3aGl0ZSIvPjwvc3ZnPjwvZGl2PgogICAgPGRpdiBjbGFzcz0ib3B0LXRleHQiPjxkaXYgY2xhc3M9Im9wdC10aXRsZSI+SW5zdGFncmFtPC9kaXY+PGRpdiBjbGFzcz0ib3B0LWhhbmRsZSI+QHJqX2dyb29taW5nPC9kaXY+PC9kaXY+CiAgICA8c3BhbiBjbGFzcz0ib3B0LWFycm93Ij7ihpI8L3NwYW4+CiAgPC9hPgogIDxhIGhyZWY9Imh0dHBzOi8vd2EubWUvMzcyNTg3MzU0NTYiIHRhcmdldD0iX2JsYW5rIiBjbGFzcz0ib3B0Ij4KICAgIDxkaXYgY2xhc3M9Im9wdC1pY29uIj48c3ZnIHdpZHRoPSIzNiIgaGVpZ2h0PSIzNiIgdmlld0JveD0iMCAwIDI0IDI0IiB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciPjxjaXJjbGUgY3g9IjEyIiBjeT0iMTIiIHI9IjEyIiBmaWxsPSIjMjVEMzY2Ii8+PHBhdGggZmlsbD0id2hpdGUiIGQ9Ik0xNy40NzIgMTQuMzgyYy0uMjk3LS4xNDktMS43NTgtLjg2Ny0yLjAzLS45NjctLjI3My0uMDk5LS40NzEtLjE0OC0uNjcuMTUtLjE5Ny4yOTctLjc2Ny45NjYtLjk0IDEuMTY0LS4xNzMuMTk5LS4zNDcuMjIzLS42NDQuMDc1LS4yOTctLjE1LTEuMjU1LS40NjMtMi4zOS0xLjQ3NS0uODgzLS43ODgtMS40OC0xLjc2MS0xLjY1My0yLjA1OS0uMTczLS4yOTctLjAxOC0uNDU4LjEzLS42MDYuMTM0LS4xMzMuMjk4LS4zNDcuNDQ2LS41Mi4xNDktLjE3NC4xOTgtLjI5OC4yOTgtLjQ5Ny4wOTktLjE5OC4wNS0uMzcxLS4wMjUtLjUyLS4wNzUtLjE0OS0uNjY5LTEuNjEyLS45MTYtMi4yMDctLjI0Mi0uNTc5LS40ODctLjUtLjY2OS0uNTEtLjE3My0uMDA4LS4zNzEtLjAxLS41Ny0uMDEtLjE5OCAwLS41Mi4wNzQtLjc5Mi4zNzItLjI3Mi4yOTctMS4wNCAxLjAxNi0xLjA0IDIuNDc5IDAgMS40NjIgMS4wNjUgMi44NzUgMS4yMTMgMy4wNzQuMTQ5LjE5OCAyLjA5NiAzLjIgNS4wNzcgNC40ODcuNzA5LjMwNiAxLjI2Mi40ODkgMS42OTQuNjI1LjcxMi4yMjcgMS4zNi4xOTUgMS44NzEuMTE4LjU3MS0uMDg1IDEuNzU4LS43MTkgMi4wMDYtMS40MTMuMjQ4LS42OTQuMjQ4LTEuMjg5LjE3My0xLjQxMy0uMDc0LS4xMjQtLjI3Mi0uMTk4LS41Ny0uMzQ3Ii8+PC9zdmc+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJvcHQtdGV4dCI+PGRpdiBjbGFzcz0ib3B0LXRpdGxlIj5XaGF0c0FwcDwvZGl2PjxkaXYgY2xhc3M9Im9wdC1oYW5kbGUiPiszNzIgNTg3IDM1NDU2PC9kaXY+PC9kaXY+CiAgICA8c3BhbiBjbGFzcz0ib3B0LWFycm93Ij7ihpI8L3NwYW4+CiAgPC9hPgogIDxhIGhyZWY9Imh0dHBzOi8vd3d3LmZhY2Vib29rLmNvbS9zaGFyZS8xRUxQNktDNnJWLz9taWJleHRpZD13d1hJZnIiIHRhcmdldD0iX2JsYW5rIiBjbGFzcz0ib3B0Ij4KICAgIDxkaXYgY2xhc3M9Im9wdC1pY29uIj48c3ZnIHdpZHRoPSIzNiIgaGVpZ2h0PSIzNiIgdmlld0JveD0iMCAwIDI0IDI0IiB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciPjxjaXJjbGUgY3g9IjEyIiBjeT0iMTIiIHI9IjEyIiBmaWxsPSIjMTg3N0YyIi8+PHBhdGggZmlsbD0id2hpdGUiIGQ9Ik0xMyAxMC41aDJsLjUtMi41SDEzVjYuNWMwLS43LjItMS41IDEuNS0xLjVIMTZWM3MtMS0uMi0yLS4yYy0yLjEgMC0zLjUgMS4zLTMuNSAzLjVWOEg4djIuNWgyLjVWMThIMTN2LTcuNXoiLz48L3N2Zz48L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im9wdC10ZXh0Ij48ZGl2IGNsYXNzPSJvcHQtdGl0bGUiPkZhY2Vib29rPC9kaXY+PGRpdiBjbGFzcz0ib3B0LWhhbmRsZSI+UiZhbXA7SiBHcm9vbWluZzwvZGl2PjwvZGl2PgogICAgPHNwYW4gY2xhc3M9Im9wdC1hcnJvdyI+4oaSPC9zcGFuPgogIDwvYT4KICA8YnV0dG9uIGNsYXNzPSJvcHQiIG9uY2xpY2s9IndpbmRvdy5sb2NhdGlvbi5ocmVmPSd0ZWw6KzM3MjU4NzM1NDU2JyI+CiAgICA8ZGl2IGNsYXNzPSJvcHQtaWNvbiI+PHN2ZyB3aWR0aD0iMzIiIGhlaWdodD0iMzIiIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjYzlhODRjIiBzdHJva2Utd2lkdGg9IjEuNiI+PHBhdGggZD0iTTIyIDE2LjkydjNhMiAyIDAgMDEtMi4xOCAyIDE5Ljc5IDE5Ljc5IDAgMDEtOC42My0zLjA3QTE5LjUgMTkuNSAwIDAxMy4wNyA5LjgyYTE5Ljc5IDE5Ljc5IDAgMDEtMy4wNy04LjY3QTIgMiAwIDAxMiAxaDNhMiAyIDAgMDEyIDEuNzJjLjEyNy45Ni4zNjEgMS45MDMuNyAyLjgxYTIgMiAwIDAxLS40NSAyLjExTDYuOTEgOC45MWExNiAxNiAwIDAwNiA2bDEuMjctMS4yN2EyIDIgMCAwMTIuMTEtLjQ1Yy45MDcuMzM5IDEuODUuNTczIDIuODEuN0EyIDIgMCAwMTIyIDE2LjkyeiIvPjwvc3ZnPjwvZGl2PgogICAgPGRpdiBjbGFzcz0ib3B0LXRleHQiPjxkaXYgY2xhc3M9Im9wdC10aXRsZSIgZGF0YS1pMThuPSJjYWxsX3VzIj5DYWxsIFVzPC9kaXY+PGRpdiBjbGFzcz0ib3B0LWhhbmRsZSI+KzM3MiA1ODcgMzU0NTY8L2Rpdj48L2Rpdj4KICAgIDxzcGFuIGNsYXNzPSJvcHQtYXJyb3ciPuKGkjwvc3Bhbj4KICA8L2J1dHRvbj4KICA8ZGl2IGNsYXNzPSJob21lLWZvb3QiPgogICAgPHNwYW4+VGFsbGlubjwvc3Bhbj48ZGl2IGNsYXNzPSJmZG90Ij48L2Rpdj48c3Bhbj5Fc3RvbmlhPC9zcGFuPjxkaXYgY2xhc3M9ImZkb3QiPjwvZGl2PjxzcGFuPkFsbHZlZWxhZXZhIDQ8L3NwYW4+CiAgPC9kaXY+CjwvZGl2Pgo8L2Rpdj4KCjwhLS0gQk9PS0lORyAtLT4KPGRpdiBjbGFzcz0ic2NyZWVuIiBpZD0iYm9va1NjcmVlbiI+CjxkaXYgY2xhc3M9ImNvbiI+CiAgPGJ1dHRvbiBjbGFzcz0iYmFjay1idG4iIGlkPSJiYWNrQnRuIiBkYXRhLWkxOG49ImJhY2siPuKGkCDQndCw0LfQsNC0PC9idXR0b24+CiAgPGRpdiBjbGFzcz0ibG9nby1yaiI+UiZhbXA7SjwvZGl2PgogIDxkaXYgY2xhc3M9ImxvZ28tc3ViIiBkYXRhLWkxOG49ImxvZ29fc3ViIj5Hcm9vbWluZyDCtyDQotCw0LvQu9C40L08L2Rpdj4KICA8ZGl2IGNsYXNzPSJwcm9ncmVzcyI+CiAgICA8ZGl2IGNsYXNzPSJwcyBhY3RpdmUiIGlkPSJwczEiPjxkaXYgY2xhc3M9InBkb3QiPjwvZGl2PjxzcGFuIGRhdGEtaTE4bj0icHNfc2VydmljZSI+0KPRgdC70YPQs9CwPC9zcGFuPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icGwiIGlkPSJwbDEiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icHMiIGlkPSJwczIiPjxkaXYgY2xhc3M9InBkb3QiPjwvZGl2PjxzcGFuIGRhdGEtaTE4bj0icHNfbWFzdGVyIj7QnNCw0YHRgtC10YA8L3NwYW4+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwbCIgaWQ9InBsMiI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwcyIgaWQ9InBzMyI+PGRpdiBjbGFzcz0icGRvdCI+PC9kaXY+PHNwYW4gZGF0YS1pMThuPSJwc19wZXQiPtCf0LjRgtC+0LzQtdGGPC9zcGFuPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icGwiIGlkPSJwbDMiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icHMiIGlkPSJwczQiPjxkaXYgY2xhc3M9InBkb3QiPjwvZGl2PjxzcGFuIGRhdGEtaTE4bj0icHNfZGF0ZSI+0JTQsNGC0LA8L3NwYW4+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwbCIgaWQ9InBsNCI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwcyIgaWQ9InBzNSI+PGRpdiBjbGFzcz0icGRvdCI+PC9kaXY+PHNwYW4gZGF0YS1pMThuPSJwc19kZXRhaWxzIj7QlNCw0L3QvdGL0LU8L3NwYW4+PC9kaXY+CiAgPC9kaXY+CgogIDwhLS0gU3RlcCAxIC0tPgogIDxkaXYgY2xhc3M9InN0ZXAgc2hvdyIgaWQ9ImJrMSI+CiAgICA8ZGl2IGNsYXNzPSJzbGJsIiBkYXRhLWkxOG49InN0ZXAxX2xibCI+MDEgwrcg0J/QvtGA0L7QtNCwINGB0L7QsdCw0LrQuDwvZGl2PgogICAgPGRpdiBjbGFzcz0iYndyYXAiPgogICAgICA8ZGl2IGNsYXNzPSJzYm94Ij4KICAgICAgICA8c3BhbiBjbGFzcz0ic2kiPvCflI08L3NwYW4+CiAgICAgICAgPGlucHV0IGlkPSJiSW5wdXQiIHR5cGU9InRleHQiIHBsYWNlaG9sZGVyPSLQndCw0YfQvdC40YLQtSDQstCy0L7QtNC40YLRjCDQv9C+0YDQvtC00YMuLi4iIGRhdGEtaTE4bi1waD0iYnJlZWRfcGgiIGF1dG9jb21wbGV0ZT0ib2ZmIj4KICAgICAgICA8YnV0dG9uIGNsYXNzPSJjbHIiIGlkPSJjbHJCdG4iPuKclTwvYnV0dG9uPgogICAgICA8L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0iZHJvcCIgaWQ9ImJEcm9wIj48L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0ic2JhZGdlIiBpZD0ic0JhZGdlIj48L2Rpdj4KICAgIDxkaXYgaWQ9InN2Y1NlYyIgc3R5bGU9ImRpc3BsYXk6bm9uZTttYXJnaW4tdG9wOjE2cHgiPgogICAgICA8ZGl2IGNsYXNzPSJzbGJsIiBkYXRhLWkxOG49InN0ZXAyX2xibCI+MDIgwrcg0KPRgdC70YPQs9CwPC9kaXY+CiAgICAgIDxkaXYgaWQ9InN2Y0xpc3QiPjwvZGl2PgogICAgPC9kaXY+CiAgPC9kaXY+CgogIDwhLS0gU3RlcCAyIC0tPgogIDxkaXYgY2xhc3M9InN0ZXAiIGlkPSJiazIiPgogICAgPGRpdiBjbGFzcz0ic2xibCIgZGF0YS1pMThuPSJzdGVwMl9tYXN0ZXIiPtCS0YvQsdC10YDQuNGC0LUg0LzQsNGB0YLQtdGA0LA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im1hc3RlcnMiPgogICAgICA8ZGl2IGNsYXNzPSJtYnRuIiBkYXRhLW1hc3Rlcj0i0KLQsNGC0YzRj9C90LAiPjxkaXYgY2xhc3M9Im1uYW1lIj7QotCw0YLRjNGP0L3QsDwvZGl2PjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJtYnRuIiBkYXRhLW1hc3Rlcj0i0JDQu9C40YHQsCI+PGRpdiBjbGFzcz0ibW5hbWUiPtCQ0LvQuNGB0LA8L2Rpdj48L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0ibWJ0biIgZGF0YS1tYXN0ZXI9ItCa0YDQuNGB0YLQuNC90LAiPjxkaXYgY2xhc3M9Im1uYW1lIj7QmtGA0LjRgdGC0LjQvdCwPC9kaXY+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9Im1idG4iIGRhdGEtbWFzdGVyPSLQkNC90L3QsCI+PGRpdiBjbGFzcz0ibW5hbWUiPtCQ0L3QvdCwPC9kaXY+PC9kaXY+CiAgICA8L2Rpdj4KICA8L2Rpdj4KCiAgPCEtLSBTdGVwIDMgLS0+CiAgPGRpdiBjbGFzcz0ic3RlcCIgaWQ9ImJrMyI+CiAgICA8ZGl2IGNsYXNzPSJzbGJsIiBkYXRhLWkxOG49InN0ZXAzX2xibCI+0JrQsNC6INC00LDQstC90L4g0LLRiyDQv9C+0YHQtdGJ0LDQu9C4INCz0YDRg9C80LjQvdCzPzwvZGl2PgogICAgPGJ1dHRvbiBjbGFzcz0iZ2J0biIgZGF0YS12YWw9ItCf0LXRgNCy0YvQuSDRgNCw0LciIGRhdGEtaTE4bj0iZzEiPtCf0LXRgNCy0YvQuSDRgNCw0Lc8L2J1dHRvbj4KICAgIDxidXR0b24gY2xhc3M9ImdidG4iIGRhdGEtdmFsPSLQntGCIDEg0LTQviAzINC80LXRgdGP0YbQtdCyIiBkYXRhLWkxOG49ImcyIj7QntGCIDEg0LTQviAzINC80LXRgdGP0YbQtdCyPC9idXR0b24+CiAgICA8YnV0dG9uIGNsYXNzPSJnYnRuIiBkYXRhLXZhbD0i0J7RgiAzINC00L4gNiDQvNC10YHRj9GG0LXQsiIgZGF0YS1pMThuPSJnMyI+0J7RgiAzINC00L4gNiDQvNC10YHRj9GG0LXQsjwvYnV0dG9uPgogICAgPGJ1dHRvbiBjbGFzcz0iZ2J0biIgZGF0YS12YWw9ItCR0L7Qu9C10LUgNiDQvNC10YHRj9GG0LXQsiIgZGF0YS1pMThuPSJnNCI+0JHQvtC70LXQtSA2INC80LXRgdGP0YbQtdCyPC9idXR0b24+CiAgPC9kaXY+CgogIDwhLS0gU3RlcCA0IC0tPgogIDxkaXYgY2xhc3M9InN0ZXAiIGlkPSJiazQiPgogICAgPGRpdiBjbGFzcz0ic2xibCIgZGF0YS1pMThuPSJzdGVwNF9sYmwiPtCS0YvQsdC10YDQuNGC0LUg0LTQsNGC0YM8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImNhbC1oIj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iY2FsLW4iIGlkPSJwcmV2TSI+JiM4MjQ5OzwvYnV0dG9uPgogICAgICA8ZGl2IGNsYXNzPSJjYWwtbSIgaWQ9ImNhbE0iPjwvZGl2PgogICAgICA8YnV0dG9uIGNsYXNzPSJjYWwtbiIgaWQ9Im5leHRNIj4mIzgyNTA7PC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImNnIiBpZD0iY2FsRyI+PC9kaXY+CiAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7Z2FwOjIwcHg7YWxpZ24taXRlbXM6Y2VudGVyO21hcmdpbi10b3A6MTJweDtwYWRkaW5nLXRvcDoxMnB4O2JvcmRlci10b3A6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEpO2ZsZXgtd3JhcDp3cmFwOyI+PGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OHB4OyI+PGRpdiBzdHlsZT0id2lkdGg6MTZweDtoZWlnaHQ6MTZweDtib3JkZXItcmFkaXVzOjUwJTtiYWNrZ3JvdW5kOnJnYmEoOTAsMTgwLDkwLC4xNSk7Ym9yZGVyOjFweCBzb2xpZCAjNWFiNDVhO2ZsZXgtc2hyaW5rOjA7Ij48L2Rpdj48c3BhbiBzdHlsZT0iZm9udC1zaXplOi43cmVtO2NvbG9yOiM5YTk1OTA7bGV0dGVyLXNwYWNpbmc6LjAzZW07IiBkYXRhLWkxOG49ImNhbF9hdmFpbCI+0JXRgdGC0Ywg0YHQstC+0LHQvtC00L3QvtC1INCy0YDQtdC80Y88L3NwYW4+PC9kaXY+PGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OHB4OyI+PGRpdiBzdHlsZT0id2lkdGg6MTZweDtoZWlnaHQ6MTZweDtib3JkZXItcmFkaXVzOjUwJTtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA0KTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEpO2ZsZXgtc2hyaW5rOjA7Ij48L2Rpdj48c3BhbiBzdHlsZT0iZm9udC1zaXplOi43cmVtO2NvbG9yOiM5YTk1OTA7bGV0dGVyLXNwYWNpbmc6LjAzZW07IiBkYXRhLWkxOG49ImNhbF9ub25lIj7QodCy0L7QsdC+0LTQvdC+0LPQviDQstGA0LXQvNC10L3QuCDQvdC10YI8L3NwYW4+PC9kaXY+PC9kaXY+CiAgICA8ZGl2IGlkPSJ0aW1lU2VjIiBzdHlsZT0iZGlzcGxheTpub25lO21hcmdpbi10b3A6MTZweCI+CiAgICAgIDxkaXYgY2xhc3M9InNsYmwiIGRhdGEtaTE4bj0ic3RlcDRfdGltZSI+0JLRi9Cx0LXRgNC40YLQtSDQstGA0LXQvNGPPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9InRnIiBpZD0idGltZUciPjwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IHN0eWxlPSJtYXJnaW4tdG9wOjIwcHg7cGFkZGluZy10b3A6MTZweDtib3JkZXItdG9wOjFweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjEyKTt0ZXh0LWFsaWduOmNlbnRlciI+CiAgICAgIDxidXR0b24gaWQ9ImNhbGxiYWNrQnRuIiBjbGFzcz0iY2JrLWJ0biI+0J3QtSDQvdCw0YjQu9C4INGD0LTQvtCx0L3QvtC1INCy0YDQtdC80Y8/IOKGkjwvYnV0dG9uPgogICAgPC9kaXY+CiAgPC9kaXY+CgogIDwhLS0gU3RlcCA1IC0tPgogIDxkaXYgY2xhc3M9InN0ZXAiIGlkPSJiazUiPgogICAgPGRpdiBjbGFzcz0ic2xibCIgZGF0YS1pMThuPSJzdGVwNV9sYmwiPtCS0LDRiNC4INC00LDQvdC90YvQtTwvZGl2PgogICAgPGRpdiBjbGFzcz0iZmciPjxsYWJlbCBjbGFzcz0iZmwiIGRhdGEtaTE4bj0ibGJsX25hbWUiPtCY0LzRjzwvbGFiZWw+PGlucHV0IGNsYXNzPSJmaSIgaWQ9ImNOYW1lIiB0eXBlPSJ0ZXh0IiBwbGFjZWhvbGRlcj0i0JLQsNGI0LUg0LjQvNGPIiBkYXRhLWkxOG4tcGg9InBoX25hbWUiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0iZmciPjxsYWJlbCBjbGFzcz0iZmwiIGRhdGEtaTE4bj0ibGJsX3Bob25lIj7QotC10LvQtdGE0L7QvTwvbGFiZWw+PGlucHV0IGNsYXNzPSJmaSIgaWQ9ImNQaG9uZSIgdHlwZT0idGVsIiBwbGFjZWhvbGRlcj0iKzM3MiAuLi4iPjwvZGl2PgogICAgPGRpdiBjbGFzcz0iZmciPjxsYWJlbCBjbGFzcz0iZmwiIGRhdGEtaTE4bj0ibGJsX2VtYWlsIj5FbWFpbDwvbGFiZWw+PGlucHV0IGNsYXNzPSJmaSIgaWQ9ImNFbWFpbCIgdHlwZT0iZW1haWwiIHBsYWNlaG9sZGVyPSJlbWFpbEBleGFtcGxlLmNvbSI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJmZyI+PGxhYmVsIGNsYXNzPSJmbCIgZGF0YS1pMThuPSJsYmxfcGV0Ij7QmtC70LjRh9C60LAg0L/QuNGC0L7QvNGG0LA8L2xhYmVsPjxpbnB1dCBjbGFzcz0iZmkiIGlkPSJjUGV0IiB0eXBlPSJ0ZXh0IiBwbGFjZWhvbGRlcj0i0J3QtdC+0LHRj9C30LDRgtC10LvRjNC90L4iIGRhdGEtaTE4bi1waD0icGhfb3B0aW9uYWwiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0ic3VtIiBpZD0ic3VtQmxvY2siPjwvZGl2PgogICAgPGJ1dHRvbiBjbGFzcz0iY2J0biIgaWQ9ImNvbmZpcm1CdG4iIGRhdGEtaTE4bj0iY29uZmlybV9idG4iPtCf0L7QtNGC0LLQtdGA0LTQuNGC0Ywg0LfQsNC/0LjRgdGMPC9idXR0b24+CiAgPC9kaXY+CgogIDwhLS0gU3VjY2VzcyAtLT4KICA8ZGl2IGNsYXNzPSJzYmxvY2siIGlkPSJzdWNCbG9jayI+CiAgICA8ZGl2IGNsYXNzPSJzaTIiPvCfkL48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InN0IiBkYXRhLWkxOG49InN1Y2Nlc3NfdGl0bGUiPtCX0LDQv9C40YHRjCDQv9GA0LjQvdGP0YLQsCE8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InNzIiBkYXRhLWkxOG49InN1Y2Nlc3Nfc3ViIj7QnNGLINGB0LLRj9C20LXQvNGB0Y8g0YEg0LLQsNC80Lgg0LTQu9GPINC/0L7QtNGC0LLQtdGA0LbQtNC10L3QuNGPLjxicj7QodC/0LDRgdC40LHQviwg0YfRgtC+INCy0YvQsdGA0LDQu9C4IFImSiBHcm9vbWluZyE8L2Rpdj4KICAgIDxidXR0b24gY2xhc3M9ImhidG4iIGlkPSJob21lQnRuIiBkYXRhLWkxOG49InRvX2hvbWUiPuKGkCDQndCwINCz0LvQsNCy0L3Rg9GOPC9idXR0b24+CiAgPC9kaXY+CjwvZGl2Pgo8L2Rpdj4KCjxkaXYgaWQ9ImNia01vZGFsIiBzdHlsZT0iZGlzcGxheTpub25lO3Bvc2l0aW9uOmZpeGVkO2luc2V0OjA7YmFja2dyb3VuZDpyZ2JhKDAsMCwwLC43NSk7ei1pbmRleDozMDA7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7cGFkZGluZzoyMHB4Ij4KICA8ZGl2IHN0eWxlPSJiYWNrZ3JvdW5kOiMxYzFjMTg7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjMpO2JvcmRlci10b3A6MnB4IHNvbGlkICNjOWE4NGM7cGFkZGluZzoyOHB4IDI0cHg7d2lkdGg6MTAwJTttYXgtd2lkdGg6MzYwcHgiPgogICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOi41OHJlbTtsZXR0ZXItc3BhY2luZzouMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojYzlhODRjO21hcmdpbi1ib3R0b206MTZweDtmb250LXdlaWdodDo2MDAiPtCe0LHRgNCw0YLQvdGL0Lkg0LfQstC+0L3QvtC6PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJmZyI+PGxhYmVsIGNsYXNzPSJmbCI+0JjQvNGPPC9sYWJlbD48aW5wdXQgY2xhc3M9ImZpIiBpZD0iY2JrTmFtZSIgdHlwZT0idGV4dCIgcGxhY2Vob2xkZXI9ItCS0LDRiNC1INC40LzRjyI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJmZyI+CiAgICAgIDxsYWJlbCBjbGFzcz0iZmwiPtCi0LXQu9C10YTQvtC9PC9sYWJlbD4KICAgICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOnN0cmV0Y2g7YmFja2dyb3VuZDojMTQxNDEwO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMSkiPgogICAgICAgIDxzcGFuIHN0eWxlPSJwYWRkaW5nOjEycHggMTBweDtjb2xvcjojOGE4YTU1O2ZvbnQtc2l6ZTouODVyZW07Ym9yZGVyLXJpZ2h0OjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wOCk7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlciI+KzM3Mjwvc3Bhbj4KICAgICAgICA8aW5wdXQgaWQ9ImNia1Bob25lIiB0eXBlPSJ0ZWwiIHBsYWNlaG9sZGVyPSJYWFhYWFhYWCIgc3R5bGU9ImZsZXg6MTtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjpub25lO291dGxpbmU6bm9uZTtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXNpemU6Ljg1cmVtO2NvbG9yOiNjOGMyYjg7cGFkZGluZzoxMnB4IDE0cHgiPgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBpZD0iY2JrU3VjY2VzcyIgc3R5bGU9ImRpc3BsYXk6bm9uZTt0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjIwcHggMCI+CiAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToycmVtO21hcmdpbi1ib3R0b206MTBweCI+4pyTPC9kaXY+CiAgICAgIDxkaXYgc3R5bGU9ImZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmO2ZvbnQtc2l6ZToxLjNyZW07Y29sb3I6I2M5YTg0YzttYXJnaW4tYm90dG9tOjZweCI+0JfQsNGP0LLQutCwINC/0YDQuNC90Y/RgtCwITwvZGl2PgogICAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6LjcycmVtO2NvbG9yOiM3Nzc3NzA7bGluZS1oZWlnaHQ6MS42Ij7QnNGLINC/0LXRgNC10LfQstC+0L3QuNC8INCy0LDQvCDQsiDQsdC70LjQttCw0LnRiNC10LUg0LLRgNC10LzRjzwvZGl2PgogICAgPC9kaXY+CiAgICA8YnV0dG9uIGlkPSJjYmtTdWJtaXQiIGNsYXNzPSJjYnRuIiBzdHlsZT0ibWFyZ2luLXRvcDoxNHB4Ij7QntGC0L/RgNCw0LLQuNGC0Yw8L2J1dHRvbj4KICAgIDxidXR0b24gaWQ9ImNia0Nsb3NlIiBzdHlsZT0iZGlzcGxheTpibG9jazt3aWR0aDoxMDAlO21hcmdpbi10b3A6OHB4O2JhY2tncm91bmQ6bm9uZTtib3JkZXI6bm9uZTtjb2xvcjojNTU1NTUwO2ZvbnQtc2l6ZTouNjhyZW07bGV0dGVyLXNwYWNpbmc6LjFlbTtjdXJzb3I6cG9pbnRlcjtwYWRkaW5nOjhweDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZiI+0J7RgtC80LXQvdCwPC9idXR0b24+CiAgPC9kaXY+CjwvZGl2PgoKPHNjcmlwdD4KdmFyIERBVEEgPSBbeyJicmVlZCI6ItCQ0LLRgdGC0YDQsNC70LjQudGB0LrQsNGPINC+0LLRh9Cw0YDQutCwIDE14oCTMjUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjYwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjg1fSwiYnJlZWRfZW4iOiJBdXN0cmFsaWFuIFNoZXBoZXJkIDE14oCTMjUga2ciLCJicmVlZF9ldCI6IkF1c3RyYWFsaWEgbGFtYmFrb2VyIDE14oCTMjUga2cifSx7ImJyZWVkIjoi0JDQstGB0YLRgNCw0LvQuNC50YHQutCw0Y8g0L7QstGH0LDRgNC60LAgMjXigJMzNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjU1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJBdXN0cmFsaWFuIFNoZXBoZXJkIDI14oCTMzUga2ciLCJicmVlZF9ldCI6IkF1c3RyYWFsaWEgbGFtYmFrb2VyIDI14oCTMzUga2cifSx7ImJyZWVkIjoi0JDQutC40YLQsC3QuNC90YMgMjDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQWtpdGEgSW51IDIw4oCTNDAga2ciLCJicmVlZF9ldCI6IkFraXRhIEludSAyMOKAkzQwIGtnIn0seyJicmVlZCI6ItCQ0LrQuNGC0LAt0LjQvdGDINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJBa2l0YSBJbnUgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiQWtpdGEgSW51IMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JDQutC40YLQsC3QuNC90YMg0YTQu9Cw0YTRhNC4IDIw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkFraXRhIEludSBmbHVmZnkgMjDigJM0MCBrZyIsImJyZWVkX2V0IjoiQWtpdGEgSW51IHBlaG1la2FydmFsaW5lIDIw4oCTNDAga2cifSx7ImJyZWVkIjoi0JDQutC40YLQsC3QuNC90YMg0YTQu9Cw0YTRhNC4INCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJBa2l0YSBJbnUgZmx1ZmZ5IG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkFraXRhIEludSBwZWhtZWthcnZhbGluZSDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCQ0LvQsNCx0LDQuSA0MOKAkzYwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQ2VudHJhbCBBc2lhbiBTaGVwaGVyZCA0MOKAkzYwIGtnIiwiYnJlZWRfZXQiOiJLZXNrLUFhc2lhIGxhbWJha29lciA0MOKAkzYwIGtnIn0seyJicmVlZCI6ItCQ0LvQsNCx0LDQuSDQsdC+0LvQtdC1IDYwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MTAwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MTE1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTMwfSwiYnJlZWRfZW4iOiJDZW50cmFsIEFzaWFuIFNoZXBoZXJkIG92ZXIgNjAga2ciLCJicmVlZF9ldCI6Iktlc2stQWFzaWEgbGFtYmFrb2VyIMO8bGUgNjAga2cifSx7ImJyZWVkIjoi0JDQu9GP0YHQutC40L3RgdC60LjQuSDQvNCw0LvQsNC80YPRgiAyMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJBbGFza2FuIE1hbGFtdXRlIDIw4oCTNDAga2ciLCJicmVlZF9ldCI6IkFsYXNrYSBtYWxhbXV1dCAyMOKAkzQwIGtnIn0seyJicmVlZCI6ItCQ0LvRj9GB0LrQuNC90YHQutC40Lkg0LzQsNC70LDQvNGD0YIg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkFsYXNrYW4gTWFsYW11dGUgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiQWxhc2thIG1hbGFtdXV0IMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JDQu9GP0YHQutC40L3RgdC60LjQuSDQvNCw0LvQsNC80YPRgiDRhNC70LDRhNGE0LggMjDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQWxhc2thbiBNYWxhbXV0ZSBmbHVmZnkgMjDigJM0MCBrZyIsImJyZWVkX2V0IjoiQWxhc2thIG1hbGFtdXV0IHBlaG1la2FydmFsaW5lIDIw4oCTNDAga2cifSx7ImJyZWVkIjoi0JDQu9GP0YHQutC40L3RgdC60LjQuSDQvNCw0LvQsNC80YPRgiDRhNC70LDRhNGE0Lgg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkFsYXNrYW4gTWFsYW11dGUgZmx1ZmZ5IG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkFsYXNrYSBtYWxhbXV1dCBwZWhtZWthcnZhbGluZSDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LDRjyDQsNC60LjRgtCwIDIw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkFtZXJpY2FuIEFraXRhIDIw4oCTNDAga2ciLCJicmVlZF9ldCI6IkFtZWVyaWthIEFraXRhIDIw4oCTNDAga2cifSx7ImJyZWVkIjoi0JDQvNC10YDQuNC60LDQvdGB0LrQsNGPINCw0LrQuNGC0LAg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkFtZXJpY2FuIEFraXRhIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkFtZWVyaWthIEFraXRhIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JDQvNC10YDQuNC60LDQvdGB0LrQsNGPINCw0LrQuNGC0LAg0YTQu9Cw0YTRhNC4IDIw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkFtZXJpY2FuIEFraXRhIGZsdWZmeSAyMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBBa2l0YSBwZWhtZWthcnZhbGluZSAyMOKAkzQwIGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LDRjyDQsNC60LjRgtCwINGE0LvQsNGE0YTQuCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQW1lcmljYW4gQWtpdGEgZmx1ZmZ5IG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkFtZWVyaWthIEFraXRhIHBlaG1la2FydmFsaW5lIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JDQvNC10YDQuNC60LDQvdGB0LrQuNC5INC60L7QutC10YAt0YHQv9Cw0L3QuNC10LvRjCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IkFtZXJpY2FuIENvY2tlciBTcGFuaWVsIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IkFtZWVyaWthIGtva2Vyc3BhbmplbCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LjQuSDQutC+0LrQtdGALdGB0L/QsNC90LjQtdC70YwgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwfSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBDb2NrZXIgU3BhbmllbCAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBrb2tlcnNwYW5qZWwgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutC40Lkg0YHRgtCw0YTRhNC+0YDQtNGI0LjRgNGB0LrQuNC5INGC0LXRgNGM0LXRgCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiQW1lcmljYW4gU3RhZmZvcmRzaGlyZSBUZXJyaWVyIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkFtZWVyaWthIFN0YWZmb3Jkc2hpcmUgdGVyamVyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JDQvNC10YDQuNC60LDQvdGB0LrQuNC5INGB0YLQsNGE0YTQvtGA0LTRiNC40YDRgdC60LjQuSDRgtC10YDRjNC10YAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjU1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjV9LCJicmVlZF9lbiI6IkFtZXJpY2FuIFN0YWZmb3Jkc2hpcmUgVGVycmllciAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBTdGFmZm9yZHNoaXJlIHRlcmplciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCQ0L3Qs9C70LjQudGB0LrQuNC5INCx0YPQu9GM0LTQvtCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo3MH0sImJyZWVkX2VuIjoiRW5nbGlzaCBCdWxsZG9nIiwiYnJlZWRfZXQiOiJJbmdsaXNlIGJ1bGRvZyJ9LHsiYnJlZWQiOiLQkNC90LPQu9C40LnRgdC60LjQuSDQutC+0LrQtdGALdGB0L/QsNC90LjQtdC70YwgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJFbmdsaXNoIENvY2tlciBTcGFuaWVsIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IkluZ2xpc2Uga29rZXJzcGFuamVsIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0JDQvdCz0LvQuNC50YHQutC40Lkg0LrQvtC60LXRgC3RgdC/0LDQvdC40LXQu9GMIDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4MH0sImJyZWVkX2VuIjoiRW5nbGlzaCBDb2NrZXIgU3BhbmllbCAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiJJbmdsaXNlIGtva2Vyc3BhbmplbCAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCQ0YTQs9Cw0L0gMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwfSwiYnJlZWRfZW4iOiJBZmdoYW4gSG91bmQgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiQWZnYW5pc3Rhbmkga29lciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCQ0YTQs9Cw0L0gMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMH0sImJyZWVkX2VuIjoiQWZnaGFuIEhvdW5kIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkFmZ2FuaXN0YW5pIGtvZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkdCw0YHRgdC10YIt0YXQsNGD0L3QtCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiQmFzc2V0IEhvdW5kIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkJhc3NldGhvdW5kIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JHQsNGB0YHQtdGCLdGF0LDRg9C90LQgMzDigJMzNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjU1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjV9LCJicmVlZF9lbiI6IkJhc3NldCBIb3VuZCAzMOKAkzM1IGtnIiwiYnJlZWRfZXQiOiJCYXNzZXRob3VuZCAzMOKAkzM1IGtnIn0seyJicmVlZCI6ItCR0LXRgNC90YHQutC40Lkg0LfQtdC90L3QtdC90YXRg9C90LQgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjExMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiQmVybmVzZSBNb3VudGFpbiBEb2cgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiQmVybmkgbcOkZ2lrb2VyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JHQtdGA0L3RgdC60LjQuSDQt9C10L3QvdC10L3RhdGD0L3QtCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTMwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJCZXJuZXNlIE1vdW50YWluIERvZyBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJCZXJuaSBtw6RnaWtvZXIgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQkdC40LLQtdGALdC50L7RgNC6INCx0L7Qu9C10LUgMyw1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkJpZXdlciBZb3Jrc2hpcmUgVGVycmllciBvdmVyIDMsNSBrZyIsImJyZWVkX2V0IjoiQmlld2VyIFlvcmtzaGlyZSBUZXJyaWVyIMO8bGUgMyw1IGtnIn0seyJicmVlZCI6ItCR0LjQstC10YAt0LnQvtGA0Log0LTQviAzLDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiQmlld2VyIFlvcmtzaGlyZSBUZXJyaWVyIHVwIHRvIDMsNSBrZyIsImJyZWVkX2V0IjoiQmlld2VyIFlvcmtzaGlyZSBUZXJyaWVyIGt1bmkgMyw1IGtnIn0seyJicmVlZCI6ItCR0LjQs9C70YwgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2MH0sImJyZWVkX2VuIjoiQmVhZ2xlIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IkJpaWdlbCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCR0LjQs9C70YwgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2NX0sImJyZWVkX2VuIjoiQmVhZ2xlIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IkJpaWdlbCAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCR0LjRiNC+0L0t0YTRgNC40LfQtSA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiQmljaG9uIEZyaXPDqSA14oCTMTAga2ciLCJicmVlZF9ldCI6IkJpxaFvbiBGcmlzw6kgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCR0LjRiNC+0L0t0YTRgNC40LfQtSDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiQmljaG9uIEZyaXPDqSB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJCacWhb24gRnJpc8OpIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQkdC+0LrRgdC10YAgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTB9LCJicmVlZF9lbiI6IkJveGVyIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkJva3NlciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCR0L7QutGB0LXRgCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiQm94ZXIgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiQm9rc2VyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JHQvtGA0LTQtdGALdC60L7Qu9C70LggMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6ODB9LCJicmVlZF9lbiI6IkJvcmRlciBDb2xsaWUgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiQm9yZGVya29sbCAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCR0L7RgNC00LXRgC3QutC+0LvQu9C4IDIw4oCTMjUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQm9yZGVyIENvbGxpZSAyMOKAkzI1IGtnIiwiYnJlZWRfZXQiOiJCb3JkZXJrb2xsIDIw4oCTMjUga2cifSx7ImJyZWVkIjoi0JHQvtGB0YLQvtC9LdGC0LXRgNGM0LXRgCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NX0sImJyZWVkX2VuIjoiQm9zdG9uIFRlcnJpZXIgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiQm9zdG9uaSB0ZXJqZXIgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQkdC+0YHRgtC+0L0t0YLQtdGA0YzQtdGAIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDB9LCJicmVlZF9lbiI6IkJvc3RvbiBUZXJyaWVyIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiQm9zdG9uaSB0ZXJqZXIgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCR0YDQsNCx0LDQvdGB0L7QvSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NTB9LCJicmVlZF9lbiI6IkdyaWZmb24gQnJ1eGVsbG9pcyIsImJyZWVkX2V0IjoiQnLDvHNzZWxpIGdyaWZvbiJ9LHsiYnJlZWQiOiLQkdGD0LvRjNGC0LXRgNGM0LXRgCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MH0sImJyZWVkX2VuIjoiQnVsbCBUZXJyaWVyIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkJ1bGx0ZXJqZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQktC10LvRjNGILdC60L7RgNCz0LggMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2NX0sImJyZWVkX2VuIjoiV2Vsc2ggQ29yZ2kgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiV2FsZXNpIGtvcmdpIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0JLQtdC70YzRiC3QutC+0YDQs9C4IDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NzV9LCJicmVlZF9lbiI6IldlbHNoIENvcmdpIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IldhbGVzaSBrb3JnaSAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCS0LXRgdGCLdGF0LDQudC70LXQvdC0LdCy0LDQudGCLdGC0LXRgNGM0LXRgCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiV2VzdCBIaWdobGFuZCBXaGl0ZSBUZXJyaWVyIiwiYnJlZWRfZXQiOiJMw6TDpG5lLcWgb3RpbWFhIHZhbGdlIHRlcmplciJ9LHsiYnJlZWQiOiLQktC+0YHRgtC+0YfQvdC+0YHQuNCx0LjRgNGB0LrQsNGPINC70LDQudC60LAgMTjigJMyNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4NX0sImJyZWVkX2VuIjoiRWFzdCBTaWJlcmlhbiBMYWlrYSAxOOKAkzI1IGtnIiwiYnJlZWRfZXQiOiJJZGEtU2liZXJpIGxhaWthIDE44oCTMjUga2cifSx7ImJyZWVkIjoi0JLQvtGB0YLQvtGH0L3QvtGB0LjQsdC40YDRgdC60LDRjyDQu9Cw0LnQutCwINCx0L7Qu9C10LUgMjUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJFYXN0IFNpYmVyaWFuIExhaWthIG92ZXIgMjUga2ciLCJicmVlZF9ldCI6IklkYS1TaWJlcmkgbGFpa2Egw7xsZSAyNSBrZyJ9LHsiYnJlZWQiOiLQk9C+0LvQtNC10L0t0YDQtdGC0YDQuNCy0LXRgCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkdvbGRlbiBSZXRyaWV2ZXIgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiS3VsZG5lIHJldHJpaXZlciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCT0L7Qu9C00LXQvS3RgNC10YLRgNC40LLQtdGAIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IkdvbGRlbiBSZXRyaWV2ZXIgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiS3VsZG5lIHJldHJpaXZlciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCT0YDQuNGE0YTQvtC9Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJHcmlmZm9uIiwiYnJlZWRfZXQiOiJHcmlmb24ifSx7ImJyZWVkIjoi0JTQsNC70LzQsNGC0LjQvSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NzB9LCJicmVlZF9lbiI6IkRhbG1hdGlhbiIsImJyZWVkX2V0IjoiRGFsbWFhdHNpYSBrb2VyIn0seyJicmVlZCI6ItCU0LbQtdC6LdGA0LDRgdGB0LXQuy3RgtC10YDRjNC10YAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NTB9LCJicmVlZF9lbiI6IkphY2sgUnVzc2VsbCBUZXJyaWVyIHNtb290aCIsImJyZWVkX2V0IjoiSmFjayBSdXNzZWxsaSB0ZXJqZXIgbMO8aGlrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0JTQttC10Lot0YDQsNGB0YHQtdC7LdGC0LXRgNGM0LXRgCDQttC10YHRgtC60L7RiNC10YDRgdGC0L3Ri9C5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJKYWNrIFJ1c3NlbGwgVGVycmllciB3aXJlLWhhaXJlZCIsImJyZWVkX2V0IjoiSmFjayBSdXNzZWxsaSB0ZXJqZXIga2FydWthcnZhbGluZSJ9LHsiYnJlZWQiOiLQlNC+0LHQtdGA0LzQsNC9IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6ODB9LCJicmVlZF9lbiI6IkRvYmVybWFubiAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJEb2Jlcm1hbm4gMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQlNC+0LHQtdGA0LzQsNC9INCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjgwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTV9LCJicmVlZF9lbiI6IkRvYmVybWFubiBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJEb2Jlcm1hbm4gw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQl9Cw0L/QsNC00L3QvtGB0LjQsdC40YDRgdC60LDRjyDQu9Cw0LnQutCwIDE44oCTMjUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjYwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6ODV9LCJicmVlZF9lbiI6Ildlc3QgU2liZXJpYW4gTGFpa2EgMTjigJMyNSBrZyIsImJyZWVkX2V0IjoiTMOkw6RuZS1TaWJlcmkgbGFpa2EgMTjigJMyNSBrZyJ9LHsiYnJlZWQiOiLQl9C+0LvQvtGC0LjRgdGC0YvQuSDRgNC10YLRgNC40LLQtdGAIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJHb2xkZW4gUmV0cmlldmVyIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6Ikt1bGRuZSByZXRyaWl2ZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQl9C+0LvQvtGC0LjRgdGC0YvQuSDRgNC10YLRgNC40LLQtdGAIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjgwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTB9LCJicmVlZF9lbiI6IkdvbGRlbiBSZXRyaWV2ZXIgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiS3VsZG5lIHJldHJpaXZlciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCY0YDQu9Cw0L3QtNGB0LrQuNC5INC80Y/Qs9C60L7RiNC10YDRgdGC0L3Ri9C5INC/0YjQtdC90LjRh9C90YvQuSDRgtC10YDRjNC10YAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODB9LCJicmVlZF9lbiI6IklyaXNoIFNvZnQgQ29hdGVkIFdoZWF0ZW4gVGVycmllciIsImJyZWVkX2V0IjoiSWlyaSBwZWhtZWthcnZhbmUgbmlzdXbDpHJ2aSB0ZXJqZXIifSx7ImJyZWVkIjoi0JjRgNC70LDQvdC00YHQutC40Lkg0YLQtdGA0YzQtdGAIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJJcmlzaCBUZXJyaWVyIiwiYnJlZWRfZXQiOiJJaXJpIHRlcmplciJ9LHsiYnJlZWQiOiLQmNGB0L/QsNC90YHQutC40Lkg0LPQsNC70YzQs9C+IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NzB9LCJicmVlZF9lbiI6IlNwYW5pc2ggR2FsZ28gMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiSGlzcGFhbmlhIGdhbGdvIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JjRgdC/0LDQvdGB0LrQuNC5INCz0LDQu9GM0LPQviAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjgwfSwiYnJlZWRfZW4iOiJTcGFuaXNoIEdhbGdvIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6Ikhpc3BhYW5pYSBnYWxnbyAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCZ0L7RgNC60YjQuNGA0YHQutC40Lkg0YLQtdGA0YzQtdGAINCx0L7Qu9C10LUgMyw1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IllvcmtzaGlyZSBUZXJyaWVyIG92ZXIgMyw1IGtnIiwiYnJlZWRfZXQiOiJZb3Jrc2hpcmUgdGVyamVyIMO8bGUgMyw1IGtnIn0seyJicmVlZCI6ItCZ0L7RgNC60YjQuNGA0YHQutC40Lkg0YLQtdGA0YzQtdGAINC00L4gMyw1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IllvcmtzaGlyZSBUZXJyaWVyIHVwIHRvIDMsNSBrZyIsImJyZWVkX2V0IjoiWW9ya3NoaXJlIHRlcmplciBrdW5pIDMsNSBrZyJ9LHsiYnJlZWQiOiLQmtCw0LLQsNC70LXRgC3QutC40L3Qsy3Rh9Cw0YDQu9GM0Lct0YHQv9Cw0L3QuNC10LvRjCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IkNhdmFsaWVyIEtpbmcgQ2hhcmxlcyBTcGFuaWVsIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IkNhdmFsaWVyIEtpbmcgQ2hhcmxlcyBTcGFuaWVsIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0JrQsNCy0LDQu9C10YAt0LrQuNC90LMt0YfQsNGA0LvRjNC3LdGB0L/QsNC90LjQtdC70YwgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkNhdmFsaWVyIEtpbmcgQ2hhcmxlcyBTcGFuaWVsIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiQ2F2YWxpZXIgS2luZyBDaGFybGVzIFNwYW5pZWwgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCa0LDQvdC1LdC60L7RgNGB0L4gNDDigJM2MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODV9LCJicmVlZF9lbiI6IkNhbmUgQ29yc28gNDDigJM2MCBrZyIsImJyZWVkX2V0IjoiQ2FuZSBDb3JzbyA0MOKAkzYwIGtnIn0seyJicmVlZCI6ItCa0LDQvdC1LdC60L7RgNGB0L4g0LHQvtC70LXQtSA2MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjkwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MTA1fSwiYnJlZWRfZW4iOiJDYW5lIENvcnNvIG92ZXIgNjAga2ciLCJicmVlZF9ldCI6IkNhbmUgQ29yc28gw7xsZSA2MCBrZyJ9LHsiYnJlZWQiOiLQmtCw0YDQtdC70L4t0YTQuNC90YHQutCw0Y8g0LvQsNC50LrQsCDQtNC+IDEzINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjY1fSwiYnJlZWRfZW4iOiJLYXJlbGlhbi1GaW5uaXNoIExhaWthIHVwIHRvIDEzIGtnIiwiYnJlZWRfZXQiOiJLYXJqYWxhLVNvb21lIGxhaWthIGt1bmkgMTMga2cifSx7ImJyZWVkIjoi0JrQuNGC0LDQudGB0LrQsNGPINGF0L7RhdC70LDRgtCw0Y8g0LPQvtC70LDRjyA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMiwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQyLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiQ2hpbmVzZSBDcmVzdGVkIGhhaXJsZXNzIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiSGlpbmEgaGFyamFrb2VyIGthcnZhdHUgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCa0LjRgtCw0LnRgdC60LDRjyDRhdC+0YXQu9Cw0YLQsNGPINCz0L7Qu9Cw0Y8g0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MjgsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjozNSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkNoaW5lc2UgQ3Jlc3RlZCBoYWlybGVzcyB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJIaWluYSBoYXJqYWtvZXIga2FydmF0dSBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JrQuNGC0LDQudGB0LrQsNGPINGF0L7RhdC70LDRgtCw0Y8g0L/Rg9GF0L7QstCw0Y8gNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkNoaW5lc2UgQ3Jlc3RlZCBwb3dkZXJwdWZmIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiSGlpbmEgaGFyamFrb2VyIFBvd2RlcnB1ZmYgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCa0LjRgtCw0LnRgdC60LDRjyDRhdC+0YXQu9Cw0YLQsNGPINC/0YPRhdC+0LLQsNGPINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJDaGluZXNlIENyZXN0ZWQgcG93ZGVycHVmZiB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJIaWluYSBoYXJqYWtvZXIgUG93ZGVycHVmZiBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JrQvtC60LDQv9GDIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjc1fSwiYnJlZWRfZW4iOiJDb2NrYXBvbyA14oCTMTAga2ciLCJicmVlZF9ldCI6IkNvY2thcG9vIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQmtC+0LrQsNC/0YMg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjV9LCJicmVlZF9lbiI6IkNvY2thcG9vIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IkNvY2thcG9vIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQmtC+0LvQu9C4IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQ29sbGllIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IktvbGwgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQmtC+0LvQu9C4IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IkNvbGxpZSAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJLb2xsIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JrQvtC80L7QvdC00L7RgCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTEwfSwiYnJlZWRfZW4iOiJLb21vbmRvciAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJLb21vbmRvciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCa0L7QvNC+0L3QtNC+0YAg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEzMH0sImJyZWVkX2VuIjoiS29tb25kb3Igb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiS29tb25kb3Igw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQm9Cw0LHRgNCw0LTQvtGAINCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdGL0LkgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo3MH0sImJyZWVkX2VuIjoiTGFicmFkb3IgUmV0cmlldmVyIHNtb290aCAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvcmkgcmV0cmlpdmVyIGzDvGhpa2FydmFsaW5lIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00L7RgCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3Ri9C5IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6ODB9LCJicmVlZF9lbiI6IkxhYnJhZG9yIFJldHJpZXZlciBzbW9vdGggMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiTGFicmFkb3JpIHJldHJpaXZlciBsw7xoaWthcnZhbGluZSAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNC+0YAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjk1fSwiYnJlZWRfZW4iOiJMYWJyYWRvciBSZXRyaWV2ZXIgc21vb3RoIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9yaSByZXRyaWl2ZXIgbMO8aGlrYXJ2YWxpbmUgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQm9Cw0LHRgNCw0LTQvtGAINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdGL0LkgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJMYWJyYWRvciBSZXRyaWV2ZXIgbG9uZy1jb2F0ZWQgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiTGFicmFkb3JpIHJldHJpaXZlciBwaWtrYXJ2YWxpbmUgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQm9Cw0LHRgNCw0LTQvtGAINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdGL0LkgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjExMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiTGFicmFkb3IgUmV0cmlldmVyIGxvbmctY29hdGVkIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9yaSByZXRyaWl2ZXIgcGlra2FydmFsaW5lIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00L7RgCDQtNC70LjQvdC90L7RiNC10YDRgdGC0L3Ri9C5INCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMzAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkxhYnJhZG9yIFJldHJpZXZlciBsb25nLWNvYXRlZCBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvcmkgcmV0cmlpdmVyIHBpa2thcnZhbGluZSDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNGD0LTQtdC70YwgMTDigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJMYWJyYWRvb2RsZSAxMOKAkzIwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvb2RsZSAxMOKAkzIwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNGD0LTQtdC70YwgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwfSwiYnJlZWRfZW4iOiJMYWJyYWRvb2RsZSAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvb2RsZSAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNGD0LTQtdC70YwgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMH0sImJyZWVkX2VuIjoiTGFicmFkb29kbGUgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiTGFicmFkb29kbGUgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQm9C10LLRgNC10YLQutCwIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDB9LCJicmVlZF9lbiI6Ikl0YWxpYW4gR3JleWhvdW5kIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiSXRhYWxpYSB2aW5ka29lciA14oCTMTAga2cifSx7ImJyZWVkIjoi0JvQtdCy0YDQtdGC0LrQsCDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1fSwiYnJlZWRfZW4iOiJJdGFsaWFuIEdyZXlob3VuZCB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJJdGFhbGlhIHZpbmRrb2VyIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQm9GF0LDRgdGB0LrQuNC5INCw0L/RgdC+IDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjc1fSwiYnJlZWRfZW4iOiJMaGFzYSBBcHNvIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiTGhhc2EgQXBzbyA14oCTMTAga2cifSx7ImJyZWVkIjoi0JvRhdCw0YHRgdC60LjQuSDQsNC/0YHQviDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2NX0sImJyZWVkX2VuIjoiTGhhc2EgQXBzbyB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJMaGFzYSBBcHNvIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQnNCw0LvRjNGC0LXQt9C1Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJNYWx0ZXNlIiwiYnJlZWRfZXQiOiJNYWx0YSBib2xvbmVlcyJ9LHsiYnJlZWQiOiLQnNCw0LvRjNGC0LjQudGB0LrQsNGPINCx0L7Qu9C+0L3QutCwIDXigJM4INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjV9LCJicmVlZF9lbiI6Ik1hbHRlc2UgQm9sb2duZXNlIDXigJM4IGtnIiwiYnJlZWRfZXQiOiJNYWx0YSBib2xvbmVlcyA14oCTOCBrZyJ9LHsiYnJlZWQiOiLQnNCw0LvRjNGC0LjQudGB0LrQsNGPINCx0L7Qu9C+0L3QutCwINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJNYWx0ZXNlIEJvbG9nbmVzZSB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJNYWx0YSBib2xvbmVlcyBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JzQsNC70YzRgtC40L/RgyAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6Ik1hbHRpcG9vIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6Ik1hbHRpcHV1IDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0JzQsNC70YzRgtC40L/RgyA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiTWFsdGlwb28gNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJNYWx0aXB1dSA14oCTMTAga2cifSx7ImJyZWVkIjoi0JzQsNC70YzRgtC40L/RgyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiTWFsdGlwb28gdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiTWFsdGlwdXUga3VuaSA1IGtnIn0seyJicmVlZCI6ItCc0LXRgtC40YEg0LrRgNGD0L/QvdGL0LkgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiTWl4ZWQgYnJlZWQgbGFyZ2UgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiU2VnYXZlcmQgc3V1ciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCc0LXRgtC40YEg0LrRgNGD0L/QvdGL0Lkg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjc1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEyMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEyMH0sImJyZWVkX2VuIjoiTWl4ZWQgYnJlZWQgbGFyZ2Ugb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiU2VnYXZlcmQgc3V1ciDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCc0LXRgtC40YEg0LzQtdC70LrQuNC5IDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJNaXhlZCBicmVlZCBzbWFsbCA14oCTMTAga2ciLCJicmVlZF9ldCI6IlNlZ2F2ZXJkIHbDpGlrZSA14oCTMTAga2cifSx7ImJyZWVkIjoi0JzQtdGC0LjRgSDQvNC10LvQutC40Lkg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MjUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjozNSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTB9LCJicmVlZF9lbiI6Ik1peGVkIGJyZWVkIHNtYWxsIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IlNlZ2F2ZXJkIHbDpGlrZSBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JzQtdGC0LjRgSDRgdGA0LXQtNC90LjQuSAxMOKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo3MH0sImJyZWVkX2VuIjoiTWl4ZWQgYnJlZWQgbWVkaXVtIDEw4oCTMjAga2ciLCJicmVlZF9ldCI6IlNlZ2F2ZXJkIGtlc2ttaW5lIDEw4oCTMjAga2cifSx7ImJyZWVkIjoi0JzQtdGC0LjRgSDRgdGA0LXQtNC90LjQuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4NX0sImJyZWVkX2VuIjoiTWl4ZWQgYnJlZWQgbWVkaXVtIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IlNlZ2F2ZXJkIGtlc2ttaW5lIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JzQuNGC0YLQtdC70YzRiNC90LDRg9GG0LXRgCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IlN0YW5kYXJkIFNjaG5hdXplciAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJTdGFuZGFyZMWhbmF1dHNlciAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCc0LjRgtGC0LXQu9GM0YjQvdCw0YPRhtC10YAgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwfSwiYnJlZWRfZW4iOiJTdGFuZGFyZCBTY2huYXV6ZXIgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiU3RhbmRhcmTFoW5hdXRzZXIgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQnNC+0L/RgSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NTB9LCJicmVlZF9lbiI6IlB1ZyIsImJyZWVkX2V0IjoiTW9wcyJ9LHsiYnJlZWQiOiLQndC10LLRgdC60LDRjyDQvtGA0YXQuNC00LXRjyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiTmV2YSBPcmNoaWQiLCJicmVlZF9ldCI6Ik5lZXZhIG9yaGlkZWUifSx7ImJyZWVkIjoi0J3QtdC80LXRhtC60LDRjyDQvtCy0YfQsNGA0LrQsCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJHZXJtYW4gU2hlcGhlcmQgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiU2Frc2EgbGFtYmFrb2VyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0J3QtdC80LXRhtC60LDRjyDQvtCy0YfQsNGA0LrQsCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiR2VybWFuIFNoZXBoZXJkIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IlNha3NhIGxhbWJha29lciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCd0LXQvNC10YbQutCw0Y8g0L7QstGH0LDRgNC60LAg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6Ikdlcm1hbiBTaGVwaGVyZCBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJTYWtzYSBsYW1iYWtvZXIgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQndC+0YDQstC40Yct0YLQtdGA0YzQtdGAIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJOb3J3aWNoIFRlcnJpZXIiLCJicmVlZF9ldCI6Ik5vcndpdMWhaSB0ZXJqZXIifSx7ImJyZWVkIjoi0J3QvtGA0YTQvtC70Lot0YLQtdGA0YzQtdGAIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJOb3Jmb2xrIFRlcnJpZXIiLCJicmVlZF9ldCI6Ik5vcmZvbGtpIHRlcmplciJ9LHsiYnJlZWQiOiLQndGM0Y7RhNCw0YPQvdC00LvQtdC90LQgNDDigJM2MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEzMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiTmV3Zm91bmRsYW5kIDQw4oCTNjAga2ciLCJicmVlZF9ldCI6Ik5ld2ZvdW5kbGFuZGkga29lciA0MOKAkzYwIGtnIn0seyJicmVlZCI6ItCd0YzRjtGE0LDRg9C90LTQu9C10L3QtCDQsdC+0LvQtdC1IDYwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MTAwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MTE1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxNTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMzB9LCJicmVlZF9lbiI6Ik5ld2ZvdW5kbGFuZCBvdmVyIDYwIGtnIiwiYnJlZWRfZXQiOiJOZXdmb3VuZGxhbmRpIGtvZXIgw7xsZSA2MCBrZyJ9LHsiYnJlZWQiOiLQn9Cw0L/QuNC50L7QvSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiUGFwaWxsb24iLCJicmVlZF9ldCI6IlBhcGlsbG9uIn0seyJicmVlZCI6ItCf0LXQutC40L3QtdGBIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJQZWtpbmdlc2UgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJQZWtpbmVzaSBrb2VyIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQn9C10LrQuNC90LXRgSDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiUGVraW5nZXNlIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IlBla2luZXNpIGtvZXIga3VuaSA1IGtnIn0seyJicmVlZCI6ItCf0YPQtNC10LvRjCDQsdC+0LvRjNGI0L7QuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTB9LCJicmVlZF9lbiI6IlN0YW5kYXJkIFBvb2RsZSAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJTdGFuZGFyZHB1dWRlbCAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCf0YPQtNC10LvRjCDQsdC+0LvRjNGI0L7QuSAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwfSwiYnJlZWRfZW4iOiJTdGFuZGFyZCBQb29kbGUgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiU3RhbmRhcmRwdXVkZWwgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQn9GD0LTQtdC70Ywg0LrQsNGA0LvQuNC60L7QstGL0LkgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6Ik1pbmlhdHVyZSBQb29kbGUgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJLw6TDpGJ1c3B1dWRlbCA14oCTMTAga2cifSx7ImJyZWVkIjoi0J/Rg9C00LXQu9GMINC80LDQu9GL0LkgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJTbWFsbCBQb29kbGUgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiVsOkaWtlIHB1dWRlbCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCf0YPQtNC10LvRjCDQvNCw0LvRi9C5IDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4MH0sImJyZWVkX2VuIjoiU21hbGwgUG9vZGxlIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IlbDpGlrZSBwdXVkZWwgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQn9GD0LTQtdC70Ywg0YLQvtC5INC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJUb3kgUG9vZGxlIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6Ik3DpG5ndWFzamEgcHV1ZGVsIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQoNC40LfQtdC90YjQvdCw0YPRhtC10YAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMH0sImJyZWVkX2VuIjoiR2lhbnQgU2NobmF1emVyIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IlN1dXLFoW5hdXRzZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQoNC40LfQtdC90YjQvdCw0YPRhtC10YAg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjc1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEyMH0sImJyZWVkX2VuIjoiR2lhbnQgU2NobmF1emVyIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IlN1dXLFoW5hdXRzZXIgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQoNGD0YHRgdC60LDRjyDRhtCy0LXRgtC90LDRjyDQsdC+0LvQvtC90LrQsCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiUnVzc2lhbiBDb2xvcmVkIExhcGRvZyIsImJyZWVkX2V0IjoiVmVuZSB2w6RydmlsaW5lIHPDvGxla29lciJ9LHsiYnJlZWQiOiLQoNGD0YHRgdC60LjQuSDQvtGF0L7RgtC90LjRh9C40Lkg0YHQv9Cw0L3QuNC10LvRjCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IlJ1c3NpYW4gU3BhbmllbCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJWZW5lIGphaGlzcGFuamVsIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KDRg9GB0YHQutC40Lkg0L7RhdC+0YLQvdC40YfQuNC5INGB0L/QsNC90LjQtdC70YwgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwfSwiYnJlZWRfZW4iOiJSdXNzaWFuIFNwYW5pZWwgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiVmVuZSBqYWhpc3BhbmplbCAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCg0YPRgdGB0LrQuNC5INGC0L7QuSDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3Ri9C5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjI1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MzV9LCJicmVlZF9lbiI6IlJ1c3NpYW4gVG95IHNtb290aCIsImJyZWVkX2V0IjoiVmVuZSBUb3kgbMO8aGlrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0KDRg9GB0YHQutC40Lkg0YLQvtC5INC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdGL0LkiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IlJ1c3NpYW4gVG95IGxvbmctY29hdGVkIiwiYnJlZWRfZXQiOiJWZW5lIFRveSBwaWtrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0KDRg9GB0YHQutC40Lkg0YfQtdGA0L3Ri9C5INGC0LXRgNGM0LXRgCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwfSwiYnJlZWRfZW4iOiJCbGFjayBSdXNzaWFuIFRlcnJpZXIgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiTXVzdCBWZW5lIHRlcmplciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCg0YPRgdGB0LrQuNC5INGH0LXRgNC90YvQuSDRgtC10YDRjNC10YAg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjc1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEyMH0sImJyZWVkX2VuIjoiQmxhY2sgUnVzc2lhbiBUZXJyaWVyIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6Ik11c3QgVmVuZSB0ZXJqZXIgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQoNGD0YHRgdC60L4t0LXQstGA0L7Qv9C10LnRgdC60LDRjyDQu9Cw0LnQutCwIDIw4oCTMjgg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IlJ1c3NpYW4tRXVyb3BlYW4gTGFpa2EgMjDigJMyOCBrZyIsImJyZWVkX2V0IjoiVmVuZS1FdXJvb3BhIGxhaWthIDIw4oCTMjgga2cifSx7ImJyZWVkIjoi0KHQsNC80L7QtdC0IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IlNhbW95ZWQgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiU2Ftb2plZWQgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQodCw0LzQvtC10LQgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IlNhbW95ZWQgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiU2Ftb2plZWQgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQodC10YLRgtC10YAg0LDQvdCz0LvQuNC50YHQutC40LkgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwfSwiYnJlZWRfZW4iOiJFbmdsaXNoIFNldHRlciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJJbmdsaXNlIHNldHRlciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCh0LXRgtGC0LXRgCDQs9C+0YDQtNC+0L0gMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMH0sImJyZWVkX2VuIjoiR29yZG9uIFNldHRlciAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJHb3Jkb25pIHNldHRlciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCh0LXRgtGC0LXRgCDQuNGA0LvQsNC90LTRgdC60LjQuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTB9LCJicmVlZF9lbiI6IklyaXNoIFNldHRlciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJJaXJpIHNldHRlciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCh0LjQsdCwLdC40L3RgyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjYwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NzB9LCJicmVlZF9lbiI6IlNoaWJhIEludSIsImJyZWVkX2V0IjoiU2hpYmEgSW51In0seyJicmVlZCI6ItCh0LjQu9C40YXQtdC8LdGC0LXRgNGM0LXRgCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiU2VhbHloYW0gVGVycmllciIsImJyZWVkX2V0IjoiU2VhbHloYW1pIHRlcmplciJ9LHsiYnJlZWQiOiLQodC60L7RgtGHLdGC0LXRgNGM0LXRgCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiU2NvdHRpc2ggVGVycmllciIsImJyZWVkX2V0IjoixaBvdGkgdGVyamVyIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90LDRjyDQutCw0YDQu9C40LrQvtCy0LDRjyA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NTB9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCBzbW9vdGggbWluaWF0dXJlIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiVGFrc2lrb2VyIGzDvGhpa2FydmFsaW5lIGvDpMOkYnVzIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdCw0Y8g0LrRgNC+0LvQuNGH0YzRjyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NDV9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCBzbW9vdGggcmFiYml0IHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBsw7xoaWthcnZhbGluZSBrw7zDvGxpayBrdW5pIDUga2cifSx7ImJyZWVkIjoi0KLQsNC60YHQsCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3QsNGPINGB0YLQsNC90LTQsNGA0YLQvdCw0Y8gMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2MH0sImJyZWVkX2VuIjoiRGFjaHNodW5kIHNtb290aCBzdGFuZGFyZCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIgbMO8aGlrYXJ2YWxpbmUgc3RhbmRhcmQgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdCw0Y8g0LrQsNGA0LvQuNC60L7QstCw0Y8gNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCBsb25nLWNvYXRlZCBtaW5pYXR1cmUgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIgcGlra2FydmFsaW5lIGvDpMOkYnVzIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdCw0Y8g0LrRgNC+0LvQuNGH0YzRjyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiRGFjaHNodW5kIGxvbmctY29hdGVkIHJhYmJpdCB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIgcGlra2FydmFsaW5lIGvDvMO8bGlrIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdCw0Y8g0YHRgtCw0L3QtNCw0YDRgtC90LDRjyAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCBsb25nLWNvYXRlZCBzdGFuZGFyZCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIgcGlra2FydmFsaW5lIHN0YW5kYXJkIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KLQsNC60YHQsCDQttC10YHRgtC60L7RiNC10YDRgdGC0L3QsNGPINC60LDRgNC70LjQutC+0LLQsNGPIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJEYWNoc2h1bmQgd2lyZS1oYWlyZWQgbWluaWF0dXJlIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiVGFrc2lrb2VyIGthcnVrYXJ2YWxpbmUga8Okw6RidXMgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90LDRjyDQutGA0L7Qu9C40YfRjNGPINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJEYWNoc2h1bmQgd2lyZS1oYWlyZWQgcmFiYml0IHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBrYXJ1a2FydmFsaW5lIGvDvMO8bGlrIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINC20LXRgdGC0LrQvtGI0LXRgNGB0YLQvdCw0Y8g0YHRgtCw0L3QtNCw0YDRgtC90LDRjyAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCB3aXJlLWhhaXJlZCBzdGFuZGFyZCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIga2FydWthcnZhbGluZSBzdGFuZGFyZCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCj0LjQv9C/0LXRgiAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NX0sImJyZWVkX2VuIjoiV2hpcHBldCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJXaGlwcGV0IDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KPQuNC/0L/QtdGCIDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwfSwiYnJlZWRfZW4iOiJXaGlwcGV0IDE14oCTMjAga2ciLCJicmVlZF9ldCI6IldoaXBwZXQgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQpNC40L3RgdC60LjQuSDQu9Cw0L/RhdGD0L3QtCAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODV9LCJicmVlZF9lbiI6IkZpbm5pc2ggTGFwcGh1bmQgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiU29vbWUgbGFtYmFrb2VyIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0KTQuNC90YHQutC40Lkg0LvQsNC/0YXRg9C90LQgMjDigJMyNCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjU1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwfSwiYnJlZWRfZW4iOiJGaW5uaXNoIExhcHBodW5kIDIw4oCTMjQga2ciLCJicmVlZF9ldCI6IlNvb21lIGxhbWJha29lciAyMOKAkzI0IGtnIn0seyJicmVlZCI6ItCk0L7QutGB0YLQtdGA0YzQtdGAINC20LXRgdGC0LrQvtGI0LXRgNGB0YLQvdGL0LkgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJXaXJlIEZveCBUZXJyaWVyIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IkthcnVrYXJ2YWxpbmUgZm94dGVyamVyIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KTQvtC60YHRgtC10YDRjNC10YAg0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90YvQuSA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiV2lyZSBGb3ggVGVycmllciA14oCTMTAga2ciLCJicmVlZF9ldCI6IkthcnVrYXJ2YWxpbmUgZm94dGVyamVyIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQpNGA0LDQvdGG0YPQt9GB0LrQuNC5INCx0YPQu9GM0LTQvtCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2MH0sImJyZWVkX2VuIjoiRnJlbmNoIEJ1bGxkb2ciLCJicmVlZF9ldCI6IlByYW50c3VzZSBidWxkb2cifSx7ImJyZWVkIjoi0KXQsNGB0LrQuCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJTaWJlcmlhbiBIdXNreSAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJTaWJlcmkgaHVza3kgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQpdCw0YHQutC4IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJTaWJlcmlhbiBIdXNreSAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJTaWJlcmkgaHVza3kgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQptCy0LXRgNCz0YjQvdCw0YPRhtC10YAgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJNaW5pYXR1cmUgU2NobmF1emVyIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IkvDpMOkYnVzxaFuYXV0c2VyIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KbQstC10YDQs9GI0L3QsNGD0YbQtdGAIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJNaW5pYXR1cmUgU2NobmF1emVyIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiS8Okw6RidXPFoW5hdXRzZXIgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCn0LDRgy3Rh9Cw0YMgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJDaG93IENob3cgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiQ2hvdyBDaG93IDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0KfQsNGDLdGH0LDRgyAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTEwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJDaG93IENob3cgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiQ2hvdyBDaG93IDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0KfQuNGF0YPQsNGF0YPQsCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3Ri9C5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjI1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MzV9LCJicmVlZF9lbiI6IkNoaWh1YWh1YSBzbW9vdGgiLCJicmVlZF9ldCI6IlTFoWlodWFodWEgbMO8aGlrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0KfQuNGF0YPQsNGF0YPQsCDQtNC70LjQvdC90L7RiNC10YDRgdGC0L3Ri9C5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJDaGlodWFodWEgbG9uZy1jb2F0ZWQiLCJicmVlZF9ldCI6IlTFoWlodWFodWEgcGlra2FydmFsaW5lIn0seyJicmVlZCI6ItCo0LDRgNC/0LXQuSAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjY1fSwiYnJlZWRfZW4iOiJTaGFyIFBlaSAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiLFoGFyLVBlaSAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCo0LDRgNC/0LXQuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjcwfSwiYnJlZWRfZW4iOiJTaGFyIFBlaSAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiLFoGFyLVBlaSAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCo0LXQu9GC0LgiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2MH0sImJyZWVkX2VuIjoiU2hldGxhbmQgU2hlZXBkb2ciLCJicmVlZF9ldCI6IsWgZXRsYW5kaSBsYW1iYWtvZXIifSx7ImJyZWVkIjoi0KjQuC3RgtGG0YMgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IlNoaWggVHp1IDXigJMxMCBrZyIsImJyZWVkX2V0IjoiU2hpaCBUenUgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCo0Lgt0YLRhtGDINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJTaGloIFR6dSB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJTaGloIFR6dSBrdW5pIDUga2cifSx7ImJyZWVkIjoi0KjQvdCw0YPRhtC10YAg0LzQuNC90LjQsNGC0Y7RgNC90YvQuSDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1fSwiYnJlZWRfZW4iOiJNaW5pYXR1cmUgU2NobmF1emVyIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IkvDpMOkYnVzxaFuYXV0c2VyIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQqNC/0LjRhiDQvdC10LzQtdGG0LrQuNC5IC8g0L/QvtC80LXRgNCw0L3RgdC60LjQuSDQsdC+0LvQtdC1IDMsNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjB9LCJicmVlZF9lbiI6Ikdlcm1hbiBTcGl0eiAvIFBvbWVyYW5pYW4gb3ZlciAzLDUga2ciLCJicmVlZF9ldCI6IlNha3NhIHNwaXRzIC8gUG9tZXJhbmlhbiDDvGxlIDMsNSBrZyJ9LHsiYnJlZWQiOiLQqNC/0LjRhiDQvdC10LzQtdGG0LrQuNC5IC8g0L/QvtC80LXRgNCw0L3RgdC60LjQuSDQtNC+IDMsNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NTV9LCJicmVlZF9lbiI6Ikdlcm1hbiBTcGl0eiAvIFBvbWVyYW5pYW4gdXAgdG8gMyw1IGtnIiwiYnJlZWRfZXQiOiJTYWtzYSBzcGl0cyAvIFBvbWVyYW5pYW4ga3VuaSAzLDUga2cifSx7ImJyZWVkIjoi0KjQv9C40YYg0Y/Qv9C+0L3RgdC60LjQuSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjYwfSwiYnJlZWRfZW4iOiJKYXBhbmVzZSBTcGl0eiIsImJyZWVkX2V0IjoiSmFhcGFuaSBzcGl0cyJ9LHsiYnJlZWQiOiLQrdGB0YLQvtC90YHQutCw0Y8g0LPQvtC90YfQsNGPIDE14oCTMjUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwfSwiYnJlZWRfZW4iOiJFc3RvbmlhbiBIb3VuZCAxNeKAkzI1IGtnIiwiYnJlZWRfZXQiOiJFZXN0aSBoYWdpamFzIDE14oCTMjUga2cifSx7ImJyZWVkIjoi0K/Qv9C+0L3RgdC60LjQuSDRhdC40L0iLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkphcGFuZXNlIENoaW4iLCJicmVlZF9ldCI6IkphYXBhbmkgQ2hpbiJ9LHsiYnJlZWQiOiLQmtC+0YjQutCwINC60L7RgNC+0YLQutC+0YjQtdGA0YHRgtC90LDRjyIsInNlcnZpY2VzIjp7ItCS0YvRh9C10YEiOjQ1fSwiYnJlZWRfZW4iOiJDYXQgc2hvcnQtaGFpcmVkIiwiYnJlZWRfZXQiOiJLYXNzIGzDvGhpa2FydmFsaW5lIn0seyJicmVlZCI6ItCa0L7RiNC60LAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90LDRjyIsInNlcnZpY2VzIjp7ItCS0YvRh9C10YEiOjU1fSwiYnJlZWRfZW4iOiJDYXQgbG9uZy1oYWlyZWQiLCJicmVlZF9ldCI6Ikthc3MgcGlra2FydmFsaW5lIn0seyJicmVlZCI6ItCa0L7RiNC60LAg0JzQtdC50L0t0LrRg9C9Iiwic2VydmljZXMiOnsi0JLRi9GH0ZHRgSI6NjB9LCJicmVlZF9lbiI6IkNhdCBNYWluZSBDb29uIiwiYnJlZWRfZXQiOiJLYXNzIE1haW5lIENvb24ifV07CnZhciBSQUlMV0FZID0gImh0dHBzOi8vcmpncm9vbWluZy51cC5yYWlsd2F5LmFwcC9ib29rIjsKdmFyIEdPT0dMRV9TQ1JJUFQgPSAiaHR0cHM6Ly9zY3JpcHQuZ29vZ2xlLmNvbS9tYWNyb3Mvcy9BS2Z5Y2J6Z0NJOFM3amZEYXRJMDZLTnRGaFROeUlQY1JHVzNJQUJRV0xkN25sVXdzNmpueEtTdzRZRVZFVjlOaUlabzRZeGI4QS9leGVjIjsKdmFyIEZBTExCQUNLX1RJTUVTID0gWycxMDowMCcsJzEwOjMwJywnMTE6MDAnLCcxMTozMCcsJzEyOjAwJywnMTI6MzAnLCcxMzowMCcsJzEzOjMwJywnMTQ6MDAnLCcxNDozMCcsJzE1OjAwJywnMTU6MzAnLCcxNjowMCcsJzE2OjMwJywnMTc6MDAnLCcxNzozMCcsJzE4OjAwJ107CnZhciBib29raW5nID0ge2JyZWVkOicnLGJyZWVkRGlzcGxheTonJyxzZXJ2aWNlOicnLHByaWNlOjAsbWFzdGVyOicnLGdyb29tSGlzdG9yeTonJyxkYXRlOicnLHRpbWU6JycsbGFuZzoncnUnfTsKdmFyIHNlbEJyZWVkID0gbnVsbDsKdmFyIGNZID0gbmV3IERhdGUoKS5nZXRGdWxsWWVhcigpOwp2YXIgY00gPSBuZXcgRGF0ZSgpLmdldE1vbnRoKCk7CnZhciBzdGVwID0gMTsKdmFyIE1PTlRIUyA9IFsn0K/QvdCy0LDRgNGMJywn0KTQtdCy0YDQsNC70YwnLCfQnNCw0YDRgicsJ9CQ0L/RgNC10LvRjCcsJ9Cc0LDQuScsJ9CY0Y7QvdGMJywn0JjRjtC70YwnLCfQkNCy0LPRg9GB0YInLCfQodC10L3RgtGP0LHRgNGMJywn0J7QutGC0Y/QsdGA0YwnLCfQndC+0Y/QsdGA0YwnLCfQlNC10LrQsNCx0YDRjCddOwoKZnVuY3Rpb24gc2hvd1NjcmVlbihpZCkgewogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5zY3JlZW4nKS5mb3JFYWNoKGZ1bmN0aW9uKHMpe3MuY2xhc3NMaXN0LnJlbW92ZSgnYWN0aXZlJyk7fSk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoaWQpLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpOwogIHdpbmRvdy5zY3JvbGxUbygwLDApOwp9CgpmdW5jdGlvbiBnb1N0ZXAobikgewogIFsnYmsxJywnYmsyJywnYmszJywnYms0JywnYms1J10uZm9yRWFjaChmdW5jdGlvbihpZCxpKXsKICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKGlkKS5jbGFzc05hbWUgPSAnc3RlcCcgKyAoaSsxPT09bj8nIHNob3cnOicnKTsKICB9KTsKICBmb3IodmFyIGk9MTtpPD01O2krKyl7CiAgICB2YXIgcHM9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3BzJytpKTsKICAgIHZhciBwbD1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgncGwnK2kpOwogICAgaWYoaTxuKXtwcy5jbGFzc05hbWU9J3BzIGRvbmUnO2lmKHBsKXBsLmNsYXNzTmFtZT0ncGwgZG9uZSc7fQogICAgZWxzZSBpZihpPT09bil7cHMuY2xhc3NOYW1lPSdwcyBhY3RpdmUnO2lmKHBsKXBsLmNsYXNzTmFtZT0ncGwnO30KICAgIGVsc2V7cHMuY2xhc3NOYW1lPSdwcyc7aWYocGwpcGwuY2xhc3NOYW1lPSdwbCc7fQogIH0KICBzdGVwPW47IHdpbmRvdy5zY3JvbGxUbygwLDApOwogIGlmKG49PT0yKSBmaWx0ZXJNYXN0ZXJzKCk7Cn0KCmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdib29rQnRuJykub25jbGljayA9IGZ1bmN0aW9uKCl7CiAgc2hvd1NjcmVlbignYm9va1NjcmVlbicpOyBnb1N0ZXAoMSk7IGJ1aWxkQ2FsKCk7Cn07CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdiYWNrQnRuJykub25jbGljayA9IGZ1bmN0aW9uKCl7CiAgaWYoc3RlcD4xKXtnb1N0ZXAoc3RlcC0xKTt9ZWxzZXtzaG93U2NyZWVuKCdob21lU2NyZWVuJyk7fQp9Owpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnaG9tZUJ0bicpLm9uY2xpY2sgPSBmdW5jdGlvbigpewogIHNob3dTY3JlZW4oJ2hvbWVTY3JlZW4nKTsgcmVzZXRBbGwoKTsKfTsKCi8vIEJyZWVkIHNlYXJjaAp2YXIgaW5wID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2JJbnB1dCcpOwp2YXIgZHJvcCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdiRHJvcCcpOwp2YXIgY2xyID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NsckJ0bicpOwp2YXIgYmFkZ2UgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc0JhZGdlJyk7CgppbnAuYWRkRXZlbnRMaXN0ZW5lcignaW5wdXQnLCBmdW5jdGlvbigpewogIHZhciBxID0gaW5wLnZhbHVlLnRyaW0oKTsKICBjbHIuY2xhc3NMaXN0LnRvZ2dsZSgnc2hvdycsIHEubGVuZ3RoPjApOwogIGlmKCFxKXtkcm9wLmNsYXNzTGlzdC5yZW1vdmUoJ29wZW4nKTtkcm9wLmlubmVySFRNTD0nJztyZXR1cm47fQogIHZhciBzZj1MQU5HPT09J2VuJz8nYnJlZWRfZW4nOkxBTkc9PT0nZXQnPydicmVlZF9ldCc6J2JyZWVkJzsKICB2YXIgcmVzPURBVEEuZmlsdGVyKGZ1bmN0aW9uKGIpe3JldHVybihiW3NmXXx8Yi5icmVlZCkudG9Mb3dlckNhc2UoKS5pbmRleE9mKHEudG9Mb3dlckNhc2UoKSkhPT0tMTt9KS5zbGljZSgwLDM1KTsKICBkcm9wLmlubmVySFRNTD0nJzsKICB2YXIgX25yPUxBTkc9PT0nZW4nPydCcmVlZCBub3QgZm91bmQnOkxBTkc9PT0nZXQnPydUw7V1Z3UgZWkgbGVpdHVkJzon0J/QvtGA0L7QtNCwINC90LUg0L3QsNC50LTQtdC90LAnOwogIHZhciBfbnQ9TEFORz09PSdlbic/IkNhbid0IGZpbmQgeW91ciBicmVlZD8iOkxBTkc9PT0nZXQnPydFaSBsZWlhIG9tYSB0w7V1Z3U/Jzon0J3QtSDQvdCw0YjQu9C4INGB0LLQvtGOINC/0L7RgNC+0LTRgz8nOwogIHZhciBfbnM9TEFORz09PSdlbic/J0NvbnRhY3QgdXMg4oCUIHdlIHdpbGwgaGVscCB5b3UgY2hvb3NlIGEgc2VydmljZSc6TEFORz09PSdldCc/J1bDtXRrZSBtZWllZ2Egw7xoZW5kdXN0IOKAlCBhaXRhbWUgdGVlbnVzZSB2YWxpZGEnOifQodCy0Y/QttC40YLQtdGB0Ywg0YEg0L3QsNC80Lgg0LvRjtCx0YvQvCDRg9C00L7QsdC90YvQvCDRgdC/0L7RgdC+0LHQvtC8IOKAlCDQvNGLINC/0L7QvNC+0LbQtdC8INC/0L7QtNC+0LHRgNCw0YLRjCDRg9GB0LvRg9Cz0YMnOwogIGlmKCFyZXMubGVuZ3RoKXtkcm9wLmlubmVySFRNTD0nPGRpdiBjbGFzcz0ibm9yZXMiPicrX25yKyc8L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXIiIG9uY2xpY2s9InNob3dTY3JlZW4oXCdob21lU2NyZWVuXCcpIj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItaWNvbiI+8J+QvjwvZGl2PjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci10ZXh0Ij48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItdGl0bGUiPicrX250Kyc8L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItc3ViIj4nK19ucysnPC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLWFycm93Ij7ihpI8L2Rpdj48L2Rpdj4nO30KICBlbHNlewogICAgcmVzLmZvckVhY2goZnVuY3Rpb24oYil7CiAgICAgIHZhciBkPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpOyBkLmNsYXNzTmFtZT0nZGl0ZW0nOwogICAgICB2YXIgYm5hbWU9YltzZl18fGIuYnJlZWQ7CiAgICAgIHZhciBpZHg9Ym5hbWUudG9Mb3dlckNhc2UoKS5pbmRleE9mKHEudG9Mb3dlckNhc2UoKSk7CiAgICAgIGQuaW5uZXJIVE1MPWJuYW1lLnN1YnN0cmluZygwLGlkeCkrJzxtYXJrPicrYm5hbWUuc3Vic3RyaW5nKGlkeCxpZHgrcS5sZW5ndGgpKyc8L21hcms+JytibmFtZS5zdWJzdHJpbmcoaWR4K3EubGVuZ3RoKTsKICAgICAgZC5vbmNsaWNrPWZ1bmN0aW9uKCl7c2VsZWN0QnJlZWQoYik7fTsKICAgICAgZHJvcC5hcHBlbmRDaGlsZChkKTsKICAgIH0pOwogIH0KICBkcm9wLmNsYXNzTGlzdC5hZGQoJ29wZW4nKTsKfSk7Cgpkb2N1bWVudC5hZGRFdmVudExpc3RlbmVyKCdjbGljaycsZnVuY3Rpb24oZSl7CiAgaWYoIWUudGFyZ2V0LmNsb3Nlc3QoJy5id3JhcCcpKWRyb3AuY2xhc3NMaXN0LnJlbW92ZSgnb3BlbicpOwp9KTsKY2xyLm9uY2xpY2sgPSByZXNldEJyZWVkOwoKZnVuY3Rpb24gc2VsZWN0QnJlZWQoYil7CiAgc2VsQnJlZWQ9YjsgYm9va2luZy5icmVlZD1iLmJyZWVkOwogIGlucC52YWx1ZT0nJzsgY2xyLmNsYXNzTGlzdC5yZW1vdmUoJ3Nob3cnKTsKICBkcm9wLmNsYXNzTGlzdC5yZW1vdmUoJ29wZW4nKTsgZHJvcC5pbm5lckhUTUw9Jyc7CiAgYmFkZ2UuaW5uZXJIVE1MPScnOwogIHZhciBiRmllbGQ9TEFORz09PSdlbic/J2JyZWVkX2VuJzpMQU5HPT09J2V0Jz8nYnJlZWRfZXQnOidicmVlZCc7CiAgdmFyIGRpc3BCcmVlZD1iW2JGaWVsZF18fGIuYnJlZWQ7CiAgYm9va2luZy5icmVlZERpc3BsYXk9ZGlzcEJyZWVkOwogIHZhciBibj1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdzcGFuJyk7Ym4uY2xhc3NOYW1lPSdibmFtZSc7Ym4udGV4dENvbnRlbnQ9ZGlzcEJyZWVkOwogIHZhciBjaGdUeHQ9TEFORz09PSdlbic/J0NoYW5nZSc6TEFORz09PSdldCc/J011dWRhJzon0JjQt9C80LXQvdC40YLRjCc7CiAgdmFyIGJjPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NwYW4nKTtiYy5jbGFzc05hbWU9J2JjaGcnO2JjLnRleHRDb250ZW50PWNoZ1R4dDsKICBiYy5vbmNsaWNrPXJlc2V0QnJlZWQ7CiAgYmFkZ2UuYXBwZW5kQ2hpbGQoYm4pO2JhZGdlLmFwcGVuZENoaWxkKGJjKTsKICBiYWRnZS5jbGFzc0xpc3QuYWRkKCdzaG93Jyk7CiAgcmVuZGVyU3ZjcyhiKTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjU2VjJykuc3R5bGUuZGlzcGxheT0nYmxvY2snOwogICAgLy8gQWRkIGltcG9ydGFudCBub3RlIGlmIG5vdCBleGlzdHMKICAgIGlmKCFkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjTm90ZScpKXsKICAgICAgdmFyIG5vdGU9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7CiAgICAgIG5vdGUuaWQ9J3N2Y05vdGUnOwogICAgICBub3RlLnN0eWxlLmNzc1RleHQ9J2JvcmRlcjoxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4xNSk7cGFkZGluZzoxNHB4IDE2cHg7YmFja2dyb3VuZDpyZ2JhKDIwMSwxNjgsNzYsLjA0KTttYXJnaW4tdG9wOjEycHg7JzsKICAgICAgdmFyIG5vdGVUaXRsZT1MQU5HPT09J2VuJz8nUGxlYXNlIG5vdGUnOkxBTkc9PT0nZXQnPydQYW5nZSB0w6RoZWxlJzon0JLQsNC20L3QviDQt9C90LDRgtGMJzsKICAgICAgdmFyIG5vdGVCb2R5PUxBTkc9PT0nZW4nPydGaW5hbCBwcmljZSBkZXBlbmRzIG9uIGNvYXQgY29uZGl0aW9uIGFuZCBwZXQgYmVoYXZpb3VyLjxicj5EZW1hdHRpbmcgZnJvbSA1IOKCrC48YnI+QWdncmVzc2l2ZSBiZWhhdmlvdXIgc3VyY2hhcmdlIG1heSBhcHBseTogKzUwJS4nOkxBTkc9PT0nZXQnPydMw7VwbGlrIGhpbmQgc8O1bHR1YiBrYXJ2YXN0aWt1IHNlaXN1bmRpc3QgamEgbGVtbWlrbG9vbWEga8OkaXR1bWlzZXN0Ljxicj5Lb2x0c3VuaXRlIGxhaHRpaGFydXRhbWluZSBhbGF0ZXMgNSDigqwuPGJyPkFncmVzc2lpdnNlIGvDpGl0dW1pc2Uga29ycmFsIHbDtWliIGxpc2FuZHVkYSA1MCUganV1cmRlaGluZGx1cy4nOifQntC60L7QvdGH0LDRgtC10LvRjNC90LDRjyDRgdGC0L7QuNC80L7RgdGC0Ywg0LfQsNCy0LjRgdC40YIg0L7RgiDRgdC+0YHRgtC+0Y/QvdC40Y8g0YjQtdGA0YHRgtC4INC4INC/0L7QstC10LTQtdC90LjRjyDQv9C40YLQvtC80YbQsC48YnI+0KDQsNC30LHQvtGAINC60L7Qu9GC0YPQvdC+0LIg4oCUINC+0YIgNSDigqwuPGJyPtCf0YDQuCDQsNCz0YDQtdGB0YHQuNCy0L3QvtC8INC/0L7QstC10LTQtdC90LjQuCDQvNC+0LbQtdGCINC/0YDQuNC80LXQvdGP0YLRjNGB0Y8g0LTQvtC/0LvQsNGC0LAgNTAlLic7CiAgICAgIG5vdGUuaW5uZXJIVE1MPSc8ZGl2IHN0eWxlPSJmb250LXNpemU6LjU4cmVtO2xldHRlci1zcGFjaW5nOi4xNWVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojOGE4YTU1O21hcmdpbi1ib3R0b206OHB4O2ZvbnQtd2VpZ2h0OjYwMDsiPicrbm90ZVRpdGxlKyc8L2Rpdj48ZGl2IHN0eWxlPSJmb250LXNpemU6LjcxcmVtO2NvbG9yOiM3Nzc3NzA7bGluZS1oZWlnaHQ6MS44OyI+Jytub3RlQm9keSsnPC9kaXY+JzsKICAgICAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y1NlYycpLmFwcGVuZENoaWxkKG5vdGUpOwogICAgfQogIGZpbHRlck1hc3RlcnMoKTsKfQoKZnVuY3Rpb24gcmVzZXRCcmVlZCgpewogIHNlbEJyZWVkPW51bGw7Ym9va2luZy5icmVlZD0nJztib29raW5nLnNlcnZpY2U9Jyc7Ym9va2luZy5wcmljZT0wOwogIGlucC52YWx1ZT0nJztjbHIuY2xhc3NMaXN0LnJlbW92ZSgnc2hvdycpOwogIGJhZGdlLmNsYXNzTGlzdC5yZW1vdmUoJ3Nob3cnKTtiYWRnZS5pbm5lckhUTUw9Jyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y1NlYycpLnN0eWxlLmRpc3BsYXk9J25vbmUnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdmNMaXN0JykuaW5uZXJIVE1MPScnOwp9CgoKdmFyIFNWQ19UUkFOU0xBVElPTlMgPSB7CiAgJ9CR0LDQt9C+0LLRi9C5INGD0YXQvtC0JzogICAgICB7ZW46J0Jhc2ljIGdyb29tJywgICAgICBldDonUMO1aGlob29sZHVzJ30sCiAgJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Jzp7ZW46J0h5Z2llbmUgZ3Jvb20nLCAgICBldDonSMO8Z2llZW5paG9vbGR1cyd9LAogICfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0JzogIHtlbjonRnVsbCBncm9vbScsICAgICAgICBldDonVMOkaWVsaWsgaG9vbGR1cyd9LAogICfQotGA0LjQvNC80LjQvdCzJzogICAgICAgICAge2VuOidUcmltbWluZycsICAgICAgICAgIGV0OidUcmltbWVyaW1pbmUnfSwKICAn0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAnOiAgIHtlbjonRXhwcmVzcyBzaGVkJywgICAgICBldDonS2lpcmthcnZhdmFoZXR1cyd9LAogICfQktGL0YfQtdGBJzogICAgICAgICAgICAge2VuOidCcnVzaC1vdXQnLCAgICAgICAgIGV0OidIYXJqYW1pbmUnfQp9Owp2YXIgU1ZDX1RBR0xJTkVfSTE4Tj17CiAgcnU6eyfQktGL0YfQtdGBJzon0KHRgtC+0LjQvNC+0YHRgtGMINC30LDQstC40YHQuNGCINC+0YIg0YHQvtGB0YLQvtGP0L3QuNGPINGI0LXRgNGB0YLQuCDQuCDQvtCx0YrRkdC80LAg0YDQsNCx0L7RgicsJ9CR0LDQt9C+0LLRi9C5INGD0YXQvtC0Jzon0J/QvtC00YXQvtC00LjRgiDQtNC70Y8g0L/QvtC00LTQtdGA0LbQsNC90LjRjyDRh9C40YHRgtC+0YLRiyDQvNC10LbQtNGDINC/0YDQvtGG0LXQtNGD0YDQsNC80LgnLCfQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCc6J9CU0LvRjyDQutC+0LzRhNC+0YDRgtCwINC4INCw0LrQutGD0YDQsNGC0L3QvtGB0YLQuCDQv9C40YLQvtC80YbQsCcsJ9Ca0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQnOifQn9C+0LvQvdGL0Lkg0YPRhdC+0LQg0YHQviDRgdGC0YDQuNC20LrQvtC5Jywn0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAnOifQn9C+0LzQvtCz0LDQtdGCINGD0LzQtdC90YzRiNC40YLRjCDQutC+0LvQuNGH0LXRgdGC0LLQviDQu9C40L3Rj9GO0YnQtdC5INGI0LXRgNGB0YLQuCcsJ9Ci0YDQuNC80LzQuNC90LMnOifQlNC70Y8g0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90YvRhSDQv9C+0YDQvtC0J30sCiAgZW46eyfQktGL0YfQtdGBJzonUHJpY2UgZGVwZW5kcyBvbiBjb2F0IGNvbmRpdGlvbiBhbmQgdm9sdW1lIG9mIHdvcmsnLCfQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCc6J0lkZWFsIGZvciBtYWludGFpbmluZyBjbGVhbmxpbmVzcyBiZXR3ZWVuIGZ1bGwgZ3Jvb21zJywn0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQnOidGb3IgeW91ciBwZXRcJ3MgY29tZm9ydCBhbmQgbmVhdG5lc3MnLCfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0JzonRnVsbCBncm9vbWluZyB3aXRoIGhhaXJjdXQnLCfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6J1NpZ25pZmljYW50bHkgcmVkdWNlcyBzaGVkZGluZycsJ9Ci0YDQuNC80LzQuNC90LMnOidGb3Igd2lyZS1oYWlyZWQgYnJlZWRzJ30sCiAgZXQ6eyfQktGL0YfQtdGBJzonSGluZCBzw7VsdHViIGthcnZhc3Rpa3Ugc2Vpc3VuZGlzdCBqYSB0w7bDtm1haHVzdCcsJ9CR0LDQt9C+0LLRi9C5INGD0YXQvtC0JzonU29iaWIgcHVodHVzZSBob2lkbWlzZWtzIHByb3RzZWR1dXJpZGUgdmFoZWwnLCfQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCc6J0xlbW1pa2xvb21hIG11Z2F2dXNla3MgamEga29ycmFzaG9pdWtzJywn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc6J1TDpGllbGlrIGhvb2xkdXMga29vcyBsw7Vpa3VzZWdhJywn0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAnOidWw6RoZW5kYWIgb2x1bGlzZWx0IGthcnZhZGUgbGFuZ2VtaXN0Jywn0KLRgNC40LzQvNC40L3Qsyc6J1RyYWF0a2FydmFsaXN0ZWxlIHTDtXVndWRlbGUnfQp9Owp2YXIgU1ZDX0RFU0NfSTE4Tj17CiAgcnU6eyfQktGL0YfQtdGBJzon0KfQuNGB0YLQutCwINCz0LvQsNC3LCDRg9GI0LXQuSwg0L/QvtC00YHRgtGA0LjQs9Cw0L3QuNC1INC60L7Qs9GC0LXQuSwg0LLRi9GH0ZHRgSAo0LTQu9GPINC60L7RiNC10LopJywn0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQnOifQnNGL0YLRjNGRINC/0YDQvtGE0LXRgdGB0LjQvtC90LDQu9GM0L3Ri9C80Lgg0YHRgNC10LTRgdGC0LLQsNC80LgsINC00LXQu9C40LrQsNGC0L3QsNGPINGB0YPRiNC60LAnLCfQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCc6J9Ch0YLRgNC40LbQutCwINC60L7Qs9GC0LXQuSwg0YfQuNGB0YLQutCwINGD0YjQtdC5INC4INCz0LvQsNC3LCDQutGD0L/QsNC90LjQtSwg0YHRg9GI0LrQsCwg0YPRhdC+0LQg0LfQsCDQu9Cw0L/QutCw0LzQuCDQuCDRh9GD0LLRgdGC0LLQuNGC0LXQu9GM0L3Ri9C80Lgg0LfQvtC90LDQvNC4Jywn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc6J9Ch0YLRgNC40LbQutCwINC60L7Qs9GC0LXQuSwg0YfQuNGB0YLQutCwINGD0YjQtdC5INC4INCz0LvQsNC3LCDQutGD0L/QsNC90LjQtSwg0YHRg9GI0LrQsCwg0YPRhdC+0LQg0LfQsCDQu9Cw0L/QutCw0LzQuCDQuCDRh9GD0LLRgdGC0LLQuNGC0LXQu9GM0L3Ri9C80Lgg0LfQvtC90LDQvNC4LCDQvNC+0LTQtdC70YzQvdCw0Y8g0YHRgtGA0LjQttC60LAnLCfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6J9Cc0YvRgtGM0ZEsINGB0YPRiNC60LAsINGD0YXQvtC0INC30LAg0YjQtdGA0YHRgtGM0Y4sINC80LDRgdC60LAsINC/0L7QtNGB0YLRgNC40LPQsNC90LjQtSDQutC+0LPRgtC10LksINGH0LjRgdGC0LrQsCDRg9GI0LXQuSDQuCDQs9C70LDQtywg0YPRhdC+0LQg0LfQsCDQu9Cw0L/QsNC80Lgg0Lgg0LfQvtC90LDQvNC4INGC0YDQtdCx0YPRjtGJ0LjQvNC4INC+0YHQvtCx0L7Qs9C+INCy0L3QuNC80LDQvdC40Y8nLCfQotGA0LjQvNC80LjQvdCzJzon0JLRi9GJ0LjQv9GL0LLQsNC90LjQtSDRgdGC0LDRgNC+0LPQviDRgdC70L7RjyDRiNC10YDRgdGC0LgsINC80YvRgtGM0ZEsINGB0YPRiNC60LAsINGB0YLRgNC40LbQutCwINC60L7Qs9GC0LXQuSwg0YfQuNGB0YLQutCwINGD0YjQtdC5INC4INCz0LvQsNC3LCDQvtGE0L7RgNC80LvQtdC90LjQtSDRiNC10YDRgdGC0LgnfSwKICBlbjp7J9CS0YvRh9C10YEnOidFeWUgYW5kIGVhciBjbGVhbmluZywgbmFpbCB0cmltbWluZywgYnJ1c2hpbmcgKGZvciBjYXRzKScsJ9CR0LDQt9C+0LLRi9C5INGD0YXQvtC0JzonV2FzaGluZyB3aXRoIHByb2Zlc3Npb25hbCBwcm9kdWN0cywgZ2VudGxlIGRyeWluZycsJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0JzonTmFpbCB0cmltbWluZywgZWFyIGFuZCBleWUgY2xlYW5pbmcsIGJhdGhpbmcsIGRyeWluZywgcGF3IGFuZCBzZW5zaXRpdmUgYXJlYSBjYXJlJywn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc6J05haWwgdHJpbW1pbmcsIGVhciBhbmQgZXllIGNsZWFuaW5nLCBiYXRoaW5nLCBkcnlpbmcsIHBhdyBhbmQgc2Vuc2l0aXZlIGFyZWEgY2FyZSwgc3R5bGluZyBoYWlyY3V0Jywn0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAnOidXYXNoaW5nLCBkcnlpbmcsIGNvYXQgY2FyZSwgbWFzaywgbmFpbCB0cmltbWluZywgZWFyIGFuZCBleWUgY2xlYW5pbmcsIHBhdyBhbmQgc3BlY2lhbCBhcmVhIGNhcmUnLCfQotGA0LjQvNC80LjQvdCzJzonUmVtb3Zpbmcgb2xkIGNvYXQgbGF5ZXIsIHdhc2hpbmcsIGRyeWluZywgbmFpbCB0cmltbWluZywgZWFyIGFuZCBleWUgY2xlYW5pbmcsIGNvYXQgc3R5bGluZyd9LAogIGV0Onsn0JLRi9GH0LXRgSc6J1NpbG1hZGUgamEga8O1cnZhZGUgcHVoYXN0YW1pbmUsIGvDvMO8bnRlIGzDtWlrYW1pbmUsIGhhcmphbWluZSAoa2Fzc2lkZWxlKScsJ9CR0LDQt9C+0LLRi9C5INGD0YXQvtC0JzonUGVzZW1pbmUgcHJvZmVzc2lvbmFhbHNldGUgdmFoZW5kaXRlZ2EsIMO1cm4ga3VpdmF0YW1pbmUnLCfQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCc6J0vDvMO8bnRlIGzDtWlrYW1pbmUsIGvDtXJ2YWRlIGphIHNpbG1hZGUgcHVoYXN0YW1pbmUsIHBlc2VtaW5lLCBrdWl2YXRhbWluZSwga8OkcHBhZGUgamEgdHVuZGxpa2UgcGlpcmtvbmRhZGUgaG9vbGR1cycsJ9Ca0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQnOidLw7zDvG50ZSBsw7Vpa2FtaW5lLCBrw7VydmFkZSBqYSBzaWxtYWRlIHB1aGFzdGFtaW5lLCBwZXNlbWluZSwga3VpdmF0YW1pbmUsIGvDpHBwYWRlIGphIHR1bmRsaWtlIHBpaXJrb25kYWRlIGhvb2xkdXMsIG1vZGVsbMO1aWt1cycsJ9Ct0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwJzonUGVzZW1pbmUsIGt1aXZhdGFtaW5lLCBrYXJ2YXN0aWt1IGhvb2xkdXMsIG1hc2ssIGvDvMO8bnRlIGzDtWlrYW1pbmUsIGvDtXJ2YWRlIGphIHNpbG1hZGUgcHVoYXN0YW1pbmUsIGvDpHBwYWRlIGphIGVyaWxpc3RlIHBpaXJrb25kYWRlIGhvb2xkdXMnLCfQotGA0LjQvNC80LjQvdCzJzonVmFuYSBrYXJ2YWtpaGkgZWVtYWxkYW1pbmUsIHBlc2VtaW5lLCBrdWl2YXRhbWluZSwga8O8w7xudGUgbMO1aWthbWluZSwga8O1cnZhZGUgamEgc2lsbWFkZSBwdWhhc3RhbWluZSwga2FydmFzdGlrdSBrdWp1bmRhbWluZSd9Cn07CmZ1bmN0aW9uIGdldFN2Y1RhZyhuYW1lKXtyZXR1cm4oU1ZDX1RBR0xJTkVfSTE4TltMQU5HXSYmU1ZDX1RBR0xJTkVfSTE4TltMQU5HXVtuYW1lXSl8fFNWQ19UQUdMSU5FX0kxOE4ucnVbbmFtZV18fCcnO30KZnVuY3Rpb24gZ2V0U3ZjRGVzYyhuYW1lKXtyZXR1cm4oU1ZDX0RFU0NfSTE4TltMQU5HXSYmU1ZDX0RFU0NfSTE4TltMQU5HXVtuYW1lXSl8fFNWQ19ERVNDX0kxOE4ucnVbbmFtZV18fCcnO30KCmZ1bmN0aW9uIHJlbmRlclN2Y3MoYil7CiAgdmFyIGxpc3Q9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y0xpc3QnKTtsaXN0LmlubmVySFRNTD0nJzsKICBPYmplY3QuZW50cmllcyhiLnNlcnZpY2VzKS5mb3JFYWNoKGZ1bmN0aW9uKGt2KXsKICAgIHZhciBuYW1lPWt2WzBdLHByaWNlPWt2WzFdOwogICAgdmFyIGRpc3BsYXlOYW1lPShMQU5HIT09J3J1JyYmU1ZDX1RSQU5TTEFUSU9OU1tuYW1lXSk/U1ZDX1RSQU5TTEFUSU9OU1tuYW1lXVtMQU5HXTpuYW1lOwogICAgdmFyIGJ0bj1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdidXR0b24nKTtidG4uY2xhc3NOYW1lPSdzdmJ0bic7CiAgICB2YXIgcm93PWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO3Jvdy5jbGFzc05hbWU9J3N2YnRuLXJvdyc7CiAgICB2YXIgbnM9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnc3BhbicpO25zLmNsYXNzTmFtZT0nc3ZidG4tbmFtZSc7bnMudGV4dENvbnRlbnQ9ZGlzcGxheU5hbWU7CiAgICB2YXIgcHM9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnc3BhbicpO3BzLmNsYXNzTmFtZT0nc3ZidG4tcHJpY2UnO3BzLnRleHRDb250ZW50PXByaWNlKycg4oKsJzsKICAgIHJvdy5hcHBlbmRDaGlsZChucyk7cm93LmFwcGVuZENoaWxkKHBzKTsKICAgIGJ0bi5hcHBlbmRDaGlsZChyb3cpOwogICAgdmFyIGRlc2M9Z2V0U3ZjRGVzYyhuYW1lKTsKICAgIGlmKGRlc2Mpe3ZhciBkcz1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdzcGFuJyk7ZHMuY2xhc3NOYW1lPSdzdmJ0bi1kZXNjJztkcy50ZXh0Q29udGVudD1kZXNjO2J0bi5hcHBlbmRDaGlsZChkcyk7fQogICAgdmFyIHRhZz1nZXRTdmNUYWcobmFtZSk7CiAgICBpZih0YWcpe3ZhciB0cz1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdzcGFuJyk7dHMuY2xhc3NOYW1lPSdzdmJ0bi10YWcnO3RzLnRleHRDb250ZW50PXRhZztidG4uYXBwZW5kQ2hpbGQodHMpO30KICAgIGJ0bi5vbmNsaWNrPWZ1bmN0aW9uKCl7CiAgICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5zdmJ0bicpLmZvckVhY2goZnVuY3Rpb24oYil7Yi5jbGFzc0xpc3QucmVtb3ZlKCdhY3RpdmUnKTt9KTsKICAgICAgYnRuLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpOwogICAgICBib29raW5nLnNlcnZpY2U9bmFtZTtib29raW5nLnByaWNlPXByaWNlOwogICAgICBzZXRUaW1lb3V0KGZ1bmN0aW9uKCl7Z29TdGVwKDIpO30sMzAwKTsKICAgIH07CiAgICBsaXN0LmFwcGVuZENoaWxkKGJ0bik7CiAgfSk7Cn0KCi8vIE1hc3RlcnMKZnVuY3Rpb24gZmlsdGVyTWFzdGVycygpewogIHZhciBpc0NhdCA9IGJvb2tpbmcuYnJlZWQgJiYgYm9va2luZy5icmVlZC5pbmRleE9mKCfQmtC+0YjQutCwJykgPT09IDA7CiAgdmFyIGJyZWVkID0gYm9va2luZy5icmVlZCB8fCAnJzsKICB2YXIgYW5uYUJyZWVkcyA9IFsn0JTQsNC70LzQsNGC0LjQvScsJ9CR0LjQs9C70YwnLCfQpdCw0YHQutC4Jywn0JHQtdGA0L3RgdC60LjQuSDQt9C10L3QvdC10L3RhdGD0L3QtCcsJ9Ck0LjQvdGB0LrQuNC5INC70LDQv9GF0YPQvdC0Jywn0JrQvtGI0LrQsCDQutC+0YDQvtGC0LrQvtGI0LXRgNGB0YLQvdCw0Y8nLCfQpNGA0LDQvdGG0YPQt9GB0LrQuNC5INCx0YPQu9GM0LTQvtCzJywn0KLQsNC60YHQsCDQtNC70LjQvdC90L7RiNC10YDRgdGC0L3QsNGPJywn0KLQsNC60YHQsCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3QsNGPJywn0JTQttC10Lot0YDQsNGB0YHQtdC7LdGC0LXRgNGM0LXRgCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3Ri9C5Jywn0KHQsNC80L7QtdC0Jywn0KjQtdC70YLQuCcsJ9Ca0L7RgNCz0LgnLCfQktC10LvRjNGIJywn0JvQsNCx0YDQsNC00L7RgCcsJ9Cn0LjRhdGD0LDRhdGD0LAnXTsKICB2YXIgaXNBbm5hQnJlZWQgPSBhbm5hQnJlZWRzLnNvbWUoZnVuY3Rpb24oYil7IHJldHVybiBicmVlZC5pbmRleE9mKGIpICE9PSAtMTsgfSk7CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLm1idG4nKS5mb3JFYWNoKGZ1bmN0aW9uKGJ0bil7CiAgICB2YXIgbWFzdGVyID0gYnRuLmdldEF0dHJpYnV0ZSgnZGF0YS1tYXN0ZXInKTsKICAgIGlmKG1hc3RlciA9PT0gJ9CQ0LvQuNGB0LAnKXsKICAgICAgYnRuLnN0eWxlLmRpc3BsYXkgPSBpc0NhdCA/ICcnIDogJ25vbmUnOwogICAgfSBlbHNlIGlmKG1hc3RlciA9PT0gJ9CQ0L3QvdCwJyl7CiAgICAgIGJ0bi5zdHlsZS5kaXNwbGF5ID0gaXNBbm5hQnJlZWQgPyAnJyA6ICdub25lJzsKICAgIH0KICB9KTsKfQoKZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLm1idG4nKS5mb3JFYWNoKGZ1bmN0aW9uKGJ0bil7CiAgYnRuLm9uY2xpY2s9ZnVuY3Rpb24oKXsKICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5tYnRuJykuZm9yRWFjaChmdW5jdGlvbihiKXtiLmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpO30pOwogICAgYnRuLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpOwogICAgYm9va2luZy5tYXN0ZXI9YnRuLmdldEF0dHJpYnV0ZSgnZGF0YS1tYXN0ZXInKTsKICAgIHNldFRpbWVvdXQoZnVuY3Rpb24oKXtnb1N0ZXAoMyk7fSwzMDApOwogIH07Cn0pOwoKLy8gR3Jvb20gaGlzdG9yeQpkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuZ2J0bicpLmZvckVhY2goZnVuY3Rpb24oYnRuKXsKICBidG4ub25jbGljaz1mdW5jdGlvbigpewogICAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmdidG4nKS5mb3JFYWNoKGZ1bmN0aW9uKGIpe2IuY2xhc3NMaXN0LnJlbW92ZSgnYWN0aXZlJyk7fSk7CiAgICBidG4uY2xhc3NMaXN0LmFkZCgnYWN0aXZlJyk7CiAgICBib29raW5nLmdyb29tSGlzdG9yeT1idG4uZ2V0QXR0cmlidXRlKCdkYXRhLXZhbCcpOwogICAgc2V0VGltZW91dChmdW5jdGlvbigpe2dvU3RlcCg0KTtidWlsZENhbCgpO30sMzAwKTsKICB9Owp9KTsKCi8vIENhbGVuZGFyCmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdwcmV2TScpLm9uY2xpY2s9ZnVuY3Rpb24oKXtjTS0tO2lmKGNNPDApe2NNPTExO2NZLS07fWJ1aWxkQ2FsKCk7fTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ25leHRNJykub25jbGljaz1mdW5jdGlvbigpe2NNKys7aWYoY00+MTEpe2NNPTA7Y1krKzt9YnVpbGRDYWwoKTt9OwoKdmFyIGF2YWlsYWJsZURheXMgPSBbXTsKCmZ1bmN0aW9uIGxvYWRBdmFpbGFibGVEYXlzKCkgewogIHZhciBtYXN0ZXIgPSBib29raW5nLm1hc3RlcjsKICBpZiAoIW1hc3RlcikgcmV0dXJuOwogIGF2YWlsYWJsZURheXMgPSBbXTsKICBmZXRjaCh3aW5kb3cubG9jYXRpb24ub3JpZ2luICsgJy9hcGkvYXZhaWxhYmxlX2RheXM/bW9udGg9JyArIChjTSsxKSArICcmeWVhcj0nICsgY1kgKyAnJm1hc3Rlcj0nICsgZW5jb2RlVVJJQ29tcG9uZW50KG1hc3RlcikpCiAgICAudGhlbihmdW5jdGlvbihyKXsgcmV0dXJuIHIuanNvbigpOyB9KQogICAgLnRoZW4oZnVuY3Rpb24oZGF0YSl7CiAgICAgIGF2YWlsYWJsZURheXMgPSBkYXRhLmF2YWlsYWJsZSB8fCBbXTsKICAgICAgbWFya0F2YWlsYWJsZURheXMoKTsKICAgIH0pCiAgICAuY2F0Y2goZnVuY3Rpb24oKXsgYXZhaWxhYmxlRGF5cyA9IFtdOyB9KTsKfQoKZnVuY3Rpb24gbWFya0F2YWlsYWJsZURheXMoKSB7CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmNkJykuZm9yRWFjaChmdW5jdGlvbihjKXtpZighYy5jbGFzc0xpc3QuY29udGFpbnMoJ2RpcycpKWMuY2xhc3NMaXN0LnJlbW92ZSgnc2VsJyk7fSk7CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmNkOm5vdCguZGlzKTpub3QoLmNkbik6bm90KC5wYWQpJykuZm9yRWFjaChmdW5jdGlvbihlbCkgewogICAgdmFyIGRheSA9IGVsLnRleHRDb250ZW50LnRyaW0oKTsKICAgIGlmICghZGF5IHx8IGlzTmFOKHBhcnNlSW50KGRheSkpKSByZXR1cm47CiAgICB2YXIgZGF0ZVN0ciA9IFN0cmluZyhwYXJzZUludChkYXkpKS5wYWRTdGFydCgyLCcwJykgKyAnLicgKyBTdHJpbmcoY00rMSkucGFkU3RhcnQoMiwnMCcpICsgJy4nICsgY1k7CiAgICBpZiAoYXZhaWxhYmxlRGF5cy5pbmRleE9mKGRhdGVTdHIpICE9PSAtMSkgewogICAgICBlbC5jbGFzc0xpc3QuYWRkKCdhdmFpbCcpOwogICAgICBlbC5jbGFzc0xpc3QucmVtb3ZlKCdidXN5Jyk7CiAgICB9IGVsc2UgewogICAgICBlbC5jbGFzc0xpc3QuYWRkKCdidXN5Jyk7CiAgICAgIGVsLmNsYXNzTGlzdC5yZW1vdmUoJ2F2YWlsJyk7CiAgICB9CiAgfSk7Cn0KCmZ1bmN0aW9uIGJ1aWxkQ2FsKCl7CiAgbG9hZEF2YWlsYWJsZURheXMoKTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2FsTScpLnRleHRDb250ZW50PU1PTlRIU1tjTV0rJyAnK2NZOwogIGJvb2tpbmcuZGF0ZT0nJzsgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmNkJykuZm9yRWFjaChmdW5jdGlvbihjKXtjLmNsYXNzTGlzdC5yZW1vdmUoJ3NlbCcpO2MuY2xhc3NMaXN0LnJlbW92ZSgnYXZhaWwnKTtjLmNsYXNzTGlzdC5yZW1vdmUoJ2J1c3knKTt9KTsKICB2YXIgZz1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2FsRycpO2cuaW5uZXJIVE1MPScnOwogIFsn0J/QvScsJ9CS0YInLCfQodGAJywn0KfRgicsJ9Cf0YInLCfQodCxJywn0JLRgSddLmZvckVhY2goZnVuY3Rpb24oZCl7CiAgICB2YXIgZWw9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7ZWwuY2xhc3NOYW1lPSdjZG4nO2VsLnRleHRDb250ZW50PWQ7Zy5hcHBlbmRDaGlsZChlbCk7CiAgfSk7CiAgdmFyIGZpcnN0PW5ldyBEYXRlKGNZLGNNLDEpLmdldERheSgpOwogIHZhciBkYXlzPW5ldyBEYXRlKGNZLGNNKzEsMCkuZ2V0RGF0ZSgpOwogIHZhciBzdGFydD1maXJzdD09PTA/NjpmaXJzdC0xOwogIHZhciB0b2RheT1uZXcgRGF0ZSgpOwogIGZvcih2YXIgaT0wO2k8c3RhcnQ7aSsrKXt2YXIgZWw9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7ZWwuY2xhc3NOYW1lPSdjZCBwYWQnO2cuYXBwZW5kQ2hpbGQoZWwpO30KICBmb3IodmFyIGRheT0xO2RheTw9ZGF5cztkYXkrKyl7CiAgICB2YXIgZWw9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7ZWwuY2xhc3NOYW1lPSdjZCc7CiAgICB2YXIgZGF0ZT1uZXcgRGF0ZShjWSxjTSxkYXkpOwogICAgdmFyIGlzUGFzdD1kYXRlPG5ldyBEYXRlKHRvZGF5LmdldEZ1bGxZZWFyKCksdG9kYXkuZ2V0TW9udGgoKSx0b2RheS5nZXREYXRlKCkpOwogICAgdmFyIGlubmVyPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO2lubmVyLmNsYXNzTmFtZT0nY2QtaW5uZXInO2lubmVyLnRleHRDb250ZW50PWRheTtlbC5hcHBlbmRDaGlsZChpbm5lcik7CiAgICBpZihpc1Bhc3Qpe2VsLmNsYXNzTGlzdC5hZGQoJ2RpcycpO30KICAgIGVsc2V7CiAgICAgIGlmKGRhdGUudG9EYXRlU3RyaW5nKCk9PT10b2RheS50b0RhdGVTdHJpbmcoKSllbC5jbGFzc0xpc3QuYWRkKCd0b2QnKTsKICAgICAgKGZ1bmN0aW9uKGQsIGVsUmVmKXsKICAgICAgICBlbFJlZi5vbmNsaWNrPWZ1bmN0aW9uKCl7CiAgICAgICAgICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuY2QnKS5mb3JFYWNoKGZ1bmN0aW9uKGMpe2MuY2xhc3NMaXN0LnJlbW92ZSgnc2VsJyk7fSk7CiAgICAgICAgICBlbFJlZi5jbGFzc0xpc3QuYWRkKCdzZWwnKTsKICAgICAgICAgIGJvb2tpbmcuZGF0ZT1TdHJpbmcoZCkucGFkU3RhcnQoMiwnMCcpKycuJytTdHJpbmcoY00rMSkucGFkU3RhcnQoMiwnMCcpKycuJytjWTsKICAgICAgICAgIHNob3dUaW1lcygpOwogICAgICAgIH07CiAgICAgIH0pKGRheSwgZWwpOwogICAgfQogICAgZy5hcHBlbmRDaGlsZChlbCk7CiAgfQogIC8vIGZpbGwgdHJhaWxpbmcgY2VsbHMgdG8gY29tcGxldGUgbGFzdCBncmlkIHJvdwogIHZhciB0b3RhbCA9IHN0YXJ0ICsgZGF5czsKICB2YXIgdHJhaWwgPSAoNyAtICh0b3RhbCAlIDcpKSAlIDc7CiAgZm9yKHZhciB0PTA7dDx0cmFpbDt0Kyspe3ZhciBlcD1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdkaXYnKTtlcC5jbGFzc05hbWU9J2NkIHBhZCc7Zy5hcHBlbmRDaGlsZChlcCk7fQp9CgpmdW5jdGlvbiBzaG93VGltZXMoKXsKICB2YXIgdGc9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3RpbWVHJyk7CiAgdGcuaW5uZXJIVE1MPSc8ZGl2IGNsYXNzPSJsb2FkaW5nLXNsb3RzIj7ij7Mg0JfQsNCz0YDRg9C20LDQtdC8INGA0LDRgdC/0LjRgdCw0L3QuNC1Li4uPC9kaXY+JzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgndGltZVNlYycpLnN0eWxlLmRpc3BsYXk9J2Jsb2NrJzsKCiAgdmFyIHVybCA9IHdpbmRvdy5sb2NhdGlvbi5vcmlnaW4gKyAiL2FwaS9zbG90cyIgKyAnP2FjdGlvbj1zbG90cyZkYXRlPScgKyBlbmNvZGVVUklDb21wb25lbnQoYm9va2luZy5kYXRlKSArICcmbWFzdGVyPScgKyBlbmNvZGVVUklDb21wb25lbnQoYm9va2luZy5tYXN0ZXIpOwoKICBmZXRjaCh1cmwpCiAgICAudGhlbihmdW5jdGlvbihyKXtyZXR1cm4gci5qc29uKCk7fSkKICAgIC50aGVuKGZ1bmN0aW9uKGRhdGEpewogICAgICB2YXIgc2xvdHMgPSAoZGF0YS5zbG90cyAmJiBkYXRhLnNsb3RzLmxlbmd0aCA+IDApID8gZGF0YS5zbG90cyA6IFtdOwogICAgICByZW5kZXJUaW1lU2xvdHMoc2xvdHMpOwogICAgfSkKICAgIC5jYXRjaChmdW5jdGlvbigpewogICAgICByZW5kZXJUaW1lU2xvdHMoW10pOwogICAgfSk7Cn0KCmZ1bmN0aW9uIHJlbmRlclRpbWVTbG90cyhzbG90cyl7CiAgdmFyIHRnPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCd0aW1lRycpO3RnLmlubmVySFRNTD0nJzsKICBpZihzbG90cy5sZW5ndGg9PT0wKXsKICAgIHRnLmlubmVySFRNTD0nPGRpdiBjbGFzcz0ibG9hZGluZy1zbG90cyI+0J3QtdGCINC00L7RgdGC0YPQv9C90YvRhSDRgdC70L7RgtC+0LIg0L3QsCDRjdGC0YMg0LTQsNGC0YM8L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXIiIG9uY2xpY2s9InNob3dTY3JlZW4oXCdob21lU2NyZWVuXCcpIiBzdHlsZT0ibWFyZ2luLXRvcDo4cHg7Ij48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItaWNvbiI+8J+QvjwvZGl2PjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci10ZXh0Ij48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItdGl0bGUiPtCd0LUg0L3QsNGI0LvQuCDQv9C+0LTRhdC+0LTRj9GJ0LXQtSDQstGA0LXQvNGPPzwvZGl2PjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci1zdWIiPtCh0LLRj9C20LjRgtC10YHRjCDRgSDQvdCw0LzQuCDQu9GO0LHRi9C8INGD0LTQvtCx0L3Ri9C8INGB0L/QvtGB0L7QsdC+0Lwg4oCUINC80Ysg0L/QvtC00LHQtdGA0ZHQvCDRg9C00L7QsdC90L7QtSDQstGA0LXQvNGPPC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLWFycm93Ij7ihpI8L2Rpdj48L2Rpdj4nOwogICAgcmV0dXJuOwogIH0KICBzbG90cy5mb3JFYWNoKGZ1bmN0aW9uKHQpewogICAgdmFyIGJ0bj1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdidXR0b24nKTtidG4uY2xhc3NOYW1lPSd0YnRuJztidG4udGV4dENvbnRlbnQ9dDsKICAgIGJ0bi5vbmNsaWNrPWZ1bmN0aW9uKCl7CiAgICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy50YnRuJykuZm9yRWFjaChmdW5jdGlvbihiKXtiLmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpO30pOwogICAgICBidG4uY2xhc3NMaXN0LmFkZCgnYWN0aXZlJyk7Ym9va2luZy50aW1lPXQ7CiAgICAgIHNldFRpbWVvdXQoZnVuY3Rpb24oKXtnb1N0ZXAoNSk7YnVpbGRTdW0oKTt9LDMwMCk7CiAgICB9OwogICAgdGcuYXBwZW5kQ2hpbGQoYnRuKTsKICB9KTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgndGltZVNlYycpLnNjcm9sbEludG9WaWV3KHtiZWhhdmlvcjonc21vb3RoJyxibG9jazonbmVhcmVzdCd9KTsKfQoKZnVuY3Rpb24gYnVpbGRTdW0oKXsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3VtQmxvY2snKS5pbm5lckhUTUw9CiAgICAnPGRpdiBjbGFzcz0ic3IiPjxzcGFuIGNsYXNzPSJzbCI+JytUW0xBTkddLnN1bV9icmVlZCsnPC9zcGFuPjxzcGFuIGNsYXNzPSJzdiI+JysoYm9va2luZy5icmVlZERpc3BsYXl8fGJvb2tpbmcuYnJlZWQpKyc8L3NwYW4+PC9kaXY+JysKICAgICc8ZGl2IGNsYXNzPSJzciI+PHNwYW4gY2xhc3M9InNsIj4nK1RbTEFOR10uc3VtX3NlcnZpY2UrJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrKChMQU5HIT09J3J1JyYmU1ZDX1RSQU5TTEFUSU9OU1tib29raW5nLnNlcnZpY2VdKT9TVkNfVFJBTlNMQVRJT05TW2Jvb2tpbmcuc2VydmljZV1bTEFOR106Ym9va2luZy5zZXJ2aWNlKSsnPC9zcGFuPjwvZGl2PicrCiAgICAnPGRpdiBjbGFzcz0ic3IiPjxzcGFuIGNsYXNzPSJzbCI+JytUW0xBTkddLnN1bV9tYXN0ZXIrJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrYm9va2luZy5tYXN0ZXIrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fZ3Jvb20rJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrYm9va2luZy5ncm9vbUhpc3RvcnkrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fZGF0ZSsnPC9zcGFuPjxzcGFuIGNsYXNzPSJzdiI+Jytib29raW5nLmRhdGUrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fdGltZSsnPC9zcGFuPjxzcGFuIGNsYXNzPSJzdiI+Jytib29raW5nLnRpbWUrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fcHJpY2UrJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3AiPicrYm9va2luZy5wcmljZSsnIOKCrDwvc3Bhbj48L2Rpdj4nOwp9Cgpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY29uZmlybUJ0bicpLm9uY2xpY2sgPSBmdW5jdGlvbigpewogIHZhciBuYW1lPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjTmFtZScpLnZhbHVlOwogIHZhciBwaG9uZT1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY1Bob25lJykudmFsdWU7CiAgaWYoIW5hbWV8fCFwaG9uZSl7YWxlcnQoVFtMQU5HXS5hbGVydF9maWxsKTtyZXR1cm47fQogIGlmKCEvXlwrXGR7MTAsfSQvLnRlc3QocGhvbmUudHJpbSgpKSl7YWxlcnQoVFtMQU5HXS5hbGVydF9waG9uZSk7cmV0dXJuO30KICBib29raW5nLm5hbWU9bmFtZTsgYm9va2luZy5waG9uZT1waG9uZTsgYm9va2luZy5lbWFpbD1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY0VtYWlsJykudmFsdWU7IGJvb2tpbmcucGV0PWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjUGV0JykudmFsdWU7IGJvb2tpbmcubGFuZz1MQU5HOwogIGJvb2tpbmcuZHVyYXRpb24gPSBib29raW5nLmJyZWVkICYmIGJvb2tpbmcuYnJlZWQuaW5kZXhPZign0JrQvtGI0LrQsCcpID09PSAwID8gMTIwIDogMTgwOwogIHZhciBidG49ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NvbmZpcm1CdG4nKTsKICBidG4udGV4dENvbnRlbnQ9VFtMQU5HXS5zZW5kaW5nOyBidG4uZGlzYWJsZWQ9dHJ1ZTsKICBmZXRjaChSQUlMV0FZLCB7CiAgICBtZXRob2Q6J1BPU1QnLAogICAgaGVhZGVyczp7J0NvbnRlbnQtVHlwZSc6J2FwcGxpY2F0aW9uL2pzb24nfSwKICAgIGJvZHk6SlNPTi5zdHJpbmdpZnkoYm9va2luZykKICB9KS50aGVuKGZ1bmN0aW9uKCl7c2hvd1N1Y2Nlc3MoKTt9KS5jYXRjaChmdW5jdGlvbigpe3Nob3dTdWNjZXNzKCk7fSk7Cn07CgpmdW5jdGlvbiBzaG93U3VjY2VzcygpewogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdiazUnKS5jbGFzc05hbWU9J3N0ZXAnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdWNCbG9jaycpLmNsYXNzTGlzdC5hZGQoJ3Nob3cnKTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgncHJvZ3Jlc3MnKS5zdHlsZS5kaXNwbGF5PSdub25lJzsKfQoKZnVuY3Rpb24gcmVzZXRBbGwoKXsKICBib29raW5nPXticmVlZDonJyxicmVlZERpc3BsYXk6Jycsc2VydmljZTonJyxwcmljZTowLG1hc3RlcjonJyxncm9vbUhpc3Rvcnk6JycsZGF0ZTonJyx0aW1lOicnLGxhbmc6J3J1J307CiAgc2VsQnJlZWQ9bnVsbDsgaW5wLnZhbHVlPScnOyBjbHIuY2xhc3NMaXN0LnJlbW92ZSgnc2hvdycpOwogIGJhZGdlLmNsYXNzTGlzdC5yZW1vdmUoJ3Nob3cnKTsgYmFkZ2UuaW5uZXJIVE1MPScnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdmNTZWMnKS5zdHlsZS5kaXNwbGF5PSdub25lJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgndGltZVNlYycpLnN0eWxlLmRpc3BsYXk9J25vbmUnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdWNCbG9jaycpLmNsYXNzTGlzdC5yZW1vdmUoJ3Nob3cnKTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgncHJvZ3Jlc3MnKS5zdHlsZS5kaXNwbGF5PSdmbGV4JzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY05hbWUnKS52YWx1ZT0nJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY1Bob25lJykudmFsdWU9Jyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NFbWFpbCcpLnZhbHVlPScnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjUGV0JykudmFsdWU9Jyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NvbmZpcm1CdG4nKS50ZXh0Q29udGVudD1UW0xBTkddLmNvbmZpcm1fYnRuOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjb25maXJtQnRuJykuZGlzYWJsZWQ9ZmFsc2U7CiAgZ29TdGVwKDEpOwp9Cgp2YXIgTEFORyA9IGxvY2FsU3RvcmFnZS5nZXRJdGVtKCdyamxhbmcnKSB8fCAncnUnOwp2YXIgVCA9IHsKICBydTp7CiAgICBsb2dvX3RhZzon0J/RgNC10LzQuNCw0LvRjNC90YvQuSDQs9GA0YPQvNC40L3Qsy08YnI+0YHQsNC70L7QvSDQsiDQotCw0LvQu9C40L3QtScsCiAgICBjaG9vc2VfaG93OidDaG9vc2UgaG93IHRvIGNvbm5lY3QnLAogICAgYm9va19vbmxpbmU6J0Jvb2sgT25saW5lJywKICAgIGJvb2tfZmxvdzon0J/QvtGA0L7QtNCwIOKGkiDQo9GB0LvRg9Cz0LAg4oaSINCc0LDRgdGC0LXRgCDihpIg0JLRgNC10LzRjycsCiAgICBvcl9jb250YWN0OidvciBjb250YWN0IHVzJywKICAgIGNhbGxfdXM6J0NhbGwgVXMnLAogICAgYmFjazon4oaQINCd0LDQt9Cw0LQnLAogICAgbG9nb19zdWI6J0dyb29taW5nIMK3INCi0LDQu9C70LjQvScsCiAgICBwc19zZXJ2aWNlOifQo9GB0LvRg9Cz0LAnLHBzX21hc3Rlcjon0JzQsNGB0YLQtdGAJyxwc19wZXQ6J9Cf0LjRgtC+0LzQtdGGJyxwc19kYXRlOifQlNCw0YLQsCcscHNfZGV0YWlsczon0JTQsNC90L3Ri9C1JywKICAgIHN0ZXAxX2xibDonMDEgwrcg0J/QvtGA0L7QtNCwINGB0L7QsdCw0LrQuCcsCiAgICBicmVlZF9waDon0J3QsNGH0L3QuNGC0LUg0LLQstC+0LTQuNGC0Ywg0L/QvtGA0L7QtNGDLi4uJywKICAgIHN0ZXAyX2xibDonMDIgwrcg0KPRgdC70YPQs9CwJywKICAgIHN0ZXAyX21hc3Rlcjon0JLRi9Cx0LXRgNC40YLQtSDQvNCw0YHRgtC10YDQsCcsCiAgICBzdGVwM19sYmw6J9Ca0LDQuiDQtNCw0LLQvdC+INCy0Ysg0L/QvtGB0LXRidCw0LvQuCDQs9GA0YPQvNC40L3Qsz8nLAogICAgZzE6J9Cf0LXRgNCy0YvQuSDRgNCw0LcnLGcyOifQntGCIDEg0LTQviAzINC80LXRgdGP0YbQtdCyJyxnMzon0J7RgiAzINC00L4gNiDQvNC10YHRj9GG0LXQsicsZzQ6J9CR0L7Qu9C10LUgNiDQvNC10YHRj9GG0LXQsicsCiAgICBzdGVwNF9sYmw6J9CS0YvQsdC10YDQuNGC0LUg0LTQsNGC0YMnLAogICAgY2FsX2F2YWlsOifQldGB0YLRjCDRgdCy0L7QsdC+0LTQvdC+0LUg0LLRgNC10LzRjycsY2FsX25vbmU6J9Ch0LLQvtCx0L7QtNC90L7Qs9C+INCy0YDQtdC80LXQvdC4INC90LXRgicsCiAgICBzdGVwNF90aW1lOifQktGL0LHQtdGA0LjRgtC1INCy0YDQtdC80Y8nLAogICAgc3RlcDVfbGJsOifQktCw0YjQuCDQtNCw0L3QvdGL0LUnLAogICAgbGJsX25hbWU6J9CY0LzRjycscGhfbmFtZTon0JLQsNGI0LUg0LjQvNGPJywKICAgIGxibF9waG9uZTon0KLQtdC70LXRhNC+0L0nLGxibF9lbWFpbDonRW1haWwnLAogICAgbGJsX3BldDon0JrQu9C40YfQutCwINC/0LjRgtC+0LzRhtCwJyxwaF9vcHRpb25hbDon0J3QtdC+0LHRj9C30LDRgtC10LvRjNC90L4nLAogICAgY29uZmlybV9idG46J9Cf0L7QtNGC0LLQtdGA0LTQuNGC0Ywg0LfQsNC/0LjRgdGMJywKICAgIHN1Y2Nlc3NfdGl0bGU6J9CX0LDQv9C40YHRjCDQv9GA0LjQvdGP0YLQsCEnLAogICAgc3VjY2Vzc19zdWI6J9Cc0Ysg0YHQstGP0LbQtdC80YHRjyDRgSDQstCw0LzQuCDQtNC70Y8g0L/QvtC00YLQstC10YDQttC00LXQvdC40Y8uPGJyPtCh0L/QsNGB0LjQsdC+LCDRh9GC0L4g0LLRi9Cx0YDQsNC70LggUiZhbXA7SiBHcm9vbWluZyEnLAogICAgdG9faG9tZTon4oaQINCd0LAg0LPQu9Cw0LLQvdGD0Y4nLAogICAgYWxlcnRfZmlsbDon0JLQstC10LTQuNGC0LUg0LjQvNGPINC4INGC0LXQu9C10YTQvtC9JyxhbGVydF9waG9uZTon0JLQstC10LTQuNGC0LUg0L3QvtC80LXRgCDQsiDRhNC+0YDQvNCw0YLQtSArMzcyMTIzNDU2NzgnLAogICAgc2VuZGluZzon0J7RgtC/0YDQsNCy0LvRj9C10LwuLi4nLAogICAgc3VtX2JyZWVkOifQn9C+0YDQvtC00LAnLHN1bV9zZXJ2aWNlOifQo9GB0LvRg9Cz0LAnLHN1bV9tYXN0ZXI6J9Cc0LDRgdGC0LXRgCcsc3VtX2dyb29tOifQn9C+0YHQu9C10LTQvdC40Lkg0LPRgNGD0LwnLHN1bV9kYXRlOifQlNCw0YLQsCcsc3VtX3RpbWU6J9CS0YDQtdC80Y8nLHN1bV9wcmljZTon0KHRgtC+0LjQvNC+0YHRgtGMJywKICAgIG1vbnRoczpbJ9Cv0L3QstCw0YDRjCcsJ9Ck0LXQstGA0LDQu9GMJywn0JzQsNGA0YInLCfQkNC/0YDQtdC70YwnLCfQnNCw0LknLCfQmNGO0L3RjCcsJ9CY0Y7Qu9GMJywn0JDQstCz0YPRgdGCJywn0KHQtdC90YLRj9Cx0YDRjCcsJ9Ce0LrRgtGP0LHRgNGMJywn0J3QvtGP0LHRgNGMJywn0JTQtdC60LDQsdGA0YwnXQogIH0sCiAgZW46ewogICAgbG9nb190YWc6J1ByZW1pdW0gZ3Jvb21pbmc8YnI+c2Fsb24gaW4gVGFsbGlubicsCiAgICBjaG9vc2VfaG93OidDaG9vc2UgaG93IHRvIGNvbm5lY3QnLAogICAgYm9va19vbmxpbmU6J0Jvb2sgT25saW5lJywKICAgIGJvb2tfZmxvdzonQnJlZWQg4oaSIFNlcnZpY2Ug4oaSIE1hc3RlciDihpIgVGltZScsCiAgICBvcl9jb250YWN0OidvciBjb250YWN0IHVzJywKICAgIGNhbGxfdXM6J0NhbGwgVXMnLAogICAgYmFjazon4oaQIEJhY2snLAogICAgbG9nb19zdWI6J0dyb29taW5nIMK3IFRhbGxpbm4nLAogICAgcHNfc2VydmljZTonU2VydmljZScscHNfbWFzdGVyOidNYXN0ZXInLHBzX3BldDonUGV0Jyxwc19kYXRlOidEYXRlJyxwc19kZXRhaWxzOidEZXRhaWxzJywKICAgIHN0ZXAxX2xibDonMDEgwrcgRG9nIGJyZWVkJywKICAgIGJyZWVkX3BoOidTdGFydCB0eXBpbmcgYnJlZWQuLi4nLAogICAgc3RlcDJfbGJsOicwMiDCtyBTZXJ2aWNlJywKICAgIHN0ZXAyX21hc3RlcjonQ2hvb3NlIG1hc3RlcicsCiAgICBzdGVwM19sYmw6J0hvdyBsb25nIGFnbyB3YXMgeW91ciBsYXN0IGdyb29taW5nPycsCiAgICBnMTonRmlyc3QgdGltZScsZzI6JzHigJMzIG1vbnRocyBhZ28nLGczOicz4oCTNiBtb250aHMgYWdvJyxnNDonT3ZlciA2IG1vbnRocycsCiAgICBzdGVwNF9sYmw6J0Nob29zZSBkYXRlJywKICAgIGNhbF9hdmFpbDonQXZhaWxhYmxlJyxjYWxfbm9uZTonTm90IGF2YWlsYWJsZScsCiAgICBzdGVwNF90aW1lOidDaG9vc2UgdGltZScsCiAgICBzdGVwNV9sYmw6J1lvdXIgZGV0YWlscycsCiAgICBsYmxfbmFtZTonTmFtZScscGhfbmFtZTonWW91ciBuYW1lJywKICAgIGxibF9waG9uZTonUGhvbmUnLGxibF9lbWFpbDonRW1haWwnLAogICAgbGJsX3BldDoiUGV0J3MgbmFtZSIscGhfb3B0aW9uYWw6J09wdGlvbmFsJywKICAgIGNvbmZpcm1fYnRuOidDb25maXJtIGJvb2tpbmcnLAogICAgc3VjY2Vzc190aXRsZTonQm9va2luZyBjb25maXJtZWQhJywKICAgIHN1Y2Nlc3Nfc3ViOidXZSB3aWxsIGNvbnRhY3QgeW91IHRvIGNvbmZpcm0uPGJyPlRoYW5rIHlvdSBmb3IgY2hvb3NpbmcgUiZhbXA7SiBHcm9vbWluZyEnLAogICAgdG9faG9tZTon4oaQIEhvbWUnLAogICAgYWxlcnRfZmlsbDonUGxlYXNlIGVudGVyIG5hbWUgYW5kIHBob25lJyxhbGVydF9waG9uZTonRW50ZXIgcGhvbmUgbnVtYmVyIGluIGZvcm1hdCArMzcyMTIzNDU2NzgnLAogICAgc2VuZGluZzonU2VuZGluZy4uLicsCiAgICBzdW1fYnJlZWQ6J0JyZWVkJyxzdW1fc2VydmljZTonU2VydmljZScsc3VtX21hc3RlcjonTWFzdGVyJyxzdW1fZ3Jvb206J0xhc3QgZ3Jvb21pbmcnLHN1bV9kYXRlOidEYXRlJyxzdW1fdGltZTonVGltZScsc3VtX3ByaWNlOidQcmljZScsCiAgICBtb250aHM6WydKYW51YXJ5JywnRmVicnVhcnknLCdNYXJjaCcsJ0FwcmlsJywnTWF5JywnSnVuZScsJ0p1bHknLCdBdWd1c3QnLCdTZXB0ZW1iZXInLCdPY3RvYmVyJywnTm92ZW1iZXInLCdEZWNlbWJlciddCiAgfSwKICBldDp7CiAgICBsb2dvX3RhZzonRXNtYWtsYXNzaWxpbmUgaG9vbGR1c3RlZW51czxicj5UYWxsaW5uYXMnLAogICAgY2hvb3NlX2hvdzonVmFsaSDDvGhlbmR1c3ZpaXMnLAogICAgYm9va19vbmxpbmU6J0Jyb25lZXJpIHZlZWJpcycsCiAgICBib29rX2Zsb3c6J1TDtXVnIOKGkiBUZWVudXMg4oaSIE1laXN0ZXIg4oaSIEFlZycsCiAgICBvcl9jb250YWN0Oid2w7VpIHbDtXRhIMO8aGVuZHVzdCcsCiAgICBjYWxsX3VzOidIZWxpc3RhIG1laWxlJywKICAgIGJhY2s6J+KGkCBUYWdhc2knLAogICAgbG9nb19zdWI6J0dyb29taW5nIMK3IFRhbGxpbm4nLAogICAgcHNfc2VydmljZTonVGVlbnVzJyxwc19tYXN0ZXI6J01laXN0ZXInLHBzX3BldDonTGVtbWlrbG9vbScscHNfZGF0ZTonS3V1cMOkZXYnLHBzX2RldGFpbHM6J0FuZG1lZCcsCiAgICBzdGVwMV9sYmw6JzAxIMK3IEtvZXJhIHTDtXVnJywKICAgIGJyZWVkX3BoOidBbHVzdGFnZSB0w7V1IHNpc2VzdGFtaXN0Li4uJywKICAgIHN0ZXAyX2xibDonMDIgwrcgVGVlbnVzJywKICAgIHN0ZXAyX21hc3RlcjonVmFsaSBtZWlzdGVyJywKICAgIHN0ZXAzX2xibDonTWlsbGFsIGvDpGlzaXRlIHZpaW1hdGkgZ3Jvb21pbmd1cz8nLAogICAgZzE6J0VzaW1lc3Qga29yZGEnLGcyOicx4oCTMyBrdXVkIHRhZ2FzaScsZzM6JzPigJM2IGt1dWQgdGFnYXNpJyxnNDonw5xsZSA2IGt1dScsCiAgICBzdGVwNF9sYmw6J1ZhbGkga3V1cMOkZXYnLAogICAgY2FsX2F2YWlsOidWYWJ1IGFlZ3Ugb24nLGNhbF9ub25lOidWYWJ1IGFlZ3UgcG9sZScsCiAgICBzdGVwNF90aW1lOidWYWxpIGtlbGxhYWVnJywKICAgIHN0ZXA1X2xibDonVGVpZSBhbmRtZWQnLAogICAgbGJsX25hbWU6J05pbWknLHBoX25hbWU6J1RlaWUgbmltaScsCiAgICBsYmxfcGhvbmU6J1RlbGVmb24nLGxibF9lbWFpbDonRW1haWwnLAogICAgbGJsX3BldDonTGVtbWlrbG9vbWEgbmltaScscGhfb3B0aW9uYWw6J1ZhbGlrdWxpbmUnLAogICAgY29uZmlybV9idG46J0tpbm5pdGEgYnJvbmVlcmluZycsCiAgICBzdWNjZXNzX3RpdGxlOidCcm9uZWVyaW5nIGtpbm5pdGF0dWQhJywKICAgIHN1Y2Nlc3Nfc3ViOidWw7V0YW1lIHRlaWVnYSDDvGhlbmR1c3Qga2lubml0YW1pc2Vrcy48YnI+VMOkbmFtZSwgZXQgdmFsaXNpdGUgUiZhbXA7SiBHcm9vbWluZyEnLAogICAgdG9faG9tZTon4oaQIEF2YWxlaGVsZScsCiAgICBhbGVydF9maWxsOidQYWx1biBzaXNlc3RhZ2UgbmltaSBqYSB0ZWxlZm9uJyxhbGVydF9waG9uZTonU2lzZXN0YWdlIHRlbGVmb25pbnVtYmVyIHZvcm1pbmd1cyArMzcyMTIzNDU2NzgnLAogICAgc2VuZGluZzonU2FhZGFuLi4uJywKICAgIHN1bV9icmVlZDonVMO1dWcnLHN1bV9zZXJ2aWNlOidUZWVudXMnLHN1bV9tYXN0ZXI6J01laXN0ZXInLHN1bV9ncm9vbTonVmlpbWFuZSBncm9vbWluZycsc3VtX2RhdGU6J0t1dXDDpGV2JyxzdW1fdGltZTonS2VsbGFhZWcnLHN1bV9wcmljZTonSGluZCcsCiAgICBtb250aHM6WydKYWFudWFyJywnVmVlYnJ1YXInLCdNw6RydHMnLCdBcHJpbGwnLCdNYWknLCdKdXVuaScsJ0p1dWxpJywnQXVndXN0JywnU2VwdGVtYmVyJywnT2t0b29iZXInLCdOb3ZlbWJlcicsJ0RldHNlbWJlciddCiAgfQp9OwoKZnVuY3Rpb24gc2V0TGFuZyhsKXsKICBMQU5HPWw7CiAgbG9jYWxTdG9yYWdlLnNldEl0ZW0oJ3JqbGFuZycsbCk7CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmxhbmctYnRuJykuZm9yRWFjaChmdW5jdGlvbihiKXsKICAgIGIuY2xhc3NMaXN0LnRvZ2dsZSgnYWN0aXZlJywgYi50ZXh0Q29udGVudC50b0xvd2VyQ2FzZSgpPT09bCk7CiAgfSk7CiAgdmFyIHRyPVRbbF07CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnW2RhdGEtaTE4bl0nKS5mb3JFYWNoKGZ1bmN0aW9uKGVsKXsKICAgIHZhciBrPWVsLmdldEF0dHJpYnV0ZSgnZGF0YS1pMThuJyk7CiAgICBpZih0cltrXSE9PXVuZGVmaW5lZCkgZWwuaW5uZXJIVE1MPXRyW2tdOwogIH0pOwogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tkYXRhLWkxOG4tcGhdJykuZm9yRWFjaChmdW5jdGlvbihlbCl7CiAgICB2YXIgaz1lbC5nZXRBdHRyaWJ1dGUoJ2RhdGEtaTE4bi1waCcpOwogICAgaWYodHJba10hPT11bmRlZmluZWQpIGVsLnBsYWNlaG9sZGVyPXRyW2tdOwogIH0pOwogIE1PTlRIUz10ci5tb250aHM7CiAgYnVpbGRDYWwoKTsKICAvLyBSZS1yZW5kZXIgYmFkZ2UgYW5kIHNlcnZpY2VzIGlmIGJyZWVkIGFscmVhZHkgc2VsZWN0ZWQKICBpZihzZWxCcmVlZCl7CiAgICB2YXIgYmY9bD09PSdlbic/J2JyZWVkX2VuJzpsPT09J2V0Jz8nYnJlZWRfZXQnOidicmVlZCc7CiAgICB2YXIgZGI9c2VsQnJlZWRbYmZdfHxzZWxCcmVlZC5icmVlZDsKICAgIGJvb2tpbmcuYnJlZWREaXNwbGF5PWRiOwogICAgdmFyIGJuRWw9ZG9jdW1lbnQucXVlcnlTZWxlY3RvcignI3NCYWRnZSAuYm5hbWUnKTsKICAgIGlmKGJuRWwpIGJuRWwudGV4dENvbnRlbnQ9ZGI7CiAgICB2YXIgYmNFbD1kb2N1bWVudC5xdWVyeVNlbGVjdG9yKCcjc0JhZGdlIC5iY2hnJyk7CiAgICBpZihiY0VsKSBiY0VsLnRleHRDb250ZW50PWw9PT0nZW4nPydDaGFuZ2UnOmw9PT0nZXQnPydNdXVkYSc6J9CY0LfQvNC10L3QuNGC0YwnOwogICAgaWYoZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y1NlYycpLnN0eWxlLmRpc3BsYXkhPT0nbm9uZScpIHJlbmRlclN2Y3Moc2VsQnJlZWQpOwogICAgdmFyIHNuPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdmNOb3RlJyk7CiAgICBpZihzbil7CiAgICAgIHZhciBudD1sPT09J2VuJz8nUGxlYXNlIG5vdGUnOmw9PT0nZXQnPydQYW5nZSB0w6RoZWxlJzon0JLQsNC20L3QviDQt9C90LDRgtGMJzsKICAgICAgdmFyIG5iPWw9PT0nZW4nPydGaW5hbCBwcmljZSBkZXBlbmRzIG9uIGNvYXQgY29uZGl0aW9uIGFuZCBwZXQgYmVoYXZpb3VyLjxicj5EZW1hdHRpbmcgZnJvbSA1IOKCrC48YnI+QWdncmVzc2l2ZSBiZWhhdmlvdXIgc3VyY2hhcmdlIG1heSBhcHBseTogKzUwJS4nOmw9PT0nZXQnPydMw7VwbGlrIGhpbmQgc8O1bHR1YiBrYXJ2YXN0aWt1IHNlaXN1bmRpc3QgamEgbGVtbWlrbG9vbWEga8OkaXR1bWlzZXN0Ljxicj5Lb2x0c3VuaXRlIGxhaHRpaGFydXRhbWluZSBhbGF0ZXMgNSDigqwuPGJyPkFncmVzc2lpdnNlIGvDpGl0dW1pc2Uga29ycmFsIHbDtWliIGxpc2FuZHVkYSA1MCUganV1cmRlaGluZGx1cy4nOifQntC60L7QvdGH0LDRgtC10LvRjNC90LDRjyDRgdGC0L7QuNC80L7RgdGC0Ywg0LfQsNCy0LjRgdC40YIg0L7RgiDRgdC+0YHRgtC+0Y/QvdC40Y8g0YjQtdGA0YHRgtC4INC4INC/0L7QstC10LTQtdC90LjRjyDQv9C40YLQvtC80YbQsC48YnI+0KDQsNC30LHQvtGAINC60L7Qu9GC0YPQvdC+0LIg4oCUINC+0YIgNSDigqwuPGJyPtCf0YDQuCDQsNCz0YDQtdGB0YHQuNCy0L3QvtC8INC/0L7QstC10LTQtdC90LjQuCDQvNC+0LbQtdGCINC/0YDQuNC80LXQvdGP0YLRjNGB0Y8g0LTQvtC/0LvQsNGC0LAgNTAlLic7CiAgICAgIHNuLmlubmVySFRNTD0nPGRpdiBzdHlsZT0iZm9udC1zaXplOi41OHJlbTtsZXR0ZXItc3BhY2luZzouMTVlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6IzhhOGE1NTttYXJnaW4tYm90dG9tOjhweDtmb250LXdlaWdodDo2MDA7Ij4nK250Kyc8L2Rpdj48ZGl2IHN0eWxlPSJmb250LXNpemU6LjcxcmVtO2NvbG9yOiM3Nzc3NzA7bGluZS1oZWlnaHQ6MS44OyI+JytuYisnPC9kaXY+JzsKICAgIH0KICB9Cn0KCi8vIEFwcGx5IHNhdmVkIGxhbmd1YWdlIG9uIGxvYWQKKGZ1bmN0aW9uKCl7IHNldExhbmcoTEFORyk7IH0pKCk7CgovLyBDYWxsYmFjayBmb3JtCmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYWxsYmFja0J0bicpLm9uY2xpY2sgPSBmdW5jdGlvbigpewogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtNb2RhbCcpLnN0eWxlLmRpc3BsYXkgPSAnZmxleCc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia05hbWUnKS52YWx1ZSA9ICcnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtQaG9uZScpLnZhbHVlID0gJyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1N1Y2Nlc3MnKS5zdHlsZS5kaXNwbGF5ID0gJ25vbmUnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtTdWJtaXQnKS5zdHlsZS5kaXNwbGF5ID0gJ2Jsb2NrJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrQ2xvc2UnKS50ZXh0Q29udGVudCA9ICfQntGC0LzQtdC90LAnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtTdWJtaXQnKS50ZXh0Q29udGVudCA9ICfQntGC0L/RgNCw0LLQuNGC0YwnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtTdWJtaXQnKS5kaXNhYmxlZCA9IGZhbHNlOwp9Owpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrQ2xvc2UnKS5vbmNsaWNrID0gZnVuY3Rpb24oKXsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrTW9kYWwnKS5zdHlsZS5kaXNwbGF5ID0gJ25vbmUnOwp9Owpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrU3VibWl0Jykub25jbGljayA9IGZ1bmN0aW9uKCl7CiAgdmFyIG5hbWUgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrTmFtZScpLnZhbHVlLnRyaW0oKTsKICB2YXIgcGhvbmUgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrUGhvbmUnKS52YWx1ZS50cmltKCkucmVwbGFjZSgvXEQvZywnJyk7CiAgaWYoIW5hbWUgfHwgIXBob25lKXthbGVydCgn0JLQstC10LTQuNGC0LUg0LjQvNGPINC4INGC0LXQu9C10YTQvtC9Jyk7cmV0dXJuO30KICB2YXIgYnRuID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1N1Ym1pdCcpOwogIGJ0bi50ZXh0Q29udGVudCA9ICfQntGC0L/RgNCw0LLQu9GP0LXQvC4uLic7IGJ0bi5kaXNhYmxlZCA9IHRydWU7CiAgZmV0Y2goJy9hcGkvY2FsbGJhY2snLHsKICAgIG1ldGhvZDonUE9TVCcsCiAgICBoZWFkZXJzOnsnQ29udGVudC1UeXBlJzonYXBwbGljYXRpb24vanNvbid9LAogICAgYm9keTpKU09OLnN0cmluZ2lmeSh7bmFtZTpuYW1lLCBwaG9uZTonKzM3MicrcGhvbmV9KQogIH0pLnRoZW4oZnVuY3Rpb24oKXsKICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtTdWNjZXNzJykuc3R5bGUuZGlzcGxheSA9ICdibG9jayc7CiAgICBidG4uc3R5bGUuZGlzcGxheSA9ICdub25lJzsKICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtDbG9zZScpLnRleHRDb250ZW50ID0gJ+KGkCDQl9Cw0LrRgNGL0YLRjCc7CiAgICBzZXRUaW1lb3V0KGZ1bmN0aW9uKCl7ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia01vZGFsJykuc3R5bGUuZGlzcGxheT0nbm9uZSc7fSwzMDAwKTsKICB9KS5jYXRjaChmdW5jdGlvbigpewogICAgYnRuLnRleHRDb250ZW50ID0gJ9Ce0YLQv9GA0LDQstC40YLRjCc7IGJ0bi5kaXNhYmxlZCA9IGZhbHNlOwogICAgYWxlcnQoJ9Ce0YjQuNCx0LrQsC4g0J/QvtC/0YDQvtCx0YPQudGC0LUg0LXRidGRINGA0LDQty4nKTsKICB9KTsKfTsKCjwvc2NyaXB0Pgo8L2JvZHk+CjwvaHRtbD4="



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
STATS_HTML_B64 = "PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9InJ1Ij4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCxpbml0aWFsLXNjYWxlPTEiPgo8bWV0YSBuYW1lPSJ0aGVtZS1jb2xvciIgY29udGVudD0iIzFjMWMxOCI+Cjx0aXRsZT5SJkogU3RhdHM8L3RpdGxlPgo8bGluayBocmVmPSJodHRwczovL2ZvbnRzLmdvb2dsZWFwaXMuY29tL2NzczI/ZmFtaWx5PUNvcm1vcmFudCtHYXJhbW9uZDppdGFsLHdnaHRAMCw2MDA7MSw0MDAmZmFtaWx5PU1vbnRzZXJyYXQ6d2dodEAzMDA7NDAwOzUwMDs2MDAmZGlzcGxheT1zd2FwIiByZWw9InN0eWxlc2hlZXQiPgo8c3R5bGU+Cip7Ym94LXNpemluZzpib3JkZXItYm94O21hcmdpbjowO3BhZGRpbmc6MH0KaHRtbCxib2R5e21pbi1oZWlnaHQ6MTAwdmg7YmFja2dyb3VuZDojMWMxYzE4O2NvbG9yOiNjOGMyYjg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC13ZWlnaHQ6MzAwfQouY29ue3dpZHRoOjEwMCU7bWF4LXdpZHRoOjUwMHB4O3BhZGRpbmc6MCAyMnB4O21hcmdpbjowIGF1dG99Ci5wYWdle3BhZGRpbmc6MzJweCAwIDYwcHh9Ci5sb2dvLXJqe2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmO2ZvbnQtc2l6ZToxLjhyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNlOGUwZDB9Ci5sb2dvLXN1Yntmb250LXNpemU6LjQ0cmVtO2xldHRlci1zcGFjaW5nOi40ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiM4YThhNTU7bWFyZ2luLXRvcDoycHg7cGFkZGluZy1ib3R0b206MTZweDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjIpO21hcmdpbi1ib3R0b206MjBweH0KLnRhYnMtbWFpbntkaXNwbGF5OmZsZXg7Z2FwOjA7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4yKTttYXJnaW4tYm90dG9tOjB9Ci50bWJ7cGFkZGluZzoxMnB4IDE4cHg7Zm9udC1zaXplOi41NnJlbTtsZXR0ZXItc3BhY2luZzouMThlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6IzU1NTU1MDtjdXJzb3I6cG9pbnRlcjtib3JkZXItYm90dG9tOjJweCBzb2xpZCB0cmFuc3BhcmVudDtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyLXRvcDpub25lO2JvcmRlci1sZWZ0Om5vbmU7Ym9yZGVyLXJpZ2h0Om5vbmU7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7dHJhbnNpdGlvbjphbGwgLjJzfQoudG1iLmFjdGl2ZXtjb2xvcjojYzlhODRjO2JvcmRlci1ib3R0b20tY29sb3I6I2M5YTg0Y30KLnRtYjpob3Zlcntjb2xvcjojYzhjMmI4fQoucGFuZWx7ZGlzcGxheTpub25lO3BhZGRpbmc6MjBweCAwfQoucGFuZWwuYWN0aXZle2Rpc3BsYXk6YmxvY2t9Ci5zbGJse2ZvbnQtc2l6ZTouNTJyZW07bGV0dGVyLXNwYWNpbmc6LjJlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2M5YTg0YzttYXJnaW4tYm90dG9tOjEwcHg7Zm9udC13ZWlnaHQ6NTAwfQoubWV0cmljc3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCgzLDFmcik7Z2FwOjZweDttYXJnaW4tYm90dG9tOjIycHh9Ci5tZXRyaWN7YmFja2dyb3VuZDojMTQxNDEwO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDcpO3BhZGRpbmc6MTJweCAxNHB4fQoubWV0cmljLWxhYmVse2ZvbnQtc2l6ZTouNTRyZW07bGV0dGVyLXNwYWNpbmc6LjFlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6IzU1NTU1MDttYXJnaW4tYm90dG9tOjVweH0KLm1ldHJpYy12YWx7Zm9udC1mYW1pbHk6J0Nvcm1vcmFudCBHYXJhbW9uZCcsc2VyaWY7Zm9udC1zaXplOjEuMzVyZW07Y29sb3I6I2M5YTg0Yztmb250LXdlaWdodDo2MDB9Ci5tZXRyaWMtc3Vie2ZvbnQtc2l6ZTouNThyZW07Y29sb3I6IzQ0NDQ0MDttYXJnaW4tdG9wOjJweH0KLmRpc2NvdW50LXJvd3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxMHB4O2JhY2tncm91bmQ6IzE0MTQxMDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjAxLDE2OCw3NiwuMik7cGFkZGluZzoxMnB4IDE0cHg7bWFyZ2luLWJvdHRvbToxOHB4fQouZGlzY291bnQtbGFiZWx7Zm9udC1zaXplOi41OHJlbTtsZXR0ZXItc3BhY2luZzouMWVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojOGE4YTU1O2ZsZXg6MX0KLmRpc2NvdW50LWlucHV0e2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOm5vbmU7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC40KTtjb2xvcjojYzlhODRjO2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmO2ZvbnQtc2l6ZToxLjJyZW07Zm9udC13ZWlnaHQ6NjAwO3dpZHRoOjgwcHg7dGV4dC1hbGlnbjpyaWdodDtvdXRsaW5lOm5vbmU7cGFkZGluZzoycHggNHB4fQouZGlzY291bnQtaW5wdXQ6Zm9jdXN7Ym9yZGVyLWJvdHRvbS1jb2xvcjojYzlhODRjfQouZGlzY291bnQtZXVye2NvbG9yOiM4YThhNTU7Zm9udC1zaXplOi43NXJlbTttYXJnaW4tbGVmdDoycHh9Ci5wZXJpb2Qtcm93e2Rpc3BsYXk6ZmxleDtnYXA6OHB4O21hcmdpbi1ib3R0b206MThweDthbGlnbi1pdGVtczpjZW50ZXJ9Ci5wZXJpb2Qtc2VsZWN0e2JhY2tncm91bmQ6IzE0MTQxMDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEpO2NvbG9yOiNjOGMyYjg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC1zaXplOi43MnJlbTtwYWRkaW5nOjhweCAxMnB4O291dGxpbmU6bm9uZTtmbGV4OjF9Ci5wZXJpb2Qtc2VsZWN0OmZvY3Vze2JvcmRlci1jb2xvcjojYzlhODRjfQoucmVmcmVzaC1idG57YmFja2dyb3VuZDpyZ2JhKDIwMSwxNjgsNzYsLjEyKTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjAxLDE2OCw3NiwuMyk7Y29sb3I6I2M5YTg0YztwYWRkaW5nOjhweCAxNHB4O2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZTouNThyZW07bGV0dGVyLXNwYWNpbmc6LjE0ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO3doaXRlLXNwYWNlOm5vd3JhcH0KLnJlZnJlc2gtYnRuOmhvdmVye2JhY2tncm91bmQ6cmdiYSgyMDEsMTY4LDc2LC4yMil9Ci5tYXN0ZXItY2FyZHtiYWNrZ3JvdW5kOiMxNDE0MTA7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNyk7Ym9yZGVyLWxlZnQ6M3B4IHNvbGlkIHJnYmEoMjAxLDE2OCw3NiwuMyk7bWFyZ2luLWJvdHRvbTo2cHg7b3ZlcmZsb3c6aGlkZGVufQoubWFzdGVyLWhlYWR7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2FsaWduLWl0ZW1zOmNlbnRlcjtwYWRkaW5nOjEzcHggMTVweDtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmJhY2tncm91bmQgLjJzfQoubWFzdGVyLWhlYWQ6aG92ZXJ7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wMil9Ci5tYXN0ZXItaGVhZC5vcGVue2JvcmRlci1sZWZ0LWNvbG9yOiNjOWE4NGN9Ci5tbmFtZXtmb250LXNpemU6Ljg4cmVtO2ZvbnQtd2VpZ2h0OjUwMDtjb2xvcjojZThlMGQwfQoubWNvdW50e2ZvbnQtc2l6ZTouNnJlbTtjb2xvcjojNTU1NTUwO21hcmdpbi10b3A6MnB4fQoubWVhcm5pbmdze2Rpc3BsYXk6ZmxleDtnYXA6MTRweDthbGlnbi1pdGVtczpjZW50ZXJ9Ci5lYXJuLWl0ZW17dGV4dC1hbGlnbjpyaWdodH0KLmVhcm4tbGFiZWx7Zm9udC1zaXplOi41cmVtO2NvbG9yOiM1NTU1NTA7bGV0dGVyLXNwYWNpbmc6LjFlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2V9Ci5lYXJuLXZhbHtmb250LXNpemU6Ljg4cmVtO2NvbG9yOiNjOWE4NGM7Zm9udC13ZWlnaHQ6NTAwfQouZWFybi12YWwuc2Fsb257Y29sb3I6IzhhOGE1NX0KLmNoZXZyb257Y29sb3I6IzhhOGE1NTtmb250LXNpemU6Ljc1cmVtO3RyYW5zaXRpb246dHJhbnNmb3JtIC4yNXM7bWFyZ2luLWxlZnQ6OHB4fQoubWFzdGVyLWhlYWQub3BlbiAuY2hldnJvbnt0cmFuc2Zvcm06cm90YXRlKDE4MGRlZyl9Ci5tYXN0ZXItYm9keXtkaXNwbGF5Om5vbmU7Ym9yZGVyLXRvcDoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDYpfQoubWFzdGVyLWJvZHkub3BlbntkaXNwbGF5OmJsb2NrfQoudmlzaXRzLWhlYWRlcntkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjcycHggMWZyIDg1cHggNjBweDtnYXA6NnB4O3BhZGRpbmc6OHB4IDE1cHg7Zm9udC1zaXplOi41cmVtO2NvbG9yOiM0NDQ0NDA7bGV0dGVyLXNwYWNpbmc6LjFlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wMil9Ci52aXNpdC1yb3d7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczo3MnB4IDFmciA4NXB4IDYwcHg7Z2FwOjZweDtwYWRkaW5nOjlweCAxNXB4O2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA0KTtmb250LXNpemU6LjcycmVtO2FsaWduLWl0ZW1zOnN0YXJ0fQoudmlzaXQtcm93Omxhc3QtY2hpbGR7Ym9yZGVyLWJvdHRvbTpub25lfQoudmlzaXQtZGF0ZXtjb2xvcjojNTU1NTUwfQoudmlzaXQtY2xpZW50e2NvbG9yOiNjOGMyYjg7Zm9udC1zaXplOi43NXJlbX0KLnZpc2l0LXBldHtjb2xvcjojNTU1NTUwO2ZvbnQtc2l6ZTouNjJyZW07bWFyZ2luLXRvcDoxcHh9Ci52aXNpdC1zdmN7Y29sb3I6IzY2NjY2MDtmb250LXNpemU6LjY1cmVtfQoudmlzaXQtcHJpY2V7Y29sb3I6I2M5YTg0Yzt0ZXh0LWFsaWduOnJpZ2h0O2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmO2ZvbnQtc2l6ZTouOTVyZW07Zm9udC13ZWlnaHQ6NjAwfQoubm8tdmlzaXRze3BhZGRpbmc6MTZweCAxNXB4O2ZvbnQtc2l6ZTouNzJyZW07Y29sb3I6IzQ0NDQ0MDtmb250LXN0eWxlOml0YWxpY30KLnN1Yi10YWJze2Rpc3BsYXk6ZmxleDtnYXA6NXB4O21hcmdpbi1ib3R0b206MThweH0KLnN0YntwYWRkaW5nOjdweCAxNHB4O2ZvbnQtc2l6ZTouNTRyZW07bGV0dGVyLXNwYWNpbmc6LjE0ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiM1NTU1NTA7Y3Vyc29yOnBvaW50ZXI7YmFja2dyb3VuZDojMTQxNDEwO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDcpO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO3RyYW5zaXRpb246YWxsIC4yc30KLnN0Yi5hY3RpdmV7Y29sb3I6I2M5YTg0Yztib3JkZXItY29sb3I6cmdiYSgyMDEsMTY4LDc2LC40KTtiYWNrZ3JvdW5kOnJnYmEoMjAxLDE2OCw3NiwuMDYpfQouc3RiOmhvdmVye2NvbG9yOiNjOGMyYjh9Ci5zdWItcGFuZWx7ZGlzcGxheTpub25lfQouc3ViLXBhbmVsLmFjdGl2ZXtkaXNwbGF5OmJsb2NrfQouZml7d2lkdGg6MTAwJTtiYWNrZ3JvdW5kOiMxNDE0MTA7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xKTtjb2xvcjojYzhjMmI4O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZTouODJyZW07cGFkZGluZzoxMXB4IDE0cHg7b3V0bGluZTpub25lO21hcmdpbi1ib3R0b206OHB4fQouZmk6Zm9jdXN7Ym9yZGVyLWNvbG9yOiNjOWE4NGN9CnNlbGVjdC5maXthcHBlYXJhbmNlOm5vbmU7LXdlYmtpdC1hcHBlYXJhbmNlOm5vbmV9Ci5maS1yb3d7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyO2dhcDo4cHh9Ci5jYnRue2Rpc3BsYXk6YmxvY2s7d2lkdGg6MTAwJTtiYWNrZ3JvdW5kOiM0YTRhMmU7Y29sb3I6I2M4YzJiODtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXNpemU6LjZyZW07Zm9udC13ZWlnaHQ6NjAwO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtwYWRkaW5nOjEzcHg7Ym9yZGVyOm5vbmU7Y3Vyc29yOnBvaW50ZXI7bWFyZ2luLXRvcDo0cHg7dHJhbnNpdGlvbjpiYWNrZ3JvdW5kIC4yc30KLmNidG46aG92ZXJ7YmFja2dyb3VuZDojNmI2YjQyfQouY2J0bi5naG9zdHtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjMpO2NvbG9yOiNjOWE4NGN9Ci5jYnRuLmdob3N0OmhvdmVye2JhY2tncm91bmQ6cmdiYSgyMDEsMTY4LDc2LC4wOCl9Ci5saXN0LWl0ZW17ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2FsaWduLWl0ZW1zOmNlbnRlcjtwYWRkaW5nOjExcHggMTRweDtiYWNrZ3JvdW5kOiMxNDE0MTA7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNyk7bWFyZ2luLWJvdHRvbTo1cHh9Ci5saS1uYW1le2ZvbnQtc2l6ZTouODJyZW07Y29sb3I6I2M4YzJiOH0KLmxpLXN1Yntmb250LXNpemU6LjZyZW07Y29sb3I6IzU1NTU1MDttYXJnaW4tdG9wOjJweH0KLmRlbC1idG57YmFja2dyb3VuZDpub25lO2JvcmRlcjpub25lO2NvbG9yOiM0NDQ0NDA7Y3Vyc29yOnBvaW50ZXI7Zm9udC1zaXplOi44cmVtO3BhZGRpbmc6NHB4IDhweDt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLmRlbC1idG46aG92ZXJ7Y29sb3I6I2MwNTA1MH0KLmJyZWVkLWNhcmR7YmFja2dyb3VuZDojMTQxNDEwO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDcpO2JvcmRlci1sZWZ0OjNweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjIpO21hcmdpbi1ib3R0b206NnB4fQouYnJlZWQtaGVhZHtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6Y2VudGVyO3BhZGRpbmc6MTFweCAxNHB4O2N1cnNvcjpwb2ludGVyfQouYnJlZWQtaGVhZDpob3ZlcntiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjAyKX0KLmJyZWVkLW5hbWV7Zm9udC1zaXplOi44MnJlbTtjb2xvcjojZThlMGQwfQouYnJlZWQtY291bnR7Zm9udC1zaXplOi42cmVtO2NvbG9yOiM1NTU1NTB9Ci5icmVlZC1ib2R5e2Rpc3BsYXk6bm9uZTtib3JkZXItdG9wOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNik7cGFkZGluZzoxMHB4IDE0cHh9Ci5icmVlZC1ib2R5Lm9wZW57ZGlzcGxheTpibG9ja30KLnN2Yy1yb3d7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2FsaWduLWl0ZW1zOmNlbnRlcjtwYWRkaW5nOjZweCAwO2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA0KTtmb250LXNpemU6Ljc1cmVtfQouc3ZjLXJvdzpsYXN0LWNoaWxke2JvcmRlci1ib3R0b206bm9uZX0KLnN2Yy1uYW1le2NvbG9yOiNjOGMyYjh9Ci5zdmMtcHJpY2V7Y29sb3I6I2M5YTg0Yztmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZjtmb250LXNpemU6Ljk1cmVtO2ZvbnQtd2VpZ2h0OjYwMH0KLmFkZC1zdmMtcm93e2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6MWZyIDcwcHggMzRweDtnYXA6NnB4O21hcmdpbi10b3A6MTBweH0KLmFkZC1zdmMtcm93IC5maXttYXJnaW4tYm90dG9tOjA7Zm9udC1zaXplOi43NXJlbTtwYWRkaW5nOjhweCAxMHB4fQouaWNvbi1idG57d2lkdGg6MzRweDtoZWlnaHQ6MzRweDtiYWNrZ3JvdW5kOnJnYmEoMjAxLDE2OCw3NiwuMTIpO2JvcmRlcjoxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4zKTtjb2xvcjojYzlhODRjO2N1cnNvcjpwb2ludGVyO2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OmNlbnRlcjtmb250LXNpemU6MS4xcmVtO2ZvbnQtd2VpZ2h0OjMwMH0KLmljb24tYnRuOmhvdmVye2JhY2tncm91bmQ6cmdiYSgyMDEsMTY4LDc2LC4yNCl9Ci5jbGllbnQtY2FyZHtiYWNrZ3JvdW5kOiMxNDE0MTA7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNyk7cGFkZGluZzoxNHB4O21hcmdpbi1ib3R0b206NnB4fQouY2wtcm93e2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpmbGV4LXN0YXJ0fQouY2wtYXZhdGFye3dpZHRoOjM2cHg7aGVpZ2h0OjM2cHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDpyZ2JhKDIwMSwxNjgsNzYsLjEyKTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjAxLDE2OCw3NiwuMjUpO2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OmNlbnRlcjtmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZjtmb250LXNpemU6Ljg4cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojYzlhODRjO2ZsZXgtc2hyaW5rOjA7bWFyZ2luLXJpZ2h0OjEwcHh9Ci5jbC1uYW1le2ZvbnQtc2l6ZTouODVyZW07Zm9udC13ZWlnaHQ6NTAwO2NvbG9yOiNlOGUwZDB9Ci5jbC1kZXRhaWx7Zm9udC1zaXplOi42MnJlbTtjb2xvcjojNTU1NTUwO21hcmdpbi10b3A6MnB4fQouY2wtc3RhdHN7ZGlzcGxheTpmbGV4O2dhcDoxNHB4O21hcmdpbi10b3A6MTBweDtwYWRkaW5nLXRvcDoxMHB4O2JvcmRlci10b3A6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA1KX0KLmNzdC12YWx7Zm9udC1mYW1pbHk6J0Nvcm1vcmFudCBHYXJhbW9uZCcsc2VyaWY7Zm9udC1zaXplOjEuMXJlbTtjb2xvcjojYzlhODRjO2ZvbnQtd2VpZ2h0OjYwMH0KLmNzdC1sYWJlbHtmb250LXNpemU6LjUycmVtO2NvbG9yOiM1NTU1NTA7dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2xldHRlci1zcGFjaW5nOi4xZW19Ci5jbC1sYXN0e2ZvbnQtc2l6ZTouNnJlbTtjb2xvcjojNTU1NTUwO21hcmdpbi10b3A6OHB4fQouY2wtbGFzdCBzcGFue2NvbG9yOiM4YThhNTV9Ci5jbC1iYWRnZXtmb250LXNpemU6LjU4cmVtO2NvbG9yOiNjOWE4NGM7YmFja2dyb3VuZDpyZ2JhKDIwMSwxNjgsNzYsLjEpO2JvcmRlcjoxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4yKTtwYWRkaW5nOjNweCA4cHg7d2hpdGUtc3BhY2U6bm93cmFwfQoubG9hZGluZ3t0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjQwcHggMDtjb2xvcjojNDQ0NDQwO2ZvbnQtc2l6ZTouNzVyZW07bGV0dGVyLXNwYWNpbmc6LjEyZW19Ci5zZWN0e21hcmdpbi1ib3R0b206MjJweH0KLmZvcm0tYmxvY2t7YmFja2dyb3VuZDojMTQxNDEwO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDcpO3BhZGRpbmc6MTZweDttYXJnaW4tYm90dG9tOjE2cHh9Ci50YWd7Zm9udC1zaXplOi41OHJlbTtjb2xvcjojOGE4YTU1O2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDUpO3BhZGRpbmc6MnB4IDhweDttYXJnaW4tcmlnaHQ6NHB4O21hcmdpbi1ib3R0b206NHB4O2Rpc3BsYXk6aW5saW5lLWJsb2NrfQpAa2V5ZnJhbWVzIGZ1e2Zyb217b3BhY2l0eTowO3RyYW5zZm9ybTp0cmFuc2xhdGVZKDhweCl9dG97b3BhY2l0eToxO3RyYW5zZm9ybTp0cmFuc2xhdGVZKDApfX0KLmFuaW17YW5pbWF0aW9uOmZ1IC4zcyBlYXNlIGJvdGh9Cjwvc3R5bGU+CjwvaGVhZD4KPGJvZHk+CjxkaXYgY2xhc3M9ImNvbiI+CjxkaXYgY2xhc3M9InBhZ2UiPgoKPGRpdiBjbGFzcz0ibG9nby1yaiI+UiZhbXA7SjwvZGl2Pgo8ZGl2IGNsYXNzPSJsb2dvLXN1YiI+R3Jvb21pbmcgJm1pZGRvdDsg0KHRgtCw0YLQuNGB0YLQuNC60LAgJm1pZGRvdDsg0KLQsNC70LvQuNC9PC9kaXY+Cgo8ZGl2IGNsYXNzPSJ0YWJzLW1haW4iPgogIDxidXR0b24gY2xhc3M9InRtYiBhY3RpdmUiIG9uY2xpY2s9InN3aXRjaE1haW4oJ3N0YXRzJyx0aGlzKSI+0KHRgtCw0YLQuNGB0YLQuNC60LA8L2J1dHRvbj4KICA8YnV0dG9uIGNsYXNzPSJ0bWIiIG9uY2xpY2s9InN3aXRjaE1haW4oJ21nbXQnLHRoaXMpIj7Qo9C/0YDQsNCy0LvQtdC90LjQtTwvYnV0dG9uPgo8L2Rpdj4KCjwhLS0g4pWQ4pWQ4pWQINCh0KLQkNCi0JjQodCi0JjQmtCQIOKVkOKVkOKVkCAtLT4KPGRpdiBjbGFzcz0icGFuZWwgYWN0aXZlIiBpZD0icGFuZWwtc3RhdHMiPgoKICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7Z2FwOjhweDttYXJnaW4tYm90dG9tOjEycHg7bWFyZ2luLXRvcDoyMHB4O2FsaWduLWl0ZW1zOmNlbnRlciI+CiAgICA8c2VsZWN0IGNsYXNzPSJwZXJpb2Qtc2VsZWN0IiBpZD0icGVyaW9kU2VsZWN0IiBvbmNoYW5nZT0ibG9hZFN0YXRzKCkiPgogICAgICA8b3B0aW9uIHZhbHVlPSJtb250aCI+0K3RgtC+0YIg0LzQtdGB0Y/Rhjwvb3B0aW9uPgogICAgICA8b3B0aW9uIHZhbHVlPSJsYXN0X21vbnRoIj7Qn9GA0L7RiNC70YvQuSDQvNC10YHRj9GGPC9vcHRpb24+CiAgICAgIDxvcHRpb24gdmFsdWU9IjNtb250aHMiPjMg0LzQtdGB0Y/RhtCwPC9vcHRpb24+CiAgICAgIDxvcHRpb24gdmFsdWU9ImFsbCI+0JLRgdGRINCy0YDQtdC80Y88L29wdGlvbj4KICAgIDwvc2VsZWN0PgogICAgPGJ1dHRvbiBjbGFzcz0icmVmcmVzaC1idG4iIG9uY2xpY2s9ImxvYWRTdGF0cygpIj4mIzg2MzU7INCe0LHQvdC+0LLQuNGC0Yw8L2J1dHRvbj4KICA8L2Rpdj4KCiAgPGRpdiBjbGFzcz0iZGlzY291bnQtcm93Ij4KICAgIDxkaXYgY2xhc3M9ImRpc2NvdW50LWxhYmVsIj7QodC60LjQtNC60LAg0YHQsNC70L7QvdCwICjQstGL0YfQuNGC0LDQtdGC0YHRjyDRgtC+0LvRjNC60L4g0LjQtyDQtNC+0LvQuCDRgdCw0LvQvtC90LApPC9kaXY+CiAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo0cHgiPgogICAgICA8aW5wdXQgY2xhc3M9ImRpc2NvdW50LWlucHV0IiBpZD0iZGlzY291bnRJbnB1dCIgdHlwZT0ibnVtYmVyIiBtaW49IjAiIHZhbHVlPSIwIiBvbmlucHV0PSJyZWNhbGMoKSI+CiAgICAgIDxzcGFuIGNsYXNzPSJkaXNjb3VudC1ldXIiPuKCrDwvc3Bhbj4KICAgIDwvZGl2PgogIDwvZGl2PgoKICA8ZGl2IGNsYXNzPSJtZXRyaWNzIiBpZD0ibWV0cmljc0Jsb2NrIj4KICAgIDxkaXYgY2xhc3M9Im1ldHJpYyI+PGRpdiBjbGFzcz0ibWV0cmljLWxhYmVsIj7QktGL0YDRg9GH0LrQsDwvZGl2PjxkaXYgY2xhc3M9Im1ldHJpYy12YWwiIGlkPSJtVG90YWwiPuKAlDwvZGl2PjxkaXYgY2xhc3M9Im1ldHJpYy1zdWIiPtGB0YPQvNC80LAg0YPRgdC70YPQszwvZGl2PjwvZGl2PgogICAgPGRpdiBjbGFzcz0ibWV0cmljIj48ZGl2IGNsYXNzPSJtZXRyaWMtbGFiZWwiPtCc0LDRgdGC0LXRgNCw0Lw8L2Rpdj48ZGl2IGNsYXNzPSJtZXRyaWMtdmFsIiBpZD0ibU1hc3RlcnMiPuKAlDwvZGl2PjxkaXYgY2xhc3M9Im1ldHJpYy1zdWIiPjUwJSDQvtGCINGD0YHQu9GD0LM8L2Rpdj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im1ldHJpYyI+PGRpdiBjbGFzcz0ibWV0cmljLWxhYmVsIj7QodCw0LvQvtC90YM8L2Rpdj48ZGl2IGNsYXNzPSJtZXRyaWMtdmFsIiBpZD0ibVNhbG9uIj7igJQ8L2Rpdj48ZGl2IGNsYXNzPSJtZXRyaWMtc3ViIj41MCUg4oiSINGB0LrQuNC00LrQsDwvZGl2PjwvZGl2PgogIDwvZGl2PgogIDxkaXYgY2xhc3M9Im1ldHJpY3MiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjAiPgogICAgPGRpdiBjbGFzcz0ibWV0cmljIj48ZGl2IGNsYXNzPSJtZXRyaWMtbGFiZWwiPtCX0LDQv9C40YHQtdC5PC9kaXY+PGRpdiBjbGFzcz0ibWV0cmljLXZhbCIgaWQ9Im1Db3VudCI+4oCUPC9kaXY+PGRpdiBjbGFzcz0ibWV0cmljLXN1YiI+0LfQsCDQv9C10YDQuNC+0LQ8L2Rpdj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im1ldHJpYyI+PGRpdiBjbGFzcz0ibWV0cmljLWxhYmVsIj7QmtC70LjQtdC90YLQvtCyPC9kaXY+PGRpdiBjbGFzcz0ibWV0cmljLXZhbCIgaWQ9Im1DbGllbnRzIj7igJQ8L2Rpdj48ZGl2IGNsYXNzPSJtZXRyaWMtc3ViIj7Rg9C90LjQutCw0LvRjNC90YvRhTwvZGl2PjwvZGl2PgogICAgPGRpdiBjbGFzcz0ibWV0cmljIj48ZGl2IGNsYXNzPSJtZXRyaWMtbGFiZWwiPtCh0YAuINGH0LXQujwvZGl2PjxkaXYgY2xhc3M9Im1ldHJpYy12YWwiIGlkPSJtQXZnIj7igJQ8L2Rpdj48ZGl2IGNsYXNzPSJtZXRyaWMtc3ViIj7Qt9CwINGD0YHQu9GD0LPRgzwvZGl2PjwvZGl2PgogIDwvZGl2PgoKICA8ZGl2IHN0eWxlPSJtYXJnaW4tdG9wOjIycHgiPgogICAgPGRpdiBjbGFzcz0ic2xibCI+0J/QviDQvNCw0YHRgtC10YDQsNC8PC9kaXY+CiAgICA8ZGl2IGlkPSJtYXN0ZXJzTGlzdCI+PGRpdiBjbGFzcz0ibG9hZGluZyI+0JfQsNCz0YDRg9C30LrQsCDQtNCw0L3QvdGL0YUuLi48L2Rpdj48L2Rpdj4KICA8L2Rpdj4KPC9kaXY+Cgo8IS0tIOKVkOKVkOKVkCDQo9Cf0KDQkNCS0JvQldCd0JjQlSDilZDilZDilZAgLS0+CjxkaXYgY2xhc3M9InBhbmVsIiBpZD0icGFuZWwtbWdtdCI+CiAgPGRpdiBzdHlsZT0ibWFyZ2luLXRvcDoyMHB4IiBjbGFzcz0ic3ViLXRhYnMiPgogICAgPGJ1dHRvbiBjbGFzcz0ic3RiIGFjdGl2ZSIgb25jbGljaz0ic3dpdGNoU3ViKCdtYXN0ZXJzJyx0aGlzKSI+0JzQsNGB0YLQtdGA0LA8L2J1dHRvbj4KICAgIDxidXR0b24gY2xhc3M9InN0YiIgb25jbGljaz0ic3dpdGNoU3ViKCdicmVlZHMnLHRoaXMpIj7Qn9C+0YDQvtC00Ys8L2J1dHRvbj4KICAgIDxidXR0b24gY2xhc3M9InN0YiIgb25jbGljaz0ic3dpdGNoU3ViKCdjbGllbnRzJyx0aGlzKSI+0JrQu9C40LXQvdGC0Ys8L2J1dHRvbj4KICA8L2Rpdj4KCiAgPCEtLSDQnNCw0YHRgtC10YDQsCAtLT4KICA8ZGl2IGNsYXNzPSJzdWItcGFuZWwgYWN0aXZlIiBpZD0ic3ViLW1hc3RlcnMiPgogICAgPGRpdiBjbGFzcz0ic2VjdCI+CiAgICAgIDxkaXYgY2xhc3M9InNsYmwiPtCU0L7QsdCw0LLQuNGC0Ywg0LzQsNGB0YLQtdGA0LA8L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0iZm9ybS1ibG9jayI+CiAgICAgICAgPGRpdiBjbGFzcz0iZmktcm93Ij4KICAgICAgICAgIDxpbnB1dCBjbGFzcz0iZmkiIGlkPSJuZXdNYXN0ZXJOYW1lIiBwbGFjZWhvbGRlcj0i0JjQvNGPINC80LDRgdGC0LXRgNCwIj4KICAgICAgICAgIDxpbnB1dCBjbGFzcz0iZmkiIGlkPSJuZXdNYXN0ZXJQaG9uZSIgcGxhY2Vob2xkZXI9ItCi0LXQu9C10YTQvtC9ICjQvdC10L7QsdGP0LcuKSI+CiAgICAgICAgPC9kaXY+CiAgICAgICAgPGJ1dHRvbiBjbGFzcz0iY2J0biIgb25jbGljaz0iYWRkTWFzdGVyKCkiPisg0JTQvtCx0LDQstC40YLRjDwvYnV0dG9uPgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0ic2VjdCI+CiAgICAgIDxkaXYgY2xhc3M9InNsYmwiPtCc0LDRgdGC0LXRgNCwINGB0LDQu9C+0L3QsDwvZGl2PgogICAgICA8ZGl2IGlkPSJtYXN0ZXJMaXN0VUkiPjwvZGl2PgogICAgPC9kaXY+CiAgPC9kaXY+CgogIDwhLS0g0J/QvtGA0L7QtNGLINC4INGD0YHQu9GD0LPQuCAtLT4KICA8ZGl2IGNsYXNzPSJzdWItcGFuZWwiIGlkPSJzdWItYnJlZWRzIj4KICAgIDxkaXYgY2xhc3M9InNlY3QiPgogICAgICA8ZGl2IGNsYXNzPSJzbGJsIj7QlNC+0LHQsNCy0LjRgtGMINC/0L7RgNC+0LTRgzwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJmb3JtLWJsb2NrIj4KICAgICAgICA8aW5wdXQgY2xhc3M9ImZpIiBpZD0ibmV3QnJlZWROYW1lIiBwbGFjZWhvbGRlcj0i0J3QsNC30LLQsNC90LjQtSDQv9C+0YDQvtC00YsgKNC90LDQv9GALiDQpdCw0YHQutC4IDIw4oCTMzAg0LrQsykiPgogICAgICAgIDxidXR0b24gY2xhc3M9ImNidG4iIG9uY2xpY2s9ImFkZEJyZWVkKCkiPisg0JTQvtCx0LDQstC40YLRjCDQv9C+0YDQvtC00YM8L2J1dHRvbj4KICAgICAgPC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InNlY3QiPgogICAgICA8ZGl2IGNsYXNzPSJzbGJsIj7Qn9C+0YDQvtC00Ysg0Lgg0YPRgdC70YPQs9C4PC9kaXY+CiAgICAgIDxkaXYgaWQ9ImJyZWVkTGlzdFVJIj48L2Rpdj4KICAgIDwvZGl2PgogIDwvZGl2PgoKICA8IS0tINCa0LvQuNC10L3RgtGLIC0tPgogIDxkaXYgY2xhc3M9InN1Yi1wYW5lbCIgaWQ9InN1Yi1jbGllbnRzIj4KICAgIDxkaXYgY2xhc3M9InNlY3QiPgogICAgICA8ZGl2IGNsYXNzPSJzbGJsIj7QndC+0LLQsNGPINC60LDRgNGC0L7Rh9C60LAg0LrQu9C40LXQvdGC0LA8L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0iZm9ybS1ibG9jayI+CiAgICAgICAgPGRpdiBjbGFzcz0iZmktcm93Ij4KICAgICAgICAgIDxpbnB1dCBjbGFzcz0iZmkiIGlkPSJuZXdDbGllbnROYW1lIiBwbGFjZWhvbGRlcj0i0JjQvNGPINC60LvQuNC10L3RgtCwIj4KICAgICAgICAgIDxpbnB1dCBjbGFzcz0iZmkiIGlkPSJuZXdDbGllbnRQaG9uZSIgcGxhY2Vob2xkZXI9IiszNzIgLi4uIj4KICAgICAgICA8L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJmaS1yb3ciPgogICAgICAgICAgPGlucHV0IGNsYXNzPSJmaSIgaWQ9Im5ld0NsaWVudFBldCIgcGxhY2Vob2xkZXI9ItCa0LvQuNGH0LrQsCDQv9C40YLQvtC80YbQsCI+CiAgICAgICAgICA8c2VsZWN0IGNsYXNzPSJmaSIgaWQ9Im5ld0NsaWVudEJyZWVkIj48b3B0aW9uIHZhbHVlPSIiPtCf0L7RgNC+0LTQsC4uLjwvb3B0aW9uPjwvc2VsZWN0PgogICAgICAgIDwvZGl2PgogICAgICAgIDxpbnB1dCBjbGFzcz0iZmkiIGlkPSJuZXdDbGllbnROb3RlIiBwbGFjZWhvbGRlcj0i0JrQvtC80LzQtdC90YLQsNGA0LjQuSAo0LDQu9C70LXRgNCz0LjQuCwg0L7RgdC+0LHQtdC90L3QvtGB0YLQuC4uLikiPgogICAgICAgIDxidXR0b24gY2xhc3M9ImNidG4iIG9uY2xpY2s9ImFkZENsaWVudCgpIj4rINCh0L7Qt9C00LDRgtGMINC60LDRgNGC0L7Rh9C60YM8L2J1dHRvbj4KICAgICAgPC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InNlY3QiPgogICAgICA8ZGl2IGNsYXNzPSJzbGJsIj7QkdCw0LfQsCDQutC70LjQtdC90YLQvtCyPC9kaXY+CiAgICAgIDxkaXYgaWQ9ImNsaWVudExpc3RVSSI+PC9kaXY+CiAgICA8L2Rpdj4KICA8L2Rpdj4KPC9kaXY+Cgo8L2Rpdj4KPC9kaXY+Cgo8c2NyaXB0Pgp2YXIgR09PR0xFX1NDUklQVCA9IHdpbmRvdy5sb2NhdGlvbi5vcmlnaW4gKyAnL2FwaS9zdGF0cyc7CnZhciBkYiA9IHsKICBtYXN0ZXJzOiBKU09OLnBhcnNlKGxvY2FsU3RvcmFnZS5nZXRJdGVtKCdyal9tYXN0ZXJzJykgfHwgJ1si0KLQsNGC0YzRj9C90LAiLCLQkNC70LjRgdCwIiwi0JrRgNC40YHRgtC40L3QsCIsItCQ0L3QvdCwIl0nKSwKICBicmVlZHM6ICBKU09OLnBhcnNlKGxvY2FsU3RvcmFnZS5nZXRJdGVtKCdyal9icmVlZHMnKSAgfHwgJ1tdJyksCiAgY2xpZW50czogSlNPTi5wYXJzZShsb2NhbFN0b3JhZ2UuZ2V0SXRlbSgncmpfY2xpZW50cycpIHx8ICdbXScpLAogIGRpc2NvdW50OiBwYXJzZUZsb2F0KGxvY2FsU3RvcmFnZS5nZXRJdGVtKCdyal9kaXNjb3VudCcpIHx8ICcwJykKfTsKdmFyIGFsbEJvb2tpbmdzID0gW107CnZhciBzdGF0c0xvYWRlZCA9IGZhbHNlOwoKZnVuY3Rpb24gc2F2ZSgpIHsKICBsb2NhbFN0b3JhZ2Uuc2V0SXRlbSgncmpfbWFzdGVycycsICBKU09OLnN0cmluZ2lmeShkYi5tYXN0ZXJzKSk7CiAgbG9jYWxTdG9yYWdlLnNldEl0ZW0oJ3JqX2JyZWVkcycsICAgSlNPTi5zdHJpbmdpZnkoZGIuYnJlZWRzKSk7CiAgbG9jYWxTdG9yYWdlLnNldEl0ZW0oJ3JqX2NsaWVudHMnLCAgSlNPTi5zdHJpbmdpZnkoZGIuY2xpZW50cykpOwogIGxvY2FsU3RvcmFnZS5zZXRJdGVtKCdyal9kaXNjb3VudCcsIGRiLmRpc2NvdW50KTsKfQoKLy8g4pSA4pSAINCd0JDQktCY0JPQkNCm0JjQryDilIDilIAKZnVuY3Rpb24gc3dpdGNoTWFpbihuYW1lLCBidG4pIHsKICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcudG1iJykuZm9yRWFjaChmdW5jdGlvbih0KXt0LmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpfSk7CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLnBhbmVsJykuZm9yRWFjaChmdW5jdGlvbihwKXtwLmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpfSk7CiAgYnRuLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdwYW5lbC0nK25hbWUpLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpOwogIGlmIChuYW1lID09PSAnc3RhdHMnICYmICFzdGF0c0xvYWRlZCkgbG9hZFN0YXRzKCk7Cn0KZnVuY3Rpb24gc3dpdGNoU3ViKG5hbWUsIGJ0bikgewogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5zdGInKS5mb3JFYWNoKGZ1bmN0aW9uKHQpe3QuY2xhc3NMaXN0LnJlbW92ZSgnYWN0aXZlJyl9KTsKICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuc3ViLXBhbmVsJykuZm9yRWFjaChmdW5jdGlvbihwKXtwLmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpfSk7CiAgYnRuLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdWItJytuYW1lKS5jbGFzc0xpc3QuYWRkKCdhY3RpdmUnKTsKfQoKLy8g4pSA4pSAINCh0KLQkNCi0JjQodCi0JjQmtCQIOKUgOKUgApmdW5jdGlvbiBnZXRQZXJpb2RQYXJhbXMoKSB7CiAgdmFyIHZhbCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdwZXJpb2RTZWxlY3QnKS52YWx1ZTsKICB2YXIgbm93ID0gbmV3IERhdGUoKTsKICB2YXIgZnJvbSwgdG87CiAgaWYgKHZhbCA9PT0gJ21vbnRoJykgewogICAgZnJvbSA9IG5ldyBEYXRlKG5vdy5nZXRGdWxsWWVhcigpLCBub3cuZ2V0TW9udGgoKSwgMSk7CiAgICB0byAgID0gbmV3IERhdGUobm93LmdldEZ1bGxZZWFyKCksIG5vdy5nZXRNb250aCgpKzEsIDApOwogIH0gZWxzZSBpZiAodmFsID09PSAnbGFzdF9tb250aCcpIHsKICAgIGZyb20gPSBuZXcgRGF0ZShub3cuZ2V0RnVsbFllYXIoKSwgbm93LmdldE1vbnRoKCktMSwgMSk7CiAgICB0byAgID0gbmV3IERhdGUobm93LmdldEZ1bGxZZWFyKCksIG5vdy5nZXRNb250aCgpLCAwKTsKICB9IGVsc2UgaWYgKHZhbCA9PT0gJzNtb250aHMnKSB7CiAgICBmcm9tID0gbmV3IERhdGUobm93LmdldEZ1bGxZZWFyKCksIG5vdy5nZXRNb250aCgpLTIsIDEpOwogICAgdG8gICA9IG5ldyBEYXRlKG5vdy5nZXRGdWxsWWVhcigpLCBub3cuZ2V0TW9udGgoKSsxLCAwKTsKICB9IGVsc2UgewogICAgZnJvbSA9IG5ldyBEYXRlKDIwMjQsIDAsIDEpOwogICAgdG8gICA9IG5ldyBEYXRlKG5vdy5nZXRGdWxsWWVhcigpLCBub3cuZ2V0TW9udGgoKSsxLCAwKTsKICB9CiAgdmFyIGZtdCA9IGZ1bmN0aW9uKGQpIHsgcmV0dXJuIFN0cmluZyhkLmdldERhdGUoKSkucGFkU3RhcnQoMiwnMCcpKycuJytTdHJpbmcoZC5nZXRNb250aCgpKzEpLnBhZFN0YXJ0KDIsJzAnKSsnLicrZC5nZXRGdWxsWWVhcigpOyB9OwogIHJldHVybiB7ZnJvbTogZm10KGZyb20pLCB0bzogZm10KHRvKX07Cn0KCmZ1bmN0aW9uIGxvYWRTdGF0cygpIHsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbWFzdGVyc0xpc3QnKS5pbm5lckhUTUwgPSAnPGRpdiBjbGFzcz0ibG9hZGluZyI+0JfQsNCz0YDRg9C30LrQsCDQtNCw0L3QvdGL0YUg0LjQtyDQutCw0LvQtdC90LTQsNGA0Y8uLi48L2Rpdj4nOwogIHZhciBwID0gZ2V0UGVyaW9kUGFyYW1zKCk7CiAgZmV0Y2god2luZG93LmxvY2F0aW9uLm9yaWdpbiArICcvYXBpL3N0YXRzP2Zyb209JyArIHAuZnJvbSArICcmdG89JyArIHAudG8pCiAgICAudGhlbihmdW5jdGlvbihyKXtyZXR1cm4gci5qc29uKCk7fSkKICAgIC50aGVuKGZ1bmN0aW9uKGRhdGEpewogICAgICBpZiAoZGF0YS5zdWNjZXNzKSB7CiAgICAgICAgYWxsQm9va2luZ3MgPSBkYXRhLmJvb2tpbmdzIHx8IFtdOwogICAgICAgIHN0YXRzTG9hZGVkID0gdHJ1ZTsKICAgICAgICByZWNhbGMoKTsKICAgICAgfSBlbHNlIHsKICAgICAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbWFzdGVyc0xpc3QnKS5pbm5lckhUTUwgPSAnPGRpdiBjbGFzcz0ibG9hZGluZyI+0J7RiNC40LHQutCwOiAnICsgKGRhdGEuZXJyb3J8fCfQvdC10YIg0LTQsNC90L3Ri9GFJykgKyAnPC9kaXY+JzsKICAgICAgfQogICAgfSkKICAgIC5jYXRjaChmdW5jdGlvbihlKXsKICAgICAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ21hc3RlcnNMaXN0JykuaW5uZXJIVE1MID0gJzxkaXYgY2xhc3M9ImxvYWRpbmciPtCd0LXRgiDRgdC+0LXQtNC40L3QtdC90LjRjyDRgSDQutCw0LvQtdC90LTQsNGA0ZHQvDwvZGl2Pic7CiAgICB9KTsKfQoKZnVuY3Rpb24gcmVjYWxjKCkgewogIHZhciBkaXNjb3VudCA9IHBhcnNlRmxvYXQoZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Rpc2NvdW50SW5wdXQnKS52YWx1ZSkgfHwgMDsKICBkYi5kaXNjb3VudCA9IGRpc2NvdW50OwogIHNhdmUoKTsKCiAgdmFyIHRvdGFsID0gMCwgY291bnQgPSAwOwogIHZhciBtYXN0ZXJNYXAgPSB7fTsKICB2YXIgcGhvbmVzID0ge307CgogIGFsbEJvb2tpbmdzLmZvckVhY2goZnVuY3Rpb24oYikgewogICAgdmFyIHByaWNlID0gcGFyc2VGbG9hdChiLnByaWNlKSB8fCAwOwogICAgdG90YWwgKz0gcHJpY2U7CiAgICBjb3VudCsrOwogICAgaWYgKGIuY2xpZW50UGhvbmUpIHBob25lc1tiLmNsaWVudFBob25lXSA9IHRydWU7CiAgICB2YXIgbSA9IGIubWFzdGVyOwogICAgaWYgKCFtYXN0ZXJNYXBbbV0pIG1hc3Rlck1hcFttXSA9IHtib29raW5nczpbXSwgdG90YWw6MH07CiAgICBtYXN0ZXJNYXBbbV0uYm9va2luZ3MucHVzaChiKTsKICAgIG1hc3Rlck1hcFttXS50b3RhbCArPSBwcmljZTsKICB9KTsKCiAgdmFyIG1hc3RlclRvdGFsID0gTWF0aC5yb3VuZCh0b3RhbCAvIDIpOwogIHZhciBzYWxvblRvdGFsICA9IE1hdGgucm91bmQodG90YWwgLyAyIC0gZGlzY291bnQpOwogIHZhciBhdmcgPSBjb3VudCA+IDAgPyBNYXRoLnJvdW5kKHRvdGFsIC8gY291bnQpIDogMDsKICB2YXIgdW5pcXVlQ2xpZW50cyA9IE9iamVjdC5rZXlzKHBob25lcykubGVuZ3RoOwoKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbVRvdGFsJykudGV4dENvbnRlbnQgICA9IHRvdGFsICsgJyDigqwnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdtTWFzdGVycycpLnRleHRDb250ZW50ID0gbWFzdGVyVG90YWwgKyAnIOKCrCc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ21TYWxvbicpLnRleHRDb250ZW50ICAgPSBzYWxvblRvdGFsICsgJyDigqwnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdtQ291bnQnKS50ZXh0Q29udGVudCAgID0gY291bnQ7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ21DbGllbnRzJykudGV4dENvbnRlbnQgPSB1bmlxdWVDbGllbnRzOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdtQXZnJykudGV4dENvbnRlbnQgICAgID0gYXZnICsgJyDigqwnOwoKICByZW5kZXJNYXN0ZXJzKG1hc3Rlck1hcCwgZGlzY291bnQsIHRvdGFsKTsKfQoKZnVuY3Rpb24gcmVuZGVyTWFzdGVycyhtYXN0ZXJNYXAsIGRpc2NvdW50LCB0b3RhbEFsbCkgewogIHZhciBlbCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdtYXN0ZXJzTGlzdCcpOwogIGlmIChPYmplY3Qua2V5cyhtYXN0ZXJNYXApLmxlbmd0aCA9PT0gMCkgewogICAgZWwuaW5uZXJIVE1MID0gJzxkaXYgY2xhc3M9ImxvYWRpbmciPtCd0LXRgiDQt9Cw0L/QuNGB0LXQuSDQt9CwINCy0YvQsdGA0LDQvdC90YvQuSDQv9C10YDQuNC+0LQ8L2Rpdj4nOwogICAgcmV0dXJuOwogIH0KICB2YXIgaHRtbCA9ICcnOwogIHZhciBtYXN0ZXJzID0gT2JqZWN0LmtleXMobWFzdGVyTWFwKS5zb3J0KGZ1bmN0aW9uKGEsYil7IHJldHVybiBtYXN0ZXJNYXBbYl0udG90YWwgLSBtYXN0ZXJNYXBbYV0udG90YWw7IH0pOwogIG1hc3RlcnMuZm9yRWFjaChmdW5jdGlvbihuYW1lKSB7CiAgICB2YXIgZCA9IG1hc3Rlck1hcFtuYW1lXTsKICAgIHZhciBtYXN0ZXJFYXJuID0gTWF0aC5yb3VuZChkLnRvdGFsIC8gMik7CiAgICB2YXIgc2Fsb25TaGFyZSA9IE1hdGgucm91bmQoZC50b3RhbCAvIDIpOwogICAgdmFyIHJhdGlvID0gdG90YWxBbGwgPiAwID8gZC50b3RhbCAvIHRvdGFsQWxsIDogMDsKICAgIHZhciBzYWxvbkRpc2NvdW50ID0gTWF0aC5yb3VuZChkaXNjb3VudCAqIHJhdGlvKTsKICAgIHZhciBzYWxvbkVhcm4gPSBzYWxvblNoYXJlIC0gc2Fsb25EaXNjb3VudDsKICAgIHZhciBpZCA9ICdtY18nICsgbmFtZS5yZXBsYWNlKC9ccy9nLCdfJyk7CiAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJtYXN0ZXItY2FyZCBhbmltIj4nOwogICAgaHRtbCArPSAnPGRpdiBjbGFzcz0ibWFzdGVyLWhlYWQiIGlkPSJtaF8nK2lkKyciIG9uY2xpY2s9InRvZ2dsZU1hc3RlcihcJycgKyBpZCArICdcJykiPic7CiAgICBodG1sICs9ICc8ZGl2PjxkaXYgY2xhc3M9Im1uYW1lIj4nICsgbmFtZSArICc8L2Rpdj4nOwogICAgaHRtbCArPSAnPGRpdiBjbGFzcz0ibWNvdW50Ij4nICsgZC5ib29raW5ncy5sZW5ndGggKyAnINC30LDQv9C40YHQtdC5IMK3ICcgKyBkLnRvdGFsICsgJyDigqwg0L7QsdGJ0LDRjyDRgdGD0LzQvNCwPC9kaXY+PC9kaXY+JzsKICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9Im1lYXJuaW5ncyI+JzsKICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9ImVhcm4taXRlbSI+PGRpdiBjbGFzcz0iZWFybi1sYWJlbCI+0JzQsNGB0YLQtdGAPC9kaXY+PGRpdiBjbGFzcz0iZWFybi12YWwiPicgKyBtYXN0ZXJFYXJuICsgJyDigqw8L2Rpdj48L2Rpdj4nOwogICAgaHRtbCArPSAnPGRpdiBjbGFzcz0iZWFybi1pdGVtIj48ZGl2IGNsYXNzPSJlYXJuLWxhYmVsIj7QodCw0LvQvtC9PC9kaXY+PGRpdiBjbGFzcz0iZWFybi12YWwgc2Fsb24iPicgKyBzYWxvbkVhcm4gKyAnIOKCrDwvZGl2PjwvZGl2Pic7CiAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJjaGV2cm9uIj7ilrw8L2Rpdj48L2Rpdj48L2Rpdj4nOwogICAgaHRtbCArPSAnPGRpdiBjbGFzcz0ibWFzdGVyLWJvZHkiIGlkPSJtYl8nK2lkKyciPic7CiAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJ2aXNpdHMtaGVhZGVyIj48c3Bhbj7QlNCw0YLQsDwvc3Bhbj48c3Bhbj7QmtC70LjQtdC90YIgLyDQn9C40YLQvtC80LXRhjwvc3Bhbj48c3Bhbj7Qo9GB0LvRg9Cz0LA8L3NwYW4+PHNwYW4gc3R5bGU9InRleHQtYWxpZ246cmlnaHQiPtCm0LXQvdCwPC9zcGFuPjwvZGl2Pic7CiAgICB2YXIgc29ydGVkID0gZC5ib29raW5ncy5zbGljZSgpLnNvcnQoZnVuY3Rpb24oYSxiKXsgcmV0dXJuIGIuZGF0ZS5sb2NhbGVDb21wYXJlKGEuZGF0ZSk7IH0pOwogICAgc29ydGVkLmZvckVhY2goZnVuY3Rpb24oYikgewogICAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJ2aXNpdC1yb3ciPic7CiAgICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9InZpc2l0LWRhdGUiPicgKyBiLmRhdGUgKyAnPC9kaXY+JzsKICAgICAgaHRtbCArPSAnPGRpdj48ZGl2IGNsYXNzPSJ2aXNpdC1jbGllbnQiPicgKyAoYi5jbGllbnROYW1lfHwn4oCUJykgKyAnPC9kaXY+JzsKICAgICAgaHRtbCArPSAnPGRpdiBjbGFzcz0idmlzaXQtcGV0Ij4nICsgKGIucGV0TmFtZSA/IGIucGV0TmFtZSArICcgwrcgJyA6ICcnKSArIGIuYnJlZWQgKyAnPC9kaXY+PC9kaXY+JzsKICAgICAgaHRtbCArPSAnPGRpdiBjbGFzcz0idmlzaXQtc3ZjIj4nICsgYi5zZXJ2aWNlICsgJzwvZGl2Pic7CiAgICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9InZpc2l0LXByaWNlIj4nICsgKGIucHJpY2V8fDApICsgJyDigqw8L2Rpdj48L2Rpdj4nOwogICAgfSk7CiAgICBodG1sICs9ICc8L2Rpdj48L2Rpdj4nOwogIH0pOwogIGVsLmlubmVySFRNTCA9IGh0bWw7Cn0KCmZ1bmN0aW9uIHRvZ2dsZU1hc3RlcihpZCkgewogIHZhciBoZWFkID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ21oXycraWQpOwogIHZhciBib2R5ID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ21iXycraWQpOwogIGhlYWQuY2xhc3NMaXN0LnRvZ2dsZSgnb3BlbicpOwogIGJvZHkuY2xhc3NMaXN0LnRvZ2dsZSgnb3BlbicpOwp9CgovLyDilIDilIAg0KPQn9Cg0JDQktCb0JXQndCY0JU6INCc0JDQodCi0JXQoNCQIOKUgOKUgApmdW5jdGlvbiByZW5kZXJNYXN0ZXJMaXN0KCkgewogIHZhciBlbCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdtYXN0ZXJMaXN0VUknKTsKICBpZiAoIWVsKSByZXR1cm47CiAgdmFyIGh0bWwgPSAnJzsKICBkYi5tYXN0ZXJzLmZvckVhY2goZnVuY3Rpb24obmFtZSwgaSkgewogICAgaHRtbCArPSAnPGRpdiBjbGFzcz0ibGlzdC1pdGVtIj48ZGl2PjxkaXYgY2xhc3M9ImxpLW5hbWUiPicrbmFtZSsnPC9kaXY+PC9kaXY+JzsKICAgIGh0bWwgKz0gJzxidXR0b24gY2xhc3M9ImRlbC1idG4iIG9uY2xpY2s9ImRlbE1hc3RlcignK2krJykiPuKclTwvYnV0dG9uPjwvZGl2Pic7CiAgfSk7CiAgZWwuaW5uZXJIVE1MID0gaHRtbCB8fCAnPGRpdiBzdHlsZT0iY29sb3I6IzQ0NDQ0MDtmb250LXNpemU6Ljc1cmVtO3BhZGRpbmc6MTJweCAwIj7QndC10YIg0LzQsNGB0YLQtdGA0L7QsjwvZGl2Pic7Cn0KZnVuY3Rpb24gYWRkTWFzdGVyKCkgewogIHZhciBuYW1lID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ25ld01hc3Rlck5hbWUnKS52YWx1ZS50cmltKCk7CiAgaWYgKCFuYW1lKSByZXR1cm47CiAgZGIubWFzdGVycy5wdXNoKG5hbWUpOwogIHNhdmUoKTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbmV3TWFzdGVyTmFtZScpLnZhbHVlID0gJyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ25ld01hc3RlclBob25lJykudmFsdWUgPSAnJzsKICByZW5kZXJNYXN0ZXJMaXN0KCk7Cn0KZnVuY3Rpb24gZGVsTWFzdGVyKGkpIHsKICBpZiAoIWNvbmZpcm0oJ9Cj0LTQsNC70LjRgtGMINC80LDRgdGC0LXRgNCwPycpKSByZXR1cm47CiAgZGIubWFzdGVycy5zcGxpY2UoaSwgMSk7CiAgc2F2ZSgpOwogIHJlbmRlck1hc3Rlckxpc3QoKTsKfQoKLy8g4pSA4pSAINCj0J/QoNCQ0JLQm9CV0J3QmNCVOiDQn9Ce0KDQntCU0Ksg4pSA4pSACmZ1bmN0aW9uIHJlbmRlckJyZWVkTGlzdCgpIHsKICB2YXIgZWwgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnYnJlZWRMaXN0VUknKTsKICBpZiAoIWVsKSByZXR1cm47CiAgdmFyIGh0bWwgPSAnJzsKICBkYi5icmVlZHMuZm9yRWFjaChmdW5jdGlvbihicmVlZCwgYmkpIHsKICAgIHZhciBiaWQgPSAnYnJfJytiaTsKICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9ImJyZWVkLWNhcmQiPic7CiAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJicmVlZC1oZWFkIiBvbmNsaWNrPSJ0b2dnbGVCcmVlZChcJycgKyBiaWQgKyAnXCcpIj4nOwogICAgaHRtbCArPSAnPGRpdj48ZGl2IGNsYXNzPSJicmVlZC1uYW1lIj4nK2JyZWVkLm5hbWUrJzwvZGl2Pic7CiAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJicmVlZC1jb3VudCI+JysoYnJlZWQuc2VydmljZXN8fFtdKS5sZW5ndGgrJyDRg9GB0LvRg9CzPC9kaXY+PC9kaXY+JzsKICAgIGh0bWwgKz0gJzxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEwcHgiPic7CiAgICBodG1sICs9ICc8YnV0dG9uIGNsYXNzPSJkZWwtYnRuIiBvbmNsaWNrPSJldmVudC5zdG9wUHJvcGFnYXRpb24oKTtkZWxCcmVlZCgnK2JpKycpIj7inJU8L2J1dHRvbj4nOwogICAgaHRtbCArPSAnPHNwYW4gc3R5bGU9ImNvbG9yOiM4YThhNTU7Zm9udC1zaXplOi43NXJlbSI+4pa8PC9zcGFuPjwvZGl2PjwvZGl2Pic7CiAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJicmVlZC1ib2R5IiBpZD0iJytiaWQrJyI+JzsKICAgIChicmVlZC5zZXJ2aWNlc3x8W10pLmZvckVhY2goZnVuY3Rpb24oc3ZjLCBzaSkgewogICAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJzdmMtcm93Ij48c3BhbiBjbGFzcz0ic3ZjLW5hbWUiPicrc3ZjLm5hbWUrJzwvc3Bhbj4nOwogICAgICBodG1sICs9ICc8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxMHB4Ij4nOwogICAgICBodG1sICs9ICc8c3BhbiBjbGFzcz0ic3ZjLXByaWNlIj4nK3N2Yy5wcmljZSsnIOKCrDwvc3Bhbj4nOwogICAgICBodG1sICs9ICc8YnV0dG9uIGNsYXNzPSJkZWwtYnRuIiBvbmNsaWNrPSJkZWxTdmMoJytiaSsnLCcrc2krJykiPuKclTwvYnV0dG9uPjwvZGl2PjwvZGl2Pic7CiAgICB9KTsKICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9ImFkZC1zdmMtcm93Ij4nOwogICAgaHRtbCArPSAnPGlucHV0IGNsYXNzPSJmaSIgaWQ9InNuXycrYmkrJyIgcGxhY2Vob2xkZXI9ItCj0YHQu9GD0LPQsCI+JzsKICAgIGh0bWwgKz0gJzxpbnB1dCBjbGFzcz0iZmkiIGlkPSJzcF8nK2JpKyciIHBsYWNlaG9sZGVyPSLQptC10L3QsCDigqwiPic7CiAgICBodG1sICs9ICc8YnV0dG9uIGNsYXNzPSJpY29uLWJ0biIgb25jbGljaz0iYWRkU3ZjKCcrYmkrJykiPis8L2J1dHRvbj48L2Rpdj4nOwogICAgaHRtbCArPSAnPC9kaXY+PC9kaXY+JzsKICB9KTsKICBlbC5pbm5lckhUTUwgPSBodG1sIHx8ICc8ZGl2IHN0eWxlPSJjb2xvcjojNDQ0NDQwO2ZvbnQtc2l6ZTouNzVyZW07cGFkZGluZzoxMnB4IDAiPtCd0LXRgiDQv9C+0YDQvtC0PC9kaXY+JzsKICByZW5kZXJCcmVlZFNlbGVjdCgpOwp9CmZ1bmN0aW9uIHRvZ2dsZUJyZWVkKGlkKSB7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoaWQpLmNsYXNzTGlzdC50b2dnbGUoJ29wZW4nKTsKfQpmdW5jdGlvbiBhZGRCcmVlZCgpIHsKICB2YXIgbmFtZSA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCduZXdCcmVlZE5hbWUnKS52YWx1ZS50cmltKCk7CiAgaWYgKCFuYW1lKSByZXR1cm47CiAgZGIuYnJlZWRzLnB1c2goe25hbWU6IG5hbWUsIHNlcnZpY2VzOiBbXX0pOwogIHNhdmUoKTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbmV3QnJlZWROYW1lJykudmFsdWUgPSAnJzsKICByZW5kZXJCcmVlZExpc3QoKTsKfQpmdW5jdGlvbiBkZWxCcmVlZChpKSB7CiAgaWYgKCFjb25maXJtKCfQo9C00LDQu9C40YLRjCDQv9C+0YDQvtC00YMg0Lgg0LLRgdC1INC10ZEg0YPRgdC70YPQs9C4PycpKSByZXR1cm47CiAgZGIuYnJlZWRzLnNwbGljZShpLCAxKTsKICBzYXZlKCk7CiAgcmVuZGVyQnJlZWRMaXN0KCk7Cn0KZnVuY3Rpb24gYWRkU3ZjKGJpKSB7CiAgdmFyIG5hbWUgID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3NuXycrYmkpLnZhbHVlLnRyaW0oKTsKICB2YXIgcHJpY2UgPSBwYXJzZUZsb2F0KGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzcF8nK2JpKS52YWx1ZSkgfHwgMDsKICBpZiAoIW5hbWUgfHwgIXByaWNlKSByZXR1cm47CiAgZGIuYnJlZWRzW2JpXS5zZXJ2aWNlcy5wdXNoKHtuYW1lOiBuYW1lLCBwcmljZTogcHJpY2V9KTsKICBzYXZlKCk7CiAgcmVuZGVyQnJlZWRMaXN0KCk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2JyXycrYmkpLmNsYXNzTGlzdC5hZGQoJ29wZW4nKTsKfQpmdW5jdGlvbiBkZWxTdmMoYmksIHNpKSB7CiAgZGIuYnJlZWRzW2JpXS5zZXJ2aWNlcy5zcGxpY2Uoc2ksIDEpOwogIHNhdmUoKTsKICByZW5kZXJCcmVlZExpc3QoKTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnYnJfJytiaSkuY2xhc3NMaXN0LmFkZCgnb3BlbicpOwp9CmZ1bmN0aW9uIHJlbmRlckJyZWVkU2VsZWN0KCkgewogIHZhciBzZWwgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbmV3Q2xpZW50QnJlZWQnKTsKICBpZiAoIXNlbCkgcmV0dXJuOwogIHNlbC5pbm5lckhUTUwgPSAnPG9wdGlvbiB2YWx1ZT0iIj7Qn9C+0YDQvtC00LAuLi48L29wdGlvbj4nOwogIGRiLmJyZWVkcy5mb3JFYWNoKGZ1bmN0aW9uKGIpIHsKICAgIHNlbC5pbm5lckhUTUwgKz0gJzxvcHRpb24+JytiLm5hbWUrJzwvb3B0aW9uPic7CiAgfSk7Cn0KCi8vIOKUgOKUgCDQo9Cf0KDQkNCS0JvQldCd0JjQlTog0JrQm9CY0JXQndCi0Ksg4pSA4pSACmZ1bmN0aW9uIHJlbmRlckNsaWVudExpc3QoKSB7CiAgdmFyIGVsID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NsaWVudExpc3RVSScpOwogIGlmICghZWwpIHJldHVybjsKICB2YXIgbWVyZ2VkID0ge307CiAgZGIuY2xpZW50cy5mb3JFYWNoKGZ1bmN0aW9uKGMpIHsKICAgIG1lcmdlZFtjLnBob25lfHxjLm5hbWVdID0gYzsKICB9KTsKICBhbGxCb29raW5ncy5mb3JFYWNoKGZ1bmN0aW9uKGIpIHsKICAgIGlmICghYi5jbGllbnRQaG9uZSAmJiAhYi5jbGllbnROYW1lKSByZXR1cm47CiAgICB2YXIga2V5ID0gYi5jbGllbnRQaG9uZSB8fCBiLmNsaWVudE5hbWU7CiAgICBpZiAoIW1lcmdlZFtrZXldKSB7CiAgICAgIG1lcmdlZFtrZXldID0ge25hbWU6IGIuY2xpZW50TmFtZSwgcGhvbmU6IGIuY2xpZW50UGhvbmUsIHBldDogYi5wZXROYW1lLCBicmVlZDogYi5icmVlZCwgbm90ZTogJycsIHZpc2l0czogMCwgdG90YWw6IDB9OwogICAgfQogICAgbWVyZ2VkW2tleV0udmlzaXRzID0gKG1lcmdlZFtrZXldLnZpc2l0c3x8MCkgKyAxOwogICAgbWVyZ2VkW2tleV0udG90YWwgID0gKG1lcmdlZFtrZXldLnRvdGFsfHwwKSArIChwYXJzZUZsb2F0KGIucHJpY2UpfHwwKTsKICAgIG1lcmdlZFtrZXldLmxhc3REYXRlICAgPSBiLmRhdGU7CiAgICBtZXJnZWRba2V5XS5sYXN0TWFzdGVyID0gYi5tYXN0ZXI7CiAgfSk7CiAgdmFyIGFyciA9IE9iamVjdC52YWx1ZXMobWVyZ2VkKTsKICBhcnIuc29ydChmdW5jdGlvbihhLGIpeyByZXR1cm4gKGIudmlzaXRzfHwwKS0oYS52aXNpdHN8fDApOyB9KTsKICB2YXIgaHRtbCA9ICcnOwogIGFyci5mb3JFYWNoKGZ1bmN0aW9uKGMsIGkpIHsKICAgIHZhciBpbml0aWFscyA9IChjLm5hbWV8fCc/Jykuc3BsaXQoJyAnKS5tYXAoZnVuY3Rpb24odyl7cmV0dXJuIHdbMF18fCcnO30pLmpvaW4oJycpLnN1YnN0cmluZygwLDIpLnRvVXBwZXJDYXNlKCk7CiAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJjbGllbnQtY2FyZCBhbmltIj4nOwogICAgaHRtbCArPSAnPGRpdiBjbGFzcz0iY2wtcm93Ij4nOwogICAgaHRtbCArPSAnPGRpdiBjbGFzcz0iY2wtYXZhdGFyIj4nK2luaXRpYWxzKyc8L2Rpdj4nOwogICAgaHRtbCArPSAnPGRpdiBzdHlsZT0iZmxleDoxIj48ZGl2IGNsYXNzPSJjbC1uYW1lIj4nKyhjLm5hbWV8fCfigJQnKSsnPC9kaXY+JzsKICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9ImNsLWRldGFpbCI+JysoYy5waG9uZXx8JycpKyhjLnBldD8nIMK3ICcrYy5wZXQ6JycpKyhjLmJyZWVkPycgwrcgJytjLmJyZWVkOicnKSsnPC9kaXY+PC9kaXY+JzsKICAgIGlmICghYy5mcm9tQ2FsZW5kYXIpIHsKICAgICAgaHRtbCArPSAnPGJ1dHRvbiBjbGFzcz0iZGVsLWJ0biIgb25jbGljaz0iZGVsQ2xpZW50KCcraSsnKSI+4pyVPC9idXR0b24+JzsKICAgIH0KICAgIGh0bWwgKz0gJzwvZGl2Pic7CiAgICBpZiAoKGMudmlzaXRzfHwwKSA+IDApIHsKICAgICAgaHRtbCArPSAnPGRpdiBjbGFzcz0iY2wtc3RhdHMiPic7CiAgICAgIGh0bWwgKz0gJzxkaXYgY2xhc3M9ImNzdGF0Ij48ZGl2IGNsYXNzPSJjc3QtdmFsIj4nKyhjLnZpc2l0c3x8MCkrJzwvZGl2PjxkaXYgY2xhc3M9ImNzdC1sYWJlbCI+0LLQuNC30LjRgtC+0LI8L2Rpdj48L2Rpdj4nOwogICAgICBodG1sICs9ICc8ZGl2IGNsYXNzPSJjc3RhdCI+PGRpdiBjbGFzcz0iY3N0LXZhbCI+JysoYy50b3RhbHx8MCkrJyDigqw8L2Rpdj48ZGl2IGNsYXNzPSJjc3QtbGFiZWwiPtC/0L7RgtGA0LDRh9C10L3QvjwvZGl2PjwvZGl2Pic7CiAgICAgIGh0bWwgKz0gJzwvZGl2Pic7CiAgICAgIGlmIChjLmxhc3REYXRlKSB7CiAgICAgICAgaHRtbCArPSAnPGRpdiBjbGFzcz0iY2wtbGFzdCI+0J/QvtGB0LvQtdC00L3QuNC5INCy0LjQt9C40YI6IDxzcGFuPicrYy5sYXN0RGF0ZSsoYy5sYXN0TWFzdGVyPycgwrcgJytjLmxhc3RNYXN0ZXI6JycpKyc8L3NwYW4+PC9kaXY+JzsKICAgICAgfQogICAgfQogICAgaWYgKGMubm90ZSkgaHRtbCArPSAnPGRpdiBzdHlsZT0iZm9udC1zaXplOi42MnJlbTtjb2xvcjojNTU1NTUwO21hcmdpbi10b3A6NnB4Ij4nK2Mubm90ZSsnPC9kaXY+JzsKICAgIGh0bWwgKz0gJzwvZGl2Pic7CiAgfSk7CiAgZWwuaW5uZXJIVE1MID0gaHRtbCB8fCAnPGRpdiBzdHlsZT0iY29sb3I6IzQ0NDQ0MDtmb250LXNpemU6Ljc1cmVtO3BhZGRpbmc6MTJweCAwIj7QndC10YIg0LrQu9C40LXQvdGC0L7QsjwvZGl2Pic7Cn0KZnVuY3Rpb24gYWRkQ2xpZW50KCkgewogIHZhciBuYW1lICA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCduZXdDbGllbnROYW1lJykudmFsdWUudHJpbSgpOwogIHZhciBwaG9uZSA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCduZXdDbGllbnRQaG9uZScpLnZhbHVlLnRyaW0oKTsKICB2YXIgcGV0ICAgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbmV3Q2xpZW50UGV0JykudmFsdWUudHJpbSgpOwogIHZhciBicmVlZCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCduZXdDbGllbnRCcmVlZCcpLnZhbHVlOwogIHZhciBub3RlICA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCduZXdDbGllbnROb3RlJykudmFsdWUudHJpbSgpOwogIGlmICghbmFtZSkgeyBhbGVydCgn0JLQstC10LTQuNGC0LUg0LjQvNGPINC60LvQuNC10L3RgtCwJyk7IHJldHVybjsgfQogIGRiLmNsaWVudHMucHVzaCh7bmFtZTpuYW1lLCBwaG9uZTpwaG9uZSwgcGV0OnBldCwgYnJlZWQ6YnJlZWQsIG5vdGU6bm90ZSwgdmlzaXRzOjAsIHRvdGFsOjB9KTsKICBzYXZlKCk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ25ld0NsaWVudE5hbWUnKS52YWx1ZSA9ICcnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCduZXdDbGllbnRQaG9uZScpLnZhbHVlID0gJyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ25ld0NsaWVudFBldCcpLnZhbHVlID0gJyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ25ld0NsaWVudE5vdGUnKS52YWx1ZSA9ICcnOwogIHJlbmRlckNsaWVudExpc3QoKTsKfQpmdW5jdGlvbiBkZWxDbGllbnQoaSkgewogIGlmICghY29uZmlybSgn0KPQtNCw0LvQuNGC0Ywg0LrQsNGA0YLQvtGH0LrRgyDQutC70LjQtdC90YLQsD8nKSkgcmV0dXJuOwogIGRiLmNsaWVudHMuc3BsaWNlKGksIDEpOwogIHNhdmUoKTsKICByZW5kZXJDbGllbnRMaXN0KCk7Cn0KCi8vIOKUgOKUgCBJTklUIOKUgOKUgApkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnZGlzY291bnRJbnB1dCcpLnZhbHVlID0gZGIuZGlzY291bnQ7CnJlbmRlck1hc3Rlckxpc3QoKTsKcmVuZGVyQnJlZWRMaXN0KCk7CnJlbmRlckNsaWVudExpc3QoKTsKbG9hZFN0YXRzKCk7Cjwvc2NyaXB0Pgo8L2JvZHk+CjwvaHRtbD4="

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
    msg = f"📋 Новая заявка на обратный звонок:\nИмя: {name}\nТелефон: {phone}"
    twilio_sid   = os.environ.get("TWILIO_ACCOUNT_SID")
    twilio_token = os.environ.get("TWILIO_AUTH_TOKEN")
    twilio_phone = os.environ.get("TWILIO_PHONE", "+37266922128")
    twilio_wa_from = "whatsapp:" + twilio_phone
    print(f"[callback] name={name!r} phone={phone!r} sid_set={bool(twilio_sid)} from={twilio_wa_from}", flush=True)
    if twilio_sid and twilio_token:
        try:
            sms_url = f"https://api.twilio.com/2010-04-01/Accounts/{twilio_sid}/Messages.json"
            resp = requests.post(
                sms_url,
                auth=(twilio_sid, twilio_token),
                data={"From": twilio_wa_from, "To": "whatsapp:+37258735456", "Body": msg},
                timeout=10
            )
            print(f"[callback] Twilio response: {resp.status_code} {resp.text[:200]}", flush=True)
        except Exception as e:
            print(f"[callback] Twilio error: {e}", flush=True)
    else:
        print("[callback] Twilio creds not set — skipping", flush=True)
    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
