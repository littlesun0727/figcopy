"""Execute individual analysis, review, build, render, and approval stages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.errors import CollageError
from ..core.io import read_json
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
from .model import ARTIFACT_PATHS
from .state import transition


class WorkflowStages:
    """Run workflow stages while delegating every transition to durable state."""

    def __init__(self, store: ProjectStore):
        self.store = store

    def analyze(self, project: ProjectPaths, workflow: dict[str, Any]) -> None:
        """Create or reuse a validated candidate Draft."""

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
        transition(
            self.store,
            project,
            workflow,
            "awaiting_review",
            wait={
                "code": "HUMAN_REVIEW_REQUIRED",
                "message": "请在本机审核页确认 Draft 和删除蒙版",
            },
        )

    def review(
        self,
        project: ProjectPaths,
        workflow: dict[str, Any],
        *,
        open_review: bool,
    ) -> bool:
        """Wait for or run the local human Draft review page."""

        reviewed_path = project.review / "reviewed.json"
        if reviewed_path.is_file():
            validate_reviewed_spec(read_json(reviewed_path))
            transition(self.store, project, workflow, "reviewed")
            return True
        if not open_review:
            transition(
                self.store,
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
        transition(self.store, project, workflow, "reviewed")
        return True

    def build(self, project: ProjectPaths, workflow: dict[str, Any]) -> None:
        """Build or reuse a needs-review template package and upload guide."""

        transition(self.store, project, workflow, "building")
        manifest_path = project.template / "template.json"
        if manifest_path.is_file():
            template = validate_package(project.template, require_ready=False)
            if template["status"] == "ready":
                transition(self.store, project, workflow, "complete")
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
        transition(
            self.store,
            project,
            workflow,
            "awaiting_bindings",
            wait={
                "code": "BINDINGS_REQUIRED",
                "message": "请按上传指南提供客户素材和 Bindings",
            },
        )

    def prepare_bindings(
        self,
        project: ProjectPaths,
        workflow: dict[str, Any],
    ) -> bool:
        """Wait for customer inputs and prepare cutout slots when necessary."""

        ensure_upload_files(project)
        reason = bindings_wait_reason(project)
        if reason is not None:
            transition(
                self.store,
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
        transition(self.store, project, workflow, "rendering")
        return True

    def render(self, project: ProjectPaths, workflow: dict[str, Any]) -> None:
        """Render a local preview from the project template and Bindings."""

        render_from_files(
            project.template,
            project.renders / "bindings.json",
            project.renders / "result.png",
            require_ready=False,
        )
        transition(
            self.store,
            project,
            workflow,
            "awaiting_approval",
            wait={
                "code": "VISUAL_APPROVAL_REQUIRED",
                "message": "请检查 renders/result.png，再显式运行 resume --approve",
            },
        )

    def approve(
        self,
        project: ProjectPaths,
        workflow: dict[str, Any],
        *,
        approve: bool,
        notes: str,
        allow_fixture: bool,
    ) -> bool:
        """Wait for explicit approval, then publish the reviewed template."""

        template = validate_package(project.template, require_ready=False)
        if template["status"] == "ready":
            transition(self.store, project, workflow, "complete")
            return True
        if not approve:
            transition(
                self.store,
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
        transition(self.store, project, workflow, "complete")
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
