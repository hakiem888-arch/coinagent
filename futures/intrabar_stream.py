"""Public Futures WebSocket + REST Fallback Intrabar Stream Manager (CoinAgentX v6.0.5 P0-02).

Read-only public market data ONLY (Binance USD-M Futures Primary -> Bybit USDT Linear Secondary).
NEVER places orders, NEVER mixes Spot Reference into Futures execution streams, and strictly
separates running intrabar candles (`is_closed=False`) from closed historical candles (`is_closed=True`).

Honest Connection State Rule (P0-02):
- `WEBSOCKET_CONNECTED` is reported ONLY after BOTH a real WebSocket connection succeeds AND
  at least one valid market message has been received (`messages_received >= 1`).
- If WebSocket is unavailable, disconnected, or waiting without live traffic, `REST_FALLBACK`
  (or `CONNECTED_AWAITING_MSG` / `RECONNECTING`) is reported honestly — NEVER `WEBSOCKET_CONNECTED`.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Iterable, Optional

import pandas as pd

from .models import INTERVAL_MS, MONITOR_TIMEFRAMES, IntrabarCandle, MonitorSnapshot

BINANCE_FUTURES_WS_URL = "wss://fstream.binance.com/stream"
BYBIT_LINEAR_WS_URL = "wss://stream.bybit.com/v5/public/linear"
MAX_STREAM_SUBSCRIPTIONS = 50


def validate_ohlcv_dataframe(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate OHLCV integrity, drop invalid/corrupted rows, deduplicate by open_time, and sort ascending."""
    if frame is None or len(frame) == 0:
        return pd.DataFrame(columns=["open_time", "open", "high", "low", "close", "volume", "close_time"])
    df = frame.copy()
    req = ("open_time", "open", "high", "low", "close", "volume", "close_time")
    for col in req:
        if col not in df.columns:
            raise ValueError(f"Kolom OHLCV wajib tidak ditemukan: {col}")
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=list(req))
    valid_mask = (
        (df["open"] > 0)
        & (df["high"] > 0)
        & (df["low"] > 0)
        & (df["close"] > 0)
        & (df["volume"] >= 0)
        & (df["high"] >= df[["open", "close"]].max(axis=1))
        & (df["low"] <= df[["open", "close"]].min(axis=1))
        & (df["close_time"] > df["open_time"])
    )
    df = df.loc[valid_mask].copy()
    if df.empty:
        return df.reset_index(drop=True)
    df["open_time"] = df["open_time"].astype("int64")
    df["close_time"] = df["close_time"].astype("int64")
    df = df.sort_values("open_time").drop_duplicates(subset=["open_time"], keep="last").reset_index(drop=True)
    return df


def parse_binance_kline_message(payload: dict, receive_ms: Optional[int] = None) -> Optional[IntrabarCandle]:
    """Parse a Binance USD-M Futures combined-stream kline event into an IntrabarCandle."""
    data = payload.get("data", payload) if isinstance(payload, dict) else None
    if not isinstance(data, dict) or data.get("e") != "kline":
        return None
    k = data.get("k")
    if not isinstance(k, dict):
        return None
    tf = str(k.get("i") or "")
    if tf not in INTERVAL_MS:
        return None
    rx_ms = int(receive_ms if receive_ms is not None else time.time() * 1000)
    ex_ms = int(data.get("E") or k.get("T") or rx_ms)
    candle = IntrabarCandle(
        symbol=str(k.get("s") or data.get("s") or "").upper(),
        timeframe=tf,
        open_time=int(k["t"]),
        close_time=int(k["T"]),
        open=float(k["o"]),
        high=float(k["h"]),
        low=float(k["l"]),
        close=float(k["c"]),
        volume=float(k["v"]),
        is_closed=bool(k.get("x", False)),
        provider="Binance Futures",
        instrument_kind="USD_M_PERPETUAL",
        exchange_timestamp_ms=ex_ms,
        local_receive_ms=rx_ms,
        data_freshness_ms=max(0, rx_ms - ex_ms),
    )
    return candle if candle.is_valid_ohlc() else None


