"""Orchestrate a resumable reference-to-render project workflow."""

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
from ..providers import (
    DeterministicFixtureImageProvider,
    ImageProvider,
    VisionProvider,
    load_provider,
)
from ..rendering import render_from_files
from ..schemas import validate_draft, validate_reviewed_spec
from ..studio.review_server import serve_review_ui
from ..template.analysis import analyze_reference
from ..template.build import approve_template, build_template
from ..template.validation import validate_package
from .bindings import (
    bindings_wait_reason,
    ensure_upload_files,
    prepare_cutout_bindings,
)
from .inputs import import_bindings
from .model import (
    ARTIFACT_PATHS,
    DEFAULT_CUTOUT_PROVIDER,
    DEFAULT_IMAGE_PROVIDER,
    DEFAULT_VISION_PROVIDER,
    new_workflow,
    project_status_for_stage,
    utc_now,
    validate_workflow,
)

LOGGER = logging.getLogger(__name__)

_BLOCKING_ERROR_CODES = {
    "CUSTOMER_UPLOAD_NOT_AUTHORIZED",
    "CUTOUT_PROVIDER_UNAVAILABLE",
    "EXACT_CONTENT_REQUIRED",
    "FIXTURE_APPROVAL_BLOCKED",
    "IMAGE_PROVIDER_CAPABILITY_MISSING",
    "IMAGE_PROVIDER_UNAVAILABLE",
    "VISION_PROVIDER_UNAVAILABLE",
}


def _is_blocking_error(code: str) -> bool:
    return code in _BLOCKING_ERROR_CODES or code.startswith(
        ("BIREFNET_", "PROVIDER_", "YIBU_")
    )


