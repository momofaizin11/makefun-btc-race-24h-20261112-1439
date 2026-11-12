"""btc-race-24h-20261112-1439 -- a trend-following bot for agent-fun/bars-replay/v1.

One symbol (CBBTC), long only, at most one open position, no shorting and no leverage.

The rules, in order, on every batch:

1.  The EMA of the 1-minute closes, span 480 (alpha = 2 / 481), seeded with the first close and
    stepped once per batch, in a module-level variable. Nothing else carries state between batches
    but that EMA, the batch counter and the entry price, so a replayed dataset gives byte-identical
    action logs.
2.  The batch counter counts the batches this bot has answered (0 on the first one). No wall clock
    is ever read.
3.  On the first batch it sends one small buy, 20 USDC of notional at 100 bps of slippage: the
    contest needs at least one trade, so the bot always has one even if the trend never shows up.
4.  From the 30th batch on, while the position counts as empty (worth less than 1% of equity, so
    the leftovers of the insurance ticket do not count), it buys when the last close is more than
    10 bps above the EMA. The buy spends the cash, but its weight is capped by weight_room (100% of
    equity) with 1% kept back as a margin, and an amount under 1 USDC is not sent at all.
5.  While a real position is held it sells all of it when the close falls below the EMA, or when it
    falls 100 bps below the price the entry was decided on (stop-loss).
6.  After an exit the rules above are free to enter again, so the bot may round-trip many times.

Everything is decided from the bars the harness has already sent. The stdlib only.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bars  # noqa: E402  (the shim, imported after sys.path is fixed)

SYMBOL = "CBBTC"
BPS = 10_000
QUOTE_RAW = 1_000_000

#: EMA of the close, span 480 bars: alpha = 2 / (480 + 1).
EMA_SPAN = 480
EMA_ALPHA = 2.0 / (EMA_SPAN + 1.0)

#: The entry needs the close 10 bps above the EMA (EMA x 1.001), from this batch on.
ENTRY_BATCH = 30
ENTRY_ABOVE_EMA = 1.001

#: The exit when the close is 100 bps under the price the entry was decided on.
STOP_LOSS = 0.99

#: The insurance ticket: 20 USDC, and the floor under every order, 1 USDC.
INSURANCE_NOTIONAL = 20 * QUOTE_RAW
MIN_NOTIONAL = 1 * QUOTE_RAW
#: At most this far past the close a fill may land.
MAX_SLIPPAGE_BPS = 100
#: A position worth less than 1% of equity counts as empty.
REAL_POSITION_BPS = 100
#: The margin kept under the 100% weight cap, a 100th of the room.
WEIGHT_MARGIN_DIVISOR = 100

#: Strategy state: the EMA, the batches answered, and the close the entry was decided on.
_ema = None
_batches = 0
_entry_price = None


def _held_qty(state):
    """The quantity of SYMBOL held (0 when flat), whatever the shim calls its positions."""
    positions = getattr(state, "positions", None)
    if positions is not None:
        held = positions.get(SYMBOL)
        if held is not None:
            return int(getattr(held, "qty", 0) or 0)
        return 0
    position = getattr(state, "position", None)
    if callable(position):
        held = position(SYMBOL)
        if held is None:
            return 0
        return int(held["qty"]) if isinstance(held, dict) else int(getattr(held, "qty", 0) or 0)
    return 0


def on_batch(state, batch):
    """The orders decided on this bar: at most one, a sell or a buy (or none)."""
    global _ema, _batches, _entry_price

    close = state.close(SYMBOL)
    if close is None:
        # No CBBTC bar yet (a gap at the window's start): nothing to read, nothing decided.
        return []

    # 1. The EMA, seeded with the first close and stepped once per batch.
    if _ema is None:
        _ema = float(close)
    else:
        _ema = _ema + EMA_ALPHA * (float(close) - _ema)

    # 2. How many batches have been answered before this one.
    n = _batches
    _batches += 1

    equity = state.equity
    qty = _held_qty(state)
    value = state.value(SYMBOL, qty) if qty else 0
    real = value * BPS >= equity * REAL_POSITION_BPS

    # 5. A real position exits when the trend breaks or the stop is hit.
    if real:
        if close < _ema or (_entry_price is not None and close < _entry_price * STOP_LOSS):
            _entry_price = None
            return [{"side": "sell", "symbol": SYMBOL, "qty": qty, "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        return []

    # 3. The insurance ticket, on the very first batch.
    if n == 0:
        return [{"side": "buy", "symbol": SYMBOL, "notional_quote_units": INSURANCE_NOTIONAL, "max_slippage_bps": MAX_SLIPPAGE_BPS}]

    # 4. The trend entry, from the 30th batch on.
    if n < ENTRY_BATCH or float(close) <= _ema * ENTRY_ABOVE_EMA:
        return []
    room = state.weight_room(SYMBOL)
    room = room - room // WEIGHT_MARGIN_DIVISOR
    cash = state.cash
    notional = cash if cash < room else room
    if notional < MIN_NOTIONAL:
        return []
    _entry_price = close
    return [{"side": "buy", "symbol": SYMBOL, "notional_quote_units": notional, "max_slippage_bps": MAX_SLIPPAGE_BPS}]


if __name__ == "__main__":
    makefun_bars.run(on_batch)
