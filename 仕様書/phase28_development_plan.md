# Phase 28 開発プラン — UIデザイン全面刷新

## 目次

| Step | ファイル | 内容 |
|------|---------|------|
| Phase28-Step1 | common.css | CSS変数置き換え・サイドバークラスのダーク化 |
| Phase28-Step2 | 全5HTMLページ | Google Fonts（BIZ UDGothic）リンク追加 |
| Phase28-Step3 | index.html | チャット画面ダッシュボードHTML構造更新（3カラムグリッド） |
| Phase28-Step4 | stats.html | Chart.jsカラーパレット更新 |
| Phase28-Step5 | report_generator.py | レポートカラー・フォント更新 + 2ページ目デザイン改善 |

---

## 概要

`samples/HealthReport-handoff.zip` 内のデザインモック（`HealthReport Design.html` / `HealthReport Report Design.html`）に基づき、アプリ全体のビジュアルを刷新する。

| 項目 | 旧 | 新 |
|------|----|----|
| アクセントカラー | `#007aff`（iOS ブルー）| `#2BA899`（ティール）|
| 背景色（ライト） | `#f2f2f7` | `#F5F4F0`（ウォームオフホワイト）|
| カード背景 | `#ffffff` | `#FFFFFF`（+ shadow強化）|
| 角丸 | `12px` | `14px` |
| フォント | システムフォント（BIZ UDPGothic候補）| `BIZ UDGothic`（Google Fonts）|
| サイドバー背景 | ライト/ダーク連動 | 常時ダーク `#1A2B38`（ツートーン）|
| Chartカラー | Chart.jsデフォルト | 統一パレット（ティール・アンバー・ペリウィンクル・コーラル）|

### 設計方針
- **要素・機能は変更しない**（グラフ種類・表示項目は Phase 13/25 系で確定済み）
- **CSS変数の直接置き換え**（変数名はそのまま、値のみ更新）
- **モバイル**: レイアウト構造維持、カラーのみ新テーマを適用
- **レポートP1**: レイアウト固定（カラー・フォントのみ更新）
- **レポートP2**: デザイン強化 + 情報集約（メトリクスカード + 2カラムレイアウト）

---

## Step 1 — common.css: CSS変数置き換え・サイドバークラスのダーク化

### 変更内容

#### `:root`（ライトモード変数）

| 変数 | 旧値 | 新値 |
|------|------|------|
| `--bg` | `#f2f2f7` | `#F5F4F0` |
| `--header-bg` | `rgba(255,255,255,0.85)` | `#FFFFFF` |
| `--card-bg` | `#ffffff` | `#FFFFFF` |
| `--bubble-user` | `#007aff` | `#2BA899` |
| `--accent` | `#007aff` | `#2BA899` |
| `--success` | `#34c759` | `#2BA899` |
| `--border` | `#e5e5ea` | `#EDECE8` |
| `--text-secondary` | `#8e8e93` | `#6B7280` |
| `--text-primary` | `#1c1c1e` | `#1A1A1E` |
| `--radius` | `12px` | `14px` |
| `--card-shadow` | `0 1px 4px rgba(0,0,0,0.08)` | `0 2px 12px rgba(0,0,0,0.07), 0 1px 3px rgba(0,0,0,0.04)` |
| `--input-bg` | `#ffffff` | `#FFFFFF` |

#### `@media (prefers-color-scheme: dark)` および `[data-theme="dark"]`（ダークモード変数）

| 変数 | 旧値 | 新値 |
|------|------|------|
| `--bg` | `#1c1c1e` | `#141B22` |
| `--header-bg` | `rgba(28,28,30,0.88)` | `#1A2B38` |
| `--card-bg` | `#2c2c2e` | `#1E2B38` |
| `--bubble-user` | `#0a84ff` | `#2BA899` |
| `--accent` | `#0a84ff` | `#2BA899` |
| `--success` | `#30d158` | `#2BA899` |
| `--border` | `#38383a` | `#2A3A48` |
| `--text-secondary` | `#98989d` | `#7A8B97` |
| `--text-primary` | `#f2f2f7` | `#EEEDe8` |
| `--card-shadow` | `0 1px 6px rgba(0,0,0,0.32)` | `0 2px 14px rgba(0,0,0,0.4)` |
| `--input-bg` | `#2c2c2e` | `#1E2B38` |

