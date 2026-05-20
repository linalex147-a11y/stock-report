from __future__ import annotations

import os
import shutil
import subprocess
import requests
from datetime import datetime, timedelta

import pandas as pd
import shioaji as sj

from config import 設定
from data_loader import get_kbars
from report_config import 報表設定

print("🔥 AI 劇本版報表 啟動")


# =========================================================
# 基本設定
# =========================================================

def _cfg(name: str, default=None):
    return getattr(報表設定, name, default)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _round(v):
    try:
        return round(float(v), 2)
    except Exception:
        return None


def _price(v):
    if v is None:
        return "-"
    try:
        fv = float(v)
        if fv.is_integer():
            return str(int(fv))
        return str(_round(fv))
    except Exception:
        return "-"


def _股票池() -> dict:
    return dict(_cfg("報表標的", {}))


def _分類設定() -> dict:
    return dict(_cfg("分類設定", {}))


# =========================================================
# API
# =========================================================

def 登入():
    api = sj.Shioaji(simulation=False)
    api.login(
        api_key=設定.永豐API_KEY,
        secret_key=設定.永豐SECRET_KEY,
    )
    return api


# =========================================================
# K棒
# =========================================================

def _load_kbars(api, symbol):
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=_cfg("回看天數", 10))).strftime("%Y-%m-%d")

    try:
        df = get_kbars(api, symbol, start=start, end=end)
        if df is None or len(df) == 0:
            return None
        df = df.sort_index()
        return df[["open", "high", "low", "close", "volume"]].copy()
    except Exception as e:
        print(f"  ⚠️ {symbol} 載入失敗：{e}")
        return None


def _resample_30(df):
    return df.resample(
        "30min",
        closed="left",
        label="left",
    ).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()


def _resample_day(df):
    return df.resample(
        "D",
        closed="left",
        label="left",
    ).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()


# =========================================================
# 指標
# =========================================================

def _add_ma(df):
    df = df.copy()
    df["MA20"] = df["close"].rolling(20, min_periods=1).mean()
    df["MA60"] = df["close"].rolling(60, min_periods=1).mean()
    df["VMA10"] = df["volume"].rolling(10, min_periods=1).mean()
    return df


def _方向(df):
    df = _add_ma(df)
    last = df.iloc[-1]

    close = float(last["close"])
    ma20 = float(last["MA20"])
    ma60 = float(last["MA60"])

    if close >= ma20 and ma20 >= ma60:
        return "多"
    if close <= ma20 and ma20 <= ma60:
        return "空"
    if close >= ma20:
        return "多"
    return "空"


# =========================================================
# 壓力 / 支撐
# =========================================================

def _前高前低(df_day):
    recent = df_day.iloc[-_cfg("前高前低天數", 3):]
    前高 = _round(recent["high"].max())
    前低 = _round(recent["low"].min())
    return 前高, 前低


def _平台(df30):
    recent = df30.iloc[-_cfg("平台回看根數", 6):]
    平台高 = _round(recent["high"].max())
    平台低 = _round(recent["low"].min())
    return 平台高, 平台低


def _ma(df_day):
    df_day = _add_ma(df_day)
    ma20 = _round(df_day.iloc[-1]["MA20"])
    ma60 = _round(df_day.iloc[-1]["MA60"])
    return ma20, ma60


def _壓力支撐(現價, 前高, 前低, 平台高, 平台低, ma20, ma60):
    壓力候選 = []
    for x in [前高, 平台高, ma60]:
        if x is not None and x > 現價:
            壓力候選.append(x)

    支撐候選 = []
    for x in [前低, 平台低, ma20]:
        if x is not None and x <= 現價:
            支撐候選.append(x)

    壓力候選 = sorted(list(set(壓力候選)))
    支撐候選 = sorted(list(set(支撐候選)), reverse=True)

    壓力 = " / ".join([_price(x) for x in 壓力候選[:2]]) if 壓力候選 else "-"
    支撐 = " / ".join([_price(x) for x in 支撐候選[:2]]) if 支撐候選 else "-"

    return 壓力, 支撐


# =========================================================
# 劇本 / 狀態
# =========================================================

