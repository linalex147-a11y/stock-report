"""
backtest.py
回測引擎：大量區突破策略

使用方式：
    python backtest.py

所有參數從 config.py 讀取：
    回測開始日期、回測結束日期、回測標的、回測方向
    移動停利比例、手續費率、手續費折扣、交易稅率
    回測輸出目錄
"""

import os
import time
import pandas as pd

from datetime import datetime
from typing import Optional
from config import 設定
from strategy import (
    add_indicators,
    取大量區,
    多方突破,
    空方突破,
    多方EXIT,
    空方EXIT,
    靠近均線,
)
from data_loader import get_daily_kbars


# ==========================================================
# 日K趨勢
# ==========================================================

def 建立日K趨勢表(api, symbol: str) -> dict:
    """
    載入日K並計算 MA20
    回傳 {date: "多" / "空" / "中立"} 對照表
    昨日收盤 > 日K MA20 → 多
    昨日收盤 < 日K MA20 → 空
    """

    try:
        df = get_daily_kbars(
            api,
            symbol,
            start=設定.回測開始日期,
            end=設定.回測結束日期,
        )
    except Exception as e:
        print(f"  ⚠️ {symbol} 日K載入失敗：{e}，趨勢過濾停用")
        return {}

    if df is None or len(df) < 設定.日K_MA週期:
        print(f"  ⚠️ {symbol} 日K資料不足，趨勢過濾停用")
        return {}

    df = df.copy()
    df['MA'] = df['close'].rolling(設定.日K_MA週期, min_periods=1).mean()

    趨勢表 = {}
    dates = df.index.normalize().unique()

    for i in range(1, len(dates)):
        今日 = dates[i].date()
        昨日 = dates[i - 1].date()

        if 昨日 not in df.index.date:
            continue

        昨日close = float(df[df.index.date == 昨日]['close'].iloc[-1])
        昨日MA    = float(df[df.index.date == 昨日]['MA'].iloc[-1])

        if 昨日close > 昨日MA:
            趨勢表[今日] = "多"
        elif 昨日close < 昨日MA:
            趨勢表[今日] = "空"
        else:
            趨勢表[今日] = "中立"

    return 趨勢表


# ==========================================================
# 手續費
# ==========================================================

def _總成本比例() -> float:
    """來回手續費 + 賣方交易稅"""
    單邊 = 設定.手續費率 * 設定.手續費折扣
    return 單邊 * 2 + 設定.交易稅率


# ==========================================================
# 資料載入
# ==========================================================

def 載入CSV(path: str) -> pd.DataFrame:
    """
    從 CSV 載入K棒
    時間欄位自動偵測：ts / datetime / date / time
    """
    df = pd.read_csv(path)

    for col in ["ts", "datetime", "date", "time"]:
        if col in df.columns:
            df = df.rename(columns={col: "ts"})
            break

    df["ts"] = pd.to_datetime(df["ts"])
    df = df.sort_values("ts").set_index("ts")

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    return df.dropna(subset=["open", "high", "low", "close", "volume"])


def 載入API(api, symbol: str) -> Optional[pd.DataFrame]:
    """從永豐 API 載入，區間從 config 讀取"""
    from data_loader import get_kbars

    try:
        df = get_kbars(
            api,
            symbol,
            start=設定.回測開始日期,
            end=設定.回測結束日期,
        )
        return df

    except Exception as e:
        print(f"  ⚠️ {symbol} API 載入失敗：{e}")
        return None


# ==========================================================
# 單筆交易模擬
# ==========================================================

