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
BOOKING_HTML_B64 = "77u/PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9InJ1Ij4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idGhlbWUtY29sb3IiIGNvbnRlbnQ9IiMwYTBhMGEiPgo8bWV0YSBuYW1lPSJ2aWV3cG9ydCIgY29udGVudD0id2lkdGg9ZGV2aWNlLXdpZHRoLGluaXRpYWwtc2NhbGU9MSI+Cjx0aXRsZT5SJkogR3Jvb21pbmc8L3RpdGxlPgo8bGluayBocmVmPSJodHRwczovL2ZvbnRzLmdvb2dsZWFwaXMuY29tL2NzczI/ZmFtaWx5PUNvcm1vcmFudCtHYXJhbW9uZDp3Z2h0QDQwMDs2MDAmZmFtaWx5PVBsYXlmYWlyK0Rpc3BsYXk6aXRhbCx3Z2h0QDAsNDAwOzAsNjAwOzAsNzAwOzEsNDAwJmZhbWlseT1Nb250c2VycmF0OndnaHRAMzAwOzQwMDs1MDA7NjAwJmRpc3BsYXk9c3dhcCIgcmVsPSJzdHlsZXNoZWV0Ij4KPHN0eWxlPgoqe2JveC1zaXppbmc6Ym9yZGVyLWJveDttYXJnaW46MDtwYWRkaW5nOjB9Cmh0bWwsYm9keXttaW4taGVpZ2h0OjEwMHZoO2JhY2tncm91bmQ6IzBhMGEwYTtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXdlaWdodDo0MDB9Ci5zY3JlZW57ZGlzcGxheTpub25lO21pbi1oZWlnaHQ6MTAwdmg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2FsaWduLWl0ZW1zOmNlbnRlcjtwYWRkaW5nOjQ4cHggMCA2NHB4fQouc2NyZWVuLmFjdGl2ZXtkaXNwbGF5OmZsZXh9Ci5jb257d2lkdGg6MTAwJTttYXgtd2lkdGg6NDAwcHg7cGFkZGluZzowIDI4cHh9Ci5iYWNrLWJ0bntkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo4cHg7Y29sb3I6I2ZmZmZmZjtmb250LXNpemU6MC44cmVtO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyOm5vbmU7Y3Vyc29yOnBvaW50ZXI7cGFkZGluZzowO21hcmdpbi1ib3R0b206MzZweDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXdlaWdodDozMDA7dHJhbnNpdGlvbjpjb2xvciAuMnN9Ci5iYWNrLWJ0bjpob3Zlcntjb2xvcjojZmZmZmZmfQoubG9nby1yantmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZjtmb250LXNpemU6Mi41cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmfQoubG9nby1zdWJ7Zm9udC1zaXplOjAuNjYzcmVtO2ZvbnQtd2VpZ2h0OjYwMDtsZXR0ZXItc3BhY2luZzouNGVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO21hcmdpbi10b3A6M3B4O3BhZGRpbmctYm90dG9tOjE0cHg7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4yKTttYXJnaW4tYm90dG9tOjIwcHh9Ci5ob21lLXJqe2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmO2ZvbnQtc2l6ZTozLjI1cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjF9Ci5sb2dvLXRhZ3tmb250LXNpemU6MC43NXJlbTtsZXR0ZXItc3BhY2luZzouMTJlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjtsaW5lLWhlaWdodDoxLjV9Ci5sb2dvLXJvd3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6ZmxleC1lbmQ7Z2FwOjEycHg7bWFyZ2luLWJvdHRvbToyOHB4O3BhZGRpbmctYm90dG9tOjE4cHg7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4yKX0KLmxvZ28taW1nLXJvd3ttYXJnaW4tYm90dG9tOjI4cHg7cGFkZGluZy1ib3R0b206MThweDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjIpfQoubG9nby1pbWd7aGVpZ2h0OjkwcHg7d2lkdGg6YXV0bztkaXNwbGF5OmJsb2NrfQouaG9tZS1nc3Vie2ZvbnQtc2l6ZTowLjY2M3JlbTtmb250LXdlaWdodDo2MDA7bGV0dGVyLXNwYWNpbmc6LjRlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjttYXJnaW4tdG9wOjZweDttYXJnaW4tYm90dG9tOjIycHh9Ci5ob21lLWgxe2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6My4xMjVyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNmZmZmZmY7bGluZS1oZWlnaHQ6MS4xO21hcmdpbi1ib3R0b206NnB4fQouaG9tZS1oMSBlbXtmb250LXN0eWxlOml0YWxpYztjb2xvcjojZmZmZmZmfQouaG9tZS1zdWJ7Zm9udC1zaXplOjAuOHJlbTtsZXR0ZXItc3BhY2luZzouMThlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjI4cHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5vcHR7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTZweDtwYWRkaW5nOjE2cHggMDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNyk7dGV4dC1kZWNvcmF0aW9uOm5vbmU7Y29sb3I6I2ZmZmZmZjt0cmFuc2l0aW9uOmNvbG9yIC4ycztjdXJzb3I6cG9pbnRlcjtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyLXRvcDpub25lO2JvcmRlci1sZWZ0Om5vbmU7Ym9yZGVyLXJpZ2h0Om5vbmU7d2lkdGg6MTAwJTtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWZ9Ci5vcHQ6aG92ZXJ7Y29sb3I6I2ZmZn0KLm9wdC1pY29ue3dpZHRoOjM4cHg7aGVpZ2h0OjM4cHg7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2ZsZXgtc2hyaW5rOjB9Ci5vcHQtaWNvbi1pbWd7d2lkdGg6MzhweDtoZWlnaHQ6MzhweDtvYmplY3QtZml0OmNvbnRhaW59Ci5vcHQtdGV4dHtmbGV4OjE7dGV4dC1hbGlnbjpsZWZ0fQoub3B0LXRpdGxle2ZvbnQtc2l6ZToxLjUxMnJlbTtmb250LXdlaWdodDo2MDA7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjJweDt0cmFuc2l0aW9uOmNvbG9yIC4ycztmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWZ9Ci5vcHQ6aG92ZXIgLm9wdC10aXRsZXtjb2xvcjojZmZmfQoub3B0LWhhbmRsZXtmb250LXNpemU6MC44ODdyZW07Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXdlaWdodDozMDB9Ci5vcHQtYXJyb3d7Y29sb3I6I2ZmZmZmZjtmb250LXNpemU6MS4yMjVyZW07ZmxleC1zaHJpbms6MDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLm9wdDpob3ZlciAub3B0LWFycm93e2NvbG9yOiNmZmZmZmZ9Ci5kaXZpZGVye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7cGFkZGluZzoxMnB4IDB9Ci5kaXZpZGVyOjpiZWZvcmUsLmRpdmlkZXI6OmFmdGVye2NvbnRlbnQ6Jyc7ZmxleDoxO2hlaWdodDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNil9Ci5kaXZpZGVyIHNwYW57Zm9udC1zaXplOjAuNjg4cmVtO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQouaG9tZS1mb290e21hcmdpbi10b3A6MzZweDtwYWRkaW5nLXRvcDoyMHB4O2JvcmRlci10b3A6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA2KTtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6Y2VudGVyfQouaG9tZS1mb290IHNwYW57Zm9udC1zaXplOjAuNzc1cmVtO2xldHRlci1zcGFjaW5nOi4yZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5mZG90e3dpZHRoOjJweDtoZWlnaHQ6MnB4O2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMTYpfQoucHJvZ3Jlc3N7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjttYXJnaW4tYm90dG9tOjQwcHg7b3ZlcmZsb3c6aGlkZGVuO2NvdW50ZXItcmVzZXQ6c3RlcH0KLnBze2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjVweDtmb250LXNpemU6MC42NjNyZW07bGV0dGVyLXNwYWNpbmc6LjEyZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7d2hpdGUtc3BhY2U6bm93cmFwO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2NvdW50ZXItaW5jcmVtZW50OnN0ZXB9Ci5wcy5kb25le2NvbG9yOiNmZmZmZmZ9Ci5wcy5hY3RpdmV7Y29sb3I6I2ZmZmZmZn0KLnBkb3R7d2lkdGg6MThweDtoZWlnaHQ6MThweDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7ZmxleC1zaHJpbms6MDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEyKTtmb250LXNpemU6MC42NjNyZW07Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC13ZWlnaHQ6NjAwfQoucGRvdDo6YmVmb3Jle2NvbnRlbnQ6Y291bnRlcihzdGVwLGRlY2ltYWwtbGVhZGluZy16ZXJvKX0KLnBzLmRvbmUgLnBkb3R7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLnBzLmFjdGl2ZSAucGRvdHtib3JkZXItY29sb3I6I2ZmZmZmZjtjb2xvcjojZmZmZmZmfQoucGx7ZmxleDoxO2hlaWdodDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNyk7bWFyZ2luOjAgNXB4O21pbi13aWR0aDo2cHh9Ci5wbC5kb25le2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMTgpfQouc3RlcHtkaXNwbGF5Om5vbmV9LnN0ZXAuc2hvd3tkaXNwbGF5OmJsb2NrO2FuaW1hdGlvbjpmdSAuMzVzIGVhc2UgYm90aH0KLnNsYmx7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjkzOHJlbTtmb250LXdlaWdodDo2MDA7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjIwcHg7bGV0dGVyLXNwYWNpbmc6LjAxZW19Ci5zYm94e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEwcHg7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTYpO3BhZGRpbmc6MCAycHg7dHJhbnNpdGlvbjpib3JkZXItY29sb3IgLjJzfQouc2JveDpmb2N1cy13aXRoaW57Ym9yZGVyLWJvdHRvbS1jb2xvcjojZmZmZmZmfQouc2l7b3BhY2l0eTouMjtmb250LXNpemU6MS4yMjVyZW07ZmxleC1zaHJpbms6MH0KI2JJbnB1dHtmbGV4OjE7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtvdXRsaW5lOm5vbmU7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjUxMnJlbTtjb2xvcjojZmZmZmZmO3BhZGRpbmc6MTJweCAwfQojYklucHV0OjpwbGFjZWhvbGRlcntjb2xvcjojZmZmZmZmfQouY2xye2JhY2tncm91bmQ6bm9uZTtib3JkZXI6bm9uZTtjb2xvcjojZmZmZmZmO2N1cnNvcjpwb2ludGVyO2ZvbnQtc2l6ZToxLjE1cmVtO2Rpc3BsYXk6bm9uZTtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZn0KLmNsci5zaG93e2Rpc3BsYXk6YmxvY2t9Ci5id3JhcHtwb3NpdGlvbjpyZWxhdGl2ZTttYXJnaW4tYm90dG9tOjIwcHh9Ci5kcm9we3Bvc2l0aW9uOmFic29sdXRlO2xlZnQ6MDtyaWdodDowO2JhY2tncm91bmQ6IzBmMGYwZjtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEpO2JvcmRlci10b3A6bm9uZTttYXgtaGVpZ2h0OjIwMHB4O292ZXJmbG93LXk6YXV0bzt6LWluZGV4OjUwO2Rpc3BsYXk6bm9uZX0KLmRyb3Aub3BlbntkaXNwbGF5OmJsb2NrfQouZGl0ZW17cGFkZGluZzoxMXB4IDE0cHg7Zm9udC1zaXplOjEuMzYzcmVtO2NvbG9yOiNmZmZmZmY7Y3Vyc29yOnBvaW50ZXI7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDUpO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLmRpdGVtOmhvdmVye2NvbG9yOiNmZmZ9Ci5kaXRlbSBtYXJre2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Y29sb3I6I2ZmZjtmb250LXdlaWdodDo3MDB9Ci5ub3Jlc3twYWRkaW5nOjE0cHg7Zm9udC1zaXplOjEuMjg4cmVtO2NvbG9yOiNmZmZmZmY7Zm9udC1zdHlsZTppdGFsaWM7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmfQoubm8tYnJlZWQtYmFubmVye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7cGFkZGluZzoxNHB4IDA7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDYpO2N1cnNvcjpwb2ludGVyO3RyYW5zaXRpb246Y29sb3IgLjJzO21hcmdpbi10b3A6NHB4fQoubm8tYnJlZWQtYmFubmVyOmhvdmVyIC5uby1icmVlZC1iYW5uZXItdGl0bGV7Y29sb3I6I2ZmZmZmZn0KLm5vLWJyZWVkLWJhbm5lci1pY29ue2ZvbnQtc2l6ZToxLjU3NXJlbTtmbGV4LXNocmluazowO29wYWNpdHk6LjN9Ci5uby1icmVlZC1iYW5uZXItdGV4dHtmbGV4OjF9Ci5uby1icmVlZC1iYW5uZXItdGl0bGV7Zm9udC1zaXplOjEuNDM4cmVtO2NvbG9yOiNmZmZmZmY7Zm9udC13ZWlnaHQ6NjAwO21hcmdpbi1ib3R0b206MnB4O2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLm5vLWJyZWVkLWJhbm5lci1zdWJ7Zm9udC1zaXplOjAuODg3cmVtO2NvbG9yOiNmZmZmZmY7bGluZS1oZWlnaHQ6MS41O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQoubm8tYnJlZWQtYmFubmVyLWFycm93e2NvbG9yOiNmZmZmZmY7Zm9udC1zaXplOjEuMjI1cmVtO2ZsZXgtc2hyaW5rOjA7dHJhbnNpdGlvbjpjb2xvciAuMnN9Ci5zYmFkZ2V7ZGlzcGxheTpub25lO2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTJweDttYXJnaW4tYm90dG9tOjIwcHh9Ci5zYmFkZ2Uuc2hvd3tkaXNwbGF5OmZsZXh9Ci5ibmFtZXtib3JkZXItYm90dG9tOjFweCBzb2xpZCAjZmZmZmZmO2NvbG9yOiNmZmZmZmY7cGFkZGluZzoycHggMDtmb250LXNpemU6MS40MzhyZW07Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmfQouYmNoZ3tmb250LXNpemU6MC44cmVtO2NvbG9yOiNmZmZmZmY7Y3Vyc29yOnBvaW50ZXI7bGV0dGVyLXNwYWNpbmc6LjEyZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO3RyYW5zaXRpb246Y29sb3IgLjJzfQouYmNoZzpob3Zlcntjb2xvcjojZmZmZmZmfQouc3ZidG57ZGlzcGxheTpibG9jaztwYWRkaW5nOjA7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wOCk7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Y3Vyc29yOnBvaW50ZXI7dGV4dC1hbGlnbjpsZWZ0O3RyYW5zaXRpb246Ym9yZGVyLWNvbG9yIC4yczt3aWR0aDoxMDAlO292ZXJmbG93OmhpZGRlbjtwb3NpdGlvbjpyZWxhdGl2ZX0KLnN2YnRuOmhvdmVye2JvcmRlci1ib3R0b20tY29sb3I6I2ZmZmZmZn0KLnN2YnRuLmFjdGl2ZXtib3JkZXItYm90dG9tLWNvbG9yOiNmZmZmZmZ9Ci5zdnB7Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNmZmZmZmY7ZmxleC1zaHJpbms6MH0KLm1hc3RlcnN7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyO2dhcDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNyl9Ci5tYnRue2JhY2tncm91bmQ6IzBhMGEwYTtwYWRkaW5nOjIycHggMTJweDt0ZXh0LWFsaWduOmNlbnRlcjtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmJhY2tncm91bmQgLjJzO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtib3JkZXI6bm9uZX0KLm1idG46aG92ZXJ7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wMyl9Ci5tYnRuLmFjdGl2ZXtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA1KX0KLm1hdnt3aWR0aDo0MHB4O2hlaWdodDo0MHB4O2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xNCk7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO21hcmdpbjowIGF1dG8gMTBweDtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjEuNDM4cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmfQoubWJ0bi5hY3RpdmUgLm1hdntib3JkZXItY29sb3I6I2ZmZmZmZjtjb2xvcjojZmZmZmZmfQoubW5hbWV7Zm9udC1zaXplOjEuNDM4cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZn0KLm1idG46aG92ZXIgLm1uYW1le2NvbG9yOiNmZmZmZmZ9Ci5tYnRuLmFjdGl2ZSAubW5hbWV7Y29sb3I6I2ZmZmZmZn0KLm10aXRsZXtmb250LXNpemU6MC44cmVtO2NvbG9yOiNmZmZmZmY7bWFyZ2luLXRvcDozcHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5nYnRue2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7cGFkZGluZzoxNHB4IDA7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wOCk7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjEuNDM4cmVtO2N1cnNvcjpwb2ludGVyO3dpZHRoOjEwMCU7dHJhbnNpdGlvbjphbGwgLjJzfQouZ2J0bjpob3Zlcntjb2xvcjojZmZmZmZmfQouZ2J0bi5hY3RpdmV7Y29sb3I6I2ZmZmZmZjtib3JkZXItYm90dG9tLWNvbG9yOiNmZmZmZmZ9Ci5jYWwtaHtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6Y2VudGVyO21hcmdpbi1ib3R0b206MTZweH0KLmNhbC1te2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6MS45MzhyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNmZmZmZmZ9Ci5jYWwtbntiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyOm5vbmU7Y29sb3I6I2ZmZmZmZjtjdXJzb3I6cG9pbnRlcjtmb250LXNpemU6MS41NzVyZW07cGFkZGluZzo0cHggOHB4O3RyYW5zaXRpb246Y29sb3IgLjJzfQouY2FsLW46aG92ZXJ7Y29sb3I6I2ZmZmZmZn0KLmNne2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDcsMWZyKTtnYXA6MnB4O21hcmdpbi1ib3R0b206MTJweH0KLmNkbnt0ZXh0LWFsaWduOmNlbnRlcjtmb250LXNpemU6MC42NjNyZW07Y29sb3I6I2ZmZmZmZjtwYWRkaW5nOjRweCAwO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtsZXR0ZXItc3BhY2luZzouMWVtfQouY2R7dGV4dC1hbGlnbjpjZW50ZXI7Y3Vyc29yOnBvaW50ZXI7Y29sb3I6I2ZmZmZmZjtib3JkZXI6MXB4IHNvbGlkIHRyYW5zcGFyZW50O3RyYW5zaXRpb246YWxsIC4yc30KLmNkOmhvdmVyOm5vdCguZGlzKTpub3QoLnBhZCkgLmNkLWlubmVye2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDcpIWltcG9ydGFudDtjb2xvcjojZmZmZmZmIWltcG9ydGFudH0KLmNkLnNlbCAuY2QtaW5uZXJ7YmFja2dyb3VuZDojZmZmZmZmIWltcG9ydGFudDtjb2xvcjojMGEwYTBhIWltcG9ydGFudDtmb250LXdlaWdodDo3MDAhaW1wb3J0YW50O2JvcmRlcjpub25lIWltcG9ydGFudH0KLmNkLnRvZCAuY2QtaW5uZXJ7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4yOCk7Y29sb3I6I2ZmZn0KLmNkLmRpc3tjb2xvcjojZmZmZmZmO2N1cnNvcjpkZWZhdWx0fQoudGd7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoNCwxZnIpO2dhcDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNyl9Ci50YnRue2JhY2tncm91bmQ6IzBhMGEwYTtib3JkZXI6bm9uZTtwYWRkaW5nOjEzcHggNHB4O3RleHQtYWxpZ246Y2VudGVyO2ZvbnQtc2l6ZToxLjMyNXJlbTtjb2xvcjojZmZmZmZmO2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjt0cmFuc2l0aW9uOmFsbCAuMnN9Ci50YnRuOmhvdmVye2NvbG9yOiNmZmZmZmY7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNCl9Ci50YnRuLmFjdGl2ZXtjb2xvcjojZmZmZmZmfQouc3Vte2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOm5vbmU7Ym9yZGVyLXRvcDoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDgpO3BhZGRpbmc6MjBweCAwO21hcmdpbi1ib3R0b206MjBweH0KLnNye2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjtwYWRkaW5nOjhweCAwO2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA1KTtmb250LXNpemU6MS4zNjNyZW07Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmfQouc3I6bGFzdC1jaGlsZHtib3JkZXItYm90dG9tOm5vbmU7cGFkZGluZy10b3A6MTRweH0KLnNse2NvbG9yOiNmZmZmZmZ9LnN2e2NvbG9yOiNmZmZmZmY7dGV4dC1hbGlnbjpyaWdodH0KLnNwe2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6Mi40MzhyZW07Y29sb3I6I2ZmZmZmZjtmb250LXdlaWdodDo2MDB9Ci5mZ3ttYXJnaW4tYm90dG9tOjIwcHh9Ci5mbHtmb250LXNpemU6MC43MTJyZW07bGV0dGVyLXNwYWNpbmc6LjIyZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbTo4cHg7ZGlzcGxheTpibG9jaztmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZn0KLmZpe3dpZHRoOjEwMCU7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xNCk7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjEuNTEycmVtO3BhZGRpbmc6MTBweCAwO291dGxpbmU6bm9uZTt0cmFuc2l0aW9uOmJvcmRlci1jb2xvciAuMnN9Ci5maTpmb2N1c3tib3JkZXItYm90dG9tLWNvbG9yOiNmZmZmZmZ9Ci5jYnRue2Rpc3BsYXk6YmxvY2s7d2lkdGg6MTAwJTtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC1zaXplOjAuODYycmVtO2ZvbnQtd2VpZ2h0OjYwMDtsZXR0ZXItc3BhY2luZzouMjhlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7dGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzoxNnB4O2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMjUpO2N1cnNvcjpwb2ludGVyO3RyYW5zaXRpb246YWxsIC4yc30KLmNidG46aG92ZXJ7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLnNibG9ja3t0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjUycHggMjBweDtkaXNwbGF5Om5vbmV9Ci5zYmxvY2suc2hvd3tkaXNwbGF5OmJsb2NrO2FuaW1hdGlvbjpmdSAuNXMgZWFzZSBib3RofQouc2kye2ZvbnQtc2l6ZTozLjZyZW07bWFyZ2luLWJvdHRvbToyMHB4O29wYWNpdHk6LjR9Ci5zdHtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjIuNzI1cmVtO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbToxMHB4O2ZvbnQtd2VpZ2h0OjYwMH0KLnNze2ZvbnQtc2l6ZToxLjA3NXJlbTtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjEuOTttYXJnaW4tYm90dG9tOjI4cHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5oYnRue2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xNik7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXNpemU6MC44NjJyZW07bGV0dGVyLXNwYWNpbmc6LjIyZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO3BhZGRpbmc6MTNweCAyOHB4O2N1cnNvcjpwb2ludGVyO3RyYW5zaXRpb246YWxsIC4yc30KLmhidG46aG92ZXJ7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLmxvYWRpbmctc2xvdHN7Y29sb3I6I2ZmZmZmZjtmb250LXNpemU6MS4yODhyZW07cGFkZGluZzoxMnB4IDA7dGV4dC1hbGlnbjpjZW50ZXI7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc3R5bGU6aXRhbGljfQouY2R7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpjZW50ZXI7YWxpZ24taXRlbXM6Y2VudGVyO2hlaWdodDozNnB4IWltcG9ydGFudDtwYWRkaW5nOjAhaW1wb3J0YW50fQouY2QtaW5uZXJ7d2lkdGg6MzJweDtoZWlnaHQ6MzJweDtib3JkZXItcmFkaXVzOjA7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2ZvbnQtc2l6ZToxLjE1cmVtO2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZn0KLmNkLmF2YWlsIC5jZC1pbm5lcntib3JkZXI6MXB4IHNvbGlkIHJnYmEoOTAsMTgwLDkwLC4zNSk7Y29sb3I6cmdiYSg5MCwxODAsOTAsLjY1KX0KLmNkLmJ1c3kgLmNkLWlubmVye2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDcpO2NvbG9yOiNmZmZmZmZ9Ci5jZC5zZWwgLmNkLWlubmVye2JhY2tncm91bmQ6I2ZmZmZmZiFpbXBvcnRhbnQ7Y29sb3I6IzBhMGEwYSFpbXBvcnRhbnQ7Zm9udC13ZWlnaHQ6NzAwIWltcG9ydGFudDtib3JkZXI6bm9uZSFpbXBvcnRhbnR9Ci5jZC50b2QgLmNkLWlubmVye2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMjgpO2NvbG9yOiNmZmY7Zm9udC13ZWlnaHQ6NjAwfQouY2QuZGlzIC5jZC1pbm5lcntjb2xvcjojZmZmZmZmO2N1cnNvcjpkZWZhdWx0O2JhY2tncm91bmQ6bm9uZTtib3JkZXI6bm9uZX0KLnN2YnRuLXJvd3tkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6YmFzZWxpbmU7bWFyZ2luLWJvdHRvbTo2cHg7cGFkZGluZzoxNnB4IDAgMH0KLnN2YnRuLW5hbWV7Zm9udC1zaXplOjEuNTEycmVtO2NvbG9yOiNmZmZmZmY7Zm9udC13ZWlnaHQ6NjAwO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZn0KLnN2YnRuLmFjdGl2ZSAuc3ZidG4tbmFtZXtjb2xvcjojZmZmZmZmfQouc3ZidG4tcHJpY2V7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjcyNXJlbTtjb2xvcjojZmZmZmZmO2ZvbnQtd2VpZ2h0OjYwMDtmbGV4LXNocmluazowfQouc3ZidG4uYWN0aXZlIC5zdmJ0bi1wcmljZXtjb2xvcjojZmZmZmZmfQouc3ZidG4tZGVzY3tmb250LXNpemU6MXJlbTtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjEuNztkaXNwbGF5OmJsb2NrO3BhZGRpbmc6MCAwIDE0cHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7d2hpdGUtc3BhY2U6cHJlLWxpbmV9Ci5zdmJ0bi5hY3RpdmUgLnN2YnRuLWRlc2N7Y29sb3I6I2ZmZmZmZn0KLnN2YnRuLXRhZ3tmb250LXNpemU6MC45NzVyZW07Y29sb3I6I2ZmZmZmZjtmb250LXN0eWxlOml0YWxpYztkaXNwbGF5OmJsb2NrO21hcmdpbi10b3A6MnB4O3BhZGRpbmc6MCAwIDE0cHg7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmfQouc3ZidG4uYWN0aXZlIC5zdmJ0bi10YWd7Y29sb3I6I2ZmZmZmZn0KQG1lZGlhKG1heC13aWR0aDo0MDBweCl7LnN2YnRuLW5hbWV7Zm9udC1zaXplOjEuMzYzcmVtfS5zdmJ0bi1wcmljZXtmb250LXNpemU6MS41MTJyZW19LnN2YnRuLWRlc2N7Zm9udC1zaXplOjAuOTM4cmVtfS5zdmJ0bi10YWd7Zm9udC1zaXplOjAuODg3cmVtfX0KQGtleWZyYW1lcyBmdXtmcm9te29wYWNpdHk6MDt0cmFuc2Zvcm06dHJhbnNsYXRlWSgxMHB4KX10b3tvcGFjaXR5OjE7dHJhbnNmb3JtOnRyYW5zbGF0ZVkoMCl9fQoubGFuZy1iYXJ7cG9zaXRpb246Zml4ZWQ7dG9wOjEycHg7cmlnaHQ6MTRweDt6LWluZGV4Ojk5OTtkaXNwbGF5OmZsZXg7Z2FwOjZweH0KLmxhbmctYnRue2JhY2tncm91bmQ6cmdiYSgxMCwxMCwxMCwuOTIpO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMSk7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXNpemU6MC43NzVyZW07bGV0dGVyLXNwYWNpbmc6LjE1ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO3BhZGRpbmc6NXB4IDEwcHg7Y3Vyc29yOnBvaW50ZXI7dHJhbnNpdGlvbjphbGwgLjJzfQoubGFuZy1idG46aG92ZXJ7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLmxhbmctYnRuLmFjdGl2ZXtib3JkZXItY29sb3I6I2ZmZmZmZjtjb2xvcjojZmZmZmZmfQouY2JrLWJ0bntiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTQpO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC1zaXplOjAuODYycmVtO2xldHRlci1zcGFjaW5nOi4xNmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtwYWRkaW5nOjEycHggMjBweDtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmFsbCAuMnM7d2lkdGg6MTAwJX0KLmNiay1idG46aG92ZXJ7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLm1idG4sLnN2YnRuLC5nYnRuLC50YnRuLC5jYnRuLC5oYnRuLC5jYmstYnRuLC5sYW5nLWJ0biwuYmFjay1idG4sLm9wdCwuZGl0ZW0sLmNkLC5uby1icmVlZC1iYW5uZXIsLmJjaGd7dHJhbnNpdGlvbjphbGwgLjE1cyBlYXNlfQoubWJ0bjphY3RpdmUsLnN2YnRuOmFjdGl2ZSwuZ2J0bjphY3RpdmUsLnRidG46YWN0aXZlLC5jYnRuOmFjdGl2ZSwuaGJ0bjphY3RpdmUsLmNiay1idG46YWN0aXZlLC5sYW5nLWJ0bjphY3RpdmUsLmJhY2stYnRuOmFjdGl2ZSwub3B0OmFjdGl2ZSwuZGl0ZW06YWN0aXZlLC5jZDphY3RpdmUsLm5vLWJyZWVkLWJhbm5lcjphY3RpdmUsLmJjaGc6YWN0aXZle3RyYW5zZm9ybTpzY2FsZSgwLjk2KX0KPC9zdHlsZT4KPC9oZWFkPgo8Ym9keT4KPGEgaHJlZj0iL2FkbWluP3Bhc3M9YW56YTE5ODUiIGlkPSJhZG1pbkJhY2tMaW5rIiBzdHlsZT0iZGlzcGxheTpub25lO3Bvc2l0aW9uOmZpeGVkO3RvcDoxNHB4O3JpZ2h0OjE0cHg7Zm9udC1zaXplOjAuOXJlbTtjb2xvcjojYzlhMDVhO3RleHQtZGVjb3JhdGlvbjpub25lO3otaW5kZXg6OTk5O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2JhY2tncm91bmQ6cmdiYSgxMCwxMCw5LC44NSk7cGFkZGluZzo2cHggMTJweDtib3JkZXItcmFkaXVzOjIwcHg7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDIwMSwxNjAsOTAsLjM1KSI+4oaQINCQ0LTQvNC40L0t0L/QsNC90LXQu9GMPC9hPgo8c2NyaXB0PmlmKGxvY2F0aW9uLnNlYXJjaC5pbmRleE9mKCdwYXNzPWFuemExOTg1JykhPT0tMSl7ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2FkbWluQmFja0xpbmsnKS5zdHlsZS5kaXNwbGF5PSdibG9jayc7fTwvc2NyaXB0Pgo8ZGl2IGNsYXNzPSJsYW5nLWJhciI+CiAgPGJ1dHRvbiBjbGFzcz0ibGFuZy1idG4gYWN0aXZlIiBvbmNsaWNrPSJzZXRMYW5nKCdydScpIj5SVTwvYnV0dG9uPgogIDxidXR0b24gY2xhc3M9ImxhbmctYnRuIiBvbmNsaWNrPSJzZXRMYW5nKCdlbicpIj5FTjwvYnV0dG9uPgogIDxidXR0b24gY2xhc3M9ImxhbmctYnRuIiBvbmNsaWNrPSJzZXRMYW5nKCdldCcpIj5FVDwvYnV0dG9uPgo8L2Rpdj4KCjwhLS0gSE9NRSAtLT4KPGRpdiBjbGFzcz0ic2NyZWVuIGFjdGl2ZSIgaWQ9ImhvbWVTY3JlZW4iPgo8ZGl2IGNsYXNzPSJjb24iPgogIDxkaXYgY2xhc3M9ImxvZ28taW1nLXJvdyI+CiAgICA8aW1nIHNyYz0iZGF0YTppbWFnZS9wbmc7YmFzZTY0LGlWQk9SdzBLR2dvQUFBQU5TVWhFVWdBQUFVTUFBQURyQ0FZQUFBRHpDL1F3QUFBQldHbERRMUJKUTBNZ1VISnZabWxzWlFBQWVKeDlrTEZMdzFBUXhyOVdwYUIxRUIwY0hES0pRNVNTQ3JvNHRCVkVjUWhWd2VxVXZxYXBrTVpIa2lJRk4vK0JnditCQ3M1dUZvYzZPamdJb3BQbzV1U2s0S0xsZVMrSnBDSjZqK04rZk8rNzR6Z2dPVzV3YnZjRHFEdStXMXpLSzV1bExTWDFqQVM5SUF6bThaeXVyMHIrcmovai9UNzAzazdMV2IvLy80M0JpdWt4cXArVUdjWmRIMGlveFBxZXp5WHZFNCs1dEJSeFM3SVY4b25rY3NqbmdXZTlXQ0MrSmxaWXphZ1F2eENyNVI3ZDZ1RzYzV0RSRG5MN3RPbHNyTWs1bEJOWXhBNDhjTmd3MElRQ0hkay8vTE9CdjRCZGNqZmhVcCtGR256cXlaRWlKNWpFeTNEQU1BT1ZXRU9HVXBOM2p1NTNGOTFQamJXREoyQ2hJNFM0aUxXVkRuQTJSeWRyeDlyVVBEQXlCRnkxdWVFYWdkUkhtYXhXZ2RkVFlMZ0VqTjVRejdaWHpXcmg5dWs4TVBBb3hOc2trRG9FdWkwaFBvNkU2QjVUOHdOdzZYd0JBNmRpRThIWVdoTUFBRUh3U1VSQlZIaWM3WjE1ZkZWRnN2anIzRFg3Qm9RbFFBaWJLQUkrVUZCeFgxQVp3SEY1SWlCUEhSY2VEaTdvcVBoVFJsRkFRY1ZSVVo4UFVYSFVKK0xvNEs2QUFzNjRvT0RHSWhEQ2tvUkE5dlZ1WjZuZkgxaE5uNzduSmpjUUlJSDZmajc1M0NYbmR2ZnBjN3BPVlZkMU5RRERNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNRXpiUUR2U0RUaGFjTGxjZ0lpZ2FScFlsZ1Z1dHh0TTB3Uk5PN2d1UnNTb2VnQUFMTXNDQUJEbDAzR2Fwb2syTUMyUHgrTUJ3ekFPK3JyS0lDSzRYQzV4emVUM0ROTm1JT0VrbzJrYWVEeWVGaW5mNC9HQTIrMk9XVThzV3JJTnh6b3VsMHYwdjl2dEJyZmIzYUxsYTVvR2JyY2JmRDZmK0s2bDYyQ2FoalhERnNEbjgwRWtFckY5UnhxYXF0azFGL24zVGxxZy9IL1dLQTRkYnJjYkVGSDhrU1hRRXFqWDArdjFncTdyTFZJMnd4eFdWQTNNU1lzN1VIdytuMDBEYkVvYmRMbGNMV3JDSGV0NFBCN1JuM0svdHFUV3JWNHYrc3phSWRPbWtNMG53dXYxQ3NIVWtxakNqbDVsTTA3K3pFS3haWkN2cmN2bGFuRWhKWmRIcGpKUGNUQnRFbFVRdGpRa0JGWGhHcS9HeUJ3NGREMXAyZ05nLy9WdWlZZGRyTEw0ZWpKdER2V205ZnY5QUdBM3J3NjJmTFVjV2V1VEo5M3BmN0hheGh3WXFpWVl5NkYxSUxoY0xxRUZVajJ5QUdZT0g5emJMWURQNTRPSEhub0lPM2Z1REpGSXBFWE5LRGxjeHpBTTBIVWQ2dXJxb0tTa0JQYnUzUXViTm0zU1NrcEtvTEt5RWdEc1RwU1djT0FjNjV4Kyt1azRhZElrQUFDSVJDTGc5WG9oSEE2RDIrMEd5N0lPV2lnYWhnRVZGUlh3OE1NUGErRndXSHpQempDbVRaS1ltQWpyMTYvSGNEaU1obUVnSXFLdTYyaFpGcHFtaVlnb1h1bi9oSzdyaUlob1dSWmFsbVg3SDMybVYvbTM4dnRJSklLR1llRGF0V3Z4amp2dXdMeThQRWhOVFkzU0V1a3p2UjRPemNPcExucFkwS3VUOWlzalAxem8yRU14Sit2RXVISGpSRCtyMTFLRnJvbDZqZVZyS0Y5eituNzkrdldZa3BJaU5NVERjVjVNTkt3WnRnQStudyttVHAyS2FXbHBvR2thSkNVbFFidDI3V0R3NE1Gd3dna25nR1ZadG1Cc1hkZkI2L1VLclk4K2s2WkJtb2ZINHhIZm1hWXBoRUlrRWdHZnoyZlRUT1QzaG1IQSt2WHI0YVdYWG9JVksxWm9CUVVGSXZSSERzK2h3UEJERFlXS09HbXE5SjJzQ2RGNWE1b20ycWVHRmNtL1BaU2NjY1laT0hIaVJQQjRQT0R6K1NBM054Y0dEeDRNQ1FrSjRoanFTMVdncXlFemRLMi8vZlpiS0Nnb0FGM1hJU0VoQVlxS2l1RGhoeC9XZ3NIZ1lUMDN4ZzRMdzRPRUJBcVpUWFFEZXp3ZU9PNjQ0K0NpaXk3Q0o1OTgwaWJNQUVBSVFIcXRyYTJGMU5SVTJMbHpKMnpjdUJIV3JWc0hSVVZGVUYxZExjcE1Ta3FDcmwyN3dybm5uZ3Rubm5tbXplT29hWnBOaUFhRFFmRDcvZkR6enovRDBxVkxZZmJzMlpxdTY3YjR1TU14Mk5SQlRTczQ1TDZqNzAzVGpHczFCczJaSGc1QlRtMEQyUGVReWN2TGcrT1BQeDVuekpnQko1OThzamlHaEowVGRPMzM3TmtETjl4d0Eyelpza1VyTEN5RVNDUUNpR2lMVTZWK29QTm1nY2kwS1dLWk5iUjY1UGJiYjBmVE5ERWNEdHRNTE5rTXJxeXN4QjQ5ZW9EUDU0UEV4RVFBMkNkSWFESmREcmx3dTkyUWtaRUJNMmJNUUVURVFDQmdLOU0wVFp2WkhRZ0VjTjI2ZGFpYXpvY2pqbzBFaVd6V3FtRWo4bWNuQjVDNitrTTE5dzhsY2gyeW84UHY5OE9MTDc2SW9WRElaZzdMMHg2eVNWMVdWb2JkdW5XRHBLUWtVYTdzQ0NQSEc4TzBhV2lReXNKRm5nODc0WVFUWU4yNmRiWkJJd3RGMHpTeHJLd00yN1ZySjM1RFpYaTlYdHU4R3cwZ2VzM096b2EzMzM0YnE2dXIwYklzRElmRFVlVmJsb1dHWWVEbm4zK09mZnYyQllBak0vamNiamVrcDZmRHFhZWVpdVBHamNQWFgzOGR0MjdkaW5WMWRVSjRSQ0lSTENzcnd3OC8vQkN2dnZwcUhEeDRNQUxZUGVpcWtEeGNiVmZuT3Z2MTZ3ZmZmZmVkcmI5cHZwQWVTSWlJNFhBWXI3MzIycWp6QUlDb2VVSmFta2Z2R2FiTjRCUWZSamUwSFA3eTBVY2ZvV0VZcU91NkdEZzBXSFJkeCtycWF1emN1Yk1vUTM1dHJDNlB4d05wYVdrd2NlSkVySyt2anhLNGhtSFlCdXF5WmNzd0t5dnJzQTAwT1Q3eXozLytNNzd4eGhzMjRVZEVJcEVvUjVGbFdiaGp4dzU4L1BISE1UYzMxeVlrV2lwMHFTbms2Nmc2Z2R4dU56ejExRk1ZRG9lam5GMHl2Lzc2S3g1MzNIRTJqZC9wVmRXWWVRVUsweVp4OG9qS04vTnJyNzBXcFEyU1NXV2FKbFpXVm1LSERoMUVXVEswb2dYQVdVTUMyQ2R3VHp2dE5BeUh3MUdtbW15K1JTSVJYTGR1M1dHYmlISzVYTkNwVXlkWXZIZ3hWbGRYQzAySjJvSzR6NFQ4K3V1djhldXZ2eGJIMFArb245YXVYWXNubjN3eUhvbVZOZW9LRkpscnJybkdaaXFybm1iTHNuREZpaFZJZ2hEQW5rUkRmbGp3TWp6bXFFRU5tZ1hZUDNqbXpadG5FMHBxbUVaRlJRVzJhOWZPY2FVRFFMU3dkWHJ2OVhwaC9QangyTkRRWUJ1VVZDZHBYTUZnRUtkUG40Nkh3OHpNenM2R3p6NzdUQWczRWhDaFVBaS8vLzU3UFBQTU0xRU9QUGI3L2ZESUk0OWdUVTJOemVSRVJDd29LTUNjbkJ4Ujl1RVVHcktKTEQrWWhnNGRpclcxdGVMYzVMQWFhdmZTcFV0Um5qTlZ6VzJBNkN4RGJDSXpSeDMwNUo4MmJacE5HS2hVVkZRSXpaQm96b0NnZ1phYW1ncExseTYxelZuUndKUS8vL0RERDJMKzhHQ0ZpcXk1RW02M0d4SVNFdUNmLy93bm1xWnBFNGFJaU04OTl4d21KaWJHWE1vNGFkS2tLTE0vRW9uZ3FsV3JzRFU1Ry9MeThxQzZ1dG9tQk5YcnUzanhZZ1N3TzVGWSsyT09PVWlqdS9mZWUyMkRSUjB3QnlvTXlic01BQ0tzNXVTVFR4WmFpbXEya1VuWDBOQ0FFeVpNc0drc1ZDZVpvczNWVG1TaHFHa2FUSjgrUGFwKzB6Unh5WklsbUoyZGJmc3RlY3ZwWERJeU1tRFJva1cyTnBQQXVmbm1tMXVOUU96ZXZUdFVWVlhGRE1SR1JGeTBhSkVRaGtmQytjUEVCMStSUXd6K252L3VVTVdNVWRabGl0a3pUUlBXcmwyckZSWVcyc0pRS0ZiUDUvTUJJa0pTVWhLTUd6ZE9CSHZMUWMxMGJIUGJxK3U2aUxkTFQwK0hNV1BHaVBvamtRaFlsZ1dXWmNFWFgzd0JwYVdsdHJBYmlyT2o3T0RWMWRXd1pzMGFDSWZENFBmN2Jjc2N4NHdaMDJyeS9jbm5yRUxYdnFHaHdmYWQvTXEwSGxnWUhnVlFFRFBBL2dINHdBTVBDQUVqcjRDaElHakRNT0NjYzg0UkdwbnFtR2pPWUZWakNMMWVMd3diTmd5N2QrOHV5dkg1Zk9CeXVXRFBuajN3MDA4L2dhWnBRcENyU1NjUUVSSVNFdUNISDM2QVBYdjJpSExwM0RwMjdBaloyZG10WWw3Tk1BemJ3OFFKZVdVSndjS3c5Y0hDOEFod0lDWm9VMlhSS2dlWHl3VStudytXTEZtaXljS0Y2cU5sZXg2UEI1S1RrNEZpRzFYTnRUbG1uRncrSWtJa0VvSCsvZnNET1lSSVdDTWkxTlhWUVhWMXRZWlN0bWhhcGlobkN3K0ZRckIxNjFhdHFxcktWb2VtYVpDYW1pcmFmYVJwU3FqUkVyeldJTGlaeG1GaDJNWWhvUUt3ejhTazdEYkJZQkFhR2hxRW9BSFlyLzNSQUxZc0MzSnljbEJOWVg4ZzgxbGszdElTT1ovUEoweDNlUk9sek14TTZOeTVNMUw5VHJHVVZEOWxjYUZ6Q0lWQ0FBRFEwTkFnbHJJZGFaeldUS3YvazkrelVHeTlzREJzNDZqSkZ1VFVVc0ZnVUF3KzBzNUlHSklRVFV0THM1Vkg1dlRCcEk5U056YVNOY2YyN2R2RHNHSER4UDlsUVU3SksrU0VEWFFNbWM2V1pVRkRRd09VbEpTMENvK3NVL0MzS2hobGdjbENzZlhDd3ZBd29RNlFsdEpxYUZFL1FMUmdESVZDUXZqSmMyNXk4Z05kMTIxSkVXVGlFVGJ5NEtiQkhvbEVRTmYxS0NGTjdSZzJiQmhRdktEY2ZzTXdSQVlZQUlDZVBYc2lDV3NTMG9nSU8zZnVoSWFHaHNPV3FLRXhxSzJ4cmkvMUNYMW1ZZGg2WVdGNGlJbDE4N2ZrbkdFczd5K2xtU0t0VU43SG1ZNHRMQ3pVU0FNallTTnJhMDBoRDNaNTBOZlYxVUVnRUJCdHBIa3p5N0xnNG9zdmh2NzkreU1KRWxrVGxQdmx6RFBQQkZxaUtBdnhSeDU1UkNQQkt2ZERZeXVBNUdQa1B6cE9YU1BzbEpMTGlhYTJjVlhiUmRlZ0thY0xjL2hoWWRqR0lmTVJZTDl6aEFaWldsb2FHSVloZ3BrcFBaUmxXZUR4ZUNBY0RrTkZSWVhOaEhaNmJRelMrS2d0SklDKytlWWIyTE5uRHhpR0lVeGdlVDd4bzQ4K2d0VFVWQURZSDJ4Tm1xRnBtdENoUXdjNDU1eHpJREV4VVdpdmlBalRwMCtIZ29JQ2Nidzh4MGp0SU1HbUNuUFNqT1UvT282T3BmbEpTaWNXei9telVEczZZR0hZeHRFMFRUZ1cvSDYvME00eU16UEI0L0dBclBYUjhTNlhDd3pEZ0czYnRrRnRiYTM0WHZZNHh3dDVzVlVCVkZCUW9PM2V2VHNxS0Z4MjRuejIyV2VZbDVjSGNwNUZFaTVUcGt6QlVhTkdpWDJFQTRFQVBQLzg4L0RFRTA5b3NvQ1g1emZsK1ZGNWlWK3NmcVA0VEFDN0kwbzFhUnZqVUd3QXhod1pXQmkyY1VoWUFPenp2dElBSGp0MmJOVGFZMW5vdUZ3dWVPdXR0NFM1SnM4ak5rY1kwUEVrZ0FEMm1ab05EUTJ3WU1FQzRmV1Z0VFRMc3NEcjljSkpKNTBFVTZkT3hheXNMTnU4NVlJRkMvQ3ZmLzJyMERwcmFtcGd4b3daOE5CREQybFVEZ1ZkazJrdGEzcHlteHByTTdWTEZvamtnSkpOL3NidysvMTRxS1pBR09hb2dnYkczWGZmN2JnbW1UalE1WGgwakpvZ3RiQ3dVQ3pIazljbDB4cmg2dXBxdlBqaWk2T1dpY2xseHV0QWtaZVl5V2E2MisyRzh2SnlrYVVHTVhwL0VNdXk4SjU3N2tHWHl3VURCZ3pBOWV2WG82N3JZZ2xlUVVFQnBxYW1SczBuT3EySGx0dnNsQkEyVnNZYnRjMXFmemJHS2FlY2dyVzF0WTdaYW9qSEgzOGNuZnFYaFdicmdqWEROZzVwTDZRRmVUd2VPUGZjYzdGcjE2NDJJVUNha05mcmhVZ2tBdSsvL3o1ODlkVlhHcFhocEEzRzY2Mmxjc2xrSmVlR2FacHc0b2tuYWlVbEpXSVZoaXhnTGNzQzB6Umh6cHc1c0dyVkt2emxsMStnWDc5K1VGOWZENnRYcjRZLy9lbFAwS2RQSHkwWURJcnpJL09YbHNHUlZxeHF3ZkxlTVRSUEdtdEpKSDB2eDBuR2k4L25PK0NWTzB6cklyN0hIOU5xSWZPV3pMcXNyQ3k0N2JiYkFBQnMzOHNlNGxBb0JFODk5WlJ0elN6QWdXMUNSTWVyMjVOU2tQWGV2WHRoeG93WjhOUlRUNG5rcGlTWVpPM3JqRFBPZ0pLU0VuanZ2ZmRnMmJKbHNIejVjcTIrdmw3OG4weG1WYURwdWc2Wm1ablFwMDhmMUhWZHJMMm0rVURTS09sUFRrS2hhWnFZai96eXl5ODErZnpqRllxa29UckZHckxtMTdaZ1lkakdrYjJmUHA4UFJvNGNpU05IamhRYlRRSHNENzhod1huNTVaZkRqei8rcUtsSkhHUmhJQXVmZUNEaElXOWtSRUw0alRmZTBNNC8vM3djTjI2YzBGSXA3bEVXNWo2ZkR6NzU1QlA0NUpOUE5KckxvM0xwbGI2amNqUk5nd2tUSnVEOTk5OHZObGFTblRheTRJd2xuRFpzMkFBWFhIQ0JtSE9sY3VQMUpqTkhCMndtdHdMSSthRHVvQ2ZQeFJFZWo4ZVdHWmtFWFVKQ0FseHd3UVc0Y09IQ3FQMVlLRXlrdXJvYUprMmFCRjk4OFlWR2pnSloyTW52eWJSc0NyV05ja2dLQ1RyVE5PSEJCeC9VZ3NHZzdSelZlYlIyN2RyQjdObXpvVmV2WHFLc1dQTnJKSEF0eTRMcTZtcll2bjA3N04yN0YrcnI2OEh2OTBObVppWmtabVpDVmxZV1pHVmxRVVpHaHZndVBUMGRhRnZYMnRwYXFLK3ZqOG9tRSs5RGdNS2E1R2tHRXZKc01qT01SRk1PRkhJbzFOVFVZRlpXRmdEc0V5S3FJMEF1aTk3VFg4K2VQV0gyN05sUkUvZUkrL2RCMmJ0M0wwNmFOQW5UMDlOYjNIeFR3MVRJL0FRQVNFOVBoMm5UcHVIUFAvOHNIRGkwZzU5OC9yS0Q1N1BQUHNQTXpFeXhyTTlwWHRBcGNCb0FvRy9mdnZEZ2d3OWlUVTJOS0plY1NJajdzbndIQWdHY05Xc1cvdGQvL1JjT0dqUUllL2JzS2NwdExNVy9FMlBHakluS0xLN3VoOElPRklhQitMekpobUZnWldVbHRtL2ZQdWEybWFxMlIvTmZVNlpNd2Z6OGZBeUZRbWhabGtqeFQ0SVFFYkc0dUJpSERSdUc2a0J2Q1JNdjFxb1BUZE5nN05peCtNTVBQMkFvRkVKZDE5R3lMTnkyYlJ0V1ZGUkU3ZE5pR0lZdDZlMEhIM3lBQ1FrSmpsdDF4dHBxbElTajMrK0g1NTkvUGlxN055SmllWGs1bm5MS0thaHBta2czSnBkQjdaZlhWemZHRlZkY2dZRkF3RllQQzBPR2NhQXBZVWpmVlZSVVlLZE9uY1NrUDNsTkV4SVNoQ0JNU2txQ3RMUTA2TmV2SDh5Wk0wZUVyRkI2ZkhYZmsrcnFhbnpzc2Nkc1dhRkpDTFFrY3VBeDdlazhlL1pzMng3T2htRmdmbjQrQWdEY2YvLzlXRjlmTDhKODVOQWJFcEtCUUFDZmZmYlpxTFlUcXRhbWZrNU5UUVc1VDNSZHg5cmFXcnpra2t0UTFtTGwzOHBseEp1NVo5eTRjVkhDVUwyMkxBemJCdXhBT2NLUUE4VG44OEU5OTl5RDVlWGxZbGthSlVSdDM3NDlkT3pZRVhKemM2RnYzNzZRbFpWbFcxS1duSndNQVBzR2NDQVFnTFZyMThMMjdkdGgzcng1OFBQUFAyc0Erd1FXaGFNUUIrSTlkb0tDdVFFQWhnMGJodmZjY3crTUhqMWFwTyt5TEF0V3Jsd0pJMGVPMU54dU44eWFOVXZMenM3RzIyNjdEV1FQTVA0ZTlJeS9MekdjUEhreTdOMjdGMmZQbnExUjJBczVTZVE1UFRsZ25EN0xDVldwanovNzdEUDQ3cnZ2TkpUbU5La2ZBT3pMK1F6REVLK04wZGdLRkJaMkRDTVJiOUMxdW9NZGFSYnFmaW55WjluRUxDMHR4VnR1dVFWSGp4Nk51Ym01VWZXVFdYMG92SitrYVY1NjZhVzRaY3VXcUIzaVZxeFlnYm01dVVMNDBMTEJSeDU1eEdiV3E1dEdXWmFGZ1VBQUgzMzBVWlRyY1FxU3BuaEtpcTNNeU1nQXVheXFxaXI4d3gvK0VDWDVuVEwxeEdzaUF3RGNlT09OR0F3R0c5MDNtVFZEaG9INDV3d1I5MjNTNUNRWWRGMlBXcmtoUXdLRkFxMEpKeWNNbVowdExSVFBPdXNzM0xGamgyaExNQmhFMHpTeHJxNU83TUluOTRmWDY0V1VsQlI0OGNVWGJRSlJGcUp5djl4MzMzMm96cG5LMjR1cTUzbmlpU2NDOVkxcG1yaHg0MFpVSFRHMER0cHBaVXE4WnZMa3laTnQreWF6TUdTWUdEUWxER2xPcTdTMEZJY1BINDREQmd6QWdRTUg0b0FCQS9Dc3M4N0NKNTU0QXRldFd5Y2NETExna0xYRVVDaUVoWVdGbUpPVFk5TnNhRkFmeW9RQ0tTa3BzSExsU3NmenV2UE9POFVhYVhsSkhiM201dWJDUng5OUpJNTMwZzRSOXprK3JyLytlb3gxTHFvRDVPV1hYeFpsQmdJQjdOKy92emhXem5Rai81Wm9UbDlObVRMRk51ZnBwQ0d5TUdRWWlFOHp0Q3dMS3lzcm83YlBKUEx5OHVDZGQ5NFJ4OHNDUTlZWURjUEFKNTk4RWxOU1VzUnYxWmcrZWUxdGN3ZGpyTkNlSjU1NHdsRnpMU2dvaUd0Q2NzQ0FBZmp0dDkrS2MxQ25BdWk3MHRKU3ZPR0dHNFNHcUo0YkNiak16RXdvS1NrUlFtcmV2SG5vZEZ4TDhKZS8vRVhVNCtSUlptSElNTDl6c01LUXRKYWhRNGVLV0QwbnlHdGJWMWVIRjE1NElhcmFUcXdrcE0xQlhzcEdnaWd2THcvSXZDZE5qSVREdEduVDR2Yk81T1Rrd0lZTkcyd2VjWG92QzhhcXFpcTg1WlpiYk1KTkZUVDMzWGVmTUYwM2JkcUUvL0VmL3hGMWZFc0pvbnZ1dVllRjRWRUNyMEJwNVpBM2M4MmFOZHFpUllzQUVXMWVZVlM4d3lrcEtiQnc0VUtRUTFKOFBwOVlVU0l2MFl0bk1LcEpDS2crV2g1Mzc3MzNvaHlhWWxtV01OUFhyRmtUMXpsNnZWNG9MUzJGYTY2NUJnb0tDa1JxTFhuSkhkV1prWkVCczJiTmd1dXV1dzdKWVNJZjA3dDNiN2ppaWl2QTcvZERYVjBkL00vLy9JOVllaWozV1VzSklrN3VldlRBd3JDVkk1dUM4K2ZQMXhZc1dDQ3lSdE9hWGtxS0FMQnZzL1p1M2JyQks2KzhnbjYvSDF3dWw5aUMwK1Z5Z2E3clFvQmdNOE5xVkFIY29VTUg2TmV2bjFpU0ZncUZoQUNMUkNJaTdYOVQ1NmZyT3VpNkRqLysrS00yZGVwVUtDNHV0cDAzU2t2ZEFQYkZFTTZjT1JNbVRweUk4cnBxVGRQZ25udnV3Zjc5K3dNaXd0cTFhK0gxMTEvWG5NbzRtQTJ2WkdKTk43Q0FaQmlGbHBnemxFbEtTb0lWSzFaRWxVRnpkdlFhQ0FUd2dRY2VRSUJvSjRxYzJpdmVjNURuSE1rRGUvYlpaMk5SVVpGWVlTS2J0bVZsWlhqYWFhYzFLVzFWNzdlbWFUQnMyRENzcXFyQ1lEQW9jakxLNTBmemlIdjM3c1hKa3ljanRlK1paNTdCdXJvNmNWeS9mdjBjcjBWTGV0Sm56SmpScUtlZnplUzJBMnVHclJ4MTdpOFVDc0hNbVRPaHVycmFacktxZ2kweE1SRnV1dWttT1BQTU00WDJSQUhPY242L2VKRzFTTklxdTNmdkRoa1pHZUQzKzIzYkN5QWlVRUxXcGlCek96RXhVWnpIbWpWcnRIUFBQVmNFYzZ2TEVtbk9zbjM3OWpCejVreTQrZWFiOGZ6eno4ZkpreWREU2tvS2hNTmhHRGx5SlB6MjIyK092M1hxcndQbFFCeFJUT3VFaFdFcmh6TGF5QU51MWFwVjJtT1BQU2IyUGdFQVlTNVROaG9BZ0U2ZE9zSGt5Wk50ZXlOSEloRmJucittVUZlcGtQREMzMWVKSkNjbmkwdzF0R3FEWWdEakVZWjBic0ZnRUh3K256RDkxNjlmcjAyY09CRktTa3JFQ2hJQSs4YnlMcGNMTWpNellkcTBhYkI0OFdKd3U5MFFEQWJoMVZkZkZZbHI2YmQwSGkydGxYRUtyNk1IRm9hdEhFM2JuNHRRL2p4Ly9ueHQ1Y3FWdHBSWWNzSUMvSDA1MzVWWFhnbDMzbm1uYlFVSENhNTQ1Z3lkd21rQTltZWNsbytoN05ZQSs0VEV3SUVEbXl4ZnpveE4rNlZZbGdXR1ljRDc3Nyt2M1hqampkRFEwQ0FjU2JKamlPcnUwYU1IWkdSa0FBREFsMTkrQ2JObno5Ym9RVUdhSUpVcjUzOXNDWUVZYXlzQnB1M0J3dkFRZzcvbnRxUEI1eVNBMU1Fa0N4MDZuclE5S2ljY0RzTWYvL2hITFJBSWlNMlJDRG5EdGNmamdXblRwc0hnd1lOUkxyODU3YWZmeUpvVkNSWXlPVW5veUdYZmVlZWROdTFRRFl4V3R3QlE1L1J3M3c1NjJza25uNnpWMWRYWmNpVTZlZE4xWFllLy92V3ZVRlJVSlBwUWJUOEpiTG1kY2h1ZDJ0WVl0TjFCWTMybk9xdFVadzdUT21CaDJJcUkxN3NyYXpsLytNTWZvS3FxU2d3NDJWUW1nZUQxZXVHTk45NkFqaDA3QW9BOXMzVnoyMFMvcFIzd1pMT1pIQ3MwSjltdFd6YzQ5OXh6UmVnTnRZbENmR1N0Vms3blQyVlNrb2N0VzdiQWhBa1RoSkFEMkNlc1pDODZ6U08rOHNvcmNNWVpaeUQxQndscWVXOW05ZHprNzZpTmFxTGRXRFFXb3RSVS96YlhtODhjV2xnWXRoSlU3YUVwU0N2NzVwdHZ0RVdMRm9ud0dYbi9ZdG5rN051M0wweWZQaDFwSHhKS3U5OGNaS2NESWtKK2ZqNFVGUlhaQkdRNEhMYnRqL3o2NjYvRGNjY2RKOXBNNTZuck92ajlmaUg0NUVCdVRkT0VvNGY0NUpOUHRFbVRKc0dtVFpzQVlKOFdTT2RBNStwMnUySEFnQUV3ZCs1Y09PZWNjOURyOVlyNmFEc0FwM09TQThubDZ4RFBQaWpOMGU1WUUyemRzREE4RE1RajZHS1pVazM5eHJJc21EdDNycloxNjFZaGxHaVRkZExVS0JYWVZWZGRCVU9IRGtWS1RkV2NvR3UxUFlnSW16WnQwalp2M2l5RWlXRVlZazZQMnRLdVhUdTQ5OTU3TVQwOVBTcUhJTzA1UWtLTk5FWVNYcVRaRWN1V0xkUFdybDBMQVB0VGtnSFlWOWNZaGdGRGh3NkZ2Ly85NzNERkZWZUkzSVV1bDB0b3pkUiswanhsODUvS3BuTGo2UjgxTUQxZVdETnNYYkF3YkNYRUl6Q2RoSmRsV1ZCUlVRRWpSb3pRNnVycUFBQ0VxVXIvSjFKVFUrR0REejZJdWFOYnJIYkZha2ROVFEwc1diSWt5dXltZVVTYW03djIybXZoN3J2dlJqbnZJam1GeUxTbnVpaWNSamFmL1g0L1pHVmx3VHZ2dklNVEprd1EycUM4ZDdKaEdEWnZkbloyTml4WXNBQnV2dmxtbElXcS9KNDg5ZkltVnJRTktRbktsb0RuQ0JubWR6Uk5nenZ1dUtQUndOeXlzaklrajJoamMxQk9mOFRFaVJORjZpekUvUUhLaUNqUzdsdVdoUjkvL0RGU1RzSG00SlFKMnVWeWlTUVNjcHA5TmVzMkl1TENoUXZ4ckxQT1FxY04zV1V2dFZ5UHorZUQwYU5INDdKbHk4VDVCSU5CM0xCaEE0YkRZVnRBdGdvZCsvREREMlA3OXUwQndPNHNVWk83MHZ2bWhNczgvZlRUVVJ2SXExQSt4cFpNRU1Fd2JaSjRoR0ZwYVNtbXA2ZUw0K1hmTmdWcFVxbXBxZkQ0NDQvYnlsVVRyWnFtaVlGQUFPKy8vLzY0YkRReXRSdHJWM1oyTm56enpUYzJ3YWRDZ216YnRtMzR3Z3N2aUJ5SDZ2bVJJRXBPVG9aUm8wYmg0c1dMc2FTa0JBM0R3RWdrZ3FacDRzeVpNM0hJa0NINDdydnYyb1M5Q2dsbjB6VHg4ODgveHpGanh0Z1NObEQ4WWMrZVBlSHFxNjlHVlFqR0l4VC85cmUvTlNvTUxjdkMyYk5uUndsRDFoS1pZeFluWVNndno5dTdkMjlNWWRqWXdGR2RBams1T2ZETk45L1lFbzZTWUpEckxDd3N4UFBPTzY5SmdSaExXTkgvNUhUL1JVVkZ0bDN1WklFa1o1NHhUUk4xWGNlZE8zZmk0NDgvamlOR2pNQWhRNGJnT2VlY2czZmNjUWQrL2ZYWFdGOWZMNDZUaGZxZ1FZTnNLYnkrK09JTFd6MXEvOUo1MDROZ3hZb1YyTHQzYi9ENWZPRDMrK0d1dSs3Q2NEaU00WEFZNTh5Wmc3SVRKeDZlZXVxcFJqVlR5N0p3MXF4WkxBd1poZ1RHOWRkZjd6aG82WDFaV1JtbXBLUTRiazRVYnozMGV2bmxsMk00SEJacmVKMkVvcTdyK000NzcyQnFhaW9BMkxPdk5HZkRLSGxRanh3NUVyLysrbXRIVFNuV2VjdUNUdjVlTnJkMVhjZVBQdm9JaHd3WlloUGVIbzhIMnJkdkQwdVhMc1ZBSUdDclQwMFNLN2RKbmo2Z3o5OSsreTNtNWVXSmpEdXhoSldhb0hiZXZIbU8yN1BLMzkxeHh4MDJZZWlVZ1p4aGpoa21USmhnRytTVWpoNXhYLzYvMHRKU0VmWkN4Q3VVMU9OOFBoOU1uanc1U2dDb2dzQTBUWHp5eVNmeFlBZW4zTzRlUFhyQXJGbXpzTHE2MnFiWkVmSm5kVTVURmFLNnJ1UDY5ZXZ4cHB0dXdzNmRPd09BODd4cHIxNjk0TEhISGhOOVNlY3E3NWRNZlM5L3BqeUVuMzMyR1E0ZVBOaW1jY3J6bXJHbUJ6Uk5neWVmZkZMTXg4cklmZnluUC8wSjVZY05lZEFaNXBpQ3R2bTgrdXFyUlFZV2ViRFFnS21xcWtLQWZjdk5EalJGdnh4YzdQZjdZY0dDQlZIQ2tPcW10bGlXaGJmZmZqdktBNVZRbDc0NVFmWEpyMzYvSHdZTkdvU2ZmdnFwWTMweTZpYnkxRDg3ZHV6QUtWT21ZRTVPanEwZjVYT1YrOWpyOWNJSko1d0FCUVVGcUNLbjVaZjd3TElzbkRselp0UisxVTc5R211VjBOTlBQeDJWblZzVmlCZGNjSUhRYU9rY0R1VTJEQXpUYW5HNVhIRHp6VGMzNmx3b0xTMUZlWUFjeUp5U2FvWU5HalFJdDJ6WjRpZ0U2TDFwbXJoMTYxWWNPblFvcWltK21vdmNadnA5Ky9idFlmcjA2Ymh1M1Rvc0tDakFvcUlpckt5c3hKcWFHcXlwcWNIeThuSXNLeXZEelpzMzQ2Wk5tL0RWVjE5RmlvVnM3QnpsK3NpMGRidmRrSktTQWhNbVRNQjE2OWJodG0zYnNLeXNUQWpFVUNpRVpXVmxXRlJVaE11V0xjT3p6anBMT0ZSa0lSV1BSNW5hOGVLTEwwWTkyRlQ2OU9ramZ0ZWNuZmVZd3d2UDRoNWlLTUQ1aVNlZXdMdnV1Z3NBOW1kY29WZkRNS0N5c2hMeTh2STBTblFLQUNLaFFGTlFXZXJ4SG84SHJycnFLcHcvZno1a1ptYUNydXVPR29saEdMQnExU3E0NXBwcnRJcUtpcWkxenZIVTdmVjZSZklIZGI5aFdnK2NscFlHeHg5L1BIYnIxZzBTRXhNaE1URVJhbXBxb0tpb0NMWnMyYUxWMU5TQVlSZ2lQbEZlQnkxRGdkbDBuUHAvcWo4dkx3K0dEUnVHM2JwMUU5bDFkdXpZQWYvNjE3KzBIVHQyQU1EK1pZVDQrd29XaXBHVTEzZXJ5TisvL1BMTGVPMjExd3JocUM3ajAzVWRNak16dFZBb0ZIVXRZNVhQTUVjdExwY0wzbi8vZmFFRnFrNENSTVQ2K25vODlkUlREem9lamJKT0UzNi9IeDU2NkNFUmp5ZUhtNmdPbHUrLy85NDJNcHVUckNEVzkwMmRpenhQcDVycTZ2eWNhcmJUbko2c0VhdkpJR1JrQWF2R0c4cDFxdHBuckRsRGw4c0ZyNzMybXMxWkk1djcxdS83UHFzYU85WFBEaFRtbU9Pc3M4N0NqUnMzUnBsUDhoeGFPQnpHVjE5OUZRL1VtNndLSkRXYjlTZWZmQ0tFc1NxSTZmdHdPSXo1K2ZuWXFWT25aZ1Vla3pDUlBhMnFtYXNLb2xnQ1ZCWkdzYlk1VlIwK0xwY3I2aGhWY01vUENWa0FxMU1UOHVvWHAvMlk1ZmRwYVdudzNudnZPYzRGMCtmUFAvOGNHeFBRREhQTU1IVG9VRnl5WklsdG9EaUZrcGltaWNYRnhYanJyYmRpY3liWG5lYnBuT2pRb1FPOC8vNzdHQXFGYkN0VTFFRWNpVVJ3dzRZTk9IbnlaT3pkdS9jQm5YTmpxMlNjMnVzMFIrZjBHNmYwV3JKV3B6b25TQWlwMmE2ZGtQZDJsbDlqL2Q3ajhVQ1hMbDFzY1k3eUsvWG5xRkdqVUsyWE41RnFuZkFWYVFHOFhpOWNldW1sMkxselp3Z0dnNUNabVFudDJyV0QzTnhjR0RKa0NIVHIxZzBTRWhLaThnSEtxYTFvem1yMzd0M3czWGZmd1pZdFc2Q3lzaExxNnVyQXNpd29MQ3lFenovL1hKUG40dUtkYzZJMXdOMjZkWVBycnJzT0gzcm9JZkY3V3M5TGhNTmg4UHY5WUJnR3JGdTNEZ29LQ3FDOHZCenE2K3ZCTkUwb0xTMkYxMTU3VGF1dnI3Zk5DeDZ0MER5aW5CQ1crdnpNTTgvRXQ5NTZDenAzN2l6V1BNdlh0YWFtQmpwMDZLQlJ1aldlSDJTT2F0eHVOL2o5ZnRpMGFSTUdBZ0hoTVpiTlg5a1VkWHF2bXEyNnJvdmxaL1IrMGFKRjJLRkRCMUZuY3lFdnBzdmxndHpjWFBqM3YvOXQwd3hsVDdmcUVhVzJCWU5CTENzcnc3NTkreDZUODEya2NWSmZYbm5sbFdoWmxsanRJMS9UU0NTQzgrYk53MWllZWRZTVd4OGMrWG1RbUtZSlBwOFBObTdjQ0pXVmxVSnpJQzJBc3ArUVZpR25rSEs3M1NMUG5xdzVVRXdkcFp3eURBTzJiZHNtdkx4eTJVMXBaMlNtUmlJUjRmSGR0V3NYREI4K1hPdmR1emVNR3pjT1R6NzVaT2pRb1lQd0JIdTlYbEUyWlhhaG5JR2tJY3A3clJ6TitIdytzVTgxOVQrbFB4c3dZSUJ3N0pDbm5xNVpYVjBkZlBUUlI0NGFvZXlzWVcyeDljQ1BwNE5Fam5XVDAwSEprQWxGL3lQQktHOXNEckIvYmtvTndVQkU4SHE5VUY5ZmI2czMzb0hrMUNhNURIcE5TVW1CMU5SVU1hZEZvVEtHWVVBZ0VCQkNNUmdNeHRjNVJ3bjBFS01IZzJWWmtKaVlDSnMzYjhhdVhidmEwb0xoNzNrWkZ5OWVERGZmZkxQVzBOQndoRnZQTUljUk5SeUR2cFAvWWsyYU4yYnl5czRCSnkvemdaaXFicmNia3BLU291cVJ2YWpVZnZtOTdOUWh6K3l4WUNxcmpnL2ltbXV1Y2R3SDJ6Uk5MQzB0eFI0OWVyQXB6QnhieUtzZmlLYThoWEk4SEhHb01wcFF1M3crbjYxY05lR0EzQTVaZ011b251RmpRUmdDMk5jU2E1b0dIVHQyQkZxUExLOS9EZ2FEV0ZOVGcrZWRkeDZxMTVBRkkzTk1RSm9oYVUrcU5xY0tFQ2Nobys0aVI0TEhhZWMyS3VkQUI1amFIbFV3eHhMc3NiU2tveG0xbnp0MjdBai8rTWMvTUJLSlJLVXJLeTh2eDl0dnYxM01YY2ozZzlPMVlnSEpIRldvZzZVNW5rTjE1VU5qZGNobE5YY1F4ZExnMUhJYjAvVFVPTHhqUlNzRTJIK3U2ZW5wTUcvZVBLeXZyN2VaeFpabFlXVmxKZDUrKysyWW1wb2FsK0JycXI4WmhtRU9PMnB3dVByWjYvV0N6K2VEcjcvK1dvUkt5U0ZKcG1uaWlCRWpzREZObm1FWXBsV2phbXl5dHBhWW1BakRody9IdSs2NkMvZnMyV1BMazJoWkZwYVhsK1B5NWNzeE96czdha3JrWUtZeEdJWmhqaGl5RnRldlh6OTQ3TEhIY01tU0pWaFVWQlNWaDlFMFRWeXpaZzFPbURCQmJEUkZqalJPMnNvd1RKdUV6R0Zaa3hzN2RpeEdJaEdiU1d4WkZ1cTZqcXRYcjhiaHc0ZGpabVltQUVRN3RRRHNxMzJZdGdNL3hwaGpHZ3BjbHdQWWRWMkhpb29LcUt5c2hFZ2tBc1hGeGJCeTVVcFl1SENoVmxWVkpWYWFVQkM2MysrSGNEZ3NQTytSU01ReHp5TERNRXlid2VmelFaY3VYV0Q0OE9HWWw1Y25NbCtUK1JzcnAySmpXWFlZaG1IYURFNG1MU1ZnZFpvSGxJT3dDZG43ekRBTTA2WndFbWJ4eEdXcXYrRUVyZ3pETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUVjdlJ6ekhrTG92TFVEek5qdHFLazFTYTlsOUxGWTdXMHY3bUFPanFldDZwSGZBYSs3NFVJOTNhcnQ4RE8yb1NMczBIdW56UFJpT3VEQlVvYldlNmc1eGg0b2pMVXhiTXVlZFUxbHkrNTJ5YWpmMy9PSVpMQWRUWG5OcHF2NldhbCtzY2xyNy9YRTRCSk5USHgzdWNkd1N0QXBoNkhhN1lkU29VV2haRm5nOEh0QjFYV3k5S1dmK2FPN3VjdkhRMU0zV1ZINDZwNDJjNURMVnphRmlMZW8vMFBycC9CdmJXaUJXMitJWmFPcnYxTjhmYkpxcVNDUnlVTDl2S2pOTVUvdEtOelZZbmJadGpmVlozaStiUGpkVi84RzJyeW5pRllaTzJYdGluYXQ2aks3cjhORkhIMmswWHIxZXI5aGp1aTNSS2xKNFVSb2tnSDNaaFNPUkNMaGNMdEIxWFd4TFNiUzFYY2Rvc0RkWENCMHNWSWRsV1FkVnQ1Tm1LWDgrMUpwSFd6SzVuQVFKUGF4aW5VZFREL1BEbVN6V1NkZzVXUkZPRHdENW9VaUNVSjc2YWdzY2NVbEMyb1ZUcHpYV21VMlpMeTNadnNab3F2NURiWVlmYVRPL3RkTlMxeS9XY1FkYmZsT2E5YUhPaVhnd1V5Wk9xTlpjVytLSWE0YnlCVkNGWDJPZDJsSU9scVpvaVJ2a1lNcG9hV0hjM0xhb2c3VXhNL0ZRY0xEbmY2Z2ZWbzM5UDU1N3J5bkJjVGp2MzZibW5KMGdrMWdXZ3JUbmRsTlRBSzJOSTY0WmtnQWtqeFFsMHJRc0MwelRQT0thVDBzSW80TVpNQWQ2ZnZGcXpvZGE4MjJLMXE2NUhzekQ1SEJaTDRlU2VPNFB5dlN0d21ieUFSQ1BPVXcwRlFxZ2NxUnV4Q001RU9JeGZRNjBmUzN0VFdZT0xjMTVXQjNNdFNSbGhqVEV0bXd1dDJsY0xwZHRJbHFkZEZZMy9DSGNibmZVSmo3eGJPcnVWTC9hQnFleURtUXlQRmE3blhEYVlMNnhEYzNqUFVjMWM3TnNPcXNiSWpXbjM1eTgwZXBtOWVyeFR2VTRaWmh1YkF0UUtsL3VCL3EvN0oyUDkxelV0c3FmNVhMVWRzZnF4MWpuRUMveU5nV05lZnhidS9PUmFTYnlSWGNTUms0WG5JU1gvSm1POTNxOWpvSXRGazZDU2hVUU5QQU9SaWlxNTlkVVNJMzhPem9mcHpDZ3BsQUhOdUdVRHAvYTVIYTdoV0NLUjZpNDNXN3dlRHd4QmF2SDR4RjlLTmVwQ2pDcXora1lwM05RZjB2dktiS2h1V0ZEaloyRFhCYWREN1ZIdlM3cXRXb00rajNWcGRZcEMzd3FsL1oxWVk1UzFFSGdwRG1veDNnOEhuSGpPKzFiRWM5ZUZqVFlaVTFEclYrRmJ2UjRibmgxc01nM3VDd1FhVUE0YVdleDRoempIUkErbjgvMk82ZnRNWjMrUjc5Ui85UzJ5ZGRFN25OVjRLc2J2QU9BVFVpcS9lcjMreDNiSlF0dE9sNnRTdzNwYWd5eUtwejZSVVVXaENxeHRPUjRpS1V0QS9DZUxNY01UaHFUa3puU21GYWsvaS9XSmtEeElBZFprNmJnWk1iRlc3NTZIdXJnVnM5TlBRKzFiVTREdHpGaW1hc0pDUW0ydHFsN0JkTm5wOS9HMHFabFRZL2FwMnBiQUkwUGJuV2pKdlZCSWd0TnFrdWx1ZWErWEk5cXFaQzE0ZFNQY2grcEpuNjg5VHVaOTFSWFk5TVo2ditabzRSNE5SM1ZaS0NienVsR2JrN2RWQjc5M21uQXFScFV2TWptajFxZWs4YW5ta3J4bXRQeHRNUEpmSE9hNDJ0S0UybXNMYkVlSHZJMVU0K2xxUTIxVGZUcUpJemtoNmlxVVRzSjBsZzRUUWVRMXFxZXEydzlxQnRJT1puOHpkWG9ZZ20vV1AzTm0xZlphZk1UQjA3ZUs1ZkxaVnNhbFppWUNFT0dETUdCQXdkQ2NuSXlKQ1FrUUhWMU5mejg4OCt3Y2VOR3JieThIRHdlRDVpbUthTHBxYnltUEdJdWx3dDY5ZW9GZ1VBQWtwS1NJQmdNUmoybEE0RUFHSVlCWldWbFVRdmI0MEVPTzFJRDFIMCtIK1RrNUVDZlBuMndVNmRPQUFDd2QrOWUyTFp0bTFaVVZBU2hVRWljQzdWSDEzWEhzaHFEUFA3VXQ3MTY5WUxldlh0alJrWUdKQ1ltd3M2ZE82RzB0RlRidEdrVFdKWUZpQ2dHb1ZNRWdKUG5VbDdQbXBpWUtKWmxCb05CMFZiNnJScUJRTjhuSlNWQklCQ0FsSlFVc2ZvbkVvbEVIVS94Y2ZRN3VVMnBxYW1nNnpvZ29pZ2pscWRWdmRmOGZyL3RHcE9IdGJGRUJqNmZEMHpUdExXSjdzVjRJTUZ1R0FaNHZWN28xNjhmbm5qaWlkQ2pSdy93ZUR3UWlVUmcrL2J0c0hidFdxMm9xQWgwWFdkUDc5R01iRllSUFhyMGdEZmZmQk8zYjkrT3RiVzFXRlpXaHBGSUJCRVI2K3Jxc0s2dURvdUtpdkJmLy9vWFhuYlpaZUxPYTQ1bWtKR1JBYnQzNzhhYW1ob3NMaTdHdlh2M1ltVmxKZTdac3dmTHlzcXdwS1FFUzB0TGNmZnUzYmh6NTA1Y3ZudzVYbnJwcFJqdkpMYXFNZEQ1dFcvZkhsNTY2U1hjdVhNbjd0MjdGMnRxYXJDK3ZoN3I2K3V4dHJZV1MwdExzYkN3RUY5NjZTWE15c29DQU9lNXJhYVF6YTdNekV4WXVIQWhidDY4R2N2THk3R2lvZ0lEZ1FEVzFkVmhUVTBObHBXVjRhNWR1L0RsbDEvR3pwMDdDM1BYYVo1UTFjTGs2M2JsbFZkaWFXa3BGaFVWWVdGaG9iZzJUdWFmMms5ang0N0ZvcUlpM0xseko1YVVsT0NVS1ZPUTV2L1UrVW01UGZUNzNOeGNLQ3dzeEowN2QySlJVUkdTZ0c3c1dzbWE3UExseTdHd3NCQUxDd3R4OXV6WjZLVGx5dTlQTyswMC9PNjc3N0M4dkJ4WHJseUpKNTEwVXRSOTJCaXlCZkQvL3QvL3d4MDdkbUJ4Y1RFMk5EUmdLQlRDWURDSTRYQVlnOEVnRmhjWDQvcjE2L0dCQng3QTVzeUpNbTBFdXBGbGN5TTVPUm5HangrUG9WQUlUZE5FeTdMUXNpd01oOE5ZVVZHQmUvYnN3ZXJxYWpRTUF4RVJEY05BeTdJd1B6OGZjM0p5bWxWL1VsSVNJQ0thcGluS0lYUmRSL2wvaEdWWitNTVBQMkNQSGozaXFrUFdOQk1URStIUGYvNHoxdGJXb3E3cmFKb21CZ0lCM0xObkQrYm41Mk4rZmo3dTNic1hnOEVnbXFZcGhQK01HVE93ZmZ2MkI3Uy9iMlptSmt5ZE9oVXR5OEpRS0lTSWFLdHoyN1p0dUh2M2JneUh3NksvUTZFUS92ZC8vemY2Zkw1R2sxV281cjdiN1lZeFk4YUlmclFzQ3lzcks3RjM3OTdpdDNJNThubjA2OWNQcXF1cmJmMS96ejMzb05QNWtrQlNweSs2ZHUwS1ZDOGlZaXluRktGNmlYLysrV2R4elUzVHhFY2ZmUlF6TXpNQklIcGVGV0NmTUN3c0xNUkFJSUJidDI3RlFZTUdOVXNZZWp3ZXVPaWlpL0RYWDM4VjF3VVJNUktKWUhsNU9lN1pzd2RyYTJ1eHJxNU9mSStJV0ZaV2h1UEhqK2NnVVlranZoenZZQ0VUaDB5b3BLUWtlUERCQi9IT08rOFVac2JHalJ2aHd3OC9oSktTRWlnb0tJQklKQUlkT25TQTNyMTdRNzkrL2VEaWl5K0d0TFEwK1BUVFQ4VWk4M2hOV2JwaEtlUE92SG56d0RSTnNDeExtR1lwS1NuUXBVc1h1T3l5eThBd0RQQjRQREJvMENCWXVIQWhqaGt6UmdzRUFxQnBtaWpETUl5b1BJOHVsd3Y4ZmovTW5Ea1RwMDZkQ3JxdWc4ZmpnUTgvL0JCV3IxNE4zMy8vUGV6YXRVdlROQTI2ZCsrT2d3Y1BodUhEaDhQbzBhTWhGQXJCOU9uVDRZUVRUc0FwVTZabzVlWGxva3k1blFBUTliNWp4NDR3ZCs1Y0hEdDJyT2pqdDk1NkM5YXRXd2RyMXF5QjR1SmlUZE0wNk5hdEd3NFpNZ1RPT09NTUdEVnFGUGo5ZnBnL2Z6NE1IRGdRSDN6d1FhMnNyRXlZaUxLWkt5L2hRa1RSTGxselRFMU5oUmRmZkJHdnUrNDZyYVNreE5ZLzFLYk16RXg0OWRWWE1UMDlYZHdUOGh5bmJHSnJtaWF1TTlWUDk0cXFzZEoxa2Y4dnY2ZnBCL3FPekdxNmx0T21UWU9FaEFTY08zZXVWbEpTWXF1VEhoSTBMU0RYcXk1em8xYzVJNHpMNVlJcnJyZ0MvL2EzdjBHblRwMUExM1dvcjYrSC8vdS8vNFBmZnZzTnRtM2JKcVp2ZXZmdURUMTc5b1FSSTBaQVhsNGVGQllXUWxGUmtlMWVsdTk1RHBwdWc2ZzM3dzAzM0lDVmxaWGk2VHh2M2p3OC92ampiWnFCUE1HZGxwWUdJMGVPeERGanhtQlNVaElBTk0vTGxwS1NBcVpwaXFkdVJrWUdBT3lmdFBmNy9aQ1FrQUFkT25TQW9VT0hZaUFRRUUvbjR1SmlQUHZzczhYVFdZMnRVNWsxYXhaYWxpVzB6SWtUSjJLSERoMml2T0gwbDVtWkNRODg4QUEyTkRRZ0ltSTRITVpISG5uRVVSdHdNbVVCQUtaT25ZcUJRQUF0eThLYW1ocWNNV01HZHVqUUllbzNKSGl5c3JMZ3BwdHVFbHA1SUJEQW1UTm5vcXlCT1ptZDh1Yy8vdkdQUXJzekRBTU53MERUTklXV3B6cTZORTJERjE1NFFSeEw1MnFhSms2Yk5pMUswMnBNNCtyV3JSdWdoSk5XNi9TZVlpdlhyVnNuN2oyNlZvRkFBSjk5OWxsTVNVa0JnR2d6ZWNlT0hZaUltSitmajBPR0RCSHRqYVhGeTFwc1ZWV1YwSUtycXFwdzhPREJvaDdaSys5eXVTQWhJUUZPUFBGRWVQVFJSN0ZuejU3Q29STXJIT3Rnblc3TVlVWWVaRGs1T2ZEVlYxK0pBZkgyMjI5alVsSlNWREF0UUh5ZTQzaUVZbHBhR3BCSlpab21wcWFtUnYxV0R0Vlp0R2lSTU51cnFxcHNwb284U05TNisvVHBBNy84OG9zdzMvNzYxNytpR3J5ci9wWmVQL2pnQTJFK05UUTBvT3g5VmIzT2hLWnBrSktTQWhVVkZVSzR2UDMyMjZqK1RnMVFkN3Zka0ppWUNETm56aFRUQkpzMmJjTCsvZnMzZW42eGhLRnM5cG1taVgzNjlMSDl6dVB4d1BqeDQ3R3lzaElOdzhCSUpDS0VQeUxpZmZmZGQwaUZvZnpuOC9sZ3c0WU5HQWdFTUJnTTR2YnQyOUd5TE5FUHp6enpUTlFEOTl4eno4WGk0bUswTEFzM2I5Nk1nd2NQUnZrK3BUNVYrOW5uODhHU0pVc3dFb21nWVJoWVgxOXZtd2VVNTBHOVhxLzRMZVVKamNXeExBVGJmS0NSYk9ha3BLVEFhYWVkQmk2WEMwS2hFQ3hac2dUQzRiQzR3T0Z3V0FTOXV0MXVTRXBLZ29TRUJNakt5b0xrNUdUSXlzcUN0TFEwRVNZVGo1bEFwaElBUURnY0ZnTmVEck9oLzE5NDRZVjQzbm5uQ1ZPdHBxWUdkdTdjS2NxUzg4Q3BkWjl6emptWWw1Y0htcVpCZm40K3JGeTVVaVN6b0hiSWJaTDc1cXFycnRLb1BRa0pDZkNmLy9tZktIdmVBY0NXRklQTXZxRkRoMkpXVnBid3h0NXh4eDJhYkJiS3BpME5NdE0wUWRkMVdMWnNHZXpac3djUUVYcjA2QUVEQnc1RThvaXI3VzJNSDMvOEVhWk1tU0s4L0JzMmJNRGpqejllUE5neU1qTGd4aHR2aE9Ua1pIQzVYUERVVTAvQnh4OS9MSzVIYytvNkVNZzh4dDg5ejZGUVNGZ2V0OXh5Qzd6NjZxdkNtM3pycmJmQ3M4OCthOVBtdytHd01MWHBuR1N2Ti9XcG5MaUVndUI3OSs0dHdvcW1UNTh1VEhTZnp3ZUlLTXhyT29idTdhU2tKTWpNeklSMjdkcEJjbkt5N1h5T1ZVRUljQlRNR1pLdytWMlRRWUI5RnpRU2ljQy8vLzF2VFJZV2RGTysrZWFiNlBQNVJMWU5DaUIydTkxUVdsb0tEenp3Z0xacjE2NjQ2ZytId3lLc0lURXhFV2JObW9VMEQwUmFoZHZ0aG5idDJzSGd3WU9oYTlldUl1U2lvYUVCZnYzMVY0M2FoMHFJQjMwUHNNK0prWktTQXBabFFYRnhNV3pldkZrSUpnQjdLaWoxbk1QaE1HemJ0ZzM2OWVzSExwY0x6anZ2UEhqenpUZkZmSlZhRjUzUGlCRWp3TElzOFBsOFVGUlVCRFRuUllLSkhnUXVsMHVFb25pOVhqQU1BMzc4OFVldHRMUVV1M2J0Q242L0g5TFMwbXkvd3pqRFJyS3lzdUMxMTE3VFRqenhSSncwYVJLNDNXNllNMmNPWG4vOTlWbzRISVo1OCtiaDJXZWZEWlpsd1U4Ly9RUXZ2dmlpOXRoamp5SCtuazNsY0F4dUVsaCt2eC84ZnI4dFljSE5OOStzVlZaVzR0U3BVeUVTaWNDRUNSUEE1L1BoZmZmZHB4VVZGVUZDUW9JSXJTRkJKNGZoeUE4dHVsNlJTQVQ2OXUwcjdnZVh5d1g1K2ZtaVBTUVVYUzRYWEhubGxYajExVmVEMStzRlJCUjFVU2paM1hmZnJXM2N1Rkg4TmxaNDJyRkFteGVHOG9SdlltS2l1R0hvQnZYNWZCQ0pSR3dYOXB4enpvSE9uVHVEcnV2aUpnSFlkL1A5OXR0dmtKS1NJcjV2eW9sQ1QzUFN2RzY2NlNZaGhGUm5BRUZDY3N5WU1WcHRiUzBBMkRVTUVvcjBXVzRMeFFsR0loRnhRMVBiU2R1UUoveEpLRk84R1QwRVpJRWtPMDFJbzlBMERkTFQwMjBUK2pTWUlwR0lUWkJTV1NRVXFRMDBjSDArWDVTamh2cXNxZjZ0cmEyRit2cDZtRHQzcmpaczJEQWNNR0FBWEhUUlJUQisvSGlzcXFxQzhlUEhpL2FlZnZycEdtbE5jcDhlU3VnY2FQNHRIQTdiQXVVdHk0Sjc3NzFYeThqSXdCdHV1QUVNdzRDcnI3NGFFQkd2dSs0NmphNFZQYWdwNUlydUF5cUQ3Z242VEhHSWRJL1I5M0k2TFVTRWJ0MjZ3V1dYWFNiYXF6ck9aczJhaFlpb0Fld1hnTWNxYmQ1TWx1Zk15c3ZMTmZJMGhzTmh1T1NTUzlCcGo0MWZmdmtGVnE5ZURkOTg4dzBzWDc0Y3RtL2ZicnV4M0c0MzZyb2VsemRaenVObUdBWlVWbGJDbmoxN1lQZnUzVkJXVmdiRnhjVkFaVm1XQllXRmhiQnc0VUxvM3IyN3RtM2JOc2U1UGhWZDE0VzViMWtXOU9qUkEvcjI3WXR5KzBpSXlZS1FTRWhJZ083ZHU0UFg2d1hUTk9ITEw3KzBwV3BYaFFlVnNXclZLbEZ1VmxZV1pHZG5DNjJEaEN3ZEQ3QS9kTVRqOGNEQWdRT3hVNmRPNFBQNVFOZDFLQzh2anpxL2VQcVhoUEQyN2R2aHlTZWZGSjdrbVRObndxSkZpMERUTktpcnE0UExMcnNNUXFHUTBMRFVQNW1XSFBDa0JacW1DYUZRU016dkJRSUIwWGNBQUpNbVRkTGVmdnR0Y2I5T25EZ1JYbi85ZGN6SnlSSFhsc3hadVgxeXNEeVp5eTZYQzJwcWFpQVVDZ252OHRDaFF3RUFoQWNhWU45MUxTMHRoVldyVnNFWFgzd0JxMWF0Z2kxYnRvaHJweTRxb0xMcC9iRXNHTnNrOHMyZWs1TUQzMzc3TFpKM2Qvbnk1ZGkxYTllb1kybWVoTXlhdSsrK1cweVk1K2ZuWTY5ZXZlSmVPNXlTa2lMaURFM1R4REZqeHVEbzBhTng1TWlSZU9HRkYrSUZGMXlBNzc3N3JuQjhyRm16Qm84NzdyaW9DWEZxbi9wSzcwODk5VlRjdW5XcmNDYk1uVHNYNDAwazhmVFRUNHY2YTJ0ck1UVTFOZTZBOG5BNExCd0FjK2JNRVVIRWFueWQ3TFJKU1VtQnVYUG5ZakFZUk11eThKZGZmc0dUVGpvSlplMDFYbS95NnRXclVlNm52L3psTDhKVGk0Z1lDb1Z3M3J4NVNOYzBOVFVWM243N2JmSDcrKysvUDhycDB4Z0g2azBHMkhjL2taTXJHQXppUlJkZGhBRDdIV01KQ1FudzlOTlBJOTB2dXE3ajExOS9qWFYxZFdpYUp1N1lzUVBQTys4OGxQdFNyb2VtWEFEMmFmTkxsaXdSbnZiUzBsTE15OHVMT2grZnp3ZUppWWxpVHZlbW0yNFM4YW1JaU1PSEQwZTUzTWJPajJrRDBFWHplRHp3d0FNUENBOWJLQlRDVjE1NUJaM1NKZEVONm5hNzRiNzc3a1BFZmVFSlc3WnN3Zjc5KzhkZGQzSnlNdEJ2RVJIVDB0S2k2anJoaEJQZzExOS9GZDdPdi8vOTcrSTQ5VmhxazNwdW1xYkJQLzd4RDR4RUlxanJPZ1lDQWJ6bW1tdEVVSEFzN3JyckxsdkE3YTIzM3Rya2IyUWVmdmhoTWVCcWEydHgwcVJKWWw1V2JxdThVbWI4K1BGWVhWMk5wbWxpTUJqRVo1OTkxaVpVMU1HdWxqZDY5R2doN0w3ODhrdWJBTlkwRFo1NTVobmJ3NHVDbWowZUQvaDhQbmpublhkc1huZjVmTlIrVmg5SWVYbDVRaGhhbGhVbERPVzJxbmc4SHZqcHA1OFFFYkcrdmg1SGpCZ1IxZGVabVpudy9QUFBvNjdySXFxQTJycHQyelk4Ly96em83emZUaGwzdkY0djVPYm1pbnZQTUF3c0tDZ1FEMW9aT1NHRWt6QnM2cnlZTmdiZEFBa0pDZkRwcDUrS0FZeTRMOXArMUtoUjJLZFBIK2pTcFF2MDd0MGJ1bmJ0Q3NjZGR4eWNmdnJwU0Rjd0RTNDVCcXNwTWpJeWdHN3NZREFvWXZBSWV1cE9uanhaaFB6b3VvNjMzSEpMbEFZQUVIdHhQZzNLclZ1M29tRVlHQTZIRVJIeG5YZmV3VUdEQm1IMzd0MGhPenNic3JPem9XdlhydEMzYjE5WXVIQ2gwRmlEd1NBdVhyd1lPM2JzYUd0YlUrVGw1Y0hISDMrTWhtRUlUZStGRjE3QVBuMzZRTGR1M1NBckt3czZkdXdJWGJ0MmhVR0RCdUc3Nzc1cjAzeldybDNyYUd1cFFsUm16Smd4b3A4Kysrd3ptL0QxZXIzUXUzZHYrT0tMTDNEejVzM1lybDA3MGNma2tYM25uWGVFMWtoeGhuTFlpWk1RSmtIYnNXTkhRRVJ4cmVJUmh2TEQrS2VmZnNKSUpJS0JRQUF2dnZoaXNmcEYvazFxYWlxOC8vNzc0djRrU2twSzhPS0xMMGFLV1hTcVQwMklNWG55WkF5SHcyTDFVME5EQXo3ODhNUFlwMDhmNk5XckYzVHAwZ1c2ZCs4T09UazVjUGJaWitOYmI3MGw2alZORTRjTkd5WXNqTWFtRnBnMmdqcXcycmR2RHkrLy9ES0d3MkcwTEV2RXFEVTBOR0JoWVNGdTNyelp0azZaYm83UzBsSjgrT0dIeFFDTGg5L0RHTVRnVFU1T2p2b3RQWm1mZSs0NVJFUng4OTU0NDQwb2EzN3FPY2thSTUxamNuSXkvTy8vL2krV2xwYUtRV1FZQm03ZHVoVlhybHlKWDMzMUZlN1lzVU1JSTlNMHNhYW1CaDk2NkNHYkdkWFlFak9WL3YzN3cvejU4MjFML0F6RHdQejhmRnkyYkJtdVhyMGE4L1B6UlV4Z09Cekc3ZHUzNDNQUFBTY0VvV3FHT1FrVU90ZlJvMGVMNjdKMDZWS2s4QkRDNC9GQVZsWVdkT25TUlh3bS9INC92UDMyMitLYVB2VFFRMUhDbUV4R2VYMDR6Y3ZsNU9RSVlXaFpGanIxazJwS3l0ZUlIcXpoY0JqUFAvOThsSStWajB0UFQ0ZEZpeGFKZXhRUnNiQ3dFQys4OE1LWWZlWlVmMUpTRXR4MjIyMWlDYWE4N0xPMHRCUzNiOStPaFlXRkdBcUZ4UDhvZ0g3cDBxV1ltNXNiOCtIQXRER2NCclRmNzRmVTFGUVlQWG8wcmxpeEl1b0pMR3N1aVB1Q2dxZE5tNFpEaHc0VlFiSHhrcEdSWVF1NlZqVkRtWVNFQk50OFZtVmxKVjV6elRWaXdNZ3JZOVNnYlptMHREUVlObXdZenBrekIzZnYzaDExYnRTV25UdDM0b01QUG9qRGh3OUhPWjZzdWFtYlNCQ05HREVDNTgrZmoyVmxaVGJ0Z2dZWTRqNVQ3Lzc3NzhkVFRqbEZKS05RcHlib3MxT3VRcmZiRFdQSGpoV0M5ZjMzM3hjYXRPeWdVZHRHeHlRbEpjRjc3NzBuVnFEY2Q5OTlLQ2VvY0xwZlpJR1htNXNMdFA0NkhtRkkrSHcrOFBsOHNIYnRXcUdoWFhMSkpXS2VWRTB5NGZWNklTc3JDMTUrK1dWeEg1YVVsT0RsbDE5dVcyVkQ5VGs5TEtudkVoSVNZTWlRSWZqY2M4OWhUVTJORU9UeSttNTZXTmZWMWVHQ0JRdnd2UFBPaTFxOXBKN2pzYVlkdHZtemxjTTE1SFdiQVB0REJUUk5nNkZEaCtKcHA1MEdQWHIwZ05yYVdpZ3VMb1pmZnZrRmZ2NzVaeTBTaWRnOHNmSjY0SGhTZUkwYk53NnJxNnNoSVNFQi92blBmMnB5T2lieTRGSTVmcjhmc3JPelFkZDE4UHY5RUF3R29heXNUSGdrQWZiZDNPU1pwTy9VSGNoa0V6czVPUm02ZCsrTzNidDNCOHV5WVB2MjdkcXVYYnNnRUFpSTQ5WFlQdGxyMk5UNXFXRXhMcGNMMHRQVElTOHZEN3QyN1FxUlNBU0tpb3EwclZ1M2dtRVk0amlLQVkxVmg5d20rWDIzYnQzZzlOTlBSMTNYWWRldVhmREREejlvVkQ4ZHA0YnB5RzA5OWRSVHNVdVhMdUJ5dVdETGxpM3d5eSsvYUdxOUZLSWtwODJpT2NkTEw3MFU2K3ZySVRVMUZkNTg4MDBONC9TcSt2MSs2Tnk1TXlRa0pFQTRISWFLaWdxb3JhMk51ZlliWUovd1B1T01NekE1T1JsQ29SQjgvLzMzV20xdHJRaGZvcnJsT01QRyt0SHI5VUt2WHIzZzlOTlBGNDdBb3FJaTJMcDFLL3o2NjY5YWFXbHAxQ2J2TkU3a2U1QnBvelNsNlRpWlpoU1BGc3RaRWE4M1dhMjdxZldrcENVNHJhK045WlNXVFVRNWppNVdPK1NKZC9KYXk5clZnU1N4SmRUVVQ2cUoxVmpiVkEweFZ2dGwxRzBhbk9xVkhRU3gwb2JGeWxyalZKNjZpcWd4bk14S3ArdW9KbTJWUGNYMFA5a3lvTitvWm5aajV5WDN2MnhpcSthMldrNnNlNVZwZ3poTk1NdmVZa0tkZTZMdkdqT2Y0aVZXbXZ0WVhtS24vOHMzcm14YUFkalhWYXZ0ZFVvTkZVdmdPSGtvbTBJZVBQS0R3bW0vRXZtOTB6d2gvVlplSFNJUFl2azlIU3YzclNvd2lLYXVYNnk1c0ZnUHMxaGx4cXJYcVM1NndNbk9IN1Y5alRuUm5CNlFzb05EWGFzc2x5dTNRL1pBTzVXbkVtdXVrbW5seURlTStzU1RKNnpWNzVzYUtNMFJoRTQzbU5NTlJkODVEVGluOWpqVm9aWVhTek9nMzhqOTR5Um9ta0oxRmpUMjN1bWhJczhQT3JVLzFweWMrcDBxU0p5Y1MzSzhvM3ArVHYzbmRLL0UrbjBzMVA2UHBYVTdoWGM1WFF1bnBDSk5PYnZVKzBydGw4WTBkcWY3c3pGUFA4TXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13VEN2aS93TXZ0T1Q3WHl4MmZBQUFBQUJKUlU1RXJrSmdnZz09IiBhbHQ9IlImYW1wO0ogR3Jvb21pbmciIGNsYXNzPSJsb2dvLWltZyI+CiAgPC9kaXY+CgogIDxidXR0b24gY2xhc3M9Im9wdCIgaWQ9ImJvb2tCdG4iPgogICAgPGRpdiBjbGFzcz0ib3B0LWljb24iPjxzdmcgd2lkdGg9IjM2IiBoZWlnaHQ9IjM2IiB2aWV3Qm94PSIwIDAgMjQgMjQiIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyI+PHJlY3Qgd2lkdGg9IjI0IiBoZWlnaHQ9IjI0IiByeD0iNiIgZmlsbD0icmdiYSgyNTUsMjU1LDI1NSwuMDgpIi8+PHJlY3QgeD0iNSIgeT0iNyIgd2lkdGg9IjE0IiBoZWlnaHQ9IjEzIiByeD0iMS41IiBmaWxsPSJub25lIiBzdHJva2U9InJnYmEoMjU1LDI1NSwyNTUsLjU1KSIgc3Ryb2tlLXdpZHRoPSIxLjUiLz48cGF0aCBkPSJNOCA1djRNMTYgNXY0TTUgMTFoMTQiIHN0cm9rZT0icmdiYSgyNTUsMjU1LDI1NSwuNTUpIiBzdHJva2Utd2lkdGg9IjEuNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+PGNpcmNsZSBjeD0iOC41IiBjeT0iMTUiIHI9IjEiIGZpbGw9InJnYmEoMjU1LDI1NSwyNTUsLjU1KSIvPjxjaXJjbGUgY3g9IjEyIiBjeT0iMTUiIHI9IjEiIGZpbGw9InJnYmEoMjU1LDI1NSwyNTUsLjU1KSIvPjxjaXJjbGUgY3g9IjE1LjUiIGN5PSIxNSIgcj0iMSIgZmlsbD0icmdiYSgyNTUsMjU1LDI1NSwuNTUpIi8+PC9zdmc+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJvcHQtdGV4dCI+PGRpdiBjbGFzcz0ib3B0LXRpdGxlIiBkYXRhLWkxOG49ImJvb2tfb25saW5lIj5Cb29rIE9ubGluZTwvZGl2PjxkaXYgY2xhc3M9Im9wdC1oYW5kbGUiIGRhdGEtaTE4bj0iYm9va19mbG93Ij7Qn9C+0YDQvtC00LAg4oaSINCj0YHQu9GD0LPQsCDihpIg0JzQsNGB0YLQtdGAIOKGkiDQktGA0LXQvNGPPC9kaXY+PC9kaXY+CiAgICA8c3BhbiBjbGFzcz0ib3B0LWFycm93Ij7ihpI8L3NwYW4+CiAgPC9idXR0b24+CiAgPGRpdiBjbGFzcz0iZGl2aWRlciI+PHNwYW4gZGF0YS1pMThuPSJvcl9jb250YWN0Ij5vciBjb250YWN0IHVzPC9zcGFuPjwvZGl2PgogIDxhIGhyZWY9Imh0dHBzOi8vd3d3Lmluc3RhZ3JhbS5jb20vcmpfZ3Jvb21pbmc/aWdzaD1NV3htZEhOcWNYRmthbk52YlE9PSIgdGFyZ2V0PSJfYmxhbmsiIGNsYXNzPSJvcHQiPgogICAgPGRpdiBjbGFzcz0ib3B0LWljb24iPjxpbWcgc3JjPSJkYXRhOmltYWdlL3BuZztiYXNlNjQsaVZCT1J3MEtHZ29BQUFBTlNVaEVVZ0FBQUhnQUFBQjRDQVlBQUFBNVpEYlNBQUJabDBsRVFWUjQydTI5ZVp4bFYxVTl2dlkrNTc3M2F1cXFIak4wUmpLUk5CQWdrSUdwT3d3QlJCSUVxeEVSQVpXZzRnQmY0T3NRc2JwQUVFVkJGSlNnb0NDS2RDRmpRRUFnM1dFS2tJUUVTQ0FFU0RwSnA5TnpUVys2OTV5OWZuK2NjMTlWTngwbXcrRDN4OHVuVS9PcmVuZmZQYTI5OWpyQXp4NC9lL3pzOGJQSHp4NC9lL3pzOGJQSHp4NzMra1ArWDNzOUJMQUZVd0lBRzNDVDNJZzkrVFZ1T3V4YnQyRUQxdkZHbkVVQW1NWTA4eGY0TXdQL2xQenRVNWlTRGJoSjF1SXMyWVFOVkd5Ty9COWVEQU4xRzdib05nQTM0U2FlaGJNNGpXbjdtWUYvREk4cFRPa21RRGRoQXdXYjR4RytwVGdSSjQ0OFovejhpWEdPck9sQjF2UXJHd1cxNFZ3Qmp5QVJoZ0JXMFdBbTFoMTJ4ZjVkMkhmZ2pRdnYyd09nQzZEL25SZEo4QzY4eXdIQWpianhmNVhCZjlvTkxGc3hxWk9ZaE9McGtjdWo1eVRjbjMxczh2NzNpV3RQYllzOWFneWpSNjBJeFFiUUhUV2t6YWFxdGdyeGhVY0RGSUdJQWhBWURBUVFqYWhncUdCV3hhcmZ0bjRiaXYxdHFXNitDd3Q3V2lpK2ZudTE5Nlp2NmJkdXZLTDNtVHVXaDI2Q3NnVmJIQUQ3YVRlMi9MUjY2aFpza01ORDd1dEduM1htaUI5OTdOclF1bUNOalQyNENYOXlVUlNOSmd2QWdHNE1xS3FBYURBQllqU0RBVVlJSTB3SUFWT2FGb05RVkVYRXFhaDZMNFdJZW5pbmdDb3FBSDFXV0lpOUEvdGxkdWVjTFg2bXNLSC91aDQ3dnZHMjhwMWZQOXpZMDVpT1A0MzVXMzc2dkhXclNUSUNjQ0phcjl6M3l4ZWNMcXN1SHRXaFRhTnNuamt1dzAxWENhcXlRaS8yRVJCREJSSk8xSTE3OFN0RzRjYWEwaHhybWh0dmlRNDNSQXBQaXFnSkNBaWlHU3dHcTlxVmxBY3JsQWQ2VXJVRHk0VVMxckZvQVZBNmVHMUlVVFI4VVRRQUp5aER3RUczV0I2d3VldDJZK0hEZDdvN3JuanIzSDkrYWRtTjZYL2F2RnArV2d6N2RNd012UFZYVjE2NDRkSGg1R2NjeDRsTHhqbDh2d2tNb1l3QjdWNG5WcWlpTlZYODZpRWRPbVcxREIrN1NvZFBIbWRqelFvMFZ3N0REemVnNGdBU29ncUprdnhLQk5UMGNpbUFCTktNUWdVSmFveUcza0tmdllOZDl2YTJNWC9ub25UM2RXVmh4NEwxOXZVWUYwbW5xczJSSWJTS2xocUIyWExXRG5EeFV6dXc4MS8vdnJqOFBYTnpPQWdBVzdIVmJjWm0rMm53Nkorb2dZbEpwM2gzeXExbm9mRTNPNTc5MUhYYXV2VDR1T0w4NDNWaXFGOVZhUGZib1llK3lWaExoczlZclNQbkh5MFRKeDhyUTBlUHd6VUxhQ2xpL1VoV1VXSnBaR1hDWUxSbzZmSlNBUk1oU2FvSUFZSUFVMElYQUJCUkVhK1FRb2hDb1E2UXdrSEVvZXBYWER5d0tBZS9mVkQyM1hTUWMzY3NXUGRnRlNVSXg1b2pycWtOMzJlRm5kaTdZMmZZKythUDQvUC85cm5lNTNiVUh2MlREdDAvRVFPbkhMdUZPUlEzM2o3MnE4ODhEbXQrL3lpT25kMmdRN2ZkdGg0NnhtR3ZZMmNkallselQ5RFIwNDYyeHRnd1FRZzdsVmczd0NvVE1ZR1JGSUVZaFNRaEZERWprZjVUVUFpS0dJMkVrSUFDWXFKQ2tncFRnU1F6UkNNQmtJU0lLY1VCcnVGRmh3ckFBMlV2WVAvT0JidnpobDB5LysxNVZIUFJDbCtnYUxTOG1XQ2Y3WnViOHdmLzhRUGhVNi85Vk9kVHV3REJWdnlpMjR5WitQOEhBd3V4VmVzVzUxOUhuL21MOTdFMWY3aGVKczR4T3N6MTVxclN1aXhPSG5jckgzbVNUcHg5RWxzclY0aVVCcHZyVyt4RkdDbWF5dUhrRmdIQ1lDUUJVb1NSSkFIa0FFbUtaSVBCVEFaSUJnVVFhSFpuQWFCQ1FYNGFRRVFnRkNFSVVrQURLSVFyRkg2b0FXMEllNHM5MmYyTi9ianpxL3RsZmxmZmhBZ2p6YUZpeURYbGpyQm43eDF5K3h0ZTRWNy9kNWpEd2EzWTZpWXh1VlJmL0w5bTRDbE02U3Z3Y2pNUWZ6Wit5UU1mVXExNytXbTY5c21GZWN4MUZ2dHRkRjNydnF2ZHVvdlA1TXJUam9OVHA5V0JIcTFUQ1F4RUJFbUJSUk1OQmdZQVJqQlNFQUd6NU56TTRSZ21KQ2lnQUpiY05yOEJJRGt0QzR5RW1BcEVRSUNtQUpFY1dxQTBwY0FVK1JQSUVVQUl3RGNFdnVVUkxXTC96bm5jY2YxKzdOdTVhRU8rRVFyZmJIcFIzSWxkdDN3NTN2VEhseSsrL2QwcFAwLytXTDM1eDJMZ0t6SGxMOFIwd0lsb3ZldkFzeTg3TTZ4ODZWRnVSWE8ydXhqYmNVNGFwNjdXMVQ5L05sYzk4Q1JvejFEZU5ROVVCb2dLREdRMHNVZ3lrRFJBK2xFc0pvc2hrb3pNbmlxTXpDNXFJSVZBRkdWTXhxYUFsdDFUQURNSVJDZ01LVVJUODAyZ0tYZHJiV0JtNjJwT0F3RGhJS0FnR3FFaTRndEhPc0dCdXhkeHg5Y09jbjV2SDc3aHc2aHZOUmF0amQyMjY5L2ZXMzN3RHovVnUrYU9IMmR1L3BFYW1JQUFVeUtZdGhldnVlaWNpN29uditsc1cvT1FMa3ByZHc5RXJobjJxNTU1RGxZLy9EUzRPVXExYjVIc0JhQWZnWXBnSUdDRUpZOGxLeUNhaWZhSlpGUUFnYkJJVUNTRjB0VG9Fc21qUWFzOVdKamlvNlNFVGRCRWtxdFdTQjZzZ0luUzZoQU5BVVhxbUM2bVlQWmlRSVJNdDRkQUpLWCtDQ3RhS3RvRWR1OVk0STZ2elVvc1kydzJDbzZGb1dKUDJMM3JzL0xsMzM1ejk2M3Z5LzJ6L0toYkt2bFJodVNYWTlvSTRJcVJYMzdSZXE1KzFiRzJvclhZbit0M3VOZ1kvb1V6Y096bWgwdGhCY3FkczBRM0NnTEJia1gwRFN3anJNcHhOUmdza0F5QVJZT1VCS01BdFR0RndraFFsTFJzWU9hTGJzazRUR2tibHV4RnBaSktvU2tacVJBRkpZZm9oSHdSVUlHbUcwVkUwcy9tQ0dBQ2lrZ3lOa1JTRlFBWUFYRkVvK0ZaOWlOdS8rWXM5dTN1c3FFU1I5QnF0S1dMcjhXYi8rclB1bi85UndEQ0pDYmR6STh3WlA5SURGem5tVldyVnEzNDkvTG4zblJPUFBvWjdSaXRLaGRWam5aWS9idVA1c1NEVGtmODFnR0pCOXRnTUtBVFlMMEFsZ1RMUUFTRFZSQldScXNNTklGRmtoR1FRTEZvQ1k1S1laTEppQ2t0SjNjV3M1U0RoU2FFSkNNWUlObURqY2tiQmFIMllJRkJqRTV5NHl5QUF5QnU4R0h5V3JIa3Q3bTNGcEVZa2N0dkNGTk5CM1dLUnN2Ym50MGQzSFhib3FQUm11clpvbmZmaUYrL2FpdmYrOHd2ZEw5eVp3N1o0WCtGZ2EvRVJuOGh0b2NuckQzbmxEL3MzdjgvSDJqcnpwNEx2WjdHWHF0N1J1dm1FMTYrZWFRNVBIcGNlZk11WXFFU2RrdWlYWW4xQ09zSHNEU2lOTEF5c0NLc3JwSk5HWU1KVEloZ2dzaGszd2doVTZWcnlYT0Z1VjNLMEdSZFRjT1FLcVFFZUlnQkFoTUlLZ2dGb0ZOU2xNejJGWXBBQlV3NHR0Q2w3b2xRMXFBSlZOTGZZU0pXdDljQ0dKaER1YUxSZE96MUt0bTVZeEhkVG9tbWQ3MGhEZy9kemgxM1hzWFAvc3JNNHZ1Mi82aU1MRDhLNDc1MTdLS0hQZGlPZTllSk5uRmNMMVJWWCthSzVqTWVkTTNSTDc3NHhQaVZuUlBjdlZpZ1Y0RUhlckIyQlhZRHJhU3dIMkZsSkNxU1ZSU3JDSXNpckF4bUpLTXdHaURCa2pXWkxHVE03Uzl5WG1UcWZPTWdPU29vSW9ZYzFhVXVpQlVFaEhFcDdGb3lQaUdhU2pBRkNSR29rMVJoZzFBbEJRSW9JU0lVRWN1OWwwbjJaQUNpU2dQRUNLZ0tSQ0Y3Nys1eWJyNmtLS29odHByN3NhZDdSZlZmei94QTl5UHYvVkVZV2U1dDQvN0Q2R011ZVhRNDZUOVdjN2hWTXFMaTdOeUtGejN1d3hPLzlNZ0g0Ylo5OTQyMzc0Zk5kb25Gbm5BK3dEcUI3RVZZQ1lsbEpQb0dWSkVNSnF3Z01WWFBzRWhZcEZtNm1zS0l1TlFDQ1EwVW9kSkVFMEpsMmR0RThoUXB0YldwMkJJUldyNFhSSk5WVWplVVdsNGxCQ3FpTkNlT0VBR1NzWmlkbVM3RmRJb1FLa0tybnlaVjgzUklPY0JTVlU0VEZSR29BdzdNbDV5ZExXRzBNTUxDN2JmOStJSmQrL1IvNnI3OTNmZTJrZThWQTErT1M0dm40ODNWWHcwOTl1Skw3S1QzaktGcE1mYUxxdFcrYy9WYmZ1MmEwVE5PT1IvZjJuMTBPREJydUx1dG5PMkM4MzNZUWdTNkVkYUxqSldJbFpFc0kxQVpVbUVGV2xDVENNYUtORVhCRUNFTGxRd1ZMVGlub0JGa0dnS2FTUjRHSnFBamVWSXVmVlBCbmRPelpJeFNRU2NRVTBBY1RCUlJjNzhrQ2tDeDJPdUFvcERDQVUwUE9vVVpvam1ZaUlQQjFGUlY2QkFsUFQ4a0ZYT0c5RHpMSmxoQ0VLNEFGOXVWSEp5dHpHSXZEa2xURitJQ3ZxclhQK1cxQy85MFJYMDlmeW9Ndk54ekx3b25iQjJMVFNVcTM5Y0RYMXY5enQvN3dQQ0pKL3dXdm4zM2lyQm5Mbkt1bzlqWEJ1ZjZ3b1dTN0poWUo4QjZFYkUwc0crMEFFTkppNVZwMFlkckdlQUZpQ1BBZ2JuNWFDc0thNXd3TEwxYmRsMWI3YTNtQ3U4aG84TUlLMXBBeTFHUEhnUEdDOENsZUtxalRialJZYkhTeUNaZ0JWQXRWQ0lCeG02VWN1Y0NaTDRTMjk5SE9SL0pxbzhZQ1pMVXdoZEREem5oNGM3NW9qcllSZStPSGhDVUl4Tmp2Z2hEc0NnSUlMb3d4RUtpcGZ0S3hVbHV3WlFHRWFyQUtCQ21Fb0lLcUFxN3ZZakZoUjY3VllWaGFVbHBzNzFQMnVkKzZTM2RkMzd3M3ZKa3VUZU0rNWZEajNqQzArT3A3MStGbGk3MEYzMmxjemNmL2VFLzN0Rll0Zm9pdStWTzhFRGJlS0Fqbk9zQUIvdGljejJ5SFdBZGl2VUNyUi9FK29qb0MzMHYrb2FWc0NHaVUxVDc1SmptemYxdjd2L2c4SytlVjVhZit0S0hxNU5PNngzemJ5OFFHZmEzb1J1Lzg1WElEL0FLdVF4cU9CTGtvQUFqandQZ0RuN3lKdXo0NVEvSXp0MWY0ZjB1Mi95STlnZDNIOVB1ejYwWjhxc2VWOFY0LzJLMldUQjQ5TVVRbTZoTTZZeWFqQ3dVMGcxNmNkT1VwMFhBTWtaWldLaWtxb0tOd01rY0Q1Yi9GYTY2K0YzOTkzM3MzakR5RDIzZ3VoWDZBMy9lSTU3bnovam9XcllhbmRDVnNLSzNmOTA3WG5SOVk5WEVSZmFOT3lQbjJvTDlIZUhCbm5DdVM4NVZ3bllGVzZoZ1BTS1dadXdhVzUzb1dIU3hPQkZ2MDZOR3JsajF3Rk5uOEZmUHZsbWFiamNxTzZJQmlLME9tQmw4UEhPdmxTYVQrZjgzVXI0YkVLRUFITEQzUDc5ODMvMHozemk3ZCt2Q1UvczdPbzhlS3NmV2RLTWhORnhsS2o0TnIxUU1JcEVnblN5QmJRTHBoNEJPcHdKQ0NDMDAzRzd1V2Z4UDkxK1AvdGpjbGRkdXhFYS9IZHZEajlYQUJGUUJlL1hZZWFkZFVwMTg5WG9iVzdYQUhqblJDNnZmL3NLeU9UdzZFcjkxRjNHZ1F4eFlFTTcyaEFzVmJMRlBMRlN3ZGdBWEErS3N4VVlmdm9FRjlFOFp1YUU4KzVpL1h2RVBMOWdxVHZxdzVXallSb2VwTGNEME5zTVVBR3lCVE11UFphak9xU2tGdGdEWWdpM1Q2WE9iTmtJM1lSTzJiZCtDVGRoZVQ1eUJBcmo3N3o5KzFLNzMzL1U4dTduL3U4V0I1cnEySWtZUG9haFFuRVFTcG9PNVNKcGdnZG9ySTZzeXNyU0tJeGh5ZCtnZHUvNnorLzZObnlxL2NNdWY0ay8xaDBXOGZoZ0RDekdwcDUzNkpmL0JYUS9hZmxwWWNkNitXRlpWdUx1MzZoK2Z2MlA0UHZjNTBiNTkxd2dQdEFYNzJyQzVEckFRWU8wUzFxNkFoU0JzbDhZMmJHUWgrTGE3KzV0NHlGR3ZIdm5RWC95TGlNUTY5RythZkFHeGRkSkVqang5SVNuWXNpWDkvZFBMdjVJKzJJS3B3Nzl3ajVqYlBUMjJETDcwM1c4b1RsRngwNHhnWnNuajkzNzYybU52L3ovWC9nSHYxTityMmc0MjdLTkJuS1hPamdaQlNDTkttaEdCcHAweUlnWWlzQ3BYdXBIR1RmakdGLzlnYnZwUkJFdmtWdjFIYm1CaTBnbG00cWRHTHY3bmM4T2E1OHdqZE1yKy9xR2hQM3JpenBVWG5hKzRaZmV4Y2Yrc1lWOWJlTEFMbSsrRDdRaDBvbGluSWhjcnNBMk10QmVrczJMMm44TmYvOEpMeDUvNjFQMEFoSk5iRlRPSGp0UUlDaVpuRkROdkZHQTc2L0FwUCtiNTZoU2dXekFwTTNVQTMzaVdZTk1XazJsWm5zbEJVTFpoaTdzUTB3RUZjUFhqL3VsSjhrMzdEOTAzTXRvdExCSndVVFZOTkNra2lFaEtCRkZGb2xlWkdFTVV4UDZZRGc5ZlpWZi95MnZtMy9qY0h6WWZ5dytUZDk4Kzl1am5QS1ZjLzgrQkxQdmxQbDg4NjJHeStyY3ZFVnovYllSOWk1UzVqdGorRGpuYkJ4ZExzR093ZGtEc0JCVDd5K0RqQWVrLzh0aS9IYjNpVlM4R0FXNmM4ckw5MEQrZWs1TU9Nek1RSUI2Uzgyemd3V3ZReFVqM3pSOW43MTNiQkxmdVFtLzJWdlI2c3dDQUxsb0FlZ0I2NktFTFlDZy9TUmNBME1zZnR6QUJvQVZnSlZxdENhQzFFbWhOWU9pWWxXajkzRW1ZdU94aEdCcGFDV25LRHBUTHJoZ1Byd1VPdXpGSm1aSE51aGt6OGJvLytlQnBpNisvNWQ5V05kWS9kSTdkR0F0MXBtS1JFQU1ZU0RFbUtMVVhJV1dJQmdubTZWZ2dGbGRWbi82dHYrbSs3VTAvektoUmZwQTcrQlVRKzUyeHMwLzcvZkwwNjQ2MlluaXhtcU5zV0NOci8rclhKWDd6YnVOc1IyUzJMVGJiQlE3MmhiTWwyUzdCdGtsc1I5aThWV090ZmxGZXZQNFZ6Y3YvNkUvNW9Fc0xYSE41V0I2R3MySFRUTUFMRHY3M2RSUCtneDk5cUx6bmhtUDY5enZ4RXZuTTNhUDkyZDFXUFBDMGgzZzJWMWI3MnNSY0VGWUVRd1RTL0VFQWJ5QVF4WlFBUUFkVHBMWUZBb3Fhd0lHQWtnNmdnbDVCYlJEcVlRV0ZvdzV1N1RCa3ZNSHVkWGQ5em5wRmUrVG9GUzZlSnQ4YTV2aG5Wajc3ckJ0SG4vK29MNkVYbDNrNXVkelE5YWowSXl0ZXRHcEZlZXpIaDR0MUQxb2Nab3dRaldrQ3htQW1CcEUwUEJOMFkwUkFvSkFjalo2enRyZjZML25FUTk3Vi90aU5QMmcrbHU4L05FK3BZSnFmSG5yU3RvZjFKeDYxVjN1VnVXNHg4YnBuVzhNM0pPemNENW52UWViN3d0a3VPSnZEOFVJRlc0ekNyb1NSY3RIM0x6N21uMXJ2Kzh2bmNlR2hIdHdXYStPU0ZJaW9BQkVOaDk3di9OWEZldTFkeis3Y3Z1ZkM1cnlzYkMwT0kxUVI2Z3JBUERxeGh3QkM0QUVvcUFtc1FBWXcwbEMvZnB1cjFqeFNoQWhNMDgvWmNxeGFCVlROM1pPQUZjQmdDQUJHbXFOcFBDMEdJK0ZHSExwRkw4aW9mRkhQV3ZmK05TODg5eTByTHJydlBsVEExcTFiM2ViTlM4UjhicnpTeS9ZTHcrZGYrT1lOL24yOHZweHJzV3JDbVFFUkNlWU1KR0llbmdTaDlXQ0lNWXF6S2t4d3VMakp2dkdaRjNkZXVYRXJKckVaTTk4M29lLzdNbkRkNzc1ai9GRXZmbnE1L3E5NnNhcDY1WnhyYkQ0UEkwOTZDT3pHblNLOUNsem9Bd3NsT0YrQ2l4WFFybWdMcFhBQmNiaEh2N2grOFNOanQ3enRpWXhVTEx2VFNZcUlFRjR3dC9tUG51Zy8rWlVYdDdwamo5SEZJZlNpb0lTU29xRm1ZRGd4bUtwTHNCSEF6SWZOV0RTaEtubXdBNlJwQVdLR0xDRkNFMDJJVmg0R3dFUUloWW5BVk1tRVF5ZEFMQ0ZiUW9vdERSblVJaWxpVXJURVF4cUdNTkhmSTJldi9LZm0reTk5NVhxUkRpZTNPcGxaTXZLVkc2ZjhoWitlRHArNTRFM1BIYitsOGRhRmdDcUl1QWhCQkNRYUVFRWtvd05SemJyQkZCYmhJZVV3aXVaVjhUTy8rNWZkdDd4aFFLRDRQaDc2L1lUbVRkZ2VmMjNpcEJQUHJjYTN3Q3FycXJaeko2eVU0UXMzaUgxek45RHR3ZWJhd0VJUG1POERpMzJpVTRMdFV0Q0xWdlFvL1JYekIrT0xmKzUzR1NtWW1zTEF1Rk5US29sbE05cTU2SGZmTVByeEhSOXU3Vjc5bU03Y1VGd2tRcVhSUkVvSVNnY0xLZ2lPWmw1Q0ZLRkJJaUdJQ1pCTUk0RHNyclgxQjZVLzhzZVNabjZEdTFza29kS2lnMXNrVFRaZ2VUQm9FV0pVZ1RtMTZNRGdSZWlna1QwTm9STXMyRTYzcnZIZkMzL2MyL0RYbjdqamZkZWNKek9iNDliSlNWZGZ4d3UzVHdmR1NmZXdUejMvWCtiR0RueDR1SUozUU15VVR5Z29haEROSXhBUGlKY1VqU0xNR3hCUGxaUCs5UHloczlkdndwWTQ5WDNZN3ZzeThCWk1pZ0I4Vm5YcXkwK3Joa2ZiVmxuVVVscFBmU2gwdmlkWTZBcm5lOEJpQ1Z2b0ErMVMyQ21GblFyc0JyQkxLOUIybmZPUCtjZUo1MjMrSmpaT09abE9PWVNrWUhxYXQ1TkQ3VXRlZE1YUTlnTXY2TzFwVmgwZ2lGWktxeFJtS1lqVEFNUkVxb0VsS3pJQ2lHbDhSRXVCbHBZTW4zeFlFaWlkUUVRaWc0bjVjMEtJR2tsWWFrZ05GRnE2T2RKRXFoN3dKbW9JTGMwRFNSRVloRENZQ1drdU5NaEZTdVZ1MXZQTFYzNzZrN3ZmdS8xaG05ODlFem5Gd1RXZW1aeUVpSEQ4bVNkYzFoL3ZpSlNWYWhwWlFFRFdxTG1RY0FBYUFqZ0JWVlE2Nk5weE9HcnQ0M0hlbEVDNEFWdmxmMnpnclpoMGlwbjQycFViSG40bW1zL3FhamVHYXNFVkR6NGV4ZkdyRWUvY1N5eTBpWFlKYWZlQlRnL1c3Wk9kbm1DeEJMdG11dGp6M2ZWeGNlWGxML2hiQW9wTldETHU1czBLVXRkTi90LzNqZnpYM1J2YjdVWnBVbmxoNWNSTUZDWU9CaEhMdzlsSVljenVhSWxRSXhFSjFvK2lDQ0lNRkFSSktIR0VpRUhFb05ud2FmeGdvb2dRQmhKQkhLTW9Jc0NnWWxFVUFjb0lJRUROS0RRS0xQL3VTbWt4U0duYUV1ZGFkRTRDSk4xSlZkRnR4UkMvdURnODk4clBmM2pQamRjY00zUFRaa2xnQ2JBNWUvWDlYdlgwNjNtaWZXREU2RnkwNEtMUldicHhGWkVLQTJsMEpEd29wSWtaWE1teTJ1QlArYlduVFR6K2tVL0gwK01rbGlMRUQyWGdTWnhGQW5KT1hQMW5SMWtoWlF3V1BhWDVxUHNLZHM4QzdTNDQyeEVzOW1BTFBXQ3hMMWpzQTkxQWxpWFFEOWJTbnBUci9UL0oyaE4yWW5KU2F1L0Y1czBxTXpOeDhSRlAvOTNtaDI2OXFGMDFLMGhaSk41amhDQlNhcTlqcENZUFhmSmNScVNMVGhJUjlVZy9lemRCUWttSUdSTDB6MXgycFZJTG9LVE1tOXRZV2dZVE03d0VrM1J4bUdaRW1kVW5wY1VWWWtVMVB0ZnR5TDdyMnNYKzY3aTZGMGFxNERSSWtCQzhOWDNWdXJZYXYvUHg3LzI3elRNejhWQzhaUktvaU5ZcFkzOWVOanBCelZ5ZFlod0JOWXFtYkM5S29vQkFVKzBncFpSWUUxZTRoNVpudnB5Z2JNVlcvdEFHbnNKR0w1aTJ2MS81d0NlY0ZWZHM2aUJVSVhSYzhZQmo0VWVIYUh2bWdZVmtVQzcyZ0hZSnRrdWlXd242UVZoRk1BUS9QOUlKT0dYVkcwZ0l6anByS2UvT3pNVHVhOTUrY3VOcm5WZUViaXRBZ2s4WE1YSnBDbUNadEp5TVhyOWZoN01Vb2sxMEVIb1RBeURmSEZ3SzV3WW1CbDhtOVJpRWxtaDM5Y2NwV1NmaUR3Y1V2a3lRelRkUG9BMnZqSzcvcU9iYnh2LzYzQWVlMmZuemN6WjBYbm5PaWt0UDNkQzViKyt0RXhQcUpUSUN3ZmNMclNabWg1LzI5Vi82bDgzQUZxWmVPWGt4UVQzN2JjKzl1bE1zWEQwRTV4UXdZWVRDNklSUWdKcllERkFZR29sM0FJSHFJbnZWZmJsKzA3UEduL1FZZ2RqMzhtSzk1OXk3eVFESXViM3h5OVpVUkFoOVVhMTArUDdIRWJ0bkJZczljS0ZyN0hTQlRsK2syNGYwS2tGVmtXVkZDV1l0SzhYV042OGZ2L3cxdHdOWTh0N3Btd1ROQnFxUGZPNE5qZG1oMFZJaUhHSnRHTTBHRXdFMHZiQjZpaHNvRWlHSUFnU2syamltWmtkU2FJVVlCVkVWSlJRVlJBTUZNYm8wenpkbFFncVYwWlRSeEFKVThzMWhKckFvRG5IQWRwY2NPZ21OelVhbDRSR3IvdUtrYmYvM09jYzkrNm5mSUtlVWNVcVBtMzdHTjg2NDdkVy92bkNPdkwyd1VwMW9OSW1xUFlmcUs3ZFBhNnNnTUdsMVhiY05XeFFBR3NlUHZsK2xnb1pvQWtJWm9CYW9DT0xNNEFrNmkvQVdwV0RheFlnRVZtSU01OFJUL3lDbDBlL3V4WHBQdVZjd2JYODlzbUhUeVdnK3ZNdktyT283UFg0Vi9WQkxlS0FONmZTQnhaNmczUWU2SmExZmdXVUF5Z0NFQ0lrMDUwdkl5dFo3UmFRQ051cFNQejBUNTEvd3FqV05hMjUvUkRRMVJYQ3BxYW05MWlBd1FpS0pTQ0RtcFlNQlR5ZUZjU0ZyVm56eS9BaE5ZLzBnMUFEU0dwRTZndWlHck9lR3JPZGExbmREck55d09LL1JuSmdTaGlDMG1ISThjeXBJTkhrRm9VWWJqdVlXMTh4OTdvUVB2UEFQV1U0NlRrMnBZTm9FMDhiSnJRNExKUTZlUC9aYllXMDE2d0lkRUNVUXNmR3Q4dVNySC94L0g1SzZoaVF0c1drcTFTRUxhM3NmNk5oc3p4bTlzNGk2aW5hWml5U01rZ1pXQmwvekFFSGZaczlPdEtNZSsvTXJOejdpZTNteDN2T3dERGhmeDM5blZYU29FQzJnUXVNK2E0bjVIckRRb3kzMGdHNEpka3V3bnd6TEVNZ3FDbUlBUXlrMkVpci95UHRkazU3MEJmbE8yNllBb0YrKytiZWJiYjhpNU5JMGRZR1pDd3ZtM0p2ZUJ3aExuYXpVWVZZQk1GZkhLYTlHQUJLRW9pTXMvVmlyM1JoYTNYZmRvK2Z1bnBzNGNGMzdFU1BYTFQ1aDdMcjVoemV2bXp1MWY5MmMvL2ExMVVsbDFSaVpjeXRicFI4eE9FU2Fpa1FpSmk0dUNLWmlMalk4VWU0NStBbStiRXFCczVhaUVRQ1oyUnl2eEpRL1o4dWwvYkpjMkQ0a0ltcUlsQkRIdUtMWjJGdjlKa25aVmwvdjZXa1NrTWYrL1ROdWp5UDl1M3cwVlpwcERza0tneU5UbUdhRUV2Ukd1c3pwSzFuWlNodkd3K01abHlaN1RkNmpCL3NqOWIyQ21makdWUnZPUEsxcy9Gd2ZYV01WVmNjSytsV2p3bjN6a0Y0UTlBTFlxNGd5UUNvUnhraUpKb2lKMDFnQXZxUGR4YkJtN1JkeWoyRExjRnEzZU96anptVVlneUFBTldJQlFlSTdtRUhxQ0Ixenp5d0FCaHVtVW5PVmdRalNSVkRjS0RwK1liaFR6YTNWcXh2M08vNHpvMmVjK2FIbVh6LzNhK0xHOStPTFJiMnNoSVFtRUhQditNSVo4bGZ2WHp0ZlZUOXZYOTN6OExIZTZDTnNUM0FMTEl6ZVJVVjB0SVNJZURvMFhRc3lQVTNld3dSS1JPS043cmNQRmg3UUtEQTExdzBsL0dKODdMWnQyNXFiTU4wbklBTHdTa3o1VFNlZDFMZHEvck1qc3VvK2xSbFY4N1lOOGg1VVhvQTBHQnhOQ2hBaFVYSmQxQ3FlYkt1ZmN0NzRlU2R0bnR0ODJ4U2cwd09rL3J0NDhLWWNTaytQUTg5ZXk2SVZ4SUtoRW4vY2hFZzBjTEVIdG50RXR5VDZGYlFNWkJuSUVJQVlLUlpCR01VcTZMckdqb2tYUHFka1hnWktMMjU3dVB0akgydXBOczhuREpTb3VaaUNwV21wc1o2TTBaaER0b2prWFJVaFVoNU9ZUnJVT0l5dWs5VUgyVCszK1k3bUUrL3pvTlc3L3Z0Ull4KzYvSS9rdFMvOHRNajRmaHFFL1VyWUQ4SXlDaFBNSytPUE9QZm1GZTk3NWFmWGZPZ3YvM0Rkdm5jK2tzL1pjR0gzUEgySFA2WXI0NmdLbUZoT0ExcUZDczFUeGgrTjRRWVB4d0RyTm9qazhORHhxeDdWS3lzSVRCR2pBckNpN1k4Ni9vb2JUaFdBbUpxU1pUY0VWejMxakVqcG96Q3BzVElSNXFpVWF3RE5GOUNsSGxtRVJJVlFyYmVKc1UxMmVuYmZLZjIrUXZRbWJJOEFpbVBSdUlTb1lBeEtFRzcxS0d5K0MvVDdZTDhFeW9vSUFReFJFQUlrUnBoRkVCUTFNMjBJNHRkdi9hS0lkSUNOVGtRR0wyN2lzOWNlb3dkN3JRUW5WSktZU2htSXJiMXNBRXd3RWNtWjJoclcwemt4S29IaDVxSXJUeTAvV1YxeTNMbXRMODA4cS9tZmYzOGp5MHE0Y2FQbjVLUkxtNlc1MXpua241QlQxSzJZZE53NDVkSHV5OWhmL05hMm82NTcvYk9hbHo3ZzNPcjA5cnVHeDlyZVYxRWhocTdFdUdLM1BPejI1NzN1S1lMcGNDWGhpWTMrU216ME05UFRjaUdtdzYzUGZmM3pKbWI5S1VHdEFrMFZKR2xoaFk2MitwODhzQUVBdG0xTDEzd1RiaUlBN0h2M0Y5NGJRNy91QmtSaHFiMWpxdUk5U1dXRW84RXhza0NLUUlHRW11Qyt0bm9TZ0c3QmxuZ2s2Rm0vczdnQ0wyK2Q4ZEQxSnFlYmxDWm1UbHNlemdOWWFBTlZnRlJCRUNwaENHQU1FSXVBR1Z6dGFRSkFTK2haWnhWSCtuMGNQK2FDMXFyeGtSVDdLWkJBRWF0dlQ4a2dSbHJvQkNtYTJwVWFpU0xFaEJRL3VyY1RINzdpc3EvZThwRW5UTHoxcmRld0NpNFZjYUJzM3g1a1ppWWVNcWxLMldQSmc2YkZObU1tNWxFbE9UbnB0cFowRTlPL2VjMnFtOS8wUy8yZkczKzJyTm8vTjJiUnFURDI5NXI1Lzd6bG4zYzkrOVZQdW5Da0dVUzJod3ZkOWpCSjJyZWY4V2UvNmo5MjUydmFzeUdLTUtIaG10YmM0bndGZjFUcndSQmc3L1prV0V3bXh6djJra2V3V04wQXlnb0tVbWxVR0x3WlBRMkNLSTRHaHdUVzVJSkxhQ3o2aU9FNEczM1Fid3h2ZklCQU9JV3A3MjdneVN3YWRycHJQSDVjdkFaWXRHaVE4UmJVQ1BRclFiK0NsQUZTR1ZCRmFFeDlKTkppYmw3NnlndTZEejZwZWNodjI3WU5BRkQreDhjRFpydFk3cTFMa0dMZEI5dXlpcnFldVJnQVdJTjlkV3NXRmp2cmVaRy84bDJ2T2tja3BLb1djVG1IS3ZuN3BDTTIraFFSaGRuZ1VudGZIVjVUc1RRVE53T1JVMVBLT09XUGV1ZWZ2YjMxVDV1ZkdEZkVPNGFzYkpoWHVqdjlCRDZ3KzRyYjcvZVNMMzFqMWJQLzhGc24vTmJMN3p6blQ3ODYvT0hadCtsZHpZSU9Rak1sSTlUSVZBazdGRDMvb09VRkxNNjZrWUNnK2JUMXQzYXhzT2dsMGVRVDRKR3g2UXpXNUdrSTY0OWQyb0FTUTR3ck1leFA1N3FucGFqd25SSDVzRTlzaXdEY2F1Y2ZEMGJFdEpwSE4rckJiaCtvS2lKVXNKZzlseEZrQkMyQVNkZ21qYkNGQWxad3RuRHpJVSsvUGIvOStvMkNzZ0xVQUFrWlRMQlVtWWlKaWhGcWdFWkNEUlFERVlWQ2VLc0V4NWE5eFdjKzR1ZkhiLzdVWjhsekNnRzR2S3JsMUpRU2NJbW9PaE1GMndOR2gwaHlndVFFUmxzVWJBOFhZbnVRNldrajRMZ3NoOG4wdEFtbUF6ZE8rZkduUHZGemM0OGVmMENuMlBuWm9WQzU2TFNLQjcwTmY3Ny93R01XanZ2ejFYZXNmRm5yT2pzcnpqY2oxU2cwbFJSbVU4VUVneXJSKzhKdHZVTUdmTlBUQklnVG52UzRHenU3OXg4c2lxUTNrQkVzS295NWRhUUQ2V0JRa0I2a1N4V0xFYVl1S282MU5ZL0ZGSFJUQ3ROSHJxSlQ5U3cydGZJKzYxZFgvb0d3Q2pCVE9FalJBTkR0a0VFMHJmSGx2elF0WmtCRjh6MEZTU05ZQldJUC9iZS9LWnQwTzVkYnVERi9GK0hIQU5XOHFwOG1oNUs2WENHWUttU0tpa1JTRkNxa1VFMkc5MGw3M2NURnExNC9mUld4MFF1MkgwSVE1OGFOWHFhbkE3d0Qzekt6dW56bnZ6OHUzTW1mcThyT1NmTlArZjM3SVFMN2ozbk1sMXVoMk9IT3Y4L1g1SlRqUGlpditqODN3cWF4RlhDYmx6RklaUHQwSUNhZHZINTY5dW9uUE9HSnA5elF2cm0xeHgvVmN5R1VjQ3hqWlJDaGVmcUVnaTVmTzgwcjVTQUJqN0k3ZjhRaTZOYXBmMjRjZlAxWEZPMEFjVkxyQ2RRNkU4aXpCektYOHdUZ1JPQkFNVEVFVkxhYVF3KzQ4TzgzbkN5UWIwMWg2aEJDZ0I1ZVBkKzM5QmVzbzI5V0VpUE5CRTFKMzFXYVNJeVFFQVV4QWpGbXVDK1ZRSG1hbWFwY2pZQUR2R3Mwai9TaUhLcTBxaWVXWG9vWUlRWVRRd2FQQVltQUdxa0VKUUNpc2VrTyt1cXhwLzdOeEpjLzluR2VjMmtoaDlGSkNhaHMzeDU2LzMzVjZaMUhQT1d0dlplKzdtdU5iZlB2SEw2cGV0YjROeHFQTE43L2paWCtpbHRXanQvUzNEaDB1L3ZWeG45KzY4L0xmOWgyM2NLcXg3MjFkL0d2bmJGWkpSS3BNRnNhbU05RVRtNTE1My8wby9QNitBMi96cUhaNkUxRUpJZ3dlRmdveE5Lb0wrMG1ta2xDeFpoSGo2SUVIQzBlaWpLa2UvNmtMYytoWDllRXhwQjRKZ2sraFNORk1jQUJ4R1doQ2NsQWo0TklOTk5LWTdVR284UG45MDgrYnpuT2NJOVY5RW5PbmUvRllMUjAxL2pzWlJiQkVOTktGNk13dFVOQ21oQlI2dUlJZWJvRHBMYnBTQVl1VVJKS1FOT2tweTZnUkV3b1VVUWlxQVpLSkNTS2lNUW0ya1h2T1B2YzZCWHZmQW1OSHRlK09Yd0hHYkRSc1BsZnZ2UTMrZHhYWER2MDZibm42dDZKdFoxK0szU01vYTJNNWxvMDE3Q3VXRnlNckJiS1JoWDN0NHJSQXhQUHhmYTd2clQzdk1rL0U2VEM3QkFqejJ5TzVLUDg2bjk1NFllN2p6MzZEVU90MHRPWTlsZHpqUUJFcGpiV1JOSWdSSVNSVUdyWDJoaCs2UEVYa0Z3RGJMYmx6dzBBbUdpQWpIQ3B5SUlZcUFrM2xZUlJKOFF1aFc1Q0dlRVpxUVRKaUdFNEhHZmpGeVNJK1FVOG9vRTNwZnlMVWRGejRBSUFFd09wRFlGRUF5M20zYmxJTUkyMFVPc29wRHhKY1NiUUtIUVJRTVE5czhOS0FHV2E1Q1N2RlUzR3BxcEJOVkxGSUdwWk5BRWFHd2ZMMW1scm5nY2pnQ21UUTVpTThJS1pPTGYrM0plUGZlUzJmOENkSE8xS1VVWFhOOVhLcTBTbmpFN1M0RjRBT3BIb1ZDb25VbUxPR01xNWlhRVZuNXU3Yk83aHozamZ0OGp4dzQwTWJvdFhZcU5mLzk1WFhEWjdmUFhOcGtXZmtMWjZWR2t5WUJNZzVsNmRhWCtSSmREVWlUemk1ZUdrUFQ4Q0NHc2hFa3NMTGt4ekR5VkV6ZEwyV2g1ek9sSUsxbE8xSUVKaW9odytNMGNJKzg2MkpXL0RYakkrUHJGUzNYMk1oS1VOUzRGWE1FWklaTjdBVGxVZDg3NmVwaEFMYUFURjBuUmNESEJNdFA4alBCcTVYek9OZ0ZKU1ZVRlFLZlZiVWN2R2w5Q1F0b2FWZnF0ODlJTTNFdlNIVk1xVGswNUV3dUthcy85a3hSMzJzdXFBbFBSaWdzcW5pR1pKVndtRE1XQWFNNmF4WWw0eGk5Nmt6MUpXbFN1dUxTOVovY1JMUDBTeUFVbnJoelVvc1FucktDSWRXOU43Z1E3MW9CQUNvZGJtcWFjLzJZQlpYb0FtQWdWN1ZiZ25IcFcyVkJScHJPbVFnUTRtbytlcG1DaVdUNW5xRFZoQkJhcFpoVkg2czdBUzQ1a3BJNGQ3c0FEQWszWHNPQVdQcFFValRDaUFObEpqelJSMjhpVEhVRmU2MUpnTndZd3lXVmJKQ0hEMzRNTVJqbkNKQmc1bmdJdUpuZVJpeXQ5cW9CcmdqQ3JSaFpGZXBXZWYrQmY1RGoyRWdTa3pNM0h4VjM3cjEwYTQ2aFVWUnNwWWxJVktsQUVYUjFtem5rUWxRbkphRUJsRWoxU3BFa0wwR25NOTdZOS9jdmZEZDUyNDhZOVNXSm5VUS9JeEp0M25QL08yVHl4TXpGMHp4T2lFRXRKclRtMGNjdzdOeG9FaVg3ZXlMNTFPNThnc0RLY1pxUkxtbWZWZzhLL0c5SllHWlpYYktJT2ptV09hWTFOQ1dFbGQ5Nno0c05NeU5xMUhOUEJSamNiOVZzTUpFUk5SMXhGYUlCVlhqS0NrOFZ5R0NUT29ZYUxwWDJaZVpLTkpoaEx2d2NSd29UWW9hOE9tOXczaUlxRVI2bWtlSFEybnJMeXBjY1Y3dnA1RU05NGRCL0RnekF3Nzc3N2lSUCtKNjE0ZEQ0Wm92dTlGRFZBVGxRaEloS3BKbnFJRGtvWVNNc2lkQ1ZUSW8wb0lEYXBWTVYrMnlqWDdScmJjL2RqTmoxUE1SRTR1bjlic2tjMGlzWEhCU2U5ck5NdU1PQ1dqUWhJNEl6VTVWemd3dEE4QncyM3l5UFpsNGxlaS9uNUNHRVVUQ1VHVnl5S081ZndPcHVWVlJJVWdycE1odHlZTTN4OEF6aHFJb0djRGI4TkdBWURLN1BoRzJtZU5FYUQ0aFA4enhzeFpUSjVNcENJSStlSlJ5Znl4aWN2aFZmbWRrNHpsSWRvSUtnR2YzdElSZEpaNlkwZVJkSk5FR2FxZ3ZmMS9LeUloNy92bFBuS2JDbUQyaGpmOVFmT2dySTJGVU1VVXlVdEpsMnNER0VVdEJWczFwUGZUN3hHdGlaMXhBS2FRRUdoUTZ6bzB2ajM3OTE4aEc1aVpHY3h5TWJYTkFLQng0UU0vMEIwTGxVaHdvREVGQzZ1RkVqUGJKSVZyelMzbHZqdnVrRU1MUXdvQXM0T0x1eHJxSUphbVpKcnlZRmJsc3FWTjV5UUZCb0ZaNHA1QkxHbVJvRW1INHpDNjVyc0NIZmRsc1FZU1FVMUZGZ293aGN3SVNBQnI3MVFqTkFDYXdpcGNFUEZSeEVXRkN4U1h2b1o3OWwvQzUzWktnc0JGd0NWdlkzbyswQnNFcFZwaFhYbmF4VjlPaWt4VE5tQ0VZSHZvZnVUS1UvMXQrNThWbzBYeHdjRlppZ0JxVUUwcFJIeElrVUVEeEFWQ0FxRWhwNElBMVVCUlk5NzlFOVVBb0hTbE5NTFF6dkkrcXgvK3RJZW53aWlIdmVsRXJGLzVPOCs4c2QyWS9YSXJxWFNZU0pYVFZ3VkJTRFFoV0hvZkFhbmFQWnl6blBLbGpnK3ZNU3NoRXRJaVI0SWtUWE9GbnNLelFSZ1R5a1dLSXJLQVVjekVXRWtUQWNmSDR2NnBrbDdIUXd5OEtiRTMwQUJPaDBRRWhScklKRjZRd2xjS2N3R2lkZDRrNENLb0p2Q1JnNHZyQXVBaTRCTmgvSjZwQnVuR0VUWFUrUmMrcHJ6c0k4UkZPaGQ5R1BNTHhaWXROd0RNNkErQWJkc1VJdUQwS3k5b0xzUlJ0aVNLQzJsWHdGbTZZU1NLYUtTSWlXZ1VhQlNSS09xaTFEZXNhQ1EwaXFnSnRXNW1ZK2F2Vm15Vnc5cm85bjQ3K2U1TW5jc0liSFFBeUZCOWZzZ25XQ2t6UEtsMU5FQ0Fpa2xhQlk0Z1RmTFd6T0VlVEYxY09PaVQyQ2Jya0U3R2ZJTlFKUThoWkZDQUpWeGFMYzNRaFVFZEk1clJqc3M0S0EvejRDMEVnSkpjZzFxZ0JJU29wRWxPS2tva2QrNHBseVZtR01SWldpQlF3bHpNcWdWcE9PQy9GK0hlV2ZKVW4wSThsWU9iUnp3TkNNREV5TTAxaURFSXo5dTNFeVQ4N3IwYllaSHdKb214bHFPTnB2NENRa2tBcmlHQnVGWmpnWkwwVXlqUW1oMFNrNXlwcEFKU05HalhJdEIxbTJoY0MyQ3BmOTJZcXVybUpRL3VSdGRESHU4TjZMeEwyNytCU2N3aENhK2xmZEdsVVNFd293QkM5YlhidnRaMEJZUkx6SlZFM1Rjb3piS01FSm1mUDRzMkR0UVZhNW56bFd3MWw5dHptWUh6dU4xakNCb0FpUXJObnBSREtEV2tuT3NDeFJ2Z0xlVkpieUtwQ2hieEZCUUJiS1F3ZTQ4aFdnRTBEZkNXSytZbzRqaUlEdUlpNkFJeEF1aFhicmd1VVg2Z3kzWitTSElvaW5zb0pBaUtvT0ppTm5MQ3RNVUZpRThGWFAyNTlOYnFnaTV0QTBuNmZhb0pwTW1HVGp4b3I0aTM3MW54SlJsRExhbVZickFVQWhmLy9RTWY2RlY5UUttWkk1YmFNSW1zR1p1Sk14WWhFdEIxSW9lc3Y2YWUxYnZUMXA5UnhtNFM5b0hCcGRDYmdCTlFrSXBBY1JsLzBKclZJcWtZTXhxRUFTdmdSN0VWVHBlMVNvZms0SWFxVDE1Wlg2Z2E5TS9jWWhjSEJaQXNMNHA4Skx4UlhCUVVCaFFSOElTN3h5SXJFaTdrbjAxUXZ6Z0RDMHRoM3lWeUVyeEJUN2xQdmlzM0RpNk1BTllCVnVsUnF6ZUFKZUJNNFZQdUZUV0tTOVY0Q3I5UmtOdXh6SkVXNW1JTG1xaXl5ZU1qMUZudHhZUkdJYXU0b2ptc0o3M201ZWVtRG0zcklkZXIyVHFCVWdEQ2V0bzFBRGlRR1ptNS80NVFFc09kSStaZ2JSNDFjV0prbWZCcld0cVZHWVJsUTYyUm0vWGJJR0lpUWlHalpMNitXdW9heHZHeTFjTTh2TWlxWTkrUVVCSnRBRWs2VGltRUVXSWlMcWtEaWhyb0RMYkVFTXY1Mk1DQ2hLYjJDdCtOek9sYytqMERyNjF2bHRyWXpONE5oR2FMeSt3N2VBemZkbHVVN21JSm56WE5CamRrQ3MrbTllZVlhd05qL2pzejhkZ29qZ0puT1JkblVyS3paQ1ExQ0VOc2pnMTVHUnMrTFkvNERxbUNoMWFPd21zOTVrejlheUxYbTJCQUJLejUxVUFIblNOZURvdFdwamxORHMxNVF5Tnh1eTMzeHF5blMxbjV6UkxRSXN0RVJzU2F4eTQwRzh1Qk1wOWRtUUFRWFVoeTUyb2lEa21vSVJ1U0FxakxFNUk4TWFtanU0Q0NyS21Od2dBSFFmSGRxbWdIMXdUZ1E1NUlKZDBoUmRhREZJT29BRVpvZTBFT0dUVU9RUE9UVkZhTkY5eTVHL0FPWWdaVEZVVEorUzN6TmhMeWw3UUs4enBVNXJFRHRDUURiNUluUDVwZWtTb2szYzBTK3owV0Z2WWY2WFgwOW41Ym1tRTF4QlZJQlkrbUdRcXp2bmorcFltb1VNbDNsTkcxYVN6ajl4WkIxZ3g3bUdVaExvZUl5SHBkam5uT25iVFhITkkxZEFCS1ZLM1NsNDNEMjZTQlIvZGhCcGYzOURTdmgyaStTamsvTW9HalMxV3ZEeFJ2aERQbTZoZmlqU24wM3BNRFI2Q0lFSi82WVBFR0tWSk9WbThpM2dDbG9HSEFuZC9zTHg4MXBvWVdDbUEvYnI3NUdta1ZnRWJMb0czeWZML1U2eUtIZTJvV1VKRHMxUmtTelpoM25ac0ZkZHJSS0s2aFJYZi9YZjBEdi8wN253UUVtSjZPeStWZWVyTmY3Q1BHM0xha2ZscFR1RWJLNHpWaDM2QVVyb2xIQmpyU1FDRm1ZR1F3NU05N2pwYmgxVHFuMTFCbDJvOUt5c2FFMGhBUXRXM3Q3NXdtR1FhNllGV3FpRE53bTZZakdjVElGYWt6c0E1MXpzUWNoSFdJOWlSVUJtSGJmNi9GVlc5QVFjQXpkZkUrUDY4SFVGQmdGWERlUXk5SUpFMUVMaE5MRXBFK2oxNjdDd1VBRjBtZlFuQUdTTkw3eWJDRVMrQ0hxWkdlZFY1bXFpTllnelpMbElrRWx4S3hoM2o4S3JZdWY2MHRzVThBYkV6QTBQZ3ZQVzlqcTJoQUdhMDJRazFlU0hoM0pPcUJneEQ3K25Ka3FOSnNzS1NjYUVxV3dRNWJ4Z1d2RFR2Z3FwRnBPU0I5anhnOEdhUXY4UjZCamdPYUVhdmtoVUl4aURPUmhnbUtTUFZSNENPa0NJQTNvVS81RGQ0RWhZbDRFellqMlRTZ1FZVG1QY1pvd2dlZ1lSQWZBWitLTXlraVVKaXdpR0FSQkEyQ3MzdFBUaFgwSVVDOVFBUVlIZjRvcEV4L2c2c0FGek9uSmVWdytnanhHZGYyVVZSTlJHTnF6VExRSVJvSG1MaElGSEZCMUNYQlJDOUc5T1krdC9iU0YrMGg0QWI4cnUycDJBdlgzWHFxbW9LYVlkd0VoWW9pcE9Jb0RXTk1FTkx2V0QxOFQrejBMSjBhTStjcGowOEg3VkRHMHdkSGZBM29QQVNEZUttb0NQQzBibWU0MDF0TytqekV3SDBnb0VEeUpNMm9tK1p3blh0VThjbUR4VnQ5STBDOFVSeVhQTWlsQVo2L3gwSXJDbHlvaXlzT1BEbFh6dkFHZFJEekZwMlc0M2p0YXgrVVlNSk1LcHVhTXBCWWVNeGpQaHdhUmQraFVuRkk0VDBaS3hWUGpsZ3ErbklCbDE0UFV6ZVFYMU5kWWRlRlhwNWthVkVKVm8rOUw2MHdicFFsV2FmdEVZQzRFTTR2UXdWSTBMUStrN2NRYTJxdjFFVlhCSnhnYUdqb3lDRTZ0ejZTSmtoNTh6R0JHa2lyNmJrQk42UXoyMGltTFp3VW9uT0tNRWdIeDZBdlIvRGdSR2QxMkprTm12QmxKc21MakJtTHVSU3VjK1ZMOFlPd1dvZGJpb05JYm4zdU1VWTdESXlwUlM3MTZ1ZndwSGlBenNDbW1tcS9VYjMzblE5T0NOYTAxcHdwQW03Vm4vekpUcTQvWmp0UUtRdFlBa2hNNEcxUWpVdStnVVJUcFo1dVhwTmt5RlRKWTZrYklCeGh6dURZbC81S3Rublo3Mzh3M1ZTYmJKQWVBTzdmL094aldydmJKMFVSQ3F1a044dDZFSlBIaDR3cEYrZmNPVEk4ZkUrNlkxbC9vRmI1WjMyakpFSmpOcTdtaGJqY09sbHFuUXdPTkVjaTBHYnhPZlFFUzZ2dmFkaXdNUnU2aGEvRFJUaG4wTUlTVmNaSFNKR01xTXBVWFR1azNqUERrNFBldDRpQ1JnWHpsYUFaVWp0MHBNZVFFY01HTkEwc0xJWG5Sb1EwRFd6azBGcVl3RlBRQUxCNDhHbFFBZFlkRXFZcEloWlBQUDYzTExoRkNaSDBOQTRpU2dCOXpOaDVIb1lzdFV1RVJyTEdwRE1XRHMxWXNpQjQxM1B1QVNmLzQ4cG5QT00yWXFOZkl2VWxhcFBybEw4MXd1R3hLS0UrOFNHL1ZoTXdVQWJiam1sblNrSUFPa2R1a3lSWVVpTk9MUllUOGIvdUwrcU55WG9oei9MSElSZDFBUzFVNXNVd3g4NGlCSXo0VXoxaURyNmpYODFDTlFtY2V5N055Qk9BVHl6cmVkUHFHMVBGdTFRTkU1N1FBa1FCb0l4SGhMTmNBVUZCcUdkZE9STWVZRkdETE5uN2xNNGFadHFvSHRtKzdFOGVLak1Zak80RU1FNU91cUYvZXNldFhMUHVNbzNtSWFHQ3crRHZGSmNxNWRUMkVWb0RPQzYxY3VxWVhxdld2OU1BcjFYVGVvM2VCRC9qbi93SC96ZjFBdHVYRlM2YmpHUWgxM3o5NGxnQm9wVm1Oa1FOY2pEMVNSR3liSDBWQXJUdndZTlJsYUlEeFpCY09Rc2w5Y0NvcGNYem1WNzFRVDhtQm9NRDJVaHNEeXhJTlorZThDWTVvb0Y3REcyb0llV3pYRWhiQkFxU0JYUEZtNnBsSnQ1SWJWeEJrY0sxT0VEVEJRUWVkdTZ4aDd5UWpVdnpRampDQ2dKRmtnQmtrY09sSjZTQlZKRjdBeHBxYnJoWE5LKy82by9nUExCblppa3F6TXdZSnllZHUvcWFOK0toRjF5aDBDWWNJaHpTMytvTVVwRHdHWWYyUnVhYk1kOEVBbzJwRG5BR2N3aStQZDhJUjQvc0QyLzlpOStVNXora3d0VFVRR0dPRzlQTzlPeWpIbi94V0xkNS83NGlpRVlWeWVzNlNIQ241cnlaQkVRb2dnalg4c1NKYTQ4Y29rdXJjekZ6R09aZ0xwdy9OOWk0Uk41cHpnWjNJRHlFQVVSMGVuMGEveDQyRDk2N1BiMkFWaEcvc3FBbHRCbWNHNHBDSDJDTUlvM1UyMG9SZ1FZaGhZblducHNMTDNoQ1BTUmRLQkl0QWljY3MvSWVzTW9FVmJZQXRBeHNta2d6UUFwRHF0anI5c2tFUW1kRFlwajcxc1Y4L25NMnlIYUVtcXd1QUxGMXE0bEl4TCsrNjJtMjdzUzM2TzVacjFaUm13aXFKRndVYVFTUlJwbzBpV2E5c3FJQ2ZHWDBBUlNqd1VLeE9PZDUybEh6NWFYUGU4TFloVS8rS3FlbTlCQTlrZTNiYlpaYzVYYTMvNjdxS2VGanhuMG9LcGFxYURGSms2clU4aVNKaVFBM05sU01MSGVvNWJ0TlZabTNLWm40NWF3aHpreG96QlUwTTNXblBnd3FndWxTQ3RsVDR0WmlibWN5OEdGRjFvMzVEdjNRZ2U2ZHMyWVZuS280VWh3WXlnei9PVXJLWGFuQVdpcXVRUHBVbk5DUlREbGEwSFN3VTA4NU4zdnVvYmw0UkFFRjFCRkxVU0I1SGQxU215TWV5Y2dxZEtNOWh5OS80alVvbXNBVlZ6aGlpU3VWTmJZcTk3SHR2eEhQZk1BZm9EbXVxSHBGTXFJRkZvaDBsb1F3SEEzT3FKNG1EWWxhSUhpTFdxRDBkcCtqcjR4Ylh2cUlrZC85aldzNE9lbVdrK254a0lkNEFRd2JIdm83WTNmM2p3bU5JZ3FpUzNKbWFRcTFyRGpLVSs4SXdGaUlBb3ZkM1FDNnk4UjlsbExXWWlsdW1ieEVRdFh5ODRJRDc4NlZkRDJmbElpSWhrUXF6Yyt6WWxYSUxRQ3c0ZkI1OEhScTRXV2gxZDhsWXQrRUI2UWcxVk5pRldFd1NZVVdJYjdPbmN4SWxDWFA4SFdMUThESFpMQzVQZVVSUFhqRmFPSmI1NkVFYWlNWHkvNzVGS1lsL1MzT1dqNWdwUDNFL3ZuSC9ycGNlMjJGYzVacTlMU1ZSbkRqUnUrdit0eGZWcHVmOFFoYmZaLzNHWVpGQ2ErSVRqM1V0VVNkQytxYlVHWGxGS0ZRUVJGOHZMbTg3L0cvcGwrOCtxTG1rMzdoS3pYWDZ4QXkvYlhYVnJNdmVkRVRoZy9FTGIxWUJISFJwZjZFZy9Hb1NNYm5jOHNFTVNpakRic203THJydmlnaUI0QkpQVnhnTlM3MDgrZ2hNVmRGMGlRcGd5UWNVS1N5YmtsU0N6TXFJbG8wZUlqT1NqbjN0cmp6UmdDMVVOb2hPWmlZZ3J4dEIzcHpVdDZNVm9RMkl2MVFvbjVhTktLUlFBZ1VxZklWbjJhNXlhaW9qWi9nUlVlZ0NXajN6c040MXlrSnh4T1BBOFk4VUdUUUlVT1ZoNFpvQXdvVGFURGxiS1hhU0NqOVNQV1AvVGU4ZXJOY2k0cFRHdzh4c216Zkh0anZhK1AvWFBZWjk4bHJma0dmOHZ5ejR6Rm5Uc0ZHcjdCT2RWdThmYyt0b1l2WjZ2YTdiNHZORmQ5R01mNlBjZU5qZjNQUFZWYzl1UG14Sy85WjZoMm41Y2JGcEpQdDI4UGk0eDkvVE92eTkveWQ5cHBBa2NiTGc4TjNjaWhPSVRxS2FvU3FxZE1JUlFEUWg2b1ZSOWJLK0JkeEI3c0RUREVyQ2NtZ09CdklWREF6UnRKeVpFUVVoeElqQ0d6QVNVZkxyM3p6eFhmdnp5czQvSTRpcTE1cjdFUjhBUUtvaDdsbVd0T0tIUk54SW5sb25vc2hrK3pGZFJFamRhaEd3VFR2WGJ6cjhOM1U5TGpQYVVEUnFJc2dzQUdnRUxBQXhLY0tYQXFLRkpMNmJaZWVINFYzMmxxd3hwWHZmQ2ZmOC81TFpIcDc0TlJrNHpBRTFEZzU2VmoyVmY3d3hWL3hIL2pJeStXR1c1NnNYM25kcVc3bndxbit6OSs4b2RpemNLcjd3cGRQbCt1K2ZLbC93OTlmdm43OStrNHV1dzdaM0svNTFyejgzOVkwYjluNzMwMnNPVFU0bUtvNUpCdzdUZklsbjVpcGxrWTltalZrNnhNazFNQkc2NGo2a2x1bW4xdHlmakg2cExTNlRJTXZuemd5SUJBd1Q0VVNOaDFoYUlEU0ZKZ1RZcWYwYnNJMGJOczliVGJzelQzbTdkMXdRNDhSY0tiU1NGVm8xU05ObU1CbGg5UWllVkE4Z1FJS2w2cm9aREJRNjFacVllOWhCazRXZHZkL0F0RWFRb29La3ZKd2tZMWRVTlFCOUVoTWp3YUVSZjM4VkJ0cHdzTE53RHYrOE4zVnkzL3Y1MlI2cGlUZ09JVkR0Z1FGTUU1TktUZHU5S3dxRmRrY1JXRHlsSXZ1RXNtVVVJdU9HemY2RERRTWhMMlpUajMwSWhMdVB1L2tvK3hQLytTLy9RSTNWQTBOcXRIbGdVdnFKRklMS2F4SGxabE5xY216VTJzbUFmNjhoN1NXVTM4d2xWTHFTeis3L1lGang2NWFYYkVmYXpHWUxFT1FsWFdXTkVzeUM1UkFaRVNGTVVRV05NeExIM2V6L1puREM2eERERHc1azNZelA3UFF1WG9PY1IrYTVxUXdjeVBwbExFWUlxV1pVU0pQYUdGQ2I2QXo1dnhNOFJSNkU4dGViTFB6aDk2MUYrWnB6THMvZVNYMjdaekRVT0ZSVDU0YXVYS3VwMHFwa0V1NXYyNm5HaVNjS1NiR2dPWk9wNTkveDN2Q0M1NzJFbW0wb2t6RHVCRitPY1ZWcHFkTnRtOFBraytjSXlHY210S2FlQ1JBekY5Zk11ekdqVDd0NFNCVVQvajV4NjArTUhLOW92WEFxaWxSaXVEcGplcU00bzJhR1pyaURBbm5KdFRWU0IrMG5zZ2g5QkRzNEdjUHVSWWJVcnRuWDlsMVFxTm9EUnVyTEowYXM3TG1ZSXFrbWxaajhsa3VrQUNxb3VLd0JCSlc3R1czL3lVNWNFMmV1dGtSRFN3QU9RVjkzWjA0c0doMkRiMUNDcGp6SUlSU3RVM1M1Q2FKdXFhN0YzQU9USGd5UVplMnBPQkZFZnJRMHgvOFNKSmoySTU0eUJySUk4N3FZSHl0UW1MS3J3VUpMMHVHTE9wSWtYTzdBOVVuRFFONlFOWFVtZzNvR2pUZHJrKy9Kajd0bkU5MG4vNnJwOGgyQ1RJekV3a29OOEp6NjZSYlpreldhNmIxKzBRMitPU2t5eDZiOHZpVlY2N3BYL0N3TjhtWHJ2K2dEM0owT2RTSWlzcEJTVTE4cnJTWldzTzRXU1VOdFFlbTkxTmxIU013QW1EZnJxK0JTNU1vYkU2ZWZQQ2xyMmx6MXh3OFhKSmhYQXJKVW11RjFVZHZFa1JNLzdFSlNoTmtDMDRYRUc1OHl5VTd2a0ZBRHRmcDBNUDJzeFVBRHZUc28rSUlWYU5yR2RBQVE5c1M2OHd6RlZkT2dBS3dnaWtYdS9RV0JhZ3VBczBDV1B6V0JKNTF0TlY4SmdISVNUajgzcFl1RmpxZlE0T3dBZ1kvK05uY0xnRnNVbGlRS0VBV0ZQUDE3MEhTOS9NVVU2R3RLSVB5cTQ5dXRUOXlYWHphS1gvTnYzanBJMFdkeVhZRTJUd1Q2eU1IQ1RodWhCLzhTNjhnR1R3cEFZU0QvL1hPazdqcHZOK0x6MzdHWnhzSDl6d2ZhMVkwcWliTmFYUndVZzljT0Joa09LWU4yTVFyNDZDS2xvUWJKektFYVJoRkhMN3NKWHNBQUM5SXkyRTFHREYwMFhrWCtZQjBYRWRtZ05UaGVDazAxeXArU2QwdkltQk1TallrUm1qRUh1MTlGRE9JbWUxNXo3em9UVmxIOHVvZXJ6eUlHRkRRdVJIQ3R3d2hSb1JPQkpwWmxubFFRUk5wd0FCS2tTZ3c1aW1ReXJDaWFPRVhwODlKK2hDWlY3d0hJaUlWVHRpd0I2SlFiN2JrcmNqdFVnclgwcURRVTFKcmxyNUhjNTRXVDBvRG9rNjlqUlVSUjFjcmRHai8vOEVYMzNrVkgzZjBSL204cDd5YXIvN2p4L1BUWHhzajZVU0xLTnNSQnYvVVI1S09sMTkrQXAvNG1KK3Z6ajdqcmFOL2N0bjE2Tzk3dlR0NjZMUXdKaFUxUUlvS1VtU2dKL08rNEJPUlQxMFV1RWpOSVRyVGYrcTFHQkdsZWFFcm0vYnQxak9mL2hrQ2dzMmJyYTQzU1FxdXZ1bEVEd2RoeUdFNXN0NzlXcXFpUXliZUpROTJpQmlUQ2s2QzI2MDl1MnI0NEFkU2R0L083eXFqSk5QcFFHeVJ6bGVlY01Mb0RTdkg5QnhVREs0Rkp3SG83NDlvcnNuN2FpNEJGZm5JMVFSd0pDQTFIL3ZuQWhyV3hIWHZPQkVBY0dPR3orcUJ3ZWh4NzhUKzYzOEpLNkY1SmxZZi9zdEV0Y25nanRSQ2MyRGVKS1lDUk5Za0JRQXhjMGFsRm9qUVJZY1JmeEVPZnZvaVhIdmpIK0FqYjc0TDk3bGZ5UXRPMkdhN2I5K25SVk5zc1V1c09XN0NubmpPNDdobnowclhjaXQ4TXdMT0lZaFVTV3lQWGpRNWxDbW9tdVJEVE1qMGlid1dWQjgvNjNJT1NsU2dkSGhhOE5FVnBlTlJFeDhUMVRKVjVRaTEyaENCVVRHOW9HOUliQkp6U1pVeUh5aGZqNHBqcnFJSm9JVEpNQ28waFd6QUZUdFEzdlNxaGE5Zms4SzRmRzhacFcyYjRBRFl2cmE5QzU1UVp5eEdEZG9rUWplaWFrZWl4VFRwcVlsemppS2VrcXBucERtclZ3RTdNS2VwK2IwcDMxMHpxZURCeTk3K0VUU1AzZ1VOaWlKdE9pQjdhWTFxTFEvWktDaVNjM0x5ZEFGZCtwcjRta1lGRC9Fd2FMUlJEVmJzRHpoV2pzV0I2MC9DMnRubjZQMVd2Z1NuanJ4WUg3RHVKWHBNL3plVWQ1M28xbkNGRFZ1MFlSZWlFNFBDdzFGRnljRTZqVnRHLzNHNVZjeXNEeWFBWXlBY1UrZGhCYWdJR2x1OVhuSCtmZjhCNUdBem8yWm43dis5bHo1c3JDUHJBeVNBNlhqcXBlTEtEREJZUHNjMHJjRkhFQlhIRWVHSnl1RHdUZGQ1UDRDd0RadmNrYllYdjlQQTIxT1kzcjR2enV6cGhUWWE4RzZZYkF3YlVFQjYrNE9nbWFwY2N3TkNBTE9xZFlJekhaS0lxQWV3OTlaelNEWndWdnJsQ1QrZVRNMzRjUTk1UFdJaGFORGdCZlFDZUpITTU1SmxWYlRBQStwSU9CbUU4cFFxa0tLSEo4UW50cjA0T3NBY3ZIY0dNUnR1bVRVYndSb2FZbE5EYktLeXBsU3hxVEY2bWpnNlVmTlM1R09zSEFsUDBTS1RBVHdUWnl3WldLQjE1VXpOek0wVXRSeWxOakloMGFIbnc5R3Q5elZmLzdjM0VsaUNQbWMySisvOHdGWG5OQmFjQ09zMUZSc1VWWW05WWZWcGl5Q0lnQ2dGZ2d5RDhKVGlXOXJ0ZjdyWS9ZNlUwN2ZiOTZXVE5RMFlKK0grNkliK2JidmErRGdjQklBMVJnVHF3YkJnS0dlTmFPV2ZkaktvZGhQZ1VlZFRVUlJxT2x6ZUgxc3VPUlhUcVVvSEFOazhFd0dCL1A2Ly96VTY0MWNEMGFQaFl1SmlnU2dFOEpJTEtrbU1MRTlhQVVHRGRUVXZ1ZUlXS2RMM3BTcGJKUDljeXRrZW9tS3FpRTRGVG9SZUpOMEdUa1hGcVlobWxURkZtaEFvVW5TUS9CclRlWU9FU3gyeTVPTWg4cFpFelN4Rlpzd0JFSk1Zbksxcnh1YUxmdjNQU1JOTVRTM3pyaW5qRk5YTjh4ZU1qYlJva0xZYUJoT2s1ZExJaWNCanJCQTVnY2dHWXZEcS9MZGM5OHJMRjIrL2laZzZvc3JkUFdwVjFrZkVmWDJCYjJnejBXVjFCUERONUZHOVhhWFVvVlE5UVlXa0tyTW1BOVJWc1l1WTZMcGdlMThrY01STnl4RDJMVk9KdUgzeXhjOURXQXRvVCtBa3pWTHJjVi8rSGFLU0RKby9wcStOVEF5RWxuMHF5T3JadEE1WUk0TUt2ZjQ2MDk5bllpNXh2SmxYV2VDU3pvdlQxRUVNNXR6T0JBbk1xTGNmYThZcG9SQlJneVc1Q1ZDTTZnU2UrN1h2K3MrUVN5Lzk4bktsWFU0bW9kZlpMengxVTZ0c25kT2xCRWxYTUhIUWw4SjBQZ1k1c1RJckJEaVVHRWVFUW1TUFZMemV6YjgyMld0YWZ1QkRPWmlGYm03OHBlYlZaNjJSYzJQRllCMXo3WU9KTE5nNnJXRHJhQStyMG5VMEZZZ1RTVGxaaUJ4Z1ZDUGp3WW5nVG4vK2ZmR3JMN3NOVTFPSHZ0aDN2eWVHTjE3eUxEZi9tYmVqV0NEUUNrYnphWXVqUHMweDF4aFdUOU9nVWgreEhUTXpOV2FkMGxDckZ3a3M1bTJVdEdoZm4zWUIxbWZRWnV4QXluUU1tVERCL1JLVU5TdVZRYUNWQ2lvRmcxTFNHTUVRVktSeXdnaHFkTUtLTUhOQlN6cXRacVZjdVAxWlRkZzdjanUyRE51R0UrZmkzSm1iUHJIaVJubDBHNDJLTkk5YUZEbHRZdGVxWVVJSVNoRHpxTGdTcGF4RmpFMXAraS82emhmT3JUNzdNSUtIbk5mMC9TdStiMDZjNlZ0dTV5dkxLSENPVW93b2lnWWdUdGk3dlVwM2ZpUGxSRTNzQ1VvOUx2UWk2Z1VvZkhRclp4djQ4ai8vc1lobnphdXFJVVgrWW5UK3Q5L3pyMWo3dU45QVdGUEJoMEtiaGNCTGhGY1RsN2ZZY240Mmx3SnNLc0RxcjZHR1VVa3ZVdWRwMWZybmt0aVVPQ0Zkb3ZaQ2hPcVFjMm9tSU9UamZxa1VadTU4SnYxbmFZcEVIVTY4ZjlhYkZLU21VM0Y4MmZXMlVxcnk0ZmY5alNic0hVa0phSmx4SnllZEFISGgwa3NmTzdTajgrZ09pa2dFTHpLZ3dpSmowVFgzaW9DeGhORWpjQ0tYRy9OaXVNck52UlpBbk1GbS9hRUV3V1VHa1ZQUXAzeTJ2T0tXT2ZzOGhxaFdSR3V1SWxDWVdFbDJiZ3ZVRVlFcGdVSlNyK3B5WGg1cytNSERXOFNxeGQvZ0c1LzFXTGxLQXJjdWd4Tm5FRG0xMGN1di85dGJjUHB6endQUCtCQU94aEkrZW1pbFNZMno1cUZtTm5paUxTNHZHcFA3U28yV1pld3E1MDlrS2t3eUNrUmNFbjNKU2lPc0NYaEozWXA1RlpESTZZOWsyZzhpelpJYW9ob0NnbGFJcmcvVnN2U3VXYmw0MXJGWDkzL2w0dk9hbi9qNFc5S0ljZW1RWndLQ21SbmNTcmJ3eVZ2ZUpBdE5ZMTRTcjNWUDByMFFNcS9ZU0JBbFRFcFVXQW1UQWpGNGNlNUdiWC9wSmIwYjMwTk02ZmM2Q2UyN1duOG01VXo3NHU2d1piRVMwUUx3SzBBL0JNaXdhTyt1Q3RWc2hJNkltQXp5M0xKV3BzNkpoV0RGbkdIZkp5L251OHpKMDJmaUljT0I2ZTJCVTFUNWhlbnI1WVhYL1R6MC91ZWhkOG83MEZ0OU42VGw0SnlIVmc1RmROQlNZVjJEVkJFT0JpZlJIQ0tjQkRnRzhWYVpJa0l0d0RGUUxFQVlEQXltaU9wWVVWbVpZNFFpaUZxZ01GQXQwRFBBTWNBalNJRUlSNlBMa3E5VlA2cUR1aXFvYTNlZEl3cVllWGpzc3FORzM0SE5GejNOZitucUMwYW5wNjluRlp4c1AveEkySTFPaWlJZSs2VE4venI2emZZcGZXbWFNT3JTcENocmZPUThuSUZQNlNPZ2dTamppT0lBekRxVHExc0hwd1dvWnBaeHIzN29nN0U0Q1NjemlEZjhzdi9BQTA3UUo2T01NZmJoNXUvT1ljc0pWang4cUY3Z0JYd3V1T3FodG1iMWZaR0lxbkxZd2ZmZ3I5cS9LQkRXejcwa1FRakZUWkQwT1FmdXZIME52dmo2eCtLMlQ1OWpYVDVVYmRlSnFPd29yRnc1aE1WOVFGZ0VlcGJuUUFxWVN5cDdJZWZqbUFjejBmSTEwM3FUWkVEUFJoU2dMOHQrUm9CZVNPT0pvQW1Xc0FJWVdZZnF0anZ2ZGtjZDIrZjR1aDJ1VVh3ZTkzdlFkUXNYUGViakt4NzN1SDBJWVVtTWJWbEZtd0w5V1lYSTE4ckZGZXQvYmFTejhpMjllRlFnelNGckJnN09rRW5sUXRJSGdMQ0xLRjFFcm9OaERNRUs5WDViWStHakYvYSs5SVI4RXAzOXp3MDhCZFdYd3k1N0lNNzh6ZlA5OWV0WDBBTkE1d0NrTzA4Z0VJMDFYc1llTmt3c3BySUFMZ0hQMEVFb0ZWQUlsUWlZeHkyOWQrR0NXMzlOTGw3ZjRSUTh0dkJRVmRoRERKMGZ4VEJZdGgxdStOb3BXTG5ySkh6c2I0Z3ZmMEx3alk1Z0FjUlo1eCtOWTg0NEdzY2RkMy9zK3JxaGMxQnNZUTY2c0Evb0w4QklRYmNreWdEekJYUnNCRm9Ndzl3b3JMbGFkR3d0c2VwNHNWSVA0QTEvOVNIZmRBajlpQURBMysrKzlNOTdpUnpzclBqOHlrc25GMlZzTEdKeGNUbXYyV0Z5RXN1SkFrdEw2eWwvTEQ3eFdTOGIrZVJOTDYvS3NSQ0VlVmxRTXUxZEJpTEpNWi9vMUFleGlDaWpxTGhhWVUwSTduQ3grK2JtanZOZXNYajMxN2NjWWJEd3d4OXR0eEgrd3UwSUgzdXFlL25qenBhWG9XMEJYdDNzRGtzclZvdkU4UDFhR0RxcktaaXRpQ0lkVXA5N3ducFJPQ3YxVzBUVjk0aG4zNHpWbXk2UnphOUpHL3hiNFRDWlNFbUhMRW5QYkZiY3VFY3d2ZXdnNXAvd2c0Qmc0MGFIZGV0cTBoK1hYMUpPL2FsaWVsb0VpQ1I5K1pCSHZrcSt0UC9Gd3JXSU1FQlV6Tktldm1TUkoyUXZUdkt1d0VLV1lEcEtqU0lJVGZHTmR4UjdwNS9WdlhsTGZkVGc5L08zeXZmOWdxWWd4MCtqK2ZGTDNUVm5ISSt6cktmUit1Ym03ekNpSWVBQ01mcW9JVFRYT21FUGtNYWcrd2NVTWdqWmxTWE0zRXFIZzJQeldIdkJHM0hSKy85R2pwRTlkVXJBNUNRd09YT0lzUWQvQjZjRTJDQ1ltVWtkKy9KejNmZEFscmk1MjM5QXN5MWJRTjUrR0dnL09abis1VUhCa1c0MGtvSk5tNVp5cjNNb24velVjK3ptTy8rcGVWdjV3S29hTWNZZ2dJb0pLRm1EaDRjQW5Na2wyekJXTUs1RmxLWWllbWtVWC9DTE41M1gvOUpEbWM3TS9iNFBpLzcrVHgrZGdzckxZVzk1T001N3lrWi8xYXFtcUpIYTNXUFMzVS9SQm1odHdjUVRoK0hISE5oblFwU3lNSGJtUUM3SlFJc2FRa3o3eUF2anQySjI3TVg0emE5Y0tTdmRiQjE1T0FVUGJBUTJiQ2NtdHlaajVvdWNDMlgrUkx3M0F3WFl2Rmt4TTVOelVFNG5wSlJiTGp0SDN2L0ozK09PK1Y4dUZscXVsS0VTMFh6aTBHV2h3clFKbkpXeGNxeURvQXRpQVpIakVqaXVrSUl1M05HTStuYnNldVRMdWp1dnRzTnkvTDFtNERxTXltYkVqejNUWC9hNEI4dWZZY0ZLVXlrV2RrU1VaWDFxbDNMOGtsR29FMkZwQ1I5V3lUczZPdUJPd0NDb2hGbmwxR094QU96b096Risrc2V3ZXNONzhJQlhYeW5ycGZNOUxyYkQxT0d2WVNPT1RBVDdYbzl0eTk0YzV2MUxIOW9STDY3MzRQWFhIMTFPditJSmV1dmRsN3JkOHhmSS9ncFZIS0pSSTBpVjZFU01rZzdyUVQwZG9rRWw4YmRFcUU0T1dxUkh4Q3BuYUJpQytxTDRsK2JleTM1dDhWdXZxazlmLzBGczlvTWU4UzdjQ3BYTmlEYyszMy9pckRQd2FKdEhaYUNmKzNaTUx0VWxmTXRoL0pLUnRMc2FFNkVPdWt3K3VWNk5xdkxiS0laQUlJWkVtT1lvc05qWWdXNzFLWnoyMUE1dS91Qjc4YWdYR05aZU9EdHp4bDllTzhtdG1VblpqRW5ZOU1mMGFMYkFYdGNEc1BhSFByU3UrVi8vZmJZYzNQdEE5KzA3enEzMkxqNjJtSXNyMERZRWVrSmRwRUhOVk1Bc0VtaFNUMXhxblh1U0VEcUtqQ3NXWm9sb2h0VWFxQ0tod2FLeHpTOWNlV0Y1MDJPSlNaRWY0TnpnSDliQTlhRzhlTTBHSFBYMHA3alBuVEF1SjFxZ3hSSnU3cHNSV29EV2cvZ1JoNG1uakM2ZEQrd1NYQ2gxZUJ2c2F0VWNNMDJUWFNOUkJhakNRUjFRQ1ZBUjhHdUJCU0xPMzMycnlCcm95Z2VKN2J6cE05aDd5eTZOTFVGemlEWTBBaDA2RVJoWkF4dWVJQnJqb28xeFFJZVNRa0l4QklOQ1djRjZBUWdsdGRNUmlBUDZiV0IyRDdCckY4UHRkd3JhQzBDN0Q4eDEwL1N1SCtnMlBPUU1Ici91QWZHNmF5TjZPdDRjbWxpTjJjVUVxNVVPTWJpSytiQTJoblR3Q21JU081UmFZWmpwc0hFQ0NUWVJDdFkwcEwwUTBlMFpWc0hRa0JnSzMzUmZRdS9iTDYxdU8vL2pXRGh3VDduL1hqZndJQjlQd3k2L0NBOTc4bmx1MnpGamNCYUEwSUVzM2hZaExjQVdJSDdVY2V3WHh4S3pzRFJoTGZBemlGQ1F3ZkdFR0J4UnlJUnZTNjJYQzBSUnEwcHFvTUEzSGFvQWRQcEFvNWx1akQ2VE5oTUZxRnp1WHpNb1pMSjBrMFRtM2pkck9RV2tQamZrL3JkZUhJaEpvaXYxeDBnOWNwQVVMQ0lCYVFKQkVUb2F3WUlTQVFuT01XU3NPeWtmZ2VrMFhDWThQVkdubVE0K1NPalloTUlkUGNUT3JrbzZjd0dqWWpKRU15K2VYMitGOERyZWNlR2Jld2MvZDdnUy9RL3ljRC9NRDAxdkI2L2NDUCtMbjhTT0I0L3gyNmVjSUU5ck9BUXBSSHdMMHQ4UHlJZ2d6cHVVMzZqUXVHOGh1c0lCSmRPcHh3cW1YbGxTeWFCNTYzWndZbk5ka1VFaHFnQlUxQUZTU0JvNUtPQ2JxVzJNRXNGYUhnNEVRa3lhSGRIU3VkcVZtWlFVRFJFU0RCclM1elVhTkppbDA3d01GczJRampvaEdDMUw5MWltVFpKWlBhTW9LQVJqV3B0U0lkUGhXVmtXWkhDakpwaXozaWZPeHhha0VDV0YwWjNaZ0w5dkM5MDdTL1QzVlJnU2t5RVlDemp1S2FMK0IvYy80eS82ZXo1MkplQi8vb2MwN2c5dFlBQjQydzdZbFZQd0YvOGpibmpRbUR0NDhnbjZwSVl5dWxHS0d4SXA5MUprRExTUzZIMmxSTEcrRUhkTUFmYVNnbDdDcTdOeEIwWk9KalVQaU12RGY0VlEwNzJROVVPRXlreEpGUWVGbzZyTEEwVk5CMEZJaHJSRUtlb0Vva0xucU9vSXB4Qk5TeWRVQWRVaFNRdTVGS3RWR2RXQnFxQXFUTlJGMFN6dUtrajhqWFJXbHlZeXBXbzl0azJpTlNJa0ZhSkNCZE94QklJb1dwV0NveHo4QTRaRlZ6cHBmN21OM3QwOU5KVXk1STBOTXU3ejlCOXlDOC8rM2Y0ZC81SDYzUjNoZjFJMnVQL0pENzl0ZXpMeWs5NWtuN3RncmZaT1BGRXU4bURVSVdneExGTHVCVENVYnVIZWRhWG9zS0k0eGFWd3lUenBVU3daV1FaRDk5UmFaUm1KMnFGTmt6aEhVdjdKUHpNNEFnTkxKNUlkRmcwa2YxTTZ4U2lKUzJsV09aSmF3NDcxR29vd2pVcUZBeHl1MWlldmUvbzBUSks4cDgyTU1RNld5cGlmTkNtTkN0aFB3dzNkTUN6dUFTTUNVWFN1WDBSdmI1Q1dVeG55a1EySUxSUlN2TCtZLzczbmRuZjg0K1U0cC9oRmZDNzhUK3RDOXo5OWdyZHRoM0VLL293MzJLY3VYSWYyc2V2bENRVmhyaW53b3lMbEFaS0E2RENrLzlXS2RzQlFuTmtRYWRiRmt3d2FXcWxEdFI3QzJCYlJtbmVYbXEzQlFZY2ltYU5jczkxa21TNmpITFo5SmZtR1NkUDFmQ09JSlY3QW9MdzFIWWdXaXRhZnpzdjFySnZWekx0WVl0WUlOSis1bGM5SVRTSmxnV0FuQ280cnFPZXVFTGUrS2JZUTJQbmlIS3Y5RVMwbmJMcG9EVHFiVnhSWDZQd0xmN1Z6eDk4U0cvMUQ3Z1hqM2lzR3pqblpPQVYvbjcvbHB4ODhMdnZYcjhlVG1nMkJhOENhcTZEVkxNUktRRmRDd3M0bzVWY3J1UFVGM0lrRlVNcGc3Z29xcUNLWkxpTUR2YlVsMWMzNlFHR3BEYlJNV0NsZlZhUVVVUCtjVXFBNUJyZ1VIWmdQak04eW9VakgyRWdTWHgxUTF3WlZQcGNNanJ3eHRHeGdrY2ZHYWE4L3A1OFNRRGNvaGhYNm9GSG9BOGFnQlRUZTBVSDc2am5FQlpOV3c2Ulp4RmlJeFBtR05kK25zeTk2Vm1mbjY5TXR2eVBjVzUyZHU3ZWVxRGJ5V1cvazFRK1owRnZXSFMxUEhoNUZvWWJZT2tyVU9rQllBTnhZcWx6TGEvcEFDZmpUQzBoTGdXNG1mcnA2a1VzU3ZKblRyemhKWkFJTWpMdGtZRm15UnhhbFdxYW9wWU9iZ0NJaTZjYkpETUhjbnFmQVBIREkydXd5aUxjWmJXYnE1MU5KSUVsVUo5OWNLcEt3eDdhQkxRWE9Ib1o3MElUSXFpWUVsSEJqRzcwdnpJc0VRYk1oYURoR0QzVUhDaFR2a1lYZmYrN2lydGVuYzZEdVBlUGVxd1plYnVRTmIrUU5EeUMvc0dxdFBubkZPZzVMSDFYemFIRUNzRG9BRVEvb0tGRGRYS0g4U2dsWjZlbFBkdWwwOVVwRVZOT1ZyN1Bud0VhNThsNHlxaXlmYUdlcXhjQTZ5NU13SUVpaFBtVmJwVkJVeEpqSkdvUDhxYWxlcnNtdmRUSzFuSExyZWlCM2VhS2E5RkRhQkZzQ09Xc1Urb0FSNkpvQzZzQTRINlM2ZXBiVjF6cmluYUx3Wk5GQTVaeHI3SlhZK1JUN3ozcm00czYzWGduNGsrOWw0OTdyQnE2TmZPVVUvQ1Z2eFMxSDdlY1Y2NDZYODFZZkw4ZWhKNkVZaC9pVmdqQVBpUjFBVndEV0ovdlg5aVhjRWFIckczREgrc1M3cXJLaGRNbE9wdW0wbWJ4b3RFdzJMcmNtTGgvQkpETDRhbEpZeVB5dEJQSVB0R1dZdGNVMC8xamR6ZVFsQXNrSytxbGlSbFpWVUsyaEd0RlNCSDFDV2s3a2xHSFJEV01pSzcwb0l0Z1BLRy9wU3YrcU9WWjdBL3lRU0ZGRU5wd0VhVW56Sm5TLytjRnE0WkxuZEhaL2pJQS9HYmpYamZ0REF4M2ZGeGlTaC9sSEFTTlh2Tmk5NFVFYjhCeEhBaVVEUWQrN0EremRtY3BaTkFIT0FRaGc0NndXaGg0NURIOUNJZWdZMFk1Z1RNdGprSFI4azZiek1KUElhTXlDNmlGdlJvUkVsQk1ERVFCR0VRa0V5cXhFR2tWWUFTaEJWQUlOQkNzS0trQ2kwcUtDSlltUTVUVXFnWlFpcUFBR1FFMXBBV0FKRVdrQWF3cks2cVpJVVFBVmFXMUR2TE9QNnFzOXhIMGdDeWV1QUFwaGJEaDFVU2xmWVBmOWYybnRYMy9md3NMK3cwbDUvMnNNdkp3c1FBSWZtdFRmZk9DNWZQV3hSMkVjaTZ4UWlJc2RhdnNiUURtYndDRlJ3T1pBVjRGdVEwdGFqeGhpNHpoTkVGK1BZRG1RYVlRT2xBMXFKQ3FqVUVHQWtNTnFFQ0JTRXBLVm9kSklzaEtSQ21ZQmdrcWdKUVNCWUpYMGJWQUNDRUtZQ011a3FTSkJJQUUwVTBKVU1PcUJrUWEwVUtBUDJGNlRlSHVGc0tOa09FaUlPbXBMSVVvMEZKWHoycndiVm4yNjEzM1o1T0xldndDQS93bEM5Vk5oNEVHUm1RY1VVdy9FV1UvL2VienB6TlB4eUh6Ukk0U3UzQXQwNzB3TUhCUjVUWFFSa0JKU0hGT3dlWFpML0prTnVvbk1SdTlHUVk5RUJVRzB2SytWNDJhVnFNU1dZVW1ORkZZQ3FXcllNbmsyQW1nbFJCTUVLYWdTdEdrbVJCQWlKTTFTaTZSRVZUR1hHQ29OTHlnY0xSbzRheEozUnNRN0EreUFwZk04MWRFMUFOOENuR29vVEJWZTNBMmhmOE43KzUzZm5qNjQ4RmtDdWlVdkdmeW9yLytQM01BRFZzZ1UvSVhUQ0FEY2Z6OFB2M2YvTS9FblJ4MHRxN0NRejNKWGNkVmVvSHNIV2MwQ1VrQzBnVFRlN3FZcTJoMWJvRGlqaWVMa0FtNmRDcHdDM1pDK3AyOWd0UXgvRHJtVkNaS1VsUk9tbkVKNUFDU0k1UU5TZ0Y0V2pxdlNpVUhSSUdJSlFoT1FwQk5od3J6anJDTHNpYlRkQnU2UFlpVkJwNkFtWHJWM1FORVU4d0lBM3UyTVZuMlYxZDg4NGU0OTB3RGFWd0wrd2g5UnZ2MkpHaGdBcHFhZ3IzZ0Z6QXg0elprNDhjbVh5R1hIck1melZrd1E2RWpNQlpXV2MwUnZCeVFjbElUeU5iSzRXdzlnRDZZUWNhc2M5TVFDeFVsTy9Gb1BHZE8wQ1dGQzlMTkIrOW5nSlFSbEdrZ3dFRklLTGFURFZ5d3BHTXJnY1BFSVdBV2dCN0lOMkFFVG15V3dEOEk5eGxBU0JrVk5vcFlzbmVFVUtMeFlVYWpCb2VoWGd0djY5dkYzbHQzTHBnL01mMEVBdk92SEVKSi9vZ1krbkRnQUFPK1p4QVZuUDBpbWpscUh4NCtNQ3RET0J3UVpmT3hDd202ZzJnMlU3U1RMS1Y3Z3lDVHQxb05vVEtvQ091cEVWampvYWdjZGM5QVJNV21KcUYvcWd6Tnpra2x0WDR4OUNIcUFMRkJzd2NCWlE1d0Q0eXhoQ3dUN0JQdUFCQkcxTE5pdmFRQW1FU2ljaUc4SkM2L21LaldJRnFVQU56SGNjRzI3ZXZWdjNEbi9INE50QnVBSG51WCtyelZ3N2MxYk5rQnFRNy83R1hqOC9lK3ZMMW8zenNkUFREQ1JBZnFKY0d1RXNpM3M3NFZVK3dFdU1IbVpxNDhaU0VVV3U4Z2pQV0htd0VBb1VFM0hYQ0FtN2pzaFFGUklsUTRHbFlycEhFbUNScGZBRGswc09FWWh5clMrSW83VWhvZzJCZG9DbTVYRXRFdnNOQmh3Uzk5dXZHYWgvN3BmM2JYNGRpUWc5c2VXYTMvcURMeTgwc2FXdkNNSjRHMlB3UVZuUDBoKzVaaGpPYm51YUZrTEE5RE4yNU11SHgvZHBYSU9VdTBIYlI1aWk0RDFheVphQXFaVkJpcEUxTHh5YTFHWWQ1aGtjSUJxMmlsVFJzQ0N3RXlKZEVZSDFhdkE1WTNXUXFCTk1hZGkrYVNiQWxHd3UwM3M3OG4yR3l1OGVmTTNELzRuZ0w0QytJK2ZRRGorcVRSdy9kZzZDVGU1ZFlrMis4ZHJjTXpQUFZPZmNkeFJuQndkeDBOWHI0S0RFZWdLRWZMU2V6cW9CTmFGc0EySmJUQzBBZllBOWdIMkJTd0JWQkJHZ3FhcFowN0xoS1RsdFY3THgyYzBRR2tJdEJDb1Y0b1FubUxvS2xHcUIxV2dncklpN3Vqd3JtOTI0cnMrUDF1K2Mrck84TVg2WXVZOCt4TUp4ei9WQmo3RW81ZUZiZ0I0NjZQeDRMTWVvRTlZdlk1UFdOSENPZXRHT1l3V2xoZFM2WUxhWUZOYVVhVWpxaGlRd0pDQTJzRDFCa1BTK1FTSWl1S1E5eFg3a282RnFWUUhtdzhkaDcxdDR1NmUzYnk3eEhWN0Z2bUIzOXJkKzlqOFBBN1VyZURNSkhUenpFK1BZWDlxRGJ5OGY5NDJCZmVZVnlEWXNnejJ1ck54MG5rUHdFT1BPUUhuV0lGSHJseUJVeHNlNjBhS1BCbzBMSzJ0NU5Oc1VKOHdOemdFSmJkUmVjMFVIVnRhUVkySzJiYWcwMEYvc2VTM0RzekxqWHRuOWVwdnQ4T25YbmhyK0dwcTJuTHJ0eEYrMjNiWVR6TEgvcTgxOE9GZXZRM1FUVnN5U3J6czhidEhZKzNESDRMalR6c1JwL2VDM0hmZEJNWWJCVS9zOW5DU0ZqTGNDQkJXMUVZRFRpdXhySmVMVGhkVnA0S0ZFaHdwc1NjRzNMaG5BZk5GNGI5NTl5eHYrTlNPTVBlM08vR3RaWVBCdElrM0NUZURKQnduUDJYZSt2L0VZMm9LZXVWR2VFNmx3M2kreDgwN0VHYzZGV2h1QkZxL0FveE1Ba05Za2x1NzV4VmFTWmo2bFJ2aHR3THVmNHRETEgvOGY4ZHJVb0NScTFXS0FBQUFBRWxGVGtTdVFtQ0MiIGFsdD0iSW5zdGFncmFtIiBjbGFzcz0ib3B0LWljb24taW1nIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im9wdC10ZXh0Ij48ZGl2IGNsYXNzPSJvcHQtdGl0bGUiPkluc3RhZ3JhbTwvZGl2PjxkaXYgY2xhc3M9Im9wdC1oYW5kbGUiPkByal9ncm9vbWluZzwvZGl2PjwvZGl2PgogICAgPHNwYW4gY2xhc3M9Im9wdC1hcnJvdyI+4oaSPC9zcGFuPgogIDwvYT4KICA8YSBocmVmPSJodHRwczovL3dhLm1lLzM3MjU4NzM1NDU2IiB0YXJnZXQ9Il9ibGFuayIgY2xhc3M9Im9wdCI+CiAgICA8ZGl2IGNsYXNzPSJvcHQtaWNvbiI+PGltZyBzcmM9ImRhdGE6aW1hZ2UvcG5nO2Jhc2U2NCxpVkJPUncwS0dnb0FBQUFOU1VoRVVnQUFBSGdBQUFCNENBWUFBQUE1WkRiU0FBQllwMGxFUVZSNDJ1MjllWmdkVmJVKy9LNjlxODQ1UFhjNjZZeEFJR0dTUUJnQ2lJQjBJaUNEb0NoMFVBRVZFZkFxeW5XNDE2dW9wMXVjcm5yMXFvQXlPWUdvM1lDaW9reVNOQ0NLRWdsVG1FbEM1dTZrNXpOVTFkNXJmWC9zcWpwMU9xamdkUDE5aitkNUlOMG42ZE5WdGZaYSsxM3ZldGZhd0w5ZS8zcjk2L1d2MTc5ZS8zcjk2L1d2MTc5ZWYvTVgvZi9tSHJxaHNGOVg3WDRXelJSMFovNVZmL3puaTczMytHRG1PUXd3ZWlIeE4vSXZBLytqWDBVb3JBRmh2eTdDb3BtQ00vdnQzOFVNQk9EdUxnOHJBYXdaRVBTQlFmL3ZHWnorbjdqR1BpZzgza1c0OUI0RHJuL0dzNENtOG1FdEJYTkV4MTVTeGdLWjVSZW93NXRIQnZ1WVVKckZTZ0ZhZ1JRcGFBZ0VBZ3RJeEJiZzBHdlFJNnp4dEl5R1cybENKdk9NUnlhdVh6dUlNWlFBUkhXL1RFRG82ZElaTDVkL0dmZ3Z2YTV1S0hSM0E4djdiZll2OGwyemR0Y0hOdTFuTzlTeGRwcmVuWHp2RURTb2FkVG90YUhnUXhUY2YrUnVqaUZnQXBRVkVBU2lDQkNBaEVCRVVBS0lBQW9FQWlDVHBpS1ZxRUpWM21ySHpFTzZZdGJMWUhTZmYvLzJWYVZIU29OMVQrNlRYVjVzYlA2WGdWOXErRjNVVGRtdzI0YTJhWk5mNkZqQ0kzeENyalYvak95YTM1dGFjdTFvOUJBS1E4SVFDQ0tnR2dvNE1yQUVDQkZZUWFrY2N0b2pYeWtSSVJBRWxsaXNBQlpNaHF4QUdMQVdJQkg0Q3ZCeU9lUTlRQ3Q0ZVI5YUNESVpRWWJESVJxMWo0ZVQ0VzErUmQwUi9mZXpqd0l3TmMrR1JpL3NQNXRYMHorVllXTnZuUThVdHJ4N3QrUHNiZzFuWXJyZmhUbmVyaWg0c0lZQlV3S0NTb1RRUW5OZXpXNW9wem1OMDJtWGxobm9LTFJoVmtzbmRiYTBpMGNlZVY0ZU9jOEhFWW1GSWN0V0dFSXNMQkZiQ1RoUXpFYkdLaVhaTmpsQzQ5R0ViQytOWWNQSUVJMVZKM2trS2dtSWdid1A1UE9lS3VSQlFwQ1JDR3BIK0tUZEZ2ekVlN3JhRi8xdzgwTjFYdDB6WVA5WjltdjZaekpzL3NqT2hYVFM5TE5rcGw2T3pvWkZ0c21EQ1VKZ1l0d2lxa3FUYXFLOXBzMmpSZE1XME9LWkN6R3JmVFpOYjJrWDdXa0tQWWFCa2FvTkVCSlRaRUt4c0xETVpObUtJVXRnZ1VDRUZFRkJpUUpCQ0tUSlEwNHBJaUloUzRCbFZHMklvZEVkMkRpMkRjOXUzNFJuUmpiSjF1b1lBeEQ0ZVU4M05KRHlDTmhXRmRvZURzajY2bmVpcjYzN01ZQnhBRUJmdDhieWZ2Ni85bWo2UHpNc3VoUjZCd3dBK0JmUFA0aGI5UVY2VHNOWnNsZFRxekJneHNjTXloTm84enZvMEk1OTFKRzdMYVlEWnk5Q2MzTWpXQXNtSlpDU2xLa2NsUkZJaEZBaVdMYkN6QkFseENKaXhSSVRnNW5CSWlKd0d5NXBpaCs3UUlSaUd4QklRQVFTcFRROHBhamc1ZENnY3ZDaFVBNUNiQjBkbEtlSDF1R3hUUy9naGJFZHpNU001a2J0dGVZVWxRMndObGhIVzgyMzlTKzJYMXQ1ZUhqVFA0T2gvL0VHTG5aNWlXRmJsazNmeHh6WCtaSHFndHpiVkVkQjJ6QUV5aU9STWtvdG1iNVFIYmZiSyttUXVZdWx0YTBWSlZRd0VrMWdJcHhFd0pFWU1VckFZbUJod1dTRnhiQ2hTQ3dNVytFWVhsa0lXSnhoUlVTRWhCUklCQUNSRUlIRVBZZ0VjZ0VnSWdJZ2J1Y21FaEtsRkRYbEdyaFI1OERXcXNIeEhmTG9odWZ4K09iMUdLbVVMSEllcUxuUjA3NkdyS3Zzd0tib20va2Z2WEI1K2NueUZoQ0FIMEZqT2V6L2Z3MWNoRUlQQkFScE83UnRRZVdVMlIrZytZM3ZvSG1OemRYSmttQnN4SGI0TGZyNDNRK1RFeFljaFYxbTdrWmxIV0EwR0pOUkhxWFFHbGl4Q0NSQ3hCRWlNVEN3Wk5pSUVVc0dMSVl0ckZpeUltQXh3Z0J4YkZpM2VjWXVTeERFcUptUzc0amdzTFFJa1NJQm9JaWdvVVN4ZHYrT1JBQ0NKa1crNTB1RGwwTVVSTmcwdEEyUHZmQThQYmQ5aUswbVN5ME52c29wMEticWRqeGQvb3I1NytlK0JtQVNBb1VlNEIrSnV1a2Y3YlhxUCtkLzBOdTc1UkxzMXRoaEE0RWQzeDUxSU8rOWRlRUpkUHcrWGVLM044aU9jSlIyaEtNSUpTU0dTQ0FCUWdrcDRBZ0JCNGpFU01RV1JpeFp0akN3TUdMRmlzUUdab2dJbUJqQ0xsVlNjT2tRQkFJQ3dkbEszTTZiV0RvMk1nbUlWUElkU09LdlNZa2lCUkwzbVFJaFJWcGEvUVo0U3RIUXlLZzg4Y0o2ZVhyclZsZ05vOW9LT1VDZ253cWV4b2JnMHVncnoxN3Z3dlkvenB2L3ZnWVc1eVFnY01OYlpoMGVIdFh4WmRtajhTaEZIc3o0VUpRTFdaMjV4ekoxK3VLVEpOL2FnbzNWYlJnT1J5aVNDRVlNQld4UTVVQU1ERUkyQ0NWQ3lCRkZISW1CSllZZ3Nnd3JGZ3dMNTdrTUkyN0hCWVJFQUpkemljRHR3a1NBaVB1Q05HbUlpS2c0S2dORUtqRTJBVW9JenYwQnJUU1J4SEhkZllBb0YrbUZRVlR3Y3loNEhnK1BUZENUNjlkajQ0NVJnZGJzZGVROVZBVjRwbktyV2pIeTRmRG5XNTVNMXR6ZkcyMy8vUXpjRFkxK1dBS1EvNDg5UG1RUGFQNk16R25NbTlGS2hQRlJ2WFQyQVhUaElXZktuTmx6NmZsZ004YkRDUWtSWWNKT1VzVldKWVJWZ1kwUWN1akNzVmhFSEVuQUVWbG11SUROeE14aWtZUmxBWXVGbGRSWGhUT09LeVExVkNYT2l6M1NTa1NjZFlsaUxvUUlwRUN4czVNb0lTTFM1SGJxT0txREFGRWdVbEF4cjBJQ1VjajVHcDdTR0J3ZXhkTWJObUZzSWhEVjZETmFQRTl0cVV6Z2tmSWw1a3ZQZlQzN25QN2ZNbkFSSG5waG12ZHVubEg5dDNsWHlkNE5iNFQxeFU0TTJkbXFXWC93Z0xOdzZDdVdZTWlNWVNqY2dTb0NWR3dWSmE2Z2JLcW9jdWlRTVVjVWNPVENMMXNLeFNDMGtWaFlNbUpoMkJtVUJXS1VXSUZJYUNQU252WUNVNFVVQ0o1UElDdHdPRXRBQkZpd1k2L0loNjBhZU94QmtZWVlNWm8waEFSS29CeGpRa1NpQkFSb1V0QkU1SUlTSWY1S0NJcGlvQWJsdURFSUEvbGNEaUNoclZ1RzVmbk5RN0RNVnJYN0NnWUt6NVp1YnZqV3lIdEtqdzF1eTI1aC8rd0dKaFM3TkhvSFRNT1J1eDdLYjIzL3Z0azN2emRQR2lNVDI3MFQ1eXlSOXgxeExxZ3BSODlWMWt2VmhCSklvTXBjUWRsVXBNSWhWVzNJVlJ0U0pKWkNqaEN3ODE3RGtVVE9pMkdFWVluWkNOaFk2MVc4Z01nVHNJUm9sVVpzSGQ0MHNsL0x2alJaMnZIb1UrRzZkZlB5YzJrYXRhSk50VW1EVjhDUUdhSG5vL1V5SGczamxma2xSMjJtNFdrbFcyNXNtZDZSSHpjbGFNckRCZ0lseXVUaFE3RW9FU0dsRkRRcGdFQmFkTElKT2ZCR0JPV2dtampiSzBCSUNLQ1d4Z2FVS3FHc1hiK0Z4aVlxZ2diUHFHYnRxMmVEdGZqcDhObm1GNXZ2VDV6aW45bkFTVVRqeHJNWHZNTjJ0WDZkZG0xcXJvNlBSMnBzdTN6b29ITkczbmpvcVRPZkR0WmhQSnhBR1ZXYU1KTmNzUlVxYzBoVkcwaVZRMVE1bE5BYUNzVlFZSXdEVURCZ01LeGxpY0EyRXFOc2pwWHhRdmdUQmgxaHkvWVdyL0VQRUhQcjY2WWZ0L21hMzN6NTdzZTdOM0FMTlkxT292eW5ZWUpJb1dmZC94YnlsVzB6TjdmUTRiZHV1S1hkVjgybmlPVkRLMjJZYnJRQ1JSNG84dGhYU2toWUFZb1NFRWFTSkxqa0FGa00yV0syR3dTUXB6emtmRitJQ0lQYmhyRmxjQVFXTU5Ta2ZSb3lnYjVuOU4zUnR6Wjg1KzhCdnVodmFGd1FRWEk5KzM2UjkydjRNSm85Uk51SDdWeXZRWC82MWY5dTV1K3llK21weXRxMmlpMXoyUVkwSGsyZ2JLc29jNVVxSENLMElhb2NvV29qaVRoQ0JFdVJNQXhIaUppRlFUWlN4Z3U5RWhvandFNkVEN2NWbW04NG8yWFpiLzlyajU0bk5Pa2gzam43SUt5QWRsOTJwZjhmY0hWZjk5MnkrdENZUEpCVjQvZDAzalIwMTJ2dkhyLy9rQjNsMHFuanVmSmVZZDZIamdvZ0M2TUJIZWRXb29oSWhFUVJTQWxFS1BGa1JSQWdwenpFd0k3eU9VL0s1U28yYnRwQlVjVll0T2FVS2huQzZvbi80TSt2KzFKczVMOFpNVUovRytNV0NkUXIrZDU5dm04UGIzc0xXVGJSamkzNjBOYUZLQjU3TWFwTmdxM0JJSlZNQmVObUVpVmJRU21xb21RcUVuQklWWWtrc0JGQ2lTaTBrUVJpd1JBeWJNVUN0aXlCWnhvcW9CM2w4ZGFvY01OcnB4OTU0MWYzK2VxOVJCU21WOUVIalU1UUVVWDBMdTIxTG1yU24zMUlEbUFSaWlnU0FOVzdzaGNZZ2lTZXBLQmd4UlpPK3QxcFJ3MUdneGUvZ1BGanFpMzVOcS9xUTRreUFMU25YUEJ5aU5yOVlwYzBFd0dBcDBnZ2xESW9ubEt3ekxKMTB4aUZwVkNvMVdPbFNmUEE2TmZsODJ2ZkQ0RUcvVzJNVEgvMXovZDFLMXJlYi8xUDd2MTllMVRiVy8yS0RxckRtM0luN0xLRS91dVlpM2dEdHRKZ3VBTVJHeGt4NHpSdUpxbHNxNmlZcWxSTWdFQWlCR0lwdEpHRVloQzVQWmVZeUVaZ0hUUlVrQitMSm5mTlRiL3lhQ3o1K21jWGZXWjljdGRkSzdxOG1VTXpwYSs3ajZjYXN5aEYxWXRlZEszc1VsZ0tES3djcUx2d3J0aWpCNVlPY0JGRjlLQkhzcDhoSXJRY3kxVi9mejhTWStlUXczRy9QbTcrQnRweDBSWXovdTZ3cGFFWlZWOThVaXppOEJjbFZBcmloQXFBSm9vZk5ZbUttUldYaW1zYTJUNk95a1FncXNrTFVhQzgzRDE2cFh6K3VYZER1alhvcjZjNDZhL3ozRzRGNnJlNVMvZTZqZzl0UDF0Q0NlM0lsdHp5UFpiaFhVZWZMZXVybXpBU2pxT01DazFFSlV6YUNpYWpzcFM1U2lVYlNHQkRoNHpGU21RTklyRmtoTmtBdHV4UCtsSWFyOHltOW12ZjNuTHFWLzlqcjQ4L3kyQmdCYnp1b2U0Nm80b0k5YUNIVnE1Y3FRWlcvaFgxMlNKVTE5SXV0WFRwVXU2bFhzN204OTM5M2FyLzhYNUJMNWhBT09YK1UvWmNWWDM2NG9yQ2UweExpNUtLSHluQVU0cGRNaFp6SnhKSGdZUmIwU3F1TzhkY21pS0ZzZUVTZ3NtSTBhQ004aWpIRDQ1L0E1OTg3ajEvQ3g2Yi91S2ZXOUdsc1d6QUZENjE5MVhtOE5ienFZb29HdDNpdlhXZjQrbnRSM1RMRTVQcnFHSXJVckpsakVlVG1MUVZLbk1WcGJBcUZZbW9ZcXRTdFNFaU1FVnN4WW9oRnJLVEhKRTBWTlNzU3NPdmR4bHRPZitYUzMvNWhDc1NRQmQ3aXRMYlczdndSU21xM3Y1ZXlnSVRIejRJZ3N1Zis5N2lqejcvYi9yY1hTODY5VGZoSXkyUERENklraDBISTRRWDVySHY3SU93dE9Hd2lXK3Uvc3BQUDdqMFUvVDVlWjk0MWlkdndtUXh6Z3A0eGFYRmVtTVhvYkFJaE9VdXh6Ly92Z3RlK3l2OC9oUGJjdWJvTVBDbFFKcXRzRUtOd1NCRktuM1VPcUZaaEp3cUFTQ3ROTXBqVmFtV0FrSmVSZFNrZmF3Y3ZVdysvZno3L3Rwdy9aY1plRVdYaDJVREp2K0pCVisyUjNWOFFKVjBHSTVzOFY2L3o2dnB3aVBQeEZNVDZ6REpaU3BGRlptMEpVellNaXEyaXJJTnFXSURWR3lFaWdTSUhJK01TSmhZSkNycml0OVFGWHRJYmtIdmJRZis5TktLVk54RFhsbmtyR0c3KzdwMWY3eXlDUVFXYnJsNjlPcUZ0eng5eDdIRFBIcmMwN0p1YnF2dVhMeEZiVU91cVJYaUFTd01jVHdHdERpZlVwR2dNamFLbVEyZGtLRHl3clRLdERXK0NYNTFSTnVoZDEzN3ltdFhCd2pTL2IwYjNlalBxa3VLVUZnS2hXVXdlZVJ3eW05Ty84VHZxMC8zYkMxWTVSdmZpckJpRUVFY3A1MDg2amhqRnVHNHFpRUNpQlpmS1V5TWw4bVVqVkRlaTlBb09keXk0N055K1F1WC9EVjVNdjBGWWN4REwwelRKUXMvR0w2cTQzOXNsU01lMjZHUG5iTS9YWEQ4V1ZoZjNrS2xxSUp4TzRsU1ZKVkpXNkd5VkZFeEFTbzJSTldHQ0RoQ2xTT3h6SWpFVWlDSVZGUFY3NWowbnpxbCtUVnYvL3crbHo2UXBUbnJETnZkTHlCd0V6WGh2TWZQMlgrdEhicG9zd3lmc04yTzdsNHBNRW9TUVZFZVVTV1V2TXFEU1N4WUpLRWlZalFrWUFFRHBFVHBpQ05SZVZLQUlFY0NOV0xRcFAwSFh0RTQveWVuK0NmKzdQMkwzdjg0ZzRFaVZQZWlic29hT3I0bUJrRysrUGhYWG5udHRyNXZQdWVQSGdUYkVHckF0ODVyQ2ZIaUlpaUFKV2JDeVVGckVMeVlNcTFNQkJERGdqeFpzdUxMeW9uMzRpdHJyOEFGOEhIVkZJM1kzOXpBTWEzV2RQWnVyekVuVDcrZEd6eEVZOE8wZit0YzlZR1Qza0ViSzRNUzJJQW1iQm1UVVFVbFU1VXFWNm5Nb1ZSc1NGVWJJTEFSSXJFSTJjS3c1WkRZUnFyaVQ2OTYxNjllZU9OSG1qcm5iMTd5NEJKLzFhR3JvanFrMjBPRVhuQWVPWnp6NkR0UFdCTTg4N0huektaWFRiVEFqd3doeHo0VXlDaHlmQ01KS1VtVldVaHF2dUxZS1VkVFE4UVZJa1JCU0lSQklrcllXdmpzV2VTVndCOEtnNW5ldEZ0T2JIdlZ0eTQ3NkJ1M2h3aUJJbFN4cDRpNjBMMENIcGJCaUVqdVZiODY2YksxN1VQbjc1aXdsb1NVMGk3V3VEb1dwZlV0c0Z0d3RYSUd3QXlLeWhIQUltZ2sxcU5zcUgvb2VIUFRsbnYvRWxxVFhnNEF3YWZBTTE2NzI1eXgwOW9mbGJtRjZYYThiR1pDNmY4NjhUeE1vSUlSTzA2QkRUQnBLbEl5VmFyWVVDb1NvbW9EcXRwUUFvNFFzcVZJTENKcnhHcFluYTk2eDRUN1gzZnp3VDk4VzRnUTNkS3QrNm5tSVYwcnVyeUJaUU1tandLNmYzdjJheDQwRC8zbnRuemxoSWs4UVJzZldzZ3FFbkltU2pJVWlobG9xdHU2S1BOOVV1OTNZTkg5d1NDUU1GaElSSWhKU0NMRkh1VU5tc3FNbWVPTnR4L3E3ZldGdm1OdnZqdEVtQlR6YTk0Y1gzdXJic0dSZDd6NnYrNHFQZm81YVpuT3ZpZ1NFcEs0UmlHMXlsYThVWk9RS01lR0N3a2JnUTBqZ29XbFpxM29oY293ZjJ2VEVmamR4TFA0aEtpWEF5THBaU0ptVmZqcS9uZUVlK2VYWXNRWXJ6THBmZkExYjVHbTFpWU1sa2VvakFCVkc2QmtxbkI3YlNoVkRxbHFJd25aVUNoeEhkZFlzY3JqY3JoTnQwMUc3eDQ5ZWZESzBnL0t1dGhkbE1RclJJUm9KV2tzZzducnVWdG1mVzdrNnE4L0xHdTdTd1dHQ3ZMc0VZbUFWVW9YWld4SlNUMGg2N3laRzY1VHRic25MbkNPNzd4SWtHYXh6dDFnV1VpSloxVERoTUVoTFh2KzVPekNHYjNuN24vdTZteWRPNjJnTFlkQ1AremlIeDk5em9hV2tlK05LTEk1eVpFRnA2RTY4VjVKVEJWSEZJQUFCc1F5WUVBd2JEQmRlZnFSNnUvdEI1NDhDbjNnbDBPRXFKZFl6OVdnZmx1NFpLK1BtSDBibDJMY0dwNGM4YzQ0cUV1YTJwdXdZWHdiSm14WnhzT1NqSWRsVEpnS0pxSUtKazFBWlJNZ01BRUNFMGxvRGNMSUNQdWFLMlpJbDdkc2V0ZW1FOVpkV2ZwNFdXRTViR0xjb2hRVkVRbVd3WnoybTlQZTlLNU5uMWg5bi85czl3UjUxZy96Um9PVkNPdVk3M2U1U0pLYmlNUjFmZmNJcEZhN2RkL0RXY3dwZU53YkVudHhMUHh3Mno1SjhsT0FrS2NnU2h2UGxITUZleCt0UGUzak83Nnk0czMzbnZQdmpaYzJPa0Y4WDdkT1hJSDZ5WGIzZGVjZWZlT3ZyenRKSGYydXVWYnIwQVNzUkl1d09BT3l3RlUxNCt0aGhnaEltRWs0TG1vU0JJbzg3TEJHRG1nNlRIOWt6eTloT1N5S0NUdjN0L0RnT0RRM3ZtM09rdkExblErb3BrWU9oNGU4UStidFFjdGZkUUtlSEZtUENBWVZFMGhaUXFyYUVCVU80VXA5VmdLT3lJaUJaWXRJckxCSHBteTIrNlcxejcwSGIrRnY0TUVsUHBhc01va0h4QWpaaWtqdTVGVnZ2T0xYOXNuektyNlBuTTFaZ1ZWRURCVlhkTnlUZ3RNNlU3ekRwa3h3a29YRWNWZ1NjWTR6ZUNMSmNaNGJmdytxOHd1M0tCUVNRWWlUZTRGRXlBWXd1a2xaSEJZcy9NV3ZadjdzVE5xZm5HSWpBd3FYWExuRVgzWGhINkt2L09HLy8rM0xPMzV3eFVZT2pTWmZjN3lEaUkyZnY5TnBDMFFjdGMxRTZZVmFFSXdGQ29pVUpZOStQSGFTdlc3ZDdTOTFQMVovZGdIMEZORXQwTUZCN1ZkeVowNmJ5aVExNTMxYXR2aFFQRGUrU2NadEdhTmhDYU9tVE9OUkdST21qTElKRUxCRHpLR05FRnFEVUF5cUlrWnlvVDk5TUNyaUxmeU5KUTh1OFhIb3FtaXFjUi9iOXNEc2ZlNDkrT2JmTmoxM1hsVThVN0NLaVl4V3hGQUF1V1hQRUxoSWw3Z3F4NzRLRVREVnZCWnhxWkFwRnQ3RmRxU0VmVUJjSlVaTkRjQVNGNVBCY0lpSFFTUmd4WUMyT2k5S1F2SHRpdnlha3cvZWZzenF5MWRkdlJnRXZ1REJDL3prNGEyNmNGV0VLdy94UDNESWYzN2pPUCtvaTV0SlBJYXJjNHExc1pVNTNRZWNJa0VJY080TlliZFhLQUlDS0c1U3NFY1V2b0hkMnFhaHJ5Z3Z4VUgvOUQrSVFZVDNvVDB1NHFVZFgwZkpHcDRjMTI4NDZDaWFPMnNXaGtvam1KQXFxaWFFcXdRRkVqS1RnVVhFRnNaYU1UQmtyQlZSc0tNeTdoMGU3ckx5L3E0N1Q2S1ZTNDBzWFdrVE5xcTRvdWoxTHVzMWYxajMyLzNlTjNqSm5mZXJkWE1iVEVzSTRSeDVzV1pLVWlHR2MxRVN4UElLbCtPS1pNQlUvVDRidjVmVTZTRVppQzJKek1QNUVVU0V1TjZyeFcyZWtrUjVFazdxU0dRanNsN3plRFRvYmRtK2JQaTg0VFVKb3A2S3NEdC9zUGpEUTgyalh5Ui9XZ2dybmx0cUdXOUZDcndFakFSSUVLd2s2WVRCZE0ranUwZXZrTTg4Lzk2cElPL2xHbGhCSUUwSHpKeForZmZPUjJWbVlib01UZUlWMDJlclpZY2NqaTJUT3hDSmtWRlRwc0RsdGhJZ29vZ1pSb3dZY1dxTFVDd1l6SkVPMVc3bHhzSHI5cmx4d2FIejVwWFRTaXFBYnVuVC9iVGNmbmIxVlh0Zlg3cDJ4Um8xTXJlQWxvZ285QVVrcElsVUxISk45dDBZZk5heW54VGx4R0k2a1RoV1N1WTl4eE5MRWhZemFEcE9vQ2pacWtXWWtvVWlrckJQc2JvMm9WaGlnNVAxVE9RWnY3azBzYjFuemtVZi9lQ0JINzVtYXVrdlNmMFdmbi8vYXpiT3hubEJ4UXNKa3BNVTFMc0ZoRVIrd2xMTDJ6bmRhWUFDc1o1Z29wdDNIR2x1M3ZRQXp2alRvVnI5Q2U4bEVLUjZabnVQekduc3hFUmdmVSt3LzU1N3lKYnFEcFFsd0lRdFU5bFVVTGFCVkd4SW9iVVNGK1NKbVozbzNBaXFJbEtZQ1BpODl0UFBPWHplTHVXK3ZqNmQyS05ZTEtwK1dtNi90dnFxUGI0OWZ2VmRUK2RHNXpaS295R0VmdUowd2l3czhYN3JBSXFUUFhQODdMTWU2NTYrTXk2bC91cWVHN2xpVCtJcnFUUTZkdEg0SzNIZFRKUUZaQkFSRi9SdGdzeGNEaTBpWUdVODN4SlBGaHBuWExIOWgxZGY5ZWczenZQUHpObXVGVjFlY2wwUExublFZRVdYZDkzcmZ2aWZiWVBoYWtLUUV4WWJJenVLdGIxSWJ4S2N2TysraGhBc0E1TkdiS2RXOXZDR3IwTGdvZTlQbzJuMVI0RlZkeiszZE8rK0QrM2U4QTRhdDFhcW9iZHc5bHhDd1pPeDZpVEtVUldsTUVERmhLaVlpQUpyRUhCRW9Rdk5pQ3pETU1OQUxLVGtMZVlGWC8vd0lSKytrL3RFTDErKzNLWmNjZzhnMjJXWDc0OTkvNjduOGlPNzVrMkRFVmdOUUxpV0RNU1JVd0RBTUpHQ3I3WDRSSUN5U0pCeUVsWlRFVmFOMUsvdGM2bmhrcEJMeWE3TXFkVnJEaE4vTE9JY2xpU05PMUx6YVJGWVl1V3haNThOSit6bk5seDF6WnR1T3ZrVkE4c0dUTGM0ZEUxRTBqMDBVNDVzMzMvNHJiTk92cWcxbEFpS0pZRU56ckFrdFY5TXFFdmFGUW1jU3NURHVESFlyK21WNnR6ZDNnd0NvMWhiU0MvTndJdWM5MVlPYS9pSTdmQUxFbGp4dGNadXMyZGp2RlNpaW9sUU1nSEtOa1RGR0lUR0lMUldIRHZGQ0szYmd4bXdJU3JlTHFPTmErOSs3YTBmNzFyUjVVbDNtdm1oZDFXdkJ2WHlnZ2YyL1orSG1yY3YwRkZ6S0dJOUVZb2ZlaHF0UkN3UWdsbHlvZWVYSm9OZ2ROdDZiM3h5REtxcUxiTVFVNXdTdVR5V2E5bFR6YkExVEpQMmZxYWdMSDBqb1pvUVMyNmRRU1ZkR1VnV0FnRWdSNHE0bjJVV2xWTU5zalpYNVp2RDM5eDh3azBuek9tbmZvdGlVUUZBLy9KKzI3V2l5L3ZmMTN6MTF3ZlN3c3MwSW85QU5pMCtwTENBYWptZVpCU3FPcFo2aG9Cb0VsNVMrQ1Rtb0JFOUEvYVBiYmZxUmIxM2VUL25UOTF0RDVuaG5ZbVNaVEdCbmoyOW5WZ0xScU1TbFcySWN1UzhOK1FJRVZ1SjJNQllDMk1OckRDTVpRUmlwREdNN0N0YkQzdzNFWlZtTHAxWnE3bjJRZE9oRkoxNDl4dS9zSEY2dEJ4bEhTbVluSlg0eFFDNVNFVVVrVmdRVFl0RUx4NmZlYzNyN1pJRDkxMnhaZTgzMjlmcy81clMvbGY1bFJJQ05oR01FcmF4dHpMWGVYUXNtcFdhaTdvY05Kdi94aDZOWkRWTXlaM1RrSjZDYzRsUnIxc01JZ3JFMm5nK2ZKSE9hZnNPNThNN1JLUUp2YjJ1U2NMVm55MzZ6dEFEcC8zODQ3TzJlODhMRzAxTVRnVm9rOCtUT3UxeHVqTFRPNEdIY2NPWTM3QVgzckQ3NlNESUg4dU5kemJ3MGk2SFp3NHZuQzh6ODQxU3RxSUZOSHZHZEl4V0s2aVlTQ3BSaUlxSkVCaURpQk5lbWNreXc0ckF1bDRnSXlyeUN0dlZENy9mOWEwN3NBSmVTa0ZLVVdFNTdObS9lRWZYL2RYVkh5SnVOQVRXdGRnVVU0Y0NRQlJIbXNYczJEaDRsTjMzakZYSDNILys5NC90ZjJwTkw0WFhMTHRtNDUxSC8rekNBNko1NTA4cmVMNFZNY1FwMW5MN0k2ZWJMVGttTUg1bWlBczVDY3pPT0F2cTZjUVlrRkY4WGJGQlJSSXZUdjV0SENJSXdxd2w5S05IY2x2MlAvekc0ejZUVnptaC91VXE1UVRSRHlJcTU4TDhlN3hxU0lMWXdMVThuRkp1TEcweko3Y25LM0tneXdEd1NOUStEUmNEME9qQmkzcXgyZ2xWTHgyd2JidTFUZU01K1hjZ0VFRm9WRWR6QzhoWG1BeXFWRFlSeWlaRXdBWUJHNFNXd2N4a1djUktUTlJZRVNPc1dpWnQ5TWFPcnM5RFFNV2x4WlNDQkhwRlJOb2VsRFY5RXdXZllFa0pReUhkRmtFc2JuTUtKYkNkbnFmUGJqejVQYmQwM1h6VFBvL3RuWE1oVDFDVW9ncjdRdjM0aVE5ZnUrdEk4L2NaWlorZ2JEYi9UVnlPWTA4RUkzRmNwS0ZZVU52OEdMVnduY0NiR0x3Slp3cXpjZUxNdGZEdFFub2N3Z0hTUWFqRDlhMDdMbjduM2U5N0U1YjMyKzZZN2VwZkRvdHU2TFhucjdwcmo4cTAzNE1EbjJJc1VmdVBVRGREZ0pNbkU5OGJrOGFFWWN6UExkRm43ZjQ2RUFUZE96dXMycG1TaEVSdm1IRzZkT1RtU05sWU1GTnpXeVBHd3FxVW8wQXFVVUNCaVJDWUNCRmJDRE9ZQlN4Q3pBNzFzY0N5aXRTZWR0WlByem54bXNmUUQ1WFFrSVRsU3BHU0UrOTQzWmVmYXRrK1UrdUNFYmFxTG53NjFnSmlZWEllKzdzUHQzN2x4dU52dW1tL3gvYkxQYkgvRXlIaTJuQXY5WEt4dXlnVFpoTGZidnZzUmJ1WDJ5Y2pDWWhZc1REVjJJdjRZUWtuOG5jQkhJS2pKTXJHZnkrY3dMQ1k2S2piQzBtb3RoSVNIbFRTeUo0eTJRSmhzRktrdkVFenlYY01EbHd1b3pKdHY4ZjNTME4xZDNjM2lNaDI3L082ejdTSkIySGpyc2xsQzQ3b1NDOGNOYVJ0UlVBaU1Dd0lSTGhCZ3cvSlgrQ3FIZDEvSmtUM0xHVUFNTHMzbkNWRUlnSEQ4elM4aGh4SzFZQUNObFMxUmdKckVGb0x5eXd1N3hXd3VGU0doV0VnMUZyaThMam13ejlqeEtBN0htMVRsS0lDOWZPN1YxeTg1d1BtdWJOZ0dpMVowYWhWV1p5VHVYWVJhNVJSTThieVQvejJoSUZpOWVNVjlmaWl4eU9aa2hYMFVpK2pyMXNmZXZCeG83TzQ3ZU5hUlpxaEpIbE9Ma1JMMmlwYVE4bngxM0ZOTmdtNWFjYUNldU1tS0o0VGI4K3V4MHhZcUZXSlJGaFlrVFR3QzQzanN4ZmVzTis3ZW50N2VXblBVdTJ5MEg1R0Vlb3pSMzN5WjN2ejNJZEFvU1pTWENQQWlYWXVLTVRLQVJXWHZneHBURmpHTkc4WkRwKzFSMHg2cUJjM2NCRUsxTXV0SjNZdXRPM3FDQzR4WUZnM051WmhJYWdhZzlBd0lzc1VzU1I3TGJFQXpKd3lheUprbFRhNm81Uy8rM05Mdi9RUStxQ1RBbmx2VDY4aVFQcGV1UGsvSnRwVW5oeEFWZ2tpZFE5WWlBVEVMTklxU2gxZDJMZElSQk5kUzd2VUgxTkpTbmNmVzdGMHcxNDNmWGZPUlB2MlNBSU5KK0pJRTE1SjRYQVNWak1HbC9yOU5nM2hvSGl4eFgvUHJ1MDBTVXRyRnhBVEpJbkRTVUtFRTBTc2lwRG5iVTJWRDE3NjZ5L09IOEFBRjR0RlJRU0o3NGtQYmRqN3YxdHRqa1E0WVdleWJGWU5GS1F1QUlFbXdBaFFGU016L0VaME5aMFJSK0UvWW1DNHZ5aS9xdTEwNmZBS0NJMkZadkthZlpSTUFMWk9lRzR0aTdDRmNNMDdZaGFZd0NJV2tJS0pzTFR6c0g0TFMxMmQ4ZXlxSWhSNlllZGZjL0Q4OFNZNlJ5SXRnTlhKSHVrZW9FQWNpV3hGV1MrL3pmemhoMTM5TnhlTFJUV3c3STlMVm9oSXVsWjI2UVVMT2tZN3BQRDVuQ1pZVWlhejEyYjlFY1FwTkU3L0VHUWdNbXA1RWsycFVpV05UMGw3aW5DS3NCUHF3d1VGa3Jpa1FJb1lITXhvbXIxaTB6MmZWTDNFYXhhdGNZaDYyWUNCZ0s0NDllczNObTduaHlIaUVSRm5PTmNhOUV2MmpaUmJqVmNvc3djUjBDenZEQUFLUFN2dGl4bVkwTFBTRmdIRnJkN3BNRzQvVWxCQ1dpRUlyVVNPbVlKbGdXVWsrMjZ5VjhYM1NDTEtlazJUdWMzWHZ1YXE2d0ZJYXBoRklBVWwzQnA5SUpyUjBFQVdGa0tPUnBiNlJXcUpiRTVyN0tGM3ZaT0k3TXFsSy85c1dYUGwwcFZXUktqdjhHLytaTzU0b1NKaDRDdHhtRnBTcjgxZ2xCcUljWStOTTR4WTZvbElXWllFMlZKYUpFNDZsQktNbSt6UHJwNUxLWUZCZ0lVMmdmQVRkdU5aUDMzcVp3djZsL2R6VVZ4dTNMV3lTeE9SWGRpNHk0ODBXd2lSZlJFMnVmWlpOZTBDSmI4TFpSRTArNGZrdXVic0RTSnhFeFN5Qmk2Q1FDUmZPYlZ6Z1dySkhjUVZCZ3c4N1d0aUFOWXlHWGJNbEJ0aUlqV013WEY5eGlGTXE3U2drNXJ2OHNnTDB4cXBLNExibnp4NTNZeklwL01Ra2dDc0V6S1IweENaTWs1YWpRV3lkMjdoU2dCWXVuVHBuMVV3a0xzeHZYZjdBYy9ONFk3dktWK0lYZHRaRW1ZRUF1SmFRa3N4aG5KdENhZ1AwWWlOenB6WmIyT3lyTFlnYXNBMllSYUpFN0pFMHM5d0ZRUE5PeG9xK2V1Zi84bUhOSlNzNlhkZW5OemJtUXRQL1VYN2hHY1JHVTNPY0lTVXBVelpzOXA3U1V5S29CQUpVMmZPNDFlMnZqWWJqVE1lN042bzdOMzZHcDdoNWNpSWdRaklVMkpZWUZ3cWxPYTRhUzRqVThweEF0TGxBQjNzMzJSaEtjVjAvZDJLUVBqNm85ZWRPRnlvTmdQYXhnOUpzblJTVE4yRG80QjJMOHlqYy9aNnkxWUE2RUhQUzlNbDlMaFU3TERjcS82bmVaeENacXVkY2lLV2FibmZTYWtST2FGK1NXcGdLN21mMkZ1WUVtbzRqdG9aek94b002b0JSSW01N3FRL0NiVmhJRW9vRk1pOVczOTNxdGxrRy91WDl6TUlpYTZMM25mMCt4NmJ6UzJQUW93R0U4ZTlzRWpwdVlTUDVReGRwd0JZNTJYc0M3aER2VFlMbG1zR2p0K2dEbG9LS01Bd0FHTHRhUmlUaEdXQnNGRGNQTzlXTnFmYlV5eEJNYm85eUpjdTNQL0N4d0JJWDNlZiswV1A5MHVlY2hqUEIrOE50WEpjUHVJMjZvVFJUY3UwSWlxWG8rR0o3ZXRmdS9mU0Raa3BLWC8yMVV1OURJSDZhdGVsYXp0dDA2UGtXWlU0YWcwTlV4cXEwNHBEb2hSd25pUElLa1NtRHJTcmNkR1o2cU5RamVTbXhOTXlQRE1nRmdyd2VVZXV1dXV4SzE3M1NnQ0NIN2tJMTFYczBvcVVuVk9ZK1hNTmlLaTQyTzIwMDdYNngxUWlneWhHdTZRUUN0Q2lEd2JhMnFIY29ra01URkNmNHZuelVlQUc3MUFFREdFb0VpalJCQnRiTVVISnFZNUlZaUZDZkNrZ1lpR21lWmo1M0huN3Z2UDV1QkhINVgyOTRLODlkTjBlNjNua1FJa0laRmtMUTRSSkllYWRKZjdjcEpCaVJiQnBmUHpsQzRGWFFoR1JtZVczOXlscndXNUp4cCtiUlZ5U1JjeFVXNm1vQzlmSWJCMHhySGJ2V2NxUUVTVHB2NnNuV1NpdEZqa1F5bUdUa3VlcUcwNGpFTkRaVHdBd2M5Rk1FUWdPbm5IWUxhM1ZQTUVZanpnekljR3hhS242STcyR1pQVkVRZ2lZcFVuTjhicGI5NGNBQ2VtaFVIUkxlc2ZDamdXVTl4WkltWVVZRkl2eGlhMkFZV08zVGZMdkdQR0tnQ2pOTlZpSmdNUGdiaU1HNkhPL1lIbE0wZDJ6NmM1RFJyMUtBNVRtbURDZ2VPSkNFaXBGWFBna0RvdzBGcHJuWC9HSEszY0ZrZlNnNXlXclA3dVhkZ3NBNmlqc3VUb3Z2Z3Vobk5sYnVZYldFOEVFR0xVNHkxTC9BQ1hESXJIVUN2TzFFSjNzdFpMdWtUVVlWQ2M3SUJiTkJsUmhQb21GL1VRVUVFYzYrc0xKbDZ5YnBwcEh3SlpjY1d0SzlLaXhCVW5ZZGdVSUt3UXJWdG9WOFd6dmNBQklKdSs2UVdRQTlDdmFYaUZOT1EwTEZ1djJYMlpJTGI5RkJtakkxUHlSckZnMG94RkwyZy9lWUdCUjdDd1NBQXgydWxHOXE3Yy9jb2prUFNBQjM3VjRXZWMyTEFEcG5Hd0pCK1h4SGIrZTlYSWR1SC9WODhxbm5Hd2VYWE54bGFONGNHVUd4Q0V0MmxBbUJTR2tjMWt5a1ZBeXFEVlJYaURtZ2pPV2pCTnF5bENNTGp5bktvMVlMZ0lRV051d1FBdlB2dm5DQXhLWkVoRUp1cUdJYUh0MXNuUXJQSjFpOGxvMWlSTHhHZFdxVFhIWjJnaGc0ekV5bmY3K0xtdVpLYzdBZHoydkFLRGFxQTVBZ2VJcXJCQVJGQXVUNVZxT0ljbjBvVnJWeFhVSXNBaUo3NVdHZHNoejJ4NzdGUUQwcm5SMDRzREtBUllSZ2tlTFF4dENKV3hNTFZxU3l5VnJIVGhpaGFtbGtYYm8wckVpUWl0WHJueEo2czlFNC9YbWxXL3BXZE84NVdRMnZpRnJkU3FOelJMUU1WbFJxNjNIYWtlWjRwa1o5alFycVU3RE1TU3pjRkJEYUpsTUZrblZXUVJna25HdnFvYUNvV01Bb1A5eEY2YXhYeGRCUUhrcnY4bEJ4ZVIzZHB2STVwSUpZc25vZkUxOC9UNE9CQUM4dVQ5dWY1M1Q3QzVobWplUFNRRHJBQUlyQ0R1YUtrNFY0dnBZbkJjaFRlbGppdFpXYVY3VFBEcCswVnRMTWFLTmtRK1lTTWtXak80bnlvTXdLNm1YVTZRUE5ya0hFbEZoYVBCQ2VmRFZQdmt5c0hUZ3o2b0h1L3U2OWFwRFYwVkgvZXdOUzM4MGNmZEhxcFN6QUtuYUZpQk9yRmNyRkV0ZGxhRUd0bXI2NUt5b092bDNMUFg4ZEZxTm9nejhwa3k5UHZQenJrbUprYy9od2NGVis5WmQvNktaQW9LY3ZOZnh6M2lseUFDaTA4UXN1OGdjb0tETWdxTTRXaWd4QW1sVzh6QjdkbWZDaUNuMERGakhFOUlCTUxGWVVlS1F4Wm1iVHpqVzVNWXorNXJMMmdqNVNMWjk5SkIzVFVKQVBlaEppZlV0VzU2WmFUa3NrS1VhRTV3ZzJXeFlTeDRIaTRMeGVEUVhIbnpKd0NXSGdDQUpNZkNpNlpFVVZmL3lmbnZMSTdjczNvTE50NGI1cGp4Rm1zQ2lhdUFuU1pkU0RwcmlYWTR5M09TTDNtUHQvVVRCa2ZIWXBCQ1FSZ25VcXdtbTNDTUpOSWNHYzZmdGZwaUkrT2lGaFlENnUvc1pBQzcveGRWL2lFcVJnU2p0SG9haU9vbUp4Q0ZhNm1UOFJBWkVrVER5L2l6TVV2TVNma05CT1pyV0ZxZ2RGaUIyUFJSdVpYQXR4SEMycXBFcFp3R3dJcXp5UHA0WmYrSXhJdHFhSU9oKzlDc0F1SDM3YnhkTm16NTNyb1FoRTlLeVlGcFB6YWdKa1ZGUDJISXo4cmNQM2ZkT0YvSmZQRXduaStqNlZaZk52L2lwLy9yQldyMmprVml6aUZWMUQ5c2lVeDlFVWtDdkFhZUVGUktxMWYwU3c5VHkwRVM2VjQ5aVUrRVh4YU9VMHRTdlJtVEhhRnJJRXBTSGplVnR1L1o4NXpzNkk5eDA5M1BaZUg3ZjFvVUNFMEVsTklWa3J5VldFMHBHWDBTQVJBd3dzelFBK2tCL0dnQmdEVWhCZ0puN2RUYkJWMDBPMHhHNTVGNUVrbVNiTXkwQ1dScXJWb3dtWVl0Wnpidm1SSVFTWG1KNXZ6c1U0Vk4vK0VLMHViUlZRRG13bGV6cVRvUlFOYnJJaFRpQlFCdWo1YUhLczZjZS83MnptN0Jzd05hcDZ6SU0xcGZ6WCtidnZuRDdMZXR5WS90Qk5ScGhxM2RTUkVEcWUxWWtveTNJN25Nc05lRzB5TTRvT2FrK0pJUkRncklUYitLa3Z5M09oZXNGdk80UFQyTTAyRkh0UGZkY204STZJa0VmTklCdGE3YytjUjhLT2JqcVpmelp5U2hHd3BSVXJQNDVVZ0d3TGJKWHNxKzdKYklRc3dscWhoaEpTMnVVSVdIcU9GQ2hSRzJlNW44RWlGakNMSCttZUtRek4rVU0vTUxZR2dvcG9yUUZJUVVnVXBPTUloT3FYY2hUTUdUTnRNSnVtL1N6SDlLa3BidS9XNzFZZUM1SFplaDg0UWROcWhrd0ZxbjRncVYyclpnaXhFbzlNMGJVbkpKUG1kNEVtbEtxb3lrY01XVWIyQ2h0b1VqWnJMcjdvbFJmSFFSMjNxeUZzMjkvL1BaWEFZSSs2Vk9aQld2TGhBcVE5cEJUU25oa2xSNzFXN3g3enlvMzFKenRYQURBbGtsbjRNbWNibWN0QldFUmltdGtrckF6TEk0T3N3a2lTc1hDYVI2UnlGVWFWRjVlVFB0bFlKQ2hoVEloaTJyU1VFWkdGNXdNZldWdG1lMTRJZmo0ald0dTNEMUwwcWZzRlhyRmZ0S3FYNTdVOTRXOXkzUHVrTERza1NYaktMeU1GeWI5SXB5aHMxTGFEelV2VFQyV1hBR2VNK1JDZ2trNHU0Q0U2bnBrT0VtVGt2dktTa3ZpZElwRkFtMzhYMnk5djkyNVFYL2Q4NXJiUGw4Um01b21PdnQ4V0xJZVc4TUdKdVlvR0VDenpybTBJcVlxUlNtZjNmYmg0S0NMQmpYZU15MllaRUowWmdWUkREbzhvaGNuSkV6TW1hYjcyRTRBcG1iWWhCbHlmeERCbDQxcXpPOVovYVV2NXNpWDNwNWV0ZE9NSHhSQlJQS0JYVDUyemg3aHpFbkFlc29xVGdtQjFFaFVZNlVTOGlLVE1xVmJRTjBDUkcyUGxoaXgxdjA5MVdTYmFlMkphbUVqQXlCcjZpK05TVlBHSFVPL05wbEFCL1E3OTlpbDBCbHJodXYyMmpSUFRhTmZiWDhuTUlnb3hreU52bGZQUlRkNThZVGtoRDlOMnJDbTdGRjEyck1NU2t3Yzg0L1VmQXBlczZQR2hHcjFPcUg2TGwyMzBxYWdVNEt3ZUVEQnJQVzJuM0hPVDg1OUUzcGhFbTFUNnNXOXZkemQxNjNmZnRTSmcvUFVyRk5rZUhTQ2labFlaUXNEdFJqTnRTSjlKcjNKL0Y0MU5VUlRwc0pDVTBpUUdsS3V6em9rMC85TE1UcVhKSG14REl4WFJpaHIzK1RWa1d0Mmt3RHFpSTZNb2lOYk9xdzFXNlhnaXpSY2Y5VG1abmNuUmtDaVhMMHI2ZGR3SWppdWIvREpwaFBKU3Nyd3g4UGhSRDJ1aUtVNnU4MCtVSEtTQnd3VGJJSldYVDlYWFZtTWR5cVBKWW04R3Zjcy8yejhucTkvNWcvWGRxS3VYU3hlL012NzdURXJqdkh1Vy83emdSTms4ZnM3V0hsaWc0Z3N3V21LMkcwelpzcnYyaW1VQTdEc1VMZGppTngvU1lpMFZBdlJrb1RPbUpGekRWSVVSd2ZLQ09hYzhZM0U5MHV3SnNESTlvMTFXQ1Y1MlZvRFRiYmRrZHlXSWRrRjViWUlScjNTUCtRcEJmOG9zenNtSkFDbnpUMjFXSi9zTFZub0g5OEFrY0tXMGxiSlRwdEx0RmduZEx6V2I2TTJRSXhrY2wrcWl3QlNCNFJxK05CZHZDTE95MkJ6TlBjblQxeDN3NDNMYjdSWVRqc0JybnVXM1dPV1hMbkV2KzJjTzc3NzVselg1UzJDbkxBSnlSbUY2dmRhMU82UEtXNGl6S1JBU1I2WEZNQ1pwalFhWjdZYnFVMnZqZW5KS2FNRXBtSlZnVllhMHhwYjZ4d2hlWldqTW1VaFMrMTZzazFZZFJ4MWpXQ1pvdE54WHhwcjQvWWw5OUN0MUxxN3VDNmR5UENnVS9kT3duaGx1eHMxRWFkSiszVjNDd0Q4MjZMdW9WeklwUmdSU3QwQWdnVFJwcU9GTXFndy9YMUtSRVNUTFpoVjl2bmpqcnYralI4djNGeW82LzFKUHVyQkN4NDBSRVRmZXQyM0x6cW44WlJyWm5nNkJ4RW1wbVJPUTYxS3cybnhuR3BhS01ydTF5d0lTVnk3dnlHcGF3YkxvblBhT2QzS2ZGOFhsUUF3VTA1eVdEajlBTm5adk1EZzVMQUxwRmxWSjJjUnRXUUpsS1NTSlpUOFhqdkZ3QTBpSTZyQ0FZbFNtUWRlWTk5bHltckpoamdXS0NnbHBUSU83ano0TUdIWkhRUVdFZFZMcmhhNzM1ejlIdHU4ZmZObUtGK0pqWVZsTmcxL01YQWhxaXR3OHhRUTRXN0s0NFpHODFqaHVVdGY5NTAzSER1d2JNQmNjR1d0SHpjZFRDUWlWYW1xNzUxeTdmbUhUT3orVVJrZTFpTE1NTXJDaUxqd213bkxOdlpRRTNPNlZvQklPTWRHelMrMVRyUldvUkNWUFRFc3NNckFndU1IS1RBRVdCSllTc2NCd01TSTJVcFdmU0dwbE1jWTZjaTE0S3c5VDg0bEV0ckVrUVhBYzROclROcW5ZRkVqVFRpSnJFUjEyeGtuNFZlNW9HN2kxdFc1Y1pxRWRkaE9rWXhCdzUwOElpRFltTWpqdXZycHppd0s0aEliK2RnYWpoWjZWbjdaQTRDZW5wNDZ0bW4vOW4wbkVWWkF5YkZrcWJlUzdIUklYUDJnc0V4MVd3RDIxQmF1bU51Q0I2NDc5eGNmM2UrcUM2K0tZb0tnWHI0RDhPUWJKL1VkWjkzMitiMXB3VG16YkVFakNqUkVtVFRWa0F3NDRiUW1JQkJFQldYVW9iTHc4K3RPZjJoUmQ5T3hKNzBhKzk0Mm13c0txSHB3SXYzNEVLNVVIUkF2MEpRV3pVaWxNOXVCQUZCYWxTZkdKdmYyZG4wV0FCN0g0OWtSaWpvSHYxRTRrNmVuSXdHUVJmbzFUQlJISDFJeGNneXRNL0NxMklOMy9mM21DYkV5Q1hjc0hBdUxHd0FpR2RWSm9oUEs4cUNDcEcwZUlFKzJWTFpKLzRQWDV1TDhOSjJycFVqSjJOajJGVnJsNEE0ZFMrZGpVTzNQRFA4TDdFeE8xTUNlVWloUXVUVS81NEZ0ZDk4NU5MWnhIenhlbEQ3cDI2azNoMjRrZThHREYvalBuZi9nOVJmT2Zldlp1MFJObXhVaUg0b3NwWTM3VkNNUDNBTzFubWY5R1NQNnE3L3R2dk9qMUVnYnJuM0RGYmM5dUh6Z3BMZk5QT09vMlVQNnN1YXhNQ0J0UEhlR0J4bXlzUmNsKzNTNnhhUnBGc1ZJMnFVb1dxdmhpWkhSWlFlZHNDWldvamplM3ZVVHo5cHYxNE9PUnFrSzVTSXFaZVN5R2Z5VFpnR1p2eUVYbmNZa3JJVm9CYXdCUWkvaVNGRTh1NXlRcXMvclFtVWRFWUNhVXNHMTFwbEM2elNhTjJlZlpRRFF0VFFXZmkxeStVVm52dk0rbEExZ1JMa0N0VnNjRGtrbm9USkJ0c2w3UUliS1ROMkJPVkpnM3p6aERjNDkvZWZ2dkYxNmV2UnlXbTZua2lBaWdxc092U3F5UmV0OTZ1aVBmUDgvNTE5MDlLNDdjamZsd3NBVEUxRWFzbzNESGNSa3BEcWg5NW5zZkhURCtVOWR3bjJzKzZSUGQvZDE2d29xNmd2SGZ1TCt3ZmMrKzc2anZVV0g3RFhTZW5YemFCQ0NJMDlFQ1VTYlJKaVdncmcwZFlxSkl1c08rVUJRd2FIVEQ0YUk1RkEvZ1FBTHYzU2tlWExrT1VEbmtQUkVwNTVyMCtwU0p2Mkx0d1NkYkxnQzVha3RBSUE1emFLUURBS0plRFJoNmh6dEpiS1Q5OVJWU0ZKVWw1U1BxV0lycUVUbFY0c0lEYXdjY0JBaGJoYzljTzZDMVMxVkx3Q0xRZ3A0NmlvdzlUa2tFajFTZkZHU0xZQVRSRmdMTmR0N0ttdm03M25Gb3B0RlpFNHY5WEozMzg2ZWpGNllyaFZkM3Z1N0xsaTc1YjNQbm5HMExIcjNQRk1ZQXBlOG1LdTBTclNCTldvaHphWjN6SC9ibTRtb1ZPd3V5bkphYm1QaFBuZjNkV3Z1WTMzYmVUOWQ4K3o1RDExd2ZPT1JoKzlWYXIxK2VwV1pFSGxwYUU2OUxoMEpVNk1LUkN6bGMzaDI0Nk1yQUFRb3dnT1JvTWY5dXk3c082TnFBd0lST3ljVHFRT0RLZTJaY1RRR1NCT0l5S01xZ3lieGxDczJESWhDek8rU3BUVWdncWc0c3pPUzJRUFNlbWthYXRJS1VLcE1aRURsc2E2MDVSVStlWkljc2h3MzFkTjNUdnZPV2xVMnY0Y25sSUVPMlNZcnl1ekxjUzZaZ3F0TXpTOWVIRVNBV0swSzA4ekdUajVseWJXditxNU15cHorNWN2dFZDSWtFWmtYaTBVVmNrZ3J6N3IxeW85Ty83ZkZSNWg5cm1zcnk3aG5RNDlSOGFRMHFmYnk5enpubzhkY3RLYTdyMXZYVGJLTGMyMDN4cWlvdUkvMWo5OTIvY05yTDN6a25Bdm5udnFxdlVhYWZwd2JtMFNhYjlzTTMxNmplQWxRVUlISW91a0hiQ1lpU2NZYWR5MTFEWFZEcmFVakM5UGFDckJzMGhFT2VCRW5RMVlQSnVSVWRBQW1CZmIzNWVGYWlJNVB2NWFSYUJ0eHpKVW9na3ZLdWQ2ek9KYnpjeXc2NDB3ZkNJdUNVYkxWak0xLzlSZVBtUS9VYWgvbzZkSkdERHFsOVJZVnNic1Ftd2pYMHJ3NlUyYUxDWVM0UHpnRktRbjNhK0hVOThSZ01WNFFVUFJ3WWVqNHhUY2RjMmZ4d2UvUDZGL2ViMUhFVGwzdjhVQlQ0VDdSRjUxeTBkWS92R1BnYmE5clAzcnhFbDd3dVQxR3ZKVVh6bnZqdDI4Nzg0ZlhtejZyKy8vVWNKUGVYazRNYmJxTi91enJ2dmpncys5LzVFMUwxRjV2YmE4aWhCVUxTMGovUzdZWkt3QmJsYmRFN1g3dXprUndCd0FEVi9RS2dmREkxb2Yzbkl6S2dDVkt1WENiQWx0SmVXbGJSM1RFT1E4UmhSakVHRzJOK1JOUldPbXVXVmZ4QkZYWlZWWTA0RTYreURUd3BPUkVRZ1p3aG54UEdSMkR0cWEyZFhyd2VHZllCTjA2V2U0aDB3LzllZE1FQ0JZcUhTdVk4THpaR1ZVY2w4WnI5UXhLUS9hVWZxQjRaZnVXODlHamF2T2lyOTcveVR0UC9kOVRaNkVYNXNXTTdPcVlyc2dlSXFRYnp2alcrZ2ZlZnRmSEJ0KzNlZGszWDNmNXU0cFNWSFFtdmJSNWtMMjlqSDdZUHVuVFhHUjEvNy85NnFhREdoZE5JZ2c4UlNyVGdSNDNzaE14bUZWcmxOOXg5Wm5mZkRRanVBUDJnN0N3aW56ZEJlM0hDNTlxL1VncDhVVDEvVW8yam1jZWtjb3Bra216RllPRDIyTG5Zb1dsYnEvRSt0SWpxRVFzU2lsNE1XSU9wM0xSTWlYUlQ1RmNPbzdHZWtvaWtlNmNLMmh3K2lDS1VEZTg0OHBuVk1uY0FZbElKRG12S2tXd21WSVkxWGQzMWVaWTFMUk9VMHQzQW8rNFlFZGI1S0RWdVhVUHYrT205eDVQdldTNmlsMWVzZmdpYXBDRW1paENvUWl2eENVaUl1NmxYaFo1ZWFPWmwyTTVveGY4MFJzKzJ2elEwQm9GcndBeDhmNW02MnJQVEVxb1F4cnVuZGN5YnhCRnVJYTZZbEdoRjN6Sno3ODhwd29zaG1HUVFOVXRjQ0hLakZqS0NCbmlPVnFhTElSQUlhMUptZzFjaU81MXR1NTRidWdwVk0wTGxJT0NSdzdXUlp5bDkycUlNUEUyVGpxZTB5RVluaGlXU3FNNjd0OXYvdmdpOUtMV2c0TXVSVVQyb01hRjF4V01xeUxVVjVReUt6S0xvRE15M2JTMnkybnpicTJBbzVoRVdRMlZ0eHVhd2xuOVE3LzgrY25mZWYybjcrMjl6L1M2QmZiaTN0d0xqbyt6ZVVtRHhWNzAxZE9sQWRCdDYrNDh0SlFQV3FGaGhFUWxLcEpFRkNCV0tHY0lpMmZ2ZHhPRHFaWnByQ0VBZFB0dmIxZzBvU2NiQVdVbGFYQm0xTVkzSkVkOVpVY1FoRUx3SWVMRkhaVGJnMGRjczhGZ0tud1hNTlRHamFoZzNENEdyZHlFTEkvWXBRK011dHBqWXVTZHluMUo2UFpraHhlb201Lzh5VnVTbGxFQVdObXowa0pBdDV4MjdTODZKL0tiWUkxSFFsempmNmxlSWxydm5mUWlWWlVYVVdjUUlOQmduMHNORGJuYnE2c3ZXZnkxdzI5WnMyYk5IUFRDb0J2NlJiMTVKOW5GeTMwTmdBRFpLR1BuMkFaZjE3WGtTREtIaXdSYWRPdWs5OExuRHYzV2pXbDNJUUQwOTBOQnlaZ0t6dURHZkt5b3pBb1ZNczhnelg5ak90VkNLSzlCU2lrMWJxQzJtb2NUQkoxcFhYRXJpVWFyOTRFQmFHTEtLWUlSU3RkMmx1RGdESjAraFk4bUZnWHh4RGJ6dTBYR3BxTVhWdHhCTTlMVjA2WGJkOXR0K09ET2c3N2FvRFhGelM5VXI4bWlLWjhwc3BPOEpoMDZTVk5Ja0pqd2N0NGpKdDlpSDJrY2ZQMGI3bjdMd3hmZGNuRjNvVDl2ZTN0N3VjdU5IZnJiSENua3dxdDUwMVh2V2xUSjg1dkZhb0VScis2NkxBZ1E5aUtEbzJZZDl2TTk5L0NxNk11b1UvcGhQL0h0VHhTR3BQUmFGZ1Z3YkpkNlRWa2lHYXF4WWtuTklFZUFJbzFSTTZudkdINGtBVmcxQThmV3ptM2orM1U1QkJSNWtvc0ZYaEZMUmpGWVU3eW5CRVRXMWR5WnZWRGFidklyMDkvNHJRdmVDMEFvN21wZjJiUFNvaHY2bHJPL2RFVmh5SzRDV3gvc1NGRms5Y2lZSW5qTEdycFdkNVZhYmJrdVlYY3Rmb29CR01WZTNqelRNTkg1bzIyMzlSM3lqU08vYzg5ekQrdzk0TWJqeTR1bFV5L3JSUUN3VW9tSWZxNzA5RFdsSnZFSllNY1cxZlJjSkNKaVFwcFphclR2MnZ2MS8yTmhxZGk5bjhSOVNSNEFlcnI2M09uVlZqVWZBbE5UZzliYUJCeTNqVnBEbWdDSUdOQWdhREJ5QUpYNThVcWxzamwyR3E0WnVOOE5ZRzI1WmNzcUdiYnI0UkhCSjRaSExzYWJURnRBbllRbHNXbVN5cmkwZ0JncXRKQS9qRDcxbnJVamE5dlJNNUI2c1p0Tk1hZDBZSDd2U3h2S1lnSGgydVRRektZYXorVE15RVFwRS9Kb3A1cHhDdW5xeFlCZzlvZ0tNa1F3OStzTmIzL0xyZWM5dU9lWEY3OTd4UW9wcEtsUXNjdVRLZlhsbC9MYTgzOVB6S04zd0x6cE8yZSs3Mmx2eXhGQTNnaUx4czQ5YXd3YjZkMGI1bHoyK3NPWFA0L3Uyc3lTQVF5d2dwSjdONjN1RG5MS2xTeXBqdStuVE81TFNGUXpMSUlJUUY0Qm5ndHBhano2TlFCRzdGRFp5cUdncDB0dkFjcFNDdTRtbndTYUdBM0sxWXBEVlN0Nk8xWUp0Y0Zkb0xxQlhZNnVVWUJudGpTVlo3MzdSKzg3bndoQ1BSUlBtT2xuaU5DS2kyLzlhVEE1dmhsSysvVWo2R0pkZElhYW5LSk5mcEVkTTB2dTB4VEZIRUdFaVVoNTBINjBxYkhhc3I2NS9JM1RIOWh6OVdzdVgzcWVpR2owRHBpNFFLSFEzYTMvbkxHTFVsUW93bnYyNHR1Q0MyLzQ0UEYzRFAvKzAyV0ZSTXNjNTZveERXdkZDbGsxbzlTd3JjTi8xY2U0eUFwOWJpa1dYWGlYLzdqK1B3NllhQWhPZ2lVTGhsZkxMQ2dUMVJMQlFaeENHaUZFTE5TZ0JKNVNhdFFDajFkL21ZM0k5YVhoK09Bb3ZUWDRxUTZFbEFKUlEzeWZvYTJoYU00V3VnbVp0aENxQ2N3YzJJbklrOThPcnZuM1k3N3lobmIwdU55ektFVUNrYnp6MnZjdGJpcDBUSWMxSENmdW1SQWNUME5neW5ZVTFJaVFMTmViZW5xR0RNaldhcE1VV3dsQTRoUDVFaW5QREhlcWZYNmpOMXd6K3d0Ny8vN0FMeDU2N29xbmY3dExEbmxHZjc5TmpWMkVoMktYMTdYQy9SY2pjZXFsWHM3MzVzMFpQenl2KytZdHYvaEp1ZGx2SXUwcEtNNkNRZVY0QkpKQ0pMUnM5cEVmdS9YQ1Q1VzcxOVFhVU5jc1drT2FQUG5GQy9kK2JMeEJja1FxcWVCSlRXRWltWW5HbkpRNENVRjhuRXVlUk9WSVlTVFlIUHhxODI5QmNVUitNVjBvQUdtYmovYnl1L2Q5d25ibVo2TnFtSGNZaFJDQ05rM0lVVTNLcWFUR1cxTlNRcGRVczBWQ0loelNIcWJkZk90MVYrK3hiTzhETjRvSVVUOHBMSWRkOXFVVDMzbXY5OHkxcGlFWGdjUjNoVW9BYWtwVEYyWGFRTktESDVPLzU5b2RwUG0vU0QwWlFFRDJsTkJrTDB2Njhqbnk4c3lZRVRhTXphTlp2K3J3V3IvMnk3Zi8rS2xDYzM1cmlLaHU1MUJRYU1vMzRvSWJManJzNTJ0dmYrOVFZL0QyWVZNRitYa1dFWlZPUVNVQ0xCT0JqSERGT3lqWS9ZNDFINzMvaFBEMHFEWVp0bGhVNk8zbHMvL25QWHYrZFBMT3g4WmJsRWVXbEZQYlp0aTdKSmRHRXVYSVNZckdMTkNrb1dibkltcjFmUDc5eERYeXRlZlBUNDQ4U3E3WnF3dDRmZDE2YkhuL2FHNUhlQ3ZOSzV6SElTd0tpbEJtUW9VQlh5RTk4Q0p0TVU2bXBxTTIvbGdFd21KSmtlY2IrdDNTdlJadmpaTjZSdEg5VkZDUUxxczBNazFVdGJuSnlUNlRsZkVrdjVmU1FjQTFURUJaZVF0bC9qb3JlRThXVEJ5M0hKQlJVQjRIU25pVEg3VnRNdXZlMUJ6cU4rMXk5ZUx0UzY1ZDl0UWpheC84V1d0TGh6bGs5aUZxUzdpRGh5ZTN0TFRsT3QvMDNiVzNIRERjRklEWnM2VHlTcEo4bFdxS0RRVmlRZWd0VW5NbkxqdjVNKzg1dW55MEt2WVVwWmZpTXVxYVhoSVIvYW92SG5mWlJFT1VKMmt3Ym9STjdFUjF3dnFNamF3QVFkeE4yT3dKUEZJWXQxQlBsSDVnQWVDS2dicFVyejc1anpzUmNzK1dyNnZPYjNpbitLVFFvQWlLZ1FvRERTVElUK200cTFkODFJbk5KSXJRcWRvZUlDS0Q3cGkyWEFNUmtmd2VuMTI4bTdSYmtQWEl0VmxsRFpSOGNHeFZRbzJ6VHMvcmpXdHNPdzBBa05yV1FmWEpkR2JRZjN5QVlOcVpyd2trNHVmdHBNYzB5WlVabSt5ek03QjcrMUVWUy9oVjlURllzUWdiRGNBYmdVWXRvSndGc1NlVWRFTWduVXBPUk16YU1vWW15ek5iOXovbDZBT09mcTRveFJSWUpaUHRQL1M5ZnovaGFiWGxCRkcrSWNPNnh0YVJaRlF6OGFRMm9uUi9yd3FRVTBJTm1xbmdhWHAyOGtsejU3Yjc0amhsLy9nZ3RINVlGS0VtZjdMMVhyVTFlRkRsdFlhR1JWTXNaeWxQV1ZWVGRjZUpjUzBFaHRHaUd0SFcyUGg3QU9oNmoydVBSRC9zMXEzd3lHOVlna2lqTGlYSVZwanF0TCtnakNZWTZUa3JPL2NIMVhSbHlLWlpkZDM0R1pxVmFrTytBWUlWRDVZVXNjL0VPUXZyRzJ1OHFCS3lDUTBpR0Q4aTYxdXlibktPRysrYlZ0WElUVWtqQ0Z2YlZpVnYzOUdaYjF2eGdaL2UwMVhzOHJLVnFmNjdubGNpUXRjK2N0TW5oaEVJV0tNMk9Da2pyQk5rVzFIZCt4RUlvUkJOOHdqYVFRdnZoZWk3QU1LWVVmc3p3MGpqQUtZMmxTOG5FNWVYbWpTZ1NWQVZRbFdvZmdqWWxHNjdKRlZSckhRcENzN2FwL3ZwWkpwTTBucHkxYjFmWEx5TlJ6d29iVVV5QXJ0c3R3Qm41aDBseFl5NGNsS2JGNVg1bmJaZXZsSS9JQ3N6blN3emJiWTJqaWk3ZUlpRVJJbUlob2dIeFQ2VWVIQUg1SGxDckVVbHJTcVNIYU1IUi9QWXNJM2huejN6aENzMlhMSHV4MTByaXQ1QTlsaTZMbmk0YWxXMDdJc24vczlFcDM4VWlXOWh4YXRicERZRmxyVVNxaFdDRVVIWkNud1JOSHNpT2FWbE1CaW4reWV1QXdIb0hlQS9iK0M0bmJIbE81dHZ3cWJxUnVTMVFrNHhtcFFqdGtzc2RUb3R5SlQ1RmZHcHVSeXBhYlo1OEszTDN2cDQwbUl5R1BPalAzMys1ajNRNERXNEVYbFNrNXR5UnZ0Vk03WkR6RFk1S1VNb1JaVXBpS3ByaFprNlp6bGJYcGlTYis4a2FLaTFIeExYY3RGNEZuZmMvSjRsZTlJV0ZRSll5TXAwMXZrM2R4eC96YlZ2di9xOXBXQlNEU3p0c1JrZHNWYjNhUE9HLzExKzh2MlRqNzZQL1p4TjgrYVVZOCtvUGFrbTFvdUpEVUxBd1BRY0VZbFZUWnF3TWVpclBETzhLUjdvd2kvRmd3VTlYWG9JbUpRTjFTOUR1L01mMGE0ZGNvNEFsR3g5YThjVVZiOERXQjdZaEwrcnFSWWdBMnNHaEVDWTNqVHZtSElVQUtRU1hWZm1JZE9VejFWSk00Rms4bDFLbThTa0RrUmxWU0QxVzRsa21zaXkvUUwxWVQwWk1DYjFwSW5VOWtQVWpzTEppQlNzYUtWeU95cjZ4TUpoWDdqK3ZPK2NYN1ZWRlIvYms1NG9nMzdZYTcvMXBkMGZxejc3NDdBbHI4Z2lLU2hJWFRPNU94QWlacTVpZzFzSUtnSTBhRkNiTDhpUm9nM1ZTTjB6K2hVSUNJLzN2eWlQL3VMRWU2OXIxZVFmbDY3VkwxVFdvb0UwUEdJMGFVSVU3OFVtQ2VicGZ4bmtxMGdxSVJwQ2ZUY1JDZGJFajdFYllHRjZlTnVqZThETjY2QlV1cE1vOUsyQWpNQk53b09CV0pZb0lIY1VOMXV5OGNSVkt6SmwvNjhWUEhqcWFMdFVFVXFaNXJOYVIyQk5YMWEvNzlXRThVbVRITlZFOG9qTGFSSkJRcTk5S054eXlzeFhuZlg5QzY3N1NPbTBzbmFRS3liTmkxRDl5L3Z0bkF1V05MN3o0VTlkOTV3YXlRRTVGaFpWYThCTHhBMHhPMmhJNnRwVUF5YUVESnJoQXhDREpxM2t1ZklQd3RYRGE5RGYvVWVQdTFOL3RLclNENFhoNFhIdm9mRkxWZEt6Mis2NTAyeU5BT04yU3FjYjBzZ0dObWpuSmh3NFkvR3ppZTQzVVEydTJ2elU5SVo4MDJFd3BqYmF0VFpLSVE3R1ZrU3Mxc3hlUzBCNnQwckxpSDVoNUJtZmpTZHNDYUpzV2dTUmpOQTdvVENSVllka0I2VmtLRUNiMlJKcUFqbGtGSitaY21YTVRDVmRIT3lFOEJCUnVTanc5eTEzUEhqMnZKT1B1dms5L1Rjc3VYS0pULzNPTjdOczFiZFhyQ2lVT29adnhTNHpqaVlxR0ZqUmRXYzFKR0k5SzVScHE2RzRoVVpRWnFEWkUycnhoSEpLcWExQlJmK3U5T2svNWIxL3lzQ3g2cUdvZ2g5dnU1NmVMRDJFWms5RGc5R3VZNmpPd0tRa0J4cGxEbGVBUUpTeWs5V3g1WXZPV3Ayb0ZoTFY0UDljKzdGcFc0TGhKbERlUFdBbit6TWlUSURSeWtUZTlFbFB6UnNxckQ0azJ2Vkw3OXEzKzZUMUgzbjRBUFBlYlFlZDBIekVONlpOY0ZWSjZDU3JVSmJxR3I0a0t5bWxlaVBWVFlmTjlqclh3RmZkUkJ2c1BLYkJFU1Fzd2dJYmVNMDdnb2xqdkFNKzk4VEhWcjN5c2dzdVc0dnVicjNxd2xXMWtjZDkwTDI5dlZ4OHJNLy96MTlkK05QSkRsb3FUS0ZZcTNjZUUwSDFiU28xdWhPb3N1c3RucFVqQ0RFMWFrMVBCWmVGcTdjLzg2ZThGMysyWk5ZTmpSdGhDNithOGVyd2pKbjNjTE8yRkVESnBvQVF4ajg5VFFONVNudUhTWk1WQ2ZTOGtmeHZObjc2OGFQaUNVWG9XbEhVQTh0NnpadStmZlk3N2hqL3c5V1RIQmhZS1VBVFdzUkhRMGxYMnJ5bWV4cVZ2dmxOQzQ1NytMTnYrZThIQWhQdWRMbG5YWEh1UG5kdHZmdkRFejZmVzU2bU5NUURsR2VJb09JRE5adzJWYkw4M0pTdk00Mk5hWjVKbVNiclRPOFJ4WWZOT1dwQmZDakJqQ0NIQTlyMnZPdTQzWmErNzVJVFAvd2tBRUlSbEgzUVhjVXViNkIzd0Z6NWtjKzNYVFQ1K1J2TUhqTk9sa2hGS2ZkUW8yVnIrdVprKzdDcEJvMFFpbURjRUdZWFJNM3hHWTFhNGZueUJ2NzA0SUdRZng4SDljcWZxbVAvK1FwS2N2clpoYnRmTGNkTWV4ZVBSWkVFMXNjTE1mTFhBc3p3WGVnV0FSUWlwZGlmUDFpNGN1M25IM2wzY3FBMHV1Q3BBV1ZlOFluRm4xL1R0dTBqVFZFYjJxUDg5c1lvZCtjQjAzZjd4Ym12ZWZmOXB4OTR5dk9CQkJsV3Y4dnJYalJUK3JyN21JamM5TForV0lMQ3lkOTR3NEU3ekk0UFBWL2EzajNjRkJTTTFvQVZobFpNUWlUQ0ZKOGhYRE1xSVRrcUxkdjZTaW05NkFyemJwOGhjby9lR0EyMmxGTStHaWFqOG9LVzNXNS95MEZ2dWVJVHgzN2dyb0NEMUpCMTQ1d3VXT0t2dW1wVjlKNnJQcjdvN2kyL3ZQVkpOVGdmdVdZRGE3M2tQTE9NdnR4NUFNZWl2QVR3SlQzVkU1YmdFZFErVFVDZURCZ2UzYnkxMjk0MWRPT2ZPeFRycFJuWWtlNW8rWExMdE1xSDVqektDeHBuY3lrU0RCdUZiU3hRREJRVU1NMXpZY3hURnFXeVBwWVhYZmlyM2x1dnloeWlTQnBhVHZqYWFkZHZLTDB3Y3R5K3AvM20zMTU1N3AzN3pwMDNsQmtlcDlBTjZ1Nk9qMU92alNTcXJkQWlGTlk0d3NTSGoyLzgraHZ6djdyeXFsTkdWT2tkbFp3Y09xNGpSTm9BeWdlTVlTaS9ma1pPSFFOSG1kbE5zWnpVQkFDUkJqRzgwRU43NktOVG1oN2RjOGI4bTErejYyRS8rdkFwbHp3UmQrcnQ1TFh4VEd3QklPLzgrdmx2L3RFTHQzNjUxSkdiQStRaldQRmpoRUNab2tudFZMTmtLN0daUnZKSkM1UXRzSGNqcVdadE1NM3pjTmZJTGZ6dDlhZE5QVm50cnpGd2V2SjM3b1Rwcnplbno3eUZsV2NRUVdOalFCaXpBbzhJVFNUVXBFaUllWVlwcUpQYVgzWGtkZSs3NmpjSkxWZS9ZcWcyRWJBYnVtdS9MbHFLcGR6VDB5UEorTVAreS9zSkEzRVRWUkdlOUlqTlRud3ZGb3VxZDAwdkpTdFlSUFJaMTd6am9PZEhONzdHYTh1OThjbmhwK2ZrOCsyN0Qra1JXRVdBcjhFS2tLVHJOSm1rWXcyVTFTQWJRU0xHcmcyN0lDcE5idDJ0ZGM0YUhneCtmTWk4eGIrKy9yM2ZlbWd5S0tYWG15N0E3TFdnVjZFWFJrVFUvaDgvOUpNdjBFaHhvbEdCUkZrUmQyd0JXT0xqNVRPUzErd0VBWnRoYU1zTWpFZWdlWG1pV1hsR3N3SnRDSWJzWlJzUHhMYlNJSHFtTEs2L3lzQXhZTUJ5V1ArZDg3NXVqK3U4aU1ldGdSRVBheXVTN01mVUNKRldUVzA3b3Myam4zaCtNYlhUY0ZMb3IvdWQzVkJkK3hWcFpZOGpBWHA2ZXFnWEt4VjZCMnhkWTZaSTQvdDczdTk5dmZmcjQ4bDJJUzVjMXh2YS9Xd2FKcHY5Smt5RWsvNVY5OTV3NU1jSExzbWZ1ZStwSnc5c2YyTDZveHQvejRoQ2d1Z1k0Rm1BZlJ5MlR4Y2QzTGo3cG12dnZlcnVxOS8yTFhYdWtXOTlvTWxySEMzYlNtYTdnRmRFa1dOdGRkcFVSejJrbldpUHNQeHozUWYrZnZMeGIyNHNUQjRSZVo2QlVRUkExMDh4UUxaYklXbFlTOHFmQko4YzF6d1NnVm8xMUlKR2lBK3JMSG5xaDl2ZkdONjM1U2N2NTZqM2w2TmlTQUtNVmg5YThBQU9henVZaDQxRllCUTlINUl3aEpTSU5MSmE0dTN4eEdPZnYzKy93QVovUk1aVVZHc1dyYUU2THdYUTVEZmhrU2NmbVhYeExSY2Z1R243eE9zbkd5dXZIeHpma2w4MGM1K3J2dldXNzM1MTMzbnp0bWNpU3YwcDJDSlU3T21obFZpcEJxWXNsTDlZa0ZQczBrVXNyVE5xYXFUNGxHOEErUFdxWDgvLzVNQ25QdnpiclUrOHF6UXRWd0FvaElXR216bEd0UmFmbEptaW1taWRhdExYZ2dkVUxEQWNFVFNKM3FlUjRGTWtqZHFuTzdaZmJuKzA2YUlVMDd3Y1ZkRkxGNWhCNFZJdzltM2QwM3ZIblB2TjdOeDBUQUlvczhMYWloQ0pKYzk2ODNia0xuL2hlOCs4ajVhU3hnQ01pRkFtN0tZMGdadGV4UDZGWDdud2dHZkthNDh0RmN3YjE1V0c5cXI0WmtiSll6QXNRQW8rQ2FhTnF6VXRVVVB2czU5ZHZZS0lockxoZldYUFN2c2lCM1pRWDErZldvN2w2SHE4aXdZdzhDZHVyQXVKRXFLdnJ3L2Q2T2FkUGs5QTNmM2RxcisvSHc3b0VmN3d3cFB6anYzMHN0ZnFqb1pMUjlwa25oRU5rREt3Y1dWSTB1cDVwb1UwTS9YSEFnaVprTk5DYlRsZ3drQUdBMEFFNmhYTlJBVVY4alNkbzkrTjM4WmZlLzVrOUVHOW5PUGRYNzZCcy92eHNta244Mm16YmpVdE9Zc3FLUm9MSVpzbWJUNGlmV3JuMFpmYytOUU5YOWh6OXA3ZXMxOS9Oc29hMUllUEQzejNvN3M5Ty9yQ0VYL1lzV3FKQVo5YUp2T0s4YnlKd1ZFT0lJcGNIMENNZ2lFV01INGhGT3p0emRtNlovc2VYL3Znb25kZmRzelJ4MHhrUmtab0ZFSGRpN3FsZjNrLy85VWVuQmowOG43Q1V0ZkFsdUNIRDM3N2s3TlhiZi85QnpaV3Q3eHJyZDNSWWJRR3hIUHpKWldxejJXUjRtWmttdHdkU2k0enZQWWNxUmtOTUtPaDhMb1NZQVRxRlUxUXpiNlZWdVhoaWNrTjlyODNIdzRwYll2cjVQenl3dEJmOHJwZ2lZK3JWa1hlS1ozdnRhZlB2a3dzR1FxaE1GWkYvcWtKZFdDNDhOQUhicmh2RlFBMDVocFFHdHd5N2IrdS9mVFMzNVlmZitXbWFOc3JKNExxRVdNTjFVSlFTSWdtSmRES0VIa0VLNjZBcnVvR3BibUVWSUZSS1hrNVB3ZHZlN0IrdjdZOUhqMXd6bDQzZit4MUYvOWkzOW1IYkl1bVJxNGl2SzZsWGNCS0oyNERpaWoyVEdGbGUxd0J2bnROTncyK1o1QUdWZzdneFVMZzdiZC9yK2t6OTMvdjJBMmx6V2NNOGZocnl4M2VMQllCVk02UWdSSVdCYVVFS2pQVUJabE9oSmpVSUFCU3RVU1JTUE84VnZKbk5pSVlMS1A4eEpoSUlGQjdOVUxOekRFS1NzdTI2b2o2N3RhbDBUT2pqMEJTWWhoL2Z3UEhPU3A2QjB6dXJYUCtrNCtmOGQ5c0tlSnlSUy9ZM0Z6NjhSRmZPdXpxeDM3Y3R2TFozeDFNaXQ5WXlrZUhqYlRhanBHV2t0dG5oQUJTcmtPWVNFTkVDV1hIZlZKbWlrNVNua3FrbGVSWWF3VVB4cUk1MG1nTDlQaXN4cmE3OXUzWTgzY0xaKzd6MjArZCtySGZOT2Vhd2xKVS9vdHVyVG5maklucVJPTkhiL2o0a1hldHVYdlhJWnBjTmhhVWxsYWJzR3MxUjREU2dNQ1FoUklSVlR1ZEk2TktFYUYwT0V0dEZnbGhJcFJDZzQ4WmUzWUNiVG1hMkRDQjhkWGJJVmFnOW1nUU5hZkF0aUNraG14QXZ4cDVyYmxyeTMwdk5TWDYyeG80WTJSOTFweEw4ZHJPajF0RzVEODNHWG5ySjdma2JkUENpWHpnUk0rS2dEa05CdFB5UU1SRXBOekJEWW5ReGFVR0xvVklvbHBTVGFsTlRwZjZFbDk2ckFmQTFvTlBVSkhCRE5zTXR0RUx1emZNM3ZUdytsWDNublhrTy9XMFFCNy95ajFmVzczdmZrZXJlWjE3Y0tmZkJOLzNzWGxzRk0vc1dFMHZQUFNvbkxyNHBIbk44eGNzdmZtQkgvREJ1eDV5L0ZOanozZjZUVTN6aHN3RWtQUGplZExLeGlWRlZhc3FaYXBCeUJ6TG5wUXpHVVJLaVlRV0tGdWFQcWNWTXhaT0Y4bHBESzhidzQ0SHRqcUYzMTZOb2pyeklnMUVldGhZM2IvalRaVUh0LzBNWGZDeVFQUWZhMkNBc0tKTFk5bUE4Uy9jNWZObVdjZEhwQUtyQXRHOGVVS3d6UnBTR3FnWTE2MnpzSW5RN2dHaDFFVHJDcld1Y3dJeXB4UlJacHBxdHBFNkhYVWV0N2VDRklub21DSmdTeUN0d1NHSWdEeHk0RXFBS0tvZ3B4dVFWM2w0bWtDaVlOaWdJaFdFVVFEdEYrQTFOQ0JBQ0VRRzhISUFzd1g1cmxkYTBtNERWWGRJcEpERWJaNjFocmhVcUI4SG5WS0V2T2RobDMxbW96Q3JTYXd3N1hoOGgyei93MVlTclVHTFc2RGJQQ3NGcFRBYVdYM0w4RnZDKzdiZStISVI4OS9Ed0xHUm9iRU14ajkzemhmbG1Pa2Z0cFlNS3FKa1ExVmhRK0MwWERhdVF1M2VBTXpKQVJFN29FSFowZVd4bnJEbXk1bkJuakZmblAyK3B1RkszNmVrU0E3dG1HbTJBbElLVUFwaUJjd1pLOFQxRmhWUFVHWjJReFlFV3BoQmljYlRTWHRxdWk3Sml0RWxNOTZRMG9NUFNRQUpEQkF3ZGM1dHg2eTlab3JYNktOU0RiSDFnYzBZV3pNTU5IdFFpOXVJV3JXUnZOSnF4SVJ5OTBpM3ZXM3p6NUxvK05jYVIrTnY4ZnF1VTJUeUpRL2M3alY0Vm5ZckhDZDVUV2pXRm8ya3NEMENOQkUwQkVNaG9jcUNObytRVjhpTUowaHFXelVGTzJjOEdobTFwTUNKQlNCVXAzTlBKaEM0aUtEZ1psTnJTQ3lrRjVBN3U0eEEwTFdXVEdhM3E0dlNtZkNyVW9FQng4Q0FNb2JtekxYV0JzVzVONHdsbEF5YUd3dVl2Lzg4bXJISERDR2ZVTm8yUVJ0L3RaNG0xNDBUVFM5QUwyNTF4bTFTbnQ1Y3JlaWJ0cjBodW5md2w4NXoxNXUvaFduK05nWUdnUDQxUUJHZXZYeGlKVFhtMXFwTy95Uk04M0xJSzBOdFdtTkhCRVFDRkRRd2JnbGJRNEZIUUlzR2ZFWHB1QWlIbUtuK1FJeDRqeWFxS1N0Qm1SQk9xRHRJSXptRkpoMytscHpURkg4dXBjZlVVUHBacWY0NjZkeklhczh5VS9rb0xzcG44UURITU5DSW9HSlE4SDJhdTlkTXpOeXJrM0t0ZWRqQVlHajFJRGI4ZWhPaVNRTTFyeEg2d0daUWd6Sm8wajV0REo1Ri85QWJvdFhEOXpqUC9kc1k5MjhWb25kS1RkQUwwM0JFeCtIMlRlM2ZON3MwN1NsamlLVEVIamFVQ1VOR2tJdkhFMVJZVU5DRVhmUEE3THdEWTBFNjFZdXlaMCtsbTE0cW15VktlNFNKNmlld1U5dzJRcWlOR1V4MjBGbzNvNlJueXpKY2w0QmtKVHgxdWk2cDc2aVVyTWJMbGZTcUZybWNSOU4zYlpHMm1XMmdCbytzTlZKZU4wbmJIeDVDZFh0RmtQTklMV2dBelM4d2ZEQVZ0Q2NQamY4Ni85VzEzV1ZneTE4THFQNHhCczRZR2NCTTd5TUx2aXVMV2srMEFRdEZMTmdlS2xsZkJjb0c4Sk9DdGhDYU5MQkx3WlVlZmVXYTNrSjJ2Y3FVT1NGRFFNa3hCa2pHREdXbkE5VVY5VFB6cFdva1krMThoUm9naXZkM2t1eFpzdW53ODZtREl5VWQrVWdJTEh4b2FadlZRdTN6V3VFMWVnSVdsRGVYc09QUjdTaHRLUkZZZ1RwODZMMGFnQTdQd0NkUHFneGFQWG10dVhiOWUwRUlYa3JwNzUvSHdCbkdDNERTYjl2bFV6aTQrU004UGU5SnhSb0VyTEd4Q213TW5Dd2xyMXo0ZGlGY01Ec0h6TXdCQmVYR0Fack1HVUhJZW1iczVRb1pVWHVpSzY2YnBpK3A4aVFkRGlNMTc2NHBBcVEyT1RaenlGVlM2VkdKK29PQXlFS0QwTkxSTEsxem11RVhOSEhaU2pCVXdkZ3pZeWh0bm5EN2RJTkhlcGU4MFB5Q2lLOHNtc21udFpWUnZuUDBBM3p2MEhkaSt1SXZJakgrYncyY3dLYjRrZVVQYkZwcTNqam5tOWlyZVI4cGk3QVZSc2txcktzUWhnTDNqUDJZZUkvaSt1d01uekE3RDdSNWdJNjUyNGdURkIyREt1V09tZEdVVlZqV1pqdlhkemxJZlR1bTFBNWlWblhTMlpvQWo1SlVxUGJqSUNLdjJVUHpqQ2Jrb0JBTlZpVGNVa0t3dFVwbU1uSXEwYndHemZSQnV4ZElOV3NqSG1rQXBCNGV2eXY2MGZyM1lEdWVpUW1NdjU1Vy9UODBjRklZajB0cWFQUGZ1L3QvWVVIREIyMW5Qc2RWYTRoQlVySWFHeXFDYllIejRyeHlLVWZGbFhxUjE0Uk9INWlWRnpUcmVMOU1qaHFJRWEyYWNsdDFuU3BKa1oreURGTmFoY3BLUHVxT0JwZE1ZeHVSMjFJOEFsbUNYeFhDamhCbXNBb3V4eXVTRk1oWG9NNGNhRjVlcEVVek5KRnFVZ29iS2hYOGJ1SlM4N010bjVzUzRmN09ELzhmOVlyMVhSREFQNnp0SUh0U1I1Rm1OcHdtYlRsd1lBMnNFRlZZeTZaQXNDVUF5a3p3Z1BRbzh6QU9yODBhbU9ZRGJUN1FRSVFHN1R3L29lRU5hbFAwa05Gc1U5eWdrczRTeVk0cGtub2tuZ2pka3lka0JGUVd5SVFWakVlRUNTc0kySG05U3JvaWlXaWFMMnFQUnRBMGJVVkVPS2Q5TlI2Qm5pLzlqUHEzZnl6YVZub3N3ODB4L2lIZTlZOTlFZnE2RldJMVJPNjE3YWZJa2RNL1llYzNIUTVQQXhWcm1RR0VyTEFqSW15ckFzT1JhM2IyS1I3UXhraG1TNEpBS0dpZ3hRTmFOZERrQVEwNm5sa0IxQVp2WmowOGUweE9QRVhkWmtSNEVRU0JKWlFOVUdJbjhpL1oyZ0xUVkJQMUNRa1ZGR0ZtVGpBM1Q5U2lMWGtra2xNZVZTelUydkw5L1B1SlMrM0EwRzFaMGNRLzlvSC9YN3lLVU9ncEFxNGhTK3V6WjUrRy9kcytJTk56UjBsQlE4b2k3a1JxSmxTRXNEMEFoa0xuT1JFREhoRjB2UGRLTEU1TFJ2dXFlQ0g0Y08ydUhnR2VjcUxBSk5UVzFKU0FaU2ZtVDBSdW9jVGFaSzU5MXRTekpYSUthTThSWnVWQUhUN0RJd2FSUncwYVZBNkJUY0dBZnJoMGVYamJWdGV1S1VVRjF6YksrSWQ3MVAvbHF4c2FOOFZEV0FEU2I1anpCbnRnd3p0MG96cEo1alhrMkNxZ3duSGpobWhNV3NKb0JPeUl3MlRWVW94dUhXVkRWQyttazJ6cExrWFhHVVNkQVdDVXNHT1pvbnpTV2FBVTBFQkFlMDdRNlFPdFBxT0IzT2RvcFpVUHFHMlJ4V2g0dS94MjRrcDd6K0JQMDZoeCtqOW1yLzNuTkhCR281VXhOUHdGL3Y0NGR2YVp2RnZqR1RMTjIxZmE0dWE3aUNBUkc5ZHB4NFNLSlpTWU1HbWMrckJzZ2FxdFRYUFBuazZDekNGY25EbnhLOGx4VmV6aFdnRjV1RkRmN0xuT3lpWlAwT2d4Y3NLa1NKRW1MUnFnU1lZYWpKNlhUYVdiMU9yUzk2UFZvdytud0cwNXFmOUx3LzR6R2JqZW8vdUtnbG92YlY1M2RieWFYOUY4TXMzT3ZRWnQvbUtaNXJ1eHFsYUEwQ1pKVGp5U254VWlBUUp4VS9vaWRtbVZUVWM0MU95Y0ZPY1ZFVHdJUEVYd3RDQkhqbWpSWUJmV2xZS0s1M2RhQVkxRndIQzRVVzAzOTJGajlZZjI5c0U3QUZUU1VMeThwdlRFUDRYMy9ETytpbEJBbDhLbkJrd21ROVQrb1cyTG8xZE9PNFlLOWpndjd5ODIwL3pkME9xRENoN0V4RFJIY2hROU04ZUNjdGNDb3RKUWpmUnJsWlFnUlVDa0tQbGVNVVFKeUFJMGFTR2pVUW1WNkRFTTJ4VnFWZVV1KzlDT1ZRQkcweWY0eVM0UEdPQ1hJbVA5bDRGZkxIeDNkN3NEanpPUGJ6clFzbU12ZjZGNjVheEYycFBGTWpjL0hWQkhTRE0xU2w1M2tLSTIwUVR4NGcxV0o0WkZ2V2JLd0NGekN5WWp3eWpiTVZUdHNJVDJkM3JFUElQQnlnUG1nZUhOR01FTGRVL3RSOTBhVG9EM2R5VXEvdjl1NEttRUNRRmRDajFMR2FxWFgrU3hFb0I4N3JDVzNTelVybWhVUG1ibGZEVG1DNVRESENodVpkS0NFak8waktOaWgybEVBbXdMcWhSRlkxN0oyMWhkTjdvRlFMalRKNHNRZXBacXJCbVFmM2FqWmwvL0g5Sm9Rcmo5bHcvbEFBQUFBRWxGVGtTdVFtQ0MiIGFsdD0iV2hhdHNBcHAiIGNsYXNzPSJvcHQtaWNvbi1pbWciPjwvZGl2PgogICAgPGRpdiBjbGFzcz0ib3B0LXRleHQiPjxkaXYgY2xhc3M9Im9wdC10aXRsZSI+V2hhdHNBcHA8L2Rpdj48ZGl2IGNsYXNzPSJvcHQtaGFuZGxlIj4rMzcyIDU4NyAzNTQ1NjwvZGl2PjwvZGl2PgogICAgPHNwYW4gY2xhc3M9Im9wdC1hcnJvdyI+4oaSPC9zcGFuPgogIDwvYT4KICA8YSBocmVmPSJodHRwczovL3d3dy5mYWNlYm9vay5jb20vc2hhcmUvMUVMUDZLQzZyVi8/bWliZXh0aWQ9d3dYSWZyIiB0YXJnZXQ9Il9ibGFuayIgY2xhc3M9Im9wdCI+CiAgICA8ZGl2IGNsYXNzPSJvcHQtaWNvbiI+PGltZyBzcmM9ImRhdGE6aW1hZ2UvcG5nO2Jhc2U2NCxpVkJPUncwS0dnb0FBQUFOU1VoRVVnQUFBSGdBQUFCNENBWUFBQUE1WkRiU0FBQkJJa2xFUVZSNDJ1MjllYnhsVjFVdFBPWmFlNTl6Ymx1MytpYVZQb0dRMENhUlJvU3FvQkJSZk5MZEN1b0hQMFNsODJmeitkNm44aFJ2RlRaUHhGNVJBZVhEOXhSTlhlemVrMDRpcVRJQ0VoSklRMEpDMnFwVWY2dnE5cWZiZTgzeC9iSFczbnZ0VXhWSVVqZUIrT1ZBL2FweTI3UDMzR3V1TWNjWWN5N2dxZGRUcjZkZVQ3MmVlajMxZXVyMTFPdXAxNHEvNUQvZjlSQ1luRFk0ZGtlNHR1M0FodTA4NVNzdmhmL1luUVAzNE5nZUFmWVUzNnJZdGF2NFhqNFY0Q2Y4TldVd2VabmcySHJCOXUySzl4Z0YrZmpjcGttMU9MWkhzR0dHbUo1VVFQaFVnQi9QZ1A3YmQrZWcxajY3SGhoZE92c040N3Jtd21lcWpHNXl6UTBqdHRrNjIvVzdUd2Z0Q0V3cWtGeU1zUVRFcUxFQ2x6c3dwMFZPcXM1SmEvUWgwZllSTHM4Y3Q4MytQZW50Ly96MXl4ZS9OTDhYeU90dmhRWjdkaHJzdlpQQXRINjdybkI1VWp5RTI2WXN0dTlVN0pKYVJGdm4vZkM1MmZyblhwNDB4clpqZFBQNVRzemxFTFBLdENaRzFZekE1UUNOQlpTQWhxdlZIS0Q0ZndzQU1ZQUlZUHpQTkFJQUNnUEM1RXZRL3VJY2lBT21QMytINlIzL1dqWi8zOTd4Ty83blYyWXhPMSs3amRzK20yRHZIZ1YyNlZNQmZrU3ZTWXZKM2NDMHVES2dxMTl4dGp2L2UxK0VWbU1iMDRrWFkzVFQrV3l0R1VjNkRPY0k5SHBBcncvMGM0V3FRd0lnTlRMVUFGcFdZQzFnNlpNNFF6U2RDaklsZWc3bzkwRTRKM0JLWkJRa1NZSldLa2dhc0kwRUJqbVl6UVB0a3dkMTZjUXRhSi80ZDRQTzllZmROblhMdlVEUDc5UVU3SmcybU43eGJiR3E1ZHR1dFU3dU5yaDBrc1ZxSFIxOTJycjhXVzkvWlQ1NjdxczFXWFdWREs5YnJiWUI1ZzdvWkVDdjcwQ25RMFBXckJsdG1JMXJoN2xoZFVNbTFyUmt3K29XV2syRFJzc3d0UVppQkFJQlNhaUtpSUE1d1N4VDArczdkakpsMW5QUzYyVTRjYUtQMmZrdURzLzJPSHVpaDhWZXBuUktXQ3NZYmlXd0ZvWU95QmJCNWRsN1RPL3dQOG5jbC84K3YrdURYL0JYWW9DWHZqdkIzcDN1VzdsZmYvc0VlSEszeGU1SmhmaWJNWFRCRzY1MFo3MzBUVzc4Z3RmSnlPWXR6amJBVGdkb2QzT0FIQjl1bVBNM2paaHp6eHJGMW8xRFdMdW1KYU5EbHJCR1NFRXZKN285SW5kRW5oUE9BWTZPSGc0TG9SUUNRZ2hCZ2dZd1JtaElNVmFRR0lHeEFxVkl2K3N3djlqaHpQRXVEaDFaNXVHWlpaeFl5RWhWSW9XZ05XSk5ZaUdkdytEeWljL1lFMS81ODR1Lytsdi9lQ2ZRTDYvdFc3U2l2dzBDUEdVd3VWT0tWR3kvNDcydlluUHRPMlQ4bk8vRnhMbkc5YnJBZkR1RHF0bTh2b1ZMTDF4bG5uYitPTGFzSDVWbTB5QnpRQ2RUZGpvcW1TcHpoYWdqTWdVZFRkaHNGZW9JZW9STkFpU3JhNmNDRG9SQUFCSUVRVkpBZ1lqQUdpQk53RFF4WW96UTlWVVdGcnA0OE9BaUhqbzB6Nk16WGFXcVlxUWwwbWdsaGt2QThnTzM4K2hESDliYlAvcFI0TFpqSG9sZmF6Rzl3LzMvSmNDQ2JkZGI3TDBxQjREbTVWTXZ5OVk5KzkwWU8zYzdHdVBRemhJeDM5V1JWaXFYUDMwMXJueldLdG04Y1lpd0ZvdHRaNWE3WkxkUHlaVlFVSlZpbEFSSjVncHhLbkEwY0NTb1NoS2lTb0Nnd20vRVZnUUVvUlJSSlNHRUFBTDZHME1SQ0FuL0lTVlYvUWVOc0prYWFUWXNCTXI1K1Q0UEgxcVVmUS9OWS9aa1d6SFVJTWJIRW5FS3M3VHZnRjMrK2g4TmZmN25QamdQeklFVXlFNTVvc0NZZk12U2NYaVNSODUvdzdON0YvN2dUazVjOEJvMFY4TXRMK1JZeXJCaFBMWGJybGlQNXoxem5ReU5KbGhzNTV4ZnpORHRFNnBBUm90Y0tVNkJuSVJUd2psU0tlSklPQnFvRXVxWEs1VWlwSklFMWNNc3NXSkFRcFQwQlpjeTdCRGlkMDBSSVgxNEJhU2dnR2NCY3BPd0JraFN3MmJEQ0RQS3NhUExlR0QvTEkvTXRoVUNsYUUwRlp2QUxPemJoeVAvL3I3ODl0OSsveE9adHVVSlg3V1ROSmdXdHg3clIwOSsxNi8vZDY1OTJzL0lxbk9HM2VLY3cyd1hXODRhTWxjL2Z3T2U5NHcxUWlPWW1jKzQzUFdCekZ6T0xCZjBGZWpsUnB3amNpVnpFcW9RSmVFVWNFbzZDaFFpcXZBcFYzMWVWdEl2WS85MkpJQXVraFFQaHVqZkp3a1JIMUtQQ3BRKzBBRWtFTEFHTUFJQ2hDaEZSTkJzV0JnTExzeDNjZi85czNMczZIeU94Q2lHaHh1R0diQncvMmZOdmYvOGkvbUJ2LzRTUkFDKzNnTFQ3c2tmNE1sSmk0LzluUU1WOW9wZEwrZWF5LzhRYTU5K0NhVUh6aXpuRTYwa2VlMzJqWGpSZDZ4SHJzSWpKL3BZNmxGeUJicVpzcHRUbkZMN3VVcm1SUHE1TUNja1YwQlZtUk9pQktoS1J4R0ZRRldnOElFRkNmVVpYRWdvUUtHL3czNnRsdVVUZlhBQnFGL0pZUzJISnlUY012RUpIRVpJQTBMRWlBRUlKWXhSYWFhR2lUVTRlYnd0OTk5M1FoY1hsb0d4SVpYR1JJS2wvWmxaK01wdnV5Lyt3cThCYUdQYlZJSzl1L0luYjRCRFNyNElhTzc3enZlOU4xLy93cDh4b3h2Z2xoWXpMT2IyKzErd1hsNy95czB3YVlxRE16MHNkQlg5SE9qMFZUcDlzcHREK3JsU0NmUWRrRHRLM3dreUZTZ0pWU0tuQjhQT3gxSVV2c2FsK0ZSS3Y1TEo4SFhGdGZ0WWtoQ0JLRUZBQ2pEbWw3VDRUWm5LNG9ZUkVFTkFCRFFlaDhNWUFLSDBNdUw4OXdCc05qMTFjbWpmTEE0ZW1oY0tuVFNIQktrMU9IbjdsKzI5bjN4SGZ1QnZic1FrTGFabHhWUDI0eC9nYmRjbjJIdFZubDc4dHVmb2Vkcy9oTFhQL0E1VktJOHM4Y0t6aHN3N3Jqa0hGNSszU3U0LzBzWEpaWWUrQTlvOVpic0g5SEpLSjFQMGNoOVk1eHh6aXVRS1pMa2dwOEJSUVNVZGpCU3AySVVscWpSU0FPY0FuWVhoMy83YXBSUVJXSExZbEFDakpXUnFHQ0VrUEI1VjBQM2ViSXJDS3lSNHZ6SDRIeUpTZkEyUnRoSjBPbjA1ZE45SkxzMjNpZEZHaHNaSTB5enVhOXVaVzM0MnUvWGRINElZZ084Mkt3bkE1SEhkYndOS2Jqejc3YS9MejNuZFgyTE5SU082Tko5aHBwdSs0ZXJOZVBOcnpzTEpaZkwrUTEyME0wcTNyMWpxS2JwOW9KY0RuUnpvNTBRL0ovdU9vbFJtRHNocEpGTXdwd2U1Sk5YNWYwdFlyRUw2OUt5c2hWQ1VvUGdsTEdFMUVvU29oN2UrWmdKaEVGSStKUVJQdzArVThzWUpJYVRTRkp1MUIyTXdBRVFVNHZPOXovYUVOQnNpSXNKamgrWng0dmdpWWFqU0hFcGdDSFBreSs5My8vRzIvd3FnQjB5dTJMNHNqMFBLWnd5bUdpLzQ1Vjl5VzEvK2F4alpBbmZzcEZ2Zk12SmZmK1FDOHgzUFc0UDdEdlo0YkQ3RFlsZXgzRlZaN2luYUliajlIT2c1b3UvOGYrZXE5T2daVUJWa2hEajFPNmhmclQ2MU1yQ0ZLaElLWGpCT3UyUWhPQW5FQk5iREwycVJFT3hTa1dKSTFDQ2tCRm4wcFZMeEJjcnFzbGtFbCtHTGl1OEJERVNNRVFnVVNXclFYdTV3NXVBODhpeWpERFV5SktOTk0zUGp2elh2M1BtRzl2SDloekU1YVRGOTVrR1d4MlhsaHVDbUwvekQzM0hudi9UbnhEWnpkMkJPcnJoNHhQemlXeTRBR3kzWmY3U1BUdVk0dDV4am9hdlM3aEdkUHRETmlWNE9aZzdTVjA4cloycjhIcXRLUjRpalFhNkFvL2d5Q0VvdFpJSVFRQ1VJc2VJMzNZTEQ4R1FIQytnRUNzUVVIMmV4djRMcUF5ZStYQ0lKS3g2ZCtVQUtJQlNmSFh4UVNaK3V4YS9lWXVWU3FPVm1JQUhNZ1lva0FaeFRuRHl5SVAxdTE4R0t5dkJFYXVidnVyZDU1d2NuMjRjK2M4dEtnSzhWRGpBRlV4RHNFcFdYL3VtZll2TkwzeTZXbVI1WnRLKzZjcDM4NUJ2UDU2SFpYQTZmeUtTbndFTEhjYUdkWTdsSHRETklMeU02T1pBN0lGZXdyeUpaV1FvRjBra0pSNWhNQmM3ZldrOUNoRjJ2UU0xRnNnemhSQW10QXFqeXRFYkJaSVZJUVJ6aEJNWkhKMVBBbGJ1aFk1RitEWUhFK05STlZUSDBuemFrS1RaM1kzd3E4QVYyMk1QVi8yNEJZS2dVQVZURUxKNVlZcmJjRVZqa0dGcWRtUGw3WnMxWFAvQjkrYkZQL2NlWkJsbFdOTGlUTUpnV0o5cy8vR0ZzZU5HUEdwdjEzZUhGOUkydjJJUTMvT0M1dk85Z1R4WTd5c1d1bXNXdVlxbW5YT3BST24yaUU0TGJkLzZtNWc3TUNQRnFyWWp6UzFIVWI3THMwL29WRkdnbm53eEowdGVwaW1qSFpIR3hoS3BXS05VWFFPcEFxaUpKa21HWW5OQk1rZVNDVVNzWXNRb2JjQmVvY0ZCMG5VRW5KM29nRkFxVENtQ0J2TmRHa3RKWnFnaGhpdHFyNEVVWmlCUWhhY0dpZkJNMXd0NUNXN0oyaHpBdVIyTk5taTdjY1NLOSt3UGYzVDU0L2ExbkVtUlpPVUJGaTcyUzIrMGYrUXUzOGNWdk1kck85TWlpZmRQM25TV3ZmZVhadkhOL1c3cVpvdDJudEx2RVlvOVk3aWtXdThxZUE3b1owVk5JUDZoMWZyODFjS3B3OEtsWXFhUUNLb0pjUXlJc0VKU0lsQ3Mzb0dpUGYrRVRzaENrQ0gwaEJSckoxV2xxTVlyUk5qQ3lQSmN2ek9zTmwxdzhsbTEyeTNmY2NIdjd5Ni9hUG1JbUx4L1I4UmFBQnJEY0JZNHZPM3orZ01ObnY5ekYvYmN1NEMydm5uajFqUWN4OXVCTXA3RjVYRjU4cE5kTU8wTTVrRGdpQUxTeTFBNXNpOStUQTREd1FBMFVRYmJjZ2ZaNi9nbHJyYkoyNmY1anpmdis4Ulh0Qi8vcTFwajllK0lEL05ZUHBQamcyN0wwcGIvekhyZmxsZTlHbG1WNmZDbmQ4VDFiOU5YZmV4YnUzZC9CY3Vaa3VhZm85Q0dkdm1DNXIxanVLWmY3REdES3AyTVBva0JIUVVZZk5DWGhJQVFWcW9BVEl6bUxaRmNnNUFCdGZENE1LTnFYTXdYQUFpR3dMbGRIU2R5WVhiZThESnpzWFB1eXB5V2YvOTduTlQ3eFkyL2FlRzllK0FJZTRhWGI4TFdwQVQ1KzQ4bHpicnl4ODEwZitWTHZUKy9KZE5RYXBaQW12QWtwMzZwcVNYMlNNRlFsU0NFQTEra1MvVXlBM0tHMXhwcUZlNDhtdC8vQlMvckhiN2dIK0pWSFhVS2RlWUJEK21oZThRdnZjQmUrL2svVU5QcDZaQ0Y5K2ZQWHlZNGZPRi92UGRDUlh1N1E2WlB0aktiVEo5dDlrWGJtQVZVbkEvcU96SlNTTzBFZWdLbFRRWTdLak9IQ3J1bnhqNmNTQ2o2WUJXSkdRVUpXekxHSUFFb1lrTTRpUjdlUmJtN1A0N3pVL05sYnZuL1ZYNzF6eC9qbnN2S1cwUURUQWt3QzIvYkl0dTNic1gwN3NEMThkay94OXg3L0gzdXhCOWc3RXhFVE8xeWpBVno0dzNjLzlMVXMzU28yZDFDYUFxQjdXbFI5eWVXWkY0QmFZZ0lQK1JWc2QrR1Y2dHloc1NaSkZtNjZ0M0hqdTE3UzdwdzQ4bWpyNURNTThGUUM3TXJ0MDMvK2FqN2pWWitVa2ZXNU8zamNQdXZDMWVaSEp5L0FnNGVYMmMwVi9ReG9aNHB1RG5SelNqY3p4Yi9SZFVUbTRCVWdvZ3h3d1VRcENJWEhvbFF0cVArUW5zdXRsSkFnQW9XNnlQOC9mTkNva3RCVjJlcGs3WW1adjM3TE52dUg3LzdwelRmNjc5OXR0MDFOeW5aQWR3MVlnaDR0QnBtYWd1emNpZGJUZnVpcjk5NWpXcHZGcXFQQ0NBTWNoMWM2UEtMT0F6ZEtvUWI2clhoRW5RT3lQcEU3UU5YSnlFU1NIdi84NS9vM3ZQTmxtR0tPWGNKSHluaWRRWUNuRExpVHpVMlhuSnRkK1Z1ZjUvZ2xtN2g0MHAwMTByUnZmTzFGTXJ1UW9kM3JNOHU5SGFhVEFkMWMwY3NGZlNmU2N4SlNNNW41a2tlY3N0cHZBNzJ2WVpFNmdSaXZDVkE5VTRpS2tDcUZYaUdGZ1dIMmxZcW9FNXZhcytjZFhyVFovZnluL25qTCsrYVhpVzFUVE00OHFBTWcwNWZVemFmOThLMzMzNE9STFdLZG80ZnkwWDdpVWFJVWxDbHBQSStxUGhWNXhseWdDdlF6UURNQTZKdWhpVVp5NEJOLzNyL3B2LzlFd1E0K2tuZGxIbnV0ZTVsTWlZaGU5dE4vZzFWUDI0ejJuQnNWMkt1djJvSkRKM3M0c2REblVsZXgwQ1VXTzRxbHZtSTVFMmxua09XTUhvVTZvQmM0NVV5Sm5ENUYrejhDNTZzUDVsNDRvQU9RQzB3aEI3QkV0eDVsQmQxSHFzWGdWTklSYXgvczcvL1J5L0FEMTc1Mzgvdm1sMVVtZDlQdTNTWDV5Z1YzSU5SRlJVenhtb2FYbVl1UEZaSnp3YUtFZlVVS3QwRkE5d1pJVWtBc0lVaTAxK203RFMvKzhlYUZiLzV4N0wwcXg3YXA1UEZid1FIUnlRdC83L2R4M3RVL0kvMmx2czUxazVkLzExbllzRzVJWnBkOC91eXJZejhBcUo0RCt3cnBPeUJ6Z295Q1hBRWw2UURKZy9MajZGRXd4WU9rUUNiUncyVEhVR1ZLUmYwREFlZVljSzhZWkNDRmFRTDN0NDgwRHA2NHFuL2JjNytPSzI1S2NmT1YyZVBEekVZcitBMWZ2ZjhlR2RvaUp0TlFJWlhPa3BMbnBpdjJrbkRCNmk4bUFLNUMva0tlKzY5MWpwSU0wL1FQZE16WFB2clM3UERmZndXLzhpc0d1Nzd4ZnZ3WVZyQVBycjNzelZmTDVoZitqTENmNjNJdnVlRGNDVXlzYmVIWWJBK2R2c05pTjhkUzExVm91YWRvOTVTZERPZ0crckh2aUV5SnpKRk9pVnlKbkJGRDVVdFBUeEJRaTZlYkRMb2ZpLzJXNnZkZTljRVZ6eVRxK21WamZ1NDUrclA5MjU3NzljbmRiRHgrd2EyL1ZHVWc4Q2lCUVhsaEJYbGRYbVR4OGVwVEFBWEdFTVlBSW9iWk10em9CYU55OXZZUGdXemd6c3ZrbXkxUzg2aFQ4OVFrVjYxYU5XRzJidjhnRzJOa1owbUdobE9jZC82WW5KanRTamNuMmoxRnUrOFJjcnNQYVdkRXh3RmRKOUozUkM4bitvN0lGY3hWSktjSnlsQzgvL3EvS1l6Mlk0RVdGOFJBOEpPeEFCQ2dzK2FOYkZYeW5OSFozLzJqOTUwN2ZjVmJtVTd2a1A0VGJvVHlXU2FrNkpKWWtVSjJScUZCU1VsaWhvOEZRTzg5MndLeElkQmkwVjNLOG5WWFh0RjQ1bjk3RjZaM09FenVOaXNYNE1uZEJydEV1OC80dVY5MmE1NS9EbnRMT1RPYUM3YXVRaStqTHJRZDIxMkhicWJvWmc3dHZxTFRjK3hsaWp4WEQ2TFU2N2RVd2prVnA0NVVCM28rQ2FENmZ5dEJWVi9oYXVDQkN5NjVlT3FMRFVzcng0WkFjMmJXYmw2YXVmc3pmM1QrcnN6dHRqZDlBRG1ld0pkQUJRVkhXZnI4b3BJOUlFT3ZleFdPb0pER0s5MHlaSzJTUmcyZnp4SjExcmwxTC9pbDRTMHZmeTZtcjNGZWZUclRBRTlOR1h6c0d0ZTQ0UHN2enRaYytRN05zaHhaYmxldkdzTG9XQU9MUzVuME1xS2RFVjQ0SUx1Wm9wZXpRc3ZPc1VqSG1lZVU0WWlBbnVFOWJjcENrdldYcWlDZ0lmaWUrYWpNRjNFNlUwQTlKVEtXcGZMcVo5cmZFSkdGYlZPVEl2TEUrcElUS3p3RjRUREt2MXJrYXltc250WDdrM2o1UzBYUWlORExYMWFRTFltT1hwRDJ6M241Ny9wbXU4a1ZXTUYzWGlZZ29XZS82bi9vOEhuRHlKWW9GR3hjTjRKMko1TmVydWhsbm5MczVrUTNwL1J6b3Evdys2d0N1UlBKRlpJRnNVWUpPbC8yUkVTRlZOUnh1RHJDZUt0RWVKTGxWSUFvOFBlQXlKTGtMRGwreisvLzB0YmRtS0xac3hOUHFFM1YzOVd3TlpMaHdvclZoOEtWV1VyS05mQmQzUVdmd2dzZ0tjTHl5MzNjRFh0TE9TZWVkMVhyd2pmdDhCVG02VmR4OGdoVHM4WDBOYTUxOFkrOHBMZnEwdGZDZFJ3eUpoUGpUZGdFNlBTYzN6K2h5RFdvTUFITUJxSUM2cWwvS2lCSzY5Ky8xOWhaRWowVk54RnBkU0tnZ3FVZlRrdFdZekR0MFZCVE5zMXFkUDVBUkxxWXVqNFJ1V3BGMGpOSmtaMlFLZXd4QmJlMUovcjgwbUhJcXphVEFKSXNSNVdpUTZTamY3SzBHYkJZWkN6U1Z2VHhFbndWbnFGcWhUc0E3Qm1YcmxKczJ2YmV0WE5mL09TSjQ3dVh5djM5OUFFdVhJVVB0NjhRK2RZWFRyRzVUdENkcDdFV3c2TXRMbmVkT0JVcUtRNkVVMEhmZVdBRUlWUUNXVkc4ZHpFK1hLR3dFWGpiYWxrYmx2cDVkRU9rTUU4SlNQR09qSElobDNJY3FjYXN6dHJkZDB5dStjUVgvbDlnQ3R0MTF4a0dkbXFLWnRlZEVCRnhBTGpyRzlEVU53UDQ5VjlEZnRFMWR6amt4dE9RNVlOWklNSEM1MGVwUWVmNHpvc0lWT2xUZ1l0a3puQjNCQUNNUWJhY2NkVXp6cHZmL0YvZUNwSGZPWjNxOU0zcjRPQXNHSG5XTzcrN2U4RnJyM01jYzNDNUhScHVjR0s4UlNyRlU0a0dDa3F1QmptTlp4Qzg3ZGliVnd4QVdBRkJCL2gyUGhObFkyR3duUmRCTlVFcEtGWnpNRno1cjVHU1NBaUt2TGRQRE5sTmN5ZXVQL0YzbDd3c3k5VUFaMHBrc1B3WkpFZSs4MldIMTEzemc3cDlNVzJ1UDNxMHc0VytTajhqMDRiQ0psYUdhWGp1VnB2OHhSY1czdlgxamxrbDF0dnB5K3prSFFuaE9oU1ZleVRVdklqcXcrSmpBWWlFZE8vVG9jc0JPc0NvSWhrVnMzamJZZjNjYjF3R1BqZ2Y5bkErOGhSOTZXNENndDdhWi8yOFM5Y0IzU1VDUUNPMXlMSmNHR3htU2xKRjZFaHgxRkNyZTB1RFYwSDkwK2ovRnA5eUhRUW1XRkZMYlRld1BhSU1nUXpCUlVSYmxjdFdxaTFMTkhIV1hyS3BlZmVlbklJcEdPekNZdzd3NUc3YXYzK0R1QSs5LzZHTFAvRHYrVStmLy9wN1h6a3p5blB1L0Z3cnRRMkgzS1VGV1Jac2VJQlFJSGNyRmhSQTZrZ0hnU2xTZEpGb05Icm5JWTh4amtsSTE5N2ZXWmtBaXl3cmtUR0tScEF0TzR3OVkwdmoyZGY4ZUYva3R3ZGx4VzhTNEVtTFhlS0duL1hEVjNSSHovMGU1TjBjcExWSkFnV1F1YkoycDBxUXUwQzR3c2ZrbVRqL05nT253eEFyTU9vUGlSTlFCSzhRRm5YNVJFdjRXQ2dveStvQndTVFJ6ckEyMGY4QVFOeDVCdmJUU2RycEhlSmU4bzY3cnRtNWx4OCt5SkZoTjl3RlJqT0FXUTd0RTJuNDJyVFFGZ3M3RHdBaUNUM0lMT2xJalpPbVZzUkg5WTlDL2dwMklSVFhHR2txOFRXWFc3c29MVGwrL2p1MkF1OC9NRDNaalI2QmI0S2lBL3p1clhueE8zVm9rNEhMSEVnWVkrRWNrWVUvdVJKT3ZTZFpRNW1qREo3bENoZDZ2cmlzY29wTnVXaitZVlVpTURLMkZVOTh4ZkFVZlNXSVBrOFFrc0xoTTBleXI1MXBXcFpwY2M5NXpkMnYrY0sra2IvWjM1SmgxMXpPUkp5REE2RklBRWxCU1VHazBQQjMrWWRwSURHS0dqZVFHdVVlTEdXQXduNFRTQnRHMVZIeFBZVkQ5MVNtcXdTbXNNaTdUb2ZPditEbzA5NzZYd0FoSmlmTkl5bVRES1ozdUxFdGw2elZkT09yMGM4SXpkTUNGdVdxa2p1RmN4cW9RdTlQOXJTcWcvZ1dyNGhTWkJubDZzMFdlVHc4MFZyWUpBdDFSY1ArRmZqWnVPd285TlRpVm1XS3MxY1ovTU12YmhnRmdOMlRqdzFRQWVCZi9sUG4vTm1tbWM1WE96R2FPVGltSkUwVW1CZzdzRXBIUVZ3UUk1QlN6eFJVL0JzS08yQkpWeFptM2dMZlN2RjBsUHQwVEcxV2dTNzM2Wnh3R1doR0thdWU4WTVxVy8xbUFkNDJaUUJJdnZsbHJ6ZGpXOVlnNitlQUZURkJqMVZTQzNIZWdVNGh6bXQ1UlkxYm1KS2xhQWNLc1FaNG1pMm5SbHhJbExLS214U1RHNnpJRFlZeXFkR1FvMGNXRGw2eFdiNENBU1luSC8zK3UrdE9TR0tFZjNuZGtWL1l6MUVyVmpPbHNRRVhSTkJGcW9xRFh2eUkwbkRJVW9pQ0psRkFvN1RNcUZ4aW5LMkxaOTlJaFRWRVRwR0FBMTRScXFYclFvZk9lbEZqN1ZWUDk4M3pVK1liQjNqN1RnWEFmUFNpSDNKb2VrVTJwTkZpZ2FscVdIU2hxVXRaNHFBU1Z6QktyU3g5cUJGS0xENVg3dFoxd3IxMFBxRFltMlJRbkNPRVlxd3NMUGFYSnM2ZG1BMzM5Tkh2d2J1aElvTGJEM1pmZ2lFbEtUYmlVYUtxUTJLL2tFZSt0WnFrZGcwczNYODFycm1vQXFvZlhtMWJSVEhFa0xtRVVibFZydzRsTUozT1pkcmExTUNtRjcvZUwxQjh3d0FMZGhsdGJYcmV1ZG82OTBwa1hZQnFLdk9ZTjhYNWdDcVVIdDdUOTJrV3NONWp4TktkVHMrdHVzcURVUkFYd1lkVHF2dTFVVWhhNDJjTE9TMm9NaFVwUUJLU1dNUHF4ais2OU94VE1HKzRkZjdTa2ZHaEMrQjZFQWxiUW1HUUsxbmpXdnQ0aWU4cmtTRjZEMFdUY2V6UkpVdUhSMTFOUXJXVWk5K0xxRFh5bElLNXpHb2lMamVrZ0VPclh3M0FZTTlPOS9BQjNqWmxBY0p0M3ZaU050ZVBRUFBjNzRXbXNpRXJDNm1uYkJ3b3I3cGtOY0tLSzd5anBmR3M0TS9MaWlCYTdqRmxWOHIzeFFxWDZOa0ZVSDRHb1pIWHI5ekhncDkzK3I5KzlxTkhXeWN5dGp6L0hRQlJ6QStYT0tLMldLVzJ3cVRHazBmb01hemdrcXlMaFFpcGwxTmVjWktpUjZycW9odWtNY3FTd3pMdlVZYzJQMmQwN1RPZjV1bk5LWFA2QUcrNHpMKzEwUXRlbzZaRmFDWVZGVm9hdUtGUVlkaGpXVkZyVmRjQUloZGNpUVpMWEIydlZpa3ZPTzZ6amtGWmdVZ3I5SG5LYWprVFJhRmd2RzYrcGFkTFBmam1YdzZXYnFqVDRNWDdxc3FqV0RHUXNzVFR3WGZtMjlPcUZSRlZGMVd5a2xydEpXWEhLcXFXbWVLM2hDWTdsem1tRzlQdWh1L1pYcVJwYzlyMFBMM0RyVjU5eFNwdGJmZ3U1SmtBTk9XSzlLaXZBa0dGSXEvUjAxMmszU0RFbHoyZVZGYmZWd2krQThHT0ExNDFiTEpFMnVYNzBLcmNLcDZpRlRBQnAwVTJLSitsV3VBaSthOEVHZlg2cGZwUFZrRmdYQ1VRNmxCcW5pVXB3RXA4b05aTkFDU2dqdVZXVWJZcEYvZEJRd2VzRXFZQjA5aXdyVmlvNWpTMXJ3R0F6cm1YUDVmcHhEcmtmUmZlaE1CWVJ1bVhFUUllSUdQcTl6MFFHaXdSYjZXV1ZUZEphcW05VmoxWEZhSnY5Z2p1anByZ0wxU0lQZk1JWjh2MXJiQVc1S3JQTE5TdXhmdVBVSEw1SUxCK1YxZ1R5cUliVit5M0VpRlRLUkJuQlNsTG93Q2k2SVk2Mm9UVjROUjdzSWZXdnZCU1hOckE5RFh1MUFBZmU2Y0FRSC9vb2hjeUdSZFExZSt6d2NQTHVQQUdLd0JGUkh0dFVTZkZvTUw0TktQVnFpMVRWN2haWmZOOFJOQVhMdE9LL2hKZlowcFZkamdWRUN2U09wMDJTa0V5WnRGcUJsME0vckphTnlKcnlsZ1VWaWtYU2luZWh6RSs1ZFlWdnQrVVdZMzFueDIrbjZWVERXVXpuTmN1aFAwTzFVeHN2V2ZEbHFlSE50akIvZGRQWm1XeTVuSm9XcVJaNDNVL1Y2V09zampVUVRRbzFVQ01JaFVIRzRkcW5aa3AzaVFqcTJoeGd6Ujh2MGI3bUtMNFdZRXlpd29Wd3FmQU00NXdCaFlsZE4wakZZSEY0bitCY0JtSVF2V2YxYnlQa3F1TUI0WXc4bVJGQkVDVjVqVU9aRlU3VjZVb1kvRHExWURNMFk0a012N3NTMDZIb2dYVDFnRm9JRzFlQmpxVXFhaFFjTXAzcjlGR0phempBcW5BUjRXSHFsUlZQZjBSSnl0VnFWR1dJVVM5L3F2VndYVkdMTXA2WjVTaXMzSWx5b0FXenpyWUtpN2ZGQkpZdERnMVFyc2FwMlVNWXU5cTdrZVI1aUVEU0RrV1dPcitBQ2tCWGFtcEUwcWtJMkNqZGVWcEFqemxWK1Q2UzlmUThUeTR2cTkvNjRnbjZvVldHWGphNnFBcEppcjg3NUpvaGRmVXpWSitRa2o1ZXByTm5YWFRRMXhSQ2JFaUV3T0hoMVBZT0F6MWliWVNBY0Q2SGhwdk8zRXZEUWEyblpnZ3FVeDRsZFJTSlFNcDAzS1p4dU5GRWU0VDR2S1RGT2NWUGpiV1hYeHFnTGZ0TVFCZzF6M2pNbWxPRENQdmE0V01DY1NrY2xYS3NBclk0TisxWXA1KzNGeThBQUw1d2FwVnYzemdpNVJicG5nWFd2ZWhwVTRhZDRLV1c4ZVp2VVltd29RVkxZQmt0TjE2a2lVQW5aS2dxWWdKRnB5eVZzbU9KU05WWVJTTnR6R3Q3N1cxV3BlUjhzVEtzQmQ3d2t0elg3bklqUGdhL2xrQWt0UEtoYWJSMnF6SmlLQ3ZMa2lLckFFS2lkNjBTU29xTFU3R2xlMm9LdURMQVdLRGRvTllXZ3Q5bmdicVp4OUlIY3I2MytOS2JsYThKQzNla3JWUy9xdTgxS2xqc2RvVWt6blUrS3FnQkZKQkJSU0FhaU1hQm5VMWlZQXAzQm9ZMEhxTHZWb2k0Q0dWcDdxd1AyaVJ0VFdTRXpReVFSRENISklPanpoc0hSOEk4SFlBZStHR25yV09tZ0xhamxMamdNV292SUNpSmk2NE40MUlpT0JnTUZMSmYrTEtrUXFnOFduRzFJZ0NQNXdxYnhnL0lWU2lQQk1FYUdOUnNUc0N3RmttQ1d6UFRaQk1SZVF4Rzl5Wk40Mlk4UVRpMWZxS2s0d1VvYUx0WHdvamV5eFhMd1BNNklXQ2VHWEdpTHNnTnNJV3BYR21ZTVJWSzB0RWZqb21rRklLT3NYdm9sTm9ua0hzOE5wa3paVVgxUU84WVNlQlhTRHNjeUFwb0s0YW1GMmtFUkVUdWMwRENSd0RBNmtBQU91N2l3K1FrV29jWUZIM3FiOW9CV0VoWnRuaHNnMzVIWnRidHBjN0ZiRitXMGw5UnpWQmxYWXVNQXBZWTJDYm9rMzB6S3JOWTdPTHdDb0F4MzJ2eXlQZmxLY0NtL1ZqcjFqZHVmWmpNNzk1enkzTHgwV04wQmlXNk54a2ZuSGxXbVdqNHVITG5LRlN6YVhEUDZRVEkxZkE5WjEvZ2lNbERFRWtMN3NkRUc5L2lQWjBLVldwMHBBWExiS2l0NmRBMzNWMlVLaE9ZWVlhU0JzYms5TXJEZjAxZGJ3WDloSVQ2alV2c2tSbXNNS3pVcHRRdzFLWUozeHRSNG1jZFRFS2xHQUhFS0VTWTViOGtiZHQvTDZkMzdsbVB3VjEzWGVnOUVRbG04TTU0Sy9lVy9JQ2oycERMaHJSZnVQTjY3NEc0bDAxRThZanJiQUVHSHY2VjY0OEtla1ZrRjVFOTBSU241U3JWMnF5bXdocVRGL0JzakRlNzRydmc4UWtRc2w5bDVROUlXS0I0ZldOMDF0MlRNT0Fyb291NDd4dm9rSytOTUw1SUdrNXp5WkdnZjVqV3BBSFlRQ05oQ1pnZzZwTUtNVUhsZXMrY2JMWjdVT0EzUWJZOFREM2VuQXpMNTNUanhsdCtkdDJmWUp0MjcvQlYrMUIzQloreGRPM3k4MTM3MkYvei9ZMWwvL29IYTg4T2RlR1NjU0U2YlorMVpxSWVTc05kQnJKaDZXUklhSXRhNnMzU3RGQmZTdVZKb1F4ZlFTY0kvMlJGREROOWFNUEYrQ20veUdGazB3QTlXMWRQb0Fhc2s5Tkl5c0k4VGhGVjlSYXNWL1REaHIzcFp4T0VtWXVhRVlZRjlMYTFDU3g2K0U0S2pubHYyVkZobEpjbFdQdkkvL3FtL2RTZ0tzbzhyK1haTnRteGFZeGFKNmRVc3JXTjgreXN5R3N3R0x5VXB5ZVN0VXFxb1VaVzR1bHhvQVZnak9wRUFPeFpzSU1DdDcrTVRhTnNPOVUrMnhKSmhTejRjZ0IxYU8rc0NxeUt6WThTK25FaUhPMUZHYVY4STBHRENjZ1BDbGVrNVBUQmdMOHpwODg2OWxqNHlQRG9Eb3A2OWVDSkFKT3VWOENVd29ScFNXRUpUTlFPbDFBaWNEVzZlVEM2cUNSUUNtTENJemhtRGw5NG12WWFIcWJsSkE4SmhtMEZNTUp1RW8xS3ZsRnJWU1JncndRY2dBc1ZNVjg5TE9OVUpyalQ1NEFIN3QwdllEQXY5elp2UVRONFNiVWhYVlpPRExJNkw1RjNZYWhxMThHRmtuczVXSjhud2VWdDVMS3JJS3ZyalNaT0pjMXpZRG9MY1VhRjJCUXptT05INDA5Vkl5NVpRd3MwcHJnWDdlWFJ1M2R0U3Q3a3AweHRuZVAvL3ZUSDNsb1lhR3ZvVGVwVklrNEtMR1ZUdEFpZDJ1Qm5FdUNaWURnaUUwQVJjcVA0b0pUMHJTM3pLcVQwNi9nUE05cUVGS0R0aHQ3aGg1T3ZtRmtYNmxaZWhIMTlBNFMrZEZoQ1FEVUdDeWcrU1NLOEF5TkFDLzlnWFV2UTJNZ0NjZG1BRCthUVNvdk42WGVXb3FvcGtWbFJxeCtsTlJwMmdIRFhtVlU5QTN4c0l2MUFJZHovSVJMWFcvb2N4V05XS1VMS2RXZlU2MnczcHlIU0xEV1VLd3JxMzVYUmxSalJWTldrMmh5OVdjZ1BYazJZU2lCcjg3cCtURCt0dFZSY0N6ZUsyc0tHUXBianRZa3FQQ29zN0lIaFhzbmtRbXhmRWdpTWdTRW9maDIrYnczZjlvVmJGdzdSOGxueE9KOXhMeFgwMDhZN1RYUlE2a0JnVE55UGVpQTY2RjRDdFhQeE5mcWFYeXlyRjhCZ0V0QmtxUGpLYlpDcy9JNWpRWjNEYmdodFBwc3hXVkhIRUtFZThvT2QwWU96Wkt5clc0bWkxRk04RnNFYzJpMk1IL2FNb2t1YjBNVjVSdytxaDg4SWJZWXV4eU9JNkVuWUdsWTh5eFZ3bEJBa0VVOWJlU1VkRzRLd1QvaTlJMWc0VW15Z05Yek03cm5wemlSakRlZWlhVStZR2xpdjFIZGpSajFXaUV5UEZRVXNBek9nSWc0NGdwc1Y1K08rb0MweU9RaUxvY3VIVzNYVi9DeFBmNG4yOFlSMFR4U2ZBcFIzZ1ZuWTV5aXdib1lYaURvc09BckJXYlEvQjNVb1JxZ1lLRDk4R1FwazRvQzZLcDEwKzE3RDNaNmFBaW9XblZzYUpGK1dYVnRhTkhTVXJwRzRsSXB5blFhdCs2d0xwMGlxbEFDNGc0Y2lZR0RhQS9Pc0d0T1pXZ0E5dWJ2RW1aZU55c2drOGF5VkUyNGpGeitrWTJua3IyaWtpQ1MyalFTMWpXZVFCUCtmcExzd1pPVFBnZjk1THVmZThYdzhGQWpEQnZCS2ZlSk1jbXFqTnBKY1dvWkdqT0VZUXNyOWZFQmxCcTJ0OURxNkEyV1lneVp6elpsNlo3VEJyalpmbUJHOG5iNEJmSGdqRUNXbjRMaUNtTjQyUFFsdHJZeUJvSVY0SkRJZEZkMnpRWDBaNERldDNRVExoSHVOLzF6LytxYkRVRDV6TmZtTHRibW1JVlNnMnMyYXIyUkNrMlh6cGdnaDBuZEZoRDV0cUpaSGdVK1pyMGtyVUVCVnVaOHBKQjhhYjUzN0Y4UDF2Zmd2ZHNWMkFzajgzZEQ1ektZOVVrbGJNTTNnMG5RSDB0VnlCS2lRY2MxTEFHQ0dNQm8xTzVwcEVhc2Erai9OUmlnWnZ5SkphMU1FMnk3UHNHZGR4aHN1LzVVTG5vdkFNd1EyOVpYSjMxakR4N3BpTDl2a25nZmNTVis4d2VSQzhEelIyNTY2ZGVQZHlBTkdzYWtUalV0UjZxU00xUVUza2xtcXZGQlJhTmRjTXVVYm8yb202T21QR2wwS0lYZkdnMUlJeFpnOWdDQS9nREkya1ZBMExqditnT2R0VCs0aEtFdHE1RjNLMGxQbFRBMFZhVmRzRjFoMXBNV3NoSHFqVkpxcXRVcVJxSlpLcXdCaldKcWdRRXVmK081TTE5NHdiTWZObGdXZ0xWQWYrOEtyOTFpblBPakE5Szg2RWR1UFJlcFZMM0NWVzhWSTdPZVZPbTN3RVdoeFlPSUdNUFkvblE2V1pGMWpzRUx3ZjcrMjVSaUFIWm43d2JBNUZTYVFtVldaRWt3ZkNmRXZCakZnU1RGUGl3eGlvNWRDY0d0VUNoT3NaOWJTZ3R2bkVxcVF5OGtzc1NDNkpvRWYvWVRYL2xOYkx4dURvMVVvQzdNMGtmWjY3TjZiV3Q4YkNnZGV1QkE1NmpZQkdRS2RFOGN4K0h2ZmU5aml1d1VEWGFKdnVUdDl6eGpabDQvNFNSand6TlNrZ2NRWklJY0RmSGpnWXdRa3FRMHpzbis0L2tHakFoSXRUV2hnT0Jnam9vNkZhV2FKYzNCRHFmS2cxMWFlUmpkdEdoZ05nR29vMjg5VGdEWGhYR0x0d0tuNi9EZnZzY0N5TkdmdndjaittSi9FaUJNUU5IUlRJbGloQmNyUjBjcGFSWEVTQ0YvaGFNTWlyU01XbGNoUVZ0ZGlBcWNCWERobGgvSGhiWjYwb3NGTHdLSXdYRUZqaFBBV1FaVWhUU0dnQWZ5NHp5TTk1N0pDcjVyUG12T0xxWG51dUdtUi9JbEF4VlpYNTJpZGw0QUxUQ015bUJZbnBKV2xKR0ZsMHFqUGI1c0hnaW5nbWpWd2M5WUp0UW9DOFRNV0Z4OXFNQmxFR01CTVVheVJiaWx1eDhtd0FGb3lkS1JMMkxzNlcvMm8zRFVPM0hVK1gxVkJLVjd2MVNYZ0pvcHZxUXREU0tqa2RTMFFna1IwL2lnd0tLUVg4cExEOVJBaHlVZ1FDSitkcFlLWVZVcG1ZVk5aaDVHMzNyaytkYjBtVmlxcHBZMTY2ejRVNlY5N2E1Ujg1a1dtVGhxQ29oV2JZM29FTlRMU1lsc3hERmZFTDVRU3pWUHFsSTNJdm9ycmNDYkVwTW1rVFFNdWc4ZHo0OTk3UzVBVGhQZ3ZmNHh0UXQzZk42dGYxNm1kdGpDWmQ1cnhYQndsQm8vUEVYaUZDczhSWU9YYUpXYTJISnFpdFVvVVh0SVpJb0hRQ2JlSkJCcHk4WDNGT0MrSW5vVUZnYU95Wm51d2YwTVVJb0p3OGNsQ2tyRXhKU1RZNFBvRS9tMVZJdHFvaTZ1QUxGdGxpVmpWWDVuMmZrdkVUS09xRW9KR1JRRDNaa1FyeUNCeGlScWFBeXkyYnVBL2JNZ1R5YzI3Q0xFSUR2MnFhK1o3T2lEeGxxRFlpUzVPc0RsZ0lsL2NXMWlLaytSczRLdHdULzl5dG9UclJWL1dzbVBKVUtNQmxOcW1PamlEMG1xMTRKRjMrN0tTRkFqYVI5V2dwdEZOSkxGdEh6REFTRlgzSEhSLzF2Y3A1SzcxNnJPcjFZZXkwSFhMRm5DVU9kRzMrZUhLMGNyVkZIMVU4ZDByeEt1SDZvdlMyRUdrODN0QlNDNDhtMkpPYTBjL1BxL3RRQXlkR1p1RUVrSVNUUUFKM296Zk9RRjF1aTBWZzdLaTRnTGVEMmxjN0E2UmlQNmR6dzRMTlk1eTMxc1FMU0lsQ3V1UkhNUy9ERUxFWFdPR052b3dPeU0rdDRvMWZ6bkNCeEozSG9TVDc0ckRITXkyQjlkNmNiVlFOSTYwQ3BOckFwb1RtTlQwRmdyMlN6c3dsYy9BNEM0ZVZaUFh3NU1UM3ZSWWVtZWo2Ty9GTWJaaG90d09aRG4xY3luV2orT1JnQXFvaWJEVUNmL01jZXFCMmxBV1NvUmRrMHZybTVnb1V6RlNUMDJpc3NLMktLekJ1b1BXanhHb3ZaK29wb1ZnK1ZMTlBBNmZnQnFaVTVSNjRhT3lYS2xTczFoR1d1SmpOZ3NMWWFoT0VCVnhDWnFUR0trYytpaGtTUFgzZVJ2MGZUREJCalRDZ2hHRjYrL0R1MTl4MkJhU2ZpSjNyK1k5MUMxTVRJMms0VzVHNkZCakZxOEFaVDl3V1ZxZHlndnBuQW1hUFJVbGorckpBV0tHNjdsQlJiTFN0MUFGK0NacldBL1NVWUxOSXhhNml5ODN1V0tkYXk2TTdRNlNiR1ljZXhsVWkxSkNYViszbU5CSG9HZVlpeS9scWRKODFveGdPWDlEWjkzUFlneGxNU3FHTU9zTy9QcG84QXlKcSsxeGFvNnZXdG44bHA3OHVUSkJmUU9mMEtzQVNSVmlBM0hmMlVBODRocUxhd2prYzJuWElyeEU0aTZjYXptUTJPRURNRW9QZGM3OThxeVErcTlQSFhDOXd4V2NJRVFEYUVES3Q5Z2R4ODQ0UHdyd1JYcjNETk0xZHhoS3B3dDRWZ1p4bTM3akFLS3FqMElxUHpQUlpPOTVvQm1rTFFKU01PZ1B5L214TmYvd2EvUjkzdmw4R0V2MUtkcFNaWnUvYWhreHdGSnBKZ0c2bzhsNjZNNDdieDZFeHJIb21wT3EweTdVdXRscXZwRHBUWTlnQnJQYkl6bVZERHVrWlhCQnJRVk8xdFpXWnhuVkFjMUJRZGRUakVvZnJlVG9CS0Y4d2pDOTJyY00xMGNlaG4zUjBjZ3JtcmtxL2ord2FZK0RwZ0hOQmRBeENRTkZkT3c2TzYvcnovNzhldjkrOXZydm5HQU1lMEFZZStoejk2QS91eTlzS2tGNEVyQ0llOVdOaDVFVDNmeHB1TjlWcVAweEtpQWQ3V21jZGFFbGlJTmxhZE5sdnVnMW1WSnFYeXBUcy9ZTkp1bUdEQU5NcVlPTlVMS2pFZ0l4Z2RNVkM2T29tRXVRc1psT3RhcWk0RlJvN3hQNzNLS2NaR1JtUkVCNVdjOVNKSlNUS0lHT1JxZCsvNFhnQTYyNzdSNFpLTU1yN1VBdWpKLzMxOGJrd2lNVlJoYnBZdGV4MDhoTDF0RXk1TUNVVXV2REk5enFYMVdsU0EwMW9rTE14cnIvYUxsakN4RkJVaUs3T2NxVzh3SzFFcFpkcHJ0dk5hNWorTGt5N2h6ZjRBZkxpc0dsdDBJZzhQZnlwK3RBNkxFUUZWVzA0VWpWNnJMQ0RxYXhqREVwb25wUGRUT0QxejNFYzlsVkNleGZPTUFUKzlRZ05LYy82Y1BtTTZESjhXMGt1ajhDNE5lRzNEOXlsUlEwR2J4bmxTa05zVUFtbFRVdkVpVmF6OUt2OUVvWHFuSnFCS1JCMVVLWDRGWGx2VWphaktTUWhGYlZEVnFIMVdQZkRWZTZXcEtsQnl2dkZyZmtRWVVqY3BRVWYwYkpiVlpWbFJTYlExUUlPdUlKQTJJYVRpeFRVSG5vWC91ZGgvY2g4bmROdko4ZkZQVmhKaWNOdTNqK3crYmhkdisybGhqQkttV2FSb2F4Z3RMaFBBSWFNN2EvTW1DeEtpbDZPaHBWRDIxVklMS0tlVlVXVG9NZkV3REViQVNFeHhTUU1UVlNZVzR4N2syU3pNOGtEWFBzZzVNQVNyTmhxeHFlSzJiSXdaTHhlckJDdnU1SXFCMS8vRzhEekNIYlk0Q0poVjBaMVJuYnYwRFFNb1M5NUVHR0pqZTRadG05bjNxZDdHOGY0SHBpSUZwK2xNeVJJQitSNUIxNEJ2U1ZFb2pTbmtzbVVpTjV5c0JSTlJQVTNYU25RYWxGa3hZSWJQRlBtdEVOMldGQUZhRzBqd1JHeS9xaUxhWUg2ZzFWckhlMUY3am9pVmkvcVJpeEZTcWtpdDJyaFphY2ZpakJVSVB1Q1R2d3FUREZOdHdZcHFXSjcveTJlN012M3dlVTJvOGRubzBBUVlVMjNiYVhtL2ZnOG5DTFg5aGpCZ2tOb2VJZ2N2OG5XalBpdmRyR1paN1pQRjB4MDl5eVU1RlZsbEdqcEZLZDY0VEF0N1haR29UYVRIZzhTb3NLMmRNUmdOR0I2ZTdhbXdQcnR1Q0I2dUM0cnBLbGtucll4NjBPSEFqMVAxU201ckRFb1NKUnZxdnErcGgxd1BVd2JSR1FFbGd1Z2VveDc3NEhrQ0FYVHRPMmFjZW1iQzlkNWVDRkh2NHV2Zko4cjZUa2d4YlNkS3FKc296WVBrRUtxdHRURld5SWdmVXNjWlR4NVNqUDFNV1VTMk4ybmg3eEY2ditnQ3dLajJlZVlEVFJtbGJaTDBMbzB5VFlhZ2I2Z0FvTG92SzFGNFFHVnB6WFpSQkxFYzRhTlZjUUMzWXJPcHpETDliSFpCMUlLMVJ3RFRVSm1saUYyLzl4M3pwOHpkNFFEenRIbHVBQWNXT2FkTnU3ejhzODdlOHh3Q1c2WkJET2t5NEhMQ1c2QzBCblhtQnRYRWJTOVViSzNWaXVtNFZyZzNDR09CNWkzRk1HbEY4ckZPRWcrVEptYm82U2t3UklXaU5oZnFvMUNzNkRsVHJEZHJLYUtiVjRMbEJBek12SzRSY2VjMVZUNTJ2blhVcFltQ2JvNFJwd2ZUMnRaTmpuLzEvQUFxbTd6anQxVDl5YTRwSDFDWi84RVB2eCt5Tk40cTBVa21iQ21PQlBQTmVyYVhqUkwvcjkyTTZBTTVVM0hPeEQ3RkNuS3FlWmxSV2U3T1dJQ1lhamVna0ZuTnF0WFNwV2dXZWZDV29Tb1QzRnFmaUNna0hTdFZGQUZFcnc3SVdUMExFdVpjMHJlSTBXU3orSTlHcWxiRFMvZmU1SHBDM1lZYkhJV0kxTmM2YUUxLzR2WVdGTys3RDVQVERIaHB0SHRXRGpSMENrVnptUHYyVHR2dFFMbllZMGhyem94a0tDRHQvaE5ETXAydlZhdGhYZVpFUmRzTEEyQUtOVEdRYUV5VEZaSnA0cUpyaktZSUZWd0JHRDZPeW5KMzJUNUdtSTlJL2ZzZ1lTWWtWY2g1bzAzRUZDY0phZWxaWFBmaWxVWThDellIdU1reGpSQ1J0T1pNT1c4N2VldS9vZzMvMVB6QkY0eGZmNlYrUDhuREthWWZYWDJ2ekkxKzZpYk5mK1EyWUpJRWRjbWl0SVp5cjVpak1IUTRqK1F5aW1ZeVZDejhPaEw4SVU1ZlE0dUdlOGZsOThYUzhXcXJEd0Z5cU0rT2k0eTBUc1FaYmpqQ1N5SGd1QThwYW5HMmllWmF4eW9ZS2hHcHRTNHIyNURCQmlFcjBsd0VqTU1OclZkQ2tkQTZETTllOTdTaGtPUUFycmxDQWdYRGlwWFg3L255WHpIM3BQMHphU2swamRkSmE1ZE9JZ2FEZkJtWWZpbnovWlUxWHlXSDFHVkhoNHlYTktRUElOVkNiV3UzdkpVSWRuTUI2aHE5MkJxbHp4TEZOTlZKNVhPU0xPaVdiU08xN0MxbFFpeFN0RmNMR0FPQ0U1d3ZFQ3NSYUlHc0w4aTdzeUZwQVVtZEVFeDY3N24yOUV6ZDhGbmhwY2pwZ2RXWUJCdUEzZEZGNzlCOS96TGJ2V3hJemFreHJuSklNZVNuUkprUnZtVGl4THh5b0tFV0tIbUM4bERVQ1hSSHgwMW9mc0ZhUklhenRhWXhLSzZ6VWFlMERuWStNM1NlUlM2WEdOTVZwT3FZV05hb2dTa1NNOG5oVmRYVVVuZmNoRFN1bTJRVDd5MEIzZ1daa3JValNjaVlkVHMzczU3N1FPL2kzNy9LTTFkNXZXalk4eGlQZWR5bndldHMvZWN1ZGN1eXpQMlhjb3FWcE9obmRTTEVOd21VQzJ3QjZTOFNKZllCSkVFMlNaYjErVlo0V3R0YjZsaENaL0xRKzB6RTZmV1ZGaHBHV3ZuZldSUlRHM2ZZY1FNVTFZWUoxRkJ6NW9Hcy9vd1JWTEZkOTFxTnROWkdNam9QZFpXRHBPS1ExQWRNY2MwakdyTXplTXFQNzN2OG1pSEVCTmZOeENuRFlqNjk0YTlvNzlFOGZ3Y3huZjlVSUdtS1FZWHhqcUkwN0FtTUZuWlBBeVFlQnBDSGxDaWpGYngxWXJScWg2MmdlSXgxS3k2NUdUV3VNeUFUVkZSUDh2WW5jbllweUM5cTE5dDQ1aUlvcmh3YWRRSjBwcjhrNW42SlZBWmY3L3dZRkxpZXlIdE94RVRUR0o0RGxSZWpzQVVqYWdoMWRBNUdFYWU5QmxabC9uZXoxZXZlQ3I3TVBoNXBYTU1BQWJ2NWdqa25hYk4rSGY4WE9mdTRmYkRMYWhHM2xXSDFPdUNFWllKdkEwbkZnNWo3Q0pxaTdGQ01wVFRGWUNnVzVzSkFOSTJlRDAyZ2ZpMUw0Q2dSNEpDM3RZWXpTYnp6Mmw5WEJJTkhXRUtkbHJYVmFocTZEQWw4RUJGMVFsUDArakRvWjNyQUdqZFdyUmRzTHlHYjJFVWtMZG53enhEVHlCTjBFUno3MXM5M2oxKzNGdHFsdnV1K3VYSUFCWWxvVVV6UWpkLy9XbStYRURUZWFkS1FCTzVSajdmbWhmbk9BYlFCTEo0QWpkd1d6UklMYW5HVXRpTitJS0NnRjkvaDhJV1d0aGFOa2tiQmlscDJzSFNOYzRwVGgyNzV1bDVyQ1ZCdU9XckpRMVFPcnFGTzJoWmpTN1VxamxYRHNyQTFJUjBlZzh5ZlFQM0FmUkt3a0Uxc2h0cGxiazZaeS9JYmY2UnorUDM5OHV0TkZIKzhBKzRqczJvbVRNQXZOdTM3emxjbmNGKzR3eVZBaXRwSEw2clA5S21ibTU1NTJGNEFEdHdxeUpTL2J4RUsyeHVwUnNUY1ZOMU85Qk9kWHJFWWtRRURYNFRSTzExK0JGSjFCTkFzWnc4V2VzbWcydFF2dnpRWHc1SEFxNWVpcUtibWFFK29FemtIVUVWa2Y2SFN4YXNNRTE1eTNTVXlhb0gvMEFEcjc3d2FzZ1ZsM0hveHQ5Rk5CYW85KzluKzE3Ly9BZjhQa2JvdTl1eDQxRjJ0V0JwWHNVdUIxZGdIbVpPL09QNzdhTE41eW42U3JVMG5IY2xsN1FTaDdNaUJwK0ZMbjRGZUJ1YU5BMGd6bWQ2M3ArM1hEUUV6U2gxWGtndHBTazlwMFJZaU9GRGpOUVNHMU1jbkZZUnVzMStMeFhCSVhTNFUxTHBxZHJqUXNzUEdpelJ6YnNnYTVVN1FmMnNmT1EvY0RqUkdZZFJjQXRwVWxTZG8weHovN2llWDdmKy9ISWpLRDM2SUFGeGFmbHlUQTdFRjczKzllamZtYkhwQjBMSlhHU0c3V1hlVEhITHErd0ZyQ0pNRE1QY0NSci9uYlpCdmhWSkdJQ2RMQ3crWHFZMnJVU1cwNmdFYWxpcTVBaWdhaXNWU25LWldLRmhXTmVPYVNaVU04Zk1hLzEwTGl6REtnMDhQRXBsWFk5SXl6a1k2TlNuZXhMUXQzM3M3ZWtRZEZ4dGZEcmp1UFlvZnl4Q1NwSE52emowdjMvdkVreUJ5NzVERWJ6bFl3d0FDd053ZGViM3U5bWZzYUQvenFWY25zNSs4eHlYQXF5VkRmYm5pNmlFa0kxL0ZuMkRkYXdOSUp3VU5mOWtwVTJ2SWYxOExwb0ZVWkVpTm5GQktiT3cxUG5KM1pYa01hSzg1QSt6N2QwOFVITWNhZEJ3eG5yWVlITXlCanVOQ1k1dnkvUlluY0FlMHVoMW9wenJwMEM4YTNyS1Z6Qmt0SFozanl0cHZRbno4dVp1MVdKQk5iWVUwcmJ4aE56ZEgvODVIbGUzL3Y5UkRURG0yMmp6azFyWENBaTVVOGFidmQzajV6MS90ZVptWS85MFZKeHBwSXhqTFpjQW1rT1FIa0hYK3YwcVlQMHBHN2dNTjNFbmtHTkpxUm8wRWo5YVlnRnhDbFo2M2t3OEFTOFZIYWQwaksxQlRONUdVUUVkRzdicDVaTWlWNUVxOWNqWmczbFhvRkVKWjhmRHhEcm9KMkR3MGhObDI0SHB1ZnNRbTIxVVRXeldYKy9uc3dkOGROVU0xcE4xOENPN2FCSWsxTjJFbHg4Ry9lMzNuZ1F6OGFESDdtVE5tYkJJL0x5d2U1ZzQ4ZHdGMi8vdkwwL0xkL3hHeDh4V3ZWakRpdXV3aHU3b2pGd2tPQXNZUkpDR3NGeXllQjVWbGd6VlpnWWlzaGlRVFFSTkJXSjM0aEhFVkRHM3hhTG5UZGhYSDJ3Y3JMcXJHTDhmVFpZbzQwU2RtekIxWkU4dEwxVEY3eGdYL1k5N0tmMm5WelY1cERMU3BQc3g5alVNNlUydWZVQWIwTWFRcE9uTDBhSTJ2SElNWkt2K2ZZUFg1UzV1NjdGL244Y2NqRVdwZzE1OEhZVm03dFVHSTZCNkVQZmZxWHVzZi83amNBZjQ3aVNsQnpqMU9BaXlEREFHWXhlK0RQWHRmSTV0K2Ridnp1OTdqR0JuRFZscHpENHdsUDNDUG90NGwweUFNd091RDRBOFRjWVdEZGVjRDRKcCtpWGI4NkJyNFluUTlsMmRsSXBaQmdOK3NmM1Q5M1VYcjJxaGtBQ3lKUzNxRGR1MmwzN0JBWGdtdkM1L0w5YzF6VEVxeHQ5NWFYK3NDN3p0c3l0RFpiN3JUdHV1R1c4MGVjUjIya3JJeUNOZHJWZWN0dVA2TzFSaVkyajh2RXBuSGFac3FseFo1MFprOW81OUJEMHAwNTRQdE0xNStEZE5WR0ZkTjBTVHJhbE1YYlo5Mit2MzFiZC83bWFYOUV1NndRYS9PNEJyZ1VTQVZUbFA0dStkV2gvdjZ2eXNiWGZFQkducjFlQ1NjYm4yV3djTWh3NGFBUG5XMEM2WkRBNWNDUnU0aTVROENhOHdSamE3d29sZmM5bWhZTlkvMjE2S1lRSTRCcjk3dVM0bklIZkZKRWVIU0p6K25tMkgvdWhNenUyT0VibDBpT2lNanlrUTR2c0ptN21wcS9FTVk4djVrMloxMk9TenU5L0RNUUpINHZEVE1sTkJ4V3BWb0Z2Q0JBL05ZQW80cXg5ZU15Y2RacUpNMkUzWVZsZEE0ZncvTGhBK2lkUEM3SXU4RFFHTXphaThRMng1eXhSaEpvVXc5L2VrOTI0RVB2N1BlWHZnWk1KWmpla2E5a0FCN3ZBSWM2V1lodFUwbG43NjUvYUI3NzNDMXk0Uy8vV2JydUJhOXdtb0NyenNuTjBCckx1WDFrZDA1Z0RHQWFoQmtDK20zQndkdUlvWEZnWWdzd3NoWkltOGEzc1dxRklpbzFpWDBIbWtYM1E4Y1c4blYwdUhvOEJXY1czTTFES1M1czl6QTNzNkR0bzNQNW5ZbnFUK1NLWjRwSlRETUZlakNBQlF3bDkrZzNQazQxTnVCRnRoeUJ3Q2F3clJiRzE0MXd1RWtzSHoyRXBjUEgwSnRmQVBzZEFYdUFUY1NzUForeWFndU5UZkxVdGxMYmVURExqdTdkMVQxMDdhLzdIelpwZ1YzNVN0LzhKeUxBaGE4ckJ5WnREeDk3QVBmOTJ0VWorWnYrYjdmMkplOXhyWE5HWWFTbkd5NUoyRjJ5bk44SEgrZ0dZUnFBVFFUOVplTHdIYjV1SHR1b1dMWEpvRG5tR1RGMVFML24wYXFsemJMKzhuaUMzMjAyN2RhbFJjekNZSFZpekRaSmdZWUJzaDdRYXZuRjJOZGNTZWZ5TnFpa3VsWml0WWhtN2pCd0tsbG9hamRlTFJPQndJbk5PNVRPUEJidnVoZHppeWZCZnM5enNRa0JheUROTlREalcybGFxOVNLV0NzMjVkeVhiOFBodjMxN2QvYXJYd2pIMjVsSFF6OStld1k0M3BlbmlPVmQ4bnZwb1UvOXF6bnZMWDhxYTU3L25acU1BczBrbDQzalJqcHpSaGNPQU4wRlFxd2dhUVNTSkFQbUhoTE1IU1RTRWNISVdtQmtOWkVPaVRSYVFEZnJyRnU5YXRRMjdJWmNnYUVock81MmZYamFIYTlPR3dCejg1bExrc1Jhay9pUlFCWndmV0lrQVVodGd1S3RTS3FBVGNJb3FOdy9IYjBsb05jVzlKYkEvaEx5ck92UnRERitpb0gxRDRHMDFzS3MyZ1RUR0ZOakVoalRzTkxkMTlmWkwvMStaOStIZHdMb1lOdFU0azNZSzZWem52cXllT0pmeE41ZHhMYXBSTy8veEdGMzh0OC9nbjd2cUUzVHk1S2g5V3VBVkpBa3VSbmRaRXhyekhmUVpjdmVUQUFoa3FhQnRVRGVGN1JuZ1lWRHdOSmhjSGtCSTBQT25IZE80MU5iTHpoN1p1MFFibGVMMlVZVGtoak1OaHBZVEJ1WXN5a1dtMDNiYkRUa1VKcGlJVWt4WjFNY0h4dVYyUlRZcy85Z2R0dTFINzluRzFNbTZIWUVuWGxnNFpCZzlnQXd1eDlZT0FwMFRnajZIZDlLWS8wcFdwQmNZSnVRMGMxSTFwNFBPNzdSbWJURnhMYVNKRHRtY1B5TGY1L2Q5eGMvMGp0NS9WOERrZ09URnZ2K3hEM2VOMXZ3TFgxTkdjaXZhbUI3VmcyZmZjMDd1UHFLbitYdzB6Y3FMS2k5bkVyRHJHTzBQUU11SHlPeXRwKzFaVkxDcEdFd214QXVRMktNTk5qTGsxWjZJRitZdTZrMXNhbjdsamMrZjBLWFptLy81TWZ2dThVT05UQytibFg2WFMrKzZGbWYvUGh0TitlOUJkbTBwdFc2Y3R2RnI5Nzd4Y1BKamRmZmVmU1N5elpjZGUraDNvVjVub2ZSeU9GSUYydFludGZrMnpZbEhEdEVORmZEakc0VU03UmF4VFRVR2tpU0psYjZKNEgyM2RmMWozem12YjJUTjEwSEFCNGw3MWd4bFB4dEh1RGlQVXdheU4rNUVPak56YTF2L0JtWnVQekhPSHJ4T3FXQjVsMEhLaW5Pc3JjRXRFK0MzWGxCdHV3Umxra0JZd1UwQ2lURys3TUZRZ2RxRHVUTyt3S04raWtBL1I2UXRxb2pheG9wQUF0Smg4SGVNcEE2K3A4bnBZVUdESUtHcDFhTk5NZUI0ZFdRNWlwS091cU1DQ3drRVJHWS9tR2EzcUYvNlIrLzRmZDdNLy82S2Y4czAyRFhUanhTSGZjL1U0Q3I5N0p0eW1MdmU4S1lXMnhxYlAySk55VVQ1NzlaV3VjOHc2V3I0VndYcE9jajFZbEJ0bXpRblFQN2JUQnZpN2dlNEhKQ2NvRGlpTVNMR2NaQUpKUlVSdjBvTlRYQlRrU2hVc01SQ3hUa2xwcFhJNUhFQXNrb3BEbXFwamtxYUk0UmFSTmlVaHBqVk1qVVNBcnJsc0QyZ3dlMWMreWoyWkYvMloxM3ZucFRhU3FVSFk4YmlIb3lCYmg2VDVPN0RhYXZLVHJBV2hNYlhuSzFycm55LzNMTnpTK1Q1bGxyTkZtRlBIZUJzNVRjbnducERGM2YwT1ZnM2haa2JTRHZrNjd2bFN5WGh3TXU4L0lNYjhBV0J6YUJKaUdTRkNJcGtEUWc2VEFrYVFtU1lTSnBNckZ3L3N3OUpHSmdERktJTGdPOXc0dm9IUDEzWGZqNjd2YlJ2LzhuQUxQZkRvSDlkZzV3ZlVYLzI2L21rUXk0YVdUcjVNdlkydnFETHQzOFF0TlljdzRiRzZDU0FpQlVNMUM5YUV4ak5JeWtFZ0ZGMUo4bUtXRXNNbzBmUE9USk1GT2NMRTR4NGlkdUtndlFiY1JZcEVJSU0yaDJFdUs2KzlFOStCL1p3dGMrS1VjL2ZYMFgzWDNWYm5PdDlYNnBKellWUHhrRFhOK2pKeWNScldvQUdCOGF2K0lTTi9LY0YySGtyTXNTS3k4d3piR0wyVmczaEdRY1NodUcxa3M1YjQwSUl6YkZ4OU13RE0wdEd2Nk1RQmgwU3MwQWRzRGVuTUoxdm83cytMM3N6Tzd0ejkveHVYeng4M2NDbUsrd0lvMzNKMDgvWWVEcFAxT0FUeC9zajEzakJ1ZGlyUUl1d0xyTFY1dnhaMTZaNTltNVROZTE3UENHOVZDZVEzQTFJQ21vRmpDa3dQZ2hrWEJRS3JUWGhlQ2c1TDFEenMwdUdIQy9zbjBianQ5d2JHbnA4RDFlLzR2ZnhyWFdEenJadTRKKzNhZGVnOEcyMkRhVllKSzJmaVQ3d3o3TUJ0NjBrUWFTSncxY3dEZVdUY1g0VlhyRlcxTlBLVDU1RnNiL0Izc2pZWVV4OWFXUUFBQUFBRWxGVGtTdVFtQ0MiIGFsdD0iRmFjZWJvb2siIGNsYXNzPSJvcHQtaWNvbi1pbWciPjwvZGl2PgogICAgPGRpdiBjbGFzcz0ib3B0LXRleHQiPjxkaXYgY2xhc3M9Im9wdC10aXRsZSI+RmFjZWJvb2s8L2Rpdj48ZGl2IGNsYXNzPSJvcHQtaGFuZGxlIj5SJmFtcDtKIEdyb29taW5nPC9kaXY+PC9kaXY+CiAgICA8c3BhbiBjbGFzcz0ib3B0LWFycm93Ij7ihpI8L3NwYW4+CiAgPC9hPgogIDxidXR0b24gY2xhc3M9Im9wdCIgb25jbGljaz0id2luZG93LmxvY2F0aW9uLmhyZWY9J3RlbDorMzcyNTg3MzU0NTYnIj4KICAgIDxkaXYgY2xhc3M9Im9wdC1pY29uIj48c3ZnIHdpZHRoPSIzMiIgaGVpZ2h0PSIzMiIgdmlld0JveD0iMCAwIDI0IDI0IiBmaWxsPSJub25lIiBzdHJva2U9InJnYmEoMjU1LDI1NSwyNTUsLjQ1KSIgc3Ryb2tlLXdpZHRoPSIxLjYiPjxwYXRoIGQ9Ik0yMiAxNi45MnYzYTIgMiAwIDAxLTIuMTggMiAxOS43OSAxOS43OSAwIDAxLTguNjMtMy4wN0ExOS41IDE5LjUgMCAwMTMuMDcgOS44MmExOS43OSAxOS43OSAwIDAxLTMuMDctOC42N0EyIDIgMCAwMTIgMWgzYTIgMiAwIDAxMiAxLjcyYy4xMjcuOTYuMzYxIDEuOTAzLjcgMi44MWEyIDIgMCAwMS0uNDUgMi4xMUw2LjkxIDguOTFhMTYgMTYgMCAwMDYgNmwxLjI3LTEuMjdhMiAyIDAgMDEyLjExLS40NWMuOTA3LjMzOSAxLjg1LjU3MyAyLjgxLjdBMiAyIDAgMDEyMiAxNi45MnoiLz48L3N2Zz48L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im9wdC10ZXh0Ij48ZGl2IGNsYXNzPSJvcHQtdGl0bGUiIGRhdGEtaTE4bj0iY2FsbF91cyI+Q2FsbCBVczwvZGl2PjxkaXYgY2xhc3M9Im9wdC1oYW5kbGUiPiszNzIgNTg3IDM1NDU2PC9kaXY+PC9kaXY+CiAgICA8c3BhbiBjbGFzcz0ib3B0LWFycm93Ij7ihpI8L3NwYW4+CiAgPC9idXR0b24+CiAgPGRpdiBjbGFzcz0iaG9tZS1mb290Ij4KICAgIDxzcGFuPlRhbGxpbm48L3NwYW4+PGRpdiBjbGFzcz0iZmRvdCI+PC9kaXY+PHNwYW4+RXN0b25pYTwvc3Bhbj48ZGl2IGNsYXNzPSJmZG90Ij48L2Rpdj48c3Bhbj5BbGx2ZWVsYWV2YSA0PC9zcGFuPgogIDwvZGl2Pgo8L2Rpdj4KPC9kaXY+Cgo8IS0tIEJPT0tJTkcgLS0+CjxkaXYgY2xhc3M9InNjcmVlbiIgaWQ9ImJvb2tTY3JlZW4iPgo8ZGl2IGNsYXNzPSJjb24iPgogIDxidXR0b24gY2xhc3M9ImJhY2stYnRuIiBpZD0iYmFja0J0biIgZGF0YS1pMThuPSJiYWNrIj7ihpAg0J3QsNC30LDQtDwvYnV0dG9uPgogIDxkaXYgY2xhc3M9ImxvZ28tcmoiPlImYW1wO0o8L2Rpdj4KICA8ZGl2IGNsYXNzPSJsb2dvLXN1YiIgZGF0YS1pMThuPSJsb2dvX3N1YiI+R3Jvb21pbmcgwrcg0KLQsNC70LvQuNC9PC9kaXY+CiAgPGRpdiBjbGFzcz0icHJvZ3Jlc3MiPgogICAgPGRpdiBjbGFzcz0icHMgYWN0aXZlIiBpZD0icHMxIj48ZGl2IGNsYXNzPSJwZG90Ij48L2Rpdj48c3BhbiBkYXRhLWkxOG49InBzX3NlcnZpY2UiPtCj0YHQu9GD0LPQsDwvc3Bhbj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBsIiBpZD0icGwxIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBzIiBpZD0icHMyIj48ZGl2IGNsYXNzPSJwZG90Ij48L2Rpdj48c3BhbiBkYXRhLWkxOG49InBzX21hc3RlciI+0JzQsNGB0YLQtdGAPC9zcGFuPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icGwiIGlkPSJwbDIiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icHMiIGlkPSJwczMiPjxkaXYgY2xhc3M9InBkb3QiPjwvZGl2PjxzcGFuIGRhdGEtaTE4bj0icHNfcGV0Ij7Qn9C40YLQvtC80LXRhjwvc3Bhbj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBsIiBpZD0icGwzIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBzIiBpZD0icHM0Ij48ZGl2IGNsYXNzPSJwZG90Ij48L2Rpdj48c3BhbiBkYXRhLWkxOG49InBzX2RhdGUiPtCU0LDRgtCwPC9zcGFuPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icGwiIGlkPSJwbDQiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icHMiIGlkPSJwczUiPjxkaXYgY2xhc3M9InBkb3QiPjwvZGl2PjxzcGFuIGRhdGEtaTE4bj0icHNfZGV0YWlscyI+0JTQsNC90L3Ri9C1PC9zcGFuPjwvZGl2PgogIDwvZGl2PgoKICA8IS0tIFN0ZXAgMSAtLT4KICA8ZGl2IGNsYXNzPSJzdGVwIHNob3ciIGlkPSJiazEiPgogICAgPGRpdiBjbGFzcz0ic2xibCIgZGF0YS1pMThuPSJzdGVwMV9sYmwiPjAxIMK3INCf0L7RgNC+0LTQsDwvZGl2PgogICAgPGRpdiBjbGFzcz0iYndyYXAiPgogICAgICA8ZGl2IGNsYXNzPSJzYm94Ij4KICAgICAgICA8c3BhbiBjbGFzcz0ic2kiPvCflI08L3NwYW4+CiAgICAgICAgPGlucHV0IGlkPSJiSW5wdXQiIHR5cGU9InRleHQiIHBsYWNlaG9sZGVyPSLQndCw0YfQvdC40YLQtSDQstCy0L7QtNC40YLRjCDQv9C+0YDQvtC00YMuLi4iIGRhdGEtaTE4bi1waD0iYnJlZWRfcGgiIGF1dG9jb21wbGV0ZT0ib2ZmIj4KICAgICAgICA8YnV0dG9uIGNsYXNzPSJjbHIiIGlkPSJjbHJCdG4iPuKclTwvYnV0dG9uPgogICAgICA8L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0iZHJvcCIgaWQ9ImJEcm9wIj48L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0ic2JhZGdlIiBpZD0ic0JhZGdlIj48L2Rpdj4KICAgIDxkaXYgaWQ9InN2Y1NlYyIgc3R5bGU9ImRpc3BsYXk6bm9uZTttYXJnaW4tdG9wOjE2cHgiPgogICAgICA8ZGl2IGNsYXNzPSJzbGJsIiBpZD0ic3RlcDJMYmxFbCIgZGF0YS1pMThuPSJzdGVwMl9sYmwiPjAyIMK3INCj0YHQu9GD0LPQsDwvZGl2PgogICAgICA8ZGl2IGlkPSJzdmNMaXN0Ij48L2Rpdj4KICAgIDwvZGl2PgogIDwvZGl2PgoKICA8IS0tIFN0ZXAgMiAtLT4KICA8ZGl2IGNsYXNzPSJzdGVwIiBpZD0iYmsyIj4KICAgIDxkaXYgY2xhc3M9InNsYmwiIGRhdGEtaTE4bj0ic3RlcDJfbWFzdGVyIj7QktGL0LHQtdGA0LjRgtC1INC80LDRgdGC0LXRgNCwPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJtYXN0ZXJzIj4KICAgICAgPGRpdiBjbGFzcz0ibWJ0biIgZGF0YS1tYXN0ZXI9ItCi0LDRgtGM0Y/QvdCwIj48ZGl2IGNsYXNzPSJtbmFtZSI+0KLQsNGC0YzRj9C90LA8L2Rpdj48L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0ibWJ0biIgZGF0YS1tYXN0ZXI9ItCQ0LvQuNGB0LAiPjxkaXYgY2xhc3M9Im1uYW1lIj7QkNC70LjRgdCwPC9kaXY+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9Im1idG4iIGRhdGEtbWFzdGVyPSLQmtGA0LjRgdGC0LjQvdCwIj48ZGl2IGNsYXNzPSJtbmFtZSI+0JrRgNC40YHRgtC40L3QsDwvZGl2PjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJtYnRuIiBkYXRhLW1hc3Rlcj0i0JDQvdC90LAiPjxkaXYgY2xhc3M9Im1uYW1lIj7QkNC90L3QsDwvZGl2PjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJtYnRuIiBkYXRhLW1hc3Rlcj0i0JDQu9C10LrRgdCw0L3QtNGA0LAiPjxkaXYgY2xhc3M9Im1uYW1lIj7QkNC70LXQutGB0LDQvdC00YDQsDwvZGl2PjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJtYnRuIiBkYXRhLW1hc3Rlcj0i0JrRgdC10L3QuNGPIj48ZGl2IGNsYXNzPSJtbmFtZSI+0JrRgdC10L3QuNGPPC9kaXY+PC9kaXY+CiAgICA8L2Rpdj4KICA8L2Rpdj4KCiAgPCEtLSBTdGVwIDMgLS0+CiAgPGRpdiBjbGFzcz0ic3RlcCIgaWQ9ImJrMyI+CiAgICA8ZGl2IGNsYXNzPSJzbGJsIiBkYXRhLWkxOG49InN0ZXAzX2xibCI+0JrQsNC6INC00LDQstC90L4g0LLRiyDQv9C+0YHQtdGJ0LDQu9C4INCz0YDRg9C80LjQvdCzPzwvZGl2PgogICAgPGJ1dHRvbiBjbGFzcz0iZ2J0biIgZGF0YS12YWw9ItCf0LXRgNCy0YvQuSDRgNCw0LciIGRhdGEtaTE4bj0iZzEiPtCf0LXRgNCy0YvQuSDRgNCw0Lc8L2J1dHRvbj4KICAgIDxidXR0b24gY2xhc3M9ImdidG4iIGRhdGEtdmFsPSLQntGCIDEg0LTQviAzINC80LXRgdGP0YbQtdCyIiBkYXRhLWkxOG49ImcyIj7QntGCIDEg0LTQviAzINC80LXRgdGP0YbQtdCyPC9idXR0b24+CiAgICA8YnV0dG9uIGNsYXNzPSJnYnRuIiBkYXRhLXZhbD0i0J7RgiAzINC00L4gNiDQvNC10YHRj9GG0LXQsiIgZGF0YS1pMThuPSJnMyI+0J7RgiAzINC00L4gNiDQvNC10YHRj9GG0LXQsjwvYnV0dG9uPgogICAgPGJ1dHRvbiBjbGFzcz0iZ2J0biIgZGF0YS12YWw9ItCR0L7Qu9C10LUgNiDQvNC10YHRj9GG0LXQsiIgZGF0YS1pMThuPSJnNCI+0JHQvtC70LXQtSA2INC80LXRgdGP0YbQtdCyPC9idXR0b24+CiAgPC9kaXY+CgogIDwhLS0gU3RlcCA0IC0tPgogIDxkaXYgY2xhc3M9InN0ZXAiIGlkPSJiazQiPgogICAgPGRpdiBjbGFzcz0ic2xibCIgZGF0YS1pMThuPSJzdGVwNF9sYmwiPtCS0YvQsdC10YDQuNGC0LUg0LTQsNGC0YM8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImNhbC1oIj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iY2FsLW4iIGlkPSJwcmV2TSI+JiM4MjQ5OzwvYnV0dG9uPgogICAgICA8ZGl2IGNsYXNzPSJjYWwtbSIgaWQ9ImNhbE0iPjwvZGl2PgogICAgICA8YnV0dG9uIGNsYXNzPSJjYWwtbiIgaWQ9Im5leHRNIj4mIzgyNTA7PC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImNnIiBpZD0iY2FsRyI+PC9kaXY+CiAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7Z2FwOjIwcHg7YWxpZ24taXRlbXM6Y2VudGVyO21hcmdpbi10b3A6MTJweDtwYWRkaW5nLXRvcDoxMnB4O2JvcmRlci10b3A6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEpO2ZsZXgtd3JhcDp3cmFwOyI+PGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OHB4OyI+PGRpdiBzdHlsZT0id2lkdGg6MTZweDtoZWlnaHQ6MTZweDtib3JkZXItcmFkaXVzOjUwJTtiYWNrZ3JvdW5kOnJnYmEoOTAsMTgwLDkwLC4xNSk7Ym9yZGVyOjFweCBzb2xpZCAjNWFiNDVhO2ZsZXgtc2hyaW5rOjA7Ij48L2Rpdj48c3BhbiBzdHlsZT0iZm9udC1zaXplOjFyZW07Y29sb3I6I2ZmZmZmZjtsZXR0ZXItc3BhY2luZzouMDNlbTsiIGRhdGEtaTE4bj0iY2FsX2F2YWlsIj7QldGB0YLRjCDRgdCy0L7QsdC+0LTQvdC+0LUg0LLRgNC10LzRjzwvc3Bhbj48L2Rpdj48ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo4cHg7Ij48ZGl2IHN0eWxlPSJ3aWR0aDoxNnB4O2hlaWdodDoxNnB4O2JvcmRlci1yYWRpdXM6NTAlO2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDQpO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMSk7ZmxleC1zaHJpbms6MDsiPjwvZGl2PjxzcGFuIHN0eWxlPSJmb250LXNpemU6MXJlbTtjb2xvcjojZmZmZmZmO2xldHRlci1zcGFjaW5nOi4wM2VtOyIgZGF0YS1pMThuPSJjYWxfbm9uZSI+0KHQstC+0LHQvtC00L3QvtCz0L4g0LLRgNC10LzQtdC90Lgg0L3QtdGCPC9zcGFuPjwvZGl2PjwvZGl2PgogICAgPGRpdiBpZD0idGltZVNlYyIgc3R5bGU9ImRpc3BsYXk6bm9uZTttYXJnaW4tdG9wOjE2cHgiPgogICAgICA8ZGl2IGNsYXNzPSJzbGJsIiBkYXRhLWkxOG49InN0ZXA0X3RpbWUiPtCS0YvQsdC10YDQuNGC0LUg0LLRgNC10LzRjzwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJ0ZyIgaWQ9InRpbWVHIj48L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBzdHlsZT0ibWFyZ2luLXRvcDoyMHB4O3BhZGRpbmctdG9wOjE2cHg7Ym9yZGVyLXRvcDoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDgpO3RleHQtYWxpZ246Y2VudGVyIj4KICAgICAgPGJ1dHRvbiBpZD0iY2FsbGJhY2tCdG4iIGNsYXNzPSJjYmstYnRuIj7QndC1INC90LDRiNC70Lgg0YPQtNC+0LHQvdC+0LUg0LLRgNC10LzRjz8g4oaSPC9idXR0b24+CiAgICA8L2Rpdj4KICA8L2Rpdj4KCiAgPCEtLSBTdGVwIDUgLS0+CiAgPGRpdiBjbGFzcz0ic3RlcCIgaWQ9ImJrNSI+CiAgICA8ZGl2IGNsYXNzPSJzbGJsIiBkYXRhLWkxOG49InN0ZXA1X2xibCI+0JLQsNGI0Lgg0LTQsNC90L3Ri9C1PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJmZyI+PGxhYmVsIGNsYXNzPSJmbCIgZGF0YS1pMThuPSJsYmxfbmFtZSI+0JjQvNGPPC9sYWJlbD48aW5wdXQgY2xhc3M9ImZpIiBpZD0iY05hbWUiIHR5cGU9InRleHQiIHBsYWNlaG9sZGVyPSLQktCw0YjQtSDQuNC80Y8iIGRhdGEtaTE4bi1waD0icGhfbmFtZSI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJmZyI+PGxhYmVsIGNsYXNzPSJmbCIgZGF0YS1pMThuPSJsYmxfcGhvbmUiPtCi0LXQu9C10YTQvtC9PC9sYWJlbD48aW5wdXQgY2xhc3M9ImZpIiBpZD0iY1Bob25lIiB0eXBlPSJ0ZWwiIHBsYWNlaG9sZGVyPSIrMzcyIC4uLiI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJmZyI+PGxhYmVsIGNsYXNzPSJmbCIgZGF0YS1pMThuPSJsYmxfZW1haWwiPkVtYWlsPC9sYWJlbD48aW5wdXQgY2xhc3M9ImZpIiBpZD0iY0VtYWlsIiB0eXBlPSJlbWFpbCIgcGxhY2Vob2xkZXI9ImVtYWlsQGV4YW1wbGUuY29tIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImZnIj48bGFiZWwgY2xhc3M9ImZsIiBkYXRhLWkxOG49ImxibF9wZXQiPtCa0LvQuNGH0LrQsCDQv9C40YLQvtC80YbQsDwvbGFiZWw+PGlucHV0IGNsYXNzPSJmaSIgaWQ9ImNQZXQiIHR5cGU9InRleHQiIHBsYWNlaG9sZGVyPSLQndC10L7QsdGP0LfQsNGC0LXQu9GM0L3QviIgZGF0YS1pMThuLXBoPSJwaF9vcHRpb25hbCI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzdW0iIGlkPSJzdW1CbG9jayI+PC9kaXY+CiAgICA8YnV0dG9uIGNsYXNzPSJjYnRuIiBpZD0iY29uZmlybUJ0biIgZGF0YS1pMThuPSJjb25maXJtX2J0biI+0J/QvtC00YLQstC10YDQtNC40YLRjCDQt9Cw0L/QuNGB0Yw8L2J1dHRvbj4KICA8L2Rpdj4KCiAgPCEtLSBTdWNjZXNzIC0tPgogIDxkaXYgY2xhc3M9InNibG9jayIgaWQ9InN1Y0Jsb2NrIj4KICAgIDxkaXYgY2xhc3M9InNpMiI+8J+QvjwvZGl2PgogICAgPGRpdiBjbGFzcz0ic3QiIGRhdGEtaTE4bj0ic3VjY2Vzc190aXRsZSI+0JfQsNC/0LjRgdGMINC/0YDQuNC90Y/RgtCwITwvZGl2PgogICAgPGRpdiBjbGFzcz0ic3MiIGRhdGEtaTE4bj0ic3VjY2Vzc19zdWIiPtCc0Ysg0YHQstGP0LbQtdC80YHRjyDRgSDQstCw0LzQuCDQtNC70Y8g0L/QvtC00YLQstC10YDQttC00LXQvdC40Y8uPGJyPtCh0L/QsNGB0LjQsdC+LCDRh9GC0L4g0LLRi9Cx0YDQsNC70LggUiZKIEdyb29taW5nITwvZGl2PgogICAgPGJ1dHRvbiBjbGFzcz0iaGJ0biIgaWQ9ImhvbWVCdG4iIGRhdGEtaTE4bj0idG9faG9tZSI+4oaQINCd0LAg0LPQu9Cw0LLQvdGD0Y48L2J1dHRvbj4KICA8L2Rpdj4KPC9kaXY+CjwvZGl2PgoKPGRpdiBpZD0iY2JrTW9kYWwiIHN0eWxlPSJkaXNwbGF5Om5vbmU7cG9zaXRpb246Zml4ZWQ7aW5zZXQ6MDtiYWNrZ3JvdW5kOnJnYmEoMCwwLDAsLjc1KTt6LWluZGV4OjMwMDthbGlnbi1pdGVtczpjZW50ZXI7anVzdGlmeS1jb250ZW50OmNlbnRlcjtwYWRkaW5nOjIwcHgiPgogIDxkaXYgc3R5bGU9ImJhY2tncm91bmQ6IzBhMGEwYTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEyKTtib3JkZXItdG9wOjFweCBzb2xpZCAjZmZmZmZmO3BhZGRpbmc6MjhweCAyNHB4O3dpZHRoOjEwMCU7bWF4LXdpZHRoOjM2MHB4Ij4KICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZTowLjgzOHJlbTtsZXR0ZXItc3BhY2luZzouMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO21hcmdpbi1ib3R0b206MTZweDtmb250LXdlaWdodDo2MDA7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWYiPtCe0LHRgNCw0YLQvdGL0Lkg0LfQstC+0L3QvtC6PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJmZyI+PGxhYmVsIGNsYXNzPSJmbCI+0JjQvNGPPC9sYWJlbD48aW5wdXQgY2xhc3M9ImZpIiBpZD0iY2JrTmFtZSIgdHlwZT0idGV4dCIgcGxhY2Vob2xkZXI9ItCS0LDRiNC1INC40LzRjyI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJmZyI+CiAgICAgIDxsYWJlbCBjbGFzcz0iZmwiPtCi0LXQu9C10YTQvtC9PC9sYWJlbD4KICAgICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOnN0cmV0Y2g7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTUpIj4KICAgICAgICA8c3BhbiBzdHlsZT0icGFkZGluZzoxMHB4IDEwcHggMTBweCAwO2NvbG9yOiNmZmZmZmY7Zm9udC1zaXplOjEuMzYzcmVtO2JvcmRlci1yaWdodDoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMSk7bWFyZ2luLXJpZ2h0OjEwcHg7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWYiPiszNzI8L3NwYW4+CiAgICAgICAgPGlucHV0IGlkPSJjYmtQaG9uZSIgdHlwZT0idGVsIiBwbGFjZWhvbGRlcj0iWFhYWFhYWFgiIHN0eWxlPSJmbGV4OjE7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtvdXRsaW5lOm5vbmU7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjQzOHJlbTtjb2xvcjojZmZmZmZmO3BhZGRpbmc6MTBweCAwIj4KICAgICAgPC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgaWQ9ImNia1N1Y2Nlc3MiIHN0eWxlPSJkaXNwbGF5Om5vbmU7dGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzoyMHB4IDAiPgogICAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6Mi44NzVyZW07bWFyZ2luLWJvdHRvbToxMHB4O29wYWNpdHk6LjUiPuKckzwvZGl2PgogICAgICA8ZGl2IHN0eWxlPSJmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjEuODc1cmVtO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbTo2cHgiPtCX0LDRj9Cy0LrQsCDQv9GA0LjQvdGP0YLQsCE8L2Rpdj4KICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjEuMDM3cmVtO2NvbG9yOiNmZmZmZmY7bGluZS1oZWlnaHQ6MS42O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmIj7QnNGLINC/0LXRgNC10LfQstC+0L3QuNC8INCy0LDQvCDQsiDQsdC70LjQttCw0LnRiNC10LUg0LLRgNC10LzRjzwvZGl2PgogICAgPC9kaXY+CiAgICA8YnV0dG9uIGlkPSJjYmtTdWJtaXQiIGNsYXNzPSJjYnRuIiBzdHlsZT0ibWFyZ2luLXRvcDoxNHB4Ij7QntGC0L/RgNCw0LLQuNGC0Yw8L2J1dHRvbj4KICAgIDxidXR0b24gaWQ9ImNia0Nsb3NlIiBzdHlsZT0iZGlzcGxheTpibG9jazt3aWR0aDoxMDAlO21hcmdpbi10b3A6OHB4O2JhY2tncm91bmQ6bm9uZTtib3JkZXI6bm9uZTtjb2xvcjojZmZmZmZmO2ZvbnQtc2l6ZTowLjgzOHJlbTtsZXR0ZXItc3BhY2luZzouMTJlbTtjdXJzb3I6cG9pbnRlcjtwYWRkaW5nOjhweDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZiI+0J7RgtC80LXQvdCwPC9idXR0b24+CiAgPC9kaXY+CjwvZGl2PgoKPHNjcmlwdD4KdmFyIERBVEEgPSBbeyJicmVlZCI6ItCQ0LLRgdGC0YDQsNC70LjQudGB0LrQsNGPINC+0LLRh9Cw0YDQutCwIDE14oCTMjUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjYwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjg1fSwiYnJlZWRfZW4iOiJBdXN0cmFsaWFuIFNoZXBoZXJkIDE14oCTMjUga2ciLCJicmVlZF9ldCI6IkF1c3RyYWFsaWEgbGFtYmFrb2VyIDE14oCTMjUga2cifSx7ImJyZWVkIjoi0JDQstGB0YLRgNCw0LvQuNC50YHQutCw0Y8g0L7QstGH0LDRgNC60LAgMjXigJMzNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjU1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJBdXN0cmFsaWFuIFNoZXBoZXJkIDI14oCTMzUga2ciLCJicmVlZF9ldCI6IkF1c3RyYWFsaWEgbGFtYmFrb2VyIDI14oCTMzUga2cifSx7ImJyZWVkIjoi0JDQutC40YLQsC3QuNC90YMgMjDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQWtpdGEgSW51IDIw4oCTNDAga2ciLCJicmVlZF9ldCI6IkFraXRhIEludSAyMOKAkzQwIGtnIn0seyJicmVlZCI6ItCQ0LrQuNGC0LAt0LjQvdGDINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJBa2l0YSBJbnUgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiQWtpdGEgSW51IMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JDQutC40YLQsC3QuNC90YMg0YTQu9Cw0YTRhNC4IDIw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkFraXRhIEludSBmbHVmZnkgMjDigJM0MCBrZyIsImJyZWVkX2V0IjoiQWtpdGEgSW51IHBlaG1la2FydmFsaW5lIDIw4oCTNDAga2cifSx7ImJyZWVkIjoi0JDQutC40YLQsC3QuNC90YMg0YTQu9Cw0YTRhNC4INCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJBa2l0YSBJbnUgZmx1ZmZ5IG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkFraXRhIEludSBwZWhtZWthcnZhbGluZSDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCQ0LvQsNCx0LDQuSA0MOKAkzYwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQ2VudHJhbCBBc2lhbiBTaGVwaGVyZCA0MOKAkzYwIGtnIiwiYnJlZWRfZXQiOiJLZXNrLUFhc2lhIGxhbWJha29lciA0MOKAkzYwIGtnIn0seyJicmVlZCI6ItCQ0LvQsNCx0LDQuSDQsdC+0LvQtdC1IDYwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MTAwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MTE1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTMwfSwiYnJlZWRfZW4iOiJDZW50cmFsIEFzaWFuIFNoZXBoZXJkIG92ZXIgNjAga2ciLCJicmVlZF9ldCI6Iktlc2stQWFzaWEgbGFtYmFrb2VyIMO8bGUgNjAga2cifSx7ImJyZWVkIjoi0JDQu9GP0YHQutC40L3RgdC60LjQuSDQvNCw0LvQsNC80YPRgiAyMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJBbGFza2FuIE1hbGFtdXRlIDIw4oCTNDAga2ciLCJicmVlZF9ldCI6IkFsYXNrYSBtYWxhbXV1dCAyMOKAkzQwIGtnIn0seyJicmVlZCI6ItCQ0LvRj9GB0LrQuNC90YHQutC40Lkg0LzQsNC70LDQvNGD0YIg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkFsYXNrYW4gTWFsYW11dGUgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiQWxhc2thIG1hbGFtdXV0IMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JDQu9GP0YHQutC40L3RgdC60LjQuSDQvNCw0LvQsNC80YPRgiDRhNC70LDRhNGE0LggMjDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQWxhc2thbiBNYWxhbXV0ZSBmbHVmZnkgMjDigJM0MCBrZyIsImJyZWVkX2V0IjoiQWxhc2thIG1hbGFtdXV0IHBlaG1la2FydmFsaW5lIDIw4oCTNDAga2cifSx7ImJyZWVkIjoi0JDQu9GP0YHQutC40L3RgdC60LjQuSDQvNCw0LvQsNC80YPRgiDRhNC70LDRhNGE0Lgg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkFsYXNrYW4gTWFsYW11dGUgZmx1ZmZ5IG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkFsYXNrYSBtYWxhbXV1dCBwZWhtZWthcnZhbGluZSDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LDRjyDQsNC60LjRgtCwIDIw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkFtZXJpY2FuIEFraXRhIDIw4oCTNDAga2ciLCJicmVlZF9ldCI6IkFtZWVyaWthIEFraXRhIDIw4oCTNDAga2cifSx7ImJyZWVkIjoi0JDQvNC10YDQuNC60LDQvdGB0LrQsNGPINCw0LrQuNGC0LAg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkFtZXJpY2FuIEFraXRhIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkFtZWVyaWthIEFraXRhIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JDQvNC10YDQuNC60LDQvdGB0LrQsNGPINCw0LrQuNGC0LAg0YTQu9Cw0YTRhNC4IDIw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkFtZXJpY2FuIEFraXRhIGZsdWZmeSAyMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBBa2l0YSBwZWhtZWthcnZhbGluZSAyMOKAkzQwIGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LDRjyDQsNC60LjRgtCwINGE0LvQsNGE0YTQuCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQW1lcmljYW4gQWtpdGEgZmx1ZmZ5IG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkFtZWVyaWthIEFraXRhIHBlaG1la2FydmFsaW5lIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JDQvNC10YDQuNC60LDQvdGB0LrQuNC5INC60L7QutC10YAt0YHQv9Cw0L3QuNC10LvRjCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IkFtZXJpY2FuIENvY2tlciBTcGFuaWVsIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IkFtZWVyaWthIGtva2Vyc3BhbmplbCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LjQuSDQutC+0LrQtdGALdGB0L/QsNC90LjQtdC70YwgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwfSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBDb2NrZXIgU3BhbmllbCAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBrb2tlcnNwYW5qZWwgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutC40Lkg0YHRgtCw0YTRhNC+0YDQtNGI0LjRgNGB0LrQuNC5INGC0LXRgNGM0LXRgCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiQW1lcmljYW4gU3RhZmZvcmRzaGlyZSBUZXJyaWVyIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkFtZWVyaWthIFN0YWZmb3Jkc2hpcmUgdGVyamVyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JDQvNC10YDQuNC60LDQvdGB0LrQuNC5INGB0YLQsNGE0YTQvtGA0LTRiNC40YDRgdC60LjQuSDRgtC10YDRjNC10YAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjU1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjV9LCJicmVlZF9lbiI6IkFtZXJpY2FuIFN0YWZmb3Jkc2hpcmUgVGVycmllciAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBTdGFmZm9yZHNoaXJlIHRlcmplciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCQ0L3Qs9C70LjQudGB0LrQuNC5INCx0YPQu9GM0LTQvtCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo3MH0sImJyZWVkX2VuIjoiRW5nbGlzaCBCdWxsZG9nIiwiYnJlZWRfZXQiOiJJbmdsaXNlIGJ1bGRvZyJ9LHsiYnJlZWQiOiLQkNC90LPQu9C40LnRgdC60LjQuSDQutC+0LrQtdGALdGB0L/QsNC90LjQtdC70YwgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJFbmdsaXNoIENvY2tlciBTcGFuaWVsIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IkluZ2xpc2Uga29rZXJzcGFuamVsIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0JDQvdCz0LvQuNC50YHQutC40Lkg0LrQvtC60LXRgC3RgdC/0LDQvdC40LXQu9GMIDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4MH0sImJyZWVkX2VuIjoiRW5nbGlzaCBDb2NrZXIgU3BhbmllbCAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiJJbmdsaXNlIGtva2Vyc3BhbmplbCAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCQ0YTQs9Cw0L0gMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwfSwiYnJlZWRfZW4iOiJBZmdoYW4gSG91bmQgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiQWZnYW5pc3Rhbmkga29lciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCQ0YTQs9Cw0L0gMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMH0sImJyZWVkX2VuIjoiQWZnaGFuIEhvdW5kIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkFmZ2FuaXN0YW5pIGtvZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkdCw0YHRgdC10YIt0YXQsNGD0L3QtCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiQmFzc2V0IEhvdW5kIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkJhc3NldGhvdW5kIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JHQsNGB0YHQtdGCLdGF0LDRg9C90LQgMzDigJMzNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjU1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjV9LCJicmVlZF9lbiI6IkJhc3NldCBIb3VuZCAzMOKAkzM1IGtnIiwiYnJlZWRfZXQiOiJCYXNzZXRob3VuZCAzMOKAkzM1IGtnIn0seyJicmVlZCI6ItCR0LXRgNC90YHQutC40Lkg0LfQtdC90L3QtdC90YXRg9C90LQgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjExMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiQmVybmVzZSBNb3VudGFpbiBEb2cgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiQmVybmkgbcOkZ2lrb2VyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JHQtdGA0L3RgdC60LjQuSDQt9C10L3QvdC10L3RhdGD0L3QtCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTMwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJCZXJuZXNlIE1vdW50YWluIERvZyBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJCZXJuaSBtw6RnaWtvZXIgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQkdC40LLQtdGALdC50L7RgNC6INCx0L7Qu9C10LUgMyw1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkJpZXdlciBZb3Jrc2hpcmUgVGVycmllciBvdmVyIDMsNSBrZyIsImJyZWVkX2V0IjoiQmlld2VyIFlvcmtzaGlyZSBUZXJyaWVyIMO8bGUgMyw1IGtnIn0seyJicmVlZCI6ItCR0LjQstC10YAt0LnQvtGA0Log0LTQviAzLDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiQmlld2VyIFlvcmtzaGlyZSBUZXJyaWVyIHVwIHRvIDMsNSBrZyIsImJyZWVkX2V0IjoiQmlld2VyIFlvcmtzaGlyZSBUZXJyaWVyIGt1bmkgMyw1IGtnIn0seyJicmVlZCI6ItCR0LjQs9C70YwgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2MH0sImJyZWVkX2VuIjoiQmVhZ2xlIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IkJpaWdlbCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCR0LjQs9C70YwgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2NX0sImJyZWVkX2VuIjoiQmVhZ2xlIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IkJpaWdlbCAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCR0LjRiNC+0L0t0YTRgNC40LfQtSA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiQmljaG9uIEZyaXPDqSA14oCTMTAga2ciLCJicmVlZF9ldCI6IkJpxaFvbiBGcmlzw6kgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCR0LjRiNC+0L0t0YTRgNC40LfQtSDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiQmljaG9uIEZyaXPDqSB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJCacWhb24gRnJpc8OpIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQkdC+0LrRgdC10YAgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTB9LCJicmVlZF9lbiI6IkJveGVyIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkJva3NlciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCR0L7QutGB0LXRgCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiQm94ZXIgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiQm9rc2VyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JHQvtGA0LTQtdGALdC60L7Qu9C70LggMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6ODB9LCJicmVlZF9lbiI6IkJvcmRlciBDb2xsaWUgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiQm9yZGVya29sbCAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCR0L7RgNC00LXRgC3QutC+0LvQu9C4IDIw4oCTMjUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQm9yZGVyIENvbGxpZSAyMOKAkzI1IGtnIiwiYnJlZWRfZXQiOiJCb3JkZXJrb2xsIDIw4oCTMjUga2cifSx7ImJyZWVkIjoi0JHQvtGB0YLQvtC9LdGC0LXRgNGM0LXRgCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NX0sImJyZWVkX2VuIjoiQm9zdG9uIFRlcnJpZXIgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiQm9zdG9uaSB0ZXJqZXIgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQkdC+0YHRgtC+0L0t0YLQtdGA0YzQtdGAIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDB9LCJicmVlZF9lbiI6IkJvc3RvbiBUZXJyaWVyIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiQm9zdG9uaSB0ZXJqZXIgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCR0YDQsNCx0LDQvdGB0L7QvSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NTB9LCJicmVlZF9lbiI6IkdyaWZmb24gQnJ1eGVsbG9pcyIsImJyZWVkX2V0IjoiQnLDvHNzZWxpIGdyaWZvbiJ9LHsiYnJlZWQiOiLQkdGD0LvRjNGC0LXRgNGM0LXRgCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MH0sImJyZWVkX2VuIjoiQnVsbCBUZXJyaWVyIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkJ1bGx0ZXJqZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQktC10LvRjNGILdC60L7RgNCz0LggMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2NX0sImJyZWVkX2VuIjoiV2Vsc2ggQ29yZ2kgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiV2FsZXNpIGtvcmdpIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0JLQtdC70YzRiC3QutC+0YDQs9C4IDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NzV9LCJicmVlZF9lbiI6IldlbHNoIENvcmdpIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IldhbGVzaSBrb3JnaSAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCS0LXRgdGCLdGF0LDQudC70LXQvdC0LdCy0LDQudGCLdGC0LXRgNGM0LXRgCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MCwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6Ildlc3QgSGlnaGxhbmQgV2hpdGUgVGVycmllciIsImJyZWVkX2V0IjoiTMOkw6RuZS3FoG90aW1hYSB2YWxnZSB0ZXJqZXIifSx7ImJyZWVkIjoi0JLQvtGB0YLQvtGH0L3QvtGB0LjQsdC40YDRgdC60LDRjyDQu9Cw0LnQutCwIDE44oCTMjUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjYwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6ODV9LCJicmVlZF9lbiI6IkVhc3QgU2liZXJpYW4gTGFpa2EgMTjigJMyNSBrZyIsImJyZWVkX2V0IjoiSWRhLVNpYmVyaSBsYWlrYSAxOOKAkzI1IGtnIn0seyJicmVlZCI6ItCS0L7RgdGC0L7Rh9C90L7RgdC40LHQuNGA0YHQutCw0Y8g0LvQsNC50LrQsCDQsdC+0LvQtdC1IDI1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiRWFzdCBTaWJlcmlhbiBMYWlrYSBvdmVyIDI1IGtnIiwiYnJlZWRfZXQiOiJJZGEtU2liZXJpIGxhaWthIMO8bGUgMjUga2cifSx7ImJyZWVkIjoi0JPQvtC70LTQtdC9LdGA0LXRgtGA0LjQstC10YAgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJHb2xkZW4gUmV0cmlldmVyIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6Ikt1bGRuZSByZXRyaWl2ZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQk9C+0LvQtNC10L0t0YDQtdGC0YDQuNCy0LXRgCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTEwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJHb2xkZW4gUmV0cmlldmVyIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6Ikt1bGRuZSByZXRyaWl2ZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQk9GA0LjRhNGE0L7QvSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MCwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6IkdyaWZmb24iLCJicmVlZF9ldCI6IkdyaWZvbiJ9LHsiYnJlZWQiOiLQlNCw0LvQvNCw0YLQuNC9Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo3MH0sImJyZWVkX2VuIjoiRGFsbWF0aWFuIiwiYnJlZWRfZXQiOiJEYWxtYWF0c2lhIGtvZXIifSx7ImJyZWVkIjoi0JTQttC10Lot0YDQsNGB0YHQtdC7LdGC0LXRgNGM0LXRgCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3Ri9C5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo1MH0sImJyZWVkX2VuIjoiSmFjayBSdXNzZWxsIFRlcnJpZXIgc21vb3RoIiwiYnJlZWRfZXQiOiJKYWNrIFJ1c3NlbGxpIHRlcmplciBsw7xoaWthcnZhbGluZSJ9LHsiYnJlZWQiOiLQlNC20LXQui3RgNCw0YHRgdC10Lst0YLQtdGA0YzQtdGAINC20LXRgdGC0LrQvtGI0LXRgNGB0YLQvdGL0LkiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJKYWNrIFJ1c3NlbGwgVGVycmllciB3aXJlLWhhaXJlZCIsImJyZWVkX2V0IjoiSmFjayBSdXNzZWxsaSB0ZXJqZXIga2FydWthcnZhbGluZSJ9LHsiYnJlZWQiOiLQlNC+0LHQtdGA0LzQsNC9IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6ODB9LCJicmVlZF9lbiI6IkRvYmVybWFubiAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJEb2Jlcm1hbm4gMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQlNC+0LHQtdGA0LzQsNC9INCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjgwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTV9LCJicmVlZF9lbiI6IkRvYmVybWFubiBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJEb2Jlcm1hbm4gw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQl9Cw0L/QsNC00L3QvtGB0LjQsdC40YDRgdC60LDRjyDQu9Cw0LnQutCwIDE44oCTMjUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjYwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6ODV9LCJicmVlZF9lbiI6Ildlc3QgU2liZXJpYW4gTGFpa2EgMTjigJMyNSBrZyIsImJyZWVkX2V0IjoiTMOkw6RuZS1TaWJlcmkgbGFpa2EgMTjigJMyNSBrZyJ9LHsiYnJlZWQiOiLQl9C+0LvQvtGC0LjRgdGC0YvQuSDRgNC10YLRgNC40LLQtdGAIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJHb2xkZW4gUmV0cmlldmVyIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6Ikt1bGRuZSByZXRyaWl2ZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQl9C+0LvQvtGC0LjRgdGC0YvQuSDRgNC10YLRgNC40LLQtdGAIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjgwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTB9LCJicmVlZF9lbiI6IkdvbGRlbiBSZXRyaWV2ZXIgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiS3VsZG5lIHJldHJpaXZlciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCY0YDQu9Cw0L3QtNGB0LrQuNC5INC80Y/Qs9C60L7RiNC10YDRgdGC0L3Ri9C5INC/0YjQtdC90LjRh9C90YvQuSDRgtC10YDRjNC10YAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODB9LCJicmVlZF9lbiI6IklyaXNoIFNvZnQgQ29hdGVkIFdoZWF0ZW4gVGVycmllciIsImJyZWVkX2V0IjoiSWlyaSBwZWhtZWthcnZhbmUgbmlzdXbDpHJ2aSB0ZXJqZXIifSx7ImJyZWVkIjoi0JjRgNC70LDQvdC00YHQutC40Lkg0YLQtdGA0YzQtdGAIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwLCLQotGA0LjQvNC80LjQvdCzIjo3NX0sImJyZWVkX2VuIjoiSXJpc2ggVGVycmllciIsImJyZWVkX2V0IjoiSWlyaSB0ZXJqZXIifSx7ImJyZWVkIjoi0JjRgdC/0LDQvdGB0LrQuNC5INCz0LDQu9GM0LPQviAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjcwfSwiYnJlZWRfZW4iOiJTcGFuaXNoIEdhbGdvIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6Ikhpc3BhYW5pYSBnYWxnbyAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCY0YHQv9Cw0L3RgdC60LjQuSDQs9Cw0LvRjNCz0L4gMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjU1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4MH0sImJyZWVkX2VuIjoiU3BhbmlzaCBHYWxnbyAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJIaXNwYWFuaWEgZ2FsZ28gMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQmdC+0YDQutGI0LjRgNGB0LrQuNC5INGC0LXRgNGM0LXRgCDQsdC+0LvQtdC1IDMsNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJZb3Jrc2hpcmUgVGVycmllciBvdmVyIDMsNSBrZyIsImJyZWVkX2V0IjoiWW9ya3NoaXJlIHRlcmplciDDvGxlIDMsNSBrZyJ9LHsiYnJlZWQiOiLQmdC+0YDQutGI0LjRgNGB0LrQuNC5INGC0LXRgNGM0LXRgCDQtNC+IDMsNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJZb3Jrc2hpcmUgVGVycmllciB1cCB0byAzLDUga2ciLCJicmVlZF9ldCI6IllvcmtzaGlyZSB0ZXJqZXIga3VuaSAzLDUga2cifSx7ImJyZWVkIjoi0JrQsNCy0LDQu9C10YAt0LrQuNC90LMt0YfQsNGA0LvRjNC3LdGB0L/QsNC90LjQtdC70YwgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJDYXZhbGllciBLaW5nIENoYXJsZXMgU3BhbmllbCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJDYXZhbGllciBLaW5nIENoYXJsZXMgU3BhbmllbCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCa0LDQstCw0LvQtdGALdC60LjQvdCzLdGH0LDRgNC70YzQty3RgdC/0LDQvdC40LXQu9GMIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJDYXZhbGllciBLaW5nIENoYXJsZXMgU3BhbmllbCA14oCTMTAga2ciLCJicmVlZF9ldCI6IkNhdmFsaWVyIEtpbmcgQ2hhcmxlcyBTcGFuaWVsIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQmtCw0L3QtS3QutC+0YDRgdC+IDQw4oCTNjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1fSwiYnJlZWRfZW4iOiJDYW5lIENvcnNvIDQw4oCTNjAga2ciLCJicmVlZF9ldCI6IkNhbmUgQ29yc28gNDDigJM2MCBrZyJ9LHsiYnJlZWQiOiLQmtCw0L3QtS3QutC+0YDRgdC+INCx0L7Qu9C10LUgNjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo5MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjEwNX0sImJyZWVkX2VuIjoiQ2FuZSBDb3JzbyBvdmVyIDYwIGtnIiwiYnJlZWRfZXQiOiJDYW5lIENvcnNvIMO8bGUgNjAga2cifSx7ImJyZWVkIjoi0JrQsNGA0LXQu9C+LdGE0LjQvdGB0LrQsNGPINC70LDQudC60LAg0LTQviAxMyDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2NX0sImJyZWVkX2VuIjoiS2FyZWxpYW4tRmlubmlzaCBMYWlrYSB1cCB0byAxMyBrZyIsImJyZWVkX2V0IjoiS2FyamFsYS1Tb29tZSBsYWlrYSBrdW5pIDEzIGtnIn0seyJicmVlZCI6ItCa0LjRgtCw0LnRgdC60LDRjyDRhdC+0YXQu9Cw0YLQsNGPINCz0L7Qu9Cw0Y8gNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzIsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0Miwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkNoaW5lc2UgQ3Jlc3RlZCBoYWlybGVzcyA14oCTMTAga2ciLCJicmVlZF9ldCI6IkhpaW5hIGhhcmpha29lciBrYXJ2YXR1IDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQmtC40YLQsNC50YHQutCw0Y8g0YXQvtGF0LvQsNGC0LDRjyDQs9C+0LvQsNGPINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjI4LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MzUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJDaGluZXNlIENyZXN0ZWQgaGFpcmxlc3MgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiSGlpbmEgaGFyamFrb2VyIGthcnZhdHUga3VuaSA1IGtnIn0seyJicmVlZCI6ItCa0LjRgtCw0LnRgdC60LDRjyDRhdC+0YXQu9Cw0YLQsNGPINC/0YPRhdC+0LLQsNGPIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJDaGluZXNlIENyZXN0ZWQgcG93ZGVycHVmZiA14oCTMTAga2ciLCJicmVlZF9ldCI6IkhpaW5hIGhhcmpha29lciBQb3dkZXJwdWZmIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQmtC40YLQsNC50YHQutCw0Y8g0YXQvtGF0LvQsNGC0LDRjyDQv9GD0YXQvtCy0LDRjyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiQ2hpbmVzZSBDcmVzdGVkIHBvd2RlcnB1ZmYgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiSGlpbmEgaGFyamFrb2VyIFBvd2RlcnB1ZmYga3VuaSA1IGtnIn0seyJicmVlZCI6ItCa0L7QutCw0L/RgyA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3NX0sImJyZWVkX2VuIjoiQ29ja2Fwb28gNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJDb2NrYXBvbyA14oCTMTAga2cifSx7ImJyZWVkIjoi0JrQvtC60LDQv9GDINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjY1fSwiYnJlZWRfZW4iOiJDb2NrYXBvbyB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJDb2NrYXBvbyBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JrQvtC70LvQuCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkNvbGxpZSAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJLb2xsIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JrQvtC70LvQuCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTEwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJDb2xsaWUgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiS29sbCAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCa0L7QvNC+0L3QtNC+0YAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjExMH0sImJyZWVkX2VuIjoiS29tb25kb3IgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiS29tb25kb3IgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQmtC+0LzQvtC90LTQvtGAINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMzB9LCJicmVlZF9lbiI6IktvbW9uZG9yIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IktvbW9uZG9yIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00L7RgCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3Ri9C5IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NzB9LCJicmVlZF9lbiI6IkxhYnJhZG9yIFJldHJpZXZlciBzbW9vdGggMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiTGFicmFkb3JpIHJldHJpaXZlciBsw7xoaWthcnZhbGluZSAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNC+0YAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjgwfSwiYnJlZWRfZW4iOiJMYWJyYWRvciBSZXRyaWV2ZXIgc21vb3RoIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9yaSByZXRyaWl2ZXIgbMO8aGlrYXJ2YWxpbmUgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQm9Cw0LHRgNCw0LTQvtGAINCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdGL0Lkg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5NX0sImJyZWVkX2VuIjoiTGFicmFkb3IgUmV0cmlldmVyIHNtb290aCBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvcmkgcmV0cmlpdmVyIGzDvGhpa2FydmFsaW5lIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00L7RgCDQtNC70LjQvdC90L7RiNC10YDRgdGC0L3Ri9C5IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiTGFicmFkb3IgUmV0cmlldmVyIGxvbmctY29hdGVkIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9yaSByZXRyaWl2ZXIgcGlra2FydmFsaW5lIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00L7RgCDQtNC70LjQvdC90L7RiNC10YDRgdGC0L3Ri9C5IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IkxhYnJhZG9yIFJldHJpZXZlciBsb25nLWNvYXRlZCAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvcmkgcmV0cmlpdmVyIHBpa2thcnZhbGluZSAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNC+0YAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90YvQuSDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTMwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJMYWJyYWRvciBSZXRyaWV2ZXIgbG9uZy1jb2F0ZWQgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiTGFicmFkb3JpIHJldHJpaXZlciBwaWtrYXJ2YWxpbmUgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQm9Cw0LHRgNCw0LTRg9C00LXQu9GMIDEw4oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MH0sImJyZWVkX2VuIjoiTGFicmFkb29kbGUgMTDigJMyMCBrZyIsImJyZWVkX2V0IjoiTGFicmFkb29kbGUgMTDigJMyMCBrZyJ9LHsiYnJlZWQiOiLQm9Cw0LHRgNCw0LTRg9C00LXQu9GMIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5MH0sImJyZWVkX2VuIjoiTGFicmFkb29kbGUgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiTGFicmFkb29kbGUgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQm9Cw0LHRgNCw0LTRg9C00LXQu9GMIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjgwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDB9LCJicmVlZF9lbiI6IkxhYnJhZG9vZGxlIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9vZGxlIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JvQtdCy0YDQtdGC0LrQsCA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwfSwiYnJlZWRfZW4iOiJJdGFsaWFuIEdyZXlob3VuZCA14oCTMTAga2ciLCJicmVlZF9ldCI6Ikl0YWFsaWEgdmluZGtvZXIgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCb0LXQstGA0LXRgtC60LAg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MjUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjozNX0sImJyZWVkX2VuIjoiSXRhbGlhbiBHcmV5aG91bmQgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiSXRhYWxpYSB2aW5ka29lciBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JvRhdCw0YHRgdC60LjQuSDQsNC/0YHQviA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3NX0sImJyZWVkX2VuIjoiTGhhc2EgQXBzbyA14oCTMTAga2ciLCJicmVlZF9ldCI6IkxoYXNhIEFwc28gNeKAkzEwIGtnIn0seyJicmVlZCI6ItCb0YXQsNGB0YHQutC40Lkg0LDQv9GB0L4g0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjV9LCJicmVlZF9lbiI6IkxoYXNhIEFwc28gdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiTGhhc2EgQXBzbyBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JzQsNC70YzRgtC10LfQtSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiTWFsdGVzZSIsImJyZWVkX2V0IjoiTWFsdGEgYm9sb25lZXMifSx7ImJyZWVkIjoi0JzQsNC70YzRgtC40LnRgdC60LDRjyDQsdC+0LvQvtC90LrQsCA14oCTOCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjY1fSwiYnJlZWRfZW4iOiJNYWx0ZXNlIEJvbG9nbmVzZSA14oCTOCBrZyIsImJyZWVkX2V0IjoiTWFsdGEgYm9sb25lZXMgNeKAkzgga2cifSx7ImJyZWVkIjoi0JzQsNC70YzRgtC40LnRgdC60LDRjyDQsdC+0LvQvtC90LrQsCDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiTWFsdGVzZSBCb2xvZ25lc2UgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiTWFsdGEgYm9sb25lZXMga3VuaSA1IGtnIn0seyJicmVlZCI6ItCc0LDQu9GM0YLQuNC/0YMgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJNYWx0aXBvbyAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJNYWx0aXB1dSAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCc0LDQu9GM0YLQuNC/0YMgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6Ik1hbHRpcG9vIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiTWFsdGlwdXUgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCc0LDQu9GM0YLQuNC/0YMg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6Ik1hbHRpcG9vIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6Ik1hbHRpcHV1IGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQnNC10YLQuNGBINC60YDRg9C/0L3Ri9C5IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6Ik1peGVkIGJyZWVkIGxhcmdlIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IlNlZ2F2ZXJkIHN1dXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQnNC10YLQuNGBINC60YDRg9C/0L3Ri9C5INCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjkwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMjAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMjB9LCJicmVlZF9lbiI6Ik1peGVkIGJyZWVkIGxhcmdlIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IlNlZ2F2ZXJkIHN1dXIgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQnNC10YLQuNGBINC80LXQu9C60LjQuSA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiTWl4ZWQgYnJlZWQgc21hbGwgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJTZWdhdmVyZCB2w6Rpa2UgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCc0LXRgtC40YEg0LzQtdC70LrQuNC5INC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjI1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MzUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjUwfSwiYnJlZWRfZW4iOiJNaXhlZCBicmVlZCBzbWFsbCB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJTZWdhdmVyZCB2w6Rpa2Uga3VuaSA1IGtnIn0seyJicmVlZCI6ItCc0LXRgtC40YEg0YHRgNC10LTQvdC40LkgMTDigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NzB9LCJicmVlZF9lbiI6Ik1peGVkIGJyZWVkIG1lZGl1bSAxMOKAkzIwIGtnIiwiYnJlZWRfZXQiOiJTZWdhdmVyZCBrZXNrbWluZSAxMOKAkzIwIGtnIn0seyJicmVlZCI6ItCc0LXRgtC40YEg0YHRgNC10LTQvdC40LkgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjg1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6ODV9LCJicmVlZF9lbiI6Ik1peGVkIGJyZWVkIG1lZGl1bSAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJTZWdhdmVyZCBrZXNrbWluZSAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCc0LjRgtGC0LXQu9GM0YjQvdCw0YPRhtC10YAgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwLCLQotGA0LjQvNC80LjQvdCzIjo3NX0sImJyZWVkX2VuIjoiU3RhbmRhcmQgU2NobmF1emVyIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IlN0YW5kYXJkxaFuYXV0c2VyIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0JzQuNGC0YLQtdC70YzRiNC90LDRg9GG0LXRgCAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODAsItCi0YDQuNC80LzQuNC90LMiOjg1fSwiYnJlZWRfZW4iOiJTdGFuZGFyZCBTY2huYXV6ZXIgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiU3RhbmRhcmTFoW5hdXRzZXIgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQnNC+0L/RgSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NTB9LCJicmVlZF9lbiI6IlB1ZyIsImJyZWVkX2V0IjoiTW9wcyJ9LHsiYnJlZWQiOiLQndC10LLRgdC60LDRjyDQvtGA0YXQuNC00LXRjyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiTmV2YSBPcmNoaWQiLCJicmVlZF9ldCI6Ik5lZXZhIG9yaGlkZWUifSx7ImJyZWVkIjoi0J3QtdC80LXRhtC60LDRjyDQvtCy0YfQsNGA0LrQsCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJHZXJtYW4gU2hlcGhlcmQgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiU2Frc2EgbGFtYmFrb2VyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0J3QtdC80LXRhtC60LDRjyDQvtCy0YfQsNGA0LrQsCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiR2VybWFuIFNoZXBoZXJkIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IlNha3NhIGxhbWJha29lciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCd0LXQvNC10YbQutCw0Y8g0L7QstGH0LDRgNC60LAg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6Ikdlcm1hbiBTaGVwaGVyZCBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJTYWtzYSBsYW1iYWtvZXIgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQqNCy0LXQudGG0LDRgNGB0LrQsNGPINC+0LLRh9Cw0YDQutCwIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IlN3aXNzIFNoZXBoZXJkIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IsWgdmVpdHNpIGxhbWJha29lciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCo0LLQtdC50YbQsNGA0YHQutCw0Y8g0L7QstGH0LDRgNC60LAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IlN3aXNzIFNoZXBoZXJkIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IsWgdmVpdHNpIGxhbWJha29lciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCo0LLQtdC50YbQsNGA0YHQutCw0Y8g0L7QstGH0LDRgNC60LAg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IlN3aXNzIFNoZXBoZXJkIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IsWgdmVpdHNpIGxhbWJha29lciDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCd0L7RgNCy0LjRhy3RgtC10YDRjNC10YAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJOb3J3aWNoIFRlcnJpZXIiLCJicmVlZF9ldCI6Ik5vcndpdMWhaSB0ZXJqZXIifSx7ImJyZWVkIjoi0J3QvtGA0YTQvtC70Lot0YLQtdGA0YzQtdGAIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiTm9yZm9sayBUZXJyaWVyIiwiYnJlZWRfZXQiOiJOb3Jmb2xraSB0ZXJqZXIifSx7ImJyZWVkIjoi0J3RjNGO0YTQsNGD0L3QtNC70LXQvdC0IDQw4oCTNjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMzAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6Ik5ld2ZvdW5kbGFuZCA0MOKAkzYwIGtnIiwiYnJlZWRfZXQiOiJOZXdmb3VuZGxhbmRpIGtvZXIgNDDigJM2MCBrZyJ9LHsiYnJlZWQiOiLQndGM0Y7RhNCw0YPQvdC00LvQtdC90LQg0LHQvtC70LXQtSA2MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjEwMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjExNSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTUwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTMwfSwiYnJlZWRfZW4iOiJOZXdmb3VuZGxhbmQgb3ZlciA2MCBrZyIsImJyZWVkX2V0IjoiTmV3Zm91bmRsYW5kaSBrb2VyIMO8bGUgNjAga2cifSx7ImJyZWVkIjoi0J/QsNC/0LjQudC+0L0iLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IlBhcGlsbG9uIiwiYnJlZWRfZXQiOiJQYXBpbGxvbiJ9LHsiYnJlZWQiOiLQn9C10LrQuNC90LXRgSA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiUGVraW5nZXNlIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiUGVraW5lc2kga29lciA14oCTMTAga2cifSx7ImJyZWVkIjoi0J/QtdC60LjQvdC10YEg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IlBla2luZ2VzZSB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJQZWtpbmVzaSBrb2VyIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQn9GD0LTQtdC70Ywg0LHQvtC70YzRiNC+0LkgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwfSwiYnJlZWRfZW4iOiJTdGFuZGFyZCBQb29kbGUgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiU3RhbmRhcmRwdXVkZWwgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQn9GD0LTQtdC70Ywg0LHQvtC70YzRiNC+0LkgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMH0sImJyZWVkX2VuIjoiU3RhbmRhcmQgUG9vZGxlIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IlN0YW5kYXJkcHV1ZGVsIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0J/Rg9C00LXQu9GMINC60LDRgNC70LjQutC+0LLRi9C5IDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJNaW5pYXR1cmUgUG9vZGxlIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiS8Okw6RidXNwdXVkZWwgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCf0YPQtNC10LvRjCDQvNCw0LvRi9C5IDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MH0sImJyZWVkX2VuIjoiU21hbGwgUG9vZGxlIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IlbDpGlrZSBwdXVkZWwgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQn9GD0LTQtdC70Ywg0LzQsNC70YvQuSAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODB9LCJicmVlZF9lbiI6IlNtYWxsIFBvb2RsZSAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiJWw6Rpa2UgcHV1ZGVsIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0J/Rg9C00LXQu9GMINGC0L7QuSDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiVG95IFBvb2RsZSB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJNw6RuZ3Vhc2phIHB1dWRlbCBrdW5pIDUga2cifSx7ImJyZWVkIjoi0KDQuNC30LXQvdGI0L3QsNGD0YbQtdGAIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjgwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDAsItCi0YDQuNC80LzQuNC90LMiOjExMH0sImJyZWVkX2VuIjoiR2lhbnQgU2NobmF1emVyIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IlN1dXLFoW5hdXRzZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQoNC40LfQtdC90YjQvdCw0YPRhtC10YAg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjc1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEyMCwi0KLRgNC40LzQvNC40L3QsyI6MTI1fSwiYnJlZWRfZW4iOiJHaWFudCBTY2huYXV6ZXIgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiU3V1csWhbmF1dHNlciDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCg0YPRgdGB0LrQsNGPINGG0LLQtdGC0L3QsNGPINCx0L7Qu9C+0L3QutCwIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJSdXNzaWFuIENvbG9yZWQgTGFwZG9nIiwiYnJlZWRfZXQiOiJWZW5lIHbDpHJ2aWxpbmUgc8O8bGVrb2VyIn0seyJicmVlZCI6ItCg0YPRgdGB0LrQuNC5INC+0YXQvtGC0L3QuNGH0LjQuSDRgdC/0LDQvdC40LXQu9GMIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MH0sImJyZWVkX2VuIjoiUnVzc2lhbiBTcGFuaWVsIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IlZlbmUgamFoaXNwYW5qZWwgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQoNGD0YHRgdC60LjQuSDQvtGF0L7RgtC90LjRh9C40Lkg0YHQv9Cw0L3QuNC10LvRjCAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODB9LCJicmVlZF9lbiI6IlJ1c3NpYW4gU3BhbmllbCAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiJWZW5lIGphaGlzcGFuamVsIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0KDRg9GB0YHQutC40Lkg0YLQvtC5INCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdGL0LkiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MjUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjozNX0sImJyZWVkX2VuIjoiUnVzc2lhbiBUb3kgc21vb3RoIiwiYnJlZWRfZXQiOiJWZW5lIFRveSBsw7xoaWthcnZhbGluZSJ9LHsiYnJlZWQiOiLQoNGD0YHRgdC60LjQuSDRgtC+0Lkg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90YvQuSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiUnVzc2lhbiBUb3kgbG9uZy1jb2F0ZWQiLCJicmVlZF9ldCI6IlZlbmUgVG95IHBpa2thcnZhbGluZSJ9LHsiYnJlZWQiOiLQoNGD0YHRgdC60LjQuSDRh9C10YDQvdGL0Lkg0YLQtdGA0YzQtdGAIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjgwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDB9LCJicmVlZF9lbiI6IkJsYWNrIFJ1c3NpYW4gVGVycmllciAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJNdXN0IFZlbmUgdGVyamVyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0KDRg9GB0YHQutC40Lkg0YfQtdGA0L3Ri9C5INGC0LXRgNGM0LXRgCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTIwfSwiYnJlZWRfZW4iOiJCbGFjayBSdXNzaWFuIFRlcnJpZXIgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiTXVzdCBWZW5lIHRlcmplciDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCg0YPRgdGB0LrQvi3QtdCy0YDQvtC/0LXQudGB0LrQsNGPINC70LDQudC60LAgMjDigJMyOCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiUnVzc2lhbi1FdXJvcGVhbiBMYWlrYSAyMOKAkzI4IGtnIiwiYnJlZWRfZXQiOiJWZW5lLUV1cm9vcGEgbGFpa2EgMjDigJMyOCBrZyJ9LHsiYnJlZWQiOiLQodCw0LzQvtC10LQgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiU2Ftb3llZCAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJTYW1vamVlZCAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCh0LDQvNC+0LXQtCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiU2Ftb3llZCAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJTYW1vamVlZCAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCh0LXRgtGC0LXRgCDQsNC90LPQu9C40LnRgdC60LjQuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTB9LCJicmVlZF9lbiI6IkVuZ2xpc2ggU2V0dGVyIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkluZ2xpc2Ugc2V0dGVyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0KHQtdGC0YLQtdGAINCz0L7RgNC00L7QvSAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwfSwiYnJlZWRfZW4iOiJHb3Jkb24gU2V0dGVyIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkdvcmRvbmkgc2V0dGVyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0KHQtdGC0YLQtdGAINC40YDQu9Cw0L3QtNGB0LrQuNC5IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5MH0sImJyZWVkX2VuIjoiSXJpc2ggU2V0dGVyIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6Iklpcmkgc2V0dGVyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0KHQuNCx0LAt0LjQvdGDIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo3MH0sImJyZWVkX2VuIjoiU2hpYmEgSW51IiwiYnJlZWRfZXQiOiJTaGliYSBJbnUifSx7ImJyZWVkIjoi0KHQuNC70LjRhdC10Lwt0YLQtdGA0YzQtdGAIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiU2VhbHloYW0gVGVycmllciIsImJyZWVkX2V0IjoiU2VhbHloYW1pIHRlcmplciJ9LHsiYnJlZWQiOiLQodC60L7RgtGHLdGC0LXRgNGM0LXRgCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MCwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6IlNjb3R0aXNoIFRlcnJpZXIiLCJicmVlZF9ldCI6IsWgb3RpIHRlcmplciJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdCw0Y8g0LrQsNGA0LvQuNC60L7QstCw0Y8gNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjUwfSwiYnJlZWRfZW4iOiJEYWNoc2h1bmQgc21vb3RoIG1pbmlhdHVyZSA14oCTMTAga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBsw7xoaWthcnZhbGluZSBrw6TDpGJ1cyA14oCTMTAga2cifSx7ImJyZWVkIjoi0KLQsNC60YHQsCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3QsNGPINC60YDQvtC70LjRh9GM0Y8g0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MjUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjozNSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjQ1fSwiYnJlZWRfZW4iOiJEYWNoc2h1bmQgc21vb3RoIHJhYmJpdCB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIgbMO8aGlrYXJ2YWxpbmUga8O8w7xsaWsga3VuaSA1IGtnIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90LDRjyDRgdGC0LDQvdC00LDRgNGC0L3QsNGPIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjB9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCBzbW9vdGggc3RhbmRhcmQgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiVGFrc2lrb2VyIGzDvGhpa2FydmFsaW5lIHN0YW5kYXJkIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KLQsNC60YHQsCDQtNC70LjQvdC90L7RiNC10YDRgdGC0L3QsNGPINC60LDRgNC70LjQutC+0LLQsNGPIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJEYWNoc2h1bmQgbG9uZy1jb2F0ZWQgbWluaWF0dXJlIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiVGFrc2lrb2VyIHBpa2thcnZhbGluZSBrw6TDpGJ1cyA14oCTMTAga2cifSx7ImJyZWVkIjoi0KLQsNC60YHQsCDQtNC70LjQvdC90L7RiNC10YDRgdGC0L3QsNGPINC60YDQvtC70LjRh9GM0Y8g0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCBsb25nLWNvYXRlZCByYWJiaXQgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiVGFrc2lrb2VyIHBpa2thcnZhbGluZSBrw7zDvGxpayBrdW5pIDUga2cifSx7ImJyZWVkIjoi0KLQsNC60YHQsCDQtNC70LjQvdC90L7RiNC10YDRgdGC0L3QsNGPINGB0YLQsNC90LTQsNGA0YLQvdCw0Y8gMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJEYWNoc2h1bmQgbG9uZy1jb2F0ZWQgc3RhbmRhcmQgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiVGFrc2lrb2VyIHBpa2thcnZhbGluZSBzdGFuZGFyZCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90LDRjyDQutCw0YDQu9C40LrQvtCy0LDRjyA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MCwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCB3aXJlLWhhaXJlZCBtaW5pYXR1cmUgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIga2FydWthcnZhbGluZSBrw6TDpGJ1cyA14oCTMTAga2cifSx7ImJyZWVkIjoi0KLQsNC60YHQsCDQttC10YHRgtC60L7RiNC10YDRgdGC0L3QsNGPINC60YDQvtC70LjRh9GM0Y8g0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTUsItCi0YDQuNC80LzQuNC90LMiOjU1fSwiYnJlZWRfZW4iOiJEYWNoc2h1bmQgd2lyZS1oYWlyZWQgcmFiYml0IHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBrYXJ1a2FydmFsaW5lIGvDvMO8bGlrIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINC20LXRgdGC0LrQvtGI0LXRgNGB0YLQvdCw0Y8g0YHRgtCw0L3QtNCw0YDRgtC90LDRjyAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzAsItCi0YDQuNC80LzQuNC90LMiOjc1fSwiYnJlZWRfZW4iOiJEYWNoc2h1bmQgd2lyZS1oYWlyZWQgc3RhbmRhcmQgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiVGFrc2lrb2VyIGthcnVrYXJ2YWxpbmUgc3RhbmRhcmQgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQo9C40L/Qv9C10YIgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDV9LCJicmVlZF9lbiI6IldoaXBwZXQgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiV2hpcHBldCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCj0LjQv9C/0LXRgiAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MH0sImJyZWVkX2VuIjoiV2hpcHBldCAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiJXaGlwcGV0IDE14oCTMjAga2cifSx7ImJyZWVkIjoi0KTQuNC90YHQutC40Lkg0LvQsNC/0YXRg9C90LQgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjg1fSwiYnJlZWRfZW4iOiJGaW5uaXNoIExhcHBodW5kIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IlNvb21lIGxhbWJha29lciAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCk0LjQvdGB0LrQuNC5INC70LDQv9GF0YPQvdC0IDIw4oCTMjQg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5MH0sImJyZWVkX2VuIjoiRmlubmlzaCBMYXBwaHVuZCAyMOKAkzI0IGtnIiwiYnJlZWRfZXQiOiJTb29tZSBsYW1iYWtvZXIgMjDigJMyNCBrZyJ9LHsiYnJlZWQiOiLQpNC+0LrRgdGC0LXRgNGM0LXRgCDQttC10YHRgtC60L7RiNC10YDRgdGC0L3Ri9C5IDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MCwi0KLRgNC40LzQvNC40L3QsyI6NzV9LCJicmVlZF9lbiI6IldpcmUgRm94IFRlcnJpZXIgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiS2FydWthcnZhbGluZSBmb3h0ZXJqZXIgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQpNC+0LrRgdGC0LXRgNGM0LXRgCDQttC10YHRgtC60L7RiNC10YDRgdGC0L3Ri9C5IDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiV2lyZSBGb3ggVGVycmllciA14oCTMTAga2ciLCJicmVlZF9ldCI6IkthcnVrYXJ2YWxpbmUgZm94dGVyamVyIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQpNGA0LDQvdGG0YPQt9GB0LrQuNC5INCx0YPQu9GM0LTQvtCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2MH0sImJyZWVkX2VuIjoiRnJlbmNoIEJ1bGxkb2ciLCJicmVlZF9ldCI6IlByYW50c3VzZSBidWxkb2cifSx7ImJyZWVkIjoi0KXQsNGB0LrQuCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJTaWJlcmlhbiBIdXNreSAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJTaWJlcmkgaHVza3kgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQpdCw0YHQutC4IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJTaWJlcmlhbiBIdXNreSAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJTaWJlcmkgaHVza3kgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQptCy0LXRgNCz0YjQvdCw0YPRhtC10YAgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwLCLQotGA0LjQvNC80LjQvdCzIjo3NX0sImJyZWVkX2VuIjoiTWluaWF0dXJlIFNjaG5hdXplciAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJLw6TDpGJ1c8WhbmF1dHNlciAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCm0LLQtdGA0LPRiNC90LDRg9GG0LXRgCA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MCwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6Ik1pbmlhdHVyZSBTY2huYXV6ZXIgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJLw6TDpGJ1c8WhbmF1dHNlciA14oCTMTAga2cifSx7ImJyZWVkIjoi0KfQsNGDLdGH0LDRgyAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkNob3cgQ2hvdyAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJDaG93IENob3cgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQp9Cw0YMt0YfQsNGDIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IkNob3cgQ2hvdyAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJDaG93IENob3cgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQp9C40YXRg9Cw0YXRg9CwINCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdGL0LkiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MjUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjozNX0sImJyZWVkX2VuIjoiQ2hpaHVhaHVhIHNtb290aCIsImJyZWVkX2V0IjoiVMWhaWh1YWh1YSBsw7xoaWthcnZhbGluZSJ9LHsiYnJlZWQiOiLQp9C40YXRg9Cw0YXRg9CwINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdGL0LkiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkNoaWh1YWh1YSBsb25nLWNvYXRlZCIsImJyZWVkX2V0IjoiVMWhaWh1YWh1YSBwaWtrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0KjQsNGA0L/QtdC5IDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjV9LCJicmVlZF9lbiI6IlNoYXIgUGVpIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IsWgYXItUGVpIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0KjQsNGA0L/QtdC5IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NzB9LCJicmVlZF9lbiI6IlNoYXIgUGVpIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IsWgYXItUGVpIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0KjQtdC70YLQuCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjYwfSwiYnJlZWRfZW4iOiJTaGV0bGFuZCBTaGVlcGRvZyIsImJyZWVkX2V0IjoixaBldGxhbmRpIGxhbWJha29lciJ9LHsiYnJlZWQiOiLQqNC4LdGC0YbRgyA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiU2hpaCBUenUgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJTaGloIFR6dSA14oCTMTAga2cifSx7ImJyZWVkIjoi0KjQuC3RgtGG0YMg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IlNoaWggVHp1IHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IlNoaWggVHp1IGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQqNC90LDRg9GG0LXRgCDQvNC40L3QuNCw0YLRjtGA0L3Ri9C5INC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJNaW5pYXR1cmUgU2NobmF1emVyIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IkvDpMOkYnVzxaFuYXV0c2VyIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQqNC/0LjRhiDQvdC10LzQtdGG0LrQuNC5IC8g0L/QvtC80LXRgNCw0L3RgdC60LjQuSDQsdC+0LvQtdC1IDMsNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjB9LCJicmVlZF9lbiI6Ikdlcm1hbiBTcGl0eiAvIFBvbWVyYW5pYW4gb3ZlciAzLDUga2ciLCJicmVlZF9ldCI6IlNha3NhIHNwaXRzIC8gUG9tZXJhbmlhbiDDvGxlIDMsNSBrZyJ9LHsiYnJlZWQiOiLQqNC/0LjRhiDQvdC10LzQtdGG0LrQuNC5IC8g0L/QvtC80LXRgNCw0L3RgdC60LjQuSDQtNC+IDMsNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NTV9LCJicmVlZF9lbiI6Ikdlcm1hbiBTcGl0eiAvIFBvbWVyYW5pYW4gdXAgdG8gMyw1IGtnIiwiYnJlZWRfZXQiOiJTYWtzYSBzcGl0cyAvIFBvbWVyYW5pYW4ga3VuaSAzLDUga2cifSx7ImJyZWVkIjoi0KjQv9C40YYg0Y/Qv9C+0L3RgdC60LjQuSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjYwfSwiYnJlZWRfZW4iOiJKYXBhbmVzZSBTcGl0eiIsImJyZWVkX2V0IjoiSmFhcGFuaSBzcGl0cyJ9LHsiYnJlZWQiOiLQqdC10L3QutC4Iiwic2VydmljZXMiOnsi0JLRgdGPINC/0YDQvtCz0YDQsNC80LzQsCI6NTV9LCJicmVlZF9lbiI6IlB1cHBpZXMiLCJicmVlZF9ldCI6Ikt1dHNpa2FkIn0seyJicmVlZCI6ItCt0YHRgtC+0L3RgdC60LDRjyDQs9C+0L3Rh9Cw0Y8gMTXigJMyNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTB9LCJicmVlZF9lbiI6IkVzdG9uaWFuIEhvdW5kIDE14oCTMjUga2ciLCJicmVlZF9ldCI6IkVlc3RpIGhhZ2lqYXMgMTXigJMyNSBrZyJ9LHsiYnJlZWQiOiLQr9C/0L7QvdGB0LrQuNC5INGF0LjQvSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiSmFwYW5lc2UgQ2hpbiIsImJyZWVkX2V0IjoiSmFhcGFuaSBDaGluIn0seyJicmVlZCI6ItCa0L7RiNC60LAg0LrQvtGA0L7RgtC60L7RiNC10YDRgdGC0L3QsNGPIiwic2VydmljZXMiOnsi0JLRi9GH0LXRgSI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJDYXQgc2hvcnQtaGFpcmVkIiwiYnJlZWRfZXQiOiJLYXNzIGzDvGhpa2FydmFsaW5lIn0seyJicmVlZCI6ItCa0L7RiNC60LAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90LDRjyIsInNlcnZpY2VzIjp7ItCS0YvRh9C10YEiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4MH0sImJyZWVkX2VuIjoiQ2F0IGxvbmctaGFpcmVkIiwiYnJlZWRfZXQiOiJLYXNzIHBpa2thcnZhbGluZSJ9LHsiYnJlZWQiOiLQmtC+0YjQutCwINCc0LXQudC9LdC60YPQvSIsInNlcnZpY2VzIjp7ItCS0YvRh9GR0YEiOjYwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5MH0sImJyZWVkX2VuIjoiQ2F0IE1haW5lIENvb24iLCJicmVlZF9ldCI6Ikthc3MgTWFpbmUgQ29vbiJ9XTsKdmFyIFJBSUxXQVkgPSAiaHR0cHM6Ly9yamdyb29taW5nLnVwLnJhaWx3YXkuYXBwL2Jvb2siOwp2YXIgR09PR0xFX1NDUklQVCA9ICJodHRwczovL3NjcmlwdC5nb29nbGUuY29tL21hY3Jvcy9zL0FLZnljYnlUU1otZUpNZGVwLUQwTHItbngwX1Y0SEJXZ0lJY3RuUlQycmpTRHZCeWJqNUNZSTNOSzJNcWNBd19jZmN6Z1JFaWZnL2V4ZWMiOwp2YXIgRkFMTEJBQ0tfVElNRVMgPSBbJzEwOjAwJywnMTA6MzAnLCcxMTowMCcsJzExOjMwJywnMTI6MDAnLCcxMjozMCcsJzEzOjAwJywnMTM6MzAnLCcxNDowMCcsJzE0OjMwJywnMTU6MDAnLCcxNTozMCcsJzE2OjAwJywnMTY6MzAnLCcxNzowMCcsJzE3OjMwJywnMTg6MDAnXTsKdmFyIGJvb2tpbmcgPSB7YnJlZWQ6JycsYnJlZWREaXNwbGF5OicnLHNlcnZpY2U6JycscHJpY2U6MCxtYXN0ZXI6JycsZ3Jvb21IaXN0b3J5OicnLGRhdGU6JycsdGltZTonJyxsYW5nOidydSd9Owp2YXIgc2VsQnJlZWQgPSBudWxsOwp2YXIgY1kgPSBuZXcgRGF0ZSgpLmdldEZ1bGxZZWFyKCk7CnZhciBjTSA9IG5ldyBEYXRlKCkuZ2V0TW9udGgoKTsKdmFyIHN0ZXAgPSAxOwp2YXIgTU9OVEhTID0gWyfQr9C90LLQsNGA0YwnLCfQpNC10LLRgNCw0LvRjCcsJ9Cc0LDRgNGCJywn0JDQv9GA0LXQu9GMJywn0JzQsNC5Jywn0JjRjtC90YwnLCfQmNGO0LvRjCcsJ9CQ0LLQs9GD0YHRgicsJ9Ch0LXQvdGC0Y/QsdGA0YwnLCfQntC60YLRj9Cx0YDRjCcsJ9Cd0L7Rj9Cx0YDRjCcsJ9CU0LXQutCw0LHRgNGMJ107CgpmdW5jdGlvbiBzaG93U2NyZWVuKGlkKSB7CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLnNjcmVlbicpLmZvckVhY2goZnVuY3Rpb24ocyl7cy5jbGFzc0xpc3QucmVtb3ZlKCdhY3RpdmUnKTt9KTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZChpZCkuY2xhc3NMaXN0LmFkZCgnYWN0aXZlJyk7CiAgd2luZG93LnNjcm9sbFRvKDAsMCk7Cn0KCmZ1bmN0aW9uIGdvU3RlcChuKSB7CiAgWydiazEnLCdiazInLCdiazMnLCdiazQnLCdiazUnXS5mb3JFYWNoKGZ1bmN0aW9uKGlkLGkpewogICAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoaWQpLmNsYXNzTmFtZSA9ICdzdGVwJyArIChpKzE9PT1uPycgc2hvdyc6JycpOwogIH0pOwogIGZvcih2YXIgaT0xO2k8PTU7aSsrKXsKICAgIHZhciBwcz1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgncHMnK2kpOwogICAgdmFyIHBsPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdwbCcraSk7CiAgICBpZihpPG4pe3BzLmNsYXNzTmFtZT0ncHMgZG9uZSc7aWYocGwpcGwuY2xhc3NOYW1lPSdwbCBkb25lJzt9CiAgICBlbHNlIGlmKGk9PT1uKXtwcy5jbGFzc05hbWU9J3BzIGFjdGl2ZSc7aWYocGwpcGwuY2xhc3NOYW1lPSdwbCc7fQogICAgZWxzZXtwcy5jbGFzc05hbWU9J3BzJztpZihwbClwbC5jbGFzc05hbWU9J3BsJzt9CiAgfQogIHN0ZXA9bjsgd2luZG93LnNjcm9sbFRvKDAsMCk7CiAgaWYobj09PTIpIGZpbHRlck1hc3RlcnMoKTsKfQoKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Jvb2tCdG4nKS5vbmNsaWNrID0gZnVuY3Rpb24oKXsKICBzaG93U2NyZWVuKCdib29rU2NyZWVuJyk7IGdvU3RlcCgxKTsgYnVpbGRDYWwoKTsKfTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2JhY2tCdG4nKS5vbmNsaWNrID0gZnVuY3Rpb24oKXsKICBpZihzdGVwPjEpe2dvU3RlcChzdGVwLTEpO31lbHNle3Nob3dTY3JlZW4oJ2hvbWVTY3JlZW4nKTt9Cn07CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdob21lQnRuJykub25jbGljayA9IGZ1bmN0aW9uKCl7CiAgc2hvd1NjcmVlbignaG9tZVNjcmVlbicpOyByZXNldEFsbCgpOwp9OwoKLy8gQnJlZWQgc2VhcmNoCnZhciBpbnAgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnYklucHV0Jyk7CnZhciBkcm9wID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2JEcm9wJyk7CnZhciBjbHIgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2xyQnRuJyk7CnZhciBiYWRnZSA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzQmFkZ2UnKTsKCmlucC5hZGRFdmVudExpc3RlbmVyKCdpbnB1dCcsIGZ1bmN0aW9uKCl7CiAgdmFyIHEgPSBpbnAudmFsdWUudHJpbSgpOwogIGNsci5jbGFzc0xpc3QudG9nZ2xlKCdzaG93JywgcS5sZW5ndGg+MCk7CiAgaWYoIXEpe2Ryb3AuY2xhc3NMaXN0LnJlbW92ZSgnb3BlbicpO2Ryb3AuaW5uZXJIVE1MPScnO3JldHVybjt9CiAgdmFyIHNmPUxBTkc9PT0nZW4nPydicmVlZF9lbic6TEFORz09PSdldCc/J2JyZWVkX2V0JzonYnJlZWQnOwogIHZhciByZXM9REFUQS5maWx0ZXIoZnVuY3Rpb24oYil7cmV0dXJuKGJbc2ZdfHxiLmJyZWVkKS50b0xvd2VyQ2FzZSgpLmluZGV4T2YocS50b0xvd2VyQ2FzZSgpKSE9PS0xO30pLnNsaWNlKDAsMzUpOwogIGRyb3AuaW5uZXJIVE1MPScnOwogIHZhciBfbnI9TEFORz09PSdlbic/J0JyZWVkIG5vdCBmb3VuZCc6TEFORz09PSdldCc/J1TDtXVndSBlaSBsZWl0dWQnOifQn9C+0YDQvtC00LAg0L3QtSDQvdCw0LnQtNC10L3QsCc7CiAgdmFyIF9udD1MQU5HPT09J2VuJz8iQ2FuJ3QgZmluZCB5b3VyIGJyZWVkPyI6TEFORz09PSdldCc/J0VpIGxlaWEgb21hIHTDtXVndT8nOifQndC1INC90LDRiNC70Lgg0YHQstC+0Y4g0L/QvtGA0L7QtNGDPyc7CiAgdmFyIF9ucz1MQU5HPT09J2VuJz8nQ29udGFjdCB1cyDigJQgd2Ugd2lsbCBoZWxwIHlvdSBjaG9vc2UgYSBzZXJ2aWNlJzpMQU5HPT09J2V0Jz8nVsO1dGtlIG1laWVnYSDDvGhlbmR1c3Qg4oCUIGFpdGFtZSB0ZWVudXNlIHZhbGlkYSc6J9Ch0LLRj9C20LjRgtC10YHRjCDRgSDQvdCw0LzQuCDQu9GO0LHRi9C8INGD0LTQvtCx0L3Ri9C8INGB0L/QvtGB0L7QsdC+0Lwg4oCUINC80Ysg0L/QvtC80L7QttC10Lwg0L/QvtC00L7QsdGA0LDRgtGMINGD0YHQu9GD0LPRgyc7CiAgaWYoIXJlcy5sZW5ndGgpe2Ryb3AuaW5uZXJIVE1MPSc8ZGl2IGNsYXNzPSJub3JlcyI+JytfbnIrJzwvZGl2PjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lciIgb25jbGljaz0ic2hvd1NjcmVlbihcJ2hvbWVTY3JlZW5cJykiPjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci1pY29uIj7wn5C+PC9kaXY+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLXRleHQiPjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci10aXRsZSI+JytfbnQrJzwvZGl2PjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci1zdWIiPicrX25zKyc8L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItYXJyb3ciPuKGkjwvZGl2PjwvZGl2Pic7fQogIGVsc2V7CiAgICByZXMuZm9yRWFjaChmdW5jdGlvbihiKXsKICAgICAgdmFyIGQ9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7IGQuY2xhc3NOYW1lPSdkaXRlbSc7CiAgICAgIHZhciBibmFtZT1iW3NmXXx8Yi5icmVlZDsKICAgICAgdmFyIGlkeD1ibmFtZS50b0xvd2VyQ2FzZSgpLmluZGV4T2YocS50b0xvd2VyQ2FzZSgpKTsKICAgICAgZC5pbm5lckhUTUw9Ym5hbWUuc3Vic3RyaW5nKDAsaWR4KSsnPG1hcms+JytibmFtZS5zdWJzdHJpbmcoaWR4LGlkeCtxLmxlbmd0aCkrJzwvbWFyaz4nK2JuYW1lLnN1YnN0cmluZyhpZHgrcS5sZW5ndGgpOwogICAgICBkLm9uY2xpY2s9ZnVuY3Rpb24oKXtzZWxlY3RCcmVlZChiKTt9OwogICAgICBkcm9wLmFwcGVuZENoaWxkKGQpOwogICAgfSk7CiAgfQogIGRyb3AuY2xhc3NMaXN0LmFkZCgnb3BlbicpOwp9KTsKCmRvY3VtZW50LmFkZEV2ZW50TGlzdGVuZXIoJ2NsaWNrJyxmdW5jdGlvbihlKXsKICBpZighZS50YXJnZXQuY2xvc2VzdCgnLmJ3cmFwJykpZHJvcC5jbGFzc0xpc3QucmVtb3ZlKCdvcGVuJyk7Cn0pOwpjbHIub25jbGljayA9IHJlc2V0QnJlZWQ7CgpmdW5jdGlvbiBzZWxlY3RCcmVlZChiKXsKICBzZWxCcmVlZD1iOyBib29raW5nLmJyZWVkPWIuYnJlZWQ7CiAgaW5wLnZhbHVlPScnOyBjbHIuY2xhc3NMaXN0LnJlbW92ZSgnc2hvdycpOwogIGRyb3AuY2xhc3NMaXN0LnJlbW92ZSgnb3BlbicpOyBkcm9wLmlubmVySFRNTD0nJzsKICBiYWRnZS5pbm5lckhUTUw9Jyc7CiAgdmFyIGJGaWVsZD1MQU5HPT09J2VuJz8nYnJlZWRfZW4nOkxBTkc9PT0nZXQnPydicmVlZF9ldCc6J2JyZWVkJzsKICB2YXIgZGlzcEJyZWVkPWJbYkZpZWxkXXx8Yi5icmVlZDsKICBib29raW5nLmJyZWVkRGlzcGxheT1kaXNwQnJlZWQ7CiAgdmFyIGJuPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NwYW4nKTtibi5jbGFzc05hbWU9J2JuYW1lJztibi50ZXh0Q29udGVudD1kaXNwQnJlZWQ7CiAgdmFyIGNoZ1R4dD1MQU5HPT09J2VuJz8nQ2hhbmdlJzpMQU5HPT09J2V0Jz8nTXV1ZGEnOifQmNC30LzQtdC90LjRgtGMJzsKICB2YXIgYmM9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnc3BhbicpO2JjLmNsYXNzTmFtZT0nYmNoZyc7YmMudGV4dENvbnRlbnQ9Y2hnVHh0OwogIGJjLm9uY2xpY2s9cmVzZXRCcmVlZDsKICBiYWRnZS5hcHBlbmRDaGlsZChibik7YmFkZ2UuYXBwZW5kQ2hpbGQoYmMpOwogIGJhZGdlLmNsYXNzTGlzdC5hZGQoJ3Nob3cnKTsKICByZW5kZXJTdmNzKGIpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdmNTZWMnKS5zdHlsZS5kaXNwbGF5PSdibG9jayc7CiAgICAvLyBBZGQgaW1wb3J0YW50IG5vdGUgaWYgbm90IGV4aXN0cwogICAgaWYoIWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdmNOb3RlJykpewogICAgICB2YXIgbm90ZT1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdkaXYnKTsKICAgICAgbm90ZS5pZD0nc3ZjTm90ZSc7CiAgICAgIG5vdGUuc3R5bGUuY3NzVGV4dD0nYm9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wOCk7cGFkZGluZzoxNHB4IDE2cHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wMik7bWFyZ2luLXRvcDoxMnB4Oyc7CiAgICAgIHZhciBub3RlVGl0bGU9TEFORz09PSdlbic/J1BsZWFzZSBub3RlJzpMQU5HPT09J2V0Jz8nUGFuZ2UgdMOkaGVsZSc6J9CS0LDQttC90L4g0LfQvdCw0YLRjCc7CiAgICAgIHZhciBub3RlQm9keT1MQU5HPT09J2VuJz8nRmluYWwgcHJpY2UgZGVwZW5kcyBvbiBjb2F0IGNvbmRpdGlvbiBhbmQgcGV0IGJlaGF2aW91ci48YnI+RGVtYXR0aW5nIGZyb20gNSDigqwuPGJyPkFnZ3Jlc3NpdmUgYmVoYXZpb3VyIHN1cmNoYXJnZSBtYXkgYXBwbHk6ICs1MCUuJzpMQU5HPT09J2V0Jz8nTMO1cGxpayBoaW5kIHPDtWx0dWIga2FydmFzdGlrdSBzZWlzdW5kaXN0IGphIGxlbW1pa2xvb21hIGvDpGl0dW1pc2VzdC48YnI+S29sdHN1bml0ZSBsYWh0aWhhcnV0YW1pbmUgYWxhdGVzIDUg4oKsLjxicj5BZ3Jlc3NpaXZzZSBrw6RpdHVtaXNlIGtvcnJhbCB2w7VpYiBsaXNhbmR1ZGEgNTAlIGp1dXJkZWhpbmRsdXMuJzon0J7QutC+0L3Rh9Cw0YLQtdC70YzQvdCw0Y8g0YHRgtC+0LjQvNC+0YHRgtGMINC30LDQstC40YHQuNGCINC+0YIg0YHQvtGB0YLQvtGP0L3QuNGPINGI0LXRgNGB0YLQuCDQuCDQv9C+0LLQtdC00LXQvdC40Y8g0L/QuNGC0L7QvNGG0LAuPGJyPtCg0LDQt9Cx0L7RgCDQutC+0LvRgtGD0L3QvtCyIOKAlCDQvtGCIDUg4oKsLjxicj7Qn9GA0Lgg0LDQs9GA0LXRgdGB0LjQstC90L7QvCDQv9C+0LLQtdC00LXQvdC40Lgg0LzQvtC20LXRgiDQv9GA0LjQvNC10L3Rj9GC0YzRgdGPINC00L7Qv9C70LDRgtCwIDUwJS4nOwogICAgICBub3RlLmlubmVySFRNTD0nPGRpdiBzdHlsZT0iZm9udC1zaXplOjAuODM4cmVtO2xldHRlci1zcGFjaW5nOi4xNWVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO21hcmdpbi1ib3R0b206OHB4O2ZvbnQtd2VpZ2h0OjYwMDtmb250LWZhbWlseTpcJ01vbnRzZXJyYXRcJyxzYW5zLXNlcmlmIj4nK25vdGVUaXRsZSsnPC9kaXY+PGRpdiBzdHlsZT0iZm9udC1zaXplOjEuMDI1cmVtO2NvbG9yOiNmZmZmZmY7bGluZS1oZWlnaHQ6MS44O2ZvbnQtZmFtaWx5OlwnTW9udHNlcnJhdFwnLHNhbnMtc2VyaWYiPicrbm90ZUJvZHkrJzwvZGl2Pic7CiAgICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdmNTZWMnKS5hcHBlbmRDaGlsZChub3RlKTsKICAgIH0KICBmaWx0ZXJNYXN0ZXJzKCk7Cn0KCmZ1bmN0aW9uIHJlc2V0QnJlZWQoKXsKICBzZWxCcmVlZD1udWxsO2Jvb2tpbmcuYnJlZWQ9Jyc7Ym9va2luZy5zZXJ2aWNlPScnO2Jvb2tpbmcucHJpY2U9MDsKICBpbnAudmFsdWU9Jyc7Y2xyLmNsYXNzTGlzdC5yZW1vdmUoJ3Nob3cnKTsKICBiYWRnZS5jbGFzc0xpc3QucmVtb3ZlKCdzaG93Jyk7YmFkZ2UuaW5uZXJIVE1MPScnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdmNTZWMnKS5zdHlsZS5kaXNwbGF5PSdub25lJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjTGlzdCcpLmlubmVySFRNTD0nJzsKfQoKCnZhciBTVkNfVFJBTlNMQVRJT05TID0gewogICfQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCc6ICAgICAge2VuOidCYXNpYyBncm9vbScsICAgICAgZXQ6J1DDtWhpaG9vbGR1cyd9LAogICfQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCc6e2VuOidIeWdpZW5lIGdyb29tJywgICAgZXQ6J0jDvGdpZWVuaWhvb2xkdXMnfSwKICAn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc6ICB7ZW46J0Z1bGwgZ3Jvb20nLCAgICAgICAgZXQ6J1TDpGllbGlrIGhvb2xkdXMnfSwKICAn0KLRgNC40LzQvNC40L3Qsyc6ICAgICAgICAgIHtlbjonVHJpbW1pbmcnLCAgICAgICAgICBldDonVHJpbW1lcmltaW5lJ30sCiAgJ9Ct0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwJzogICB7ZW46J0V4cHJlc3Mgc2hlZCcsICAgICAgZXQ6J0tpaXJrYXJ2YXZhaGV0dXMnfSwKICAn0JLRi9GH0LXRgSc6ICAgICAgICAgICAgIHtlbjonQnJ1c2gtb3V0JywgICAgICAgICBldDonSGFyamFtaW5lJ30sCiAgJ9CS0YHRjyDQv9GA0L7Qs9GA0LDQvNC80LAnOiAgICAge2VuOidGdWxsIHByb2dyYW0nLCAgICAgIGV0OidLb2d1IHByb2dyYW1tJ30KfTsKdmFyIFNWQ19UQUdMSU5FX0kxOE49ewogIHJ1Onsn0JLRi9GH0LXRgSc6J9Ch0YLQvtC40LzQvtGB0YLRjCDQt9Cw0LLQuNGB0LjRgiDQvtGCINGB0L7RgdGC0L7Rj9C90LjRjyDRiNC10YDRgdGC0Lgg0Lgg0L7QsdGK0ZHQvNCwINGA0LDQsdC+0YInLCfQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCc6J9Cf0L7QtNGF0L7QtNC40YIg0LTQu9GPINC/0L7QtNC00LXRgNC20LDQvdC40Y8g0YfQuNGB0YLQvtGC0Ysg0LzQtdC20LTRgyDQv9GA0L7RhtC10LTRg9GA0LDQvNC4Jywn0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQnOifQlNC70Y8g0LrQvtC80YTQvtGA0YLQsCDQuCDQsNC60LrRg9GA0LDRgtC90L7RgdGC0Lgg0L/QuNGC0L7QvNGG0LAnLCfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Jzon0J/QvtC70L3Ri9C5INGD0YXQvtC0INGB0L4g0YHRgtGA0LjQttC60L7QuScsJ9Ct0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwJzon0J/QvtC80L7Qs9Cw0LXRgiDRg9C80LXQvdGM0YjQuNGC0Ywg0LrQvtC70LjRh9C10YHRgtCy0L4g0LvQuNC90Y/RjtGJ0LXQuSDRiNC10YDRgdGC0LgnLCfQotGA0LjQvNC80LjQvdCzJzon0JTQu9GPINC20LXRgdGC0LrQvtGI0LXRgNGB0YLQvdGL0YUg0L/QvtGA0L7QtCd9LAogIGVuOnsn0JLRi9GH0LXRgSc6J1ByaWNlIGRlcGVuZHMgb24gY29hdCBjb25kaXRpb24gYW5kIHZvbHVtZSBvZiB3b3JrJywn0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQnOidJZGVhbCBmb3IgbWFpbnRhaW5pbmcgY2xlYW5saW5lc3MgYmV0d2VlbiBmdWxsIGdyb29tcycsJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0JzonRm9yIHlvdXIgcGV0XCdzIGNvbWZvcnQgYW5kIG5lYXRuZXNzJywn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc6J0Z1bGwgZ3Jvb21pbmcgd2l0aCBoYWlyY3V0Jywn0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAnOidTaWduaWZpY2FudGx5IHJlZHVjZXMgc2hlZGRpbmcnLCfQotGA0LjQvNC80LjQvdCzJzonRm9yIHdpcmUtaGFpcmVkIGJyZWVkcyd9LAogIGV0Onsn0JLRi9GH0LXRgSc6J0hpbmQgc8O1bHR1YiBrYXJ2YXN0aWt1IHNlaXN1bmRpc3QgamEgdMO2w7ZtYWh1c3QnLCfQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCc6J1NvYmliIHB1aHR1c2UgaG9pZG1pc2VrcyBwcm90c2VkdXVyaWRlIHZhaGVsJywn0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQnOidMZW1taWtsb29tYSBtdWdhdnVzZWtzIGphIGtvcnJhc2hvaXVrcycsJ9Ca0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQnOidUw6RpZWxpayBob29sZHVzIGtvb3MgbMO1aWt1c2VnYScsJ9Ct0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwJzonVsOkaGVuZGFiIG9sdWxpc2VsdCBrYXJ2YWRlIGxhbmdlbWlzdCcsJ9Ci0YDQuNC80LzQuNC90LMnOidUcmFhdGthcnZhbGlzdGVsZSB0w7V1Z3VkZWxlJ30KfTsKdmFyIFNWQ19ERVNDX0kxOE49ewogIHJ1Onsn0JLRi9GH0LXRgSc6J9Cn0LjRgdGC0LrQsCDQs9C70LDQtywg0YPRiNC10LksINC/0L7QtNGB0YLRgNC40LPQsNC90LjQtSDQutC+0LPRgtC10LksINCy0YvRh9GR0YEgKNC00LvRjyDQutC+0YjQtdC6KScsJ9CR0LDQt9C+0LLRi9C5INGD0YXQvtC0Jzon0JzRi9GC0YzRkSDQv9GA0L7RhNC10YHRgdC40L7QvdCw0LvRjNC90YvQvNC4INGB0YDQtdC00YHRgtCy0LDQvNC4LCDQtNC10LvQuNC60LDRgtC90LDRjyDRgdGD0YjQutCwJywn0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQnOifQodGC0YDQuNC20LrQsCDQutC+0LPRgtC10LksINGH0LjRgdGC0LrQsCDRg9GI0LXQuSDQuCDQs9C70LDQtywg0LrRg9C/0LDQvdC40LUsINGB0YPRiNC60LAsINGD0YXQvtC0INC30LAg0LvQsNC/0LrQsNC80Lgg0Lgg0YfRg9Cy0YHRgtCy0LjRgtC10LvRjNC90YvQvNC4INC30L7QvdCw0LzQuCcsJ9Ca0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQnOifQodGC0YDQuNC20LrQsCDQutC+0LPRgtC10LksINGH0LjRgdGC0LrQsCDRg9GI0LXQuSDQuCDQs9C70LDQtywg0LrRg9C/0LDQvdC40LUsINGB0YPRiNC60LAsINGD0YXQvtC0INC30LAg0LvQsNC/0LrQsNC80Lgg0Lgg0YfRg9Cy0YHRgtCy0LjRgtC10LvRjNC90YvQvNC4INC30L7QvdCw0LzQuCwg0LzQvtC00LXQu9GM0L3QsNGPINGB0YLRgNC40LbQutCwJywn0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAnOifQnNGL0YLRjNGRLCDRgdGD0YjQutCwLCDRg9GF0L7QtCDQt9CwINGI0LXRgNGB0YLRjNGOLCDQvNCw0YHQutCwLCDQv9C+0LTRgdGC0YDQuNCz0LDQvdC40LUg0LrQvtCz0YLQtdC5LCDRh9C40YHRgtC60LAg0YPRiNC10Lkg0Lgg0LPQu9Cw0LcsINGD0YXQvtC0INC30LAg0LvQsNC/0LDQvNC4INC4INC30L7QvdCw0LzQuCDRgtGA0LXQsdGD0Y7RidC40LzQuCDQvtGB0L7QsdC+0LPQviDQstC90LjQvNCw0L3QuNGPJywn0KLRgNC40LzQvNC40L3Qsyc6J9CS0YvRidC40L/Ri9Cy0LDQvdC40LUg0YHRgtCw0YDQvtCz0L4g0YHQu9C+0Y8g0YjQtdGA0YHRgtC4LCDQvNGL0YLRjNGRLCDRgdGD0YjQutCwLCDRgdGC0YDQuNC20LrQsCDQutC+0LPRgtC10LksINGH0LjRgdGC0LrQsCDRg9GI0LXQuSDQuCDQs9C70LDQtywg0L7RhNC+0YDQvNC70LXQvdC40LUg0YjQtdGA0YHRgtC4Jywn0JLRgdGPINC/0YDQvtCz0YDQsNC80LzQsCc6J9Cf0JXQoNCS0KvQmSDQktCY0JfQmNCiICgyMC0zMCDQvNC40L0pIOKAlCAyMCDigqxcbuKAoiDQt9C90LDQutC+0LzRgdGC0LLQviDRgdC+INGB0YLQvtC70L7QvCDQuCDQuNC90YHRgtGA0YPQvNC10L3RgtCw0LzQuFxu4oCiINC70ZHQs9C60L7QtSDQstGL0YfRkdGB0YvQstCw0L3QuNC1XG7igKIg0LfQstGD0LrQuCDRhNC10L3QsCDQuCDQu9C10LPQutCw0Y8g0L/RgNC+0LTRg9Cy0LrQsFxu4oCiINC+0YHQstC10LbQtdC90LjQtSDQs9C70LDQt9C+0Log0Lgg0YPRiNC10LpcbuKAoiDQutC+0LPQvtGC0LrQuFxu4oCiINCy0LrRg9GB0L3Rj9GI0LrQuCDQuCDRgdC/0L7QutC+0LnQvdCw0Y8g0LDQtNCw0L/RgtCw0YbQuNGPXG5cbtCS0KLQntCg0J7QmSDQktCY0JfQmNCiICg0MC02MCDQvNC40L0pIOKAlCAzNSDigqxcbuKAoiDQv9C10YDQstC+0LUg0LrRg9C/0LDQvdC40LUg0Lgg0YHRg9GI0LrQsFxu4oCiINCy0YvRh9GR0YHRi9Cy0LDQvdC40LVcbuKAoiDQs9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtFxu4oCiINC90LXQsdC+0LvRjNGI0LDRjyDRgdGC0YDQuNC20LrQsCAvINC60L7RgNGA0LXQutGG0LjRjyDRiNC10YDRgdGC0LggKNC/0YDQuCDQvdC10L7QsdGF0L7QtNC40LzQvtGB0YLQuClcbuKAoiDQt9Cw0LrRgNC10L/Qu9C10L3QuNC1INC/0L7Qu9C+0LbQuNGC0LXQu9GM0L3QvtCz0L4g0L7Qv9GL0YLQsCd9LAogIGVuOnsn0JLRi9GH0LXRgSc6J0V5ZSBhbmQgZWFyIGNsZWFuaW5nLCBuYWlsIHRyaW1taW5nLCBicnVzaGluZyAoZm9yIGNhdHMpJywn0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQnOidXYXNoaW5nIHdpdGggcHJvZmVzc2lvbmFsIHByb2R1Y3RzLCBnZW50bGUgZHJ5aW5nJywn0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQnOidOYWlsIHRyaW1taW5nLCBlYXIgYW5kIGV5ZSBjbGVhbmluZywgYmF0aGluZywgZHJ5aW5nLCBwYXcgYW5kIHNlbnNpdGl2ZSBhcmVhIGNhcmUnLCfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0JzonTmFpbCB0cmltbWluZywgZWFyIGFuZCBleWUgY2xlYW5pbmcsIGJhdGhpbmcsIGRyeWluZywgcGF3IGFuZCBzZW5zaXRpdmUgYXJlYSBjYXJlLCBzdHlsaW5nIGhhaXJjdXQnLCfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6J1dhc2hpbmcsIGRyeWluZywgY29hdCBjYXJlLCBtYXNrLCBuYWlsIHRyaW1taW5nLCBlYXIgYW5kIGV5ZSBjbGVhbmluZywgcGF3IGFuZCBzcGVjaWFsIGFyZWEgY2FyZScsJ9Ci0YDQuNC80LzQuNC90LMnOidSZW1vdmluZyBvbGQgY29hdCBsYXllciwgd2FzaGluZywgZHJ5aW5nLCBuYWlsIHRyaW1taW5nLCBlYXIgYW5kIGV5ZSBjbGVhbmluZywgY29hdCBzdHlsaW5nJywn0JLRgdGPINC/0YDQvtCz0YDQsNC80LzQsCc6J0ZJUlNUIFZJU0lUICgyMC0zMCBtaW4pIOKAlCDigqwyMFxu4oCiIGdldHRpbmcgdXNlZCB0byB0aGUgdGFibGUgYW5kIHRvb2xzXG7igKIgZ2VudGxlIGJydXNoaW5nXG7igKIgZHJ5ZXIgc291bmRzIGFuZCBsaWdodCBhaXJmbG93XG7igKIgZXllIGFuZCBlYXIgcmVmcmVzaFxu4oCiIG5haWwgdHJpbVxu4oCiIHRyZWF0cyBhbmQgY2FsbSBhZGFwdGF0aW9uXG5cblNFQ09ORCBWSVNJVCAoNDAtNjAgbWluKSDigJQg4oKsMzVcbuKAoiBmaXJzdCBiYXRoIGFuZCBkcnlpbmdcbuKAoiBicnVzaGluZ1xu4oCiIGh5Z2llbmUgY2FyZVxu4oCiIGxpZ2h0IHRyaW0gLyBjb2F0IGFkanVzdG1lbnQgKGlmIG5lZWRlZClcbuKAoiByZWluZm9yY2luZyB0aGUgcG9zaXRpdmUgZXhwZXJpZW5jZSd9LAogIGV0Onsn0JLRi9GH0LXRgSc6J1NpbG1hZGUgamEga8O1cnZhZGUgcHVoYXN0YW1pbmUsIGvDvMO8bnRlIGzDtWlrYW1pbmUsIGhhcmphbWluZSAoa2Fzc2lkZWxlKScsJ9CR0LDQt9C+0LLRi9C5INGD0YXQvtC0JzonUGVzZW1pbmUgcHJvZmVzc2lvbmFhbHNldGUgdmFoZW5kaXRlZ2EsIMO1cm4ga3VpdmF0YW1pbmUnLCfQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCc6J0vDvMO8bnRlIGzDtWlrYW1pbmUsIGvDtXJ2YWRlIGphIHNpbG1hZGUgcHVoYXN0YW1pbmUsIHBlc2VtaW5lLCBrdWl2YXRhbWluZSwga8OkcHBhZGUgamEgdHVuZGxpa2UgcGlpcmtvbmRhZGUgaG9vbGR1cycsJ9Ca0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQnOidLw7zDvG50ZSBsw7Vpa2FtaW5lLCBrw7VydmFkZSBqYSBzaWxtYWRlIHB1aGFzdGFtaW5lLCBwZXNlbWluZSwga3VpdmF0YW1pbmUsIGvDpHBwYWRlIGphIHR1bmRsaWtlIHBpaXJrb25kYWRlIGhvb2xkdXMsIG1vZGVsbMO1aWt1cycsJ9Ct0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwJzonUGVzZW1pbmUsIGt1aXZhdGFtaW5lLCBrYXJ2YXN0aWt1IGhvb2xkdXMsIG1hc2ssIGvDvMO8bnRlIGzDtWlrYW1pbmUsIGvDtXJ2YWRlIGphIHNpbG1hZGUgcHVoYXN0YW1pbmUsIGvDpHBwYWRlIGphIGVyaWxpc3RlIHBpaXJrb25kYWRlIGhvb2xkdXMnLCfQotGA0LjQvNC80LjQvdCzJzonVmFuYSBrYXJ2YWtpaGkgZWVtYWxkYW1pbmUsIHBlc2VtaW5lLCBrdWl2YXRhbWluZSwga8O8w7xudGUgbMO1aWthbWluZSwga8O1cnZhZGUgamEgc2lsbWFkZSBwdWhhc3RhbWluZSwga2FydmFzdGlrdSBrdWp1bmRhbWluZScsJ9CS0YHRjyDQv9GA0L7Qs9GA0LDQvNC80LAnOidFU0lNRU5FIEvDnExBU1RVUyAoMjAtMzAgbWluKSDigJQgMjAg4oKsXG7igKIgdHV0dnVtaW5lIGxhdWFnYSBqYSB0w7bDtnJpaXN0YWRlZ2FcbuKAoiBrZXJnZSBoYXJqYW1pbmVcbuKAoiBmw7bDtm5paGVsaWQgamEga2VyZ2Ugw7VodXZvb2xcbuKAoiBzaWxtYWRlIGphIGvDtXJ2YWRlIHbDpHJza2VuZHVzXG7igKIga8O8w7xudGUgbMO1aWthbWluZVxu4oCiIG1haXVzZWQgamEgcmFodWxpayBrb2hhbmVtaW5lXG5cblRFSU5FIEvDnExBU1RVUyAoNDAtNjAgbWluKSDigJQgMzUg4oKsXG7igKIgZXNpbWVuZSB2YW5uaXRhbWluZSBqYSBrdWl2YXRhbWluZVxu4oCiIGhhcmphbWluZVxu4oCiIGjDvGdpZWVuaWhvb2xkdXNcbuKAoiBrZXJnZSBsw7Vpa3VzIC8ga2FydmEga29ycmlnZWVyaW1pbmUgKHZhamFkdXNlbClcbuKAoiBwb3NpdGlpdnNlIGtvZ2VtdXNlIGtpbm5pc3RhbWluZSd9Cn07CnZhciBTVkNfREVTQ19DQVRfQ09NUExFWD17CiAgcnU6J9Cc0YvRgtGM0ZEsINGB0YPRiNC60LAsINCy0YvRh9GR0YHRi9Cy0LDQvdC40LUsINGB0YLRgNC40LbQutCwINC60L7Qs9GC0LXQuSwg0LAg0YLQsNC60LbQtSDQvtCx0YDQsNCx0L7RgtC60LAg0LPQu9Cw0Lcg0Lgg0YPRiNC10LonLAogIGVuOidXYXNoaW5nLCBkcnlpbmcsIGJydXNoaW5nLCBuYWlsIHRyaW1taW5nLCBhbmQgZXllIGFuZCBlYXIgY2FyZScsCiAgZXQ6J1Blc2VtaW5lLCBrdWl2YXRhbWluZSwgaGFyamFtaW5lLCBrw7zDvG50ZSBsw7Vpa2FtaW5lIG5pbmcgc2lsbWFkZSBqYSBrw7VydmFkZSBob29sZHVzJwp9OwpmdW5jdGlvbiBnZXRTdmNUYWcobmFtZSl7cmV0dXJuKFNWQ19UQUdMSU5FX0kxOE5bTEFOR10mJlNWQ19UQUdMSU5FX0kxOE5bTEFOR11bbmFtZV0pfHxTVkNfVEFHTElORV9JMThOLnJ1W25hbWVdfHwnJzt9CmZ1bmN0aW9uIGdldFN2Y0Rlc2MobmFtZSl7CiAgaWYobmFtZT09PSfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0JyAmJiBib29raW5nLmJyZWVkICYmIGJvb2tpbmcuYnJlZWQuaW5kZXhPZign0JrQvtGI0LrQsCcpPT09MCl7CiAgICB2YXIgZD1TVkNfREVTQ19DQVRfQ09NUExFWFtMQU5HXXx8U1ZDX0RFU0NfQ0FUX0NPTVBMRVgucnU7CiAgICByZXR1cm4gZDsKICB9CiAgcmV0dXJuKFNWQ19ERVNDX0kxOE5bTEFOR10mJlNWQ19ERVNDX0kxOE5bTEFOR11bbmFtZV0pfHxTVkNfREVTQ19JMThOLnJ1W25hbWVdfHwnJzsKfQoKZnVuY3Rpb24gcmVuZGVyU3ZjcyhiKXsKICB2YXIgbGJsRWw9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N0ZXAyTGJsRWwnKTsKICBpZihsYmxFbCl7CiAgICB2YXIgYmFzZUxibD0oVFtMQU5HXSYmVFtMQU5HXS5zdGVwMl9sYmwpfHwnMDIgwrcg0KPRgdC70YPQs9CwJzsKICAgIGxibEVsLnRleHRDb250ZW50PShiLmJyZWVkPT09J9Cp0LXQvdC60LgnKT8oYmFzZUxibCsnIFB1cHB5IFN0YXInKTpiYXNlTGJsOwogIH0KICB2YXIgbGlzdD1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjTGlzdCcpO2xpc3QuaW5uZXJIVE1MPScnOwogIE9iamVjdC5lbnRyaWVzKGIuc2VydmljZXMpLmZvckVhY2goZnVuY3Rpb24oa3YpewogICAgdmFyIG5hbWU9a3ZbMF0scHJpY2U9a3ZbMV07CgogICAgdmFyIGRpc3BsYXlOYW1lPShMQU5HIT09J3J1JyYmU1ZDX1RSQU5TTEFUSU9OU1tuYW1lXSk/U1ZDX1RSQU5TTEFUSU9OU1tuYW1lXVtMQU5HXTpuYW1lOwogICAgdmFyIGJ0bj1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdidXR0b24nKTtidG4uY2xhc3NOYW1lPSdzdmJ0bic7CiAgICB2YXIgcm93PWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO3Jvdy5jbGFzc05hbWU9J3N2YnRuLXJvdyc7CiAgICB2YXIgbnM9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnc3BhbicpO25zLmNsYXNzTmFtZT0nc3ZidG4tbmFtZSc7bnMudGV4dENvbnRlbnQ9ZGlzcGxheU5hbWU7CiAgICB2YXIgcHM9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnc3BhbicpO3BzLmNsYXNzTmFtZT0nc3ZidG4tcHJpY2UnO3BzLnRleHRDb250ZW50PXByaWNlKycg4oKsJzsKICAgIHJvdy5hcHBlbmRDaGlsZChucyk7cm93LmFwcGVuZENoaWxkKHBzKTsKICAgIGJ0bi5hcHBlbmRDaGlsZChyb3cpOwogICAgdmFyIGRlc2M9Z2V0U3ZjRGVzYyhuYW1lKTsKICAgIGlmKGRlc2Mpe3ZhciBkcz1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdzcGFuJyk7ZHMuY2xhc3NOYW1lPSdzdmJ0bi1kZXNjJztkcy50ZXh0Q29udGVudD1kZXNjO2J0bi5hcHBlbmRDaGlsZChkcyk7fQogICAgdmFyIHRhZz1nZXRTdmNUYWcobmFtZSk7CiAgICBpZih0YWcpe3ZhciB0cz1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdzcGFuJyk7dHMuY2xhc3NOYW1lPSdzdmJ0bi10YWcnO3RzLnRleHRDb250ZW50PXRhZztidG4uYXBwZW5kQ2hpbGQodHMpO30KICAgIGJ0bi5vbmNsaWNrPWZ1bmN0aW9uKCl7CiAgICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5zdmJ0bicpLmZvckVhY2goZnVuY3Rpb24oYil7Yi5jbGFzc0xpc3QucmVtb3ZlKCdhY3RpdmUnKTt9KTsKICAgICAgYnRuLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpOwogICAgICBib29raW5nLnNlcnZpY2U9bmFtZTtib29raW5nLnByaWNlPXByaWNlOwogICAgICBmaWx0ZXJNYXN0ZXJzKCk7CiAgICAgIHNldFRpbWVvdXQoZnVuY3Rpb24oKXtnb1N0ZXAoMik7fSwzMDApOwogICAgfTsKICAgIGxpc3QuYXBwZW5kQ2hpbGQoYnRuKTsKICB9KTsKfQoKLy8gTWFzdGVycwpmdW5jdGlvbiBmaWx0ZXJNYXN0ZXJzKCl7CiAgdmFyIGlzQ2F0ID0gYm9va2luZy5icmVlZCAmJiBib29raW5nLmJyZWVkLmluZGV4T2YoJ9Ca0L7RiNC60LAnKSA9PT0gMDsKICB2YXIgYnJlZWQgPSBib29raW5nLmJyZWVkIHx8ICcnOwogIHZhciBpc0NhdENvbXBsZXggPSBpc0NhdCAmJiBib29raW5nLnNlcnZpY2UgPT09ICfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0JzsKICB2YXIgYW5uYUV4Y2x1ZGUgPSBbJ9Cc0LDQu9GM0YLQuNC/0YMnLCfQn9GD0LTQtdC70YwnLCfQmdC+0YDQuicsJ9CR0LjRiNC+0L0nLCfQkdC+0LvQvtC90LrQsCcsJ9Cc0LDQu9GM0YLQuNC50YHQutCw0Y8nXTsKICB2YXIgaXNBbm5hQnJlZWQgPSBicmVlZCAmJiAhYW5uYUV4Y2x1ZGUuc29tZShmdW5jdGlvbihiKXsgcmV0dXJuIGJyZWVkLmluZGV4T2YoYikgIT09IC0xOyB9KTsKICB2YXIgYWxleGFuZHJhRXhjbHVkZSA9IFsn0KTQvtC60YHRgtC10YDRjNC10YAnLCfQptCy0LXRgNCz0YjQvdCw0YPRhtC10YAnXTsKICB2YXIgaXNBbGV4YW5kcmFCcmVlZCA9ICFhbGV4YW5kcmFFeGNsdWRlLnNvbWUoZnVuY3Rpb24oYil7IHJldHVybiBicmVlZC5pbmRleE9mKGIpICE9PSAtMTsgfSk7CiAgdmFyIGtzZW5pYUV4Y2x1ZGUgPSBbJ9Cf0YPQtNC10LvRjCcsJ9Cc0LDQu9GM0YLQuNC/0YMnLCfQmdC+0YDQuiddOwogIHZhciBpc0tzZW5pYUJyZWVkID0gIWtzZW5pYUV4Y2x1ZGUuc29tZShmdW5jdGlvbihiKXsgcmV0dXJuIGJyZWVkLmluZGV4T2YoYikgIT09IC0xOyB9KTsKICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcubWJ0bicpLmZvckVhY2goZnVuY3Rpb24oYnRuKXsKICAgIHZhciBtYXN0ZXIgPSBidG4uZ2V0QXR0cmlidXRlKCdkYXRhLW1hc3RlcicpOwogICAgdmFyIGlzVHJpbW1pbmcgPSBib29raW5nLnNlcnZpY2UgPT09ICfQotGA0LjQvNC80LjQvdCzJzsKICAgIGlmKGlzQ2F0Q29tcGxleCl7CiAgICAgIGJ0bi5zdHlsZS5kaXNwbGF5ID0gKG1hc3RlciA9PT0gJ9Ci0LDRgtGM0Y/QvdCwJyB8fCBtYXN0ZXIgPT09ICfQmtGB0LXQvdC40Y8nKSA/ICcnIDogJ25vbmUnOwogICAgICByZXR1cm47CiAgICB9CiAgICBpZihtYXN0ZXIgPT09ICfQkNC70LjRgdCwJyl7CiAgICAgIGJ0bi5zdHlsZS5kaXNwbGF5ID0gaXNDYXQgPyAnJyA6ICdub25lJzsKICAgIH0gZWxzZSBpZihtYXN0ZXIgPT09ICfQkNC90L3QsCcpewogICAgICBidG4uc3R5bGUuZGlzcGxheSA9IChpc0FubmFCcmVlZCAmJiAhaXNUcmltbWluZykgPyAnJyA6ICdub25lJzsKICAgIH0gZWxzZSBpZihtYXN0ZXIgPT09ICfQkNC70LXQutGB0LDQvdC00YDQsCcpewogICAgICBidG4uc3R5bGUuZGlzcGxheSA9IChpc0FsZXhhbmRyYUJyZWVkICYmICFpc1RyaW1taW5nICYmICFpc0NhdCkgPyAnJyA6ICdub25lJzsKICAgIH0gZWxzZSBpZihtYXN0ZXIgPT09ICfQmtGB0LXQvdC40Y8nKXsKICAgICAgYnRuLnN0eWxlLmRpc3BsYXkgPSBpc0tzZW5pYUJyZWVkID8gJycgOiAnbm9uZSc7CiAgICB9IGVsc2UgaWYoaXNUcmltbWluZyl7CiAgICAgIGJ0bi5zdHlsZS5kaXNwbGF5ID0gJ25vbmUnOwogICAgfSBlbHNlIHsKICAgICAgYnRuLnN0eWxlLmRpc3BsYXkgPSAnJzsKICAgIH0KICB9KTsKfQoKZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLm1idG4nKS5mb3JFYWNoKGZ1bmN0aW9uKGJ0bil7CiAgYnRuLm9uY2xpY2s9ZnVuY3Rpb24oKXsKICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5tYnRuJykuZm9yRWFjaChmdW5jdGlvbihiKXtiLmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpO30pOwogICAgYnRuLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpOwogICAgYm9va2luZy5tYXN0ZXI9YnRuLmdldEF0dHJpYnV0ZSgnZGF0YS1tYXN0ZXInKTsKICAgIHNldFRpbWVvdXQoZnVuY3Rpb24oKXtnb1N0ZXAoMyk7fSwzMDApOwogIH07Cn0pOwoKLy8gR3Jvb20gaGlzdG9yeQpkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuZ2J0bicpLmZvckVhY2goZnVuY3Rpb24oYnRuKXsKICBidG4ub25jbGljaz1mdW5jdGlvbigpewogICAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmdidG4nKS5mb3JFYWNoKGZ1bmN0aW9uKGIpe2IuY2xhc3NMaXN0LnJlbW92ZSgnYWN0aXZlJyk7fSk7CiAgICBidG4uY2xhc3NMaXN0LmFkZCgnYWN0aXZlJyk7CiAgICBib29raW5nLmdyb29tSGlzdG9yeT1idG4uZ2V0QXR0cmlidXRlKCdkYXRhLXZhbCcpOwogICAgc2V0VGltZW91dChmdW5jdGlvbigpe2dvU3RlcCg0KTtidWlsZENhbCgpO30sMzAwKTsKICB9Owp9KTsKCi8vIENhbGVuZGFyCmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdwcmV2TScpLm9uY2xpY2s9ZnVuY3Rpb24oKXtjTS0tO2lmKGNNPDApe2NNPTExO2NZLS07fWJ1aWxkQ2FsKCk7fTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ25leHRNJykub25jbGljaz1mdW5jdGlvbigpe2NNKys7aWYoY00+MTEpe2NNPTA7Y1krKzt9YnVpbGRDYWwoKTt9OwoKdmFyIGF2YWlsYWJsZURheXMgPSBbXTsKCmZ1bmN0aW9uIGxvYWRBdmFpbGFibGVEYXlzKCkgewogIHZhciBtYXN0ZXIgPSBib29raW5nLm1hc3RlcjsKICBpZiAoIW1hc3RlcikgcmV0dXJuOwogIGF2YWlsYWJsZURheXMgPSBbXTsKICBmZXRjaCh3aW5kb3cubG9jYXRpb24ub3JpZ2luICsgJy9hcGkvYXZhaWxhYmxlX2RheXM/bW9udGg9JyArIChjTSsxKSArICcmeWVhcj0nICsgY1kgKyAnJm1hc3Rlcj0nICsgZW5jb2RlVVJJQ29tcG9uZW50KG1hc3RlcikpCiAgICAudGhlbihmdW5jdGlvbihyKXsgcmV0dXJuIHIuanNvbigpOyB9KQogICAgLnRoZW4oZnVuY3Rpb24oZGF0YSl7CiAgICAgIGF2YWlsYWJsZURheXMgPSBkYXRhLmF2YWlsYWJsZSB8fCBbXTsKICAgICAgbWFya0F2YWlsYWJsZURheXMoKTsKICAgIH0pCiAgICAuY2F0Y2goZnVuY3Rpb24oKXsgYXZhaWxhYmxlRGF5cyA9IFtdOyB9KTsKfQoKZnVuY3Rpb24gbWFya0F2YWlsYWJsZURheXMoKSB7CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmNkJykuZm9yRWFjaChmdW5jdGlvbihjKXtpZighYy5jbGFzc0xpc3QuY29udGFpbnMoJ2RpcycpKWMuY2xhc3NMaXN0LnJlbW92ZSgnc2VsJyk7fSk7CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmNkOm5vdCguZGlzKTpub3QoLmNkbik6bm90KC5wYWQpJykuZm9yRWFjaChmdW5jdGlvbihlbCkgewogICAgdmFyIGRheSA9IGVsLnRleHRDb250ZW50LnRyaW0oKTsKICAgIGlmICghZGF5IHx8IGlzTmFOKHBhcnNlSW50KGRheSkpKSByZXR1cm47CiAgICB2YXIgZGF0ZVN0ciA9IFN0cmluZyhwYXJzZUludChkYXkpKS5wYWRTdGFydCgyLCcwJykgKyAnLicgKyBTdHJpbmcoY00rMSkucGFkU3RhcnQoMiwnMCcpICsgJy4nICsgY1k7CiAgICBpZiAoYXZhaWxhYmxlRGF5cy5pbmRleE9mKGRhdGVTdHIpICE9PSAtMSkgewogICAgICBlbC5jbGFzc0xpc3QuYWRkKCdhdmFpbCcpOwogICAgICBlbC5jbGFzc0xpc3QucmVtb3ZlKCdidXN5Jyk7CiAgICB9IGVsc2UgewogICAgICBlbC5jbGFzc0xpc3QuYWRkKCdidXN5Jyk7CiAgICAgIGVsLmNsYXNzTGlzdC5yZW1vdmUoJ2F2YWlsJyk7CiAgICB9CiAgfSk7Cn0KCmZ1bmN0aW9uIGJ1aWxkQ2FsKCl7CiAgbG9hZEF2YWlsYWJsZURheXMoKTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2FsTScpLnRleHRDb250ZW50PU1PTlRIU1tjTV0rJyAnK2NZOwogIGJvb2tpbmcuZGF0ZT0nJzsgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmNkJykuZm9yRWFjaChmdW5jdGlvbihjKXtjLmNsYXNzTGlzdC5yZW1vdmUoJ3NlbCcpO2MuY2xhc3NMaXN0LnJlbW92ZSgnYXZhaWwnKTtjLmNsYXNzTGlzdC5yZW1vdmUoJ2J1c3knKTt9KTsKICB2YXIgZz1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2FsRycpO2cuaW5uZXJIVE1MPScnOwogIFsn0J/QvScsJ9CS0YInLCfQodGAJywn0KfRgicsJ9Cf0YInLCfQodCxJywn0JLRgSddLmZvckVhY2goZnVuY3Rpb24oZCl7CiAgICB2YXIgZWw9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7ZWwuY2xhc3NOYW1lPSdjZG4nO2VsLnRleHRDb250ZW50PWQ7Zy5hcHBlbmRDaGlsZChlbCk7CiAgfSk7CiAgdmFyIGZpcnN0PW5ldyBEYXRlKGNZLGNNLDEpLmdldERheSgpOwogIHZhciBkYXlzPW5ldyBEYXRlKGNZLGNNKzEsMCkuZ2V0RGF0ZSgpOwogIHZhciBzdGFydD1maXJzdD09PTA/NjpmaXJzdC0xOwogIHZhciB0b2RheT1uZXcgRGF0ZSgpOwogIGZvcih2YXIgaT0wO2k8c3RhcnQ7aSsrKXt2YXIgZWw9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7ZWwuY2xhc3NOYW1lPSdjZCBwYWQnO2cuYXBwZW5kQ2hpbGQoZWwpO30KICBmb3IodmFyIGRheT0xO2RheTw9ZGF5cztkYXkrKyl7CiAgICB2YXIgZWw9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7ZWwuY2xhc3NOYW1lPSdjZCc7CiAgICB2YXIgZGF0ZT1uZXcgRGF0ZShjWSxjTSxkYXkpOwogICAgdmFyIGlzUGFzdD1kYXRlPG5ldyBEYXRlKHRvZGF5LmdldEZ1bGxZZWFyKCksdG9kYXkuZ2V0TW9udGgoKSx0b2RheS5nZXREYXRlKCkpOwogICAgdmFyIGlubmVyPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO2lubmVyLmNsYXNzTmFtZT0nY2QtaW5uZXInO2lubmVyLnRleHRDb250ZW50PWRheTtlbC5hcHBlbmRDaGlsZChpbm5lcik7CiAgICBpZihpc1Bhc3Qpe2VsLmNsYXNzTGlzdC5hZGQoJ2RpcycpO30KICAgIGVsc2V7CiAgICAgIGlmKGRhdGUudG9EYXRlU3RyaW5nKCk9PT10b2RheS50b0RhdGVTdHJpbmcoKSllbC5jbGFzc0xpc3QuYWRkKCd0b2QnKTsKICAgICAgKGZ1bmN0aW9uKGQsIGVsUmVmKXsKICAgICAgICBlbFJlZi5vbmNsaWNrPWZ1bmN0aW9uKCl7CiAgICAgICAgICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuY2QnKS5mb3JFYWNoKGZ1bmN0aW9uKGMpe2MuY2xhc3NMaXN0LnJlbW92ZSgnc2VsJyk7fSk7CiAgICAgICAgICBlbFJlZi5jbGFzc0xpc3QuYWRkKCdzZWwnKTsKICAgICAgICAgIGJvb2tpbmcuZGF0ZT1TdHJpbmcoZCkucGFkU3RhcnQoMiwnMCcpKycuJytTdHJpbmcoY00rMSkucGFkU3RhcnQoMiwnMCcpKycuJytjWTsKICAgICAgICAgIHNob3dUaW1lcygpOwogICAgICAgIH07CiAgICAgIH0pKGRheSwgZWwpOwogICAgfQogICAgZy5hcHBlbmRDaGlsZChlbCk7CiAgfQogIC8vIGZpbGwgdHJhaWxpbmcgY2VsbHMgdG8gY29tcGxldGUgbGFzdCBncmlkIHJvdwogIHZhciB0b3RhbCA9IHN0YXJ0ICsgZGF5czsKICB2YXIgdHJhaWwgPSAoNyAtICh0b3RhbCAlIDcpKSAlIDc7CiAgZm9yKHZhciB0PTA7dDx0cmFpbDt0Kyspe3ZhciBlcD1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdkaXYnKTtlcC5jbGFzc05hbWU9J2NkIHBhZCc7Zy5hcHBlbmRDaGlsZChlcCk7fQp9CgpmdW5jdGlvbiBzaG93VGltZXMoKXsKICB2YXIgdGc9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3RpbWVHJyk7CiAgdGcuaW5uZXJIVE1MPSc8ZGl2IGNsYXNzPSJsb2FkaW5nLXNsb3RzIj7ij7Mg0JfQsNCz0YDRg9C20LDQtdC8INGA0LDRgdC/0LjRgdCw0L3QuNC1Li4uPC9kaXY+JzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgndGltZVNlYycpLnN0eWxlLmRpc3BsYXk9J2Jsb2NrJzsKCiAgdmFyIHVybCA9IHdpbmRvdy5sb2NhdGlvbi5vcmlnaW4gKyAiL2FwaS9zbG90cyIgKyAnP2FjdGlvbj1zbG90cyZkYXRlPScgKyBlbmNvZGVVUklDb21wb25lbnQoYm9va2luZy5kYXRlKSArICcmbWFzdGVyPScgKyBlbmNvZGVVUklDb21wb25lbnQoYm9va2luZy5tYXN0ZXIpOwoKICBmZXRjaCh1cmwpCiAgICAudGhlbihmdW5jdGlvbihyKXtyZXR1cm4gci5qc29uKCk7fSkKICAgIC50aGVuKGZ1bmN0aW9uKGRhdGEpewogICAgICB2YXIgc2xvdHMgPSAoZGF0YS5zbG90cyAmJiBkYXRhLnNsb3RzLmxlbmd0aCA+IDApID8gZGF0YS5zbG90cyA6IFtdOwogICAgICByZW5kZXJUaW1lU2xvdHMoc2xvdHMpOwogICAgfSkKICAgIC5jYXRjaChmdW5jdGlvbigpewogICAgICByZW5kZXJUaW1lU2xvdHMoW10pOwogICAgfSk7Cn0KCmZ1bmN0aW9uIHJlbmRlclRpbWVTbG90cyhzbG90cyl7CiAgdmFyIHRnPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCd0aW1lRycpO3RnLmlubmVySFRNTD0nJzsKICBpZihzbG90cy5sZW5ndGg9PT0wKXsKICAgIHRnLmlubmVySFRNTD0nPGRpdiBjbGFzcz0ibG9hZGluZy1zbG90cyI+0J3QtdGCINC00L7RgdGC0YPQv9C90YvRhSDRgdC70L7RgtC+0LIg0L3QsCDRjdGC0YMg0LTQsNGC0YM8L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXIiIG9uY2xpY2s9InNob3dTY3JlZW4oXCdob21lU2NyZWVuXCcpIiBzdHlsZT0ibWFyZ2luLXRvcDo4cHg7Ij48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItaWNvbiI+8J+QvjwvZGl2PjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci10ZXh0Ij48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItdGl0bGUiPtCd0LUg0L3QsNGI0LvQuCDQv9C+0LTRhdC+0LTRj9GJ0LXQtSDQstGA0LXQvNGPPzwvZGl2PjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci1zdWIiPtCh0LLRj9C20LjRgtC10YHRjCDRgSDQvdCw0LzQuCDQu9GO0LHRi9C8INGD0LTQvtCx0L3Ri9C8INGB0L/QvtGB0L7QsdC+0Lwg4oCUINC80Ysg0L/QvtC00LHQtdGA0ZHQvCDRg9C00L7QsdC90L7QtSDQstGA0LXQvNGPPC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLWFycm93Ij7ihpI8L2Rpdj48L2Rpdj4nOwogICAgcmV0dXJuOwogIH0KICBzbG90cy5mb3JFYWNoKGZ1bmN0aW9uKHQpewogICAgdmFyIGJ0bj1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdidXR0b24nKTtidG4uY2xhc3NOYW1lPSd0YnRuJztidG4udGV4dENvbnRlbnQ9dDsKICAgIGJ0bi5vbmNsaWNrPWZ1bmN0aW9uKCl7CiAgICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy50YnRuJykuZm9yRWFjaChmdW5jdGlvbihiKXtiLmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpO30pOwogICAgICBidG4uY2xhc3NMaXN0LmFkZCgnYWN0aXZlJyk7Ym9va2luZy50aW1lPXQ7CiAgICAgIHNldFRpbWVvdXQoZnVuY3Rpb24oKXtnb1N0ZXAoNSk7YnVpbGRTdW0oKTt9LDMwMCk7CiAgICB9OwogICAgdGcuYXBwZW5kQ2hpbGQoYnRuKTsKICB9KTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgndGltZVNlYycpLnNjcm9sbEludG9WaWV3KHtiZWhhdmlvcjonc21vb3RoJyxibG9jazonbmVhcmVzdCd9KTsKfQoKZnVuY3Rpb24gYnVpbGRTdW0oKXsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3VtQmxvY2snKS5pbm5lckhUTUw9CiAgICAnPGRpdiBjbGFzcz0ic3IiPjxzcGFuIGNsYXNzPSJzbCI+JytUW0xBTkddLnN1bV9icmVlZCsnPC9zcGFuPjxzcGFuIGNsYXNzPSJzdiI+JysoYm9va2luZy5icmVlZERpc3BsYXl8fGJvb2tpbmcuYnJlZWQpKyc8L3NwYW4+PC9kaXY+JysKICAgICc8ZGl2IGNsYXNzPSJzciI+PHNwYW4gY2xhc3M9InNsIj4nK1RbTEFOR10uc3VtX3NlcnZpY2UrJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrKChMQU5HIT09J3J1JyYmU1ZDX1RSQU5TTEFUSU9OU1tib29raW5nLnNlcnZpY2VdKT9TVkNfVFJBTlNMQVRJT05TW2Jvb2tpbmcuc2VydmljZV1bTEFOR106Ym9va2luZy5zZXJ2aWNlKSsnPC9zcGFuPjwvZGl2PicrCiAgICAnPGRpdiBjbGFzcz0ic3IiPjxzcGFuIGNsYXNzPSJzbCI+JytUW0xBTkddLnN1bV9tYXN0ZXIrJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrYm9va2luZy5tYXN0ZXIrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fZ3Jvb20rJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrYm9va2luZy5ncm9vbUhpc3RvcnkrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fZGF0ZSsnPC9zcGFuPjxzcGFuIGNsYXNzPSJzdiI+Jytib29raW5nLmRhdGUrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fdGltZSsnPC9zcGFuPjxzcGFuIGNsYXNzPSJzdiI+Jytib29raW5nLnRpbWUrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fcHJpY2UrJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3AiPicrYm9va2luZy5wcmljZSsnIOKCrDwvc3Bhbj48L2Rpdj4nOwp9Cgpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY29uZmlybUJ0bicpLm9uY2xpY2sgPSBmdW5jdGlvbigpewogIHZhciBuYW1lPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjTmFtZScpLnZhbHVlOwogIHZhciBwaG9uZT1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY1Bob25lJykudmFsdWU7CiAgaWYoIW5hbWV8fCFwaG9uZSl7YWxlcnQoVFtMQU5HXS5hbGVydF9maWxsKTtyZXR1cm47fQogIGlmKCEvXlwrXGR7MTAsfSQvLnRlc3QocGhvbmUudHJpbSgpKSl7YWxlcnQoVFtMQU5HXS5hbGVydF9waG9uZSk7cmV0dXJuO30KICBib29raW5nLm5hbWU9bmFtZTsgYm9va2luZy5waG9uZT1waG9uZTsgYm9va2luZy5lbWFpbD1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY0VtYWlsJykudmFsdWU7IGJvb2tpbmcucGV0PWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjUGV0JykudmFsdWU7IGJvb2tpbmcubGFuZz1MQU5HOwogIGJvb2tpbmcuZHVyYXRpb24gPSBib29raW5nLmJyZWVkID09PSAn0KnQtdC90LrQuCcgPyA2MCA6IChib29raW5nLmJyZWVkICYmIGJvb2tpbmcuYnJlZWQuaW5kZXhPZign0JrQvtGI0LrQsCcpID09PSAwID8gMTIwIDogMTgwKTsKICB2YXIgYnRuPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjb25maXJtQnRuJyk7CiAgYnRuLnRleHRDb250ZW50PVRbTEFOR10uc2VuZGluZzsgYnRuLmRpc2FibGVkPXRydWU7CiAgZmV0Y2goUkFJTFdBWSwgewogICAgbWV0aG9kOidQT1NUJywKICAgIGhlYWRlcnM6eydDb250ZW50LVR5cGUnOidhcHBsaWNhdGlvbi9qc29uJ30sCiAgICBib2R5OkpTT04uc3RyaW5naWZ5KGJvb2tpbmcpCiAgfSkudGhlbihmdW5jdGlvbigpe3Nob3dTdWNjZXNzKCk7fSkuY2F0Y2goZnVuY3Rpb24oKXtzaG93U3VjY2VzcygpO30pOwp9OwoKZnVuY3Rpb24gc2hvd1N1Y2Nlc3MoKXsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnYms1JykuY2xhc3NOYW1lPSdzdGVwJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3VjQmxvY2snKS5jbGFzc0xpc3QuYWRkKCdzaG93Jyk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3Byb2dyZXNzJykuc3R5bGUuZGlzcGxheT0nbm9uZSc7Cn0KCmZ1bmN0aW9uIHJlc2V0QWxsKCl7CiAgYm9va2luZz17YnJlZWQ6JycsYnJlZWREaXNwbGF5OicnLHNlcnZpY2U6JycscHJpY2U6MCxtYXN0ZXI6JycsZ3Jvb21IaXN0b3J5OicnLGRhdGU6JycsdGltZTonJyxsYW5nOidydSd9OwogIHNlbEJyZWVkPW51bGw7IGlucC52YWx1ZT0nJzsgY2xyLmNsYXNzTGlzdC5yZW1vdmUoJ3Nob3cnKTsKICBiYWRnZS5jbGFzc0xpc3QucmVtb3ZlKCdzaG93Jyk7IGJhZGdlLmlubmVySFRNTD0nJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjU2VjJykuc3R5bGUuZGlzcGxheT0nbm9uZSc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3RpbWVTZWMnKS5zdHlsZS5kaXNwbGF5PSdub25lJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3VjQmxvY2snKS5jbGFzc0xpc3QucmVtb3ZlKCdzaG93Jyk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3Byb2dyZXNzJykuc3R5bGUuZGlzcGxheT0nZmxleCc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NOYW1lJykudmFsdWU9Jyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NQaG9uZScpLnZhbHVlPScnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjRW1haWwnKS52YWx1ZT0nJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY1BldCcpLnZhbHVlPScnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjb25maXJtQnRuJykudGV4dENvbnRlbnQ9VFtMQU5HXS5jb25maXJtX2J0bjsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY29uZmlybUJ0bicpLmRpc2FibGVkPWZhbHNlOwogIGdvU3RlcCgxKTsKfQoKdmFyIExBTkcgPSBsb2NhbFN0b3JhZ2UuZ2V0SXRlbSgncmpsYW5nJykgfHwgJ3J1JzsKdmFyIFQgPSB7CiAgcnU6ewogICAgbG9nb190YWc6J9Cf0YDQtdC80LjQsNC70YzQvdGL0Lkg0LPRgNGD0LzQuNC90LMtPGJyPtGB0LDQu9C+0L0g0LIg0KLQsNC70LvQuNC90LUnLAogICAgY2hvb3NlX2hvdzonQ2hvb3NlIGhvdyB0byBjb25uZWN0JywKICAgIGJvb2tfb25saW5lOifQntC90LvQsNC50L0g0LHRgNC+0L3QuNGA0L7QstCw0L3QuNC1JywKICAgIGJvb2tfZmxvdzon0J/QvtGA0L7QtNCwIOKGkiDQo9GB0LvRg9Cz0LAg4oaSINCc0LDRgdGC0LXRgCDihpIg0JLRgNC10LzRjycsCiAgICBvcl9jb250YWN0OifQuNC70Lgg0YHQstGP0LbQuNGC0LXRgdGMINGBINC90LDQvNC4JywKICAgIGNhbGxfdXM6J0NhbGwgVXMnLAogICAgYmFjazon4oaQINCd0LDQt9Cw0LQnLAogICAgbG9nb19zdWI6J0dyb29taW5nIMK3INCi0LDQu9C70LjQvScsCiAgICBwc19zZXJ2aWNlOifQo9GB0LvRg9Cz0LAnLHBzX21hc3Rlcjon0JzQsNGB0YLQtdGAJyxwc19wZXQ6J9Cf0LjRgtC+0LzQtdGGJyxwc19kYXRlOifQlNCw0YLQsCcscHNfZGV0YWlsczon0JTQsNC90L3Ri9C1JywKICAgIHN0ZXAxX2xibDonMDEgwrcg0J/QvtGA0L7QtNCwJywKICAgIGJyZWVkX3BoOifQndCw0YfQvdC40YLQtSDQstCy0L7QtNC40YLRjCDQv9C+0YDQvtC00YMuLi4nLAogICAgc3RlcDJfbGJsOicwMiDCtyDQo9GB0LvRg9Cz0LAnLAogICAgc3RlcDJfbWFzdGVyOifQktGL0LHQtdGA0LjRgtC1INC80LDRgdGC0LXRgNCwJywKICAgIHN0ZXAzX2xibDon0JrQsNC6INC00LDQstC90L4g0LLRiyDQv9C+0YHQtdGJ0LDQu9C4INCz0YDRg9C80LjQvdCzPycsCiAgICBnMTon0J/QtdGA0LLRi9C5INGA0LDQtycsZzI6J9Ce0YIgMSDQtNC+IDMg0LzQtdGB0Y/RhtC10LInLGczOifQntGCIDMg0LTQviA2INC80LXRgdGP0YbQtdCyJyxnNDon0JHQvtC70LXQtSA2INC80LXRgdGP0YbQtdCyJywKICAgIHN0ZXA0X2xibDon0JLRi9Cx0LXRgNC40YLQtSDQtNCw0YLRgycsCiAgICBjYWxfYXZhaWw6J9CV0YHRgtGMINGB0LLQvtCx0L7QtNC90L7QtSDQstGA0LXQvNGPJyxjYWxfbm9uZTon0KHQstC+0LHQvtC00L3QvtCz0L4g0LLRgNC10LzQtdC90Lgg0L3QtdGCJywKICAgIHN0ZXA0X3RpbWU6J9CS0YvQsdC10YDQuNGC0LUg0LLRgNC10LzRjycsCiAgICBzdGVwNV9sYmw6J9CS0LDRiNC4INC00LDQvdC90YvQtScsCiAgICBsYmxfbmFtZTon0JjQvNGPJyxwaF9uYW1lOifQktCw0YjQtSDQuNC80Y8nLAogICAgbGJsX3Bob25lOifQotC10LvQtdGE0L7QvScsbGJsX2VtYWlsOidFbWFpbCcsCiAgICBsYmxfcGV0OifQmtC70LjRh9C60LAg0L/QuNGC0L7QvNGG0LAnLHBoX29wdGlvbmFsOifQndC10L7QsdGP0LfQsNGC0LXQu9GM0L3QvicsCiAgICBjb25maXJtX2J0bjon0J/QvtC00YLQstC10YDQtNC40YLRjCDQt9Cw0L/QuNGB0YwnLAogICAgc3VjY2Vzc190aXRsZTon0JfQsNC/0LjRgdGMINC/0YDQuNC90Y/RgtCwIScsCiAgICBzdWNjZXNzX3N1Yjon0JzRiyDRgdCy0Y/QttC10LzRgdGPINGBINCy0LDQvNC4INC00LvRjyDQv9C+0LTRgtCy0LXRgNC20LTQtdC90LjRjy48YnI+0KHQv9Cw0YHQuNCx0L4sINGH0YLQviDQstGL0LHRgNCw0LvQuCBSJmFtcDtKIEdyb29taW5nIScsCiAgICB0b19ob21lOifihpAg0J3QsCDQs9C70LDQstC90YPRjicsCiAgICBhbGVydF9maWxsOifQktCy0LXQtNC40YLQtSDQuNC80Y8g0Lgg0YLQtdC70LXRhNC+0L0nLGFsZXJ0X3Bob25lOifQktCy0LXQtNC40YLQtSDQvdC+0LzQtdGAINCyINGE0L7RgNC80LDRgtC1ICszNzIxMjM0NTY3OCcsCiAgICBzZW5kaW5nOifQntGC0L/RgNCw0LLQu9GP0LXQvC4uLicsCiAgICBzdW1fYnJlZWQ6J9Cf0L7RgNC+0LTQsCcsc3VtX3NlcnZpY2U6J9Cj0YHQu9GD0LPQsCcsc3VtX21hc3Rlcjon0JzQsNGB0YLQtdGAJyxzdW1fZ3Jvb206J9Cf0L7RgdC70LXQtNC90LjQuSDQs9GA0YPQvCcsc3VtX2RhdGU6J9CU0LDRgtCwJyxzdW1fdGltZTon0JLRgNC10LzRjycsc3VtX3ByaWNlOifQodGC0L7QuNC80L7RgdGC0YwnLAogICAgbW9udGhzOlsn0K/QvdCy0LDRgNGMJywn0KTQtdCy0YDQsNC70YwnLCfQnNCw0YDRgicsJ9CQ0L/RgNC10LvRjCcsJ9Cc0LDQuScsJ9CY0Y7QvdGMJywn0JjRjtC70YwnLCfQkNCy0LPRg9GB0YInLCfQodC10L3RgtGP0LHRgNGMJywn0J7QutGC0Y/QsdGA0YwnLCfQndC+0Y/QsdGA0YwnLCfQlNC10LrQsNCx0YDRjCddCiAgfSwKICBlbjp7CiAgICBsb2dvX3RhZzonUHJlbWl1bSBncm9vbWluZzxicj5zYWxvbiBpbiBUYWxsaW5uJywKICAgIGNob29zZV9ob3c6J0Nob29zZSBob3cgdG8gY29ubmVjdCcsCiAgICBib29rX29ubGluZTonQm9vayBPbmxpbmUnLAogICAgYm9va19mbG93OidCcmVlZCDihpIgU2VydmljZSDihpIgTWFzdGVyIOKGkiBUaW1lJywKICAgIG9yX2NvbnRhY3Q6J29yIGNvbnRhY3QgdXMnLAogICAgY2FsbF91czonQ2FsbCBVcycsCiAgICBiYWNrOifihpAgQmFjaycsCiAgICBsb2dvX3N1YjonR3Jvb21pbmcgwrcgVGFsbGlubicsCiAgICBwc19zZXJ2aWNlOidTZXJ2aWNlJyxwc19tYXN0ZXI6J01hc3RlcicscHNfcGV0OidQZXQnLHBzX2RhdGU6J0RhdGUnLHBzX2RldGFpbHM6J0RldGFpbHMnLAogICAgc3RlcDFfbGJsOicwMSDCtyBEb2cgYnJlZWQnLAogICAgYnJlZWRfcGg6J1N0YXJ0IHR5cGluZyBicmVlZC4uLicsCiAgICBzdGVwMl9sYmw6JzAyIMK3IFNlcnZpY2UnLAogICAgc3RlcDJfbWFzdGVyOidDaG9vc2UgbWFzdGVyJywKICAgIHN0ZXAzX2xibDonSG93IGxvbmcgYWdvIHdhcyB5b3VyIGxhc3QgZ3Jvb21pbmc/JywKICAgIGcxOidGaXJzdCB0aW1lJyxnMjonMeKAkzMgbW9udGhzIGFnbycsZzM6JzPigJM2IG1vbnRocyBhZ28nLGc0OidPdmVyIDYgbW9udGhzJywKICAgIHN0ZXA0X2xibDonQ2hvb3NlIGRhdGUnLAogICAgY2FsX2F2YWlsOidBdmFpbGFibGUnLGNhbF9ub25lOidOb3QgYXZhaWxhYmxlJywKICAgIHN0ZXA0X3RpbWU6J0Nob29zZSB0aW1lJywKICAgIHN0ZXA1X2xibDonWW91ciBkZXRhaWxzJywKICAgIGxibF9uYW1lOidOYW1lJyxwaF9uYW1lOidZb3VyIG5hbWUnLAogICAgbGJsX3Bob25lOidQaG9uZScsbGJsX2VtYWlsOidFbWFpbCcsCiAgICBsYmxfcGV0OiJQZXQncyBuYW1lIixwaF9vcHRpb25hbDonT3B0aW9uYWwnLAogICAgY29uZmlybV9idG46J0NvbmZpcm0gYm9va2luZycsCiAgICBzdWNjZXNzX3RpdGxlOidCb29raW5nIGNvbmZpcm1lZCEnLAogICAgc3VjY2Vzc19zdWI6J1dlIHdpbGwgY29udGFjdCB5b3UgdG8gY29uZmlybS48YnI+VGhhbmsgeW91IGZvciBjaG9vc2luZyBSJmFtcDtKIEdyb29taW5nIScsCiAgICB0b19ob21lOifihpAgSG9tZScsCiAgICBhbGVydF9maWxsOidQbGVhc2UgZW50ZXIgbmFtZSBhbmQgcGhvbmUnLGFsZXJ0X3Bob25lOidFbnRlciBwaG9uZSBudW1iZXIgaW4gZm9ybWF0ICszNzIxMjM0NTY3OCcsCiAgICBzZW5kaW5nOidTZW5kaW5nLi4uJywKICAgIHN1bV9icmVlZDonQnJlZWQnLHN1bV9zZXJ2aWNlOidTZXJ2aWNlJyxzdW1fbWFzdGVyOidNYXN0ZXInLHN1bV9ncm9vbTonTGFzdCBncm9vbWluZycsc3VtX2RhdGU6J0RhdGUnLHN1bV90aW1lOidUaW1lJyxzdW1fcHJpY2U6J1ByaWNlJywKICAgIG1vbnRoczpbJ0phbnVhcnknLCdGZWJydWFyeScsJ01hcmNoJywnQXByaWwnLCdNYXknLCdKdW5lJywnSnVseScsJ0F1Z3VzdCcsJ1NlcHRlbWJlcicsJ09jdG9iZXInLCdOb3ZlbWJlcicsJ0RlY2VtYmVyJ10KICB9LAogIGV0OnsKICAgIGxvZ29fdGFnOidFc21ha2xhc3NpbGluZSBob29sZHVzdGVlbnVzPGJyPlRhbGxpbm5hcycsCiAgICBjaG9vc2VfaG93OidWYWxpIMO8aGVuZHVzdmlpcycsCiAgICBib29rX29ubGluZTonQnJvbmVlcmkgdmVlYmlzJywKICAgIGJvb2tfZmxvdzonVMO1dWcg4oaSIFRlZW51cyDihpIgTWVpc3RlciDihpIgQWVnJywKICAgIG9yX2NvbnRhY3Q6J3bDtWkgdsO1dGEgw7xoZW5kdXN0JywKICAgIGNhbGxfdXM6J0hlbGlzdGEgbWVpbGUnLAogICAgYmFjazon4oaQIFRhZ2FzaScsCiAgICBsb2dvX3N1YjonR3Jvb21pbmcgwrcgVGFsbGlubicsCiAgICBwc19zZXJ2aWNlOidUZWVudXMnLHBzX21hc3RlcjonTWVpc3RlcicscHNfcGV0OidMZW1taWtsb29tJyxwc19kYXRlOidLdXVww6RldicscHNfZGV0YWlsczonQW5kbWVkJywKICAgIHN0ZXAxX2xibDonMDEgwrcgS29lcmEgdMO1dWcnLAogICAgYnJlZWRfcGg6J0FsdXN0YWdlIHTDtXUgc2lzZXN0YW1pc3QuLi4nLAogICAgc3RlcDJfbGJsOicwMiDCtyBUZWVudXMnLAogICAgc3RlcDJfbWFzdGVyOidWYWxpIG1laXN0ZXInLAogICAgc3RlcDNfbGJsOidNaWxsYWwga8OkaXNpdGUgdmlpbWF0aSBncm9vbWluZ3VzPycsCiAgICBnMTonRXNpbWVzdCBrb3JkYScsZzI6JzHigJMzIGt1dWQgdGFnYXNpJyxnMzonM+KAkzYga3V1ZCB0YWdhc2knLGc0OifDnGxlIDYga3V1JywKICAgIHN0ZXA0X2xibDonVmFsaSBrdXVww6RldicsCiAgICBjYWxfYXZhaWw6J1ZhYnUgYWVndSBvbicsY2FsX25vbmU6J1ZhYnUgYWVndSBwb2xlJywKICAgIHN0ZXA0X3RpbWU6J1ZhbGkga2VsbGFhZWcnLAogICAgc3RlcDVfbGJsOidUZWllIGFuZG1lZCcsCiAgICBsYmxfbmFtZTonTmltaScscGhfbmFtZTonVGVpZSBuaW1pJywKICAgIGxibF9waG9uZTonVGVsZWZvbicsbGJsX2VtYWlsOidFbWFpbCcsCiAgICBsYmxfcGV0OidMZW1taWtsb29tYSBuaW1pJyxwaF9vcHRpb25hbDonVmFsaWt1bGluZScsCiAgICBjb25maXJtX2J0bjonS2lubml0YSBicm9uZWVyaW5nJywKICAgIHN1Y2Nlc3NfdGl0bGU6J0Jyb25lZXJpbmcga2lubml0YXR1ZCEnLAogICAgc3VjY2Vzc19zdWI6J1bDtXRhbWUgdGVpZWdhIMO8aGVuZHVzdCBraW5uaXRhbWlzZWtzLjxicj5Uw6RuYW1lLCBldCB2YWxpc2l0ZSBSJmFtcDtKIEdyb29taW5nIScsCiAgICB0b19ob21lOifihpAgQXZhbGVoZWxlJywKICAgIGFsZXJ0X2ZpbGw6J1BhbHVuIHNpc2VzdGFnZSBuaW1pIGphIHRlbGVmb24nLGFsZXJ0X3Bob25lOidTaXNlc3RhZ2UgdGVsZWZvbmludW1iZXIgdm9ybWluZ3VzICszNzIxMjM0NTY3OCcsCiAgICBzZW5kaW5nOidTYWFkYW4uLi4nLAogICAgc3VtX2JyZWVkOidUw7V1Zycsc3VtX3NlcnZpY2U6J1RlZW51cycsc3VtX21hc3RlcjonTWVpc3Rlcicsc3VtX2dyb29tOidWaWltYW5lIGdyb29taW5nJyxzdW1fZGF0ZTonS3V1cMOkZXYnLHN1bV90aW1lOidLZWxsYWFlZycsc3VtX3ByaWNlOidIaW5kJywKICAgIG1vbnRoczpbJ0phYW51YXInLCdWZWVicnVhcicsJ03DpHJ0cycsJ0FwcmlsbCcsJ01haScsJ0p1dW5pJywnSnV1bGknLCdBdWd1c3QnLCdTZXB0ZW1iZXInLCdPa3Rvb2JlcicsJ05vdmVtYmVyJywnRGV0c2VtYmVyJ10KICB9Cn07CgpmdW5jdGlvbiBzZXRMYW5nKGwpewogIExBTkc9bDsKICBsb2NhbFN0b3JhZ2Uuc2V0SXRlbSgncmpsYW5nJyxsKTsKICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcubGFuZy1idG4nKS5mb3JFYWNoKGZ1bmN0aW9uKGIpewogICAgYi5jbGFzc0xpc3QudG9nZ2xlKCdhY3RpdmUnLCBiLnRleHRDb250ZW50LnRvTG93ZXJDYXNlKCk9PT1sKTsKICB9KTsKICB2YXIgdHI9VFtsXTsKICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCdbZGF0YS1pMThuXScpLmZvckVhY2goZnVuY3Rpb24oZWwpewogICAgdmFyIGs9ZWwuZ2V0QXR0cmlidXRlKCdkYXRhLWkxOG4nKTsKICAgIGlmKHRyW2tdIT09dW5kZWZpbmVkKSBlbC5pbm5lckhUTUw9dHJba107CiAgfSk7CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnW2RhdGEtaTE4bi1waF0nKS5mb3JFYWNoKGZ1bmN0aW9uKGVsKXsKICAgIHZhciBrPWVsLmdldEF0dHJpYnV0ZSgnZGF0YS1pMThuLXBoJyk7CiAgICBpZih0cltrXSE9PXVuZGVmaW5lZCkgZWwucGxhY2Vob2xkZXI9dHJba107CiAgfSk7CiAgTU9OVEhTPXRyLm1vbnRoczsKICBidWlsZENhbCgpOwogIC8vIFJlLXJlbmRlciBiYWRnZSBhbmQgc2VydmljZXMgaWYgYnJlZWQgYWxyZWFkeSBzZWxlY3RlZAogIGlmKHNlbEJyZWVkKXsKICAgIHZhciBiZj1sPT09J2VuJz8nYnJlZWRfZW4nOmw9PT0nZXQnPydicmVlZF9ldCc6J2JyZWVkJzsKICAgIHZhciBkYj1zZWxCcmVlZFtiZl18fHNlbEJyZWVkLmJyZWVkOwogICAgYm9va2luZy5icmVlZERpc3BsYXk9ZGI7CiAgICB2YXIgYm5FbD1kb2N1bWVudC5xdWVyeVNlbGVjdG9yKCcjc0JhZGdlIC5ibmFtZScpOwogICAgaWYoYm5FbCkgYm5FbC50ZXh0Q29udGVudD1kYjsKICAgIHZhciBiY0VsPWRvY3VtZW50LnF1ZXJ5U2VsZWN0b3IoJyNzQmFkZ2UgLmJjaGcnKTsKICAgIGlmKGJjRWwpIGJjRWwudGV4dENvbnRlbnQ9bD09PSdlbic/J0NoYW5nZSc6bD09PSdldCc/J011dWRhJzon0JjQt9C80LXQvdC40YLRjCc7CiAgICBpZihkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjU2VjJykuc3R5bGUuZGlzcGxheSE9PSdub25lJykgcmVuZGVyU3ZjcyhzZWxCcmVlZCk7CiAgICB2YXIgc249ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y05vdGUnKTsKICAgIGlmKHNuKXsKICAgICAgdmFyIG50PWw9PT0nZW4nPydQbGVhc2Ugbm90ZSc6bD09PSdldCc/J1BhbmdlIHTDpGhlbGUnOifQktCw0LbQvdC+INC30L3QsNGC0YwnOwogICAgICB2YXIgbmI9bD09PSdlbic/J0ZpbmFsIHByaWNlIGRlcGVuZHMgb24gY29hdCBjb25kaXRpb24gYW5kIHBldCBiZWhhdmlvdXIuPGJyPkRlbWF0dGluZyBmcm9tIDUg4oKsLjxicj5BZ2dyZXNzaXZlIGJlaGF2aW91ciBzdXJjaGFyZ2UgbWF5IGFwcGx5OiArNTAlLic6bD09PSdldCc/J0zDtXBsaWsgaGluZCBzw7VsdHViIGthcnZhc3Rpa3Ugc2Vpc3VuZGlzdCBqYSBsZW1taWtsb29tYSBrw6RpdHVtaXNlc3QuPGJyPktvbHRzdW5pdGUgbGFodGloYXJ1dGFtaW5lIGFsYXRlcyA1IOKCrC48YnI+QWdyZXNzaWl2c2Uga8OkaXR1bWlzZSBrb3JyYWwgdsO1aWIgbGlzYW5kdWRhIDUwJSBqdXVyZGVoaW5kbHVzLic6J9Ce0LrQvtC90YfQsNGC0LXQu9GM0L3QsNGPINGB0YLQvtC40LzQvtGB0YLRjCDQt9Cw0LLQuNGB0LjRgiDQvtGCINGB0L7RgdGC0L7Rj9C90LjRjyDRiNC10YDRgdGC0Lgg0Lgg0L/QvtCy0LXQtNC10L3QuNGPINC/0LjRgtC+0LzRhtCwLjxicj7QoNCw0LfQsdC+0YAg0LrQvtC70YLRg9C90L7QsiDigJQg0L7RgiA1IOKCrC48YnI+0J/RgNC4INCw0LPRgNC10YHRgdC40LLQvdC+0Lwg0L/QvtCy0LXQtNC10L3QuNC4INC80L7QttC10YIg0L/RgNC40LzQtdC90Y/RgtGM0YHRjyDQtNC+0L/Qu9Cw0YLQsCA1MCUuJzsKICAgICAgc24uaW5uZXJIVE1MPSc8ZGl2IHN0eWxlPSJmb250LXNpemU6MC44MzhyZW07bGV0dGVyLXNwYWNpbmc6LjE1ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbTo4cHg7Zm9udC13ZWlnaHQ6NjAwO2ZvbnQtZmFtaWx5OlwnTW9udHNlcnJhdFwnLHNhbnMtc2VyaWYiPicrbnQrJzwvZGl2PjxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxLjAyNXJlbTtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjEuODtmb250LWZhbWlseTpcJ01vbnRzZXJyYXRcJyxzYW5zLXNlcmlmIj4nK25iKyc8L2Rpdj4nOwogICAgfQogIH0KfQoKLy8gQXBwbHkgc2F2ZWQgbGFuZ3VhZ2Ugb24gbG9hZAooZnVuY3Rpb24oKXsgc2V0TGFuZyhMQU5HKTsgfSkoKTsKCi8vIENhbGxiYWNrIGZvcm0KZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NhbGxiYWNrQnRuJykub25jbGljayA9IGZ1bmN0aW9uKCl7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia01vZGFsJykuc3R5bGUuZGlzcGxheSA9ICdmbGV4JzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrTmFtZScpLnZhbHVlID0gJyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1Bob25lJykudmFsdWUgPSAnJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrU3VjY2VzcycpLnN0eWxlLmRpc3BsYXkgPSAnbm9uZSc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1N1Ym1pdCcpLnN0eWxlLmRpc3BsYXkgPSAnYmxvY2snOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtDbG9zZScpLnRleHRDb250ZW50ID0gJ9Ce0YLQvNC10L3QsCc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1N1Ym1pdCcpLnRleHRDb250ZW50ID0gJ9Ce0YLQv9GA0LDQstC40YLRjCc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1N1Ym1pdCcpLmRpc2FibGVkID0gZmFsc2U7Cn07CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtDbG9zZScpLm9uY2xpY2sgPSBmdW5jdGlvbigpewogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtNb2RhbCcpLnN0eWxlLmRpc3BsYXkgPSAnbm9uZSc7Cn07CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtTdWJtaXQnKS5vbmNsaWNrID0gZnVuY3Rpb24oKXsKICB2YXIgbmFtZSA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtOYW1lJykudmFsdWUudHJpbSgpOwogIHZhciBwaG9uZSA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtQaG9uZScpLnZhbHVlLnRyaW0oKS5yZXBsYWNlKC9cRC9nLCcnKTsKICBpZighbmFtZSB8fCAhcGhvbmUpe2FsZXJ0KCfQktCy0LXQtNC40YLQtSDQuNC80Y8g0Lgg0YLQtdC70LXRhNC+0L0nKTtyZXR1cm47fQogIHZhciBidG4gPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrU3VibWl0Jyk7CiAgYnRuLnRleHRDb250ZW50ID0gJ9Ce0YLQv9GA0LDQstC70Y/QtdC8Li4uJzsgYnRuLmRpc2FibGVkID0gdHJ1ZTsKICBmZXRjaCgnL2FwaS9jYWxsYmFjaycsewogICAgbWV0aG9kOidQT1NUJywKICAgIGhlYWRlcnM6eydDb250ZW50LVR5cGUnOidhcHBsaWNhdGlvbi9qc29uJ30sCiAgICBib2R5OkpTT04uc3RyaW5naWZ5KHtuYW1lOm5hbWUsIHBob25lOicrMzcyJytwaG9uZX0pCiAgfSkudGhlbihmdW5jdGlvbigpewogICAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1N1Y2Nlc3MnKS5zdHlsZS5kaXNwbGF5ID0gJ2Jsb2NrJzsKICAgIGJ0bi5zdHlsZS5kaXNwbGF5ID0gJ25vbmUnOwogICAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia0Nsb3NlJykudGV4dENvbnRlbnQgPSAn4oaQINCX0LDQutGA0YvRgtGMJzsKICAgIHNldFRpbWVvdXQoZnVuY3Rpb24oKXtkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrTW9kYWwnKS5zdHlsZS5kaXNwbGF5PSdub25lJzt9LDMwMDApOwogIH0pLmNhdGNoKGZ1bmN0aW9uKCl7CiAgICBidG4udGV4dENvbnRlbnQgPSAn0J7RgtC/0YDQsNCy0LjRgtGMJzsgYnRuLmRpc2FibGVkID0gZmFsc2U7CiAgICBhbGVydCgn0J7RiNC40LHQutCwLiDQn9C+0L/RgNC+0LHRg9C50YLQtSDQtdGJ0ZEg0YDQsNC3LicpOwogIH0pOwp9OwoKPC9zY3JpcHQ+CjwvYm9keT4KPC9odG1sPgo="



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
