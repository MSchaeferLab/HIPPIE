"""MCP server for HIPPIE.

Exposes the protein-interaction database to MCP hosts over streamable HTTP. The
tools are thin adapters over ``hippie_website.services``, so an agent-issued
query means exactly what the same query means on the website.

Entry point: ``hippie_mcp.asgi:app`` (see ``asgi.py``). Do not import this
package before ``django.setup()`` has run.
"""