def 模擬交易(
    df: pd.DataFrame,
    進場idx: int,
    進場價: float,
    方向: str,
    鎖定high: float,
    鎖定low: float,
    當日最後idx: int,
) -> dict:
    """
    從進場那根的下一根開始逐根模擬，不跨日。
    回傳 出場idx 供主迴圈跳過持倉期間（P0-1 修正）。

    出場條件（先到先出）：
      1. close trailing stop：收盤價更新最佳價，再用收盤觸發停利
      2. EXIT：收盤跌破大量low（多）/ 突破大量high（空）
      3. 當日最後一根強制收盤平倉

    ⚠️ 已知偏差（P0-2/3/4，暫不修正）：
      - 改用 close trailing 取代 high/low trailing，
        避免 intrabar 順序假設，但停利會慢一根反應。
      - 棒內 high/low 先後仍無法還原（需 tick replay 才能精確）。
      - 回踩 / 第二確認用收盤判斷，與實盤棒內觸發有落差。
    """

    停利比例 = 設定.移動停利比例
    # close trailing：用收盤價追蹤最佳價
    最佳收盤  = 進場價
    出場價    = None
    出場原因  = None
    出場時間  = None
    出場idx   = 當日最後idx          # 預設收盤平倉位置
    結束idx   = min(當日最後idx + 1, len(df))

    for i in range(進場idx + 1, 結束idx):

        row   = df.iloc[i]
        close = float(row['close'])
        ts    = df.index[i]

        if 方向 == "多":

            # ── close trailing：先檢查停利，再更新最佳收盤 ──
            停利價 = 最佳收盤 * (1 - 停利比例)

            if close <= 停利價:
                出場價   = close
                出場原因 = "移動停利"
                出場時間 = ts
                出場idx  = i
                break

            # EXIT
            if 多方EXIT(row, 鎖定low):
                出場價   = close
                出場原因 = "EXIT"
                出場時間 = ts
                出場idx  = i
                break

            最佳收盤 = max(最佳收盤, close)

        else:

            停利價 = 最佳收盤 * (1 + 停利比例)

            if close >= 停利價:
                出場價   = close
                出場原因 = "移動停利"
                出場時間 = ts
                出場idx  = i
                break

            if 空方EXIT(row, 鎖定high):
                出場價   = close
                出場原因 = "EXIT"
                出場時間 = ts
                出場idx  = i
                break

            最佳收盤 = min(最佳收盤, close)

    if 出場價 is None:
        last     = min(當日最後idx, len(df) - 1)
        出場價   = float(df.iloc[last]['close'])
        出場原因 = "收盤平倉"
        出場時間 = df.index[last]
        出場idx  = last

    if 方向 == "多":
        毛損益pct = (出場價 - 進場價) / 進場價
    else:
        毛損益pct = (進場價 - 出場價) / 進場價

    淨損益pct = 毛損益pct - _總成本比例()

    return {
        "出場價":   round(出場價, 2),
        "出場原因": 出場原因,
        "出場時間": 出場時間,
        "出場idx":  出場idx,          # P0-1：供主迴圈跳過持倉期間
        "毛損益%":  round(毛損益pct * 100, 3),
        "淨損益%":  round(淨損益pct * 100, 3),
    }


# ==========================================================
# 單標的回測
# ==========================================================

def _重置狀態() -> dict:
    """每日開盤重置為乾淨狀態"""
    return {
        "當前狀態":   "WAIT_BREAK",
        "鎖定high":   None,
        "鎖定low":    None,
        "回踩次數":   0,
        "回踩中":     False,
        "進場idx":    None,
        "進場價":     None,
        "進場時間":   None,
        "當日已進場": False,   # 每天每方向只進場一次
    }


def _訊號結束狀態(s: dict) -> dict:
    """進場出場後，當天不再找訊號（保留 當日已進場 旗標）"""
    ns = _重置狀態()
    ns["當日已進場"] = True
    return ns