#### サイドバー専用変数（`:root` に追加）

```css
--sidebar-bg: #1A2B38;
--sidebar-border: transparent;
--sidebar-nav-text: rgba(255,255,255,0.55);
--sidebar-nav-active-bg: rgba(255,255,255,0.1);
--sidebar-nav-active-text: #ffffff;
--sidebar-nav-active-border: #2BA899;
--sidebar-app-title-color: rgba(255,255,255,0.9);
--sidebar-footer-color: rgba(255,255,255,0.4);
--sidebar-footer-border: rgba(255,255,255,0.08);
```

これらはライト・ダーク両方で同値（サイドバーは常にダーク）。

#### `.sidebar-*` クラス更新

```css
#sidebar {
  background: var(--sidebar-bg);
  border-right: 1px solid var(--sidebar-border);
}

.sidebar-app-title {
  color: var(--sidebar-app-title-color);
}

.sidebar-nav-item {
  color: var(--sidebar-nav-text);
  border-left: 2px solid transparent;
  border-radius: 10px;
}

.sidebar-nav-item.current {
  background: var(--sidebar-nav-active-bg);
  color: var(--sidebar-nav-active-text);
  border-left-color: var(--sidebar-nav-active-border);
  font-weight: 600;
}

.sidebar-footer {
  border-top: 1px solid var(--sidebar-footer-border);
}

.sidebar-footer-btn {
  color: var(--sidebar-footer-color);
}
```

#### `body` フォントスタック更新

```css
body {
  font-family: "BIZ UDGothic", "BIZ UDPGothic", -apple-system, BlinkMacSystemFont, sans-serif;
}
```

#### カード `.card` / `.stat-card` 等の角丸・shadow更新

`--radius` が `14px` に変わることで自動反映。`var(--card-shadow)` 参照箇所も自動反映。

### テスト
視覚変更のため自動テストなし。サーバー起動後に全ページの表示を目視確認。

---

## Step 2 — 全5HTMLページ: Google Fonts リンク追加

### 対象ファイル
`static/index.html`, `static/history.html`, `static/stats.html`, `static/settings.html`, `static/report.html`

### 変更内容
各ファイルの `<head>` 内、既存 `<link rel="stylesheet" href="/static/common.css">` の**前**に追加：

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=BIZ+UDGothic:wght@400;700&display=swap" rel="stylesheet">
```

### テスト
視覚変更のため自動テストなし。各ページで DevTools → Network タブで `BIZ+UDGothic` フォントが読み込まれること、フォントが変化していることを目視確認。

---

## Step 3 — index.html: チャット画面ダッシュボードHTML構造更新

### 現行構造

現在のダッシュボードパネル（`.daily-summary` 等）は横並びのシンプルな構成。

### 新構造

```html
<!-- デイリーサマリーパネル（新3カラムグリッドデザイン） -->
<div id="daily-summary" class="daily-summary-new">
  <!-- 日付 + BMI 行 -->
  <div class="ds-meta-row">
    <span id="ds-date" class="ds-date"></span>
    <span id="ds-bmi" class="ds-bmi"></span>
  </div>
  <!-- 3カラムメトリクスグリッド -->
  <div class="ds-metrics">
    <div class="ds-metric" id="ds-cal">
      <div class="ds-metric-icon">🔥</div>
      <div class="ds-metric-val" id="ds-cal-val">—</div>
      <div class="ds-metric-sub" id="ds-cal-sub">/ 1,800 kcal</div>
    </div>
    <div class="ds-metric">
      <div class="ds-metric-icon">⚖️</div>
      <div class="ds-metric-val" id="ds-weight-val">—</div>
      <div class="ds-metric-sub">kg (朝)</div>
    </div>
    <div class="ds-metric">
      <div class="ds-metric-icon">👟</div>
      <div class="ds-metric-val" id="ds-steps-val">—</div>
      <div class="ds-metric-sub">歩</div>
    </div>
  </div>
  <!-- カロリー進捗バー -->
  <div class="ds-progress-row">
    <div class="ds-progress-bar"><div id="ds-progress-fill" class="ds-progress-fill"></div></div>
    <span id="ds-progress-pct" class="ds-progress-pct">0%</span>
  </div>
  <!-- 食事ステータスチップ -->
  <div class="ds-meal-chips" id="ds-meal-chips"></div>
