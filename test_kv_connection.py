import os
import requests
import json

ACCOUNT_ID = os.environ["CF_ACCOUNT_ID"]
NAMESPACE_ID = os.environ["CF_KV_NAMESPACE_ID"]
API_TOKEN = os.environ["CF_API_TOKEN"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

KV_URL = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/storage/kv/namespaces/{NAMESPACE_ID}"
HEADERS = {"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/json"}


def kv_put(key, value):
    r = requests.put(f"{KV_URL}/values/{key}", headers=HEADERS, data=value)
    r.raise_for_status()
    print(f"✅ Wrote key '{key}'")


def kv_get(key):
    r = requests.get(f"{KV_URL}/values/{key}", headers=HEADERS)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.text


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    r.raise_for_status()


if __name__ == "__main__":
    # Write a test value
    kv_put("python_test_key", "Hello from GitHub Actions!")

    # Read it back to confirm
    value = kv_get("python_test_key")
    print(f"Read back: {value}")

    # Message you on Telegram so you see it end-to-end
    send_telegram(f"🐍 Python→Cloudflare test: {value}")
