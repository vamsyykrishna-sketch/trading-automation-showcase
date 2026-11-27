"""
order_journal.py - append-only, per-day record of every order the bot sends.

Why it exists
-------------
A broker returns an order number when an order is placed, but if the bot
restarts, that number is gone from memory. Without a saved record, the bot
cannot tell its own orders apart from manual trades placed on the same
symbol, and reconciliation after a crash becomes guesswork.

Design
------
* One JSON Lines file per strategy tag per day:
      order_journal/<YYYY-MM-DD>_<TAG>.jsonl
* Each line is written the moment the broker's placement response is read
  (before waiting for the fill), then flushed and fsynced, so a crash right
  after placement still leaves a record on disk.
* Every line has an explicit outcome:
      placed           order number known
      rejected         broker refused it: no order exists
      no_order_number  broker said OK but returned no number: an order MAY exist
      send_raised      the request raised: it MAY have reached the broker
      paper            paper-trading order (fake number)
  The two "may exist" outcomes are kept separate on purpose: reconciliation
  must treat them as "attribution may be incomplete", never as "nothing sent".
* Never raises on write: a journal problem is logged and the order path
  carries on. Standard library only, so independent watchdog processes can
  import it without pulling in the trading engine.
"""
import json
import os
import threading
from datetime import datetime

JOURNAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "order_journal")
_lock = threading.Lock()


def journal_path(tag, day=None):
    day = day or datetime.now()
    return os.path.join(JOURNAL_DIR, f"{day.strftime('%Y-%m-%d')}_{tag}.jsonl")


def record(tag, *, symbol, side, qty, outcome, segment, price=None,
           order_no=None, paper=False, detail=None):
    """Append one order line for `tag`. Never raises."""
    try:
        now = datetime.now()
        line = {
            "ts": now.isoformat(timespec="seconds"), "tag": tag, "symbol": symbol,
            "side": side, "qty": qty, "price": price, "segment": segment,
            "order_no": None if order_no is None else str(order_no),
            "outcome": outcome, "paper": bool(paper),
            "detail": None if detail is None else str(detail)[:300],
        }
        data = json.dumps(line, separators=(",", ":")) + "\n"
        with _lock:
            os.makedirs(JOURNAL_DIR, exist_ok=True)
            with open(journal_path(tag, now), "a") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())   # survive a crash / power loss
    except Exception as e:
        print(f"order_journal: could not record {outcome} {side} {symbol} x{qty} "
              f"#{order_no} for {tag}: {type(e).__name__}: {e}")


def read(tag, day=None):
    """All lines for `tag` on `day` (default today), oldest first.

    Skips a line torn by a crash mid-write. A missing file means no orders
    ([]). Any other read error RAISES, so a caller can never mistake
    "couldn't read the journal" for "the bot sent nothing"."""
    rows = []
    try:
        with open(journal_path(tag, day)) as f:
            for raw in f:
                try:
                    rows.append(json.loads(raw))
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return rows


def journal_order_numbers(tag, day=None):
    """(placed, unknown): order numbers recorded as placed, plus the records
    that may exist at the broker with no number known. A non-empty `unknown`
    means attribution may be incomplete."""
    rows = read(tag, day)
    placed = {r["order_no"] for r in rows if r.get("outcome") == "placed" and r.get("order_no")}
    unknown = [r for r in rows if r.get("outcome") in ("send_raised", "no_order_number")]
    return placed, unknown


_SIDE = {"b": 1, "buy": 1, "s": -1, "sell": -1}


def _signed_fill(row):
    """Signed FILLED quantity of an order-book row (+buy / -sell), or None if
    unreadable. Uses filled qty, never order qty: an unfilled or partly filled
    order moves the position only by what actually filled."""
    side = _SIDE.get(str(row.get("side", "")).strip().lower())
    try:
        filled = int(float(row.get("filled_qty")))
    except (TypeError, ValueError):
        return None
    return None if side is None or filled < 0 else side * filled


def bot_attributed_net(rows, my_order_nos, wanted):
    """Net position built ONLY from the bot's own orders.

    rows:         broker order-book rows, normalised by the broker adapter to
                  {order_no, symbol, side, filled_qty, source}
    my_order_nos: the bot's order numbers (from journal_order_numbers)
    wanted(row):  scope filter (e.g. one segment / one underlying)

    The same order number appearing twice is counted once, at its largest
    filled qty. Returns None if any of the bot's own rows is unreadable:
    attribution unavailable, never a guess. Otherwise:
      {"net":     {symbol: signed qty from the bot's own fills},
       "other":   {symbol: [(order_no, signed qty or None, source), ...]},
       "missing": sorted bot order numbers not in the order book at all}
    """
    def filled(row):
        q = _signed_fill(row)
        return -1 if q is None else abs(q)

    mine = {str(n) for n in my_order_nos}
    seen, best = set(), {}
    for row in rows:
        no = str(row.get("order_no", ""))
        seen.add(no)
        if wanted(row) and (no not in best or filled(row) > filled(best[no])):
            best[no] = row
    net, other = {}, {}
    for no, row in best.items():
        sym, q = row.get("symbol"), _signed_fill(row)
        if no in mine:
            if q is None:
                return None
            net[sym] = net.get(sym, 0) + q
        else:
            other.setdefault(sym, []).append((no, q, row.get("source")))
    return {"net": net, "other": other, "missing": sorted(mine - seen)}
