"""Minimal stdio MCP server exposing the Jebat-Cortex tools.

Named `mcp` per MCP_SURFACE.md section 5 (`python3 -m mcp.server`). The upstream
`mcp` SDK is not installed in this runtime, so nothing is shadowed; if it is
ever added, run the server by path or rename this package to avoid a collision.
"""
