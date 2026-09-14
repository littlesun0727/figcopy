"""Application services exposed by the local Figcopy browser workbench."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ...core.errors import CollageError
from ...core.io import atomic_write_json, read_json, resolve_input_path, sha256_file
from ...projects import DataPaths, ProjectPaths, ProjectStore
from ...providers import VisionProvider, load_provider
from ...schemas import validate_bindings
from ...template.review.feedback import review_revision, revise_draft, validate_feedback
from ...template.review.recovery import available_recoveries, recover_saved_correction
from ...template.validation import validate_package
from ...workflows import WorkflowService
from ...workflows.model import (
    ARTIFACT_PATHS,
    DEFAULT_VISION_PROVIDER,
    validate_workflow,
)
from ..review_session import ReviewSession
from .jobs import JobRegistry
from .multipart import (
    MultipartForm,
    UploadedFile,
    boolean_field,
    optional_text,
    required_text,
)
from .provider_settings import ProviderRuntimeSettings

_START_UPLOADS = {
    "reference",
    "manual_draft",
    "product_policy",
    "background_candidate",
    "initial_mask",
    "allowed_mask",
}


class WorkbenchApplication:
    """Translate browser actions into the same resumable WorkflowService calls as CLI."""

    def __init__(
        self,
        paths: DataPaths,
        *,
        jobs: JobRegistry | None = None,
        provider_settings: ProviderRuntimeSettings | None = None,
    ) -> None:
        self.paths = paths.ensure()
        self.store = ProjectStore(self.paths)
        self.workflow = WorkflowService(self.store)
        self.jobs = jobs or JobRegistry()
        self.provider_settings = provider_settings or ProviderRuntimeSettings()

    def provider_status(self, *, probe_audit: bool = False) -> dict[str, Any]:
        """Return public Provider readiness without exposing secret values or paths."""

        return self.provider_settings.status(probe_audit=probe_audit)

    def configure_providers(self, payload: Any) -> dict[str, Any]:
        """Apply process-memory Provider settings while no background job is active."""

        if self.jobs.active_project_ids():
            raise CollageError(
                "PROVIDER_SETTINGS_BUSY",
                "有后台任务正在使用 Provider，请等待任务结束后再修改设置",
            )
        return self.provider_settings.configure(payload)

    def list_projects(self) -> list[dict[str, Any]]:
        """List valid projects plus transient jobs that have not created one yet."""

        projects: list[dict[str, Any]] = []
        known_ids: set[str] = set()
        for manifest in self.store.list():
            project_id = manifest["id"]
            known_ids.add(project_id)
            try:
                status = self.project_status(project_id)
            except CollageError as exc:
                status = {
                    "project_id": project_id,
                    "name": manifest.get("name", project_id),
                    "status": "failed",
                    "stage": "invalid",
                    "next_action": "检查 project.json",
                    "wait": None,
                    "last_error": exc.as_dict(),
                    "artifacts": {},
                    "task": self.jobs.latest(project_id),
                    "updated_at": manifest.get("updated_at"),
                    "history": [],
                }
            projects.append(status)
        for project_id in sorted(self.jobs.active_project_ids() - known_ids):
            projects.append(
                {
                    "project_id": project_id,
                    "name": project_id,
                    "status": "new",
                    "stage": "analyzing",
                    "next_action": "正在导入参考图并分析",
                    "wait": None,
                    "last_error": None,
                    "artifacts": {},
                    "task": self.jobs.latest(project_id),
                    "updated_at": None,
                    "history": [],
                }
            )
        return sorted(
            projects,
            key=lambda item: (
                item.get("updated_at")
                or (item.get("task") or {}).get("updated_at")
                or ""
            ),
            reverse=True,
        )

    def project_status(self, project_id: str) -> dict[str, Any]:
        """Return a browser-safe status containing URLs rather than filesystem paths."""

        status = self.workflow.status(project_id)
        manifest = self.store.get_manifest(project_id)
        artifacts = {
            name: {
                "exists": details["exists"],
                "url": (
                    f"/api/projects/{project_id}/artifacts/{name}"
                    if details["exists"]
                    else None
                ),
            }
            for name, details in status["artifacts"].items()
        }
        workflow = manifest.get("workflow")
        history = workflow.get("history", []) if isinstance(workflow, dict) else []
        project = self.store.open(project_id)
        template_path = project.template / "template.json"
        template = read_json(template_path) if template_path.is_file() else {}
        reviewed_path = project.review / "reviewed.json"
        reviewed = read_json(reviewed_path) if reviewed_path.is_file() else {}
        return {
            "project_id": status["project_id"],
            "name": status["name"],
            "status": status["status"],
            "stage": status["stage"],
            "next_action": status["next_action"],
            "wait": status["wait"],
            "last_error": status["last_error"],
            "artifacts": artifacts,
            "task": self.jobs.latest(project_id),
            "updated_at": manifest.get("updated_at"),
            "history": history[-20:],
            "warnings": template.get("build", {}).get("warnings", []),
            "template_revision": sha256_file(template_path) if template else None,
            "overlays": [
                {"id": item["id"], "label": item["label"]}
                for item in reviewed.get("overlays", [])
                if item["action"] == "reference_generate"
            ]
            if template
            else [],
        }

    def latest_job(self, project_id: str) -> dict[str, Any] | None:
        """Return transient progress even before a project's manifest exists."""

        self.paths.project(project_id)
        return self.jobs.latest(project_id)

    def start_project(self, form: MultipartForm) -> dict[str, Any]:
        """Validate a create form and enqueue reference analysis."""

        project_id = required_text(form.fields, "project_id", max_length=64)
        name = optional_text(form.fields, "name", max_length=160)
        reviewer = required_text(form.fields, "reviewer", max_length=160)
        project = self.paths.project(project_id)
        if project.manifest.exists():
            raise CollageError(
                "PROJECT_ALREADY_EXISTS",
                "项目已经存在",
                details={"project_id": project_id},
            )
        unknown_uploads = set(form.files) - _START_UPLOADS
        if unknown_uploads:
            raise CollageError(
                "UNKNOWN_UPLOAD_FIELD",
                "创建项目包含未知上传字段",
                details={"fields": sorted(unknown_uploads)},
            )
        reference = self._required_upload(form.files, "reference")
        uploads = {key: value for key, value in form.files.items() if value.data}
        uploads["reference"] = reference
        fixture_provider = boolean_field(form.fields, "fixture_provider")
        allow_cloud_upload = boolean_field(form.fields, "allow_cloud_upload")
        vision_provider = optional_text(form.fields, "vision_provider", max_length=300)
        image_provider = optional_text(form.fields, "image_provider", max_length=300)
        cutout_provider = optional_text(form.fields, "cutout_provider", max_length=300)

        def operation() -> None:
            with self._staged_uploads(project_id, uploads) as (_root, staged):
                self.workflow.start(
                    project_id,
                    staged["reference"],
                    reviewer=reviewer,
                    name=name,
                    manual_draft_path=staged.get("manual_draft"),
                    product_policy_path=staged.get("product_policy"),
                    background_candidate_path=staged.get("background_candidate"),
                    initial_mask_path=staged.get("initial_mask"),
                    allowed_mask_path=staged.get("allowed_mask"),
                    vision_provider_spec=vision_provider,
                    image_provider_spec=image_provider,
                    cutout_provider_spec=cutout_provider,
                    fixture_provider=fixture_provider,
                    allow_cloud_upload=allow_cloud_upload,
                    open_review=False,
                )

        return self.jobs.submit(project_id, "create", operation)

    def review_session(self, project_id: str) -> ReviewSession:
        """Build the shared review session for a workflow's current Draft."""

        project = self.store.open(project_id)
        manifest = self.store.get_manifest(project_id)
        workflow = validate_workflow(manifest.get("workflow"))
        draft_path = project.analysis / "draft.json"
        if not draft_path.is_file():
            raise CollageError("REVIEW_NOT_READY", "项目尚未生成可审核的 Draft")

        def optional_artifact(name: str) -> Path | None:
            path = project.root / ARTIFACT_PATHS[name]
            return path if path.is_file() else None

        return ReviewSession(
            draft_path,
            project.review / "reviewed.json",
            reviewer=workflow["options"]["reviewer"],
            initial_mask_path=optional_artifact("initial_mask"),
            allowed_mask_path=optional_artifact("allowed_mask"),
            background_candidate_path=optional_artifact("background_candidate"),
        )

    def revise_review(self, project_id: str, payload: Any) -> dict[str, Any]:
        """Queue one correction of the current Draft with explicit customer feedback."""
        project = self.store.open(project_id)
        workflow = validate_workflow(
            self.store.get_manifest(project_id).get("workflow")
        )
        if workflow["stage"] != "awaiting_review":
            raise CollageError("REVIEW_NOT_READY", "当前项目不在识别复核阶段")
        session = self.review_session(project_id)
        validate_feedback(session.draft, payload)
        provider_spec = (
            workflow["options"].get("vision_provider") or DEFAULT_VISION_PROVIDER
        )
        return self.jobs.submit(
            project_id,
            "revise_review",
            lambda: revise_draft(
                project.analysis / "draft.json",
                payload,
                provider=load_provider(provider_spec, VisionProvider),
                reviewed_path=project.review / "reviewed.json",
            ),
        )

    def review_recoveries(self, project_id: str) -> list[dict[str, Any]]:
        """List local correction responses for this project's current Draft."""
        project = self.store.open(project_id)
        if (project.review / "reviewed.json").exists():
            return []
        return available_recoveries(project.analysis / "draft.json")

    def recover_review(self, project_id: str, payload: Any) -> dict[str, Any]:
        """Queue local recovery without loading a provider or advancing to build."""
        project = self.store.open(project_id)
        workflow = validate_workflow(
            self.store.get_manifest(project_id).get("workflow")
        )
        if workflow["stage"] != "awaiting_review":
            raise CollageError("REVIEW_NOT_READY", "当前项目不在识别复核阶段")
        if not isinstance(payload, dict):
            raise CollageError("INVALID_REQUEST", "恢复请求必须是 JSON object")
        current = read_json(project.analysis / "draft.json")
        if payload.get("revision") != review_revision(current):
            raise CollageError("REVIEW_REVISION_CONFLICT", "识别结果已更新，请重新载入")
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or len(request_id) != 64:
            raise CollageError("INVALID_REVIEW_REQUEST_ID", "纠正记录 ID 无效")
        background_slot = payload.get("background_slot_id")
        if background_slot is not None and not isinstance(background_slot, str):
            raise CollageError("INVALID_BACKGROUND_SLOT", "背景槽 ID 无效")
        return self.jobs.submit(
            project_id,
            "recover_review",
            lambda: recover_saved_correction(
                project.analysis / "draft.json",
                request_id,
                background_slot=background_slot,
                reviewed_path=project.review / "reviewed.json",
            ),
        )

    def save_review(self, project_id: str, payload: Any) -> dict[str, Any]:
        """Persist human review and enqueue template construction."""

        project = self.store.open(project_id)
        if (project.review / "reviewed.json").exists():
            raise CollageError("REVIEW_ALREADY_SAVED", "该项目的 Draft 已经确认")
        if project_id in self.jobs.active_project_ids():
            raise CollageError("PROJECT_BUSY", "请等待当前任务结束后确认")
        self.review_session(project_id).save(payload)
        job = self.jobs.submit(
            project_id,
            "build",
            lambda: self.workflow.resume(project_id, open_review=False),
        )
        return {"ok": True, "path": "review/reviewed.json", "task": job}

    def layout(self, project_id: str) -> dict[str, Any]:
        from .layout import layout_document

        return layout_document(self.store.open(project_id))

    def background_revision(self, project_id: str) -> dict[str, Any]:
        """Return a token for explicitly forking the current confirmed project."""
        from .background_revision import revision_document

        return revision_document(self.store.open(project_id))

    def fork_background(self, project_id: str, payload: Any) -> dict[str, Any]:
        """Fork a local review while excluding simultaneous generation jobs."""
        from .background_revision import fork_background_review

        return self.jobs.run_exclusive(
            project_id, lambda: fork_background_review(self.store, project_id, payload)
        )

    def layout_layer(self, project_id: str, identifier: str) -> bytes:
        from .layout import layer_image

        return layer_image(self.store.open(project_id), identifier)

    def edit_layout(self, project_id: str, payload: Any) -> dict:
        from .layout import apply_layout, layer_items

        project = self.store.open(project_id)
        template = apply_layout(project, payload)
        return {
            "revision": payload["revision"],
            "canvas": template["canvas"],
            "items": layer_items(template),
        }

    def preview_layout(self, project_id: str, payload: Any) -> bytes:
        from .layout import preview_layout

        return preview_layout(self.store.open(project_id), payload)

    def save_layout(self, project_id: str, payload: Any) -> dict[str, Any]:
        from .layout import fork_layout

        if project_id in self.jobs.active_project_ids():
            raise CollageError("PROJECT_BUSY", "请等待当前任务结束后保存布局")
        return fork_layout(self.store, project_id, payload)

    def project_slots(self, project_id: str) -> list[dict[str, Any]]:
        """Describe customer inputs without returning local customer paths."""

        project = self.store.open(project_id)
        template = validate_package(project.template, require_ready=False)
        bindings_path = project.renders / "bindings.json"
        current_slots: dict[str, Any] = {}
        if bindings_path.is_file():
            raw = read_json(bindings_path)
            if isinstance(raw, dict) and isinstance(raw.get("slots"), dict):
                current_slots = raw["slots"]
        result: list[dict[str, Any]] = []
        for slot in template["slots"]:
            current = current_slots.get(slot["id"], {})
            item = {
                key: slot.get(key)
                for key in (
                    "id",
                    "label",
                    "type",
                    "mode",
                    "required",
                    "upload_hint",
                    "default_text",
                )
            }
            if slot["type"] == "image":
                path = current.get("path") if isinstance(current, dict) else None
                item["has_image"] = isinstance(path, str) and not path.startswith(
                    "REPLACE_"
                )
                item["scale"] = current.get("scale", 1.0)
                item["offset_px"] = current.get("offset_px", [0, 0])
            else:
                item["text"] = current.get("text", slot.get("default_text") or "")
            result.append(item)
        return result

    def submit_bindings(self, project_id: str, form: MultipartForm) -> dict[str, Any]:
        """Create Bindings from named browser inputs and enqueue local rendering."""

        project = self.store.open(project_id)
        template = validate_package(project.template, require_ready=False)
        raw_configuration = required_text(
            form.fields, "configuration", max_length=1024 * 1024
        )
        try:
            configuration = json.loads(raw_configuration)
        except json.JSONDecodeError as exc:
            raise CollageError(
                "INVALID_BINDINGS_FORM", "客户素材设置不是有效 JSON"
            ) from exc
        if not isinstance(configuration, dict) or not isinstance(
            configuration.get("slots"), dict
        ):
            raise CollageError("INVALID_BINDINGS_FORM", "客户素材设置必须包含 slots")
        configured = configuration["slots"]
        template_slots = {slot["id"]: slot for slot in template["slots"]}
        current_bindings_path = project.renders / "bindings.json"
        current_bindings: dict[str, Any] = {}
        if current_bindings_path.is_file():
            raw_current = read_json(current_bindings_path)
            if isinstance(raw_current, dict) and isinstance(
                raw_current.get("slots"), dict
            ):
                current_bindings = raw_current["slots"]
        unknown_slots = set(configured) - set(template_slots)
        allowed_file_fields = {
            f"{kind}.{slot_id}"
            for slot_id, slot in template_slots.items()
            if slot["type"] == "image"
            for kind in ("image", "alpha")
        }
        unknown_files = set(form.files) - allowed_file_fields
        if unknown_slots or unknown_files:
            raise CollageError(
                "UNKNOWN_BINDING_SLOT",
                "客户素材包含模板中不存在的槽位",
                details={
                    "slots": sorted(unknown_slots),
                    "files": sorted(unknown_files),
                },
            )

        uploads: dict[str, UploadedFile] = {}
        binding_slots: dict[str, dict[str, Any]] = {}
        for index, (slot_id, slot) in enumerate(template_slots.items(), start=1):
            settings = configured.get(slot_id, {})
            if not isinstance(settings, dict):
                raise CollageError(
                    "INVALID_BINDINGS_FORM", f"槽位 {slot_id} 设置必须是 object"
                )
            if slot["type"] == "text":
                text = settings.get("text")
                if text is None:
                    if slot["required"] and slot.get("default_text") is None:
                        raise CollageError(
                            "MISSING_BINDING", f"缺少文字槽位：{slot_id}"
                        )
                    continue
                binding_slots[slot_id] = {"text": text}
                continue

            image_key = f"image.{slot_id}"
            image = form.files.get(image_key)
            if image is None or not image.data:
                current = current_bindings.get(slot_id)
                current_path = (
                    current.get("path") if isinstance(current, dict) else None
                )
                reusable = self._reusable_binding_path(
                    project, current_bindings_path, current_path
                )
                if reusable is not None:
                    binding = {"path": str(reusable)}
                    for field in ("scale", "offset_px"):
                        if field in settings:
                            binding[field] = settings[field]
                    current_alpha = (
                        current.get("subject_alpha")
                        if isinstance(current, dict)
                        else None
                    )
                    reusable_alpha = self._reusable_binding_path(
                        project, current_bindings_path, current_alpha
                    )
                    if reusable_alpha is not None:
                        binding["subject_alpha"] = str(reusable_alpha)
                    binding_slots[slot_id] = binding
                    continue
                if slot["required"]:
                    raise CollageError(
                        "MISSING_UPLOAD", f"请为 {slot['label']} 选择图片"
                    )
                continue
            staged_image_name = f"{index:03d}_image.bin"
            uploads[staged_image_name] = image
            binding: dict[str, Any] = {"path": staged_image_name}
            for field in ("scale", "offset_px"):
                if field in settings:
                    binding[field] = settings[field]
            alpha = form.files.get(f"alpha.{slot_id}")
            if alpha is not None and alpha.data:
                staged_alpha_name = f"{index:03d}_alpha.bin"
                uploads[staged_alpha_name] = alpha
                binding["subject_alpha"] = staged_alpha_name
            binding_slots[slot_id] = binding

        bindings = {"version": "collage-bindings/1", "slots": binding_slots}
        # Schema validation here gives immediate field feedback before queuing work.
        validate_bindings(bindings, template)
        cutout_provider = optional_text(form.fields, "cutout_provider", max_length=300)
        allow_cloud_upload = boolean_field(form.fields, "allow_cloud_upload")

        def operation() -> None:
            with self._staged_uploads(project_id, uploads) as (root, staged):
                bindings_path = root / "bindings.json"
                local_bindings = json.loads(json.dumps(bindings))
                for binding in local_bindings["slots"].values():
                    for field in ("path", "subject_alpha"):
                        name = binding.get(field)
                        if name in staged:
                            binding[field] = staged[name].name
                atomic_write_json(bindings_path, local_bindings)
                self.workflow.resume(
                    project_id,
                    bindings_path=bindings_path,
                    cutout_provider_spec=cutout_provider,
                    allow_cloud_upload=allow_cloud_upload,
                    open_review=False,
                )

        return self.jobs.submit(project_id, "render", operation)

    def retry_project(self, project_id: str, payload: Any) -> dict[str, Any]:
        """Retry a blocked or failed project with optional provider overrides."""

        self.store.open(project_id)
        if not isinstance(payload, dict):
            raise CollageError("INVALID_REQUEST", "重试请求必须是 JSON object")
        allowed = {
            "vision_provider",
            "image_provider",
            "cutout_provider",
            "fixture_provider",
            "allow_cloud_upload",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise CollageError(
                "INVALID_REQUEST",
                "重试请求包含未知字段",
                details={"fields": sorted(unknown)},
            )
        provider_values: dict[str, str | None] = {}
        for name in ("vision_provider", "image_provider", "cutout_provider"):
            value = payload.get(name)
            if value is not None and (not isinstance(value, str) or len(value) > 300):
                raise CollageError("INVALID_REQUEST", f"字段 {name} 格式不正确")
            provider_values[name] = value.strip() if isinstance(value, str) else None
            if provider_values[name] == "":
                provider_values[name] = None
        fixture = payload.get("fixture_provider")
        cloud = payload.get("allow_cloud_upload")
        if fixture is not None and not isinstance(fixture, bool):
            raise CollageError("INVALID_REQUEST", "fixture_provider 必须是 boolean")
        if cloud is not None and not isinstance(cloud, bool):
            raise CollageError("INVALID_REQUEST", "allow_cloud_upload 必须是 boolean")

        return self.jobs.submit(
            project_id,
            "retry",
            lambda: self.workflow.resume(
                project_id,
                vision_provider_spec=provider_values["vision_provider"],
                image_provider_spec=provider_values["image_provider"],
                cutout_provider_spec=provider_values["cutout_provider"],
                fixture_provider=fixture,
                allow_cloud_upload=cloud,
                open_review=False,
            ),
        )

    def regenerate_overlay(self, project_id: str, payload: Any) -> dict[str, Any]:
        """Explicitly regenerate one known decoration and save a separate revision."""
        from .layout import fork_layout, layout_document

        project = self.store.open(project_id)
        document = layout_document(project)
        if (
            not isinstance(payload, dict)
            or payload.get("revision") != document["revision"]
        ):
            raise CollageError("LAYOUT_REVISION_CONFLICT", "模板已更新，请刷新后重做")
        reviewed = read_json(project.review / "reviewed.json")
        identifier = payload.get("overlay_id")
        if not any(
            item["id"] == identifier and item["action"] == "reference_generate"
            for item in reviewed["overlays"]
        ):
            raise CollageError("LAYOUT_LAYER_NOT_FOUND", "没有可重做的这件装饰")
        workflow = validate_workflow(
            self.store.get_manifest(project_id).get("workflow")
        )

        def operation() -> dict:
            provider = self.workflow.stages._image_provider(workflow, reviewed)
            return fork_layout(
                self.store,
                project_id,
                document,
                overlay_id=identifier,
                image_provider=provider,
            )

        return self.jobs.submit(project_id, "regenerate_overlay", operation)

    def approve_project(self, project_id: str, payload: Any) -> dict[str, Any]:
        """Enqueue the explicit final human approval gate."""

        self.store.open(project_id)
        if not isinstance(payload, dict):
            raise CollageError("INVALID_REQUEST", "批准请求必须是 JSON object")
        notes = payload.get("notes", "")
        allow_fixture = payload.get("allow_fixture", False)
        if not isinstance(notes, str) or len(notes) > 2000:
            raise CollageError("INVALID_REQUEST", "批准备注格式不正确或过长")
        if not isinstance(allow_fixture, bool):
            raise CollageError("INVALID_REQUEST", "allow_fixture 必须是 boolean")
        return self.jobs.submit(
            project_id,
            "approve",
            lambda: self.workflow.resume(
                project_id,
                open_review=False,
                approve=True,
                approval_notes=notes,
                allow_fixture_approval=allow_fixture,
            ),
        )

    def artifact_path(self, project_id: str, name: str) -> Path:
        """Resolve only named workflow artifacts inside the selected project."""

        if name not in ARTIFACT_PATHS:
            raise CollageError("ARTIFACT_NOT_FOUND", "未知项目产物")
        project = self.store.open(project_id)
        path = (project.root / ARTIFACT_PATHS[name]).resolve()
        try:
            path.relative_to(project.root.resolve())
        except ValueError as exc:  # pragma: no cover - constant map defense
            raise CollageError("UNSAFE_ARTIFACT_PATH", "项目产物路径越界") from exc
        if not path.is_file():
            raise CollageError("ARTIFACT_NOT_FOUND", "项目产物尚未生成")
        return path

    @staticmethod
    def _required_upload(uploads: dict[str, UploadedFile], name: str) -> UploadedFile:
        upload = uploads.get(name)
        if upload is None or not upload.data:
            raise CollageError("MISSING_UPLOAD", f"缺少必填文件：{name}")
        return upload

    @staticmethod
    def _reusable_binding_path(
        project: ProjectPaths,
        bindings_path: Path,
        raw_path: Any,
    ) -> Path | None:
        """Reuse an imported binding only while it remains inside this project."""

        if not isinstance(raw_path, str) or raw_path.startswith("REPLACE_"):
            return None
        path = resolve_input_path(bindings_path, raw_path)
        try:
            path.relative_to(project.root.resolve())
        except ValueError:
            return None
        return path if path.is_file() else None

    @contextmanager
    def _staged_uploads(
        self,
        project_id: str,
        uploads: dict[str, UploadedFile],
    ) -> Iterator[tuple[Path, dict[str, Path]]]:
        """Write opaque upload names to regenerable cache for one operation only."""

        prefix = f"upload-{project_id}-"
        with tempfile.TemporaryDirectory(
            prefix=prefix, dir=self.paths.cache
        ) as raw_dir:
            root = Path(raw_dir)
            staged: dict[str, Path] = {}
            for name, upload in uploads.items():
                safe_name = name.replace(".", "_")
                path = root / safe_name
                path.write_bytes(upload.data)
                staged[name] = path
            yield root, staged
