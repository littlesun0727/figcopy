# 本次修复说明

已完成背景和人物的本地修复预览：[前后对比](comparison.html)、[结果 PNG](result.png)。

- 背景采用已经生成的完整 background_candidate，移除按硬边 mask 拼回的原图碎片。
- 人物改回 photo_feather + cover，绑定四张原照片，分别调整缩放和位移；右侧采用特写构图。
- 七个装饰仍使用旧文件。旧流程裁掉的内容已无法从现有素材缓存恢复，顶部心形等仍需重新生成。

工程修复包括背景整张重建选项、素材完整输出补边、取消按输入补边窗口回裁、抠图人物按可见 alpha 范围缩放。106 项测试通过，静态检查和审核页脚本语法检查通过。真实模型重新生成未执行：当前会话缺少 Yibu 凭据（YIBU_CREDENTIAL_MISSING）。

## 文件

- reviewed.preview.json：本地预览规格，导入旧装饰以先验证背景和人物。
- reviewed.regenerate.json：完整修复规格，七个装饰交给新生成流程，背景复用已有候选。
- bindings.json：原照片路径及本次缩放、位移参数。
- template/：当前预览模板；build/：背景、mask、素材边缘检查。
- result.png：背景和人物修复预览；原项目的模板、绑定和成图未被覆盖。
- repair-report.json：实际完成范围及未完成的模型生成状态。

## 重新生成装饰

在已经配置 YIBU_API_KEY 或 YIBU_SHARED_PATH、且本机审计代理正常运行的 PowerShell 中执行同目录的 regenerate.ps1。无需把密钥写入该脚本。

脚本只用参考图生成装饰，客户照片继续本地合成。输出为 regenerated-template/、regenerated-build/ 和 result.regenerated.png。结果仍为待检查预览，不自动发布模板。

新项目在确认页选“整张重建”，人物选“照片羽化”。需要保留原背景细节的项目继续选择“局部保护”；羽化只能柔化边界，不能修复已经重画错位的纹理。
