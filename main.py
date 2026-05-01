from flask import Flask, request, jsonify, session, redirect
import anthropic
import os
import requests
from datetime import datetime, timedelta
from functools import wraps
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "rjgrooming-secret-2024")
client_ai = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

WHATSAPP_TOKEN    = os.environ.get("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID")
VERIFY_TOKEN           = os.environ.get("WHATSAPP_VERIFY_TOKEN")
INSTAGRAM_VERIFY_TOKEN = os.environ.get("INSTAGRAM_VERIFY_TOKEN", "mydeal13")
INSTAGRAM_TOKEN   = os.environ.get("INSTAGRAM_TOKEN", os.environ.get("WHATSAPP_TOKEN"))

# ── Twilio ─────────────────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "AC530b60029911e804768fa97eab1221e7")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN",  "0e4c4126a6d5e4bb667f508ff123c755")
TWILIO_WA_NUMBER   = os.environ.get("TWILIO_WA_NUMBER",   "whatsapp:+37258735456")
twilio_client      = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ── State ──────────────────────────────────────────────────────────────────
conversation_history = {}
MAX_HISTORY = 20

jarvis_enabled   = True
instagram_enabled = True
pause_until      = None
manual_mode      = False

schedule = {
    "Mon": {"enabled": True,  "open": "09:00", "close": "18:00"},
    "Tue": {"enabled": True,  "open": "09:00", "close": "18:00"},
    "Wed": {"enabled": True,  "open": "09:00", "close": "18:00"},
    "Thu": {"enabled": True,  "open": "09:00", "close": "18:00"},
    "Fri": {"enabled": True,  "open": "09:00", "close": "18:00"},
    "Sat": {"enabled": True,  "open": "10:00", "close": "16:00"},
    "Sun": {"enabled": False, "open": "10:00", "close": "14:00"},
}

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
- Online booking: https://n1425894.alteg.io"""

# ── Helpers ────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        return f(*args, **kwargs)
    return decorated

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

def track_client(phone, channel, text):
    if phone not in clients:
        clients[phone] = {"channel": channel, "timestamps": [], "last_seen": None, "last_text": "", "mode": "jarvis"}
    now = datetime.now()
    clients[phone]["timestamps"].append(now)
    clients[phone]["last_seen"] = now
    clients[phone]["last_text"] = text

def should_reply(phone, channel="whatsapp"):
    global jarvis_enabled, instagram_enabled, pause_until, manual_mode
    if manual_mode:
        return False
    if pause_until and datetime.now() < pause_until:
        return False
    if not jarvis_enabled:
        return False
    if channel == "instagram" and not instagram_enabled:
        return False
    if clients.get(phone, {}).get("mode") == "manual":
        return False
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
    reply = response.content[0].text
    history.append({"role": "assistant", "content": reply})
    return reply

