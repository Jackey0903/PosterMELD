# Paper2Poster

> A multi-agent pipeline that turns research papers into editable academic posters.

Paper2Poster 是一个从论文 PDF 自动生成可编辑 PowerPoint 学术海报的系统。项目围绕论文理解、内容策展、模板适配、布局优化、视觉资产处理和 PPTX 渲染构建了一套清晰的多智能体流水线，目标是让论文海报生成从“能跑通”进一步走向“可选择模板、可自检、可修正、可稳定输出”。

当前代码已经跑通从 `PDF -> PPTX` 的主流程，并完成了三类核心能力接入：

- 图片生成与编辑工具：`ImageTools`
- 布局模板工具：`LayoutTemplates`
- PPTX 操作工具：`PPTXDirector`

项目目前适合交给学长重点评审这些问题：pipeline 设计是否合理、模板适配是否足够稳定、视觉资产 agent 是否需要进一步增强、最终海报审美和可读性如何继续提升。

## Current Status

当前实现状态：

- 已完成 PDF 解析、内容抽取、图表抽取和视觉资产 registry。
- 已完成主链 agent 编排，并通过 LangGraph 串联。
- 已完成 `ImageTools / LayoutTemplates / PPTXDirector` 三个封装工具的实际接入。
- 已完成三种内置版式的手动选择与微调：
  - `three_column_postergen`
  - `two_plus_one_mixed`
  - `one_plus_two_mixed`
- 已接入四个竖版模板：
  - `cluster_0`
  - `cluster_1`
  - `cluster_2`
  - `cluster_3`
- 已加入确定性的 `micro_layout_refiner`，用于避免重叠、溢出和栏内越界。
- 已加入 `visual_legibility_reviewer` 和 `vlm_layout_reviewer`，用于竖版模板的图像可读性和整体布局质检。
- 已修复竖版模板中标题与会议 logo 重叠的问题。
- 当前测试结果：`41 passed, 2 warnings`。

四个竖版模板在同一篇 demo 论文上已经重新跑通，当前检查结果：

| Template | Output | Micro Layout | Force Fit | VLM |
| --- | --- | --- | --- | --- |
| `cluster_0` | `output/0409_demo_portrait_cluster_0/` | `0 issues` | `false` | `accept=true` |
| `cluster_1` | `output/0409_demo_portrait_cluster_1/` | `0 issues` | `false` | `accept=true` |
| `cluster_2` | `output/0409_demo_portrait_cluster_2/` | `0 issues` | `false` | `accept=true` |
| `cluster_3` | `output/0409_demo_portrait_cluster_3/` | `0 issues` | `false` | `accept=true` |

## Demo Preview

下面三张图是三种内置模板的真实生成效果截图，方便快速了解当前版本的输出形态。

### Three Column

![three-column](docs/assets/paper2poster_three.pptx.png)

### Two Plus One

![two-plus-one](docs/assets/paper2poster_two_plus_one.pptx.png)

### One Plus Two

![one-plus-two](docs/assets/paper2poster_one_plus_two.pptx.png)

四个竖版模板的总览图：

![portrait-template-contact-sheet](docs/assets/portrait_templates_contact_sheet.png)

## Pipeline Overview

当前主 workflow 定义在 [src/workflow/pipeline.py](src/workflow/pipeline.py)。

```text
PDF
  -> parser
  -> affiliation_logo_agent
  -> curator
  -> template_block_planner
  -> color_agent
  -> section_title_designer
  -> layout_with_balancer
  -> font_agent
  -> micro_layout_refiner
  -> visual_asset_agent
  -> renderer
  -> visual_legibility_reviewer
  -> vlm_layout_reviewer
  -> optional repair
  -> final renderer
  -> PPTX / PNG
```

整体设计思想：

- 上游负责论文理解和信息抽取。
- 中游负责内容选择、模板适配、布局优化和字体设计。
- 下游负责视觉资产解析、PPTX 渲染和质量检查。
- `renderer` 只负责渲染，不再负责临场决定图片如何处理。

### Pipeline Stages