class WorkflowService:
    """Run and resume the durable workflow stored in one project manifest."""

    def __init__(self, store: ProjectStore):
        self.store = store

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
            self._record_failure(project, workflow, exc)
            raise
        except Exception as exc:
            LOGGER.exception("工作流发生未预期错误 | project=%s", project_id)
            error = CollageError(
                "WORKFLOW_FAILED",
                f"工作流发生未预期错误：{type(exc).__name__}",
            )
            self._record_failure(project, workflow, error)
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
            resume_stage = workflow.get("resume_stage") or self._infer_resume_stage(
                project
            )
            self._transition(project, workflow, resume_stage)
        if approve and workflow["stage"] not in {"awaiting_approval", "complete"}:
            raise CollageError(
                "APPROVAL_NOT_READY",
                "必须先生成并查看 renders/result.png，之后才能显式批准",
                details={"stage": workflow["stage"]},
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
            return self._advance(
                project,
                workflow,
                open_review=open_review,
                approve=approve,
                approval_notes=approval_notes,
                allow_fixture_approval=allow_fixture_approval,
            )
        except CollageError as exc:
            self._record_failure(project, workflow, exc)
            raise
        except Exception as exc:
            LOGGER.exception("工作流恢复时发生未预期错误 | project=%s", project_id)
            error = CollageError(
                "WORKFLOW_FAILED",
                f"工作流发生未预期错误：{type(exc).__name__}",
            )
            self._record_failure(project, workflow, error)
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
        return self._summary(project, manifest, workflow)

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
                self._analyze(project, workflow)
                continue
            if stage == "awaiting_review":
                if not self._review(project, workflow, open_review=open_review):
                    return self._current_summary(project, workflow)
                continue
            if stage in {"reviewed", "building"}:
                self._build(project, workflow)
                continue
            if stage == "awaiting_bindings":
                if not self._prepare_bindings(project, workflow):
                    return self._current_summary(project, workflow)
                continue
            if stage == "rendering":
                self._render(project, workflow)
                continue
            if stage == "awaiting_approval":
                if not self._approve(
                    project,
                    workflow,
                    approve=approve,
                    notes=approval_notes,
                    allow_fixture=allow_fixture_approval,
                ):
                    return self._current_summary(project, workflow)
                continue
            if stage == "complete":
                return self._current_summary(project, workflow)
            raise CollageError(
                "INVALID_WORKFLOW_STATE",
                f"无法推进工作流阶段：{stage}",
            )
        raise CollageError("WORKFLOW_LOOP", "工作流阶段推进次数超过安全限制")

    def _analyze(self, project: ProjectPaths, workflow: dict[str, Any]) -> None:
        draft_path = project.analysis / "draft.json"
        if draft_path.is_file():
            validate_draft(read_json(draft_path))
        else:
            manual_path = project.inputs / "manual_draft.json"
            policy_path = project.inputs / "product_policy.json"
            provider: VisionProvider | None = None
            if not manual_path.is_file():
                provider_spec = workflow["options"].get("vision_provider")
                if provider_spec is None:
                    raise CollageError(
                        "VISION_PROVIDER_UNAVAILABLE",
                        "项目没有人工 Draft，也没有配置 VLM provider",
                    )
                provider = load_provider(provider_spec, VisionProvider)
            analyze_reference(
                project.inputs / "reference.png",
                project.analysis,
                manual_draft_path=manual_path if manual_path.is_file() else None,
                provider=provider,
                product_policy=read_json(policy_path)
                if policy_path.is_file()
                else None,
            )
        self._transition(
            project,
            workflow,
            "awaiting_review",
            wait={
                "code": "HUMAN_REVIEW_REQUIRED",
                "message": "请在本机审核页确认 Draft 和删除蒙版",
            },
        )

    def _review(
        self,
        project: ProjectPaths,
        workflow: dict[str, Any],
        *,
        open_review: bool,
    ) -> bool:
        reviewed_path = project.review / "reviewed.json"
        if reviewed_path.is_file():
            validate_reviewed_spec(read_json(reviewed_path))
            self._transition(project, workflow, "reviewed")
            return True
        if not open_review:
            self._transition(
                project,
                workflow,
                "awaiting_review",
                wait={
                    "code": "HUMAN_REVIEW_REQUIRED",
                    "message": "运行 resume 并打开审核页后才能继续",
                },
            )
            return False

        def optional_input(name: str) -> Path | None:
            path = project.root / ARTIFACT_PATHS[name]
            return path if path.is_file() else None

        serve_review_ui(
            project.analysis / "draft.json",
            reviewed_path,
            reviewer=workflow["options"]["reviewer"],
            initial_mask_path=optional_input("initial_mask"),
            allowed_mask_path=optional_input("allowed_mask"),
            background_candidate_path=optional_input("background_candidate"),
            port=workflow["options"]["review_port"],
        )
        if not reviewed_path.is_file():
            return False
        validate_reviewed_spec(read_json(reviewed_path))
        self._transition(project, workflow, "reviewed")
        return True

    def _build(self, project: ProjectPaths, workflow: dict[str, Any]) -> None:
        self._transition(project, workflow, "building")
        manifest_path = project.template / "template.json"
        if manifest_path.is_file():
            template = validate_package(project.template, require_ready=False)
            if template["status"] == "ready":
                self._transition(project, workflow, "complete")
                return
        else:
            reviewed = validate_reviewed_spec(
                read_json(project.review / "reviewed.json")
            )
            image_provider = self._image_provider(workflow, reviewed)
            build_template(
                project.review / "reviewed.json",
                project.template,
                work_dir=project.workspace,
                image_provider=image_provider,
            )
        ensure_upload_files(project)
        self._transition(
            project,
            workflow,
            "awaiting_bindings",
            wait={
                "code": "BINDINGS_REQUIRED",
                "message": "请按上传指南提供客户素材和 Bindings",
            },
        )

    def _prepare_bindings(
        self,
        project: ProjectPaths,
        workflow: dict[str, Any],
    ) -> bool:
        ensure_upload_files(project)
        reason = bindings_wait_reason(project)
        if reason is not None:
            self._transition(
                project,
                workflow,
                "awaiting_bindings",
                wait=reason,
            )
            return False
        prepare_cutout_bindings(
            project,
            provider_spec=workflow["options"].get("cutout_provider"),
            allow_cloud_upload=workflow["options"]["allow_cloud_upload"],
        )
        self._transition(project, workflow, "rendering")
        return True

    def _render(self, project: ProjectPaths, workflow: dict[str, Any]) -> None:
        render_from_files(
            project.template,
            project.renders / "bindings.json",
            project.renders / "result.png",
            require_ready=False,
        )
        self._transition(
            project,
            workflow,
            "awaiting_approval",
            wait={
                "code": "VISUAL_APPROVAL_REQUIRED",
                "message": "请检查 renders/result.png，再显式运行 resume --approve",
            },
        )

    def _approve(
        self,
        project: ProjectPaths,
        workflow: dict[str, Any],
        *,
        approve: bool,
        notes: str,
        allow_fixture: bool,
    ) -> bool:
        template = validate_package(project.template, require_ready=False)
        if template["status"] == "ready":
            self._transition(project, workflow, "complete")
            return True
        if not approve:
            self._transition(
                project,
                workflow,
                "awaiting_approval",
                wait={
                    "code": "VISUAL_APPROVAL_REQUIRED",
                    "message": "请检查 renders/result.png，再显式运行 resume --approve",
                },
            )
            return False
        approve_template(
            project.template,
            [project.renders / "result.png"],
            reviewer=workflow["options"]["reviewer"],
            notes=notes,
            allow_fixture=allow_fixture,
            work_dir=project.workspace,
        )
        self._transition(project, workflow, "complete")
        return True

    @staticmethod
    def _image_provider(
        workflow: dict[str, Any],
        reviewed: dict[str, Any],
    ) -> ImageProvider | None:
        provider_required = reviewed["background"]["candidate_path"] is None or any(
            overlay["action"] == "reference_generate"
            and overlay["prepared_asset"] is None
            for overlay in reviewed["overlays"]
        )
        if not provider_required:
            return None
        if workflow["options"]["fixture_provider"]:
            return DeterministicFixtureImageProvider()
        provider_spec = workflow["options"].get("image_provider")
        if provider_spec is None:
            raise CollageError(
                "IMAGE_PROVIDER_UNAVAILABLE",
                "ReviewedSpec 需要生成图片，但工作流没有配置图片 provider",
            )
        return load_provider(provider_spec, ImageProvider)

    def _transition(
        self,
        project: ProjectPaths,
        workflow: dict[str, Any],
        stage: str,
        *,
        wait: dict[str, Any] | None = None,
    ) -> None:
        previous = workflow["stage"]
        timestamp = utc_now()
        workflow["stage"] = stage
        workflow["resume_stage"] = None
        workflow["stage_updated_at"] = timestamp
        workflow["wait"] = wait
        workflow["last_error"] = None
        if previous != stage:
            workflow.setdefault("history", []).append({"stage": stage, "at": timestamp})
        validate_workflow(workflow)
        self.store.update_workflow(
            project.project_id,
            workflow,
            status=project_status_for_stage(stage),
        )
        LOGGER.info(
            "项目工作流阶段更新 | project=%s stage=%s",
            project.project_id,
            stage,
        )

    def _record_failure(
        self,
        project: ProjectPaths,
        workflow: dict[str, Any],
        error: CollageError,
    ) -> None:
        current = workflow.get("stage", "analyzing")
        if current in {"blocked", "failed"}:
            current = workflow.get("resume_stage") or self._infer_resume_stage(project)
        failure_stage = "blocked" if _is_blocking_error(error.code) else "failed"
        timestamp = utc_now()
        workflow["stage"] = failure_stage
        workflow["resume_stage"] = current
        workflow["stage_updated_at"] = timestamp
        workflow["wait"] = None
        # Persist only the stable code/message; provider details can contain local paths.
        workflow["last_error"] = {"code": error.code, "message": error.message}
        workflow.setdefault("history", []).append(
            {"stage": failure_stage, "at": timestamp, "code": error.code}
        )
        self.store.update_workflow(
            project.project_id,
            workflow,
            status=project_status_for_stage(failure_stage),
        )

    @staticmethod
    def _infer_resume_stage(project: ProjectPaths) -> str:
        manifest_path = project.template / "template.json"
        if manifest_path.is_file():
            template = validate_package(project.template, require_ready=False)
            if template["status"] == "ready":
                return "complete"
            if (project.renders / "result.png").is_file():
                return "awaiting_approval"
            return "awaiting_bindings"
        if (project.review / "reviewed.json").is_file():
            return "reviewed"
        if (project.analysis / "draft.json").is_file():
            return "awaiting_review"
        return "analyzing"

    def _current_summary(
        self,
        project: ProjectPaths,
        workflow: dict[str, Any],
    ) -> dict[str, Any]:
        manifest = self.store.get_manifest(project.project_id)
        return self._summary(project, manifest, workflow)

    @staticmethod
    def _summary(
        project: ProjectPaths,
        manifest: dict[str, Any],
        workflow: dict[str, Any],
    ) -> dict[str, Any]:
        stage = workflow["stage"]
        next_actions = {
            "analyzing": "继续分析参考图",
            "awaiting_review": "运行 resume 打开审核页并保存确认结果",
            "reviewed": "运行 resume 构建模板",
            "building": "运行 resume 继续构建模板",
            "awaiting_bindings": "编辑 renders/bindings.json，或用 resume --bindings 导入",
            "rendering": "运行 resume 继续生成预览",
            "awaiting_approval": "检查 renders/result.png 后运行 resume --approve",
            "complete": "模板已发布，可以继续用本地 Renderer 生成结果",
            "blocked": "修复 last_error 后运行 resume",
            "failed": "检查 last_error 和项目文件后运行 resume",
        }
        artifacts = {
            name: {
                "path": str(project.root / relative),
                "exists": (project.root / relative).is_file(),
            }
            for name, relative in workflow["artifacts"].items()
        }
        return {
            "project_id": project.project_id,
            "name": manifest["name"],
            "status": manifest["status"],
            "stage": stage,
            "root": str(project.root),
            "next_action": next_actions[stage],
            "wait": workflow.get("wait"),
            "last_error": workflow.get("last_error"),
            "artifacts": artifacts,
        }
