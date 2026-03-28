"""レポート生成モジュール — グラフ・HTML・Claude補足コメント（ブラウザ印刷方式）"""

import io
import base64
import json
import os
from datetime import date as _date

import anthropic
import database
from database import SKIP_MEAL_TYPES as SKIP_LABEL_TYPES

WEEKDAYS_JA = ["月", "火", "水", "木", "金", "土", "日"]

# 1セルあたりの最大文字数（溢れ防止のフォールバック）
_TRUNCATE_LEN = 30


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

    fig_w, ax_w = plt.subplots(figsize=(10, 2.4))
    if valid_idx:
        ax_w.plot(valid_idx, valid_y, color="#8e8e93", linewidth=1.5, zorder=1)
        for xi, yi, ci in zip(valid_idx, valid_y, valid_col):
            ax_w.scatter([xi], [yi], color=ci, s=30, zorder=2)
        margin = max(0.5, (max(valid_y) - min(valid_y)) * 0.15) if len(valid_y) > 1 else 0.5
        ax_w.set_ylim(min(valid_y) - margin, max(valid_y) + margin)
    ax_w.set_xticks(range(len(w_labels)))
    ax_w.set_xticklabels(w_labels, fontsize=7, rotation=45, ha="right")
    ax_w.set_ylabel("kg", fontsize=8)
    ax_w.tick_params(axis="y", labelsize=8)
    ax_w.set_title("体重推移", fontsize=10)
    ax_w.legend(handles=[
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#34c759", markersize=6, label="朝"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#ff9500", markersize=6, label="夜"),
    ], fontsize=8)
    fig_w.tight_layout(pad=0.4)
    weight_b64 = _fig_to_b64(fig_w)
    plt.close(fig_w)

    # ── カロリー推移（棒グラフ＋目標ライン）──────────────
    goal = data.get("calorie_goal", 1500)
    c_labels = [_date_label(d["date"]) for d in days]
    c_vals   = [d["calories"] if d["calories"] is not None else 0 for d in days]
    c_colors = ["#ff3b30" if v > goal else "#007aff" for v in c_vals]

    fig_c, ax_c = plt.subplots(figsize=(4.8, 2.2))
    ax_c.bar(c_labels, c_vals, color=c_colors, alpha=0.8)
    ax_c.axhline(goal, color="#ff3b30", linestyle="--", linewidth=1, label=f"目標 {goal}kcal")
    ax_c.set_title("カロリー推移", fontsize=9)
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

    fig_s, ax_s = plt.subplots(figsize=(4.8, 2.2))
    ax_s.bar(s_labels, s_vals, color="#34c759", alpha=0.8)
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


def _truncate(text: str, max_len: int = _TRUNCATE_LEN) -> str:
    """フォールバック省略（30文字超のみ）"""
    return text if len(text) <= max_len else text[:max_len] + "…"


