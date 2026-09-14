"""Verify photo attachments through structure review, rendering, revision and recovery."""

import base64
import io
import json

import pytest
from PIL import Image, ImageDraw

from collage.core.errors import CollageError, SpecValidationError
from collage.core.io import atomic_save_image, atomic_write_json, read_json, sha256_file
from collage.providers import ProviderAudit
from collage.projects import DataPaths
from collage.schemas import validate_draft
from collage.studio.workbench.application import WorkbenchApplication
from collage.studio.workbench.multipart import MultipartForm, UploadedFile
from collage.template.layout import compile_layers
from collage.template.validation import validate_package
from test_candidate_workflow import CandidateProvider
from test_workbench import _create_form, _image_bytes, _review_payload, _wait_for_job


def attachment_draft(*, generated=False, photo_background=False):
    """A rear photo overlaps a front photo; two decorations remain independent."""

    def slot(identifier, rect):
        return {
            "id": identifier,
            "label": identifier,
            "type": "image",
            "mode": "photo",
            "source_rect": rect,
            "target_rect": rect,
            "upload_hint": "照片",
            "review_notes": "",
        }

    def overlay(identifier, rect, shape, owner=None, position="above"):
        return {
            "id": identifier,
            "label": identifier,
            "source_rect": rect,
            "target_rect": rect,
            "attachment": {"slot_id": owner, "position": position} if owner else None,
            "action": "basic_shape",
            "shape": shape,
            "generation_brief": "",
            "requires_exact_content": False,
            "review_notes": "",
        }

    frame = {
        "kind": "dashed_rectangle",
        "outline": "#FFFFFF",
        "width": 3,
        "dash": 8,
        "gap": 4,
    }
    slots = [slot("back", [20, 45, 100, 100]), slot("front", [75, 20, 100, 90])]
    overlays = [
        overlay(
            "mat",
            [16, 41, 108, 108],
            {"kind": "rectangle", "fill": "#222222", "outline": None, "width": 0},
            "back",
            "below",
        ),
        overlay("frame_back", [20, 45, 100, 100], frame, "back"),
        overlay("frame_front", [75, 20, 100, 90], frame, "front"),
        overlay(
            "middle",
            [100, 80, 20, 20],
            {"kind": "ellipse", "fill": "#FF00FF", "outline": None, "width": 0},
        ),
        overlay(
            "top",
            [140, 115, 20, 20],
            {"kind": "ellipse", "fill": "#00FFFF", "outline": None, "width": 0},
        ),
    ]
    if generated:
        ribbon = overlay("ribbon", [24, 49, 30, 12], None, "back")
        ribbon.update(action="reference_generate", generation_brief="first ribbon")
        overlays.append(ribbon)
    background = {"background_brief": "固定底板", "review_notes": ""}
    first = {"type": "background"}
    if photo_background:
        slots.insert(0, slot("base", [0, 0, 260, 220]))
        background = {"mode": "slot", "slot_id": "base", "review_notes": ""}
        first = {"type": "slot", "id": "base"}
    return {
        "slots": slots,
        "overlays": overlays,
        "background": background,
        "layer_order": [
            first,
            {"type": "slot", "id": "back"},
            {"type": "overlay", "id": "middle"},
            {"type": "slot", "id": "front"},
            {"type": "overlay", "id": "top"},
        ],
        "questions": [],
    }


