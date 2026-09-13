"""Verify customer-driven correction, stale-review protection and request recovery."""

import base64
import copy
import io

import pytest
from PIL import Image

from collage.core.errors import CollageError
from collage.core.io import atomic_write_json, read_json
from collage.providers import ProviderAudit
from collage.studio.review_session import ReviewSession
from collage.template.analysis import analyze_reference
from collage.template.review.feedback import (
    revise_draft,
    review_revision,
    validate_feedback,
)

CORE = {
    "slots": [],
    "overlays": [],
    "background": {"background_brief": "plain", "review_notes": ""},
    "layer_order": [{"type": "background"}],
    "questions": ["有几张照片？"],
}


def _draft(tmp_path):
    Image.new("RGB", (80, 60), "gray").save(tmp_path / "source.png")
    atomic_write_json(tmp_path / "manual.json", CORE)
    return analyze_reference(
        tmp_path / "source.png",
        tmp_path / "analysis",
        manual_draft_path=tmp_path / "manual.json",
    )


class Vision:
    name = "fixture-questions"
    requested_model = "fixture"
    fixture = True

    def __init__(self, result=None, error=None):
        self.result = result if result is not None else {**CORE, "questions": []}
        self.error = error
        self.calls = []

    def analyze(self, reference_bytes, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return copy.deepcopy(self.result), ProviderAudit(
            self.name, "fixture", "fixture", "test", True, 1
        )


def _feedback(path, **changes):
    draft = read_json(path)
    return {
        "revision": review_revision(draft),
        "question_resolutions": [
            {"question": question, "answer": "四张"} for question in draft["questions"]
        ],
        **changes,
    }


def _save_payload(session):
    stream = io.BytesIO()
    Image.new("RGBA", session.canvas_size, "white").save(stream, format="PNG")
    return {
        "draft": copy.deepcopy(session.draft),
        "revision": review_revision(session.draft),
        "final_confirmed": True,
        "mask_png": "data:image/png;base64,"
        + base64.b64encode(stream.getvalue()).decode(),
    }


def test_feedback_requires_answers_and_binds_confirmation_to_new_revision(tmp_path):
    path = _draft(tmp_path)
    old_session = ReviewSession(path, tmp_path / "reviewed.json", reviewer="tester")
    provider = Vision()
    with pytest.raises(CollageError) as empty:
        revise_draft(path, _feedback(path, question_resolutions=[]), provider=provider)
    assert empty.value.code == "REVIEW_FEEDBACK_EMPTY"
    assert not provider.calls
    payload = _feedback(path, other_feedback="右上角还少了星星")
    revise_draft(path, payload, provider=provider)
    assert (
        "四张" in provider.calls[0]["prompt"]
        and "右上角还少了星星" in provider.calls[0]["prompt"]
    )
    assert read_json(path)["questions"] == []
    evidence = read_json(path.parent / "review_feedback.json")
    assert evidence["other_feedback"] == "右上角还少了星星"
    with pytest.raises(CollageError) as stale:
        old_session.save(_save_payload(old_session))
    assert stale.value.code == "REVIEW_REVISION_CONFLICT"
    session = ReviewSession(path, tmp_path / "reviewed.json", reviewer="tester")
    final = _save_payload(session)
    final["final_confirmed"] = False
    with pytest.raises(CollageError) as unconfirmed:
        session.save(final)
    assert unconfirmed.value.code == "HUMAN_REVIEW_REQUIRED"
    final["final_confirmed"] = True
    session.save(final)
    assert read_json(tmp_path / "confirmation.json")["revision"] == review_revision(
        session.draft
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"question_resolutions": [{"question": "有几张照片？", "answer": "四张"}]},
        {"other_feedback": "缺少照片"},
        {"defer_questions": True, "other_feedback": "缺少照片"},
    ],
)
def test_written_feedback_cannot_bypass_correction(tmp_path, extra):
    path = _draft(tmp_path)
    session = ReviewSession(path, tmp_path / "reviewed.json", reviewer="tester")
    payload = _save_payload(session)
    payload["draft"]["questions"] = []
    payload.update(extra)
    with pytest.raises(CollageError) as caught:
        session.save(payload)
    assert caught.value.code == "REVIEW_CORRECTION_REQUIRED"
    assert not (tmp_path / "reviewed.json").exists()


@pytest.mark.parametrize(
    "answers", [[], [{"question": CORE["questions"][0], "answer": " \n "}]]
)
def test_empty_feedback_accepts_current_draft_and_preserves_original_questions(
    tmp_path, answers
):
    path = _draft(tmp_path)
    original = path.read_bytes()
    session = ReviewSession(path, tmp_path / "reviewed.json", reviewer="tester")
    payload = _save_payload(session)
    payload.update(question_resolutions=answers, other_feedback=" \n ")
    # A client's removal of questions must not remove the original evidence.
    payload["draft"]["questions"] = []
    session.save(payload)
    assert path.read_bytes() == original
    assert not (path.parent / "review_feedback.json").exists()
    assert (
        read_json(tmp_path / "confirmation.json")["questions_accepted_as_is"]
        == CORE["questions"]
    )
    reviewed = read_json(tmp_path / "reviewed.json")
    assert CORE["questions"][0] in reviewed["review"]["notes"]
    assert "未调用 VLM 纠正" in reviewed["review"]["notes"]


