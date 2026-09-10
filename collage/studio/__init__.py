"""Local browser interfaces for interactive Figcopy work."""

from .review_server import serve_review_ui
from .workbench import serve_workbench

__all__ = ["serve_review_ui", "serve_workbench"]