| Stage | File | Responsibility | Main Outputs |
| --- | --- | --- | --- |
| `parser` | `src/agents/parser.py` | 解析 PDF，抽取正文、图、表、DOI、机构信息 | `raw.md`, `structured_sections.json`, `classified_visuals.json`, `visual_assets.json` |
| `affiliation_logo_agent` | `src/agents/affiliation_logo_agent.py` | 根据机构名解析或下载机构 logo | `affiliation_logos.json` |
| `curator` | `src/agents/curator.py` | 将论文内容组织成适合海报表达的 story board | `story_board.json`, `narrative_content.json` |
| `template_block_planner` | `src/agents/template_block_planner.py` | 对 `cluster_*` 竖版模板做 slot 级内容映射 | `template_block_plan.json` |
| `color_agent` | `src/agents/color_agent.py` | 生成主题色、辅助色和文字颜色 | `color_scheme.json` |
| `section_title_designer` | `src/agents/section_title_designer.py` | 生成章节标题视觉样式 | `section_title_design.json` |
| `layout_with_balancer` | `src/agents/layout_with_balancer.py` | 初始布局、列平衡、最终布局 | `initial_layout_data.json`, `optimized_story_board.json`, `final_design_layout.json` |
| `font_agent` | `src/agents/font_agent.py` | 注入字体、字号、关键词高亮和排版接口 | `styled_layout.json`, `keywords.json`, `styling_interfaces.json` |
| `micro_layout_refiner` | `src/agents/micro_layout_refiner.py` | 确定性微调，保证不重叠、不溢出 | `micro_layout_report.json`, updated `styled_layout.json` |
| `visual_asset_agent` | `src/agents/visual_asset_agent.py` | 将视觉 slot 绑定到最终可渲染图片 | `visual_plan.json`, `resolved_visual_assets.json` |
| `renderer` | `src/agents/renderer.py` | 生成可编辑 PowerPoint 文件和预览图 | `.pptx`, `.png` |
| `visual_legibility_reviewer` | `src/agents/visual_legibility_reviewer.py` | 检查 figure/table 内文字可读性 | `visual_legibility_review.json` |
| `vlm_layout_reviewer` | `src/agents/vlm_layout_reviewer.py` | 使用 VLM 对海报截图做布局验收和安全修补 | `vlm_layout_review.json` |

## Repository Layout

当前目录可以按功能分成以下几类。

```text
paper2poster/
├── assets/                 # 会议 logo 等静态素材
├── config/                 # 全局配置与 prompt
│   ├── poster_config.yaml
│   └── prompts/
├── data/                   # 输入论文和模板评测输入目录
├── docs/                   # 设计说明、流程说明、截图素材
│   └── assets/
├── fonts/                  # PPTX 渲染使用的字体
├── output/                 # 运行结果和中间产物，本目录不随 review 包提交
├── src/
│   ├── agents/             # 各阶段 agent
│   ├── config/             # 配置加载
│   ├── layout/             # 模板选择和文本高度估计
│   ├── state/              # PosterState 状态契约
│   ├── template_extraction/# 模板抽取与 cluster 模板 registry
│   ├── tools/              # 三个封装工具
│   ├── utils/              # 会议 logo、文本清洗等工具
│   └── workflow/           # pipeline 入口
├── template/
│   ├── json/               # cluster_0..3 模板结构
│   └── picture/            # cluster_0..3 原始模板图片
├── tests/                  # 合同测试和关键回归测试
├── utils/                  # LangGraph agent 封装和日志工具
├── pyproject.toml
├── requirements.txt
└── README.md
```

### Core Code Classification

`src/agents/` 是主要业务逻辑，可以继续细分：

- 内容理解类：
  - `parser.py`
  - `curator.py`
  - `affiliation_logo_agent.py`
- 模板和布局类：
  - `template_block_planner.py`
  - `template_region_relayout.py`
  - `layout_agent.py`
  - `layout_with_balancer.py`
  - `adaptive_column_relayout.py`
  - `micro_layout_refiner.py`
- 视觉和审美类：
  - `color_agent.py`
  - `section_title_designer.py`
  - `font_agent.py`
  - `visual_asset_agent.py`
  - `visual_legibility_reviewer.py`
  - `vlm_layout_reviewer.py`
- 渲染类：
  - `renderer.py`

`src/tools/` 是底层封装能力：

- `image_api.py`：图片生成、图片编辑、裁剪缩放。
- `layout_api.py`：内置布局、竖版模板布局、抽取模板布局。
- `pptx_api.py`：PPTX 创建、文本、形状、图片、保存。

`src/template_extraction/` 是模板库能力：

- `block_template_registry.py`：加载 `cluster_0..3` 模板 JSON，转换为运行时 layout。
- `extract_templates.py`：从 poster 图片中抽取可复用模板结构。
- `registry.py`：管理抽取模板 registry。

## Key Design Points

### 1. Unified State Contract

所有 agent 都通过 `PosterState` 传递数据，定义在 [src/state/poster_state.py](src/state/poster_state.py)。

