from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_PORTRAIT_WIDTH = 36.0
DEFAULT_LANDSCAPE_WIDTH = 54.0


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_template_dir() -> Path:
    return repo_root() / "template" / "json"


def default_template_picture_dir() -> Path:
    return repo_root() / "template" / "picture"


def iter_block_template_files(template_dir: Optional[Path] = None) -> Iterable[Path]:
    root = template_dir or default_template_dir()
    if not root.exists():
        return []
    return sorted(root.glob("cluster_*_template.json"))


def list_block_template_ids(template_dir: Optional[Path] = None) -> List[str]:
    return [path.stem.replace("_template", "") for path in iter_block_template_files(template_dir)]


def is_block_template_id(template_id: str) -> bool:
    return template_id in set(list_block_template_ids())


def load_block_template_raw(template_id: str, template_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    for path in iter_block_template_files(template_dir):
        candidate_id = path.stem.replace("_template", "")
        if candidate_id != template_id:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        data["_template_path"] = str(path)
        return data
    return None


def get_block_template_info(template_id: str, template_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    raw = load_block_template_raw(template_id, template_dir=template_dir)
    if not raw:
        return None
    aspect_ratio = _template_aspect_ratio(raw, template_id)
    orientation = "portrait" if aspect_ratio < 1.0 else "landscape"
    if orientation == "portrait":
        width = DEFAULT_PORTRAIT_WIDTH
        height = width / max(aspect_ratio, 1e-6)
    else:
        width = DEFAULT_LANDSCAPE_WIDTH
        height = width / max(aspect_ratio, 1e-6)
    return {
        "template_id": template_id,
        "orientation": orientation,
        "aspect_ratio": aspect_ratio,
        "recommended_canvas_size": {
            "width": round(width, 3),
            "height": round(height, 3),
        },
        "slot_count": int(raw.get("num_slots") or len(raw.get("slots") or [])),
        "source_template_path": raw.get("_template_path"),
        "source_image_path": str(_template_image_path(template_id)) if _template_image_path(template_id).exists() else None,
    }


def load_block_template_layout(
    template_id: str,
    page_width: float,
    page_height: float,
    *,
    margin: float = 1.0,
) -> Optional[Dict[str, Any]]:
    raw = load_block_template_raw(template_id)
    if not raw:
        return None
    return build_runtime_template(raw, template_id, page_width, page_height, margin=margin)


def build_runtime_template(
    raw: Dict[str, Any],
    template_id: str,
    page_width: float,
    page_height: float,
    *,
    margin: float = 1.0,
) -> Dict[str, Any]:
    slots = raw.get("slots") or []
    source_slots = [_slot_from_raw(slot) for slot in slots]
    if not source_slots:
        raise ValueError(f"template '{template_id}' has no slots")

    template_aspect_ratio = _template_aspect_ratio(raw, template_id)
    template_orientation = "portrait" if template_aspect_ratio < 1.0 else "landscape"
    source_width, source_height = _normalized_coordinate_extent(source_slots)
    norm_slots = [_normalize_slot(slot, source_width, source_height) for slot in source_slots]

    header_slot = _identify_header_slot(norm_slots)
    content_slots = [slot for slot in norm_slots if slot["slot_id"] != header_slot["slot_id"]]
    ordered_content_slots = _order_content_slots(content_slots)
    semantic_slots = _decorate_content_slots(ordered_content_slots)

    inner_w = max(page_width - 2 * margin, 0.1)
    inner_h = max(page_height - 2 * margin, 0.1)

    scaled_header = _scale_box(header_slot, inner_w, inner_h, margin, margin)
    scaled_content_slots = [
        _scale_box(slot, inner_w, inner_h, margin, margin)
        for slot in semantic_slots
    ]

    adjacency_graph = _build_adjacency_graph(semantic_slots)
    slot_prominence = {
        slot["slot_id"]: slot["prominence_score"]
        for slot in semantic_slots
    }
    ordered_by_prominence = sorted(
        semantic_slots,
        key=lambda slot: (-float(slot.get("prominence_score", 0.0)), float(slot.get("y", 0.0)), float(slot.get("x", 0.0))),
    )
    hero_region_id = ordered_by_prominence[0]["slot_id"]
    primary_region_ids = [slot["slot_id"] for slot in ordered_by_prominence[: max(2, min(3, len(ordered_by_prominence)))]]
    secondary_region_ids = [slot["slot_id"] for slot in ordered_by_prominence if slot["slot_id"] not in primary_region_ids]
    density_profile = _template_density_profile(semantic_slots)
    regions = _build_regions(scaled_content_slots, hero_region_id, primary_region_ids)

    return {
        "template_name": template_id,
        "template_id": template_id,
        "layout_mode": "template_prior",
        "header_slot": scaled_header,
        "header_region": scaled_header,
        "header": {
            "x": scaled_header["x"],
            "y": scaled_header["y"],
            "w": scaled_header["w"],
            "h": scaled_header["h"],
        },
        "content_slots": scaled_content_slots,
        "slot_count": len(scaled_content_slots),
        "lanes": scaled_content_slots,
        "columns": scaled_content_slots,
        "slot_order": [slot["slot_id"] for slot in scaled_content_slots],
        "normalized_slots": semantic_slots,
        "adjacency_graph": adjacency_graph,
        "slot_prominence_score": slot_prominence,
        "orientation": template_orientation,
        "template_aspect_ratio": template_aspect_ratio,
        "recommended_canvas_size": get_block_template_info(template_id).get("recommended_canvas_size"),
        "regions": regions,
        "hero_region_id": hero_region_id,
        "primary_regions": [region for region in regions if region["region_id"] in primary_region_ids],
        "secondary_regions": [region for region in regions if region["region_id"] in secondary_region_ids],
        "recommended_visual_anchor": hero_region_id,
        "template_density_profile": density_profile,
        "style_tokens": {
            "background": "#FFFFFF",
            "header_background": "#FFFFFF",
        },
        "panel_style_tokens": {},
        "logo_regions": [],
        "footer": None,
        "visual_width_cap": None,
        "raw_num_posters": raw.get("num_posters"),
        "occupancy_heatmap": raw.get("occupancy_heatmap"),
        "source_template_path": raw.get("_template_path"),
        "template_prior": True,
    }


def _template_image_path(template_id: str) -> Path:
    suffix = template_id.replace("cluster_", "")
    return default_template_picture_dir() / f"{suffix}.png"


def _template_aspect_ratio(raw: Dict[str, Any], template_id: str) -> float:
    try:
        ratio = float(raw.get("aspect_ratio") or 0)
        if ratio > 0:
            return ratio
    except (TypeError, ValueError):
        pass

    image_path = _template_image_path(template_id)
    if image_path.exists():
        try:
            from PIL import Image

            with Image.open(image_path) as image:
                width, height = image.size
            if height > 0:
                return float(width) / float(height)
        except Exception:
            pass
    return 1.0


def _normalized_coordinate_extent(slots: List[Dict[str, Any]]) -> Tuple[float, float]:
    max_x = max(slot["x"] + slot["w"] for slot in slots)
    max_y = max(slot["y"] + slot["h"] for slot in slots)
    # The cluster template JSONs use a canonical 0..1000 coordinate frame,
    # independent from the real portrait image aspect ratio. The aspect ratio
    # must be applied by the PPT canvas size, not by stretching these bboxes.
    source_width = 1000.0 if max_x <= 1005.0 else max_x
    source_height = 1000.0 if max_y <= 1005.0 else max_y
    return source_width, source_height


def _slot_from_raw(slot: Dict[str, Any]) -> Dict[str, Any]:
    bbox = slot.get("bbox") or [0, 0, 1, 1]
    if isinstance(bbox, dict):
        x = float(bbox.get("x", 0.0))
        y = float(bbox.get("y", 0.0))
        w = float(bbox.get("w", bbox.get("width", 1.0)))
        h = float(bbox.get("h", bbox.get("height", 1.0)))
    else:
        x1, y1, x2, y2 = [float(value) for value in bbox[:4]]
        x, y, w, h = x1, y1, x2 - x1, y2 - y1
    return {
        "slot_id": f"slot_{slot.get('slot_id')}",
        "x": x,
        "y": y,
        "w": max(w, 1.0),
        "h": max(h, 1.0),
        "frequency": float(slot.get("frequency", 1.0)),
        "polygon": slot.get("polygon"),
    }


def _normalize_slot(slot: Dict[str, Any], source_width: float, source_height: float) -> Dict[str, Any]:
    return {
        **slot,
        "x": slot["x"] / max(source_width, 1.0),
        "y": slot["y"] / max(source_height, 1.0),
        "w": slot["w"] / max(source_width, 1.0),
        "h": slot["h"] / max(source_height, 1.0),
        "area_ratio": (slot["w"] * slot["h"]) / max(source_width * source_height, 1.0),
    }


def _identify_header_slot(slots: List[Dict[str, Any]]) -> Dict[str, Any]:
    max_width = max(slot["w"] for slot in slots)

    def score(slot: Dict[str, Any]) -> Tuple[float, float, float]:
        width_score = slot["w"] / max(max_width, 1e-6)
        return (-slot["y"], width_score, -slot["h"])

    return max(slots, key=score)


def _order_content_slots(slots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(slots, key=lambda slot: (round(slot["y"], 4), round(slot["x"], 4), -slot["area_ratio"]))


def _decorate_content_slots(slots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    decorated: List[Dict[str, Any]] = []
    max_area = max((slot["area_ratio"] for slot in slots), default=1.0)
    for slot in slots:
        center_x = slot["x"] + slot["w"] / 2
        center_y = slot["y"] + slot["h"] / 2
        topness = 1.0 - min(center_y, 1.0)
        centrality = 1.0 - min(abs(center_x - 0.5) / 0.5, 1.0)
        area_weight = slot["area_ratio"] / max(max_area, 1e-6)
        prominence = round(area_weight * 0.55 + topness * 0.25 + centrality * 0.20, 4)
        semantic_lane = "left" if center_x < 0.34 else "middle" if center_x < 0.67 else "right"
        vertical_band = "top" if center_y < 0.34 else "middle" if center_y < 0.67 else "bottom"
        decorated.append({
            **slot,
            "prominence_score": prominence,
            "semantic_lane": semantic_lane,
            "vertical_band": vertical_band,
        })
    return decorated


def _scale_box(slot: Dict[str, Any], inner_w: float, inner_h: float, offset_x: float, offset_y: float) -> Dict[str, Any]:
    scaled = {
        "id": slot["slot_id"],
        "slot_id": slot["slot_id"],
        "x": offset_x + slot["x"] * inner_w,
        "y": offset_y + slot["y"] * inner_h,
        "w": slot["w"] * inner_w,
        "h": slot["h"] * inner_h,
        "area_ratio": slot.get("area_ratio", 0.0),
        "prominence_score": slot.get("prominence_score", 0.0),
        "semantic_lane": slot.get("semantic_lane"),
        "vertical_band": slot.get("vertical_band"),
        "frequency": slot.get("frequency", 1.0),
        "template_block_slot": True,
    }
    return scaled


def _build_adjacency_graph(slots: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    adjacency: Dict[str, List[Dict[str, Any]]] = {slot["slot_id"]: [] for slot in slots}
    for idx, slot in enumerate(slots):
        for other in slots[idx + 1:]:
            relation = _shared_edge(slot, other)
            if not relation:
                continue
            adjacency[slot["slot_id"]].append({
                "slot_id": other["slot_id"],
                **relation,
            })
            adjacency[other["slot_id"]].append({
                "slot_id": slot["slot_id"],
                **relation,
            })
    return adjacency


def _shared_edge(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tol = 0.03
    ax1, ay1, ax2, ay2 = a["x"], a["y"], a["x"] + a["w"], a["y"] + a["h"]
    bx1, by1, bx2, by2 = b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]

    if abs(ax2 - bx1) <= tol or abs(bx2 - ax1) <= tol:
        overlap = min(ay2, by2) - max(ay1, by1)
        if overlap > tol:
            return {"orientation": "vertical", "shared_span": round(overlap, 4)}
    if abs(ay2 - by1) <= tol or abs(by2 - ay1) <= tol:
        overlap = min(ax2, bx2) - max(ax1, bx1)
        if overlap > tol:
            return {"orientation": "horizontal", "shared_span": round(overlap, 4)}
    return None


def _build_regions(
    scaled_slots: List[Dict[str, Any]],
    hero_region_id: str,
    primary_region_ids: List[str],
) -> List[Dict[str, Any]]:
    regions = []
    for rank, slot in enumerate(
        sorted(
            scaled_slots,
            key=lambda region: (-float(region.get("prominence_score", 0.0)), float(region.get("y", 0.0)), float(region.get("x", 0.0))),
        ),
        start=1,
    ):
        area_ratio = float(slot.get("area_ratio", 0.0) or 0.0)
        can_host_visual = area_ratio >= 0.12 and float(slot.get("w", 0.0) or 0.0) >= 15.0
        regions.append({
            **slot,
            "region_id": slot["slot_id"],
            "region_rank": rank,
            "region_tier": "primary" if slot["slot_id"] in primary_region_ids else "secondary",
            "can_host_visual": can_host_visual,
            "text_density_limit": "high" if area_ratio >= 0.18 else "medium" if area_ratio >= 0.09 else "low",
            "is_hero_region": slot["slot_id"] == hero_region_id,
        })
    return sorted(regions, key=lambda region: (float(region.get("y", 0.0)), float(region.get("x", 0.0))))


def _template_density_profile(slots: List[Dict[str, Any]]) -> str:
    if not slots:
        return "balanced"
    ordered = sorted(slots, key=lambda slot: float(slot.get("area_ratio", 0.0)), reverse=True)
    largest = float(ordered[0].get("area_ratio", 0.0))
    if largest >= 0.20:
        return "hero_wide"
    if len(ordered) >= 2 and float(ordered[0].get("area_ratio", 0.0)) + float(ordered[1].get("area_ratio", 0.0)) >= 0.32:
        return "dual_primary"
    return "balanced"