def attachment_project(
    tmp_path,
    monkeypatch,
    *,
    generated=False,
    photo_background=False,
    confirm=True,
    clip=False,
):
    app = WorkbenchApplication(DataPaths.resolve(tmp_path / "data"))
    provider = CandidateProvider(broken=generated)
    monkeypatch.setattr(app.workflow.stages, "_image_provider", lambda *_: provider)
    form = _create_form("attachment")
    form.files["reference"] = UploadedFile(
        "ref.png", "image/png", _image_bytes("#BBBBBB", size=(260, 220))
    )
    form.files["background_candidate"] = UploadedFile(
        "base.png", "image/png", _image_bytes("#DDCCBB", size=(260, 220))
    )
    form.files["manual_draft"] = UploadedFile(
        "draft.json",
        "application/json",
        json.dumps(
            attachment_draft(generated=generated, photo_background=photo_background)
        ).encode(),
    )
    app.start_project(form)
    _wait_for_job(app, "attachment")
    if confirm:
        payload = _review_payload(app, "attachment")
        if clip:
            mask = Image.new("L", (100, 100), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, 99, 99), fill=255)
            atomic_save_image(
                mask, app.store.open("attachment").review / "back-mask.png"
            )
            payload["slot_overrides"]["back"]["clip_mask"] = "back-mask.png"
        app.save_review("attachment", payload)
        _wait_for_job(app, "attachment")
        colors = {"back": "#C03040", "front": "#10A050"}
        if photo_background:
            colors["base"] = "#DDCCBB"
        app.submit_bindings(
            "attachment",
            MultipartForm(
                fields={"configuration": json.dumps({"slots": {}})},
                files={
                    "image." + identifier: UploadedFile(
                        identifier + ".png",
                        "image/png",
                        _image_bytes(color, size=(200, 200)),
                    )
                    for identifier, color in colors.items()
                },
            ),
        )
        _wait_for_job(app, "attachment")
    return app, provider, app.store.open("attachment")


@pytest.mark.parametrize("photo_background", [False, True])
def test_occlusion_independent_decoration_and_photo_crop(
    tmp_path, monkeypatch, photo_background
):
    app, provider, project = attachment_project(
        tmp_path, monkeypatch, photo_background=photo_background
    )
    template = validate_package(project.template, require_ready=False)
    assert "layers" not in template
    assert (
        next(item for item in template["overlays"] if item["id"] == "middle")[
            "attachment"
        ]
        is None
    )
    result = Image.open(project.renders / "result.png").convert("RGB")
    assert result.getpixel((95, 45)) == (
        16,
        160,
        80,
    )  # rear border is behind the front photo
    assert result.getpixel((45, 45)) == (
        255,
        255,
        255,
    )  # exposed rear border remains visible
    assert result.getpixel((110, 90)) == (
        16,
        160,
        80,
    )  # independent middle decoration is occluded
    assert result.getpixel((150, 125)) == (
        0,
        255,
        255,
    )  # independent top decoration remains visible
    assert result.getpixel((17, 80)) == (
        34,
        34,
        34,
    )  # below attachment extends outside its photo
    result.save(tmp_path / "occlusion.png")
    manifest = sha256_file(project.template / "template.json")
    app.submit_bindings(
        "attachment",
        MultipartForm(
            fields={
                "configuration": json.dumps(
                    {"slots": {"back": {"scale": 1.3, "offset_px": [3, -2]}}}
                )
            },
            files={},
        ),
    )
    _wait_for_job(app, "attachment")
    assert sha256_file(project.template / "template.json") == manifest
    assert provider.calls == []


