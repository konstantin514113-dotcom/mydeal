
from flask import Flask, request
import anthropic
import os
import requests
import json

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID")
VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN")

SYSTEM_PROMPT = """Ты — Jarvis, тёплый и дружелюбный AI-администратор салона груминга R&J Grooming в Таллине.

Общаешься тепло, как хороший знакомый — не робот.
Короткие фразы, никакой воды.
Всегда позитивный.
Говоришь на языке клиента — русский, эстонский или английский.
Никогда не говоришь что ты AI, если не спросят напрямую.

Приветствие: "Привет! Это R&J Grooming, чем могу помочь?"

Умеешь:
- Записать питомца на груминг
- Назвать цены
- Рассказать о салоне
- Перенести или отменить запись

Тон: тепло, коротко, по делу."""

def send_whatsapp_message(to, text):
    url = f"https://graph.facebook.com/v18.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    requests.post(url, headers=headers, json=data)

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    try:
        entry = data["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]
        messages = value.get("messages", [])
        if not messages:
            return "ok", 200
        message = messages[0]
        from_number = message["from"]
        text = message.get("text", {}).get("body", "")
        if not text:
            return "ok", 200
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}]
        )
        reply = response.content[0].text
        send_whatsapp_message(from_number, reply)
    except Exception as e:
        print(f"Error: {e}")
    return "ok", 200

@app.route("/")
def home():
    return "MyDeal Jarvis работает!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)



Отправлено из мобильной Почты Mail.ru

