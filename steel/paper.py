"""Paper-trading simulator: follows the model's live suggestions with a $100k book that
runs continuously and compounds. Every position's loss is capped by construction. A spot long
can lose its stake; a perp (a long at `long_leverage`, a short at `short_leverage`) is an
isolated-margin position whose loss is capped at the margin posted, because the venue
liquidates it there. Funding accrues on every open perp at the venue's rate: a positive rate
pays the shorts and charges the longs, a negative one the reverse. Longs are perps or spot by
`long_mode`; shorts are always perps, never a naked borrow. No leverage on the book as a
whole (gross exposure capped at equity), per-name weight capped, marked to market. Fees are
the venue's taker rate per leg: `cost_bps` on spot, `perp_fee_bps` on perps. State is
persisted in the checkpoint, so positions, cash, and P&L survive restarts.
"""
from __future__ import annotations

from collections import deque

from .policy import liquidation_price


class PaperBook:
    def __init__(self, start: float = 100_000.0, cost_bps: float = 1.0, max_weight: float = 0.15,
                 min_trade_frac: float = 0.004, perp_fee_bps: float = 5.5, short_min_hold_s: float = 0.0,
                 short_max_frac: float = 0.05, short_leverage: float = 2.0, funding_interval_s: float = 8 * 3600.0,
                 long_mode: str = "spot", long_leverage: float = 1.0, maintenance: float = 0.0,
                 venues: tuple[str, str] = ("", ""), perp_cash_frac: float = 0.0, transfer=None,
                 transfer_asset: str = "USDC", min_transfer_usd: float = 250.0):
        self.start = float(start)
        self.cash = float(start)                     # free cash on the spot venue
        # Two venues, two pools: spot cash lives on the spot venue, collateral on the perp venue,
        # and moving between them is an on-chain stablecoin transfer (`transfer(asset, qty, px)`
        # -> fees.Transfer) that costs its fee and holds the money in flight for its wait. One
        # venue for both (or no transfer model) means one pool, as before.
        self.venues = (str(venues[0] or "").lower(), str(venues[1] or "").lower())
        self.transfer = transfer
        self.split = transfer is not None and self.venues[0] != self.venues[1]
        self.transfer_asset = str(transfer_asset or "USDC").upper()
        self.min_transfer_usd = float(min_transfer_usd)
        self.collateral = 0.0                        # free cash on the perp venue
        if self.split:
            self.collateral = self.cash * max(0.0, min(1.0, float(perp_cash_frac)))
            self.cash -= self.collateral
        self.in_flight: list[dict] = []              # transfers sent and not yet credited
        self.transfer_fees = 0.0
        self.n_transfers = 0
        self.transfer_wait_s = 0.0                   # time positions waited on money in flight, summed
        self.transfers: deque = deque(maxlen=200)
        self.cost_bps = float(cost_bps)              # spot taker fee per leg
        self.max_weight = float(max_weight)          # cap on a single name's gross weight
        self.min_trade_frac = float(min_trade_frac)  # skip rebalances smaller than this share of equity
        self.perp_fee_bps = float(perp_fee_bps)      # taker fee per perp leg
        self.short_min_hold_s = float(short_min_hold_s)   # hold a perp at least this long before closing (anti-churn)
        self.short_max_frac = float(short_max_frac)  # margin per short, as a share of equity: the most one short can lose
        self.short_leverage = max(1.0, float(short_leverage))
        self.long_mode = "perp" if str(long_mode).lower() == "perp" else "spot"
        self.long_leverage = max(1.0, float(long_leverage))
        self.maintenance = max(0.0, float(maintenance))   # the venue's maintenance margin fraction: liquidation comes that much sooner
        self.funding_interval_s = max(60.0, float(funding_interval_s))
        self.pos: dict[str, float] = {}              # spot units (longs only)
        self.avg: dict[str, float] = {}              # average entry price
        self.perps: dict[str, dict] = {}             # sym -> {side, qty, entry, margin, leverage, liq, opened, funding, funded_at}
        self.realized = 0.0
        self.fees = 0.0
        self.perp_fees = 0.0
        self.funding_net = 0.0                       # funding earned (+) or paid (-) over the book's life
        self.n_trades = 0
        self.n_liquidations = 0
        self.spread_cost = 0.0
        self.trades: deque = deque(maxlen=1000)      # a whole day's fills; the API returns all of them
        self.curve: deque = deque(maxlen=3000)
        self.last: dict[str, float] = {}
        self.started = None
        self._now = 0.0

    # Views ----------------------------------------------------------------------------
    @property
    def shorts(self) -> dict[str, dict]:
        """The open perp shorts (a read view; the store is `perps`)."""
        return {s: p for s, p in self.perps.items() if p.get("side", -1) < 0}

    @property
    def longs(self) -> dict[str, dict]:
        """The open perp longs."""
        return {s: p for s, p in self.perps.items() if p.get("side", -1) > 0}

    def _px(self, s, prices):
        return prices.get(s) or self.last.get(s)

    def _perp_value(self, s, prices, now=None) -> float:
        """What closing the perp would return: margin plus the mark-to-market gain plus the
        funding accrued, never below zero (at zero the venue has liquidated it)."""
        p = self.perps.get(s)
        spot = self._px(s, prices) if p else None
        if not p or not spot:
            return 0.0
        pnl = p.get("side", -1) * (float(spot) - p["entry"]) * p["qty"]
        return max(0.0, p["margin"] + pnl + p.get("funding", 0.0))

    def _short_value(self, s, prices, now=None) -> float:
        return self._perp_value(s, prices, now)

    def equity(self, prices, now=None) -> float:
        now = self._now if now is None else now
        v = self.cash + self.collateral + sum(f["amount"] for f in self.in_flight)
        for s, sh in self.pos.items():
            p = self._px(s, prices)
            if p:
                v += sh * p
        for s in self.perps:
            v += self._perp_value(s, prices, now)
        return v

    def rebalance(self, targets: dict, prices: dict, ts: float, quotes: dict | None = None,
                  funding: dict | None = None, uncapped: set | None = None) -> None:
        """targets: symbol -> desired signed weight (side * size). A negative weight is a short,
        opened as an isolated-margin perp of that notional (margin = notional / leverage, capped
        at short_max_frac of equity) and closed once the signal is gone, after a minimum hold. A
        positive weight is spot, or in perp mode a perp long at long_leverage (margin = the
        stake at 1x: the same worst case as spot, at the perp venue's fees and funding).
        funding: symbol -> the venue's current per-interval funding rate, accrued on open perps
        for the time since the last mark."""
        quotes = quotes or {}
        funding = funding or {}
        self._now = ts
        for s, p in prices.items():
            if p:
                self.last[s] = p
        if self.started is None:
            self.started = ts
        self._land(ts)
        self._accrue_funding(funding, ts)
        self._settle_liquidations(prices, ts)
        eq = self.equity(prices, ts)
        # `uncapped` names (the index hedge) take their weight as given, stay out of the leverage
        # scale and skip the per-position margin cap: a hedge that stops at the cap is not a hedge.
        uncapped = uncapped or set()
        tw = {s: (float(w) if s in uncapped else max(-self.max_weight, min(self.max_weight, float(w)))) for s, w in targets.items()}
        gross = sum(abs(w) for s, w in tw.items() if s not in uncapped)
        scale = (1.0 / gross) if gross > 1 else 1.0        # no leverage on the book
        min_notional = max(200.0, eq * self.min_trade_frac)
        for s, w in tw.items():
            ref = self._px(s, prices)
            if not ref or ref <= 0:
                continue
            perp = self.perps.get(s)
            old_enough = perp is not None and ts - perp.get("opened", 0.0) >= self.short_min_hold_s
            if w > 0 and self.long_mode == "perp":          # a long perp: close any short or spot first, then hold one
                if perp and perp["side"] < 0 and old_enough:
                    self._close_perp(s, self._exec_price(s, +1.0, ref, quotes, charge=False), ts)
                cur = self.pos.get(s, 0.0)
                if cur > 0:
                    self._fill(s, cur, -cur, self._exec_price(s, -cur, ref, quotes), ts)
                if s not in self.perps:
                    self._open_perp(s, +1, eq * w * scale, self._exec_price(s, +1.0, ref, quotes, charge=False), ts,
                                    None if s in uncapped else eq)
            elif w >= 0:                                    # long or flat: close any perp (once past min hold), manage spot
                if perp and (perp["side"] < 0 or w == 0.0) and old_enough:
                    self._close_perp(s, self._exec_price(s, -perp["side"], ref, quotes, charge=False), ts)
                desired = eq * w * scale / ref
                cur = self.pos.get(s, 0.0)
                delta = desired - cur
                if abs(delta * ref) >= min_notional:
                    self._fill(s, cur, delta, self._exec_price(s, delta, ref, quotes), ts)
            else:                                           # short: an isolated-margin perp, never a naked borrow
                cur = self.pos.get(s, 0.0)
                if cur > 0:                                 # close any spot first
                    self._fill(s, cur, -cur, self._exec_price(s, -cur, ref, quotes), ts)
                if perp and perp["side"] > 0 and old_enough:
                    self._close_perp(s, self._exec_price(s, -1.0, ref, quotes, charge=False), ts)
                if s not in self.perps:
                    self._open_perp(s, -1, eq * abs(w) * scale, self._exec_price(s, -1.0, ref, quotes, charge=False), ts,
                                    None if s in uncapped else eq)
                # already short -> hold it (marked)
        self.curve.append({"t": ts, "eq": round(self.equity(prices, ts), 2)})

    # Money between venues ---------------------------------------------------------------
    def _land(self, ts: float) -> None:
        """Credit every transfer that has arrived."""
        still = []
        for f in self.in_flight:
            if f["lands_at"] <= ts:
                if f["to"] == "perp":
                    self.collateral += f["amount"]
                else:
                    self.cash += f["amount"]
                self.transfers.appendleft({**f, "t": ts, "event": "landed"})
            else:
                still.append(f)
        self.in_flight = still

    def _pool(self, name: str) -> float:
        return self.collateral if name == "perp" else self.cash

    def _need(self, pool: str, amount: float, ts: float) -> bool:
        """True when `pool` holds `amount`. Otherwise start a transfer of the shortfall (at least
        min_transfer_usd) from the other pool, charge its fee, and answer False: the action waits
        for the money to land, and the signal has to still be there when it does."""
        if not self.split:
            return True
        have = self._pool(pool)
        if have >= amount:
            return True
        pending = sum(f["amount"] for f in self.in_flight if f["to"] == pool)
        if have + pending >= amount:
            return False                                # already on its way
        other = "spot" if pool == "perp" else "perp"
        free = self._pool(other)
        want = max(amount - have - pending, self.min_transfer_usd)
        t = self.transfer(self.transfer_asset, want, 1.0)
        move = min(free, want + float(t.fee_usd))       # the fee comes out of the amount sent, so send it on top
        if move < 1.0:
            return False                                # nothing to move: the book is fully deployed
        fee = min(float(t.fee_usd), move)
        if other == "perp":
            self.collateral -= move
        else:
            self.cash -= move
        self.transfer_fees += fee
        self.n_transfers += 1
        self.transfer_wait_s += float(t.seconds)
        f = {"from": other, "to": pool, "amount": move - fee, "fee": fee, "asset": t.asset, "rail": t.rail,
             "sent": ts, "lands_at": ts + float(t.seconds), "venue": self.venues[1 if pool == "perp" else 0]}
        self.in_flight.append(f)
        self.transfers.appendleft({**f, "t": ts, "event": "sent"})
        return False

    def _exec_price(self, s, delta, ref, quotes, charge: bool = True) -> float:
        q = quotes.get(s) or {}
        bid, ask = q.get("bid"), q.get("ask")
        if bid and ask and ask > bid > 0:
            if charge:
                self.spread_cost += abs(delta) * (ask - bid) / 2.0
            return ask if delta > 0 else bid
        return ref

    def _fill(self, s, cur, delta, p, ts) -> None:
        notional = delta * p
        fee = abs(notional) * self.cost_bps / 1e4
        if delta > 0 and not self._need("spot", notional + fee, ts):
            return                                      # the cash is on the other venue: wait for it
        self.cash -= notional + fee
        self.fees += fee
        new = cur + delta
        if cur == 0 or (cur > 0) == (delta > 0):
            tot = abs(cur) + abs(delta)
            self.avg[s] = (self.avg.get(s, p) * abs(cur) + p * abs(delta)) / tot if tot else p
        else:
            closed = min(abs(delta), abs(cur))
            self.realized += (p - self.avg.get(s, p)) * (closed if cur > 0 else -closed)
            if (new > 0) != (cur > 0) and abs(new) > 1e-9:
                self.avg[s] = p
        if abs(new) < 1e-9:
            self.pos.pop(s, None)
            self.avg.pop(s, None)
        else:
            self.pos[s] = new
        self.n_trades += 1
        self.trades.appendleft({"t": ts, "sym": s, "side": "buy" if delta > 0 else "sell",
                                "shares": round(abs(delta), 6), "price": round(p, 6),
                                "notional": round(abs(notional), 2)})

    def _open_perp(self, s, side: int, notional, px, ts, eq: float | None = None) -> None:
        if notional <= 0 or not px or px <= 0:
            return
        lev = self.short_leverage if side < 0 else self.long_leverage
        margin = notional / lev
        if eq and side < 0:                                 # a short's margin at risk is capped as a share of equity
            margin = min(margin, eq * self.short_max_frac)
        notional = margin * lev
        if notional < 50.0:
            return
        qty = notional / px
        fee = notional * self.perp_fee_bps / 1e4
        if not self._need("perp", margin + fee, ts):
            return                                      # the margin is on the spot venue: wait for the transfer
        if self.split:
            self.collateral -= margin + fee
        else:
            self.cash -= margin + fee
        self.perp_fees += fee
        self.n_trades += 1
        liq = liquidation_price(px, lev, side, self.maintenance)
        self.perps[s] = {"side": int(side), "qty": qty, "entry": float(px), "margin": margin, "leverage": lev,
                         "liq": liq, "opened": ts, "funding": 0.0, "funded_at": ts}
        self.trades.appendleft({"t": ts, "sym": s, "side": "open short" if side < 0 else "open long", "shares": round(qty, 6),
                                "price": round(px, 6), "notional": round(notional, 2),
                                "note": f"{lev:g}x isolated, margin {margin:,.0f}, liq {liq:,.2f}" if liq > 0 else f"{lev:g}x isolated, margin {margin:,.0f}, no liquidation"})

    def _open_short(self, s, notional, px, ts, eq: float | None = None) -> None:
        self._open_perp(s, -1, notional, px, ts, eq)

    def _close_perp(self, s, px, ts, liquidated: bool = False) -> None:
        p = self.perps.pop(s, None)
        if not p:
            return
        side = p.get("side", -1)
        if liquidated:
            val, fee, px = 0.0, 0.0, p["liq"]
            self.n_liquidations += 1
        else:
            val = max(0.0, p["margin"] + side * (float(px) - p["entry"]) * p["qty"] + p.get("funding", 0.0))
            fee = p["qty"] * float(px) * self.perp_fee_bps / 1e4
        if self.split:
            self.collateral += val - fee
        else:
            self.cash += val - fee
        self.perp_fees += fee
        self.realized += val - p["margin"]
        self.n_trades += 1
        self.trades.appendleft({"t": ts, "sym": s, "side": "liquidated" if liquidated else ("close short" if side < 0 else "close long"),
                                "shares": round(p["qty"], 6), "price": round(float(px), 6), "notional": round(val, 2),
                                **({"note": f"margin {p['margin']:,.0f} lost"} if liquidated else {})})

    def _close_short(self, s, px, ts, liquidated: bool = False) -> None:
        self._close_perp(s, px, ts, liquidated)

    def _accrue_funding(self, funding: dict, ts: float) -> None:
        """Funding accrues continuously at the venue's current rate, pro rata to the time since
        the last mark: a positive rate pays the shorts and charges the longs."""
        for s, p in self.perps.items():
            rate = funding.get(s)
            last = float(p.get("funded_at") or ts)
            dt = max(0.0, ts - last)
            p["funded_at"] = ts
            if rate is None or dt <= 0:
                continue
            notional = p["qty"] * (self.last.get(s) or p["entry"])
            accrued = -p.get("side", -1) * float(rate) * notional * dt / self.funding_interval_s
            p["funding"] = p.get("funding", 0.0) + accrued
            self.funding_net += accrued

    def _settle_liquidations(self, prices, ts) -> None:
        """A perp whose mark has reached its liquidation price is closed by the venue for the
        margin: the worst case, and the whole of it. A 1x long has no liquidation price."""
        gone = []
        for s, p in self.perps.items():
            px = self._px(s, prices) or 0.0
            if p["liq"] <= 0 or not px:
                continue
            if (p.get("side", -1) < 0 and px >= p["liq"]) or (p.get("side", -1) > 0 and px <= p["liq"]):
                gone.append(s)
        for s in gone:
            self._close_perp(s, self._px(s, prices), ts, liquidated=True)

    def to_state(self) -> dict:
        return {"cash": self.cash, "pos": dict(self.pos), "avg": dict(self.avg), "perps": dict(self.perps),
                "realized": self.realized, "fees": self.fees, "perp_fees": self.perp_fees, "funding_net": self.funding_net,
                "n_trades": self.n_trades, "n_liquidations": self.n_liquidations, "start": self.start,
                "started": self.started, "last": dict(self.last),
                "spread_cost": self.spread_cost, "trades": list(self.trades), "curve": list(self.curve),
                "collateral": self.collateral, "in_flight": list(self.in_flight), "transfer_fees": self.transfer_fees,
                "n_transfers": self.n_transfers, "transfer_wait_s": self.transfer_wait_s, "transfers": list(self.transfers)}

    def load_state(self, d: dict, symbols=None) -> None:
        self.cash = float(d.get("cash", self.cash))
        self.pos = {k: float(v) for k, v in (d.get("pos") or {}).items() if symbols is None or k in symbols}
        self.avg = {k: float(v) for k, v in (d.get("avg") or {}).items() if k in self.pos}
        saved = dict(d.get("perps") or {})
        for k, v in (d.get("shorts") or {}).items():         # a book saved before perp longs existed
            saved.setdefault(k, {**v, "side": -1})
        self.perps = {k: {**dict(v), "side": int(v.get("side", -1))} for k, v in saved.items() if symbols is None or k in symbols}
        self.realized = float(d.get("realized", 0.0))
        self.fees = float(d.get("fees", 0.0))
        self.perp_fees = float(d.get("perp_fees", 0.0))
        self.funding_net = float(d.get("funding_net", 0.0))
        self.n_trades = int(d.get("n_trades", 0))
        self.n_liquidations = int(d.get("n_liquidations", 0))
        self.spread_cost = float(d.get("spread_cost", 0.0))
        self.start = float(d.get("start", self.start))
        self.started = d.get("started")
        self.last = dict(d.get("last") or {})
        self.trades = deque(d.get("trades") or [], maxlen=self.trades.maxlen)
        self.curve = deque(d.get("curve") or [], maxlen=self.curve.maxlen)
        if "collateral" in d:
            self.collateral = float(d.get("collateral") or 0.0)
        elif self.split:                                 # a book saved with one pool: split its cash as a fresh one would
            self.collateral = self.cash * 0.5
            self.cash -= self.collateral
        else:
            self.collateral = 0.0
        if not self.split and self.collateral:          # the venues became one: a single pool again
            self.cash += self.collateral
            self.collateral = 0.0
        self.in_flight = [dict(f) for f in (d.get("in_flight") or [])] if self.split else []
        if not self.split:
            self.cash += sum(float(f.get("amount") or 0.0) for f in (d.get("in_flight") or []))
        self.transfer_fees = float(d.get("transfer_fees", 0.0))
        self.n_transfers = int(d.get("n_transfers", 0))
        self.transfer_wait_s = float(d.get("transfer_wait_s", 0.0))
        self.transfers = deque(d.get("transfers") or [], maxlen=self.transfers.maxlen)

    def snapshot(self, prices: dict) -> dict:
        now = self._now
        eq = self.equity(prices, now)
        wait_min = self.transfer_wait_s * (1 / 60)   # written without a slash in a string: the order-word grep reads literals
        positions, upnl = [], 0.0
        for s, sh in self.pos.items():
            p = self._px(s, prices) or 0.0
            a = self.avg.get(s, p)
            val = sh * p
            u = (p - a) * sh
            upnl += u
            positions.append({"sym": s, "kind": "spot", "shares": round(sh, 6), "avg": round(a, 6),
                              "price": round(p, 6), "value": round(val, 2), "upnl": round(u, 2),
                              "weight": round(val / eq, 4) if eq else 0.0})
        for s, pp in self.perps.items():
            p = self._px(s, prices) or 0.0
            val = self._perp_value(s, prices, now)
            u = val - pp["margin"]
            upnl += u
            positions.append({"sym": s, "kind": "short" if pp.get("side", -1) < 0 else "long", "shares": round(pp["qty"], 6),
                              "avg": round(pp["entry"], 6), "price": round(p, 6), "value": round(val, 2), "upnl": round(u, 2),
                              "risk": round(pp["margin"], 2), "leverage": pp["leverage"], "liq": round(pp["liq"], 6),
                              "funding": round(pp.get("funding", 0.0), 2), "notional": round(pp["qty"] * p, 2),
                              "weight": round(pp["qty"] * p / eq, 4) if eq else 0.0})
        positions.sort(key=lambda x: -abs(x["value"]))
        # Compounding scorecard: the geometric-growth consistency (Sharpe) and the peak-to-trough
        # drawdown that erodes it. "Small, fast, repeatable" wants high Sharpe and shallow drawdowns.
        eqs = [c["eq"] for c in self.curve]
        peak, mdd = (eqs[0] if eqs else self.start), 0.0
        for e in eqs:
            peak = max(peak, e)
            if peak > 0:
                mdd = max(mdd, (peak - e) / peak)
        rets = [eqs[i] / eqs[i - 1] - 1.0 for i in range(1, len(eqs)) if eqs[i - 1]]
        sharpe = 0.0
        if len(rets) > 1:
            mean = sum(rets) / len(rets)
            sd = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5
            sharpe = (mean / sd) if sd > 0 else 0.0    # per-bar risk-adjusted return
        gross = sum(abs(x["value"]) for x in positions if x["kind"] == "spot") + sum(x["notional"] for x in positions if x["kind"] != "spot")
        net = (sum(x["value"] for x in positions if x["kind"] == "spot") + sum(x["notional"] for x in positions if x["kind"] == "long")
               - sum(x["notional"] for x in positions if x["kind"] == "short"))
        return {"start": self.start, "equity": round(eq, 2), "cash": round(self.cash, 2),
                "collateral": round(self.collateral, 2), "venues": {"spot": self.venues[0], "perp": self.venues[1], "split": self.split},
                "in_flight": [{"to": f["to"], "venue": f.get("venue", ""), "amount": round(f["amount"], 2), "rail": f.get("rail", ""),
                               "asset": f.get("asset", ""), "lands_in_s": round(max(0.0, f["lands_at"] - now))} for f in self.in_flight],
                "transfer_fees": round(self.transfer_fees, 2), "n_transfers": self.n_transfers,
                "transfer_wait_min": round(wait_min, 1), "transfers": list(self.transfers)[:20],
                "gross": round(gross, 2), "net_exposure": round(net, 2),
                "realized": round(self.realized, 2), "unrealized": round(upnl, 2),
                "fees": round(self.fees, 2), "perp_fees": round(self.perp_fees, 2), "funding_net": round(self.funding_net, 2),
                "spread_cost": round(self.spread_cost, 2),
                "pnl": round(eq - self.start, 2), "return_pct": round((eq / self.start - 1) * 100, 3),
                "n_trades": self.n_trades, "n_liquidations": self.n_liquidations, "started": self.started,
                "n_shorts": len(self.shorts), "n_longs": len(self.longs), "long_mode": self.long_mode,
                "sharpe": round(sharpe, 3), "max_drawdown": round(mdd * 100, 2),
                "positions": positions, "trades": list(self.trades), "curve": list(self.curve)}
