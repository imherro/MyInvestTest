from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests


TODAY = datetime.now().strftime("%Y%m%d")
DEFAULT_CODE = "480092.CNI"
DEFAULT_CNINDEX_CODE = "480092"
DEFAULT_NAME = "国证自由现金流全收益指数"


@dataclass
class IndexSeries:
    code: str
    name: str
    source: str
    data: pd.DataFrame
    base_date: str = "20121231"
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


def normalize_trade_date(value: Any) -> str:
    text = str(value)
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return pd.to_datetime(value).strftime("%Y-%m-%d")


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
    data = data.dropna(subset=["trade_date", "close"])
    data = data.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
    return data.reset_index(drop=True)


def filter_by_date(data: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    start = normalize_trade_date(start_date)
    end = normalize_trade_date(end_date)
    return data[(data["trade_date"] >= start) & (data["trade_date"] <= end)].copy().reset_index(drop=True)


def add_record_flags(data: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    data = data.copy()
    high_records: list[dict[str, Any]] = []
    low_records: list[dict[str, Any]] = []
    record_high = float("-inf")
    record_low = float("inf")
    high_seq = 0
    low_seq = 0

    data["record_high"] = False
    data["record_low"] = False

    for idx, row in data.iterrows():
        close = float(row["close"])
        is_first = idx == 0
        if is_first or close > record_high:
            high_seq += 1
            record_high = close
            data.at[idx, "record_high"] = True
            high_records.append(
                {
                    "record_type": "新高" if not is_first else "起点/新高基准",
                    "sequence": high_seq,
                    "trade_date": row["trade_date"],
                    "close": close,
                }
            )
        if is_first or close < record_low:
            low_seq += 1
            record_low = close
            data.at[idx, "record_low"] = True
            low_records.append(
                {
                    "record_type": "新低" if not is_first else "起点/新低基准",
                    "sequence": low_seq,
                    "trade_date": row["trade_date"],
                    "close": close,
                }
            )

    records = pd.DataFrame(high_records + low_records)
    records = records.sort_values(["trade_date", "record_type", "sequence"]).reset_index(drop=True)
    return data, records


def fetch_index_series(code: str, start_date: str, end_date: str) -> IndexSeries:
    cnindex_code = code.split(".")[0] if code.endswith(".CNI") else DEFAULT_CNINDEX_CODE
    data = fetch_cnindex_daily(cnindex_code, start_date, end_date)
    data = filter_by_date(data, start_date, end_date)
    if data.empty:
        raise RuntimeError(f"{code} 按日期过滤后没有数据。")
    data, _ = add_record_flags(data)
    return IndexSeries(
        code=code,
        name=DEFAULT_NAME if code == DEFAULT_CODE else code,
        source="国证官网 getIndexDailyDataWithDataFormat",
        data=data,
        note="历史新高/新低按收盘点位逐日严格刷新计算，首日作为新高和新低的共同基准点。",
    )


def write_daily_csv(series: IndexSeries, output_path: Path) -> None:
    cols = [
        col
        for col in (
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "change",
            "pct_chg",
            "vol",
            "amount",
            "record_high",
            "record_low",
        )
        if col in series.data.columns
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    series.data[cols].to_csv(output_path, index=False, encoding="utf-8-sig")


def write_record_csv(records: pd.DataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    records.to_csv(output_path, index=False, encoding="utf-8-sig")


def safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def chart_payload(series: IndexSeries, records: pd.DataFrame) -> dict[str, Any]:
    data = series.data
    points = data[["trade_date", "close"]].to_dict(orient="records")
    record_highs = data[data["record_high"]][["trade_date", "close"]].to_dict(orient="records")
    record_lows = data[data["record_low"]][["trade_date", "close"]].to_dict(orient="records")
    first = points[0]
    last = points[-1]
    all_time_high = max(points, key=lambda item: item["close"])
    all_time_low = min(points, key=lambda item: item["close"])
    total_return = (last["close"] / first["close"] - 1) * 100
    return {
        "code": series.code,
        "name": series.name,
        "source": series.source,
        "baseDate": series.base_date,
        "note": series.note,
        "first": first,
        "last": last,
        "rows": len(points),
        "totalReturn": total_return,
        "allTimeHigh": all_time_high,
        "allTimeLow": all_time_low,
        "recordHighCount": len(record_highs),
        "recordLowCount": len(record_lows),
        "recordRows": records.to_dict(orient="records"),
        "points": points,
        "recordHighs": record_highs,
        "recordLows": record_lows,
    }


def write_html(series: IndexSeries, records: pd.DataFrame, output_path: Path) -> None:
    payload = chart_payload(series, records)
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{series.name}新高新低标记</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #17202a;
      --muted: #66717f;
      --line: #d8dee8;
      --panel: #ffffff;
      --bg: #f5f7f9;
      --main: #1f7a68;
      --record-high: #b45f06;
      --record-low: #275f9f;
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
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 10px;
      margin: 16px 0;
    }}
    .stat {{
      min-height: 86px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px 14px;
    }}
    .label {{
      display: block;
      margin-bottom: 8px;
      color: var(--muted);
      font-size: 12px;
    }}
    .value {{
      display: block;
      font-size: 18px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .legend {{
      display: flex;
      flex-wrap: wrap;
      gap: 16px;
      margin: 0 0 12px;
      color: var(--muted);
      font-size: 13px;
    }}
    .legend span {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }}
    .swatch {{
      width: 22px;
      height: 3px;
      border-radius: 999px;
      display: inline-block;
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
      max-width: 360px;
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
    @media (max-width: 980px) {{
      .stats {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      canvas {{
        min-height: 360px;
      }}
      .value {{
        font-size: 16px;
      }}
    }}
  </style>
</head>
<body>
<main>
  <header>
    <h1>{series.name}（{series.code}）</h1>
    <p class="subtitle">收盘价曲线 + 历史新高折线 + 历史新低折线 · {payload["first"]["trade_date"]} 至 {payload["last"]["trade_date"]}</p>
  </header>
  <section class="stats" id="stats" aria-label="指数统计"></section>
  <div class="legend">
    <span><i class="swatch" style="background: var(--main)"></i>收盘价</span>
    <span><i class="swatch" style="background: var(--record-high)"></i>历史新高记录点连线</span>
    <span><i class="swatch" style="background: var(--record-low)"></i>历史新低记录点连线</span>
  </div>
  <section class="chart-shell">
    <canvas id="chart" aria-label="{series.name}新高新低折线图"></canvas>
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

payload.points = payload.points.map((p) => ({{ ...p, time: Date.parse(p.trade_date + "T00:00:00Z") }}));
payload.recordHighs = payload.recordHighs.map((p) => ({{ ...p, time: Date.parse(p.trade_date + "T00:00:00Z") }}));
payload.recordLows = payload.recordLows.map((p) => ({{ ...p, time: Date.parse(p.trade_date + "T00:00:00Z") }}));

document.getElementById("stats").innerHTML = `
  <div class="stat"><span class="label">最新点位</span><span class="value">${{fmt.format(payload.last.close)}}</span></div>
  <div class="stat"><span class="label">区间涨幅</span><span class="value">${{pct.format(payload.totalReturn)}}%</span></div>
  <div class="stat"><span class="label">新高记录点</span><span class="value">${{payload.recordHighCount}}</span></div>
  <div class="stat"><span class="label">新低记录点</span><span class="value">${{payload.recordLowCount}}</span></div>
  <div class="stat"><span class="label">最高收盘</span><span class="value">${{fmt.format(payload.allTimeHigh.close)}}</span></div>
  <div class="stat"><span class="label">最低收盘</span><span class="value">${{fmt.format(payload.allTimeLow.close)}}</span></div>
`;

document.getElementById("notes").innerHTML = `
  <p>${{payload.source}}，共 ${{payload.rows}} 条日线；基日 ${{payload.baseDate}}。</p>
  <p>${{payload.note}}</p>
`;

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

function drawLine(points, x, y, color, width, markerKind = null) {{
  if (!points.length) return;
  ctx.beginPath();
  points.forEach((point, index) => {{
    const xx = x(point.time);
    const yy = y(point.close);
    if (index === 0) ctx.moveTo(xx, yy);
    else ctx.lineTo(xx, yy);
  }});
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.stroke();
  if (!markerKind) return;
  ctx.fillStyle = color;
  ctx.strokeStyle = "#ffffff";
  ctx.lineWidth = 1.2;
  for (const point of points) {{
    const xx = x(point.time);
    const yy = y(point.close);
    ctx.beginPath();
    if (markerKind === "diamond") {{
      ctx.moveTo(xx, yy - 4);
      ctx.lineTo(xx + 4, yy);
      ctx.lineTo(xx, yy + 4);
      ctx.lineTo(xx - 4, yy);
      ctx.closePath();
    }} else {{
      ctx.arc(xx, yy, 3.4, 0, Math.PI * 2);
    }}
    ctx.fill();
    ctx.stroke();
  }}
}}

function draw(activeTime = null) {{
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  const margin = {{ left: 64, right: 24, top: 28, bottom: 46 }};
  const plotW = width - margin.left - margin.right;
  const plotH = height - margin.top - margin.bottom;
  const [minTime, maxTime] = extent(payload.points.map((p) => p.time));
  const [minValue, maxValue] = extent(payload.points.map((p) => p.close));
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

  drawLine(payload.points, x, y, "#1f7a68", 2.2);
  drawLine(payload.recordHighs, x, y, "#b45f06", 1.8, "circle");
  drawLine(payload.recordLows, x, y, "#275f9f", 1.8, "diamond");

  if (activeTime !== null) {{
    const nearest = nearestPoint(payload.points, activeTime);
    ctx.strokeStyle = "#9aa5b2";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x(nearest.time), margin.top);
    ctx.lineTo(x(nearest.time), height - margin.bottom);
    ctx.stroke();
    ctx.fillStyle = "#17202a";
    ctx.beginPath();
    ctx.arc(x(nearest.time), y(nearest.close), 4, 0, Math.PI * 2);
    ctx.fill();
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

function lastRecordAtOrBefore(points, time) {{
  let result = points[0];
  for (const point of points) {{
    if (point.time <= time) result = point;
    else break;
  }}
  return result;
}}

canvas.addEventListener("mousemove", (event) => {{
  const rect = canvas.getBoundingClientRect();
  const state = draw();
  const mx = event.clientX - rect.left;
  const ratio = Math.max(0, Math.min(1, (mx - state.margin.left) / Math.max(1, state.plotW)));
  const activeTime = state.minTime + ratio * (state.maxTime - state.minTime);
  const point = nearestPoint(payload.points, activeTime);
  draw(point.time);
  const highRecord = lastRecordAtOrBefore(payload.recordHighs, point.time);
  const lowRecord = lastRecordAtOrBefore(payload.recordLows, point.time);
  tip.innerHTML = `
    <strong>${{point.trade_date}}</strong><br>
    收盘：${{fmt.format(point.close)}}<br>
    <span style="color:#b45f06">截至当日新高线：</span>${{highRecord.trade_date}} / ${{fmt.format(highRecord.close)}}<br>
    <span style="color:#275f9f">截至当日新低线：</span>${{lowRecord.trade_date}} / ${{fmt.format(lowRecord.close)}}
  `;
  tip.style.display = "block";
  tip.style.left = `${{Math.min(canvas.clientWidth - 380, Math.max(12, mx + 16))}}px`;
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
    parser = argparse.ArgumentParser(description="显示 480092.CNI 新高新低标记折线图。")
    parser.add_argument("--env-file", default=".env", help="可选环境文件。默认 .env。")
    parser.add_argument("--output-dir", default="output", help="输出目录。默认 output。")
    parser.add_argument("--code", default=DEFAULT_CODE, help="指数代码。默认 480092.CNI。")
    parser.add_argument("--start-date", default="20121231", help="开始日期，格式 YYYYMMDD。默认 20121231。")
    parser.add_argument("--end-date", default=TODAY, help="结束日期，格式 YYYYMMDD。")
    parser.add_argument("--no-open", action="store_true", help="生成后不自动打开 HTML。")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    load_env_file(Path(args.env_file))

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    series = fetch_index_series(args.code, args.start_date, args.end_date)
    series.data, records = add_record_flags(series.data)

    daily_csv_path = output_dir / f"{series.code.replace('.', '_')}_daily.csv"
    records_csv_path = output_dir / f"{series.code.replace('.', '_')}_record_points.csv"
    html_path = output_dir / f"{series.code.replace('.', '_')}_new_high_low.html"
    legacy_html_path = output_dir / "a_share_free_cash_flow_total_return.html"
    legacy_csv_path = output_dir / "a_share_free_cash_flow_total_return.csv"

    write_daily_csv(series, daily_csv_path)
    write_daily_csv(series, legacy_csv_path)
    write_record_csv(records, records_csv_path)
    write_html(series, records, html_path)
    write_html(series, records, legacy_html_path)

    first = series.data.iloc[0]
    last = series.data.iloc[-1]
    high_count = int(series.data["record_high"].sum())
    low_count = int(series.data["record_low"].sum())
    print(f"已生成 {series.code} 新高新低折线图：")
    print(f"- 数据范围：{first['trade_date']} 至 {last['trade_date']}，共 {len(series.data)} 条")
    print(f"- 最新收盘：{last['close']:.4f}")
    print(f"- 新高记录点：{high_count} 个；新低记录点：{low_count} 个")
    print(f"HTML：{html_path.resolve()}")
    print(f"当前浏览器旧路径也已覆盖：{legacy_html_path.resolve()}")
    print(f"日线 CSV：{daily_csv_path.resolve()}")
    print(f"记录点 CSV：{records_csv_path.resolve()}")

    if not args.no_open:
        webbrowser.open(html_path.resolve().as_uri())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
