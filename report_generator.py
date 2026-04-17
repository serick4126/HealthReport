"""レポート生成モジュール — グラフ・HTML・Claude補足コメント（ブラウザ印刷方式）"""

import io
import base64
import json
import logging
import os
import re
from datetime import date as _date

logger = logging.getLogger(__name__)

import anthropic
import database
from database import SKIP_MEAL_TYPES as SKIP_LABEL_TYPES

WEEKDAYS_JA = ["月", "火", "水", "木", "金", "土", "日"]


def _date_label(iso: str) -> str:
    d = _date.fromisoformat(iso)
    return f"{d.month}/{d.day}({WEEKDAYS_JA[d.weekday()]})"


def _fig_to_b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


def _setup_matplotlib_font():
    import matplotlib.font_manager as fm
    import matplotlib.pyplot as plt
    for font in ["Yu Gothic", "Noto Sans CJK JP", "Hiragino Sans", "MS Gothic"]:
        try:
            fm.findfont(fm.FontProperties(family=font), fallback_to_default=False)
            plt.rcParams["font.family"] = font
            break
        except Exception:
            pass
    plt.rcParams["axes.unicode_minus"] = False


def generate_charts_base64(data: dict) -> dict:
    """3グラフ（体重・カロリー・歩数）を生成して base64 PNG を返す"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    _setup_matplotlib_font()
    days = data["days"]

    # ── 体重推移（朝→夜時系列1本線）──────────────────────
    w_labels, w_data, w_colors = [], [], []
    for d in days:
        lbl = _date_label(d["date"])
        w_labels.append(lbl + "朝"); w_data.append(d["weight_morning"]); w_colors.append("#34c759")
        w_labels.append(lbl + "夜"); w_data.append(d["weight_evening"]); w_colors.append("#ff9500")

    valid_idx = [i for i, v in enumerate(w_data) if v is not None]
    valid_y   = [w_data[i]   for i in valid_idx]
    valid_col = [w_colors[i] for i in valid_idx]

    trend = calculate_trend(list(range(len(w_data))), w_data)

    fig_w, ax_w = plt.subplots(figsize=(10, 2.4))
    if valid_idx:
        ax_w.plot(valid_idx, valid_y, color="#8e8e93", linewidth=1.5, zorder=1)
        for xi, yi, ci in zip(valid_idx, valid_y, valid_col):
            ax_w.scatter([xi], [yi], color=ci, s=30, zorder=2)
        if trend is not None:
            s, ic = trend
            tx = [valid_idx[0], valid_idx[-1]]
            ty = [s * x + ic for x in tx]
            ax_w.plot(tx, ty, color="#ff3b30", linewidth=1, linestyle="--", zorder=0)
        margin = max(0.5, (max(valid_y) - min(valid_y)) * 0.15) if len(valid_y) > 1 else 0.5
        ax_w.set_ylim(min(valid_y) - margin, max(valid_y) + margin)
    ax_w.set_xticks(range(len(w_labels)))
    ax_w.set_xticklabels(w_labels, fontsize=7, rotation=45, ha="right")
    ax_w.set_ylabel("kg", fontsize=8)
    ax_w.tick_params(axis="y", labelsize=8)
    ax_w.set_title("体重推移", fontsize=10)
    legend_handles = [
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#34c759", markersize=6, label="朝"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#ff9500", markersize=6, label="夜"),
    ]
    if trend is not None:
        legend_handles.append(
            Line2D([0], [0], color="#ff3b30", linewidth=1, linestyle="--", label="トレンド")
        )
    ax_w.legend(handles=legend_handles, fontsize=8)
    fig_w.tight_layout(pad=0.4)
    weight_b64 = _fig_to_b64(fig_w)
    plt.close(fig_w)

    # ── PFC積み上げ棒グラフ＋目標ライン ──────────────────
    goal = data.get("calorie_goal", 1500)
    c_labels = [_date_label(d["date"]) for d in days]
    pfc_data = build_pfc_chart_data(days, skip_dates=set())
    p_vals  = [v or 0 for v in pfc_data["protein_kcal"]]
    f_vals  = [v or 0 for v in pfc_data["fat_kcal"]]
    c_vals2 = [v or 0 for v in pfc_data["carb_kcal"]]
    pf_bottom = [p + f for p, f in zip(p_vals, f_vals)]

    fig_c, ax_c = plt.subplots(figsize=(4.8, 2.2))
    ax_c.bar(c_labels, p_vals, label="P", color="#60a5fa", alpha=0.85)
    ax_c.bar(c_labels, f_vals, bottom=p_vals, label="F", color="#fbbf24", alpha=0.85)
    ax_c.bar(c_labels, c_vals2, bottom=pf_bottom, label="C", color="#34d399", alpha=0.85)
    ax_c.axhline(goal, color="#ff3b30", linestyle="--", linewidth=1, label=f"目標 {goal}kcal")
    ax_c.set_title("PFC推移", fontsize=9)
    ax_c.set_ylabel("kcal", fontsize=7)
    ax_c.tick_params(axis="x", labelsize=6.5, rotation=30)
    ax_c.tick_params(axis="y", labelsize=7)
    ax_c.legend(fontsize=7)
    fig_c.tight_layout(pad=0.4)
    cal_b64 = _fig_to_b64(fig_c)
    plt.close(fig_c)

    # ── 歩数推移（棒グラフ）──────────────────────────────
    s_labels = c_labels
    s_vals   = [d["steps"] if d["steps"] is not None else 0 for d in days]

    steps_goal = data.get("steps_goal", 8000)

    fig_s, ax_s = plt.subplots(figsize=(4.8, 2.2))
    ax_s.bar(s_labels, s_vals, color="#34c759", alpha=0.8)
    if steps_goal and steps_goal > 0:
        ax_s.axhline(steps_goal, color="#ff3b30", linestyle="--", linewidth=1,
                     label=f"目標 {steps_goal:,}歩")
        ax_s.legend(fontsize=7)
    ax_s.set_title("歩数推移", fontsize=9)
    ax_s.set_ylabel("歩", fontsize=7)
    ax_s.tick_params(axis="x", labelsize=6.5, rotation=30)
    ax_s.tick_params(axis="y", labelsize=7)
    ax_s.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda x, _: f"{int(x):,}")
    )
    fig_s.tight_layout(pad=0.4)
    steps_b64 = _fig_to_b64(fig_s)
    plt.close(fig_s)

    return {"weight": weight_b64, "calories": cal_b64, "steps": steps_b64}


def build_pfc_chart_data(days: list, skip_dates: set) -> dict:
    """PFC積み上げグラフ用日別kcalデータを返す（スキップ日・データなし日はNone）"""
    protein_kcal, fat_kcal, carb_kcal = [], [], []
    for d in days:
        if d["date"] in skip_dates or d.get("protein") is None:
            protein_kcal.append(None)
            fat_kcal.append(None)
            carb_kcal.append(None)
        else:
            protein_kcal.append((d["protein"] or 0) * 4)
            fat_kcal.append((d["fat"] or 0) * 9)
            carb_kcal.append((d["carbs"] or 0) * 4)
    return {"protein_kcal": protein_kcal, "fat_kcal": fat_kcal, "carb_kcal": carb_kcal}


def calculate_trend(dates: list, values: list) -> tuple[float, float] | None:
    """最小二乗法で傾き(slope)と切片(intercept)を返す。
    Noneは除外して計算。有効データが2点未満の場合はNoneを返す。
    戻り値: (slope, intercept) or None
    """
    valid = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(valid) < 2:
        return None
    xs = [p[0] for p in valid]
    ys = [p[1] for p in valid]
    n = len(xs)
    sum_x = sum(xs)
    sum_y = sum(ys)
    sum_xy = sum(x * y for x, y in zip(xs, ys))
    sum_xx = sum(x * x for x in xs)
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return None
    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n
    return slope, intercept


def build_achievement_summary(days: list, goal_kcal: int, skip_dates: set) -> dict:
    """週間達成率サマリーを返す。
    戻り値: total_days, achieved_days, achievement_rate, avg_diff_kcal, weight_diff
    """
    total_days = len(days)
    achieved_days = 0
    diffs = []

    morning_weights = [d.get("weight_morning") for d in days if d.get("weight_morning") is not None]
    weight_diff = None
    if len(morning_weights) >= 2:
        weight_diff = round(morning_weights[-1] - morning_weights[0], 1)

    for d in days:
        if d["date"] in skip_dates:
            continue
        cal = d.get("calories")
        if cal is None:
            continue
        diffs.append(cal - goal_kcal)
        if cal <= goal_kcal:
            achieved_days += 1

    achievement_rate = round(achieved_days / total_days * 100) if total_days > 0 else 0
    avg_diff_kcal = round(sum(diffs) / len(diffs)) if diffs else 0

    return {
        "total_days": total_days,
        "achieved_days": achieved_days,
        "achievement_rate": achievement_rate,
        "avg_diff_kcal": avg_diff_kcal,
        "weight_diff": weight_diff,
    }


def _build_achievement_html(summary: dict) -> str:
    """達成率サマリーHTMLを生成"""
    rate = summary["achievement_rate"]
    total = summary["total_days"]
    achieved = summary["achieved_days"]
    diff = summary["avg_diff_kcal"]
    wdiff = summary["weight_diff"]

    diff_str = f"+{diff} kcal" if diff > 0 else f"{diff} kcal"
    wdiff_str = f"{wdiff:+.1f} kg" if wdiff is not None else "&#8212;"
    rate_color = "#34d399" if rate >= 70 else "#f87171"

    return (
        f'<div class="achievement-summary">'
        f'<span class="ach-label">&#128202; 週間サマリー</span>'
        f'<span>{total}日中<strong>{achieved}日</strong>目標達成'
        f'（<span id="achievement-rate" style="color:{rate_color}">{rate}%</span>）</span>'
        f'<span>平均差分: {diff_str}</span>'
        f'<span>体重変化（週間）: {wdiff_str}</span>'
        f'</div>'
    )


def _diff_cell(calories, goal_kcal: int) -> str:
    """カロリー差分セルHTMLを返す"""
    if calories is None:
        return '<td style="color:#8e8e93">&#8212;</td>'
    diff = calories - goal_kcal
    color = "#34d399" if diff <= 0 else "#f87171"
    sign = "+" if diff > 0 else ""
    return f'<td style="color:{color}">{sign}{diff:,}</td>'


def generate_report_html(data: dict, charts: dict, comment: str) -> str:
    days  = data["days"]
    start = _date.fromisoformat(data["start"])
    end   = _date.fromisoformat(data["end"])

    def fmt_header(iso: str) -> str:
        d = _date.fromisoformat(iso)
        return f"{d.month}/{d.day}<br/>({WEEKDAYS_JA[d.weekday()]})"

    def meals_cell(meal_list: list, is_skipped: bool = False, css_class: str = "meal-cell") -> str:
        if meal_list:
            inner = "<br/>".join(m["description"] for m in meal_list)
            return f'<div class="{css_class}">{inner}</div>'
        if is_skipped:
            return f'<div class="{css_class}"><span class="skip-cell">食べなかった</span></div>'
        return ""

    def dash(v, fmt: str = "{}") -> str:
        return "&#8212;" if v is None else fmt.format(v)

    MAIN_MEAL_TYPES = ["breakfast", "lunch", "dinner"]
    MAIN_MEAL_LABELS = {"breakfast": "朝食", "lunch": "昼食", "dinner": "夕食"}

    date_headers = "".join(f"<th>{fmt_header(d['date'])}</th>" for d in days)

    meal_rows = ""
    for mt in MAIN_MEAL_TYPES:
        cells = ""
        for d in days:
            is_skipped = (
                mt in SKIP_LABEL_TYPES
                and d.get("skipped", {}).get(mt, False)
                and not d["meals"][mt]
            )
            cells += f"<td>{meals_cell(d['meals'][mt], is_skipped)}</td>"
        meal_rows += f"<tr><th>{MAIN_MEAL_LABELS[mt]}</th>{cells}</tr>\n"

    # 間食/夜食統合行
    snack_cells = ""
    for d in days:
        combined = d["meals"].get("snack", []) + d["meals"].get("late_night", [])
        both_skipped = (
            d.get("skipped", {}).get("snack", False)
            and d.get("skipped", {}).get("late_night", False)
            and not combined
        )
        snack_cells += f"<td>{meals_cell(combined, both_skipped, css_class='meal-cell-sub')}</td>"
    meal_rows += f'<tr><th>間食<br/>夜食</th>{snack_cells}</tr>\n'

    def pfc(d: dict) -> str:
        p = d["protein"]
        f_ = d["fat"]
        c = d["carbs"]
        if p is None and f_ is None and c is None:
            return "&#8212;"
        p_val = p or 0
        f_val = f_ or 0
        c_val = c or 0
        p_kcal = p_val * 4
        f_kcal = f_val * 9
        c_kcal = c_val * 4
        total_kcal = p_kcal + f_kcal + c_kcal
        if total_kcal > 0:
            p_pct = round(p_kcal / total_kcal * 100)
            f_pct = round(f_kcal / total_kcal * 100)
            c_pct = round(c_kcal / total_kcal * 100)
            return (f'<span style="color:#60a5fa">P{p_pct}%</span> {dash(p)}g<br/>'
                    f'<span style="color:#fbbf24">F{f_pct}%</span> {dash(f_)}g<br/>'
                    f'<span style="color:#34d399">C{c_pct}%</span> {dash(c)}g')
        return f"P{dash(p)}g<br/>F{dash(f_)}g<br/>C{dash(c)}g"

    goal_kcal = data.get("calorie_goal", 1500)
    skip_dates = {d["date"] for d in days if d.get("skipped_types")}
    achievement_html = _build_achievement_html(
        build_achievement_summary(days, goal_kcal, skip_dates=skip_dates)
    )

    # R-2: 食事時刻行
    def _meal_time_cell(d: dict) -> str:
        time_labels = {"breakfast": "朝", "lunch": "昼", "dinner": "夕"}
        parts = []
        for mt, label in time_labels.items():
            meals_for_type = d["meals"].get(mt, [])
            times = set()
            for m in meals_for_type:
                mt_val = m.get("meal_time")
                if mt_val:
                    times.add(mt_val[:2])
            if times:
                parts.append(f"{label}{sorted(times)[0]}")
        return "/".join(parts) if parts else "&#8212;"

    # R-4: 日別BMI行
    height_m = float(data.get("height_cm", 160)) / 100.0

    def _bmi_cell(d: dict) -> str:
        wm = d.get("weight_morning")
        we = d.get("weight_evening")
        if wm is not None and we is not None:
            avg_w = (wm + we) / 2
        elif wm is not None:
            avg_w = wm
        elif we is not None:
            avg_w = we
        else:
            return "&#8212;"
        if height_m <= 0:
            return "&#8212;"
        bmi = avg_w / (height_m ** 2)
        color = "#34d399" if 18.5 <= bmi < 25 else "#f87171"
        return f'<span style="color:{color}">{bmi:.1f}</span>'

    meal_time_cells = "".join(f"<td class='pfc'>{_meal_time_cell(d)}</td>" for d in days)
    cal_cells   = "".join(f"<td>{dash(d['calories'])}</td>"         for d in days)
    diff_cells  = "".join(_diff_cell(d.get("calories"), goal_kcal)  for d in days)
    pfc_cells   = "".join(f"<td class='pfc'>{pfc(d)}</td>"          for d in days)
    sod_cells   = "".join(f"<td>{dash(d['sodium'])}</td>"           for d in days)
    wm_cells    = "".join(f"<td>{dash(d['weight_morning'])}</td>"   for d in days)
    we_cells    = "".join(f"<td>{dash(d['weight_evening'])}</td>"   for d in days)
    bmi_cells   = "".join(f"<td>{_bmi_cell(d)}</td>"                for d in days)
    steps_cells = "".join(f'<td>{dash(d["steps"], "{:,}")}</td>'   for d in days)

    if comment:
        comment_html = _format_structured_comment(comment)
    else:
        weekly_cals = [d["calories"] for d in days if d["calories"] is not None]
        weekly_weights = [d["weight_morning"] for d in days if d["weight_morning"] is not None]
        weekly_steps = [d["steps"] for d in days if d["steps"] is not None]
        if not weekly_cals and not weekly_weights and not weekly_steps:
            comment_html = "<p>該当週の記録がありません</p>"
        else:
            comment_html = "<p>AI注釈の生成に失敗しました（データ不足または APIエラー）</p>"

    period_str = (
        f"{start.year}年{start.month}月{start.day}日"
        f"（{WEEKDAYS_JA[start.weekday()]}）〜"
        f"{end.month}月{end.day}日（{WEEKDAYS_JA[end.weekday()]}）"
    )
    period_short = (
        f"{start.month}/{start.day}（{WEEKDAYS_JA[start.weekday()]}）"
        f"〜{end.month}/{end.day}（{WEEKDAYS_JA[end.weekday()]}）"
    )

    # colgroup（ラベル列6% ＋ 7日列 各13.4%）
    colgroup = (
        '<col style="width:6%"/>'
        + '<col style="width:13.4%"/>' * 7
    )

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8"/>
<title>食事記録レポート {period_str}</title>
<style>
  /* ===== 画面表示 ===== */
  body {{
    font-family: -apple-system, "Yu Gothic UI", "Hiragino Sans", "Noto Sans CJK JP", sans-serif;
    font-size: 9pt;
    margin: 0;
    background: #f0f0f5;
    color: #1c1c1e;
  }}
  .print-toolbar {{
    position: fixed; top: 0; left: 0; right: 0; z-index: 100;
    background: rgba(255,255,255,0.92); backdrop-filter: blur(8px);
    border-bottom: 1px solid #e5e5ea;
    padding: 8px 16px; display: flex; align-items: center; gap: 12px;
  }}
  .print-toolbar h2 {{ font-size: 13px; font-weight: 700; flex: 1; margin: 0; }}
  .print-btn {{
    padding: 7px 18px; background: #007aff; color: #fff;
    border: none; border-radius: 8px; font-size: 13px;
    font-weight: 600; cursor: pointer; white-space: nowrap;
  }}
  .print-btn:active {{ opacity: 0.8; }}

  /* ページブロック（画面では2枚のカード） */
  .page {{
    max-width: 277mm;
    margin: 52px auto 12px;
    background: #fff;
    padding: 10mm 12mm;
    box-shadow: 0 2px 12px rgba(0,0,0,0.12);
  }}
  .page + .page {{
    margin-top: 8px;
    border-top: none;
  }}
  .page-label {{
    font-size: 9px; color: #aaa; text-align: right;
    margin-bottom: 2mm; letter-spacing: 0.5px;
  }}

  /* ===== レポート本体 ===== */
  h1 {{ font-size: 13pt; margin: 0 0 1.5mm; }}
  .sub {{ font-size: 8.5pt; color: #555; margin-bottom: 3mm; }}

  .chart-wrap {{ margin-bottom: 3mm; }}
  .chart-wrap img {{ width: 100%; display: block; }}

  .chart-row {{
    display: flex; gap: 3mm; margin-bottom: 3mm;
  }}
  .chart-row img {{ flex: 1; width: 0; min-width: 0; }}

  table {{
    width: 100%;
    border-collapse: collapse;
    table-layout: fixed;
    margin-bottom: 3mm;
    font-size: 7.5pt;
  }}
  th, td {{
    border: 0.5pt solid #aaa;
    padding: 1.2mm 1.5mm;
    text-align: center;
    vertical-align: top;
    word-break: break-all;
    overflow-wrap: anywhere;
  }}
  th {{
    background: #f0f0f0; font-weight: 700;
    font-size: 8pt; white-space: nowrap;
  }}
  td {{ line-height: 1.4; }}
  tr th:first-child {{ text-align: left; width: 6%; }}
  .pfc {{ font-size: 6.5pt; }}
  .meal-cell {{ max-height: 22mm; overflow: hidden; line-height: 1.35; }}
  .meal-cell-sub {{ font-size: 6.5pt; max-height: 14mm; overflow: hidden; line-height: 1.35; }}
  .skip-cell {{ color: #8e8e93; font-size: 6.5pt; font-style: italic; }}
  .achievement-summary {{
    display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
    background: #f0f4ff; border: 0.5pt solid #c0d0f0;
    border-radius: 4px; padding: 3mm 4mm; margin-bottom: 3mm; font-size: 8pt;
  }}
  .ach-label {{ font-weight: 700; margin-right: 4px; }}
  @media print {{
    .achievement-summary {{
      background: #f5f7ff;
      border: 0.5pt solid #c0d0f0 !important;
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
    }}
  }}
  .sec-hd th, .sec-hd td {{
    background: #dde8f5; font-weight: 700;
  }}

  /* 2ページ目ヘッダー */
  .page2-title {{
    font-size: 9pt; font-weight: 700; color: #555;
    margin-bottom: 2mm; border-bottom: 0.5pt solid #ddd; padding-bottom: 1mm;
  }}

  .comment-section {{
    border: 0.5pt solid #aaa;
    padding: 2mm 3mm;
    border-radius: 2mm;
    margin-top: 2mm;
    font-size: 7pt;
    line-height: 1.4;
    overflow: hidden;
    column-count: 2;
    column-gap: 3mm;
  }}
  .box-title {{
    font-weight: 700; font-size: 8.5pt; margin-bottom: 1.5mm;
    border-bottom: 0.5pt solid #ddd; padding-bottom: 1mm;
    -webkit-column-span: all;
    column-span: all;
  }}
  ul {{ margin: 0; padding-left: 4mm; }}
  li {{ margin-bottom: 1mm; line-height: 1.5; }}

  /* ===== 印刷時 ===== */
  @media print {{
    @page {{ size: A4 landscape; margin: 8mm 10mm; }}
    body {{ background: #fff; font-size: 7.5pt; }}
    .print-toolbar {{ display: none; }}
    .page {{
      max-width: 100%;
      margin: 0;
      padding: 0;
      box-shadow: none;
    }}
    /* 1ページ目の終わりで改ページ */
    .page-1 {{ page-break-after: always; }}
    table {{ font-size: 7pt; }}
    th {{ font-size: 7.5pt; }}
    .page-label {{ display: none; }}
    .skip-cell {{ color: #636366; }}
    .sec-hd th, .sec-hd td {{
      border-top: 1.5pt solid #666;
      background: #e8e8e8 !important;
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
    }}
    .comment-section {{
      max-height: 65mm;
      overflow: hidden;
      column-count: 2;
      column-gap: 3mm;
    }}
  }}
</style>
</head>
<body>

<div class="print-toolbar">
  <h2>📄 食事記録レポート — {period_str}</h2>
  <button class="print-btn" onclick="window.print()">🖨 印刷 / PDF保存</button>
</div>

<!-- ===== 1ページ目：体重グラフ＋食事内容 ===== -->
<div class="page page-1">
  <div class="page-label">1 / 2</div>
  <h1>食事記録レポート　{period_str}</h1>
  <div class="sub">氏名：{data["user_name"]}　身長：{data["height_cm"]}cm　目標カロリー：{data["calorie_goal"]}kcal/日</div>

  <div class="chart-wrap">
    <img src="data:image/png;base64,{charts['weight']}" alt="体重推移"/>
  </div>

  <table>
    <colgroup>{colgroup}</colgroup>
    <thead>
      <tr><th></th>{date_headers}</tr>
    </thead>
    <tbody>
      {meal_rows}
    </tbody>
  </table>
</div>

<!-- ===== 2ページ目：栄養サマリー＋カロリー・歩数グラフ＋メモ ===== -->
<div class="page page-2">
  <div class="page-label">2 / 2</div>
  <div class="page2-title">
    食事記録レポート（続き）— {period_short}
    氏名：{data["user_name"]}　目標カロリー：{data["calorie_goal"]}kcal/日
  </div>

  {achievement_html}

  <table>
    <colgroup>{colgroup}</colgroup>
    <thead>
      <tr><th></th>{date_headers}</tr>
    </thead>
    <tbody>
      <tr class="sec-hd"><th>食事時刻</th>{meal_time_cells}</tr>
      <tr class="sec-hd"><th>Cal(kcal)</th>{cal_cells}</tr>
      <tr><th>目標差分</th>{diff_cells}</tr>
      <tr><th>PFC</th>{pfc_cells}</tr>
      <tr><th>塩分(g)</th>{sod_cells}</tr>
      <tr class="sec-hd"><th>体重・朝</th>{wm_cells}</tr>
      <tr><th>体重・夜</th>{we_cells}</tr>
      <tr><th>BMI</th>{bmi_cells}</tr>
      <tr><th>歩数</th>{steps_cells}</tr>
    </tbody>
  </table>

  <div class="chart-row">
    <img src="data:image/png;base64,{charts['calories']}" alt="カロリー推移"/>
    <img src="data:image/png;base64,{charts['steps']}"    alt="歩数推移"/>
  </div>

  <div class="comment-section">
    <div class="box-title">AI注釈：</div>
    {comment_html}
  </div>
</div>

</body>
</html>"""


