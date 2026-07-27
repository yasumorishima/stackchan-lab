"""本体（ESP32 側）の MCP サーバーに対するクライアント。

XiaoZhi のプロトコルでは、本体が MCP サーバー、バックエンドが MCP クライアントになる。
WebSocket の上に `{"type":"mcp","payload":{JSON-RPC 2.0}}` で包んで往復する。
仕様は 78/xiaozhi-esp32 の docs/mcp-protocol.md に準拠。

  1. 本体の hello に features.mcp があれば initialize を送る
  2. tools/list でツール一覧を取る（nextCursor があればページング）
  3. LLM が関数呼び出しを返したら tools/call で本体に実行させる
"""
import asyncio
import json
import logging

log = logging.getLogger("stackchan.mcp")

PROTOCOL_VERSION = "2024-11-05"


def _safe_name(name: str) -> str:
    # OpenAI 互換 API の function 名は [a-zA-Z0-9_-] のみ。本体のツール名は "self.x.y" 形式
    return "".join(c if (c.isalnum() or c in "_-") else "_" for c in name)[:64]


class McpSession:
    def __init__(self, send_json, timeout: float = 10.0):
        self._send_json = send_json
        self._timeout = timeout
        self._next_id = 1
        self._pending = {}
        self.tools = []          # 本体が返した生のツール定義
        self.server_info = {}
        self._by_safe = {}       # 安全名 -> 本体のツール名

    # ---- 送受信 --------------------------------------------------------
    async def _request(self, method: str, params=None):
        rid = self._next_id
        self._next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        payload = {"jsonrpc": "2.0", "method": method, "id": rid}
        if params is not None:
            payload["params"] = params
        await self._send_json({"type": "mcp", "payload": payload})
        try:
            return await asyncio.wait_for(fut, self._timeout)
        finally:
            self._pending.pop(rid, None)

    def handle(self, payload: dict):
        """本体から来た mcp メッセージ。応答なら待っている request を起こす。"""
        rid = payload.get("id")
        if rid is None:
            log.info("mcp notification: %s", json.dumps(payload, ensure_ascii=False)[:200])
            return
        fut = self._pending.get(rid)
        if fut is None or fut.done():
            log.warning("mcp response for unknown id %s", rid)
            return
        if "error" in payload:
            fut.set_exception(RuntimeError(json.dumps(payload["error"], ensure_ascii=False)))
        else:
            fut.set_result(payload.get("result") or {})

    # ---- 手順 ----------------------------------------------------------
    async def handshake(self):
        init = await self._request("initialize", {"capabilities": {}})
        self.server_info = init.get("serverInfo") or {}
        log.info("mcp initialize ok: %s (protocol %s)",
                 self.server_info, init.get("protocolVersion"))

        tools, cursor = [], ""
        for _ in range(10):      # ページングの上限（暴走よけ）
            res = await self._request("tools/list", {"cursor": cursor,
                                                     "withUserTools": False})
            tools.extend(res.get("tools") or [])
            cursor = res.get("nextCursor") or ""
            if not cursor:
                break
        self.tools = tools
        self._by_safe = {_safe_name(t.get("name", "")): t.get("name", "") for t in tools}
        log.info("mcp tools: %d (%s)", len(tools),
                 ", ".join(t.get("name", "?") for t in tools[:8]))
        return tools

    def openai_tools(self):
        """OpenAI 互換 API の tools 配列に変換する。"""
        out = []
        for t in self.tools:
            name = t.get("name", "")
            if not name:
                continue
            out.append({
                "type": "function",
                "function": {
                    "name": _safe_name(name),
                    "description": (t.get("description") or name)[:1024],
                    "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
                },
            })
        return out

    async def call(self, safe_name: str, arguments: dict) -> str:
        name = self._by_safe.get(safe_name, safe_name)
        res = await self._request("tools/call", {"name": name, "arguments": arguments})
        parts = []
        for c in res.get("content") or []:
            if c.get("type") == "text":
                parts.append(c.get("text") or "")
        text = "\n".join(parts) or json.dumps(res, ensure_ascii=False)
        if res.get("isError"):
            return "error: " + text
        return text
