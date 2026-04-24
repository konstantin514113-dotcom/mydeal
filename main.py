from flask import Flask, request
import anthropic
import os
import requests

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID")
VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN")

SYSTEM_PROMPT = "Ty Jarvis, AI-administrator salona gruminga RJ Grooming v Talline. Obshaeshsya teplo i druzhelyubno. Korotkie frazy. Govori na yazyke klienta - russkiy, estonskiy ili angliyskiy. Ne govori chto ty AI esli ne sprosyat. Privetstvie: Privet! Eto RJ Grooming, chem mogu pomoch? Umeesh: zapisat pitomca, nazvat ceny, rasskazat o salone, perelogit zapis."


def send_whatsapp(to, text):
    url = "https://graph.facebook.com/v18.0/" + WHATSAPP_PHONE_ID + "/m
