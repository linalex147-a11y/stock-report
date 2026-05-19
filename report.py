# ============================================================
# report.py
# AI盤後結構分析器
#
# 目標：
#   - 比單純「close > MA20」更接近人眼盤感
#   - 同時看：
#       1) 日K / 30分K / 5分K 結構
#       2) MA位置 + MA斜率 + 均線排列
#       3) MACD / KD 動能
#       4) 大量K / 平台 / 前高前低 / 日MA20
#   - 保留原本報表輸出、Telegram 發送、Git 自動提交
#
# 欄位：
#   股票　現價　交易狀態　結構型態　日K方向　30分K方向
#   前高（3日）　大量K高點　30分平台高
#   前低（3日）　大量K低點　30分平台低　日MA20
#   日MACD　30分MACD　日KD　30分KD　明日備註
# ============================================================

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any, List

import numpy as np
import pandas as pd
import requests
import shioaji as sj

from config import 設定
from report_config import 報表設定
from data_loader import get_kbars


# ==========================================================
# 工具
# ==========================================================

def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _股票池() -> dict:
    return dict(報表設定.報表標的)


def _safe_round(v, n: int = 2):
    try:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return None
        return round(float(v), n)
    except Exception:
        return None


def _pct(a: float, b: float) -> Optional[float]:
    """回傳 (a-b)/a * 100"""
    try:
        if a is None or b is None or a == 0:
            return None
        return round((a - b) / a * 100, 2)
    except Exception:
        return None


def _pct_up(a: float, b: float) -> Optional[float]:
    """回傳 (a-b)/a * 100，語意上常用於現價距離壓力"""
    return _pct(a, b)


def _pct_down(a: float, b: float) -> Optional[float]:
    """回傳 (a-b)/a * 100，語意上常用於現價距離支撐"""
    try:
        if a is None or b is None or a == 0:
            return None
        return round((a - b) / a * 100, 2)
    except Exception:
        return None


def _fmt_levels(items) -> str:
    out = []
    for name, price in items:
        if price is None or price == "-":
            continue
        out.append(f"{name}{_safe_round(price, 2)}")
    return "、".join(out) if out else "-"


def _trend_to_text(score: int, direction_hint: Optional[str] = None) -> str:
    if score >= 4:
        return "強多"
    if score >= 2:
        return "偏多"
    if score <= -4:
        return "強空"
    if score <= -2:
        return "偏空"
    return "盤整"


# ==========================================================
# API / 資料載入
# ==========================================================

def 登入() -> sj.Shioaji:
    api = sj.Shioaji(simulation=False)
    api.login(
        api_key=設定.永豐API_KEY,
        secret_key=設定.永豐SECRET_KEY,
    )
    return api


def _載入分K(api, symbol: str) -> Optional[pd.DataFrame]:
    """
    載入最近 N 天的 5分K
    """
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=報表設定.回看天數)).strftime("%Y-%m-%d")

    try:
        df = get_kbars(api, symbol, start=start, end=end)
        if df is None or len(df) == 0:
            return None
        df = df.sort_index()
        # 保留資料乾淨
        df = df[["open", "high", "low", "close", "volume"]].copy()
        return df
    except Exception as e:
        print(f"  ⚠️ {symbol} 載入失敗：{e}")
        return None


def _resample_30min(df5: pd.DataFrame) -> pd.DataFrame:
    """5分K → 30分K"""
    return df5.resample("30min", closed="left", label="left").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()


def _resample_day(df5: pd.DataFrame) -> pd.DataFrame:
    """5分K → 日K"""
    return df5.resample("D", closed="left", label="left").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()


# ==========================================================
# 指標
# ==========================================================

