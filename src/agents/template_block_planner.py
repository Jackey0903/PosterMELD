"""
Soft template-prior planner for cluster_* poster templates.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

from jinja2 import Template

from src.state.poster_state import PosterState
from src.tools.layout_api import LayoutTemplates
from src.template_extraction.block_template_registry import is_block_template_id
from utils.langgraph_utils import LangGraphAgent, extract_json, load_prompt
from utils.src.logging_utils import log_agent_error, log_agent_info, log_agent_success, log_agent_warning


class TemplatePriorPlanner:
    def __init__(self):
        self.name = "template_prior_planner"
        self.prompt = load_prompt("config/prompts/template_block_planner.txt")

    def __call__(self, state: PosterState) -> PosterState:
        template_name = state.get("resolved_layout_template") or state.get("layout_template")
        if not is_block_template_id(template_name):
            return state

        log_agent_info(self.name, f"building soft template prior for {template_name}")

        try:
            template_layout = self._resolve_template_layout(state, template_name)
            story_board = deepcopy(state.get("story_board") or {})
            sections = (story_board.get("spatial_content_plan") or {}).get("sections") or []
            if not sections:
                raise ValueError("missing story_board sections for template prior planning")

            normalized_sections = self._normalize_sections(sections)
            layout_intent = self._build_layout_intent(normalized_sections, template_layout, state)
            rewritten_story_board = self._rewrite_story_board(story_board, layout_intent, template_layout)

            state["layout_intent"] = layout_intent
            state["template_prior_source_story_board"] = deepcopy(story_board)
            state["template_block_plan"] = {
                "template_id": template_name,
                "active_region_ids": [item["region_id"] for item in layout_intent["region_plan"]],
                "hero_section": layout_intent["hero_section"],
                "blocks": [
                    {
                        "block_id": section["section_id"],
                        "slot_id": section["region_id"],
                        "content_role": section["content_role"],
                        "target_title": section["section_title"],
                        "target_bullets": section["text_content"],
                    }
                    for section in layout_intent["active_sections"]
                ],
            }
            state["story_board"] = rewritten_story_board
            state["layout_template_metadata"] = template_layout
            state["template_layout_mode"] = "template_prior"
            state["resolved_layout_template"] = template_name
            state["render_stage"] = "draft"
            state["final_poster_accepted"] = False
            state["current_agent"] = self.name
            self._save_outputs(state)

            log_agent_success(
                self.name,
                f"planned {len(layout_intent['active_sections'])} active sections across {len(layout_intent['region_plan'])} regions",
            )
        except Exception as e:
            log_agent_error(self.name, f"failed: {e}")
            state["errors"].append(f"{self.name}: {e}")

        return state

    def _resolve_template_layout(self, state: PosterState, template_name: str) -> Dict[str, Any]:
        from src.config.poster_config import load_config

        config = load_config()
        return LayoutTemplates(
            state["poster_width"],
            state["poster_height"],
            margin=config["layout"]["poster_margin"],
            col_gap=config["layout"]["column_spacing"],
        ).get_template(template_name)

    def _normalize_sections(self, sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        normalized = []
        for index, section in enumerate(sections):
            title = str(section.get("section_title") or f"Section {index + 1}").strip()
            normalized.append({
                "section_id": section.get("section_id", f"section_{index + 1}"),
                "section_title": title,
                "text_content": [str(item).strip() for item in section.get("text_content") or [] if str(item).strip()],
                "visual_assets": list(section.get("visual_assets") or []),
                "column_assignment": section.get("column_assignment", "middle"),
                "vertical_priority": section.get("vertical_priority", "middle"),
                "content_role": self._infer_role(title),
                "source_sections": list(section.get("source_sections") or [section.get("section_id", f"section_{index + 1}")]),
            })
        normalized.sort(
            key=lambda item: (
                self._role_priority(item["content_role"]),
                self._priority_rank(item.get("vertical_priority")),
                self._lane_rank(item.get("column_assignment")),
            )
        )
        return normalized

    def _build_layout_intent(
        self,
        sections: List[Dict[str, Any]],
        template_layout: Dict[str, Any],
        state: PosterState,
    ) -> Dict[str, Any]:
        regions = list(template_layout.get("regions") or [])
        if not regions:
            raise ValueError("template prior has no regions")

        active_regions = self._select_active_regions(regions, sections, template_layout, state)
        hero_section = self._choose_hero_section(sections, state)
        hero_region_id = template_layout.get("hero_region_id") or active_regions[0]["region_id"]

        ordered_sections = self._select_active_sections(sections, active_regions, hero_section, state)
        assigned_sections = self._assign_sections_to_regions(ordered_sections, active_regions, hero_section, hero_region_id)
        refined_sections = self._refine_with_llm(assigned_sections, template_layout, state) or assigned_sections

        active_section_ids = [section["section_id"] for section in refined_sections]
        drop_candidates = [
            section["section_id"]
            for section in sections
            if section["section_id"] not in active_section_ids
        ]
        compressible_sections = [
            section["section_id"]
            for section in refined_sections
            if section.get("region_meta", {}).get("text_density_limit") != "high"
        ]

        return {
            "template_id": template_layout.get("template_name"),
            "hero_section": hero_section["section_id"],
            "hero_region_id": hero_region_id,
            "visual_priority": [section["section_id"] for section in refined_sections if section.get("visual_assets")],
            "active_sections": refined_sections,
            "supporting_sections": [section["section_id"] for section in refined_sections if section["section_id"] != hero_section["section_id"]],
            "suggested_region_assignment": {
                section["section_id"]: section["region_id"]
                for section in refined_sections
            },
            "drop_candidates": drop_candidates,
            "compressible_sections": compressible_sections,
            "region_plan": [
                {
                    "region_id": region["region_id"],
                    "region_rank": region["region_rank"],
                    "region_tier": region["region_tier"],
                    "text_density_limit": region["text_density_limit"],
                    "is_hero_region": region.get("is_hero_region", False),
                }
                for region in active_regions
            ],
        }

    def _select_active_regions(
        self,
        regions: List[Dict[str, Any]],
        sections: List[Dict[str, Any]],
        template_layout: Dict[str, Any],
        state: PosterState,
    ) -> List[Dict[str, Any]]:
        density = template_layout.get("template_density_profile") or "balanced"
        section_count = len(sections)
        large_visual = any(self._visual_is_large(section, state) for section in sections)
        if template_layout.get("orientation") == "portrait":
            max_regions = min(len(regions), max(3, min(section_count, len(regions))))
        elif density == "hero_wide" and large_visual:
            max_regions = 3
        elif density in {"hero_wide", "dual_primary"}:
            max_regions = 4
        else:
            max_regions = min(4, len(regions))
        max_regions = min(max_regions, len(regions), max(3, min(section_count, len(regions))))
        ranked = sorted(
            regions,
            key=lambda region: (
                0 if region.get("is_hero_region") else 1,
                region.get("region_rank", 999),
                -float(region.get("area_ratio", 0.0)),
            ),
        )
        return sorted(ranked[:max_regions], key=lambda region: (float(region.get("y", 0.0)), float(region.get("x", 0.0))))

    def _choose_hero_section(self, sections: List[Dict[str, Any]], state: PosterState) -> Dict[str, Any]:
        key_visual = ((state.get("classified_visuals") or {}).get("key_visual"))
        if key_visual:
            for section in sections:
                if any(visual.get("visual_id") == key_visual for visual in section.get("visual_assets", [])):
                    return section
        scored = []
        for section in sections:
            role_bonus = {
                "method": 4.0,
                "results": 3.2,
                "overview": 2.0,
                "setup": 1.6,
                "takeaway": 1.0,
            }.get(section["content_role"], 1.0)
            has_visual = 1.5 if section.get("visual_assets") else 0.0
            key_bonus = 2.0 if key_visual and any(v.get("visual_id") == key_visual for v in section.get("visual_assets", [])) else 0.0
            text_bonus = min(len(section.get("text_content") or []), 4) * 0.15
            scored.append((role_bonus + has_visual + key_bonus + text_bonus, section))
        return max(scored, key=lambda item: item[0])[1]

    def _select_active_sections(
        self,
        sections: List[Dict[str, Any]],
        active_regions: List[Dict[str, Any]],
        hero_section: Dict[str, Any],
        state: PosterState,
    ) -> List[Dict[str, Any]]:
        max_sections = len(active_regions)
        if (state.get("layout_template_metadata") or {}).get("template_density_profile") == "hero_wide":
            max_sections = min(max_sections, 3 if self._visual_is_large(hero_section, state) else 4)

        picked: List[Dict[str, Any]] = [deepcopy(hero_section)]

        for desired_role in ["overview", "results", "takeaway", "setup"]:
            if len(picked) >= max_sections:
                break
            candidate = next(
                (
                    deepcopy(section)
                    for section in sections
                    if section["section_id"] not in {item["section_id"] for item in picked}
                    and section["content_role"] == desired_role
                ),
                None,
            )
            if candidate is not None:
                picked.append(candidate)

        for section in sections:
            if len(picked) >= max_sections:
                break
            if section["section_id"] in {item["section_id"] for item in picked}:
                continue
            picked.append(deepcopy(section))

        return picked[:max_sections]

    def _assign_sections_to_regions(
        self,
        sections: List[Dict[str, Any]],
        regions: List[Dict[str, Any]],
        hero_section: Dict[str, Any],
        hero_region_id: str,
    ) -> List[Dict[str, Any]]:
        region_map = {region["region_id"]: deepcopy(region) for region in regions}
        assigned: List[Dict[str, Any]] = []

        remaining_regions = [region for region in regions if region["region_id"] != hero_region_id]
        hero_region = deepcopy(region_map[hero_region_id])

        for section in sections:
            item = deepcopy(section)
            if section["section_id"] == hero_section["section_id"]:
                region = hero_region
            else:
                region = self._best_region_for_section(item, remaining_regions) or remaining_regions[0]
                remaining_regions = [candidate for candidate in remaining_regions if candidate["region_id"] != region["region_id"]]
            item["region_id"] = region["region_id"]
            item["column_assignment"] = region["region_id"]
            item["semantic_lane"] = region.get("semantic_lane", item.get("column_assignment", "middle"))
            item["slot_id"] = region["region_id"]
            item["region_meta"] = region
            item["visual_assets"] = self._limit_visuals_for_region(item, region)
            item["text_content"] = self._compress_bullets_for_region(item["text_content"], region, bool(item["visual_assets"]))
            assigned.append(item)
        return assigned

    def _best_region_for_section(self, section: Dict[str, Any], regions: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not regions:
            return None
        role = section["content_role"]
        if role == "overview":
            ranked = sorted(regions, key=lambda region: (float(region.get("y", 0.0)), -float(region.get("w", 0.0))))
            return deepcopy(ranked[0])
        if role == "results":
            ranked = sorted(
                regions,
                key=lambda region: (
                    0 if region.get("region_tier") == "primary" else 1,
                    -float(region.get("w", 0.0)),
                    -float(region.get("h", 0.0)),
                ),
            )
            return deepcopy(ranked[0])
        if role == "takeaway":
            ranked = sorted(regions, key=lambda region: (0 if region.get("text_density_limit") == "low" else 1, float(region.get("y", 0.0))))
            return deepcopy(ranked[0])
        ranked = sorted(
            regions,
            key=lambda region: (
                0 if region.get("can_host_visual") else 1,
                region.get("region_rank", 999),
            ),
        )
        return deepcopy(ranked[0])

    def _limit_visuals_for_region(self, section: Dict[str, Any], region: Dict[str, Any]) -> List[Dict[str, Any]]:
        visuals = list(section.get("visual_assets") or [])
        if not visuals or not region.get("can_host_visual", False):
            return []
        if region.get("text_density_limit") == "low":
            return visuals[:0]
        if section["content_role"] == "results":
            preferred = [visual for visual in visuals if str(visual.get("visual_id", "")).startswith("figure_")]
            if preferred:
                visuals = preferred + [visual for visual in visuals if visual not in preferred]
        return visuals[:1]

    def _compress_bullets_for_region(self, bullets: List[str], region: Dict[str, Any], has_visual: bool) -> List[str]:
        density = region.get("text_density_limit", "medium")
        max_bullets = {"high": 4, "medium": 3, "low": 2}.get(density, 3)
        if has_visual:
            max_bullets = max(1, max_bullets - 1)
        char_limit = {"high": 150, "medium": 115, "low": 90}.get(density, 115)
        trimmed = []
        for bullet in bullets:
            text = str(bullet).strip()
            if not text:
                continue
            if len(text) > char_limit:
                text = self._truncate_on_word_boundary(text, char_limit)
            trimmed.append(text)
            if len(trimmed) >= max_bullets:
                break
        return trimmed or ["Key takeaway."]

    def _truncate_on_word_boundary(self, text: str, char_limit: int) -> str:
        if len(text) <= char_limit:
            return text
        cutoff = max(1, char_limit - 1)
        candidate = text[:cutoff].rstrip(" ,;:")
        boundary = candidate.rfind(" ")
        if boundary >= int(char_limit * 0.6):
            candidate = candidate[:boundary].rstrip(" ,;:")
        return candidate.rstrip(".") + "."

    def _refine_with_llm(
        self,
        sections: List[Dict[str, Any]],
        template_layout: Dict[str, Any],
        state: PosterState,
    ) -> Optional[List[Dict[str, Any]]]:
        try:
            agent = LangGraphAgent("expert academic poster editor", state["text_model"], state, self.name)
            prompt = Template(self.prompt).render(
                template_name=template_layout.get("template_name"),
                slot_count=len(sections),
                blocks=json.dumps(
                    [
                        {
                            "block_id": section["section_id"],
                            "slot_id": section["region_id"],
                            "content_role": section["content_role"],
                            "target_title": section["section_title"],
                            "target_bullets": section["text_content"],
                            "region_tier": section["region_meta"]["region_tier"],
                            "text_density_limit": section["region_meta"]["text_density_limit"],
                        }
                        for section in sections
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            response = agent.step(prompt)
            state["tokens"].add_text(response.input_tokens, response.output_tokens)
            payload = extract_json(response.content)
            blocks = payload.get("blocks")
            if not isinstance(blocks, list) or len(blocks) != len(sections):
                return None
            refined = []
            for original, candidate in zip(sections, blocks):
                item = deepcopy(original)
                item["section_title"] = str(candidate.get("target_title") or original["section_title"]).strip()
                item["text_content"] = self._compress_bullets_for_region(
                    candidate.get("target_bullets") or original["text_content"],
                    original["region_meta"],
                    bool(original.get("visual_assets")),
                )
                refined.append(item)
            return refined
        except Exception as exc:
            log_agent_warning(self.name, f"LLM region refinement unavailable: {exc}")
            return None

    def _rewrite_story_board(
        self,
        base_story_board: Dict[str, Any],
        layout_intent: Dict[str, Any],
        template_layout: Dict[str, Any],
    ) -> Dict[str, Any]:
        rewritten = deepcopy(base_story_board)
        sections = []
        for order, section in enumerate(layout_intent["active_sections"]):
            sections.append({
                "section_id": section["section_id"],
                "section_title": section["section_title"],
                "column_assignment": section["region_id"],
                "semantic_lane": section["region_meta"].get("semantic_lane", section.get("column_assignment", "middle")),
                "vertical_priority": section["region_meta"].get("vertical_band", "middle"),
                "text_content": section["text_content"],
                "visual_assets": section.get("visual_assets") or [],
                "content_role": section.get("content_role", "body"),
                "slot_id": section["region_id"],
                "template_prior": True,
                "source_sections": section.get("source_sections") or [],
                "order_index": order,
                "region_tier": section["region_meta"].get("region_tier"),
            })
        rewritten.setdefault("spatial_content_plan", {})
        rewritten["spatial_content_plan"]["sections"] = sections
        rewritten["layout_intent"] = layout_intent
        rewritten["template_layout"] = {
            "template_id": template_layout.get("template_name"),
            "hero_region_id": template_layout.get("hero_region_id"),
            "regions": template_layout.get("regions"),
        }
        return rewritten

    def _infer_role(self, title: str) -> str:
        lowered = str(title or "").lower()
        if any(token in lowered for token in ["result", "experiment", "ablation", "evaluation", "performance"]):
            return "results"
        if any(token in lowered for token in ["method", "framework", "approach", "model", "pipeline", "search", "hierarchical"]):
            return "method"
        if any(token in lowered for token in ["conclusion", "discussion", "future", "limitation", "takeaway"]):
            return "takeaway"
        if any(token in lowered for token in ["setup", "data", "task", "objective"]):
            return "setup"
        return "overview"

    def _role_priority(self, role: str) -> int:
        return {
            "method": 0,
            "overview": 1,
            "results": 2,
            "takeaway": 3,
            "setup": 4,
        }.get(role, 5)

    def _priority_rank(self, priority: Optional[str]) -> int:
        return {"top": 0, "middle": 1, "bottom": 2}.get(str(priority or "middle"), 1)

    def _lane_rank(self, lane: Optional[str]) -> int:
        return {"left": 0, "middle": 1, "right": 2}.get(str(lane or "middle"), 1)

    def _visual_is_large(self, section: Dict[str, Any], state: PosterState) -> bool:
        visual_assets = state.get("visual_assets") or {}
        for visual in section.get("visual_assets") or []:
            asset = visual_assets.get(visual.get("visual_id"))
            if not asset:
                continue
            aspect = float(asset.get("aspect") or 1.0)
            if aspect >= 2.2 or asset.get("asset_type") == "table":
                return True
        return False

    def _save_outputs(self, state: PosterState) -> None:
        output_dir = Path(state["output_dir"]) / "content"
        output_dir.mkdir(parents=True, exist_ok=True)
        with open(output_dir / "layout_intent.json", "w", encoding="utf-8") as f:
            json.dump(state.get("layout_intent", {}), f, indent=2)
        with open(output_dir / "template_block_plan.json", "w", encoding="utf-8") as f:
            json.dump(state.get("template_block_plan", {}), f, indent=2)
        with open(output_dir / "story_board.json", "w", encoding="utf-8") as f:
            json.dump(state.get("story_board", {}), f, indent=2)


TemplateBlockPlanner = TemplatePriorPlanner


def template_block_planner_node(state: PosterState) -> Dict[str, Any]:
    result = TemplatePriorPlanner()(state)
    return {
        **state,
        "story_board": result.get("story_board"),
        "template_block_plan": result.get("template_block_plan"),
        "layout_intent": result.get("layout_intent"),
        "layout_template_metadata": result.get("layout_template_metadata"),
        "template_layout_mode": result.get("template_layout_mode"),
        "resolved_layout_template": result.get("resolved_layout_template"),
        "tokens": result.get("tokens"),
        "current_agent": result.get("current_agent"),
        "errors": result.get("errors", []),
    }
