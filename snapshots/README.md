# Figcopy 项目数据快照

本目录保存 2026-09-15 提交时的完整运行数据：6 个项目、各阶段图片与 JSON、前端任务历史，以及历史归档。代码与快照配套使用，无需重新调用 VLM 或生图服务即可查看已有产物。

## 恢复并打开工作台

先安装 Python 项目依赖，并拉取 LFS 图片/视频：

```powershell
git lfs install
git lfs pull
python -m pip install -e .
```

建议复制到新的运行目录，保留仓库快照不变。下面的目标目录应尚不存在：

```powershell
if (Test-Path -LiteralPath 'D:\datas\figcopy-restored') { throw '目标目录已存在，请换一个新目录' }
Copy-Item -LiteralPath '.\snapshots\figcopy' -Destination 'D:\datas\figcopy-restored' -Recurse
python -m collage studio --data-dir 'D:\datas\figcopy-restored'
```

工作台地址为 http://127.0.0.1:8787/ 。其他系统可将快照复制到任意目录，并通过 `--data-dir` 指定。

## 内容及状态

- `figcopy/projects/`：全部项目清单、参考图和客户照片、识别与确认结果、模板、生成过程、成图与报告。
- `figcopy/projects/*/workspace/`：前端回看所需的参考裁片、生成原图、去底结果、状态与生成记录，随快照保留。
- `figcopy/logs/jobs/`：前端显示的任务历史与诊断。
- `figcopy/archive/`：历史备份、prompt 对照实验及 test2 回退前的成图。
- `figcopy/cache/`、`figcopy/project_hist/`：提交时为空，使用 `.gitkeep` 保留顶层目录。

**test2 保持生成预览前（awaiting_bindings）状态，4 张客户照片和模板已保存。** 打开项目直接点击“生成预览”，即可用此分支的本地相框窗口修复重新合成；旧成图仍在 archive 的回退备份中。其他项目保留原有完成、失败或旧流程状态，不伪造完成结果。

密钥和进程内 Provider 设置不包含在快照里。查看已有内容、使用 test2 现有照片本地 render 不需要配置生图凭据；重新分析或生成素材时按需配置。

## 完整性

`manifest.json` 记录源文件的相对路径、字节数和 SHA-256，以及快照时各项目阶段。图片和视频通过 Git LFS 保存，JSON 等文件保留原始字节；必须执行 `git lfs pull` 后才能正常读取图片。复制时已逐文件校验快照与源数据一致，原运行目录未修改。

归档内的旧脚本和说明可能引用历史本机路径；它们用于留存实验记录，恢复当前工作台不依赖运行这些归档脚本。
