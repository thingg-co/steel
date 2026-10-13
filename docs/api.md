# Steel's data API, version 1

Everything Steel stores, for other programs, under `/api/v1`. Reads only: nothing here places an
order or changes a setting. `GET /api/v1` returns this index as JSON with every route's weight.

## Access

- **Token.** Whenever auth is on (`STEEL_AUTH=mutate`, the default, or `all`), every v1 request
  needs the operator token: `Authorization: Bearer <token>` or `?token=<token>`. `steel token`
  prints it on the box. The dashboard's own routes under `/api/` keep their rules; v1 is for
  programs, which are not the dashboard.
- **Weight budget.** Each route costs its weight; a token (or, without one, a client address)
  may spend `api_weight_per_minute` (`STEEL_API_WEIGHT_PER_MINUTE`, 1200) in a sliding minute.
  Every answer carries `X-Weight-Used` and `X-Weight-Limit`; past the budget the answer is
  429 with `Retry-After`. `GET /api/v1/usage` says what is left and costs nothing.
- **Shapes.** Pairs are canonical `BASE-USD` (`btc`, `BTC/USDT`, `BTCUSD` all resolve); an
  unknown pair is 404. Times are unix seconds, dollar amounts USD, rates fractions (a funding
  rate of 0.0001 is 0.01%), and bps are basis points. Shapes are the dashboard's and are frozen
  under this version; `X-API-Version` names it.

## Routes

| Route | Weight | Returns |
|---|---|---|
| `GET /api/v1` | 0 | this index |
| `GET /api/v1/usage` | 0 | weight used and left in the current minute |
| `GET /api/v1/markets` | 1 | every modeled pair: base, serving venue, kind, price, 24-hour dollar volume, bars held, core |
| `GET /api/v1/screener` | 5 | one row per pair from every stored series |
| `GET /api/v1/candles/{pair}?tf=300&n=300&venues=a,b&to=` | 2 | OHLCV with buy and sell volume per candle: the tape at any `tf` for the day held, the 5-minute bars beyond |
| `GET /api/v1/tape/{pair}?n=50&min_usd=0&bars=60` | 2 | the last prints, the size-band volumes over the hour, per-bar delta and CVD |
| `GET /api/v1/delta/{pair}?bars=60&band=` | 2 | per-bar buy, sell, delta and CVD; `band` is a size band index (0..10, the MMT bands) or `min_usd,max_usd` |
| `GET /api/v1/footprint/{pair}?tf=300&n=120&bucket_bps=2` | 5 | bid and ask volume per price bucket per candle, POC, value area, imbalances, large prints |
| `GET /api/v1/book/{pair}?n=16&bucket_bps=0&quote=` | 2 | the aggregate ladder with venue shares, the depth bands, the per-venue books |
| `GET /api/v1/heatmap/{pair}?tf=300&n=120&bucket_bps=5` | 5 | resting liquidity per price row per column |
| `GET /api/v1/liqmap/{pair}?n=288&bucket_bps=10` | 5 | the modeled liquidation map |
| `GET /api/v1/derivs/{pair}?bars=72` | 2 | open interest, funding and mark per venue and aggregated; liquidations per bar and the recent list |
| `GET /api/v1/positioning/{pair}?n=288` | 3 | OI-confirmed net longs and shorts per bar, absorption flags |
| `GET /api/v1/premium/{pair}?tf=300&n=288&reference=binance` | 3 | per-venue premium to the reference venue and the perp-spot basis |
| `GET /api/v1/levels/{pair}?days=35` | 2 | day, week and month opens, highs, lows and closes |
| `GET /api/v1/options/{asset}` | 2 | Deribit's chain: GEX by strike, smile, term structure, max pain, zero gamma |
| `GET /api/v1/forecast/{pair}` | 1 | the model's latest call: action, quantiles, P(up), size, the extra horizons |
| `GET /api/v1/calendar` | 1 | the next scheduled events and the event clock |
| `GET /api/v1/predictions` | 1 | Polymarket's open crypto markets |

## The stream

`ws://host:8100/api/v1/stream?pairs=BTC-USD,ETH-USD&channels=trades,book,liquidations,bars,rules&token=…`

One JSON object per event, `ch` first. Without `pairs` every pair is sent; without `channels`,
trades only. The first message is `{"ch": "hello", ...}` with what was subscribed.

| Channel | One message per | Fields |
|---|---|---|
| `trades` | real print on any live venue | `pair, t, venue, px, sz, usd, side` (1 taker buy, -1 taker sell, 0 unknown) |
| `book` | depth update, at most four a second per pair | `pair, t, tops: {venue: {bid, ask, t}}` |
| `liquidations` | forced order on a perp venue | `pair, t, venue, px, usd, side` (-1 a long liquidated) |
| `bars` | 5-minute bar close | `t, index, bars: {pair: bar}` |
| `rules` | series-alert hit | `pair, id, series, value, t, text` |

A client that falls behind loses events (each subscriber has a bounded queue); nothing is
buffered on its behalf. Send anything to keep a proxy's idle timer quiet; the server ignores it.

## Examples

```
curl -s -H "Authorization: Bearer $TOKEN" localhost:8100/api/v1/candles/btc?tf=60&n=120
curl -s "localhost:8100/api/v1/delta/ETH-USD?band=7&token=$TOKEN"       # prints of 250k to 500k
python - <<'EOF'
import asyncio, json, websockets
async def main():
    async with websockets.connect("ws://localhost:8100/api/v1/stream?pairs=BTC-USD&channels=trades,liquidations&token=TOKEN") as ws:
        async for m in ws: print(json.loads(m))
asyncio.run(main())
EOF
```
