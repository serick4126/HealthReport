# Phase 35 コピーボタン機能 実装プラン

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 履歴ページと集計ページに「コピー▾」ボタンを追加し、1日分／期間分の健康データをタブ区切り・JSON形式でクリップボードにコピーできるようにする。

**Architecture:** 純フロントエンド実装（新規APIエンドポイントなし）。database.py の `get_stats()` に sodium を追加し、history.html・stats.html 内の JS に BMR計算ユーティリティ・TSV/JSON生成関数・イベントハンドラを追加する。

**Tech Stack:** バニラJS、navigator.clipboard API、Mifflin-St Jeor BMR計算式

---

## 目次

| Step | ファイル | 内容 |
|------|---------|------|
| Phase35-Step1 | database.py | get_stats() に sodium 追加 |
| Phase35-Step2 | history.html | pageState 拡張・fetchSettings() 追加 |
| Phase35-Step3 | history.html | BMR計算ユーティリティ関数追加 |
| Phase35-Step4 | history.html | CSS・コピーボタンUI・renderDay組み込み |
| Phase35-Step5 | history.html | buildHistoryTsv() 実装 |
| Phase35-Step6 | history.html | buildHistoryJson() 実装 |
| Phase35-Step7 | history.html | コピーメニューイベントハンドラ |
| Phase35-Step8 | stats.html | pageState拡張・設定ロード拡張・CSS・コピーボタンUI |
| Phase35-Step9 | stats.html | BMRユーティリティ・buildWeightMap・buildStatsTsv() |
| Phase35-Step10 | stats.html | buildStatsJson()・イベントハンドラ |

---

## 設計仕様書

`仕様書/2026-04-28-copy-button-design.md` を参照。

---

## Step 1: database.py — get_stats() に sodium 追加

**Files:**
- Modify: `database.py`（`get_stats()` 関数、1370行付近）

`get_stats()` の SQL クエリに `SUM(sodium) AS s` を追加し、戻り値に `sodium` 配列を含める。

- [ ] **1-1: SQL クエリを修正する**

`database.py` の `get_stats()` 内、`cal_rows` クエリを以下のように変更:

```python
# 変更前
        cal_rows = conn.execute(
            """
            SELECT meal_date,
                   SUM(calories) AS cal, SUM(protein) AS p,
                   SUM(fat) AS f, SUM(carbs) AS c
            FROM meals WHERE meal_date >= ? AND meal_date <= ?
            GROUP BY meal_date
            """,
            (since, until),
        ).fetchall()

# 変更後
        cal_rows = conn.execute(
            """
            SELECT meal_date,
                   SUM(calories) AS cal, SUM(protein) AS p,
                   SUM(fat) AS f, SUM(carbs) AS c,
                   SUM(sodium) AS s
            FROM meals WHERE meal_date >= ? AND meal_date <= ?
            GROUP BY meal_date
            """,
            (since, until),
        ).fetchall()
```

- [ ] **1-2: 集計ループに sodium を追加する**

`calories`, `protein`, `fat`, `carbs` リストを宣言している箇所（1413行付近）のすぐ後に追加:

```python
# 変更前（1413行付近）
    calories, protein, fat, carbs = [], [], [], []

# 変更後
    calories, protein, fat, carbs, sodium_daily = [], [], [], [], []
```

ループ内（`calories.append(...)` の直後）に追加:

```python
        sodium_daily.append(round(c["s"], 2) if c and c["s"] is not None else None)
```

- [ ] **1-3: 戻り値に sodium を追加する**

`return` dict に追加（`"carbs": carbs,` の直後）:

```python
        "sodium": sodium_daily,
```

- [ ] **1-4: サーバーを起動して動作確認する**

```bash
uv run run.py
```

ブラウザで `http://localhost:8000/api/stats?days=7` にアクセスし、レスポンスに `"sodium": [...]` 配列が含まれることを確認。

- [ ] **1-5: コミットする**

```bash
git add database.py
git commit -m "Phase35-Step1	database.py	get_stats() に sodium 追加"
```

---

## Step 2: history.html — pageState 拡張・fetchSettings() 追加

**Files:**
- Modify: `static/history.html`

BMR計算に必要な設定値（身長・性別・生年月日・歩数目標）をページロード時に1回だけ取得して `pageState` に保存する。

- [ ] **2-1: pageState に BMR用フィールドを追加する**

`static/history.html` の `pageState` 宣言（392行付近）を以下のように拡張:

```javascript
// 変更前
    const pageState = {
      calorieGoal: 1500,
      allDays: [],
      currentPage: 0,
      currentPeriod: { days: 7, start: null, end: null },
      savedScrollY: 0,
      vitalsData: null,
      activePopup: null,
      lastActivityDate: null,
    };

// 変更後
    const pageState = {
      calorieGoal: 1500,
      stepsGoal: 8000,
      heightCm: null,
      gender: null,
      birthdate: null,
      activeCopyMenu: null,
      allDays: [],
      currentPage: 0,
      currentPeriod: { days: 7, start: null, end: null },
      savedScrollY: 0,
      vitalsData: null,
      activePopup: null,
      lastActivityDate: null,
    };
```

- [ ] **2-2: fetchSettings() 関数を追加する**

`fetchLastActivityDate()` 関数（442行付近）の直前に追加:

```javascript
    async function fetchSettings() {
      try {
        var res = await fetch('/api/settings');
        if (res.status === 401) { location.href = '/'; return; }
        if (!res.ok) return;
        var sett = await res.json();
        pageState.stepsGoal   = parseInt(sett.daily_steps_goal) || 8000;
        pageState.heightCm    = parseFloat(sett.user_height_cm) || null;
        pageState.gender      = sett.user_gender || null;
        pageState.birthdate   = sett.user_birthdate || null;
      } catch(e) {
        console.warn('[fetchSettings] 設定ロード失敗（BMR計算無効）', e);
      }
    }
```

- [ ] **2-3: 初期化順序を変更する**

