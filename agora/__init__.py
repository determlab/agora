"""Agora — a meeting room shared by a human chair and any number of agents."""

#: The single source of truth for Agora's version (D9). Everything that reports
#: a version — the MCP `serverInfo`, the HTTP `Server:` header, `/api/state`,
#: the page, and the container image tag — reads it from here. It is declared in
#: a module that imports nothing so that importing it can never cost anything.
#:
#: 0.3.0 rather than 0.1.0 or 0.2: those two numbers disagreed for the whole of
#: this repo's life and neither was ever released, so adopting either would
#: claim a release that did not happen. This is the first version that is one
#: number in every place that shows one.
__version__ = "0.3.0"
