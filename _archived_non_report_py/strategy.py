# ============================================================
# strategy.py
# 核心策略：大量區突破 + 回踩確認
#
# 狀態機（多空對稱）：
#   WAIT_BREAK      → 等待收盤突破大量K high（多）/ 跌破大量K low（空）
#   WATCH_PULLBACK  → 已突破，等待回踩均線後再次轉強/弱
#   READY_ENTRY     → 第二確認成立，持續監控 EXIT
#   FAILED          → 回踩超過上限，訊號作廢
#   EXIT            → 跌破大量low（多）/ 突破大量high（空），發退場通知
#
# 目前保留的有效大量K條件：
#   1. 量能放大：volume > volume_ma * 倍數
#   2. 多方紅K / 空方黑K
#   3. 實體夠大
#
# 目前的大量區定義：
#   - 在回看區間內，只取「單一最強大量K」作為高低點
#   - 不再使用 max(high) / min(low) 混合多根K
#
# 其餘先拿掉（close靠近high/low、上下影線限制、波動比例、ATR）
# ============================================================

import pandas as pd
from typing import Optional, Tuple
from config import 設定


# ==========================================================
# 指標計算
# ==========================================================

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    df['MA5']  = df['close'].rolling(設定.MA短期,  min_periods=1).mean()
    df['MA10'] = df['close'].rolling(設定.MA中期, min_periods=1).mean()
    df['MA20'] = df['close'].rolling(設定.MA長期, min_periods=1).mean()

    return df


def _ensure_indicators(df: pd.DataFrame) -> pd.DataFrame:

    required = {"MA5", "MA10", "MA20"}

    if required.issubset(df.columns):
        return df

    return add_indicators(df)


# ==========================================================
# 大量K篩選（多空對稱）
# ==========================================================

def _是強勢多方K(row: pd.Series, vol_ma: float) -> bool:
    """
    多方大量K條件：
    1. 量能放大：volume > vol_ma * 倍數
    2. 收紅K：close > open
    3. 實體夠大：abs(close-open)/open >= 0.5%（由 config 控制）
    """
    close = float(row['close'])
    open_ = float(row['open'])
    vol   = float(row['volume'])

    if close <= 0 or open_ <= 0:
        return False

    if vol < vol_ma * 設定.大量K量能倍數:
        return False

    if close <= open_:
        return False

    if abs(close - open_) / open_ < 設定.大量K實體最小比例:
        return False

    return True


def _是強勢空方K(row: pd.Series, vol_ma: float) -> bool:
    """
    空方大量K條件（對稱）：
    1. 量能放大：volume > vol_ma * 倍數
    2. 收黑K：close < open
    3. 實體夠大：abs(close-open)/open >= 0.5%（由 config 控制）
    """
    close = float(row['close'])
    open_ = float(row['open'])
    vol   = float(row['volume'])

    if close <= 0 or open_ <= 0:
        return False

    if vol < vol_ma * 設定.大量K量能倍數:
        return False

    if close >= open_:
        return False

    if abs(close - open_) / open_ < 設定.大量K實體最小比例:
        return False

    return True




def _大量K候選評分(row: pd.Series, vol_ma: float) -> tuple:
    """
    回傳候選大量K的排序分數。
    分數越高，代表量能放大越明顯、實體越大。
    """
    high = float(row['high'])
    low = float(row['low'])
    close = float(row['close'])
    open_ = float(row['open'])
    vol = float(row['volume'])

    if vol_ma <= 0 or close <= 0 or open_ <= 0:
        return (-1.0, -1.0, -1.0, -1.0)

    vol_ratio = vol / vol_ma
    body_ratio = abs(close - open_) / open_
    range_ratio = (high - low) / close if close > 0 else 0.0

    # 以量能倍率為主，其次是實體，再來是振幅，最後用成交量當 tie-break
    return (vol_ratio, body_ratio, range_ratio, vol)