def parse_bybit_kline_message(payload: dict, receive_ms: Optional[int] = None) -> list[IntrabarCandle]:
    """Parse a Bybit v5 public linear kline message into IntrabarCandle list."""
    if not isinstance(payload, dict):
        return []
    topic = str(payload.get("topic") or "")
    if not topic.startswith("kline."):
        return []
    parts = topic.split(".")
    if len(parts) < 3:
        return []
    tf_map = {"1": "1m", "5": "5m", "15": "15m", "60": "1h", "240": "4h", "D": "1d"}
    tf = tf_map.get(parts[1])
    symbol = parts[2].upper()
    if not tf:
        return []
    rx_ms = int(receive_ms if receive_ms is not None else time.time() * 1000)
    ex_ms = int(payload.get("ts") or rx_ms)
    out = []
    for row in payload.get("data") or []:
        try:
            candle = IntrabarCandle(
                symbol=symbol,
                timeframe=tf,
                open_time=int(row["start"]),
                close_time=int(row["end"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                is_closed=bool(row.get("confirm", False)),
                provider="Bybit USDT Perpetual",
                instrument_kind="BYBIT_LINEAR_PERPETUAL",
                exchange_timestamp_ms=int(row.get("timestamp") or ex_ms),
                local_receive_ms=rx_ms,
                data_freshness_ms=max(0, rx_ms - int(row.get("timestamp") or ex_ms)),
            )
            if candle.is_valid_ohlc():
                out.append(candle)
        except (KeyError, ValueError, TypeError):
            continue
    return out


# ---------------------------------------------------------------------------
# P0-02: Real Public Futures WebSocket Connectors (Binance USD-M & Bybit Linear)
# ---------------------------------------------------------------------------
class BaseFuturesWSConnector:
    """Real WebSocket connector base with dynamic subscribe/unsubscribe, ping heartbeat, and clean shutdown."""

    provider_name: str = "Binance Futures"
    ws_url: str = BINANCE_FUTURES_WS_URL

    def __init__(
        self,
        *,
        on_message: Optional[Callable[[str | dict, str], None]] = None,
        on_disconnect: Optional[Callable[[str], None]] = None,
        transport_factory: Optional[Callable[[str], Any]] = None,
        ping_interval_s: float = 20.0,
    ):
        self.on_message = on_message
        self.on_disconnect = on_disconnect
        self._transport_factory = transport_factory
        self.ping_interval_s = max(5.0, float(ping_interval_s))
        self.connected = False
        self.subscribed_streams: set[str] = set()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws_obj: Any = None
        self._req_id = 0

    def build_subscribe_payload(self, symbols: Iterable[str], timeframes: tuple[str, ...]) -> dict:
        raise NotImplementedError

    def build_unsubscribe_payload(self, symbols: Iterable[str], timeframes: tuple[str, ...]) -> dict:
        raise NotImplementedError

    def build_ping_payload(self) -> Optional[dict]:
        return None

    def connect(self, symbols: Iterable[str], timeframes: tuple[str, ...] = MONITOR_TIMEFRAMES) -> bool:
        """Open a real WebSocket connection (uses `websockets` package or injected `transport_factory`)."""
        sym_list = [str(s).strip().upper() for s in symbols if s]
        sub_payload = self.build_subscribe_payload(sym_list, timeframes)

        if self._transport_factory is not None:
            ws = self._transport_factory(self.ws_url)
            self._ws_obj = ws
            if hasattr(ws, "send"):
                ws.send(json.dumps(sub_payload))
            self.connected = True
            # Drain any immediately available frames from transport (useful for deterministic tests)
            if hasattr(ws, "recv_available"):
                for raw_msg in ws.recv_available():
                    if self.on_message:
                        self.on_message(raw_msg, self.provider_name)
            return True

        try:
            import websockets  # type: ignore
        except ImportError as exc:
            self.connected = False
            raise RuntimeError("Paket `websockets` belum terpasang; menggunakan REST_FALLBACK.") from exc

        # Launch background asyncio listener thread for real network WebSocket I/O
        ready_evt = threading.Event()
        err_box: list[BaseException] = []
        self._stop_event.clear()

        async def _runner():
            try:
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=self.ping_interval_s,
                    ping_timeout=10.0,
                    close_timeout=5.0,
                ) as ws:
                    self._ws_obj = ws
                    if sym_list:
                        await ws.send(json.dumps(sub_payload))
                    self.connected = True
                    ready_evt.set()
                    last_ping = time.monotonic()
                    while not self._stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                            if self.on_message:
                                self.on_message(raw, self.provider_name)
                        except asyncio.TimeoutError:
                            ping_msg = self.build_ping_payload()
                            if ping_msg and (time.monotonic() - last_ping) >= self.ping_interval_s:
                                await ws.send(json.dumps(ping_msg))
                                last_ping = time.monotonic()
            except Exception as exc:
                self.connected = False
                if not ready_evt.is_set():
                    err_box.append(exc)
                    ready_evt.set()
                elif self.on_disconnect:
                    self.on_disconnect(f"{type(exc).__name__}: {exc}")

        self._thread = threading.Thread(target=lambda: asyncio.run(_runner()), name=f"ws-{self.provider_name}", daemon=True)
        self._thread.start()
        ready_evt.wait(timeout=8.0)
        if err_box:
            raise RuntimeError(f"WebSocket {self.provider_name} gagal terhubung: {err_box[0]}") from err_box[0]
        if not self.connected:
            raise RuntimeError(f"WebSocket {self.provider_name} timeout saat handshake.")
        return True

    def send_dynamic_subscribe(self, symbols: Iterable[str], timeframes: tuple[str, ...] = MONITOR_TIMEFRAMES) -> dict:
        payload = self.build_subscribe_payload(symbols, timeframes)
        if self._ws_obj is not None and hasattr(self._ws_obj, "send") and not asyncio.iscoroutinefunction(self._ws_obj.send):
            self._ws_obj.send(json.dumps(payload))
        return payload

    def send_dynamic_unsubscribe(self, symbols: Iterable[str], timeframes: tuple[str, ...] = MONITOR_TIMEFRAMES) -> dict:
        payload = self.build_unsubscribe_payload(symbols, timeframes)
        if self._ws_obj is not None and hasattr(self._ws_obj, "send") and not asyncio.iscoroutinefunction(self._ws_obj.send):
            self._ws_obj.send(json.dumps(payload))
        return payload

    def close(self) -> None:
        self._stop_event.set()
        self.connected = False
        if self._ws_obj is not None and hasattr(self._ws_obj, "close") and not asyncio.iscoroutinefunction(self._ws_obj.close):
            try:
                self._ws_obj.close()
            except Exception:
                pass


class BinanceFuturesWSConnector(BaseFuturesWSConnector):
    """Real Binance USD-M Futures Public WebSocket (`wss://fstream.binance.com/stream`)."""

    provider_name = "Binance Futures"
    ws_url = BINANCE_FUTURES_WS_URL

    def build_subscribe_payload(self, symbols: Iterable[str], timeframes: tuple[str, ...]) -> dict:
        self._req_id += 1
        params = [f"{str(s).strip().lower()}@kline_{tf}" for s in symbols if s for tf in timeframes]
        self.subscribed_streams.update(params)
        return {"method": "SUBSCRIBE", "params": params, "id": self._req_id}

    def build_unsubscribe_payload(self, symbols: Iterable[str], timeframes: tuple[str, ...]) -> dict:
        self._req_id += 1
        params = [f"{str(s).strip().lower()}@kline_{tf}" for s in symbols if s for tf in timeframes]
        self.subscribed_streams.difference_update(params)
        return {"method": "UNSUBSCRIBE", "params": params, "id": self._req_id}


class BybitLinearWSConnector(BaseFuturesWSConnector):
    """Real Bybit USDT Linear Perpetual Public WebSocket (`wss://stream.bybit.com/v5/public/linear`)."""

    provider_name = "Bybit USDT Perpetual"
    ws_url = BYBIT_LINEAR_WS_URL
    _TF_MAP = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "D"}

    def build_subscribe_payload(self, symbols: Iterable[str], timeframes: tuple[str, ...]) -> dict:
        args = [f"kline.{self._TF_MAP[tf]}.{str(s).strip().upper()}" for s in symbols if s for tf in timeframes if tf in self._TF_MAP]
        self.subscribed_streams.update(args)
        return {"op": "subscribe", "args": args}

    def build_unsubscribe_payload(self, symbols: Iterable[str], timeframes: tuple[str, ...]) -> dict:
        args = [f"kline.{self._TF_MAP[tf]}.{str(s).strip().upper()}" for s in symbols if s for tf in timeframes if tf in self._TF_MAP]
        self.subscribed_streams.difference_update(args)
        return {"op": "unsubscribe", "args": args}

    def build_ping_payload(self) -> Optional[dict]:
        return {"op": "ping"}


