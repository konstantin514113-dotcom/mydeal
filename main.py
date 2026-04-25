from flask import Flask, request
import anthropic
import os
import requests

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID")
VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN")

conversation_history = {}
MAX_HISTORY = 20

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
- Services: grooming for dogs and cats — haircut, bathing, nail trimming, ear cleaning, full grooming packages"""


def send_whatsapp(to, text):
    url = "https://graph.facebook.com/v18.0/" + WHATSAPP_PHONE_ID + "/messages"
    headers = {
        "Authorization": "Bearer " + WHATSAPP_TOKEN,
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
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print("Incoming: " + str(data))
    try:
        messages = data["entry"][0]["changes"][0]["value"].get("messages", [])
        if not messages:
            print("No messages")
            return "ok", 200
        msg = messages[0]
        from_number = msg["from"]
        text = msg.get("text", {}).get("body", "")
        print("From: " + from_number + " Text: " + text)
        if not text:
            return "ok", 200

        if from_number not in conversation_history:
            conversation_history[from_number] = []

        history = conversation_history[from_number]
        history.append({"role": "user", "content": text})

        if len(history) > MAX_HISTORY:
            history = history[-MAX_HISTORY:]
            conversation_history[from_number] = history

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=history
        )
        reply = response.content[0].text
        history.append({"role": "assistant", "content": reply})

        send_whatsapp(from_number, reply)
        print("Sent: " + reply)
    except Exception as e:
        print("Error: " + str(e))
    return "ok", 200


@app.route("/")
def home():
    return "MyDeal Jarvis rabotaet!"


@app.route("/privacy")
def privacy():
    return "Privacy Policy: We do not store or share your personal data."


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
