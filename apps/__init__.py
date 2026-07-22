"""UI adapters for `selayer_chat`.

Each module here is a thin wrapper that exposes the backend's
`AnalyticalBackend` through a different chat-style UI framework. They
share rendering logic in `_common.py`.
"""

from __future__ import annotations

__all__ = ["streamlit_app", "gradio_app", "panel_app", "marimo_app"]

