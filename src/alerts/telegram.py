"""
telegram.py - one-way Telegram alerts for trades, errors and daily summaries.

Design choices
--------------
* Standard library only (urllib, not requests). Alerts fire when something
  is already broken; the alerter must not share a dependency or virtualenv
  failure mode with the bots it is reporting on.
* Credentials come from environment variables (loaded from a .env file the
  repo never contains): TELEGRAM_TOKEN, TELEGRAM_CHAT_ID.
* Never print the raw exception: the request URL contains the bot token, so
  `print(e)` can leak it into logs. Only the exception TYPE is logged.
* Never raises. A failed alert must not crash the trading or kill-switch
  loop that called it.
* `send_burst()` repeats a critical alert N times so it can't be missed on a
  phone (used for "MANUAL ACTION REQUIRED" situations).
"""
import os
import sys
import time
import urllib.parse
import urllib.request

TIMEOUT_SEC = 10


def _creds():
    return os.environ.get("TELEGRAM_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")


def send_telegram(message):
    """Send one message. Returns True if Telegram accepted it. Never raises."""
    token, chat_id = _creds()
    if not token or not chat_id:
        print("telegram: TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set", file=sys.stderr)
        return False
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": message[:4000]}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"telegram: send failed ({type(e).__name__})", file=sys.stderr)  # never {e}: URL has the token
        return False


def send_burst(message, count=5, gap_sec=2):
    """Repeat a critical alert so it is impossible to miss."""
    for i in range(count):
        send_telegram(f"[{i + 1}/{count}] {message}")
        time.sleep(gap_sec)


if __name__ == "__main__":
    # Used by systemd's OnFailure= hook:  python -m src.alerts.telegram <unit>
    unit = sys.argv[1] if len(sys.argv) > 1 else "unknown unit"
    send_telegram(f"🚨 systemd: {unit} has FAILED (check: systemctl status {unit})")