def 回測單標的(
    df: pd.DataFrame,
    symbol: str,
    方向: str,
    進場模式: str = "兩者",  # "突破" / "回踩" / "兩者"
    趨勢表: dict = None,     # {date: "多"/"空"/"中立"}，None = 不過濾
) -> pd.DataFrame:

    df = add_indicators(df.copy())
    df = df.sort_index()
    df = df[~df.index.duplicated(keep='last')]

    最少根數 = 設定.大量區回看根數 + 設定.最少K棒緩衝
    records  = []

    s = _重置狀態()
    前一日 = None

    # 預先建立每日最後一根的 idx 對照表（加速換日查找）
    日期_最後idx = {}
    for idx, ts in enumerate(df.index):
        日期_最後idx[ts.date()] = idx

    # 用 while 取代 for，讓 READY_ENTRY 出場後能跳過持倉期間（P0-1）
    i = 最少根數
    while i < len(df):

        now      = df.iloc[i]
        close    = float(now['close'])
        當日     = df.index[i].date()
        今日最後idx = 日期_最後idx[當日]

        # ── 換日重置：策略是當日邏輯，跨日狀態清除 ──
        if 前一日 is not None and 當日 != 前一日:
            s = _重置狀態()

        前一日 = 當日

        # ── WAIT_BREAK ──
        if s["當前狀態"] == "WAIT_BREAK":

            # 當天已進場過，不再找訊號
            if s["當日已進場"]:
                i += 1
                continue

            # 日K趨勢過濾：昨日方向不符則跳過
            if 設定.日K趨勢過濾 and 趨勢表:
                今日趨勢 = 趨勢表.get(當日, "中立")
                if 今日趨勢 == "中立":
                    i += 1
                    continue
                if 今日趨勢 != 方向:
                    i += 1
                    continue

            # 大量區用最近N根（可跨昨天），確保早盤也有足夠資料
            大量high, 大量low = 取大量區(df.iloc[:i+1])

            if 大量high is None:
                i += 1
                continue

            突破 = (
                多方突破(close, 大量high) if 方向 == "多"
                else 空方突破(close, 大量low)
            )

            if 突破:
                if 進場模式 == "突破":
                    # 直接進場，不等確認
                    s["進場idx"]  = i
                    s["進場價"]   = close
                    s["進場時間"] = df.index[i]
                    s["鎖定high"] = 大量high
                    s["鎖定low"]  = 大量low
                    s["當前狀態"] = "READY_ENTRY"
                else:
                    # 回踩 / 兩者：進 WATCH_PULLBACK 等確認
                    s["當前狀態"] = "WATCH_PULLBACK"
                    s["鎖定high"] = 大量high
                    s["鎖定low"]  = 大量low
                    s["回踩次數"] = 0
                    s["回踩中"]   = False
                    s["等待紅K"]  = False

            i += 1
            continue

        # ── WATCH_PULLBACK ──
        if s["當前狀態"] == "WATCH_PULLBACK":

            # EXIT 先判斷
            if 方向 == "多" and 多方EXIT(now, s["鎖定low"]):
                s = _重置狀態()
                i += 1
                continue

            if 方向 == "空" and 空方EXIT(now, s["鎖定high"]):
                s = _重置狀態()
                i += 1
                continue

            # 第二確認
            ma5      = float(now['MA5'])
            ma10     = float(now['MA10'])
            low_val  = float(now['low'])
            high_val = float(now['high'])
            prev     = df.iloc[i - 1]

            # ── 回踩模式：回踩均線後下一根收紅K進場 ──
            if 進場模式 == "回踩":

                if s.get("等待紅K", False):
                    紅K = (
                        close > float(prev['close'])
                        if 方向 == "多"
                        else close < float(prev['close'])
                    )
                    if 紅K:
                        s["進場idx"]  = i
                        s["進場價"]   = close
                        s["進場時間"] = df.index[i]
                        s["當前狀態"] = "READY_ENTRY"
                        i += 1
                        continue
                    else:
                        s["等待紅K"] = False

                是否回踩 = (
                    靠近均線(low_val,  ma5) or 靠近均線(low_val,  ma10)
                    if 方向 == "多"
                    else 靠近均線(high_val, ma5) or 靠近均線(high_val, ma10)
                )

                if 是否回踩 and not s["回踩中"]:
                    s["回踩次數"] += 1
                    s["回踩中"]   = True
                    s["等待紅K"]  = True
                    if s["回踩次數"] > 設定.最大回踩次數:
                        s = _重置狀態()
                elif not 是否回踩:
                    s["回踩中"] = False

                i += 1
                continue

            # ── 兩者模式：回踩後再次站上大量high + MA5 ──
            確認 = (
                多方突破(close, s["鎖定high"]) and close > ma5
                if 方向 == "多"
                else 空方突破(close, s["鎖定low"]) and close < ma5
            )

            if 確認:
                s["進場idx"]  = i
                s["進場價"]   = close
                s["進場時間"] = df.index[i]
                s["當前狀態"] = "READY_ENTRY"
                i += 1
                continue

            是否回踩 = (
                靠近均線(low_val,  ma5) or 靠近均線(low_val,  ma10)
                if 方向 == "多"
                else 靠近均線(high_val, ma5) or 靠近均線(high_val, ma10)
            )

            if 是否回踩 and not s["回踩中"]:
                s["回踩次數"] += 1
                s["回踩中"]   = True
                if s["回踩次數"] > 設定.最大回踩次數:
                    s = _重置狀態()
            elif not 是否回踩:
                s["回踩中"] = False

            i += 1
            continue

        # ── READY_ENTRY：模擬持倉，主迴圈跳到出場後（P0-1 修正）──
        if s["當前狀態"] == "READY_ENTRY":

            result = 模擬交易(
                df, s["進場idx"], s["進場價"],
                方向, s["鎖定high"], s["鎖定low"],
                今日最後idx,
            )

            records.append({
                "symbol":   symbol,
                "方向":     方向,
                "進場模式": 進場模式,
                "進場時間": s["進場時間"],
                "進場價":   s["進場價"],
                "出場時間": result["出場時間"],
                "出場價":   result["出場價"],
                "出場原因": result["出場原因"],
                "毛損益%":  result["毛損益%"],
                "淨損益%":  result["淨損益%"],
                "鎖定high": s["鎖定high"],
                "鎖定low":  s["鎖定low"],
            })

            # 跳過持倉期間：i 設為出場那根，while 結尾的 i+=1 會再+1
            i = result["出場idx"]

            s = _訊號結束狀態(s)
            i += 1
            continue

        i += 1

    return pd.DataFrame(records)


