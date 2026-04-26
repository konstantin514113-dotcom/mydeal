from flask import Flask, request, jsonify, session, redirect
import anthropic
import os
import requests
from datetime import datetime, timedelta
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "rjgrooming-secret-2024")
client_ai = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

WHATSAPP_TOKEN   = os.environ.get("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID")
VERIFY_TOKEN     = os.environ.get("WHATSAPP_VERIFY_TOKEN")
ADMIN_PASSWORD   = os.environ.get("ADMIN_PASSWORD", "rjadmin2024")

# ── State ──────────────────────────────────────────────────────────────────
conversation_history = {}
MAX_HISTORY = 20

jarvis_enabled = True
pause_until    = None   # datetime or None
manual_mode    = False  # full manual — no auto replies

schedule = {
    "Mon": {"enabled": True,  "open": "09:00", "close": "18:00"},
    "Tue": {"enabled": True,  "open": "09:00", "close": "18:00"},
    "Wed": {"enabled": True,  "open": "09:00", "close": "18:00"},
    "Thu": {"enabled": True,  "open": "09:00", "close": "18:00"},
    "Fri": {"enabled": True,  "open": "09:00", "close": "18:00"},
    "Sat": {"enabled": True,  "open": "10:00", "close": "16:00"},
    "Sun": {"enabled": False, "open": "10:00", "close": "14:00"},
}

# phone -> {channel, timestamps: [datetime,...], last_seen, last_text, mode: "jarvis"|"manual"}
clients = {}

# ── System prompt ──────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are Jarvis, the AI administrator of R&J Grooming — a professional pet grooming salon in Tallinn, Estonia.

Your responsibilities:
- Book appointments for dogs and cats
- Answer questions about services, prices, and working hours
- Provide information about the salon

Rules:
- Always respond in the client's language (Russian, Estonian, or English — match exactly what they write)
- Keep answers short: 1–3 sentences maximum, no long lists
- Be warm and professional
- Do not reveal you are an AI unless directly asked
- Greet the client ONLY on their very first message in the conversation. For all subsequent messages — no greeting, go straight to the point.

