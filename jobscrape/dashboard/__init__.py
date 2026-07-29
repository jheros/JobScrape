"""Local management dashboard: browse jobs, drive the pipeline, watch it run."""

from __future__ import annotations


def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    """Start the web UI. Imported lazily so the CLI works without FastAPI."""
    from .app import serve as _serve

    _serve(host=host, port=port, reload=reload)


__all__ = ["serve"]
