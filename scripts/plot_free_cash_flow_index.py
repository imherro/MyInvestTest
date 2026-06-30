from __future__ import annotations

import argparse
import json
import os
import random
import string
import sys
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import tushare as ts


DEFAULT_INDEX_NAME = "富时中国A股自由现金流聚焦全收益指数"
DEFAULT_TRADINGVIEW_SYMBOL = "FTSE:FCFQCD.TR"
TUSHARE_MARKETS = ("CSI", "SSE", "SZSE", "CICC", "SW", "MSCI", "OTH")
TUSHARE_ENDPOINTS = ("index_daily", "index_weekly", "index_monthly", "index_global")


@dataclass
class IndexInfo:
    ts_code: str
    name: str
    market: str = ""
    publisher: str = ""
    category: str = ""
    base_date: str = ""
    list_date: str = ""


@dataclass
class SeriesResult:
    data: pd.DataFrame
    source: str
    notes: list[str]


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def get_tushare_token() -> str:
    for key in ("TUSHARE_TOKEN", "TS_TOKEN", "TUSHARE_KEY"):
        token = os.environ.get(key)
        if token:
            return token
    raise RuntimeError("没有找到 Tushare token。请在 .env 里设置 TUSHARE_TOKEN=你的token。")


def parse_yyyymmdd(value: str) -> datetime:
    return datetime.strptime(value, "%Y%m%d")


def yyyymmdd(value: datetime) -> str:
    return value.strftime("%Y%m%d")


def normalize_trade_date(value: Any) -> str:
    text = str(value)
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return pd.to_datetime(value).strftime("%Y-%m-%d")


def chunk_date_ranges(start_date: str, end_date: str, years: int = 5) -> Iterable[tuple[str, str]]:
    start = parse_yyyymmdd(start_date)
    end = parse_yyyymmdd(end_date)
    cursor = start
    while cursor <= end:
        chunk_end = min(datetime(cursor.year + years, 1, 1) - pd.Timedelta(days=1), end)
        yield yyyymmdd(cursor), yyyymmdd(chunk_end)
        cursor = chunk_end + pd.Timedelta(days=1)


def tushare_client() -> Any:
    token = get_tushare_token()
    ts.set_token(token)
    return ts.pro_api()


def score_index(row: pd.Series) -> int:
    name = str(row.get("name", ""))
    publisher = str(row.get("publisher", ""))
    score = 0
    if name == DEFAULT_INDEX_NAME:
        score += 200
    for term in ("中国A股", "A股", "自由现金流", "全收益"):
        if term in name:
            score += 20
    if "FTSE" in publisher or "富时" in publisher:
        score += 20
    if "中债" in name or "股债" in name:
        score -= 80
    return score


def find_index_info(pro: Any, preferred_code: str | None = None) -> tuple[IndexInfo, pd.DataFrame]:
    frames: list[pd.DataFrame] = []
    fields = "ts_code,name,market,publisher,category,base_date,list_date"
    for market in TUSHARE_MARKETS:
        try:
            frame = pro.index_basic(market=market, fields=fields)
        except Exception as exc:
            print(f"Tushare index_basic({market}) 查询失败：{exc}", file=sys.stderr)
            continue
        if frame is not None and not frame.empty:
            frames.append(frame)

    if not frames:
        raise RuntimeError("Tushare index_basic 没有返回指数元数据。")

    all_indexes = pd.concat(frames, ignore_index=True).drop_duplicates("ts_code")
    if preferred_code:
        selected = all_indexes[all_indexes["ts_code"] == preferred_code]
        if selected.empty:
            raise RuntimeError(f"Tushare 指数元数据里找不到 {preferred_code}。")
        row = selected.iloc[0]
        return row_to_index_info(row), selected

    candidates = all_indexes[
        all_indexes["name"].astype(str).str.contains("自由现金流", regex=False, na=False)
    ].copy()
    if candidates.empty:
        raise RuntimeError("Tushare 指数元数据里没有匹配“自由现金流”的指数。")

    candidates["score"] = candidates.apply(score_index, axis=1)
    candidates = candidates.sort_values(["score", "list_date"], ascending=[False, False])
    return row_to_index_info(candidates.iloc[0]), candidates


