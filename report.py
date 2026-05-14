# ============================================================
# report.py
# 盤後自動產生每日報表
#
# 欄位：
#   股票　現價　交易狀態　日K方向　30分K方向
#   前高（3日）　大量K高點　30分平台高
#   前低（3日）　大量K低點　日MA20
#
# 特點：
#   - 可單獨執行：python report.py
#   - 報表股票池與 main.py 監控股票池分開
#   - HTML 會直接發 TG
#   - 報表版大量K搜尋「包含最後一根」，避免收盤後空白
#   - 自動複製最新報表為 index.html（GitHub Pages 首頁）
#   - 自動執行 Git 操作（有變更時）
# ============================================================

from __future__ import annotations

import os
import requests
import subprocess
import shutil
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
# 交易狀態
# ==========================================================

def _交易狀態(
    狀態: str,
    距前高,
    距平台高,
    距前低,
    距大量K高,
    距大量K低,
) -> str:

    # =====================================================
    # 趨勢多
    # =====================================================

    if 狀態 == "趨勢多":

        if (
            (距前高 is not None and 距前高 < 1)
            or
            (距平台高 is not None and 距平台高 < 1)
            or
            (距大量K高 is not None and 距大量K高 < 1)
        ):
            return "🔥 多方趨勢｜等突破"

        return "⭕ 多方趨勢｜可觀察"

    # =====================================================
    # 趨勢空
    # =====================================================

    if 狀態 == "趨勢空":

        if (
            (距前低 is not None and 距前低 < 1)
            or
            (距大量K低 is not None and 距大量K低 < 1)
        ):
            return "⚠️ 空方趨勢｜不追空"

        if (
            (距平台高 is not None and 距平台高 < 1)
            or
            (距大量K高 is not None and 距大量K高 < 1)
        ):
            return "👀 空方趨勢｜等反彈空"

        return "🔥 空方趨勢｜等跌破"

    # =====================================================
    # 盤整
    # =====================================================

    return "💤 盤整｜等方向"


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


def _價位文字(items) -> str:
    有效價位 = []
    for 名稱, 價位 in items:
        if 價位 is None or 價位 == "-":
            continue
        有效價位.append(f"{名稱}{價位}")
    return "、".join(有效價位) if 有效價位 else "-"


def _明日備註(
    日K方向: str,
    分K方向30: str,
    前高,
    大量high,
    平台高,
    前低,
    大量low,
    平台低,
    日ma20,
) -> str:
    壓力 = _價位文字([
        ("前高", 前高),
        ("大量K高", 大量high),
        ("30分平台高", 平台高),
    ])
    支撐 = _價位文字([
        ("前低", 前低),
        ("大量K低", 大量low),
        ("30分平台低", 平台低),
        ("日MA20", 日ma20),
    ])

    if 日K方向 == "多" and 分K方向30 == "多":
        劇本 = "偏多，明日看突破壓力，回檔看支撐"
    elif 日K方向 == "空" and 分K方向30 == "空":
        劇本 = "偏空，明日看跌破支撐，反彈看壓力"
    elif 日K方向 == "多" and 分K方向30 == "空":
        劇本 = "日多30空，多方回檔，先看支撐有沒有守"
    elif 日K方向 == "空" and 分K方向30 == "多":
        劇本 = "日空30多，空方反彈，先看壓力有沒有過"
    else:
        劇本 = "盤整，先看區間上下緣"

    return f"{劇本}｜壓力：{壓力}｜支撐：{支撐}"


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

    距前高 = None
    if 前高 is not None:
        距前高 = round((前高 - 現價) / 現價 * 100, 2)

    距大量K高 = None
    if 大量high is not None:
        距大量K高 = round((大量high - 現價) / 現價 * 100, 2)

    距平台高 = None
    if 平台高 is not None:
        距平台高 = round((平台高 - 現價) / 現價 * 100, 2)

    距前低 = None
    if 前低 is not None:
        距前低 = round((現價 - 前低) / 現價 * 100, 2)

    距大量K低 = None
    if 大量low is not None:
        距大量K低 = round((現價 - 大量low) / 現價 * 100, 2)

    距日MA20 = None
    if 日ma20 is not None:
        距日MA20 = round((現價 - 日ma20) / 現價 * 100, 2)

    return {
        # =====================================================
        # 基本
        # =====================================================
        "股票": f"{symbol} {名稱}",
        "現價": round(現價, 2),
        "交易狀態": _交易狀態(
            狀態,
            距前高,
            距平台高,
            距前低,
            距大量K高,
            距大量K低,
        ),

        # =====================================================
        # 方向
        # =====================================================
        "日K方向": 日K方向,
        "30分K方向": 分K方向30,

        # =====================================================
        # 壓力區
        # =====================================================
        "前高（3日）": 前高,
        "大量K高點": 大量high if 大量high is not None else "-",
        "30分平台高": 平台高,

        # =====================================================
        # 支撐區
        # =====================================================
        "前低（3日）": 前低,
        "大量K低點": 大量low if 大量low is not None else "-",
        "30分平台低": 平台低,
        "日MA20": 日ma20,

        # =====================================================
        # 明日劇本
        # =====================================================
        "明日備註": _明日備註(
            日K方向,
            分K方向30,
            前高,
            大量high,
            平台高,
            前低,
            大量low,
            平台低,
            日ma20,
        ),
    }