核心字段包括：

- `raw_text`
- `structured_sections`
- `classified_visuals`
- `visual_assets`
- `story_board`
- `design_layout`
- `styled_layout`
- `visual_plan`
- `resolved_visual_assets`
- `layout_template_metadata`
- `micro_layout_report`

这样可以避免每个 agent 各自读写临时字段，降低 pipeline 后期接新模块的成本。

### 2. Visual Asset Registry

parser 会把论文中的 figure/table 统一整理成 `visual_assets`。

示意结构：

```json
{
  "figure_1": {
    "asset_id": "figure_1",
    "asset_type": "figure",
    "source_path": "output/.../assets/figure-1.png",
    "resolved_path": null,
    "caption": "Figure caption",
    "aspect": 1.6,
    "provenance": "paper_extracted"
  }
}
```

后续渲染不再直接读 parser 的临时图片路径，而是走：

```text
visual_assets -> visual_asset_agent -> resolved_visual_assets -> renderer
```

### 3. Renderer Is Deliberately Dumb

`renderer` 只消费：

- `styled_layout`
- `resolved_visual_assets`

它不再负责判断：

- 该不该裁剪图片
- 该不该编辑图片
- 该不该生成新图片
- 图片和 slot 怎么匹配

这些决策被集中放到 `visual_asset_agent`，这样后续增强图片生成和编辑能力时，不需要继续改 PPTX 渲染器。

### 4. Micro Layout Refiner

`micro_layout_refiner` 是当前稳定性的核心模块。它不是 LLM，而是确定性几何后处理。

目标：

- section 不重叠
- 子元素不溢出 section container
- section 不超出 lane
- 字号、间距、图片缩放在安全范围内收敛

验收文件：

```text
output/<poster_name>/content/micro_layout_report.json
```

验收标准：

```text
validation.issues == []
```

### 5. Template Prior Mode

对于 `cluster_0..3` 这类竖版模板，系统不会简单回退到横版三栏，而是进入 `template_prior` 模式：

- 读取 `template/json/cluster_*_template.json`
- 识别 header slot 和 content slots
- 将论文内容映射到模板 slot
- 通过 `template_block_planner` 进行内容压缩和 slot 分配
- 通过 VLM review 和 template region relayout 做一次安全修复

当前四个竖版模板默认画布为：

```text
36 x 50.876 in
```

四个模板的当前定位：

- `cluster_0`：多 block 竖版，适合内容较多、方法和结果都要展示的 poster。
- `cluster_1`：左右交错式竖版，适合分区明显、想突出结构变化的 poster。
- `cluster_2`：上方双块 + 中下大块，适合突出一个核心方法图和一个主要结果表。
- `cluster_3`：右侧长块 + 底部大块，适合一侧放背景/动机，底部突出核心方法或结果。

## Supported Templates

查看可用模板：

```bash
uv run python -m src.workflow.pipeline --list-layout-templates
```

当前输出包括：

```text
auto
adaptive_three_column
cluster_0
cluster_1
cluster_2
cluster_3
one_plus_two_mixed
single_column_vertical
three_column_postergen
two_plus_one_mixed
```

推荐优先级：

1. `three_column_postergen`：最稳定，适合作为 baseline。
2. `cluster_2`：当前竖版模板中综合效果最好，适合展示模板适配能力。
3. `cluster_0 / cluster_1 / cluster_3`：均已跑通，可用于说明模板库扩展能力。
4. `two_plus_one_mixed / one_plus_two_mixed`：可手动选择，但需要继续优化视觉比例和文字密度。
5. `single_column_vertical`：已实现，但观感一般，不建议作为主要展示模板。

## Installation

推荐环境：

- Python `3.11`
- `uv`

安装依赖：

```bash
uv sync
```

项目依赖中包含 PDF 解析、PPTX 渲染、图像处理、LangGraph、LangChain 和测试工具。第一次安装会创建本地 `.venv/`，依赖体积较大，建议在网络稳定的环境下执行。

## Configuration

项目从根目录 `.env` 读取模型密钥。可以参考 `.env.example` 创建本地配置。

最小配置：

```bash
OPENAI_API_KEY=your_key_here
```

如果启用 VLM review，还需要：

```bash
VLM_API_KEY=your_vlm_gateway_key
VLM_BASE_URL=https://your-vlm-endpoint
VLM_MODEL=gpt-5.1
```

其他可选模型提供商：

