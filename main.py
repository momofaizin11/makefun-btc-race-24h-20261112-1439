"""btc-race-24h-20261112-1439 -- a trend-following bot for agent-fun/bars-replay/v1.

One symbol (CBBTC), long only, at most one open position, no shorting and no leverage.

The rules, in order, on every batch:

1.  The EMA of the 1-minute closes, span 480 (alpha = 2 / 481), seeded with the first close and
    stepped once per batch, in a module-level variable. Nothing else carries state between batches
    but that EMA, the batch counter and the entry price, so a replayed dataset gives byte-identical
    action logs.
2.  A warm-up batch (state.warming_up, the first state.warmup_bars batches) only feeds the EMA: no
    order is sent on one, so the history is there before the scored window opens. The batch counter
    counts the tradeable batches only (0 on the first one, the batch whose seq is warmup_bars). No
    wall clock is ever read.
3.  On the first tradeable batch it sends one small buy, 20 USDC of notional at 100 bps of
    slippage: the contest needs at least one trade, so the bot always has one even if the trend
    never shows up.
4.  From the 30th tradeable batch on, while the position counts as empty (worth less than 1% of
    equity, so the leftovers of the insurance ticket do not count), it buys when the last close is
    more than 10 bps above the EMA. The buy spends the cash, but its weight is capped by weight_room
    (100% of equity) with 1% kept back as a margin, and an amount under 1 USDC is not sent at all.
5.  While a real position is held it sells all of it when the close falls below the EMA, or when it
    falls 100 bps below the price the entry was decided on (stop-loss).
6.  After an exit the rules above are free to enter again, so the bot may round-trip many times.
7.  An equity brake: the equity the first bar showed is remembered, and once equity has fallen to
    90% of it or less the bot opens no new position for the rest of the run. A position it already
    holds is still sold by rule 5, so the brake never traps the bot in the drawdown.

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
#: The equity brake trips once equity is down to this share of the first bar's equity (90%).
EQUITY_BRAKE_BPS = 9_000

#: Strategy state: the EMA, the tradeable batches answered, the close the entry was decided on, the
#: equity the first bar showed and whether the equity brake has tripped.
_ema = None
_batches = 0
_entry_price = None
_start_equity = None
_braked = False


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
    global _ema, _batches, _entry_price, _start_equity, _braked

    close = state.close(SYMBOL)
    if close is None:
        # No CBBTC bar yet (a gap at the window's start): nothing to read, nothing decided.
        return []

    # 1. The EMA, seeded with the first close and stepped once per batch, warm-up ones included.
    if _ema is None:
        _ema = float(close)
    else:
        _ema = _ema + EMA_ALPHA * (float(close) - _ema)

    equity = state.equity
    if _start_equity is None:
        # 7. The equity the first bar showed, the reference of the brake.
        _start_equity = equity

    # 2a. A warm-up bar builds the history and nothing else: every order decided on one is rejected.
    if state.warming_up:
        return []

    # 2. How many tradeable batches have been answered before this one (0 on the first).
    n = _batches
    _batches += 1

    # 7. Once equity is down to 90% of the first bar's, no new position is opened for the rest of
    # the run. The latch stays on: the brake never re-opens. Exits below still run.
    if _start_equity > 0 and equity * BPS <= _start_equity * EQUITY_BRAKE_BPS:
        _braked = True

    qty = _held_qty(state)
    value = state.value(SYMBOL, qty) if qty else 0
    real = value * BPS >= equity * REAL_POSITION_BPS

    # 5. A real position exits when the trend breaks or the stop is hit.
    if real:
        if close < _ema or (_entry_price is not None and close < _entry_price * STOP_LOSS):
            _entry_price = None
            return [{"side": "sell", "symbol": SYMBOL, "qty": qty, "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        return []

    if _braked:
        return []

    # 3. The insurance ticket, on the very first tradeable batch.
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
