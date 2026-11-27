"""
kill_switch.py - standalone emergency kill switch for live option positions.

Runs as its OWN process (its own systemd service), completely independent of
the trading bots:
  * its own broker login, zero shared state or imports with the bot engine
  * reads ONLY the broker's real position data every cycle, never the bot's
    own state file (a bug in the bot cannot blind the kill switch)
  * its only write actions: the KILL_FILE and one closing order per leg

Three ways to stop trading
--------------------------
1. Manual    - operator sends a stop command (e.g. via Telegram) that writes
               the KILL_FILE
2. File      - any process (or a human over SSM) creates KILL_FILE; every bot
               checks it before each new entry
3. Automatic - this process: unrealized loss across all in-scope positions
               crosses LOSS_THRESHOLD -> fire

Fail-safe rules (each one learned from a real production edge case)
--------------------------------------------------------------------
* P&L that can't be verified is NEVER assumed to be 0. A missing broker
  field or a nonsensical value (avg price <= 0) marks the whole cycle
  UNVERIFIED and alerts, instead of silently treating the account as flat.
* Scope by an exact underlying field, never a symbol prefix: "NIFTY" as a
  prefix also matches other index symbols that start with the same letters.
* KILL_FILE is written BEFORE any order, so bots stop opening new entries
  even if closing fails. An in-memory flag survives a failed file write.
* ONE close order per leg, priced through the book (buy at best ask + tick,
  sell at best bid - tick), quantity taken from the broker's own position,
  never an assumed lot count. No reprice loop: a kill switch that keeps
  firing orders is itself a risk.
* After closing, verify with read-only position reads. Anything still open
  -> loud "MANUAL ACTION REQUIRED" alert listing the exact legs.
* Every order goes through the order journal, so it can be reconciled later.
"""
import os
import time
from datetime import datetime

from src.alerts.telegram import send_telegram
from src.execution import order_journal

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KILL_FILE = os.path.join(BASE_DIR, "KILL.txt")
TAG = "KILL_SWITCH"

CHECK_INTERVAL = 30          # seconds between checks
LOSS_THRESHOLD = float(os.environ.get("KILL_LOSS_THRESHOLD", "50000"))  # set per account
CLOSE_OFFSET = 1             # price through the book, one-shot
CLOSE_VERIFY_READS = 6
CLOSE_VERIFY_INTERVAL = 5    # seconds
UNDERLYING = os.environ.get("KILL_UNDERLYING", "NIFTY")

_kill_fired = False          # in memory: survives a failed KILL_FILE write


class UnverifiedPnL(Exception):
    """Raised when a position's P&L can't be trusted this cycle."""


def in_scope(position):
    """Exact-match scope: underlying field, derivatives segment only."""
    return position.get("underlying") == UNDERLYING and position.get("segment") == "fo"


def position_pnl(position, ltp):
    """Unrealized P&L of one position from the BROKER's average prices.
    Shorts: (avg_sell - ltp) * qty. Longs: (ltp - avg_buy) * qty."""
    qty = position.get("net_qty")
    if qty is None:
        raise UnverifiedPnL(f"{position.get('symbol')}: net_qty missing")
    if qty < 0:
        avg = position.get("avg_sell_price")
        if avg is None or avg <= 0:
            raise UnverifiedPnL(f"{position.get('symbol')}: bad avg_sell_price {avg}")
        return (avg - ltp) * abs(qty)
    avg = position.get("avg_buy_price")
    if avg is None or avg <= 0:
        raise UnverifiedPnL(f"{position.get('symbol')}: bad avg_buy_price {avg}")
    return (ltp - avg) * qty


def write_kill_file(total_pnl):
    """Write KILL_FILE (atomic). Returns True on success."""
    try:
        tmp = f"{KILL_FILE}.tmp"
        with open(tmp, "w") as f:
            f.write(f"Kill switch activated\nWriter : {TAG}\n")
            f.write(f"Reason : unrealized loss {abs(total_pnl):,.2f} >= {LOSS_THRESHOLD:,.0f}\n")
            f.write(f"Time   : {datetime.now().isoformat(timespec='seconds')}\n")
        os.replace(tmp, KILL_FILE)
        return True
    except Exception as e:
        print(f"KILL_FILE write FAILED: {type(e).__name__}: {e}")
        return False


def close_position(broker, position):
    """ONE aggressive limit order to flatten one leg. Returns order no or None."""
    qty = abs(position["net_qty"])
    bid, ask = broker.best_bid_ask(position["token"])
    if bid is None or ask is None:
        send_telegram(f"🚨 {TAG}: no live depth for {position['symbol']} - MANUAL ACTION")
        return None
    if position["net_qty"] < 0:
        side, price = "BUY", ask + CLOSE_OFFSET
    else:
        side, price = "SELL", max(bid - CLOSE_OFFSET, 0.05)
    try:
        order_no = broker.place_limit_order(position["symbol"], side, qty, price)
        outcome = "placed" if order_no else "no_order_number"
    except Exception as e:
        order_no, outcome = None, "send_raised"
        print(f"close {position['symbol']} raised: {e}")
    order_journal.record(TAG, symbol=position["symbol"], side=side, qty=qty,
                         price=price, outcome=outcome, segment="fo", order_no=order_no)
    return order_no


def verify_closed(broker, symbols):
    """Read-only: poll broker positions until every symbol is flat. Returns
    the set still open (empty = success)."""
    remaining = set(symbols)
    for _ in range(CLOSE_VERIFY_READS):
        time.sleep(CLOSE_VERIFY_INTERVAL)
        open_now = {p["symbol"] for p in broker.positions() if in_scope(p) and p["net_qty"] != 0}
        remaining &= open_now
        if not remaining:
            break
    return remaining


def trigger_kill(broker, positions, total_pnl):
    global _kill_fired
    _kill_fired = True                               # 1. memory first
    file_ok = write_kill_file(total_pnl)             # 2. stop new entries
    send_telegram(f"🚨💀 {TAG} TRIGGERED - loss {abs(total_pnl):,.0f}. "
                  + ("KILL_FILE written." if file_ok else "KILL_FILE write FAILED - bots won't stop on their own!"))
    for p in positions:                              # 3. one order per leg
        close_position(broker, p)
    still_open = verify_closed(broker, [p["symbol"] for p in positions])  # 4. verify
    if still_open:
        send_telegram(f"🚨🚨 {TAG}: MANUAL CLOSING REQUIRED: {', '.join(sorted(still_open))}")
    else:
        send_telegram(f"✅ {TAG}: all {len(positions)} leg(s) confirmed closed at broker")


def run(broker):
    """Main loop. `broker` is an adapter exposing positions(), ltp(token),
    best_bid_ask(token), place_limit_order(symbol, side, qty, price)."""
    while True:
        try:
            if not _kill_fired:
                positions = [p for p in broker.positions() if in_scope(p) and p["net_qty"] != 0]
                total = sum(position_pnl(p, broker.ltp(p["token"])) for p in positions)
                if positions and total <= -LOSS_THRESHOLD:
                    trigger_kill(broker, positions, total)
        except UnverifiedPnL as e:
            send_telegram(f"⚠️ {TAG}: cycle UNVERIFIED ({e}) - not assumed flat")
        except Exception as e:
            send_telegram(f"⚠️ {TAG}: check failed: {type(e).__name__}: {e}")
        time.sleep(CHECK_INTERVAL)
