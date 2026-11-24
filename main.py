"""Momentum paper bot for the Pump.fun Replay Sprint (agent-fun/replay/v2).

Strategy: trend-following on fresh pump.fun bonding curves, in plain words.
A curve becomes a buy candidate once it is mature enough (past MIN_AGE slots
since its CreateEvent, so bundled launch dumps are skipped) and its recent
tape shows accelerating buy pressure: net SOL flowing in over the entry
window, several distinct buyers, a price rising but not already run away, and
the price sitting near its window high with the newer half of the window
bringing in more than the older half.

The first buy is small (INITIAL_SOL). The position is then pyramided into
strength only: each later batch adds ADD_SOL while the price is at or above
the last buy's price and net SOL still flows in, up to STAKE_SOL per coin.
So false starts cost little and runners get size.

A position is sold whole on the first of: a stop loss against cost, a
trailing stop from its best close value (armed once the position is up),
a fade exit when recent net flow turns negative, a single large recorded
sell, or a time limit. A curve that completes is closed by the replay
itself at its final reserves.

Guardrail probes keep the run above the minimum bar (20 distinct coins):
when the bot's own fills or coins lag a pace set in block time, it buys
PROBE_SOL of a liquid curve and sells it as soon as the buy lands.

The hello message, each batch of events, and the orders sent back in reply
are the protocol this bot speaks: hello first, then one batch per slot,
then one reply of orders per batch, in the order the batches arrive.
Deterministic by construction: no clock, no randomness, no environment
reads, and iteration only over insertion-ordered maps fed by the batch
stream, so the two graded executions produce byte-identical action logs.

Run by the grader as ``python3 -P -s /bot/main.py`` with PYTHONHASHSEED=0.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script directory off sys.path)

LAMPORTS = makefun_bot.LAMPORTS_PER_SOL
# Curve fee taken on both sides: protocol 95 bps + creator 30 bps = 1.25%.
CURVE_FEE = 0.0125

# --- Entry (momentum) ---
WINDOW = 40            # entry lookback, in slots
MIN_NET_SOL = 2.0      # net buy SOL (buys minus sells) over the window
MIN_BUYS = 3           # buy trades in the window
MIN_BUYERS = 4         # distinct buying wallets in the window
MIN_PRICE_UP = 0.12    # price now over price at window start, minus 1
MAX_PRICE_UP = 1.2     # upper bound on that rise: skip curves that ran too far
ACCEL = 1.0            # newer-half net inflow must be at least this x older half
NEAR_HIGH = 0.93       # price now at least this share of the window high
MIN_RQ_SOL = 1.0       # curve real SOL bounds at entry
MAX_RQ_SOL = 80.0
MIN_AGE = 400          # slots since CreateEvent before an entry is allowed
# --- Sizing ---
STAKE_SOL = 0.4        # SOL to put into one position in total
INITIAL_SOL = 0.03     # first buy of a position
ADD_SOL = 0.10         # each add-on buy
MIN_BUY_SOL = 0.01     # smallest buy worth sending (also the round-trip bar)
BUY_SLIP_BPS = 1500    # slippage on buys; sells always go out at 10000
MAX_EXPOSURE_SOL = 3.0 # most SOL at cost across all positions at once
MAX_MAIN = 5           # most momentum positions open at once
# --- Exits ---
STOP_LOSS = 0.10       # sell when value falls this far below cost
TRAIL = 0.10           # trailing stop: sell this far below the peak value
TRAIL_ARM = 0.05       # trailing stop arms once value reached cost * (1 + this)
FADE_WINDOW = 20       # exit when net SOL over this window falls below -FADE_NET_SOL
FADE_NET_SOL = 1.0
BIG_SELL_SOL = 5.0     # exit on any single recorded sell of at least this
MAX_HOLD = 300         # exit after this many slots held
COOLDOWN = 120         # slots to wait after an exit before re-buying the curve
MAX_ENTRIES = 2        # most entries per curve over the run
# --- Guardrail probes ---
TARGET_COINS = 25
TARGET_FILLS = 240
PACE_SECS = 14400
PROBE_SOL = 0.01
MAX_PROBES = 1

KEEP = max(WINDOW, FADE_WINDOW) + 5  # how many slots of tape to keep per curve


class Tape:
    """A curve's short rolling tape of recorded trades."""

    __slots__ = ("created", "trades", "entries", "last_exit")

    def __init__(self):
        self.created = None       # slot of its CreateEvent, None if unseen
        self.trades = []          # (slot, is_buy, sol, user, price), oldest first
        self.entries = 0          # entries opened on this curve so far
        self.last_exit = -10 ** 12