- `ANTHROPIC_API_KEY`
- `GOOGLE_API_KEY`
- `ZHIPU_API_KEY`
- `MOONSHOT_API_KEY`
- `MINIMAX_API_KEY`
- `ALIBABA_API_KEY`

注意：`.env` 已经在 `.gitignore` 中，不要提交给别人。

## Quick Start

### Run Baseline Three-Column Poster

```bash
uv run python -m src.workflow.pipeline \
  --paper_path ./data/0409_demo/paper.pdf \
  --text_model gpt-4.1-2025-04-14 \
  --vision_model gpt-4.1-2025-04-14 \
  --layout-template three_column_postergen \
  --logo '' \
  --aff_logo ''
```

### Run Mixed Layouts

```bash
uv run python -m src.workflow.pipeline \
  --paper_path ./data/0409_demo/paper.pdf \
  --text_model gpt-4.1-2025-04-14 \
  --vision_model gpt-4.1-2025-04-14 \
  --layout-template two_plus_one_mixed \
  --logo '' \
  --aff_logo ''
```

```bash
uv run python -m src.workflow.pipeline \
  --paper_path ./data/0409_demo/paper.pdf \
  --text_model gpt-4.1-2025-04-14 \
  --vision_model gpt-4.1-2025-04-14 \
  --layout-template one_plus_two_mixed \
  --logo '' \
  --aff_logo ''
```

### Run Portrait Cluster Template

`cluster_*` 模板会自动开启视觉可读性检查和 VLM layout review。

```bash
uv run python -m src.workflow.pipeline \
  --paper_path ./data/0409_demo/paper.pdf \
  --text_model gpt-5.1 \
  --vision_model gpt-5.1 \
  --layout-template cluster_2 \
  --conference ICML
```

### Enable Visual Refinement

当前 `visual_asset_agent` 默认策略是保守的 `crop_only`。如果打开视觉增强，会允许 `edit` 或 `generate_new` 等动作。

```bash
uv run python -m src.workflow.pipeline \
  --paper_path ./data/0409_demo/paper.pdf \
  --text_model gpt-4.1-2025-04-14 \
  --vision_model gpt-4.1-2025-04-14 \
  --enable-visual-refinement
```

## Outputs

默认输出目录：

```text
output/<paper_parent_dir_name>/
```

典型结构：

```text
output/0409_demo/
├── 0409_demo.pptx
├── 0409_demo.png
├── timing_cost_log.json
├── assets/
│   ├── figure-*.png
│   ├── table-*.png
│   └── resolved/
└── content/
    ├── raw.md
    ├── structured_sections.json
    ├── classified_visuals.json
    ├── visual_assets.json
    ├── story_board.json
    ├── color_scheme.json
    ├── section_title_design.json
    ├── initial_layout_data.json
    ├── optimized_story_board.json
    ├── final_design_layout.json
    ├── styled_layout.json
    ├── micro_layout_report.json
    ├── visual_plan.json
    ├── resolved_visual_assets.json
    └── vlm_layout_review.json
```

调试时优先看：

- `content/story_board.json`：内容组织是否合理。
- `content/final_design_layout.json`：布局是否符合模板。
- `content/styled_layout.json`：字体和元素位置。
- `content/micro_layout_report.json`：是否有重叠或溢出。
- `content/visual_plan.json`：图片处理策略。
- `content/resolved_visual_assets.json`：最终渲染图片路径。
- `timing_cost_log.json`：耗时、API 调用和 token 统计。

## Testing

运行合同测试：

```bash
uv run python -m pytest tests/test_pipeline_contracts.py -q
```

当前结果：

```text
41 passed, 2 warnings
```

测试覆盖重点：

- parser 视觉资产 registry 是否正确。
- 机构 logo 提取和 fallback 是否可用。
- 标题区和会议 logo 是否避免重叠。
- `cluster_0..3` 模板 registry 是否可加载。
- `LayoutTemplates` 是否支持内置模板和竖版模板。
- `visual_asset_agent` 默认 `crop_only` 是否保持 slot-preserving。
- VLM reviewer 是否使用 responses endpoint。
- `micro_layout_refiner` 是否能处理 overflow 和 underflow。
- 自适应栏宽是否可触发并保存决策。

## What To Review

如果请学长看代码，建议重点看以下模块。

### 1. Pipeline Architecture

文件：

- [src/workflow/pipeline.py](src/workflow/pipeline.py)
- [src/state/poster_state.py](src/state/poster_state.py)

问题：

- LangGraph 节点和条件边是否设计合理。
- `PosterState` 是否过大，是否需要拆分成更清晰的数据结构。
- draft/final render、VLM repair、template repair 的状态流是否还能简化。

