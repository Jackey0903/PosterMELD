# Paper2Poster 组会汇报提纲

## 1. 本阶段目标

本阶段主要目标是把 Paper2Poster 从“能生成海报”推进到“流程更稳定、模板更可控、版面更接近可展示效果”。

重点解决三个问题：

- 模板太多且质量不稳定，很多模板实际不可用。
- 内容生成和模板容量不匹配，导致 block 内留白明显或局部拥挤。
- 白底海报视觉效果偏弱，需要更像学术顶会 poster 的背景和整体风格。

## 2. 当前完整 pipeline 简化版

```text
论文 PDF
  -> 解析文本、图表、作者机构
  -> 提取 poster-worthy keypoints
  -> 选择标准模板或 adaptive_auto
  -> 根据模板 block 容量规划文字和图表
  -> 生成 sections、布局、字体、颜色和视觉资产
  -> 渲染 draft poster
  -> block 级利用率检查 + 局部/全局 VLM 审查
  -> 必要时小幅修复
  -> 基于 draft poster 生成背景图
  -> 最终输出 PPTX / PNG
```

一句话概括：

```text
先确定模板和空间容量，再生成匹配容量的内容，最后用 VLM 做验收、用 image model 做背景美化。
```

## 3. 当前实现的核心功能

### 论文解析

- 从 PDF 中抽取正文、标题、作者、机构。
- 抽取论文图像和表格，并建立 visual asset registry。
- 对图表进行初步分类，例如方法图、系统图、主结果表、补充结果等。

### 内容切块

- 使用 keypoint-first 方式，从全文中提取约 10 个适合 poster 展示的关键点。
- keypoints 不再强制一一对应模板 block，而是作为内容池。
- curator 根据模板结构把 10 个 keypoints 合并成 4-7 个 poster sections。
- 文字后处理会清理不适合放上海报的内容，例如外部 Table/Figure 编号、Supplement/Algorithm 引用、路径乱码、截断句。

### 模板策略

- 不再默认扫描全部模板。
- 当前只保留少量相对稳定的标准模板：

```text
横版：cluster_8, cluster_10, cluster_34
竖版：cluster_2, cluster_3
```

- `auto` 模式只在标准模板里选。
- `adaptive_auto` 保留无模板自适应布局模式。
- 模板作为 soft prior，实际排版时允许 block 轻微吸收空隙和微调。

### 容量驱动生成

- 模板确定后，先计算每个 block 的可用宽高、标题高度、图表预留空间和文字容量。
- 每个 block 生成 target/min/max chars。
- 文本生成时按 block 容量控制长度，而不是生成后硬塞进模板。
- 目标利用率：

```text
target: 0.95
acceptable: 0.90 - 0.97
hard max: 0.98
final minimum: 0.88
```

### 质量检查

- draft poster 渲染后进行 block occupancy 分析。
- 对每个 block 裁剪 crop，使用 VLM 判断：

```text
empty / underfilled / ok / crowded / overflow / visual_too_small
```

- 全局 VLM 检查标题可读性、阅读顺序、整体平衡、大面积留白和视觉层级。
- fast mode 下 VLM 主要负责报告和小修建议，不再触发反复大规模重排。

### 背景生成

- 当前背景不是提前生成，而是在 draft poster 确定后生成。
- 流程：

```text
draft poster PNG
  -> 作为 reference 输入 gpt-image-2
  -> 生成同尺寸 background-only 图片
  -> 后处理降低干扰
  -> 放到最终 PPT 底层
```

- 最近测试了科技顶会风背景，包括：
  - tech grid 风格
  - geospatial contour 风格
  - premium gradient 风格

## 4. 当前效果

以 HAGS 论文为例，当前横版 `cluster_34` 已经能稳定生成：

- 6 个主要内容 block。
- 2 张核心图 + 2 个结果表。
- 学校 logo 和会议 logo 可见。
- 生成可编辑 PPTX 和预览 PNG。
- 平均 block 利用率约 95%。
- micro-layout 无 overlap / overflow。
- 背景从纯白升级为 poster-conditioned 科技学术淡色背景。

## 5. 当前主要问题

### 模板仍然是瓶颈

- 很多模板视觉上看起来可用，但实际 block 比例不适合论文内容。
- 竖版模板目前效果不稳定，容易出现图表小、文字挤、阅读顺序弱。
- 目前更合理的策略是维护少量高质量模板，而不是追求模板数量。

### 图表可读性仍需优化

- VLM 经常认为论文原图和表格内部文字偏小。
- 这个问题不能只靠补文字解决。
- 后续需要针对图表做更强的策略：
  - 图表优先分配大 slot。
  - 表格过宽时转成摘要表或 callout。
  - 对关键图做裁剪、局部放大或重绘。

### 背景风格还在调

- 当前背景已经不是纯白，但默认风格仍偏保守。
- 最近实验显示 `geo_contour` 和 `premium_gradient` 更接近想要的“科技顶会风”。
- 下一步应该把背景 prompt 和后处理参数进一步固定下来。

## 6. 下一步计划

- 固化 5-6 个标准美观模板，明确每个模板适合的论文类型。
- 把背景生成默认风格改成“科技顶会风”，并保留低干扰原则。
- 加强图表处理：
  - 关键图自动放大。
  - 表格太小时自动转摘要。
  - 图表区域优先于硬填文字。
- 完善最终质量报告，让系统明确说明：
  - 哪些 block 通过。
  - 哪些图表可读性不足。
  - 是否因为模板限制导致无法继续优化。

## 7. 组会可以强调的结论

当前系统已经形成了比较完整的闭环：

```text
模板约束 -> 内容规划 -> 排版渲染 -> VLM 检查 -> 背景美化 -> 最终输出
```

相比之前，核心改进是：

- 从“生成后补救”改成“模板容量先验”。
- 从“随机用模板”改成“少量标准模板 + soft 微调”。
- 从“只看整体 VLM”改成“block 级局部检查 + 全局检查”。
- 从“白底 poster”改成“poster-conditioned 背景生成”。

目前最大瓶颈不是 pipeline 是否能跑通，而是模板和图表可读性的上限。