ファイル末尾の初期化呼び出し（1552行付近）を変更:

```javascript
// 変更前
    fetchLastActivityDate().then(function() { load(7, null, null); });

// 変更後
    Promise.all([fetchLastActivityDate(), fetchSettings()])
      .then(function() { load(7, null, null); });
```

- [ ] **2-4: コミットする**

```bash
git add static/history.html
git commit -m "Phase35-Step2	history.html	pageState 拡張・fetchSettings() 追加"
```

---

## Step 3: history.html — BMR計算ユーティリティ関数

**Files:**
- Modify: `static/history.html`

BMR計算・年齢計算・体重フォールバック取得の3関数を追加する。

- [ ] **3-1: 3つのユーティリティ関数を追加する**

`buildMealBreakdown()` 関数（403行付近）の直前に追加:

```javascript
    function calcAgeAt(birthdate, dateStr) {
      if (!birthdate || !dateStr) return null;
      var b = new Date(birthdate + 'T00:00:00');
      var d = new Date(dateStr  + 'T00:00:00');
      var age = d.getFullYear() - b.getFullYear();
      var m = d.getMonth() - b.getMonth();
      if (m < 0 || (m === 0 && d.getDate() < b.getDate())) age--;
      return age < 0 ? null : age;
    }

    function calcBmrForDay(weightKg, dateStr) {
      if (!weightKg || !pageState.heightCm || !pageState.gender || !pageState.birthdate) return null;
      var age = calcAgeAt(pageState.birthdate, dateStr);
      if (age === null) return null;
      var base = 10 * weightKg + 6.25 * pageState.heightCm - 5 * age;
      return Math.round(pageState.gender === 'male' ? base + 5 : base - 161);
    }

    function getWeightForBmr(day) {
      if (day.weight && day.weight.morning) return day.weight.morning.weight_kg;
      if (day.weight && day.weight.evening) return day.weight.evening.weight_kg;
      var best = null, bestDiff = Infinity;
      pageState.allDays.forEach(function(d) {
        var w = d.weight && d.weight.morning ? d.weight.morning.weight_kg
               : d.weight && d.weight.evening ? d.weight.evening.weight_kg : null;
        if (w !== null) {
          var diff = Math.abs(new Date(d.date) - new Date(day.date));
          if (diff < bestDiff) { bestDiff = diff; best = w; }
        }
      });
      return best;
    }
```

- [ ] **3-2: コミットする**

```bash
git add static/history.html
git commit -m "Phase35-Step3	history.html	BMR計算ユーティリティ関数追加"
```

---

## Step 4: history.html — CSS・コピーボタンUI・renderDay 組み込み

**Files:**
- Modify: `static/history.html`

コピーボタンの CSS を追加し、`renderDay()` の `.day-header` 内にコピーボタン HTML を組み込む。

- [ ] **4-1: CSS を追加する**

`<style>` ブロック内の末尾（`@media (max-width: 480px)` ルール付近）に追加:

```css
    .copy-menu-wrap { position: relative; display: inline-flex; align-items: center; }
    .copy-btn {
      background: var(--bg); border: 1px solid var(--border);
      cursor: pointer; font-size: 11px; color: var(--text-secondary);
      padding: 3px 8px; border-radius: 4px; white-space: nowrap;
      font-family: inherit; margin-left: 4px;
    }
    .copy-btn:hover { background: var(--card-bg); color: var(--text-primary); }
    .copy-popup {
      position: absolute; right: 0; top: calc(100% + 4px); z-index: 102;
      background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
      box-shadow: 0 4px 16px rgba(0,0,0,0.15); overflow: hidden; white-space: nowrap; min-width: 180px;
    }
    .copy-popup button {
      display: block; width: 100%; padding: 9px 16px; background: none; border: none;
      border-bottom: 1px solid var(--border); font-size: 13px; cursor: pointer;
      text-align: left; font-family: inherit; color: var(--text-primary);
    }
    .copy-popup button:last-child { border-bottom: none; }
    .copy-popup button:hover { background: var(--card-bg); }
```

- [ ] **4-2: renderDay() にコピーボタンを追加する**

`renderDay()` 関数（600行付近）内、`infoBtn` / `breakdownHtml` 変数の定義直後に追加:

```javascript
      // 変更前（706行付近）
      return `
        <div class="day-section">
          <div class="day-header">
            <span class="day-date">${label}</span>
            <span class="day-totals">${t.calories} kcal / P:${t.protein}g F:${t.fat}g C:${t.carbs}g 塩:${t.sodium}g${infoBtn}</span>
            ${breakdownHtml}
          </div>

      // 変更後
      var copyBtnHtml = '<span class="copy-menu-wrap">'
        + '<button class="copy-btn" data-date="' + day.date + '" aria-label="' + label + ' のデータをコピー">コピー▾</button>'
        + '<div class="copy-popup" style="display:none">'
        + '<button data-copy="tsv" data-date="' + day.date + '">📊 タブ区切りでコピー</button>'
        + '<button data-copy="json" data-date="' + day.date + '">🤖 JSONでコピー</button>'
        + '</div></span>';
      return `
        <div class="day-section">
          <div class="day-header">
            <span class="day-date">${label}</span>
            <span class="day-totals">${t.calories} kcal / P:${t.protein}g F:${t.fat}g C:${t.carbs}g 塩:${t.sodium}g${infoBtn}${copyBtnHtml}</span>
            ${breakdownHtml}
          </div>
```

- [ ] **4-3: ブラウザで表示確認する**

`uv run run.py` でサーバー起動し、履歴ページを開いて各日付行の右端に「コピー▾」ボタンが表示されることを確認（まだクリックしても何も起きない）。

- [ ] **4-4: コミットする**

```bash
git add static/history.html
git commit -m "Phase35-Step4	history.html	CSS・コピーボタンUI・renderDay組み込み"
```

---

## Step 5: history.html — buildHistoryTsv() 実装

**Files:**
- Modify: `static/history.html`

1日分のデータをタブ区切り縦展開形式に変換する `buildHistoryTsv()` を実装する。

