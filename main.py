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

_MASTERS = ["татьяна", "алиса", "кристина", "анна", "александра"]

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
BOOKING_HTML_B64 = "77u/PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9InJ1Ij4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idGhlbWUtY29sb3IiIGNvbnRlbnQ9IiMwYTBhMGEiPgo8bWV0YSBuYW1lPSJ2aWV3cG9ydCIgY29udGVudD0id2lkdGg9ZGV2aWNlLXdpZHRoLGluaXRpYWwtc2NhbGU9MSI+Cjx0aXRsZT5SJkogR3Jvb21pbmc8L3RpdGxlPgo8bGluayBocmVmPSJodHRwczovL2ZvbnRzLmdvb2dsZWFwaXMuY29tL2NzczI/ZmFtaWx5PUNvcm1vcmFudCtHYXJhbW9uZDp3Z2h0QDQwMDs2MDAmZmFtaWx5PVBsYXlmYWlyK0Rpc3BsYXk6aXRhbCx3Z2h0QDAsNDAwOzAsNjAwOzAsNzAwOzEsNDAwJmZhbWlseT1Nb250c2VycmF0OndnaHRAMzAwOzQwMDs1MDA7NjAwJmRpc3BsYXk9c3dhcCIgcmVsPSJzdHlsZXNoZWV0Ij4KPHN0eWxlPgoqe2JveC1zaXppbmc6Ym9yZGVyLWJveDttYXJnaW46MDtwYWRkaW5nOjB9Cmh0bWwsYm9keXttaW4taGVpZ2h0OjEwMHZoO2JhY2tncm91bmQ6IzBhMGEwYTtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXdlaWdodDo0MDB9Ci5zY3JlZW57ZGlzcGxheTpub25lO21pbi1oZWlnaHQ6MTAwdmg7ZmxleC1kaXJlY3Rpb246Y29sdW1uO2FsaWduLWl0ZW1zOmNlbnRlcjtwYWRkaW5nOjQ4cHggMCA2NHB4fQouc2NyZWVuLmFjdGl2ZXtkaXNwbGF5OmZsZXh9Ci5jb257d2lkdGg6MTAwJTttYXgtd2lkdGg6NDAwcHg7cGFkZGluZzowIDI4cHh9Ci5iYWNrLWJ0bntkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo4cHg7Y29sb3I6I2ZmZmZmZjtmb250LXNpemU6MC42NHJlbTtsZXR0ZXItc3BhY2luZzouMjJlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7YmFja2dyb3VuZDpub25lO2JvcmRlcjpub25lO2N1cnNvcjpwb2ludGVyO3BhZGRpbmc6MDttYXJnaW4tYm90dG9tOjM2cHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC13ZWlnaHQ6MzAwO3RyYW5zaXRpb246Y29sb3IgLjJzfQouYmFjay1idG46aG92ZXJ7Y29sb3I6I2ZmZmZmZn0KLmxvZ28tcmp7Zm9udC1mYW1pbHk6J0Nvcm1vcmFudCBHYXJhbW9uZCcsc2VyaWY7Zm9udC1zaXplOjJyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNmZmZmZmZ9Ci5sb2dvLXN1Yntmb250LXNpemU6MC41M3JlbTtmb250LXdlaWdodDo2MDA7bGV0dGVyLXNwYWNpbmc6LjRlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjttYXJnaW4tdG9wOjNweDtwYWRkaW5nLWJvdHRvbToxNHB4O2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjAxLDE2OCw3NiwuMik7bWFyZ2luLWJvdHRvbToyMHB4fQouaG9tZS1yantmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZjtmb250LXNpemU6Mi42cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjF9Ci5sb2dvLXRhZ3tmb250LXNpemU6MC42cmVtO2xldHRlci1zcGFjaW5nOi4xMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjEuNX0KLmxvZ28tcm93e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpmbGV4LWVuZDtnYXA6MTJweDttYXJnaW4tYm90dG9tOjRweDtwYWRkaW5nLWJvdHRvbToxOHB4O2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjAxLDE2OCw3NiwuMil9Ci5ob21lLWdzdWJ7Zm9udC1zaXplOjAuNTNyZW07Zm9udC13ZWlnaHQ6NjAwO2xldHRlci1zcGFjaW5nOi40ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7bWFyZ2luLXRvcDo2cHg7bWFyZ2luLWJvdHRvbToyMnB4fQouaG9tZS1oMXtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjIuNXJlbTtmb250LXdlaWdodDo2MDA7Y29sb3I6I2ZmZmZmZjtsaW5lLWhlaWdodDoxLjE7bWFyZ2luLWJvdHRvbTo2cHh9Ci5ob21lLWgxIGVte2ZvbnQtc3R5bGU6aXRhbGljO2NvbG9yOiNmZmZmZmZ9Ci5ob21lLXN1Yntmb250LXNpemU6MC42NHJlbTtsZXR0ZXItc3BhY2luZzouMThlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjI4cHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5vcHR7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTZweDtwYWRkaW5nOjE2cHggMDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNyk7dGV4dC1kZWNvcmF0aW9uOm5vbmU7Y29sb3I6I2ZmZmZmZjt0cmFuc2l0aW9uOmNvbG9yIC4ycztjdXJzb3I6cG9pbnRlcjtiYWNrZ3JvdW5kOm5vbmU7Ym9yZGVyLXRvcDpub25lO2JvcmRlci1sZWZ0Om5vbmU7Ym9yZGVyLXJpZ2h0Om5vbmU7d2lkdGg6MTAwJTtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWZ9Ci5vcHQ6aG92ZXJ7Y29sb3I6I2ZmZn0KLm9wdC1pY29ue3dpZHRoOjM4cHg7aGVpZ2h0OjM4cHg7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2ZsZXgtc2hyaW5rOjB9Ci5vcHQtdGV4dHtmbGV4OjE7dGV4dC1hbGlnbjpsZWZ0fQoub3B0LXRpdGxle2ZvbnQtc2l6ZToxLjIxcmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmO21hcmdpbi1ib3R0b206MnB4O3RyYW5zaXRpb246Y29sb3IgLjJzO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZn0KLm9wdDpob3ZlciAub3B0LXRpdGxle2NvbG9yOiNmZmZ9Ci5vcHQtaGFuZGxle2ZvbnQtc2l6ZTowLjcxcmVtO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC13ZWlnaHQ6MzAwfQoub3B0LWFycm93e2NvbG9yOiNmZmZmZmY7Zm9udC1zaXplOjAuOThyZW07ZmxleC1zaHJpbms6MDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLm9wdDpob3ZlciAub3B0LWFycm93e2NvbG9yOiNmZmZmZmZ9Ci5kaXZpZGVye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7cGFkZGluZzoxMnB4IDB9Ci5kaXZpZGVyOjpiZWZvcmUsLmRpdmlkZXI6OmFmdGVye2NvbnRlbnQ6Jyc7ZmxleDoxO2hlaWdodDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNil9Ci5kaXZpZGVyIHNwYW57Zm9udC1zaXplOjAuNTVyZW07bGV0dGVyLXNwYWNpbmc6LjIyZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5ob21lLWZvb3R7bWFyZ2luLXRvcDozNnB4O3BhZGRpbmctdG9wOjIwcHg7Ym9yZGVyLXRvcDoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDYpO2Rpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjthbGlnbi1pdGVtczpjZW50ZXJ9Ci5ob21lLWZvb3Qgc3Bhbntmb250LXNpemU6MC42MnJlbTtsZXR0ZXItc3BhY2luZzouMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQouZmRvdHt3aWR0aDoycHg7aGVpZ2h0OjJweDtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjE2KX0KLnByb2dyZXNze2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7bWFyZ2luLWJvdHRvbTo0MHB4O292ZXJmbG93OmhpZGRlbjtjb3VudGVyLXJlc2V0OnN0ZXB9Ci5wc3tkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDo1cHg7Zm9udC1zaXplOjAuNTNyZW07bGV0dGVyLXNwYWNpbmc6LjEyZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7d2hpdGUtc3BhY2U6bm93cmFwO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2NvdW50ZXItaW5jcmVtZW50OnN0ZXB9Ci5wcy5kb25le2NvbG9yOiNmZmZmZmZ9Ci5wcy5hY3RpdmV7Y29sb3I6I2ZmZmZmZn0KLnBkb3R7d2lkdGg6MThweDtoZWlnaHQ6MThweDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7ZmxleC1zaHJpbms6MDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEyKTtmb250LXNpemU6MC41M3JlbTtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXdlaWdodDo2MDB9Ci5wZG90OjpiZWZvcmV7Y29udGVudDpjb3VudGVyKHN0ZXAsZGVjaW1hbC1sZWFkaW5nLXplcm8pfQoucHMuZG9uZSAucGRvdHtib3JkZXItY29sb3I6I2ZmZmZmZjtjb2xvcjojZmZmZmZmfQoucHMuYWN0aXZlIC5wZG90e2JvcmRlci1jb2xvcjojZmZmZmZmO2NvbG9yOiNmZmZmZmZ9Ci5wbHtmbGV4OjE7aGVpZ2h0OjFweDtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA3KTttYXJnaW46MCA1cHg7bWluLXdpZHRoOjZweH0KLnBsLmRvbmV7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4xOCl9Ci5zdGVwe2Rpc3BsYXk6bm9uZX0uc3RlcC5zaG93e2Rpc3BsYXk6YmxvY2s7YW5pbWF0aW9uOmZ1IC4zNXMgZWFzZSBib3RofQouc2xibHtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjEuNTVyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbToyMHB4O2xldHRlci1zcGFjaW5nOi4wMWVtfQouc2JveHtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxMHB4O2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjE2KTtwYWRkaW5nOjAgMnB4O3RyYW5zaXRpb246Ym9yZGVyLWNvbG9yIC4yc30KLnNib3g6Zm9jdXMtd2l0aGlue2JvcmRlci1ib3R0b20tY29sb3I6I2ZmZmZmZn0KLnNpe29wYWNpdHk6LjI7Zm9udC1zaXplOjAuOThyZW07ZmxleC1zaHJpbms6MH0KI2JJbnB1dHtmbGV4OjE7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtvdXRsaW5lOm5vbmU7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjIxcmVtO2NvbG9yOiNmZmZmZmY7cGFkZGluZzoxMnB4IDB9CiNiSW5wdXQ6OnBsYWNlaG9sZGVye2NvbG9yOiNmZmZmZmZ9Ci5jbHJ7YmFja2dyb3VuZDpub25lO2JvcmRlcjpub25lO2NvbG9yOiNmZmZmZmY7Y3Vyc29yOnBvaW50ZXI7Zm9udC1zaXplOjAuOTJyZW07ZGlzcGxheTpub25lO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQouY2xyLnNob3d7ZGlzcGxheTpibG9ja30KLmJ3cmFwe3Bvc2l0aW9uOnJlbGF0aXZlO21hcmdpbi1ib3R0b206MjBweH0KLmRyb3B7cG9zaXRpb246YWJzb2x1dGU7bGVmdDowO3JpZ2h0OjA7YmFja2dyb3VuZDojMGYwZjBmO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMSk7Ym9yZGVyLXRvcDpub25lO21heC1oZWlnaHQ6MjAwcHg7b3ZlcmZsb3cteTphdXRvO3otaW5kZXg6NTA7ZGlzcGxheTpub25lfQouZHJvcC5vcGVue2Rpc3BsYXk6YmxvY2t9Ci5kaXRlbXtwYWRkaW5nOjExcHggMTRweDtmb250LXNpemU6MS4wOXJlbTtjb2xvcjojZmZmZmZmO2N1cnNvcjpwb2ludGVyO2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA1KTtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7dHJhbnNpdGlvbjpjb2xvciAuMnN9Ci5kaXRlbTpob3Zlcntjb2xvcjojZmZmfQouZGl0ZW0gbWFya3tiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2NvbG9yOiNmZmY7Zm9udC13ZWlnaHQ6NzAwfQoubm9yZXN7cGFkZGluZzoxNHB4O2ZvbnQtc2l6ZToxLjAzcmVtO2NvbG9yOiNmZmZmZmY7Zm9udC1zdHlsZTppdGFsaWM7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmfQoubm8tYnJlZWQtYmFubmVye2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEycHg7cGFkZGluZzoxNHB4IDA7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDYpO2N1cnNvcjpwb2ludGVyO3RyYW5zaXRpb246Y29sb3IgLjJzO21hcmdpbi10b3A6NHB4fQoubm8tYnJlZWQtYmFubmVyOmhvdmVyIC5uby1icmVlZC1iYW5uZXItdGl0bGV7Y29sb3I6I2ZmZmZmZn0KLm5vLWJyZWVkLWJhbm5lci1pY29ue2ZvbnQtc2l6ZToxLjI2cmVtO2ZsZXgtc2hyaW5rOjA7b3BhY2l0eTouM30KLm5vLWJyZWVkLWJhbm5lci10ZXh0e2ZsZXg6MX0KLm5vLWJyZWVkLWJhbm5lci10aXRsZXtmb250LXNpemU6MS4xNXJlbTtjb2xvcjojZmZmZmZmO2ZvbnQtd2VpZ2h0OjYwMDttYXJnaW4tYm90dG9tOjJweDtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7dHJhbnNpdGlvbjpjb2xvciAuMnN9Ci5uby1icmVlZC1iYW5uZXItc3Vie2ZvbnQtc2l6ZTowLjcxcmVtO2NvbG9yOiNmZmZmZmY7bGluZS1oZWlnaHQ6MS41O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQoubm8tYnJlZWQtYmFubmVyLWFycm93e2NvbG9yOiNmZmZmZmY7Zm9udC1zaXplOjAuOThyZW07ZmxleC1zaHJpbms6MDt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLnNiYWRnZXtkaXNwbGF5Om5vbmU7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxMnB4O21hcmdpbi1ib3R0b206MjBweH0KLnNiYWRnZS5zaG93e2Rpc3BsYXk6ZmxleH0KLmJuYW1le2JvcmRlci1ib3R0b206MXB4IHNvbGlkICNmZmZmZmY7Y29sb3I6I2ZmZmZmZjtwYWRkaW5nOjJweCAwO2ZvbnQtc2l6ZToxLjE1cmVtO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZn0KLmJjaGd7Zm9udC1zaXplOjAuNjRyZW07Y29sb3I6I2ZmZmZmZjtjdXJzb3I6cG9pbnRlcjtsZXR0ZXItc3BhY2luZzouMTJlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7dHJhbnNpdGlvbjpjb2xvciAuMnN9Ci5iY2hnOmhvdmVye2NvbG9yOiNmZmZmZmZ9Ci5zdmJ0bntkaXNwbGF5OmJsb2NrO3BhZGRpbmc6MDtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjpub25lO2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA4KTtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtjdXJzb3I6cG9pbnRlcjt0ZXh0LWFsaWduOmxlZnQ7dHJhbnNpdGlvbjpib3JkZXItY29sb3IgLjJzO3dpZHRoOjEwMCU7b3ZlcmZsb3c6aGlkZGVuO3Bvc2l0aW9uOnJlbGF0aXZlfQouc3ZidG46aG92ZXJ7Ym9yZGVyLWJvdHRvbS1jb2xvcjojZmZmZmZmfQouc3ZidG4uYWN0aXZle2JvcmRlci1ib3R0b20tY29sb3I6I2ZmZmZmZn0KLnN2cHtmb250LXdlaWdodDo2MDA7Y29sb3I6I2ZmZmZmZjtmbGV4LXNocmluazowfQoubWFzdGVyc3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnI7Z2FwOjFweDtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA3KX0KLm1idG57YmFja2dyb3VuZDojMGEwYTBhO3BhZGRpbmc6MjJweCAxMnB4O3RleHQtYWxpZ246Y2VudGVyO2N1cnNvcjpwb2ludGVyO3RyYW5zaXRpb246YmFja2dyb3VuZCAuMnM7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2JvcmRlcjpub25lfQoubWJ0bjpob3ZlcntiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjAzKX0KLm1idG4uYWN0aXZle2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDUpfQoubWF2e3dpZHRoOjQwcHg7aGVpZ2h0OjQwcHg7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjE0KTtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7bWFyZ2luOjAgYXV0byAxMHB4O2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6MS4xNXJlbTtmb250LXdlaWdodDo2MDA7Y29sb3I6I2ZmZmZmZn0KLm1idG4uYWN0aXZlIC5tYXZ7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLm1uYW1le2ZvbnQtc2l6ZToxLjE1cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZn0KLm1idG46aG92ZXIgLm1uYW1le2NvbG9yOiNmZmZmZmZ9Ci5tYnRuLmFjdGl2ZSAubW5hbWV7Y29sb3I6I2ZmZmZmZn0KLm10aXRsZXtmb250LXNpemU6MC42NHJlbTtjb2xvcjojZmZmZmZmO21hcmdpbi10b3A6M3B4O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQouZ2J0bntkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO3BhZGRpbmc6MTRweCAwO2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOm5vbmU7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDgpO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToxLjE1cmVtO2N1cnNvcjpwb2ludGVyO3dpZHRoOjEwMCU7dHJhbnNpdGlvbjphbGwgLjJzfQouZ2J0bjpob3Zlcntjb2xvcjojZmZmZmZmfQouZ2J0bi5hY3RpdmV7Y29sb3I6I2ZmZmZmZjtib3JkZXItYm90dG9tLWNvbG9yOiNmZmZmZmZ9Ci5jYWwtaHtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6Y2VudGVyO21hcmdpbi1ib3R0b206MTZweH0KLmNhbC1te2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6MS41NXJlbTtmb250LXdlaWdodDo2MDA7Y29sb3I6I2ZmZmZmZn0KLmNhbC1ue2JhY2tncm91bmQ6bm9uZTtib3JkZXI6bm9uZTtjb2xvcjojZmZmZmZmO2N1cnNvcjpwb2ludGVyO2ZvbnQtc2l6ZToxLjI2cmVtO3BhZGRpbmc6NHB4IDhweDt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLmNhbC1uOmhvdmVye2NvbG9yOiNmZmZmZmZ9Ci5jZ3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOnJlcGVhdCg3LDFmcik7Z2FwOjJweDttYXJnaW4tYm90dG9tOjEycHh9Ci5jZG57dGV4dC1hbGlnbjpjZW50ZXI7Zm9udC1zaXplOjAuNTNyZW07Y29sb3I6I2ZmZmZmZjtwYWRkaW5nOjRweCAwO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtsZXR0ZXItc3BhY2luZzouMWVtfQouY2R7dGV4dC1hbGlnbjpjZW50ZXI7Y3Vyc29yOnBvaW50ZXI7Y29sb3I6I2ZmZmZmZjtib3JkZXI6MXB4IHNvbGlkIHRyYW5zcGFyZW50O3RyYW5zaXRpb246YWxsIC4yc30KLmNkOmhvdmVyOm5vdCguZGlzKTpub3QoLnBhZCkgLmNkLWlubmVye2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDcpIWltcG9ydGFudDtjb2xvcjojZmZmZmZmIWltcG9ydGFudH0KLmNkLnNlbCAuY2QtaW5uZXJ7YmFja2dyb3VuZDojZmZmZmZmIWltcG9ydGFudDtjb2xvcjojMGEwYTBhIWltcG9ydGFudDtmb250LXdlaWdodDo3MDAhaW1wb3J0YW50O2JvcmRlcjpub25lIWltcG9ydGFudH0KLmNkLnRvZCAuY2QtaW5uZXJ7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4yOCk7Y29sb3I6I2ZmZn0KLmNkLmRpc3tjb2xvcjojZmZmZmZmO2N1cnNvcjpkZWZhdWx0fQoudGd7ZGlzcGxheTpncmlkO2dyaWQtdGVtcGxhdGUtY29sdW1uczpyZXBlYXQoNCwxZnIpO2dhcDoxcHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNyl9Ci50YnRue2JhY2tncm91bmQ6IzBhMGEwYTtib3JkZXI6bm9uZTtwYWRkaW5nOjEzcHggNHB4O3RleHQtYWxpZ246Y2VudGVyO2ZvbnQtc2l6ZToxLjA2cmVtO2NvbG9yOiNmZmZmZmY7Y3Vyc29yOnBvaW50ZXI7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO3RyYW5zaXRpb246YWxsIC4yc30KLnRidG46aG92ZXJ7Y29sb3I6I2ZmZmZmZjtiYWNrZ3JvdW5kOnJnYmEoMjU1LDI1NSwyNTUsLjA0KX0KLnRidG4uYWN0aXZle2NvbG9yOiNmZmZmZmZ9Ci5zdW17YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtib3JkZXItdG9wOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wOCk7cGFkZGluZzoyMHB4IDA7bWFyZ2luLWJvdHRvbToyMHB4fQouc3J7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO3BhZGRpbmc6OHB4IDA7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDUpO2ZvbnQtc2l6ZToxLjA5cmVtO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZn0KLnNyOmxhc3QtY2hpbGR7Ym9yZGVyLWJvdHRvbTpub25lO3BhZGRpbmctdG9wOjE0cHh9Ci5zbHtjb2xvcjojZmZmZmZmfS5zdntjb2xvcjojZmZmZmZmO3RleHQtYWxpZ246cmlnaHR9Ci5zcHtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjEuOTVyZW07Y29sb3I6I2ZmZmZmZjtmb250LXdlaWdodDo2MDB9Ci5mZ3ttYXJnaW4tYm90dG9tOjIwcHh9Ci5mbHtmb250LXNpemU6MC41N3JlbTtsZXR0ZXItc3BhY2luZzouMjJlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6I2ZmZmZmZjttYXJnaW4tYm90dG9tOjhweDtkaXNwbGF5OmJsb2NrO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmfQouZml7d2lkdGg6MTAwJTtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjpub25lO2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjE0KTtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6MS4yMXJlbTtwYWRkaW5nOjEwcHggMDtvdXRsaW5lOm5vbmU7dHJhbnNpdGlvbjpib3JkZXItY29sb3IgLjJzfQouZmk6Zm9jdXN7Ym9yZGVyLWJvdHRvbS1jb2xvcjojZmZmZmZmfQouY2J0bntkaXNwbGF5OmJsb2NrO3dpZHRoOjEwMCU7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZTowLjY5cmVtO2ZvbnQtd2VpZ2h0OjYwMDtsZXR0ZXItc3BhY2luZzouMjhlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7dGV4dC1hbGlnbjpjZW50ZXI7cGFkZGluZzoxNnB4O2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMjUpO2N1cnNvcjpwb2ludGVyO3RyYW5zaXRpb246YWxsIC4yc30KLmNidG46aG92ZXJ7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLnNibG9ja3t0ZXh0LWFsaWduOmNlbnRlcjtwYWRkaW5nOjUycHggMjBweDtkaXNwbGF5Om5vbmV9Ci5zYmxvY2suc2hvd3tkaXNwbGF5OmJsb2NrO2FuaW1hdGlvbjpmdSAuNXMgZWFzZSBib3RofQouc2kye2ZvbnQtc2l6ZToyLjg4cmVtO21hcmdpbi1ib3R0b206MjBweDtvcGFjaXR5Oi40fQouc3R7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc2l6ZToyLjE4cmVtO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbToxMHB4O2ZvbnQtd2VpZ2h0OjYwMH0KLnNze2ZvbnQtc2l6ZTowLjg2cmVtO2NvbG9yOiNmZmZmZmY7bGluZS1oZWlnaHQ6MS45O21hcmdpbi1ib3R0b206MjhweDtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZn0KLmhidG57YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjE2KTtjb2xvcjojZmZmZmZmO2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZTowLjY5cmVtO2xldHRlci1zcGFjaW5nOi4yMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtwYWRkaW5nOjEzcHggMjhweDtjdXJzb3I6cG9pbnRlcjt0cmFuc2l0aW9uOmFsbCAuMnN9Ci5oYnRuOmhvdmVye2JvcmRlci1jb2xvcjojZmZmZmZmO2NvbG9yOiNmZmZmZmZ9Ci5sb2FkaW5nLXNsb3Rze2NvbG9yOiNmZmZmZmY7Zm9udC1zaXplOjEuMDNyZW07cGFkZGluZzoxMnB4IDA7dGV4dC1hbGlnbjpjZW50ZXI7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmO2ZvbnQtc3R5bGU6aXRhbGljfQouY2R7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpjZW50ZXI7YWxpZ24taXRlbXM6Y2VudGVyO2hlaWdodDozNnB4IWltcG9ydGFudDtwYWRkaW5nOjAhaW1wb3J0YW50fQouY2QtaW5uZXJ7d2lkdGg6MzJweDtoZWlnaHQ6MzJweDtib3JkZXItcmFkaXVzOjA7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtqdXN0aWZ5LWNvbnRlbnQ6Y2VudGVyO2ZvbnQtc2l6ZTowLjkycmVtO2N1cnNvcjpwb2ludGVyO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZn0KLmNkLmF2YWlsIC5jZC1pbm5lcntib3JkZXI6MXB4IHNvbGlkIHJnYmEoOTAsMTgwLDkwLC4zNSk7Y29sb3I6cmdiYSg5MCwxODAsOTAsLjY1KX0KLmNkLmJ1c3kgLmNkLWlubmVye2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDcpO2NvbG9yOiNmZmZmZmZ9Ci5jZC5zZWwgLmNkLWlubmVye2JhY2tncm91bmQ6I2ZmZmZmZiFpbXBvcnRhbnQ7Y29sb3I6IzBhMGEwYSFpbXBvcnRhbnQ7Zm9udC13ZWlnaHQ6NzAwIWltcG9ydGFudDtib3JkZXI6bm9uZSFpbXBvcnRhbnR9Ci5jZC50b2QgLmNkLWlubmVye2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMjgpO2NvbG9yOiNmZmY7Zm9udC13ZWlnaHQ6NjAwfQouY2QuZGlzIC5jZC1pbm5lcntjb2xvcjojZmZmZmZmO2N1cnNvcjpkZWZhdWx0O2JhY2tncm91bmQ6bm9uZTtib3JkZXI6bm9uZX0KLnN2YnRuLXJvd3tkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6YmFzZWxpbmU7bWFyZ2luLWJvdHRvbTo2cHg7cGFkZGluZzoxNnB4IDAgMH0KLnN2YnRuLW5hbWV7Zm9udC1zaXplOjEuMjFyZW07Y29sb3I6I2ZmZmZmZjtmb250LXdlaWdodDo2MDA7Zm9udC1mYW1pbHk6J1BsYXlmYWlyIERpc3BsYXknLHNlcmlmfQouc3ZidG4uYWN0aXZlIC5zdmJ0bi1uYW1le2NvbG9yOiNmZmZmZmZ9Ci5zdmJ0bi1wcmljZXtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjEuMzhyZW07Y29sb3I6I2ZmZmZmZjtmb250LXdlaWdodDo2MDA7ZmxleC1zaHJpbms6MH0KLnN2YnRuLmFjdGl2ZSAuc3ZidG4tcHJpY2V7Y29sb3I6I2ZmZmZmZn0KLnN2YnRuLWRlc2N7Zm9udC1zaXplOjAuOHJlbTtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjEuNztkaXNwbGF5OmJsb2NrO3BhZGRpbmc6MCAwIDE0cHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5zdmJ0bi5hY3RpdmUgLnN2YnRuLWRlc2N7Y29sb3I6I2ZmZmZmZn0KLnN2YnRuLXRhZ3tmb250LXNpemU6MC43OHJlbTtjb2xvcjojZmZmZmZmO2ZvbnQtc3R5bGU6aXRhbGljO2Rpc3BsYXk6YmxvY2s7bWFyZ2luLXRvcDoycHg7cGFkZGluZzowIDAgMTRweDtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWZ9Ci5zdmJ0bi5hY3RpdmUgLnN2YnRuLXRhZ3tjb2xvcjojZmZmZmZmfQpAbWVkaWEobWF4LXdpZHRoOjQwMHB4KXsuc3ZidG4tbmFtZXtmb250LXNpemU6MS4wOXJlbX0uc3ZidG4tcHJpY2V7Zm9udC1zaXplOjEuMjFyZW19LnN2YnRuLWRlc2N7Zm9udC1zaXplOjAuNzVyZW19LnN2YnRuLXRhZ3tmb250LXNpemU6MC43MXJlbX19CkBrZXlmcmFtZXMgZnV7ZnJvbXtvcGFjaXR5OjA7dHJhbnNmb3JtOnRyYW5zbGF0ZVkoMTBweCl9dG97b3BhY2l0eToxO3RyYW5zZm9ybTp0cmFuc2xhdGVZKDApfX0KLmxhbmctYmFye3Bvc2l0aW9uOmZpeGVkO3RvcDoxMnB4O3JpZ2h0OjE0cHg7ei1pbmRleDo5OTk7ZGlzcGxheTpmbGV4O2dhcDo2cHh9Ci5sYW5nLWJ0bntiYWNrZ3JvdW5kOnJnYmEoMTAsMTAsMTAsLjkyKTtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEpO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC1zaXplOjAuNjJyZW07bGV0dGVyLXNwYWNpbmc6LjE1ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO3BhZGRpbmc6NXB4IDEwcHg7Y3Vyc29yOnBvaW50ZXI7dHJhbnNpdGlvbjphbGwgLjJzfQoubGFuZy1idG46aG92ZXJ7Ym9yZGVyLWNvbG9yOiNmZmZmZmY7Y29sb3I6I2ZmZmZmZn0KLmxhbmctYnRuLmFjdGl2ZXtib3JkZXItY29sb3I6I2ZmZmZmZjtjb2xvcjojZmZmZmZmfQouY2JrLWJ0bntiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTQpO2NvbG9yOiNmZmZmZmY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC1zaXplOjAuNjlyZW07bGV0dGVyLXNwYWNpbmc6LjE2ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO3BhZGRpbmc6MTJweCAyMHB4O2N1cnNvcjpwb2ludGVyO3RyYW5zaXRpb246YWxsIC4yczt3aWR0aDoxMDAlfQouY2JrLWJ0bjpob3Zlcntib3JkZXItY29sb3I6I2ZmZmZmZjtjb2xvcjojZmZmZmZmfQo8L3N0eWxlPgo8L2hlYWQ+Cjxib2R5Pgo8ZGl2IGNsYXNzPSJsYW5nLWJhciI+CiAgPGJ1dHRvbiBjbGFzcz0ibGFuZy1idG4gYWN0aXZlIiBvbmNsaWNrPSJzZXRMYW5nKCdydScpIj5SVTwvYnV0dG9uPgogIDxidXR0b24gY2xhc3M9ImxhbmctYnRuIiBvbmNsaWNrPSJzZXRMYW5nKCdlbicpIj5FTjwvYnV0dG9uPgogIDxidXR0b24gY2xhc3M9ImxhbmctYnRuIiBvbmNsaWNrPSJzZXRMYW5nKCdldCcpIj5FVDwvYnV0dG9uPgo8L2Rpdj4KCjwhLS0gSE9NRSAtLT4KPGRpdiBjbGFzcz0ic2NyZWVuIGFjdGl2ZSIgaWQ9ImhvbWVTY3JlZW4iPgo8ZGl2IGNsYXNzPSJjb24iPgogIDxkaXYgY2xhc3M9ImxvZ28tcm93Ij4KICAgIDxkaXYgY2xhc3M9ImhvbWUtcmoiPlImYW1wO0o8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImxvZ28tdGFnIiBkYXRhLWkxOG49ImxvZ29fdGFnIj7Qn9GA0LXQvNC40LDQu9GM0L3Ri9C5INCz0YDRg9C80LjQvdCzLTxicj7RgdCw0LvQvtC9INCyINCi0LDQu9C70LjQvdC1PC9kaXY+CiAgPC9kaXY+CiAgPGRpdiBjbGFzcz0iaG9tZS1nc3ViIj5Hcm9vbWluZzwvZGl2PgogIDxkaXYgY2xhc3M9ImhvbWUtaDEiPkJvb2sgdGhlIHdheSA8ZW0+eW91IGxpa2U8L2VtPjwvZGl2PgogIDxkaXYgY2xhc3M9ImhvbWUtc3ViIiBkYXRhLWkxOG49ImNob29zZV9ob3ciPkNob29zZSBob3cgdG8gY29ubmVjdDwvZGl2PgoKICA8YnV0dG9uIGNsYXNzPSJvcHQiIGlkPSJib29rQnRuIj4KICAgIDxkaXYgY2xhc3M9Im9wdC1pY29uIj48c3ZnIHdpZHRoPSIzNiIgaGVpZ2h0PSIzNiIgdmlld0JveD0iMCAwIDI0IDI0IiB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciPjxyZWN0IHdpZHRoPSIyNCIgaGVpZ2h0PSIyNCIgcng9IjYiIGZpbGw9InJnYmEoMjU1LDI1NSwyNTUsLjA4KSIvPjxyZWN0IHg9IjUiIHk9IjciIHdpZHRoPSIxNCIgaGVpZ2h0PSIxMyIgcng9IjEuNSIgZmlsbD0ibm9uZSIgc3Ryb2tlPSJyZ2JhKDI1NSwyNTUsMjU1LC41NSkiIHN0cm9rZS13aWR0aD0iMS41Ii8+PHBhdGggZD0iTTggNXY0TTE2IDV2NE01IDExaDE0IiBzdHJva2U9InJnYmEoMjU1LDI1NSwyNTUsLjU1KSIgc3Ryb2tlLXdpZHRoPSIxLjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPjxjaXJjbGUgY3g9IjguNSIgY3k9IjE1IiByPSIxIiBmaWxsPSJyZ2JhKDI1NSwyNTUsMjU1LC41NSkiLz48Y2lyY2xlIGN4PSIxMiIgY3k9IjE1IiByPSIxIiBmaWxsPSJyZ2JhKDI1NSwyNTUsMjU1LC41NSkiLz48Y2lyY2xlIGN4PSIxNS41IiBjeT0iMTUiIHI9IjEiIGZpbGw9InJnYmEoMjU1LDI1NSwyNTUsLjU1KSIvPjwvc3ZnPjwvZGl2PgogICAgPGRpdiBjbGFzcz0ib3B0LXRleHQiPjxkaXYgY2xhc3M9Im9wdC10aXRsZSIgZGF0YS1pMThuPSJib29rX29ubGluZSI+Qm9vayBPbmxpbmU8L2Rpdj48ZGl2IGNsYXNzPSJvcHQtaGFuZGxlIiBkYXRhLWkxOG49ImJvb2tfZmxvdyI+0J/QvtGA0L7QtNCwIOKGkiDQo9GB0LvRg9Cz0LAg4oaSINCc0LDRgdGC0LXRgCDihpIg0JLRgNC10LzRjzwvZGl2PjwvZGl2PgogICAgPHNwYW4gY2xhc3M9Im9wdC1hcnJvdyI+4oaSPC9zcGFuPgogIDwvYnV0dG9uPgogIDxkaXYgY2xhc3M9ImRpdmlkZXIiPjxzcGFuIGRhdGEtaTE4bj0ib3JfY29udGFjdCI+b3IgY29udGFjdCB1czwvc3Bhbj48L2Rpdj4KICA8YSBocmVmPSJodHRwczovL3d3dy5pbnN0YWdyYW0uY29tL3JqX2dyb29taW5nP2lnc2g9TVd4bWRITnFjWEZrYW5OdmJRPT0iIHRhcmdldD0iX2JsYW5rIiBjbGFzcz0ib3B0Ij4KICAgIDxkaXYgY2xhc3M9Im9wdC1pY29uIj48c3ZnIHdpZHRoPSIzNiIgaGVpZ2h0PSIzNiIgdmlld0JveD0iMCAwIDI0IDI0IiB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciPjxkZWZzPjxsaW5lYXJHcmFkaWVudCBpZD0iaWciIHgxPSIwJSIgeTE9IjEwMCUiIHgyPSIxMDAlIiB5Mj0iMCUiPjxzdG9wIG9mZnNldD0iMCUiIHN0b3AtY29sb3I9IiNmMDk0MzMiLz48c3RvcCBvZmZzZXQ9IjUwJSIgc3RvcC1jb2xvcj0iI2RjMjc0MyIvPjxzdG9wIG9mZnNldD0iMTAwJSIgc3RvcC1jb2xvcj0iI2JjMTg4OCIvPjwvbGluZWFyR3JhZGllbnQ+PC9kZWZzPjxyZWN0IHdpZHRoPSIyNCIgaGVpZ2h0PSIyNCIgcng9IjYiIGZpbGw9InVybCgjaWcpIi8+PHJlY3QgeD0iNiIgeT0iNiIgd2lkdGg9IjEyIiBoZWlnaHQ9IjEyIiByeD0iMyIgZmlsbD0ibm9uZSIgc3Ryb2tlPSJ3aGl0ZSIgc3Ryb2tlLXdpZHRoPSIxLjUiLz48Y2lyY2xlIGN4PSIxMiIgY3k9IjEyIiByPSIzIiBmaWxsPSJub25lIiBzdHJva2U9IndoaXRlIiBzdHJva2Utd2lkdGg9IjEuNSIvPjxjaXJjbGUgY3g9IjE2LjUiIGN5PSI3LjUiIHI9IjEiIGZpbGw9IndoaXRlIi8+PC9zdmc+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJvcHQtdGV4dCI+PGRpdiBjbGFzcz0ib3B0LXRpdGxlIj5JbnN0YWdyYW08L2Rpdj48ZGl2IGNsYXNzPSJvcHQtaGFuZGxlIj5AcmpfZ3Jvb21pbmc8L2Rpdj48L2Rpdj4KICAgIDxzcGFuIGNsYXNzPSJvcHQtYXJyb3ciPuKGkjwvc3Bhbj4KICA8L2E+CiAgPGEgaHJlZj0iaHR0cHM6Ly93YS5tZS8zNzI1ODczNTQ1NiIgdGFyZ2V0PSJfYmxhbmsiIGNsYXNzPSJvcHQiPgogICAgPGRpdiBjbGFzcz0ib3B0LWljb24iPjxzdmcgd2lkdGg9IjM2IiBoZWlnaHQ9IjM2IiB2aWV3Qm94PSIwIDAgMjQgMjQiIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyI+PGNpcmNsZSBjeD0iMTIiIGN5PSIxMiIgcj0iMTIiIGZpbGw9IiMyNUQzNjYiLz48cGF0aCBmaWxsPSJ3aGl0ZSIgZD0iTTE3LjQ3MiAxNC4zODJjLS4yOTctLjE0OS0xLjc1OC0uODY3LTIuMDMtLjk2Ny0uMjczLS4wOTktLjQ3MS0uMTQ4LS42Ny4xNS0uMTk3LjI5Ny0uNzY3Ljk2Ni0uOTQgMS4xNjQtLjE3My4xOTktLjM0Ny4yMjMtLjY0NC4wNzUtLjI5Ny0uMTUtMS4yNTUtLjQ2My0yLjM5LTEuNDc1LS44ODMtLjc4OC0xLjQ4LTEuNzYxLTEuNjUzLTIuMDU5LS4xNzMtLjI5Ny0uMDE4LS40NTguMTMtLjYwNi4xMzQtLjEzMy4yOTgtLjM0Ny40NDYtLjUyLjE0OS0uMTc0LjE5OC0uMjk4LjI5OC0uNDk3LjA5OS0uMTk4LjA1LS4zNzEtLjAyNS0uNTItLjA3NS0uMTQ5LS42NjktMS42MTItLjkxNi0yLjIwNy0uMjQyLS41NzktLjQ4Ny0uNS0uNjY5LS41MS0uMTczLS4wMDgtLjM3MS0uMDEtLjU3LS4wMS0uMTk4IDAtLjUyLjA3NC0uNzkyLjM3Mi0uMjcyLjI5Ny0xLjA0IDEuMDE2LTEuMDQgMi40NzkgMCAxLjQ2MiAxLjA2NSAyLjg3NSAxLjIxMyAzLjA3NC4xNDkuMTk4IDIuMDk2IDMuMiA1LjA3NyA0LjQ4Ny43MDkuMzA2IDEuMjYyLjQ4OSAxLjY5NC42MjUuNzEyLjIyNyAxLjM2LjE5NSAxLjg3MS4xMTguNTcxLS4wODUgMS43NTgtLjcxOSAyLjAwNi0xLjQxMy4yNDgtLjY5NC4yNDgtMS4yODkuMTczLTEuNDEzLS4wNzQtLjEyNC0uMjcyLS4xOTgtLjU3LS4zNDciLz48L3N2Zz48L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im9wdC10ZXh0Ij48ZGl2IGNsYXNzPSJvcHQtdGl0bGUiPldoYXRzQXBwPC9kaXY+PGRpdiBjbGFzcz0ib3B0LWhhbmRsZSI+KzM3MiA1ODcgMzU0NTY8L2Rpdj48L2Rpdj4KICAgIDxzcGFuIGNsYXNzPSJvcHQtYXJyb3ciPuKGkjwvc3Bhbj4KICA8L2E+CiAgPGEgaHJlZj0iaHR0cHM6Ly93d3cuZmFjZWJvb2suY29tL3NoYXJlLzFFTFA2S0M2clYvP21pYmV4dGlkPXd3WElmciIgdGFyZ2V0PSJfYmxhbmsiIGNsYXNzPSJvcHQiPgogICAgPGRpdiBjbGFzcz0ib3B0LWljb24iPjxzdmcgd2lkdGg9IjM2IiBoZWlnaHQ9IjM2IiB2aWV3Qm94PSIwIDAgMjQgMjQiIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyI+PGNpcmNsZSBjeD0iMTIiIGN5PSIxMiIgcj0iMTIiIGZpbGw9IiMxODc3RjIiLz48cGF0aCBmaWxsPSJ3aGl0ZSIgZD0iTTEzIDEwLjVoMmwuNS0yLjVIMTNWNi41YzAtLjcuMi0xLjUgMS41LTEuNUgxNlYzcy0xLS4yLTItLjJjLTIuMSAwLTMuNSAxLjMtMy41IDMuNVY4SDh2Mi41aDIuNVYxOEgxM3YtNy41eiIvPjwvc3ZnPjwvZGl2PgogICAgPGRpdiBjbGFzcz0ib3B0LXRleHQiPjxkaXYgY2xhc3M9Im9wdC10aXRsZSI+RmFjZWJvb2s8L2Rpdj48ZGl2IGNsYXNzPSJvcHQtaGFuZGxlIj5SJmFtcDtKIEdyb29taW5nPC9kaXY+PC9kaXY+CiAgICA8c3BhbiBjbGFzcz0ib3B0LWFycm93Ij7ihpI8L3NwYW4+CiAgPC9hPgogIDxidXR0b24gY2xhc3M9Im9wdCIgb25jbGljaz0id2luZG93LmxvY2F0aW9uLmhyZWY9J3RlbDorMzcyNTg3MzU0NTYnIj4KICAgIDxkaXYgY2xhc3M9Im9wdC1pY29uIj48c3ZnIHdpZHRoPSIzMiIgaGVpZ2h0PSIzMiIgdmlld0JveD0iMCAwIDI0IDI0IiBmaWxsPSJub25lIiBzdHJva2U9InJnYmEoMjU1LDI1NSwyNTUsLjQ1KSIgc3Ryb2tlLXdpZHRoPSIxLjYiPjxwYXRoIGQ9Ik0yMiAxNi45MnYzYTIgMiAwIDAxLTIuMTggMiAxOS43OSAxOS43OSAwIDAxLTguNjMtMy4wN0ExOS41IDE5LjUgMCAwMTMuMDcgOS44MmExOS43OSAxOS43OSAwIDAxLTMuMDctOC42N0EyIDIgMCAwMTIgMWgzYTIgMiAwIDAxMiAxLjcyYy4xMjcuOTYuMzYxIDEuOTAzLjcgMi44MWEyIDIgMCAwMS0uNDUgMi4xMUw2LjkxIDguOTFhMTYgMTYgMCAwMDYgNmwxLjI3LTEuMjdhMiAyIDAgMDEyLjExLS40NWMuOTA3LjMzOSAxLjg1LjU3MyAyLjgxLjdBMiAyIDAgMDEyMiAxNi45MnoiLz48L3N2Zz48L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im9wdC10ZXh0Ij48ZGl2IGNsYXNzPSJvcHQtdGl0bGUiIGRhdGEtaTE4bj0iY2FsbF91cyI+Q2FsbCBVczwvZGl2PjxkaXYgY2xhc3M9Im9wdC1oYW5kbGUiPiszNzIgNTg3IDM1NDU2PC9kaXY+PC9kaXY+CiAgICA8c3BhbiBjbGFzcz0ib3B0LWFycm93Ij7ihpI8L3NwYW4+CiAgPC9idXR0b24+CiAgPGRpdiBjbGFzcz0iaG9tZS1mb290Ij4KICAgIDxzcGFuPlRhbGxpbm48L3NwYW4+PGRpdiBjbGFzcz0iZmRvdCI+PC9kaXY+PHNwYW4+RXN0b25pYTwvc3Bhbj48ZGl2IGNsYXNzPSJmZG90Ij48L2Rpdj48c3Bhbj5BbGx2ZWVsYWV2YSA0PC9zcGFuPgogIDwvZGl2Pgo8L2Rpdj4KPC9kaXY+Cgo8IS0tIEJPT0tJTkcgLS0+CjxkaXYgY2xhc3M9InNjcmVlbiIgaWQ9ImJvb2tTY3JlZW4iPgo8ZGl2IGNsYXNzPSJjb24iPgogIDxidXR0b24gY2xhc3M9ImJhY2stYnRuIiBpZD0iYmFja0J0biIgZGF0YS1pMThuPSJiYWNrIj7ihpAg0J3QsNC30LDQtDwvYnV0dG9uPgogIDxkaXYgY2xhc3M9ImxvZ28tcmoiPlImYW1wO0o8L2Rpdj4KICA8ZGl2IGNsYXNzPSJsb2dvLXN1YiIgZGF0YS1pMThuPSJsb2dvX3N1YiI+R3Jvb21pbmcgwrcg0KLQsNC70LvQuNC9PC9kaXY+CiAgPGRpdiBjbGFzcz0icHJvZ3Jlc3MiPgogICAgPGRpdiBjbGFzcz0icHMgYWN0aXZlIiBpZD0icHMxIj48ZGl2IGNsYXNzPSJwZG90Ij48L2Rpdj48c3BhbiBkYXRhLWkxOG49InBzX3NlcnZpY2UiPtCj0YHQu9GD0LPQsDwvc3Bhbj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBsIiBpZD0icGwxIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBzIiBpZD0icHMyIj48ZGl2IGNsYXNzPSJwZG90Ij48L2Rpdj48c3BhbiBkYXRhLWkxOG49InBzX21hc3RlciI+0JzQsNGB0YLQtdGAPC9zcGFuPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icGwiIGlkPSJwbDIiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icHMiIGlkPSJwczMiPjxkaXYgY2xhc3M9InBkb3QiPjwvZGl2PjxzcGFuIGRhdGEtaTE4bj0icHNfcGV0Ij7Qn9C40YLQvtC80LXRhjwvc3Bhbj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBsIiBpZD0icGwzIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBzIiBpZD0icHM0Ij48ZGl2IGNsYXNzPSJwZG90Ij48L2Rpdj48c3BhbiBkYXRhLWkxOG49InBzX2RhdGUiPtCU0LDRgtCwPC9zcGFuPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icGwiIGlkPSJwbDQiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icHMiIGlkPSJwczUiPjxkaXYgY2xhc3M9InBkb3QiPjwvZGl2PjxzcGFuIGRhdGEtaTE4bj0icHNfZGV0YWlscyI+0JTQsNC90L3Ri9C1PC9zcGFuPjwvZGl2PgogIDwvZGl2PgoKICA8IS0tIFN0ZXAgMSAtLT4KICA8ZGl2IGNsYXNzPSJzdGVwIHNob3ciIGlkPSJiazEiPgogICAgPGRpdiBjbGFzcz0ic2xibCIgZGF0YS1pMThuPSJzdGVwMV9sYmwiPjAxIMK3INCf0L7RgNC+0LTQsDwvZGl2PgogICAgPGRpdiBjbGFzcz0iYndyYXAiPgogICAgICA8ZGl2IGNsYXNzPSJzYm94Ij4KICAgICAgICA8c3BhbiBjbGFzcz0ic2kiPvCflI08L3NwYW4+CiAgICAgICAgPGlucHV0IGlkPSJiSW5wdXQiIHR5cGU9InRleHQiIHBsYWNlaG9sZGVyPSLQndCw0YfQvdC40YLQtSDQstCy0L7QtNC40YLRjCDQv9C+0YDQvtC00YMuLi4iIGRhdGEtaTE4bi1waD0iYnJlZWRfcGgiIGF1dG9jb21wbGV0ZT0ib2ZmIj4KICAgICAgICA8YnV0dG9uIGNsYXNzPSJjbHIiIGlkPSJjbHJCdG4iPuKclTwvYnV0dG9uPgogICAgICA8L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0iZHJvcCIgaWQ9ImJEcm9wIj48L2Rpdj4KICAgIDwvZGl2PgogICAgPGRpdiBjbGFzcz0ic2JhZGdlIiBpZD0ic0JhZGdlIj48L2Rpdj4KICAgIDxkaXYgaWQ9InN2Y1NlYyIgc3R5bGU9ImRpc3BsYXk6bm9uZTttYXJnaW4tdG9wOjE2cHgiPgogICAgICA8ZGl2IGNsYXNzPSJzbGJsIiBkYXRhLWkxOG49InN0ZXAyX2xibCI+MDIgwrcg0KPRgdC70YPQs9CwPC9kaXY+CiAgICAgIDxkaXYgaWQ9InN2Y0xpc3QiPjwvZGl2PgogICAgPC9kaXY+CiAgPC9kaXY+CgogIDwhLS0gU3RlcCAyIC0tPgogIDxkaXYgY2xhc3M9InN0ZXAiIGlkPSJiazIiPgogICAgPGRpdiBjbGFzcz0ic2xibCIgZGF0YS1pMThuPSJzdGVwMl9tYXN0ZXIiPtCS0YvQsdC10YDQuNGC0LUg0LzQsNGB0YLQtdGA0LA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im1hc3RlcnMiPgogICAgICA8ZGl2IGNsYXNzPSJtYnRuIiBkYXRhLW1hc3Rlcj0i0KLQsNGC0YzRj9C90LAiPjxkaXYgY2xhc3M9Im1uYW1lIj7QotCw0YLRjNGP0L3QsDwvZGl2PjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJtYnRuIiBkYXRhLW1hc3Rlcj0i0JDQu9C40YHQsCI+PGRpdiBjbGFzcz0ibW5hbWUiPtCQ0LvQuNGB0LA8L2Rpdj48L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0ibWJ0biIgZGF0YS1tYXN0ZXI9ItCa0YDQuNGB0YLQuNC90LAiPjxkaXYgY2xhc3M9Im1uYW1lIj7QmtGA0LjRgdGC0LjQvdCwPC9kaXY+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9Im1idG4iIGRhdGEtbWFzdGVyPSLQkNC90L3QsCI+PGRpdiBjbGFzcz0ibW5hbWUiPtCQ0L3QvdCwPC9kaXY+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9Im1idG4iIGRhdGEtbWFzdGVyPSLQkNC70LXQutGB0LDQvdC00YDQsCI+PGRpdiBjbGFzcz0ibW5hbWUiPtCQ0LvQtdC60YHQsNC90LTRgNCwPC9kaXY+PC9kaXY+CiAgICA8L2Rpdj4KICA8L2Rpdj4KCiAgPCEtLSBTdGVwIDMgLS0+CiAgPGRpdiBjbGFzcz0ic3RlcCIgaWQ9ImJrMyI+CiAgICA8ZGl2IGNsYXNzPSJzbGJsIiBkYXRhLWkxOG49InN0ZXAzX2xibCI+0JrQsNC6INC00LDQstC90L4g0LLRiyDQv9C+0YHQtdGJ0LDQu9C4INCz0YDRg9C80LjQvdCzPzwvZGl2PgogICAgPGJ1dHRvbiBjbGFzcz0iZ2J0biIgZGF0YS12YWw9ItCf0LXRgNCy0YvQuSDRgNCw0LciIGRhdGEtaTE4bj0iZzEiPtCf0LXRgNCy0YvQuSDRgNCw0Lc8L2J1dHRvbj4KICAgIDxidXR0b24gY2xhc3M9ImdidG4iIGRhdGEtdmFsPSLQntGCIDEg0LTQviAzINC80LXRgdGP0YbQtdCyIiBkYXRhLWkxOG49ImcyIj7QntGCIDEg0LTQviAzINC80LXRgdGP0YbQtdCyPC9idXR0b24+CiAgICA8YnV0dG9uIGNsYXNzPSJnYnRuIiBkYXRhLXZhbD0i0J7RgiAzINC00L4gNiDQvNC10YHRj9GG0LXQsiIgZGF0YS1pMThuPSJnMyI+0J7RgiAzINC00L4gNiDQvNC10YHRj9GG0LXQsjwvYnV0dG9uPgogICAgPGJ1dHRvbiBjbGFzcz0iZ2J0biIgZGF0YS12YWw9ItCR0L7Qu9C10LUgNiDQvNC10YHRj9GG0LXQsiIgZGF0YS1pMThuPSJnNCI+0JHQvtC70LXQtSA2INC80LXRgdGP0YbQtdCyPC9idXR0b24+CiAgPC9kaXY+CgogIDwhLS0gU3RlcCA0IC0tPgogIDxkaXYgY2xhc3M9InN0ZXAiIGlkPSJiazQiPgogICAgPGRpdiBjbGFzcz0ic2xibCIgZGF0YS1pMThuPSJzdGVwNF9sYmwiPtCS0YvQsdC10YDQuNGC0LUg0LTQsNGC0YM8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImNhbC1oIj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iY2FsLW4iIGlkPSJwcmV2TSI+JiM4MjQ5OzwvYnV0dG9uPgogICAgICA8ZGl2IGNsYXNzPSJjYWwtbSIgaWQ9ImNhbE0iPjwvZGl2PgogICAgICA8YnV0dG9uIGNsYXNzPSJjYWwtbiIgaWQ9Im5leHRNIj4mIzgyNTA7PC9idXR0b24+CiAgICA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImNnIiBpZD0iY2FsRyI+PC9kaXY+CiAgICA8ZGl2IHN0eWxlPSJkaXNwbGF5OmZsZXg7Z2FwOjIwcHg7YWxpZ24taXRlbXM6Y2VudGVyO21hcmdpbi10b3A6MTJweDtwYWRkaW5nLXRvcDoxMnB4O2JvcmRlci10b3A6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjEpO2ZsZXgtd3JhcDp3cmFwOyI+PGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OHB4OyI+PGRpdiBzdHlsZT0id2lkdGg6MTZweDtoZWlnaHQ6MTZweDtib3JkZXItcmFkaXVzOjUwJTtiYWNrZ3JvdW5kOnJnYmEoOTAsMTgwLDkwLC4xNSk7Ym9yZGVyOjFweCBzb2xpZCAjNWFiNDVhO2ZsZXgtc2hyaW5rOjA7Ij48L2Rpdj48c3BhbiBzdHlsZT0iZm9udC1zaXplOjAuOHJlbTtjb2xvcjojZmZmZmZmO2xldHRlci1zcGFjaW5nOi4wM2VtOyIgZGF0YS1pMThuPSJjYWxfYXZhaWwiPtCV0YHRgtGMINGB0LLQvtCx0L7QtNC90L7QtSDQstGA0LXQvNGPPC9zcGFuPjwvZGl2PjxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjhweDsiPjxkaXYgc3R5bGU9IndpZHRoOjE2cHg7aGVpZ2h0OjE2cHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wNCk7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xKTtmbGV4LXNocmluazowOyI+PC9kaXY+PHNwYW4gc3R5bGU9ImZvbnQtc2l6ZTowLjhyZW07Y29sb3I6I2ZmZmZmZjtsZXR0ZXItc3BhY2luZzouMDNlbTsiIGRhdGEtaTE4bj0iY2FsX25vbmUiPtCh0LLQvtCx0L7QtNC90L7Qs9C+INCy0YDQtdC80LXQvdC4INC90LXRgjwvc3Bhbj48L2Rpdj48L2Rpdj4KICAgIDxkaXYgaWQ9InRpbWVTZWMiIHN0eWxlPSJkaXNwbGF5Om5vbmU7bWFyZ2luLXRvcDoxNnB4Ij4KICAgICAgPGRpdiBjbGFzcz0ic2xibCIgZGF0YS1pMThuPSJzdGVwNF90aW1lIj7QktGL0LHQtdGA0LjRgtC1INCy0YDQtdC80Y88L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0idGciIGlkPSJ0aW1lRyI+PC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgc3R5bGU9Im1hcmdpbi10b3A6MjBweDtwYWRkaW5nLXRvcDoxNnB4O2JvcmRlci10b3A6MXB4IHNvbGlkIHJnYmEoMjU1LDI1NSwyNTUsLjA4KTt0ZXh0LWFsaWduOmNlbnRlciI+CiAgICAgIDxidXR0b24gaWQ9ImNhbGxiYWNrQnRuIiBjbGFzcz0iY2JrLWJ0biI+0J3QtSDQvdCw0YjQu9C4INGD0LTQvtCx0L3QvtC1INCy0YDQtdC80Y8/IOKGkjwvYnV0dG9uPgogICAgPC9kaXY+CiAgPC9kaXY+CgogIDwhLS0gU3RlcCA1IC0tPgogIDxkaXYgY2xhc3M9InN0ZXAiIGlkPSJiazUiPgogICAgPGRpdiBjbGFzcz0ic2xibCIgZGF0YS1pMThuPSJzdGVwNV9sYmwiPtCS0LDRiNC4INC00LDQvdC90YvQtTwvZGl2PgogICAgPGRpdiBjbGFzcz0iZmciPjxsYWJlbCBjbGFzcz0iZmwiIGRhdGEtaTE4bj0ibGJsX25hbWUiPtCY0LzRjzwvbGFiZWw+PGlucHV0IGNsYXNzPSJmaSIgaWQ9ImNOYW1lIiB0eXBlPSJ0ZXh0IiBwbGFjZWhvbGRlcj0i0JLQsNGI0LUg0LjQvNGPIiBkYXRhLWkxOG4tcGg9InBoX25hbWUiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0iZmciPjxsYWJlbCBjbGFzcz0iZmwiIGRhdGEtaTE4bj0ibGJsX3Bob25lIj7QotC10LvQtdGE0L7QvTwvbGFiZWw+PGlucHV0IGNsYXNzPSJmaSIgaWQ9ImNQaG9uZSIgdHlwZT0idGVsIiBwbGFjZWhvbGRlcj0iKzM3MiAuLi4iPjwvZGl2PgogICAgPGRpdiBjbGFzcz0iZmciPjxsYWJlbCBjbGFzcz0iZmwiIGRhdGEtaTE4bj0ibGJsX2VtYWlsIj5FbWFpbDwvbGFiZWw+PGlucHV0IGNsYXNzPSJmaSIgaWQ9ImNFbWFpbCIgdHlwZT0iZW1haWwiIHBsYWNlaG9sZGVyPSJlbWFpbEBleGFtcGxlLmNvbSI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJmZyI+PGxhYmVsIGNsYXNzPSJmbCIgZGF0YS1pMThuPSJsYmxfcGV0Ij7QmtC70LjRh9C60LAg0L/QuNGC0L7QvNGG0LA8L2xhYmVsPjxpbnB1dCBjbGFzcz0iZmkiIGlkPSJjUGV0IiB0eXBlPSJ0ZXh0IiBwbGFjZWhvbGRlcj0i0J3QtdC+0LHRj9C30LDRgtC10LvRjNC90L4iIGRhdGEtaTE4bi1waD0icGhfb3B0aW9uYWwiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0ic3VtIiBpZD0ic3VtQmxvY2siPjwvZGl2PgogICAgPGJ1dHRvbiBjbGFzcz0iY2J0biIgaWQ9ImNvbmZpcm1CdG4iIGRhdGEtaTE4bj0iY29uZmlybV9idG4iPtCf0L7QtNGC0LLQtdGA0LTQuNGC0Ywg0LfQsNC/0LjRgdGMPC9idXR0b24+CiAgPC9kaXY+CgogIDwhLS0gU3VjY2VzcyAtLT4KICA8ZGl2IGNsYXNzPSJzYmxvY2siIGlkPSJzdWNCbG9jayI+CiAgICA8ZGl2IGNsYXNzPSJzaTIiPvCfkL48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InN0IiBkYXRhLWkxOG49InN1Y2Nlc3NfdGl0bGUiPtCX0LDQv9C40YHRjCDQv9GA0LjQvdGP0YLQsCE8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InNzIiBkYXRhLWkxOG49InN1Y2Nlc3Nfc3ViIj7QnNGLINGB0LLRj9C20LXQvNGB0Y8g0YEg0LLQsNC80Lgg0LTQu9GPINC/0L7QtNGC0LLQtdGA0LbQtNC10L3QuNGPLjxicj7QodC/0LDRgdC40LHQviwg0YfRgtC+INCy0YvQsdGA0LDQu9C4IFImSiBHcm9vbWluZyE8L2Rpdj4KICAgIDxidXR0b24gY2xhc3M9ImhidG4iIGlkPSJob21lQnRuIiBkYXRhLWkxOG49InRvX2hvbWUiPuKGkCDQndCwINCz0LvQsNCy0L3Rg9GOPC9idXR0b24+CiAgPC9kaXY+CjwvZGl2Pgo8L2Rpdj4KCjxkaXYgaWQ9ImNia01vZGFsIiBzdHlsZT0iZGlzcGxheTpub25lO3Bvc2l0aW9uOmZpeGVkO2luc2V0OjA7YmFja2dyb3VuZDpyZ2JhKDAsMCwwLC43NSk7ei1pbmRleDozMDA7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7cGFkZGluZzoyMHB4Ij4KICA8ZGl2IHN0eWxlPSJiYWNrZ3JvdW5kOiMwYTBhMGE7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xMik7Ym9yZGVyLXRvcDoxcHggc29saWQgI2ZmZmZmZjtwYWRkaW5nOjI4cHggMjRweDt3aWR0aDoxMDAlO21heC13aWR0aDozNjBweCI+CiAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6MC42N3JlbTtsZXR0ZXItc3BhY2luZzouMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojZmZmZmZmO21hcmdpbi1ib3R0b206MTZweDtmb250LXdlaWdodDo2MDA7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWYiPtCe0LHRgNCw0YLQvdGL0Lkg0LfQstC+0L3QvtC6PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJmZyI+PGxhYmVsIGNsYXNzPSJmbCI+0JjQvNGPPC9sYWJlbD48aW5wdXQgY2xhc3M9ImZpIiBpZD0iY2JrTmFtZSIgdHlwZT0idGV4dCIgcGxhY2Vob2xkZXI9ItCS0LDRiNC1INC40LzRjyI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJmZyI+CiAgICAgIDxsYWJlbCBjbGFzcz0iZmwiPtCi0LXQu9C10YTQvtC9PC9sYWJlbD4KICAgICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOnN0cmV0Y2g7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMTUpIj4KICAgICAgICA8c3BhbiBzdHlsZT0icGFkZGluZzoxMHB4IDEwcHggMTBweCAwO2NvbG9yOiNmZmZmZmY7Zm9udC1zaXplOjEuMDlyZW07Ym9yZGVyLXJpZ2h0OjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4xKTttYXJnaW4tcmlnaHQ6MTBweDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2ZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZiI+KzM3Mjwvc3Bhbj4KICAgICAgICA8aW5wdXQgaWQ9ImNia1Bob25lIiB0eXBlPSJ0ZWwiIHBsYWNlaG9sZGVyPSJYWFhYWFhYWCIgc3R5bGU9ImZsZXg6MTtiYWNrZ3JvdW5kOnRyYW5zcGFyZW50O2JvcmRlcjpub25lO291dGxpbmU6bm9uZTtmb250LWZhbWlseTonUGxheWZhaXIgRGlzcGxheScsc2VyaWY7Zm9udC1zaXplOjEuMTVyZW07Y29sb3I6I2ZmZmZmZjtwYWRkaW5nOjEwcHggMCI+CiAgICAgIDwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IGlkPSJjYmtTdWNjZXNzIiBzdHlsZT0iZGlzcGxheTpub25lO3RleHQtYWxpZ246Y2VudGVyO3BhZGRpbmc6MjBweCAwIj4KICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjIuM3JlbTttYXJnaW4tYm90dG9tOjEwcHg7b3BhY2l0eTouNSI+4pyTPC9kaXY+CiAgICAgIDxkaXYgc3R5bGU9ImZvbnQtZmFtaWx5OidQbGF5ZmFpciBEaXNwbGF5JyxzZXJpZjtmb250LXNpemU6MS41cmVtO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbTo2cHgiPtCX0LDRj9Cy0LrQsCDQv9GA0LjQvdGP0YLQsCE8L2Rpdj4KICAgICAgPGRpdiBzdHlsZT0iZm9udC1zaXplOjAuODNyZW07Y29sb3I6I2ZmZmZmZjtsaW5lLWhlaWdodDoxLjY7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWYiPtCc0Ysg0L/QtdGA0LXQt9Cy0L7QvdC40Lwg0LLQsNC8INCyINCx0LvQuNC20LDQudGI0LXQtSDQstGA0LXQvNGPPC9kaXY+CiAgICA8L2Rpdj4KICAgIDxidXR0b24gaWQ9ImNia1N1Ym1pdCIgY2xhc3M9ImNidG4iIHN0eWxlPSJtYXJnaW4tdG9wOjE0cHgiPtCe0YLQv9GA0LDQstC40YLRjDwvYnV0dG9uPgogICAgPGJ1dHRvbiBpZD0iY2JrQ2xvc2UiIHN0eWxlPSJkaXNwbGF5OmJsb2NrO3dpZHRoOjEwMCU7bWFyZ2luLXRvcDo4cHg7YmFja2dyb3VuZDpub25lO2JvcmRlcjpub25lO2NvbG9yOiNmZmZmZmY7Zm9udC1zaXplOjAuNjdyZW07bGV0dGVyLXNwYWNpbmc6LjEyZW07Y3Vyc29yOnBvaW50ZXI7cGFkZGluZzo4cHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWYiPtCe0YLQvNC10L3QsDwvYnV0dG9uPgogIDwvZGl2Pgo8L2Rpdj4KCjxzY3JpcHQ+CnZhciBEQVRBID0gW3siYnJlZWQiOiLQkNCy0YHRgtGA0LDQu9C40LnRgdC60LDRjyDQvtCy0YfQsNGA0LrQsCAxNeKAkzI1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4NX0sImJyZWVkX2VuIjoiQXVzdHJhbGlhbiBTaGVwaGVyZCAxNeKAkzI1IGtnIiwiYnJlZWRfZXQiOiJBdXN0cmFhbGlhIGxhbWJha29lciAxNeKAkzI1IGtnIn0seyJicmVlZCI6ItCQ0LLRgdGC0YDQsNC70LjQudGB0LrQsNGPINC+0LLRh9Cw0YDQutCwIDI14oCTMzUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiQXVzdHJhbGlhbiBTaGVwaGVyZCAyNeKAkzM1IGtnIiwiYnJlZWRfZXQiOiJBdXN0cmFhbGlhIGxhbWJha29lciAyNeKAkzM1IGtnIn0seyJicmVlZCI6ItCQ0LrQuNGC0LAt0LjQvdGDIDIw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkFraXRhIEludSAyMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBa2l0YSBJbnUgMjDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC60LjRgtCwLdC40L3RgyDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQWtpdGEgSW51IG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkFraXRhIEludSDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCQ0LrQuNGC0LAt0LjQvdGDINGE0LvQsNGE0YTQuCAyMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJBa2l0YSBJbnUgZmx1ZmZ5IDIw4oCTNDAga2ciLCJicmVlZF9ldCI6IkFraXRhIEludSBwZWhtZWthcnZhbGluZSAyMOKAkzQwIGtnIn0seyJicmVlZCI6ItCQ0LrQuNGC0LAt0LjQvdGDINGE0LvQsNGE0YTQuCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQWtpdGEgSW51IGZsdWZmeSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJBa2l0YSBJbnUgcGVobWVrYXJ2YWxpbmUgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQkNC70LDQsdCw0LkgNDDigJM2MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkNlbnRyYWwgQXNpYW4gU2hlcGhlcmQgNDDigJM2MCBrZyIsImJyZWVkX2V0IjoiS2Vzay1BYXNpYSBsYW1iYWtvZXIgNDDigJM2MCBrZyJ9LHsiYnJlZWQiOiLQkNC70LDQsdCw0Lkg0LHQvtC70LXQtSA2MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjEwMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjExNSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEzMH0sImJyZWVkX2VuIjoiQ2VudHJhbCBBc2lhbiBTaGVwaGVyZCBvdmVyIDYwIGtnIiwiYnJlZWRfZXQiOiJLZXNrLUFhc2lhIGxhbWJha29lciDDvGxlIDYwIGtnIn0seyJicmVlZCI6ItCQ0LvRj9GB0LrQuNC90YHQutC40Lkg0LzQsNC70LDQvNGD0YIgMjDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQWxhc2thbiBNYWxhbXV0ZSAyMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBbGFza2EgbWFsYW11dXQgMjDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC70Y/RgdC60LjQvdGB0LrQuNC5INC80LDQu9Cw0LzRg9GCINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJBbGFza2FuIE1hbGFtdXRlIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkFsYXNrYSBtYWxhbXV1dCDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCQ0LvRj9GB0LrQuNC90YHQutC40Lkg0LzQsNC70LDQvNGD0YIg0YTQu9Cw0YTRhNC4IDIw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkFsYXNrYW4gTWFsYW11dGUgZmx1ZmZ5IDIw4oCTNDAga2ciLCJicmVlZF9ldCI6IkFsYXNrYSBtYWxhbXV1dCBwZWhtZWthcnZhbGluZSAyMOKAkzQwIGtnIn0seyJicmVlZCI6ItCQ0LvRj9GB0LrQuNC90YHQutC40Lkg0LzQsNC70LDQvNGD0YIg0YTQu9Cw0YTRhNC4INCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJBbGFza2FuIE1hbGFtdXRlIGZsdWZmeSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJBbGFza2EgbWFsYW11dXQgcGVobWVrYXJ2YWxpbmUgw7xsZSA0MCBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutCw0Y8g0LDQutC40YLQsCAyMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBBa2l0YSAyMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBBa2l0YSAyMOKAkzQwIGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LDRjyDQsNC60LjRgtCwINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBBa2l0YSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBBa2l0YSDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LDRjyDQsNC60LjRgtCwINGE0LvQsNGE0YTQuCAyMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBBa2l0YSBmbHVmZnkgMjDigJM0MCBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2EgQWtpdGEgcGVobWVrYXJ2YWxpbmUgMjDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutCw0Y8g0LDQutC40YLQsCDRhNC70LDRhNGE0Lgg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMTV9LCJicmVlZF9lbiI6IkFtZXJpY2FuIEFraXRhIGZsdWZmeSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBBa2l0YSBwZWhtZWthcnZhbGluZSDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LjQuSDQutC+0LrQtdGALdGB0L/QsNC90LjQtdC70YwgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBDb2NrZXIgU3BhbmllbCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBrb2tlcnNwYW5qZWwgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQkNC80LXRgNC40LrQsNC90YHQutC40Lkg0LrQvtC60LXRgC3RgdC/0LDQvdC40LXQu9GMIDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4MH0sImJyZWVkX2VuIjoiQW1lcmljYW4gQ29ja2VyIFNwYW5pZWwgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2Ega29rZXJzcGFuamVsIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0JDQvNC10YDQuNC60LDQvdGB0LrQuNC5INGB0YLQsNGE0YTQvtGA0LTRiNC40YDRgdC60LjQuSDRgtC10YDRjNC10YAgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkFtZXJpY2FuIFN0YWZmb3Jkc2hpcmUgVGVycmllciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJBbWVlcmlrYSBTdGFmZm9yZHNoaXJlIHRlcmplciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCQ0LzQtdGA0LjQutCw0L3RgdC60LjQuSDRgdGC0LDRhNGE0L7RgNC00YjQuNGA0YHQutC40Lkg0YLQtdGA0YzQtdGAIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1fSwiYnJlZWRfZW4iOiJBbWVyaWNhbiBTdGFmZm9yZHNoaXJlIFRlcnJpZXIgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiQW1lZXJpa2EgU3RhZmZvcmRzaGlyZSB0ZXJqZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQkNC90LPQu9C40LnRgdC60LjQuSDQsdGD0LvRjNC00L7QsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NzB9LCJicmVlZF9lbiI6IkVuZ2xpc2ggQnVsbGRvZyIsImJyZWVkX2V0IjoiSW5nbGlzZSBidWxkb2cifSx7ImJyZWVkIjoi0JDQvdCz0LvQuNC50YHQutC40Lkg0LrQvtC60LXRgC3RgdC/0LDQvdC40LXQu9GMIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MH0sImJyZWVkX2VuIjoiRW5nbGlzaCBDb2NrZXIgU3BhbmllbCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJJbmdsaXNlIGtva2Vyc3BhbmplbCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCQ0L3Qs9C70LjQudGB0LrQuNC5INC60L7QutC10YAt0YHQv9Cw0L3QuNC10LvRjCAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODB9LCJicmVlZF9lbiI6IkVuZ2xpc2ggQ29ja2VyIFNwYW5pZWwgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiSW5nbGlzZSBrb2tlcnNwYW5qZWwgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQkNGE0LPQsNC9IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5MH0sImJyZWVkX2VuIjoiQWZnaGFuIEhvdW5kIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkFmZ2FuaXN0YW5pIGtvZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQkNGE0LPQsNC9IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjgwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDB9LCJicmVlZF9lbiI6IkFmZ2hhbiBIb3VuZCAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJBZmdhbmlzdGFuaSBrb2VyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JHQsNGB0YHQtdGCLdGF0LDRg9C90LQgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkJhc3NldCBIb3VuZCAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJCYXNzZXRob3VuZCAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCR0LDRgdGB0LXRgi3RhdCw0YPQvdC0IDMw4oCTMzUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1fSwiYnJlZWRfZW4iOiJCYXNzZXQgSG91bmQgMzDigJMzNSBrZyIsImJyZWVkX2V0IjoiQmFzc2V0aG91bmQgMzDigJMzNSBrZyJ9LHsiYnJlZWQiOiLQkdC10YDQvdGB0LrQuNC5INC30LXQvdC90LXQvdGF0YPQvdC0IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IkJlcm5lc2UgTW91bnRhaW4gRG9nIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkJlcm5pIG3DpGdpa29lciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCR0LXRgNC90YHQutC40Lkg0LfQtdC90L3QtdC90YXRg9C90LQg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEzMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiQmVybmVzZSBNb3VudGFpbiBEb2cgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiQmVybmkgbcOkZ2lrb2VyIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JHQuNCy0LXRgC3QudC+0YDQuiDQsdC+0LvQtdC1IDMsNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJCaWV3ZXIgWW9ya3NoaXJlIFRlcnJpZXIgb3ZlciAzLDUga2ciLCJicmVlZF9ldCI6IkJpZXdlciBZb3Jrc2hpcmUgVGVycmllciDDvGxlIDMsNSBrZyJ9LHsiYnJlZWQiOiLQkdC40LLQtdGALdC50L7RgNC6INC00L4gMyw1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkJpZXdlciBZb3Jrc2hpcmUgVGVycmllciB1cCB0byAzLDUga2ciLCJicmVlZF9ldCI6IkJpZXdlciBZb3Jrc2hpcmUgVGVycmllciBrdW5pIDMsNSBrZyJ9LHsiYnJlZWQiOiLQkdC40LPQu9GMIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjB9LCJicmVlZF9lbiI6IkJlYWdsZSAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJCaWlnZWwgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQkdC40LPQu9GMIDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjV9LCJicmVlZF9lbiI6IkJlYWdsZSAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiJCaWlnZWwgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQkdC40YjQvtC9LdGE0YDQuNC30LUgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkJpY2hvbiBGcmlzw6kgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJCacWhb24gRnJpc8OpIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQkdC40YjQvtC9LdGE0YDQuNC30LUg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkJpY2hvbiBGcmlzw6kgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiQmnFoW9uIEZyaXPDqSBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JHQvtC60YHQtdGAIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwfSwiYnJlZWRfZW4iOiJCb3hlciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJCb2tzZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQkdC+0LrRgdC10YAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkJveGVyIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkJva3NlciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCR0L7RgNC00LXRgC3QutC+0LvQu9C4IDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjgwfSwiYnJlZWRfZW4iOiJCb3JkZXIgQ29sbGllIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IkJvcmRlcmtvbGwgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQkdC+0YDQtNC10YAt0LrQvtC70LvQuCAyMOKAkzI1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkJvcmRlciBDb2xsaWUgMjDigJMyNSBrZyIsImJyZWVkX2V0IjoiQm9yZGVya29sbCAyMOKAkzI1IGtnIn0seyJicmVlZCI6ItCR0L7RgdGC0L7QvS3RgtC10YDRjNC10YAgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDV9LCJicmVlZF9lbiI6IkJvc3RvbiBUZXJyaWVyIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IkJvc3RvbmkgdGVyamVyIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0JHQvtGB0YLQvtC9LdGC0LXRgNGM0LXRgCA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwfSwiYnJlZWRfZW4iOiJCb3N0b24gVGVycmllciA14oCTMTAga2ciLCJicmVlZF9ldCI6IkJvc3RvbmkgdGVyamVyIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQkdGA0LDQsdCw0L3RgdC+0L0iLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjUwfSwiYnJlZWRfZW4iOiJHcmlmZm9uIEJydXhlbGxvaXMiLCJicmVlZF9ldCI6IkJyw7xzc2VsaSBncmlmb24ifSx7ImJyZWVkIjoi0JHRg9C70YzRgtC10YDRjNC10YAgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTB9LCJicmVlZF9lbiI6IkJ1bGwgVGVycmllciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJCdWxsdGVyamVyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JLQtdC70YzRiC3QutC+0YDQs9C4IDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjV9LCJicmVlZF9lbiI6IldlbHNoIENvcmdpIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IldhbGVzaSBrb3JnaSAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCS0LXQu9GM0Ygt0LrQvtGA0LPQuCAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjc1fSwiYnJlZWRfZW4iOiJXZWxzaCBDb3JnaSAxNeKAkzIwIGtnIiwiYnJlZWRfZXQiOiJXYWxlc2kga29yZ2kgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQktC10YHRgi3RhdCw0LnQu9C10L3QtC3QstCw0LnRgi3RgtC10YDRjNC10YAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJXZXN0IEhpZ2hsYW5kIFdoaXRlIFRlcnJpZXIiLCJicmVlZF9ldCI6IkzDpMOkbmUtxaBvdGltYWEgdmFsZ2UgdGVyamVyIn0seyJicmVlZCI6ItCS0L7RgdGC0L7Rh9C90L7RgdC40LHQuNGA0YHQutCw0Y8g0LvQsNC50LrQsCAxOOKAkzI1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjg1fSwiYnJlZWRfZW4iOiJFYXN0IFNpYmVyaWFuIExhaWthIDE44oCTMjUga2ciLCJicmVlZF9ldCI6IklkYS1TaWJlcmkgbGFpa2EgMTjigJMyNSBrZyJ9LHsiYnJlZWQiOiLQktC+0YHRgtC+0YfQvdC+0YHQuNCx0LjRgNGB0LrQsNGPINC70LDQudC60LAg0LHQvtC70LXQtSAyNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjU1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IkVhc3QgU2liZXJpYW4gTGFpa2Egb3ZlciAyNSBrZyIsImJyZWVkX2V0IjoiSWRhLVNpYmVyaSBsYWlrYSDDvGxlIDI1IGtnIn0seyJicmVlZCI6ItCT0L7Qu9C00LXQvS3RgNC10YLRgNC40LLQtdGAIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiR29sZGVuIFJldHJpZXZlciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJLdWxkbmUgcmV0cmlpdmVyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JPQvtC70LTQtdC9LdGA0LXRgtGA0LjQstC10YAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjExMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiR29sZGVuIFJldHJpZXZlciAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJLdWxkbmUgcmV0cmlpdmVyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JPRgNC40YTRhNC+0L0iLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJHcmlmZm9uIiwiYnJlZWRfZXQiOiJHcmlmb24ifSx7ImJyZWVkIjoi0JTQsNC70LzQsNGC0LjQvSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NzB9LCJicmVlZF9lbiI6IkRhbG1hdGlhbiIsImJyZWVkX2V0IjoiRGFsbWFhdHNpYSBrb2VyIn0seyJicmVlZCI6ItCU0LbQtdC6LdGA0LDRgdGB0LXQuy3RgtC10YDRjNC10YAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NTB9LCJicmVlZF9lbiI6IkphY2sgUnVzc2VsbCBUZXJyaWVyIHNtb290aCIsImJyZWVkX2V0IjoiSmFjayBSdXNzZWxsaSB0ZXJqZXIgbMO8aGlrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0JTQttC10Lot0YDQsNGB0YHQtdC7LdGC0LXRgNGM0LXRgCDQttC10YHRgtC60L7RiNC10YDRgdGC0L3Ri9C5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiSmFjayBSdXNzZWxsIFRlcnJpZXIgd2lyZS1oYWlyZWQiLCJicmVlZF9ldCI6IkphY2sgUnVzc2VsbGkgdGVyamVyIGthcnVrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0JTQvtCx0LXRgNC80LDQvSAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjgwfSwiYnJlZWRfZW4iOiJEb2Jlcm1hbm4gMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiRG9iZXJtYW5uIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JTQvtCx0LXRgNC80LDQvSDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjk1fSwiYnJlZWRfZW4iOiJEb2Jlcm1hbm4gb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiRG9iZXJtYW5uIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JfQsNC/0LDQtNC90L7RgdC40LHQuNGA0YHQutCw0Y8g0LvQsNC50LrQsCAxOOKAkzI1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjg1fSwiYnJlZWRfZW4iOiJXZXN0IFNpYmVyaWFuIExhaWthIDE44oCTMjUga2ciLCJicmVlZF9ldCI6IkzDpMOkbmUtU2liZXJpIGxhaWthIDE44oCTMjUga2cifSx7ImJyZWVkIjoi0JfQvtC70L7RgtC40YHRgtGL0Lkg0YDQtdGC0YDQuNCy0LXRgCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiR29sZGVuIFJldHJpZXZlciAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJLdWxkbmUgcmV0cmlpdmVyIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JfQvtC70L7RgtC40YHRgtGL0Lkg0YDQtdGC0YDQuNCy0LXRgCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTEwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTEwfSwiYnJlZWRfZW4iOiJHb2xkZW4gUmV0cmlldmVyIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6Ikt1bGRuZSByZXRyaWl2ZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQmNGA0LvQsNC90LTRgdC60LjQuSDQvNGP0LPQutC+0YjQtdGA0YHRgtC90YvQuSDQv9GI0LXQvdC40YfQvdGL0Lkg0YLQtdGA0YzQtdGAIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwfSwiYnJlZWRfZW4iOiJJcmlzaCBTb2Z0IENvYXRlZCBXaGVhdGVuIFRlcnJpZXIiLCJicmVlZF9ldCI6IklpcmkgcGVobWVrYXJ2YW5lIG5pc3V2w6RydmkgdGVyamVyIn0seyJicmVlZCI6ItCY0YDQu9Cw0L3QtNGB0LrQuNC5INGC0LXRgNGM0LXRgCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MCwi0KLRgNC40LzQvNC40L3QsyI6NzV9LCJicmVlZF9lbiI6IklyaXNoIFRlcnJpZXIiLCJicmVlZF9ldCI6IklpcmkgdGVyamVyIn0seyJicmVlZCI6ItCY0YHQv9Cw0L3RgdC60LjQuSDQs9Cw0LvRjNCz0L4gMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo3MH0sImJyZWVkX2VuIjoiU3BhbmlzaCBHYWxnbyAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJIaXNwYWFuaWEgZ2FsZ28gMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQmNGB0L/QsNC90YHQutC40Lkg0LPQsNC70YzQs9C+IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6ODB9LCJicmVlZF9lbiI6IlNwYW5pc2ggR2FsZ28gMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiSGlzcGFhbmlhIGdhbGdvIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JnQvtGA0LrRiNC40YDRgdC60LjQuSDRgtC10YDRjNC10YAg0LHQvtC70LXQtSAzLDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiWW9ya3NoaXJlIFRlcnJpZXIgb3ZlciAzLDUga2ciLCJicmVlZF9ldCI6IllvcmtzaGlyZSB0ZXJqZXIgw7xsZSAzLDUga2cifSx7ImJyZWVkIjoi0JnQvtGA0LrRiNC40YDRgdC60LjQuSDRgtC10YDRjNC10YAg0LTQviAzLDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiWW9ya3NoaXJlIFRlcnJpZXIgdXAgdG8gMyw1IGtnIiwiYnJlZWRfZXQiOiJZb3Jrc2hpcmUgdGVyamVyIGt1bmkgMyw1IGtnIn0seyJicmVlZCI6ItCa0LDQstCw0LvQtdGALdC60LjQvdCzLdGH0LDRgNC70YzQty3RgdC/0LDQvdC40LXQu9GMIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MH0sImJyZWVkX2VuIjoiQ2F2YWxpZXIgS2luZyBDaGFybGVzIFNwYW5pZWwgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiQ2F2YWxpZXIgS2luZyBDaGFybGVzIFNwYW5pZWwgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQmtCw0LLQsNC70LXRgC3QutC40L3Qsy3Rh9Cw0YDQu9GM0Lct0YHQv9Cw0L3QuNC10LvRjCA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiQ2F2YWxpZXIgS2luZyBDaGFybGVzIFNwYW5pZWwgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJDYXZhbGllciBLaW5nIENoYXJsZXMgU3BhbmllbCA14oCTMTAga2cifSx7ImJyZWVkIjoi0JrQsNC90LUt0LrQvtGA0YHQviA0MOKAkzYwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NX0sImJyZWVkX2VuIjoiQ2FuZSBDb3JzbyA0MOKAkzYwIGtnIiwiYnJlZWRfZXQiOiJDYW5lIENvcnNvIDQw4oCTNjAga2cifSx7ImJyZWVkIjoi0JrQsNC90LUt0LrQvtGA0YHQviDQsdC+0LvQtdC1IDYwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6OTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjoxMDV9LCJicmVlZF9lbiI6IkNhbmUgQ29yc28gb3ZlciA2MCBrZyIsImJyZWVkX2V0IjoiQ2FuZSBDb3JzbyDDvGxlIDYwIGtnIn0seyJicmVlZCI6ItCa0LDRgNC10LvQvi3RhNC40L3RgdC60LDRjyDQu9Cw0LnQutCwINC00L4gMTMg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjV9LCJicmVlZF9lbiI6IkthcmVsaWFuLUZpbm5pc2ggTGFpa2EgdXAgdG8gMTMga2ciLCJicmVlZF9ldCI6IkthcmphbGEtU29vbWUgbGFpa2Ega3VuaSAxMyBrZyJ9LHsiYnJlZWQiOiLQmtC40YLQsNC50YHQutCw0Y8g0YXQvtGF0LvQsNGC0LDRjyDQs9C+0LvQsNGPIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMyLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDIsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJDaGluZXNlIENyZXN0ZWQgaGFpcmxlc3MgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJIaWluYSBoYXJqYWtvZXIga2FydmF0dSA14oCTMTAga2cifSx7ImJyZWVkIjoi0JrQuNGC0LDQudGB0LrQsNGPINGF0L7RhdC70LDRgtCw0Y8g0LPQvtC70LDRjyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyOCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiQ2hpbmVzZSBDcmVzdGVkIGhhaXJsZXNzIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IkhpaW5hIGhhcmpha29lciBrYXJ2YXR1IGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQmtC40YLQsNC50YHQutCw0Y8g0YXQvtGF0LvQsNGC0LDRjyDQv9GD0YXQvtCy0LDRjyA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MH0sImJyZWVkX2VuIjoiQ2hpbmVzZSBDcmVzdGVkIHBvd2RlcnB1ZmYgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJIaWluYSBoYXJqYWtvZXIgUG93ZGVycHVmZiA14oCTMTAga2cifSx7ImJyZWVkIjoi0JrQuNGC0LDQudGB0LrQsNGPINGF0L7RhdC70LDRgtCw0Y8g0L/Rg9GF0L7QstCw0Y8g0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6IkNoaW5lc2UgQ3Jlc3RlZCBwb3dkZXJwdWZmIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IkhpaW5hIGhhcmpha29lciBQb3dkZXJwdWZmIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQmtC+0LrQsNC/0YMgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzV9LCJicmVlZF9lbiI6IkNvY2thcG9vIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiQ29ja2Fwb28gNeKAkzEwIGtnIn0seyJicmVlZCI6ItCa0L7QutCw0L/RgyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2NX0sImJyZWVkX2VuIjoiQ29ja2Fwb28gdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiQ29ja2Fwb28ga3VuaSA1IGtnIn0seyJicmVlZCI6ItCa0L7Qu9C70LggMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJDb2xsaWUgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiS29sbCAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCa0L7Qu9C70LggMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjExMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiQ29sbGllIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IktvbGwgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQmtC+0LzQvtC90LTQvtGAIDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMTB9LCJicmVlZF9lbiI6IktvbW9uZG9yIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IktvbW9uZG9yIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JrQvtC80L7QvdC00L7RgCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6ODUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTMwfSwiYnJlZWRfZW4iOiJLb21vbmRvciBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJLb21vbmRvciDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNC+0YAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjcwfSwiYnJlZWRfZW4iOiJMYWJyYWRvciBSZXRyaWV2ZXIgc21vb3RoIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9yaSByZXRyaWl2ZXIgbMO8aGlrYXJ2YWxpbmUgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQm9Cw0LHRgNCw0LTQvtGAINCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdGL0LkgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjU1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo4MH0sImJyZWVkX2VuIjoiTGFicmFkb3IgUmV0cmlldmVyIHNtb290aCAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvcmkgcmV0cmlpdmVyIGzDvGhpa2FydmFsaW5lIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00L7RgCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3Ri9C5INCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjgwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTV9LCJicmVlZF9lbiI6IkxhYnJhZG9yIFJldHJpZXZlciBzbW9vdGggb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiTGFicmFkb3JpIHJldHJpaXZlciBsw7xoaWthcnZhbGluZSDDvGxlIDQwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNC+0YAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90YvQuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IkxhYnJhZG9yIFJldHJpZXZlciBsb25nLWNvYXRlZCAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvcmkgcmV0cmlpdmVyIHBpa2thcnZhbGluZSAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCb0LDQsdGA0LDQtNC+0YAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90YvQuSAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTEwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJMYWJyYWRvciBSZXRyaWV2ZXIgbG9uZy1jb2F0ZWQgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiTGFicmFkb3JpIHJldHJpaXZlciBwaWtrYXJ2YWxpbmUgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQm9Cw0LHRgNCw0LTQvtGAINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdGL0Lkg0LHQvtC70LXQtSA0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEzMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiTGFicmFkb3IgUmV0cmlldmVyIGxvbmctY29hdGVkIG92ZXIgNDAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9yaSByZXRyaWl2ZXIgcGlra2FydmFsaW5lIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00YPQtNC10LvRjCAxMOKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IkxhYnJhZG9vZGxlIDEw4oCTMjAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9vZGxlIDEw4oCTMjAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00YPQtNC10LvRjCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTB9LCJicmVlZF9lbiI6IkxhYnJhZG9vZGxlIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkxhYnJhZG9vZGxlIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0JvQsNCx0YDQsNC00YPQtNC10LvRjCAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwfSwiYnJlZWRfZW4iOiJMYWJyYWRvb2RsZSAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJMYWJyYWRvb2RsZSAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCb0LXQstGA0LXRgtC60LAgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MH0sImJyZWVkX2VuIjoiSXRhbGlhbiBHcmV5aG91bmQgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJJdGFhbGlhIHZpbmRrb2VyIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQm9C10LLRgNC10YLQutCwINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjI1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MzV9LCJicmVlZF9lbiI6Ikl0YWxpYW4gR3JleWhvdW5kIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6Ikl0YWFsaWEgdmluZGtvZXIga3VuaSA1IGtnIn0seyJicmVlZCI6ItCb0YXQsNGB0YHQutC40Lkg0LDQv9GB0L4gNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzV9LCJicmVlZF9lbiI6IkxoYXNhIEFwc28gNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJMaGFzYSBBcHNvIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQm9GF0LDRgdGB0LrQuNC5INCw0L/RgdC+INC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjY1fSwiYnJlZWRfZW4iOiJMaGFzYSBBcHNvIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IkxoYXNhIEFwc28ga3VuaSA1IGtnIn0seyJicmVlZCI6ItCc0LDQu9GM0YLQtdC30LUiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6Ik1hbHRlc2UiLCJicmVlZF9ldCI6Ik1hbHRhIGJvbG9uZWVzIn0seyJicmVlZCI6ItCc0LDQu9GM0YLQuNC50YHQutCw0Y8g0LHQvtC70L7QvdC60LAgNeKAkzgg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2NX0sImJyZWVkX2VuIjoiTWFsdGVzZSBCb2xvZ25lc2UgNeKAkzgga2ciLCJicmVlZF9ldCI6Ik1hbHRhIGJvbG9uZWVzIDXigJM4IGtnIn0seyJicmVlZCI6ItCc0LDQu9GM0YLQuNC50YHQutCw0Y8g0LHQvtC70L7QvdC60LAg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6Ik1hbHRlc2UgQm9sb2duZXNlIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6Ik1hbHRhIGJvbG9uZWVzIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQnNCw0LvRjNGC0LjQv9GDIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MH0sImJyZWVkX2VuIjoiTWFsdGlwb28gMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiTWFsdGlwdXUgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQnNCw0LvRjNGC0LjQv9GDIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJNYWx0aXBvbyA14oCTMTAga2ciLCJicmVlZF9ldCI6Ik1hbHRpcHV1IDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQnNCw0LvRjNGC0LjQv9GDINC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJNYWx0aXBvbyB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJNYWx0aXB1dSBrdW5pIDUga2cifSx7ImJyZWVkIjoi0JzQtdGC0LjRgSDQutGA0YPQv9C90YvQuSAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJNaXhlZCBicmVlZCBsYXJnZSAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJTZWdhdmVyZCBzdXVyIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0JzQtdGC0LjRgSDQutGA0YPQv9C90YvQuSDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTIwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTIwfSwiYnJlZWRfZW4iOiJNaXhlZCBicmVlZCBsYXJnZSBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJTZWdhdmVyZCBzdXVyIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0JzQtdGC0LjRgSDQvNC10LvQutC40LkgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6Ik1peGVkIGJyZWVkIHNtYWxsIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiU2VnYXZlcmQgdsOkaWtlIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQnNC10YLQuNGBINC80LXQu9C60LjQuSDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1MH0sImJyZWVkX2VuIjoiTWl4ZWQgYnJlZWQgc21hbGwgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiU2VnYXZlcmQgdsOkaWtlIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQnNC10YLQuNGBINGB0YDQtdC00L3QuNC5IDEw4oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjcwfSwiYnJlZWRfZW4iOiJNaXhlZCBicmVlZCBtZWRpdW0gMTDigJMyMCBrZyIsImJyZWVkX2V0IjoiU2VnYXZlcmQga2Vza21pbmUgMTDigJMyMCBrZyJ9LHsiYnJlZWQiOiLQnNC10YLQuNGBINGB0YDQtdC00L3QuNC5IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjYwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjg1fSwiYnJlZWRfZW4iOiJNaXhlZCBicmVlZCBtZWRpdW0gMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiU2VnYXZlcmQga2Vza21pbmUgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQnNC40YLRgtC10LvRjNGI0L3QsNGD0YbQtdGAIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MCwi0KLRgNC40LzQvNC40L3QsyI6NzV9LCJicmVlZF9lbiI6IlN0YW5kYXJkIFNjaG5hdXplciAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJTdGFuZGFyZMWhbmF1dHNlciAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCc0LjRgtGC0LXQu9GM0YjQvdCw0YPRhtC10YAgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NjUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjgwLCLQotGA0LjQvNC80LjQvdCzIjo4NX0sImJyZWVkX2VuIjoiU3RhbmRhcmQgU2NobmF1emVyIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IlN0YW5kYXJkxaFuYXV0c2VyIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0JzQvtC/0YEiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjUwfSwiYnJlZWRfZW4iOiJQdWciLCJicmVlZF9ldCI6Ik1vcHMifSx7ImJyZWVkIjoi0J3QtdCy0YHQutCw0Y8g0L7RgNGF0LjQtNC10Y8iLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NTV9LCJicmVlZF9lbiI6Ik5ldmEgT3JjaGlkIiwiYnJlZWRfZXQiOiJOZWV2YSBvcmhpZGVlIn0seyJicmVlZCI6ItCd0LXQvNC10YbQutCw0Y8g0L7QstGH0LDRgNC60LAgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiR2VybWFuIFNoZXBoZXJkIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IlNha3NhIGxhbWJha29lciAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCd0LXQvNC10YbQutCw0Y8g0L7QstGH0LDRgNC60LAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6Ikdlcm1hbiBTaGVwaGVyZCAzMOKAkzQwIGtnIiwiYnJlZWRfZXQiOiJTYWtzYSBsYW1iYWtvZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQndC10LzQtdGG0LrQsNGPINC+0LLRh9Cw0YDQutCwINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo4NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTE1fSwiYnJlZWRfZW4iOiJHZXJtYW4gU2hlcGhlcmQgb3ZlciA0MCBrZyIsImJyZWVkX2V0IjoiU2Frc2EgbGFtYmFrb2VyIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0J3QvtGA0LLQuNGHLdGC0LXRgNGM0LXRgCIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQ1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo2MCwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6Ik5vcndpY2ggVGVycmllciIsImJyZWVkX2V0IjoiTm9yd2l0xaFpIHRlcmplciJ9LHsiYnJlZWQiOiLQndC+0YDRhNC+0LvQui3RgtC10YDRjNC10YAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJOb3Jmb2xrIFRlcnJpZXIiLCJicmVlZF9ldCI6Ik5vcmZvbGtpIHRlcmplciJ9LHsiYnJlZWQiOiLQndGM0Y7RhNCw0YPQvdC00LvQtdC90LQgNDDigJM2MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjg1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6OTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEzMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjExNX0sImJyZWVkX2VuIjoiTmV3Zm91bmRsYW5kIDQw4oCTNjAga2ciLCJicmVlZF9ldCI6Ik5ld2ZvdW5kbGFuZGkga29lciA0MOKAkzYwIGtnIn0seyJicmVlZCI6ItCd0YzRjtGE0LDRg9C90LTQu9C10L3QtCDQsdC+0LvQtdC1IDYwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MTAwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6MTE1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxNTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMzB9LCJicmVlZF9lbiI6Ik5ld2ZvdW5kbGFuZCBvdmVyIDYwIGtnIiwiYnJlZWRfZXQiOiJOZXdmb3VuZGxhbmRpIGtvZXIgw7xsZSA2MCBrZyJ9LHsiYnJlZWQiOiLQn9Cw0L/QuNC50L7QvSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiUGFwaWxsb24iLCJicmVlZF9ldCI6IlBhcGlsbG9uIn0seyJicmVlZCI6ItCf0LXQutC40L3QtdGBIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJQZWtpbmdlc2UgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJQZWtpbmVzaSBrb2VyIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQn9C10LrQuNC90LXRgSDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiUGVraW5nZXNlIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IlBla2luZXNpIGtvZXIga3VuaSA1IGtnIn0seyJicmVlZCI6ItCf0YPQtNC10LvRjCDQsdC+0LvRjNGI0L7QuSAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6OTB9LCJicmVlZF9lbiI6IlN0YW5kYXJkIFBvb2RsZSAyMOKAkzMwIGtnIiwiYnJlZWRfZXQiOiJTdGFuZGFyZHB1dWRlbCAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCf0YPQtNC10LvRjCDQsdC+0LvRjNGI0L7QuSAzMOKAkzQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo4MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTAwfSwiYnJlZWRfZW4iOiJTdGFuZGFyZCBQb29kbGUgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiU3RhbmRhcmRwdXVkZWwgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQn9GD0LTQtdC70Ywg0LrQsNGA0LvQuNC60L7QstGL0LkgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6Ik1pbmlhdHVyZSBQb29kbGUgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJLw6TDpGJ1c3B1dWRlbCA14oCTMTAga2cifSx7ImJyZWVkIjoi0J/Rg9C00LXQu9GMINC80LDQu9GL0LkgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJTbWFsbCBQb29kbGUgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiVsOkaWtlIHB1dWRlbCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCf0YPQtNC10LvRjCDQvNCw0LvRi9C5IDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4MH0sImJyZWVkX2VuIjoiU21hbGwgUG9vZGxlIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IlbDpGlrZSBwdXVkZWwgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQn9GD0LTQtdC70Ywg0YLQvtC5INC00L4gNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJUb3kgUG9vZGxlIHVwIHRvIDUga2ciLCJicmVlZF9ldCI6Ik3DpG5ndWFzamEgcHV1ZGVsIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQoNC40LfQtdC90YjQvdCw0YPRhtC10YAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMCwi0KLRgNC40LzQvNC40L3QsyI6MTEwfSwiYnJlZWRfZW4iOiJHaWFudCBTY2huYXV6ZXIgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiU3V1csWhbmF1dHNlciAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCg0LjQt9C10L3RiNC90LDRg9GG0LXRgCDQsdC+0LvQtdC1IDQwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo5NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6MTIwLCLQotGA0LjQvNC80LjQvdCzIjoxMjV9LCJicmVlZF9lbiI6IkdpYW50IFNjaG5hdXplciBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJTdXVyxaFuYXV0c2VyIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0KDRg9GB0YHQutCw0Y8g0YbQstC10YLQvdCw0Y8g0LHQvtC70L7QvdC60LAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IlJ1c3NpYW4gQ29sb3JlZCBMYXBkb2ciLCJicmVlZF9ldCI6IlZlbmUgdsOkcnZpbGluZSBzw7xsZWtvZXIifSx7ImJyZWVkIjoi0KDRg9GB0YHQutC40Lkg0L7RhdC+0YLQvdC40YfQuNC5INGB0L/QsNC90LjQtdC70YwgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwfSwiYnJlZWRfZW4iOiJSdXNzaWFuIFNwYW5pZWwgMTDigJMxNSBrZyIsImJyZWVkX2V0IjoiVmVuZSBqYWhpc3BhbmplbCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCg0YPRgdGB0LrQuNC5INC+0YXQvtGC0L3QuNGH0LjQuSDRgdC/0LDQvdC40LXQu9GMIDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjY1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo4MH0sImJyZWVkX2VuIjoiUnVzc2lhbiBTcGFuaWVsIDE14oCTMjAga2ciLCJicmVlZF9ldCI6IlZlbmUgamFoaXNwYW5qZWwgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQoNGD0YHRgdC60LjQuSDRgtC+0Lkg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1fSwiYnJlZWRfZW4iOiJSdXNzaWFuIFRveSBzbW9vdGgiLCJicmVlZF9ldCI6IlZlbmUgVG95IGzDvGhpa2FydmFsaW5lIn0seyJicmVlZCI6ItCg0YPRgdGB0LrQuNC5INGC0L7QuSDQtNC70LjQvdC90L7RiNC10YDRgdGC0L3Ri9C5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjMwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjU1fSwiYnJlZWRfZW4iOiJSdXNzaWFuIFRveSBsb25nLWNvYXRlZCIsImJyZWVkX2V0IjoiVmVuZSBUb3kgcGlra2FydmFsaW5lIn0seyJicmVlZCI6ItCg0YPRgdGB0LrQuNC5INGH0LXRgNC90YvQuSDRgtC10YDRjNC10YAgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjYwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjEwMH0sImJyZWVkX2VuIjoiQmxhY2sgUnVzc2lhbiBUZXJyaWVyIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6Ik11c3QgVmVuZSB0ZXJqZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQoNGD0YHRgdC60LjQuSDRh9C10YDQvdGL0Lkg0YLQtdGA0YzQtdGAINCx0L7Qu9C10LUgNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3NSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjk1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMjB9LCJicmVlZF9lbiI6IkJsYWNrIFJ1c3NpYW4gVGVycmllciBvdmVyIDQwIGtnIiwiYnJlZWRfZXQiOiJNdXN0IFZlbmUgdGVyamVyIMO8bGUgNDAga2cifSx7ImJyZWVkIjoi0KDRg9GB0YHQutC+LdC10LLRgNC+0L/QtdC50YHQutCw0Y8g0LvQsNC50LrQsCAyMOKAkzI4INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJSdXNzaWFuLUV1cm9wZWFuIExhaWthIDIw4oCTMjgga2ciLCJicmVlZF9ldCI6IlZlbmUtRXVyb29wYSBsYWlrYSAyMOKAkzI4IGtnIn0seyJicmVlZCI6ItCh0LDQvNC+0LXQtCAyMOKAkzMwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NjAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo3NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjkwfSwiYnJlZWRfZW4iOiJTYW1veWVkIDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IlNhbW9qZWVkIDIw4oCTMzAga2cifSx7ImJyZWVkIjoi0KHQsNC80L7QtdC0IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo3MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjg1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6MTAwfSwiYnJlZWRfZW4iOiJTYW1veWVkIDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IlNhbW9qZWVkIDMw4oCTNDAga2cifSx7ImJyZWVkIjoi0KHQtdGC0YLQtdGAINCw0L3Qs9C70LjQudGB0LrQuNC5IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo1MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjcwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo5MH0sImJyZWVkX2VuIjoiRW5nbGlzaCBTZXR0ZXIgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiSW5nbGlzZSBzZXR0ZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQodC10YLRgtC10YAg0LPQvtGA0LTQvtC9IDMw4oCTNDAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjgwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDB9LCJicmVlZF9lbiI6IkdvcmRvbiBTZXR0ZXIgMzDigJM0MCBrZyIsImJyZWVkX2V0IjoiR29yZG9uaSBzZXR0ZXIgMzDigJM0MCBrZyJ9LHsiYnJlZWQiOiLQodC10YLRgtC10YAg0LjRgNC70LDQvdC00YHQutC40LkgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjUwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwfSwiYnJlZWRfZW4iOiJJcmlzaCBTZXR0ZXIgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoiSWlyaSBzZXR0ZXIgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQodC40LHQsC3QuNC90YMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2MCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjcwfSwiYnJlZWRfZW4iOiJTaGliYSBJbnUiLCJicmVlZF9ldCI6IlNoaWJhIEludSJ9LHsiYnJlZWQiOiLQodC40LvQuNGF0LXQvC3RgtC10YDRjNC10YAiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJTZWFseWhhbSBUZXJyaWVyIiwiYnJlZWRfZXQiOiJTZWFseWhhbWkgdGVyamVyIn0seyJicmVlZCI6ItCh0LrQvtGC0Yct0YLQtdGA0YzQtdGAIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiU2NvdHRpc2ggVGVycmllciIsImJyZWVkX2V0IjoixaBvdGkgdGVyamVyIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90LDRjyDQutCw0YDQu9C40LrQvtCy0LDRjyA14oCTMTAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NTB9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCBzbW9vdGggbWluaWF0dXJlIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiVGFrc2lrb2VyIGzDvGhpa2FydmFsaW5lIGvDpMOkYnVzIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdCw0Y8g0LrRgNC+0LvQuNGH0YzRjyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NDV9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCBzbW9vdGggcmFiYml0IHVwIHRvIDUga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBsw7xoaWthcnZhbGluZSBrw7zDvGxpayBrdW5pIDUga2cifSx7ImJyZWVkIjoi0KLQsNC60YHQsCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3QsNGPINGB0YLQsNC90LTQsNGA0YLQvdCw0Y8gMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2MH0sImJyZWVkX2VuIjoiRGFjaHNodW5kIHNtb290aCBzdGFuZGFyZCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIgbMO8aGlrYXJ2YWxpbmUgc3RhbmRhcmQgMTDigJMxNSBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdCw0Y8g0LrQsNGA0LvQuNC60L7QstCw0Y8gNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjB9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCBsb25nLWNvYXRlZCBtaW5pYXR1cmUgNeKAkzEwIGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIgcGlra2FydmFsaW5lIGvDpMOkYnVzIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdCw0Y8g0LrRgNC+0LvQuNGH0YzRjyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiRGFjaHNodW5kIGxvbmctY29hdGVkIHJhYmJpdCB1cCB0byA1IGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIgcGlra2FydmFsaW5lIGvDvMO8bGlrIGt1bmkgNSBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdCw0Y8g0YHRgtCw0L3QtNCw0YDRgtC90LDRjyAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzB9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCBsb25nLWNvYXRlZCBzdGFuZGFyZCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIgcGlra2FydmFsaW5lIHN0YW5kYXJkIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KLQsNC60YHQsCDQttC10YHRgtC60L7RiNC10YDRgdGC0L3QsNGPINC60LDRgNC70LjQutC+0LLQsNGPIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiRGFjaHNodW5kIHdpcmUtaGFpcmVkIG1pbmlhdHVyZSA14oCTMTAga2ciLCJicmVlZF9ldCI6IlRha3Npa29lciBrYXJ1a2FydmFsaW5lIGvDpMOkYnVzIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQotCw0LrRgdCwINC20LXRgdGC0LrQvtGI0LXRgNGB0YLQvdCw0Y8g0LrRgNC+0LvQuNGH0YzRjyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NSwi0KLRgNC40LzQvNC40L3QsyI6NTV9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCB3aXJlLWhhaXJlZCByYWJiaXQgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiVGFrc2lrb2VyIGthcnVrYXJ2YWxpbmUga8O8w7xsaWsga3VuaSA1IGtnIn0seyJicmVlZCI6ItCi0LDQutGB0LAg0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90LDRjyDRgdGC0LDQvdC00LDRgNGC0L3QsNGPIDEw4oCTMTUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjU1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo3MCwi0KLRgNC40LzQvNC40L3QsyI6NzV9LCJicmVlZF9lbiI6IkRhY2hzaHVuZCB3aXJlLWhhaXJlZCBzdGFuZGFyZCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJUYWtzaWtvZXIga2FydWthcnZhbGluZSBzdGFuZGFyZCAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCj0LjQv9C/0LXRgiAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NX0sImJyZWVkX2VuIjoiV2hpcHBldCAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJXaGlwcGV0IDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KPQuNC/0L/QtdGCIDE14oCTMjAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo0MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjUwfSwiYnJlZWRfZW4iOiJXaGlwcGV0IDE14oCTMjAga2ciLCJicmVlZF9ldCI6IldoaXBwZXQgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQpNC40L3RgdC60LjQuSDQu9Cw0L/RhdGD0L3QtCAxNeKAkzIwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NTAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo2NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6ODV9LCJicmVlZF9lbiI6IkZpbm5pc2ggTGFwcGh1bmQgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoiU29vbWUgbGFtYmFrb2VyIDE14oCTMjAga2cifSx7ImJyZWVkIjoi0KTQuNC90YHQutC40Lkg0LvQsNC/0YXRg9C90LQgMjDigJMyNCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjU1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NzAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjkwfSwiYnJlZWRfZW4iOiJGaW5uaXNoIExhcHBodW5kIDIw4oCTMjQga2ciLCJicmVlZF9ldCI6IlNvb21lIGxhbWJha29lciAyMOKAkzI0IGtnIn0seyJicmVlZCI6ItCk0L7QutGB0YLQtdGA0YzQtdGAINC20LXRgdGC0LrQvtGI0LXRgNGB0YLQvdGL0LkgMTDigJMxNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjcwLCLQotGA0LjQvNC80LjQvdCzIjo3NX0sImJyZWVkX2VuIjoiV2lyZSBGb3ggVGVycmllciAxMOKAkzE1IGtnIiwiYnJlZWRfZXQiOiJLYXJ1a2FydmFsaW5lIGZveHRlcmplciAxMOKAkzE1IGtnIn0seyJicmVlZCI6ItCk0L7QutGB0YLQtdGA0YzQtdGAINC20LXRgdGC0LrQvtGI0LXRgNGB0YLQvdGL0LkgNeKAkzEwINC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCi0YDQuNC80LzQuNC90LMiOjY1fSwiYnJlZWRfZW4iOiJXaXJlIEZveCBUZXJyaWVyIDXigJMxMCBrZyIsImJyZWVkX2V0IjoiS2FydWthcnZhbGluZSBmb3h0ZXJqZXIgNeKAkzEwIGtnIn0seyJicmVlZCI6ItCk0YDQsNC90YbRg9C30YHQutC40Lkg0LHRg9C70YzQtNC+0LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjYwfSwiYnJlZWRfZW4iOiJGcmVuY2ggQnVsbGRvZyIsImJyZWVkX2V0IjoiUHJhbnRzdXNlIGJ1bGRvZyJ9LHsiYnJlZWQiOiLQpdCw0YHQutC4IDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6OTB9LCJicmVlZF9lbiI6IlNpYmVyaWFuIEh1c2t5IDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IlNpYmVyaSBodXNreSAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCl0LDRgdC60LggMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjoxMDB9LCJicmVlZF9lbiI6IlNpYmVyaWFuIEh1c2t5IDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IlNpYmVyaSBodXNreSAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCm0LLQtdGA0LPRiNC90LDRg9GG0LXRgCAxMOKAkzE1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NzAsItCi0YDQuNC80LzQuNC90LMiOjc1fSwiYnJlZWRfZW4iOiJNaW5pYXR1cmUgU2NobmF1emVyIDEw4oCTMTUga2ciLCJicmVlZF9ldCI6IkvDpMOkYnVzxaFuYXV0c2VyIDEw4oCTMTUga2cifSx7ImJyZWVkIjoi0KbQstC10YDQs9GI0L3QsNGD0YbQtdGAIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwLCLQotGA0LjQvNC80LjQvdCzIjo2NX0sImJyZWVkX2VuIjoiTWluaWF0dXJlIFNjaG5hdXplciA14oCTMTAga2ciLCJicmVlZF9ldCI6IkvDpMOkYnVzxaFuYXV0c2VyIDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQp9Cw0YMt0YfQsNGDIDIw4oCTMzAg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0Ijo2MCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjc1LCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjoxMDAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo5MH0sImJyZWVkX2VuIjoiQ2hvdyBDaG93IDIw4oCTMzAga2ciLCJicmVlZF9ldCI6IkNob3cgQ2hvdyAyMOKAkzMwIGtnIn0seyJicmVlZCI6ItCn0LDRgy3Rh9Cw0YMgMzDigJM0MCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjcwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6ODUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjExMCwi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOjEwMH0sImJyZWVkX2VuIjoiQ2hvdyBDaG93IDMw4oCTNDAga2ciLCJicmVlZF9ldCI6IkNob3cgQ2hvdyAzMOKAkzQwIGtnIn0seyJicmVlZCI6ItCn0LjRhdGD0LDRhdGD0LAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjoyNSwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjM1fSwiYnJlZWRfZW4iOiJDaGlodWFodWEgc21vb3RoIiwiYnJlZWRfZXQiOiJUxaFpaHVhaHVhIGzDvGhpa2FydmFsaW5lIn0seyJicmVlZCI6ItCn0LjRhdGD0LDRhdGD0LAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90YvQuSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiQ2hpaHVhaHVhIGxvbmctY29hdGVkIiwiYnJlZWRfZXQiOiJUxaFpaHVhaHVhIHBpa2thcnZhbGluZSJ9LHsiYnJlZWQiOiLQqNCw0YDQv9C10LkgMTXigJMyMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2NX0sImJyZWVkX2VuIjoiU2hhciBQZWkgMTXigJMyMCBrZyIsImJyZWVkX2V0IjoixaBhci1QZWkgMTXigJMyMCBrZyJ9LHsiYnJlZWQiOiLQqNCw0YDQv9C10LkgMjDigJMzMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQ1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo3MH0sImJyZWVkX2VuIjoiU2hhciBQZWkgMjDigJMzMCBrZyIsImJyZWVkX2V0IjoixaBhci1QZWkgMjDigJMzMCBrZyJ9LHsiYnJlZWQiOiLQqNC10LvRgtC4Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjB9LCJicmVlZF9lbiI6IlNoZXRsYW5kIFNoZWVwZG9nIiwiYnJlZWRfZXQiOiLFoGV0bGFuZGkgbGFtYmFrb2VyIn0seyJicmVlZCI6ItCo0Lgt0YLRhtGDIDXigJMxMCDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjM1LCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NDUsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjYwfSwiYnJlZWRfZW4iOiJTaGloIFR6dSA14oCTMTAga2ciLCJicmVlZF9ldCI6IlNoaWggVHp1IDXigJMxMCBrZyJ9LHsiYnJlZWQiOiLQqNC4LdGC0YbRgyDQtNC+IDUg0LrQsyIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiU2hpaCBUenUgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiU2hpaCBUenUga3VuaSA1IGtnIn0seyJicmVlZCI6ItCo0L3QsNGD0YbQtdGAINC80LjQvdC40LDRgtGO0YDQvdGL0Lkg0LTQviA1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0KLRgNC40LzQvNC40L3QsyI6NjV9LCJicmVlZF9lbiI6Ik1pbmlhdHVyZSBTY2huYXV6ZXIgdXAgdG8gNSBrZyIsImJyZWVkX2V0IjoiS8Okw6RidXPFoW5hdXRzZXIga3VuaSA1IGtnIn0seyJicmVlZCI6ItCo0L/QuNGGINC90LXQvNC10YbQutC40LkgLyDQv9C+0LzQtdGA0LDQvdGB0LrQuNC5INCx0L7Qu9C10LUgMyw1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6NDAsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo1MCwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjUsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo2MH0sImJyZWVkX2VuIjoiR2VybWFuIFNwaXR6IC8gUG9tZXJhbmlhbiBvdmVyIDMsNSBrZyIsImJyZWVkX2V0IjoiU2Frc2Egc3BpdHMgLyBQb21lcmFuaWFuIMO8bGUgMyw1IGtnIn0seyJicmVlZCI6ItCo0L/QuNGGINC90LXQvNC10YbQutC40LkgLyDQv9C+0LzQtdGA0LDQvdGB0LrQuNC5INC00L4gMyw1INC60LMiLCJzZXJ2aWNlcyI6eyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6MzUsItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Ijo0NSwi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6NjAsItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjo1NX0sImJyZWVkX2VuIjoiR2VybWFuIFNwaXR6IC8gUG9tZXJhbmlhbiB1cCB0byAzLDUga2ciLCJicmVlZF9ldCI6IlNha3NhIHNwaXRzIC8gUG9tZXJhbmlhbiBrdW5pIDMsNSBrZyJ9LHsiYnJlZWQiOiLQqNC/0LjRhiDRj9C/0L7QvdGB0LrQuNC5Iiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTAsItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOjY1LCLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6NjB9LCJicmVlZF9lbiI6IkphcGFuZXNlIFNwaXR6IiwiYnJlZWRfZXQiOiJKYWFwYW5pIHNwaXRzIn0seyJicmVlZCI6ItCt0YHRgtC+0L3RgdC60LDRjyDQs9C+0L3Rh9Cw0Y8gMTXigJMyNSDQutCzIiwic2VydmljZXMiOnsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOjQwLCLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6NTB9LCJicmVlZF9lbiI6IkVzdG9uaWFuIEhvdW5kIDE14oCTMjUga2ciLCJicmVlZF9ldCI6IkVlc3RpIGhhZ2lqYXMgMTXigJMyNSBrZyJ9LHsiYnJlZWQiOiLQr9C/0L7QvdGB0LrQuNC5INGF0LjQvSIsInNlcnZpY2VzIjp7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjozMCwi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOjQwLCLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Ijo1NX0sImJyZWVkX2VuIjoiSmFwYW5lc2UgQ2hpbiIsImJyZWVkX2V0IjoiSmFhcGFuaSBDaGluIn0seyJicmVlZCI6ItCa0L7RiNC60LAg0LrQvtGA0L7RgtC60L7RiNC10YDRgdGC0L3QsNGPIiwic2VydmljZXMiOnsi0JLRi9GH0LXRgSI6NDV9LCJicmVlZF9lbiI6IkNhdCBzaG9ydC1oYWlyZWQiLCJicmVlZF9ldCI6Ikthc3MgbMO8aGlrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0JrQvtGI0LrQsCDQtNC70LjQvdC90L7RiNC10YDRgdGC0L3QsNGPIiwic2VydmljZXMiOnsi0JLRi9GH0LXRgSI6NTV9LCJicmVlZF9lbiI6IkNhdCBsb25nLWhhaXJlZCIsImJyZWVkX2V0IjoiS2FzcyBwaWtrYXJ2YWxpbmUifSx7ImJyZWVkIjoi0JrQvtGI0LrQsCDQnNC10LnQvS3QutGD0L0iLCJzZXJ2aWNlcyI6eyLQktGL0YfRkdGBIjo2MH0sImJyZWVkX2VuIjoiQ2F0IE1haW5lIENvb24iLCJicmVlZF9ldCI6Ikthc3MgTWFpbmUgQ29vbiJ9XTsKdmFyIFJBSUxXQVkgPSAiaHR0cHM6Ly9yamdyb29taW5nLnVwLnJhaWx3YXkuYXBwL2Jvb2siOwp2YXIgR09PR0xFX1NDUklQVCA9ICJodHRwczovL3NjcmlwdC5nb29nbGUuY29tL21hY3Jvcy9zL0FLZnljYnlUU1otZUpNZGVwLUQwTHItbngwX1Y0SEJXZ0lJY3RuUlQycmpTRHZCeWJqNUNZSTNOSzJNcWNBd19jZmN6Z1JFaWZnL2V4ZWMiOwp2YXIgRkFMTEJBQ0tfVElNRVMgPSBbJzEwOjAwJywnMTA6MzAnLCcxMTowMCcsJzExOjMwJywnMTI6MDAnLCcxMjozMCcsJzEzOjAwJywnMTM6MzAnLCcxNDowMCcsJzE0OjMwJywnMTU6MDAnLCcxNTozMCcsJzE2OjAwJywnMTY6MzAnLCcxNzowMCcsJzE3OjMwJywnMTg6MDAnXTsKdmFyIGJvb2tpbmcgPSB7YnJlZWQ6JycsYnJlZWREaXNwbGF5OicnLHNlcnZpY2U6JycscHJpY2U6MCxtYXN0ZXI6JycsZ3Jvb21IaXN0b3J5OicnLGRhdGU6JycsdGltZTonJyxsYW5nOidydSd9Owp2YXIgc2VsQnJlZWQgPSBudWxsOwp2YXIgY1kgPSBuZXcgRGF0ZSgpLmdldEZ1bGxZZWFyKCk7CnZhciBjTSA9IG5ldyBEYXRlKCkuZ2V0TW9udGgoKTsKdmFyIHN0ZXAgPSAxOwp2YXIgTU9OVEhTID0gWyfQr9C90LLQsNGA0YwnLCfQpNC10LLRgNCw0LvRjCcsJ9Cc0LDRgNGCJywn0JDQv9GA0LXQu9GMJywn0JzQsNC5Jywn0JjRjtC90YwnLCfQmNGO0LvRjCcsJ9CQ0LLQs9GD0YHRgicsJ9Ch0LXQvdGC0Y/QsdGA0YwnLCfQntC60YLRj9Cx0YDRjCcsJ9Cd0L7Rj9Cx0YDRjCcsJ9CU0LXQutCw0LHRgNGMJ107CgpmdW5jdGlvbiBzaG93U2NyZWVuKGlkKSB7CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLnNjcmVlbicpLmZvckVhY2goZnVuY3Rpb24ocyl7cy5jbGFzc0xpc3QucmVtb3ZlKCdhY3RpdmUnKTt9KTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZChpZCkuY2xhc3NMaXN0LmFkZCgnYWN0aXZlJyk7CiAgd2luZG93LnNjcm9sbFRvKDAsMCk7Cn0KCmZ1bmN0aW9uIGdvU3RlcChuKSB7CiAgWydiazEnLCdiazInLCdiazMnLCdiazQnLCdiazUnXS5mb3JFYWNoKGZ1bmN0aW9uKGlkLGkpewogICAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoaWQpLmNsYXNzTmFtZSA9ICdzdGVwJyArIChpKzE9PT1uPycgc2hvdyc6JycpOwogIH0pOwogIGZvcih2YXIgaT0xO2k8PTU7aSsrKXsKICAgIHZhciBwcz1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgncHMnK2kpOwogICAgdmFyIHBsPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdwbCcraSk7CiAgICBpZihpPG4pe3BzLmNsYXNzTmFtZT0ncHMgZG9uZSc7aWYocGwpcGwuY2xhc3NOYW1lPSdwbCBkb25lJzt9CiAgICBlbHNlIGlmKGk9PT1uKXtwcy5jbGFzc05hbWU9J3BzIGFjdGl2ZSc7aWYocGwpcGwuY2xhc3NOYW1lPSdwbCc7fQogICAgZWxzZXtwcy5jbGFzc05hbWU9J3BzJztpZihwbClwbC5jbGFzc05hbWU9J3BsJzt9CiAgfQogIHN0ZXA9bjsgd2luZG93LnNjcm9sbFRvKDAsMCk7CiAgaWYobj09PTIpIGZpbHRlck1hc3RlcnMoKTsKfQoKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Jvb2tCdG4nKS5vbmNsaWNrID0gZnVuY3Rpb24oKXsKICBzaG93U2NyZWVuKCdib29rU2NyZWVuJyk7IGdvU3RlcCgxKTsgYnVpbGRDYWwoKTsKfTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2JhY2tCdG4nKS5vbmNsaWNrID0gZnVuY3Rpb24oKXsKICBpZihzdGVwPjEpe2dvU3RlcChzdGVwLTEpO31lbHNle3Nob3dTY3JlZW4oJ2hvbWVTY3JlZW4nKTt9Cn07CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdob21lQnRuJykub25jbGljayA9IGZ1bmN0aW9uKCl7CiAgc2hvd1NjcmVlbignaG9tZVNjcmVlbicpOyByZXNldEFsbCgpOwp9OwoKLy8gQnJlZWQgc2VhcmNoCnZhciBpbnAgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnYklucHV0Jyk7CnZhciBkcm9wID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2JEcm9wJyk7CnZhciBjbHIgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2xyQnRuJyk7CnZhciBiYWRnZSA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzQmFkZ2UnKTsKCmlucC5hZGRFdmVudExpc3RlbmVyKCdpbnB1dCcsIGZ1bmN0aW9uKCl7CiAgdmFyIHEgPSBpbnAudmFsdWUudHJpbSgpOwogIGNsci5jbGFzc0xpc3QudG9nZ2xlKCdzaG93JywgcS5sZW5ndGg+MCk7CiAgaWYoIXEpe2Ryb3AuY2xhc3NMaXN0LnJlbW92ZSgnb3BlbicpO2Ryb3AuaW5uZXJIVE1MPScnO3JldHVybjt9CiAgdmFyIHNmPUxBTkc9PT0nZW4nPydicmVlZF9lbic6TEFORz09PSdldCc/J2JyZWVkX2V0JzonYnJlZWQnOwogIHZhciByZXM9REFUQS5maWx0ZXIoZnVuY3Rpb24oYil7cmV0dXJuKGJbc2ZdfHxiLmJyZWVkKS50b0xvd2VyQ2FzZSgpLmluZGV4T2YocS50b0xvd2VyQ2FzZSgpKSE9PS0xO30pLnNsaWNlKDAsMzUpOwogIGRyb3AuaW5uZXJIVE1MPScnOwogIHZhciBfbnI9TEFORz09PSdlbic/J0JyZWVkIG5vdCBmb3VuZCc6TEFORz09PSdldCc/J1TDtXVndSBlaSBsZWl0dWQnOifQn9C+0YDQvtC00LAg0L3QtSDQvdCw0LnQtNC10L3QsCc7CiAgdmFyIF9udD1MQU5HPT09J2VuJz8iQ2FuJ3QgZmluZCB5b3VyIGJyZWVkPyI6TEFORz09PSdldCc/J0VpIGxlaWEgb21hIHTDtXVndT8nOifQndC1INC90LDRiNC70Lgg0YHQstC+0Y4g0L/QvtGA0L7QtNGDPyc7CiAgdmFyIF9ucz1MQU5HPT09J2VuJz8nQ29udGFjdCB1cyDigJQgd2Ugd2lsbCBoZWxwIHlvdSBjaG9vc2UgYSBzZXJ2aWNlJzpMQU5HPT09J2V0Jz8nVsO1dGtlIG1laWVnYSDDvGhlbmR1c3Qg4oCUIGFpdGFtZSB0ZWVudXNlIHZhbGlkYSc6J9Ch0LLRj9C20LjRgtC10YHRjCDRgSDQvdCw0LzQuCDQu9GO0LHRi9C8INGD0LTQvtCx0L3Ri9C8INGB0L/QvtGB0L7QsdC+0Lwg4oCUINC80Ysg0L/QvtC80L7QttC10Lwg0L/QvtC00L7QsdGA0LDRgtGMINGD0YHQu9GD0LPRgyc7CiAgaWYoIXJlcy5sZW5ndGgpe2Ryb3AuaW5uZXJIVE1MPSc8ZGl2IGNsYXNzPSJub3JlcyI+JytfbnIrJzwvZGl2PjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lciIgb25jbGljaz0ic2hvd1NjcmVlbihcJ2hvbWVTY3JlZW5cJykiPjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci1pY29uIj7wn5C+PC9kaXY+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLXRleHQiPjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci10aXRsZSI+JytfbnQrJzwvZGl2PjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci1zdWIiPicrX25zKyc8L2Rpdj48L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItYXJyb3ciPuKGkjwvZGl2PjwvZGl2Pic7fQogIGVsc2V7CiAgICByZXMuZm9yRWFjaChmdW5jdGlvbihiKXsKICAgICAgdmFyIGQ9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7IGQuY2xhc3NOYW1lPSdkaXRlbSc7CiAgICAgIHZhciBibmFtZT1iW3NmXXx8Yi5icmVlZDsKICAgICAgdmFyIGlkeD1ibmFtZS50b0xvd2VyQ2FzZSgpLmluZGV4T2YocS50b0xvd2VyQ2FzZSgpKTsKICAgICAgZC5pbm5lckhUTUw9Ym5hbWUuc3Vic3RyaW5nKDAsaWR4KSsnPG1hcms+JytibmFtZS5zdWJzdHJpbmcoaWR4LGlkeCtxLmxlbmd0aCkrJzwvbWFyaz4nK2JuYW1lLnN1YnN0cmluZyhpZHgrcS5sZW5ndGgpOwogICAgICBkLm9uY2xpY2s9ZnVuY3Rpb24oKXtzZWxlY3RCcmVlZChiKTt9OwogICAgICBkcm9wLmFwcGVuZENoaWxkKGQpOwogICAgfSk7CiAgfQogIGRyb3AuY2xhc3NMaXN0LmFkZCgnb3BlbicpOwp9KTsKCmRvY3VtZW50LmFkZEV2ZW50TGlzdGVuZXIoJ2NsaWNrJyxmdW5jdGlvbihlKXsKICBpZighZS50YXJnZXQuY2xvc2VzdCgnLmJ3cmFwJykpZHJvcC5jbGFzc0xpc3QucmVtb3ZlKCdvcGVuJyk7Cn0pOwpjbHIub25jbGljayA9IHJlc2V0QnJlZWQ7CgpmdW5jdGlvbiBzZWxlY3RCcmVlZChiKXsKICBzZWxCcmVlZD1iOyBib29raW5nLmJyZWVkPWIuYnJlZWQ7CiAgaW5wLnZhbHVlPScnOyBjbHIuY2xhc3NMaXN0LnJlbW92ZSgnc2hvdycpOwogIGRyb3AuY2xhc3NMaXN0LnJlbW92ZSgnb3BlbicpOyBkcm9wLmlubmVySFRNTD0nJzsKICBiYWRnZS5pbm5lckhUTUw9Jyc7CiAgdmFyIGJGaWVsZD1MQU5HPT09J2VuJz8nYnJlZWRfZW4nOkxBTkc9PT0nZXQnPydicmVlZF9ldCc6J2JyZWVkJzsKICB2YXIgZGlzcEJyZWVkPWJbYkZpZWxkXXx8Yi5icmVlZDsKICBib29raW5nLmJyZWVkRGlzcGxheT1kaXNwQnJlZWQ7CiAgdmFyIGJuPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NwYW4nKTtibi5jbGFzc05hbWU9J2JuYW1lJztibi50ZXh0Q29udGVudD1kaXNwQnJlZWQ7CiAgdmFyIGNoZ1R4dD1MQU5HPT09J2VuJz8nQ2hhbmdlJzpMQU5HPT09J2V0Jz8nTXV1ZGEnOifQmNC30LzQtdC90LjRgtGMJzsKICB2YXIgYmM9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnc3BhbicpO2JjLmNsYXNzTmFtZT0nYmNoZyc7YmMudGV4dENvbnRlbnQ9Y2hnVHh0OwogIGJjLm9uY2xpY2s9cmVzZXRCcmVlZDsKICBiYWRnZS5hcHBlbmRDaGlsZChibik7YmFkZ2UuYXBwZW5kQ2hpbGQoYmMpOwogIGJhZGdlLmNsYXNzTGlzdC5hZGQoJ3Nob3cnKTsKICByZW5kZXJTdmNzKGIpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdmNTZWMnKS5zdHlsZS5kaXNwbGF5PSdibG9jayc7CiAgICAvLyBBZGQgaW1wb3J0YW50IG5vdGUgaWYgbm90IGV4aXN0cwogICAgaWYoIWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdmNOb3RlJykpewogICAgICB2YXIgbm90ZT1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdkaXYnKTsKICAgICAgbm90ZS5pZD0nc3ZjTm90ZSc7CiAgICAgIG5vdGUuc3R5bGUuY3NzVGV4dD0nYm9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wOCk7cGFkZGluZzoxNHB4IDE2cHg7YmFja2dyb3VuZDpyZ2JhKDI1NSwyNTUsMjU1LC4wMik7bWFyZ2luLXRvcDoxMnB4Oyc7CiAgICAgIHZhciBub3RlVGl0bGU9TEFORz09PSdlbic/J1BsZWFzZSBub3RlJzpMQU5HPT09J2V0Jz8nUGFuZ2UgdMOkaGVsZSc6J9CS0LDQttC90L4g0LfQvdCw0YLRjCc7CiAgICAgIHZhciBub3RlQm9keT1MQU5HPT09J2VuJz8nRmluYWwgcHJpY2UgZGVwZW5kcyBvbiBjb2F0IGNvbmRpdGlvbiBhbmQgcGV0IGJlaGF2aW91ci48YnI+RGVtYXR0aW5nIGZyb20gNSDigqwuPGJyPkFnZ3Jlc3NpdmUgYmVoYXZpb3VyIHN1cmNoYXJnZSBtYXkgYXBwbHk6ICs1MCUuJzpMQU5HPT09J2V0Jz8nTMO1cGxpayBoaW5kIHPDtWx0dWIga2FydmFzdGlrdSBzZWlzdW5kaXN0IGphIGxlbW1pa2xvb21hIGvDpGl0dW1pc2VzdC48YnI+S29sdHN1bml0ZSBsYWh0aWhhcnV0YW1pbmUgYWxhdGVzIDUg4oKsLjxicj5BZ3Jlc3NpaXZzZSBrw6RpdHVtaXNlIGtvcnJhbCB2w7VpYiBsaXNhbmR1ZGEgNTAlIGp1dXJkZWhpbmRsdXMuJzon0J7QutC+0L3Rh9Cw0YLQtdC70YzQvdCw0Y8g0YHRgtC+0LjQvNC+0YHRgtGMINC30LDQstC40YHQuNGCINC+0YIg0YHQvtGB0YLQvtGP0L3QuNGPINGI0LXRgNGB0YLQuCDQuCDQv9C+0LLQtdC00LXQvdC40Y8g0L/QuNGC0L7QvNGG0LAuPGJyPtCg0LDQt9Cx0L7RgCDQutC+0LvRgtGD0L3QvtCyIOKAlCDQvtGCIDUg4oKsLjxicj7Qn9GA0Lgg0LDQs9GA0LXRgdGB0LjQstC90L7QvCDQv9C+0LLQtdC00LXQvdC40Lgg0LzQvtC20LXRgiDQv9GA0LjQvNC10L3Rj9GC0YzRgdGPINC00L7Qv9C70LDRgtCwIDUwJS4nOwogICAgICBub3RlLmlubmVySFRNTD0nPGRpdiBzdHlsZT0iZm9udC1zaXplOjAuNjdyZW07bGV0dGVyLXNwYWNpbmc6LjE1ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbTo4cHg7Zm9udC13ZWlnaHQ6NjAwO2ZvbnQtZmFtaWx5OlwnTW9udHNlcnJhdFwnLHNhbnMtc2VyaWYiPicrbm90ZVRpdGxlKyc8L2Rpdj48ZGl2IHN0eWxlPSJmb250LXNpemU6MC44MnJlbTtjb2xvcjojZmZmZmZmO2xpbmUtaGVpZ2h0OjEuODtmb250LWZhbWlseTpcJ01vbnRzZXJyYXRcJyxzYW5zLXNlcmlmIj4nK25vdGVCb2R5Kyc8L2Rpdj4nOwogICAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjU2VjJykuYXBwZW5kQ2hpbGQobm90ZSk7CiAgICB9CiAgZmlsdGVyTWFzdGVycygpOwp9CgpmdW5jdGlvbiByZXNldEJyZWVkKCl7CiAgc2VsQnJlZWQ9bnVsbDtib29raW5nLmJyZWVkPScnO2Jvb2tpbmcuc2VydmljZT0nJztib29raW5nLnByaWNlPTA7CiAgaW5wLnZhbHVlPScnO2Nsci5jbGFzc0xpc3QucmVtb3ZlKCdzaG93Jyk7CiAgYmFkZ2UuY2xhc3NMaXN0LnJlbW92ZSgnc2hvdycpO2JhZGdlLmlubmVySFRNTD0nJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjU2VjJykuc3R5bGUuZGlzcGxheT0nbm9uZSc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y0xpc3QnKS5pbm5lckhUTUw9Jyc7Cn0KCgp2YXIgU1ZDX1RSQU5TTEFUSU9OUyA9IHsKICAn0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQnOiAgICAgIHtlbjonQmFzaWMgZ3Jvb20nLCAgICAgIGV0OidQw7VoaWhvb2xkdXMnfSwKICAn0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQnOntlbjonSHlnaWVuZSBncm9vbScsICAgIGV0OidIw7xnaWVlbmlob29sZHVzJ30sCiAgJ9Ca0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQnOiAge2VuOidGdWxsIGdyb29tJywgICAgICAgIGV0OidUw6RpZWxpayBob29sZHVzJ30sCiAgJ9Ci0YDQuNC80LzQuNC90LMnOiAgICAgICAgICB7ZW46J1RyaW1taW5nJywgICAgICAgICAgZXQ6J1RyaW1tZXJpbWluZSd9LAogICfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6ICAge2VuOidFeHByZXNzIHNoZWQnLCAgICAgIGV0OidLaWlya2FydmF2YWhldHVzJ30sCiAgJ9CS0YvRh9C10YEnOiAgICAgICAgICAgICB7ZW46J0JydXNoLW91dCcsICAgICAgICAgZXQ6J0hhcmphbWluZSd9Cn07CnZhciBTVkNfVEFHTElORV9JMThOPXsKICBydTp7J9CS0YvRh9C10YEnOifQodGC0L7QuNC80L7RgdGC0Ywg0LfQsNCy0LjRgdC40YIg0L7RgiDRgdC+0YHRgtC+0Y/QvdC40Y8g0YjQtdGA0YHRgtC4INC4INC+0LHRitGR0LzQsCDRgNCw0LHQvtGCJywn0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQnOifQn9C+0LTRhdC+0LTQuNGCINC00LvRjyDQv9C+0LTQtNC10YDQttCw0L3QuNGPINGH0LjRgdGC0L7RgtGLINC80LXQttC00YMg0L/RgNC+0YbQtdC00YPRgNCw0LzQuCcsJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Jzon0JTQu9GPINC60L7QvNGE0L7RgNGC0LAg0Lgg0LDQutC60YPRgNCw0YLQvdC+0YHRgtC4INC/0LjRgtC+0LzRhtCwJywn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc6J9Cf0L7Qu9C90YvQuSDRg9GF0L7QtCDRgdC+INGB0YLRgNC40LbQutC+0LknLCfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6J9Cf0L7QvNC+0LPQsNC10YIg0YPQvNC10L3RjNGI0LjRgtGMINC60L7Qu9C40YfQtdGB0YLQstC+INC70LjQvdGP0Y7RidC10Lkg0YjQtdGA0YHRgtC4Jywn0KLRgNC40LzQvNC40L3Qsyc6J9CU0LvRjyDQttC10YHRgtC60L7RiNC10YDRgdGC0L3Ri9GFINC/0L7RgNC+0LQnfSwKICBlbjp7J9CS0YvRh9C10YEnOidQcmljZSBkZXBlbmRzIG9uIGNvYXQgY29uZGl0aW9uIGFuZCB2b2x1bWUgb2Ygd29yaycsJ9CR0LDQt9C+0LLRi9C5INGD0YXQvtC0JzonSWRlYWwgZm9yIG1haW50YWluaW5nIGNsZWFubGluZXNzIGJldHdlZW4gZnVsbCBncm9vbXMnLCfQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCc6J0ZvciB5b3VyIHBldFwncyBjb21mb3J0IGFuZCBuZWF0bmVzcycsJ9Ca0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQnOidGdWxsIGdyb29taW5nIHdpdGggaGFpcmN1dCcsJ9Ct0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwJzonU2lnbmlmaWNhbnRseSByZWR1Y2VzIHNoZWRkaW5nJywn0KLRgNC40LzQvNC40L3Qsyc6J0ZvciB3aXJlLWhhaXJlZCBicmVlZHMnfSwKICBldDp7J9CS0YvRh9C10YEnOidIaW5kIHPDtWx0dWIga2FydmFzdGlrdSBzZWlzdW5kaXN0IGphIHTDtsO2bWFodXN0Jywn0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQnOidTb2JpYiBwdWh0dXNlIGhvaWRtaXNla3MgcHJvdHNlZHV1cmlkZSB2YWhlbCcsJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0JzonTGVtbWlrbG9vbWEgbXVnYXZ1c2VrcyBqYSBrb3JyYXNob2l1a3MnLCfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0JzonVMOkaWVsaWsgaG9vbGR1cyBrb29zIGzDtWlrdXNlZ2EnLCfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6J1bDpGhlbmRhYiBvbHVsaXNlbHQga2FydmFkZSBsYW5nZW1pc3QnLCfQotGA0LjQvNC80LjQvdCzJzonVHJhYXRrYXJ2YWxpc3RlbGUgdMO1dWd1ZGVsZSd9Cn07CnZhciBTVkNfREVTQ19JMThOPXsKICBydTp7J9CS0YvRh9C10YEnOifQp9C40YHRgtC60LAg0LPQu9Cw0LcsINGD0YjQtdC5LCDQv9C+0LTRgdGC0YDQuNCz0LDQvdC40LUg0LrQvtCz0YLQtdC5LCDQstGL0YfRkdGBICjQtNC70Y8g0LrQvtGI0LXQuiknLCfQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCc6J9Cc0YvRgtGM0ZEg0L/RgNC+0YTQtdGB0YHQuNC+0L3QsNC70YzQvdGL0LzQuCDRgdGA0LXQtNGB0YLQstCw0LzQuCwg0LTQtdC70LjQutCw0YLQvdCw0Y8g0YHRg9GI0LrQsCcsJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0Jzon0KHRgtGA0LjQttC60LAg0LrQvtCz0YLQtdC5LCDRh9C40YHRgtC60LAg0YPRiNC10Lkg0Lgg0LPQu9Cw0LcsINC60YPQv9Cw0L3QuNC1LCDRgdGD0YjQutCwLCDRg9GF0L7QtCDQt9CwINC70LDQv9C60LDQvNC4INC4INGH0YPQstGB0YLQstC40YLQtdC70YzQvdGL0LzQuCDQt9C+0L3QsNC80LgnLCfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0Jzon0KHRgtGA0LjQttC60LAg0LrQvtCz0YLQtdC5LCDRh9C40YHRgtC60LAg0YPRiNC10Lkg0Lgg0LPQu9Cw0LcsINC60YPQv9Cw0L3QuNC1LCDRgdGD0YjQutCwLCDRg9GF0L7QtCDQt9CwINC70LDQv9C60LDQvNC4INC4INGH0YPQstGB0YLQstC40YLQtdC70YzQvdGL0LzQuCDQt9C+0L3QsNC80LgsINC80L7QtNC10LvRjNC90LDRjyDRgdGC0YDQuNC20LrQsCcsJ9Ct0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwJzon0JzRi9GC0YzRkSwg0YHRg9GI0LrQsCwg0YPRhdC+0LQg0LfQsCDRiNC10YDRgdGC0YzRjiwg0LzQsNGB0LrQsCwg0L/QvtC00YHRgtGA0LjQs9Cw0L3QuNC1INC60L7Qs9GC0LXQuSwg0YfQuNGB0YLQutCwINGD0YjQtdC5INC4INCz0LvQsNC3LCDRg9GF0L7QtCDQt9CwINC70LDQv9Cw0LzQuCDQuCDQt9C+0L3QsNC80Lgg0YLRgNC10LHRg9GO0YnQuNC80Lgg0L7RgdC+0LHQvtCz0L4g0LLQvdC40LzQsNC90LjRjycsJ9Ci0YDQuNC80LzQuNC90LMnOifQktGL0YnQuNC/0YvQstCw0L3QuNC1INGB0YLQsNGA0L7Qs9C+INGB0LvQvtGPINGI0LXRgNGB0YLQuCwg0LzRi9GC0YzRkSwg0YHRg9GI0LrQsCwg0YHRgtGA0LjQttC60LAg0LrQvtCz0YLQtdC5LCDRh9C40YHRgtC60LAg0YPRiNC10Lkg0Lgg0LPQu9Cw0LcsINC+0YTQvtGA0LzQu9C10L3QuNC1INGI0LXRgNGB0YLQuCd9LAogIGVuOnsn0JLRi9GH0LXRgSc6J0V5ZSBhbmQgZWFyIGNsZWFuaW5nLCBuYWlsIHRyaW1taW5nLCBicnVzaGluZyAoZm9yIGNhdHMpJywn0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQnOidXYXNoaW5nIHdpdGggcHJvZmVzc2lvbmFsIHByb2R1Y3RzLCBnZW50bGUgZHJ5aW5nJywn0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQnOidOYWlsIHRyaW1taW5nLCBlYXIgYW5kIGV5ZSBjbGVhbmluZywgYmF0aGluZywgZHJ5aW5nLCBwYXcgYW5kIHNlbnNpdGl2ZSBhcmVhIGNhcmUnLCfQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0JzonTmFpbCB0cmltbWluZywgZWFyIGFuZCBleWUgY2xlYW5pbmcsIGJhdGhpbmcsIGRyeWluZywgcGF3IGFuZCBzZW5zaXRpdmUgYXJlYSBjYXJlLCBzdHlsaW5nIGhhaXJjdXQnLCfQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCc6J1dhc2hpbmcsIGRyeWluZywgY29hdCBjYXJlLCBtYXNrLCBuYWlsIHRyaW1taW5nLCBlYXIgYW5kIGV5ZSBjbGVhbmluZywgcGF3IGFuZCBzcGVjaWFsIGFyZWEgY2FyZScsJ9Ci0YDQuNC80LzQuNC90LMnOidSZW1vdmluZyBvbGQgY29hdCBsYXllciwgd2FzaGluZywgZHJ5aW5nLCBuYWlsIHRyaW1taW5nLCBlYXIgYW5kIGV5ZSBjbGVhbmluZywgY29hdCBzdHlsaW5nJ30sCiAgZXQ6eyfQktGL0YfQtdGBJzonU2lsbWFkZSBqYSBrw7VydmFkZSBwdWhhc3RhbWluZSwga8O8w7xudGUgbMO1aWthbWluZSwgaGFyamFtaW5lIChrYXNzaWRlbGUpJywn0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQnOidQZXNlbWluZSBwcm9mZXNzaW9uYWFsc2V0ZSB2YWhlbmRpdGVnYSwgw7VybiBrdWl2YXRhbWluZScsJ9CT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0JzonS8O8w7xudGUgbMO1aWthbWluZSwga8O1cnZhZGUgamEgc2lsbWFkZSBwdWhhc3RhbWluZSwgcGVzZW1pbmUsIGt1aXZhdGFtaW5lLCBrw6RwcGFkZSBqYSB0dW5kbGlrZSBwaWlya29uZGFkZSBob29sZHVzJywn0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCc6J0vDvMO8bnRlIGzDtWlrYW1pbmUsIGvDtXJ2YWRlIGphIHNpbG1hZGUgcHVoYXN0YW1pbmUsIHBlc2VtaW5lLCBrdWl2YXRhbWluZSwga8OkcHBhZGUgamEgdHVuZGxpa2UgcGlpcmtvbmRhZGUgaG9vbGR1cywgbW9kZWxsw7Vpa3VzJywn0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAnOidQZXNlbWluZSwga3VpdmF0YW1pbmUsIGthcnZhc3Rpa3UgaG9vbGR1cywgbWFzaywga8O8w7xudGUgbMO1aWthbWluZSwga8O1cnZhZGUgamEgc2lsbWFkZSBwdWhhc3RhbWluZSwga8OkcHBhZGUgamEgZXJpbGlzdGUgcGlpcmtvbmRhZGUgaG9vbGR1cycsJ9Ci0YDQuNC80LzQuNC90LMnOidWYW5hIGthcnZha2loaSBlZW1hbGRhbWluZSwgcGVzZW1pbmUsIGt1aXZhdGFtaW5lLCBrw7zDvG50ZSBsw7Vpa2FtaW5lLCBrw7VydmFkZSBqYSBzaWxtYWRlIHB1aGFzdGFtaW5lLCBrYXJ2YXN0aWt1IGt1anVuZGFtaW5lJ30KfTsKZnVuY3Rpb24gZ2V0U3ZjVGFnKG5hbWUpe3JldHVybihTVkNfVEFHTElORV9JMThOW0xBTkddJiZTVkNfVEFHTElORV9JMThOW0xBTkddW25hbWVdKXx8U1ZDX1RBR0xJTkVfSTE4Ti5ydVtuYW1lXXx8Jyc7fQpmdW5jdGlvbiBnZXRTdmNEZXNjKG5hbWUpe3JldHVybihTVkNfREVTQ19JMThOW0xBTkddJiZTVkNfREVTQ19JMThOW0xBTkddW25hbWVdKXx8U1ZDX0RFU0NfSTE4Ti5ydVtuYW1lXXx8Jyc7fQoKZnVuY3Rpb24gcmVuZGVyU3ZjcyhiKXsKICB2YXIgbGlzdD1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjTGlzdCcpO2xpc3QuaW5uZXJIVE1MPScnOwogIE9iamVjdC5lbnRyaWVzKGIuc2VydmljZXMpLmZvckVhY2goZnVuY3Rpb24oa3YpewogICAgdmFyIG5hbWU9a3ZbMF0scHJpY2U9a3ZbMV07CgogICAgdmFyIGRpc3BsYXlOYW1lPShMQU5HIT09J3J1JyYmU1ZDX1RSQU5TTEFUSU9OU1tuYW1lXSk/U1ZDX1RSQU5TTEFUSU9OU1tuYW1lXVtMQU5HXTpuYW1lOwogICAgdmFyIGJ0bj1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdidXR0b24nKTtidG4uY2xhc3NOYW1lPSdzdmJ0bic7CiAgICB2YXIgcm93PWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO3Jvdy5jbGFzc05hbWU9J3N2YnRuLXJvdyc7CiAgICB2YXIgbnM9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnc3BhbicpO25zLmNsYXNzTmFtZT0nc3ZidG4tbmFtZSc7bnMudGV4dENvbnRlbnQ9ZGlzcGxheU5hbWU7CiAgICB2YXIgcHM9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnc3BhbicpO3BzLmNsYXNzTmFtZT0nc3ZidG4tcHJpY2UnO3BzLnRleHRDb250ZW50PXByaWNlKycg4oKsJzsKICAgIHJvdy5hcHBlbmRDaGlsZChucyk7cm93LmFwcGVuZENoaWxkKHBzKTsKICAgIGJ0bi5hcHBlbmRDaGlsZChyb3cpOwogICAgdmFyIGRlc2M9Z2V0U3ZjRGVzYyhuYW1lKTsKICAgIGlmKGRlc2Mpe3ZhciBkcz1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdzcGFuJyk7ZHMuY2xhc3NOYW1lPSdzdmJ0bi1kZXNjJztkcy50ZXh0Q29udGVudD1kZXNjO2J0bi5hcHBlbmRDaGlsZChkcyk7fQogICAgdmFyIHRhZz1nZXRTdmNUYWcobmFtZSk7CiAgICBpZih0YWcpe3ZhciB0cz1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdzcGFuJyk7dHMuY2xhc3NOYW1lPSdzdmJ0bi10YWcnO3RzLnRleHRDb250ZW50PXRhZztidG4uYXBwZW5kQ2hpbGQodHMpO30KICAgIGJ0bi5vbmNsaWNrPWZ1bmN0aW9uKCl7CiAgICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5zdmJ0bicpLmZvckVhY2goZnVuY3Rpb24oYil7Yi5jbGFzc0xpc3QucmVtb3ZlKCdhY3RpdmUnKTt9KTsKICAgICAgYnRuLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpOwogICAgICBib29raW5nLnNlcnZpY2U9bmFtZTtib29raW5nLnByaWNlPXByaWNlOwogICAgICBmaWx0ZXJNYXN0ZXJzKCk7CiAgICAgIHNldFRpbWVvdXQoZnVuY3Rpb24oKXtnb1N0ZXAoMik7fSwzMDApOwogICAgfTsKICAgIGxpc3QuYXBwZW5kQ2hpbGQoYnRuKTsKICB9KTsKfQoKLy8gTWFzdGVycwpmdW5jdGlvbiBmaWx0ZXJNYXN0ZXJzKCl7CiAgdmFyIGlzQ2F0ID0gYm9va2luZy5icmVlZCAmJiBib29raW5nLmJyZWVkLmluZGV4T2YoJ9Ca0L7RiNC60LAnKSA9PT0gMDsKICB2YXIgYnJlZWQgPSBib29raW5nLmJyZWVkIHx8ICcnOwogIHZhciBhbm5hRXhjbHVkZSA9IFsn0JzQsNC70YzRgtC40L/RgycsJ9Cf0YPQtNC10LvRjCcsJ9CZ0L7RgNC6Jywn0JHQuNGI0L7QvScsJ9CR0L7Qu9C+0L3QutCwJywn0JzQsNC70YzRgtC40LnRgdC60LDRjyddOwogIHZhciBpc0FubmFCcmVlZCA9IGJyZWVkICYmICFhbm5hRXhjbHVkZS5zb21lKGZ1bmN0aW9uKGIpeyByZXR1cm4gYnJlZWQuaW5kZXhPZihiKSAhPT0gLTE7IH0pOwogIHZhciBhbGV4YW5kcmFFeGNsdWRlID0gWyfQpNC+0LrRgdGC0LXRgNGM0LXRgCcsJ9Cm0LLQtdGA0LPRiNC90LDRg9GG0LXRgCddOwogIHZhciBpc0FsZXhhbmRyYUJyZWVkID0gIWFsZXhhbmRyYUV4Y2x1ZGUuc29tZShmdW5jdGlvbihiKXsgcmV0dXJuIGJyZWVkLmluZGV4T2YoYikgIT09IC0xOyB9KTsKICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcubWJ0bicpLmZvckVhY2goZnVuY3Rpb24oYnRuKXsKICAgIHZhciBtYXN0ZXIgPSBidG4uZ2V0QXR0cmlidXRlKCdkYXRhLW1hc3RlcicpOwogICAgdmFyIGlzVHJpbW1pbmcgPSBib29raW5nLnNlcnZpY2UgPT09ICfQotGA0LjQvNC80LjQvdCzJzsKICAgIGlmKG1hc3RlciA9PT0gJ9CQ0LvQuNGB0LAnKXsKICAgICAgYnRuLnN0eWxlLmRpc3BsYXkgPSBpc0NhdCA/ICcnIDogJ25vbmUnOwogICAgfSBlbHNlIGlmKG1hc3RlciA9PT0gJ9CQ0L3QvdCwJyl7CiAgICAgIGJ0bi5zdHlsZS5kaXNwbGF5ID0gaXNBbm5hQnJlZWQgPyAnJyA6ICdub25lJzsKICAgIH0gZWxzZSBpZihtYXN0ZXIgPT09ICfQkNC70LXQutGB0LDQvdC00YDQsCcpewogICAgICBidG4uc3R5bGUuZGlzcGxheSA9IChpc0FsZXhhbmRyYUJyZWVkICYmICFpc1RyaW1taW5nKSA/ICcnIDogJ25vbmUnOwogICAgfSBlbHNlIGlmKGlzVHJpbW1pbmcpewogICAgICBidG4uc3R5bGUuZGlzcGxheSA9ICdub25lJzsKICAgIH0gZWxzZSB7CiAgICAgIGJ0bi5zdHlsZS5kaXNwbGF5ID0gJyc7CiAgICB9CiAgfSk7Cn0KCmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5tYnRuJykuZm9yRWFjaChmdW5jdGlvbihidG4pewogIGJ0bi5vbmNsaWNrPWZ1bmN0aW9uKCl7CiAgICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcubWJ0bicpLmZvckVhY2goZnVuY3Rpb24oYil7Yi5jbGFzc0xpc3QucmVtb3ZlKCdhY3RpdmUnKTt9KTsKICAgIGJ0bi5jbGFzc0xpc3QuYWRkKCdhY3RpdmUnKTsKICAgIGJvb2tpbmcubWFzdGVyPWJ0bi5nZXRBdHRyaWJ1dGUoJ2RhdGEtbWFzdGVyJyk7CiAgICBzZXRUaW1lb3V0KGZ1bmN0aW9uKCl7Z29TdGVwKDMpO30sMzAwKTsKICB9Owp9KTsKCi8vIEdyb29tIGhpc3RvcnkKZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmdidG4nKS5mb3JFYWNoKGZ1bmN0aW9uKGJ0bil7CiAgYnRuLm9uY2xpY2s9ZnVuY3Rpb24oKXsKICAgIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5nYnRuJykuZm9yRWFjaChmdW5jdGlvbihiKXtiLmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpO30pOwogICAgYnRuLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpOwogICAgYm9va2luZy5ncm9vbUhpc3Rvcnk9YnRuLmdldEF0dHJpYnV0ZSgnZGF0YS12YWwnKTsKICAgIHNldFRpbWVvdXQoZnVuY3Rpb24oKXtnb1N0ZXAoNCk7YnVpbGRDYWwoKTt9LDMwMCk7CiAgfTsKfSk7CgovLyBDYWxlbmRhcgpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgncHJldk0nKS5vbmNsaWNrPWZ1bmN0aW9uKCl7Y00tLTtpZihjTTwwKXtjTT0xMTtjWS0tO31idWlsZENhbCgpO307CmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCduZXh0TScpLm9uY2xpY2s9ZnVuY3Rpb24oKXtjTSsrO2lmKGNNPjExKXtjTT0wO2NZKys7fWJ1aWxkQ2FsKCk7fTsKCnZhciBhdmFpbGFibGVEYXlzID0gW107CgpmdW5jdGlvbiBsb2FkQXZhaWxhYmxlRGF5cygpIHsKICB2YXIgbWFzdGVyID0gYm9va2luZy5tYXN0ZXI7CiAgaWYgKCFtYXN0ZXIpIHJldHVybjsKICBhdmFpbGFibGVEYXlzID0gW107CiAgZmV0Y2god2luZG93LmxvY2F0aW9uLm9yaWdpbiArICcvYXBpL2F2YWlsYWJsZV9kYXlzP21vbnRoPScgKyAoY00rMSkgKyAnJnllYXI9JyArIGNZICsgJyZtYXN0ZXI9JyArIGVuY29kZVVSSUNvbXBvbmVudChtYXN0ZXIpKQogICAgLnRoZW4oZnVuY3Rpb24ocil7IHJldHVybiByLmpzb24oKTsgfSkKICAgIC50aGVuKGZ1bmN0aW9uKGRhdGEpewogICAgICBhdmFpbGFibGVEYXlzID0gZGF0YS5hdmFpbGFibGUgfHwgW107CiAgICAgIG1hcmtBdmFpbGFibGVEYXlzKCk7CiAgICB9KQogICAgLmNhdGNoKGZ1bmN0aW9uKCl7IGF2YWlsYWJsZURheXMgPSBbXTsgfSk7Cn0KCmZ1bmN0aW9uIG1hcmtBdmFpbGFibGVEYXlzKCkgewogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5jZCcpLmZvckVhY2goZnVuY3Rpb24oYyl7aWYoIWMuY2xhc3NMaXN0LmNvbnRhaW5zKCdkaXMnKSljLmNsYXNzTGlzdC5yZW1vdmUoJ3NlbCcpO30pOwogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5jZDpub3QoLmRpcyk6bm90KC5jZG4pOm5vdCgucGFkKScpLmZvckVhY2goZnVuY3Rpb24oZWwpIHsKICAgIHZhciBkYXkgPSBlbC50ZXh0Q29udGVudC50cmltKCk7CiAgICBpZiAoIWRheSB8fCBpc05hTihwYXJzZUludChkYXkpKSkgcmV0dXJuOwogICAgdmFyIGRhdGVTdHIgPSBTdHJpbmcocGFyc2VJbnQoZGF5KSkucGFkU3RhcnQoMiwnMCcpICsgJy4nICsgU3RyaW5nKGNNKzEpLnBhZFN0YXJ0KDIsJzAnKSArICcuJyArIGNZOwogICAgaWYgKGF2YWlsYWJsZURheXMuaW5kZXhPZihkYXRlU3RyKSAhPT0gLTEpIHsKICAgICAgZWwuY2xhc3NMaXN0LmFkZCgnYXZhaWwnKTsKICAgICAgZWwuY2xhc3NMaXN0LnJlbW92ZSgnYnVzeScpOwogICAgfSBlbHNlIHsKICAgICAgZWwuY2xhc3NMaXN0LmFkZCgnYnVzeScpOwogICAgICBlbC5jbGFzc0xpc3QucmVtb3ZlKCdhdmFpbCcpOwogICAgfQogIH0pOwp9CgpmdW5jdGlvbiBidWlsZENhbCgpewogIGxvYWRBdmFpbGFibGVEYXlzKCk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NhbE0nKS50ZXh0Q29udGVudD1NT05USFNbY01dKycgJytjWTsKICBib29raW5nLmRhdGU9Jyc7IGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5jZCcpLmZvckVhY2goZnVuY3Rpb24oYyl7Yy5jbGFzc0xpc3QucmVtb3ZlKCdzZWwnKTtjLmNsYXNzTGlzdC5yZW1vdmUoJ2F2YWlsJyk7Yy5jbGFzc0xpc3QucmVtb3ZlKCdidXN5Jyk7fSk7CiAgdmFyIGc9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NhbEcnKTtnLmlubmVySFRNTD0nJzsKICBbJ9Cf0L0nLCfQktGCJywn0KHRgCcsJ9Cn0YInLCfQn9GCJywn0KHQsScsJ9CS0YEnXS5mb3JFYWNoKGZ1bmN0aW9uKGQpewogICAgdmFyIGVsPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO2VsLmNsYXNzTmFtZT0nY2RuJztlbC50ZXh0Q29udGVudD1kO2cuYXBwZW5kQ2hpbGQoZWwpOwogIH0pOwogIHZhciBmaXJzdD1uZXcgRGF0ZShjWSxjTSwxKS5nZXREYXkoKTsKICB2YXIgZGF5cz1uZXcgRGF0ZShjWSxjTSsxLDApLmdldERhdGUoKTsKICB2YXIgc3RhcnQ9Zmlyc3Q9PT0wPzY6Zmlyc3QtMTsKICB2YXIgdG9kYXk9bmV3IERhdGUoKTsKICBmb3IodmFyIGk9MDtpPHN0YXJ0O2krKyl7dmFyIGVsPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO2VsLmNsYXNzTmFtZT0nY2QgcGFkJztnLmFwcGVuZENoaWxkKGVsKTt9CiAgZm9yKHZhciBkYXk9MTtkYXk8PWRheXM7ZGF5KyspewogICAgdmFyIGVsPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO2VsLmNsYXNzTmFtZT0nY2QnOwogICAgdmFyIGRhdGU9bmV3IERhdGUoY1ksY00sZGF5KTsKICAgIHZhciBpc1Bhc3Q9ZGF0ZTxuZXcgRGF0ZSh0b2RheS5nZXRGdWxsWWVhcigpLHRvZGF5LmdldE1vbnRoKCksdG9kYXkuZ2V0RGF0ZSgpKTsKICAgIHZhciBpbm5lcj1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdkaXYnKTtpbm5lci5jbGFzc05hbWU9J2NkLWlubmVyJztpbm5lci50ZXh0Q29udGVudD1kYXk7ZWwuYXBwZW5kQ2hpbGQoaW5uZXIpOwogICAgaWYoaXNQYXN0KXtlbC5jbGFzc0xpc3QuYWRkKCdkaXMnKTt9CiAgICBlbHNlewogICAgICBpZihkYXRlLnRvRGF0ZVN0cmluZygpPT09dG9kYXkudG9EYXRlU3RyaW5nKCkpZWwuY2xhc3NMaXN0LmFkZCgndG9kJyk7CiAgICAgIChmdW5jdGlvbihkLCBlbFJlZil7CiAgICAgICAgZWxSZWYub25jbGljaz1mdW5jdGlvbigpewogICAgICAgICAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmNkJykuZm9yRWFjaChmdW5jdGlvbihjKXtjLmNsYXNzTGlzdC5yZW1vdmUoJ3NlbCcpO30pOwogICAgICAgICAgZWxSZWYuY2xhc3NMaXN0LmFkZCgnc2VsJyk7CiAgICAgICAgICBib29raW5nLmRhdGU9U3RyaW5nKGQpLnBhZFN0YXJ0KDIsJzAnKSsnLicrU3RyaW5nKGNNKzEpLnBhZFN0YXJ0KDIsJzAnKSsnLicrY1k7CiAgICAgICAgICBzaG93VGltZXMoKTsKICAgICAgICB9OwogICAgICB9KShkYXksIGVsKTsKICAgIH0KICAgIGcuYXBwZW5kQ2hpbGQoZWwpOwogIH0KICAvLyBmaWxsIHRyYWlsaW5nIGNlbGxzIHRvIGNvbXBsZXRlIGxhc3QgZ3JpZCByb3cKICB2YXIgdG90YWwgPSBzdGFydCArIGRheXM7CiAgdmFyIHRyYWlsID0gKDcgLSAodG90YWwgJSA3KSkgJSA3OwogIGZvcih2YXIgdD0wO3Q8dHJhaWw7dCsrKXt2YXIgZXA9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7ZXAuY2xhc3NOYW1lPSdjZCBwYWQnO2cuYXBwZW5kQ2hpbGQoZXApO30KfQoKZnVuY3Rpb24gc2hvd1RpbWVzKCl7CiAgdmFyIHRnPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCd0aW1lRycpOwogIHRnLmlubmVySFRNTD0nPGRpdiBjbGFzcz0ibG9hZGluZy1zbG90cyI+4o+zINCX0LDQs9GA0YPQttCw0LXQvCDRgNCw0YHQv9C40YHQsNC90LjQtS4uLjwvZGl2Pic7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3RpbWVTZWMnKS5zdHlsZS5kaXNwbGF5PSdibG9jayc7CgogIHZhciB1cmwgPSB3aW5kb3cubG9jYXRpb24ub3JpZ2luICsgIi9hcGkvc2xvdHMiICsgJz9hY3Rpb249c2xvdHMmZGF0ZT0nICsgZW5jb2RlVVJJQ29tcG9uZW50KGJvb2tpbmcuZGF0ZSkgKyAnJm1hc3Rlcj0nICsgZW5jb2RlVVJJQ29tcG9uZW50KGJvb2tpbmcubWFzdGVyKTsKCiAgZmV0Y2godXJsKQogICAgLnRoZW4oZnVuY3Rpb24ocil7cmV0dXJuIHIuanNvbigpO30pCiAgICAudGhlbihmdW5jdGlvbihkYXRhKXsKICAgICAgdmFyIHNsb3RzID0gKGRhdGEuc2xvdHMgJiYgZGF0YS5zbG90cy5sZW5ndGggPiAwKSA/IGRhdGEuc2xvdHMgOiBbXTsKICAgICAgcmVuZGVyVGltZVNsb3RzKHNsb3RzKTsKICAgIH0pCiAgICAuY2F0Y2goZnVuY3Rpb24oKXsKICAgICAgcmVuZGVyVGltZVNsb3RzKFtdKTsKICAgIH0pOwp9CgpmdW5jdGlvbiByZW5kZXJUaW1lU2xvdHMoc2xvdHMpewogIHZhciB0Zz1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgndGltZUcnKTt0Zy5pbm5lckhUTUw9Jyc7CiAgaWYoc2xvdHMubGVuZ3RoPT09MCl7CiAgICB0Zy5pbm5lckhUTUw9JzxkaXYgY2xhc3M9ImxvYWRpbmctc2xvdHMiPtCd0LXRgiDQtNC+0YHRgtGD0L/QvdGL0YUg0YHQu9C+0YLQvtCyINC90LAg0Y3RgtGDINC00LDRgtGDPC9kaXY+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyIiBvbmNsaWNrPSJzaG93U2NyZWVuKFwnaG9tZVNjcmVlblwnKSIgc3R5bGU9Im1hcmdpbi10b3A6OHB4OyI+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLWljb24iPvCfkL48L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItdGV4dCI+PGRpdiBjbGFzcz0ibm8tYnJlZWQtYmFubmVyLXRpdGxlIj7QndC1INC90LDRiNC70Lgg0L/QvtC00YXQvtC00Y/RidC10LUg0LLRgNC10LzRjz88L2Rpdj48ZGl2IGNsYXNzPSJuby1icmVlZC1iYW5uZXItc3ViIj7QodCy0Y/QttC40YLQtdGB0Ywg0YEg0L3QsNC80Lgg0LvRjtCx0YvQvCDRg9C00L7QsdC90YvQvCDRgdC/0L7RgdC+0LHQvtC8IOKAlCDQvNGLINC/0L7QtNCx0LXRgNGR0Lwg0YPQtNC+0LHQvdC+0LUg0LLRgNC10LzRjzwvZGl2PjwvZGl2PjxkaXYgY2xhc3M9Im5vLWJyZWVkLWJhbm5lci1hcnJvdyI+4oaSPC9kaXY+PC9kaXY+JzsKICAgIHJldHVybjsKICB9CiAgc2xvdHMuZm9yRWFjaChmdW5jdGlvbih0KXsKICAgIHZhciBidG49ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnYnV0dG9uJyk7YnRuLmNsYXNzTmFtZT0ndGJ0bic7YnRuLnRleHRDb250ZW50PXQ7CiAgICBidG4ub25jbGljaz1mdW5jdGlvbigpewogICAgICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcudGJ0bicpLmZvckVhY2goZnVuY3Rpb24oYil7Yi5jbGFzc0xpc3QucmVtb3ZlKCdhY3RpdmUnKTt9KTsKICAgICAgYnRuLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpO2Jvb2tpbmcudGltZT10OwogICAgICBzZXRUaW1lb3V0KGZ1bmN0aW9uKCl7Z29TdGVwKDUpO2J1aWxkU3VtKCk7fSwzMDApOwogICAgfTsKICAgIHRnLmFwcGVuZENoaWxkKGJ0bik7CiAgfSk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3RpbWVTZWMnKS5zY3JvbGxJbnRvVmlldyh7YmVoYXZpb3I6J3Ntb290aCcsYmxvY2s6J25lYXJlc3QnfSk7Cn0KCmZ1bmN0aW9uIGJ1aWxkU3VtKCl7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N1bUJsb2NrJykuaW5uZXJIVE1MPQogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fYnJlZWQrJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrKGJvb2tpbmcuYnJlZWREaXNwbGF5fHxib29raW5nLmJyZWVkKSsnPC9zcGFuPjwvZGl2PicrCiAgICAnPGRpdiBjbGFzcz0ic3IiPjxzcGFuIGNsYXNzPSJzbCI+JytUW0xBTkddLnN1bV9zZXJ2aWNlKyc8L3NwYW4+PHNwYW4gY2xhc3M9InN2Ij4nKygoTEFORyE9PSdydScmJlNWQ19UUkFOU0xBVElPTlNbYm9va2luZy5zZXJ2aWNlXSk/U1ZDX1RSQU5TTEFUSU9OU1tib29raW5nLnNlcnZpY2VdW0xBTkddOmJvb2tpbmcuc2VydmljZSkrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPicrVFtMQU5HXS5zdW1fbWFzdGVyKyc8L3NwYW4+PHNwYW4gY2xhc3M9InN2Ij4nK2Jvb2tpbmcubWFzdGVyKyc8L3NwYW4+PC9kaXY+JysKICAgICc8ZGl2IGNsYXNzPSJzciI+PHNwYW4gY2xhc3M9InNsIj4nK1RbTEFOR10uc3VtX2dyb29tKyc8L3NwYW4+PHNwYW4gY2xhc3M9InN2Ij4nK2Jvb2tpbmcuZ3Jvb21IaXN0b3J5Kyc8L3NwYW4+PC9kaXY+JysKICAgICc8ZGl2IGNsYXNzPSJzciI+PHNwYW4gY2xhc3M9InNsIj4nK1RbTEFOR10uc3VtX2RhdGUrJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrYm9va2luZy5kYXRlKyc8L3NwYW4+PC9kaXY+JysKICAgICc8ZGl2IGNsYXNzPSJzciI+PHNwYW4gY2xhc3M9InNsIj4nK1RbTEFOR10uc3VtX3RpbWUrJzwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrYm9va2luZy50aW1lKyc8L3NwYW4+PC9kaXY+JysKICAgICc8ZGl2IGNsYXNzPSJzciI+PHNwYW4gY2xhc3M9InNsIj4nK1RbTEFOR10uc3VtX3ByaWNlKyc8L3NwYW4+PHNwYW4gY2xhc3M9InNwIj4nK2Jvb2tpbmcucHJpY2UrJyDigqw8L3NwYW4+PC9kaXY+JzsKfQoKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NvbmZpcm1CdG4nKS5vbmNsaWNrID0gZnVuY3Rpb24oKXsKICB2YXIgbmFtZT1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY05hbWUnKS52YWx1ZTsKICB2YXIgcGhvbmU9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NQaG9uZScpLnZhbHVlOwogIGlmKCFuYW1lfHwhcGhvbmUpe2FsZXJ0KFRbTEFOR10uYWxlcnRfZmlsbCk7cmV0dXJuO30KICBpZighL15cK1xkezEwLH0kLy50ZXN0KHBob25lLnRyaW0oKSkpe2FsZXJ0KFRbTEFOR10uYWxlcnRfcGhvbmUpO3JldHVybjt9CiAgYm9va2luZy5uYW1lPW5hbWU7IGJvb2tpbmcucGhvbmU9cGhvbmU7IGJvb2tpbmcuZW1haWw9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NFbWFpbCcpLnZhbHVlOyBib29raW5nLnBldD1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY1BldCcpLnZhbHVlOyBib29raW5nLmxhbmc9TEFORzsKICBib29raW5nLmR1cmF0aW9uID0gYm9va2luZy5icmVlZCAmJiBib29raW5nLmJyZWVkLmluZGV4T2YoJ9Ca0L7RiNC60LAnKSA9PT0gMCA/IDEyMCA6IDE4MDsKICB2YXIgYnRuPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjb25maXJtQnRuJyk7CiAgYnRuLnRleHRDb250ZW50PVRbTEFOR10uc2VuZGluZzsgYnRuLmRpc2FibGVkPXRydWU7CiAgZmV0Y2goUkFJTFdBWSwgewogICAgbWV0aG9kOidQT1NUJywKICAgIGhlYWRlcnM6eydDb250ZW50LVR5cGUnOidhcHBsaWNhdGlvbi9qc29uJ30sCiAgICBib2R5OkpTT04uc3RyaW5naWZ5KGJvb2tpbmcpCiAgfSkudGhlbihmdW5jdGlvbigpe3Nob3dTdWNjZXNzKCk7fSkuY2F0Y2goZnVuY3Rpb24oKXtzaG93U3VjY2VzcygpO30pOwp9OwoKZnVuY3Rpb24gc2hvd1N1Y2Nlc3MoKXsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnYms1JykuY2xhc3NOYW1lPSdzdGVwJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3VjQmxvY2snKS5jbGFzc0xpc3QuYWRkKCdzaG93Jyk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3Byb2dyZXNzJykuc3R5bGUuZGlzcGxheT0nbm9uZSc7Cn0KCmZ1bmN0aW9uIHJlc2V0QWxsKCl7CiAgYm9va2luZz17YnJlZWQ6JycsYnJlZWREaXNwbGF5OicnLHNlcnZpY2U6JycscHJpY2U6MCxtYXN0ZXI6JycsZ3Jvb21IaXN0b3J5OicnLGRhdGU6JycsdGltZTonJyxsYW5nOidydSd9OwogIHNlbEJyZWVkPW51bGw7IGlucC52YWx1ZT0nJzsgY2xyLmNsYXNzTGlzdC5yZW1vdmUoJ3Nob3cnKTsKICBiYWRnZS5jbGFzc0xpc3QucmVtb3ZlKCdzaG93Jyk7IGJhZGdlLmlubmVySFRNTD0nJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3ZjU2VjJykuc3R5bGUuZGlzcGxheT0nbm9uZSc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3RpbWVTZWMnKS5zdHlsZS5kaXNwbGF5PSdub25lJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc3VjQmxvY2snKS5jbGFzc0xpc3QucmVtb3ZlKCdzaG93Jyk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3Byb2dyZXNzJykuc3R5bGUuZGlzcGxheT0nZmxleCc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NOYW1lJykudmFsdWU9Jyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NQaG9uZScpLnZhbHVlPScnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjRW1haWwnKS52YWx1ZT0nJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY1BldCcpLnZhbHVlPScnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjb25maXJtQnRuJykudGV4dENvbnRlbnQ9VFtMQU5HXS5jb25maXJtX2J0bjsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY29uZmlybUJ0bicpLmRpc2FibGVkPWZhbHNlOwogIGdvU3RlcCgxKTsKfQoKdmFyIExBTkcgPSBsb2NhbFN0b3JhZ2UuZ2V0SXRlbSgncmpsYW5nJykgfHwgJ3J1JzsKdmFyIFQgPSB7CiAgcnU6ewogICAgbG9nb190YWc6J9Cf0YDQtdC80LjQsNC70YzQvdGL0Lkg0LPRgNGD0LzQuNC90LMtPGJyPtGB0LDQu9C+0L0g0LIg0KLQsNC70LvQuNC90LUnLAogICAgY2hvb3NlX2hvdzonQ2hvb3NlIGhvdyB0byBjb25uZWN0JywKICAgIGJvb2tfb25saW5lOidCb29rIE9ubGluZScsCiAgICBib29rX2Zsb3c6J9Cf0L7RgNC+0LTQsCDihpIg0KPRgdC70YPQs9CwIOKGkiDQnNCw0YHRgtC10YAg4oaSINCS0YDQtdC80Y8nLAogICAgb3JfY29udGFjdDonb3IgY29udGFjdCB1cycsCiAgICBjYWxsX3VzOidDYWxsIFVzJywKICAgIGJhY2s6J+KGkCDQndCw0LfQsNC0JywKICAgIGxvZ29fc3ViOidHcm9vbWluZyDCtyDQotCw0LvQu9C40L0nLAogICAgcHNfc2VydmljZTon0KPRgdC70YPQs9CwJyxwc19tYXN0ZXI6J9Cc0LDRgdGC0LXRgCcscHNfcGV0OifQn9C40YLQvtC80LXRhicscHNfZGF0ZTon0JTQsNGC0LAnLHBzX2RldGFpbHM6J9CU0LDQvdC90YvQtScsCiAgICBzdGVwMV9sYmw6JzAxIMK3INCf0L7RgNC+0LTQsCcsCiAgICBicmVlZF9waDon0J3QsNGH0L3QuNGC0LUg0LLQstC+0LTQuNGC0Ywg0L/QvtGA0L7QtNGDLi4uJywKICAgIHN0ZXAyX2xibDonMDIgwrcg0KPRgdC70YPQs9CwJywKICAgIHN0ZXAyX21hc3Rlcjon0JLRi9Cx0LXRgNC40YLQtSDQvNCw0YHRgtC10YDQsCcsCiAgICBzdGVwM19sYmw6J9Ca0LDQuiDQtNCw0LLQvdC+INCy0Ysg0L/QvtGB0LXRidCw0LvQuCDQs9GA0YPQvNC40L3Qsz8nLAogICAgZzE6J9Cf0LXRgNCy0YvQuSDRgNCw0LcnLGcyOifQntGCIDEg0LTQviAzINC80LXRgdGP0YbQtdCyJyxnMzon0J7RgiAzINC00L4gNiDQvNC10YHRj9GG0LXQsicsZzQ6J9CR0L7Qu9C10LUgNiDQvNC10YHRj9GG0LXQsicsCiAgICBzdGVwNF9sYmw6J9CS0YvQsdC10YDQuNGC0LUg0LTQsNGC0YMnLAogICAgY2FsX2F2YWlsOifQldGB0YLRjCDRgdCy0L7QsdC+0LTQvdC+0LUg0LLRgNC10LzRjycsY2FsX25vbmU6J9Ch0LLQvtCx0L7QtNC90L7Qs9C+INCy0YDQtdC80LXQvdC4INC90LXRgicsCiAgICBzdGVwNF90aW1lOifQktGL0LHQtdGA0LjRgtC1INCy0YDQtdC80Y8nLAogICAgc3RlcDVfbGJsOifQktCw0YjQuCDQtNCw0L3QvdGL0LUnLAogICAgbGJsX25hbWU6J9CY0LzRjycscGhfbmFtZTon0JLQsNGI0LUg0LjQvNGPJywKICAgIGxibF9waG9uZTon0KLQtdC70LXRhNC+0L0nLGxibF9lbWFpbDonRW1haWwnLAogICAgbGJsX3BldDon0JrQu9C40YfQutCwINC/0LjRgtC+0LzRhtCwJyxwaF9vcHRpb25hbDon0J3QtdC+0LHRj9C30LDRgtC10LvRjNC90L4nLAogICAgY29uZmlybV9idG46J9Cf0L7QtNGC0LLQtdGA0LTQuNGC0Ywg0LfQsNC/0LjRgdGMJywKICAgIHN1Y2Nlc3NfdGl0bGU6J9CX0LDQv9C40YHRjCDQv9GA0LjQvdGP0YLQsCEnLAogICAgc3VjY2Vzc19zdWI6J9Cc0Ysg0YHQstGP0LbQtdC80YHRjyDRgSDQstCw0LzQuCDQtNC70Y8g0L/QvtC00YLQstC10YDQttC00LXQvdC40Y8uPGJyPtCh0L/QsNGB0LjQsdC+LCDRh9GC0L4g0LLRi9Cx0YDQsNC70LggUiZhbXA7SiBHcm9vbWluZyEnLAogICAgdG9faG9tZTon4oaQINCd0LAg0LPQu9Cw0LLQvdGD0Y4nLAogICAgYWxlcnRfZmlsbDon0JLQstC10LTQuNGC0LUg0LjQvNGPINC4INGC0LXQu9C10YTQvtC9JyxhbGVydF9waG9uZTon0JLQstC10LTQuNGC0LUg0L3QvtC80LXRgCDQsiDRhNC+0YDQvNCw0YLQtSArMzcyMTIzNDU2NzgnLAogICAgc2VuZGluZzon0J7RgtC/0YDQsNCy0LvRj9C10LwuLi4nLAogICAgc3VtX2JyZWVkOifQn9C+0YDQvtC00LAnLHN1bV9zZXJ2aWNlOifQo9GB0LvRg9Cz0LAnLHN1bV9tYXN0ZXI6J9Cc0LDRgdGC0LXRgCcsc3VtX2dyb29tOifQn9C+0YHQu9C10LTQvdC40Lkg0LPRgNGD0LwnLHN1bV9kYXRlOifQlNCw0YLQsCcsc3VtX3RpbWU6J9CS0YDQtdC80Y8nLHN1bV9wcmljZTon0KHRgtC+0LjQvNC+0YHRgtGMJywKICAgIG1vbnRoczpbJ9Cv0L3QstCw0YDRjCcsJ9Ck0LXQstGA0LDQu9GMJywn0JzQsNGA0YInLCfQkNC/0YDQtdC70YwnLCfQnNCw0LknLCfQmNGO0L3RjCcsJ9CY0Y7Qu9GMJywn0JDQstCz0YPRgdGCJywn0KHQtdC90YLRj9Cx0YDRjCcsJ9Ce0LrRgtGP0LHRgNGMJywn0J3QvtGP0LHRgNGMJywn0JTQtdC60LDQsdGA0YwnXQogIH0sCiAgZW46ewogICAgbG9nb190YWc6J1ByZW1pdW0gZ3Jvb21pbmc8YnI+c2Fsb24gaW4gVGFsbGlubicsCiAgICBjaG9vc2VfaG93OidDaG9vc2UgaG93IHRvIGNvbm5lY3QnLAogICAgYm9va19vbmxpbmU6J0Jvb2sgT25saW5lJywKICAgIGJvb2tfZmxvdzonQnJlZWQg4oaSIFNlcnZpY2Ug4oaSIE1hc3RlciDihpIgVGltZScsCiAgICBvcl9jb250YWN0OidvciBjb250YWN0IHVzJywKICAgIGNhbGxfdXM6J0NhbGwgVXMnLAogICAgYmFjazon4oaQIEJhY2snLAogICAgbG9nb19zdWI6J0dyb29taW5nIMK3IFRhbGxpbm4nLAogICAgcHNfc2VydmljZTonU2VydmljZScscHNfbWFzdGVyOidNYXN0ZXInLHBzX3BldDonUGV0Jyxwc19kYXRlOidEYXRlJyxwc19kZXRhaWxzOidEZXRhaWxzJywKICAgIHN0ZXAxX2xibDonMDEgwrcgRG9nIGJyZWVkJywKICAgIGJyZWVkX3BoOidTdGFydCB0eXBpbmcgYnJlZWQuLi4nLAogICAgc3RlcDJfbGJsOicwMiDCtyBTZXJ2aWNlJywKICAgIHN0ZXAyX21hc3RlcjonQ2hvb3NlIG1hc3RlcicsCiAgICBzdGVwM19sYmw6J0hvdyBsb25nIGFnbyB3YXMgeW91ciBsYXN0IGdyb29taW5nPycsCiAgICBnMTonRmlyc3QgdGltZScsZzI6JzHigJMzIG1vbnRocyBhZ28nLGczOicz4oCTNiBtb250aHMgYWdvJyxnNDonT3ZlciA2IG1vbnRocycsCiAgICBzdGVwNF9sYmw6J0Nob29zZSBkYXRlJywKICAgIGNhbF9hdmFpbDonQXZhaWxhYmxlJyxjYWxfbm9uZTonTm90IGF2YWlsYWJsZScsCiAgICBzdGVwNF90aW1lOidDaG9vc2UgdGltZScsCiAgICBzdGVwNV9sYmw6J1lvdXIgZGV0YWlscycsCiAgICBsYmxfbmFtZTonTmFtZScscGhfbmFtZTonWW91ciBuYW1lJywKICAgIGxibF9waG9uZTonUGhvbmUnLGxibF9lbWFpbDonRW1haWwnLAogICAgbGJsX3BldDoiUGV0J3MgbmFtZSIscGhfb3B0aW9uYWw6J09wdGlvbmFsJywKICAgIGNvbmZpcm1fYnRuOidDb25maXJtIGJvb2tpbmcnLAogICAgc3VjY2Vzc190aXRsZTonQm9va2luZyBjb25maXJtZWQhJywKICAgIHN1Y2Nlc3Nfc3ViOidXZSB3aWxsIGNvbnRhY3QgeW91IHRvIGNvbmZpcm0uPGJyPlRoYW5rIHlvdSBmb3IgY2hvb3NpbmcgUiZhbXA7SiBHcm9vbWluZyEnLAogICAgdG9faG9tZTon4oaQIEhvbWUnLAogICAgYWxlcnRfZmlsbDonUGxlYXNlIGVudGVyIG5hbWUgYW5kIHBob25lJyxhbGVydF9waG9uZTonRW50ZXIgcGhvbmUgbnVtYmVyIGluIGZvcm1hdCArMzcyMTIzNDU2NzgnLAogICAgc2VuZGluZzonU2VuZGluZy4uLicsCiAgICBzdW1fYnJlZWQ6J0JyZWVkJyxzdW1fc2VydmljZTonU2VydmljZScsc3VtX21hc3RlcjonTWFzdGVyJyxzdW1fZ3Jvb206J0xhc3QgZ3Jvb21pbmcnLHN1bV9kYXRlOidEYXRlJyxzdW1fdGltZTonVGltZScsc3VtX3ByaWNlOidQcmljZScsCiAgICBtb250aHM6WydKYW51YXJ5JywnRmVicnVhcnknLCdNYXJjaCcsJ0FwcmlsJywnTWF5JywnSnVuZScsJ0p1bHknLCdBdWd1c3QnLCdTZXB0ZW1iZXInLCdPY3RvYmVyJywnTm92ZW1iZXInLCdEZWNlbWJlciddCiAgfSwKICBldDp7CiAgICBsb2dvX3RhZzonRXNtYWtsYXNzaWxpbmUgaG9vbGR1c3RlZW51czxicj5UYWxsaW5uYXMnLAogICAgY2hvb3NlX2hvdzonVmFsaSDDvGhlbmR1c3ZpaXMnLAogICAgYm9va19vbmxpbmU6J0Jyb25lZXJpIHZlZWJpcycsCiAgICBib29rX2Zsb3c6J1TDtXVnIOKGkiBUZWVudXMg4oaSIE1laXN0ZXIg4oaSIEFlZycsCiAgICBvcl9jb250YWN0Oid2w7VpIHbDtXRhIMO8aGVuZHVzdCcsCiAgICBjYWxsX3VzOidIZWxpc3RhIG1laWxlJywKICAgIGJhY2s6J+KGkCBUYWdhc2knLAogICAgbG9nb19zdWI6J0dyb29taW5nIMK3IFRhbGxpbm4nLAogICAgcHNfc2VydmljZTonVGVlbnVzJyxwc19tYXN0ZXI6J01laXN0ZXInLHBzX3BldDonTGVtbWlrbG9vbScscHNfZGF0ZTonS3V1cMOkZXYnLHBzX2RldGFpbHM6J0FuZG1lZCcsCiAgICBzdGVwMV9sYmw6JzAxIMK3IEtvZXJhIHTDtXVnJywKICAgIGJyZWVkX3BoOidBbHVzdGFnZSB0w7V1IHNpc2VzdGFtaXN0Li4uJywKICAgIHN0ZXAyX2xibDonMDIgwrcgVGVlbnVzJywKICAgIHN0ZXAyX21hc3RlcjonVmFsaSBtZWlzdGVyJywKICAgIHN0ZXAzX2xibDonTWlsbGFsIGvDpGlzaXRlIHZpaW1hdGkgZ3Jvb21pbmd1cz8nLAogICAgZzE6J0VzaW1lc3Qga29yZGEnLGcyOicx4oCTMyBrdXVkIHRhZ2FzaScsZzM6JzPigJM2IGt1dWQgdGFnYXNpJyxnNDonw5xsZSA2IGt1dScsCiAgICBzdGVwNF9sYmw6J1ZhbGkga3V1cMOkZXYnLAogICAgY2FsX2F2YWlsOidWYWJ1IGFlZ3Ugb24nLGNhbF9ub25lOidWYWJ1IGFlZ3UgcG9sZScsCiAgICBzdGVwNF90aW1lOidWYWxpIGtlbGxhYWVnJywKICAgIHN0ZXA1X2xibDonVGVpZSBhbmRtZWQnLAogICAgbGJsX25hbWU6J05pbWknLHBoX25hbWU6J1RlaWUgbmltaScsCiAgICBsYmxfcGhvbmU6J1RlbGVmb24nLGxibF9lbWFpbDonRW1haWwnLAogICAgbGJsX3BldDonTGVtbWlrbG9vbWEgbmltaScscGhfb3B0aW9uYWw6J1ZhbGlrdWxpbmUnLAogICAgY29uZmlybV9idG46J0tpbm5pdGEgYnJvbmVlcmluZycsCiAgICBzdWNjZXNzX3RpdGxlOidCcm9uZWVyaW5nIGtpbm5pdGF0dWQhJywKICAgIHN1Y2Nlc3Nfc3ViOidWw7V0YW1lIHRlaWVnYSDDvGhlbmR1c3Qga2lubml0YW1pc2Vrcy48YnI+VMOkbmFtZSwgZXQgdmFsaXNpdGUgUiZhbXA7SiBHcm9vbWluZyEnLAogICAgdG9faG9tZTon4oaQIEF2YWxlaGVsZScsCiAgICBhbGVydF9maWxsOidQYWx1biBzaXNlc3RhZ2UgbmltaSBqYSB0ZWxlZm9uJyxhbGVydF9waG9uZTonU2lzZXN0YWdlIHRlbGVmb25pbnVtYmVyIHZvcm1pbmd1cyArMzcyMTIzNDU2NzgnLAogICAgc2VuZGluZzonU2FhZGFuLi4uJywKICAgIHN1bV9icmVlZDonVMO1dWcnLHN1bV9zZXJ2aWNlOidUZWVudXMnLHN1bV9tYXN0ZXI6J01laXN0ZXInLHN1bV9ncm9vbTonVmlpbWFuZSBncm9vbWluZycsc3VtX2RhdGU6J0t1dXDDpGV2JyxzdW1fdGltZTonS2VsbGFhZWcnLHN1bV9wcmljZTonSGluZCcsCiAgICBtb250aHM6WydKYWFudWFyJywnVmVlYnJ1YXInLCdNw6RydHMnLCdBcHJpbGwnLCdNYWknLCdKdXVuaScsJ0p1dWxpJywnQXVndXN0JywnU2VwdGVtYmVyJywnT2t0b29iZXInLCdOb3ZlbWJlcicsJ0RldHNlbWJlciddCiAgfQp9OwoKZnVuY3Rpb24gc2V0TGFuZyhsKXsKICBMQU5HPWw7CiAgbG9jYWxTdG9yYWdlLnNldEl0ZW0oJ3JqbGFuZycsbCk7CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLmxhbmctYnRuJykuZm9yRWFjaChmdW5jdGlvbihiKXsKICAgIGIuY2xhc3NMaXN0LnRvZ2dsZSgnYWN0aXZlJywgYi50ZXh0Q29udGVudC50b0xvd2VyQ2FzZSgpPT09bCk7CiAgfSk7CiAgdmFyIHRyPVRbbF07CiAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnW2RhdGEtaTE4bl0nKS5mb3JFYWNoKGZ1bmN0aW9uKGVsKXsKICAgIHZhciBrPWVsLmdldEF0dHJpYnV0ZSgnZGF0YS1pMThuJyk7CiAgICBpZih0cltrXSE9PXVuZGVmaW5lZCkgZWwuaW5uZXJIVE1MPXRyW2tdOwogIH0pOwogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJ1tkYXRhLWkxOG4tcGhdJykuZm9yRWFjaChmdW5jdGlvbihlbCl7CiAgICB2YXIgaz1lbC5nZXRBdHRyaWJ1dGUoJ2RhdGEtaTE4bi1waCcpOwogICAgaWYodHJba10hPT11bmRlZmluZWQpIGVsLnBsYWNlaG9sZGVyPXRyW2tdOwogIH0pOwogIE1PTlRIUz10ci5tb250aHM7CiAgYnVpbGRDYWwoKTsKICAvLyBSZS1yZW5kZXIgYmFkZ2UgYW5kIHNlcnZpY2VzIGlmIGJyZWVkIGFscmVhZHkgc2VsZWN0ZWQKICBpZihzZWxCcmVlZCl7CiAgICB2YXIgYmY9bD09PSdlbic/J2JyZWVkX2VuJzpsPT09J2V0Jz8nYnJlZWRfZXQnOidicmVlZCc7CiAgICB2YXIgZGI9c2VsQnJlZWRbYmZdfHxzZWxCcmVlZC5icmVlZDsKICAgIGJvb2tpbmcuYnJlZWREaXNwbGF5PWRiOwogICAgdmFyIGJuRWw9ZG9jdW1lbnQucXVlcnlTZWxlY3RvcignI3NCYWRnZSAuYm5hbWUnKTsKICAgIGlmKGJuRWwpIGJuRWwudGV4dENvbnRlbnQ9ZGI7CiAgICB2YXIgYmNFbD1kb2N1bWVudC5xdWVyeVNlbGVjdG9yKCcjc0JhZGdlIC5iY2hnJyk7CiAgICBpZihiY0VsKSBiY0VsLnRleHRDb250ZW50PWw9PT0nZW4nPydDaGFuZ2UnOmw9PT0nZXQnPydNdXVkYSc6J9CY0LfQvNC10L3QuNGC0YwnOwogICAgaWYoZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y1NlYycpLnN0eWxlLmRpc3BsYXkhPT0nbm9uZScpIHJlbmRlclN2Y3Moc2VsQnJlZWQpOwogICAgdmFyIHNuPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdmNOb3RlJyk7CiAgICBpZihzbil7CiAgICAgIHZhciBudD1sPT09J2VuJz8nUGxlYXNlIG5vdGUnOmw9PT0nZXQnPydQYW5nZSB0w6RoZWxlJzon0JLQsNC20L3QviDQt9C90LDRgtGMJzsKICAgICAgdmFyIG5iPWw9PT0nZW4nPydGaW5hbCBwcmljZSBkZXBlbmRzIG9uIGNvYXQgY29uZGl0aW9uIGFuZCBwZXQgYmVoYXZpb3VyLjxicj5EZW1hdHRpbmcgZnJvbSA1IOKCrC48YnI+QWdncmVzc2l2ZSBiZWhhdmlvdXIgc3VyY2hhcmdlIG1heSBhcHBseTogKzUwJS4nOmw9PT0nZXQnPydMw7VwbGlrIGhpbmQgc8O1bHR1YiBrYXJ2YXN0aWt1IHNlaXN1bmRpc3QgamEgbGVtbWlrbG9vbWEga8OkaXR1bWlzZXN0Ljxicj5Lb2x0c3VuaXRlIGxhaHRpaGFydXRhbWluZSBhbGF0ZXMgNSDigqwuPGJyPkFncmVzc2lpdnNlIGvDpGl0dW1pc2Uga29ycmFsIHbDtWliIGxpc2FuZHVkYSA1MCUganV1cmRlaGluZGx1cy4nOifQntC60L7QvdGH0LDRgtC10LvRjNC90LDRjyDRgdGC0L7QuNC80L7RgdGC0Ywg0LfQsNCy0LjRgdC40YIg0L7RgiDRgdC+0YHRgtC+0Y/QvdC40Y8g0YjQtdGA0YHRgtC4INC4INC/0L7QstC10LTQtdC90LjRjyDQv9C40YLQvtC80YbQsC48YnI+0KDQsNC30LHQvtGAINC60L7Qu9GC0YPQvdC+0LIg4oCUINC+0YIgNSDigqwuPGJyPtCf0YDQuCDQsNCz0YDQtdGB0YHQuNCy0L3QvtC8INC/0L7QstC10LTQtdC90LjQuCDQvNC+0LbQtdGCINC/0YDQuNC80LXQvdGP0YLRjNGB0Y8g0LTQvtC/0LvQsNGC0LAgNTAlLic7CiAgICAgIHNuLmlubmVySFRNTD0nPGRpdiBzdHlsZT0iZm9udC1zaXplOjAuNjdyZW07bGV0dGVyLXNwYWNpbmc6LjE1ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNmZmZmZmY7bWFyZ2luLWJvdHRvbTo4cHg7Zm9udC13ZWlnaHQ6NjAwO2ZvbnQtZmFtaWx5OlwnTW9udHNlcnJhdFwnLHNhbnMtc2VyaWYiPicrbnQrJzwvZGl2PjxkaXYgc3R5bGU9ImZvbnQtc2l6ZTowLjgycmVtO2NvbG9yOiNmZmZmZmY7bGluZS1oZWlnaHQ6MS44O2ZvbnQtZmFtaWx5OlwnTW9udHNlcnJhdFwnLHNhbnMtc2VyaWYiPicrbmIrJzwvZGl2Pic7CiAgICB9CiAgfQp9CgovLyBBcHBseSBzYXZlZCBsYW5ndWFnZSBvbiBsb2FkCihmdW5jdGlvbigpeyBzZXRMYW5nKExBTkcpOyB9KSgpOwoKLy8gQ2FsbGJhY2sgZm9ybQpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2FsbGJhY2tCdG4nKS5vbmNsaWNrID0gZnVuY3Rpb24oKXsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrTW9kYWwnKS5zdHlsZS5kaXNwbGF5ID0gJ2ZsZXgnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtOYW1lJykudmFsdWUgPSAnJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrUGhvbmUnKS52YWx1ZSA9ICcnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtTdWNjZXNzJykuc3R5bGUuZGlzcGxheSA9ICdub25lJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrU3VibWl0Jykuc3R5bGUuZGlzcGxheSA9ICdibG9jayc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia0Nsb3NlJykudGV4dENvbnRlbnQgPSAn0J7RgtC80LXQvdCwJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrU3VibWl0JykudGV4dENvbnRlbnQgPSAn0J7RgtC/0YDQsNCy0LjRgtGMJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrU3VibWl0JykuZGlzYWJsZWQgPSBmYWxzZTsKfTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia0Nsb3NlJykub25jbGljayA9IGZ1bmN0aW9uKCl7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia01vZGFsJykuc3R5bGUuZGlzcGxheSA9ICdub25lJzsKfTsKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1N1Ym1pdCcpLm9uY2xpY2sgPSBmdW5jdGlvbigpewogIHZhciBuYW1lID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia05hbWUnKS52YWx1ZS50cmltKCk7CiAgdmFyIHBob25lID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Nia1Bob25lJykudmFsdWUudHJpbSgpLnJlcGxhY2UoL1xEL2csJycpOwogIGlmKCFuYW1lIHx8ICFwaG9uZSl7YWxlcnQoJ9CS0LLQtdC00LjRgtC1INC40LzRjyDQuCDRgtC10LvQtdGE0L7QvScpO3JldHVybjt9CiAgdmFyIGJ0biA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtTdWJtaXQnKTsKICBidG4udGV4dENvbnRlbnQgPSAn0J7RgtC/0YDQsNCy0LvRj9C10LwuLi4nOyBidG4uZGlzYWJsZWQgPSB0cnVlOwogIGZldGNoKCcvYXBpL2NhbGxiYWNrJyx7CiAgICBtZXRob2Q6J1BPU1QnLAogICAgaGVhZGVyczp7J0NvbnRlbnQtVHlwZSc6J2FwcGxpY2F0aW9uL2pzb24nfSwKICAgIGJvZHk6SlNPTi5zdHJpbmdpZnkoe25hbWU6bmFtZSwgcGhvbmU6JyszNzInK3Bob25lfSkKICB9KS50aGVuKGZ1bmN0aW9uKCl7CiAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrU3VjY2VzcycpLnN0eWxlLmRpc3BsYXkgPSAnYmxvY2snOwogICAgYnRuLnN0eWxlLmRpc3BsYXkgPSAnbm9uZSc7CiAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2JrQ2xvc2UnKS50ZXh0Q29udGVudCA9ICfihpAg0JfQsNC60YDRi9GC0YwnOwogICAgc2V0VGltZW91dChmdW5jdGlvbigpe2RvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYmtNb2RhbCcpLnN0eWxlLmRpc3BsYXk9J25vbmUnO30sMzAwMCk7CiAgfSkuY2F0Y2goZnVuY3Rpb24oKXsKICAgIGJ0bi50ZXh0Q29udGVudCA9ICfQntGC0L/RgNCw0LLQuNGC0YwnOyBidG4uZGlzYWJsZWQgPSBmYWxzZTsKICAgIGFsZXJ0KCfQntGI0LjQsdC60LAuINCf0L7Qv9GA0L7QsdGD0LnRgtC1INC10YnRkSDRgNCw0LcuJyk7CiAgfSk7Cn07Cgo8L3NjcmlwdD4KPC9ib2R5Pgo8L2h0bWw+Cg=="



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
    msg = f"Заявка: {name}, тел {phone}"
    twilio_sid   = os.environ.get("TWILIO_ACCOUNT_SID")
    twilio_token = os.environ.get("TWILIO_AUTH_TOKEN")
    twilio_from  = os.environ.get("TWILIO_ADMIN_PHONE", "+37266922128")
    admin_to     = "+37258243141"
    print(f"[callback] name={name!r} phone={phone!r} sid_set={bool(twilio_sid)} from={twilio_from}", flush=True)
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
