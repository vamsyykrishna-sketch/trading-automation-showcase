"""
position_watchdog.py - standalone position-imbalance watchdog with a
two-way Telegram "Acknowledge" button.

Why
---
A short straddle/strangle always sells equal CE and PE quantity. If the two
totals ever differ, a naked leg exists somewhere (e.g. one leg filled, the
other failed, and a retry re-sold the filled leg). Checking the aggregate is
simpler and exact: no need to match legs entry by entry.

Isolation
---------
Separate process and systemd service. A crash or bug in any trading bot can't
affect it, and a bug here can't affect them. Read-only towards the broker;
its only outbound actions are Telegram messages.

Two checks every 30 s
---------------------
1. STATE-FILE check (paper bots): sums open short CE vs PE quantity from each
   bot's own state file, INCLUDING pending (half-filled) entries. Checking
   only completed entries is blind to exactly the failure mode that matters:
   a naked leg from an entry attempt that never completed.
2. BROKER check (live): sums short CE vs PE quantity from the broker's real
   positions. Aggregate only - broker positions carry no bot id.

Escalation + acknowledgement
----------------------------
Imbalance -> alert with an inline "Acknowledge" button -> re-alert every
cycle until a human taps it. Acks are tracked per occurrence: a new imbalance
after a resolved one always needs a fresh ack. The listener uses Telegram
long-polling (getUpdates with an offset), so no inbound port or webhook has
to be opened on the server.
"""
import json
import os
import time
import urllib.parse
import urllib.request

CHECK_INTERVAL = 30
POLL_TIMEOUT = 5

_acked = set()          # instance ids a human acknowledged
_active = {}            # label -> current imbalance instance id
_update_offset = None


# ---- Telegram (two-way) ----------------------------------------------------

def _api(method, params):
    token = os.environ.get("TELEGRAM_TOKEN", "")
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=POLL_TIMEOUT + 10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"telegram {method} failed ({type(e).__name__})")   # never {e}: URL has token
        return None


def send_alert_with_ack(instance_id, text):
    button = {"inline_keyboard": [[{"text": "✅ Acknowledge", "callback_data": instance_id}]]}
    _api("sendMessage", {"chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
                         "text": text, "reply_markup": json.dumps(button)})


def poll_acks():
    """Long-poll Telegram for button taps; record acks. Only taps from the
    configured chat are accepted."""
    global _update_offset
    params = {"timeout": POLL_TIMEOUT, "allowed_updates": json.dumps(["callback_query"])}
    if _update_offset is not None:
        params["offset"] = _update_offset
    resp = _api("getUpdates", params)
    for upd in (resp or {}).get("result", []):
        _update_offset = upd["update_id"] + 1
        cb = upd.get("callback_query")
        if not cb:
            continue
        if str(cb.get("message", {}).get("chat", {}).get("id")) != os.environ.get("TELEGRAM_CHAT_ID"):
            continue
        _acked.add(cb.get("data"))
        _api("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "Acknowledged"})


# ---- imbalance handling ------------------------------------------------------

def handle(label, ce_qty, pe_qty):
    if ce_qty == pe_qty:
        _active.pop(label, None)                      # resolved: next one needs a new ack
        return
    instance = _active.setdefault(label, f"{label}-{int(time.time())}")
    if instance in _acked:
        return
    send_alert_with_ack(instance, f"🚨 {label}: position IMBALANCE - short CE {ce_qty} vs "
                                  f"short PE {pe_qty}. Naked leg likely. Re-alerting until acknowledged.")


def short_qty_from_state(state_file):
    """(ce, pe) open short qty from a bot's state file: completed entries with
    the leg not yet closed + pending entries with the leg already filled."""
    with open(state_file) as f:
        s = json.load(f)
    ce = pe = 0
    for e in s.get("active_entries", []):
        ce += 0 if e.get("ce_closed") else e.get("qty", 0)
        pe += 0 if e.get("pe_closed") else e.get("qty", 0)
    for e in s.get("pending_entries", []):
        ce += e.get("qty", 0) if e.get("ce_filled") else 0
        pe += e.get("qty", 0) if e.get("pe_filled") else 0
    return ce, pe


def short_qty_from_broker(broker):
    ce = pe = 0
    for p in broker.positions():
        if p.get("net_qty", 0) < 0:
            if p.get("option_type") == "CE":
                ce += -p["net_qty"]
            elif p.get("option_type") == "PE":
                pe += -p["net_qty"]
    return ce, pe


def run(state_files, broker=None):
    """state_files: {label: path}. broker: optional adapter for the live check."""
    while True:
        poll_acks()
        for label, path in state_files.items():
            try:
                handle(label, *short_qty_from_state(path))
            except FileNotFoundError:
                pass                                   # bot not running today
            except Exception as e:
                print(f"{label}: state check failed: {type(e).__name__}")
        if broker is not None:
            try:
                handle("LIVE", *short_qty_from_broker(broker))
            except Exception as e:
                print(f"LIVE: broker check failed: {type(e).__name__}")
        time.sleep(CHECK_INTERVAL)
