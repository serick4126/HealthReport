# HealthReport — 食事記録 Web アプリ

FastAPI + Claude API を使った個人用健康管理 Web アプリです。
自然言語で食事・体重・歩数を記録でき、グラフ表示や週次レポート出力に対応しています。

---

## 主な機能

- **自然言語で記録** — 「朝食にトースト2枚と目玉焼き」のように話しかけるだけで食事を記録
- **カロリー自動検索** — Slism 食品データベースから栄養情報を自動取得
- **体重・歩数の記録** — iPhone ヘルスケア連携（ショートカット API）にも対応
- **グラフ表示** — カロリー推移・体重推移・PFC バランスを可視化
- **週次レポート** — PDF 出力可能な週次健康レポートを AI コメント付きで生成
- **食事画像の保存** — 写真を添付して記録
- **ダークモード** — 自動 / 手動切り替え対応

---

## 技術スタック

| 項目 | 内容 |
|------|------|
| バックエンド | Python 3.11+, FastAPI, uvicorn |
| AI | Anthropic Claude API (claude-sonnet-4-6 / claude-haiku-4-5) |
| DB | SQLite3 (WAL モード) |
| フロント | HTML / CSS / JavaScript (バニラ) |
| グラフ | matplotlib, Chart.js |
| パッケージ管理 | uv (推奨) / pip |

---

## クイックスタート

### 前提条件

- Python 3.11 以上
- [uv](https://docs.astral.sh/uv/) インストール済み
- Anthropic API キー（[console.anthropic.com](https://console.anthropic.com/) で取得）

```bash
# uv のインストール — Linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```powershell
# uv のインストール — Windows (PowerShell)
winget install --id astral-sh.uv 
```

### セットアップと起動

```bash
# 1. リポジトリをクローン
git clone https://github.com/serick4126/HealthReport.git
cd HealthReport

# 2. Python 3.11 のインストール（未インストールの場合のみ。システムの Python を変更しません）
uv python install 3.11

# 3. 仮想環境の作成と依存パッケージのインストール
uv sync

# 4. 起動（ブラウザ自動オープン）
uv run run.py --browser
```

Windows の場合は `start.bat` をダブルクリックでも起動できます。

<details>
<summary>その他の起動方法</summary>

```bash
# 開発時（ホットリロード）
uv run run.py --browser --reload

# uvicorn を直接使う場合
uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

</details>

---

## 初回起動ガイド

### 1. ログイン

初期パスワードは **`1234`** です。
ブラウザで `http://localhost:8000` を開き、このパスワードでログインしてください。

### 2. 設定画面で初期設定

ログイン後、ナビバーの **設定** を開いて以下を設定します。

| 設定項目 | 説明 | 初期値 |
|---------|------|--------|
| **Anthropic API キー** | Claude API の利用に必須 | (未設定) |
| **アクセスパスワード** | ログインパスワード。セキュアなものに変更を推奨 | `1234` |
| **ユーザー名** | Claude が呼びかけに使用 | DefaultName |
| **身長** | BMI 計算に使用 | 160 cm |
| **カロリー目標** | 1日の目標カロリー | 1800 kcal |
| **歩数目標** | 1日の歩数目標 | 8000 歩 |
| **1日の開始時刻** | この時刻より前の記録は前日扱い（深夜の食事対策） | 4 時 |
| **注意事項** | 疾患・食事制限など Claude への補足情報 | (空) |

> **API キーの設定は必須です。** 設定しないとチャット機能が使えません。

### 3. 使い始める

チャット画面に戻って、自然言語で記録を開始できます。

```
朝食にトースト2枚とコーヒー
体重 65.2kg
昨日は8000歩歩いた
```

---

## Ubuntu (VPS) へのデプロイ

### 1. 必要パッケージのインストール

```bash
sudo apt update && sudo apt upgrade -y

# Python 3.11+
sudo apt install -y python3.11 python3.11-venv python3-pip git

# matplotlib 日本語フォント（グラフ内の日本語表示に必要）
sudo apt install -y fonts-noto-cjk

# uv のインストール
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env   # またはシェルを再起動

# Python 3.11 のインストール
uv python install 3.11
```

### 2. アプリのデプロイ

```bash
git clone <repository-url> /opt/healthreport
cd /opt/healthreport
uv sync
```

### 3. systemd サービスとして登録（自動起動）

`/etc/systemd/system/healthreport.service` を作成：

```ini
[Unit]
Description=HealthReport FastAPI App
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/healthreport
ExecStart=/opt/healthreport/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable healthreport
sudo systemctl start healthreport

# 動作確認
sudo systemctl status healthreport
```

起動後、ブラウザでアクセスして「初回起動ガイド」の手順で API キーとパスワードを設定してください。

### 4. Nginx でリバースプロキシ設定（推奨）

```bash
sudo apt install -y nginx
```

`/etc/nginx/sites-available/healthreport`：

```nginx
server {
    listen 80;
    server_name your-domain.com;

    client_max_body_size 20M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        # SSE（ストリーミング）に必要
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/healthreport /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### 5. HTTPS 化（Let's Encrypt、任意）

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```

---

## pip を使う場合（uv が使えない環境）

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

---

## データベース

SQLite3 ファイル（`health.db`）がアプリ起動時に自動作成されます。
バックアップは `health.db` をコピーするだけです。

```bash
# バックアップ例
cp /opt/healthreport/health.db /backup/health_$(date +%Y%m%d).db
```

---

## ログの確認

```bash
# systemd 経由の場合
sudo journalctl -u healthreport -f

# 直接起動の場合はコンソール出力を確認
```
