from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests
import tushare as ts


TODAY = datetime.now().strftime("%Y%m%d")

DEFAULT_CODES = ("932365.CSI", "980092.CNI")
KNOWN_INDEXES = {
    "932365.CSI": {
        "name": "中证全指自由现金流指数",
        "market": "CSI",
        "base_date": "20131231",
        "publisher": "中证指数有限公司",
        "preferred_source": "tushare",
    },
    "980092.CNI": {
        "name": "国证自由现金流指数",
        "market": "CNI",
        "base_date": "20121231",
        "publisher": "深圳证券信息有限公司",
        "preferred_source": "cnindex",
        "cnindex_code": "980092",
    },
}

COLORS = {
    "932365.CSI": "#1f7a68",
    "980092.CNI": "#b14b36",
}


@dataclass
class IndexSeries:
    code: str
    name: str
    source: str
    data: pd.DataFrame
    base_date: str = ""
    publisher: str = ""
    note: str = ""


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


def tushare_client() -> Any:
    ts.set_token(get_tushare_token())
    return ts.pro_api()


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


def normalize_tushare_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty or "trade_date" not in frame.columns or "close" not in frame.columns:
        return pd.DataFrame()
    data = frame.copy()
    data["trade_date"] = data["trade_date"].map(normalize_trade_date)
    for col in ("open", "high", "low", "close", "pre_close", "change", "pct_chg", "vol", "amount"):
        if col in data.columns:
            data[col] = pd.to_numeric(data[col], errors="coerce")
    data = data.dropna(subset=["trade_date", "close"])
    data = data.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
    return data


def fetch_tushare_daily(pro: Any, code: str, start_date: str, end_date: str) -> pd.DataFrame:
    chunks: list[pd.DataFrame] = []
    for chunk_start, chunk_end in chunk_date_ranges(start_date, end_date):
        frame = pro.index_daily(ts_code=code, start_date=chunk_start, end_date=chunk_end)
        if frame is not None and not frame.empty:
            chunks.append(frame.dropna(axis=1, how="all"))
    if not chunks:
        return pd.DataFrame()
    return normalize_tushare_frame(pd.concat(chunks, ignore_index=True))


def fetch_tushare_metadata(pro: Any, code: str, fallback: dict[str, str]) -> dict[str, str]:
    market = fallback.get("market", "")
    fields = "ts_code,name,market,publisher,category,base_date,list_date"
    if market:
        frame = pro.index_basic(market=market, fields=fields)
        if frame is not None and not frame.empty:
            hit = frame[frame["ts_code"].eq(code)]
            if not hit.empty:
                row = hit.iloc[0]
                return {
                    "name": fallback.get("name") or str(row.get("name", "")),
                    "market": str(row.get("market", "")),
                    "publisher": str(row.get("publisher", "")),
                    "category": str(row.get("category", "")),
                    "base_date": "" if pd.isna(row.get("base_date", "")) else str(row.get("base_date", "")),
                    "list_date": "" if pd.isna(row.get("list_date", "")) else str(row.get("list_date", "")),
                }
    return {
        "name": fallback.get("name", code),
        "market": fallback.get("market", ""),
        "publisher": fallback.get("publisher", ""),
        "category": "",
        "base_date": fallback.get("base_date", ""),
        "list_date": "",
    }


