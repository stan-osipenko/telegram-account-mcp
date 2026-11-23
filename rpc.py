"""Private SSH-friendly MCP caller; credentials stay on the VPS."""
import asyncio
import json
from pathlib import Path
import sys
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main():
    from instance import CONFIG, PRIVATE
    token = (PRIVATE / 'mcp-token').read_text().strip()
    tool = sys.argv[1]
    arguments = json.loads(sys.stdin.read() or '{}')
    async with streamablehttp_client(f"http://127.0.0.1:{CONFIG['port']}/mcp",
            headers={'Authorization':'Bearer '+token}) as (reader, writer, _):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
            text = next((item.text for item in result.content if item.type == 'text'), '')
            try:
                output = result.structuredContent or json.loads(text)
            except ValueError:
                output = text
            print(json.dumps({'is_error':bool(result.isError), 'result':output}, ensure_ascii=False))
            if result.isError:
                raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
