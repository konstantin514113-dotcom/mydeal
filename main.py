from flask import Flask, request
import anthropic
import os

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    message = data.get("message", "")
    
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="""Ты AI-администратор груминг-салона MyDeal. 
        Ты отвечаешь клиентам на том языке на котором они пишут.
        Ты помогаешь записаться на стрижку, отвечаешь на вопросы об услугах и ценах.
        Ты вежливый и профессиональный помощник.""",
        messages=[{"role": "user", "content": message}]
    )
    
    return {"reply": response.content[0].text}

@app.route("/")
def home():
    return "MyDeal Jarvis работает!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