def 取大量區(
    df: pd.DataFrame,
    方向: str = "多",
) -> Tuple[Optional[float], Optional[float]]:
    """
    在最近 大量區回看根數 根內（不含最後一根）
    找出符合條件的強勢K棒。

    目前改成「只取單一最強大量K」：
    - 多方：取最強多方K那一根的 high / low
    - 空方：取最強空方K那一根的 high / low

    找不到符合條件的K棒時回傳 (None, None)
    """

    回看 = 設定.大量區回看根數
    均線根數 = 設定.大量K量能均線根數

    # 不含最後一根（當前未封棒）
    window = df.iloc[-(回看 + 1):-1]

    if len(window) < 均線根數:
        return None, None

    最佳分數 = None
    最佳row = None

    for i in range(len(window)):
        # 前面樣本不夠就不判斷，避免 MA 太短失真
        if i < 均線根數:
            continue

        row = window.iloc[i]
        vol_ma = float(window['volume'].iloc[i - 均線根數:i].mean())

        if vol_ma <= 0:
            continue

        if 方向 == "多":
            合格 = _是強勢多方K(row, vol_ma)
        else:
            合格 = _是強勢空方K(row, vol_ma)

        if not 合格:
            continue

        分數 = _大量K候選評分(row, vol_ma)
        比較鍵 = (*分數, i)

        if 最佳分數 is None or 比較鍵 > 最佳分數:
            最佳分數 = 比較鍵
            最佳row = row

    if 最佳row is None:
        return None, None

    return float(最佳row['high']), float(最佳row['low'])

# ==========================================================
# 趨勢方向
# ==========================================================

def 多方趨勢(now: pd.Series) -> bool:
    return float(now['close']) > float(now['MA20'])


def 空方趨勢(now: pd.Series) -> bool:
    return float(now['close']) < float(now['MA20'])


def 多方趨勢強度(now: pd.Series) -> bool:
    return (
        float(now['MA5']) > float(now['MA10'])
        and float(now['MA10']) > float(now['MA20'])
    )


def 空方趨勢強度(now: pd.Series) -> bool:
    return (
        float(now['MA5']) < float(now['MA10'])
        and float(now['MA10']) < float(now['MA20'])
    )


# ==========================================================
# 突破判斷（含容忍值）
# ==========================================================

def 多方突破(close: float, 大量high: float) -> bool:
    return close >= 大量high * (1 - 設定.突破容忍比例)


def 空方突破(close: float, 大量low: float) -> bool:
    return close <= 大量low * (1 + 設定.突破容忍比例)


# ==========================================================
# 回踩判斷
# 多方用 low，空方用 high
# ==========================================================

def 靠近均線(價格: float, ma: float) -> bool:
    容忍 = 設定.回踩容忍比例
    return ma * (1 - 容忍) <= 價格 <= ma * (1 + 容忍)


def 多方回踩(now: pd.Series) -> bool:
    low = float(now['low'])
    return (
        靠近均線(low, float(now['MA5']))
        or
        靠近均線(low, float(now['MA10']))
    )


def 空方回踩(now: pd.Series) -> bool:
    high = float(now['high'])
    return (
        靠近均線(high, float(now['MA5']))
        or
        靠近均線(high, float(now['MA10']))
    )


# ==========================================================
# 第二確認（AND）
# ==========================================================

def 多方第二確認(now: pd.Series, 大量high: float) -> bool:
    close = float(now['close'])
    return (
        多方突破(close, 大量high)
        and close > float(now['MA5'])
    )


def 空方第二確認(now: pd.Series, 大量low: float) -> bool:
    close = float(now['close'])
    return (
        空方突破(close, 大量low)
        and close < float(now['MA5'])
    )


# ==========================================================
# EXIT 判斷
# ==========================================================

def 多方EXIT(now: pd.Series, 大量low: float) -> bool:
    return float(now['close']) < 大量low


def 空方EXIT(now: pd.Series, 大量high: float) -> bool:
    return float(now['close']) > 大量high


# ==========================================================
# 主訊號函式
# ==========================================================

