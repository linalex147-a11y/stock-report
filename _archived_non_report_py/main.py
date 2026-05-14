# ============================================================
#  main.py — 永豐 API 主程式
# ============================================================

from __future__ import annotations

import time
import schedule
import requests
import pandas as pd
import threading

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime
from collections import defaultdict
from typing import Optional, Dict

import shioaji as sj
from shioaji.constant import Exchange

from config import 設定
from strategy import 計算訊號, add_indicators
from data_loader import get_kbars


# ============================================================
# 全域狀態
# ============================================================

# key = "{symbol}_多" / "{symbol}_空"
# value = {
#     "狀態":     "WAIT_BREAK" / "WATCH_PULLBACK" / "READY_ENTRY" / "FAILED" / "EXIT"
#     "回踩次數": int
#     "大量high": float
#     "大量low":  float
# }
追蹤狀態: Dict[str, dict] = {}

_tick_buf: Dict[str, list] = defaultdict(list)
_kbar_5m:  Dict[str, pd.DataFrame] = {}
_buf_lock  = threading.Lock()
_scan_locks: Dict[str, threading.Lock] = defaultdict(threading.Lock)
_last_bar: Dict[str, Optional[pd.Timestamp]] = {}

_BAR = 設定.TG分隔線


# ============================================================
# 工具函式
# ============================================================

def _now(fmt: str = "%H:%M:%S") -> str:
    return datetime.now().strftime(fmt)


def _now_full() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def 交易時間內() -> bool:
    t     = datetime.now().time()
    start = dtime(*map(int, 設定.交易開始時間.split(":")))
    end   = dtime(*map(int, 設定.交易結束時間.split(":")))
    return start <= t <= end


# ============================================================
# Telegram
# ============================================================