### 2. Template Adaptation

文件：

- [src/template_extraction/block_template_registry.py](src/template_extraction/block_template_registry.py)
- [src/agents/template_block_planner.py](src/agents/template_block_planner.py)
- [src/agents/template_region_relayout.py](src/agents/template_region_relayout.py)
- [src/tools/layout_api.py](src/tools/layout_api.py)

问题：

- `cluster_*` 模板的 slot 识别、内容映射和压缩策略是否足够通用。
- 当前模板先验是否能扩展到更多真实 poster 模板。
- VLM gate 对 whitespace、visual readability、overflow 的判定是否过松或过严。

### 3. Layout Robustness

文件：

- [src/agents/layout_agent.py](src/agents/layout_agent.py)
- [src/agents/layout_with_balancer.py](src/agents/layout_with_balancer.py)
- [src/agents/micro_layout_refiner.py](src/agents/micro_layout_refiner.py)
- [src/layout/text_height_measurement.py](src/layout/text_height_measurement.py)

问题：

- 文本高度估计是否足够接近 PPT 实际渲染。
- `micro_layout_refiner` 是否应该继续用确定性规则，还是引入更强的优化器。
- force-fit 是否应该作为失败信号，而不是可接受输出。

### 4. Visual Asset Agent

文件：

- [src/agents/visual_asset_agent.py](src/agents/visual_asset_agent.py)
- [src/tools/image_api.py](src/tools/image_api.py)
- [src/agents/visual_legibility_reviewer.py](src/agents/visual_legibility_reviewer.py)

问题：

- 当前默认 `crop_only` 保守策略是否足够。
- 什么时候应该允许 `edit`。
- 什么时候应该允许 `generate_new`。
- `add_new / drop` 是否真的需要触发 layout reflow。

### 5. Rendering Quality

文件：

- [src/agents/renderer.py](src/agents/renderer.py)
- [src/tools/pptx_api.py](src/tools/pptx_api.py)

问题：

- PPTX 中字体、颜色、图层顺序、标题样式是否稳定。
- 生成的 PowerPoint 是否足够可编辑。
- 是否需要加入更多 shape style 或模板背景。

## Known Limitations

当前版本已经能跑通并生成可编辑 PPTX，但仍有明显改进空间：

1. `visual_asset_agent` 还没有真正形成强视觉增强策略，默认主要是 `crop_only`。
2. 混合栏模板能跑通，但在审美上不一定优于三栏。
3. `single_column_vertical` 已实现，但实际观感较弱，不建议重点展示。
4. 竖版模板依赖 VLM review，模型或中转站不稳定时可能影响完整运行。
5. 当前测试主要是合同测试和局部回归，端到端 fixture 还不够系统。
6. 运行输出默认写入 `output/`，该目录属于可再生成产物，review 包中默认不包含。

## Suggested Next Steps

短期建议：

1. 固定一篇 demo paper，整理最小可复现脚本。
2. 选择 `three_column_postergen` 和 `cluster_2` 作为主要展示模板。
3. 增加端到端测试，至少覆盖：
   - 无图论文
   - 多表格论文
   - 超宽图论文
   - 有会议 logo
   - 有机构 logo
4. 把 `visual_asset_agent` 从 `crop_only` 扩展到真正的 `edit / generate_new` 决策。
5. 建立模板质量指标，例如：
   - overflow issue 数量
   - force-fit 是否触发
   - VLM score
   - 可读性问题数量

中期建议：

1. 做一个模板选择器，根据论文内容自动选择模板。
2. 将 `micro_layout_refiner` 改造成更标准的约束优化模块。
3. 对 figure/table 做更细粒度的重绘或局部放大。
4. 将中间产物可视化，方便 debug 每个 agent 的输出。
5. 恢复或重做 Web UI，用于上传 PDF、选择模板、预览结果。

## Review Package Notes

发给学长看代码时，建议包含：

- `src/`
- `config/`
- `template/`
- `tests/`
- `docs/`
- `README.md`
- `pyproject.toml`
- `uv.lock`
- `requirements.txt`

不建议直接发送：

- `.env`
- `.venv/`
- `.git/`
- `.pytest_cache/`
- `__pycache__/`
- `.DS_Store`
- `output/`

当前最小可运行 demo 输入：

- `data/0409_demo/paper.pdf`

运行后结果会重新生成到：

- `output/0409_demo/`

## License

MIT. See [LICENSE](LICENSE).
