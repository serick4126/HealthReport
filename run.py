import argparse
import threading
import webbrowser
import socket
import time
import sys
import uvicorn


def wait_and_open_browser(host: str, port: int):
    """ポートが開くまで待機してからブラウザを開く"""
    url = f"http://localhost:{port}"
    connect_host = "localhost" if host == "0.0.0.0" else host
    for _ in range(30):  # 最大30秒待機
        try:
            with socket.create_connection((connect_host, port), timeout=1):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(1)
    print(f"[run.py] タイムアウト: {url} に接続できませんでした")


def main():
    parser = argparse.ArgumentParser(description="FastAPI アプリケーション起動スクリプト")
    parser.add_argument("--browser", action="store_true", help="起動時にブラウザを自動で開く")
    parser.add_argument("--host", default="0.0.0.0", help="ホスト (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="ポート番号 (default: 8000)")
    parser.add_argument("--reload", action="store_true", help="ホットリロードを有効にする")
    parser.add_argument("--workers", type=int, default=None, help="ワーカー数 (reloadと併用不可)")
    args = parser.parse_args()

    if args.reload and args.workers is not None:
        print("[run.py] エラー: --reload と --workers は同時に使用できません。")
        sys.exit(1)

    if args.browser:
        t = threading.Thread(
            target=wait_and_open_browser,
            args=(args.host, args.port),
            daemon=True,
        )
        t.start()

    uvicorn.run(
        "main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