# ==========================================================
# 績效統計
# ==========================================================

def 計算績效(trades: pd.DataFrame) -> dict:

    if trades.empty:
        return {"總交易次數": 0}

    total = len(trades)
    wins  = trades[trades["淨損益%"] > 0]
    losses = trades[trades["淨損益%"] <= 0]

    累積     = trades["淨損益%"].cumsum()
    最高水位 = 累積.cummax()
    最大回撤 = (累積 - 最高水位).min()

    return {
        "總交易次數":    total,
        "獲利次數":      len(wins),
        "虧損次數":      len(losses),
        "勝率%":         round(len(wins) / total * 100, 1),
        "平均獲利%":     round(wins["淨損益%"].mean(), 3)   if len(wins)   > 0 else 0.0,
        "平均虧損%":     round(losses["淨損益%"].mean(), 3) if len(losses) > 0 else 0.0,
        "總淨損益%":     round(trades["淨損益%"].sum(), 3),
        "最大單筆獲利%": round(trades["淨損益%"].max(), 3),
        "最大單筆虧損%": round(trades["淨損益%"].min(), 3),
        "最大回撤%":     round(最大回撤, 3),
        "出場分布":      trades["出場原因"].value_counts().to_dict(),
    }


# ==========================================================
# 輸出
# ==========================================================

def 印出績效(label: str, stats: dict) -> None:

    BAR = "=" * 50
    print(f"\n{BAR}")
    print(f"  {label} 回測結果")
    print(BAR)

    if stats.get("總交易次數", 0) == 0:
        print("  無任何交易訊號")
        print(BAR)
        return

    print(f"  總交易次數  ：{stats['總交易次數']}")
    print(f"  獲利 / 虧損 ：{stats['獲利次數']} / {stats['虧損次數']}")
    print(f"  勝率        ：{stats['勝率%']}%")
    print(f"  平均獲利    ：{stats['平均獲利%']}%")
    print(f"  平均虧損    ：{stats['平均虧損%']}%")
    print(f"  總淨損益    ：{stats['總淨損益%']}%")
    print(f"  最大單筆獲利：{stats['最大單筆獲利%']}%")
    print(f"  最大單筆虧損：{stats['最大單筆虧損%']}%")
    print(f"  最大回撤    ：{stats['最大回撤%']}%")
    print(f"  出場分布    ：{stats['出場分布']}")
    print(BAR)