def _format_structured_comment(comment: str) -> str:
    """構造化コメントをHTMLに変換（■見出し→<strong>、・項目→<li>）"""
    lines = [ln.strip() for ln in comment.splitlines() if ln.strip()]
    html_parts = []
    in_list = False
    for line in lines:
        if line.startswith("■") or line.startswith("#"):
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            if line.startswith("#"):
                stripped = re.sub(r"^#+\s*", "", line)
                display_line = f"■ {stripped}" if stripped else ""
            else:
                display_line = line
            if display_line:
                html_parts.append(
                    f'<div style="font-weight:700;font-size:7.5pt;margin-top:2mm;'
                    f'break-after:avoid;break-inside:avoid;'
                    f'-webkit-column-break-after:avoid;-webkit-column-break-inside:avoid">{display_line}</div>'
                )
        elif line.startswith("・") or line.startswith("- ") or line.startswith("• "):
            if not in_list:
                html_parts.append('<ul style="break-inside:avoid;-webkit-column-break-inside:avoid">')
                in_list = True
            text = line.lstrip("・•-").strip()
            html_parts.append(f"<li>{text}</li>")
        else:
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            html_parts.append(f"<p>{line}</p>")
    if in_list:
        html_parts.append("</ul>")
    return "\n".join(html_parts)


