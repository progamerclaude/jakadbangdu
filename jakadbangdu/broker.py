"""Dhan is the only broker/data source. DhanData = market data (always real);
PaperExecutor simulates fills; DhanExecutor places real orders (TRADING_MODE=live)."""
import re
import threading
import time
import warnings
from dataclasses import dataclass

import pandas as pd

from .config import Settings


@dataclass(frozen=True)
class Instrument:
    symbol: str
    security_id: str
    segment: str = "NSE_EQ"


class _RateLimiter:
    """Blocks so that calls are at least `gap_s` seconds apart (thread-safe)."""

    def __init__(self, gap_s: float):
        self.gap, self.last, self.lock = gap_s, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            d = self.last + self.gap - time.monotonic()
            if d > 0:
                time.sleep(d)
            self.last = time.monotonic()


def _ok(resp, what: str):
    if not isinstance(resp, dict) or resp.get("status") != "success":
        raise RuntimeError(f"Dhan {what} failed: {resp}")
    return resp["data"]


def _unwrap(d):  # SDK sometimes nests the payload under a second "data"
    return d["data"] if isinstance(d, dict) and "data" in d and isinstance(d["data"], dict) else d


def _lookup(m: pd.DataFrame, query: str) -> Instrument:
    q = query.strip().upper()
    eq = m[(m["SEM_EXM_EXCH_ID"] == "NSE") & (m["SEM_INSTRUMENT_NAME"] == "EQUITY")]
    if "SEM_SERIES" in eq.columns and (eq["SEM_SERIES"].astype(str).str.upper() == "EQ").any():
        eq = eq[eq["SEM_SERIES"].astype(str).str.upper() == "EQ"]
    col = lambda c: eq[c].astype(str).str.upper().str.strip() if c in eq.columns else pd.Series("", index=eq.index)
    for hit in (eq[col("SEM_TRADING_SYMBOL") == q], eq[col("SEM_CUSTOM_SYMBOL") == q],
                eq[col("SM_SYMBOL_NAME").str.contains(re.escape(q)) | col("SEM_CUSTOM_SYMBOL").str.contains(re.escape(q))]):
        if len(hit) == 1:
            r = hit.iloc[0]
            return Instrument(str(r["SEM_TRADING_SYMBOL"]), str(int(r["SEM_SMST_SECURITY_ID"])))
        if len(hit) > 1:
            names = ", ".join(sorted(set(hit["SEM_TRADING_SYMBOL"].astype(str))))[:200]
            raise KeyError(f"'{query}' matches several NSE stocks ({names}); use the exact trading symbol")
    raise KeyError(f"'{query}' not found on NSE equity (tried trading symbol, display name, company name)")


class DhanData:
    NIFTY = Instrument("NIFTY 50", "13", "IDX_I")
    BANKNIFTY = Instrument("BANK NIFTY", "25", "IDX_I")

    def __init__(self, s: Settings):
        from dhanhq import DhanContext, dhanhq
        if not (s.dhan_client_id and s.dhan_access_token):
            raise RuntimeError("Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN")
        self.dhan = dhanhq(DhanContext(s.dhan_client_id, s.dhan_access_token))
        self._data_rl = _RateLimiter(s.data_request_interval_s)   # history: one request per interval (default 20s)
        self._quote_rl = _RateLimiter(1.0)                        # quotes: Dhan allows 1 request/sec
        self._master: pd.DataFrame | None = None
        self._cache: dict[str, Instrument] = {}

    # ---- instruments ------------------------------------------------------
    def resolve(self, symbol: str) -> Instrument:
        """NSE cash equity by trading symbol; failing that by Dhan display name, then by company name (must be unique)."""
        if symbol in self._cache:
            return self._cache[symbol]
        if self._master is None:
            from dhanhq import dhanhq
            with warnings.catch_warnings():   # pandas DtypeWarning from Dhan's own CSV: harmless
                warnings.simplefilter("ignore")
                self._master = dhanhq.fetch_security_list("compact")
        inst = _lookup(self._master, symbol)
        self._cache[symbol] = inst
        return inst

    # ---- candles ----------------------------------------------------------
    def _frame(self, d: dict) -> pd.DataFrame:
        df = pd.DataFrame({k: d[k] for k in ("open", "high", "low", "close", "volume")})
        df.index = pd.to_datetime([self.dhan.convert_to_date_time(t) for t in d["timestamp"]])
        return df.astype(float)

    def daily(self, inst: Instrument, days: int = 300) -> pd.DataFrame:
        self._data_rl.wait()
        end = pd.Timestamp.now().normalize() + pd.Timedelta(days=1)
        instrument_type = "INDEX" if inst.segment == "IDX_I" else "EQUITY"
        r = self.dhan.historical_daily_data(inst.security_id, inst.segment, instrument_type,
                                            (end - pd.Timedelta(days=days)).strftime("%Y-%m-%d"),
                                            end.strftime("%Y-%m-%d"))
        return self._frame(_ok(r, "historical_daily_data"))

    def intraday(self, inst: Instrument, interval: int = 15, days: int = 5) -> pd.DataFrame:
        self._data_rl.wait()
        end = pd.Timestamp.now()
        instrument_type = "INDEX" if inst.segment == "IDX_I" else "EQUITY"
        r = self.dhan.intraday_minute_data(inst.security_id, inst.segment, instrument_type,
                                           (end - pd.Timedelta(days=days)).strftime("%Y-%m-%d 09:15:00"),
                                           end.strftime("%Y-%m-%d %H:%M:%S"), interval)
        return self._frame(_ok(r, "intraday_minute_data"))

    # ---- quotes -----------------------------------------------------------
    def quotes(self, insts: list[Instrument]) -> dict[str, dict]:
        """symbol -> {ltp, open, high, low, prev_close}. One call, 1 req/sec."""
        req: dict[str, list[int]] = {}
        for i in insts:
            req.setdefault(i.segment, []).append(int(i.security_id))
        self._quote_rl.wait()
        data = _unwrap(_ok(self.dhan.ohlc_data(req), "ohlc_data"))
        out = {}
        for i in insts:
            q = data.get(i.segment, {}).get(i.security_id)
            if q:
                o = q.get("ohlc", {})
                out[i.symbol] = {"ltp": float(q["last_price"]), "open": o.get("open"), "high": o.get("high"),
                                 "low": o.get("low"), "prev_close": o.get("close")}
        return out

    def ltp(self, inst: Instrument) -> float:
        return self.quotes([inst])[inst.symbol]["ltp"]