def _交易狀態(日方向, 方向30):
    if 日方向 == "多" and 方向30 == "多":
        return "⭕ 偏多｜可觀察"
    if 日方向 == "空" and 方向30 == "空":
        return "⚠️ 偏空｜不追空"
    if 日方向 == "空" and 方向30 == "多":
        return "👀 空方反彈｜等壓力空"
    if 日方向 == "多" and 方向30 == "空":
        return "⚠️ 多方回檔｜看支撐"
    return "💤 盤整｜等方向"


def _量價(df5, df30):
    df5 = _add_ma(df5)
    df30 = _add_ma(df30)

    v1 = float(df5.iloc[-1]["volume"])
    v1m = float(df5.iloc[-1]["VMA10"])
    v2 = float(df30.iloc[-1]["volume"])
    v2m = float(df30.iloc[-1]["VMA10"])

    ratio = 1.0
    if v1m > 0:
        ratio = max(ratio, v1 / v1m)
    if v2m > 0:
        ratio = max(ratio, v2 / v2m)

    return "量增" if ratio >= 1.2 else "量縮"


def _AI劇本(日方向, 方向30, 現價, ma60):
    if 日方向 == "空" and 方向30 == "空":
        if ma60 is not None and 現價 < ma60:
            return "日30同步偏空，反彈不過月線，空方仍有優勢。"
        return "日30同步偏空，壓力沉重，先以反彈壓回看待。"

    if 日方向 == "多" and 方向30 == "多":
        if ma60 is not None and 現價 > ma60:
            return "日30同步偏多，回檔守月線，多方仍有優勢。"
        return "日30同步偏多，若能放量突破壓力，多方可再延伸。"

    if 日方向 == "空" and 方向30 == "多":
        return "日空30多，屬空方反彈，先看壓力是否過得去。"

    if 日方向 == "多" and 方向30 == "空":
        return "日多30空，屬多方回檔，先看支撐能不能守住。"

    return "方向不明，先觀察區間上下緣。"


# =========================================================
# 單檔分析
# =========================================================

def _analyze(api, symbol):
    name = _股票池().get(symbol, symbol)

    df5 = _load_kbars(api, symbol)
    if df5 is None or len(df5) < 20:
        return None

    df30 = _resample_30(df5)
    df_day = _resample_day(df5)

    if len(df30) == 0 or len(df_day) == 0:
        return None

    日方向 = _方向(df_day)
    方向30 = _方向(df30)

    前高, 前低 = _前高前低(df_day)
    平台高, 平台低 = _平台(df30)
    ma20, ma60 = _ma(df_day)

    現價 = float(df5.iloc[-1]["close"])

    壓力, 支撐 = _壓力支撐(
        現價,
        前高,
        前低,
        平台高,
        平台低,
        ma20,
        ma60,
    )

    return {
        "股票": f"{symbol} {name}",
        "現價": _round(現價),
        "交易狀態": _交易狀態(日方向, 方向30),
        "日K方向": 日方向,
        "30分K方向": 方向30,
        "量價判讀": _量價(df5, df30),
        "壓力": 壓力,
        "支撐": 支撐,
        "AI交易劇本": _AI劇本(日方向, 方向30, 現價, ma60),
    }


# =========================================================
# 分類
# =========================================================

def _row_cats(symbol):
    matched = []
    for cat, symbols in _分類設定().items():
        if symbol in symbols:
            matched.append(cat)
    if not matched:
        matched = ["其他"]
    return ",".join(matched)


# =========================================================
# HTML
# =========================================================