</div>
```

CSS（`common.css` に追加、または `index.html` の `<style>` タグに追加）：

```css
.daily-summary-new {
  background: var(--card-bg);
  border-bottom: 1px solid var(--border);
  padding: 12px 20px 14px;
  box-shadow: var(--card-shadow);
}
.ds-meta-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
  font-size: 12px;
  color: var(--text-secondary);
}
.ds-metrics {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 10px;
  margin-bottom: 10px;
}
.ds-metric {
  background: var(--bg);
  border-radius: 10px;
  padding: 8px 10px;
}
.ds-metric-icon { font-size: 11px; color: var(--text-secondary); margin-bottom: 3px; }
.ds-metric-val  { font-size: 16px; font-weight: 700; color: var(--accent); line-height: 1; }
.ds-metric:nth-child(2) .ds-metric-val,
.ds-metric:nth-child(3) .ds-metric-val { color: var(--text-primary); }
.ds-metric-sub  { font-size: 11px; color: var(--text-secondary); }
.ds-progress-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 10px;
}
.ds-progress-bar {
  flex: 1; height: 5px;
  background: var(--bg); border-radius: 3px; overflow: hidden;
}
.ds-progress-fill {
  height: 100%; width: 0%;
  background: var(--accent); border-radius: 3px;
  transition: width 0.3s;
}
.ds-progress-pct { font-size: 11px; color: var(--text-secondary); }
.ds-meal-chips   { display: flex; gap: 6px; flex-wrap: wrap; }
.ds-chip {
  font-size: 12px; padding: 3px 10px; border-radius: 20px;
  background: var(--bg); color: var(--text-secondary);
}
.ds-chip.done {
  background: rgba(43,168,153,0.12); color: var(--accent); font-weight: 600;
}
```

既存の JS（`updateDailySummary()` 等）も新しい要素 ID に合わせて更新する。

### テスト
`uv run pytest test/ -v -k "summary or daily"` で関連テストを確認。視覚確認はサーバー起動後にチャット画面を目視。

---

## Step 4 — stats.html: Chart.jsカラーパレット更新

### 新カラー定数

`stats.html` のスクリプト冒頭（`CHART_DRAW_FUNCTIONS` の前）に定数を追加・更新：

```js
const CHART_COLORS = {
  primary:    '#2BA899',  // ティール  — 摂取カロリー・朝食・タンパク質
  secondary:  '#E8A042',  // アンバー  — 消費カロリー・夕食・脂質
  tertiary:   '#7B9ED9',  // ペリウィンクル — 炭水化物・最低血圧
  danger:     '#E06B62',  // コーラル  — 最高血圧・体脂肪率
  dangerLt:   '#F4A89E',  // ライトコーラル
  tertiaryLt: '#A8C5E2',  // ライトペリウィンクル
  target:     '#C4B9A8',  // ウォームグレー（破線・目標）
};
```

### 各グラフへの適用

| グラフ | 変更箇所 |
|--------|---------|
| カロリー推移 | 摂取 → `CHART_COLORS.primary`、消費 → `CHART_COLORS.secondary`、目標破線 → `CHART_COLORS.target` |
| 体重推移 | 折れ線 → `CHART_COLORS.primary`、目標破線 → `CHART_COLORS.target` |
| 歩数推移 | バー → `CHART_COLORS.primary`、目標破線 → `CHART_COLORS.target` |
| PFC積み上げ | P → `CHART_COLORS.primary`、F → `CHART_COLORS.secondary`、C → `CHART_COLORS.tertiary` |
| 血圧 | 最高 → `CHART_COLORS.danger`、最低 → `CHART_COLORS.tertiary` |
| 体脂肪率 | 折れ線 → `CHART_COLORS.danger` |

既存の `chartDefaults` オブジェクト内の `gridColor`・`tickColor` も `var(--border)` / `var(--text-secondary)` に合わせて確認・更新。

### テスト
`uv run pytest test/ -v -k "stats"` で関連テストを確認。視覚確認は集計ページの全グラフを目視。

---

## Step 5 — report_generator.py: カラー・フォント更新 + 2ページ目デザイン改善

### 5-1: スタイル変数の更新（全ページ共通）

`report_generator.py` 内の CSS 文字列を更新：

```css
:root {
  --teal:   #2BA899;
  --amber:  #E8A042;
  --coral:  #E06B62;
  --lav:    #7B9ED9;
  --text:   #1A1A1E;
  --sub:    #6B7280;
  --border: #EDECE8;
  --bg:     #F7F6F3;
  --white:  #FFFFFF;
}
body {
  font-family: 'BIZ UDGothic', 'BIZ UDPGothic', -apple-system, sans-serif;
}
```

`<head>` に Google Fonts リンク追加：

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=BIZ+UDGothic:wght@400;700&display=swap" rel="stylesheet">
```