# ── Send functions ─────────────────────────────────────────────────────────
def send_whatsapp_meta(to, text):
    """Send via Meta WhatsApp Business API"""
    url = "https://graph.facebook.com/v18.0/" + WHATSAPP_PHONE_ID + "/messages"
    headers = {"Authorization": "Bearer " + WHATSAPP_TOKEN, "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=data)

def send_whatsapp_twilio(to, text):
    """Send via Twilio WhatsApp"""
    try:
        twilio_client.messages.create(
            from_=TWILIO_WA_NUMBER,
            body=text,
            to=f"whatsapp:{to}"
        )
    except Exception as e:
        print(f"Twilio send error: {e}")

INSTAGRAM_ACCOUNT_ID = "17841479914115449"

def send_instagram(to, text):
    url = "https://graph.facebook.com/v18.0/" + INSTAGRAM_ACCOUNT_ID + "/messages"
    headers = {"Authorization": "Bearer " + INSTAGRAM_TOKEN, "Content-Type": "application/json"}
    data = {"recipient": {"id": to}, "message": {"text": text}}
    requests.post(url, headers=headers, json=data)

def handle_message(sender_id, text, channel):
    track_client(sender_id, channel, text)
    print(f"From: {sender_id} [{channel}] Text: {text}")

    if not should_reply(sender_id, channel):
        print("Jarvis skipped reply")
        return None

    reply = get_ai_reply(sender_id, text)

    if channel == "instagram":
        send_instagram(sender_id, reply)
    elif channel == "twilio_whatsapp":
        pass  # reply returned for TwiML response
    else:
        send_whatsapp_meta(sender_id, reply)

    print("Sent:", reply[:80])
    return reply

# ── Twilio WhatsApp Webhook ────────────────────────────────────────────────
@app.route("/twilio/whatsapp", methods=["POST"])
def twilio_whatsapp():
    """Receive WhatsApp messages via Twilio"""
    from_number = request.form.get("From", "")  # format: whatsapp:+37258735456
    body        = request.form.get("Body", "")
    
    # Clean phone number
    phone = from_number.replace("whatsapp:", "").strip()
    
    print(f"Twilio WA from {phone}: {body}")
    
    resp = MessagingResponse()
    
    if phone and body:
        reply = handle_message(phone, body, "twilio_whatsapp")
        if reply:
            resp.message(reply)
    
    return str(resp), 200, {"Content-Type": "text/xml"}

# ── Meta WhatsApp & Instagram Webhook ─────────────────────────────────────
@app.route("/webhook", methods=["GET"])
def verify():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token in (VERIFY_TOKEN, INSTAGRAM_VERIFY_TOKEN):
        return challenge, 200
    return "Forbidden", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print("Incoming:", str(data)[:300])
    try:
        obj   = data.get("object", "")
        entry = data.get("entry", [{}])[0]

        if obj == "instagram" or "messaging" in entry:
            for msg_event in entry.get("messaging", []):
                sender_id    = msg_event.get("sender", {}).get("id", "")
                recipient_id = msg_event.get("recipient", {}).get("id", "")
                if sender_id == recipient_id:
                    continue
                msg = msg_event.get("message", {})
                if msg.get("is_echo"):
                    continue
                text = msg.get("text", "")
                if sender_id and text:
                    handle_message(sender_id, text, "instagram")
            return "ok", 200

        value    = entry.get("changes", [{}])[0].get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return "ok", 200
        msg   = messages[0]
        phone = msg["from"]
        text  = msg.get("text", {}).get("body", "")
        if text:
            handle_message(phone, text, "whatsapp")

    except Exception as e:
        print("Error:", str(e))
    return "ok", 200

# ── Admin ──────────────────────────────────────────────────────────────────
DAY_RU   = {"Mon":"Пн","Tue":"Вт","Wed":"Ср","Thu":"Чт","Fri":"Пт","Sat":"Сб","Sun":"Вс"}
CH_LINKS = {"whatsapp":"https://wa.me/","instagram":"https://instagram.com/rj_grooming","facebook":"https://www.facebook.com/messages/","calls":"tel:"}
CH_COLOR = {"whatsapp":"#25D366","instagram":"#E1306C","facebook":"#1877F2","calls":"#FF9F0A"}
CH_ICON  = {"whatsapp":"📱","instagram":"📸","facebook":"💬","calls":"📞"}

@app.route("/admin")
@login_required
def admin():
    return "<h1>Jarvis Admin</h1><p>Working!</p>"

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

@app.route("/admin/api/toggle-instagram", methods=["POST"])
@login_required
def api_toggle_instagram():
    global instagram_enabled
    instagram_enabled = bool(request.get_json().get("enabled", True))
    status = "включён ✅" if instagram_enabled else "выключен ❌"
    return jsonify({"ok": True, "message": f"Instagram {status}"})

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

# ── Public ─────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return "MyDeal Jarvis rabotaet!"

@app.route("/privacy")
def privacy():
    return "<h1>Privacy Policy</h1><p>R&J Grooming, Tallinn, Estonia</p>"

@app.route("/terms")
def terms():
    return "<h1>Terms of Service</h1><p>R&J Grooming, Tallinn, Estonia</p>"


BOOKING_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="theme-color" content="#1c1c18">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>R&J Grooming</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,600;1,400&family=Montserrat:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{min-height:100vh;background:#1c1c18;color:#c8c2b8;font-family:'Montserrat',sans-serif;font-weight:300}
.screen{display:none;min-height:100vh;flex-direction:column;align-items:center;padding:40px 0 48px}
.screen.active{display:flex}
.con{width:100%;max-width:400px;padding:0 28px}
.back-btn{display:flex;align-items:center;gap:8px;color:#8a8a55;font-size:.62rem;letter-spacing:.18em;text-transform:uppercase;background:none;border:none;cursor:pointer;padding:0;margin-bottom:24px;font-family:'Montserrat',sans-serif}
.logo-rj{font-family:'Cormorant Garamond',serif;font-size:2rem;font-weight:600;color:#e8e0d0}
.logo-sub{font-size:.46rem;font-weight:600;letter-spacing:.4em;text-transform:uppercase;color:#8a8a55;margin-top:3px;padding-bottom:14px;border-bottom:1px solid rgba(201,168,76,.2);margin-bottom:20px}
.home-rj{font-family:'Cormorant Garamond',serif;font-size:2.6rem;font-weight:600;color:#e8e0d0;line-height:1}
.logo-tag{font-size:.52rem;letter-spacing:.12em;text-transform:uppercase;color:#666660;line-height:1.5}
.logo-row{display:flex;align-items:flex-end;gap:12px;margin-bottom:4px;padding-bottom:18px;border-bottom:1px solid rgba(201,168,76,.2)}
.home-gsub{font-size:.46rem;font-weight:600;letter-spacing:.4em;text-transform:uppercase;color:#8a8a55;margin-top:6px;margin-bottom:22px}
.home-h1{font-family:'Cormorant Garamond',serif;font-size:2.2rem;font-weight:600;color:#d8d0c0;line-height:1.1;margin-bottom:5px}
.home-h1 em{font-style:italic;color:#c9a84c}
.home-sub{font-size:.62rem;letter-spacing:.15em;text-transform:uppercase;color:#555550;margin-bottom:22px}
.opt{display:flex;align-items:center;gap:16px;padding:15px 0;border-bottom:1px solid rgba(255,255,255,.06);text-decoration:none;color:#c8c2b8;transition:all .2s;cursor:pointer;background:none;border-top:none;border-left:none;border-right:none;width:100%;font-family:'Montserrat',sans-serif}
.opt:hover{color:#c9a84c;padding-left:6px}
.opt-icon{width:40px;height:40px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.opt-text{flex:1;text-align:left}
.opt-title{font-size:.88rem;font-weight:500;color:#9a9590;margin-bottom:2px;transition:color .2s}
.opt:hover .opt-title{color:#c9a84c}
.opt-handle{font-size:.68rem;color:#555550}
.opt-arrow{color:#8a8a55;font-size:.9rem;flex-shrink:0}
.divider{display:flex;align-items:center;gap:12px;padding:10px 0}
.divider::before,.divider::after{content:'';flex:1;height:1px;background:rgba(255,255,255,.06)}
.divider span{font-size:.52rem;letter-spacing:.2em;text-transform:uppercase;color:#444440}
.home-foot{margin-top:28px;padding-top:18px;border-top:1px solid rgba(201,168,76,.12);display:flex;justify-content:space-between;align-items:center}
.home-foot span{font-size:.58rem;letter-spacing:.18em;text-transform:uppercase;color:#444440}
.fdot{width:3px;height:3px;border-radius:50%;background:#6b6b42}
.progress{display:flex;align-items:center;margin-bottom:24px;overflow:hidden}
.ps{display:flex;align-items:center;gap:4px;font-size:.52rem;letter-spacing:.1em;text-transform:uppercase;color:#444440;white-space:nowrap}
.ps.done{color:#8a8a55}.ps.active{color:#c9a84c}
.pdot{width:5px;height:5px;border-radius:50%;background:#2a2a24;flex-shrink:0}
.ps.done .pdot{background:#8a8a55}.ps.active .pdot{background:#c9a84c}
.pl{flex:1;height:1px;background:#2a2a24;margin:0 5px;min-width:8px}
.pl.done{background:#8a8a55}
.step{display:none}.step.show{display:block;animation:fu .4s ease both}
.slbl{font-size:.56rem;letter-spacing:.22em;text-transform:uppercase;color:#c9a84c;margin-bottom:10px;font-weight:500}
.sbox{display:flex;align-items:center;gap:10px;background:#141410;border:1px solid rgba(201,168,76,.3);padding:0 14px}
.sbox:focus-within{border-color:#c9a84c}
.si{opacity:.4;font-size:.85rem;flex-shrink:0}
#bInput{flex:1;background:transparent;border:none;outline:none;font-family:'Montserrat',sans-serif;font-size:.85rem;color:#c8c2b8;padding:13px 0}
#bInput::placeholder{color:#444440}
.clr{background:none;border:none;color:#555550;cursor:pointer;font-size:.8rem;display:none}
.clr.show{display:block}
.bwrap{position:relative;margin-bottom:8px}
.drop{position:absolute;left:0;right:0;background:#141410;border:1px solid rgba(201,168,76,.25);border-top:none;max-height:200px;overflow-y:auto;z-index:50;display:none}
.drop.open{display:block}
.ditem{padding:11px 14px;font-size:.8rem;color:#c8c2b8;cursor:pointer;border-bottom:1px solid rgba(255,255,255,.04)}
.ditem:hover{background:rgba(201,168,76,.1);color:#c9a84c}
.ditem mark{background:transparent;color:#c9a84c;font-weight:600}
.nores{padding:14px;font-size:.75rem;color:#555550;font-style:italic}
.sbadge{display:none;align-items:center;gap:10px;margin-bottom:16px}
.sbadge.show{display:flex}
.bname{background:rgba(201,168,76,.1);border:1px solid rgba(201,168,76,.3);color:#c9a84c;padding:4px 12px;font-size:.7rem}
.bchg{font-size:.6rem;color:#555550;cursor:pointer;text-decoration:underline}
.svbtn{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:13px 16px;background:#141410;border:1px solid rgba(255,255,255,.07);border-left:2px solid transparent;color:#c8c2b8;font-family:'Montserrat',sans-serif;font-size:.8rem;cursor:pointer;text-align:left;transition:all .2s;width:100%;margin-bottom:2px}
.svbtn:hover,.svbtn.active{border-left-color:#c9a84c;color:#c9a84c;background:#1a1a14}
.svp{font-weight:600;color:#c9a84c;flex-shrink:0}
.masters{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.mbtn{background:#141410;border:1px solid rgba(255,255,255,.08);padding:16px 12px;text-align:center;cursor:pointer;transition:all .2s;font-family:'Montserrat',sans-serif}
.mbtn:hover,.mbtn.active{border-color:#c9a84c;background:rgba(201,168,76,.08)}
.mav{width:46px;height:46px;border-radius:50%;background:rgba(201,168,76,.15);border:1px solid rgba(201,168,76,.3);display:flex;align-items:center;justify-content:center;margin:0 auto 10px;font-family:'Cormorant Garamond',serif;font-size:1.2rem;font-weight:600;color:#c9a84c}
.mbtn.active .mav{background:rgba(201,168,76,.25);border-color:#c9a84c}
.mname{font-size:.8rem;font-weight:500;color:#c8c2b8}
.mbtn.active .mname{color:#c9a84c}
.mtitle{font-size:.58rem;color:#555550;margin-top:2px}
.gbtn{display:flex;align-items:center;padding:13px 16px;background:#141410;border:1px solid rgba(255,255,255,.07);border-left:2px solid transparent;color:#c8c2b8;font-family:'Montserrat',sans-serif;font-size:.82rem;cursor:pointer;width:100%;margin-bottom:4px;transition:all .2s}
.gbtn:hover,.gbtn.active{border-left-color:#c9a84c;color:#c9a84c;background:#1a1a14}
.cal-h{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.cal-m{font-family:'Cormorant Garamond',serif;font-size:1rem;color:#c8c2b8}
.cal-n{background:none;border:none;color:#8a8a55;cursor:pointer;font-size:.9rem;padding:4px 8px}
.cg{display:grid;grid-template-columns:repeat(7,1fr);gap:2px;margin-bottom:8px}
.cdn{text-align:center;font-size:.52rem;color:#444440;padding:4px 0;text-transform:uppercase}
.cd{text-align:center;padding:8px 4px;font-size:.78rem;cursor:pointer;color:#888880;border:1px solid transparent;transition:all .2s}
.cd:hover:not(.dis),.cd.sel{background:rgba(201,168,76,.12);border-color:rgba(201,168,76,.4);color:#c9a84c}
.cd.tod{color:#c9a84c;font-weight:600}
.cd.dis{color:#2a2a24;cursor:default}
.tg{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
.tbtn{background:#141410;border:1px solid rgba(255,255,255,.08);padding:10px 4px;text-align:center;font-size:.72rem;color:#c8c2b8;cursor:pointer;font-family:'Montserrat',sans-serif;transition:all .2s}
.tbtn:hover,.tbtn.active{border-color:#c9a84c;background:rgba(201,168,76,.08);color:#c9a84c}
.sum{background:#141410;border:1px solid rgba(201,168,76,.2);border-top:2px solid #c9a84c;padding:20px;margin-bottom:14px}
.sr{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid rgba(255,255,255,.05);font-size:.78rem}
.sr:last-child{border-bottom:none;padding-top:10px}
.sl{color:#666660}.sv{color:#c8c2b8;font-weight:500;text-align:right}
.sp{font-family:'Cormorant Garamond',serif;font-size:1.5rem;color:#c9a84c;font-weight:600}
.fg{margin-bottom:12px}
.fl{font-size:.56rem;letter-spacing:.2em;text-transform:uppercase;color:#c9a84c;margin-bottom:6px;display:block}
.fi{width:100%;background:#141410;border:1px solid rgba(255,255,255,.1);color:#c8c2b8;font-family:'Montserrat',sans-serif;font-size:.85rem;padding:12px 14px;outline:none}
.fi:focus{border-color:#c9a84c}
.cbtn{display:block;width:100%;background:#4a4a2e;color:#c8c2b8;font-family:'Montserrat',sans-serif;font-size:.68rem;font-weight:600;letter-spacing:.25em;text-transform:uppercase;text-align:center;padding:16px;border:none;cursor:pointer}
.cbtn:hover{background:#6b6b42}
.sblock{text-align:center;padding:40px 20px;display:none}
.sblock.show{display:block;animation:fu .5s ease both}
.si2{font-size:3rem;margin-bottom:16px}
.st{font-family:'Cormorant Garamond',serif;font-size:1.6rem;color:#c9a84c;margin-bottom:8px}
.ss{font-size:.78rem;color:#777770;line-height:1.6;margin-bottom:24px}
.hbtn{background:transparent;border:1px solid rgba(201,168,76,.3);color:#c9a84c;font-family:'Montserrat',sans-serif;font-size:.65rem;letter-spacing:.2em;text-transform:uppercase;padding:12px 24px;cursor:pointer}
@keyframes fu{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}
</style>
</head>
<body>

<!-- HOME -->
<div class="screen active" id="homeScreen">
<div class="con">
  <div class="logo-row">
    <div class="home-rj">R&amp;J</div>
    <div class="logo-tag">Премиальная груминг-<br>студия в Таллине</div>
  </div>
  <div class="home-gsub">Grooming</div>
  <div class="home-h1">Book the way <em>you like</em></div>
  <div class="home-sub">Choose how to connect</div>

  <a href="https://www.instagram.com/rj_grooming?igsh=MWxmdHNqcXFkanNvbQ==" target="_blank" class="opt">
    <div class="opt-icon"><svg width="36" height="36" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="ig" x1="0%" y1="100%" x2="100%" y2="0%"><stop offset="0%" stop-color="#f09433"/><stop offset="50%" stop-color="#dc2743"/><stop offset="100%" stop-color="#bc1888"/></linearGradient></defs><rect width="24" height="24" rx="6" fill="url(#ig)"/><rect x="6" y="6" width="12" height="12" rx="3" fill="none" stroke="white" stroke-width="1.5"/><circle cx="12" cy="12" r="3" fill="none" stroke="white" stroke-width="1.5"/><circle cx="16.5" cy="7.5" r="1" fill="white"/></svg></div>
    <div class="opt-text"><div class="opt-title">Instagram</div><div class="opt-handle">@rj_grooming</div></div>
    <span class="opt-arrow">→</span>
  </a>
  <a href="https://wa.me/37258735456" target="_blank" class="opt">
    <div class="opt-icon"><svg width="36" height="36" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><circle cx="12" cy="12" r="12" fill="#25D366"/><path fill="white" d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347"/></svg></div>
    <div class="opt-text"><div class="opt-title">WhatsApp</div><div class="opt-handle">+372 587 35456</div></div>
    <span class="opt-arrow">→</span>
  </a>
  <a href="https://www.facebook.com/share/1ELP6KC6rV/?mibextid=wwXIfr" target="_blank" class="opt">
    <div class="opt-icon"><svg width="36" height="36" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><circle cx="12" cy="12" r="12" fill="#1877F2"/><path fill="white" d="M13 10.5h2l.5-2.5H13V6.5c0-.7.2-1.5 1.5-1.5H16V3s-1-.2-2-.2c-2.1 0-3.5 1.3-3.5 3.5V8H8v2.5h2.5V18H13v-7.5z"/></svg></div>
    <div class="opt-text"><div class="opt-title">Facebook</div><div class="opt-handle">R&amp;J Grooming</div></div>
    <span class="opt-arrow">→</span>
  </a>
  <div class="divider"><span>or book directly</span></div>
  <button class="opt" id="bookBtn">
    <div class="opt-icon"><svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#c9a84c" stroke-width="1.6"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg></div>
    <div class="opt-text"><div class="opt-title">Book Online</div><div class="opt-handle">Порода → Услуга → Мастер → Время</div></div>
    <span class="opt-arrow">→</span>
  </button>
  <button class="opt" onclick="window.location.href='tel:+37258735456'">
    <div class="opt-icon"><svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#c9a84c" stroke-width="1.6"><path d="M22 16.92v3a2 2 0 01-2.18 2 19.79 19.79 0 01-8.63-3.07A19.5 19.5 0 013.07 9.82a19.79 19.79 0 01-3.07-8.67A2 2 0 012 1h3a2 2 0 012 1.72c.127.96.361 1.903.7 2.81a2 2 0 01-.45 2.11L6.91 8.91a16 16 0 006 6l1.27-1.27a2 2 0 012.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0122 16.92z"/></svg></div>
    <div class="opt-text"><div class="opt-title">Call Us</div><div class="opt-handle">+372 587 35456</div></div>
    <span class="opt-arrow">→</span>
  </button>
  <div class="home-foot">
    <span>Tallinn</span><div class="fdot"></div><span>Estonia</span><div class="fdot"></div><span>© 2025 R&amp;J</span>
  </div>
</div>
</div>

<!-- BOOKING -->
<div class="screen" id="bookScreen">
<div class="con">
  <button class="back-btn" id="backBtn">← Назад</button>
  <div class="logo-rj">R&amp;J</div>
  <div class="logo-sub">Grooming · Таллин</div>
  <div class="progress">
    <div class="ps active" id="ps1"><div class="pdot"></div>Услуга</div>
    <div class="pl" id="pl1"></div>
    <div class="ps" id="ps2"><div class="pdot"></div>Мастер</div>
    <div class="pl" id="pl2"></div>
    <div class="ps" id="ps3"><div class="pdot"></div>Питомец</div>
    <div class="pl" id="pl3"></div>
    <div class="ps" id="ps4"><div class="pdot"></div>Дата</div>
    <div class="pl" id="pl4"></div>
    <div class="ps" id="ps5"><div class="pdot"></div>Данные</div>
  </div>

  <!-- Step 1 -->
  <div class="step show" id="bk1">
    <div class="slbl">01 · Порода собаки</div>
    <div class="bwrap">
      <div class="sbox">
        <span class="si">🔍</span>
        <input id="bInput" type="text" placeholder="Начните вводить породу..." autocomplete="off">
        <button class="clr" id="clrBtn">✕</button>
      </div>
      <div class="drop" id="bDrop"></div>
    </div>
    <div class="sbadge" id="sBadge"></div>
    <div id="svcSec" style="display:none;margin-top:16px">
      <div class="slbl">02 · Услуга</div>
      <div id="svcList"></div>
    </div>
  </div>

  <!-- Step 2 -->
  <div class="step" id="bk2">
    <div class="slbl">Выберите мастера</div>
    <div class="masters">
      <div class="mbtn" data-master="Аня"><div class="mav">А</div><div class="mname">Аня</div><div class="mtitle">Грумер</div></div>
      <div class="mbtn" data-master="Катя"><div class="mav">К</div><div class="mname">Катя</div><div class="mtitle">Грумер</div></div>
      <div class="mbtn" data-master="Маша"><div class="mav">М</div><div class="mname">Маша</div><div class="mtitle">Грумер</div></div>
      <div class="mbtn" data-master="Зина"><div class="mav">З</div><div class="mname">Зина</div><div class="mtitle">Грумер</div></div>
    </div>
  </div>

  <!-- Step 3 -->
  <div class="step" id="bk3">
    <div class="slbl">Как давно были в груме?</div>
    <button class="gbtn" data-val="Первый раз">Первый раз</button>
    <button class="gbtn" data-val="1 месяц">1 месяц</button>
    <button class="gbtn" data-val="2 месяца">2 месяца</button>
    <button class="gbtn" data-val="3 месяца">3 месяца</button>
    <button class="gbtn" data-val="4 месяца">4 месяца</button>
    <button class="gbtn" data-val="5 месяцев">5 месяцев</button>
    <button class="gbtn" data-val="6 месяцев и более">6 месяцев и более</button>
  </div>

  <!-- Step 4 -->
  <div class="step" id="bk4">
    <div class="slbl">Выберите дату</div>
    <div class="cal-h">
      <button class="cal-n" id="prevM">‹</button>
      <div class="cal-m" id="calM"></div>
      <button class="cal-n" id="nextM">›</button>
    </div>
    <div class="cg" id="calG"></div>
    <div id="timeSec" style="display:none;margin-top:16px">
      <div class="slbl">Выберите время</div>
      <div class="tg" id="timeG"></div>
    </div>
  </div>

  <!-- Step 5 -->
  <div class="step" id="bk5">
    <div class="slbl">Ваши данные</div>
    <div class="fg"><label class="fl">Имя</label><input class="fi" id="cName" type="text" placeholder="Ваше имя"></div>
    <div class="fg"><label class="fl">Телефон</label><input class="fi" id="cPhone" type="tel" placeholder="+372 ..."></div>
    <div class="fg"><label class="fl">Кличка питомца</label><input class="fi" id="cPet" type="text" placeholder="Необязательно"></div>
    <div class="sum" id="sumBlock"></div>
    <button class="cbtn" id="confirmBtn">Подтвердить запись</button>
  </div>

  <!-- Success -->
  <div class="sblock" id="sucBlock">
    <div class="si2">🐾</div>
    <div class="st">Запись принята!</div>
    <div class="ss">Мы свяжемся с вами для подтверждения.<br>Спасибо, что выбрали R&J Grooming!</div>
    <button class="hbtn" id="homeBtn">← На главную</button>
  </div>
</div>
</div>

<script>
var DATA = [{"breed": "Акита-ину 20–40 кг", "services": {"Базовый уход": 60.0, "Гигиенический уход": 75.0, "Экспресс-линька": 90.0}}, {"breed": "Акита-ину более 40 кг", "services": {"Базовый уход": 85.0, "Гигиенический уход": 95.0, "Экспресс-линька": 115.0}}, {"breed": "Акита-ину флаффи 20–40 кг", "services": {"Базовый уход": 60.0, "Гигиенический уход": 75.0, "Экспресс-линька": 90.0}}, {"breed": "Акита-ину флаффи более 40 кг", "services": {"Базовый уход": 85.0, "Гигиенический уход": 95.0, "Экспресс-линька": 115.0}}, {"breed": "Американская акита 20–40 кг", "services": {"Базовый уход": 60.0, "Гигиенический уход": 75.0, "Экспресс-линька": 90.0}}, {"breed": "Американская акита более 40 кг", "services": {"Базовый уход": 85.0, "Гигиенический уход": 95.0, "Экспресс-линька": 115.0}}, {"breed": "Американская акита флаффи 20–40 кг", "services": {"Базовый уход": 60.0, "Гигиенический уход": 75.0, "Экспресс-линька": 90.0}}, {"breed": "Американская акита флаффи более 40 кг", "services": {"Базовый уход": 85.0, "Гигиенический уход": 95.0, "Экспресс-линька": 115.0}}, {"breed": "Алабай 40–60 кг", "services": {"Базовый уход": 85.0, "Гигиенический уход": 95.0, "Экспресс-линька": 115.0}}, {"breed": "Алабай более 60 кг", "services": {"Базовый уход": 100.0, "Гигиенический уход": 115.0, "Экспресс-линька": 130.0}}, {"breed": "Аляскинский маламут 20–40 кг", "services": {"Базовый уход": 60.0, "Гигиенический уход": 75.0, "Экспресс-линька": 90.0}}, {"breed": "Аляскинский маламут более 40 кг", "services": {"Базовый уход": 85.0, "Гигиенический уход": 95.0, "Экспресс-линька": 115.0}}, {"breed": "Аляскинский маламут флаффи 20–40 кг", "services": {"Базовый уход": 60.0, "Гигиенический уход": 75.0, "Экспресс-линька": 90.0}}, {"breed": "Аляскинский маламут флаффи более 40 кг", "services": {"Базовый уход": 85.0, "Гигиенический уход": 95.0, "Экспресс-линька": 115.0}}, {"breed": "Американский кокер-спаниель 10–15 кг", "services": {"Базовый уход": 40.0, "Гигиенический уход": 55.0, "Комплексный уход": 70.0}}, {"breed": "Американский кокер-спаниель 15–20 кг", "services": {"Базовый уход": 45.0, "Гигиенический уход": 65.0, "Комплексный уход": 80.0}}, {"breed": "Английский бульдог", "services": {"Базовый уход": 45.0, "Гигиенический уход": 55.0, "Экспресс-линька": 70.0}}, {"breed": "Английский кокер-спаниель 10–15 кг", "services": {"Базовый уход": 40.0, "Гигиенический уход": 55.0, "Комплексный уход": 70.0}}, {"breed": "Английский кокер-спаниель 15–20 кг", "services": {"Базовый уход": 45.0, "Гигиенический уход": 65.0, "Комплексный уход": 80.0}}, {"breed": "Афган 20–30 кг", "services": {"Базовый уход": 50.0, "Гигиенический уход": 70.0, "Комплексный уход": 90.0}}, {"breed": "Афган 30–40 кг", "services": {"Базовый уход": 60.0, "Гигиенический уход": 80.0, "Комплексный уход": 100.0}}, {"breed": "Бернский зенненхунд 30–40 кг", "services": {"Базовый уход": 70.0, "Гигиенический уход": 85.0, "Комплексный уход": 110.0, "Экспресс-линька": 100.0}}, {"breed": "Бернский зенненхунд более 40 кг", "services": {"Базовый уход": 85.0, "Гигиенический уход": 95.0, "Комплексный уход": 130.0, "Экспресс-линька": 115.0}}, {"breed": "Бигль 10–15 кг", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Экспресс-линька": 60.0}}, {"breed": "Бигль 15–20 кг", "services": {"Базовый уход": 40.0, "Гигиенический уход": 50.0, "Экспресс-линька": 65.0}}, {"breed": "Бишон-фризе до 5 кг", "services": {"Базовый уход": 30.0, "Гигиенический уход": 40.0, "Комплексный уход": 55.0}}, {"breed": "Бишон-фризе 5–10 кг", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Комплексный уход": 60.0}}, {"breed": "Бордер-колли 15–20 кг", "services": {"Базовый уход": 50.0, "Гигиенический уход": 70.0, "Комплексный уход": 90.0, "Экспресс-линька": 80.0}}, {"breed": "Бордер-колли 20–25 кг", "services": {"Базовый уход": 60.0, "Гигиенический уход": 75.0, "Комплексный уход": 100.0, "Экспресс-линька": 90.0}}, {"breed": "Брабансон", "services": {"Базовый уход": 30.0, "Гигиенический уход": 40.0, "Экспресс-линька": 50.0}}, {"breed": "Вельш-корги 10–15 кг", "services": {"Базовый уход": 45.0, "Гигиенический уход": 60.0, "Экспресс-линька": 70.0}}, {"breed": "Вельш-корги 15–20 кг", "services": {"Базовый уход": 50.0, "Гигиенический уход": 70.0, "Экспресс-линька": 80.0}}, {"breed": "Вест-хайленд-вайт-терьер", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Комплексный уход": 60.0, "Тримминг": 65.0}}, {"breed": "Голден-ретривер 20–30 кг", "services": {"Базовый уход": 60.0, "Гигиенический уход": 75.0, "Комплексный уход": 100.0, "Экспресс-линька": 90.0}}, {"breed": "Голден-ретривер 30–40 кг", "services": {"Базовый уход": 70.0, "Гигиенический уход": 85.0, "Комплексный уход": 110.0, "Экспресс-линька": 100.0}}, {"breed": "Гриффон", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Комплексный уход": 60.0, "Тримминг": 65.0}}, {"breed": "Джек-рассел-терьер гладкошерстный", "services": {"Базовый уход": 30.0, "Гигиенический уход": 40.0, "Экспресс-линька": 50.0}}, {"breed": "Джек-рассел-терьер жесткошерстный", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Комплексный уход": 60.0, "Тримминг": 65.0}}, {"breed": "Далматин", "services": {"Базовый уход": 45.0, "Гигиенический уход": 55.0, "Экспресс-линька": 70.0}}, {"breed": "Доберман 30–40 кг", "services": {"Базовый уход": 55.0, "Гигиенический уход": 65.0, "Экспресс-линька": 80.0}}, {"breed": "Доберман более 40 кг", "services": {"Базовый уход": 70.0, "Гигиенический уход": 80.0, "Экспресс-линька": 95.0}}, {"breed": "Ирландский мягкошерстный пшеничный терьер", "services": {"Базовый уход": 45.0, "Гигиенический уход": 65.0, "Комплексный уход": 80.0}}, {"breed": "Ирландский терьер", "services": {"Базовый уход": 40.0, "Гигиенический уход": 55.0, "Комплексный уход": 70.0, "Тримминг": 75.0}}, {"breed": "Испанский гальго 20–30 кг", "services": {"Базовый уход": 45.0, "Гигиенический уход": 55.0, "Экспресс-линька": 70.0}}, {"breed": "Испанский гальго 30–40 кг", "services": {"Базовый уход": 55.0, "Гигиенический уход": 65.0, "Экспресс-линька": 80.0}}, {"breed": "Йоркширский терьер до 3,5 кг", "services": {"Базовый уход": 30.0, "Гигиенический уход": 40.0, "Комплексный уход": 55.0}}, {"breed": "Йоркширский терьер более 3,5 кг", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Комплексный уход": 60.0}}, {"breed": "Бивер-йорк до 3,5 кг", "services": {"Базовый уход": 30.0, "Гигиенический уход": 40.0, "Комплексный уход": 55.0}}, {"breed": "Бивер-йорк более 3,5 кг", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Комплексный уход": 60.0}}, {"breed": "Кавалер-кинг-чарльз-спаниель 5–10 кг", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Комплексный уход": 60.0}}, {"breed": "Кавалер-кинг-чарльз-спаниель 10–15 кг", "services": {"Базовый уход": 40.0, "Гигиенический уход": 55.0, "Комплексный уход": 70.0}}, {"breed": "Китайская хохлатая пуховая до 5 кг", "services": {"Базовый уход": 30.0, "Гигиенический уход": 40.0, "Комплексный уход": 55.0}}, {"breed": "Китайская хохлатая пуховая 5–10 кг", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Комплексный уход": 60.0}}, {"breed": "Китайская хохлатая голая до 5 кг", "services": {"Базовый уход": 28.0, "Гигиенический уход": 35.0, "Комплексный уход": 55.0}}, {"breed": "Китайская хохлатая голая 5–10 кг", "services": {"Базовый уход": 32.0, "Гигиенический уход": 42.0, "Комплексный уход": 60.0}}, {"breed": "Колли 20–30 кг", "services": {"Базовый уход": 60.0, "Гигиенический уход": 75.0, "Комплексный уход": 100.0, "Экспресс-линька": 90.0}}, {"breed": "Колли 30–40 кг", "services": {"Базовый уход": 70.0, "Гигиенический уход": 85.0, "Комплексный уход": 110.0, "Экспресс-линька": 100.0}}, {"breed": "Комондор 30–40 кг", "services": {"Базовый уход": 70.0, "Гигиенический уход": 85.0, "Комплексный уход": 110.0}}, {"breed": "Комондор более 40 кг", "services": {"Базовый уход": 85.0, "Гигиенический уход": 95.0, "Комплексный уход": 130.0}}, {"breed": "Лабрадор гладкошерстный 20–30 кг", "services": {"Базовый уход": 45.0, "Гигиенический уход": 55.0, "Экспресс-линька": 70.0}}, {"breed": "Лабрадор гладкошерстный 30–40 кг", "services": {"Базовый уход": 55.0, "Гигиенический уход": 65.0, "Экспресс-линька": 80.0}}, {"breed": "Лабрадор гладкошерстный более 40 кг", "services": {"Базовый уход": 70.0, "Гигиенический уход": 80.0, "Экспресс-линька": 95.0}}, {"breed": "Лабрадор длинношерстный 20–30 кг", "services": {"Базовый уход": 60.0, "Гигиенический уход": 75.0, "Комплексный уход": 100.0, "Экспресс-линька": 90.0}}, {"breed": "Лабрадор длинношерстный 30–40 кг", "services": {"Базовый уход": 70.0, "Гигиенический уход": 85.0, "Комплексный уход": 110.0, "Экспресс-линька": 100.0}}, {"breed": "Лабрадор длинношерстный более 40 кг", "services": {"Базовый уход": 85.0, "Гигиенический уход": 95.0, "Комплексный уход": 130.0, "Экспресс-линька": 115.0}}, {"breed": "Лабрадудель 10–20 кг", "services": {"Базовый уход": 40.0, "Гигиенический уход": 55.0, "Комплексный уход": 70.0}}, {"breed": "Лабрадудель 20–30 кг", "services": {"Базовый уход": 50.0, "Гигиенический уход": 70.0, "Комплексный уход": 90.0}}, {"breed": "Лабрадудель 30–40 кг", "services": {"Базовый уход": 60.0, "Гигиенический уход": 80.0, "Комплексный уход": 100.0}}, {"breed": "Мальтипу до 5 кг", "services": {"Базовый уход": 30.0, "Гигиенический уход": 40.0, "Комплексный уход": 55.0}}, {"breed": "Мальтипу 5–10 кг", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Комплексный уход": 60.0}}, {"breed": "Мальтипу 10–15 кг", "services": {"Базовый уход": 40.0, "Гигиенический уход": 55.0, "Комплексный уход": 70.0}}, {"breed": "Мальтезе", "services": {"Базовый уход": 30.0, "Гигиенический уход": 40.0, "Комплексный уход": 55.0}}, {"breed": "Миттельшнауцер 10–15 кг", "services": {"Базовый уход": 40.0, "Гигиенический уход": 55.0, "Комплексный уход": 70.0, "Тримминг": 75.0}}, {"breed": "Миттельшнауцер 15–20 кг", "services": {"Базовый уход": 45.0, "Гигиенический уход": 65.0, "Комплексный уход": 80.0, "Тримминг": 85.0}}, {"breed": "Мопс", "services": {"Базовый уход": 30.0, "Гигиенический уход": 40.0, "Экспресс-линька": 50.0}}, {"breed": "Невская орхидея", "services": {"Базовый уход": 30.0, "Гигиенический уход": 40.0, "Комплексный уход": 55.0}}, {"breed": "Немецкая овчарка 20–30 кг", "services": {"Базовый уход": 60.0, "Гигиенический уход": 75.0, "Экспресс-линька": 90.0}}, {"breed": "Немецкая овчарка 30–40 кг", "services": {"Базовый уход": 70.0, "Гигиенический уход": 85.0, "Экспресс-линька": 100.0}}, {"breed": "Немецкая овчарка более 40 кг", "services": {"Базовый уход": 85.0, "Гигиенический уход": 95.0, "Экспресс-линька": 115.0}}, {"breed": "Норвич-терьер", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Комплексный уход": 60.0, "Тримминг": 65.0}}, {"breed": "Норфолк-терьер", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Комплексный уход": 60.0, "Тримминг": 65.0}}, {"breed": "Ньюфаундленд 40–60 кг", "services": {"Базовый уход": 85.0, "Гигиенический уход": 95.0, "Комплексный уход": 130.0, "Экспресс-линька": 115.0}}, {"breed": "Ньюфаундленд более 60 кг", "services": {"Базовый уход": 100.0, "Гигиенический уход": 115.0, "Комплексный уход": 150.0, "Экспресс-линька": 130.0}}, {"breed": "Папийон", "services": {"Базовый уход": 30.0, "Гигиенический уход": 40.0, "Комплексный уход": 55.0}}, {"breed": "Пекинес до 5 кг", "services": {"Базовый уход": 30.0, "Гигиенический уход": 40.0, "Комплексный уход": 55.0}}, {"breed": "Пекинес 5–10 кг", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Комплексный уход": 60.0}}, {"breed": "Пудель той до 5 кг", "services": {"Базовый уход": 30.0, "Гигиенический уход": 40.0, "Комплексный уход": 55.0}}, {"breed": "Пудель карликовый 5–10 кг", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Комплексный уход": 60.0}}, {"breed": "Пудель малый 10–15 кг", "services": {"Базовый уход": 40.0, "Гигиенический уход": 55.0, "Комплексный уход": 70.0}}, {"breed": "Пудель малый 15–20 кг", "services": {"Базовый уход": 45.0, "Гигиенический уход": 65.0, "Комплексный уход": 80.0}}, {"breed": "Пудель большой 20–30 кг", "services": {"Базовый уход": 50.0, "Гигиенический уход": 70.0, "Комплексный уход": 90.0}}, {"breed": "Пудель большой 30–40 кг", "services": {"Базовый уход": 60.0, "Гигиенический уход": 80.0, "Комплексный уход": 100.0}}, {"breed": "Ризеншнауцер 30–40 кг", "services": {"Базовый уход": 60.0, "Гигиенический уход": 80.0, "Комплексный уход": 100.0, "Тримминг": 110.0}}, {"breed": "Ризеншнауцер более 40 кг", "services": {"Базовый уход": 75.0, "Гигиенический уход": 95.0, "Комплексный уход": 120.0, "Тримминг": 125.0}}, {"breed": "Русский охотничий спаниель 10–15 кг", "services": {"Базовый уход": 40.0, "Гигиенический уход": 55.0, "Комплексный уход": 70.0}}, {"breed": "Русский охотничий спаниель 15–20 кг", "services": {"Базовый уход": 45.0, "Гигиенический уход": 65.0, "Комплексный уход": 80.0}}, {"breed": "Русский той гладкошерстный", "services": {"Базовый уход": 25.0, "Гигиенический уход": 35.0}}, {"breed": "Русский той длинношерстный", "services": {"Базовый уход": 30.0, "Гигиенический уход": 40.0, "Комплексный уход": 55.0}}, {"breed": "Русская цветная болонка", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Комплексный уход": 60.0}}, {"breed": "Русский черный терьер 30–40 кг", "services": {"Базовый уход": 60.0, "Гигиенический уход": 80.0, "Комплексный уход": 100.0}}, {"breed": "Русский черный терьер более 40 кг", "services": {"Базовый уход": 75.0, "Гигиенический уход": 95.0, "Комплексный уход": 120.0}}, {"breed": "Самоед 20–30 кг", "services": {"Базовый уход": 60.0, "Гигиенический уход": 75.0, "Экспресс-линька": 90.0}}, {"breed": "Самоед 30–40 кг", "services": {"Базовый уход": 70.0, "Гигиенический уход": 85.0, "Экспресс-линька": 100.0}}, {"breed": "Сеттер английский 20–30 кг", "services": {"Базовый уход": 50.0, "Гигиенический уход": 70.0, "Комплексный уход": 90.0}}, {"breed": "Сеттер ирландский 20–30 кг", "services": {"Базовый уход": 50.0, "Гигиенический уход": 70.0, "Комплексный уход": 90.0}}, {"breed": "Сеттер гордон 30–40 кг", "services": {"Базовый уход": 60.0, "Гигиенический уход": 80.0, "Комплексный уход": 100.0}}, {"breed": "Сиба-ину", "services": {"Базовый уход": 45.0, "Гигиенический уход": 60.0, "Экспресс-линька": 70.0}}, {"breed": "Скотч-терьер", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Комплексный уход": 60.0, "Тримминг": 65.0}}, {"breed": "Силихем-терьер", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Комплексный уход": 60.0, "Тримминг": 65.0}}, {"breed": "Такса гладкошерстная кроличья до 5 кг", "services": {"Базовый уход": 25.0, "Гигиенический уход": 35.0, "Экспресс-линька": 45.0}}, {"breed": "Такса гладкошерстная карликовая 5–10 кг", "services": {"Базовый уход": 30.0, "Гигиенический уход": 40.0, "Экспресс-линька": 50.0}}, {"breed": "Такса гладкошерстная стандартная 10–15 кг", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Экспресс-линька": 60.0}}, {"breed": "Такса жесткошерстная кроличья до 5 кг", "services": {"Базовый уход": 30.0, "Гигиенический уход": 40.0, "Комплексный уход": 55.0, "Тримминг": 55.0}}, {"breed": "Такса жесткошерстная карликовая 5–10 кг", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Комплексный уход": 60.0, "Тримминг": 65.0}}, {"breed": "Такса жесткошерстная стандартная 10–15 кг", "services": {"Базовый уход": 40.0, "Гигиенический уход": 55.0, "Комплексный уход": 70.0, "Тримминг": 75.0}}, {"breed": "Такса длинношерстная кроличья до 5 кг", "services": {"Базовый уход": 30.0, "Гигиенический уход": 40.0, "Комплексный уход": 55.0}}, {"breed": "Такса длинношерстная карликовая 5–10 кг", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Комплексный уход": 60.0}}, {"breed": "Такса длинношерстная стандартная 10–15 кг", "services": {"Базовый уход": 40.0, "Гигиенический уход": 55.0, "Комплексный уход": 70.0}}, {"breed": "Фокстерьер жесткошерстный 5–10 кг", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Комплексный уход": 60.0, "Тримминг": 65.0}}, {"breed": "Фокстерьер жесткошерстный 10–15 кг", "services": {"Базовый уход": 40.0, "Гигиенический уход": 55.0, "Комплексный уход": 70.0, "Тримминг": 75.0}}, {"breed": "Французский бульдог", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Экспресс-линька": 60.0}}, {"breed": "Хаски 20–30 кг", "services": {"Базовый уход": 60.0, "Гигиенический уход": 75.0, "Экспресс-линька": 90.0}}, {"breed": "Хаски 30–40 кг", "services": {"Базовый уход": 70.0, "Гигиенический уход": 85.0, "Экспресс-линька": 100.0}}, {"breed": "Цвергшнауцер 5–10 кг", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Комплексный уход": 60.0, "Тримминг": 65.0}}, {"breed": "Цвергшнауцер 10–15 кг", "services": {"Базовый уход": 40.0, "Гигиенический уход": 55.0, "Комплексный уход": 70.0, "Тримминг": 75.0}}, {"breed": "Чау-чау 20–30 кг", "services": {"Базовый уход": 60.0, "Гигиенический уход": 75.0, "Комплексный уход": 100.0, "Экспресс-линька": 90.0}}, {"breed": "Чау-чау 30–40 кг", "services": {"Базовый уход": 70.0, "Гигиенический уход": 85.0, "Комплексный уход": 110.0, "Экспресс-линька": 100.0}}, {"breed": "Чихуахуа гладкошерстный", "services": {"Базовый уход": 25.0, "Гигиенический уход": 35.0}}, {"breed": "Чихуахуа длинношерстный", "services": {"Базовый уход": 30.0, "Гигиенический уход": 40.0, "Комплексный уход": 55.0}}, {"breed": "Шарпей 15–20 кг", "services": {"Базовый уход": 40.0, "Гигиенический уход": 50.0, "Экспресс-линька": 65.0}}, {"breed": "Шарпей 20–30 кг", "services": {"Базовый уход": 45.0, "Гигиенический уход": 55.0, "Экспресс-линька": 70.0}}, {"breed": "Шелти", "services": {"Базовый уход": 40.0, "Гигиенический уход": 50.0, "Комплексный уход": 65.0, "Экспресс-линька": 60.0}}, {"breed": "Ши-тцу до 5 кг", "services": {"Базовый уход": 30.0, "Гигиенический уход": 40.0, "Комплексный уход": 55.0}}, {"breed": "Ши-тцу 5–10 кг", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Комплексный уход": 60.0}}, {"breed": "Шпиц немецкий / померанский до 3,5 кг", "services": {"Базовый уход": 35.0, "Гигиенический уход": 45.0, "Комплексный уход": 60.0, "Экспресс-линька": 55.0}}, {"breed": "Шпиц немецкий / померанский более 3,5 кг", "services": {"Базовый уход": 40.0, "Гигиенический уход": 50.0, "Комплексный уход": 65.0, "Экспресс-линька": 60.0}}, {"breed": "Шпиц японский", "services": {"Базовый уход": 40.0, "Гигиенический уход": 50.0, "Комплексный уход": 65.0, "Экспресс-линька": 60.0}}, {"breed": "Японский хин", "services": {"Базовый уход": 30.0, "Гигиенический уход": 40.0, "Комплексный уход": 55.0}}];
var RAILWAY = "https://dynamic-cooperation-production-dd95.up.railway.app/book";
var booking = {breed:'',service:'',price:0,master:'',groomHistory:'',date:'',time:''};
var selBreed = null;
var cY = new Date().getFullYear();
var cM = new Date().getMonth();
var step = 1;
var MONTHS = ['Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь'];
var TIMES = ['10:00','10:30','11:00','11:30','12:00','13:00','14:00','14:30','15:00','15:30','16:00','17:00'];

function showScreen(id) {
  document.querySelectorAll('.screen').forEach(function(s){s.classList.remove('active');});
  document.getElementById(id).classList.add('active');
  window.scrollTo(0,0);
}

function goStep(n) {
  ['bk1','bk2','bk3','bk4','bk5'].forEach(function(id,i){
    document.getElementById(id).className = 'step' + (i+1===n?' show':'');
  });
  for(var i=1;i<=5;i++){
    var ps=document.getElementById('ps'+i);
    var pl=document.getElementById('pl'+i);
    if(i<n){ps.className='ps done';if(pl)pl.className='pl done';}
    else if(i===n){ps.className='ps active';if(pl)pl.className='pl';}
    else{ps.className='ps';if(pl)pl.className='pl';}
  }
  step=n; window.scrollTo(0,0);
}

// Book button
document.getElementById('bookBtn').onclick = function(){
  showScreen('bookScreen'); goStep(1); buildCal();
};

// Back button
document.getElementById('backBtn').onclick = function(){
  if(step>1){goStep(step-1);}else{showScreen('homeScreen');}
};

// Home again
document.getElementById('homeBtn').onclick = function(){
  showScreen('homeScreen'); resetAll();
};

// Breed search
var inp = document.getElementById('bInput');
var drop = document.getElementById('bDrop');
var clr = document.getElementById('clrBtn');
var badge = document.getElementById('sBadge');

inp.addEventListener('input', function(){
  var q = inp.value.trim();
  clr.classList.toggle('show', q.length>0);
  if(!q){drop.classList.remove('open');drop.innerHTML='';return;}
  var res = DATA.filter(function(b){return b.breed.toLowerCase().indexOf(q.toLowerCase())!==-1;}).slice(0,35);
  drop.innerHTML='';
  if(!res.length){drop.innerHTML='<div class="nores">Порода не найдена</div>';}
  else{
    res.forEach(function(b){
      var d=document.createElement('div'); d.className='ditem';
      var idx=b.breed.toLowerCase().indexOf(q.toLowerCase());
      d.innerHTML=b.breed.substring(0,idx)+'<mark>'+b.breed.substring(idx,idx+q.length)+'</mark>'+b.breed.substring(idx+q.length);
      d.onclick=function(){selectBreed(b);};
      drop.appendChild(d);
    });
  }
  drop.classList.add('open');
});

document.addEventListener('click',function(e){
  if(!e.target.closest('.bwrap'))drop.classList.remove('open');
});

clr.onclick = resetBreed;

function selectBreed(b){
  selBreed=b; booking.breed=b.breed;
  inp.value=''; clr.classList.remove('show');
  drop.classList.remove('open'); drop.innerHTML='';
  badge.innerHTML='';
  var bn=document.createElement('span');bn.className='bname';bn.textContent=b.breed;
  var bc=document.createElement('span');bc.className='bchg';bc.textContent='Изменить';
  bc.onclick=resetBreed;
  badge.appendChild(bn);badge.appendChild(bc);
  badge.classList.add('show');
  renderSvcs(b);
  document.getElementById('svcSec').style.display='block';
}

function resetBreed(){
  selBreed=null;booking.breed='';booking.service='';booking.price=0;
  inp.value='';clr.classList.remove('show');
  badge.classList.remove('show');badge.innerHTML='';
  document.getElementById('svcSec').style.display='none';
  document.getElementById('svcList').innerHTML='';
}

function renderSvcs(b){
  var list=document.getElementById('svcList');list.innerHTML='';
  Object.entries(b.services).forEach(function(kv){
    var name=kv[0],price=kv[1];
    var btn=document.createElement('button');btn.className='svbtn';
    var ns=document.createElement('span');ns.textContent=name;
    var ps=document.createElement('span');ps.className='svp';ps.textContent=price+' €';
    btn.appendChild(ns);btn.appendChild(ps);
    btn.onclick=function(){
      document.querySelectorAll('.svbtn').forEach(function(b){b.classList.remove('active');});
      btn.classList.add('active');
      booking.service=name;booking.price=price;
      setTimeout(function(){goStep(2);},300);
    };
    list.appendChild(btn);
  });
}

// Masters
document.querySelectorAll('.mbtn').forEach(function(btn){
  btn.onclick=function(){
    document.querySelectorAll('.mbtn').forEach(function(b){b.classList.remove('active');});
    btn.classList.add('active');
    booking.master=btn.getAttribute('data-master');
    setTimeout(function(){goStep(3);},300);
  };
});

// Groom history
document.querySelectorAll('.gbtn').forEach(function(btn){
  btn.onclick=function(){
    document.querySelectorAll('.gbtn').forEach(function(b){b.classList.remove('active');});
    btn.classList.add('active');
    booking.groomHistory=btn.getAttribute('data-val');
    setTimeout(function(){goStep(4);buildCal();},300);
  };
});

// Calendar
document.getElementById('prevM').onclick=function(){cM--;if(cM<0){cM=11;cY--;}buildCal();};
document.getElementById('nextM').onclick=function(){cM++;if(cM>11){cM=0;cY++;}buildCal();};

function buildCal(){
  document.getElementById('calM').textContent=MONTHS[cM]+' '+cY;
  var g=document.getElementById('calG');g.innerHTML='';
  ['Пн','Вт','Ср','Чт','Пт','Сб','Вс'].forEach(function(d){
    var el=document.createElement('div');el.className='cdn';el.textContent=d;g.appendChild(el);
  });
  var first=new Date(cY,cM,1).getDay();
  var days=new Date(cY,cM+1,0).getDate();
  var start=first===0?6:first-1;
  var today=new Date();
  for(var i=0;i<start;i++){var el=document.createElement('div');el.className='cd';g.appendChild(el);}
  for(var day=1;day<=days;day++){
    var el=document.createElement('div');el.className='cd';
    var date=new Date(cY,cM,day);
    var isPast=date<new Date(today.getFullYear(),today.getMonth(),today.getDate());
    el.textContent=day;
    if(isPast){el.classList.add('dis');}
    else{
      if(date.toDateString()===today.toDateString())el.classList.add('tod');
      (function(d){
        el.onclick=function(){
          document.querySelectorAll('.cd').forEach(function(c){c.classList.remove('sel');});
          el.classList.add('sel');
          booking.date=String(d).padStart(2,'0')+'.'+String(cM+1).padStart(2,'0')+'.'+cY;
          showTimes();
        };
      })(day);
    }
    g.appendChild(el);
  }
}

function showTimes(){
  var tg=document.getElementById('timeG');tg.innerHTML='';
  TIMES.forEach(function(t){
    var btn=document.createElement('button');btn.className='tbtn';btn.textContent=t;
    btn.onclick=function(){
      document.querySelectorAll('.tbtn').forEach(function(b){b.classList.remove('active');});
      btn.classList.add('active');booking.time=t;
      setTimeout(function(){goStep(5);buildSum();},300);
    };
    tg.appendChild(btn);
  });
  document.getElementById('timeSec').style.display='block';
  document.getElementById('timeSec').scrollIntoView({behavior:'smooth',block:'nearest'});
}

function buildSum(){
  document.getElementById('sumBlock').innerHTML=
    '<div class="sr"><span class="sl">Порода</span><span class="sv">'+booking.breed+'</span></div>'+
    '<div class="sr"><span class="sl">Услуга</span><span class="sv">'+booking.service+'</span></div>'+
    '<div class="sr"><span class="sl">Мастер</span><span class="sv">'+booking.master+'</span></div>'+
    '<div class="sr"><span class="sl">Последний грум</span><span class="sv">'+booking.groomHistory+'</span></div>'+
    '<div class="sr"><span class="sl">Дата</span><span class="sv">'+booking.date+'</span></div>'+
    '<div class="sr"><span class="sl">Время</span><span class="sv">'+booking.time+'</span></div>'+
    '<div class="sr"><span class="sl">Стоимость</span><span class="sp">'+booking.price+' €</span></div>';
}

// Confirm
document.getElementById('confirmBtn').onclick = function(){
  var name=document.getElementById('cName').value;
  var phone=document.getElementById('cPhone').value;
  if(!name||!phone){alert('Введите имя и телефон');return;}
  booking.name=name; booking.phone=phone; booking.pet=document.getElementById('cPet').value;

  var btn=document.getElementById('confirmBtn');
  btn.textContent='Отправляем...'; btn.disabled=true;

  // Build GET params
  var params = Object.keys(booking).map(function(k){
    return encodeURIComponent(k) + '=' + encodeURIComponent(booking[k]);
  }).join('&');

  fetch(RAILWAY + '?' + params, {method:'GET'})
    .then(function(){showSuccess();})
    .catch(function(){showSuccess();});
};

function showSuccess(){
  document.getElementById('bk5').className='step';
  document.getElementById('sucBlock').classList.add('show');
  document.getElementById('progress').style.display='none';
}

function resetAll(){
  booking={breed:'',service:'',price:0,master:'',groomHistory:'',date:'',time:''};
  selBreed=null; inp.value=''; clr.classList.remove('show');
  badge.classList.remove('show'); badge.innerHTML='';
  document.getElementById('svcSec').style.display='none';
  document.getElementById('timeSec').style.display='none';
  document.getElementById('sucBlock').classList.remove('show');
  document.getElementById('progress').style.display='flex';
  document.getElementById('cName').value='';
  document.getElementById('cPhone').value='';
  document.getElementById('cPet').value='';
  document.getElementById('confirmBtn').textContent='Подтвердить запись';
  document.getElementById('confirmBtn').disabled=false;
  goStep(1);
}
</script>
</body>
</html>"""


BOOKING_HTML_B64 = "PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9InJ1Ij4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idGhlbWUtY29sb3IiIGNvbnRlbnQ9IiMxYzFjMTgiPgo8bWV0YSBuYW1lPSJ2aWV3cG9ydCIgY29udGVudD0id2lkdGg9ZGV2aWNlLXdpZHRoLGluaXRpYWwtc2NhbGU9MSI+Cjx0aXRsZT5SJkogR3Jvb21pbmc8L3RpdGxlPgo8bGluayBocmVmPSJodHRwczovL2ZvbnRzLmdvb2dsZWFwaXMuY29tL2NzczI/ZmFtaWx5PUNvcm1vcmFudCtHYXJhbW9uZDppdGFsLHdnaHRAMCw2MDA7MSw0MDAmZmFtaWx5PU1vbnRzZXJyYXQ6d2dodEAzMDA7NDAwOzUwMDs2MDAmZGlzcGxheT1zd2FwIiByZWw9InN0eWxlc2hlZXQiPgo8c3R5bGU+Cip7Ym94LXNpemluZzpib3JkZXItYm94O21hcmdpbjowO3BhZGRpbmc6MH0KaHRtbCxib2R5e21pbi1oZWlnaHQ6MTAwdmg7YmFja2dyb3VuZDojMWMxYzE4O2NvbG9yOiNjOGMyYjg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC13ZWlnaHQ6MzAwfQouc2NyZWVue2Rpc3BsYXk6bm9uZTttaW4taGVpZ2h0OjEwMHZoO2ZsZXgtZGlyZWN0aW9uOmNvbHVtbjthbGlnbi1pdGVtczpjZW50ZXI7cGFkZGluZzo0MHB4IDAgNDhweH0KLnNjcmVlbi5hY3RpdmV7ZGlzcGxheTpmbGV4fQouY29ue3dpZHRoOjEwMCU7bWF4LXdpZHRoOjQwMHB4O3BhZGRpbmc6MCAyOHB4fQouYmFjay1idG57ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6OHB4O2NvbG9yOiM4YThhNTU7Zm9udC1zaXplOi42MnJlbTtsZXR0ZXItc3BhY2luZzouMThlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7YmFja2dyb3VuZDpub25lO2JvcmRlcjpub25lO2N1cnNvcjpwb2ludGVyO3BhZGRpbmc6MDttYXJnaW4tYm90dG9tOjI0cHg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5sb2dvLXJqe2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmO2ZvbnQtc2l6ZToycmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZThlMGQwfQoubG9nby1zdWJ7Zm9udC1zaXplOi40NnJlbTtmb250LXdlaWdodDo2MDA7bGV0dGVyLXNwYWNpbmc6LjRlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6IzhhOGE1NTttYXJnaW4tdG9wOjNweDtwYWRkaW5nLWJvdHRvbToxNHB4O2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjAxLDE2OCw3NiwuMik7bWFyZ2luLWJvdHRvbToyMHB4fQouaG9tZS1yantmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZjtmb250LXNpemU6Mi42cmVtO2ZvbnQtd2VpZ2h0OjYwMDtjb2xvcjojZThlMGQwO2xpbmUtaGVpZ2h0OjF9Ci5sb2dvLXRhZ3tmb250LXNpemU6LjUycmVtO2xldHRlci1zcGFjaW5nOi4xMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtjb2xvcjojNjY2NjYwO2xpbmUtaGVpZ2h0OjEuNX0KLmxvZ28tcm93e2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpmbGV4LWVuZDtnYXA6MTJweDttYXJnaW4tYm90dG9tOjRweDtwYWRkaW5nLWJvdHRvbToxOHB4O2JvcmRlci1ib3R0b206MXB4IHNvbGlkIHJnYmEoMjAxLDE2OCw3NiwuMil9Ci5ob21lLWdzdWJ7Zm9udC1zaXplOi40NnJlbTtmb250LXdlaWdodDo2MDA7bGV0dGVyLXNwYWNpbmc6LjRlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6IzhhOGE1NTttYXJnaW4tdG9wOjZweDttYXJnaW4tYm90dG9tOjIycHh9Ci5ob21lLWgxe2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmO2ZvbnQtc2l6ZToyLjJyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNkOGQwYzA7bGluZS1oZWlnaHQ6MS4xO21hcmdpbi1ib3R0b206NXB4fQouaG9tZS1oMSBlbXtmb250LXN0eWxlOml0YWxpYztjb2xvcjojYzlhODRjfQouaG9tZS1zdWJ7Zm9udC1zaXplOi42MnJlbTtsZXR0ZXItc3BhY2luZzouMTVlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6IzU1NTU1MDttYXJnaW4tYm90dG9tOjIycHh9Ci5vcHR7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTZweDtwYWRkaW5nOjE1cHggMDtib3JkZXItYm90dG9tOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNik7dGV4dC1kZWNvcmF0aW9uOm5vbmU7Y29sb3I6I2M4YzJiODt0cmFuc2l0aW9uOmFsbCAuMnM7Y3Vyc29yOnBvaW50ZXI7YmFja2dyb3VuZDpub25lO2JvcmRlci10b3A6bm9uZTtib3JkZXItbGVmdDpub25lO2JvcmRlci1yaWdodDpub25lO3dpZHRoOjEwMCU7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWZ9Ci5vcHQ6aG92ZXJ7Y29sb3I6I2M5YTg0YztwYWRkaW5nLWxlZnQ6NnB4fQoub3B0LWljb257d2lkdGg6NDBweDtoZWlnaHQ6NDBweDtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7ZmxleC1zaHJpbms6MH0KLm9wdC10ZXh0e2ZsZXg6MTt0ZXh0LWFsaWduOmxlZnR9Ci5vcHQtdGl0bGV7Zm9udC1zaXplOi44OHJlbTtmb250LXdlaWdodDo1MDA7Y29sb3I6IzlhOTU5MDttYXJnaW4tYm90dG9tOjJweDt0cmFuc2l0aW9uOmNvbG9yIC4yc30KLm9wdDpob3ZlciAub3B0LXRpdGxle2NvbG9yOiNjOWE4NGN9Ci5vcHQtaGFuZGxle2ZvbnQtc2l6ZTouNjhyZW07Y29sb3I6IzU1NTU1MH0KLm9wdC1hcnJvd3tjb2xvcjojOGE4YTU1O2ZvbnQtc2l6ZTouOXJlbTtmbGV4LXNocmluazowfQouZGl2aWRlcntkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2dhcDoxMnB4O3BhZGRpbmc6MTBweCAwfQouZGl2aWRlcjo6YmVmb3JlLC5kaXZpZGVyOjphZnRlcntjb250ZW50OicnO2ZsZXg6MTtoZWlnaHQ6MXB4O2JhY2tncm91bmQ6cmdiYSgyNTUsMjU1LDI1NSwuMDYpfQouZGl2aWRlciBzcGFue2ZvbnQtc2l6ZTouNTJyZW07bGV0dGVyLXNwYWNpbmc6LjJlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6IzQ0NDQ0MH0KLmhvbWUtZm9vdHttYXJnaW4tdG9wOjI4cHg7cGFkZGluZy10b3A6MThweDtib3JkZXItdG9wOjFweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjEyKTtkaXNwbGF5OmZsZXg7anVzdGlmeS1jb250ZW50OnNwYWNlLWJldHdlZW47YWxpZ24taXRlbXM6Y2VudGVyfQouaG9tZS1mb290IHNwYW57Zm9udC1zaXplOi41OHJlbTtsZXR0ZXItc3BhY2luZzouMThlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6IzQ0NDQ0MH0KLmZkb3R7d2lkdGg6M3B4O2hlaWdodDozcHg7Ym9yZGVyLXJhZGl1czo1MCU7YmFja2dyb3VuZDojNmI2YjQyfQoucHJvZ3Jlc3N7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjttYXJnaW4tYm90dG9tOjI0cHg7b3ZlcmZsb3c6aGlkZGVufQoucHN7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6NHB4O2ZvbnQtc2l6ZTouNTJyZW07bGV0dGVyLXNwYWNpbmc6LjFlbTt0ZXh0LXRyYW5zZm9ybTp1cHBlcmNhc2U7Y29sb3I6IzQ0NDQ0MDt3aGl0ZS1zcGFjZTpub3dyYXB9Ci5wcy5kb25le2NvbG9yOiM4YThhNTV9LnBzLmFjdGl2ZXtjb2xvcjojYzlhODRjfQoucGRvdHt3aWR0aDo1cHg7aGVpZ2h0OjVweDtib3JkZXItcmFkaXVzOjUwJTtiYWNrZ3JvdW5kOiMyYTJhMjQ7ZmxleC1zaHJpbms6MH0KLnBzLmRvbmUgLnBkb3R7YmFja2dyb3VuZDojOGE4YTU1fS5wcy5hY3RpdmUgLnBkb3R7YmFja2dyb3VuZDojYzlhODRjfQoucGx7ZmxleDoxO2hlaWdodDoxcHg7YmFja2dyb3VuZDojMmEyYTI0O21hcmdpbjowIDVweDttaW4td2lkdGg6OHB4fQoucGwuZG9uZXtiYWNrZ3JvdW5kOiM4YThhNTV9Ci5zdGVwe2Rpc3BsYXk6bm9uZX0uc3RlcC5zaG93e2Rpc3BsYXk6YmxvY2s7YW5pbWF0aW9uOmZ1IC40cyBlYXNlIGJvdGh9Ci5zbGJse2ZvbnQtc2l6ZTouNTZyZW07bGV0dGVyLXNwYWNpbmc6LjIyZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNjOWE4NGM7bWFyZ2luLWJvdHRvbToxMHB4O2ZvbnQtd2VpZ2h0OjUwMH0KLnNib3h7ZGlzcGxheTpmbGV4O2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTBweDtiYWNrZ3JvdW5kOiMxNDE0MTA7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjMpO3BhZGRpbmc6MCAxNHB4fQouc2JveDpmb2N1cy13aXRoaW57Ym9yZGVyLWNvbG9yOiNjOWE4NGN9Ci5zaXtvcGFjaXR5Oi40O2ZvbnQtc2l6ZTouODVyZW07ZmxleC1zaHJpbms6MH0KI2JJbnB1dHtmbGV4OjE7YmFja2dyb3VuZDp0cmFuc3BhcmVudDtib3JkZXI6bm9uZTtvdXRsaW5lOm5vbmU7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC1zaXplOi44NXJlbTtjb2xvcjojYzhjMmI4O3BhZGRpbmc6MTNweCAwfQojYklucHV0OjpwbGFjZWhvbGRlcntjb2xvcjojNDQ0NDQwfQouY2xye2JhY2tncm91bmQ6bm9uZTtib3JkZXI6bm9uZTtjb2xvcjojNTU1NTUwO2N1cnNvcjpwb2ludGVyO2ZvbnQtc2l6ZTouOHJlbTtkaXNwbGF5Om5vbmV9Ci5jbHIuc2hvd3tkaXNwbGF5OmJsb2NrfQouYndyYXB7cG9zaXRpb246cmVsYXRpdmU7bWFyZ2luLWJvdHRvbTo4cHh9Ci5kcm9we3Bvc2l0aW9uOmFic29sdXRlO2xlZnQ6MDtyaWdodDowO2JhY2tncm91bmQ6IzE0MTQxMDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjAxLDE2OCw3NiwuMjUpO2JvcmRlci10b3A6bm9uZTttYXgtaGVpZ2h0OjIwMHB4O292ZXJmbG93LXk6YXV0bzt6LWluZGV4OjUwO2Rpc3BsYXk6bm9uZX0KLmRyb3Aub3BlbntkaXNwbGF5OmJsb2NrfQouZGl0ZW17cGFkZGluZzoxMXB4IDE0cHg7Zm9udC1zaXplOi44cmVtO2NvbG9yOiNjOGMyYjg7Y3Vyc29yOnBvaW50ZXI7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDQpfQouZGl0ZW06aG92ZXJ7YmFja2dyb3VuZDpyZ2JhKDIwMSwxNjgsNzYsLjEpO2NvbG9yOiNjOWE4NGN9Ci5kaXRlbSBtYXJre2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Y29sb3I6I2M5YTg0Yztmb250LXdlaWdodDo2MDB9Ci5ub3Jlc3twYWRkaW5nOjE0cHg7Zm9udC1zaXplOi43NXJlbTtjb2xvcjojNTU1NTUwO2ZvbnQtc3R5bGU6aXRhbGljfQouc2JhZGdle2Rpc3BsYXk6bm9uZTthbGlnbi1pdGVtczpjZW50ZXI7Z2FwOjEwcHg7bWFyZ2luLWJvdHRvbToxNnB4fQouc2JhZGdlLnNob3d7ZGlzcGxheTpmbGV4fQouYm5hbWV7YmFja2dyb3VuZDpyZ2JhKDIwMSwxNjgsNzYsLjEpO2JvcmRlcjoxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4zKTtjb2xvcjojYzlhODRjO3BhZGRpbmc6NHB4IDEycHg7Zm9udC1zaXplOi43cmVtfQouYmNoZ3tmb250LXNpemU6LjZyZW07Y29sb3I6IzU1NTU1MDtjdXJzb3I6cG9pbnRlcjt0ZXh0LWRlY29yYXRpb246dW5kZXJsaW5lfQouc3ZidG57ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2FsaWduLWl0ZW1zOmNlbnRlcjtnYXA6MTJweDtwYWRkaW5nOjEzcHggMTZweDtiYWNrZ3JvdW5kOiMxNDE0MTA7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wNyk7Ym9yZGVyLWxlZnQ6MnB4IHNvbGlkIHRyYW5zcGFyZW50O2NvbG9yOiNjOGMyYjg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC1zaXplOi44cmVtO2N1cnNvcjpwb2ludGVyO3RleHQtYWxpZ246bGVmdDt0cmFuc2l0aW9uOmFsbCAuMnM7d2lkdGg6MTAwJTttYXJnaW4tYm90dG9tOjJweH0KLnN2YnRuOmhvdmVyLC5zdmJ0bi5hY3RpdmV7Ym9yZGVyLWxlZnQtY29sb3I6I2M5YTg0Yztjb2xvcjojYzlhODRjO2JhY2tncm91bmQ6IzFhMWExNH0KLnN2cHtmb250LXdlaWdodDo2MDA7Y29sb3I6I2M5YTg0YztmbGV4LXNocmluazowfQoubWFzdGVyc3tkaXNwbGF5OmdyaWQ7Z3JpZC10ZW1wbGF0ZS1jb2x1bW5zOjFmciAxZnI7Z2FwOjhweH0KLm1idG57YmFja2dyb3VuZDojMTQxNDEwO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDgpO3BhZGRpbmc6MTZweCAxMnB4O3RleHQtYWxpZ246Y2VudGVyO2N1cnNvcjpwb2ludGVyO3RyYW5zaXRpb246YWxsIC4ycztmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZn0KLm1idG46aG92ZXIsLm1idG4uYWN0aXZle2JvcmRlci1jb2xvcjojYzlhODRjO2JhY2tncm91bmQ6cmdiYSgyMDEsMTY4LDc2LC4wOCl9Ci5tYXZ7d2lkdGg6NDZweDtoZWlnaHQ6NDZweDtib3JkZXItcmFkaXVzOjUwJTtiYWNrZ3JvdW5kOnJnYmEoMjAxLDE2OCw3NiwuMTUpO2JvcmRlcjoxcHggc29saWQgcmdiYSgyMDEsMTY4LDc2LC4zKTtkaXNwbGF5OmZsZXg7YWxpZ24taXRlbXM6Y2VudGVyO2p1c3RpZnktY29udGVudDpjZW50ZXI7bWFyZ2luOjAgYXV0byAxMHB4O2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmO2ZvbnQtc2l6ZToxLjJyZW07Zm9udC13ZWlnaHQ6NjAwO2NvbG9yOiNjOWE4NGN9Ci5tYnRuLmFjdGl2ZSAubWF2e2JhY2tncm91bmQ6cmdiYSgyMDEsMTY4LDc2LC4yNSk7Ym9yZGVyLWNvbG9yOiNjOWE4NGN9Ci5tbmFtZXtmb250LXNpemU6LjhyZW07Zm9udC13ZWlnaHQ6NTAwO2NvbG9yOiNjOGMyYjh9Ci5tYnRuLmFjdGl2ZSAubW5hbWV7Y29sb3I6I2M5YTg0Y30KLm10aXRsZXtmb250LXNpemU6LjU4cmVtO2NvbG9yOiM1NTU1NTA7bWFyZ2luLXRvcDoycHh9Ci5nYnRue2Rpc3BsYXk6ZmxleDthbGlnbi1pdGVtczpjZW50ZXI7cGFkZGluZzoxM3B4IDE2cHg7YmFja2dyb3VuZDojMTQxNDEwO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDcpO2JvcmRlci1sZWZ0OjJweCBzb2xpZCB0cmFuc3BhcmVudDtjb2xvcjojYzhjMmI4O2ZvbnQtZmFtaWx5OidNb250c2VycmF0JyxzYW5zLXNlcmlmO2ZvbnQtc2l6ZTouODJyZW07Y3Vyc29yOnBvaW50ZXI7d2lkdGg6MTAwJTttYXJnaW4tYm90dG9tOjRweDt0cmFuc2l0aW9uOmFsbCAuMnN9Ci5nYnRuOmhvdmVyLC5nYnRuLmFjdGl2ZXtib3JkZXItbGVmdC1jb2xvcjojYzlhODRjO2NvbG9yOiNjOWE4NGM7YmFja2dyb3VuZDojMWExYTE0fQouY2FsLWh7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2FsaWduLWl0ZW1zOmNlbnRlcjttYXJnaW4tYm90dG9tOjEwcHh9Ci5jYWwtbXtmb250LWZhbWlseTonQ29ybW9yYW50IEdhcmFtb25kJyxzZXJpZjtmb250LXNpemU6MXJlbTtjb2xvcjojYzhjMmI4fQouY2FsLW57YmFja2dyb3VuZDpub25lO2JvcmRlcjpub25lO2NvbG9yOiM4YThhNTU7Y3Vyc29yOnBvaW50ZXI7Zm9udC1zaXplOi45cmVtO3BhZGRpbmc6NHB4IDhweH0KLmNne2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDcsMWZyKTtnYXA6MnB4O21hcmdpbi1ib3R0b206OHB4fQouY2Rue3RleHQtYWxpZ246Y2VudGVyO2ZvbnQtc2l6ZTouNTJyZW07Y29sb3I6IzQ0NDQ0MDtwYWRkaW5nOjRweCAwO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZX0KLmNke3RleHQtYWxpZ246Y2VudGVyO3BhZGRpbmc6OHB4IDRweDtmb250LXNpemU6Ljc4cmVtO2N1cnNvcjpwb2ludGVyO2NvbG9yOiM4ODg4ODA7Ym9yZGVyOjFweCBzb2xpZCB0cmFuc3BhcmVudDt0cmFuc2l0aW9uOmFsbCAuMnN9Ci5jZDpob3Zlcjpub3QoLmRpcyksLmNkLnNlbHtiYWNrZ3JvdW5kOnJnYmEoMjAxLDE2OCw3NiwuMTIpO2JvcmRlci1jb2xvcjpyZ2JhKDIwMSwxNjgsNzYsLjQpO2NvbG9yOiNjOWE4NGN9Ci5jZC50b2R7Y29sb3I6I2M5YTg0Yztmb250LXdlaWdodDo2MDB9Ci5jZC5kaXN7Y29sb3I6IzJhMmEyNDtjdXJzb3I6ZGVmYXVsdH0KLnRne2Rpc3BsYXk6Z3JpZDtncmlkLXRlbXBsYXRlLWNvbHVtbnM6cmVwZWF0KDQsMWZyKTtnYXA6NnB4fQoudGJ0bntiYWNrZ3JvdW5kOiMxNDE0MTA7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDI1NSwyNTUsMjU1LC4wOCk7cGFkZGluZzoxMHB4IDRweDt0ZXh0LWFsaWduOmNlbnRlcjtmb250LXNpemU6LjcycmVtO2NvbG9yOiNjOGMyYjg7Y3Vyc29yOnBvaW50ZXI7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7dHJhbnNpdGlvbjphbGwgLjJzfQoudGJ0bjpob3ZlciwudGJ0bi5hY3RpdmV7Ym9yZGVyLWNvbG9yOiNjOWE4NGM7YmFja2dyb3VuZDpyZ2JhKDIwMSwxNjgsNzYsLjA4KTtjb2xvcjojYzlhODRjfQouc3Vte2JhY2tncm91bmQ6IzE0MTQxMDtib3JkZXI6MXB4IHNvbGlkIHJnYmEoMjAxLDE2OCw3NiwuMik7Ym9yZGVyLXRvcDoycHggc29saWQgI2M5YTg0YztwYWRkaW5nOjIwcHg7bWFyZ2luLWJvdHRvbToxNHB4fQouc3J7ZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO3BhZGRpbmc6NnB4IDA7Ym9yZGVyLWJvdHRvbToxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMDUpO2ZvbnQtc2l6ZTouNzhyZW19Ci5zcjpsYXN0LWNoaWxke2JvcmRlci1ib3R0b206bm9uZTtwYWRkaW5nLXRvcDoxMHB4fQouc2x7Y29sb3I6IzY2NjY2MH0uc3Z7Y29sb3I6I2M4YzJiODtmb250LXdlaWdodDo1MDA7dGV4dC1hbGlnbjpyaWdodH0KLnNwe2ZvbnQtZmFtaWx5OidDb3Jtb3JhbnQgR2FyYW1vbmQnLHNlcmlmO2ZvbnQtc2l6ZToxLjVyZW07Y29sb3I6I2M5YTg0Yztmb250LXdlaWdodDo2MDB9Ci5mZ3ttYXJnaW4tYm90dG9tOjEycHh9Ci5mbHtmb250LXNpemU6LjU2cmVtO2xldHRlci1zcGFjaW5nOi4yZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO2NvbG9yOiNjOWE4NGM7bWFyZ2luLWJvdHRvbTo2cHg7ZGlzcGxheTpibG9ja30KLmZpe3dpZHRoOjEwMCU7YmFja2dyb3VuZDojMTQxNDEwO2JvcmRlcjoxcHggc29saWQgcmdiYSgyNTUsMjU1LDI1NSwuMSk7Y29sb3I6I2M4YzJiODtmb250LWZhbWlseTonTW9udHNlcnJhdCcsc2Fucy1zZXJpZjtmb250LXNpemU6Ljg1cmVtO3BhZGRpbmc6MTJweCAxNHB4O291dGxpbmU6bm9uZX0KLmZpOmZvY3Vze2JvcmRlci1jb2xvcjojYzlhODRjfQouY2J0bntkaXNwbGF5OmJsb2NrO3dpZHRoOjEwMCU7YmFja2dyb3VuZDojNGE0YTJlO2NvbG9yOiNjOGMyYjg7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC1zaXplOi42OHJlbTtmb250LXdlaWdodDo2MDA7bGV0dGVyLXNwYWNpbmc6LjI1ZW07dGV4dC10cmFuc2Zvcm06dXBwZXJjYXNlO3RleHQtYWxpZ246Y2VudGVyO3BhZGRpbmc6MTZweDtib3JkZXI6bm9uZTtjdXJzb3I6cG9pbnRlcn0KLmNidG46aG92ZXJ7YmFja2dyb3VuZDojNmI2YjQyfQouc2Jsb2Nre3RleHQtYWxpZ246Y2VudGVyO3BhZGRpbmc6NDBweCAyMHB4O2Rpc3BsYXk6bm9uZX0KLnNibG9jay5zaG93e2Rpc3BsYXk6YmxvY2s7YW5pbWF0aW9uOmZ1IC41cyBlYXNlIGJvdGh9Ci5zaTJ7Zm9udC1zaXplOjNyZW07bWFyZ2luLWJvdHRvbToxNnB4fQouc3R7Zm9udC1mYW1pbHk6J0Nvcm1vcmFudCBHYXJhbW9uZCcsc2VyaWY7Zm9udC1zaXplOjEuNnJlbTtjb2xvcjojYzlhODRjO21hcmdpbi1ib3R0b206OHB4fQouc3N7Zm9udC1zaXplOi43OHJlbTtjb2xvcjojNzc3NzcwO2xpbmUtaGVpZ2h0OjEuNjttYXJnaW4tYm90dG9tOjI0cHh9Ci5oYnRue2JhY2tncm91bmQ6dHJhbnNwYXJlbnQ7Ym9yZGVyOjFweCBzb2xpZCByZ2JhKDIwMSwxNjgsNzYsLjMpO2NvbG9yOiNjOWE4NGM7Zm9udC1mYW1pbHk6J01vbnRzZXJyYXQnLHNhbnMtc2VyaWY7Zm9udC1zaXplOi42NXJlbTtsZXR0ZXItc3BhY2luZzouMmVtO3RleHQtdHJhbnNmb3JtOnVwcGVyY2FzZTtwYWRkaW5nOjEycHggMjRweDtjdXJzb3I6cG9pbnRlcn0KQGtleWZyYW1lcyBmdXtmcm9te29wYWNpdHk6MDt0cmFuc2Zvcm06dHJhbnNsYXRlWSgxMnB4KX10b3tvcGFjaXR5OjE7dHJhbnNmb3JtOnRyYW5zbGF0ZVkoMCl9fQo8L3N0eWxlPgo8L2hlYWQ+Cjxib2R5PgoKPCEtLSBIT01FIC0tPgo8ZGl2IGNsYXNzPSJzY3JlZW4gYWN0aXZlIiBpZD0iaG9tZVNjcmVlbiI+CjxkaXYgY2xhc3M9ImNvbiI+CiAgPGRpdiBjbGFzcz0ibG9nby1yb3ciPgogICAgPGRpdiBjbGFzcz0iaG9tZS1yaiI+UiZhbXA7SjwvZGl2PgogICAgPGRpdiBjbGFzcz0ibG9nby10YWciPtCf0YDQtdC80LjQsNC70YzQvdCw0Y8g0LPRgNGD0LzQuNC90LMtPGJyPtGB0YLRg9C00LjRjyDQsiDQotCw0LvQu9C40L3QtTwvZGl2PgogIDwvZGl2PgogIDxkaXYgY2xhc3M9ImhvbWUtZ3N1YiI+R3Jvb21pbmc8L2Rpdj4KICA8ZGl2IGNsYXNzPSJob21lLWgxIj5Cb29rIHRoZSB3YXkgPGVtPnlvdSBsaWtlPC9lbT48L2Rpdj4KICA8ZGl2IGNsYXNzPSJob21lLXN1YiI+Q2hvb3NlIGhvdyB0byBjb25uZWN0PC9kaXY+CgogIDxhIGhyZWY9Imh0dHBzOi8vd3d3Lmluc3RhZ3JhbS5jb20vcmpfZ3Jvb21pbmc/aWdzaD1NV3htZEhOcWNYRmthbk52YlE9PSIgdGFyZ2V0PSJfYmxhbmsiIGNsYXNzPSJvcHQiPgogICAgPGRpdiBjbGFzcz0ib3B0LWljb24iPjxzdmcgd2lkdGg9IjM2IiBoZWlnaHQ9IjM2IiB2aWV3Qm94PSIwIDAgMjQgMjQiIHhtbG5zPSJodHRwOi8vd3d3LnczLm9yZy8yMDAwL3N2ZyI+PGRlZnM+PGxpbmVhckdyYWRpZW50IGlkPSJpZyIgeDE9IjAlIiB5MT0iMTAwJSIgeDI9IjEwMCUiIHkyPSIwJSI+PHN0b3Agb2Zmc2V0PSIwJSIgc3RvcC1jb2xvcj0iI2YwOTQzMyIvPjxzdG9wIG9mZnNldD0iNTAlIiBzdG9wLWNvbG9yPSIjZGMyNzQzIi8+PHN0b3Agb2Zmc2V0PSIxMDAlIiBzdG9wLWNvbG9yPSIjYmMxODg4Ii8+PC9saW5lYXJHcmFkaWVudD48L2RlZnM+PHJlY3Qgd2lkdGg9IjI0IiBoZWlnaHQ9IjI0IiByeD0iNiIgZmlsbD0idXJsKCNpZykiLz48cmVjdCB4PSI2IiB5PSI2IiB3aWR0aD0iMTIiIGhlaWdodD0iMTIiIHJ4PSIzIiBmaWxsPSJub25lIiBzdHJva2U9IndoaXRlIiBzdHJva2Utd2lkdGg9IjEuNSIvPjxjaXJjbGUgY3g9IjEyIiBjeT0iMTIiIHI9IjMiIGZpbGw9Im5vbmUiIHN0cm9rZT0id2hpdGUiIHN0cm9rZS13aWR0aD0iMS41Ii8+PGNpcmNsZSBjeD0iMTYuNSIgY3k9IjcuNSIgcj0iMSIgZmlsbD0id2hpdGUiLz48L3N2Zz48L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im9wdC10ZXh0Ij48ZGl2IGNsYXNzPSJvcHQtdGl0bGUiPkluc3RhZ3JhbTwvZGl2PjxkaXYgY2xhc3M9Im9wdC1oYW5kbGUiPkByal9ncm9vbWluZzwvZGl2PjwvZGl2PgogICAgPHNwYW4gY2xhc3M9Im9wdC1hcnJvdyI+4oaSPC9zcGFuPgogIDwvYT4KICA8YSBocmVmPSJodHRwczovL3dhLm1lLzM3MjU4NzM1NDU2IiB0YXJnZXQ9Il9ibGFuayIgY2xhc3M9Im9wdCI+CiAgICA8ZGl2IGNsYXNzPSJvcHQtaWNvbiI+PHN2ZyB3aWR0aD0iMzYiIGhlaWdodD0iMzYiIHZpZXdCb3g9IjAgMCAyNCAyNCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48Y2lyY2xlIGN4PSIxMiIgY3k9IjEyIiByPSIxMiIgZmlsbD0iIzI1RDM2NiIvPjxwYXRoIGZpbGw9IndoaXRlIiBkPSJNMTcuNDcyIDE0LjM4MmMtLjI5Ny0uMTQ5LTEuNzU4LS44NjctMi4wMy0uOTY3LS4yNzMtLjA5OS0uNDcxLS4xNDgtLjY3LjE1LS4xOTcuMjk3LS43NjcuOTY2LS45NCAxLjE2NC0uMTczLjE5OS0uMzQ3LjIyMy0uNjQ0LjA3NS0uMjk3LS4xNS0xLjI1NS0uNDYzLTIuMzktMS40NzUtLjg4My0uNzg4LTEuNDgtMS43NjEtMS42NTMtMi4wNTktLjE3My0uMjk3LS4wMTgtLjQ1OC4xMy0uNjA2LjEzNC0uMTMzLjI5OC0uMzQ3LjQ0Ni0uNTIuMTQ5LS4xNzQuMTk4LS4yOTguMjk4LS40OTcuMDk5LS4xOTguMDUtLjM3MS0uMDI1LS41Mi0uMDc1LS4xNDktLjY2OS0xLjYxMi0uOTE2LTIuMjA3LS4yNDItLjU3OS0uNDg3LS41LS42NjktLjUxLS4xNzMtLjAwOC0uMzcxLS4wMS0uNTctLjAxLS4xOTggMC0uNTIuMDc0LS43OTIuMzcyLS4yNzIuMjk3LTEuMDQgMS4wMTYtMS4wNCAyLjQ3OSAwIDEuNDYyIDEuMDY1IDIuODc1IDEuMjEzIDMuMDc0LjE0OS4xOTggMi4wOTYgMy4yIDUuMDc3IDQuNDg3LjcwOS4zMDYgMS4yNjIuNDg5IDEuNjk0LjYyNS43MTIuMjI3IDEuMzYuMTk1IDEuODcxLjExOC41NzEtLjA4NSAxLjc1OC0uNzE5IDIuMDA2LTEuNDEzLjI0OC0uNjk0LjI0OC0xLjI4OS4xNzMtMS40MTMtLjA3NC0uMTI0LS4yNzItLjE5OC0uNTctLjM0NyIvPjwvc3ZnPjwvZGl2PgogICAgPGRpdiBjbGFzcz0ib3B0LXRleHQiPjxkaXYgY2xhc3M9Im9wdC10aXRsZSI+V2hhdHNBcHA8L2Rpdj48ZGl2IGNsYXNzPSJvcHQtaGFuZGxlIj4rMzcyIDU4NyAzNTQ1NjwvZGl2PjwvZGl2PgogICAgPHNwYW4gY2xhc3M9Im9wdC1hcnJvdyI+4oaSPC9zcGFuPgogIDwvYT4KICA8YSBocmVmPSJodHRwczovL3d3dy5mYWNlYm9vay5jb20vc2hhcmUvMUVMUDZLQzZyVi8/bWliZXh0aWQ9d3dYSWZyIiB0YXJnZXQ9Il9ibGFuayIgY2xhc3M9Im9wdCI+CiAgICA8ZGl2IGNsYXNzPSJvcHQtaWNvbiI+PHN2ZyB3aWR0aD0iMzYiIGhlaWdodD0iMzYiIHZpZXdCb3g9IjAgMCAyNCAyNCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48Y2lyY2xlIGN4PSIxMiIgY3k9IjEyIiByPSIxMiIgZmlsbD0iIzE4NzdGMiIvPjxwYXRoIGZpbGw9IndoaXRlIiBkPSJNMTMgMTAuNWgybC41LTIuNUgxM1Y2LjVjMC0uNy4yLTEuNSAxLjUtMS41SDE2VjNzLTEtLjItMi0uMmMtMi4xIDAtMy41IDEuMy0zLjUgMy41VjhIOHYyLjVoMi41VjE4SDEzdi03LjV6Ii8+PC9zdmc+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJvcHQtdGV4dCI+PGRpdiBjbGFzcz0ib3B0LXRpdGxlIj5GYWNlYm9vazwvZGl2PjxkaXYgY2xhc3M9Im9wdC1oYW5kbGUiPlImYW1wO0ogR3Jvb21pbmc8L2Rpdj48L2Rpdj4KICAgIDxzcGFuIGNsYXNzPSJvcHQtYXJyb3ciPuKGkjwvc3Bhbj4KICA8L2E+CiAgPGRpdiBjbGFzcz0iZGl2aWRlciI+PHNwYW4+b3IgYm9vayBkaXJlY3RseTwvc3Bhbj48L2Rpdj4KICA8YnV0dG9uIGNsYXNzPSJvcHQiIGlkPSJib29rQnRuIj4KICAgIDxkaXYgY2xhc3M9Im9wdC1pY29uIj48c3ZnIHdpZHRoPSIzMiIgaGVpZ2h0PSIzMiIgdmlld0JveD0iMCAwIDI0IDI0IiBmaWxsPSJub25lIiBzdHJva2U9IiNjOWE4NGMiIHN0cm9rZS13aWR0aD0iMS42Ij48cmVjdCB4PSIzIiB5PSI0IiB3aWR0aD0iMTgiIGhlaWdodD0iMTgiIHJ4PSIyIi8+PHBhdGggZD0iTTE2IDJ2NE04IDJ2NE0zIDEwaDE4Ii8+PC9zdmc+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJvcHQtdGV4dCI+PGRpdiBjbGFzcz0ib3B0LXRpdGxlIj5Cb29rIE9ubGluZTwvZGl2PjxkaXYgY2xhc3M9Im9wdC1oYW5kbGUiPtCf0L7RgNC+0LTQsCDihpIg0KPRgdC70YPQs9CwIOKGkiDQnNCw0YHRgtC10YAg4oaSINCS0YDQtdC80Y88L2Rpdj48L2Rpdj4KICAgIDxzcGFuIGNsYXNzPSJvcHQtYXJyb3ciPuKGkjwvc3Bhbj4KICA8L2J1dHRvbj4KICA8YnV0dG9uIGNsYXNzPSJvcHQiIG9uY2xpY2s9IndpbmRvdy5sb2NhdGlvbi5ocmVmPSd0ZWw6KzM3MjU4NzM1NDU2JyI+CiAgICA8ZGl2IGNsYXNzPSJvcHQtaWNvbiI+PHN2ZyB3aWR0aD0iMzIiIGhlaWdodD0iMzIiIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjYzlhODRjIiBzdHJva2Utd2lkdGg9IjEuNiI+PHBhdGggZD0iTTIyIDE2LjkydjNhMiAyIDAgMDEtMi4xOCAyIDE5Ljc5IDE5Ljc5IDAgMDEtOC42My0zLjA3QTE5LjUgMTkuNSAwIDAxMy4wNyA5LjgyYTE5Ljc5IDE5Ljc5IDAgMDEtMy4wNy04LjY3QTIgMiAwIDAxMiAxaDNhMiAyIDAgMDEyIDEuNzJjLjEyNy45Ni4zNjEgMS45MDMuNyAyLjgxYTIgMiAwIDAxLS40NSAyLjExTDYuOTEgOC45MWExNiAxNiAwIDAwNiA2bDEuMjctMS4yN2EyIDIgMCAwMTIuMTEtLjQ1Yy45MDcuMzM5IDEuODUuNTczIDIuODEuN0EyIDIgMCAwMTIyIDE2LjkyeiIvPjwvc3ZnPjwvZGl2PgogICAgPGRpdiBjbGFzcz0ib3B0LXRleHQiPjxkaXYgY2xhc3M9Im9wdC10aXRsZSI+Q2FsbCBVczwvZGl2PjxkaXYgY2xhc3M9Im9wdC1oYW5kbGUiPiszNzIgNTg3IDM1NDU2PC9kaXY+PC9kaXY+CiAgICA8c3BhbiBjbGFzcz0ib3B0LWFycm93Ij7ihpI8L3NwYW4+CiAgPC9idXR0b24+CiAgPGRpdiBjbGFzcz0iaG9tZS1mb290Ij4KICAgIDxzcGFuPlRhbGxpbm48L3NwYW4+PGRpdiBjbGFzcz0iZmRvdCI+PC9kaXY+PHNwYW4+RXN0b25pYTwvc3Bhbj48ZGl2IGNsYXNzPSJmZG90Ij48L2Rpdj48c3Bhbj7CqSAyMDI1IFImYW1wO0o8L3NwYW4+CiAgPC9kaXY+CjwvZGl2Pgo8L2Rpdj4KCjwhLS0gQk9PS0lORyAtLT4KPGRpdiBjbGFzcz0ic2NyZWVuIiBpZD0iYm9va1NjcmVlbiI+CjxkaXYgY2xhc3M9ImNvbiI+CiAgPGJ1dHRvbiBjbGFzcz0iYmFjay1idG4iIGlkPSJiYWNrQnRuIj7ihpAg0J3QsNC30LDQtDwvYnV0dG9uPgogIDxkaXYgY2xhc3M9ImxvZ28tcmoiPlImYW1wO0o8L2Rpdj4KICA8ZGl2IGNsYXNzPSJsb2dvLXN1YiI+R3Jvb21pbmcgwrcg0KLQsNC70LvQuNC9PC9kaXY+CiAgPGRpdiBjbGFzcz0icHJvZ3Jlc3MiPgogICAgPGRpdiBjbGFzcz0icHMgYWN0aXZlIiBpZD0icHMxIj48ZGl2IGNsYXNzPSJwZG90Ij48L2Rpdj7Qo9GB0LvRg9Cz0LA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBsIiBpZD0icGwxIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBzIiBpZD0icHMyIj48ZGl2IGNsYXNzPSJwZG90Ij48L2Rpdj7QnNCw0YHRgtC10YA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBsIiBpZD0icGwyIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InBzIiBpZD0icHMzIj48ZGl2IGNsYXNzPSJwZG90Ij48L2Rpdj7Qn9C40YLQvtC80LXRhjwvZGl2PgogICAgPGRpdiBjbGFzcz0icGwiIGlkPSJwbDMiPjwvZGl2PgogICAgPGRpdiBjbGFzcz0icHMiIGlkPSJwczQiPjxkaXYgY2xhc3M9InBkb3QiPjwvZGl2PtCU0LDRgtCwPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwbCIgaWQ9InBsNCI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJwcyIgaWQ9InBzNSI+PGRpdiBjbGFzcz0icGRvdCI+PC9kaXY+0JTQsNC90L3Ri9C1PC9kaXY+CiAgPC9kaXY+CgogIDwhLS0gU3RlcCAxIC0tPgogIDxkaXYgY2xhc3M9InN0ZXAgc2hvdyIgaWQ9ImJrMSI+CiAgICA8ZGl2IGNsYXNzPSJzbGJsIj4wMSDCtyDQn9C+0YDQvtC00LAg0YHQvtCx0LDQutC4PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJid3JhcCI+CiAgICAgIDxkaXYgY2xhc3M9InNib3giPgogICAgICAgIDxzcGFuIGNsYXNzPSJzaSI+8J+UjTwvc3Bhbj4KICAgICAgICA8aW5wdXQgaWQ9ImJJbnB1dCIgdHlwZT0idGV4dCIgcGxhY2Vob2xkZXI9ItCd0LDRh9C90LjRgtC1INCy0LLQvtC00LjRgtGMINC/0L7RgNC+0LTRgy4uLiIgYXV0b2NvbXBsZXRlPSJvZmYiPgogICAgICAgIDxidXR0b24gY2xhc3M9ImNsciIgaWQ9ImNsckJ0biI+4pyVPC9idXR0b24+CiAgICAgIDwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJkcm9wIiBpZD0iYkRyb3AiPjwvZGl2PgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzYmFkZ2UiIGlkPSJzQmFkZ2UiPjwvZGl2PgogICAgPGRpdiBpZD0ic3ZjU2VjIiBzdHlsZT0iZGlzcGxheTpub25lO21hcmdpbi10b3A6MTZweCI+CiAgICAgIDxkaXYgY2xhc3M9InNsYmwiPjAyIMK3INCj0YHQu9GD0LPQsDwvZGl2PgogICAgICA8ZGl2IGlkPSJzdmNMaXN0Ij48L2Rpdj4KICAgIDwvZGl2PgogIDwvZGl2PgoKICA8IS0tIFN0ZXAgMiAtLT4KICA8ZGl2IGNsYXNzPSJzdGVwIiBpZD0iYmsyIj4KICAgIDxkaXYgY2xhc3M9InNsYmwiPtCS0YvQsdC10YDQuNGC0LUg0LzQsNGB0YLQtdGA0LA8L2Rpdj4KICAgIDxkaXYgY2xhc3M9Im1hc3RlcnMiPgogICAgICA8ZGl2IGNsYXNzPSJtYnRuIiBkYXRhLW1hc3Rlcj0i0JDQvdGPIj48ZGl2IGNsYXNzPSJtYXYiPtCQPC9kaXY+PGRpdiBjbGFzcz0ibW5hbWUiPtCQ0L3RjzwvZGl2PjxkaXYgY2xhc3M9Im10aXRsZSI+0JPRgNGD0LzQtdGAPC9kaXY+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9Im1idG4iIGRhdGEtbWFzdGVyPSLQmtCw0YLRjyI+PGRpdiBjbGFzcz0ibWF2Ij7QmjwvZGl2PjxkaXYgY2xhc3M9Im1uYW1lIj7QmtCw0YLRjzwvZGl2PjxkaXYgY2xhc3M9Im10aXRsZSI+0JPRgNGD0LzQtdGAPC9kaXY+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9Im1idG4iIGRhdGEtbWFzdGVyPSLQnNCw0YjQsCI+PGRpdiBjbGFzcz0ibWF2Ij7QnDwvZGl2PjxkaXYgY2xhc3M9Im1uYW1lIj7QnNCw0YjQsDwvZGl2PjxkaXYgY2xhc3M9Im10aXRsZSI+0JPRgNGD0LzQtdGAPC9kaXY+PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9Im1idG4iIGRhdGEtbWFzdGVyPSLQl9C40L3QsCI+PGRpdiBjbGFzcz0ibWF2Ij7QlzwvZGl2PjxkaXYgY2xhc3M9Im1uYW1lIj7Ql9C40L3QsDwvZGl2PjxkaXYgY2xhc3M9Im10aXRsZSI+0JPRgNGD0LzQtdGAPC9kaXY+PC9kaXY+CiAgICA8L2Rpdj4KICA8L2Rpdj4KCiAgPCEtLSBTdGVwIDMgLS0+CiAgPGRpdiBjbGFzcz0ic3RlcCIgaWQ9ImJrMyI+CiAgICA8ZGl2IGNsYXNzPSJzbGJsIj7QmtCw0Log0LTQsNCy0L3QviDQsdGL0LvQuCDQsiDQs9GA0YPQvNC1PzwvZGl2PgogICAgPGJ1dHRvbiBjbGFzcz0iZ2J0biIgZGF0YS12YWw9ItCf0LXRgNCy0YvQuSDRgNCw0LciPtCf0LXRgNCy0YvQuSDRgNCw0Lc8L2J1dHRvbj4KICAgIDxidXR0b24gY2xhc3M9ImdidG4iIGRhdGEtdmFsPSIxINC80LXRgdGP0YYiPjEg0LzQtdGB0Y/RhjwvYnV0dG9uPgogICAgPGJ1dHRvbiBjbGFzcz0iZ2J0biIgZGF0YS12YWw9IjIg0LzQtdGB0Y/RhtCwIj4yINC80LXRgdGP0YbQsDwvYnV0dG9uPgogICAgPGJ1dHRvbiBjbGFzcz0iZ2J0biIgZGF0YS12YWw9IjMg0LzQtdGB0Y/RhtCwIj4zINC80LXRgdGP0YbQsDwvYnV0dG9uPgogICAgPGJ1dHRvbiBjbGFzcz0iZ2J0biIgZGF0YS12YWw9IjQg0LzQtdGB0Y/RhtCwIj40INC80LXRgdGP0YbQsDwvYnV0dG9uPgogICAgPGJ1dHRvbiBjbGFzcz0iZ2J0biIgZGF0YS12YWw9IjUg0LzQtdGB0Y/RhtC10LIiPjUg0LzQtdGB0Y/RhtC10LI8L2J1dHRvbj4KICAgIDxidXR0b24gY2xhc3M9ImdidG4iIGRhdGEtdmFsPSI2INC80LXRgdGP0YbQtdCyINC4INCx0L7Qu9C10LUiPjYg0LzQtdGB0Y/RhtC10LIg0Lgg0LHQvtC70LXQtTwvYnV0dG9uPgogIDwvZGl2PgoKICA8IS0tIFN0ZXAgNCAtLT4KICA8ZGl2IGNsYXNzPSJzdGVwIiBpZD0iYms0Ij4KICAgIDxkaXYgY2xhc3M9InNsYmwiPtCS0YvQsdC10YDQuNGC0LUg0LTQsNGC0YM8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImNhbC1oIj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iY2FsLW4iIGlkPSJwcmV2TSI+4oC5PC9idXR0b24+CiAgICAgIDxkaXYgY2xhc3M9ImNhbC1tIiBpZD0iY2FsTSI+PC9kaXY+CiAgICAgIDxidXR0b24gY2xhc3M9ImNhbC1uIiBpZD0ibmV4dE0iPuKAujwvYnV0dG9uPgogICAgPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJjZyIgaWQ9ImNhbEciPjwvZGl2PgogICAgPGRpdiBpZD0idGltZVNlYyIgc3R5bGU9ImRpc3BsYXk6bm9uZTttYXJnaW4tdG9wOjE2cHgiPgogICAgICA8ZGl2IGNsYXNzPSJzbGJsIj7QktGL0LHQtdGA0LjRgtC1INCy0YDQtdC80Y88L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0idGciIGlkPSJ0aW1lRyI+PC9kaXY+CiAgICA8L2Rpdj4KICA8L2Rpdj4KCiAgPCEtLSBTdGVwIDUgLS0+CiAgPGRpdiBjbGFzcz0ic3RlcCIgaWQ9ImJrNSI+CiAgICA8ZGl2IGNsYXNzPSJzbGJsIj7QktCw0YjQuCDQtNCw0L3QvdGL0LU8L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImZnIj48bGFiZWwgY2xhc3M9ImZsIj7QmNC80Y88L2xhYmVsPjxpbnB1dCBjbGFzcz0iZmkiIGlkPSJjTmFtZSIgdHlwZT0idGV4dCIgcGxhY2Vob2xkZXI9ItCS0LDRiNC1INC40LzRjyI+PC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJmZyI+PGxhYmVsIGNsYXNzPSJmbCI+0KLQtdC70LXRhNC+0L08L2xhYmVsPjxpbnB1dCBjbGFzcz0iZmkiIGlkPSJjUGhvbmUiIHR5cGU9InRlbCIgcGxhY2Vob2xkZXI9IiszNzIgLi4uIj48L2Rpdj4KICAgIDxkaXYgY2xhc3M9ImZnIj48bGFiZWwgY2xhc3M9ImZsIj7QmtC70LjRh9C60LAg0L/QuNGC0L7QvNGG0LA8L2xhYmVsPjxpbnB1dCBjbGFzcz0iZmkiIGlkPSJjUGV0IiB0eXBlPSJ0ZXh0IiBwbGFjZWhvbGRlcj0i0J3QtdC+0LHRj9C30LDRgtC10LvRjNC90L4iPjwvZGl2PgogICAgPGRpdiBjbGFzcz0ic3VtIiBpZD0ic3VtQmxvY2siPjwvZGl2PgogICAgPGJ1dHRvbiBjbGFzcz0iY2J0biIgaWQ9ImNvbmZpcm1CdG4iPtCf0L7QtNGC0LLQtdGA0LTQuNGC0Ywg0LfQsNC/0LjRgdGMPC9idXR0b24+CiAgPC9kaXY+CgogIDwhLS0gU3VjY2VzcyAtLT4KICA8ZGl2IGNsYXNzPSJzYmxvY2siIGlkPSJzdWNCbG9jayI+CiAgICA8ZGl2IGNsYXNzPSJzaTIiPvCfkL48L2Rpdj4KICAgIDxkaXYgY2xhc3M9InN0Ij7Ql9Cw0L/QuNGB0Ywg0L/RgNC40L3Rj9GC0LAhPC9kaXY+CiAgICA8ZGl2IGNsYXNzPSJzcyI+0JzRiyDRgdCy0Y/QttC10LzRgdGPINGBINCy0LDQvNC4INC00LvRjyDQv9C+0LTRgtCy0LXRgNC20LTQtdC90LjRjy48YnI+0KHQv9Cw0YHQuNCx0L4sINGH0YLQviDQstGL0LHRgNCw0LvQuCBSJkogR3Jvb21pbmchPC9kaXY+CiAgICA8YnV0dG9uIGNsYXNzPSJoYnRuIiBpZD0iaG9tZUJ0biI+4oaQINCd0LAg0LPQu9Cw0LLQvdGD0Y48L2J1dHRvbj4KICA8L2Rpdj4KPC9kaXY+CjwvZGl2PgoKPHNjcmlwdD4KdmFyIERBVEEgPSBbeyJicmVlZCI6ICLQkNC60LjRgtCwLdC40L3RgyAyMOKAkzQwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNjAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNzUuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogOTAuMH19LCB7ImJyZWVkIjogItCQ0LrQuNGC0LAt0LjQvdGDINCx0L7Qu9C10LUgNDAg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA4NS4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA5NS4wLCAi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOiAxMTUuMH19LCB7ImJyZWVkIjogItCQ0LrQuNGC0LAt0LjQvdGDINGE0LvQsNGE0YTQuCAyMOKAkzQwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNjAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNzUuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogOTAuMH19LCB7ImJyZWVkIjogItCQ0LrQuNGC0LAt0LjQvdGDINGE0LvQsNGE0YTQuCDQsdC+0LvQtdC1IDQwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogODUuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogOTUuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogMTE1LjB9fSwgeyJicmVlZCI6ICLQkNC80LXRgNC40LrQsNC90YHQutCw0Y8g0LDQutC40YLQsCAyMOKAkzQwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNjAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNzUuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogOTAuMH19LCB7ImJyZWVkIjogItCQ0LzQtdGA0LjQutCw0L3RgdC60LDRjyDQsNC60LjRgtCwINCx0L7Qu9C10LUgNDAg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA4NS4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA5NS4wLCAi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOiAxMTUuMH19LCB7ImJyZWVkIjogItCQ0LzQtdGA0LjQutCw0L3RgdC60LDRjyDQsNC60LjRgtCwINGE0LvQsNGE0YTQuCAyMOKAkzQwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNjAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNzUuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogOTAuMH19LCB7ImJyZWVkIjogItCQ0LzQtdGA0LjQutCw0L3RgdC60LDRjyDQsNC60LjRgtCwINGE0LvQsNGE0YTQuCDQsdC+0LvQtdC1IDQwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogODUuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogOTUuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogMTE1LjB9fSwgeyJicmVlZCI6ICLQkNC70LDQsdCw0LkgNDDigJM2MCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDg1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDk1LjAsICLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6IDExNS4wfX0sIHsiYnJlZWQiOiAi0JDQu9Cw0LHQsNC5INCx0L7Qu9C10LUgNjAg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiAxMDAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogMTE1LjAsICLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6IDEzMC4wfX0sIHsiYnJlZWQiOiAi0JDQu9GP0YHQutC40L3RgdC60LjQuSDQvNCw0LvQsNC80YPRgiAyMOKAkzQwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNjAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNzUuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogOTAuMH19LCB7ImJyZWVkIjogItCQ0LvRj9GB0LrQuNC90YHQutC40Lkg0LzQsNC70LDQvNGD0YIg0LHQvtC70LXQtSA0MCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDg1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDk1LjAsICLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6IDExNS4wfX0sIHsiYnJlZWQiOiAi0JDQu9GP0YHQutC40L3RgdC60LjQuSDQvNCw0LvQsNC80YPRgiDRhNC70LDRhNGE0LggMjDigJM0MCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDYwLjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDc1LjAsICLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6IDkwLjB9fSwgeyJicmVlZCI6ICLQkNC70Y/RgdC60LjQvdGB0LrQuNC5INC80LDQu9Cw0LzRg9GCINGE0LvQsNGE0YTQuCDQsdC+0LvQtdC1IDQwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogODUuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogOTUuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogMTE1LjB9fSwgeyJicmVlZCI6ICLQkNC80LXRgNC40LrQsNC90YHQutC40Lkg0LrQvtC60LXRgC3RgdC/0LDQvdC40LXQu9GMIDEw4oCTMTUg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA0MC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA1NS4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDcwLjB9fSwgeyJicmVlZCI6ICLQkNC80LXRgNC40LrQsNC90YHQutC40Lkg0LrQvtC60LXRgC3RgdC/0LDQvdC40LXQu9GMIDE14oCTMjAg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA0NS4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA2NS4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDgwLjB9fSwgeyJicmVlZCI6ICLQkNC90LPQu9C40LnRgdC60LjQuSDQsdGD0LvRjNC00L7QsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA0NS4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA1NS4wLCAi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOiA3MC4wfX0sIHsiYnJlZWQiOiAi0JDQvdCz0LvQuNC50YHQutC40Lkg0LrQvtC60LXRgC3RgdC/0LDQvdC40LXQu9GMIDEw4oCTMTUg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA0MC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA1NS4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDcwLjB9fSwgeyJicmVlZCI6ICLQkNC90LPQu9C40LnRgdC60LjQuSDQutC+0LrQtdGALdGB0L/QsNC90LjQtdC70YwgMTXigJMyMCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDQ1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDY1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogODAuMH19LCB7ImJyZWVkIjogItCQ0YTQs9Cw0L0gMjDigJMzMCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDUwLjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDcwLjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogOTAuMH19LCB7ImJyZWVkIjogItCQ0YTQs9Cw0L0gMzDigJM0MCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDYwLjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDgwLjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogMTAwLjB9fSwgeyJicmVlZCI6ICLQkdC10YDQvdGB0LrQuNC5INC30LXQvdC90LXQvdGF0YPQvdC0IDMw4oCTNDAg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA3MC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA4NS4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDExMC4wLCAi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOiAxMDAuMH19LCB7ImJyZWVkIjogItCR0LXRgNC90YHQutC40Lkg0LfQtdC90L3QtdC90YXRg9C90LQg0LHQvtC70LXQtSA0MCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDg1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDk1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogMTMwLjAsICLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6IDExNS4wfX0sIHsiYnJlZWQiOiAi0JHQuNCz0LvRjCAxMOKAkzE1INC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogMzUuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNDUuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogNjAuMH19LCB7ImJyZWVkIjogItCR0LjQs9C70YwgMTXigJMyMCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDQwLjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDUwLjAsICLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6IDY1LjB9fSwgeyJicmVlZCI6ICLQkdC40YjQvtC9LdGE0YDQuNC30LUg0LTQviA1INC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogMzAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNDAuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiA1NS4wfX0sIHsiYnJlZWQiOiAi0JHQuNGI0L7QvS3RhNGA0LjQt9C1IDXigJMxMCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDM1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDQ1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogNjAuMH19LCB7ImJyZWVkIjogItCR0L7RgNC00LXRgC3QutC+0LvQu9C4IDE14oCTMjAg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA1MC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA3MC4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDkwLjAsICLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6IDgwLjB9fSwgeyJicmVlZCI6ICLQkdC+0YDQtNC10YAt0LrQvtC70LvQuCAyMOKAkzI1INC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNjAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNzUuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiAxMDAuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogOTAuMH19LCB7ImJyZWVkIjogItCR0YDQsNCx0LDQvdGB0L7QvSIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiAzMC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA0MC4wLCAi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOiA1MC4wfX0sIHsiYnJlZWQiOiAi0JLQtdC70YzRiC3QutC+0YDQs9C4IDEw4oCTMTUg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA0NS4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA2MC4wLCAi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOiA3MC4wfX0sIHsiYnJlZWQiOiAi0JLQtdC70YzRiC3QutC+0YDQs9C4IDE14oCTMjAg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA1MC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA3MC4wLCAi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOiA4MC4wfX0sIHsiYnJlZWQiOiAi0JLQtdGB0YIt0YXQsNC50LvQtdC90LQt0LLQsNC50YIt0YLQtdGA0YzQtdGAIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDM1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDQ1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogNjAuMCwgItCi0YDQuNC80LzQuNC90LMiOiA2NS4wfX0sIHsiYnJlZWQiOiAi0JPQvtC70LTQtdC9LdGA0LXRgtGA0LjQstC10YAgMjDigJMzMCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDYwLjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDc1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogMTAwLjAsICLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6IDkwLjB9fSwgeyJicmVlZCI6ICLQk9C+0LvQtNC10L0t0YDQtdGC0YDQuNCy0LXRgCAzMOKAkzQwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNzAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogODUuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiAxMTAuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogMTAwLjB9fSwgeyJicmVlZCI6ICLQk9GA0LjRhNGE0L7QvSIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiAzNS4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA0NS4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDYwLjAsICLQotGA0LjQvNC80LjQvdCzIjogNjUuMH19LCB7ImJyZWVkIjogItCU0LbQtdC6LdGA0LDRgdGB0LXQuy3RgtC10YDRjNC10YAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiAzMC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA0MC4wLCAi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOiA1MC4wfX0sIHsiYnJlZWQiOiAi0JTQttC10Lot0YDQsNGB0YHQtdC7LdGC0LXRgNGM0LXRgCDQttC10YHRgtC60L7RiNC10YDRgdGC0L3Ri9C5IiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDM1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDQ1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogNjAuMCwgItCi0YDQuNC80LzQuNC90LMiOiA2NS4wfX0sIHsiYnJlZWQiOiAi0JTQsNC70LzQsNGC0LjQvSIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA0NS4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA1NS4wLCAi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOiA3MC4wfX0sIHsiYnJlZWQiOiAi0JTQvtCx0LXRgNC80LDQvSAzMOKAkzQwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNTUuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNjUuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogODAuMH19LCB7ImJyZWVkIjogItCU0L7QsdC10YDQvNCw0L0g0LHQvtC70LXQtSA0MCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDcwLjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDgwLjAsICLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6IDk1LjB9fSwgeyJicmVlZCI6ICLQmNGA0LvQsNC90LTRgdC60LjQuSDQvNGP0LPQutC+0YjQtdGA0YHRgtC90YvQuSDQv9GI0LXQvdC40YfQvdGL0Lkg0YLQtdGA0YzQtdGAIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDQ1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDY1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogODAuMH19LCB7ImJyZWVkIjogItCY0YDQu9Cw0L3QtNGB0LrQuNC5INGC0LXRgNGM0LXRgCIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA0MC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA1NS4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDcwLjAsICLQotGA0LjQvNC80LjQvdCzIjogNzUuMH19LCB7ImJyZWVkIjogItCY0YHQv9Cw0L3RgdC60LjQuSDQs9Cw0LvRjNCz0L4gMjDigJMzMCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDQ1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDU1LjAsICLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6IDcwLjB9fSwgeyJicmVlZCI6ICLQmNGB0L/QsNC90YHQutC40Lkg0LPQsNC70YzQs9C+IDMw4oCTNDAg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA1NS4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA2NS4wLCAi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOiA4MC4wfX0sIHsiYnJlZWQiOiAi0JnQvtGA0LrRiNC40YDRgdC60LjQuSDRgtC10YDRjNC10YAg0LTQviAzLDUg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiAzMC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA0MC4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDU1LjB9fSwgeyJicmVlZCI6ICLQmdC+0YDQutGI0LjRgNGB0LrQuNC5INGC0LXRgNGM0LXRgCDQsdC+0LvQtdC1IDMsNSDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDM1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDQ1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogNjAuMH19LCB7ImJyZWVkIjogItCR0LjQstC10YAt0LnQvtGA0Log0LTQviAzLDUg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiAzMC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA0MC4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDU1LjB9fSwgeyJicmVlZCI6ICLQkdC40LLQtdGALdC50L7RgNC6INCx0L7Qu9C10LUgMyw1INC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogMzUuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNDUuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiA2MC4wfX0sIHsiYnJlZWQiOiAi0JrQsNCy0LDQu9C10YAt0LrQuNC90LMt0YfQsNGA0LvRjNC3LdGB0L/QsNC90LjQtdC70YwgNeKAkzEwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogMzUuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNDUuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiA2MC4wfX0sIHsiYnJlZWQiOiAi0JrQsNCy0LDQu9C10YAt0LrQuNC90LMt0YfQsNGA0LvRjNC3LdGB0L/QsNC90LjQtdC70YwgMTDigJMxNSDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDQwLjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDU1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogNzAuMH19LCB7ImJyZWVkIjogItCa0LjRgtCw0LnRgdC60LDRjyDRhdC+0YXQu9Cw0YLQsNGPINC/0YPRhdC+0LLQsNGPINC00L4gNSDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDMwLjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDQwLjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogNTUuMH19LCB7ImJyZWVkIjogItCa0LjRgtCw0LnRgdC60LDRjyDRhdC+0YXQu9Cw0YLQsNGPINC/0YPRhdC+0LLQsNGPIDXigJMxMCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDM1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDQ1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogNjAuMH19LCB7ImJyZWVkIjogItCa0LjRgtCw0LnRgdC60LDRjyDRhdC+0YXQu9Cw0YLQsNGPINCz0L7Qu9Cw0Y8g0LTQviA1INC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogMjguMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogMzUuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiA1NS4wfX0sIHsiYnJlZWQiOiAi0JrQuNGC0LDQudGB0LrQsNGPINGF0L7RhdC70LDRgtCw0Y8g0LPQvtC70LDRjyA14oCTMTAg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiAzMi4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA0Mi4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDYwLjB9fSwgeyJicmVlZCI6ICLQmtC+0LvQu9C4IDIw4oCTMzAg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA2MC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA3NS4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDEwMC4wLCAi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOiA5MC4wfX0sIHsiYnJlZWQiOiAi0JrQvtC70LvQuCAzMOKAkzQwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNzAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogODUuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiAxMTAuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogMTAwLjB9fSwgeyJicmVlZCI6ICLQmtC+0LzQvtC90LTQvtGAIDMw4oCTNDAg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA3MC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA4NS4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDExMC4wfX0sIHsiYnJlZWQiOiAi0JrQvtC80L7QvdC00L7RgCDQsdC+0LvQtdC1IDQwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogODUuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogOTUuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiAxMzAuMH19LCB7ImJyZWVkIjogItCb0LDQsdGA0LDQtNC+0YAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSAyMOKAkzMwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNDUuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNTUuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogNzAuMH19LCB7ImJyZWVkIjogItCb0LDQsdGA0LDQtNC+0YAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSAzMOKAkzQwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNTUuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNjUuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogODAuMH19LCB7ImJyZWVkIjogItCb0LDQsdGA0LDQtNC+0YAg0LPQu9Cw0LTQutC+0YjQtdGA0YHRgtC90YvQuSDQsdC+0LvQtdC1IDQwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNzAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogODAuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogOTUuMH19LCB7ImJyZWVkIjogItCb0LDQsdGA0LDQtNC+0YAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90YvQuSAyMOKAkzMwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNjAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNzUuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiAxMDAuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogOTAuMH19LCB7ImJyZWVkIjogItCb0LDQsdGA0LDQtNC+0YAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90YvQuSAzMOKAkzQwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNzAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogODUuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiAxMTAuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogMTAwLjB9fSwgeyJicmVlZCI6ICLQm9Cw0LHRgNCw0LTQvtGAINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdGL0Lkg0LHQvtC70LXQtSA0MCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDg1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDk1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogMTMwLjAsICLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6IDExNS4wfX0sIHsiYnJlZWQiOiAi0JvQsNCx0YDQsNC00YPQtNC10LvRjCAxMOKAkzIwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNDAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNTUuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiA3MC4wfX0sIHsiYnJlZWQiOiAi0JvQsNCx0YDQsNC00YPQtNC10LvRjCAyMOKAkzMwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNTAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNzAuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiA5MC4wfX0sIHsiYnJlZWQiOiAi0JvQsNCx0YDQsNC00YPQtNC10LvRjCAzMOKAkzQwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNjAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogODAuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiAxMDAuMH19LCB7ImJyZWVkIjogItCc0LDQu9GM0YLQuNC/0YMg0LTQviA1INC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogMzAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNDAuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiA1NS4wfX0sIHsiYnJlZWQiOiAi0JzQsNC70YzRgtC40L/RgyA14oCTMTAg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiAzNS4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA0NS4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDYwLjB9fSwgeyJicmVlZCI6ICLQnNCw0LvRjNGC0LjQv9GDIDEw4oCTMTUg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA0MC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA1NS4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDcwLjB9fSwgeyJicmVlZCI6ICLQnNCw0LvRjNGC0LXQt9C1IiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDMwLjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDQwLjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogNTUuMH19LCB7ImJyZWVkIjogItCc0LjRgtGC0LXQu9GM0YjQvdCw0YPRhtC10YAgMTDigJMxNSDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDQwLjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDU1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogNzAuMCwgItCi0YDQuNC80LzQuNC90LMiOiA3NS4wfX0sIHsiYnJlZWQiOiAi0JzQuNGC0YLQtdC70YzRiNC90LDRg9GG0LXRgCAxNeKAkzIwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNDUuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNjUuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiA4MC4wLCAi0KLRgNC40LzQvNC40L3QsyI6IDg1LjB9fSwgeyJicmVlZCI6ICLQnNC+0L/RgSIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiAzMC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA0MC4wLCAi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOiA1MC4wfX0sIHsiYnJlZWQiOiAi0J3QtdCy0YHQutCw0Y8g0L7RgNGF0LjQtNC10Y8iLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogMzAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNDAuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiA1NS4wfX0sIHsiYnJlZWQiOiAi0J3QtdC80LXRhtC60LDRjyDQvtCy0YfQsNGA0LrQsCAyMOKAkzMwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNjAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNzUuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogOTAuMH19LCB7ImJyZWVkIjogItCd0LXQvNC10YbQutCw0Y8g0L7QstGH0LDRgNC60LAgMzDigJM0MCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDcwLjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDg1LjAsICLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6IDEwMC4wfX0sIHsiYnJlZWQiOiAi0J3QtdC80LXRhtC60LDRjyDQvtCy0YfQsNGA0LrQsCDQsdC+0LvQtdC1IDQwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogODUuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogOTUuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogMTE1LjB9fSwgeyJicmVlZCI6ICLQndC+0YDQstC40Yct0YLQtdGA0YzQtdGAIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDM1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDQ1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogNjAuMCwgItCi0YDQuNC80LzQuNC90LMiOiA2NS4wfX0sIHsiYnJlZWQiOiAi0J3QvtGA0YTQvtC70Lot0YLQtdGA0YzQtdGAIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDM1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDQ1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogNjAuMCwgItCi0YDQuNC80LzQuNC90LMiOiA2NS4wfX0sIHsiYnJlZWQiOiAi0J3RjNGO0YTQsNGD0L3QtNC70LXQvdC0IDQw4oCTNjAg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA4NS4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA5NS4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDEzMC4wLCAi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOiAxMTUuMH19LCB7ImJyZWVkIjogItCd0YzRjtGE0LDRg9C90LTQu9C10L3QtCDQsdC+0LvQtdC1IDYwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogMTAwLjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDExNS4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDE1MC4wLCAi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOiAxMzAuMH19LCB7ImJyZWVkIjogItCf0LDQv9C40LnQvtC9IiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDMwLjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDQwLjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogNTUuMH19LCB7ImJyZWVkIjogItCf0LXQutC40L3QtdGBINC00L4gNSDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDMwLjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDQwLjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogNTUuMH19LCB7ImJyZWVkIjogItCf0LXQutC40L3QtdGBIDXigJMxMCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDM1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDQ1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogNjAuMH19LCB7ImJyZWVkIjogItCf0YPQtNC10LvRjCDRgtC+0Lkg0LTQviA1INC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogMzAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNDAuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiA1NS4wfX0sIHsiYnJlZWQiOiAi0J/Rg9C00LXQu9GMINC60LDRgNC70LjQutC+0LLRi9C5IDXigJMxMCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDM1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDQ1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogNjAuMH19LCB7ImJyZWVkIjogItCf0YPQtNC10LvRjCDQvNCw0LvRi9C5IDEw4oCTMTUg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA0MC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA1NS4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDcwLjB9fSwgeyJicmVlZCI6ICLQn9GD0LTQtdC70Ywg0LzQsNC70YvQuSAxNeKAkzIwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNDUuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNjUuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiA4MC4wfX0sIHsiYnJlZWQiOiAi0J/Rg9C00LXQu9GMINCx0L7Qu9GM0YjQvtC5IDIw4oCTMzAg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA1MC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA3MC4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDkwLjB9fSwgeyJicmVlZCI6ICLQn9GD0LTQtdC70Ywg0LHQvtC70YzRiNC+0LkgMzDigJM0MCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDYwLjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDgwLjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogMTAwLjB9fSwgeyJicmVlZCI6ICLQoNC40LfQtdC90YjQvdCw0YPRhtC10YAgMzDigJM0MCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDYwLjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDgwLjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogMTAwLjAsICLQotGA0LjQvNC80LjQvdCzIjogMTEwLjB9fSwgeyJicmVlZCI6ICLQoNC40LfQtdC90YjQvdCw0YPRhtC10YAg0LHQvtC70LXQtSA0MCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDc1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDk1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogMTIwLjAsICLQotGA0LjQvNC80LjQvdCzIjogMTI1LjB9fSwgeyJicmVlZCI6ICLQoNGD0YHRgdC60LjQuSDQvtGF0L7RgtC90LjRh9C40Lkg0YHQv9Cw0L3QuNC10LvRjCAxMOKAkzE1INC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNDAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNTUuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiA3MC4wfX0sIHsiYnJlZWQiOiAi0KDRg9GB0YHQutC40Lkg0L7RhdC+0YLQvdC40YfQuNC5INGB0L/QsNC90LjQtdC70YwgMTXigJMyMCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDQ1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDY1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogODAuMH19LCB7ImJyZWVkIjogItCg0YPRgdGB0LrQuNC5INGC0L7QuSDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3Ri9C5IiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDI1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDM1LjB9fSwgeyJicmVlZCI6ICLQoNGD0YHRgdC60LjQuSDRgtC+0Lkg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90YvQuSIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiAzMC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA0MC4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDU1LjB9fSwgeyJicmVlZCI6ICLQoNGD0YHRgdC60LDRjyDRhtCy0LXRgtC90LDRjyDQsdC+0LvQvtC90LrQsCIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiAzNS4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA0NS4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDYwLjB9fSwgeyJicmVlZCI6ICLQoNGD0YHRgdC60LjQuSDRh9C10YDQvdGL0Lkg0YLQtdGA0YzQtdGAIDMw4oCTNDAg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA2MC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA4MC4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDEwMC4wfX0sIHsiYnJlZWQiOiAi0KDRg9GB0YHQutC40Lkg0YfQtdGA0L3Ri9C5INGC0LXRgNGM0LXRgCDQsdC+0LvQtdC1IDQwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNzUuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogOTUuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiAxMjAuMH19LCB7ImJyZWVkIjogItCh0LDQvNC+0LXQtCAyMOKAkzMwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNjAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNzUuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogOTAuMH19LCB7ImJyZWVkIjogItCh0LDQvNC+0LXQtCAzMOKAkzQwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNzAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogODUuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogMTAwLjB9fSwgeyJicmVlZCI6ICLQodC10YLRgtC10YAg0LDQvdCz0LvQuNC50YHQutC40LkgMjDigJMzMCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDUwLjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDcwLjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogOTAuMH19LCB7ImJyZWVkIjogItCh0LXRgtGC0LXRgCDQuNGA0LvQsNC90LTRgdC60LjQuSAyMOKAkzMwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNTAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNzAuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiA5MC4wfX0sIHsiYnJlZWQiOiAi0KHQtdGC0YLQtdGAINCz0L7RgNC00L7QvSAzMOKAkzQwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNjAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogODAuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiAxMDAuMH19LCB7ImJyZWVkIjogItCh0LjQsdCwLdC40L3RgyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA0NS4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA2MC4wLCAi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOiA3MC4wfX0sIHsiYnJlZWQiOiAi0KHQutC+0YLRhy3RgtC10YDRjNC10YAiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogMzUuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNDUuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiA2MC4wLCAi0KLRgNC40LzQvNC40L3QsyI6IDY1LjB9fSwgeyJicmVlZCI6ICLQodC40LvQuNGF0LXQvC3RgtC10YDRjNC10YAiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogMzUuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNDUuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiA2MC4wLCAi0KLRgNC40LzQvNC40L3QsyI6IDY1LjB9fSwgeyJicmVlZCI6ICLQotCw0LrRgdCwINCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdCw0Y8g0LrRgNC+0LvQuNGH0YzRjyDQtNC+IDUg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiAyNS4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiAzNS4wLCAi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOiA0NS4wfX0sIHsiYnJlZWQiOiAi0KLQsNC60YHQsCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3QsNGPINC60LDRgNC70LjQutC+0LLQsNGPIDXigJMxMCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDMwLjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDQwLjAsICLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6IDUwLjB9fSwgeyJicmVlZCI6ICLQotCw0LrRgdCwINCz0LvQsNC00LrQvtGI0LXRgNGB0YLQvdCw0Y8g0YHRgtCw0L3QtNCw0YDRgtC90LDRjyAxMOKAkzE1INC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogMzUuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNDUuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogNjAuMH19LCB7ImJyZWVkIjogItCi0LDQutGB0LAg0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90LDRjyDQutGA0L7Qu9C40YfRjNGPINC00L4gNSDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDMwLjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDQwLjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogNTUuMCwgItCi0YDQuNC80LzQuNC90LMiOiA1NS4wfX0sIHsiYnJlZWQiOiAi0KLQsNC60YHQsCDQttC10YHRgtC60L7RiNC10YDRgdGC0L3QsNGPINC60LDRgNC70LjQutC+0LLQsNGPIDXigJMxMCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDM1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDQ1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogNjAuMCwgItCi0YDQuNC80LzQuNC90LMiOiA2NS4wfX0sIHsiYnJlZWQiOiAi0KLQsNC60YHQsCDQttC10YHRgtC60L7RiNC10YDRgdGC0L3QsNGPINGB0YLQsNC90LTQsNGA0YLQvdCw0Y8gMTDigJMxNSDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDQwLjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDU1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogNzAuMCwgItCi0YDQuNC80LzQuNC90LMiOiA3NS4wfX0sIHsiYnJlZWQiOiAi0KLQsNC60YHQsCDQtNC70LjQvdC90L7RiNC10YDRgdGC0L3QsNGPINC60YDQvtC70LjRh9GM0Y8g0LTQviA1INC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogMzAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNDAuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiA1NS4wfX0sIHsiYnJlZWQiOiAi0KLQsNC60YHQsCDQtNC70LjQvdC90L7RiNC10YDRgdGC0L3QsNGPINC60LDRgNC70LjQutC+0LLQsNGPIDXigJMxMCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDM1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDQ1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogNjAuMH19LCB7ImJyZWVkIjogItCi0LDQutGB0LAg0LTQu9C40L3QvdC+0YjQtdGA0YHRgtC90LDRjyDRgdGC0LDQvdC00LDRgNGC0L3QsNGPIDEw4oCTMTUg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA0MC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA1NS4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDcwLjB9fSwgeyJicmVlZCI6ICLQpNC+0LrRgdGC0LXRgNGM0LXRgCDQttC10YHRgtC60L7RiNC10YDRgdGC0L3Ri9C5IDXigJMxMCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDM1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDQ1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogNjAuMCwgItCi0YDQuNC80LzQuNC90LMiOiA2NS4wfX0sIHsiYnJlZWQiOiAi0KTQvtC60YHRgtC10YDRjNC10YAg0LbQtdGB0YLQutC+0YjQtdGA0YHRgtC90YvQuSAxMOKAkzE1INC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNDAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNTUuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiA3MC4wLCAi0KLRgNC40LzQvNC40L3QsyI6IDc1LjB9fSwgeyJicmVlZCI6ICLQpNGA0LDQvdGG0YPQt9GB0LrQuNC5INCx0YPQu9GM0LTQvtCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDM1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDQ1LjAsICLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6IDYwLjB9fSwgeyJicmVlZCI6ICLQpdCw0YHQutC4IDIw4oCTMzAg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA2MC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA3NS4wLCAi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOiA5MC4wfX0sIHsiYnJlZWQiOiAi0KXQsNGB0LrQuCAzMOKAkzQwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNzAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogODUuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogMTAwLjB9fSwgeyJicmVlZCI6ICLQptCy0LXRgNCz0YjQvdCw0YPRhtC10YAgNeKAkzEwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogMzUuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNDUuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiA2MC4wLCAi0KLRgNC40LzQvNC40L3QsyI6IDY1LjB9fSwgeyJicmVlZCI6ICLQptCy0LXRgNCz0YjQvdCw0YPRhtC10YAgMTDigJMxNSDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDQwLjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDU1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogNzAuMCwgItCi0YDQuNC80LzQuNC90LMiOiA3NS4wfX0sIHsiYnJlZWQiOiAi0KfQsNGDLdGH0LDRgyAyMOKAkzMwINC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNjAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNzUuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiAxMDAuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogOTAuMH19LCB7ImJyZWVkIjogItCn0LDRgy3Rh9Cw0YMgMzDigJM0MCDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDcwLjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDg1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogMTEwLjAsICLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6IDEwMC4wfX0sIHsiYnJlZWQiOiAi0KfQuNGF0YPQsNGF0YPQsCDQs9C70LDQtNC60L7RiNC10YDRgdGC0L3Ri9C5IiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDI1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDM1LjB9fSwgeyJicmVlZCI6ICLQp9C40YXRg9Cw0YXRg9CwINC00LvQuNC90L3QvtGI0LXRgNGB0YLQvdGL0LkiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogMzAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNDAuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiA1NS4wfX0sIHsiYnJlZWQiOiAi0KjQsNGA0L/QtdC5IDE14oCTMjAg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA0MC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA1MC4wLCAi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOiA2NS4wfX0sIHsiYnJlZWQiOiAi0KjQsNGA0L/QtdC5IDIw4oCTMzAg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA0NS4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA1NS4wLCAi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOiA3MC4wfX0sIHsiYnJlZWQiOiAi0KjQtdC70YLQuCIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA0MC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA1MC4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDY1LjAsICLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6IDYwLjB9fSwgeyJicmVlZCI6ICLQqNC4LdGC0YbRgyDQtNC+IDUg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiAzMC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA0MC4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDU1LjB9fSwgeyJicmVlZCI6ICLQqNC4LdGC0YbRgyA14oCTMTAg0LrQsyIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiAzNS4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA0NS4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDYwLjB9fSwgeyJicmVlZCI6ICLQqNC/0LjRhiDQvdC10LzQtdGG0LrQuNC5IC8g0L/QvtC80LXRgNCw0L3RgdC60LjQuSDQtNC+IDMsNSDQutCzIiwgInNlcnZpY2VzIjogeyLQkdCw0LfQvtCy0YvQuSDRg9GF0L7QtCI6IDM1LjAsICLQk9C40LPQuNC10L3QuNGH0LXRgdC60LjQuSDRg9GF0L7QtCI6IDQ1LjAsICLQmtC+0LzQv9C70LXQutGB0L3Ri9C5INGD0YXQvtC0IjogNjAuMCwgItCt0LrRgdC/0YDQtdGB0YEt0LvQuNC90YzQutCwIjogNTUuMH19LCB7ImJyZWVkIjogItCo0L/QuNGGINC90LXQvNC10YbQutC40LkgLyDQv9C+0LzQtdGA0LDQvdGB0LrQuNC5INCx0L7Qu9C10LUgMyw1INC60LMiLCAic2VydmljZXMiOiB7ItCR0LDQt9C+0LLRi9C5INGD0YXQvtC0IjogNDAuMCwgItCT0LjQs9C40LXQvdC40YfQtdGB0LrQuNC5INGD0YXQvtC0IjogNTAuMCwgItCa0L7QvNC/0LvQtdC60YHQvdGL0Lkg0YPRhdC+0LQiOiA2NS4wLCAi0K3QutGB0L/RgNC10YHRgS3Qu9C40L3RjNC60LAiOiA2MC4wfX0sIHsiYnJlZWQiOiAi0KjQv9C40YYg0Y/Qv9C+0L3RgdC60LjQuSIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiA0MC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA1MC4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDY1LjAsICLQrdC60YHQv9GA0LXRgdGBLdC70LjQvdGM0LrQsCI6IDYwLjB9fSwgeyJicmVlZCI6ICLQr9C/0L7QvdGB0LrQuNC5INGF0LjQvSIsICJzZXJ2aWNlcyI6IHsi0JHQsNC30L7QstGL0Lkg0YPRhdC+0LQiOiAzMC4wLCAi0JPQuNCz0LjQtdC90LjRh9C10YHQutC40Lkg0YPRhdC+0LQiOiA0MC4wLCAi0JrQvtC80L/Qu9C10LrRgdC90YvQuSDRg9GF0L7QtCI6IDU1LjB9fV07CnZhciBSQUlMV0FZID0gImh0dHBzOi8vZHluYW1pYy1jb29wZXJhdGlvbi1wcm9kdWN0aW9uLWRkOTUudXAucmFpbHdheS5hcHAvYm9vayI7CnZhciBib29raW5nID0ge2JyZWVkOicnLHNlcnZpY2U6JycscHJpY2U6MCxtYXN0ZXI6JycsZ3Jvb21IaXN0b3J5OicnLGRhdGU6JycsdGltZTonJ307CnZhciBzZWxCcmVlZCA9IG51bGw7CnZhciBjWSA9IG5ldyBEYXRlKCkuZ2V0RnVsbFllYXIoKTsKdmFyIGNNID0gbmV3IERhdGUoKS5nZXRNb250aCgpOwp2YXIgc3RlcCA9IDE7CnZhciBNT05USFMgPSBbJ9Cv0L3QstCw0YDRjCcsJ9Ck0LXQstGA0LDQu9GMJywn0JzQsNGA0YInLCfQkNC/0YDQtdC70YwnLCfQnNCw0LknLCfQmNGO0L3RjCcsJ9CY0Y7Qu9GMJywn0JDQstCz0YPRgdGCJywn0KHQtdC90YLRj9Cx0YDRjCcsJ9Ce0LrRgtGP0LHRgNGMJywn0J3QvtGP0LHRgNGMJywn0JTQtdC60LDQsdGA0YwnXTsKdmFyIFRJTUVTID0gWycxMDowMCcsJzEwOjMwJywnMTE6MDAnLCcxMTozMCcsJzEyOjAwJywnMTM6MDAnLCcxNDowMCcsJzE0OjMwJywnMTU6MDAnLCcxNTozMCcsJzE2OjAwJywnMTc6MDAnXTsKCmZ1bmN0aW9uIHNob3dTY3JlZW4oaWQpIHsKICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuc2NyZWVuJykuZm9yRWFjaChmdW5jdGlvbihzKXtzLmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpO30pOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKGlkKS5jbGFzc0xpc3QuYWRkKCdhY3RpdmUnKTsKICB3aW5kb3cuc2Nyb2xsVG8oMCwwKTsKfQoKZnVuY3Rpb24gZ29TdGVwKG4pIHsKICBbJ2JrMScsJ2JrMicsJ2JrMycsJ2JrNCcsJ2JrNSddLmZvckVhY2goZnVuY3Rpb24oaWQsaSl7CiAgICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZChpZCkuY2xhc3NOYW1lID0gJ3N0ZXAnICsgKGkrMT09PW4/JyBzaG93JzonJyk7CiAgfSk7CiAgZm9yKHZhciBpPTE7aTw9NTtpKyspewogICAgdmFyIHBzPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdwcycraSk7CiAgICB2YXIgcGw9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3BsJytpKTsKICAgIGlmKGk8bil7cHMuY2xhc3NOYW1lPSdwcyBkb25lJztpZihwbClwbC5jbGFzc05hbWU9J3BsIGRvbmUnO30KICAgIGVsc2UgaWYoaT09PW4pe3BzLmNsYXNzTmFtZT0ncHMgYWN0aXZlJztpZihwbClwbC5jbGFzc05hbWU9J3BsJzt9CiAgICBlbHNle3BzLmNsYXNzTmFtZT0ncHMnO2lmKHBsKXBsLmNsYXNzTmFtZT0ncGwnO30KICB9CiAgc3RlcD1uOyB3aW5kb3cuc2Nyb2xsVG8oMCwwKTsKfQoKLy8gQm9vayBidXR0b24KZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2Jvb2tCdG4nKS5vbmNsaWNrID0gZnVuY3Rpb24oKXsKICBzaG93U2NyZWVuKCdib29rU2NyZWVuJyk7IGdvU3RlcCgxKTsgYnVpbGRDYWwoKTsKfTsKCi8vIEJhY2sgYnV0dG9uCmRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdiYWNrQnRuJykub25jbGljayA9IGZ1bmN0aW9uKCl7CiAgaWYoc3RlcD4xKXtnb1N0ZXAoc3RlcC0xKTt9ZWxzZXtzaG93U2NyZWVuKCdob21lU2NyZWVuJyk7fQp9OwoKLy8gSG9tZSBhZ2Fpbgpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnaG9tZUJ0bicpLm9uY2xpY2sgPSBmdW5jdGlvbigpewogIHNob3dTY3JlZW4oJ2hvbWVTY3JlZW4nKTsgcmVzZXRBbGwoKTsKfTsKCi8vIEJyZWVkIHNlYXJjaAp2YXIgaW5wID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2JJbnB1dCcpOwp2YXIgZHJvcCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdiRHJvcCcpOwp2YXIgY2xyID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NsckJ0bicpOwp2YXIgYmFkZ2UgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnc0JhZGdlJyk7CgppbnAuYWRkRXZlbnRMaXN0ZW5lcignaW5wdXQnLCBmdW5jdGlvbigpewogIHZhciBxID0gaW5wLnZhbHVlLnRyaW0oKTsKICBjbHIuY2xhc3NMaXN0LnRvZ2dsZSgnc2hvdycsIHEubGVuZ3RoPjApOwogIGlmKCFxKXtkcm9wLmNsYXNzTGlzdC5yZW1vdmUoJ29wZW4nKTtkcm9wLmlubmVySFRNTD0nJztyZXR1cm47fQogIHZhciByZXMgPSBEQVRBLmZpbHRlcihmdW5jdGlvbihiKXtyZXR1cm4gYi5icmVlZC50b0xvd2VyQ2FzZSgpLmluZGV4T2YocS50b0xvd2VyQ2FzZSgpKSE9PS0xO30pLnNsaWNlKDAsMzUpOwogIGRyb3AuaW5uZXJIVE1MPScnOwogIGlmKCFyZXMubGVuZ3RoKXtkcm9wLmlubmVySFRNTD0nPGRpdiBjbGFzcz0ibm9yZXMiPtCf0L7RgNC+0LTQsCDQvdC1INC90LDQudC00LXQvdCwPC9kaXY+Jzt9CiAgZWxzZXsKICAgIHJlcy5mb3JFYWNoKGZ1bmN0aW9uKGIpewogICAgICB2YXIgZD1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdkaXYnKTsgZC5jbGFzc05hbWU9J2RpdGVtJzsKICAgICAgdmFyIGlkeD1iLmJyZWVkLnRvTG93ZXJDYXNlKCkuaW5kZXhPZihxLnRvTG93ZXJDYXNlKCkpOwogICAgICBkLmlubmVySFRNTD1iLmJyZWVkLnN1YnN0cmluZygwLGlkeCkrJzxtYXJrPicrYi5icmVlZC5zdWJzdHJpbmcoaWR4LGlkeCtxLmxlbmd0aCkrJzwvbWFyaz4nK2IuYnJlZWQuc3Vic3RyaW5nKGlkeCtxLmxlbmd0aCk7CiAgICAgIGQub25jbGljaz1mdW5jdGlvbigpe3NlbGVjdEJyZWVkKGIpO307CiAgICAgIGRyb3AuYXBwZW5kQ2hpbGQoZCk7CiAgICB9KTsKICB9CiAgZHJvcC5jbGFzc0xpc3QuYWRkKCdvcGVuJyk7Cn0pOwoKZG9jdW1lbnQuYWRkRXZlbnRMaXN0ZW5lcignY2xpY2snLGZ1bmN0aW9uKGUpewogIGlmKCFlLnRhcmdldC5jbG9zZXN0KCcuYndyYXAnKSlkcm9wLmNsYXNzTGlzdC5yZW1vdmUoJ29wZW4nKTsKfSk7CgpjbHIub25jbGljayA9IHJlc2V0QnJlZWQ7CgpmdW5jdGlvbiBzZWxlY3RCcmVlZChiKXsKICBzZWxCcmVlZD1iOyBib29raW5nLmJyZWVkPWIuYnJlZWQ7CiAgaW5wLnZhbHVlPScnOyBjbHIuY2xhc3NMaXN0LnJlbW92ZSgnc2hvdycpOwogIGRyb3AuY2xhc3NMaXN0LnJlbW92ZSgnb3BlbicpOyBkcm9wLmlubmVySFRNTD0nJzsKICBiYWRnZS5pbm5lckhUTUw9Jyc7CiAgdmFyIGJuPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NwYW4nKTtibi5jbGFzc05hbWU9J2JuYW1lJztibi50ZXh0Q29udGVudD1iLmJyZWVkOwogIHZhciBiYz1kb2N1bWVudC5jcmVhdGVFbGVtZW50KCdzcGFuJyk7YmMuY2xhc3NOYW1lPSdiY2hnJztiYy50ZXh0Q29udGVudD0n0JjQt9C80LXQvdC40YLRjCc7CiAgYmMub25jbGljaz1yZXNldEJyZWVkOwogIGJhZGdlLmFwcGVuZENoaWxkKGJuKTtiYWRnZS5hcHBlbmRDaGlsZChiYyk7CiAgYmFkZ2UuY2xhc3NMaXN0LmFkZCgnc2hvdycpOwogIHJlbmRlclN2Y3MoYik7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y1NlYycpLnN0eWxlLmRpc3BsYXk9J2Jsb2NrJzsKfQoKZnVuY3Rpb24gcmVzZXRCcmVlZCgpewogIHNlbEJyZWVkPW51bGw7Ym9va2luZy5icmVlZD0nJztib29raW5nLnNlcnZpY2U9Jyc7Ym9va2luZy5wcmljZT0wOwogIGlucC52YWx1ZT0nJztjbHIuY2xhc3NMaXN0LnJlbW92ZSgnc2hvdycpOwogIGJhZGdlLmNsYXNzTGlzdC5yZW1vdmUoJ3Nob3cnKTtiYWRnZS5pbm5lckhUTUw9Jyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N2Y1NlYycpLnN0eWxlLmRpc3BsYXk9J25vbmUnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdmNMaXN0JykuaW5uZXJIVE1MPScnOwp9CgpmdW5jdGlvbiByZW5kZXJTdmNzKGIpewogIHZhciBsaXN0PWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdmNMaXN0Jyk7bGlzdC5pbm5lckhUTUw9Jyc7CiAgT2JqZWN0LmVudHJpZXMoYi5zZXJ2aWNlcykuZm9yRWFjaChmdW5jdGlvbihrdil7CiAgICB2YXIgbmFtZT1rdlswXSxwcmljZT1rdlsxXTsKICAgIHZhciBidG49ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnYnV0dG9uJyk7YnRuLmNsYXNzTmFtZT0nc3ZidG4nOwogICAgdmFyIG5zPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NwYW4nKTtucy50ZXh0Q29udGVudD1uYW1lOwogICAgdmFyIHBzPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ3NwYW4nKTtwcy5jbGFzc05hbWU9J3N2cCc7cHMudGV4dENvbnRlbnQ9cHJpY2UrJyDigqwnOwogICAgYnRuLmFwcGVuZENoaWxkKG5zKTtidG4uYXBwZW5kQ2hpbGQocHMpOwogICAgYnRuLm9uY2xpY2s9ZnVuY3Rpb24oKXsKICAgICAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLnN2YnRuJykuZm9yRWFjaChmdW5jdGlvbihiKXtiLmNsYXNzTGlzdC5yZW1vdmUoJ2FjdGl2ZScpO30pOwogICAgICBidG4uY2xhc3NMaXN0LmFkZCgnYWN0aXZlJyk7CiAgICAgIGJvb2tpbmcuc2VydmljZT1uYW1lO2Jvb2tpbmcucHJpY2U9cHJpY2U7CiAgICAgIHNldFRpbWVvdXQoZnVuY3Rpb24oKXtnb1N0ZXAoMik7fSwzMDApOwogICAgfTsKICAgIGxpc3QuYXBwZW5kQ2hpbGQoYnRuKTsKICB9KTsKfQoKLy8gTWFzdGVycwpkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcubWJ0bicpLmZvckVhY2goZnVuY3Rpb24oYnRuKXsKICBidG4ub25jbGljaz1mdW5jdGlvbigpewogICAgZG9jdW1lbnQucXVlcnlTZWxlY3RvckFsbCgnLm1idG4nKS5mb3JFYWNoKGZ1bmN0aW9uKGIpe2IuY2xhc3NMaXN0LnJlbW92ZSgnYWN0aXZlJyk7fSk7CiAgICBidG4uY2xhc3NMaXN0LmFkZCgnYWN0aXZlJyk7CiAgICBib29raW5nLm1hc3Rlcj1idG4uZ2V0QXR0cmlidXRlKCdkYXRhLW1hc3RlcicpOwogICAgc2V0VGltZW91dChmdW5jdGlvbigpe2dvU3RlcCgzKTt9LDMwMCk7CiAgfTsKfSk7CgovLyBHcm9vbSBoaXN0b3J5CmRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5nYnRuJykuZm9yRWFjaChmdW5jdGlvbihidG4pewogIGJ0bi5vbmNsaWNrPWZ1bmN0aW9uKCl7CiAgICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuZ2J0bicpLmZvckVhY2goZnVuY3Rpb24oYil7Yi5jbGFzc0xpc3QucmVtb3ZlKCdhY3RpdmUnKTt9KTsKICAgIGJ0bi5jbGFzc0xpc3QuYWRkKCdhY3RpdmUnKTsKICAgIGJvb2tpbmcuZ3Jvb21IaXN0b3J5PWJ0bi5nZXRBdHRyaWJ1dGUoJ2RhdGEtdmFsJyk7CiAgICBzZXRUaW1lb3V0KGZ1bmN0aW9uKCl7Z29TdGVwKDQpO2J1aWxkQ2FsKCk7fSwzMDApOwogIH07Cn0pOwoKLy8gQ2FsZW5kYXIKZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3ByZXZNJykub25jbGljaz1mdW5jdGlvbigpe2NNLS07aWYoY008MCl7Y009MTE7Y1ktLTt9YnVpbGRDYWwoKTt9Owpkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnbmV4dE0nKS5vbmNsaWNrPWZ1bmN0aW9uKCl7Y00rKztpZihjTT4xMSl7Y009MDtjWSsrO31idWlsZENhbCgpO307CgpmdW5jdGlvbiBidWlsZENhbCgpewogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjYWxNJykudGV4dENvbnRlbnQ9TU9OVEhTW2NNXSsnICcrY1k7CiAgdmFyIGc9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NhbEcnKTtnLmlubmVySFRNTD0nJzsKICBbJ9Cf0L0nLCfQktGCJywn0KHRgCcsJ9Cn0YInLCfQn9GCJywn0KHQsScsJ9CS0YEnXS5mb3JFYWNoKGZ1bmN0aW9uKGQpewogICAgdmFyIGVsPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO2VsLmNsYXNzTmFtZT0nY2RuJztlbC50ZXh0Q29udGVudD1kO2cuYXBwZW5kQ2hpbGQoZWwpOwogIH0pOwogIHZhciBmaXJzdD1uZXcgRGF0ZShjWSxjTSwxKS5nZXREYXkoKTsKICB2YXIgZGF5cz1uZXcgRGF0ZShjWSxjTSsxLDApLmdldERhdGUoKTsKICB2YXIgc3RhcnQ9Zmlyc3Q9PT0wPzY6Zmlyc3QtMTsKICB2YXIgdG9kYXk9bmV3IERhdGUoKTsKICBmb3IodmFyIGk9MDtpPHN0YXJ0O2krKyl7dmFyIGVsPWRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpO2VsLmNsYXNzTmFtZT0nY2QnO2cuYXBwZW5kQ2hpbGQoZWwpO30KICBmb3IodmFyIGRheT0xO2RheTw9ZGF5cztkYXkrKyl7CiAgICB2YXIgZWw9ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnZGl2Jyk7ZWwuY2xhc3NOYW1lPSdjZCc7CiAgICB2YXIgZGF0ZT1uZXcgRGF0ZShjWSxjTSxkYXkpOwogICAgdmFyIGlzUGFzdD1kYXRlPG5ldyBEYXRlKHRvZGF5LmdldEZ1bGxZZWFyKCksdG9kYXkuZ2V0TW9udGgoKSx0b2RheS5nZXREYXRlKCkpOwogICAgZWwudGV4dENvbnRlbnQ9ZGF5OwogICAgaWYoaXNQYXN0KXtlbC5jbGFzc0xpc3QuYWRkKCdkaXMnKTt9CiAgICBlbHNlewogICAgICBpZihkYXRlLnRvRGF0ZVN0cmluZygpPT09dG9kYXkudG9EYXRlU3RyaW5nKCkpZWwuY2xhc3NMaXN0LmFkZCgndG9kJyk7CiAgICAgIChmdW5jdGlvbihkKXsKICAgICAgICBlbC5vbmNsaWNrPWZ1bmN0aW9uKCl7CiAgICAgICAgICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcuY2QnKS5mb3JFYWNoKGZ1bmN0aW9uKGMpe2MuY2xhc3NMaXN0LnJlbW92ZSgnc2VsJyk7fSk7CiAgICAgICAgICBlbC5jbGFzc0xpc3QuYWRkKCdzZWwnKTsKICAgICAgICAgIGJvb2tpbmcuZGF0ZT1TdHJpbmcoZCkucGFkU3RhcnQoMiwnMCcpKycuJytTdHJpbmcoY00rMSkucGFkU3RhcnQoMiwnMCcpKycuJytjWTsKICAgICAgICAgIHNob3dUaW1lcygpOwogICAgICAgIH07CiAgICAgIH0pKGRheSk7CiAgICB9CiAgICBnLmFwcGVuZENoaWxkKGVsKTsKICB9Cn0KCmZ1bmN0aW9uIHNob3dUaW1lcygpewogIHZhciB0Zz1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgndGltZUcnKTt0Zy5pbm5lckhUTUw9Jyc7CiAgVElNRVMuZm9yRWFjaChmdW5jdGlvbih0KXsKICAgIHZhciBidG49ZG9jdW1lbnQuY3JlYXRlRWxlbWVudCgnYnV0dG9uJyk7YnRuLmNsYXNzTmFtZT0ndGJ0bic7YnRuLnRleHRDb250ZW50PXQ7CiAgICBidG4ub25jbGljaz1mdW5jdGlvbigpewogICAgICBkb2N1bWVudC5xdWVyeVNlbGVjdG9yQWxsKCcudGJ0bicpLmZvckVhY2goZnVuY3Rpb24oYil7Yi5jbGFzc0xpc3QucmVtb3ZlKCdhY3RpdmUnKTt9KTsKICAgICAgYnRuLmNsYXNzTGlzdC5hZGQoJ2FjdGl2ZScpO2Jvb2tpbmcudGltZT10OwogICAgICBzZXRUaW1lb3V0KGZ1bmN0aW9uKCl7Z29TdGVwKDUpO2J1aWxkU3VtKCk7fSwzMDApOwogICAgfTsKICAgIHRnLmFwcGVuZENoaWxkKGJ0bik7CiAgfSk7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3RpbWVTZWMnKS5zdHlsZS5kaXNwbGF5PSdibG9jayc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3RpbWVTZWMnKS5zY3JvbGxJbnRvVmlldyh7YmVoYXZpb3I6J3Ntb290aCcsYmxvY2s6J25lYXJlc3QnfSk7Cn0KCmZ1bmN0aW9uIGJ1aWxkU3VtKCl7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N1bUJsb2NrJykuaW5uZXJIVE1MPQogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPtCf0L7RgNC+0LTQsDwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrYm9va2luZy5icmVlZCsnPC9zcGFuPjwvZGl2PicrCiAgICAnPGRpdiBjbGFzcz0ic3IiPjxzcGFuIGNsYXNzPSJzbCI+0KPRgdC70YPQs9CwPC9zcGFuPjxzcGFuIGNsYXNzPSJzdiI+Jytib29raW5nLnNlcnZpY2UrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPtCc0LDRgdGC0LXRgDwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrYm9va2luZy5tYXN0ZXIrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPtCf0L7RgdC70LXQtNC90LjQuSDQs9GA0YPQvDwvc3Bhbj48c3BhbiBjbGFzcz0ic3YiPicrYm9va2luZy5ncm9vbUhpc3RvcnkrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPtCU0LDRgtCwPC9zcGFuPjxzcGFuIGNsYXNzPSJzdiI+Jytib29raW5nLmRhdGUrJzwvc3Bhbj48L2Rpdj4nKwogICAgJzxkaXYgY2xhc3M9InNyIj48c3BhbiBjbGFzcz0ic2wiPtCS0YDQtdC80Y88L3NwYW4+PHNwYW4gY2xhc3M9InN2Ij4nK2Jvb2tpbmcudGltZSsnPC9zcGFuPjwvZGl2PicrCiAgICAnPGRpdiBjbGFzcz0ic3IiPjxzcGFuIGNsYXNzPSJzbCI+0KHRgtC+0LjQvNC+0YHRgtGMPC9zcGFuPjxzcGFuIGNsYXNzPSJzcCI+Jytib29raW5nLnByaWNlKycg4oKsPC9zcGFuPjwvZGl2Pic7Cn0KCi8vIENvbmZpcm0KZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NvbmZpcm1CdG4nKS5vbmNsaWNrID0gZnVuY3Rpb24oKXsKICB2YXIgbmFtZT1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY05hbWUnKS52YWx1ZTsKICB2YXIgcGhvbmU9ZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NQaG9uZScpLnZhbHVlOwogIGlmKCFuYW1lfHwhcGhvbmUpe2FsZXJ0KCfQktCy0LXQtNC40YLQtSDQuNC80Y8g0Lgg0YLQtdC70LXRhNC+0L0nKTtyZXR1cm47fQogIGJvb2tpbmcubmFtZT1uYW1lOyBib29raW5nLnBob25lPXBob25lOyBib29raW5nLnBldD1kb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY1BldCcpLnZhbHVlOwoKICB2YXIgYnRuPWRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdjb25maXJtQnRuJyk7CiAgYnRuLnRleHRDb250ZW50PSfQntGC0L/RgNCw0LLQu9GP0LXQvC4uLic7IGJ0bi5kaXNhYmxlZD10cnVlOwoKICAvLyBCdWlsZCBHRVQgcGFyYW1zCiAgdmFyIHBhcmFtcyA9IE9iamVjdC5rZXlzKGJvb2tpbmcpLm1hcChmdW5jdGlvbihrKXsKICAgIHJldHVybiBlbmNvZGVVUklDb21wb25lbnQoaykgKyAnPScgKyBlbmNvZGVVUklDb21wb25lbnQoYm9va2luZ1trXSk7CiAgfSkuam9pbignJicpOwoKICBmZXRjaChSQUlMV0FZICsgJz8nICsgcGFyYW1zLCB7bWV0aG9kOidHRVQnfSkKICAgIC50aGVuKGZ1bmN0aW9uKCl7c2hvd1N1Y2Nlc3MoKTt9KQogICAgLmNhdGNoKGZ1bmN0aW9uKCl7c2hvd1N1Y2Nlc3MoKTt9KTsKfTsKCmZ1bmN0aW9uIHNob3dTdWNjZXNzKCl7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2JrNScpLmNsYXNzTmFtZT0nc3RlcCc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3N1Y0Jsb2NrJykuY2xhc3NMaXN0LmFkZCgnc2hvdycpOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdwcm9ncmVzcycpLnN0eWxlLmRpc3BsYXk9J25vbmUnOwp9CgpmdW5jdGlvbiByZXNldEFsbCgpewogIGJvb2tpbmc9e2JyZWVkOicnLHNlcnZpY2U6JycscHJpY2U6MCxtYXN0ZXI6JycsZ3Jvb21IaXN0b3J5OicnLGRhdGU6JycsdGltZTonJ307CiAgc2VsQnJlZWQ9bnVsbDsgaW5wLnZhbHVlPScnOyBjbHIuY2xhc3NMaXN0LnJlbW92ZSgnc2hvdycpOwogIGJhZGdlLmNsYXNzTGlzdC5yZW1vdmUoJ3Nob3cnKTsgYmFkZ2UuaW5uZXJIVE1MPScnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdmNTZWMnKS5zdHlsZS5kaXNwbGF5PSdub25lJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgndGltZVNlYycpLnN0eWxlLmRpc3BsYXk9J25vbmUnOwogIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdzdWNCbG9jaycpLmNsYXNzTGlzdC5yZW1vdmUoJ3Nob3cnKTsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgncHJvZ3Jlc3MnKS5zdHlsZS5kaXNwbGF5PSdmbGV4JzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY05hbWUnKS52YWx1ZT0nJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY1Bob25lJykudmFsdWU9Jyc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NQZXQnKS52YWx1ZT0nJzsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY29uZmlybUJ0bicpLnRleHRDb250ZW50PSfQn9C+0LTRgtCy0LXRgNC00LjRgtGMINC30LDQv9C40YHRjCc7CiAgZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2NvbmZpcm1CdG4nKS5kaXNhYmxlZD1mYWxzZTsKICBnb1N0ZXAoMSk7Cn0KPC9zY3JpcHQ+CjwvYm9keT4KPC9odG1sPg=="

@app.route("/app")
def booking_app():
    import base64
    html = base64.b64decode(BOOKING_HTML_B64).decode("utf-8")
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