def test_transform_save_then_restore_missing_attachment(tmp_path, monkeypatch):
    app, provider, original = attachment_project(tmp_path, monkeypatch, generated=True)
    before = {
        name: sha256_file(original.root / name)
        for name in ("template/template.json", "review/reviewed.json")
    }
    document = app.edit_layout(
        "attachment",
        {
            **app.layout("attachment"),
            "change": {
                "id": "slot:back",
                "rect": [30, 60, 150, 150],
                "rotation_deg": 90,
            },
        },
    )
    items = {item["id"]: item for item in document["items"]}
    assert items["asset:frame_back"]["rect"] == pytest.approx([30, 60, 150, 150])
    assert items["asset:mat"]["rect"] == pytest.approx([24, 54, 162, 162])
    assert items["asset:ribbon"]["rect"] == pytest.approx([142.5, 79.5, 45, 18])
    assert items["asset:ribbon"]["missing"]
    assert items["asset:ribbon"]["rotation_deg"] == 90
    assert items["asset:top"]["rect"] == [140, 115, 20, 20]
    document = app.edit_layout(
        "attachment",
        {
            **document,
            "root_order": [
                "asset:bg",
                "asset:middle",
                "slot:front",
                "slot:back",
                "asset:top",
            ],
        },
    )
    expected_preview = Image.open(
        io.BytesIO(app.preview_layout("attachment", document))
    ).tobytes()
    result = app.save_layout("attachment", document)
    moved = app.store.open(result["project_id"])
    assert Image.open(moved.renders / "result.png").tobytes() == expected_preview
    assert len(provider.calls) == 1
    reviewed = read_json(moved.review / "reviewed.json")
    ribbon = next(item for item in reviewed["overlays"] if item["id"] == "ribbon")
    assert ribbon["target_rect"] == pytest.approx([142.5, 79.5, 45, 18])
    provider.broken = False
    app.regenerate_overlay(
        moved.project_id,
        {"overlay_id": "ribbon", "revision": app.layout(moved.project_id)["revision"]},
    )
    job = _wait_for_job(app, moved.project_id)
    restored = app.store.open(job["result"]["project_id"])
    template = validate_package(restored.template, require_ready=False)
    ribbon = next(item for item in template["overlays"] if item["id"] == "ribbon")
    assert ribbon["rect"] == pytest.approx([142.5, 79.5, 45, 18])
    order = [
        item.get("slot_id", item.get("asset_id")) for item in compile_layers(template)
    ]
    assert order == [
        "bg",
        "middle",
        "front",
        "frame_front",
        "mat",
        "back",
        "frame_back",
        "ribbon",
        "top",
    ]
    assert len(provider.calls) == 2
    assert not template["build"]["warnings"]
    for name, fingerprint in before.items():
        assert sha256_file(original.root / name) == fingerprint
    assert sha256_file(moved.template / "assets/overlay_top.png") == sha256_file(
        restored.template / "assets/overlay_top.png"
    )
    # A background revision also preserves the edited attachment layout.
    fork = app.fork_background(
        restored.project_id, app.background_revision(restored.project_id)
    )
    session = app.review_session(fork["project_id"])
    assert (
        next(item for item in session.draft["overlays"] if item["id"] == "ribbon")[
            "target_rect"
        ]
        == ribbon["rect"]
    )
    assert session.draft["layer_order"] == template["layer_order"]


def test_review_edits_attachment_and_geometry_without_changing_source(
    tmp_path, monkeypatch
):
    app, provider, project = attachment_project(tmp_path, monkeypatch, confirm=False)
    session = app.review_session("attachment")
    original = sha256_file(session.draft_path)
    payload = _review_payload(app, "attachment")
    response = session.layout(
        {
            **payload,
            "change": {
                "kind": "slot",
                "id": "back",
                "rect": [30, 55, 100, 100],
                "rotation_deg": 90,
            },
        }
    )
    items = {item["id"]: item for item in response["draft"]["overlays"]}
    assert items["frame_back"]["target_rect"] == pytest.approx([30, 55, 100, 100])
    assert response["review_options"]["overlays"]["frame_back"]["rotation_deg"] == 90
    payload.update(
        draft=response["draft"],
        slot_overrides=response["review_options"]["slots"],
        overlay_overrides=response["review_options"]["overlays"],
    )
    response = session.layout(
        {
            **payload,
            "change": {"kind": "overlay", "id": "frame_back", "attachment": None},
        }
    )
    assert {"type": "overlay", "id": "frame_back"} in response["draft"]["layer_order"]
    payload["draft"] = response["draft"]
    response = session.layout(
        {
            **payload,
            "change": {
                "kind": "overlay",
                "id": "frame_back",
                "attachment": {"slot_id": "front", "position": "below"},
            },
        }
    )
    assert {"type": "overlay", "id": "frame_back"} not in response["draft"][
        "layer_order"
    ]
    assert response["draw_order"].index(
        {"type": "overlay", "id": "frame_back"}
    ) + 1 == response["draw_order"].index({"type": "slot", "id": "front"})
    raw = base64.b64decode(response["structure_preview"].split(",", 1)[1])
    Image.open(io.BytesIO(raw)).save(tmp_path / "structure.png")
    assert provider.calls == [] and sha256_file(session.draft_path) == original
    payload["draft"] = response["draft"]
    session.save(payload)
    reviewed = read_json(project.review / "reviewed.json")
    assert next(item for item in reviewed["overlays"] if item["id"] == "frame_back")[
        "attachment"
    ] == {"slot_id": "front", "position": "below"}