def _build_comment_summary(data: dict) -> dict:
    """AIコメント用のデータサマリーを構築"""
    days = data["days"]
    valid_cals = [d["calories"] for d in days if d["calories"] is not None]
    avg_cal = round(sum(valid_cals) / len(valid_cals)) if valid_cals else None

    _skip_ja = {"breakfast": "朝食", "lunch": "昼食", "dinner": "夕食"}
    skip_counts = {}
    for d in days:
        skipped = [
            mt for mt in ["breakfast", "lunch", "dinner"]
            if d.get("skipped", {}).get(mt) and not d.get("meals", {}).get(mt)
        ]
        if skipped:
            skip_counts[d["date"]] = [_skip_ja.get(mt, mt) for mt in skipped]

    height_cm = float(data.get("height_cm", 160))
    height_m = height_cm / 100.0

    summary = {
        "期間": f"{data['start']} 〜 {data['end']}",
        "目標カロリー": f"{data['calorie_goal']}kcal/日",
        "平均カロリー": f"{avg_cal}kcal" if avg_cal else "データなし",
        "記録日数": len(valid_cals),
        "目標超過日数": len([c for c in valid_cals if c > data["calorie_goal"]]),
        "日別データ": [],
    }
    for d in days:
        wm = d.get("weight_morning")
        we = d.get("weight_evening")
        avg_w = None
        if wm is not None and we is not None:
            avg_w = round((wm + we) / 2, 1)
        elif wm is not None:
            avg_w = wm
        elif we is not None:
            avg_w = we
        bmi = round(avg_w / (height_m ** 2), 1) if avg_w is not None and height_m > 0 else None

        meal_times = {}
        for mt in ["breakfast", "lunch", "dinner"]:
            times = [m.get("meal_time") for m in d.get("meals", {}).get(mt, []) if m.get("meal_time")]
            if times:
                meal_times[mt] = times[0][:2] + "時"

        summary["日別データ"].append({
            "日付": d["date"],
            "カロリー": d["calories"],
            "タンパク質g": d["protein"],
            "脂質g": d["fat"],
            "炭水化物g": d["carbs"],
            "塩分g": d["sodium"],
            "体重朝kg": wm,
            "体重夜kg": we,
            "BMI": bmi,
            "歩数": d["steps"],
            "食事時刻": meal_times if meal_times else None,
        })

    if skip_counts:
        summary["食事スキップ"] = skip_counts
    return summary


