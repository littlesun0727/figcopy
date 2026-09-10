"""Expose the Web studio, resumable workflow, and advanced single-step commands."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from .. import __version__
from ..core.errors import CollageError
from ..core.io import read_json
from ..core.logging import configure_logging
from ..devtools.demo import create_demo
from ..projects import DataPaths, ProjectStore
from ..providers import (
    CutoutProvider,
    DeterministicFixtureImageProvider,
    ImageProvider,
    VisionProvider,
    load_provider,
)
from ..rendering import render_from_files
from ..rendering.cutout import prepare_cutout
from ..studio.review_server import serve_review_ui
from ..studio.workbench import serve_workbench
from ..template.analysis import analyze_reference
from ..template.build import approve_template, build_template
from ..template.guide import create_upload_guide
from ..template.review import confirm_draft
from ..template.validation import validate_package
from ..workflows import (
    DEFAULT_CUTOUT_PROVIDER,
    DEFAULT_IMAGE_PROVIDER,
    DEFAULT_VISION_PROVIDER,
    WorkflowService,
)

LOGGER = logging.getLogger(__name__)


def _path(value: str) -> Path:
    return Path(value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="collage", description="参考拼贴模板制作与本地确定性渲染"
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--verbose", action="store_true", help="输出 debug 日志")
    subparsers = parser.add_subparsers(dest="command", required=True)

    studio = subparsers.add_parser("studio", help="启动端到端本机 Web 工作台")
    studio.add_argument("--data-dir", type=_path, help="Figcopy 外部数据根目录")
    studio.add_argument("--port", type=int, default=8787, help="本机监听端口")
    studio.add_argument(
        "--no-open",
        action="store_true",
        help="启动后不自动打开浏览器",
    )

    run = subparsers.add_parser("run", help="创建项目并端到端推进到下一个人工门禁")
    run.add_argument("--project", required=True, help="项目 ID")
    run.add_argument("--data-dir", type=_path, help="Figcopy 外部数据根目录")
    run.add_argument("--name", help="可选项目显示名称")
    run.add_argument("--reference", type=_path, required=True, help="参考拼贴图")
    run.add_argument("--reviewer", required=True, help="审核人名称")
    run_source = run.add_mutually_exclusive_group()
    run_source.add_argument("--manual-draft", type=_path, help="可选人工 Draft JSON")
    run.add_argument("--policy", type=_path, help="可选 product_policy JSON")
    run.add_argument("--background-candidate", type=_path, help="可选清版背景")
    run.add_argument("--mask", type=_path, help="可选初始删除蒙版")
    run.add_argument("--allowed-mask", type=_path, help="可选允许编辑区域蒙版")
    run.add_argument("--bindings", type=_path, help="可提前导入客户 Bindings")
    run_source.add_argument(
        "--vision-provider",
        help=f"VLM provider；默认 {DEFAULT_VISION_PROVIDER}",
    )
    run_image = run.add_mutually_exclusive_group()
    run_image.add_argument(
        "--image-provider",
        help=f"图片 provider；默认 {DEFAULT_IMAGE_PROVIDER}",
    )
    run_image.add_argument(
        "--fixture-provider",
        action="store_true",
        help="仅用于离线流程验证",
    )
    run.add_argument(
        "--cutout-provider",
        help=f"抠图 provider；默认 {DEFAULT_CUTOUT_PROVIDER}",
    )
    run.add_argument("--allow-cloud-upload", action="store_true")
    run.add_argument("--review-port", type=int, default=8765)
    run.add_argument(
        "--no-review-ui",
        action="store_true",
        help="分析后暂停，不在本次命令启动审核页",
    )

    resume = subparsers.add_parser("resume", help="从项目记录的阶段继续执行")
    resume.add_argument("--project", required=True, help="项目 ID")
    resume.add_argument("--data-dir", type=_path, help="Figcopy 外部数据根目录")
    resume.add_argument("--bindings", type=_path, help="导入客户 Bindings 及图片")
    resume.add_argument("--background-candidate", type=_path, help="补充清版背景")
    resume.add_argument("--mask", type=_path, help="替换初始删除蒙版")
    resume.add_argument("--allowed-mask", type=_path, help="替换允许编辑区域蒙版")
    resume.add_argument("--reviewer", help="更新审核人名称")
    resume.add_argument("--vision-provider", help="更新 VLM provider")
    resume_image = resume.add_mutually_exclusive_group()
    resume_image.add_argument("--image-provider", help="更新图片 provider")
    resume_image.add_argument(
        "--fixture-provider",
        action="store_const",
        const=True,
        default=None,
        help="改用离线 fixture 图片 provider",
    )
    resume.add_argument("--cutout-provider", help="更新抠图 provider")
    cloud_permission = resume.add_mutually_exclusive_group()
    cloud_permission.add_argument(
        "--allow-cloud-upload",
        dest="allow_cloud_upload",
        action="store_const",
        const=True,
        default=None,
    )
    cloud_permission.add_argument(
        "--no-cloud-upload",
        dest="allow_cloud_upload",
        action="store_const",
        const=False,
    )
    resume.add_argument("--review-port", type=int)
    resume.add_argument("--no-review-ui", action="store_true")
    resume.add_argument(
        "--approve",
        action="store_true",
        help="检查结果图后显式批准模板",
    )
    resume.add_argument("--approval-notes", default="")
    resume.add_argument(
        "--allow-fixture-approval",
        action="store_true",
        help="仅允许明确发布演示用 fixture 模板",
    )

    status = subparsers.add_parser("status", help="查看项目阶段和下一步操作")
    status.add_argument("--project", required=True, help="项目 ID")
    status.add_argument("--data-dir", type=_path, help="Figcopy 外部数据根目录")

    analyze = subparsers.add_parser("analyze", help="规范化参考图并生成候选 Draft")
    analyze.add_argument("--reference", type=_path, required=True)
    analyze.add_argument("--out", type=_path, required=True)
    analyze.add_argument("--manual-draft", type=_path)
    analyze.add_argument("--provider", help="真实 VLM provider，格式 module:object")
    analyze.add_argument("--policy", type=_path, help="可选 product_policy JSON")
    analyze.add_argument("--force", action="store_true")

    review = subparsers.add_parser(
        "review", help="把已人工修正 Draft 固化为 ReviewedSpec"
    )
    review.add_argument("--draft", type=_path, required=True)
    review.add_argument("--out", type=_path, required=True)
    review.add_argument("--remove-mask", type=_path, required=True)
    review.add_argument("--allowed-mask", type=_path)
    review.add_argument("--background-candidate", type=_path)
    review.add_argument("--slot-overrides", type=_path)
    review.add_argument("--overlay-overrides", type=_path)
    review.add_argument(
        "--expand-px", type=int, default=0, help="人工确认的删除区域扩张宽度"
    )
    review.add_argument(
        "--feather-px", type=int, default=0, help="人工确认的背景混合边带宽度"
    )
    review.add_argument("--reviewer", required=True)
    review.add_argument("--notes", default="")
    review.add_argument("--force", action="store_true")

    review_ui = subparsers.add_parser(
        "review-ui", help="启动仅监听本机的一页式人工确认界面"
    )
    review_ui.add_argument("--draft", type=_path, required=True)
    review_ui.add_argument("--out", type=_path, required=True)
    review_ui.add_argument("--reviewer", required=True)
    review_ui.add_argument(
        "--mask", type=_path, help="可选：覆盖根据 Draft 矩形自动生成的初始清版蒙版"
    )
    review_ui.add_argument("--allowed-mask", type=_path)
    review_ui.add_argument("--background-candidate", type=_path)
    review_ui.add_argument("--slot-overrides", type=_path)
    review_ui.add_argument("--overlay-overrides", type=_path)
    review_ui.add_argument("--expand-px", type=int, default=0)
    review_ui.add_argument("--feather-px", type=int, default=0)
    review_ui.add_argument("--port", type=int, default=8765)

    build = subparsers.add_parser("build", help="构建 needs_review 模板包")
    build.add_argument("--spec", type=_path, required=True)
    build.add_argument("--out", type=_path, required=True)
    build.add_argument("--work", type=_path)
    provider_group = build.add_mutually_exclusive_group()
    provider_group.add_argument(
        "--provider", help="真实图片 provider，格式 module:object"
    )
    provider_group.add_argument(
        "--fixture-provider", action="store_true", help="仅用确定性 fixture 验证链路"
    )
    build.add_argument("--force", action="store_true")

    render = subparsers.add_parser("render", help="按模板和 Bindings 本地导出 PNG")
    render.add_argument("--template", type=_path, required=True)
    render.add_argument("--bindings", type=_path, required=True)
    render.add_argument("--out", type=_path, required=True)
    render.add_argument(
        "--allow-unreviewed", action="store_true", help="仅供模板作者生成验收预览"
    )

    validate = subparsers.add_parser("validate", help="校验模板包")
    validate.add_argument("--template", type=_path, required=True)
    validate.add_argument("--allow-unreviewed", action="store_true")

    approve = subparsers.add_parser("approve", help="记录人工视觉验收并发布模板")
    approve.add_argument("--template", type=_path, required=True)
    approve.add_argument("--evidence", type=_path, action="append", required=True)
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--notes", default="")
    approve.add_argument(
        "--work", type=_path, help="自定义构建 work 目录；默认按模板目录名推导"
    )
    approve.add_argument(
        "--allow-fixture", action="store_true", help="只用于明确的演示模板"
    )

    cutout = subparsers.add_parser("cutout", help="在 Renderer 外准备透明主体素材")
    cutout.add_argument("--input", type=_path, required=True)
    cutout.add_argument("--out", type=_path, required=True)
    cutout.add_argument(
        "--provider",
        help=("抠图 provider，格式 module:object；默认使用本地 BiRefNet_lite-matting"),
    )
    cutout.add_argument("--allow-cloud-upload", action="store_true")

    guide = subparsers.add_parser(
        "guide", help="从 slots 生成上传指南和 Bindings 起始文件"
    )
    guide.add_argument("--template", type=_path, required=True)
    guide.add_argument("--out", type=_path, required=True, help="HTML 指南路径")
    guide.add_argument("--bindings-out", type=_path, required=True)
    guide.add_argument("--allow-unreviewed", action="store_true")

    demo = subparsers.add_parser("demo", help="生成不依赖 AI 的完整 M1 演示")
    destination = demo.add_mutually_exclusive_group()
    destination.add_argument("--out", type=_path, help="兼容旧版的显式输出目录")
    destination.add_argument("--project", default="m1-demo", help="数据目录内的项目 ID")
    demo.add_argument(
        "--data-dir",
        type=_path,
        help="运行数据根目录；默认读取 FIGCOPY_DATA_DIR 或系统用户数据目录",
    )
    return parser


def _run(args: argparse.Namespace) -> Any:
    if args.command == "studio":
        serve_workbench(
            args.data_dir,
            port=args.port,
            open_browser=not args.no_open,
        )
        return None
    if args.command == "run":
        workflow = WorkflowService(ProjectStore(DataPaths.resolve(args.data_dir)))
        return workflow.start(
            args.project,
            args.reference,
            reviewer=args.reviewer,
            name=args.name,
            manual_draft_path=args.manual_draft,
            product_policy_path=args.policy,
            background_candidate_path=args.background_candidate,
            initial_mask_path=args.mask,
            allowed_mask_path=args.allowed_mask,
            bindings_path=args.bindings,
            vision_provider_spec=args.vision_provider,
            image_provider_spec=args.image_provider,
            cutout_provider_spec=args.cutout_provider,
            fixture_provider=args.fixture_provider,
            allow_cloud_upload=args.allow_cloud_upload,
            review_port=args.review_port,
            open_review=not args.no_review_ui,
        )
    if args.command == "resume":
        workflow = WorkflowService(ProjectStore(DataPaths.resolve(args.data_dir)))
        return workflow.resume(
            args.project,
            bindings_path=args.bindings,
            background_candidate_path=args.background_candidate,
            initial_mask_path=args.mask,
            allowed_mask_path=args.allowed_mask,
            reviewer=args.reviewer,
            vision_provider_spec=args.vision_provider,
            image_provider_spec=args.image_provider,
            cutout_provider_spec=args.cutout_provider,
            fixture_provider=args.fixture_provider,
            allow_cloud_upload=args.allow_cloud_upload,
            review_port=args.review_port,
            open_review=not args.no_review_ui,
            approve=args.approve,
            approval_notes=args.approval_notes,
            allow_fixture_approval=args.allow_fixture_approval,
        )
    if args.command == "status":
        workflow = WorkflowService(ProjectStore(DataPaths.resolve(args.data_dir)))
        return workflow.status(args.project)
    if args.command == "analyze":
        provider = (
            load_provider(args.provider, VisionProvider) if args.provider else None
        )
        policy = read_json(args.policy) if args.policy else None
        return analyze_reference(
            args.reference,
            args.out,
            manual_draft_path=args.manual_draft,
            provider=provider,
            product_policy=policy,
            force=args.force,
        )
    if args.command == "review":
        return confirm_draft(
            args.draft,
            args.out,
            remove_mask_path=args.remove_mask,
            reviewer=args.reviewer,
            allowed_mask_path=args.allowed_mask,
            background_candidate_path=args.background_candidate,
            slot_overrides_path=args.slot_overrides,
            overlay_overrides_path=args.overlay_overrides,
            background_expand_px=args.expand_px,
            background_feather_px=args.feather_px,
            notes=args.notes,
            force=args.force,
        )
    if args.command == "review-ui":
        serve_review_ui(
            args.draft,
            args.out,
            reviewer=args.reviewer,
            initial_mask_path=args.mask,
            allowed_mask_path=args.allowed_mask,
            background_candidate_path=args.background_candidate,
            slot_overrides_path=args.slot_overrides,
            overlay_overrides_path=args.overlay_overrides,
            background_expand_px=args.expand_px,
            background_feather_px=args.feather_px,
            port=args.port,
        )
        return args.out
    if args.command == "build":
        if args.fixture_provider:
            provider: ImageProvider | None = DeterministicFixtureImageProvider()
        else:
            provider = (
                load_provider(args.provider, ImageProvider) if args.provider else None
            )
        return build_template(
            args.spec,
            args.out,
            work_dir=args.work,
            image_provider=provider,
            force=args.force,
        )
    if args.command == "render":
        return render_from_files(
            args.template,
            args.bindings,
            args.out,
            require_ready=not args.allow_unreviewed,
        )
    if args.command == "validate":
        return validate_package(args.template, require_ready=not args.allow_unreviewed)
    if args.command == "approve":
        return approve_template(
            args.template,
            args.evidence,
            reviewer=args.reviewer,
            notes=args.notes,
            allow_fixture=args.allow_fixture,
            work_dir=args.work,
        )
    if args.command == "cutout":
        provider_spec = args.provider or DEFAULT_CUTOUT_PROVIDER
        provider = load_provider(provider_spec, CutoutProvider)
        return prepare_cutout(
            args.input,
            args.out,
            provider=provider,
            allow_cloud_upload=args.allow_cloud_upload,
        )
    if args.command == "guide":
        return create_upload_guide(
            args.template,
            args.out,
            args.bindings_out,
            require_ready=not args.allow_unreviewed,
        )
    if args.command == "demo":
        if args.out is not None:
            output_dir = args.out
            store = None
        else:
            store = ProjectStore(DataPaths.resolve(args.data_dir))
            output_dir = store.create(args.project).root
        result = create_demo(output_dir)
        if store is not None:
            store.set_status(args.project, "needs_review")
        return result
    raise AssertionError(f"unknown command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    # argparse 的 --help 也可能在 parse_args 内直接输出，因此先设置终端编码。
    configure_logging(False)
    parser = _parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    try:
        result = _run(args)
        if isinstance(result, Path):
            print(result.resolve())
        elif isinstance(result, tuple):
            print("\n".join(str(Path(item).resolve()) for item in result))
        elif isinstance(result, dict):
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(json.dumps({"ok": True}, ensure_ascii=False))
        return 0
    except CollageError as exc:
        LOGGER.error("操作失败 | code=%s message=%s", exc.code, exc.message)
        print(json.dumps(exc.as_dict(), ensure_ascii=False), file=sys.stderr)
        return 2
