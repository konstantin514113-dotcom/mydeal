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


@app.route("/app")
def booking_app():
    return """<!DOCTYPE html>
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
  <a href="tel:+37258735456" class="opt">
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
