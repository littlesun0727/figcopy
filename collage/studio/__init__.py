"""Load local browser entrypoints without importing the workflow recursively."""

__all__ = ["serve_review_ui", "serve_workbench"]


def __getattr__(name):
    if name == "serve_review_ui":
        from .review_server import serve_review_ui

        return serve_review_ui
    if name == "serve_workbench":
        from .workbench import serve_workbench

        return serve_workbench
    raise AttributeError(name)
