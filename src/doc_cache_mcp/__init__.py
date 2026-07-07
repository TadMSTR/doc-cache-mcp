"""doc-cache-mcp — capability-scoped FastMCP server for the forge docs cache.

Exposes exactly three verbs — list / add / sync — over the shared documentation cache,
replacing the research agent's generic read_file/edit_file/run_command grant (ADR-0005)
with a purpose-built tool that also validates every source URL before it can enter the
trusted cache (SSRF / cache-poisoning guard). See SECURITY.md.
"""

from __future__ import annotations

__version__ = "0.1.0"
