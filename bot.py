import os
import logging
import requests
from flask import Flask, request

# Logging
logging.basicConfig(level=logging.INFO)

# Bot config
TOKEN = os.getenv("8323791507:AAHffJ1lQal40YGf0SaNcjxSLp4ZkrHFniw")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # Optional if you want to lock to one user
TELEGRAM_URL = f"https://api.telegram.org/bot{TOKEN}"

# Flask server (needed for Railway/Heroku)
app = Flask(__name__)

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json()
    logging.info(f"Update: {data}")

    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text", "")

        if text.startswith("/start"):
            send_message(chat_id, "👋 Welcome! Send me your expenses like: 50 food")
        else:
            send_message(chat_id, f"✅ Recorded: {text}")

    return {"ok": True}

def send_message(chat_id, text):
    url = f"{TELEGRAM_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    r = requests.post(url, json=payload)
    return r.json()

@app.route("/", methods=["GET"])
def home():
    return "Bot is running!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