def _add_basic_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["MA5"] = df["close"].rolling(5, min_periods=1).mean()
    df["MA10"] = df["close"].rolling(10, min_periods=1).mean()
    df["MA20"] = df["close"].rolling(20, min_periods=1).mean()
    df["MA60"] = df["close"].rolling(60, min_periods=1).mean()
    return df


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _add_macd(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    ema12 = _ema(df["close"], 12)
    ema26 = _ema(df["close"], 26)
    df["DIF"] = ema12 - ema26
    df["DEA"] = _ema(df["DIF"], 9)
    df["OSC"] = df["DIF"] - df["DEA"]
    return df


def _add_kdj(df: pd.DataFrame, n: int = 9) -> pd.DataFrame:
    df = df.copy()
    low_n = df["low"].rolling(n, min_periods=1).min()
    high_n = df["high"].rolling(n, min_periods=1).max()
    rsv = (df["close"] - low_n) / (high_n - low_n).replace(0, np.nan) * 100
    rsv = rsv.fillna(50.0)

    k = pd.Series(index=df.index, dtype=float)
    d = pd.Series(index=df.index, dtype=float)

    for i, idx in enumerate(df.index):
        if i == 0:
            k.iloc[i] = 50.0
            d.iloc[i] = 50.0
        else:
            k.iloc[i] = k.iloc[i - 1] * 2 / 3 + rsv.iloc[i] * 1 / 3
            d.iloc[i] = d.iloc[i - 1] * 2 / 3 + k.iloc[i] * 1 / 3

    df["K"] = k.clip(0, 100)
    df["D"] = d.clip(0, 100)
    df["J"] = (3 * df["K"] - 2 * df["D"]).clip(-50, 150)
    return df


def _add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = _add_basic_indicators(df)
    df = _add_macd(df)
    df = _add_kdj(df)
    return df


def _slope(series: pd.Series, lookback: int = 3) -> Optional[float]:
    try:
        if len(series) <= lookback:
            return None
        a = float(series.iloc[-1])
        b = float(series.iloc[-1 - lookback])
        return a - b
    except Exception:
        return None


# ==========================================================
# 結構判斷
# ==========================================================

def _方向(df: pd.DataFrame) -> str:
    """
    單純方向：
    收盤 > MA20 且 MA20 上彎 => 多
    收盤 < MA20 且 MA20 下彎 => 空
    否則維持為中性偏向
    """
    df = _add_all_indicators(df)
    last = df.iloc[-1]
    close = float(last["close"])
    ma20 = float(last["MA20"])
    ma20_slope = _slope(df["MA20"], 3) or 0.0

    if close > ma20 and ma20_slope > 0:
        return "多"
    if close < ma20 and ma20_slope < 0:
        return "空"

    # 若斜率不明顯，仍以價格位置為主
    return "多" if close >= ma20 else "空"


def _結構分數(df: pd.DataFrame) -> int:
    """
    結構分數：
    正分偏多，負分偏空
    """
    if df is None or len(df) < 5:
        return 0

    df = _add_all_indicators(df)
    last = df.iloc[-1]

    close = float(last["close"])
    ma5 = float(last["MA5"])
    ma10 = float(last["MA10"])
    ma20 = float(last["MA20"])
    ma60 = float(last["MA60"]) if "MA60" in df.columns else ma20
    dif = float(last["DIF"])
    dea = float(last["DEA"])
    osc = float(last["OSC"])
    k = float(last["K"])
    d = float(last["D"])
    j = float(last["J"])

    s = 0

    # 均線排列 / 位置
    if close > ma5 > ma10 > ma20:
        s += 3
    elif close > ma20:
        s += 1

    if close < ma5 < ma10 < ma20:
        s -= 3
    elif close < ma20:
        s -= 1

    # 中期均線斜率
    ma20_slope = _slope(df["MA20"], 3) or 0.0
    ma60_slope = _slope(df["MA60"], 3) or 0.0
    if ma20_slope > 0:
        s += 1
    elif ma20_slope < 0:
        s -= 1

    if ma60_slope > 0:
        s += 1
    elif ma60_slope < 0:
        s -= 1

    # MACD
    if dif > dea:
        s += 1
    else:
        s -= 1

    if osc > 0:
        s += 1
    else:
        s -= 1

    # KD
    if k > d:
        s += 1
    else:
        s -= 1

    if k > 80 and j > 90:
        s -= 1  # 高檔鈍化偏壓力
    if k < 20 and j < 10:
        s += 1  # 低檔鈍化偏支撐

    return int(s)


def _綜合狀態(日K方向: str, 分K方向: str, 日分數: int, 分30分數: int) -> str:
    """
    以結構分數為主，方向為輔
    """
    # 強勢多
    if 日分數 >= 4 and 分30分數 >= 2:
        return "主升多"
    # 主跌空
    if 日分數 <= -4 and 分30分數 <= -2:
        return "主跌空"
    # 日多30空：回檔
    if 日K方向 == "多" and 分K方向 == "空":
        return "多方回檔"
    # 日空30多：反彈
    if 日K方向 == "空" and 分K方向 == "多":
        return "空方反彈"
    # 單邊偏多
    if 日分數 >= 2 or 分30分數 >= 2:
        return "偏多"
    # 單邊偏空
    if 日分數 <= -2 or 分30分數 <= -2:
        return "偏空"
    return "盤整"


# ==========================================================
# 壓力 / 支撐
# ==========================================================

def _前高前低(df_day: pd.DataFrame, 天數: int = 3):
    recent = df_day.iloc[-天數:]
    return _safe_round(recent["high"].max(), 2), _safe_round(recent["low"].min(), 2)


def _30分平台高低(df30: pd.DataFrame, 回看根數: int = 6):
    recent = df30.iloc[-回看根數:]
    return _safe_round(recent["high"].max(), 2), _safe_round(recent["low"].min(), 2)


def _日MA20(df_day: pd.DataFrame) -> Optional[float]:
    df = _add_basic_indicators(df_day)
    return _safe_round(df.iloc[-1]["MA20"], 2)


def _日MA60(df_day: pd.DataFrame) -> Optional[float]:
    df = _add_basic_indicators(df_day)
    return _safe_round(df.iloc[-1]["MA60"], 2)


def _大量K_high_low_報表(df5: pd.DataFrame, 方向: str) -> Tuple[Optional[float], Optional[float]]:
    """
    報表專用大量K搜尋：
    - 包含最後一根（盤後已封棒）
    - 只在報表中使用，不影響即時策略
    """
    df = _add_basic_indicators(df5.copy())

    回看 = 報表設定.大量區回看根數
    均線根數 = 報表設定.大量K量能均線根數
    window = df.iloc[-回看:]

    if len(window) < 均線根數 + 1:
        return None, None

    最佳分數 = None
    最佳row = None

    for i in range(len(window)):
        if i < 均線根數:
            continue

        row = window.iloc[i]
        vol_ma = float(window["volume"].iloc[i - 均線根數:i].mean())
        if vol_ma <= 0:
            continue

        close = float(row["close"])
        open_ = float(row["open"])
        vol = float(row["volume"])
        if close <= 0 or open_ <= 0:
            continue

        # 大量K條件
        if vol < vol_ma * 報表設定.大量K量能倍率:
            continue

        # 多方大量K：實體陽K；空方大量K：實體陰K
        if 方向 == "多" and close <= open_:
            continue
        if 方向 == "空" and close >= open_:
            continue

        body_ratio = abs(close - open_) / open_
        if body_ratio < 報表設定.大量K實體最小比例:
            continue

        high = float(row["high"])
        low = float(row["low"])
        range_ratio = (high - low) / close if close > 0 else 0.0
        vol_ratio = vol / vol_ma

        # 量 > 體 > range > 絕對量
        分數 = (vol_ratio, body_ratio, range_ratio, vol)
        if 最佳分數 is None or 分數 > 最佳分數:
            最佳分數 = 分數
            最佳row = row

    if 最佳row is None:
        return None, None

    return _safe_round(最佳row["high"], 2), _safe_round(最佳row["low"], 2)


def _重要價位分析(
    現價: float,
    前高,
    大量high,
    平台高,
    前低,
    大量low,
    平台低,
    日ma20,
    日ma60,
) -> Dict[str, Any]:
    """
    將各種價位整理成「壓力/支撐/中繼」與最近距離。
    """
    壓力候選 = [v for v in [前高, 大量high, 平台高, 日ma60] if v is not None]
    支撐候選 = [v for v in [前低, 大量low, 平台低, 日ma20] if v is not None]

    價位集 = []

    for name, price in [
        ("前高", 前高),
        ("大量K高", 大量high),
        ("30分平台高", 平台高),
        ("日MA60", 日ma60),
        ("前低", 前低),
        ("大量K低", 大量low),
        ("30分平台低", 平台低),
        ("日MA20", 日ma20),
    ]:
        if price is None:
            continue
        dist = round((price - 現價) / 現價 * 100, 2)
        價位集.append((name, price, dist))

    壓力 = sorted(
        [(name, price, dist) for name, price, dist in 價位集 if price > 現價],
        key=lambda x: x[1]
    )
    支撐 = sorted(
        [(name, price, dist) for name, price, dist in 價位集 if price <= 現價],
        key=lambda x: x[1],
        reverse=True
    )

    最近壓力 = 壓力[0] if 壓力 else None
    最近支撐 = 支撐[0] if 支撐 else None

    return {
        "壓力文字": _fmt_levels([
            ("前高", 前高),
            ("大量K高", 大量high),
            ("30分平台高", 平台高),
            ("日MA60", 日ma60),
        ]),
        "支撐文字": _fmt_levels([
            ("前低", 前低),
            ("大量K低", 大量low),
            ("30分平台低", 平台低),
            ("日MA20", 日ma20),
        ]),
        "最近壓力": 最近壓力,
        "最近支撐": 最近支撐,
        "壓力候選": 壓力候選,
        "支撐候選": 支撐候選,
    }


# ==========================================================
# 結構劇本
# ==========================================================

def _明日備註(
    狀態: str,
    日K方向: str,
    分K方向30: str,
    日分數: int,
    分30分數: int,
    前高,
    大量high,
    平台高,
    前低,
    大量low,
    平台低,
    日ma20,
    日ma60,
    現價: float,
) -> str:
    價位 = _重要價位分析(現價, 前高, 大量high, 平台高, 前低, 大量low, 平台低, 日ma20, 日ma60)
    壓力 = 價位["壓力文字"]
    支撐 = 價位["支撐文字"]

    # 主升 / 主跌
    if 狀態 == "主升多":
        劇本 = "主升結構，回檔先看支撐，站穩再看續攻"
    elif 狀態 == "主跌空":
        劇本 = "主跌結構，反彈先看壓力，跌破支撐才有延續"
    elif 狀態 == "多方回檔":
        劇本 = "日多30空，偏多回檔，先看支撐是否守住"
    elif 狀態 == "空方反彈":
        劇本 = "日空30多，偏空反彈，先看壓力是否過得去"
    elif 狀態 == "偏多":
        劇本 = "偏多，但還不是主升，等突破壓力才會更漂亮"
    elif 狀態 == "偏空":
        劇本 = "偏空，但還不是主跌，等跌破支撐才會更乾淨"
    else:
        劇本 = "盤整，先看區間上下緣，沒有突破前不要預設方向"

    return f"{劇本}｜壓力：{壓力}｜支撐：{支撐}"


# ==========================================================
# 交易狀態
# ==========================================================

def _交易狀態(
    狀態: str,
    現價: float,
    前高,
    大量high,
    平台高,
    前低,
    大量low,
    平台低,
    日ma20,
    日ma60,
    日分數: int,
    分30分數: int,
) -> str:
    """
    更接近人工盤感的狀態文字：
      - 主升多 / 主跌空
      - 多方回檔 / 空方反彈
      - 區間整理
    """
    價位 = _重要價位分析(現價, 前高, 大量high, 平台高, 前低, 大量low, 平台低, 日ma20, 日ma60)
    最近壓力 = 價位["最近壓力"]
    最近支撐 = 價位["最近支撐"]

    # 距離關鍵價位是否很近（%）
    close_to_res = False
    close_to_sup = False

    if 最近壓力 is not None and 最近壓力[2] is not None:
        close_to_res = 最近壓力[1] > 現價 and 最近壓力[2] <= 1.0

    if 最近支撐 is not None and 最近支撐[2] is not None:
        close_to_sup = 最近支撐[1] <= 現價 and abs(最近支撐[2]) <= 1.0

    if 狀態 == "主升多":
        if close_to_res:
            return "🔥 主升多｜等突破"
        if close_to_sup:
            return "⭕ 主升多｜回檔守支撐"
        return "⭕ 主升多｜續抱觀察"

    if 狀態 == "主跌空":
        if close_to_sup:
            return "⚠️ 主跌空｜不追空"
        if close_to_res:
            return "👀 主跌空｜等反彈空"
        return "🔥 主跌空｜等跌破"

    if 狀態 == "多方回檔":
        if close_to_sup:
            return "⚠️ 多方回檔｜看支撐"
        if close_to_res:
            return "👀 多方回檔｜等壓力過"
        return "⭕ 多方回檔｜等止穩"

    if 狀態 == "空方反彈":
        if close_to_res:
            return "👀 空方反彈｜等壓力空"
        if close_to_sup:
            return "⚠️ 空方反彈｜靠近支撐"
        return "⭕ 空方反彈｜等反彈結束"

    if 狀態 == "偏多":
        if close_to_res:
            return "🔥 偏多｜等突破"
        return "⭕ 偏多｜可觀察"

    if 狀態 == "偏空":
        if close_to_sup:
            return "⚠️ 偏空｜不追空"
        if close_to_res:
            return "👀 偏空｜等反彈空"
        return "🔥 偏空｜等跌破"

    return "💤 盤整｜等方向"


# ==========================================================
# 單檔分析
# ==========================================================

def _分析單檔(api, symbol: str) -> Optional[dict]:
    名稱 = 報表設定.報表標的.get(symbol, symbol)

    df5 = _載入分K(api, symbol)
    if df5 is None or len(df5) < 30:
        print(f"  {symbol} 資料不足，略過")
        return None

    df30 = _resample_30min(df5)
    df_day = _resample_day(df5)

    if len(df30) < 5 or len(df_day) < 3:
        print(f"  {symbol} resample 後資料不足，略過")
        return None

    # 指標
    df5i = _add_all_indicators(df5)
    df30i = _add_all_indicators(df30)
    dfi = _add_all_indicators(df_day)

    日K方向 = _方向(df_day)
    分K方向30 = _方向(df30)

    日分數 = _結構分數(df_day)
    分30分數 = _結構分數(df30)

    狀態 = _綜合狀態(日K方向, 分K方向30, 日分數, 分30分數)

    # 關鍵價位
    前高, 前低 = _前高前低(df_day, 天數=報表設定.前高前低天數)
    大量high, 大量low = _大量K_high_low_報表(df5, 日K方向)
    平台高, 平台低 = _30分平台高低(df30, 回看根數=報表設定.平台回看根數)
    日ma20 = _日MA20(df_day)
    日ma60 = _日MA60(df_day)

    現價 = float(df5["close"].iloc[-1])

    # 與價位距離
    距前高 = _pct_up(前高, 現價) if 前高 is not None else None
    距大量K高 = _pct_up(大量high, 現價) if 大量high is not None else None
    距平台高 = _pct_up(平台高, 現價) if 平台高 is not None else None

    距前低 = _pct_down(現價, 前低) if 前低 is not None else None
    距大量K低 = _pct_down(現價, 大量low) if 大量low is not None else None
    距平台低 = _pct_down(現價, 平台低) if 平台低 is not None else None
    距日MA20 = _pct_down(現價, 日ma20) if 日ma20 is not None else None
    距日MA60 = _pct_down(現價, 日ma60) if 日ma60 is not None else None

    # 指標輸出
    日macd = _safe_round(dfi.iloc[-1]["OSC"], 3)
    三十分macd = _safe_round(df30i.iloc[-1]["OSC"], 3)
    日kd = f'{_safe_round(dfi.iloc[-1]["K"], 1)}/{_safe_round(dfi.iloc[-1]["D"], 1)}'
    三十分kd = f'{_safe_round(df30i.iloc[-1]["K"], 1)}/{_safe_round(df30i.iloc[-1]["D"], 1)}'

    結構型態 = _trend_to_text(日分數) + "｜" + _trend_to_text(分30分數)
    量價判讀 = _量價判讀(df5i, df30i, df_day, 狀態)

    return {
        # =====================================================
        # 基本
        # =====================================================
        "股票": f"{symbol} {名稱}",
        "現價": _safe_round(現價, 2),
        "交易狀態": _交易狀態(
            狀態,
            現價,
            前高,
            大量high,
            平台高,
            前低,
            大量low,
            平台低,
            日ma20,
            日ma60,
            日分數,
            分30分數,
        ),

        # =====================================================
        # 結構 / 方向
        # =====================================================
        "結構型態": 結構型態,
        "日K方向": 日K方向,
        "30分K方向": 分K方向30,

        # =====================================================
        # 關鍵壓力區
        # =====================================================
        "前高（3日）": 前高,
        "大量K高點": 大量high if 大量high is not None else "-",
        "30分平台高": 平台高,

        # =====================================================
        # 關鍵支撐區
        # =====================================================
        "前低（3日）": 前低,
        "大量K低點": 大量low if 大量low is not None else "-",
        "30分平台低": 平台低,
        "日MA20": 日ma20,

        # =====================================================
        # 指標
        # =====================================================
        "日MA60": 日ma60,
        "日MACD": 日macd,
        "30分MACD": 三十分macd,
        "日KD": 日kd,
        "30分KD": 三十分kd,

        # =====================================================
        # 距離
        # =====================================================
        "距前高%": 距前高,
        "距大量K高%": 距大量K高,
        "距平台高%": 距平台高,
        "距前低%": 距前低,
        "距大量K低%": 距大量K低,
        "距平台低%": 距平台低,
        "距日MA20%": 距日MA20,
        "距日MA60%": 距日MA60,

        # =====================================================
        # 結論
        # =====================================================
        "量價判讀": 量價判讀,
        "明日備註": _明日備註(
            狀態,
            日K方向,
            分K方向30,
            日分數,
            分30分數,
            前高,
            大量high,
            平台高,
            前低,
            大量low,
            平台低,
            日ma20,
            日ma60,
            現價,
        ),
    }


def _量價判讀(df5i: pd.DataFrame, df30i: pd.DataFrame, df_day: pd.DataFrame, 狀態: str) -> str:
    """
    簡短量價註解，方便快速掃圖。
    """
    try:
        last5 = df5i.iloc[-1]
        last30 = df30i.iloc[-1]
        lastd = df_day.iloc[-1]

        vol5 = float(last5["volume"])
        vol5_ma = float(df5i["volume"].rolling(20, min_periods=1).mean().iloc[-1])
        vol30 = float(last30["volume"])
        vol30_ma = float(df30i["volume"].rolling(20, min_periods=1).mean().iloc[-1])

        k5 = float(last5["K"])
        d5 = float(last5["D"])
        k30 = float(last30["K"])
        d30 = float(last30["D"])

        if vol5_ma > 0:
            ratio5 = vol5 / vol5_ma
        else:
            ratio5 = 1.0
        if vol30_ma > 0:
            ratio30 = vol30 / vol30_ma
        else:
            ratio30 = 1.0

        # 量能
        量 = "量增" if ratio5 >= 1.2 or ratio30 >= 1.2 else "量縮"

        # 動能
        if k5 > d5 and k30 > d30:
            動能 = "短多轉強"
        elif k5 < d5 and k30 < d30:
            動能 = "短空轉弱"
        elif k5 > d5 and k30 < d30:
            動能 = "日內反彈"
        else:
            動能 = "日內回檔"

        # 結構語意
        if 狀態 in ("主升多", "偏多"):
            結語 = "多方結構"
        elif 狀態 in ("主跌空", "偏空"):
            結語 = "空方結構"
        elif 狀態 in ("多方回檔", "空方反彈"):
            結語 = "反彈／回檔中"
        else:
            結語 = "區間整理"

        return f"{量}｜{動能}｜{結語}"
    except Exception:
        return "-"


# ==========================================================
# Git / TG
# ==========================================================

def _自動提交報表(root_index_path: str, html_path: str) -> None:
    """
    自動執行 Git 操作：
    - 檢查是否有變更
    - 有變更時執行 git add, commit, push
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            print("  ⚠️ 非 Git 倉庫，略過自動提交")
            return

        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )

        changes = status_result.stdout.strip()
        if not changes:
            print("  ✅ 無變更，略過 Git 操作")
            return

        print("  🔄 檢測到變更，執行 Git 操作...")

        subprocess.run(["git", "add", "."], timeout=10, check=True)
        print("    git add . ✅")

        subprocess.run(["git", "commit", "-m", "auto update report"], timeout=10, check=True)
        print("    git commit ✅")

        subprocess.run(["git", "push"], timeout=30, check=True)
        print("    git push ✅")

        print("  📤 Git 操作完成")

    except subprocess.TimeoutExpired:
        print("  ⚠️ Git 操作超時")
    except subprocess.CalledProcessError as e:
        print(f"  ⚠️ Git 操作失敗：{e}")
    except Exception as e:
        print(f"  ⚠️ Git 操作例外：{e}")


def _發送HTML到TG(path: str) -> None:
    if not 報表設定.發送HTML到TG:
        return

    url = (
        "https://api.telegram.org/bot"
        + 設定.TG_TOKEN
        + "/sendDocument"
    )

    try:
        with open(path, "rb") as f:
            r = requests.post(
                url,
                data={
                    "chat_id": 設定.TG_CHAT_ID,
                    "caption": "📊 今日盤後報表",
                },
                files={
                    "document": f
                },
                timeout=20,
            )

        if r.status_code == 200:
            print("📨 HTML 已發送 TG")
        else:
            print("TG HTML 發送失敗：", r.text)

    except Exception as e:
        print("TG HTML 發送例外：", e)


# ==========================================================
# 報表產生
# ==========================================================

def 產生報表(api=None) -> pd.DataFrame:
    """
    api:
      - 傳入已登入 API：讓 main.py 收盤時直接呼叫
      - api=None：report.py 自己登入 / 登出，可獨立執行
    """
    should_logout = False

    if api is None:
        api = 登入()
        should_logout = True

    print(f"\n📊 產生 AI 盤後結構報表...")

    rows = []
    for symbol in _股票池().keys():
        try:
            row = _分析單檔(api, symbol)
            if row:
                rows.append(row)
                print(f"  {symbol} ✅")
        except Exception as e:
            print(f"  {symbol} ❌ {e}")

    if not rows:
        print("  無任何資料，報表取消")
        if should_logout:
            api.logout()
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # 排序：優先顯示主升/主跌
    sort_order = {
        "主升多": 0,
        "偏多": 1,
        "多方回檔": 2,
        "盤整": 3,
        "空方反彈": 4,
        "偏空": 5,
        "主跌空": 6,
    }

    if "交易狀態" in df.columns:
        df["_sort"] = df["交易狀態"].map(lambda x: min([v for k, v in sort_order.items() if k in str(x)] or [3]))
        df = df.sort_values(["_sort", "股票"], ascending=[True, True]).drop(columns=["_sort"])

    os.makedirs(報表設定.報表輸出目錄, exist_ok=True)
    today = _today_str()

    csv_path = f"{報表設定.報表輸出目錄}/report_{today}.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"  CSV：{csv_path}")

    html_path = f"{報表設定.報表輸出目錄}/report_{today}.html"
    _輸出HTML(df, html_path, today)
    print(f"  HTML：{html_path}")

    _發送HTML到TG(html_path)

    root_index_path = "index.html"
    try:
        shutil.copy2(html_path, root_index_path)
        print(f"  根目錄 index.html：已更新")
    except Exception as e:
        print(f"  ⚠️ 複製 index.html 失敗：{e}")

    print("📊 報表完成")

    _自動提交報表(root_index_path, html_path)

    if should_logout:
        api.logout()

    return df


# ==========================================================
# HTML
# ==========================================================

_狀態顏色 = {
    "主升多": "#0b7a0b",
    "主跌空": "#a01010",
    "偏多": "#1a7a1a",
    "偏空": "#a06000",
    "多方回檔": "#1f6feb",
    "空方反彈": "#8b5cf6",
    "盤整": "#555555",
}

_方向顏色 = {
    "多": "#1a7a1a",
    "空": "#a01010",
}

def _cell_方向(v: str) -> str:
    color = _方向顏色.get(v, "#333")
    return f'<td style="color:{color};font-weight:bold;text-align:center">{v}</td>'

def _cell_狀態(v: str) -> str:
    color = _狀態顏色.get(v, "#333")
    bg = {
        "主升多": "#e8f5e9",
        "主跌空": "#fce4e4",
        "偏多": "#eef8ee",
        "偏空": "#fff6e8",
        "多方回檔": "#eef4ff",
        "空方反彈": "#f4ecff",
        "盤整": "#f0f0f0",
    }.get(v, "#fff")
    return f'<td style="color:{color};background:{bg};font-weight:bold;text-align:center">{v}</td>'


def _輸出HTML(df: pd.DataFrame, path: str, today: str) -> None:
    cols = df.columns.tolist()

    rows_html = ""
    for _, row in df.iterrows():
        cells = ""
        for col in cols:
            v = row[col]
            if col in ("日K方向", "30分K方向"):
                cells += _cell_方向(str(v))
            elif col == "交易狀態":
                cells += _cell_狀態(str(v))
            elif col == "明日備註":
                cells += f'<td style="text-align:left;min-width:420px;line-height:1.5">{v}</td>'
            elif col == "量價判讀":
                cells += f'<td style="text-align:left;min-width:160px">{v}</td>'
            else:
                cells += f'<td style="text-align:center">{v}</td>'
        rows_html += f"<tr>{cells}</tr>\n"

    headers = "".join(f"<th>{c}</th>" for c in cols)

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI盤後結構報表 {today}</title>
<style>
  :root {{
    color-scheme: light;
    --bg: #f4f6f8;
    --panel: #ffffff;
    --ink: #1f2933;
    --muted: #6b7280;
    --line: #e6e8ec;
    --head: #263442;
    --hover: #f4f7fb;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0 0 14px;
    font-family: Arial, "Microsoft JhengHei", sans-serif;
    background: var(--bg);
    color: var(--ink);
  }}
  main {{
    width: min(100%, 1450px);
    margin: 0 auto;
    padding: 20px;
  }}
  h2 {{
    margin: 0 0 10px;
    color: #1d2733;
    font-size: 24px;
    line-height: 1.2;
  }}
  .sub {{
    color: var(--muted);
    font-size: 12px;
    margin: 0 0 14px;
  }}
  .table-wrap {{
    width: 100%;
    overflow-x: auto;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 8px;
    box-shadow: 0 1px 4px rgba(15, 23, 42, .08);
    -webkit-overflow-scrolling: touch;
  }}
  table {{
    border-collapse: collapse;
    width: 100%;
    min-width: 1600px;
    background: var(--panel);
  }}
  th {{
    background: var(--head);
    color: #fff;
    padding: 11px 12px;
    text-align: center;
    font-size: 13px;
    position: sticky;
    top: 0;
    z-index: 2;
    white-space: nowrap;
  }}
  td {{
    padding: 10px 12px;
    border-bottom: 1px solid var(--line);
    font-size: 13px;
    vertical-align: middle;
    white-space: nowrap;
  }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: var(--hover); }}
  th:first-child,
  td:first-child {{
    position: sticky;
    left: 0;
    z-index: 1;
  }}
  td:first-child {{ background: var(--panel); font-weight: 700; }}
  th:first-child {{ z-index: 3; }}
  .ts {{
    color: var(--muted);
    font-size: 12px;
    margin: 10px 0 0;
  }}
  @media (max-width: 720px) {{
    main {{ padding: 12px; }}
    h2 {{ font-size: 20px; }}
    .table-wrap {{
      border-radius: 6px;
      box-shadow: none;
    }}
    table {{ min-width: 1400px; }}
    th, td {{
      padding: 9px 10px;
      font-size: 12px;
    }}
    td[style*="min-width:420px"] {{
      min-width: 260px !important;
      white-space: normal;
    }}
  }}
</style>
</head>
<body>
<main>
<h2>📊 AI盤後結構報表 {today}</h2>
<p class="sub">以「均線位置 + 斜率 + MACD/KD + 大量K + 平台/前高前低」綜合判讀</p>
<div class="table-wrap">
<table>
  <thead><tr>{headers}</tr></thead>
  <tbody>{rows_html}</tbody>
</table>
</div>
<p class="ts">產生時間：{_now_str()}</p>
</main>
</body>
</html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    產生報表()