First-message greeting examples (choose by client's language):
- Russian: "Привет! Это R&J Grooming, чем могу помочь?"
- Estonian: "Tere! See on R&J Grooming, kuidas saan aidata?"
- English: "Hi! This is R&J Grooming, how can I help?"

Salon info:
- Name: R&J Grooming
- Location: Tallinn, Estonia
- Services: grooming for dogs and cats — haircut, bathing, nail trimming, ear cleaning, full grooming packages

--- ПРАЙС-ЛИСТ R&J GROOMING (все цены в €) ---

УСЛУГИ:
Б = Базовый уход (мытьё, сушка, вычёсывание)
Г = Гигиенический уход (мытьё, сушка, вычёсывание + когти, уши, глаза)
К = Комплексный уход (всё выше + породная стрижка)
Л = Экспресс-линька (удаление подшёрстка, продувка)
Т = Тримминг (ручная щипка, формирование силуэта)

СОБАКИ — ЦЕНЫ ПО ПОРОДАМ:
Акита-ину 20–40 кг: Б=60, Г=75, Л=90
Акита-ину более 40 кг: Б=85, Г=95, Л=115
Акита-ину флаффи 20–40 кг: Б=60, Г=75, Л=90
Акита-ину флаффи более 40 кг: Б=85, Г=95, Л=115
Американская акита 20–40 кг: Б=60, Г=75, Л=90
Американская акита более 40 кг: Б=85, Г=95, Л=115
Американская акита флаффи 20–40 кг: Б=60, Г=75, Л=90
Американская акита флаффи более 40 кг: Б=85, Г=95, Л=115
Алабай 40–60 кг: Б=85, Г=95, Л=115
Алабай более 60 кг: Б=100, Г=115, Л=130
Аляскинский маламут 20–40 кг: Б=60, Г=75, Л=90
Аляскинский маламут более 40 кг: Б=85, Г=95, Л=115
Аляскинский маламут флаффи 20–40 кг: Б=60, Г=75, Л=90
Аляскинский маламут флаффи более 40 кг: Б=85, Г=95, Л=115
Американский кокер-спаниель 10–15 кг: Б=40, Г=55, К=70
Американский кокер-спаниель 15–20 кг: Б=45, Г=65, К=80
Английский бульдог: Б=45, Г=55, Л=70
Английский кокер-спаниель 10–15 кг: Б=40, Г=55, К=70
Английский кокер-спаниель 15–20 кг: Б=45, Г=65, К=80
Афган 20–30 кг: Б=50, Г=70, К=90
Афган 30–40 кг: Б=60, Г=80, К=100
Бернский зенненхунд 30–40 кг: Б=70, Г=85, К=110, Л=100
Бернский зенненхунд более 40 кг: Б=85, Г=95, К=130, Л=115
Бигль 10–15 кг: Б=35, Г=45, Л=60
Бигль 15–20 кг: Б=40, Г=50, Л=65
Бишон-фризе до 5 кг: Б=30, Г=40, К=55
Бишон-фризе 5–10 кг: Б=35, Г=45, К=60
Бордер-колли 15–20 кг: Б=50, Г=70, К=90, Л=80
Бордер-колли 20–25 кг: Б=60, Г=75, К=100, Л=90
Брабансон: Б=30, Г=40, Л=50
Вельш-корги 10–15 кг: Б=45, Г=60, Л=70
Вельш-корги 15–20 кг: Б=50, Г=70, Л=80
Вест-хайленд-вайт-терьер: Б=35, Г=45, К=60, Т=65
Голден-ретривер 20–30 кг: Б=60, Г=75, К=100, Л=90
Голден-ретривер 30–40 кг: Б=70, Г=85, К=110, Л=100
Гриффон: Б=35, Г=45, К=60, Т=65
Джек-рассел-терьер гладкошерстный: Б=30, Г=40, Л=50
Джек-рассел-терьер жесткошерстный: Б=35, Г=45, К=60, Т=65
Далматин: Б=45, Г=55, Л=70
Доберман 30–40 кг: Б=55, Г=65, Л=80
Доберман более 40 кг: Б=70, Г=80, Л=95
Ирландский мягкошерстный пшеничный терьер: Б=45, Г=65, К=80
Ирландский терьер: Б=40, Г=55, К=70, Т=75
Испанский гальго 20–30 кг: Б=45, Г=55, Л=70
Испанский гальго 30–40 кг: Б=55, Г=65, Л=80
Йоркширский терьер до 3,5 кг: Б=30, Г=40, К=55
Йоркширский терьер более 3,5 кг: Б=35, Г=45, К=60
Бивер-йорк до 3,5 кг: Б=30, Г=40, К=55
Бивер-йорк более 3,5 кг: Б=35, Г=45, К=60
Кавалер-кинг-чарльз-спаниель 5–10 кг: Б=35, Г=45, К=60
Кавалер-кинг-чарльз-спаниель 10–15 кг: Б=40, Г=55, К=70
Китайская хохлатая пуховая до 5 кг: Б=30, Г=40, К=55
Китайская хохлатая пуховая 5–10 кг: Б=35, Г=45, К=60
Китайская хохлатая голая до 5 кг: Б=28, Г=35, К=55
Китайская хохлатая голая 5–10 кг: Б=32, Г=42, К=60
Колли 20–30 кг: Б=60, Г=75, К=100, Л=90
Колли 30–40 кг: Б=70, Г=85, К=110, Л=100
Комондор 30–40 кг: Б=70, Г=85, К=110
Комондор более 40 кг: Б=85, Г=95, К=130
Лабрадор гладкошерстный 20–30 кг: Б=45, Г=55, Л=70
Лабрадор гладкошерстный 30–40 кг: Б=55, Г=65, Л=80
Лабрадор гладкошерстный более 40 кг: Б=70, Г=80, Л=95
Лабрадор длинношерстный 20–30 кг: Б=60, Г=75, К=100, Л=90
Лабрадор длинношерстный 30–40 кг: Б=70, Г=85, К=110, Л=100
Лабрадор длинношерстный более 40 кг: Б=85, Г=95, К=130, Л=115
Лабрадудель 10–20 кг: Б=40, Г=55, К=70
Лабрадудель 20–30 кг: Б=50, Г=70, К=90
Лабрадудель 30–40 кг: Б=60, Г=80, К=100
Мальтипу до 5 кг: Б=30, Г=40, К=55
Мальтипу 5–10 кг: Б=35, Г=45, К=60
Мальтипу 10–15 кг: Б=40, Г=55, К=70
Мальтезе: Б=30, Г=40, К=55
Миттельшнауцер 10–15 кг: Б=40, Г=55, К=70, Т=75
Миттельшнауцер 15–20 кг: Б=45, Г=65, К=80, Т=85
Мопс: Б=30, Г=40, Л=50
Невская орхидея: Б=30, Г=40, К=55
Немецкая овчарка 20–30 кг: Б=60, Г=75, Л=90
Немецкая овчарка 30–40 кг: Б=70, Г=85, Л=100
Немецкая овчарка более 40 кг: Б=85, Г=95, Л=115
Норвич-терьер: Б=35, Г=45, К=60, Т=65
Норфолк-терьер: Б=35, Г=45, К=60, Т=65
Ньюфаундленд 40–60 кг: Б=85, Г=95, К=130, Л=115
Ньюфаундленд более 60 кг: Б=100, Г=115, К=150, Л=130
Папийон: Б=30, Г=40, К=55
Пекинес до 5 кг: Б=30, Г=40, К=55
Пекинес 5–10 кг: Б=35, Г=45, К=60
Пудель той до 5 кг: Б=30, Г=40, К=55
Пудель карликовый 5–10 кг: Б=35, Г=45, К=60
Пудель малый 10–15 кг: Б=40, Г=55, К=70
Пудель малый 15–20 кг: Б=45, Г=65, К=80
Пудель большой 20–30 кг: Б=50, Г=70, К=90
Пудель большой 30–40 кг: Б=60, Г=80, К=100
Ризеншнауцер 30–40 кг: Б=60, Г=80, К=100, Т=110
Ризеншнауцер более 40 кг: Б=75, Г=95, К=120, Т=125
Русский охотничий спаниель 10–15 кг: Б=40, Г=55, К=70
Русский охотничий спаниель 15–20 кг: Б=45, Г=65, К=80
Русский той гладкошерстный: Б=25, Г=35
Русский той длинношерстный: Б=30, Г=40, К=55
Русская цветная болонка: Б=35, Г=45, К=60
Русский черный терьер 30–40 кг: Б=60, Г=80, К=100
Русский черный терьер более 40 кг: Б=75, Г=95, К=120
Самоед 20–30 кг: Б=60, Г=75, Л=90
Самоед 30–40 кг: Б=70, Г=85, Л=100
Сеттер английский 20–30 кг: Б=50, Г=70, К=90
Сеттер ирландский 20–30 кг: Б=50, Г=70, К=90
Сеттер гордон 30–40 кг: Б=60, Г=80, К=100
Сиба-ину: Б=45, Г=60, Л=70
Скотч-терьер: Б=35, Г=45, К=60, Т=65
Силихем-терьер: Б=35, Г=45, К=60, Т=65
Такса гладкошерстная кроличья до 5 кг: Б=25, Г=35, Л=45
Такса гладкошерстная карликовая 5–10 кг: Б=30, Г=40, Л=50
Такса гладкошерстная стандартная 10–15 кг: Б=35, Г=45, Л=60
Такса жесткошерстная кроличья до 5 кг: Б=30, Г=40, К=55, Т=55
Такса жесткошерстная карликовая 5–10 кг: Б=35, Г=45, К=60, Т=65
Такса жесткошерстная стандартная 10–15 кг: Б=40, Г=55, К=70, Т=75
Такса длинношерстная кроличья до 5 кг: Б=30, Г=40, К=55
Такса длинношерстная карликовая 5–10 кг: Б=35, Г=45, К=60
Такса длинношерстная стандартная 10–15 кг: Б=40, Г=55, К=70
Фокстерьер жесткошерстный 5–10 кг: Б=35, Г=45, К=60, Т=65
Фокстерьер жесткошерстный 10–15 кг: Б=40, Г=55, К=70, Т=75
Французский бульдог: Б=35, Г=45, Л=60
Хаски 20–30 кг: Б=60, Г=75, Л=90
Хаски 30–40 кг: Б=70, Г=85, Л=100
Цвергшнауцер 5–10 кг: Б=35, Г=45, К=60, Т=65
Цвергшнауцер 10–15 кг: Б=40, Г=55, К=70, Т=75
Чау-чау 20–30 кг: Б=60, Г=75, К=100, Л=90
Чау-чау 30–40 кг: Б=70, Г=85, К=110, Л=100
Чихуахуа гладкошерстный: Б=25, Г=35
Чихуахуа длинношерстный: Б=30, Г=40, К=55
Шарпей 15–20 кг: Б=40, Г=50, Л=65
Шарпей 20–30 кг: Б=45, Г=55, Л=70
Шелти: Б=40, Г=50, К=65, Л=60
Ши-тцу до 5 кг: Б=30, Г=40, К=55
Ши-тцу 5–10 кг: Б=35, Г=45, К=60
Шпиц немецкий/померанский до 3,5 кг: Б=35, Г=45, К=60, Л=55
Шпиц немецкий/померанский более 3,5 кг: Б=40, Г=50, К=65, Л=60
Шпиц японский: Б=40, Г=50, К=65, Л=60
Японский хин: Б=30, Г=40, К=55
Эстонская гончая 10–15 кг: Б=35, Г=45, Л=60
Эстонская гончая 15–20 кг: Б=40, Г=50, Л=65
Австралийская овчарка 15–20 кг: Б=50, Г=70, К=90, Л=80
Австралийская овчарка 20–25 кг: Б=60, Г=75, К=100, Л=90
Ретривер 20–30 кг: Б=60, Г=75, К=100, Л=90
Ретривер 30–40 кг: Б=70, Г=85, К=110, Л=100

КОШКИ:
Кошки длинношерстные: вычёсывание=55
Кошки короткошерстные: вычёсывание=45

ДОПОЛНИТЕЛЬНЫЕ УСЛУГИ:
Стрижка когтей + оконтовка лап: до 5 кг=20, 5–15 кг=25, 15+ кг=30
Стрижка когтей: до 5 кг=12, 5–15 кг=15, 15+ кг=20
Чистка ушей у собаки: 12
SPA-уход (доп. к любой услуге): до 5 кг=от 15, 5–15 кг=от 20, 15+ кг=от 30
Распутывание колтунов: от 10 до 40 (в зависимости от степени спутанности)
Сложное поведение: от 15 до 30"""

# ── Auth ───────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin"):
            return redirect("/admin/login")
        return f(*args, **kwargs)
    return decorated

# ── Stats helpers ──────────────────────────────────────────────────────────
def count_for(channel, period):
    now = datetime.now()
    cutoffs = {
        "today": now.replace(hour=0, minute=0, second=0, microsecond=0),
        "week":  now - timedelta(days=7),
        "month": now - timedelta(days=30),
    }
    cutoff = cutoffs.get(period, cutoffs["today"])
    total = 0
    for info in clients.values():
        if info.get("channel") == channel:
            total += sum(1 for ts in info.get("timestamps", []) if ts >= cutoff)
    return total

def jarvis_status():
    global jarvis_enabled, pause_until, manual_mode
    now = datetime.now()
    if manual_mode:
        return "manual", "Ручной режим"
    if pause_until and now < pause_until:
        mins = int((pause_until - now).total_seconds() / 60)
        return "paused", f"Пауза ещё {mins} мин"
    if pause_until and now >= pause_until:
        pause_until = None
    if not jarvis_enabled:
        return "off", "Выключен"
    return "on", "Работает"

# ── Admin page ─────────────────────────────────────────────────────────────
DAY_RU = {"Mon":"Пн","Tue":"Вт","Wed":"Ср","Thu":"Чт","Fri":"Пт","Sat":"Сб","Sun":"Вс"}
CH_LINKS = {
    "whatsapp":  "https://wa.me/",
    "instagram": "https://instagram.com/direct/inbox/",
    "facebook":  "https://www.facebook.com/messages/",
    "calls":     "tel:",
}
CH_COLOR = {"whatsapp":"#25D366","instagram":"#E1306C","facebook":"#1877F2","calls":"#FF9F0A"}
CH_ICON  = {"whatsapp":"📱","instagram":"📸","facebook":"💬","calls":"📞"}

@app.route("/admin/login", methods=["GET","POST"])
def admin_login():
    err = ""
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect("/admin")
        err = "Неверный пароль"
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Jarvis Admin</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display',sans-serif;
  background:#000;color:#fff;min-height:100vh;display:flex;align-items:center;justify-content:center}}
.box{{background:#1C1C1E;border-radius:20px;padding:36px 28px;width:320px;text-align:center}}
.logo{{font-size:2.5rem;margin-bottom:8px}}
h2{{font-size:1.3rem;font-weight:700;margin-bottom:4px}}
p{{color:#8E8E93;font-size:.85rem;margin-bottom:24px}}
input{{width:100%;background:#2C2C2E;border:none;color:#fff;padding:14px 16px;
  border-radius:12px;font-size:1rem;margin-bottom:12px;outline:none;-webkit-appearance:none}}
input:focus{{box-shadow:0 0 0 2px #30D158}}
button{{width:100%;background:#30D158;border:none;color:#fff;padding:14px;
  border-radius:12px;font-size:1rem;font-weight:600;cursor:pointer}}
.err{{color:#FF453A;font-size:.82rem;margin-top:10px}}
</style></head><body>
<div class="box">
  <div class="logo">🐾</div>
  <h2>R&amp;J Grooming</h2>
  <p>Jarvis Admin Panel</p>
  <form method="POST">
    <input type="password" name="password" placeholder="Пароль" autofocus>
    <button type="submit">Войти</button>
    {"<div class='err'>"+err+"</div>" if err else ""}
  </form>
</div></body></html>"""

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect("/admin/login")

@app.route("/admin")
@login_required
def admin():
    status_key, status_text = jarvis_status()
    status_colors = {"on":"#30D158","paused":"#FF9F0A","manual":"#636366","off":"#FF453A"}
    status_bg     = {"on":"rgba(48,209,88,.15)","paused":"rgba(255,159,10,.12)","manual":"rgba(99,99,102,.15)","off":"rgba(255,69,58,.12)"}
    sc = status_colors[status_key]
    sb = status_bg[status_key]

    # schedule rows
    sched_html = ""
    for day, cfg in schedule.items():
        checked = "checked" if cfg["enabled"] else ""
        sched_html += f"""
        <div class="sched-row" id="srow-{day}">
          <span class="sday">{DAY_RU[day]}</span>
          <label class="ios-toggle"><input type="checkbox" {checked} onchange="saveSched('{day}',this.checked)"><span class="ios-knob"></span></label>
          <div class="sched-times">
            <input type="time" value="{cfg['open']}"  id="open-{day}"  onchange="saveSched('{day}')">
            <span style="color:#636366">–</span>
            <input type="time" value="{cfg['close']}" id="close-{day}" onchange="saveSched('{day}')">
          </div>
        </div>"""

    # clients rows
    clients_html = ""
    if clients:
        for phone, info in sorted(clients.items(), key=lambda x: x[1].get("last_seen") or datetime.min, reverse=True):
            ch   = info.get("channel","whatsapp")
            mode = info.get("mode","jarvis")
            col  = CH_COLOR.get(ch,"#25D366")
            ico  = CH_ICON.get(ch,"📱")
            link = CH_LINKS.get(ch,"")
            last = info.get("last_seen")
            last_str = last.strftime("%d.%m %H:%M") if isinstance(last, datetime) else "—"
            preview = (info.get("last_text","") or "")[:35]
            jarvis_on = "checked" if mode == "jarvis" else ""
            ts_iso = last.isoformat() if isinstance(last, datetime) else ""
            clients_html += f"""
            <div class="client-row" data-phone="{phone}" data-ts="{ts_iso}">
              <a href="{link}{phone}" class="client-ch" style="color:{col}">{ico}</a>
              <div class="client-info">
                <div class="client-phone">{phone}</div>
                <div class="client-preview">{preview}</div>
              </div>
              <div class="client-meta">
                <div class="client-count">{len(info.get('timestamps',[]))} сообщ.</div>
                <div class="client-time">{last_str}</div>
              </div>
              <label class="ios-toggle sm" title="Jarvis / ручной">
                <input type="checkbox" {jarvis_on} onchange="toggleClientMode('{phone}',this.checked)">
                <span class="ios-knob"></span>
              </label>
            </div>"""
    else:
        clients_html = '<div class="empty">Пока нет клиентов</div>'

    # counters
    def cnt_block(ch):
        d = count_for(ch, "today")
        w = count_for(ch, "week")
        m = count_for(ch, "month")
        col = CH_COLOR[ch]
        ico = CH_ICON[ch]
        link = CH_LINKS.get(ch, "#")
        mx = max(d, w, m, 1)
        return f"""
        <a href="{link}" class="cnt-card" style="--accent:{col}" target="_blank" rel="noopener">
          <div class="cnt-header"><span>{ico}</span><span class="cnt-name">{ch.capitalize()}</span></div>
          <div class="cnt-row"><span class="cnt-label">Сегодня</span><span class="cnt-val">{d}</span><div class="cnt-bar"><div style="width:{d*100//mx}%;background:{col}"></div></div></div>
          <div class="cnt-row"><span class="cnt-label">Неделя</span><span class="cnt-val">{w}</span><div class="cnt-bar"><div style="width:{w*100//mx}%;background:{col}88"></div></div></div>
          <div class="cnt-row"><span class="cnt-label">Месяц</span><span class="cnt-val">{m}</span><div class="cnt-bar"><div style="width:{m*100//mx}%;background:{col}44"></div></div></div>
        </a>"""

    counters_html = "".join(cnt_block(ch) for ch in ["whatsapp","instagram","facebook","calls"])

    pause_info = ""
    global pause_until
    if pause_until and datetime.now() < pause_until:
        pause_info = f'<div class="pause-banner">⏸ Пауза до {pause_until.strftime("%H:%M")}</div>'

    return f"""<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Jarvis Admin</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{--green:#30D158;--bg:#000;--card:#1C1C1E;--card2:#2C2C2E;--sep:#38383A;--text:#fff;--sub:#8E8E93}}
body{{font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display',sans-serif;background:var(--bg);color:var(--text);padding-bottom:env(safe-area-inset-bottom,20px)}}

/* Topbar */
.topbar{{background:#1C1C1E;padding:14px 20px calc(14px + env(safe-area-inset-top,0px));padding-top:calc(14px + env(safe-area-inset-top,0px));display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;border-bottom:1px solid var(--sep)}}
.topbar-title{{font-size:1rem;font-weight:700}}
.topbar-title span{{color:var(--green)}}
.logout{{color:var(--sub);font-size:.82rem;text-decoration:none}}

.wrap{{padding:16px 16px 40px;max-width:480px;margin:0 auto;display:flex;flex-direction:column;gap:16px}}

/* Section header */
.sec-title{{font-size:.72rem;font-weight:600;color:var(--sub);text-transform:uppercase;letter-spacing:.6px;padding:0 4px;margin-bottom:-8px}}

/* Card */
.card{{background:var(--card);border-radius:16px;overflow:hidden}}

/* Jarvis status toggle */
.status-card{{background:{sb};border:1.5px solid {sc}33;border-radius:16px;padding:18px 20px;display:flex;align-items:center;gap:16px}}
.status-dot{{width:12px;height:12px;border-radius:50%;background:{sc};flex-shrink:0;box-shadow:0 0 8px {sc}}}
.status-info{{flex:1}}
.status-label{{font-size:1.05rem;font-weight:700;color:{sc}}}
.status-sub{{font-size:.78rem;color:var(--sub);margin-top:2px}}
.ios-toggle{{position:relative;width:51px;height:31px;flex-shrink:0;cursor:pointer}}
.ios-toggle input{{display:none}}
.ios-knob{{position:absolute;inset:0;background:#39393D;border-radius:31px;transition:.25s}}
.ios-knob:before{{content:'';position:absolute;width:27px;height:27px;background:#fff;border-radius:50%;top:2px;left:2px;transition:.25s;box-shadow:0 2px 6px rgba(0,0,0,.4)}}
.ios-toggle input:checked+.ios-knob{{background:var(--green)}}
.ios-toggle input:checked+.ios-knob:before{{transform:translateX(20px)}}
.ios-toggle.sm{{width:42px;height:26px}}
.ios-toggle.sm .ios-knob:before{{width:22px;height:22px}}
.ios-toggle.sm input:checked+.ios-knob:before{{transform:translateX(16px)}}

/* Pause banner */
.pause-banner{{background:rgba(255,159,10,.15);border:1px solid #FF9F0A44;border-radius:12px;padding:10px 16px;font-size:.85rem;color:#FF9F0A;text-align:center}}

/* Quick buttons */

/* Counters */
.cnt-grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:14px}}
.cnt-card{{background:var(--card2);border-radius:12px;padding:14px;text-decoration:none;color:var(--text);display:block;-webkit-tap-highlight-color:transparent;border:1px solid transparent;transition:.15s}}
.cnt-card:active{{opacity:.75}}
.cnt-header{{display:flex;align-items:center;gap:8px;margin-bottom:10px;font-size:.9rem;font-weight:600}}
.cnt-name{{color:var(--accent)}}
.cnt-row{{display:flex;align-items:center;gap:6px;margin-bottom:5px}}
.cnt-label{{font-size:.7rem;color:var(--sub);width:52px;flex-shrink:0}}
.cnt-val{{font-size:.82rem;font-weight:700;width:24px;text-align:right;flex-shrink:0}}
.cnt-bar{{flex:1;height:4px;background:#38383A;border-radius:2px;overflow:hidden}}
.cnt-bar div{{height:100%;border-radius:2px;min-width:2px;transition:.4s}}

/* Schedule */
.sched-row{{display:flex;align-items:center;gap:12px;padding:13px 16px;border-bottom:1px solid var(--sep)}}
.sched-row:last-child{{border-bottom:none}}
.sday{{width:24px;font-size:.88rem;font-weight:600;color:var(--sub)}}
.sched-times{{flex:1;display:flex;align-items:center;gap:8px;justify-content:flex-end}}
.sched-times input[type=time]{{background:var(--card2);border:none;color:var(--text);padding:6px 10px;border-radius:8px;font-size:.82rem;width:82px;-webkit-appearance:none;color-scheme:dark}}

/* Clients */
.client-row{{display:flex;align-items:center;gap:12px;padding:13px 16px;border-bottom:1px solid var(--sep)}}
.client-row:last-child{{border-bottom:none}}
.client-ch{{font-size:1.3rem;text-decoration:none;flex-shrink:0}}
.client-info{{flex:1;min-width:0}}
.client-phone{{font-size:.88rem;font-weight:600}}
.client-preview{{font-size:.74rem;color:var(--sub);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}}
.client-meta{{text-align:right;flex-shrink:0}}
.client-count{{font-size:.78rem;font-weight:600}}
.client-time{{font-size:.7rem;color:var(--sub);margin-top:2px}}
.empty{{padding:28px;text-align:center;color:var(--sub);font-size:.88rem}}

/* Toast */
.toast{{position:fixed;bottom:calc(28px + env(safe-area-inset-bottom,0px));left:50%;transform:translateX(-50%) translateY(20px);background:#2C2C2E;color:#fff;padding:11px 20px;border-radius:14px;font-size:.85rem;font-weight:500;opacity:0;transition:.3s;white-space:nowrap;z-index:999;pointer-events:none;box-shadow:0 4px 20px rgba(0,0,0,.5)}}
.toast.show{{opacity:1;transform:translateX(-50%) translateY(0)}}
</style>
</head><body>

<div class="topbar">
  <div class="topbar-title"><span>R&amp;J</span> Grooming · Jarvis</div>
  <a href="/admin/logout" class="logout">Выйти</a>
</div>

<div class="wrap">

  {pause_info}

  <!-- Статус -->
  <div class="sec-title">Статус</div>
  <div class="status-card" id="status-card">
    <div class="status-dot" id="status-dot"></div>
    <div class="status-info">
      <div class="status-label" id="status-label">{status_text}</div>
      <div class="status-sub" id="status-sub">Нажмите тумблер для {"отключения" if status_key=="on" else "включения"}</div>
    </div>
    <label class="ios-toggle">
      <input type="checkbox" id="main-toggle" {"checked" if status_key=="on" else ""} onchange="toggleJarvis(this.checked)">
      <span class="ios-knob"></span>
    </label>
  </div>

  <!-- Счётчики -->
  <div class="sec-title">Обращения</div>
  <div class="card">
    <div class="cnt-grid">
      {counters_html}
    </div>
  </div>

  <!-- Расписание -->
  <div class="sec-title collapsible" onclick="toggleSection('sched')" style="cursor:pointer;display:flex;align-items:center;justify-content:space-between">
    <span>Расписание работы</span>
    <span id="sched-arrow" style="font-size:.9rem;transition:.2s">▾</span>
  </div>
  <div id="sched-section" class="card">
    {sched_html}
  </div>

  <!-- Клиенты -->
  <div class="sec-title">Клиенты — {len(clients)}</div>
  <div class="card">
    {clients_html}
  </div>

</div>

<div class="toast" id="toast"></div>

<script>
// ── Toast ──────────────────────────────────────────────────────────────
function toast(msg, color) {{
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.background = color || '#2C2C2E';
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2200);
}}

// ── Jarvis toggle ──────────────────────────────────────────────────────
function toggleJarvis(on) {{
  fetch('/admin/api/toggle', {{method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{enabled: on}})
  }}).then(r=>r.json()).then(d=>{{
    toast(d.message, on ? '#1a3d24' : '#3d1a1a');
    setTimeout(()=>location.reload(), 800);
  }});
}}

// ── Schedule ───────────────────────────────────────────────────────────
function saveSched(day) {{
  const en    = document.querySelector(`#srow-${{day}} input[type=checkbox]`).checked;
  const open  = document.getElementById(`open-${{day}}`).value;
  const close = document.getElementById(`close-${{day}}`).value;
  fetch('/admin/api/schedule', {{method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{day, enabled: en, open, close}})
  }}).then(r=>r.json()).then(d=>toast(d.message));
}}

function toggleSection(id) {{
  const el = document.getElementById(id + '-section');
  const arrow = document.getElementById(id + '-arrow');
  const hidden = el.style.display === 'none';
  el.style.display = hidden ? '' : 'none';
  arrow.style.transform = hidden ? '' : 'rotate(-90deg)';
}}

// ── Client mode ────────────────────────────────────────────────────────
function toggleClientMode(phone, jarvisOn) {{
  fetch('/admin/api/client-mode', {{method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{phone, mode: jarvisOn ? 'jarvis' : 'manual'}})
  }}).then(r=>r.json()).then(d=>toast(d.message));
}}

// ── Notification banner ────────────────────────────────────────────────
let notifAudio = null;
function playBeep() {{
  try {{
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain); gain.connect(ctx.destination);
    osc.frequency.value = 880;
    osc.type = 'sine';
    gain.gain.setValueAtTime(0.4, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.4);
    osc.start(); osc.stop(ctx.currentTime + 0.4);
  }} catch(e) {{}}
}}

function showBanner(phone, text) {{
  let b = document.getElementById('notif-banner');
  if (!b) {{
    b = document.createElement('div');
    b.id = 'notif-banner';
    b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:9999;background:#1C3A2A;border-bottom:2px solid #30D158;padding:14px 20px;display:flex;align-items:center;gap:12px;cursor:pointer;transform:translateY(-100%);transition:.3s';
    b.innerHTML = '<span style="font-size:1.4rem">💬</span><div style="flex:1"><div id="bn-phone" style="font-weight:700;font-size:.9rem;color:#30D158"></div><div id="bn-text" style="font-size:.82rem;color:#aaa;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"></div></div><span style="color:#666;font-size:1.2rem" onclick="closeBanner(event)">✕</span>';
    b.addEventListener('click', () => location.reload());
    document.body.appendChild(b);
  }}
  document.getElementById('bn-phone').textContent = phone;
  document.getElementById('bn-text').textContent = text;
  b.style.transform = 'translateY(0)';
  setTimeout(() => {{ if(b) b.style.transform = 'translateY(-100%)'; }}, 8000);
}}

function closeBanner(e) {{
  e.stopPropagation();
  const b = document.getElementById('notif-banner');
  if (b) b.style.transform = 'translateY(-100%)';
}}

// ── Auto-refresh with new message detection ────────────────────────────
let lastMsgTs = {{}};  // phone -> last known timestamp string

function initLastTs() {{
  document.querySelectorAll('[data-phone][data-ts]').forEach(el => {{
    lastMsgTs[el.dataset.phone] = el.dataset.ts;
  }});
}}

function pollMessages() {{
  fetch('/admin/api/messages')
    .then(r => r.json())
    .then(data => {{
      let gotNew = false;
      let newPhone = '', newText = '';
      data.forEach(m => {{
        const prev = lastMsgTs[m.phone];
        if (!prev || m.ts > prev) {{
          if (!gotNew) {{ newPhone = m.phone; newText = m.last_text; }}
          gotNew = true;
          lastMsgTs[m.phone] = m.ts;
        }}
      }});
      if (gotNew) {{
        playBeep();
        showBanner(newPhone, newText);
        setTimeout(() => location.reload(), 2000);
      }}
    }})
    .catch(() => {{}});
}}

initLastTs();
setInterval(pollMessages, 30000);
</script>
</body></html>"""

# ── Admin API endpoints ────────────────────────────────────────────────────
@app.route("/admin/api/toggle", methods=["POST"])
@login_required
def api_toggle():
    global jarvis_enabled, manual_mode, pause_until
    data = request.get_json()
    jarvis_enabled = bool(data.get("enabled", True))
    manual_mode    = False
    pause_until    = None
    status = "включён ✅" if jarvis_enabled else "выключен ❌"
    return jsonify({"ok": True, "message": f"Jarvis {status}"})

@app.route("/admin/api/quick", methods=["POST"])
@login_required
def api_quick():
    global jarvis_enabled, manual_mode, pause_until
    action = request.get_json().get("action")
    now = datetime.now()
    if action == "pause1h":
        pause_until = now + timedelta(hours=1)
        jarvis_enabled = True
        manual_mode = False
        return jsonify({"ok": True, "message": f"⏸ Пауза до {pause_until.strftime('%H:%M')}"})
    elif action == "pausemorning":
        morning = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if now.hour >= 9:
            morning += timedelta(days=1)
        pause_until = morning
        jarvis_enabled = True
        manual_mode = False
        return jsonify({"ok": True, "message": f"🌙 Пауза до {morning.strftime('%d.%m %H:%M')}"})
    elif action == "manual":
        manual_mode = True
        pause_until = None
        return jsonify({"ok": True, "message": "✋ Ручной режим включён"})
    elif action == "resume":
        manual_mode = False
        pause_until = None
        jarvis_enabled = True
        return jsonify({"ok": True, "message": "▶️ Jarvis возобновил работу"})
    return jsonify({"ok": False, "message": "Неизвестное действие"})

@app.route("/admin/api/schedule", methods=["POST"])
@login_required
def api_schedule():
    data = request.get_json()
    day = data.get("day")
    if day in schedule:
        schedule[day]["enabled"] = bool(data.get("enabled", True))
        schedule[day]["open"]    = data.get("open", "09:00")
        schedule[day]["close"]   = data.get("close", "18:00")
    return jsonify({"ok": True, "message": f"✅ {DAY_RU.get(day, day)} сохранён"})

@app.route("/admin/api/client-mode", methods=["POST"])
@login_required
def api_client_mode():
    data  = request.get_json()
    phone = data.get("phone")
    mode  = data.get("mode", "jarvis")
    if phone in clients:
        clients[phone]["mode"] = mode
    label = "Jarvis 🤖" if mode == "jarvis" else "Ручной ✋"
    return jsonify({"ok": True, "message": f"{phone}: {label}"})

@app.route("/admin/api/messages")
@login_required
def api_messages():
    result = []
    for phone, info in clients.items():
        last = info.get("last_seen")
        result.append({
            "phone": phone,
            "ts": last.isoformat() if isinstance(last, datetime) else "",
            "last_text": info.get("last_text", ""),
            "channel": info.get("channel", "whatsapp"),
        })
    return jsonify(result)

# ── WhatsApp ───────────────────────────────────────────────────────────────
def send_whatsapp(to, text):
    url = "https://graph.facebook.com/v18.0/" + WHATSAPP_PHONE_ID + "/messages"
    headers = {"Authorization": "Bearer " + WHATSAPP_TOKEN, "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=data)

def track_client(phone, channel, text):
    if phone not in clients:
        clients[phone] = {"channel": channel, "timestamps": [], "last_seen": None, "last_text": "", "mode": "jarvis"}
    now = datetime.now()
    clients[phone]["timestamps"].append(now)
    clients[phone]["last_seen"] = now
    clients[phone]["last_text"] = text

def should_reply(phone):
    global jarvis_enabled, pause_until, manual_mode
    if manual_mode:
        return False
    if pause_until and datetime.now() < pause_until:
        return False
    if not jarvis_enabled:
        return False
    if clients.get(phone, {}).get("mode") == "manual":
        return False
    return True

# ── Webhook ────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["GET"])
def verify():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print("Incoming:", str(data)[:200])
    try:
        value    = data["entry"][0]["changes"][0]["value"]
        channel  = "whatsapp"
        metadata = str(value.get("metadata", {})).lower()
        if "instagram" in metadata:
            channel = "instagram"
        elif "facebook" in metadata:
            channel = "facebook"

        messages = value.get("messages", [])
        if not messages:
            return "ok", 200

        msg  = messages[0]
        phone = msg["from"]
        text  = msg.get("text", {}).get("body", "")
        if not text:
            return "ok", 200

        track_client(phone, channel, text)
        print(f"From: {phone} [{channel}] Text: {text}")

        if not should_reply(phone):
            print("Jarvis skipped reply")
            return "ok", 200

        if phone not in conversation_history:
            conversation_history[phone] = []
        history = conversation_history[phone]
        history.append({"role": "user", "content": text})
        if len(history) > MAX_HISTORY:
            history = history[-MAX_HISTORY:]
            conversation_history[phone] = history

        response = client_ai.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=history
        )
        reply = response.content[0].text
        history.append({"role": "assistant", "content": reply})
        send_whatsapp(phone, reply)
        print("Sent:", reply[:80])
    except Exception as e:
        print("Error:", str(e))
    return "ok", 200

# ── Public ─────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return "MyDeal Jarvis rabotaet!"

@app.route("/privacy")
def privacy():
    return "Privacy Policy: We do not store or share your personal data."

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