_COMMENT_PROMPT = """\
以下の1週間分の健康データを分析し、主治医（糖尿病・代謝・内分泌科）への\
提出レポートに添付する補足コメントを生成してください。

【出力形式】
セクションごとに見出しをつけ、各セクション1〜3項目の箇条書き（「・」始め）。
不要なセクションは省略可。

■ 前週比較（前週データがある場合のみ）
  - 体重・カロリー・PFC・歩数の前週比変化
■ パターン分析
  - 曜日別の傾向（週末の過食傾向、平日の欠食等）
  - PFCバランスの偏り傾向
  - 食事時刻と体重変動の相関
■ 臨床的所見
  - 目標カロリーとの乖離度
  - 塩分摂取傾向
  - 体重トレンド（増加/減少/横ばい）
  - BMI推移

【ルール】
- 医師が読むことを前提とした簡潔な医療向け日本語で記載
- 患者への励まし・アドバイス・提案は不要（医師向けデータ分析のみ）
- 特記事項がない場合は空文字のみを返す
- 客観的なデータに基づくコメントのみ
- 全体で400文字以内に収めること（印刷レイアウト制約）
"""


def _build_comment_prompt(focus_items: list[dict], is_monthly: bool = False) -> str:
    """フォーカス設定に基づいてAIコメントプロンプトを動的生成。
    週次で全項目OFFの場合のみ既存の _COMMENT_PROMPT（固定プロンプト）にフォールバック。
    月次で全項目OFFの場合は全項目ON相当のデフォルトで構築（月次専用プロンプトを必ず生成）。
    """
    enabled = {item["id"] for item in focus_items if item.get("enabled")}

    if not enabled:
        if is_monthly:
            enabled = {"meal_content", "calories", "pfc", "sodium", "expenditure",
                       "exercise", "weight", "steps", "blood_pressure", "body_fat"}
        else:
            return _COMMENT_PROMPT

    period_label = "1ヶ月（途中経過を含む）" if is_monthly else "1週間"
    report_type = "月次" if is_monthly else "週次"

    sections = []
    sections.append("■ 前回比較（前回データがある場合のみ）")
    if "weight" in enabled:
        sections.append("  - 体重の前回比変化・トレンド")
    if "calories" in enabled:
        sections.append("  - カロリー摂取の前回比変化")
    if "pfc" in enabled:
        sections.append("  - PFCバランスの前回比変化")
    if "steps" in enabled:
        sections.append("  - 歩数の前回比変化")
    if "expenditure" in enabled:
        sections.append("  - 消費カロリー（特に運動消費）の前回比変化")

    sections.append("■ パターン分析")
    if "meal_content" in enabled:
        sections.append("  - 食事内容の傾向（偏り・欠食パターン）")
    if "calories" in enabled:
        sections.append("  - 目標カロリーとの乖離度")
    if "pfc" in enabled:
        sections.append("  - PFCバランスの偏り傾向")
    if "sodium" in enabled:
        sections.append("  - 塩分摂取傾向")
    if "exercise" in enabled:
        sections.append("  - 運動内容・頻度の分析")
    if "expenditure" in enabled:
        sections.append("  - 基礎代謝外の消費カロリー分析（運動消費に重点）")

    sections.append("■ 臨床的所見")
    if "weight" in enabled:
        sections.append("  - 体重トレンド（増加/減少/横ばい）・BMI推移")
    if "blood_pressure" in enabled:
        sections.append("  - 血圧推移（朝/夜、正常範囲との比較）")
    if "body_fat" in enabled:
        sections.append("  - 体脂肪率推移")
    if "steps" in enabled:
        sections.append("  - 活動量（歩数）の評価")

    analysis_sections = "\n".join(sections)

    if is_monthly:
        rule_no_special = (
            "- 該当データが取得できないセクションのみ省略可。月次レポートでは最低1セクション以上の所見を必ず出力する\n"
            "- 記録日数が1ヶ月分に満たない場合（月途中の集計など）も、利用可能な日数のデータから所見を出力すること\n"
            "- セクション見出しは必ず「■」で始めること。Markdown記法（`#`・`##`等）、タイトル行、装飾記号は一切使用しない"
        )
    else:
        rule_no_special = "- 特記事項がない場合は空文字のみを返す"

    return f"""\
以下の{period_label}分の健康データを分析し、{report_type}レポートに添付する補足コメントを生成してください。

【出力形式】
セクションごとに見出しをつけ、各セクション1〜3項目の箇条書き（「・」始め）。
セクション見出しは必ず「■」で始めること（例: 「■ パターン分析」）。
不要なセクションは省略可。

{analysis_sections}

【ルール】
- 医師・トレーナーが読むことを前提とした簡潔な日本語で記載
- 患者への励まし・アドバイス・提案は不要（データ分析のみ）
{rule_no_special}
- 客観的なデータに基づくコメントのみ
- 全体で300文字以内に収めること（印刷レイアウト制約）
"""


