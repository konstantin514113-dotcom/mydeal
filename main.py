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
BOOKING_HTML_B64 = "77u/PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9InJ1Ij4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idGhlbWUtY29sb3IiIGNvbnRlbnQ9IiMwYTBhMGEiPgo8bWV0YSBuYW1lPSJ2aWV3cG9ydCIgY29udGVudD0id2lkdGg9ZGV2aWNlLXdpZHRoLGluaXRpYWwtc2NhbGU9MSI+Cjx0aXRsZT5SJkogR3Jvb21pbmc8L3RpdGxlPgo8bGluayBocmVmPSJodHRwczovL2ZvbnRzLmdvb2dsZWFwaXMuY29tL2NzczI/ZmFtaWx5PUNvcm1vcmFudCtHYXJhbW9uZDp3Z2h0QDQwMDs2MDAmZmFtaWx5PVBsYXlmYWlyK0Rpc3BsYXk6aXRhbCx3Z2h0QDAsNDAwOzAsNjAwOzAsNzAwOzEsNDAwJmZhbWlseT1Nb250c2VycmF0OndnaHRAMzAwOzQwMDs1MDA7NjAwJmRpc3BsYXk9c3dhcCIgcmVsPSJzdHlsZXNoZWV0Ij4KPHN0eWxlPgoqe2JveC1zaXppbmc6Ym9yZGVyLWJveDttYXJnaW46MDtwYWRkaW5nOjB9Cmh0bWwsYm9keXttaW4taGVpZ2h0OjEwMHZoO2JhY2tncm91bmQ6IzBhMGEwYTtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXdlaWdodDo0MDB9Ci5zY3JlZW57ZGlzcGxheTpub25lO21pbi1oZWlnaHQ6MTAwdmg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2FsaWduLWl0ZW1zOmNlbnRlcjtwYWRkaW5nOjQ4cHggMCA2NHB4fQouc2NyZWVuLmFjdGl2ZXtkaXNwbGF5OmZsZXh9Ci5jb257d2lkdGg6MTAwJTttYXgtd2lkdGg6NDAwcHg7cGFkZGluZzowIDI4cHh9Ci5iYWNrLWJ0bntkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo4cHg7Y29sb3I6I2ZmZmZmZjtmb250LXNpemU6MC44cmVtO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyOm5vbmU7Y3Vyc29yOnBvaW50ZXI7cGFkZGluZzowO21hcmdpbi1ib3R0b206MzZweDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXdlaWdodDozMDA7dHJhbnNpdGlvbjpjb2xvciAuMnN9Ci5iYWNrLWJ0bjpob3Zlcntjb2xvcjojZmZmZmZmfQoubG9nby1yantmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZjtmb250LXNpemU6Mi41cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmfQoubG9nby1zdWJ7Zm9udC1zaXplOjAuNjYzcmVtO2ZvbnQtd2VpZ2h0OjYwMDtsZXR0ZXItc3BhY2luZzouNGVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO21hcmdpbi10b3A6M3B4O3BhZGRpbmctYm90dG9tOjE0cHg7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4yKTttYXJnaW4tYm90dG9tOjIwcHh9Ci5ob21lLXJqe2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmO2ZvbnQtc2l6ZTozLjI1cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjF9Ci5sb2dvLXRhZ3tmb250LXNpemU6MC43NXJlbTtsZXR0ZXItc3BhY2luZzouMTJlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjtsaW5lLWhlaWdodDoxLjV9Ci5sb2dvLXJvd3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6ZmxleC1lbmQ7Z2FwOjEycHg7bWFyZ2luLWJvdHRvbToyOHB4O3BhZGRpbmctYm90dG9tOjE4cHg7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4yKX0KLmxvZ28taW1nLXJvd3ttYXJnaW4tYm90dG9tOjI4cHg7cGFkZGluZy1ib3R0b206MThweDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjIpfQoubG9nby1pbWd7aGVpZ2h0OjkwcHg7d2lkdGg6YXV0bztkaXNwbGF5OmJsb2NrfQouaG9tZS1nc3Vie2ZvbnQtc2l6ZTowLjY2M3JlbTtmb250LXdlaWdodDo2MDA7bGV0dGVyLXNwYWNpbmc6LjRlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjttYXJnaW4tdG9wOjZweDttYXJnaW4tYm90dG9tOjIycHh9Ci5ob21lLWgxe2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6My4xMjVyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNmZmZmZmY7bGluZS1oZWlnaHQ6MS4xO21hcmdpbi1ib3R0b206NnB4fQouaG9tZS1oMSBlbXtmb250LXN0eWxlOml0YWxpYztjb2xvcjojZmZmZmZmfQouaG9tZS1zdWJ7Zm9udC1zaXplOjAuOHJlbTtsZXR0ZXItc3BhY2luZzouMThlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjI4cHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5vcHR7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTZweDtwYWRkaW5nOjE2cHggMDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNyk7dGV4dC1kZWNvcmF0aW9uOm5vbmU7Y29sb3I6I2ZmZmZmZjt0cmFuc2l0aW9uOmNvbG9yIC4ycztjdXJzb3I6cG9pbnRlcjtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyLXRvcDpub25lO2JvcmRlci1sZWZ0Om5vbmU7Ym9yZGVyLXJpZ2h0Om5vbmU7d2lkdGg6MTAwJTtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWZ9Ci5vcHQ6aG92ZXJ7Y29sb3I6I2ZmZn0KLm9wdC1pY29ue3dpZHRoOjM4cHg7aGVpZ2h0OjM4cHg7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2ZsZXgtc2hyaW5rOjB9Ci5vcHQtaWNvbi1pbWd7d2lkdGg6MzhweDtoZWlnaHQ6MzhweDtvYmplY3QtZml0OmNvbnRhaW59Ci5vcHQtdGV4dHtmbGV4OjE7dGV4dC1hbGlnbjpsZWZ0fQoub3B0LXRpdGxle2ZvbnQtc2l6ZToxLjUxMnJlbTtmb250LXdlaWdodDo2MDA7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjJweDt0cmFuc2l0aW9uOmNvbG9yIC4ycztmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWZ9Ci5vcHQ6aG92ZXIgLm9wdC10aXRsZXtjb2xvcjojZmZmfQoub3B0LWhhbmRsZXtmb250LXNpemU6MC44ODdyZW07Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXdlaWdodDozMDB9Ci5vcHQtYXJyb3d7Y29sb3I6I2ZmZmZmZjtmb250LXNpemU6MS4yMjVyZW07ZmxleC1zaHJpbms6MDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLm9wdDpob3ZlciAub3B0LWFycm93e2NvbG9yOiNmZmZmZmZ9Ci5kaXZpZGVye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7cGFkZGluZzoxMnB4IDB9Ci5kaXZpZGVyOjpiZWZvcmUsLmRpdmlkZXI6OmFmdGVye2NvbnRlbnQ6Jyc7ZmxleDoxO2hlaWdodDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNil9Ci5kaXZpZGVyIHNwYW57Zm9udC1zaXplOjAuNjg4cmVtO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQouaG9tZS1mb290e21hcmdpbi10b3A6MzZweDtwYWRkaW5nLXRvcDoyMHB4O2JvcmRlci10b3A6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA2KTtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6Y2VudGVyfQouaG9tZS1mb290IHNwYW57Zm9udC1zaXplOjAuNzc1cmVtO2xldHRlci1zcGFjaW5nOi4yZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5mZG90e3dpZHRoOjJweDtoZWlnaHQ6MnB4O2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMTYpfQoucHJvZ3Jlc3N7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjttYXJnaW4tYm90dG9tOjQwcHg7b3ZlcmZsb3c6aGlkZGVuO2NvdW50ZXItcmVzZXQ6c3RlcH0KLnBze2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjVweDtmb250LXNpemU6MC42NjNyZW07bGV0dGVyLXNwYWNpbmc6LjEyZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7d2hpdGUtc3BhY2U6bm93cmFwO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2NvdW50ZXItaW5jcmVtZW50OnN0ZXB9Ci5wcy5kb25le2NvbG9yOiNmZmZmZmZ9Ci5wcy5hY3RpdmV7Y29sb3I6I2ZmZmZmZn0KLnBkb3R7d2lkdGg6MThweDtoZWlnaHQ6MThweDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7ZmxleC1zaHJpbms6MDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEyKTtmb250LXNpemU6MC42NjNyZW07Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC13ZWlnaHQ6NjAwfQoucGRvdDo6YmVmb3Jle2NvbnRlbnQ6Y291bnRlcihzdGVwLGRlY2ltYWwtbGVhZGluZy16ZXJvKX0KLnBzLmRvbmUgLnBkb3R7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLnBzLmFjdGl2ZSAucGRvdHtib3JkZXItY29sb3I6I2ZmZmZmZjtjb2xvcjojZmZmZmZmfQoucGx7ZmxleDoxO2hlaWdodDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNyk7bWFyZ2luOjAgNXB4O21pbi13aWR0aDo2cHh9Ci5wbC5kb25le2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMTgpfQouc3RlcHtkaXNwbGF5Om5vbmV9LnN0ZXAuc2hvd3tkaXNwbGF5OmJsb2NrO2FuaW1hdGlvbjpmdSAuMzVzIGVhc2UgYm90aH0KLnNsYmx7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjkzOHJlbTtmb250LXdlaWdodDo2MDA7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjIwcHg7bGV0dGVyLXNwYWNpbmc6LjAxZW19Ci5zYm94e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEwcHg7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTYpO3BhZGRpbmc6MCAycHg7dHJhbnNpdGlvbjpib3JkZXItY29sb3IgLjJzfQouc2JveDpmb2N1cy13aXRoaW57Ym9yZGVyLWJvdHRvbS1jb2xvcjojZmZmZmZmfQouc2l7b3BhY2l0eTouMjtmb250LXNpemU6MS4yMjVyZW07ZmxleC1zaHJpbms6MH0KI2JJbnB1dHtmbGV4OjE7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtvdXRsaW5lOm5vbmU7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjUxMnJlbTtjb2xvcjojZmZmZmZmO3BhZGRpbmc6MTJweCAwfQojYklucHV0OjpwbGFjZWhvbGRlcntjb2xvcjojZmZmZmZmfQouY2xye2JhY2tncm91bmQ6bm9uZTtib3JkZXI6bm9uZTtjb2xvcjojZmZmZmZmO2N1cnNvcjpwb2ludGVyO2ZvbnQtc2l6ZToxLjE1cmVtO2Rpc3BsYXk6bm9uZTtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZn0KLmNsci5zaG93e2Rpc3BsYXk6YmxvY2t9Ci5id3JhcHtwb3NpdGlvbjpyZWxhdGl2ZTttYXJnaW4tYm90dG9tOjIwcHh9Ci5kcm9we3Bvc2l0aW9uOmFic29sdXRlO2xlZnQ6MDtyaWdodDowO2JhY2tncm91bmQ6IzBmMGYwZjtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEpO2JvcmRlci10b3A6bm9uZTttYXgtaGVpZ2h0OjIwMHB4O292ZXJmbG93LXk6YXV0bzt6LWluZGV4OjUwO2Rpc3BsYXk6bm9uZX0KLmRyb3Aub3BlbntkaXNwbGF5OmJsb2NrfQouZGl0ZW17cGFkZGluZzoxMXB4IDE0cHg7Zm9udC1zaXplOjEuMzYzcmVtO2NvbG9yOiNmZmZmZmY7Y3Vyc29yOnBvaW50ZXI7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDUpO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLmRpdGVtOmhvdmVye2NvbG9yOiNmZmZ9Ci5kaXRlbSBtYXJre2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Y29sb3I6I2ZmZjtmb250LXdlaWdodDo3MDB9Ci5ub3Jlc3twYWRkaW5nOjE0cHg7Zm9udC1zaXplOjEuMjg4cmVtO2NvbG9yOiNmZmZmZmY7Zm9udC1zdHlsZTppdGFsaWM7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmfQoubm8tYnJlZWQtYmFubmVye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7cGFkZGluZzoxNHB4IDA7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDYpO2N1cnNvcjpwb2ludGVyO3RyYW5zaXRpb246Y29sb3IgLjJzO21hcmdpbi10b3A6NHB4fQoubm8tYnJlZWQtYmFubmVyOmhvdmVyIC5uby1icmVlZC1iYW5uZXItdGl0bGV7Y29sb3I6I2ZmZmZmZn0KLm5vLWJyZWVkLWJhbm5lci1pY29ue2ZvbnQtc2l6ZToxLjU3NXJlbTtmbGV4LXNocmluazowO29wYWNpdHk6LjN9Ci5uby1icmVlZC1iYW5uZXItdGV4dHtmbGV4OjF9Ci5uby1icmVlZC1iYW5uZXItdGl0bGV7Zm9udC1zaXplOjEuNDM4cmVtO2NvbG9yOiNmZmZmZmY7Zm9udC13ZWlnaHQ6NjAwO21hcmdpbi1ib3R0b206MnB4O2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLm5vLWJyZWVkLWJhbm5lci1zdWJ7Zm9udC1zaXplOjAuODg3cmVtO2NvbG9yOiNmZmZmZmY7bGluZS1oZWlnaHQ6MS41O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQoubm8tYnJlZWQtYmFubmVyLWFycm93e2NvbG9yOiNmZmZmZmY7Zm9udC1zaXplOjEuMjI1cmVtO2ZsZXgtc2hyaW5rOjA7dHJhbnNpdGlvbjpjb2xvciAuMnN9Ci5zYmFkZ2V7ZGlzcGxheTpub25lO2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTJweDttYXJnaW4tYm90dG9tOjIwcHh9Ci5zYmFkZ2Uuc2hvd3tkaXNwbGF5OmZsZXh9Ci5ibmFtZXtib3JkZXItYm90dG9tOjFweCBzb2xpZCAjZmZmZmZmO2NvbG9yOiNmZmZmZmY7cGFkZGluZzoycHggMDtmb250LXNpemU6MS40MzhyZW07Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmfQouYmNoZ3tmb250LXNpemU6MC44cmVtO2NvbG9yOiNmZmZmZmY7Y3Vyc29yOnBvaW50ZXI7bGV0dGVyLXNwYWNpbmc6LjEyZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO3RyYW5zaXRpb246Y29sb3IgLjJzfQouYmNoZzpob3Zlcntjb2xvcjojZmZmZmZmfQouc3ZidG57ZGlzcGxheTpibG9jaztwYWRkaW5nOjA7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wOCk7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Y3Vyc29yOnBvaW50ZXI7dGV4dC1hbGlnbjpsZWZ0O3RyYW5zaXRpb246Ym9yZGVyLWNvbG9yIC4yczt3aWR0aDoxMDAlO292ZXJmbG93OmhpZGRlbjtwb3NpdGlvbjpyZWxhdGl2ZX0KLnN2YnRuOmhvdmVye2JvcmRlci1ib3R0b20tY29sb3I6I2ZmZmZmZn0KLnN2YnRuLmFjdGl2ZXtib3JkZXItYm90dG9tLWNvbG9yOiNmZmZmZmZ9Ci5zdnB7Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNmZmZmZmY7ZmxleC1zaHJpbms6MH0KLm1hc3RlcnN7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczoxZnIgMWZyO2dhcDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNyl9Ci5tYnRue2JhY2tncm91bmQ6IzBhMGEwYTtwYWRkaW5nOjIycHggMTJweDt0ZXh0LWFsaWduOmNlbnRlcjtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmJhY2tncm91bmQgLjJzO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtib3JkZXI6bm9uZX0KLm1idG46aG92ZXJ7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wMyl9Ci5tYnRuLmFjdGl2ZXtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA1KX0KLm1hdnt3aWR0aDo0MHB4O2hlaWdodDo0MHB4O2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xNCk7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO21hcmdpbjowIGF1dG8gMTBweDtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjEuNDM4cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmfQoubWJ0bi5hY3RpdmUgLm1hdntib3JkZXItY29sb3I6I2ZmZmZmZjtjb2xvcjojZmZmZmZmfQoubW5hbWV7Zm9udC1zaXplOjEuNDM4cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZn0KLm1idG46aG92ZXIgLm1uYW1le2NvbG9yOiNmZmZmZmZ9Ci5tYnRuLmFjdGl2ZSAubW5hbWV7Y29sb3I6I2ZmZmZmZn0KLm10aXRsZXtmb250LXNpemU6MC44cmVtO2NvbG9yOiNmZmZmZmY7bWFyZ2luLXRvcDozcHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5nYnRue2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7cGFkZGluZzoxNHB4IDA7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wOCk7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjEuNDM4cmVtO2N1cnNvcjpwb2ludGVyO3dpZHRoOjEwMCU7dHJhbnNpdGlvbjphbGwgLjJzfQouZ2J0bjpob3Zlcntjb2xvcjojZmZmZmZmfQouZ2J0bi5hY3RpdmV7Y29sb3I6I2ZmZmZmZjtib3JkZXItYm90dG9tLWNvbG9yOiNmZmZmZmZ9Ci5jYWwtaHtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6Y2VudGVyO21hcmdpbi1ib3R0b206MTZweH0KLmNhbC1te2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6MS45MzhyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNmZmZmZmZ9Ci5jYWwtbntiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyOm5vbmU7Y29sb3I6I2ZmZmZmZjtjdXJzb3I6cG9pbnRlcjtmb250LXNpemU6MS41NzVyZW07cGFkZGluZzo0cHggOHB4O3RyYW5zaXRpb246Y29sb3IgLjJzfQouY2FsLW46aG92ZXJ7Y29sb3I6I2ZmZmZmZn0KLmNne2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDcsMWZyKTtnYXA6MnB4O21hcmdpbi1ib3R0b206MTJweH0KLmNkbnt0ZXh0LWFsaWduOmNlbnRlcjtmb250LXNpemU6MC42NjNyZW07Y29sb3I6I2ZmZmZmZjtwYWRkaW5nOjRweCAwO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtsZXR0ZXItc3BhY2luZzouMWVtfQouY2R7dGV4dC1hbGlnbjpjZW50ZXI7Y3Vyc29yOnBvaW50ZXI7Y29sb3I6I2ZmZmZmZjtib3JkZXI6MXB4IHNvbGlkIHRyYW5zcGFyZW50O3RyYW5zaXRpb246YWxsIC4yc30KLmNkOmhvdmVyOm5vdCguZGlzKTpub3QoLnBhZCkgLmNkLWlubmVye2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDcpIWltcG9ydGFudDtjb2xvcjojZmZmZmZmIWltcG9ydGFudH0KLmNkLnNlbCAuY2QtaW5uZXJ7YmFja2dyb3VuZDojZmZmZmZmIWltcG9ydGFudDtjb2xvcjojMGEwYTBhIWltcG9ydGFudDtmb250LXdlaWdodDo3MDAhaW1wb3J0YW50O2JvcmRlcjpub25lIWltcG9ydGFudH0KLmNkLnRvZCAuY2QtaW5uZXJ7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4yOCk7Y29sb3I6I2ZmZn0KLmNkLmRpc3tjb2xvcjojZmZmZmZmO2N1cnNvcjpkZWZhdWx0fQoudGd7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoNCwxZnIpO2dhcDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNyl9Ci50YnRue2JhY2tncm91bmQ6IzBhMGEwYTtib3JkZXI6bm9uZTtwYWRkaW5nOjEzcHggNHB4O3RleHQtYWxpZ246Y2VudGVyO2ZvbnQtc2l6ZToxLjMyNXJlbTtjb2xvcjojZmZmZmZmO2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjt0cmFuc2l0aW9uOmFsbCAuMnN9Ci50YnRuOmhvdmVye2NvbG9yOiNmZmZmZmY7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNCl9Ci50YnRuLmFjdGl2ZXtjb2xvcjojZmZmZmZmfQouc3Vte2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOm5vbmU7Ym9yZGVyLXRvcDoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDgpO3BhZGRpbmc6MjBweCAwO21hcmdpbi1ib3R0b206MjBweH0KLnNye2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjtwYWRkaW5nOjhweCAwO2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA1KTtmb250LXNpemU6MS4zNjNyZW07Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmfQouc3I6bGFzdC1jaGlsZHtib3JkZXItYm90dG9tOm5vbmU7cGFkZGluZy10b3A6MTRweH0KLnNse2NvbG9yOiNmZmZmZmZ9LnN2e2NvbG9yOiNmZmZmZmY7dGV4dC1hbGlnbjpyaWdodH0KLnNwe2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6Mi40MzhyZW07Y29sb3I6I2ZmZmZmZjtmb250LXdlaWdodDo2MDB9Ci5mZ3ttYXJnaW4tYm90dG9tOjIwcHh9Ci5mbHtmb250LXNpemU6MC43MTJyZW07bGV0dGVyLXNwYWNpbmc6LjIyZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbTo4cHg7ZGlzcGxheTpibG9jaztmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZn0KLmZpe3dpZHRoOjEwMCU7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xNCk7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjEuNTEycmVtO3BhZGRpbmc6MTBweCAwO291dGxpbmU6bm9uZTt0cmFuc2l0aW9uOmJvcmRlci1jb2xvciAuMnN9Ci5maTpmb2N1c3tib3JkZXItYm90dG9tLWNvbG9yOiNmZmZmZmZ9Ci5jYnRue2Rpc3BsYXk6YmxvY2s7d2lkdGg6MTAwJTtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC1zaXplOjAuODYycmVtO2ZvbnQtd2VpZ2h0OjYwMDtsZXR0ZXItc3BhY2luZzouMjhlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7dGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzoxNnB4O2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMjUpO2N1cnNvcjpwb2ludGVyO3RyYW5zaXRpb246YWxsIC4yc30KLmNidG46aG92ZXJ7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLnNibG9ja3t0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjUycHggMjBweDtkaXNwbGF5Om5vbmV9Ci5zYmxvY2suc2hvd3tkaXNwbGF5OmJsb2NrO2FuaW1hdGlvbjpmdSAuNXMgZWFzZSBib3RofQouc2kye2ZvbnQtc2l6ZTozLjZyZW07bWFyZ2luLWJvdHRvbToyMHB4O29wYWNpdHk6LjR9Ci5zdHtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjIuNzI1cmVtO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbToxMHB4O2ZvbnQtd2VpZ2h0OjYwMH0KLnNze2ZvbnQtc2l6ZToxLjA3NXJlbTtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjEuOTttYXJnaW4tYm90dG9tOjI4cHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5oYnRue2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xNik7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXNpemU6MC44NjJyZW07bGV0dGVyLXNwYWNpbmc6LjIyZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO3BhZGRpbmc6MTNweCAyOHB4O2N1cnNvcjpwb2ludGVyO3RyYW5zaXRpb246YWxsIC4yc30KLmhidG46aG92ZXJ7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLmxvYWRpbmctc2xvdHN7Y29sb3I6I2ZmZmZmZjtmb250LXNpemU6MS4yODhyZW07cGFkZGluZzoxMnB4IDA7dGV4dC1hbGlnbjpjZW50ZXI7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc3R5bGU6aXRhbGljfQouY2R7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpjZW50ZXI7YWxpZ24taXRlbXM6Y2VudGVyO2hlaWdodDozNnB4IWltcG9ydGFudDtwYWRkaW5nOjAhaW1wb3J0YW50fQouY2QtaW5uZXJ7d2lkdGg6MzJweDtoZWlnaHQ6MzJweDtib3JkZXItcmFkaXVzOjA7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2ZvbnQtc2l6ZToxLjE1cmVtO2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZn0KLmNkLmF2YWlsIC5jZC1pbm5lcntib3JkZXI6MXB4IHNvbGlkIHJnYmEoOTAsMTgwLDkwLC4zNSk7Y29sb3I6cmdiYSg5MCwxODAsOTAsLjY1KX0KLmNkLmJ1c3kgLmNkLWlubmVye2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDcpO2NvbG9yOiNmZmZmZmZ9Ci5jZC5zZWwgLmNkLWlubmVye2JhY2tncm91bmQ6I2ZmZmZmZiFpbXBvcnRhbnQ7Y29sb3I6IzBhMGEwYSFpbXBvcnRhbnQ7Zm9udC13ZWlnaHQ6NzAwIWltcG9ydGFudDtib3JkZXI6bm9uZSFpbXBvcnRhbnR9Ci5jZC50b2QgLmNkLWlubmVye2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMjgpO2NvbG9yOiNmZmY7Zm9udC13ZWlnaHQ6NjAwfQouY2QuZGlzIC5jZC1pbm5lcntjb2xvcjojZmZmZmZmO2N1cnNvcjpkZWZhdWx0O2JhY2tncm91bmQ6bm9uZTtib3JkZXI6bm9uZX0KLnN2YnRuLXJvd3tkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6YmFzZWxpbmU7bWFyZ2luLWJvdHRvbTo2cHg7cGFkZGluZzoxNnB4IDAgMH0KLnN2YnRuLW5hbWV7Zm9udC1zaXplOjEuNTEycmVtO2NvbG9yOiNmZmZmZmY7Zm9udC13ZWlnaHQ6NjAwO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZn0KLnN2YnRuLmFjdGl2ZSAuc3ZidG4tbmFtZXtjb2xvcjojZmZmZmZmfQouc3ZidG4tcHJpY2V7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjcyNXJlbTtjb2xvcjojZmZmZmZmO2ZvbnQtd2VpZ2h0OjYwMDtmbGV4LXNocmluazowfQouc3ZidG4uYWN0aXZlIC5zdmJ0bi1wcmljZXtjb2xvcjojZmZmZmZmfQouc3ZidG4tZGVzY3tmb250LXNpemU6MXJlbTtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjEuNztkaXNwbGF5OmJsb2NrO3BhZGRpbmc6MCAwIDE0cHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7d2hpdGUtc3BhY2U6cHJlLWxpbmV9Ci5zdmJ0bi5hY3RpdmUgLnN2YnRuLWRlc2N7Y29sb3I6I2ZmZmZmZn0KLnN2YnRuLXRhZ3tmb250LXNpemU6MC45NzVyZW07Y29sb3I6I2ZmZmZmZjtmb250LXN0eWxlOml0YWxpYztkaXNwbGF5OmJsb2NrO21hcmdpbi10b3A6MnB4O3BhZGRpbmc6MCAwIDE0cHg7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmfQouc3ZidG4uYWN0aXZlIC5zdmJ0bi10YWd7Y29sb3I6I2ZmZmZmZn0KQG1lZGlhKG1heC13aWR0aDo0MDBweCl7LnN2YnRuLW5hbWV7Zm9udC1zaXplOjEuMzYzcmVtfS5zdmJ0bi1wcmljZXtmb250LXNpemU6MS41MTJyZW19LnN2YnRuLWRlc2N7Zm9udC1zaXplOjAuOTM4cmVtfS5zdmJ0bi10YWd7Zm9udC1zaXplOjAuODg3cmVtfX0KQGtleWZyYW1lcyBmdXtmcm9te29wYWNpdHk6MDt0cmFuc2Zvcm06dHJhbnNsYXRlWSgxMHB4KX10b3tvcGFjaXR5OjE7dHJhbnNmb3JtOnRyYW5zbGF0ZVkoMCl9fQoubGFuZy1iYXJ7cG9zaXRpb246Zml4ZWQ7dG9wOjEycHg7cmlnaHQ6MTRweDt6LWluZGV4Ojk5OTtkaXNwbGF5OmZsZXg7Z2FwOjZweH0KLmxhbmctYnRue2JhY2tncm91bmQ6cmdiYSgxMCwxMCwxMCwuOTIpO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMSk7Y29sb3I6I2ZmZmZmZjtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXNpemU6MC43NzVyZW07bGV0dGVyLXNwYWNpbmc6LjE1ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO3BhZGRpbmc6NXB4IDEwcHg7Y3Vyc29yOnBvaW50ZXI7dHJhbnNpdGlvbjphbGwgLjJzfQoubGFuZy1idG46aG92ZXJ7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLmxhbmctYnRuLmFjdGl2ZXtib3JkZXItY29sb3I6I2ZmZmZmZjtjb2xvcjojZmZmZmZmfQouY2JrLWJ0bntiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTQpO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC1zaXplOjAuODYycmVtO2xldHRlci1zcGFjaW5nOi4xNmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtwYWRkaW5nOjEycHggMjBweDtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmFsbCAuMnM7d2lkdGg6MTAwJX0KLmNiay1idG46aG92ZXJ7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLm1idG4sLnN2YnRuLC5nYnRuLC50YnRuLC5jYnRuLC5oYnRuLC5jYmstYnRuLC5sYW5nLWJ0biwuYmFjay1idG4sLm9wdCwuZGl0ZW0sLmNkLC5uby1icmVlZC1iYW5uZXIsLmJjaGd7dHJhbnNpdGlvbjphbGwgLjE1cyBlYXNlfQoubWJ0bjphY3RpdmUsLnN2YnRuOmFjdGl2ZSwuZ2J0bjphY3RpdmUsLnRidG46YWN0aXZlLC5jYnRuOmFjdGl2ZSwuaGJ0bjphY3RpdmUsLmNiay1idG46YWN0aXZlLC5sYW5nLWJ0bjphY3RpdmUsLmJhY2stYnRuOmFjdGl2ZSwub3B0OmFjdGl2ZSwuZGl0ZW06YWN0aXZlLC5jZDphY3RpdmUsLm5vLWJyZWVkLWJhbm5lcjphY3RpdmUsLmJjaGc6YWN0aXZle3RyYW5zZm9ybTpzY2FsZSgwLjk2KX0KPC9zdHlsZT4KPC9oZWFkPgo8Ym9keT4KPGEgaHJlZj0iL2FkbWluP3Bhc3M9YW56YTE5ODUiIGlkPSJhZG1pbkJhY2tMaW5rIiBzdHlsZT0iZGlzcGxheTpub25lO3Bvc2l0aW9uOmZpeGVkO3RvcDoxNHB4O3JpZ2h0OjE0cHg7Zm9udC1zaXplOjAuOXJlbTtjb2xvcjojYzlhMDVhO3RleHQtZGVjb3JhdGlvbjpub25lO3otaW5kZXg6OTk5O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2JhY2tncm91bmQ6cmdiYSgxMCwxMCw5LC44NSk7cGFkZGluZzo2cHggMTJweDtib3JkZXItcmFkaXVzOjIwcHg7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDIwMSwxNjAsOTAsLjM1KSI+4oaQINCQ0LTQvNC40L0t0L/QsNC90LXQu9GMPC9hPgo8c2NyaXB0PmlmKGxvY2F0aW9uLnNlYXJjaC5pbmRleE9mKCdwYXNzPWFuemExOTg1JykhPT0tMSl7ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2FkbWluQmFja0xpbmsnKS5zdHlsZS5kaXNwbGF5PSdibG9jayc7fTwvc2NyaXB0Pgo8ZGl2IGNsYXNzPSJsYW5nLWJhciI+CiAgPGJ1dHRvbiBjbGFzcz0ibGFuZy1idG4gYWN0aXZlIiBvbmNsaWNrPSJzZXRMYW5nKCdydScpIj5SVTwvYnV0dG9uPgogIDxidXR0b24gY2xhc3M9ImxhbmctYnRuIiBvbmNsaWNrPSJzZXRMYW5nKCdlbicpIj5FTjwvYnV0dG9uPgogIDxidXR0b24gY2xhc3M9ImxhbmctYnRuIiBvbmNsaWNrPSJzZXRMYW5nKCdldCcpIj5FVDwvYnV0dG9uPgo8L2Rpdj4KCjwhLS0gSE9NRSAtLT4KPGRpdiBjbGFzcz0ic2NyZWVuIGFjdGl2ZSIgaWQ9ImhvbWVTY3JlZW4iPgo8ZGl2IGNsYXNzPSJjb24iPgogIDxkaXYgY2xhc3M9ImxvZ28taW1nLXJvdyI+CiAgICA8aW1nIHNyYz0iZGF0YTppbWFnZS9wbmc7YmFzZTY0LGlWQk9SdzBLR2dvQUFBQU5TVWhFVWdBQUFVTUFBQURyQ0FZQUFBRHpDL1F3QUFBQldHbERRMUJKUTBNZ1VISnZabWxzWlFBQWVKeDlrTEZMdzFBUXhyOVdwYUIxRUIwY0hES0pRNVNTQ3JvNHRCVkVjUWhWd2VxVXZxYXBrTVpIa2lJRk4vK0JnditCQ3M1dUZvYzZPamdJb3BQbzV1U2s0S0xsZVMrSnBDSjZqK04rZk8rNzR6Z2dPVzV3YnZjRHFEdStXMXpLSzV1bExTWDFqQVM5SUF6bThaeXVyMHIrcmovai9UNzAzazdMV2IvLy80M0JpdWt4cXArVUdjWmRIMGlveFBxZXp5WHZFNCs1dEJSeFM3SVY4b25rY3NqbmdXZTlXQ0MrSmxaWXphZ1F2eENyNVI3ZDZ1RzYzV0RSRG5MN3RPbHNyTWs1bEJOWXhBNDhjTmd3MElRQ0hkay8vTE9CdjRCZGNqZmhVcCtGR256cXlaRWlKNWpFeTNEQU1BT1ZXRU9HVXBOM2p1NTNGOTFQamJXREoyQ2hJNFM0aUxXVkRuQTJSeWRyeDlyVVBEQXlCRnkxdWVFYWdkUkhtYXhXZ2RkVFlMZ0VqTjVRejdaWHpXcmg5dWs4TVBBb3hOc2trRG9FdWkwaFBvNkU2QjVUOHdOdzZYd0JBNmRpRThIWVdoTUFBRUh3U1VSQlZIaWM3WjE1ZkZWRnN2anIzRFg3Qm9RbFFBaWJLQUkrVUZCeFgxQVp3SEY1SWlCUEhSY2VEaTdvcVBoVFJsRkFRY1ZSVVo4UFVYSFVKK0xvNEs2QUFzNjRvT0RHSWhEQ2tvUkE5dlZ1WjZuZkgxaE5uNzduSmpjUUlJSDZmajc1M0NYbmR2ZnBjN3BPVlZkMU5RRERNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNRXpiUUR2U0RUaGFjTGxjZ0lpZ2FScFlsZ1Z1dHh0TTB3Uk5PN2d1UnNTb2VnQUFMTXNDQUJEbDAzR2Fwb2syTUMyUHgrTUJ3ekFPK3JyS0lDSzRYQzV4emVUM0ROTm1JT0VrbzJrYWVEeWVGaW5mNC9HQTIrMk9XVThzV3JJTnh6b3VsMHYwdjl2dEJyZmIzYUxsYTVvR2JyY2JmRDZmK0s2bDYyQ2FoalhERnNEbjgwRWtFckY5UnhxYXF0azFGL24zVGxxZy9IL1dLQTRkYnJjYkVGSDhrU1hRRXFqWDArdjFncTdyTFZJMnd4eFdWQTNNU1lzN1VIdytuMDBEYkVvYmRMbGNMV3JDSGV0NFBCN1JuM0svdHFUV3JWNHYrc3phSWRPbWtNMG53dXYxQ3NIVWtxakNqbDVsTTA3K3pFS3haWkN2cmN2bGFuRWhKWmRIcGpKUGNUQnRFbFVRdGpRa0JGWGhHcS9HeUJ3NGREMXAyZ05nLy9WdWlZZGRyTEw0ZWpKdER2V205ZnY5QUdBM3J3NjJmTFVjV2V1VEo5M3BmN0hheGh3WXFpWVl5NkYxSUxoY0xxRUZVajJ5QUdZT0g5emJMWURQNTRPSEhub0lPM2Z1REpGSXBFWE5LRGxjeHpBTTBIVWQ2dXJxb0tTa0JQYnUzUXViTm0zU1NrcEtvTEt5RWdEc1RwU1djT0FjNjV4Kyt1azRhZElrQUFDSVJDTGc5WG9oSEE2RDIrMEd5N0lPV2lnYWhnRVZGUlh3OE1NUGErRndXSHpQempDbVRaS1ltQWpyMTYvSGNEaU1obUVnSXFLdTYyaFpGcHFtaVlnb1h1bi9oSzdyaUlob1dSWmFsbVg3SDMybVYvbTM4dnRJSklLR1llRGF0V3Z4amp2dXdMeThQRWhOVFkzU0V1a3p2UjRPemNPcExucFkwS3VUOWlzalAxem8yRU14Sit2RXVISGpSRCtyMTFLRnJvbDZqZVZyS0Y5eituNzkrdldZa3BJaU5NVERjVjVNTkt3WnRnQStudyttVHAyS2FXbHBvR2thSkNVbFFidDI3V0R3NE1Gd3dna25nR1ZadG1Cc1hkZkI2L1VLclk4K2s2WkJtb2ZINHhIZm1hWXBoRUlrRWdHZnoyZlRUT1QzaG1IQSt2WHI0YVdYWG9JVksxWm9CUVVGSXZSSERzK2h3UEJERFlXS09HbXE5SjJzQ2RGNWE1b20ycWVHRmNtL1BaU2NjY1laT0hIaVJQQjRQT0R6K1NBM054Y0dEeDRNQ1FrSjRoanFTMVdncXlFemRLMi8vZlpiS0Nnb0FGM1hJU0VoQVlxS2l1RGhoeC9XZ3NIZ1lUMDN4ZzRMdzRPRUJBcVpUWFFEZXp3ZU9PNjQ0K0NpaXk3Q0o1OTgwaWJNQUVBSVFIcXRyYTJGMU5SVTJMbHpKMnpjdUJIV3JWc0hSVVZGVUYxZExjcE1Ta3FDcmwyN3dybm5uZ3Rubm5tbXplT29hWnBOaUFhRFFmRDcvZkR6enovRDBxVkxZZmJzMlpxdTY3YjR1TU14Mk5SQlRTczQ1TDZqNzAzVGpHczFCczJaSGc1QlRtMEQyUGVReWN2TGcrT1BQeDVuekpnQko1OThzamlHaEowVGRPMzM3TmtETjl4d0Eyelpza1VyTEN5RVNDUUNpR2lMVTZWK29QTm1nY2kwS1dLWk5iUjY1UGJiYjBmVE5ERWNEdHRNTE5rTXJxeXN4QjQ5ZW9EUDU0UEV4RVFBMkNkSWFESmREcmx3dTkyUWtaRUJNMmJNUUVURVFDQmdLOU0wVFp2WkhRZ0VjTjI2ZGFpYXpvY2pqbzBFaVd6V3FtRWo4bWNuQjVDNitrTTE5dzhsY2gyeW84UHY5OE9MTDc2SW9WRElaZzdMMHg2eVNWMVdWb2JkdW5XRHBLUWtVYTdzQ0NQSEc4TzBhV2lReXNKRm5nODc0WVFUWU4yNmRiWkJJd3RGMHpTeHJLd00yN1ZySjM1RFpYaTlYdHU4R3cwZ2VzM096b2EzMzM0YnE2dXIwYklzRElmRFVlVmJsb1dHWWVEbm4zK09mZnYyQllBak0vamNiamVrcDZmRHFhZWVpdVBHamNQWFgzOGR0MjdkaW5WMWRVSjRSQ0lSTENzcnd3OC8vQkN2dnZwcUhEeDRNQUxZUGVpcWtEeGNiVmZuT3Z2MTZ3ZmZmZmVkcmI5cHZwQWVTSWlJNFhBWXI3MzIycWp6QUlDb2VVSmFta2Z2R2FiTjRCUWZSamUwSFA3eTBVY2ZvV0VZcU91NkdEZzBXSFJkeCtycWF1emN1Yk1vUTM1dHJDNlB4d05wYVdrd2NlSkVySyt2anhLNGhtSFlCdXF5WmNzd0t5dnJzQTAwT1Q3eXozLytNNzd4eGhzMjRVZEVJcEVvUjVGbFdiaGp4dzU4L1BISE1UYzMxeVlrV2lwMHFTbms2Nmc2Z2R4dU56ejExRk1ZRG9lam5GMHl2Lzc2S3g1MzNIRTJqZC9wVmRXWWVRVUsweVp4OG9qS04vTnJyNzBXcFEyU1NXV2FKbFpXVm1LSERoMUVXVEswb2dYQVdVTUMyQ2R3VHp2dE5BeUh3MUdtbW15K1JTSVJYTGR1M1dHYmlISzVYTkNwVXlkWXZIZ3hWbGRYQzAySjJvSzR6NFQ4K3V1djhldXZ2eGJIMFArb245YXVYWXNubjN3eUhvbVZOZW9LRkpscnJybkdaaXFybm1iTHNuREZpaFZJZ2hEQW5rUkRmbGp3TWp6bXFFRU5tZ1hZUDNqbXpadG5FMHBxbUVaRlJRVzJhOWZPY2FVRFFMU3dkWHJ2OVhwaC9QangyTkRRWUJ1VVZDZHBYTUZnRUtkUG40Nkh3OHpNenM2R3p6NzdUQWczRWhDaFVBaS8vLzU3UFBQTU0xRU9QUGI3L2ZESUk0OWdUVTJOemVSRVJDd29LTUNjbkJ4Ujl1RVVHcktKTEQrWWhnNGRpclcxdGVMYzVMQWFhdmZTcFV0Um5qTlZ6VzJBNkN4RGJDSXpSeDMwNUo4MmJacE5HS2hVVkZRSXpaQm96b0NnZ1phYW1ncExseTYxelZuUndKUS8vL0RERDJMKzhHQ0ZpcXk1RW02M0d4SVNFdUNmLy93bm1xWnBFNGFJaU04OTl4d21KaWJHWE1vNGFkS2tLTE0vRW9uZ3FsV3JzRFU1Ry9MeThxQzZ1dG9tQk5YcnUzanhZZ1N3TzVGWSsyT09PVWlqdS9mZWUyMkRSUjB3QnlvTXlic01BQ0tzNXVTVFR4WmFpbXEya1VuWDBOQ0FFeVpNc0drc1ZDZVpvczNWVG1TaHFHa2FUSjgrUGFwKzB6Unh5WklsbUoyZGJmc3RlY3ZwWERJeU1tRFJva1cyTnBQQXVmbm1tMXVOUU96ZXZUdFVWVlhGRE1SR1JGeTBhSkVRaGtmQytjUEVCMStSUXd6K252L3VVTVdNVWRabGl0a3pUUlBXcmwyckZSWVcyc0pRS0ZiUDUvTUJJa0pTVWhLTUd6ZE9CSHZMUWMxMGJIUGJxK3U2aUxkTFQwK0hNV1BHaVBvamtRaFlsZ1dXWmNFWFgzd0JwYVdsdHJBYmlyT2o3T0RWMWRXd1pzMGFDSWZENFBmN2Jjc2N4NHdaMDJyeS9jbm5yRUxYdnFHaHdmYWQvTXEwSGxnWUhnVlFFRFBBL2dINHdBTVBDQUVqcjRDaElHakRNT0NjYzg0UkdwbnFtR2pPWUZWakNMMWVMd3diTmd5N2QrOHV5dkg1Zk9CeXVXRFBuajN3MDA4L2dhWnBRcENyU1NjUUVSSVNFdUNISDM2QVBYdjJpSExwM0RwMjdBaloyZG10WWw3Tk1BemJ3OFFKZVdVSndjS3c5Y0hDOEFod0lDWm9VMlhSS2dlWHl3VStudytXTEZtaXljS0Y2cU5sZXg2UEI1S1RrNEZpRzFYTnRUbG1uRncrSWtJa0VvSCsvZnNET1lSSVdDTWkxTlhWUVhWMXRZWlN0bWhhcGlobkN3K0ZRckIxNjFhdHFxcktWb2VtYVpDYW1pcmFmYVJwU3FqUkVyeldJTGlaeG1GaDJNWWhvUUt3ejhTazdEYkJZQkFhR2hxRW9BSFlyLzNSQUxZc0MzSnljbEJOWVg4ZzgxbGszdElTT1ovUEoweDNlUk9sek14TTZOeTVNMUw5VHJHVVZEOWxjYUZ6Q0lWQ0FBRFEwTkFnbHJJZGFaeldUS3YvazkrelVHeTlzREJzNDZqSkZ1VFVVc0ZnVUF3KzBzNUlHSklRVFV0THM1Vkg1dlRCcEk5U056YVNOY2YyN2R2RHNHSER4UDlsUVU3SksrU0VEWFFNbWM2V1pVRkRRd09VbEpTMENvK3NVL0MzS2hobGdjbENzZlhDd3ZBd29RNlFsdEpxYUZFL1FMUmdESVZDUXZqSmMyNXk4Z05kMTIxSkVXVGlFVGJ5NEtiQkhvbEVRTmYxS0NGTjdSZzJiQmhRdktEY2ZzTXdSQVlZQUlDZVBYc2lDV3NTMG9nSU8zZnVoSWFHaHNPV3FLRXhxSzJ4cmkvMUNYMW1ZZGg2WVdGNGlJbDE4N2ZrbkdFczd5K2xtU0t0VU43SG1ZNHRMQ3pVU0FNallTTnJhMDBoRDNaNTBOZlYxVUVnRUJCdHBIa3p5N0xnNG9zdmh2NzkreU1KRWxrVGxQdmx6RFBQQkZxaUtBdnhSeDU1UkNQQkt2ZERZeXVBNUdQa1B6cE9YU1BzbEpMTGlhYTJjVlhiUmRlZ0thY0xjL2hoWWRqR0lmTVJZTDl6aEFaWldsb2FHSVloZ3BrcFBaUmxXZUR4ZUNBY0RrTkZSWVhOaEhaNmJRelMrS2d0SklDKytlWWIyTE5uRHhpR0lVeGdlVDd4bzQ4K2d0VFVWQURZSDJ4Tm1xRnBtdENoUXdjNDU1eHpJREV4VVdpdmlBalRwMCtIZ29JQ2Nidzh4MGp0SU1HbUNuUFNqT1UvT282T3BmbEpTaWNXei9telVEczZZR0hZeHRFMFRUZ1cvSDYvME00eU16UEI0L0dBclBYUjhTNlhDd3pEZ0czYnRrRnRiYTM0WHZZNHh3dDVzVlVCVkZCUW9PM2V2VHNxS0Z4MjRuejIyV2VZbDVjSGNwNUZFaTVUcGt6QlVhTkdpWDJFQTRFQVBQLzg4L0RFRTA5b3NvQ1g1emZsK1ZGNWlWK3NmcVA0VEFDN0kwbzFhUnZqVUd3QXhod1pXQmkyY1VoWUFPenp2dElBSGp0MmJOVGFZMW5vdUZ3dWVPdXR0NFM1SnM4ak5rY1kwUEVrZ0FEMm1ab05EUTJ3WU1FQzRmV1Z0VFRMc3NEcjljSkpKNTBFVTZkT3hheXNMTnU4NVlJRkMvQ3ZmLzJyMERwcmFtcGd4b3daOE5CREQybFVEZ1ZkazJrdGEzcHlteHByTTdWTEZvamtnSkpOL3NidysvMTRxS1pBR09hb2dnYkczWGZmN2JnbW1UalE1WGgwakpvZ3RiQ3dVQ3pIazljbDB4cmg2dXBxdlBqaWk2T1dpY2xseHV0QWtaZVl5V2E2MisyRzh2SnlrYVVHTVhwL0VNdXk4SjU3N2tHWHl3VURCZ3pBOWV2WG82N3JZZ2xlUVVFQnBxYW1SczBuT3EySGx0dnNsQkEyVnNZYnRjMXFmemJHS2FlY2dyVzF0WTdaYW9qSEgzOGNuZnFYaFdicmdqWEROZzVwTDZRRmVUd2VPUGZjYzdGcjE2NDJJVUNha05mcmhVZ2tBdSsvL3o1ODlkVlhHcFhocEEzRzY2Mmxjc2xrSmVlR2FacHc0b2tuYWlVbEpXSVZoaXhnTGNzQzB6Umh6cHc1c0dyVkt2emxsMStnWDc5K1VGOWZENnRYcjRZLy9lbFAwS2RQSHkwWURJcnpJL09YbHNHUlZxeHF3ZkxlTVRSUEdtdEpKSDB2eDBuR2k4L25PK0NWTzB6cklyN0hIOU5xSWZPV3pMcXNyQ3k0N2JiYkFBQnMzOHNlNGxBb0JFODk5WlJ0elN6QWdXMUNSTWVyMjVOU2tQWGV2WHRoeG93WjhOUlRUNG5rcGlTWVpPM3JqRFBPZ0pLU0VuanZ2ZmRnMmJKbHNIejVjcTIrdmw3OG4weG1WYURwdWc2Wm1ablFwMDhmMUhWZHJMMm0rVURTS09sUFRrS2hhWnFZai96eXl5ODErZnpqRllxa29UckZHckxtMTdaZ1lkakdrYjJmUHA4UFJvNGNpU05IamhRYlRRSHNENzhod1huNTVaZkRqei8rcUtsSkhHUmhJQXVmZUNEaElXOWtSRUw0alRmZTBNNC8vM3djTjI2YzBGSXA3bEVXNWo2ZkR6NzU1QlA0NUpOUE5KckxvM0xwbGI2amNqUk5nd2tUSnVEOTk5OHZObGFTblRheTRJd2xuRFpzMkFBWFhIQ0JtSE9sY3VQMUpqTkhCMndtdHdMSSthRHVvQ2ZQeFJFZWo4ZVdHWmtFWFVKQ0FseHd3UVc0Y09IQ3FQMVlLRXlrdXJvYUprMmFCRjk4OFlWR2pnSloyTW52eWJSc0NyV05ja2dLQ1RyVE5PSEJCeC9VZ3NHZzdSelZlYlIyN2RyQjdObXpvVmV2WHFLc1dQTnJKSEF0eTRMcTZtcll2bjA3N04yN0YrcnI2OEh2OTBObVppWmtabVpDVmxZV1pHVmxRVVpHaHZndVBUMGRhRnZYMnRwYXFLK3ZqOG9tRSs5RGdNS2E1R2tHRXZKc01qT01SRk1PRkhJbzFOVFVZRlpXRmdEc0V5S3FJMEF1aTk3VFg4K2VQV0gyN05sUkUvZUkrL2RCMmJ0M0wwNmFOQW5UMDlOYjNIeFR3MVRJL0FRQVNFOVBoMm5UcHVIUFAvOHNIRGkwZzU5OC9yS0Q1N1BQUHNQTXpFeXhyTTlwWHRBcGNCb0FvRy9mdnZEZ2d3OWlUVTJOS0plY1NJajdzbndIQWdHY05Xc1cvdGQvL1JjT0dqUUllL2JzS2NwdExNVy9FMlBHakluS0xLN3VoOElPRklhQitMekpobUZnWldVbHRtL2ZQdWEybWFxMlIvTmZVNlpNd2Z6OGZBeUZRbWhabGtqeFQ0SVFFYkc0dUJpSERSdUc2a0J2Q1JNdjFxb1BUZE5nN05peCtNTVBQMkFvRkVKZDE5R3lMTnkyYlJ0V1ZGUkU3ZE5pR0lZdDZlMEhIM3lBQ1FrSmpsdDF4dHBxbElTajMrK0g1NTkvUGlxN055SmllWGs1bm5MS0thaHBta2czSnBkQjdaZlhWemZHRlZkY2dZRkF3RllQQzBPR2NhQXBZVWpmVlZSVVlLZE9uY1NrUDNsTkV4SVNoQ0JNU2txQ3RMUTA2TmV2SDh5Wk0wZUVyRkI2ZkhYZmsrcnFhbnpzc2Nkc1dhRkpDTFFrY3VBeDdlazhlL1pzMng3T2htRmdmbjQrQWdEY2YvLzlXRjlmTDhKODVOQWJFcEtCUUFDZmZmYlpxTFlUcXRhbWZrNU5UUVc1VDNSZHg5cmFXcnpra2t0UTFtTGwzOHBseEp1NVo5eTRjVkhDVUwyMkxBemJCdXhBT2NLUUE4VG44OEU5OTl5RDVlWGxZbGthSlVSdDM3NDlkT3pZRVhKemM2RnYzNzZRbFpWbFcxS1duSndNQVBzR2NDQVFnTFZyMThMMjdkdGgzcng1OFBQUFAyc0Erd1FXaGFNUUIrSTlkb0tDdVFFQWhnMGJodmZjY3crTUhqMWFwTyt5TEF0V3Jsd0pJMGVPMU54dU44eWFOVXZMenM3RzIyNjdEV1FQTVA0ZTlJeS9MekdjUEhreTdOMjdGMmZQbnExUjJBczVTZVE1UFRsZ25EN0xDVldwanovNzdEUDQ3cnZ2TkpUbU5La2ZBT3pMK1F6REVLK04wZGdLRkJaMkRDTVJiOUMxdW9NZGFSYnFmaW55WjluRUxDMHR4VnR1dVFWSGp4Nk51Ym01VWZXVFdYMG92SitrYVY1NjZhVzRaY3VXcUIzaVZxeFlnYm01dVVMNDBMTEJSeDU1eEdiV3E1dEdXWmFGZ1VBQUgzMzBVWlRyY1FxU3BuaEtpcTNNeU1nQXVheXFxaXI4d3gvK0VDWDVuVEwxeEdzaUF3RGNlT09OR0F3R0c5MDNtVFZEaG9INDV3d1I5MjNTNUNRWWRGMlBXcmtoUXdLRkFxMEpKeWNNbVowdExSVFBPdXNzM0xGamgyaExNQmhFMHpTeHJxNU83TUluOTRmWDY0V1VsQlI0OGNVWGJRSlJGcUp5djl4MzMzMm96cG5LMjR1cTUzbmlpU2NDOVkxcG1yaHg0MFpVSFRHMER0cHBaVXE4WnZMa3laTnQreWF6TUdTWUdEUWxER2xPcTdTMEZJY1BINDREQmd6QWdRTUg0b0FCQS9Dc3M4N0NKNTU0QXRldFd5Y2NETExna0xYRVVDaUVoWVdGbUpPVFk5TnNhRkFmeW9RQ0tTa3BzSExsU3NmenV2UE9POFVhYVhsSkhiM201dWJDUng5OUpJNTMwZzRSOXprK3JyLytlb3gxTHFvRDVPV1hYeFpsQmdJQjdOKy92emhXem5Rai81Wm9UbDlObVRMRk51ZnBwQ0d5TUdRWWlFOHp0Q3dMS3lzcm83YlBKUEx5OHVDZGQ5NFJ4OHNDUTlZWURjUEFKNTk4RWxOU1VzUnYxWmcrZWUxdGN3ZGpyTkNlSjU1NHdsRnpMU2dvaUd0Q2NzQ0FBZmp0dDkrS2MxQ25BdWk3MHRKU3ZPR0dHNFNHcUo0YkNiak16RXdvS1NrUlFtcmV2SG5vZEZ4TDhKZS8vRVhVNCtSUlptSElNTDl6c01LUXRKYWhRNGVLV0QwbnlHdGJWMWVIRjE1NElhcmFUcXdrcE0xQlhzcEdnaWd2THcvSXZDZE5qSVREdEduVDR2Yk81T1Rrd0lZTkcyd2VjWG92QzhhcXFpcTg1WlpiYk1KTkZUVDMzWGVmTUYwM2JkcUUvL0VmL3hGMWZFc0pvbnZ1dVllRjRWRUNyMEJwNVpBM2M4MmFOZHFpUllzQUVXMWVZVlM4d3lrcEtiQnc0VUtRUTFKOFBwOVlVU0l2MFl0bk1LcEpDS2crV2g1Mzc3MzNvaHlhWWxtV01OUFhyRmtUMXpsNnZWNG9MUzJGYTY2NUJnb0tDa1JxTFhuSkhkV1prWkVCczJiTmd1dXV1dzdKWVNJZjA3dDNiN2ppaWl2QTcvZERYVjBkL00vLy9JOVllaWozV1VzSklrN3VldlRBd3JDVkk1dUM4K2ZQMXhZc1dDQ3lSdE9hWGtxS0FMQnZzL1p1M2JyQks2KzhnbjYvSDF3dWw5aUMwK1Z5Z2E3clFvQmdNOE5xVkFIY29VTUg2TmV2bjFpU0ZncUZoQUNMUkNJaTdYOVQ1NmZyT3VpNkRqLysrS00yZGVwVUtDNHV0cDAzU2t2ZEFQYkZFTTZjT1JNbVRweUk4cnBxVGRQZ25udnV3Zjc5K3dNaXd0cTFhK0gxMTEvWG5NbzRtQTJ2WkdKTk43Q0FaQmlGbHBnemxFbEtTb0lWSzFaRWxVRnpkdlFhQ0FUd2dRY2VRSUJvSjRxYzJpdmVjNURuSE1rRGUvYlpaMk5SVVpGWVlTS2J0bVZsWlhqYWFhYzFLVzFWNzdlbWFUQnMyRENzcXFyQ1lEQW9jakxLNTBmemlIdjM3c1hKa3ljanRlK1paNTdCdXJvNmNWeS9mdjBjcjBWTGV0Sm56SmpScUtlZnplUzJBMnVHclJ4MTdpOFVDc0hNbVRPaHVycmFacktxZ2kweE1SRnV1dWttT1BQTU00WDJSQUhPY242L2VKRzFTTklxdTNmdkRoa1pHZUQzKzIzYkN5QWlVRUxXcGlCek96RXhVWnpIbWpWcnRIUFBQVmNFYzZ2TEVtbk9zbjM3OWpCejVreTQrZWFiOGZ6eno4ZkpreWREU2tvS2hNTmhHRGx5SlB6MjIyK092M1hxcndQbFFCeFJUT3VFaFdFcmh6TGF5QU51MWFwVjJtT1BQU2IyUGdFQVlTNVROaG9BZ0U2ZE9zSGt5Wk50ZXlOSEloRmJucittVUZlcGtQREMzMWVKSkNjbmkwdzF0R3FEWWdEakVZWjBic0ZnRUh3K256RDkxNjlmcjAyY09CRktTa3JFQ2hJQSs4YnlMcGNMTWpNellkcTBhYkI0OFdKd3U5MFFEQWJoMVZkZkZZbHI2YmQwSGkydGxYRUtyNk1IRm9hdEhFM2JuNHRRL2p4Ly9ueHQ1Y3FWdHBSWWNzSUMvSDA1MzVWWFhnbDMzbm1uYlFVSENhNTQ1Z3lkd21rQTltZWNsbytoN05ZQSs0VEV3SUVEbXl4ZnpveE4rNlZZbGdXR1ljRDc3Nyt2M1hqampkRFEwQ0FjU2JKamlPcnUwYU1IWkdSa0FBREFsMTkrQ2JObno5Ym9RVUdhSUpVcjUzOXNDWUVZYXlzQnB1M0J3dkFRZzcvbnRxUEI1eVNBMU1Fa0N4MDZuclE5S2ljY0RzTWYvL2hITFJBSWlNMlJDRG5EdGNmamdXblRwc0hnd1lOUkxyODU3YWZmeUpvVkNSWXlPVW5veUdYZmVlZWROdTFRRFl4V3R3QlE1L1J3M3c1NjJza25uNnpWMWRYWmNpVTZlZE4xWFllLy92V3ZVRlJVSlBwUWJUOEpiTG1kY2h1ZDJ0WVl0TjFCWTMybk9xdFVadzdUT21CaDJJcUkxN3NyYXpsLytNTWZvS3FxU2d3NDJWUW1nZUQxZXVHTk45NkFqaDA3QW9BOXMzVnoyMFMvcFIzd1pMT1pIQ3MwSjltdFd6YzQ5OXh6UmVnTnRZbENmR1N0Vms3blQyVlNrb2N0VzdiQWhBa1RoSkFEMkNlc1pDODZ6U08rOHNvcmNNWVpaeUQxQndscWVXOW05ZHprNzZpTmFxTGRXRFFXb3RSVS96YlhtODhjV2xnWXRoSlU3YUVwU0N2NzVwdHZ0RVdMRm9ud0dYbi9ZdG5rN051M0wweWZQaDFwSHhKS3U5OGNaS2NESWtKK2ZqNFVGUlhaQkdRNEhMYnRqL3o2NjYvRGNjY2RKOXBNNTZuck92ajlmaUg0NUVCdVRkT0VvNGY0NUpOUHRFbVRKc0dtVFpzQVlKOFdTT2RBNStwMnUySEFnQUV3ZCs1Y09PZWNjOURyOVlyNmFEc0FwM09TQThubDZ4RFBQaWpOMGU1WUUyemRzREE4RE1RajZHS1pVazM5eHJJc21EdDNycloxNjFZaGxHaVRkZExVS0JYWVZWZGRCVU9IRGtWS1RkV2NvR3UxUFlnSW16WnQwalp2M2l5RWlXRVlZazZQMnRLdVhUdTQ5OTU3TVQwOVBTcUhJTzA1UWtLTk5FWVNYcVRaRWN1V0xkUFdybDBMQVB0VGtnSFlWOWNZaGdGRGh3NkZ2Ly85NzNERkZWZUkzSVV1bDB0b3pkUiswanhsODUvS3BuTGo2UjgxTUQxZVdETnNYYkF3YkNYRUl6Q2RoSmRsV1ZCUlVRRWpSb3pRNnVycUFBQ0VxVXIvSjFKVFUrR0REejZJdWFOYnJIYkZha2ROVFEwc1diSWt5dXltZVVTYW03djIybXZoN3J2dlJqbnZJam1GeUxTbnVpaWNSamFmL1g0L1pHVmx3VHZ2dklNVEprd1EycUM4ZDdKaEdEWnZkbloyTml4WXNBQnV2dmxtbElXcS9KNDg5ZkltVnJRTktRbktsb0RuQ0JubWR6Uk5nenZ1dUtQUndOeXlzaklrajJoamMxQk9mOFRFaVJORjZpekUvUUhLaUNqUzdsdVdoUjkvL0RGU1RzSG00SlFKMnVWeWlTUVNjcHA5TmVzMkl1TENoUXZ4ckxQT1FxY04zV1V2dFZ5UHorZUQwYU5INDdKbHk4VDVCSU5CM0xCaEE0YkRZVnRBdGdvZCsvREREMlA3OXUwQndPNHNVWk83MHZ2bWhNczgvZlRUVVJ2SXExQSt4cFpNRU1Fd2JaSjRoR0ZwYVNtbXA2ZUw0K1hmTmdWcFVxbXBxZkQ0NDQvYnlsVVRyWnFtaVlGQUFPKy8vLzY0YkRReXRSdHJWM1oyTm56enpUYzJ3YWRDZ216YnRtMzR3Z3N2aUJ5SDZ2bVJJRXBPVG9aUm8wYmg0c1dMc2FTa0JBM0R3RWdrZ3FacDRzeVpNM0hJa0NINDdydnYyb1M5Q2dsbjB6VHg4ODgveHpGanh0Z1NObEQ4WWMrZVBlSHFxNjlHVlFqR0l4VC85cmUvTlNvTUxjdkMyYk5uUndsRDFoS1pZeFluWVNndno5dTdkMjlNWWRqWXdGR2RBams1T2ZETk45L1lFbzZTWUpEckxDd3N4UFBPTzY5SmdSaExXTkgvNUhUL1JVVkZ0bDN1WklFa1o1NHhUUk4xWGNlZE8zZmk0NDgvamlOR2pNQWhRNGJnT2VlY2czZmNjUWQrL2ZYWFdGOWZMNDZUaGZxZ1FZTnNLYnkrK09JTFd6MXEvOUo1MDROZ3hZb1YyTHQzYi9ENWZPRDMrK0d1dSs3Q2NEaU00WEFZNTh5Wmc3SVRKeDZlZXVxcFJqVlR5N0p3MXF4WkxBd1poZ1RHOWRkZjd6aG82WDFaV1JtbXBLUTRiazRVYnozMGV2bmxsMk00SEJacmVKMkVvcTdyK000NzcyQnFhaW9BMkxPdk5HZkRLSGxRanh3NUVyLysrbXRIVFNuV2VjdUNUdjVlTnJkMVhjZVBQdm9JaHd3WlloUGVIbzhIMnJkdkQwdVhMc1ZBSUdDclQwMFNLN2RKbmo2Z3o5OSsreTNtNWVXSmpEdXhoSldhb0hiZXZIbU8yN1BLMzkxeHh4MDJZZWlVZ1p4aGpoa21USmhnRytTVWpoNXhYLzYvMHRKU0VmWkN4Q3VVMU9OOFBoOU1uanc1U2dDb2dzQTBUWHp5eVNmeFlBZW4zTzRlUFhyQXJGbXpzTHE2MnFiWkVmSm5kVTVURmFLNnJ1UDY5ZXZ4cHB0dXdzNmRPd09BODd4cHIxNjk0TEhISGhOOVNlY3E3NWRNZlM5L3BqeUVuMzMyR1E0ZVBOaW1jY3J6bXJHbUJ6Uk5neWVmZkZMTXg4cklmZnluUC8wSjVZY05lZEFaNXBpQ3R2bTgrdXFyUlFZV2ViRFFnS21xcWtLQWZjdk5EalJGdnh4YzdQZjdZY0dDQlZIQ2tPcW10bGlXaGJmZmZqdktBNVZRbDc0NVFmWEpyMzYvSHdZTkdvU2ZmdnFwWTMweTZpYnkxRDg3ZHV6QUtWT21ZRTVPanEwZjVYT1YrOWpyOWNJSko1d0FCUVVGcUNLbjVaZjd3TElzbkRselp0UisxVTc5R211VjBOTlBQeDJWblZzVmlCZGNjSUhRYU9rY0R1VTJEQXpUYW5HNVhIRHp6VGMzNmx3b0xTMUZlWUFjeUp5U2FvWU5HalFJdDJ6WjRpZ0U2TDFwbXJoMTYxWWNPblFvcWltK21vdmNadnA5Ky9idFlmcjA2Ymh1M1Rvc0tDakFvcUlpckt5c3hKcWFHcXlwcWNIeThuSXNLeXZEelpzMzQ2Wk5tL0RWVjE5RmlvVnM3QnpsK3NpMGRidmRrSktTQWhNbVRNQjE2OWJodG0zYnNLeXNUQWpFVUNpRVpXVmxXRlJVaE11V0xjT3p6anBMT0ZSa0lSV1BSNW5hOGVLTEwwWTkyRlQ2OU9ramZ0ZWNuZmVZd3d2UDRoNWlLTUQ1aVNlZXdMdnV1Z3NBOW1kY29WZkRNS0N5c2hMeTh2STBTblFLQUNLaFFGTlFXZXJ4SG84SHJycnFLcHcvZno1a1ptYUNydXVPR29saEdMQnExU3E0NXBwcnRJcUtpcWkxenZIVTdmVjZSZklIZGI5aFdnK2NscFlHeHg5L1BIYnIxZzBTRXhNaE1URVJhbXBxb0tpb0NMWnMyYUxWMU5TQVlSZ2lQbEZlQnkxRGdkbDBuUHAvcWo4dkx3K0dEUnVHM2JwMUU5bDFkdXpZQWYvNjE3KzBIVHQyQU1EK1pZVDQrd29XaXBHVTEzZXJ5TisvL1BMTGVPMjExd3JocUM3ajAzVWRNak16dFZBb0ZIVXRZNVhQTUVjdExwY0wzbi8vZmFFRnFrNENSTVQ2K25vODlkUlREem9lamJKT0UzNi9IeDU2NkNFUmp5ZUhtNmdPbHUrLy85NDJNcHVUckNEVzkwMmRpenhQcDVycTZ2eWNhcmJUbko2c0VhdkpJR1JrQWF2R0c4cDFxdHBuckRsRGw4c0ZyNzMybXMxWkk1djcxdS83UHFzYU85WFBEaFRtbU9Pc3M4N0NqUnMzUnBsUDhoeGFPQnpHVjE5OUZRL1VtNndLSkRXYjlTZWZmQ0tFc1NxSTZmdHdPSXo1K2ZuWXFWT25aZ1Vla3pDUlBhMnFtYXNLb2xnQ1ZCWkdzYlk1VlIwK0xwY3I2aGhWY01vUENWa0FxMU1UOHVvWHAvMlk1ZmRwYVdudzNudnZPYzRGMCtmUFAvOGNHeFBRREhQTU1IVG9VRnl5WklsdG9EaUZrcGltaWNYRnhYanJyYmRpY3liWG5lYnBuT2pRb1FPOC8vNzdHQXFGYkN0VTFFRWNpVVJ3dzRZTk9IbnlaT3pkdS9jQm5YTmpxMlNjMnVzMFIrZjBHNmYwV3JKV3B6b25TQWlwMmE2ZGtQZDJsbDlqL2Q3ajhVQ1hMbDFzY1k3eUsvWG5xRkdqVUsyWE41RnFuZkFWYVFHOFhpOWNldW1sMkxselp3Z0dnNUNabVFudDJyV0QzTnhjR0RKa0NIVHIxZzBTRWhLaThnSEtxYTFvem1yMzd0M3czWGZmd1pZdFc2Q3lzaExxNnVyQXNpd29MQ3lFenovL1hKUG40dUtkYzZJMXdOMjZkWVBycnJzT0gzcm9JZkY3V3M5TGhNTmg4UHY5WUJnR3JGdTNEZ29LQ3FDOHZCenE2K3ZCTkUwb0xTMkYxMTU3VGF1dnI3Zk5DeDZ0MER5aW5CQ1crdnpNTTgvRXQ5NTZDenAzN2l6V1BNdlh0YWFtQmpwMDZLQlJ1aldlSDJTT2F0eHVOL2o5ZnRpMGFSTUdBZ0hoTVpiTlg5a1VkWHF2bXEyNnJvdmxaL1IrMGFKRjJLRkRCMUZuY3lFdnBzdmxndHpjWFBqM3YvOXQwd3hsVDdmcUVhVzJCWU5CTENzcnc3NTkreDZUODEya2NWSmZYbm5sbFdoWmxsanRJMS9UU0NTQzgrYk53MWllZWRZTVd4OGMrWG1RbUtZSlBwOFBObTdjQ0pXVmxVSnpJQzJBc3ArUVZpR25rSEs3M1NMUG5xdzVVRXdkcFp3eURBTzJiZHNtdkx4eTJVMXBaMlNtUmlJUjRmSGR0V3NYREI4K1hPdmR1emVNR3pjT1R6NzVaT2pRb1lQd0JIdTlYbEUyWlhhaG5JR2tJY3A3clJ6TitIdytzVTgxOVQrbFB4c3dZSUJ3N0pDbm5xNVpYVjBkZlBUUlI0NGFvZXlzWVcyeDljQ1BwNE5Fam5XVDAwSEprQWxGL3lQQktHOXNEckIvYmtvTndVQkU4SHE5VUY5ZmI2czMzb0hrMUNhNURIcE5TVW1CMU5SVU1hZEZvVEtHWVVBZ0VCQkNNUmdNeHRjNVJ3bjBFS01IZzJWWmtKaVlDSnMzYjhhdVhidmEwb0xoNzNrWkZ5OWVERGZmZkxQVzBOQndoRnZQTUljUk5SeUR2cFAvWWsyYU4yYnl5czRCSnkvemdaaXFicmNia3BLU291cVJ2YWpVZnZtOTdOUWh6K3l4WUNxcmpnL2ltbXV1Y2R3SDJ6Uk5MQzB0eFI0OWVyQXB6QnhieUtzZmlLYThoWEk4SEhHb01wcFF1M3crbjYxY05lR0EzQTVaZ011b251RmpRUmdDMk5jU2E1b0dIVHQyQkZxUExLOS9EZ2FEV0ZOVGcrZWRkeDZxMTVBRkkzTk1RSm9oYVUrcU5xY0tFQ2Nobys0aVI0TEhhZWMyS3VkQUI1amFIbFV3eHhMc3NiU2tveG0xbnp0MjdBai8rTWMvTUJLSlJLVXJLeTh2eDl0dnYxM01YY2ozZzlPMVlnSEpIRldvZzZVNW5rTjE1VU5qZGNobE5YY1F4ZExnMUhJYjAvVFVPTHhqUlNzRTJIK3U2ZW5wTUcvZVBLeXZyN2VaeFpabFlXVmxKZDUrKysyWW1wb2FsK0JycXI4WmhtRU9PMnB3dVByWjYvV0N6K2VEcjcvK1dvUkt5U0ZKcG1uaWlCRWpzREZObm1FWXBsV2phbXl5dHBhWW1BakRody9IdSs2NkMvZnMyV1BMazJoWkZwYVhsK1B5NWNzeE96czdha3JrWUtZeEdJWmhqaGl5RnRldlh6OTQ3TEhIY01tU0pWaFVWQlNWaDlFMFRWeXpaZzFPbURCQmJEUkZqalJPMnNvd1RKdUV6R0Zaa3hzN2RpeEdJaEdiU1d4WkZ1cTZqcXRYcjhiaHc0ZGpabVltQUVRN3RRRHNxMzJZdGdNL3hwaGpHZ3BjbHdQWWRWMkhpb29LcUt5c2hFZ2tBc1hGeGJCeTVVcFl1SENoVmxWVkpWYWFVQkM2MysrSGNEZ3NQTytSU01ReHp5TERNRXlid2VmelFaY3VYV0Q0OE9HWWw1Y25NbCtUK1JzcnAySmpXWFlZaG1IYURFNG1MU1ZnZFpvSGxJT3dDZG43ekRBTTA2WndFbWJ4eEdXcXYrRUVyZ3pETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUF6RE1BekRNQXpETUVjdlJ6ekhrTG92TFVEek5qdHFLazFTYTlsOUxGWTdXMHY3bUFPanFldDZwSGZBYSs3NFVJOTNhcnQ4RE8yb1NMczBIdW56UFJpT3VEQlVvYldlNmc1eGg0b2pMVXhiTXVlZFUxbHkrNTJ5YWpmMy9PSVpMQWRUWG5OcHF2NldhbCtzY2xyNy9YRTRCSk5USHgzdWNkd1N0QXBoNkhhN1lkU29VV2haRm5nOEh0QjFYV3k5S1dmK2FPN3VjdkhRMU0zV1ZINDZwNDJjNURMVnphRmlMZW8vMFBycC9CdmJXaUJXMitJWmFPcnYxTjhmYkpxcVNDUnlVTDl2S2pOTVUvdEtOelZZbmJadGpmVlozaStiUGpkVi84RzJyeW5pRllaTzJYdGluYXQ2aks3cjhORkhIMmswWHIxZXI5aGp1aTNSS2xKNFVSb2tnSDNaaFNPUkNMaGNMdEIxWFd4TFNiUzFYY2Rvc0RkWENCMHNWSWRsV1FkVnQ1Tm1LWDgrMUpwSFd6SzVuQVFKUGF4aW5VZFREL1BEbVN6V1NkZzVXUkZPRHdENW9VaUNVSjc2YWdzY2NVbEMyb1ZUcHpYV21VMlpMeTNadnNab3F2NURiWVlmYVRPL3RkTlMxeS9XY1FkYmZsT2E5YUhPaVhnd1V5Wk9xTlpjVytLSWE0YnlCVkNGWDJPZDJsSU9scVpvaVJ2a1lNcG9hV0hjM0xhb2c3VXhNL0ZRY0xEbmY2Z2ZWbzM5UDU1N3J5bkJjVGp2MzZibW5KMGdrMWdXZ3JUbmRsTlRBSzJOSTY0WmtnQWtqeFFsMHJRc0MwelRQT0thVDBzSW80TVpNQWQ2ZnZGcXpvZGE4MjJLMXE2NUhzekQ1SEJaTDRlU2VPNFB5dlN0d21ieUFSQ1BPVXcwRlFxZ2NxUnV4Q001RU9JeGZRNjBmUzN0VFdZT0xjMTVXQjNNdFNSbGhqVEV0bXd1dDJsY0xwZHRJbHFkZEZZMy9DSGNibmZVSmo3eGJPcnVWTC9hQnFleURtUXlQRmE3blhEYVlMNnhEYzNqUFVjMWM3TnNPcXNiSWpXbjM1eTgwZXBtOWVyeFR2VTRaWmh1YkF0UUtsL3VCL3EvN0oyUDkxelV0c3FmNVhMVWRzZnF4MWpuRUMveU5nV05lZnhidS9PUmFTYnlSWGNTUms0WG5JU1gvSm1POTNxOWpvSXRGazZDU2hVUU5QQU9SaWlxNTlkVVNJMzhPem9mcHpDZ3BsQUhOdUdVRHAvYTVIYTdoV0NLUjZpNDNXN3dlRHd4QmF2SDR4RjlLTmVwQ2pDcXora1lwM05RZjB2dktiS2h1V0ZEaloyRFhCYWREN1ZIdlM3cXRXb00rajNWcGRZcEMzd3FsL1oxWVk1UzFFSGdwRG1veDNnOEhuSGpPKzFiRWM5ZUZqVFlaVTFEclYrRmJ2UjRibmgxc01nM3VDd1FhVUE0YVdleDRoempIUkErbjgvMk82ZnRNWjMrUjc5Ui85UzJ5ZGRFN25OVjRLc2J2QU9BVFVpcS9lcjMreDNiSlF0dE9sNnRTdzNwYWd5eUtwejZSVVVXaENxeHRPUjRpS1V0QS9DZUxNY01UaHFUa3puU21GYWsvaS9XSmtEeElBZFprNmJnWk1iRlc3NTZIdXJnVnM5TlBRKzFiVTREdHpGaW1hc0pDUW0ydHFsN0JkTm5wOS9HMHFabFRZL2FwMnBiQUkwUGJuV2pKdlZCSWd0TnFrdWx1ZWErWEk5cXFaQzE0ZFNQY2grcEpuNjg5VHVaOTFSWFk5TVo2ditabzRSNE5SM1ZaS0NienVsR2JrN2RWQjc5M21uQXFScFV2TWptajFxZWs4YW5ta3J4bXRQeHRNUEpmSE9hNDJ0S0UybXNMYkVlSHZJMVU0K2xxUTIxVGZUcUpJemtoNmlxVVRzSjBsZzRUUWVRMXFxZXEydzlxQnRJT1puOHpkWG9ZZ20vV1AzTm0xZlphZk1UQjA3ZUs1ZkxaVnNhbFppWUNFT0dETUdCQXdkQ2NuSXlKQ1FrUUhWMU5mejg4OCt3Y2VOR3JieThIRHdlRDVpbUthTHBxYnltUEdJdWx3dDY5ZW9GZ1VBQWtwS1NJQmdNUmoybEE0RUFHSVlCWldWbFVRdmI0MEVPTzFJRDFIMCtIK1RrNUVDZlBuMndVNmRPQUFDd2QrOWUyTFp0bTFaVVZBU2hVRWljQzdWSDEzWEhzaHFEUFA3VXQ3MTY5WUxldlh0alJrWUdKQ1ltd3M2ZE82RzB0RlRidEdrVFdKWUZpQ2dHb1ZNRWdKUG5VbDdQbXBpWUtKWmxCb05CMFZiNnJScUJRTjhuSlNWQklCQ0FsSlFVc2ZvbkVvbEVIVS94Y2ZRN3VVMnBxYW1nNnpvZ29pZ2pscWRWdmRmOGZyL3RHcE9IdGJGRUJqNmZEMHpUdExXSjdzVjRJTUZ1R0FaNHZWN28xNjhmbm5qaWlkQ2pSdy93ZUR3UWlVUmcrL2J0c0hidFdxMm9xQWgwWFdkUDc5R01iRllSUFhyMGdEZmZmQk8zYjkrT3RiVzFXRlpXaHBGSUJCRVI2K3Jxc0s2dURvdUtpdkJmLy9vWFhuYlpaZUxPYTQ1bWtKR1JBYnQzNzhhYW1ob3NMaTdHdlh2M1ltVmxKZTdac3dmTHlzcXdwS1FFUzB0TGNmZnUzYmh6NTA1Y3ZudzVYbnJwcFJqdkpMYXFNZEQ1dFcvZkhsNTY2U1hjdVhNbjd0MjdGMnRxYXJDK3ZoN3I2K3V4dHJZV1MwdExzYkN3RUY5NjZTWE15c29DQU9lNXJhYVF6YTdNekV4WXVIQWhidDY4R2N2THk3R2lvZ0lEZ1FEVzFkVmhUVTBObHBXVjRhNWR1L0RsbDEvR3pwMDdDM1BYYVo1UTFjTGs2M2JsbFZkaWFXa3BGaFVWWVdGaG9iZzJUdWFmMms5ang0N0ZvcUlpM0xseko1YVVsT0NVS1ZPUTV2L1UrVW01UGZUNzNOeGNLQ3dzeEowN2QySlJVUkdTZ0c3c1dzbWE3UExseTdHd3NCQUxDd3R4OXV6WjZLVGx5dTlQTyswMC9PNjc3N0M4dkJ4WHJseUpKNTEwVXRSOTJCaXlCZkQvL3QvL3d4MDdkbUJ4Y1RFMk5EUmdLQlRDWURDSTRYQVlnOEVnRmhjWDQvcjE2L0dCQng3QTVzeUpNbTBFdXBGbGN5TTVPUm5HangrUG9WQUlUZE5FeTdMUXNpd01oOE5ZVVZHQmUvYnN3ZXJxYWpRTUF4RVJEY05BeTdJd1B6OGZjM0p5bWxWL1VsSVNJQ0thcGluS0lYUmRSL2wvaEdWWitNTVBQMkNQSGozaXFrUFdOQk1URStIUGYvNHoxdGJXb3E3cmFKb21CZ0lCM0xObkQrYm41Mk4rZmo3dTNic1hnOEVnbXFZcGhQK01HVE93ZmZ2MkI3Uy9iMlptSmt5ZE9oVXR5OEpRS0lTSWFLdHoyN1p0dUh2M2JneUh3NksvUTZFUS92ZC8vemY2Zkw1R2sxV281cjdiN1lZeFk4YUlmclFzQ3lzcks3RjM3OTdpdDNJNThubjA2OWNQcXF1cmJmMS96ejMzb05QNWtrQlNweSs2ZHUwS1ZDOGlZaXluRktGNmlYLysrV2R4elUzVHhFY2ZmUlF6TXpNQklIcGVGV0NmTUN3c0xNUkFJSUJidDI3RlFZTUdOVXNZZWp3ZXVPaWlpL0RYWDM4VjF3VVJNUktKWUhsNU9lN1pzd2RyYTJ1eHJxNU9mSStJV0ZaV2h1UEhqK2NnVVlranZoenZZQ0VUaDB5b3BLUWtlUERCQi9IT08rOFVac2JHalJ2aHd3OC9oSktTRWlnb0tJQklKQUlkT25TQTNyMTdRNzkrL2VEaWl5K0d0TFEwK1BUVFQ4VWk4M2hOV2JwaEtlUE92SG56d0RSTnNDeExtR1lwS1NuUXBVc1h1T3l5eThBd0RQQjRQREJvMENCWXVIQWhqaGt6UmdzRUFxQnBtaWpETUl5b1BJOHVsd3Y4ZmovTW5Ea1RwMDZkQ3JxdWc4ZmpnUTgvL0JCV3IxNE4zMy8vUGV6YXRVdlROQTI2ZCsrT2d3Y1BodUhEaDhQbzBhTWhGQXJCOU9uVDRZUVRUc0FwVTZabzVlWGxva3k1blFBUTliNWp4NDR3ZCs1Y0hEdDJyT2pqdDk1NkM5YXRXd2RyMXF5QjR1SmlUZE0wNk5hdEd3NFpNZ1RPT09NTUdEVnFGUGo5ZnBnL2Z6NE1IRGdRSDN6d1FhMnNyRXlZaUxLWkt5L2hRa1RSTGxselRFMU5oUmRmZkJHdnUrNDZyYVNreE5ZLzFLYk16RXg0OWRWWE1UMDlYZHdUOGh5bmJHSnJtaWF1TTlWUDk0cXFzZEoxa2Y4dnY2ZnBCL3FPekdxNmx0T21UWU9FaEFTY08zZXVWbEpTWXF1VEhoSTBMU0RYcXk1em8xYzVJNHpMNVlJcnJyZ0MvL2EzdjBHblRwMUExM1dvcjYrSC8vdS8vNFBmZnZzTnRtM2JKcVp2ZXZmdURUMTc5b1FSSTBaQVhsNGVGQllXUWxGUmtlMWVsdTk1RHBwdWc2ZzM3dzAzM0lDVmxaWGk2VHh2M2p3OC92ampiWnFCUE1HZGxwWUdJMGVPeERGanhtQlNVaElBTk0vTGxwS1NBcVpwaXFkdVJrWUdBT3lmdFBmNy9aQ1FrQUFkT25TQW9VT0hZaUFRRUUvbjR1SmlQUHZzczhYVFdZMnRVNWsxYXhaYWxpVzB6SWtUSjJLSERoMml2T0gwbDVtWkNRODg4QUEyTkRRZ0ltSTRITVpISG5uRVVSdHdNbVVCQUtaT25ZcUJRQUF0eThLYW1ocWNNV01HZHVqUUllbzNKSGl5c3JMZ3BwdHVFbHA1SUJEQW1UTm5vcXlCT1ptZDh1Yy8vdkdQUXJzekRBTU53MERUTklXV3B6cTZORTJERjE1NFFSeEw1MnFhSms2Yk5pMUswMnBNNCtyV3JSdWdoSk5XNi9TZVlpdlhyVnNuN2oyNlZvRkFBSjk5OWxsTVNVa0JnR2d6ZWNlT0hZaUltSitmajBPR0RCSHRqYVhGeTFwc1ZWV1YwSUtycXFwdzhPREJvaDdaSys5eXVTQWhJUUZPUFBGRWVQVFJSN0ZuejU3Q29STXJIT3Rnblc3TVlVWWVaRGs1T2ZEVlYxK0pBZkgyMjI5alVsSlNWREF0UUh5ZTQzaUVZbHBhR3BCSlpab21wcWFtUnYxV0R0Vlp0R2lSTU51cnFxcHNwb284U05TNisvVHBBNy84OG9zdzMvNzYxNytpR3J5ci9wWmVQL2pnQTJFK05UUTBvT3g5VmIzT2hLWnBrSktTQWhVVkZVSzR2UDMyMjZqK1RnMVFkN3Zka0ppWUNETm56aFRUQkpzMmJjTCsvZnMzZW42eGhLRnM5cG1taVgzNjlMSDl6dVB4d1BqeDQ3R3lzaElOdzhCSUpDS0VQeUxpZmZmZGQwaUZvZnpuOC9sZ3c0WU5HQWdFTUJnTTR2YnQyOUd5TE5FUHp6enpUTlFEOTl4eno4WGk0bUswTEFzM2I5Nk1nd2NQUnZrK3BUNVYrOW5uODhHU0pVc3dFb21nWVJoWVgxOXZtd2VVNTBHOVhxLzRMZVVKamNXeExBVGJmS0NSYk9ha3BLVEFhYWVkQmk2WEMwS2hFQ3hac2dUQzRiQzR3T0Z3V0FTOXV0MXVTRXBLZ29TRUJNakt5b0xrNUdUSXlzcUN0TFEwRVNZVGo1bEFwaElBUURnY0ZnTmVEck9oLzE5NDRZVjQzbm5uQ1ZPdHBxWUdkdTdjS2NxUzg4Q3BkWjl6emptWWw1Y0htcVpCZm40K3JGeTVVaVN6b0hiSWJaTDc1cXFycnRLb1BRa0pDZkNmLy9tZktIdmVBY0NXRklQTXZxRkRoMkpXVnBid3h0NXh4eDJhYkJiS3BpME5NdE0wUWRkMVdMWnNHZXpac3djUUVYcjA2QUVEQnc1RThvaXI3VzJNSDMvOEVhWk1tU0s4L0JzMmJNRGpqejllUE5neU1qTGd4aHR2aE9Ua1pIQzVYUERVVTAvQnh4OS9MSzVIYytvNkVNZzh4dDg5ejZGUVNGZ2V0OXh5Qzd6NjZxdkNtM3pycmJmQ3M4OCthOVBtdytHd01MWHBuR1N2Ti9XcG5MaUVndUI3OSs0dHdvcW1UNTh1VEhTZnp3ZUlLTXhyT29idTdhU2tKTWpNeklSMjdkcEJjbkt5N1h5T1ZVRUljQlRNR1pLdytWMlRRWUI5RnpRU2ljQy8vLzF2VFJZV2RGTysrZWFiNlBQNVJMWU5DaUIydTkxUVdsb0tEenp3Z0xacjE2NjQ2ZytId3lLc0lURXhFV2JObW9VMEQwUmFoZHZ0aG5idDJzSGd3WU9oYTlldUl1U2lvYUVCZnYzMVY0M2FoMHFJQjMwUHNNK0prWktTQXBabFFYRnhNV3pldkZrSUpnQjdLaWoxbk1QaE1HemJ0ZzM2OWVzSExwY0x6anZ2UEhqenpUZkZmSlZhRjUzUGlCRWp3TElzOFBsOFVGUlVCRFRuUllLSkhnUXVsMHVFb25pOVhqQU1BMzc4OFVldHRMUVV1M2J0Q242L0g5TFMwbXkvd3pqRFJyS3lzdUMxMTE3VFRqenhSSncwYVJLNDNXNllNMmNPWG4vOTlWbzRISVo1OCtiaDJXZWZEWlpsd1U4Ly9RUXZ2dmlpOXRoamp5SCtuazNsY0F4dUVsaCt2eC84ZnI4dFljSE5OOStzVlZaVzR0U3BVeUVTaWNDRUNSUEE1L1BoZmZmZHB4VVZGVUZDUW9JSXJTRkJKNGZoeUE4dHVsNlJTQVQ2OXUwcjdnZVh5d1g1K2ZtaVBTUVVYUzRYWEhubGxYajExVmVEMStzRlJCUjFVU2paM1hmZnJXM2N1Rkg4TmxaNDJyRkFteGVHOG9SdlltS2l1R0hvQnZYNWZCQ0pSR3dYOXB4enpvSE9uVHVEcnV2aUpnSFlkL1A5OXR0dmtKS1NJcjV2eW9sQ1QzUFN2RzY2NlNZaGhGUm5BRUZDY3N5WU1WcHRiUzBBMkRVTUVvcjBXVzRMeFFsR0loRnhRMVBiU2R1UUoveEpLRk84R1QwRVpJRWtPMDFJbzlBMERkTFQwMjBUK2pTWUlwR0lUWkJTV1NRVXFRMDBjSDArWDVTamh2cXNxZjZ0cmEyRit2cDZtRHQzcmpaczJEQWNNR0FBWEhUUlJUQisvSGlzcXFxQzhlUEhpL2FlZnZycEdtbE5jcDhlU3VnY2FQNHRIQTdiQXVVdHk0Sjc3NzFYeThqSXdCdHV1QUVNdzRDcnI3NGFFQkd2dSs0NmphNFZQYWdwNUlydUF5cUQ3Z242VEhHSWRJL1I5M0k2TFVTRWJ0MjZ3V1dYWFNiYXF6ck9aczJhaFlpb0Fld1hnTWNxYmQ1TWx1Zk15c3ZMTmZJMGhzTmh1T1NTUzlCcGo0MWZmdmtGVnE5ZURkOTg4dzBzWDc0Y3RtL2ZicnV4M0c0MzZyb2VsemRaenVObUdBWlVWbGJDbmoxN1lQZnUzVkJXVmdiRnhjVkFaVm1XQllXRmhiQnc0VUxvM3IyN3RtM2JOc2U1UGhWZDE0VzViMWtXOU9qUkEvcjI3WXR5KzBpSXlZS1FTRWhJZ083ZHU0UFg2d1hUTk9ITEw3KzBwV3BYaFFlVnNXclZLbEZ1VmxZV1pHZG5DNjJEaEN3ZEQ3QS9kTVRqOGNEQWdRT3hVNmRPNFBQNVFOZDFLQzh2anpxL2VQcVhoUEQyN2R2aHlTZWZGSjdrbVRObndxSkZpMERUTktpcnE0UExMcnNNUXFHUTBMRFVQNW1XSFBDa0JacW1DYUZRU016dkJRSUIwWGNBQUpNbVRkTGVmdnR0Y2I5T25EZ1JYbi85ZGN6SnlSSFhsc3hadVgxeXNEeVp5eTZYQzJwcWFpQVVDZ252OHRDaFF3RUFoQWNhWU45MUxTMHRoVldyVnNFWFgzd0JxMWF0Z2kxYnRvaHJweTRxb0xMcC9iRXNHTnNrOHMyZWs1TUQzMzc3TFpKM2Qvbnk1ZGkxYTllb1kybWVoTXlhdSsrK1cweVk1K2ZuWTY5ZXZlSmVPNXlTa2lMaURFM1R4REZqeHVEbzBhTng1TWlSZU9HRkYrSUZGMXlBNzc3N3JuQjhyRm16Qm84NzdyaW9DWEZxbi9wSzcwODk5VlRjdW5XcmNDYk1uVHNYNDAwazhmVFRUNHY2YTJ0ck1UVTFOZTZBOG5BNExCd0FjK2JNRVVIRWFueWQ3TFJKU1VtQnVYUG5ZakFZUk11eThKZGZmc0dUVGpvSlplMDFYbS95NnRXclVlNm52L3psTDhKVGk0Z1lDb1Z3M3J4NVNOYzBOVFVWM243N2JmSDcrKysvUDhycDB4Z0g2azBHMkhjL2taTXJHQXppUlJkZGhBRDdIV01KQ1FudzlOTlBJOTB2dXE3ajExOS9qWFYxZFdpYUp1N1lzUVBQTys4OGxQdFNyb2VtWEFEMmFmTkxsaXdSbnZiUzBsTE15OHVMT2grZnp3ZUppWWxpVHZlbW0yNFM4YW1JaU1PSEQwZTUzTWJPajJrRDBFWHplRHp3d0FNUENBOWJLQlRDVjE1NUJaM1NKZEVONm5hNzRiNzc3a1BFZmVFSlc3WnN3Zjc5KzhkZGQzSnlNdEJ2RVJIVDB0S2k2anJoaEJQZzExOS9GZDdPdi8vOTcrSTQ5VmhxazNwdW1xYkJQLzd4RDR4RUlxanJPZ1lDQWJ6bW1tdEVVSEFzN3JyckxsdkE3YTIzM3Rya2IyUWVmdmhoTWVCcWEydHgwcVJKWWw1V2JxdThVbWI4K1BGWVhWMk5wbWxpTUJqRVo1OTkxaVpVMU1HdWxqZDY5R2doN0w3ODhrdWJBTlkwRFo1NTVobmJ3NHVDbWowZUQvaDhQbmpublhkc1huZjVmTlIrVmg5SWVYbDVRaGhhbGhVbERPVzJxbmc4SHZqcHA1OFFFYkcrdmg1SGpCZ1IxZGVabVpudy9QUFBvNjdySXFxQTJycHQyelk4Ly96em83emZUaGwzdkY0djVPYm1pbnZQTUF3c0tDZ1FEMW9aT1NHRWt6QnM2cnlZTmdiZEFBa0pDZkRwcDUrS0FZeTRMOXArMUtoUjJLZFBIK2pTcFF2MDd0MGJ1bmJ0Q3NjZGR4eWNmdnJwU0Rjd0RTNDVCcXNwTWpJeWdHN3NZREFvWXZBSWV1cE9uanhaaFB6b3VvNjMzSEpMbEFZQUVIdHhQZzNLclZ1M29tRVlHQTZIRVJIeG5YZmV3VUdEQm1IMzd0MGhPenNic3JPem9XdlhydEMzYjE5WXVIQ2gwRmlEd1NBdVhyd1lPM2JzYUd0YlUrVGw1Y0hISDMrTWhtRUlUZStGRjE3QVBuMzZRTGR1M1NBckt3czZkdXdJWGJ0MmhVR0RCdUc3Nzc1cjAzeldybDNyYUd1cFFsUm16Smd4b3A4Kysrd3ptL0QxZXIzUXUzZHYrT0tMTDNEejVzM1lybDA3MGNma2tYM25uWGVFMWtoeGhuTFlpWk1RSmtIYnNXTkhRRVJ4cmVJUmh2TEQrS2VmZnNKSUpJS0JRQUF2dnZoaXNmcEYvazFxYWlxOC8vNzc0djRrU2twSzhPS0xMMGFLV1hTcVQwMklNWG55WkF5SHcyTDFVME5EQXo3ODhNUFlwMDhmNk5XckYzVHAwZ1c2ZCs4T09UazVjUGJaWitOYmI3MGw2alZORTRjTkd5WXNqTWFtRnBnMmdqcXcycmR2RHkrLy9ES0d3MkcwTEV2RXFEVTBOR0JoWVNGdTNyelp0azZaYm83UzBsSjgrT0dIeFFDTGg5L0RHTVRnVFU1T2p2b3RQWm1mZSs0NVJFUng4OTU0NDQwb2EzN3FPY2thSTUxamNuSXkvTy8vL2krV2xwYUtRV1FZQm03ZHVoVlhybHlKWDMzMUZlN1lzVU1JSTlNMHNhYW1CaDk2NkNHYkdkWFlFak9WL3YzN3cvejU4MjFML0F6RHdQejhmRnkyYkJtdVhyMGE4L1B6UlV4Z09Cekc3ZHUzNDNQUFBTY0VvV3FHT1FrVU90ZlJvMGVMNjdKMDZWS2s4QkRDNC9GQVZsWVdkT25TUlh3bS9INC92UDMyMitLYVB2VFFRMUhDbUV4R2VYMDR6Y3ZsNU9RSVlXaFpGanIxazJwS3l0ZUlIcXpoY0JqUFAvOThsSStWajB0UFQ0ZEZpeGFKZXhRUnNiQ3dFQys4OE1LWWZlWlVmMUpTRXR4MjIyMWlDYWE4N0xPMHRCUzNiOStPaFlXRkdBcUZ4UDhvZ0g3cDBxV1ltNXNiOCtIQXRER2NCclRmNzRmVTFGUVlQWG8wcmxpeEl1b0pMR3N1aVB1Q2dxZE5tNFpEaHc0VlFiSHhrcEdSWVF1NlZqVkRtWVNFQk50OFZtVmxKVjV6elRWaXdNZ3JZOVNnYlptMHREUVlObXdZenBrekIzZnYzaDExYnRTV25UdDM0b01QUG9qRGh3OUhPWjZzdWFtYlNCQ05HREVDNTgrZmoyVmxaVGJ0Z2dZWTRqNVQ3Lzc3NzhkVFRqbEZKS05RcHlib3MxT3VRcmZiRFdQSGpoV0M5ZjMzM3hjYXRPeWdVZHRHeHlRbEpjRjc3NzBuVnFEY2Q5OTlLQ2VvY0xwZlpJR1htNXNMdFA0NkhtRkkrSHcrOFBsOHNIYnRXcUdoWFhMSkpXS2VWRTB5NGZWNklTc3JDMTUrK1dWeEg1YVVsT0RsbDE5dVcyVkQ5VGs5TEtudkVoSVNZTWlRSWZqY2M4OWhUVTJORU9UeSttNTZXTmZWMWVHQ0JRdnd2UFBPaTFxOXBKN2pzYVlkdHZtemxjTTE1SFdiQVB0REJUUk5nNkZEaCtKcHA1MEdQWHIwZ05yYVdpZ3VMb1pmZnZrRmZ2NzVaeTBTaWRnOHNmSjY0SGhTZUkwYk53NnJxNnNoSVNFQi92blBmMnB5T2lieTRGSTVmcjhmc3JPelFkZDE4UHY5RUF3R29heXNUSGdrQWZiZDNPU1pwTy9VSGNoa0V6czVPUm02ZCsrTzNidDNCOHV5WVB2MjdkcXVYYnNnRUFpSTQ5WFlQdGxyMk5UNXFXRXhMcGNMMHRQVElTOHZEN3QyN1FxUlNBU0tpb3EwclZ1M2dtRVk0amlLQVkxVmg5d20rWDIzYnQzZzlOTlBSMTNYWWRldVhmREREejlvVkQ4ZHA0YnB5RzA5OWRSVHNVdVhMdUJ5dVdETGxpM3d5eSsvYUdxOUZLSWtwODJpT2NkTEw3MFU2K3ZySVRVMUZkNTg4MDBONC9TcSt2MSs2Tnk1TXlRa0pFQTRISWFLaWdxb3JhMk51ZlliWUovd1B1T01NekE1T1JsQ29SQjgvLzMzV20xdHJRaGZvcnJsT01QRyt0SHI5VUt2WHIzZzlOTlBGNDdBb3FJaTJMcDFLL3o2NjY5YWFXbHAxQ2J2TkU3a2U1QnBvelNsNlRpWlpoU1BGc3RaRWE4M1dhMjdxZldrcENVNHJhK045WlNXVFVRNWppNVdPK1NKZC9KYXk5clZnU1N4SmRUVVQ2cUoxVmpiVkEweFZ2dGwxRzBhbk9xVkhRU3gwb2JGeWxyalZKNjZpcWd4bk14S3ArdW9KbTJWUGNYMFA5a3lvTitvWm5aajV5WDN2MnhpcSthMldrNnNlNVZwZ3poTk1NdmVZa0tkZTZMdkdqT2Y0aVZXbXZ0WVhtS24vOHMzcm14YUFkalhWYXZ0ZFVvTkZVdmdPSGtvbTBJZVBQS0R3bW0vRXZtOTB6d2gvVlplSFNJUFl2azlIU3YzclNvd2lLYXVYNnk1c0ZnUHMxaGx4cXJYcVM1NndNbk9IN1Y5alRuUm5CNlFzb05EWGFzc2x5dTNRL1pBTzVXbkVtdXVrbW5seURlTStzU1RKNnpWNzVzYUtNMFJoRTQzbU5NTlJkODVEVGluOWpqVm9aWVhTek9nMzhqOTR5Um9ta0oxRmpUMjN1bWhJczhQT3JVLzFweWMrcDBxU0p5Y1MzSzhvM3ArVHYzbmRLL0UrbjBzMVA2UHBYVTdoWGM1WFF1bnBDSk5PYnZVKzBydGw4WTBkcWY3c3pGUFA4TXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13RE1Nd0RNTXdETU13VEN2aS93TXZ0T1Q3WHl4MmZBQUFBQUJKUlU1RXJrSmdnZz09IiBhbHQ9IlImYW1wO0ogR3Jvb21pbmciIGNsYXNzPSJsb2dvLWltZyI+CiAgPC9kaXY+CgogIDxidXR0b24gY2xhc3M9Im9wdCIgaWQ9ImJvb2tCdG4iPgogICAgPGRpdiBjbGFzcz0ib3B0LWljb24iPjxzdmcgd2lkdGg9IjM2IiBoZWlnaHQ9IjM2IiB2aWV3Qm94PSIwIDAgMjQgMjQiIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyI+PHJlY3Qgd2lkdGg9IjI0IiBoZWlnaHQ9IjI0IiByeD0iNiIgZmlsbD0icmdiYSgyNTUsMjU1LDI1NSwuMDgpIi8+PHJlY3QgeD0iNSIgeT0iNyIgd2lkdGg9IjE0IiBoZWlnaHQ9IjEzIiByeD0iMS41IiBmaWxsPSJub25lIiBzdHJva2U9InJnYmEoMjU1LDI1NSwyNTUsLjU1KSIgc3Ryb2tlLXdpZHRoPSIxLjUiLz48cGF0aCBkPSJNOCA1djRNMTYgNXY0TTUgMTFoMTQiIHN0cm9rZT0icmdiYSgyNTUsMjU1LDI1NSwuNTUpIiBzdHJva2Utd2lkdGg9IjEuNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+PGNpcmNsZSBjeD0iOC41IiBjeT0iMTUiIHI9IjEiIGZpbGw9InJnYmEoMjU1LDI1NSwyNTUsLjU1KSIvPjxjaXJjbGUgY3g9IjEyIiBjeT0iMTUiIHI9IjEiIGZpbGw9InJnYmEoMjU1LDI1NSwyNTUsLjU1KSIvPjxjaXJjbGUgY3g9IjE1LjUiIGN5PSIxNSIgcj0iMSIgZmlsbD0icmdiYSgyNTUsMjU1LDI1NSwuNTUpIi8+PC9zdmc+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJvcHQtdGV4dCI+PGRpdiBjbGFzcz0ib3B0LXRpdGxlIiBkYXRhLWkxOG49ImJvb2tfb25saW5lIj5Cb29rIE9ubGluZTwvZGl2PjxkaXYgY2xhc3M9Im9wdC1oYW5kbGUiIGRhdGEtaTE4bj0iYm9va19mbG93Ij7Qn9C+0YDQvtC00LAg4oaSINCj0YHQu9GD0LPQsCDihpIg0JzQsNGB0YLQtdGAIOKGkiDQktGA0LXQvNGPPC9kaXY+PC9kaXY+CiAgICA8c3BhbiBjbGFzcz0ib3B0LWFycm93Ij7ihpI8L3NwYW4+CiAgPC9idXR0b24+CiAgPGRpdiBjbGFzcz0iZGl2aWRlciI+PHNwYW4gZGF0YS1pMThuPSJvcl9jb250YWN0Ij5vciBjb250YWN0IHVzPC9zcGFuPjwvZGl2PgogIDxhIGhyZWY9Imh0dHBzOi8vd3d3Lmluc3RhZ3JhbS5jb20vcmpfZ3Jvb21pbmc/aWdzaD1NV3htZEhOcWNYRmthbk52YlE9PSIgdGFyZ2V0PSJfYmxhbmsiIGNsYXNzPSJvcHQiPgogICAgPGRpdiBjbGFzcz0ib3B0LWljb24iPjxpbWcgc3JjPSJkYXRhOmltYWdlL3BuZztiYXNlNjQsaVZCT1J3MEtHZ29BQUFBTlNVaEVVZ0FBQU1vQUFBRENDQVlBQUFBSU5sM0ZBQURyeUVsRVFWUjRuS1Q5YWRNc1czYlhDZjdXM3RzOUlwN2hESGNlYzFRcVU4cUJuS1RVZ0tTVWxFZ05BZ0ZpYXFPcXJNdm9idXQ2MFMvYStndjBKMml6dHFhTExpaXNxZ3d3Q21nd0NpZ1F4UXhDSUZGU2Frb0pLVk5LS2ZObTN2bWU0UmxpY045N3IzNngxbmIzZU02NUtsVzNYNHNiNTRud2lIRGZlNDMvTmNtUC9aay9Bb0NJWUVmMTU4RE53ODZ4OTFXTFBaTkFLa0d5UWtXb1FLRFdKTFVDRW9sSnBzOGtnUkFTUVFPMXdGaEFWVm1sRFZvRmNtWFZuekx1UmlBU1MxS1JSR0pOTFlLV2lOWklGM3U2dE9ac2RjNXF0ZUZzZmM3SnlSbW5xMVBXcTFOTytnMHA5cVMwb3VzNk51bUVydXRJR3YwMkZWVUlkUTBLaXFLcWlMYTd0MnVPQ0xVcVZLSFdTcUNDbjZ0YUVKM1hTVlVScWoxWE93ZTE1NkQyZnZ2ZTlqdXFNbjBXbUQ0WDZudzk2dC9SUHFlcTFHcS9reEI3cjFTV2g5WktyWFcrSDUzM1dGWFJVcWxWRVlsb2xjWDM2L1MrcWtMVkJXM1krN1ZXY3M1a0hSaDF6NmdIeGpJeTFwRlNpdjFkQjBZWkdNckF5TWlvQnpJalZTbzFLaUl3eEFQN3RHWG9Eb2dvSlJSS1BhQ3hranJRVUtXaUhQTE9mcnVEZ2xKQ1JrUW9XcEhhUVJCRUJKVmlkQ25WcmhORkpGQUVJRkdCV0FNQkNMVlJ1TjlubzN1cDB4b0JDRVl2NlpoSmZpOUg4Qzg5WmlSVnNLOEo0TVFUUXBwK0xFVFFJb3pEaU9hQkVDSmQyckFLUGVPUUtUVVNpVkNTRHRlQnFHZWtzT0xzNURaM3pwL2loZWRlNXRsblh1THUrWk4wM1JxcENhbFF4MmpFV3BSU0ZCMGc1MElkamJpVHJxbjd5cjRvdTVMUlBFSTFBb0FBZVVCVlVDMU82SGJFOXEvYW5uVWlPRnZNZWtUNGJYR0RNMUU3WDlycjJnaHRYc1hITVVCampxQ3czQlh4NzJ2bjB4aEtaR0xNbzZQcTBlOEdPYjdPaVJISTlsb1ZFM2pxKzF0dFRTS1Jxbm5hOTBBRVVaSjJCQkdTUkZheVJsT2xTalVpaUlVcUZaVkNqUXFoVWtObTBKR3hIRGlNZTNiRGdWMis1aUpmTUpROXhFcVdBMWt5dWU3UmtoazVhSldSMC82VVFROHlqcG5VUjBESU5kT2xuZ3JVV2lrb0twWGdaQ2tTaUg2dkVhaFVJb0ZBUlRRY1VhL1IveXdzNXRjQWZ5azl5aVEzTmNrc3FWU0RmWUZHbEVZSWFsODJNWWt6alFZa1JHS01IQTRIYXEzMFhjZW11d1ZScUVXUUhOR1NXTW1aQmxiMFljTXp6NzdJZTE3NElPOTc4VU5zK2pNaUcrb1FHUGZLT01EdVRiZ2VsSm9GcVVJK0FLTlNTNkhrU2lnUjFXZ2FvQ2ppa3JWbWs3cWlnbWlUbkpsQWRBbWRKMElYa1lYa3QrY21rUVhYSkUzaWkvanJkU0o0VzRsalJncm8wZnMzTlVWYjlmYVpLUE5ubDVwbDJpVzExUjlyUFdLb21TRUZiVnNqZ3NyTThLYm8yaTlXbDhBQmFGTFlaSUZnMzhIMDNLakkza3ZTbzlyWmRVbWxVSnpBakdpUlRPZ2lLaFdWak9MTTVCZFpZNlgybFVFR0J0MXpsZS96WUgrUEs3M1BHUGNNYWNkZTlveGxoTFRTMUswWWNoVkNJUkFaRGdQckZLaWhVZ0lnZ2RJc0FMVTFUMDF6UzUySTN0WW5vRGRrSWM0d1I0ZXZXK0ovMVZGQmsvMUFXMml0U0lpQWlvblI0THhueEpxTElwbzQ2VllFVFl6WFNwS2VMbTUwM01QWjZnN3ZmL2xiK1BZUC96NmV2UE04T25aY1BjalVCeXV1Y3Nld1ZjWTk1RDJVRWNxbzZCaW9WZEdpaEJLcHVWQkhlMTlxUlF2VUFscVdSQzB1cWUxWnF5SzFVUFZnOTFWMW91S2dGWkZJb0RGQjB6Qm15aWpGL3Qya3VOUUZRd2lJT21QTkdtcjZ1MzNreHV2UVREZUltQVlJQWFUTzV1N3ljKzFvMTlnTzFmbDFnTEZXb3N6bkhETmNSWnhvbFdyM3F1MXZKYWdRUXJzaWtJbWQ3ZStxbFVTaVNpQm9kZlBFVE5QSlRKZUtCZ1U2RkdNU0VTVzQ2TmVnWkFxYU1rK3ZuaUNmdmN3K1hIR3RGMXpXaDl3YjduTzF1K0RRSFJocjRWcDNLbUdrZGlwZGlPaTRSMElGSWhvRXhjeEpvY0lrNUtxaVVLV0tzYys4bnZYbWdyN0xrVlNhSkd5RTd3dTZaTC81Wlg4T29FSVZDT0lMVEtSV0ZkUTJKUktvVlVpeEk0YU9mSUJ4RE5ySk9aMmM4UEp6SCtSVEgvMGNMejN6QWE0dkNzTzJjUDkzaEhFZmtYTEcxVHNEaDYyU3R4VXRDYkpRczFKR3lJZUJZUmlOUVNwUXFqMVh4VXhVNDFtcFRXb0xXb3daWkNIbEkwb002aExWQ1ZicVJCYUNvcmxNR2taQys2d1JXRkFJNGd3Z3MwOEQxVTBkKzk3QXpFalRlV0prRjZpUE1GQ2xraVJRUTlOb3g5Ky8xQ3lsbVc2TkVmejFpY2xGRUV4RkdDUE1leW9pazNCb21rQ090NTJ4alBQMzNIZ1BZQWdGUkNiVHpzNjFkUXBSR0FielM2ZnZrTEJZcjBxTlFpK1IwQ1YwVkhKZHNRNXJidmRQVWZ1UmNUMXlVUi93eHU0MTN0cTl6YnEvWmhkM0hNcGVkWlBJVWhra2lxS01wU0pKQ0NFZ05WRExTRUNvRWdTcTAyNmRISWZxdEt6TGhadU94Zy8yL0h2UUtEZDlFbk44TkFUUUc0NmVSRkFsYUVBMUlocGhTSWhzVk1iQW5mVXpmUHpEbitXakgvb3M2M1RPOVVQbG03OCtVSWVPT2lhdTdvOWMzTjhUTmJKOVVLRkV4cTFRaHBGaFA1SVBtVEthVFIwbEVHTWs1NHlLRTBhZHpTcHhNMUdjZENPQ2FKMWxvZ1NDS0pvSHdKeERDU0JxVWkrNGdvbWh0NDEzUmpLQ1Y4U1pUR3RHMm45cTZ4TXdZbWorUXpSU2RiUFZHTlJFaVg5UFd6dG5MTVFacnRyN2pkaU5vWXk0VmNVWjJ3WFREUk82TVVvcHp1Z1ZST3A4SFU0Yld2MDczRWRCQXhMVUJXZWxUeXU3cnZiKzBsY0pTdFpxQnB1dmx6S2JlNlVvNjlqYks3V2FsbGRqTEFscTYrMm1zUjZFR0NQcnVJYXdwZ3lGb2U2cE1mRDBuZWQ1OHM3VFhOVDd2SlBmNUVHK3g4UDlBeTUzRHptc2xiRE9tanFWRUFwREdkQmEzQnVJWnVxQmdVY1lvNmhBbGNLazcvV211L0hva1JycHRGT2JHY0FDSllGb0N4R2FtYWNneFd4WkRBMFNxaE5mUklzUWFpVG9Hc3BLazl6bWV6LzdnM3ppSTkvRjRTSng5VnJtWWhDdTcyWEtZY1BsdlQySDY4eTRGdzdYZ2YzVk5idnJqSlJFelVvazJIZlQwOUdoeGRBbkxVSVVjK2dpNGhlb0p1V2RjRlhkd1Z4S1JWVzBHb0tTUWdJSlJsZ29RY3BzTWkwV1dkVE5NMVUzWmF1Wkp6Rk1SQmtBWER1SUtDYm5sT0FTMUV3Nlp4cmN2TUswRW1xRTAzeVBjQ1RoMjViYWJqVUdtWTF1blh5cEphT2cwTWN3YVV3V1RCSVFWSnFwWnI4OUg0MUJHejNZdjVWcUptdFExQmszaE1sQm5meWo2ZmRGR1lZRElmcnZoaVlRVEs2SEtpYndYS3ByVnZiam5oQUNxVStzNG9Za2llSEJIbmJDcmU0V3A5MmFsL29YMmNvVnIyNWY1WTN0YTF6a1MzYmRvTklOMEZjNVNERVRMNEpXeDdaTVFvQUlTc2JNajJEYnpMeVVLbzlqbXZEdUdxVWhBZEpRbGJZdllnc2xVdFZlVDZLNW9rQktpYWdkdFFxUzEzUnlyaC85MEdmNStMZDlMMmZkczd6NTFRUGpoWkszS3g2K3MyZThGaDdlZThDNGcvRlFPZXdxbWp2cUdPakNPWnFWaFBrS1FjMThDVlFJbGVERVVuREdVQ2JwRFJCZElrcElJRXFzdGhyQmFTeTQyZzB1ZTRLNmJZMUFyVzZ1TlBTcDJmbk5iREJ0SWUzOTV2eUxtWHVHSHRsNWh0SDQ3NFpaNjhUR2hNaDBMbzVpbVZheXRRN1RYamhCaWtIUWdpd1lhOTdwMlJkcFFJTWVvWG5OQ2xBSFljUk5aYlRaN1hWeXpnT0MxdHdJQXNIOEpoVXhkRzlpRGdjRFhDTXB4VFNUS0YwODlxSE1WOEN0ampMRDhpS0VDSjFHTzM4c2xGRUpLYkFLSzlnWHlBbkdqdUg2d0NwMjNENi94WFBwT2I2Ky96cXZiOTlnMjI5Sm9kT1VrdVM2TitSTmxTcW1YWnB2SWhKUWRlMzZHSFB5SnBQQTB2VHlqWnM1S3pxQ1pRUVNSTWg1SVBaQ0tSbWswSGNkK1RDaXFweHV6c2w3b0hhVVhkRDNQZjlodnVjelA4S1RaKzloLzZEajliY0sxKzkwREJlSjYzc2pGL2NHRHJ2Q1lac3BHZW9JYUNTRmpwWDBhSTBHRlBpbFNwZ01IRWR6Z2lNYllsSk9iUUZTWXhTcEZxTlEweTdtQ3pRM1ZCY2FGR09VNEl3RkJpTTJ4OTRFb1RNV1JCYy80dDhSR2xIaXBwbmI0OEhQQzQ3eE40SUtNbXVmMlhrT0x0Rmx1bzltU2kwMVd6dmlaUDRkbTlaTlcrSHJCUkNDR0ZwWnpXU2VtUTcvM1RCRDFJMmdGNDYvM0FBTEFzWW80c2lTU0dEU2tkcXVTSTQxM2cxaUREU2ZjT2wvbVhsbnkyYjNtd0Fad1ZFTlNnN1VxQlllRUtIdU1zK3NPdFp4eFozK050OGNYK09kaTdlSWE5V3hFdzVoTHlSakZDTHNoNUVhbGI1UFZEVjlmL09RU1dBMEg0WHBXbTRjeHpHU1VncDl2MllZOXZSOXh6anVpWjNvY01oRVg2U1Q3b1N5QzZTeUp1WlQvZDd2L0R6ZjlvSFB3ZUdVdDcrbTdCNG9EOStvWEw4TjJ3Y0hyaDhNakRzbGtOQ2FUS3BWTlNta0hjVGt6QkVSVi9XaU9KNC9YNzJJbVBOT1E2dm1qVEJwMlV3ZGRiaDExajcyQmRYTkpwMld4c3pKNGt5Z0x0VjEvcHZxUG9WdGNGejhwcmpETDA2UUVYR0dxNVBUUHpuMXdSaXNVWFpqUE1SL1I5WEJnL21ZNEdhcGs0OWpIRll0QnVWNzE4eWx0dkd5MkhjY0xuQ1N0OS94MXhlZUM3UnJkekFCbFptWmNiVXNPcUZrU3pPd1BZczJlcHFaVUpzNTE0eWU2Z0xLZmJTd2lDdFpvQ3hBTmI4aWlDSkZxQVVrUWh3aWVsMTU4dlF1SnljbnJPT0cxZEJ6Yi84MjErTUZxVlBOV2hqaUtQdHhwRi8zRUFOakdTd3NVQ3M4MXRSYUhLNGxVM054cHMwNEVsSFFyMWNjRG52NnJxUFdRdGV0R01lZG5LeFBHTWZDcGp0ajl6QnoycCt6aVUvcEgvcVJQODFaOXh6bDRTbHZ2enJ3NFBYQzRTSnk4VmJsNnEyQmNhdEUxcVFLZVNqRTFBUG1YQ3RLSURwUzVBUVIzR2NRMzBMVktiaHBaazAxOU1tMVJ2TTFqRFlVa2VyUHpZbG1JbVQ3M1JuT3RGaUkrVDlCelBtUG9wTjJhV2FVdVBrWm1uMHZNdmtoSWpoVE1uMlhtWHJpZnplbnVER21USVRVWWpHaG1VMSszY3Y5RUcyK2cvMTIwSm1aWjFkNnZyL1ozSnpzMC9uWktiS1oyVWJVTTZQSUZFVjJoNS9qdjFXVUd0bzFQMHBndW9qcU4vajVKbGxhNUR0T1dxVEJ0YUhGZDN3ZFJXZHpXaFcwbUZEcXVoUEc2OHltQmw0OGVZbXpreE5lMjUzd3h2ZzZWM3JKWmIxQVRvT1dpT1JhcUZvUkFpbDBaTTAzQk1qeVdkc09nZWpqTkVxWVZDRWFLR1VnUmpPN0FMUUtIVDJkckNuanlQYXk4T1RaUzNwbjh5SS8vcVAvS2J1M082N2U3bmp3ZXViaUxXVjdQM0R2eld2MkQ2RmpRNkpEc3UzVktrVXFhbmFOQ3RwZ1dwY21waHlGR01UdGZablNOeURZZVNFUUVmTkJCS0tqUXBOUDRNUnZXc0JqSk1FanRKTjVNek1VVGV0UW5Fa2NNUXR6eE56OEJHZTY0TUxWTlFqK1BSTUQwa3d5SnhRdGs5L0g1Q3N0VEVBbnpnWW9IRUhIVFZvMFIzb1NDazByTG9pOHdiNnF0ZzVOZ3Jmekp0Q21NV1B4L1MrK0pzVi82OGJya3pwZS9DMFZWVnZUR1NWdHdscGh1b2YyMmVxTUZlYnJrSGJkOXY1MFAxb252M1A2ZjNWN3VKaGVsZGh4MkNwcHJEeHhlb2V6c3pXM2hsTytjWGlGZWloSUd1QzA0NkplV2hwVjMxT0x4WExVTmI0dXROOTh6R1pZTXBzc1RFRkVjM3lhUFpGMUhFZE8xNmRTeGJpNEQydnlVSGg0TmZMRStWTjBjcWJQMy9rQVAvNGoveGtQWHhPdVhndmNlNjJ3ZnhCNThNYVd5L3NqdTZ0S3FDc2tkVkFDcFJUVEFFRW5BZWZXc0FmYTNLZFFjNjZETzdrTkttMVNUUWhFNTNXTFh4bjZKbUgyQ2N6R0RrNlE3Vy8vTFdlODJHejc1cVNyT0RPcCt5aW1NWnBwWkpxdG1EOFRqVkdYL2tHY3pLM21jNWpwMXpTT1U1ZzlOYW03TURtYTdXN0lsUjRIREJjcVJuVU8zQzF6dVphSDQxT0wrK1lvNEtqa0l6L2twci9TSE8yYng0VGVUZmZqSnB3TGdHTUFZWTR2K2NrWWlxYk0vbGQxMnZORnFFeENVMlFPb0U0cFExV3BVaG1Ha2JEcVNFU0xlWldldmlaaTl6emRxaU1PaVZlM3I1TjFyK3ZWaWRRdWsydWhsRUszNmhqcmlEbzYyWlF1Ym5KT0lCYTFhUlNMdU05SFFDVXJVbG12TzhhNlY4MHFYVnhUaXRLSFV6Wm5KK3d2UlQvemllL2hPMy9mRC9QTzF3cFhiNjE0OEtwdy9YYkhPNjllY2ZIMmlKYkF5aE1TUzFHMEtsM1hJU0xrbkFraEx1akdRSVBvaXl2VkpEbFZUV1g2YTgwaE5wT255Wms2RVVUd0pEZWhFa0lnZUdxRndiOHQzOGNkYzFtYVUyNXVoZllhSkpmT1NkdTEySWVjNUtiMzQwUUNzN1pCY1FacFlFT2RIZkhKVDVwOWxrWWpob3JoNTgxTys3RWpib1FXbStab0ZNa05acHBnM3ZaOXMzR0JOSmgzK2RrRmRjUFJlOGZmdmZBbEZuK0hsamVESERHWlBDS3QyM1ZVZEdHSzB2eExYTnRLUTF5MXZXdlhyUllyQ1JGcUhna2hzWWtyNnFEa0IzdjZkYyt6NTAvVG5VYmlQdkxWNjY4RFZjY1lwZWlXRUtNeHlYUTFicEs0UUR0SzJRR1M1WGRNQmtDN2RCUDFWSlRDYnIvbGJIV3VVYUtNT3lYRm52MFcvYUh2K1lOOCswdmZ3ZVVid3NWcjhQQ055dXUvTTNEOU5wUmRZQjF1VVVwR2lpRmxJUVNrVDlUcTZROHAwZFJzY0ZKck9WRlUweXpHQUpIUU9OelJycWlSRUZ5VGFKa2tXblQ0MUhLdHFwbHROR2k1UmVhcm16M3V2d1IxdjhBM2lEbzU0Y25ObStRUWFwTjRVVnJncmJnbWJJVGcyc050N3VnT2MzQXpZcGxFeVFRN3o0d3pFZjZrUWN3Y1hLYWd6T2dVQzFqM1VTSzIxL3h2Lzl3aTEva1IvL3YzbEJ5N0lLQm1JaDlyczdEUVdDWlF6R2FKUjZ6U0lPZ3BjOERYWTBZalBWY3V4TVozSGlqRU5JQ2Foa3JWTkM1YXFhTUp6NzVia1liSThGQjUrczRkUWwrQnpHdjVUZTd0TG5Yb1ZPaVZvV1ppdEJoaEErd21EOWV4amhtQjA5Q3lKQllMSUc0RVZpMmxjbjV5VGgyRGxBeXBuaERMaWY3STkvODRMejM1RWVyRFU5NzY2alVQM3dpOC9kcU9oNjhMakd2R0xheTZRQ0FTbzFBMFUrcm9VUkZNdFZrS216dkFkblVUd1VTWjRFbHoxajBUdVM0SXh1STVOT3kvSlJKR0FYR2ZKOXFIM0lkeDV4b213azFSbk1FQ2tDZi93OUpUakxtQ0Fsb3NiZ0d1bWR3M29jVkxaTjU0VmFMYjU2TE5EMmtKbHpPUm1ROWRhS3M5bTIrR0FqVUdhbkdKbHQxck1aZm1TN2FOQ3hOSjhtN1Bhc21FUjNEek1pcTlJSGk5S1ZJZmM1aUFhZlpTdTYvbWdQdnZhaE1pc3c5VHBTNThzcnE0SHFYNWxMUG15OFlrQk1zWkV4UGZHaUNxV1ExYUtrcG4yZW9hNERBU2szQVMxbHcvR0xoejY1eHZPZjBXNGpaUnR0OGduSWcrakZ0Si9RcXRucmNIeG16QjE3VTJuOCtPMUNLUmJUUG1IS0VFRWlUbkFkV09sWnhReDQ1VnVhUGYrNmsveUxjKy9SME1EM3NlZktOdzcydktnN2NITHQ0dTZMaW1qcFcrNzgzK2paV2lidWVIT0VrUGNaeDJUaDVzaUpOTGRUWG51R3FadU53SWRoSi9pQVRRMFlnL21OTVBhaG1SVWtuQm5YZUtRNjR6ZmgvRnRpNjJZSmY3TWcyQ0ZaWndNUVREY3ozUWFXWmE4MVZubEd2MmZXWW1zWC9IMlZPZ3hWdHdiUWJ6ZVpOa3JtMDkvSjRtemVMM2NmTjhtZ1ZnQkh5RWJrM1gxODZibVNCS1F4S2JEeFhtc3g3cm54eWpVelhPM3pYNUZOZytpZitXM2tEYkxBUGJOUXJMUFYxa3FzdjhXNzYwaU5yOXhZYkN1VmxMOUhzUVMxUVZWVUxPNkZCWTlTdkNMbkZIZTc2bC95QWIyZkFiRDcvS3FFRzNjcEFhUnpMRnpkQjJoNEdnUWkwak1ScHNuYW90aUtZUXBIR1NhcVdxNVZJRk9zb1FFVmtSODRsK3gwZC9rRTkvK0FmWXY5UHo5dTljY2YrYmhhdTNxdVZtRFQwMUI1SWs1cndrOFhpR1JTSXNiNnBKd3puUU42V0hMNlh3bEtEb2hDWk04SFVBUW1pMUJVYTA2b1ZLTVFUN3ZwSkpya0tsU1hSSGdGcGNKUGpDaTFUYkNDLzhDVEpyS0VUOW5CbXlGV20veFFUWk5rYWVncFhPU2Mza2k4M2VkZ1pvT1dwR0VEclRzVlBkWFB2Q0loRG1NWnJKTjFubTRBbFZQTHZBVVNOMTN5M2MrUHhFa0Y2QUZ3alRYdFhteXdTSGlJUFRoV3RRSTFaTEFacDhFK0lrV0V4Q3QvY005TkRnNkpuNDM0MEpwSEhCWWczbVJWZ3dpdDFQOVBzMGs4aGNBd052TEwwK2FIV3RWdEJjNkZKQTk0cGt1SHQyVHVoZlpOOGZHSzVmSVNUUm9VZDJNa0pVSkFYMjQwZ3RoVTIvUWVob3pKcGNHZ3RVU3ZGczF4QkFLeVZiU3J6b2lySmY2YWMrOUYxOHo2ZCtoSWV2d2V0ZmZjamxHNVczWHJ2aStsNG1EeDExVkZacHhiaXZyRlk5SlE5dXZrU0VPRG5nemZGVXFpK3ZMZnUwSHI2aElJNlExYVljWFNnWjBkWnFtYWNpZ1ZhTUZTV1F4SExEYWpGZndZakEvWlRRb0ZQVFlsR2I4KzIrVFZEM1VWcmNwYVdzMklXMTJJMm9hOFBKQWFSWnQ4aUNtRnNzeG53aXUyOFRJam9GMFpaV3p0TGVYeFpiTlI5b21hdGxUbWNqL0VYT25rNFVlK1E3SFA4TmxxODJFN1k2VVFmeDMyNE1BcDdmQmN0RVdGR0YyaXdRQngyME1ZZ1IvaFFYOGZXYmlzbWtwU2cyRUduaFFFK1hxQTBXV1lBZi9rNzEzNUhxMW85YTRxZFdvdGo5MUZvNTdBYldKeXVFeU81cXgrbEcrSlpiTHpKZTcvbWRpMWVKNTBtMVg4bE85OVJnOUdQSm1uYWQxWVZ6YWhmdkdYLzJuNHFJQklKMjZFN281VXpmKy95Mzh3UGYrVWQ0NTV1WjdWdUo2N2NDVi9jcTIvc0ZIWHZLUVZqM0o5UXNkREZCVllJa1Y1TnppcmN3TDFMVXhTSk02QWl1a2syNnJUclBRb1pKYzdUcVFjc0laaEpBeldTS2FqY2F0WG82dTRFQUxXbytGVkc1bnhGYS9DWjRqbFpvVVh4QnFpRm1jZnA5WStBdzNZZm5oYmxrbTFHck9oSFFrWlpwbWlBWUNVeUZXNU5GS1JOTWFjODZDWmdsd1V4STFvU0szZkFwaElXdHZhUzB4WG5hUG1jdXQySnFzUWt5MUdGVDEwZ3RmS0d1UFd2QTAwNWtvUkRuMzlFZ0JyU0FKYWN5aGZBV2UyYlZpV0dCZm5IelhxWjdtbzJqRm1ZeG5peFlIcDRRNGl6NElrb25rUGNEcTlXS1RVeXdGYnAremZ1Nlo4bDU0SnZYYjFFRkhkSW8rMkZrdFVwV1NqV01TSmhqUXFsSmFFS1FHSU5WQTVaS2xNUXFySW5kaHZQNE5ILzBDMytXaTI4b2w2OExsNjhyOTE4djNIdDk1ejVKb0U4cnlxQ3NWMnNPMndOOW44ZzUrMFpIOTMvcXRFaHp0RDFNamwyTGNFOXJJcDZackI2VFFLYmFjNmtCeEp4Kzh5dUFGRnpqV08xMERLM214QjE4RHhwTzBXMHN1a3dJazQ4d09aUTZsOXJPR2I5TGhLYkZkQ3JTQ3Jla0JlbWFzd3BvUmFOT2hESmxQclFUR3V6cGhOMjB6cFEyNVhiNXBJMGZRNUJNV20yR1pBME1nRHA5NEJpZW5iTUNQUGpaR05QL25oaFl4Qm1LS2ViVEJKNEJIc0d6a00yWTBzWXpMaWltKzZ6SHVWTVQ3ZU14RmwwbVRqVWZxTExFNlphZmJmdWxxUFVIQUtMYko5UmlaY0VSdXRReERBTzZIMWl0RWltc3VYNjQ0L256MjdBeWpUTmUzMk56NjFTclhBdTVPUGhVU0NHNG5GRVNCRUl3ZVpLejNWeEthMlFVaGwzbVRqclZML3orUDh6KzdVQzkzSEQ5WnVIZTZ3T1hiMmRDWGxGekpFbUFNZEIzSzJyT2RMM1ZpUmp4dUtuZ051OEVCemN6bWdhak51ZFVMYmZIaWI4eFNYUW51UVVIbTIrRDFrbnFoNVpOcXdYQkcxbTQvMkh2TlVlOUViN1Nzc2RFUFZEcGdFS1E0TDhkcHMrRmhzakFaTDdaOTF0dHVTeVl3QnpQNHRlckhvU2UvWU1XQUcwQm1NY0ZEQnY4dWpTM2tJV1RyY3lwU3VJNnhTN09pRWhuNnBJUXArK2QrZ0c0Y0twVkcrem9kZThMVGRkb1ZCZW1ZTXNlY09xVjJSYVp3UVFXSm1KdFdtdDYrZmcrRGNkQ0ppYWJEQzVtN21ody9PS2VFRlNGMmdSbGF3eEJ0aldYYWpVeC9Rb2RNK1B1d0dxZE9LR25IeUpQclc3eHZzMkxYRjN1eU9zZG1qYnN5aFdGUW96Uk1rQmMxU2R6c04zR1Y4czBSVG9pSGFHdTlIT2YrRHpQM2Y0QWh6ZlhYTDZSdVhnemMvWG1TRDBFUzRlWE5hcEMxeWZHY2FUck9tTVNhUm03N1pZOVFpeXRNTWp0OW9YNTFTTFV3VGRkMUFPR2FvRy9LSUU0U1NaZm5EcGlEcUk0TTZvaldzWkVjd3AreTJPQ0tkMUVsY2hvak9aT2VVdWViSVZaa3k5RGl3aTBUVFNHVDZHNjNXN1B4c0E2bmRzMEJBdENNMEJqTmtsbitqbEdzYUs0M2I3SS9iTFAreU1JMUxMUURvQUdQNzg1eiszN2xwQ3duYTlTcVNMVVdDYkdhRjFoSkRRbXFwT21tcklIR2hSWUJabTY4ZGgreXVRRDJyckhTZUMxK0VxQTRPRkhxUTRQbTRGbnNWeGJ0OGJXemNlUk9zUEdrK21PV2krQUtRUGF6VkQ3UWFxWVpxNlVLY3F1S21nTzFPdkNTVHpsK1kxd3I3OGs3MTlEVTlXeFN6THFnWkFpT1k5VzhOVjhsSEVjaWFsanN6NW5IQXZqVHRta1U1NTU4ajE4N0ZzK3kvQlc1UDQzOTl6L1ptYTg3QmwzRUhJa1N1Y2JJN1FjTVZVMWM2Z1VRb3hIS1JIQmI2Q1pNcE85T3YxYm5XRE4rVWJEVkIzWU5FckFrQmoxODJPTVJCR3ZQVENpU2NFSXJOWkNTQkVyQzNiZklzckVDSkRwZ3FNdDdtK2dpb3BoNjFYdCt3MEpZdElvT3BsYWpVVEtKRW1ybUxGUXBVeWI1enR0NmVtZWx0T0lZa0s5RnBMV2dBQ2hpQk1XcmZsRDgzc2FwN2dwNm8wVDFHTXBzL251QXBDQWV1YTF3Ykp1b2pwUlRWQnY5WkpxSUhwakVIRWlOdFFyelA2a1dQMUliZDFjbW1sTHBGV0VObUZteTZTVGZsREYvQmZ4bks3YWhHYWxVbWZ6bExuMFdkdmFvN1Q4TC9YVnQvMFhiOW5rd2txcWxVa0xqRFhUZDRsQVlIODRrUHFJbEVBOHdJYU85MjJlNStMNmdzTnVJSGU5RGpwSXJ0VWdUVFdsa2tTVWxBSlZsZjErSUdqUFNYZUNEQnY5a2UvN28renZKL1JxeGU3ZXdNTTNSNjd2allTOFJwMklJZHBLbEV3WGhWWldTM0xmb3piYmUxYjN6UjRWc1RUMTVxK0FwYmlJbTFneFJLalY0TjZpMDhJSHp6VEdiV3NOZ2VpMlBMVlN0ZERGUU5ldktlT0JrZ2RpbDZqMXdKaEhVZ3dVRERxZS9ZeUdib0dFRm9ScTNVTVVrMHYyT2lpZEU5RTRRZHN6S2xYZGhCUVJ5cFNlb1pPdndaSFQraGhiQk1jQXBZSm55YllTM2VVUjNGeHMwcktxVExCemxHRFZlbFdSeVQ3elg2M3pmcFF5RWxYUUlFZ1NKSVhKUnlrVUwrb1VSSVVrU2d6SlJFeXAxSm9SNnBTU0pJQm1LOWNPSVJyQmVxa3h3VHE4WkIxTmdBWWo3TmJUUTVrRGozSlQremx2cWpZNDNYOUxGUEVTMzZCQWREaGJHc05BOWYzS05adHdqb0d4Rm9JRTZsalprSGhxZGM0SFQxN2c2dktDY2QzUmI5WnN5NDZRbXY4ckpDUWJQRkdGRkNKNUN6RnQ5TnMrOEdtZVBuc3ZiNzY2WS85RzRmS3RrWHdORElsQVFvSW5oUnloS2k0aHBEbGhPcUVSUzlXL2JBa1VteG5tU3pTeGs4N21taVVsMm1zdHVtMlI4V2c5dktxU2tqR3RWZ3NlbFhwQUVMck92aXl0Q2pFSnBWWWttTGsyVUtCazQvVUFJb1hXNEs2bDk1ZFNMSGpkTkU3RDdoMmQ2cU0xQUxUNFV6TjNGcEZtdi9jbDFEdG40REtaaXhNakxMU3BhUnl2cFpkV2I3TDBaZXFzc2QyWmJxWkNSS2pTQ3JaY1dDMGdlbkJmUjhOUmlud2pSbk9NSXlFa2FxNUloa3drTUVJVlVwZUlNWkcxTXRSc3B0V1lPVnR0aUdIRjljVkRWbjF2WnBZalpqZ3FtRFVqVlVneFFIWnQydGIzU0c0Y1p4YkU1Z1UxZFVnREsyUkM1R2p4TU9OTjYydUFheVNkMXh5RlZPeTc2N1h3NHQybitPYmxiZmJqUTNaZHBrdVpncld6aWloSnBWQkwwWlE2NldwaXM3cXRzbC96L1ovNVVSNjhsaGtmOXR4NzdjRHVnYkMvVlBxNFlSeVVyazhXQUYvQWppYngybVkyMHJlcmlrdlRRczJkampRWXR0bkhDNjNoUGtkUTBKb3BlRmtwbGt3cFZGYXJGWDIwc2s3Vmthb0RxejRTVXlBUEJVTGxvQ1AwRlQxSnZISDVCcUVybkp5dWVIRDVOdjFKTWpNckZMOTIwd1NwZzI3ZDAvZkpUTHRPNkxxZXJvdWtMdEoxMWpVa0JFUGRFQ1ZKSUFRUDBnWURHMExreVBSY0NvdEg0ZDZiaFZlUFQwV1pIZnY1S0s3Vld2K0grV3hqZ2xJc1c3Wm1uZjQ5anFNMTVxaUJtcFZ4SENuanlIakkxTU1Jb3lLbHdtaFNlSjA2K3RBVHE2Q2pNVVlNSy9NbVJFaFpXUFdSUXo0UXFucG1Cb3dsSXpHWUdhUkNqT1lmYWk2VU1aUENsTjAzRS9FVWpEUXpxejNQOVRsTnpUU3ZwT1hLMVptT25PNUNiVFFaam53c1ZaQ3hva1c1YzdwaHZOano0YnZ2NDUzTEw3RkxTVXZxWlY4S2pzS1lqMUpySmFiSWVDam9XUG1CVDMrZU5YZTV1aGc0UEZEMjl6UFg5d2ZXOFp4eGdGVzNvdVNDeE1XUDR6YzA0NWZUUDZPNGVUVmg2YzNiYUk3ZVlwRWEvTmpNTTZ5MUQ2SWt0MnVEdWdrazFrcG50NzNnL0hTTmRJSGQvaUhVa2RBVkpJMWtHVWduZ1cyOVpueGl6L284Y092bFV6N3k4cWVJYStYMDFobnJrNDZ6OHhOT1RpS2R1MTJlU0VEelB4dnpTemgrLzRoaWp5MGpwbURJMFFsdXFVL09ONy83TVdNUXgwZjczTHY5L3MxLzF4dlhVcG56bVJvdHRlQjR4aVI5QmtiUXF5MVg3MXp4NEsxM3VQL1dBeTd1M1dkL2ZRM2pGaG5YSk8xSm9WS2xaN2NiU2NCcGY4Sit1Mk8xMm9DWFBSaGoyajUzQXFsTGxESUwxeU1HYVljMEpsaWNKMHVCRXlZZnFVWHJKK3VtTlFRSkh0aWRhTk44cmx3enAyZHJkaGZYM0RyYk1CYmx4ZjVwOXZ0WE9jUkIrOVZLbEVKRlNIMWFNV3kzSWpFaU9YSm45UVNmKy9oM3MvMUdZZnRXNXZKdDJENG8xRU9rVHgwcWF1cTROb2RXZmIvVTdFRnQwdExNSlZQdlh0ZlJidHB2eE94Tk01V0NheGx4TGRJY2VLMkZsSUtiVTN2RHhudERzUW83OUZCWXI0UVN0dVM4UmRkYndqb3p4QzIxSDNqKy9VL3gvUHVmNGRZenB6ejk4aFBjZlU0b1hsUW5DY1ROcnBpWXBOVEVCTkhlUHlKVTk2TW5OT0p4eDJRL3lqSEJ0cjhiYW5TVFNkNk4wTi90Kzl0NU54bHArZG1KcGhhTU9UMEV4bmY1VEJaemxESUlaNXpYTTg3MU9WNHV3QUM2UFZDdksvZS9jbzlYZi8yYnZQSEthMnkzMTV6MlBYMUpESWRMMXJkV2JJYzlVaUJwSklWQTUrYWdGRVhIQWdzd1llNXkrY2pGUDNJbzBITEZXcVp4TThsYUFXQTdnbGpBZEtuZEZVZkZ0SkNId2tsWnNUNTB2Ty8wR2Q1NStBNURQNkNwTXNhSWlwSjIyMEZ1blQrQmJnUG5xenY2c1cvNWZlUXJZZmNnTTE1RjlnOUdEcGVWZFRyM2dPS0t3empReFVpaFpjM2FiazM0L0NJak5Yb1hRYUY0NVNCZXM5RE9kUVJLbVNGZVdsekIzcXZsZ0pJUnNhNGRwWnFUSGFLaTY0RzREbXp6QTRad3lmb0o0YzZMWjN6dzR4L2hXei8xQXVrVTloSE9ud0U2S0FMOUtWQVUxa3Q3a0puNEg4Y0VzbmpjZlAyUkhYeVg5eHR6eUdQK3JqZisvcjArTnp2cmNlL2ZQSlpNVWgveit2SmE2NExKYjJxY0FySmZFYStVcDU1K2thYys4eUxzWVAvYWZiN3lpNy9CcTEvK2hwbXI0NEZlT3RiYUVXdWk1c0tZc3plOHN3SStaaUNZS3VvOUNCWWE0K2JTeXJGV0NTemdhZmR4eEJGSjhScWlGcVF1N2pOR3F2VytqaDFYMjJ0V0p5c09Wd2Z1UEhYQzlmNktaOU10TG9kTHRza2d0U0pLNnJ0VHlCMWxFTGJiZ2UvK3pQZXhlNzJ5ZndqalpXQi9YWW1zRUEzMHZjVklXc3drcFdTTEprc3FhczY3cWNTSW1OUUdXZ002MGRtcExQNzVsZ1lSSFBLY2FqUzh3VUlWQ0JFSVZwV0haR3AzWU1kYlhIYzdYdnJ3MDN6Mis3K1BsNyt0Sjl3R3pxQkVpS2ZRblRBeFFHeEVsQmFFc0NSY3UxQW5ObVh1K254ODdwekV2RFFUSG1janpidXNZdUJFZTU2RmZKbDZwazFLeCtNTmN6SWlqMzhPNy9KNis5bGxCdkQ4b2d1Q01EUFZJaXY3RWVwMDVqaGlyZzF3Sm5EWDNpdVhBK3NYYnZIeGozNFhIOS9EK0kwdFAvcy8vVFQzZitjZTZ5RnhIamRzck5FV1JTTmQ2Q25xU2E4YUpsKzJCVW50RWd6TXFJdkxuM2gzaXB2VTZUMXVRTXRnOU9TZ3NsdVpTbTRtdnlYME1ReDdOdjBwNWJwd3VrcThlUG9rMzdoNmphN0NUcUNJa0NDeDJ3N2M3Wi9WRDd6d1VlSmhRNzZBZkJXNWZyQ243TERhWkJGM3JJcEJqcDZ5SGh5M205TVZmR1diZnhLa1hlSGtqMGpBa3hFaGhBNmtXaHQrOFVwQ3djMjF5bjYzNC9SOGhYU1I3WGhGbFIzeEZFYlpjWlhmNHYyZmVJcnYvcUh2NXRuM3JnbG5FTzRDcDhDWm0xTzlFNEhhb3BoWmdVV1FGdjdIc2lSMWd0VThlS2NMUjJXeW5DYU40MkxkKzFsTm0xazlPN2hCMlkwcEduRGhpSjVPRXBIalo0K05pSTBCOE1aczlUSFA3ZnpIUDAvTzcvSllNZ3h5WEpMU21Oay9NNEVORFkycGkwY0NUaFNxRXAvb0lVTjlxSVNEME4wNTRmYy85d2ZnelFPLyttOS9rVmQvN1d2c3g4aEo3Q2xacVZxOEcrZkt0SUsyT05WTnhYYkQvS3BsVHU5cHQ2UHpPb2h2ZGZPSERSOHFkbCtOYVVxMXY0dlNkUmJySzNsQWduQW1HMjdWUFUvR08xeU1oVU5SUmhsSVdvVStiUml2S3QvMStlOUZMeEtIZXlQN0MrVndvZWdBcXhCcHhteUlSaFI5WElFdXVyNHZOOFFoWXRTaTJhWWg0aVRxekdrdlNJZ0VWOGxCTERjck9MRWxVZW80OHZUZGN4NWN2azBNd3NBRnF5Y3I5L1ByZk90blgrYTd2dkNIZVBJRlNHZkFPWEFDcklCUUlWWFRHcmhXVUkvMlIyZ2pVcXdLb08zNjh2Q2FpUVc2SWhObzBXNXpscnd0TFZKZExUUkozd0t3MnNTOGYvNVk4YnlibzhPMDBVMGRpc1RwYjIzVTRzV3BjcE1YYnZ4dDkzcEQzTXFzU05vNWRTTFdtZkltNGdNTG1rYndoc0ltUExWWUxsZUtoTFhBQU95QVc4Q3RubzgrL3prKyt1QTcrZUpQL2hUZitPV3Y4TXo2TnNQVnlLMTRRdERBN21yUDdmTmJETmVaVmQ4VEZNYnF4QTFtVmJqQWFxRm1NQ3NqTGtqTy91RUM1Z2hoYkVyVGFHdHVlbEdKYXFpZ2hFSWdNQjVHK3REeGZQOHNieDhPN0E3WHVwWW9TUXVzNDVxei9pN1AzMzJKQjc4OVVyZVIzWU85dFVZbEVJTkJsaTNBRktKMzh5dGgvdEVKeVdtNjJtZFF1T2FnTWpWSk1PZkxSakZFc1hUMEdCUWRCNGhXODE1eUljVEs5bkJCZDBzNHhJY004Zzd4bHZMalAvSDlmUENUNTZTN0dIUDAvdWlBbENFVUx4UnBSQllhWFV4V1JuV2lFRyt1c0NSWFkrODVwak50QUpqbXVHR2RISmxSc3Z5N3p1TVdGb2lMNnV4STNDVGVPVDlyYVdpMDkyWWlzWS9KamI4ZmN5dzRZVm0xT05YMVNETmFGaCtaN3VzNDdRZXdkSHUxQ0x5WmpNM2ZiS2huWjVwN2hlM05xY0E3b092Q3AvL1Q3K2ZUWC84MC8reHYvSDFPZXVIdEIvYzVDK2VzYjU5dy8rSStkMC9PR2ZjSE0rZFRSSUpRY1FzbVlQVkdZcWFyWlNiZmNMYU9BcFo2L1BvVWlGd2UzbExKNDFHMVdnWHBKcXc1bDNOdXl5a1B4bXZHcEpvNk9zYnJxdC95L20rbGx4UEtZYyt3SytSOVJRY2hhTElFUWFDbEVMWUFvTHFEUGkrdkhEMldYUkhCRXhMVnpRNkZxaG1oMkdkTFpaVmFJbUpGazZLcHN0Y2RPVnh3V0wvRlozLzRRM3oyRDd5ZjliTndSZVg4eVFCclpsNUlVQ1ZSeGRzZ3RYWHl3ckVKc1Y4ME13alZVaHYwNkQ2T0dXRkpQSWpUaGVyaVJZOS8zTlJNVThCd0p0SXBQUWNieUdOVzRTSmVJblBjUkxXNEZpbnorMGR4RnFhc1pEQml2cWxKYnQ1SVkrU2xEelBkbDg3MUg0Z0pTYlI5WjVNRWZvYmFRdFFZTEUwK2VHZDliOGZVaFdETVVvRm5RVTRTKzNzSDFoL2M4SVgvNjUvbDlaLzVFbC82MTcvQTFUc1B1QnVWazFzZCs3eEh0WExyMWkyR1lTRFhZaGtHMmhJdks0UkE5Um9qdmJHZk05UE1GWlQyWnd0azNqVGpXa20zbWMwbEZ3Z0JVV0Vsa1kwa09vMTBOWkpXWWNOdU4vREpiLzhNMS9jSGRJaU0yd002S3RRd0JmNXNtYUpMMm9xb1JhNkRCMlRzOHVaRmJmbEtVMm10R21PSnA2SkUwU24xblZwSXNhTU9CMWFkc0IydU9idDd5c1h3a0hCMm9KNWM4TWYvc3kvdzNMY0YraGVCSHM3UGd0bklBVWdGRld0ZVlKcWlWU1RNcUp5eDdpd3I3VTQ4SUtJczZodWFqY0g4ekN5WXA3Zjk3M0RrNkN6MFVvdXVzMkNRaVZyOWRXMEVlL1B6VFFOR055TWFNclN3R2FjN2tlbGZxREkzUUZnK3YvdlJ6S3FXNmQzZys1WXEwcjU2WHJyNU5SR3Z6aEV2MVhZVHgyclByWkNLRTRGRGhxY1M2OU1WQUx2ZjJ2SGtwMS9tczArZTgvUC81RC93enV2WEREbHd4b3JUellxTDNRVVVyQXpYVTM0YUtHSFB6Z2kxRFM1YXBnUVpuUjB6eHFPbXRTVkEyM3FaQldtbVhpa0tPZEoza2JPNHBodUZsQ0pKeHFTM051ZTg4TlI3dVBwNllMaklqSmVWY1R2U3l4b0pQVlFQRG9ZT3lHaFZhK1FXQkRDR3NaNWduclRvcVJJbVkvTVVQS1N5SUZ6ZkNHL2FVUE5BaUJXTlFuY2lQTXl2Y3AzZTV1NkxIZi9GLytVSDBEc1FuOGZRbHQ1cEpnSjE5QVE3Mi9IV3ZENDJ5ZW0wSWw3aU8wbFFkUVpacmwvVDJFZUV3ZEY1TjU4ZmdWbW1JejcrNVpzU3YzM1h6YStaUlA2N2ZIMTdiMFpYSDJYd1I2MjMrV2NtQmRHQUdCWW1wUnpkdnYyakVhRnB3RlovSWlRRDBacXdjVFJOTlZQRTh1NWs3WW10cXdRNzJIeHdBL2ZXM0xsN3pnKy8vQkwvNEMvOURYWVBSalpWMmU4R1VvRytXeE9LQzFmRnRFaG9XbGFwcFM3UytZOXZjc3AyWHI2bTh6MVlHcEw0TTBneDBSckZ2ZE9pYkdyZ2llNk1rN0ZuekVyU1FYbjV1ZmRSRG9HeUUrbytNRzRMa2sybXBOaWNjTHZRQWxDek00NllXZUMvSHhYUWFBNnlsYjlaZHhNdFNNbTBxc0dnUXMzRnVySkVzTmFwQ2dsMjlZcTh2dVN3dXMrSFB2ME1QL2luUGtaNERuZ0tPTVVrVlNlTTQwQ0t3WkVsbDRhWWtxbStvNU5NYklSZUE5TjRCR2VVSTNOMlhzZjU3K1g3anlHODM1V1FmeS9IOHZQeW1IL2ZwTmpsK1V0bDFrNVNPQXBzdHMvZUZLcVQ2eFNtQ084UnlyODhUL3k3V3RjYno1U1lNb1BkbjU4MHJXR3ZWdGNSRWtwbHFJT1ZNS1JFaWdKRkxMdGJoVC95WC93Wi91VmYrYnU4OVkzNzNJNGJUdnNPeGdPZFJDdXJVS1prMG9BbnpnWnNHdGh5ZHVXUkNiYndYK1I0WStmZ1pKMm1GVmc5aktmODVFTEt3bG0zNFZZOFpUdG1VcWlKRDc3bmcrUnQ1WENKelNRNUtDbDB4Qkt0WGlCR3JFR2VwemNITjhsRTBWWm40Q2pNbEhDSDFaVlRpa256T2llbUZWV1ArQ294SmdxVjFVblB4WENmZlhvQUoxZDg3UHZmdzNmOXdROXc4akx3TkhBRzZuTTJ4Z3hkYnoyTHRSWWIxTGxJTTRsQXJaQkxvZStpcjFzOGhqYVg2UnJ0S1A1UTVyakJzTEM1MnRyL1hnaWFHNjhmdmFZTEozdjJDZjUvT2hxRkxsTmlCS1lZeWJ0cG12YlRMV1lrVEg3ZXhBSHROYzlTc0VZQzBSQzlORFBZOGw1RjJ6MWJMbGl1bytYcGhjNUdDVGJHM2tEUXhHRTRzQ0x5Zy8vSG4rQS8vSzJmNVA2WFhpVUZvUXVSc3NzRXNTeVFQbldVVXVsallCZ0hRdWRZL3lOT21URkNXTjZuTmwrczFmRFhPVVZLTWN0SVlmSWRNOFNoMGtYbFBLeDVaLytBbERUeDByTXYwN0dpSG5ia1hhRm1JUlNzY2xFY0UzTGJINFRvTS9LQ2lxZDUrNFhvSkd4UTFMclBlK2ZGRU1ReVRJczFsRWlyU0VvYnhyd245c0tENjdkWVBTWHM0cFp2LzU2WCtkUVgzcy9KK3pGTnNzcVVHTWdvV2lKZG92VzhJMFN6NDB2MkJRcFdKMkd3ZjRRRDVyaDQzaElqeGdUdGVjNmN0M01LRnJYUDFUK25ibThzTk5DVU1jMjhVWXQ1anZiOExvVGZtS1E5MnVjZVp6SXRuK2Qrby9OekVLaDVkZzZPbnNQdnpuelRlMlhCRkFHNk5ETkxZNUJPTFRtcnc5NUxhZ3NjMVY2VG05L0psQzJReE5JaFFvZ3V3WVJhSVNUZ0RGWnBSWGs3RTFmd25YL3NCL21WOEZPOCtVdXZVR3ZoTksyUmt0RmE2S1VuNTRGVjJqRG1nYjZ6L3NFeExGWHFNaGJXRnJXOWZ0TmNhTXRlM0Exb2dJenpmNFcrQk82c1Q5alVSRXFTZU83cEZ4aGZLZWdBaCt1QnBJRVFvNldhTk9mQzYwOVV6UE9JMnVxNzNiNTErMGExOVF6Mm1lelJWVnUyR29Bb1VGMmE3dmM3UWxjWWRZdWNEVnpLZlo3N3RnMmYvNG4zazU0Q2J2djN4a1R4UXB3K09rM25UTmVscVlkMGpGQVBhZzN5Qm9VcUZuQWNnVDF3aldIN2UzOGM4T1EvME9zZFYxZGJyaTR1MlY1Y2N2M2drc1BWbGp5TzFFT2g1b0lPRlMwRkxWam1heW1VTXFLeGVQYXlIdG5GVThyOXU5SnBuRklyL0FQVVJYSFRWT1hKNDNuSDNqZVJ2OERNNXVkV21TZU9TcW5ISFlnUUE1MFQ3cXFMRUFMOWVzWHE5SVMwNmdsOW9EL1pzRHBmOGNSelQzRnk5eHh1clN6bEp5cXNBNXhBTFlWd0h1RWt1TVpoYnFqVHNsTVVhaFZpTWt4d0tKbDFsK2J6Rk9MZFpIc3lSRDcrRTMrQWYvL2c3M0w1bTIvVHA0NDRDRUVEMi8wMXArc051OTJPMDlOVHkxSU9ZYXJBYkF6UUpnemNGQklHT0pUak1JQUxsYW1yanhienR3UkNGdm9DZDFabjlLT1FicC9lUlhLQVVTbUg0dEsyRURYWlRXQUZOOGpjMTZrMWtrTVh4Zi9WWWlOYTNVRlN6TzhvbVppU0phQlJrQkJNdzRpU29pSXI1U3Bmd2RtV2NMN2x4LzkzMzhkNHFuVFBDTWlJckRvbkFJTkZSMFpyY3hvZEhoVmJjQm1VVU8wK1VERkcySU5lWThOb0RzQUYzUHZtUFY3OXJXL3c1aXV2c2IvWWN2SFdBNEkzVGVoakloRWhWNlJVUWxHNmxNRC8zZWFxVzc1UVFJa1dENUl3MTROTWpESnYwSEVmcmNaTWdVY0NnSXROL1Y4ODNOY3FtUFo0OURNTlBsNjBGNXFja0FYeDVKRXFsWVBzMk1zRGFoQTBCV3BRU3FpTW9WS2lVbGVCazd1bjNIbjJDWjU3end1OCtMNlhpRStkd3E2Z2NZczhmVExubkNWbWs3YUR2b3Zzc3pXb1MxMHlwWnhIVXVnc2VIbm1RdUN1elh2ODdqLytCZjcxWC91SHZQbjFCendkempsZHJTaEZxY0UwV2Ric2pyMXlqSHExWEM4NDlsSHFEZWQrS1ZMVVM2TDlIQzJFWWpIQVdKUytSazdwU1UvZmZwWjZxT1I5b1I0cVlSQ2tRQmRhMHBySEg0SUZ6eHFtalplQXRxbzc4ZUJQOUZSbnMwb3NPQlJFdlhVbGtDbzVaNFphMFZRSU1qS3NMNUdUSy83UC83Y2ZwWnhEZks5WWZJUm8yYVVWSk5xVkZDMm9DREZaZ0toVXF6WmtkUFBxU2t3NlBWUVlCWGtJWC8yVjMrTFh2L2dmZWZqNkE3b2M2R3FFVVFsVnVDdTNiR0l3SnFVN3JPb3lJR1p0bElqV2JFM0NxOG52Q2FaMGRJL0ZzcmNhY3BIK2lHeVhTTXh5MDZieHpZOGxkcmlKM3JUdmFveHc1TXN1WHArUGNNU3NUZk9wRjY5MGNZMXFvUlJ2YUNlQ2xFRFd6SWhBaUl4YWtUNmdWd01QWHZrRzkzL2hHL3hhL0RucXV2S2g3L3dJNy92a0IrbXVFOXp0NGFyQzdRQTkxSDBoajVYK3JMTVNiSW1URG95ZGpUMHZBaWxFNU56V2dHR0VGMi94QTMvdVQvSXYvdXUveWRWYkEyV2ZPZXZYWEIrMlVBcVZSTjkxU0MwT1R4K3YwVkYrbTV1NDl0ZkN1Vi9XdVhnZGxkYUsxSWlFYWtIT1hFa0ZOaVRTODArOWlPNkZ2RFBUcXc2RnBORjY5U3JtRElRNGZYZExtemVIMTVzQWVKYXZCRnNNNDVrNVIwcUtnZ1JMYXhkSW5ZOXMyMFF1NUQ2NnZ1QlAvKzkvaEtzS1p5OEJzWmp0blFBNlgxeVA5SHRMR3U4MVFGRDNRN2JBQlhBSnZEVlFIeWhmL0RkZjVDdS85SnR3Q0lUUmV0RjJKYUNqZGFIZnBKVVBhalVUSlFVZkNWY0xvWmJKZkZTMUhEUkQvdUtVQlkySXRVMWlybFlYOWQ2K2Vzd2M3OFlvaUV3ekRjMWtjbmVrcXIvTzR0bHI1TDIrWThsazdic01KWitaeGZZckx2Njk2REt2Z1NEVlRDTkFZZ2RBR1NzOUhaSWk0emlZQ1ZjaXcyRmtxSUhRSlNxVjhWcjVyWC94cS96aXYvNTU3cjd2S1Q3M2hlL2gvQVBQTWJ4OVFmL2NMY0t0U0I4aXc3NGFFTk5iUnh0RExJVmNLdjFxdytFd0VuTWduVWNvRm9KZ2hCLzZzMytFZi9xWC93N0Q5VFZuNTZmSVByUGFiTkFoVTJzR3pSaXlzT1FTWXdoMTUvd1JCbWwvaTBITmlLWGIxNm5SdXhBMVRsOGJ4c3BwdHlLOTlOUjdpYVdqYmtmQ0tMQlh1cG9zSGxKOUk3VWxkSWh4Wi9YbXpQaHNSN1VTV2lsMWF2VUo0czBaT3V2MkdPeVhOWXdrRVlhNjVYcjdnUDJ0ZTN6Zkgvb1VUN3dQMGd1WWM3aFJjNmhUTk5NdFdxUmRDbWI2Vkt4Qm1VWkNoYnFGOGpaME8zajdWOS9oUC95VG4rWEJiOThuN1FOOTNwQTAwVXUwK3lyV1k2c2owTlZFbnhMVVFpMEZMV1kwUjRFdVJtS0lpRHF5MTV3aFQ3OXBnaUI0d1lwRm1wb1FtVTJkS1hOaENYUTVPbGpGaU56OHpsYkVKaTJ6eXpiS3Y2TUpDODlCOWQwNEpvQ0taMW92K1hCQktDS3ViOFJCRjJuZEdadVdkUE9qdERhcnlxYlZIdVdBREVJdmlSZzZ4cExaYlVkaW56Zzd1OFBodHcvOHd6Ly90M255UFUvemhUL3hJekJzNGVrVjNJbjBKNlpoeWxpUkZMeDhXdWxYa1Z4SHVsVWlSREYvOGd5bzBiejlLanoxMFplNUNLL3gybXR2Y3BzVlJVYldZdWFiVFA1ekUwWXRQMitCN3g4cDZkbEU4NmFQWnZzc0JGZlE2clR0VmJsNTVDUjFwS2Z2UElmc0kreEhHQ3BsS0Q3NjJxTHVxaURCR0VVbldDM1ltREFCRlNYVzZoeGVyRW1CbXFTWGFpaFg2anBDRWc3ak5VUGRFZGFWMkN2ckZheWZUM3piNTU0bVBlbUwxRmNLbWRnbGNzbWswTTJTc2xZajNncVNFeklLZWduaEdzSjkrS1YvODJ2ODhyLzRSZXE5eWkyOVJYZElyR0xuekdIYXI0K0pQaGxxVjNOaEhBK2tBRjJJcE9nTk1iUkFVVFNQYnZONnJEL1F5QmRDUTluaW91RElSUC92VnY0N042aWJ1Z3E0K1REakx0cmVXMmdPbS9LaXh4cUdPSFZ3dEUwRzFXWGUybXgyV1hyTEl1MEQ4emV6S2pHWUpyRnJDNnhXcmxsS1FVdEZ2S2Z6eWhzWmtvVllJMUhYeExEaTR0NFZxNVh3dnZQbnVIeGx5Ly80Ri80MkgvdUIzOGY3dis4VHdJYXBKMmtEaDZwZ1RhR2hTakhLQ2twWUJYUUF1V1dTWWJ6SWZPb1BmNTUvOEpYL2psdTNWdlI1eFpxTzRXTEhxdXVuQXEzV1ZsWnBHbVNCUUI1WlpmVmQvdDFXeTNOTWZVSjBkTTErMnExSUorbU03VDZqbzJVS2g2TEVhbmsrUWpSSFJ3WElrNU1lNmp4OTExUldzZDY0YXEzNFcxZDhzN1VDbXBXeFpHSUtuSitkTUxCbGUzakEyRC9rai83RTk3TitDc3YrM1FCOVlKOExxN0JHUTZJb1lOYWZjM2lCSEpDZHdDWElBK0ErL0pYL3gxOGxYblhFQngxbjVZUVR6cEJjNk9pODRYY21VS3lHV3EwNVg1UkE2SHBhZTUycG1UYkIwc3AxTGw5dWtUVnZSbVNSYVdrT3RTN09jWEU5a2VTeHoyRGRZNHpoVEhOWU5saVQ5bE9zc0FrcG5mSCsxdnhQYURwczFqd3o5UytuY3JyRGE3QWtyVmZCNG1LOHpMcjVMM1V5MjlRelI3c1EwZGJsUHdTYmJWTXFmWXkyZnFYalBKd3hqSm55b0hJbnJ0bnZoUy8vNUMveW03LzA2L3lCLzlOUHdBdFdFQlR2V0dGU1ZnakY2NVJFeUl4MDN0MmxoR0xDNXh6UzNUV0k4UGsvK3FQODNOLzhKMXh2cndoajU5ZFVUZE5KbXpCd0ErWHkrenRpaWtWU1pCdXFhbjYzbTEzQnJDVmM0QURXMjNxMUpxV1MwTjJCVkNLU002dG9UUUdTdEFEL1ltU0FndVVuK1hCTnNTSXFtN0hvSGV5eERFenJLaUlJQ1ExS3BKS2xrdXVCcS9FQi9lM0NCejc3WHA3K1VHZkZQMnRnQlZmYmE5WW5tMm16UmF3MVRveldxSy91TTNLSThBQjRHM2hEK2V0Ly9xK3lHbGFVKzVVN3E3dDBwVU4zc09rMkpnMDk4aDk5MmxhaXpYK3ZacTZLK2oxNGoyRzNYOFgvYmRyWndWbHR2TkFhdmFuM3ptWEtlNW82Rms3YXczdzFLMGR0V3NienViVE1lV0F5UDZuWGFMU01nK1B3aVoxaGY4ZGpScG0rZVJHQjFjRE5ZM0x1WmJZVVduZkVjYXptcDRWQTE2MG9aV1E4RE5aUkVtdWlJZEhRd2VGd1lOMzNyRWlNZFNTR25qQVUxdkdVNjdjUC9NUC84bS95dy8vNUgyYmRQMnVROEFta1BwclFqcTF2Vi9EcmdOQkZRbkswOGl5ZzF3Zk8zL3NrejN6clM3ejk4MS9sRkdDb0ZxTVQwNHF6ZlhWVGs5eGtrS1YyZDR0QWhleU92VGgzV1NheEM2dFNDRVZJZlJIa0FMb3RkRmtvcXRiYnFSVDYxdEZ2WVdBckFxS0VhRU4zVWxDS1ZncldsVVFBaXZzMEZldmpoRUlZVUJuSURNaHA0WDU2alQvMTQ5OUp1UXZ4ampIS01CWTI2OVBwbmtMQXJxT0hpL3R2Y2Q3ZkpwVGVuUFkzNGY0dlArQWYvL1YvUkg4NEpWK1BQSFh5Rk9VcWsySkh0NHBRUis4a24xMWpGS0pXa2dRNm1RVkFHMWphb285V2tJWVQrQ0tNdjBCWHRHa2JxY1RRNXE1TTcwNzVVL01HQldOWVBIdlk3YVhXU00rS2l1YnZuaXNyM2QrWmlOM013emtGL2dZcU5wMjNkRlFlUmM0YUxOckZ3SkFMTVFSdlRXb0VzMXFkTUF3RDQyQ1RkR05jbUdPWVgxcXdjZ1lSaFhHZ3E1aytkVlFOakJtR0FjNFUvdkZmK050OC92L3c0OXo1K0V0d044SUdaQU1NSUgwa3FaRFZXK0tLeDFBamtFQk9BK1ZFK2Nqdi96VC82RXRmWmtOUFQ2RHNEdFlPeVFsYkhUeXhqalRHS0NFMmMvU21NMjlXUVBCdS9sSE1XNm5WQTVXaVU3KzBVQ0ZVSWNnQVlRVEpwZzZqUWtKSkFsSHExQlNpVTdYWE1lY3lpbzlGVUV0YWk5aXNreFFnUmJGR0FpbFkzNjR3SWlHeldvT0dBN1hmOGI2UFAwLy9sREdKeGdJSnVsV2d6VmFQVGs5UkFvZXJMYmRPN3FMWDFkQ3RlOFlvLytDdi9JK3M5eWZVaThyZDdnbksxY2hKWENQWnh6bVViUEVha1duZVNTZG1ZZ1h3RE9aaWpieFpaRFJMUlNZR01XYVk1M2MwZzhvSHM3Sm9yd1RUdnkwenBQb0RiODNVTXEvOWJ5K05ubG5BSFhyL1htak55YUVoT0VIbTh0ZWpXTUgwSFk5NzFLbjEwektIUnhUeU1OSzFWa3NpSkVsMG9mUEp5MURWd0JTSkVKSWdmWUJPQ0gxZ2xFS1ZFVUtoMWt3SU1PejNyTHBJek1KNVdQRmtPV0g5UVBtZi9wdS95L1dYMzRRM2QxT01pMlpXZSs4M1hkeVZBU3NaVmhCdjljUm5iL0dlajMySWF3YXU2MERhOU5RNk9wTzA0RzdiSjN0dXpkb25WRllYOXg1YzJ4L2xOQzNYdFBwRUJKQXFCSzBPeGFxaVRsaXQxNWIxM0cwRTFBYmlWR2NRSnlZZnIyQU10ZGdJTjh0Q3JJWkxrOUV3c3MwWDBBLzgvaTk4SjdVRFRrRFdrVXFocUNWYlZzM2tJYU8xUWhGVzZSeTJRdGozOEFEeVBmaHJmK0Z2c0tvYjZxQ3N1NTUxMTNPeVdrT3BFek5zMXYzRTBDa3crU1VwaUkrRGNDZTM1U3lnVStyVDhlTE5SMk9BYVpTRlJId0FIOVlhMEI1UmhWaXRWM0xTU05CQVVEc3Z0aUVTWXExZnJGYkc1cnlJdngvVVgvUG5zSHhkYlNKVVF4NlhSL1hnMlhMNzYrTDFtMGNYZTZJazZpalVVYkIrd3ViL3JOYWRkVnVVeko2QnNhdU1zWEJWdHV4MVJKT3lQdWtwa3BGZXpDRmY5MlFQVWt1cHBLRndQZ2hQREQzLzlMLzlPK2czTHVIMTdYUmhXdVlNaGdZK3hXaWFobFV5aWR3RldBa2YrOXduR2JwQzZlQlFCNnluc0NPdWFqbUZ5MlordGM0WkU4ZVpFNjUxSHJPL01JTXUwL2xWQ2VJRGVGcXYyRFk4SjB6TWtBblYwbEtpcDZaWUhvZkZHcUlISUFPWjRGM0VqWHRIZTFDSVVSbkdhNGE2Sloxa1h2amdFOXg1RHNJZGZESlB0ZVM1TWhvbnU0T1dRcUFlaXNHR1Y1RjZUOUUzNFcvOHYvOFc1WUVTeDBnaXNrNXJ0aGVYbHB1V1IxWjlJb2hTODBBblFneUJwS1pOakVrY2JBQnZ2dGNlc3lSNnZDYVpKZGJVQVZJdEJtTFN5UWpiSHU0OEx4NkJaYlpoYzBoYUcwQW1zOGxHUURUR1lQb05veVpocm1QaGlEQnVIdTlhQWJBNGdtS1pCeUpUdEx0bzVsQUc5aHdvRzJXL0xseXRCdTUzV3k1UEJ2Ym5sZXZOd0g0MThwQkxkbUhQb2N1VVh0aVdnZjA0MEhlUlRlbzVQTnp5OHAxbk9kOUZ6cThDLy9pLy9Udnd6bUR4cnNIdWszYVBia21xVmtjd0t0eEtjQkloS2ZINXU1dy85d1Q3T0xMWFBTVlVrRHp2Ri9QRXRHTnorWWFXb1J5dGdjWExITXh4S3lDQTBiaXZiMEtOODRNSDJTcGVaWWdYM1l0SFV0MzVtVG8rbXF0aTQ4QUNURU5nakVLbXNReGFSN1FXVG05dHlHZGJyc09CNy9xaDcyUHNvVHZEcWhKcnNZNHVRYjNlSU5DbERoMnhPTVVXNjFpNGovejhQLzBseGpkR25naDNDSHRobFRvMklURkdpQlZpbjB5anBFZ3BiYnkwRjVsSjB5ck5FVFlHYUJPeTVtckJPcitudVBhWWJkM211N1R1KzBhVnJzb1hUUTFtaXJYUzFhbFJHN1N0d0txYkhqMld5SlY5UjF0M04vMG1McGk3Uk1JeGN6eTJUbjdlUHZ0MEdWQVZVdWdwS0NVVU5DZzFWTVp1NUg2K29wd0d3dTBWNGJUbnlhZWY0dno4bkhmZXVzZWJyN3hPMm8ya0RQRVFPWXNidXBBZ0MyVVk2YnZFRTJlMzJONjdaQjF0a3VaaHVPWWIvLzVYZWVuMnAyRzFnUTNVc2FEUkJGcjFRSzhFUWFVWXphOGozRm5CZnNkSHYvZlQvUFQvNXgreFdhOFk5M2xLS2JJNU1yNC9mbmZpL1k2bjFXNzl2dFJ0UGwvT213S2xKZmcyTUNhSWtoaVZtaTBObmxKZDdWb2RpVGFudENGZk1vT2ViUXVEQVZ5bUJ0dkFWQUhVT0gzVkIzWjFqMGptL3NWYmJHL2Q1L1p6Z3R4aDZvUWlNYnJ6V2ljRzFKd3ArMHFxMXQxRFg0ZkxyMTd4cS8vMlM1em5FL29TYldMWFBsT2ljdEt0Q05YdXBUQlNhdlo0U1VQb21wL1E2aDh4dkR6T0dxVlZ6aTNicXk3OWdPTlVDWS8rK2hyNVA2MmVXOXU0aDNhbWIwNW9NS3lmN052YU9yUE1YeStQTkhFN2dyWTAyS3hHa1lsaFJJN1BzOFlNNFppL0ZqMk4yeHN4Q2ptYlpNNGxJK3RJam9YcnNPZTZPeEJmT09YbGo3K2Y5My9xdzNEVzJ3OTFLMTdlalpBRHZQMkEzL2o1TC9HVm4vc05LckFpY0NxSlZaOG93MGlLQ2RIQUp2YXNrbEJMNVl2Ly9HZDU3aVB2SmQxNUVVN0VtbjI0OEJMRmc5TUswU1l0ZFAwS05nRk9FcmMvOFNIR3YxYzVIRWE2R0tqRkVVUVBCRGVCOTdnY3NHUGdRNDlUWGFhZGNwZWlKZjE2R1hiU1lrNTgwQURWZTZ5S0VrT2xWQ2JmUTRyOVNEcEt5OEF2VWlsU2JMb3F2Z2xpenFxV3pHb1YyZW9PdXN6bnZ1OVRGaTlaMlVkTHlZZ20xSUdnVGlLb1VuTW1Tckw4clV1UUEvenJ2L2N2Nks1aG5RUHNSL3B1N1FOWlRaTEhFT2k2am5XZkdQWVpheHpoS2xXYU16MlI3ckZhQnBjNGp2QkptMnVvUitjZlpmekNrZDAvZVF2Q0RlVSs3WTEvcHAxWm1lQmZscnl3QkhqcmpjOHlvV1QyNG56dWtXNFNPSnF5NVlUU3Bvbk4xNCtOcFk2WVE5NUhIdVlyOWh2bDVQMVA4ZG1mK0VGNG9vTW5WN0FTcHBHTGwxYTN3Yk5QOCtIbnY0Y1BmOTkzODAvKzh0OUVycFIweUxBdm5LWVZZeW4wcXhVSGhjc0hWNXllcnpqa3pCZi8rYi9qTzU3OFFlVFcwOFJ6aTYvVXJJdkpaNVZLb1R0Ynd3aDFxNFFUZ1YzaG1mZS95Tld2ZlozVHRMR01ibFdpSkJkUzFoL2hkejFraVZMT1doN3haaFArdDZXQUdVMEVTOGljTzhhYlk5VDhoTmJkdlU0T3IvaS9nN2dFbmpSSkptaTJJVDdSbWd0WWsrcEMxWUdzTzJLdnZQakI1NUJUTE1BWWxiaXladDhxbG0rbG5xSWRZMjlaelR2SWw4cTlyOTdqM3RmZjVuWTZZNldKVzZzVHdtaitVd3BpZ2FLUzZhSnkyRi9SZDJMOXc5QnBKcU1Od2ZUY2JuRlVaZUdMcURhN2RtSHZOalBzc1FWQ3ZqNWkzMU5ETnFmNXhqTTNIb0hzaUpldnRUK1dFbkIyTm5XNnBwa3BaemU5SVR0VERsN3JYdS9DYXI3MkphWTBIeFZGbzJtZ3ZZN3NPSEFaRHR6NmxtZjU3Si80WWZqZ09YeGdEYzhLM0FXOTdYdjNQUEFpOEl6QWU5YndUTWVQL0xrL2lkN3B1Q2pYeEUwazY4SDhuWEVrSUp5dHp1aHFSOXpEMjEvOUp2bWRLM2l3bmNvZDhzRlRqajNMV1ZKa0dFZEtnWEFTb0k4UURuemsweDhsbm5TTW1xMElrSVhwZjdRK0xKNTFZb1pIK01hQnFmWnNNVFF6dDZ0bTgrRURZY3JxVEE3VHBSaG8wNm9hdGgwOEJXUkd4WXpvWXJDdUtZRThvMXZ1TUpselZGRE41RHB3ZW5mTjB5K2ZtMFpwWWVnS3FUTndveFFNWksxQ3ZqNUFGY2FyUXIwYStlSlAvUWY2TVZLdUJtUTBmOHJnWjVCYVNNSGlHY3BJMXdXVVRCVHJGeGJFSnNYaVJOMUdPcGdKVXJCd3N5K1NJeW0yWDQ2T0tkWXZ0eGFXanFOTmUyc3BIbGFYZ3Rnd25JbzFLOUNKd0Jza2E0K28xUUVUUXgxdDJsY2JqVGR2NW9SaWlWV0lXak0zRzNGZDYveTlOeDNXNVc5TkNLVXpka014c2QyaUNBeFU0a25IQXc3Y2Z2K3pmUElQL3dDOC85U1lvZ2RXc0plUnE4T09QQ0VKV0xQQnUxQnVLengzd3ZmODhSOW1QQmV1Nmc1TlVEUURwb21qUXFmQ1NnUHBvUHpTdi9zNXVCamh1cUpEcFY5M1FLQU1nMzE5N0pBWXFGSWdndllDblhEM1BjK1RJMnpISFRVcVdUTlZNOVY5VVczNWlOeE1yejhXRVRmWGVtbUtxZmNyczRHNmhlQ1RZb3dRRkplOGJTYTZFYnFJRWlLT1BjOHc4RFN3Vkt2Vk1FdEREVExvaVBVTHRqRUswbFdlZnVrSmFtU3FsTXNsZTJLZ1dRTVRVRkNFcEFFR1MzTi8rTnBEM243bEhUYmFjMnQxd2pwMjFESTY2S0JIWTZxdGFLd3hTWjIwaWllSVlUSjBSS2NTUjUybC9YUlA2dWMxTFRNVDRkR2psam1lSXZNY3lvQk1uU1I5Qk9naTMwcU5NWjFCbzhjd3dJclJjczVURE9OSTZoM2xiTjFRY0hMTUxBS1BKWkJIRytCNWtDMEdSakk3UGFDbmtmZDk5c1B3NGdhZUFHN0JRR0VZRHF6WGliUHpEVU05TUhxTW82NlVFaXZ4K1k1OXR5TStmNXNuM3Y4Y1l5b2N5Z2hobnNSVzh3aTVzQ0VTdHBsdi92clgwSWQ3MkptNVZIZFdiUm43OVlRQXhoanAxcEZjS3JLT2NOSkRuM2p5K1dmcFR0WmVhcHpKUjFDd0NXcHJKSS92NDhLRWR1ZCtnb0FYc1JkN1QyZExxb0VuMUlwazh5VkV5Nnd4bkFCRE04SDhFY1c2NnFWUVRZcjczNUZDMUV6Q0pvY0xCZFFDalZVR2N0anp2bTk5aWRvVmsxQUN1YnFkNWVEUjFOSXFGOURPZ2xKWGhXLzh4dThnKzhvbXJ0RFI4N0dDVmVkRnZ6NEpGZnhhSkZqc1J0cjEwdnlWNGlaWFFWdXdURnp2azBFS2dma2hPanBUelpEanNha0VvVnB1WEt6aXNSTnpzQnN4UitlWWh1bUhFRWdwV1JxSW1DTmRzMDduaGpBelRtdklKbFhRWE5GY2JibXFOVVR2UW5TVFRCZHhrdGtoYlZxa0JkVm1NNjFNUkJKRnZLNWpaRWlaY2d1ZSt2aDdyVS9CQmc2bDBKMUcrazJ5OVNXelhpZTZVNnRTREdzaGJJSlZMajU3Q25jNlB2T0Y3MkdJaFJLcWFRTW53aWlnSmJNT2lkdHBqVnpzZWVWTFg0VXJDd0hVck9oZ1VyTVVBNWlhMENpbFRLRUVZdVgyYzA5eXNNSXBtKzZXOENwTG5kWTJPTjdjSU41SFl5b0xFOWFGWmpPQm0xWXluVkFJTnZMTkhaWVdWQXpWQ0dYUmJTRlFKdk9sdldjZDZ1czBYRFNCbTJLTm9TcXhoNUU5SmU1NDdyMTNTYmVpNVhWRlNLdmVWS3FveDJhd09YeTVXdHY1WFlFZGZPVVhmNTI0cjlSZHB1WUJxazM4SFE3YnlheUtFMFBYS1RqYWZJQVdDN0dPTWM0Y1lnWHlPb1hITWt2Tm9aVEZRaTY2VG9qN04yS201ZVRYMkZaNHBLUTUvYk5MWDhVbWJZeGFHR3BtME15b3hTcjhnbElwWkxHMGtLeVpRcUZLblo2SlFvem05eUdWV2taeUdTallyRWt6ODQ3akJvK3RmMWxLVXF4b1NtdW1ob0wwd3ViSkU3aWRZR09sSVlNT1ZFYmJuNUtweGJXeFZJcVA3YXRVdWhPdnRUK0xjTFlpckV4TDFTQm9zSFVLQ0ZJcTlUQ3lMb0Z6N2ZucUYzOE50aGwyU2xvblM1SEpPdlgwU3A1bDNQZkpRWklDcThoVEx6L0xFQTE0VTZrVUxXYnkxbUphdkZibWVvTmpqUUhIUXFYNWdaTlp2WWlqTlkyU2JQeTBUbExTL0pFQVhsODhnNWpWbnlQTnZyUER5bUFicXFKaXJWODBXREdNVFFMY0ladU0zTUpzMmxiOE45bHg3WUpieHhTdmhSOEV0b1g5TzFjOG9XZXNZMkRkclFpNWttSXIwMjVCVGdkRFc0QlEyczB6U2YrcEVHdTY5dU1hNnFrcFdGczR5aFNZbkFodUFUdFc4VjVUcUFmT3FxMkhtRlNNb3VReWVoS2hOZTB1VHFSVkRBWXR4VTB0dFVSREE5M01rUlNYOWliZGpOQmE0REY0UnNGQW5kTHg3UjU5VCt5Q0YrWlc5R3ZtS0dZa0paTVNISXBsVHJ6d25tZThMVzJDQUNmbks2YTZYaFZDc3RxZ2ZUbVFZbUlZRDZ6aWlqcFVwQXRRQmdpRjFma3B1cjFtUEZnMmNCNEdWakhSZFJGcWhneHJpZHg3OVI3Y3U0STdDV1JGbTJYdmdTdHlVWk5yNnFHTFBzQ3RFKzU4OFAzRTA1NWh2MlBWNG1NdzdXMXJQOVJRVHBYR01FendlQ3RYZU56UjhzZmFaSVZrUG9yVkJFVEJvdkRVU1RvZFdiWFZVNXJWcEthLzZMVGpIS3ZlTDFhTTAvZnNJUlZ1UDMxcUcrQmRQZ2FGV2d1cjVGVVpyU0RESTgvV05TWHd5bi84VFU1a1JhZGlBZEc2UjhkQ3FZRVVyV0pPRUUrMXR0d3QwZGFkY3E1d0N5Mk9RSmxRdmZKSXZVSmpobW9RYkhPZXBFNmxzNVBna0NPZ0djZVBzTTVuQVh3cWNFeUJTcWI0YldrVWlqdmlXU3ZkcXZORXcwSWJpR05PcFJGMjZqcEQ5S3JuMU5VdzNSL0JpNzlRaWsvc3RZMDFvV054dUtaVjZuVGR0Y0hlVlVFRFhleXA0elU1Wis0OGNRZGNlb09aUENrRWtFQVo5MmhTNnozdGszeWpwK0dVY1NTVTZCM3VlMDV1bjNMOTlwWlJLNnZZWWxUV2lEMEZtK1I4RWxaY0Rqc3VYbm1EVysrL2F6ME94RTFPNDJnRGxnSmV5Q2VFOVFxdWRyQU9QQnl1aVdxbHdZZ2lmbDBwdUJ1b056dEpWbzRKdWduNCtaeVdOVzYwRWlabk1JbVdDU0VRcXFXWWw1WVUyTVkyR09wamRSMytTMUpjYUNjUHNCbWhtMXAyK2d1RkVKVFlLMCs5ZUJjMkFpdUZYdXpHdllHZHFsZzZld1h2aVdxSmN5Tjg0NnRmWXkxV21peTFFcUt5Mm5URVltWlVESjFuMHhaUG5XazM2VGxjMWR1cVVtYWlkNXUwQlZHUG1BVE1GUFVGdHZxUnVuQ2cycmw0MS9wS3hhWklWVSs5Q05aUkdxVVNVODlZQzBNdHhpQXExR2hUdnlyS3czcUpKbStXb1hQZEIyVHJzcTdXdVNaMUhYMklKSTJlQTJmWkV4M1JUUXgxQzJBZU9mNDRZYm1NUWxjVk9vbVVBVkxzRUQxdzllQWg1KzRMaFF5cXdmS3VnSkJPa0doRW1CQm5FaUJqbXVKZzR4T2lGdTVmWDFyQTJmdEFyNktnT1ROV25SQkh6WVZWRk41KzVRMXVoWTlEaGRoSDVwN0NRaTBtRkZUVnpOUjFCLzBJSFR6OTR2TWNMcjlHZGtTdEVFQXpYWVZhelk5dHFFcnI2OFdDaFBHY09HbEZRSTliTHpYZVNGbzlnRmd0cnRBYW9acTZzckhLVlBkZE1QaldJdEhaelFDckM3QVpIdTRRK1F5THFCbGhvTERuNVBZenNBcFRaS3lXVEVxZFd6bGl6RlVYSnZZQURNckROKzl6dUx6bUxKeVJSSWhxK1dXb2plc1d0VzcwclFyeEtMaW9TNFJ1QVp2U29OWkZoNUxKVnZVMXhPcEk2aUt0WlRtV3M2aHRwMFpyb2wyRE4vZ0xVS09ab0tOV2R1TUZaU1dVS0J5Q01nUkYxajBuNTJmMHB4czJwMnZPYjkvaTdoTlBzRGs3dFZIVU1iRGZIYmkrdXVMeXdTV0g2d09IaXl0MkYxdXVyN2JXR3pwYmI0TzE5SFExRUlzU3F5VkwxcWxIVENSR0YwRDR2SlkyWE5TZFZRSmM3NjdvN2lSV29lZk5WMTdqK2QwQXBYZGdyNUxXa1R5YXBrdmVxem1FUkRsVVloY1l0bnY2MVJyS1NJd0phdWI2K3BvVFg5ZHlHRUVTWGZDUzZpZ29FY25tNzF5OCtUWmM3K0dKRGExaE5xcldEaWxGNitFV2d2VXdIbkhMWmVTRjl6N1BLMS85aG9FZ1EzRTR1Q0RTSVdMOURZdzlXaG0zT3NKYVdTS0Flak9menhiSWVFQ2hCaUhoT1YxV2dKV0pHanlsd3V6eDRoellOT0JzcnpxQ3BOR2xWSVl3cDVXSHFvdzYwSVZDcVZ2T25ybHRqZFBXdHRBZHdmUEgvTG9XTUxHNE5xRW9WMisvelluUG1ZOUo2ZFJpRmhGdnp6cEJ1dUlNTXZza2JkcXd0RnB3ajJzMHpXQytSSjdXY0ttVmhUWXZ2a2tWUTJ6NnJtTVlSL3ErWTZpRjNXSExhck1taUxEZkh4QVJEbU9tTzF2ek1HOFpOb0Z5bWxnL2U1ZVh2K1g5dlBEaDk5RTkvWVJ4c0JhcmtFdkJUTkl1V1o4QWxIVlYxcVZ5NTVDSnVVSUpzUGZuaXl1KzlxdS93ZGQvODZ2Y2Urc0IzUUNyVWRqVVNEZEd1aFRKaDVFeEQzVDBSSW5rN0wwSFZCbUdnZlY2VGFrakk1VitFeWpqZ01UQzFSc1BZUnZnQ3RpWUpydTYybk42c2thVzZUS0MrU1FDdVNyOUh2Y3BLL3R2dmszWkhhaDdJZVdlalFSNnNhQndyVmEvcEVIbys4UktJdSs4K2dZY1p1QWo1MG9mamRpTDl4eW0rWWMraFZuemdmWFppcXdqNHloc3FsQ0dQWDNxYUNpV1N1czlmZHlMdU1Ic3FvWFdaNkFKVUdubW1kZzdHaXpGeUh5VXFnUXlsUkVKMXMvTHhsNzR2RzdxbFA3UkJwZ0dtVkdWQ2hEeXhMblNmQml4ZGtRU0tpZG5HNnVuOTR1TnVJblY0ZzNPbTBHWm03WmZYcEtxdWdZWkNhNWFBOEdRcmFVL0lUbzU5VWoxVWs4anhva1JNU1JrVHVCc1dzaE1LM0ZuWEtXQ1JBS1YwU3Y5QkN0ZFBZeDdZb3lNNHdINmhJVEFvU0ZudHpwS0ZDN0hBME8vNDhWUGZKRDMvcjRQczNydUNYamlscVdBckNKc1ZuRFNHYWl4VENpT0U1ZE9BaTZXbFFNYndMWWFRVDE3bC9lKzlCbmUrL2xQVWkrdWVmakttM3p6UzcvSi9kOTVuZjNWeU81dzRPemtObVUza3BJd0RwblVXYU1NQ1VyWFIvTkxSWkhPb1dzS2txRmVEbHo5eGl1YzNmNGduQ1JDRCt0dTVmbDN6WFF4VTJVY0M3VmFKU2tYeFJqbDRZR3YvUHl2c0ttSkZjb3FHTkpsTFhWblFqVi9Fdm9nWEI5Rzh6dDJaN0RDWm5RV1F3UlZtWk1LS1pDdE40T3NPNTU0Nmk2eFM4U3hFdDNVaXRJYTJWVkt5YVNRUEJEYy9BOW5ORzJVNjNRaTFYOXN0aUNtTGpkQXNvYmIyVzM4T2lGSGs0OGpSc1hOL2pjL1JlY2ZrVElsRDdhcFNJMWJsbG0ydDI3ZDh1NzNUQlpPTS8yYkFxdFpDY1h0eFdIZzlXKythc1ZpYWtWajBTSEdJQzBiV0NZSXI2WExpODdTQVozenBScHUzandQdTR3NlRabUtsUWtOYVNvWHZMWkZZTXlqVmN6MUpwVWxDSmZYRjNSM1RobERaY3ZJTzRjcnVpZHY4YUVmL0F6di9keW5ZUlB0Y1dzTnAyc0x0TGEycEkxcUd1eW1NR2VJaVROMk1HWnlRSkFUYzZvcFFMWmswWEIxeXQyWG51RHV4ejRJYjEzeXpWLzZNbC82dHovSDFmVTduSzAybEt4MEVraGhSZENBbGhGUklmYUpzU2k3NGNEbWRNMXVOOUwza2F1SDEvekNQLzlwdnU5RDd6R212aXVrZzVKM21kQWxRaWVVb1JKcm9OZG84WXNyNE1GZ3pQTEdCZC80NVM5enRrK3NpK1hoSVhYcWh5ZkJFRkVEYjJ5U3diRGRzYi8vZ1BYMUdaeHZaczBlamFGcnhZUWx3Ykt0alNQbyt0NGovd1lNTmJ2ZS9MeTJqdzBXZHNab1FkL0ZZWDBGSnIvZFRQUWJ3ZG1FWmtTelZ3QVc5enZFYkhybWhNSzJwNmJKR2tPWXpXOXpVREpsa1ZNa1FKUkNjYnV4MjZ4bWdxZytRS2Q1eFA0Y0cvS1ZnVEZ6NzgwM2JFMkNWMUJXU3lsSXdmMm9LWHVnT2JOMVluSjFwR3Rha2ltVVhTZm1NWkROMldhaFpacjJNYzBaS0dVZ2RaSHRZVTljSmF2OXo1bVRaMjl6dis2NE4xNnhldVl1bi8zdUgrYnBUM3pZQ1BxNU8zRFdtUlpwdzQ0VXRDaTVtbG5RcnlLbG1NQnBVdlpva29FNlRDdlZHRldDK1FaQVdvbFp2K2NSdGhIT083aTc0c1ZuYnZQaUp6L0NxNy8wRzd6NjYxL2p3YXYzTERseGYwVTZLT2Y5Q2FIQWNEQi9vbFJsSEVmNjFMSGI3emhmcjdqLytnVy84bmYvQ1IvL3NlK0h5dzZlUGlPdHJVa0lveUZ2Z0lXaUhpcnNNanpJY0cvUHYveWJmNS9UTWJJYUtxbWFENG1ZQ1lOYkZNdmE5RlZNZEFRTzJ4M3JZb0pLZ3FDNUlNbDZXOWRpQko5Q05OTzA3NEFkR29SU0NsbUZxSlZWaUJZZnFaVVlPcVpZV0xOK0hPUnBuTGdVNm9ZT2xnbk52V21xcFVBbVNabHlaVUpyekszV2hTODY1aDVjc3dUeHFiYXF0UG1INW84WGdwYkorYlhLd1dwK2pHUzdRWmVlYzRyR0loTzUrUWlLSVdJRTh1NWd2b2tXRXNXVE52MGFKcHZTL1pSbVh6YUdvU0YvTTFMVkdLVnhyQ3R6bzB1WmVVbWNpY1VSc2x4RytxNmpXd1Z5VjltTkI4THROYit6ZTR1cjg4Qm5mK3p6UFAveGI0UGJhN2k5Z3VkTzdENDhReG9CellWY1RTcW5sUkZPMFlyRUZ2L0JUdmFVSWx0VXRiWHJnbVZWQjRmU2k2SWh1Rk1hMFEzSUpsajA3VllIVDI5NDRaa05MM3p1WTN6MVozNkpyL3lITDNFYTRQYTZKeDh5SzQzVW5DazUwOFVWblVRT3V5MmJsSWlqOG9Sc2VQMFhmb3RPQWgvNWtlK0ZPTUJwUkU3akRMVHNSOFA0RDhEbEFHODg0Ty8rNWIvSzAzcUs3QXFiMnRHcDErY0lFSUozOUhFeldkMmtWMWlsd0hDOTg0V3kxbEVCb0JYQWlWcTlVdlUxOGE3Ni9mbUpaVDY3TUc4UXRLV3doSVZtYUNqUlBBY3lJdDRaczVuOWN5aGtyaHR5eTBtRUpEWFR4c0VGelRCVjRobDhXblZoKzRkRzN0VjlsZUtrNTBpU3pMQ3ljZTZBaEVMZmQrYTRodVFYcGhCbHJnY0hhbEhQTGxFWUxYMmhIUFpJTVlEQjBLNDZ0YVpwekJCbWZXbnBDNjVxcFVubXgrRitSejdLdERvY04vNnhwdG01VnRJbWNjZ0RZeWdjRk1ienlEdjVncWMrOFQ1Ky93OTlMOTFMVDhJVFozRDd4QkkrTjNZNWhVck9vL1VRNkd4a05FR3B0WkNyUlo4dHJYeUdNS2NSQkExNWlja1FJS25nam0zczdaeWkxV1JLZ2lGWFZuMGlQU053TFhCNkMzM2pnZy84eUdkNDRWdmZ3eGYvMGIvaDFkOTZnK2Y2Y3c3WDJRSzNtZ2hSdWJxNjROYkpLYXFGWWJ2akpQWThLUnRlLy9uZjVJMnZ2Y3EzZmU5bmVPYmpINERUM2pvK3BtaE1zc3R3LzRwZis5bGY0SnYvOGF2Y0ducjBhbWZwS1dPbVQ3MEpSYU42QjUwc1ZWd3duMkkvakpDVTdjV2w5WTJ1elllWmlUYUllUHJLdkorSzJwekhHQ3h1NXpFbHJYTThaSDdXU1dqSzlHa25BUTBMMUt1WjNMTkdhZDEzVWxETEFyWUlkNTNpS2VLb2dkTHE0V1dpLzBkR2YwbExNc3VUbVdiMmZrWTFzOTU0MSthWVpvSndid0Z2cEZkclJiSWdXV0VjWWNqc0xxOFdER0x4QVV0MXNZVTJzOHV1SzFDWlovWE56SENjSFQvYnFxMktMUVNaK25JcFdPdFVJSXVpdFJMNnhLNGNpSDNrNHJBbG42NjU3SlVQL2VCMzh2NGYraTZEdkorNVk4OCtEZXh5MkxQYXJLbFY2ZFlycEpZWnNYRU5HRVdJSVpMSHdxTU5oOXBhQjNTMG9Gbm9lcWFMREJZblNLdU9JV2N6RFpvMEhZV2ltWFMyUXVJNStjR085ZVk1dnVmRlA4a3IvK3JuK2ZKUGZaRjFWZ2lKZkgzZ05KMVBxVE0xWjg0M0oxeGZiZGwwY0xJNjVlSzFIYi85ajM2R1gvK1hQOHY2emlsM25yN0wrZWtabCsvYzUrcnRoMXhmYmhtM0E2c0tmUkg2dUtLTWhiUDFpdUV3MExWMnZBMm05LytDZ2VuRVlNTjc5cGZYMW5kWXJhTmtTZ0U5Rk9pRktqcVhRcnMyS2FXUVNvWVlLSFVFV2c4ekwveHE1dXlrRmZ6M0YwSjJSbHBkaXd1MFJub3dndzZxbGRReVljM21MMHkxM1o3V1Baa3hZYjRRcFpYUDFnazZ0YmlMT2RaMTBqWVpaQ0N0MWxnRWl4bGtrTG1BUDBTeHlQZk1pVkF6aDkyV1RpQUdKVkxjQ1dzb2xqTGhsQk50dVJyMlFPTUVxMGxqeUlWVGg0TFlkSXpnam4vRW8waUNvU2JlbUR4M3dwVWMwQ2RQR000NlB2L0hmNVR1SSsrRnV4dTRjMklPZWxCSXlzVmhUMyt5SVZOSlhhU1dZazNFVmRIRGFJeWRyTDBFb3puRng2Mk5tQm5DZUlXYXpVUXByY3pZYmZQWUJXTG4zVGlUQ1pKYU1tblRXYzNDZVNTZG44QkRaU3dYdlB3RG4rU0paNS9tRi8vWnYrT3ROeDZ5M2tUUVBadGJhL2JiZzNVZHFaRStkbWlwak52TXJkUXhYR2JrT3BQdjdYbnI2Kzl3c1ZwUnRudWlDdDJoMEl1UVFyUVlTNmxFWUwvZnMrNTZ0RlJQTDVrMmZycTlXaXVTN0Y3MnV4Mk0yWEwvVkswTlV0dFB0V2FBNmtWVTBoSkhVeUtsaElZQnJYT1ZhZkNTWXF0SG00Y0gyWUlhc3g3bnY5VlpvanJ6dEtyVlNhUFVhcWlCVGhteEhsTVJNT09oMmZ1dG41UmZUYzIwa3RmcVAyYkRTL0hHejRVY0xDTzM2OTBCbUh5VTFsek9jNXRxeTNzcVBnb1B5QVd0aFZJUFdQMStBUW1FMEdEaHhpalJHOHM1ZzJobHlrVjB4MXhiMUg1eTFKWU1FOUNnNW5nV2MvWlNpZ3dvMGdjT0ZBNUp1ZW9ENSs5N2x1LzRZMStBNSsvYTQ4eFhOVmpKUUlpSlRWcWozbmtmSUlZNFZlNkp0dXdEMDFZU2dvOVprMm1EYXEyVHYxUlJVbWR0VEd1MWRKaWNyVGR1Q0dtQzFNRmF6VEprUXN2ZjZJUnhmNkNMUFp3SjNYTzM0T0dCMC82OWZPLzczOE8vLzF0L2ordXZXYndqajVuVDlZclQ3b1FIYjkzbjl1M2I1SnlSbWtnR2N5SXFESG1rakpXNjIxbjJzZ2dub1NlcTVibU5wU0JpS1NwMmZjV2FlVENiVkRaQ1JPeWVzTXlIR0MzT0F4aVEwemRuM2pyT2xBbnM4TGhJS1RaQXFyYTVqclB2b1NnU29wbnlMZjZ5bEQrcTAzd2VCZk96RzJQVTZ1LzVzd1NqcnhCSXhyR2VIU29PT2FucERIUEVuYUNhTmhFSVdqd1h6SklHeFNWMW14QlhLSVJnWFZtUWpFZ3hXSytaNDVXRmRuSmFhMHJBN2dhMEdKU0ovWTZoWTluL3RrL09pWkRHMU9xTzM5SXYwUW1ZTFBPYUxYeVVFS0VNSTBpMFJ0RlZHVXJoUUtab1l0OExEK0xBK3IzUDg4bi83WS9CODdmaDZmT3BzK1h1Y2tlMzd0SGtJcVJXU3pOUmZJYTV0RmEyMUJHa2ltMWcwWG1qeXpnTnhBbGRNcGl2QWlVejdyMWFzbG9VUE1aSTZEREpXMkFzeTZ6bWFKL05tU3BLWEhkV1hWZ2ozWjBJcTdVbHBiNjU1YnYvOUIva1gvejF2OGZ1bFh1c3VwN3Q5WmJMaXdjODkvVHpETHM5T1ZkaUVzWXhteUFKUW93cjVtcEs4d05Uc2Y1dnRnMXVEYmpVYnJMUmtDN3ZCS2tjeFNkYStVRnhzd3VzQm1WWnhoeU90M1JCOWMzTUFvSU5LYTBzd0tJRjNOL012NXRKa0hOdTNmTDgrWHJidnhQdTdQak91SzloaFRTcW85K2tGMndwem1uUWNxc01xRzJCUmlPTUtKWE1DR0wxS0trVHV3aDN5TXFFVEZWYUVmWkV4QXF0SFZMT2V4S1dvaEk4U0dRZDQ0VjVLRTVESzVvVDE1ejhHN2JNVXYzU0NucUVNUTgydXlNR2htR3c3Kzk2VnVzTmJ3eFhYTVhBNW9QUDhibi81SS9CVTZmR0pHZTIyV08yZEJTNlNIUmIxd1lPK2M4VW9UZ3dnU1JMR3F6QXFKYnQ3OXhqYStiU2J6LzZ4dHRjbWhDaWxZQ2E5dzdWR0R0MkhWcEhRbEJTMzV2ZmtqTVNBam1KZDMwVUlzbEdOeHhnRmFNMVFwY1Q2Q00vOUovL0JQL3VyL3c5cm43bmJVNzdRTktPcSsxRFFoRTBScXJJUEJIUFcxcXBJMGR0Q3ZBeUZhUnI2VTNlNXNjc2tGYUR2cUJRRWZNelJkeTVMd3o3L2JRL0lVcExtSmlybUk5d2xrYXpTa3JScDVGYm9tbU15WVZ3Ulp2Sk5sSC80N2p0Y2NmUzRqQmZPazNWZmxPTmhvSzBaTURtVjlURkJhdExEL05IV2dmeHBpSGFENGhtVkFaVVIyTHZuMXU0RkV0VVFYeVFxRFE3elAybFhBNklqRzRyaXVlYm1RcFdyRytzeUdKaEd0cUYrMlkzbWNVWHEzVk1EQWhkTU5SdVAyNEpxVU1WRGpKeVBlelpyaUM4Y0lmUC9Zbi9EYnh3RjE2eWRxODVXTnBHNm9RWXZlYS9ldEFTOFhvYUJhS1pKYVZZTVZxTEVkVktPQlRJbzVsSnhTUE9wVzJvRDVJSlBvb2hCbXFvaEw0am5LNnRqRmN0dFR5a0JQdU14a0R3ckY5MXJSV0RGeSt0VnFRbzZBajFBUEVja0JYa2dlLzVZMytBWC83SC81YlhmL20zdU51dkxKVjl0Q1REUEE3ZXRyU0NpbXNUQnlTQ3IyL1JxWkpRZ21WTXFJcm5Xb1dqaUR5WWdESHBidnZjMGtsR0wvOWwyai92QWZCdXpjbmNGRXNwTVJ3aFZGQnJ0djRMRFI2V0prVGhtT05tbXBnWnVaMTd6RlJKZEVBWUVSMHNPS1J6V2tjbXp5cXcwYmtUWlp0UzFONFh3OG1jajh4Y0VobEFNbDNuRWRYQUhJbjJQOFNkcFluUlhKdWdsYXFEYXpWZGFKQzVDM3l6Mjh5aXN3SWl5d2hleEU0ZWFWQzlVTWNTSUN0ZGw5QWs3UFdBZEIzWG91dzJpWlAzUHNObi81TS9CdTk3R2s0VE5SZkNhV1RjSFNpcVJGbGhqVGFMVlJ0NndJeGFLYU5WZlVMMElyU0JzanNRQmtWcWdNT0k3bmZJWVhBdGt4bTJCOGJkSHEzQ2VyMG1iZGJJWm8wa3NTbTRHek0vMFFLclFrMEJpWlVTbE5DYkQxbHlwZTl0YmNyQm90Z3hpZkdpVmxZYkg2MXdBangzRGtYNHhJOTlubm9ZZWZEbGI5S1hSQ3llY05sMWpNTmhRdVdPZ3RCTkVBVnBsdkJpaVkzd3pCWHhUR1l0ekVLeW1XTm13Z1hjOUpwaVNNM1pQdDQ3azZjdGk3MWFPQ0EwNG5ZVWRTcnJ4V2x6d1JndVlKYUhOUzZVSTErbXBXSVpJR1dNbHBEaUdzWEtQQ0c2TnZEOEwyMlNPbmljcFAxZ2t3aHpqVVpROSthbEd2T0pWUTUyZlh3c0k5dkYrSTNYVmxIb0RDakZtclBaTEdBZ1dHUjZzb1diSFZ6YmozdGtudU5kV3pETlVpdWFjMWlzUVhNZEo3QmhId3Y3VldSL3QrZTdmK0pINGJsYmNLdUhUU1Qwc0wzYTBmY2RYUkNTQ0tWWTAyK2lHSnpaZWcva2FwcGxPTUFJZXJVbkRnVnFnb3NkYi8zMjEvajZWNzdDTzYrK2dlNEh4a05HeHd5MUZXSFpuSlJ1czZLN2RjcmRGNTdsbWZlOXpIUHZlNGw0K3d6V2tiRHE0SHhscG0xb1RtZ20xSVI0clE2MUlqRVFlNGhkb094TllZMmpCVkY1WWszWjcvbmtuL294ZnZMLy9wZFk3eklucG1iSk9iT0t5VHZDbUdFZTNDeHYzZjhyMklpTTZpVzRXTUtqWGI5TDVqQnJsUUFPOStKTVlzVGFTbjh0Z2JHZGlmdkxPalhOdmtsR3NVdDJIZFdFdDJxWmhmaUM5SmYwZHB3NURGb1hrNEhkVHpteWVJQWthZ21FUmEwc0Z1OCtZWTY3ZGF5M0ROMHkzeWlXd1F0V0U5M0duSUVqME81Y0I3RW9hWXhOMzdxMEVHcy9hblNzY3lCVHE1a2l6b1NsSE15Ulo4cmxkZVpvc3hQTlVKNHpmNCtqOEhJazZzSUNQM2ZIT1FSS05hWlBuaTYvbGNxRFZQa0RmK1pQd2N0UHdQTjNEUDRGdGhmWG5Helc5anRkb3V3UHhMU2FLa2R6cnNUc2tmUGRBUmtLZW5rd2MrYXRDOTc4cmEvenRTOTloVGQvK3hYa01OSlZhNklSRUU2d0hzWkJqZWhFTEpoMmZiRkRIbVRlZWYyQ1YzLysxNGxuRzU1OStVVmUrdUI3ZVBvakg0STdhN2pWVTA4aTRXeEYxeVh5VUZBRzY0bmxDTW00SFVncEVWZUJ2TXZFZGVJd1pGWlBKV0o4Q3I3MmtCLytNMytVbi9wcmY5ODdoNW93a0RaN0V6TzM2K1Q4K3BpTTZJT1VndTJyaWdsTHBSVlAxNFVEYmYrd2J2UEsxTGdFcGptYXlFeWtFNnpuQW45aTE1Wm1KTmJBejhDdDRrSDRiRmtBalNIMEJsM0k3SkdiVGU5aGl1WHZjZXpNQTZUV2wwdW5Yc0hpeE9mT3ZiVFVFQ2JwSEp6b1ZjdFVEMks2d01OL1hrR0laSlRSNG95TmlkMEp0UHdibVZLM3A0RTJ6bVRVWXQzbzNaZXc2a0d2cFRDOWJTYkJsRFkvMjZMdFJpTkx2RHdzdEZxZGZ2ZFE5cXhPVDlnTm1iRUxITmFCajMvK2M4U1huNFlYbjRMT2lwazFWOWFyM3RKSWFvWDlTSlNJam9YOWZxQVBFUjB5Z1FqN0VYMXdEZHNCT2NBM3Z2Z3IvTWQvL3dzTTd6emtwRVJ1WjZFbmVwUEtRR3RGTTVXenFrUHhXZWhyaHg0aVk2NmtVdEhEbnQzaE5iNzBhMTlELzlYUDh0SHYvdzZlL2NTM0VwNCtwKzR5NFh4RnVyMnl3T1BWU0ExS2Q5SlBEUmMwVnhOY1d1aTZ5T1YyNEdUVkU1OVkwNy84RkovNDRlL2laLytIZjhacTh3Uzc2NEdhb1ErUkdJVXVSYXQwY2NjWnRTWVFiZjVMNDRqWlYydzVWb3QxOStjV2kyc3d1clZ0MG9XMU5mc1ZiUWRsUWRETnY0OHhlbWZKMFRCUUg5MGdpdTN4UW5zc2ZmblFmc0hqYmZaOXg0SzIwUkZBcW0zNHBXYUNaQi8rYWM1YnhPb0JUUG0xUmcyNDFwbnphOW9QRjFtZ1daNG5KVk1UT0dXR2d4YjJIdzZGcWpXL2t6YXNwR1k2ZytXdzFQb1p2VkFLMGQ0eDVDZzI0Nnd0Z2djYW01a0dhTldwY0VrRXE3YkxtZlhKbXZ1N0MzSzNZci9ad05QbnZQaWRuekw3L1V5Z1ZuWlhXODVPVDZHTFUyV21PZTNLL21ySDV1U00vY1VWNjlURDlVQjk1NHE0TFZ6OCt0ZjVuLy9wdjJKODh3SDlYbmttOXNSQjBYSDBEVTZ6bnlYTjVQSi9PMEVFRlhMT0pBMnNwYWVPZ3Q0ZjZGREczWmJmK01sL3o2Lzl1eS95bVIvOWZtNTkyd2Znb0ZBQ2N0WlphYmZBeFlNZFo3YzM3SE5tRlFPMUZHTHFrWm81MmZRTTEzczJ0M3NZVnp6OXlXL2gyZC82S20vL3g2L3gxT3FFVHBRWWJPaXBWSUhVVGY1RDlFSXNiVTU1MHhqTWlhcm14aXk3bnN6RUp5Sm1LdEltRXB2WkU2SXdESVhZbVhjVThlQ2tWbWVtWm9LWU01OXpwbXZ4S09aa1ZrcEJXMk02dUNFNG14bG8zVzkwd1pHcUJZMXBxa29IYTVHTmNickZPMVRHQlQ4MTV6aTdGcGkxaTdvcEprNitTaXNuOXZmVXVyVFVNRFBUZElHTFA0MEI0OFJjRG5IUkd0SlJSLzllVDF2eEJXb3FldXFON01sdmM1TUZaMnF2dlJadkFsR29wb21jSDYrSExadTc1N3d6WnQ0dU8zNzBUL3hadUwyQlc2ZVVXaGpHUGV1ek5ZVEllR1VacTEzWEliV3l2OTZ5V1orUkw3YXNpM0Q1NmpjNVowVzQzUFBsZi9rei9QYlAveEx4L3A2VG9YSldJK3VDVDBpMmtYM04vamI4U3R6TWpZdVZxcXk3bnB5OTFhZVhDMXZNb0hJb1VQYksxYmpsMy82ZG4rUjluLzRZMy81RDMydm5hSVMxSUtmQ3lYcERLZEIxaVpwZGF1WUJZdWV0UWpLY3JORTF5Rk5uZlBUN1BzUFB2ZjJBcTdjdTZmczFReTJrUG5KOWZjMm1zL3lGbENMRDRlQmRVaHFCdHN6c0dVRnFESFBzSjVUcGZiQVVFWjJ5eUtlVDNCU3E3dXVFV1NVb3RNRktwazJzYjRNVmE4MXIxK2hPMVpJZ2wvamJNcTVTY2V0b3VxYmp3MUtFdk9lVlJiR3RMVThVbVRJeEoxTkltTFVGZUVxOXBSU1lrKy9KYVFwNDN0amNvN2c1NlBONnRSU1cyV1R5TWs3MTM2T1lmZHphOUlzNDBtVWdRd3VBNHQ4aFB0NWEyeUpNZHhrV3RxcEZaUlZQNlhkcDgzQy9aWCt5NXJ1LzhFUHd4RGs4OTdSVkc1Wng4bVVzR3B5SW5UWGZJMWU2bENpN1BYbDdqVjRQbkkvQXcwditoLy95TDdLNnpHeUdRai9BbVhRR3p3NERWWldVZW5kT1RlTVpyd3ZMcHR0TjhFbTFVUnlLQVFkMnZ0MVBxRW9vc0ZhUU1mTGxuLzQ1dnZiVjMrWVAvcmsvQy91OStWY3hFZFpDR1pXY1IxWW5QZlVRSUFYeU9CQWtFbnZMN0paYko3Qzk0dVM5ei9HUjcvb2tYL3JuUDgzbDlZR2FoZFBRY1haMlJxMFcvQ3k1c3U2U1Q2bGF0RWJ5LzR1YUxEaUd4R1lUL21hUExaMThpV1pVTlg3UjJkeWVYM1JocW5QQWtZVnZ3K3lFejUxenVIR2VtZXVQZS8vb1d2MEloaXBaOGlJVDA5aGNFM3VNL3RwQWF4SzNiQlFITFFJL3VGWWFhYW4xZ2pYVm04WWMrMHJLTkF6bitPYmFCZHA0NmlVYVo3MTRaZW90VmhDeFJNN3BPalE3ZEYybWE3Uk9LSFp2VmJ5Wm5ZN1VPbEFZS1ZJWnFNanBtdDFKNHRablBnYlBQV0hseXRVR0dkbmNqdFl4d0kreFFpNlU3WUc2M2RMdlI3cXJBenpjODQvK1gzK1JKL1p3NjFBNEhaWFZNTkxYU3ZJZXlldStZN1hxU0I1UXRHbGZRdkpIdVBGQTFacVB4MFRuM1V1aTJuanZQaVJpcnR4T2EvcnJrUmY3Mi9ENkJmLzR2L29yY0ZVWWYrY04yQ3NoV3l2VFB2UWNyZzVJREdSVnBFdlFCVklmS2ZsZ0hlUFBPamlKUFBQN1BzeDRsdEN6aUs0Q1Y0ZHJFR3RCRmFqVVBOQWFBN1lncjJtRzJmU0crVEcxc0QwcWU5RHBYTkV5TThERVZ6ZHBZMEV2elY5ZU1Bb3dDL2oyRlRKLzdyangzV05vNzdFbEdmNDc2QUE2ZUdEUEdLRVIxQnlJbkFsUHBxNktnekdVak01TTlwaVlqVWFvb3lGbUU1TFJia2ptaTlIbTRmdXoxR2tEREk3MHpvOWhac0RnN1gyV1hTeXRwL0NOaDM5R3NldU5yamtybFVFeXJCS3ZYVC9rWXovNFBSYW5lT29jemlMRFlVOEtwazJxdHprVmtZbEpwQ2hwS1BEZ2luRC9HdDY1NGlmL24vOFZaMWNEM2NOcjBuYlBSdUhKc3pQV0tWaUZYcldXcWVOaEQrTklVbU1PMFRhcXprQU5HeEVYU0E2SHo2ODdjNGtsSXFjQUsrbW8xd05QcGxQNnk0Rm5kRVY4NDRKLzg5Lzk5M1NEb0srK0JkY1pCcnZ1VUMwSFNnVkdMTnNxcEVDVmloNE9jR3RsTlRWM2V6N3dtWTl5WDNjY1VvVlY0RER1a1dySnFYMUs1R0dZR1FOdjZpRXRZVlpuUXZPazI5a0NuMDJ4dUpUbXh4YWFIMlp1VHFrbU45NFBQbnV5V1NoejcyQTlzbFNXVFB0NHhsZ0NRWXRUUEdBWldtVGVVSytESTErWklDT0JFV09rY2NyYm9rbHZpaFBzM0lxMDFjZ2JrN1Erdjk3TXVpVThMZjBUVDdPWTZwVmJ0RlpibDNsbjBuWk5ydVdPTkZiVGZLRStvazJRUW9qcTdWVWJ3eFVEcmtJbUIrVWlIMWcvK3dSUGYrVDk4TXk1cGNzWEgrVFpkYVRvM1FzYm81ZHFma1lSazlUYkFnKzMvTU0vL3hlNU04SjZPM0FlQTVzWTZhUnl1THBpMkcwUk5kUnM0OXFrdFVRMXBLdjFJMmdCT085bEhId3lXRzB6YktvMzhnWnFwZVpDbE1CS0ltRW9kQU9jbGNBVHN1SHl0MTdqWC80My96M3lZSUEzN2tNTlNJMUVTZXl1ZHBTaHN1NDdTMzRVb1ZzbDZ4NFRLNXhFeWtyNDBIZC9pbkVsWEpZZHBFaEtZWXBYNFBHS2x2czFNY1FFOE5SMzBTRE5ISjlON3BiVXVqeVczU3lGUjNPMHpFTHoyRWtEanp4ZjBYeWE4cWpHT0RwYXpkS1NlZlR4VnBqdFRYRXBNYzZFNW5EeFVwdW9aamR2eG9rSmpGdmJTTHBIbjF0QzVKUnc1Z3Y4aU9yejFCVnBxZkV0QzI1cWdlcXRUMTNMSFYyclE5Q1Q5cU42Y3dqN2ZSV2JSanhySHRNb0dwWGF3V0V0ZlBEVEgyYzQ3ZUhwdXhEaCtyQm5jM3FDam50cXNXbGdxZXNZRGdjTzJ4MlNxelZEZVBNKzNML2luLzNYZjVYTnJyRFpGZXB1aDVSc2ZhNGk5T3VPbEFKZHRMcU1NbnJXdFpzTXhoUnRGS0NCRU9LUXVLaVNvbmh6ZEc5UWlFNWFwZk1ZaG5nZTF0bDZReXpLcVNSZVBMbERlUHVLZi95WC9ncThzNE5YM29MOWFDa3ZQc1F6YjBkU2lKTVVqbWRycEF0b0wraEpnblhrMDkvMzNlUU9kbm5QVUFZYmcwQmxMTU1STXpTTk1SRzQreEQybU0yemRvOXQ3TnRSbXRIdlN0aGdKZVN6a3c4Y1p3OVBuMSsyb2pvMisrb2p6SXVqdWJQamJ5YmxiTjFZckdmU0RnMjF5aTdCaThPeUM4Sy9vVDBzaGpJVDhNUkFNak9ja0VsaGFiTXlxZEFqTlRyZGZMdUo1dGkxbXBnOE1jbVVseWJaZ3FLaFFDaW9aRFRrK1RxT05FeWVJcmFWUWhGbFRFTGVKSjcvMUVmcG43b05uVkJxSWZoUUhUd3RmRHdNNkpoSkt2UXh3SkRoNFJWc0I3NytzMThrUHJ6bVpLeUUvWUhiL2NyTUxLK3JuenZUZUUva0tIUXgrRXgzQytoT3BSQXliMjc3WFBVT2tXMktiWXRocUZwR0wxS25XRUxPTmlZOEZrV3Y5MndHNVdTbi9PYS8vaG00M0Z0RjRqYXppcjB0bnpjSDFGb3RNbDRLbzJaeVZOTDVHbDFIWHZyRXQxT2lzRG85WWRSQzZudHlMWWJBaGJuRC9yeG55LzJEdWFTaFVlVk5jNmY2R2l5MEVNeWpOamdXOGRvQUl6OWZwS0ZvZGZIOGVJWjd0THNuSE50NjgrL1BXY1gybVdCVHNRNUVzWUUvTEUwYkozWVdqRkdyelQreGVTaUZ1ZVdQSGtuNVdkSTNlM1dSc3I5d3VDWUpzZFF1SG1FVjU2Yld4dCsrcXhLQyt1amtQRFduMXBiVEhjVXkrc0tjMGhBOHg2dzZ1aWVpYUFwYzZZRVhQdkt0Y09lV2RTQU13ampzU1dJZGlpWE9FS1FPMlVjcEs3cmJ3L1dXN2RlK3huLzhtWitseXdPcmt1bW00YS9XYmljRlFZcDFRVEZ6eVlsZHJBZ3JKQU5ubTgvUlJtYTNUV3ZPY3N1K1ZjemNhVU9kSnFScG1tcGNwMnJOVFVpa29YQW5SNzc4NzMrZS9kZGVoM2N1SUZkQ1RFaXBhRmJVRXpGajExSGNEQ01FYW9DYUFteFd2UENCOTdMTmc4MVIwWkhRaGJtQmVNM002VWVQTXNQRVJJOUlja2UrM1BkVER3bkFEUDBEaTlKZVBUYlZiekJBOVpJRlc2bnE2THZUMmdLR25pWDBzVVBVTWhDbS9uQXRadU5Ic0RraDFmMkxPbmRqb1hXdHJ3dGZKRS9PY2RNdXd1SnZsL3htdXJuWjFwQ3lTZG80SVRVVjk3aERtalpvRXNkOUkyZk82WkVxR3NyeFE3S2JYdlhvL0JEZGZvNVFvNUJENVZvekwzNzdoOGlTalZsaU1OT29XTWVQWVJoSVhXZkprS3BXMkRXTTFLdEwyTzc1bFovNmQ4VDluamdXSkE4a2JJSDdFT25FZWxhMThkM0IwYTFwQnIycmtlZ3hpQ2tZRjlwN0MrZFQ1clU3MnVESnB5dVRPV2NCVlNXRnlHbTNZcngvd1JPeTRSZi85Yy9BNVFqWGd6ZCt0dXVvTGNjS2lGMmNDTHBRaUNjOWxEMGYrc1MzYzVtM2hIWFBkcjlqR0VmR3hsUitUTTd6SXo0Smp4RDJFaVlXYWFiUERVZjc2UFFiT1Y2UE9QMit0NDhwWEhtY2RubTBNYmV2UFV4NWJVZlhDUVRqTWtlTkpwU29hWkc4ZURTbzJORXRad0x6UVJibWxqejZ1U096N0tiaW0vRDNOdFo0ZHF4bUp0RWpmMG1DOVNDMnVYb1ZZcDJrYlBWck1jY2RWODN6K0lRcVNnbVZJUUtuSzA3ZTh6enA3cmtYUEkyRXF2UnBkbklwbGVRT3RlNE9jTmdSRHlNUGZ1UFhlZjNMdjhHNkZqYWlyRERHU0ZnUHNsQ05BS2RaOWkwM0NtdXFFZFdtYmswMmZsZzZ3VHBCNmszQ3RmZm5oOTFieTU2ZDV0bTRKRlMxOXFkcmlXeHE0TjV2dmNMWC8rZGZoSXNkRElYWTk0eTVXaUdaMm4wU0F0RjdFMGhWV1BWd3NtYno4dk4wdDgrNEdMZjBweXVLRkVJZnZLZldyUG0wNWVwVlorb2JqTk02cEx3ckhUemkwQy9vUTlXMCtYU2VaWkkxay9RbTRSK0JDa2NFdnhBK2p6RG5iUGJkUE1MUjlDa3BoR1hjUkFySTZJL0JuL1AwYkk1eGUyOVluT01PdUpvUE1WVWRTajFhdkNXRWQzU3gwM3dLSHpVV0dpQ1F2WG5hek5UaW1nVHgzc2RTSm9ldG5WT0R4WXFxRkNxRkdvU0RWSjU4K1FVNDdlR1p1MUFHOG01SDEzVlRpa1M3N0dHM1J3Y3JZbU0vd01VRnYvTFRQODB6cXcxOXpzU1NTV0ttWi9KNWtTbUtsMGFyNVl1NlB5RlVvcW8vV3ZtMUg5SXlDNXJFWFpwaTNGZy8xeVpZOW5WemtLY0FyUlpxTHB5czFvU2hjTGZiOEpXZi9RVjQ2OUw4bFdocDkyRG1qV0ttajlXak43T2wydHpFTHJCNTZqYUhvSXhhQ1Y0blU4cHdaTlpNOFlzYjEzanpNVGR2c0hzMUpsaEU2NVVKQ0hoOHJNMlBaVXhGbXNabEFoWG0wL1N4MzdGa01BbjZ1NTRiMEFHckpER0dzUnFReGd6REZIK3dha1dyV0pUUXRNcjhYZzJqcGI4NHc2aTNLc0liZHk4My9KR0xlV1RnaTg0YUtNQ1J6eVNaR2lvYU1qVTBFM0Q1WEtkenpaNDNwcXNXaktDRVNrbktYakl2ZmZqOU5zTzh0eExhVWdxcDc2ZzVrL29OTmF2WG1LaWxrbzhEWEYzejhPdXZjUFhOMXpoQjJXZ202VUFYbFZvSGc1NTFuRHVyZU0zR2NpSlc4UGhRYU0zUEo0YXhEVjltUURSbU9ackhxSzFUampHSExJZzFDdlJpalI0RXlJZUJUZ0w5b2REdE1tLzgrbS9CMVI1MmhVMi9Ra3QxeU5kbXRZQnBwS2FoeDFoZ0hYam1BKytoYmhKN0hSbkxnT2pvcmFLT1RVVDF4Mk9aWlBJUDZueGZqZEFiSGJ3TGdjOCt5dEw4Yk9zeG56OC9OMERrNW5WTVJFY2JhanExdkxweExCa3BJRE5xWmRINU1ta1RvY1ZQbG5Dc1I4dERucGpIQW84RndyaDR2eEdzT2tFdmpsWk1WVnRueEJzMzRZNjViWmFqVlVGdDRPY0VETXdnZzlYTkYrLzVsYjJCOTZ5ZU5YZ2YyMkF6VVRLVjBrZWVlUEU1YytLMW9NV2EwN1U1SmhSTDVkRmhaTjJ0YkYrdXpXeDU5Y3UveWEzVU1WNWRzbzVpUXJlMzdGbU5abDVNdVVmZVFwUUFMQnh4V0JTL3lTd0oyNTYxQVRZenM5dzh2TFdVZHh0cHBsbkFORmxTSVJUelZjaUZOWkZ3ZmVDZDMvNEczTCtDSVJPVHhZaHM3SnMxY1ZEdlF4eXNyd0xkNlFwZEJUN3c4UTl6WUNUMDBSclpWWjhtY0FRNnpQZG1NUTA5Zml5SXRjVThqTmh2bUY0M05FS2JMem94UVlPZUY4UzhOT0dYUjFpYzgyN0g3eFp2YVNaemFDYU9hajZLeEl1bnRvZ3NKWFJ4cldLYVE0Sk5hU0l1bUNVV041VjA4Vmx0ZU4veTZvREFNdDFnMWp5emRKci9uaVB2MDZUZHRqbmtxV20zdGM0eWlXUFYvQjQ4RE9yUHdoZ0ttenVuaE52bjFwNHpSUTdEQUNLTVdLRlRHUVlpTnZlRVhNalhXd3VhSGdaZS9jMnYwbEZaUjZCYTZrN0JHcmxKRkdJWFNGMWdsZElVWEp6OUZHTWNqZll3djhVazVUVDExNGtvTExVUCtzZ0RtS1M2dGpnVkZTM1pHRjJWTGtZMnFVZUd6RW5vZU8wcnZ3M1hlL1R5Mmo2dldQNldGaVFLUlEzNnRTN3V3S3FqZEFGdW41Qk9lb1o4b091aWd4NDNmUTZkbnh0YWVTTXF2bnp0aUJtYTA3WDhteHRhNHFZbDhoanRZeDFLOWVpendDeWd4RHZkMzRpOVRQOStCSGl3SXl4VFJteUtia08yTEIxbHp2R2EwMUtPWXhPZVhoS2JobG04MWh6VW14SlJGbElGVEtJL0VrbXQwKzlwR0oxQlptZStMWHp3dElrcVpvcEo4MnNZcVF2a1RCeGVyYkZTZzNCeSt3NXNOdFpaUHZuWXRtcU1Od3dEc2UvUm5NbDVRTXRJRWpOenh0ZStTYm04WUNWaUJPeElHc0dlQzhYbXRtdVRzaFZaQ0E2amh6cHBtcVdHQ1E1ZjJwTFZvN1Y2L0dFQ29aa1lFek1DVWN4UEdzZVJXa1k2aWFTaXBHSGt3ZTk4YzlyZXpwc1M1ckZNTlN1a1JETmVhbERvQlBLQnM3dm5WSXFObzF0N0Y4aWJTQnlOOEk2SjhVZ2dUc1M2R0ZYK0dLSGU3a1ZWcDFRZS8vS0pTZWFKV2pxdklWNDUrWmp2ZkxjTWdLWEd1cW1WckF2TGhCQlVLZ2NrMkFBZysyR1BmVWlGSll3cGxzMXFlWjB0UXV1THBKNEdvRDZtdFFXRXhOT29wVUxKQkMvbXN2cDZzNmVsNWI1VExZZ29JOGdCNUdEOWpoRkNUSlI2TUpQQys0S0ptTzloeVpLUlNCdFhZWkh2bFhRTTVVQ01QWVdSR2dRNU9ZWFlRK29oWjJJUzYvN2hXY0xrMFV5cE1wcUcyVjBTcngveStsZC9rNjVrdEtpVjBub1hTSldLQlBOTXpPd3krOXMySU5DYWpkbjhjazlOMXpZNnJYVW4xS21kajdvTXMyWU9oZGI5WkpLMFByZFJwc1pxbmoydEhpOUFFUEVaTkFHMDJvaTVjTWk4OGR0ZjU4NTNmSnR0WGRkUkR3ZExqQnd6dlkvVWF2VVpVckVHRlRGd2ZuN0dneVNVYlBHVDJNeGoxTlpnUVp5emp6WTcyWTBqVktHMitwV21FU2xRTTYzQXJ0S2t2VDFIZ1RJSlZtMkxERERuNFUxRTN0TG1HMVVKT3ZWT09CWThzY1cwUE1MZkdDT2poQmdzcVZKWU1Fb29CclZLdHBJSGIxS240Qks2ZGZvMkZXOE44Y3hjcU9KRUllb1IvT0Fxc05XUnpVaVhzVk1Ca2lNbHZzaTZxRnRwaXl1THVJbUFwWmpiZXlJQmtXSVRwcGJpeHIvUGlvTEVPNUlJYzhWbG9BU2wyNnl0eFd0SVVDdEZGVkhyNWkrMW9GV28xUUtzZEQzcVpjbHZmK01WTmttSTJZZzNOTkhWWUZ2bVo1aWZMWjh0d0VRY0JXdHNaNStYYXNRVDFIcFhNbjIrb1lQTk1uR1RTK2U2bFNBMmJLOEpzK1dvdFNDdDVxVkNGVllTdWI3LzBBWTE1UXE5VEFHN0Z1Z1R0VHd6OWJFTUN0WVVlNVdBT25XbmJMNkVCUXliZExjOVZ3MlRDVFFmOVlqNHArQkl5MDd3T2lOWW1sSkhQSGJEbDNtY3BkSSswTDcvWGZ5VFNZWElYRlRaZm1PQnhyV0ZUQm9zYUVjME9MVUdad2pINTIxcVhDdllxdk5HNEtZRHgxV09VM1c3MnVjbHlHTEI3SWNyVFFVNkt6YjdVRzgyRDdpSlZqakVLaXhpSkM2VnhlcGlyUGxFbURSVisxMko0azBPck9IMTZlMVRtM1lWWkphZ3dCUWxkbnB1MDU1aUNGQXFEKzdmNTZ4aUJVdTVTVzVuanREV1NtbHpJRzJwR3JPNHVtaFU3TmVNdGpSK0w0OUdwODlZTm9LWEg5L2NhOWRHWUhFWlZmTXJxbGgvVCtQaGdvM1VnRUVMTVFVdUh6eUFrazFyWXNSZjFUSVByUE9zM1krMjBseXAwQWU2dnArR2tVS2thQ2EyWFhkbW1hd0dqMThkSDh0MnU3aVdETWYwZmpPSTJaaUQrZmx4NXk0L015TnA4WkgzLzljZXpaeE5yYlpEWkJIWmJrVGNibGdxVlIxbGFSdy9KUzdXcWIrck5xSldwWFc3RTRrdXpYOHZsK1YreWVRVVFuUE10ZFdxVDB6akhXTkN4Rm9oT2RUWTBoVFV0WmxZazJkQ20rWWlWQkluNXlmZU5Mc3dOdmkvU1dMMXpqQWhXQ0J5SE0zRUdVZDBHS3lXUDVqRU5FMGFKa2Q5MHF5cU05TTBzM01TUUl2ckF4Tk80Z051MUFoWDNPUXl4aXBUT2F0cUt4azJ6VlBkdERNenoxdEdpZGZjYTdXeTN4Q3NlcmxrcEl2a2NiUkc2SWNCU0diV2xnTFJ4aFRXNmtOdktZZ2tGeWpLK255RGlGTEtpS1F3OVNGVGpObm5qR0NZOThudkY0NnRpa21qVk5maU9zVk9wdmY5Q0UybXZzdlJDci9ra2ZPcWE3ci8veGttYWN4b3JHZ3NIcWV3V28zaW1tUHFRaDVBbXBrbGlpRldpaEZzazJ6Vm9XYWxOalhhS3R3V1djTjJkMkd5cVFrMzdGanNjN0p3ZUpmUmFXaTJjYlljTG1sNVhVMkxSUDl1b2NxVWhlWnNHS2d5c2pudGpaZTFra3YxKzVFalFwblZjd0Z2N0JCS0psS2hGcUxvRVpyVjZzYWxhV0hSV1JOTVBoeEE0TGdudWRJNmh6QTV0eko5VEZXbXBnNW0xdnBsTmZPaWxlQldHL0U0Mjh5VmhFMTlqajZISldpRm9wVGROWEU0UURqeDVmWXNacDA2TkRQWituMEV6ZHgrNHZZa0lMTVd1bUQrMENRTUZzcHk2VTg5eWlEemZwc1BkaVBHNHZDdjZQSjYyakhENmZaNWc2RUREVER3WWFsdHZWeXpUYmJGRFlaclYzSmMvdlhva2VZOExUTzdvdWFGajlCc1JrZGoydVpMblpMVjJrL1o4clhjTG9EUmt1c0Vxb3dRV3I1V2ZGU0ZQbkxaN2dmSjRuVy9waHBrU21Wb1hlQWxzSmh4d1pIOUNjblBjZk9nWmlTdDZkYTlTVXEzNzlQTmtsS3B0TlpOQU1USWZyc2xTU0JxcFVzQ3hTU3VMSm5aQ2Rla1dKMkl4MHdUQyt4TjA0bWxOV0ZpY3ZJdER3NFRXdHJBamNicTRzK3pTVWtEUTZxdGdUVnFjTU9yRnBKRHZscWc2NjIwVG12bXNOdHo0ZzNhczQwMW1Qd2VFVEZDclRiTEpZWkExY0xtOUlSYUMxMGZ5WWVCR0JPdG40SHRYZHV3MXZ6aHB1bFZaMlpvdnBaaGROeDBzdWU5OFAyb2N6cjk4amlLcnY4dnhFc1czL3A3UEcvKzNrUlFUd0h4VXVBMjJiZUNoR1dYZUxlWDFaMjRhZTU2V2RpS0NzRW1yWW9FaDRwbkI5eDJzeTFBTTkyYWViRmt2RVpJYlZHUFVUY0xQTGFtenpKOWw0Z1ZObG16UHR5ZkVidldnQTh1VWtLcmxnd1dhS1BVeFRYSk5GOURtKy9pY1NEalM0ZlNvODE3VncrRVJ2ODlNUEFBSjN3TlRaQzBTcjNzckdIb1RuUFd6ZDBvV0ZPTk1Ha0VBeVlDMWdsL2xwRGk3d3RoN3U3dkJHcmZQby83Ymo2ZHhUeU4rUVBWVEV2QkFvNnBjNDFhdlZqTXRKcWk0SGx2MjhOKzhrL3FFY1BPdnRLc3pWZzQrTWRNY0pTLzFYeWcrVTBBTThlYWRlYm1jQnYxYm9xaUJad1hlVjFIOU4rOHRKdkhzVWszL1hMTFYzcHNDMWVkdTdCb3l4Q201VWcxUjZaSnJaYlV1THdRTjcrOVU3MTRucEVSVkVGQ3NOeWhpTU85ZnFGVEJIZHhUUGs2elVoYW1GcEJwKzc1Uk5Nb05kZ2NkYnV1aFcwOFJZZ3gxS3RCanhicTl3WnRsVkpHVXJiTTZWYk9xbE1mS0w4M0xaUXlFbE5DaDRIMTJSbUh3NEc3S1RHT1c0TkhpZVpzdTlNdGk3V1pzZUxtTDluOWk4dDlPOGZiSnpXTllzNEdVOTlkTWZPbXV1Q1YydmJFQ0RsTUxhUjAvczNwU2NtbDBLMTZSQ3JWR1MwbzF0ZzdSWERFS3pRckR0TmI2RnhjbGc4N1VrcGNYbDRnUVMzRnAyVTVMd2hybHZiRlVLOGJUc095cldxTGJWbG5VdjkzYmY1cCs0QTF5aldmM3RmRTUzK2k3Z2ZlZ01Yc1Q3dCthV0RKRGFkZVdHUUpUN0NlLytUeTZ4WW1Yc0NEWm5GQndDM3lhM2swUzZtdXpDbjBkcjZFRmpnYXFaSTlYYVJDcXRZbFBnaG1qdm4zSEtFYU0zUmNhM2FDVVpkMEhuMlBlSjRXbHF2bEVXUVQwQ2JSaVhPbWFFdDhiT1pOMWRIaUNGaFUyTkxkWVh2NXdOcWRDdlI5NzUzUXJjOVVTMzB2eGZwdjRaMUgySnl3T2wyVEtXakNKdEcybEkxZ2FKcmxPaGx6YytSYk9YRXNBcnlJYStoYXB2eXR5WS93ZFptL3l6SVMydTlVcVE0QzJHL1U2S2t5R09qU0FvNHFsYUxaUjdpWmlkeXRWNlR6czJtbWZVcHBZZGJZY0ZFSWxMSENxS1RZZzhMMTVSVmRzSGlMZGRrY2FiVkNjODdaVXNBdUNXNnB0ZWZ6cG9CakF6d2EwbFhxeFB4enh4N21jKzBmanlCcnkrK2ZQaU9laUNrM1VkV1pWK3IwVzI2L3lEem95dnkzVm9uWU5uV1M2SXRFUThselVaSE1FZmNRM0xrUEJZMXpOSDd5SjhSbTE5ZW1LVzVxRE45VWFrdHNxL01qcUhGM2FCcWxUb1JpRlkyTmdQem1GcmxHTFJ0Q2docm1qMjJvRFRrcWxMeG5mM2xKYXdhZTRqeU9URXMxamExR2VQL2Yxdjc4V1picnVQTUVQMzdPaVlqTXZNdDdEdzhFUUJBa3VFb1VLWlpLUzZrMEtrbWxWcXROTTlWVG11NnVzdTRwczVtZVAyNStHTE51bXhxclhxcGJHb21saFNxdFhDV1NBTGlDSUFCaWVldGRNaVBpTFBPRCt6a1JlZCtEU2o5TXdpN3VmYmxHUmh3Lzd2NzFyMy9kQjRFNDQ3ZGI4TUxKM2R0TVBoTWw0N3BhYUt5ZnEwYmg3Smh1Q2l6VW5PTDQvcmcwWTdYN2xyLzF2WTFTTHBuYXpWblBnUXBOR2tYSFdCWE9XaUFLTStJemtjUmhQdWc1NmozRG1ZbjU5UjBVcGVQWFc1c3RVb3pXbmpQei9nQlRKSTBUSmFrUmxqaWJGM295RHpuVzdjcEhDL2FwdVlqVTE2U2owTHlZQjZtSi9aTUdzWHptQitVNFQ3OTlRRDdVanVkSm93d3E5MVBwS1hGbEtLc1gzV0NEYWhkY2RiMG00aTJRamVhaThya3pLbHBXMGFOcWFMVWd0YkJlcTNjcXJVRm5GVnNibWxRc0w5RnEyRXJNVEN4SGNrV25Vam5YSmhNWFFKeFcyOFdwb0hkd2prRGhjR21HRW1mY3RsZTBxeVJLeVJReEJVT25oVFVCM1gzalRIZjdsT3UzaFIxYW9lN3RzMXRCMDJaUzRyS0ZYQXNNM0pDdG1wL1lWM0IxVVloVEtIc1Y1QmNiUDExMzExWitzZ3Z1RENhZUpkdmlkbTB1YU1iT1ZZWFlKVE9XaE44T2VsaWJRZlhLYXJWZklLZXNVckVtcUNIV1g2L0tNNUdTWmp1M1Z0OHBGbmxZWHJlWW5LR1dKUzhlb0N3Y3RlS3FwbFk1cnFOVXhLdjk4MFo0WlV0U0llblVIbXVNaEpvZjFqendpZHQvMnFCSzBRMm9LVVU2TWRSTGJuQzVXQ2ZkQ3I5bEM4bnFqRVlCQ3d0c3A4ZmNab1Y4eFZGbnVDL2hoa0o0WGtyYmlXcFJTVTkrcmY1WGJ5Rkk4TnF1NnUzU1M3R2FncG1LcFFDdEZUanBlMnQ0NzRnbElyN2dPaUhGR1NFUWZNZisra0tMYnVNQnRqdFZBb25MaWM4QzNnbHhuT21HbnNQRGUyeE90b1JiNTVSZHorSHlpdDRMbVdoakNEVGViaWhnV1dEY0pVZXh4VlZKZVliNVpCeE9saG1OYldzVFd2NURDd1AwNHF2ZW4wT2MxcmlxRjNSV2NLdlhJd080UUI4Q295dGM1NUVQM2IxRjdoeXU5K1R4OE9SQ2JFd0pJNEU0Z2NOSTNJK0lvV0Nkb1dwMVhzMkNOTlkzV1MvSUd0WVoyRkhaQWxMcE5wNDZSM05aSjRhNjFYeXN2dTN4VHZIa0FpK3JSMXVsL2lsMWxIWjh4OGIwUWFoYU9DSTJPb04zU3lhN3VpdVh0ck5yWXB3TlVTb1c3dGdIMk9NYVY5dkluMHJ2cUpyRFV0WFJsemh5Y2MwcjVReUZTOHh6RmQydDNISzZpbW5NSml6aGRGaUZ2WkkxYk5mUzhodGtzVjRzSlV0Szhld3ZIOEswaDJuU0JXYUNFcDN2R3psU3Y3dUFUUWZHT1Q3NjJVL3ordGYvbXFGSzk1UmtueEswbDc5b082NUtFTlZlRDUzRDJHb0c5bFZrQlhkbVdIbmJ0VGVxTjlkZTA4NkVTMlpBWXVjTEpPdXVIZzMybGd6WlJlWmM4RU5IS29rWFAvTUozRTRIMEtZVWNWNWg5N3JtZFdIcmhxWWwvUWlwOFBqOTl3bUNLWWxtY293NDUzR3RDTHBlMFBwZHBiaVZZUmlpWnpCMjVuaFJybFpyVzdTMUxJUXNpM2hwRUpPVzY1V2JudUlJYUtpUGZZQ0JQZjBvbHJlcWxmbDFEN3kyN3BiV2NTWmc0Z1cyMDB2VlRWb09xQzdMNGt6aFVLQ0lLSVRxVm9sZDNTbGNhVitzTGdWdlFaOG04UXJsdGpFQ1RyVHRWNXk2MVZXcHRocHRrWnJNR3F4b3U2THpqamhuVXBrUVYraWt3MG5oc0wvV0tWZFI2ZlZlSExFWlJxRjRvY1JxdU5DZm5jUDllK3hlK2dqdWJJdVVrYlMvcG5NQkozWjhyZmRiR1orRnVpTVpLbWdjcmxKbFU2VTJMcFVqenlvMVhLMmhseTJ1SjBNSkd4Z3FoVkxGOGxndGVGSEVVWnpqNG5wUDJqcTZreTIzWG5wZU96dnJkR0Z2R2FnVldyTnBocW5SZWEzaU8rSHk0UVBPbkVxeGVySk85TEtjcXVEYnVWb2c0VXlGcyt0Q2wxV2VVcldrS3lWMkhZTTlqZVZidFJZK2FERmp0U2JYRE9Mdk40R2F0Ti84dktmVlpseDIxaUxybG43NVNsV3ZqVWphaEhWRGlkRnA3OGtpWlFndUNCSUU4UW9MMXcrNUtVSnd2RFBjM0FsVzNtY1ZCaFVyR0RwVE9jU0plUko5TExrS0FHUlZ0M2RaRjdzVVRENUZwWWE5aGxUVGVNMTA4WWd5VDVBWGNRYU53UzE4Y2piZW9POTFmTnpRZ1lOUC9zeFBNNWFrbnExeDJXb1NyZ1RTMWdkek02RnZ6QWI3akFwaE5zQ2lib1MyQTFmZ1JHcDh2ODRYclhmSUVMWDE4eXZ0djVTRTg0SjRWVkI1N3VVWFlkZkRicUFPdU5YSjBKbVk5YnVuUExkOGpmR2dFNW9mUFdRKzdEVzAwMWw4RFRSaGRWeFBDR1BVKzYwelU2KzVobHhQTHZxc3lmc3FvVy9Mb2l4clo3MkdxcWJaZXAwOXlURjc4bllzczhUcTJqenBkUXoxcWw4d05mU2t1THBycDFxbFdzR1NsdVQ0MHZvd2lpczY0Y25wN2xXYmZ0SVJNUEFrakNjMmlxSENxMGZQZFlvb1ZVZ1Q5TE95ZWFzczZzRjBzZG9pTnpvNG9zeGw1VFBxUUV6ZmVUTWdRK3ZHSzY3dXY0UEVBenJjY05aUUthM1UvTVZUZ29OcFZnY1dBdHphOHR3blgrYmdoQ2w0RHN6TVpTUVc2eFNWckgwelBpUGVWR0s4d2JuclZtVlUzVWEvczZGYTdSeVlra3psdmRXOHNiR3BTN3V3VmV6UFMyNUZ1Q0twVVgvbUZKbExobTNQM0FVKzh2bFBxVGZaYmF5UXVFcVdEUXlScXNvaWp2bnlHdWFaZDEvL0VXN1dXbHNYaEJCc1UybXhmbzA0eXNxakxOZTlGVzRwS3dOUUtGeWFabkZiSE1lYmFGWHRLWEprSk9zMTFXcExOOVo1WThhc2tMRTFkRjJlWUEvVTl6TmZWejFLbFNWYXRMaTBGNlhZVG4wRXpRb1VMMlNuUDdQTFJGOUlvVmliclhLTmtpL0VVQ2hCdlV5MkRzTjZ1Q1V2TXFJaVFqSmtwRFlDMVhsTldUVFdMazdhcEtkazkybmVVcHFoVW13TWp4T3kwNURQT1djeW9GSGZ3d25SYWd1bk12SE9kLzhPNWd1WUx1a0dJYzBIcldNRWxSK2ZjMGI2alE3VExFVVY3anZINmNkZVlQZlJGM25rSVc0Q285T2FSdkdacmhmdGR1d0trOHhFbjBndWtrMHRKdHVpOTA1L3FtWnlCVk9VZDFjWjNWVitLZXJpSjFMMW1EMVYwVVdOeTh1TWs1bFpEbVNmU0huU29aOTl4K1NGeHdHbTJ6dE9mdXFUY0dzSHZlTXdYdW9RMVNCSWlteTd3SHg5alM5WnA4K2tSSmVCT2ZHalYxNUZwc25VWlNJbFI3elRSZDZHVFRYVjBTYzdNcXRuYmIzK2RZTjJOM1p5TTFMbkZDQW91ZmJ4ckdoTExOSEk0cUVTSlUwMjVHb0JaWTR5UFRGaTd3MVBsdVZKNzlhaUh0Q3dGckwydEZzOVpTbWFMWFdMVWhFb3I1VnQ4WTdzRldVcUhnMkRMSmZJdFlWMHZZZ2JJbEtlT0NGSEI3YXlkQ3VmYWdqbnNhUzlHb2ZtVFVYRTN0K0lpZDZCQ3dweU9CMzlYSVhSS2xwWG5PQUR5THpuMFUvZWdQRWFIdC9YcXJjQkV2UGhRRm5wVm5sckY2WWsyUGF3M2ZEcFgvZ0Y4c2tKbzNmSTBKRzlHck1FelM4eWtYNVlqdGRiT0tzMUUvVTZ4UmpiTFdRUkM2ZlFVRGRMV1RwRnJXNGlibGwwZGFFNHlSWStadHpnb05md0srWUlROGY5OGNBanlYem1WMzZKMkR0NDlqYkVndzZKS25XUlpTUmxldE14azVpVmhqK09jUG1ZOTM3OEJyZDJBNEZhR0UxSEJkZmppR0U1dHZXaVhqL2VPa0FMVHl6Y3VoNXlybUhiY2ZlcjhqSnY1QlFXZHVycFdYbWFKMUM0RDY2NXlKT0hzZnBNejlKdnZWcmM0cDB0UEF1eFRKeWhpalNJR1lZZTZBcGd3SElLVU9PU3duR1ZWcDZ3M25waXBLcHcySHNYVXk5cE9VazdLZmJaWnFCbG5aOVVvemIzNzd6bUpaVVA1c2g0cHpQWDUvMDFsMis5QVpjWE1FOE11NkVCRTZyTnBjTGdLZ3duYXR1aGcyN0RzNS8rTkdjdnZNRGpLYkdQVUZ5bnpWOVNDN2VaR0dlYzdjNDYxcUVRQklwUEpCK0pibDZGWm1vMDZ1R1hmdmxHdEtSU01xSzFQYXNoT2JRdjNudmY4bzBwVGhEQWJRZXVpSXdEUFBmWlQvTEN6LzBNNFlXN3NPbVpVaVN5ekdBTU51bzZ4c2c4ejRvU3hnUnA1T3JlUFRxQnZEK1E1a25wK3l0bHhycFR1NHA2Vm8wMldReWlQWDVqa1g0UWtiRjVrSnQ1U1M1UFhULy9rUGUwZDM3aUh2ZWZlSTJJNE5aZVkvRWU5dHVNb2lHVmpodC8xNFZoekZ6ekpndjFmSlV3RlhXSlRmYXluWWdsVVd1eHJTdnRBT3RQYmdrdmk1RWFmYVdJa0N4VVZFOVgydlByZDhna1FqRCtVWXIwbmJEcmhEZS84eDBvRVM0dm9RK0tSSGtJWG5CNXRwd2xrNlFnUTZmdHc2YzdPTC9GWjM3aEY5amNlWlpSQW5OeHVCQjRkUEdZcmhjeXMrVVRkWEhvdUlTYWZHZWZLY0UySjZvaXk4clFaTjN3VnBOMERVbUtyd0JLUXNMU2llbWNhUm9Ea3hRdThvRnhjRnh0aEUvODRoZDBrdGlkY3lpSnd4VHAreDZIa0taWmU4cHlKbzRUd1h2TngrWVI1cGtmLytDN01JMXNleDBJMkFkUGNOTENubVh4cmZPQUNzbzhaZmNXQzc5YUNMYUNmUCtCdDZlaVloVXVic2R5ZkV5cnpJUVA4aXFLUGtyekxxMk9vZ3VLaFZOa0lZcXZiMnNVOXR5K3VMT0V1aFp6Rk9TenlCR0hORFdpWmNTNzVoNlVZckk0Q2VPb0xsOTRIYWMyNjg2bS8rc2F3bElzZktvam1pdXlLS3NmaWg2M3p1TlVZL1JlaTVkRTdUOXhBajJKaHovK0lUeCtDT2ZQd0hUQWRZNXhIS0ZZRVRCR2l2TmtEMFVDSmUzcGhpMzBFN2MrL1JrKy9wUDNlZU9yWDJYYVg5RVZ4MmJiRS9OTTFuUUpvMDIxWW4wV1NDNnJGeXc2MjZQV2VCQW91VUxJeC9CNVdWMDByUTBwRkpxc3R1V2NJRDVvK05jSG92ZnNTK0YrT2ZDNWYvNnJuUC8wSitEV0NXdzZEbGVYbERraXBjTjdKY3FyOExmZ1hOQ2V4NnRMeWtHSm42OSsvV3ZjZFNEenlLWnprQks1UkVUOGltYS9xc21MRVJaWDE3ZEdITFYrWWlVeTBob1NycHZKVTR5ZzJQSk02elZqYjFxWkJhMGtVQXVtTitzbXNpQnE2MGJUK3RENjg0NWVWajFLUzdaOWhTc1g0cDNXS2FTRk5TMkhXZVV1K3ZpeWdNdnF4Q3dIVlZHMGNuUXdPbHlWbFk2Vll1NkxrWmdSMi91c0NZYzEvR3JlelBJU2JZZGRIaS9CSHN1S0pvWE9FWnpnVXlSZVBPQ2RWNzhKYVlhcng0U1REYWxFNWppcHZHZzk2U0dRS0J4aUlvbUhreDJjbnZMaUwvMENjdmNacm9lZVN6S2pkeVF2ekNXU1NtcDhOKzMzTWJWOWFqdnpEWjVjVTVqUmVrT1ZZbW94dmdtZjV6eVJ5MFFpa29qUU9kc1lvb1pVVG5pY0k1ZURoK2R1OFpHZi93S2NEV29vNDRFMFJ6YStVL0psem9UUU4wU3A4d0htUkQ0YzhBVmUvOHFYMlRwSWgydDZJSThqWXNteTgzcWRkRFZsamhBd2VWcUlaT0Z2V1hMU2xxdlVjTjZlZHZSYTY0MDVXdEFyYmJnbnZOaFRiMDhKMTU3aTdkYWVSQm9aQ0RXVTZ1NkxLTHk2aERZc0laa1YvMjdpejlXZ2tzRzQyV1dLdDBwQ2lSeVIvWUExTTNnWkk3WTZxY1VXdUhkYUNCTnJNRjU1a3l6SGp2UDRCQ250b1NKenVjSFlFQzJFOFY1d1B0TUYyRW5rOVc5K0hkNTdpM0w5Q1BMRWRyZlJuTVF1U002Mlc0bER1cDdpUFp5YzZjTDcwRG0vL0svK3o4UVAzZUp5RzNqczRZTElSTVlOSGxWV1QrQm5NNExVR01LdVpMeEV2Smo2NXBGT21xbDFoa1R4VmJsVGY1ek1PSWw0bWFrRXlaalZ0NVRPTTNySFl5Y01MNzNBZi9iZi94dDQ0UzQ4ZXdlMkE5ZVhsL1RPRTNLbUw4SjBHSWs1RVZPVkYwcWthU1RnNFBGai91N1AvaU9iT1hKNzAwTWM2WG9kcGYyQk5aTXE2MW9TbGJIYm12NVdDOVN4RXBxbzBVUUxHNDRKa1VyM3FXUGUxZU5VQU9CbU05Y0hGUTQveURnVytQaURid1duWGt2TVFKeTNLbnBGc21wU3ZQcXQ5WTNsUzdWMjRaVlhhUWlZTEhaY0c2bFk1VEJna2ROS0NWQnZlb0tyRUZ0VmlpeFcwNm5lWkcyNFRaUEtyWTdUY3FZb2hSSWNydk1RdE9rb3hnbEg0dGF1NS9LOU4zbjgxZytSL1FVOGVvOXUwK0dERUZOQ1FnZEZ5SW1XQTh3NU1ZMGpiTFp3ZmdZZi9oQy85cS8vRlp1UHZzUzlOSk4ycDhoMnk5VjRhUFVRY1JuY1RQSHFaUll2VWVzcld2UXRUbi9qYzh1WHhHZHJNOUI5eHZ1Q0M0THJQSWQ0ME0ydGQ4eGQ0TElVSHJ2QzlxTXY4a3YvOWUvQ3JUTjQ2UVhZRHNUREZYT0t4RW05QWtWbnE1UlNsbzFybnJUOVlCeDUrMXZmNWlURGtDTGp4U09HM3BQTHpHRSsyQ2EzcnBrc09jR1NmRCtKZGgwdjFOb3J0QUpwMWd0MFpUQnFrMHBwcVExY3VvZ05OU3dyVm5aakkrZW5XOEVIMUU3YXcvVTQzRkszY2NzaVU3VEpoV1VDVTBJWFpNWTl0ZkdyeUNwMnRrUS9HOFJjcThXNjRPWkZFTVVnUCswL1dTTlo5aVhyODZUaTJ6YTBScXlqMFh0YzhBMkNyaFY3cUp5MHBZZGRtUVVxU1pTZFVMeXoraENFb1dQWWVQYlhEN2w3MHZQdHYvd1NqSmZFaS91UUovclFhVWx3VG9CTnY4MkpQamljRTdJSXFSUmkzNU9ITFR4N2wxLzgzZjhMSnk5L2t2dFp1RXlDNnpjVUVXS2FtUE5Ja2dndTQvb0ZhV3poYTJVekdNT2hlTWdleGp5VG5Dcnd6MFR0aVRlOXFWUXl1OTJPT1UwOGpqUHhaTVBoYk1OODU1eGYrdGYvRmR3K2c1Yy9BdHNObC90TEx2WUhocUhEZTFHRFpHcUdNazBUd1VFZTk0UjVodjJCNy96MTN6RHNSN3BwWkJjNlNvNjR6dEZ0T2tCRjlsclk1SlpGV2czbnBzZFp2TVRTTDFTamlwVG14VkJ1Vk9UcndsN25JRjZKaEVkVitYWDEvbmdJNnBNTTVMcmY2c2RaVW90RGJPWjgwd3BETjBoWDQvc0ZValZMY3ZxRU5qbTh4Zi9ManQyU1M4bHQ5ODRjdzgyNUhvaFU5N0pZODRLYTNMeHAzT3E3MENEcVl2QnZsZUlSOHpqMStJODgzeFB3c2JRYVNxdmVXODR5ZEk1ZUV2djNmOExsRDE0bFRGZnc0RjBva1Q1NFNyRU9RTHZvS2MxNDV3Z2hFRE9FelFudTlCUk9ic0hkRC9Fci8rcS93OTE5bnV2K2hHcy9jQTNJZGtzT2pvbkk3Qkw3cUlKek1VWnJHTkxtcWx3aU9XdmpXZTJsVWNQeHVLQndmUmFZY21IS2lURkhMcWNEZWJjaDN6bmxoL3ZIWE40KzRWZitILzhHbnJzTkwzOFVkZ01QN3IxTExvTDNRdGQxeERRcGFKTzF0ak1kOWdUSnVCVHBVNFJwNVBEcWE4ejM3dVAyMSt6RWM3cnRnRXlNazNhSGRsNEJoRFdRc2pZV3lVZW8zWkcwcW9WYTlmSEZtU2d5U2xvYUJtdlpvSHFVWldHclFlV3M0VjZGcG8vUXRLY1VFbTB4VjNONXl0cmpxWStGM0hZQW1qS0lxMGUvL0dQeGl1c2NTOUJpbDMxd2swOTFPbk9qRnRpV2s2UUdVMHJsSWV1WExiS0VJUnJuNnZPNnpxdUJGQzNtT1R0Sk5jUTdRc2xxQ05uQUFNc3hxakZiUE54MGFYTW01c3pRRCtROGMrSUNYLzdqUCtUWG4zOGVOMnhnZDRZclFuQ09uSklWUFNIbERLSWhHQ2t6N21kOEVjTHBiU2dCenVIWC8yLy9QVC84aXovamphOS9oVTJLekdtaVM0bE44SlNTNkllT2NaNzArNVdNNEUzMVJSbkdLU3NaTXBQWURBTTV3WlNTOGtGZFFFSXc0VDhoNFhoMzNQTzRDL3pjLyttM2VmNFhmeDVPeitIREg0WWdqQmVYNUFUZVozd1I5dnNydGlIZ25XTWNKNlpKYzVaK3N5VzkveDcrOHBycDlSL3hwWC8vdjNKYUNyYzNBN0svWm83S3dON3NCdEpzZEpsU08xanJvdERyVUh0UWRHOWNKZmpMeXJGd0dZMHdtbEhwdGE5YUJWSU1SY1ZDckpiVDJuWFBoWnlPQmVBMUNhL2FiSCtma2F4dkgyd3dEUjZ1QzY5Q3ZvTHQySlVOV3NVUnFvc3pPbllSMDRXdEhVZ1ZEQkNEaDBzeGluV2xwanpsbU51WFdYMHhvUUVKRWd4anpkWVlSc1k3VXoweG1OcWJrWUJRV2NYdDlFajduNTA4N2FDVFdvOHhHRGFJWXlpUm5FYSs5ZWQvd3MvK3pyK2tSSkFQZlF3ZmlxSmxFVlNpMUtrd0hJb1dCUkVPK3oxQlBKeWN3S2FIdzVhUC83TmY0eU9mK2poLzkwZi9nZW5oQXlUUFBMaDR6TFliU0RrVGloQ3pqcThENjFrM2hyQUtjd2plT2FZNGtvb3dDMlRuRkN3aEV5bE14Zk13Sno3MHVaL2hGMzdsbCtoZmZwbTQyUkR1UGdQYmdmSHlncXZyQTl2Tmhwd3lKYzRNSVRDUEU4NUNxU0gwK0JqaDRRUDg5UlVjcnZuYkwvMHg3dW9SUXl3NEFzR3JCNTNtMFFxTkVMd2p6M0dWYnpxdDFGY1c4Qk1TcGpjV3JlZ1F2U3pyL25yYVJycXNrYlQ2VzBNM3A1MkIrdTU1UlpleDFuVmp2MWhvbFk4TTRXbU5ZRWRyVWdBVEp4SFVMcFJtdjBxUzljRmxseGJuekd5V2VlQ0ZCWUZDTDZzZXNCT3E3bXRkbk1XcXlwV2QydHFCcGFCZGFFckFhekJ3TlJUNy9CYnJtbHFJeHh1VnBmcjZDamhXdFpXS250aGI2SnRhelVXVFFDRnJ2bFJzUEhVUjVtbGsyMjNaN1FiZStONXJQSHpsYjduOWlaOVdFZS9UNStuNlFJeVJsSldTcnU2K0VEckhORWVrQ3p5NmVNVDVyVE5rYzZKdzdiYkhCOGZQLzdmL2hvZXZ2c0lyZi9VWEZBTFRZV1Nnc0ExQzU0UTV6YWhHbU9DZGRtR3FURkNHNUpoTFFicWU3RHhqaHJFVVppSysyMUJPenZpRlgvMDFUai83TTNDNmhlYytSTmp0SUF3Y0hqd2d4a2duc0EyT2k0c3J0cUhIUisxZ25QWUhRblo0VjJBNkVOKy9UNWdQWEwvNkd1Kys5Z29mSGpiMGFVUlNRcHhYMms5V2IxRzFwSjB2VU14emxDcU9ia3dDSzVvMkRZZVdmdFR3Q1JPNzBHS3dQaW5EaW5XZWMrMVlXVW9HeDZ0NllUNGptVktyOWhYU04rL3luN3F0L09FVHR3cFdoUWE3Q2xUVmlwb2NMMlJHYWUyMWFnQ1dxN1FZVUJkMVdYbmhMSW1TMVN1bGxHeVhLUTBqYi83b0p0cFJEYVFrSFFacVlaemFpak5PVm8yTlRYZTRGckNzYUxlOG03YlcxcTVBNTJpd29yT0NaWnhtZHJzZFY0ZEV2TDdrMlUzUDMvM1pIL0ZySC80d1hONkR6UWwwcHdTRU1TWnkxdHlOcE5wbTNSQ1lwb25Uc3hNT2h3TXhKYmFibnJBOXdYMzRJM0IxemUxaDRGZGUvamhYYjcvTmc3ZmU1TTNYdnN2MXhTVStSN3BOajhzWm43V1B3cmVkc05Qdkp4MWp5VnpseEJRQ20xdTNlZmFsai9HeFQzK0c3cVdQUTcrRDUxOVEydnpKbHNQMXlPSHhKU2RoQXpIVGRjTGg4b0xUemNDNFA5QU5KOHo3S3pvZmNCVEs5VFZ5ZlNCNGdYc1gvUEcvKzNjODB3ZTR1cUFUeHpBTXpHTmtuRFBlQi9vK2tHWmRvQTZPOGd4TURyWTFxSldsRjZuZXA4bXl0cm9wN0ZzQS8vZDRsS1U0V1o1Q1g4a3hyV29yRmVWeVJwZTVjY3RyYjdMT1V5cUN0cXp2OXR1aW9sQm5rRlJKSEpFcUlzT1N2RXRlNmtweXJOeW5IN3h1VXRLRjcwUncxbkJVZCtENjAyQy82cmF6a2ZzcS9HdVBEY1BBaU5GUjdEM0ZXTUdDTWdpY1g3bjRHOGNHR3Y2dEMxSk9uRmE5cldqbXZSQmpaT2c3WElaMDJOTzd3Ti8rNlJmNXd1LzhsM0I5Q2pzQmYwcXd3VHVnMWVDY0UzRWM2WU5ENXNTbVpKSnpFQk5qU2d5YkxZUUJUazdoOWwxMmR6N0V5YWMvelV1LzhSdGN2UDRqSHI3N0V4Njg4dzd4K3NEKzhwcDVmeUNORTFLZzh6MWQzN005dTBWL2ZzWXpkei9FeWZQUGMvYjhDNFR6YzlpZUtEeDllaHRPenlBbDlsZDdTaXhzWGMvK3dVUE9UOC8wMnM0VGljS21ENVI1cEJSSXNlQzZvQjJlT2NMNzcvTzFQL2g5VHNpRThjQVFIQklqc1FqZEppQ3UxK2xjY2FiejFoeVZjb3V3VksvWXRmWitOWUpsK2VtMmFHeGdoYWVhWjEvV0VXQkcxSzZnR2NwNnhxZXVOVjFUS1ZYQzVMcm9tS250djNXOUhwRXFEVmg2cW9UWEI5eENIY1paS2ZXNnp0MlNWOVFkb1lZdkZYQ1FHdTdJYXRFcjFhVjZDZWVXemprd2FMRFVSaHZyYlVmMXBxcGNrQkwvQzg0SmZkOHplZzN2eE1KZTFScXVBWjdtTGpvU0FqMkdsYVdzWFRoRnUvMWNjZWJ5RFc0MnhmMVVJbmxPblBRRFFSeVAzbnViNy8zMW4vT3BYL3R0L2JCVHdZY2RKVVZpQW54SEthcWFPSThUdlhqRWUwSVJFc0orZjgwY00ySG8yV3cyc05zaXo5eUI2eXZ5NHdlY2ZlNnpuUDNVWi9ob3pOcHFPODFrKzNFSlhBalFiNVNFR1lJMmoyMjNzRHVEWWRCLzl4dkljSGo4bVAwOGtYTm1FenBLS3B5Zm5oR25BODdCN3ZTRWViOW52QjRKRXVoQ1FNUXhQYnBIZjdpRzYwdSs4U2RmNU40UHZzOTVpZ3dDekNPK0h4QVJZczZRUjRadVVHMkJwSDFDdnZPTkJpOU9xQXI5bEl4a3NYQnNrVGJGcXZHVmtvUlY4S3VHWEYxZ09WZkJDdFVnVUcva0xPOU5qUm9qSldsUFRJNnJ1bzNnQ0phWkxBWnpkS3NkcGxsdTVQRXJ5Nm5xRXZaWm9RcEdLQ3ZZNGhiRitqUXhLblVrSE9aYUxmUnhBUkViTjJCMWl5TGdUUkJaRVNkSGNWNUZFOFRCT01QV2tWT2g3NFNTSjd5SWhVeVpXQXBkRVlyMnIwSm5KenVvaTNYaWxjdmtQYVdNU0hPenEzaHJqWWFablh2djJrblF3cWVudHVNaW12eTVuT2tHci9LandKYkMyNjk5azdsa1B2dXIveHpLRE1NNVlYY09VMmFjRWwwM0VPZE1QMnhWWWpRcERKMXk0dXprbEpnVDA3aG5IUGY0SURwNDlQUUVkN296Wm02MnFlQUp4b2pMVmpqRGc3Y0s0OURyMzc1VG94SDFXR21jbWE4ZWs5Sk0xM1ZzUldzN2toT3BaTWFjNkRlQncrR0FQeVQ2THRDSnFGR1d4SGo5Z0NGT2NIbVByLy9QL3hPWFAzNlQvbkJKTDQ3ZWFUR3pTTGIyQXNHYk9xU09WN2RjeGZJOUZZRXdwcXlGNDY3eTg5Q0lvcW5aVkk4dWFMODkrbGkzR2ZSN3BxUkRqVUJaeWdKU21jcFphU1hGRW5aS0lxZlpjaEc3dGtrTHVDRnN0S2RmL01KRU00VWMweFU4OGpaTnRDT2JNbWV0MlRpSGlDZVVZZ1MzVW5RUmlzYnhZcTdUV3o1UVAwQjNCV2ZHVUJQbG1zU3RGbU9CeXZDTnhpblN1TlYyOFp4c1RIT0NkWEdvcUFJK0lvUmdkUlJ5NDVDSkQ4YjkwdmJXK2dXYlI0RWpRMkd4Qnp0QmVvZUkxL2Z4ZHFoSlBaN1BXdWdjZ0ZQZ3dRKy93N2RMNG1kKzliZmdYR1BnMEo5QzU0anhRT2NESmM2UUxDWDFqdUM5c25LTGl1dEZnenV2OWlOdVAyck1Hd0loQk56SlJnOHdBVjRuMFZNS3VHRG4ya09NNUhGbXVyb2t4dFJpYllmUWVVZWVWQWM0V0Q0WW5NTjV4elRySWlvcE1WNGY2QUZKaWJJZkdVVGc4Z0hmL3VMdmNmOEgzMkVUQ3pzUGZVcE4wTUY1c2NXYUcxU3JxaXRXVUs1MURTMlhyOElvcCt3QzlIdzdRMDAxSEtzQ0U3clFxMjV4SGErQmM3aWNiY095VkZ3eVpOLzRYcHFXWmlpWmxDeEhhYXpyUXEzWHRiU2hzQXJCUTkxUmVmcHRWZkZlL1R0azh5QzE2Tmc4aWkyb2pOVXRSUEgrQlVWWWtxVWxMaXp0emNYQUFiRmRUdG11bHFSN2FTOVBGSlZpRUtIMlVkY21yYTdyQ0NGQW1YRm9hMjl0Ny9YTzRWYTBleW02bXkvQ0FqZHUxYlBYaE4rc1A2MnptdW9WSFF5Mkg2UjA0SjN2dm9MM0F6LzFhNzhKc2NDWkk3Z05vZk9rYVNSbm5RVG16TXVub24wZENKUmtQcStpY1JseXlzU2tFM3RsZjlEazFpZ3lJbUl6N3IyZSsxeXBNNXJjQjA4TGg1MnEzMmxvQUMzMEl4ZFNqS1I1b3U4N2lvTnVNSkdJUjQrUk9NRmg0dS8rNkl1OC9lb3J1UDAxUStqWkJVOVBJdFRJQWIyR1dSeSsxalZFV2oycm5WT0Z2U3d1MThTamtSZ1JXNXVpdW0wV2ZsVkdzS0pjd1hwK0ZQVlM4UkxkREd2WjRGaHJvVFNXY1l5eFhqck5ZWE5walY1MUU3MTV5N0pVNVpmMWU1ekgzTHdGNTFSZXAvN29hMWF3MmlyWnlsU1kyTDVFYXlwYVYySDFSZGtXbkJkSXVjNnhYOTZybnVoYXg3R01UVy9tWVp6M1drdVp4M1pjemptVlZYVkxINHh5Y2pSTy9xRHZXajJLVzJ2bHlsSmtyV291VGpSRThKTHBBVDhkT090UGVQVEc5L25XSDBVKzkzLzQ1N29nenU4QUhYNnpvNHc2RUxIa1FzRWhQcWhueklVZ3grZFJZdzVQTXFDKzVsaWw5cXVMSUY0YmZiT0E3NVhCcXJRS0RkZEszZDFkSUJmWW51NWdtams4Zkl3VG9SOEdQTkI3Z1R6UmRZN3B3VVA4WHR0OGlSUGYvdUlmOE02M1gyRTdUK3lHbmlFbE9yUUh4L3RnZVVEQyt4NW51WVpiTGFyMTViTG91eVhwZFpFdVBLNWk5bE9STUt2TjVZTE96eEh0SWkzRkdCTkxSVjJLZW96YTFGZHpVSzJmUUo0WFRleTJIdXdZYm1ZbnRUWlNiMW40Z0ZzdG9QdjJucUZWenlVM3NURXd2VGtVa3F2MWlwcThyeTQ1NjJwOE8yQ0s1aE9pTklVY0kyMkVRbEh2b3FPbEhXUlBYdVBLamUrbG1yZ2k1V2c0emdwKzB5OXU5QlJ2WG5IZEJucDhzeEN5Z2hGdG9WYnJUYXJja2xVeFVyTGdjdVRaMDRFb3dvUDlJeDc4OEZXK3VyL21aMy8xMXlsWGoranZQaythSmx5M1Ezd2d4UnBmaThXMVdweDA1azJLaFJTSzFvSDNuamhqbmxxbGdWd2JKYVk4cG5pWXdXbjlxSlBRUENyWmxERUZwcXNMSkJjMkp4dEFtUGQ3VXByMTN6bkNQTkhuRWRJQjNuNmJ2L3ozdjBkOC94NG4wMGlZSTd1K3g2ZEVKN2FybDRnNFpXK0xJWTRsbHh0VmVCWTBxU2JzMW0yaW50T0V0NHNhUmVYZkZaOUJ2Q3BqdWNwYXovU2JnWnJ3cDVSc010ZUNmOTJrM1djMGYwN0paa25HeWgwenJsOUpHcmIrLytYbXJESmZqcHRzTEVEUjllTm9UVm9hZmxtSUJwYXMyZTdSR3JuMC9td2dnWE13R2JkSTZRcWxKWHczcTZTMWZsUEZlN3ZOWU9vdWF1SGVhVjdoakllbWJjdzFrYXVBd3NwakhMMi90Q1JPSDVSVnpsV0xvTnFENzRyT2FNOVNJTTNFK2NDSjN6SjBoUWZ2L1pnLy9aLy9MYi84Vy84Rm5ZQS91MnZuWTFZZ28vZWtNdHRZdDBBZDQxQXBQVUYwNDhrbGtlWkU4SU9laTlsZytxUUx3ZG5PMmdTMHM4a2ZXUktLNVpNWkhYM3RFVks4Um1LbUM1NnVEM0M0SUUxNzNIU05VSGp2bTEvajYzLzRoMnl2UjNZeGNlb0QwNXpvaUJyZUNraXY5U2puVmcxUXhaamNxMkx5Y241MUE5QkR5aTEvRlROb1Z6Mk5iY2lXTWlNdTQzQWtXd2ZEWnFNaFNOR0pYcG83QzFJS3Z0WmxjakV5SktvUkxZVjVPdEJSb0NUTFhmM1JtcnA1cXlxbjlmaHJWS01HNXRmN2dPVlVscU04R1pjdEpmK0ZKR2xvZ1JYcHF0Vm1zY2xVV0xBbUM3VmVpaUFXZW1YVHptS2VORmtyTkZoWkYvaFM2OGc1cThzVVliczlvWWhYQ1NRS0VsUk1XNEsydzZwQWRnWHlNZjhQaTJmVGl3S29pc3VTR2tHZFJGelUrQ3RsMzc0NHVJTExLbUNkQmNZNHMzR0Jrd1NoRkw3K1I3L0grZk12OGRsZitsVzYyM2NwYm9CK3dHL1B3WGZFa3BuelpPVEpXVU9Ibkd5U3JtOEZzWnduY040S3A5YlFWYlhScXZORTh6Y0FTVFZXVDVSY21Dd2U5ejVvRkZDMHo1MXBoc01WZnBySTk5L2pLMy84Unp6ODBldWNqaFBkdk9mTU8yUStzTzBjWG1ZZEs4NXN6R2pkSEJNVHdRWElxNEtIYlRqTEJtdEZSanZWVFNpaVdJN3E2ckZia1JnMUtBMHh6Y3U2VEg4eXdLWXpGUGdwTkJQVDd4TGIzR3J1TWswVG5UM0YyZWM3SjBmYUpXcy9XT3M3UzVVbnRUVmR5aXA4dTJFUllYMUhLeGphMTE4YkJIVUdpbjFNcTVjMHRNdENuNlBEMC9kTUthazNzY216WWdGdVFVbDFxaVFpNUZ3cnRRV2NwOXNNWklyMnd4dUtscElLYlplVjZTL1UraHIrM2ZSUzdTd3RlWkpkME01NTY5cTBpVTU2OWF6NENuTk9PQ2VjYkR1dXBtczIwdEY3WVo5bkhyM3hHbi95NWh2ODBxLy9KcmMrOFdtWUpuSWVrZjZVWVh0S0ZNYzhqM2h4TnBvN002ZU0yS1NyWWg3Qmw2N1p1TGQ1V1NWR3BqaGJJYThlZENaWk01TXZXc3ZvZTY4aGFwckpod011YWsyR2VZS1N1ZnJ1Sy96VkgzNlJjdm1JM1JUWjVNVDVyaWRmWGRLRm9MTTVuZUNISlV4WjlsRm5qMWNPbWxqU0xpWnNnU0ZZaG5ycUtsZ1NhVE9JYWloMXViYThYNEkyL1Rud20xN2xvTHhDekJWZG83NmZvWUZHeEtET2ZJelc3OSt1dS9IbGF1M3RTZDlTdmFMVlp5b3l0bEs2cEFIS3JxMnJJRVhod0p3aVl1SVA2a0dsTFhjdm9vUTgrNXhhVUZTMWtXcE10VS9adHlZdEY0SWlPcElvY1VUTWZhNFhkMXZRcmxEaUNoUVEySjJlazhTbTBVNHFNdWVEZWg5bmhjbFdKSFdWNFZ6ajVkd3Vla3RBWWZsOHR5VFpqZ1ZwMGQraWNiQ1JML0hhK3pFTWdhNElzY3dFZ1czbmlPWEE5Lzd5aThTdi9rYys5YlAvbUxzLzlUbVlyc2p4QWdrYmV0ZGI5dEhybUlYc21KT042eGJqeHVhRkFadExxejBUR21oanJiZFNkMi9MZTRLRE9CR3ZMcEU0NDFPRS9RSDJlNjdmZXBQdmZ2bXIzUHZSNjJ4eVpwc21mRW9FQ21WT2hFMkFVbmZLMU9wUFl0TmhTaTVJcmtWbFE3TGFSbFB3dFNaaFk5YWw1aWdOT2w1eWxHV0V0NXBnbldnV0thUWlTT2NKdTU0NjNpSTRUNXBtWEIrMC9pSk9wVjhGeUlrY1RiOHJUa0EyY1kxTU4yeklod09oQWlENVNVdFpNMHZhR3FLbUhhc1NoM20xbW51MUhPWG1teDJqRzR0N0VzVFVWMVlmWEo5Znc2aGFJVFhWUVMvQ2ZOalRHekd5NzdSTEVMRG1MME55UEl1UDlJRnV0eU03ejVRTEcrdExDWDJuMHA5T0RIWjkwazNmblBSMDlMaS9JVHhnU1l2YW1JNkFLMWhEMVFxT1hNNlREbDF5enJCMUwxd2RIdE1UK2Y1WC81eFh2dmxWUHZINWY4U0xuL2taNVdINWpSWUxDWlRpS05MaFE0OFBvVm1HVkhWNyt6enJYQ1lYTFpibWxDd1g5SURxSmVkb00yc2tJVmNYV2xTTk0yLy8zZC95eXBmL2h2amdJYWNDNXlVeTVFUmZvblpHMWhCSVZDKzVnWEtDSmhaU05Hd0dSTExWSDdUaVhvVG00ZHB5azJ5YmtiTzVualVVcjBSRzVmbHB1d1hOME1WcUtuUTlzeXRzYnAxWmNiTW9LOW1aa1htVTRrUzJhS0p2bndDYXREc3FHbXUxdW54YzEzdmlacldXbXFNc25tUzVhZnFneFVibm5CWWN0UitBMWFKYndxcmFsQ1VXdCtzaFdrd25XZUhRYWl5MnE1ZHNicThSZlF2WCt5dDYyeEdrQzVTRFFZY3NReXdyMlJDY0pzTGJIWDY3WWJ5OFp2Q2VSTlFlakp4MTdudkZKZXV4dGZOeW5LdVVHenRGZmZnWUhuUUdIUXNPM3hEQTJpWmJja1h0Y3B0bklzNlI0c1JKNTVqenhQVitUeGkydlBHMXYrRDdYL3RyUHZ6eXgvbllKMzZhc0QxRFRzNlFib2Y0QVZMUGhDT21RaDg2Z3ZlbWZDa1k2eE55VnZwN1VoaTBwRm5aQ1paVE1jM2tjWTg3WE1IbEkzN3czZS95L1ZlL2pleXZPUTBla1lnZkQyeWR4eFB4cnREVnNkcTU5b09zY2tQemJvcDJhbjZwZEhhRFlXWDV6bElxb1V2UHUzSzRzb1pOcmFWaU9ibDFQc3NpVXE3Y3d1SzhOclE1T0gvdUxtd0h5RHJ5T3dSZEN6bEdIYXJxdEFHazVFaUpFeTR0c0xDell6MU8waTA2V2JWNHFEZFowKzdYZjkrd3BlWW9kRTJFOWcvekNxNE51MWxDSkFRTFE4eWpxSyt0bVl4MU5LN3pCRTNpeEtHOEpVbkVhYThMTGMvUTkrUWE1cFVhaDZaMndwVzlFY0I3VG00OXczaDRyRUp4Q1pJMmhpeUZ4cFpMM2ZBYzYxekZ5VkVkcFVpTmtWRktUeWx0OGZ2aXFCU01WT25mTE5DeVE1TytLdGVsaldXRklKSG5uemxuakltcjhabytkTHovdlcvejNnKy9peHRPT1h2bVdaNzd5TXZjL2ZESDRPd092ZXZvQzdDUExOdTZvalo0M1ZtSnNYMFhTUmJLNEdCLzROMDMzK1RkTjE3bjNnKytEOWNYK0ZMbzVvbStKRFk1NEpnSWZZSHBtcUh2Y1NMRWNjUVZ4MmF6d2VPWnBrbkhYWUI1ckpwbktJMWV0RnBxaWJwdGdrVWdSNXdFR2tXK0ZCU0sxUkNPWEdwbHVCWDNTcEhtY1ZKUkJmN2tQQk1aTmgzZDdYUFliWFd3RVZwQXpUbHF1M0d4SERkbHBLdTdmMlo4OU5BNlQybGgwbm9Ock9IbEp5MUIrWDkvMzYyNHhWaENiZUYxWmlUYXAxNDcxMWp5R3ZYRmVnTHR6Mlduc0JUZm5xY0RPZTFEbklZcGM5eERtU0ZOSUZ1S3NYZXpkYlhWcEU5Uk00ZDRsWGc4di9NTTc5OS9pOEpJUWNtYW9iTkdIOCtTb0ZmUElzYzdXa1Z4RmdxTEdVNWxYRGh6dzFtTkxsdklVSXdpb2JsQnBoUVV3Q2hGWDJzZjEvdEFpbkN5M1hEdndTT21YTGg5NjV6OVlTSjQyTThIOHJ6bjBmVkRIcjcxQTRyYjBBMERweWUzMloyYzBYVTl3ekN3Mld3WWhrSG5KWW9RcDRseEhKbW1pY1AxbnF2SEYxeGRQbVovY2NuaCtwbzB6M1Nsc0UyUmtGWFpNcFdSQVBnNFVWSmtzOTFBNkpubkVjbkNzRlBCdXptT0pEemIzY0NVSmxXbzE4UkV3dzBzRW5CTDJKa3NtbkNWWnloS3BTa2xHUSt5aG1MYWVKWk5PMDBzNXlxSTBvVnlCaS9rb2kwU2h4elozRG1IallmQlV5NnZDVTVIZDJEMW90cGFvUUJuMXB3dVorNi85LzZSVVdqTHRyUTg1TytydEQ5aE4zS3pXTDBndnlJcmoxS2hYYmZ5THRqNmErSjB6bUpNTVlQQXJMdFNkNW9sMXNXcnY3MTNYRjVkQUZFOVNxWFUxNzJzSk1SN2NqUlZlakZOTCtjNXZYMmJkNzBuUjJsOU1DRUVrazNucXFvdnpsQTNwOUd4T25tUnlvTmJxcklWbVhOcVZMcVRXcTYwbmhWWXRFS3ZFNjFza1dqamdINi9ySjg5anhQRHNHV2E5cHllYlNrWi9Yc1lHT2ZaanJXUW1jbHBvcFFKdHc4Y3JoNXdHYTFXWUlVeHJWbFlWMmhDRzYrYzE2R2lTYVZaZllIVHRCTHFkcGtjSjRhdXgvdU9lVHF3Q1IzQmRVenpBWEhDWUtQMzVubkM0K2tHbmVreXhnUE91eVZmTTdXUTJxTmVsVlpxODE2Mm5FaTliakthdk9WeWRTZUNkdjF5VlUyeHJUU0RVbFN3dFJPRUtTZGUrTWlIcVJvSDQvVWVvOG5pcVJ0b2FrYWdZYW1HM0Q5NSsrMmpmSG9oTWRvQ3JldXpHa0l6QWxzaGJWTjkwck5vSk9VeEtpZEJKMlhWVGRtc3NmSytzTTlxOGo4MHJMazRlN084S0Y1VTFVbG55N1VtNmFGejNMdjNIcDlPTTZRWnlaSFFPZVUwdVdEOThBWVQrNTZjb3ZHOUFyZWZlUlp4Z2VLRUZJVSthSkVLWVZIYWNJYmtPM1h4VWl2STVtbU9YUElhYWFzV0R3cENMQ0c3aFJtRnFrbGxYOUFXVUkyMUMzNGpGRCtyVUlNaE5rVUVYN0xxWjZHMW9wd3pjNm1jdHdrd2xadWdVN0JJMXJlRFZmT1RMcFNTQ2xzZk5HYVBVWWVOZWsxWXRUdTA0TGFPSEVlS0NKdHR3S1ZNVEFsWGlsSHFyVWJRQjBwUnBjb0U0SW9XNnlxY1dVcWorTFRXYkR1WEdiSFJKUm9HbFNKSHFpZVZDbCtzVmJlR1Bjb2xkSlkvYS9oVnoyZ21nZTk0L3FVWHNYbmNKSnNwV2RlNjk1NFVKL2JUeU9BQ0hWNm5RZ3U4L2VNZmMxWlc2emF2Nm1wUHJQeFdsREtBNHZpMmNNdHFDTFdFOHRvUVhaOG95MCtsQXJSMUpib1FYWTFUWFQyNGhXL1ZDbzEyQVFxbU5wa0tuUk11SDk1RDhxd0s2U1JDQ0J6R1dlY2c1aXB0b3pNNURsRU5DZDh4M0w0RG9hZmtqaUtqUVhwMk1wdVNKQ3VEc1VYT1loamlGTWtwTnd4Rno1dTB2d0FrMUlWUzk1SXFhaUdXajJqQnRGYVJ2WU41bnEwM0ExSktkSjBuNTZMTVh2VGlpMlNGdGx2bHVkQjFRa1Q3VDdJWXM1NGErdFpqRW5LY0lLa0hVUXdqNFozSENjU1VDY0dUbklWTjV1SDdybTlRZUYwRWRWdG8xN2lJZVRRTG9XMEtjT1ZsRlR1RWd0VkVTdEdMdjZiUjFCd3ZLVWlRblNiMHhlbGlzcmNtaTNvSlJkNExrY3hCWUE2ZTAyZnZRdGRyRzBHOUV0WjNjaGl2Q1NIUTl6MDlqanp1aVZlUDZjV1I5MWM0MjNncHFxNWZaRkFncG9hVHJLSUpYWnJxVHdwR2s1R1dmK2xadVhFejVaN2d4Wk9hRjFHWDZuMm5RZEdxcWNhM1JhaUJwOVFaajhIcjdsUUVmQ0NiR0lDQ0ZwSHQ5b1I5U1d5TGgrdExVbmlJSDdZdzNBS0p6SE5pMC9mTTQyZ2VJclcrK09JOHNqdmo3RU1mNXZKSEY1eHNUempzSDNOeXVxRTRQZGxTRGNFcTZCVUpxOFZIWlF2WEVBREQ4eFUvVnhpMG5rSmJWQlhFYUdMaTlXd3ZPMld4RlZSRUtIaDg3L1FNbDZLN25WRXZ5Q3JJN1gyeGhqSE5kZnhxMS9NNEZlek9hekxnZ3Q5TDBUd0xBeG1VQWVUTThGWEpIZ3BkNTlWUXNuNnYzRHlzVWZKWHU2aXMzcnRTMUVzRzhhVkJJQzF4cDZhQmxVUGxiQUJVSmtuV1FVdEpXUXpLVjNPV3AxaWhzYWpvUkU0SmNiMVNZUXJNT1BZQ0p4OStBZmZNczZvY015ZHJQa1BETFFyRk94QlBpak11RktieG1xRVRlSFJKZjlnVHBtc2tSc1I1dXE2M1kxMzY3aldQcm9tVkZoRmRjVWIvZCswNnFrZlNkWmR5MWQvV0ZlU0NFRnFNWjdOSE5CRXZ1cU0wMTdPRVpuWGtnek5FQTFoMkQ0UTJEYXFLNmdHa3pPbG13K04zMytMODVFNnoyeEE2VWh3WGFMZ05rWEdrWEhDaGh4VDR5TWMveWF0di93QWRvNmZIRVUwQ0tiZ2xYSlI2cEEwcUxzMHIxQzdJYWc2dXdkTDZqQnAxMWQ5dEJ6SVVzS0VxMWZQWVIyVnoxV0s3bUhxYTZ1bkZZbmh3ZWFGSnBCV3Z0ZFlsaXRQRjNLamtlVlV0cUFoYnNiREJMMGhTSmVZQ0dvNDVmMFFoYndtdEdVclYvWlgybUdpVjI0ekZCWDJmYlAvVzkyMTRtKzFEZHI1Rmk5RE9vZ3VjTGN5aUVMRERrYktPLys2R1RsRTJlaUpDRE1MRkhQbjg1ejhQbXczc1RwZ1BCeVdLeGtnWGJHSmJLb1RlTWNZSndvYSs4M0NZdVBqZWQranp4RUFtaUk3VHFONDRONWhiQ1oxYUJGZjA1bWxoRjIwRmFEamMrSUlpT3BYYU8wSVRzM082V0YxTmtDdXM0K3B1Z29WVXFnZGN5WWdWYmFwTVhyM1BqSzZvbTgwb3grbnR0MzdFK1NjK3F4WFZiYUhmRHV3dnhvYlA1NndUcGFvaERKMkhTYmo5a1kvZ1QwNjR1bnlmYlJlSUpSSzhJTTdoU3AxdmlBbEptTWVyM3FVdXlGVmhVYjFHblgxb0o2N1FJR0ROUDlyZExjODV5bTlhcUNrMUI0YVZFUldyR0xiRHNQTkQwWkdTUis5amk3NTVLak80R3Q1Z2hWdk1XN1JMdTRUZHJmOWpPZWdiZjdjd3pMNXhydjUxNVhtV3JVWTNoSlZIV3BKaVdzWGJaWXpzS1loWE5uZ1dvV1RCNVNYZWw1eVk0NngxSndyU2I5bW5HWGQ2eHZtTEw2cU9jOTl4LzYxMzJPU09FSlQ4V3VhRWQ4TCsrb0xORU1ncGtzWUQvV0htN2RkL1NDODY3eWFva21QTHE3eFRGa05qbUt6UHgxTnVVcTladXlaMUl4RHJzckp3dTRwSWlFbU8xdUppcGQvclRtSFl4MXFOc1hrWTNVTGI4MnVocjJpRjIzc2h4NUdMQi9kZ3VxTHNIME9LT3BLNllTckxRUzZDM0Y3N3czYzdudi9ZeTh3dUVEWmJwcWh6RmlVdkt1aE5EYjJxRUlvWnZoTlRsRVNOWHFRTkYzSW1hbGUxbHF1ODZUSmhER3FYWmh1cVpPZW1LbWFLVGVxcWc1ZEUxR0NicEZNRlJnS21JK3gwRkZ3ZDFPUnJMNUNPcHFqNnhwVWh2ZnlOeXQxNndkbnpwTlBYaXhmNzdkcGN6ZnIrRlFXcU14M3I5NnZOZUtDLzlYd1hkVGwyWEhqYklIMUJ0THR1ZVk3VHovUmkwSTFYUVhTZHpwd2JtOXM1aUVXbmp4V25RNWtPY1dSUDRlWFBmWmJkYzgvQjdYTVlENnFUc09rUUtZemp5REIwNURSQ1NReDlJRThqdlFzZzhPWVB2MGN3L21CVkhOV3hjN3IrUkxnaDgxcmR2UDIwNXJkamcvRU5BYlJacERibU1HVHJCOUFUSmJwalNTMGsycTVDWFNDeTZxdGZGblpscWhXSkNwN1lVeXBhMWdYUEdDTmx1dUxxM2p1YzdHNlRwMnZjZG1kd3JXZ01LN1J4Y3NWNVVpNkVib0I0eGZNdmY0SUg3LzZZOGVvZW0zNmdxa3ZXNXBwMkxQWWpmcm12MWsrS2FNR3dWZVRiN215Wmlaam1yQ1UwTjJubGdpdzFsSEwwOHBXaDI3L3JTYWpuc2hRamdHb00zY0lhTVhDa2VoTExnMXl1dTcrK3B0Ukt1YTh1U09ITnBsV1doYXI4M21wZzBKclphcm5KR1lXNjhmWXNGTE1FVHI5eFdmeGpMc3M1VUY2YVZlS0xlUlJ4RnRYVjUyZzQ2QzNpS0NVeGJEVEU5aUVRUzlMUWM5ancway8vRER6N0hKeWNjcmg4cE1oZVNZb0Nra2x4WXROMzJwOHlUOW9wTzBmeSsvZElsOWY0UE9OeVFzVHJBcmRyLzRGZHJxdEx2aEFwbDVCVVo2L29WMmxJV2lta2duYU9GcUNxTTVMRlFpdFdKOXdNeGkzaEdCVXRxYjBxcmw1d2RlSGVkWGgwZENxUzJYVENOTSs4LzVNZmMvS3hUNVBpQWVlS2R2REZiTzU4bFJPSkVIT3hIcFNlL3M1ZGJuMzRSZDU2NVYyMnU0RTA3L0V1TkZLYk5GRUphVHJJYmVWS1pUdGp1SGc5RzZ1VDV1MHNlcnZjRG1nRXkrUFhpS2xGTGgreDVCWlV2bHBkM08wS0tYTFVLditOUldId2FrWXJEUmFDS2ZsV1d0aFQ4dklaRGcwcjZvWWwxSXRySVZOZUxlNVNqMmVCYzUxb1E1YTA5elRjUCt0QmF4Rll4VVVxUEZ3VlMxcU9CalpjcWhadnEwRldpRG1pb0VraUoraTZnZjFoNXJvVTBza0pKODkvaFA3RmwrRHNsREx1MmUvMzVEZ3p4WmtPeDJiVGMzMTFRYjhKeEhuU0NXaW12ZkRLMTcvQldSL3c0OEdHUUZ0RWt6WEQxUEVRR3ZvNUkra2ViV3lybTg3dVdXMjBwWjVodTY1VVhhOUdQZEdGZXNUcWxWcUZ0MUNqL1N3WFNTK3FCYjVXVWEzUWJlM0dLMFg3VnJaZDRPTCtPekJlNG5aM0lJNzBmY2RodnRZUUtSZHlLbmp4bEtMdDZZakhEVnZ3dDNqMnBVL3c2TjBmYzNWNW42MWQ3TnJDSE15SVJiVENXQ3o4a2JhSy9ISXlLMzFoamEzTEdnRmJrdUNieGQxYWFWNjN3ZWlUN1BsMTRSYnpFazBRd1RhQm1rdlVYYnZhb01YU0ZXRmliUWhyVzIwSFl1ZmZnQXJucE9tL2FkNmpUODZOUVd0Z1NkSDdHc3pmUEptdGwyUk5XTllTVWRHMjBxZ3BXRDZnWHphcHFxQjluWXpMQlVvbUk1U2tnNDAybXgwWlQrbzZzai9sRVlGLyt1dS9DWGVlZ2UyV0IrKyt5enpQOU5MUiswQ0ppbjcxZmMvVjFSVzd6Ullkb1RmaCtzQmIzLzBPWi9zRGdVVG52SDVtYlVjUWFaRmhaYlN2TjhUankza2pzVjhqZ3dhNGVPL3hvY2ZWK2V5VjExSUhBMEdGaHVzRlhSa1F5LzNMdjVlanFSNUJ4MWNIalpOTFpPZ2Q4LzZLNmZGOUpJM2t3d1d1RnlKYVlNeVZodUJVck5vVjRUQmxZbll3bkhEeXdrZDQ4Vk9mNVJvSG14T3k3NW5GUm5ZTGJYaFFjU3RFVGxBamNXb3NZbkFqemVzc3RhQkZOMENldUcrSjlZMU1hTE5ibkZQUk4xWHJXYitPUmREUHJkQkNUNlAxS0ZJazlqcjl1NzVPUFBpZ2ZTeDRhZk5SNnF3WThXWmwxczFVQjlIV1VSaDFkdVV5bWF3Yy9idXErcmNwYVRWYXNCRWJ6cnBNajdoVDloejEyR0xuVUQrdnpwVXNJZXVQbThnK2N2dlpPenphN3hueHhPMHQ3aGZQSi83cHI5Sjk5R1Y0OWk0VWxaVGRoQTVKV2c4WmdtYzhITWc1YzNKMlR0ZjF6SWRSeGZyZWZaZHlkVVdRckxKS1VvNkFtclZDWk1zM0d0QlJnUnQzN0VXY0hLK1grbjFaeHAyNFZKYkV1eTdzdFZIVVJMa21RZXNSWW1LZUF4OW9FN244SXFOYVk3d1FIQ25QU0pvSkx2TFc2OStqVEplVXVJYzBjbloyd3RYK2tyNVhxb1ZTeURYV2pHTWtiRThoUXBHT1oxLytOQzk4NnFkNU9CZEczK0UySjB3NWs4VVJpY1F5bTQ1WFdSeWdXOXFTNnl3UDFhYzZUb0JwNEVWNTRrZGNPUUlCNnIrcjVxNHBMTFdkdW9XaW9nWWhzbDZveGtpdWk5ZG1vcmpPNGJ1YTdDL0hKQjd0ZWd4cU5CSmNNeHpzTVJmVU9OYXYwL2t3cFgyT3JJeW5MdXgxMHIvZURJNEswQ3VteG5vNFZCWjB1cHBMdWtFRklJaisyMmY4eG5QdjhpSEQrVGtQRWp4d1BmMG5Qc2xIL3NrL2hSZGZnSk1OaDRmMzZVckM1Y3pKc0NITkU1VEVicmZSV1pvNHJxNzJwSEVrQk9GYmYveEhkR25FbTFUUmVwTmVXbnFYZng5dDR0Q2F1WmF5eVBGejFZTUt6blFLS3JnVXhIWVJ2VU5kWnkzZ09jczVXdldncmdTcmROZEUvRVowc2lSM1RtVkhTNTdwYmNaSHZqNXc3OTBmOCtHcmgzVGJVNklJWVhmT2JyZGhtZy8wL1lCa0dBOHptOTJBT05nL3VtVFlkTGp0R1pENDJFLy9JKzYvZjQ5VTlqeTZlc2h6ZCsrd3Y3NUVVdUZrMjZ1WWR0QmRNU2JyalpGSzlMUkNXQ1B4SFovTW15elVkdko1K3MwRmsrNEVKUTRXMXh4NmhiMzFZNWIzY3lLVUd4NDU1MlVocWt1dWRTaDdmVGF1bEZXNGF4emhxa1pTc1JBWkpYTzI3cjFjdjZPemtNdVFxMktjcS9Xc0Q4dmp4Q0JwbDdYT3BLTGhadmhsQmRia2pCT2IySld6Q2gwV1BmZEZoRGxIU3Rmek1FWFM3ZWU1OUNmODJ1LzhEcnowRVRnN0pjOEhyaTRlY3JyWmtXZmwzZlhPTXg5R1VrcHN0MXRyRFM5c1E0RExTMzcweXJkNHhna3VSWTFXcTlpN1VZMEVRUk00dXk3dWlYanJlSzFtYVNGNnZTVFZnQlFJOGhiT3lkS2NzbDRrdGRDbUYyV0JmdGN3TWVUbWpwZEtLQWFiRnB4VGNZU3U2NmlWOHR1blc4cDQ0SzN2dmtLOHVFOFpyOVRkN25xOEYxS2FHY2VEZHNYRlJKcG12TzlJV1NnbHdNazUvdGFIK01YZitHMGVUWUxmblhQLzhrQy8zZEh2VGpqTTJ2V21STHBJRnh6QnF3ZXBkRzBoNDBVOVF0dFZWajlpZzRhTy9wYmpIN0VkK0dobnY3R2JMMkdRUWJoYUdOYU5wSG9HNTR5OHVReVliYzNoM2kxZXA0VTd4NTZwd3NVYWF0bUM5M0wwdWhwNnRlT1I1ZC9MVUZ1N3RsSmFOYnQ2dmVQdlZBdThDdllVSWs0eWZlZlloS0Roem15TmFDRndDTUs0Mi9JVE12L3N2LzF2Q0I5N0NXNmRrY1lEais3ZG8zZENWMkRjWDNPNHZzSUxiSWFlTG5qNkxqRHZyd2dsZ1NTKzlTZGZwSXQ3QmlKbEh1bjdKZnE1K1hQejlrSDNWN1VYMjQwV0ExcXRDd3V4M2FvbTROb1lPTEN3d2s1dXJZZ3ZIN2dZU1FNRUhLMnVVdWt2dVV4VVNhU1VaNEozREM1ei82M3Ywek5TeGdzNFhLSzZYMUV2aHRlZFFwd29GRndLQ1NGSkFMZUY3VG1jM09WWC9yUC9JM3M2K3JPNzNMdWEyRWR3L1pZeEZick5WdXRDTlhRUU1mQkhjYkpTdzhsVmZuWXpOem42dmkyWFdQMWVuYmQ2MzlQeW5Cck8xSnBKTlRTazFoLzBwMzVHWnNrSHBHNWdiU09xeDFDVG9OV0dXYitMVzQ2ajFrUWFzVlZvMXpNTE5zMUxkWlBYdFozMTk2MTF0bVgrVFYxQUJmRzZLYVdjaWJtQUJBZ2JrdXU1U0k1cGU4YUQwUEViLzgxL1RmZmhGK0R1cytBOTF4ZVh5SlRvbkpEanpLYnZHWXlmTms4cUNqaVBWM1FrZW9sdy9aanZmK1BMM0I0OEVnK2NiZ2JtY1craHRJWFVOZnl0YTlEVmFLRUZpKzMzUXNIUExUeHU2OTdXZWxuVjFJSzRZQXZLTG9CM1ZNMVhhZ0hTT0ZYRktjbXZOaXNJUnFEVHVNd2cya1FtdG5DbGM1NDRqODNyeEhSZzAzbFMzSFAvaDYveXpFLy9Bdkh5QWVIdVFMZnR1YjRjR1laQnVVUnBzcUtiMHJFdnIwYk96amY0c0lQVGdRRDgwOS82RjN6MUwvOEkyZDRoK2NqMWVNbnA5cHlyOFJxSHFPZW9TYkxYWXBhaVBLa2lnVkFyOFI4VVg5VmR4cmhXRlc3TTlyM3RhaGhTYmtiWElNR2FLM2txRzdsMmtJdElRNkxzamlVMGE1d3lVMk54OVVrTGo0bFNHcmZPR2NTckc5OUNDRnl5VkRzV01NYUFoWHE1dHZLdTJtY2J5b25WVk9vUjAwWXZ1R284V1pBaGtMS1FaZzNsaW5qRzRMaDJqbkwzT1g3cnQvOEY3dTZINGU0ekVCejU0b3F5bnhqRVE0b2NvbTZnenV2bmxWSVlOb3F6NTh0TDR1VWozdnJyTDNOU1ptU2M2RXBDY0F5OVI5TFNjTFlVRlA5aGVsNXJSS3pkNm5RSEsxampIZGtYd3B4VXlCam5TUWFLMWFweWF4VjFOUStwbll6MXdzbUs2eU8yS09vbkZ4QWR2Rk95UTV3bmxxemhSdEcrangrKytuZWNQdnNTL2ZNdnczZ05wM2ZvcHBseEdobTZ3SHdZNlFnVXI4cjJ1N01kang4L1pqTUUrcUdIM1RQZ0hELy9xNy9OZDcvMURSNzg1SWVjOUNkTVpjYjdEYzVDQ1JGSUdGR3cwQmF1QmVRTmZxMkx0eTNncDUzY0dwOExiZkhVa0JYTGdkYlYzZ285NjJvMnBLM21qZnJpOWg1VUdzbWFxbDZrSWRtMVRyUU9JZXF3MHNvVHU4bmxxb2NpNWxFcnd0Wm9lTFpodEZvRHRGeE9GNU8wR2dtZ0JjeGNpUFlaR1czTlRuVElNSUFMN0JOTTJ3M3VtYnQ4NGJmK2M4TEhQZzVuejBJNFlmLytmZEk0MFltajVFUy8yUkl5eERHeTMxK3hDWjJON0V2RS9RWERBRnduWHZ2cVgzR2FSczc2Z01zUlNaTjJXVHJMa2kwdjA0N0tDajRCcmU5a1ZSS3dIQkN5TllSWUJNQnlmbXRZN2Z1TzdJUnd1Yi9tZE5PYkJGQTlrYTZxWDlwdVZ6UzJ0Z3UwRmhFVG80Q0lyU0JGZ2h4WlZDQTY1MGpmNzhnSWM5U2VaeWhzZ2llV3d2ZSsvYmY4ekRNZklsOExUanpkeVNtNUhJaHhwdStEemZPRGFkeno4R0ptZDNiS05FOTBYcEQrVkEvRTkzejY1MzZaZDgvUGVlL0gzK1hxOEJDWE1tZmJudUJzRHRocXhKbUk5cis0QmtaVXJkb2xSRnRYdDZIdVBzZjNxUkIzYVF0ZVJQUEk0aFp1bURSajBEbVU5ZjNiK3pxV1JXaWxDN3pZenMzQ2NLNXdEYmEvV3dHeGhYZTVlako3anVqbjF1bTJqWHhwUzZoK1BSZk1DSXpsNFpJYWVodnprY3pnYkJQTTJYSVhsSUJaZkVjbXNJK3dUMFlTUGJuRmh6LzNPVjcrNVYrQ0YxL1MrVEFqOE9pU2VIM1FEa1lSZk5mejZPS2FrOTFPRjdpRG1CSmhjTXh4SnNjUkJzZnYvdy8vVCtUcVB1ZmRRTG02cG5lNkxrTG55RWtYYVFNbExCcHlDTFNSZCsxYjI2YUdVbDJjZjZwWHFXaHVEYWxqVG9USCs0bnRkc0IxUFc2c0JjTTZMcXcwejlIeUdDc0JpMVQ0MEhZdlY5MjNkU2lpTWE4djJxR1h4TkVab2JIM0hmTzh4ek53ZFhHUDkxLy9EczkrK3ZNUTk4UjlZVGkvUlh4MHhmWDFudFBkRHNtWkdDTW5tNEU4UlRiYkhSZVhWNXllYkhHYmN3amFQdnZjcDcvQTdUdDNlZnYxVnhrdjczTTlYOEU4NDEybUM1M3FWeGt5bEhJaGtURUh0NUFGelFOa2xsREx6cDd1MkhiSzY2NnVDQlpMMWR2eWhrYlM4MllZUlpvbldsdUtLOHZudHcybkZLT2UxRmt5RmU2V1ZTRlJFMjBwenRpKzJZcm96bmJNU3AyMzkyenFNMFZsWTNXZWdubWRlajlRVkVLSXFxZ2lrTlpBaG5QbVliVFlHRXZIVmZFOHpKRndkcHVYUC9XenZQQ0puOUtxKzNQUHdYWURVMlQvNklwNGZVQVNwQmp4UWRuRTIrMldtQkpwUEhCK2NzcTBQMEFXUW81SUo3eng1MytLUEh6SUxlZVlMaS9ZK0EyRlRBZzZUVUQ4QUFSRnZJcmxKYTE5d3VEK1Vsc0pRWE9Tc0JTWExZeXJDdnNaSFZXU25VTkM0SkFucnVkTStNbmpTKzQ4YzR1ZHdHYXpJYzRqMDNobDlSVGJ2VVRJVHVzakRpM3lsRnJwcm9pSzdXQmFGZmVJQkZMMTRNNjB3WWphdFZkbXVrN3JNL044emZ0dmZJZCtPM0QrMGljSk8wKzZma3dZQmpac1NTUktqR3ljd3dtTTh3U1RZK2cyWEYxUGJCQzYzUTUySHNvWi9iRGw1Zk03akpmM3VmZnVHeng4N3kxeU9qRGxtWkFUd1JYNnJzTzVRaTZSMFhSMkcvM0ZsRlpFaXJKU1dSYW5FMU1nb2JUZUViR0ZXWTcyOCtPd1NyY3dLK0t1N3E3ZUFTdUxxOHF5YStFTllwVjFzalZzcVVXcDk3TThKR2s4YmNBdzlUQ2tLTVJmdzhocUVLWFZjL1RDYUdHWGF1bkVZdE9IMGNYbWZVZWhFTzJZTStvMVVpcU14Yk5uSUczUGVlbHpuK0lUUC91UENiZWVnKzBaM0w0RDRwbnVQeVRPSTJtYTZiekRlUjI4TkhqUHhXSFUwREZIdGwyblNGWW94S3NIQkVudzhEN2YvdUtmY0RaR05qR3o2emM0c1hFV0x0ajMwM3hUV2NKbUlJVTZGY0lBUkF2TkVDamVQS1IrSHc5UUl1STdZekY0Q0IzWmVaS0h1TzM0OGVQM0NPODh1dVJUTGpETnlyeE1TUnUzSUZrYlo3RmR0clFEYS9uSUtuR3N5RXdSK3pDcGxXODFwbUlzSzVGS2h5OTRpZlJlbU1iNy9PQ1ZyL0J6SDNrUjlvK1FzSU50SU93R3JoNDhKQ0NFWVF2enJBVEwvVFdFanMxbVM1b2o0NE05UTYrOTRHenZ3TEJqT0RubnhidlBjK3Y1bjdDL3VNL1Y0M3VNMTQ4NFROZk1XY1hVSEE2Y3QyTnhaRmVWQ0JOU0NyTUl4VWFmU1dIVmo0MTI4Z0grU1BPb3JBVEhiL3pPMk80bVZFNVhYZFFGVzd6WWhMSmlZVTNSKzExQkIvVlVRbUo5ZnE0SW9UU2FmYXViRlVVYkVzV0FGMHZJUGMzQWk3a3hNWnFSb3BjZEJFY3FTb21aNTBSTTZrV3k4OFNpZVVtMzI3RTVlNGFQZnZLejNQckl5N2piZDNWTTN1NGN1aTFNa2VuNmd2SHlRSW9IdHNOQWpqTmtuY0k4anhPYkVFZzVHWThySVRsUjVwRlFEcEFMZi9vLy9vK2NqSkh0SE9rS0lGa1J3cHpBQlEwbk1aRENlbXJXVlBSYVc2azNMMHB3bEJWN1JDZDRoZlpTUlFVZEJFZnBPdUxnZWVmaUllSDlpd3VrMzVEVHBTVWNEdThja1VMSlVRWElKQ011dDRSSGJNWFhENnZ4Y0lYVmxnTmRrc0lLTTJvMlhYZmd6T25XTVJWNE9EN211My96SlQ3NWM3OU1FUmd2QzhQcGJicmRocEpnUEl4TTg0R3owMXYwMXNBY3B3dUM5NUFqWmZaTVdSRXlQd3h3OGl5NHpNbjVjNXdjSG5IN2NFa2NMeGt2SDNEOStDR0g2OGZFY1NTTjEwaEp6TWxPdUUyOUZWT0pDYjVUV1ZHV2ZHSDlYZG9JeWJiZnV1UGZXVk5lN1M5SnRvdlgzOVdRV1BXVExKMk1UVm5FN2w4UXN4cFhtM0VWemJScThsNjlCMmcxditnWU1NdHQ3TEZWdDZZU01vdTFxRGhMVndKSkJObDBaTmV4T1Qzbjl0MW5PYnQxbDkzWk9adXoyM0IrRHYyZ0J0SnRJVHZZUi9iMzN5TWwxWkQyenRHRmpqVE9PREt1Q0RFbWdsY1dSa1UyYzRuNEFPbnlraEJuL3VMZi9qdW1kOS9qbVJnNUVVY3FFMzQ3TU00VDIyMlBuN1RWdWhUTkUydGhzVTFYcUxsajBldFNiQU1ybFFBcUN0YzYwMUZUTU1XMVJMNEVEOEhUbld5NWYvV1E4R2hNekU1bkJFcWVjRDVRU0ZCc0JJQ3JZNUgxOU9WS2EyODFGSE5qelVqV1JTMEZCdXF3b2hxWk5QaVRRcDcyRkRkejBtKzR2UDhXcjcveWRUN3g4NytLYzVFMFhkRnZ6NkY0TGg5ZUVEWmJNb2xwT3RBSHAvbEd6R3k2VGlmMlVwaWo0SEptZTNKQ1NUUGlPL1ZPbTF1RVBMTzVmYzJ0K1FCeHBxU29nNERtaVhtYW1PZVJIQ2RTaWlxMFZoSnBqdVFTU1NrcFpTSmxZckZ4YXlWVDUzYzBuOUxJbEtXdSt3VUZrd1dTWEg1YjBtem5UK3BDYnhtK0hMMTNSYldhb1JtUnI2VGp6MTI4ekdMSlhoek9oYU02RDhWWlViaW42MVRGc3V1MmhINUF1ZzJiN1ptU1VzTkdzKzB3Nk96SXpWYm5TQTY5anJzYkkvTWhNdTRuU3RSam1ISkdraFo5cFNUNnJzZEpvSlJSdys2a3JPQStPRWlSY25sRk9PbjU2di9uZitQNkoyOXltaE45bkpFTVhlYzRISzd4UThjVW8vYk14NlJUU1BNS3NYTEZpS2NxeGJxZ1hCcDIxUnlsR1llSVlyMEdqdWcxYzlyWjJBbVRSQjVlWHhLdWkrTzlpMnZ1N0RwTnVFTWd6bG56REFjaUp2emdpcUZFOWthTlNvRVZlR3BJNWF5ZVVwclJWRlVVaFpIcnNIdWxrWGdmaU9QRStja1oxdzh2dUg3d0U5NzQ1bC96MGMvK0F0NDV5bmlOOUNlY25KOVJVbVlhOXdZaUtDMWxqb2s1NjFpOUVEcFN5Y3d4Y2pnY2RPU2VDRjNuQ2QwT2drQjNSdVdlUzBxRTRpRE45REdxam02dStZUjVpRndhS2xiaHhEVnhyc2ticlc5TC9HTy9uM3hLZStwS1dWRVg3dW85Vm9hd3ZLRCs0WmE4c0RLRDYvM3Q5VlFZVWkxeDFjclFOcTRLYXhzOHJXSUxEcnptbVJCMHlHclkwbkJrNXlFVjBqaHgrZUNCSnNyalRKeG5laC93d1NCckR4SUNlWjRJempHT0k2VWNWRmlrek96T3RraWFlZnordTV6dmRraWFlZlgzLzVEM3YvY2E0Zkl4Vy9Gc3ZXTUlDdTEzZzJmS2ljM1FnL1hKcXhpRzZoRzRwcHJ2R2tyaVhOMlUxWE9LTWFlYnVvNjRvK3RUYTRtbDg1VGU4M2pjTTd0TW1Mc3RQM3IzSHAvODVMT3R5TWhzRjg4cGhsNXJKN3BBOUFJSk5DWnJnV1gwOXVJNEdybE9LbUlqSUU0WGVMYS9jOHAwWGVENjZoR2J6aUVjZVB6T0c3dzdiSG51MHorTEZCTlRHMDZKT2VLQ0o0UWRPVTdFT05NTlF4UEZLMGs3SC92UWthSUtOOWVpV0p6bnR0c093NEFiQm13bVhsdlFHczJZOG1FTmRrdE5qSmRGSnMxMTIyS3VDL3htZXZKVUk2b2ZKSlowTDdBMXRzTXZ6eTNINzNIam9yWkZYc09KK2g1NjhsZnY0VzY4ajczR2lxMlVRcFVlVlc2YVZHNkhJb294a3c4ejh4eVY4MVVLY2M1TTA2UXFxaWt6Qk0vZ0IrSThNMDh6UllveHgzM2pyOVZaTDk3cjFMUWNSOHE0NTN6WFE1cjV6cGYrbURlLzhRMU80OFJ1MnhPdVI3eW8wa3kvR2RpUGUvck53RHdkTkQzSUNlOTZLMDBZOGRPdWx3SXNtdEZYWlgwMW9scU0xTUprQTNGWWpFUjZUK2tkcFhlODgrQWRVaStFNUFkKy9ONEQwaWRmb1BnQWFWWnhoenFQUTN4TDFzR1R4UEFReWRTaG8wZDFGYW05RGplS2tFNE1vTXZXOElPNnZNNGhPVE40MklRTlU0NlE5N3ovK211TSsycysrcWt2NElaYjZLanFFMHBSbWFNNEY3cHVVQmpiU0p3cVZ3T2Q2MVRZd0dKNGNvRlVLRmxEcHNPVUNkY1RtV1NGVlUzVTY1QWE3S1MxSEtzVVd0OXdoVW1MdCtiM3N1enNOdzNseUVCdTNORjI5bnpqZVgrUG9WUTF2L1llSzNKbmU4Nk4zK3NYbEFJcEhYbXFuTFN4S3BlNGdxTVZmczQ1azJOcWFHY3AwbnBGSExEeEFWY2NNVWNrcVQ2emxFaGZLL1ZwdHVrRGhmMDBjVHBzMFZhb3pMWUxrQ2J5dENkZlBPYmJmL3FudlBQS0s1eW1TQjhueWpnUk9nL1o0NE5LN3c3RFFFNlpUZGVUMDZ4TTY3TDAwN1NrdmhvSHpscEc3REs1T2hiZUJxNGVYUTY5cmk1NFNoY29tNDR5Qkg3NHd6ZVlpSVJaQnQ1N2VNbEJoQk1KWkFlK2MwUTdRV0xqaHpYbk1NbVhDdFhYamJRV3ZVUllWNnYxSmFhQ2FKMlExYWkwVm1GQXY4WE1jZDdqbktkM0FTK2V5L2ZmNEVjcDhiRXYvQlBZeitiWmVyWm5aNUJoZi9tWVE1d0lUanZ3TnBzTmM4NWNYajJtN3piTGNlVFM0TjVpa0trWWc3alVOdWFTdFJzenE1dXZLRmNOWlVxbHZLekFDUkVoV2h0MURjbHFZOWVhaVhxVWM5eTRPS1hVc083NHdqMzVmSG5pTVZmekVSS1MzZEhudHVmbDVWaEwwY0pjb2c3bEVlMk9PeksyWThQVnlWYkdrMHY2V0cvWEs2R2o0ZnJnS1drbXprby9tdWVKOFRCeGNuSkNUalB6UExQYmJnZytrTVlycEFqenhXTTZVYVA1Ni8vd2h6ejQzdmZZalFkOG1na3BHV0ZWdzk2VUJXM2FaK0hvclFxSWxpQXY1OGpWeHJLYTJDL1BXelkvKzM1dGgwZWhjdytsRitMZ21Idmg0ZjRDNlIxaEtvRkRkcnh6L3pHbloxNkxldnRKSzdhMlV4WkNSZFhObFdaRW9vVWoraUZ0dmw5TGtsZ1FoSkxhRHJxTUd0Q1cxRHJNS1plTTcyd211Q1FrN3hsSVhEMzRNVC84K3N6SHYvQlA0RHJEeVYwNFBDSmxyME0rUzg4OGpjU1l5Q0k0Q2ZUZGhqNEVuUmhyUW1xVmUrV0tqbmR6Q0Q1NGJKcWtmcTYyWkZJYXltVzVWelp4T1p0WFgvVzVFSVUxMTRaQ3RuRnM2emt3TjViOGtSR1U4dFRIUC9BMVpmVit4bHRUMkxsMkpOWUhyWjIxaGxidFVvbE41aXE0N0NCbkc1bFFQMGVzb0dvMDlVNHN4RkxpcEJZNkhmTnNpdlFDSlNlQ0NNTXdrT2FJRjlIaTRUUWhlTzdjdlVNZUo4YjlKUjBKcHBrdVRuQjl6WmYrbC8rSitQQSt1NXpaQlVlWEMxMkFQbmpTSEsyWTdFa203aWNFU3NtRTRuSFdoYW5IcTBJQXNxYlhpMWVQazUzQzlxV3VSeXRsV0VCUTBjRGNPZnltWnhvQ2FlTjQ3L29oVitsQUdoekI5VHR5UFBESzk5L2s4Ny94Y3p4KzkvdWNkUjV2cm5QZGNGekVhK1ZTcW9xZ1RRMnZvWlJVWWlRTHlaTGNyTFoyK2RYbmFpMUdwd1dyeTdmZWo2SmZYRkEwYXJ3c2ZPc3Yvb0NQZmZwbk9YV0o3TGY0L3BSNHZjZDNQZDJnWTdiSGNkSndJY0dZRXNNd0FGYkZUaEVybE90SktVQ01wdU9rL1NDcmJBRzhiUXJaR0FxaWh0TVlDemd6cU53V1o5dk43ZVFEUyt2djZyWTJBbGRqNXBYeHRDRkhxMXRaR2NCeU00UEZVNUcwZXRGckhWcjdVakllcThzVTViYVZiT2ZZaUlqNlhIMDhzK1FzOHp3M3BDeWxRa29td0lIbUhCbjlkeXlaVGtRTGlERWhKWEt5MlpGalpIejRVQ3ZwKzB1R2t3MlErZUhYdnN5clgva0tQSHJNN2VCd2VVYml6SFpRb0dDZUovcCtPSTVZQ3pxRXRqaENMbzAxb3R3MVpVanIzWFdUcXV1V2hvclZEYTcrTzR1UU1ram5LWjFuOHNCSlI5eDIvT0NkMTVtbE1KRWx4Q1NFYnNNYjd6L2czblhpdWZNNzVNTjk1ampTZVc5ZFpJSllnMUNqbDdQVVZlcjlhMCtDaU5HY0Y0TnAzc2JDblFvSUNMQ01JWVphUlhVVWJwOXZPTXdIb3NEM3YvTTNiTjUrblovNng3OU11YjRnYkc5REVmWVgxNFRRTTNUQktBM0NlSmpiSU0xU0NoS00vZzVBV2xRYnhldnU2YzJnYW5JdFdXSGlGdDdNbHZlV0JxdFdvYis2eUdBeGxob0QzK1NNM2J6bDlubldyZGZVUHZSV3UvamFXcmtSa2psVG42OWVxUDF0eDZxR2FFU3VWV1dmb3IwdXVkaFFWMnpjbjgyMnJGOWswd2ZBa2ROTXlXaHgwRG55SExXOXg4NUhDSU9oZzVGdTZQRTRLRE11UjNwSnpKY1huQVJoK3ZIci9Qa1gvNzljdi84ZXB6aUdBTjAwY2JMcENZTlhOUmJ2R0U1M3hDbTJzQndLdnBMVnM1RVdzNFcrZGNpUmdUQlpuTTFaMWsybkZXUHJ4U2xLc1RLOUQ2S29GcGhzT3NvdVVMYWVhWUFmdnZjbXNZZmtJV1FYR0tQak1nWGVlUCtDVzg5M3VINkRTN01tdSsxQ3F6UlExYlo2TWppZ0lWNktnQmxsUUZnU0t1R29sYmhXNkpVTmFEdXIySzZBN3Q1enZNUzdEbkhDV2RkemZYaVBiMy81UC9DaEZ6N0tzeTk5Q3Rpd2NRUFNBV2xtbnEvcE5pY00yNDVwbkJwVVdSZGxTcEdDcXVsN2tUWjV0aXFsZTJmOTlMWFkxNkR1bWdqV2ZLeHV1cmt0ekp1M2VvR2Z0c0RYYmFwRlZvdTdGU0xORUtyc2tzbDhPcXRodW1hUUJmTHhobFVMa2kwUHFkNEUybmt1elVCcUdGSmFjYlFhVWkzK3Q1MjVaSnhUWm04RlFIcVVOT2pJNFBYODVSU0o0MGh3RHNZRGd0REhQZC8rajMvSm05OTVoVTNPM0pHQ08xeXp3N0haQkE3WEY0d3BjdnY4RENnY0RnZDhxQWlWeWpjNVVUYUZ5REpqc3o0T21lS0Noc3NWdFN3QXFtbVFiYS9RQ1dGTDM3YWl1bzRjSExMcDhlYzdybnJITzRjTDdrMVhqQnNrT2lGRUJQRTlCemI4N1EvZTVETWYvaXg5R1hIRGhFdGpvM0FVUUx5cXFWUlhwbUNXTmZuWUptdDN0b3RWUTRBRkFiTW1vcGJQb0ZRWXU5QTFsQlBSdVNUQmU2WTVJaVhqU21MYmRZelRudmZldk9iZU96L2loUmRmNXV6MmM4aThnekRRaFMzTUNjS1didE14emFtRk1xcitubTBSMlhHaDhqemF5bG9zWGpkRXFCUktpWTNvbUlzTzluRVZwczFheEJPamdSd2JpS0tESDJRZ1VGdEZzc1hlMVFlVm85QXJWOWpXOG9vS3JxbUhzRERNV2YwRGk3ZXBHNFBsZ1UzVDJCWjlmZDVSUVZJZ1MxT0xvYUo5NXFsU2ppMHlxTWZrbmZiZ0RFTkhTVFBqZE5CclZnbzVYY09ZNFBLYTExLzVGcTk5N2F0MDA4U3pmWWRNTS9sd3phN3ZtUGRYek5teDNRU0M5TVE0YTJnWFBDNEVyZkJubFVnTkJiemxqOFdyZ2VlNGpscHFoYjRzSExuS2txaXlXaExNYXF5YnRJb2ZiZ0xzQW1YWFVVNDdYdnZSZDluN3dteE5kV0ZLa2E0Zk9CeG1Ybi8zUG85bllmQWQyOUFoUkF1T2w5cEM1WHM1ckUvQzFjbzdTK0hLMWZqUFFnOUJFOC9hZ1ZkRE5LQVdMcXVYVVhrdGFhcUdNYzdheGlzMk5MTWsrdURJNVFySk0yOTgveHYwMnpQT2J6M0xDeC81T054NlJ0VW8wd0h4RzNyZjI5ZzFoM1FnZEJwVzVHaVNtMnFra2gwSmFTcm5PbzhEU3FOcUc5ZkxpbmI2dkxvQVZieXZHbG9DMDJiV3hYOUVCMnQvSzU5VjE2OWV6Q3daVjF4RHBSYWowNWY1aWx3MXkyRVIwbVBkQzJSSW02akhXYWptbUllMENjd3JHM1lGdEdCdHRBNHMrZ3BCcjMzTUdCUUlTVU5YU2xSKzF2VVZsTXhHQ3BRSVVoamZmNHR2Zi9uTFBIenJiWVlVdVZVUzIwR0lGdzhaZ3NkM2pqeVBuT3dHWXB4MEl4U3ZBdkY1WVQ4RTAyN29NdmhpTGQxVWxxL21LWlUxWEF6aEt3WVBtNyswVWxlcFo0YUc0aFVWZWk4dTREYzlzdWtaZTg4WTRQdnZ2VTNhT0psZEF1Y0o0anZta29rbFVEYTMrUG9yMytXMy90SExlQmxJODVWNjZsdzA4YXQwbFhvQlpibEFMUzlwT1l5dyt0TisxMFRmZWhzS0tPVzVKbHQyQ21vSVYwcVRHUzNBSEF1aFJQcHVJT1pDekNPbjI4QWNIM0h4OElycnk1OGdic3ZaN1dlNTgreEg4TU1aRXJaSzhuUTlSSWZwbWlxcHptVktucWlGSnUrQ3NnVW9wdVN5TEVoZGk0V2xZRmc1US9adkF3cld2N0hVN0VadGZYWHp5L3NYZTNKeGVNcVNwNnhtRUI3ZEdncTJLaXkyRTA3ekJ1MXpubEtRVktPemYyZDdmczdMMzZVd1hUeW05NEZhWUsyMUtPY2NjUjZWRGw4S3BFaDYrSWpYdnZrTjN2dlJqK2dsTTExZGNKSVR2VUFnNGFiSUVBcjk0SFZVUm5Da05PR0RlcVpVdEhWUWRVdThqalpQa1ZDZ0s3UTJpQUtxcEYrTTlkRk9VWVdNYTYzUHFmeWoxZTRVS0xMb3AyUXlEdDhGWk5QVG53eVVrNTc1Wk1NUGZ2SW1WK25BSEhyR25LSE1CSzhERTNHOTQvN2xKYSs5ZWNFdmZ1NFRkRjFITit3bzgwSFJub0xGOGlvdWtXVzl1Q3VVak1hQWRiY1RhNDZ4ZGRXcTh5TGFzMkpoV0ZVYjExMVVWMW15NUZOY3RYN1JnWjFkeHp6UERGMVBYNFNaR2xwRjVubkV5WUdMZTNzZXZ2Y21ZVGpsNVBRT3UvTTduTng2Rm9ZVGxPOGpFUFc0SkFoYWNmTnRZU281VmVuV3BGcXdLaXRET1c0MTFScVJMdGgxcS9DNnJySTJsNmZWVk55TnUxb0YrUU02TGNsbE9aZWxObkt0eUpoSno2SmduTDNXM2VmMGZwdWFwV0c3MVhJc1Q1RmNDM2VGUHZTUVI1aDExcVE0cjNOY1lpUSt2TWVEKys5eWNlOGUzMy8xRmR3NGNUNXM4TmVQb0VSdWhhQ0xMR2VDZ09zMENzbGxvdXQxa0ZUZmVYSlNUV0tQSjgwWjd6V2NuY2NKNzlTalp5TTdLcDVYV29kcEd6cFUyZzYybk5CaVhFUWI4MkFyU1VFTWE0SHV0eHZpYm91Y2JvaW5BK204NTIrLzloMVM3MlF1Q1lLK1RmQTRwbkZFUWlmU241WDN4cEd2L09Bbi9Ocm5YMlRubFA3Uis0RE1FekhObkF3ZCsrbEE1MFNMZHlaQlVxd0lsRnhwMExEeXVzeUN6UUJjUmN3VVdJVHFRb0hzRnNNRHYxVHduU1BJWWpCOXIzaCtUQnAzK3VEeHp1TzlFR05rbmgrUkNUQWVlRGcrNFA2OVFNSGhmRSsvMlhGNmVvdXo4M09HM1JuTXRsdlczZGlwcGhPcG1BZHFLMWQvcXRYWCt4cVpiamwyV2Y5T3lYYkNmMWdmZDd0Vis3QkdybllUYVFZcjVrV2sxQTQvTTRpNnM1SnR0N1g3aTQ3QkZsRkRrbXlUZFVzMURQdmNValRQcTk0c0pxYkRnY2NQSC9IdzNuMGVQcmpIL3VxYU1zLzRQT0xpcE9QeVhLS2JKM1k3UjU2RUVyV2JNVGlQTTJhRXJ3SWJRT2M5WHZTSG9pUHdmRmRKbmduWGErMGtaZzFyRlRDdG5yUWFRcUpwRFJzUlZKeXVNRlV0RWxKTUJMUW5QNWRNaW9BTFlGT0ozZGtKOFh4RC9OQVpmLzNqMTNnblh6RVAwbVM4U2k0U3BtbFNCWTJZMlc1UEtISG1hOTk3aTgvOTFFZUpPTzZlbkZHdUgrRzZydEhSaDJGZ1RnZUNUWlNxOGNXU3BOdUlzSldCVjhPcHNMSXV2R3hjc1dVaGxGVWlLL2lWQ2dsTE1pc0Y3enRjcHhLczJncy80d1Q2VGpzcFN4RndTYnY1akdhZDhzaDg5Wmg3Vis5eS8xMnZ6VCt1SXhZb1dmQkJDTDVYVlJMYnFZZGhNTG1hUlJySE9XYzZBMnZ1a0IxejFjdXlOVmRyVE8zeFpPd0VRN0Z1VWxodWVwWmMwZUduZ0FJS3NsVEV5bUR3MWQ5ckpuTkpKaGhuaVgyT3lvU2U1MW1aMFhOVWZ0dzRrMk5rSGlkOUR1allQR05PT3pPcVBoY1Z5TTRUZ3hkbFlDU0k0MFNhSXIxMGRKdWVOTmwwdE5VMWR1S1dyMlFlbzQzSW9QRVo3Y2U2TDNGTFRTb2J1RlN5UWR3TEVISjBqaVNqbkVtZFdWbkl4Q1N0ZTlFTkcrUjB4elI0NHVtRyt4ejRxKzk5aS9uRUV4MXRCQ0pBa0Q3UWxZQkx3c1hWbmxQdktSSCs3Q3ZmNUwvNnpaOW51bjVYWWJsY0NFUFA5ZjZLWGJmQmRiM0dsQzRiTFdTSmtiV3FyWnVoZGphaU94a0ZJZWlPNXFxS2kydkRSQlVCcXdoRy9iSzFrcitjaUtwMVhFZTFSYnY0WHMrNzh2bXlvaUp6R2xIZE10OElmMXE3U0lna0Zmdk9XWGVmaEhaeWVnM0xzc0RWZmxWM09GcXN6dENtZEhTOE5iZVNodTBmTDN5SDdZN0ZHaGZhZTdyVklxbm5ReHJDdEVZRWJsSmlxb0dzNnllMU1TdFYxTElVRFNPcmlnb1ZDSERxYUZLMlJpZzE0aTdsWnB4T1VDVkxyNnM3eDBSSk00Vk1GNVRCR3c4SGdoYzJYYUI0Q01XUjBvenpOaUMyZ2pmMUpMSDh1NWFsZ0tVVjJnQ0lPdTY4QWtDMW1iWTY4WXpLNkVvNjJwZlJkTjgydDVLTnBhMGoyZWZpb0hQMFp6dTR0Y1BkUGtWdW4vQTMzL29icmtya0lFSDJNZUoyUS9PcUlUdFBqb21Pd21hN2svR1FpdytuZlArdEI3enl3M2Y0d2t1bmJMdENHUy9KMHpXYjNaYVlJem5QREJ0dGFpcEdGOUJqMTQ3QmlveUpGQjIvUnJIZHR5eUpZZFVEczloWlhPMDgwNWkrZGdUV0dvNjR1bE9taGhhSmVJTFhHTDFJcGhSTkxnVU4relpEYURCdlJzakpkaWdyN0cxTW5LOGFkczRUcVJpZEh0Mk42bUpNRk9VaDFnc21VTWRQdE56QTJOVkxvZElXZlZ2WGpsQnJUS3ZINjJmVTFkQ1VKU3VjV3pXcWtucmlVbHVIY3lVRmxtWm85a0w5M1ZJdkMzWE5nMGxSV0Q4bHV3N2EvNnM4VHc4U1ZDWXB6WkdDMVlsSzFtR3JwWUFrbk5kR3VaejBoWDBYNkhDa1dRaE9TTFAxaW9nbTNGbTBxcjcwL2pzVkQ2bEFVTEh6NGN4WVZyV2hXbzZvTnFPRlUrTWRKa1ZUWFpZR0Qrc0x0VkZNWENCRml5eEN3UG1Cc3QwU3R3UHVkS0E4Yzg3cmwvZjR6cnR2RWJlZFhCTnhtNTRwUnBOL29vU1VZWTR6SWZTRjBFbHl2V2pYd0NsLy9vM3Y4Y21YZnBtU3JyaTFIU2hNTnFzaU0yd0c1aElYVDRKV3VLMTZnSkRzeTZuOUcxQktuYWlieGVCR2JPRkwzUVdXR2t4N2ZoUFZzeGtXclRHbjRNeFFYZTJSS1JhSzJRbE5hVnFBQkp3bVoyV1psNmg4THNGVFJmNHlHRVVETTJLOXFBV0h4ci9PM1p6QlVaOVRxMXBQZXBKVzBWODk1N2dScXo2UGxlSFJQRUM5cWZNcUM5aVZTak9Vby9lclVISUZWaHlrb3JLbGltcnBhSVRndFVKZDljNGttdUNFVVZXR3ZxTWtOUkJLVmdnNUYxMzR4aFlPM2tNWDZIMUYxL1JhK2JETWdWRmdVUXpDdFZOZ0xzVktiRllzMUs4dnpWaXFoN1RQclZ3M3k2ZWtDZ1hXSHhRZXhzQ21SS1lMQXhTSUVmQ2U3dXlFZkhwT09qMWhQQjBZTjhLZmZQMXJYQTdDdzNpQTdaWlVCUEhxOTdOQUFFY0lQY1U3SHUrdlNpZE9DRnV1cHBGM0hoMzQwbGRlNFQvLzVjOXdlYmhINXp6QmFXLzVuS00yeGNnU1crcUJ1bWIxUlhTa2RDMnNZV2hYRVd6cld5cmRsU3BmNzZ1NVRJV1NsK2NXdkRHU25mTTZlTE5BTnFQUzhYclp2SWhkTUYzS2FJV2pGamx0WVlseXR1b3U1TDBLWGxlMFNpZjdWakVKVzV4QWJiMWRiQ1ZicUxYa0hHTDFGcTJMdUtZNXBUbEtQbzRWOHNJK3JtMnJvQjVsdlVsV2ZsT2JnV0lNbFZKWTVwMlVzbmlhckNpbE0wSWdadXpWVU9NY0ZVNTFsUmpxMVRoRGdleVlwbG5uMlh1SDg3VmxJUnZTWnkwVk9XUE40d1M4VHJxMXpjTVp6QzdvNXVTOGVnM0owclNNc1pDdXBDWEhMV0lyeVFtU1RkMVRkUHU0c1FmcGRjTW1CYVNpTE9DaW9KTHpPcDQ4U3dkRFJ4NENLWGpZOXNnejU3Z1AzZUZ2dnY5dGZ2RG9QYWJUUWRqMnpJNHk3VWVHN1piVVFxK1k2YnFlR0EvMHcwQ1pJM05CaG5CV0xnOEh2djY5ZC9qSWl4L2lNODhON0RZQnBzY0VDUlNFL1hoTkh6eUNKcmVLVWR0aU4ybWo3S0xGbUY2N0pzMUlsZzVJVzFCMWt1emE2S1NHR1hrVnBwazdkbnJ5cGU1YTFVTVlFbFZ2WGRjOW1kaldTcjBWZVhJcGRySHRQbWZFVEVycmNjRXRHMExOdzQ1V2VvVnJiMTdGYkJCbWtjWXFycm5ZK2laNVNmS1ArY1ppaWU1eGpsVDVTNjF5cjF1b25wTkN5NmtFelFHclB0ZDZNbGZKbVc3b0YrTk1GbGRLUnR2NkM4TTJOQkpsU1lsY2t1b3VlOC9RTytJMHF5eVZWK2xib3VZL0RpTklKcnN1MXJDblJsMGpnbVhERXJBQ2VxRnFBelRJdlNLbnBkREVHSzBlVVVQazVYcmF1cXB1eXNNMEovQTlmcmZEblp3eTcwNlFzeFBTNlpZZlhyelBYMzNubTh6YndNRVZzdGRKREgzZmsyUFUwRTdBZGJqMlpWSktpTzlJR2ZhcHdPWTJqL0tHMy8rUFgyZlBPWThtVDcrNVJSRWhkQjFkMXgxZmJERmVsSzZJMWs5ZjF1TFRyalJ0MlBXcVdodEJ2YTJMbXcxdFdwRVFTd3Q1ckdvdWl4Uk5IV0ZSaldjOTU4U3pmRlpHUjlFNXJ3MUNhM1p6bFhQVkg5OSthdytYTy80SkhtLzZ2YXdBaVRaSHNmWjkyWUNsT202aXptK1VJRWUvMXovMXRldWZPditrZWtBOUJwdW40dHd5bnNJK284NmpkRjdyR2ZwOVBNdStrdHRJRER3NkRTQ0luU0U5MTg0SlhkZlJENTBXSEV2R2R6YUhVMmhhWTExWCtYS3BYYnYxTlF6Tjh4OC9WaE1zM1pqRXhNTUx5Y0xwR29QZ1JEdHhPNzlvRFJ6cERhdFh5cElaYzFSMndhWW5EUjJjbkxCNS9sbkNzM2U0M2pqK3Q3LzRFOUxwbG90NWt0SjFYTy9Ic2xFTlpERkpIUUJVb0Q4clFvQWxiQ0tDZEIwendtRWMyQVRQdi8zOXYrVC8vcnUveWVQNVBYcTNoWkFZR0JyczZKeDJHSFpESU9XWktjOTRiOG1vRTZ1dExJeGpiL3RCa3phNllUQkxNYzJLa28yNlVLd3ZwQ2JMVVBzeXFvZW9YTEdsWC84NEQ2RHRWYVd5dWxyZ1hBV29GVTlZUWpDcXR5eFdQeXBQZjEvc0dKNG9nRlhqcjJ3Ky9ZSjQ2aGp2VlE1NmM4TW9OUWRmUG5QOS9QVkhxZGlkVzhrWG1kZTJTYjF1bGZNNGJ3SU51SmFqSU10bjZkT2NoWUsyS1dWZEk2NHFpcGFDNzd4NkNuR0s1aG1aRkJjb2FmR1M3ZlVWSE1EQ3NDWFNYSjFHTWNWTWs4TFZ3aEd1RkpMUmlMd1VLdzhsSFdsUmRGcWI2eDIrT09iczZJWXRoeHh3bTRGdzY0eDR0aVdmN3BoUE4vekJYM3lKUnlueThCQUp1dzNqTkhHNjJjcThQeEJjWit4ekhWSVZuTTNhTTBhanh0NENpVWdHdXUwNWwvTVZiejZZK0YvLzZDdjh5OS80QXFlbkc2NGYvNFJ0R0pqbWEvclFVMHNqU0NZUkNiMVMxNTJIMkJBZWJMc3cvTi9KVTViU2pZWFZRcTdGQlIrZHpDZitsa1Vza2FyY1dFbURkVVd0ZjY4UzUxWDF2TFlWSUpwa3JnZDZyZytpNUEvNEJqY05xTjR0UzBnQmppcFUxeEN2TmtxdXZrMU5VdGZ2Snl0RHNkZFdxbEhWSUpaYXNiY1FUS3poemthNVZWMHdNZTFpYVljc1I3YmV4TVhSSXErMjI5WnpDcm5tVnZybGpEK244STNVNlduaW1tRW85ODJvVE5UUGtFVUV3bU50eGJZeG12SGk2a2hzUGJBc2hXUUc2VzJVdW1RaEpaVmxuVkpoS2pEbnpNbXpkOW5MaG5MbkZ2N3VoemljYnZuaTE3L01xKys5elRqMEpGOUVRZ2Z6cE1jWk16NUFFU0VXelUyRGtOU29pMGZ3WkJPK3l4UlN5YVJZY0c1SDhoMWZlKzBubk94ZTVYZisyZWZabkgySThmQ0FmdU1wZVNiR0NTUXlUNG1Uc3gySGFVK1dnbk85TGphcFNwTW1HYU5XWTJ6a092SjZ3Y3oxMThwQTNISS9zRWdnY1p3OFc5cW9pMG55OGh4eGhoZ1pLbUtHc3A3YnZyeUhIcHRlKzZxWitQVGJUVHVwZFF4V0lad3UrR0tQbDZmZlQwWEd5dkg5MVRNOG9ZVmNYMlY1aVEycHJmeEhUZWJWMElybEdNdk1tMUlaSGdBR29kTG1QZGJGSzFoT1ljQ0JxNmQ3L2YzOFV2Z1VkUFhyYWJiRmIxeXI1Wnl1cmluUVpLK3ExNVJDTm8ydVJ1eTAwV1c2MTFVZ1FUZWRYS0I0NVhUTlJXdEJSUUt1SHdoaElBOW5wTTJPZVRqQlBYT0g2ZmFXTDczeUxiNzZ4ZzhvdDg2WjBpalpDZE00MG5VZE9TYTJ2UW9yS2xxcXpqWm95VWFLeTA2V1VjaFdEQk5IMy9YTU1mSjRMcHh0bitXdnZ2VmpUczUyL09KblgrRDI5bGxLMmlONVR5YXpIVG9PNHhYWGh5djZJWkJ4VnQzMHpXTTFvNm1yaW9WVXVmWVlJdXVaTE11RnViRk1XOEpmOFZKcFBSWTFLYzZyKzFldnNSMng5cDYweGJ2Nm5Cck9QYzBQVVdyTlo2bXIxQlV2dHRycTQ0VzFBYlduVWVxaFc5NnBCcUhQcXo2bmFVMDBRNnV2SzFhYnN1TnZ6R1o5VEpWZmk3MS9EWDMwYlhSbkxrc1N2ektrYk50OGtRcm9sM1l0cUFteVd4bkZFalBycVFzb2dkaFZmV005WWNXSkNud1lOUjh6aHFyQ1gwb3grTnJzeDdVdmJ2Q3dyUGZFMWVOQTF0ZG13WEl4ajNRYjNPYWNjUDRNMDhrcHczTXZNSjJmODJldnZjYVhYLzgrVjEzZzBkVmpDYnNOVTRrRjV5WEdUQjhHblNZUVBERk5WTzhmd0ZGd1VtZDlhUDdpRWRmaHlzeWNDN3ZOaG5sZmVKd0NKLzZNUC9pclYzRDl3T2MvOFNHZTNYWjQ1K204NDNwNnlHYTdJY2VEVWtLYzRLV25nSkVja3pHR3RiZDlmYXNrdDdwM0x6dTFRb1UxZUtzVGtwN1k0OXN1cytyRXROZnIvV3NhK3JKWXVla3ZxbWNyZFhFcHZPcU4xVXB0b0VKYUV0eFNrdlY3bTZIUURLVTg1VWtncGd4ZjhxcCtWTW9UVDYzYmY4MlpLaldtMUsvZWtDTERVc3J5WUtXV3ErRVl6VVZqSUZvVlg5UllTcUhsVHhweFNhdllseGFtbXBwOUxzc0hXZGlOSVZ5U3phakVqcTFXM3BOUjRsUDE2bi8vclhwSXFidVViV1oxZEVWS1N2eE1BaVU0bkF6Z2R6b01kM2VDUEhPSGRIcWJSNTNuR3ovNEhuLzUvZGQ0UHliY2JpdGR0K1V3anZqUXl4UXp1ODNBNFhwazB3L004NGp2Z3FxRWxreXdBTkFXcXJaUkZxRzRySDNZemhjdXI2NVVDNnQzWEkySkhPRGYvK25mTXNVdjhPcy8vd2xDU3BEMm5KM2VJc2NycEhRNkM3STRjc3pnSGNHalRPRGFCbHkxWWxma3BqWGlwTHR4V2VKZmpwKzM3aDQ4a3ZpUjllUFFDZzByVDdXKzVadDRyaGxzZmY0NjNoZDdPeW1yNDdMRjhMUU9SNHZrVzFoMVZGeHNyN2VLZElXZnEwRGIrbDNzdm9ibzJmdHBHT2RhZk4rNG1pYWtWWE1OaFlxdDBPbldSY2tsSEZ2Zko3NkdXOVY5Mlh1dFdyZ1ZKQ2ltMG1qbnlqYTB4aVlRV201QnNzdFVsQjJoNlpoSnk2SnJ3MWtOTGxmMmMxbG9PWHJxTFZNVHNRSW9pdGlLQ1dGNWoydzJ1UDRFdDd1Rm5ONGgzNzROdDI3enpkZmY0RXV2Zm90SElzeERKN0ZBbkdaODZJaWs0cnZBT004U3VvNHBSU1NZWkpmRnZrRnlMMFVndVV4cTY5Z2hCTDF1T1JZM2RDUlh1SjRqMjJISG1EclliUG05Ly9oTkRsY1ArQmUvOFkvQXplelRCVnNmVkFBN3Fhc2N4MUduemJrbFNiUXlxdzBzeXMxdDF3VlMvNjZoMkhyaExDRlVEZG44NnJHNnVubHFxTFlVTHBmM2NXWHhORnFzdTJtd3VqQ3lXL1d1SDIzM3FuQy9NdFdqejFrTVE0NWkrNk9haUdyOHI1NjJQRmFNOEhjVFhTdjJuNHE3MmQ5dFlSMzMwWU55SlF4Q29jWmd0YTIyK3VmaTdUbDVNY1RxNlNzVG90WnJzZ05YYzRxeWhMT0tuaTFIS1dLb29YbktSTjBBVjlTVjNFNVh1KzdMTlZpY0ZSUjhLV3JFOXZ6ZUJ5UjB6SGl5SC9Bbko3ak5PZVhrSExsMWg4UEpqcjk0OVRYKytMVlhlUndDZStkbDlnRVh1cEpUMHRtYUJLWXBNWFJkbVZNaTlJRjVIdlU0N011RWVnSnhGSnJRQWVaUnRTYmduQ1BtVEQ5c09Fd3pyblRNU2RqMmQvanFhKy93Nk5FZjg2OS85emZaZENmTSszZUJrVlJtWEM3NDBJTXI1RHkzQWs1ai83cEFXbDMvbWhNa3UzVGxDU094QzNremhLcEpmTFdUVmRnbTRsZWhXMjdQWDBDeUdqNnNGbWsxMG5xaGI3aUw5ano3b05wdXVrenh2UWt3MElwbE5YU3FjS21pV2lzakY5cENmZkk3OHNSOWprWGtiZmxPWWw3SWpxL0lTblN3L3MrdHFENFd0clZOWXhYdkdGb29GU1Z6Mkh2WDNHWEpZVnIrSkVYVi90UHhkOUNEcVRtZjFvK3k1UzNZSnBVc3JLdzVpSlFhcHVrNXlmYjlpajJlVWxIR2R6L2dOcWVFVzg5U1RtNFRUMjZSenAvaEQvN21xL3oxZDM3SWZIN0dKRjY2N1k3OTliNkVyQ3lUWlBSczd6MHg2Nlk5eGxIRHg1b21GRVQreS8vcjc5cnVubXNSUWM5a0ZYOTJxWlNTcUxMdFBpTzV4REowa09NVjRmQ1lPOXZFYzJmdzMvM0wzK0RGV3hEeUEyUzhwUE82MDhScFQyRm1HRHpUdkZkWEgzcFRJVjhFbzJ2dHc4NFFCVXN3WmZFQTY5Mitubmo5ZDFVYnFSZXI3dXBMdjh1eXdKNXlBVy9jbnJZd3k1TjNyVUtERlNseW5YWGV1RlZLeTFxOXZyNVAvZHlhK09jNnpHZWxFVmJEa2JxcDFFbFRhMkprblFzQ1ZybGZFU2VYTU12ZXExWHM5WHZrV0JvdFNBdlJZdjlXNDZ1Tlhkb0tYQm9yZ3FJRVI1SWltbWxlV0VwclJvQzJxNWZWWnpxdHd4V25RdU9sYUQ0NEhiT21TOUx2TzA4SjU3U05ZcG9qdzhrcGhJSGgvQmJ1NURiM3MrZld4ejdGVzFQaWYvbnp2K1NOeDNzdW5lZXFPRldBekY1YzhGVG1naFkxdWJFZ0t0dGhJV1E0RzU5Y1hFRWMyb3pmbW1Pa1NrODZUVGF6a0xYUGtibkFPQVBiMnp5ZWV0NThkOC8vNi8vOXYvUEtkMy9NR0FQYjI3ZTVNRUZ0NlVMalo0bUlDZ2VzRmRYci9VZm9seXhveXovb1ZvMmsrdkZzNS9ubW9uMzZJbjRhTTJCOWU1cVJIRDlXMy9kSmIzTDA2V2JFNjBuTE41Ky9aaVFvbUpHZjJDRGFiNmRoYkQxbm1vdVhOdlczVnJ6WDUvU0lEaVRMS3FtZmU2UWp2WHArWlJ3c2NybHJQODNSKzl6OG5MbzN0UklBVDBMclNLRUsyTUVTWWRTY05SZnQyU3hPQ05zdHQ1OTdqczM1T2FkMzc3Szc5U3pkMlMxdXYvaFJ2dlBPTy93UC8vdnY4ZmJWTlpmZXNSYzFFbVU1VmdqZFF1LzZzY3NWMGtPNWNXai9QNWM4UEZKTEp4TmtBQUFBQUVsRlRrU3VRbUNDIiBhbHQ9Ikluc3RhZ3JhbSIgY2xhc3M9Im9wdC1pY29uLWltZyI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJvcHQtdGV4dCI+PGRpdiBjbGFzcz0ib3B0LXRpdGxlIj5JbnN0YWdyYW08L2Rpdj48ZGl2IGNsYXNzPSJvcHQtaGFuZGxlIj5AcmpfZ3Jvb21pbmc8L2Rpdj48L2Rpdj4KICAgIDxzcGFuIGNsYXNzPSJvcHQtYXJyb3ciPuKGkjwvc3Bhbj4KICA8L2E+CiAgPGEgaHJlZj0iaHR0cHM6Ly93YS5tZS8zNzI1ODczNTQ1NiIgdGFyZ2V0PSJfYmxhbmsiIGNsYXNzPSJvcHQiPgogICAgPGRpdiBjbGFzcz0ib3B0LWljb24iPjxpbWcgc3JjPSJkYXRhOmltYWdlL3BuZztiYXNlNjQsaVZCT1J3MEtHZ29BQUFBTlNVaEVVZ0FBQU5rQUFBRGlDQVlBQUFEREEySVlBQURaYjBsRVFWUjRuT3o5MTc4a3laWGZDWDZQbWJsN1JGeVJPck15U3hka1FUYUFKdEFDVGJJNVRYS0dITTdPcW9kWmt2UFplZHgvYmw3MlpSOTJkbWMvUTlGRGRqZTdHMmhDbGk1VW9TclZ6U3Npd3QzTjdPekRNZmZ3dUNJelN3Rm9OZ3k0RlhualJyZ3d0Mk5IL2M3dnlQL3dMLzl2ZkpJaDZsQWdTMFpsK2o2b1FDNC9VVlNIOTBOR3FneVNGTnp3SldjdjZzYmpwUEthQlZReUFqak5pSUpYTzE4U1I1NmMxK25wNnl1SHRkTVBaeG5mRS96VzM4ZnZpWXpudm5qazh1Rjg4ZmxGeG1PcjZuZzlVT1lIdTE4NTliM05BZXpZRi81NVBKZzcvKytxNVY3eWVFOWJkL0RZKzN2Y3lJQXI1M1ZuamoyZGgvUE9XMllYSWFLaW9KN3NCVFNVK2JENzhab0JlOWJxRkpWTUVrZ09WY0RuVUE0ZUVCR2tMQjQvUEJwYm5lUDNoM3ZPZ2pwRlFnYXZkamZuWGVmV05YL012NGZIZnVzcHhua1BYN1FzWmkyTHg5a1VaRUZGYlJZeTRKMk1BalV1c2lKTWlqMElGVHRXeGg3Y2VPeFQ1N3B3a1E2Zkd5WmdJa3dpTXQ3QWt5YndpVU1kU0NaanIwNGRXVEllUjVhRVV3Y2tWTWE3MmJxSEp4MTNhMDdPKzh3VGh6dnZtMmZPYytIcnVjY3IxeTk2NXRpRDhEb2duek8zdzdmdFd6TFpMVEtpRHFkNS9IM3pLcUFPQVh3MmVaTHllUk5ZKy92cGpXNVljVTZGTEdVZGxjdCswcnI1Tk1ZbkZqSmJXR3dKaXdQQzVMbmtETkZCY29qS01LR09CSkFUQUU3eWVNTlNWcFBYemFNYi9yWjV0TTQwR296Q3ZCSGY3VEhWc0p4NjNsazJXdVp4UXg4cmd3NmtYRmw1elppR1R5cW91UEs3VHU1Z2NpZFArYUFIMFR4N2h4Y0xtY3BrRHM4VHN2RXRWLzU5MGVzNVEzTDVPWDl5ZExqV2MvNmV5bXN1OCtYS3VuR1k1aHEvc1dVbGVMS0F6L1pKeUdWRGk1ZzI5VnZYYWhhUVdWa0Fub3pQcm16S2RsNHQ2L2V6SEo5WXlLWUNkbnFJbGwzRHJCVWNHeE53SEU1TVFCZytaeVlCWjQ3NytOMTY4MWxucHFXNjhUandHTE9vUEJRdk1pN2lwTHJSbnVWMUVPTFRyMCs2bnVucjZiVXFhaHFQeWZVT3UvSlVuTFNjMzRtOTc4cmZQL3JpT0U5RTNUbi9QdS8xN05rRVg0VGduSTJ0YUhRdG11ZWkrZCtZeW9PbWRjaWd4U1F6K0JnWlFRU2JMM1UyVHdJUUFVVzBhTEp4em15ZTB0YTFpd2x5M214czBaOS9YWi9tK09TYXJJdzBiT1JxRnBtSWFabmg5cnc2MjNoT0xiWmhKNE95bzNoVFkyYUFKRVJNeFd2T09JV3Nyano2d1JjOHU5aHplUmpuUDFpN29tR1Noem1XNGg2S21zQ0pib1IrY0J0ejJXMm5yL2JsWENUaDhhOHlUazU1ZFlyTGRxZUNvdVdWeWVkVWJRY2FmSmpwOFZ5eENJWjdNdk4zZXpOS1pRNXRuRlZKVDZ0RlQxc0FVallsTytwNUcrQkVFeUlYbjJlMFhvcVFxYk9OZUxMUlpoRmNIaXdFZTJaU3ZydDU5am94TVFlaExuNDgyWDVYWDN5dmJTTjBQTWJqZk9OUE1NS1RmSkVubVZGblBsOXVZTkJZNDBaUnRKcVpEK05iNDNlbW1tRVVEaEh6Y2NDZVdiRS84ckNycXlPZGE4MlVCOFZwZnlLZitkZXdWQnptUTUxNWRhWmhOSXVaeGtYajVGT2FjanpQT2E5Wk5nOTYrZ3FRM0NEMDVmMHpyenI2RWViYm5QYk1oc2lPSzd0K1BqV2ZRcHI0aUp0WCs5b1p5K0t4WXlMTWsxblBiaklQNnJZK2F3R1hpOVdGVTdjSlRvazl0ODNHdTFrYldjRGp6aFhuelgxT3pNRHk0elJ2UE9BeGlMVDV6SzlpaEduMDY3enhKQ0gwWmRGa2xTM1RLQldoMEd6bXd1bWRjQmc2THNKQnVOeTVadUxvcjBsR3hvbFhDeXpnME5IS0gwWjV5T01EM0JhMjRSeHg0a3RxOFF2UGU4VU41b3hhTkN5WHhWeTB4K1BtY0loa25mdHFIc05qd2c1dTYvZnBwaUVpdUhSV1g1L2R0SEk1My9icmhSZDg4WjJNNTdWWHRnSko5bWJhWE1UbVFzODFyZVhVcG92NDhybU5tV3JQUGVNeitISmZNb2s0SmhrMkY3c3VtWXJoOUJKMDQrZHRiZlNucnV0TUlPNVRHQUcyQmVtamFpNndYVFlnNUxMckRpT2JyajczZ20xblB0OW4ybnorOUw1VjFMNWtFcUJra2dRZGZUaE5wNDZWSjRLdjI4ZGdlSDlZT29PUWJyL0txV09ybkJabU4zNzJvcm5UclpmSnEyMHd1b21kUHZsVnRoWnNSdFFpMDhPNXp6T1J5K2NIejdlOGZ2eU5YSnlaZGVLS2lwbjRrRTZubStKZ2RtOHV5azNNV2ZQWlhkSExEcEUwK2x1V3BnRVBRa25YWkJIYzVKbG1jU2l1dkpiemlOdFlKOFBHWEs1ckUvZ1pnaDFQRTVYOTVDTjhIS0dham1IdGVzQWpXM2F5N1RjWm5QbFZPV2U4OTRnSXNZL1VkVTBiZTNLT09CY0k0dWpiU08wRFZWWFJ0aTNPMlY2Zk5FTWxKTzJoRW8yYVVTZjByaU9qbzFaeFZXQytNMlB2MGlYMkx1MnpmM21QWmpGbloyK0huWjBkNm5sTk01OHhuOCtwNnBxdzIxandaYklRQnUwK25adHBybXY2M3BDRG1vNnR2TmlwWFh6Ni9hU21GYk5NQXgwbEhEMllwN2dpNFBiKzlMcFVNdXJpdWNmV3N1RnRXeXBhRkkrU09YdHY1NDJjOHptYnNGMm5xdEludTZhVUZJMDlzVXYwYlV1M1hCUGJTSHV5cEc4N1ZpZEwycE1scStNVnE1TWx5K1dTMlBWb1ZGS2ZJRUVnVUZFUnNxTktEa1ZJTVd0QUpHY2xxVUpaUDFxdXhXVWh1QW9WNkxxT3Bxa0JpTEVuSXdUc0diZ3lEOE42RGNWQ1NUYXI0M1AvSkdPWXg0Mm10M09PUHRuSEZiYkJOSm1HMlBNZ2FJUHFEUjVTd250UFNnbkpTdE0wdE8zS1ZMeFRna0NNSGZQNUhGV2xqVDFTT3lKS2Rrb2thNjZVbGZic1h0N2h1ZWVmNDg3THozSHp6bTJ1M0x6T2xTdVg4RldnVDhuTUNCSXhKektaN093Y0dVZ2F4MTF4NVpWMWZFUnl0a0VrbEdGekZsV3lZL3g5ZkQ5dmZLUlJ1SXB0cG1SUUlYdDcrTmx0WHFjMjNQQTVGU1dSemplbG5EMlQ4eU9ZWlpGTEprdENYUzVSV1pEc1VMZnhJUWVEZEN0cU9qRkFzNnI1Y2VYejArK2QvbDFKYUxhNUhKTDRjV0tUQ1RhUEx0YzBxV0dlbGN0eUhVbVdvM0o1RTFRQzAyS3hTNlErMHg2dk9Mei9pSHZ2ZmNqZGR6L2c4TzVEMXNjcjVyTWFyNkpPSFJVQlVTWDFkdi96YWdhOXlucTlwcTVyZ3ZQRXJrZEVDTjVERXJ0bWhTUmlTWlJ6Z2h0UEkxd2ZSd0JIb2Z1WC8vcGZuZnVIcHg5RjhZNzVoK0tUbForTW9rNE1xWUU5aEJ3VE9XZXEydHRPRG5ReDBpem1yTmFkYVpiS2Fac2pIWW5uUC84aVgvcm1sM241UzYrd2Yrc0tiaDVvVTg4NnJmQ3pRTkpJakpFdWxYQ3VkOFNjNlZQRVY0NG9TczZScUxrRUxTYW1sYk5GSjZyRW5MZjB5WG1oL0xPcDVESnZweGYxcWRmVFI3Q2MvR0Q2bks4SkwzcHZland0UXJMWkRMYTlQTTB5b21YR2tNa2tEMkZlWmpwWHFNNFRzc0ZNRlB3WThCam1jMWlHSHNGTENWeG4wN3krL0hVSVhZaklLSEJPSFdSRjFGR3J4NmxIa2lJUlBuenZROTcrMlJ0OCtOWXZhUTlXNkdISExOY3NYSVByZ0JURWt0Y09ZbUpXejhsUmlUSGl2QzhiVVI1Q3NRVVZVaElHcXVncEMrYWk4VkdGYlByNU15SDh4eDNzdkljLytBQWlROVJ2T3l5cVRzeE1GTkNZY0tGaU5tK0tLU2owSk5yY2FWak01R0I5cUc1bUp0em52dkpGdnZYN2Y0L25QdjhpcmZhc3BhUDNrUWQ2d2lxMmRCckJLNm52aVRtUzg4WVoxcWdrbEN5SjFLWXRiVEZDYTFUSm81OWxtaUJwUnJhVnpxZ2hMcHpNVXlIejB6TTB3cm1LaWJPOTJQTVpFK09pZWQ2OFAzaVE1YmlUNk56V1p5L1VnS2ZlT1FXSE8vK2NwOWJGRUZOU2k3NU84M3hhRm9TVTcyekJ5Z0JSSWJNeDIxU1ZKbFI0NzlBRW1oSmVBazFUNFJjVnU1ZXU4WlhQWGVHYnZVZVdpZnV2L1pLZi85VlBlUGp1UGFyZ2lhdFdYUy9zTkF0OEVPbTZFNElFRnJPYVBsbCtUY3ZjMjZVcDRvUUVPSlZ6NSs2aWVmaTQ1dVFaVGZaUnhsU1kvTVJrekJUVGJJaStPd2V4cC9hQm5IcTZycVdxS3BMTDVGcTBEOG9xZDN6aEcxL2xkLzdnNy9IQ2wxNmtyNFdUdkdLdEhXdHQ2WW4wWWdMVGFTd2FNckZhSHlIZUxpSkcwMmhqZ0dEMFgzS3h1MjFSNTV6Sm1rWWYwVFF0VytiaTZkZkh6MEtaekhOTTcrSGZIbm5NY1RmSHVNZ1ZIMU1PV3cvYW1lLzIxT01zZnZGcHZ5NmlaNFRmZktMaTgyQmFZNGkwam5NeFBiNTNCSEhqZktzVE1zbU9teFduanVBODNudHl6dWJ2aWNPNXdLTGFvY3FCc1BicytqbnhhTTBIci8yQzEvL3FweHo4NGk2eXpqUzVwc2tCV1NPMWVpUjdYUEJrQzVkWVRoVzMwV29LSWVjSnZuTTZVeGNybkttZi9UUnd2azh0R1owWnI5MHk3OEtJcG9teEk0aVpNZEVwekd0U0ZlaGMwandYdnZyZGIvSUhmL3g5d3FVNWF4KzVKeWQwTHJKeUxVZnJJN0pYZWpyV1hXdnFQMmU2RkZtdlZ6aWZ5VVdUZWU4dHJ6VmsvY2RGc2NuWmpPK1o3VXBVYzdyeklKUWxXNTVnNnhYT2hzYnRISnRrcjR4TzZGUUQyRnVwVEhnZWY1OCtIZE50RmhMZjBuVmpvR0p6enVtRDN6aStaeGJFWU81dFlUVVZQV1h3bnFjMHQ3OVRBakJaejJnMXArQmNHTVBzRnBKUGxQaFgrUzZielM1Q1A5bjhzbVRVRzdnNGlDTnJwbzg5a2dUeERpcEgxb1N2aEtOOEJCR2EwTENTRldIWHNmK1ZHL3pEcjcvSS9iYy80T2MvK0Nsdi9PQTEvRXJZMjEyb3BFRElpTVlDQk5nOEdNdUNsR0RJeHhrNm9DMmVjc2kvK2gvLzlabUpmZHB4a1NZYnd1SXF0Z2hkRlVncGtUVGk1cFVldEV2cS9UbmYvZU0vNU50LzlGM0NwWWFqL29UV2RheGR5MkYzeEdGM2lOU2dMckh1VHN6bjZ0YmtuSW5SSXBOMVhkTjFIYmtzOUdFSFRNbWMzNnIyV3hwTk5aMTduMDlyQnB5bjJhWm9oOEhlZjVxNTNOWllGM2w5bHNoVlRlTTVUMGM1TDd6MlU4QmVWNFIxMjJCOXZDYmJhS1NMejNuUnJRNm00dGxqblpvZkVmSVFQNUt5U2FxU1VpTG5pSzhyaTlDRmdNZmpNMVMrcGhKSDZoTzFteEUwc0Z2dGMvekxRMTc3OHgvejVnL2VSQjkyN0RLajdqMjFWdUp6U1EwTTVxejRzdW1jVHNsc3JuV0thaG4vTnBtdjRYay9hY2kvL2gvLzc1c1Q1YzAzcG90bmdLZ0FZM0JqMk5IakJFNDE1anpLdzgyU0VRK3IyT0tiV3RmMHJGem0yMy8wUGY3b24vMEpici9tc0QrZ2RUMGRIY2ZkTVd0dDZWeExHMWVjZE1mRTFDS2lGc3JQa1NZMG85RDBiV2VUNzB1a0szVm9GcHpIMGdZNUY0VDRXYzJ5ZWVEYmsvdll5Wm9zbW5GeDZ1TkxWUzQ2NW5sbTRlT0VmWnBUUEhXRzAwYzU5KzhYd1pwT0krVFArb1o2Um1DbWY4K25qcnV0aHpkWE14V3c2U0xXUFBIUm5COTlZSWZnZ2hSZkcwdnpEQm92Wnh4Q1hjOGdWS0NlS2xmVXNXTFA3WEx3OWoxKyttYy80djJmL29Kd3JPem1HZk5ZU1VnZWwyUnpMczczUjVNKzVwNVByWituRTdKLzlUK1ZIVzhTakZXSEVvcEdzbEJ6ZGhtbkdhOFd5YXFTWVFQWFBrSHdrQnlwejh5cUd0VkVUR3RjSlNRZldlZE91MHA0NXZNdjhmdi96VC9rOXBkZjVsaGFIdllQeUtGajJSK3pYaS9wVWt1YmVycThKdVllRmNYNTZlTEtHNWdWNXkzVXFTYjQxWTFOU1AvdjNqaXRDYy96YVovbUdKWnFkOXQ2L05UR01rMjZRekcvblFDT25YcUJ5eDd0NEZLelI2MDE3L3puTi9uUnYvMGJUdDU4eUpXNHgwNXV4SzJWNER3dUNGSE5VdkI0WW93bU9NNzh0NFFTU21KN3VBZDFRaDZsY29OR0dhN2xvbkhLSnpzZG5KWXhwelJNaGorOWMyVkZVNGJzbVZVTmdLbDZyMFJKZE5xcTdnUys5dysvejNmL3ErOFQ5d0lQOHlFZkxPL0JRbmwwL0NGZFhoRmozRUpWR0h6S25GSjdienpoOXRXZWVZaS9XZ0hqM0d2NHV6UE91L2ZodmFlZGwvSHpwM0FZNTMxL3VwZ1RpdmNPSjQ3VitvVGFOeVNVUjFtcFhNVXpYM3VXRzNkdThlWi8vQmx2L1llZncwbXJPMkZHNnJLczFpMTEzWkQ3QkI1Q0NDVzVudkJWSUdzbWtiWUNWaWJnSDkyUEN6clVCSTFqQUZHYXdNbXBteDEycnE1Z1BxdFFrWkpTT1VlM1d1UHJDcndqQjhmU2RicDc1eHIvOVAveXozbnVTNjl3ckN1TyswTWVkSWQwdXVMazhJZ2thMkx1U2hqZTBBdnFzc0d4U2g1bk9qYlhjaHFJK3R2eFgvSTRMOFhoRWZxMnAvWktFK2FrUHJPWXoybmJqbzZlSERJNzEzWjQ2ZmMveDVWYlYva1AvNjgvcFQ5S3pMcWdWL2IzSmE4eW9URy9QdWFNaEZJS0k0ckx1VVEvTjFvV3RXcVNvZkxqc1JYdGsyR0lEeG1pVkVWak9UWDBOMnFsMlNXL01FVHR0aHovbE5BRVhiOW1QcCt6N0Z1WWU5cWdldnVMci9EUC90WC9nVFFUN3VvaGgrMFJMU3VPdWtOV3JEaGNINkl1RVRXT1Vib2g1T21jd3psRGlQeDIvSFpjbEtlcW5DY2xaZFd0YkIxMkhZdkZBb0JPV2xKT3lGeTQ4YzA3L0tOci80VC84UC84dDdUdnJWbDJ2ZGJleVhLMW9xcXFZaWtaYUNGa014TTFLOWw1aTBSU2dudVkwRm0xd01heWUxd2ViYlFOQnk2T0xNcW1tdFMwbkN1ZWtLYzRneUtrSUdRdmlLOFFFUlk3TXpydGlWWG1ZWGVrTC83T0Yvay8vei8rTlZ6ZllibWIrYUMvenlOM3lQM3VMb2ZwSWNmdEFXR1dVZC9qSzZobkZmV3NJdFFlZFJaYTcxSlBGaDJybDRFQ0huVmJVSzdmanI4YjR6U2VGRUI4UlZMRlY0SFpvc0Y1Mi9EYjdvUjFlOHlTSmYwaWNkY2R3SjJLUC9rZi96bjdYN3JGUVRqaFlUeFdGZ0hYVktSazVaMU5WUmRFRWlOMExEUFU5UlcweWtlNFBqZ0g4VEhBVEFZb0RycnQ0RkdFVWN1L1U4NTQ3MWhxUis4VHNYSDYvWC84eDN6Ly8vU1BlV3Y1QVErT0Q5R0Y4TEIveEtvL0l1VTFNYS94amRLbGpqNVpHWW5EbWZiQzFMQ1RJWS96VzBuNjdUaC9aQUdOc1NTYU0yM3NxZXVLdnU4SlRwQXFzSTc5V0VKRkFHcjR3Ly8rKy96bC9NLzV4Vis5VFplU3BtNk5TaTlPUFROZldjNFZoNG9iWXdLdzBWYUdYTGtZT0hCYXE0V25Sak1VS1hacUFqYVUwMGZOVUh2V09lblNkZnpKZi8vUGVQVVB2OGxieXc5WTFpMnJmc1hEaHc5UTZWbkhFMmFONVRkT2xpZk1kK2RVVFkycWhXcHozbUREUnpqU2IyWHN0K01KWXo2ZjA3WXJmT1hvWTR2M2pyN3JrYXlFcG1LOTdxa1dNNUtIVmV6SVB2RTcvK3c3dUlYdzJ2LytPbFhudWI2L3AvR2tsYlpYUXFnUkhERkZReXNOaVhiWklHYU1CdUhwRURORms3a0oraHd1eWg5QXlaR3h5WlBsNFBSWVcvcEcrSlAvL3AveitlKzl5dEdzNVVUWEhLWkREdHVIOUxLaWJVL0FKZGE5NFJqcnVvWUVLWlV5bGd3cWloZFhVQnRhY2lRZkx5di8yL0ZmM2poTHIyZjFaTEhyY1Y2QURCNzZQaktiejlEc1diVWRvYXJJZmVaa2ZZU2Y3WkpjNWpBZThaVy8velhFQlg3NnB6L21zRDloNXAzV3JwYVlMRUllcXBxc3BaUklObFdIZ3NlWHdPRFRKSXpDZ0k0V1BHNUVodnNSc1pGTFdqbzRUODZXdzB1eHA2a3JEdnUxNXVCSTg4QWYvTk0vNUl0LytEV1dzNDc3OFNHUDRoR1AxZzladHNlb1M3amlJWXBhK01RVDBGd2NTYkV3N0ZnV243U2duczRxWkwwd0tmdmI4Vi82T0FQM1Fzd1VVeXRwc1hoQ1JvS2pUYlpPdkt2SnlmZy9RbkIwNjJOYzNSQ2x4UytFci83eE4yajdOYi80aXpmUkdKaUZIZHFITFR1elBRT3grd0ozYzdJQmJTaUlYcHpNUG4ydFJuM0FnT0p3WjFEbHcrNlJNd1d5bE1BNStweHdqU2N2SE4vNC9uZjQ5ai82SGlkVng3MytBUWZ4Z09QdWdENnZVRG9VaXg0T0lFMGJIbEhQVUtZd0lrYWVpa1B3dCtPM1kySlZZZWgrQndXQjQ4WVF1dzlXRWUwemRNc1ZkUjFZcm8raGdVZnBpR1U0NFR2LzlIdmMvTXB0anVzMUIvMnhoa1hOeWVxRXZiMDlXL3RNQWhwNW03RG5hWVk3UyttMXZkQ2RjNFFRNkdKUDFkUzQ0R2x6QjQzVHRYUjg3bmUrd0QvOFAvNVhuSVFWaC9tUVIrc0hMTmVISEs4ZWt1SUtKOG5JWXlnbEp5TW5od01ORmxqSjJ4SERUUVR4WW9FN0hYWDg3Zmk3UFVSQjhyQmhEenQ1cG0xUG1OVU9JYktZTlJ3OWVrU09pWlAxaWh5Z0RSMFAwZ08rOTgrL3p6TmZmNEZWMDdFT25ZYUY1M2g1RE03cUlVZXkxaUxZRjBVU3p4dk9vQ1F5NXFqR0h3cE5RSXlvS2pzN2M5cCt6VW5zcVBkbWV0QWY4OEtyTC9QZi9BLy9IZmU3QTQ0NTVyQS9ZSzByRHRjUFVJMWs3WEJEVXRuSlNFTmdMTHFiY09nQXcveXR6UHgyZkpTaGtzZWZncXBGMUk5TTAwS21EbzdZclhFQ3NlOEkzaHZTSThFcXJqblJKY2R5d25vUitmYWZmSmY2bVFWeEozT2NUbFNyU1BiYk9FWWpVQzIxYy9LVVlQQXhIemJSWmxNN2M0Q2JkRjJIZUUrMVcrbGhXak83c2NzLy9SLytCUWY1bUx5alBGamY1NkI5d0tvL3hQbU11RFRXM1l6VXlLZVFJNmJTODVtZjRYb2U1Mzg1bGVKRC9uYjhYUjdKNWRFMEJNZEF4R01ZV3pWWWJlNFJzV3JwdXBsWllNRjUyajdSeFpiNTVUbjN1d2VrZmZqRC8rNlBlT1NQY0plRjFxOVZ4ZW9UQVFiZWY1MnMzY3lUdFpyYkxHZ21Gd3NELzdyVkRGbkV6MWVPdy9XU05JTi84Uy8vcjhpbGhpTTU0ZDFINzNMVVA2TE5TNUxyV2FjMU1VZkRnT1dNWkFXMWdraW5VNkVlQkV0aFdyMHNYT2hRL25iOGRtd1AzVlIzbitOZXRHMUxYZGNremN6bWM5bytzdTZzL3RCNTZGTFBxanNoaFVoWGQ4eHZMZmplZi8xN0hISkVIM29TVVpWVVNtUUNTU2Rjb0U4NXJHNU5sTlBhekVZbXhnNHBQdFhSK3BqRnBWMisrL2Yva0tzdjN1UkJmNGpPaGVONHhKbzFrWjUxdjhaWEFWY0YxbjFudFVKWkNDb01IVFNHVGgxWmN2R3RNdXB5aVE3cDZHLzkxdWY2N1hqOEdOYnRCa1ErL2NrQ0xuaVNHaGx0ekNEZUcrZUlXTm9xcFVTZkVuMWFjOXdlc3FwYW52bkNiVDcvblMraWMxQVhTNjdXdEdRU1IzSnVGTFNucmgxVWNVV0Z5c1JFczNCNlhWV2tuSWtvMWY2T3lsN0QzLzl2LzRRMkpMb3E4djdCdTNUU2NydytJbW9zd0Y1QkVVSUlaNmpXaGd5Nll4dVlQSjcxVjZ6Qk5uQ3lqLzc1a1ROQ3RkelgyUWtmUHpzaDdQeXNobTFNWjE4Zk4vNkxncWVkdytQcGduRjlxQk5penFVQTFKRlNUL0RDTEZUMGJXdmY4Y3B4UG1ZMTYvamE5NzlKM0lXdXlxU3dxVXVVajRHT0NKcURaYlhWcUw1U1NuZ3ZWT0x3d2RHdE85eTg1bGg3WFVySHYveWYvaFVmeGdjY2M4U1JQbVNwaDV5c0QwYkdwN0ZVWDRkRWNpbWRGMk43bjVadGU2UVVUVDZtRlA3amMzQnVqWE1MOEp5TW9HZDBvSVRiWlBqSGF5L2xEaU1WTmdQcnNZVnpOUnN2b25qYjdTYmJsT0U4VThLVnVxZVVyRG1DYlVDbEFsZzJ6dlZZdXpRVW1VNStuMVltYnhXUURxa1JVWXZjbGtDQWQ2QXg0M3dnZFQzQmVWSXk4aHNwMXhYY1FLR3Q0KzQvak1IbkhhbXZKeFIxNDNLK3dHOCtyMC9iNlhHYXZmcmpRT2lHU09LbXdsbEJOeDJDY2s3NG9mWkx5a2FvRUp5VmFYa2NXdWdJb2lSY2NKeklHdGtSdnZsUGZwYy8vNS8vZDhpZDdxZ1hseU5HSnUzR3ZQRXdiMzVLTFQ1Tm1JdmdRcWdCTjBZUkpRamVDekgyTEk5UGpNc3dKM3F2ZlBzZmZKZm0rZzVkMWJQa2hGVTZKdVlPOVFrREsyOG04blNCM1huTzRlWjNPVmZBUHMzeCtQT3pKZUdiMU1HbU1zRitIY3hjbUdvdEsyVVhYQUZRTzRvZ09ETm5CZ0pVSTJCVnFxcWFMTzdONlhQTzZLbTZKUkVUQ09mOFdKbmduRWZFb21sR04rR29YTUJMd011Z090VUlRNTAzbnpodmpwVUx1YWt2cHBPeUlVMjE4ek5leDFUSWJXNCttL24vK0JqVjA4Sjd2dEJmZFA0WXN6MkxCRjIzSnJuTUtxL3A2OFROejkzbTJ1ZHUwVldSTloycVYzd1FVQXVpNE53SUJ6UzZoRHhXY2tNaGtBSkMzN2Q0NzZrYjIxbUhwSE5kMTRURmdqNUZPbzBhZG1xKzh3ZS9pOXNOUEdqWEhKMDg0aVFlMDB0ckNQMXlRblNDYnp3SC92L3JCdnh1b1FZS05Hd1QrZFRDcys2QU1LRmd5Q0NGMDVGY3lodktJcWRpaXloMFRGd21CQWplazhyS3pMbGc0QXBjTEdiVGFCUU5haHdrVXE3RFhwTU9ndXJMZkxvaVdJS1dUaWNhamVKTU5lSEUwUlNocDd5SFNQbStnUE1NaHEwMTFKZytEemxEbzIyYVBtNTlSdFJ0T3QxOGd2bi9kWXd6S0g1eDQwWXpwS3h5aWlRZjBaRDUxdS85RHYvclcvOExPU214VXlWbkVaV3l5VEZTWDV3K3h4WkEySHZyTVpWemNTS0Z3amt1eEp4WTVZZzJ3dC83aDk5amRuM08rNnU3TFBXWUtEMWRXcE1rb1U1SENaNmFBSitVOXZoWE1Yd2V1TnUzY1pLbmw4SzRGc3RIUkRPb1J3dGEyNzZVaTVtOE1VdmFiZ2xPak5WSkJLUWszWjJqRGhWOWwreUJxVU5rcWtVWk5hSWRXbEcxQnlwaW52M29kNHltWmdETnVHeFZ3OGFxNUVzeG9pT3IrV25ERHB0MTB4ZkFUTU1oQkQ3Y2E3bVhDVGVqNU1HY2R1TUc5YXYyb3ovVjRheUhneGJDb0pSNm5EaTYzUEx3dU9PVkYxL2kyUzgreDF0Ly9xYXhvYWtuaU1kNVIreGoyU1J0VExYbFZKaUQ4NWoyS3QxU3ZMTWRPc1ZFRnhPNThlcjJQTi80ZzI5eG1BODVqSTg0MGlOYVdSTWxib0NUS1kyMi9sVElmdDA3MStQR3h1a3ZHbUVyZGJDZFA1VFJYTEpGNXNxQ1RaTldSWktIcmlMWkZqd3dyeHN6ejhwNlZWVnl5c1NjOEVBbHh0M3VTME05elJ0L3k0bjFETmp5d1NaQ21FbGtueEF4bWpPTkNVMFpsVUtwNWdRSm5veUNNeUMyRnBObUxQVjNHMkRBY0d6em9lM2VwY3pCYURhNjRUK25tYkQrZG82VUVpNEVoRVNPa1pBRHpxbFIyeldCZy80UlgvN2VWM2p0aDIvUXRabWFtU1pWR2RaRFNzbE1kSVlJeE5rUkJrYW5FQ3B5YVMyYmM4YUZpc29ISHVVbDMvbnVINUxuc05RVmZlZzVYaDdScm8vQUoxdDhwd1JycXRYK2RveGNOTkxRbW5kb3M2TUZScE8zVy9KZ1lHYWRmbDhGa1dJdTQ0djVwK1JvSnBqTGh0MnVmSTFMa0tQMVRuSTVFTVRqeGVHOUVYdFdWVVZWTlhqdjJaMHZ4ck9rbEVoSjZmdWV0dS9wVThkSmQyVDgvcVh4UTNBZThkREhUSnM3b2xPaWRvVGFFNEw1Y3luM3FCclFJQ1hkQkR3SGxqRTJiMW5IekkxcFBYNXV5Mi85Mnp2RTIzeFFLQVhSUW41TEpIdkhjVDdtNm8xcnZQS056L1BHbjcxQkxUVStPY1JCcUFJNXBndXJvb2YzUSswREtmWGxBVVlxRnphVWF6bWhUY1YzLy9nUE9mQW5yT2c0V2gyeHprdXk2Nm05cCs4enZqanhZdDY3dlRvcHB0TnY3ckJGazlEQ3J6ZU5yamtTdnZEM1QxczU2Y0RlSldETFVjb09OZzM2T01pQ2k0NmdGVE5wY0gzQTk1N0x6V1Z1N04vZ3h0NDE5dVo3TEdnS2s0b05LVWYwVkVqSlVEb2pTTE5yTHY5TEdPQmFFUktSUktLbnA4OHRKKzJTZXlmM2ViaDZ4TVAxQTliYWtsSmsxYS9KUGhYL0EzTHVVZWZIQU9HUXZoRW9iV0JMNEVlZDdkYkZGSmFKbjZhZkhqL3VyMlU0RDdHemlLdjNEcEpwKzE1dGp1dFp6YVBsSTc3OHUxL21yUis5VGYvSXZOL0tWZFlVOVJSSnFoczAzTlJjTkZwcklUZ0I3L0hPazdPRm5xTVhmZW5WejhGTWVOUWRjK0tPaVQ2U3RDUG1IbCtTZVVNK0xPYzgydnUvNmFiaU1BWll6dURpRDJOalJwYTZvYTMydUZLcVpzMm5DK29oQzVJaHFDZWtnRVNCRnE3dVhPTzU2ODl4ZSs4T00rYnNNbWRHUTBXTlI2anhSdXRnQ1kzeEdxUUlrQU4wOHJmaGlueFo2aDNSeXBTQVNFL3JldnA1eC9QejUrbm9PV0hKaVI3eGFIM0FtKysvYWJ3cXdkSUdYZTVKd1V4SlEwMVlSeHJqUFN6aVBZYnZ6WThjQTBPRFpHNXB0Yjk5bys5N21xYWg3enRpMnpHckcyT29qaTA3T3p1Y3hHTVdZWWY5YTVkWVhOdEJ1Z1JKeUZsSnFmUk9PT2U0MHo0QUliaktPQTYxMEJjN1I0d3RZVEdqenkzZi9zUHYwdnBJbTFyV3RCd2VINUJkcHE0cmNqWW0zOEU4L05zbVlGbEFnNk5QM1pqN1NNbG91eFdQS25qWG1HK1VlbENLa3d5VnE0bWQ0anFobG9ZcWVXWXk0OXJPRmE3TnIzRjFkb1Vyc3l2c01LZWlwckxPVzlRRUJzL05GUTZKRFZTNjVHMVFCQ3R6VHpGYXJsQkJuSmc0dW9GVlNaZ3lyWXNHS25Fb00yelRTRVF5dlhTMDg0NnZ2UElsMXZRY3BpUHUzditRRDQ3dWNoZ2ZzWllWSzEyUlFzSUxkTnFTblZMWG50UkhpMHBtaXlxSzk4U2NRRDBTSEJxdHc5Y1VYQTYvL2lqaTA0NGdqdHhINnpwVE9YSWhiZ29oc083WENKNG16T21KdlBydEwvTy8vODkveWhXL1o0MVYrb2lYc0VYV0Nwd0pmZ1RydUJLMkVpQStCSG9pemY2QzY4L2U0Q0FlazBLbWJWZEcrNmdaU3Vnei80YWJoRThhdVk5VXpvRm1VdHNEVVBzYUo4R3F0cE9aemI3MFBmYlpPaGY3V0xHakRiTXU4TXorVFc1ZHVjbTF4VFd1Y3JrSWxXUEdqSnBnUGJ2R0VsUXRRUktMNXFZaVRFNjNPM3NPby9MR1pibjE0RW9TMUNSdnNwUG1UWDV0YUYrK0V6eEtRMCtrSTlLVHVPYXY4c3pOMjN6KzVvcEg2WUI3NjN2Y1BmcUFleWYzT1RvNXd0ZEM4b25sd1pLNnJxbUNKNHNTYzBURTRZSzFxK3I3U0YyRnJRVDZVOU9JLzBhTjgzT0FDbWpwdzdCa3hjMlhiakcvT3FPOTN6SnpYdFZadmNwd2gyZlRBME1Ud0xKRElnNlJFdElOMEpIMDgxLzVJdldsaHRYNlBpMXIycjR6cVMxUUlzMm5tS1QrVmt6b1pnUVZLaXJvTXlJT0g0eDNmYlZ1VVluVXpaenNoWDROVGdKQmEwSXZWTGxodjluanhjdlA4YVVYUDg5bDlxaXBjVGdDd1lvRThUUlVZMlg1SUZ4T3pYc2IvTENoWDVlVDdmbWJSaGkza1RJYkt5R3AybzQ2Q2VWUFNxbHdDbDJoUEt1RHB5YlFrNGdvRFRVN3pMbnFyM0JyNXhhcm5aYzVZY205NVllOGRmZE43cDNjb3dtMU5ZSG83VnRaTWhxc29VZHlpbnJHM09oMFhFUngvWnM0dGdUcjFDV3JtZ0JGVVRwdHVieS94K1U3VjdoLzhDRXhadHZHWk5QMUU4N1BEUWZubkpsQVdSRmZvVnBNRE5menRlOStuWVAyRVgySW5LeVhkS216WFZnMjdYSHlLVlY1MFlsK0U0ZlJmQW5CQjFJZmFUdkxlK3pVdS9Sa05DcWFZSlliZkJlb3U1cGJPemY0d3EyWHVkbmM0QXA3N0ZwYkE0THB0OUlvWHNrcFdWaEVqSERGcVBVMnFCQmZzcmxXeEQ0SjhKMTYwbGszNUMzRDN6WUNXRmgwaXdlM2xhOXlKc0QxdkVJeGhFTE1FU1hoZzJQbVBIVnBRcnhEZzNDTkZVdXVMNjd3dVJkZjV2MzRTMzc0eG4vbTBja2pOTFJJazhraDBXbEgyNjJ0L1pHRG5NUFdOZjl0ZU81UE5RYUFRYmJ1ck5Fbmpyb2pYdnp5ODl6OTJYdDBYY2RPbUJtRmh2SllGeW5nRkZVTFFUc1JpNm80VlRmMzNIcnhOdStrWDdMa2hLaTlKZXBLenNTNmx6aE96K2w1eWJqZjFLRkFFcVZYUVZ6QSs5cW9GV0pDZTZXV2l0d0sxK2JYdUxsM2l4Y3VQY2R6elcwdUZVOUx5SGdxTTZGekpHVUJDUWF6VW92T0RmNFRlRXA5N0JaWUs1NTZIWGJXamVKeUkwTGo5TFdiWml3Y0ZFUFNZVFFmdGVEVGk3OGNwUGlERlFrbGFqTCtkMGVKS0NkOGJ0Z0pEVDJabmJESDlTL2M0WjJUOTNsLytVdmVQLzRsajVZUEVCL1ptYzhRU2NUVWJjWDJMVWtPVERDT3YvRmpDMW9uSTFaV3dmeFF4RXhsaVVUZmMrZVZPOFE2MFIyMzdMZzVLZXVZSjFPd2RCYURmQXcrR1FtOG9Na3diU3FnQWE3ZXVVYm5XM3pqT1Q1OEJDNGhRMGlieVk3MXQyVXl6eG5XdUh5QVJ3a3Bna1FoNUpvcVdqRGo5dDV0UG4vakZXN1Z0N2pDSmZaWVVPTnBNTDcwVm5zTFNJamd2Qi9MMzUyM0RXZ0NRelJFUnptM0ZxUkV3ZG1NdzUwem41YVJHMUtkOXFyWXdTd1NPS3lWZ1VGcENKd1liRmJSRWV5cjVUOEJqM2NPRVUvZjlYam5DQkxvMnNpc3FxbmRqSW9GZXp2WHVMVnpoOXRYN3ZMTHczZDU5K0VibkR4NmlLK1ZVTTFvTlpFbVZHbkRHRUVqZndzMTI3VC9nMmJ3dFNlbGp1UnJ0RTdzWHA3RE1mUnRyejU3Y1FVVGV1WTRRNTRzaXowUTU2d0pPczZ3TWw5ODlZc3M0NG8yZEVRc3BHbDFPSUxtdkJXMXpUTFJYQ05NNTdPZWlrOCtuRUxNSFNMZWtvd3VvQ2ZLUE05NCtkcExmUDdHSzF6bkNwZlpZMDVOUlNEZzZicWVkVnpqdkxmYU9SeEJQQUZqUzNKcUxDWTZhWnhuOEIyM3BaRUdBWnVtdGM5b0xCMCs1VXFTZkdOZ0NrSmJCTXFMSmMwM1c0YVVEYkZBcUNaclFHUkFYbHFzeERjTnNUU3k5MEVzcUpGaDRXcUNKaHE1eXExd2xWZXVQc3U3dTNkNDUrRTdmUGpvRjl3NytSQjJBK3I3OFZxM1MvVi84eUYyNTJuY3NSZGNsdkUxaytpMFpaVXI3cnp5TE8rLy8vN20vczQ1eG1CUkNCQTh6bWlPWGVIL2Rrb25pZWMrL3dKYUN3OU9IdUFhWWYxd3pSQ3FWUjNnS0g0VXNGLzdaQTZKVkdVcmI3T0JDSlV4OUlBdW53bFM0WExBOVo3UWVtN05uK0hWWjc3TWkvVnpMR2pZWWNFdU5RNVlMOWZnb1c1bWVHZDFTWU4rR1dDM1F3TjZTdTdOT1psb3ExU1N4eHVUSk9aU3RIb0I3azJjam8zUHgvY21mQk5KekZjTHhkL3pHUHZ0cHQvb01EWXBiMWRPUG1vYlRQK0ZBaWZLb2dSMWFNcnN1SnFZUE1sbEt0bWpxVi9tbVZ2UDhJdjlPN3gyOStkODBMMUg2MW9pa2VRaUtqb0Nvb2RXdHhSMHpOYmErQ2k1dFY5QkxtNjZRWGlLcFZFYTBIZGRSOTBFUUhCengrMFhuK1huLytaMTV2VWM3U1Bnenh6SGxhZ3hRSEI0V3docVBGSTR0Q1Z5N1lWbitFWCtnT2lWZGJleXRxSU82MkxwTFJtYlhJYXNHMTQ1TzAyWm1NOTBUc3FwU21UT09ldEYzYStaVlhOcjV5UldKOWZubG5wVzBhNlhCVEd0T0FsMG1naCtqblNCc0E3c3BVdTgrc3lYK2NMbGw3bk9KWFpvcUtBRUJ4SjlpbFNWTHlCUkt3ZHlXcG9RT01OQnBKS01UNVR5RWJIRm15aTgvcXBvc0VCVEtoSERWRGFCUEprL204RnM2Sklodkx3bElzb2d1ZzVEWXZSRnlNYmNtNVlTR1hMcHMyV3BiY2VtczZVRENrRXVMaXN1akVlMFo2c0J5TlRPazVOOU53VFBnaG56ZWNPTkY2N3hrdzkveWx1UDN1WkJ2ay9lOGZTK1E3emdrdlh3bmxXT2s2TVRMbC9acFcxYlVnYnZLMk9BeXVDSFlzaThMVVRxTmhDdnJZNmg1WFg0L1BDNWp6MUdmdnNNdVVESXhEQXZvb1pORldjZE9qTndrbHIyYmw5QkY4THg0VEc3WVVkSmRqTXlQQ2MxYTJLQUd3WVlkbjlBRENpOGQyV2ZscDdrck83SWlOektmaXlHV05hQ0VQaTE1dm9Mb0RibEh1Y2Q4ekNuQ1RQNlB0RjN1ZFQ4Q0gwYnFVTkZqQjNlVjNoWDBiV1IwQWZtM1p4cjFVMis5Ymx2Y3R2ZDRocDdOQVFhS2xRak9mYVdxQ1lnWHNZNk1OV0N2a2pHWWFKZWlRWllzc2drbVY0VEtWblY3UURGVW94bW9TZGJleDlSY3RFNVdrUktDM2xlSWhZaEhjUnRNQU1GWC9KdlEzOXVQeVlRZlBHWnJidytZTmhKVFJtbmpqbzAxSzYyczJRaGxFMGlETWo4QXVOeStOSkhXOHpIekNha1FZVXN3UVFPejdkdWZvdkxPMWY0OFljLzVwZkhIeUE3d3FwZGd6Y01TN3ZzdUhidEdnOFA3dExNWjFUQmthTGwrcndNZHNEam4vSFc2MmVwMGNTd05NRElkbFVtRTdBTktIc2hlY1h2QnVKSlF1TnBlMkZ5T0xYbkdYU0laQXdKUllGbjd0eTJJczZLRGFIcFpEZTV5QTc5ZFF4cnpHNmgxaWpXMFNQR1RPVUNJVlE0UEpwNjYzK0dKMUNSVzFqMEMzYnpMbCs5L2tWZXZmb2xGc3k0d2k0MW5reW0xUTVWZzR3NThTVUxacmVkTkJNMWtyRXVJREVuTzdkQVQ4OWFyV2UxV3JCeE5BNVhhVzNONVd2RHdLOVowNUhOMUJyL0Y4a2FhYU5CMS9xK1owVEJTeDVCeExVUE9LbXAzQnhIS0hpU1FNQVJFR2JVMUZJaEpCcHFmREN4WExObUdaZDRDY3o4akhYSzFENFkvYlFxYUI0M0ZXQU1oYWJnTUNoTUprcEN5QVFjTzh6NTNNN0xQUFB5RFg3KzRPZjg5VHQvVGIwUVlwMUlMdUs5Y0hMY1V0VUxZZ1pOU3UwcjZFcy9iMWZJbEM2U25lRVBweXV3UHkwbTZWR0laVk9rS3RraUh0UExVRVVsQVFiQTNydTh4OG5kdy9MWHgxOUxHQTRnU0NuSGgrZGVlSjQrUnJMUHBCU3RoRUsyNVdxb292MTFqN0V4UlNuWjZJbDJIODZSUmNoOUlpZkIrNXBGcUZnZUxMblM3TEVqbC9qMnk5L2doZW8yMTduTW5CazVKM0xzRWZHNENrdlFXeHh2MURKZ0FPSEI4VnJuanFpMi82L3BhREVjWUFKV3VrSUVldTFSU1VTZjZIeExTOGR4ZTh5SGgvZDR1SHpFc2x1eFhxOXArOVlXblZPa3RMQnlqaEZvYmM5cFUrSGcxT0drSVNlRGhPMHZkcm0yZjVVcmU5ZllyeGRVVXJHZ29jTGpzc05yeGN3M05LRWhvdlFrbkRkenRpNllGSytDWkJuWHRobW5wdTBHU25lZndRdk1KVmpocVp1enk0TGRxenZjM0x2S2YzcnpMM25VUG1RcG1SakExUkRKNUpTcHFwcllaMnF0ekM5OGpHSlNuU2JoVC8zdE05emt0UUFHR1BrY3NRYVZ2Z0pzN3E5ZXY4YnhhNDhvV0hEYmdDK1FoeEg0Tm5JM0NOeThmZE4yNmxKN0ZGTmZFcytiaDQxZ1pwSStVZUYvcGtQVW9MTTVaMnRMU2tBcklTZm8raFpWV014MjhPcFpQK3E0M1R6TFZibkN0ejczRFc1eGphdnNqcW5rM0ZzNWloTmJiSzRJQ0JKSktuU0ljZlpqR0VZRVdpSjlnVVMxOUxUMGhucVhSQ3NkS3pxV2VzeXlXM0p3L0lBUEgzN0ljWHRFSXRMbkRxbUMxWGw1UmVyaVgwb201VWdzTlhxVUVMbHpia3g4QWdXYTFlSmNJS3Z3WVgvSWUrKy9nM3ZQTXc4elptSEJ0ZjJyM0xwOGsrdXo2MVJVbkdoTEl6UFRiZ1NxQVFXaWpsb0RqYlBrOWJDVFMyRXkyNWlwanRwQktOSE5Iald3ckdRcTJjVlh6M1A1QzFmNHEvZitpdGZ1djA2L1dMUHNsdEFJVmUzcHUyVG5pSUlacHhac2NlT083VTQ5MzRtSnhjWmovYlNHQVFZRVVlTnAyU0IwdGtjcTE1bktKbnIxK2xYZTR2V25Pa2ZZZ0JvQlp5SG95MWV2Y093NnV0U2hwSkdqemhBU0YrOHV2NDZoWXMwclVyS0VzQkhhbU1rbklzeG1NK0k2d2dsYzVScWYzLzA4WDcveEtsZllMNlpWVFVYTjBhTmpkbVk3ekdvUGlpRTJYQ1E0SysxUFJUdEZESVhScDU2SUVnUDBMcFBvT01sTGtvdDAwdk1vUGVTOWh4L3cvb1AzT082UGFiTVZ1U1k2c3JjYXZ1d1RWZFdNbTFtV0VuNTNEdkZBVGVHa3hCWXlWa2EwaVQ1YTlETG50V2tjN3dpTFFGWTRUc2VjcEJNZTNuL0l6My81TTZyY2NHVitpZHRYbitYRzVSdnNOWmZaWWNHTW1ncFBMOTRTcnBxb01MNFFRYWdHSDYrYzBZSDVVOFYzcVYxZzFYZVdzcERBZnJoRW9PRjM3M3lIM2ZrdVA3Ny9ZNkptVnUwU0V0U2hJZmF4RU0rVXFPckhFSnpCUVBzMFBMUlJjNVhVeUJCeTJweEpDaTFFSVMwaWMrbnFwUktKVFF5NlNzVlNJcWV2YWFQSlhFbEVPMkcyTStQRTk4UitLT0tNbUw5ZlRKZ1Nvc3c1YjlYUy9EcEdUdUNyWU5WWHBkeEdGVkpXUXZBUUk3SlVic2gxdnZYc04vbnF6cGV4S3E1QW9LSk5MY3YxRVZjdlhjSmxxeEpIUUlMUTVzTGJJVmJhRUJFTHBKRHBuTks1eEpMSU1jZTBkS3c1NGNPREQvajV1ei9oNE9RQmZnWlNRNVFlclNJYXdMbHNBUnZKZUxHQ1FmRitqTkNxUXRaVUZoKzRBbUdEa3ZBc0NYUXB2K2ZZNDRLM21LQVR3NVBtRnZVQ0hpb3lyZ1lWNFZGK3lJTVA3cEhmRlM3dFhlYVphM2Q0NGVwejdNa09DeGJNbVZGSlIwMU5RNFZUVDVKQUtHaWdVSVM3R3BtWkhDa3JWWENzVW85RGNUbHcyZTNqOFh6enl1OVFOUTAvZU8rdm1EVTFqL3BIOUtrbkJJL1NqemxBNDRRWm9IamI0bk5SMVBGVEM0U014em43cDYyY3BxcHhzQlRQZVhGcFRwSTRSb0tub3JYOVd4R3lFWGxRaUVZbGVMSTNmMFpQWlpWLzQ1S0t2b1RNKzBRbzRWY0JmRmJtcnFKOTFQRk1mWVB2UGY5dG5xK2U1d1pYYUtqb1U3UythQkxZM1ZuUVo2Z2Q1QWlaaU9CUTV4aGlmOWxCbnpPOTlxeUpyS1ZuVGM4eEp4eHl6UHNQM3VQdDk5OWtsWmZrMENGWGxGYlh4bGpsUURWWmkyQ1JFanNzWVY3MUl6aFlwVVN6VkVmZURpMTVzREh1WGtwaHRFU0V4YnV4cG1uWWdiTXZDV2NITWJZNEVjT2Rpc1BOUFg0V2VKRHY4ZkNYRDNuLzNqdmN2bktiTzlmdWNNVmZZY0dDR1ptZWhGZlBuSVpFb0ZGSFFrbyswQ0p2V1JWeEFmRE1RME9ua2E1VUN1LzZCWm5NRnhkZll2YktuSC8vMDMvSHdpZFN5SFI1VGRLSXJ3WU03SysvSHUzeDVWbTUvSzNFZlNWVHoycExjeFQ2aWN3Rk5IaXFCQkV0OEI5QkJhM25NeEtaTHBsRDM3WXJzNlZ6Ti9vR1dXMzM4YjhDZ1ZNMTRwZGNpQ21yeXBEeUF6ZUQ1c1M2NjVuN3F1Q1dGSTFLblR6elB2REs0ZzdmZVBZYnZPUmZZTUVDVHlibW5wd0VuS2Z5amtUR08wY0VYS0NFejN0UzJWVWptVDVtb3Mrc3BPV0lGU2VzT09DQU4zLzVCdS9jZlp0MVdpRzFrQ296QjlYRmNkZDFpcG1BT0xJNEhOVUliOEl4THR0eEF5czIyZWFaNVhGcmxNSXZxQnBCSFVMRldKVXRXT0pPemV4UG1zRlo1ZmRBNEpOVXljWC9jSm80eWdjYzNqM2d6ZnV2YzNQL0RpOWVlNUZuWm5lWU1TT29OMyt2WEtSWHFNdjFWZGhjRDdYaGl1QkZxQ3NyNE9rMTBrZ2djSmxuSGZ6OUw4MzV3Uy8ra25kUDNrYXJURE1MclBzVndkY0dYZkllelFxRmxHbW9UZndvNCtQVU1ZNWxPdWVvc3NGTU5vcU80cTg1SmFWSUpsbHpRUVkzS3BrbmRTb3hEYWQ2UnF1SXRaY1ZSalZvNE9FMFN2S3ZlZ3prUE5OSzAwSGdjalJZYmROVXFEcmF0cWVwakVPaldUdjJaWjl2dmZBN3ZPaWZZNWRkSE1ibXBDcDRIM0JlU0NVVURTWk1iYnVpYVJwYXJCZHgwc3lxYjNHMVowVm41U0E4NEVmdi9XZmV1ZjgybmE3eGpSQzgwR2xMMUI3MUlONUMvWmFjQnBDU2V5bEJDemkxaTVkUTI0aGNLVEFxNVV4a0Z3Yjh4dWJkd1hkVEFHK2diMnYwVWJ3THlXTTBXQXIvaUgwbTQyWm05cjF6K0JaM0QrK3k2L2Y1NHAwdjh2enU4d0RVdWFMT2daMVFBNTZ1Ny9HYW1kYzdaWEZhRWx4S2pxMG40b0FxZTV3RXJzaFZWRHhmdi8wdHVuZDc3cmJ2RWZzMU8vV0N3MGRMOXZiMjZXS1BlTnZzaDJlZWN6NXozMXRjSTZmR0p3ZWxid1I3bkhjeDFtdFg1aENVN016aTg5V20rWVMxdTJYa0c1MGlYRVloRTZlSVUyWTdjNUxMbzFOM21oUnlaR2JhRHZwODdQRkVza3dka3JEbG9uT3B5eXJSTHVlTUE2OUxDUW1lYnFWY2RaZDRwcm5LOTEvNUhyZDVoaDNtb0JZRnM5bnFDVVd6eklCMVd1TjhnOE14YjNaWXBoVjRUMGRMS3oycGhrTU9lQkFmOE5yZDEzbjkzbXQwWVVYZnJJbjltbm5Wc080TmRqYWJOY1NjUzk1cE1NVTNBRklwVWJzeDVGdE13NDBtOHlXbERWcUNEbG9FZERvMktKdFUySU0zVEZOWnpiY0d5S1hZMUlUYWxjWUpNdVpZdmZQMGZZOUtKRFFWYlg5QTd5Si8vdlovNUxYbVozenRwYTl4elY5bnorM1EwN0xRaGtYVjRMTG5wRnN6cTNlUlhKQS8zdUVrV0c1U2VycXNPTFc4MkMxdTRyencreS8rQVgvNnhyL2xYdmRMMXUySy9mbGxDNFE0UjR6SkFqN2VFVFhpcG91ZXoyYUxQM2NkRDM2ZUZxR1psR2JtQVRBZ1VNOXJSdlNweWhsTk9naGJNQTFoZDZFQ3UvdDdaRFZvdjZyaFBhUkFyazZuM0Q0dFFYdmNtR0lpcDB4WWd6a1IyNTZzRVY5VlZNeHhVZGhoaCs5OC91OXhtK2RZTUFOVlVqVDJWeWVtWmN4Z2l5aEM0ejI5SnJJNFRyb2x2U1NpVjVhc1dMTGlrR00rUFBxQTE5Ny9HUjh1UDRCWkpOWTlLaTJoVXJMdlViVldVWWxJbnlLcW5ubytvMHNsa2dKQVJ0eEF4VjBZc01hY0cyTUVhK2gvdFptRTdaNENNQWl3V1JuRHNjZGdnVml3L1V6cnFTSjhva1VRRlhMcUNIVWdBMUVqRW9UN3k3dnN6L1pZcjVlczN6amhsVnV2OFB6ZUMreXdTeS9XTlhuaEdsUVU3WmJNZkVNZExJZlVaNnVnOWw1WStKb3VLWDNNaUZNdXUzMXFITjk3K2ZmNHR6LzUzM2lZSGxEVkFiU2xqeEh2dFZ5WGtuTENGVHFBSWZneEZFaEsyU3MvVGI3SHFZYThhT01mUVJoaXdmekZZa0hQeXY1V3JJMUJDVTFIQ2VGdkN2NzI5dmRIWWt6amhoOGVtbEdlRGZhL0ZDbFA1eHowMHh6T2JRcEVUMnRWVWFoY1JSMXFVaFNxM25ON2NZZmZmZTUzZVk0NzFEU0ZkU2xiNllJVWlCQmFDaDR0T3RTbFRQQU5YYmVtMXdoMXhYMGVjTWdoaHh6enhyMmY4b3NQMythNE84UlhTZ2lRMDVKZUk3NE9sa2NzQVpqc2hWRFBTTkdvMjRJWVEvREl4VjZFRWRRaXV1aVltWkZwbll0T21HbGxTbU0rd0o5TXd3MGZHNERSd3dJczd4YWtRc0VCdXNKek5VUXJGVUlXVWgvSlFjQWJ1Yzd1bFJuTDVSSDFMUEJodCtMd25ZZDhlUGt1WDdqOUpXN0pMUktaZGVyWkN6TzhRcGRiWW94NFh4bGR1QXVXN000OXdSc3I4N3JMVkFIMi9RNXR2c1R2ZitrUCthdDMvcEkzNzcxT3RWOUJ0bzB5YVRkYUxRTklZZ0FJRDZIMlFSaE85N3o3T0dNZ2FkM000ZlJ2bTN5b1RhQk9nQWl3Mk4zaG9hektJeXBmTnV0NTFJWk8zY1JjTEx2RVltL1hUTVhwZ2k1OGdxZERtb09mOGF2UVpGT084ZUY5UVFqQjBTODdxbjdHUE03NTVvdGY1M21lWmNFTWp5Y21JenZ4WGdxK0R5d0phcVVkUFlZR09ZNHRYVEVOMzIvZlpkVjB2QmQvd1ovOTdNK0l2cVhURmRXdXd6c2w1dDc4d0FoZFN1Q0V4WHh1TEVkZFI4WW8xTHl2ME1Kb1ZFbzRNWlBCcmtGRkRKVWl0dWdWVzB5ai9UOHVnT0hKMmZlQWtiWFlGcU1GTXBoOHloV2h5NUpCZlNtVHlSYlhMQVZvSWxZclpYNXVJdWRFQ0k2am8wY3M1blBRUk1vWlA1L3h6cU8zK2VEK1hWNTk3dXU4Y3VWbGtyYzdpdXFaU1NBNEMzLzBoZ25BQ1ZRRjU3bktMYnROdzZwYjArZklsZW9TSHVFcnQxOWxGVmU4ZS9BdWl5c0x1cmkyVGNjcFBuZ0xnSlR5b0xFdHhpQjRmSHJyYmx6RE9nRG5kSXNaMlZvYTZCaHB0K0NSc3RpWjh4RElZbGlDNFhtY2hrSk5oQXlRek03T2ZMeVo4WWZOcXgxRGk4a3pBRm8vdXpIVllzQ21TWUlxT0tGTGtUb0hMdVY5dnYvcTMrY0Yvend1WldydldLMlBhRUpEN1ExUWEyaG9pRGlya1hPZVRxRk5hNkpMdEs2bHhYeXRQM3Y5My9QMm96ZXBMd2RPbG8rb2FtOEE0R1FUdVZ4M05QT1pVVHJIUkx1MkRpbTF6RGFkUm1KR25JY0NyeDdNUVZjZWhtUkYzWWJOMTBZK2QzY2VmZEx5bWFHK2JPQlF0RHI0Z2xlWTBOY04wYk9NN2RvbEJWd09tb215Sm1wR3ZLZjJOVGttOXVaN3BMYkRlYzlpM3ZEbzVBQVhHcUlJLytuTnYrVEIwVU8rK3Z6WHlISVpkWE1UenBob0hNd2NPUEdJS3BJdE1yendNNk5KOEdMNU96SnpyWGdtWE9jckwzK1pvNThkV3BBcEtSVGVFSStmbU1JYmEyWnJUZ1ozNVpNRVBIVHlzdVZQRlVCZHBsVFNEb0dsTkg2bWFwcW5Pc1VXckVwRXFHWlczcEFZbXFrcm1qY3NnRU1RWXFnUituUnk3amFtV25IY3BUVGpuQzljL1l6aC9Kd2pvbzUwbkxoUzMrUjNibjZERi8xelhPVUt1RXpicm1tYXBpRFZGVW5aVEVmQjRMaE9pVGc2N1lsZU9kWVZLMDY0MjMvSVg3LzlWM3l3ZkFmWlNSeXNENm5uanFFL1ZTNE9idE1zYUhzcis2bHJXNXlVaEdyT2VXeEVNRHcycTJRdWVsUmQyWTJualJ3MmM3QVpHNEViVE13Tk1OWnlhc1BqRjZlbHlEQnYzbVN5RzdQbHRaVXlEdHVWamMvZDBmZTk4ZWhuZXkvbnpIcTlaclpvMEJ6SUpXM3lpK05mRU4vdStPS3pYK0JPdU1XZTIySEhCUXRqcDlaSURueWdjcUhja3hYN050NlFrRGxIOXR3dUNlVUdOL2pPRjM2WFAvMnJmOGQ4VWRPSjBtWHpvWWY1c25XUkp5aU1TVlQxTXdIUVd2VHd6THNUeTA1ZEp0U2VKQkdvVUFXdlJqRngrb3FDd1pETTBjeE8yZDFkV0RNL0dYQ0xDVHc0QWtvcXNKRThYc1FudTBVMytpTk9MZXliMVJLL3FjQnV2Rk5TYnExQU1TVWEzN0JjcjJpYUNyZUdxM3FOVjY5OGxXOWMranI3N05Hb1VVOUhGeEd4U0oyU3NCaURZcGgycFZVbFNpSktaSlZQT0hFbmZOamY1Yy9mL0RNK1hQNFNkaExSclNGQWNvcElIbms3Rk9pVDRuMWxLSXNVTFVVM09PYkJ0cW1OQkdEKzBtQVd1Z21VNkFrbXo3anBqRnZ1MUc4Nzk1OTJoNmVPbTNVU2ViUmRFc0ZUU2RtdDFkb3pxWmcyaWNQRmliY09sVGtadlhlT1ZDSHdRYnZrNFU5L3lUZGYrUTYzWjg5eEJXVlhkb21TV0hqamZXemptcDB3eDJPOUZTcG5sQWZPUVpzU0M3L0xkWXdQNWZ0ZitRUCt6ZC84Yi9nZFIyZ016eG5OOGNRN3RXcUxvdG1zbjE0Z1p4NkQ4SHRhbFA0UVNYUWxlRkZRVGdVY2JHNTB4b3ZWQVVwNW5tMWNzM041UVhRZHlWV0UxQ0FhQ2tXSGJYWUQyOVdvaHF6c1FKQlFJbDVEYUZJWStRNUdmK0VqMzhqNVk3b1FOdTc4OWhpeGxjbVlwTnEycC9aenRQZlVhY2FMK3kveHlwVlgyREZRRUxIdElTV2FZS3JjNDFFVUw1NHV0dUJnclQyOUpCNjFSeHh6ek5LdCtORjdmOE9mdmZZZmVOQi9TTjZKVUJsNGRhZ0UxN1F4czZ4NmRqc2tQNzJuclFVL05HNXdnMkdkeDU5ZlpidmUweGJDdEV2TEZuVEp5Wmo3VVMrSUQyV2JTbmp2OERWbzFSUERtdGF2K05FN1ArSm5kMy9PSVNlY3NFUjg0RVJianJvVHFqQ2p5NGFzR1pBNHFCRFVzSkZPWVljZHJzazFybFhYZWVYbTUyaVkwUjkzMUdFR0tWUDVvYVZYWktDQUgvS2tIeWRoL2JpNUdheUVNVkIwcWlMYk92a001UW1DQmlrTlIwd2dCejk0TSt3NFlSUHBzak9GRURibDhZUGs1ckx6ZmxvMVBOT0xLRGRvMXVCMnBNZmgwR2dGbHgwZEtTWEQ3UFhRcERsWDNBMis5T3lyWE9NYW1VeFBqd2JUT0tKUWk5R0tScFRPMEttYzVDV3IzTk02cFdzU0p4enoyb09mODVQN1ArWklENGgxUjlhRWl4NmZQSFUxczhBQVJ1VHFwWlNwcXlFZ2N2RmxoMHovUU84OTFDWEptQi9ad0tLazJIQWlwNTdKcjJFOE9YZ3diQXFLYzFaWm5uTTJxanFmZUhUeWtIN1Y0MGw4K2NZWHliVHN5eTZMK2hJbjNZcktlM0NaeXZsQzVxTlc1U0NRMVBqOEF4WDdYT0lMdDcvQXc5Y2UwdnVlazhNVnM5MEdKRm9qbEt3bE4rb0xWNG9IWDJyY2ZnWGp0Rm1xSWxSVk5VTGZ4dGthQmRhMlZEOUVlemVJQ2d6eE1Za3Nqa2lQaVlCOVdodXdsQ2piZVJyTUs3aHNyRXFwejJhYXFVT3laKzUybWZlN3ZQck1WN25EblJIV3M4b3J4Q3ZPSjlCVUhvb0ZBdHFjV2RPenltdmFxbWN0YTVZc2VlM29EWDd3N2w5eklrZFVldzRKQ1NlSjNHZHFYOXV1eTBDV002QlBkRk1DVW5KU050emtwMFFUQnhTSERQY1pjU1FNQ1hoK2tPUFhQYzRFR0VxWlRaYWhXTFVIbDVDZ1ZEdENLOGY4Nk4wZjhOTUhQK0dZUXpvaVMxYkY3TS8wV29wRXhJd25LVE1xSXVSa25DUXpHcTdJTlY1OTlpc3M4b0lxZVRSbll1b01vSzVRS3Q0S1A3K01mdnF2ZWd4YXRLcXE4d1dzakJIeE1UclVaYUhYZGMxUzJ3S2wybFJFZjNhTFlZcTZOajlzUUpRNHRVTEJYbzBoUzlTejhIUGswUEhsbTEvaDg0c3YwekNqd3N6Q05yZEUzOUU0Szk5d0dISStZVndhaCtzbGJtYlZ5d2NjOHZxak4vbnJ0LytDRTMxRWFHRGRyWEFPbXFvaHFxUHlGVjBmTFhUck11bzJ2TzllU2xaYkorcElKbjI4eERGMGJ4ejlMakZQWjdNMnRvWHkvSG41Yk1kRm1telRqTkNnWWVhUFczeGVTdjR0NXBic01uN3VTVW40MFhzL3BQWVY3bExnRXNxbGNJa0VKU2dFalRNTTRPQmZCZ0s0VEs4UmtUa1o1WVg1Uzl5N2ZKLzJzT1Z3ZFVDdUU4NlpGcFUwV0FCK25OTUJ5ZjlaQXRjSFBzbHB4QjNKVlBVRXB6aDFsVThKM2xhZUxLT0UycHJHcVp5aklrZjB4ZkRPcDJEckRCcHo0aXNZTVdSQzFaR0hTSmNxc2Mra3BmTDg0alpmdnZZcWUrd3dvNkpDV1BVbk5GWE4yTFN1aE9tajl2UnFERnk1ZHZUMDNPTStyejE0blovZmY0MUg2U0ZoVjRpbEZWSGxnNVhQZUUvS2VTTmdQbzN4Y0tVUXJPQVpTTmhLL1BEVVpEdVFvU05tSGdzUXBTU25WWWErTGIrKzhUUnN6N2F3N01FNGgvVTRTNUV1OVRoblZBOTFNNk05WHZQRGQzNUkxMFZldmZFMUhKNEZjN3dJb3BhcUNCT1BOZUR3NHRGb3ZwY1RUNitSTDk3K01nZnhnUFhKQ1gwdS9jaEh2Q1hnRkpHd0ZVNy85TmpTTm5qUnJXTVA1cjdrOFZ4RGw4MU5pWTQ3ZDlNS1E2WmFCVlcxWnQwNVpqWTl1RFltNDNrbi84U1ZxdE56dU0yL2h6UkJkZzVKRU1LTW9GQzFGZC81OHJkWjZJeEdyRXRLU2gwU0hVM1ZXTTVKRFVPZnlQUXFkSkpaNTQ3czRUNzNlZVBlYS96MDdvOTR4Q09xUGV0Njd6UFUxUXp2alFMTUI0TXNTU1VGVFQ5RTdDYk1VbUxkVFd4dWJCRU5QcG5kaUN2a292WVpROElQTlhnYmMzeHJEc2QvYmllZlA3TXhBUTdEUm9QSitLQmRzV2pLNVlrQm56T0FNOVl1VUk2T0g3RTczMk81UE9FbkgvNllqT1BWRzE5RkN3UXJxS1ZlMGhBM1VGZTBXbUx1Sy9vSWxmZnN1RDNXMnZLbFo3L0UwWnNISEtSN1pHY0pKVVlpV3F2dGtvRWJZVEkrY2toZk53Ujl0c3kxek1tUTI5UXpFZURNSnNvcGc1dzRPVTBMTW80ekhkd3VpdGhNemNXQlEvN1RzQ0ZIYXEreE9MSGNWS2xFbG15MmVGcG41bW1Ycjczd0ZTNnp4dzI1WkEwVStnN0ppVXZ6WFpJbXBPaXlwSWtvbWQ1WkQ2L2VKdzQ0NEY3L2dMY08zdUtndjB1Y1dlVExpY05WRGV2MW1qclhHMjFPQWxjYXBndWdpZzR0ZTdlRW9KRHNqUDJXWFptZklmbWJUYXR0a21ZYnMvSVR6K0NuTjhhOTQ5UkZpZmpTekNKYjAvSlNpbExYbnI3djhBTDFyR0xaclpqdnpEbFpuZkRtMFp2c1g3NUVxQUpCcktWVEgyMGgrOEpEWnlKVDVsSUVraU9reUc2MXo3NHN1YjMzTEVkM0QxRTE3aE1KSlIrb2tIT3lXZjlVcmNSdExlWks4R29ZUStuU3VER09Wc3JqaHhNMkt0WTVTMGhteWRZaVZhMU1mcW9peHhQSzAwU21ubWFZVHpLMXJjZE9HVmdZR1hYa3RlT1ozV2Q0NmRMTFhHS2ZPUUdKUFpKTEUwTEFKY0ZKUU5YVFpXZzEwNUk1MFJPV0xIbVk3dkVYci8xSDdxMC9JT3dJaVE3dkxaRFIwMUl0UE5va3EyVEdXSEhKaXVTQTB3b25EVUpORlJhb3hWWkFFd0hGcTBDdlZLbWkwUWJYQlhRTkxnWG9GVitpa1VFOTJpc2hOM2h0dU5qa1BqOGc5R2tQaTMxdU9vb09FRENudzgvd2ZBbzlIQlhCVllnTVhJeUYrMTg4b2ZZY2RjZkl2dU9YeS9mNHM5ZitJeC9rWDdKaWhXTE5TV0tNaGYzTVdkNUxEZTdsblFVTEsyKzkzSGJaNWZNM3ZzZ2xmd1ZwQTE0cWNzWjZ5VlYyelVyaVBKMHdMWXY2K09Oc29HL0VVNHB4YkU3UlI0ODdsOXVxMVpLelNQdnBTVDZMTVVLTkpqVkVBOThGM3BHU1V2a1orMkdQVjY2OXdsVjNoUjBXcExUR3U0d1BVbkpRVmduY3hzNWd2OEZ6MUszcDZJaVN1YytIL09EZEgzQzN1MHVzTzNwcEVaZEdpQlBtMnBNTFM1U0l0eDJZbXNwVmlEcGlsd2dTV0orc2tjNno2eGJzcGhsaEtWUW5qbXZ1Q3JOdWpoNEllM0tKZlgrSkpnY2FWK015T0ZHY2FIazRrTkoySHVaWFBzNkpHSjkzTlFNMGF4TU1NY0VUZFRpcElWbTVUR2dDelc3RjRmSUI4MnN6MW1ISlg3LzFBKzV4bDRmNmtGQlZTUENFVUpWb1k4a1g1bFI4UFNzRXJ0U3h5dzc3N1BPbE82K3lJN3U0WEpjTlZPbjZOVWhFTUlHZCttTlBXdkFYamRNS3czNGR6UDlQdHRtRjRlS21yOEE1Rnp1VWJIejZpMkphd2pBOWQ4NFF0RUxXbVdjdlBjdXpzMmU1Vk1oSHUyNUpYVGM0MkhCbE9ITTgyOXdUZ1RCcmVCQWZFRVBMWDcvelY3ejE2QTNhYW9tckVxbnZtRlZOMmFFZDBZK3BadHV0YzRWWG9YS2VrM1lGamFkcEdqUXJqWnN4OXd2eVVjY3NCUlp1bnlvSGR2VUtWNjVkWi8veVZiTDN2SFAzYlg3Ky9vK2c2WEN6bmlROWZUYkFycE5BRlNvNjdmaFZSUkkvMGpqRGZiSHhNMkY0VmtaNkNoQ2FobFc3eGdWaHZqdm40UEErbCtkWHVIdjBQbi82bzMvRFAzNzFIL09JUjdpMXdtd0g5VlpkYjFVSzVVUUNHaU5PaE1iUFdMRGdoYjBYK09YdSs3eHovRGJPKzZKRmVwd0xPT2ZwazdYelBTL2c4VFJCa0k5aWpXMVZnSHlFQU1za2hGOEtESXNqdk5XZ1lIQ1RtTUp5UHFWUmJHek5obVlmYWE1elJucWhjVFZoSFhqbDVaZlpaWWVHR1pvejNodTJ6b1hhZkI2TmVBSlJFc2xsVnFramVxVVBMYTg5K0RsdmZ2QnpkSy9Iell4K3dJa1F4Q0tKaWxHT2kxcGVUZFRoMUxnS1kwN1VvU2FxQlVkaXA4eWx3YTBkbDduR0lsZDg4ZmJuZVA3SzgxaHIyUXJQakJOYTltN3MwSFhIdkhQd0dsMGZRVG9yYlBRVnFVdjBmY0tGenk3MC9NUXhWbUp2TnM1cFFzV3dzYmw0VDZmcFlZWktiMC9XaUtvU25QbG9RWVNxRnRyY0lxN2xJSHQrZXYrbmZPdmFOOW5aV2RDckFac3JjU1MxWnpsUVBZZ29GUTdKaWJtYkVlbDU0ZnBMM0R1NXh6SWU0NnFBU2lyYXo5YkxOT0F3WGZ5ZjNHVDhPT1BzaGhrMkZ6RzkwanhHV2FZNXRQSFBuNnFnR1h4cElQbFhTdkpUYW9JS1llbDVZZjlabmcyM1dEQXpRYy9ncEVaRjhlTG9Vb3Z6bnA2ZVpiZEU2b3JPZHp6b0QxaFhLLzdtN1IrUVp3a05pWlFpS1VXYXNDQkdOUi9KQ1JVMVNRSHhPQTJsclpJdEJsUUptTS9Gc2JLL3M4ZU4zVnU4ZU9VbFh0eTl6UTR6QWpWS3hqRkRjV04zNk9ldlBNLzl3dy93b1djdGlTNTE5S1YvZ1BPMVJhb21qZjdPT1BLZnRUazVORjRmZmg4NDZNc3p6bnBCY0VZZFJ0ZXVrSXh6SlFSSDAreHd0RHhpTnA4Wi9Hbm1TTEhsUjcvNElkZDJyL0JTOHhKUmxKcUdqS1BMbVlWM1JLeHhRL0JHWmRCR0VEZWpvK1BPN2gzdVhudUJuOTc3TWFxQ256ZkV0Q0ttRGg5SzhTZ0RsY053dGY2ekZUREpHenY2ek5pV2x5M3M0dkRINFNQblhlU24zUkpKeFhCOTFoekJ5aHcwVyszWDNDMll0VFZmZmZiTDdEQmpRV01rSmdJcGc3amFvb0RPM09DV1dIb2pXM2ZrdnVyNW0xLzhEVWQ2QkhVbTVoWk5tY3JWaERBakV3eUlMSjZoRTRkWHNkQ3d5L1F1b2w3TUpJbFE5VFhQekc3eWxadXY4cjNuZjQ4djdYNkpTMXhqbDh2TW1MUEhMbUV0dUZaTEo3TUZ0eGQzMksvM2lTZEtXaWxlQThGVnBab2dubmtndnlsalNGZG9pZlJ1RUN1YmZCRVVMaThQVlZVUnV4NUptZDFtaC9XeUpQWVhnWVAxQVcyMTRpOWUvd3Z1Nm4wamdkVUVWQ1F0N01MWk5qVHIvU200blBBNU02ZW1wdUxGWjE2bWtUbTVWU1RMV013TDIwbmkweitmL2pndmJqRmN4elpkaDFDQ1I2ZHR5Mmx1N0xOV3RDcUc1amZDbUEzU1ExVFJCTklKejEyNXcrMXdpeGtWam9HWFgwQUNLczZTbGVJTTdaRjdxc3F6WXNtU0krNjNIL0t6OTM5QzJQSDB5U3B1RzE5UlMyMjg3S0VpZWlINjBqSkhGRWNIc3FiM0s3cXdaaTB0emM0Q2lUVTNtMmY0enZOL2o2L3RmWlhiM0dDWEJRdjJTZG5UcnhPU2hjWUhpeVJpaUlZRmV6eC80eFhtL2hLTjdDRjlBMnZCUjA5bCtuR3ppRDkxYk9qVGpCSTlGRGVDbTdVOEd4VXRyd05mZlJ6L0RRTWpWMFM4b1RwbTFZemw4Y3JLV2tKRnUxclRkUjE3bDNaSlRlWWdQdUNEOWtQV3RIUlpFU3FRaXJidkM0OUc0WExSUk8wY3VZc0VIRFVWKyt4eDYvSnRLdGVnaVUxVHd3dUU2YWtGN0NubmZPdDQ1MFVDQy9mTTVBdmp0VTJqaXpMMmt0THpFUjhmNStLZVBGeHBzbVpBUytjTUZTNmRvQ2Z3K1p0Zm9LWmh6b0oxYjQwSTFRbWhEaU1IWGlJUms5R1RSVW0wdEJ4eXdBOWYvMHVZSmRxOHhGV0dtUlBNV2U2VFViWmx4NFlMbzRTVWxRaVVIVE1IcXE3aHNsN2wyODkvbXkvTXY4QlZyaktub1VxT2srVVJtakt6dWtHenpXVUlnYTVycWJGbTZEZjNuc0YxRFM3T0VLMFFxYXp5TnFYTmd6bjlrSDZqeHVrTDI5UzBEYzlnS0xxY3pZcVptRE5Oc01yd1ZYOUN5NXAyMXZPZjMvMFJIK1M3ZU85WXN5WTVXTWRraFo3bENRa3dDek9DYzdnczdEQW5FSGoyNnJOY2JpN2hra1dkRmJlcG1OZk44eDFnVCtMMHFkZnBzTUZ2U290TzNldUkwTC9vQ0JlY1IvS0F6dDlFRjFPeTJpc24xaGh3RkRncHJRNCtxbkJOZCtsVFA2TE9ja2hTazUzUWF3bkpwOHdzenJqc3JuQnJkcHRkOXVuSnBPeW9td1lyT2xtVGRZVktKTktERTFZcGNVTEhFVXQrK000UGVkRGZKYkVrYTJzYmlYZjBaS1IyU0loQUJMOUJNSWdJTVVaY0VocTNvRTR6RnUyQ3ZYYWZmL0Q1ZjhEbjNSZTV3alVxYXZwK1JVd256R3VsQ2RsNlF3ZG5BQkNYQ0Y2cE5MRWdzTStDYjMzeDI5UXN5TW1SeDhycDNnaFoxVFJCU2xaNWJVQm9CMGx3b2lXUFpUL0QzSTBhNVpQK0REQTBIYzVSZ2x5bExlLzI2NFNJVHV5em5rSnU1QnpSQzhrTEVZUENEV0JxWHpseWxlbXJ5Q005NEM5Zi93dnU4ajVyamtDc3dpRU90V0xaRUVjZGlWRFhJMjV3bngxdXoyOHlaNFpMbmo0SmtZRlMyeU1hME9qUlZEWnB6UmkvL1RCZlEyNDNzMDFTZEw3UE93MzhlZGx3WWc3NEVJc2J5RmgyczgzUHVuMU05L2lNOVNUYlBVMCtGL051dUlISGpjZHBRMVdsY1ZaVm5MSHM1MnExb25FVmM1M3hqYzk5emJCdmVGSldJNnNwUVlOTVFsd2lXeU5XdS9sSzZNamNYZDNsN3NrSDVLcEhqQ3F3WURQZDVENFV4RURRM25zb0tJYTl4UTRnNUZieFhjVWk3Zkt0bDM2SFcrRVpkdG5CYTRWRVp5Mk1nbElIOEpMTGRlU1N6YlVhOHB5U3RSZVNPWmRuVjR4M1g2d0prNGdRS3NkcXRTSkg0OFN2dytDcmJlcWxQdlBvMkdOTlZYZnE5ZUl4TE9Bc2VXU2tIazhoeHBxY3BLZVZsdU40d0FmSDd4RnA2ZW1vWnMxSXIyNUFtZEtFWTd4RXBhRm1sd1ZmZk80TDFOcXdhSGJzdXJLZ0pEUmJ6blZEdWx1MDJRQnllTUkwampTSHNqbnZwb3Zza0lzcjJveEpYbkhyUms5SFlPM3p3ZDdjU09makVub0RRSE5nOXREaGxPY3RoQ2tjNVJ3UTZrREVNLzVkd1hsSHpCWmR1bFR2OGNMTzg5UWxhcWM1VXZuYWhJR0FLS1Y4UGh2RHJ5cVJSTXVLOSs2K3g5SDZpRHl6UmhrNWwwNmlZcGZxMk5RQWpWUnpNWkZWaUdxUnNxWUt6S29kbnIveUhDODB6eHUxTnowdVoxTHNhV3BMQVRoRFN4cEZRM2xTUThEYmNuMkdJTjlod2EycnR6aTUvNGhlclJMZDE0NHFXSnZVbEl6RmFxQTNHRkhmcDB3MVBmWDZtekxHUlN4c1lHakFocHhtK0h0bTJTOTU3OTQ3UExkN2g0YUdtVFQwdWJQQWs3aXlEaUtVTkVzbmZVRWZDVGZtTjdrMDI2ZnRXdVBPZEFsVWl5YmVuTXFxN0cxdEtreEMxK1ZDUzM1djVOYWZCbEVLZG5HOG9lRldWSG1zVHJwZ2pJR1BpNUpybjFhazVyeW9qMkQxUXBVM3hLRkdXRlE3cEJhZXZmb3NNNEtGTzZMNVNFNDJIU0hCRnFTaG1UTFpSZGFzdUwrK3o5MkRYK0lxcklQSzRHT2V1WWZCRHpTK2tLcXFxRU5GRzN1Y0JPaUZ0T3o1L00zUFVST3NPV0EyeHg4UHdZWFNSbWtEbGhYeHFGcWRrNGp4WEF3b0NVL0Z5N2RlWmlZTnRkWTRkZGFucTVxTlpucmY5MXNiMG1tR3J0L2tZUnRDT25lZHFDcEJySWVhaUtJdWNmZjRMdThkL29LV3pqcU9PcUhQU3E5V2hqazlUbkExbXNBWGMvV0ZteS9DTWxHckw5Q3Z3ZXcxYlRNUThBeDFaeHRNNlJQR0o0MHpuREU5M2VhL3B3WHRvd2pXR0JHYytBMURNZUtUZnFibkpFVTBKYnhVekpqeDhwWG5tVE1qNE1rcDRzVFV0a01obVVtU1ZJZ0kyU210OUVSNjNyMzdGb2ZkSTJpc05MeTQ1T1ZtRFNvbEE2dnVZR2VuRFN1U2lLTUpjeVFLVjVyclhPVUtjMmJNYUNCYmk5bXFNZzJXNGphbU0wM216SXZETzQ5ekRwODlGUldYdWN4emw1OW54Z3d2bmhnamJkdGEzczg1ODJNS04rTG91RC9GZUpxNS9xeCtodk1QZU1kaG1QbG93UWlOZzQ4SFNzVFA0RVNQZWZ2QjJ5eFpzbVJsTkFmaVN1ZlJBbTBEd0dqVXZYTTBNbVBHbkR1N3o3RHI5Nmh5c0Y3WHN2RmRCMEVmbnNQMG1xYlg3UzV3ZFRiM05RRURQNDJDZVV6aStFd0lmL3pPcnloVHZza1gyY0owcmVQR3pqUHNjWWs1Yyt1QkxGWlNvYVRpNnBwcE4vU3p6aWhyYVRuaWlQY08zcWQxSFoydUlDaldiRUhHNmw2alluT2JrZzRSSys4cG5JbWFoZGdyTTFudzljOTlqVjEyMldWQjdMdFM1MmI0L0Y2dE03UnByMjNlaWJHU0lXZUREV1doS2ZtZVY1NTVHZDhMcVllcWFpeEtwa2JJUGRBYWpNNzBZNkE3bXdERloveUFQc0tRb2xYR0lBT01HcDVzREZtWmhBWWwxVDBmTGovZ2ZtOTVNOFZCOE9QekJITkl2RGtIaEZMRnNNc3VGVE9ldWZ3TVBucDhudnF0Z3hiTGt5Q0Mrd1M1M1FHRnN2MGNuc3BrbkdpMXJXVDBWSk05NmNUYlovMzRhallOWE83ZTRha0lxZWFGNnkvUzBCRHc5RG1hZ0tpeHhCczVwK1hIZW50a3RQVDBkTHo1d2VzYzlRZTRoYkpPeGpSbEhQOU1LQU9tbzZoekQxVmwvWVpDM1JBN1pjWU90OTF0OWtzblR2cE1KWjdnQTFvV3dsQzBkM3JPSE50ektXcXRaT2ZNdVNMWHVMNTNFNGxTU0Zlck00STY3S1FiM3NISlZJK24rZFdnOUo4OHlrTGNtbHBMcDFoUXpUYWRFV1FzU2s5SHFudVduUERtaDIrd1lrMUh6MUFNRzhtVURtd001SGtPajBSd0JFS3FlZWJLYlh5dVRIaXpFZTBNVk9sWk9QTnp1djQ4VHlLcnc1Z0s0L0Q1MHdMMmNRVDJZMkoyUmt6SXgvdjZaQXlGb2lLZTFDZHFiYmk1dU1rT08zaU1uU29WVG40bmxtL1BJdlF4MHNkSVJ5cTltanRlZS85MXV0QVM1bjZyMEhMMEFVL2xvbng1aURGR2tscHo5T0JyQWhXWFo1ZXBxWEVJM2JwblZzOExLc1NOVG5oU0pVNjF6dUQvVFFKSlpKaUhCaGUxdEpBVnZ2RGNGOWlwZCtsV1BSb1ZUUmFWR3JYWkU4Q252MG5heThiRndwNkxOaGZNVXNGRG4xdFM2RWwxejl2MzM2Vmx6UWtueG9lcGhWTlRyRFNJWkZ3dGxYZ0xmS213Ni9lNXZITU5Ud1hxQzhqYnRHU1cvSlRLWWpPR2d0c24zY3RwY2JuNE9VdytwMjZUakFZejNXS01aL2puUDUweHRHQXE2bnl5aTZTVXJDR0VWdHpZdWM0ZXV6VE1pVDNnSGE1eWVIRUZVbVVKMEhYcThYVk5KTEdtNWQyamQxbHpndGJLU1hkTXFEZmFRYndmQmNBbXg1aVBVUE95dlRpaUdsaDN0Vm9SeEJLZlRRbTlrQTFhNVp3cmk4V1h1dXNoSWlWbEp5MDlCSExwRFliUkdJZ3E4ekREWmNlY0dmdlZQcmN1UFVPdHRmSDRwNDBQSE9Oa1I4NmJvTTF2K2lpOURzY3hvRVdHTmVTY3M1NXl6aUZCeVVUV2FVMmVSZjdxN2I4dVpMb1UwbG9qUS9VSWxmZEl6amc4SkVvS0pPQTBjUFhTZGJ6VXRwQW5oV1VqZlVicEtUWlEyazBKU3dkcjhra2JsbU95U1EveW9CdVpNU3Zyckp5Y3BUdThZRHorNFE0UC82UERVazZYREJoVzBWTzdPVGYzYnBWbWZjWFdMajdWYU45VCtsOEZvUzlZeFphV243L3pNM3JYRWFVejdneVJNN2MzY0psYmlIeGdsTnFNTE9DY1ZmTE9nelV2SC9TU0R1VTRlYk5MRG9JMnNDOFBERmJqOFhEbWx6bVBTNHJQSU5uUk1PT0ZXeS9oazZIeG5UcFNuNHRXUDM4WFBpMXN2eG5DOXpUUDNqYlhJVktxbWtmdUZEOTNySFhGd2VvaFJ4d1JhY2NtajdIclNURnVnaXZEVDFtOWpTeTR2bitUZm1XOURqU2w4Zmgyams4dnpXRlZHZHZ2V1I4eW1kVFlsVEg1WFRtSFNHZnJzNmNmNEJnS3ArUkNkUE42N3Uxc25NYUx3cm9XaERBa3ZXU1A3eDAzZDY0enh5cUdrL2JnQjA2SEllT2VhWE1Qd2RQcW1sNGloL0dRRHg5OUFKZXN6OWFHc3RwQ1ZCdnV3OU8zTkpSNkRJNjZFc1NNK0hrb2lIOE1iUzNlK09WZHlUZXJURHV0WVBteEtaNXVrb2NycFZKVUlSQUlPUFhjY0RkNDVzb3QzajU2bDNydTZGS0hGdTJYYzBtUWp4VVFtMW91SFdFK3d6eithbmdITHhwYS9wT3dIVjRSdTQveUJ3dFBhY2xEYlNKMlRxejVmRS9IWWZlQVI2dTdYSmxmWWtFemJtaElJVm9kYXRiRVdVY2FnUmt6cmwyNkFka2JvMW5xTENjNjVNWEc1VHpOMThJd2wyTVhoL0xoRVd3OC9tK3kyV241L0ZhdTJUM1JyTjk4a2ljSTJEbEJqWSt5Z3o0SjhhR3FlQW1FNkpIT3Nlc1dWRml2cTZnWmRVSXNBWTZNSyt6OENSVWxFbG14NU1PSEh4QjJIRm1NWE1VNUxLOGl3VUxJc3FtVkd5WjltQ3MvbVV5ejdTRW5nNVJsak04aWl2SEc1Nks5eHp5ckRFd1Z3MXhaU0NUTDVyNkhlajNuU3I2SW1pQTFBcno0ek10NDhWdnRXeldtY3g1ZVBpY0g4NXN5em51K3RqM0JzQW5rcmVpdXVTTUZqdVdWN0NOM2orN1NzeVlSRWU5UUo2aHpSdFdOUFNmejNSV25GdFpmTU9QUy9Bb2FIUTYvMlZpaGtCVjlsSHVZNXRvZTg4bkgxbmhka0NlYjBnOWNLSmtYUkErM0JTaWYrbm42NGRRUmt1Zks3RElMTjBkS2lIeG9RSlhWdUJPek9HdUY1RXFob0VESG1uZnZ2WU52SEZuNlNlOHR3VHBEV2JzaVk5L1N3bW95QlgwTzE3b0o3VHRNSUJVbHVVeDBrQW9Qem5TT1pVdEEwOFF2eXd3UFc5UWF4aU9Kdm05SkpCb2FQRFdYNmt0YzNybEV1KzVJVWNmODNjQnp1QjJ4UEZVeXFTQWZZYlA3YklkeWV1ME5TZUtCSjBaa0NNMlhxQ3FlSEJPdVVxUlc3aDEvUUUraGk5Qk1seU1kbVM0bnNqT3E5WUcrd1VwakhSVVZ0Ni9mSWZWYTBEY1czTnBjbGhiZ3RlTFFjeUtEWjllN0VTSjluRG00ZUJPOE1FOTJlbnhXOXIrSW9Da1RjdURGVzgrenh4NWVIVjIzUmlaOThMSzRVV3Y0NHNocXdWdXN1eVhydURTTW9sTWtPendleWJMTjgrN1ArcEJhZGkvN3hyRElaY3pkS1dZbXhqR2xQUWxHV0xHVmdZdEwyRDFQd0tpVHF4K0JzakVaem5KR3c0d1p6ejd6TEhWZGI0SXhGSDh2R2QvZytlTTNJWFFQRkh6ZzVyZFRXOEdJaExjRSt4RElBY0ZyYVh1cmlhaEdaTHJrMk9LOUlpUnhxSE9rTXBlcHBEYWNab0pZWS9pR2lsdFhiK0UwNExLdEk0R3l6NTltd2FhWTlKL2UzRDFlZGs3bHljN2o5bmdxdE1mNHZVOTI0WkxCWjhlemw1ODFRdXdDTTdJRXRGRzJwVnh5VTg2UnNpMzNucDZqOXNpMGpaYk9MVU9rbEFyU2hqNzdUR3RYYkxjZE1JYVNDL3dtRzQvZXFsK1NpSUN4TENVdEpxc0FaM1pGMlZwc1l3aTVCRVA2MUk3M0lXTFUxQ0NsQjVkUVZjMFlUYlRwL0NoaGFQMDEvangrbUZBQTJacm1sVmt2dVMzREphb3FmVGFROXdjSEh4S0pLQTcxMXNNZ2xRM090amhybnlUWjJMOGFhdmJDbnFWWHN1QktnR0pvamo2YzN3SGtKL3V1VDFJM0gxZlJEQjcxNWtDVDF6TU1Qam9wNmp6WFBQam95VHBMRkhwQ2JOaGpmOERUVzBFZ1ZwZmsySUNXdlFodDdNZ3UwOUp4NzlFSFJGMFRnaHM3TTdvU1l0M0Frdkk0Z1htTUFFNlNrR3pNcjV3aktiV3N1bFZKa0pwVFAxVHVEcUZwdTJjMzN2dlFmSDBqWUpTbDRXaVRjVWlldENlSVU1eUhOU3VPT2VHdGQ5OWd2VjdTNTM0VU1wSFNXS0dzaTAwVTl6ZEZnMTA4QnEyK0ZiV2xSSHY5aHBGc2hEMjVzZ0ZXbVY4K2VJOFRqcTNOc0VBZkUxbXNzNmFWbHRoODU1d2hDeFdlbW9hZHNDaHRsS1JVQUp4RnpPaDJMY3E1SnVIcHQwWlF3TllHWFJoUHR1VGd2R3FGUEw3bi9IQm83M1NnQXNnYWNhWE9TbVc0cVlrZ1RrNHduR1NzTjFJM0xyNnAyV1FORnBPOVl0dExKSnN2a2l1ZXVmdzhnVGtWeHV1UXhjcFlnbmhRWmVZRFRwUmx2OGJWam82T2xoUGV1ZmNHUFd2RXFSRjJSNnZ6RVVuZ0VzNFhFczJ4UmRDQUNCaDZyRmxhSUlpUm1EcXZTQzA4V0Q0aWxhVlN1Y3JLNGNrZzBmcTNEVk5ZZUFsUlR5YVVZRWdpRVlra1RtSlBsTUM2VjN5bzZWbHp5RDBlOG9DLy9NV2Y4N0M5QzhINE1ZeCsycE9UUlNyRHhGN2VYTzhrR2pwRVRYK05QNXM1TlpEMjJEc2hPeVFIckx4bzZQeHBpV2J2cFNTTlMrVndGV2haOFhCOW40eHhYdFppRmVZQlFVaGx3M0U0cWFqQ0RDTnVDSGdjdHk3ZFJKT1FWRWlhMElDbFUxSzBZRmEyYlRZcDR6TXRxM1l5dnh2QmdmUDhYWnY3QWMxakRHdk9uaGNZV0ozenh5aDZLbm5ySkhueTMvT0dubU9QamxDVTgzYUpvcjZISFd6WTNZWjgxazY5TTBZVk00bXRyaWtLcUNLRlVqbGhvZngxWGhQcFVOZVhpUmxPNXNnTVNlOXBsTy9pb2VXYzZwUStkUnl0amd0VGlFWCt2TGlpc1VwUXhCbmFJMm8wanBFU2ZjdzVGeWMvR2QxYjhHaHdkQktKUG5HU2oxbXo0dC8vOUgvanZjTjNEY2VYZXFUVXZGbkpTMm1CKzZuU2duMVdZK295YkFlU05qL25qVTBqU2Nna1o2U3l5M3hpYTJPU3Roblh6TFQ5VkptamlwcFptSTlSNU0yNjNDekNMSnROMzQ2WHRvNXJmeXM4OWpvRXJLYkgySllOKzRJYjczcTRwb3ZHQnJzNE1mVStlcUl6VHdyMlRxdlh6VEczL1E0VE9ndFFLSHU3dXdaWndtQkdHelZzRWNHTWtyUVFrQlpkc1Z5ZmtOSjJTOWdCM3ZTa01aaTJnNkFuMHFieGR1NDVYQjdSbG1oWGxsanlmY0traXM3b0M4aW9wRzArakdMZXBwVG80NG9vTFRIMEhITEl5clg4eDUvK1I0NVdqMWl2bCtRY2FacG1KT21zS2o4UzdFeXhsci8reFBObk04WVlRTEVBRGsrT2lwQnRnTDhKUloyaGFEd2JGbCtITmEzWW1jM0xCdS9QTFBXejh6WmRHNythT2QyQ1ZVMTMrN0ZxcTZBbnp2cGJaODNIMDJNcXJHZHM1Q0dQQkpDRjNkbnVDQVpWTGJWWmpNYVJoY2daSG9acGlrZkhqNnhmMW1CR09TbXRZcysvbG90R2xpR0phV0Y0OFk1MVB1Rmhla0NQaGQwTm9qVmRFQVZSN2lJOUhiZ1M4WlRTSVVZQXA3UnhSY3VhUi9xUVF4N3g3Mzd5NzNqLzZIMDZYVFBicVFraHNGNnZxYXBxcEVrZlN2Yy9xV0Q5dWdYemFZTm5RLzR5YXViZytHRGpsMU9leTNRTk1WMkxSajB3bXkwWWU4UVZIcnZ0ZmZhOFRmZWp6ODNUUnVKUGp6RzZPQXJhZU81TlRkakZacU1GQWtBTHZiVUYxYzN4SEV5MVNTVEtiVitrUmZNRW54MU5OV1BEVnpUa2l1eC9BNkxiS201TmovVkVEbzhQU1JwSFRUYzFBOFJaa09PMG9KLzl5UVVUYWRlZlVnS3ZSSW04ZSs5dE90WWtlcHVxSkdhS1pvaEVzbE95VStzYndIQWVvWTBXeEZCUm1sbGd6UkVuY3N4L2V1c3ZlRy8xQzA3OGt0bmxoa1NpalMzTnZLYnIxcVRVRzdWYWpJOVpvRGFmb25wdW51eDBkUGk4ZS80c3g5T2NmL296bUl4bWJtY09qZzdvaVpaVDgwV0xDVEJXVVpqT0VnWE50c1pxMXhDOFdVUlRHSTVCbnM3WFhML0tEYWd3Q0crakRBWkJHMkErVXg5ckNIc0RJOExkbHBiZDMxaDZQNUVuZzlGTXRPVVFYZ1ZJRGk4VnRRUUdScnN0VklYcW1EUzJCMkgrV0tUajBmS2drSkJxK2J1TTE3SkpCejkrREpySk85T2dPQXUxUitsNC84RjdMRzhkc2NNQzhSV1N2WVdqaTVwUDZKaVF6bGpKZlFRRFdRY2hoY2hhVDNnWUgvS2YzdjVQdlBub2JkcW1KMnJMS21Kc3U0MW50Vm94bTFuYjNMNDNRWHZhOFhFV3kybnM2Q2NabjhaaXpUSkU3REpINjJPczhaVUJzbE1KbEd6c0V4MHRIcWVDbEZSSTVXb0R4bVF4eXBMaDJtUzRSb1Z6a0UwZjVmNC9mZ2gvSWtIaVRMQTJ4Q0ZUazIrRG90OGtZN2REeXlwbmRkNFE3Qmo4c1kwWkpLTW1xMXlGeDdndUZORE1SdjNiSjR0K3pFUXgycHhFWnJrK01hbjNoZHBiYzJsaDlWRW13M2dsY2ttc2VsK1p3SVRFU1R6bVFYeElTd3VsdFpPMVVDMDlpN0V3dlhOV0tERUdMcHl6MWswa2p0SXhiM3p3T3EvZmZ3UGRTWFJ1eVh5LzRXaDVSRE92eVRrem04MG1QbGxsZnFzTURFZ2JhOENlemFlcmxaNmthWjcwODhuUHZ3bCtpSWQxWEJYZ2QyL1dTTEZra202cVFqeVdFL05pRUFLSG93bTExZTJkY24yZWRJMGY5MTQvaW5CdS9FUjUvS1J0LyszalRlNFlKUnE1S3l6a0gxeUZtd0EzaHhLU3FjRTVSTy9zZlF0L2RHbUt1UC80RDM3WUFJWk5JT2RNOWhrTmtVZnJBMXJXREp2TUdCVWRIUEJzZTZrbUkvalJaSzJjRENVU1dVbkw2KysvUWRoeHRIbEpxQjNyMHNlcmpUM08rMDBKeUttTjZOZnRVLzBxeHFobEFKelNaK09CVHVTdDRJY08vSTQ1VDZ5aW9TTFExcENsVWx3eHNUNHExbk9BMmozbG5IK0VRdVV0SXAyQmpteDR5Tk9IZnJvNTREUndZWkhQZ1dYV2ZqWmNDa1orazdUNFZpVmZFY1JCc2lZUHRhK3hySWREUndTNnBlYWtRSEpVazRXNnhjekZrLzZRVUljU3JFaUlkN2h3dHJ2SFZDT1hkNWh1RW03U010YzVSK3g2NXZPR2xIcWlSTjY3L3k1ZHFiMU9MbHVSSm82KzhKRUVWNkhaanFOcVpTMng3M0ZCNk9nNVdCNXlrbGJqZGlaWkM3SHBocXBnMEw2akZwYk5ydjNrbWpMOVcvMnppZTRhYWtkcXgwazZwbXlibXcxWmhLZ0ZTRjFhMjZhVUdBaE45L2YzUzRYMFVNbXdTZXdQei8vamF1Q3RUVSsyNVdQNm1Zdkd4NFoyZnhKYmZpdUtXUnFneXlrUEtqKzI5TVlpVVhuaVM1NDNnVTh6bWM0NU5DYnp0YkkxMjE3M0hlcVU3SlhEMVNFSCtwQ09GbGQ3Mnl6VlNsL2lXTkZzbGQxK1l1SU9KbkduQ1FuV2owd3crRmhkT2xlS0NESDFaM3BzL1YwYXd6TVRFWENLdXNTeVd4Y3JSbkNsU21INkxFY0VTUmtld2JuQUZFWnRFYjFjWEpxejYrRHBCVzJNUUFBZjcvbWNLMlJQSSszam9qajlNWFdqcWo1OThNR3NzeEtyRFk5REhRWnpNWitaVUdEaTlCWk5pQkJ6Skd1ODhLWXY0bWdZanlPYm50aEQ0ZWp3c0dQc3dBbEpJOGY5SWIrNDl5NG5IRnRxMml2OUtSeWNrWlZ1SUZianpvZXd0M3VKRzlkdUVkZVpSbWRVV1F6TXF1YUlqOXJzVkJYRTF1TDdMM1JzUFdkdjBVT1Z6TW55eVA2T0VncnhiRFJIZlF6cGo5cWsvTTk3djdYaFBnMEU3Wk1FalQ2U1QzYlJ3eDBQS1BuTSszYVdDU0JXdHlONVkrVHcxSVVOLzU2cVh5L0NyS3JITk9KbWNUR1dtNXcrQmhUS2d0UEMrSkVuemRBV0laUmVad3A5NnN1T3F2VGFRUVZ2MzN1TCsva2VKeHlqM2xyOTlxblVQV2xKVzR4QmlhRm5scEcwN3JvOW5ybHlCNWFLN3p3dTFSRExBbkVRUXRoYUlOUDcrTHNnWU5ON05Kby81ZWpra0VGekRDMWpLWkhjMFdLUkNVRXRIdSszSTdJYmJHRlpFeGV5SkQvNU9qK3BiN3hWR2IzOW9DZkNOUWxWRCs4TnYxcVVjVUovclFyMi8wMHFRTXFVdVlISlYwcU9BMUJIWGMvS2ZnUmJBWXhwK0JXd3FKNXBrUmhQSVQyR3VTdy93L1VOd2o2ZzdVOFBpd3BPTjRWTUZ1UDV5eTZpSWZQbzBVUGV1djhHbDI1Y29tYUdldk5kUFlxS1hVZE9CWGNvNEYyRmFDUVFtTkZ3Wi84T0wxNS9oUStQM2dPZnlFNkkwdEtsTlpVMld4cHIrUGY0TEQ2aStmdWJPTTdiUUNaL2hjbEdxcEk1V1o5WTFCWWg0QmllZEFhY3FxVnF4dlNSSFh2dzR5R1hJQjZuNXZUTVZUR3NOZGlzTURzbWsvZk92L2FQOGl6T2FMTHBHQmIwZVBMVFdpNm5FUXo4dEdQNi9ZR2t4SHZQUmZES2FYbjlPR25GSWZhbmJOV1BaUzg3S2RBc0UzcGZWMlF0c0tZZzlIUm9uWG5uM2xzY2NraFBSNkVBcE0vOWlEakpCYzg0VmdBclZBUnFhcTY2SzN6amxXK3c0L2ZRWGtvclZzZ2F4MExQNGZxbkMrTnZFNFB3NDhiVEJCek1YRThnaFV5SWpTbTRWVkkxMFVZYmIybTdiOERaWU5jVHhsTm91RzBCMjZRZG5tYTQ0Y0lmdjl0TUwranMzNTRrWmsvS01SaTZZNUp2RzB6R0xmRHBjS3pOSWh3bTlreHRGMFd0bm9mM0dycWl1TWxQMlV5c21XQkNQSWJtbGdJQXJqUEgvUkZ2ZlBnR3g3b2tGd0ZVZ1pnamtRM2JieTRCRDhsQzBFRG9IWlVHcnRYWHVYMzllY0FUc3pYTGNHNmp0ZjVMTmcyZk5LWWJpMFd4emFleUVpZERidVNTR3gzVE9vT2lId01TR3o5czhQM1BDM2dNNHpTNzF1UEdlZkx3RWZOazI0UWdaMXJMMkNITHk4YSt0WDl1cGRuT3lKOU92amJORVkvaGFiRUZMakxvTVRkSlZwdlFwVklEYldFOW1maDdReWgxdzdWM2VySEtlYk80MVZqY1FyMUcyMllsN1gzYm1ZK21rWmdqNmpPdGRLUkdlZU85MXpqc0g5RVREWEZRZWg1Yk9XY0pYbUJJaExJcE13OHpnZ1lxR2w1OTRjdnMxNWZJclpDaUFZNEhQK1IwcUY1RjRKeks2QTJpZTN0WFAwMVBzSm4veHk4R2Qrcm43UGMvMnpIU0JjaEdRRVRjcU1sR3NMUTZGREVhQ0cvaVpodGI4ZDJHdk9YazJzL1VRK2J0T3h4bnIxQ0lEMnRpeE8xTzFpdVQ5NGFjcm9pZlBJT0x0WnB6SlFSZHJwU1llOVFsbEI2eWpnV1RtNVBsTVlKbUpTVjJOdzZyN2g5NmFJMG1sRnF6QWRIQ1Q1ZHlXZGgyckN6bTgxamxVSVU2QjE3STBoTnppMGhCVW1SSWVFdGVZeUZ3MVV6VXJtd01wZVJ0TUY5MTB0NXBVbjdoZEZOQmE1VUh4dWNYOEVqS3pLbVEzclFabGRLNUhtMGNLM3FPOG9wM0hyN0RFWTlzQWVqUUpzaktValFhN1JzeHNUZGY0UEcwTWVKZFE4MmNYZmI1M3BlK3l4VjNoWkFYQkxkanpRakZvcWFidWlmajNNOHVXNld3eXdUbkNPTExZa2dnMGZ6QkFrbHlkaFZqTHkwdFArSURNV2RFckpqVkR4dmM4RHh6d3BVZkkva2NGcW1BYytOeFRpL2EwOEw1Y1g5a1dOUkYyUHFVOGFFdWhtTENpN2tGTGprY2dWNlNkZkdKclYybldsbE0yN1piek11cXBXcGowRUxxak9KQkN6UndvaEVjc0EyQXo1dE5ySkFqalVpU1VqWGk4WVZsN1NuU1JNTS9aTkFQWTNFZ1JhQmN1U2o3MjNBakcxL01DdktNRDY5MEZTUnZQWkRCcEJzbmRIaGZBTlJZcVlhL2pzZmRxUDhOaWFnV0VJMW5WdFUyQ1E2eU83OXp5K05EdWVkeDZRM2w2MjRVVU9jOVhkZmlaeDQzRjM3eXpvOTUyRDdnU0ErTWFVcVZkZC9USmVzZzQ0UE5ROSszcUNibTFaemNLUzRMQzNhNEhDN3pyUzk4aDBXL1EzZVFrT3hOYy9aV1lDZ0Y2VDhrLzRXTUptdmpxeVYxTUZST3U5SW5JSXVWZ2NUY0UyTkh5bVdEekVCTzFsMm1IRGZHYUwzVkNrek1sMk1Zak0yVnRFclp4VXM1MHZUbjB4NUR2ZUJnaFRnWFdNeDJ4cktubURyTWFqRTJxZ0pObUFURUxBS2QwOFIzUCtjNlIrMTB3ZnZERjBkdGR0NHhCbmtkTEtxblZQVWZPeG45Y2NkNUpSd3g5cHVvemlSbGNOSDNnYkZ0cXNnR0FiQWxZQmU0WkdjUE9CVFREQnVKVFluUGpwQWRMbWJxUXF5VHBhZDNIVDk2L1lmMHN1YUVROXhRbGlLWlRFK1VudVFONEJxOEk2QTBQaURSTnJJOXVjS3pzK2Y0MHZWWHVjWlY5dndsMWtkTDQ4UlhURU9uaE1STVNGQ0xMOTA0cHd6SUhra0J5U1ZSSzVsY1pXZ2NmdVlJRlZSaW5UNGJoU3BETGdsdkp4Nk5HVWtKWWlZN1I2SXFFVHp6WjBXRktpc2hLMVZXdkJxbnhwamduVmdxbjN4WXlkQmcvanQxWE42N2pNZnFsdHUrTDJWRHhWOHV0UnEyUVppR1RTUzYySjRKWUp6SjA2cEZKNjJtK1VuajlMM2xqNzNMZlBSazlCa2JkSm9SUDN1NDB3N2lhVnllaW5WVFNXTWw4M0RNUWU4TlNlSnRyZFNFZXZSN0hqY3V2cGRKbEtwVWhjZkJqUzQwQ2o1RHlMYURhV3JKUGtJZGVkamQ0MmYzZnN5YUUyc01HS3hGVXBkNk9qVzBpREVIcVBWT2RzRjJ3UjRxYWlxZDhiWGJYK2ZydDcrQk80SzkraEpCU3pzTnpUU1ZOVGtrYWltdzl5TWlZaGcrbTVBRnNTeE1Ta3JYZGRhS3FZK0lKdE5Xc1VkUW1oQndYdkIxQUZGQ2NGUlZzTVIrU2ZEbm9oMDhGRE0wRngrelBMdWk1Wm4rZklwRFZIRkp1YlRZSTVUczBsQXlwRzQ3MmpwZWs0a2NYZGM5NGRqMit0SFNJSjlPZERkOE5wR3RjM0NPZmpzZnNzbUJRUmRiaHJTekRuK2Y1RGJzOHlaa3VUakZIdk5QY3NFTjJqM0ltTUNXVTlHbEFYNjhiUzlzZ01wcHlPV1Z4K2JVMmFzb1dUTmFXangxc2tLQzhwUDMvb1pyMTY3aFpVN0FLTjJTNXBHM01Vb0NUVlFTU0xrbnVJcktCZFpkeTZYNktwR1dMOTM2TW8rNkIvejgzcytvOXdLdDl2VGE0NnVHb0lFVUU2bDA4TXhlVWRGaXl0a3VIL0IwS2VGOFJYRE8vTnlza0NPYU1uMDJDeUdFRWptTmtSUjc4eVVWakF5anRta1JHUkVYZ3k4OXpGdWVHRnRiUUlOeGgvdjRpekdYR1BMSU14bGhVYzhZTUVEaXJZelBya1B4NnNlMGprV0ZUUk91Ky9XWldPSW1vTGI5ck1mZnh5WDUyUnAwbS9rYTRnVVg3dnlmM0NDM3l1WnRnVmFudFAxNjA4QkJKb0k0ZkdZd2s5endid3ZXMUw2YUVObWNPdTVqZDZ4cE1LUmNXZmw4WXBQZ0ZMWG9sNGpnSkpHMUordzZsdm1JbFQvaHgrLyttRFZHSFJkRlFSek9WY1JKeEV3RWNrNEZQdVhSWk9JaHliTW5sL2pXQzkvaFJuV0Q3a0dINngzMDVzUkhWVnp3WjVMVEpnQUp5Y25BeG1MMVU3bFA5T3RFNmpJNUNhaWhJT3BxamlaSFhDVjhEZ1QxTktFcWpVVkt3RVFHOHRGaU11dG1pMG9EOGtZMk5SRXkvQ2g4RWdFYjF0ckl1SnlWS2dzN3pQQ0Vzam03VW5WaFFtaStZZUZtMUlGNU03UHUxbXlUMVpaSG9JbzdGU000ZnowODNmVituT0V1RXA2blArQzJ4amc5QmhUOXNMREhUcEl3dnRmbE5RTVJONktUQ2Rua1R6WUpRRXJ3d3pHdmQ4MlUwcEpSMDQyUWJ0aUhObkF0TU4vbWRCSHFoWGRXSXB2V3lrZnhGZlM1czdJVjEvTHV3YnU4ZS9JdUp5ekpYbkRWakp3clVuUzJCU05FWWpFZExYamhYY1Z5dFNJNzh3eDIyT01mZmYwZmM3Mit5UzZYcWZJTXN2Vk03aVNUdkpLY2p2Y3lYRzl5eVNxMms0Rmd2UVI4VmVQcUJ2RU40bXBjYm5COVRkMHZ1QlN1NGJ1R0tzMmhOeUdYckRnU2JtUXJGalFISXVYSEJhSW9VWktoWDF3SDBvRkVITkhNeVUrNDkwN0Q2RUU5bFRRMHpQRVlxZ1puZE8xV2xHNHV4SUJQc0toc3BxZW43L3V0VHFjYjBxWkphbVJNZTN5MGplRko4WUVuRGZObEg2djI4d1gvUG5XUWp6QzJJbzh1V1lHakdJWGFFRS9jK3Z4UW9TMmJKSFdnWWw1WkRzb1ZVdElON3ZHakFXdWR1dTE3S09CaExaRzdFVTBnbVQ1MWRCclJCanJmOHBPMy80YUg2UzQ5UGRrWmsxWk80QWlrYkpDMElSY29LSlVYdkhjbDVTQlV6Tm5sTXYvMXQvNWJiczJlNVpLN1NtaHJSSzJ6YUMrOWxlUG5YTzZ4T1BzdUVuMWYrckFsa2thNjFMSHVJbDJ2cEY3UUxuQzV1czRYYm42RjczM3UrOXlVMjF6cXIxS3ZkdGlSZmVnZGtqS2E0aGhZVWRVU3hYT29Nb1lJc200b0ppQVZQelorN0Z6YTZZWHJGSUo2YXFtb0NJWDR0VmdYYXFRV3VHM0dNOGhselNUNjNKOXgwTGQvelkrM3hUNkM2ZnRSWGF3d3JTY2J3SmhqSGlzKzRkdWNKMkJGT1U2aWhGWTVYS0picm1aUVpGbU1Id09mNmZPYTFxM1pZNGRWN05sWldEbCsyN2I0cWpqK21nb3cxd3pHVzlkdjgvNjc3NUg2VEtnOVVnNmMwbEJlSXBPck1qL3Y5SlVQU2ZVQnNBV0dlMHRseDNNWWtGY0xGK0lBbStwenBtcUVCMGNmOHBldi9SbS8vOFU5SU5ENEdSVTFzVjNobkxkR0UyS2txYzRGTW9LVFRKZDYxQWt6WmtRaSs4ejR6c3ZmNDRkdi96WHA0T2VjeEFQVzBxTGVLcTNKWVRTVHV0aENaYzZSazJnNFRxMFFhcHE2b1R1SnpPcExYTnU1eGplZS9ScTM1UTV6NXJ6MDZ1YzVhQi95ODd1djg4YkI2eHh4UUxVd0lxQ1VNbG1zYnMrSlVOcVRZdFpFQlRKUUJ5Vmk2aEZScXFxQm1NWjVQZzlzZmhvcU5uMGZMS0h2Uy9BaWRwbVhicjlJWUVORmtiSlMxUlZkYW1sOFEwd2R6anZhTGtKbFRsWFVXT1prRXdHY3dxeE9qeUdTTFFybmdCclBqQTJDWlBMOXg2Q1lUbU1sejhpSW5sYXZjR2FIdVBCaUx2allOSms5TFdWUnRhaVIrc3poNmhCRjZiRWs0MnExUWtTTTcyS1M4Qk9Sa2lsejdPOWNSbnNyK3RSa2RHRUQ3ZTc1T2JQenhzYWNIRkVnUXdHcW1FOHloSkNIUEpKOVRjajBNRXM4YUQva3h4LytrRWM4SkVzayswenNlc0xRbER4YklqU25EczBkd1FuQlcvU3M3WHRtelBIVVhKYnJmUHZGMytYVk8xOWxIdmZZeVF0cmhyaU9CTFZHN2ltWlpvM1p0RWxQcEdvQzRoVW5tZlh4aXN2MVplVFE4NjNuZnBkbjVRVXVjNFdGenJuRlRWNW9YdUx2UGZkNy9QSFgvaW5QK0JlSUR6MnVhNmp5akJBRGphdHB2RFU4ejIxZmdrdEsxMGI2M25wMVYzVkRxSnN4VXV5Y0d5c0pwdk0rZHFwNVRMUjYrRnZRUU9ObVhONjVRazJGa21uN3ZnUXZ5a1pkaWpaVFNvWHIwcEwxWFZ3anZxUUNCdDkxVW4wL0haOFdHdlJ4bXV6MDM4TDVINTVpQmN2dWJyOFZFNkw4VzlKR09Rd2hVdE1aSlVLSStWZzUyMjRubTBZT0lvYVlHS0paRDQ4Zm9Ec0phQWhOVFd6WDVRRUtNV2E4dHdDSGN3UGx0dWZTN0JJdVcvZVdsSXE1NElickh4N3dzSlBtclkxRFIyUklXUkNEZ0pXbzA1WkppMHdQYWNLb2hRS3V5aHl0Vi96OGcvL00vdjQrYnBhNExGZW9tMER1TGZEaEs0ZUlHc1lSb0xUK1VYVzBzV1V0amlwVUJBSUxkdm5HalcrenQ3akVYNy8xbHh4MkI4em1jeDQrdXMvZTFYMlNHdDFjaFpYbmFMWmR2YW9xWEZLdXpDNGpqNFIvOEpVLzRXVmVZWTlkQWdxU2lIVFVlUGJZbzJLUGYvYnFIVDdnTFg3OC9nOTU3OE4zaUxUa3ZpMmtySW1tcVZoM0xhR3VxV2Z6b2dFU1dubzA5eWxTdWUwU2s5UENOTlZhMDc4UDd6dk1TaUI1S20zWVgxekJFY2dvV1kzb05hV0llQ25XZ0tlTDBYdzFNNmc1V2g1WWlKOWtsa041bms5anl1cVpCV3dJajhrN1p6VFQ1bjZlWmhNdnBTN24xcFJkYU1HZUgvcTBFT3ZqaDVRU2w0SGF5K2dLSUVyUC9jUDdkTGRhWnN3SnZzYlZtVDcxMUM2TUdoQXh3eTRTMFFUQk4rek05ampxSDU0Szl4dVdMWmZybkdMYW5uWkllVWlpUTJ4SW9TQkNWQ3pxaUNoYUNjd1N5M2pJRDk3OGM2cVhLMXdqWEt1dldIUXVabklFSHpZK3BTWkJCR3JuQ2MyTVpXczA0NDFyVU0yb0NDL3RmSkc5TDEzbXozNzhIM2ozZzdlNWZ2VTI2OVVKZmU0SmpjZUo1Yys4OTRUZ1NXMGt0QTM3WVkvZis5WS80QTR2Y29sOWFxMVlIaC9TeXdvLzl5ejdKVlc5NEthN3lWMGVjcDNuK003dFBiNXc2OHY4NHU1Yi9QTGhMMWlsSTNwWnMycFA4S0pvdTZZWFlhRDA4OTRUcExZTnI5elRGby8vQU4zYUlrNDZPMnp4R2taZE80ZlBnY2JOU3pnc2d4Z01MT21RZ0xaS0J5MHBGYkNHSlBjUDdwUG96S1F0bS9uWjhMM1lCdnZZM043WkZYeHhqalUvVnB0TlJ6ajl4ckJJTHo3QlJSZjIrSXRYTlUwK1BiNTl6WklnQjhjUDZDeE9SS1hXMHJTUFBUNUlNWS95Q0poTktSR2s4S0JmdmNuRFg5N0R6d0pLdnlYc2tqZkNOVkErbnAzR0Vtb1o3cm5rZlBQRVp6UHdxUWw1bHBJbUh3SWpPZU1hajZ1VWg4ZjMrZk9mL252K3dkZi9tRGsxVlYzakpLREorQ2k4T0x3elZJZVV3RUxsUGN3cXV0aHgzSGVFVURIM095d1FLai9qSDMzMUNxOC8rRGwvL2ZwZk1GczRuSHJhNVpMRmJFNk1rVDUxcEFqWHErdnM1YXQ4OThYZjQxbGVZSThkaUIxSDdSRmhGc2pKQ2xMRkJXS01ITHFIZUpTOXNNdU1ocm5iNThhdDV6aSs5WWlmdmZkajN2cndwN2dRWU5iVDZnbVpuZ3lrYkprdDhaN2dnbFVyNkFUdU5mSHhuMnBreFJFSVdyRTN2MHlGNFJaandZWDZFbEhOUmZQRnRBYW5EQTA5TXNxRG93Y201SldaMFQ0SVRpcnIvaU5TYUFOUDQ2MWRNVTQrU3ZoZXpxQktubWFjVzA5MnJnMzlzU0EwWjd0clFERVZ5MnRLUFhpbFRhdVJSRlRFMFpmYzBwVEhneEp4azZ3RUZ3alVYTDk2M2VqQmtJM3ZOa1RKZEhEYzJkckJ4ampTeEN3OEE0QlZEUEdoL2t5VWF2aHhLSDJNaUhORWpiaFpZaVZIL0x1LytUYzg0Q0hIck1tVm9MTkFkcDQrUldKU1hQRmZVdDhUKzU0Y0UzVUkxSFVneHA2ajVTRjk3Smd4NHhLWCtmelZML0lQdi9VbjdQV1hDZXNaTy9rU3VuWlVlY2FlWE9GeXZvcC9OT1Azdi9SSFBEOTdoUjBNKzlkMUhhNXhuS1FsT1ZnN0lsY0ZFb25qMVFNcWw5QzJ3MFc0ekNYMjJXV2ZLM3puem5mNXA3L3pML2pHaTc5THZkckZuVFM0OVl3NnoybmN3a3BQK3NSNnZUSXlXRFpDbFhNZU9VdWVKR2ltN2F5QlgrTnJucmwybTVvNTRPaFRISnZiaTNqenRTV1RVait5UkNlVWxwWjF0eUtMNHJ5VUFNMEZxMUUyei9sWDRac05ZNU9NdnVEREh6Y0JOLzMrRU9tWkJrQ0dvRWRVWTFIRXdRY0hIMkJZNnhxdzBueUFtQk5Pd3FsZDBpRjRkaGY3ek9wNXlaMjRyZk0rM1gxTWVmeUg5eHcrMjQ4VVJJbVp0eVVRWENCSEEwL2p1c3VJV2hTeTNnbmNYZDNsZi9uci93OEhISExFaW82SVZBSDFsbmZxVXlUUmpTVkZsYXZzK25ObTNsU0VTbW5Ua2tSTGhlY1MrOXp4ei9KUHZ2bmY4TTA3MytKNmM1dkxzeHU0cmlIZWQ5d0l0L21qMy9sSDNLcWZvYVlHSFBjZlBTUlh5a25mb3BVbk9YQytvcWNuNnBwNklhempBYkFtNUo3VnlUMVN0K1FhQ3hiVUxQS2NWL2UreVQvNityL2dHOC8vRWRlYnorSGJ5N1FuZ1JRZHJ2STBjMk1abTg3eDZaL0hyWXRobUxWUWNmUHFyUUtuRXR1OFJNbVpvdTBBdHZPbG1jelI2bWpUNUdNaTJBUE4zdE90Z1U5blhIVDhpYm00d1EycVdrNWlLNW5INlJKdU4zbk5aM2FHVGIyWlJZSkl4bnNoWXVGM0ZUTTd2RGZPd3VRNmZ2cldqM25wOGhjc1J5S1k1bElqREsycnF0alp4dkNibytLQ1dML0thc1o2ZlF6VnBxN001d0sxVWtVZFk2dWV6VFVQaWQxVDNXemtiSUo2S3hxcXBiUjkvSnZRTkRYYXJra3BjUlFQcWZjWG5MVEgvTm5yZjhxM1h2bGRNcGZaWTQ5UU8yS254TmhSZVdlVWRpbVQxR0JpR2J2WDRCd3FTcmRhb1Q0eXErZDRGbmdjWDd2OU83eHc2MlUrT1BvbGNsMjVNcnZHdGZrTmRyQ3VPQjdIbytPSDFQT2FkV3lwNWpPNjFLSWlabjVMWnRiVUpPMEFKU2lrMkZON0F4c3YxMGU0dW1MUFhXSk54eVd1OFlWck85eTU5aEwzbHIva2pROS94Z2ZINzlLMVM3S1BoQkN3YnBsbGdZczFFU0VMbWt1dmJHRUVFMDhyNmFVUWxFcnl1Rml6SjVkSERaVzFLejNudXRHWDBseE1kRXFiWVhxT1ZnY2taN3dzUS9MYWlabWJ0UXZrM0U4ZTVDU3NMNVRqbmhXS0llUndlaDFNMTdoaE9KOHk4REV0ZHZRbGdpTitPd0trUldBc3ZDL2tYSmlGR000akV6K3JlRE5hTkpaNFlzcUlNNWlNUTR4dmNid2hpNUxGT3ZGby9aQTFSeXlZc2FoM09UaytabWMrUTNOdmRXNlNFV2ZONExwbHkyelhXc0plMzczQmNUeWcxWGFFTUhtMVVvVGtGRFNUQzdlR3kyNE0xOXYxbmVkTFpwS2JiaFIydlpvVlg3Z0hWRGIzR0x1T1dkT3dYQzZwNm9wMWQwelZOTHp6NERVQzhMdXZmQThQN0xKYnFPUXMydG5ubHVDOTBZS24zZ0lacGJEUWU0OXpQWm5NcWp0Qm5LTUtnUm43WEhHWGVlSFNDL2F3TjBrRit0UnpIRnVqQ05kbzZKclVtLytZczlYNkthUVliUjd4S0c1c0dpOEZmdFNsanF3ZFNZeXZaSWNkNWxUc0xpcHV2WFNGdi9tZzVpY2YvSmhxeDlOMmE4TENRekx3a3hlSHBrU2xOV1RiUUtSU3E0MkxrYUF6S2pHdWxwUVNaRWRneHVkdWZZa0ZsMmhZMExFaXU0UUxpa1p3THRCbGlFbXBxaDFXY1FVU1NhejU4VHMvSUZWcnNpWkM4SUQ1bkNMWUhBemxLNU9jbmExcG00c2h3SFdlb0ZIV3NSWmZYTE1sNm4xbEFoYkVHMGo5dEM4Nnlrd1J5Tk1ISDBMMmp4c2JIMm5iUEx2WVJIdDhZakFEMGZla3F1T2d1MCtpSlJNSnJpSWxIVm1oMnI1RFZVa3Awb1NHMkVkcUdwNjVmaHVONEYyTkZ1M25HT3FsYkVFUE5VZ0Q5bkZ6ZldlRmJCb0VHZkpsNTEzL0FMdHl6ckZjcmxrc0ZzUzBKc3dnNnBMNUpiaTdmSS8vOVMvLzN5dzU1bEY2aFBwRU5hK0lhdW1PakxWenhVK280VlJJZlRMVFdUWVYxeElWcjFJSWV1Yk1XUkNvY1BqaUpnNU1UbWJLaW5Oa1RRYTlTdEUydC9JWlNWYWZOYlFKeHNtWTd6SXp5eUJUc2UrcDhZUVUyR09QT1ROZXV2VXlMb0dtVEZWVnJJNVB5aVp0Y0RCMUF0a0V6azF5bzk1N0t1L3hQb3ozV3JzWjBucHU3dHhpem9KTXRubzRJamxiUThDc25hVmlmTVU2UmlRSWtZNFRmVVRIMHJxeXVpbmlSNG9sZG40TDI2MW5PWUZabmVsQmR1bzdvekNlRzZHOE9QQjNKcm80WHNSSGpYbFB2L3NSaHFpcGVGV3JBTDU3Y0pkbmJ6NkhNaU9FUU4rM1ZJMkRJTGprQ0M2d1dyWE1xa0FiVzdSeVhGM2NaQjcyT2VvUExDS20wRXN5RlBjUVhjcDY3dHljVGxlTWhYbG5MdlM4cTNkME9kTlVOVjZ0M0VLeTRnWFVLMzFhRzA5amwvai8vZUQveXg5Ky9ZOUlLYkh3ZTh6ckJTbDJsclFPZ1g3ZFF6Q3RsUHJJckduSXFlU1FOSk5WNmVoWXk0YklSMFNRV0hablpVTWI0WXh0U3ozRU9QUXVzSnZZZE02UmNwOXA1SllmOWs0dkpuaENvcW9DWGI4aVoxQ1g2Q1VSQ25nMzUwenFlbWIxbkJoTnVOM1FNOENCWXZ6MkpMTStCdDg4SnJOS25BbytPcHBjYzJYM0tvRmczSlpqNkIrR2lncVZoQlIyNWxBN0loMzNIMzFBek5HMFpiSGVScTBpWjNOMHcvUGVUaXRNY3NBTTg2VFRMMnloUWphc2JPZkhNVTduMUd5VlhEQXUyZ1UrN1RGZ0RST1I2RHJ1SGI1UG9zTWhacE9UakV6VUtTNEVjNHlMaXE2cXlzSy8xTHh3OHlWazdmRzV0c2hkdHVZVVUyNCtOM0k4R0tKY1Q5V29mZFNSeFpMa2JkK051Yno1ZkU3WGRjVS9VTU0wempvTzh5UCszZC84Vys2dVBxRGxoS1VlazcyMXVOVXN1RkRUNTBUS0dUK3JhRk0wQk1ORXcwancrTkwyTnZ2Q2JWS0JCRE5oWEJXUVlJSHFMaHRvZG11dXNWQTQ2Z3lMV0xSZW91QkhjeW90WUMzUTRKMlprSDNmVWxVVlhvd1NlNVZhbXZuTWpwbWc5aldTamRmRUZjeGg5bXA5eGtaRWg4ZXBJL1d4ZEJNVmdsWlVzZUgycFdlS0ZpdGRSelZ2WVJmVktVZ2s1Zzd4Rm54S2RIejQ0SDE3ZnBOeTVta2g2UlkzeHprSjhmOS9lMy82WmRseG5mbkJ2eDBSNTV4N2IyWlcxb0FDUUJBZ1JVcWlLRXJkN3RiYmJydnRidHZMYS9sL2ZUKzlINzI2YmJlbGJsR2laQTVOaXVJRUVpQUJvbEJBVmFHR0hPNjk1NXlJMlA2d0k4NDlOek5yd0VSUTlodEExcDNQR01QZXozNzJzL2Z1WmYzc2lzbjBLZ3JWczFERmk5OTNjenUxZm1IbjZLZW4vdEQyOWhrQW9WSUVVU1NTcE9kays1aFRUbTJHZFFYcXpsYW5PZVdSWVl5VzlwOHRKVDNRNGxudzFWZSt6bEY3SFpJRk40MmpQaEIxcUNlNmc1bUxHVmlCRUtkNStwdEQ5Qy95cHlURVdmcUpEMmFxZWhjUUhCSWFSbzBNYmtBUEV1K3Y3L0NEdDc3SG5iTjNHZVNNSklPeEZFSkRQMXI4UjFwSG53ZTBoVjRzQ1hURWRQa1JxMEFwZUlJMmVCRmlTc1FjR1RVYUc2VDhWd2RVTmY2MU1EdjMvN1NraStpK09hM0pHUFprMGpqU3RWWjlKaGZ6OXQ2RER4bHpKS2FSdG0wNVB6K25kWjZGNzlCWTRsa2FpK3FYNlk1NEVYd0JtcndYeXhySUxRZDZ3QjkvK1Uvb1dGanNNTWZKcEsxclFDYVhNc05iZk9zWjZlblo4UEQwZ1dYVkZvUjR0NG9Wa0tWS09EeHJRSHlNcnZvOHhQUnA3UmtyMmR4dTFUM2I5VE1aWEVDTm10Zmlmb25FVmpmY08vMlF5R2hJWWpuQzJtRkVoY1ZpaFNvTWZjU3BwMlBCa1Z6bnRadHY0SE5MU29JTFlxZ2lPOXZjVFJOSjJqdUdUOXFjQWpuUkZEWkhUSW50T0lBUGlPOFlZNlpidG5UTGxnZG5IOUlkdzlpdStjNlAvNXFmZnZCVFRubE04cGxCZTZSMWpFUWJZSkxaOUd1a0VhVEZ5TUFlc2hQRGNiTmRCNGVuNnpxYTF1T0NUTEowZG83N1NzM3pRVFQvcTJXQ1lUZEQyM2xaeFJxbnRvMGg5dlJwaTVLNWMrOHVtLzRNWE5FTWtXQ3hzWlJ3bUJ3ZVRpeHgxWm5QcmNtMFNvS3JacTZIcmZEUzRpVyszTDFCUTJQOGplSy9pWnA1bVlVU2Q4dGtUWGdjQXowUGg0OVl4L01wTUQyMVdjTHBpN1puK1dKWHRiM3d3d3VFdnZaV3Nxdlkwc2duRzcwdmZNQlE3SDhyNWpDNmtkL2VlNGN6emkwWEs4enFsSGxuZnpzT0ZYRklKVFdpNGZYYmIzQVFWaE5DaDV2Wnh5WjNCZXdvVjFkZFhMbnc5N3pXZVVmdUI5UWtyeUI0dGpHVnhFM0hNSmlBNlkyYkIvUjZ6cFB4UGh3bGZ2SEJUL2k3WC93dEgyemZ0NVhPbTVZalRoblNZTUtxZWFUUEE0T09SQ0pKQnhNZDBsS3lLVG1HdnFmdmU0WmhJRWFyRHpDbDFzd0dtZVNNNUd3YUYrWDVsZmUxcE5OSWRtZ1Njb1poSEpIV0lWNDQ0NXcrYjNHZDRGdlBFSHU2cmpPRU9NWkpEVXNLMUo2Rk12Z3Q4eXVMM1FmcEhRczk0QTllL2tNV0pUd3hwTzEwVEJaK01SN3NtQ3dGeW50ZktwY05mSER2RHRsbmtpWkRFRVduak82TC9maVR0SXZ3MzZmWjFvdXRaSHZ2N2VKcG4wVVRFUXZtaTBmRWtWemkvc2s5empsbllFc0lEWWd4cjNPT2pEbXk2ZGNBTE5vRnBJeXB0d1Z1TG05eTBCN2hzeWZIeXZnb3gxMU5rQmVNYmJ6UXNRTnBHRXRLaTFpdE1jMjBpMjdLSG1oRFE0b0R3M2FMU0tZNzdoakNsbE45d2dlYkQvbkJXOS9sM1pPM1dYTkd6NXBFb3UzYUtlMW96eThwZnlwYTlDZ1RycUNCOWErZVYwcnBjbGxjMlZraUlvS2ZxVE9wcGwyR1JMSmdQK3Fzd21nYnlDNno0WXlmL3ZKSERMcEZYV0k3ck9tNmhrMi94bnNwUmVVTlZjdzU0MHJZUm9IazhxNm9ZWVlGUzY0M3QvaktqYS9aL2pIZ2FGcUJoYW13UjZvVGkvZm1BakJ3Ly9GOXhGdENwMTJmZmVuQ1ozRW1MemFuczJ0enFWOS8rbjQrRGJKcDlwbUpiTmJYRjFlNi9RUDROSDkyY2J4dlNyekJvd0cyYnVURDB3OVlzeUdYTkllcHZKQkxKVkVSbkNyQkMvMTZ3MElhR3ZWODQ2dmZZQmxXNUQ3VFNpREdSTnUySnJSU2FvUFpnTjNsdXozTDFuN2VpdVpjYXc0OWxJNEZLWTFXLzlrTExpZWFBc09yeS9Uamx0d2szS0ZqYUFjZXBBZjg0RGZmNDY5LzlsZXNPV2RUcXNmRU9OS0ZCcEt0RURGR2hoVEJKWEpqQ1p0OTdwbExxUVBHanhTSGt5SXJVSHdyS1RRd3dmUW9aZFo1YWlkUXRSVXc1c1NZaEQ1aFpZd0NKRWJPMGhNK09yOVByK2RvTUM1cExMR29SQ0pxTE51eEtxb1U2UVl0VWxlYnVBVmcyU3h3YTg5LysrZi9MUzFMQkUrZkxmWmxhSENSR2F3MW96SGRUZThDbTN6T281TUhQRDU5VENKWmJMZEtNaFFnU3pVVlgvbjU3Sk9uM1ZzaFgvblpSVlB4SXBOcC90bkY2L3ZVZHJYTitkbVpqM1VRZzFnUTBUbEcxL1BMOTM5bHBnSWpyaWxzYzlkTWhTWjhLZnFBSnJvMlVIWDVieTllNHFpNVJ1ZFdOTFF3S25uTWRGMDNGVG4wcm5BZDA5VkkxTWMrQi9hVm0vWjFDZ3RRSVI0bmdTeGF5clgyakg3TnFUNWkwNXp4NGZZRC92SWYvbmQrODlIYm5PZ2p0SW1jNTNPazlaTTU1cHhqT3c2TW8wSGdvZldrR2Jlem1teHBya0U0QlVsMU9sb29mbW0yTUljeGNZeDlrMUZDMStMYWh1d3l5NE5ERXBrblBPWlg3LzJjYlRvbHlwWXNTc3lqclo0ZXhJdkZNOFhpa2s0ZExzazBPVWFOTExvVlRqMGhkdHhhdmN4MXVXV29vbWJUaXlTRDI2MUNLVm1Tcm04YmFEenJ2Q0U0eDYvdnZvMXJEWVQ1ckZTT2QrcFhWN2RQMHo5YzNjQ24yY2luYlNrbDQrOWxvWTg5MldjZWJoL3d6dWJYalBTbXpCVGFJdjdpSm9mYmp0dXM1N3FOQlV2KzRKV3ZzOGhMOGxwd05NVEJvT3hxRXRXQ0VFOWZvUyszcTFZMGhTbkIwNUh4eXZRWEtDWVhqcXdlbFFZcEt4cVNqSDNjUnZ3MU9PVXh3L0tjUjN6RTM3LzFiYjcvbS8rTEQ5TDdiTjJhVVRaczB2azAwRnB2R2RKbUR1dDBESk1Pb2pPNjJjNHZNWDJWSFoyb0Jxb1Y1NWttSHB5UVhPYXNQMmM5YmxnUGEzS3JuSERDU1g1RVl1Q3Q5OStrbDU2d3RJSVpwbEUvbHhjM2dNeVh1SjJvSTBnb3lLU1FrOURxa3UzRG5tKysvazFXTEhFRXhwd1lpNDRLWk5PNExQTHNXZFZ5eVNRenVwRkgrcEFQSDk4MWE2YjRlbGZkUHlteWZxNEc4cWY3ZDltYXV0eWViaks2b3RqMWNacTc2Z0F2TDYvbGdENHpWSEhYUXZHM1JBVG5LVEVnUmJ2RWo5LytJVnMyYkhSTmNNSGlNTVh2eU5tQVorZUVuRW9BVTgxVStmTDFOM2p0K3V1a05YU3VBOXhrSWpyblNobGRuUkdTUG5tcjdBb29STmRzZjVNdlZhRHpuQ21yblpqWktnbnZGWFVqNFVBWS9CcGREUXpMTGI5KzhoYi8rUi8ra2gvZitTSDM0ejJTSDlsd1R0dTBSQzNLVFFKREhFMXpKQTBtNHlDQ09qRVRTOWo3czVRaW8vd1VBcWxaQ3VOSXQrcHNHeWpOcWlVSGNFdkhPcTh0a2RPTi9NZnYvVy80RlVoclp0bXdHVXgrZ0ozdnMvUFo4MjVsRUN1dzZHbVE2R25IQTE1ZXZzWlhqNzlDUTZDbkw1TGJOdkJ6dHBoZHdoQm52Rms0WXduRi9QU3RuNkJ0a1V6Z3NpWHlxVlljZmZyZytqVCsreTVwMCtrZUJMeS84Y3FLcUpuUkJpQW9KU0QrbkowOEMrWTBCOXd4cEI0Y2hDWVF4OFEyYm9pYmtmdmpQVjV0dm9SanlhSlowS2NCRUZ6d3h2Y3JzL0VZTXk0NnV1YUFST1pQWHY5ekhqMTV6T240a0taVG92YW9aQnJYVG9ONk9vZlpNV285cmhlNGVLWWFiTmZNNXpMRGxjSUlsQXh4dklOYzh1bHd1R3pCWUlrUnpRcE8ySXdiR3QreTFYUGMwcE55NG55SS9NTjdQK0EzSDc3TlYxLytHbi82K3AreFljTkJjMERiTEhBNFlvcU1ZOFJZVVpiT0t0a3kwYjJFVXJQckl1T2hBa0dnbWhIdldXL1BFZTlZeHkzaUJSb2gwU00rc3VHTTcvemtyMW5uSnppdlJCMXh5VktOU0VvSUxabElyVFJLbmFDTFJuK0tpcGNHbnhxNnRPU2FYdWUvKy9QL25vNk9CcytUNFpTa1k3RW9tQ1lzbFlJY09rY1NHOWdQaC91OGMrL1h1R3VabkVjTDFnc2dscC8zdklGMkpaaEhkWDUyejZ3RzJ1NkNLWFlaNXlJKzhnTDl2cllyZmJLZHMzalZram96TzNpeEhUMGRxU3dwQ1I3RUpjUm4xdjBaemlzREcyUUp2Nzc3SzVNSEl4TmN3SXZWQnE2cEREa2IrUmdnSjZNSHRheTRIbTd4eDEvOVV4amRoRFJXS3RBYzFMbDh6cDk4eHBLcUFZbXBTU1ZuZ0hNV28vMDRQSTBFV3JFa1JaOGRMZ3V0YTJtOE1mSzFLSGVGSTBkN3czUG1udkRtdloveUgzLzQ3M25uMGE5NGtPL3poRWVjY1VMMmlXN1JFaFlHR0puU0xoQzg2VjlvWmtpUk1TZGlNdVpNVk9OeDd2NU03MUtkME9lSXRBNEV6dklwWnp6bU8vLzRWM3kwdmt0MzVCblRCaEhGdVVDUVlENHZaajBVd1VhN3hoZGsxeHdPUmcrYndCKzkrazIrN0cwVkcrbUwvTGxPOFMwdlRMSFJXTXpZNEUxZCtUZnZ2MFZzQnM3VEdhN1RZakplM1FNL3pvcjIzRVZDTHpEeVA2WkZGNTd1bDN5MnB1SFQ0aGRPTGVWRmc1Z3p2UjNRcnNNM1FvNko5eis2dzRQWEh2QmFlQjJQbzNHQkhDMWVKSFpmb1lJbjRvbGJwVmwwakVTK2ZQMTEzcnYxS25mUDNyV2RpWkx5aU1kV3o2eUZ4U0NYMHhwZTdLU2N6ZGpaN2FWdzJBQXp5cEpwakpqYWxGT1BLWklzVUVsa2pRUVJuTGNFeTJ2dEllTTQwc2VSVFRvbmhCWU9sSEhjY3BJU2YvZXJ2K0hXd2N0OCthVTNlTzNXbDdudXIrTnBhR2hNT05WWnZOSGpwMnhzQXlKS3dVV3hGWFozcjhzNkxFcEV1WDV3Z3pNOUkwcVBjNW52L2V3N1BGemZzMlRVZUk0MHRxV2NNNjEwRnNST2FpRVlpaG1LM1JlbWhFdUh5d0VmTzE0OWZvMnYzZm9HaXREUzhQajhNZXE5SFVlV0dSbWJvdTlSaXRjemNwb2U4OEhEOXdoTHoxcDdnbXNtT3BWVUdYTSt6dURhcGQ1Y0ZTK2Q5NGY1U05pMzlsNnMwMXhORUM0RXlCcFBVVTA3NHV5MGpMcXJsOEdQMlJ3ZVJPblRnSE9PZzhVU1VpYUwwT2N0SHMrYnYvMHByMzd0Vlp3VHh0Rk1yNmF4UUtoM3hhVTFCVkdjd0xEdDZaWWRQUzEvK0pWdjhOR2I5OWxpVEF4aW1oUnJqZDJlK2JqVlFtc1RtUEVoNjdXWngxdEt5YUx5T3VWTWljNU9aMitESVpQR1VqazBtL3FXYTF3UjdFeTQ0TWxPQ1NId2FIekE2WjFIdkh2djE2emNBVy9jK2dvdkhkL21lSEZzcXJ0bWxKb2FscGZwdmIyWUczWENVMHVEUWNtTVBPU1VkVHJqemtmdjhvdGYvNGdZdHZobFlwQ3RzVEVFZ25ma0ROdHhhMFUveURodmpIK3dSRngxMldxQlpTR293dzhOeTNqSS8rY1AvMnVPT0dMRmdrRTNSQjN0R2puN2JzNGxWMHh0ZFhPdEowdGluYy80OE1sN25JOVBHTHVlcm0xTlNrR24yMjczNDFPQmQxZkZ5Wnl0eW1wWkNSZDdpZXlsU1dYQVhiSEs1WjFhbFZlUFpLdWNrWXM5akZOeUdtMzVkdlBnWHZFNTlISWhpR2UxeTVhWW0vd2ZCd1FKakwxMUprMEQ0b1c0MlBMT2s3ZTVsLytjVjEyZ2ExWkVYWk5TRHhvSlljRXdHaElsa3ZHU2tEVGl0R01oRFlmK0dnZk5NWnYrRk0waktoQkVHSE9jV09GWDM1dW5EYno5YzUzV0JObi9mQ3JNTUgwdmwzU1dXc1ZrOTMxVkpiU2VXdVZUQzVWSVJLYUNFazRVbFJHYVNQVENTZDV3TW5pZTNMMUhmbGM0WEI3eCtpdHY4T3J0MXpqa3lDWXZYRW1GY2VhelRYNm5EU3RUVXJTQ2hrODQ0NTBQZjgyNzk5L2xkUHNJN1FaY0dFbTV4d1ZqOVR0bkpuWkNjVjBneW1oSWFZN2d3VFdPMDlNMUI2c2pzdzRTZEhwQU54endyUy85Yzc2RTZUOUdUWnlQWnlRM2x2U1lFU2VXL2pJbVJhU3plK3VGTTA1WXU4Zjg5TGMvSWpaclkzYkVSQ1BOZEdYdG11L2p2eE9ZK3B5Qmw2OXdlL2FVcDhFV20zSmZwUVRJVGNyY2dDd3BQaWl6N1ByNWdyY3J6RDRkWEpudHBxeFJHNkV5d2NWMUk2NGN3cWRoNjJkRWdqbmc1UWEydmtNelpRQWtvaHNZWk1OYjc3ekpyYSs5d2dKSDYxdld3N2tKN01TSUtyaVNjNllVNHEva0dxRmlzOWxTZVBkMlMwU0w2cFNkbTJxK01DdTlTSnRQT1BVNjdacGNHY0RKVjVvbUwyWjBWRUJBVVUrcEhPUFlFbEV2REhuTjQvY2U4Sk4zZmt3akhUY09iM0RqNkJhdjNIcVZSYk9nZGQzdTJMQlkzVEJzdWZ2Z0ErNDl1c2ZqODBla0pwSkNaQXpucUl0MFhZT016b0NGSW5aYUUyVk1yQlpFTXlGWW9jSHQrY0MxbytzNGRmUkR6MEU0cGxtM1hIUFgrYlBYL294RERnMmF6eU9ic2NkM2xnanN2U09PeGs5c21vNngwTGg2N1luUzgrYTdQK1BKK0pqZURSWVVjVGFCbVFiTTVXdi9TZG9rQ1RpN1AxTi9sNHpQWlRFcHljcldYNTVQUUhaNndWeXNRVUNycE9tbnU3K0xzY3lDZG5wMVJQeVR0Snd6TGpoU3pIZ1BPVm5hZXM1V1RNSGp1SHYzTG56TmJQL2dBNnBpS0pDYTMyUEpnYkVjWkxBcWk1STRIUjdUOTJ2b0N1REJ6blpYdTFyRlBMNlllL1RzRzFjTFozeGFMZmpkc2VpbDkrYXRvcGh1ZHZ3QVVhd2lwMVBCTlEyU3JVTHB1TjN3VVgrZk4rLytGUElPOUFFbUNwWUdDL1piZmJNSU91TFVzZWdjR1c4c21ZTEN1cVNtZCtJY3dSVzVQWW9FbXdZanc5TVN0d281MC9vVjI5T0JseFpmNXQ5OTY5OXh6QkZWVG5zZHQrUTJrRWswS0drY1FRSlpyYzZZYTUxUnQyVGtYTS80OVh1L3Bqa0txQXYwY2FCZHR1VFJLcll5NTdKK1RtMitLazBrYXRrZllGTS8wTXNtWTdqNFk2Mzh0ZW00aTYydHlsUHNxay9kcE1adUtwaXFPcVdtcUFyQk5Td1dTMXhCR2ZPazIyNlRnUlpac2tyQkVpY2tURjMydmJ1L0pjdUlEMFVJSnlWYzBYTFV6L204bnRmbVlOQlYrVTV6b0doT2lxNmZaU25DMlQ2VkNjbllMTGlBb3FSc0d2bUM4UnROajBNWjQ4Yk1Qc25reGlhMGdFRE1vQmxWRDFMc2dFVTNEVkRSNHFwakhVK2QyYllwbXFtL2JBTm5teldkYTJuU2dwVTc0aS8rOEY5eG0xZndPQ0tKOWJpbTE1R1lJUlIrcDJvaWhCTGpTeU9FUUNLeTVwenYvL3g3MEJrbFM0UGw2NTF2TithM09qY0JPeTl5alQ5SmMvV0VaMDBvUWZ4NE9SQmVxOUxPNytPbCttUVhZVzE1eGtuTU5RNC9UVE00M1k3RFVzK0ZyS2tJZURyU21Ibjl0VGRveXB3UVk1eG1GK2NjNUZUQ0RVb3FUQTRubHFKKzcvNzdoS2JBMHc1TG1hbXhEd29JTVpsMjFiN25xZWY4NHRHUnE5c25DUkhzRGNCaTFkYnNvNXlVSnJUNDRKRnN4UUJ6VHBhVVNhWnB2YUd3T1ZvTkwxVzhGMXhqazJmMkl6RmJ1YVhHZVNRSmFjeUlkNFRRMnJXU1RQYWx6blFwY2VzeEg1NlMyNWNrTVE0anEyNUJFNWNzeHlQKzZ6LzhON3pSZlEwaEFNcjU4SVRSbGJJaTJlTnlJTWFOU1Joc3Q3VExqa0Z0ZWh6b2VldURYM0RuNFIyMEcvR05KMHFtajhsUTEwSlJreXV1MFZYWC9NVUgydVZSVlhHRGVmclF4T3ZWR3JwNWVwdFdNc1ZNa1VrSjlobDl3VGI4MmF3QU9pR1lGcXZSY2FZNks0YmU1Vkc1ZmV2bHlkUkxhYlFWREoyRVRPcWdHRk1pZUN0aHNCN1AyUTVudUlWbDk0YkdXVXhPcmJpZTgxZUNxNTlwKzZ6VGhDNlNVaGROVjJRQWpOUHBYSmhXckpRaWc4YmRmRkZPTjVGQUsyZ1dUZk94NlNhQWpHenF1MW1WWWVqUllHa3VVcGdpVXNJbWRuNG1RWTREWWthMmdoOENmL3FsUCtPUGpyNUJvR1BKa29mRFBkYmpCbGs0UklVMk5JejlnQmVoSHdkQzQxbjM1eXdQbHB4eHlwUDhrTGMvZkF2dEVzc2JDODdXcDNnbitCQW1VTWg4NlJlN3ZoOS9vRjF1VG5malFrUUtSM1Eyd1BKK2hOclNmU0JVUW1aOVU3T1phSXJ1V05SYVI3TlkwREFYclFoMUV4UGtrN1lhdkt4bW13VTdyY0NiWk1IbHdLbzU0S0E1UklGSUxER3lvcVZmTlVKSXBiU1BSNzB3TW5MbjNudW9UNmlPeG1ncGlzSlM2VVhUTVpRcWsxY2dqVThiSkhycHlTYy8vNnRNeFl2dG91bW9ycGdtbVNuQjBVeklYQ3BwV2huWUVNTEU4d3dsTnk5R0d5Q045eXgxUmR4R1JoMXhJVmpsR1cvM0hxYzByaUY3SnVLdUpYUE9uWlNJQWw2RXhyVWM2aUd2WC84Ni8relZmOEdLQXpwV1BOeDh4SG5jMHF4YXR1UEdDc1dQQXkydUtKQjVZczRjSEJ4d3FxY2tpYno1enMvWTVEUDhTbmg4L29nUUhPb2M0OWJ5MXhKNXV2WXZPbmllTmVIVmozWjZMelBsRjZsTzAvNTJja3l6KzFJeDhxSTRQZHYyTkpYWHJHRlJMUVhDZ1Fzdzlyd1Qydk84WjROKzBtYm1tL2xZdm5RV1YxZ0VMam1PVmpjSWRDakNPQnFIelNhRW5RaE96bWJpdUdBSitsdTJ2SC92RHBsSXlqMWRFNHpiVUlvWW1KS1RURFBQM0JRbzczeTZrL3FNMjhYQldIT3VwT2lHRzJwYVRGMG5PRmZOeEl4dkxEY3RsUlFYNTIxN01VWjhibWg4U3hCblVnazZJczRoelc3U1VRVVpkNWFMWUJRb1FmR3VFTGVUcHgwWC9ORXIzK0JmdnZ5dmk3TFZpclB4bkVqQ2RaNStIT2pDZ2p5TTVEalFkUjNibEJrMTBTNGJ0bm1MT1BqbDNaOXo5OUY3clBNWmFNWUYyN1VYRENDcmJCL1JUNEFLdjNpcjl0RkZ2NzJ1YURrekF3S2Z2aDFYK1ZnaWdrY1krOEVrcWwxamxSZ3YxSG1hNTVwOUZxMXUyM3Mvc2V4enptak0rQ3cwc3VRclgvb3FTMVltQUtxSm5IY3kwSDZxOW1IQjJ4QWFCZ09vT1IvT2NJM3VxbnNXRmtIT1ZqTlp4RVJxbk93REVFOERJZlk2dVQ3N3duN2NhL0FpbEs0cjZXbE9kbjlDMFFpMGduMkpaTlZPWml6OU9XSFllMXYxaTVvSDRoVHZnWkFZWFRUL3lZSDNBVWFsb3pIR1lUWnBLQ2VCdEkwczRvSlZmOHcvKzlLLzVKKzkvQmRjNXlZTkRldDh5bWw4d3FDYkl1dm5ySlNVQ3N2VmdpRVpsTitzV2thMVNpMXJ6bm5ydlY5eDFwL1JyaG95Y1VMcnBwaHFpYzM2endUZnZ0b1NtOGdYRi93d01HWHJTamkzWS9KNy9VUG4zRWNxOERIYlI4NjF2STlPSndWMlk2NCtwVThIZmV4UVBpYU5RVkltZUk4ZkhDNDVicXh1MnV4WkdkL09uSHJLQklGM3BHVEZLVElqQTF0K2MvZHRldDJVRzFSUnhQMzkxZ3N5Zjd4NHdiL0lGS0JQMzNhemZDVkd6TysxaXFERlJNK1llVmxERTFQdW5mZkVNYkpvQTVvU21tZG9hQkt1aFp1NHRlZU42My9JbjcvMEY5emdaVkxLUk4xeTNwOHhhbStyWSs5WUxGYmtma1JWMkl3RGlVaFlOclRTMHZzTkExdis1bnYvbVUwNlozbmNzazZucHJKTVFwSkJtOEY1OENYZlRGTUJWWDQzelZEVmNuM1VwUHVNYksxNzM2bXZzbVM4T29JVXFOWnBFZ0hOY1QrNFBKODljN0ZOamNGZXZlUlAxNHh2WnhKdXpubkt5TEdxTGRuUnNlQkdjNU5BQXhNd2szZW9sKzVNeHRBMDlQUWtCdTdjZjZkazc4ck9uOEFDcWhsWGdzV0dKTTJGZFNUWGdiVS91Q2JRNVRNMkl5Zkp1ays0MlhrRkdpY2wvRkhaQ1RQemQ2ZWF0azhGVWxmS2ZLZ05PRmVZRTZKbWl1VTRHcDB0RFhqbldYVXJ4aUVSQjJqbGdQRkI0dDk4NjkveTlWdmY1QWEzOERTTTJyTWVUOUJnS1VVQkt4eTQyWnliZUdyckdFWmJaUmR0d3lNZUFaRi9lT3VIbk1VblJCblFPSkx5U0tQdHp1TEF3QTVMTWkwUzRGZWdnWjlsczBXb3lEVHNoYlZndTkyKzBEWXVUUU14UmtTdGFMZUlCWU0vVHdWR0d5dzJnSU1JTVNiVEdFeUNKTS94OGpxcm92TSs1bHlrbDFQeEZmS2tadVNDeDR0bjFETjZPV2NkejlDUVVFbUFsQ0RoeFJYTjhTeDlTVE1SQ29GVkM3ei9lN2F3MWFCd2JjYkhMQk9Lem5oM0YyRG1Lazl0azF3RkN5MDFSdFRrMjRTSUQ0RVlCL1BMbkxCWjl6U3lZQ2tIaEczSC8vU3YveGRlNy82QUpkZndtSGJrK2JCbE0yNGhSSm8yUU1wc3R1ZjR0aUhtYUxVTW5OSXRsNXpvS1ZGNjdqNjV3NjgvZUl1OEhHa1dudlY0VHRNRzYrRHN4TWl0NWx5QzRydC8zcDV6V2JlQXZETUwxVmI2N2ZZY041R1RuNlVnbkkwa0t1cVE3TWd4V3NFQTltZENkWE4wS3lNRWRvdmNwMEFYaTJQdHZHVVJwbUZrdVRnZ2JpTmQ5cng2NDB0NEFwNldUVHdyVVg3THZzM0c2eW1hSHdac09GRWVubjdBTnAxWkRDaEduTjlWNnF5WExsTVF1UkkvbzFia2xMa1BLdXpLTDluZkx0R3pFSG8rdFdNbTAzV0FqNytpcVZ4bHJwdlBBbk56LzRJWlhBYVpLeE9WaUZrS0xwZnFPV3JaNUNrT1pKU0R4UUhESmhOeVI2ZUgzRDU0bVgveHJYL0ZTL0lsRHJoR1MwZFA1UEg2RVNvUjF4cUttV0syV2dqZTlQbEQ2M2o0NUJHdlhQOFNBejFPbERNZTg0T2ZmNWUwR09nNWc1UnBsNEZoR0doRFkvMnNLRUE3NTVBaU1oUTFGK2JGNXpqVUNvTmpucTFpSzJ1WVNpNVAvcGhWamJ5MGlTbHBzN1lZSXg3anVUbUVOT3VZdFh6dGJzem1UKzM5VCtZTWZtS2JPQVNKQmgrL2N2TlZNMmR4NURFWE5WbUtLR2NDVEdNZE5kUEJJZHg3OUNGajNoSzh6Y3JHOEdBYUszTW9kanB6dVNqUlBKK1pacXg1M1VYNWRXYXFmbkcrMjFNZy8wcys1djczS2lmdkVyZThRUEtRakhtZllka3VpV3NJUThjaHh5ejFtTC80MnIvaFZYbU56c29kOG1SOHpPbjZ4TzVKSTFZcHBtMkp3NGh2Rzd4WDF2MGFCYTVmTzZUUHAxWkRnQ2Y4NSsvK0ZiMmNReE1aMDRiUWxEamZPT0M4NWF6aHBJUm9Dc1ZNZGliODc2clZWVXlLV2QydnE3a29oWW9vYytCL2FoTUwzMkgxZTNQVUFwL3ZBSUdMSGNobVhmMU1KcEFKdVNrSmYwMElwREhpRUJyZmN0QWU0R2xNQUMwVmJVUEplMnBUemhVQ0s4ckFsc2VuajhCYnVveHJDdjVMM0IzOGJMV3lCTUZLRk40aFRWSk14WHJ1dG1ydTlua1JUSGthOS9BaVJlcDVMVS83djN5ZHJyeCs4MkRPck0zUk1aaXR1SDZPZkJpTDNGRktVakZTVlo1TThOVGhzc2VuanBVZTB1WVYzM3p0ei9ubUs5L2lCcmNBNk1jdGcxc3o1QTJ1R2NnNTBiUWRQcGxwU0hDY2J6WWNybGE0SkJ3ZUxGR1UwL0VKVzkzd243Ny9mNUQ5d01pR01XNVlIUy9ZRGh0VVBRY0hCK2hnMG5JdW1MbWUwWjFPaTNlZnVTOHpCOEpzemkzM0d0MzdEREdmYkMrRjZBSzRWdHVNVmxWa3ZJcit4YTdUN09UWHBvUUNoWnAwbHZmd2xJL2ZKcUp1R1dnaEJNWk5ZdW1XdE5JU2luVHBPQTdHOUdpRXFKWjNKQ0tJTTBub3RtbEo5RHhlZjhTd1hSTkNvQiszaFJwa21oRjJJcjdFQktVa0tnVFQzRkFUOC9UWmhHcThCaHplZ3JOVjVkQWwxRVZ6ekF0TTd1Wm8yeDUwVnpLRTZ5Q3MzOW56alhiSWJ2WDV5bFJueDZQektxTTZtZXhYdDkyK1RSMXAvMzBEckhRT2hHRWFCTDVZQUxNT2xxdU10c2VuQmF0MFREY2M4dS8rK2YvSXEvNTFXcFk0T2h6d3VIL01rRGRrTjFxSkpCSkR6T1FNbVlBUERYakhFQ1BIMTI2dzdwL1FkSUpyTWovNDhkOXpuazhad29CdmxMQTQ0SHp6cE5UQkRxU1lESHpCR0RxR1crM0l1WFl5bjgxcWxncVVQN2ZTSk1za1A3Z2JEUVlncUFoeFNDVzd2WWExOXRjeHA4YVhEYXJaeUxqWkUyZzRPejNqV0EveEx0T0lzTW1KZzRNbEp5ZFBXTFFCVVVIRUU5R0tKVnc0MFJxZHUrQXJYSEM4cHhrMjVSS0hBUytCYmIvaG9EMGlQeEgrNVYvOEs2NXhoRkpxYlhrYktERmxYQWdreVJBelhkZWdybWRneldaN3hyRHQ4UjAwVFVDY0NjNUlzS0lOS2hWTlZMeHZHYmRLUTB2SWdhVXVDV1BMcTlkZjVZM2JmOERMeDYvUTBESVErWEI5bHpmZit4bDNUdDZsV2NIZ2kzSldVbkpVMnJaaEdMY1dOUFVlRWF0R2trclJCZDhVYlJGS0pyRTRFMFd0bDZ1SzdPQ0syVnE4djlsZGt3cSsyQXQ3cUhFL3dBZGhIRWRjWTdsV2VZeVRNcGZGRVNOTjA1RHlTRXlwbEVreW9ScDFBVnpBT1UvY0pJSzJMUEtLVlRya2oxLzVKbi8weXA5eTA5Mm1ZNG5pMk1RdDU5dHpOdU1XMzRIM0hWa0huQ3Q1aUtwa0haSHNhYnNsRHVGczNPSTZ6NW8xMy9uSnQvbnRvOS9RM1dvNTdkZTBMa0JLTkg1aE9wdURFYm05RjBya2U1cHdLb2lUWitiOVZTdjlWZGtObDF1ZEpHM0F5dXo2bWpaL0NSSGxqQThHdG5qWDBMaU9zMGZuTk5FUmtoTXBnZE95dFQyZ0tkaGdNRmxtazlIU1hVMHBWOFF1Wi9MTExqdndkZWFkM2ZTUDBlWW5YamxnUVpwZFhHYUlISWViWFBNM0FHandQRDcvQ044SVkyWGdPMU1lTmlNeEZZYjNDZSs5L3o0QWJiTWc2WlljRThGMUJOZWlZNFprK28wK096UTZEc00xcmkydjhhV2JYK0tObDE3bm1CdXNXTkxTRWVqSUdOZnZZSFdOYTkrNGp2NUMrT0QwUGRvT2todU4rYThKajZjTEhjNUIxTVE0RHJTTGhweWloUm1LRnJ3NFI5TTBoYXBHU2MrcHFDQ0ZYcEVMaGUxaWdQb3lPcEtsK0ttcVNFbDhCQW82N0dsOHcvbjV1U2thWThDV09FL1hOQlpRVFNOZDB6RU1FYUluUldFcHgzUmp4NDF3aTMvOVovOE50NXRYdU00TlRJUjA1THdmMk1TQnBBTmg1VkdOOUhHTG9iQm15aTNhMWdSUyt3RWZNZ2VyQTNvci9zUjNmdnJYM0huNFc3cmpock40aXJxRUMxYlV3Z3JadUNtRjVkSWNQbXR6Z3ZwVlp2bkg0WTFxUVMxVlM0SnNSUlRGTXhsdUdIYmdTd2hvT08veDZnbnFaMW54bDJ2eEJWdmlyT3FHWUQ1Wm5RSGlOQU9PWldBVmJYT3Q4YVdkRHpNNzNObFJzMXZSTHRHV2RtMFhkMVBMNkUyT2E2dGpsaXp4QkNLWnZqalNXa0lMVnFVeDB2a0d6UVgyOTQ0bjU2Y2tMNHd4c1lrRGg0ZUg1RkZaeEFVZEt4WWNjYk83emF1M3Zzd3IxMS9seUIreG9NVlJlWmhtU0NxT0hxc2k0NEpEeEhPTm0vejNmL0kvY0JJZjg1TTNmOHlIWis4ekxMYTRWZWJzN0lrRkg0TWdYZ2l0SlI5NjM0Sm1tOUY5UTYwS0hyT0ZJV2gyM0UybnU2NGpOYlF3d1lNekZFMHJqemVUaUJpM3JraGpxNE5vR29jaXdqaGtsb3REQU1ZVWNUNllRdkNRYUVJd090Vm1pNWVPUmpzV1hPUDI4a3Y4eVZlL3lSdEhYMlhGSVMyZVFVZTJvNjFjWTRwV2hiTnpKQ0lxSXhLTXl4cndpRHJpR3NRTDE0K3UwYk5sd3lublBPRzdQL2syRDg3djRRODlaLzBUNkpoNGw1TUc1THhmWFBIOFdlMlRFckpyUW0vZFJ1MExOZGR3Zmh3MVpuZDJkc2JTdVJxZ2ZPcTJMVTRtNWd2WWhLZ0Z0aXhrUisvcGUyTko1eHlOZFRIRkRUNWVNUHJLQWFZWUlaaGt1aFNqMGtySHF6ZGZ3MkgxeDA0Mlo0VFdzeDIzdUdEMHI1UUdRdkZSalBqck9EL2JNUFlSSHhxQ3RxeTZBNGJUa1RkdWZabXZ2L2JIdkhUd01nY2NzK0NJaGtVcFV4RnFFam5tVVNoUHRvL3B4NkdBSXFYamVNL1NyV2hvNk1LS2YvdXRXNXpteDN6M3JlOXcvL1F1Q3c1cGxvRk5mMjQzU3dUTmpwZ015bkJPeVZWcTNKZmdjZkJFQnJ1VzRncm9ZU1djYW5YUGllNDF1MmFUS2FLT0hiblppa01FRFlYdUl4TW91aDQzSm9XT2trZXJZTnE1bG5FVDhkcHc2STd4MlhQZ3IvTm5YLy9uZlBuZ2E5emdGb0dPZ0dOZzRIemNNTVNlNktMQkR6bVRNa1FaaS9sdE9oMW9ZNGF1UWhNQ1dVYjZmTTVhVHZqZXovNmVPMC9lcFZrNXNpUTBRTk1GY3N3VEg5Rms3UGpNNlh1ZnBsVkF3K0prM3Z4VjlRemJnUVdyNS83ZTBNVnlNalZIU1dORzJvTC9PMHVVZEs2eEc0bDE2a3ZDb0UrRDhwK1Jhek5KbCtTeWlqbHNOa3llMTI2OVRrUEhTT2FzMytBNklRNGpqVGJHcU5lTTg3NmtQeXdZR2Zqd3pqMUMzM0JqZVpNM1huK0RyN3o2NWNJRVg3RGlDQkFVVDB0Ym5pZjZaSFhCdHVPVzAvTVRmQWlteGRFNGttWlN5cXhQQmhhTEJUMk93NE5Ecm5GRXJ5MEx0K0xmL3ZIL3lNaVcvL0xtZitIK3lZZW9nejV1Q1Q3Z1FpQm1zd0tjY3d4eE5ITWsyTFdMR2tzVXhCWGEycTVxNWtXZmJMK2crUzVFMmlhRHVBVURMSERHYk0vT2JPbnNFK296U1NLZGE4azlNR2FjdGh5bUk5cCt5WmVPWCtkcnIzMk5QemorR2gwZGpvWklvbWZnUVg5bVRORGNvekthem41UWlJbVVGTmNhOTFFVExMb1Y0emFTTkhQOStKaE5QcWRudzJsK3lIZCsvTmM4SGoraXVRNzllTVptNkRrK1BqWUVzdlNCUy96UUdaLzBhZTNUY283eUpVdHN2ODFqcGhXNkZ4SHlhT0VOVlpQV3F6N2pWV0hMUGNhSFU4dmZHdnVJYnhvRVl6dFVPeCtLbzExL3FEdXI4Sk0wV3loTURzMjN4cTcyc1VGR3p6VjNyWmlLRWVlOWlhMDBucHdOUEhDaWFJcU1lRkJIS3gxLzh2VnY4Uy8rOUY5TjRZaUE1NEFWaXREUTR2REY5T3hOQ1lyRWtEWkdRZ1drRmJMTERISEF4WnBKbkFsZFl4VXVNNXlkbnFLcUhGNDdKdUE1NWphSmdYLzdqZitaMC9pWTM5ejlOZTgvdk1PVHM4Y2syZUliUjU4MytOWXlESHpyR2ROb3M3VGFNZGFKeU5TSUZmQTJBYzN1V05LQ2ZPa3VGaU5hYTB5Ym1aMnlaVGFMT0x4WXNGYVRvM1VOMi9WQUJBN2tpQkFEU3puZzl0R1grUmYvMVgvRERmY3liYkZPR2hveXlubzRZek51R1h4Q2c2TE9rUklsZGpZYWRVdWc3NDF0MDRZRnArZG5IQjFjb3dtQkIrdDdMRmFCeC9rQjMvN1JYM0thUGlKMWtjMHc0QnZIUWJka08yeW5qTzBLNEZoSXh1MzF1ZC9GaXJaUG02ck43VnNUNGdrU0NIaUd6WURQY0FHdXZiTE5ndEVHeGp0MXBHMUdEcHhCbUNVbWthb0pReW9TWGhlUDhKT2VYR0hKYXdKMStCeTRlZkFTRFNzZzJFelhDR09NaENDTWNheGx3QmhUUWlRVGsrSkQ0S2g3aVk2T2dZR202RFQxeWNSWElwSDFkc013V3MyejVDREdIZ21aVUh5dUZKVVlNeUdZajVaVHBna05LVVZPejg4SkliQmNMc2tvSHp6K2tKdlhYMEx4TERpbVE3Z1didlBxRzEvajVOVVR6b2ZIdlBmd0hlNDhmcGNuK1NFNWoyemltZWs5eGx6c2VvL2tNSkZ5YTNsZEt5SmhjVDZwUmU0a2wvdTU2M3l1MHFXeXBhUlVzOWtKZURvMGpnUVZnaXhacGV1OHRIcVYyd2V2OHZyMXIzQjc5U3BMam5Dc2NKZ080c0E1cC9HVWsvVmpOQ2pSS1VNcTRZVHNFVzFLbmtjaWUxUDFiZFNZSW1NZk9UbzZRQ1d4WVl1c01tOCsvaWsvL09YM0dlU2MzRWEydzVyRndZS2trU0VsZkdqUXBDOVVFZlB6YXMvSzdzK1UxYWxrZndmeGVHa2dPYlpQenBHMGY4eEdFVFN3WmsrdGFvODZwV3ExdmZxTXp6dmJlSzhFamR0NVlxWGN3S2VLU2FlVUxDYWlFU2VCVUlyNWRhd0lOSnpITFVNYXdKbmpYcHNJT0t6b09hSkVTUVRmMHVzSUdiTExQRG83cGZVRzNKaTRyNUFhQ3dYRW5LSEorQUI5R3NrcEVrSnJBVTRSWTVMSGtWeEJsUUFTTWcvUFBtSzVXdUU3eDlubUZJbUIzR1pXM1FwVVdNbzEycWJqdUxuT3JZUGIvTWtiMytJMFBlVGg1Z0Z2dnYxVE51dHpRbTUzNVp4Y2tmTVduUXBHaU1TU2twTE1KTm1yZyt6M3lMNGgrYWx1YzQ0MkhYc0pOS21oM3d4Y1A3ckZOLy9vVDduZVdnV1ZGY2NjY0dneFFBS0tKeEY1c0wzTEdMZEU3WEd0V20wd2hTNTA1R3pha001WmpFb3h5NkthK01FNW1tWERvRDNlZWM0NDRSL2YrUys4L2NFdjZHVk51M0NNY2VEd2FFWGY5NmhUMmtWWE1wejlWUGxsRXZncGZXMmVadlY1cm1aMW9FMlp6MWZzeHF5RzRvOWx4M2E5SlVjMXBQSENHTHJZQXBTcTl5blNoazUwWE92MnlaYjI5b0ltTkNnajJ6enNvVDdUanFVR29sK01rZjgwOWtNY00rcE1qRVhHd0t1M1hxT2pJeEU1T1QvQnJ6eGpOcFhBSmdnNWpwQnFla3dnallsK2lLdzVvL0xNS0ZuYm1ZWllWZ1B2SEFsSGlxYlExRFJDaWtZZ3JvaWxFMHV0eUlCNEpUdFQrdldOMEtlQmJ0bVNOS0xPRWROQXlKbGhteGpHYzF3UVFtZ0p3VXlLRlljc1dISEwzK2FyaDMvRXQvNzV2eVFWR1B2KzJYMCtlUGdCSHo2Nnl6WnVHY2ZlQmxualVERy9KbUdoRXlrK25XcnhBMUlwNGtkQVlxQmhnY2ZSdWdXM3JyM0U2N2Uvek12WFgrV0FKUTFMQW1YVkxBUmdSWWxFY3U3WkR1Y01PaEMxSi9yUlNOY3g0Nld4U2p2amlFYWxEWjZvbVRGbkMvNjNTMVFUd1hsQ1k5RFJvQnUyYlBqMmYva3JIbXcrWUhtOUpmVUR5VFVsWlNiVGhJNlVSelFtZ3JOczU0dSsxMFcyekZWOXByNzNTZEhFcTFyRkp5clE0WndqSmlBcGk4V3FXQjdDd1hMRm0yLy9pZ1pmVE54OWQrb1NoRC8vVUhJbXVBWWRsVWFEeGNTdUdEc0drRlF6NXNWTzRHTGk1L3lDaHBKK1F2U3Nta09PL0hVaW1ZWU83ejBwUjRPbkthVjBZQ3JIb3lRRVArbXZXMmxUcGhoVHpNa0lwUUpEaWxPd0dCSFNkckFzNFN5V0NlNGRPVWRpeWdSeGhEWVF4MUp4QkZQRmlqRmJockZ2Q0JKd0FYSWFTVGt5OWhFM21zYkd3aS93RWxpR0ZZaW5vYU5qUlNxMXlRNE9iL0tWdzY4emZxVm5vQ2RpTWJjaDk1eWNuL0RveVVOTzFpZFdvalpuWXFrNXRtaVhIQjBkY2YzNmRZNVd4elRhMGZnRkxTMGRMUjMyUEJSeDB3V204cHNvQmR4VFR4OEhodGlUZEVBWjBDYWpIdENFWUQ2U1MyTDBOdWNRWjhwV0tnbmZObWdLRm13SnhrbnM2WWxzK1BEc0x2L1hqLzhXN1NLTGE0NUhKL2RZSEN4TllvRGRRSEhPN2RHVVBrNy8rYVJ4c0JmZHh4ekd0d0VIVGdLU0ZZK253ZU5UNE1tRHh6U0VTeUk2VjY1a3JnaWhSQUdUVmZiMG00RW1OelRaMGMrVVo0RlpaZnBkUE1FTXhrb2l1NUJTOFpUWnBzb0EyQXNodUVEY1ptNnVidE95d0JONHZINk1hNFErRG9qWVNxWUZiTkdDNkdUcERWcUZvaTlvRkRCRFpkU2lYNFhoNFZTTDJJbkIzbDRzcFNlWDcyb2NhNFNZN0J3cFcyMHY3MG9jVDIyd05kNEl4K1BZRS9OUW1CUE9razZ4TE9QemtqRndPcHdRUXFBTkhaMXJTaWpUMDBtSDR3QWpxK1d5ZWlyWmdSeUJIbGxBd1VTK2QxanVwTHRZdGhOb3lHVVltVCtYU1F3a0hCN1BnK0hVaEhiVVlQS1lNMWxTcmY2RkkrSnlTWFZKZFYrVzRaQTBnbWFHSEFsdGczakhtSHRFQW0zYmtzbEVSaDRQRDNqNy9WL3k3djIzZWRoL3lITFJNS3g3VnNzRjNsVjYxR3cyZGc0aklHTjIvOGRvbi9YQW1wQzdHaFdwTWJJUytoSTFpVUVRZ2pRRTErQXlmSFQzQVNFSEhDYVB2K3ZuVDEzSnpONVVUUVIxOUFQSW9MamdyTmliZUtRdzhHSGZxWHZ1T1R6am9xZ21ORHRFSEJJZG5Wdnk4bzFYRVl3M2VMNDV4Ni9zZStZTFdQMWdNNTNNN0RFd0lPSzBRdDcyUGEycVc1TElCUUh5WXFwTHh1TnprejhRcE1Ld21iWnRhYnFpVHppT3BpZmp2ZkVicGNLMnBwb2JnaUdqVXZQTkZFdnpFR2RkMkNsWkVtUHUyV3pYZUR5QmxsYmFhUlpzbXNZR29XdW5BZVJLUndkam0vanllcHBNeW5jQWhqUVVXUUh6NlliWXMrblhEQ2xPN0l0Y1ZvMkVVdXRPVzNxTFhiK1VNcUxCeWlHeDg4T0ROOU80YzRHb2tad1NpOFVLVDJCa3kxWTMzUG5vWGQ1KzcrYzhPTHRMQ2dOSHR4Y2tScm9ReklkRGlDbmJDdWdnNVlnWGorWkNJeXNxemw5MFBLd09ydDFSekxQS25iR0V4T0txY1J0WlA5bHduQTRuVnY2eldrQ0xHcXhZalNlZkhXNFUwcm9uTE4wVXUzQUlXZWRZVEQySWl6VG9pNTgvdXhrRWJlVGMxaTI0ZGVObFFJaEUya1hETnEvTkpCUlBUakl4c0NOcVpwNExvRzVYcmtjaXRaeU8vYWF3U01xUmVSRmMwUVZKWThiVHN1aVdwdXVua2NSUUtqU0NheDNTZXFzRDFsc0NJeWtWK044bW9kQUdJSnZXWVVIZzFSY0kycWlJcFFRUXFMUHFLZHUwUWRUa3htVlVHSTJZcldwcE8wM1RFSnhIRmRxMm5XSmtxb0tXR3RMam1BeDhhQlRWWFNYTk9kRHJRMGxQcXVXUmlGTmxVQ1BFT2l2Q25qMU9rNkdaWXRmR0tFNUsxeXp3NGxtMHJxeVRrY3pBM2MwZGZ2S3JIM1B2eVgyVzF6cmNvWmFReUlqM1dvUjhpbnlkVURSSWRLY3hVdTY5QXJWYTZpZHJuejQ3LzJLcng3VkwyclZLcjQxcjhCSjQ4dkFKTGxsWE15cGkrUjJWN0w0ZjJnb2lKcTFtSFVJUWRSTFU2L3BremVLbHBkbWNNck9uQ3dJemNjWSs1UVFrSXVTVWFWbXlQUms0YXE3aDhHeFR6NUFHb2xqQmkxb29lK0t6T2NzLzgvZ0NjT3lPSnhjR2hVakdlVnNmZGlwWUdaemd1NFp1NVFoMFJCMFlkVzBWUGtrbU81Y0hZbElXellyT3IyaFdqY1dpaXJobVNvbCsyQkthd3NZWGovZTJ3cWdxbW14bVRPelNNcXk2U2pLb1hqTGJsSEIxZlNvc0VDV1RrcWsvcVNxYnVLSFdWcXZYdjl4UmNzajAydHY3MkExMytJbG5PbzdScXF6TVZqcXFjMi9kaU1hMWlQZEZYczc2Z1c5YlBKWk8xTFJXVEZDS1dmdXd2OCt2N3Y2Q1gzLzROdXQwVG5kOXlWbDZ3clkvWTNYUUljNFJCeXU5RklMMUxRa09MV0pGWmpFSlZsYTFsaVArUFd1bHo5ZFd0U3k5czlKZDcvejZMWE1aK2hmVHlncEdCalpvWGtUUUlybDhldktFRlplWHc3MUV4Nmw5T3IwUHlWYTk1T2pnbUlZRllMWi9QMjZSenFCallzYmp3TmNNQU1GcHNQcG1XVXZKSGtjV2h5c21pRk9sY1FIRU5QYWxaRWZINGdNbFJrNDVJY21XdGE1NWRQSVI5eDkrd1AzSER4aUdBU2VCMTEvK0E3Nzg4aHRjWDd6RW9Wd0Ryd2FSKzRCRFNKTFFsRWlGc1ZDSHU2aDE5aGF4YWpuUitKY2lRdHMwVmtFeWorYlJDcmpDWDR5SmFiV2VpaHdXdnB5SUVHZStROVNJaE9JZkp5c29LQml5S3BJdEhxaXhTQlFJcUVleU4raGZiVEp3T2FGdW5Ld1p2Q2UwSGljTmxQVnZ6U2xuNDJQdVA3bkxyOS8vRlNmREkySVljRjNpSkQ3RUJjL3FlSVdPQTE0ZCtBYmZXaVowSnVOQ1cvemxRaFBRMlNyMkNVbm1uMVhiS3pDaE9oMmJ0VUovcS82MkNNRjVmdm1MWDFvQ1Zva1hYeFZlbUZLWVpCWW5FelhRWTB3UUNBeWJhSTZkRTN5NVFLb0pwVktBS2lIbGt3eXdtUlE0MmFEUkFiNzFKMysrOHoxRWpNcWs1MERSOFhERkRJbXhCRjBOc0JDWFFUemVDK3BEaVRWSnFXNEpnaXM4L1I1UW93dWRmc1NEaC9lNDkvaDlSdDNRcHkxajdva1N3YXV4SExManpicy80Y01uSDNCemRacy9mUDBiM0ZxOHhNSUxlSXNQdWV4UThVaEtVNFZJVFJtMHhKU3lRZUtDWnhtOERlL1JPblV0U1RXbHFyaWRTQ25zUTlscHBnU2NKakNISWthcitHeDZIbGxCMDJpK1RpblM3blIybDdLNUJXU0hWN1ZxdXpLaVhwRFdRL0FrUm1MSk9qK0xKL3ptL2JkNTUrNHYyZW81MFprbWZXSkV2R081Yk5sdXQ2UnNwcmlJRUVKZ0dBZGJRWjNRK0FwMVcvSEZjbkpGR2UzVCttSWZ6ejI1M0hUNmRlWGtxa0IyUXM0R1BEbm44T3Bwc21IRTkzNTdqOXZ4WlJvQ09hY0p5YloyK1h4Q3h0Skx5Sm5nSENPZWZvaTA3UUVuajlaMHQ1ZW9oODA0NG54Z1RLYmc2OVJVYWFXd3JpK2FqWGtXUUxXU1NHR2FvWE8ySExhVUxJQ3NtdkRSODlycVZSdmdhZURzN0l4UlJuenJpWEV3dnA5QUhFYkVDVTJ3azIrZFVYdDlNQUpzbnhOU1ZLKzJlbzZUUU0rR3MvNHhkKzY5eHdmMzN1ZThQMFhGUkRPSHZFWjhXY2xia01sZktkZmZSYzdTUTA0ZlBPYTNILzZhTDcvME9uLzgxVC9oNXVJMnpqdTg3K2lhQmsrTElzUVVpWDAwZVFTYkE0b3lMMFExLzRsR1RDb2NMZnVvTktKQ0lxYWl0NFliQXBPS2xvVmF6TlJMcWhDbEZGNXc5bHBNR3MvNG5VQzUxbUVhc0dhdStuTDl2QThzMjQ1UlJ6YTZwUmlzUEJvZThmWnYzK0w5KysrUi9VZ0tBOG1QWktJbE1vcWlqS1J0cHBWbU9yNE1oaGFIZHZLeThwZ0xlRGJ2ZzRyem45VXE5cXh0UEd2dzJVb3RJcEFjM25tU1dtRjdDVEFPaVlWdmFIemd3Qzg1WU1XOVg3M1BBVXNXMm9oRUtScU1GbjZaSnNjeTIxcis0Q1JhWjhMNU9TcGVISzAwNHBMVDRVbms0S1dPWWR6aXhaT0t1SXFJWllZaWdudk9OYXFVbVp6enhFMnpudVdwY0dVZU13ZmRjbTlXYzg2cVJWcWFqU25paGhEb2x1YmZKSlRSeE15d1VuYTk1U3ZwZ0tyeVpQT1lEKzkveVB2Mzd6REVMY2tQSkIwWmN3OU50cFVvT0RURjRpUForZVM2U3BkRFVaZEpPZEVzTzRJNDNuLzhEbmZ1djhNcnQxN2pxMS82T3E5Y2U0VldGdVpyWkpOTTZGWkcydTJIRVlmbGJjVnhuT1dPbWR5YUZRUmt1aUZqdG16Y3VYazRnUWU2bXkwcmNUczRSMmJIa0tod2VBYkwySFptb1V6b2F1Tk5BY283bTFTd3hNb05QVWtTU1FZK2VQd0I3NzcvTmgrZFArSjhPTUYxT21XRTUxSWdYVnhWa25ZNE5kc2pDL3NtVTBGN2RmWitwZTk5dGkxZmVQeDRLMXFOcThwMHYwMENQUmN0bCtYeWdCWXJqbkhVclBqKzkzK0FiTlRvY0tyRU1TS2k1dnVMVEtSaDI3WnROT1NpWFJpd2lpbytOTWFHR0J2V2p6Y2NENGY0MWpUU3h6eE9NTE81N0FGS2ZPYXBseUR0VHR3WHMyRjNNRUxXUUdnNlhudnREVUl0MWgzTkwxWW5OR0ZCNkF6Q0htTlBsQVRFVWxCODVDeWZzZTQzRE1PVzgrMEpIOXg3bjQ4ZTN5ZEx4RFZBVUxaczdhZ3RiOUY4bTVTSlVXbWJVdFM5bUVkMmRhcFlUb212ZVU5TWE4WlJhTHNGeTlXQ3g1dTdmUGlQNzNINzZHVnVYYi9OSzdkZjVkcnFCcG5BRnF0d0dWcGpONFRnYWRxRnhlU2lCYlROUkZmd3RuTFUybC9NYUVXcFpDL1hkakVjb2lWakFrQ2M0SDNKaUs3aEN4eHRWK1pSRDk0TEVXT1NWT0dJTEZzZTlZOTQ3LzEzK1BEK0I1eHNUcUJWbXM3akYyYVJPRytUanNQWnRRSG02bDZWNHpmdldITUVjZjc0K2JWUFlpNGFEOFp5RkczeXI0dnJtSlhXbWZoRm95MUw2ZkN4NFowM2Y4dlNMeXdUd3Nua2t6RXpGeS82YU1FZ1ZIUFljMVphS2NSWTlmaU4wRDhlNlY1ZU1jalcwRWVzYzBoMHhoSjRKc1Z5NXhUT2svSXMyOVJrNVhJVVF2WmNPN3BKSkZrWld4elNLdDRyd1RsNnRpUkdYTERuOTA0KzRPNkQ5OW4wNTZ6WFovUnBZenhER2NtTStBVzRCckpMREdsQUZ5WlhZTEVpbWRMdVZaMHQ3VG92RG1pWjRwVXZLR0FCWitkcEZnM295Rm5jR3JDeENueTBmWitUK3cvNXpmMWZFY1J6L2RvdFhudjV5OXc2Zm9XV0R1L2FNaGxad05wNVI3ZG9iY0JsWTJKbzRTUGE0Q29DcmdYU0g4ZHhiMVc3MkJiTGRscloxUGxwZ0VySnpVcTV0NEF3a1hOZG93SWpBdzhlM2VmOUQ5N2o4Zm9CWjl2SHhHd2hFM2NFUSt3TGdWZEtlZDFkV3IwQks3S0R1V1ZuZjN5U0pNc3ZzbFVzZ29JNDV4eXRuZ0ppcFhqREFvbkNvbDJ4OGtmYytlVUhzSVhPTDBsOXd2dUc0SXE1bU92RXZNKy9GQkdDRmhscko3dXNWSTFLNXh2UmRLRGJEOWZjdUhuTTJoVTByY0RqV3FnWHFobEtvSHIvd3U2WTFTS0NabE44aXFNVmZIUGlhSHhMSEtDaFlkRXU3UHZla1NXU1hXSmthN29kbkhMbmd6dThlK2MzbkEvbitFNklNcklkZTV3dnMyMEh2akgyeXBqR1F1VlJVb25aMEhoeVNrYWl6YWFBSkppUFlrL1k3OGcxWk9GdHZ0T1U2Wk1GbEgzYklKcEplU0Q3ekpnM0JQRWs2ZmpnNUpUM0h2emFDRTd0aWplKzlBZmNPcjdOellOYk5MSWdGdTBJeStUMmhRR3lLM0pmZ1l4YUFNUzdacnFpay9iNnRGcFlubGlaSXFtMXFDMnpPN0lsRWwyUGtObmtEUi9ldjh1ZEQ5N2g4ZGtUMUNkODV6aVBaL2lWd3djczdTZEgzTUsyZ01pMHpYb3YwVGt3WXpTM0dwZWNoM3F1NnREUGFwOThVRDV2QmR0ZkFQYjRrR3JYUFdkQlhUTHJKM3ZRUUVOTEowc1c0WkJWYzBTVE8zNzhkLy9Ja2lWcG0zRFprV1MwbGUrSzg5aGJ5U1pxaTViZ3JZQkdhQ1FnQ0dlUFQ5RlRwYjNSc1hVQmRhT2x2Vmhwa0ltWE5qK0JpeGRNeTBwUk80aU5jc2VJTVRuYVpVTmt5eU1lME9tQ2Z1ajU0T0VkN3R4L2g4ZWJqOURHZklFc2lkaEcxbkdMK294ZitpSndZdHZjbG9JVlRkZVExWlZWd0JOVEpyc1NXUFNtTmFnb2piTzRsN3Z5NG5oTWVtN0hFRytDNWFUbGJINGFubUtXV2xnZ3BoRnBMRmt6NVMzclBQQ0xPei9HdmRmUTBORDZCY2RIeDl4KzZSVnVITitrOVV1Y2VocnBDb1hYVXRubmVRM0toaXFwdWd2ZjJLeVFTeGlpcGg1bU1nTUQvYkRsMFpQSFBEbDl5UG41S1UvT0g5UDM1N2pnMENZenVpMUoxRXlsTHJIVkVVbG1hZmkydUFNbE8ySTZsTXFvb1FhUUxYSnBkTGVyQjhuSElmQStyZTk4MnZicy9WdG1wTVdLemUweEZEWVkwZHQxSERSSEhQZ2pUdDQvNTg3YjczTmRqOG1qU3R0NllqK2cyTVJzNGtsUDhjbklNaTF2OWZ5czhIVkdSMlRoR3QzYzI5QWVOYlROZ2h3aVErckJHZVJnLzE4a1NUS0phMW9GVENiVjExcWdUc25FT05pTlhBMjhkLzRySG43d2tQV1Rjd05KZktMUFBYTGdqTm1RWmFKTU5VMkhTa1N6RWhySEVJMi8ySFpMUkR4RFB3S2U1ZUxBeUxVeDRyRnlQK3BLZDh5bGpKSXJYWHFhS2NvS1BKMk1XRHlrQk0xajFySnFlOVFwb1ZzUW8yVXBoR0RYTGNhZVVhVEE1d0hKUFFPT3RaNXdldm9SNzUzOGhzcW83NW9sd1Z2cXkzS3hvRzBXUnRBdEp1S3lYUnBJa1V2NlNjcU1PWkhIV0FMaTU0eGp6L2wyUTk5dmplOVlTeU5sRzRBaGVGeHJjVFZFQ0N1SDVFd2ZONFEyNExRVzFZdGtMSGsyeDRSekMxeW9TYVVXMzFLdDRJcEFEVENYUzNYVjVQcHgyOU4rOC9UQjl6U0M3b3NCTFBQOW1Uc0RPS0ZyRndUZjBia0QvTmp4a3gvK0ROZWJGSHpqakJUZzk5U21uOTZDVXl0Tm1qU1IxTWlNemptRGtvZk13WExGazQ5T09IempKYlorTUkySFBDSXVnWmhtL2NXRDNic0V6dTJWbWFscXYyWjZDTWtwOTA3dWNYTDZtTGdkYUJ0bkpDREphSUJoSEZFUlFsZ1lNNkpvN1RseGRzelJPSGFpenRMaEthYWdlRkt5UEtqYWF0NlM5NEp6V0FxTHIvbFo5VHhtTXhFT0o1NmNvdm0xenVPOUJWUXJGM0FjUjNBQjhSUkNzUTE4VjY1SjFnUTVGd3FZMVVTckZScWRDNXlsQVkyS2pvcXNaV1k0bHE2U21hMyt1amRycXFycFpKYk9ubHZqbUJpOUxCZlVUQmwxS08rWGhTa2J3NlJwV3ZyaGZCclV0WUxsWXJFZ2pSSHZyVVRRem04M2VOcWUyMm9xTDlpWlAyMTdYajdaMVNUMDUveEdqQXNyTWt1RlVrdThERzFIMEk1R2w4alE4UE1mL3BMT0xXRlE4ZUlaTjJ1NnJpUGpwLzROVUFWeDkzd3lMeWJ1V1VVMWN6TFFRSnhEc3lQMVVRN0RTajk2K3lOdS9WY3ZjejV1YU5zRll6NG41NEhHTHhpR09NMjhjNGErclpBMVNHczN2Y0xZMVlkVEJYRU5VUlU2UjFTZEJsbXVNU1VjR2pOSjJRVjhBU2RtWkZsUnlFS2JJaGNOSUt0SDVzdktKUWgraWpkWWg1dEVXMlkrbVlVYzNJd0VMY1V2a2wxY3E4RFdYc0JxVHRldm1pUmFVa3RvTlNlbGRFekhqdEE4a1haSGNMS245bHV0Z2lreDA5ZWpxTFFybldKbmlGa0l0VkM5MGFiTXg1WkMzWEtGTWpkMXRsTEhyTXEzTlUxWDRta1FYQU1LYWJSc2FCdmdoWlE4aTdWT1RQWHkrbmZWcnA3SUw3NDNUM0RkQjJMbVlaRGFYNU5hcHIvZ0daTlJJNDZ2M1lBVVdMVkhIUGhEZnZydG41TTNZdElNUmtPbGExb2pIZFJyTzQ5ZXpQYWhxZ1JmT3cvT2hDVFI0b2ZrZ293MEVDTiszVEE4R0ZqZHZzWVFCeHJma2hnWmhqZzU4bk9OaHFkZmxOMkZNTkt4My85RTlxL2JSRzJUdmU1VkxxQ1prQjVLZjg3bHg3VVRWc1Rud216clRLeG05czFLb0dGbmZrejI0L1RvVk1raTFwRUxNZFRpaDhxT1RRNVNUZVFyVDcvT2VBNHRnWkM4MTJITGRtWUR6MW9xMTZkZW9PcERwdDEzSnNGVHB1OW9uVmxuWjFQalduYUphaFdZM2FrYUFHVG5VYlh5cHc1cmVOZm5RTXY5Zk52VEVrQWxlR0pLdEw0bHBVd2JXalI1VnMySzQ4VXg1M2ZXZlAvYjMyZVIycUl1SGNtYWFWMWptZTB2VUI1dFAyblRDVW9rNVFnWUROeW9JNDhpYmQvbyt1NmE1bkRCNGNFUjJ4eEJvdDEwcGNnbVBtdGF1OHFzeUhpMS9DK2hiQVBCaXMza0FxZVhpMVFVV25NaFN0VmRlVTBsUld3VzR5cjdVcW5kZDVhNWlpY1hveXlYWXpDL29vcloxQ09ydjV6UGpOaktBR1NwdW9odUQyRXlrS2VhdEJjbUdwMU5QaXBVY09YeWFOUnBqTmY2ekx2ek5WL0lvcndaaXU2aWJiN201emtzS0RpYmpDNXNmbmM5MkFNdTZuQ3NaKzNyTVU0dE0yZXZmbnl2Ni9OcUYvdlh4ZGNYZFVTc2VFZ3FQbnNqQ3dJdHEvYUFKWWQwZWNraUx2amVkMzhBbTBTYkE2S0lsZld5R0dad2pzaXptNGdRS29Vb1N5Nnp1MDZ6cTZpWktVRWJtbEZZUHg3Z284ang0WFZ5UENNVDhXMWcydzltejd2ZFNUelBIa1p5VVZsaXR6TE1OQjNNODZubVhDNjJzblY2RlcrOVF6S1NjNW01OXp1Q3hmUmt1cmkxZytkUzlIQ3FIUHBNRmtLZVVtaGNTYWR4dU1JS0tadlYvY3RjOHZ2SzROalYxWUo5OGRUeXBJam9YTHhPOXR1OWF6aWhDMlVWSzhIVEVxMWlRaHlsNUdreDIrNGNkaS9YMDBNNWovbWFaT0daM1RHWWFwYWJsUSthSHdxWWkveHBNekUrZlh1K1gzZ1I4WnNuV0FiZldsRUxiVGxxYnJCMGg5eHVYK0xlbTNmNStYZC93cTNGVGRLVEViSlY5d3d1b0VuTlZYaUJpaGVoa2pqSlREQ21Md1RkV3ZqYzQzQ2psMDVhSGU0bDVEQnpjSENFODQ1QnJMN1hqaTRGZGh1S1QvTFVzeTRhSExVZlZQbUFjZ0VzcGFaazZxb2pZS0MxWGRLNDI0ZXJKdUt1TXpPVFdjdFRweWtCM1F2SHRGUHRyYTJ1Z3VWNU9hNWNqdkh5Z24zaElsZDBzcGhzdThGWUo0UHBRM2JyeG43UzRsNkh1RERHOWp1VW1DWko5WU12R1hGVmxUak5UTVNkSCtFdmJIazZucjJWZFdhaXc3U3E3WHhXZDZWWi9Mc2JlUGtwei9mYlJaK3M5bFduamlZME9HbG8zWUltTDdqWjNLQTdiL2pKWC8rSWRnMDViK2xrSVloWWxvUXJKT2hoaXcvUFA5R1FCSXQxT01XcW00RHp4a21MT1JXT204TWxXT3BTT0Z2cnlic25IUDd4Q2pwWTl4OWhiS0FkeWxJQmhEa0lBbGV2YkJQZ09sK05CSnQ5aXg5VTJSQm1KZTJFY21BMkdPeVNUZTlQVks0NkcwdnhkdFQyNVJRcWUvcnFGZmRDRUxQVTh0cHRaejdqN3pJUnFqVlhCL2YwNzk0dVp2NUJGWXlkYlVYRnRCUXp4bXl2NzJmSk9OMzVVMjRDWVNwNHN6TXRSYlRrL1NtK1VNUmN1YzVPNVBLcXlueGcyQjV0ZjNWNysxVXRwOGx4WGkvNUMyVjVmREtVMHhjRnFzYTNIQyt1czVJRER2U1FILzdWOTNqODlnT3V1NVc0NUhIWjdvdjNIWnBoMEdTYU1OT2RlM29MbEtRK1Nwek1pWWRjU25iaVNTSWtRTEtqeVVMcVBkdUhJOFBEZ1J5RTRCcXl4SUljN3FDRTNRQ2JyekNYemFJNUJLd1NwbGRhelZjcHBOcFNvMXBWRUEyMkxTMDFwaS9BRi91dGVIRmE1K204bTNtcjY2Tk1KdFhrayswZGFoMUFKWlZFSEZuckpPSW5jM2YvMjliU0JWTjJucDFnQTcwU3FNMGMzcGxmdG9ybEtkQmN6bFRxcWwvNW83UEpSbkk1N2dKbGxYM1A5UnBycHErZHQ5dnpPWGY3enROKzZzbzltWW96UU1icU1WWmMrcUwveWU2NFB0ZDJvWU5mSWVFckJUZll0M2JNbU85Q1I5Y2UwQzJQT09xT09Sb09lZmVuNy9MenYvc3BSM0VCNXlQTHJtUFRqNGhya2NhUkZKSW1tamFnTVQxWGRqUk04US94bGoxY0JFMHJYVVpFa0pob3hDRVJVbkp5NkJlNnZoZnhqZVA0MWV1Y1JDV21hRjZISy9uQWxmQTZheGRYRFpuZC9Jdmp6MWFOakJhVnFLa21GVlpsWStyVW1xMmo3Z0VDczdPZWdJTWRNMlUvdHZNOG5Pemk1NVZTTmpPTnA3SkhXcmF1czlkbEczc2dUbDN0Wm1aWEtWb3dzZi9ySXhjZXBack10b3ZhWFN6SVhqT1laZnFOcTJHRWk3MTltaFJtMTJMdksxZlB6cE5HUHlYY3BwOTBEWGw2eTFjTXpGMkpxZjFqdVh5QWxVeFFKL3RpalVseFJzb0ViZjJ3UVdqbzNJb2pPY0tmTzlxaDRlLy93OThUdGdHZkcxa3NWL1NiZ2JidFNvelQ2SHErQ1NhNCt3TG5FeXlKejBSZHpBd1NTMWdURTY4a0s1MnpURm9qNFRvWU8vSlo1UHpkRGI1ZGNYejl0aFZ6a3kwOTV3eHhwQTJkMFk4aWlMY1U5cVNLNWdoWWJXZm5nM0gxcExncHhTRXZseFZSODYzOC9GUW12K0lDcmxPaGJabDM3dm8rd0R4VU1MODdNeEJpL3U0Vkd2N1YzRVRFRWthbmJWVW5lakxzWm8rbHpYeVlhVHNYMnI2NXRlY0pYVG9mRCt3dW5NNDYzK1ZKWVcvM0Z5YTFQRC9QR1h2RFh6eUxDNGprOUt1bnpsSDVxcU8vOHRpZzRqa0YwM1ZseFo1aWhnNWZRTmdKS01Pc2dqVFI0ZnlFeE5yM0JLRmtoVHRoMUVUMnZwalhBYkxIdTQ2RDVTMnVkOWRwMXkwdnUxdjhiLy9mL3gzM1VXTGxqeVFsaUJKSUxsRlZQRVV6SVRTTU9ac3dVNG5CVm9MQ1ZTM1VrM0N6YVVHbjFUWGpuVWRMaWdZSVRmRGtoTFFiZEpzOXc0ZUoxZ1dXcTBOaUdtbTdCYUVWenM4M2VOY1Eyb0NxbUpnTUlON1NDM0kyaVRLamVaZVkyZXptVkVCaWo0MHgzWTJkMmVVdXpjZ3ZNcTkrc2loUEhSajdBK1RpZnA5L0hCOUg3ZXVaN1lLWk90dkRDMjlpdmhwY0hCQlhuKzluMlhhK0xEUGdTeVl6ZXZhNTdrK2VlUXJQbEkreUdDa0IrMXJWbmhJbmpNbjArcmZEd01IcWdQWDV5TTNENnh4Mnh4eklJVWYrQmpkWDEvanIvOTkvNHNsdkg3SFVsYUFlelJEVkZvbU1xWldKZ0NhcmJqU25sRDJyN1VvbmlkK2hXbXErV1ZhYlRiSm04c1JUOHphcnhDeU5CRDI3MStNNm9YV08xV3JCV1I2SkF1M3FnTDRmclRCUkJWZlVNb05IY1NCR1B2VVVmMHROOG5tSHB0V0E5ZjR0MGVuWi8wdWJ6anZsUC8xV3dSV1lEK1pzV3BBVXFZdkpKYWcrYVpHSUxLMENZL1pMc3pKU05hUWQ1REVoU1Rob0Q0bG5BN2RXTnpoc0R3bGo0T2JoVFZiamlyLzlQLzZXWC8zalc3VGJ4a29rajlscXVTWEZlNGZHQ0dKa2c2ekpzQXVqOEQvM0hNTTBHalVYdnAzZ3hCSWNSRHhaQlhFbC93cklHdEVFRFlFRFhaSTNTcnpiczgyTzFaZVh4RGJUeDBUVE5TUXZTS296VkM3YUZHV08wUkxyMG95V1dhczY0N3NPbE5rRE5XVHU2ZnhUNHh3OHYrMmJpL3R6NUpXbXlEUEtVdjFUYXRXM2c1MHBtaFVUUk1yN05ETXQ0RTcxMjN5R0dnS3BnSkVXeXdpRWNUUjFNU2N0R29XYlI3ZG9aY1cxNWhySHk1c2N4U04rOU5jLzVPZmYremxMWFpqMi82QkZWcUNpenlVL0RBd2JVSGh1aUdyVzl2MjJVZ3h3NTlvNEcyd2xzS3RZS1I3VTBXVEhJamZTS256MDRKRkdWWHFmV2J4NmpDd1duQTRuckpvRk1hK05FYTdKZkh2bkVSckRGWE9laW8xYmdIbkdycGpGcXlwTnlab3JlVUJYT083L2hOdno0TzlMYVNPLzl3UHM0dkU5N1Q2NVM1OU5lVjR3V1lnMTlETDlxcHgvWGYzU05QRFVCbXcyOVdndndmUlgxTE5hWG1QaHI3Rmd3UTEzZzRONHlELzg1WTk0ODd1L3BPc1hMT2hnVkNGbFhCRFVLYmxVZGpYbU9GQkNJenMwL2ZsdE5zaXFlWmJKRHB3YWZPK0xBRTRzVXRjaVZrRGRLYmhlNlFoY2wyTlpudzE2OXQ3SU1Db0hyeDhTRmcyYitBVGNhREMwMXZwVG9CcFJQSmY2bGJwZERHZ0dQOWZuYzkzeGlwYXBYTDVKLzA5cW55UmQ1SjlLbXhNRmFuaWlEcHE5QVRYN1RRVS9wbEJQUmF5bG9vbVdHMWJaUzQxdkNNMkNHRDJMY01UTnhVc3M0cExEZE16My8rTjMrZG5mdk1rcXI1RGVvWW9FRGZnUTJQUWJ5eWtybXBRVUZlbTZ2elNWYVg3K1pCZU1aWjNOV1pROERUVmp0TXNzOW1YUXZCTVR0WkdVMGFTSUJnNjZsakNPb2lkclBkdHVHV1BQOG8wbDBmZElwMFFSTkc4QVNHbUVZcEtLek5aOTUyZEJWeE9JTVFwVjNqbTA1YVJjWVlJWWpQMThXc3MvaFhaNU1NbUZ4d0ljWFpxWlhzeGsrZUlINjFNNlkxRTZzM3Z2THNVVmtZS0FUcWZwZGloajZiZFNZcXBPcG9pTk1WcGNRL0FkVFRqZytzRjFtclJpcWRjNGR0ZjQ3bi80RG5kK2VvY0RQVUkzeXFwWmlwWDZOWmtJSDRVOERvUjJnZVpNa0lDbHlVYVRyQ2lXeFFzREh3b1RzM29PNGxSVldlY3daVmpOTm1GSUtRZ29wcW0zM1E2RXJ1VVlKODNZY2Y3QnVUN3BIM0g5RDYrUkZrdk81TEhsZnRIamZiMDhpcWJKUFMxeE5mTUJkNkR6dkdqdURtbWFtUHIvRDNIK2Z4ZnQ0MlFwLzY2YTZVSEs1RytWdFloNVhORnBqUTI2aVpBQXMzaVpzN1FlaitXQ0JaVVM4L1dJQk5ydUdxdm1pRU4zbmV2dERSYm5DLzdtMy84TnYvN0JyK2hpUjZjTGNTTGt3Y0MrTkk2b2N5WmU2eXhzUlFLYW5VaVI5NldNVlJrenp3MUdUNjZteXdhVWlDVStXZ1VUbytobVZjalpDRXBsMXJIUjdCaHd1RzVoZVdKWldXUUJiZG1pblA3aWhQYU5GWXVieDBqWHNFNm5qRHFRODJDNU9KZ21vS2h4RjFRenBodFN4bERGWTh1ZkZ2VW9xN3BSZ3N3NVBkT2ZtUWZBbitmM3ZLZ094Wnd1TnRjRitYdzdzbnZLL3ZjRC9KOW0vNTlXaCtPU210WlQwa3ZtclZLOW5yclA0RW1sL0cvWGVHTE1qT05JMDVqQ3NUaGxHSHNXN1JLZlBFTlMyc1dLMEM1cG14V3I1cEFEUGVDMnUwWDhjT1EvL2EvL0p4Lzk2aDdIZW9qTFhwdzA1S3o0VWpyS2VZK0kydUFxa3VmaUtmMFZRZ2hFMVlySFhIbDlMbDZIUGVCREN6TmdGcDJ3NlBiTzV5c0ZDOWo5WmR1b1E4MStkWjZRUmR3bTZ6cEJyeHZDdHNYZlhMQmNPTHAySkxjak1mV2tPQkNIdFRGTktOVWM4VWd3bmZTVW9uV2tpbHBMS1ZxQWxqUVlVL0I5Mm8yZG4vam53YXY3b2hXWnZ1ajlQNjlkNUxEV05uK3RNNXJiRkg3WDZnSllYWUFzdG9Kc05ZRVQvS0ptdmx2b3AyMFh4RDdUdUlaYlJ6ZHgwa0ZZY05qZTREaGNvOWtFVHQ0KzRlLysxMi9UZjdobU9UYlNlSSttTW9DMFpOQnJJa3g5emR3azgvMTM0cjBYd25VdjFNSUVqV0l4QUNYdEtEK1UrQldBNXBJd1dOUUlpLzltUkZSRnZDbEYyY0JJZEJMRVpmUjB1NlUvV1hQNGxlc2NmK21ZSitNcEcwN3hDMGNTUlp1RlBhcnBQdWFja1JqeFl0VlNISllTWTFWQlNoaEEwa1RTRmR3ZU8rTktFWi9Qc1ROKzBSMzlpOTcvVlcwK28rOEcxSHl3emZ6TW5DWnU1RFN4UzgwZXFDV2VNQkVnTVZIV3FNSjJIQzJwVkNQSHEyc2NOQjJNRFV1NXhrRjNIWFRCb1Y1bmRkN3g1bmQveWcvK3c5K3k3QnVPdzRFNE1SZW5hVmNtUCtDTXRhRlNVcnpxZ05JcUZiaER1bmZ0eGRIZFhkSm1aYlZUK1hPRmRlNE0vUENGVEpwTHZDcVhHSmJ6TWxXL2xPelFhQWZaK2dhdlRocmYwWStqbnIyOVpuMy9uTVZySzFZdnYwU3ZXemJPSXk0UTFTaGNUZGVhaUU0MDhjM2dUSTFxdjI0VTZGUWt2c29tekQrN1BLZytxNEgyU1hRbVBzdjJSZS8vUmR2ZVNuVWh0YVMybWt3cUpVQzJ5d3F3ZjIyUWVUeENMSTVDakZiaDFQbUFkMG9iR2c0WFMzeDJMSExIOGJXYitISEJvZHprcUwzSis3Kzh5MS8raDMvUDl2NmF3L0dBUTc4VU41b3MvYUx4SkNEbHNheTRWcXFLVE5Ib3Q3cDVjK1d3SFhuOWFpN2wwOXFrK1RXNVArSkpWWFJGTW9yRE8wR1RHTEpYSXQ0VGh4Q0ZuSXhmSmc0blhhbkROWktpNHJLd2tFYVcwckYrdU5YTmVzUDJzY2UvMXJDNmZremJITEJsemFpbFhyR01KR2RtQkk3Qy81WENxL09JV3VxR2t4cFBLVlBkN0lZK1RWUmxkM00vZVh0YVIvOWRkZkF2ZXY4djNpN1A5RmROZnM3dDdsMFdoK1NTb2xONWlHTGdsM05XcFNkSXg2STVRQjBzUWtlYk9nNmFGWWZ1Z0pVY2NtMTVuZlA3Ry83K2I3L05tei80T2RmOElRZDl4NnBaeXJEZTBJV0dZWXhJTU9KNTZmUkcvQzNFRE1FWC9tU2VFSUU5VW9TKzJPQ3FMZFRMc2MrSVYxSVJuVEhUMUVwNlNzMDN3MDNCd1ZyTk1lUmdMQTVzRUdyaFNHbFNHdGVReHN3aWROS0dqb2QzbitqcDQxTVdYMXJTM09vNFBEaEVXc2ZBbGo2dlNUNFFHUmhUandSemNDczI2MVFuVm9pWkk1L0FTUDcvdDgrdFBXdTF2VGdKcXVhcEltcDIzbmpmNHEwL09vUGRMRXZFbExVT1E0djNIUzRIbXJiamVISE0wcTFZNUlZd0JPSnA1RWMvL2lHLytQNHYwSFBsaGpzbW5ZM1NoUlhyMHkyTGRqSFYrOWFzeEpUbzJoWlZVeDBURWJ5RTZmakhsRXhoN0VKNjBpNkU4R0lSMmxEemlhWnFqZ0tLTUVvZDR5VUxBemRwS1RyQkVwSkwrTUw3WU41VFViL05DSm9Ed1J1TFkwd0pEV1o3NTBFNTlvZHlzRjNvK1cvTzZSLzJkRGM3dXVzZHphcGgwUjJSVzlNT1BoZEJ2RmlGbEp4M1BMR3NWaFZsbHFSNHNUME42Zms4MnU5bUZka0x5YzZlNTArNS80L0RITGxxOEZ4Z2EweG82OU10aHJuT2hzbWpDYmdBem1vdUJIRTBCQnlPeGxzbWhvaG4wUnpncGFPaFllVlhyR1RGQVVmY2YvY2VkOTU4azkvKzdEZXM3NS9SMGVGaUlPY29YYmRrR0NMZDBRSGpNRUxNdEtFaDUwUWJtcEpuV0VzNjJaOHBFeGpnWVZhVlRlcVdCR3NaSkZJQXQ1ckUrNndXYXZ6cElvdzZaUkRYU3luWnBMUG1tYkJsOWNzRmZsY1U3eHU4R091K2pna0ZmQ25zd0pnaEdXbFM1SUJCUjkyY2JkbmNPY2NkQ3N2YlM1WTNsN1NyQloxMHhKd1ppVVJKUlVvWjhIa3FQVFQ1WjZxWC9pQS9jN0JWaHNIRnh4ZHQrNUM5eWQ5WlV1UWNuLzJNMnQ1TU1nc1oySWYyNzhmeFBXZmJlL0h6djJvMm01L25EdGlvdm5MVklaeStMaks5NXlqSDYweFZDMmZGQjcxWW9ZZWdKaVo2MEI3aUNYaHRXTGtGeSthSWZEYnk0YnYzK1p1Ly96c2V2ZjhBdG9rbERZZHBKVjQ5bGt4ck9UUE9PZnEreDR2VjZMWktMSlo5dnUxNzJyYlVFeEFiWURsbm5QZEZKOVBDVnJPdzNNZHVvU29jMVZXcWFoRTEyZHZKbDNpV3F0aXlXU0RQcWJvSGdDYXpuVVZSQmlqTGFkWGtjSGlxM2t3TkwydDJkT0lJbTFZYVdxUVIwcGgwODJqTlBYbEFjNlBqNE5hS281Y082SnZJMkVTaUh4bGtZR1FrYWFTV3VEVktTSjVJbkxzN2FrZFFqM2ZlYkVDQUpuQkZJaTY0WFdCME1tbnlmZ2U1K0RoSHpPenRLVkwrc1c3RXhZNDRCZDd6NVpWaS8wVGMxZS96dkVIbnB1OFlFVUdteC9tK0ptR2pDNTJyWGxNdEJSY24zYzFjZjJPdkd4ZW1XdG9pcGVoR01jbWNLN0o0cy9jOHdmNlRscUNCaFN4b3RXTWhDL0pHdWYvT1BYNzBzMy9rM20vdU1wd09kTG5sSUMzTWhFdGwvY2x1dW9ZYWN5bjNBUlR6MUJVQUltTTF1WGZtYXhXZHhXSm02QzVNZXlrSVh2ekk2WXJNZUxjWEx2bUVMazU5cWFJbVpZTTdSR1VucURLand1L2FKZmFGN20xcmw4azg5ZUlKTEFrMHBENURQOGpLSDdKc0QzUjhsT2xQQjU2OGMwcnVNdTZhWjNIY3NUcGU0UmNlRFZZSE9jWlMwVkZOZjlCVXJmTEV5bzQ2N2ptdUJ0VWtWQ3lDNzF1ck1JUEszbU9kaVYwb21lUHFqSG1RcFVnanVMMjAvQjJqYmo4UDZ0TGdxVmtHbGVCYXNzZm5ROVAyWjNDeTgzSXA2YnNxaWRtZzhIdWYxOEh5dE5kV0ExeDIrM25LU2kvelkyWi9rRjNVTGF5L2w2eFE2blI1akRGQkxxR1dvcVV4N3lxQ2xZZXR2bEFvSzVqRDBkRFNTTXVET3gveDltOSt4WHR2dmMvNS9STjBLN2lvdUJFT2RFbVRnOGhWMGV6Q0x0NHRCSmUvY25XN1NGWisrcmRlZEpQUHpKNitHTEYvMXV0UDBzei95NU8yaFRHbWhUeG1ZUndoQnowSUFiYUtuaWJTM1pFMVc5UWxtcTdGTFlYRnpRNjNESFNyanRBMjRHQk1JNE9PbGx3VGhDUVZyZFNkZG9YWS9uTXVwVityM1kxSG5PNG1oYUs5SVdxQjhCb1F0OWU3YzVtYmlSYkkzQjlFOVpyTkI1bGR3OUtKWnl2Si9MSHV2ektsNTRNRU1aMStDeW5wM21NZFJBNDNEYklxNnNwc3haN0hwNlpqbkxXNXBCMFh2NmZtaDlzMUUzd3dzTUpFOTB6K3ZBbkJUTGFrSlRuWUZmNnJJemlyMERtc1J6Wm5HMDRlblhEbnd3ZDg5UDVIUEw3L2lPRjh3T2VBRHNhVGJXaHBwUkdQNE5UOHFIRm1OUHorSWF6V25pdFI4S3pCOUx5QmR0WG45VDN6SjR4eW1URlREWndWWk1zZzB0QzVSbkpVOGhETE53TXEyZFpYSjhTUU9mbmdDU2tZQ0pKSmxsemFDSzRKU09zSXJZZEdJRGg4RTZ3SVJCUHd3WU9IdGwwZ1RxZEM2SFdRVlRNeGhNdVhhTDR5cUY1dFJzN1AxMzUwdFZsWHRTWHJaMDgzU1YxQjMyVHZ1MmxNMC9Pci9uTGVXUlRUNzJlVHRiK0FHbDAxeU9yK0w5R2sxT0ZwSmo4bWpaRnhUQXpibm0yL1JZZkkyY2s1T1NYU0VPazNXelpuRzg1UHo5aHNldUl3RXZ0c3g1WEY2akluQjFFSjZsam9rWkFoaUp0S1hjVVl5V00wNFNIdmNMNmg1a2ovUHZJellWYVl2YmJuRG96UDhDU2tJRFZPQk0xcEtuaGdLeHFrc1pRMXdwelN4bGx0bG9RVk8zZGpKcmlPV2g4clM5YXNSY2luckZqbWM1anRuTWdNS0dCMExad1Njei81RlpNZHJ3blZmWjIrK2VPMHNwUTIxN0svMkJrdnQ5MktvSnFJdW0rZVhCWElWZDBOK292Yjl0N3ZmVzllTnZoRmZMTDlZMzkyalBIaVp3NGhwelI5TnA4a3FweUZsMUMwWHBScTFsazhTbWlscFhXaGxpM0FLL2dzSWtrSTZxMWNiN0lCSExNQlhjNDVYR2luL2FUWjlWUFZwNXAzWDJRempZOVBZUXArbW9FbkpjQ2NDcm5ZN1BOZHRVanZxZ05iNG04a1VreWdFRnlnOVo1eDNPc0FadTZMVlZISmtnak9UK2dqWUQ3YmZQQzRhT3lWMmFDNml0aGEzOHV5MzlubWFPdFZnK3p5OC9ubkZYN1AxTUU5Zjd4NGZhL3E3THRxSXZ1RDZWbS9zZGU3N3ljMTZseDluSHVXVmYveHFvblg0cXNaSmFrbUpKT00vMVpyRFlpYm1kdlZVakJySmFWRUtscjdOckdLQVJaWkxVU1RNekZhTlV2dmpjL3FzVUdsRkVMRWhXYXI2Ky9mS0hzUlJTdmcyYXZaeHgxbzAvZXIzNU9NN2V4TE9kYVVUZC9lVXNBVFdTT3BpcVo2UjFZbGFhUWZZakhuYWtxNGRRcXIvbWszT3ZaRDJhY05WRGZ0Mzc2dkdrcW4zaDhFVTBBeUdpeGFKZW1xY1ZjN25qem52UE9zTDF6dStQVzZHU0ZhSjJrNVQ1WExUaWs5RmFkVTFUME5ydnJlVlFQcm1jL0ZhRXdpTm1rNFp4MCtsOWR6WEdIdlBrc3VnOVdKVjhFNWcxakwzdEhpZnlYTmFOcGRDeEdIdzZoUktZMW1xU1FsYTQ5bUxZUEt2TUUrYnN5NnphNlVBeTdnU2FvcitHWDA5ZmZOWkh3dThQRzVFMUNUMGppVG1oNkgwcUdkZ1FmRE1PQ2NOM2czRkhNb3hVSk1Mdnl6VkFaVnZlQnE1cWZGUXdYMklGcWQrVkpsUllsYXpORWRTZ2FZUWhkS1cxRElTeDNUVUlicHZUcmo3ejhLcm5SU2kwTlpaMDR3Y2ZQbTI5UVowYmtxODdyWkxkb3pIN1VXSTVRTG44RThmSEI1QmJ2Y0Fldnhwb0lRWmdXbXZENlo5RGN5YXBLQlZIVmlaOEJMWFZYaVRIWmRqUlVrVHZEcXFCVXBpem9UcWp2MmtQY0NEbExLSkpORkptUGtYOWNFakJsU3pqbW1jaC8zeFdqMzc4dnZWM3ZxSUh2VzRMb0s5bjJheVhuVnpMTDN2Y3BUUkM0aGNkNkhNdHZuS1NJZ3hWOVRWVkl4RlhjSzk0S1JsM1cyVXUzMEdSTTdjOGdDakNZWlpuU3dZaTdXRGxxQ2xUck41UHN4TVFvNzI1VU9NNFZmbGNtUHJCTGFGc0xRaWI1bUZhclVXT1ZhWXkwWHk5aGV0ZkxVWTVCeS9MdHZYYXdYTmcyNlM5dVpnVExsTzFZd3B3U0g2emZrcWdGYW1FRlNMUUlUd2MzaWNLWFVscC8yVXhTcDgvNmdxTXBTdTIxN2lIa3lLWDFWTFZhS0sxRW5RZlB6cENLWE9MS21raEhDeE55NDZyaWYxbDRFYTNqZVovTnIvYlQyKzZQR1Vrb2pJVXErVVBCZ3IwM3h0aG9jbFBJSHV3SVY3Q0RxdVh5MjdqL1czYzQ3M3ZOYnBkL3NxR2hYdHVkcVA4dytud0pIditOSFRLelRNbzR2UHRybjgwZFhmdXNNRHR5ZHlSVjk3S2ttN2hVZmFLRTAyV00xL3gyNUZnN1JtamM0cjBNNmsxdC9nZmE1VzJUUGFDL3NrMzFlYlU2KzNPTnJxSnNTTlBjL04vcUFUaVdFb0phNHBaZ3hleDE4UHN2TW5udmR5WHpOMjZWYm9ic2J1Mjh5enM5aFIwdVNJZ2EwQnlDSVdhN1QrK1YxK2ZWVTg2elcxMFlMNytZVHZqOS8vYlR2Mi9YTXU1V1BDOWUvSE52KzQ3NS9CckFMQkZkeWVkMzI3dHFZOVZrcGJwVnBNaHR3VTkyMjNjSHNpa0dhcnI5ZDQxSTR2YWhINXhLUGZOWmdleGFSOTNmbHUzM2hnK3hwclJiQjJOMnVlakgzL1NGUmM3UW5ud2ZMUFhKTTV2K1VIYkNQUERrejJkaWxPMXgraEVvZnE4YlhycG01eVBSKzdUSHoyNm5zaGx2OS90TnUrOVdyeDlNZm45ZGViRHUya0Z1UDMrY3c1aXNlTDNaS3QvZnN5bzVjL3VZVGk3cmRyYWlHaEtsTDdVYXdIWWRpUWZ2WlJDelowT0M1V2ZoN0xvKzNWMm56aTBCbEtqNG1xbnVEb1BveDFaZXBMNWw5bzZKN2xXOXBQazlCeXNwciswMWxWOWRLblhXRENWTzd1a2lIcW8rN1FWNnM3NzBqcjY5ZG9XR1YrWmlLRVpxUGs0MERoKzVlbDBsaWQyWnV0azFlOEhrOXZ2cmJDeXZLekZ1N2VqdjJlbGV6YkNjT1V3ZkZWYTh2dHFrS3pyUXk3U2M2MXYzVUNqQXczZGJacW1pRHhzMTVvbEQ4M2RsMzZzUWc1VnllMFY5L240YmRGNzZTdlloZFBaLy9MN1pLeWFwOXpBcEI3RzV6bnYycjB5cFlmL3M4QmxwZGdUNmo5cG5VRVhKWFBMODQ0Qnd2VWdGeVFnTDNhTUhQYnJ0VmZiOTkzRk9iNTJUVjEwcmU4Ky9tWmFBdXI1TTdUdVlMNy9NTFdraSs4RUcycjV1b0YrYmRuVThXU3hyQy9HN2FER3NqVEV1bnFtamYxS1pnY2JKdHpUdERjUXkwbEY2cWo4eGU3N2wwekg2dVFxMUNZaHFSbDR2NTJhT0pOdFJLbHp2ZmJiZEt6TGYvMU9jemVZam5mZDllbCsvUHJ1K2w3bFhlbUZZTjV1YmlzMTgvZmFQbHZUM0NlREg3WmkvcllsUUhqNkdPczc0Z1puTWtLZlhFNXhQSW5HeCtZZjkxdXZsOWFsLzRJSFBsUXVlTFZUeGdWMTFtZWkwN2taOXFzN3NLdlROOU5obFVtckdhekx1VmJIL0N0V0s5KzFKRUZ4OHZtR2M2LyszTzM2b2RyNnJpNWdMVDUySkNWWDhtbDhCNW5yYjF4WXF6emxlT2kranI4eDdoNHgvOXZoRnVUV2FEemQ1NG10OTVsWG45b2o3cUY5ZjJCdGtuWm01dzJWNi9SRTJTWFd4RUJOQ1pqMVJlZys1QjdsSUdSZjJkSzlQZnp1Y0JzaHBDcWZYbVY2U3FJbzJ6WVhYeDNPcDM5dnlYaThPd05qTXRkd0RZN3BONXA1c1F2QXZYWUVkam1wbXVGdzdwMmMvM2YvZXM1L3V2OStONzZjTDM5dGdjN0xmbnZTNWJ1UENsL1JETDdBU21iMThjWks1TVJCZGpYRmxtZDJOdithd282ZVVqZXA0RDhMUSsvbGtUNGVmdEMxL0pnS3R2REJQb3RmK2FlUXpNL0txNWd6elo2Tk9BbS9zcVYzV2IzemZqNHY5OTdTSWVPOTJ2dlZ2elQvYysvZCtLbU1JRzZDY0MwZ0FBQUFCSlJVNUVya0pnZ2c9PSIgYWx0PSJXaGF0c0FwcCIgY2xhc3M9Im9wdC1pY29uLWltZyI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJvcHQtdGV4dCI+PGRpdiBjbGFzcz0ib3B0LXRpdGxlIj5XaGF0c0FwcDwvZGl2PjxkaXYgY2xhc3M9Im9wdC1oYW5kbGUiPiszNzIgNTg3IDM1NDU2PC9kaXY+PC9kaXY+CiAgICA8c3BhbiBjbGFzcz0ib3B0LWFycm93Ij7ihpI8L3NwYW4+CiAgPC9hPgogIDxhIGhyZWY9Imh0dHBzOi8vd3d3LmZhY2Vib29rLmNvbS9zaGFyZS8xRUxQNktDNnJWLz9taWJleHRpZD13d1hJZnIiIHRhcmdldD0iX2JsYW5rIiBjbGFzcz0ib3B0Ij4KICAgIDxkaXYgY2xhc3M9Im9wdC1pY29uIj48aW1nIHNyYz0iZGF0YTppbWFnZS9wbmc7YmFzZTY0LGlWQk9SdzBLR2dvQUFBQU5TVWhFVWdBQUFQZ0FBQUMvQ0FZQUFBRFRqQkhmQUFDRlkwbEVRVlI0bk8zOXlkTWtXNWJZaC8zT3ZlNGVFZCtRbVMvZlZGWGQxZFZkMWQyb0pvUUdHcE5hQUVpUXBxWUl5QWlqQnBQUlpDYXV0TkpDQysyMDBEK2pqZjRBbVJiUVFwUlJacFFKb2dCS29FR1FFUVRSWXczdnZaeS9JY0xkN3psYW5IdDlpT0didjh4OFZYV2VmUzh5d3QydjMrbmNNNThqLytCLzhqOEVRRVJtbi90ZzN6VVRPM2ovZGUzdHU4L01idlhjVGp1bXMrOW0zazZJRlFESklJUUFvY0pTUnhRUUREVkFEQU1rSkZiTGh0UFRFNTQvKzRUajR4TmlXTkYxUFloUTFaRmtMYW85Wm9LWllXYUVFSWhTNWY3N09KSjJ1U01oOXloL1N2UnZJc09ZL2I3NVBNUVloL2tRa1ozNVZwMlBkNXdJbmJXelBaOURlMmI3NTlyQ3dXdkRYa0VKa256TzdkQjZoZGszeWVNZXhxZmI2MlhETmY4TVExL0xYd0VWTUJWTW1NM2g5SG5aMDY5cE81RjhYZnk5eXJ3dHlYTXdqTG5zTDFGRWhKamZyYXFvS21hQ3hFQUlBUW1CaEhCMmRzYXJWNjk0OCtZTi9XYnQvY0w3SHFoOHpBS1F2TjkwdVo5Z0tlOHJvcjhqZDlmdzlRbDVQNGp1eDhOcU9tRzNSZTdyNERiUHpEYjV2U0FBT2tHUWlwUVNhb2tnRVJGRHRjOExaeVJOZVdHTXhXTEIwMCtlOE96WkUwNk9WeXdXRGRvbklOQjFIV1lRUS9EMk5FRndCS3VxaWlwRVZQT21rb2haSXFWRVZkVW92bEhOREt4c1dQOU1LUUZDUUNBSWdRaGhQRFMweklzR0xHK0E2U1lNSVd4UEFEQkJ3cEFQak8xNU52K0xCTXpDem5QK2J4elJTWDVBaWZyMy9Ja0lDb2lKYit3SmtSalhjK3RnWVk3Z2FuRm9WMEtlSnludnkvMFJ3Y3E2cXVRWkNKZ2xMRVRVRW1JeEg2cTVud1FNZ3p6K0tRTFlwRjhwOTlYVWNydmpQRWd3WkRwZHBnUUo0MzFtckx1ZUVBSUJJWVJxR0Z2Zjk4UDZuUjRkOC9Ua2xQYkxsamR2WHZINjlXdk96ODVvK3g2elBxL2hlSEFFQ2FncUtTbFJmTjhrVGQ2ZStKb1BoNDZsdmV0Zm9Mb09DZTlNU2UvNFhJRWJVL0t0RTlvQUpBejdxdGRFck1xbVVoaE85eDRRQ01acXRlVDA5SlRuejU5emZIeU1XUUpMcEtTWUdwVVlFVUdEdDlHbkJDaE4xVkFGWDR4MjNkR3JFU1JTVlFGQ1JFS2c2eFVMZ1NCVjNxeUJoQ002QmlIRVRBRU1WUWdaV2JBNGNBWER1S2p6NUVJb2lMdm5ZSFFFODMrcjdsTGg2ZHgyU1lrWjZmYk50MU5jUnhqTVB5VWZXSmlTOHFHRGhQbnpoZExvb2ZVTGsvdlVuemNkM2hmeTljS2hxSjgyK1lIeE00cWdKRDhpZzgwT0lpYVVmZnNjSEtnOEFrSHpBYUREVUJ5QnpBOTQwUUhSb3loQmhISW9TMmd3UUUzOXV4aVZCR0ljMTBkVFQ5Y3JWWXg4L3VrWFBIdnlDVy9mdnVYZHUzZThmdk9Tdm05UlZVSUltSUNhRVVTbzZ3WGRwblVLbnBHNk4rY1VSSHh2REFkM21IT0N3WHdNMVlIWnZ4ZmNCN20zV2ZVN3RVR2dyRWhCRUdlbmJXQjV6WXhRQlo0OCtXUkE3SEpmWGRkRWFadzZMeUxXRzMzbjdGR01rYnF1U1NtdzJhUkNJS2pxSXhaVkF3VFdiVS9LckZVU1FVTEFpQm54aEdTS0tXWW1WQ0g2cHM0YlVDVXZtZ2dJOUNZRnZmelRBaWJpZXp4STN1WUJ6TVp0YjF0b3NQMGQ4aVlGUXZCdGZZREZ0ajJzWDFrakZaQVl0a1NNTU13dk1KbnYvWWVNRXJDZ2tBYmVjOVplRlNMYlo0U0pMNjgzYVNEcTFOcnlzdHZJNm05ek9LRlE1SUdUMFpITkZZaVNNZ2Vnb0VwVkNVWVNzUjZoejRSQkVYbzY4TU5GaEVxQ3YxZ1R5WHFrczBGTWN0SE5EK3hlV3lUQXMyZlBlUGJKRTU2K1BlYjFhNmZxWGRjN2V5OFZwa3JYZGNQOGxYa1BNczZ2SS9xY0E5OG1qTlZETWNaVE9DalgzUUxtYk41Vk4rNjVSK1lzNlp4MVRJUVFPRGs1NXVuVHAzenk2V2VzVmlzc0dHM2JFcktNbm5xamJUdGE2d2loSXNaSUFGTGU4Q0UyaExvaUpTUDEwUGVCYUJWSklvbUZxUVNxc0NBWkpETk1CWWtWZGRXd1BGcHhkSFJDVXg5UnhSV2hXbExYdFI4c3NRSnhwQmNSQk4vZ29wQXdSR1ZnMGNlTkhrQ3pIS2MyUXdBeFhFUlF5NTh5US9SZWRjYnlUMlZnL3k0NzE2YlhrMjQvSzdONys3NGYyaG5iOUd1cHRPT2QzTnYrSmpHSUthWDlNaDRyTXJpWkl6aGdXRjV6TXRJZjBsRUFsbWhNQ2FyT2pwTUlsa0I2c0o1QWovVWRFam9UT2dJZElYUzRaTjBMSkdLakxvZGpxSFpJNmdrYUNkR0kyRERQUmlKbDJWdEVVSHhlbmoxN3ltcTE1UGo0eEZuMzgwdjZ6by8wSUlGNlVkRjFIVjN2dW81S0FwRWlzb0F5SDEvQm0zSW9Ya25CM3lkN2ZwVXk1MnFZRG5BODNjb1dLVEt5aUtDcUxCWkxQdm5rS2M4LytZU2pveU1rK0FTcTlvTnMwM1VKSVhLME9xWHZlL3BlU1NwVVRVMlFtcVN3VWFQdmhCaVdacUdpTndPcnFlc1Ruajc3bENmUFBtZDE5SVRWOFNsSWtjMkVwTkQzaWI2RDNzQnNRU0pnSmx4MGltNGdxVG9pU2lEMW1oSFhON2FtRWFFU1JpVVZDU01ZK1RPUVNBUUxXVDUyUkNxZndjVGx1SHkvRVZ4WnRZVmMyNGVySTdyckk4WlB5eHROOWo1ZjlCSFQ5b2I3VkRCeDVscXpqTzg2Q2h0MEZXWDk4bTVBc2Zuemc3NWo5MkFZdnF0aG92alpOeUljWmdSVEt1dUlwaTYraVFLOUk3b2tBb2txR2xWTXhOQVRROHIvVG9Tb1ZvWEVSaTl5R3dteGpWVFdFa2pVMGhNQ3BOUWhZcGdGelBxQnE1RGdNblRidHRSMXcrZWZmOG5KeVJPKytmb2xMMSsrcE9zNnFzcjNKa3c0SWJXczd6aU1IOVBmRHlMNGpXWHoreEhxTzRPL1h4bHBOVUNtVWhPb0t0ZFN4bEJ4Zkx6aTgwOC80L1BQUDZXdWE5YnJOWnYxaG1aWlU4ZUtaRDBwK1dZV0F1dmVFR2tnQ2hZQ2x6MjBDVE9wcUtvamFKWXNUai9oK2FmZjViUFB2MHU5T0diVHczb2piSHJod2lKdjNnaTlRZDlsTmw5Qk5XSWFVSXllU0o4U3FWTm4zVzFreFJQSktaYlpnT0JUdkZPQTFHVUtueFV3VXdRWDF4S1hBOEMvajRodkJnd1V1c2p0OHdXZElwZ3I0d1NJdzMzSjRwVmFiRTI3di9sblhqMEovamNoNWxOT1lHeHp3Z0VBRUp6d3EyYTl4UzRuWjdpU1NyR0JreGs1bTR6UTFoTUc1UFpaRlhNVm5aaGlmdnhTQnlQUVEwYitHSXdZTmh3dEUxVzFwb2s5aTlDYXhrc0NGL1RwWERiOW1oQ0VLRVkwZ3hBSnhYcVJsS1JLRlpjK0lCV09WeWZVWHk0NVdoN3o4dFUzbkorZjAvYzlNUW9oUmlDZ2xqQjhIVWMrN0FDWHdnRUUzNjhOM2IzK29lRHcrM1hRVGcrL0pDWEd5UFBuei9ueXl5OVpMWmFZd1dhelFVUllyUmJPTGlaWHVzVllvVVJVYXdnMWJSOUFLbE1OSkkwMHgwLzU4c3Z2ODlrWDN5ZlVKL1JTMC9lUjF4dGw4MDY1YUdHOVZpNWJwOWJyMWpDTnBLUm9ja3J0YkgwaW1kTHBobFRNSytLYVdEVWpwVVRmOTRRUVNWYk1NQ05yWHVhaEtMRjJWVkFwcThMaXpzWldERXM2aUM4dWx3NVNmcDdIT1B2dUNLWXpSTk1zNEU5L08yVHVuR3JxeTNYTDRzZWc2UjdrNHkyS1B4SEN6U3pmNyswVk0xUFkzYWE1UTNOejRkalBMS3hMaFlqTnhpdm1CMll4UXdVY1NZV0VXVWRBTTRJblhsK2NVY2NOZGQyeHFsdU9taVdMZXNVaXJxeVJOZWlsUkdtcEpDSDBWSmlyTk1XUTRKcnhwbW5RM3RoczFsUlZ4WmZmK1p5VG95VS8rL3Budkh5WlVGTFd5cnZPcHFwcVNMajRFOGQ1MlljWE13VGZwMjBkSHBScEF3OG51ZC8wc0xDOFVDS0NUZDR2bWJVcnlwU2tpYXFxQ0NIUXRpMkx4WUl2di95U1R6LzlsS3FxU0dsa2s2SUVxaEJZcnkrUUtoTHFobDZGVFIrSWNVbXZqZlVXa0hqTVo5LzVQbDkrK1gyT1RyK2cwNW8zYnpxNmk1cUxqZkx1c3VWeTNiRnBZZE1HdWw3b1RGQU50RzJpVjZQdkhHbXR6MVF0aXhGOXIzbWVvYmVFYWp0dWJzRE1XVFNYV2NlRDF3K0RnT2JyVXhiZFJBWjBGZXY4VTBmV3ZGQjJDODdxNVJsMk5oVVlLWUs2TEp0WjhoSGh4N1dUUTNiNDB1cEJMWHArZzR6UER5MVBEZ09maTdtR1hpV3o4bkw0M1ZJd1hyZEZqb2xpS3JnMjNFU3lZaGFFa2NxS3lFUjc3bUpPb0VlQ1UyUlJJOGdwMHZWSWYwbTF2cUFKRnl5Yk5VZUxFMWIxaGpwZTJrcDZtckFtNmxyTU5xaHRpTmE3Q2ROZzAxNkNDakVLVFF5SUtVZXJCZC8vM3EreFdDeDQ4K1lONTVjWHBKVG9VbzlpVktIR2dtV0x4aHlQcG9UNVdpMzZWUWg0WDJYYXczQUNBUkUzQnhWelF0dHVpREh5NU1rVHZ2amlDMDVPVG1pYWhyN3YzWnlSWlcwVFdMY3Q5V0pGbDVRK1JaSTBWTTJLODB0c2VmeWNILzdneDV4ODhpVjEvWlMzbDhxTG55dnJUamxmVjd4NzEzSzJibWs3bysyaDY0WE5SdGwwUnRzcGJZK3ovQ3BaaGhWbkp5ZFVkMm83VlF0c0k1S0ZpWDA1SDIyRmxWWVZpaDIwS0t3VU43OXBvWnplT0pZcGt6SG90QkF0ZEw2OEx3NlVlazdCSTFQRW5xM2hOV2Q5Q05WZUxuQjRmc0plMnZqaklHS0lGZEZqY3VlZ1FjMXlxY3g1RjFlanUyYlN6WkFKbWRxdmg5Y0lXcXdVK1lDY1hjM2N6WURvVXBSa1JoUjNpcW1rOHZkSVF4V1BXTnNsNjhzMVorMGxUZHh3dk9nNVhiUWNOVDFMT1RjTlp3UjlKOUhXUkcxZEdWZjVlN1JyV1c4dW5QQlVGYzJpNHJ0ZmZBbTR1YmZydXV5RG9hUzBJUVRaRVdWZ0ZLdENDRGN3a3gxWXdYdVpzVzdqQURPaDNEdnRFTWtNRkVoQ3pTZS9xWmVjUGpubTg4OC81L1QwbUJBQ2ZlK1VNY2FBQlp3bHQ4QnlkVVRiOWZUVzBQY1ZvWGxpR283NTBlLzlIczgrK3dHOUhYRzJDYno2K1pyekxuSitGbmo5cnFWdEE1Y2I1ZUl5MFhaSzE2VUJ5YkVBb2NZa0RJNGRBOExDWUpSMUdYTENqZVQvT3dITHNwb3FaZFBLZUJQT0xvUFphRWFaelp0dDZVa201cFhoSHBoNGVvVXk0Zm1SdWVmZHRyS3MzTHBMUmVjSGdXSlg2bWtPN2lQSnlzVlE3Tm5zdENOWjl2WXhiSWtZZm56bTUwSkcrRzB4d2Y4bnFIUHJCY0d0akpuc01WYTRGY01rWXBwY01RbDBrdHdtSFN1aVJpcXRXZHNSVmQ5UlNjdkZ1dVZkZGNseGZjbnBxdWJKc21GUkxjekNPY2lsU0hWSm05YUk5c1FBTWNTOEwxei9FbVBObDE5K2w4WHlpSysrK29xMzcxNjdTQkZjYVZ6RmF1SmdkUVdML2xnT0x3L1J6cFZjaElxcjFzemRRcXNxOE5sbm4vTDVGNS9TTkEweFRyU3NFbEVFN2YzK0VDdGVuYTFCRnNUcW1OQThzYysrOHlPKyt4dS94MFhiOE0wYjRYeGp2RDN2ZVBsYXVkd0lGNWZ3N3F4bnN3bHNPcVh2TFV0Vk5jbEFMVHUxaEdyWWdEdDl6ci8xWnBNRFlEYml3WjNFaVpINEpwU3kwN05NSzN0Mi9hRTVHeEIzTGlPVEpYVm42MEptMldOK3Z2U3RmSmZjaGFMcEJpU01TTDdISTI1NzlMdnpNVHFtVEMzK1ZnNGttWFYvMXFhVlEyL3JNUEpQR3hTSVpjemJic0JtTmxCbGZ5cGZEK1lySUlHWmh4NlpxNUhTQjgwaWhuTlRwa0tTaU5DUUJKTDBVQm1wTzZQcjNyTHBhN3BVY2JTb1dOVkxsczNhdXZSU1NGQkpSNmdFc1pZK20ydGppTFNwcDZvYW5qNzlCQkVoVnNLYk42L28rNTZtYVdaTVNSRTlRM0N4VlZYdjd1aHlZenYxMWpPM2hWMWx6ZmpkekxDUVBjQkVhWnFLMDlNVFB2M3NFMDZQVHdhcTdSNUNNY3U3QWxJUlk0WEVGVTFzdUZpTExVKyt3Ky84cGI5RldIN0t5N1BBK1VYa203Y3RMMTl0T0Y4TDc4NFNiOTV0dU53WVFrUFhwNndoamhBRE1kWkVpUU90TlEyRGQ1SkRWdmFJVXpReEVBbnNjeUV1dGx4RWtPQ2loTHVNNGtnKzJBcXVtczl0Y3JmOWp2S1BNcS9iTjIzclk4Z2JQbXZjSjlwYnNUbjE5NmNQSER5VDM4Zjk0eFJXc21aWUpNNHhlZzlZTVhrSmJMUGUvcUpCRStubjBIVC95SWo0b1Rpb0RQMHBiZm0veFFvMmgwRyttZllzNHVaRDArSkluTTE3RXRCUVlRaVZuSkFJOUp1R1RSOVpWaFhISytQMGFNTlIwMWdWM21KMkNicVdaRW9rSVVGZFBETnhaWnBFbmoxN1J0MjRTKzdiTjY4eFZmb3VFV005bUlIbmV5NVQ4UGRoNzM2b2QreTBJMHFJVUlmQXMyZlArT0xMejFpdEZxVGtYa0J0MTFIWEN6L3JMU0t4Z2xEUko2TnQxYWhYL001LzU2L3o1WGQvbDU5OXZlYmlUSGo5enZqNjVUbHZ6bzIzN3pyZW5hdWJ2bG9oU0VOZEw1QUFUUjBIUnc1TVVDdU9NRUlJaC8zRUJ6dTlaYUlzODROclpNWGR0SUtsVE5GRzJmd3VCK3o5SU95bnZxWk0zTXJ1QmxiUWZqeHNydG91OXhVUFoyS0d5a3dNZFFLdzUrV2FQUVh6NGdRelZBS1NsT3pJUEptQzROeWNDbTNuTHFyTGNFd3ZrZDRhdXJTbVRSM3JmczN6MDhEeFlnSGhBdXRmVzROS1ZRdENTOWUyeEZCbDV5ZEJGWmFMSTc3em5lOVJ4Y0RYWDM4OWpHVkt0Y3QzOTdEN3dDYXZLZXp0eTFYOUV5V2xaUFdpNGVoa0tjOC9mY0xKeVJKTmlTNTFOTEZ4TzJnQ2s1QVZQcEhVUjBLc09UcDV3ZzkvL0ljMFI5L2w1OThrWHI5citNblBMM216RHJ4K20zaDMzdlBtWFVjSVJ4QnFRdlJUc3UzZFB0cjFHMElXQVlKVWhCaUg2S0x0d0FVYk5wYU0xTE5vcUdmN2RldEFHNVJHQlpIOHMxQ3dRNGgxMWFvT3o5NEt5bmoySERCWEl2ZmhRdzVrWk1GMW0xTTdSQlIwNU43bXVEbURuU2NuU0YzT1RnaG9PZmlMWDRXNGIwVUpjaG5XcHBqeVNzTkc5bCt2bkhFWGRmNUQzQmZETWxmaVVZekNSbDN6RFN0TU9ycHV3N3A3UzljbVBqdU5QRGxlMENEMDFoTlVJYlBzUVFUTDhyaVpCeU1kTFkvNTlPbG5pTUxiTjJkT3VaTVNFV1RpaXYxb3Z1aDNnYnV5NzNVVFdTMGJlZjc4T1UrZW5BenlZVjFYQTN2ZUovVUlyZEN3YnNXU1ZYem5pOS9pTjMvNzkrbmo1L3pwVDg5NStRcmVuUWRldmhWKzl0VTVaeHZCck1ia2lONHErbzFSVlRFcjdIcjNqcXNBbEdSQURnQW9OdHdwU3pqYWJzZXh6a1dQYkgvZEkxUGJFR1ZrODg5SGhwdFlTS1pCTFhlRk1oZmJacDVyKy9kQWRHbnZ1N0k3Yk5ZNnpDOEJXRVppRTZyc1NSZEZ3TExqNkhEdW1SczVnczlUMXdzcFJtSVZxS2pwUTRBdW9HL1BXSGNWbnh3SnB3c3hpMUVTWUNubE0wWVJVZmU2dFVCZE5UeDU4b3ppQ1hkNWVVbnFFc1JBWGRWdVVrMEoxWDVFOEx1YXZLYlBUWi9ma1oxdjBjNmhmdzhudDNuUVNFcUowK1dKZlBINUY1eWVuR2FOc2d5VTBTeTViYkdxMldRdnRPYjBTNTQ4LzNXKy84Ty93cXVMbXE5ZUtWKzlqTHg5YTd4NjFmUHlkYy9GcHFMdkExUUxxcXFpVHoxS29rdEtIUnVheGRKUDFJelVjUWdmbkN1aFppeDZtWUFCUVNmVUwydDVSN2I5QnV0d3hZVGVEREd2TUYxZCtmeCt4NUhEengreVZkdHdlZmR0MTQvL3FuUHU1dnQ0dTIranhVWkdUUnJBb0VpY0dPSWdtUHVDeTNpZGJQMG85MGdvaDdUdjQ4NENmVzkwb1hKMW9rRFNCZTE1eDdwOXgrWlVlSFprdG94R0haTm91a1NBeWhJU1BNWk9YZW5FMGVrSm45bm5mUFhWVjNUcG5GNFZTVzUyMDVTUWNFQ0x2cjBCcnBxd20vakQzaGJtQjBiV2s0WXcrT2VLQ0p2TlpnanhmUDc4T1NMQ3hmcHkwSnluM20yZmJaZVFhSFJhRXhhZjhQeUxIL0tEMy9rYi9PVHJsbTllR3kvZkNGKy9FRjY5YW5uenBtUFQxbFQxTWZXaXdnZ2s3WnpWcVNaUmFRcFFaYVhUMk85aVF4Ni8zOURFdUtVcXZ1Nnd2VTRHL2RYenQzbCs5d0M2N3ZtcDZhOXdXUHZ1bHNtN0J1S1V6WGM5UXJTS3RhMUlXdEdyWVZwUkJTZE9KOHVPbzFxdERpS1ZiYUFvRlpONC9IdndmQVpQbno1RnRXZlRKbWdURWdOdDErWm9RZHRsMFhlbzhRM3g5TkNFSEZxS2ZkUitmb09iaFRUcG9DR0VIT21WRW5WZDgvbm5uL1BzK1ZPNjFGSlZGWXRGVGRlMXpqcFZEU2taVmIzZ1loT1ErcW45NkxmL0JxdG5QK1RuM3lSZXZvMzg5T3R6dnZsNnc4dlhQZXRMUWJXaHJvNkJpcjRYRk1YRUNEbTIyeXhoS0dwZGxxVGp0UWZaN3RodXh0T1dUWFpYaGRLdm5uL1k1dysxc24zOVNydUd5SGd3bU9WZ293Q2hJYWpRaW5DV0ExTEFrNVdFS3BqWUcwbDlTNDNiL0ZVTkZVZnd1cXA0OXV3NWw1Zkd5NWV2YWRzV3NVQ0lydDMvS0dUd2ZTNnlGRk5Sdmw2aXdncjcrOWxubi9IcHA1OFM2a0JLSFcxcUIwcWJrbkd4YnFtYlU5UldXRmpaai8rdHYwdDk4dXU4T3F2NTg2OWF2bnJaOHVKVng2dFhpZlZhMEZSRGFEQUxKQzBaU3R3YXJlb0pJRHpJdm5pZWdXbUNpUjI3OUh2Ky9lN3dxK2UvM2MrWE52YUtydWJlaG1xQlpJSE9JcFVFMWlsZzY0Z0dkeVhtR0Jhb1JUbVRxQzJWMU1Tb21MU1lRTnYxTkZYRGQ3N3pIYm91OGVMRmk2eFJkeGZvQjBQd3U4cnc3UFVuSGllMnFxcVpiYS92ZTA1UFQvbnNzODg4bkM1MW51ckl4Tk1xeFVoVkw5RmVhUHNHNWNqKzByLzFkNmhQZnBPM2wwdis0dXVlbjcrRWwyOERQLzE2UTdjSk5QVUpnWXEyYzMvc1VEVlVsVWQ1dVl0bjl0TVdSaTgwWWVZVC95djR4WVpETXY4UStGTlVLdVgrOHJudHZ6RjV4a04zSTJMUkU0aElSS25ZbUxHNWhGYmR6K05wbmFoanBPS2NKQjVnTzFYYUpvUGpZL2ZjYk5jYkxpN1BTTWtEcis2dFpKc080cUh1MzlZSEZKYTg2em9XaXdYZitjNTNPRDQrenZuTUFuM1gwZFNSR0dzM2ljVWE0cEsrVzlwdi9zNGZjUExaai9qNmRlVG5MNVdmZjVQNCtZdU9WMjg3Mm41SnI0Wm9EUVEvS0xKTFprb3BLeXJjdTh2VkpqckxIK0FuNWEyRy9pdjRKWWZpWU9QbWR2R2NlT29oeFc2Mml3UldhQzlZTU9LNUVsYktZaWxXQlJGTmx4NjJLaUFTaUNIU2R4MFg2WUtuVDUvU3JqZjgrVjljb3VyK0dZZGRWVzlnanJsU2tYSEwrdzlCMy9jekZ2Mnp6ejdqNmRPbi9nNHpKRlEwVlFXbW1DcFZ2V0RkVlNoTCsrTFhmc3duWC93bFhyeUxmUFhLK0pPZlh2RFZpNTZYN3pvMmZjUXMwQ3lXUUtUZGRFT3dTdGUxN3VFZTNlUlFFaVdhQlU5T3dMYUo2T3FJcXZuRWZEeCtCNytDaDROcktYbTVNZVBBNElvTXVOKzkwRXZDeEs4cEM0STk1V3l0QkRVaVBTeXhWWVVFN1JEdGlNRmxlalVqOVIxTjAvRDAyU2xuNTg5NDkvb1ZiZHZlejFYMXNlNmZ5ajZlaFdVeGFNMC8vZlRUSVJRMFJuZk1UMzBpTmpVaEJqYXRvT0hJUHYzeVIvend4My9JMTY4alAvbDZ3MTk4bFhqMXpuaHpscmhjK3dLRVdLTzRNOEVZaDZDRW9Ea010V1NzREtqNmlUbE5kdWhSU3I5QzJGL0IzY0FQQTgxc3U3cFNWMzFQUmF2WXNDQlVUM2w3MlZMSmhxWU8xRTIwS3ZVaXFVTjBReCtncm11a2hyWmIwelExbjMzMm5QWDVXVWJ3TFVwdDdHZlZEMnJEcjdrK1ByOGZHUXAxTHRlS3ZGMGNTc3JuWXJIZzg4OC9aN1ZhNVN3WHJzSDJrMUNnRHhnVlNtT0w0eS81emQvOW0zejFXdm42VmVCbkw1VVhieEl2WHZWY2JLQnVqa0dOUGhrbVJtZUptTTF2bUJHcmtIT0VlY1NaOXpQUGpzMzcvaXY0RlZ3Rmh5ajVLS05uWWlFSlNLN3JRVkNyYURVU3d3cXpVMTV2T2pnelloUk82dDVpZnlsMVphQ2VGNml1YTlwMnpXWnp5Y25Sa3MrLytKVCtwM3NvK0dOdDJwdll5MFZrNWh3U2NrcGk4Q3lVSnllanAxcU0wZjJEODRIUUpxR2pzdWI0QzM3NHUzK0Q4M2JKVjY5NmZ2YkMrT1pWNHNXYm5rM3ZRUXg5QjFXb0VGR1gyWVBucjU3bXdSNTVMcjNtOUxvRmUvNHIrQlZzZ2VpWXVFSkVVQkVQM2pFblhGMEhVaDF6YVQyeE5SWVhFWll0eCtIRVZCRFROY1JBbTNPM2hlak9WaWNuSnp4Nzl1eDZYL1RyWk9ucktMY2N6S1dUbjk5eXFDa0lYSlIrSVlRaGVjTnF0U0lsNXdSRWhENzFORlhOZXBOb1ZrODV2eEIrOEJzL1puSDZYZjcwNTRsWDc0U2Z2OWp3OHEzeTdrd0pZWWtnYURKUExtOVpGdHBHYWhqVGRodnNjRGtQcEZtNzYySDZxL2QvZk8rL1RtVzFmWG42NWdDZUNsc2kwV3BBRUtzQTlkUmRzY0wwaUxOT2llZENwS001ZWtKSFR3aEdZRU9uaVZvS0RpVVdkYzJUazVQYnkrQVBUZUczM1ZMTE82YVUrdG16WjZ4V3E4R2JyYkQxbG1YakVCY2txL2prMCsveC9Jc2Y4ZFhyeElzM3hzdTNnVmR2RXBjYndhaHg3ek1oaEpKRVFZWms4OFA3cHl1eDdWWktmbXp1dzNqck1YOW8xdjVYNzMrNDk5ODNrTTc5WE1QRS85V1J1MlQvU2NIVGNHbGFJR0pjdHNhNzJIT3l1RVJrWTh0Z0lpU2lLQXhzdmhPdzFXcjE0UjFkcGo3Yit4QThoTURubjM5T1V5OXpMak0zVFNVMTZycGhzMDRzams2NDZCZjI0OS8rZmM3V0ZTL2ZHdS9PQXovLzZveTNiejFiU3d5MXkvb20xSlVuV3ZSTUlEa2hvY2x1RExQcHBCYllsQldmdTViK0NuN3hZZDlLNzRRWDNBS0tUN3RuN3hxd2UzeWZqQ21xU3MyektBdGFsSXROeDd2MkZHSE5ZcUdtYVNOMURJaTFPZWdwWWdxTHhmSkFMTjhCZU15VGQ0aTZta0FJZ2VQajQ2SHFpTmY2cW9ZYzBja0VRc1BiODk2KzkvM2Y1ZWprTzd4ODNmUDZiZUtibHkxdjN5VTJuYnNEaGxBTk5jZ0dEc0NkaFptSGQ0YmhiNTU0WUN0cnlKQW02RmZ3eXdyM3hRYjNld3dNL284NVZOV2s5MXp1MlhOU05hQTlua3MvTGRoMEM4N1dLeTY2RTFTWENBc0lUWTVCOTU0VjRuaGpDdjQra2JzbzFwcW00ZFBubitla2hTNlBMSmZMNGY2K1YwSnpRcVduZlBjM2ZwY1hiMXRldlRHK2VabjQrc1dhcEEweEJHS29RWTBvN3N6U3BUWW5nRkJHOTJDQktic2xYc2x6NjhoQnpLdU56Q09xYjZkb3U0OVRrWGZ0ZnNrZWZySGVmL3REMW1ZUmU3ZFhrczdDZlV1YnQyNWwwaCtCVW5TQjRPbXJNQTlxS2JYcUFnRkxYajZydDRhMzY0WXFMbG5YbFJlNE5BTThBV1N2UFFHdmkvZkJXZlNTaWFMOFc0aDBmVWVzaEtacGVQTDBoS1FkZGJVWU1rcXFLc1FJVnBPc3NkLzQwVittVFVlOE9UZStlYjNoZkYxeGRtNlpjamU1elVTbzNMVFdkNGxRMVFPTHZnL21mdVhsdmw4bWxueWVhZ3FZZUhGTWxLeXllL245d2swUTlOQWhNSW0xMytIVWJ0UCtYU0VNY2VmYjRGRnFudWROUWtVUWRXNlRRRWREMTYxWWRoM3ZOaXNXc2JXb1NlcFFFUVJNRFluUTl6ZHdkTG5ldkxWN2hwblp4TWQ4VzY2ZDVMd2V2TUxNVHlqMW9nQjF2YVR0em5uKytYT0lTb1ZnMnJOb0dwSWFGZ0o5RDlYeWhNdCt3WmZmL3pGLy9KUEVpOWMxRjVzanZ2bm1ncFFXU0d3UWFuZFNDUzZYR0VZbzFVWnpSY3R4VUpNc0xEQy9Oc3NsVnZLRXcxMHB3SDNndmxwa2YzL0pQZ3F6dGN2aGpLWDJsWVJTc21oY1V5dGNqSlM4NW43NGxWSUVzQS9aUnhGb1pocWxQRCtIc3UySGhCQjd4cUZtMlNkN3ZGN3Fkdzl6Rk9MV091YlN1eVd2WGZZZUV3S2Vobm5mWVREUnhReUpOTGZXZjN2QUIvVXo4L0VIVDE2UTI2c21VeEVvZVFLRVFMSU9OTkJxb29vTlZYaksyMDFpR1o1eHRJeFVzcmJBV3RTOFlLTkpoM29TN2NQdzJMN3BVK2VXNHV4U0ZuUzFXckZZMU1DWVo2bzR0M2dLcHBxemRXKy85b01mYzM0cHROMlNiMTczdkQwM0x0dElxRmFrUHB2QWhuempOOUdMNmVSdkYwYmtPbnpQZFhCZjl2YStzTXZlWHVGeVhPNlZjVjZHYXB3V2NrV1JhWFdVcTVEYi8zMHdUbjZQN25JYWpybGphaExaMm1zRjJieVlvQ04vUms0cEZXRVV5K1YveHQvMmRXYi9BZjZ3ODcvZGZwajg3ZlpIaVNTTDlCcm90S2JYWTlicGxMZnJCZzNISkNxVUNnM09HY1RxUU1xbTJ3U0Q3RXg3TWRvZjJqUmJLWGFMVDNlUnl6eTRwT09UNTUrd1hCN2hTUldTaThobHNTVWdvU2F3NG52Zit5RmZ2ZWw1ZHhFNE8rOTQ5V3BEc2dvaEY0QXZSZTBtMU5sTWtXdmlmRzhLZTczK2JyQUo3aHV2ZlAvM0owUWlPdVJWTHdoY3hKRkNzZkxjVFFyTkM1Tjg3MVlUOTFYd3pKUjl1MWFjNVhkWm9XVDVnQml2elg3YWxkY0hxVWtHaTJYaEhXeHl6L1N3S0pSc3JEcWUyNS91aWR6bjRYQWFEZ05qcUJoUmZwOGs5dGpoWkdWWFdid1BkdGQvbXdNb0E1bnJxRXpkajhQNVg2SHJLODR1QTkzcEtjSE9UTEplM3N3SWNVOWxrOXNoOTkyZ1pHb3hKcWZ3Uk9raEloeWZudVlTUkpGT1BQTjQyMjh3OGF5b0tVWDd6cS8vRmhhT2VYdldjWDZwbkcrVWRhL0RwcDNLOXlOOEVFRnhMenlVdzhiZDM1L1kzUUpqYnZMeCt3aGlmbkNPRXV2SXljeXJGTzB6TDRaOGZIaGhQd3JlWkJsNEtBRnNjNjJIMjNVbEh6dmpuTWxPcHRmeTFGYXFyRU1xbEZtOXRMa3ZCaVR2Kzk3eTFQYzJmbSs5Qy9abmtDd0g3bFl4Q2MzNSswSkRyd3ZXM1pMTExsSFhUOUMwQVF0ZVNaWnJhcE05S21RWFVGZm5WNEFIYjVnSWk2Ymg2T2dJOEJNTEM0UVlhTnNPcWJ4K2RxOFYzL3Z1NytUc3A4SzdNM2o3dGlOVXAzUWJvNm9reTkyVERTSHp1bWIzNnY2MzFWbGpKL2FnSC80RlpadW5TY1dUY21tT0RNVzBtS1RiNGNxR29nVm1pT3l5bStQeFh0b2ZENVdTZDNMSVA2bGV4V1BZNjVQMTNCODNVVWgzK2VaVXU0Z1RNajdzKzBGMDFERnM5K3NLcmVFd3JCMzZJYnUvWFFtVHcvUkFtdGdTOURUb1RzeTVVS1ZtblpiVWNzenI4NWFqVDU0UWVHTndLU0s1cXV6UTRSdHNtSWZhMURPV3cwWVp1OWlwVjBjTG1rWGxjYks5NW9KNkVFT2RrVFJTTDU5U0x6L2g3VGxjdG9FWHJ5L1piQ0NsU0ZVdDJGZXo2VU5Uekk4VE1qc3V5azd5RFp2SzZFVTJuR1N3bWVOU0xtVTBQajVucmYxaUZvZTNZQzY3VHo5RlpQZjdWRW0xWjBsdGFMTzBHNFpSRE8xWWNKWkRoYjF5NzNzMUNkelExR2RqdFJWVEY2KzZWTEhoaUxjWE5adCtSYUwyZk8yeEFydkNEdjdlaG1lNUFrak0xTHhpY0VzVmhKRGQrRFNCVkJHVlNKZkV2dmU5SDdEdUt0WWI0Znl5NTgyN0ZvdExOdXVPYXJFZ2RaM0xtZVUxZTg2bUlMbjI5Z1BEUThqV0QvNythemJzdkRwSkdKRjdvTXBieUM5aytsQ1A0dUp3eis2N1pDdFFPcER0dlRPK3VSd2VONXM3TTBnVFMrZkFnNFNKakd3d1dndDhQRGw5WnI0ajdFN045SWREclBnVnRPNTI2ejhWaDdiNllENFdIMEwyK016eXZ4a2txekNwNkxUbVFoc3VlMkVWRnhnUnJBTDYyOW5CNyt1Z3NLKzlLcWMvTG5XOVF3Z3NGb3VNNElFZ01WZFQ3SW5WQXRWSWJ4V2ZmZkdiWEc0aTZ3NWV2MnZwMWRQZEpBVkpuaTY1NzVNN3QyeVBnU3VVZ0wvTWNHQXpiLzg4TFJka1ZtVlplbGRtbjFZT2RaWmJCK1FTdjJGTERtYUhpeWpjM1hCNVM4RWJzaHp2a1ZpNXZKUHRJbzNCb09tMzRkQ1N1NHZUVjFWZGVDZ1FMdys4elkxNk5SYkJGRFJXOUJwSnNtTGRLbnEwUkxVbVpFL01xaWkzcnUzcU5DLzBBeUY1VVlKNUttUmZ6Q1lLVDUrZXVyWndPRkFDb2E1SnllZ2xFS3NWeStQbmZQVXo0K3pTV0xmUTlwNHNVWEo2NDVSNllneU1vV0xlNTBHUGFxWHk1TU12MG9jV0JXN3pmdG5hUE5sdzZScGxJU2Y0Tno5b3M4blNxMmNZcWtZZEt2cGVxYUpiUS9ya0JTQkRDS1MrOStLZ0ltQjlYZ3RYeXZuUytIdW1ZbG9jdXBPVEJrN0tKNXNaeXFRWW9JMWpsVkNSOFBKUkhtWThZZWZOTS9HYVJDOG5ySDRZQkdsUVRVaXViSkpVYzZXYUtUSGJReUNLbWJCWWFtNDEvMU9YNk9uMy9UQWtJQlVobWZ1Wmg2b0c4R3pDd2RCUWs2aDVkNkY4ZnR5UXJLS2lRZlFEWmxVZGJONWF6QVZLakpHNnJva3hadFo1OUtsTkpKSUp2WXFkUHYrY0xsVmNicFR6VFdMZEduMENuelFacVlEb3dlTzVuUEVURHU5QjROdUUzTENmbmZTa0E2NUI5a05ZU0pwOFhUeWJOU241ZXZYZG1saEZVdXFwcXNCUlU1TzZTeVFaVlFYV3JaSFFFeVNCYm9BV0NUMlJSSWhLYWx0aUZHSVQ2Vk1MU1FuQmZSNUs0bzhwZ2llODJHUkt5V3U5RTNKWnFGdzVsZ29zSXNGbDBOUkRxQm9XZFVQWEc2WU5JVGFJK1lFbFhwNG1sNTdPeFN3RzY4c2VCV0cyQVBuZTNKOVQvVEZnbTZpYUdWWVFIeU5SMDF1azdTTkgxZEtVVnFMY2hFV1hxMWlrKzREaUJlY2R3Wk81SEw1Y0hsRlZEWnV1UXdSS1dpUkxna2lOV01Obm4vMGFsNjF3c1ZFdUxoTHJOdEdyNU0xUVRsblh5QW9oYTNYOU5DNW1sNXZBVGNaNlhWdDN0Vk0vL1B2SHR1YlBiSHVKT1lXeWtFQUV0UjVORUVKRDMvWmV5VEtYR2Jha1ZMWFJ0bWNzbW9vWWpQYnlnbHFNS2hwMGE0S3RxWFJOWFc5UTNsTFhHNTQvWC9MbEZ5ZDgrdnlVWC8vc2V6eDljc3Jubno5bnVZUmxBMDFEOWhjbmN3QmtxZ3FkUXVxaDd5RWxPTCtBdDJjdFAvdjZCVy9lYlhqOWJzMnJsMmU4ZXYyV1Z5L1BXQ2RJWFVXTXgwUlpZclpFZGNrbUJhcXdwTE5Ja0lZZ2tXUkNTa29UYW8vUEhtQWJrY2NxcTlkUjRNUHJmK0M1YVlYV2JQWHhOZ1pWWnY0Ykl5NDFnVWFoMDhqRlduank5Qmp0THlBc1Bxd3Z1cXYvWTNaWmRWUEpjcmtjcm9jQTFudVFTVXBHV0ZRRVdmSDBrKy93NWlMUnBWS2pXMTBXQktwYzRsYnd6QmFEdWVRV3N0WkRJTlo5NEgyLzM3SUIyLzBSc3NrcUs4QkNDYnBKeHNuUktXM2JZcHJRMUZMVlNxQkRxZ3RJSGRwdFdNWkVKUzJWdFJ5dGpDWjAvT2IzUCtNUC91RDMrYTBmTFBuMFUzaHk2bXZSUktpeXFYbFJRWi8zZkN3ZXhHSGt0QUtlSGhnR1BWTm14eUZKdzhYbGQ1SG85NjViZi81eURXL2Z3Y3RYOE5WWEcvNmJmL01UL3MyZnZlVFYyemNlVmxrOXcvcFRlalBxdUVSN1QyVU1FS1dpMTg3WjhjS1NsM1V4bDR0SE0rREQrcXZmbUlqbVBxaUN4WXJlS2k2NmlIS0Vhb1dGV3dhYjNJcDZGOVBFamgxajdpdGMyUE9TbnFsWkh0RWxjMWtqQkRUMGtPWEExQXRTTFdtYUV5NWZHVzNuUHVtYWNvcWIyZW1YdFMrUG9FeTdDWEk5ZHZUZCtLTHByci9xL1ZOMmMrSWJnQ3RzeW5WVk41a1YwUW56eWk1VnJMaDQrNDRRemRQMnBrdXFxS1QwanBOVm9yMTh5WExSODkzUEZ2emdOejdsYi96K2IvRnIzMTN5M2MrZ0VqaGVRREUzTnpWMG5TTjNFRWpxUVZRTmpyQ2xabGRoaGpWM09XU3FidVJEQUVma0lOQlVodFN1MXVzQ1ZCWDB4MkRQb2ZzKzlMcmcvUEszT085K2k0c08vdXdueXAvK3BPVS8vYzkvemxmZmJMTFdPVkRsb0tZUVNyNi9iTTdObFhibXlSa093NDNYLzhENkhXNm5lTkRsVEw5OVFHTkVhNk9uNXFMM1lKUWdsYW4wOGlnVWZKUlQ0S3FaOEl3dGdUNzFlTVpTMTZDcjJwQzlOQVFnQ0JVMWF6V0wxUUtqWXRNcG16YlJiaEtwQjVIYVV6SGxldHBCQ21lUXFmZTJOOUFqalB2YkM0VnF5N0IyRW9RZ3lXUHdSU0JUWkZnamJEaXRFNmw3aWNoTG50WHdkLzdlNy9OWC84b1gvUHF2d2FkUDNLeDVXbzhXNWdib284Y3Jod1I5MjlPbU5DaXNldEdjSlRlbjZ3bzU3MTRPa1N6c3FHazJIUm1JR1UwUWtobWIxQkc3Qm9JZ1pvaFYxSGxrZGVXbHAwNldzREhvZ09lZkJMNzg3cEwvNmwrdStPYWJjMUozQ2JLa3FpTnAwNk80a3RFUlVITS9iYUw1My9hRGYzell0bUw1dnlzdkw2d1JpelZkcW1tVDBraU4ydnBoNDhIMzNsUE1JSHNybUl6UGlBaDFYZE0waTV6aHBkVEE5a0NVR0NPaWdaUGpaN1M5MGZhSmRhZXNOeDJxZ1VpazMzSmRMTXpEOEdZTEhraVBBSWxEOE5EbXdHMjR6azU2cC9jZnBBVDdGVVZGRHN5cW1teTJxZ2JQczVEUDZDZzlaaHVDdGFSMFFaQUw2c1dhdCs5K3huLzNyLytRdi84My93NS84SHRISEI4NW05MVVRSUpsbmJrQmRWTldsNVIxNjNXM3FxYW1ib1JHYWt5Rk9xdk9vNS9OZEoySEM1dVpkeVJyMGpVajE5VFpKYlU5U0dSVnJWQ01MdlZlSUZLVXVvNmt2cVhkck9sNmc4V0tucHBONFJnQ05MV0J0TmwwcHJtWVJsbURNbU9LcDBNQzA1d0VST2JUZnF1bDJsbC9wVVNPK2J1bmg4YzBXcTNJL2VQdlFvV3BlL3duaS9RMGJOS0drMWhoY291RUR6ZnA5TzN2RHpNV3ZxNFhOTEhDZ3BCU24xTXNRZHUzTk0wUm9WcXlPbjVDMXd0dEQzMFMrczY5ZW1JVnNSUW9kWnZCRlhlUnJLV0J2WWZNWFNUWmo5S1JCWGFRdTlpcjkzbU9HYzVxaWlpaVdXdHVQYW9SUWVucGlOSVRRb3ZvT1hWMWlkZ3JJbTg0cVJQLzIvL2RQK0pIMzRmbkRjVGtpQjNFZlo5VkVwdDM1NWdaaTdvbWhCcEJPRnBVU0lnUXZiSkgzL2RVQk9vbzlEM1pVdzJxR0dtcXNPTStYZ0pMWFQzcjBDd3JPaTNXVUtHcEc4d2FyTzhSZFk2dVhrUTJNUkdxaXN1VVpYMXhuRzB2ejRpQXhValNuUE12Tm9RYzVEUWd1czRMRll3WmZSNkNGN3laSjlzWW5ETnlOQ0Z6TTZZUjA0b1VGN1Fwa2FxQXNnL0I3NmcxMzZFNkJ5aDIwVUQ2S1l6WE5CYVhuNCtXeDFqeXlhdERwT3VVSkVMZHJOaDBDblhOODA5L2pmTk82Rk53RnIwVGtJcXVaRnZGTjBtZjFFc1ptZVdOb2pOc0hsbjIzWDdlbDNxL3orZU5xU3RuSGxOd0gydk5Gd0tBNVpUUTJUTktNL01jcEtmWG5xcUNsRHJxR09oVFJ6Q2phUVRWYzRndnFLdlgvUEQ3Sy83OWYvdmY1bS8vd1NtckFFY05OQUxhUTNmUm8zMUhGS09LZ2FwYUVreXBRZ1R4UW81UkRISmFYeEZZMUpWeldWbFpWdGFucW9xQTdkOVZpa0syekU5QmFEKzhLNEdwUDFNd29LbnljNUJNT1pLR3BFSkswUFhRazAzelhldlpUenFCcWthSnhCRHp2akdDeGl4MmwwQ1pxUk42WXArTzUzYTZxbjNQVHdaLzVYMkNSTjhEbHNRNTJIckYrZWFDdEt6UkVPMkRVUERpZkZDY0hJaGUwS0d1UFRGaXpONXJnSmNQSWhEaWdwNktxbG5SblJtcGg1UXMrMFQ1Wm5YSGliSUFJVlB5cVVJcDd4dmJwZzF6dUk1RnZvNTZ2Ky9uM2FscU9tNUZRL2xkUVJoY2ZnZVhVdkgwUGluMXhEcVF0S1dxSW0xM1JoT0VWUlBwMmhkWStobS8vcjNJZi9EZi8xdjg0Ujg4NWJ2UG5HS2ZSbS91NVl2WFZGVkZIU0oxSGFsQ0pNWTRPS3pFWXVJcTBTUG15azhiS3NTVS80MkRNY2I5ckpwWjQreCtXbnpUQzRFZ0dUcGhaME1KRDg1c3RFUUlKZmhGSVJvNVlNVmoyQ00rUis3ZjdXa2d4RHpmdmpjQW9uR1U5OHFyWkt4NnM4TTV2Y2YxdDFtQ2l3cTFpczRDbXRPQzcrWkZuemdWM0FUdVM2M2NreTFLNm8yNmFXYnZMVVhPVS9JRWljbTg5bGk3NmVsNzZEclByT0l4dTVOR2d3eFJSenZJY0l0KzNZY1ZmMy9QN3orc3hKTytJd05yT1ZsWE01QU9VRnBkczJ3cWpJUldCbWxEYkl6MStqVU5ML25ON3hqL3EvL2wzK0ZIMzRlakNpb253Z2p3N3MyR3VscXdXTllzNjJwR2hYT0dvUW51K3Z0MTBCV1U5WmxiUDJiREFrUWlERkhscGRqanVLbXpRWFE0dEVmWFZoa2NtWVltQlZKUjFyRmRXeTU3MVNsNEZaNTV2MGNvbnBCWGE3L2YxL3FyT29sTE9oS3ZsQXozOWorUThBRWV6OHd6ZWlVVk5sRkJvaGticWV2YUZXemlwaG8xQ0hsQmZKN2R2N1pQUnArVVB1bHNrVXhDT1hRZnBQLzNsYk1mKy9seGIrMi9UL0loSjBQaVBqLzNYTkhZWTJLY25CeHhjZm5PdmRUNkM4d3VpV3hZTnQvdzkvN1diL0lmLzZNZjhOa3BQS21nQm9JWWw1Y2JMaTE2ekVCVTZqb1FTOXNadjRKNS8zV0NCOU53M2VrK3VBcGtXUGVSV2tGd0JSNXVMbkttUUVEVUtmS2d1TTE5a0RJSGsza3I3eFYxYTh1a3VrZ3hENHJJUUxpZGFRNDVIcDRyUk5BUjNzZitHZVJ3ZGFRMkZWSnZXYmxkZlZoWDFUTExSWE1wd1FoUkJvUTJrcC91TVdBcVZsWDFnTlRKY3RqY2dUa3cwUzNhTnF6b2NCM3VwZ1g5V0tHdzVPT1l5dWl6c1NvcnNSQkRvaXVQdXZXR1piMmc3OWFjSGpXMEY2K0kvZGY4MGQvL0RmNlQvOWtQZUJwaEpkQWtXSjlkWXJGaldVVVNGWFh0Q0NibXlKMlNPdHNiZ3l1RmQyTEt2UzlUeEEvVFd3cHJ6YWdYbmFYTW01UzFLbks4VHRldkJKR016Y3hBOHd0RmJMQjJEUlRkM05uS1NnYWhRWXlMTyszSThLNlB4OWdxRWhGUmN2VndVbWJUNzRYZ2Q5T2NiLzBXZzdsalFSaktGSFdweFNTektPWGtOMk94V05DMVJxK0J2bE9TK21LbzRFa0JzaXoxb1pNeHZDOFl2Zk5jUGgzWVI1bjhld0lsNDRwSXBKaGR0RGNXVlNSb3g4WEx2K0N6WnkzLzBULzgyL3lQL3VFeEM0TVZVQ1hqOWRkdmVIcDZURndFVktDTzFlQkpWbGp4RUVLbW9KbEQwN2tzbVR6c2JBWnplWEs4YnJhN1h3WmtsMXhqZTlhY0RjLzV0Nnh4RHVLUmJ1YlBsVU5IcC9jS09UTFRHMVRacjlmZTlZYjhPSkI4bWx2QkJ4TklCaWJ4dzVVUExsUzYvRjVWbFVjcEJVTlVQSkFnZ0pvcjRpd0l6WEtWODFCbFRhamFvTldjeWlzZk91RGovY0owWHZXZzd0Q21TRzlHVG5QSHFsNFN1elVMTnB5ZTl2eWovK0MzK1EvLzZKaVZ3a21BMUVLNzdqekNEeVhHQlYxU0xQVVFneGVxejNKdkNHR1FiWXNzWEpaQ1ozMlovSHY2dTQyL0RFa2FoRjB1elViSzdkdkhwdEhkZVZaazhJQXJMRDZaV3BleVY5dUhTTnFhUEdmVlI1bCtkczNJN1AySEI0L3d5K3k1Z1VwRnI0TEYrdUZaOUpzb0JyYTEySm9SUGNZU2ZPSUZ6YXRjMGxmVkVBSjF0VUFJcUtiUjltZjl3ZmZJM3QzeGl3VXpFV1BtbmpzMUErcUlWRG5EckdtSFdZZDJGMVM4WVZtOTRuLzZILzQxL3VFZkxUaXUzZk5NZTZOYjkwZ3kraXB4ZE5TNGE3QzVwaTNzT2JRTmhneTU3bjY4cGVTVS9RZndOR0tzakFCQTl4TElpZUNWeCtzdXQyUFJTbTh6OXpWcjNJTUZBamJNam5NZm80ZGMrZmZCUFRPVnUyOGdnNzhQS1BnQmdZUVFMUit5R3RHck1yb2NhdXltOTgyMDRTSnpSa1prNitSVFQvRmFCL3ErSFVJRTNXVlJFSWxvZ21aMVJKYzhSMmF2TmloZGloOTdDQUUxR2Y3dG5uQU80d2EvellpL0RSQ0d4QXFTTll4bWhWMExTT1VEVDlZVFFvVnBnS1FzRmczU3QxUjJ4a245a3YvZTMzak8vK0RmWFhCVXdZS2UxRUpxRlZWaGRid2dWc3A1ZXdFRXFtYUpBVjJmcUltVTVJZXVPQjN6c0tXa2szWFA4bkh1NlJUS3N3emU1MWx2YnRmclNJSVZBc0c0eUFvV3NvWTl5S3lOd3NZYVFDTHZyZDczVzRJUUk2cENDSkpOdGxPNzkvYiszL2ZiKzRHQ2k1cmpPSko1WWxLSkZVazlONExKSXlyWkJnUTljQ2k0ZkszV204Y2N5eEQzV3p4MHFpeENaU1ZIRVBjdjEySldFWko2YnBCdGVlbVhSUVlIUjIrYitoUmtXWHlvRXBNNk4vNUdvOWNOVWFDcEkzMTNTY003VHBkditmS1Rsdi9rZi82N0hOVndJckJlbjlPdUE4dkZLZlVDUHpTQ0VKc0Zxa3JYZDFrME9td2l1Z29PWFo4R0lOMXFEaWF5OFRZeUI1T0JhN0RNcm0rN0tZdDU5aUFwOFErSGU4N0hJSE5Qd1oyN1BBSEhPUFNBV3NRMFVOMTFVbS82Y3AzOGUzdGhuYjN3OUVwaDBKQzZvNFlFbWNoeHJqaHdSeGovcnViMjhXTFd1RXNLcHVMNWRsdDRLQm4vcm5PKzcvM3V0ejFwZS9LN2k1K0pBQ3lxQ3UzWExNSWxGVzlZTFg3Ty8rWi8vUTk0ZGdwTG9MTU9UWUhsOFJISmVwcTZJaVhEZXFoRFJDeGlxU2VKVVlWc2w4NEhycGtkOVBBZktvUG9TS1hCcGVkOVk1RXQwcjF0S3g5ODBYTzg5QTV0MVpJeE5hZm5ObzlhZE80bU85TVlSTUs0Znl4TStLR3JZV0E0N3JFVkhoTG5pdXQzMFJqMkNub2JNOWxEZGFac3poQUtPNWtRcWJmYUwzWlA3M1RoQk9wNlFXc3k2RDNjdkRheWh5SzdKbzJEWS9tQVJQNUJGemJMdmU2enFIbE9zL2hpV1hsWndYcXpkcGRPNjBpWGIxaXQxcXlxMS93di91Ty96MmZQWE9ZMk16WVh2Yk80NXFtMCtwUnoyR1UzVC9kZUNPNnBsczJVd3RXSFhwSE4vY3ZXMkI5NEhRWVpQenV6RkZiZi8zSVEwdzBSYy84NlRYUWJkOHlOL3FBK0dsdG1SaFdYeGMxS1RyWUhoTHU3NTQweWM1RGlhcGlkL1JWZkdBbFVUYzNGUmttV2JYMGw0a3ltN3lpaWdiTXRBMHQxTU1ycTQySzU3Z0tXRlVtWmtHTG80S0xxemhwR0pRRXNJV25EVWRNaCtqVi8rUzg5NXc5K2Y4WHBFdWloWGJmUUcwZEh4eERka2lFNUMya3dodUltWHU0cmtNcnZBRFBQc0cxd2s1V3Z6NXhqM0JYamluL0M5bnFONnpUMW9TQ0VrWnFTblcwbTM2R1l2VHdhYmFiQkg0d0tPYUpPRk10MjcrdTV0QTluSXR2eHpzenJuMlVwQk5kWldSRmZyNUtUcjdxKzc5NmJRR0hYcDR0YmFuNVByNHQ0RGpCeWh0VVk2c0hNb1R1OFlIRnkrSVhUb2wwSkpxV2UyNmc1ZHcwMnhDaWdpWGF6b1k0TnE2cUJ2cU9KTFNmTkJmLyt2L2RYV0RVZUFYYng5cExHR282Ykl5d3BkUno5Q1Vxd21ZZm1lMjI0b3QwdVN1ZkJwanc0anRqZTc5dHdHOFh0TkV3VUdMeGtUR1JJY1Q0N0Y0SU10djlVK3BpSnczZ0FXSGJxSEpGMUtEUXc3d0dqZFdMNjkyRmhlNjVMREViS0hPMnVMM3FHMnlEM2JXQzd2V0txcUtwcTZLVEw1dG11bWl0YkZFY0tSL0NjcG1ackU5MG04dTArck9GOXcwVnYwOWRyMzcrVldITHE0akpzV2dWVWlMRkN1dzdUTS83SzcvOGFQLzV0cURNeGpBcUxLR2hTbHN1S3phYkZZczRiYjBJU2lCTEdvQlp4VCsrcExYbTZIamVkb3pGSVJuTTdlN1JsMDNGZitWdldhb2V4SFIzNEM4bDF5bUQwYVdmb3J4VnViMXFIYklCcFFFa1lNeVlQMDM0N1NuN2Y5ZDhGQmFvSnNvY2NPQ09FaDNqUmZaVkZWalRwazNiRTB1QzBFSWllODFwcVBGSGpOS2hnMnVDNENBNVhUYnhlYy8zYkFVazFtNlpjbTFxa0xxTkh0VU0xc1ZnMGFOZGozWnFtYWprOVN2eURQL285VGxld2lIRDU3b3pGWWdGa2J6U0JPdFR1eTI1R0ZSaVVvSm8za0g4Wm5WbTJuWXh1ZHdBVzVENndIdVlsZEkzSkFiQVhDc1VlbldTMnp3a1ZSM3JkMjB3WWNIeE1zT0Q5bTJYcTVjN2k5NFBBTHI0VldXUGtLQnpaaGNvZDdlLytNdHR6cXQ0TVJodDIzL2RVVlpYdGprNVN2R2E0a29oNDlaTUFVdEVuQWFrSjBrRktMbWVJeTNnaUFzblpTSFJmVGVteThQdE82WnZEZmNXQU94MkllYWRhZWI4QStOeVhnOUF4emQxUVEvUm9NVXVlSXh6cnFLUWoyRXYrNWwvOUhqLytrUWVQV0c4czZxWHJQbkptdzh2V1M4OUdxWkNTNlZCa1lHVE5GTkdwNWNLR0NqRzZWV2hpVysxWm9zbmM4UVE4eEhNWXp0QmVjUmwxbGxyRy95Uy9mK3hXRHFJSnU4ZURLcFc0T1RXWjZ5QTl5WXdyV1FXdzRJa1JzTXJuU1lJcmdNMHdNVFRiQlNLV3gxYVBTQTVEUGZMYndNTXFXZjNRU3lsNWtvek1IWGd5amRYOWhJajdkOVRqZHdjV2IzSmxyRUVOemhhNXpLTTJsK3UyY3pydWFOS0hySmp6MDIzUytLM2dJV1Q4KzdMM2syOTRrSUZzY1QrZ0pEU25JS3FiU0pTRTJEbDFlTU8vODNlL3o2cHlzOWo2N0IxdDI2TGFleG1nS0RtalRrSzdoR2l4Wk1pZ0RUZklSU0VuM2w4SCt6dks1MEJPbkRqcHF3Z1dESjJWZDg1L21wTkttWkh5MzJqTHpwOHAvMlpqeUxaejIxbHNNeVBnU1NGS0NtWndIRlVEelY1K0lRUXFZdDQvb3k1anVMbjBkemhHZE5ibjI4REQ2b2ttZlozVWNDL09SbytYVmZVbTkwL2t0S2tjRjhKY1JoS1JMS3VOTW5qUm1POG9YbTRJUmZ2K0dOVDRKbTNlUms3TlQrejlOVEJIbHVLUGJubU9GcXRqMXV0TG9uWlVpdzNmKzN6QjcvNjJVOVpOcDRSUVVWZjFvT1FVeVNiTUZBbEI2SzNrUXdxenlDMlQzS1BDa2crZWFnZmtTOU1CdTBROXRrQ0hEZWtGQ05Sa1VBYUxPTldjdTdhTzdRN0pQTUw4WFVOZUJuRVB5TlIzMmJvdzdpbkpnVXdoZ0dydmZRaVdEeE92eVNrU3ZBaUM1T3d5VzJNU0dUUFQ3QXoxVWRaL3YrNW4xRnZZRUMwM0ZSOGVNYXZxM1orNTFxWnFOdVR5dmc5cys4Uy9iN2ovU1Y3azFqQUxodldOa0w5bnBGK3RCTzFmOHovK2ovNnVQNldRT3MrWVU5ZTErM2NsWjRZTFoyUkJCdlBXb1JJOTE4M2VRQVNaYzJuRlZtK2hwR2VXZ1oyWGNuaGtkand4ZXFTTkkzY0V5Mm9DRWt6R3pCalZScTZkWnVPOTdvNVRaYncxU21WVjArUm1zdUU5SllORlNZOXpMOGx1QjI2Ny9uZlpMeCs4K0NDTUpwQUNJcEpsSlJ0QytjcjkxN0dFN3d2ZXV6dnM0SXU1OWY0OEZkdHJJMWFoMXJOZVgxQkxUNUJMdnZ4OHlSLzhWV0dSTTdPMGJZY0VRN1hPaURpbVpPbU5MSDlMamhBcmN6NTNVZFd0K08wQ28raFVsRlB6RzRZY2F4SXBDU0Ixb3RjcWRjc2tLOFdTNWd5d0ZzYzlZS1ZmSG9NdzIwK0dad1FxS1kwa2tMSTVUZUprMjhjS1F1OWloNVIzQm96b295eTZoVEFkLzk0aGZSQ3dMR3RzOTZXWURUOVl3b2VCUldaRWNKRXg5RkRrOEhGcE9qOGNmbGxCTE51bHpldHBLZWE1MTdKemtCZ2V3TE81Sk9rcmZ1OTN2b2ZrbE1iZHh1WHpSVjJQWjBkWkR5Um42N1JCcVRmT3Q5KzdmZEFlT3Z5bnJPajJJZTVRUWpuSE0wd2hsdzZLcEQ2aFNlZ0htZHpUYUhzQlFjczUyWFNpUVhaWDU1QXA5VkFvVUJTTEZVbUVWcDNpdDcyTEZrYVZ2ZHQyVlZJbVlUd2tKcitYd2d3ZkdsemhlcGdUM1VId2gzS2p1MDA3VVVaZjlBSFJ0NUJiczZhMnlGNkQ3ZnZldlgxNHVLK2QvSFpnQklGT2NvWlBxeno0UUpRZ1N0MEFxZVZrcWZ6bWIzekN5UUxFak11ejF4eXRUZ2paLzZENEZlamczMStRMkdXOE1iRmhVWDVtMlRzcnpkd3FQbDJiUEJmNXFXbEFTUDRsdDhQSUJadXZjNWZNbzZGTXVOeVFiYnFlNnRrbHM1Z3BydWRTbnlWdnlQS3laSTM0eEgrcXVBUFE0WFhGK3doSlk2YlcyVjA2aXpSRlZCR3ByakNKQ2V5aFF1OTMvWGRoaW52Vm9RdlhQZmdZSFFveVR0Z1lBREMvZDJiTWYzQ0hnVzhmK0Z3b2hKR0tpZVhJTzRUMXhSbVZYYkpzTnZ5bDMxMFJFaXdyNFVYYlVSMUh3SjFiaW5sb3F0dVk2aWgybER2YmZSaW85THgvVTRlcHE3YThhaUtac2tsSzJ4dGRIK2o3NE9HUDVybk0xUW0ySnpYSUIwYktYbldxby9hOXlPRCszdHgrd2Qwc1lVZ05teDQ2WGJpYlNLakJLamNYa21YelFjY0JvNWx5ZENVUy9YQkl2QXZLM0VMcGZic3hpKzdaTWU2QlVEdXpVVXJVQkRSNXNudkxLelRkVE82WWtHenEzZ3BqVW9IeDFCMlJQOFl3MUpmYXB4eDZiUGd3cDNmMlBjY2pxRkp5TzY1WngySmxIQzFidnZONWxyM1h5dEhSMGZCa2pNN09wK1NIdzhoQnVST25EeXAvNUxHTm5zTEZ5VUszOXNaV1JwZEpuVzh0R1pURXIzZGRUNUtPVGVwSkV0aW9FT3NsbmNGbTQ4amQ0LzkrZXc3djNzRzdNN2k0N0duYm5yNVBkRjNueVFZWisybG14QkNvcWtDc0t4OW5ERWlNMUhVRkZaeWYxMVN4b3U4aFZqVjlUaGRXMXpXbWFSeGY4WkdYQ3JDWkI5NDJ2SS8xSDAxNDgvZFprTUY5R1NZSS90RCs1ZzhDb21CenUvYklOc0xVLzF4a1R0Rm5BUW52RVQ0VWF4YlFTWmJhSEFJWkFuVmNFTFRqdDM3d25DaFE1VFJNMFdvMm13MkwxZEZnUno0MGhodTVuTXFjMGsrZktTN0dTYjJBUFRFanQwTGZtcGMwcW9TTkNoMFZuUzdZck9IdEdXeGErUE9mdFB5ci8vYlArYk0vL3prdlhsK3kyUWk5TFVtOVlCcG1ic3VsYUdXQnZ1KzlvR0h3dWZFY2Z3WXhFT0lSblQ1aHM0bFpyQm5yb3BtbEhNcGNiODNGYUdQZUIrOTcvWGZmTno5d0R2cWlEdzBVaExuRlMyZHQzcFNQc1FBV3N0T0QvN1NUN21ldkRYQ1UrVldkZXBlYTRzeHNzbFBXOHc3ak9OVHRheHE3cTUzODRQc1BkY2tDWXNuSERuZ0dhYVBickZuVUxYLzR0LzhhSjB2b052RHV6VHVPam81WXJaWkRTZDVpRHBQWlBJM21NWk1TR3VyZlN5eFZ0bkJsTjlMOU9oRzNpbmc4dHBLTElRQjlnblVyZExyQUVteFNRMnVCMTJkT3FmK2YvOFZML3JQLzdMOGk2UkZJaFlaVE9qdjJQb2NJRXBGUXc4UzVhUjQzcmxDQkRoUnR6S0FhMUwzWDJoU1J1Q0lHcjRrWHFKQmNTWFI2VUF6dXdNWk0zWDhkaC9qZzYzOHR6QStmRDVvMitUYVVvVURKblZWT1VWZXdXRTRRa2FncXlkVkp3NDBSK2JwMzdvUEhQS2x2K3Y3QmlDVTR5Mml1YWg2cWlJaHl2S3pSYnMxM3YrT1VxS21nYVJva1ZyUWJKVFpqcXVIUzd2NzM3WCsvZDNoNlFkajJFQ3l5dk9XeVNVV21icE1ydXk1N043ZXRlK0hydC9CUC8vbEwvdkUvL3FkY1hLNlE4Q1ZKYXdnTmdRb05oa25LYlFVMHVVT0xUQWpSZGlyRVVaY3orcFA3UWVDSFJCVnJoTXBOZDdKSEJJVDNhaE83MWY0N29QTW9GejRZZ2tOUnBNMi9UemRhOFNJU0VkR3MveG5DQXJmdW55bHpIcGxOdXN2QmRIL1lkcldkS0lBR2pYUXhtVmlPMWQ0UXBPUGtwT2I1VTEvc3JtUHdCSlE0SXJka3RYVFJuay90N2hOZEc0TnkwMFk3a2JrS2ZYUnFLUXBTZ0xKd0ladjBnTDZIZFovb2tuRys3cEZteWRzTCtKZi96U1gvK1AveXovbnBpNTQzbDZlc2pyL0VPS0pyMDRCd0pnR2pkcmRWbFp6SmxlSGxJY3ozMVB3Z0tsZXljakZMY1dvRzJ2bnpzWEF5RUVPRmJwdk9iaGhsOHBqaTdNSDlaN2tjMDhUc2ZPKzg2QStCVE9JdzJMNnZiWE5QMHZucE03OU1tdldDZTE1eHd6WHFyckZPYUdwSjZSM2YrL0laR1pkWnQzMDJGeWxWbUI0YTVkUG0zNmZ2TXR0UmE2aGRUZHpjaE9hOWxPQUt0azNYc2VrVHZVVm9sbHowOEUvKzJkZjhuLy9UZjg3cjh5TmVuVFVjblh6SnBUYXNONzM3aWRjckpBVFBNQ05DckdMMm04aWFiZld4RnoyRW1iODNoS2tPSjJZQ3JhNklBcVFhYmZRaFFJaUNadjk3SnpEZmxzUWcreE5RM0RzditrTWhVNUYzdGgwcTFBemRZUkRMWnBzSEJkeUc3Yi9PSGZZeEQ0bUhmcjhCMFZ3MHNUQ1crUkZhVE0vNDRROS9SQlRIZ2ZXNnBTSVFveURpbFdNc2tWM0ZaU2p4TXpxb2xIY2M3bTh4SEJVWmZ2QlJFQm5TSTJYcGdTNFpYVkl2SENrMUw5L0NONi9oLy9oLyttZWNkYWUwZHNyeXlhZWNiU0NwVUMrT1BYaEdJcW4zZHdUUEYwVktpU2llMWRWZDBoMHBQZXJMWFYvVDFIRUszMWNpTVh2YW1lZmRSMTN4SmpsbmVyRVU5TVpnZTVJNUFwV2drN3VRdDhmYWYxT0hzUWVoNEhjRkVSSExJelN6bVpQTC9MN3RZTU95ZWZ5YjIxL25keFFGMjdUY3JPK3lhU3FFNmU5MzZ2OEhkMlNZY1N6azVJUERENHBnQk9sWk5oMC8rUFZQcUlPejRjbU1wdllLb0YyM0ljWUZvMG5MUEo5WERoMUpBMy9BY0wzczZBU2pyV3Y0ZDA2WUtlUDlsbU0xTUhPUHRCNTZGWlNhSlBEMUsvamYveC8rNzd4Ykg5SGFDVlkvNWQxWngrcjRLYWlRektoRFRSVnFUNFdzaWtRUVNYazVjeUJTTE5GamhmdklsSDVMQ3o3bWRCb1JQMGpKek91NXpFTElLYjlzRW1CVHFyTitCSlI4ZC8vNS9nNXpmZ3VZVVBCQkU3M2QyTFd2dTRhRnlYVmlMZHV5ODdzbVBWRkFqR0JpNGk2WFF4UlBqZzRJT1NhODlMSEk3a0Y4UVpKSlh2U3NZc201b3IzeTRxNXQ5anE0TC9XK3pmT0h0TTYyZFJpTlo1SE9GamZpR0dRQ1Z2azY5aWtSckNkSWgvU3YrY0d2MTI0ZVMxQXRBaXBlZmpuSUlyTzRNbVk5OVE0NE41QTVBZS9BWEo0ZHgraDUyNlpPWGFJSnpheXhrWkJZdWIxN0ExMXY5RnF4a2NEcmMvalAvMTgvNWFjdmhUNCtSemttMUF1aUpUcDF6N3dRUEU5QWI2M25LQTlaaHlCZTdzckhQdko0Ym9ISjluWXpaR0t4RjVIUmJPOE1PcFN1RHdNTDJYL0E4TVRwTnE3VEpQYjdxdVA5ZmV5ZndoMnBDWkxMMURnSHBhQ0oyRVJNSnJ0b3Y1L3d3OEcrTmdmSEI1eUtEN1d6UWhpQ0ZlSWtDVjVtMTlsWFZXTDB5YjBlaWlmY29aeGExMUhuRDNsOVJybU5ISmM5Wmx2eFE5SVRaa1JSUkRZY3IzeUR4Z2lkZGprSFBSTnF0L3VPN1Fva1YvWlpiS2VkMGtZcURuWld0T2VCUGdYYUh2NzhaL0JmL05OL2hmS0V4REgxNGdrdlhwOXhjdktFRUFKTjB4QkZpTUhqNVNRay94VEx0ZXhHMFd6cTlGU3lBNVhQcVRaODVodzEyUGkyMmNCdG9uQzc3RCtQdlQvS1BZTytKSXlIYldUOHQ1a2Q5a1cvT1pJL0RNdFNWV05PcVZDb2VNamxiOHBCa09XajZmZ1BPV1hzYzdpNERZd3k2UHQ5ZnJoN0VEVzJ0RjE3bG1Xd1YrKzVGZ0ljSFRzeTE4SGxTbW4ycjYyL2EyditCdGZWTFhQbExNMVNuS1FpTmxLUjVSVThyaHE2QkYxMmllMXhPL2YvNTUvL01WMi9vRzByNnNVeGx4ZUp6ei85THVmbmw3NGZlay9XSUlERS9EN0pWQ3E0RjZRUCt6RHgrRkR3dnZhUGMzdTd2NVUyUGxoYVNDbXE4OXlSR09PQXdGTUhneElOZEpOVDc2Rmw0L3UyOVg2ZUQzdUpVSWx4cnVwQVhZK1UyaDJDY3FIN2lTNWpIN1hlZDNqdTNoZkcrNFd0YStYZmtIcW4zb1pUOVJjdjRWLzhmLytjcnF0cHFsUDZMbERGRlp0MVI1UUtMY2h0UHBaZzdxMUhwdUNTaTFKK2FFUytDaDU3L1E5eHhWT081Y1BuZmVVUWN0Nk1NOWczQ1I5YUNYWWJzSzIvNjZCa29Bb3lYZUNjem1xaTNCSlRoTVRSY2psSUxyM3E1Q0RkWnMvSHRFemdsSHRha0c4YnNRdGxWd283WC81c2h1azZpdkFZMEtvSGVmenNxNVlYcjNyV1hZVkpRNGhMaEVoS1JwU0tSZDFrMDFjZ0VyUFcrbmFzOGk4eWpBNDRmdWp0dTJha1hTWGIrd0l6TTluendtM2tITTFtSTl1QnhabVd2TEFwa3BWREgvT3AvcmhRNUhITmNuSGlrK2RQQnllVXZ1K3BRazU5WEtaWXdSTXBqSzNzV0NyMnZVbXlBMHZSWHBkbkpWQUNUOHhjUTUyeTExcUpCdXNVL3Z4bjcrajBoT1h5Q1NhUlRkZXhYSjBRdXA2cThtQWhFU0htekMzRlY5eDFhTUZkcUMyd3p4SnlLeFBqdDRRUTNCUkVKT3RnY2tYVjZZWEhlYVBhMUI5OW14cDRJTEJURDNLZHFPSzFCS0I3dW5YVnB2dUZoeUdKcE1NK1M1OFlCRk9pR0o4K2Uwb0VZb0N1NjBZZHh3eUJSeCtFUXJuSGF5UHNaZVB6L2FYS3pEYmJMdUpVdk8rVjN0dzF0VlA0a3o5N2hjcUtkVzkwS0hWZHM5bXNBYVZQTFJMbW9ob0ZvWWxaTWVZcHRIOVo0U29kMDBmRm9wZk9URTFnNWZjQ3huZ3c3TEtMODgxNFgrWGF0dzFjd1RaNlhBbHpFOWJ4eVNvSG5rRHFsTlRyeEtrSVNqcW1RL08zUGVjN3JQcldmZU05L280U3IrMEtNbGUyWGJUd3M2L2ZvbUhoV1ZiTWJkc3FQY2NuUzhCSXFSLzBDT1NSdVl0eXpyQnJudHgvWHoyNjdiMXkxZCszQ2E0NmNFY1kvZTFGZGl2dlB2cWdEMUZaTjN1RWd4dG95cnBQNzV2K3ZpMW0vREpROU5rWUp5bW9KZHNabjU2ZXVLY2F6cUxucHdZWFU1dVkydWFVTzl6cWIxaVByRkYzaG13c0tkUnI1eEtFd0thRHMzWHJCZXFiR2lMMDFsTTFrYmJmSUZHOWFLSUloRENVSnBxTzA3bTkrSzFEMHZ2QXJpUFk1SEFXbmVGQzRZUjNFUHhESXNWMHNYVGJEaXNGNGQzSDJCUDE3YkdEWnZobFFPNEN4VDJwdUlzeWZDck5JaWRVTk5CYzhMNU1hOUw5RlBzNjJNZXFGeVdmVi9NTXMvYkt3VnZDU3pldFUvVmtRcStHNW5MUVpvWnFQL3VjdkRVZkhFN1ZIZUYvOFJWdSs1RDY0SGZSSFR6WVNkbDBJMDN1VFpEbmx2bHMvQlR5a3pvWXM1T29RS0lrdjdmWnhybnJHZTV0My80Z2VDaXFjYWREYU9zUk1SMnFpakpCZEk4T013OE56WlE2cFVSVnUvdGxrRkdVTHpuT2ZRMEtjbVlFWlhLVHZ6RFBlVjZmckhtWE1CakNVVWtFaTRNaVQzUDFRc3VIelBsNThoeDc1S2kwVEkxRkpoeFpEaTBkeGx2R0p6NnVVSDY3UTJXUlllN3VTQVRlOS9wZmRkLzJIajVvSm51SWx6MFVISkkxeHMrcGVXZ09NOW45Q21YY2g2VHdEL0YrbDcxSEdiWG8zcmFkK1dLY1VHeURFTnh0dEx4K3g0bnJEaFJjY3BpaWQwUW5tMnhpZHplRDVQYndkMi9QWFVPZXE5VUl3VzNlTWlwYWdlRzd3N2FKVEcvcXVMZ0QzNmIxdjBzL0QxTHdEd0lXaEN1SXNGTVZab3QrRlRzNXRhbC9HOWgwMllvMzN0V0tiNFhGTXVGdWhsL21nVFFEc2t2MlZkOENmMzc3SUF5bDlhRWowL3ZHZDVidlpVMktIYmJZeFVjem0yaEppS2lNeWxDNFBHOUhsMU56eFZrbzRZNmxxV0lDeXhwQ0Y4a21TRzI3Yy9QTENXWDk1K0NIeUMwOTJSNExZY1Rtak9laDkxeUgyTDlNQ2hjWVVER2ZqNk8yZVJzR2U2Z0pLZlB6WnN4Q0k2L1NpTytYdVF2MVpuYWZxZzZlY3AxNnVLYXJBOHhyZ3ltMEc3ZHhTeTRpNk95MkltcE1Td2dPNzVya1BJZDhlTEY3T1A0aXdtMDRLckVSc1F0OHdCa2E1VVU0ckR6WXB0aFQyZndtcm5yNzRMNEh3VU93MXdmYjNzTm1BL3ZkVVlHcnFGZ3BCbFJrOFBKdUN4N3U2WWtZdGp6WURwaVI5aUo5THZ0ak9SZmNWT0ZXU2drVlNtL210RjhUZEYwYWdpSWtlT2tCOTd6ekF5Tk8yUFdNeXA3dDFVcDljaUZvNVlVcjc0RGtIL1A2MytYOVYrSEJyV2JuNFNtazEzZWNkbW9LMjkrM2JkN2dtVWx1MzYrUDQrVDNMYXp6dURaUjF4VHZ3TWcrei9RTWcrN0xOL3VBR0NGUzNCaGxNc3NlTDE5azhBTm15Y2xKY2lVRlZ3YnQ5M2p3eGtGaFZyS1pSdUpRWGpoQjlsTHp0RWx1SDkvSG1jM3NZcE1aa2dsRkwvQnhyT2Rqd3lFL2tkbTFnVHE0TG1TUXdlOHV6ZHcwcFUxT0twQ1Zma1ZEYXFJV1FoQjNQUzMyVFM5TUo4RTgvVUNPckJJaVpxNlVtU3BkSkJpU0poRk13MTdZMGdBWEZtL2lDSElYZU1pRExxcjNROFUxMjBtS2ZyckkxVDVXd1czWXhjbERSUWptbXVnZ0VEVTdsbVN0dXBqbGVubjlrSmZCNys0UXF5QUZncm5wS2cydXFrVnpQVUg4TGR2cUZGekdHODJWd1NRblFSUVFOOE81WXMzYjZLSGtPYVR0UWFUR05PZFFGdkNhWUdGSVhTRmlDRjErbTA1T001MnNuT2QwdDBITDdsOEVaZ2ZCZUdpRzNDN0ExR3F3WDFjeHNMdVR2VFMvLy9aNzZLNktzME9pYVFnQlRlM3dtNnBtWDVIMGNSeDlKbXE3blorbmNDci85cHBVUmZsMnc4bWQxUWNQOTBMUWgrWmloaVNTQWlxMlplZDFLRFhTdlFPam02bG1aZFNjcFhlV0dSbFRFY1haTlFYNmdYM2VIVnZZK3I1OWZadURLcGxmZEtEa3UyMVA1anpnb1o0ekRmbDBQaVptbitMSnRtK2RoOThMeDNOZ0wreDVmclRDN0JmL2hrZjNJT0pEclA5dDJ0am5wM0NZcTVwYU1MenY3MUdMZnZjVGIzdytEQnZvS3JpcFlrTGsvZ3QyMTAxUTdQN2R0SGJXcEtsZ1dYRldzTDlRdDlrOS92NmdUcW5kUnQxakpNdzZSSHBYUW1mdWdLeUpUaWdtTFlFS1paY3lIR0xKOTE5ekt1ZUhpWE1kVmxocW05YVlHd05jUkFRVFJRZXVoRUZzS01PNzhhcUVNZWlvNlBtSDdES1NLZm8wKzZ4NUg5Vmk1aFJMMXBieTV0MUNHL01lWFMvLzNuei8zU3pxOGJwNzhwazUycjRuK1EwL3ZKbU1mUVB3akdENzdyc0pndC85dmU4WEZDTU40Z2E0dkR5cHpqS0kzZG5YdkxDaFU3M0Z4QUZsWkVNbmgraFFlem4vMnlJbGJmQVU5bEdLN1d2YlBnYVNJOGY4YXNnaWdHVkU5N0xEQXpIT0RrbmVWT2ErN09IQ1AwZER6SlFybUNEMm52ZllIcTNsKzl3VHR6a0lEb0diR2NkMkJ1VmFkdWQrL3dnK1U1QUkreEI1KzE0UlllK1JQbW1yVE1MVUsrc3h6ZUFQYVRJc2xMdU1acStzTmFHQTB3Q01ZdW0wbkRKWXpVQXFvTUtzQm12QUhKZE1hMHdiVEN2SThuZVJ2V0dYUmZVNWpKUVhPOU5oczAxbkZyTnRQUG5oVVRUMk9XMlVBaFk4QTBzbTZpU01JRXFFUWNLK001VDY1SVhERytZRDNCbW1PQUxsMUY5NTdpelBZekV0M1JhdngvWC9zRVFDZGcvZ2d1Um1IekNqeXhSdWdpeFQyY01IY0ZXbzRMNWhiWi9nSDVPVGhPZWc4ekRQNERXK0J6TlVBaGtqcXdhLzg4SWVvMTRmdThqQkFqQlBuam5veXlqdFM4bG5tYS92bXNDdStqNytMb1BTeVN6aTgxNCt5Kzl1UWl2OU1CMkp0NWtSaW5rTWRqN0grTytyUDkwbW5zYzQ0TnRFcVdham5VS1l6Q2xqUmM1dDd1UkRjM2NGYmtOSWl1TlI4U0NFQjVIQkh4OVI5ckdOSTB4Q0phVUlYMU9ZZTNtOWovN2VWTFp5bTY5cndHWHkyNUNsdzFLbU9DUDFOdFJ4ZlZoNFJ4NE5pbm85RTh3VXJ3dnFySEJPUm9wWmgxbVBXY2d5UGFDN1laNytXY2F3emFxUFZNSXNaemVsVU1DWTYwWEtRTzFUWnR6ZEFhWWNZZ3VDTmtESWptcWFxNW9XMzNSRmlHaDJSelh4ekszN1BnTnhObDg2MGRFQmlEb2hDTU5jdFNqcTFnZ0NscFdVaFdBOEJGcmZkUDBmQnR4c0tGdUt4SUxzSDRVTUR0ZWZWRE5LWXNHbHZSMDFzTHRiZml5bjcwMmdWTnB3ajY2VWFhOVRGeE1ENlZFQ2dlamhuRG5uZVNsVFU1UklwYVpIa29EUUV0Z1FhQW1TQmlQQ3BCandRRldMSzhUZFpQRHN2VllRclZ4VGNyQ0lUbGo1N01RaVVFV2pDajBwYlRDcnNKd0JKZ3d4N2VON2lnQlFYRjZRckFhVGlUck1uRGt2L3g1MHlnSkNSQ3prVVNvcUhSNlBtQlBWbVdYemF4bTNINUxmQmpmbmZiQ3RGTHcxZ2w4bjlOODBpbXdmUytRc0pGbU9HN1hjRWtiZmNnK1lDS1RVNXR6blE0dGJHM0VmTlg4L2NKc0RKa2FoYTF2cUNORmFRdGlBZFI3NVZRbEpPMmU1dE01VXBrZEUzWXNMcjJoaVppU1N4MThEWWkwVkZ6U3lvVUtKUmNiUHRsSEJTSm84RUNYblBSdm5hanhFeDdFVUpJVVNUREpjS3pxOFRPMHRCNW40c1JNd2MwMTUwYU1zRjdCc2V1aGZzS2hyT3UyQkJzbmVhakFpZUJBL3hNcSsyTjExby9rdGlsUHY3THVIaVZKSlJFS0ZhY0NTMjRhRlJLK0JXSzFRSXBqTk1naUJFS1BYVjUvdDgxc1V5WGdmQkdZN2l0S0RldEtzaUlqSUhSRDhNVTYyYmRkTWw4MlliWjVwQUlsbXJQNFlLZlZ0KzlSM0xjZXJDbTNmVU1Wem12Z2E2OThSRzBpcFI5d1RoaVFWRUJETlpoM0xFcSs0V2NveWcrNEZCSlZhMWpTOHBwWUxORDBoVk9WZ2RKZlNHT05Fc3oyWHQ2OGJ5M2pJaCt3SUlnTnlXYmJzNlhCbUJGTHFuSHBIeitpeVdxeDVjbkxPbTdQL2xtVnpDaEtkQXdpMnk0Skx6TjhwdHI0SmVOR0xpSUQwV2Q3UHlzTWN1R0lhaVZJVHE1aVZrRUpkbmJBeEFWdmlSUjhFRVNjV0VzTDFST3dLZUYvSURjTTIyTGttd1d1Y1kxY2crUHRpVVVhbDJmNzNUVzE3S1dWNWN3K0NxM2t3dzNURUl5V2YvazJlRzEyZ3JuejNkZjIvQ2V6MUY4WVZhZTNsYTJwZThnLys2Qy96RC84SVRwYlFSTkMrVU1JOHJFRXBOaDZLMFVVd3pKeXhML2JrZ0h2SkxTczRXc0Q2c2hRTGFOQmt6djZYaW9TRFFtd3FnOXZBU2MwSFhEUzArVWt0Rk1POHdzelFJbGdRZk1rQ1ZSV29LNGdMK0R0LytDUCsybC8vSFphclRKa0RWQUpFLzdTUUQ0c2NuR0lobTRPMmRhZm0zbklpK2Jtc09FeVdEeGlnN3hnc2kxMFBuY0dyYy9pLy90OTYvc2wvK2NmMG5OSmJuYmxFSlE3eTgvWDdZbHRIY2RXOXMyN2ZZOC9zM2pNcUpiMnE2UHh3K3VBeWVOamVQMWFpWWVhcldTajM5SFBLSXZxemN5WFE0Y20vV3RIMjJJZzl0Q0VnRVpZTElmUWJUbzViUHZ1azRTaEFUZFk0Qjh0ZTZNSjBPMDIxeGpuL3Y3UG4rVThCNjcwZWVOdkJ1N2NYWEc0U1RRM1ZzZ2FCZGR0U1NjVjBubTVybTUxelYxa2htRHNoQWswVDBRU2J5NVlRZXVxNjRzdFBBOUo0S09reStpRWxrNzhwNkJUQlpmU1FDRGdpRzR5c3FQcHZLYzh0NHFXS1E3NTJzWWFMTHFzbDlSeXM5NE5vR0VOQ3BQSDlGZllyeXViSS9UandFTVMxaUVSWEl2ajcxUWJtVGgxQXpFSTFwa2lzRXZhZXM0ZnRtcU5HWFdDbWtMa04zRWhEZm1DUlpsd0hQYjJ0aWFFamhBMlgzVnVTZkRiVThYYkZxQXkyL1dLM0hpbTQ4OE14ZTZ0NTZVQm52Vk5TK2w1NThlWU1VeUhHaHFaNWdraWRreUJDSFpwaG5zWjUzVmF5YmJzTTcrYitndUwrR25LbUZtY3BMRUcwd0xLSlNMOGd0WW1ML2h5QzBSd3RDQ0hReGxKZWFQODhoakFpdHZ2WTU0TXNJM3lTN01OdkNra3hpeVF6VWxBSWZyaTA2eFpWQ0dHQkFSZVg4TTNMUzlxdUpsWmhsa0ZvVk9UT3gzOWJNK3Y3Vk5LWkdaYXRLVUZrVnI3b2cxUHdLUnhramJZU0F0b0JQK2J0TmtaRVZ6NjJGTHVGSFE2aG9rOXJqcVRLaVFaaGJmRDFYM3pEcWxwaUpEUVlKcm11aDRsbm9zNSs1bWdKN2xCRVBTZ241TTJWTkNCVWRBcFZzNkJwQXFoVE5WV2pyajJQL0x4UDgwTm9yOVBOQkxsSE5sVUdwZWdReFRiVURvTVlReTdZVU5PbERkM0ZCV3BHbERpME5ZVWl0cVdVRGkyMXkvbzVEM3pGSm52K1ZUNVhRVUNDaDZhS0Z6Q3NGZ3M2aFhmdjRNMjdDNnBxa2FQY3hpb3B0NUcvN3lPclB5N293S3AvTkFoZXRPZ3dzdGQrTXMxWm91bUcyOVlrVHR1NkNweDlPWHpQWXk3YzFOdklsWWtMcXRRaGZZV3RBN0dIVlFXQml0UUhqL3JNYkxpblBhc1JDUjQvTFlKV2t6NEhwU3BVV0x5S2ljV2FLcnVRWFd5OFF1ZWlxa2k5MHJmTUZGZmI5dkRwYjVNUjVNK3NCOUhnTmVRS1R5NnVDSEgxaDVINkRxUW1DaXdXcmdQb3JFWkpKTE5jdFFSS1BQbHNmUkVzeEZsbDFTbTR0ZC90NEpWQkRJWllnNFZJa2dwRGFDcG9Hcmk0Nk5Ia2VvM05wWEYrZnM1bVkxUjF6ZFFDTS8zY3Jsbi9VSEFkWjN6YlE2YTBXZmk4NlI2N0VzSGZCM3V1c24xQUYwbHlQOVdkSzRKdUFzVlZhWjlDN1c1SWZCdlJaWjhOZWVpWk9BV3RRaVNFaGxvV2d6a29FcW1xaHFvS1dFeloxMFU4MUZOSHBWUEM1ekRhYUN1V2tvQWh1TGtIY1RtM2xOdnQrMFRxdk5oQU10MTdlRjVIdWYxN01YTUlscE1rcWhVaDJQeHdra2pmSjJyejNIQjlqNXZtb2xDSFNCZzRzNnhSSStRd1lTbG5CY2pvMER6cmw4Z2dhMWNwWkdWVDVTVjF4ZVZ4VVpBZXVrMUxXUGgyNy90RXYya1JPYzduZk5HY3h3R3hQMElEelY0b2FHMEVWRWEzNzhLbVZ6cTU4VEZBOTV4RzI5OVZFeUZtVGJoNFhlODZCSHJOYkY2ZTdOVDdyaDVPcTV5UmM5d0paVFRaWGRLS0ZtYWlrYktKTC9JQnVBdjFucDRkVTkvNW5iRXpibEkxRCsxVTZVamFra2l1V01NdEJTa2xOQWdoMUlUaWNaYmI3YzJRakJ5QkxJTVBsd1dSSmp1SUZKZFNxS1RLU0FpaGlwNnl1RGlIYm9sSEpZQmg3MEUya2RjbEt6TUdHL1pnd25IYk0wQWxUb1Y3eXhyKzJoRklrODBYb2lUMFNGa1ZvNFhSMnI5YVdvcTBBYjFWK1dEckhGRWxFZ1ZTZ0l0Mk0yeVQxTUdidDJzNkxTeTU0SDc3WTVpbHFReml4VlFYVWZRTmxwVWdoL1M0OStYK2J1V2Vhb0twa1lJaElWSldOSWFBdHU5QkJyL1NUSkNkSnZSS2RDdnRITXJpY1owc2JxTjJabmJ0Y1BqcWRTelNkZFRieFkwYkxwSW9LamFQL3g3NlZWd3F5WVVBR1RkVWxuZDE5UFRaWVc5aG5QODBVT25wbW95aTBMNXgzWVJMVVd4SDVCazRIQk5QUEpHbld2TUpGSkFCZWVkTVZWRzJUUSthK2ZkNUIvMndBdWRndkI2cHU4ZU9Kc1hzQjQvUXFWc1UvdVJQZmtycWhSaHJrcFlvdll6UWVsamhkeE96V0xudjN2dm5HaVF2YlZTSU94UmxSNS94QnZmZU80amdoNVFPVjdHY3VXR2ZydEgrdFRPYWJUbGFrQm5WRzlzZmZ5aEo5VWM1YmI5U3hoWHgrekt2MnV6ak9waU8veWF3blR4bSsvbVJyWjJFZXNxb1l3Q3VQT2pNRE5PSjJMSkZNWGRoNUdiTVBBeTE2RG1teUQvTk03Lzl2cHVBK0VtVHpWVzc4MVZTT3NXSmVXbXNPR3E3cUd0Ylk3cGlueXVaM1MvOWtOR0xybHp2a25tQmh4QTlRWmpDbi8zRk55Qkg3QXNYelk3K3dIYlZsTnZGTU54Mi85em4rVzNUM2ZTSkc4bmcrMTUySDBYVWVNS1hZSU5kZHNnWmpUUWJ3UHllcmZZWUt5cGVkZTl0NENHZkh4QjkzMzJ6WjNiUElMUHh1ZTAxY2U3a2NGNjZjcUNFd241T25yZU1tRGVWd1dIM1FKbnVnOE45c0wxSGwwbXVhQ0tqaXNTOXMyendYSnY5dnVkekZDV3ljd3ptTk1aR29jMkloQmpaS0p4ZHdLdFhsNWc4cFV0R2lQdE5yUStoYUgzcy9WZUk0emJ0S3Q1c2FnZml3ZmNON3JhZE5mUHl3SWVmbXdmaEY2bzhyWEc5elg0UG0vSUFrbjhNY0RBWnYyeXRBbVBLb1ZsbTBza2pLaUI1WHVaakxwNW4wL21idjI2Y3VTd0diUjNTdzF6S0dOcTVmZnpzM3dkYnc1cjRISXlJdm51OW4zSXlBL3RzUTIxeFljK24ydjdmSjUrSWpTNmJHZlA5ZTZJa2dPdFJKQXBkRHovN0dzNHZsRDRHUW15dUhlL0hDbGYxYzd4MlFBYS9tWXZjOWZmWURVK0Y2MitUUWQ0OGhPRGZkcGltUlRienBBaGk0d1ozcmRSSWhhZXMvNXp0eit4M2FmZUtRN0ZjbjM0ZXVyNE4yMVQ3RURzNTdlOVU1RFBMcFlzSzkxYmVkK3RQVnlLS0NSbzhDYVNpZy9vbG1iRHBsQmc4R2NYWFg1OGhZWVZwSkZSajFaZnBtTDVOa0xaNG8xRlU5YkY4QUR2NDFUa20zR2tESUovdVczYTBxemJpKy9hOHV4dnNla1ROVFQ5ek9YNUFEbldGRmFvSXBaNzY3b0ZYOXVnZ20yNVIxU21WY3RGbWwrb1dzRzE1ZU92YWREeUhxTisyRXRDRStjdHVWOEp1QjZUVU1Nc1ROMlJzeVRxTGxGeFQzcHY3b2YvOG16ZFUxVEZ0cXJQTDh5RUhxR0t1L1RoaDczNFh5em40UGlpQ2p6Q3loVUtKNWZaWWIzZjYzOTUxdzJZK2tPeitPbXIwTVlMM2RVUUNuZjArejNCcXlmT2c2U0FIbE92Rk8xc0h4R2M0QVBMM2lVZzBZOU4zckJQczNIUE5DSURERkh4SHljaTRPVVhrdGpVcUJ4QVpzK3U2OXJ4QW1RZmZLeWtwZFZPUmFtalg4UE92WHJIcFFHSVJXKzcwK2c4Qyt4VGVBaDV0aDJaTGswMFYzSStJNEZiOEZBK3Y0SlJsSzZhV2dhV2pVSE54ZGxXeTJ5TGJMTis0aVR3Ky9HTkt4ZVJnazQya2VFSThNYzJ1bW1GR3hRYjVrZ2tGVCtaeDMyVkQ1K29lV3BSUjRxNjhMb2NXcXVZZVpwNGtBc2FralE1bFdmYXJ2MjREeGRzcjkzL3I2bzVjYnVCclNIWlIySDdpK2l4aXc3cFR1SXpKdjdQT1FXQW90Z0I0UlpVRVAvMzVDd2pQMlRrOHQwV05pUWp5c2JEdSs4VWlENHdwM0VnUjEyS01kSWVVYk84WGdreDMrRGloaHpmZTlvSjgrMkYzVXcrSDNJN3VxK1FtbVZEWXJXVDg3cmR2WXpiSHd2R1V1dDNpYkw1ZWtWZit6dWFkL0htZFpyMUFrU0ZEN200d0cwcEk3L3VNK0lFL2xEN3lweWN0K3RoTDBjTVFLanJnY2czbkY3QnBEU1htYkMvT09SWkNNaFZkbVA3N0k0RnRzM1U1NkR5TnhYWmZ0eXFiM0IwT080ek1yMThOaDlnN3cyWWIvS29OczhNT1hyTzUvTDdiTCtCTk4vK01iUjFzcmhreEpYalFjcTZ3V2Z5ZW9SZ0hEeUI5Um56M0ZjbXBpL1AzN1g0TkI4UzhWNFB5YWZBYzJ6c0Z1M080ZjdQZmoyTXFlVDgxVStHZHo1eU1xY1RFajg1dmtya1YyL0ZDTm5PYVlja2RmQXduSWE5Zmc2YVFROVNjbzNIdU1WeUwwRHQ3NmhZWlhnN0JYUTZQcTVTWnUyMStSTkZrYVVxUktKUjhURFk0QlZWRjd6SEJkMFhzMjdWL0dLYW5jQkdYcGxMa3lNOFVuK2lTQ3o1SGxNbm85RE45MDAyVmpOUDY2dnZxb04xR28vNVFCTzZnbGx6MmY4NDg2Q1NieE1qY2k3aG12U2dKeTJIeGIvN2s1NFRZWUdHQjVld3VNN0hsR3VSK0tIaUk5Z1lkeGx6SHR2T09ENDdnbG5lcG1XMXo2L3Z1dlpGYjYzdUZuWHhUdTRzM1c5QVNIWkU5eFgzcmxReWZtVW9VVDYrQldoUTVWd2ZaTTN0N3NwTlh2cnlxektPTXlEeTBlV0IvM1lVdDE2M0dSalBkelRieGpjL3BmWXBWSzlWVUJMTXd4STE3ZUxFZmlySEtJYklKL3V2LytzOGhMSEJHdjBiMkZJRFlobytKUmI4cEJDYTU3RDUwWjZhd3p6VjJXMnU0ejZhNzcvdkhva20vMFFZcENxODk0OWl1M2dtRmdsLy8zdHM0S3gyYXczMS9oKzdiOTl4MXNKM1AvT0FuK3o4QlQ0TThLV0ZzT1FoSnpXdVVHeTZEZi9QaUhVaE5ueXduNzZ5djdkKzNIUjZRZ2w4bmkrK0hRZFcveFc3dXUyLzZPZi85ZHFmc1ZLdDlGN2lPRlo0ZlVsdUo0aWo5RmNqcGtzZExnY0oxMmg2SzY1c1hTZ1VSU0pUVXhUdDlHTVpYMmtuemFScFNGQitlMjZ2R040Q01ScmpwdUllS1NnZmEyR1o4ZGhFNzdQNHVUREt2NXI0VUhVMnBpMVpZZUhOUlJGWFJHSGo1Q3BDR1pGNndXU3ptZE5URExCd2M2LzVEK203Ny9lbzJidytPTjZQOFBaWEZIMGpKOXJEZzFib21xVitSc2V6TURmaTVrbG56MndHRHVzaGxjVXR1Q2h2NEtxZEVaamJrR1ROOGpvcDUwRDI1Ump2NDhPVGd3REVKQ043RDV2cGNIWTRvdTdMM1JSTTl2SE5YMFZORXFoMWxuN0Z6N3dnM1ZNekttUC9OMi9OZ0hETWptZEdyVWgvWGJGcjQrVmVYSUF1U1FxeHJMRVVmKzdkbHF4eUFNVlcwZzJTdGV0RTczQW5CdHpWMUFMWS91Y3JPYzl0YTJhUktIU0lwZVo3dWtCZEtSTkNrV01qSjlSSFVzbDEzejZvWU9RcEliRDlWcysyTmVIZnQ3Nnp0YXc2VEVxNDR2dHQ3V3hSQUlZQzJQYUZTWW9WbklXMmc1TmhDM1JuRml2Tkd4cFFBT1RHZ2Z6TkxTR0EwZldXVFdCd2N2eHVtZEcvNGwyVDVkY0M0K1JnUGFtMjljeE90dmpDWW5LeVltdnk2aWhKek1nVXM0TG5qY3R1NFhkODVDY2JQckd2WStjeEhScm5QWXUxanovcDJyd01SNlUyaGpsZ0ZGK2Z3NnUwNW04NXR4WDFLMUZGSWxqejkxM2Jtenh2QjNmZlBmYW0zNUgxdTJZcVNVazlzM0VsM21sazZjVWM3K0VPeUdOdSt5a1hoRnBHdDNHdTNVQmZzU3hnOWdmdjAvMkZsZTZWTExYVWxnNSs5U2pZRlNTVDE1c2tSQ3RJQlE5Skl5d0hWRWtaeHd6VDdZdVBJQm9pa25UN1A5Qm82b012ZUhsNDEzaUhpUzl4emJoQWg4dGlBSWYreG44c3grOWRIUkJTSk9SVzJEY2RMc2FXUU5hOTdQc2Y3VkJRL1A1VXE4elp1SUJHQ1ZCQ0ZMam03M25YR1pxMDBxeFdwZDRjUVZTRlcrd25HWThKRDQwL3B2MWsydVpJVjFtSENvdC8ycGFNbldhRVEyWU5jeW9GK3VLMWRZLzJ1NDBxUnJmZjlYamJTZlZqeG5mN2ZFZTZ6VUVXaExnSDZWa2w0NXBNMmdWRm55dTFKRE1CdHdvVmxEK2Jwamd2ckc2VE9Dcm1FV0VCa2t2bUdndVJsclB1eXpzN25vcmlCWGszYy9DQ1ZRbnBMRW5OR0g1czZWcDRpS3FrblhveTV4bnNTU0Y2Sll6OHRMSDNmL2h3aEVMQmNuelNBMjcxeEgzVHRJMzF5VjROdUEyOWVyUkZjZzI3NU1BcnhabXQzYUs4Yzl0Ty9may9kZi8vNWN6RkUvSXdzT0RIdjE0eUMzd1hKcjdwMm5SWjNXL002cGVEQXdPNU43MzlJRGZsamF0cXYwdlJQNXlYR2lHWFBLa0wwQk1UcWY0dFlaN2JXM1A5Y2JDS2l6RU5HWlZBd2Vib3F5KzdvSVRxRmw4aVFtVFpSM0YwaEhsRFNIUnJIdlA5ZTZGQWs1elRMS1p2OUhyK3Y3eE1pZ2JxT2lFQktSa29KRWFoaVRkZDdIYlpSaHpEMXI0ZXhxSjd1dWMrVE5FZHgraUlpUkNwU0NGU1ZJRFdzRmRhWDhKTy8rSnE2T3FGcmpSQ3JuWFc0aWUvQ1E4TjkyeXdLMkt2NnZzT2kzNWQ5a0MxS2Z1VzlreE5zUVBKcjJLWHQreitVa3VRNkR1VzYrd2MvZS9VeDFMRkNjMHJqcGdMVjN0UHdBS1NwYk8yZVY3MjYzcUVxbkEwanExNjh2c3dNa3N2cVFpQUVJWkZjRXozeTlmZ3I1blBwUXhqajg0TnRYU3VJbkt0M0VzYkRlV1FiL1QxcWhpbUVJSjRiRGZFU1Ixbzg4V1RyN3hDVWU4TndYejNrSVF0b1NxZ2xrcXBIaXhuUVFOdkQ2OWRucUR3QmljUlFvMnJabGZWdWx1SVBiUjhmOTlqYzRXbmJyUHlvV3ZTYkhoYUhUckx5YzlFSS8rTEZnd2Y2RGtLRUVDcFNiMnhhV0ZRZ01kQnJONWlMb0hodmVmSEFNZ2NwTzc5Z09UTks1dGxWOHdKSHBSS3ZJUlpDOFQrZm1KTVlOZHhYY1IzYjROZkNRTFZkQ2VpSGtHWWRpS2tRS2cvWFRLa241VVNIeVF4VllkTjMyYWQ4SDVPK2pYaDc3aEZqazd4MmVpV1Y2MjdFN1RBRXNPaml6cnFGV0MvcFdpTTJOWDMyTzloRzdobHlIQno1eHdFejBUWWYvbk1yUms1NCtXZ2RLSlM4MElJcm5GZkt5ZS9zYUdITjV4UzZPREFVSndaSCtvOHZjdXdRN05NbENFSVZGa1RyWFNaRjZIdG9ONDdnaHFBNlVpdWZqeXlMRnNvYXFpejN1c25JOHB4NDlWRXZocEFzZTN1a1JBalp5akRwMnp6WDNmNitGMXRBR0E3ZHNqQTVza3RjN3RlUUM2cVpZR0treml1b1NoWHArNDYrNjV6QmxvWVlteXcwM21RZDkxTmFpYzF3UkpUOWt5U1ExT1h2VFFjLytla0xxbnBCU0RWcWdscnVVN2hOK3UyYncwUG9kbTc2bnQzZnhuMFd3a2RtQjcrS1JmZXNvaC83dWVwd0U0Vk0rYjVxbG1pL29XdEI0b3Jlbk9KWVdCRmlJS1NzVmJkTWJZVWhsN3ppR21MTkhMY3J4QU5CWEJLdkpYZzRhbEpTbDRCSmFLNkFYWU5YMTFMd2pDd1RSYTY3ZjRwQ0NBUERuUlRhYm9PaVZIWEZvbTVRYzNGRWdtQVNENnJVcnZ1Y2R0Rndic2g4eUJBOVRmSWYvOW5QdVZ4M2hHcEoxeG14aVVpTTlIMDdWRm45MEN6M0liaU9DNTZaSlFmUVhVZVhEemxBRWZFU3MrSTVudmRSK1BKdjFWeXhvMmdobVdza1JYSnM3QWYwd3IycEZoVmd2YjVFdGVOb3NhSlg0ZndTdWdpcEMxam5vbTNSam5wbVdXYlZOcWZ1TFZtc0o0cW5FVzZCcHl0WXJRTG43NHoxeFRtcm8wVkdiQys2TUtCaGZrbUpPYzh0N3NRR1RLMzZVd2NMbDhzVFNUMXN5SmN3VUZkQzZvMCtLYXZqSlJZRG13NDJyVC9YQzFjbVZUejBXWEphRk0rL0tXanV0Z0dYTGJ4NDA2Rmg0VFhDcTVxKzd3bEFyQm9vYVpVbmlEUnlTbmVEaHlSRVUyS3huMktYTkY3dWt6K3RENjZxZHplVDNSUWVwdDM5d2ZsUUptRDNpYm01N2YzQnJkOFhJOHY2aEs1citYLzhrMy9Odi95WGIrbmJsd1NMTkhGSjZwMzlUbDdEeFAzUTNac0RSSFBoUFBId1R6emVMR2lIYUV2Tk9mL2V2L05YK1h0LytEdDgrbW5rSjJ0anZWNVRWd3RpSlFPM1ArM3pWSWtKbzhwcm54VWc0VEt2VTlRc0hBek9aWTY1ZmUrdGhGZ2pJWEIrYWZ5Ly8vbS81ci84WjMvTTJTWkFiTkRnT2RVY2NTMEhqY2cxaUo3VFB4VTN6ZEx2b2xzUVJTM1FOTS80ODUrY0kvRjdvRXVJSVp2cm9POWJxcmhiRzYwUWpydXcyby9GOGgvNmZWdlp0cTJrM0RHVFhkWGduVG8zRklDWWozeGJnejd2aDJ0WnQ3bDE5KzNJM213V0JsbHkyd2QzK3ozYmNOTkZ1SStDOEtidjczcVFPbUoyd3NzM1o3dzlXeUI4UXJBYTZ3R3JTQmhLQXVtQm9zVHkrWFBsa29CbHJnWWpzcUd4YzZKY1VqZlBxZXBDNVJMTHBpR1owbllicWxoblNwZlhnUktvQVdYeWgraTlNcitabXhpUjNEa3ZDZG5Mem5LZU0vV0YxNVM1RDNQVFg0akN4UVg4OForL0kvRUpHNnRSYVhZUStIb0VMM05adE9xNUdDTytMN1RNRlFtenoybXFaNng3SWNhUXZRU0Z1Z21qd2Y3QXV0M1hmUFlRKzIrL2o4alkxcWhGVjZhK0lRZTE2SWRzdG84SkJ3ZHR1OVI3T21BcnVkeTQyNG03RHg0Q3NhOXJ2enhmMTlGTE50VXIwSXBPYTZJa2VvMGVjR0lSRXlYUkEvMjR5SWhyeWlYenVMaXlMWWdSN0pJa0MyTG9rTmd3QkUxSmo4aUNJSUcrbjJma0hPZDFOLy85Zk9Cai8wdGZOTHVlWW9WbHRvRnZGc2tXTkhWcXJnRHhoSzVmWXZFSlBTY1lTd1o3YnFIQVpaNjJYR2kzN3lPVmhCZVRQUEVZUnAvZkg1RzRvT3Nya0lCa2YrRGlHSExYM2YyWW5PRk5pRlRwdzdiT2F2dGdlbStsaXc3Wnh3czdCSWVWYkRzeStRRjRYK3o0VGQ1ell6dTVKRkxxczZJcG9OUWtxMTFaVmpWb2wzTGdnT0J4ekU2eHZKYVdvWkl3Sm1tT0xBSVZ3YndJWDV0YWtzR3lBWW13NlhxQ1ZNVFk1T1FJUTVwSFJqYnZLZ2dEQlRkVFRHd1NkR0tFSEJFakJIZHMwMUg1VlF3ZkZoWVFuOUN6OHZGbWM5dklMT1E5Zyt6RlFFZndZaG9hWmhYblh3cEI4SzJka2xGUkF3RVhUN1B0VzRUVUo2bzdWSlcrNy9vL0xPeHFTZzk2c24wSWNHV0E3U1JMOU5ONEVqSzVEdzVrVi8wMmdvdVRnUkFXV2Vub2lpbUpyZ2dTcWZEd1VwZkpvMFNTSkNSVWpCRjRnYUNDYUlYUVlCSTRQamx5VjlnOHZZUE93bHdHMTR6a1Y1bkl0bjRZYmJCaVhzblRmQVNlZ2NmdDdDR0h2c1lLdWxaUjZhbENSZGREMXl1ZFJucHBzTkJnVWxGVWRqUDVkM3VPQnU2RkVjR0hxK3JjdGxWTVZZOU43Y2p0VlhTTVhyTnJhNmlJc2JEMjd3c1pId2VLYWN6L1BaYndnaHNnK0VQTDVWUDcrTFRKcVNmYjZFTzllNTFzQzkvbmgzNVRGdjI2K3g1TDRiajMvU2xRaHdXaUh0MmtsaHhoWTAzZFZQUmR5UkxxS3VPUWxWYytUeFVwbCtJY3M4QUlKZ0UxOXhYWXJGdENXRkhGb3IrSUJQRVlzellabGN3Vmw3c0lQVDlrYzlpSDArc2gwMnNnbUJjeW4rVlp0MXhKR0IrVENNUWFpSUhnL3dDcmlIak5jMkRnNE1vK09MVC9iQ0FJemhZVTV4b1hhWElMNHA1NG1sdytqMEdjd3dBczVYa043eCs1SDNML0ZTdlRnTlF5UDdCdVRNRWZhdFBQMjNINWVYUmkyVGZvSWxmdFVtdk5iT1hkSmFtN3dVUEkrdk0yZ3BkUXJnSlZYSkpTaDFKOHRqTmx5K21GSE1VZDJSRUlWSmg0V0sxVEtnL0NjRTJWVUZVVlZVVjJEVldLbUtySlhVZE5kVlN5N1VIeWZjTWNrUjA4UXF6SXl1UEIyMXVPQ0F3UXE0cE9OL1M5TzVXS2lOY1Q3d1VKRVMvL0hJWjlNVmVhanZNMWd1SWxmeTByV3lOU3dwYUwvVjJ5cjdvS1ZSV2RldmNkb0ZSVmsydUk3eEtTMjYvZCs0T3A3c01vT29mczVFUnlUaW9meUtISTRHRXIrbWVlTkdDL3d1VTJpTDU5TUF3YjFoaXMyRmdnaEt3TkZtYzVWQlVKdFZPR2xGUERhcWJleUNUbGI4Z3NTaDU4anJZYUp1Qmd6clRER1RudWU1RGQ5SGxmcUJ6ZkhGMVpwUWxYQnVIb1BQcWNPV0tiU0xaTk8wdHFRN0JJekZsUEZNMlowclJrTEZXbmM3NmVLWi95TnRxNFN6cmwwZUVnZitSRGc5MjFIM3BsT3QweFdjM3R5T28rOFVxWE9xQkNUQWc2OFdzUE9mcE5xZ0dwVGJ3NlNWay8wOUVWMWw4eEpyQXduNnA4Yis3ZnNGOHpDeDh6QjZFUWdnZmlxRnJtTmhqSHZMVXU5NEhIZW42bS9NNkh1ZDhiTWZXNjZDRnplVEg3TU55b3V1aDlPbitkc21rNG9kVTNSbkZiUGZUKzJmT2l3OGtrdzk2OC9tVGRaeS9jN2RjTmxXUjNlSDRYZEo0M1lpYzV4ZkF0bjRkamNZaDhrN09laFhjV0wzWGtqSThVWXM4Z3AySUl1NWx2cnJLSGIvOCttbXlFd0ZqZnZKaHN6SXJ5cmVoVFpPemVGaEdaL3R1R2Y4K3ZqNHExL0Y3SnlyemhBQzhGQ3dxTGVtaityMStYeDE3L3V6NS95THBWOXJPSUoxdU0rYkFNS05YdzBBMDI1R1BLcGRzMXJNcWJKQzlrK1gzYWx5RGpWcGw1WDgxKzJ6WHozQVFHVnVpT3JOaDluejhFTzJzd2tUZnpEZm5EOHQ4VXdSbTRHMnp1bWJDTDBPRWdrZytLdWtKbGJhU3dsc1dtN1oyeXZTSDN0VGw4WnRaNVpLRnpwTnowQU1oamxudHlZSWRXNTBPdi84MmZ0NnpmTU9MRWFVZXlNOVNOMWRDUHJXbDBlWE5PclhaT3JIdzZtNDJKQ0I1YUNUaUYreUxuKzdDVjdodjN0aXk3ZmN1MlA4RisyZnN3RnpVOVRNMXN6R2t2ekpSeTJ6NExPMmZUbHB6dFgrYVJVVHRqekhIeGgwU3JoOXdISDNyOWIvcjhNTWZCUEJ4WVJnN2dRWlZzaDFpSTY4RE5RbW5ZQkdaR0dJb2VlRXBjZ3Q2dC9TRzk1L3RYaWp3MkZBcTNEeGxHRm5vY2Q2bG9CRmxwSnlVNVg0bmNtendqNHljd0MweHhyc29iR3BPaWVQS0hVUmN5UXVFYTl1MVh1Vk0rdEVuYjkzcjYyd3NEaFJkY2x6SFJub3U0cUlKTUtQak5aZGZiWDdzSmVHSEJFYnl2UmJGU3FJbG45YkRzaHozZUsxdlBmcnpSUVk4RlY0MDVYY0dLYjFQdGZkY1B5ZUhUZjJ2V2FLZk1VcHN4VVBlZDU3TnVZTzRsK3ZnVStSY094R2FFVjhTVGVBN0sxTzNTUlVWbXZZbUM0S0VtdnJRM1JmRFpoaGpZc1V6SlpYc1QzbDFaOW9zS1U3M0ZGSXJpeTB5eXRZR0JVcGNJczNGdW1YMGVvcFVKUDJ0TnlqMUdFc3M1VjF4V1Y3eTJ1ZGxoaWx1S29HWlJlN2Z2djN6TGVDMlV3emZtM0FFaEJQK0xBcVlJdGk4bjI0Z1krOHhrdDBXWXE1NlphazMzY2dCU0tMYWZSc1BQdjZ4ODJiMGhaSVZZOFVQZlhkK3JLUHcrY0hOWXNZSUxrZEhrbGlaS3prUHR6STdvaVpaOGJxUE8rZGgrdGZCek1IY1hIcTBHaW1RS1hwVFdNd1RmTnZ3ZjBuVE8zbkZvNFE2YVYrYi9kbnQzR0NpNFdVNHRsSHBFS2l4SENZa2xnaGlXZWtyZTh4QUNxYmZoWU5wKzc3ZVplaDg4RlBQblRVU2l1ZWFheWI5bE1HVzUwY3BtLzhHb0I5LzNsbEhUellEWUlpTWlENFJoSXRPWGNrdjd5cmVydUdQR1hDazRUY0I0TzdpTnJ1amJDRFA5aW5oZXZ4QThCWlhqUm5MZFNPcnZuaFhob1NZcDVMUTUyMll5L3l5eWRxRUVjOWticG5iUVh4eDRqSU5wYUhKTHl6MU4xM1FiNnIxRCtYVzB6VnZPU21IbXRkWDI5dU9hOXNZSGJwK1c2ekYxUlI4cmJGdE9WSjN6M2F0RnYra0dlNGpKY204bEhSQjhKdU1idVVxaURsckJFY2wxS0dtMHEwV2VubHRqTnRKOTc3NUtoajhFRDdWSnR1ZjVmanFEYVc2WFBVZzFNeXRhcnZFbGsvdjlyMUQxNjVCa29OSVdtRWFrVFpoeTVvSGI0NXBjNTZrMWNua0dFdSt5UlBNK1hnRjNtZS9IV3YrYnZIZWZRaG5JeWpXSUdFR01wQjBpZG5jSy9sQWdJaktsNENJeUNlYWZ5dDV6U2g0bXN0cGM4WGY5a0Q2MGR2YlEreCtIY2svYVBCQmJ2MDNCOThFK0tqLzhaYmxiaStLT3cxcjRXWGR1akNqWjMvcVdtcmFQbFZyZmRmOGRJZ2hUdTdjRUk4YVNBdTBEaG91YWxZQjdOVVJSVlZOVmlWRWdLSmhOL1lvbHB4ZWN0Ykd0TS9nVlhBK0Q1YUVFOEJoa205WWdxTytuNHZ2bCtWRXBXK3pnK1h2K05LeDRJTjhTeG15eWp3SGZadjBNRlBuYi80b25XK0ZzUXpUUzVkcVRmeHg2K0xGT3YrMjJ5eW1VVWlMbFdOMFJKamJ2UElCY1p4UFlWaUk1Uy9odFg3akhnbWt5eGIxVStBbzUvQ2IzRGQ1c1E0MHlHV3VxemU2L2dyTCtBc1gzdnk4b2N6dFNjdi9ydWcxR2R6VS8reGlJdm8yQXBicEVTb20rN3lmMzdBYmlIOUtRMzdhZjl4M1RmUStSOThrNnpnNVRZUytDM3VXdlBLdnN0algvdDB3VWUvTytIWjdIai91US9yalczMFhYV0huR212S1pkQU42UU1tMnIwT1BTUmtEUW10S3lybXlQRlBQVnIxcmVrS09lUzA5dVYrZnZqMUZFNjREMTFsTXgxUG1aVVFzNThSemtJZ1dWOVBETXZnaFRmcU9DNndVaWcxaENEb1p6WERiNWt0LzdhOG85VU5Cb2R4Um9JNkJHSHBpTUV3OVZlZUE0S05MOFA3NDhQc2d1Uy91YUo0cDdhZ21qK1lXTXpPbDE0NjJWeFoxalFESmVpaVJVdG9TcGFmQ2lCUEZlTkdFbTVXRUJvSklaRmFYZTBpbTdlT1RhY0NDM1Q0cDE0ZmtBR3pyMHh2TVk1bkJIaVRLSWJtUy9md0hwQk5jVVRhbHhtTm45NzQvVEZueVVEem5iSGlQNVF5blhtUE5JSlpjYnFOcGRPanBkSG5JMUQ5bmFIbU1oQ3NmbWdMdkJNL0FZUVhpbm53R0JaK0NDS2FKRUFXeGppZ2dkRlJCaVJqcHBoUjhwMU8zZ1BHNXVTbXJzRytTM1JoRmpKU1NpVWd1ZmUyT0xMMTZoZ0FSOWM2alhnRjdraHNjaXdPaWw0UVNEdnU5bi95UW1maTUzMEk3K3hEczFYMDVvc045bVB5K1oweiswMWdKVkl0U0xBZUM3Sk8vOThHNGZxNElNOFVUTDJRWFdIODQ3TXo5ZG1qdk5tSlAyeTloc0k4Qjl5ZFc3L2Y5VjkxZi9NNmJ1czdWYkpRZ0NiV2VFSy9Rb3NlU0l1REJXSFBOSFkwN3NuU2h0VzNycFdRMEtXSkdESUV1QjVnSVBVaVBoQTdCVSs5NHJlZjljdmwxVUxLU1d1RmY3d2kzOGU3YnZ1Y21kdHE3d0ZVSU90VncrL2V4T3N6MG1WSnRkRHNzZDJ5b1dEQzhPb3BtTDlQcFpyUnNhL2Y0ODFGVUFKeWJ1T2ZXT3VUSDhEN24vOTdyUHpCZWh4U1BVRGpVZmYwTmJzWmdVVmVFcUlRZ2lDbmFkelR4cnI2QXQ0QkRTaHBWUlpVSndnZmE5WVk2eG9uOGxvMzRLQklVckNXRUZtTkRvQytqbjlnVml4aVE3ZWNING9iSHZoVnowWWVCeDFHMmFmNy8vbkdQN0hpNWUxZnVudDIvUjRHNVg1bEczcXdoZTdVZGZ2NG1jSld0K0tIOEdONm5zdk1oM3I5emY5N2ZSWE1lZzdLb2hUNnRRYlprOEgyTjdZc3BIdHErczZHK09MUmtPU3dFTEJrU0Fwdk5KbDhEY214UkZLL2tJZlFJTFJKYWtBNGtEZm1ueWltSHpUZnNMb1R4ODdyS2V6Y2F5NGVIcktjZXYwK2N4MkJldUdOQU5pc1UxYW13bVkzMXdXMFVuOHhzOEVrUG1ZV1hNTDdIci9yQk9vMEZSNHFTTGI5alR4WFBtem9sZmF6dzRkWi9QbWVxeXFLSkNEMTFGSm9HTnV0M3hBQ2lTZDY3bVd6dmV3Z0VnMzdUa3JxV1lMbDBUWDYzb0FRUzJJWXFkb1RRSW9WMTM3YUZGN2lHZXY4aXdYVXJaSklybEpidmx1M1VCNmp6OXVjaGlqMm1zSFlPYkl4UUs0Y0hXOFVNQ3p5dUU4c3ZGeWhOSFluQnFHS2lycFROK294Z2JuSys5Z2dONWtuc0h4b0tHejFkL0xadGFWc3ZPMWxjVmdzMUtraGVoNDQ2ZElTb1ZJR1ptOTZrcUM3WEkzZEpOL3Z3OE5GUTk2MXVUQkZ2K0s0WitVc1ZrZ0doWmZibm1jMXozSGl1SFdmbXlSYW5TUjBHRWN6R0FCVFBKUE53YzMwZDBmblE4MytiOSsvWElrd2hiUDJWOFJldk5hR3FBc3NtVUZkR2xKNitQY2ZRdzU1czd4TUdLbDI4MmJxT1FZQXJaWElNSWlaQkVqRWtvcmcySGRURDRtUTNuZE1NNlEva1ZmOUZoVkxYNjlDMTVBN0M3dFYvamZQTFZkK0x2RDA2c3NnUVBXYm1ZYWh6RVdGWGszNWQ0TW12WUIrTThSa2lSb2hLczRoVU1SRkNpK29hUzcySGtCNVNnbTFEb2VUbHp5YnMyVTJlMzRhQ2hHM2ZXWXpSYXhsWEZSY1haNVQ2VVlxQkJWSXlVQ0dhRW16RGNnbFlSNFZUOERDUnA5M0VNcmZuemluWjQ3UHQ3M2R6WGs4RFlISm1EdDh6Z2c2SG4vOHBYaG1sNkIvSHNaUXl4b0pLeUg5ald6cVo1SG1JYUdteitLMlArcDJydE5oWFVjR1BQZDc3VnUrM1hGYUpZaG1jc0syVHRrYWRtTy9mRUFVMXo0MVFONEVxSmtkd09zVGNUVlhrUFpPMTZjRExRVkFvZC82UjlYbzlPRUtFRUdZNUUyTVFndlJFTmtRMnJuVExWU1Q5OFFTV1poc0k5bEh2eDBQeUQ3MjVyZ1pIMGtFK3ZvSDh2US8yeXVPQW1pUC9kckdFaDU2VHE5cjcwUFAvR08vZlZuWkxMcUpZQWt2cWFEU1ZzYXFGdm4zbk9DS0kyVFdGRDY2Q1loL1ZRVTkzT0FuaXZ0OEhFMWowZkd5QlN0UjZ1N3k4QkpSazd2RkVHdTEvRVpOZ3JWWGk1cklRV2tKY3VGSXVHV21hMFZNbTc3RWNVeXc2bXVBT2g0bGZPNDRwM0VVZXZPa21lQWoyZFo4L3YydlNDNkxuakM3RjNxcWpCdDF0MlBQM3lXQ1BIVFhobHRtazRsZGd4bEREdkZRL21CM3VvV2p2RTBqTlhjQVA4UTh6LzdkcDYyN3ZWeS9wWktPNXNmdytWUjRMeHFJV21ob1d0VkxYaVl1MzN4QnNEZlR1NVhidENHNEpONW1VUWwxRlBGSGNjT0tyMExZdDYzYURpcExVYTI2NXdzM0FFdEhXMUxGbFViVUlhNnJnOWJaQzlJb091MzNaTjhUZHpERDcrbmNWUEthRjRiWjIzbTFudlRGVjd0WExxK3h5VmR1Zis4YTRvMUJqeko1cW1XODM5WnhzWThZWWVYQlZ5SWVjLzhkWi8yTFhuckN0ZzFsNTJoK2Y4UkNVNVNLeXJHSFJRQjA2Tmhldk1OdUE5Wmc5S0lMZlRpczl5T3M1VDd0WHp3cW9LbWRuYjVGZ3FIV3p3Vm5xZ1pZcWJsZ3RFcFdzRVJKQlVyNXY3dEkzbHg4TFRCTjgzNDFWdjQyTzRiRmhHN2wzWEpmM0lQQTIyekxYbnhTcVBKZk5wenFYMlRPeTIwWmluaTY1MkRUSzY3Zlg2VEhnTWVmLy9hei9OSVZaOG5wdDJhSEZsY3FKV01GcUVWZzB4bEZ0aEhRSmVrbEZKNVVrNGtOcTBhODY2YWZmellvWDI1RGtnYUYrYzY3bCt1N2kzSXZGUjNMUVFqN1ZOQkdsbDBwYW1xYW5xbnFNRFVXYnVBdGhRc1cya2Z4amxwVWZENHFTNjdvL3YzZS8wdlRRTXlrcjB3YnpHUHVmL3hWTVlSY0Z4L202aXN1RW8yVkQwOEN5TnByYWFEZnZxQ1VSUlBGRXlvL0FvbzhuL3MwVU5xVnNiTURsZWxWbHZiNjBybXNKQVJBbG1RNGhrVEVrVkMrb3E1YW1WbUt3R2NzL1ZVZ2NldU40c2w2dFJIb3N1SzhXK0thVW9jakwwM2FubEhyLzM5WHg0b3E0VnZ3QXZ6Mm42ckozL1Q4MDBuOW9MZnoxNnpmWFozbC9NdVhPZjdFU2xzdUdPZ1lXVFdRUmhiUFhMd2pXU2RBdWgxWS9BSUpmMWRsREZLREkzaUVFMlE0WTJQU2R0VzFIMjJiMjNNQ1M4L0ZCakJEQmRFTXQ1elQxT1Uxc3FTc2xabGtjd0JCMm1SUE4zbS81SG1Gd3czeklNWDhiNENxcXZYM1BvV2RnZEF0TzJXbFZ6U1phOUhMUWpqWERTMkxjYnpzODVQcFBPYzlSY2hwRm8zM3Y4a0FzWmJtRVphMnNHcU9PRzg3UFg2QzJBVWxlRXVxcWNGSFhVbDV2cHh4VTl6c0tyZ2lUTEoxYlQrZDc4RVQ1R0wxMW5qQk9LdGFianZPelM0Nld4MjdyampITElZSWxpSkpBWG5QYVJEYk5FOWJ0TytvUXNhcGlzMDRRR2tLQVhwVTRaR0NkaG80NkE0T0VHZEp2aisydThGNmZuOFFUQjJObW15N1N5ZFQrWE5yMzlTMGFjUCtici9XMktXM2VKOFdmVVR4VzFMSWxWL002UlF0SWNDZVlLT0padHdSTVBWSnhXcWp3b2VGRHpQK1VXSWpJWWU2eDZDR0c3MmtTQURUSkJaOERkNXdyOWRodUNVWVZBa2tUeDBlUjQwWFBKOGZHazBYSE4xLzlhK3BGU3dpR1dvS1VxS0pjVGNHdlErNnJudGxHL0t2TUJhVTZnMkptd1JNSW5MKzdRQk9RRkxUUGFXSDl3SWhCcUdWTkV5NVl4Z3RXVlV1TUcwUTJ4QmhjUVRkOXQ5dDZFSE1mZHhHUEgwOEh4bmNkaS9heFg5Kzl2L3dqN0xEZmVrTUtmZ2gyYW8vTlhHR05VVnlUZVZDTTJWWnRzdTArMzUxTi90RHpmN2ZyT1Fwd3NQSE9SZDFTR2pocHg2S0drMVhncEZHTzZrVFFjeTR1dm1HNXlLbWFVS3JLRmRhM3NvUGY5R1NiRHVBNk8yRFpTQkpLTmRFQUdHL2Z2bVd6MmJCYTFJN1VlZGhZSW9xSm1scVVucnE2cEtrdlNkWVFUSkJnQ0V1WDI2bDhVeGQzU2tsSXpIeGl5ZjEwZ0lwY3g4SGNaQTdleS9OVEQ3TEpWSmQ1SDExSHgzbFhKbkxkRGZweC9YWHpBM1NxLzhpLyt6aktiemRyYzlyMlhZbngrMTYvSFIzdkZjOXYyVEJtdis0cnRtcG1WTEhDdXBZWWplTmw0SGdWV0M0U3h5dWp2WGhGMzU1eFZDdWQ5bGd3UW9oMDNUVkpGMmVkdXVOTTMyU0RGSC9vTWlIQm9HMWJlL2Z1SFNGVStiNkVtWnNIakE2MWxzaUdPcDV6MUp4VGgwdENXRk5YU2doZHZxK2trdkNhV2NHcVlhT2IyU1F6ek4zNmZ0K3hQOWJ6Tzg0dCtjK1RUMDhWWm9mbDdPM3ZWOHJyc3NzTm1Oa3NlT1dxL2ozVXVCKzZuY2QvM2hpS2VReXc2OHRobGdzWmhNVHhJbkJ5RkRtdUU2dW1vNUlMenM2K29xbU1ycjJRdW5LcTMyZlI4MUdVYkFlOXA2NkFndUNpZmw4SWxieDkrM2JJcytidXJFcVVoSkF3M1VnSWE1YjFHYXZtakdWOVJoMHVxVU5IREpZVFFxU1pxQ0FpMllSeldHejR0c0ZPeE5nMWJPK01pbWNFN0UzcFRRZjdkY0oydmsvL1NnUlp3bUNQYmJ6OEcvSjlXNTFTdVJtU1B4U2lQeWFJaWYreHEybWEzWGZndWs3TFlGdlkrc3UvQjlDK0k0aHlmQlE0WGhvbnk1NW5wOHA2L1ROUzk0b3FkRmhxaVI0SWp1ZktpemRIOEVPVHZlLzM2eFptYXM0YTJEckJ5bWxsWnNRWU9UKy81T3pzTFAvbVNvWVFEWThxNjZta1pSblBXVlR2V0RXWExLczFNVzZJSlNGRVR2VWtZY3Z1YllKb1JOelFmdE1wK0doaGV3aUhGZFZ6dVE3WUt3ZHZJK2toeWo1dFZ3K1l4S1p0ZnZ6byttSEFEanBjT2NjYWdpSWhzVndZUjB2aDVNZzRQVmFPNmpVWGIvNkNTaTZ3L2x5Q3VHa01Sb0paYlN2RHJ1eklnWHV2azdrUHRRTmp3bndSR1hLa0R5eWtLaTlldk9ENGVFV0pFaXNCajU1MGFDT0kyakpVdFBVeGkzcEIyM1YwMGxQRkNsWEpxWjU2eGxRa01HNzBQY1VNdjYyN2NGWWpiT3ZTbHBaY2g4U0xSWm16N2RjOWZoWUYyYlN0VWRZbVp6L04rb3c4cHdLTTJXSktlN2ZMWHJ0dFB2MVk0YWJiWmNjV2tkZHIrcnhuNDltaXVaSThITFNDcHljMXA4Znc1QmlPbDJ2TzMvNFpmZmVDbWpOQk5nUko5SDBpRURFVndtMG8rS3l6VzNMYmRmY2RjdHViblBnMlJYQUFWVGZqdkhuOWJtRFQzUmlqaUhXWlRWOFQyVWlRY3hiVk9ZdjZranBjMHNTZUtnaFZGSndaVFNSNkVtbGsxY3NrL3hMRmlic3hZY0ttVDVia0VOWGUyODZCOVJlUldaNWpNNWxseHBvM2R4aDVmeEZFcDV2QWNJektsQXViSkN5UmhOQVRRc3R5a1RnNWdaT1ZjbnFVYU1JRkw3LytFNVp4STVZdVdEVEJpWjRhVlZVTnVGUk5MSElIWG45MzJMYVY3N3VlblY3RXpDeUVNQ0F6QnFwcU1RVDU2cXR2K1BUVHB5eFhDMUxxU1dhZWowMEEzUkFzU0JYTzdHUjFTbWRyZXR1d3NDV2R1b3pVOXdrRWdqUlpVZWZjZ0FKUmJGQzhpWXpsZGdaTDVnNEwvLzVDVCs4R1Z4OVlBN3VkRTJGNGtvYXBsbjJrem5DRkh3VENORHF2VVBzd3BLRWVDMWVNNGFuazlNcmZWalpwRnc2bU16K3dmMnk4d2IvblFQbWk5S3hDUmxUekRFWW1HMDZQYW81V1BhZkh4ck9ueHJKZTgvcmxueUxwTFlRTmRVeFljdmRVUXFCckU3RnV1TnkwbzVtc2JQQWJEK3pBL1hkcHh3ejNoU3JJSlQ1b2NlREZpeGNjSFRXc2pwYUlDRTEwYlhqWGQ0Z0lNVVFhMlpBNFp4R1hMT0tDcEExWVJNMU5Ed25KYXFHWUhmWWgyR2k2MmRZSmpCdjMyd2VPNHZ2WFlKdjY2c1Nxc08vNmxYTmdrdXNYT3J1dlpNL0RYTk05bURGRmRHL3pycVA2ZHNNaDhWWmlqcVNNSVJmYlRPNm9Zb2tvaWVOVjRHaVJlSFpxUEg4Q3Rad1I5QzB2di9wanZ2TzhsczM1SzVwRzJHdzJoQkNvNjNyUXZKK2ZuOC90NFB0bDZXdFk4QWZpcG9JZlkvNXU3NENiQnpEYTlkcmVubC9JeWRNbmxPd3ZFTEt2dFJLc3A0b1hOSnl4V2pRWUM1QWx0bzVRTFdndEVxeDIyYk9FNUtIdWhXVVZrdVZ6RVdIcWcrUUhUUm5zeU9rOEZPSS9qT2x4VHJGbElqSVhVNzlMdytQOVU5L3pmZTFlcHpnZFBlRks1bFg4RFJPL2FjdmE5cENmdGF1OFdqNFFQSmJwZC82U3NxZkx1L0o2QlhGUFFqWDNMeC9DUkJXemprcVVwbFpPam9SbnA4b25wOGJKYXMxU0x2aUxQL2tYZlBLazV2THlHNEwxYU85c2VZdzFYYmVocWlwUzMvSDI5YXM1QmIvUG9BOU53SFh0Q1JGa3JCZzZQdTlwZUZXVnBsbks2OWV2T1RwYTh1bW5uMk9tMUtHaXFxRzNIbE9GdEpaZzcreW9hZ2lzVUd2b08wWDBDUmFQNk0wd05VVGRXUjlSMTBsOUFIbnZ2WVdSRm5QVVpEK21tVVo4ZjNESnZzK3JJV1RYMWZtOUpkaGtpQkhJZmZyUW5OR2poNnBPRDlsRHppNnVFTWtFeS9lN2g0UG1PTzhvbkJ4Rm5qMFJUbGNkVDVZZFQxZncwei85VndROWczUW1GUjBTaGI3dldLeFdnNkxhekhqMTZoVm41MjlIQkwrUDE4OVZzSGR3TzM3ckU3bFAxRXpMcmpTUVlCSUQ1eGZuZlAzTlN6bGFQZUhvNkFqSlZVMkNnWm9pMmhGbGc4ZzdwSTRrODB3eDd6VG5FZE5WTm9zbE1DL2RFd001TUVYeWhIT1FJeG0xbjB3MDhmcXRzcktWcFNqS052OVhLTC9Na0g3NnVhOGQzeTg2RTYzSTdVcm1FdHhsMkFnZm01cmlBV0hVZW8rbHNOempNdjlzbzVlNnVhSmprdXRPcWNVUjNDejdiUVNqaXNwcVdmSGtPSEs2N1BuMGlmRHBKL0RWWC93TDB2b2JsazJMMkRyN2U0QldGVzNidXZqYU5GeXV6L25teFZkc05wYzJZOUhmbC9aeXlpMk1HMjFDTWNRTEM1R05ZZTJtbzY0WFhGeXM3Y1dMRjdKWXJDZ3gzWVluNDY4SUJIcEo5ZzRWczJVRmVoVG9iWUcxUzZ5clFTSmkwS1dBcUNkaVpzS0dqMzI1elZ6TVpjeVBEUVlkaDAyL1Q2ZzNkNlBnVHBrdEIxa1loVkZYeEdYdi9DY1dzSkRHK3VIZm9nUHhMbkNWbVhVdy9RMktIN3pvQjRhUkNBSjFJNndXa1NjblM1NC9qVHc1UHVQWlNlRE5OMy9NK3V5bjFLeXBReXNWaWJTNVJLb0dRa1czN2xnc0d2cSs1OFdMcjlsc0xpMmwvdXBvc3Z5dlIwTjhHM2JlNU8rQTQwWFRMTmhzTnJ4OWU4YXpaeGVzbWlWVjViYlZZRUEybzZtdENlWng0b3VtNFZoUDZXeERwelZKYW9JS01RbVdHb1NBUmkveElvZEk5eDZRVVYxdzUzSGZaMDd2eW0zdG1yZGt6MjgzWjgvOU1CNFBpcW5WUkxZK1B5WjR5UGtmRURvM3Q4K1hmQnUwV0kvQXVac1FJRlJVRGF4V2dTZkhGVStmTEhsNmFuenh5UW52WHY3L2VQWE5uM0t5VWtMZmluYVh0SHBPSGJQMjNaUzZyZ214NHMyN2w3eDY4OXBFUUlMeC93Y0ZYRmRyRFVnQ1R3QUFBQUJKUlU1RXJrSmdnZz09IiBhbHQ9IkZhY2Vib29rIiBjbGFzcz0ib3B0LWljb24taW1nIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im9wdC10ZXh0Ij48ZGl2IGNsYXNzPSJvcHQtdGl0bGUiPkZhY2Vib29rPC9kaXY+PGRpdiBjbGFzcz0ib3B0LWhhbmRsZSI+UiZhbXA7SiBHcm9vbWluZzwvZGl2PjwvZGl2PgogICAgPHNwYW4gY2xhc3M9Im9wdC1hcnJvdyI+4oaSPC9zcGFuPgogIDwvYT4KICA8YnV0dG9uIGNsYXNzPSJvcHQiIG9uY2xpY2s9IndpbmRvdy5sb2NhdGlvbi5ocmVmPSd0ZWw6KzM3MjU4NzM1NDU2JyI+CiAgICA8ZGl2IGNsYXNzPSJvcHQtaWNvbiI+PHN2ZyB3aWR0aD0iMzIiIGhlaWdodD0iMzIiIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSJyZ2JhKDI1NSwyNTUsMjU1LC40NSkiIHN0cm9rZS13aWR0aD0iMS42Ij48cGF0aCBkPSJNMjIgMTYuOTJ2M2EyIDIgMCAwMS0yLjE4IDIgMTkuNzkgMTkuNzkgMCAwMS04LjYzLTMuMDdBMTkuNSAxOS41IDAgMDEzLjA3IDkuODJhMTkuNzkgMTkuNzkgMCAwMS0zLjA3LTguNjdBMiAyIDAgMDEyIDFoM2EyIDIgMCAwMTIgMS43MmMuMTI3Ljk2LjM2MSAxLjkwMy43IDIuODFhMiAyIDAgMDEtLjQ1IDIuMTFMNi45MSA4LjkxYTE2IDE2IDAgMDA2IDZsMS4yNy0xLjI3YTIgMiAwIDAxMi4xMS0uNDVjLjkwNy4zMzkgMS44NS41NzMgMi44MS43QTIgMiAwIDAxMjIgMTYuOTJ6Ii8+PC9zdmc+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJvcHQtdGV4dCI+PGRpdiBjbGFzcz0ib3B0LXRpdGxlIiBkYXRhLWkxOG49ImNhbGxfdXMiPkNhbGwgVXM8L2Rpdj48ZGl2IGNsYXNzPSJvcHQtaGFuZGxlIj4rMzcyIDU4NyAzNTQ1NjwvZGl2PjwvZGl2PgogICAgPHNwYW4gY2xhc3M9Im9wdC1hcnJvdyI+4oaSPC9zcGFuPgogIDwvYnV0dG9uPgogIDxkaXYgY2xhc3M9ImhvbWUtZm9vdCI+CiAgICA8c3Bhbj5UYWxsaW5uPC9zcGFuPjxkaXYgY2xhc3M9ImZkb3QiPjwvZGl2PjxzcGFuPkVzdG9uaWE8L3NwYW4+PGRpdiBjbGFzcz0iZmRvdCI+PC9kaXY+PHNwYW4+QWxsdmVlbGFldmEgNDwvc3Bhbj4KICA8L2Rpdj4KPC9kaXY+CjwvZGl2PgoKPCEtLSBCT09LSU5HIC0tPgo8ZGl2IGNsYXNzPSJzY3JlZW4iIGlkPSJib29rU2NyZWVuIj4KPGRpdiBjbGFzcz0iY29uIj4KICA8YnV0dG9uIGNsYXNzPSJiYWNrLWJ0biIgaWQ9ImJhY2tCdG4iIGRhdGEtaTE4bj0iYmFjayI+4oaQINCd0LDQt9Cw0LQ8L2J1dHRvbj4KICA8ZGl2IGNsYXNzPSJsb2dvLXJqIj5SJmFtcDtKPC9kaXY+CiAgPGRpdiBjbGFzcz0ibG9nby1zdWIiIGRhdGEtaTE4bj0ibG9nb19zdWIiPkdyb29taW5nIMK3INCi0LDQu9C70LjQvTwvZGl2PgogIDxkaXYgY2xhc3M9InByb2dyZXNzIj4KICAgIDxkaXYgY2xhc3M9InBzIGFjdGl2ZSIgaWQ9InBzMSI+PGRpdiBjbGFzcz0icGRvdCI+PC9kaXY+PHNwYW4gZGF0YS1pMThuPSJwc19zZXJ2aWNlIj7Qo9GB0LvRg9Cz0LA8L3NwYW4+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwbCIgaWQ9InBsMSI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwcyIgaWQ9InBzMiI+PGRpdiBjbGFzcz0icGRvdCI+PC9kaXY+PHNwYW4gZGF0YS1pMThuPSJwc19tYXN0ZXIiPtCc0LDRgdGC0LXRgDwvc3Bhbj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBsIiBpZD0icGwyIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBzIiBpZD0icHMzIj48ZGl2IGNsYXNzPSJwZG90Ij48L2Rpdj48c3BhbiBkYXRhLWkxOG49InBzX3BldCI+0J/QuNGC0L7QvNC10YY8L3NwYW4+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwbCIgaWQ9InBsMyI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwcyIgaWQ9InBzNCI+PGRpdiBjbGFzcz0icGRvdCI+PC9kaXY+PHNwYW4gZGF0YS1pMThuPSJwc19kYXRlIj7QlNCw0YLQsDwvc3Bhbj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBsIiBpZD0icGw0Ij48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBzIiBpZD0icHM1Ij48ZGl2IGNsYXNzPSJwZG90Ij48L2Rpdj48c3BhbiBkYXRhLWkxOG49InBzX2RldGFpbHMiPtCU0LDQvdC90YvQtTwvc3Bhbj48L2Rpdj4KICA8L2Rpdj4KCiAgPCEtLSBTdGVwIDEgLS0+CiAgPGRpdiBjbGFzcz0ic3RlcCBzaG93IiBpZD0iYmsxIj4KICAgIDxkaXYgY2xhc3M9InNsYmwiIGRhdGEtaTE4bj0ic3RlcDFfbGJsIj4wMSDCtyDQn9C+0YDQvtC00LA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImJ3cmFwIj4KICAgICAgPGRpdiBjbGFzcz0ic2JveCI+CiAgICAgICAgPHNwYW4gY2xhc3M9InNpIj7wn5SNPC9zcGFuPgogICAgICAgIDxpbnB1dCBpZD0iYklucHV0IiB0eXBlPSJ0ZXh0IiBwbGFjZWhvbGRlcj0i0J3QsNGH0L3QuNGC0LUg0LLQstC+0LTQuNGC0Ywg0L/QvtGA0L7QtNGDLi4uIiBkYXRhLWkxOG4tcGg9ImJyZWVkX3BoIiBhdXRvY29tcGxldGU9Im9mZiI+CiAgICAgICAgPGJ1dHRvbiBjbGFzcz0iY2xyIiBpZD0iY2xyQnRuIj7inJU8L2J1dHRvbj4KICAgICAgPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImRyb3AiIGlkPSJiRHJvcCI+PC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InNiYWRnZSIgaWQ9InNCYWRnZSI+PC9kaXY+CiAgICA8ZGl2IGlkPSJzdmNTZWMiIHN0eWxlPSJkaXNwbGF5Om5vbmU7bWFyZ2luLXRvcDoxNnB4Ij4KICAgICAgPGRpdiBjbGFzcz0ic2xibCIgaWQ9InN0ZXAyTGJsRWwiIGRhdGEtaTE4bj0ic3RlcDJfbGJsIj4wMiDCtyDQo9GB0LvRg9Cz0LA8L2Rpdj4KICAgICAgPGRpdiBpZD0ic3ZjTGlzdCI+PC9kaXY+CiAgICA8L2Rpdj4KICA8L2Rpdj4KCiAgPCEtLSBTdGVwIDIgLS0+CiAgPGRpdiBjbGFzcz0ic3RlcCIgaWQ9ImJrMiI+CiAgICA8ZGl2IGNsYXNzPSJzbGJsIiBkYXRhLWkxOG49InN0ZXAyX21hc3RlciI+0JLRi9Cx0LXRgNC40YLQtSDQvNCw0YHRgtC10YDQsDwvZGl2PgogICAgPGRpdiBjbGFzcz0ibWFzdGVycyI+CiAgICAgIDxkaXYgY2xhc3M9Im1idG4iIGRhdGEtbWFzdGVyPSLQotCw0YLRjNGP0L3QsCI+PGRpdiBjbGFzcz0ibW5hbWUiPtCi0LDRgtGM0Y/QvdCwPC9kaXY+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9Im1idG4iIGRhdGEtbWFzdGVyPSLQkNC70LjRgdCwIj48ZGl2IGNsYXNzPSJtbmFtZSI+0JDQu9C40YHQsDwvZGl2PjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJtYnRuIiBkYXRhLW1hc3Rlcj0i0JrRgNC40YHRgtC40L3QsCI+PGRpdiBjbGFzcz0ibW5hbWUiPtCa0YDQuNGB0YLQuNC90LA8L2Rpdj48L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0ibWJ0biIgZGF0YS1tYXN0ZXI9ItCQ0L3QvdCwIj48ZGl2IGNsYXNzPSJtbmFtZSI+0JDQvdC90LA8L2Rpdj48L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0ibWJ0biIgZGF0YS1tYXN0ZXI9ItCQ0LvQtdC60YHQsNC90LTRgNCwIj48ZGl2IGNsYXNzPSJtbmFtZSI+0JDQu9C10LrRgdCw0L3QtNGA0LA8L2Rpdj48L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0ibWJ0biIgZGF0YS1tYXN0ZXI9ItCa0YHQtdC90LjRjyI+PGRpdiBjbGFzcz0ibW5hbWUiPtCa0YHQtdC90LjRjzwvZGl2PjwvZGl2PgogICAgPC9kaXY+CiAgPC9kaXY+CgogIDwhLS0gU3RlcCAzIC0tPgogIDxkaXYgY2xhc3M9InN0ZXAiIGlkPSJiazMiPgogICAgPGRpdiBjbGFzcz0ic2xibCIgZGF0YS1pMThuPSJzdGVwM19sYmwiPtCa0LDQuiDQtNCw0LLQvdC+INCy0Ysg0L/QvtGB0LXRidCw0LvQuCDQs9GA0YPQvNC40L3Qsz88L2Rpdj4KICAgIDxidXR0b24gY2xhc3M9ImdidG4iIGRhdGEtdmFsPSLQn9C10YDQstGL0Lkg0YDQsNC3IiBkYXRhLWkxOG49ImcxIj7Qn9C10YDQstGL0Lkg0YDQsNC3PC9idXR0b24+CiAgICA8YnV0dG9uIGNsYXNzPSJnYnRuIiBkYXRhLXZhbD0i0J7RgiAxINC00L4gMyDQvNC10YHRj9GG0LXQsiIgZGF0YS1pMThuPSJnMiI+0J7RgiAxINC00L4gMyDQvNC10YHRj9GG0LXQsjwvYnV0dG9uPgogICAgPGJ1dHRvbiBjbGFzcz0iZ2J0biIgZGF0YS12YWw9ItCe0YIgMyDQtNC+IDYg0LzQtdGB0Y/RhtC10LIiIGRhdGEtaTE4bj0iZzMiPtCe0YIgMyDQtNC+IDYg0LzQtdGB0Y/RhtC10LI8L2J1dHRvbj4KICAgIDxidXR0b24gY2xhc3M9ImdidG4iIGRhdGEtdmFsPSLQkdC+0LvQtdC1IDYg0LzQtdGB0Y/RhtC10LIiIGRhdGEtaTE4bj0iZzQiPtCR0L7Qu9C10LUgNiDQvNC10YHRj9GG0LXQsjwvYnV0dG9uPgogIDwvZGl2PgoKICA8IS0tIFN0ZXAgNCAtLT4KICA8ZGl2IGNsYXNzPSJzdGVwIiBpZD0iYms0Ij4KICAgIDxkaXYgY2xhc3M9InNsYmwiIGRhdGEtaTE4bj0ic3RlcDRfbGJsIj7QktGL0LHQtdGA0LjRgtC1INC00LDRgtGDPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjYWwtaCI+CiAgICAgIDxidXR0b24gY2xhc3M9ImNhbC1uIiBpZD0icHJldk0iPiYjODI0OTs8L2J1dHRvbj4KICAgICAgPGRpdiBjbGFzcz0iY2FsLW0iIGlkPSJjYWxNIj48L2Rpdj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iY2FsLW4iIGlkPSJuZXh0TSI+JiM4MjUwOzwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjZyIgaWQ9ImNhbEciPjwvZGl2PgogICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2dhcDoyMHB4O2FsaWduLWl0ZW1zOmNlbnRlcjttYXJnaW4tdG9wOjEycHg7cGFkZGluZy10b3A6MTJweDtib3JkZXItdG9wOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xKTtmbGV4LXdyYXA6d3JhcDsiPjxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweDsiPjxkaXYgc3R5bGU9IndpZHRoOjE2cHg7aGVpZ2h0OjE2cHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDpyZ2JhKDkwLDE4MCw5MCwuMTUpO2JvcmRlcjoxcHggc29saWQgIzVhYjQ1YTtmbGV4LXNocmluazowOyI+PC9kaXY+PHNwYW4gc3R5bGU9ImZvbnQtc2l6ZToxcmVtO2NvbG9yOiNmZmZmZmY7bGV0dGVyLXNwYWNpbmc6LjAzZW07IiBkYXRhLWkxOG49ImNhbF9hdmFpbCI+0JXRgdGC0Ywg0YHQstC+0LHQvtC00L3QvtC1INCy0YDQtdC80Y88L3NwYW4+PC9kaXY+PGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OHB4OyI+PGRpdiBzdHlsZT0id2lkdGg6MTZweDtoZWlnaHQ6MTZweDtib3JkZXItcmFkaXVzOjUwJTtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA0KTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEpO2ZsZXgtc2hyaW5rOjA7Ij48L2Rpdj48c3BhbiBzdHlsZT0iZm9udC1zaXplOjFyZW07Y29sb3I6I2ZmZmZmZjtsZXR0ZXItc3BhY2luZzouMDNlbTsiIGRhdGEtaTE4bj0iY2FsX25vbmUiPtCh0LLQvtCx0L7QtNC90L7Qs9C+INCy0YDQtdC80LXQvdC4INC90LXRgjwvc3Bhbj48L2Rpdj48L2Rpdj4KICAgIDxkaXYgaWQ9InRpbWVTZWMiIHN0eWxlPSJkaXNwbGF5Om5vbmU7bWFyZ2luLXRvcDoxNnB4Ij4KICAgICAgPGRpdiBjbGFzcz0ic2xibCIgZGF0YS1pMThuPSJzdGVwNF90aW1lIj7QktGL0LHQtdGA0LjRgtC1INCy0YDQtdC80Y88L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0idGciIGlkPSJ0aW1lRyI+PC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgc3R5bGU9Im1hcmdpbi10b3A6MjBweDtwYWRkaW5nLXRvcDoxNnB4O2JvcmRlci10b3A6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA4KTt0ZXh0LWFsaWduOmNlbnRlciI+CiAgICAgIDxidXR0b24gaWQ9ImNhbGxiYWNrQnRuIiBjbGFzcz0iY2JrLWJ0biI+0J3QtSDQvdCw0YjQu9C4INGD0LTQvtCx0L3QvtC1INCy0YDQtdC80Y8/IOKGkjwvYnV0dG9uPgogICAgPC9kaXY+CiAgPC9kaXY+CgogIDwhLS0gU3RlcCA1IC0tPgogIDxkaXYgY2xhc3M9InN0ZXAiIGlkPSJiazUiPgogICAgPGRpdiBjbGFzcz0ic2xibCIgZGF0YS1pMThuPSJzdGVwNV9sYmwiPtCS0LDRiNC4INC00LDQvdC90YvQtTwvZGl2PgogICAgPGRpdiBjbGFzcz0iZmciPjxsYWJlbCBjbGFzcz0iZmwiIGRhdGEtaTE4bj0ibGJsX25hbWUiPtCY0LzRjzwvbGFiZWw+PGlucHV0IGNsYXNzPSJmaSIgaWQ9ImNOYW1lIiB0eXBlPSJ0ZXh0IiBwbGFjZWhvbGRlcj0i0JLQsNGI0LUg0LjQvNGPIiBkYXRhLWkxOG4tcGg9InBoX25hbWUiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0iZmciPjxsYWJlbCBjbGFzcz0iZmwiIGRhdGEtaTE4bj0ibGJsX3Bob25lIj7QotC10LvQtdGE0L7QvTwvbGFiZWw+PGlucHV0IGNsYXNzPSJmaSIgaWQ9ImNQaG9uZSIgdHlwZT0idGVsIiBwbGFjZWhvbGRlcj0iKzM3MiAuLi4iPjwvZGl2PgogICAgPGRpdiBjbGFzcz0iZmciPjxsYWJlbCBjbGFzcz0iZmwiIGRhdGEtaTE4bj0ibGJsX2VtYWlsIj5FbWFpbDwvbGFiZWw+PGlucHV0IGNsYXNzPSJmaSIgaWQ9ImNFbWFpbCIgdHlwZT0iZW1haWwiIHBsYWNlaG9sZGVyPSJlbWFpbEBleGFtcGxlLmNvbSI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJmZyI+PGxhYmVsIGNsYXNzPSJmbCIgZGF0YS1pMThuPSJsYmxfcGV0Ij7QmtC70LjRh9C60LAg0L/QuNGC0L7QvNGG0LA8L2xhYmVsPjxpbnB1dCBjbGFzcz0iZmkiIGlkPSJjUGV0IiB0eXBlPSJ0ZXh0IiBwbGFjZWhvbGRlcj0i0J3QtdC+0LHRj9C30LDRgtC10LvRjNC90L4iIGRhdGEtaTE4bi1waD0icGhfb3B0aW9uYWwiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0ic3VtIiBpZD0ic3VtQmxvY2siPjwvZGl2PgogICAgPGJ1dHRvbiBjbGFzcz0iY2J0biIgaWQ9ImNvbmZpcm1CdG4iIGRhdGEtaTE4bj0iY29uZmlybV9idG4iPtCf0L7QtNGC0LLQtdGA0LTQuNGC0Ywg0LfQsNC/0LjRgdGMPC9idXR0b24+CiAgPC9kaXY+CgogIDwhLS0gU3VjY2VzcyAtLT4KICA8ZGl2IGNsYXNzPSJzYmxvY2siIGlkPSJzdWNCbG9jayI+CiAgICA8ZGl2IGNsYXNzPSJzaTIiPvCfkL48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InN0IiBkYXRhLWkxOG49InN1Y2Nlc3NfdGl0bGUiPtCX0LDQv9C40YHRjCDQv9GA0LjQvdGP0YLQsCE8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InNzIiBkYXRhLWkxOG49InN1Y2Nlc3Nfc3ViIj7QnNGLINGB0LLRj9C20LXQvNGB0Y8g0YEg0LLQsNC80Lgg0LTQu9GPINC/0L7QtNGC0LLQtdGA0LbQtNC10L3QuNGPLjxicj7QodC/0LDRgdC40LHQviwg0YfRgtC+INCy0YvQsdGA0LDQu9C4IFImSiBHcm9vbWluZyE8L2Rpdj4KICAgIDxidXR0b24gY2xhc3M9ImhidG4iIGlkPSJob21lQnRuIiBkYXRhLWkxOG49InRvX2hvbWUiPuKGkCDQndCwINCz0LvQsNCy0L3Rg9GOPC9idXR0b24+CiAgPC9kaXY+CjwvZGl2Pgo8L2Rpdj4KCjxkaXYgaWQ9ImNia01vZGFsIiBzdHlsZT0iZGlzcGxheTpub25lO3Bvc2l0aW9uOmZpeGVkO2luc2V0OjA7YmFja2dyb3VuZDpyZ2JhKDAsMCwwLC43NSk7ei1pbmRleDozMDA7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7cGFkZGluZzoyMHB4Ij4KICA8ZGl2IHN0eWxlPSJiYWNrZ3JvdW5kOiMwYTBhMGE7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xMik7Ym9yZGVyLXRvcDoxcHggc29saWQgI2ZmZmZmZjtwYWRkaW5nOjI4cHggMjRweDt3aWR0aDoxMDAlO21heC13aWR0aDozNjBweCI+CiAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6MC44MzhyZW07bGV0dGVyLXNwYWNpbmc6LjJlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjE2cHg7Zm9udC13ZWlnaHQ6NjAwO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmIj7QntCx0YDQsNGC0L3Ri9C5INC30LLQvtC90L7QujwvZGl2PgogICAgPGRpdiBjbGFzcz0iZmciPjxsYWJlbCBjbGFzcz0iZmwiPtCY0LzRjzwvbGFiZWw+PGlucHV0IGNsYXNzPSJmaSIgaWQ9ImNia05hbWUiIHR5cGU9InRleHQiIHBsYWNlaG9sZGVyPSLQktCw0YjQtSDQuNC80Y8iPjwvZGl2PgogICAgPGRpdiBjbGFzcz0iZmciPgogICAgICA8bGFiZWwgY2xhc3M9ImZsIj7QotC10LvQtdGE0L7QvTwvbGFiZWw+CiAgICAgIDxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpzdHJldGNoO2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjE1KSI+CiAgICAgICAgPHNwYW4gc3R5bGU9InBhZGRpbmc6MTBweCAxMHB4IDEwcHggMDtjb2xvcjojZmZmZmZmO2ZvbnQtc2l6ZToxLjM2M3JlbTtib3JkZXItcmlnaHQ6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEpO21hcmdpbi1yaWdodDoxMHB4O2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmIj4rMzcyPC9zcGFuPgogICAgICAgIDxpbnB1dCBpZD0iY2JrUGhvbmUiIHR5cGU9InRlbCIgcGxhY2Vob2xkZXI9IlhYWFhYWFhYIiBzdHlsZT0iZmxleDoxO2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOm5vbmU7b3V0bGluZTpub25lO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6MS40MzhyZW07Y29sb3I6I2ZmZmZmZjtwYWRkaW5nOjEwcHggMCI+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IGlkPSJjYmtTdWNjZXNzIiBzdHlsZT0iZGlzcGxheTpub25lO3RleHQtYWxpZ246Y2VudGVyO3BhZGRpbmc6MjBweCAwIj4KICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjIuODc1cmVtO21hcmdpbi1ib3R0b206MTBweDtvcGFjaXR5Oi41Ij7inJM8L2Rpdj4KICAgICAgPGRpdiBzdHlsZT0iZm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjg3NXJlbTtjb2xvcjojZmZmZmZmO21hcmdpbi1ib3R0b206NnB4Ij7Ql9Cw0Y/QstC60LAg0L/RgNC40L3Rj9GC0LAhPC9kaXY+CiAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxLjAzN3JlbTtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjEuNjtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZiI+0JzRiyDQv9C10YDQtdC30LLQvtC90LjQvCDQstCw0Lwg0LIg0LHQu9C40LbQsNC50YjQtdC1INCy0YDQtdC80Y88L2Rpdj4KICAgIDwvZGl2PgogICAgPGJ1dHRvbiBpZD0iY2JrU3VibWl0IiBjbGFzcz0iY2J0biIgc3R5bGU9Im1hcmdpbi10b3A6MTRweCI+0J7RgtC/0YDQsNCy0LjRgtGMPC9idXR0b24+CiAgICA8YnV0dG9uIGlkPSJjYmtDbG9zZSIgc3R5bGU9ImRpc3BsYXk6YmxvY2s7d2lkdGg6MTAwJTttYXJnaW4tdG9wOjhweDtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyOm5vbmU7Y29sb3I6I2ZmZmZmZjtmb250LXNpemU6MC44MzhyZW07bGV0dGVyLXNwYWNpbmc6LjEyZW07Y3Vyc29yOnBvaW50ZXI7cGFkZGluZzo4cHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWYiPtCe0YLQvNC10L3QsDwvYnV0dG9uPgogIDwvZGl2Pgo8L2Rpdj4KCjxzY3JpcHQ+CnZhciBEQVRBID0gW3siYnJlZWQiOiLQkNCy0YHRgtGA0LDQu9C40LnRgdC60LDRjyDQvtCy0YfQsNGA0LrQsCAxNeKAkzI1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4NX0sImJyZWVkX2VuIjoiQXVzdHJhbGlhbiBTaGVwaGVyZCAxNeKAkzI1IGtnIiwiYnJlZWRfZXQiOiJBdXN0cmFhbGlhIGxhbWJha29lciAxNeKAkzI1IGtnIn0seyJicmVlZCI6ItCQ0LLRgdGC0YDQsNC70LjQudGB0LrQsNGPINC+0LLRh9Cw0YDQutCwIDI14oCTMzUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiQXVzdHJhbGlhbiBTaGVwaGVyZCAyNeKAkzM1IGtnIiwiYnJlZWRfZXQiOiJBdXN0cmFhbGlhIGxhbWJha29lciAyNeKAkzM1IGtnIn0seyJicmVlZCI6ItCQ0LrQuNGC0LAt0LjQvdGDIDIw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkFraXRhIEludSAyMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBa2l0YSBJbnUgMjDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC60LjRgtCwLdC40L3RgyDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQWtpdGEgSW51IG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkFraXRhIEludSDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCQ0LrQuNGC0LAt0LjQvdGDINGE0LvQsNGE0YTQuCAyMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJBa2l0YSBJbnUgZmx1ZmZ5IDIw4oCTNDAga2ciLCJicmVlZF9ldCI6IkFraXRhIEludSBwZWhtZWthcnZhbGluZSAyMOKAkzQwIGtnIn0seyJicmVlZCI6ItCQ0LrQuNGC0LAt0LjQvdGDINGE0LvQsNGE0YTQuCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQWtpdGEgSW51IGZsdWZmeSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJBa2l0YSBJbnUgcGVobWVrYXJ2YWxpbmUgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQkNC70LDQsdCw0LkgNDDigJM2MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkNlbnRyYWwgQXNpYW4gU2hlcGhlcmQgNDDigJM2MCBrZyIsImJyZWVkX2V0IjoiS2Vzay1BYXNpYSBsYW1iYWtvZXIgNDDigJM2MCBrZyJ9LHsiYnJlZWQiOiLQkNC70LDQsdCw0Lkg0LHQvtC70LXQtSA2MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjEwMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjExNSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEzMH0sImJyZWVkX2VuIjoiQ2VudHJhbCBBc2lhbiBTaGVwaGVyZCBvdmVyIDYwIGtnIiwiYnJlZWRfZXQiOiJLZXNrLUFhc2lhIGxhbWJha29lciDDvGxlIDYwIGtnIn0seyJicmVlZCI6ItCQ0LvRj9GB0LrQuNC90YHQutC40Lkg0LzQsNC70LDQvNGD0YIgMjDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQWxhc2thbiBNYWxhbXV0ZSAyMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBbGFza2EgbWFsYW11dXQgMjDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC70Y/RgdC60LjQvdGB0LrQuNC5INC80LDQu9Cw0LzRg9GCINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJBbGFza2FuIE1hbGFtdXRlIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkFsYXNrYSBtYWxhbXV1dCDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCQ0LvRj9GB0LrQuNC90YHQutC40Lkg0LzQsNC70LDQvNGD0YIg0YTQu9Cw0YTRhNC4IDIw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkFsYXNrYW4gTWFsYW11dGUgZmx1ZmZ5IDIw4oCTNDAga2ciLCJicmVlZF9ldCI6IkFsYXNrYSBtYWxhbXV1dCBwZWhtZWthcnZhbGluZSAyMOKAkzQwIGtnIn0seyJicmVlZCI6ItCQ0LvRj9GB0LrQuNC90YHQutC40Lkg0LzQsNC70LDQvNGD0YIg0YTQu9Cw0YTRhNC4INCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJBbGFza2FuIE1hbGFtdXRlIGZsdWZmeSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJBbGFza2EgbWFsYW11dXQgcGVobWVrYXJ2YWxpbmUgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutCw0Y8g0LDQutC40YLQsCAyMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBBa2l0YSAyMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBBa2l0YSAyMOKAkzQwIGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LDRjyDQsNC60LjRgtCwINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBBa2l0YSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBBa2l0YSDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LDRjyDQsNC60LjRgtCwINGE0LvQsNGE0YTQuCAyMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBBa2l0YSBmbHVmZnkgMjDigJM0MCBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2EgQWtpdGEgcGVobWVrYXJ2YWxpbmUgMjDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutCw0Y8g0LDQutC40YLQsCDRhNC70LDRhNGE0Lgg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkFtZXJpY2FuIEFraXRhIGZsdWZmeSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBBa2l0YSBwZWhtZWthcnZhbGluZSDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LjQuSDQutC+0LrQtdGALdGB0L/QsNC90LjQtdC70YwgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBDb2NrZXIgU3BhbmllbCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBrb2tlcnNwYW5qZWwgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutC40Lkg0LrQvtC60LXRgC3RgdC/0LDQvdC40LXQu9GMIDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4MH0sImJyZWVkX2VuIjoiQW1lcmljYW4gQ29ja2VyIFNwYW5pZWwgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2Ega29rZXJzcGFuamVsIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0JDQvNC10YDQuNC60LDQvdGB0LrQuNC5INGB0YLQsNGE0YTQvtGA0LTRiNC40YDRgdC60LjQuSDRgtC10YDRjNC10YAgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkFtZXJpY2FuIFN0YWZmb3Jkc2hpcmUgVGVycmllciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBTdGFmZm9yZHNoaXJlIHRlcmplciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LjQuSDRgdGC0LDRhNGE0L7RgNC00YjQuNGA0YHQutC40Lkg0YLQtdGA0YzQtdGAIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1fSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBTdGFmZm9yZHNoaXJlIFRlcnJpZXIgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2EgU3RhZmZvcmRzaGlyZSB0ZXJqZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC90LPQu9C40LnRgdC60LjQuSDQsdGD0LvRjNC00L7QsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NzB9LCJicmVlZF9lbiI6IkVuZ2xpc2ggQnVsbGRvZyIsImJyZWVkX2V0IjoiSW5nbGlzZSBidWxkb2cifSx7ImJyZWVkIjoi0JDQvdCz0LvQuNC50YHQutC40Lkg0LrQvtC60LXRgC3RgdC/0LDQvdC40LXQu9GMIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MH0sImJyZWVkX2VuIjoiRW5nbGlzaCBDb2NrZXIgU3BhbmllbCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJJbmdsaXNlIGtva2Vyc3BhbmplbCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCQ0L3Qs9C70LjQudGB0LrQuNC5INC60L7QutC10YAt0YHQv9Cw0L3QuNC10LvRjCAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODB9LCJicmVlZF9lbiI6IkVuZ2xpc2ggQ29ja2VyIFNwYW5pZWwgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiSW5nbGlzZSBrb2tlcnNwYW5qZWwgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQkNGE0LPQsNC9IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5MH0sImJyZWVkX2VuIjoiQWZnaGFuIEhvdW5kIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkFmZ2FuaXN0YW5pIGtvZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQkNGE0LPQsNC9IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjgwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDB9LCJicmVlZF9lbiI6IkFmZ2hhbiBIb3VuZCAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBZmdhbmlzdGFuaSBrb2VyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JHQsNGB0YHQtdGCLdGF0LDRg9C90LQgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkJhc3NldCBIb3VuZCAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJCYXNzZXRob3VuZCAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCR0LDRgdGB0LXRgi3RhdCw0YPQvdC0IDMw4oCTMzUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1fSwiYnJlZWRfZW4iOiJCYXNzZXQgSG91bmQgMzDigJMzNSBrZyIsImJyZWVkX2V0IjoiQmFzc2V0aG91bmQgMzDigJMzNSBrZyJ9LHsiYnJlZWQiOiLQkdC10YDQvdGB0LrQuNC5INC30LXQvdC90LXQvdGF0YPQvdC0IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IkJlcm5lc2UgTW91bnRhaW4gRG9nIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkJlcm5pIG3DpGdpa29lciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCR0LXRgNC90YHQutC40Lkg0LfQtdC90L3QtdC90YXRg9C90LQg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEzMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQmVybmVzZSBNb3VudGFpbiBEb2cgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiQmVybmkgbcOkZ2lrb2VyIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JHQuNCy0LXRgC3QudC+0YDQuiDQsdC+0LvQtdC1IDMsNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJCaWV3ZXIgWW9ya3NoaXJlIFRlcnJpZXIgb3ZlciAzLDUga2ciLCJicmVlZF9ldCI6IkJpZXdlciBZb3Jrc2hpcmUgVGVycmllciDDvGxlIDMsNSBrZyJ9LHsiYnJlZWQiOiLQkdC40LLQtdGALdC50L7RgNC6INC00L4gMyw1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkJpZXdlciBZb3Jrc2hpcmUgVGVycmllciB1cCB0byAzLDUga2ciLCJicmVlZF9ldCI6IkJpZXdlciBZb3Jrc2hpcmUgVGVycmllciBrdW5pIDMsNSBrZyJ9LHsiYnJlZWQiOiLQkdC40LPQu9GMIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjB9LCJicmVlZF9lbiI6IkJlYWdsZSAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJCaWlnZWwgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQkdC40LPQu9GMIDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjV9LCJicmVlZF9lbiI6IkJlYWdsZSAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiJCaWlnZWwgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQkdC40YjQvtC9LdGE0YDQuNC30LUgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkJpY2hvbiBGcmlzw6kgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJCacWhb24gRnJpc8OpIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQkdC40YjQvtC9LdGE0YDQuNC30LUg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkJpY2hvbiBGcmlzw6kgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiQmnFoW9uIEZyaXPDqSBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JHQvtC60YHQtdGAIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwfSwiYnJlZWRfZW4iOiJCb3hlciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJCb2tzZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQkdC+0LrRgdC10YAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkJveGVyIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkJva3NlciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCR0L7RgNC00LXRgC3QutC+0LvQu9C4IDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjgwfSwiYnJlZWRfZW4iOiJCb3JkZXIgQ29sbGllIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IkJvcmRlcmtvbGwgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQkdC+0YDQtNC10YAt0LrQvtC70LvQuCAyMOKAkzI1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkJvcmRlciBDb2xsaWUgMjDigJMyNSBrZyIsImJyZWVkX2V0IjoiQm9yZGVya29sbCAyMOKAkzI1IGtnIn0seyJicmVlZCI6ItCR0L7RgdGC0L7QvS3RgtC10YDRjNC10YAgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDV9LCJicmVlZF9lbiI6IkJvc3RvbiBUZXJyaWVyIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IkJvc3RvbmkgdGVyamVyIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0JHQvtGB0YLQvtC9LdGC0LXRgNGM0LXRgCA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwfSwiYnJlZWRfZW4iOiJCb3N0b24gVGVycmllciA14oCTMTAga2ciLCJicmVlZF9ldCI6IkJvc3RvbmkgdGVyamVyIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQkdGA0LDQsdCw0L3RgdC+0L0iLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjUwfSwiYnJlZWRfZW4iOiJHcmlmZm9uIEJydXhlbGxvaXMiLCJicmVlZF9ldCI6IkJyw7xzc2VsaSBncmlmb24ifSx7ImJyZWVkIjoi0JHRg9C70YzRgtC10YDRjNC10YAgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTB9LCJicmVlZF9lbiI6IkJ1bGwgVGVycmllciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJCdWxsdGVyamVyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JLQtdC70YzRiC3QutC+0YDQs9C4IDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjV9LCJicmVlZF9lbiI6IldlbHNoIENvcmdpIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IldhbGVzaSBrb3JnaSAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCS0LXQu9GM0Ygt0LrQvtGA0LPQuCAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjc1fSwiYnJlZWRfZW4iOiJXZWxzaCBDb3JnaSAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiJXYWxlc2kga29yZ2kgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQktC10YHRgi3RhdCw0LnQu9C10L3QtC3QstCw0LnRgi3RgtC10YDRjNC10YAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJXZXN0IEhpZ2hsYW5kIFdoaXRlIFRlcnJpZXIiLCJicmVlZF9ldCI6IkzDpMOkbmUtxaBvdGltYWEgdmFsZ2UgdGVyamVyIn0seyJicmVlZCI6ItCS0L7RgdGC0L7Rh9C90L7RgdC40LHQuNGA0YHQutCw0Y8g0LvQsNC50LrQsCAxOOKAkzI1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjg1fSwiYnJlZWRfZW4iOiJFYXN0IFNpYmVyaWFuIExhaWthIDE44oCTMjUga2ciLCJicmVlZF9ldCI6IklkYS1TaWJlcmkgbGFpa2EgMTjigJMyNSBrZyJ9LHsiYnJlZWQiOiLQktC+0YHRgtC+0YfQvdC+0YHQuNCx0LjRgNGB0LrQsNGPINC70LDQudC60LAg0LHQvtC70LXQtSAyNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjU1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IkVhc3QgU2liZXJpYW4gTGFpa2Egb3ZlciAyNSBrZyIsImJyZWVkX2V0IjoiSWRhLVNpYmVyaSBsYWlrYSDDvGxlIDI1IGtnIn0seyJicmVlZCI6ItCT0L7Qu9C00LXQvS3RgNC10YLRgNC40LLQtdGAIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiR29sZGVuIFJldHJpZXZlciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJLdWxkbmUgcmV0cmlpdmVyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JPQvtC70LTQtdC9LdGA0LXRgtGA0LjQstC10YAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjExMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiR29sZGVuIFJldHJpZXZlciAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJLdWxkbmUgcmV0cmlpdmVyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JPRgNC40YTRhNC+0L0iLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJHcmlmZm9uIiwiYnJlZWRfZXQiOiJHcmlmb24ifSx7ImJyZWVkIjoi0JTQsNC70LzQsNGC0LjQvSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NzB9LCJicmVlZF9lbiI6IkRhbG1hdGlhbiIsImJyZWVkX2V0IjoiRGFsbWFhdHNpYSBrb2VyIn0seyJicmVlZCI6ItCU0LbQtdC6LdGA0LDRgdGB0LXQuy3RgtC10YDRjNC10YAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NTB9LCJicmVlZF9lbiI6IkphY2sgUnVzc2VsbCBUZXJyaWVyIHNtb290aCIsImJyZWVkX2V0IjoiSmFjayBSdXNzZWxsaSB0ZXJqZXIgbMO8aGlrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0JTQttC10Lot0YDQsNGB0YHQtdC7LdGC0LXRgNGM0LXRgCDQttC10YHRgtC60L7RiNC10YDRgdGC0L3Ri9C5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiSmFjayBSdXNzZWxsIFRlcnJpZXIgd2lyZS1oYWlyZWQiLCJicmVlZF9ldCI6IkphY2sgUnVzc2VsbGkgdGVyamVyIGthcnVrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0JTQvtCx0LXRgNC80LDQvSAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjgwfSwiYnJlZWRfZW4iOiJEb2Jlcm1hbm4gMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiRG9iZXJtYW5uIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JTQvtCx0LXRgNC80LDQvSDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjk1fSwiYnJlZWRfZW4iOiJEb2Jlcm1hbm4gb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiRG9iZXJtYW5uIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JfQsNC/0LDQtNC90L7RgdC40LHQuNGA0YHQutCw0Y8g0LvQsNC50LrQsCAxOOKAkzI1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjg1fSwiYnJlZWRfZW4iOiJXZXN0IFNpYmVyaWFuIExhaWthIDE44oCTMjUga2ciLCJicmVlZF9ldCI6IkzDpMOkbmUtU2liZXJpIGxhaWthIDE44oCTMjUga2cifSx7ImJyZWVkIjoi0JfQvtC70L7RgtC40YHRgtGL0Lkg0YDQtdGC0YDQuNCy0LXRgCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiR29sZGVuIFJldHJpZXZlciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJLdWxkbmUgcmV0cmlpdmVyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JfQvtC70L7RgtC40YHRgtGL0Lkg0YDQtdGC0YDQuNCy0LXRgCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTEwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTEwfSwiYnJlZWRfZW4iOiJHb2xkZW4gUmV0cmlldmVyIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6Ikt1bGRuZSByZXRyaWl2ZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQmNGA0LvQsNC90LTRgdC60LjQuSDQvNGP0LPQutC+0YjQtdGA0YHRgtC90YvQuSDQv9GI0LXQvdC40YfQvdGL0Lkg0YLQtdGA0YzQtdGAIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwfSwiYnJlZWRfZW4iOiJJcmlzaCBTb2Z0IENvYXRlZCBXaGVhdGVuIFRlcnJpZXIiLCJicmVlZF9ldCI6IklpcmkgcGVobWVrYXJ2YW5lIG5pc3V2w6RydmkgdGVyamVyIn0seyJicmVlZCI6ItCY0YDQu9Cw0L3QtNGB0LrQuNC5INGC0LXRgNGM0LXRgCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MCwi0KLRgNC40LzQvNC40L3QsyI6NzV9LCJicmVlZF9lbiI6IklyaXNoIFRlcnJpZXIiLCJicmVlZF9ldCI6IklpcmkgdGVyamVyIn0seyJicmVlZCI6ItCY0YHQv9Cw0L3RgdC60LjQuSDQs9Cw0LvRjNCz0L4gMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo3MH0sImJyZWVkX2VuIjoiU3BhbmlzaCBHYWxnbyAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJIaXNwYWFuaWEgZ2FsZ28gMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQmNGB0L/QsNC90YHQutC40Lkg0LPQsNC70YzQs9C+IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6ODB9LCJicmVlZF9lbiI6IlNwYW5pc2ggR2FsZ28gMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiSGlzcGFhbmlhIGdhbGdvIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JnQvtGA0LrRiNC40YDRgdC60LjQuSDRgtC10YDRjNC10YAg0LHQvtC70LXQtSAzLDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiWW9ya3NoaXJlIFRlcnJpZXIgb3ZlciAzLDUga2ciLCJicmVlZF9ldCI6IllvcmtzaGlyZSB0ZXJqZXIgw7xsZSAzLDUga2cifSx7ImJyZWVkIjoi0JnQvtGA0LrRiNC40YDRgdC60LjQuSDRgtC10YDRjNC10YAg0LTQviAzLDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiWW9ya3NoaXJlIFRlcnJpZXIgdXAgdG8gMyw1IGtnIiwiYnJlZWRfZXQiOiJZb3Jrc2hpcmUgdGVyamVyIGt1bmkgMyw1IGtnIn0seyJicmVlZCI6ItCa0LDQstCw0LvQtdGALdC60LjQvdCzLdGH0LDRgNC70YzQty3RgdC/0LDQvdC40LXQu9GMIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MH0sImJyZWVkX2VuIjoiQ2F2YWxpZXIgS2luZyBDaGFybGVzIFNwYW5pZWwgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiQ2F2YWxpZXIgS2luZyBDaGFybGVzIFNwYW5pZWwgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQmtCw0LLQsNC70LXRgC3QutC40L3Qsy3Rh9Cw0YDQu9GM0Lct0YHQv9Cw0L3QuNC10LvRjCA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiQ2F2YWxpZXIgS2luZyBDaGFybGVzIFNwYW5pZWwgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJDYXZhbGllciBLaW5nIENoYXJsZXMgU3BhbmllbCA14oCTMTAga2cifSx7ImJyZWVkIjoi0JrQsNC90LUt0LrQvtGA0YHQviA0MOKAkzYwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NX0sImJyZWVkX2VuIjoiQ2FuZSBDb3JzbyA0MOKAkzYwIGtnIiwiYnJlZWRfZXQiOiJDYW5lIENvcnNvIDQw4oCTNjAga2cifSx7ImJyZWVkIjoi0JrQsNC90LUt0LrQvtGA0YHQviDQsdC+0LvQtdC1IDYwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6OTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjoxMDV9LCJicmVlZF9lbiI6IkNhbmUgQ29yc28gb3ZlciA2MCBrZyIsImJyZWVkX2V0IjoiQ2FuZSBDb3JzbyDDvGxlIDYwIGtnIn0seyJicmVlZCI6ItCa0LDRgNC10LvQvi3RhNC40L3RgdC60LDRjyDQu9Cw0LnQutCwINC00L4gMTMg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjV9LCJicmVlZF9lbiI6IkthcmVsaWFuLUZpbm5pc2ggTGFpa2EgdXAgdG8gMTMga2ciLCJicmVlZF9ldCI6IkthcmphbGEtU29vbWUgbGFpa2Ega3VuaSAxMyBrZyJ9LHsiYnJlZWQiOiLQmtC40YLQsNC50YHQutCw0Y8g0YXQvtGF0LvQsNGC0LDRjyDQs9C+0LvQsNGPIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMyLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDIsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJDaGluZXNlIENyZXN0ZWQgaGFpcmxlc3MgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJIaWluYSBoYXJqYWtvZXIga2FydmF0dSA14oCTMTAga2cifSx7ImJyZWVkIjoi0JrQuNGC0LDQudGB0LrQsNGPINGF0L7RhdC70LDRgtCw0Y8g0LPQvtC70LDRjyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyOCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiQ2hpbmVzZSBDcmVzdGVkIGhhaXJsZXNzIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IkhpaW5hIGhhcmpha29lciBrYXJ2YXR1IGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQmtC40YLQsNC50YHQutCw0Y8g0YXQvtGF0LvQsNGC0LDRjyDQv9GD0YXQvtCy0LDRjyA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiQ2hpbmVzZSBDcmVzdGVkIHBvd2RlcnB1ZmYgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJIaWluYSBoYXJqYWtvZXIgUG93ZGVycHVmZiA14oCTMTAga2cifSx7ImJyZWVkIjoi0JrQuNGC0LDQudGB0LrQsNGPINGF0L7RhdC70LDRgtCw0Y8g0L/Rg9GF0L7QstCw0Y8g0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkNoaW5lc2UgQ3Jlc3RlZCBwb3dkZXJwdWZmIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IkhpaW5hIGhhcmpha29lciBQb3dkZXJwdWZmIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQmtC+0LrQsNC/0YMgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzV9LCJicmVlZF9lbiI6IkNvY2thcG9vIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiQ29ja2Fwb28gNeKAkzEwIGtnIn0seyJicmVlZCI6ItCa0L7QutCw0L/RgyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2NX0sImJyZWVkX2VuIjoiQ29ja2Fwb28gdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiQ29ja2Fwb28ga3VuaSA1IGtnIn0seyJicmVlZCI6ItCa0L7Qu9C70LggMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJDb2xsaWUgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiS29sbCAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCa0L7Qu9C70LggMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjExMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiQ29sbGllIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IktvbGwgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQmtC+0LzQvtC90LTQvtGAIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMTB9LCJicmVlZF9lbiI6IktvbW9uZG9yIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IktvbW9uZG9yIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JrQvtC80L7QvdC00L7RgCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTMwfSwiYnJlZWRfZW4iOiJLb21vbmRvciBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJLb21vbmRvciDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNC+0YAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjcwfSwiYnJlZWRfZW4iOiJMYWJyYWRvciBSZXRyaWV2ZXIgc21vb3RoIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9yaSByZXRyaWl2ZXIgbMO8aGlrYXJ2YWxpbmUgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQm9Cw0LHRgNCw0LTQvtGAINCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdGL0LkgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjU1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4MH0sImJyZWVkX2VuIjoiTGFicmFkb3IgUmV0cmlldmVyIHNtb290aCAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvcmkgcmV0cmlpdmVyIGzDvGhpa2FydmFsaW5lIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00L7RgCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3Ri9C5INCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjgwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTV9LCJicmVlZF9lbiI6IkxhYnJhZG9yIFJldHJpZXZlciBzbW9vdGggb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiTGFicmFkb3JpIHJldHJpaXZlciBsw7xoaWthcnZhbGluZSDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNC+0YAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90YvQuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkxhYnJhZG9yIFJldHJpZXZlciBsb25nLWNvYXRlZCAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvcmkgcmV0cmlpdmVyIHBpa2thcnZhbGluZSAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNC+0YAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90YvQuSAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTEwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJMYWJyYWRvciBSZXRyaWV2ZXIgbG9uZy1jb2F0ZWQgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiTGFicmFkb3JpIHJldHJpaXZlciBwaWtrYXJ2YWxpbmUgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQm9Cw0LHRgNCw0LTQvtGAINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdGL0Lkg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEzMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiTGFicmFkb3IgUmV0cmlldmVyIGxvbmctY29hdGVkIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9yaSByZXRyaWl2ZXIgcGlra2FydmFsaW5lIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00YPQtNC10LvRjCAxMOKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IkxhYnJhZG9vZGxlIDEw4oCTMjAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9vZGxlIDEw4oCTMjAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00YPQtNC10LvRjCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTB9LCJicmVlZF9lbiI6IkxhYnJhZG9vZGxlIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9vZGxlIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00YPQtNC10LvRjCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwfSwiYnJlZWRfZW4iOiJMYWJyYWRvb2RsZSAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvb2RsZSAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCb0LXQstGA0LXRgtC60LAgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MH0sImJyZWVkX2VuIjoiSXRhbGlhbiBHcmV5aG91bmQgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJJdGFhbGlhIHZpbmRrb2VyIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQm9C10LLRgNC10YLQutCwINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjI1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MzV9LCJicmVlZF9lbiI6Ikl0YWxpYW4gR3JleWhvdW5kIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6Ikl0YWFsaWEgdmluZGtvZXIga3VuaSA1IGtnIn0seyJicmVlZCI6ItCb0YXQsNGB0YHQutC40Lkg0LDQv9GB0L4gNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzV9LCJicmVlZF9lbiI6IkxoYXNhIEFwc28gNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJMaGFzYSBBcHNvIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQm9GF0LDRgdGB0LrQuNC5INCw0L/RgdC+INC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjY1fSwiYnJlZWRfZW4iOiJMaGFzYSBBcHNvIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IkxoYXNhIEFwc28ga3VuaSA1IGtnIn0seyJicmVlZCI6ItCc0LDQu9GM0YLQtdC30LUiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6Ik1hbHRlc2UiLCJicmVlZF9ldCI6Ik1hbHRhIGJvbG9uZWVzIn0seyJicmVlZCI6ItCc0LDQu9GM0YLQuNC50YHQutCw0Y8g0LHQvtC70L7QvdC60LAgNeKAkzgg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2NX0sImJyZWVkX2VuIjoiTWFsdGVzZSBCb2xvZ25lc2UgNeKAkzgga2ciLCJicmVlZF9ldCI6Ik1hbHRhIGJvbG9uZWVzIDXigJM4IGtnIn0seyJicmVlZCI6ItCc0LDQu9GM0YLQuNC50YHQutCw0Y8g0LHQvtC70L7QvdC60LAg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6Ik1hbHRlc2UgQm9sb2duZXNlIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6Ik1hbHRhIGJvbG9uZWVzIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQnNCw0LvRjNGC0LjQv9GDIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MH0sImJyZWVkX2VuIjoiTWFsdGlwb28gMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiTWFsdGlwdXUgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQnNCw0LvRjNGC0LjQv9GDIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJNYWx0aXBvbyA14oCTMTAga2ciLCJicmVlZF9ldCI6Ik1hbHRpcHV1IDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQnNCw0LvRjNGC0LjQv9GDINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJNYWx0aXBvbyB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJNYWx0aXB1dSBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JzQtdGC0LjRgSDQutGA0YPQv9C90YvQuSAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJNaXhlZCBicmVlZCBsYXJnZSAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJTZWdhdmVyZCBzdXVyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JzQtdGC0LjRgSDQutGA0YPQv9C90YvQuSDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTIwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTIwfSwiYnJlZWRfZW4iOiJNaXhlZCBicmVlZCBsYXJnZSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJTZWdhdmVyZCBzdXVyIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JzQtdGC0LjRgSDQvNC10LvQutC40LkgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6Ik1peGVkIGJyZWVkIHNtYWxsIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiU2VnYXZlcmQgdsOkaWtlIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQnNC10YLQuNGBINC80LXQu9C60LjQuSDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1MH0sImJyZWVkX2VuIjoiTWl4ZWQgYnJlZWQgc21hbGwgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiU2VnYXZlcmQgdsOkaWtlIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQnNC10YLQuNGBINGB0YDQtdC00L3QuNC5IDEw4oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjcwfSwiYnJlZWRfZW4iOiJNaXhlZCBicmVlZCBtZWRpdW0gMTDigJMyMCBrZyIsImJyZWVkX2V0IjoiU2VnYXZlcmQga2Vza21pbmUgMTDigJMyMCBrZyJ9LHsiYnJlZWQiOiLQnNC10YLQuNGBINGB0YDQtdC00L3QuNC5IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjYwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjg1fSwiYnJlZWRfZW4iOiJNaXhlZCBicmVlZCBtZWRpdW0gMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiU2VnYXZlcmQga2Vza21pbmUgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQnNC40YLRgtC10LvRjNGI0L3QsNGD0YbQtdGAIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MCwi0KLRgNC40LzQvNC40L3QsyI6NzV9LCJicmVlZF9lbiI6IlN0YW5kYXJkIFNjaG5hdXplciAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJTdGFuZGFyZMWhbmF1dHNlciAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCc0LjRgtGC0LXQu9GM0YjQvdCw0YPRhtC10YAgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwLCLQotGA0LjQvNC80LjQvdCzIjo4NX0sImJyZWVkX2VuIjoiU3RhbmRhcmQgU2NobmF1emVyIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IlN0YW5kYXJkxaFuYXV0c2VyIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0JzQvtC/0YEiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjUwfSwiYnJlZWRfZW4iOiJQdWciLCJicmVlZF9ldCI6Ik1vcHMifSx7ImJyZWVkIjoi0J3QtdCy0YHQutCw0Y8g0L7RgNGF0LjQtNC10Y8iLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6Ik5ldmEgT3JjaGlkIiwiYnJlZWRfZXQiOiJOZWV2YSBvcmhpZGVlIn0seyJicmVlZCI6ItCd0LXQvNC10YbQutCw0Y8g0L7QstGH0LDRgNC60LAgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiR2VybWFuIFNoZXBoZXJkIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IlNha3NhIGxhbWJha29lciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCd0LXQvNC10YbQutCw0Y8g0L7QstGH0LDRgNC60LAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6Ikdlcm1hbiBTaGVwaGVyZCAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJTYWtzYSBsYW1iYWtvZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQndC10LzQtdGG0LrQsNGPINC+0LLRh9Cw0YDQutCwINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJHZXJtYW4gU2hlcGhlcmQgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiU2Frc2EgbGFtYmFrb2VyIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0KjQstC10LnRhtCw0YDRgdC60LDRjyDQvtCy0YfQsNGA0LrQsCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJTd2lzcyBTaGVwaGVyZCAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiLFoHZlaXRzaSBsYW1iYWtvZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQqNCy0LXQudGG0LDRgNGB0LrQsNGPINC+0LLRh9Cw0YDQutCwIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJTd2lzcyBTaGVwaGVyZCAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiLFoHZlaXRzaSBsYW1iYWtvZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQqNCy0LXQudGG0LDRgNGB0LrQsNGPINC+0LLRh9Cw0YDQutCwINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJTd2lzcyBTaGVwaGVyZCBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiLFoHZlaXRzaSBsYW1iYWtvZXIgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQndC+0YDQstC40Yct0YLQtdGA0YzQtdGAIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiTm9yd2ljaCBUZXJyaWVyIiwiYnJlZWRfZXQiOiJOb3J3aXTFoWkgdGVyamVyIn0seyJicmVlZCI6ItCd0L7RgNGE0L7Qu9C6LdGC0LXRgNGM0LXRgCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MCwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6Ik5vcmZvbGsgVGVycmllciIsImJyZWVkX2V0IjoiTm9yZm9sa2kgdGVyamVyIn0seyJicmVlZCI6ItCd0YzRjtGE0LDRg9C90LTQu9C10L3QtCA0MOKAkzYwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTMwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJOZXdmb3VuZGxhbmQgNDDigJM2MCBrZyIsImJyZWVkX2V0IjoiTmV3Zm91bmRsYW5kaSBrb2VyIDQw4oCTNjAga2cifSx7ImJyZWVkIjoi0J3RjNGO0YTQsNGD0L3QtNC70LXQvdC0INCx0L7Qu9C10LUgNjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoxMDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjoxMTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjE1MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEzMH0sImJyZWVkX2VuIjoiTmV3Zm91bmRsYW5kIG92ZXIgNjAga2ciLCJicmVlZF9ldCI6Ik5ld2ZvdW5kbGFuZGkga29lciDDvGxlIDYwIGtnIn0seyJicmVlZCI6ItCf0LDQv9C40LnQvtC9Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJQYXBpbGxvbiIsImJyZWVkX2V0IjoiUGFwaWxsb24ifSx7ImJyZWVkIjoi0J/QtdC60LjQvdC10YEgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IlBla2luZ2VzZSA14oCTMTAga2ciLCJicmVlZF9ldCI6IlBla2luZXNpIGtvZXIgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCf0LXQutC40L3QtdGBINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJQZWtpbmdlc2UgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiUGVraW5lc2kga29lciBrdW5pIDUga2cifSx7ImJyZWVkIjoi0J/Rg9C00LXQu9GMINCx0L7Qu9GM0YjQvtC5IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5MH0sImJyZWVkX2VuIjoiU3RhbmRhcmQgUG9vZGxlIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IlN0YW5kYXJkcHV1ZGVsIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0J/Rg9C00LXQu9GMINCx0L7Qu9GM0YjQvtC5IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjgwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDB9LCJicmVlZF9lbiI6IlN0YW5kYXJkIFBvb2RsZSAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJTdGFuZGFyZHB1dWRlbCAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCf0YPQtNC10LvRjCDQutCw0YDQu9C40LrQvtCy0YvQuSA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiTWluaWF0dXJlIFBvb2RsZSA14oCTMTAga2ciLCJicmVlZF9ldCI6IkvDpMOkYnVzcHV1ZGVsIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQn9GD0LTQtdC70Ywg0LzQsNC70YvQuSAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IlNtYWxsIFBvb2RsZSAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJWw6Rpa2UgcHV1ZGVsIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0J/Rg9C00LXQu9GMINC80LDQu9GL0LkgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwfSwiYnJlZWRfZW4iOiJTbWFsbCBQb29kbGUgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiVsOkaWtlIHB1dWRlbCAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCf0YPQtNC10LvRjCDRgtC+0Lkg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IlRveSBQb29kbGUgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiTcOkbmd1YXNqYSBwdXVkZWwga3VuaSA1IGtnIn0seyJicmVlZCI6ItCg0LjQt9C10L3RiNC90LDRg9GG0LXRgCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwLCLQotGA0LjQvNC80LjQvdCzIjoxMTB9LCJicmVlZF9lbiI6IkdpYW50IFNjaG5hdXplciAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJTdXVyxaFuYXV0c2VyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0KDQuNC30LXQvdGI0L3QsNGD0YbQtdGAINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMjAsItCi0YDQuNC80LzQuNC90LMiOjEyNX0sImJyZWVkX2VuIjoiR2lhbnQgU2NobmF1emVyIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IlN1dXLFoW5hdXRzZXIgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQoNGD0YHRgdC60LDRjyDRhtCy0LXRgtC90LDRjyDQsdC+0LvQvtC90LrQsCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiUnVzc2lhbiBDb2xvcmVkIExhcGRvZyIsImJyZWVkX2V0IjoiVmVuZSB2w6RydmlsaW5lIHPDvGxla29lciJ9LHsiYnJlZWQiOiLQoNGD0YHRgdC60LjQuSDQvtGF0L7RgtC90LjRh9C40Lkg0YHQv9Cw0L3QuNC10LvRjCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IlJ1c3NpYW4gU3BhbmllbCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJWZW5lIGphaGlzcGFuamVsIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KDRg9GB0YHQutC40Lkg0L7RhdC+0YLQvdC40YfQuNC5INGB0L/QsNC90LjQtdC70YwgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwfSwiYnJlZWRfZW4iOiJSdXNzaWFuIFNwYW5pZWwgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiVmVuZSBqYWhpc3BhbmplbCAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCg0YPRgdGB0LrQuNC5INGC0L7QuSDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3Ri9C5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjI1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MzV9LCJicmVlZF9lbiI6IlJ1c3NpYW4gVG95IHNtb290aCIsImJyZWVkX2V0IjoiVmVuZSBUb3kgbMO8aGlrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0KDRg9GB0YHQutC40Lkg0YLQvtC5INC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdGL0LkiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IlJ1c3NpYW4gVG95IGxvbmctY29hdGVkIiwiYnJlZWRfZXQiOiJWZW5lIFRveSBwaWtrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0KDRg9GB0YHQutC40Lkg0YfQtdGA0L3Ri9C5INGC0LXRgNGM0LXRgCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwfSwiYnJlZWRfZW4iOiJCbGFjayBSdXNzaWFuIFRlcnJpZXIgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiTXVzdCBWZW5lIHRlcmplciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCg0YPRgdGB0LrQuNC5INGH0LXRgNC90YvQuSDRgtC10YDRjNC10YAg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjc1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEyMH0sImJyZWVkX2VuIjoiQmxhY2sgUnVzc2lhbiBUZXJyaWVyIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6Ik11c3QgVmVuZSB0ZXJqZXIgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQoNGD0YHRgdC60L4t0LXQstGA0L7Qv9C10LnRgdC60LDRjyDQu9Cw0LnQutCwIDIw4oCTMjgg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IlJ1c3NpYW4tRXVyb3BlYW4gTGFpa2EgMjDigJMyOCBrZyIsImJyZWVkX2V0IjoiVmVuZS1FdXJvb3BhIGxhaWthIDIw4oCTMjgga2cifSx7ImJyZWVkIjoi0KHQsNC80L7QtdC0IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IlNhbW95ZWQgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiU2Ftb2plZWQgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQodCw0LzQvtC10LQgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IlNhbW95ZWQgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiU2Ftb2plZWQgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQodC10YLRgtC10YAg0LDQvdCz0LvQuNC50YHQutC40LkgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwfSwiYnJlZWRfZW4iOiJFbmdsaXNoIFNldHRlciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJJbmdsaXNlIHNldHRlciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCh0LXRgtGC0LXRgCDQs9C+0YDQtNC+0L0gMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMH0sImJyZWVkX2VuIjoiR29yZG9uIFNldHRlciAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJHb3Jkb25pIHNldHRlciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCh0LXRgtGC0LXRgCDQuNGA0LvQsNC90LTRgdC60LjQuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTB9LCJicmVlZF9lbiI6IklyaXNoIFNldHRlciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJJaXJpIHNldHRlciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCh0LjQsdCwLdC40L3RgyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjYwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NzB9LCJicmVlZF9lbiI6IlNoaWJhIEludSIsImJyZWVkX2V0IjoiU2hpYmEgSW51In0seyJicmVlZCI6ItCh0LjQu9C40YXQtdC8LdGC0LXRgNGM0LXRgCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MCwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6IlNlYWx5aGFtIFRlcnJpZXIiLCJicmVlZF9ldCI6IlNlYWx5aGFtaSB0ZXJqZXIifSx7ImJyZWVkIjoi0KHQutC+0YLRhy3RgtC10YDRjNC10YAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJTY290dGlzaCBUZXJyaWVyIiwiYnJlZWRfZXQiOiLFoG90aSB0ZXJqZXIifSx7ImJyZWVkIjoi0KLQsNC60YHQsCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3QsNGPINC60LDRgNC70LjQutC+0LLQsNGPIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo1MH0sImJyZWVkX2VuIjoiRGFjaHNodW5kIHNtb290aCBtaW5pYXR1cmUgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIgbMO8aGlrYXJ2YWxpbmUga8Okw6RidXMgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90LDRjyDQutGA0L7Qu9C40YfRjNGPINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjI1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo0NX0sImJyZWVkX2VuIjoiRGFjaHNodW5kIHNtb290aCByYWJiaXQgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiVGFrc2lrb2VyIGzDvGhpa2FydmFsaW5lIGvDvMO8bGlrIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdCw0Y8g0YHRgtCw0L3QtNCw0YDRgtC90LDRjyAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjYwfSwiYnJlZWRfZW4iOiJEYWNoc2h1bmQgc21vb3RoIHN0YW5kYXJkIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBsw7xoaWthcnZhbGluZSBzdGFuZGFyZCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90LDRjyDQutCw0YDQu9C40LrQvtCy0LDRjyA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiRGFjaHNodW5kIGxvbmctY29hdGVkIG1pbmlhdHVyZSA14oCTMTAga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBwaWtrYXJ2YWxpbmUga8Okw6RidXMgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90LDRjyDQutGA0L7Qu9C40YfRjNGPINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJEYWNoc2h1bmQgbG9uZy1jb2F0ZWQgcmFiYml0IHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBwaWtrYXJ2YWxpbmUga8O8w7xsaWsga3VuaSA1IGtnIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90LDRjyDRgdGC0LDQvdC00LDRgNGC0L3QsNGPIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MH0sImJyZWVkX2VuIjoiRGFjaHNodW5kIGxvbmctY29hdGVkIHN0YW5kYXJkIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBwaWtrYXJ2YWxpbmUgc3RhbmRhcmQgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINC20LXRgdGC0LrQvtGI0LXRgNGB0YLQvdCw0Y8g0LrQsNGA0LvQuNC60L7QstCw0Y8gNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJEYWNoc2h1bmQgd2lyZS1oYWlyZWQgbWluaWF0dXJlIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiVGFrc2lrb2VyIGthcnVrYXJ2YWxpbmUga8Okw6RidXMgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90LDRjyDQutGA0L7Qu9C40YfRjNGPINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1LCLQotGA0LjQvNC80LjQvdCzIjo1NX0sImJyZWVkX2VuIjoiRGFjaHNodW5kIHdpcmUtaGFpcmVkIHJhYmJpdCB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIga2FydWthcnZhbGluZSBrw7zDvGxpayBrdW5pIDUga2cifSx7ImJyZWVkIjoi0KLQsNC60YHQsCDQttC10YHRgtC60L7RiNC10YDRgdGC0L3QsNGPINGB0YLQsNC90LTQsNGA0YLQvdCw0Y8gMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwLCLQotGA0LjQvNC80LjQvdCzIjo3NX0sImJyZWVkX2VuIjoiRGFjaHNodW5kIHdpcmUtaGFpcmVkIHN0YW5kYXJkIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBrYXJ1a2FydmFsaW5lIHN0YW5kYXJkIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KPQuNC/0L/QtdGCIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1fSwiYnJlZWRfZW4iOiJXaGlwcGV0IDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IldoaXBwZXQgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQo9C40L/Qv9C10YIgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTB9LCJicmVlZF9lbiI6IldoaXBwZXQgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiV2hpcHBldCAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCk0LjQvdGB0LrQuNC5INC70LDQv9GF0YPQvdC0IDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4NX0sImJyZWVkX2VuIjoiRmlubmlzaCBMYXBwaHVuZCAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiJTb29tZSBsYW1iYWtvZXIgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQpNC40L3RgdC60LjQuSDQu9Cw0L/RhdGD0L3QtCAyMOKAkzI0INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTB9LCJicmVlZF9lbiI6IkZpbm5pc2ggTGFwcGh1bmQgMjDigJMyNCBrZyIsImJyZWVkX2V0IjoiU29vbWUgbGFtYmFrb2VyIDIw4oCTMjQga2cifSx7ImJyZWVkIjoi0KTQvtC60YHRgtC10YDRjNC10YAg0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90YvQuSAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzAsItCi0YDQuNC80LzQuNC90LMiOjc1fSwiYnJlZWRfZW4iOiJXaXJlIEZveCBUZXJyaWVyIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IkthcnVrYXJ2YWxpbmUgZm94dGVyamVyIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KTQvtC60YHRgtC10YDRjNC10YAg0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90YvQuSA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MCwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6IldpcmUgRm94IFRlcnJpZXIgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJLYXJ1a2FydmFsaW5lIGZveHRlcmplciA14oCTMTAga2cifSx7ImJyZWVkIjoi0KTRgNCw0L3RhtGD0LfRgdC60LjQuSDQsdGD0LvRjNC00L7QsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjB9LCJicmVlZF9lbiI6IkZyZW5jaCBCdWxsZG9nIiwiYnJlZWRfZXQiOiJQcmFudHN1c2UgYnVsZG9nIn0seyJicmVlZCI6ItCl0LDRgdC60LggMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiU2liZXJpYW4gSHVza3kgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiU2liZXJpIGh1c2t5IDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0KXQsNGB0LrQuCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiU2liZXJpYW4gSHVza3kgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiU2liZXJpIGh1c2t5IDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0KbQstC10YDQs9GI0L3QsNGD0YbQtdGAIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MCwi0KLRgNC40LzQvNC40L3QsyI6NzV9LCJicmVlZF9lbiI6Ik1pbmlhdHVyZSBTY2huYXV6ZXIgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiS8Okw6RidXPFoW5hdXRzZXIgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQptCy0LXRgNCz0YjQvdCw0YPRhtC10YAgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJNaW5pYXR1cmUgU2NobmF1emVyIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiS8Okw6RidXPFoW5hdXRzZXIgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCn0LDRgy3Rh9Cw0YMgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJDaG93IENob3cgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiQ2hvdyBDaG93IDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0KfQsNGDLdGH0LDRgyAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTEwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJDaG93IENob3cgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiQ2hvdyBDaG93IDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0KfQuNGF0YPQsNGF0YPQsCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3Ri9C5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjI1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MzV9LCJicmVlZF9lbiI6IkNoaWh1YWh1YSBzbW9vdGgiLCJicmVlZF9ldCI6IlTFoWlodWFodWEgbMO8aGlrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0KfQuNGF0YPQsNGF0YPQsCDQtNC70LjQvdC90L7RiNC10YDRgdGC0L3Ri9C5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJDaGlodWFodWEgbG9uZy1jb2F0ZWQiLCJicmVlZF9ldCI6IlTFoWlodWFodWEgcGlra2FydmFsaW5lIn0seyJicmVlZCI6ItCo0LDRgNC/0LXQuSAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjY1fSwiYnJlZWRfZW4iOiJTaGFyIFBlaSAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiLFoGFyLVBlaSAxNeKAkzIwIGtnIn0seyJicmVlZCI6ItCo0LDRgNC/0LXQuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjcwfSwiYnJlZWRfZW4iOiJTaGFyIFBlaSAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiLFoGFyLVBlaSAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCo0LXQu9GC0LgiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2MH0sImJyZWVkX2VuIjoiU2hldGxhbmQgU2hlZXBkb2ciLCJicmVlZF9ldCI6IsWgZXRsYW5kaSBsYW1iYWtvZXIifSx7ImJyZWVkIjoi0KjQuC3RgtGG0YMgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IlNoaWggVHp1IDXigJMxMCBrZyIsImJyZWVkX2V0IjoiU2hpaCBUenUgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCo0Lgt0YLRhtGDINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJTaGloIFR6dSB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJTaGloIFR6dSBrdW5pIDUga2cifSx7ImJyZWVkIjoi0KjQvdCw0YPRhtC10YAg0LzQuNC90LjQsNGC0Y7RgNC90YvQuSDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiTWluaWF0dXJlIFNjaG5hdXplciB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJLw6TDpGJ1c8WhbmF1dHNlciBrdW5pIDUga2cifSx7ImJyZWVkIjoi0KjQv9C40YYg0L3QtdC80LXRhtC60LjQuSAvINC/0L7QvNC10YDQsNC90YHQutC40Lkg0LHQvtC70LXQtSAzLDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjYwfSwiYnJlZWRfZW4iOiJHZXJtYW4gU3BpdHogLyBQb21lcmFuaWFuIG92ZXIgMyw1IGtnIiwiYnJlZWRfZXQiOiJTYWtzYSBzcGl0cyAvIFBvbWVyYW5pYW4gw7xsZSAzLDUga2cifSx7ImJyZWVkIjoi0KjQv9C40YYg0L3QtdC80LXRhtC60LjQuSAvINC/0L7QvNC10YDQsNC90YHQutC40Lkg0LTQviAzLDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjU1fSwiYnJlZWRfZW4iOiJHZXJtYW4gU3BpdHogLyBQb21lcmFuaWFuIHVwIHRvIDMsNSBrZyIsImJyZWVkX2V0IjoiU2Frc2Egc3BpdHMgLyBQb21lcmFuaWFuIGt1bmkgMyw1IGtnIn0seyJicmVlZCI6ItCo0L/QuNGGINGP0L/QvtC90YHQutC40LkiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2MH0sImJyZWVkX2VuIjoiSmFwYW5lc2UgU3BpdHoiLCJicmVlZF9ldCI6IkphYXBhbmkgc3BpdHMifSx7ImJyZWVkIjoi0KnQtdC90LrQuCIsInNlcnZpY2VzIjp7ItCS0YHRjyDQv9GA0L7Qs9GA0LDQvNC80LAiOjU1fSwiYnJlZWRfZW4iOiJQdXBwaWVzIiwiYnJlZWRfZXQiOiJLdXRzaWthZCJ9LHsiYnJlZWQiOiLQrdGB0YLQvtC90YHQutCw0Y8g0LPQvtC90YfQsNGPIDE14oCTMjUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwfSwiYnJlZWRfZW4iOiJFc3RvbmlhbiBIb3VuZCAxNeKAkzI1IGtnIiwiYnJlZWRfZXQiOiJFZXN0aSBoYWdpamFzIDE14oCTMjUga2cifSx7ImJyZWVkIjoi0K/Qv9C+0L3RgdC60LjQuSDRhdC40L0iLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkphcGFuZXNlIENoaW4iLCJicmVlZF9ldCI6IkphYXBhbmkgQ2hpbiJ9LHsiYnJlZWQiOiLQmtC+0YjQutCwINC60L7RgNC+0YLQutC+0YjQtdGA0YHRgtC90LDRjyIsInNlcnZpY2VzIjp7ItCS0YvRh9C10YEiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiQ2F0IHNob3J0LWhhaXJlZCIsImJyZWVkX2V0IjoiS2FzcyBsw7xoaWthcnZhbGluZSJ9LHsiYnJlZWQiOiLQmtC+0YjQutCwINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdCw0Y8iLCJzZXJ2aWNlcyI6eyLQktGL0YfQtdGBIjo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODB9LCJicmVlZF9lbiI6IkNhdCBsb25nLWhhaXJlZCIsImJyZWVkX2V0IjoiS2FzcyBwaWtrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0JrQvtGI0LrQsCDQnNC10LnQvS3QutGD0L0iLCJzZXJ2aWNlcyI6eyLQktGL0YfRkdGBIjo2MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTB9LCJicmVlZF9lbiI6IkNhdCBNYWluZSBDb29uIiwiYnJlZWRfZXQiOiJLYXNzIE1haW5lIENvb24ifV07CnZhciBSQUlMV0FZID0gImh0dHBzOi8vcmpncm9vbWluZy51cC5yYWlsd2F5LmFwcC9ib29rIjsKdmFyIEdPT0dMRV9TQ1JJUFQgPSAiaHR0cHM6Ly9zY3JpcHQuZ29vZ2xlLmNvbS9tYWNyb3Mvcy9BS2Z5Y2J5VFNaLWVKTWRlcC1EMExyLW54MF9WNEhCV2dJSWN0blJUMnJqU0R2QnliajVDWUkzTksyTXFjQXdfY2ZjemdSRWlmZy9leGVjIjsKdmFyIEZBTExCQUNLX1RJTUVTID0gWycxMDowMCcsJzEwOjMwJywnMTE6MDAnLCcxMTozMCcsJzEyOjAwJywnMTI6MzAnLCcxMzowMCcsJzEzOjMwJywnMTQ6MDAnLCcxNDozMCcsJzE1OjAwJywnMTU6MzAnLCcxNjowMCcsJzE2OjMwJywnMTc6MDAnLCcxNzozMCcsJzE4OjAwJ107CnZhciBib29raW5nID0ge2JyZWVkOicnLGJyZWVkRGlzcGxheTonJyxzZXJ2aWNlOicnLHByaWNlOjAsbWFzdGVyOicnLGdyb29tSGlzdG9yeTonJyxkYXRlOicnLHRpbWU6JycsbGFuZzoncnUnfTsKdmFyIHNlbEJyZWVkID0gbnVsbDsKdmFyIGNZID0gbmV3IERhdGUoKS5nZXRGdWxsWWVhcigpOwp2YXIgY00gPSBuZXcgRGF0ZSgpLmdldE1vbnRoKCk7CnZhciBzdGVwID0gMTsKdmFyIE1PTlRIUyA9IFsn0K/QvdCy0LDRgNGMJywn0KTQtdCy0YDQsNC70YwnLCfQnNCw0YDRgicsJ9CQ0L/RgNC10LvRjCcsJ9Cc0LDQuScsJ9CY0Y7QvdGMJywn0JjRjtC70YwnLCfQkNCy0LPRg9GB0YInLCfQodC10L3RgtGP0LHRgNGMJywn0J7QutGC0Y/QsdGA0YwnLCfQndC+0Y/QsdGA0YwnLCfQlNC10LrQsNCx0YDRjCddOwoKZnVuY3Rpb24gc2hvd1NjcmVlbihpZCkgewogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5zY3JlZW4nKS5mb3JFYWNoKGZ1bmN0aW9uKHMpe3MuY2xhc3NMaXN0LnJlbW92ZSgnYWN0aXZlJyk7fSk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoaWQpLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpOwogIHdpbmRvdy5zY3JvbGxUbygwLDApOwp9CgpmdW5jdGlvbiBnb1N0ZXAobikgewogIFsnYmsxJywnYmsyJywnYmszJywnYms0JywnYms1J10uZm9yRWFjaChmdW5jdGlvbihpZCxpKXsKICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKGlkKS5jbGFzc05hbWUgPSAnc3RlcCcgKyAoaSsxPT09bj8nIHNob3cnOicnKTsKICB9KTsKICBmb3IodmFyIGk9MTtpPD01O2krKyl7CiAgICB2YXIgcHM9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3BzJytpKTsKICAgIHZhciBwbD1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgncGwnK2kpOwogICAgaWYoaTxuKXtwcy5jbGFzc05hbWU9J3BzIGRvbmUnO2lmKHBsKXBsLmNsYXNzTmFtZT0ncGwgZG9uZSc7fQogICAgZWxzZSBpZihpPT09bil7cHMuY2xhc3NOYW1lPSdwcyBhY3RpdmUnO2lmKHBsKXBsLmNsYXNzTmFtZT0ncGwnO30KICAgIGVsc2V7cHMuY2xhc3NOYW1lPSdwcyc7aWYocGwpcGwuY2xhc3NOYW1lPSdwbCc7fQogIH0KICBzdGVwPW47IHdpbmRvdy5zY3JvbGxUbygwLDApOwogIGlmKG49PT0yKSBmaWx0ZXJNYXN0ZXJzKCk7Cn0KCmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdib29rQnRuJykub25jbGljayA9IGZ1bmN0aW9uKCl7CiAgc2hvd1NjcmVlbignYm9va1NjcmVlbicpOyBnb1N0ZXAoMSk7IGJ1aWxkQ2FsKCk7Cn07CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdiYWNrQnRuJykub25jbGljayA9IGZ1bmN0aW9uKCl7CiAgaWYoc3RlcD4xKXtnb1N0ZXAoc3RlcC0xKTt9ZWxzZXtzaG93U2NyZWVuKCdob21lU2NyZWVuJyk7fQp9Owpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnaG9tZUJ0bicpLm9uY2xpY2sgPSBmdW5jdGlvbigpewogIHNob3dTY3JlZW4oJ2hvbWVTY3JlZW4nKTsgcmVzZXRBbGwoKTsKfTsKCi8vIEJyZWVkIHNlYXJjaAp2YXIgaW5wID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2JJbnB1dCcpOwp2YXIgZHJvcCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdiRHJvcCcpOwp2YXIgY2xyID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NsckJ0bicpOwp2YXIgYmFkZ2UgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc0JhZGdlJyk7CgppbnAuYWRkRXZlbnRMaXN0ZW5lcignaW5wdXQnLCBmdW5jdGlvbigpewogIHZhciBxID0gaW5wLnZhbHVlLnRyaW0oKTsKICBjbHIuY2xhc3NMaXN0LnRvZ2dsZSgnc2hvdycsIHEubGVuZ3RoPjApOwogIGlmKCFxKXtkcm9wLmNsYXNzTGlzdC5yZW1vdmUoJ29wZW4nKTtkcm9wLmlubmVySFRNTD0nJztyZXR1cm47fQogIHZhciBzZj1MQU5HPT09J2VuJz8nYnJlZWRfZW4nOkxBTkc9PT0nZXQnPydicmVlZF9ldCc6J2JyZWVkJzsKICB2YXIgcmVzPURBVEEuZmlsdGVyKGZ1bmN0aW9uKGIpe3JldHVybihiW3NmXXx8Yi5icmVlZCkudG9Mb3dlckNhc2UoKS5pbmRleE9mKHEudG9Mb3dlckNhc2UoKSkhPT0tMTt9KS5zbGljZSgwLDM1KTsKICBkcm9wLmlubmVySFRNTD0nJzsKICB2YXIgX25yPUxBTkc9PT0nZW4nPydCcmVlZCBub3QgZm91bmQnOkxBTkc9PT0nZXQnPydUw7V1Z3UgZWkgbGVpdHVkJzon0J/QvtGA0L7QtNCwINC90LUg0L3QsNC50LTQtdC90LAnOwogIHZhciBfbnQ9TEFORz09PSdlbic/IkNhbid0IGZpbmQgeW91ciBicmVlZD8iOkxBTkc9PT0nZXQnPydFaSBsZWlhIG9tYSB0w7V1Z3U/Jzon0J3QtSDQvdCw0YjQu9C4INGB0LLQvtGOINC/0L7RgNC+0LTRgz8nOwogIHZhciBfbnM9TEFORz09PSdlbic/J0NvbnRhY3QgdXMg4oCUIHdlIHdpbGwgaGVscCB5b3UgY2hvb3NlIGEgc2VydmljZSc6TEFORz09PSdldCc/J1bDtXRrZSBtZWllZ2Egw7xoZW5kdXN0IOKAlCBhaXRhbWUgdGVlbnVzZSB2YWxpZGEnOifQodCy0Y/QttC40YLQtdGB0Ywg0YEg0L3QsNC80Lgg0LvRjtCx0YvQvCDRg9C00L7QsdC90YvQvCDRgdC/0L7RgdC+0LHQvtC8IOKAlCDQvNGLINC/0L7QvNC+0LbQtdC8INC/0L7QtNC+0LHRgNCw0YLRjCDRg9GB0LvRg9Cz0YMnOwogIGlmKCFyZXMubGVuZ3RoKXtkcm9wLmlubmVySFRNTD0nPGRpdiBjbGFzcz0ibm9yZXMiPicrX25yKyc8L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXIiIG9uY2xpY2s9InNob3dTY3JlZW4oXCdob21lU2NyZWVuXCcpIj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItaWNvbiI+8J+QvjwvZGl2PjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci10ZXh0Ij48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItdGl0bGUiPicrX250Kyc8L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItc3ViIj4nK19ucysnPC9kaXY+PC9kaXY+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLWFycm93Ij7ihpI8L2Rpdj48L2Rpdj4nO30KICBlbHNlewogICAgcmVzLmZvckVhY2goZnVuY3Rpb24oYil7CiAgICAgIHZhciBkPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpOyBkLmNsYXNzTmFtZT0nZGl0ZW0nOwogICAgICB2YXIgYm5hbWU9YltzZl18fGIuYnJlZWQ7CiAgICAgIHZhciBpZHg9Ym5hbWUudG9Mb3dlckNhc2UoKS5pbmRleE9mKHEudG9Mb3dlckNhc2UoKSk7CiAgICAgIGQuaW5uZXJIVE1MPWJuYW1lLnN1YnN0cmluZygwLGlkeCkrJzxtYXJrPicrYm5hbWUuc3Vic3RyaW5nKGlkeCxpZHgrcS5sZW5ndGgpKyc8L21hcms+JytibmFtZS5zdWJzdHJpbmcoaWR4K3EubGVuZ3RoKTsKICAgICAgZC5vbmNsaWNrPWZ1bmN0aW9uKCl7c2VsZWN0QnJlZWQoYik7fTsKICAgICAgZHJvcC5hcHBlbmRDaGlsZChkKTsKICAgIH0pOwogIH0KICBkcm9wLmNsYXNzTGlzdC5hZGQoJ29wZW4nKTsKfSk7Cgpkb2N1bWVudC5hZGRFdmVudExpc3RlbmVyKCdjbGljaycsZnVuY3Rpb24oZSl7CiAgaWYoIWUudGFyZ2V0LmNsb3Nlc3QoJy5id3JhcCcpKWRyb3AuY2xhc3NMaXN0LnJlbW92ZSgnb3BlbicpOwp9KTsKY2xyLm9uY2xpY2sgPSByZXNldEJyZWVkOwoKZnVuY3Rpb24gc2VsZWN0QnJlZWQoYil7CiAgc2VsQnJlZWQ9YjsgYm9va2luZy5icmVlZD1iLmJyZWVkOwogIGlucC52YWx1ZT0nJzsgY2xyLmNsYXNzTGlzdC5yZW1vdmUoJ3Nob3cnKTsKICBkcm9wLmNsYXNzTGlzdC5yZW1vdmUoJ29wZW4nKTsgZHJvcC5pbm5lckhUTUw9Jyc7CiAgYmFkZ2UuaW5uZXJIVE1MPScnOwogIHZhciBiRmllbGQ9TEFORz09PSdlbic/J2JyZWVkX2VuJzpMQU5HPT09J2V0Jz8nYnJlZWRfZXQnOidicmVlZCc7CiAgdmFyIGRpc3BCcmVlZD1iW2JGaWVsZF18fGIuYnJlZWQ7CiAgYm9va2luZy5icmVlZERpc3BsYXk9ZGlzcEJyZWVkOwogIHZhciBibj1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdzcGFuJyk7Ym4uY2xhc3NOYW1lPSdibmFtZSc7Ym4udGV4dENvbnRlbnQ9ZGlzcEJyZWVkOwogIHZhciBjaGdUeHQ9TEFORz09PSdlbic/J0NoYW5nZSc6TEFORz09PSdldCc/J011dWRhJzon0JjQt9C80LXQvdC40YLRjCc7CiAgdmFyIGJjPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NwYW4nKTtiYy5jbGFzc05hbWU9J2JjaGcnO2JjLnRleHRDb250ZW50PWNoZ1R4dDsKICBiYy5vbmNsaWNrPXJlc2V0QnJlZWQ7CiAgYmFkZ2UuYXBwZW5kQ2hpbGQoYm4pO2JhZGdlLmFwcGVuZENoaWxkKGJjKTsKICBiYWRnZS5jbGFzc0xpc3QuYWRkKCdzaG93Jyk7CiAgcmVuZGVyU3ZjcyhiKTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjU2VjJykuc3R5bGUuZGlzcGxheT0nYmxvY2snOwogICAgLy8gQWRkIGltcG9ydGFudCBub3RlIGlmIG5vdCBleGlzdHMKICAgIGlmKCFkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjTm90ZScpKXsKICAgICAgdmFyIG5vdGU9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7CiAgICAgIG5vdGUuaWQ9J3N2Y05vdGUnOwogICAgICBub3RlLnN0eWxlLmNzc1RleHQ9J2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDgpO3BhZGRpbmc6MTRweCAxNnB4O2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDIpO21hcmdpbi10b3A6MTJweDsnOwogICAgICB2YXIgbm90ZVRpdGxlPUxBTkc9PT0nZW4nPydQbGVhc2Ugbm90ZSc6TEFORz09PSdldCc/J1BhbmdlIHTDpGhlbGUnOifQktCw0LbQvdC+INC30L3QsNGC0YwnOwogICAgICB2YXIgbm90ZUJvZHk9TEFORz09PSdlbic/J0ZpbmFsIHByaWNlIGRlcGVuZHMgb24gY29hdCBjb25kaXRpb24gYW5kIHBldCBiZWhhdmlvdXIuPGJyPkRlbWF0dGluZyBmcm9tIDUg4oKsLjxicj5BZ2dyZXNzaXZlIGJlaGF2aW91ciBzdXJjaGFyZ2UgbWF5IGFwcGx5OiArNTAlLic6TEFORz09PSdldCc/J0zDtXBsaWsgaGluZCBzw7VsdHViIGthcnZhc3Rpa3Ugc2Vpc3VuZGlzdCBqYSBsZW1taWtsb29tYSBrw6RpdHVtaXNlc3QuPGJyPktvbHRzdW5pdGUgbGFodGloYXJ1dGFtaW5lIGFsYXRlcyA1IOKCrC48YnI+QWdyZXNzaWl2c2Uga8OkaXR1bWlzZSBrb3JyYWwgdsO1aWIgbGlzYW5kdWRhIDUwJSBqdXVyZGVoaW5kbHVzLic6J9Ce0LrQvtC90YfQsNGC0LXQu9GM0L3QsNGPINGB0YLQvtC40LzQvtGB0YLRjCDQt9Cw0LLQuNGB0LjRgiDQvtGCINGB0L7RgdGC0L7Rj9C90LjRjyDRiNC10YDRgdGC0Lgg0Lgg0L/QvtCy0LXQtNC10L3QuNGPINC/0LjRgtC+0LzRhtCwLjxicj7QoNCw0LfQsdC+0YAg0LrQvtC70YLRg9C90L7QsiDigJQg0L7RgiA1IOKCrC48YnI+0J/RgNC4INCw0LPRgNC10YHRgdC40LLQvdC+0Lwg0L/QvtCy0LXQtNC10L3QuNC4INC80L7QttC10YIg0L/RgNC40LzQtdC90Y/RgtGM0YHRjyDQtNC+0L/Qu9Cw0YLQsCA1MCUuJzsKICAgICAgbm90ZS5pbm5lckhUTUw9JzxkaXYgc3R5bGU9ImZvbnQtc2l6ZTowLjgzOHJlbTtsZXR0ZXItc3BhY2luZzouMTVlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjhweDtmb250LXdlaWdodDo2MDA7Zm9udC1mYW1pbHk6XCdNb250c2VycmF0XCcsc2Fucy1zZXJpZiI+Jytub3RlVGl0bGUrJzwvZGl2PjxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxLjAyNXJlbTtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjEuODtmb250LWZhbWlseTpcJ01vbnRzZXJyYXRcJyxzYW5zLXNlcmlmIj4nK25vdGVCb2R5Kyc8L2Rpdj4nOwogICAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjU2VjJykuYXBwZW5kQ2hpbGQobm90ZSk7CiAgICB9CiAgZmlsdGVyTWFzdGVycygpOwp9CgpmdW5jdGlvbiByZXNldEJyZWVkKCl7CiAgc2VsQnJlZWQ9bnVsbDtib29raW5nLmJyZWVkPScnO2Jvb2tpbmcuc2VydmljZT0nJztib29raW5nLnByaWNlPTA7CiAgaW5wLnZhbHVlPScnO2Nsci5jbGFzc0xpc3QucmVtb3ZlKCdzaG93Jyk7CiAgYmFkZ2UuY2xhc3NMaXN0LnJlbW92ZSgnc2hvdycpO2JhZGdlLmlubmVySFRNTD0nJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjU2VjJykuc3R5bGUuZGlzcGxheT0nbm9uZSc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y0xpc3QnKS5pbm5lckhUTUw9Jyc7Cn0KCgp2YXIgU1ZDX1RSQU5TTEFUSU9OUyA9IHsKICAn0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQnOiAgICAgIHtlbjonQmFzaWMgZ3Jvb20nLCAgICAgIGV0OidQw7VoaWhvb2xkdXMnfSwKICAn0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQnOntlbjonSHlnaWVuZSBncm9vbScsICAgIGV0OidIw7xnaWVlbmlob29sZHVzJ30sCiAgJ9Ca0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQnOiAge2VuOidGdWxsIGdyb29tJywgICAgICAgIGV0OidUw6RpZWxpayBob29sZHVzJ30sCiAgJ9Ci0YDQuNC80LzQuNC90LMnOiAgICAgICAgICB7ZW46J1RyaW1taW5nJywgICAgICAgICAgZXQ6J1RyaW1tZXJpbWluZSd9LAogICfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6ICAge2VuOidFeHByZXNzIHNoZWQnLCAgICAgIGV0OidLaWlya2FydmF2YWhldHVzJ30sCiAgJ9CS0YvRh9C10YEnOiAgICAgICAgICAgICB7ZW46J0JydXNoLW91dCcsICAgICAgICAgZXQ6J0hhcmphbWluZSd9LAogICfQktGB0Y8g0L/RgNC+0LPRgNCw0LzQvNCwJzogICAgIHtlbjonRnVsbCBwcm9ncmFtJywgICAgICBldDonS29ndSBwcm9ncmFtbSd9Cn07CnZhciBTVkNfVEFHTElORV9JMThOPXsKICBydTp7J9CS0YvRh9C10YEnOifQodGC0L7QuNC80L7RgdGC0Ywg0LfQsNCy0LjRgdC40YIg0L7RgiDRgdC+0YHRgtC+0Y/QvdC40Y8g0YjQtdGA0YHRgtC4INC4INC+0LHRitGR0LzQsCDRgNCw0LHQvtGCJywn0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQnOifQn9C+0LTRhdC+0LTQuNGCINC00LvRjyDQv9C+0LTQtNC10YDQttCw0L3QuNGPINGH0LjRgdGC0L7RgtGLINC80LXQttC00YMg0L/RgNC+0YbQtdC00YPRgNCw0LzQuCcsJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Jzon0JTQu9GPINC60L7QvNGE0L7RgNGC0LAg0Lgg0LDQutC60YPRgNCw0YLQvdC+0YHRgtC4INC/0LjRgtC+0LzRhtCwJywn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc6J9Cf0L7Qu9C90YvQuSDRg9GF0L7QtCDRgdC+INGB0YLRgNC40LbQutC+0LknLCfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6J9Cf0L7QvNC+0LPQsNC10YIg0YPQvNC10L3RjNGI0LjRgtGMINC60L7Qu9C40YfQtdGB0YLQstC+INC70LjQvdGP0Y7RidC10Lkg0YjQtdGA0YHRgtC4Jywn0KLRgNC40LzQvNC40L3Qsyc6J9CU0LvRjyDQttC10YHRgtC60L7RiNC10YDRgdGC0L3Ri9GFINC/0L7RgNC+0LQnfSwKICBlbjp7J9CS0YvRh9C10YEnOidQcmljZSBkZXBlbmRzIG9uIGNvYXQgY29uZGl0aW9uIGFuZCB2b2x1bWUgb2Ygd29yaycsJ9CR0LDQt9C+0LLRi9C5INGD0YXQvtC0JzonSWRlYWwgZm9yIG1haW50YWluaW5nIGNsZWFubGluZXNzIGJldHdlZW4gZnVsbCBncm9vbXMnLCfQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCc6J0ZvciB5b3VyIHBldFwncyBjb21mb3J0IGFuZCBuZWF0bmVzcycsJ9Ca0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQnOidGdWxsIGdyb29taW5nIHdpdGggaGFpcmN1dCcsJ9Ct0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwJzonU2lnbmlmaWNhbnRseSByZWR1Y2VzIHNoZWRkaW5nJywn0KLRgNC40LzQvNC40L3Qsyc6J0ZvciB3aXJlLWhhaXJlZCBicmVlZHMnfSwKICBldDp7J9CS0YvRh9C10YEnOidIaW5kIHPDtWx0dWIga2FydmFzdGlrdSBzZWlzdW5kaXN0IGphIHTDtsO2bWFodXN0Jywn0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQnOidTb2JpYiBwdWh0dXNlIGhvaWRtaXNla3MgcHJvdHNlZHV1cmlkZSB2YWhlbCcsJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0JzonTGVtbWlrbG9vbWEgbXVnYXZ1c2VrcyBqYSBrb3JyYXNob2l1a3MnLCfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0JzonVMOkaWVsaWsgaG9vbGR1cyBrb29zIGzDtWlrdXNlZ2EnLCfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6J1bDpGhlbmRhYiBvbHVsaXNlbHQga2FydmFkZSBsYW5nZW1pc3QnLCfQotGA0LjQvNC80LjQvdCzJzonVHJhYXRrYXJ2YWxpc3RlbGUgdMO1dWd1ZGVsZSd9Cn07CnZhciBTVkNfREVTQ19JMThOPXsKICBydTp7J9CS0YvRh9C10YEnOifQp9C40YHRgtC60LAg0LPQu9Cw0LcsINGD0YjQtdC5LCDQv9C+0LTRgdGC0YDQuNCz0LDQvdC40LUg0LrQvtCz0YLQtdC5LCDQstGL0YfRkdGBICjQtNC70Y8g0LrQvtGI0LXQuiknLCfQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCc6J9Cc0YvRgtGM0ZEg0L/RgNC+0YTQtdGB0YHQuNC+0L3QsNC70YzQvdGL0LzQuCDRgdGA0LXQtNGB0YLQstCw0LzQuCwg0LTQtdC70LjQutCw0YLQvdCw0Y8g0YHRg9GI0LrQsCcsJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Jzon0KHRgtGA0LjQttC60LAg0LrQvtCz0YLQtdC5LCDRh9C40YHRgtC60LAg0YPRiNC10Lkg0Lgg0LPQu9Cw0LcsINC60YPQv9Cw0L3QuNC1LCDRgdGD0YjQutCwLCDRg9GF0L7QtCDQt9CwINC70LDQv9C60LDQvNC4INC4INGH0YPQstGB0YLQstC40YLQtdC70YzQvdGL0LzQuCDQt9C+0L3QsNC80LgnLCfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Jzon0KHRgtGA0LjQttC60LAg0LrQvtCz0YLQtdC5LCDRh9C40YHRgtC60LAg0YPRiNC10Lkg0Lgg0LPQu9Cw0LcsINC60YPQv9Cw0L3QuNC1LCDRgdGD0YjQutCwLCDRg9GF0L7QtCDQt9CwINC70LDQv9C60LDQvNC4INC4INGH0YPQstGB0YLQstC40YLQtdC70YzQvdGL0LzQuCDQt9C+0L3QsNC80LgsINC80L7QtNC10LvRjNC90LDRjyDRgdGC0YDQuNC20LrQsCcsJ9Ct0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwJzon0JzRi9GC0YzRkSwg0YHRg9GI0LrQsCwg0YPRhdC+0LQg0LfQsCDRiNC10YDRgdGC0YzRjiwg0LzQsNGB0LrQsCwg0L/QvtC00YHRgtGA0LjQs9Cw0L3QuNC1INC60L7Qs9GC0LXQuSwg0YfQuNGB0YLQutCwINGD0YjQtdC5INC4INCz0LvQsNC3LCDRg9GF0L7QtCDQt9CwINC70LDQv9Cw0LzQuCDQuCDQt9C+0L3QsNC80Lgg0YLRgNC10LHRg9GO0YnQuNC80Lgg0L7RgdC+0LHQvtCz0L4g0LLQvdC40LzQsNC90LjRjycsJ9Ci0YDQuNC80LzQuNC90LMnOifQktGL0YnQuNC/0YvQstCw0L3QuNC1INGB0YLQsNGA0L7Qs9C+INGB0LvQvtGPINGI0LXRgNGB0YLQuCwg0LzRi9GC0YzRkSwg0YHRg9GI0LrQsCwg0YHRgtGA0LjQttC60LAg0LrQvtCz0YLQtdC5LCDRh9C40YHRgtC60LAg0YPRiNC10Lkg0Lgg0LPQu9Cw0LcsINC+0YTQvtGA0LzQu9C10L3QuNC1INGI0LXRgNGB0YLQuCcsJ9CS0YHRjyDQv9GA0L7Qs9GA0LDQvNC80LAnOifQn9CV0KDQktCr0Jkg0JLQmNCX0JjQoiAoMjAtMzAg0LzQuNC9KSDigJQgMjAg4oKsXG7igKIg0LfQvdCw0LrQvtC80YHRgtCy0L4g0YHQviDRgdGC0L7Qu9C+0Lwg0Lgg0LjQvdGB0YLRgNGD0LzQtdC90YLQsNC80LhcbuKAoiDQu9GR0LPQutC+0LUg0LLRi9GH0ZHRgdGL0LLQsNC90LjQtVxu4oCiINC30LLRg9C60Lgg0YTQtdC90LAg0Lgg0LvQtdCz0LrQsNGPINC/0YDQvtC00YPQstC60LBcbuKAoiDQvtGB0LLQtdC20LXQvdC40LUg0LPQu9Cw0LfQvtC6INC4INGD0YjQtdC6XG7igKIg0LrQvtCz0L7RgtC60LhcbuKAoiDQstC60YPRgdC90Y/RiNC60Lgg0Lgg0YHQv9C+0LrQvtC50L3QsNGPINCw0LTQsNC/0YLQsNGG0LjRj1xuXG7QktCi0J7QoNCe0Jkg0JLQmNCX0JjQoiAoNDAtNjAg0LzQuNC9KSDigJQgMzUg4oKsXG7igKIg0L/QtdGA0LLQvtC1INC60YPQv9Cw0L3QuNC1INC4INGB0YPRiNC60LBcbuKAoiDQstGL0YfRkdGB0YvQstCw0L3QuNC1XG7igKIg0LPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LRcbuKAoiDQvdC10LHQvtC70YzRiNCw0Y8g0YHRgtGA0LjQttC60LAgLyDQutC+0YDRgNC10LrRhtC40Y8g0YjQtdGA0YHRgtC4ICjQv9GA0Lgg0L3QtdC+0LHRhdC+0LTQuNC80L7RgdGC0LgpXG7igKIg0LfQsNC60YDQtdC/0LvQtdC90LjQtSDQv9C+0LvQvtC20LjRgtC10LvRjNC90L7Qs9C+INC+0L/Ri9GC0LAnfSwKICBlbjp7J9CS0YvRh9C10YEnOidFeWUgYW5kIGVhciBjbGVhbmluZywgbmFpbCB0cmltbWluZywgYnJ1c2hpbmcgKGZvciBjYXRzKScsJ9CR0LDQt9C+0LLRi9C5INGD0YXQvtC0JzonV2FzaGluZyB3aXRoIHByb2Zlc3Npb25hbCBwcm9kdWN0cywgZ2VudGxlIGRyeWluZycsJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0JzonTmFpbCB0cmltbWluZywgZWFyIGFuZCBleWUgY2xlYW5pbmcsIGJhdGhpbmcsIGRyeWluZywgcGF3IGFuZCBzZW5zaXRpdmUgYXJlYSBjYXJlJywn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc6J05haWwgdHJpbW1pbmcsIGVhciBhbmQgZXllIGNsZWFuaW5nLCBiYXRoaW5nLCBkcnlpbmcsIHBhdyBhbmQgc2Vuc2l0aXZlIGFyZWEgY2FyZSwgc3R5bGluZyBoYWlyY3V0Jywn0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAnOidXYXNoaW5nLCBkcnlpbmcsIGNvYXQgY2FyZSwgbWFzaywgbmFpbCB0cmltbWluZywgZWFyIGFuZCBleWUgY2xlYW5pbmcsIHBhdyBhbmQgc3BlY2lhbCBhcmVhIGNhcmUnLCfQotGA0LjQvNC80LjQvdCzJzonUmVtb3Zpbmcgb2xkIGNvYXQgbGF5ZXIsIHdhc2hpbmcsIGRyeWluZywgbmFpbCB0cmltbWluZywgZWFyIGFuZCBleWUgY2xlYW5pbmcsIGNvYXQgc3R5bGluZycsJ9CS0YHRjyDQv9GA0L7Qs9GA0LDQvNC80LAnOidGSVJTVCBWSVNJVCAoMjAtMzAgbWluKSDigJQg4oKsMjBcbuKAoiBnZXR0aW5nIHVzZWQgdG8gdGhlIHRhYmxlIGFuZCB0b29sc1xu4oCiIGdlbnRsZSBicnVzaGluZ1xu4oCiIGRyeWVyIHNvdW5kcyBhbmQgbGlnaHQgYWlyZmxvd1xu4oCiIGV5ZSBhbmQgZWFyIHJlZnJlc2hcbuKAoiBuYWlsIHRyaW1cbuKAoiB0cmVhdHMgYW5kIGNhbG0gYWRhcHRhdGlvblxuXG5TRUNPTkQgVklTSVQgKDQwLTYwIG1pbikg4oCUIOKCrDM1XG7igKIgZmlyc3QgYmF0aCBhbmQgZHJ5aW5nXG7igKIgYnJ1c2hpbmdcbuKAoiBoeWdpZW5lIGNhcmVcbuKAoiBsaWdodCB0cmltIC8gY29hdCBhZGp1c3RtZW50IChpZiBuZWVkZWQpXG7igKIgcmVpbmZvcmNpbmcgdGhlIHBvc2l0aXZlIGV4cGVyaWVuY2UnfSwKICBldDp7J9CS0YvRh9C10YEnOidTaWxtYWRlIGphIGvDtXJ2YWRlIHB1aGFzdGFtaW5lLCBrw7zDvG50ZSBsw7Vpa2FtaW5lLCBoYXJqYW1pbmUgKGthc3NpZGVsZSknLCfQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCc6J1Blc2VtaW5lIHByb2Zlc3Npb25hYWxzZXRlIHZhaGVuZGl0ZWdhLCDDtXJuIGt1aXZhdGFtaW5lJywn0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQnOidLw7zDvG50ZSBsw7Vpa2FtaW5lLCBrw7VydmFkZSBqYSBzaWxtYWRlIHB1aGFzdGFtaW5lLCBwZXNlbWluZSwga3VpdmF0YW1pbmUsIGvDpHBwYWRlIGphIHR1bmRsaWtlIHBpaXJrb25kYWRlIGhvb2xkdXMnLCfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0JzonS8O8w7xudGUgbMO1aWthbWluZSwga8O1cnZhZGUgamEgc2lsbWFkZSBwdWhhc3RhbWluZSwgcGVzZW1pbmUsIGt1aXZhdGFtaW5lLCBrw6RwcGFkZSBqYSB0dW5kbGlrZSBwaWlya29uZGFkZSBob29sZHVzLCBtb2RlbGzDtWlrdXMnLCfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6J1Blc2VtaW5lLCBrdWl2YXRhbWluZSwga2FydmFzdGlrdSBob29sZHVzLCBtYXNrLCBrw7zDvG50ZSBsw7Vpa2FtaW5lLCBrw7VydmFkZSBqYSBzaWxtYWRlIHB1aGFzdGFtaW5lLCBrw6RwcGFkZSBqYSBlcmlsaXN0ZSBwaWlya29uZGFkZSBob29sZHVzJywn0KLRgNC40LzQvNC40L3Qsyc6J1ZhbmEga2FydmFraWhpIGVlbWFsZGFtaW5lLCBwZXNlbWluZSwga3VpdmF0YW1pbmUsIGvDvMO8bnRlIGzDtWlrYW1pbmUsIGvDtXJ2YWRlIGphIHNpbG1hZGUgcHVoYXN0YW1pbmUsIGthcnZhc3Rpa3Uga3VqdW5kYW1pbmUnLCfQktGB0Y8g0L/RgNC+0LPRgNCw0LzQvNCwJzonRVNJTUVORSBLw5xMQVNUVVMgKDIwLTMwIG1pbikg4oCUIDIwIOKCrFxu4oCiIHR1dHZ1bWluZSBsYXVhZ2EgamEgdMO2w7ZyaWlzdGFkZWdhXG7igKIga2VyZ2UgaGFyamFtaW5lXG7igKIgZsO2w7ZuaWhlbGlkIGphIGtlcmdlIMO1aHV2b29sXG7igKIgc2lsbWFkZSBqYSBrw7VydmFkZSB2w6Ryc2tlbmR1c1xu4oCiIGvDvMO8bnRlIGzDtWlrYW1pbmVcbuKAoiBtYWl1c2VkIGphIHJhaHVsaWsga29oYW5lbWluZVxuXG5URUlORSBLw5xMQVNUVVMgKDQwLTYwIG1pbikg4oCUIDM1IOKCrFxu4oCiIGVzaW1lbmUgdmFubml0YW1pbmUgamEga3VpdmF0YW1pbmVcbuKAoiBoYXJqYW1pbmVcbuKAoiBow7xnaWVlbmlob29sZHVzXG7igKIga2VyZ2UgbMO1aWt1cyAvIGthcnZhIGtvcnJpZ2VlcmltaW5lICh2YWphZHVzZWwpXG7igKIgcG9zaXRpaXZzZSBrb2dlbXVzZSBraW5uaXN0YW1pbmUnfQp9Owp2YXIgU1ZDX0RFU0NfQ0FUX0NPTVBMRVg9ewogIHJ1OifQnNGL0YLRjNGRLCDRgdGD0YjQutCwLCDQstGL0YfRkdGB0YvQstCw0L3QuNC1LCDRgdGC0YDQuNC20LrQsCDQutC+0LPRgtC10LksINCwINGC0LDQutC20LUg0L7QsdGA0LDQsdC+0YLQutCwINCz0LvQsNC3INC4INGD0YjQtdC6JywKICBlbjonV2FzaGluZywgZHJ5aW5nLCBicnVzaGluZywgbmFpbCB0cmltbWluZywgYW5kIGV5ZSBhbmQgZWFyIGNhcmUnLAogIGV0OidQZXNlbWluZSwga3VpdmF0YW1pbmUsIGhhcmphbWluZSwga8O8w7xudGUgbMO1aWthbWluZSBuaW5nIHNpbG1hZGUgamEga8O1cnZhZGUgaG9vbGR1cycKfTsKZnVuY3Rpb24gZ2V0U3ZjVGFnKG5hbWUpe3JldHVybihTVkNfVEFHTElORV9JMThOW0xBTkddJiZTVkNfVEFHTElORV9JMThOW0xBTkddW25hbWVdKXx8U1ZDX1RBR0xJTkVfSTE4Ti5ydVtuYW1lXXx8Jyc7fQpmdW5jdGlvbiBnZXRTdmNEZXNjKG5hbWUpewogIGlmKG5hbWU9PT0n0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCcgJiYgYm9va2luZy5icmVlZCAmJiBib29raW5nLmJyZWVkLmluZGV4T2YoJ9Ca0L7RiNC60LAnKT09PTApewogICAgdmFyIGQ9U1ZDX0RFU0NfQ0FUX0NPTVBMRVhbTEFOR118fFNWQ19ERVNDX0NBVF9DT01QTEVYLnJ1OwogICAgcmV0dXJuIGQ7CiAgfQogIHJldHVybihTVkNfREVTQ19JMThOW0xBTkddJiZTVkNfREVTQ19JMThOW0xBTkddW25hbWVdKXx8U1ZDX0RFU0NfSTE4Ti5ydVtuYW1lXXx8Jyc7Cn0KCmZ1bmN0aW9uIHJlbmRlclN2Y3MoYil7CiAgdmFyIGxibEVsPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdGVwMkxibEVsJyk7CiAgaWYobGJsRWwpewogICAgdmFyIGJhc2VMYmw9KFRbTEFOR10mJlRbTEFOR10uc3RlcDJfbGJsKXx8JzAyIMK3INCj0YHQu9GD0LPQsCc7CiAgICBsYmxFbC50ZXh0Q29udGVudD0oYi5icmVlZD09PSfQqdC10L3QutC4Jyk/KGJhc2VMYmwrJyBQdXBweSBTdGFyJyk6YmFzZUxibDsKICB9CiAgdmFyIGxpc3Q9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y0xpc3QnKTtsaXN0LmlubmVySFRNTD0nJzsKICBPYmplY3QuZW50cmllcyhiLnNlcnZpY2VzKS5mb3JFYWNoKGZ1bmN0aW9uKGt2KXsKICAgIHZhciBuYW1lPWt2WzBdLHByaWNlPWt2WzFdOwoKICAgIHZhciBkaXNwbGF5TmFtZT0oTEFORyE9PSdydScmJlNWQ19UUkFOU0xBVElPTlNbbmFtZV0pP1NWQ19UUkFOU0xBVElPTlNbbmFtZV1bTEFOR106bmFtZTsKICAgIHZhciBidG49ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnYnV0dG9uJyk7YnRuLmNsYXNzTmFtZT0nc3ZidG4nOwogICAgdmFyIHJvdz1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdkaXYnKTtyb3cuY2xhc3NOYW1lPSdzdmJ0bi1yb3cnOwogICAgdmFyIG5zPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NwYW4nKTtucy5jbGFzc05hbWU9J3N2YnRuLW5hbWUnO25zLnRleHRDb250ZW50PWRpc3BsYXlOYW1lOwogICAgdmFyIHBzPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NwYW4nKTtwcy5jbGFzc05hbWU9J3N2YnRuLXByaWNlJztwcy50ZXh0Q29udGVudD1wcmljZSsnIOKCrCc7CiAgICByb3cuYXBwZW5kQ2hpbGQobnMpO3Jvdy5hcHBlbmRDaGlsZChwcyk7CiAgICBidG4uYXBwZW5kQ2hpbGQocm93KTsKICAgIHZhciBkZXNjPWdldFN2Y0Rlc2MobmFtZSk7CiAgICBpZihkZXNjKXt2YXIgZHM9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnc3BhbicpO2RzLmNsYXNzTmFtZT0nc3ZidG4tZGVzYyc7ZHMudGV4dENvbnRlbnQ9ZGVzYztidG4uYXBwZW5kQ2hpbGQoZHMpO30KICAgIHZhciB0YWc9Z2V0U3ZjVGFnKG5hbWUpOwogICAgaWYodGFnKXt2YXIgdHM9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnc3BhbicpO3RzLmNsYXNzTmFtZT0nc3ZidG4tdGFnJzt0cy50ZXh0Q29udGVudD10YWc7YnRuLmFwcGVuZENoaWxkKHRzKTt9CiAgICBidG4ub25jbGljaz1mdW5jdGlvbigpewogICAgICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuc3ZidG4nKS5mb3JFYWNoKGZ1bmN0aW9uKGIpe2IuY2xhc3NMaXN0LnJlbW92ZSgnYWN0aXZlJyk7fSk7CiAgICAgIGJ0bi5jbGFzc0xpc3QuYWRkKCdhY3RpdmUnKTsKICAgICAgYm9va2luZy5zZXJ2aWNlPW5hbWU7Ym9va2luZy5wcmljZT1wcmljZTsKICAgICAgZmlsdGVyTWFzdGVycygpOwogICAgICBzZXRUaW1lb3V0KGZ1bmN0aW9uKCl7Z29TdGVwKDIpO30sMzAwKTsKICAgIH07CiAgICBsaXN0LmFwcGVuZENoaWxkKGJ0bik7CiAgfSk7Cn0KCi8vIE1hc3RlcnMKZnVuY3Rpb24gZmlsdGVyTWFzdGVycygpewogIHZhciBpc0NhdCA9IGJvb2tpbmcuYnJlZWQgJiYgYm9va2luZy5icmVlZC5pbmRleE9mKCfQmtC+0YjQutCwJykgPT09IDA7CiAgdmFyIGJyZWVkID0gYm9va2luZy5icmVlZCB8fCAnJzsKICB2YXIgaXNDYXRDb21wbGV4ID0gaXNDYXQgJiYgYm9va2luZy5zZXJ2aWNlID09PSAn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc7CiAgdmFyIGFubmFFeGNsdWRlID0gWyfQnNCw0LvRjNGC0LjQv9GDJywn0J/Rg9C00LXQu9GMJywn0JnQvtGA0LonLCfQkdC40YjQvtC9Jywn0JHQvtC70L7QvdC60LAnLCfQnNCw0LvRjNGC0LjQudGB0LrQsNGPJ107CiAgdmFyIGlzQW5uYUJyZWVkID0gYnJlZWQgJiYgIWFubmFFeGNsdWRlLnNvbWUoZnVuY3Rpb24oYil7IHJldHVybiBicmVlZC5pbmRleE9mKGIpICE9PSAtMTsgfSk7CiAgdmFyIGFsZXhhbmRyYUV4Y2x1ZGUgPSBbJ9Ck0L7QutGB0YLQtdGA0YzQtdGAJywn0KbQstC10YDQs9GI0L3QsNGD0YbQtdGAJ107CiAgdmFyIGlzQWxleGFuZHJhQnJlZWQgPSAhYWxleGFuZHJhRXhjbHVkZS5zb21lKGZ1bmN0aW9uKGIpeyByZXR1cm4gYnJlZWQuaW5kZXhPZihiKSAhPT0gLTE7IH0pOwogIHZhciBrc2VuaWFFeGNsdWRlID0gWyfQn9GD0LTQtdC70YwnLCfQnNCw0LvRjNGC0LjQv9GDJywn0JnQvtGA0LonXTsKICB2YXIgaXNLc2VuaWFCcmVlZCA9ICFrc2VuaWFFeGNsdWRlLnNvbWUoZnVuY3Rpb24oYil7IHJldHVybiBicmVlZC5pbmRleE9mKGIpICE9PSAtMTsgfSk7CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLm1idG4nKS5mb3JFYWNoKGZ1bmN0aW9uKGJ0bil7CiAgICB2YXIgbWFzdGVyID0gYnRuLmdldEF0dHJpYnV0ZSgnZGF0YS1tYXN0ZXInKTsKICAgIHZhciBpc1RyaW1taW5nID0gYm9va2luZy5zZXJ2aWNlID09PSAn0KLRgNC40LzQvNC40L3Qsyc7CiAgICBpZihpc0NhdENvbXBsZXgpewogICAgICBidG4uc3R5bGUuZGlzcGxheSA9IChtYXN0ZXIgPT09ICfQotCw0YLRjNGP0L3QsCcgfHwgbWFzdGVyID09PSAn0JrRgdC10L3QuNGPJykgPyAnJyA6ICdub25lJzsKICAgICAgcmV0dXJuOwogICAgfQogICAgaWYobWFzdGVyID09PSAn0JDQu9C40YHQsCcpewogICAgICBidG4uc3R5bGUuZGlzcGxheSA9IGlzQ2F0ID8gJycgOiAnbm9uZSc7CiAgICB9IGVsc2UgaWYobWFzdGVyID09PSAn0JDQvdC90LAnKXsKICAgICAgYnRuLnN0eWxlLmRpc3BsYXkgPSAoaXNBbm5hQnJlZWQgJiYgIWlzVHJpbW1pbmcpID8gJycgOiAnbm9uZSc7CiAgICB9IGVsc2UgaWYobWFzdGVyID09PSAn0JDQu9C10LrRgdCw0L3QtNGA0LAnKXsKICAgICAgYnRuLnN0eWxlLmRpc3BsYXkgPSAoaXNBbGV4YW5kcmFCcmVlZCAmJiAhaXNUcmltbWluZyAmJiAhaXNDYXQpID8gJycgOiAnbm9uZSc7CiAgICB9IGVsc2UgaWYobWFzdGVyID09PSAn0JrRgdC10L3QuNGPJyl7CiAgICAgIGJ0bi5zdHlsZS5kaXNwbGF5ID0gaXNLc2VuaWFCcmVlZCA/ICcnIDogJ25vbmUnOwogICAgfSBlbHNlIGlmKGlzVHJpbW1pbmcpewogICAgICBidG4uc3R5bGUuZGlzcGxheSA9ICdub25lJzsKICAgIH0gZWxzZSB7CiAgICAgIGJ0bi5zdHlsZS5kaXNwbGF5ID0gJyc7CiAgICB9CiAgfSk7Cn0KCmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5tYnRuJykuZm9yRWFjaChmdW5jdGlvbihidG4pewogIGJ0bi5vbmNsaWNrPWZ1bmN0aW9uKCl7CiAgICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcubWJ0bicpLmZvckVhY2goZnVuY3Rpb24oYil7Yi5jbGFzc0xpc3QucmVtb3ZlKCdhY3RpdmUnKTt9KTsKICAgIGJ0bi5jbGFzc0xpc3QuYWRkKCdhY3RpdmUnKTsKICAgIGJvb2tpbmcubWFzdGVyPWJ0bi5nZXRBdHRyaWJ1dGUoJ2RhdGEtbWFzdGVyJyk7CiAgICBzZXRUaW1lb3V0KGZ1bmN0aW9uKCl7Z29TdGVwKDMpO30sMzAwKTsKICB9Owp9KTsKCi8vIEdyb29tIGhpc3RvcnkKZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmdidG4nKS5mb3JFYWNoKGZ1bmN0aW9uKGJ0bil7CiAgYnRuLm9uY2xpY2s9ZnVuY3Rpb24oKXsKICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5nYnRuJykuZm9yRWFjaChmdW5jdGlvbihiKXtiLmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpO30pOwogICAgYnRuLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpOwogICAgYm9va2luZy5ncm9vbUhpc3Rvcnk9YnRuLmdldEF0dHJpYnV0ZSgnZGF0YS12YWwnKTsKICAgIHNldFRpbWVvdXQoZnVuY3Rpb24oKXtnb1N0ZXAoNCk7YnVpbGRDYWwoKTt9LDMwMCk7CiAgfTsKfSk7CgovLyBDYWxlbmRhcgpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgncHJldk0nKS5vbmNsaWNrPWZ1bmN0aW9uKCl7Y00tLTtpZihjTTwwKXtjTT0xMTtjWS0tO31idWlsZENhbCgpO307CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCduZXh0TScpLm9uY2xpY2s9ZnVuY3Rpb24oKXtjTSsrO2lmKGNNPjExKXtjTT0wO2NZKys7fWJ1aWxkQ2FsKCk7fTsKCnZhciBhdmFpbGFibGVEYXlzID0gW107CgpmdW5jdGlvbiBsb2FkQXZhaWxhYmxlRGF5cygpIHsKICB2YXIgbWFzdGVyID0gYm9va2luZy5tYXN0ZXI7CiAgaWYgKCFtYXN0ZXIpIHJldHVybjsKICBhdmFpbGFibGVEYXlzID0gW107CiAgZmV0Y2god2luZG93LmxvY2F0aW9uLm9yaWdpbiArICcvYXBpL2F2YWlsYWJsZV9kYXlzP21vbnRoPScgKyAoY00rMSkgKyAnJnllYXI9JyArIGNZICsgJyZtYXN0ZXI9JyArIGVuY29kZVVSSUNvbXBvbmVudChtYXN0ZXIpKQogICAgLnRoZW4oZnVuY3Rpb24ocil7IHJldHVybiByLmpzb24oKTsgfSkKICAgIC50aGVuKGZ1bmN0aW9uKGRhdGEpewogICAgICBhdmFpbGFibGVEYXlzID0gZGF0YS5hdmFpbGFibGUgfHwgW107CiAgICAgIG1hcmtBdmFpbGFibGVEYXlzKCk7CiAgICB9KQogICAgLmNhdGNoKGZ1bmN0aW9uKCl7IGF2YWlsYWJsZURheXMgPSBbXTsgfSk7Cn0KCmZ1bmN0aW9uIG1hcmtBdmFpbGFibGVEYXlzKCkgewogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5jZCcpLmZvckVhY2goZnVuY3Rpb24oYyl7aWYoIWMuY2xhc3NMaXN0LmNvbnRhaW5zKCdkaXMnKSljLmNsYXNzTGlzdC5yZW1vdmUoJ3NlbCcpO30pOwogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5jZDpub3QoLmRpcyk6bm90KC5jZG4pOm5vdCgucGFkKScpLmZvckVhY2goZnVuY3Rpb24oZWwpIHsKICAgIHZhciBkYXkgPSBlbC50ZXh0Q29udGVudC50cmltKCk7CiAgICBpZiAoIWRheSB8fCBpc05hTihwYXJzZUludChkYXkpKSkgcmV0dXJuOwogICAgdmFyIGRhdGVTdHIgPSBTdHJpbmcocGFyc2VJbnQoZGF5KSkucGFkU3RhcnQoMiwnMCcpICsgJy4nICsgU3RyaW5nKGNNKzEpLnBhZFN0YXJ0KDIsJzAnKSArICcuJyArIGNZOwogICAgaWYgKGF2YWlsYWJsZURheXMuaW5kZXhPZihkYXRlU3RyKSAhPT0gLTEpIHsKICAgICAgZWwuY2xhc3NMaXN0LmFkZCgnYXZhaWwnKTsKICAgICAgZWwuY2xhc3NMaXN0LnJlbW92ZSgnYnVzeScpOwogICAgfSBlbHNlIHsKICAgICAgZWwuY2xhc3NMaXN0LmFkZCgnYnVzeScpOwogICAgICBlbC5jbGFzc0xpc3QucmVtb3ZlKCdhdmFpbCcpOwogICAgfQogIH0pOwp9CgpmdW5jdGlvbiBidWlsZENhbCgpewogIGxvYWRBdmFpbGFibGVEYXlzKCk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NhbE0nKS50ZXh0Q29udGVudD1NT05USFNbY01dKycgJytjWTsKICBib29raW5nLmRhdGU9Jyc7IGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5jZCcpLmZvckVhY2goZnVuY3Rpb24oYyl7Yy5jbGFzc0xpc3QucmVtb3ZlKCdzZWwnKTtjLmNsYXNzTGlzdC5yZW1vdmUoJ2F2YWlsJyk7Yy5jbGFzc0xpc3QucmVtb3ZlKCdidXN5Jyk7fSk7CiAgdmFyIGc9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NhbEcnKTtnLmlubmVySFRNTD0nJzsKICBbJ9Cf0L0nLCfQktGCJywn0KHRgCcsJ9Cn0YInLCfQn9GCJywn0KHQsScsJ9CS0YEnXS5mb3JFYWNoKGZ1bmN0aW9uKGQpewogICAgdmFyIGVsPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO2VsLmNsYXNzTmFtZT0nY2RuJztlbC50ZXh0Q29udGVudD1kO2cuYXBwZW5kQ2hpbGQoZWwpOwogIH0pOwogIHZhciBmaXJzdD1uZXcgRGF0ZShjWSxjTSwxKS5nZXREYXkoKTsKICB2YXIgZGF5cz1uZXcgRGF0ZShjWSxjTSsxLDApLmdldERhdGUoKTsKICB2YXIgc3RhcnQ9Zmlyc3Q9PT0wPzY6Zmlyc3QtMTsKICB2YXIgdG9kYXk9bmV3IERhdGUoKTsKICBmb3IodmFyIGk9MDtpPHN0YXJ0O2krKyl7dmFyIGVsPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO2VsLmNsYXNzTmFtZT0nY2QgcGFkJztnLmFwcGVuZENoaWxkKGVsKTt9CiAgZm9yKHZhciBkYXk9MTtkYXk8PWRheXM7ZGF5KyspewogICAgdmFyIGVsPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO2VsLmNsYXNzTmFtZT0nY2QnOwogICAgdmFyIGRhdGU9bmV3IERhdGUoY1ksY00sZGF5KTsKICAgIHZhciBpc1Bhc3Q9ZGF0ZTxuZXcgRGF0ZSh0b2RheS5nZXRGdWxsWWVhcigpLHRvZGF5LmdldE1vbnRoKCksdG9kYXkuZ2V0RGF0ZSgpKTsKICAgIHZhciBpbm5lcj1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdkaXYnKTtpbm5lci5jbGFzc05hbWU9J2NkLWlubmVyJztpbm5lci50ZXh0Q29udGVudD1kYXk7ZWwuYXBwZW5kQ2hpbGQoaW5uZXIpOwogICAgaWYoaXNQYXN0KXtlbC5jbGFzc0xpc3QuYWRkKCdkaXMnKTt9CiAgICBlbHNlewogICAgICBpZihkYXRlLnRvRGF0ZVN0cmluZygpPT09dG9kYXkudG9EYXRlU3RyaW5nKCkpZWwuY2xhc3NMaXN0LmFkZCgndG9kJyk7CiAgICAgIChmdW5jdGlvbihkLCBlbFJlZil7CiAgICAgICAgZWxSZWYub25jbGljaz1mdW5jdGlvbigpewogICAgICAgICAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmNkJykuZm9yRWFjaChmdW5jdGlvbihjKXtjLmNsYXNzTGlzdC5yZW1vdmUoJ3NlbCcpO30pOwogICAgICAgICAgZWxSZWYuY2xhc3NMaXN0LmFkZCgnc2VsJyk7CiAgICAgICAgICBib29raW5nLmRhdGU9U3RyaW5nKGQpLnBhZFN0YXJ0KDIsJzAnKSsnLicrU3RyaW5nKGNNKzEpLnBhZFN0YXJ0KDIsJzAnKSsnLicrY1k7CiAgICAgICAgICBzaG93VGltZXMoKTsKICAgICAgICB9OwogICAgICB9KShkYXksIGVsKTsKICAgIH0KICAgIGcuYXBwZW5kQ2hpbGQoZWwpOwogIH0KICAvLyBmaWxsIHRyYWlsaW5nIGNlbGxzIHRvIGNvbXBsZXRlIGxhc3QgZ3JpZCByb3cKICB2YXIgdG90YWwgPSBzdGFydCArIGRheXM7CiAgdmFyIHRyYWlsID0gKDcgLSAodG90YWwgJSA3KSkgJSA3OwogIGZvcih2YXIgdD0wO3Q8dHJhaWw7dCsrKXt2YXIgZXA9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7ZXAuY2xhc3NOYW1lPSdjZCBwYWQnO2cuYXBwZW5kQ2hpbGQoZXApO30KfQoKZnVuY3Rpb24gc2hvd1RpbWVzKCl7CiAgdmFyIHRnPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCd0aW1lRycpOwogIHRnLmlubmVySFRNTD0nPGRpdiBjbGFzcz0ibG9hZGluZy1zbG90cyI+4o+zINCX0LDQs9GA0YPQttCw0LXQvCDRgNCw0YHQv9C40YHQsNC90LjQtS4uLjwvZGl2Pic7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3RpbWVTZWMnKS5zdHlsZS5kaXNwbGF5PSdibG9jayc7CgogIHZhciB1cmwgPSB3aW5kb3cubG9jYXRpb24ub3JpZ2luICsgIi9hcGkvc2xvdHMiICsgJz9hY3Rpb249c2xvdHMmZGF0ZT0nICsgZW5jb2RlVVJJQ29tcG9uZW50KGJvb2tpbmcuZGF0ZSkgKyAnJm1hc3Rlcj0nICsgZW5jb2RlVVJJQ29tcG9uZW50KGJvb2tpbmcubWFzdGVyKTsKCiAgZmV0Y2godXJsKQogICAgLnRoZW4oZnVuY3Rpb24ocil7cmV0dXJuIHIuanNvbigpO30pCiAgICAudGhlbihmdW5jdGlvbihkYXRhKXsKICAgICAgdmFyIHNsb3RzID0gKGRhdGEuc2xvdHMgJiYgZGF0YS5zbG90cy5sZW5ndGggPiAwKSA/IGRhdGEuc2xvdHMgOiBbXTsKICAgICAgcmVuZGVyVGltZVNsb3RzKHNsb3RzKTsKICAgIH0pCiAgICAuY2F0Y2goZnVuY3Rpb24oKXsKICAgICAgcmVuZGVyVGltZVNsb3RzKFtdKTsKICAgIH0pOwp9CgpmdW5jdGlvbiByZW5kZXJUaW1lU2xvdHMoc2xvdHMpewogIHZhciB0Zz1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgndGltZUcnKTt0Zy5pbm5lckhUTUw9Jyc7CiAgaWYoc2xvdHMubGVuZ3RoPT09MCl7CiAgICB0Zy5pbm5lckhUTUw9JzxkaXYgY2xhc3M9ImxvYWRpbmctc2xvdHMiPtCd0LXRgiDQtNC+0YHRgtGD0L/QvdGL0YUg0YHQu9C+0YLQvtCyINC90LAg0Y3RgtGDINC00LDRgtGDPC9kaXY+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyIiBvbmNsaWNrPSJzaG93U2NyZWVuKFwnaG9tZVNjcmVlblwnKSIgc3R5bGU9Im1hcmdpbi10b3A6OHB4OyI+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLWljb24iPvCfkL48L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItdGV4dCI+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLXRpdGxlIj7QndC1INC90LDRiNC70Lgg0L/QvtC00YXQvtC00Y/RidC10LUg0LLRgNC10LzRjz88L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItc3ViIj7QodCy0Y/QttC40YLQtdGB0Ywg0YEg0L3QsNC80Lgg0LvRjtCx0YvQvCDRg9C00L7QsdC90YvQvCDRgdC/0L7RgdC+0LHQvtC8IOKAlCDQvNGLINC/0L7QtNCx0LXRgNGR0Lwg0YPQtNC+0LHQvdC+0LUg0LLRgNC10LzRjzwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci1hcnJvdyI+4oaSPC9kaXY+PC9kaXY+JzsKICAgIHJldHVybjsKICB9CiAgc2xvdHMuZm9yRWFjaChmdW5jdGlvbih0KXsKICAgIHZhciBidG49ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnYnV0dG9uJyk7YnRuLmNsYXNzTmFtZT0ndGJ0bic7YnRuLnRleHRDb250ZW50PXQ7CiAgICBidG4ub25jbGljaz1mdW5jdGlvbigpewogICAgICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcudGJ0bicpLmZvckVhY2goZnVuY3Rpb24oYil7Yi5jbGFzc0xpc3QucmVtb3ZlKCdhY3RpdmUnKTt9KTsKICAgICAgYnRuLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpO2Jvb2tpbmcudGltZT10OwogICAgICBzZXRUaW1lb3V0KGZ1bmN0aW9uKCl7Z29TdGVwKDUpO2J1aWxkU3VtKCk7fSwzMDApOwogICAgfTsKICAgIHRnLmFwcGVuZENoaWxkKGJ0bik7CiAgfSk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3RpbWVTZWMnKS5zY3JvbGxJbnRvVmlldyh7YmVoYXZpb3I6J3Ntb290aCcsYmxvY2s6J25lYXJlc3QnfSk7Cn0KCmZ1bmN0aW9uIGJ1aWxkU3VtKCl7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N1bUJsb2NrJykuaW5uZXJIVE1MPQogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fYnJlZWQrJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrKGJvb2tpbmcuYnJlZWREaXNwbGF5fHxib29raW5nLmJyZWVkKSsnPC9zcGFuPjwvZGl2PicrCiAgICAnPGRpdiBjbGFzcz0ic3IiPjxzcGFuIGNsYXNzPSJzbCI+JytUW0xBTkddLnN1bV9zZXJ2aWNlKyc8L3NwYW4+PHNwYW4gY2xhc3M9InN2Ij4nKygoTEFORyE9PSdydScmJlNWQ19UUkFOU0xBVElPTlNbYm9va2luZy5zZXJ2aWNlXSk/U1ZDX1RSQU5TTEFUSU9OU1tib29raW5nLnNlcnZpY2VdW0xBTkddOmJvb2tpbmcuc2VydmljZSkrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fbWFzdGVyKyc8L3NwYW4+PHNwYW4gY2xhc3M9InN2Ij4nK2Jvb2tpbmcubWFzdGVyKyc8L3NwYW4+PC9kaXY+JysKICAgICc8ZGl2IGNsYXNzPSJzciI+PHNwYW4gY2xhc3M9InNsIj4nK1RbTEFOR10uc3VtX2dyb29tKyc8L3NwYW4+PHNwYW4gY2xhc3M9InN2Ij4nK2Jvb2tpbmcuZ3Jvb21IaXN0b3J5Kyc8L3NwYW4+PC9kaXY+JysKICAgICc8ZGl2IGNsYXNzPSJzciI+PHNwYW4gY2xhc3M9InNsIj4nK1RbTEFOR10uc3VtX2RhdGUrJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrYm9va2luZy5kYXRlKyc8L3NwYW4+PC9kaXY+JysKICAgICc8ZGl2IGNsYXNzPSJzciI+PHNwYW4gY2xhc3M9InNsIj4nK1RbTEFOR10uc3VtX3RpbWUrJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrYm9va2luZy50aW1lKyc8L3NwYW4+PC9kaXY+JysKICAgICc8ZGl2IGNsYXNzPSJzciI+PHNwYW4gY2xhc3M9InNsIj4nK1RbTEFOR10uc3VtX3ByaWNlKyc8L3NwYW4+PHNwYW4gY2xhc3M9InNwIj4nK2Jvb2tpbmcucHJpY2UrJyDigqw8L3NwYW4+PC9kaXY+JzsKfQoKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NvbmZpcm1CdG4nKS5vbmNsaWNrID0gZnVuY3Rpb24oKXsKICB2YXIgbmFtZT1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY05hbWUnKS52YWx1ZTsKICB2YXIgcGhvbmU9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NQaG9uZScpLnZhbHVlOwogIGlmKCFuYW1lfHwhcGhvbmUpe2FsZXJ0KFRbTEFOR10uYWxlcnRfZmlsbCk7cmV0dXJuO30KICBpZighL15cK1xkezEwLH0kLy50ZXN0KHBob25lLnRyaW0oKSkpe2FsZXJ0KFRbTEFOR10uYWxlcnRfcGhvbmUpO3JldHVybjt9CiAgYm9va2luZy5uYW1lPW5hbWU7IGJvb2tpbmcucGhvbmU9cGhvbmU7IGJvb2tpbmcuZW1haWw9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NFbWFpbCcpLnZhbHVlOyBib29raW5nLnBldD1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY1BldCcpLnZhbHVlOyBib29raW5nLmxhbmc9TEFORzsKICBib29raW5nLmR1cmF0aW9uID0gYm9va2luZy5icmVlZCA9PT0gJ9Cp0LXQvdC60LgnID8gNjAgOiAoYm9va2luZy5icmVlZCAmJiBib29raW5nLmJyZWVkLmluZGV4T2YoJ9Ca0L7RiNC60LAnKSA9PT0gMCA/IDEyMCA6IDE4MCk7CiAgdmFyIGJ0bj1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY29uZmlybUJ0bicpOwogIGJ0bi50ZXh0Q29udGVudD1UW0xBTkddLnNlbmRpbmc7IGJ0bi5kaXNhYmxlZD10cnVlOwogIGZldGNoKFJBSUxXQVksIHsKICAgIG1ldGhvZDonUE9TVCcsCiAgICBoZWFkZXJzOnsnQ29udGVudC1UeXBlJzonYXBwbGljYXRpb24vanNvbid9LAogICAgYm9keTpKU09OLnN0cmluZ2lmeShib29raW5nKQogIH0pLnRoZW4oZnVuY3Rpb24oKXtzaG93U3VjY2VzcygpO30pLmNhdGNoKGZ1bmN0aW9uKCl7c2hvd1N1Y2Nlc3MoKTt9KTsKfTsKCmZ1bmN0aW9uIHNob3dTdWNjZXNzKCl7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2JrNScpLmNsYXNzTmFtZT0nc3RlcCc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N1Y0Jsb2NrJykuY2xhc3NMaXN0LmFkZCgnc2hvdycpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdwcm9ncmVzcycpLnN0eWxlLmRpc3BsYXk9J25vbmUnOwp9CgpmdW5jdGlvbiByZXNldEFsbCgpewogIGJvb2tpbmc9e2JyZWVkOicnLGJyZWVkRGlzcGxheTonJyxzZXJ2aWNlOicnLHByaWNlOjAsbWFzdGVyOicnLGdyb29tSGlzdG9yeTonJyxkYXRlOicnLHRpbWU6JycsbGFuZzoncnUnfTsKICBzZWxCcmVlZD1udWxsOyBpbnAudmFsdWU9Jyc7IGNsci5jbGFzc0xpc3QucmVtb3ZlKCdzaG93Jyk7CiAgYmFkZ2UuY2xhc3NMaXN0LnJlbW92ZSgnc2hvdycpOyBiYWRnZS5pbm5lckhUTUw9Jyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y1NlYycpLnN0eWxlLmRpc3BsYXk9J25vbmUnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCd0aW1lU2VjJykuc3R5bGUuZGlzcGxheT0nbm9uZSc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N1Y0Jsb2NrJykuY2xhc3NMaXN0LnJlbW92ZSgnc2hvdycpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdwcm9ncmVzcycpLnN0eWxlLmRpc3BsYXk9J2ZsZXgnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjTmFtZScpLnZhbHVlPScnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjUGhvbmUnKS52YWx1ZT0nJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY0VtYWlsJykudmFsdWU9Jyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NQZXQnKS52YWx1ZT0nJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY29uZmlybUJ0bicpLnRleHRDb250ZW50PVRbTEFOR10uY29uZmlybV9idG47CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NvbmZpcm1CdG4nKS5kaXNhYmxlZD1mYWxzZTsKICBnb1N0ZXAoMSk7Cn0KCnZhciBMQU5HID0gbG9jYWxTdG9yYWdlLmdldEl0ZW0oJ3JqbGFuZycpIHx8ICdydSc7CnZhciBUID0gewogIHJ1OnsKICAgIGxvZ29fdGFnOifQn9GA0LXQvNC40LDQu9GM0L3Ri9C5INCz0YDRg9C80LjQvdCzLTxicj7RgdCw0LvQvtC9INCyINCi0LDQu9C70LjQvdC1JywKICAgIGNob29zZV9ob3c6J0Nob29zZSBob3cgdG8gY29ubmVjdCcsCiAgICBib29rX29ubGluZTon0J7QvdC70LDQudC9INCx0YDQvtC90LjRgNC+0LLQsNC90LjQtScsCiAgICBib29rX2Zsb3c6J9Cf0L7RgNC+0LTQsCDihpIg0KPRgdC70YPQs9CwIOKGkiDQnNCw0YHRgtC10YAg4oaSINCS0YDQtdC80Y8nLAogICAgb3JfY29udGFjdDon0LjQu9C4INGB0LLRj9C20LjRgtC10YHRjCDRgSDQvdCw0LzQuCcsCiAgICBjYWxsX3VzOidDYWxsIFVzJywKICAgIGJhY2s6J+KGkCDQndCw0LfQsNC0JywKICAgIGxvZ29fc3ViOidHcm9vbWluZyDCtyDQotCw0LvQu9C40L0nLAogICAgcHNfc2VydmljZTon0KPRgdC70YPQs9CwJyxwc19tYXN0ZXI6J9Cc0LDRgdGC0LXRgCcscHNfcGV0OifQn9C40YLQvtC80LXRhicscHNfZGF0ZTon0JTQsNGC0LAnLHBzX2RldGFpbHM6J9CU0LDQvdC90YvQtScsCiAgICBzdGVwMV9sYmw6JzAxIMK3INCf0L7RgNC+0LTQsCcsCiAgICBicmVlZF9waDon0J3QsNGH0L3QuNGC0LUg0LLQstC+0LTQuNGC0Ywg0L/QvtGA0L7QtNGDLi4uJywKICAgIHN0ZXAyX2xibDonMDIgwrcg0KPRgdC70YPQs9CwJywKICAgIHN0ZXAyX21hc3Rlcjon0JLRi9Cx0LXRgNC40YLQtSDQvNCw0YHRgtC10YDQsCcsCiAgICBzdGVwM19sYmw6J9Ca0LDQuiDQtNCw0LLQvdC+INCy0Ysg0L/QvtGB0LXRidCw0LvQuCDQs9GA0YPQvNC40L3Qsz8nLAogICAgZzE6J9Cf0LXRgNCy0YvQuSDRgNCw0LcnLGcyOifQntGCIDEg0LTQviAzINC80LXRgdGP0YbQtdCyJyxnMzon0J7RgiAzINC00L4gNiDQvNC10YHRj9GG0LXQsicsZzQ6J9CR0L7Qu9C10LUgNiDQvNC10YHRj9GG0LXQsicsCiAgICBzdGVwNF9sYmw6J9CS0YvQsdC10YDQuNGC0LUg0LTQsNGC0YMnLAogICAgY2FsX2F2YWlsOifQldGB0YLRjCDRgdCy0L7QsdC+0LTQvdC+0LUg0LLRgNC10LzRjycsY2FsX25vbmU6J9Ch0LLQvtCx0L7QtNC90L7Qs9C+INCy0YDQtdC80LXQvdC4INC90LXRgicsCiAgICBzdGVwNF90aW1lOifQktGL0LHQtdGA0LjRgtC1INCy0YDQtdC80Y8nLAogICAgc3RlcDVfbGJsOifQktCw0YjQuCDQtNCw0L3QvdGL0LUnLAogICAgbGJsX25hbWU6J9CY0LzRjycscGhfbmFtZTon0JLQsNGI0LUg0LjQvNGPJywKICAgIGxibF9waG9uZTon0KLQtdC70LXRhNC+0L0nLGxibF9lbWFpbDonRW1haWwnLAogICAgbGJsX3BldDon0JrQu9C40YfQutCwINC/0LjRgtC+0LzRhtCwJyxwaF9vcHRpb25hbDon0J3QtdC+0LHRj9C30LDRgtC10LvRjNC90L4nLAogICAgY29uZmlybV9idG46J9Cf0L7QtNGC0LLQtdGA0LTQuNGC0Ywg0LfQsNC/0LjRgdGMJywKICAgIHN1Y2Nlc3NfdGl0bGU6J9CX0LDQv9C40YHRjCDQv9GA0LjQvdGP0YLQsCEnLAogICAgc3VjY2Vzc19zdWI6J9Cc0Ysg0YHQstGP0LbQtdC80YHRjyDRgSDQstCw0LzQuCDQtNC70Y8g0L/QvtC00YLQstC10YDQttC00LXQvdC40Y8uPGJyPtCh0L/QsNGB0LjQsdC+LCDRh9GC0L4g0LLRi9Cx0YDQsNC70LggUiZhbXA7SiBHcm9vbWluZyEnLAogICAgdG9faG9tZTon4oaQINCd0LAg0LPQu9Cw0LLQvdGD0Y4nLAogICAgYWxlcnRfZmlsbDon0JLQstC10LTQuNGC0LUg0LjQvNGPINC4INGC0LXQu9C10YTQvtC9JyxhbGVydF9waG9uZTon0JLQstC10LTQuNGC0LUg0L3QvtC80LXRgCDQsiDRhNC+0YDQvNCw0YLQtSArMzcyMTIzNDU2NzgnLAogICAgc2VuZGluZzon0J7RgtC/0YDQsNCy0LvRj9C10LwuLi4nLAogICAgc3VtX2JyZWVkOifQn9C+0YDQvtC00LAnLHN1bV9zZXJ2aWNlOifQo9GB0LvRg9Cz0LAnLHN1bV9tYXN0ZXI6J9Cc0LDRgdGC0LXRgCcsc3VtX2dyb29tOifQn9C+0YHQu9C10LTQvdC40Lkg0LPRgNGD0LwnLHN1bV9kYXRlOifQlNCw0YLQsCcsc3VtX3RpbWU6J9CS0YDQtdC80Y8nLHN1bV9wcmljZTon0KHRgtC+0LjQvNC+0YHRgtGMJywKICAgIG1vbnRoczpbJ9Cv0L3QstCw0YDRjCcsJ9Ck0LXQstGA0LDQu9GMJywn0JzQsNGA0YInLCfQkNC/0YDQtdC70YwnLCfQnNCw0LknLCfQmNGO0L3RjCcsJ9CY0Y7Qu9GMJywn0JDQstCz0YPRgdGCJywn0KHQtdC90YLRj9Cx0YDRjCcsJ9Ce0LrRgtGP0LHRgNGMJywn0J3QvtGP0LHRgNGMJywn0JTQtdC60LDQsdGA0YwnXQogIH0sCiAgZW46ewogICAgbG9nb190YWc6J1ByZW1pdW0gZ3Jvb21pbmc8YnI+c2Fsb24gaW4gVGFsbGlubicsCiAgICBjaG9vc2VfaG93OidDaG9vc2UgaG93IHRvIGNvbm5lY3QnLAogICAgYm9va19vbmxpbmU6J0Jvb2sgT25saW5lJywKICAgIGJvb2tfZmxvdzonQnJlZWQg4oaSIFNlcnZpY2Ug4oaSIE1hc3RlciDihpIgVGltZScsCiAgICBvcl9jb250YWN0OidvciBjb250YWN0IHVzJywKICAgIGNhbGxfdXM6J0NhbGwgVXMnLAogICAgYmFjazon4oaQIEJhY2snLAogICAgbG9nb19zdWI6J0dyb29taW5nIMK3IFRhbGxpbm4nLAogICAgcHNfc2VydmljZTonU2VydmljZScscHNfbWFzdGVyOidNYXN0ZXInLHBzX3BldDonUGV0Jyxwc19kYXRlOidEYXRlJyxwc19kZXRhaWxzOidEZXRhaWxzJywKICAgIHN0ZXAxX2xibDonMDEgwrcgRG9nIGJyZWVkJywKICAgIGJyZWVkX3BoOidTdGFydCB0eXBpbmcgYnJlZWQuLi4nLAogICAgc3RlcDJfbGJsOicwMiDCtyBTZXJ2aWNlJywKICAgIHN0ZXAyX21hc3RlcjonQ2hvb3NlIG1hc3RlcicsCiAgICBzdGVwM19sYmw6J0hvdyBsb25nIGFnbyB3YXMgeW91ciBsYXN0IGdyb29taW5nPycsCiAgICBnMTonRmlyc3QgdGltZScsZzI6JzHigJMzIG1vbnRocyBhZ28nLGczOicz4oCTNiBtb250aHMgYWdvJyxnNDonT3ZlciA2IG1vbnRocycsCiAgICBzdGVwNF9sYmw6J0Nob29zZSBkYXRlJywKICAgIGNhbF9hdmFpbDonQXZhaWxhYmxlJyxjYWxfbm9uZTonTm90IGF2YWlsYWJsZScsCiAgICBzdGVwNF90aW1lOidDaG9vc2UgdGltZScsCiAgICBzdGVwNV9sYmw6J1lvdXIgZGV0YWlscycsCiAgICBsYmxfbmFtZTonTmFtZScscGhfbmFtZTonWW91ciBuYW1lJywKICAgIGxibF9waG9uZTonUGhvbmUnLGxibF9lbWFpbDonRW1haWwnLAogICAgbGJsX3BldDoiUGV0J3MgbmFtZSIscGhfb3B0aW9uYWw6J09wdGlvbmFsJywKICAgIGNvbmZpcm1fYnRuOidDb25maXJtIGJvb2tpbmcnLAogICAgc3VjY2Vzc190aXRsZTonQm9va2luZyBjb25maXJtZWQhJywKICAgIHN1Y2Nlc3Nfc3ViOidXZSB3aWxsIGNvbnRhY3QgeW91IHRvIGNvbmZpcm0uPGJyPlRoYW5rIHlvdSBmb3IgY2hvb3NpbmcgUiZhbXA7SiBHcm9vbWluZyEnLAogICAgdG9faG9tZTon4oaQIEhvbWUnLAogICAgYWxlcnRfZmlsbDonUGxlYXNlIGVudGVyIG5hbWUgYW5kIHBob25lJyxhbGVydF9waG9uZTonRW50ZXIgcGhvbmUgbnVtYmVyIGluIGZvcm1hdCArMzcyMTIzNDU2NzgnLAogICAgc2VuZGluZzonU2VuZGluZy4uLicsCiAgICBzdW1fYnJlZWQ6J0JyZWVkJyxzdW1fc2VydmljZTonU2VydmljZScsc3VtX21hc3RlcjonTWFzdGVyJyxzdW1fZ3Jvb206J0xhc3QgZ3Jvb21pbmcnLHN1bV9kYXRlOidEYXRlJyxzdW1fdGltZTonVGltZScsc3VtX3ByaWNlOidQcmljZScsCiAgICBtb250aHM6WydKYW51YXJ5JywnRmVicnVhcnknLCdNYXJjaCcsJ0FwcmlsJywnTWF5JywnSnVuZScsJ0p1bHknLCdBdWd1c3QnLCdTZXB0ZW1iZXInLCdPY3RvYmVyJywnTm92ZW1iZXInLCdEZWNlbWJlciddCiAgfSwKICBldDp7CiAgICBsb2dvX3RhZzonRXNtYWtsYXNzaWxpbmUgaG9vbGR1c3RlZW51czxicj5UYWxsaW5uYXMnLAogICAgY2hvb3NlX2hvdzonVmFsaSDDvGhlbmR1c3ZpaXMnLAogICAgYm9va19vbmxpbmU6J0Jyb25lZXJpIHZlZWJpcycsCiAgICBib29rX2Zsb3c6J1TDtXVnIOKGkiBUZWVudXMg4oaSIE1laXN0ZXIg4oaSIEFlZycsCiAgICBvcl9jb250YWN0Oid2w7VpIHbDtXRhIMO8aGVuZHVzdCcsCiAgICBjYWxsX3VzOidIZWxpc3RhIG1laWxlJywKICAgIGJhY2s6J+KGkCBUYWdhc2knLAogICAgbG9nb19zdWI6J0dyb29taW5nIMK3IFRhbGxpbm4nLAogICAgcHNfc2VydmljZTonVGVlbnVzJyxwc19tYXN0ZXI6J01laXN0ZXInLHBzX3BldDonTGVtbWlrbG9vbScscHNfZGF0ZTonS3V1cMOkZXYnLHBzX2RldGFpbHM6J0FuZG1lZCcsCiAgICBzdGVwMV9sYmw6JzAxIMK3IEtvZXJhIHTDtXVnJywKICAgIGJyZWVkX3BoOidBbHVzdGFnZSB0w7V1IHNpc2VzdGFtaXN0Li4uJywKICAgIHN0ZXAyX2xibDonMDIgwrcgVGVlbnVzJywKICAgIHN0ZXAyX21hc3RlcjonVmFsaSBtZWlzdGVyJywKICAgIHN0ZXAzX2xibDonTWlsbGFsIGvDpGlzaXRlIHZpaW1hdGkgZ3Jvb21pbmd1cz8nLAogICAgZzE6J0VzaW1lc3Qga29yZGEnLGcyOicx4oCTMyBrdXVkIHRhZ2FzaScsZzM6JzPigJM2IGt1dWQgdGFnYXNpJyxnNDonw5xsZSA2IGt1dScsCiAgICBzdGVwNF9sYmw6J1ZhbGkga3V1cMOkZXYnLAogICAgY2FsX2F2YWlsOidWYWJ1IGFlZ3Ugb24nLGNhbF9ub25lOidWYWJ1IGFlZ3UgcG9sZScsCiAgICBzdGVwNF90aW1lOidWYWxpIGtlbGxhYWVnJywKICAgIHN0ZXA1X2xibDonVGVpZSBhbmRtZWQnLAogICAgbGJsX25hbWU6J05pbWknLHBoX25hbWU6J1RlaWUgbmltaScsCiAgICBsYmxfcGhvbmU6J1RlbGVmb24nLGxibF9lbWFpbDonRW1haWwnLAogICAgbGJsX3BldDonTGVtbWlrbG9vbWEgbmltaScscGhfb3B0aW9uYWw6J1ZhbGlrdWxpbmUnLAogICAgY29uZmlybV9idG46J0tpbm5pdGEgYnJvbmVlcmluZycsCiAgICBzdWNjZXNzX3RpdGxlOidCcm9uZWVyaW5nIGtpbm5pdGF0dWQhJywKICAgIHN1Y2Nlc3Nfc3ViOidWw7V0YW1lIHRlaWVnYSDDvGhlbmR1c3Qga2lubml0YW1pc2Vrcy48YnI+VMOkbmFtZSwgZXQgdmFsaXNpdGUgUiZhbXA7SiBHcm9vbWluZyEnLAogICAgdG9faG9tZTon4oaQIEF2YWxlaGVsZScsCiAgICBhbGVydF9maWxsOidQYWx1biBzaXNlc3RhZ2UgbmltaSBqYSB0ZWxlZm9uJyxhbGVydF9waG9uZTonU2lzZXN0YWdlIHRlbGVmb25pbnVtYmVyIHZvcm1pbmd1cyArMzcyMTIzNDU2NzgnLAogICAgc2VuZGluZzonU2FhZGFuLi4uJywKICAgIHN1bV9icmVlZDonVMO1dWcnLHN1bV9zZXJ2aWNlOidUZWVudXMnLHN1bV9tYXN0ZXI6J01laXN0ZXInLHN1bV9ncm9vbTonVmlpbWFuZSBncm9vbWluZycsc3VtX2RhdGU6J0t1dXDDpGV2JyxzdW1fdGltZTonS2VsbGFhZWcnLHN1bV9wcmljZTonSGluZCcsCiAgICBtb250aHM6WydKYWFudWFyJywnVmVlYnJ1YXInLCdNw6RydHMnLCdBcHJpbGwnLCdNYWknLCdKdXVuaScsJ0p1dWxpJywnQXVndXN0JywnU2VwdGVtYmVyJywnT2t0b29iZXInLCdOb3ZlbWJlcicsJ0RldHNlbWJlciddCiAgfQp9OwoKZnVuY3Rpb24gc2V0TGFuZyhsKXsKICBMQU5HPWw7CiAgbG9jYWxTdG9yYWdlLnNldEl0ZW0oJ3JqbGFuZycsbCk7CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmxhbmctYnRuJykuZm9yRWFjaChmdW5jdGlvbihiKXsKICAgIGIuY2xhc3NMaXN0LnRvZ2dsZSgnYWN0aXZlJywgYi50ZXh0Q29udGVudC50b0xvd2VyQ2FzZSgpPT09bCk7CiAgfSk7CiAgdmFyIHRyPVRbbF07CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnW2RhdGEtaTE4bl0nKS5mb3JFYWNoKGZ1bmN0aW9uKGVsKXsKICAgIHZhciBrPWVsLmdldEF0dHJpYnV0ZSgnZGF0YS1pMThuJyk7CiAgICBpZih0cltrXSE9PXVuZGVmaW5lZCkgZWwuaW5uZXJIVE1MPXRyW2tdOwogIH0pOwogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tkYXRhLWkxOG4tcGhdJykuZm9yRWFjaChmdW5jdGlvbihlbCl7CiAgICB2YXIgaz1lbC5nZXRBdHRyaWJ1dGUoJ2RhdGEtaTE4bi1waCcpOwogICAgaWYodHJba10hPT11bmRlZmluZWQpIGVsLnBsYWNlaG9sZGVyPXRyW2tdOwogIH0pOwogIE1PTlRIUz10ci5tb250aHM7CiAgYnVpbGRDYWwoKTsKICAvLyBSZS1yZW5kZXIgYmFkZ2UgYW5kIHNlcnZpY2VzIGlmIGJyZWVkIGFscmVhZHkgc2VsZWN0ZWQKICBpZihzZWxCcmVlZCl7CiAgICB2YXIgYmY9bD09PSdlbic/J2JyZWVkX2VuJzpsPT09J2V0Jz8nYnJlZWRfZXQnOidicmVlZCc7CiAgICB2YXIgZGI9c2VsQnJlZWRbYmZdfHxzZWxCcmVlZC5icmVlZDsKICAgIGJvb2tpbmcuYnJlZWREaXNwbGF5PWRiOwogICAgdmFyIGJuRWw9ZG9jdW1lbnQucXVlcnlTZWxlY3RvcignI3NCYWRnZSAuYm5hbWUnKTsKICAgIGlmKGJuRWwpIGJuRWwudGV4dENvbnRlbnQ9ZGI7CiAgICB2YXIgYmNFbD1kb2N1bWVudC5xdWVyeVNlbGVjdG9yKCcjc0JhZGdlIC5iY2hnJyk7CiAgICBpZihiY0VsKSBiY0VsLnRleHRDb250ZW50PWw9PT0nZW4nPydDaGFuZ2UnOmw9PT0nZXQnPydNdXVkYSc6J9CY0LfQvNC10L3QuNGC0YwnOwogICAgaWYoZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y1NlYycpLnN0eWxlLmRpc3BsYXkhPT0nbm9uZScpIHJlbmRlclN2Y3Moc2VsQnJlZWQpOwogICAgdmFyIHNuPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdmNOb3RlJyk7CiAgICBpZihzbil7CiAgICAgIHZhciBudD1sPT09J2VuJz8nUGxlYXNlIG5vdGUnOmw9PT0nZXQnPydQYW5nZSB0w6RoZWxlJzon0JLQsNC20L3QviDQt9C90LDRgtGMJzsKICAgICAgdmFyIG5iPWw9PT0nZW4nPydGaW5hbCBwcmljZSBkZXBlbmRzIG9uIGNvYXQgY29uZGl0aW9uIGFuZCBwZXQgYmVoYXZpb3VyLjxicj5EZW1hdHRpbmcgZnJvbSA1IOKCrC48YnI+QWdncmVzc2l2ZSBiZWhhdmlvdXIgc3VyY2hhcmdlIG1heSBhcHBseTogKzUwJS4nOmw9PT0nZXQnPydMw7VwbGlrIGhpbmQgc8O1bHR1YiBrYXJ2YXN0aWt1IHNlaXN1bmRpc3QgamEgbGVtbWlrbG9vbWEga8OkaXR1bWlzZXN0Ljxicj5Lb2x0c3VuaXRlIGxhaHRpaGFydXRhbWluZSBhbGF0ZXMgNSDigqwuPGJyPkFncmVzc2lpdnNlIGvDpGl0dW1pc2Uga29ycmFsIHbDtWliIGxpc2FuZHVkYSA1MCUganV1cmRlaGluZGx1cy4nOifQntC60L7QvdGH0LDRgtC10LvRjNC90LDRjyDRgdGC0L7QuNC80L7RgdGC0Ywg0LfQsNCy0LjRgdC40YIg0L7RgiDRgdC+0YHRgtC+0Y/QvdC40Y8g0YjQtdGA0YHRgtC4INC4INC/0L7QstC10LTQtdC90LjRjyDQv9C40YLQvtC80YbQsC48YnI+0KDQsNC30LHQvtGAINC60L7Qu9GC0YPQvdC+0LIg4oCUINC+0YIgNSDigqwuPGJyPtCf0YDQuCDQsNCz0YDQtdGB0YHQuNCy0L3QvtC8INC/0L7QstC10LTQtdC90LjQuCDQvNC+0LbQtdGCINC/0YDQuNC80LXQvdGP0YLRjNGB0Y8g0LTQvtC/0LvQsNGC0LAgNTAlLic7CiAgICAgIHNuLmlubmVySFRNTD0nPGRpdiBzdHlsZT0iZm9udC1zaXplOjAuODM4cmVtO2xldHRlci1zcGFjaW5nOi4xNWVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO21hcmdpbi1ib3R0b206OHB4O2ZvbnQtd2VpZ2h0OjYwMDtmb250LWZhbWlseTpcJ01vbnRzZXJyYXRcJyxzYW5zLXNlcmlmIj4nK250Kyc8L2Rpdj48ZGl2IHN0eWxlPSJmb250LXNpemU6MS4wMjVyZW07Y29sb3I6I2ZmZmZmZjtsaW5lLWhlaWdodDoxLjg7Zm9udC1mYW1pbHk6XCdNb250c2VycmF0XCcsc2Fucy1zZXJpZiI+JytuYisnPC9kaXY+JzsKICAgIH0KICB9Cn0KCi8vIEFwcGx5IHNhdmVkIGxhbmd1YWdlIG9uIGxvYWQKKGZ1bmN0aW9uKCl7IHNldExhbmcoTEFORyk7IH0pKCk7CgovLyBDYWxsYmFjayBmb3JtCmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYWxsYmFja0J0bicpLm9uY2xpY2sgPSBmdW5jdGlvbigpewogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtNb2RhbCcpLnN0eWxlLmRpc3BsYXkgPSAnZmxleCc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia05hbWUnKS52YWx1ZSA9ICcnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtQaG9uZScpLnZhbHVlID0gJyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1N1Y2Nlc3MnKS5zdHlsZS5kaXNwbGF5ID0gJ25vbmUnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtTdWJtaXQnKS5zdHlsZS5kaXNwbGF5ID0gJ2Jsb2NrJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrQ2xvc2UnKS50ZXh0Q29udGVudCA9ICfQntGC0LzQtdC90LAnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtTdWJtaXQnKS50ZXh0Q29udGVudCA9ICfQntGC0L/RgNCw0LLQuNGC0YwnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtTdWJtaXQnKS5kaXNhYmxlZCA9IGZhbHNlOwp9Owpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrQ2xvc2UnKS5vbmNsaWNrID0gZnVuY3Rpb24oKXsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrTW9kYWwnKS5zdHlsZS5kaXNwbGF5ID0gJ25vbmUnOwp9Owpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrU3VibWl0Jykub25jbGljayA9IGZ1bmN0aW9uKCl7CiAgdmFyIG5hbWUgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrTmFtZScpLnZhbHVlLnRyaW0oKTsKICB2YXIgcGhvbmUgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrUGhvbmUnKS52YWx1ZS50cmltKCkucmVwbGFjZSgvXEQvZywnJyk7CiAgaWYoIW5hbWUgfHwgIXBob25lKXthbGVydCgn0JLQstC10LTQuNGC0LUg0LjQvNGPINC4INGC0LXQu9C10YTQvtC9Jyk7cmV0dXJuO30KICB2YXIgYnRuID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1N1Ym1pdCcpOwogIGJ0bi50ZXh0Q29udGVudCA9ICfQntGC0L/RgNCw0LLQu9GP0LXQvC4uLic7IGJ0bi5kaXNhYmxlZCA9IHRydWU7CiAgZmV0Y2goJy9hcGkvY2FsbGJhY2snLHsKICAgIG1ldGhvZDonUE9TVCcsCiAgICBoZWFkZXJzOnsnQ29udGVudC1UeXBlJzonYXBwbGljYXRpb24vanNvbid9LAogICAgYm9keTpKU09OLnN0cmluZ2lmeSh7bmFtZTpuYW1lLCBwaG9uZTonKzM3MicrcGhvbmV9KQogIH0pLnRoZW4oZnVuY3Rpb24oKXsKICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtTdWNjZXNzJykuc3R5bGUuZGlzcGxheSA9ICdibG9jayc7CiAgICBidG4uc3R5bGUuZGlzcGxheSA9ICdub25lJzsKICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtDbG9zZScpLnRleHRDb250ZW50ID0gJ+KGkCDQl9Cw0LrRgNGL0YLRjCc7CiAgICBzZXRUaW1lb3V0KGZ1bmN0aW9uKCl7ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia01vZGFsJykuc3R5bGUuZGlzcGxheT0nbm9uZSc7fSwzMDAwKTsKICB9KS5jYXRjaChmdW5jdGlvbigpewogICAgYnRuLnRleHRDb250ZW50ID0gJ9Ce0YLQv9GA0LDQstC40YLRjCc7IGJ0bi5kaXNhYmxlZCA9IGZhbHNlOwogICAgYWxlcnQoJ9Ce0YjQuNCx0LrQsC4g0J/QvtC/0YDQvtCx0YPQudGC0LUg0LXRidGRINGA0LDQty4nKTsKICB9KTsKfTsKCjwvc2NyaXB0Pgo8L2JvZHk+CjwvaHRtbD4K"



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
