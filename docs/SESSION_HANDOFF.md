# PosterMELD 会话交接文档（2026-07-09）

给下一个会话的完整上下文。项目投稿 **AAAI 2027**，目标：把论文 PDF 稳定转成美观、零留白的学术海报，并批量跑 621 篇 benchmark。

---

## 1. 仓库当前状态

- 公开仓库为 `github.com/Jackey0903/PosterMELD`；生成和评测代码分别位于 `poster_generation/` 与 `benchmark_eval/`。
- 生成模块完整测试：**271 passed**（在 `poster_generation/` 内运行 `PYTHONPATH=. python -m pytest -q`）。
- 安全备份分支 `wip/adr-stabilization-snapshot`（4b7ab3b）保留。
- 设计事实来源：`poster_generation/CONTEXT.md`（术语）+ `docs/adr/0001-0008`（架构决策）。真实流程以 `poster_generation/src/workflow/pipeline.py:create_workflow_graph` 为准。默认模板 = `cluster_43_landscape`。

## 2. 本会话已完成（都已提交+推送）

| commit | 内容 |
|---|---|
| baseline | 把上一会话未提交的 ADR 稳定化(3941行)固化为 `wip/adr-stabilization-snapshot`，fast-forward main |
| docs | 详细 README 重写（真实流程/ADR/环境变量/logo/故障排查）|
| 死代码 | `cluster_72` 全仓清零（7文件；2个误名活方法改名 `_grouped_*_candidates`）|
| **端点韧性** | 推理模型(gpt-5/o系列)**只接受默认 temperature=1**（传0.7/0.1→400"operation not allowed"）；渠道路由型4xx/5xx改为**可重试**+重试预算8；**文本改流式 streaming=True**（根治重型调用被网关~30s超时的502）；curator story board **缓存/keypoint 回退** |
| 色块统一 | 正常路径 title_accent_block 强制统一 0.78（`micro_layout_refiner` ~808行）|
| force-fit回退 `ee1293a` | 撤销"force-fit保持色块统一"（有bug致溢出/重叠）；恢复原始压缩 |
| 机构logo | OpenAlex按标题清洗名→Clearbit autocomplete拿真域名→favicon兜底取图（Clearbit logo API已停服）；修 `_paper_title` 读 `narrative.meta.poster_title` |
| logo参数 `a9d20c5` | `--affiliation-logo-mode single|multi`（默认single=1个，multi=1-3个）|

## 3. Benchmark 成本测量结果（621篇）

`Benchmark/aaai2026/` 有5篇（每个文件夹 `paper.pdf`），完整 benchmark **621 篇**。

**成功篇实测**（AbductiveMLLM，fresh无缓存，balanced密度，关图像生成）：
- 输入 **175,795** token / 输出 **12,439** token / 10次API调用 / 270s
- **×621 外推：~1.09 亿输入 / ~770 万输出 token**（仅文本LLM+color vision；VLM图未计入日志）

**计费口径要点**：
- 图像输入实测 **~1.3k token/张**（`chat/completions` usage 实测）。日志里 color_agent 的 185k 是 langchain 数 base64 字符的**假象**，忽略。
- VLM 3个reviewer走 raw requests，**token不进日志**（每篇~3张图×1.3k≈4k，很轻）。
- **图像生成（背景/teaser）是独立按张计费、非token**；用户图像端点当前全挂(500)，贡献0。
- **最准成本 = 中转站后台的 token 消耗差值**（跑前后对比）。建议以此校准。
- 测量时设 `PAPER2POSTER_GENERATED_BACKGROUND=0 PAPER2POSTER_GENERATED_TEASER=0` 省时间（图像端点挂着会疯狂重试拖慢）。

## 4. ⚠️ 关键未解决：Benchmark 产出率只有 ~20%

跑5篇 fresh 只有 1/5 成功。**两个瓶颈**：

1. **视觉密度**：`--visual-density rich` 过度打包（method_visual块被塞2张图→141%溢出→质量门拒绝→无海报）。→ **改用 `balanced`（默认）**，同一篇立刻跑通（141%→109%，force-fit能压下）。**benchmark 必须用 balanced，不要 rich。**

2. **端点抖动（更致命）**：全新无缓存论文，curator 的 story board 生成撞上端点坏窗口就失败（日志 `missing story_board`，疯狂重试~18分钟后放弃）。curator 有缓存回退，但 fresh 论文无缓存；确定性 keypoint fallback 似乎没触发。之前"balanced成功"那篇其实用了 rich 跑剩下的缓存 story_board。
   - 用户中转站是多渠道 flaky 的，重型/长调用间歇失败。
   - **下一步方向**：①排查 curator keypoint 确定性 fallback 为何在 story board 生成失败时没兜住（应保证无缓存也能出图）；②或提高重试/换更稳端点；③考虑"benchmark宽松模式"：确定性质量失败时记录降级态但仍出图（当前 ADR 0006 是硬拒→benchmark 无法接受80%无产出）。

## 5. 其他已诊断未修

- **左下角文字溢出**：文字测量 `_measure_text_height_for_refinement` 按平均字宽估算、没算换行浪费→偏小~8%，最后一行渲染出面板。gate 用同一偏小测量没拦，VLM本该拦但端点降级。**正解**：终检加"按真实渲染高度精测、只对溢出文字块降1-2pt字号"的定向pass（不要全局调 text_height_safety_factor，试过1.12太激进会让紧块反溢出+破坏测试，已回退）。
- **logo 质量**：favicon 是中等质量兜底（圆形小校徽）。要高清可加 `LOGO_DEV_TOKEN` 支持 logo.dev。

## 6. 推荐的 benchmark 运行命令

```bash
PAPER2POSTER_GENERATED_BACKGROUND=0 PAPER2POSTER_GENERATED_TEASER=0 \
PYTHONPATH=. .venv/bin/python -m src.workflow.pipeline "<paper.pdf>" \
  --layout-template auto --conference AAAI \
  --enable-affiliation-logos --affiliation-logo-mode single \
  --poster-style navy_serif --visual-density balanced
```
批量脚本模板：`scratchpad/batch5.py`（清缓存fresh跑+聚合token+×621外推）。

## 7. 下一会话优先级建议

1. **解决 benchmark 产出率**（最高优先）：让 fresh 无缓存论文也能稳定出图——修 curator keypoint fallback 兜底 + 评估端点稳定性/宽松模式。这是能否批量跑 621 篇的前提。
2. 左下角溢出的定向修复。
3. （可选）logo.dev 高清 logo。
