"""Stable entry points to tools and parameters added after a client cached its tool list.

ChatGPT and Claude remember a connector's tool list (names and parameter schemas) when it is
connected or refreshed. A tool added in a later version, or a new parameter of an existing one,
stays invisible to them until somebody presses "Refresh" in the connector settings. These three
tools never change their names or parameters, so once a client has them it reaches everything
deployed later:

- list_tools_live: the catalog as deployed right now, with full parameter schemas;
- call_read_tool: runs a read-only tool by name (no write confirmation needed);
- call_tool: runs any tool by name.

The call goes through the same registered tool as a direct call: the same argument validation,
chat reference resolution, quotas, expected_chat checks and request_id deduplication.
"""
GATEWAY_TOOLS = ('list_tools_live', 'call_read_tool', 'call_tool')


def _params(tool):
    schema = tool.parameters or {}
    required = set(schema.get('required') or [])
    return [name + ('' if name in required else '?') for name in (schema.get('properties') or {})]


def _read_only(tool):
    return bool(tool.annotations and tool.annotations.readOnlyHint)


def register_gateway_tools(mcp, version, read, write):
    manager = mcp._tool_manager

    def live_tools():
        return [t for t in manager.list_tools() if t.name not in GATEWAY_TOOLS]

    def target(name):
        if name in GATEWAY_TOOLS:
            raise ValueError(f'{name} is a gateway tool; call the target tool by its own name')
        tool = manager.get_tool(name)
        if tool is None:
            raise ValueError(f'Unknown tool {name!r}; call list_tools_live to see the current tools')
        return tool

    @mcp.tool(annotations=read, description=(
        f'Live catalog of this connector as deployed now. Your tool list was captured at version {version} '
        'and may be older: if the user mentions a tool or parameter you do not have, or get_status shows a '
        'newer service_version, call this, then run the tool through call_read_tool (read-only tools) or '
        'call_tool (any tool) with the parameters from input_schema. query filters by name/description; '
        'name returns one tool with its full input_schema.'))
    async def list_tools_live(query: str = '', name: str = '') -> dict:
        if name:
            tool = target(name)
            return {'service_version': version, 'tool': {
                'name': tool.name, 'read_only': _read_only(tool), 'description': tool.description,
                'input_schema': tool.parameters}}
        q = query.strip().lower()
        tools = [t for t in live_tools() if not q or q in t.name.lower() or q in (t.description or '').lower()]
        return {'service_version': version, 'tool_count': len(live_tools()),
                'how_to_call': 'call_read_tool(name, arguments) for read_only tools, call_tool(name, arguments) '
                               'for the rest; list_tools_live(name=...) gives the full input_schema',
                'tools': [{'name': t.name, 'read_only': _read_only(t),
                           'summary': (t.description or '').strip().split('\n')[0][:300],
                           'parameters': _params(t)} for t in tools]}

    @mcp.tool(annotations=read, description=(
        'Run a read-only tool of this connector by name, including tools added after your tool list was '
        'captured. arguments are that tool\'s parameters (see list_tools_live). Refuses tools that change '
        'anything; use call_tool for those.'))
    async def call_read_tool(name: str, arguments: dict | None = None) -> dict:
        tool = target(name)
        if not _read_only(tool):
            raise ValueError(f'{name} changes data; use call_tool after the owner authorized this exact action')
        return {'tool': name, 'result': await manager.call_tool(name, arguments or {})}

    @mcp.tool(annotations=write, description=(
        'Run any tool of this connector by name, including tools and parameters added after your tool list '
        'was captured. arguments are that tool\'s parameters (see list_tools_live). Every rule of the target '
        'tool applies unchanged: authorization from the owner for the exact chat and action, expected_chat, '
        'request_id, quotas.'))
    async def call_tool(name: str, arguments: dict | None = None) -> dict:
        target(name)
        return {'tool': name, 'result': await manager.call_tool(name, arguments or {})}

    return list_tools_live, call_read_tool, call_tool