- [ ] **5-1: 定数とヘルパー関数を追加する**

`buildMealBreakdown()` の直前に追加:

```javascript
    const MEAL_TYPE_JA = { breakfast: '朝', lunch: '昼', dinner: '夜', snack: '間食', late_night: '夜食' };
    const TSV_HEADER = ['日付','種別','区分','内容','時刻','kcal','P(g)','F(g)','C(g)','Na(g)','数値1','数値2'].join('\t');

    function tsvRow(cells) {
      return cells.map(function(v) { return v == null ? '' : String(v); }).join('\t');
    }
```

- [ ] **5-2: buildHistoryTsv() を追加する**

`tsvRow()` の直後に追加:

```javascript
    function buildHistoryTsv(day) {
      var rows = [TSV_HEADER];
      var d = day.date;
      var t = day.totals;
      var mealOrder = ['breakfast', 'lunch', 'dinner', 'snack', 'late_night'];

      mealOrder.forEach(function(type) {
        day.meals.filter(function(m) { return m.meal_type === type; })
          .forEach(function(m) {
            rows.push(tsvRow([
              d, '食事', MEAL_TYPE_JA[type] || type, m.description,
              m.meal_time ? m.meal_time.slice(0, 5) : '',
              m.calories, m.protein, m.fat, m.carbs, m.sodium, '', ''
            ]));
          });
      });

      var weight  = getWeightForBmr(day);
      var bmr     = calcBmrForDay(weight, d);
      var exCal   = day.exercise.reduce(function(s, e) { return s + (e.calories_burned || 0); }, 0);
      var totalExp = bmr !== null ? bmr + exCal : null;
      var balance  = totalExp !== null && t.calories != null ? t.calories - totalExp : null;
      var balStr   = balance !== null ? (balance >= 0 ? '+' : '') + balance : '';
      var noteStr  = 'ゴール:' + pageState.calorieGoal
        + (bmr      !== null ? ' / 基礎代謝:' + bmr      : '')
        + (totalExp !== null ? ' / 総消費:'   + totalExp  : '')
        + (balStr            ? ' / 収支:'     + balStr    : '');
      rows.push(tsvRow([d, '食事合計', '', noteStr, '', t.calories, t.protein, t.fat, t.carbs, t.sodium, '', '']));

      ['morning', 'evening'].forEach(function(tod) {
        var label = tod === 'morning' ? '朝' : '夜';
        if (day.weight && day.weight[tod]) {
          rows.push(tsvRow([d, '体重', label, '', '', '', '', '', '', '', day.weight[tod].weight_kg, '']));
        }
      });

      if (day.steps !== null) {
        rows.push(tsvRow([d, '歩数', '', 'ゴール:' + pageState.stepsGoal, '', '', '', '', '', '', day.steps, '']));
      }

      day.exercise.forEach(function(ex) {
        rows.push(tsvRow([d, '運動', '', ex.description || '', '', ex.calories_burned || '', '', '', '', '', '', '']));
      });

      ['morning', 'evening'].forEach(function(tod) {
        var label = tod === 'morning' ? '朝' : '夜';
        if (day.blood_pressure && day.blood_pressure[tod]) {
          var bp = day.blood_pressure[tod];
          rows.push(tsvRow([d, '血圧', label, '', '', '', '', '', '', '', bp.systolic, bp.diastolic]));
        }
      });

      if (day.body_fat !== null && day.body_fat !== undefined) {
        rows.push(tsvRow([d, '体脂肪', '', '', '', '', '', '', '', '', day.body_fat, '']));
      }

      return rows.join('\n');
    }
```

- [ ] **5-3: コミットする**

```bash
git add static/history.html
git commit -m "Phase35-Step5	history.html	buildHistoryTsv() 実装"
```

---

## Step 6: history.html — buildHistoryJson() 実装

**Files:**
- Modify: `static/history.html`

1日分のデータをAI投入用JSON形式に変換する `buildHistoryJson()` を実装する。

- [ ] **6-1: buildHistoryJson() を追加する**

`buildHistoryTsv()` の直後に追加:

```javascript
    function buildHistoryJson(day) {
      var d = day.date;
      var t = day.totals;
      var dateObj  = new Date(d + 'T00:00:00');
      var weekdays = ['日','月','火','水','木','金','土'];

      var weight   = getWeightForBmr(day);
      var bmr      = calcBmrForDay(weight, d);
      var exCal    = day.exercise.reduce(function(s, e) { return s + (e.calories_burned || 0); }, 0);
      var totalExp = bmr !== null ? bmr + exCal : null;
      var balance  = totalExp !== null && t.calories != null ? t.calories - totalExp : null;

      var mealOrder = ['breakfast', 'lunch', 'dinner', 'snack', 'late_night'];
      var meals = {};
      mealOrder.forEach(function(type) {
        var items = day.meals
          .filter(function(m) { return m.meal_type === type; })
          .map(function(m) {
            return {
              description: m.description,
              meal_time:   m.meal_time ? m.meal_time.slice(0, 5) : null,
              calories:    m.calories,
              protein:     m.protein,
              fat:         m.fat,
              carbs:       m.carbs,
              sodium:      m.sodium
            };
          });
        if (items.length) meals[type] = items;
      });

      function bpObj(tod) {
        var bp = day.blood_pressure && day.blood_pressure[tod];
        return bp ? { systolic: bp.systolic, diastolic: bp.diastolic } : null;
      }

      return JSON.stringify({
        meta: {
          date:         d,
          weekday:      weekdays[dateObj.getDay()],
          generated_at: new Date().toISOString().slice(0, 19)
        },
        profile: {
          calories_goal: pageState.calorieGoal,
          steps_goal:    pageState.stepsGoal
        },
        summary: {
          calories_total:    t.calories,
          calories_diff:     t.calories != null ? t.calories - pageState.calorieGoal : null,
          protein_g:         t.protein,
          fat_g:             t.fat,
          carbs_g:           t.carbs,
          sodium_g:          t.sodium,
          weight_morning_kg: day.weight && day.weight.morning ? day.weight.morning.weight_kg : null,
          weight_evening_kg: day.weight && day.weight.evening ? day.weight.evening.weight_kg : null,
          steps:             day.steps,
          steps_diff:        day.steps != null ? day.steps - pageState.stepsGoal : null,
          exercise_calories: exCal || null,
          bmr_kcal:          bmr,
          total_expenditure: totalExp,
          calorie_balance:   balance,
          body_fat_pct:      day.body_fat !== undefined ? day.body_fat : null,
          bp_morning:        bpObj('morning'),
          bp_evening:        bpObj('evening')
        },
        meals:    meals,
        exercise: day.exercise.map(function(ex) {
          return { description: ex.description, calories_burned: ex.calories_burned };
        })
      }, null, 2);
    }
```

