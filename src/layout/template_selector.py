"""
Deterministic template selector for Paper2Poster.

`auto` mode only considers the three user-facing templates:
- three_column_postergen
- two_plus_one_mixed
- one_plus_two_mixed

Selection is based on estimated text/visual burden per semantic lane and a
strong prior preference for the balanced three-column layout.
"""

from __future__ import annotations

from math import ceil
from statistics import pstdev
from typing import Any, Dict, List

from src.tools.layout_api import LayoutTemplates


AUTO_TEMPLATE_CANDIDATES = [
    "three_column_postergen",
    "two_plus_one_mixed",
    "one_plus_two_mixed",
]


class TemplateSelector:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.layout_config = config["layout"]
        self.typography_config = config["typography"]
        self.section_estimation = config["section_estimation"]

    def select(
        self,
        state: Dict[str, Any],
        structured_sections: Dict[str, Any],
        classified_visuals: Dict[str, Any],
        visual_assets: Dict[str, Any],
    ) -> Dict[str, Any]:
        requested_template = state.get("layout_template", "auto")
        if requested_template and requested_template != "auto":
            return {
                "selected_template": requested_template,
                "selection_mode": "manual",
                "candidates": [],
            }

        lane_buckets = self._build_lane_buckets(structured_sections, classified_visuals, visual_assets)
        preferred_template = self._prefer_template_from_lane_pressure(lane_buckets, visual_assets)
        candidate_scores = []
        for template_name in AUTO_TEMPLATE_CANDIDATES:
            template_layout = self._build_template_layout(state, template_name)
            candidate_scores.append(
                self._score_template(
                    template_name,
                    template_layout,
                    lane_buckets,
                    state,
                    preferred_template=preferred_template,
                )
            )

        candidate_scores.sort(key=lambda item: item["score"], reverse=True)
        selected = candidate_scores[0]
        return {
            "selected_template": selected["template_name"],
            "selection_mode": "auto",
            "candidates": candidate_scores,
            "lane_buckets": lane_buckets,
            "preferred_template": preferred_template,
        }

    def _build_template_layout(self, state: Dict[str, Any], template_name: str) -> Dict[str, Any]:
        poster_width = state["poster_width"]
        poster_height = state["poster_height"]
        poster_margin = self.layout_config["poster_margin"]
        column_spacing = self.layout_config["column_spacing"]
        title_height_fraction = self.layout_config["title_height_fraction"]
        effective_height = poster_height - 2 * poster_margin
        title_region_height = effective_height * title_height_fraction
        return LayoutTemplates(
            poster_width,
            poster_height,
            margin=poster_margin,
            col_gap=column_spacing,
        ).get_template(template_name, header_height=title_region_height)

    def _build_lane_buckets(
        self,
        structured_sections: Dict[str, Any],
        classified_visuals: Dict[str, Any],
        visual_assets: Dict[str, Any],
    ) -> Dict[str, Dict[str, Any]]:
        paper_sections = structured_sections.get("paper_sections", [])
        buckets = {
            "left": {"sections": [], "text_blocks": [], "section_count": 0, "visual_ids": []},
            "middle": {"sections": [], "text_blocks": [], "section_count": 0, "visual_ids": []},
            "right": {"sections": [], "text_blocks": [], "section_count": 0, "visual_ids": []},
        }

        for section in paper_sections:
            lane_id = self._section_to_lane(section)
            buckets[lane_id]["sections"].append(section.get("section_name", ""))
            buckets[lane_id]["section_count"] += 1
            key_points = section.get("key_points") or []
            text_block = "\n".join(key_points[:4]) if key_points else section.get("content", "")[:600]
            buckets[lane_id]["text_blocks"].append(text_block)

            for fig_id in section.get("contains_figures", []):
                if fig_id in visual_assets and fig_id not in buckets[lane_id]["visual_ids"]:
                    buckets[lane_id]["visual_ids"].append(fig_id)
            for table_id in section.get("contains_tables", []):
                if table_id in visual_assets and table_id not in buckets[lane_id]["visual_ids"]:
                    buckets[lane_id]["visual_ids"].append(table_id)

        self._inject_classified_visuals(buckets, classified_visuals, visual_assets)
        return buckets

    def _section_to_lane(self, section: Dict[str, Any]) -> str:
        section_type = (section.get("section_type") or "").lower()
        section_name = (section.get("section_name") or "").lower()

        if section_type in {"evaluation", "results"} or any(
            token in section_name for token in ["result", "experiment", "evaluation", "ablation", "comparison"]
        ):
            return "right"
        if section_type == "method" or any(
            token in section_name for token in ["method", "framework", "approach", "algorithm", "policy"]
        ):
            return "middle"
        return "left"

    def _inject_classified_visuals(
        self,
        buckets: Dict[str, Dict[str, Any]],
        classified_visuals: Dict[str, Any],
        visual_assets: Dict[str, Any],
    ) -> None:
        lane_mapping = {
            "left": classified_visuals.get("problem_illustration", []) + classified_visuals.get("supporting", []),
            "middle": ([classified_visuals.get("key_visual")] if classified_visuals.get("key_visual") else []) + classified_visuals.get("method_workflow", []),
            "right": classified_visuals.get("main_results", []) + classified_visuals.get("comparative_results", []),
        }

        for lane_id, asset_ids in lane_mapping.items():
            for asset_id in asset_ids:
                if asset_id in visual_assets and asset_id not in buckets[lane_id]["visual_ids"]:
                    buckets[lane_id]["visual_ids"].append(asset_id)

    def _score_template(
        self,
        template_name: str,
        template_layout: Dict[str, Any],
        lane_buckets: Dict[str, Dict[str, Any]],
        state: Dict[str, Any],
        preferred_template: str,
    ) -> Dict[str, Any]:
        visual_width_cap = template_layout.get("visual_width_cap")
        text_padding = 2 * self.layout_config["text_padding"]["left_right"]
        lane_scores = {}
        utilizations = []

        for lane in template_layout["lanes"]:
            lane_id = lane["id"]
            lane_bucket = lane_buckets[lane_id]
            lane_effective_width = max(lane["w"] - text_padding, 0.5)
            if visual_width_cap:
                lane_effective_width = min(lane_effective_width, visual_width_cap)

            text_content = "\n".join(block for block in lane_bucket["text_blocks"] if block).strip()
            text_height = 0.0
            if text_content:
                text_height = self._estimate_text_height(text_content, lane_effective_width)

            title_height = lane_bucket["section_count"] * self.section_estimation["base_title_height"]
            visual_height = self._estimate_visual_height(lane_bucket["visual_ids"], lane_effective_width, state)
            spacing_height = max(lane_bucket["section_count"] - 1, 0) * self.layout_config["section_spacing"]

            total_height = text_height + title_height + visual_height + spacing_height
            utilization = total_height / max(lane["h"], 0.1)
            utilizations.append(utilization)

            lane_scores[lane_id] = {
                "estimated_height": round(total_height, 2),
                "available_height": round(lane["h"], 2),
                "utilization": round(utilization, 3),
                "lane_width": round(lane["w"], 2),
            }

        score = self._score_utilization_profile(
            template_name,
            lane_scores,
            lane_buckets,
            preferred_template,
        )
        return {
            "template_name": template_name,
            "score": round(score, 3),
            "lane_scores": lane_scores,
            "utilization_stddev": round(pstdev(utilizations), 3),
        }

    def _estimate_text_height(self, text_content: str, width_inches: float) -> float:
        font_size = self.typography_config["sizes"]["body_text"]
        chars_per_line = max(int(width_inches * 8), 14)
        line_height = (font_size / 72) * 1.05

        rendered_lines = 0
        for raw_line in text_content.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            rendered_lines += max(1, ceil(len(line) / chars_per_line))

        if rendered_lines == 0:
            return 0.0
        return rendered_lines * line_height + max(rendered_lines - 1, 0) * 0.05

    def _estimate_visual_height(self, visual_ids: List[str], lane_width: float, state: Dict[str, Any]) -> float:
        visual_assets = state.get("visual_assets") or {}
        selected_assets = []
        for asset_id in visual_ids:
            asset = visual_assets.get(asset_id)
            if asset:
                selected_assets.append(asset)

        max_visuals = 1 if len(selected_assets) <= 1 else 2
        selected_assets = selected_assets[:max_visuals]

        total = 0.0
        for asset in selected_assets:
            aspect = asset.get("aspect", 1.0) or 1.0
            total += (lane_width / aspect) + self.layout_config["visual_spacing"]["below_visual"]
        return total

    def _score_utilization_profile(
        self,
        template_name: str,
        lane_scores: Dict[str, Dict[str, Any]],
        lane_buckets: Dict[str, Dict[str, Any]],
        preferred_template: str,
    ) -> float:
        target_utilization = 0.82
        score = 100.0
        utilizations = []

        for lane_id, lane_score in lane_scores.items():
            utilization = lane_score["utilization"]
            utilizations.append(utilization)
            score -= abs(utilization - target_utilization) * 35
            if utilization > 1.0:
                score -= (utilization - 1.0) * 120
            if utilization < 0.45:
                score -= (0.45 - utilization) * 20

        score -= pstdev(utilizations) * 30

        prior_bonus = {
            "three_column_postergen": 12.0,
            "two_plus_one_mixed": 4.0,
            "one_plus_two_mixed": 4.0,
        }
        score += prior_bonus.get(template_name, 0.0)

        left_load = lane_scores["left"]["utilization"]
        middle_load = lane_scores["middle"]["utilization"]
        right_load = lane_scores["right"]["utilization"]

        if max(abs(left_load - middle_load), abs(middle_load - right_load), abs(left_load - right_load)) < 0.12:
            score += 6.0 if template_name == "three_column_postergen" else -2.0

        if right_load > left_load + 0.08 and right_load > middle_load + 0.08:
            score += 8.0 if template_name == "two_plus_one_mixed" else -1.5

        if left_load > middle_load + 0.08 and left_load > right_load + 0.08:
            score += 8.0 if template_name == "one_plus_two_mixed" else -1.5

        if lane_buckets["right"]["section_count"] >= lane_buckets["left"]["section_count"] + 1:
            score += 3.0 if template_name == "two_plus_one_mixed" else 0.0

        if lane_buckets["left"]["section_count"] >= lane_buckets["right"]["section_count"] + 1:
            score += 3.0 if template_name == "one_plus_two_mixed" else 0.0

        if preferred_template == template_name:
            score += 28.0
        elif preferred_template != "three_column_postergen" and template_name == "three_column_postergen":
            score -= 6.0
        elif preferred_template == "three_column_postergen" and template_name != "three_column_postergen":
            score -= 4.0

        return score

    def _prefer_template_from_lane_pressure(
        self,
        lane_buckets: Dict[str, Dict[str, Any]],
        visual_assets: Dict[str, Any],
    ) -> str:
        burdens = {
            lane_id: self._estimate_lane_pressure(bucket, visual_assets)
            for lane_id, bucket in lane_buckets.items()
        }

        left_pressure = burdens["left"]
        middle_pressure = burdens["middle"]
        right_pressure = burdens["right"]
        spread = max(burdens.values()) - min(burdens.values())

        if spread < 1.25:
            return "three_column_postergen"

        if right_pressure > left_pressure + 1.5 and right_pressure > middle_pressure + 1.0:
            return "two_plus_one_mixed"

        if left_pressure > right_pressure + 1.5 and left_pressure > middle_pressure + 1.0:
            return "one_plus_two_mixed"

        return "three_column_postergen"

    def _estimate_lane_pressure(self, bucket: Dict[str, Any], visual_assets: Dict[str, Any]) -> float:
        text_chars = sum(len(block or "") for block in bucket.get("text_blocks", []))
        visual_pressure = 0.0
        for asset_id in bucket.get("visual_ids", []):
            asset = visual_assets.get(asset_id, {})
            aspect = asset.get("aspect", 1.0) or 1.0
            # Taller visuals impose more vertical pressure than wide visuals.
            visual_pressure += 1.0 / max(aspect, 0.4)

        return (
            bucket.get("section_count", 0) * 1.2
            + text_chars / 120.0
            + visual_pressure * 4.0
        )
