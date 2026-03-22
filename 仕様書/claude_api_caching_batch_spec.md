# Claude API コスト最適化仕様書
## プロンプトキャッシング & Batch API 実装仕様

**作成日**: 2026-03-22  
**対象モデル**: claude-sonnet-4-6 / claude-haiku-4-5  
**対象言語**: Python  
**開発環境**: Claude Code

---

## 目次

1. [概要・前提条件](#1-概要前提条件)
2. [プロンプトキャッシング仕様](#2-プロンプトキャッシング仕様)
3. [Batch API 仕様](#3-batch-api-仕様)
4. [組み合わせ利用（推奨構成）](#4-組み合わせ利用推奨構成)
5. [コスト比較シミュレーション](#5-コスト比較シミュレーション)
6. [Claude Code 開発時の注意事項・提案機能](#6-claude-code-開発時の注意事項提案機能)
7. [参考URL](#7-参考url)

---

## 1. 概要・前提条件

### 目的
食品栄養成分推定アプリにおいて、Claude API のコストを最小化する。

### 利用モデル
| モデル | 用途 | 標準料金（入力/出力 per 1M） |
|--------|------|----------------------------|
| claude-sonnet-4-6 | 精度重視の推定 | $3.00 / $15.00 |
| claude-haiku-4-5  | 軽量・高速処理 | $1.00 / $5.00  |

### コスト最適化の2本柱
| 機能 | 効果 | 適用場面 |
|------|------|---------|
| **プロンプトキャッシング** | システムプロンプト再利用でコスト最大90%削減 | 対話型（リアルタイム応答） |
| **Batch API** | 全トークン 50% 割引 | 非同期・バックグラウンド処理 |

---

## 2. プロンプトキャッシング仕様

### 2-1. 仕組み

- 同一のシステムプロンプトを繰り返し送信する場合、初回のみ書き込みコストが発生し、  
  2回目以降は**キャッシュ読み取りコスト（通常の10%）**が適用される
- キャッシュ有効期間（TTL）: デフォルト **5分**（アクセスのたびにリセット）
- 拡張TTL: **1時間**（ベータ機能、別途ヘッダー必要）

### 2-2. 料金倍率

| 種別 | 料金倍率 | 備考 |
|------|---------|------|
| 通常入力 | ×1.0 | ベースライン |
| キャッシュ書き込み（5分） | ×1.25 | 初回のみ |
| キャッシュ書き込み（1時間） | ×2.0 | ベータ機能 |
| **キャッシュ読み取り** | **×0.1** | 2回目以降、90%オフ |

### 2-3. 最小キャッシュトークン数

| モデル | 最小トークン数 |
|--------|--------------|
| Sonnet 4.6 / Opus 4.x | **1,024 トークン** |
| Haiku 4.5 / Haiku 3.x | **2,048 トークン** |

> ⚠️ これ未満のプロンプトは `cache_control` を付けてもキャッシュされない

### 2-4. 実装方法

#### システムプロンプトをキャッシュする（基本形）

```python
import anthropic

client = anthropic.Anthropic(api_key="YOUR_API_KEY")

# system を文字列 → リスト形式に変更し、cache_control を追加するだけ
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    system=[
        {
            "type": "text",
            "text": "（大きなシステムプロンプト）",
            "cache_control": {"type": "ephemeral"}  # ← これだけ追加
        }
    ],
    messages=[
        {"role": "user", "content": "田中そば店 山形味噌ラーメンの栄養成分を推定して"}
    ]
)

# キャッシュ状況の確認
usage = response.usage
print(f"通常入力トークン      : {usage.input_tokens}")
print(f"キャッシュ書き込み    : {usage.cache_creation_input_tokens}")
print(f"キャッシュ読み取り    : {usage.cache_read_input_tokens}")
print(f"出力トークン          : {usage.output_tokens}")
```

#### 1時間TTL（拡張キャッシュ）を使う場合

```python
# ベータヘッダーが必要
client = anthropic.Anthropic(
    api_key="YOUR_API_KEY",
    default_headers={"anthropic-beta": "extended-cache-ttl-2025-04-11"}
)

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    system=[
        {
            "type": "text",
            "text": "（大きなシステムプロンプト）",
            "cache_control": {"type": "ephemeral", "ttl": "1h"}  # TTL指定
        }
    ],
    messages=[{"role": "user", "content": "..."}]
)
```

### 2-5. キャッシュブレークポイントの設計

- 1つのリクエストに最大 **4つ** の `cache_control` を設定できる
- **静的な内容（変わらない部分）を前に、動的な部分（ユーザー入力）を後ろに**配置するのが鉄則

```
【推奨レイアウト】

[system]
  → テキスト①: 基本ルール・役割定義  ←── cache_control ここ
  → テキスト②: 栄養推定のガイドライン ←── cache_control ここ（必要なら）

[messages]
  → user: 食品名（毎回変わる動的部分）
```

### 2-6. キャッシュヒット率を上げるためのポイント

- システムプロンプトの**内容を一切変えない**（1文字でも異なるとキャッシュミス）
- **5分以内**に次のリクエストを送ればキャッシュが再利用される（1時間TTLなら1時間以内）
- 並列リクエストの場合、最初の1件の応答が返ってから次を送ること（同時送信はキャッシュが使えない）

---

## 3. Batch API 仕様

### 3-1. 仕組み

- 複数のリクエストをまとめて非同期で処理する
- リアルタイム応答は得られないが、**全トークンが50%割引**になる
- 1バッチに最大 **10,000件** / 最大 **32MB** まで送信可能

### 3-2. 処理時間

| ケース | 目安 |
|--------|------|
| 通常 | **1時間以内**に完了することが多い |
| 最大 | **24時間以内** |
| 有効期限切れ | 29日後に自動削除 |

> ⚠️ リアルタイム応答が必要な対話型UIには **使用不可**

### 3-3. バッチ料金（Sonnet 4.6の場合）

| | 通常API | Batch API |
|-|---------|-----------|
| 入力 | $3.00/1M | **$1.50/1M** |
| 出力 | $15.00/1M | **$7.50/1M** |

### 3-4. 実装方法

#### バッチ送信

```python
import anthropic
import time

client = anthropic.Anthropic(api_key="YOUR_API_KEY")

# 複数食品名をまとめてバッチ送信
food_items = [
    "田中そば店 山形味噌ラーメン",
    "マクドナルド ビッグマック",
    "吉野家 牛丼（並）",
    # ... 最大10,000件
]

requests = [
    {
        "custom_id": f"food_{i}",  # 後で結果を紐づけるID
        "params": {
            "model": "claude-sonnet-4-6",  # または claude-haiku-4-5
            "max_tokens": 1024,
            "system": "あなたは食品の栄養成分推定の専門家です。...",
            "messages": [
                {"role": "user", "content": f"{food}の栄養成分を推定してください"}
            ]
        }
    }
    for i, food in enumerate(food_items)
]

# バッチ作成
batch = client.messages.batches.create(requests=requests)
print(f"バッチID: {batch.id}")
print(f"ステータス: {batch.processing_status}")
```

#### ポーリング（完了待機）

```python
def wait_for_batch(client, batch_id, poll_interval=60):
    """バッチ完了まで待機する"""
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        status = batch.processing_status

        if status == "ended":
            print("✅ バッチ処理完了")
            return batch
        elif status == "errored":
            raise Exception(f"バッチエラー: {batch_id}")
        
        counts = batch.request_counts
        print(f"処理中... 完了: {counts.succeeded} / エラー: {counts.errored} / 処理待ち: {counts.processing}")
        time.sleep(poll_interval)

batch = wait_for_batch(client, batch.id)
```

#### 結果取得

```python
results = {}
for result in client.messages.batches.results(batch.id):
    custom_id = result.custom_id

    if result.result.type == "succeeded":
        text = result.result.message.content[0].text
        results[custom_id] = {"status": "ok", "text": text}
    elif result.result.type == "errored":
        error = result.result.error
        results[custom_id] = {"status": "error", "error": str(error)}

# 例: 結果を確認
for food_id, data in results.items():
    print(f"{food_id}: {data['status']}")
```

#### バッチのキャンセル

```python
client.messages.batches.cancel(batch_id)
```

### 3-5. バッチ処理フロー

```
[アプリ側]                         [Anthropic側]
    │                                    │
    ├─ バッチ作成リクエスト ──────────────►│
    │                                    │ 非同期処理中
    │◄── バッチID返却 ───────────────────┤ (最大24時間)
    │                                    │
    ├─ ステータス確認（ポーリング） ───────►│
    │◄── processing_status: in_progress ─┤
    │                                    │
    │  ...（60秒ごとに確認推奨）...       │
    │                                    │
    ├─ ステータス確認 ─────────────────── ►│
    │◄── processing_status: ended ───────┤
    │                                    │
    ├─ 結果取得リクエスト ─────────────── ►│
    │◄── 全リクエストの結果 ─────────────┤
```

---

## 4. 組み合わせ利用（推奨構成）

### 4-1. 処理モード選択フロー

```
ユーザーから食品名入力
        │
        ▼
  即時応答が必要？
   ┌──────┴──────┐
  Yes             No
   │               │
   ▼               ▼
通常API          Batch API
+ キャッシング   + キャッシング（任意）
（リアルタイム）  （50%割引・非同期）
```

### 4-2. 併用時のコスト計算（Sonnet 4.6、システムプロンプト8,000トークンの場合）

| 方法 | 入力コスト（1回あたり） | 削減率 |
|------|----------------------|--------|
| 通常API、キャッシュなし | $0.048 | 基準 |
| 通常API + キャッシング（2回目以降） | $0.005 | **約90%削減** |
| Batch API のみ | $0.024 | **50%削減** |
| Batch API + キャッシング | $0.0025 | **約95%削減** |

### 4-3. 推奨アーキテクチャ

```python
def estimate_nutrition(food_name: str, mode: str = "realtime"):
    """
    mode:
      "realtime" → 通常API + プロンプトキャッシング
      "batch"    → Batch API（後で結果を取得）
    """
    if mode == "realtime":
        return call_with_caching(food_name)
    elif mode == "batch":
        return submit_to_batch(food_name)
```

---

## 5. コスト比較シミュレーション

### 前提条件
- システムプロンプト: 8,000トークン
- ユーザー入力 + 出力: 各500トークン
- 1日100件の処理

### Sonnet 4.6 使用時

| 構成 | 1日コスト | 月額コスト（30日） |
|------|----------|------------------|
| キャッシュなし・通常API | $6.00 | **$180** |
| キャッシング（2回目以降） | $0.82 | **$24.6** |
| Batch API のみ | $3.00 | **$90** |
| **Batch API + キャッシング** | **$0.41** | **$12.3** |

---

## 6. Claude Code 開発時の注意事項・提案機能

### 6-1. ⚠️ 注意事項

#### キャッシング関連
- `system` パラメータを**文字列から配列に変更**する必要がある（既存コードの修正箇所）
- システムプロンプトに**動的な値（日付・タイムスタンプ等）を含めない**こと（キャッシュミスの原因）
- `cache_creation_input_tokens` が返ってこない = キャッシュされていない → トークン数を確認

#### Batch API関連
- バッチ送信後、**結果取得のポーリング処理**を必ず実装する
- `custom_id` は**ユーザーID・食品名・タイムスタンプ等で一意**にすること
- エラーレスポンス（`result.type == "errored"`）のハンドリングを忘れずに
- バッチは**29日後に自動削除**されるため、結果はDBに保存すること

#### 共通
- **APIキーは環境変数で管理**（`.env` ファイル + `python-dotenv`、または OS 環境変数）
- レートリミットエラー（429）に備えた**リトライ処理**を実装する

---

### 6-2. 💡 提案機能（Claude Code 実装推奨）

#### ① トークン・コスト追跡ログ
```python
# レスポンスのusageを毎回ログに残す
def log_usage(response, mode="realtime"):
    usage = response.usage
    cost = calculate_cost(usage, model="sonnet")
    logger.info({
        "mode": mode,
        "input_tokens": usage.input_tokens,
        "cache_creation": usage.cache_creation_input_tokens,
        "cache_read": usage.cache_read_input_tokens,
        "output_tokens": usage.output_tokens,
        "estimated_cost_usd": cost
    })
```
→ コスト最適化が実際に効いているか可視化できる

#### ② モデル自動切り替え機能
```python
# 精度が必要 → Sonnet / 大量処理 → Haiku と自動で切り替え
def select_model(food_name: str, priority: str = "balanced") -> str:
    if priority == "speed":
        return "claude-haiku-4-5"
    elif priority == "accuracy":
        return "claude-sonnet-4-6"
    else:
        return "claude-haiku-4-5"  # デフォルトは安価なHaiku
```

#### ③ バッチ結果の永続化（SQLite推奨）
```python
# バッチ結果はDBに保存し、同じ食品名なら再利用
def get_or_estimate(food_name: str) -> dict:
    cached = db.get_cached_result(food_name)
    if cached:
        return cached  # API不要（タダ）
    result = call_api(food_name)
    db.save_result(food_name, result)
    return result
```
→ 同じ食品名への重複リクエストをゼロにできる（最大のコスト削減）

#### ④ キャッシュヒット率モニタリング
```python
# キャッシュヒット率を計算して警告
def check_cache_efficiency(usage):
    total_input = usage.input_tokens + usage.cache_read_input_tokens
    hit_rate = usage.cache_read_input_tokens / total_input if total_input > 0 else 0
    if hit_rate < 0.5:
        logger.warning(f"キャッシュヒット率が低下: {hit_rate:.1%}")
    return hit_rate
```

#### ⑤ バッチ進捗ステータスのファイル保存
```python
# バッチIDをファイルに保存しておく（プロセス再起動後も追跡可能）
import json, pathlib

def save_batch_state(batch_id, metadata):
    state = {"batch_id": batch_id, "metadata": metadata}
    pathlib.Path("batch_state.json").write_text(json.dumps(state, ensure_ascii=False))

def load_batch_state():
    p = pathlib.Path("batch_state.json")
    if p.exists():
        return json.loads(p.read_text())
    return None
```

#### ⑥ ドライランモード（開発時コスト節約）
```python
# 開発中はAPIを叩かずにダミーレスポンスを返す
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

def call_api(food_name: str):
    if DRY_RUN:
        return {"food": food_name, "calories": 500, "note": "DRY RUN"}
    return actual_api_call(food_name)
```
→ Claude Code での開発・テスト中に余計なAPIコストが発生しない

---

## 7. 参考URL

### 公式ドキュメント（Anthropic）

| 項目 | URL |
|------|-----|
| プロンプトキャッシング（日本語） | https://docs.anthropic.com/ja/docs/build-with-claude/prompt-caching |
| Batch API / Message Batches（日本語） | https://docs.anthropic.com/ja/docs/build-with-claude/message-batches |
| Batch API リファレンス（日本語） | https://docs.anthropic.com/ja/docs/build-with-claude/batch-processing |
| 料金ページ | https://www.anthropic.com/pricing |
| APIリファレンス（Messages） | https://docs.anthropic.com/en/api/messages |
| Python SDK（GitHub） | https://github.com/anthropics/anthropic-sdk-python |
| モデル一覧 | https://docs.anthropic.com/en/docs/about-claude/models/overview |

### Python SDK インストール

```bash
pip install anthropic
# または uv を使う場合（Serick さんの環境）
uv add anthropic
```

---

*以上。Claude Code での開発時は、本仕様書をプロジェクトルートに置いて CLAUDE.md から参照させると効果的です。*