# ==========================================================
# Telegram HTML
# ==========================================================

def _自動提交報表(root_index_path: str, html_path: str) -> None:
    """
    自動執行 Git 操作：
    - 檢查是否有變更
    - 有變更時執行 git add, commit, push
    """
    try:
        # 檢查是否在 Git 倉庫內
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            print("  ⚠️ 非 Git 倉庫，略過自動提交")
            return

        # 檢查 git status（是否有變更）
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )

        changes = status_result.stdout.strip()
        
        # 如果沒有變更就返回
        if not changes:
            print("  ✅ 無變更，略過 Git 操作")
            return

        print("  🔄 檢測到變更，執行 Git 操作...")

        # git add .
        subprocess.run(
            ["git", "add", "."],
            timeout=10,
            check=True,
        )
        print("    git add . ✅")

        # git commit
        subprocess.run(
            ["git", "commit", "-m", "auto update report"],
            timeout=10,
            check=True,
        )
        print("    git commit ✅")

        # git push
        subprocess.run(
            ["git", "push"],
            timeout=30,
            check=True,
        )
        print("    git push ✅")

        print("  📤 Git 操作完成")

    except subprocess.TimeoutExpired:
        print("  ⚠️ Git 操作超時")
    except subprocess.CalledProcessError as e:
        print(f"  ⚠️ Git 操作失敗：{e}")
    except Exception as e:
        print(f"  ⚠️ Git 操作例外：{e}")


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

    csv_path = f"{報表設定.報表輸出目錄}/report_{today}.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"  CSV：{csv_path}")

    html_path = f"{報表設定.報表輸出目錄}/report_{today}.html"
    _輸出HTML(df, html_path, today)
    print(f"  HTML：{html_path}")

    _發送HTML到TG(html_path)

    # =====================================================
    # 複製最新報表到根目錄 index.html
    # =====================================================
    root_index_path = "index.html"
    try:
        shutil.copy2(html_path, root_index_path)
        print(f"  根目錄 index.html：已更新")
    except Exception as e:
        print(f"  ⚠️ 複製 index.html 失敗：{e}")

    print("📊 報表完成")

    # =====================================================
    # 自動執行 Git 操作
    # =====================================================
    _自動提交報表(root_index_path, html_path)

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
                cells += f'<td style="text-align:left;min-width:360px;line-height:1.5">{v}</td>'
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
    width: min(100%, 1280px);
    margin: 0 auto;
    padding: 20px;
  }}
  .page-head {{
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 14px;
  }}
  h2 {{
    margin: 0;
    color: #1d2733;
    font-size: 24px;
    line-height: 1.2;
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
    min-width: 1080px;
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
    .page-head {{
      display: block;
      margin-bottom: 10px;
    }}
    h2 {{ font-size: 20px; }}
    .table-wrap {{
      border-radius: 6px;
      box-shadow: none;
    }}
    table {{ min-width: 980px; }}
    th, td {{
      padding: 9px 10px;
      font-size: 12px;
    }}
    td[style*="min-width:360px"] {{
      min-width: 260px !important;
      white-space: normal;
    }}
  }}
</style>
</head>
<body>
<main>
<h2>📊 盤後報表 {today}</h2>
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
