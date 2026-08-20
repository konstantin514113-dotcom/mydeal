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
BOOKING_HTML_B64 = "77u/PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9InJ1Ij4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idGhlbWUtY29sb3IiIGNvbnRlbnQ9IiMwYTBhMGEiPgo8bWV0YSBuYW1lPSJ2aWV3cG9ydCIgY29udGVudD0id2lkdGg9ZGV2aWNlLXdpZHRoLGluaXRpYWwtc2NhbGU9MSI+Cjx0aXRsZT5SJkogR3Jvb21pbmc8L3RpdGxlPgo8bGluayBocmVmPSJodHRwczovL2ZvbnRzLmdvb2dsZWFwaXMuY29tL2NzczI/ZmFtaWx5PUNvcm1vcmFudCtHYXJhbW9uZDp3Z2h0QDQwMDs2MDAmZmFtaWx5PVBsYXlmYWlyK0Rpc3BsYXk6aXRhbCx3Z2h0QDAsNDAwOzAsNjAwOzAsNzAwOzEsNDAwJmZhbWlseT1Nb250c2VycmF0OndnaHRAMzAwOzQwMDs1MDA7NjAwJmRpc3BsYXk9c3dhcCIgcmVsPSJzdHlsZXNoZWV0Ij4KPHN0eWxlPgoqe2JveC1zaXppbmc6Ym9yZGVyLWJveDttYXJnaW46MDtwYWRkaW5nOjB9Cmh0bWwsYm9keXttaW4taGVpZ2h0OjEwMHZoO2JhY2tncm91bmQ6IzBhMGEwYTtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXdlaWdodDo0MDB9Ci5zY3JlZW57ZGlzcGxheTpub25lO21pbi1oZWlnaHQ6MTAwdmg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2FsaWduLWl0ZW1zOmNlbnRlcjtwYWRkaW5nOjQ4cHggMCA2NHB4fQouc2NyZWVuLmFjdGl2ZXtkaXNwbGF5OmZsZXh9Ci5jb257d2lkdGg6MTAwJTttYXgtd2lkdGg6NDAwcHg7cGFkZGluZzowIDI4cHh9Ci5iYWNrLWJ0bntkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo4cHg7Y29sb3I6I2ZmZmZmZjtmb250LXNpemU6MC44cmVtO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyOm5vbmU7Y3Vyc29yOnBvaW50ZXI7cGFkZGluZzowO21hcmdpbi1ib3R0b206MzZweDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXdlaWdodDozMDA7dHJhbnNpdGlvbjpjb2xvciAuMnN9Ci5iYWNrLWJ0bjpob3Zlcntjb2xvcjojZmZmZmZmfQoubG9nby1yantmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZjtmb250LXNpemU6Mi41cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmfQoubG9nby1zdWJ7Zm9udC1zaXplOjAuNjYzcmVtO2ZvbnQtd2VpZ2h0OjYwMDtsZXR0ZXItc3BhY2luZzouNGVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO21hcmdpbi10b3A6M3B4O3BhZGRpbmctYm90dG9tOjE0cHg7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4yKTttYXJnaW4tYm90dG9tOjIwcHh9Ci5ob21lLXJqe2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmO2ZvbnQtc2l6ZTozLjI1cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjF9Ci5sb2dvLXRhZ3tmb250LXNpemU6MC43NXJlbTtsZXR0ZXItc3BhY2luZzouMTJlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjtsaW5lLWhlaWdodDoxLjV9Ci5sb2dvLXJvd3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6ZmxleC1lbmQ7Z2FwOjEycHg7bWFyZ2luLWJvdHRvbToyOHB4O3BhZGRpbmctYm90dG9tOjE4cHg7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4yKX0KLmxvZ28taW1nLXJvd3ttYXJnaW4tYm90dG9tOjI4cHg7cGFkZGluZy1ib3R0b206MThweDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjIpfQoubG9nby1pbWd7aGVpZ2h0OjkwcHg7d2lkdGg6YXV0bztkaXNwbGF5OmJsb2NrfQouaG9tZS1nc3Vie2ZvbnQtc2l6ZTowLjY2M3JlbTtmb250LXdlaWdodDo2MDA7bGV0dGVyLXNwYWNpbmc6LjRlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjttYXJnaW4tdG9wOjZweDttYXJnaW4tYm90dG9tOjIycHh9Ci5ob21lLWgxe2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6My4xMjVyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNmZmZmZmY7bGluZS1oZWlnaHQ6MS4xO21hcmdpbi1ib3R0b206NnB4fQouaG9tZS1oMSBlbXtmb250LXN0eWxlOml0YWxpYztjb2xvcjojZmZmZmZmfQouaG9tZS1zdWJ7Zm9udC1zaXplOjAuOHJlbTtsZXR0ZXItc3BhY2luZzouMThlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjI4cHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5vcHR7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTZweDtwYWRkaW5nOjE2cHggMDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNyk7dGV4dC1kZWNvcmF0aW9uOm5vbmU7Y29sb3I6I2ZmZmZmZjt0cmFuc2l0aW9uOmNvbG9yIC4ycztjdXJzb3I6cG9pbnRlcjtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyLXRvcDpub25lO2JvcmRlci1sZWZ0Om5vbmU7Ym9yZGVyLXJpZ2h0Om5vbmU7d2lkdGg6MTAwJTtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWZ9Ci5vcHQ6aG92ZXJ7Y29sb3I6I2ZmZn0KLm9wdC1pY29ue3dpZHRoOjM4cHg7aGVpZ2h0OjM4cHg7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2ZsZXgtc2hyaW5rOjB9Ci5vcHQtdGV4dHtmbGV4OjE7dGV4dC1hbGlnbjpsZWZ0fQoub3B0LXRpdGxle2ZvbnQtc2l6ZToxLjUxMnJlbTtmb250LXdlaWdodDo2MDA7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjJweDt0cmFuc2l0aW9uOmNvbG9yIC4ycztmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWZ9Ci5vcHQ6aG92ZXIgLm9wdC10aXRsZXtjb2xvcjojZmZmfQoub3B0LWhhbmRsZXtmb250LXNpemU6MC44ODdyZW07Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXdlaWdodDozMDB9Ci5vcHQtYXJyb3d7Y29sb3I6I2ZmZmZmZjtmb250LXNpemU6MS4yMjVyZW07ZmxleC1zaHJpbms6MDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLm9wdDpob3ZlciAub3B0LWFycm93e2NvbG9yOiNmZmZmZmZ9Ci5kaXZpZGVye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7cGFkZGluZzoxMnB4IDB9Ci5kaXZpZGVyOjpiZWZvcmUsLmRpdmlkZXI6OmFmdGVye2NvbnRlbnQ6Jyc7ZmxleDoxO2hlaWdodDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNil9Ci5kaXZpZGVyIHNwYW57Zm9udC1zaXplOjAuNjg4cmVtO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQouaG9tZS1mb290e21hcmdpbi10b3A6MzZweDtwYWRkaW5nLXRvcDoyMHB4O2JvcmRlci10b3A6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA2KTtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6Y2VudGVyfQouaG9tZS1mb290IHNwYW57Zm9udC1zaXplOjAuNzc1cmVtO2xldHRlci1zcGFjaW5nOi4yZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5mZG90e3dpZHRoOjJweDtoZWlnaHQ6MnB4O2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMTYpfQoucHJvZ3Jlc3N7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjttYXJnaW4tYm90dG9tOjQwcHg7b3ZlcmZsb3c6aGlkZGVuO2NvdW50ZXItcmVzZXQ6c3RlcH0KLnBze2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjVweDtmb250LXNpemU6MC42NjNyZW07bGV0dGVyLXNwYWNpbmc6LjEyZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7d2hpdGUtc3BhY2U6bm93cmFwO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2NvdW50ZXItaW5jcmVtZW50OnN0ZXB9Ci5wcy5kb25le2NvbG9yOiNmZmZmZmZ9Ci5wcy5hY3RpdmV7Y29sb3I6I2ZmZmZmZn0KLnBkb3R7d2lkdGg6MThweDtoZWlnaHQ6MThweDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7ZmxleC1zaHJpbms6MDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEyKTtmb250LXNpemU6MC42NjNyZW07Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC13ZWlnaHQ6NjAwfQoucGRvdDo6YmVmb3Jle2NvbnRlbnQ6Y291bnRlcihzdGVwLGRlY2ltYWwtbGVhZGluZy16ZXJvKX0KLnBzLmRvbmUgLnBkb3R7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLnBzLmFjdGl2ZSAucGRvdHtib3JkZXItY29sb3I6I2ZmZmZmZjtjb2xvcjojZmZmZmZmfQoucGx7ZmxleDoxO2hlaWdodDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNyk7bWFyZ2luOjAgNXB4O21pbi13aWR0aDo2cHh9Ci5wbC5kb25le2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMTgpfQouc3RlcHtkaXNwbGF5Om5vbmV9LnN0ZXAuc2hvd3tkaXNwbGF5OmJsb2NrO2FuaW1hdGlvbjpmdSAuMzVzIGVhc2UgYm90aH0KLnNsYmx7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjkzOHJlbTtmb250LXdlaWdodDo2MDA7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjIwcHg7bGV0dGVyLXNwYWNpbmc6LjAxZW19Ci5zYm94e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEwcHg7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTYpO3BhZGRpbmc6MCAycHg7dHJhbnNpdGlvbjpib3JkZXItY29sb3IgLjJzfQouc2JveDpmb2N1cy13aXRoaW57Ym9yZGVyLWJvdHRvbS1jb2xvcjojZmZmZmZmfQouc2l7b3BhY2l0eTouMjtmb250LXNpemU6MS4yMjVyZW07ZmxleC1zaHJpbms6MH0KI2JJbnB1dHtmbGV4OjE7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtvdXRsaW5lOm5vbmU7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjUxMnJlbTtjb2xvcjojZmZmZmZmO3BhZGRpbmc6MTJweCAwfQojYklucHV0OjpwbGFjZWhvbGRlcntjb2xvcjojZmZmZmZmfQouY2xye2JhY2tncm91bmQ6bm9uZTtib3JkZXI6bm9uZTtjb2xvcjojZmZmZmZmO2N1cnNvcjpwb2ludGVyO2ZvbnQtc2l6ZToxLjE1cmVtO2Rpc3BsYXk6bm9uZTtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZn0KLmNsci5zaG93e2Rpc3BsYXk6YmxvY2t9Ci5id3JhcHtwb3NpdGlvbjpyZWxhdGl2ZTttYXJnaW4tYm90dG9tOjIwcHh9Ci5kcm9we3Bvc2l0aW9uOmFic29sdXRlO2xlZnQ6MDtyaWdodDowO2JhY2tncm91bmQ6IzBmMGYwZjtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEpO2JvcmRlci10b3A6bm9uZTttYXgtaGVpZ2h0OjIwMHB4O292ZXJmbG93LXk6YXV0bzt6LWluZGV4OjUwO2Rpc3BsYXk6bm9uZX0KLmRyb3Aub3BlbntkaXNwbGF5OmJsb2NrfQouZGl0ZW17cGFkZGluZzoxMXB4IDE0cHg7Zm9udC1zaXplOjEuMzYzcmVtO2NvbG9yOiNmZmZmZmY7Y3Vyc29yOnBvaW50ZXI7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDUpO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLmRpdGVtOmhvdmVye2NvbG9yOiNmZmZ9Ci5kaXRlbSBtYXJre2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Y29sb3I6I2ZmZjtmb250LXdlaWdodDo3MDB9Ci5ub3Jlc3twYWRkaW5nOjE0cHg7Zm9udC1zaXplOjEuMjg4cmVtO2NvbG9yOiNmZmZmZmY7Zm9udC1zdHlsZTppdGFsaWM7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmfQoubm8tYnJlZWQtYmFubmVye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7cGFkZGluZzoxNHB4IDA7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDYpO2N1cnNvcjpwb2ludGVyO3RyYW5zaXRpb246Y29sb3IgLjJzO21hcmdpbi10b3A6NHB4fQoubm8tYnJlZWQtYmFubmVyOmhvdmVyIC5uby1icmVlZC1iYW5uZXItdGl0bGV7Y29sb3I6I2ZmZmZmZn0KLm5vLWJyZWVkLWJhbm5lci1pY29ue2ZvbnQtc2l6ZToxLjU3NXJlbTtmbGV4LXNocmluazowO29wYWNpdHk6LjN9Ci5uby1icmVlZC1iYW5uZXItdGV4dHtmbGV4OjF9Ci5uby1icmVlZC1iYW5uZXItdGl0bGV7Zm9udC1zaXplOjEuNDM4cmVtO2NvbG9yOiNmZmZmZmY7Zm9udC13ZWlnaHQ6NjAwO21hcmdpbi1ib3R0b206MnB4O2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLm5vLWJyZWVkLWJhbm5lci1zdWJ7Zm9udC1zaXplOjAuODg3cmVtO2NvbG9yOiNmZmZmZmY7bGluZS1oZWlnaHQ6MS41O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQoubm8tYnJlZWQtYmFubmVyLWFycm93e2NvbG9yOiNmZmZmZmY7Zm9udC1zaXplOjEuMjI1cmVtO2ZsZXgtc2hyaW5rOjA7dHJhbnNpdGlvbjpjb2xvciAuMnN9Ci5zYmFkZ2V7ZGlzcGxheTpub25lO2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTJweDttYXJnaW4tYm90dG9tOjIwcHh9Ci5zYmFkZ2Uuc2hvd3tkaXNwbGF5OmZsZXh9Ci5ibmFtZXtib3JkZXItYm90dG9tOjFweCBzb2xpZCAjZmZmZmZmO2NvbG9yOiNmZmZmZmY7cGFkZGluZzoycHggMDtmb250LXNpemU6MS40MzhyZW07Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmfQouYmNoZ3tmb250LXNpemU6MC44cmVtO2NvbG9yOiNmZmZmZmY7Y3Vyc29yOnBvaW50ZXI7bGV0dGVyLXNwYWNpbmc6LjEyZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO3RyYW5zaXRpb246Y29sb3IgLjJzfQouYmNoZzpob3Zlcntjb2xvcjojZmZmZmZmfQouc3ZidG57ZGlzcGxheTpibG9jaztwYWRkaW5nOjA7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wOCk7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Y3Vyc29yOnBvaW50ZXI7dGV4dC1hbGlnbjpsZWZ0O3RyYW5zaXRpb246Ym9yZGVyLWNvbG9yIC4yczt3aWR0aDoxMDAlO292ZXJmbG93OmhpZGRlbjtwb3NpdGlvbjpyZWxhdGl2ZX0KLnN2YnRuOmhvdmVye2JvcmRlci1ib3R0b20tY29sb3I6I2ZmZmZmZn0KLnN2YnRuLmFjdGl2ZXtib3JkZXItYm90dG9tLWNvbG9yOiNmZmZmZmZ9Ci5zdnB7Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNmZmZmZmY7ZmxleC1zaHJpbms6MH0KLm1hc3RlcnN7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyO2dhcDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNyl9Ci5tYnRue2JhY2tncm91bmQ6IzBhMGEwYTtwYWRkaW5nOjIycHggMTJweDt0ZXh0LWFsaWduOmNlbnRlcjtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmJhY2tncm91bmQgLjJzO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtib3JkZXI6bm9uZX0KLm1idG46aG92ZXJ7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wMyl9Ci5tYnRuLmFjdGl2ZXtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA1KX0KLm1hdnt3aWR0aDo0MHB4O2hlaWdodDo0MHB4O2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xNCk7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO21hcmdpbjowIGF1dG8gMTBweDtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjEuNDM4cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmfQoubWJ0bi5hY3RpdmUgLm1hdntib3JkZXItY29sb3I6I2ZmZmZmZjtjb2xvcjojZmZmZmZmfQoubW5hbWV7Zm9udC1zaXplOjEuNDM4cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZn0KLm1idG46aG92ZXIgLm1uYW1le2NvbG9yOiNmZmZmZmZ9Ci5tYnRuLmFjdGl2ZSAubW5hbWV7Y29sb3I6I2ZmZmZmZn0KLm10aXRsZXtmb250LXNpemU6MC44cmVtO2NvbG9yOiNmZmZmZmY7bWFyZ2luLXRvcDozcHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5nYnRue2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7cGFkZGluZzoxNHB4IDA7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wOCk7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjEuNDM4cmVtO2N1cnNvcjpwb2ludGVyO3dpZHRoOjEwMCU7dHJhbnNpdGlvbjphbGwgLjJzfQouZ2J0bjpob3Zlcntjb2xvcjojZmZmZmZmfQouZ2J0bi5hY3RpdmV7Y29sb3I6I2ZmZmZmZjtib3JkZXItYm90dG9tLWNvbG9yOiNmZmZmZmZ9Ci5jYWwtaHtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6Y2VudGVyO21hcmdpbi1ib3R0b206MTZweH0KLmNhbC1te2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6MS45MzhyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNmZmZmZmZ9Ci5jYWwtbntiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyOm5vbmU7Y29sb3I6I2ZmZmZmZjtjdXJzb3I6cG9pbnRlcjtmb250LXNpemU6MS41NzVyZW07cGFkZGluZzo0cHggOHB4O3RyYW5zaXRpb246Y29sb3IgLjJzfQouY2FsLW46aG92ZXJ7Y29sb3I6I2ZmZmZmZn0KLmNne2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDcsMWZyKTtnYXA6MnB4O21hcmdpbi1ib3R0b206MTJweH0KLmNkbnt0ZXh0LWFsaWduOmNlbnRlcjtmb250LXNpemU6MC42NjNyZW07Y29sb3I6I2ZmZmZmZjtwYWRkaW5nOjRweCAwO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtsZXR0ZXItc3BhY2luZzouMWVtfQouY2R7dGV4dC1hbGlnbjpjZW50ZXI7Y3Vyc29yOnBvaW50ZXI7Y29sb3I6I2ZmZmZmZjtib3JkZXI6MXB4IHNvbGlkIHRyYW5zcGFyZW50O3RyYW5zaXRpb246YWxsIC4yc30KLmNkOmhvdmVyOm5vdCguZGlzKTpub3QoLnBhZCkgLmNkLWlubmVye2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDcpIWltcG9ydGFudDtjb2xvcjojZmZmZmZmIWltcG9ydGFudH0KLmNkLnNlbCAuY2QtaW5uZXJ7YmFja2dyb3VuZDojZmZmZmZmIWltcG9ydGFudDtjb2xvcjojMGEwYTBhIWltcG9ydGFudDtmb250LXdlaWdodDo3MDAhaW1wb3J0YW50O2JvcmRlcjpub25lIWltcG9ydGFudH0KLmNkLnRvZCAuY2QtaW5uZXJ7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4yOCk7Y29sb3I6I2ZmZn0KLmNkLmRpc3tjb2xvcjojZmZmZmZmO2N1cnNvcjpkZWZhdWx0fQoudGd7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoNCwxZnIpO2dhcDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNyl9Ci50YnRue2JhY2tncm91bmQ6IzBhMGEwYTtib3JkZXI6bm9uZTtwYWRkaW5nOjEzcHggNHB4O3RleHQtYWxpZ246Y2VudGVyO2ZvbnQtc2l6ZToxLjMyNXJlbTtjb2xvcjojZmZmZmZmO2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjt0cmFuc2l0aW9uOmFsbCAuMnN9Ci50YnRuOmhvdmVye2NvbG9yOiNmZmZmZmY7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNCl9Ci50YnRuLmFjdGl2ZXtjb2xvcjojZmZmZmZmfQouc3Vte2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOm5vbmU7Ym9yZGVyLXRvcDoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDgpO3BhZGRpbmc6MjBweCAwO21hcmdpbi1ib3R0b206MjBweH0KLnNye2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjtwYWRkaW5nOjhweCAwO2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA1KTtmb250LXNpemU6MS4zNjNyZW07Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmfQouc3I6bGFzdC1jaGlsZHtib3JkZXItYm90dG9tOm5vbmU7cGFkZGluZy10b3A6MTRweH0KLnNse2NvbG9yOiNmZmZmZmZ9LnN2e2NvbG9yOiNmZmZmZmY7dGV4dC1hbGlnbjpyaWdodH0KLnNwe2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6Mi40MzhyZW07Y29sb3I6I2ZmZmZmZjtmb250LXdlaWdodDo2MDB9Ci5mZ3ttYXJnaW4tYm90dG9tOjIwcHh9Ci5mbHtmb250LXNpemU6MC43MTJyZW07bGV0dGVyLXNwYWNpbmc6LjIyZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbTo4cHg7ZGlzcGxheTpibG9jaztmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZn0KLmZpe3dpZHRoOjEwMCU7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xNCk7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjEuNTEycmVtO3BhZGRpbmc6MTBweCAwO291dGxpbmU6bm9uZTt0cmFuc2l0aW9uOmJvcmRlci1jb2xvciAuMnN9Ci5maTpmb2N1c3tib3JkZXItYm90dG9tLWNvbG9yOiNmZmZmZmZ9Ci5jYnRue2Rpc3BsYXk6YmxvY2s7d2lkdGg6MTAwJTtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC1zaXplOjAuODYycmVtO2ZvbnQtd2VpZ2h0OjYwMDtsZXR0ZXItc3BhY2luZzouMjhlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7dGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzoxNnB4O2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMjUpO2N1cnNvcjpwb2ludGVyO3RyYW5zaXRpb246YWxsIC4yc30KLmNidG46aG92ZXJ7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLnNibG9ja3t0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjUycHggMjBweDtkaXNwbGF5Om5vbmV9Ci5zYmxvY2suc2hvd3tkaXNwbGF5OmJsb2NrO2FuaW1hdGlvbjpmdSAuNXMgZWFzZSBib3RofQouc2kye2ZvbnQtc2l6ZTozLjZyZW07bWFyZ2luLWJvdHRvbToyMHB4O29wYWNpdHk6LjR9Ci5zdHtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjIuNzI1cmVtO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbToxMHB4O2ZvbnQtd2VpZ2h0OjYwMH0KLnNze2ZvbnQtc2l6ZToxLjA3NXJlbTtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjEuOTttYXJnaW4tYm90dG9tOjI4cHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5oYnRue2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xNik7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXNpemU6MC44NjJyZW07bGV0dGVyLXNwYWNpbmc6LjIyZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO3BhZGRpbmc6MTNweCAyOHB4O2N1cnNvcjpwb2ludGVyO3RyYW5zaXRpb246YWxsIC4yc30KLmhidG46aG92ZXJ7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLmxvYWRpbmctc2xvdHN7Y29sb3I6I2ZmZmZmZjtmb250LXNpemU6MS4yODhyZW07cGFkZGluZzoxMnB4IDA7dGV4dC1hbGlnbjpjZW50ZXI7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc3R5bGU6aXRhbGljfQouY2R7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpjZW50ZXI7YWxpZ24taXRlbXM6Y2VudGVyO2hlaWdodDozNnB4IWltcG9ydGFudDtwYWRkaW5nOjAhaW1wb3J0YW50fQouY2QtaW5uZXJ7d2lkdGg6MzJweDtoZWlnaHQ6MzJweDtib3JkZXItcmFkaXVzOjA7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2ZvbnQtc2l6ZToxLjE1cmVtO2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZn0KLmNkLmF2YWlsIC5jZC1pbm5lcntib3JkZXI6MXB4IHNvbGlkIHJnYmEoOTAsMTgwLDkwLC4zNSk7Y29sb3I6cmdiYSg5MCwxODAsOTAsLjY1KX0KLmNkLmJ1c3kgLmNkLWlubmVye2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDcpO2NvbG9yOiNmZmZmZmZ9Ci5jZC5zZWwgLmNkLWlubmVye2JhY2tncm91bmQ6I2ZmZmZmZiFpbXBvcnRhbnQ7Y29sb3I6IzBhMGEwYSFpbXBvcnRhbnQ7Zm9udC13ZWlnaHQ6NzAwIWltcG9ydGFudDtib3JkZXI6bm9uZSFpbXBvcnRhbnR9Ci5jZC50b2QgLmNkLWlubmVye2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMjgpO2NvbG9yOiNmZmY7Zm9udC13ZWlnaHQ6NjAwfQouY2QuZGlzIC5jZC1pbm5lcntjb2xvcjojZmZmZmZmO2N1cnNvcjpkZWZhdWx0O2JhY2tncm91bmQ6bm9uZTtib3JkZXI6bm9uZX0KLnN2YnRuLXJvd3tkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6YmFzZWxpbmU7bWFyZ2luLWJvdHRvbTo2cHg7cGFkZGluZzoxNnB4IDAgMH0KLnN2YnRuLW5hbWV7Zm9udC1zaXplOjEuNTEycmVtO2NvbG9yOiNmZmZmZmY7Zm9udC13ZWlnaHQ6NjAwO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZn0KLnN2YnRuLmFjdGl2ZSAuc3ZidG4tbmFtZXtjb2xvcjojZmZmZmZmfQouc3ZidG4tcHJpY2V7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjcyNXJlbTtjb2xvcjojZmZmZmZmO2ZvbnQtd2VpZ2h0OjYwMDtmbGV4LXNocmluazowfQouc3ZidG4uYWN0aXZlIC5zdmJ0bi1wcmljZXtjb2xvcjojZmZmZmZmfQouc3ZidG4tZGVzY3tmb250LXNpemU6MXJlbTtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjEuNztkaXNwbGF5OmJsb2NrO3BhZGRpbmc6MCAwIDE0cHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7d2hpdGUtc3BhY2U6cHJlLWxpbmV9Ci5zdmJ0bi5hY3RpdmUgLnN2YnRuLWRlc2N7Y29sb3I6I2ZmZmZmZn0KLnN2YnRuLXRhZ3tmb250LXNpemU6MC45NzVyZW07Y29sb3I6I2ZmZmZmZjtmb250LXN0eWxlOml0YWxpYztkaXNwbGF5OmJsb2NrO21hcmdpbi10b3A6MnB4O3BhZGRpbmc6MCAwIDE0cHg7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmfQouc3ZidG4uYWN0aXZlIC5zdmJ0bi10YWd7Y29sb3I6I2ZmZmZmZn0KQG1lZGlhKG1heC13aWR0aDo0MDBweCl7LnN2YnRuLW5hbWV7Zm9udC1zaXplOjEuMzYzcmVtfS5zdmJ0bi1wcmljZXtmb250LXNpemU6MS41MTJyZW19LnN2YnRuLWRlc2N7Zm9udC1zaXplOjAuOTM4cmVtfS5zdmJ0bi10YWd7Zm9udC1zaXplOjAuODg3cmVtfX0KQGtleWZyYW1lcyBmdXtmcm9te29wYWNpdHk6MDt0cmFuc2Zvcm06dHJhbnNsYXRlWSgxMHB4KX10b3tvcGFjaXR5OjE7dHJhbnNmb3JtOnRyYW5zbGF0ZVkoMCl9fQoubGFuZy1iYXJ7cG9zaXRpb246Zml4ZWQ7dG9wOjEycHg7cmlnaHQ6MTRweDt6LWluZGV4Ojk5OTtkaXNwbGF5OmZsZXg7Z2FwOjZweH0KLmxhbmctYnRue2JhY2tncm91bmQ6cmdiYSgxMCwxMCwxMCwuOTIpO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMSk7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXNpemU6MC43NzVyZW07bGV0dGVyLXNwYWNpbmc6LjE1ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO3BhZGRpbmc6NXB4IDEwcHg7Y3Vyc29yOnBvaW50ZXI7dHJhbnNpdGlvbjphbGwgLjJzfQoubGFuZy1idG46aG92ZXJ7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLmxhbmctYnRuLmFjdGl2ZXtib3JkZXItY29sb3I6I2ZmZmZmZjtjb2xvcjojZmZmZmZmfQouY2JrLWJ0bntiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTQpO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC1zaXplOjAuODYycmVtO2xldHRlci1zcGFjaW5nOi4xNmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtwYWRkaW5nOjEycHggMjBweDtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmFsbCAuMnM7d2lkdGg6MTAwJX0KLmNiay1idG46aG92ZXJ7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLm1idG4sLnN2YnRuLC5nYnRuLC50YnRuLC5jYnRuLC5oYnRuLC5jYmstYnRuLC5sYW5nLWJ0biwuYmFjay1idG4sLm9wdCwuZGl0ZW0sLmNkLC5uby1icmVlZC1iYW5uZXIsLmJjaGd7dHJhbnNpdGlvbjphbGwgLjE1cyBlYXNlfQoubWJ0bjphY3RpdmUsLnN2YnRuOmFjdGl2ZSwuZ2J0bjphY3RpdmUsLnRidG46YWN0aXZlLC5jYnRuOmFjdGl2ZSwuaGJ0bjphY3RpdmUsLmNiay1idG46YWN0aXZlLC5sYW5nLWJ0bjphY3RpdmUsLmJhY2stYnRuOmFjdGl2ZSwub3B0OmFjdGl2ZSwuZGl0ZW06YWN0aXZlLC5jZDphY3RpdmUsLm5vLWJyZWVkLWJhbm5lcjphY3RpdmUsLmJjaGc6YWN0aXZle3RyYW5zZm9ybTpzY2FsZSgwLjk2KX0KPC9zdHlsZT4KPC9oZWFkPgo8Ym9keT4KPGEgaHJlZj0iL2FkbWluP3Bhc3M9YW56YTE5ODUiIGlkPSJhZG1pbkJhY2tMaW5rIiBzdHlsZT0iZGlzcGxheTpub25lO3Bvc2l0aW9uOmZpeGVkO3RvcDoxNHB4O3JpZ2h0OjE0cHg7Zm9udC1zaXplOjAuOXJlbTtjb2xvcjojYzlhMDVhO3RleHQtZGVjb3JhdGlvbjpub25lO3otaW5kZXg6OTk5O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2JhY2tncm91bmQ6cmdiYSgxMCwxMCw5LC44NSk7cGFkZGluZzo2cHggMTJweDtib3JkZXItcmFkaXVzOjIwcHg7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDIwMSwxNjAsOTAsLjM1KSI+4oaQINCQ0LTQvNC40L0t0L/QsNC90LXQu9GMPC9hPgo8c2NyaXB0PmlmKGxvY2F0aW9uLnNlYXJjaC5pbmRleE9mKCdwYXNzPWFuemExOTg1JykhPT0tMSl7ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2FkbWluQmFja0xpbmsnKS5zdHlsZS5kaXNwbGF5PSdibG9jayc7fTwvc2NyaXB0Pgo8ZGl2IGNsYXNzPSJsYW5nLWJhciI+CiAgPGJ1dHRvbiBjbGFzcz0ibGFuZy1idG4gYWN0aXZlIiBvbmNsaWNrPSJzZXRMYW5nKCdydScpIj5SVTwvYnV0dG9uPgogIDxidXR0b24gY2xhc3M9ImxhbmctYnRuIiBvbmNsaWNrPSJzZXRMYW5nKCdlbicpIj5FTjwvYnV0dG9uPgogIDxidXR0b24gY2xhc3M9ImxhbmctYnRuIiBvbmNsaWNrPSJzZXRMYW5nKCdldCcpIj5FVDwvYnV0dG9uPgo8L2Rpdj4KCjwhLS0gSE9NRSAtLT4KPGRpdiBjbGFzcz0ic2NyZWVuIGFjdGl2ZSIgaWQ9ImhvbWVTY3JlZW4iPgo8ZGl2IGNsYXNzPSJjb24iPgogIDxkaXYgY2xhc3M9ImxvZ28taW1nLXJvdyI+CiAgICA8aW1nIHNyYz0iZGF0YTppbWFnZS9wbmc7YmFzZTY0LGlWQk9SdzBLR2dvQUFBQU5TVWhFVWdBQUFVTUFBQURyQ0FZQUFBRHpDL1F3QUFBQldHbERRMUJKUTBNZ1VISnZabWxzWlFBQWVKeDlrTEZMdzFBUXhyOVdwYUIxRUIwY0hES0pRNVNTQ3JvNHRCVkVjUWhWd2VxVXZxYXBrTVpIa2lJRk4vK0JnditCQ3M1dUZvYzZPamdJb3BQbzV1U2s0S0xsZVMrSnBDSjZqK04rZk8rNzR6Z2dPVzV3YnZjRHFEdStXMXpLSzV1bExTWDFqQVM5SUF6bThaeXVyMHIrcmovai9UNzAzazdMV2IvLy80M0JpdWt4cXArVUdjWmRIMGlveFBxZXp5WHZFNCs1dEJSeFM3SVY4b25rY3NqbmdXZTlXQ0MrSmxaWXphZ1F2eENyNVI3ZDZ1RzYzV0RSRG5MN3RPbHNyTWs1bEJOWXhBNDhjTmd3MElRQ0hkay8vTE9CdjRCZGNqZmhVcCtGR256cXlaRWlKNWpFeTNEQU1BT1ZXRU9HVXBOM2p1NTNGOTFQamJXREoyQ2hJNFM0aUxXVkRuQTJSeWRyeDlyVVBEQXlCRnkxdWVFYWdkUkhtYXhXZ2RkVFlMZ0VqTjVRejdaWHpXcmg5dWs4TVBBb3hOc2trRG9FdWkwaFBvNkU2QjVUOHdOdzZYd0JBNmRpRThIWVdoTUFBRUh3U1VSQlZIaWM3WjE1ZkZWRnN2anIzRFg3Qm9RbFFBaWJLQUkrVUZCeFgxQVp3SEY1SWlCUEhSY2VEaTdvcVBoVFJsRkFRY1ZSVVo4UFVYSFVKK0xvNEs2QUFzNjRvT0RHSWhEQ2tvUkE5dlZ1WjZuZkgxaE5uNzduSmpjUUlJSDZmajc1M0NYbmR2ZnBjN3BPVlZkMU5RRERNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNRXpiUUR2U0RUaGFjTGxjZ0lpZ2FScFlsZ1Z1dHh0TTB3Uk5PN2d1UnNTb2VnQUFMTXNDQUJEbDAzR2Fwb2syTUMyUHgrTUJ3ekFPK3JyS0lDSzRYQzV4emVUM0ROTm1JT0VrbzJrYWVEeWVGaW5mNC9HQTIrMk9XVThzV3JJTnh6b3VsMHYwdjl2dEJyZmIzYUxsYTVvR2JyY2JmRDZmK0s2bDYyQ2FoalhERnNEbjgwRWtFckY5UnhxYXF0azFGL24zVGxxZy9IL1dLQTRkYnJjYkVGSDhrU1hRRXFqWDArdjFncTdyTFZJMnd4eFdWQTNNU1lzN1VIdytuMDBEYkVvYmRMbGNMV3JDSGV0NFBCN1JuM0svdHFUV3JWNHYrc3phSWRPbWtNMG53dXYxQ3NIVWtxakNqbDVsTTA3K3pFS3haWkN2cmN2bGFuRWhKWmRIcGpKUGNUQnRFbFVRdGpRa0JGWGhHcS9HeUJ3NGREMXAyZ05nLy9WdWlZZGRyTEw0ZWpKdER2V205ZnY5QUdBM3J3NjJmTFVjV2V1VEo5M3BmN0hheGh3WXFpWVl5NkYxSUxoY0xxRUZVajJ5QUdZT0g5emJMWURQNTRPSEhub0lPM2Z1REpGSXBFWE5LRGxjeHpBTTBIVWQ2dXJxb0tTa0JQYnUzUXViTm0zU1NrcEtvTEt5RWdEc1RwU1djT0FjNjV4Kyt1azRhZElrQUFDSVJDTGc5WG9oSEE2RDIrMEd5N0lPV2lnYWhnRVZGUlh3OE1NUGErRndXSHpQempDbVRaS1ltQWpyMTYvSGNEaU1obUVnSXFLdTYyaFpGcHFtaVlnb1h1bi9oSzdyaUlob1dSWmFsbVg3SDMybVYvbTM4dnRJSklLR1llRGF0V3Z4amp2dXdMeThQRWhOVFkzU0V1a3p2UjRPemNPcExucFkwS3VUOWlzalAxem8yRU14Sit2RXVISGpSRCtyMTFLRnJvbDZqZVZyS0Y5eituNzkrdldZa3BJaU5NVERjVjVNTkt3WnRnQStudyttVHAyS2FXbHBvR2thSkNVbFFidDI3V0R3NE1Gd3dna25nR1ZadG1Cc1hkZkI2L1VLclk4K2s2WkJtb2ZINHhIZm1hWXBoRUlrRWdHZnoyZlRUT1QzaG1IQSt2WHI0YVdYWG9JVksxWm9CUVVGSXZSSERzK2h3UEJERFlXS09HbXE5SjJzQ2RGNWE1b20ycWVHRmNtL1BaU2NjY1laT0hIaVJQQjRQT0R6K1NBM054Y0dEeDRNQ1FrSjRoanFTMVdncXlFemRLMi8vZlpiS0Nnb0FGM1hJU0VoQVlxS2l1RGhoeC9XZ3NIZ1lUMDN4ZzRMdzRPRUJBcVpUWFFEZXp3ZU9PNjQ0K0NpaXk3Q0o1OTgwaWJNQUVBSVFIcXRyYTJGMU5SVTJMbHpKMnpjdUJIV3JWc0hSVVZGVUYxZExjcE1Ta3FDcmwyN3dybm5uZ3Rubm5tbXplT29hWnBOaUFhRFFmRDcvZkR6enovRDBxVkxZZmJzMlpxdTY3YjR1TU14Mk5SQlRTczQ1TDZqNzAzVGpHczFCczJaSGc1QlRtMEQyUGVReWN2TGcrT1BQeDVuekpnQko1OThzamlHaEowVGRPMzM3TmtETjl4d0Eyelpza1VyTEN5RVNDUUNpR2lMVTZWK29QTm1nY2kwS1dLWk5iUjY1UGJiYjBmVE5ERWNEdHRNTE5rTXJxeXN4QjQ5ZW9EUDU0UEV4RVFBMkNkSWFESmREcmx3dTkyUWtaRUJNMmJNUUVURVFDQmdLOU0wVFp2WkhRZ0VjTjI2ZGFpYXpvY2pqbzBFaVd6V3FtRWo4bWNuQjVDNitrTTE5dzhsY2gyeW84UHY5OE9MTDc2SW9WRElaZzdMMHg2eVNWMVdWb2JkdW5XRHBLUWtVYTdzQ0NQSEc4TzBhV2lReXNKRm5nODc0WVFUWU4yNmRiWkJJd3RGMHpTeHJLd00yN1ZySjM1RFpYaTlYdHU4R3cwZ2VzM096b2EzMzM0YnE2dXIwYklzRElmRFVlVmJsb1dHWWVEbm4zK09mZnYyQllBak0vamNiamVrcDZmRHFhZWVpdVBHamNQWFgzOGR0MjdkaW5WMWRVSjRSQ0lSTENzcnd3OC8vQkN2dnZwcUhEeDRNQUxZUGVpcWtEeGNiVmZuT3Z2MTZ3ZmZmZmVkcmI5cHZwQWVTSWlJNFhBWXI3MzIycWp6QUlDb2VVSmFta2Z2R2FiTjRCUWZSamUwSFA3eTBVY2ZvV0VZcU91NkdEZzBXSFJkeCtycWF1emN1Yk1vUTM1dHJDNlB4d05wYVdrd2NlSkVySyt2anhLNGhtSFlCdXF5WmNzd0t5dnJzQTAwT1Q3eXozLytNNzd4eGhzMjRVZEVJcEVvUjVGbFdiaGp4dzU4L1BISE1UYzMxeVlrV2lwMHFTbms2Nmc2Z2R4dU56ejExRk1ZRG9lam5GMHl2Lzc2S3g1MzNIRTJqZC9wVmRXWWVRVUsweVp4OG9qS04vTnJyNzBXcFEyU1NXV2FKbFpXVm1LSERoMUVXVEswb2dYQVdVTUMyQ2R3VHp2dE5BeUh3MUdtbW15K1JTSVJYTGR1M1dHYmlISzVYTkNwVXlkWXZIZ3hWbGRYQzAySjJvSzR6NFQ4K3V1djhldXZ2eGJIMFArb245YXVYWXNubjN3eUhvbVZOZW9LRkpscnJybkdaaXFybm1iTHNuREZpaFZJZ2hEQW5rUkRmbGp3TWp6bXFFRU5tZ1hZUDNqbXpadG5FMHBxbUVaRlJRVzJhOWZPY2FVRFFMU3dkWHJ2OVhwaC9QangyTkRRWUJ1VVZDZHBYTUZnRUtkUG40Nkh3OHpNenM2R3p6NzdUQWczRWhDaFVBaS8vLzU3UFBQTU0xRU9QUGI3L2ZESUk0OWdUVTJOemVSRVJDd29LTUNjbkJ4Ujl1RVVHcktKTEQrWWhnNGRpclcxdGVMYzVMQWFhdmZTcFV0Um5qTlZ6VzJBNkN4RGJDSXpSeDMwNUo4MmJacE5HS2hVVkZRSXpaQm96b0NnZ1phYW1ncExseTYxelZuUndKUS8vL0RERDJMKzhHQ0ZpcXk1RW02M0d4SVNFdUNmLy93bm1xWnBFNGFJaU04OTl4d21KaWJHWE1vNGFkS2tLTE0vRW9uZ3FsV3JzRFU1Ry9MeThxQzZ1dG9tQk5YcnUzanhZZ1N3TzVGWSsyT09PVWlqdS9mZWUyMkRSUjB3QnlvTXlic01BQ0tzNXVTVFR4WmFpbXEya1VuWDBOQ0FFeVpNc0drc1ZDZVpvczNWVG1TaHFHa2FUSjgrUGFwKzB6Unh5WklsbUoyZGJmc3RlY3ZwWERJeU1tRFJva1cyTnBQQXVmbm1tMXVOUU96ZXZUdFVWVlhGRE1SR1JGeTBhSkVRaGtmQytjUEVCMStSUXd6K252L3VVTVdNVWRabGl0a3pUUlBXcmwyckZSWVcyc0pRS0ZiUDUvTUJJa0pTVWhLTUd6ZE9CSHZMUWMxMGJIUGJxK3U2aUxkTFQwK0hNV1BHaVBvamtRaFlsZ1dXWmNFWFgzd0JwYVdsdHJBYmlyT2o3T0RWMWRXd1pzMGFDSWZENFBmN2Jjc2N4NHdaMDJyeS9jbm5yRUxYdnFHaHdmYWQvTXEwSGxnWUhnVlFFRFBBL2dINHdBTVBDQUVqcjRDaElHakRNT0NjYzg0UkdwbnFtR2pPWUZWakNMMWVMd3diTmd5N2QrOHV5dkg1Zk9CeXVXRFBuajN3MDA4L2dhWnBRcENyU1NjUUVSSVNFdUNISDM2QVBYdjJpSExwM0RwMjdBaloyZG10WWw3Tk1BemJ3OFFKZVdVSndjS3c5Y0hDOEFod0lDWm9VMlhSS2dlWHl3VStudytXTEZtaXljS0Y2cU5sZXg2UEI1S1RrNEZpRzFYTnRUbG1uRncrSWtJa0VvSCsvZnNET1lSSVdDTWkxTlhWUVhWMXRZWlN0bWhhcGlobkN3K0ZRckIxNjFhdHFxcktWb2VtYVpDYW1pcmFmYVJwU3FqUkVyeldJTGlaeG1GaDJNWWhvUUt3ejhTazdEYkJZQkFhR2hxRW9BSFlyLzNSQUxZc0MzSnljbEJOWVg4ZzgxbGszdElTT1ovUEoweDNlUk9sek14TTZOeTVNMUw5VHJHVVZEOWxjYUZ6Q0lWQ0FBRFEwTkFnbHJJZGFaeldUS3YvazkrelVHeTlzREJzNDZqSkZ1VFVVc0ZnVUF3KzBzNUlHSklRVFV0THM1Vkg1dlRCcEk5U056YVNOY2YyN2R2RHNHSER4UDlsUVU3SksrU0VEWFFNbWM2V1pVRkRRd09VbEpTMENvK3NVL0MzS2hobGdjbENzZlhDd3ZBd29RNlFsdEpxYUZFL1FMUmdESVZDUXZqSmMyNXk4Z05kMTIxSkVXVGlFVGJ5NEtiQkhvbEVRTmYxS0NGTjdSZzJiQmhRdktEY2ZzTXdSQVlZQUlDZVBYc2lDV3NTMG9nSU8zZnVoSWFHaHNPV3FLRXhxSzJ4cmkvMUNYMW1ZZGg2WVdGNGlJbDE4N2ZrbkdFczd5K2xtU0t0VU43SG1ZNHRMQ3pVU0FNallTTnJhMDBoRDNaNTBOZlYxVUVnRUJCdHBIa3p5N0xnNG9zdmh2NzkreU1KRWxrVGxQdmx6RFBQQkZxaUtBdnhSeDU1UkNQQkt2ZERZeXVBNUdQa1B6cE9YU1BzbEpMTGlhYTJjVlhiUmRlZ0thY0xjL2hoWWRqR0lmTVJZTDl6aEFaWldsb2FHSVloZ3BrcFBaUmxXZUR4ZUNBY0RrTkZSWVhOaEhaNmJRelMrS2d0SklDKytlWWIyTE5uRHhpR0lVeGdlVDd4bzQ4K2d0VFVWQURZSDJ4Tm1xRnBtdENoUXdjNDU1eHpJREV4VVdpdmlBalRwMCtIZ29JQ2Nidzh4MGp0SU1HbUNuUFNqT1UvT282T3BmbEpTaWNXei9telVEczZZR0hZeHRFMFRUZ1cvSDYvME00eU16UEI0L0dBclBYUjhTNlhDd3pEZ0czYnRrRnRiYTM0WHZZNHh3dDVzVlVCVkZCUW9PM2V2VHNxS0Z4MjRuejIyV2VZbDVjSGNwNUZFaTVUcGt6QlVhTkdpWDJFQTRFQVBQLzg4L0RFRTA5b3NvQ1g1emZsK1ZGNWlWK3NmcVA0VEFDN0kwbzFhUnZqVUd3QXhod1pXQmkyY1VoWUFPenp2dElBSGp0MmJOVGFZMW5vdUZ3dWVPdXR0NFM1SnM4ak5rY1kwUEVrZ0FEMm1ab05EUTJ3WU1FQzRmV1Z0VFRMc3NEcjljSkpKNTBFVTZkT3hheXNMTnU4NVlJRkMvQ3ZmLzJyMERwcmFtcGd4b3daOE5CREQybFVEZ1ZkazJrdGEzcHlteHByTTdWTEZvamtnSkpOL3NidysvMTRxS1pBR09hb2dnYkczWGZmN2JnbW1UalE1WGgwakpvZ3RiQ3dVQ3pIazljbDB4cmg2dXBxdlBqaWk2T1dpY2xseHV0QWtaZVl5V2E2MisyRzh2SnlrYVVHTVhwL0VNdXk4SjU3N2tHWHl3VURCZ3pBOWV2WG82N3JZZ2xlUVVFQnBxYW1SczBuT3EySGx0dnNsQkEyVnNZYnRjMXFmemJHS2FlY2dyVzF0WTdaYW9qSEgzOGNuZnFYaFdicmdqWEROZzVwTDZRRmVUd2VPUGZjYzdGcjE2NDJJVUNha05mcmhVZ2tBdSsvL3o1ODlkVlhHcFhocEEzRzY2Mmxjc2xrSmVlR2FacHc0b2tuYWlVbEpXSVZoaXhnTGNzQzB6Umh6cHc1c0dyVkt2emxsMStnWDc5K1VGOWZENnRYcjRZLy9lbFAwS2RQSHkwWURJcnpJL09YbHNHUlZxeHF3ZkxlTVRSUEdtdEpKSDB2eDBuR2k4L25PK0NWTzB6cklyN0hIOU5xSWZPV3pMcXNyQ3k0N2JiYkFBQnMzOHNlNGxBb0JFODk5WlJ0elN6QWdXMUNSTWVyMjVOU2tQWGV2WHRoeG93WjhOUlRUNG5rcGlTWVpPM3JqRFBPZ0pLU0VuanZ2ZmRnMmJKbHNIejVjcTIrdmw3OG4weG1WYURwdWc2Wm1ablFwMDhmMUhWZHJMMm0rVURTS09sUFRrS2hhWnFZai96eXl5ODErZnpqRllxa29UckZHckxtMTdaZ1lkakdrYjJmUHA4UFJvNGNpU05IamhRYlRRSHNENzhod1huNTVaZkRqei8rcUtsSkhHUmhJQXVmZUNEaElXOWtSRUw0alRmZTBNNC8vM3djTjI2YzBGSXA3bEVXNWo2ZkR6NzU1QlA0NUpOUE5KckxvM0xwbGI2amNqUk5nd2tUSnVEOTk5OHZObGFTblRheTRJd2xuRFpzMkFBWFhIQ0JtSE9sY3VQMUpqTkhCMndtdHdMSSthRHVvQ2ZQeFJFZWo4ZVdHWmtFWFVKQ0FseHd3UVc0Y09IQ3FQMVlLRXlrdXJvYUprMmFCRjk4OFlWR2pnSloyTW52eWJSc0NyV05ja2dLQ1RyVE5PSEJCeC9VZ3NHZzdSelZlYlIyN2RyQjdObXpvVmV2WHFLc1dQTnJKSEF0eTRMcTZtcll2bjA3N04yN0YrcnI2OEh2OTBObVppWmtabVpDVmxZV1pHVmxRVVpHaHZndVBUMGRhRnZYMnRwYXFLK3ZqOG9tRSs5RGdNS2E1R2tHRXZKc01qT01SRk1PRkhJbzFOVFVZRlpXRmdEc0V5S3FJMEF1aTk3VFg4K2VQV0gyN05sUkUvZUkrL2RCMmJ0M0wwNmFOQW5UMDlOYjNIeFR3MVRJL0FRQVNFOVBoMm5UcHVIUFAvOHNIRGkwZzU5OC9yS0Q1N1BQUHNQTXpFeXhyTTlwWHRBcGNCb0FvRy9mdnZEZ2d3OWlUVTJOS0plY1NJajdzbndIQWdHY05Xc1cvdGQvL1JjT0dqUUllL2JzS2NwdExNVy9FMlBHakluS0xLN3VoOElPRklhQitMekpobUZnWldVbHRtL2ZQdWEybWFxMlIvTmZVNlpNd2Z6OGZBeUZRbWhabGtqeFQ0SVFFYkc0dUJpSERSdUc2a0J2Q1JNdjFxb1BUZE5nN05peCtNTVBQMkFvRkVKZDE5R3lMTnkyYlJ0V1ZGUkU3ZE5pR0lZdDZlMEhIM3lBQ1FrSmpsdDF4dHBxbElTajMrK0g1NTkvUGlxN055SmllWGs1bm5MS0thaHBta2czSnBkQjdaZlhWemZHRlZkY2dZRkF3RllQQzBPR2NhQXBZVWpmVlZSVVlLZE9uY1NrUDNsTkV4SVNoQ0JNU2txQ3RMUTA2TmV2SDh5Wk0wZUVyRkI2ZkhYZmsrcnFhbnpzc2Nkc1dhRkpDTFFrY3VBeDdlazhlL1pzMng3T2htRmdmbjQrQWdEY2YvLzlXRjlmTDhKODVOQWJFcEtCUUFDZmZmYlpxTFlUcXRhbWZrNU5UUVc1VDNSZHg5cmFXcnpra2t0UTFtTGwzOHBseEp1NVo5eTRjVkhDVUwyMkxBemJCdXhBT2NLUUE4VG44OEU5OTl5RDVlWGxZbGthSlVSdDM3NDlkT3pZRVhKemM2RnYzNzZRbFpWbFcxS1duSndNQVBzR2NDQVFnTFZyMThMMjdkdGgzcng1OFBQUFAyc0Erd1FXaGFNUUIrSTlkb0tDdVFFQWhnMGJodmZjY3crTUhqMWFwTyt5TEF0V3Jsd0pJMGVPMU54dU44eWFOVXZMenM3RzIyNjdEV1FQTVA0ZTlJeS9MekdjUEhreTdOMjdGMmZQbnExUjJBczVTZVE1UFRsZ25EN0xDVldwanovNzdEUDQ3cnZ2TkpUbU5La2ZBT3pMK1F6REVLK04wZGdLRkJaMkRDTVJiOUMxdW9NZGFSYnFmaW55WjluRUxDMHR4VnR1dVFWSGp4Nk51Ym01VWZXVFdYMG92SitrYVY1NjZhVzRaY3VXcUIzaVZxeFlnYm01dVVMNDBMTEJSeDU1eEdiV3E1dEdXWmFGZ1VBQUgzMzBVWlRyY1FxU3BuaEtpcTNNeU1nQXVheXFxaXI4d3gvK0VDWDVuVEwxeEdzaUF3RGNlT09OR0F3R0c5MDNtVFZEaG9INDV3d1I5MjNTNUNRWWRGMlBXcmtoUXdLRkFxMEpKeWNNbVowdExSVFBPdXNzM0xGamgyaExNQmhFMHpTeHJxNU83TUluOTRmWDY0V1VsQlI0OGNVWGJRSlJGcUp5djl4MzMzMm96cG5LMjR1cTUzbmlpU2NDOVkxcG1yaHg0MFpVSFRHMER0cHBaVXE4WnZMa3laTnQreWF6TUdTWUdEUWxER2xPcTdTMEZJY1BINDREQmd6QWdRTUg0b0FCQS9Dc3M4N0NKNTU0QXRldFd5Y2NETExna0xYRVVDaUVoWVdGbUpPVFk5TnNhRkFmeW9RQ0tTa3BzSExsU3NmenV2UE9POFVhYVhsSkhiM201dWJDUng5OUpJNTMwZzRSOXprK3JyLytlb3gxTHFvRDVPV1hYeFpsQmdJQjdOKy92emhXem5Rai81Wm9UbDlObVRMRk51ZnBwQ0d5TUdRWWlFOHp0Q3dMS3lzcm83YlBKUEx5OHVDZGQ5NFJ4OHNDUTlZWURjUEFKNTk4RWxOU1VzUnYxWmcrZWUxdGN3ZGpyTkNlSjU1NHdsRnpMU2dvaUd0Q2NzQ0FBZmp0dDkrS2MxQ25BdWk3MHRKU3ZPR0dHNFNHcUo0YkNiak16RXdvS1NrUlFtcmV2SG5vZEZ4TDhKZS8vRVhVNCtSUlptSElNTDl6c01LUXRKYWhRNGVLV0QwbnlHdGJWMWVIRjE1NElhcmFUcXdrcE0xQlhzcEdnaWd2THcvSXZDZE5qSVREdEduVDR2Yk81T1Rrd0lZTkcyd2VjWG92QzhhcXFpcTg1WlpiYk1KTkZUVDMzWGVmTUYwM2JkcUUvL0VmL3hGMWZFc0pvbnZ1dVllRjRWRUNyMEJwNVpBM2M4MmFOZHFpUllzQUVXMWVZVlM4d3lrcEtiQnc0VUtRUTFKOFBwOVlVU0l2MFl0bk1LcEpDS2crV2g1Mzc3MzNvaHlhWWxtV01OUFhyRmtUMXpsNnZWNG9MUzJGYTY2NUJnb0tDa1JxTFhuSkhkV1prWkVCczJiTmd1dXV1dzdKWVNJZjA3dDNiN2ppaWl2QTcvZERYVjBkL00vLy9JOVllaWozV1VzSklrN3VldlRBd3JDVkk1dUM4K2ZQMXhZc1dDQ3lSdE9hWGtxS0FMQnZzL1p1M2JyQks2KzhnbjYvSDF3dWw5aUMwK1Z5Z2E3clFvQmdNOE5xVkFIY29VTUg2TmV2bjFpU0ZncUZoQUNMUkNJaTdYOVQ1NmZyT3VpNkRqLysrS00yZGVwVUtDNHV0cDAzU2t2ZEFQYkZFTTZjT1JNbVRweUk4cnBxVGRQZ25udnV3Zjc5K3dNaXd0cTFhK0gxMTEvWG5NbzRtQTJ2WkdKTk43Q0FaQmlGbHBnemxFbEtTb0lWSzFaRWxVRnpkdlFhQ0FUd2dRY2VRSUJvSjRxYzJpdmVjNURuSE1rRGUvYlpaMk5SVVpGWVlTS2J0bVZsWlhqYWFhYzFLVzFWNzdlbWFUQnMyRENzcXFyQ1lEQW9jakxLNTBmemlIdjM3c1hKa3ljanRlK1paNTdCdXJvNmNWeS9mdjBjcjBWTGV0Sm56SmpScUtlZnplUzJBMnVHclJ4MTdpOFVDc0hNbVRPaHVycmFacktxZ2kweE1SRnV1dWttT1BQTU00WDJSQUhPY242L2VKRzFTTklxdTNmdkRoa1pHZUQzKzIzYkN5QWlVRUxXcGlCek96RXhVWnpIbWpWcnRIUFBQVmNFYzZ2TEVtbk9zbjM3OWpCejVreTQrZWFiOGZ6eno4ZkpreWREU2tvS2hNTmhHRGx5SlB6MjIyK092M1hxcndQbFFCeFJUT3VFaFdFcmh6TGF5QU51MWFwVjJtT1BQU2IyUGdFQVlTNVROaG9BZ0U2ZE9zSGt5Wk50ZXlOSEloRmJucittVUZlcGtQREMzMWVKSkNjbmkwdzF0R3FEWWdEakVZWjBic0ZnRUh3K256RDkxNjlmcjAyY09CRktTa3JFQ2hJQSs4YnlMcGNMTWpNellkcTBhYkI0OFdKd3U5MFFEQWJoMVZkZkZZbHI2YmQwSGkydGxYRUtyNk1IRm9hdEhFM2JuNHRRL2p4Ly9ueHQ1Y3FWdHBSWWNzSUMvSDA1MzVWWFhnbDMzbm1uYlFVSENhNTQ1Z3lkd21rQTltZWNsbytoN05ZQSs0VEV3SUVEbXl4ZnpveE4rNlZZbGdXR1ljRDc3Nyt2M1hqampkRFEwQ0FjU2JKamlPcnUwYU1IWkdSa0FBREFsMTkrQ2JObno5Ym9RVUdhSUpVcjUzOXNDWUVZYXlzQnB1M0J3dkFRZzcvbnRxUEI1eVNBMU1Fa0N4MDZuclE5S2ljY0RzTWYvL2hITFJBSWlNMlJDRG5EdGNmamdXblRwc0hnd1lOUkxyODU3YWZmeUpvVkNSWXlPVW5veUdYZmVlZWROdTFRRFl4V3R3QlE1L1J3M3c1NjJza25uNnpWMWRYWmNpVTZlZE4xWFllLy92V3ZVRlJVSlBwUWJUOEpiTG1kY2h1ZDJ0WVl0TjFCWTMybk9xdFVadzdUT21CaDJJcUkxN3NyYXpsLytNTWZvS3FxU2d3NDJWUW1nZUQxZXVHTk45NkFqaDA3QW9BOXMzVnoyMFMvcFIzd1pMT1pIQ3MwSjltdFd6YzQ5OXh6UmVnTnRZbENmR1N0Vms3blQyVlNrb2N0VzdiQWhBa1RoSkFEMkNlc1pDODZ6U08rOHNvcmNNWVpaeUQxQndscWVXOW05ZHprNzZpTmFxTGRXRFFXb3RSVS96YlhtODhjV2xnWXRoSlU3YUVwU0N2NzVwdHZ0RVdMRm9ud0dYbi9ZdG5rN051M0wweWZQaDFwSHhKS3U5OGNaS2NESWtKK2ZqNFVGUlhaQkdRNEhMYnRqL3o2NjYvRGNjY2RKOXBNNTZuck92ajlmaUg0NUVCdVRkT0VvNGY0NUpOUHRFbVRKc0dtVFpzQVlKOFdTT2RBNStwMnUySEFnQUV3ZCs1Y09PZWNjOURyOVlyNmFEc0FwM09TQThubDZ4RFBQaWpOMGU1WUUyemRzREE4RE1RajZHS1pVazM5eHJJc21EdDNycloxNjFZaGxHaVRkZExVS0JYWVZWZGRCVU9IRGtWS1RkV2NvR3UxUFlnSW16WnQwalp2M2l5RWlXRVlZazZQMnRLdVhUdTQ5OTU3TVQwOVBTcUhJTzA1UWtLTk5FWVNYcVRaRWN1V0xkUFdybDBMQVB0VGtnSFlWOWNZaGdGRGh3NkZ2Ly85NzNERkZWZUkzSVV1bDB0b3pkUiswanhsODUvS3BuTGo2UjgxTUQxZVdETnNYYkF3YkNYRUl6Q2RoSmRsV1ZCUlVRRWpSb3pRNnVycUFBQ0VxVXIvSjFKVFUrR0REejZJdWFOYnJIYkZha2ROVFEwc1diSWt5dXltZVVTYW03djIybXZoN3J2dlJqbnZJam1GeUxTbnVpaWNSamFmL1g0L1pHVmx3VHZ2dklNVEprd1EycUM4ZDdKaEdEWnZkbloyTml4WXNBQnV2dmxtbElXcS9KNDg5ZkltVnJRTktRbktsb0RuQ0JubWR6Uk5nenZ1dUtQUndOeXlzaklrajJoamMxQk9mOFRFaVJORjZpekUvUUhLaUNqUzdsdVdoUjkvL0RGU1RzSG00SlFKMnVWeWlTUVNjcHA5TmVzMkl1TENoUXZ4ckxQT1FxY04zV1V2dFZ5UHorZUQwYU5INDdKbHk4VDVCSU5CM0xCaEE0YkRZVnRBdGdvZCsvREREMlA3OXUwQndPNHNVWk83MHZ2bWhNczgvZlRUVVJ2SXExQSt4cFpNRU1Fd2JaSjRoR0ZwYVNtbXA2ZUw0K1hmTmdWcFVxbXBxZkQ0NDQvYnlsVVRyWnFtaVlGQUFPKy8vLzY0YkRReXRSdHJWM1oyTm56enpUYzJ3YWRDZ216YnRtMzR3Z3N2aUJ5SDZ2bVJJRXBPVG9aUm8wYmg0c1dMc2FTa0JBM0R3RWdrZ3FacDRzeVpNM0hJa0NINDdydnYyb1M5Q2dsbjB6VHg4ODgveHpGanh0Z1NObEQ4WWMrZVBlSHFxNjlHVlFqR0l4VC85cmUvTlNvTUxjdkMyYk5uUndsRDFoS1pZeFluWVNndno5dTdkMjlNWWRqWXdGR2RBams1T2ZETk45L1lFbzZTWUpEckxDd3N4UFBPTzY5SmdSaExXTkgvNUhUL1JVVkZ0bDN1WklFa1o1NHhUUk4xWGNlZE8zZmk0NDgvamlOR2pNQWhRNGJnT2VlY2czZmNjUWQrL2ZYWFdGOWZMNDZUaGZxZ1FZTnNLYnkrK09JTFd6MXEvOUo1MDROZ3hZb1YyTHQzYi9ENWZPRDMrK0d1dSs3Q2NEaU00WEFZNTh5Wmc3SVRKeDZlZXVxcFJqVlR5N0p3MXF4WkxBd1poZ1RHOWRkZjd6aG82WDFaV1JtbXBLUTRiazRVYnozMGV2bmxsMk00SEJacmVKMkVvcTdyK000NzcyQnFhaW9BMkxPdk5HZkRLSGxRanh3NUVyLysrbXRIVFNuV2VjdUNUdjVlTnJkMVhjZVBQdm9JaHd3WlloUGVIbzhIMnJkdkQwdVhMc1ZBSUdDclQwMFNLN2RKbmo2Z3o5OSsreTNtNWVXSmpEdXhoSldhb0hiZXZIbU8yN1BLMzkxeHh4MDJZZWlVZ1p4aGpoa21USmhnRytTVWpoNXhYLzYvMHRKU0VmWkN4Q3VVMU9OOFBoOU1uanc1U2dDb2dzQTBUWHp5eVNmeFlBZW4zTzRlUFhyQXJGbXpzTHE2MnFiWkVmSm5kVTVURmFLNnJ1UDY5ZXZ4cHB0dXdzNmRPd09BODd4cHIxNjk0TEhISGhOOVNlY3E3NWRNZlM5L3BqeUVuMzMyR1E0ZVBOaW1jY3J6bXJHbUJ6Uk5neWVmZkZMTXg4cklmZnluUC8wSjVZY05lZEFaNXBpQ3R2bTgrdXFyUlFZV2ViRFFnS21xcWtLQWZjdk5EalJGdnh4YzdQZjdZY0dDQlZIQ2tPcW10bGlXaGJmZmZqdktBNVZRbDc0NVFmWEpyMzYvSHdZTkdvU2ZmdnFwWTMweTZpYnkxRDg3ZHV6QUtWT21ZRTVPanEwZjVYT1YrOWpyOWNJSko1d0FCUVVGcUNLbjVaZjd3TElzbkRselp0UisxVTc5R211VjBOTlBQeDJWblZzVmlCZGNjSUhRYU9rY0R1VTJEQXpUYW5HNVhIRHp6VGMzNmx3b0xTMUZlWUFjeUp5U2FvWU5HalFJdDJ6WjRpZ0U2TDFwbXJoMTYxWWNPblFvcWltK21vdmNadnA5Ky9idFlmcjA2Ymh1M1Rvc0tDakFvcUlpckt5c3hKcWFHcXlwcWNIeThuSXNLeXZEelpzMzQ2Wk5tL0RWVjE5RmlvVnM3QnpsK3NpMGRidmRrSktTQWhNbVRNQjE2OWJodG0zYnNLeXNUQWpFVUNpRVpXVmxXRlJVaE11V0xjT3p6anBMT0ZSa0lSV1BSNW5hOGVLTEwwWTkyRlQ2OU9ramZ0ZWNuZmVZd3d2UDRoNWlLTUQ1aVNlZXdMdnV1Z3NBOW1kY29WZkRNS0N5c2hMeTh2STBTblFLQUNLaFFGTlFXZXJ4SG84SHJycnFLcHcvZno1a1ptYUNydXVPR29saEdMQnExU3E0NXBwcnRJcUtpcWkxenZIVTdmVjZSZklIZGI5aFdnK2NscFlHeHg5L1BIYnIxZzBTRXhNaE1URVJhbXBxb0tpb0NMWnMyYUxWMU5TQVlSZ2lQbEZlQnkxRGdkbDBuUHAvcWo4dkx3K0dEUnVHM2JwMUU5bDFkdXpZQWYvNjE3KzBIVHQyQU1EK1pZVDQrd29XaXBHVTEzZXJ5TisvL1BMTGVPMjExd3JocUM3ajAzVWRNak16dFZBb0ZIVXRZNVhQTUVjdExwY0wzbi8vZmFFRnFrNENSTVQ2K25vODlkUlREem9lamJKT0UzNi9IeDU2NkNFUmp5ZUhtNmdPbHUrLy85NDJNcHVUckNEVzkwMmRpenhQcDVycTZ2eWNhcmJUbko2c0VhdkpJR1JrQWF2R0c4cDFxdHBuckRsRGw4c0ZyNzMybXMxWkk1djcxdS83UHFzYU85WFBEaFRtbU9Pc3M4N0NqUnMzUnBsUDhoeGFPQnpHVjE5OUZRL1VtNndLSkRXYjlTZWZmQ0tFc1NxSTZmdHdPSXo1K2ZuWXFWT25aZ1Vla3pDUlBhMnFtYXNLb2xnQ1ZCWkdzYlk1VlIwK0xwY3I2aGhWY01vUENWa0FxMU1UOHVvWHAvMlk1ZmRwYVdudzNudnZPYzRGMCtmUFAvOGNHeFBRREhQTU1IVG9VRnl5WklsdG9EaUZrcGltaWNYRnhYanJyYmRpY3liWG5lYnBuT2pRb1FPOC8vNzdHQXFGYkN0VTFFRWNpVVJ3dzRZTk9IbnlaT3pkdS9jQm5YTmpxMlNjMnVzMFIrZjBHNmYwV3JKV3B6b25TQWlwMmE2ZGtQZDJsbDlqL2Q3ajhVQ1hMbDFzY1k3eUsvWG5xRkdqVUsyWE41RnFuZkFWYVFHOFhpOWNldW1sMkxselp3Z0dnNUNabVFudDJyV0QzTnhjR0RKa0NIVHIxZzBTRWhLaThnSEtxYTFvem1yMzd0M3czWGZmd1pZdFc2Q3lzaExxNnVyQXNpd29MQ3lFenovL1hKUG40dUtkYzZJMXdOMjZkWVBycnJzT0gzcm9JZkY3V3M5TGhNTmg4UHY5WUJnR3JGdTNEZ29LQ3FDOHZCenE2K3ZCTkUwb0xTMkYxMTU3VGF1dnI3Zk5DeDZ0MER5aW5CQ1crdnpNTTgvRXQ5NTZDenAzN2l6V1BNdlh0YWFtQmpwMDZLQlJ1aldlSDJTT2F0eHVOL2o5ZnRpMGFSTUdBZ0hoTVpiTlg5a1VkWHF2bXEyNnJvdmxaL1IrMGFKRjJLRkRCMUZuY3lFdnBzdmxndHpjWFBqM3YvOXQwd3hsVDdmcUVhVzJCWU5CTENzcnc3NTkreDZUODEya2NWSmZYbm5sbFdoWmxsanRJMS9UU0NTQzgrYk53MWllZWRZTVd4OGMrWG1RbUtZSlBwOFBObTdjQ0pXVmxVSnpJQzJBc3ArUVZpR25rSEs3M1NMUG5xdzVVRXdkcFp3eURBTzJiZHNtdkx4eTJVMXBaMlNtUmlJUjRmSGR0V3NYREI4K1hPdmR1emVNR3pjT1R6NzVaT2pRb1lQd0JIdTlYbEUyWlhhaG5JR2tJY3A3clJ6TitIdytzVTgxOVQrbFB4c3dZSUJ3N0pDbm5xNVpYVjBkZlBUUlI0NGFvZXlzWVcyeDljQ1BwNE5Fam5XVDAwSEprQWxGL3lQQktHOXNEckIvYmtvTndVQkU4SHE5VUY5ZmI2czMzb0hrMUNhNURIcE5TVW1CMU5SVU1hZEZvVEtHWVVBZ0VCQkNNUmdNeHRjNVJ3bjBFS01IZzJWWmtKaVlDSnMzYjhhdVhidmEwb0xoNzNrWkZ5OWVERGZmZkxQVzBOQndoRnZQTUljUk5SeUR2cFAvWWsyYU4yYnl5czRCSnkvemdaaXFicmNia3BLU291cVJ2YWpVZnZtOTdOUWh6K3l4WUNxcmpnL2ltbXV1Y2R3SDJ6Uk5MQzB0eFI0OWVyQXB6QnhieUtzZmlLYThoWEk4SEhHb01wcFF1M3crbjYxY05lR0EzQTVaZ011b251RmpRUmdDMk5jU2E1b0dIVHQyQkZxUExLOS9EZ2FEV0ZOVGcrZWRkeDZxMTVBRkkzTk1RSm9oYVUrcU5xY0tFQ2Nobys0aVI0TEhhZWMyS3VkQUI1amFIbFV3eHhMc3NiU2tveG0xbnp0MjdBai8rTWMvTUJLSlJLVXJLeTh2eDl0dnYxM01YY2ozZzlPMVlnSEpIRldvZzZVNW5rTjE1VU5qZGNobE5YY1F4ZExnMUhJYjAvVFVPTHhqUlNzRTJIK3U2ZW5wTUcvZVBLeXZyN2VaeFpabFlXVmxKZDUrKysyWW1wb2FsK0JycXI4WmhtRU9PMnB3dVByWjYvV0N6K2VEcjcvK1dvUkt5U0ZKcG1uaWlCRWpzREZObm1FWXBsV2phbXl5dHBhWW1BakRody9IdSs2NkMvZnMyV1BMazJoWkZwYVhsK1B5NWNzeE96czdha3JrWUtZeEdJWmhqaGl5RnRldlh6OTQ3TEhIY01tU0pWaFVWQlNWaDlFMFRWeXpaZzFPbURCQmJEUkZqalJPMnNvd1RKdUV6R0Zaa3hzN2RpeEdJaEdiU1d4WkZ1cTZqcXRYcjhiaHc0ZGpabVltQUVRN3RRRHNxMzJZdGdNL3hwaGpHZ3BjbHdQWWRWMkhpb29LcUt5c2hFZ2tBc1hGeGJCeTVVcFl1SENoVmxWVkpWYWFVQkM2MysrSGNEZ3NQTytSU01ReHp5TERNRXlid2VmelFaY3VYV0Q0OE9HWWw1Y25NbCtUK1JzcnAySmpXWFlZaG1IYURFNG1MU1ZnZFpvSGxJT3dDZG43ekRBTTA2WndFbWJ4eEdXcXYrRUVyZ3pETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUVjdlJ6ekhrTG92TFVEek5qdHFLazFTYTlsOUxGWTdXMHY3bUFPanFldDZwSGZBYSs3NFVJOTNhcnQ4RE8yb1NMczBIdW56UFJpT3VEQlVvYldlNmc1eGg0b2pMVXhiTXVlZFUxbHkrNTJ5YWpmMy9PSVpMQWRUWG5OcHF2NldhbCtzY2xyNy9YRTRCSk5USHgzdWNkd1N0QXBoNkhhN1lkU29VV2haRm5nOEh0QjFYV3k5S1dmK2FPN3VjdkhRMU0zV1ZINDZwNDJjNURMVnphRmlMZW8vMFBycC9CdmJXaUJXMitJWmFPcnYxTjhmYkpxcVNDUnlVTDl2S2pOTVUvdEtOelZZbmJadGpmVlozaStiUGpkVi84RzJyeW5pRllaTzJYdGluYXQ2aks3cjhORkhIMmswWHIxZXI5aGp1aTNSS2xKNFVSb2tnSDNaaFNPUkNMaGNMdEIxWFd4TFNiUzFYY2Rvc0RkWENCMHNWSWRsV1FkVnQ1Tm1LWDgrMUpwSFd6SzVuQVFKUGF4aW5VZFREL1BEbVN6V1NkZzVXUkZPRHdENW9VaUNVSjc2YWdzY2NVbEMyb1ZUcHpYV21VMlpMeTNadnNab3F2NURiWVlmYVRPL3RkTlMxeS9XY1FkYmZsT2E5YUhPaVhnd1V5Wk9xTlpjVytLSWE0YnlCVkNGWDJPZDJsSU9scVpvaVJ2a1lNcG9hV0hjM0xhb2c3VXhNL0ZRY0xEbmY2Z2ZWbzM5UDU1N3J5bkJjVGp2MzZibW5KMGdrMWdXZ3JUbmRsTlRBSzJOSTY0WmtnQWtqeFFsMHJRc0MwelRQT0thVDBzSW80TVpNQWQ2ZnZGcXpvZGE4MjJLMXE2NUhzekQ1SEJaTDRlU2VPNFB5dlN0d21ieUFSQ1BPVXcwRlFxZ2NxUnV4Q001RU9JeGZRNjBmUzN0VFdZT0xjMTVXQjNNdFNSbGhqVEV0bXd1dDJsY0xwZHRJbHFkZEZZMy9DSGNibmZVSmo3eGJPcnVWTC9hQnFleURtUXlQRmE3blhEYVlMNnhEYzNqUFVjMWM3TnNPcXNiSWpXbjM1eTgwZXBtOWVyeFR2VTRaWmh1YkF0UUtsL3VCL3EvN0oyUDkxelV0c3FmNVhMVWRzZnF4MWpuRUMveU5nV05lZnhidS9PUmFTYnlSWGNTUms0WG5JU1gvSm1POTNxOWpvSXRGazZDU2hVUU5QQU9SaWlxNTlkVVNJMzhPem9mcHpDZ3BsQUhOdUdVRHAvYTVIYTdoV0NLUjZpNDNXN3dlRHd4QmF2SDR4RjlLTmVwQ2pDcXora1lwM05RZjB2dktiS2h1V0ZEaloyRFhCYWREN1ZIdlM3cXRXb00rajNWcGRZcEMzd3FsL1oxWVk1UzFFSGdwRG1veDNnOEhuSGpPKzFiRWM5ZUZqVFlaVTFEclYrRmJ2UjRibmgxc01nM3VDd1FhVUE0YVdleDRoempIUkErbjgvMk82ZnRNWjMrUjc5Ui85UzJ5ZGRFN25OVjRLc2J2QU9BVFVpcS9lcjMreDNiSlF0dE9sNnRTdzNwYWd5eUtwejZSVVVXaENxeHRPUjRpS1V0QS9DZUxNY01UaHFUa3puU21GYWsvaS9XSmtEeElBZFprNmJnWk1iRlc3NTZIdXJnVnM5TlBRKzFiVTREdHpGaW1hc0pDUW0ydHFsN0JkTm5wOS9HMHFabFRZL2FwMnBiQUkwUGJuV2pKdlZCSWd0TnFrdWx1ZWErWEk5cXFaQzE0ZFNQY2grcEpuNjg5VHVaOTFSWFk5TVo2ditabzRSNE5SM1ZaS0NienVsR2JrN2RWQjc5M21uQXFScFV2TWptajFxZWs4YW5ta3J4bXRQeHRNUEpmSE9hNDJ0S0UybXNMYkVlSHZJMVU0K2xxUTIxVGZUcUpJemtoNmlxVVRzSjBsZzRUUWVRMXFxZXEydzlxQnRJT1puOHpkWG9ZZ20vV1AzTm0xZlphZk1UQjA3ZUs1ZkxaVnNhbFppWUNFT0dETUdCQXdkQ2NuSXlKQ1FrUUhWMU5mejg4OCt3Y2VOR3JieThIRHdlRDVpbUthTHBxYnltUEdJdWx3dDY5ZW9GZ1VBQWtwS1NJQmdNUmoybEE0RUFHSVlCWldWbFVRdmI0MEVPTzFJRDFIMCtIK1RrNUVDZlBuMndVNmRPQUFDd2QrOWUyTFp0bTFaVVZBU2hVRWljQzdWSDEzWEhzaHFEUFA3VXQ3MTY5WUxldlh0alJrWUdKQ1ltd3M2ZE82RzB0RlRidEdrVFdKWUZpQ2dHb1ZNRWdKUG5VbDdQbXBpWUtKWmxCb05CMFZiNnJScUJRTjhuSlNWQklCQ0FsSlFVc2ZvbkVvbEVIVS94Y2ZRN3VVMnBxYW1nNnpvZ29pZ2pscWRWdmRmOGZyL3RHcE9IdGJGRUJqNmZEMHpUdExXSjdzVjRJTUZ1R0FaNHZWN28xNjhmbm5qaWlkQ2pSdy93ZUR3UWlVUmcrL2J0c0hidFdxMm9xQWgwWFdkUDc5R01iRllSUFhyMGdEZmZmQk8zYjkrT3RiVzFXRlpXaHBGSUJCRVI2K3Jxc0s2dURvdUtpdkJmLy9vWFhuYlpaZUxPYTQ1bWtKR1JBYnQzNzhhYW1ob3NMaTdHdlh2M1ltVmxKZTdac3dmTHlzcXdwS1FFUzB0TGNmZnUzYmh6NTA1Y3ZudzVYbnJwcFJqdkpMYXFNZEQ1dFcvZkhsNTY2U1hjdVhNbjd0MjdGMnRxYXJDK3ZoN3I2K3V4dHJZV1MwdExzYkN3RUY5NjZTWE15c29DQU9lNXJhYVF6YTdNekV4WXVIQWhidDY4R2N2THk3R2lvZ0lEZ1FEVzFkVmhUVTBObHBXVjRhNWR1L0RsbDEvR3pwMDdDM1BYYVo1UTFjTGs2M2JsbFZkaWFXa3BGaFVWWVdGaG9iZzJUdWFmMms5ang0N0ZvcUlpM0xseko1YVVsT0NVS1ZPUTV2L1UrVW01UGZUNzNOeGNLQ3dzeEowN2QySlJVUkdTZ0c3c1dzbWE3UExseTdHd3NCQUxDd3R4OXV6WjZLVGx5dTlQTyswMC9PNjc3N0M4dkJ4WHJseUpKNTEwVXRSOTJCaXlCZkQvL3QvL3d4MDdkbUJ4Y1RFMk5EUmdLQlRDWURDSTRYQVlnOEVnRmhjWDQvcjE2L0dCQng3QTVzeUpNbTBFdXBGbGN5TTVPUm5HangrUG9WQUlUZE5FeTdMUXNpd01oOE5ZVVZHQmUvYnN3ZXJxYWpRTUF4RVJEY05BeTdJd1B6OGZjM0p5bWxWL1VsSVNJQ0thcGluS0lYUmRSL2wvaEdWWitNTVBQMkNQSGozaXFrUFdOQk1URStIUGYvNHoxdGJXb3E3cmFKb21CZ0lCM0xObkQrYm41Mk4rZmo3dTNic1hnOEVnbXFZcGhQK01HVE93ZmZ2MkI3Uy9iMlptSmt5ZE9oVXR5OEpRS0lTSWFLdHoyN1p0dUh2M2JneUh3NksvUTZFUS92ZC8vemY2Zkw1R2sxV281cjdiN1lZeFk4YUlmclFzQ3lzcks3RjM3OTdpdDNJNThubjA2OWNQcXF1cmJmMS96ejMzb05QNWtrQlNweSs2ZHUwS1ZDOGlZaXluRktGNmlYLysrV2R4elUzVHhFY2ZmUlF6TXpNQklIcGVGV0NmTUN3c0xNUkFJSUJidDI3RlFZTUdOVXNZZWp3ZXVPaWlpL0RYWDM4VjF3VVJNUktKWUhsNU9lN1pzd2RyYTJ1eHJxNU9mSStJV0ZaV2h1UEhqK2NnVVlranZoenZZQ0VUaDB5b3BLUWtlUERCQi9IT08rOFVac2JHalJ2aHd3OC9oSktTRWlnb0tJQklKQUlkT25TQTNyMTdRNzkrL2VEaWl5K0d0TFEwK1BUVFQ4VWk4M2hOV2JwaEtlUE92SG56d0RSTnNDeExtR1lwS1NuUXBVc1h1T3l5eThBd0RQQjRQREJvMENCWXVIQWhqaGt6UmdzRUFxQnBtaWpETUl5b1BJOHVsd3Y4ZmovTW5Ea1RwMDZkQ3JxdWc4ZmpnUTgvL0JCV3IxNE4zMy8vUGV6YXRVdlROQTI2ZCsrT2d3Y1BodUhEaDhQbzBhTWhGQXJCOU9uVDRZUVRUc0FwVTZabzVlWGxva3k1blFBUTliNWp4NDR3ZCs1Y0hEdDJyT2pqdDk1NkM5YXRXd2RyMXF5QjR1SmlUZE0wNk5hdEd3NFpNZ1RPT09NTUdEVnFGUGo5ZnBnL2Z6NE1IRGdRSDN6d1FhMnNyRXlZaUxLWkt5L2hRa1RSTGxselRFMU5oUmRmZkJHdnUrNDZyYVNreE5ZLzFLYk16RXg0OWRWWE1UMDlYZHdUOGh5bmJHSnJtaWF1TTlWUDk0cXFzZEoxa2Y4dnY2ZnBCL3FPekdxNmx0T21UWU9FaEFTY08zZXVWbEpTWXF1VEhoSTBMU0RYcXk1em8xYzVJNHpMNVlJcnJyZ0MvL2EzdjBHblRwMUExM1dvcjYrSC8vdS8vNFBmZnZzTnRtM2JKcVp2ZXZmdURUMTc5b1FSSTBaQVhsNGVGQllXUWxGUmtlMWVsdTk1RHBwdWc2ZzM3dzAzM0lDVmxaWGk2VHh2M2p3OC92ampiWnFCUE1HZGxwWUdJMGVPeERGanhtQlNVaElBTk0vTGxwS1NBcVpwaXFkdVJrWUdBT3lmdFBmNy9aQ1FrQUFkT25TQW9VT0hZaUFRRUUvbjR1SmlQUHZzczhYVFdZMnRVNWsxYXhaYWxpVzB6SWtUSjJLSERoMml2T0gwbDVtWkNRODg4QUEyTkRRZ0ltSTRITVpISG5uRVVSdHdNbVVCQUtaT25ZcUJRQUF0eThLYW1ocWNNV01HZHVqUUllbzNKSGl5c3JMZ3BwdHVFbHA1SUJEQW1UTm5vcXlCT1ptZDh1Yy8vdkdQUXJzekRBTU53MERUTklXV3B6cTZORTJERjE1NFFSeEw1MnFhSms2Yk5pMUswMnBNNCtyV3JSdWdoSk5XNi9TZVlpdlhyVnNuN2oyNlZvRkFBSjk5OWxsTVNVa0JnR2d6ZWNlT0hZaUltSitmajBPR0RCSHRqYVhGeTFwc1ZWV1YwSUtycXFwdzhPREJvaDdaSys5eXVTQWhJUUZPUFBGRWVQVFJSN0ZuejU3Q29STXJIT3Rnblc3TVlVWWVaRGs1T2ZEVlYxK0pBZkgyMjI5alVsSlNWREF0UUh5ZTQzaUVZbHBhR3BCSlpab21wcWFtUnYxV0R0Vlp0R2lSTU51cnFxcHNwb284U05TNisvVHBBNy84OG9zdzMvNzYxNytpR3J5ci9wWmVQL2pnQTJFK05UUTBvT3g5VmIzT2hLWnBrSktTQWhVVkZVSzR2UDMyMjZqK1RnMVFkN3Zka0ppWUNETm56aFRUQkpzMmJjTCsvZnMzZW42eGhLRnM5cG1taVgzNjlMSDl6dVB4d1BqeDQ3R3lzaElOdzhCSUpDS0VQeUxpZmZmZGQwaUZvZnpuOC9sZ3c0WU5HQWdFTUJnTTR2YnQyOUd5TE5FUHp6enpUTlFEOTl4eno4WGk0bUswTEFzM2I5Nk1nd2NQUnZrK3BUNVYrOW5uODhHU0pVc3dFb21nWVJoWVgxOXZtd2VVNTBHOVhxLzRMZVVKamNXeExBVGJmS0NSYk9ha3BLVEFhYWVkQmk2WEMwS2hFQ3hac2dUQzRiQzR3T0Z3V0FTOXV0MXVTRXBLZ29TRUJNakt5b0xrNUdUSXlzcUN0TFEwRVNZVGo1bEFwaElBUURnY0ZnTmVEck9oLzE5NDRZVjQzbm5uQ1ZPdHBxWUdkdTdjS2NxUzg4Q3BkWjl6emptWWw1Y0htcVpCZm40K3JGeTVVaVN6b0hiSWJaTDc1cXFycnRLb1BRa0pDZkNmLy9tZktIdmVBY0NXRklQTXZxRkRoMkpXVnBid3h0NXh4eDJhYkJiS3BpME5NdE0wUWRkMVdMWnNHZXpac3djUUVYcjA2QUVEQnc1RThvaXI3VzJNSDMvOEVhWk1tU0s4L0JzMmJNRGpqejllUE5neU1qTGd4aHR2aE9Ua1pIQzVYUERVVTAvQnh4OS9MSzVIYytvNkVNZzh4dDg5ejZGUVNGZ2V0OXh5Qzd6NjZxdkNtM3pycmJmQ3M4OCthOVBtdytHd01MWHBuR1N2Ti9XcG5MaUVndUI3OSs0dHdvcW1UNTh1VEhTZnp3ZUlLTXhyT29idTdhU2tKTWpNeklSMjdkcEJjbkt5N1h5T1ZVRUljQlRNR1pLdytWMlRRWUI5RnpRU2ljQy8vLzF2VFJZV2RGTysrZWFiNlBQNVJMWU5DaUIydTkxUVdsb0tEenp3Z0xacjE2NjQ2ZytId3lLc0lURXhFV2JObW9VMEQwUmFoZHZ0aG5idDJzSGd3WU9oYTlldUl1U2lvYUVCZnYzMVY0M2FoMHFJQjMwUHNNK0prWktTQXBabFFYRnhNV3pldkZrSUpnQjdLaWoxbk1QaE1HemJ0ZzM2OWVzSExwY0x6anZ2UEhqenpUZkZmSlZhRjUzUGlCRWp3TElzOFBsOFVGUlVCRFRuUllLSkhnUXVsMHVFb25pOVhqQU1BMzc4OFVldHRMUVV1M2J0Q242L0g5TFMwbXkvd3pqRFJyS3lzdUMxMTE3VFRqenhSSncwYVJLNDNXNllNMmNPWG4vOTlWbzRISVo1OCtiaDJXZWZEWlpsd1U4Ly9RUXZ2dmlpOXRoamp5SCtuazNsY0F4dUVsaCt2eC84ZnI4dFljSE5OOStzVlZaVzR0U3BVeUVTaWNDRUNSUEE1L1BoZmZmZHB4VVZGVUZDUW9JSXJTRkJKNGZoeUE4dHVsNlJTQVQ2OXUwcjdnZVh5d1g1K2ZtaVBTUVVYUzRYWEhubGxYajExVmVEMStzRlJCUjFVU2paM1hmZnJXM2N1Rkg4TmxaNDJyRkFteGVHOG9SdlltS2l1R0hvQnZYNWZCQ0pSR3dYOXB4enpvSE9uVHVEcnV2aUpnSFlkL1A5OXR0dmtKS1NJcjV2eW9sQ1QzUFN2RzY2NlNZaGhGUm5BRUZDY3N5WU1WcHRiUzBBMkRVTUVvcjBXVzRMeFFsR0loRnhRMVBiU2R1UUoveEpLRk84R1QwRVpJRWtPMDFJbzlBMERkTFQwMjBUK2pTWUlwR0lUWkJTV1NRVXFRMDBjSDArWDVTamh2cXNxZjZ0cmEyRit2cDZtRHQzcmpaczJEQWNNR0FBWEhUUlJUQisvSGlzcXFxQzhlUEhpL2FlZnZycEdtbE5jcDhlU3VnY2FQNHRIQTdiQXVVdHk0Sjc3NzFYeThqSXdCdHV1QUVNdzRDcnI3NGFFQkd2dSs0NmphNFZQYWdwNUlydUF5cUQ3Z242VEhHSWRJL1I5M0k2TFVTRWJ0MjZ3V1dYWFNiYXF6ck9aczJhaFlpb0Fld1hnTWNxYmQ1TWx1Zk15c3ZMTmZJMGhzTmh1T1NTUzlCcGo0MWZmdmtGVnE5ZURkOTg4dzBzWDc0Y3RtL2ZicnV4M0c0MzZyb2VsemRaenVObUdBWlVWbGJDbmoxN1lQZnUzVkJXVmdiRnhjVkFaVm1XQllXRmhiQnc0VUxvM3IyN3RtM2JOc2U1UGhWZDE0VzViMWtXOU9qUkEvcjI3WXR5KzBpSXlZS1FTRWhJZ083ZHU0UFg2d1hUTk9ITEw3KzBwV3BYaFFlVnNXclZLbEZ1VmxZV1pHZG5DNjJEaEN3ZEQ3QS9kTVRqOGNEQWdRT3hVNmRPNFBQNVFOZDFLQzh2anpxL2VQcVhoUEQyN2R2aHlTZWZGSjdrbVRObndxSkZpMERUTktpcnE0UExMcnNNUXFHUTBMRFVQNW1XSFBDa0JacW1DYUZRU016dkJRSUIwWGNBQUpNbVRkTGVmdnR0Y2I5T25EZ1JYbi85ZGN6SnlSSFhsc3hadVgxeXNEeVp5eTZYQzJwcWFpQVVDZ252OHRDaFF3RUFoQWNhWU45MUxTMHRoVldyVnNFWFgzd0JxMWF0Z2kxYnRvaHJweTRxb0xMcC9iRXNHTnNrOHMyZWs1TUQzMzc3TFpKM2Qvbnk1ZGkxYTllb1kybWVoTXlhdSsrK1cweVk1K2ZuWTY5ZXZlSmVPNXlTa2lMaURFM1R4REZqeHVEbzBhTng1TWlSZU9HRkYrSUZGMXlBNzc3N3JuQjhyRm16Qm84NzdyaW9DWEZxbi9wSzcwODk5VlRjdW5XcmNDYk1uVHNYNDAwazhmVFRUNHY2YTJ0ck1UVTFOZTZBOG5BNExCd0FjK2JNRVVIRWFueWQ3TFJKU1VtQnVYUG5ZakFZUk11eThKZGZmc0dUVGpvSlplMDFYbS95NnRXclVlNm52L3psTDhKVGk0Z1lDb1Z3M3J4NVNOYzBOVFVWM243N2JmSDcrKysvUDhycDB4Z0g2azBHMkhjL2taTXJHQXppUlJkZGhBRDdIV01KQ1FudzlOTlBJOTB2dXE3ajExOS9qWFYxZFdpYUp1N1lzUVBQTys4OGxQdFNyb2VtWEFEMmFmTkxsaXdSbnZiUzBsTE15OHVMT2grZnp3ZUppWWxpVHZlbW0yNFM4YW1JaU1PSEQwZTUzTWJPajJrRDBFWHplRHp3d0FNUENBOWJLQlRDVjE1NUJaM1NKZEVONm5hNzRiNzc3a1BFZmVFSlc3WnN3Zjc5KzhkZGQzSnlNdEJ2RVJIVDB0S2k2anJoaEJQZzExOS9GZDdPdi8vOTcrSTQ5VmhxazNwdW1xYkJQLzd4RDR4RUlxanJPZ1lDQWJ6bW1tdEVVSEFzN3JyckxsdkE3YTIzM3Rya2IyUWVmdmhoTWVCcWEydHgwcVJKWWw1V2JxdThVbWI4K1BGWVhWMk5wbWxpTUJqRVo1OTkxaVpVMU1HdWxqZDY5R2doN0w3ODhrdWJBTlkwRFo1NTVobmJ3NHVDbWowZUQvaDhQbmpublhkc1huZjVmTlIrVmg5SWVYbDVRaGhhbGhVbERPVzJxbmc4SHZqcHA1OFFFYkcrdmg1SGpCZ1IxZGVabVpudy9QUFBvNjdySXFxQTJycHQyelk4Ly96em83emZUaGwzdkY0djVPYm1pbnZQTUF3c0tDZ1FEMW9aT1NHRWt6QnM2cnlZTmdiZEFBa0pDZkRwcDUrS0FZeTRMOXArMUtoUjJLZFBIK2pTcFF2MDd0MGJ1bmJ0Q3NjZGR4eWNmdnJwU0Rjd0RTNDVCcXNwTWpJeWdHN3NZREFvWXZBSWV1cE9uanhaaFB6b3VvNjMzSEpMbEFZQUVIdHhQZzNLclZ1M29tRVlHQTZIRVJIeG5YZmV3VUdEQm1IMzd0MGhPenNic3JPem9XdlhydEMzYjE5WXVIQ2gwRmlEd1NBdVhyd1lPM2JzYUd0YlUrVGw1Y0hISDMrTWhtRUlUZStGRjE3QVBuMzZRTGR1M1NBckt3czZkdXdJWGJ0MmhVR0RCdUc3Nzc1cjAzeldybDNyYUd1cFFsUm16Smd4b3A4Kysrd3ptL0QxZXIzUXUzZHYrT0tMTDNEejVzM1lybDA3MGNma2tYM25uWGVFMWtoeGhuTFlpWk1RSmtIYnNXTkhRRVJ4cmVJUmh2TEQrS2VmZnNKSUpJS0JRQUF2dnZoaXNmcEYvazFxYWlxOC8vNzc0djRrU2twSzhPS0xMMGFLV1hTcVQwMklNWG55WkF5SHcyTDFVME5EQXo3ODhNUFlwMDhmNk5XckYzVHAwZ1c2ZCs4T09UazVjUGJaWitOYmI3MGw2alZORTRjTkd5WXNqTWFtRnBnMmdqcXcycmR2RHkrLy9ES0d3MkcwTEV2RXFEVTBOR0JoWVNGdTNyelp0azZaYm83UzBsSjgrT0dIeFFDTGg5L0RHTVRnVFU1T2p2b3RQWm1mZSs0NVJFUng4OTU0NDQwb2EzN3FPY2thSTUxamNuSXkvTy8vL2krV2xwYUtRV1FZQm03ZHVoVlhybHlKWDMzMUZlN1lzVU1JSTlNMHNhYW1CaDk2NkNHYkdkWFlFak9WL3YzN3cvejU4MjFML0F6RHdQejhmRnkyYkJtdVhyMGE4L1B6UlV4Z09Cekc3ZHUzNDNQUFBTY0VvV3FHT1FrVU90ZlJvMGVMNjdKMDZWS2s4QkRDNC9GQVZsWVdkT25TUlh3bS9INC92UDMyMitLYVB2VFFRMUhDbUV4R2VYMDR6Y3ZsNU9RSVlXaFpGanIxazJwS3l0ZUlIcXpoY0JqUFAvOThsSStWajB0UFQ0ZEZpeGFKZXhRUnNiQ3dFQys4OE1LWWZlWlVmMUpTRXR4MjIyMWlDYWE4N0xPMHRCUzNiOStPaFlXRkdBcUZ4UDhvZ0g3cDBxV1ltNXNiOCtIQXRER2NCclRmNzRmVTFGUVlQWG8wcmxpeEl1b0pMR3N1aVB1Q2dxZE5tNFpEaHc0VlFiSHhrcEdSWVF1NlZqVkRtWVNFQk50OFZtVmxKVjV6elRWaXdNZ3JZOVNnYlptMHREUVlObXdZenBrekIzZnYzaDExYnRTV25UdDM0b01QUG9qRGh3OUhPWjZzdWFtYlNCQ05HREVDNTgrZmoyVmxaVGJ0Z2dZWTRqNVQ3Lzc3NzhkVFRqbEZKS05RcHlib3MxT3VRcmZiRFdQSGpoV0M5ZjMzM3hjYXRPeWdVZHRHeHlRbEpjRjc3NzBuVnFEY2Q5OTlLQ2VvY0xwZlpJR1htNXNMdFA0NkhtRkkrSHcrOFBsOHNIYnRXcUdoWFhMSkpXS2VWRTB5NGZWNklTc3JDMTUrK1dWeEg1YVVsT0RsbDE5dVcyVkQ5VGs5TEtudkVoSVNZTWlRSWZqY2M4OWhUVTJORU9UeSttNTZXTmZWMWVHQ0JRdnd2UFBPaTFxOXBKN2pzYVlkdHZtemxjTTE1SFdiQVB0REJUUk5nNkZEaCtKcHA1MEdQWHIwZ05yYVdpZ3VMb1pmZnZrRmZ2NzVaeTBTaWRnOHNmSjY0SGhTZUkwYk53NnJxNnNoSVNFQi92blBmMnB5T2lieTRGSTVmcjhmc3JPelFkZDE4UHY5RUF3R29heXNUSGdrQWZiZDNPU1pwTy9VSGNoa0V6czVPUm02ZCsrTzNidDNCOHV5WVB2MjdkcXVYYnNnRUFpSTQ5WFlQdGxyMk5UNXFXRXhMcGNMMHRQVElTOHZEN3QyN1FxUlNBU0tpb3EwclZ1M2dtRVk0amlLQVkxVmg5d20rWDIzYnQzZzlOTlBSMTNYWWRldVhmREREejlvVkQ4ZHA0YnB5RzA5OWRSVHNVdVhMdUJ5dVdETGxpM3d5eSsvYUdxOUZLSWtwODJpT2NkTEw3MFU2K3ZySVRVMUZkNTg4MDBONC9TcSt2MSs2Tnk1TXlRa0pFQTRISWFLaWdxb3JhMk51ZlliWUovd1B1T01NekE1T1JsQ29SQjgvLzMzV20xdHJRaGZvcnJsT01QRyt0SHI5VUt2WHIzZzlOTlBGNDdBb3FJaTJMcDFLL3o2NjY5YWFXbHAxQ2J2TkU3a2U1QnBvelNsNlRpWlpoU1BGc3RaRWE4M1dhMjdxZldrcENVNHJhK045WlNXVFVRNWppNVdPK1NKZC9KYXk5clZnU1N4SmRUVVQ2cUoxVmpiVkEweFZ2dGwxRzBhbk9xVkhRU3gwb2JGeWxyalZKNjZpcWd4bk14S3ArdW9KbTJWUGNYMFA5a3lvTitvWm5aajV5WDN2MnhpcSthMldrNnNlNVZwZ3poTk1NdmVZa0tkZTZMdkdqT2Y0aVZXbXZ0WVhtS24vOHMzcm14YUFkalhWYXZ0ZFVvTkZVdmdPSGtvbTBJZVBQS0R3bW0vRXZtOTB6d2gvVlplSFNJUFl2azlIU3YzclNvd2lLYXVYNnk1c0ZnUHMxaGx4cXJYcVM1NndNbk9IN1Y5alRuUm5CNlFzb05EWGFzc2x5dTNRL1pBTzVXbkVtdXVrbW5seURlTStzU1RKNnpWNzVzYUtNMFJoRTQzbU5NTlJkODVEVGluOWpqVm9aWVhTek9nMzhqOTR5Um9ta0oxRmpUMjN1bWhJczhQT3JVLzFweWMrcDBxU0p5Y1MzSzhvM3ArVHYzbmRLL0UrbjBzMVA2UHBYVTdoWGM1WFF1bnBDSk5PYnZVKzBydGw4WTBkcWY3c3pGUFA4TXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13VEN2aS93TXZ0T1Q3WHl4MmZBQUFBQUJKUlU1RXJrSmdnZz09IiBhbHQ9IlImYW1wO0ogR3Jvb21pbmciIGNsYXNzPSJsb2dvLWltZyI+CiAgPC9kaXY+CgogIDxidXR0b24gY2xhc3M9Im9wdCIgaWQ9ImJvb2tCdG4iPgogICAgPGRpdiBjbGFzcz0ib3B0LWljb24iPjxzdmcgd2lkdGg9IjM2IiBoZWlnaHQ9IjM2IiB2aWV3Qm94PSIwIDAgMjQgMjQiIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyI+PHJlY3Qgd2lkdGg9IjI0IiBoZWlnaHQ9IjI0IiByeD0iNiIgZmlsbD0icmdiYSgyNTUsMjU1LDI1NSwuMDgpIi8+PHJlY3QgeD0iNSIgeT0iNyIgd2lkdGg9IjE0IiBoZWlnaHQ9IjEzIiByeD0iMS41IiBmaWxsPSJub25lIiBzdHJva2U9InJnYmEoMjU1LDI1NSwyNTUsLjU1KSIgc3Ryb2tlLXdpZHRoPSIxLjUiLz48cGF0aCBkPSJNOCA1djRNMTYgNXY0TTUgMTFoMTQiIHN0cm9rZT0icmdiYSgyNTUsMjU1LDI1NSwuNTUpIiBzdHJva2Utd2lkdGg9IjEuNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+PGNpcmNsZSBjeD0iOC41IiBjeT0iMTUiIHI9IjEiIGZpbGw9InJnYmEoMjU1LDI1NSwyNTUsLjU1KSIvPjxjaXJjbGUgY3g9IjEyIiBjeT0iMTUiIHI9IjEiIGZpbGw9InJnYmEoMjU1LDI1NSwyNTUsLjU1KSIvPjxjaXJjbGUgY3g9IjE1LjUiIGN5PSIxNSIgcj0iMSIgZmlsbD0icmdiYSgyNTUsMjU1LDI1NSwuNTUpIi8+PC9zdmc+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJvcHQtdGV4dCI+PGRpdiBjbGFzcz0ib3B0LXRpdGxlIiBkYXRhLWkxOG49ImJvb2tfb25saW5lIj5Cb29rIE9ubGluZTwvZGl2PjxkaXYgY2xhc3M9Im9wdC1oYW5kbGUiIGRhdGEtaTE4bj0iYm9va19mbG93Ij7Qn9C+0YDQvtC00LAg4oaSINCj0YHQu9GD0LPQsCDihpIg0JzQsNGB0YLQtdGAIOKGkiDQktGA0LXQvNGPPC9kaXY+PC9kaXY+CiAgICA8c3BhbiBjbGFzcz0ib3B0LWFycm93Ij7ihpI8L3NwYW4+CiAgPC9idXR0b24+CiAgPGRpdiBjbGFzcz0iZGl2aWRlciI+PHNwYW4gZGF0YS1pMThuPSJvcl9jb250YWN0Ij5vciBjb250YWN0IHVzPC9zcGFuPjwvZGl2PgogIDxhIGhyZWY9Imh0dHBzOi8vd3d3Lmluc3RhZ3JhbS5jb20vcmpfZ3Jvb21pbmc/aWdzaD1NV3htZEhOcWNYRmthbk52YlE9PSIgdGFyZ2V0PSJfYmxhbmsiIGNsYXNzPSJvcHQiPgogICAgPGRpdiBjbGFzcz0ib3B0LWljb24iPjxzdmcgd2lkdGg9IjM2IiBoZWlnaHQ9IjM2IiB2aWV3Qm94PSIwIDAgMjQgMjQiIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyI+PGRlZnM+PGxpbmVhckdyYWRpZW50IGlkPSJpZyIgeDE9IjAlIiB5MT0iMTAwJSIgeDI9IjEwMCUiIHkyPSIwJSI+PHN0b3Agb2Zmc2V0PSIwJSIgc3RvcC1jb2xvcj0iI2YwOTQzMyIvPjxzdG9wIG9mZnNldD0iNTAlIiBzdG9wLWNvbG9yPSIjZGMyNzQzIi8+PHN0b3Agb2Zmc2V0PSIxMDAlIiBzdG9wLWNvbG9yPSIjYmMxODg4Ii8+PC9saW5lYXJHcmFkaWVudD48L2RlZnM+PHJlY3Qgd2lkdGg9IjI0IiBoZWlnaHQ9IjI0IiByeD0iNiIgZmlsbD0idXJsKCNpZykiLz48cmVjdCB4PSI2IiB5PSI2IiB3aWR0aD0iMTIiIGhlaWdodD0iMTIiIHJ4PSIzIiBmaWxsPSJub25lIiBzdHJva2U9IndoaXRlIiBzdHJva2Utd2lkdGg9IjEuNSIvPjxjaXJjbGUgY3g9IjEyIiBjeT0iMTIiIHI9IjMiIGZpbGw9Im5vbmUiIHN0cm9rZT0id2hpdGUiIHN0cm9rZS13aWR0aD0iMS41Ii8+PGNpcmNsZSBjeD0iMTYuNSIgY3k9IjcuNSIgcj0iMSIgZmlsbD0id2hpdGUiLz48L3N2Zz48L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im9wdC10ZXh0Ij48ZGl2IGNsYXNzPSJvcHQtdGl0bGUiPkluc3RhZ3JhbTwvZGl2PjxkaXYgY2xhc3M9Im9wdC1oYW5kbGUiPkByal9ncm9vbWluZzwvZGl2PjwvZGl2PgogICAgPHNwYW4gY2xhc3M9Im9wdC1hcnJvdyI+4oaSPC9zcGFuPgogIDwvYT4KICA8YSBocmVmPSJodHRwczovL3dhLm1lLzM3MjU4NzM1NDU2IiB0YXJnZXQ9Il9ibGFuayIgY2xhc3M9Im9wdCI+CiAgICA8ZGl2IGNsYXNzPSJvcHQtaWNvbiI+PHN2ZyB3aWR0aD0iMzYiIGhlaWdodD0iMzYiIHZpZXdCb3g9IjAgMCAyNCAyNCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48Y2lyY2xlIGN4PSIxMiIgY3k9IjEyIiByPSIxMiIgZmlsbD0iIzI1RDM2NiIvPjxwYXRoIGZpbGw9IndoaXRlIiBkPSJNMTcuNDcyIDE0LjM4MmMtLjI5Ny0uMTQ5LTEuNzU4LS44NjctMi4wMy0uOTY3LS4yNzMtLjA5OS0uNDcxLS4xNDgtLjY3LjE1LS4xOTcuMjk3LS43NjcuOTY2LS45NCAxLjE2NC0uMTczLjE5OS0uMzQ3LjIyMy0uNjQ0LjA3NS0uMjk3LS4xNS0xLjI1NS0uNDYzLTIuMzktMS40NzUtLjg4My0uNzg4LTEuNDgtMS43NjEtMS42NTMtMi4wNTktLjE3My0uMjk3LS4wMTgtLjQ1OC4xMy0uNjA2LjEzNC0uMTMzLjI5OC0uMzQ3LjQ0Ni0uNTIuMTQ5LS4xNzQuMTk4LS4yOTguMjk4LS40OTcuMDk5LS4xOTguMDUtLjM3MS0uMDI1LS41Mi0uMDc1LS4xNDktLjY2OS0xLjYxMi0uOTE2LTIuMjA3LS4yNDItLjU3OS0uNDg3LS41LS42NjktLjUxLS4xNzMtLjAwOC0uMzcxLS4wMS0uNTctLjAxLS4xOTggMC0uNTIuMDc0LS43OTIuMzcyLS4yNzIuMjk3LTEuMDQgMS4wMTYtMS4wNCAyLjQ3OSAwIDEuNDYyIDEuMDY1IDIuODc1IDEuMjEzIDMuMDc0LjE0OS4xOTggMi4wOTYgMy4yIDUuMDc3IDQuNDg3LjcwOS4zMDYgMS4yNjIuNDg5IDEuNjk0LjYyNS43MTIuMjI3IDEuMzYuMTk1IDEuODcxLjExOC41NzEtLjA4NSAxLjc1OC0uNzE5IDIuMDA2LTEuNDEzLjI0OC0uNjk0LjI0OC0xLjI4OS4xNzMtMS40MTMtLjA3NC0uMTI0LS4yNzItLjE5OC0uNTctLjM0NyIvPjwvc3ZnPjwvZGl2PgogICAgPGRpdiBjbGFzcz0ib3B0LXRleHQiPjxkaXYgY2xhc3M9Im9wdC10aXRsZSI+V2hhdHNBcHA8L2Rpdj48ZGl2IGNsYXNzPSJvcHQtaGFuZGxlIj4rMzcyIDU4NyAzNTQ1NjwvZGl2PjwvZGl2PgogICAgPHNwYW4gY2xhc3M9Im9wdC1hcnJvdyI+4oaSPC9zcGFuPgogIDwvYT4KICA8YSBocmVmPSJodHRwczovL3d3dy5mYWNlYm9vay5jb20vc2hhcmUvMUVMUDZLQzZyVi8/bWliZXh0aWQ9d3dYSWZyIiB0YXJnZXQ9Il9ibGFuayIgY2xhc3M9Im9wdCI+CiAgICA8ZGl2IGNsYXNzPSJvcHQtaWNvbiI+PHN2ZyB3aWR0aD0iMzYiIGhlaWdodD0iMzYiIHZpZXdCb3g9IjAgMCAyNCAyNCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48Y2lyY2xlIGN4PSIxMiIgY3k9IjEyIiByPSIxMiIgZmlsbD0iIzE4NzdGMiIvPjxwYXRoIGZpbGw9IndoaXRlIiBkPSJNMTMgMTAuNWgybC41LTIuNUgxM1Y2LjVjMC0uNy4yLTEuNSAxLjUtMS41SDE2VjNzLTEtLjItMi0uMmMtMi4xIDAtMy41IDEuMy0zLjUgMy41VjhIOHYyLjVoMi41VjE4SDEzdi03LjV6Ii8+PC9zdmc+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJvcHQtdGV4dCI+PGRpdiBjbGFzcz0ib3B0LXRpdGxlIj5GYWNlYm9vazwvZGl2PjxkaXYgY2xhc3M9Im9wdC1oYW5kbGUiPlImYW1wO0ogR3Jvb21pbmc8L2Rpdj48L2Rpdj4KICAgIDxzcGFuIGNsYXNzPSJvcHQtYXJyb3ciPuKGkjwvc3Bhbj4KICA8L2E+CiAgPGJ1dHRvbiBjbGFzcz0ib3B0IiBvbmNsaWNrPSJ3aW5kb3cubG9jYXRpb24uaHJlZj0ndGVsOiszNzI1ODczNTQ1NiciPgogICAgPGRpdiBjbGFzcz0ib3B0LWljb24iPjxzdmcgd2lkdGg9IjMyIiBoZWlnaHQ9IjMyIiB2aWV3Qm94PSIwIDAgMjQgMjQiIGZpbGw9Im5vbmUiIHN0cm9rZT0icmdiYSgyNTUsMjU1LDI1NSwuNDUpIiBzdHJva2Utd2lkdGg9IjEuNiI+PHBhdGggZD0iTTIyIDE2LjkydjNhMiAyIDAgMDEtMi4xOCAyIDE5Ljc5IDE5Ljc5IDAgMDEtOC42My0zLjA3QTE5LjUgMTkuNSAwIDAxMy4wNyA5LjgyYTE5Ljc5IDE5Ljc5IDAgMDEtMy4wNy04LjY3QTIgMiAwIDAxMiAxaDNhMiAyIDAgMDEyIDEuNzJjLjEyNy45Ni4zNjEgMS45MDMuNyAyLjgxYTIgMiAwIDAxLS40NSAyLjExTDYuOTEgOC45MWExNiAxNiAwIDAwNiA2bDEuMjctMS4yN2EyIDIgMCAwMTIuMTEtLjQ1Yy45MDcuMzM5IDEuODUuNTczIDIuODEuN0EyIDIgMCAwMTIyIDE2LjkyeiIvPjwvc3ZnPjwvZGl2PgogICAgPGRpdiBjbGFzcz0ib3B0LXRleHQiPjxkaXYgY2xhc3M9Im9wdC10aXRsZSIgZGF0YS1pMThuPSJjYWxsX3VzIj5DYWxsIFVzPC9kaXY+PGRpdiBjbGFzcz0ib3B0LWhhbmRsZSI+KzM3MiA1ODcgMzU0NTY8L2Rpdj48L2Rpdj4KICAgIDxzcGFuIGNsYXNzPSJvcHQtYXJyb3ciPuKGkjwvc3Bhbj4KICA8L2J1dHRvbj4KICA8ZGl2IGNsYXNzPSJob21lLWZvb3QiPgogICAgPHNwYW4+VGFsbGlubjwvc3Bhbj48ZGl2IGNsYXNzPSJmZG90Ij48L2Rpdj48c3Bhbj5Fc3RvbmlhPC9zcGFuPjxkaXYgY2xhc3M9ImZkb3QiPjwvZGl2PjxzcGFuPkFsbHZlZWxhZXZhIDQ8L3NwYW4+CiAgPC9kaXY+CjwvZGl2Pgo8L2Rpdj4KCjwhLS0gQk9PS0lORyAtLT4KPGRpdiBjbGFzcz0ic2NyZWVuIiBpZD0iYm9va1NjcmVlbiI+CjxkaXYgY2xhc3M9ImNvbiI+CiAgPGJ1dHRvbiBjbGFzcz0iYmFjay1idG4iIGlkPSJiYWNrQnRuIiBkYXRhLWkxOG49ImJhY2siPuKGkCDQndCw0LfQsNC0PC9idXR0b24+CiAgPGRpdiBjbGFzcz0ibG9nby1yaiI+UiZhbXA7SjwvZGl2PgogIDxkaXYgY2xhc3M9ImxvZ28tc3ViIiBkYXRhLWkxOG49ImxvZ29fc3ViIj5Hcm9vbWluZyDCtyDQotCw0LvQu9C40L08L2Rpdj4KICA8ZGl2IGNsYXNzPSJwcm9ncmVzcyI+CiAgICA8ZGl2IGNsYXNzPSJwcyBhY3RpdmUiIGlkPSJwczEiPjxkaXYgY2xhc3M9InBkb3QiPjwvZGl2PjxzcGFuIGRhdGEtaTE4bj0icHNfc2VydmljZSI+0KPRgdC70YPQs9CwPC9zcGFuPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icGwiIGlkPSJwbDEiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icHMiIGlkPSJwczIiPjxkaXYgY2xhc3M9InBkb3QiPjwvZGl2PjxzcGFuIGRhdGEtaTE4bj0icHNfbWFzdGVyIj7QnNCw0YHRgtC10YA8L3NwYW4+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwbCIgaWQ9InBsMiI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwcyIgaWQ9InBzMyI+PGRpdiBjbGFzcz0icGRvdCI+PC9kaXY+PHNwYW4gZGF0YS1pMThuPSJwc19wZXQiPtCf0LjRgtC+0LzQtdGGPC9zcGFuPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icGwiIGlkPSJwbDMiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icHMiIGlkPSJwczQiPjxkaXYgY2xhc3M9InBkb3QiPjwvZGl2PjxzcGFuIGRhdGEtaTE4bj0icHNfZGF0ZSI+0JTQsNGC0LA8L3NwYW4+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwbCIgaWQ9InBsNCI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwcyIgaWQ9InBzNSI+PGRpdiBjbGFzcz0icGRvdCI+PC9kaXY+PHNwYW4gZGF0YS1pMThuPSJwc19kZXRhaWxzIj7QlNCw0L3QvdGL0LU8L3NwYW4+PC9kaXY+CiAgPC9kaXY+CgogIDwhLS0gU3RlcCAxIC0tPgogIDxkaXYgY2xhc3M9InN0ZXAgc2hvdyIgaWQ9ImJrMSI+CiAgICA8ZGl2IGNsYXNzPSJzbGJsIiBkYXRhLWkxOG49InN0ZXAxX2xibCI+MDEgwrcg0J/QvtGA0L7QtNCwPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJid3JhcCI+CiAgICAgIDxkaXYgY2xhc3M9InNib3giPgogICAgICAgIDxzcGFuIGNsYXNzPSJzaSI+8J+UjTwvc3Bhbj4KICAgICAgICA8aW5wdXQgaWQ9ImJJbnB1dCIgdHlwZT0idGV4dCIgcGxhY2Vob2xkZXI9ItCd0LDRh9C90LjRgtC1INCy0LLQvtC00LjRgtGMINC/0L7RgNC+0LTRgy4uLiIgZGF0YS1pMThuLXBoPSJicmVlZF9waCIgYXV0b2NvbXBsZXRlPSJvZmYiPgogICAgICAgIDxidXR0b24gY2xhc3M9ImNsciIgaWQ9ImNsckJ0biI+4pyVPC9idXR0b24+CiAgICAgIDwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJkcm9wIiBpZD0iYkRyb3AiPjwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzYmFkZ2UiIGlkPSJzQmFkZ2UiPjwvZGl2PgogICAgPGRpdiBpZD0ic3ZjU2VjIiBzdHlsZT0iZGlzcGxheTpub25lO21hcmdpbi10b3A6MTZweCI+CiAgICAgIDxkaXYgY2xhc3M9InNsYmwiIGlkPSJzdGVwMkxibEVsIiBkYXRhLWkxOG49InN0ZXAyX2xibCI+MDIgwrcg0KPRgdC70YPQs9CwPC9kaXY+CiAgICAgIDxkaXYgaWQ9InN2Y0xpc3QiPjwvZGl2PgogICAgPC9kaXY+CiAgPC9kaXY+CgogIDwhLS0gU3RlcCAyIC0tPgogIDxkaXYgY2xhc3M9InN0ZXAiIGlkPSJiazIiPgogICAgPGRpdiBjbGFzcz0ic2xibCIgZGF0YS1pMThuPSJzdGVwMl9tYXN0ZXIiPtCS0YvQsdC10YDQuNGC0LUg0LzQsNGB0YLQtdGA0LA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im1hc3RlcnMiPgogICAgICA8ZGl2IGNsYXNzPSJtYnRuIiBkYXRhLW1hc3Rlcj0i0KLQsNGC0YzRj9C90LAiPjxkaXYgY2xhc3M9Im1uYW1lIj7QotCw0YLRjNGP0L3QsDwvZGl2PjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJtYnRuIiBkYXRhLW1hc3Rlcj0i0JDQu9C40YHQsCI+PGRpdiBjbGFzcz0ibW5hbWUiPtCQ0LvQuNGB0LA8L2Rpdj48L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0ibWJ0biIgZGF0YS1tYXN0ZXI9ItCa0YDQuNGB0YLQuNC90LAiPjxkaXYgY2xhc3M9Im1uYW1lIj7QmtGA0LjRgdGC0LjQvdCwPC9kaXY+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9Im1idG4iIGRhdGEtbWFzdGVyPSLQkNC90L3QsCI+PGRpdiBjbGFzcz0ibW5hbWUiPtCQ0L3QvdCwPC9kaXY+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9Im1idG4iIGRhdGEtbWFzdGVyPSLQkNC70LXQutGB0LDQvdC00YDQsCI+PGRpdiBjbGFzcz0ibW5hbWUiPtCQ0LvQtdC60YHQsNC90LTRgNCwPC9kaXY+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9Im1idG4iIGRhdGEtbWFzdGVyPSLQmtGB0LXQvdC40Y8iPjxkaXYgY2xhc3M9Im1uYW1lIj7QmtGB0LXQvdC40Y88L2Rpdj48L2Rpdj4KICAgIDwvZGl2PgogIDwvZGl2PgoKICA8IS0tIFN0ZXAgMyAtLT4KICA8ZGl2IGNsYXNzPSJzdGVwIiBpZD0iYmszIj4KICAgIDxkaXYgY2xhc3M9InNsYmwiIGRhdGEtaTE4bj0ic3RlcDNfbGJsIj7QmtCw0Log0LTQsNCy0L3QviDQstGLINC/0L7RgdC10YnQsNC70Lgg0LPRgNGD0LzQuNC90LM/PC9kaXY+CiAgICA8YnV0dG9uIGNsYXNzPSJnYnRuIiBkYXRhLXZhbD0i0J/QtdGA0LLRi9C5INGA0LDQtyIgZGF0YS1pMThuPSJnMSI+0J/QtdGA0LLRi9C5INGA0LDQtzwvYnV0dG9uPgogICAgPGJ1dHRvbiBjbGFzcz0iZ2J0biIgZGF0YS12YWw9ItCe0YIgMSDQtNC+IDMg0LzQtdGB0Y/RhtC10LIiIGRhdGEtaTE4bj0iZzIiPtCe0YIgMSDQtNC+IDMg0LzQtdGB0Y/RhtC10LI8L2J1dHRvbj4KICAgIDxidXR0b24gY2xhc3M9ImdidG4iIGRhdGEtdmFsPSLQntGCIDMg0LTQviA2INC80LXRgdGP0YbQtdCyIiBkYXRhLWkxOG49ImczIj7QntGCIDMg0LTQviA2INC80LXRgdGP0YbQtdCyPC9idXR0b24+CiAgICA8YnV0dG9uIGNsYXNzPSJnYnRuIiBkYXRhLXZhbD0i0JHQvtC70LXQtSA2INC80LXRgdGP0YbQtdCyIiBkYXRhLWkxOG49Imc0Ij7QkdC+0LvQtdC1IDYg0LzQtdGB0Y/RhtC10LI8L2J1dHRvbj4KICA8L2Rpdj4KCiAgPCEtLSBTdGVwIDQgLS0+CiAgPGRpdiBjbGFzcz0ic3RlcCIgaWQ9ImJrNCI+CiAgICA8ZGl2IGNsYXNzPSJzbGJsIiBkYXRhLWkxOG49InN0ZXA0X2xibCI+0JLRi9Cx0LXRgNC40YLQtSDQtNCw0YLRgzwvZGl2PgogICAgPGRpdiBjbGFzcz0iY2FsLWgiPgogICAgICA8YnV0dG9uIGNsYXNzPSJjYWwtbiIgaWQ9InByZXZNIj4mIzgyNDk7PC9idXR0b24+CiAgICAgIDxkaXYgY2xhc3M9ImNhbC1tIiBpZD0iY2FsTSI+PC9kaXY+CiAgICAgIDxidXR0b24gY2xhc3M9ImNhbC1uIiBpZD0ibmV4dE0iPiYjODI1MDs8L2J1dHRvbj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0iY2ciIGlkPSJjYWxHIj48L2Rpdj4KICAgIDxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDtnYXA6MjBweDthbGlnbi1pdGVtczpjZW50ZXI7bWFyZ2luLXRvcDoxMnB4O3BhZGRpbmctdG9wOjEycHg7Ym9yZGVyLXRvcDoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMSk7ZmxleC13cmFwOndyYXA7Ij48ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo4cHg7Ij48ZGl2IHN0eWxlPSJ3aWR0aDoxNnB4O2hlaWdodDoxNnB4O2JvcmRlci1yYWRpdXM6NTAlO2JhY2tncm91bmQ6cmdiYSg5MCwxODAsOTAsLjE1KTtib3JkZXI6MXB4IHNvbGlkICM1YWI0NWE7ZmxleC1zaHJpbms6MDsiPjwvZGl2PjxzcGFuIHN0eWxlPSJmb250LXNpemU6MXJlbTtjb2xvcjojZmZmZmZmO2xldHRlci1zcGFjaW5nOi4wM2VtOyIgZGF0YS1pMThuPSJjYWxfYXZhaWwiPtCV0YHRgtGMINGB0LLQvtCx0L7QtNC90L7QtSDQstGA0LXQvNGPPC9zcGFuPjwvZGl2PjxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweDsiPjxkaXYgc3R5bGU9IndpZHRoOjE2cHg7aGVpZ2h0OjE2cHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNCk7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xKTtmbGV4LXNocmluazowOyI+PC9kaXY+PHNwYW4gc3R5bGU9ImZvbnQtc2l6ZToxcmVtO2NvbG9yOiNmZmZmZmY7bGV0dGVyLXNwYWNpbmc6LjAzZW07IiBkYXRhLWkxOG49ImNhbF9ub25lIj7QodCy0L7QsdC+0LTQvdC+0LPQviDQstGA0LXQvNC10L3QuCDQvdC10YI8L3NwYW4+PC9kaXY+PC9kaXY+CiAgICA8ZGl2IGlkPSJ0aW1lU2VjIiBzdHlsZT0iZGlzcGxheTpub25lO21hcmdpbi10b3A6MTZweCI+CiAgICAgIDxkaXYgY2xhc3M9InNsYmwiIGRhdGEtaTE4bj0ic3RlcDRfdGltZSI+0JLRi9Cx0LXRgNC40YLQtSDQstGA0LXQvNGPPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9InRnIiBpZD0idGltZUciPjwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IHN0eWxlPSJtYXJnaW4tdG9wOjIwcHg7cGFkZGluZy10b3A6MTZweDtib3JkZXItdG9wOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wOCk7dGV4dC1hbGlnbjpjZW50ZXIiPgogICAgICA8YnV0dG9uIGlkPSJjYWxsYmFja0J0biIgY2xhc3M9ImNiay1idG4iPtCd0LUg0L3QsNGI0LvQuCDRg9C00L7QsdC90L7QtSDQstGA0LXQvNGPPyDihpI8L2J1dHRvbj4KICAgIDwvZGl2PgogIDwvZGl2PgoKICA8IS0tIFN0ZXAgNSAtLT4KICA8ZGl2IGNsYXNzPSJzdGVwIiBpZD0iYms1Ij4KICAgIDxkaXYgY2xhc3M9InNsYmwiIGRhdGEtaTE4bj0ic3RlcDVfbGJsIj7QktCw0YjQuCDQtNCw0L3QvdGL0LU8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImZnIj48bGFiZWwgY2xhc3M9ImZsIiBkYXRhLWkxOG49ImxibF9uYW1lIj7QmNC80Y88L2xhYmVsPjxpbnB1dCBjbGFzcz0iZmkiIGlkPSJjTmFtZSIgdHlwZT0idGV4dCIgcGxhY2Vob2xkZXI9ItCS0LDRiNC1INC40LzRjyIgZGF0YS1pMThuLXBoPSJwaF9uYW1lIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImZnIj48bGFiZWwgY2xhc3M9ImZsIiBkYXRhLWkxOG49ImxibF9waG9uZSI+0KLQtdC70LXRhNC+0L08L2xhYmVsPjxpbnB1dCBjbGFzcz0iZmkiIGlkPSJjUGhvbmUiIHR5cGU9InRlbCIgcGxhY2Vob2xkZXI9IiszNzIgLi4uIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImZnIj48bGFiZWwgY2xhc3M9ImZsIiBkYXRhLWkxOG49ImxibF9lbWFpbCI+RW1haWw8L2xhYmVsPjxpbnB1dCBjbGFzcz0iZmkiIGlkPSJjRW1haWwiIHR5cGU9ImVtYWlsIiBwbGFjZWhvbGRlcj0iZW1haWxAZXhhbXBsZS5jb20iPjwvZGl2PgogICAgPGRpdiBjbGFzcz0iZmciPjxsYWJlbCBjbGFzcz0iZmwiIGRhdGEtaTE4bj0ibGJsX3BldCI+0JrQu9C40YfQutCwINC/0LjRgtC+0LzRhtCwPC9sYWJlbD48aW5wdXQgY2xhc3M9ImZpIiBpZD0iY1BldCIgdHlwZT0idGV4dCIgcGxhY2Vob2xkZXI9ItCd0LXQvtCx0Y/Qt9Cw0YLQtdC70YzQvdC+IiBkYXRhLWkxOG4tcGg9InBoX29wdGlvbmFsIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InN1bSIgaWQ9InN1bUJsb2NrIj48L2Rpdj4KICAgIDxidXR0b24gY2xhc3M9ImNidG4iIGlkPSJjb25maXJtQnRuIiBkYXRhLWkxOG49ImNvbmZpcm1fYnRuIj7Qn9C+0LTRgtCy0LXRgNC00LjRgtGMINC30LDQv9C40YHRjDwvYnV0dG9uPgogIDwvZGl2PgoKICA8IS0tIFN1Y2Nlc3MgLS0+CiAgPGRpdiBjbGFzcz0ic2Jsb2NrIiBpZD0ic3VjQmxvY2siPgogICAgPGRpdiBjbGFzcz0ic2kyIj7wn5C+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzdCIgZGF0YS1pMThuPSJzdWNjZXNzX3RpdGxlIj7Ql9Cw0L/QuNGB0Ywg0L/RgNC40L3Rj9GC0LAhPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzcyIgZGF0YS1pMThuPSJzdWNjZXNzX3N1YiI+0JzRiyDRgdCy0Y/QttC10LzRgdGPINGBINCy0LDQvNC4INC00LvRjyDQv9C+0LTRgtCy0LXRgNC20LTQtdC90LjRjy48YnI+0KHQv9Cw0YHQuNCx0L4sINGH0YLQviDQstGL0LHRgNCw0LvQuCBSJkogR3Jvb21pbmchPC9kaXY+CiAgICA8YnV0dG9uIGNsYXNzPSJoYnRuIiBpZD0iaG9tZUJ0biIgZGF0YS1pMThuPSJ0b19ob21lIj7ihpAg0J3QsCDQs9C70LDQstC90YPRjjwvYnV0dG9uPgogIDwvZGl2Pgo8L2Rpdj4KPC9kaXY+Cgo8ZGl2IGlkPSJjYmtNb2RhbCIgc3R5bGU9ImRpc3BsYXk6bm9uZTtwb3NpdGlvbjpmaXhlZDtpbnNldDowO2JhY2tncm91bmQ6cmdiYSgwLDAsMCwuNzUpO3otaW5kZXg6MzAwO2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO3BhZGRpbmc6MjBweCI+CiAgPGRpdiBzdHlsZT0iYmFja2dyb3VuZDojMGEwYTBhO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTIpO2JvcmRlci10b3A6MXB4IHNvbGlkICNmZmZmZmY7cGFkZGluZzoyOHB4IDI0cHg7d2lkdGg6MTAwJTttYXgtd2lkdGg6MzYwcHgiPgogICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjAuODM4cmVtO2xldHRlci1zcGFjaW5nOi4yZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbToxNnB4O2ZvbnQtd2VpZ2h0OjYwMDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZiI+0J7QsdGA0LDRgtC90YvQuSDQt9Cy0L7QvdC+0Lo8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImZnIj48bGFiZWwgY2xhc3M9ImZsIj7QmNC80Y88L2xhYmVsPjxpbnB1dCBjbGFzcz0iZmkiIGlkPSJjYmtOYW1lIiB0eXBlPSJ0ZXh0IiBwbGFjZWhvbGRlcj0i0JLQsNGI0LUg0LjQvNGPIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImZnIj4KICAgICAgPGxhYmVsIGNsYXNzPSJmbCI+0KLQtdC70LXRhNC+0L08L2xhYmVsPgogICAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6c3RyZXRjaDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xNSkiPgogICAgICAgIDxzcGFuIHN0eWxlPSJwYWRkaW5nOjEwcHggMTBweCAxMHB4IDA7Y29sb3I6I2ZmZmZmZjtmb250LXNpemU6MS4zNjNyZW07Ym9yZGVyLXJpZ2h0OjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xKTttYXJnaW4tcmlnaHQ6MTBweDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZiI+KzM3Mjwvc3Bhbj4KICAgICAgICA8aW5wdXQgaWQ9ImNia1Bob25lIiB0eXBlPSJ0ZWwiIHBsYWNlaG9sZGVyPSJYWFhYWFhYWCIgc3R5bGU9ImZsZXg6MTtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjpub25lO291dGxpbmU6bm9uZTtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjEuNDM4cmVtO2NvbG9yOiNmZmZmZmY7cGFkZGluZzoxMHB4IDAiPgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBpZD0iY2JrU3VjY2VzcyIgc3R5bGU9ImRpc3BsYXk6bm9uZTt0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjIwcHggMCI+CiAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToyLjg3NXJlbTttYXJnaW4tYm90dG9tOjEwcHg7b3BhY2l0eTouNSI+4pyTPC9kaXY+CiAgICAgIDxkaXYgc3R5bGU9ImZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6MS44NzVyZW07Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjZweCI+0JfQsNGP0LLQutCwINC/0YDQuNC90Y/RgtCwITwvZGl2PgogICAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6MS4wMzdyZW07Y29sb3I6I2ZmZmZmZjtsaW5lLWhlaWdodDoxLjY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWYiPtCc0Ysg0L/QtdGA0LXQt9Cy0L7QvdC40Lwg0LLQsNC8INCyINCx0LvQuNC20LDQudGI0LXQtSDQstGA0LXQvNGPPC9kaXY+CiAgICA8L2Rpdj4KICAgIDxidXR0b24gaWQ9ImNia1N1Ym1pdCIgY2xhc3M9ImNidG4iIHN0eWxlPSJtYXJnaW4tdG9wOjE0cHgiPtCe0YLQv9GA0LDQstC40YLRjDwvYnV0dG9uPgogICAgPGJ1dHRvbiBpZD0iY2JrQ2xvc2UiIHN0eWxlPSJkaXNwbGF5OmJsb2NrO3dpZHRoOjEwMCU7bWFyZ2luLXRvcDo4cHg7YmFja2dyb3VuZDpub25lO2JvcmRlcjpub25lO2NvbG9yOiNmZmZmZmY7Zm9udC1zaXplOjAuODM4cmVtO2xldHRlci1zcGFjaW5nOi4xMmVtO2N1cnNvcjpwb2ludGVyO3BhZGRpbmc6OHB4O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmIj7QntGC0LzQtdC90LA8L2J1dHRvbj4KICA8L2Rpdj4KPC9kaXY+Cgo8c2NyaXB0Pgp2YXIgREFUQSA9IFt7ImJyZWVkIjoi0JDQstGB0YLRgNCw0LvQuNC50YHQutCw0Y8g0L7QstGH0LDRgNC60LAgMTXigJMyNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6ODV9LCJicmVlZF9lbiI6IkF1c3RyYWxpYW4gU2hlcGhlcmQgMTXigJMyNSBrZyIsImJyZWVkX2V0IjoiQXVzdHJhYWxpYSBsYW1iYWtvZXIgMTXigJMyNSBrZyJ9LHsiYnJlZWQiOiLQkNCy0YHRgtGA0LDQu9C40LnRgdC60LDRjyDQvtCy0YfQsNGA0LrQsCAyNeKAkzM1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IkF1c3RyYWxpYW4gU2hlcGhlcmQgMjXigJMzNSBrZyIsImJyZWVkX2V0IjoiQXVzdHJhYWxpYSBsYW1iYWtvZXIgMjXigJMzNSBrZyJ9LHsiYnJlZWQiOiLQkNC60LjRgtCwLdC40L3RgyAyMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJBa2l0YSBJbnUgMjDigJM0MCBrZyIsImJyZWVkX2V0IjoiQWtpdGEgSW51IDIw4oCTNDAga2cifSx7ImJyZWVkIjoi0JDQutC40YLQsC3QuNC90YMg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkFraXRhIEludSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJBa2l0YSBJbnUgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQkNC60LjRgtCwLdC40L3RgyDRhNC70LDRhNGE0LggMjDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQWtpdGEgSW51IGZsdWZmeSAyMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBa2l0YSBJbnUgcGVobWVrYXJ2YWxpbmUgMjDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC60LjRgtCwLdC40L3RgyDRhNC70LDRhNGE0Lgg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkFraXRhIEludSBmbHVmZnkgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiQWtpdGEgSW51IHBlaG1la2FydmFsaW5lIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JDQu9Cw0LHQsNC5IDQw4oCTNjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJDZW50cmFsIEFzaWFuIFNoZXBoZXJkIDQw4oCTNjAga2ciLCJicmVlZF9ldCI6Iktlc2stQWFzaWEgbGFtYmFrb2VyIDQw4oCTNjAga2cifSx7ImJyZWVkIjoi0JDQu9Cw0LHQsNC5INCx0L7Qu9C10LUgNjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoxMDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjoxMTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMzB9LCJicmVlZF9lbiI6IkNlbnRyYWwgQXNpYW4gU2hlcGhlcmQgb3ZlciA2MCBrZyIsImJyZWVkX2V0IjoiS2Vzay1BYXNpYSBsYW1iYWtvZXIgw7xsZSA2MCBrZyJ9LHsiYnJlZWQiOiLQkNC70Y/RgdC60LjQvdGB0LrQuNC5INC80LDQu9Cw0LzRg9GCIDIw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkFsYXNrYW4gTWFsYW11dGUgMjDigJM0MCBrZyIsImJyZWVkX2V0IjoiQWxhc2thIG1hbGFtdXV0IDIw4oCTNDAga2cifSx7ImJyZWVkIjoi0JDQu9GP0YHQutC40L3RgdC60LjQuSDQvNCw0LvQsNC80YPRgiDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQWxhc2thbiBNYWxhbXV0ZSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJBbGFza2EgbWFsYW11dXQgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQkNC70Y/RgdC60LjQvdGB0LrQuNC5INC80LDQu9Cw0LzRg9GCINGE0LvQsNGE0YTQuCAyMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJBbGFza2FuIE1hbGFtdXRlIGZsdWZmeSAyMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBbGFza2EgbWFsYW11dXQgcGVobWVrYXJ2YWxpbmUgMjDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC70Y/RgdC60LjQvdGB0LrQuNC5INC80LDQu9Cw0LzRg9GCINGE0LvQsNGE0YTQuCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQWxhc2thbiBNYWxhbXV0ZSBmbHVmZnkgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiQWxhc2thIG1hbGFtdXV0IHBlaG1la2FydmFsaW5lIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JDQvNC10YDQuNC60LDQvdGB0LrQsNGPINCw0LrQuNGC0LAgMjDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQW1lcmljYW4gQWtpdGEgMjDigJM0MCBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2EgQWtpdGEgMjDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutCw0Y8g0LDQutC40YLQsCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQW1lcmljYW4gQWtpdGEgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2EgQWtpdGEgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutCw0Y8g0LDQutC40YLQsCDRhNC70LDRhNGE0LggMjDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQW1lcmljYW4gQWtpdGEgZmx1ZmZ5IDIw4oCTNDAga2ciLCJicmVlZF9ldCI6IkFtZWVyaWthIEFraXRhIHBlaG1la2FydmFsaW5lIDIw4oCTNDAga2cifSx7ImJyZWVkIjoi0JDQvNC10YDQuNC60LDQvdGB0LrQsNGPINCw0LrQuNGC0LAg0YTQu9Cw0YTRhNC4INCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBBa2l0YSBmbHVmZnkgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2EgQWtpdGEgcGVobWVrYXJ2YWxpbmUgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutC40Lkg0LrQvtC60LXRgC3RgdC/0LDQvdC40LXQu9GMIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MH0sImJyZWVkX2VuIjoiQW1lcmljYW4gQ29ja2VyIFNwYW5pZWwgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2Ega29rZXJzcGFuamVsIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0JDQvNC10YDQuNC60LDQvdGB0LrQuNC5INC60L7QutC10YAt0YHQv9Cw0L3QuNC10LvRjCAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODB9LCJicmVlZF9lbiI6IkFtZXJpY2FuIENvY2tlciBTcGFuaWVsIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IkFtZWVyaWthIGtva2Vyc3BhbmplbCAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LjQuSDRgdGC0LDRhNGE0L7RgNC00YjQuNGA0YHQutC40Lkg0YLQtdGA0YzQtdGAIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBTdGFmZm9yZHNoaXJlIFRlcnJpZXIgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2EgU3RhZmZvcmRzaGlyZSB0ZXJqZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutC40Lkg0YHRgtCw0YTRhNC+0YDQtNGI0LjRgNGB0LrQuNC5INGC0LXRgNGM0LXRgCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NX0sImJyZWVkX2VuIjoiQW1lcmljYW4gU3RhZmZvcmRzaGlyZSBUZXJyaWVyIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkFtZWVyaWthIFN0YWZmb3Jkc2hpcmUgdGVyamVyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JDQvdCz0LvQuNC50YHQutC40Lkg0LHRg9C70YzQtNC+0LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjcwfSwiYnJlZWRfZW4iOiJFbmdsaXNoIEJ1bGxkb2ciLCJicmVlZF9ldCI6IkluZ2xpc2UgYnVsZG9nIn0seyJicmVlZCI6ItCQ0L3Qs9C70LjQudGB0LrQuNC5INC60L7QutC10YAt0YHQv9Cw0L3QuNC10LvRjCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IkVuZ2xpc2ggQ29ja2VyIFNwYW5pZWwgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiSW5nbGlzZSBrb2tlcnNwYW5qZWwgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQkNC90LPQu9C40LnRgdC60LjQuSDQutC+0LrQtdGALdGB0L/QsNC90LjQtdC70YwgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwfSwiYnJlZWRfZW4iOiJFbmdsaXNoIENvY2tlciBTcGFuaWVsIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IkluZ2xpc2Uga29rZXJzcGFuamVsIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0JDRhNCz0LDQvSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTB9LCJicmVlZF9lbiI6IkFmZ2hhbiBIb3VuZCAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJBZmdhbmlzdGFuaSBrb2VyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JDRhNCz0LDQvSAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwfSwiYnJlZWRfZW4iOiJBZmdoYW4gSG91bmQgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiQWZnYW5pc3Rhbmkga29lciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCR0LDRgdGB0LXRgi3RhdCw0YPQvdC0IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJCYXNzZXQgSG91bmQgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiQmFzc2V0aG91bmQgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQkdCw0YHRgdC10YIt0YXQsNGD0L3QtCAzMOKAkzM1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NX0sImJyZWVkX2VuIjoiQmFzc2V0IEhvdW5kIDMw4oCTMzUga2ciLCJicmVlZF9ldCI6IkJhc3NldGhvdW5kIDMw4oCTMzUga2cifSx7ImJyZWVkIjoi0JHQtdGA0L3RgdC60LjQuSDQt9C10L3QvdC10L3RhdGD0L3QtCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTEwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJCZXJuZXNlIE1vdW50YWluIERvZyAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJCZXJuaSBtw6RnaWtvZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkdC10YDQvdGB0LrQuNC5INC30LXQvdC90LXQvdGF0YPQvdC0INCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMzAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkJlcm5lc2UgTW91bnRhaW4gRG9nIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkJlcm5pIG3DpGdpa29lciDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCR0LjQstC10YAt0LnQvtGA0Log0LHQvtC70LXQtSAzLDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiQmlld2VyIFlvcmtzaGlyZSBUZXJyaWVyIG92ZXIgMyw1IGtnIiwiYnJlZWRfZXQiOiJCaWV3ZXIgWW9ya3NoaXJlIFRlcnJpZXIgw7xsZSAzLDUga2cifSx7ImJyZWVkIjoi0JHQuNCy0LXRgC3QudC+0YDQuiDQtNC+IDMsNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJCaWV3ZXIgWW9ya3NoaXJlIFRlcnJpZXIgdXAgdG8gMyw1IGtnIiwiYnJlZWRfZXQiOiJCaWV3ZXIgWW9ya3NoaXJlIFRlcnJpZXIga3VuaSAzLDUga2cifSx7ImJyZWVkIjoi0JHQuNCz0LvRjCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjYwfSwiYnJlZWRfZW4iOiJCZWFnbGUgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiQmlpZ2VsIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0JHQuNCz0LvRjCAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjY1fSwiYnJlZWRfZW4iOiJCZWFnbGUgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiQmlpZ2VsIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0JHQuNGI0L7QvS3RhNGA0LjQt9C1IDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJCaWNob24gRnJpc8OpIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiQmnFoW9uIEZyaXPDqSA14oCTMTAga2cifSx7ImJyZWVkIjoi0JHQuNGI0L7QvS3RhNGA0LjQt9C1INC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJCaWNob24gRnJpc8OpIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IkJpxaFvbiBGcmlzw6kga3VuaSA1IGtnIn0seyJicmVlZCI6ItCR0L7QutGB0LXRgCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MH0sImJyZWVkX2VuIjoiQm94ZXIgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiQm9rc2VyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JHQvtC60YHQtdGAIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJCb3hlciAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJCb2tzZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkdC+0YDQtNC10YAt0LrQvtC70LvQuCAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4MH0sImJyZWVkX2VuIjoiQm9yZGVyIENvbGxpZSAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiJCb3JkZXJrb2xsIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0JHQvtGA0LTQtdGALdC60L7Qu9C70LggMjDigJMyNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJCb3JkZXIgQ29sbGllIDIw4oCTMjUga2ciLCJicmVlZF9ldCI6IkJvcmRlcmtvbGwgMjDigJMyNSBrZyJ9LHsiYnJlZWQiOiLQkdC+0YHRgtC+0L0t0YLQtdGA0YzQtdGAIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1fSwiYnJlZWRfZW4iOiJCb3N0b24gVGVycmllciAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJCb3N0b25pIHRlcmplciAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCR0L7RgdGC0L7QvS3RgtC10YDRjNC10YAgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MH0sImJyZWVkX2VuIjoiQm9zdG9uIFRlcnJpZXIgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJCb3N0b25pIHRlcmplciA14oCTMTAga2cifSx7ImJyZWVkIjoi0JHRgNCw0LHQsNC90YHQvtC9Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo1MH0sImJyZWVkX2VuIjoiR3JpZmZvbiBCcnV4ZWxsb2lzIiwiYnJlZWRfZXQiOiJCcsO8c3NlbGkgZ3JpZm9uIn0seyJicmVlZCI6ItCR0YPQu9GM0YLQtdGA0YzQtdGAIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwfSwiYnJlZWRfZW4iOiJCdWxsIFRlcnJpZXIgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiQnVsbHRlcmplciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCS0LXQu9GM0Ygt0LrQvtGA0LPQuCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjY1fSwiYnJlZWRfZW4iOiJXZWxzaCBDb3JnaSAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJXYWxlc2kga29yZ2kgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQktC10LvRjNGILdC60L7RgNCz0LggMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo3NX0sImJyZWVkX2VuIjoiV2Vsc2ggQ29yZ2kgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiV2FsZXNpIGtvcmdpIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0JLQtdGB0YIt0YXQsNC50LvQtdC90LQt0LLQsNC50YIt0YLQtdGA0YzQtdGAIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiV2VzdCBIaWdobGFuZCBXaGl0ZSBUZXJyaWVyIiwiYnJlZWRfZXQiOiJMw6TDpG5lLcWgb3RpbWFhIHZhbGdlIHRlcmplciJ9LHsiYnJlZWQiOiLQktC+0YHRgtC+0YfQvdC+0YHQuNCx0LjRgNGB0LrQsNGPINC70LDQudC60LAgMTjigJMyNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4NX0sImJyZWVkX2VuIjoiRWFzdCBTaWJlcmlhbiBMYWlrYSAxOOKAkzI1IGtnIiwiYnJlZWRfZXQiOiJJZGEtU2liZXJpIGxhaWthIDE44oCTMjUga2cifSx7ImJyZWVkIjoi0JLQvtGB0YLQvtGH0L3QvtGB0LjQsdC40YDRgdC60LDRjyDQu9Cw0LnQutCwINCx0L7Qu9C10LUgMjUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJFYXN0IFNpYmVyaWFuIExhaWthIG92ZXIgMjUga2ciLCJicmVlZF9ldCI6IklkYS1TaWJlcmkgbGFpa2Egw7xsZSAyNSBrZyJ9LHsiYnJlZWQiOiLQk9C+0LvQtNC10L0t0YDQtdGC0YDQuNCy0LXRgCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkdvbGRlbiBSZXRyaWV2ZXIgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiS3VsZG5lIHJldHJpaXZlciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCT0L7Qu9C00LXQvS3RgNC10YLRgNC40LLQtdGAIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IkdvbGRlbiBSZXRyaWV2ZXIgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiS3VsZG5lIHJldHJpaXZlciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCT0YDQuNGE0YTQvtC9Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiR3JpZmZvbiIsImJyZWVkX2V0IjoiR3JpZm9uIn0seyJicmVlZCI6ItCU0LDQu9C80LDRgtC40L0iLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjcwfSwiYnJlZWRfZW4iOiJEYWxtYXRpYW4iLCJicmVlZF9ldCI6IkRhbG1hYXRzaWEga29lciJ9LHsiYnJlZWQiOiLQlNC20LXQui3RgNCw0YHRgdC10Lst0YLQtdGA0YzQtdGAINCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdGL0LkiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjUwfSwiYnJlZWRfZW4iOiJKYWNrIFJ1c3NlbGwgVGVycmllciBzbW9vdGgiLCJicmVlZF9ldCI6IkphY2sgUnVzc2VsbGkgdGVyamVyIGzDvGhpa2FydmFsaW5lIn0seyJicmVlZCI6ItCU0LbQtdC6LdGA0LDRgdGB0LXQuy3RgtC10YDRjNC10YAg0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90YvQuSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MCwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6IkphY2sgUnVzc2VsbCBUZXJyaWVyIHdpcmUtaGFpcmVkIiwiYnJlZWRfZXQiOiJKYWNrIFJ1c3NlbGxpIHRlcmplciBrYXJ1a2FydmFsaW5lIn0seyJicmVlZCI6ItCU0L7QsdC10YDQvNCw0L0gMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjU1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4MH0sImJyZWVkX2VuIjoiRG9iZXJtYW5uIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkRvYmVybWFubiAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCU0L7QsdC10YDQvNCw0L0g0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5NX0sImJyZWVkX2VuIjoiRG9iZXJtYW5uIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkRvYmVybWFubiDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCX0LDQv9Cw0LTQvdC+0YHQuNCx0LjRgNGB0LrQsNGPINC70LDQudC60LAgMTjigJMyNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4NX0sImJyZWVkX2VuIjoiV2VzdCBTaWJlcmlhbiBMYWlrYSAxOOKAkzI1IGtnIiwiYnJlZWRfZXQiOiJMw6TDpG5lLVNpYmVyaSBsYWlrYSAxOOKAkzI1IGtnIn0seyJicmVlZCI6ItCX0L7Qu9C+0YLQuNGB0YLRi9C5INGA0LXRgtGA0LjQstC10YAgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkdvbGRlbiBSZXRyaWV2ZXIgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiS3VsZG5lIHJldHJpaXZlciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCX0L7Qu9C+0YLQuNGB0YLRi9C5INGA0LXRgtGA0LjQstC10YAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjY1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjExMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExMH0sImJyZWVkX2VuIjoiR29sZGVuIFJldHJpZXZlciAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJLdWxkbmUgcmV0cmlpdmVyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JjRgNC70LDQvdC00YHQutC40Lkg0LzRj9Cz0LrQvtGI0LXRgNGB0YLQvdGL0Lkg0L/RiNC10L3QuNGH0L3Ri9C5INGC0LXRgNGM0LXRgCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4MH0sImJyZWVkX2VuIjoiSXJpc2ggU29mdCBDb2F0ZWQgV2hlYXRlbiBUZXJyaWVyIiwiYnJlZWRfZXQiOiJJaXJpIHBlaG1la2FydmFuZSBuaXN1dsOkcnZpIHRlcmplciJ9LHsiYnJlZWQiOiLQmNGA0LvQsNC90LTRgdC60LjQuSDRgtC10YDRjNC10YAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzAsItCi0YDQuNC80LzQuNC90LMiOjc1fSwiYnJlZWRfZW4iOiJJcmlzaCBUZXJyaWVyIiwiYnJlZWRfZXQiOiJJaXJpIHRlcmplciJ9LHsiYnJlZWQiOiLQmNGB0L/QsNC90YHQutC40Lkg0LPQsNC70YzQs9C+IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NzB9LCJicmVlZF9lbiI6IlNwYW5pc2ggR2FsZ28gMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiSGlzcGFhbmlhIGdhbGdvIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JjRgdC/0LDQvdGB0LrQuNC5INCz0LDQu9GM0LPQviAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjgwfSwiYnJlZWRfZW4iOiJTcGFuaXNoIEdhbGdvIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6Ikhpc3BhYW5pYSBnYWxnbyAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCZ0L7RgNC60YjQuNGA0YHQutC40Lkg0YLQtdGA0YzQtdGAINCx0L7Qu9C10LUgMyw1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IllvcmtzaGlyZSBUZXJyaWVyIG92ZXIgMyw1IGtnIiwiYnJlZWRfZXQiOiJZb3Jrc2hpcmUgdGVyamVyIMO8bGUgMyw1IGtnIn0seyJicmVlZCI6ItCZ0L7RgNC60YjQuNGA0YHQutC40Lkg0YLQtdGA0YzQtdGAINC00L4gMyw1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IllvcmtzaGlyZSBUZXJyaWVyIHVwIHRvIDMsNSBrZyIsImJyZWVkX2V0IjoiWW9ya3NoaXJlIHRlcmplciBrdW5pIDMsNSBrZyJ9LHsiYnJlZWQiOiLQmtCw0LLQsNC70LXRgC3QutC40L3Qsy3Rh9Cw0YDQu9GM0Lct0YHQv9Cw0L3QuNC10LvRjCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IkNhdmFsaWVyIEtpbmcgQ2hhcmxlcyBTcGFuaWVsIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IkNhdmFsaWVyIEtpbmcgQ2hhcmxlcyBTcGFuaWVsIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0JrQsNCy0LDQu9C10YAt0LrQuNC90LMt0YfQsNGA0LvRjNC3LdGB0L/QsNC90LjQtdC70YwgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkNhdmFsaWVyIEtpbmcgQ2hhcmxlcyBTcGFuaWVsIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiQ2F2YWxpZXIgS2luZyBDaGFybGVzIFNwYW5pZWwgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCa0LDQvdC1LdC60L7RgNGB0L4gNDDigJM2MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODV9LCJicmVlZF9lbiI6IkNhbmUgQ29yc28gNDDigJM2MCBrZyIsImJyZWVkX2V0IjoiQ2FuZSBDb3JzbyA0MOKAkzYwIGtnIn0seyJicmVlZCI6ItCa0LDQvdC1LdC60L7RgNGB0L4g0LHQvtC70LXQtSA2MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjkwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MTA1fSwiYnJlZWRfZW4iOiJDYW5lIENvcnNvIG92ZXIgNjAga2ciLCJicmVlZF9ldCI6IkNhbmUgQ29yc28gw7xsZSA2MCBrZyJ9LHsiYnJlZWQiOiLQmtCw0YDQtdC70L4t0YTQuNC90YHQutCw0Y8g0LvQsNC50LrQsCDQtNC+IDEzINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjY1fSwiYnJlZWRfZW4iOiJLYXJlbGlhbi1GaW5uaXNoIExhaWthIHVwIHRvIDEzIGtnIiwiYnJlZWRfZXQiOiJLYXJqYWxhLVNvb21lIGxhaWthIGt1bmkgMTMga2cifSx7ImJyZWVkIjoi0JrQuNGC0LDQudGB0LrQsNGPINGF0L7RhdC70LDRgtCw0Y8g0LPQvtC70LDRjyA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMiwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQyLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiQ2hpbmVzZSBDcmVzdGVkIGhhaXJsZXNzIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiSGlpbmEgaGFyamFrb2VyIGthcnZhdHUgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCa0LjRgtCw0LnRgdC60LDRjyDRhdC+0YXQu9Cw0YLQsNGPINCz0L7Qu9Cw0Y8g0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MjgsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjozNSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkNoaW5lc2UgQ3Jlc3RlZCBoYWlybGVzcyB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJIaWluYSBoYXJqYWtvZXIga2FydmF0dSBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JrQuNGC0LDQudGB0LrQsNGPINGF0L7RhdC70LDRgtCw0Y8g0L/Rg9GF0L7QstCw0Y8gNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkNoaW5lc2UgQ3Jlc3RlZCBwb3dkZXJwdWZmIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiSGlpbmEgaGFyamFrb2VyIFBvd2RlcnB1ZmYgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCa0LjRgtCw0LnRgdC60LDRjyDRhdC+0YXQu9Cw0YLQsNGPINC/0YPRhdC+0LLQsNGPINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJDaGluZXNlIENyZXN0ZWQgcG93ZGVycHVmZiB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJIaWluYSBoYXJqYWtvZXIgUG93ZGVycHVmZiBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JrQvtC60LDQv9GDIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjc1fSwiYnJlZWRfZW4iOiJDb2NrYXBvbyA14oCTMTAga2ciLCJicmVlZF9ldCI6IkNvY2thcG9vIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQmtC+0LrQsNC/0YMg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjV9LCJicmVlZF9lbiI6IkNvY2thcG9vIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IkNvY2thcG9vIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQmtC+0LvQu9C4IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQ29sbGllIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IktvbGwgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQmtC+0LvQu9C4IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IkNvbGxpZSAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJLb2xsIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JrQvtC80L7QvdC00L7RgCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTEwfSwiYnJlZWRfZW4iOiJLb21vbmRvciAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJLb21vbmRvciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCa0L7QvNC+0L3QtNC+0YAg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEzMH0sImJyZWVkX2VuIjoiS29tb25kb3Igb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiS29tb25kb3Igw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQm9Cw0LHRgNCw0LTQvtGAINCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdGL0LkgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo3MH0sImJyZWVkX2VuIjoiTGFicmFkb3IgUmV0cmlldmVyIHNtb290aCAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvcmkgcmV0cmlpdmVyIGzDvGhpa2FydmFsaW5lIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00L7RgCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3Ri9C5IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6ODB9LCJicmVlZF9lbiI6IkxhYnJhZG9yIFJldHJpZXZlciBzbW9vdGggMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiTGFicmFkb3JpIHJldHJpaXZlciBsw7xoaWthcnZhbGluZSAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNC+0YAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjk1fSwiYnJlZWRfZW4iOiJMYWJyYWRvciBSZXRyaWV2ZXIgc21vb3RoIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9yaSByZXRyaWl2ZXIgbMO8aGlrYXJ2YWxpbmUgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQm9Cw0LHRgNCw0LTQvtGAINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdGL0LkgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJMYWJyYWRvciBSZXRyaWV2ZXIgbG9uZy1jb2F0ZWQgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiTGFicmFkb3JpIHJldHJpaXZlciBwaWtrYXJ2YWxpbmUgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQm9Cw0LHRgNCw0LTQvtGAINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdGL0LkgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjExMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiTGFicmFkb3IgUmV0cmlldmVyIGxvbmctY29hdGVkIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9yaSByZXRyaWl2ZXIgcGlra2FydmFsaW5lIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00L7RgCDQtNC70LjQvdC90L7RiNC10YDRgdGC0L3Ri9C5INCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMzAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkxhYnJhZG9yIFJldHJpZXZlciBsb25nLWNvYXRlZCBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvcmkgcmV0cmlpdmVyIHBpa2thcnZhbGluZSDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNGD0LTQtdC70YwgMTDigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJMYWJyYWRvb2RsZSAxMOKAkzIwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvb2RsZSAxMOKAkzIwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNGD0LTQtdC70YwgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwfSwiYnJlZWRfZW4iOiJMYWJyYWRvb2RsZSAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvb2RsZSAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNGD0LTQtdC70YwgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMH0sImJyZWVkX2VuIjoiTGFicmFkb29kbGUgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiTGFicmFkb29kbGUgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQm9C10LLRgNC10YLQutCwIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDB9LCJicmVlZF9lbiI6Ikl0YWxpYW4gR3JleWhvdW5kIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiSXRhYWxpYSB2aW5ka29lciA14oCTMTAga2cifSx7ImJyZWVkIjoi0JvQtdCy0YDQtdGC0LrQsCDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1fSwiYnJlZWRfZW4iOiJJdGFsaWFuIEdyZXlob3VuZCB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJJdGFhbGlhIHZpbmRrb2VyIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQm9GF0LDRgdGB0LrQuNC5INCw0L/RgdC+IDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjc1fSwiYnJlZWRfZW4iOiJMaGFzYSBBcHNvIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiTGhhc2EgQXBzbyA14oCTMTAga2cifSx7ImJyZWVkIjoi0JvRhdCw0YHRgdC60LjQuSDQsNC/0YHQviDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2NX0sImJyZWVkX2VuIjoiTGhhc2EgQXBzbyB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJMaGFzYSBBcHNvIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQnNCw0LvRjNGC0LXQt9C1Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJNYWx0ZXNlIiwiYnJlZWRfZXQiOiJNYWx0YSBib2xvbmVlcyJ9LHsiYnJlZWQiOiLQnNCw0LvRjNGC0LjQudGB0LrQsNGPINCx0L7Qu9C+0L3QutCwIDXigJM4INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjV9LCJicmVlZF9lbiI6Ik1hbHRlc2UgQm9sb2duZXNlIDXigJM4IGtnIiwiYnJlZWRfZXQiOiJNYWx0YSBib2xvbmVlcyA14oCTOCBrZyJ9LHsiYnJlZWQiOiLQnNCw0LvRjNGC0LjQudGB0LrQsNGPINCx0L7Qu9C+0L3QutCwINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJNYWx0ZXNlIEJvbG9nbmVzZSB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJNYWx0YSBib2xvbmVlcyBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JzQsNC70YzRgtC40L/RgyAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6Ik1hbHRpcG9vIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6Ik1hbHRpcHV1IDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0JzQsNC70YzRgtC40L/RgyA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiTWFsdGlwb28gNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJNYWx0aXB1dSA14oCTMTAga2cifSx7ImJyZWVkIjoi0JzQsNC70YzRgtC40L/RgyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiTWFsdGlwb28gdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiTWFsdGlwdXUga3VuaSA1IGtnIn0seyJicmVlZCI6ItCc0LXRgtC40YEg0LrRgNGD0L/QvdGL0LkgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiTWl4ZWQgYnJlZWQgbGFyZ2UgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiU2VnYXZlcmQgc3V1ciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCc0LXRgtC40YEg0LrRgNGD0L/QvdGL0Lkg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjc1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEyMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEyMH0sImJyZWVkX2VuIjoiTWl4ZWQgYnJlZWQgbGFyZ2Ugb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiU2VnYXZlcmQgc3V1ciDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCc0LXRgtC40YEg0LzQtdC70LrQuNC5IDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJNaXhlZCBicmVlZCBzbWFsbCA14oCTMTAga2ciLCJicmVlZF9ldCI6IlNlZ2F2ZXJkIHbDpGlrZSA14oCTMTAga2cifSx7ImJyZWVkIjoi0JzQtdGC0LjRgSDQvNC10LvQutC40Lkg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MjUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjozNSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTB9LCJicmVlZF9lbiI6Ik1peGVkIGJyZWVkIHNtYWxsIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IlNlZ2F2ZXJkIHbDpGlrZSBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JzQtdGC0LjRgSDRgdGA0LXQtNC90LjQuSAxMOKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo3MH0sImJyZWVkX2VuIjoiTWl4ZWQgYnJlZWQgbWVkaXVtIDEw4oCTMjAga2ciLCJicmVlZF9ldCI6IlNlZ2F2ZXJkIGtlc2ttaW5lIDEw4oCTMjAga2cifSx7ImJyZWVkIjoi0JzQtdGC0LjRgSDRgdGA0LXQtNC90LjQuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4NX0sImJyZWVkX2VuIjoiTWl4ZWQgYnJlZWQgbWVkaXVtIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IlNlZ2F2ZXJkIGtlc2ttaW5lIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JzQuNGC0YLQtdC70YzRiNC90LDRg9GG0LXRgCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzAsItCi0YDQuNC80LzQuNC90LMiOjc1fSwiYnJlZWRfZW4iOiJTdGFuZGFyZCBTY2huYXV6ZXIgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiU3RhbmRhcmTFoW5hdXRzZXIgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQnNC40YLRgtC10LvRjNGI0L3QsNGD0YbQtdGAIDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4MCwi0KLRgNC40LzQvNC40L3QsyI6ODV9LCJicmVlZF9lbiI6IlN0YW5kYXJkIFNjaG5hdXplciAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiJTdGFuZGFyZMWhbmF1dHNlciAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCc0L7Qv9GBIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo1MH0sImJyZWVkX2VuIjoiUHVnIiwiYnJlZWRfZXQiOiJNb3BzIn0seyJicmVlZCI6ItCd0LXQstGB0LrQsNGPINC+0YDRhdC40LTQtdGPIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJOZXZhIE9yY2hpZCIsImJyZWVkX2V0IjoiTmVldmEgb3JoaWRlZSJ9LHsiYnJlZWQiOiLQndC10LzQtdGG0LrQsNGPINC+0LLRh9Cw0YDQutCwIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6Ikdlcm1hbiBTaGVwaGVyZCAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJTYWtzYSBsYW1iYWtvZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQndC10LzQtdGG0LrQsNGPINC+0LLRh9Cw0YDQutCwIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJHZXJtYW4gU2hlcGhlcmQgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiU2Frc2EgbGFtYmFrb2VyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0J3QtdC80LXRhtC60LDRjyDQvtCy0YfQsNGA0LrQsCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiR2VybWFuIFNoZXBoZXJkIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IlNha3NhIGxhbWJha29lciDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCo0LLQtdC50YbQsNGA0YHQutCw0Y8g0L7QstGH0LDRgNC60LAgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiU3dpc3MgU2hlcGhlcmQgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoixaB2ZWl0c2kgbGFtYmFrb2VyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0KjQstC10LnRhtCw0YDRgdC60LDRjyDQvtCy0YfQsNGA0LrQsCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiU3dpc3MgU2hlcGhlcmQgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoixaB2ZWl0c2kgbGFtYmFrb2VyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0KjQstC10LnRhtCw0YDRgdC60LDRjyDQvtCy0YfQsNGA0LrQsCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiU3dpc3MgU2hlcGhlcmQgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoixaB2ZWl0c2kgbGFtYmFrb2VyIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0J3QvtGA0LLQuNGHLdGC0LXRgNGM0LXRgCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MCwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6Ik5vcndpY2ggVGVycmllciIsImJyZWVkX2V0IjoiTm9yd2l0xaFpIHRlcmplciJ9LHsiYnJlZWQiOiLQndC+0YDRhNC+0LvQui3RgtC10YDRjNC10YAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJOb3Jmb2xrIFRlcnJpZXIiLCJicmVlZF9ldCI6Ik5vcmZvbGtpIHRlcmplciJ9LHsiYnJlZWQiOiLQndGM0Y7RhNCw0YPQvdC00LvQtdC90LQgNDDigJM2MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEzMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiTmV3Zm91bmRsYW5kIDQw4oCTNjAga2ciLCJicmVlZF9ldCI6Ik5ld2ZvdW5kbGFuZGkga29lciA0MOKAkzYwIGtnIn0seyJicmVlZCI6ItCd0YzRjtGE0LDRg9C90LTQu9C10L3QtCDQsdC+0LvQtdC1IDYwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MTAwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MTE1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxNTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMzB9LCJicmVlZF9lbiI6Ik5ld2ZvdW5kbGFuZCBvdmVyIDYwIGtnIiwiYnJlZWRfZXQiOiJOZXdmb3VuZGxhbmRpIGtvZXIgw7xsZSA2MCBrZyJ9LHsiYnJlZWQiOiLQn9Cw0L/QuNC50L7QvSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiUGFwaWxsb24iLCJicmVlZF9ldCI6IlBhcGlsbG9uIn0seyJicmVlZCI6ItCf0LXQutC40L3QtdGBIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJQZWtpbmdlc2UgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJQZWtpbmVzaSBrb2VyIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQn9C10LrQuNC90LXRgSDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiUGVraW5nZXNlIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IlBla2luZXNpIGtvZXIga3VuaSA1IGtnIn0seyJicmVlZCI6ItCf0YPQtNC10LvRjCDQsdC+0LvRjNGI0L7QuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTB9LCJicmVlZF9lbiI6IlN0YW5kYXJkIFBvb2RsZSAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJTdGFuZGFyZHB1dWRlbCAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCf0YPQtNC10LvRjCDQsdC+0LvRjNGI0L7QuSAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwfSwiYnJlZWRfZW4iOiJTdGFuZGFyZCBQb29kbGUgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiU3RhbmRhcmRwdXVkZWwgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQn9GD0LTQtdC70Ywg0LrQsNGA0LvQuNC60L7QstGL0LkgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6Ik1pbmlhdHVyZSBQb29kbGUgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJLw6TDpGJ1c3B1dWRlbCA14oCTMTAga2cifSx7ImJyZWVkIjoi0J/Rg9C00LXQu9GMINC80LDQu9GL0LkgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJTbWFsbCBQb29kbGUgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiVsOkaWtlIHB1dWRlbCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCf0YPQtNC10LvRjCDQvNCw0LvRi9C5IDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4MH0sImJyZWVkX2VuIjoiU21hbGwgUG9vZGxlIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IlbDpGlrZSBwdXVkZWwgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQn9GD0LTQtdC70Ywg0YLQvtC5INC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJUb3kgUG9vZGxlIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6Ik3DpG5ndWFzamEgcHV1ZGVsIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQoNC40LfQtdC90YjQvdCw0YPRhtC10YAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMCwi0KLRgNC40LzQvNC40L3QsyI6MTEwfSwiYnJlZWRfZW4iOiJHaWFudCBTY2huYXV6ZXIgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiU3V1csWhbmF1dHNlciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCg0LjQt9C10L3RiNC90LDRg9GG0LXRgCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTIwLCLQotGA0LjQvNC80LjQvdCzIjoxMjV9LCJicmVlZF9lbiI6IkdpYW50IFNjaG5hdXplciBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJTdXVyxaFuYXV0c2VyIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0KDRg9GB0YHQutCw0Y8g0YbQstC10YLQvdCw0Y8g0LHQvtC70L7QvdC60LAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IlJ1c3NpYW4gQ29sb3JlZCBMYXBkb2ciLCJicmVlZF9ldCI6IlZlbmUgdsOkcnZpbGluZSBzw7xsZWtvZXIifSx7ImJyZWVkIjoi0KDRg9GB0YHQutC40Lkg0L7RhdC+0YLQvdC40YfQuNC5INGB0L/QsNC90LjQtdC70YwgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJSdXNzaWFuIFNwYW5pZWwgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiVmVuZSBqYWhpc3BhbmplbCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCg0YPRgdGB0LrQuNC5INC+0YXQvtGC0L3QuNGH0LjQuSDRgdC/0LDQvdC40LXQu9GMIDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4MH0sImJyZWVkX2VuIjoiUnVzc2lhbiBTcGFuaWVsIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IlZlbmUgamFoaXNwYW5qZWwgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQoNGD0YHRgdC60LjQuSDRgtC+0Lkg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1fSwiYnJlZWRfZW4iOiJSdXNzaWFuIFRveSBzbW9vdGgiLCJicmVlZF9ldCI6IlZlbmUgVG95IGzDvGhpa2FydmFsaW5lIn0seyJicmVlZCI6ItCg0YPRgdGB0LrQuNC5INGC0L7QuSDQtNC70LjQvdC90L7RiNC10YDRgdGC0L3Ri9C5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJSdXNzaWFuIFRveSBsb25nLWNvYXRlZCIsImJyZWVkX2V0IjoiVmVuZSBUb3kgcGlra2FydmFsaW5lIn0seyJicmVlZCI6ItCg0YPRgdGB0LrQuNC5INGH0LXRgNC90YvQuSDRgtC10YDRjNC10YAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMH0sImJyZWVkX2VuIjoiQmxhY2sgUnVzc2lhbiBUZXJyaWVyIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6Ik11c3QgVmVuZSB0ZXJqZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQoNGD0YHRgdC60LjQuSDRh9C10YDQvdGL0Lkg0YLQtdGA0YzQtdGAINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMjB9LCJicmVlZF9lbiI6IkJsYWNrIFJ1c3NpYW4gVGVycmllciBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJNdXN0IFZlbmUgdGVyamVyIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0KDRg9GB0YHQutC+LdC10LLRgNC+0L/QtdC50YHQutCw0Y8g0LvQsNC50LrQsCAyMOKAkzI4INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJSdXNzaWFuLUV1cm9wZWFuIExhaWthIDIw4oCTMjgga2ciLCJicmVlZF9ldCI6IlZlbmUtRXVyb29wYSBsYWlrYSAyMOKAkzI4IGtnIn0seyJicmVlZCI6ItCh0LDQvNC+0LXQtCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJTYW1veWVkIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IlNhbW9qZWVkIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0KHQsNC80L7QtdC0IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJTYW1veWVkIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IlNhbW9qZWVkIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0KHQtdGC0YLQtdGAINCw0L3Qs9C70LjQudGB0LrQuNC5IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5MH0sImJyZWVkX2VuIjoiRW5nbGlzaCBTZXR0ZXIgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiSW5nbGlzZSBzZXR0ZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQodC10YLRgtC10YAg0LPQvtGA0LTQvtC9IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjgwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDB9LCJicmVlZF9lbiI6IkdvcmRvbiBTZXR0ZXIgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiR29yZG9uaSBzZXR0ZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQodC10YLRgtC10YAg0LjRgNC70LDQvdC00YHQutC40LkgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwfSwiYnJlZWRfZW4iOiJJcmlzaCBTZXR0ZXIgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiSWlyaSBzZXR0ZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQodC40LHQsC3QuNC90YMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjcwfSwiYnJlZWRfZW4iOiJTaGliYSBJbnUiLCJicmVlZF9ldCI6IlNoaWJhIEludSJ9LHsiYnJlZWQiOiLQodC40LvQuNGF0LXQvC3RgtC10YDRjNC10YAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJTZWFseWhhbSBUZXJyaWVyIiwiYnJlZWRfZXQiOiJTZWFseWhhbWkgdGVyamVyIn0seyJicmVlZCI6ItCh0LrQvtGC0Yct0YLQtdGA0YzQtdGAIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiU2NvdHRpc2ggVGVycmllciIsImJyZWVkX2V0IjoixaBvdGkgdGVyamVyIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90LDRjyDQutCw0YDQu9C40LrQvtCy0LDRjyA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NTB9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCBzbW9vdGggbWluaWF0dXJlIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiVGFrc2lrb2VyIGzDvGhpa2FydmFsaW5lIGvDpMOkYnVzIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdCw0Y8g0LrRgNC+0LvQuNGH0YzRjyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NDV9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCBzbW9vdGggcmFiYml0IHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBsw7xoaWthcnZhbGluZSBrw7zDvGxpayBrdW5pIDUga2cifSx7ImJyZWVkIjoi0KLQsNC60YHQsCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3QsNGPINGB0YLQsNC90LTQsNGA0YLQvdCw0Y8gMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2MH0sImJyZWVkX2VuIjoiRGFjaHNodW5kIHNtb290aCBzdGFuZGFyZCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIgbMO8aGlrYXJ2YWxpbmUgc3RhbmRhcmQgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdCw0Y8g0LrQsNGA0LvQuNC60L7QstCw0Y8gNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCBsb25nLWNvYXRlZCBtaW5pYXR1cmUgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIgcGlra2FydmFsaW5lIGvDpMOkYnVzIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdCw0Y8g0LrRgNC+0LvQuNGH0YzRjyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiRGFjaHNodW5kIGxvbmctY29hdGVkIHJhYmJpdCB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIgcGlra2FydmFsaW5lIGvDvMO8bGlrIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdCw0Y8g0YHRgtCw0L3QtNCw0YDRgtC90LDRjyAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCBsb25nLWNvYXRlZCBzdGFuZGFyZCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIgcGlra2FydmFsaW5lIHN0YW5kYXJkIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KLQsNC60YHQsCDQttC10YHRgtC60L7RiNC10YDRgdGC0L3QsNGPINC60LDRgNC70LjQutC+0LLQsNGPIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiRGFjaHNodW5kIHdpcmUtaGFpcmVkIG1pbmlhdHVyZSA14oCTMTAga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBrYXJ1a2FydmFsaW5lIGvDpMOkYnVzIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINC20LXRgdGC0LrQvtGI0LXRgNGB0YLQvdCw0Y8g0LrRgNC+0LvQuNGH0YzRjyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NSwi0KLRgNC40LzQvNC40L3QsyI6NTV9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCB3aXJlLWhhaXJlZCByYWJiaXQgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiVGFrc2lrb2VyIGthcnVrYXJ2YWxpbmUga8O8w7xsaWsga3VuaSA1IGtnIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90LDRjyDRgdGC0LDQvdC00LDRgNGC0L3QsNGPIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MCwi0KLRgNC40LzQvNC40L3QsyI6NzV9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCB3aXJlLWhhaXJlZCBzdGFuZGFyZCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIga2FydWthcnZhbGluZSBzdGFuZGFyZCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCj0LjQv9C/0LXRgiAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NX0sImJyZWVkX2VuIjoiV2hpcHBldCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJXaGlwcGV0IDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KPQuNC/0L/QtdGCIDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwfSwiYnJlZWRfZW4iOiJXaGlwcGV0IDE14oCTMjAga2ciLCJicmVlZF9ldCI6IldoaXBwZXQgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQpNC40L3RgdC60LjQuSDQu9Cw0L/RhdGD0L3QtCAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODV9LCJicmVlZF9lbiI6IkZpbm5pc2ggTGFwcGh1bmQgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiU29vbWUgbGFtYmFrb2VyIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0KTQuNC90YHQutC40Lkg0LvQsNC/0YXRg9C90LQgMjDigJMyNCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjU1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwfSwiYnJlZWRfZW4iOiJGaW5uaXNoIExhcHBodW5kIDIw4oCTMjQga2ciLCJicmVlZF9ldCI6IlNvb21lIGxhbWJha29lciAyMOKAkzI0IGtnIn0seyJicmVlZCI6ItCk0L7QutGB0YLQtdGA0YzQtdGAINC20LXRgdGC0LrQvtGI0LXRgNGB0YLQvdGL0LkgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwLCLQotGA0LjQvNC80LjQvdCzIjo3NX0sImJyZWVkX2VuIjoiV2lyZSBGb3ggVGVycmllciAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJLYXJ1a2FydmFsaW5lIGZveHRlcmplciAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCk0L7QutGB0YLQtdGA0YzQtdGAINC20LXRgdGC0LrQvtGI0LXRgNGB0YLQvdGL0LkgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJXaXJlIEZveCBUZXJyaWVyIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiS2FydWthcnZhbGluZSBmb3h0ZXJqZXIgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCk0YDQsNC90YbRg9C30YHQutC40Lkg0LHRg9C70YzQtNC+0LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjYwfSwiYnJlZWRfZW4iOiJGcmVuY2ggQnVsbGRvZyIsImJyZWVkX2V0IjoiUHJhbnRzdXNlIGJ1bGRvZyJ9LHsiYnJlZWQiOiLQpdCw0YHQutC4IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IlNpYmVyaWFuIEh1c2t5IDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IlNpYmVyaSBodXNreSAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCl0LDRgdC60LggMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IlNpYmVyaWFuIEh1c2t5IDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IlNpYmVyaSBodXNreSAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCm0LLQtdGA0LPRiNC90LDRg9GG0LXRgCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzAsItCi0YDQuNC80LzQuNC90LMiOjc1fSwiYnJlZWRfZW4iOiJNaW5pYXR1cmUgU2NobmF1emVyIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IkvDpMOkYnVzxaFuYXV0c2VyIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KbQstC10YDQs9GI0L3QsNGD0YbQtdGAIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiTWluaWF0dXJlIFNjaG5hdXplciA14oCTMTAga2ciLCJicmVlZF9ldCI6IkvDpMOkYnVzxaFuYXV0c2VyIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQp9Cw0YMt0YfQsNGDIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQ2hvdyBDaG93IDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkNob3cgQ2hvdyAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCn0LDRgy3Rh9Cw0YMgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjExMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiQ2hvdyBDaG93IDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkNob3cgQ2hvdyAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCn0LjRhdGD0LDRhdGD0LAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1fSwiYnJlZWRfZW4iOiJDaGlodWFodWEgc21vb3RoIiwiYnJlZWRfZXQiOiJUxaFpaHVhaHVhIGzDvGhpa2FydmFsaW5lIn0seyJicmVlZCI6ItCn0LjRhdGD0LDRhdGD0LAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90YvQuSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiQ2hpaHVhaHVhIGxvbmctY29hdGVkIiwiYnJlZWRfZXQiOiJUxaFpaHVhaHVhIHBpa2thcnZhbGluZSJ9LHsiYnJlZWQiOiLQqNCw0YDQv9C10LkgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2NX0sImJyZWVkX2VuIjoiU2hhciBQZWkgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoixaBhci1QZWkgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQqNCw0YDQv9C10LkgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo3MH0sImJyZWVkX2VuIjoiU2hhciBQZWkgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoixaBhci1QZWkgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQqNC10LvRgtC4Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjB9LCJicmVlZF9lbiI6IlNoZXRsYW5kIFNoZWVwZG9nIiwiYnJlZWRfZXQiOiLFoGV0bGFuZGkgbGFtYmFrb2VyIn0seyJicmVlZCI6ItCo0Lgt0YLRhtGDIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJTaGloIFR6dSA14oCTMTAga2ciLCJicmVlZF9ldCI6IlNoaWggVHp1IDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQqNC4LdGC0YbRgyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiU2hpaCBUenUgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiU2hpaCBUenUga3VuaSA1IGtnIn0seyJicmVlZCI6ItCo0L3QsNGD0YbQtdGAINC80LjQvdC40LDRgtGO0YDQvdGL0Lkg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6Ik1pbmlhdHVyZSBTY2huYXV6ZXIgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiS8Okw6RidXPFoW5hdXRzZXIga3VuaSA1IGtnIn0seyJicmVlZCI6ItCo0L/QuNGGINC90LXQvNC10YbQutC40LkgLyDQv9C+0LzQtdGA0LDQvdGB0LrQuNC5INCx0L7Qu9C10LUgMyw1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2MH0sImJyZWVkX2VuIjoiR2VybWFuIFNwaXR6IC8gUG9tZXJhbmlhbiBvdmVyIDMsNSBrZyIsImJyZWVkX2V0IjoiU2Frc2Egc3BpdHMgLyBQb21lcmFuaWFuIMO8bGUgMyw1IGtnIn0seyJicmVlZCI6ItCo0L/QuNGGINC90LXQvNC10YbQutC40LkgLyDQv9C+0LzQtdGA0LDQvdGB0LrQuNC5INC00L4gMyw1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo1NX0sImJyZWVkX2VuIjoiR2VybWFuIFNwaXR6IC8gUG9tZXJhbmlhbiB1cCB0byAzLDUga2ciLCJicmVlZF9ldCI6IlNha3NhIHNwaXRzIC8gUG9tZXJhbmlhbiBrdW5pIDMsNSBrZyJ9LHsiYnJlZWQiOiLQqNC/0LjRhiDRj9C/0L7QvdGB0LrQuNC5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjB9LCJicmVlZF9lbiI6IkphcGFuZXNlIFNwaXR6IiwiYnJlZWRfZXQiOiJKYWFwYW5pIHNwaXRzIn0seyJicmVlZCI6ItCp0LXQvdC60LgiLCJzZXJ2aWNlcyI6eyLQktGB0Y8g0L/RgNC+0LPRgNCw0LzQvNCwIjo1NX0sImJyZWVkX2VuIjoiUHVwcGllcyIsImJyZWVkX2V0IjoiS3V0c2lrYWQifSx7ImJyZWVkIjoi0K3RgdGC0L7QvdGB0LrQsNGPINCz0L7QvdGH0LDRjyAxNeKAkzI1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MH0sImJyZWVkX2VuIjoiRXN0b25pYW4gSG91bmQgMTXigJMyNSBrZyIsImJyZWVkX2V0IjoiRWVzdGkgaGFnaWphcyAxNeKAkzI1IGtnIn0seyJicmVlZCI6ItCv0L/QvtC90YHQutC40Lkg0YXQuNC9Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJKYXBhbmVzZSBDaGluIiwiYnJlZWRfZXQiOiJKYWFwYW5pIENoaW4ifSx7ImJyZWVkIjoi0JrQvtGI0LrQsCDQutC+0YDQvtGC0LrQvtGI0LXRgNGB0YLQvdCw0Y8iLCJzZXJ2aWNlcyI6eyLQktGL0YfQtdGBIjo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkNhdCBzaG9ydC1oYWlyZWQiLCJicmVlZF9ldCI6Ikthc3MgbMO8aGlrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0JrQvtGI0LrQsCDQtNC70LjQvdC90L7RiNC10YDRgdGC0L3QsNGPIiwic2VydmljZXMiOnsi0JLRi9GH0LXRgSI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwfSwiYnJlZWRfZW4iOiJDYXQgbG9uZy1oYWlyZWQiLCJicmVlZF9ldCI6Ikthc3MgcGlra2FydmFsaW5lIn0seyJicmVlZCI6ItCa0L7RiNC60LAg0JzQtdC50L0t0LrRg9C9Iiwic2VydmljZXMiOnsi0JLRi9GH0ZHRgSI6NjAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwfSwiYnJlZWRfZW4iOiJDYXQgTWFpbmUgQ29vbiIsImJyZWVkX2V0IjoiS2FzcyBNYWluZSBDb29uIn1dOwp2YXIgUkFJTFdBWSA9ICJodHRwczovL3JqZ3Jvb21pbmcudXAucmFpbHdheS5hcHAvYm9vayI7CnZhciBHT09HTEVfU0NSSVBUID0gImh0dHBzOi8vc2NyaXB0Lmdvb2dsZS5jb20vbWFjcm9zL3MvQUtmeWNieVRTWi1lSk1kZXAtRDBMci1ueDBfVjRIQldnSUljdG5SVDJyalNEdkJ5Ymo1Q1lJM05LMk1xY0F3X2NmY3pnUkVpZmcvZXhlYyI7CnZhciBGQUxMQkFDS19USU1FUyA9IFsnMTA6MDAnLCcxMDozMCcsJzExOjAwJywnMTE6MzAnLCcxMjowMCcsJzEyOjMwJywnMTM6MDAnLCcxMzozMCcsJzE0OjAwJywnMTQ6MzAnLCcxNTowMCcsJzE1OjMwJywnMTY6MDAnLCcxNjozMCcsJzE3OjAwJywnMTc6MzAnLCcxODowMCddOwp2YXIgYm9va2luZyA9IHticmVlZDonJyxicmVlZERpc3BsYXk6Jycsc2VydmljZTonJyxwcmljZTowLG1hc3RlcjonJyxncm9vbUhpc3Rvcnk6JycsZGF0ZTonJyx0aW1lOicnLGxhbmc6J3J1J307CnZhciBzZWxCcmVlZCA9IG51bGw7CnZhciBjWSA9IG5ldyBEYXRlKCkuZ2V0RnVsbFllYXIoKTsKdmFyIGNNID0gbmV3IERhdGUoKS5nZXRNb250aCgpOwp2YXIgc3RlcCA9IDE7CnZhciBNT05USFMgPSBbJ9Cv0L3QstCw0YDRjCcsJ9Ck0LXQstGA0LDQu9GMJywn0JzQsNGA0YInLCfQkNC/0YDQtdC70YwnLCfQnNCw0LknLCfQmNGO0L3RjCcsJ9CY0Y7Qu9GMJywn0JDQstCz0YPRgdGCJywn0KHQtdC90YLRj9Cx0YDRjCcsJ9Ce0LrRgtGP0LHRgNGMJywn0J3QvtGP0LHRgNGMJywn0JTQtdC60LDQsdGA0YwnXTsKCmZ1bmN0aW9uIHNob3dTY3JlZW4oaWQpIHsKICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuc2NyZWVuJykuZm9yRWFjaChmdW5jdGlvbihzKXtzLmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpO30pOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKGlkKS5jbGFzc0xpc3QuYWRkKCdhY3RpdmUnKTsKICB3aW5kb3cuc2Nyb2xsVG8oMCwwKTsKfQoKZnVuY3Rpb24gZ29TdGVwKG4pIHsKICBbJ2JrMScsJ2JrMicsJ2JrMycsJ2JrNCcsJ2JrNSddLmZvckVhY2goZnVuY3Rpb24oaWQsaSl7CiAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZChpZCkuY2xhc3NOYW1lID0gJ3N0ZXAnICsgKGkrMT09PW4/JyBzaG93JzonJyk7CiAgfSk7CiAgZm9yKHZhciBpPTE7aTw9NTtpKyspewogICAgdmFyIHBzPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdwcycraSk7CiAgICB2YXIgcGw9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3BsJytpKTsKICAgIGlmKGk8bil7cHMuY2xhc3NOYW1lPSdwcyBkb25lJztpZihwbClwbC5jbGFzc05hbWU9J3BsIGRvbmUnO30KICAgIGVsc2UgaWYoaT09PW4pe3BzLmNsYXNzTmFtZT0ncHMgYWN0aXZlJztpZihwbClwbC5jbGFzc05hbWU9J3BsJzt9CiAgICBlbHNle3BzLmNsYXNzTmFtZT0ncHMnO2lmKHBsKXBsLmNsYXNzTmFtZT0ncGwnO30KICB9CiAgc3RlcD1uOyB3aW5kb3cuc2Nyb2xsVG8oMCwwKTsKICBpZihuPT09MikgZmlsdGVyTWFzdGVycygpOwp9Cgpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnYm9va0J0bicpLm9uY2xpY2sgPSBmdW5jdGlvbigpewogIHNob3dTY3JlZW4oJ2Jvb2tTY3JlZW4nKTsgZ29TdGVwKDEpOyBidWlsZENhbCgpOwp9Owpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnYmFja0J0bicpLm9uY2xpY2sgPSBmdW5jdGlvbigpewogIGlmKHN0ZXA+MSl7Z29TdGVwKHN0ZXAtMSk7fWVsc2V7c2hvd1NjcmVlbignaG9tZVNjcmVlbicpO30KfTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2hvbWVCdG4nKS5vbmNsaWNrID0gZnVuY3Rpb24oKXsKICBzaG93U2NyZWVuKCdob21lU2NyZWVuJyk7IHJlc2V0QWxsKCk7Cn07CgovLyBCcmVlZCBzZWFyY2gKdmFyIGlucCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdiSW5wdXQnKTsKdmFyIGRyb3AgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnYkRyb3AnKTsKdmFyIGNsciA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjbHJCdG4nKTsKdmFyIGJhZGdlID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3NCYWRnZScpOwoKaW5wLmFkZEV2ZW50TGlzdGVuZXIoJ2lucHV0JywgZnVuY3Rpb24oKXsKICB2YXIgcSA9IGlucC52YWx1ZS50cmltKCk7CiAgY2xyLmNsYXNzTGlzdC50b2dnbGUoJ3Nob3cnLCBxLmxlbmd0aD4wKTsKICBpZighcSl7ZHJvcC5jbGFzc0xpc3QucmVtb3ZlKCdvcGVuJyk7ZHJvcC5pbm5lckhUTUw9Jyc7cmV0dXJuO30KICB2YXIgc2Y9TEFORz09PSdlbic/J2JyZWVkX2VuJzpMQU5HPT09J2V0Jz8nYnJlZWRfZXQnOidicmVlZCc7CiAgdmFyIHJlcz1EQVRBLmZpbHRlcihmdW5jdGlvbihiKXtyZXR1cm4oYltzZl18fGIuYnJlZWQpLnRvTG93ZXJDYXNlKCkuaW5kZXhPZihxLnRvTG93ZXJDYXNlKCkpIT09LTE7fSkuc2xpY2UoMCwzNSk7CiAgZHJvcC5pbm5lckhUTUw9Jyc7CiAgdmFyIF9ucj1MQU5HPT09J2VuJz8nQnJlZWQgbm90IGZvdW5kJzpMQU5HPT09J2V0Jz8nVMO1dWd1IGVpIGxlaXR1ZCc6J9Cf0L7RgNC+0LTQsCDQvdC1INC90LDQudC00LXQvdCwJzsKICB2YXIgX250PUxBTkc9PT0nZW4nPyJDYW4ndCBmaW5kIHlvdXIgYnJlZWQ/IjpMQU5HPT09J2V0Jz8nRWkgbGVpYSBvbWEgdMO1dWd1Pyc6J9Cd0LUg0L3QsNGI0LvQuCDRgdCy0L7RjiDQv9C+0YDQvtC00YM/JzsKICB2YXIgX25zPUxBTkc9PT0nZW4nPydDb250YWN0IHVzIOKAlCB3ZSB3aWxsIGhlbHAgeW91IGNob29zZSBhIHNlcnZpY2UnOkxBTkc9PT0nZXQnPydWw7V0a2UgbWVpZWdhIMO8aGVuZHVzdCDigJQgYWl0YW1lIHRlZW51c2UgdmFsaWRhJzon0KHQstGP0LbQuNGC0LXRgdGMINGBINC90LDQvNC4INC70Y7QsdGL0Lwg0YPQtNC+0LHQvdGL0Lwg0YHQv9C+0YHQvtCx0L7QvCDigJQg0LzRiyDQv9C+0LzQvtC20LXQvCDQv9C+0LTQvtCx0YDQsNGC0Ywg0YPRgdC70YPQs9GDJzsKICBpZighcmVzLmxlbmd0aCl7ZHJvcC5pbm5lckhUTUw9JzxkaXYgY2xhc3M9Im5vcmVzIj4nK19ucisnPC9kaXY+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyIiBvbmNsaWNrPSJzaG93U2NyZWVuKFwnaG9tZVNjcmVlblwnKSI+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLWljb24iPvCfkL48L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItdGV4dCI+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLXRpdGxlIj4nK19udCsnPC9kaXY+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLXN1YiI+JytfbnMrJzwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci1hcnJvdyI+4oaSPC9kaXY+PC9kaXY+Jzt9CiAgZWxzZXsKICAgIHJlcy5mb3JFYWNoKGZ1bmN0aW9uKGIpewogICAgICB2YXIgZD1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdkaXYnKTsgZC5jbGFzc05hbWU9J2RpdGVtJzsKICAgICAgdmFyIGJuYW1lPWJbc2ZdfHxiLmJyZWVkOwogICAgICB2YXIgaWR4PWJuYW1lLnRvTG93ZXJDYXNlKCkuaW5kZXhPZihxLnRvTG93ZXJDYXNlKCkpOwogICAgICBkLmlubmVySFRNTD1ibmFtZS5zdWJzdHJpbmcoMCxpZHgpKyc8bWFyaz4nK2JuYW1lLnN1YnN0cmluZyhpZHgsaWR4K3EubGVuZ3RoKSsnPC9tYXJrPicrYm5hbWUuc3Vic3RyaW5nKGlkeCtxLmxlbmd0aCk7CiAgICAgIGQub25jbGljaz1mdW5jdGlvbigpe3NlbGVjdEJyZWVkKGIpO307CiAgICAgIGRyb3AuYXBwZW5kQ2hpbGQoZCk7CiAgICB9KTsKICB9CiAgZHJvcC5jbGFzc0xpc3QuYWRkKCdvcGVuJyk7Cn0pOwoKZG9jdW1lbnQuYWRkRXZlbnRMaXN0ZW5lcignY2xpY2snLGZ1bmN0aW9uKGUpewogIGlmKCFlLnRhcmdldC5jbG9zZXN0KCcuYndyYXAnKSlkcm9wLmNsYXNzTGlzdC5yZW1vdmUoJ29wZW4nKTsKfSk7CmNsci5vbmNsaWNrID0gcmVzZXRCcmVlZDsKCmZ1bmN0aW9uIHNlbGVjdEJyZWVkKGIpewogIHNlbEJyZWVkPWI7IGJvb2tpbmcuYnJlZWQ9Yi5icmVlZDsKICBpbnAudmFsdWU9Jyc7IGNsci5jbGFzc0xpc3QucmVtb3ZlKCdzaG93Jyk7CiAgZHJvcC5jbGFzc0xpc3QucmVtb3ZlKCdvcGVuJyk7IGRyb3AuaW5uZXJIVE1MPScnOwogIGJhZGdlLmlubmVySFRNTD0nJzsKICB2YXIgYkZpZWxkPUxBTkc9PT0nZW4nPydicmVlZF9lbic6TEFORz09PSdldCc/J2JyZWVkX2V0JzonYnJlZWQnOwogIHZhciBkaXNwQnJlZWQ9YltiRmllbGRdfHxiLmJyZWVkOwogIGJvb2tpbmcuYnJlZWREaXNwbGF5PWRpc3BCcmVlZDsKICB2YXIgYm49ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnc3BhbicpO2JuLmNsYXNzTmFtZT0nYm5hbWUnO2JuLnRleHRDb250ZW50PWRpc3BCcmVlZDsKICB2YXIgY2hnVHh0PUxBTkc9PT0nZW4nPydDaGFuZ2UnOkxBTkc9PT0nZXQnPydNdXVkYSc6J9CY0LfQvNC10L3QuNGC0YwnOwogIHZhciBiYz1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdzcGFuJyk7YmMuY2xhc3NOYW1lPSdiY2hnJztiYy50ZXh0Q29udGVudD1jaGdUeHQ7CiAgYmMub25jbGljaz1yZXNldEJyZWVkOwogIGJhZGdlLmFwcGVuZENoaWxkKGJuKTtiYWRnZS5hcHBlbmRDaGlsZChiYyk7CiAgYmFkZ2UuY2xhc3NMaXN0LmFkZCgnc2hvdycpOwogIHJlbmRlclN2Y3MoYik7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y1NlYycpLnN0eWxlLmRpc3BsYXk9J2Jsb2NrJzsKICAgIC8vIEFkZCBpbXBvcnRhbnQgbm90ZSBpZiBub3QgZXhpc3RzCiAgICBpZighZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y05vdGUnKSl7CiAgICAgIHZhciBub3RlPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpOwogICAgICBub3RlLmlkPSdzdmNOb3RlJzsKICAgICAgbm90ZS5zdHlsZS5jc3NUZXh0PSdib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA4KTtwYWRkaW5nOjE0cHggMTZweDtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjAyKTttYXJnaW4tdG9wOjEycHg7JzsKICAgICAgdmFyIG5vdGVUaXRsZT1MQU5HPT09J2VuJz8nUGxlYXNlIG5vdGUnOkxBTkc9PT0nZXQnPydQYW5nZSB0w6RoZWxlJzon0JLQsNC20L3QviDQt9C90LDRgtGMJzsKICAgICAgdmFyIG5vdGVCb2R5PUxBTkc9PT0nZW4nPydGaW5hbCBwcmljZSBkZXBlbmRzIG9uIGNvYXQgY29uZGl0aW9uIGFuZCBwZXQgYmVoYXZpb3VyLjxicj5EZW1hdHRpbmcgZnJvbSA1IOKCrC48YnI+QWdncmVzc2l2ZSBiZWhhdmlvdXIgc3VyY2hhcmdlIG1heSBhcHBseTogKzUwJS4nOkxBTkc9PT0nZXQnPydMw7VwbGlrIGhpbmQgc8O1bHR1YiBrYXJ2YXN0aWt1IHNlaXN1bmRpc3QgamEgbGVtbWlrbG9vbWEga8OkaXR1bWlzZXN0Ljxicj5Lb2x0c3VuaXRlIGxhaHRpaGFydXRhbWluZSBhbGF0ZXMgNSDigqwuPGJyPkFncmVzc2lpdnNlIGvDpGl0dW1pc2Uga29ycmFsIHbDtWliIGxpc2FuZHVkYSA1MCUganV1cmRlaGluZGx1cy4nOifQntC60L7QvdGH0LDRgtC10LvRjNC90LDRjyDRgdGC0L7QuNC80L7RgdGC0Ywg0LfQsNCy0LjRgdC40YIg0L7RgiDRgdC+0YHRgtC+0Y/QvdC40Y8g0YjQtdGA0YHRgtC4INC4INC/0L7QstC10LTQtdC90LjRjyDQv9C40YLQvtC80YbQsC48YnI+0KDQsNC30LHQvtGAINC60L7Qu9GC0YPQvdC+0LIg4oCUINC+0YIgNSDigqwuPGJyPtCf0YDQuCDQsNCz0YDQtdGB0YHQuNCy0L3QvtC8INC/0L7QstC10LTQtdC90LjQuCDQvNC+0LbQtdGCINC/0YDQuNC80LXQvdGP0YLRjNGB0Y8g0LTQvtC/0LvQsNGC0LAgNTAlLic7CiAgICAgIG5vdGUuaW5uZXJIVE1MPSc8ZGl2IHN0eWxlPSJmb250LXNpemU6MC44MzhyZW07bGV0dGVyLXNwYWNpbmc6LjE1ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbTo4cHg7Zm9udC13ZWlnaHQ6NjAwO2ZvbnQtZmFtaWx5OlwnTW9udHNlcnJhdFwnLHNhbnMtc2VyaWYiPicrbm90ZVRpdGxlKyc8L2Rpdj48ZGl2IHN0eWxlPSJmb250LXNpemU6MS4wMjVyZW07Y29sb3I6I2ZmZmZmZjtsaW5lLWhlaWdodDoxLjg7Zm9udC1mYW1pbHk6XCdNb250c2VycmF0XCcsc2Fucy1zZXJpZiI+Jytub3RlQm9keSsnPC9kaXY+JzsKICAgICAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y1NlYycpLmFwcGVuZENoaWxkKG5vdGUpOwogICAgfQogIGZpbHRlck1hc3RlcnMoKTsKfQoKZnVuY3Rpb24gcmVzZXRCcmVlZCgpewogIHNlbEJyZWVkPW51bGw7Ym9va2luZy5icmVlZD0nJztib29raW5nLnNlcnZpY2U9Jyc7Ym9va2luZy5wcmljZT0wOwogIGlucC52YWx1ZT0nJztjbHIuY2xhc3NMaXN0LnJlbW92ZSgnc2hvdycpOwogIGJhZGdlLmNsYXNzTGlzdC5yZW1vdmUoJ3Nob3cnKTtiYWRnZS5pbm5lckhUTUw9Jyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y1NlYycpLnN0eWxlLmRpc3BsYXk9J25vbmUnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdmNMaXN0JykuaW5uZXJIVE1MPScnOwp9CgoKdmFyIFNWQ19UUkFOU0xBVElPTlMgPSB7CiAgJ9CR0LDQt9C+0LLRi9C5INGD0YXQvtC0JzogICAgICB7ZW46J0Jhc2ljIGdyb29tJywgICAgICBldDonUMO1aGlob29sZHVzJ30sCiAgJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Jzp7ZW46J0h5Z2llbmUgZ3Jvb20nLCAgICBldDonSMO8Z2llZW5paG9vbGR1cyd9LAogICfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0JzogIHtlbjonRnVsbCBncm9vbScsICAgICAgICBldDonVMOkaWVsaWsgaG9vbGR1cyd9LAogICfQotGA0LjQvNC80LjQvdCzJzogICAgICAgICAge2VuOidUcmltbWluZycsICAgICAgICAgIGV0OidUcmltbWVyaW1pbmUnfSwKICAn0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAnOiAgIHtlbjonRXhwcmVzcyBzaGVkJywgICAgICBldDonS2lpcmthcnZhdmFoZXR1cyd9LAogICfQktGL0YfQtdGBJzogICAgICAgICAgICAge2VuOidCcnVzaC1vdXQnLCAgICAgICAgIGV0OidIYXJqYW1pbmUnfSwKICAn0JLRgdGPINC/0YDQvtCz0YDQsNC80LzQsCc6ICAgICB7ZW46J0Z1bGwgcHJvZ3JhbScsICAgICAgZXQ6J0tvZ3UgcHJvZ3JhbW0nfQp9Owp2YXIgU1ZDX1RBR0xJTkVfSTE4Tj17CiAgcnU6eyfQktGL0YfQtdGBJzon0KHRgtC+0LjQvNC+0YHRgtGMINC30LDQstC40YHQuNGCINC+0YIg0YHQvtGB0YLQvtGP0L3QuNGPINGI0LXRgNGB0YLQuCDQuCDQvtCx0YrRkdC80LAg0YDQsNCx0L7RgicsJ9CR0LDQt9C+0LLRi9C5INGD0YXQvtC0Jzon0J/QvtC00YXQvtC00LjRgiDQtNC70Y8g0L/QvtC00LTQtdGA0LbQsNC90LjRjyDRh9C40YHRgtC+0YLRiyDQvNC10LbQtNGDINC/0YDQvtGG0LXQtNGD0YDQsNC80LgnLCfQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCc6J9CU0LvRjyDQutC+0LzRhNC+0YDRgtCwINC4INCw0LrQutGD0YDQsNGC0L3QvtGB0YLQuCDQv9C40YLQvtC80YbQsCcsJ9Ca0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQnOifQn9C+0LvQvdGL0Lkg0YPRhdC+0LQg0YHQviDRgdGC0YDQuNC20LrQvtC5Jywn0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAnOifQn9C+0LzQvtCz0LDQtdGCINGD0LzQtdC90YzRiNC40YLRjCDQutC+0LvQuNGH0LXRgdGC0LLQviDQu9C40L3Rj9GO0YnQtdC5INGI0LXRgNGB0YLQuCcsJ9Ci0YDQuNC80LzQuNC90LMnOifQlNC70Y8g0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90YvRhSDQv9C+0YDQvtC0J30sCiAgZW46eyfQktGL0YfQtdGBJzonUHJpY2UgZGVwZW5kcyBvbiBjb2F0IGNvbmRpdGlvbiBhbmQgdm9sdW1lIG9mIHdvcmsnLCfQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCc6J0lkZWFsIGZvciBtYWludGFpbmluZyBjbGVhbmxpbmVzcyBiZXR3ZWVuIGZ1bGwgZ3Jvb21zJywn0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQnOidGb3IgeW91ciBwZXRcJ3MgY29tZm9ydCBhbmQgbmVhdG5lc3MnLCfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0JzonRnVsbCBncm9vbWluZyB3aXRoIGhhaXJjdXQnLCfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6J1NpZ25pZmljYW50bHkgcmVkdWNlcyBzaGVkZGluZycsJ9Ci0YDQuNC80LzQuNC90LMnOidGb3Igd2lyZS1oYWlyZWQgYnJlZWRzJ30sCiAgZXQ6eyfQktGL0YfQtdGBJzonSGluZCBzw7VsdHViIGthcnZhc3Rpa3Ugc2Vpc3VuZGlzdCBqYSB0w7bDtm1haHVzdCcsJ9CR0LDQt9C+0LLRi9C5INGD0YXQvtC0JzonU29iaWIgcHVodHVzZSBob2lkbWlzZWtzIHByb3RzZWR1dXJpZGUgdmFoZWwnLCfQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCc6J0xlbW1pa2xvb21hIG11Z2F2dXNla3MgamEga29ycmFzaG9pdWtzJywn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc6J1TDpGllbGlrIGhvb2xkdXMga29vcyBsw7Vpa3VzZWdhJywn0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAnOidWw6RoZW5kYWIgb2x1bGlzZWx0IGthcnZhZGUgbGFuZ2VtaXN0Jywn0KLRgNC40LzQvNC40L3Qsyc6J1RyYWF0a2FydmFsaXN0ZWxlIHTDtXVndWRlbGUnfQp9Owp2YXIgU1ZDX0RFU0NfSTE4Tj17CiAgcnU6eyfQktGL0YfQtdGBJzon0KfQuNGB0YLQutCwINCz0LvQsNC3LCDRg9GI0LXQuSwg0L/QvtC00YHRgtGA0LjQs9Cw0L3QuNC1INC60L7Qs9GC0LXQuSwg0LLRi9GH0ZHRgSAo0LTQu9GPINC60L7RiNC10LopJywn0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQnOifQnNGL0YLRjNGRINC/0YDQvtGE0LXRgdGB0LjQvtC90LDQu9GM0L3Ri9C80Lgg0YHRgNC10LTRgdGC0LLQsNC80LgsINC00LXQu9C40LrQsNGC0L3QsNGPINGB0YPRiNC60LAnLCfQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCc6J9Ch0YLRgNC40LbQutCwINC60L7Qs9GC0LXQuSwg0YfQuNGB0YLQutCwINGD0YjQtdC5INC4INCz0LvQsNC3LCDQutGD0L/QsNC90LjQtSwg0YHRg9GI0LrQsCwg0YPRhdC+0LQg0LfQsCDQu9Cw0L/QutCw0LzQuCDQuCDRh9GD0LLRgdGC0LLQuNGC0LXQu9GM0L3Ri9C80Lgg0LfQvtC90LDQvNC4Jywn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc6J9Ch0YLRgNC40LbQutCwINC60L7Qs9GC0LXQuSwg0YfQuNGB0YLQutCwINGD0YjQtdC5INC4INCz0LvQsNC3LCDQutGD0L/QsNC90LjQtSwg0YHRg9GI0LrQsCwg0YPRhdC+0LQg0LfQsCDQu9Cw0L/QutCw0LzQuCDQuCDRh9GD0LLRgdGC0LLQuNGC0LXQu9GM0L3Ri9C80Lgg0LfQvtC90LDQvNC4LCDQvNC+0LTQtdC70YzQvdCw0Y8g0YHRgtGA0LjQttC60LAnLCfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6J9Cc0YvRgtGM0ZEsINGB0YPRiNC60LAsINGD0YXQvtC0INC30LAg0YjQtdGA0YHRgtGM0Y4sINC80LDRgdC60LAsINC/0L7QtNGB0YLRgNC40LPQsNC90LjQtSDQutC+0LPRgtC10LksINGH0LjRgdGC0LrQsCDRg9GI0LXQuSDQuCDQs9C70LDQtywg0YPRhdC+0LQg0LfQsCDQu9Cw0L/QsNC80Lgg0Lgg0LfQvtC90LDQvNC4INGC0YDQtdCx0YPRjtGJ0LjQvNC4INC+0YHQvtCx0L7Qs9C+INCy0L3QuNC80LDQvdC40Y8nLCfQotGA0LjQvNC80LjQvdCzJzon0JLRi9GJ0LjQv9GL0LLQsNC90LjQtSDRgdGC0LDRgNC+0LPQviDRgdC70L7RjyDRiNC10YDRgdGC0LgsINC80YvRgtGM0ZEsINGB0YPRiNC60LAsINGB0YLRgNC40LbQutCwINC60L7Qs9GC0LXQuSwg0YfQuNGB0YLQutCwINGD0YjQtdC5INC4INCz0LvQsNC3LCDQvtGE0L7RgNC80LvQtdC90LjQtSDRiNC10YDRgdGC0LgnLCfQktGB0Y8g0L/RgNC+0LPRgNCw0LzQvNCwJzon0J/QldCg0JLQq9CZINCS0JjQl9CY0KIgKDIwLTMwINC80LjQvSkg4oCUIDIwIOKCrFxu4oCiINC30L3QsNC60L7QvNGB0YLQstC+INGB0L4g0YHRgtC+0LvQvtC8INC4INC40L3RgdGC0YDRg9C80LXQvdGC0LDQvNC4XG7igKIg0LvRkdCz0LrQvtC1INCy0YvRh9GR0YHRi9Cy0LDQvdC40LVcbuKAoiDQt9Cy0YPQutC4INGE0LXQvdCwINC4INC70LXQs9C60LDRjyDQv9GA0L7QtNGD0LLQutCwXG7igKIg0L7RgdCy0LXQttC10L3QuNC1INCz0LvQsNC30L7QuiDQuCDRg9GI0LXQulxu4oCiINC60L7Qs9C+0YLQutC4XG7igKIg0LLQutGD0YHQvdGP0YjQutC4INC4INGB0L/QvtC60L7QudC90LDRjyDQsNC00LDQv9GC0LDRhtC40Y9cblxu0JLQotCe0KDQntCZINCS0JjQl9CY0KIgKDQwLTYwINC80LjQvSkg4oCUIDM1IOKCrFxu4oCiINC/0LXRgNCy0L7QtSDQutGD0L/QsNC90LjQtSDQuCDRgdGD0YjQutCwXG7igKIg0LLRi9GH0ZHRgdGL0LLQsNC90LjQtVxu4oCiINCz0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0XG7igKIg0L3QtdCx0L7Qu9GM0YjQsNGPINGB0YLRgNC40LbQutCwIC8g0LrQvtGA0YDQtdC60YbQuNGPINGI0LXRgNGB0YLQuCAo0L/RgNC4INC90LXQvtCx0YXQvtC00LjQvNC+0YHRgtC4KVxu4oCiINC30LDQutGA0LXQv9C70LXQvdC40LUg0L/QvtC70L7QttC40YLQtdC70YzQvdC+0LPQviDQvtC/0YvRgtCwJ30sCiAgZW46eyfQktGL0YfQtdGBJzonRXllIGFuZCBlYXIgY2xlYW5pbmcsIG5haWwgdHJpbW1pbmcsIGJydXNoaW5nIChmb3IgY2F0cyknLCfQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCc6J1dhc2hpbmcgd2l0aCBwcm9mZXNzaW9uYWwgcHJvZHVjdHMsIGdlbnRsZSBkcnlpbmcnLCfQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCc6J05haWwgdHJpbW1pbmcsIGVhciBhbmQgZXllIGNsZWFuaW5nLCBiYXRoaW5nLCBkcnlpbmcsIHBhdyBhbmQgc2Vuc2l0aXZlIGFyZWEgY2FyZScsJ9Ca0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQnOidOYWlsIHRyaW1taW5nLCBlYXIgYW5kIGV5ZSBjbGVhbmluZywgYmF0aGluZywgZHJ5aW5nLCBwYXcgYW5kIHNlbnNpdGl2ZSBhcmVhIGNhcmUsIHN0eWxpbmcgaGFpcmN1dCcsJ9Ct0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwJzonV2FzaGluZywgZHJ5aW5nLCBjb2F0IGNhcmUsIG1hc2ssIG5haWwgdHJpbW1pbmcsIGVhciBhbmQgZXllIGNsZWFuaW5nLCBwYXcgYW5kIHNwZWNpYWwgYXJlYSBjYXJlJywn0KLRgNC40LzQvNC40L3Qsyc6J1JlbW92aW5nIG9sZCBjb2F0IGxheWVyLCB3YXNoaW5nLCBkcnlpbmcsIG5haWwgdHJpbW1pbmcsIGVhciBhbmQgZXllIGNsZWFuaW5nLCBjb2F0IHN0eWxpbmcnLCfQktGB0Y8g0L/RgNC+0LPRgNCw0LzQvNCwJzonRklSU1QgVklTSVQgKDIwLTMwIG1pbikg4oCUIOKCrDIwXG7igKIgZ2V0dGluZyB1c2VkIHRvIHRoZSB0YWJsZSBhbmQgdG9vbHNcbuKAoiBnZW50bGUgYnJ1c2hpbmdcbuKAoiBkcnllciBzb3VuZHMgYW5kIGxpZ2h0IGFpcmZsb3dcbuKAoiBleWUgYW5kIGVhciByZWZyZXNoXG7igKIgbmFpbCB0cmltXG7igKIgdHJlYXRzIGFuZCBjYWxtIGFkYXB0YXRpb25cblxuU0VDT05EIFZJU0lUICg0MC02MCBtaW4pIOKAlCDigqwzNVxu4oCiIGZpcnN0IGJhdGggYW5kIGRyeWluZ1xu4oCiIGJydXNoaW5nXG7igKIgaHlnaWVuZSBjYXJlXG7igKIgbGlnaHQgdHJpbSAvIGNvYXQgYWRqdXN0bWVudCAoaWYgbmVlZGVkKVxu4oCiIHJlaW5mb3JjaW5nIHRoZSBwb3NpdGl2ZSBleHBlcmllbmNlJ30sCiAgZXQ6eyfQktGL0YfQtdGBJzonU2lsbWFkZSBqYSBrw7VydmFkZSBwdWhhc3RhbWluZSwga8O8w7xudGUgbMO1aWthbWluZSwgaGFyamFtaW5lIChrYXNzaWRlbGUpJywn0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQnOidQZXNlbWluZSBwcm9mZXNzaW9uYWFsc2V0ZSB2YWhlbmRpdGVnYSwgw7VybiBrdWl2YXRhbWluZScsJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0JzonS8O8w7xudGUgbMO1aWthbWluZSwga8O1cnZhZGUgamEgc2lsbWFkZSBwdWhhc3RhbWluZSwgcGVzZW1pbmUsIGt1aXZhdGFtaW5lLCBrw6RwcGFkZSBqYSB0dW5kbGlrZSBwaWlya29uZGFkZSBob29sZHVzJywn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc6J0vDvMO8bnRlIGzDtWlrYW1pbmUsIGvDtXJ2YWRlIGphIHNpbG1hZGUgcHVoYXN0YW1pbmUsIHBlc2VtaW5lLCBrdWl2YXRhbWluZSwga8OkcHBhZGUgamEgdHVuZGxpa2UgcGlpcmtvbmRhZGUgaG9vbGR1cywgbW9kZWxsw7Vpa3VzJywn0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAnOidQZXNlbWluZSwga3VpdmF0YW1pbmUsIGthcnZhc3Rpa3UgaG9vbGR1cywgbWFzaywga8O8w7xudGUgbMO1aWthbWluZSwga8O1cnZhZGUgamEgc2lsbWFkZSBwdWhhc3RhbWluZSwga8OkcHBhZGUgamEgZXJpbGlzdGUgcGlpcmtvbmRhZGUgaG9vbGR1cycsJ9Ci0YDQuNC80LzQuNC90LMnOidWYW5hIGthcnZha2loaSBlZW1hbGRhbWluZSwgcGVzZW1pbmUsIGt1aXZhdGFtaW5lLCBrw7zDvG50ZSBsw7Vpa2FtaW5lLCBrw7VydmFkZSBqYSBzaWxtYWRlIHB1aGFzdGFtaW5lLCBrYXJ2YXN0aWt1IGt1anVuZGFtaW5lJywn0JLRgdGPINC/0YDQvtCz0YDQsNC80LzQsCc6J0VTSU1FTkUgS8OcTEFTVFVTICgyMC0zMCBtaW4pIOKAlCAyMCDigqxcbuKAoiB0dXR2dW1pbmUgbGF1YWdhIGphIHTDtsO2cmlpc3RhZGVnYVxu4oCiIGtlcmdlIGhhcmphbWluZVxu4oCiIGbDtsO2bmloZWxpZCBqYSBrZXJnZSDDtWh1dm9vbFxu4oCiIHNpbG1hZGUgamEga8O1cnZhZGUgdsOkcnNrZW5kdXNcbuKAoiBrw7zDvG50ZSBsw7Vpa2FtaW5lXG7igKIgbWFpdXNlZCBqYSByYWh1bGlrIGtvaGFuZW1pbmVcblxuVEVJTkUgS8OcTEFTVFVTICg0MC02MCBtaW4pIOKAlCAzNSDigqxcbuKAoiBlc2ltZW5lIHZhbm5pdGFtaW5lIGphIGt1aXZhdGFtaW5lXG7igKIgaGFyamFtaW5lXG7igKIgaMO8Z2llZW5paG9vbGR1c1xu4oCiIGtlcmdlIGzDtWlrdXMgLyBrYXJ2YSBrb3JyaWdlZXJpbWluZSAodmFqYWR1c2VsKVxu4oCiIHBvc2l0aWl2c2Uga29nZW11c2Uga2lubmlzdGFtaW5lJ30KfTsKdmFyIFNWQ19ERVNDX0NBVF9DT01QTEVYPXsKICBydTon0JzRi9GC0YzRkSwg0YHRg9GI0LrQsCwg0LLRi9GH0ZHRgdGL0LLQsNC90LjQtSwg0YHRgtGA0LjQttC60LAg0LrQvtCz0YLQtdC5LCDQsCDRgtCw0LrQttC1INC+0LHRgNCw0LHQvtGC0LrQsCDQs9C70LDQtyDQuCDRg9GI0LXQuicsCiAgZW46J1dhc2hpbmcsIGRyeWluZywgYnJ1c2hpbmcsIG5haWwgdHJpbW1pbmcsIGFuZCBleWUgYW5kIGVhciBjYXJlJywKICBldDonUGVzZW1pbmUsIGt1aXZhdGFtaW5lLCBoYXJqYW1pbmUsIGvDvMO8bnRlIGzDtWlrYW1pbmUgbmluZyBzaWxtYWRlIGphIGvDtXJ2YWRlIGhvb2xkdXMnCn07CmZ1bmN0aW9uIGdldFN2Y1RhZyhuYW1lKXtyZXR1cm4oU1ZDX1RBR0xJTkVfSTE4TltMQU5HXSYmU1ZDX1RBR0xJTkVfSTE4TltMQU5HXVtuYW1lXSl8fFNWQ19UQUdMSU5FX0kxOE4ucnVbbmFtZV18fCcnO30KZnVuY3Rpb24gZ2V0U3ZjRGVzYyhuYW1lKXsKICBpZihuYW1lPT09J9Ca0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQnICYmIGJvb2tpbmcuYnJlZWQgJiYgYm9va2luZy5icmVlZC5pbmRleE9mKCfQmtC+0YjQutCwJyk9PT0wKXsKICAgIHZhciBkPVNWQ19ERVNDX0NBVF9DT01QTEVYW0xBTkddfHxTVkNfREVTQ19DQVRfQ09NUExFWC5ydTsKICAgIHJldHVybiBkOwogIH0KICByZXR1cm4oU1ZDX0RFU0NfSTE4TltMQU5HXSYmU1ZDX0RFU0NfSTE4TltMQU5HXVtuYW1lXSl8fFNWQ19ERVNDX0kxOE4ucnVbbmFtZV18fCcnOwp9CgpmdW5jdGlvbiByZW5kZXJTdmNzKGIpewogIHZhciBsYmxFbD1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3RlcDJMYmxFbCcpOwogIGlmKGxibEVsKXsKICAgIHZhciBiYXNlTGJsPShUW0xBTkddJiZUW0xBTkddLnN0ZXAyX2xibCl8fCcwMiDCtyDQo9GB0LvRg9Cz0LAnOwogICAgbGJsRWwudGV4dENvbnRlbnQ9KGIuYnJlZWQ9PT0n0KnQtdC90LrQuCcpPyhiYXNlTGJsKycgUHVwcHkgU3RhcicpOmJhc2VMYmw7CiAgfQogIHZhciBsaXN0PWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdmNMaXN0Jyk7bGlzdC5pbm5lckhUTUw9Jyc7CiAgT2JqZWN0LmVudHJpZXMoYi5zZXJ2aWNlcykuZm9yRWFjaChmdW5jdGlvbihrdil7CiAgICB2YXIgbmFtZT1rdlswXSxwcmljZT1rdlsxXTsKCiAgICB2YXIgZGlzcGxheU5hbWU9KExBTkchPT0ncnUnJiZTVkNfVFJBTlNMQVRJT05TW25hbWVdKT9TVkNfVFJBTlNMQVRJT05TW25hbWVdW0xBTkddOm5hbWU7CiAgICB2YXIgYnRuPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2J1dHRvbicpO2J0bi5jbGFzc05hbWU9J3N2YnRuJzsKICAgIHZhciByb3c9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7cm93LmNsYXNzTmFtZT0nc3ZidG4tcm93JzsKICAgIHZhciBucz1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdzcGFuJyk7bnMuY2xhc3NOYW1lPSdzdmJ0bi1uYW1lJztucy50ZXh0Q29udGVudD1kaXNwbGF5TmFtZTsKICAgIHZhciBwcz1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdzcGFuJyk7cHMuY2xhc3NOYW1lPSdzdmJ0bi1wcmljZSc7cHMudGV4dENvbnRlbnQ9cHJpY2UrJyDigqwnOwogICAgcm93LmFwcGVuZENoaWxkKG5zKTtyb3cuYXBwZW5kQ2hpbGQocHMpOwogICAgYnRuLmFwcGVuZENoaWxkKHJvdyk7CiAgICB2YXIgZGVzYz1nZXRTdmNEZXNjKG5hbWUpOwogICAgaWYoZGVzYyl7dmFyIGRzPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NwYW4nKTtkcy5jbGFzc05hbWU9J3N2YnRuLWRlc2MnO2RzLnRleHRDb250ZW50PWRlc2M7YnRuLmFwcGVuZENoaWxkKGRzKTt9CiAgICB2YXIgdGFnPWdldFN2Y1RhZyhuYW1lKTsKICAgIGlmKHRhZyl7dmFyIHRzPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NwYW4nKTt0cy5jbGFzc05hbWU9J3N2YnRuLXRhZyc7dHMudGV4dENvbnRlbnQ9dGFnO2J0bi5hcHBlbmRDaGlsZCh0cyk7fQogICAgYnRuLm9uY2xpY2s9ZnVuY3Rpb24oKXsKICAgICAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLnN2YnRuJykuZm9yRWFjaChmdW5jdGlvbihiKXtiLmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpO30pOwogICAgICBidG4uY2xhc3NMaXN0LmFkZCgnYWN0aXZlJyk7CiAgICAgIGJvb2tpbmcuc2VydmljZT1uYW1lO2Jvb2tpbmcucHJpY2U9cHJpY2U7CiAgICAgIGZpbHRlck1hc3RlcnMoKTsKICAgICAgc2V0VGltZW91dChmdW5jdGlvbigpe2dvU3RlcCgyKTt9LDMwMCk7CiAgICB9OwogICAgbGlzdC5hcHBlbmRDaGlsZChidG4pOwogIH0pOwp9CgovLyBNYXN0ZXJzCmZ1bmN0aW9uIGZpbHRlck1hc3RlcnMoKXsKICB2YXIgaXNDYXQgPSBib29raW5nLmJyZWVkICYmIGJvb2tpbmcuYnJlZWQuaW5kZXhPZign0JrQvtGI0LrQsCcpID09PSAwOwogIHZhciBicmVlZCA9IGJvb2tpbmcuYnJlZWQgfHwgJyc7CiAgdmFyIGlzQ2F0Q29tcGxleCA9IGlzQ2F0ICYmIGJvb2tpbmcuc2VydmljZSA9PT0gJ9Ca0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQnOwogIHZhciBhbm5hRXhjbHVkZSA9IFsn0JzQsNC70YzRgtC40L/RgycsJ9Cf0YPQtNC10LvRjCcsJ9CZ0L7RgNC6Jywn0JHQuNGI0L7QvScsJ9CR0L7Qu9C+0L3QutCwJywn0JzQsNC70YzRgtC40LnRgdC60LDRjyddOwogIHZhciBpc0FubmFCcmVlZCA9IGJyZWVkICYmICFhbm5hRXhjbHVkZS5zb21lKGZ1bmN0aW9uKGIpeyByZXR1cm4gYnJlZWQuaW5kZXhPZihiKSAhPT0gLTE7IH0pOwogIHZhciBhbGV4YW5kcmFFeGNsdWRlID0gWyfQpNC+0LrRgdGC0LXRgNGM0LXRgCcsJ9Cm0LLQtdGA0LPRiNC90LDRg9GG0LXRgCddOwogIHZhciBpc0FsZXhhbmRyYUJyZWVkID0gIWFsZXhhbmRyYUV4Y2x1ZGUuc29tZShmdW5jdGlvbihiKXsgcmV0dXJuIGJyZWVkLmluZGV4T2YoYikgIT09IC0xOyB9KTsKICB2YXIga3NlbmlhRXhjbHVkZSA9IFsn0J/Rg9C00LXQu9GMJywn0JzQsNC70YzRgtC40L/RgycsJ9CZ0L7RgNC6J107CiAgdmFyIGlzS3NlbmlhQnJlZWQgPSAha3NlbmlhRXhjbHVkZS5zb21lKGZ1bmN0aW9uKGIpeyByZXR1cm4gYnJlZWQuaW5kZXhPZihiKSAhPT0gLTE7IH0pOwogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5tYnRuJykuZm9yRWFjaChmdW5jdGlvbihidG4pewogICAgdmFyIG1hc3RlciA9IGJ0bi5nZXRBdHRyaWJ1dGUoJ2RhdGEtbWFzdGVyJyk7CiAgICB2YXIgaXNUcmltbWluZyA9IGJvb2tpbmcuc2VydmljZSA9PT0gJ9Ci0YDQuNC80LzQuNC90LMnOwogICAgaWYoaXNDYXRDb21wbGV4KXsKICAgICAgYnRuLnN0eWxlLmRpc3BsYXkgPSAobWFzdGVyID09PSAn0KLQsNGC0YzRj9C90LAnIHx8IG1hc3RlciA9PT0gJ9Ca0YHQtdC90LjRjycpID8gJycgOiAnbm9uZSc7CiAgICAgIHJldHVybjsKICAgIH0KICAgIGlmKG1hc3RlciA9PT0gJ9CQ0LvQuNGB0LAnKXsKICAgICAgYnRuLnN0eWxlLmRpc3BsYXkgPSBpc0NhdCA/ICcnIDogJ25vbmUnOwogICAgfSBlbHNlIGlmKG1hc3RlciA9PT0gJ9CQ0L3QvdCwJyl7CiAgICAgIGJ0bi5zdHlsZS5kaXNwbGF5ID0gKGlzQW5uYUJyZWVkICYmICFpc1RyaW1taW5nKSA/ICcnIDogJ25vbmUnOwogICAgfSBlbHNlIGlmKG1hc3RlciA9PT0gJ9CQ0LvQtdC60YHQsNC90LTRgNCwJyl7CiAgICAgIGJ0bi5zdHlsZS5kaXNwbGF5ID0gKGlzQWxleGFuZHJhQnJlZWQgJiYgIWlzVHJpbW1pbmcgJiYgIWlzQ2F0KSA/ICcnIDogJ25vbmUnOwogICAgfSBlbHNlIGlmKG1hc3RlciA9PT0gJ9Ca0YHQtdC90LjRjycpewogICAgICBidG4uc3R5bGUuZGlzcGxheSA9IGlzS3NlbmlhQnJlZWQgPyAnJyA6ICdub25lJzsKICAgIH0gZWxzZSBpZihpc1RyaW1taW5nKXsKICAgICAgYnRuLnN0eWxlLmRpc3BsYXkgPSAnbm9uZSc7CiAgICB9IGVsc2UgewogICAgICBidG4uc3R5bGUuZGlzcGxheSA9ICcnOwogICAgfQogIH0pOwp9Cgpkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcubWJ0bicpLmZvckVhY2goZnVuY3Rpb24oYnRuKXsKICBidG4ub25jbGljaz1mdW5jdGlvbigpewogICAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLm1idG4nKS5mb3JFYWNoKGZ1bmN0aW9uKGIpe2IuY2xhc3NMaXN0LnJlbW92ZSgnYWN0aXZlJyk7fSk7CiAgICBidG4uY2xhc3NMaXN0LmFkZCgnYWN0aXZlJyk7CiAgICBib29raW5nLm1hc3Rlcj1idG4uZ2V0QXR0cmlidXRlKCdkYXRhLW1hc3RlcicpOwogICAgc2V0VGltZW91dChmdW5jdGlvbigpe2dvU3RlcCgzKTt9LDMwMCk7CiAgfTsKfSk7CgovLyBHcm9vbSBoaXN0b3J5CmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5nYnRuJykuZm9yRWFjaChmdW5jdGlvbihidG4pewogIGJ0bi5vbmNsaWNrPWZ1bmN0aW9uKCl7CiAgICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuZ2J0bicpLmZvckVhY2goZnVuY3Rpb24oYil7Yi5jbGFzc0xpc3QucmVtb3ZlKCdhY3RpdmUnKTt9KTsKICAgIGJ0bi5jbGFzc0xpc3QuYWRkKCdhY3RpdmUnKTsKICAgIGJvb2tpbmcuZ3Jvb21IaXN0b3J5PWJ0bi5nZXRBdHRyaWJ1dGUoJ2RhdGEtdmFsJyk7CiAgICBzZXRUaW1lb3V0KGZ1bmN0aW9uKCl7Z29TdGVwKDQpO2J1aWxkQ2FsKCk7fSwzMDApOwogIH07Cn0pOwoKLy8gQ2FsZW5kYXIKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3ByZXZNJykub25jbGljaz1mdW5jdGlvbigpe2NNLS07aWYoY008MCl7Y009MTE7Y1ktLTt9YnVpbGRDYWwoKTt9Owpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbmV4dE0nKS5vbmNsaWNrPWZ1bmN0aW9uKCl7Y00rKztpZihjTT4xMSl7Y009MDtjWSsrO31idWlsZENhbCgpO307Cgp2YXIgYXZhaWxhYmxlRGF5cyA9IFtdOwoKZnVuY3Rpb24gbG9hZEF2YWlsYWJsZURheXMoKSB7CiAgdmFyIG1hc3RlciA9IGJvb2tpbmcubWFzdGVyOwogIGlmICghbWFzdGVyKSByZXR1cm47CiAgYXZhaWxhYmxlRGF5cyA9IFtdOwogIGZldGNoKHdpbmRvdy5sb2NhdGlvbi5vcmlnaW4gKyAnL2FwaS9hdmFpbGFibGVfZGF5cz9tb250aD0nICsgKGNNKzEpICsgJyZ5ZWFyPScgKyBjWSArICcmbWFzdGVyPScgKyBlbmNvZGVVUklDb21wb25lbnQobWFzdGVyKSkKICAgIC50aGVuKGZ1bmN0aW9uKHIpeyByZXR1cm4gci5qc29uKCk7IH0pCiAgICAudGhlbihmdW5jdGlvbihkYXRhKXsKICAgICAgYXZhaWxhYmxlRGF5cyA9IGRhdGEuYXZhaWxhYmxlIHx8IFtdOwogICAgICBtYXJrQXZhaWxhYmxlRGF5cygpOwogICAgfSkKICAgIC5jYXRjaChmdW5jdGlvbigpeyBhdmFpbGFibGVEYXlzID0gW107IH0pOwp9CgpmdW5jdGlvbiBtYXJrQXZhaWxhYmxlRGF5cygpIHsKICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuY2QnKS5mb3JFYWNoKGZ1bmN0aW9uKGMpe2lmKCFjLmNsYXNzTGlzdC5jb250YWlucygnZGlzJykpYy5jbGFzc0xpc3QucmVtb3ZlKCdzZWwnKTt9KTsKICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuY2Q6bm90KC5kaXMpOm5vdCguY2RuKTpub3QoLnBhZCknKS5mb3JFYWNoKGZ1bmN0aW9uKGVsKSB7CiAgICB2YXIgZGF5ID0gZWwudGV4dENvbnRlbnQudHJpbSgpOwogICAgaWYgKCFkYXkgfHwgaXNOYU4ocGFyc2VJbnQoZGF5KSkpIHJldHVybjsKICAgIHZhciBkYXRlU3RyID0gU3RyaW5nKHBhcnNlSW50KGRheSkpLnBhZFN0YXJ0KDIsJzAnKSArICcuJyArIFN0cmluZyhjTSsxKS5wYWRTdGFydCgyLCcwJykgKyAnLicgKyBjWTsKICAgIGlmIChhdmFpbGFibGVEYXlzLmluZGV4T2YoZGF0ZVN0cikgIT09IC0xKSB7CiAgICAgIGVsLmNsYXNzTGlzdC5hZGQoJ2F2YWlsJyk7CiAgICAgIGVsLmNsYXNzTGlzdC5yZW1vdmUoJ2J1c3knKTsKICAgIH0gZWxzZSB7CiAgICAgIGVsLmNsYXNzTGlzdC5hZGQoJ2J1c3knKTsKICAgICAgZWwuY2xhc3NMaXN0LnJlbW92ZSgnYXZhaWwnKTsKICAgIH0KICB9KTsKfQoKZnVuY3Rpb24gYnVpbGRDYWwoKXsKICBsb2FkQXZhaWxhYmxlRGF5cygpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYWxNJykudGV4dENvbnRlbnQ9TU9OVEhTW2NNXSsnICcrY1k7CiAgYm9va2luZy5kYXRlPScnOyBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuY2QnKS5mb3JFYWNoKGZ1bmN0aW9uKGMpe2MuY2xhc3NMaXN0LnJlbW92ZSgnc2VsJyk7Yy5jbGFzc0xpc3QucmVtb3ZlKCdhdmFpbCcpO2MuY2xhc3NMaXN0LnJlbW92ZSgnYnVzeScpO30pOwogIHZhciBnPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYWxHJyk7Zy5pbm5lckhUTUw9Jyc7CiAgWyfQn9C9Jywn0JLRgicsJ9Ch0YAnLCfQp9GCJywn0J/RgicsJ9Ch0LEnLCfQktGBJ10uZm9yRWFjaChmdW5jdGlvbihkKXsKICAgIHZhciBlbD1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdkaXYnKTtlbC5jbGFzc05hbWU9J2Nkbic7ZWwudGV4dENvbnRlbnQ9ZDtnLmFwcGVuZENoaWxkKGVsKTsKICB9KTsKICB2YXIgZmlyc3Q9bmV3IERhdGUoY1ksY00sMSkuZ2V0RGF5KCk7CiAgdmFyIGRheXM9bmV3IERhdGUoY1ksY00rMSwwKS5nZXREYXRlKCk7CiAgdmFyIHN0YXJ0PWZpcnN0PT09MD82OmZpcnN0LTE7CiAgdmFyIHRvZGF5PW5ldyBEYXRlKCk7CiAgZm9yKHZhciBpPTA7aTxzdGFydDtpKyspe3ZhciBlbD1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdkaXYnKTtlbC5jbGFzc05hbWU9J2NkIHBhZCc7Zy5hcHBlbmRDaGlsZChlbCk7fQogIGZvcih2YXIgZGF5PTE7ZGF5PD1kYXlzO2RheSsrKXsKICAgIHZhciBlbD1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdkaXYnKTtlbC5jbGFzc05hbWU9J2NkJzsKICAgIHZhciBkYXRlPW5ldyBEYXRlKGNZLGNNLGRheSk7CiAgICB2YXIgaXNQYXN0PWRhdGU8bmV3IERhdGUodG9kYXkuZ2V0RnVsbFllYXIoKSx0b2RheS5nZXRNb250aCgpLHRvZGF5LmdldERhdGUoKSk7CiAgICB2YXIgaW5uZXI9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7aW5uZXIuY2xhc3NOYW1lPSdjZC1pbm5lcic7aW5uZXIudGV4dENvbnRlbnQ9ZGF5O2VsLmFwcGVuZENoaWxkKGlubmVyKTsKICAgIGlmKGlzUGFzdCl7ZWwuY2xhc3NMaXN0LmFkZCgnZGlzJyk7fQogICAgZWxzZXsKICAgICAgaWYoZGF0ZS50b0RhdGVTdHJpbmcoKT09PXRvZGF5LnRvRGF0ZVN0cmluZygpKWVsLmNsYXNzTGlzdC5hZGQoJ3RvZCcpOwogICAgICAoZnVuY3Rpb24oZCwgZWxSZWYpewogICAgICAgIGVsUmVmLm9uY2xpY2s9ZnVuY3Rpb24oKXsKICAgICAgICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5jZCcpLmZvckVhY2goZnVuY3Rpb24oYyl7Yy5jbGFzc0xpc3QucmVtb3ZlKCdzZWwnKTt9KTsKICAgICAgICAgIGVsUmVmLmNsYXNzTGlzdC5hZGQoJ3NlbCcpOwogICAgICAgICAgYm9va2luZy5kYXRlPVN0cmluZyhkKS5wYWRTdGFydCgyLCcwJykrJy4nK1N0cmluZyhjTSsxKS5wYWRTdGFydCgyLCcwJykrJy4nK2NZOwogICAgICAgICAgc2hvd1RpbWVzKCk7CiAgICAgICAgfTsKICAgICAgfSkoZGF5LCBlbCk7CiAgICB9CiAgICBnLmFwcGVuZENoaWxkKGVsKTsKICB9CiAgLy8gZmlsbCB0cmFpbGluZyBjZWxscyB0byBjb21wbGV0ZSBsYXN0IGdyaWQgcm93CiAgdmFyIHRvdGFsID0gc3RhcnQgKyBkYXlzOwogIHZhciB0cmFpbCA9ICg3IC0gKHRvdGFsICUgNykpICUgNzsKICBmb3IodmFyIHQ9MDt0PHRyYWlsO3QrKyl7dmFyIGVwPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO2VwLmNsYXNzTmFtZT0nY2QgcGFkJztnLmFwcGVuZENoaWxkKGVwKTt9Cn0KCmZ1bmN0aW9uIHNob3dUaW1lcygpewogIHZhciB0Zz1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgndGltZUcnKTsKICB0Zy5pbm5lckhUTUw9JzxkaXYgY2xhc3M9ImxvYWRpbmctc2xvdHMiPuKPsyDQl9Cw0LPRgNGD0LbQsNC10Lwg0YDQsNGB0L/QuNGB0LDQvdC40LUuLi48L2Rpdj4nOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCd0aW1lU2VjJykuc3R5bGUuZGlzcGxheT0nYmxvY2snOwoKICB2YXIgdXJsID0gd2luZG93LmxvY2F0aW9uLm9yaWdpbiArICIvYXBpL3Nsb3RzIiArICc/YWN0aW9uPXNsb3RzJmRhdGU9JyArIGVuY29kZVVSSUNvbXBvbmVudChib29raW5nLmRhdGUpICsgJyZtYXN0ZXI9JyArIGVuY29kZVVSSUNvbXBvbmVudChib29raW5nLm1hc3Rlcik7CgogIGZldGNoKHVybCkKICAgIC50aGVuKGZ1bmN0aW9uKHIpe3JldHVybiByLmpzb24oKTt9KQogICAgLnRoZW4oZnVuY3Rpb24oZGF0YSl7CiAgICAgIHZhciBzbG90cyA9IChkYXRhLnNsb3RzICYmIGRhdGEuc2xvdHMubGVuZ3RoID4gMCkgPyBkYXRhLnNsb3RzIDogW107CiAgICAgIHJlbmRlclRpbWVTbG90cyhzbG90cyk7CiAgICB9KQogICAgLmNhdGNoKGZ1bmN0aW9uKCl7CiAgICAgIHJlbmRlclRpbWVTbG90cyhbXSk7CiAgICB9KTsKfQoKZnVuY3Rpb24gcmVuZGVyVGltZVNsb3RzKHNsb3RzKXsKICB2YXIgdGc9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3RpbWVHJyk7dGcuaW5uZXJIVE1MPScnOwogIGlmKHNsb3RzLmxlbmd0aD09PTApewogICAgdGcuaW5uZXJIVE1MPSc8ZGl2IGNsYXNzPSJsb2FkaW5nLXNsb3RzIj7QndC10YIg0LTQvtGB0YLRg9C/0L3Ri9GFINGB0LvQvtGC0L7QsiDQvdCwINGN0YLRgyDQtNCw0YLRgzwvZGl2PjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lciIgb25jbGljaz0ic2hvd1NjcmVlbihcJ2hvbWVTY3JlZW5cJykiIHN0eWxlPSJtYXJnaW4tdG9wOjhweDsiPjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci1pY29uIj7wn5C+PC9kaXY+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLXRleHQiPjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci10aXRsZSI+0J3QtSDQvdCw0YjQu9C4INC/0L7QtNGF0L7QtNGP0YnQtdC1INCy0YDQtdC80Y8/PC9kaXY+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLXN1YiI+0KHQstGP0LbQuNGC0LXRgdGMINGBINC90LDQvNC4INC70Y7QsdGL0Lwg0YPQtNC+0LHQvdGL0Lwg0YHQv9C+0YHQvtCx0L7QvCDigJQg0LzRiyDQv9C+0LTQsdC10YDRkdC8INGD0LTQvtCx0L3QvtC1INCy0YDQtdC80Y88L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItYXJyb3ciPuKGkjwvZGl2PjwvZGl2Pic7CiAgICByZXR1cm47CiAgfQogIHNsb3RzLmZvckVhY2goZnVuY3Rpb24odCl7CiAgICB2YXIgYnRuPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2J1dHRvbicpO2J0bi5jbGFzc05hbWU9J3RidG4nO2J0bi50ZXh0Q29udGVudD10OwogICAgYnRuLm9uY2xpY2s9ZnVuY3Rpb24oKXsKICAgICAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLnRidG4nKS5mb3JFYWNoKGZ1bmN0aW9uKGIpe2IuY2xhc3NMaXN0LnJlbW92ZSgnYWN0aXZlJyk7fSk7CiAgICAgIGJ0bi5jbGFzc0xpc3QuYWRkKCdhY3RpdmUnKTtib29raW5nLnRpbWU9dDsKICAgICAgc2V0VGltZW91dChmdW5jdGlvbigpe2dvU3RlcCg1KTtidWlsZFN1bSgpO30sMzAwKTsKICAgIH07CiAgICB0Zy5hcHBlbmRDaGlsZChidG4pOwogIH0pOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCd0aW1lU2VjJykuc2Nyb2xsSW50b1ZpZXcoe2JlaGF2aW9yOidzbW9vdGgnLGJsb2NrOiduZWFyZXN0J30pOwp9CgpmdW5jdGlvbiBidWlsZFN1bSgpewogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdW1CbG9jaycpLmlubmVySFRNTD0KICAgICc8ZGl2IGNsYXNzPSJzciI+PHNwYW4gY2xhc3M9InNsIj4nK1RbTEFOR10uc3VtX2JyZWVkKyc8L3NwYW4+PHNwYW4gY2xhc3M9InN2Ij4nKyhib29raW5nLmJyZWVkRGlzcGxheXx8Ym9va2luZy5icmVlZCkrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fc2VydmljZSsnPC9zcGFuPjxzcGFuIGNsYXNzPSJzdiI+JysoKExBTkchPT0ncnUnJiZTVkNfVFJBTlNMQVRJT05TW2Jvb2tpbmcuc2VydmljZV0pP1NWQ19UUkFOU0xBVElPTlNbYm9va2luZy5zZXJ2aWNlXVtMQU5HXTpib29raW5nLnNlcnZpY2UpKyc8L3NwYW4+PC9kaXY+JysKICAgICc8ZGl2IGNsYXNzPSJzciI+PHNwYW4gY2xhc3M9InNsIj4nK1RbTEFOR10uc3VtX21hc3RlcisnPC9zcGFuPjxzcGFuIGNsYXNzPSJzdiI+Jytib29raW5nLm1hc3RlcisnPC9zcGFuPjwvZGl2PicrCiAgICAnPGRpdiBjbGFzcz0ic3IiPjxzcGFuIGNsYXNzPSJzbCI+JytUW0xBTkddLnN1bV9ncm9vbSsnPC9zcGFuPjxzcGFuIGNsYXNzPSJzdiI+Jytib29raW5nLmdyb29tSGlzdG9yeSsnPC9zcGFuPjwvZGl2PicrCiAgICAnPGRpdiBjbGFzcz0ic3IiPjxzcGFuIGNsYXNzPSJzbCI+JytUW0xBTkddLnN1bV9kYXRlKyc8L3NwYW4+PHNwYW4gY2xhc3M9InN2Ij4nK2Jvb2tpbmcuZGF0ZSsnPC9zcGFuPjwvZGl2PicrCiAgICAnPGRpdiBjbGFzcz0ic3IiPjxzcGFuIGNsYXNzPSJzbCI+JytUW0xBTkddLnN1bV90aW1lKyc8L3NwYW4+PHNwYW4gY2xhc3M9InN2Ij4nK2Jvb2tpbmcudGltZSsnPC9zcGFuPjwvZGl2PicrCiAgICAnPGRpdiBjbGFzcz0ic3IiPjxzcGFuIGNsYXNzPSJzbCI+JytUW0xBTkddLnN1bV9wcmljZSsnPC9zcGFuPjxzcGFuIGNsYXNzPSJzcCI+Jytib29raW5nLnByaWNlKycg4oKsPC9zcGFuPjwvZGl2Pic7Cn0KCmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjb25maXJtQnRuJykub25jbGljayA9IGZ1bmN0aW9uKCl7CiAgdmFyIG5hbWU9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NOYW1lJykudmFsdWU7CiAgdmFyIHBob25lPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjUGhvbmUnKS52YWx1ZTsKICBpZighbmFtZXx8IXBob25lKXthbGVydChUW0xBTkddLmFsZXJ0X2ZpbGwpO3JldHVybjt9CiAgaWYoIS9eXCtcZHsxMCx9JC8udGVzdChwaG9uZS50cmltKCkpKXthbGVydChUW0xBTkddLmFsZXJ0X3Bob25lKTtyZXR1cm47fQogIGJvb2tpbmcubmFtZT1uYW1lOyBib29raW5nLnBob25lPXBob25lOyBib29raW5nLmVtYWlsPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjRW1haWwnKS52YWx1ZTsgYm9va2luZy5wZXQ9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NQZXQnKS52YWx1ZTsgYm9va2luZy5sYW5nPUxBTkc7CiAgYm9va2luZy5kdXJhdGlvbiA9IGJvb2tpbmcuYnJlZWQgPT09ICfQqdC10L3QutC4JyA/IDYwIDogKGJvb2tpbmcuYnJlZWQgJiYgYm9va2luZy5icmVlZC5pbmRleE9mKCfQmtC+0YjQutCwJykgPT09IDAgPyAxMjAgOiAxODApOwogIHZhciBidG49ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NvbmZpcm1CdG4nKTsKICBidG4udGV4dENvbnRlbnQ9VFtMQU5HXS5zZW5kaW5nOyBidG4uZGlzYWJsZWQ9dHJ1ZTsKICBmZXRjaChSQUlMV0FZLCB7CiAgICBtZXRob2Q6J1BPU1QnLAogICAgaGVhZGVyczp7J0NvbnRlbnQtVHlwZSc6J2FwcGxpY2F0aW9uL2pzb24nfSwKICAgIGJvZHk6SlNPTi5zdHJpbmdpZnkoYm9va2luZykKICB9KS50aGVuKGZ1bmN0aW9uKCl7c2hvd1N1Y2Nlc3MoKTt9KS5jYXRjaChmdW5jdGlvbigpe3Nob3dTdWNjZXNzKCk7fSk7Cn07CgpmdW5jdGlvbiBzaG93U3VjY2VzcygpewogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdiazUnKS5jbGFzc05hbWU9J3N0ZXAnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdWNCbG9jaycpLmNsYXNzTGlzdC5hZGQoJ3Nob3cnKTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgncHJvZ3Jlc3MnKS5zdHlsZS5kaXNwbGF5PSdub25lJzsKfQoKZnVuY3Rpb24gcmVzZXRBbGwoKXsKICBib29raW5nPXticmVlZDonJyxicmVlZERpc3BsYXk6Jycsc2VydmljZTonJyxwcmljZTowLG1hc3RlcjonJyxncm9vbUhpc3Rvcnk6JycsZGF0ZTonJyx0aW1lOicnLGxhbmc6J3J1J307CiAgc2VsQnJlZWQ9bnVsbDsgaW5wLnZhbHVlPScnOyBjbHIuY2xhc3NMaXN0LnJlbW92ZSgnc2hvdycpOwogIGJhZGdlLmNsYXNzTGlzdC5yZW1vdmUoJ3Nob3cnKTsgYmFkZ2UuaW5uZXJIVE1MPScnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdmNTZWMnKS5zdHlsZS5kaXNwbGF5PSdub25lJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgndGltZVNlYycpLnN0eWxlLmRpc3BsYXk9J25vbmUnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdWNCbG9jaycpLmNsYXNzTGlzdC5yZW1vdmUoJ3Nob3cnKTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgncHJvZ3Jlc3MnKS5zdHlsZS5kaXNwbGF5PSdmbGV4JzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY05hbWUnKS52YWx1ZT0nJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY1Bob25lJykudmFsdWU9Jyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NFbWFpbCcpLnZhbHVlPScnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjUGV0JykudmFsdWU9Jyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NvbmZpcm1CdG4nKS50ZXh0Q29udGVudD1UW0xBTkddLmNvbmZpcm1fYnRuOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjb25maXJtQnRuJykuZGlzYWJsZWQ9ZmFsc2U7CiAgZ29TdGVwKDEpOwp9Cgp2YXIgTEFORyA9IGxvY2FsU3RvcmFnZS5nZXRJdGVtKCdyamxhbmcnKSB8fCAncnUnOwp2YXIgVCA9IHsKICBydTp7CiAgICBsb2dvX3RhZzon0J/RgNC10LzQuNCw0LvRjNC90YvQuSDQs9GA0YPQvNC40L3Qsy08YnI+0YHQsNC70L7QvSDQsiDQotCw0LvQu9C40L3QtScsCiAgICBjaG9vc2VfaG93OidDaG9vc2UgaG93IHRvIGNvbm5lY3QnLAogICAgYm9va19vbmxpbmU6J9Ce0L3Qu9Cw0LnQvSDQsdGA0L7QvdC40YDQvtCy0LDQvdC40LUnLAogICAgYm9va19mbG93OifQn9C+0YDQvtC00LAg4oaSINCj0YHQu9GD0LPQsCDihpIg0JzQsNGB0YLQtdGAIOKGkiDQktGA0LXQvNGPJywKICAgIG9yX2NvbnRhY3Q6J9C40LvQuCDRgdCy0Y/QttC40YLQtdGB0Ywg0YEg0L3QsNC80LgnLAogICAgY2FsbF91czonQ2FsbCBVcycsCiAgICBiYWNrOifihpAg0J3QsNC30LDQtCcsCiAgICBsb2dvX3N1YjonR3Jvb21pbmcgwrcg0KLQsNC70LvQuNC9JywKICAgIHBzX3NlcnZpY2U6J9Cj0YHQu9GD0LPQsCcscHNfbWFzdGVyOifQnNCw0YHRgtC10YAnLHBzX3BldDon0J/QuNGC0L7QvNC10YYnLHBzX2RhdGU6J9CU0LDRgtCwJyxwc19kZXRhaWxzOifQlNCw0L3QvdGL0LUnLAogICAgc3RlcDFfbGJsOicwMSDCtyDQn9C+0YDQvtC00LAnLAogICAgYnJlZWRfcGg6J9Cd0LDRh9C90LjRgtC1INCy0LLQvtC00LjRgtGMINC/0L7RgNC+0LTRgy4uLicsCiAgICBzdGVwMl9sYmw6JzAyIMK3INCj0YHQu9GD0LPQsCcsCiAgICBzdGVwMl9tYXN0ZXI6J9CS0YvQsdC10YDQuNGC0LUg0LzQsNGB0YLQtdGA0LAnLAogICAgc3RlcDNfbGJsOifQmtCw0Log0LTQsNCy0L3QviDQstGLINC/0L7RgdC10YnQsNC70Lgg0LPRgNGD0LzQuNC90LM/JywKICAgIGcxOifQn9C10YDQstGL0Lkg0YDQsNC3JyxnMjon0J7RgiAxINC00L4gMyDQvNC10YHRj9GG0LXQsicsZzM6J9Ce0YIgMyDQtNC+IDYg0LzQtdGB0Y/RhtC10LInLGc0OifQkdC+0LvQtdC1IDYg0LzQtdGB0Y/RhtC10LInLAogICAgc3RlcDRfbGJsOifQktGL0LHQtdGA0LjRgtC1INC00LDRgtGDJywKICAgIGNhbF9hdmFpbDon0JXRgdGC0Ywg0YHQstC+0LHQvtC00L3QvtC1INCy0YDQtdC80Y8nLGNhbF9ub25lOifQodCy0L7QsdC+0LTQvdC+0LPQviDQstGA0LXQvNC10L3QuCDQvdC10YInLAogICAgc3RlcDRfdGltZTon0JLRi9Cx0LXRgNC40YLQtSDQstGA0LXQvNGPJywKICAgIHN0ZXA1X2xibDon0JLQsNGI0Lgg0LTQsNC90L3Ri9C1JywKICAgIGxibF9uYW1lOifQmNC80Y8nLHBoX25hbWU6J9CS0LDRiNC1INC40LzRjycsCiAgICBsYmxfcGhvbmU6J9Ci0LXQu9C10YTQvtC9JyxsYmxfZW1haWw6J0VtYWlsJywKICAgIGxibF9wZXQ6J9Ca0LvQuNGH0LrQsCDQv9C40YLQvtC80YbQsCcscGhfb3B0aW9uYWw6J9Cd0LXQvtCx0Y/Qt9Cw0YLQtdC70YzQvdC+JywKICAgIGNvbmZpcm1fYnRuOifQn9C+0LTRgtCy0LXRgNC00LjRgtGMINC30LDQv9C40YHRjCcsCiAgICBzdWNjZXNzX3RpdGxlOifQl9Cw0L/QuNGB0Ywg0L/RgNC40L3Rj9GC0LAhJywKICAgIHN1Y2Nlc3Nfc3ViOifQnNGLINGB0LLRj9C20LXQvNGB0Y8g0YEg0LLQsNC80Lgg0LTQu9GPINC/0L7QtNGC0LLQtdGA0LbQtNC10L3QuNGPLjxicj7QodC/0LDRgdC40LHQviwg0YfRgtC+INCy0YvQsdGA0LDQu9C4IFImYW1wO0ogR3Jvb21pbmchJywKICAgIHRvX2hvbWU6J+KGkCDQndCwINCz0LvQsNCy0L3Rg9GOJywKICAgIGFsZXJ0X2ZpbGw6J9CS0LLQtdC00LjRgtC1INC40LzRjyDQuCDRgtC10LvQtdGE0L7QvScsYWxlcnRfcGhvbmU6J9CS0LLQtdC00LjRgtC1INC90L7QvNC10YAg0LIg0YTQvtGA0LzQsNGC0LUgKzM3MjEyMzQ1Njc4JywKICAgIHNlbmRpbmc6J9Ce0YLQv9GA0LDQstC70Y/QtdC8Li4uJywKICAgIHN1bV9icmVlZDon0J/QvtGA0L7QtNCwJyxzdW1fc2VydmljZTon0KPRgdC70YPQs9CwJyxzdW1fbWFzdGVyOifQnNCw0YHRgtC10YAnLHN1bV9ncm9vbTon0J/QvtGB0LvQtdC00L3QuNC5INCz0YDRg9C8JyxzdW1fZGF0ZTon0JTQsNGC0LAnLHN1bV90aW1lOifQktGA0LXQvNGPJyxzdW1fcHJpY2U6J9Ch0YLQvtC40LzQvtGB0YLRjCcsCiAgICBtb250aHM6WyfQr9C90LLQsNGA0YwnLCfQpNC10LLRgNCw0LvRjCcsJ9Cc0LDRgNGCJywn0JDQv9GA0LXQu9GMJywn0JzQsNC5Jywn0JjRjtC90YwnLCfQmNGO0LvRjCcsJ9CQ0LLQs9GD0YHRgicsJ9Ch0LXQvdGC0Y/QsdGA0YwnLCfQntC60YLRj9Cx0YDRjCcsJ9Cd0L7Rj9Cx0YDRjCcsJ9CU0LXQutCw0LHRgNGMJ10KICB9LAogIGVuOnsKICAgIGxvZ29fdGFnOidQcmVtaXVtIGdyb29taW5nPGJyPnNhbG9uIGluIFRhbGxpbm4nLAogICAgY2hvb3NlX2hvdzonQ2hvb3NlIGhvdyB0byBjb25uZWN0JywKICAgIGJvb2tfb25saW5lOidCb29rIE9ubGluZScsCiAgICBib29rX2Zsb3c6J0JyZWVkIOKGkiBTZXJ2aWNlIOKGkiBNYXN0ZXIg4oaSIFRpbWUnLAogICAgb3JfY29udGFjdDonb3IgY29udGFjdCB1cycsCiAgICBjYWxsX3VzOidDYWxsIFVzJywKICAgIGJhY2s6J+KGkCBCYWNrJywKICAgIGxvZ29fc3ViOidHcm9vbWluZyDCtyBUYWxsaW5uJywKICAgIHBzX3NlcnZpY2U6J1NlcnZpY2UnLHBzX21hc3RlcjonTWFzdGVyJyxwc19wZXQ6J1BldCcscHNfZGF0ZTonRGF0ZScscHNfZGV0YWlsczonRGV0YWlscycsCiAgICBzdGVwMV9sYmw6JzAxIMK3IERvZyBicmVlZCcsCiAgICBicmVlZF9waDonU3RhcnQgdHlwaW5nIGJyZWVkLi4uJywKICAgIHN0ZXAyX2xibDonMDIgwrcgU2VydmljZScsCiAgICBzdGVwMl9tYXN0ZXI6J0Nob29zZSBtYXN0ZXInLAogICAgc3RlcDNfbGJsOidIb3cgbG9uZyBhZ28gd2FzIHlvdXIgbGFzdCBncm9vbWluZz8nLAogICAgZzE6J0ZpcnN0IHRpbWUnLGcyOicx4oCTMyBtb250aHMgYWdvJyxnMzonM+KAkzYgbW9udGhzIGFnbycsZzQ6J092ZXIgNiBtb250aHMnLAogICAgc3RlcDRfbGJsOidDaG9vc2UgZGF0ZScsCiAgICBjYWxfYXZhaWw6J0F2YWlsYWJsZScsY2FsX25vbmU6J05vdCBhdmFpbGFibGUnLAogICAgc3RlcDRfdGltZTonQ2hvb3NlIHRpbWUnLAogICAgc3RlcDVfbGJsOidZb3VyIGRldGFpbHMnLAogICAgbGJsX25hbWU6J05hbWUnLHBoX25hbWU6J1lvdXIgbmFtZScsCiAgICBsYmxfcGhvbmU6J1Bob25lJyxsYmxfZW1haWw6J0VtYWlsJywKICAgIGxibF9wZXQ6IlBldCdzIG5hbWUiLHBoX29wdGlvbmFsOidPcHRpb25hbCcsCiAgICBjb25maXJtX2J0bjonQ29uZmlybSBib29raW5nJywKICAgIHN1Y2Nlc3NfdGl0bGU6J0Jvb2tpbmcgY29uZmlybWVkIScsCiAgICBzdWNjZXNzX3N1YjonV2Ugd2lsbCBjb250YWN0IHlvdSB0byBjb25maXJtLjxicj5UaGFuayB5b3UgZm9yIGNob29zaW5nIFImYW1wO0ogR3Jvb21pbmchJywKICAgIHRvX2hvbWU6J+KGkCBIb21lJywKICAgIGFsZXJ0X2ZpbGw6J1BsZWFzZSBlbnRlciBuYW1lIGFuZCBwaG9uZScsYWxlcnRfcGhvbmU6J0VudGVyIHBob25lIG51bWJlciBpbiBmb3JtYXQgKzM3MjEyMzQ1Njc4JywKICAgIHNlbmRpbmc6J1NlbmRpbmcuLi4nLAogICAgc3VtX2JyZWVkOidCcmVlZCcsc3VtX3NlcnZpY2U6J1NlcnZpY2UnLHN1bV9tYXN0ZXI6J01hc3Rlcicsc3VtX2dyb29tOidMYXN0IGdyb29taW5nJyxzdW1fZGF0ZTonRGF0ZScsc3VtX3RpbWU6J1RpbWUnLHN1bV9wcmljZTonUHJpY2UnLAogICAgbW9udGhzOlsnSmFudWFyeScsJ0ZlYnJ1YXJ5JywnTWFyY2gnLCdBcHJpbCcsJ01heScsJ0p1bmUnLCdKdWx5JywnQXVndXN0JywnU2VwdGVtYmVyJywnT2N0b2JlcicsJ05vdmVtYmVyJywnRGVjZW1iZXInXQogIH0sCiAgZXQ6ewogICAgbG9nb190YWc6J0VzbWFrbGFzc2lsaW5lIGhvb2xkdXN0ZWVudXM8YnI+VGFsbGlubmFzJywKICAgIGNob29zZV9ob3c6J1ZhbGkgw7xoZW5kdXN2aWlzJywKICAgIGJvb2tfb25saW5lOidCcm9uZWVyaSB2ZWViaXMnLAogICAgYm9va19mbG93OidUw7V1ZyDihpIgVGVlbnVzIOKGkiBNZWlzdGVyIOKGkiBBZWcnLAogICAgb3JfY29udGFjdDondsO1aSB2w7V0YSDDvGhlbmR1c3QnLAogICAgY2FsbF91czonSGVsaXN0YSBtZWlsZScsCiAgICBiYWNrOifihpAgVGFnYXNpJywKICAgIGxvZ29fc3ViOidHcm9vbWluZyDCtyBUYWxsaW5uJywKICAgIHBzX3NlcnZpY2U6J1RlZW51cycscHNfbWFzdGVyOidNZWlzdGVyJyxwc19wZXQ6J0xlbW1pa2xvb20nLHBzX2RhdGU6J0t1dXDDpGV2Jyxwc19kZXRhaWxzOidBbmRtZWQnLAogICAgc3RlcDFfbGJsOicwMSDCtyBLb2VyYSB0w7V1ZycsCiAgICBicmVlZF9waDonQWx1c3RhZ2UgdMO1dSBzaXNlc3RhbWlzdC4uLicsCiAgICBzdGVwMl9sYmw6JzAyIMK3IFRlZW51cycsCiAgICBzdGVwMl9tYXN0ZXI6J1ZhbGkgbWVpc3RlcicsCiAgICBzdGVwM19sYmw6J01pbGxhbCBrw6Rpc2l0ZSB2aWltYXRpIGdyb29taW5ndXM/JywKICAgIGcxOidFc2ltZXN0IGtvcmRhJyxnMjonMeKAkzMga3V1ZCB0YWdhc2knLGczOicz4oCTNiBrdXVkIHRhZ2FzaScsZzQ6J8OcbGUgNiBrdXUnLAogICAgc3RlcDRfbGJsOidWYWxpIGt1dXDDpGV2JywKICAgIGNhbF9hdmFpbDonVmFidSBhZWd1IG9uJyxjYWxfbm9uZTonVmFidSBhZWd1IHBvbGUnLAogICAgc3RlcDRfdGltZTonVmFsaSBrZWxsYWFlZycsCiAgICBzdGVwNV9sYmw6J1RlaWUgYW5kbWVkJywKICAgIGxibF9uYW1lOidOaW1pJyxwaF9uYW1lOidUZWllIG5pbWknLAogICAgbGJsX3Bob25lOidUZWxlZm9uJyxsYmxfZW1haWw6J0VtYWlsJywKICAgIGxibF9wZXQ6J0xlbW1pa2xvb21hIG5pbWknLHBoX29wdGlvbmFsOidWYWxpa3VsaW5lJywKICAgIGNvbmZpcm1fYnRuOidLaW5uaXRhIGJyb25lZXJpbmcnLAogICAgc3VjY2Vzc190aXRsZTonQnJvbmVlcmluZyBraW5uaXRhdHVkIScsCiAgICBzdWNjZXNzX3N1YjonVsO1dGFtZSB0ZWllZ2Egw7xoZW5kdXN0IGtpbm5pdGFtaXNla3MuPGJyPlTDpG5hbWUsIGV0IHZhbGlzaXRlIFImYW1wO0ogR3Jvb21pbmchJywKICAgIHRvX2hvbWU6J+KGkCBBdmFsZWhlbGUnLAogICAgYWxlcnRfZmlsbDonUGFsdW4gc2lzZXN0YWdlIG5pbWkgamEgdGVsZWZvbicsYWxlcnRfcGhvbmU6J1Npc2VzdGFnZSB0ZWxlZm9uaW51bWJlciB2b3JtaW5ndXMgKzM3MjEyMzQ1Njc4JywKICAgIHNlbmRpbmc6J1NhYWRhbi4uLicsCiAgICBzdW1fYnJlZWQ6J1TDtXVnJyxzdW1fc2VydmljZTonVGVlbnVzJyxzdW1fbWFzdGVyOidNZWlzdGVyJyxzdW1fZ3Jvb206J1ZpaW1hbmUgZ3Jvb21pbmcnLHN1bV9kYXRlOidLdXVww6Rldicsc3VtX3RpbWU6J0tlbGxhYWVnJyxzdW1fcHJpY2U6J0hpbmQnLAogICAgbW9udGhzOlsnSmFhbnVhcicsJ1ZlZWJydWFyJywnTcOkcnRzJywnQXByaWxsJywnTWFpJywnSnV1bmknLCdKdXVsaScsJ0F1Z3VzdCcsJ1NlcHRlbWJlcicsJ09rdG9vYmVyJywnTm92ZW1iZXInLCdEZXRzZW1iZXInXQogIH0KfTsKCmZ1bmN0aW9uIHNldExhbmcobCl7CiAgTEFORz1sOwogIGxvY2FsU3RvcmFnZS5zZXRJdGVtKCdyamxhbmcnLGwpOwogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5sYW5nLWJ0bicpLmZvckVhY2goZnVuY3Rpb24oYil7CiAgICBiLmNsYXNzTGlzdC50b2dnbGUoJ2FjdGl2ZScsIGIudGV4dENvbnRlbnQudG9Mb3dlckNhc2UoKT09PWwpOwogIH0pOwogIHZhciB0cj1UW2xdOwogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tkYXRhLWkxOG5dJykuZm9yRWFjaChmdW5jdGlvbihlbCl7CiAgICB2YXIgaz1lbC5nZXRBdHRyaWJ1dGUoJ2RhdGEtaTE4bicpOwogICAgaWYodHJba10hPT11bmRlZmluZWQpIGVsLmlubmVySFRNTD10cltrXTsKICB9KTsKICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCdbZGF0YS1pMThuLXBoXScpLmZvckVhY2goZnVuY3Rpb24oZWwpewogICAgdmFyIGs9ZWwuZ2V0QXR0cmlidXRlKCdkYXRhLWkxOG4tcGgnKTsKICAgIGlmKHRyW2tdIT09dW5kZWZpbmVkKSBlbC5wbGFjZWhvbGRlcj10cltrXTsKICB9KTsKICBNT05USFM9dHIubW9udGhzOwogIGJ1aWxkQ2FsKCk7CiAgLy8gUmUtcmVuZGVyIGJhZGdlIGFuZCBzZXJ2aWNlcyBpZiBicmVlZCBhbHJlYWR5IHNlbGVjdGVkCiAgaWYoc2VsQnJlZWQpewogICAgdmFyIGJmPWw9PT0nZW4nPydicmVlZF9lbic6bD09PSdldCc/J2JyZWVkX2V0JzonYnJlZWQnOwogICAgdmFyIGRiPXNlbEJyZWVkW2JmXXx8c2VsQnJlZWQuYnJlZWQ7CiAgICBib29raW5nLmJyZWVkRGlzcGxheT1kYjsKICAgIHZhciBibkVsPWRvY3VtZW50LnF1ZXJ5U2VsZWN0b3IoJyNzQmFkZ2UgLmJuYW1lJyk7CiAgICBpZihibkVsKSBibkVsLnRleHRDb250ZW50PWRiOwogICAgdmFyIGJjRWw9ZG9jdW1lbnQucXVlcnlTZWxlY3RvcignI3NCYWRnZSAuYmNoZycpOwogICAgaWYoYmNFbCkgYmNFbC50ZXh0Q29udGVudD1sPT09J2VuJz8nQ2hhbmdlJzpsPT09J2V0Jz8nTXV1ZGEnOifQmNC30LzQtdC90LjRgtGMJzsKICAgIGlmKGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdmNTZWMnKS5zdHlsZS5kaXNwbGF5IT09J25vbmUnKSByZW5kZXJTdmNzKHNlbEJyZWVkKTsKICAgIHZhciBzbj1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjTm90ZScpOwogICAgaWYoc24pewogICAgICB2YXIgbnQ9bD09PSdlbic/J1BsZWFzZSBub3RlJzpsPT09J2V0Jz8nUGFuZ2UgdMOkaGVsZSc6J9CS0LDQttC90L4g0LfQvdCw0YLRjCc7CiAgICAgIHZhciBuYj1sPT09J2VuJz8nRmluYWwgcHJpY2UgZGVwZW5kcyBvbiBjb2F0IGNvbmRpdGlvbiBhbmQgcGV0IGJlaGF2aW91ci48YnI+RGVtYXR0aW5nIGZyb20gNSDigqwuPGJyPkFnZ3Jlc3NpdmUgYmVoYXZpb3VyIHN1cmNoYXJnZSBtYXkgYXBwbHk6ICs1MCUuJzpsPT09J2V0Jz8nTMO1cGxpayBoaW5kIHPDtWx0dWIga2FydmFzdGlrdSBzZWlzdW5kaXN0IGphIGxlbW1pa2xvb21hIGvDpGl0dW1pc2VzdC48YnI+S29sdHN1bml0ZSBsYWh0aWhhcnV0YW1pbmUgYWxhdGVzIDUg4oKsLjxicj5BZ3Jlc3NpaXZzZSBrw6RpdHVtaXNlIGtvcnJhbCB2w7VpYiBsaXNhbmR1ZGEgNTAlIGp1dXJkZWhpbmRsdXMuJzon0J7QutC+0L3Rh9Cw0YLQtdC70YzQvdCw0Y8g0YHRgtC+0LjQvNC+0YHRgtGMINC30LDQstC40YHQuNGCINC+0YIg0YHQvtGB0YLQvtGP0L3QuNGPINGI0LXRgNGB0YLQuCDQuCDQv9C+0LLQtdC00LXQvdC40Y8g0L/QuNGC0L7QvNGG0LAuPGJyPtCg0LDQt9Cx0L7RgCDQutC+0LvRgtGD0L3QvtCyIOKAlCDQvtGCIDUg4oKsLjxicj7Qn9GA0Lgg0LDQs9GA0LXRgdGB0LjQstC90L7QvCDQv9C+0LLQtdC00LXQvdC40Lgg0LzQvtC20LXRgiDQv9GA0LjQvNC10L3Rj9GC0YzRgdGPINC00L7Qv9C70LDRgtCwIDUwJS4nOwogICAgICBzbi5pbm5lckhUTUw9JzxkaXYgc3R5bGU9ImZvbnQtc2l6ZTowLjgzOHJlbTtsZXR0ZXItc3BhY2luZzouMTVlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjhweDtmb250LXdlaWdodDo2MDA7Zm9udC1mYW1pbHk6XCdNb250c2VycmF0XCcsc2Fucy1zZXJpZiI+JytudCsnPC9kaXY+PGRpdiBzdHlsZT0iZm9udC1zaXplOjEuMDI1cmVtO2NvbG9yOiNmZmZmZmY7bGluZS1oZWlnaHQ6MS44O2ZvbnQtZmFtaWx5OlwnTW9udHNlcnJhdFwnLHNhbnMtc2VyaWYiPicrbmIrJzwvZGl2Pic7CiAgICB9CiAgfQp9CgovLyBBcHBseSBzYXZlZCBsYW5ndWFnZSBvbiBsb2FkCihmdW5jdGlvbigpeyBzZXRMYW5nKExBTkcpOyB9KSgpOwoKLy8gQ2FsbGJhY2sgZm9ybQpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2FsbGJhY2tCdG4nKS5vbmNsaWNrID0gZnVuY3Rpb24oKXsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrTW9kYWwnKS5zdHlsZS5kaXNwbGF5ID0gJ2ZsZXgnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtOYW1lJykudmFsdWUgPSAnJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrUGhvbmUnKS52YWx1ZSA9ICcnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtTdWNjZXNzJykuc3R5bGUuZGlzcGxheSA9ICdub25lJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrU3VibWl0Jykuc3R5bGUuZGlzcGxheSA9ICdibG9jayc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia0Nsb3NlJykudGV4dENvbnRlbnQgPSAn0J7RgtC80LXQvdCwJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrU3VibWl0JykudGV4dENvbnRlbnQgPSAn0J7RgtC/0YDQsNCy0LjRgtGMJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrU3VibWl0JykuZGlzYWJsZWQgPSBmYWxzZTsKfTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia0Nsb3NlJykub25jbGljayA9IGZ1bmN0aW9uKCl7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia01vZGFsJykuc3R5bGUuZGlzcGxheSA9ICdub25lJzsKfTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1N1Ym1pdCcpLm9uY2xpY2sgPSBmdW5jdGlvbigpewogIHZhciBuYW1lID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia05hbWUnKS52YWx1ZS50cmltKCk7CiAgdmFyIHBob25lID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1Bob25lJykudmFsdWUudHJpbSgpLnJlcGxhY2UoL1xEL2csJycpOwogIGlmKCFuYW1lIHx8ICFwaG9uZSl7YWxlcnQoJ9CS0LLQtdC00LjRgtC1INC40LzRjyDQuCDRgtC10LvQtdGE0L7QvScpO3JldHVybjt9CiAgdmFyIGJ0biA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtTdWJtaXQnKTsKICBidG4udGV4dENvbnRlbnQgPSAn0J7RgtC/0YDQsNCy0LvRj9C10LwuLi4nOyBidG4uZGlzYWJsZWQgPSB0cnVlOwogIGZldGNoKCcvYXBpL2NhbGxiYWNrJyx7CiAgICBtZXRob2Q6J1BPU1QnLAogICAgaGVhZGVyczp7J0NvbnRlbnQtVHlwZSc6J2FwcGxpY2F0aW9uL2pzb24nfSwKICAgIGJvZHk6SlNPTi5zdHJpbmdpZnkoe25hbWU6bmFtZSwgcGhvbmU6JyszNzInK3Bob25lfSkKICB9KS50aGVuKGZ1bmN0aW9uKCl7CiAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrU3VjY2VzcycpLnN0eWxlLmRpc3BsYXkgPSAnYmxvY2snOwogICAgYnRuLnN0eWxlLmRpc3BsYXkgPSAnbm9uZSc7CiAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrQ2xvc2UnKS50ZXh0Q29udGVudCA9ICfihpAg0JfQsNC60YDRi9GC0YwnOwogICAgc2V0VGltZW91dChmdW5jdGlvbigpe2RvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtNb2RhbCcpLnN0eWxlLmRpc3BsYXk9J25vbmUnO30sMzAwMCk7CiAgfSkuY2F0Y2goZnVuY3Rpb24oKXsKICAgIGJ0bi50ZXh0Q29udGVudCA9ICfQntGC0L/RgNCw0LLQuNGC0YwnOyBidG4uZGlzYWJsZWQgPSBmYWxzZTsKICAgIGFsZXJ0KCfQntGI0LjQsdC60LAuINCf0L7Qv9GA0L7QsdGD0LnRgtC1INC10YnRkSDRgNCw0LcuJyk7CiAgfSk7Cn07Cgo8L3NjcmlwdD4KPC9ib2R5Pgo8L2h0bWw+Cg=="



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
