import os
import requests
import time
import logging

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise Exception("❌ TELEGRAM_TOKEN not set in environment variables!")

URL = f"https://api.telegram.org/bot{TOKEN}"

def get_updates(offset=None):
    try:
        params = {"timeout": 100, "offset": offset}
        resp = requests.get(URL + "/getUpdates", params=params, timeout=120)
        return resp.json()
    except Exception as e:
        logging.error(f"Error fetching updates: {e}")
        return {}

def send_message(chat_id, text):
    try:
        requests.post(URL + "/sendMessage", data={"chat_id": chat_id, "text": text})
    except Exception as e:
        logging.error(f"Error sending message: {e}")

def main():
    logging.info("🤖 Bot started with polling...")
    offset = None
    while True:
        updates = get_updates(offset)
        if "result" in updates:
            for update in updates["result"]:
                if "message" in update:
                    chat_id = update["message"]["chat"]["id"]
                    text = update["message"].get("text", "")
                    logging.info(f"📩 Message from {chat_id}: {text}")
                    send_message(chat_id, f"You said: {text}")
                    offset = update["update_id"] + 1
        time.sleep(1)

if __name__ == "__main__":
    main()