def create_default_ws_connector(
    provider: str,
    *,
    on_message: Optional[Callable] = None,
    on_disconnect: Optional[Callable] = None,
    transport_factory: Optional[Callable] = None,
) -> BaseFuturesWSConnector:
    if "bybit" in str(provider or "").lower():
        return BybitLinearWSConnector(on_message=on_message, on_disconnect=on_disconnect, transport_factory=transport_factory)
    return BinanceFuturesWSConnector(on_message=on_message, on_disconnect=on_disconnect, transport_factory=transport_factory)


class IntrabarStreamManager:
    """Manages public M1/M5/M15 WebSocket streams with real Binance/Bybit connectors,
    exponential backoff reconnect, stale feed detection, bounded subscriptions (<=50),
    provider failover, and honest REST fallback.
    """

    def __init__(
        self,
        market_client,
        *,
        websocket_enabled: bool = True,
        rest_fallback_enabled: bool = True,
        max_subscriptions: int = MAX_STREAM_SUBSCRIPTIONS,
        stale_threshold_seconds: float = 30.0,
        monitor_interval_seconds: float = 10.0,
        backoff_base_seconds: float = 1.0,
        backoff_max_seconds: float = 60.0,
        max_rest_requests_per_cycle: int = 120,
        ws_connector: Optional[Callable] = None,
        transport_factory: Optional[Callable] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.market = market_client
        self.websocket_enabled = bool(websocket_enabled)
        self.rest_fallback_enabled = bool(rest_fallback_enabled)
        self.max_subscriptions = max(1, min(MAX_STREAM_SUBSCRIPTIONS, int(max_subscriptions)))
        self.stale_threshold_ms = int(max(5.0, float(stale_threshold_seconds)) * 1000)
        self.monitor_interval_seconds = max(5.0, float(monitor_interval_seconds))
        self.backoff_base_seconds = max(0.25, float(backoff_base_seconds))
        self.backoff_max_seconds = max(2.0, float(backoff_max_seconds))
        self.max_rest_requests_per_cycle = max(10, int(max_rest_requests_per_cycle))
        self._ws_connector = ws_connector
        self._transport_factory = transport_factory
        self._active_connector: Optional[BaseFuturesWSConnector] = None
        self.clock = clock

        self._lock = threading.RLock()
        self._subscriptions: OrderedDict[str, tuple[str, ...]] = OrderedDict()
        self._live_candles: dict[tuple[str, str], IntrabarCandle] = {}
        self._closed_buffer: dict[tuple[str, str], OrderedDict[int, IntrabarCandle]] = {}
        self._latest_snapshots: dict[str, MonitorSnapshot] = {}

        # Connection / health state (P0-02 honest status)
        self.ws_state = "DISCONNECTED"
        self.ws_socket_open = False
        self.ws_messages_received: int = 0
        self.active_provider = getattr(market_client, "market_label", "Binance Futures")
        self.active_instrument_kind = getattr(market_client, "instrument_kind", "USD_M_PERPETUAL")
        self.failover_reason: Optional[str] = None
        self.last_message_ms: int = 0
        self.last_heartbeat_ms: int = 0
        self.connection_attempts: int = 0
        self.reconnect_attempts: int = 0
        self.reconnect_count: int = 0
        self.next_reconnect_at: float = 0.0
        self.rest_fallback_active: bool = False
        self.rest_poll_count: int = 0
        self.rest_last_success_ms: int = 0
        self.rest_requests_last_cycle: int = 0
        self.last_connect_error: str = ""
        self.last_error: str = ""

    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    def subscribe(self, symbols: Iterable[str], timeframes: tuple[str, ...] = MONITOR_TIMEFRAMES) -> list[str]:
        tfs = tuple(tf for tf in timeframes if tf in INTERVAL_MS) or MONITOR_TIMEFRAMES
        added = []
        with self._lock:
            for raw in symbols:
                sym = str(raw or "").strip().upper()
                if not sym:
                    continue
                if sym not in self._subscriptions:
                    added.append(sym)
                self._subscriptions[sym] = tfs
                self._subscriptions.move_to_end(sym)
                while len(self._subscriptions) > self.max_subscriptions:
                    evicted, _ = self._subscriptions.popitem(last=False)
                    self._evict_symbol_state(evicted)
            if added and self._active_connector is not None and self._active_connector.connected:
                self._active_connector.send_dynamic_subscribe(added, tfs)
            return list(self._subscriptions.keys())

    def unsubscribe(self, symbols: Iterable[str]) -> list[str]:
        removed = []
        with self._lock:
            for raw in symbols:
                sym = str(raw or "").strip().upper()
                if sym in self._subscriptions:
                    removed.append(sym)
                self._subscriptions.pop(sym, None)
                self._evict_symbol_state(sym)
            if removed and self._active_connector is not None and self._active_connector.connected:
                self._active_connector.send_dynamic_unsubscribe(removed, MONITOR_TIMEFRAMES)
            return list(self._subscriptions.keys())

    def _evict_symbol_state(self, symbol: str) -> None:
        self._latest_snapshots.pop(symbol, None)
        for key in [k for k in self._live_candles if k[0] == symbol]:
            self._live_candles.pop(key, None)
        for key in [k for k in self._closed_buffer if k[0] == symbol]:
            self._closed_buffer.pop(key, None)

    def subscribed_symbols(self) -> list[str]:
        with self._lock:
            return list(self._subscriptions.keys())

    def compute_backoff_delay(self, attempt: int) -> float:
        exp = min(10, max(0, int(attempt)))
        return min(self.backoff_max_seconds, self.backoff_base_seconds * (2 ** exp))

    def ingest_candle(self, candle: IntrabarCandle, *, from_ws: bool = False) -> bool:
        if candle is None or not candle.is_valid_ohlc():
            return False
        with self._lock:
            existing = self._live_candles.get((candle.symbol, candle.timeframe))
            if existing is not None and existing.provider != candle.provider:
                self._evict_symbol_state(candle.symbol)
            rx_ms = self.now_ms()
            candle.local_receive_ms = rx_ms
            candle.data_freshness_ms = max(0, rx_ms - int(candle.exchange_timestamp_ms or rx_ms))
            candle.is_stale = candle.data_freshness_ms > self.stale_threshold_ms
            self.last_message_ms = rx_ms
            self.active_provider = candle.provider
            self.active_instrument_kind = candle.instrument_kind
            if from_ws:
                self.ws_messages_received += 1
                if self.ws_socket_open and self.ws_messages_received >= 1:
                    self.ws_state = "WEBSOCKET_CONNECTED"
                    self.rest_fallback_active = False

            key = (candle.symbol, candle.timeframe)
            if candle.is_closed:
                buf = self._closed_buffer.setdefault(key, OrderedDict())
                if candle.open_time not in buf:
                    buf[candle.open_time] = candle
                    while len(buf) > 300:
                        buf.popitem(last=False)
                live = self._live_candles.get(key)
                if live is not None and live.open_time == candle.open_time:
                    self._live_candles.pop(key, None)
            else:
                self._live_candles[key] = candle
            return True

    def ingest_raw_ws_message(self, raw_message: str | dict, provider_hint: Optional[str] = None) -> int:
        try:
            payload = json.loads(raw_message) if isinstance(raw_message, str) else raw_message
        except (ValueError, TypeError):
            return 0
        if not isinstance(payload, dict):
            return 0
        rx_ms = self.now_ms()
        count = 0
        is_bybit = (provider_hint and "bybit" in str(provider_hint).lower()) or str(payload.get("topic", "")).startswith("kline.")
        if is_bybit:
            for c in parse_bybit_kline_message(payload, receive_ms=rx_ms):
                if self.ingest_candle(c, from_ws=True):
                    count += 1
        else:
            c = parse_binance_kline_message(payload, receive_ms=rx_ms)
            if c is not None and self.ingest_candle(c, from_ws=True):
                count += 1
        return count

    def connect_websocket(self) -> bool:
        """Open real Binance/Bybit WebSocket connection.
        NEVER reports `WEBSOCKET_CONNECTED` until at least one valid message is received!
        Falls back honestly to `REST_FALLBACK` on error or missing transport.
        """
        with self._lock:
            if not self.websocket_enabled:
                self.ws_state = "REST_FALLBACK" if self.rest_fallback_enabled else "DISCONNECTED"
                self.ws_socket_open = False
                self.rest_fallback_active = self.rest_fallback_enabled
                return False
            now_s = self.clock()
            if now_s < self.next_reconnect_at:
                return False

            self.connection_attempts += 1
            self.ws_state = "CONNECTING"
            self.ws_socket_open = False
            self.ws_messages_received = 0
            try:
                if self._ws_connector is not None:
                    self._ws_connector(self.active_provider, list(self._subscriptions.keys()))
                    self.ws_socket_open = True
                else:
                    conn = create_default_ws_connector(
                        self.active_provider,
                        on_message=lambda raw, prov: self.ingest_raw_ws_message(raw, provider_hint=prov),
                        on_disconnect=lambda reason: self.handle_disconnect(reason),
                        transport_factory=self._transport_factory,
                    )
                    self._active_connector = conn
                    conn.connect(list(self._subscriptions.keys()), MONITOR_TIMEFRAMES)
                    self.ws_socket_open = bool(conn.connected)

                if self.ws_socket_open:
                    self.reconnect_attempts = 0
                    self.last_heartbeat_ms = self.now_ms()
                    self.last_error = ""
                    # P0-02: Only report WEBSOCKET_CONNECTED if a live message has ALREADY arrived!
                    if self.ws_messages_received >= 1:
                        self.ws_state = "WEBSOCKET_CONNECTED"
                        self.rest_fallback_active = False
                    else:
                        self.ws_state = "CONNECTED_AWAITING_MSG"
                    return True
                raise RuntimeError("WebSocket handshake selesai tanpa socket aktif.")
            except Exception as exc:
                self.ws_socket_open = False
                self.reconnect_attempts += 1
                self.reconnect_count += 1
                delay = self.compute_backoff_delay(self.reconnect_attempts - 1)
                self.next_reconnect_at = now_s + delay
                self.last_connect_error = str(exc)[:180]
                self.last_error = str(exc)[:180]
                if self.rest_fallback_enabled:
                    self.ws_state = "REST_FALLBACK"
                    self.rest_fallback_active = True
                else:
                    self.ws_state = "RECONNECTING"
                return False

    def switch_provider(self, new_provider: str, reason: str = "failover") -> None:
        """Failover stream cleanly from Binance USD-M Futures to Bybit USDT Linear."""
        with self._lock:
            if self._active_connector is not None:
                self._active_connector.close()
                self._active_connector = None
            self.ws_socket_open = False
            self.ws_messages_received = 0
            self.active_provider = str(new_provider)
            self.active_instrument_kind = "BYBIT_LINEAR_PERPETUAL" if "bybit" in self.active_provider.lower() else "USD_M_PERPETUAL"
            self.failover_reason = str(reason)
            self._live_candles.clear()
            self.ws_state = "RECONNECTING"
            self.next_reconnect_at = 0.0

    def handle_disconnect(self, reason: str = "connection_lost") -> float:
        with self._lock:
            self.ws_socket_open = False
            self.ws_messages_received = 0
            self.reconnect_attempts += 1
            self.reconnect_count += 1
            delay = self.compute_backoff_delay(self.reconnect_attempts - 1)
            self.next_reconnect_at = self.clock() + delay
            self.last_error = str(reason)[:180]
            if self.rest_fallback_enabled:
                self.ws_state = "REST_FALLBACK"
                self.rest_fallback_active = True
            else:
                self.ws_state = "RECONNECTING"
            return delay

    def is_feed_stale(self, now_ms: Optional[int] = None) -> bool:
        now = int(now_ms if now_ms is not None else self.now_ms())
        if self.last_message_ms <= 0:
            return True
        return (now - self.last_message_ms) > self.stale_threshold_ms

    def poll_rest_cycle(
        self,
        symbols: Optional[Iterable[str]] = None,
        *,
        intervals: tuple[str, ...] = MONITOR_TIMEFRAMES,
    ) -> dict[str, MonitorSnapshot]:
        if not self.rest_fallback_enabled:
            return {}
        target_symbols = list(symbols) if symbols is not None else self.subscribed_symbols()
        target_symbols = target_symbols[: self.max_subscriptions]
        results: dict[str, MonitorSnapshot] = {}
        req_count = 0
        for sym in target_symbols:
            if req_count >= self.max_rest_requests_per_cycle:
                break
            if not hasattr(self.market, "monitor_snapshot"):
                continue
            try:
                snap = self.market.monitor_snapshot(
                    sym,
                    intervals=intervals,
                    stale_threshold_ms=self.stale_threshold_ms,
                )
                req_count += len(intervals) + 1
                if snap is not None:
                    with self._lock:
                        self._latest_snapshots[sym] = snap
                        self.last_message_ms = int(snap.local_receive_ms or self.now_ms())
                        self.active_provider = snap.provider
                        self.active_instrument_kind = snap.instrument_kind
                        self.failover_reason = snap.failover_reason
                        for tf, c in (snap.current_candles or {}).items():
                            if isinstance(c, IntrabarCandle):
                                self.ingest_candle(c, from_ws=False)
                    results[sym] = snap
            except Exception as exc:
                self.last_error = str(exc)[:180]
                if getattr(exc, "access_blocked", False):
                    break
        with self._lock:
            self.rest_requests_last_cycle = req_count
            self.rest_poll_count += 1
            if results:
                self.rest_last_success_ms = self.now_ms()
            self.rest_fallback_active = True
            if self.ws_state != "WEBSOCKET_CONNECTED":
                self.ws_state = "REST_FALLBACK"
        return results


    def aggregate_m1_to_higher_tf(self, symbol: str) -> dict[str, IntrabarCandle]:
        """P0-03: Aggregate available M1 live/closed candles into M5/M15 running candles if M5/M15 stream lags."""
        sym = str(symbol or "").strip().upper()
        out = {}
        with self._lock:
            m1_bars = list(self._closed_buffer.get((sym, "1m"), {}).values())
            live_m1 = self._live_candles.get((sym, "1m"))
            if live_m1 is not None:
                m1_bars.append(live_m1)
            if not m1_bars:
                return out
            m1_bars.sort(key=lambda c: c.open_time)
            latest = m1_bars[-1]
            for tf in ("5m", "15m"):
                if (sym, tf) in self._live_candles:
                    out[tf] = self._live_candles[(sym, tf)]
                    continue
                step = INTERVAL_MS[tf]
                b_open = (latest.open_time // step) * step
                b_close = b_open + step - 1
                grp = [c for c in m1_bars if b_open <= c.open_time <= b_close]
                if grp:
                    agg = IntrabarCandle(
                        symbol=sym,
                        timeframe=tf,
                        open_time=b_open,
                        close_time=b_close,
                        open=grp[0].open,
                        high=max(c.high for c in grp),
                        low=min(c.low for c in grp),
                        close=grp[-1].close,
                        volume=sum(c.volume for c in grp),
                        is_closed=False,
                        provider=latest.provider,
                        instrument_kind=latest.instrument_kind,
                        exchange_timestamp_ms=latest.exchange_timestamp_ms,
                        local_receive_ms=latest.local_receive_ms,
                        data_freshness_ms=latest.data_freshness_ms,
                        is_stale=latest.is_stale,
                    )
                    self._live_candles[(sym, tf)] = agg
                    out[tf] = agg
        return out

    def get_live_candle(self, symbol: str, timeframe: str) -> Optional[IntrabarCandle]:
        with self._lock:
            c = self._live_candles.get((str(symbol).upper(), str(timeframe)))
            if c is None:
                return None
            c.data_freshness_ms = max(0, self.now_ms() - int(c.exchange_timestamp_ms or c.local_receive_ms))
            c.is_stale = c.data_freshness_ms > self.stale_threshold_ms
            return c

    def get_snapshot(self, symbol: str) -> Optional[MonitorSnapshot]:
        with self._lock:
            return self._latest_snapshots.get(str(symbol).upper())

    def health_status(self) -> dict:
        with self._lock:
            now = self.now_ms()
            stale_syms = []
            avail = {tf: 0 for tf in MONITOR_TIMEFRAMES}
            for (sym, tf), c in self._live_candles.items():
                age = max(0, now - int(c.exchange_timestamp_ms or c.local_receive_ms))
                if age > self.stale_threshold_ms:
                    if sym not in stale_syms:
                        stale_syms.append(sym)
                elif tf in avail:
                    avail[tf] += 1
            eff_state = (
                "WEBSOCKET_CONNECTED"
                if (self.ws_socket_open and self.ws_messages_received >= 1 and not self.rest_fallback_active)
                else ("REST_FALLBACK" if self.rest_fallback_active else self.ws_state)
            )
            total_candles = sum(avail.values()) + sum(len(b) for b in self._closed_buffer.values())
            feed_active = bool(total_candles > 0 and self.last_message_ms > 0 and not self.is_feed_stale(now))
            return {
                "websocket_state": eff_state,
                "feed_active": feed_active,
                "connection_attempts": int(self.connection_attempts),
                "last_connect_error": self.last_connect_error,
                "rest_poll_count": int(self.rest_poll_count),
                "rest_last_success_ms": int(self.rest_last_success_ms),
                "last_successful_update_ms": max(int(self.last_message_ms), int(self.rest_last_success_ms)),
                "per_timeframe_candle_counts": dict(avail),
                "ws_socket_open": bool(self.ws_socket_open),
                "ws_messages_received": int(self.ws_messages_received),
                "rest_fallback_active": bool(self.rest_fallback_active),
                "active_provider": self.active_provider,
                "instrument_kind": self.active_instrument_kind,
                "failover_reason": self.failover_reason,
                "last_message_ms": self.last_message_ms,
                "feed_freshness_ms": max(0, now - self.last_message_ms) if self.last_message_ms > 0 else None,
                "feed_stale": self.is_feed_stale(now),
                "stale_symbols": stale_syms,
                "active_subscriptions": len(self._subscriptions),
                "max_subscriptions": self.max_subscriptions,
                "reconnect_count": self.reconnect_count,
                "rest_requests_last_cycle": self.rest_requests_last_cycle,
                "m1_m5_m15_availability": avail,
                "last_error": self.last_error,
            }
