"""
WebLens MCP Server — 让 Claude/Cursor 直接调用搜+抓能力。

启动方式：
    py mcp_server.py

Claude Desktop 配置：
{
  "mcpServers": {
    "weblens": {
      "command": "py",
      "args": ["d:\\scrapling-demo\\Scrapling开发全记录\\UniversalExtractor\\mcp_server.py"]
    }
  }
}

提供的工具：
  - search_and_extract : 搜索 + 智能筛选 + 完整抓取
  - extract_text       : 从指定 URL 提取正文
  - fetch_jd           : 结构化 JD 抓取
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# Ensure parent is importable (when run as script)
_parent = Path(__file__).resolve().parent.parent
if str(_parent) not in sys.path:
    sys.path.insert(0, str(_parent))

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("weblens-mcp")

# ---- Lazy imports (avoid heavy deps on startup) ----
_extractor = None
_jd_engine = None
_weblens = None


def _get_extractor():
    global _extractor
    if _extractor is None:
        from universal_extractor import UniversalExtractor
        _extractor = UniversalExtractor(headless=True)
    return _extractor


def _get_jd_engine():
    global _jd_engine
    if _jd_engine is None:
        from universal_extractor import JDEngine
        _jd_engine = JDEngine(headless=True)
    return _jd_engine


def _get_weblens():
    global _weblens
    if _weblens is None:
        from universal_extractor.weblens import WebLens
        _weblens = WebLens(headless=True)
    return _weblens


# ============================================================
# Tool definitions
# ============================================================

TOOLS = [
    {
        "name": "search_and_extract",
        "description": (
            "搜索网页并智能提取正文。自动搜索多个搜索引擎，"
            "对候选 URL 快速扫描筛选，选择最佳来源进行完整抓取。"
            "适用于：找小说全文、查资料、搜教程、找任何网页内容。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，如 '三体 小说 全文'、'Python 异步编程 教程'",
                },
                "max_chars": {
                    "type": "integer",
                    "default": 5000,
                    "description": "最大返回字符数，控制 token 消耗",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "extract_text",
        "description": (
            "从指定 URL 提取网页正文。自动处理 JS 渲染、反爬保护、"
            "Canvas 绑图页面。6 层降级链：DOM → API 拦截 → Canvas Hook "
            "→ CDP 扫描 → OCR → Vision LLM。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "网页 URL",
                },
                "max_chars": {
                    "type": "integer",
                    "default": 5000,
                    "description": "最大返回字符数",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "fetch_jd",
        "description": (
            "抓取招聘页面的结构化 JD。返回岗位名称、公司、薪资、"
            "技术要求、职责等结构化信息。支持 BOSS直聘、猎聘、拉勾等平台。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "招聘详情页 URL",
                },
            },
            "required": ["url"],
        },
    },
]


# ============================================================
# Tool handlers
# ============================================================

def handle_search_and_extract(arguments: dict) -> str:
    query = arguments.get("query", "")
    max_chars = arguments.get("max_chars", 5000)

    if not query:
        return json.dumps({"error": "query is required"}, ensure_ascii=False)

    wl = _get_weblens()
    result = wl.search_and_extract(query)

    text = result.text[:max_chars] if result.text else ""

    return json.dumps({
        "url": result.url,
        "text": text,
        "score": result.score,
        "candidates_scanned": result.candidates_scanned,
        "candidates_total": result.candidates_total,
        "truncated": len(result.text) > max_chars if result.text else False,
        "original_length": len(result.text) if result.text else 0,
    }, ensure_ascii=False, indent=2)


def handle_extract_text(arguments: dict) -> str:
    url = arguments.get("url", "")
    max_chars = arguments.get("max_chars", 5000)

    if not url:
        return json.dumps({"error": "url is required"}, ensure_ascii=False)

    ue = _get_extractor()
    text = ue.extract(url)
    result = text[:max_chars] if text else ""

    return json.dumps({
        "url": url,
        "text": result,
        "truncated": len(text) > max_chars,
        "original_length": len(text),
    }, ensure_ascii=False, indent=2)


def handle_fetch_jd(arguments: dict) -> str:
    url = arguments.get("url", "")

    if not url:
        return json.dumps({"error": "url is required"}, ensure_ascii=False)

    engine = _get_jd_engine()
    jd = engine.fetch_jd(url)
    return json.dumps(jd.to_dict(), ensure_ascii=False, indent=2)


HANDLERS = {
    "search_and_extract": handle_search_and_extract,
    "extract_text": handle_extract_text,
    "fetch_jd": handle_fetch_jd,
}


# ============================================================
# MCP stdio server
# ============================================================

def _run_stdio():
    """Run as raw stdio JSON-RPC server (fallback without mcp package)."""
    import sys as _sys

    for line in _sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        req_id = request.get("id")
        method = request.get("method", "")

        if method == "tools/list":
            response = {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
        elif method == "tools/call":
            params = request.get("params", {})
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            handler = HANDLERS.get(tool_name)
            if handler:
                try:
                    result_text = handler(arguments)
                    response = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": result_text}],
                        },
                    }
                except Exception as exc:
                    response = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -1, "message": str(exc)},
                    }
            else:
                response = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                }
        elif method == "initialize":
            response = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "weblens", "version": "1.0.0"},
                },
            }
        elif method == "notifications/initialized":
            continue  # No response needed
        else:
            response = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
            }

        _sys.stdout.write(json.dumps(response) + "\n")
        _sys.stdout.flush()


def _run_mcp():
    """Run with mcp Python SDK."""
    from mcp.server import Server
    import mcp.server.stdio

    server = Server("weblens")

    @server.list_tools()
    async def list_tools():
        return TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        handler = HANDLERS.get(name)
        if not handler:
            raise ValueError(f"Unknown tool: {name}")
        result_text = handler(arguments)
        from mcp.types import TextContent
        return [TextContent(type="text", text=result_text)]

    mcp.server.stdio.run(server)


if __name__ == "__main__":
    try:
        _run_mcp()
    except ImportError:
        # mcp package not installed, fallback to raw stdio
        logger.info("mcp SDK not found, using raw stdio fallback")
        _run_stdio()
