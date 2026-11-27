"""
price_feed.py - stale-price protection for live quotes.

The production bug this prevents
--------------------------------
A common pattern in trading bots:

    ltp = live_prices.get(token) or fetch_ltp_via_rest(token)

`live_prices` is filled by a WebSocket tick feed. If the socket goes silent
(disconnects, broker session closed, host down), the dict still holds the
LAST value it received, so the `or` short-circuits and the REST fallback
never runs. Every decision for that symbol (entry, exit, stop-loss, hedge
strike) then silently uses a frozen price. In production this showed up as
repeated limit orders at the identical price for ~2 minutes while the real
market had moved away, so orders never filled and retries piled up.

The fix
-------
1. Store every tick as (value, receive_time), not a bare value.
2. Trust a cached tick only inside a short freshness window.
3. Otherwise force a fresh REST quote, even if a cached value exists.
4. Treat the feed itself as down if it has had no message on ANY symbol for
   a while (a socket can stay "connected" yet deliver nothing).
5. Every price read in the bot goes through `get_price()`: one audited path,
   not a pattern copied to every call site.

The feed process can also run as a separate "sidecar" that writes ticks to a
JSON file; `read_ticks_file()` applies the same freshness rules to that file
(heartbeat age + feed-silence check), written atomically so a reader never
sees half a file.
"""
import json
import os
import threading
import time

FRESH_TICK_SEC = 2.5      # a tick older than this is not trusted for trading
FEED_SILENT_SEC = 15.0    # no message on any symbol this long -> feed is down
TICKS_MAX_AGE_SEC = 3.0   # sidecar heartbeat: older file -> sidecar stalled
FORMAT_VERSION = 1


class PriceCache:
    """Thread-safe {token: (price, recv_time)} filled by the WebSocket thread."""

    def __init__(self, rest_fetch, fresh_sec=FRESH_TICK_SEC, clock=time.time):
        self._rest_fetch = rest_fetch   # callable(token) -> float, broker REST quote
        self._fresh_sec = fresh_sec
        self._clock = clock
        self._ticks = {}
        self._last_msg = None
        self._lock = threading.Lock()

    def on_tick(self, token, price):
        """Called from the WebSocket callback for every tick."""
        now = self._clock()
        with self._lock:
            self._ticks[str(token)] = (float(price), now)
            self._last_msg = now

    def feed_silent_for(self):
        """Seconds since the last message on any token (inf if none yet)."""
        with self._lock:
            return float("inf") if self._last_msg is None else self._clock() - self._last_msg

    def get_price(self, token):
        """(price, source). Uses the cached tick only if fresh; otherwise a
        REST quote. Raises if neither is available - the caller must not
        trade on a price it could not get."""
        now = self._clock()
        with self._lock:
            cached = self._ticks.get(str(token))
        if cached is not None and now - cached[1] <= self._fresh_sec:
            return cached[0], "ws"
        price = self._rest_fetch(token)          # stale or missing -> fresh fetch
        if price is None or price <= 0:
            raise RuntimeError(f"no usable price for {token} (tick stale, REST empty)")
        return float(price), "rest"


# ---- sidecar file protocol ------------------------------------------------

def atomic_write_json(path, obj):
    """Write via temp file + os.replace: readers see old or new, never half."""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def read_ticks_file(path, now=None):
    """({token: tick}, None) from a fresh sidecar ticks file, else ({}, reason).

    The file carries "written" (sidecar heartbeat) and "last_msg" (last feed
    message on any token). Either too old -> the whole feed is treated as
    down and callers fall back to REST."""
    try:
        with open(path) as f:
            d = json.load(f)
    except FileNotFoundError:
        return {}, "no ticks file (sidecar not running)"
    except Exception as e:
        return {}, f"ticks file unreadable ({type(e).__name__})"
    if not isinstance(d, dict) or d.get("v") != FORMAT_VERSION or not isinstance(d.get("ticks"), dict):
        return {}, "ticks file in an unknown format"
    try:
        written = float(d["written"])
    except (KeyError, TypeError, ValueError):
        return {}, "ticks file has no write time"
    age = (time.time() if now is None else now) - written
    if age > TICKS_MAX_AGE_SEC:
        return {}, f"ticks file {age:.0f}s old (sidecar stalled)"
    try:
        silent = written - float(d["last_msg"])
    except (KeyError, TypeError, ValueError):
        return {}, "sidecar has received no feed message yet"
    if silent > FEED_SILENT_SEC:
        return {}, f"sidecar up but feed silent for {silent:.0f}s"
    return d["ticks"], None