def fetch_cnindex_daily(index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    url = "https://www.cnindex.com.cn/market/market/getIndexDailyDataWithDataFormat"
    response = requests.get(
        url,
        params={
            "indexCode": index_code,
            "startDate": normalize_trade_date(start_date),
            "endDate": normalize_trade_date(end_date),
            "frequency": "day",
        },
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": f"https://www.cnindex.com.cn/module/index-detail.html?act_menu=1&indexCode={index_code}",
        },
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 200:
        raise RuntimeError(f"国证官网接口返回异常：{payload.get('message')}")

    rows = payload.get("data", {}).get("data", [])
    parsed: list[dict[str, Any]] = []
    for row in rows:
        if len(row) < 10:
            continue
        parsed.append(
            {
                "trade_date": normalize_trade_date(row[0]),
                "open": row[3],
                "high": row[2],
                "low": row[4],
                "close": row[5],
                "change": row[6],
                "pct_chg": parse_percent(row[7]),
                "amount": row[8],
                "vol": row[9],
            }
        )
    data = pd.DataFrame(parsed)
    if data.empty:
        return data
    for col in ("open", "high", "low", "close", "change", "pct_chg", "amount", "vol"):
        data[col] = pd.to_numeric(data[col], errors="coerce")
    return data.dropna(subset=["trade_date", "close"]).sort_values("trade_date").drop_duplicates("trade_date")


def parse_percent(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("%"):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return None


def fetch_index_series(pro: Any, code: str, start_date: str, end_date: str) -> IndexSeries:
    fallback = KNOWN_INDEXES.get(code, {"name": code, "market": code.split(".")[-1] if "." in code else ""})
    metadata = fetch_tushare_metadata(pro, code, fallback)
    preferred_source = fallback.get("preferred_source", "tushare")

    if preferred_source == "cnindex":
        cn_code = fallback.get("cnindex_code", code.split(".")[0])
        try:
            data = fetch_cnindex_daily(cn_code, start_date, end_date)
            if not data.empty:
                return IndexSeries(
                    code=code,
                    name=metadata["name"],
                    source="国证官网 getIndexDailyDataWithDataFormat",
                    data=data,
                    base_date=metadata.get("base_date", ""),
                    publisher=metadata.get("publisher", ""),
                    note="国证官网接口返回完整日线，优先于 Tushare 的较短 CNI 序列。",
                )
        except Exception as exc:
            print(f"国证官网 {code} 查询失败，改用 Tushare：{exc}", file=sys.stderr)

    data = fetch_tushare_daily(pro, code, start_date, end_date)
    if data.empty:
        raise RuntimeError(f"{code} 没有可用日线数据。")
    return IndexSeries(
        code=code,
        name=metadata["name"],
        source="Tushare index_daily",
        data=data,
        base_date=metadata.get("base_date", ""),
        publisher=metadata.get("publisher", ""),
        note="Tushare index_daily 返回日线。",
    )


def filter_by_date(data: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    start = normalize_trade_date(start_date)
    end = normalize_trade_date(end_date)
    return data[(data["trade_date"] >= start) & (data["trade_date"] <= end)].copy()


def write_series_csv(series: IndexSeries, output_dir: Path) -> Path:
    path = output_dir / f"{series.code.replace('.', '_')}_daily.csv"
    cols = [col for col in ("trade_date", "open", "high", "low", "close", "pre_close", "change", "pct_chg", "vol", "amount") if col in series.data.columns]
    series.data[cols].to_csv(path, index=False, encoding="utf-8-sig")
    return path


def write_wide_csv(series_list: list[IndexSeries], output_path: Path) -> None:
    merged: pd.DataFrame | None = None
    for series in series_list:
        frame = series.data[["trade_date", "close"]].rename(columns={"close": series.code})
        merged = frame if merged is None else merged.merge(frame, on="trade_date", how="outer")
    assert merged is not None
    merged.sort_values("trade_date").to_csv(output_path, index=False, encoding="utf-8-sig")


def build_chart_payload(series_list: list[IndexSeries]) -> dict[str, Any]:
    payload_series = []
    for index, series in enumerate(series_list):
        points = series.data[["trade_date", "close"]].to_dict(orient="records")
        first = points[0]
        last = points[-1]
        total_return = (last["close"] / first["close"] - 1) * 100
        payload_series.append(
            {
                "code": series.code,
                "name": series.name,
                "source": series.source,
                "baseDate": series.base_date,
                "publisher": series.publisher,
                "note": series.note,
                "color": COLORS.get(series.code, ["#1f7a68", "#b14b36", "#4b5f9f", "#8a6d2f"][index % 4]),
                "first": first,
                "last": last,
                "rows": len(points),
                "totalReturn": total_return,
                "points": points,
            }
        )
    return {"series": payload_series}


def safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def write_html(series_list: list[IndexSeries], output_path: Path) -> None:
    payload = build_chart_payload(series_list)
    all_start = min(item["first"]["trade_date"] for item in payload["series"])
    all_end = max(item["last"]["trade_date"] for item in payload["series"])

    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>自由现金流指数对比</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #17202a;
      --muted: #66717f;
      --line: #d8dee8;
      --panel: #ffffff;
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
      width: min(1200px, calc(100vw - 32px));
      margin: 24px auto;
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
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin: 16px 0;
    }}
    .stat {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px 14px;
      min-height: 112px;
    }}
    .stat-title {{
      display: flex;
      gap: 8px;
      align-items: center;
      font-weight: 700;
      margin-bottom: 10px;
    }}
    .swatch {{
      width: 12px;
      height: 12px;
      border-radius: 50%;
      display: inline-block;
    }}
    .stat-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
      font-size: 13px;
      color: var(--muted);
    }}
    .stat-grid strong {{
      display: block;
      margin-top: 4px;
      color: var(--ink);
      font-size: 16px;
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
      min-width: 230px;
      max-width: 340px;
      padding: 8px 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: rgba(255, 255, 255, 0.96);
      box-shadow: 0 8px 22px rgba(23, 32, 42, 0.12);
      pointer-events: none;
      font-size: 12px;
      line-height: 1.55;
    }}
    .notes {{
      margin-top: 14px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.7;
    }}
    .notes p {{ margin: 4px 0; }}
    @media (max-width: 860px) {{
      .stats {{
        grid-template-columns: 1fr;
      }}
      .stat-grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      canvas {{
        min-height: 360px;
      }}
    }}
  </style>