def 計算訊號(
    df: pd.DataFrame,
    symbol: str,
    當前狀態: Optional[str],
    回踩次數: int,
    回踩中: bool,
    鎖定high: Optional[float],
    鎖定low: Optional[float],
    方向: str,
    突破K棒idx: Optional[int] = None,
    回踩K棒idx: Optional[int] = None,
) -> Optional[dict]:

    df = _ensure_indicators(df)

    if len(df) < 設定.大量區回看根數 + 設定.最少K棒緩衝:
        return None

    now = df.iloc[-1]
    當前idx = len(df) - 1

    if now.name.time() < pd.Timestamp(設定.開盤保護到).time():
        return None

    close = float(now['close'])
    ma5   = float(now['MA5'])
    ma10  = float(now['MA10'])
    ma20  = float(now['MA20'])

    趨勢強 = 多方趨勢強度(now) if 方向 == "多" else 空方趨勢強度(now)

    if 當前狀態 in (None, "WAIT_BREAK"):
        大量high, 大量low = 取大量區(df, 方向)
        if 大量high is None or 大量low is None:
            return None
    else:
        if 鎖定high is None or 鎖定low is None:
            return None
        大量high = 鎖定high
        大量low  = 鎖定low

    基本資訊 = {
        "方向":     方向,
        "大量high": 大量high,
        "大量low":  大量low,
        "MA5":      round(ma5, 2),
        "MA10":     round(ma10, 2),
        "MA20":     round(ma20, 2),
        "趨勢強度": 趨勢強,
        "回踩次數": 回踩次數,
        "回踩中":   回踩中,
    }

    # ==================================================
    # 多方邏輯
    # ==================================================

    if 方向 == "多":

        if 當前狀態 in (None, "WAIT_BREAK"):

            if 多方突破(close, 大量high):
                return {
                    **基本資訊,
                    "狀態": "WATCH_PULLBACK",
                    "類型": "多方觀察",
                    "理由": (
                        f"收盤 {close} 突破大量區高點 {大量high}，"
                        f"等待回踩均線後確認"
                    ),
                    "鎖定high":   大量high,
                    "鎖定low":    大量low,
                    "突破K棒idx": 當前idx,
                    "回踩K棒idx": None,
                }

            return None

        if 當前狀態 == "WATCH_PULLBACK":

            if 多方EXIT(now, 大量low):
                return {
                    **基本資訊,
                    "狀態": "EXIT",
                    "類型": "多方退場",
                    "理由": f"收盤 {close} 跌破大量區低點 {大量low}，訊號作廢",
                }

            突破後足夠 = 突破K棒idx is not None and 當前idx > 突破K棒idx
            回踩後足夠 = 回踩K棒idx is not None and 當前idx > 回踩K棒idx

            if 突破後足夠 and 回踩後足夠 and 多方第二確認(now, 大量high):
                return {
                    **基本資訊,
                    "狀態": "READY_ENTRY",
                    "類型": "多方進場",
                    "理由": (
                        f"回踩後再次站上大量高點 {大量high} "
                        f"且站上 MA5 {round(ma5, 2)}，確認進場"
                    ),
                    "鎖定high":   大量high,
                    "鎖定low":    大量low,
                    "突破K棒idx": 突破K棒idx,
                    "回踩K棒idx": 回踩K棒idx,
                }

            if 突破後足夠:
                是否回踩中 = 多方回踩(now)

                if 是否回踩中 and not 回踩中:
                    次數 = 回踩次數 + 1
                    if 次數 > 設定.最大回踩次數:
                        return {
                            **基本資訊,
                            "狀態": "FAILED",
                            "類型": "多方回踩失敗",
                            "理由": f"已回踩 {次數} 次，超過上限，訊號作廢",
                            "回踩次數": 次數,
                            "回踩中":   True,
                        }
                    return {
                        **基本資訊,
                        "狀態": "WATCH_PULLBACK",
                        "類型": "回踩計數",
                        "理由": f"第 {次數} 次回踩",
                        "回踩次數": 次數,
                        "回踩中":   True,
                        "鎖定high":   大量high,
                        "鎖定low":    大量low,
                        "突破K棒idx": 突破K棒idx,
                        "回踩K棒idx": 當前idx,
                    }

                if 是否回踩中 and 回踩中:
                    return {
                        **基本資訊,
                        "狀態": "WATCH_PULLBACK",
                        "類型": "回踩計數",
                        "理由": f"回踩持續中（第 {回踩次數} 次）",
                        "回踩次數": 回踩次數,
                        "回踩中":   True,
                        "鎖定high":   大量high,
                        "鎖定low":    大量low,
                        "突破K棒idx": 突破K棒idx,
                        "回踩K棒idx": 回踩K棒idx,
                    }

                return {
                    **基本資訊,
                    "狀態": "WATCH_PULLBACK",
                    "類型": "回踩結束",
                    "理由": "離開回踩區，等待第二確認",
                    "回踩次數": 回踩次數,
                    "回踩中":   False,
                    "鎖定high":   大量high,
                    "鎖定low":    大量low,
                    "突破K棒idx": 突破K棒idx,
                    "回踩K棒idx": 回踩K棒idx,
                }

            return {
                **基本資訊,
                "狀態": "WATCH_PULLBACK",
                "類型": "回踩結束",
                "理由": "突破後第一根，等待回踩",
                "回踩次數": 回踩次數,
                "回踩中":   False,
                "鎖定high":   大量high,
                "鎖定low":    大量low,
                "突破K棒idx": 突破K棒idx,
                "回踩K棒idx": 回踩K棒idx,
            }

        if 當前狀態 == "READY_ENTRY":
            if 多方EXIT(now, 大量low):
                return {
                    **基本資訊,
                    "狀態": "EXIT",
                    "類型": "多方退場",
                    "理由": f"進場後收盤 {close} 跌破大量區低點 {大量low}，建議出場",
                }
            return None

        return None

    # ==================================================
    # 空方邏輯（對稱）
    # ==================================================

    else:

        if 當前狀態 in (None, "WAIT_BREAK"):

            if 空方突破(close, 大量low):
                return {
                    **基本資訊,
                    "狀態": "WATCH_PULLBACK",
                    "類型": "空方觀察",
                    "理由": (
                        f"收盤 {close} 跌破大量區低點 {大量low}，"
                        f"等待反彈均線後確認"
                    ),
                    "鎖定high":   大量high,
                    "鎖定low":    大量low,
                    "突破K棒idx": 當前idx,
                    "回踩K棒idx": None,
                }

            return None

        if 當前狀態 == "WATCH_PULLBACK":

            if 空方EXIT(now, 大量high):
                return {
                    **基本資訊,
                    "狀態": "EXIT",
                    "類型": "空方退場",
                    "理由": f"收盤 {close} 突破大量區高點 {大量high}，訊號作廢",
                }

            突破後足夠 = 突破K棒idx is not None and 當前idx > 突破K棒idx
            回踩後足夠 = 回踩K棒idx is not None and 當前idx > 回踩K棒idx

            if 突破後足夠 and 回踩後足夠 and 空方第二確認(now, 大量low):
                return {
                    **基本資訊,
                    "狀態": "READY_ENTRY",
                    "類型": "空方進場",
                    "理由": (
                        f"反彈後再次跌破大量低點 {大量low} "
                        f"且跌破 MA5 {round(ma5, 2)}，確認進場"
                    ),
                    "鎖定high":   大量high,
                    "鎖定low":    大量low,
                    "突破K棒idx": 突破K棒idx,
                    "回踩K棒idx": 回踩K棒idx,
                }

            if 突破後足夠:
                是否回踩中 = 空方回踩(now)

                if 是否回踩中 and not 回踩中:
                    次數 = 回踩次數 + 1
                    if 次數 > 設定.最大回踩次數:
                        return {
                            **基本資訊,
                            "狀態": "FAILED",
                            "類型": "空方反彈失敗",
                            "理由": f"已反彈 {次數} 次，超過上限，訊號作廢",
                            "回踩次數": 次數,
                            "回踩中":   True,
                        }
                    return {
                        **基本資訊,
                        "狀態": "WATCH_PULLBACK",
                        "類型": "回踩計數",
                        "理由": f"第 {次數} 次反彈",
                        "回踩次數": 次數,
                        "回踩中":   True,
                        "鎖定high":   大量high,
                        "鎖定low":    大量low,
                        "突破K棒idx": 突破K棒idx,
                        "回踩K棒idx": 當前idx,
                    }

                if 是否回踩中 and 回踩中:
                    return {
                        **基本資訊,
                        "狀態": "WATCH_PULLBACK",
                        "類型": "回踩計數",
                        "理由": f"反彈持續中（第 {回踩次數} 次）",
                        "回踩次數": 回踩次數,
                        "回踩中":   True,
                        "鎖定high":   大量high,
                        "鎖定low":    大量low,
                        "突破K棒idx": 突破K棒idx,
                        "回踩K棒idx": 回踩K棒idx,
                    }

                return {
                    **基本資訊,
                    "狀態": "WATCH_PULLBACK",
                    "類型": "回踩結束",
                    "理由": "離開反彈區，等待第二確認",
                    "回踩次數": 回踩次數,
                    "回踩中":   False,
                    "鎖定high":   大量high,
                    "鎖定low":    大量low,
                    "突破K棒idx": 突破K棒idx,
                    "回踩K棒idx": 回踩K棒idx,
                }

            return {
                **基本資訊,
                "狀態": "WATCH_PULLBACK",
                "類型": "回踩結束",
                "理由": "突破後第一根，等待回踩",
                "回踩次數": 回踩次數,
                "回踩中":   False,
                "鎖定high":   大量high,
                "鎖定low":    大量low,
                "突破K棒idx": 突破K棒idx,
                "回踩K棒idx": 回踩K棒idx,
            }

        if 當前狀態 == "READY_ENTRY":
            if 空方EXIT(now, 大量high):
                return {
                    **基本資訊,
                    "狀態": "EXIT",
                    "類型": "空方退場",
                    "理由": f"進場後收盤 {close} 突破大量區高點 {大量high}，建議出場",
                }
            return None

        return None