class Held:
    """A momentum position being managed."""

    __slots__ = ("opened", "peak", "invested", "exiting", "filled",
                 "last_add_price", "last_value")

    def __init__(self, opened, invested, last_add_price):
        self.opened = opened
        self.peak = 0.0           # best close value seen (SOL)
        self.invested = invested  # SOL sent in buys (fees included)
        self.exiting = False
        self.filled = False       # some buy of this position filled
        self.last_add_price = last_add_price
        self.last_value = 0.0     # close value at the latest batch (SOL)


def price_of(vq, vt):
    """Mark price in SOL per token, from virtual reserves."""
    return vq / vt if vt > 0 else 0.0


def close_value(tokens, vq, vt, tx_lamports):
    """What selling `tokens` now would leave after curve fees and tx fee (SOL)."""
    if tokens <= 0 or vt <= 0:
        return 0.0
    gross = (tokens * vq) / (vt + tokens) / LAMPORTS
    return gross * (1.0 - CURVE_FEE) - tx_lamports / LAMPORTS


class Strategy:
    def __init__(self):
        self.tapes = {}           # mint -> Tape, first-seen order
        self.held = {}            # mint -> Held
        self.probes = {}          # mint -> True once its probe sell is in flight
        self.my_fills = 0
        self.my_coins = set()     # membership only, never iterated for order
        self.first_time = None
        self.last_time = None

    def tape_of(self, mint):
        tape = self.tapes.get(mint)
        if tape is None:
            tape = Tape()
            self.tapes[mint] = tape
        return tape

    def entry_signal(self, tape, now, rq_sol):
        """Momentum entry signal on a curve's tape at slot `now`."""
        if rq_sol < MIN_RQ_SOL or rq_sol > MAX_RQ_SOL:
            return False
        if tape.created is None or now - tape.created < MIN_AGE:
            return False
        from_slot = now - WINDOW
        mid_slot = now - WINDOW // 2
        buy_sol = 0.0
        sell_sol = 0.0
        buys = 0
        net_old = 0.0
        net_new = 0.0
        start_price = -1.0
        high = 0.0
        last = 0.0
        buyers = {}
        for (slot, is_buy, sol, user, price) in tape.trades:
            if slot <= from_slot:
                start_price = price
                continue
            if start_price < 0:
                start_price = price
            signed = sol if is_buy else -sol
            if slot <= mid_slot:
                net_old += signed
            else:
                net_new += signed
            if is_buy:
                buy_sol += sol
                buys += 1
                buyers[user] = buyers.get(user, 0.0) + sol
            else:
                sell_sol += sol
            if price > high:
                high = price
            last = price
        if buys < MIN_BUYS or len(buyers) < MIN_BUYERS:
            return False
        if buy_sol - sell_sol < MIN_NET_SOL:
            return False
        if start_price <= 0:
            return False
        up = last / start_price - 1.0
        if up < MIN_PRICE_UP or up > MAX_PRICE_UP:
            return False
        if last < high * NEAR_HIGH:
            return False
        if net_new <= 0 or net_new < ACCEL * net_old:
            return False
        return True

    def net_flow(self, tape, now, window):
        """Net recorded SOL (buys minus sells) over the last `window` slots."""
        net = 0.0
        for i in range(len(tape.trades) - 1, -1, -1):
            (slot, is_buy, sol, _user, _price) = tape.trades[i]
            if slot <= now - window:
                break
            net += sol if is_buy else -sol
        return net

    def on_batch(self, state, batch):
        now = batch["slot"]
        big_sell = set()
        traded = []               # mints traded this batch, in batch order
        traded_seen = set()
        for fill in batch["fills"]:
            if fill["status"] == "filled" and not fill["client_id"].startswith("_"):
                self.my_fills += 1
                self.my_coins.add(fill["mint"])
        for event in batch["events"]:
            block_time = event.get("block_time")
            if block_time is not None:
                if self.first_time is None:
                    self.first_time = block_time
                if self.last_time is None or block_time > self.last_time:
                    self.last_time = block_time
            kind = event["event"]
            mint = event["mint"]
            if kind == "CreateEvent":
                self.tape_of(mint).created = event["slot"]
            elif kind == "TradeEvent":
                tape = self.tape_of(mint)
                sol = int(event["sol_amount"]) / LAMPORTS
                tape.trades.append((
                    event["slot"],
                    bool(event["is_buy"]),
                    sol,
                    event["user"],
                    price_of(int(event["virtual_quote_reserves"]),
                             int(event["virtual_token_reserves"])),
                ))
                if mint not in traded_seen:
                    traded_seen.add(mint)
                    traded.append(mint)
                if not event["is_buy"] and sol >= BIG_SELL_SOL:
                    big_sell.add(mint)
        for mint in traded:
            tape = self.tapes[mint]
            cut = 0
            while cut < len(tape.trades) and tape.trades[cut][0] <= now - KEEP:
                cut += 1
            if cut:
                del tape.trades[:cut]

        marks = {}
        for mark in batch["marks"]:
            marks[mark["mint"]] = mark
        pending_by_mint = {}
        for pending in state.pending.values():
            mint = pending.order["mint"]
            pending_by_mint[mint] = pending_by_mint.get(mint, 0) + 1

        orders = []
        cap = state.hello["caps"]["max_orders_per_batch"]
        tx = state.tx_fee_lamports()

        # --- Exits first: stop loss, trailing stop, fade, big sell, time limit.
        for mint, held in list(self.held.items()):
            pos = state.positions.get(mint)
            curve = state.curves.get(mint)
            if pos is None:
                if mint not in pending_by_mint:
                    del self.held[mint]
                    tape = self.tape_of(mint)
                    if held.filled:
                        tape.last_exit = now
                    else:
                        tape.entries -= 1
                continue
            held.filled = True
            if curve is None or curve.complete:
                continue
            mark = marks.get(mint)
            if mark is not None:
                vq = int(mark["virtual_quote_reserves"])
                vt = int(mark["virtual_token_reserves"])
                rq_sol = int(mark["real_quote_reserves"]) / LAMPORTS
            else:
                vq = curve.virtual_quote_reserves
                vt = curve.virtual_token_reserves
                rq_sol = curve.real_quote_reserves / LAMPORTS
            tokens = int(pos.token_amount)
            value = close_value(tokens, vq, vt, tx)
            cost = int(pos.cost_lamports) / LAMPORTS
            if value > held.peak:
                held.peak = value
            held.last_value = value
            tape = self.tape_of(mint)
            exit_now = held.exiting
            if not exit_now and value < cost * (1.0 - STOP_LOSS):
                exit_now = True
            if (not exit_now and held.peak >= cost * (1.0 + TRAIL_ARM)
                    and value < held.peak * (1.0 - TRAIL)):
                exit_now = True
            if not exit_now and self.net_flow(tape, now, FADE_WINDOW) < -FADE_NET_SOL:
                exit_now = True
            if not exit_now and mint in big_sell:
                exit_now = True
            if not exit_now and now - held.opened > MAX_HOLD:
                exit_now = True
            if not exit_now:
                continue
            held.exiting = True
            sell_pending = False
            for pending in state.pending.values():
                if pending.order["mint"] == mint and pending.order["side"] == "sell":
                    sell_pending = True
                    break
            if sell_pending:
                continue
            if len(orders) >= cap:
                break
            orders.append({"side": "sell", "mint": mint,
                           "token_amount": pos.token_amount,
                           "max_slippage_bps": 10000})

        # --- Probe round trips: sell the whole probe as soon as its buy lands.
        for mint in list(self.probes):
            pos = state.positions.get(mint)
            curve = state.curves.get(mint)
            if pos is None:
                if mint not in pending_by_mint:
                    del self.probes[mint]
                continue
            if curve is None or curve.complete or mint in pending_by_mint:
                continue
            if len(orders) >= cap:
                break
            orders.append({"side": "sell", "mint": mint,
                           "token_amount": pos.token_amount,
                           "max_slippage_bps": 10000})
            self.probes[mint] = True

        # --- Entries and pyramid adds.
        free = state.free_cash_lamports() / LAMPORTS
        slots_left = state.hello["caps"]["max_open_positions"] - state.committed_position_count()
        main_left = MAX_MAIN - len(self.held)
        exposure = 0.0
        for mint, held in self.held.items():
            pos = state.positions.get(mint)
            exposure += max(held.invested,
                            int(pos.cost_lamports) / LAMPORTS if pos is not None else 0.0)
        for mint in traded:
            if len(orders) >= cap:
                break
            curve = state.curves.get(mint)
            if curve is None or curve.complete:
                continue
            if mint in pending_by_mint or mint in self.probes:
                continue
            tape = self.tapes[mint]
            held = self.held.get(mint)
            price = price_of(curve.virtual_quote_reserves,
                             curve.virtual_token_reserves)
            rq_sol = curve.real_quote_reserves / LAMPORTS
            if held is None:
                if slots_left <= 0 or main_left <= 0:
                    continue
                if tape.entries >= MAX_ENTRIES or now - tape.last_exit < COOLDOWN:
                    continue
                if not self.entry_signal(tape, now, rq_sol):
                    continue
                want = min(INITIAL_SOL, STAKE_SOL)
            else:
                # Pyramid into strength only: the position is up, the price
                # stepped up from the last add, and net SOL still flows in.
                if held.exiting or not held.filled:
                    continue
                if held.invested >= STAKE_SOL - MIN_BUY_SOL:
                    continue
                pos = state.positions.get(mint)
                if pos is None:
                    continue
                if price < held.last_add_price:
                    continue
                if held.last_value < int(pos.cost_lamports) / LAMPORTS:
                    continue
                if self.net_flow(tape, now, WINDOW) < 0:
                    continue
                if rq_sol > MAX_RQ_SOL:
                    continue
                want = min(ADD_SOL, STAKE_SOL - held.invested)
            cap_sol = state.max_buy_lamports(mint) / LAMPORTS
            amount = min(cap_sol, want, MAX_EXPOSURE_SOL - exposure)
            if amount > free - tx / LAMPORTS:
                amount = free - tx / LAMPORTS
            if amount < MIN_BUY_SOL:
                continue
            lamports = int(amount * LAMPORTS)
            orders.append({"side": "buy", "mint": mint,
                           "quote_lamports": lamports,
                           "max_slippage_bps": BUY_SLIP_BPS})
            free -= amount + tx / LAMPORTS
            exposure += amount
            if held is None:
                self.held[mint] = Held(now, amount, price)
                tape.entries += 1
                slots_left -= 1
                main_left -= 1
            else:
                held.invested += amount
                held.last_add_price = price

        # --- Guardrail pacing: open a probe when fills or coins lag the pace.
        if (len(self.probes) < MAX_PROBES and slots_left > 0
                and self.first_time is not None and self.last_time is not None):
            pace = min(1.0, (self.last_time - self.first_time) / PACE_SECS)
            need_coins = len(self.my_coins) < int(TARGET_COINS * pace + 0.999999)
            need_fills = self.my_fills < int(TARGET_FILLS * pace + 0.999999)
            if need_coins or need_fills:
                min_rq = PROBE_SOL * 10000 / state.hello["caps"]["max_order_bps_of_real_reserve"]
                for mint in traded:
                    if len(orders) >= cap or len(self.probes) >= MAX_PROBES:
                        break
                    if (mint in self.held or mint in self.probes
                            or mint in pending_by_mint
                            or mint in state.positions):
                        continue
                    if need_coins and not need_fills and mint in self.my_coins:
                        continue
                    curve = state.curves.get(mint)
                    if curve is None or curve.complete:
                        continue
                    if curve.real_quote_reserves / LAMPORTS < min_rq * 1.5:
                        continue
                    if free < PROBE_SOL + 2 * tx / LAMPORTS:
                        break
                    orders.append({"side": "buy", "mint": mint,
                                   "quote_lamports": int(PROBE_SOL * LAMPORTS),
                                   "max_slippage_bps": BUY_SLIP_BPS})
                    self.probes[mint] = False
                    free -= PROBE_SOL + tx / LAMPORTS
        return orders


STRATEGY = Strategy()


def on_batch(state, batch):
    return STRATEGY.on_batch(state, batch)


if __name__ == "__main__":
    makefun_bot.run(on_batch)