- [ ] **6-2: コミットする**

```bash
git add static/history.html
git commit -m "Phase35-Step6	history.html	buildHistoryJson() 実装"
```

---

## Step 7: history.html — コピーメニューイベントハンドラ

**Files:**
- Modify: `static/history.html`

コピーボタンのクリック開閉・クリップボード書き込み・フィードバック表示のイベントハンドラを追加する。

- [ ] **7-1: closeCopyMenu() を追加する**

既存の `showPopup()` / `hidePopup()` 関数（538行付近）の直後に追加:

```javascript
    function closeCopyMenu() {
      if (pageState.activeCopyMenu) {
        pageState.activeCopyMenu.style.display = 'none';
        pageState.activeCopyMenu = null;
      }
    }
```

- [ ] **7-2: content のクリックイベントにコピーハンドラを追加する**

既存のクリックハンドラ（`.info-btn` を処理している `content.addEventListener('click', ...)` 内、`e.stopPropagation()` の後）に追記する。

既存コード（558行付近）:

```javascript
        content.addEventListener('click', function(e) {
          var btn = e.target.closest('.info-btn');
          if (!btn) return;
          e.stopPropagation();
          var popup = btn.closest('.day-header').querySelector('.meal-breakdown-popup');
          if (!popup) return;
          if (pageState.activePopup === popup) { hidePopup(); } else { showPopup(popup); }
        });
```

この addEventListener の末尾（`});` の直前）に以下を追加するのではなく、**新しい addEventListener として** `content.addEventListener('click', ...)` の直後に追加する:

```javascript
        content.addEventListener('click', function(e) {
          // コピーボタン開閉
          var copyBtn = e.target.closest('.copy-btn');
          if (copyBtn) {
            e.stopPropagation();
            var popup = copyBtn.parentElement.querySelector('.copy-popup');
            if (!popup) return;
            if (pageState.activeCopyMenu === popup) {
              closeCopyMenu();
            } else {
              closeCopyMenu();
              hidePopup();
              popup.style.display = '';
              pageState.activeCopyMenu = popup;
            }
            return;
          }
          // コピーメニュー項目クリック
          var copyItem = e.target.closest('[data-copy]');
          if (copyItem) {
            e.stopPropagation();
            var format = copyItem.dataset.copy;
            var date   = copyItem.dataset.date;
            var dayData = pageState.allDays.find(function(d) { return d.date === date; });
            if (!dayData) { closeCopyMenu(); return; }
            var text = format === 'tsv' ? buildHistoryTsv(dayData) : buildHistoryJson(dayData);
            var triggerBtn = copyItem.closest('.copy-menu-wrap').querySelector('.copy-btn');
            closeCopyMenu();
            navigator.clipboard.writeText(text).then(function() {
              triggerBtn.textContent = '✓ コピー済';
              setTimeout(function() { triggerBtn.textContent = 'コピー▾'; }, 1500);
            }).catch(function() {
              triggerBtn.textContent = '✗ 失敗';
              setTimeout(function() { triggerBtn.textContent = 'コピー▾'; }, 1500);
            });
          }
        });
```

- [ ] **7-3: document のクリックで閉じる処理を追加する**

既存の `document.addEventListener('click', ...)` 内（`hidePopup()` を呼んでいる処理、567行付近）に `closeCopyMenu()` の呼び出しを追加:

```javascript
// 変更前
        document.addEventListener('click', function(e) {
          if (!pageState.activePopup) return;
          if (!pageState.activePopup.contains(e.target)) hidePopup();
        });

// 変更後
        document.addEventListener('click', function(e) {
          if (pageState.activeCopyMenu && !pageState.activeCopyMenu.contains(e.target)) {
            closeCopyMenu();
          }
          if (!pageState.activePopup) return;
          if (!pageState.activePopup.contains(e.target)) hidePopup();
        });
```

- [ ] **7-4: 動作確認する**

サーバー起動し、以下をブラウザで確認:
1. 「コピー▾」クリック → サブメニューが表示される
2. 外側クリック → サブメニューが閉じる
3. 「📊 タブ区切りでコピー」クリック → ボタンが「✓ コピー済」に変わり、スプレッドシートに貼り付けると列構造が正しい
4. 「🤖 JSONでコピー」クリック → JSON が正しくコピーされる（`JSON.parse()` でエラーなし）

- [ ] **7-5: コミットする**

```bash
git add static/history.html
git commit -m "Phase35-Step7	history.html	コピーメニューイベントハンドラ"
```

---

## Step 8: stats.html — pageState 拡張・設定ロード拡張・CSS・コピーボタン UI

**Files:**
- Modify: `static/stats.html`

- [ ] **8-1: pageState に BMR用フィールドを追加する**

`static/stats.html` の `pageState` 宣言（185行付近）を拡張:

```javascript
// 変更前
    const pageState = {
      dateRange: { start: null, end: null },
      dates: [],
      currentDays: 7,
      // ...

// 変更後（既存フィールドの前に追加）
    const pageState = {
      dateRange: { start: null, end: null },
      dates: [],
      currentDays: 7,
      statsData: null,
      stepsGoal: 8000,
      heightCm: null,
      gender: null,
      birthdate: null,
      activeCopyMenu: null,
      // ... 既存の残りのフィールドはそのまま
```

