from flask import Flask, request
import anthropic
import os
import requests

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID")
VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN")

SYSTEM_PROMPT = (
    "Ty Jarvis, AI-administrator salona gruminga RJ Grooming v Talline. "
    "Obshaeshsya teplo i druzhelyubno. Korotkie frazy. "
    "Govori na yazyke klienta - russkiy, estonskiy ili angliyskiy. "
    "Ne govori chto ty AI esli ne sprosyat. "
    "Privetstvie: Privet! Eto RJ Grooming, chem mogu pomoch? "
    "Umeesh: zapisat pitomca, nazvat ceny, rasskazat o salone."
)


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
    try:
        messages = data["entry"][0]["changes"][0]["value"].get("messages", [])
        if not messages:
            return "ok", 200
        msg = messages[0]
        from_number = msg["from"]
        text = msg.get("text", {}).get("body", "")
        if not text:
            return "ok", 200
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}]
        )
        reply = response.content[0].text
        send_whatsapp(from_number, reply)
    except Exception as e:
        print("Error: " + str(e))
    return "ok", 200


@app.route("/")
def home():
    return "MyDeal Jarvis rabotaet!"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