レポートヘッダー（`<h1>`・`.sub`）にティール背景を適用：

```css
.report-header {
  background: var(--teal);
  color: #fff;
  padding: 10px 20px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
```

### 5-2: 1ページ目（レイアウト固定・カラーのみ更新）

- 食事テーブルのヘッダー背景: `var(--bg)` → そのまま（テーブル構造変更なし）
- 食事バッジ（朝食/昼食/夕食/間食）: 新テーマカラーに更新
  - 朝食: `rgba(43,168,153,.13)` + `#1E7A6E`
  - 昼食: `rgba(232,160,66,.13)` + `#B57A20`
  - 夕食: `rgba(123,158,217,.13)` + `#4B72A8`
  - 間食: `rgba(155,126,212,.13)` + `#6B4EA8`

### 5-3: 2ページ目デザイン改善

**新レイアウト構成**:

```
┌─────────────────────────────────────────────────┐
│ ページヘッダー（ティール背景）                     │
├─────────────────────────────────────────────────┤
│ メトリクスカード3枚横並び                          │
│ [平均カロリー] [平均体重] [平均歩数]               │
├─────────────┬───────────────────────────────────┤
│ 日次データ  │ カロリーグラフ                      │
│ テーブル    ├───────────────────────────────────┤
│（Cal/差分  │ 歩数グラフ                          │
│ PFC/塩分   ├───────────────────────────────────┤
│ 体重/BMI   │ AI注釈（3カラム：課題・良好・提案）  │
│ 歩数）     │                                     │
└─────────────┴───────────────────────────────────┘
```

**メトリクスカード HTML テンプレート**（Python 文字列として生成）:

```html
<div class="cards">
  <div class="card">
    <div class="card-lbl">平均カロリー</div>
    <div class="card-val c-teal">{avg_cal_str}</div>
    <div class="card-sub">kcal/日（目標 {calorie_goal}）</div>
  </div>
  <div class="card">
    <div class="card-lbl">平均体重</div>
    <div class="card-val c-amber">{avg_weight_str}</div>
    <div class="card-sub">kg</div>
  </div>
  <div class="card">
    <div class="card-lbl">平均歩数</div>
    <div class="card-val c-lav">{avg_steps_str}</div>
    <div class="card-sub">歩/日</div>
  </div>
</div>
```

**2カラムレイアウト**:

```html
<div class="cols">
  <div class="left-col">
    <!-- 既存の日次数値テーブル（Cal/差分/PFC/塩分/体重朝夜/BMI/歩数）-->
    {daily_table_html}
  </div>
  <div class="right-panel">
    <!-- カロリーグラフ -->
    <div class="chart-box">
      <div class="chart-lbl">🔥 カロリー推移</div>
      <img src="data:image/png;base64,{charts['calories']}" .../>
    </div>
    <!-- 歩数グラフ -->
    <div class="chart-box">
      <div class="chart-lbl">👟 歩数推移</div>
      <img src="data:image/png;base64,{charts['steps']}" .../>
    </div>
    <!-- AI注釈（3カラム） -->
    <div class="ai-box">
      <div class="ai-title">✦ AI 分析</div>
      <div class="ai-grid">
        {ai_sections_html}
      </div>
    </div>
  </div>
</div>
```

AI注釈の3カラム分割は既存の `_format_structured_comment()` 出力を `■` 見出しで3ブロックに分割してレンダリング。

### テスト

```bash
uv run pytest test/ -v -k "report"
```

加えて、レポートエンドポイント（`/api/report`）に対してテスト用リクエストを送りPDF/HTML出力を目視確認。

---

## 完了基準

- [x] 全ページでティールアクセント（`#2BA899`）が適用されている
- [x] サイドバーがライト・ダーク両モードでダーク背景（`#1A2B38`）
- [x] BIZ UDGothic フォントが全ページで読み込まれている
- [x] チャット画面ダッシュボードが3カラムグリッドになっている
- [x] 集計ページのグラフが新カラーパレットで描画されている
- [x] レポートP1のレイアウトが変わっていない（カラー・フォントのみ更新）
- [x] レポートP2にメトリクスカード + 2カラムレイアウトが実装されている
- [ ] モバイル表示で既存レイアウトが崩れていない（目視確認要）
- [ ] ダークモード切替で各ページが正しく表示される（目視確認要）

---

## 実施サマリー（2026-04-22）

### 実施内容

| Step | コミット | 結果 |
|------|---------|------|
| Step1 | ce1f683, 6f9bbff | common.css: CSS変数全置き換え + サイドバーダーク化 + @mediaバグ修正 |
| Step2 | 72ca0a7 | 全5ページにGoogle Fonts追加（common.css前に挿入）|
| Step3 | 6990f7b, ca34dc5 | index.htmlダッシュボード3カラムグリッド + onclick規約修正 |
| Step4 | d77271b, 40a354b | stats.html CHART_COLORS定数導入 + 体重グラフ朝夜カラー統一 |
| Step5 | b0ecc94, b68766e | report_generator.py 新テーマ + 2ページ目改善 + html.escape漏れ修正 |

### レビューで発見・修正した問題

- **Step1**: `@media` ダークブロックに `--skip-badge-bg` 欠落 → 追加
- **Step1**: `--text-primary` の大文字小文字混在（`#EEEDe8`）→ `#EEEDE8` に統一
- **Step3**: `addPreviewItem()` のinline onclick → `addEventListener` に修正
- **Step3**: `showChoiceButtons()` の `btn.onclick` → `addEventListener` に修正
- **Step3**: `loadQuickPalette()` の `data-text` エスケープ問題 → `replace(/"/g, '&quot;')` に修正
- **Step4**: 体重グラフ朝/夜カラー（`#34c759`/`#ff9500`）が未変換 → `CHART_COLORS.primary/secondary` に統一
- **Step5**: `user_name` / `description` / AI注釈テキストの `html.escape()` 漏れ → 全修正

### テスト結果

- `uv run pytest test/ -v`: **86 passed, 0 failed**
- 目視確認: レポート生成・全ページ表示は実装者確認済み。ダークモード・モバイルの最終目視確認はユーザーが実施すること。

### レビュアーへの報告事項

- 実装はすべてスペック準拠レビュー + コード品質レビューの2段階レビューをパス
- モバイルレイアウト崩れの有無は実機確認を推奨
- `loadVitalCharts()` 内（睡眠・脈拍・SpO2）のハードコードカラーは今フェーズでは対象外とした（バイタル専用の医療系カラーのため別途検討）