async def generate_claude_comment(
    data: dict,
    prev_week: dict | None = None,
    focus_items: list[dict] | None = None,
    is_monthly: bool = False,
) -> str:
    """Claude Haiku で構造化補足コメントを生成（失敗時は空文字）"""
    try:
        api_key = os.getenv("ANTHROPIC_API_KEY") or database.get_setting("anthropic_api_key")
        if not api_key:
            return ""

        # フォーカス設定が未指定の場合は全項目有効
        if focus_items is None:
            focus_items = [{"id": k, "enabled": True} for k in
                           ["meal_content", "calories", "pfc", "sodium", "expenditure",
                            "exercise", "weight", "steps", "blood_pressure", "body_fat"]]

        enabled = {item["id"] for item in focus_items if item.get("enabled")}

        summary = _build_comment_summary(data)

        # フォーカス項目に応じて追加データをサマリーに含める
        if "blood_pressure" in enabled:
            bp_data = database.get_blood_pressure_range(data["start"], data["end"])
            if bp_data:
                summary["血圧データ"] = [
                    {"日付": r["log_date"], "時間帯": r["time_of_day"],
                     "収縮期": r["systolic"], "拡張期": r["diastolic"]}
                    for r in bp_data
                ]

        if "body_fat" in enabled:
            bf_data = database.get_body_fat_range(data["start"], data["end"])
            if bf_data:
                summary["体脂肪率データ"] = [
                    {"日付": r["log_date"], "体脂肪率": r["body_fat_pct"]}
                    for r in bf_data
                ]

        if "exercise" in enabled or "expenditure" in enabled:
            ex_data = database.get_exercise_logs(data["start"], data["end"])
            if ex_data:
                summary["運動データ"] = [
                    {"日付": r["log_date"], "消費kcal": r["calories_burned"],
                     "内容": r["description"]}
                    for r in ex_data
                ]

        # プロンプト生成を動的関数に切り替え
        comment_prompt = _build_comment_prompt(focus_items, is_monthly=is_monthly)

        period_label_data = "【今月データ】" if is_monthly else "【今週データ】"
        data_section = f"{period_label_data}\n{json.dumps(summary, ensure_ascii=False, indent=2)}"
        if prev_week:
            prev_label = "【前月サマリー】" if is_monthly else "【前週サマリー】"
            data_section += f"\n\n{prev_label}\n{json.dumps(prev_week, ensure_ascii=False, indent=2)}"

        prompt = f"{comment_prompt}\n{data_section}"

        client = anthropic.AsyncAnthropic(api_key=api_key)
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        logger.error("Claude comment generation failed: %s", e)
        return ""


