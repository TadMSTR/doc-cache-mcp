"""doc-cache-mcp — capability-scoped FastMCP server for the forge docs cache.

Exposes five narrow verbs — list / add / update / remove / sync — over the shared
documentation cache, replacing the research agent's generic read_file/edit_file/run_command
grant (ADR-0005) with a purpose-built tool that classifies every source URL before it can
enter the cache (SSRF, cache-poisoning and content-trust policy). See SECURITY.md.
"""

from __future__ import annotations

__version__ = "0.4.0"