- [ ] **8-2: 設定ロード時に BMR設定を保存する**

`const sett = await settRes.json();` の直後（924行付近）、`pageState.widgetConfig = ...` の前に追加:

```javascript
          pageState.stepsGoal  = parseInt(sett.daily_steps_goal) || 8000;
          pageState.heightCm   = parseFloat(sett.user_height_cm) || null;
          pageState.gender     = sett.user_gender || null;
          pageState.birthdate  = sett.user_birthdate || null;
```

- [ ] **8-3: render() で statsData を保存する**

`render(data)` 関数（974行付近）の先頭に追加:

```javascript
    function render(data) {
      pageState.statsData = data;   // ← 追加
      const labels = data.dates.map(shortDate);
      // ...
```

- [ ] **8-4: CSS を追加する**

`<style>` ブロックの末尾に追加（history.html と同じ CSS）:

```css
    .copy-menu-wrap { position: relative; display: inline-flex; align-items: center; }
    .copy-btn {
      background: var(--bg); border: 1px solid var(--border);
      cursor: pointer; font-size: 11px; color: var(--text-secondary);
      padding: 3px 8px; border-radius: 4px; white-space: nowrap;
      font-family: inherit;
    }
    .copy-btn:hover { background: var(--card-bg); color: var(--text-primary); }
    .copy-popup {
      position: absolute; right: 0; top: calc(100% + 4px); z-index: 102;
      background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
      box-shadow: 0 4px 16px rgba(0,0,0,0.15); overflow: hidden; white-space: nowrap; min-width: 180px;
    }
    .copy-popup button {
      display: block; width: 100%; padding: 9px 16px; background: none; border: none;
      border-bottom: 1px solid var(--border); font-size: 13px; cursor: pointer;
      text-align: left; font-family: inherit; color: var(--text-primary);
    }
    .copy-popup button:last-child { border-bottom: none; }
    .copy-popup button:hover { background: var(--card-bg); }
```

- [ ] **8-5: 期間バーにコピーボタンを追加する**

`static/stats.html` の period-bar HTML（115行付近）の `margin-left:auto` の div を変更:

```html
<!-- 変更前 -->
  <div style="display:flex;gap:6px;margin-left:auto">
    <button class="period-shift-btn" id="prevBtn" onclick="shiftPeriod(-1)" title="前の期間（最大1年前まで）">◀ 前</button>
    <button class="period-shift-btn" id="nextBtn" onclick="shiftPeriod(1)" disabled>次 ▶</button>
  </div>

<!-- 変更後 -->
  <div style="display:flex;gap:6px;margin-left:auto;align-items:center">
    <button class="period-shift-btn" id="prevBtn" onclick="shiftPeriod(-1)" title="前の期間（最大1年前まで）">◀ 前</button>
    <button class="period-shift-btn" id="nextBtn" onclick="shiftPeriod(1)" disabled>次 ▶</button>
    <span class="copy-menu-wrap">
      <button class="copy-btn" id="statsCopyBtn" aria-label="集計データをコピー">コピー▾</button>
      <div class="copy-popup" id="statsCopyPopup" style="display:none">
        <button id="statsCopyTsv">📊 タブ区切りでコピー</button>
        <button id="statsCopyJson">🤖 JSONでコピー</button>
      </div>
    </span>
  </div>
```

- [ ] **8-6: ブラウザで表示確認する**

サーバー起動し、集計ページを開いて期間セレクタ右端に「コピー▾」ボタンが表示されることを確認。

- [ ] **8-7: コミットする**

```bash
git add static/stats.html
git commit -m "Phase35-Step8	stats.html	pageState拡張・設定ロード拡張・CSS・コピーボタンUI"
```

---

## Step 9: stats.html — BMRユーティリティ・buildWeightMap・buildStatsTsv()

**Files:**
- Modify: `static/stats.html`

- [ ] **9-1: BMRユーティリティ関数を追加する**

スクリプトブロックの先頭付近（`const pageState = ...` の直前）に追加:

```javascript
    function calcAgeAt(birthdate, dateStr) {
      if (!birthdate || !dateStr) return null;
      var b = new Date(birthdate + 'T00:00:00');
      var d = new Date(dateStr  + 'T00:00:00');
      var age = d.getFullYear() - b.getFullYear();
      var m = d.getMonth() - b.getMonth();
      if (m < 0 || (m === 0 && d.getDate() < b.getDate())) age--;
      return age < 0 ? null : age;
    }

    function calcBmrForDay(weightKg, dateStr) {
      if (!weightKg || !pageState.heightCm || !pageState.gender || !pageState.birthdate) return null;
      var age = calcAgeAt(pageState.birthdate, dateStr);
      if (age === null) return null;
      var base = 10 * weightKg + 6.25 * pageState.heightCm - 5 * age;
      return Math.round(pageState.gender === 'male' ? base + 5 : base - 161);
    }

    function buildWeightMap(data) {
      var known = {};
      data.dates.forEach(function(date, i) {
        var wm = data.weights_morning[i];
        var we = data.weights_evening[i];
        if (wm !== null && wm !== undefined) known[date] = wm;
        else if (we !== null && we !== undefined) known[date] = we;
      });
      var result = {};
      data.dates.forEach(function(date) {
        if (known[date] !== undefined) { result[date] = known[date]; return; }
        var best = null, bestDiff = Infinity;
        Object.keys(known).forEach(function(d) {
          var diff = Math.abs(new Date(d) - new Date(date));
          if (diff < bestDiff) { bestDiff = diff; best = known[d]; }
        });
        result[date] = best;
      });
      return result;
    }
```

- [ ] **9-2: buildStatsTsv() を追加する**

`buildWeightMap` の直後に追加:

```javascript
    function buildStatsTsv(data) {
      var header = [
        '日付','摂取kcal','目標kcal','差分kcal',
        'P(g)','F(g)','C(g)',
        '体重朝(kg)','体重夜(kg)',
        '歩数','目標歩数','歩数差分',
        '運動消費kcal','基礎代謝kcal','総消費kcal','収支kcal',
        '収縮期朝','拡張期朝','収縮期夜','拡張期夜',
        '体脂肪(%)'
      ].join('\t');

      var calGoal   = data.calories_goal;
      var stepsGoal = pageState.stepsGoal;
      var weightMap = buildWeightMap(data);
      var rows = [header];

      data.dates.forEach(function(date, i) {
        var cal   = data.calories[i];
        var wm    = data.weights_morning[i];
        var we    = data.weights_evening[i];
        var steps = data.steps[i];
        var exCal = data.exercise_calories[i];
        var bf    = data.body_fat[i];
        var bpMs  = data.blood_pressure.morning_systolic[i];
        var bpMd  = data.blood_pressure.morning_diastolic[i];
        var bpEs  = data.blood_pressure.evening_systolic[i];
        var bpEd  = data.blood_pressure.evening_diastolic[i];

        var bmr      = calcBmrForDay(weightMap[date], date);
        var totalExp = bmr !== null ? bmr + (exCal || 0) : null;
        var balance  = totalExp !== null && cal !== null ? cal - totalExp : null;

        rows.push([
          date,
          cal    ?? '', calGoal, cal    !== null ? cal    - calGoal   : '',
          data.protein[i] ?? '', data.fat[i] ?? '', data.carbs[i] ?? '',
          wm ?? '', we ?? '',
          steps  ?? '', stepsGoal, steps  !== null ? steps  - stepsGoal : '',
          exCal  ?? '', bmr ?? '', totalExp ?? '', balance ?? '',
          bpMs   ?? '', bpMd ?? '', bpEs ?? '', bpEd ?? '',
          bf     ?? ''
        ].map(function(v) { return v == null ? '' : String(v); }).join('\t'));
      });

      return rows.join('\n');
    }
```

- [ ] **9-3: コミットする**

```bash
git add static/stats.html
git commit -m "Phase35-Step9	stats.html	BMRユーティリティ・buildWeightMap・buildStatsTsv()"
```

---

## Step 10: stats.html — buildStatsJson()・イベントハンドラ

**Files:**
- Modify: `static/stats.html`

- [ ] **10-1: buildStatsJson() を追加する**

`buildStatsTsv()` の直後に追加:

```javascript
    function buildStatsJson(data) {
      var calGoal   = data.calories_goal;
      var stepsGoal = pageState.stepsGoal;
      var from      = pageState.dateRange.start || data.dates[0];
      var to        = pageState.dateRange.end   || data.dates[data.dates.length - 1];
      var weightMap = buildWeightMap(data);
      var weekdays  = ['日','月','火','水','木','金','土'];

      function roundN(v, n) {
        if (v == null) return null;
        var f = Math.pow(10, n);
        return Math.round(v * f) / f;
      }
      function avgArr(arr) {
        var valid = arr.filter(function(v) { return v !== null && v !== undefined; });
        if (!valid.length) return null;
        return roundN(valid.reduce(function(s, v) { return s + v; }, 0) / valid.length, 1);
      }

      var daily = data.dates.map(function(date, i) {
        var cal   = data.calories[i];
        var wm    = data.weights_morning[i];
        var we    = data.weights_evening[i];
        var steps = data.steps[i];
        var exCal = data.exercise_calories[i];
        var bf    = data.body_fat[i];
        var sodium = data.sodium ? data.sodium[i] : null;
        var bpMs  = data.blood_pressure.morning_systolic[i];
        var bpMd  = data.blood_pressure.morning_diastolic[i];
        var bpEs  = data.blood_pressure.evening_systolic[i];
        var bpEd  = data.blood_pressure.evening_diastolic[i];

        var bmr      = calcBmrForDay(weightMap[date], date);
        var totalExp = bmr !== null ? bmr + (exCal || 0) : null;
        var balance  = totalExp !== null && cal !== null ? cal - totalExp : null;

        var dateObj = new Date(date + 'T00:00:00');
        return {
          date:              date,
          weekday:           weekdays[dateObj.getDay()],
          calories:          cal,
          calories_diff:     cal !== null ? cal - calGoal : null,
          protein:           data.protein[i],
          fat:               data.fat[i],
          carbs:             data.carbs[i],
          sodium:            sodium,
          weight_morning:    wm ?? null,
          weight_evening:    we ?? null,
          steps:             steps ?? null,
          steps_diff:        steps !== null ? steps - stepsGoal : null,
          exercise_calories: exCal ?? null,
          bmr_kcal:          bmr,
          total_expenditure: totalExp,
          calorie_balance:   balance,
          bp_morning:        bpMs !== null && bpMs !== undefined ? { systolic: bpMs, diastolic: bpMd } : null,
          bp_evening:        bpEs !== null && bpEs !== undefined ? { systolic: bpEs, diastolic: bpEd } : null,
          body_fat_pct:      bf ?? null
        };
      });

      // summary 計算
      function validArr(key) { return daily.filter(function(d) { return d[key] !== null; }).map(function(d) { return d[key]; }); }
      var validCals    = validArr('calories');
      var validBal     = validArr('calorie_balance');
      var validBmr     = validArr('bmr_kcal');
      var validExp     = validArr('total_expenditure');
      var validWm      = validArr('weight_morning');
      var validSteps   = validArr('steps');
      var validExCal   = validArr('exercise_calories');
      var validSodium  = validArr('sodium');

      var wDates   = daily.filter(function(d) { return d.weight_morning !== null; });
      var wStart   = wDates.length ? wDates[0].weight_morning : null;
      var wEnd     = wDates.length ? wDates[wDates.length - 1].weight_morning : null;
      var wChange  = wStart !== null && wEnd !== null ? roundN(wEnd - wStart, 1) : null;

      var validBpMs = daily.filter(function(d) { return d.bp_morning !== null; }).map(function(d) { return d.bp_morning.systolic; });
      var validBpMd = daily.filter(function(d) { return d.bp_morning !== null; }).map(function(d) { return d.bp_morning.diastolic; });
      var validBpEs = daily.filter(function(d) { return d.bp_evening !== null; }).map(function(d) { return d.bp_evening.systolic; });
      var validBpEd = daily.filter(function(d) { return d.bp_evening !== null; }).map(function(d) { return d.bp_evening.diastolic; });

      return JSON.stringify({
        meta: {
          from: from, to: to,
          days: data.dates.length,
          generated_at: new Date().toISOString().slice(0, 19)
        },
        profile: {
          calories_goal: calGoal,
          steps_goal:    stepsGoal
        },
        summary: {
          calories_avg:             roundN(avgArr(validCals), 0),
          days_over_calories_goal:  validCals.filter(function(v) { return v > calGoal; }).length,
          protein_avg:              avgArr(validArr('protein')),
          fat_avg:                  avgArr(validArr('fat')),
          carbs_avg:                avgArr(validArr('carbs')),
          sodium_avg:               roundN(avgArr(validSodium), 2),
          weight_morning_avg:       avgArr(validWm),
          weight_morning_start:     wStart,
          weight_morning_end:       wEnd,
          weight_morning_change:    wChange,
          steps_avg:                roundN(avgArr(validSteps), 0),
          days_over_steps_goal:     validSteps.filter(function(v) { return v >= stepsGoal; }).length,
          exercise_calories_avg:    roundN(avgArr(validExCal), 0),
          bmr_kcal_avg:             roundN(avgArr(validBmr), 0),
          total_expenditure_avg:    roundN(avgArr(validExp), 0),
          calorie_balance_avg:      roundN(avgArr(validBal), 0),
          days_positive_balance:    validBal.filter(function(v) { return v > 0; }).length,
          days_negative_balance:    validBal.filter(function(v) { return v < 0; }).length,
          bp_morning_avg:           validBpMs.length ? { systolic: Math.round(avgArr(validBpMs)), diastolic: Math.round(avgArr(validBpMd)) } : null,
          bp_evening_avg:           validBpEs.length ? { systolic: Math.round(avgArr(validBpEs)), diastolic: Math.round(avgArr(validBpEd)) } : null
        },
        daily: daily
      }, null, 2);
    }
```