</head>
<body>
<main>
  <header>
    <h1>自由现金流指数对比</h1>
    <p class="subtitle">中证全指自由现金流指数（932365.CSI）与国证自由现金流指数（980092.CNI） · {all_start} 至 {all_end} · 点位口径，各自基日为 1000</p>
  </header>
  <section class="stats" id="stats" aria-label="指数统计"></section>
  <section class="chart-shell">
    <canvas id="chart" aria-label="自由现金流指数折线图"></canvas>
    <div class="tooltip" id="tooltip"></div>
  </section>
  <section class="notes" id="notes" aria-label="数据说明"></section>
</main>

<script>
const payload = {safe_json(payload)};
const canvas = document.getElementById("chart");
const tip = document.getElementById("tooltip");
const ctx = canvas.getContext("2d");
const fmt = new Intl.NumberFormat("zh-CN", {{ maximumFractionDigits: 2 }});
const pct = new Intl.NumberFormat("zh-CN", {{ maximumFractionDigits: 2, minimumFractionDigits: 2 }});

for (const series of payload.series) {{
  series.points = series.points.map((p) => ({{ ...p, time: Date.parse(p.trade_date + "T00:00:00Z") }}));
}}

document.getElementById("stats").innerHTML = payload.series.map((series) => `
  <article class="stat">
    <div class="stat-title"><span class="swatch" style="background:${{series.color}}"></span>${{series.name}}（${{series.code}}）</div>
    <div class="stat-grid">
      <span>数据起点<strong>${{series.first.trade_date}}</strong></span>
      <span>最新日期<strong>${{series.last.trade_date}}</strong></span>
      <span>最新点位<strong>${{fmt.format(series.last.close)}}</strong></span>
      <span>区间涨幅<strong>${{pct.format(series.totalReturn)}}%</strong></span>
    </div>
  </article>
`).join("");

document.getElementById("notes").innerHTML = payload.series.map((series) => `
  <p>${{series.code}}：${{series.source}}，${{series.rows}} 条；基日 ${{series.baseDate || "未知"}}。${{series.note}}</p>
`).join("");

function extent(values) {{
  return [Math.min(...values), Math.max(...values)];
}}

function resizeCanvas() {{
  const ratio = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.floor(rect.width * ratio);
  canvas.height = Math.floor(rect.height * ratio);
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  draw();
}}

function allPoints() {{
  return payload.series.flatMap((series) => series.points);
}}

function draw(activeTime = null) {{
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  const margin = {{ left: 64, right: 24, top: 28, bottom: 46 }};
  const plotW = width - margin.left - margin.right;
  const plotH = height - margin.top - margin.bottom;
  const points = allPoints();
  const [minTime, maxTime] = extent(points.map((p) => p.time));
  const [minValue, maxValue] = extent(points.map((p) => p.close));
  const pad = (maxValue - minValue) * 0.08 || 1;
  const lo = minValue - pad;
  const hi = maxValue + pad;
  const x = (time) => margin.left + ((time - minTime) / Math.max(1, maxTime - minTime)) * plotW;
  const y = (value) => margin.top + ((hi - value) / (hi - lo)) * plotH;

  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, width, height);

  ctx.strokeStyle = "#d8dee8";
  ctx.lineWidth = 1;
  ctx.fillStyle = "#66717f";
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
  const tickCount = 6;
  for (let i = 0; i < tickCount; i++) {{
    const time = minTime + ((maxTime - minTime) * i) / Math.max(1, tickCount - 1);
    const label = new Date(time).toISOString().slice(0, 7);
    ctx.fillText(label, x(time), height - margin.bottom + 18);
  }}

  for (const series of payload.series) {{
    ctx.beginPath();
    series.points.forEach((point, index) => {{
      const xx = x(point.time);
      const yy = y(point.close);
      if (index === 0) ctx.moveTo(xx, yy);
      else ctx.lineTo(xx, yy);
    }});
    ctx.strokeStyle = series.color;
    ctx.lineWidth = 2.4;
    ctx.stroke();
  }}

  if (activeTime !== null) {{
    ctx.strokeStyle = "#9aa5b2";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x(activeTime), margin.top);
    ctx.lineTo(x(activeTime), height - margin.bottom);
    ctx.stroke();
    for (const series of payload.series) {{
      const nearest = nearestPoint(series.points, activeTime);
      ctx.fillStyle = series.color;
      ctx.beginPath();
      ctx.arc(x(nearest.time), y(nearest.close), 4, 0, Math.PI * 2);
      ctx.fill();
    }}
  }}

  return {{ margin, plotW, minTime, maxTime, x, y }};
}}

