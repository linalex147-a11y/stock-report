import pandas as pd
import shioaji as sj
from datetime import datetime, timedelta
from config import 設定


def get_kbars(api, symbol, start=None, end=None) -> pd.DataFrame:
    """
    從永豐 API 拉 1 分 tick 並聚合成 N 分K棒
    start / end 未傳時使用 config 的備用起迄
    """

    start = start or 設定.歷史開始
    end   = end   or 設定.歷史結束

    contract = api.Contracts.Stocks[symbol]

    kbars = api.kbars(
        contract,
        start=start,
        end=end,
    )

    df = pd.DataFrame({
        "ts":     pd.to_datetime(kbars.ts),
        "open":   kbars.Open,
        "high":   kbars.High,
        "low":    kbars.Low,
        "close":  kbars.Close,
        "volume": kbars.Volume,
    })

    df = df.sort_values("ts").set_index("ts")

    # 過濾非交易時段（時間統一從 config 讀）
    起始 = pd.to_datetime(設定.資料起始時間).time()
    截止 = pd.to_datetime(設定.資料截止時間).time()

    df = df[
        (df.index.time >= 起始)
        &
        (df.index.time <= 截止)
    ]

    # 聚合成 N 分K棒
    resample_rule = f"{設定.K棒週期}min"

    df = df.resample(
        resample_rule,
        closed='left',
        label='left',
    ).agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }).dropna()

    return df


def get_daily_kbars(api, symbol, start=None, end=None) -> pd.DataFrame:
    """
    從分K資料 resample 出日K
    每天取：open=第一根, high=最高, low=最低, close=最後一根, volume=加總
    """

    df = get_kbars(api, symbol, start=start, end=end)

    if df is None or len(df) == 0:
        return pd.DataFrame()

    daily = df.resample('D').agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }).dropna()

    return daily


def get_kbars_with_history(
    api,
    symbol: str,
    target_date: str,
) -> pd.DataFrame:
    """
    補歷史K棒：從 target_date 往前回看 config.補歷史天數 天
    """

    target_dt = datetime.strptime(target_date, "%Y-%m-%d")
    start_dt  = target_dt - timedelta(days=設定.補歷史天數)

    return get_kbars(
        api,
        symbol,
        start=start_dt.strftime("%Y-%m-%d"),
        end=target_date,
    )