class PaperExecutor:
    """Simulated fills at the given price. No orders reach the exchange."""
    name = "paper"

    def __init__(self):
        self._n = 0

    def buy(self, inst: Instrument, qty: int, price: float) -> tuple[float, str]:
        self._n += 1
        return price, f"PAPER-{self._n}"

    def protective_sl(self, inst: Instrument, qty: int, sl: float) -> str | None:
        return None

    def cancel(self, order_id: str | None):
        pass

    def sell(self, inst: Instrument, qty: int, price: float) -> float:
        return price


class DhanExecutor:
    """Real CNC delivery orders. The protective stop lives on the exchange side
    (STOP_LOSS_MARKET) so a bot outage cannot leave a position unprotected.
    Target exits are monitored by the bot. Needs static IP whitelisted on Dhan."""
    name = "live"

    def __init__(self, data: DhanData):
        from dhanhq import dhanhq
        self.d, self.api = data.dhan, dhanhq

    def _place(self, inst, side, qty, otype, price, trigger=0, tag="jakad"):
        r = self.d.place_order(security_id=inst.security_id, exchange_segment=self.api.NSE, transaction_type=side,
                               quantity=qty, order_type=otype, product_type=self.api.CNC, price=price,
                               trigger_price=trigger, validity=self.api.DAY, tag=tag)
        return _ok(r, "place_order")["orderId"]

    def _wait_fill(self, order_id: str, timeout_s=60) -> float:
        end = time.time() + timeout_s
        while time.time() < end:
            o = _ok(self.d.get_order_by_id(order_id), "get_order_by_id")
            o = o[0] if isinstance(o, list) else o
            if o.get("orderStatus") == "TRADED":
                return float(o.get("averageTradedPrice") or o.get("price"))
            if o.get("orderStatus") in ("REJECTED", "CANCELLED", "EXPIRED"):
                raise RuntimeError(f"order {order_id} {o['orderStatus']}: {o.get('omsErrorDescription')}")
            time.sleep(2)
        self.d.cancel_order(order_id)
        raise RuntimeError(f"order {order_id} not filled in {timeout_s}s; cancelled")

    def buy(self, inst, qty, price):
        oid = self._place(inst, self.api.BUY, qty, self.api.LIMIT, price, tag="jakad_entry")
        return self._wait_fill(oid), oid

    def protective_sl(self, inst, qty, sl):
        limit = round(sl * 0.995, 2)  # SL-limit with 0.5% band (SL-M is converted by Dhan anyway)
        return self._place(inst, self.api.SELL, qty, self.api.SL, limit, trigger=sl, tag="jakad_sl")

    def cancel(self, order_id):
        if order_id:
            self.d.cancel_order(order_id)

    def sell(self, inst, qty, price):
        oid = self._place(inst, self.api.SELL, qty, self.api.LIMIT, round(price * 0.998, 2), tag="jakad_exit")
        return self._wait_fill(oid)
