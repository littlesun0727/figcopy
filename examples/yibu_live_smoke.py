"""Run a small audited Yibu image-generation smoke test through the build pipeline.

The script creates a synthetic reference and removal mask, asks the configured
Yibu image model to fill the masked area, then writes a normal template artifact.
It never reads or prints the API key directly; provider configuration comes from
environment variables documented in USAGE_GUIDE.md.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from PIL import Image, ImageDraw

from collage.build import build_template
from collage.io_utils import atomic_save_image, atomic_write_json, sha256_file
from collage.providers.yibu import YibuImageProvider

LOGGER = logging.getLogger("yibu-live-smoke")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Call the Yibu image model through the local audit proxy."
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("artifacts/yibu_live_test/image"),
        help="Artifact directory for the smoke test.",
    )
    return parser.parse_args()


def _create_fixture(out_dir: Path) -> tuple[Path, Path]:
    """Create a deterministic reference and white-edit/black-protect mask."""
    inputs_dir = out_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)

    width, height = 384, 256
    reference = Image.new("RGB", (width, height))
    pixels = reference.load()
    for y in range(height):
        color = (82 + y // 8, 135 + y // 12, 178 + y // 16)
        for x in range(width):
            pixels[x, y] = color

    draw = ImageDraw.Draw(reference)
    card_box = (116, 62, 268, 194)
    draw.rounded_rectangle(card_box, radius=12, fill=(204, 57, 54))
    for center in ((151, 128), (192, 105), (231, 142)):
        x, y = center
        draw.ellipse((x - 10, y - 10, x + 10, y + 10), fill="white")

    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle(card_box, radius=12, fill=255)

    reference_path = inputs_dir / "reference.png"
    mask_path = inputs_dir / "remove_mask.png"
    atomic_save_image(reference, reference_path)
    atomic_save_image(mask, mask_path)
    return reference_path, mask_path


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info("Preparing audited image-model smoke test in %s", out_dir)

    reference_path, _mask_path = _create_fixture(out_dir)
    spec = {
        "version": "collage-reviewed/1",
        "status": "reviewed",
        "reference": {
            "path": "inputs/reference.png",
            "sha256": sha256_file(reference_path),
        },
        "canvas": {
            "width": 384,
            "height": 256,
            "coordinate_space": "canvas_px",
            "rect_format": "xywh",
        },
        "slots": [],
        "overlays": [],
        "background": {
            "background_brief": (
                "Replace the red panel with a wholly original hand-painted turquoise "
                "paper inlay containing three asymmetrical golden circles and subtle "
                "grain. Make the motif novel rather than copying any known artwork."
            ),
            "review_notes": "Synthetic image-model connectivity check.",
            "remove_mask": "inputs/remove_mask.png",
            "allowed_mask": None,
            "candidate_path": None,
            "expand_px": 0,
            "feather_px": 0,
        },
        "layer_order": [{"type": "background"}],
        "review": {
            "reviewer": "automated Yibu smoke test",
            "reviewed_at": "2026-09-10T00:00:00+08:00",
            "notes": "No API credential is stored in this artifact.",
            "questions_resolved": True,
        },
        "audit": {},
    }
    spec_path = out_dir / "reviewed.json"
    atomic_write_json(spec_path, spec)

    LOGGER.info("Calling the configured Yibu image provider through the audit proxy")
    manifest_path = build_template(
        spec_path,
        out_dir / "template",
        image_provider=YibuImageProvider(),
        force=True,
    )
    LOGGER.info(
        "Smoke test completed: status=needs_review manifest=%s",
        manifest_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
