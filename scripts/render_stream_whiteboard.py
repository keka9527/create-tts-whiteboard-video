#!/usr/bin/env python3
"""
SRT 白板动画 - 整合渲染器（mask 编排 + stream 画法）

把一张线稿图 + 同名 annotation.json 渲染成白板手绘动画：
  - spatial-scan（项目默认）：从左侧向右推进，每个竖向带内由上到下，
    线稿在前、颜色短暂滞后跟随，不再按矩形标注跳着揭示。
  - semantic（兼容模式）：按 sequence/startMs 顺序逐区域揭示。
    所有重叠像素先经过唯一归属计算；protectedRegions 只改变归属，不再把
    像素丢进无人绘制的遮罩死区。未开始区域仍不会提前露线。
  - 画法换成 whiteboard-stream-animation：每个区域在自己的允许掩码内，
    沿骨架/网格笔迹连续落墨（起笔 ink → 添彩 color），笔尖跟随真实笔迹，
    所有区域共享同一张持久画布，已画完的区域保留在画布上。

与 mask 的矩形擦除揭示不同：这里是「笔尖沿线滑行、边走边落墨」的连贯笔迹。
输出末行打印 OUTPUT=<路径>，便于上层捕获。

用法：
  <ENV_PY> render_stream_whiteboard.py <图片> <标注json> <输出mp4> [手部素材png]
  可选参数见 --help（--ink-path / --color-fill / --pause / --total-ms 等）。
  --total-ms 缺省时用标注里的 sceneDurationMs。
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np

# 复用 stream 渲染器的全部构件（同目录）
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
import stream_render as sr  # noqa: E402

DEFAULT_HAND = _SCRIPT_DIR.parent / "assets" / "drawing-hand.png"


# ──────────────────────────────────────────────────────────────
# 区域几何：把标注画布坐标缩放到输出尺寸
# ──────────────────────────────────────────────────────────────
def _scaled_rect(region: dict, sx: float, sy: float, out_w: int, out_h: int) -> tuple[int, int, int, int]:
    x0 = int(round(region["x"] * sx))
    y0 = int(round(region["y"] * sy))
    x1 = int(round((region["x"] + region["width"]) * sx))
    y1 = int(round((region["y"] + region["height"]) * sy))
    x0 = max(0, min(out_w, x0))
    x1 = max(0, min(out_w, x1))
    y0 = max(0, min(out_h, y0))
    y1 = max(0, min(out_h, y1))
    return x0, y0, x1, y1


def _frame_progress_indices(n_steps: int, target_frames: int) -> list[int]:
    """把 n_steps 个笔尖位置均匀映射到 target_frames 帧。"""
    if n_steps == 0 or target_frames <= 0:
        return []
    if target_frames == 1:
        return [n_steps - 1]
    return [round(f * (n_steps - 1) / (target_frames - 1)) for f in range(target_frames)]


# ──────────────────────────────────────────────────────────────
# 每区域的 stream 笔迹渲染，写入共享持久画布
# ──────────────────────────────────────────────────────────────
class RegionStreamRenderer:
    """持有整段渲染的共享状态；逐区域把 stream 笔迹画进同一张画布。"""

    def __init__(self, image_bgr: np.ndarray, annotation: dict, cfg: sr.Config,
                 hand_png: Path | None, bare_tip: bool,
                 tip_smoothing: float = 0.30) -> None:
        self.cfg = cfg
        self.ann = annotation
        self.canvas_bgr = sr._hex_to_bgr(cfg.canvas_hex)

        # 输出尺寸：长边限到 cap，对齐到 grid_edge 的偶数倍（编码要求偶数）
        h0, w0 = image_bgr.shape[:2]
        scale = cfg.cap_long_edge / max(h0, w0)
        align = cfg.grid_edge if cfg.grid_edge % 2 == 0 else cfg.grid_edge * 2
        w = max(align, (int(round(w0 * scale)) // align) * align)
        h = max(align, (int(round(h0 * scale)) // align) * align)
        self.out_w, self.out_h = w, h

        # 标注画布坐标 → 输出坐标的缩放比
        cw = annotation["canvas"]["width"]
        ch = annotation["canvas"]["height"]
        self.sx = self.out_w / cw
        self.sy = self.out_h / ch

        self.elements = sorted(
            self.ann["elements"],
            key=lambda e: (e["reveal"]["startMs"], e.get("sequence", 0)),
        )
        (
            self.allowed_masks,
            self.mask_report,
            self._legacy_dead_mask,
        ) = self._build_exclusive_owner_masks(self.elements)

        self.color_img = cv2.resize(image_bgr, (self.out_w, self.out_h), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(self.color_img, cv2.COLOR_BGR2GRAY)
        self.thresh_map = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 15, 10
        )
        self.grid_blocks = sr._to_grid_blocks(self.thresh_map, cfg.grid_edge)
        self.active_all = sr._active_mask(self.thresh_map, cfg.grid_edge, cfg.ink_threshold)
        self.ink_pixels = self.thresh_map < cfg.ink_threshold
        self.ink_paint = np.repeat(self.thresh_map[:, :, None], 3, axis=2).astype(np.float32)

        # 背景染成画布底色，让上色阶段背景与起笔一致（不碰墨迹）
        if cfg.match_bg:
            self._match_original_background()
        color_diff = np.abs(
            self.color_img.astype(np.int16) - self.canvas_bgr.astype(np.int16)
        ).sum(axis=2)
        # 背景已被归一到 canvas 色；只把真实线条/颜色加入全局描画路径，
        # 避免矩形背景边缘或“浮在空中的道路横条”。
        self.foreground_pixels = (color_diff >= 8) | self.ink_pixels

        # 共享持久画布
        self.drawn = np.empty((self.out_h, self.out_w, 3), dtype=np.float32)
        self.drawn[...] = self.canvas_bgr.astype(np.float32)

        # 笔尖覆盖
        self.tip: sr.TipOverlay | None = None
        self.tip_smoothing = float(np.clip(tip_smoothing, 0.01, 1.0))
        self._tip_position: tuple[float, float] | None = None
        if not bare_tip:
            hand_data = sr._load_hand(hand_png, cfg.target_hand_height) if hand_png else None
            ax, ay = cfg.tip_anchor_x, cfg.tip_anchor_y
            if hand_data is None:
                hand_data = sr._procedural_tip(cfg.target_hand_height)
                ax, ay = 0.5, 0.70
            self.tip = sr.TipOverlay(hand_data[0], hand_data[1], tip_anchor_x=ax, tip_anchor_y=ay)

    # 采样原图四角，把接近背景色的像素替换为画布底色
    def _match_original_background(self) -> None:
        img = self.color_img
        h, w = img.shape[:2]
        margin = max(3, min(h, w) // 50)
        samples = [img[:margin, :margin], img[:margin, -margin:],
                   img[-margin:, :margin], img[-margin:, -margin:]]
        bg = np.median(np.concatenate([s.reshape(-1, 3) for s in samples]), axis=0)
        diff = np.abs(img.astype(np.int16) - bg.astype(np.int16)).sum(axis=2)
        img[diff < self.cfg.match_bg_threshold] = self.canvas_bgr

    def _cell_center(self, cell: tuple[int, int]) -> tuple[int, int]:
        r, c = cell
        e = self.cfg.grid_edge
        return (c * e + e // 2, r * e + e // 2)

    def _snapshot_with_tip(self, px: int, py: int) -> np.ndarray:
        snap = self.drawn.astype(np.uint8)
        if self.tip is not None:
            if self._tip_position is None:
                smooth_x, smooth_y = float(px), float(py)
            else:
                old_x, old_y = self._tip_position
                alpha = self.tip_smoothing
                smooth_x = old_x + alpha * (float(px) - old_x)
                smooth_y = old_y + alpha * (float(py) - old_y)
            self._tip_position = (smooth_x, smooth_y)
            self.tip.stamp(snap, int(round(smooth_x)), int(round(smooth_y)))
        return snap

    def _rect_mask(self, region: dict) -> np.ndarray:
        mask = np.zeros((self.out_h, self.out_w), dtype=bool)
        x0, y0, x1, y1 = _scaled_rect(region, self.sx, self.sy, self.out_w, self.out_h)
        mask[y0:y1, x0:x1] = True
        return mask

    def _protected_mask(self, element: dict) -> np.ndarray:
        mask = np.zeros((self.out_h, self.out_w), dtype=bool)
        for prot in element.get("reveal", {}).get("protectedRegions", []):
            px0, py0, px1, py1 = _scaled_rect(prot, self.sx, self.sy, self.out_w, self.out_h)
            mask[py0:py1, px0:px1] = True
        return mask

    @staticmethod
    def _component_boxes(mask: np.ndarray, limit: int = 20) -> tuple[int, list[dict]]:
        count, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
        components = []
        for idx in range(1, count):
            x, y, width, height, pixels = [int(v) for v in stats[idx]]
            components.append(
                {"x": x, "y": y, "width": width, "height": height, "pixels": pixels}
            )
        components.sort(key=lambda item: item["pixels"], reverse=True)
        return len(components), components[:limit]

    def _build_exclusive_owner_masks(
        self, elements: list[dict]
    ) -> tuple[list[np.ndarray], dict, np.ndarray]:
        """Assign every pixel in the annotated union to exactly one element.

        Later regions own overlaps by default, preserving delayed reveal. A
        protected region makes the current element ineligible only where another
        annotated element can own that pixel. This also recovers old annotations
        that put an earlier subject in a later element's protectedRegions: those
        pixels return to the earlier subject instead of becoming a dead zone.
        """
        regions = [self._rect_mask(element["region"]) for element in elements]
        protected = [self._protected_mask(element) for element in elements]
        region_stack = np.stack(regions, axis=0)
        claimant_count = region_stack.sum(axis=0, dtype=np.uint16)
        region_union = claimant_count > 0

        # Reproduce the legacy policy for diagnostics only.
        legacy_masks: list[np.ndarray] = []
        later_union = np.zeros_like(region_union)
        for idx in range(len(elements) - 1, -1, -1):
            legacy = regions[idx] & ~later_union & ~protected[idx]
            legacy_masks.append(legacy)
            later_union |= regions[idx]
        legacy_masks.reverse()
        legacy_union = np.logical_or.reduce(legacy_masks)
        legacy_dead = region_union & ~legacy_union

        # protectedRegions may transfer ownership, but may never erase the last
        # remaining claimant for a pixel.
        eligible_masks: list[np.ndarray] = []
        redirected_pixels = []
        for idx, region in enumerate(regions):
            has_other_claimant = (claimant_count - region.astype(np.uint16)) > 0
            redirected = region & protected[idx] & has_other_claimant
            eligible_masks.append(region & ~redirected)
            redirected_pixels.append(int(np.count_nonzero(redirected)))

        owner = np.full((self.out_h, self.out_w), -1, dtype=np.int16)
        for idx, eligible in enumerate(eligible_masks):
            owner[eligible] = idx  # later eligible elements win overlaps

        # If annotations mutually protect the same overlap, retain deterministic
        # later-element ownership instead of allowing a visible rectangular hole.
        fallback = region_union & (owner < 0)
        fallback_pixels = int(np.count_nonzero(fallback))
        if fallback_pixels:
            for idx, region in enumerate(regions):
                owner[fallback & region] = idx

        allowed_masks = [owner == idx for idx in range(len(elements))]
        coverage_gap = region_union & (owner < 0)
        legacy_component_count, legacy_components = self._component_boxes(legacy_dead)
        report = {
            "policy": "exclusive-owner-v2",
            "canvas": {"width": self.out_w, "height": self.out_h},
            "elementCount": len(elements),
            "annotatedUnionPixels": int(np.count_nonzero(region_union)),
            "legacyDeadZonePixels": int(np.count_nonzero(legacy_dead)),
            "legacyDeadZoneComponentCount": legacy_component_count,
            "legacyDeadZones": legacy_components,
            "mutualProtectionFallbackPixels": fallback_pixels,
            "coverageGapPixels": int(np.count_nonzero(coverage_gap)),
            "elements": [
                {
                    "id": element.get("id", f"element-{idx + 1}"),
                    "ownedPixels": int(np.count_nonzero(allowed_masks[idx])),
                    "protectedPixelsReassigned": redirected_pixels[idx],
                }
                for idx, element in enumerate(elements)
            ],
        }
        return allowed_masks, report, legacy_dead

    def write_mask_diagnostics(self, report_path: Path | None, preview_path: Path | None) -> None:
        if report_path is not None:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(self.mask_report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        if preview_path is not None:
            preview_path.parent.mkdir(parents=True, exist_ok=True)
            preview = self.color_img.copy()
            preview[self._legacy_dead_mask] = (40, 40, 235)
            for box in self.mask_report["legacyDeadZones"]:
                cv2.rectangle(
                    preview,
                    (box["x"], box["y"]),
                    (box["x"] + box["width"], box["y"] + box["height"]),
                    (0, 0, 255),
                    3,
                )
            ok, encoded = cv2.imencode(".png", preview)
            if not ok:
                raise RuntimeError(f"无法编码遮罩诊断图: {preview_path}")
            encoded.tofile(str(preview_path))

    # ── 区域内笔迹路径 ──
    def _region_grid_path(self, allowed: np.ndarray) -> list[tuple[int, int]]:
        """网格模式：把区域内含墨的格聚类并串成连续格路径。"""
        allowed_u8 = allowed.astype(np.uint8)
        allowed_cell = sr._to_grid_blocks(allowed_u8, self.cfg.grid_edge).any(axis=(2, 3))
        active = self.active_all & allowed_cell
        if not active.any():
            return []
        streams = sr.cluster_ink_streams(active)
        return sr.flatten_streams(streams)

    def _region_skeleton_strokes(self, allowed: np.ndarray) -> list[list[tuple[int, int]]]:
        """骨架模式：区域内墨迹细化 + 8 邻接追踪 + 重采样平滑。"""
        cfg = self.cfg
        region_ink = self.ink_pixels & allowed
        if not region_ink.any():
            return []
        skel = sr._zhang_suen_skeleton(region_ink, max_iterations=160)
        raw = sr.trace_8connected(skel, min_points=cfg.skeleton_min_points)
        if not raw:
            return []
        spacing = cfg.skeleton_resample_spacing
        out: list[list[tuple[int, int]]] = []
        for stroke in raw:
            pts = [(float(x), float(y)) for x, y in stroke]
            pts = sr._resample_stroke_points(pts, spacing)
            pts = sr._chaikin_smooth(pts, iterations=1)
            pts = sr._resample_stroke_points(pts, spacing)
            if len(pts) >= 2 and sr._stroke_cumulative_length(pts)[-1] > 2.0:
                out.append([(int(round(x)), int(round(y))) for x, y in pts])
        return sr._order_skeleton_strokes(out)

    # ── 落墨（限制在 allowed 内）──
    def _reveal_ink_segment(self, a: tuple[int, int], b: tuple[int, int], allowed: np.ndarray) -> None:
        seg = np.zeros((self.out_h, self.out_w), dtype=np.uint8)
        thick = max(1, self.cfg.ink_reveal_radius * 2 + 1)
        cv2.line(seg, a, b, 255, thickness=thick, lineType=cv2.LINE_AA)
        revealed = (seg > 0) & self.ink_pixels & allowed
        self.drawn[revealed] = self.ink_paint[revealed]

    def _ink_stamp_cell(self, cell: tuple[int, int], allowed: np.ndarray) -> None:
        r, c = cell
        e = self.cfg.grid_edge
        block = self.grid_blocks[r, c]
        allow_block = allowed[r * e:r * e + e, c * e:c * e + e]
        ink_region = (block < self.cfg.ink_threshold) & allow_block
        paint = np.repeat(block[:, :, None], 3, axis=2)
        target = self.drawn[r * e:r * e + e, c * e:c * e + e]
        target[ink_region] = paint[ink_region]

    def _color_stamp_cell(self, cell: tuple[int, int], allowed: np.ndarray) -> None:
        r, c = cell
        e = self.cfg.grid_edge
        y0, y1 = r * e, r * e + e
        x0, x1 = c * e, c * e + e
        reveal = allowed[y0:y1, x0:x1]
        target = self.drawn[y0:y1, x0:x1]
        source = self.color_img[y0:y1, x0:x1].astype(np.float32)
        target[reveal] = source[reveal]

    def _spatial_scan_path(self, band_px: int) -> list[tuple[int, int]]:
        """Return a continuous diagonal frontier: left first, upper pixels slightly ahead."""
        edge = self.cfg.grid_edge
        blocks = sr._to_grid_blocks(self.foreground_pixels.astype(np.uint8), edge)
        active = blocks.any(axis=(2, 3))
        rows, cols = active.shape
        cells = [(row, col) for row in range(rows) for col in range(cols) if active[row, col]]
        # A bottom pixel trails the top by roughly band_px. Unlike discrete
        # vertical bands, this never jumps back to the top or creates a hard
        # horizontal strip at each band boundary.
        vertical_slope = max(0.0, float(band_px)) / max(1.0, float(self.out_h))
        cells.sort(
            key=lambda cell: (
                cell[1] * edge + cell[0] * edge * vertical_slope,
                cell[1],
                cell[0],
            )
        )
        return cells

    def _color_stamp(self, px: int, py: int, disk: np.ndarray, allowed: np.ndarray) -> None:
        radius = self.cfg.brush_radius
        h, w = self.out_h, self.out_w
        y0, y1 = max(0, py - radius), min(h, py + radius + 1)
        x0, x1 = max(0, px - radius), min(w, px + radius + 1)
        if y1 <= y0 or x1 <= x0:
            return
        by0, by1 = y0 - (py - radius), disk.shape[0] - ((py + radius + 1) - y1)
        bx0, bx1 = x0 - (px - radius), disk.shape[1] - ((px + radius + 1) - x1)
        m = disk[by0:by1, bx0:bx1] * allowed[y0:y1, x0:x1]
        inv = 1.0 - m
        target = self.drawn[y0:y1, x0:x1]
        source = self.color_img[y0:y1, x0:x1].astype(np.float32)
        for ch in range(3):
            target[:, :, ch] = target[:, :, ch] * inv + source[:, :, ch] * m

    # ── 起笔段（骨架模式）：沿笔迹逐段揭原图墨迹，无块填充 ──
    def _lay_ink(self, writer, frames: int, samples: list[tuple[int, int]],
                 pen_lifts: set[int], allowed: np.ndarray) -> None:
        if frames <= 0:
            return
        n = len(samples)
        if n == 0:
            for _ in range(frames):
                writer.write(self._snapshot_with_tip(self.out_w // 2, self.out_h // 2))
            return
        idx_for_frame = _frame_progress_indices(n, frames)
        last: int | None = None
        for si in idx_for_frame:
            if last is None:
                self._reveal_ink_segment(samples[si], samples[si], allowed)
            else:
                for k in range(last + 1, si + 1):
                    if k in pen_lifts:
                        continue
                    self._reveal_ink_segment(samples[k - 1], samples[k], allowed)
            sx, sy = samples[si]
            writer.write(self._snapshot_with_tip(sx, sy))
            last = si

    # ── 添彩段：brush 或 contour-wipe，限制在 allowed 内 ──
    def _wash_brush(self, writer, frames: int, centers: list[tuple[int, int]], allowed: np.ndarray) -> None:
        if frames <= 0:
            return
        n = len(centers)
        if n == 0:
            for _ in range(frames):
                writer.write(self._snapshot_with_tip(self.out_w // 2, self.out_h // 2))
            return
        disk = sr._feathered_disk(self.cfg.brush_radius)
        idx_for_frame = _frame_progress_indices(n, frames)
        last: int | None = None
        for ci in idx_for_frame:
            if last is None:
                self._color_stamp(*centers[ci], disk, allowed)
            else:
                for k in range(last + 1, ci + 1):
                    self._color_stamp(*centers[k], disk, allowed)
            cx, cy = centers[ci]
            writer.write(self._snapshot_with_tip(cx, cy))
            last = ci

    def _wash_contour(self, writer, frames: int, allowed: np.ndarray) -> None:
        if frames <= 0:
            return
        cfg = self.cfg
        ys_all, xs_all = np.where(allowed)
        if ys_all.size == 0:
            return
        top, bottom = int(ys_all.min()), int(ys_all.max())
        left, right = int(xs_all.min()), int(xs_all.max())
        region_h = bottom - top + 1
        region_w = right - left + 1

        # 区域内的阻力场（墨线膨胀 + 模糊 + 逐行向下衰减）
        ink_u8 = ((self.ink_pixels & allowed)[top:bottom + 1, left:right + 1].astype(np.uint8)) * 255
        spread = int(np.clip(min(region_w, region_h) // 32, 3, 17))
        if spread % 2 == 0:
            spread = max(3, spread - 1)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (spread, spread))
        dilated = cv2.dilate(ink_u8, kernel, iterations=1)
        blur_r = max(1, int(round(min(region_w, region_h) / 220.0)))
        if blur_r % 2 == 0:
            blur_r += 1
        resistance = cv2.GaussianBlur(dilated, (blur_r, blur_r), 0).astype(np.float32)
        peak = float(resistance.max())
        resistance = resistance / peak if peak > 1e-6 else np.zeros_like(resistance)
        decay = cfg.wipe_decay
        for row in range(1, region_h):
            resistance[row] = np.maximum(resistance[row], resistance[row - 1] * decay)

        wave = sr._build_wipe_wave(region_w)
        delay_px = int(np.clip(region_h * cfg.wipe_delay_ratio, 12, 52))
        ys = np.arange(region_h, dtype=np.float32)[:, None]
        sweep = region_h + 2 * delay_px
        blocks = max(1, cfg.wipe_blocks)

        allowed_crop = allowed[top:bottom + 1, left:right + 1]
        color_crop = self.color_img[top:bottom + 1, left:right + 1].astype(np.float32)
        drawn_crop = self.drawn[top:bottom + 1, left:right + 1]

        for fi in range(frames):
            progress = 1.0 if frames == 1 else fi / (frames - 1)
            if cfg.motion_profile == "smooth":
                motion_progress = (fi + 1) / frames
            else:
                motion_progress = sr._ease_in_out_sine(progress)
            lead = motion_progress * sweep - delay_px
            threshold = lead + wave[None, :] - resistance * delay_px
            reveal = (ys <= threshold) & allowed_crop
            drawn_crop[reveal] = color_crop[reveal]

            lane = sr._ease_in_out_sine((fi / blocks * 2.0) % 1.0)
            forward = (int(fi // blocks) % 2 == 0)
            cx = int(lane * region_w) if forward else int((1.0 - lane) * region_w)
            cx = max(0, min(region_w - 1, cx))
            col = np.where(reveal[:, cx])[0]
            cy = int(col[-1]) if col.size > 0 else 0
            writer.write(self._snapshot_with_tip(left + cx, top + cy))

        # 收尾：确保区域内允许像素全部揭示
        drawn_crop[allowed_crop] = color_crop[allowed_crop]

    # ── 网格路径的采样计划（插值 + 抬笔 + 块填充索引）──
    def _grid_plan(self, path: list[tuple[int, int]]):
        samples: list[tuple[int, int]] = []
        pen_lifts: set[int] = set()
        sample_cell: list[int] = []
        for idx, cell in enumerate(path):
            cx, cy = self._cell_center(cell)
            if idx == 0:
                samples.append((cx, cy))
                sample_cell.append(idx)
                continue
            prev_cell = path[idx - 1]
            prev = self._cell_center(prev_cell)
            if math.hypot(cell[0] - prev_cell[0], cell[1] - prev_cell[1]) > math.sqrt(2):
                pen_lifts.add(len(samples))
                samples.append((cx, cy))
                sample_cell.append(idx)
                continue
            steps = max(1, int(math.hypot(cx - prev[0], cy - prev[1]) / self.cfg.sample_step))
            for s in range(1, steps + 1):
                samples.append((int(prev[0] + (cx - prev[0]) * s / steps),
                                int(prev[1] + (cy - prev[1]) * s / steps)))
                sample_cell.append(idx)
        return samples, pen_lifts, sample_cell

    # ── 主渲染 ──
    def render_to(
        self,
        raw_path: Path,
        total_ms: int,
        reveal_order: str = "semantic",
        spatial_band_px: int = 180,
        spatial_color_lag_ms: int = 320,
    ) -> Path:
        if reveal_order == "spatial-scan":
            return self._render_spatial_scan_to(
                raw_path,
                total_ms,
                band_px=spatial_band_px,
                color_lag_ms=spatial_color_lag_ms,
            )
        return self._render_semantic_to(raw_path, total_ms)

    def _render_semantic_to(self, raw_path: Path, total_ms: int) -> Path:
        cfg = self.cfg
        elements = self.elements
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(raw_path), fourcc, cfg.fps, (self.out_w, self.out_h))
        if not writer.isOpened():
            raise RuntimeError("无法打开视频写入器")

        weight_sum = cfg.ink_weight + cfg.color_weight
        cur_ms = 0.0
        ms_per_frame = 1000.0 / cfg.fps

        def fill_static(until_ms: float) -> None:
            nonlocal cur_ms
            n = int(round((until_ms - cur_ms) / ms_per_frame))
            if n <= 0:
                return
            snap = self.drawn.astype(np.uint8)
            for _ in range(n):
                writer.write(snap)
            cur_ms += n * ms_per_frame

        try:
            for idx, element in enumerate(elements):
                reveal = element["reveal"]
                start_ms = reveal["startMs"]
                dur_ms = reveal["durationMs"]
                fill_static(start_ms)

                allowed = self.allowed_masks[idx]
                ink_frames = max(1, round(dur_ms * cfg.ink_weight / weight_sum * cfg.fps / 1000))
                color_frames = max(1, round(dur_ms * cfg.color_weight / weight_sum * cfg.fps / 1000))

                if cfg.ink_path_mode == "skeleton":
                    strokes = self._region_skeleton_strokes(allowed)
                    if strokes:
                        samples, pen_lifts = [], set()
                        for si, stroke in enumerate(strokes):
                            if si > 0:
                                pen_lifts.add(len(samples))
                            samples.extend(stroke)
                        self._lay_ink(writer, ink_frames, samples, pen_lifts, allowed)
                        centers = samples
                    else:
                        path = self._region_grid_path(allowed)
                        samples, pen_lifts, _ = self._grid_plan(path) if path else ([], set(), [])
                        self._lay_ink(writer, ink_frames, samples, pen_lifts, allowed)
                        centers = [self._cell_center(c) for c in path]
                else:
                    path = self._region_grid_path(allowed)
                    if path:
                        samples, pen_lifts, sample_cell = self._grid_plan(path)
                        # 块填充：随笔尖推进逐格铺满（保证文字/大块实心）
                        self._lay_ink_grid(writer, ink_frames, samples, pen_lifts, sample_cell, path, allowed)
                        centers = [self._cell_center(c) for c in path]
                    else:
                        self._lay_ink(writer, ink_frames, [], set(), allowed)
                        centers = []

                cur_ms += ink_frames * ms_per_frame

                if cfg.color_fill == "contour-wipe":
                    self._wash_contour(writer, color_frames, allowed)
                else:
                    self._wash_brush(writer, color_frames, centers, allowed)
                cur_ms += color_frames * ms_per_frame

            # 凝视：补到 total_ms，并确保结尾至少停留 0.5s 完整原图
            gaze_until = max(total_ms, cur_ms + 500)
            # 最终帧显示完整原图（凝视）
            self.drawn[...] = self.color_img.astype(np.float32)
            fill_static(gaze_until)
        finally:
            writer.release()
        return raw_path

    def _render_spatial_scan_to(
        self, raw_path: Path, total_ms: int, band_px: int, color_lag_ms: int
    ) -> Path:
        """Render one continuous whole-scene scan on the TTS-derived time span."""
        cfg = self.cfg
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(raw_path), fourcc, cfg.fps, (self.out_w, self.out_h))
        if not writer.isOpened():
            raise RuntimeError("无法打开视频写入器")

        path = self._spatial_scan_path(band_px)
        if not path:
            writer.release()
            raise RuntimeError("整图连续描画没有检测到可绘制前景")

        draw_start_ms = min(element["reveal"]["startMs"] for element in self.elements)
        draw_end_ms = max(
            element["reveal"]["startMs"] + element["reveal"]["durationMs"]
            for element in self.elements
        )
        draw_end_ms = min(float(total_ms), float(draw_end_ms))
        start_frames = max(0, int(round(draw_start_ms * cfg.fps / 1000.0)))
        draw_frames = max(2, int(round((draw_end_ms - draw_start_ms) * cfg.fps / 1000.0)))
        total_frames = max(start_frames + draw_frames, int(round(total_ms * cfg.fps / 1000.0)))
        lag_frames = int(round(color_lag_ms * cfg.fps / 1000.0))
        lag_frames = max(1, min(draw_frames // 3, lag_frames))
        ink_frames = max(1, draw_frames - lag_frames)
        color_frames = max(1, draw_frames - lag_frames)
        ink_targets = _frame_progress_indices(len(path), ink_frames)
        color_targets = _frame_progress_indices(len(path), color_frames)
        ink_done = 0
        color_done = 0
        current_cell = path[0]

        try:
            blank = self.drawn.astype(np.uint8)
            for _ in range(start_frames):
                writer.write(blank)

            for frame_idx in range(draw_frames):
                if frame_idx < ink_frames:
                    ink_target = ink_targets[frame_idx]
                    while ink_done <= ink_target and ink_done < len(path):
                        current_cell = path[ink_done]
                        self._ink_stamp_cell(current_cell, self.foreground_pixels)
                        ink_done += 1

                color_idx = frame_idx - lag_frames
                if color_idx >= 0:
                    color_target = color_targets[min(color_idx, color_frames - 1)]
                    while color_done <= color_target and color_done < len(path):
                        self._color_stamp_cell(path[color_done], self.foreground_pixels)
                        color_done += 1

                px, py = self._cell_center(current_cell)
                writer.write(self._snapshot_with_tip(px, py))

            while ink_done < len(path):
                self._ink_stamp_cell(path[ink_done], self.foreground_pixels)
                ink_done += 1
            while color_done < len(path):
                self._color_stamp_cell(path[color_done], self.foreground_pixels)
                color_done += 1

            final = self.drawn.astype(np.uint8)
            written = start_frames + draw_frames
            for _ in range(max(0, total_frames - written)):
                writer.write(final)
        finally:
            writer.release()
        return raw_path

    # 网格起笔专用：带块填充，笔尖与揭墨同步
    def _lay_ink_grid(self, writer, frames: int, samples, pen_lifts, sample_cell, path, allowed) -> None:
        if frames <= 0:
            return
        n = len(samples)
        if n == 0:
            for _ in range(frames):
                writer.write(self._snapshot_with_tip(self.out_w // 2, self.out_h // 2))
            return
        if self.cfg.motion_profile == "smooth":
            # Advance by newly revealed ink cells instead of interpolated pointer
            # samples. This avoids repeated no-change frames between cell centers.
            cell_for_frame = _frame_progress_indices(len(path), frames)
            cells_done = 0
            for target_cell in cell_for_frame:
                while cells_done <= target_cell and cells_done < len(path):
                    self._ink_stamp_cell(path[cells_done], allowed)
                    cells_done += 1
                sx, sy = self._cell_center(path[target_cell])
                writer.write(self._snapshot_with_tip(sx, sy))
            while cells_done < len(path):
                self._ink_stamp_cell(path[cells_done], allowed)
                cells_done += 1
            return
        idx_for_frame = _frame_progress_indices(n, frames)
        cells_done = 0
        last: int | None = None
        for si in idx_for_frame:
            if last is None:
                self._reveal_ink_segment(samples[si], samples[si], allowed)
            else:
                for k in range(last + 1, si + 1):
                    if k in pen_lifts:
                        continue
                    self._reveal_ink_segment(samples[k - 1], samples[k], allowed)
            target_cell = sample_cell[si]
            while cells_done <= target_cell and cells_done < len(path):
                self._ink_stamp_cell(path[cells_done], allowed)
                cells_done += 1
            sx, sy = samples[si]
            writer.write(self._snapshot_with_tip(sx, sy))
            last = si
        while cells_done < len(path):
            self._ink_stamp_cell(path[cells_done], allowed)
            cells_done += 1


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="SRT 白板动画整合渲染器（mask 编排 + stream 画法）")
    p.add_argument("image", help="线稿图路径")
    p.add_argument("annotation", help="同名 annotation.json 路径")
    p.add_argument("output", help="输出 MP4 路径")
    p.add_argument("hand", nargs="?", default=str(DEFAULT_HAND), help="手部素材 PNG（默认内置）")
    p.add_argument("--total-ms", type=int, default=None, help="总时长；缺省用标注 sceneDurationMs")
    p.add_argument("--bare-tip", action="store_true", help="不叠加笔尖/手部")
    p.add_argument("--hand-height", type=int, default=260,
                   help="手部素材缩放后的高度（内部画布像素，默认 260）")
    p.add_argument("--tip-anchor-x", type=float, default=0.0,
                   help="笔尖在手部透明图中的归一化 X 锚点，默认 0.0")
    p.add_argument("--tip-anchor-y", type=float, default=0.0,
                   help="笔尖在手部透明图中的归一化 Y 锚点，默认 0.0")
    p.add_argument("--tip-smoothing", type=float, default=0.30,
                   help="手部跟随平滑系数 0-1；越小越稳，默认 0.30")
    p.add_argument("--ink-path", default="grid", choices=["grid", "skeleton"],
                   help="笔迹路径: grid 网格(默认); skeleton 骨架追踪")
    p.add_argument("--color-fill", default="contour-wipe", choices=["contour-wipe", "brush"],
                   help="上色: contour-wipe 轮廓扫描(默认); brush 沿轨迹刷")
    p.add_argument("--pause", default="heavy", choices=["heavy", "auto", "light", "off"],
                   help="起笔段停顿节奏（预留，逐区域画法下影响较弱）")
    p.add_argument("--fps", type=int, default=None)
    p.add_argument("--motion-profile", default="sampled", choices=["sampled", "smooth"],
                   help="Motion timing: sampled keeps legacy behavior; smooth reveals new ink every frame")
    p.add_argument("--grid-edge", type=int, default=None)
    p.add_argument("--brush-radius", type=int, default=None)
    p.add_argument("--cap-long-edge", type=int, default=None,
                   help="输出长边像素上限（预览可调小加速，默认 1080）")
    p.add_argument("--mask-report", type=Path, default=None,
                   help="输出遮罩归属与旧版死区诊断 JSON")
    p.add_argument("--mask-preview", type=Path, default=None,
                   help="输出旧版死区红色标记预览图")
    p.add_argument("--reveal-order", default="semantic", choices=["semantic", "spatial-scan"],
                   help="semantic 按主体逐个完成描线和上色（默认）；spatial-scan 从左到右连续描画整图")
    p.add_argument("--spatial-band-px", type=int, default=180,
                   help="spatial-scan 中画面底部相对顶部的水平滞后距离")
    p.add_argument("--spatial-color-lag-ms", type=int, default=320,
                   help="spatial-scan 中颜色落后线稿的毫秒数")
    return p.parse_args(argv)


def _build_cfg(args) -> sr.Config:
    kw: dict = {}
    if args.fps is not None:
        kw["fps"] = args.fps
    kw["motion_profile"] = args.motion_profile
    if args.grid_edge is not None:
        kw["grid_edge"] = args.grid_edge
    if args.brush_radius is not None:
        kw["brush_radius"] = args.brush_radius
    if args.cap_long_edge is not None:
        kw["cap_long_edge"] = args.cap_long_edge
    kw["target_hand_height"] = max(32, args.hand_height)
    kw["tip_anchor_x"] = float(np.clip(args.tip_anchor_x, 0.0, 1.0))
    kw["tip_anchor_y"] = float(np.clip(args.tip_anchor_y, 0.0, 1.0))
    kw["ink_path_mode"] = args.ink_path
    kw["color_fill"] = args.color_fill
    kw["pause_mode"] = args.pause
    return sr.Config(**kw)


def main(argv=None) -> int:
    args = _parse_args(argv)
    cfg = _build_cfg(args)

    print("=" * 56)
    print("SRT 白板动画整合渲染器 (mask 编排 + stream 画法)")
    print("=" * 56)

    image_bgr = sr._imread_any(args.image)
    if image_bgr is None:
        print(f"[err] 无法读取图片: {args.image}")
        return 1
    try:
        annotation = json.loads(Path(args.annotation).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[err] 无法读取标注: {e}")
        return 1
    if not annotation.get("elements"):
        print("[err] 标注中没有 elements")
        return 1

    total_ms = args.total_ms if args.total_ms is not None else annotation.get("sceneDurationMs")
    if not total_ms:
        last = max(e["reveal"]["startMs"] + e["reveal"]["durationMs"] for e in annotation["elements"])
        total_ms = last + 1000

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path = out_path.with_name(out_path.stem + "_raw.mp4")

    hand_png = Path(args.hand) if args.hand else None
    renderer = RegionStreamRenderer(
        image_bgr,
        annotation,
        cfg,
        hand_png,
        args.bare_tip,
        tip_smoothing=args.tip_smoothing,
    )
    print(f"  输入: {args.image}")
    print(f"  输出尺寸: {renderer.out_w}x{renderer.out_h}, 帧率: {cfg.fps}")
    print(f"  区域数: {len(annotation['elements'])}, 总时长: {total_ms}ms, "
          f"笔迹: {cfg.ink_path_mode}, 上色: {cfg.color_fill}, 顺序: {args.reveal_order}")
    if args.bare_tip:
        print("  手部动画: 关闭")
    else:
        print(f"  手部动画: 开启, 高度: {cfg.target_hand_height}px, "
              f"平滑系数: {float(np.clip(args.tip_smoothing, 0.01, 1.0)):.2f}")
    renderer.write_mask_diagnostics(args.mask_report, args.mask_preview)
    print(f"  遮罩策略: {renderer.mask_report['policy']}, "
          f"旧版死区像素: {renderer.mask_report['legacyDeadZonePixels']}, "
          f"新版覆盖缺口: {renderer.mask_report['coverageGapPixels']}")
    if renderer.mask_report["coverageGapPixels"] != 0:
        raise RuntimeError("遮罩唯一归属失败：仍存在无人负责的像素")

    renderer.render_to(
        raw_path,
        total_ms,
        reveal_order=args.reveal_order,
        spatial_band_px=args.spatial_band_px,
        spatial_color_lag_ms=args.spatial_color_lag_ms,
    )
    final = sr.transcode_h264(raw_path, out_path)

    size_mb = final.stat().st_size / (1024 * 1024)
    print(f"\n最终视频: {final}  ({size_mb:.2f} MB)")
    print("=" * 56)
    print(f"OUTPUT={final}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