def generate_monthly_charts_base64(data: dict) -> dict:
    """月次レポート用グラフ（体重・カロリー・歩数・PFC）を生成"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    _setup_matplotlib_font()

    days = data["days"]
    n = len(days)

    # x軸ラベル: 月初・月末・5日ごとのみ表示
    labels = []
    for d in days:
        dt = _date.fromisoformat(d["date"])
        if dt.day == 1 or dt.day == n or dt.day % 5 == 0:
            labels.append(f"{dt.month}/{dt.day}")
        else:
            labels.append("")

    x = list(range(n))
    goal_kcal = data.get("calorie_goal", 1500)
    steps_goal = data.get("steps_goal", 8000)

    # ── 体重推移（朝の折れ線 + トレンドライン）
    morning_w = [d["weight_morning"] for d in days]
    valid_w_idx = [i for i, v in enumerate(morning_w) if v is not None]
    valid_w_val = [morning_w[i] for i in valid_w_idx]
    trend = calculate_trend(x, morning_w)

    fig_w, ax_w = plt.subplots(figsize=(7, 2.0))
    if valid_w_idx:
        ax_w.plot(valid_w_idx, valid_w_val, color="#34c759", linewidth=1.5, zorder=2)
        ax_w.scatter(valid_w_idx, valid_w_val, color="#34c759", s=15, zorder=3)
        if trend is not None:
            s, ic = trend
            tx = [valid_w_idx[0], valid_w_idx[-1]]
            ty = [s * xi + ic for xi in tx]
            ax_w.plot(tx, ty, color="#ff3b30", linewidth=1, linestyle="--", zorder=1)
        margin = max(0.3, (max(valid_w_val) - min(valid_w_val)) * 0.15) if len(valid_w_val) > 1 else 0.3
        ax_w.set_ylim(min(valid_w_val) - margin, max(valid_w_val) + margin)
    ax_w.set_xticks(x)
    ax_w.set_xticklabels(labels, fontsize=5.5, rotation=45, ha="right")
    ax_w.set_ylabel("kg", fontsize=6)
    ax_w.tick_params(axis="y", labelsize=6)
    ax_w.set_title("体重推移（朝）", fontsize=8)
    fig_w.tight_layout(pad=0.3)
    weight_b64 = _fig_to_b64(fig_w)
    plt.close(fig_w)

    # ── カロリー推移（日別棒グラフ + 目標ライン）
    cal_vals = [d["calories"] if d["calories"] is not None else 0 for d in days]
    bar_colors = ["#f87171" if v > goal_kcal else "#60a5fa" for v in cal_vals]

    fig_c, ax_c = plt.subplots(figsize=(3.5, 2.0))
    ax_c.bar(x, cal_vals, color=bar_colors, alpha=0.85)
    ax_c.axhline(goal_kcal, color="#ff3b30", linestyle="--", linewidth=1,
                 label=f"目標 {goal_kcal}kcal")
    ax_c.set_xticks(x)
    ax_c.set_xticklabels(labels, fontsize=5.5, rotation=45, ha="right")
    ax_c.set_ylabel("kcal", fontsize=6)
    ax_c.tick_params(axis="y", labelsize=6)
    ax_c.set_title("カロリー推移", fontsize=8)
    ax_c.legend(fontsize=6)
    fig_c.tight_layout(pad=0.3)
    cal_b64 = _fig_to_b64(fig_c)
    plt.close(fig_c)

    # ── 歩数推移（日別棒グラフ + 目標ライン）
    steps_vals = [d["steps"] if d["steps"] is not None else 0 for d in days]
    step_colors = ["#34c759" if v >= steps_goal else "#8e8e93" for v in steps_vals]

    fig_s, ax_s = plt.subplots(figsize=(3.5, 2.0))
    ax_s.bar(x, steps_vals, color=step_colors, alpha=0.85)
    if steps_goal and steps_goal > 0:
        ax_s.axhline(steps_goal, color="#ff3b30", linestyle="--", linewidth=1,
                     label=f"目標 {steps_goal:,}歩")
        ax_s.legend(fontsize=6)
    ax_s.set_xticks(x)
    ax_s.set_xticklabels(labels, fontsize=5.5, rotation=45, ha="right")
    ax_s.set_ylabel("歩", fontsize=6)
    ax_s.tick_params(axis="y", labelsize=6)
    ax_s.set_title("歩数推移", fontsize=8)
    ax_s.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{int(v):,}"))
    fig_s.tight_layout(pad=0.3)
    steps_b64 = _fig_to_b64(fig_s)
    plt.close(fig_s)

    # ── PFC積み上げ棒グラフ
    pfc_data = build_pfc_chart_data(days, skip_dates=set())
    p_vals = [v or 0 for v in pfc_data["protein_kcal"]]
    f_vals = [v or 0 for v in pfc_data["fat_kcal"]]
    c_vals = [v or 0 for v in pfc_data["carb_kcal"]]
    pf_bottom = [p + f for p, f in zip(p_vals, f_vals)]

    fig_p, ax_p = plt.subplots(figsize=(7, 2.0))
    ax_p.bar(x, p_vals, label="P", color="#60a5fa", alpha=0.85)
    ax_p.bar(x, f_vals, bottom=p_vals, label="F", color="#fbbf24", alpha=0.85)
    ax_p.bar(x, c_vals, bottom=pf_bottom, label="C", color="#34d399", alpha=0.85)
    ax_p.axhline(goal_kcal, color="#ff3b30", linestyle="--", linewidth=1,
                 label=f"目標 {goal_kcal}kcal")
    ax_p.set_xticks(x)
    ax_p.set_xticklabels(labels, fontsize=5.5, rotation=45, ha="right")
    ax_p.set_ylabel("kcal", fontsize=6)
    ax_p.tick_params(axis="y", labelsize=6)
    ax_p.set_title("PFC推移", fontsize=8)
    ax_p.legend(fontsize=6)
    fig_p.tight_layout(pad=0.3)
    pfc_b64 = _fig_to_b64(fig_p)
    plt.close(fig_p)

    return {"weight": weight_b64, "calories": cal_b64, "steps": steps_b64, "pfc": pfc_b64}


def generate_monthly_report_html(data: dict, charts: dict, comment: str) -> str:
    """月次レポートHTML — A4縦1枚"""
    year_month = data["year_month"]
    year, month = int(year_month[:4]), int(year_month[5:])
    days = data["days"]

    valid_cals = [d["calories"] for d in days if d["calories"] is not None]
    avg_cal = round(sum(valid_cals) / len(valid_cals)) if valid_cals else None

    morning_weights = [d["weight_morning"] for d in days if d["weight_morning"] is not None]
    weight_change = None
    if len(morning_weights) >= 2:
        weight_change = round(morning_weights[-1] - morning_weights[0], 1)

    valid_steps = [d["steps"] for d in days if d["steps"] is not None]
    avg_steps = round(sum(valid_steps) / len(valid_steps)) if valid_steps else None

    if comment:
        comment_html = _format_structured_comment(comment)
    elif not valid_cals and not morning_weights and not valid_steps:
        comment_html = "<p>該当月の記録がありません</p>"
    else:
        comment_html = "<p>AI注釈の生成に失敗しました（データ不足または APIエラー）</p>"

    avg_cal_str = f"{avg_cal}kcal" if avg_cal is not None else "—"
    weight_change_str = f"{weight_change:+.1f}kg" if weight_change is not None else "—"
    avg_steps_str = f"{avg_steps:,}" if avg_steps else "—"

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8"/>
<title>月次健康レポート {year}年{month}月</title>
<style>
  body {{
    font-family: -apple-system, "Yu Gothic UI", "Hiragino Sans", "Noto Sans CJK JP", sans-serif;
    font-size: 8pt; margin: 0; background: #f0f0f5; color: #1c1c1e;
  }}
  .print-toolbar {{
    position: fixed; top: 0; left: 0; right: 0; z-index: 100;
    background: rgba(255,255,255,0.92); backdrop-filter: blur(8px);
    border-bottom: 1px solid #e5e5ea;
    padding: 8px 16px; display: flex; align-items: center; gap: 12px;
  }}
  .print-toolbar h2 {{ font-size: 13px; font-weight: 700; flex: 1; margin: 0; }}
  .print-btn {{
    padding: 7px 18px; background: #007aff; color: #fff;
    border: none; border-radius: 8px; font-size: 13px;
    font-weight: 600; cursor: pointer;
  }}
  .page {{
    max-width: 190mm; margin: 52px auto 12px; background: #fff;
    padding: 8mm 10mm; box-shadow: 0 2px 12px rgba(0,0,0,0.12);
  }}
  h1 {{ font-size: 12pt; margin: 0 0 1mm; }}
  .sub {{ font-size: 8pt; color: #555; margin-bottom: 2mm; }}
  .summary-grid {{
    display: grid; grid-template-columns: repeat(4, 1fr);
    gap: 1.5mm; margin-bottom: 2mm;
  }}
  .summary-card {{
    border: 0.5pt solid #aaa; border-radius: 2mm;
    padding: 2mm 3mm; text-align: center;
  }}
  .summary-card .value {{ font-size: 11pt; font-weight: 700; }}
  .summary-card .label {{ font-size: 7pt; color: #666; }}
  .chart-wrap {{ margin-bottom: 1mm; }}
  .chart-wrap img {{ width: 100%; display: block; }}
  .chart-row {{ display: flex; gap: 2mm; margin-bottom: 1mm; }}
  .chart-row img {{ flex: 1; width: 0; min-width: 0; }}
  .comment-section {{
    border: 0.5pt solid #aaa; padding: 2mm 3mm;
    border-radius: 2mm; margin-top: 2mm;
    font-size: 7pt; line-height: 1.4; overflow: hidden;
  }}
  .box-title {{
    font-weight: 700; font-size: 8pt; margin-bottom: 1mm;
    border-bottom: 0.5pt solid #ddd; padding-bottom: 1mm;
  }}
  ul {{ margin: 0; padding-left: 4mm; }}
  li {{ margin-bottom: 0.5mm; line-height: 1.4; }}
  @media print {{
    @page {{ size: A4 portrait; margin: 8mm 10mm; }}
    body {{ background: #fff; }}
    .print-toolbar {{ display: none; }}
    .page {{ max-width: 100%; margin: 0; padding: 0; box-shadow: none; }}
    .comment-section {{ max-height: 90mm; overflow: hidden; }}
  }}
</style>
</head>
<body>
<div class="print-toolbar">
  <h2>月次健康レポート — {year}年{month}月</h2>
  <button class="print-btn" onclick="window.print()">印刷 / PDF保存</button>
</div>
<div class="page">
  <h1>月次健康レポート　{year}年{month}月</h1>
  <div class="sub">
    氏名：{data["user_name"]}
    身長：{data["height_cm"]}cm
    目標カロリー：{data["calorie_goal"]}kcal/日
    記録日数：{len(valid_cals)}日
  </div>

  <div class="summary-grid">
    <div class="summary-card">
      <div class="value">{avg_cal_str}</div>
      <div class="label">平均摂取カロリー</div>
    </div>
    <div class="summary-card">
      <div class="value">{weight_change_str}</div>
      <div class="label">体重変化</div>
    </div>
    <div class="summary-card">
      <div class="value">{avg_steps_str}</div>
      <div class="label">平均歩数</div>
    </div>
    <div class="summary-card">
      <div class="value">{data["total_days"]}日</div>
      <div class="label">対象期間</div>
    </div>
  </div>

  <div class="chart-wrap">
    <img src="data:image/png;base64,{charts['weight']}" alt="体重推移"/>
  </div>
  <div class="chart-row">
    <img src="data:image/png;base64,{charts['calories']}" alt="カロリー推移"/>
    <img src="data:image/png;base64,{charts['steps']}" alt="歩数推移"/>
  </div>
  <div class="chart-wrap">
    <img src="data:image/png;base64,{charts['pfc']}" alt="PFC推移"/>
  </div>

  <div class="comment-section">
    <div class="box-title">AI注釈：</div>
    {comment_html}
  </div>
</div>
</body>
</html>"""