def _html(df, path):
    categories = _分類設定()

    buttons = '<button class="btn active" data-cat="全部">全部</button>\n'
    for cat in categories.keys():
        buttons += f'<button class="btn" data-cat="{cat}">{cat}</button>\n'

    rows = ""
    for _, row in df.iterrows():
        stock_text = str(row["股票"])
        symbol = stock_text.split()[0]
        cats = _row_cats(symbol)

        rows += f"""
<tr data-cats="{cats}">
<td>{row["股票"]}</td>
<td>{row["現價"]}</td>
<td>{row["交易狀態"]}</td>
<td>{row["日K方向"]}</td>
<td>{row["30分K方向"]}</td>
<td>{row["量價判讀"]}</td>
<td>{row["壓力"]}</td>
<td>{row["支撐"]}</td>
<td>{row["AI交易劇本"]}</td>
</tr>
"""

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI盤後結構報表</title>
<style>
body {{
    font-family: Arial, "Microsoft JhengHei", sans-serif;
    background:#f5f5f5;
    margin:0;
    padding:20px;
}}
.container {{
    max-width:1600px;
    margin:auto;
}}
h1 {{
    margin-bottom:20px;
}}
.toolbar {{
    margin-bottom:15px;
    display:flex;
    flex-wrap:wrap;
    gap:8px;
}}
.btn {{
    padding:8px 14px;
    border:none;
    border-radius:20px;
    cursor:pointer;
    background:#e5e7eb;
}}
.active {{
    background:#2563eb;
    color:white;
}}
table {{
    width:100%;
    border-collapse:collapse;
    background:white;
}}
th {{
    background:#1f2937;
    color:white;
    padding:12px;
}}
td {{
    padding:12px;
    border-bottom:1px solid #ddd;
}}
tr:hover {{
    background:#f9fafb;
}}
</style>
</head>
<body>
<div class="container">
<h1>📊 AI盤後結構報表</h1>
<div class="toolbar">
{buttons}
</div>
<table>
<thead>
<tr>
<th>股票</th>
<th>現價</th>
<th>交易狀態</th>
<th>日K方向</th>
<th>30分K方向</th>
<th>量價判讀</th>
<th>壓力</th>
<th>支撐</th>
<th>AI交易劇本</th>
</tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
</div>

<script>
const buttons = document.querySelectorAll(".btn");
const rows = document.querySelectorAll("tbody tr");

buttons.forEach(btn => {{
    btn.addEventListener("click", () => {{
        buttons.forEach(b => b.classList.remove("active"));
        btn.classList.add("active");

        const cat = btn.dataset.cat;
        rows.forEach(row => {{
            const cats = row.dataset.cats || "";
            if (cat === "全部") {{
                row.style.display = "";
            }} else {{
                row.style.display = cats.includes(cat) ? "" : "none";
            }}
        }});
    }});
}});
</script>
</body>
</html>
"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


# =========================================================
# TG / Git
# =========================================================

def _send_tg(html_path):
    try:
        token = getattr(設定, "TG_TOKEN", "")
        chat_id = getattr(設定, "TG_CHAT_ID", "")
        if not token or not chat_id:
            return

        url = f"https://api.telegram.org/bot{token}/sendDocument"
        with open(html_path, "rb") as f:
            r = requests.post(
                url,
                data={
                    "chat_id": chat_id,
                    "caption": "📊 AI盤後結構報表",
                },
                files={"document": f},
                timeout=30,
            )

        if r.status_code == 200:
            print("📨 HTML 已發送 TG")
        else:
            print("❌ TG 發送失敗")
            print(r.text)
    except Exception as e:
        print("❌ TG 發送錯誤")
        print(e)


def _git_sync():
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if not status.stdout.strip():
            print("✅ 無變更，略過 Git 操作")
            return

        print("🔄 自動 Git 同步...")

        subprocess.run(["git", "add", "."], check=True, timeout=20)
        subprocess.run(["git", "commit", "-m", "auto update report"], check=True, timeout=20)
        subprocess.run(["git", "push"], check=True, timeout=60)

        print("✅ GitHub 已更新")
    except Exception as e:
        print("❌ Git 同步失敗")
        print(e)


# =========================================================
# 主流程
# =========================================================

def 產生報表():
    api = 登入()

    rows = []
    print("📊 產生 AI 盤後結構報表...")

    for symbol in _股票池().keys():
        try:
            row = _analyze(api, symbol)
            if row:
                rows.append(row)
                print(f"  {symbol} ✅")
        except Exception as e:
            print(f"  {symbol} ❌ {e}")

    if len(rows) == 0:
        print("❌ 無資料")
        api.logout()
        return

    df = pd.DataFrame(rows)

    outdir = _cfg("報表輸出目錄", "report_out")
    os.makedirs(outdir, exist_ok=True)
    os.makedirs("docs", exist_ok=True)

    today = _today()
    csv_path = os.path.join(outdir, f"report_{today}.csv")
    html_path = os.path.join(outdir, f"report_{today}.html")

    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    _html(df, html_path)

    # 同步首頁與 docs
    shutil.copy2(html_path, "index.html")
    shutil.copy2(html_path, "docs/index.html")

    print("✅ 已同步：")
    print("  index.html")
    print("  docs/index.html")
    print(f"CSV：{csv_path}")
    print(f"HTML：{html_path}")

    _send_tg(html_path)
    _git_sync()

    api.logout()


if __name__ == "__main__":
    產生報表()