def test_accepting_defaults_still_requires_overall_confirmation(tmp_path):
    path = _draft(tmp_path)
    session = ReviewSession(path, tmp_path / "reviewed.json", reviewer="tester")
    payload = _save_payload(session)
    payload["final_confirmed"] = False
    with pytest.raises(CollageError) as caught:
        session.save(payload)
    assert caught.value.code == "HUMAN_REVIEW_REQUIRED"
    assert not (tmp_path / "reviewed.json").exists()


def _text_session(tmp_path):
    path = _draft(tmp_path)
    draft = read_json(path)
    draft["overlays"] = [
        {
            "id": "lettering",
            "label": "手写文字",
            "source_rect": [5, 5, 40, 20],
            "target_rect": [5, 5, 40, 20],
            "action": "reference_generate",
            "generation_brief": "保留完整 hello",
            "requires_exact_content": False,
            "review_notes": "fixture",
            "text_content": "hello",
        }
    ]
    draft["layer_order"].append({"type": "overlay", "id": "lettering"})
    atomic_write_json(path, draft)
    session = ReviewSession(path, tmp_path / "reviewed.json", reviewer="tester")
    payload = _save_payload(session)
    payload["overlay_overrides"] = copy.deepcopy(session.review_options["overlays"])
    payload["overlay_text_review_opened"] = False
    return session, payload


def test_unopened_text_uses_overall_acceptance_with_distinct_evidence(tmp_path):
    session, payload = _text_session(tmp_path)
    assert payload["overlay_overrides"]["lettering"]["text_confirmed"] is False
    session.save(payload)
    reviewed = read_json(tmp_path / "reviewed.json")
    assert reviewed["overlays"][0]["text_content"] == "hello"
    assert reviewed["overlays"][0]["text_confirmed"] is True
    assert "未逐字核对：lettering" in reviewed["review"]["notes"]
    assert read_json(tmp_path / "confirmation.json")["default_text_accepted"] == [
        "lettering"
    ]


@pytest.mark.parametrize(
    "change", ["opened", "edited", "cleared", "edited_draft", "legacy_client"]
)
def test_opened_or_changed_text_still_needs_explicit_confirmation(tmp_path, change):
    session, payload = _text_session(tmp_path)
    fields = payload["overlay_overrides"]["lettering"]
    if change == "opened":
        payload["overlay_text_review_opened"] = True
    elif change == "edited":
        fields["text_content"] = "goodbye"
    elif change == "cleared":
        fields["text_content"] = None
    elif change == "edited_draft":
        payload["draft"]["overlays"][0]["text_content"] = "goodbye"
    else:
        payload.pop("overlay_text_review_opened")
    with pytest.raises(CollageError) as caught:
        session.save(payload)
    assert caught.value.code == "HUMAN_REVIEW_REQUIRED"
    assert not (tmp_path / "reviewed.json").exists()


def test_explicit_text_confirmation_records_no_default_acceptance(tmp_path):
    session, payload = _text_session(tmp_path)
    payload["overlay_text_review_opened"] = True
    payload["overlay_overrides"]["lettering"].update(
        text_content="goodbye", text_confirmed=True
    )
    session.save(payload)
    assert (
        read_json(tmp_path / "reviewed.json")["overlays"][0]["text_content"]
        == "goodbye"
    )
    assert read_json(tmp_path / "confirmation.json")["default_text_accepted"] == []


def test_partial_answers_and_other_feedback_are_valid_corrections(tmp_path):
    path = _draft(tmp_path)
    draft = read_json(path)
    draft["questions"].append("是否保留装饰？")
    atomic_write_json(path, draft)
    payload = _feedback(path)
    payload["question_resolutions"][1]["answer"] = " "
    validated = validate_feedback(draft, payload)
    assert validated["question_resolutions"] == [
        {"question": "有几张照片？", "answer": "四张"}
    ]
    provider = Vision()
    revise_draft(
        path,
        _feedback(path, question_resolutions=[], other_feedback="保留当前照片数量"),
        provider=provider,
    )
    assert len(provider.calls) == 1
    assert read_json(path.parent / "review_feedback.json")["question_resolutions"] == []


@pytest.mark.parametrize("invalid_result", [True, False])
def test_failed_correction_keeps_original_and_never_replays_identical_request(
    tmp_path, invalid_result
):
    path = _draft(tmp_path)
    before = path.read_bytes()
    provider = (
        Vision(result={"not_a_draft": True})
        if invalid_result
        else Vision(error=CollageError("PROVIDER_TIMEOUT", "timeout"))
    )
    payload = _feedback(path)
    with pytest.raises(CollageError):
        revise_draft(path, payload, provider=provider)
    assert path.read_bytes() == before
    with pytest.raises(CollageError) as duplicate:
        revise_draft(path, payload, provider=provider)
    assert duplicate.value.code == "REVIEW_REQUEST_ALREADY_ATTEMPTED"
    assert len(provider.calls) == 1


def test_optional_other_defaults_empty_and_correction_can_leave_new_questions(tmp_path):
    path = _draft(tmp_path)
    provider = Vision(result={**CORE, "questions": ["右上角是星星吗？"]})
    revise_draft(path, _feedback(path), provider=provider)
    assert read_json(path.parent / "review_feedback.json")["other_feedback"] == ""
    session = ReviewSession(path, tmp_path / "reviewed.json", reviewer="tester")
    session.save(_save_payload(session))
    assert read_json(tmp_path / "confirmation.json")["questions_accepted_as_is"] == [
        "右上角是星星吗？"
    ]


def test_workflow_imports_without_a_preloaded_studio():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "from collage.workflows import WorkflowService; from collage.studio import serve_workbench",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