- [ ] **10-2: イベントハンドラを追加する**

`buildStatsJson()` の直後に追加:

```javascript
    function closeCopyMenu() {
      if (pageState.activeCopyMenu) {
        pageState.activeCopyMenu.style.display = 'none';
        pageState.activeCopyMenu = null;
      }
    }

    function initStatsCopyHandlers() {
      var copyBtn  = document.getElementById('statsCopyBtn');
      var popup    = document.getElementById('statsCopyPopup');
      var tsvBtn   = document.getElementById('statsCopyTsv');
      var jsonBtn  = document.getElementById('statsCopyJson');

      copyBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        if (pageState.activeCopyMenu === popup) {
          closeCopyMenu();
        } else {
          closeCopyMenu();
          popup.style.display = '';
          pageState.activeCopyMenu = popup;
        }
      });

      function doCopy(format) {
        if (!pageState.statsData) { closeCopyMenu(); return; }
        var text = format === 'tsv'
          ? buildStatsTsv(pageState.statsData)
          : buildStatsJson(pageState.statsData);
        closeCopyMenu();
        navigator.clipboard.writeText(text).then(function() {
          copyBtn.textContent = '✓ コピー済';
          setTimeout(function() { copyBtn.textContent = 'コピー▾'; }, 1500);
        }).catch(function() {
          copyBtn.textContent = '✗ 失敗';
          setTimeout(function() { copyBtn.textContent = 'コピー▾'; }, 1500);
        });
      }

      tsvBtn.addEventListener('click',  function(e) { e.stopPropagation(); doCopy('tsv');  });
      jsonBtn.addEventListener('click', function(e) { e.stopPropagation(); doCopy('json'); });

      document.addEventListener('click', function(e) {
        if (pageState.activeCopyMenu && !pageState.activeCopyMenu.contains(e.target)) {
          closeCopyMenu();
        }
      });
    }
```

- [ ] **10-3: initStatsCopyHandlers() を初期化コードから呼び出す**

stats.html の `document.addEventListener('DOMContentLoaded', ...)` または初期化呼び出し部分（ページ末尾付近）に追加:

```javascript
    initStatsCopyHandlers();
```

具体的には、stats.html 末尾の `initNav('stats');` などの初期化呼び出しと同じ場所に追記する。

- [ ] **10-4: 動作確認する**

サーバー起動し、以下をブラウザで確認:
1. 集計ページの「コピー▾」クリック → サブメニュー表示・外側クリックで閉じる
2. 「📊 タブ区切りでコピー」→ スプレッドシート貼り付けで列構造確認
3. 「🤖 JSONでコピー」→ JSON パース確認・`bp_morning_avg` がオブジェクト形式
4. 期間を7日→30日に変更後にコピー → 30日分のデータが含まれること
5. BMR設定が未登録の状態で確認 → `bmr_kcal: null` で出力されエラーなし

- [ ] **10-5: コミットする**

```bash
git add static/stats.html
git commit -m "Phase35-Step10	stats.html	buildStatsJson()・イベントハンドラ"
```

---

## 完了チェックリスト

- [x] Step 1〜10 の全コミット完了
- [ ] タブ区切りをGoogleスプレッドシートに貼り付けて列構造確認
- [ ] JSONをAI（ChatGPT等）に貼り付けて解釈確認
- [ ] 食事未記録日・体重未記録日・BMR設定なしで null が正しく出ること
- [ ] モバイル幅でサブメニューがはみ出ないこと

---

## Phase35r — フロントエンドBMR計算からAPI方式への移行

### 背景・理由