def 存CSV(trades: pd.DataFrame, label: str) -> None:

    os.makedirs(設定.回測輸出目錄, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{設定.回測輸出目錄}/{label}_{ts}.csv"
    trades.to_csv(filename, index=False, encoding="utf-8-sig")
    print(f"  已儲存：{filename}")


# ==========================================================
# 主回測入口
# ==========================================================

def 執行回測(api=None, csv_paths: dict = None) -> pd.DataFrame:
    """
    所有參數從 config 讀取
    api       : 永豐 API 物件（csv_paths 為 None 時必填）
    csv_paths : {symbol: csv路徑}，優先於 API
    """

    symbols  = 設定.回測標的
    方向列表 = 設定.回測方向
    start    = 設定.回測開始日期
    end      = 設定.回測結束日期

    print(f"\n回測區間：{start} ～ {end}")
    print(f"回測標的：{symbols}")
    print(f"回測方向：{方向列表}")
    print(f"移動停利：{設定.移動停利比例 * 100}%")
    print(f"手續費成本：{round(_總成本比例() * 100, 4)}%（來回）")

    all_trades = []

    for symbol in symbols:
        for 方向 in 方向列表:

            print(f"\n[回測] {symbol} {方向}方")

            # 載入資料
            if csv_paths and symbol in csv_paths:
                try:
                    df = 載入CSV(csv_paths[symbol])
                except Exception as e:
                    print(f"  ⚠️ CSV 載入失敗：{e}")
                    continue
            elif api is not None:
                df = 載入API(api, symbol)
            else:
                print(f"  ⚠️ 無資料來源，略過")
                continue

            if df is None or len(df) < 設定.大量區回看根數 + 設定.最少K棒緩衝:
                print(f"  ⚠️ 資料不足（{0 if df is None else len(df)} 根），略過")
                continue

            print(f"  載入 {len(df)} 根K棒")

            # 日K趨勢表
            趨勢表 = {}
            if 設定.日K趨勢過濾 and api is not None:
                print(f"  載入日K趨勢...")
                趨勢表 = 建立日K趨勢表(api, symbol)
                多日 = sum(1 for v in 趨勢表.values() if v == "多")
                空日 = sum(1 for v in 趨勢表.values() if v == "空")
                print(f"  趨勢分布：多方 {多日} 天 / 空方 {空日} 天")

            # 三種進場模式一起跑
            for 模式 in 設定.進場模式列表:

                trades = 回測單標的(df, symbol, 方向, 模式, 趨勢表)
                stats  = 計算績效(trades)
                印出績效(f"{symbol} {方向}方【{模式}】", stats)

                if not trades.empty:
                    trades["進場模式"] = 模式
                    存CSV(trades, f"{symbol}_{方向}_{模式}")
                    all_trades.append(trades)

    if not all_trades:
        print("\n無任何交易紀錄")
        return pd.DataFrame()

    合併 = pd.concat(all_trades, ignore_index=True)

    # 整體按模式分開顯示
    print("\n" + "=" * 60)
    print("  各進場模式整體比較")
    print("=" * 60)
    for 模式 in 設定.進場模式列表:
        子集 = 合併[合併["進場模式"] == 模式]
        if not 子集.empty:
            印出績效(f"整體【{模式}】", 計算績效(子集))

    存CSV(合併, "ALL_summary")

    return 合併


# ==========================================================
# 直接執行
# ==========================================================

if __name__ == "__main__":

    import shioaji as sj

    print("登入永豐 API...")
    api = sj.Shioaji(simulation=False)
    api.login(
        api_key=設定.永豐API_KEY,
        secret_key=設定.永豐SECRET_KEY,
    )
    time.sleep(2)   # 等待合約載入

    執行回測(api=api)

    api.logout()
    print("\n登出完成")
