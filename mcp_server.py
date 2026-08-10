"""MCP サーバー定義。

claude.ai（Web版）カスタムコネクタから HealthReport の記録操作を提供する。
推論はクライアント側 AI が担い、本サーバーは Anthropic API を呼ばない（設計仕様書 §1.4）。
"""
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

import database


async def get_daily_summary(date: str | None = None) -> dict:
    """指定日（省略時は論理上の今日）の食事・体重・歩数・体脂肪・スキップ状況を返す。

    date は原則省略する。ユーザーが明示的に過去日を指定した場合のみ渡すこと。
    """
    target_date = date or database.get_logical_today_jst()
    return {
        "success": True,
        "tool": "get_daily_summary",
        "date": target_date,
        "summary": database.get_daily_summary(target_date),
    }


def build_mcp_server(instructions: str | None = None) -> MCPServer:
    """MCPServer を構築し、ツールを登録して返す。"""
    server = MCPServer(name="healthreport", instructions=instructions)
    server.tool()(get_daily_summary)
    return server


def build_mcp_asgi(server: MCPServer):
    """MCPServer を Streamable HTTP の Starlette アプリとして返す。"""
    return server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            # P-2: DNSリバインディング保護は既定 ON で Host ヘッダを検証する。
            # nginx 経由の公開ドメインからアクセスするため、ここで無効化する。
            enable_dns_rebinding_protection=False,
        ),
    )