def generate_report_html(data: dict, charts: dict, comment: str) -> str:
    days  = data["days"]
    start = _date.fromisoformat(data["start"])
    end   = _date.fromisoformat(data["end"])

    def fmt_header(iso: str) -> str:
        d = _date.fromisoformat(iso)
        return f"{d.month}/{d.day}<br/>({WEEKDAYS_JA[d.weekday()]})"

    def meals_cell(meal_list: list, is_skipped: bool = False) -> str:
        if meal_list:
            return "<br/>".join(_truncate(m["description"]) for m in meal_list)
        if is_skipped:
            return '<span class="skip-cell">食べなかった</span>'
        return ""

    def dash(v, fmt: str = "{}") -> str:
        return "&#8212;" if v is None else fmt.format(v)

    MEAL_TYPES  = ["breakfast", "lunch", "dinner", "snack", "late_night"]
    MEAL_LABELS = {"breakfast": "朝食", "lunch": "昼食", "dinner": "夕食",
                   "snack": "間食", "late_night": "夜食"}

    date_headers = "".join(f"<th>{fmt_header(d['date'])}</th>" for d in days)

    meal_rows = ""
    for mt in MEAL_TYPES:
        cells = ""
        for d in days:
            is_skipped = (
                mt in SKIP_LABEL_TYPES
                and d.get("skipped", {}).get(mt, False)
                and not d["meals"][mt]
            )
            cells += f"<td>{meals_cell(d['meals'][mt], is_skipped)}</td>"
        meal_rows += f"<tr><th>{MEAL_LABELS[mt]}</th>{cells}</tr>\n"

    def pfc(d: dict) -> str:
        p = dash(d["protein"]); f_ = dash(d["fat"]); c = dash(d["carbs"])
        return f"P{p}<br/>F{f_}<br/>C{c}"

    cal_cells   = "".join(f"<td>{dash(d['calories'])}</td>"         for d in days)
    pfc_cells   = "".join(f"<td class='pfc'>{pfc(d)}</td>"          for d in days)
    sod_cells   = "".join(f"<td>{dash(d['sodium'])}</td>"           for d in days)
    wm_cells    = "".join(f"<td>{dash(d['weight_morning'])}</td>"   for d in days)
    we_cells    = "".join(f"<td>{dash(d['weight_evening'])}</td>"   for d in days)
    steps_cells = "".join(f'<td>{dash(d["steps"], "{:,}")}</td>'   for d in days)

    if comment:
        items = [ln.strip().lstrip("・•-").strip() for ln in comment.splitlines() if ln.strip()]
        comment_html = "<ul>" + "".join(f"<li>{it}</li>" for it in items) + "</ul>"
    else:
        comment_html = "<p>特記事項なし</p>"

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
  .skip-cell {{ color: #8e8e93; font-size: 6.5pt; font-style: italic; }}
  .sec-hd th, .sec-hd td {{
    background: #dde8f5; font-weight: 700;
  }}

  /* 2ページ目ヘッダー */
  .page2-title {{
    font-size: 9pt; font-weight: 700; color: #555;
    margin-bottom: 2mm; border-bottom: 0.5pt solid #ddd; padding-bottom: 1mm;
  }}

  .bottom {{
    display: flex; gap: 3mm; margin-top: 3mm;
  }}
  .bottom > div {{
    flex: 1;
    border: 0.5pt solid #aaa;
    padding: 2mm 3mm;
    min-height: 18mm;
    border-radius: 2mm;
  }}
  .box-title {{
    font-weight: 700; font-size: 8.5pt; margin-bottom: 1.5mm;
    border-bottom: 0.5pt solid #ddd; padding-bottom: 1mm;
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

  <table>
    <colgroup>{colgroup}</colgroup>
    <thead>
      <tr><th></th>{date_headers}</tr>
    </thead>
    <tbody>
      <tr class="sec-hd"><th>Cal(kcal)</th>{cal_cells}</tr>
      <tr><th>P/F/C(g)</th>{pfc_cells}</tr>
      <tr><th>塩分(g)</th>{sod_cells}</tr>
      <tr class="sec-hd"><th>体重・朝</th>{wm_cells}</tr>
      <tr><th>体重・夜</th>{we_cells}</tr>
      <tr><th>歩数</th>{steps_cells}</tr>
    </tbody>
  </table>

  <div class="chart-row">
    <img src="data:image/png;base64,{charts['calories']}" alt="カロリー推移"/>
    <img src="data:image/png;base64,{charts['steps']}"    alt="歩数推移"/>
  </div>

  <div class="bottom">
    <div>
      <div class="box-title">メモ欄：</div>
    </div>
    <div>
      <div class="box-title">Claude補足：</div>
      {comment_html}
    </div>
  </div>
</div>

</body>
</html>"""


async def generate_claude_comment(data: dict) -> str:
    """Claude Haiku で補足コメントを生成（失敗時は空文字）"""
    try:
        api_key = os.getenv("ANTHROPIC_API_KEY") or database.get_setting("anthropic_api_key")
        if not api_key:
            return ""

        days = data["days"]
        valid_cals = [d["calories"] for d in days if d["calories"] is not None]
        avg_cal = round(sum(valid_cals) / len(valid_cals)) if valid_cals else None

        _skip_ja = {"breakfast": "朝食", "lunch": "昼食", "dinner": "夕食"}
        skip_counts = {}
        for d in days:
            skipped = [
                mt for mt in ["breakfast", "lunch", "dinner"]
                if d.get("skipped", {}).get(mt) and not d["meals"].get(mt)
            ]
            if skipped:
                skip_counts[d["date"]] = [_skip_ja.get(mt, mt) for mt in skipped]

        summary = {
            "期間": f"{data['start']} 〜 {data['end']}",
            "目標カロリー": f"{data['calorie_goal']}kcal/日",
            "平均カロリー": f"{avg_cal}kcal" if avg_cal else "データなし",
            "記録日数": len(valid_cals),
            "目標超過日数": len([c for c in valid_cals if c > data["calorie_goal"]]),
            "日別データ": [
                {
                    "日付": d["date"],
                    "カロリー": d["calories"],
                    "タンパク質g": d["protein"],
                    "脂質g": d["fat"],
                    "炭水化物g": d["carbs"],
                    "塩分g": d["sodium"],
                    "体重朝kg": d["weight_morning"],
                    "体重夜kg": d["weight_evening"],
                    "歩数": d["steps"],
                }
                for d in days
            ],
        }
        if skip_counts:
            summary["食事スキップ"] = skip_counts

        prompt = (
            "以下の1週間分の食事・体重・歩数データを分析し、"
            "主治医（糖尿病・代謝・内分泌科）への提出レポートに添付する補足コメントを生成してください。\n\n"
            "【ルール】\n"
            "- 医師が読むことを前提とした簡潔な日本語で記載\n"
            "- 特記事項がない場合は空文字のみを返す\n"
            "- 箇条書きで3項目以内（各項目は「・」で始める）\n"
            "- 客観的なデータに基づくコメントのみ（励ましや感想は不要）\n\n"
            f"【データ】\n{json.dumps(summary, ensure_ascii=False, indent=2)}"
        )

        client = anthropic.AsyncAnthropic(api_key=api_key)
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception:
        return ""
