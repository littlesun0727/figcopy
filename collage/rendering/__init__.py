"""Prepare customer bindings, render templates, and create cutouts."""

from .composer import render_from_files, render_template
from .cutout import prepare_cutout

__all__ = ["prepare_cutout", "render_from_files", "render_template"]
