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
- `protectedRegions`：声明当前元素不拥有的交叠矩形。该矩形必须与至少一个其他元素区域相交；渲染器把交叠像素转交给另一个元素，不允许扣成无人绘制的空洞。
- `handPath`：保留主体的语义方向；默认显示手部素材，实际笔尖位置由当前新墨迹前沿驱动，`handPath` 不得用来制造与笔迹脱离的假移动。

## 口播与元素的映射

一条口播可以驱动多个元素。多个元素使用完全相同的 `subtitle`，并通过各自的原始 `durationMs` 分配相对绘制时长。

一个场景内按 `sequence` 读取不同字幕，必须得到连续的口播序列。例如元素字幕为 `A, A, B, C, C`，该场景消费三条口播 `A, B, C`。

不要把一条口播拆成几个不完整的 `subtitle`。不要让不同口播共用模糊摘要。

## 区域设计

默认 `semantic` 中，`region` 同时负责口播映射、主体顺序和唯一像素归属。每个区域必须覆盖一个完整语义主体，例如完整人物、动物、设备、建筑群或一组不可拆分的因果图标；不得用等宽竖条或横条切穿同一对象。

- 按“场景铺垫 → 主体 → 动作/冲突 → 结果”组织顺序；每个主体执行完“完整描线 → 完整上色”后再进入下一主体。
- 每个矩形只覆盖与当前事件有关的完整可见主体，不要仅按对象在画布上的左右位置切割。
- 区域不要大到吞掉后续主体，也不要小到漏掉轮廓。
- 对存在遮挡或相交的主体，优先在先画元素上用 `protectedRegions` 保护后画主体。旧标注若把前画主体写进后画元素的 `protectedRegions`，`exclusive-owner-v2` 会把交叠像素归还给前画主体。
- 每个交叠像素必须有且只有一个绘制元素负责。渲染后逐幕报告中的 `coverageGapPixels` 必须为 `0`。
- 可把全画布 `paper-context` 设为序列 1，用于接收主体区域之外的纸张纹理、背景辅助线和零散像素。它的原始 `durationMs` 只占很小权重，且后续主体区域拥有重叠像素，避免开场一次揭示整张图。
- 只有用户明确要求整图扫描幕时才使用 `spatial-scan`。该模式不依赖主体矩形控制揭示顺序，但仍沿用同一配音与字幕主时间轴。
