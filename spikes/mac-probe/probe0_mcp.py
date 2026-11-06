"""Probe 0: does the Mac StudioMCP answer a plain MCP client? Lists tools and running Studios."""

import json

from macmcp import MacStudioMcp

mcp = MacStudioMcp()
try:
    tools = mcp.request("tools/list", {})["tools"]
    print("tools:", ", ".join(t["name"] for t in tools))
    print("studios:", json.dumps(mcp.list_studios(), indent=2))
finally:
    mcp.close()
