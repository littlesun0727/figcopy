"""Coordinate creation and resumption of durable Figcopy workflows."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..core.errors import CollageError
from ..core.io import (
    atomic_save_image,
    atomic_write_json,
    decode_image,
    read_json,
)
from ..imaging.operations import normalize_image
from ..projects import ProjectPaths, ProjectStore
from .inputs import import_bindings
from .model import (
    DEFAULT_CUTOUT_PROVIDER,
    DEFAULT_IMAGE_PROVIDER,
    DEFAULT_VISION_PROVIDER,
    new_workflow,
    project_status_for_stage,
    validate_workflow,
)
from .stages import WorkflowStages
from .state import (
    current_summary,
    infer_resume_stage,
    record_failure,
    summary,
    transition,
)

LOGGER = logging.getLogger(__name__)


class WorkflowService:
    """Run and resume the durable workflow stored in one project manifest."""

    def __init__(self, store: ProjectStore):
        self.store = store
        self.stages = WorkflowStages(store)

    def start(
        self,
        project_id: str,
        reference_path: Path,
        *,
        reviewer: str,
        name: str | None = None,
        manual_draft_path: Path | None = None,
        product_policy_path: Path | None = None,
        background_candidate_path: Path | None = None,
        initial_mask_path: Path | None = None,
        allowed_mask_path: Path | None = None,
        bindings_path: Path | None = None,
        vision_provider_spec: str | None = None,
        image_provider_spec: str | None = None,
        cutout_provider_spec: str | None = None,
        fixture_provider: bool = False,
        allow_cloud_upload: bool = False,
        review_port: int = 8765,
        open_review: bool = True,
    ) -> dict[str, Any]:
        """Create a project, import inputs, and advance until a human gate."""

        # Decode external inputs before creating a persistent project whenever possible.
        reference = normalize_image(reference_path.resolve())
        manual_draft = (
            read_json(manual_draft_path.resolve()) if manual_draft_path else None
        )
        product_policy = (
            read_json(product_policy_path.resolve()) if product_policy_path else None
        )
        background_candidate = (
            normalize_image(background_candidate_path.resolve())
            if background_candidate_path
            else None
        )
        initial_mask = (
            decode_image(initial_mask_path.resolve(), mode="L")
            if initial_mask_path
            else None
        )
        allowed_mask = (
            decode_image(allowed_mask_path.resolve(), mode="L")
            if allowed_mask_path
            else None
        )

        vision_provider = (
            None
            if manual_draft_path is not None
            else vision_provider_spec or DEFAULT_VISION_PROVIDER
        )
        image_provider = (
            None if fixture_provider else image_provider_spec or DEFAULT_IMAGE_PROVIDER
        )
        cutout_provider = cutout_provider_spec or DEFAULT_CUTOUT_PROVIDER
        workflow = new_workflow(
            reviewer=reviewer,
            vision_provider=vision_provider,
            image_provider=image_provider,
            cutout_provider=cutout_provider,
            fixture_provider=fixture_provider,
            allow_cloud_upload=allow_cloud_upload,
            review_port=review_port,
        )
        project = self.store.create(project_id, name=name)
        self.store.update_workflow(
            project_id,
            workflow,
            status=project_status_for_stage(workflow["stage"]),
        )
        try:
            atomic_save_image(reference, project.inputs / "reference.png")
            if manual_draft is not None:
                atomic_write_json(project.inputs / "manual_draft.json", manual_draft)
            if product_policy is not None:
                atomic_write_json(
                    project.inputs / "product_policy.json", product_policy
                )
            if background_candidate is not None:
                atomic_save_image(
                    background_candidate,
                    project.inputs / "background_candidate.png",
                )
            if initial_mask is not None:
                atomic_save_image(
                    initial_mask, project.inputs / "initial_remove_mask.png"
                )
            if allowed_mask is not None:
                atomic_save_image(allowed_mask, project.inputs / "allowed_mask.png")
            if bindings_path is not None:
                import_bindings(
                    bindings_path,
                    project.renders / "bindings.json",
                    project,
                )
            return self._advance(
                project,
                workflow,
                open_review=open_review,
                approve=False,
                approval_notes="",
                allow_fixture_approval=False,
            )
        except CollageError as exc:
            record_failure(self.store, project, workflow, exc)
            raise
        except Exception as exc:
            LOGGER.exception("工作流发生未预期错误 | project=%s", project_id)
            error = CollageError(
                "WORKFLOW_FAILED",
                f"工作流发生未预期错误：{type(exc).__name__}",
            )
            record_failure(self.store, project, workflow, error)
            raise error from exc

    def resume(
        self,
        project_id: str,
        *,
        bindings_path: Path | None = None,
        background_candidate_path: Path | None = None,
        initial_mask_path: Path | None = None,
        allowed_mask_path: Path | None = None,
        reviewer: str | None = None,
        vision_provider_spec: str | None = None,
        image_provider_spec: str | None = None,
        cutout_provider_spec: str | None = None,
        fixture_provider: bool | None = None,
        allow_cloud_upload: bool | None = None,
        review_port: int | None = None,
        open_review: bool = True,
        approve: bool = False,
        approval_notes: str = "",
        allow_fixture_approval: bool = False,
    ) -> dict[str, Any]:
        """Apply optional overrides and continue an existing project."""

        background_candidate = (
            normalize_image(background_candidate_path.resolve())
            if background_candidate_path
            else None
        )
        initial_mask = (
            decode_image(initial_mask_path.resolve(), mode="L")
            if initial_mask_path
            else None
        )
        allowed_mask = (
            decode_image(allowed_mask_path.resolve(), mode="L")
            if allowed_mask_path
            else None
        )
        project = self.store.open(project_id)
        manifest = self.store.get_manifest(project_id)
        workflow = validate_workflow(manifest.get("workflow"))
        options = workflow["options"]
        if reviewer is not None:
            options["reviewer"] = reviewer
        if vision_provider_spec is not None:
            options["vision_provider"] = vision_provider_spec
        if image_provider_spec is not None:
            options["image_provider"] = image_provider_spec
            options["fixture_provider"] = False
        if cutout_provider_spec is not None:
            options["cutout_provider"] = cutout_provider_spec
        if fixture_provider is True:
            options["fixture_provider"] = True
            options["image_provider"] = None
        if allow_cloud_upload is not None:
            options["allow_cloud_upload"] = allow_cloud_upload
        if review_port is not None:
            options["review_port"] = review_port
        validate_workflow(workflow)

        if workflow["stage"] in {"blocked", "failed"}:
            resume_stage = workflow.get("resume_stage") or infer_resume_stage(project)
            transition(self.store, project, workflow, resume_stage)
        if approve and workflow["stage"] not in {"awaiting_approval", "complete"}:
            raise CollageError(
                "APPROVAL_NOT_READY",
                "必须先生成并查看 renders/result.png，之后才能显式批准",
                details={"stage": workflow["stage"]},
            )
        if bindings_path is not None and workflow["stage"] == "complete":
            raise CollageError(
                "INVALID_STATE",
                "已发布项目不能改写验收 Bindings；请新建一次客户渲染",
            )
        replacement_images = (background_candidate, initial_mask, allowed_mask)
        if any(image is not None for image in replacement_images) and (
            workflow["stage"] not in {"analyzing", "awaiting_review"}
            or (project.review / "reviewed.json").is_file()
        ):
            raise CollageError(
                "INVALID_STATE",
                "背景候选和审核蒙版只能在 ReviewedSpec 保存前替换",
            )

        self.store.update_workflow(
            project_id,
            workflow,
            status=project_status_for_stage(workflow["stage"]),
        )
        try:
            if any(image is not None for image in replacement_images):
                if background_candidate is not None:
                    atomic_save_image(
                        background_candidate,
                        project.inputs / "background_candidate.png",
                    )
                if initial_mask is not None:
                    atomic_save_image(
                        initial_mask,
                        project.inputs / "initial_remove_mask.png",
                    )
                if allowed_mask is not None:
                    atomic_save_image(
                        allowed_mask,
                        project.inputs / "allowed_mask.png",
                    )
            if bindings_path is not None:
                import_bindings(
                    bindings_path,
                    project.renders / "bindings.json",
                    project,
                )
                if workflow["stage"] == "awaiting_approval":
                    # A changed composition invalidates the previous preview.
                    transition(self.store, project, workflow, "awaiting_bindings")
            return self._advance(
                project,
                workflow,
                open_review=open_review,
                approve=approve,
                approval_notes=approval_notes,
                allow_fixture_approval=allow_fixture_approval,
            )
        except CollageError as exc:
            record_failure(self.store, project, workflow, exc)
            raise
        except Exception as exc:
            LOGGER.exception("工作流恢复时发生未预期错误 | project=%s", project_id)
            error = CollageError(
                "WORKFLOW_FAILED",
                f"工作流发生未预期错误：{type(exc).__name__}",
            )
            record_failure(self.store, project, workflow, error)
            raise error from exc

    def status(self, project_id: str) -> dict[str, Any]:
        """Return a frontend-friendly snapshot without reading customer image bytes."""

        project = self.store.open(project_id)
        manifest = self.store.get_manifest(project_id)
        raw_workflow = manifest.get("workflow")
        if raw_workflow is None:
            return {
                "project_id": project_id,
                "name": manifest["name"],
                "status": manifest["status"],
                "stage": "not_initialized",
                "root": str(project.root),
                "next_action": "该项目由旧版创建，尚未初始化统一工作流",
                "wait": None,
                "last_error": None,
                "artifacts": {},
            }
        workflow = validate_workflow(raw_workflow)
        return summary(project, manifest, workflow)

    def _advance(
        self,
        project: ProjectPaths,
        workflow: dict[str, Any],
        *,
        open_review: bool,
        approve: bool,
        approval_notes: str,
        allow_fixture_approval: bool,
    ) -> dict[str, Any]:
        for _ in range(12):
            stage = workflow["stage"]
            if stage == "analyzing":
                self.stages.analyze(project, workflow)
                continue
            if stage == "awaiting_review":
                if not self.stages.review(project, workflow, open_review=open_review):
                    return current_summary(self.store, project, workflow)
                continue
            if stage in {"reviewed", "building"}:
                self.stages.build(project, workflow)
                continue
            if stage == "awaiting_bindings":
                if not self.stages.prepare_bindings(project, workflow):
                    return current_summary(self.store, project, workflow)
                continue
            if stage == "rendering":
                self.stages.render(project, workflow)
                continue
            if stage == "awaiting_approval":
                if not self.stages.approve(
                    project,
                    workflow,
                    approve=approve,
                    notes=approval_notes,
                    allow_fixture=allow_fixture_approval,
                ):
                    return current_summary(self.store, project, workflow)
                continue
            if stage == "complete":
                return current_summary(self.store, project, workflow)
            raise CollageError(
                "INVALID_WORKFLOW_STATE",
                f"无法推进工作流阶段：{stage}",
            )
        raise CollageError("WORKFLOW_LOOP", "工作流阶段推进次数超过安全限制")
