# btc-race-24h-20261112-1439

A long-only trend bot for `agent-fun/bars-replay/v1`: one symbol (`CBBTC`), at most one open
position, no shorts, no leverage, Python 3.13 stdlib only (`main.py`, with the kit's shim under
`vendor/`).

## Strategy

State carried between batches is three values, all module level: the EMA of the close, how many
batches have been answered, and the close the current entry was decided on. Nothing else is kept and
nothing outside the harness's batches is ever read, so two runs over the same dataset write the same
replies byte for byte.

* **EMA(480).** `alpha = 2 / (480 + 1)`. It is seeded with the first close and stepped once per
  batch (`ema += alpha * (close - ema)`).
* **Batch counter.** Own counter, starting at 0. No wall clock is read.
* **Insurance ticket (batch 0).** One buy of 20 USDC notional at `max_slippage_bps` 100. The rules
  need at least one trade, so the bot always has one even on a window where the trend never shows.
* **Entry (batch 30 onward).** While the position counts as empty — worth under 1% of equity, so the
  leftovers of the insurance ticket are not a position — and the close is more than 10 bps above the
  EMA (`close > ema * 1.001`), it buys. The buy spends the cash (`min(cash, weight_room)`), the
  weight room under the 100% cap is read first and 1% of it is kept back as a margin, and an amount
  under 1 USDC is not sent.
* **Exit.** While a real position is held (worth at least 1% of equity) it sells all of the quantity
  when the close falls below the EMA (the trend broke) or below the entry close by 100 bps
  (`close < entry * 0.99`, stop-loss).
* **Re-entry.** After an exit the entry rule is free to fire again, so the bot may round-trip many
  times.

Sells and buys both cap slippage at 100 bps; at most one order is sent per batch, and no order is
sent when the amount would be under the 1 USDC minimum notional.

## Verification

Graded locally with the kit's official grader (same Docker harness and the same 1,439-bar dev
window the server scores on):

```
node /root/makefun/kits/btc-race-24h-20261112-1439/btc-race-24h-20261112-1439-kit/cli/makefun.mjs \
  grade-local --kit /root/makefun/kits/btc-race-24h-20261112-1439/btc-race-24h-20261112-1439-kit \
  --bot /root/makefun/btc-race/repo --worktree --driver docker
```

Result: net PnL **+3.000089 USDC**, a return of **0.003000089** (about +30.0 bps on the 1,000 USDC
start), **4 trades**, maximum drawdown **0.691536%** (well under the 2% target), **0 rejected orders**,
fees 0.809323 USDC, and the guardrails met (trades ok, symbols ok, drawdown ok). The grader's L1
regrade reproduced the two executions byte for byte.

The four fills: the insurance ticket (20 USDC, decided on batch 0) sold again on the next bar once
the close was under the EMA, then the one full-size round trip (entered when the close first cleared
the EMA by 10 bps, exited when the close fell back under it). This is honest about what the strategy
is: a single trend ride plus a throwaway ticket, not a busy market maker.
