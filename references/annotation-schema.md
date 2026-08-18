# 白板场景标注规范

每张场景图必须有一个同名的 `.annotation.json`。例如：

```text
scene-01.png
scene-01.annotation.json
```

最小结构：

```json
{
  "sceneId": "scene-01",
  "canvas": { "width": 1920, "height": 1080 },
  "storyBasis": "这一幕要表达的单一认知结论",
  "sceneDurationMs": 30000,
  "elements": [
    {
      "id": "street-context",
      "label": "街边场景",
      "sequence": 1,
      "narrativeRole": "铺垫使用环境",
      "subtitle": "完整复制对应的一条口播。",
      "type": "structure",
      "region": { "x": 0, "y": 80, "width": 500, "height": 850 },
      "reveal": {
        "direction": "top_to_bottom",
        "startMs": 200,
        "durationMs": 4200,
        "maskPaddingPx": 22,
        "protectedRegions": []
      },
      "handPath": {
        "start": [250, 100],
        "end": [250, 900],
        "easing": "easeInOut"
      }
    }
  ]
}
```

## 字段规则

- `sceneId`：稳定场景标识。
- `canvas`：必须与源图真实像素尺寸一致。
- `storyBasis`：说明这幕的叙事结论。
- `sceneDurationMs`：原始标注中可用估算值；时间轴脚本会覆盖。
- `elements`：按叙事先后排序的可绘制区域。
- `id`：场景内唯一、稳定、使用英文短横线命名。
- `label`、`narrativeRole`：用中文说明主体与作用。
- `subtitle`：完整复制一条 TTS 口播。不要摘录，不要换标点。
- `type`：可用 `structure`、`object`、`action`、`result`、`label`。
- `region`：原图坐标系内的整数矩形，必须完全落在画布内。
- `reveal.startMs`：表达初始先后；最终由真实音轨覆盖。
- `reveal.durationMs`：同一口播包含多个元素时，作为绘制时长权重。
- `protectedRegions`：从当前区域揭示掩码中扣除的矩形，用于保护后绘制主体。
- `handPath`：保留语义方向；当前成片默认 `bare-tip`，不显示手部素材。

## 口播与元素的映射

一条口播可以驱动多个元素。多个元素使用完全相同的 `subtitle`，并通过各自的原始 `durationMs` 分配相对绘制时长。

一个场景内按 `sequence` 读取不同字幕，必须得到连续的口播序列。例如元素字幕为 `A, A, B, C, C`，该场景消费三条口播 `A, B, C`。

不要把一条口播拆成几个不完整的 `subtitle`。不要让不同口播共用模糊摘要。

## 区域设计

- 按“场景铺垫 → 主体 → 动作/冲突 → 结果”组织顺序。
- 每个矩形只覆盖与当前事件有关的可见主体。
- 区域不要大到吞掉后续主体，也不要小到漏掉轮廓。
- 对存在遮挡或相交的主体，先画元素的 `protectedRegions` 应覆盖后画主体。
- 大背景可先画，但要避免一次揭示整张图，使后续没有可见变化。
