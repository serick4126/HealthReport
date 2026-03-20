# HealthReport — 食事記録 Web アプリ

FastAPI + Claude API を使った個人用健康管理 Web アプリです。
食事・体重・歩数の記録、グラフ表示、PDF レポート出力に対応しています。

---

## 技術スタック

| 項目 | 内容 |
|------|------|
| バックエンド | Python 3.11+, FastAPI, uvicorn |
| AI | Anthropic Claude API (claude-sonnet-4-6) |
| DB | SQLite3 |
| フロント | HTML / CSS / JavaScript (バニラ) |
| グラフ | matplotlib, Chart.js |
| パッケージ管理 | uv (推奨) / pip |

---

## ローカル開発環境のセットアップ

### 前提条件

- Python 3.11 以上
- [uv](https://docs.astral.sh/uv/) インストール済み

```bash
# uv のインストール（未インストールの場合）
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 手順

```bash
# 1. リポジトリをクローン
git clone <repository-url>
cd HealthReport

# 2. Python 3.11 のインストール（システムの Python を変更しません）
uv python install 3.11

# 3. 仮想環境の作成と依存パッケージのインストール
uv sync

# 3. 環境変数の設定
cp .env.example .env   # .env.example がない場合は手動で作成
```

`.env` ファイルの内容：

```env
ANTHROPIC_API_KEY=sk-ant-api03-xxxxxxxxxxxxxxxx
APP_PASSWORD=your_password_here
```

### 起動

```bash
uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

ブラウザで `http://localhost:8000` を開いてください。

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

# Python 3.11 のインストール（システムの Python を変更しません）
uv python install 3.11
```

### 2. アプリのデプロイ

```bash
# アプリディレクトリに配置
git clone <repository-url> /opt/healthreport
cd /opt/healthreport

# 仮想環境の作成と依存パッケージのインストール
uv sync

# 環境変数の設定
nano .env
```

`.env` の内容（本番用に強いパスワードを設定）：

```env
ANTHROPIC_API_KEY=sk-ant-api03-xxxxxxxxxxxxxxxx
APP_PASSWORD=強いパスワードをここに設定
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
EnvironmentFile=/opt/healthreport/.env
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

## 初回起動後の設定

1. `http://your-server` にアクセス
2. `.env` の `APP_PASSWORD` でログイン
3. **⚙️ 設定画面** で以下を変更：
   - ユーザー名（DefaultName → 実名）
   - 身長（160cm → 実際の値）
   - カロリー目標（1800kcal → 実際の目標値）
   - Anthropic API キー（`.env` で設定済みの場合は不要）
   - アクセスパスワード（`1234` からセキュアなものへ変更を推奨）
   - 注意事項（疾患・食事制限など Claude への指示）

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