Phase35 Step1〜10 実装後、履歴ページの読み込みが著しく遅くなる問題が発生。
原因：`fetchSettings()` をページロード時の `Promise.all` に追加したことによるバックグラウンド設定読み込みの影響。
対策：BMR計算をサーバーサイドに移し、コピーボタンクリック時のみAPIを呼び出す方式に変更。

### Phase35r 目次

| Step | ファイル | 内容 |
|------|---------|------|
| Phase35r-Step1 | database.py | コピー用エンリッチメント関数追加（Mifflin式・日別BMR） |
| Phase35r-Step2 | main.py | コピー用APIエンドポイント追加 |
| Phase35r-Step3 | history.html | fetchSettings/BMRユーティリティ除去・コピーハンドラAPI化 |
| Phase35r-Step4 | stats.html | BMRユーティリティ除去・コピーハンドラAPI化 |

### 設計方針

**新APIエンドポイント:**
- `GET /api/copy/history/{date}` → `{bmr_kcal, total_expenditure, calorie_balance, calories_goal, steps_goal}`
- `GET /api/copy/stats?from_date=...&to_date=...` → `{dates, calories_goal, steps_goal, bmr_kcal[], total_expenditure[], calorie_balance[]}`

**BMR計算式:** Mifflin-St Jeor（既存の `calculate_bmr()` は Harris-Benedict のため新関数を追加）
- 男性: `10×体重 + 6.25×身長 - 5×年齢 + 5`
- 女性: `10×体重 + 6.25×身長 - 5×年齢 - 161`
- 体重フォールバック: 朝体重 → 夜体重 → 期間内最近傍

### Phase35r-Step1 実施結果

- **修正日:** 2026-04-29
- **修正ファイル:** `database.py`
- **修正内容:**
  - `_calc_age_at_date(birthdate_str, target_date_str)` — 指定日時点の年齢計算
  - `_calc_bmr_mifflin(weight_kg, height_cm, age, gender)` — Mifflin-St Jeor式BMR
  - `_get_weight_for_copy_date(date_str)` — 指定日の体重（朝優先→夜→最近傍）
  - `get_copy_enrichment_day(date_str)` — 履歴ページ用1日分エンリッチメント
  - `get_copy_enrichment_stats(from_date, to_date)` — 集計ページ用日別BMR配列
- **コミット:** `e78efeb`
- **テスト:** なし（APIエンドポイント追加後に統合確認）

### Phase35r-Step2 実施結果

- **修正日:** 2026-04-29
- **修正ファイル:** `main.py`
- **修正内容:**
  - `GET /api/copy/history/{date}` エンドポイント追加（日付形式バリデーション付き）
  - `GET /api/copy/stats` エンドポイント追加（from_date/to_date バリデーション・大小チェック付き）
  - 既存エンドポイントの `require_auth(request)` パターンに準拠
- **コミット:** `a926bac`
- **テスト:** なし

### Phase35r-Step3 実施結果

- **修正日:** 2026-04-29
- **修正ファイル:** `static/history.html`
- **修正内容:**
  - `fetchSettings()` 関数を削除
  - `Promise.all([fetchLastActivityDate(), fetchSettings()])` → `fetchLastActivityDate().then(...)` に戻す
  - `pageState` から `stepsGoal`, `heightCm`, `gender`, `birthdate` を削除
  - `calcAgeAt`, `calcBmrForDay`, `getWeightForBmr` 関数を削除
  - `buildHistoryTsv(day, enrichment)` / `buildHistoryJson(day, enrichment)` — enrichment パラメータから値を参照
  - コピーハンドラ: クリック時に `/api/copy/history/{date}` を fetch → enrichment を取得してフォーマット
- **コミット:** `53d36fb`
- **テスト:** なし

### Phase35r-Step4 実施結果

- **修正日:** 2026-04-29
- **修正ファイル:** `static/stats.html`
- **修正内容:**
  - `calcAgeAt`, `calcBmrForDay`, `buildWeightMap` 関数を削除
  - `pageState` から `stepsGoal`, `heightCm`, `gender`, `birthdate` を削除
  - 設定ロード時の BMR設定保存コード（4行）を削除
  - `buildStatsTsv(data, enrichment)` / `buildStatsJson(data, enrichment)` — enrichment パラメータから値を参照
  - `doCopy()`: `/api/copy/stats?from_date=...&to_date=...` を fetch → enrichment を取得してフォーマット
- **コミット:** `c5efc86`
- **テスト:** なし

### Phase35r-Fix（レビュー指摘修正）実施結果

**修正日:** 2026-04-29
**修正コミット:**
- `f2f98a0` Phase35r-Fix1 `database.py` — logger追加・例外ガード修正
- `f9f0e6a` Phase35r-Fix2 `main.py` — 日付バリデーションを`_validate_date()`に統一・422に統一
- `0b6a305` Phase35r-Fix3 `history.html,stats.html` — 401リダイレクト追加・fetchエラーハンドリング規約準拠

**テスト結果:** `test/test_phase35r.py` 34件 ALL PASSED

---

### Phase35r サマリー

**完了内容:** フロントエンドBMR計算を完全にサーバーサイドAPIへ移行。ページロード時のバックグラウンド設定読み込みを排除し、BMR計算はコピーボタンクリック時のみ実行する設計に変更。

**テスト:** `test/test_phase35r.py` — 34件（`_calc_age_at_date`・`_calc_bmr_mifflin`・`get_copy_enrichment_day`・エンドポイント認証/バリデーション）ALL PASSED

**レビュアーへの報告事項:**
- `calculate_bmr()` (Harris-Benedict) と `_calc_bmr_mifflin()` (Mifflin-St Jeor) が並存する状態。コピー機能はMifflin式を使用し、既存のBMI情報表示はHarris-Benedict式のまま。
- コピーボタンクリック時にAPIコールが発生するため、オフライン環境ではコピー不可（エラー時は「✗ 失敗」表示）。
- `daily_calorie_goal` フォールバック値（1500）とDBデフォルト（1800）の乖離は既存コードベース全体の技術的負債（今回スコープ外）。