@pytest.mark.parametrize(
    "attachment",
    [
        {"slot_id": "missing", "position": "above"},
        {"slot_id": "base", "position": "above"},
        {"slot_id": "middle", "position": "above"},
        {"slot_id": "back", "position": "elsewhere"},
    ],
)
def test_invalid_attachment_is_rejected(attachment):
    draft = attachment_draft(photo_background=True)
    draft["overlays"][0]["attachment"] = attachment
    with pytest.raises(SpecValidationError):
        validate_draft(draft, require_metadata=False)


def test_old_protocol_and_nonuniform_group_scale_are_explicit(tmp_path, monkeypatch):
    app, _, project = attachment_project(tmp_path, monkeypatch)
    draft = read_json(project.analysis / "draft.json")
    draft["version"] = "collage-draft/2"
    with pytest.raises(SpecValidationError) as error:
        validate_draft(draft)
    assert any(issue.code == "UNSUPPORTED_SPEC_VERSION" for issue in error.value.issues)
    with pytest.raises(CollageError, match="等比"):
        app.edit_layout(
            "attachment",
            {
                **app.layout("attachment"),
                "change": {
                    "id": "slot:back",
                    "rect": [20, 45, 120, 100],
                    "rotation_deg": 0,
                },
            },
        )


def test_group_resize_with_slot_mask_matches_saved_render(tmp_path, monkeypatch):
    app, _, project = attachment_project(tmp_path, monkeypatch, clip=True)
    mask_hash = sha256_file(project.template / "masks/back_clip.png")
    document = app.edit_layout(
        "attachment",
        {
            **app.layout("attachment"),
            "change": {
                "id": "slot:back",
                "rect": [20, 45, 150, 150],
                "rotation_deg": 90,
            },
        },
    )
    preview = Image.open(
        io.BytesIO(app.preview_layout("attachment", document))
    ).convert("RGB")
    result = app.save_layout("attachment", document)
    target = app.store.open(result["project_id"])
    rendered = Image.open(target.renders / "result.png").convert("RGB")
    assert rendered.tobytes() == preview.tobytes()
    assert rendered.getpixel((30, 55)) == (34, 34, 34)
    assert rendered.getpixel((95, 120)) == (192, 48, 64)
    assert sha256_file(target.template / "masks/back_clip.png") == mask_hash


def test_feedback_preserves_relations_and_excludes_source_audit(tmp_path, monkeypatch):
    from collage.template.review.feedback import revise_draft, review_revision

    app, _, project = attachment_project(tmp_path, monkeypatch, confirm=False)
    path = project.analysis / "draft.json"
    current = read_json(path)
    current["provider"]["request_id"] = "fixture-private-audit-marker"
    atomic_write_json(path, current)

    class Vision:
        name = "attachment-fixture"
        requested_model = "fixture"
        fixture = True

        def __init__(self):
            self.prompts = []

        def analyze(self, image, **kwargs):
            self.prompts.append(kwargs["prompt"])
            corrected = attachment_draft()
            corrected["layer_order"] = [
                {"type": "background"},
                {"type": "slot", "id": "front"},
                {"type": "slot", "id": "back"},
                {"type": "overlay", "id": "middle"},
                {"type": "overlay", "id": "top"},
            ]
            return corrected, ProviderAudit(
                self.name, "fixture", "fixture", None, True, 0
            )

    provider = Vision()
    revise_draft(
        path,
        {
            "draft": current,
            "revision": review_revision(current),
            "other_feedback": "左下照片放在右上照片前面",
        },
        provider=provider,
    )
    corrected = read_json(path)
    assert len(provider.prompts) == 1
    assert "fixture-private-audit-marker" not in provider.prompts[0]
    assert (
        next(item for item in corrected["overlays"] if item["id"] == "frame_back")[
            "attachment"
        ]["slot_id"]
        == "back"
    )
    assert (
        next(item for item in corrected["overlays"] if item["id"] == "middle")[
            "attachment"
        ]
        is None
    )
    assert corrected["layer_order"][1] == {"type": "slot", "id": "front"}