function nearestPoint(points, time) {{
  let best = points[0];
  let bestDistance = Math.abs(best.time - time);
  for (const point of points) {{
    const distance = Math.abs(point.time - time);
    if (distance < bestDistance) {{
      best = point;
      bestDistance = distance;
    }}
  }}
  return best;
}}

canvas.addEventListener("mousemove", (event) => {{
  const rect = canvas.getBoundingClientRect();
  const state = draw();
  const mx = event.clientX - rect.left;
  const ratio = Math.max(0, Math.min(1, (mx - state.margin.left) / Math.max(1, state.plotW)));
  const activeTime = state.minTime + ratio * (state.maxTime - state.minTime);
  draw(activeTime);
  const rows = payload.series.map((series) => {{
    const point = nearestPoint(series.points, activeTime);
    return `<div><span style="color:${{series.color}}">●</span> ${{series.code}} ${{point.trade_date}}：${{fmt.format(point.close)}}</div>`;
  }}).join("");
  tip.innerHTML = rows;
  tip.style.display = "block";
  tip.style.left = `${{Math.min(canvas.clientWidth - 360, Math.max(12, mx + 16))}}px`;
  tip.style.top = `${{Math.max(12, event.clientY - rect.top - 20)}}px`;
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
    parser = argparse.ArgumentParser(description="显示自由现金流指数折线图。")
    parser.add_argument("--env-file", default=".env", help="包含 TUSHARE_TOKEN 的环境文件。默认 .env。")
    parser.add_argument("--output-dir", default="output", help="输出目录。默认 output。")
    parser.add_argument("--codes", default=",".join(DEFAULT_CODES), help="逗号分隔的指数代码。默认 932365.CSI,980092.CNI。")
    parser.add_argument("--start-date", default="20121231", help="开始日期，格式 YYYYMMDD。默认 20121231。")
    parser.add_argument("--end-date", default=TODAY, help="结束日期，格式 YYYYMMDD。")
    parser.add_argument("--no-open", action="store_true", help="生成后不自动打开 HTML。")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    load_env_file(Path(args.env_file))

    pro = tushare_client()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    codes = [code.strip() for code in args.codes.split(",") if code.strip()]
    series_list: list[IndexSeries] = []
    for code in codes:
        series = fetch_index_series(pro, code, args.start_date, args.end_date)
        series.data = filter_by_date(series.data, args.start_date, args.end_date)
        if series.data.empty:
            raise RuntimeError(f"{code} 按日期过滤后没有数据。")
        series_list.append(series)

    csv_paths = [write_series_csv(series, output_dir) for series in series_list]
    wide_csv_path = output_dir / "free_cash_flow_indices.csv"
    write_wide_csv(series_list, wide_csv_path)

    html_path = output_dir / "free_cash_flow_indices.html"
    legacy_html_path = output_dir / "a_share_free_cash_flow_total_return.html"
    legacy_csv_path = output_dir / "a_share_free_cash_flow_total_return.csv"
    write_html(series_list, html_path)
    write_html(series_list, legacy_html_path)
    write_wide_csv(series_list, legacy_csv_path)

    print("已生成自由现金流指数对比图：")
    for series in series_list:
        first = series.data.iloc[0]
        last = series.data.iloc[-1]
        print(
            f"- {series.code} {series.name}：{series.source}，"
            f"{first['trade_date']} 至 {last['trade_date']}，{len(series.data)} 条，最新 {last['close']:.2f}"
        )
    print(f"HTML：{html_path.resolve()}")
    print(f"当前浏览器旧路径也已覆盖：{legacy_html_path.resolve()}")
    print(f"合并 CSV：{wide_csv_path.resolve()}")
    for path in csv_paths:
        print(f"单指数 CSV：{path.resolve()}")

    if not args.no_open:
        webbrowser.open(html_path.resolve().as_uri())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