def row_to_index_info(row: pd.Series) -> IndexInfo:
    return IndexInfo(
        ts_code=str(row.get("ts_code", "")),
        name=str(row.get("name", "")),
        market=str(row.get("market", "")),
        publisher=str(row.get("publisher", "")),
        category=str(row.get("category", "")),
        base_date="" if pd.isna(row.get("base_date", "")) else str(row.get("base_date", "")),
        list_date="" if pd.isna(row.get("list_date", "")) else str(row.get("list_date", "")),
    )


def normalize_tushare_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty or "trade_date" not in frame.columns or "close" not in frame.columns:
        return pd.DataFrame()

    data = frame.copy()
    data["trade_date"] = data["trade_date"].map(normalize_trade_date)
    data["close"] = pd.to_numeric(data["close"], errors="coerce")
    for col in ("open", "high", "low", "pre_close", "change", "pct_chg", "vol", "amount"):
        if col in data.columns:
            data[col] = pd.to_numeric(data[col], errors="coerce")
    data = data.dropna(subset=["trade_date", "close"])
    data = data.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
    return data


def fetch_tushare_endpoint(
    pro: Any,
    endpoint: str,
    ts_code: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    chunks: list[pd.DataFrame] = []
    for chunk_start, chunk_end in chunk_date_ranges(start_date, end_date):
        try:
            frame = getattr(pro, endpoint)(ts_code=ts_code, start_date=chunk_start, end_date=chunk_end)
        except Exception as exc:
            print(f"Tushare {endpoint}({ts_code}) 查询失败：{exc}", file=sys.stderr)
            continue
        if frame is not None and not frame.empty:
            chunks.append(frame)

    if not chunks:
        return pd.DataFrame()
    return normalize_tushare_frame(pd.concat(chunks, ignore_index=True))


def fetch_tushare_pro_bar(ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    chunks: list[pd.DataFrame] = []
    for chunk_start, chunk_end in chunk_date_ranges(start_date, end_date):
        try:
            frame = ts.pro_bar(ts_code=ts_code, asset="I", start_date=chunk_start, end_date=chunk_end)
        except Exception as exc:
            print(f"Tushare pro_bar({ts_code}) 查询失败：{exc}", file=sys.stderr)
            continue
        if frame is not None and not frame.empty:
            chunks.append(frame)

    if not chunks:
        return pd.DataFrame()
    return normalize_tushare_frame(pd.concat(chunks, ignore_index=True))


def fetch_tushare_series(
    pro: Any,
    index_info: IndexInfo,
    start_date: str,
    end_date: str,
) -> SeriesResult:
    notes: list[str] = []
    for endpoint in TUSHARE_ENDPOINTS:
        frame = fetch_tushare_endpoint(pro, endpoint, index_info.ts_code, start_date, end_date)
        if not frame.empty:
            notes.append(f"Tushare {endpoint} 返回 {len(frame)} 条。")
            return SeriesResult(frame, f"Tushare {endpoint}", notes)
        notes.append(f"Tushare {endpoint} 未返回行情。")

    frame = fetch_tushare_pro_bar(index_info.ts_code, start_date, end_date)
    if not frame.empty:
        notes.append(f"Tushare pro_bar(asset='I') 返回 {len(frame)} 条。")
        return SeriesResult(frame, "Tushare pro_bar(asset='I')", notes)

    notes.append("Tushare 通用指数行情接口也未返回行情。")
    return SeriesResult(pd.DataFrame(), "Tushare", notes)


def tv_session_name(prefix: str) -> str:
    suffix = "".join(random.choice(string.ascii_lowercase) for _ in range(12))
    return f"{prefix}_{suffix}"


def tv_message(method: str, params: list[Any]) -> str:
    payload = json.dumps({"m": method, "p": params}, separators=(",", ":"))
    return f"~m~{len(payload)}~m~{payload}"


def parse_tv_messages(raw: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    parts = raw.split("~m~")
    for index in range(1, len(parts), 2):
        if index + 1 >= len(parts):
            continue
        try:
            messages.append(json.loads(parts[index + 1]))
        except json.JSONDecodeError:
            continue
    return messages


def fetch_tradingview_series(symbol: str, max_bars: int) -> SeriesResult:
    try:
        import websocket
    except ImportError as exc:
        raise RuntimeError("缺少 websocket-client。请先运行：pip install websocket-client") from exc

    chart_session = tv_session_name("cs")
    ws = websocket.create_connection(
        "wss://data.tradingview.com/socket.io/websocket?from=chart%2F",
        timeout=20,
        header=["Origin: https://www.tradingview.com"],
    )

    try:
        symbol_payload = json.dumps(
            {"symbol": symbol, "adjustment": "splits", "session": "regular"},
            separators=(",", ":"),
        )
        for method, params in (
            ("set_auth_token", ["unauthorized_user_token"]),
            ("chart_create_session", [chart_session, ""]),
            ("resolve_symbol", [chart_session, "symbol_1", f"={symbol_payload}"]),
            ("create_series", [chart_session, "s1", "s1", "symbol_1", "1D", max_bars]),
        ):
            ws.send(tv_message(method, params))

        series: list[dict[str, Any]] = []
        errors: list[str] = []
        start_time = time.time()
        while time.time() - start_time < 45:
            raw = ws.recv()
            if raw.startswith("~h~"):
                ws.send(raw)
                continue
            for message in parse_tv_messages(raw):
                method = message.get("m")
                if method == "timescale_update":
                    payload = message.get("p", [{}, {}])[1]
                    values = payload.get("s1", {}).get("s")
                    if values:
                        series = values
                elif method in {"series_error", "critical_error", "resolve_error"}:
                    errors.append(json.dumps(message, ensure_ascii=False))
                elif method == "series_completed":
                    return tv_series_to_result(symbol, series, errors)
        return tv_series_to_result(symbol, series, errors)
    finally:
        ws.close()


def tv_series_to_result(symbol: str, series: list[dict[str, Any]], errors: list[str]) -> SeriesResult:
    if not series:
        detail = "; ".join(errors) if errors else "TradingView 没有返回序列。"
        raise RuntimeError(f"TradingView {symbol} 没有可用数据：{detail}")

    rows: list[dict[str, Any]] = []
    for item in series:
        values = item.get("v", [])
        if len(values) < 5:
            continue
        rows.append(
            {
                "trade_date": datetime.fromtimestamp(values[0], timezone.utc).strftime("%Y-%m-%d"),
                "open": values[1],
                "high": values[2],
                "low": values[3],
                "close": values[4],
                "volume": values[5] if len(values) > 5 else None,
            }
        )

    data = pd.DataFrame(rows)
    data["close"] = pd.to_numeric(data["close"], errors="coerce")
    data = data.dropna(subset=["trade_date", "close"])
    data = data.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
    return SeriesResult(data, f"TradingView {symbol}", errors)


def filter_by_date(data: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    start = normalize_trade_date(start_date)
    end = normalize_trade_date(end_date)
    return data[(data["trade_date"] >= start) & (data["trade_date"] <= end)].copy()


def write_csv(data: pd.DataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    preferred_cols = [
        col
        for col in ("trade_date", "open", "high", "low", "close", "pre_close", "change", "pct_chg", "vol", "volume", "amount")
        if col in data.columns
    ]
    data[preferred_cols].to_csv(output_path, index=False, encoding="utf-8-sig")


def safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def write_html(
    data: pd.DataFrame,
    index_info: IndexInfo,
    source: str,
    notes: list[str],
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    points = data[["trade_date", "close"]].to_dict(orient="records")
    first = points[0]
    last = points[-1]
    min_point = min(points, key=lambda row: row["close"])
    max_point = max(points, key=lambda row: row["close"])
    meta = {
        "title": index_info.name,
        "tsCode": index_info.ts_code,
        "source": source,
        "rows": len(points),
        "dateRange": f"{first['trade_date']} 至 {last['trade_date']}",
        "latest": last,
        "minPoint": min_point,
        "maxPoint": max_point,
        "publisher": index_info.publisher,
        "baseDate": index_info.base_date,
        "listDate": index_info.list_date,
        "notes": notes,
    }

    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{index_info.name}</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #17202a;
      --muted: #687383;
      --line: #d8dee8;
      --panel: #ffffff;
      --accent: #1f7a68;
      --accent-2: #b14b36;
      --bg: #f5f7f9;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
      background: var(--bg);
      color: var(--ink);
    }}
    main {{
      width: min(1180px, calc(100vw - 32px));
      margin: 24px auto;
    }}
    header {{
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 24px;
      margin-bottom: 16px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: clamp(24px, 3vw, 36px);
      line-height: 1.15;
      letter-spacing: 0;
    }}
    .subtitle {{
      margin: 0;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.7;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(4, minmax(140px, 1fr));
      gap: 10px;
      margin: 16px 0;
    }}
    .stat {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px 14px;
      min-height: 78px;
    }}
    .label {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 8px;
    }}
    .value {{
      display: block;
      font-size: 20px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .chart-shell {{
      position: relative;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }}
    canvas {{
      display: block;
      width: 100%;
      height: min(68vh, 640px);
      min-height: 430px;
    }}
    .tooltip {{
      position: absolute;
      display: none;
      min-width: 150px;
      padding: 8px 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: rgba(255, 255, 255, 0.96);
      box-shadow: 0 8px 22px rgba(23, 32, 42, 0.12);
      pointer-events: none;
      font-size: 12px;
      line-height: 1.5;
    }}
    .notes {{
      margin-top: 14px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.7;
    }}
    .notes p {{ margin: 4px 0; }}
    @media (max-width: 780px) {{
      header {{
        display: block;
      }}
      .stats {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      canvas {{
        min-height: 360px;
      }}
      .value {{
        font-size: 17px;
      }}
    }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>{index_info.name}</h1>
      <p class="subtitle">代码 {index_info.ts_code} · 来源 {source} · {len(points)} 条数据 · {first["trade_date"]} 至 {last["trade_date"]}</p>
    </div>
  </header>

  <section class="stats" aria-label="指数统计">
    <div class="stat"><span class="label">最新收盘</span><span class="value" id="latest-value"></span></div>
    <div class="stat"><span class="label">最高点</span><span class="value" id="max-value"></span></div>
    <div class="stat"><span class="label">最低点</span><span class="value" id="min-value"></span></div>
    <div class="stat"><span class="label">样本跨度</span><span class="value" id="range-value"></span></div>
  </section>

  <section class="chart-shell">
    <canvas id="chart" aria-label="{index_info.name}折线图"></canvas>
    <div class="tooltip" id="tooltip"></div>
  </section>

  <section class="notes" aria-label="数据说明" id="notes"></section>
</main>

<script>
const points = {safe_json(points)};
const meta = {safe_json(meta)};
const canvas = document.getElementById("chart");
const tip = document.getElementById("tooltip");
const ctx = canvas.getContext("2d");
const fmt = new Intl.NumberFormat("zh-CN", {{ maximumFractionDigits: 2 }});

document.getElementById("latest-value").textContent = fmt.format(meta.latest.close);
document.getElementById("max-value").textContent = `${{fmt.format(meta.maxPoint.close)}}`;
document.getElementById("min-value").textContent = `${{fmt.format(meta.minPoint.close)}}`;
document.getElementById("range-value").textContent = `${{meta.rows}} 日`;
document.getElementById("notes").innerHTML = meta.notes.map((note) => `<p>${{escapeHtml(note)}}</p>`).join("");

function escapeHtml(text) {{
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}}

function resizeCanvas() {{
  const ratio = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.floor(rect.width * ratio);
  canvas.height = Math.floor(rect.height * ratio);
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  draw();
}}

function draw(activeIndex = null) {{
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  const margin = {{ left: 64, right: 24, top: 28, bottom: 46 }};
  const plotW = width - margin.left - margin.right;
  const plotH = height - margin.top - margin.bottom;
  const values = points.map((p) => p.close);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const pad = (max - min) * 0.08 || 1;
  const lo = min - pad;
  const hi = max + pad;
  const x = (i) => margin.left + (points.length === 1 ? 0 : (i / (points.length - 1)) * plotW);
  const y = (v) => margin.top + ((hi - v) / (hi - lo)) * plotH;

  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, width, height);

  ctx.strokeStyle = "#d8dee8";
  ctx.lineWidth = 1;
  ctx.fillStyle = "#687383";
  ctx.font = "12px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let i = 0; i <= 5; i++) {{
    const value = lo + ((hi - lo) * i) / 5;
    const yy = y(value);
    ctx.beginPath();
    ctx.moveTo(margin.left, yy);
    ctx.lineTo(width - margin.right, yy);
    ctx.stroke();
    ctx.fillText(fmt.format(value), margin.left - 10, yy);
  }}

  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  const tickCount = Math.min(6, points.length);
  for (let i = 0; i < tickCount; i++) {{
    const idx = Math.round((i * (points.length - 1)) / Math.max(1, tickCount - 1));
    const xx = x(idx);
    ctx.fillText(points[idx].trade_date.slice(0, 7), xx, height - margin.bottom + 18);
  }}

  const gradient = ctx.createLinearGradient(0, margin.top, 0, height - margin.bottom);
  gradient.addColorStop(0, "rgba(31, 122, 104, 0.20)");
  gradient.addColorStop(1, "rgba(31, 122, 104, 0.02)");

  ctx.beginPath();
  points.forEach((point, index) => {{
    const xx = x(index);
    const yy = y(point.close);
    if (index === 0) ctx.moveTo(xx, yy);
    else ctx.lineTo(xx, yy);
  }});
  ctx.lineTo(x(points.length - 1), height - margin.bottom);
  ctx.lineTo(x(0), height - margin.bottom);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();

  ctx.beginPath();
  points.forEach((point, index) => {{
    const xx = x(index);
    const yy = y(point.close);
    if (index === 0) ctx.moveTo(xx, yy);
    else ctx.lineTo(xx, yy);
  }});
  ctx.strokeStyle = "#1f7a68";
  ctx.lineWidth = 2.5;
  ctx.stroke();

  if (activeIndex !== null) {{
    const point = points[activeIndex];
    const xx = x(activeIndex);
    const yy = y(point.close);
    ctx.strokeStyle = "#b14b36";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(xx, margin.top);
    ctx.lineTo(xx, height - margin.bottom);
    ctx.stroke();
    ctx.fillStyle = "#b14b36";
    ctx.beginPath();
    ctx.arc(xx, yy, 4, 0, Math.PI * 2);
    ctx.fill();
  }}

  return {{ margin, plotW, plotH, x, y }};
}}

function nearestIndex(event) {{
  const rect = canvas.getBoundingClientRect();
  const state = draw();
  const mx = event.clientX - rect.left;
  const raw = ((mx - state.margin.left) / Math.max(1, state.plotW)) * (points.length - 1);
  return Math.max(0, Math.min(points.length - 1, Math.round(raw)));
}}

canvas.addEventListener("mousemove", (event) => {{
  const index = nearestIndex(event);
  const state = draw(index);
  const point = points[index];
  const xx = state.x(index);
  const yy = state.y(point.close);
  tip.innerHTML = `<strong>${{point.trade_date}}</strong><br>收盘：${{fmt.format(point.close)}}`;
  tip.style.display = "block";
  const left = Math.min(canvas.clientWidth - 170, Math.max(12, xx + 16));
  const top = Math.min(canvas.clientHeight - 62, Math.max(12, yy - 20));
  tip.style.left = `${{left}}px`;
  tip.style.top = `${{top}}px`;
}});

canvas.addEventListener("mouseleave", () => {{
  tip.style.display = "none";
  draw();
}});

window.addEventListener("resize", resizeCanvas);
resizeCanvas();
</script>
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="显示 A 股自由现金流全收益指数折线图。")
    parser.add_argument("--env-file", default=".env", help="包含 TUSHARE_TOKEN 的环境文件。默认 .env。")
    parser.add_argument("--output-dir", default="output", help="输出目录。默认 output。")
    parser.add_argument("--index-code", default=None, help="手工指定 Tushare 指数代码。默认自动查找。")
    parser.add_argument("--start-date", default="19900101", help="开始日期，格式 YYYYMMDD。默认尽量早。")
    parser.add_argument("--end-date", default=datetime.now().strftime("%Y%m%d"), help="结束日期，格式 YYYYMMDD。")
    parser.add_argument("--tv-symbol", default=DEFAULT_TRADINGVIEW_SYMBOL, help="Tushare 无行情时使用的 TradingView 代码。")
    parser.add_argument("--max-bars", type=int, default=10000, help="TradingView 最多请求多少根日线。")
    parser.add_argument("--no-tradingview", action="store_true", help="只使用 Tushare，不启用 TradingView 兜底。")
    parser.add_argument("--no-open", action="store_true", help="生成后不自动打开 HTML。")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    load_env_file(Path(args.env_file))

    pro = tushare_client()
    index_info, candidates = find_index_info(pro, args.index_code)
    print(f"匹配指数：{index_info.ts_code} {index_info.name}")

    start_date = index_info.base_date if index_info.base_date and index_info.base_date.isdigit() else args.start_date
    start_date = min(start_date, args.start_date)
    result = fetch_tushare_series(pro, index_info, start_date, args.end_date)

    if result.data.empty:
        if args.no_tradingview:
            notes = "\n".join(result.notes)
            raise RuntimeError(f"Tushare 没有返回 {index_info.ts_code} 的行情数据：\n{notes}")
        print("Tushare 没有返回行情，切换到 TradingView 总回报序列。")
        fallback = fetch_tradingview_series(args.tv_symbol, args.max_bars)
        fallback.notes = result.notes + [
            f"已使用 TradingView 兜底代码 {args.tv_symbol}。",
            "TradingView 返回的是该代码当前可取到的最大日线窗口。",
        ] + fallback.notes
        result = fallback

    data = filter_by_date(result.data, args.start_date, args.end_date)
    if data.empty:
        raise RuntimeError("按指定日期过滤后没有可绘制数据。")

    output_dir = Path(args.output_dir)
    csv_path = output_dir / "a_share_free_cash_flow_total_return.csv"
    html_path = output_dir / "a_share_free_cash_flow_total_return.html"
    write_csv(data, csv_path)
    write_html(data, index_info, result.source, result.notes, html_path)

    first = data.iloc[0]
    last = data.iloc[-1]
    print(f"数据来源：{result.source}")
    print(f"数据范围：{first['trade_date']} 至 {last['trade_date']}，共 {len(data)} 条")
    print(f"最新收盘：{last['close']:.2f}")
    print(f"CSV：{csv_path.resolve()}")
    print(f"HTML：{html_path.resolve()}")

    if not args.no_open:
        webbrowser.open(html_path.resolve().as_uri())

    if not candidates.empty:
        candidate_path = output_dir / "free_cash_flow_index_candidates.csv"
        candidates.to_csv(candidate_path, index=False, encoding="utf-8-sig")
        print(f"候选指数清单：{candidate_path.resolve()}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