def 傳TG(msg: str) -> None:

    url = (
        "https://api.telegram.org/bot"
        + 設定.TG_TOKEN
        + "/sendMessage"
    )

    try:
        r = requests.post(
            url,
            data={
                "chat_id":    設定.TG_CHAT_ID,
                "text":       msg,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        if r.status_code != 200:
            print("[TG 錯誤]", r.text)

    except Exception as e:
        print("[TG 例外]", e)


# ============================================================
# 訊息格式
# ============================================================

_標題EMOJI = {
    "多方觀察":     "👀",
    "多方進場":     "📡",
    "多方回踩失敗": "⚠️",
    "多方退場":     "🚨",
    "空方觀察":     "👀",
    "空方進場":     "📡",
    "空方反彈失敗": "⚠️",
    "空方退場":     "🚨",
}


def _趨勢強度文字(sig: dict) -> str:
    if sig["方向"] == "多":
        return (
            "📶 強勢排列（MA5 > MA10 > MA20）"
            if sig["趨勢強度"]
            else "📉 MA 排列尚未完整"
        )
    else:
        return (
            "📶 弱勢排列（MA5 < MA10 < MA20）"
            if sig["趨勢強度"]
            else "📈 MA 排列尚未完整"
        )


def _反向提示(方向: str) -> str:
    """EXIT 時加註反向觀察提示"""
    if 方向 == "多":
        return "↩️ REVERSAL WATCH：留意空方機會"
    else:
        return "↩️ REVERSAL WATCH：留意多方機會"


def _組訊息(symbol: str, sig: dict, price: float) -> str:

    名稱       = 設定.監控標的[symbol]
    類型       = sig["類型"]
    方向箭頭   = "🔺" if sig["方向"] == "多" else "🔻"
    標題emoji  = _標題EMOJI.get(類型, "📌")

    # EXIT 時附加反向提示行
    反向行 = (
        ["", _反向提示(sig["方向"])]
        if 類型 in ("多方退場", "空方退場")
        else []
    )

    return "\n".join([
        f"{標題emoji} <b>{方向箭頭} {symbol} {名稱}｜{類型}</b>",
        _BAR,
        f"現價：{price}",
        f"大量區高點：{sig['大量high']}",
        f"大量區低點：{sig['大量low']}",
        "",
        f"理由：{sig['理由']}",
        "",
        _趨勢強度文字(sig),
        *反向行,
        _BAR,
        f"MA5={sig['MA5']}  MA10={sig['MA10']}  MA20={sig['MA20']}",
        f"回踩次數：{sig['回踩次數']}／{設定.最大回踩次數}",
        "",
        "🕐 " + _now_full(),
    ])


# ============================================================
# K棒時間
# ============================================================

def _bar_label(ts: datetime) -> pd.Timestamp:
    m = (ts.minute // 設定.K棒週期) * 設定.K棒週期
    return pd.Timestamp(ts.year, ts.month, ts.day, ts.hour, m, 0)


# ============================================================
# Flush Ready Bars
# ============================================================

def _flush_ready_bars(force: bool = False) -> None:

    current_bar = _bar_label(datetime.now())
    pending = []

    for symbol, last_bar in list(_last_bar.items()):
        if last_bar is None:
            continue
        if force or last_bar < current_bar:
            pending.append((symbol, last_bar))

    for symbol, bar_time in pending:
        _flush_bar(symbol, bar_time)


# ============================================================
# 封棒（同步）
# ============================================================

def _flush_bar(symbol: str, bar_time: pd.Timestamp) -> None:

    try:
        with _buf_lock:

            ticks = [
                t for t in _tick_buf[symbol]
                if t["bar"] == bar_time
            ]

            if not ticks:
                return

            prices  = [t["price"]  for t in ticks]
            volumes = [t["volume"] for t in ticks]

            row = pd.DataFrame([{
                "open":   prices[0],
                "high":   max(prices),
                "low":    min(prices),
                "close":  prices[-1],
                "volume": sum(volumes),
            }], index=[bar_time])

            if symbol not in _kbar_5m:
                _kbar_5m[symbol] = row
            else:
                merged = pd.concat([_kbar_5m[symbol], row])
                merged = merged[~merged.index.duplicated(keep='last')]
                merged = merged.sort_index()
                merged = merged.iloc[-設定.K棒最大保留根數:]
                _kbar_5m[symbol] = merged

            _tick_buf[symbol] = [
                t for t in _tick_buf[symbol]
                if t["bar"] != bar_time
            ]

        print(f"[{_now()}] 封棒：{symbol} {bar_time.strftime('%H:%M')}")

    except Exception as e:
        print(f"[{_now()}] 封棒失敗 {symbol}：{e}")
        return

    # 封棒後直接同步掃描，不 spawn thread
    _掃描單一標的(symbol)


# ============================================================
# 永豐登入
# ============================================================

def 登入() -> sj.Shioaji:

    api = sj.Shioaji(simulation=False)
    api.login(
        api_key=設定.永豐API_KEY,
        secret_key=設定.永豐SECRET_KEY,
    )
    print(f"[{_now()}] 永豐 API 登入成功")
    return api


# ============================================================
# 補歷史K棒（使用 data_loader 統一管理）
# ============================================================

def _補單一標的歷史K棒(api, symbol: str, start: str, end: str) -> None:
    """
    單一標的補歷史K棒，含重試機制
    失敗超過 重試次數 後發 TG 警告
    """

    for attempt in range(1, 設定.重試次數 + 1):
        try:
            df = get_kbars(api, symbol, start=start, end=end)

            if df is not None and len(df) > 0:
                df = df.sort_index()
                df = df[~df.index.duplicated(keep='last')]
                _kbar_5m[symbol] = df
                print(f"[{_now()}] {symbol} 補入 {len(df)} 根")
                return

            else:
                print(f"[{_now()}] {symbol} 回傳空資料（第 {attempt} 次）")

        except Exception as e:
            print(f"[{_now()}] {symbol} 補K棒失敗（第 {attempt} 次）：{e}")

        if attempt < 設定.重試次數:
            print(f"[{_now()}] {symbol} {設定.重試間隔秒} 秒後重試...")
            time.sleep(設定.重試間隔秒)

    # 超過重試上限
    msg = f"⚠️ {symbol} 補歷史K棒失敗，已重試 {設定.重試次數} 次，今日可能無法監控"
    print(f"[{_now()}] {msg}")
    傳TG(msg)


def 補歷史K棒(api) -> None:

    from datetime import timedelta

    end   = datetime.now().strftime("%Y-%m-%d")
    start = (
        datetime.now() - timedelta(days=設定.補歷史天數)
    ).strftime("%Y-%m-%d")

    print(f"[{_now()}] 補歷史K棒中（{start} ～ {end}）...")

    for symbol in 設定.監控標的:
        _補單一標的歷史K棒(api, symbol, start, end)

    print(f"[{_now()}] 歷史K棒補完")


# ============================================================
# Tick 訂閱
# ============================================================

def 訂閱Tick(api: sj.Shioaji) -> None:

    @api.on_tick_stk_v1()
    def on_tick(exchange: Exchange, tick):

        if not 交易時間內():
            return

        symbol = tick.code

        if symbol not in 設定.監控標的:
            return

        ts    = tick.datetime
        bar   = _bar_label(ts)
        price = float(tick.close)
        vol   = int(tick.volume)

        with _buf_lock:
            _tick_buf[symbol].append({
                "price":  price,
                "volume": vol,
                "bar":    bar,
            })

        prev = _last_bar.get(symbol)

        if prev is not None and bar != prev:
            _flush_bar(symbol, prev)

        _last_bar[symbol] = bar

    for symbol in 設定.監控標的:
        contract = api.Contracts.Stocks[symbol]
        api.quote.subscribe(
            contract,
            quote_type=sj.constant.QuoteType.Tick,
            version=sj.constant.QuoteVersion.v1,
        )
        print(f"[{_now()}] 訂閱 Tick：{symbol}")


# ============================================================
# 最新成交價
# ============================================================

def 最新成交價(symbol: str) -> Optional[float]:

    with _buf_lock:
        buf = list(_tick_buf.get(symbol, []))

    if buf:
        return buf[-1]["price"]

    df = _kbar_5m.get(symbol)

    if df is not None and len(df) > 0:
        return float(df["close"].iloc[-1])

    return None


# ============================================================
# 單一標的掃描
# ============================================================

def _掃描單一標的(symbol: str) -> None:

    lock = _scan_locks[symbol]

    if not lock.acquire(blocking=False):
        print(f"[{_now()}] {symbol} 掃描中，略過")
        return

    try:
        df = _kbar_5m.get(symbol)

        if df is None or len(df) < 設定.最少K棒緩衝:
            return

        df = df.sort_index()
        df = df[~df.index.duplicated(keep='last')]

        enriched = add_indicators(df.copy())

        no_entry_cut = dtime(*map(int, 設定.停止進場時間.split(":")))
        已過停止時間 = datetime.now().time() >= no_entry_cut

        for 方向 in ["多", "空"]:

            state_key = f"{symbol}_{方向}"
            state     = 追蹤狀態.get(state_key, {})
            當前狀態  = state.get("狀態", None)
            回踩次數  = state.get("回踩次數", 0)
            回踩中    = state.get("回踩中", False)
            鎖定high  = state.get("鎖定high", None)
            鎖定low   = state.get("鎖定low",  None)
            突破K棒idx = state.get("突破K棒idx", None)
            回踩K棒idx = state.get("回踩K棒idx", None)

            # FAILED / EXIT 才是真正終態，不再掃描
            # READY_ENTRY 仍需監控 EXIT
            if 當前狀態 in ("FAILED", "EXIT"):
                continue

            sig = 計算訊號(
                enriched,
                symbol,
                當前狀態=當前狀態,
                回踩次數=回踩次數,
                回踩中=回踩中,
                鎖定high=鎖定high,
                鎖定low=鎖定low,
                方向=方向,
                突破K棒idx=突破K棒idx,
                回踩K棒idx=回踩K棒idx,
            )

            if sig is None:
                continue

            新狀態 = sig["狀態"]
            類型   = sig["類型"]

            # 回踩計數 / 回踩結束：只更新狀態，不發 TG
            if 類型 in ("回踩計數", "回踩結束"):
                追蹤狀態[state_key] = {
                    **state,
                    "回踩次數":   sig["回踩次數"],
                    "回踩中":     sig["回踩中"],
                    "鎖定high":   sig.get("鎖定high", 鎖定high),
                    "鎖定low":    sig.get("鎖定low",  鎖定low),
                    "突破K棒idx": sig.get("突破K棒idx", 突破K棒idx),
                    "回踩K棒idx": sig.get("回踩K棒idx", 回踩K棒idx),
                }
                print(f"[{_now()}] {symbol} {方向}方 {sig['理由']}")
                continue

            # 停止進場時間後，觀察訊號不發（退場 / 失敗仍發）
            if 已過停止時間 and 類型 in ("多方觀察", "空方觀察"):
                continue

            price = 最新成交價(symbol)

            if price is None:
                continue

            傳TG(_組訊息(symbol, sig, price))

            print(
                f"[{_now()}] [{類型}] {symbol} "
                f"狀態：{當前狀態} → {新狀態}"
            )

            # 更新追蹤狀態
            追蹤狀態[state_key] = {
                "狀態":       新狀態,
                "回踩次數":   sig["回踩次數"],
                "回踩中":     sig.get("回踩中", False),
                "鎖定high":   sig.get("鎖定high", 鎖定high),
                "鎖定low":    sig.get("鎖定low",  鎖定low),
                "突破K棒idx": sig.get("突破K棒idx", 突破K棒idx),
                "回踩K棒idx": sig.get("回踩K棒idx", 回踩K棒idx),
                "當日已進場": False,
            }

    finally:
        lock.release()


# ============================================================
# 主掃描
# ============================================================

def 掃描() -> None:

    if not 交易時間內():
        return

    _flush_ready_bars()

    # 線程池並行掃描，最多同時跑 最大掃描線程數 個 symbol
    with ThreadPoolExecutor(max_workers=設定.最大掃描線程數) as executor:
        executor.map(_掃描單一標的, 設定.監控標的.keys())


# ============================================================
# 收盤通知
# ============================================================

def 收盤通知() -> None:

    _flush_ready_bars(force=True)

    傳TG("📊 收盤，今日監控結束")


    追蹤狀態.clear()        # 唯一清除點

    with _buf_lock:
        _tick_buf.clear()
        _kbar_5m.clear()

    print(f"[{_now()}] 收盤重置")


# ============================================================
# 主程式
# ============================================================

def main():

    print("=" * 50)
    print("永豐 大量區突破掃描器")
    print("=" * 50)

    api = 登入()
    補歷史K棒(api)
    訂閱Tick(api)

    schedule.every(設定.掃描間隔秒).seconds.do(掃描)
    schedule.every().day.at(設定.收盤通知時間).do(收盤通知)

    print(f"[{_now()}] 開始監控")

    try:
        while True:
            schedule.run_pending()
            time.sleep(1)

    except KeyboardInterrupt:
        print(f"\n[{_now()}] 手動中斷")
        api.logout()


if __name__ == "__main__":
    main()
