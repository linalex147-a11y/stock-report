# ============================================================
# report.py
# 盤後自動產生每日報表
#
# 欄位：
#   股票　現價　狀態　日K方向　30分K方向
#   前高（3日）　大量K高點　30分平台高
#   距前高%　距大量K高%　距平台高%
#   前低（3日）　大量K低點　日MA20
#   距前低%　距大量K低%　距日MA20%
#
# 特點：
#   - 可單獨執行：python report.py
#   - 報表股票池與 main.py 監控股票池分開
#   - HTML 會直接發 TG
#   - 報表版大量K搜尋「包含最後一根」，避免收盤後空白
# ============================================================

from __future__ import annotations

import os
import requests
from datetime import datetime, timedelta
from typing import Optional, Tuple

import pandas as pd
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
    start = (datetime.now() - timedelta(days=報表設定.大量區回看根數)).strftime("%Y-%m-%d")

    try:
        df = get_kbars(api, symbol, start=start, end=end)
        if df is None or len(df) == 0:
            return None
        return df.sort_index()
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

def _add_ma20(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["MA20"] = df["close"].rolling(報表設定.MA週期, min_periods=1).mean()
    return df


def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    報表用簡化指標：
    - MA5 / MA10 / MA20
    """
    df = df.copy()
    df["MA5"] = df["close"].rolling(5, min_periods=1).mean()
    df["MA10"] = df["close"].rolling(10, min_periods=1).mean()
    df["MA20"] = df["close"].rolling(20, min_periods=1).mean()
    return df


# ==========================================================
# 方向判斷
# ==========================================================

def _方向(df: pd.DataFrame) -> str:
    """
    最後一根收盤 vs MA20
    多 / 空
    """
    df = _add_ma20(df)
    last = df.iloc[-1]
    close = float(last["close"])
    ma20 = float(last["MA20"])
    return "多" if close > ma20 else "空"


def _綜合狀態(日K方向: str, 分K方向: str) -> str:
    if 日K方向 == 分K方向 == "多":
        return "趨勢多"
    if 日K方向 == 分K方向 == "空":
        return "趨勢空"
    return "盤整"


# ==========================================================
# 關鍵壓力 / 支撐
# ==========================================================

def _前高前低(df_day: pd.DataFrame, 天數: int = 3):
    recent = df_day.iloc[-天數:]
    return round(float(recent["high"].max()), 2), round(float(recent["low"].min()), 2)


def _大量K_high_low_報表(df5: pd.DataFrame, 方向: str) -> Tuple[Optional[float], Optional[float]]:
    """
    報表專用大量K搜尋：
    - 包含最後一根（盤後已封棒）
    - 只在報表中使用，不影響即時策略
    """
    df = _add_indicators(df5.copy())

    回看 = 報表設定.大量區回看根數
    均線根數 = 報表設定.大量K量能均線根數

    window = df.iloc[-回看:]

    if len(window) < 均線根數:
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

        if vol < vol_ma * 報表設定.大量K量能倍率:
            continue

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

        分數 = (vol_ratio, body_ratio, range_ratio, vol)
        if 最佳分數 is None or 分數 > 最佳分數:
            最佳分數 = 分數
            最佳row = row

    if 最佳row is None:
        return None, None

    return round(float(最佳row["high"]), 2), round(float(最佳row["low"]), 2)


def _30分平台高低(df30: pd.DataFrame, 回看根數: int = 6):
    recent = df30.iloc[-回看根數:]
    return round(float(recent["high"].max()), 2), round(float(recent["low"].min()), 2)


def _日MA20(df_day: pd.DataFrame) -> Optional[float]:
    df = _add_ma20(df_day)
    return round(float(df.iloc[-1]["MA20"]), 2)


def _距離百分比(現價: float, 價格: Optional[float], 類型: str) -> Optional[float]:
    if 價格 is None:
        return None
    if 現價 <= 0:
        return None

    if 類型 == "上方":
        return round((價格 - 現價) / 現價 * 100, 2)
    if 類型 == "下方":
        return round((現價 - 價格) / 現價 * 100, 2)
    return None


# ==========================================================
# 單檔報表
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

    日K方向 = _方向(df_day)
    分K方向30 = _方向(df30)
    狀態 = _綜合狀態(日K方向, 分K方向30)

    前高, 前低 = _前高前低(df_day, 天數=報表設定.前高前低天數)
    大量high, 大量low = _大量K_high_low_報表(df5, 日K方向)
    平台高, 平台低 = _30分平台高低(df30, 回看根數=報表設定.平台回看根數)
    日ma20 = _日MA20(df_day)

    現價 = float(df5["close"].iloc[-1])

    return {
        # =====================================================
        # 基本
        # =====================================================
        "股票": f"{symbol} {名稱}",
        "現價": round(現價, 2),
        "狀態": 狀態,

        # =====================================================
        # 方向
        # =====================================================
        "日K方向": 日K方向,
        "30分K方向": 分K方向30,

        # =====================================================
        # 壓力
        # =====================================================
        "前高（3日）": 前高,
        "大量K高點": 大量high if 大量high is not None else "-",
        "30分平台高": 平台高,

        # =====================================================
        # 距離壓力
        # =====================================================
        "距前高%": _距離百分比(現價, 前高, "上方") if 前高 else "-",
        "距大量K高%": _距離百分比(現價, 大量high, "上方") if 大量high else "-",
        "距平台高%": _距離百分比(現價, 平台高, "上方") if 平台高 else "-",

        # =====================================================
        # 支撐
        # =====================================================
        "前低（3日）": 前低,
        "大量K低點": 大量low if 大量low is not None else "-",
        "日MA20": 日ma20,

        # =====================================================
        # 距離支撐
        # =====================================================
        "距前低%": _距離百分比(現價, 前低, "下方") if 前低 else "-",
        "距大量K低%": _距離百分比(現價, 大量low, "下方") if 大量low else "-",
        "距日MA20%": _距離百分比(現價, 日ma20, "下方") if 日ma20 else "-",
    }


# ==========================================================
# Telegram HTML
# ==========================================================

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

    print(f"\n📊 產生盤後報表...")

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

    os.makedirs(報表設定.報表輸出目錄, exist_ok=True)
    today = _today_str()

    # ── CSV ──
    csv_path = f"{報表設定.報表輸出目錄}/report_{today}.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"  CSV：{csv_path}")

    # ── HTML ──
    html_path = f"{報表設定.報表輸出目錄}/report_{today}.html"
    _輸出HTML(df, html_path, today)
    print(f"  HTML：{html_path}")

    # ── TG ──
    _發送HTML到TG(html_path)

    print("📊 報表完成")

    if should_logout:
        api.logout()

    return df


# ==========================================================
# HTML
# ==========================================================

_狀態顏色 = {
    "趨勢多": "#1a7a1a",
    "趨勢空": "#a01010",
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
    bg = {"趨勢多": "#e8f5e9", "趨勢空": "#fce4e4", "盤整": "#f0f0f0"}.get(v, "#fff")
    return f'<td style="color:{color};background:{bg};font-weight:bold;text-align:center">{v}</td>'


def _輸出HTML(df: pd.DataFrame, path: str, today: str) -> None:
    cols = df.columns.tolist()

    # 同性質欄位放一起，已在 return dict 的順序完成
    rows_html = ""
    for _, row in df.iterrows():
        cells = ""
        for col in cols:
            v = row[col]
            if col in ("日K方向", "30分K方向"):
                cells += _cell_方向(str(v))
            elif col == "狀態":
                cells += _cell_狀態(str(v))
            else:
                cells += f'<td style="text-align:center">{v}</td>'
        rows_html += f"<tr>{cells}</tr>\n"

    headers = "".join(f"<th>{c}</th>" for c in cols)

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>盤後報表 {today}</title>
<style>
  body {{ font-family: Arial, sans-serif; padding: 20px; background: #fafafa; }}
  h2   {{ color: #333; }}
  table {{
    border-collapse: collapse;
    width: 100%;
    background: #fff;
    box-shadow: 0 1px 4px rgba(0,0,0,.1);
    border-radius: 6px;
    overflow: hidden;
  }}
  th {{
    background: #2c3e50;
    color: #fff;
    padding: 10px 14px;
    text-align: center;
    font-size: 13px;
    position: sticky;
    top: 0;
  }}
  td {{
    padding: 9px 14px;
    border-bottom: 1px solid #eee;
    font-size: 13px;
  }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #f5f8ff; }}
  .ts {{ color: #888; font-size: 11px; margin-top: 8px; }}
</style>
</head>
<body>
<h2>📊 盤後報表 {today}</h2>
<table>
  <thead><tr>{headers}</tr></thead>
  <tbody>{rows_html}</tbody>
</table>
<p class="ts">產生時間：{_now_str()}</p>
</body>
</html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    產生報表()
