"""Passerelle Qraft : API REST, flux SSE, export Arrow, serveur MCP."""

from .app import create_app, run

__all__ = ["create_app", "run"]
