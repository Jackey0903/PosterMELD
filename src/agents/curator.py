"""
spatial content planning and story board curation
"""

import json
from pathlib import Path
from typing import Dict, Any, List

from src.state.poster_state import PosterState
from utils.langgraph_utils import LangGraphAgent, extract_json, load_prompt
from utils.src.logging_utils import log_agent_info, log_agent_success, log_agent_error, log_agent_warning
from src.config.poster_config import load_config
from src.layout.template_selector import TemplateSelector
from src.tools.layout_api import LayoutTemplates
from src.template_extraction.block_template_registry import is_block_template_id
from jinja2 import Template

class StoryBoardCurator:
    """creates spatial content plan and story board"""
    
    def __init__(self):
        self.name = "spatial_content_planner"
        self.spatial_planning_prompt = load_prompt("config/prompts/spatial_content_planner.txt")
        self.config = load_config()
        self.validation_config = self.config["validation"]
        self.utilization_config = self.config["utilization_thresholds"]

    def __call__(self, state: PosterState) -> PosterState:
        log_agent_info(self.name, "creating spatial content plan")
        
        try:
            structured_sections = state.get("structured_sections")
            narrative_content = state.get("narrative_content")
            classified_visuals = state.get("classified_visuals")

            if not structured_sections:
                log_agent_error(self.name, "missing structured_sections from parser")
                raise ValueError("missing structured_sections from parser")
            if not narrative_content:
                log_agent_error(self.name, "missing narrative_content from parser")
                raise ValueError("missing narrative_content from parser")
            if not classified_visuals:
                log_agent_error(self.name, "missing classified_visuals from parser")
                raise ValueError("missing classified_visuals from parser")
            
            # prepare visual height context for spatial planning
            selection_report = self._resolve_layout_template(
                state,
                structured_sections,
                classified_visuals,
            )
            resolved_template = selection_report["selected_template"]
            visual_context = self._prepare_visual_context_for_curator(state, resolved_template)
            
            story_board, inp, out = self._create_story_board(
                structured_sections, narrative_content, classified_visuals,
                state.get("visual_assets", {}),
                visual_context, state["text_model"], state
            )
            state["tokens"].add_text(inp, out)
            
            # validate height distribution
            validation_result = self._validate_height_distribution(story_board, visual_context)
            if validation_result["warnings"]:
                log_agent_warning(self.name, f"height validation warnings: {validation_result['warnings']}")
            log_agent_info(self.name, f"column utilizations: {validation_result['column_utilizations']}")
            
            state["story_board"] = story_board
            state["resolved_layout_template"] = resolved_template
            state["layout_template_metadata"] = visual_context["template_layout"]
            state["template_selection_report"] = selection_report
            state["current_agent"] = self.name
            
            self._save_story_board(state)
            
            # log story board summary
            sections = story_board.get("spatial_content_plan", {}).get("sections", [])
            total_visuals = sum(len(section.get("visual_assets", [])) for section in sections)
            
            log_agent_success(self.name, f"created story board with {len(sections)} sections")
            log_agent_success(self.name, f"selected {total_visuals} visual assets")

        except Exception as e:
            log_agent_error(self.name, f"failed: {e}")
            state["errors"].append(f"{self.name}: {e}")
            
        return state

    def _create_story_board(self, structured_sections, narrative_content, classified_visuals, visual_assets, visual_context, config, state):
        log_agent_info(self.name, "generating spatial content plan")
        agent = LangGraphAgent("expert spatial poster designer", config, state, "curator")

        images = {
            asset_id.replace("figure_", ""): {
                "caption": asset.get("caption", ""),
                "aspect": asset.get("aspect", 1.0),
            }
            for asset_id, asset in visual_assets.items()
            if asset.get("asset_type") == "figure"
        }
        tables = {
            asset_id.replace("table_", ""): {
                "caption": asset.get("caption", ""),
                "aspect": asset.get("aspect", 1.0),
            }
            for asset_id, asset in visual_assets.items()
            if asset.get("asset_type") == "table"
        }
        
        template_data = {
            "structured_sections": json.dumps(structured_sections, indent=2),
            "narrative_content": json.dumps(narrative_content, indent=2),
            "classified_visuals": json.dumps(classified_visuals, indent=2),
            "available_images": json.dumps({k: {"caption": v.get("caption", ""), "aspect": v.get("aspect", 1.0)} 
                                          for k, v in images.items()}, indent=2),
            "available_tables": json.dumps({k: {"caption": v.get("caption", ""), "aspect": v.get("aspect", 1.0)} 
                                          for k, v in tables.items()}, indent=2),
            "available_height_per_column": visual_context["available_height_per_column"],
            "visual_heights_info": json.dumps(visual_context["visual_assets_heights"], indent=2),
            "template_layout_guidance": self._template_layout_guidance(visual_context),
            "section_count_guidance": self._section_count_guidance(visual_context),
        }
        
        max_attempts = self.validation_config["max_llm_attempts"]
        for attempt in range(max_attempts):
            try:
                prompt = Template(self.spatial_planning_prompt).render(**template_data)
                agent.reset()
                response = agent.step(prompt)
                
                story_board = extract_json(response.content)
                self._remove_unknown_visual_references(story_board, visual_context)
                self._compact_portrait_text_content(story_board, visual_context)
                
                if self._validate_story_board(story_board, classified_visuals, visual_context):
                    log_agent_success(self.name, f"successfully created story board on attempt {attempt + 1}")
                    return story_board, response.input_tokens, response.output_tokens
                else:
                    log_agent_warning(self.name, f"attempt {attempt + 1}: validation failed, retrying")
                    
            except Exception as e:
                log_agent_warning(self.name, f"story board attempt {attempt + 1} failed: {e}")
                if attempt == max_attempts - 1:
                    raise ValueError("failed to create story board after multiple attempts")

        raise ValueError("failed to create story board")

    def _remove_unknown_visual_references(self, story_board: Dict, visual_context: Dict[str, Any]) -> None:
        valid_visual_ids = set((visual_context or {}).get("valid_visual_ids") or [])
        if not valid_visual_ids:
            return
        sections = story_board.get("spatial_content_plan", {}).get("sections", [])
        for section in sections:
            visuals = section.get("visual_assets", [])
            if not isinstance(visuals, list):
                section["visual_assets"] = []
                continue
            kept = [visual for visual in visuals if visual.get("visual_id") in valid_visual_ids]
            removed = [visual.get("visual_id") for visual in visuals if visual.get("visual_id") not in valid_visual_ids]
            if removed:
                log_agent_warning(
                    self.name,
                    f"removed unknown visual references from {section.get('section_id')}: {removed}",
                )
            section["visual_assets"] = kept

    def _compact_portrait_text_content(self, story_board: Dict, visual_context: Dict[str, Any]) -> None:
        if not self._is_portrait_or_vertical_template(visual_context):
            return
        sections = story_board.get("spatial_content_plan", {}).get("sections", [])
        for section in sections:
            text_items = section.get("text_content", [])
            if not isinstance(text_items, list):
                continue
            compacted = []
            for item in text_items:
                text = str(item).strip()
                if not text:
                    continue
                if len(text) > 170:
                    text = text[:167].rstrip(" ,;:") + "."
                compacted.append(text)
                if len(compacted) >= 2:
                    break
            section["text_content"] = compacted

    def _validate_story_board(self, story_board: Dict, classified_visuals: Dict = None, visual_context: Dict = None) -> bool:
        """validate story board structure and constraints"""
        if "spatial_content_plan" not in story_board:
            log_agent_warning(self.name, "validation error: missing 'spatial_content_plan'")
            return False
        
        scp = story_board["spatial_content_plan"]
        
        # check sections
        if "sections" not in scp or not isinstance(scp["sections"], list):
            log_agent_warning(self.name, "validation error: missing or invalid 'sections'")
            return False
        
        sections = scp["sections"]
        min_sections = self.validation_config["min_section_count"]
        max_sections = self._max_section_count_for_template(visual_context)
        if len(sections) < min_sections or len(sections) > max_sections:
            log_agent_warning(self.name, f"validation error: need {min_sections}-{max_sections} sections, got {len(sections)}")
            return False
        
        # validate each section
        for i, section in enumerate(sections):
            required_fields = ["section_id", "section_title", "column_assignment", "vertical_priority", "text_content"]
            for field in required_fields:
                if field not in section:
                    log_agent_warning(self.name, f"validation error: section {i} missing '{field}'")
                    return False
            
            # check column assignment is valid
            if section["column_assignment"] not in ["left", "middle", "right"]:
                log_agent_warning(self.name, f"validation error: section {i} invalid column_assignment")
                return False
                
            # check vertical priority is valid  
            if section["vertical_priority"] not in ["top", "middle", "bottom"]:
                log_agent_warning(self.name, f"validation error: section {i} invalid vertical_priority")
                return False
            
            # check section title length (4 words max)
            title = section.get("section_title", "")
            title_words = len(title.split())
            max_words = self.validation_config["max_title_words"]
            if title_words > max_words:
                log_agent_warning(self.name, f"validation error: section {i} title too long ({title_words} words): '{title}'")
                return False
            
            # check text content is list of bullet points
            min_items = self.validation_config["min_text_content_items"]
            if not isinstance(section["text_content"], list) or len(section["text_content"]) < min_items:
                log_agent_warning(self.name, f"validation error: section {i} invalid text_content")
                return False
            
            # check for ellipsis in text content
            for j, text in enumerate(section["text_content"]):
                if "..." in text:
                    log_agent_warning(self.name, f"validation error: section {i} bullet {j} contains ellipsis")
                    return False

            valid_visual_ids = set((visual_context or {}).get("valid_visual_ids") or [])
            for visual in section.get("visual_assets", []):
                visual_id = visual.get("visual_id")
                if valid_visual_ids and visual_id not in valid_visual_ids:
                    log_agent_warning(self.name, f"validation error: section {i} references unknown visual_id '{visual_id}'")
                    return False
        
        # validate key_visual placement if classified_visuals provided
        if classified_visuals:
            key_visual = classified_visuals.get("key_visual")
            if key_visual:
                key_visual_found = False
                key_visual_in_middle_top = False
                
                for section in sections:
                    visual_assets = section.get("visual_assets", [])
                    for visual in visual_assets:
                        if visual.get("visual_id") == key_visual:
                            key_visual_found = True
                            if (section.get("column_assignment") == "middle" and 
                                section.get("vertical_priority") == "top"):
                                key_visual_in_middle_top = True
                            break
                    if key_visual_found:
                        break
                
                if not key_visual_found:
                    log_agent_warning(self.name, f"validation error: key_visual '{key_visual}' not found in any section")
                    return False
                    
                if not key_visual_in_middle_top:
                    log_agent_warning(self.name, f"validation error: key_visual '{key_visual}' not placed in middle column, top priority")
                    return False

        if self._is_portrait_or_vertical_template(visual_context or {}):
            selected_visual_ids = [
                visual.get("visual_id")
                for section in sections
                for visual in section.get("visual_assets", [])
                if visual.get("visual_id")
            ]
            if len(selected_visual_ids) > 1:
                log_agent_warning(
                    self.name,
                    f"validation error: portrait template allows only 1 total visual, got {selected_visual_ids}",
                )
                return False
        
        # validate height exclusion compliance if visual_context provided
        if visual_context:
            visual_heights = visual_context.get("visual_assets_heights", {})
            max_visual_height_percentage = self._max_visual_height_percentage(visual_context)
            oversized_visuals = []
            
            # check all visual assets in the story board
            for section in sections:
                visual_assets = section.get("visual_assets", [])
                for visual in visual_assets:
                    visual_id = visual.get("visual_id")
                    if visual_id in visual_heights:
                        height_info = visual_heights[visual_id]
                        # extract percentage value from string like "91%"
                        height_str = height_info.get("height_percentage", "0%")
                        height_percentage = float(height_str.rstrip('%'))
                        
                        if height_percentage > max_visual_height_percentage:
                            oversized_visuals.append(f"{visual_id} ({height_str})")
            
            if oversized_visuals:
                # check if only one oversized visual is selected
                if len(oversized_visuals) == 1:
                    # only one oversized visual selected, allow it as fallback
                    log_agent_info(self.name, f"fallback applied: allowing single oversized visual: {oversized_visuals[0]}")
                else:
                    # multiple oversized visuals selected, only allow the smallest
                    selected_oversized = []
                    for section in sections:
                        visual_assets = section.get("visual_assets", [])
                        for visual in visual_assets:
                            visual_id = visual.get("visual_id")
                            if visual_id in visual_heights:
                                height_info = visual_heights[visual_id]
                                height_str = height_info.get("height_percentage", "0%")
                                height_percentage = float(height_str.rstrip('%'))
                                if height_percentage > max_visual_height_percentage:
                                    selected_oversized.append((visual_id, height_percentage, height_str))
                    
                    smallest = min(selected_oversized, key=lambda x: x[1])
                    invalid_visuals = [f"{vid} ({h_str})" for vid, h, h_str in selected_oversized if vid != smallest[0]]
                    log_agent_warning(self.name, f"validation error: oversized visuals (>{max_visual_height_percentage:.0f}% height) selected: {invalid_visuals} (fallback: only smallest allowed: {smallest[0]} ({smallest[2]}))")
                    return False
        
        return True

    def _section_count_guidance(self, visual_context: Dict[str, Any]) -> str:
        if self._is_portrait_or_vertical_template(visual_context):
            return "exactly 5 compact"
        return "5-8"

    def _template_layout_guidance(self, visual_context: Dict[str, Any]) -> str:
        template_layout = visual_context.get("template_layout") or {}
        if self._is_portrait_or_vertical_template(visual_context):
            return (
                "Portrait extracted template. Use exactly 5 compact sections across the three vertical bands. "
                "Use exactly 1 total visual: the key visual in the middle band. Do not include result tables as "
                "visuals; convert all secondary tables and ablations into short text bullets. Avoid splitting one "
                "idea into multiple small sections."
            )
        if template_layout.get("extracted_template"):
            return (
                "Extracted landscape template. Treat template panels as soft style guidance. "
                "Use normal three-lane flow and avoid overfilling any lane with decorative panels."
            )
        return "Standard three-lane landscape poster."

    def _max_section_count_for_template(self, visual_context: Dict[str, Any] = None) -> int:
        if self._is_portrait_or_vertical_template(visual_context or {}):
            return 5
        return self.validation_config["max_section_count"]

    def _is_portrait_or_vertical_template(self, visual_context: Dict[str, Any]) -> bool:
        template_layout = (visual_context or {}).get("template_layout") or {}
        lanes = template_layout.get("lanes") or []
        is_vertical_stack = bool(lanes) and len({round(lane.get("x", 0), 3) for lane in lanes}) == 1
        return template_layout.get("orientation") == "portrait" or is_vertical_stack

    def _max_visual_height_percentage(self, visual_context: Dict[str, Any]) -> float:
        template_layout = visual_context.get("template_layout") or {}
        template_name = template_layout.get("template_name", "")
        orientation = template_layout.get("orientation")
        lanes = template_layout.get("lanes") or []
        is_vertical_stack = bool(lanes) and len({round(lane.get("x", 0), 3) for lane in lanes}) == 1
        if orientation == "portrait" or is_vertical_stack:
            return 130.0
        if template_name.startswith("extracted_"):
            return 85.0
        return 50.0

    def _resolve_layout_template(
        self,
        state: PosterState,
        structured_sections: Dict[str, Any],
        classified_visuals: Dict[str, Any],
    ) -> Dict[str, Any]:
        selector = TemplateSelector(self.config)
        selection_report = selector.select(
            state=state,
            structured_sections=structured_sections,
            classified_visuals=classified_visuals,
            visual_assets=state.get("visual_assets", {}),
        )

        selected_template = selection_report["selected_template"]
        mode = selection_report.get("selection_mode", "auto")
        log_agent_info(self.name, f"template selection ({mode}): {selected_template}")
        if selection_report.get("candidates"):
            log_agent_info(
                self.name,
                "template candidates: "
                + ", ".join(
                    f"{candidate['template_name']}={candidate['score']:.2f}"
                    for candidate in selection_report["candidates"]
                ),
            )
        return selection_report

    def _prepare_visual_context_for_curator(self, state: PosterState, resolved_template: str) -> Dict[str, Any]:
        """prepare visual assets height information for curator's spatial planning"""
        config = load_config()
        
        # get poster dimensions
        poster_width = state["poster_width"] 
        poster_height = state["poster_height"]
        
        # calculate available height per column (18% of effective height for title region)
        poster_margins = 2 * config["layout"]["poster_margin"]
        effective_height = poster_height - poster_margins  # effective height after margins
        title_region_height = effective_height * config["layout"]["title_height_fraction"]  # 18% fixed region
        curator_template = "three_column_postergen" if is_block_template_id(resolved_template) else resolved_template
        layout = LayoutTemplates(
            poster_width,
            poster_height,
            margin=config["layout"]["poster_margin"],
            col_gap=config["layout"]["column_spacing"],
        ).get_template(curator_template, header_height=title_region_height)
        lanes = layout["lanes"]

        min_lane_height = min(lane["h"] for lane in lanes)
        min_lane_width = min(lane["w"] for lane in lanes)
        visual_width_cap = layout.get("visual_width_cap")

        # account for text padding within each lane
        text_padding = 2 * config["layout"]["text_padding"]["left_right"]
        effective_width = min_lane_width - text_padding
        if visual_width_cap:
            effective_width = min(effective_width, visual_width_cap)
        
        log_agent_info(
            self.name,
            f"visual context: template={resolved_template}, available_height={min_lane_height:.1f}\", effective_width={effective_width:.1f}\"",
        )
        
        visual_heights = {}
        
        for asset_id, asset in (state.get("visual_assets") or {}).items():
            aspect_ratio = asset.get("aspect", 1.0) or 1.0
            lane_estimates = {}
            for lane in lanes:
                lane_effective_width = max(lane["w"] - text_padding, 0.1)
                if visual_width_cap:
                    lane_effective_width = min(lane_effective_width, visual_width_cap)
                lane_visual_height = lane_effective_width / aspect_ratio
                lane_estimates[lane["id"]] = {
                    "height_inches": round(lane_visual_height, 1),
                    "height_percentage": f"{((lane_visual_height / lane['h']) * 100):.0f}%",
                }

            visual_height = effective_width / aspect_ratio
            height_percentage = (visual_height / min_lane_height) * 100
            
            visual_heights[asset_id] = {
                "height_inches": round(visual_height, 1),
                "height_percentage": f"{height_percentage:.0f}%",
                "type": asset.get("asset_type", "figure"), 
                "aspect_ratio": aspect_ratio,
                "lane_estimates": lane_estimates,
            }
            log_agent_info(self.name, f"{asset_id}: {visual_height:.1f}\" ({height_percentage:.0f}% of column)")
        
        return {
            "available_height_per_column": round(min_lane_height, 1),
            "visual_assets_heights": visual_heights,
            "column_width": round(min_lane_width, 1),
            "effective_width": round(effective_width, 1),
            "template_layout": layout,
            "valid_visual_ids": list((state.get("visual_assets") or {}).keys()),
            "requested_layout_template": resolved_template,
        }

    def _validate_height_distribution(self, story_board: Dict, visual_context: Dict) -> Dict[str, Any]:
        """validate spatial plan for height constraints and generate warnings"""
        config = load_config()
        available_height = visual_context["available_height_per_column"]
        visual_heights = visual_context["visual_assets_heights"]
        
        # extract sections from story board
        sections = story_board.get("spatial_content_plan", {}).get("sections", [])
        if not sections:
            return {"warnings": ["No sections found in story board"], "column_utilizations": {}}
        
        # organize sections by column
        columns = {"left": [], "middle": [], "right": []}
        for section in sections:
            column = section.get("column_assignment", "left")
            if column in columns:
                columns[column].append(section)
        
        # calculate estimated height for each section and column
        column_utilizations = {}
        warnings = []
        
        for column_name, column_sections in columns.items():
            total_height = 0
            total_visual_height = 0
            total_visuals = 0
            section_details = []
            
            for section in column_sections:
                section_height = self._estimate_section_height(section, visual_heights, config)
                total_height += section_height
                
                # calculate visual contribution for this section
                section_visual_height = 0
                visual_assets = section.get("visual_assets", [])
                for visual_asset in visual_assets:
                    visual_id = visual_asset.get("visual_id", "")
                    if visual_id in visual_heights:
                        section_visual_height += visual_heights[visual_id]["height_inches"]
                        total_visuals += 1
                
                total_visual_height += section_visual_height
                section_details.append({
                    "section_id": section.get("section_id", "unknown"),
                    "estimated_height": section_height,
                    "visual_count": len(visual_assets),
                    "visual_height": round(section_visual_height, 1)
                })
            
            utilization = total_height / available_height if available_height > 0 else 0
            visual_density = total_visual_height / available_height if available_height > 0 else 0
            
            column_utilizations[column_name] = {
                "total_height": round(total_height, 1),
                "utilization_percent": f"{utilization*100:.0f}%",
                "visual_density_percent": f"{visual_density*100:.0f}%",
                "section_count": len(column_sections),
                "total_visuals": total_visuals,
                "sections": section_details,
                "status": "OK" if utilization <= self.utilization_config["overflow_critical"] else "OVERFLOW"
            }
            
            if utilization > self.utilization_config["overflow_critical"]:
                warnings.append(f"{column_name} column serious overflow: {utilization*100:.0f}% (visual density: {visual_density*100:.0f}%)")
            elif utilization > self.utilization_config["overflow_warning"]:
                warnings.append(f"{column_name} column minor overflow: {utilization*100:.0f}% (visual density: {visual_density*100:.0f}%)")
            elif utilization < self.utilization_config["underutilized"]:
                warnings.append(f"{column_name} column underutilized: {utilization*100:.0f}% (visual density: {visual_density*100:.0f}%)")
            
            if total_visuals == 0:
                warnings.append(f"{column_name} column has no visuals - add visual assets")
        
        return {
            "column_utilizations": column_utilizations,
            "warnings": warnings,
            "overall_status": "PASS" if not warnings else "NEEDS_OPTIMIZATION"
        }

    def _estimate_section_height(self, section: Dict, visual_heights: Dict, config: Dict) -> float:
        """estimate total height for a section including visuals and text"""
        total_height = 0
        
        # section title height (from config)
        section_title_height = config["section_estimation"]["base_title_height"]
        total_height += section_title_height
        
        # visual assets height
        visual_assets = section.get("visual_assets", [])
        for visual_asset in visual_assets:
            visual_id = visual_asset.get("visual_id", "")
            if visual_id in visual_heights:
                visual_height = visual_heights[visual_id]["height_inches"]
                visual_spacing = config["layout"]["visual_spacing"]["below_visual"]
                total_height += visual_height + visual_spacing
        
        # text content height (rough estimation)
        text_content = section.get("text_content", [])
        text_lines = len(text_content)
        bullet_height = config["section_estimation"]["bullet_point_height"]
        text_height = text_lines * bullet_height
        total_height += text_height
        
        # spacing between title and content
        title_spacing = config["layout"]["title_to_content_spacing"]
        total_height += title_spacing
        
        # section bottom spacing
        section_spacing = config["layout"]["section_spacing"]
        total_height += section_spacing
        
        return total_height

    def _save_story_board(self, state: PosterState):
        """save story board to json file"""
        output_dir = Path(state["output_dir"]) / "content"
        output_dir.mkdir(parents=True, exist_ok=True)
        with open(output_dir / "story_board.json", "w", encoding='utf-8') as f:
            json.dump(state.get("story_board", {}), f, indent=2)
        if state.get("template_selection_report") is not None:
            with open(output_dir / "template_selection_report.json", "w", encoding="utf-8") as f:
                json.dump(state.get("template_selection_report", {}), f, indent=2)


def curator_node(state) -> Dict[str, Any]:
    result = StoryBoardCurator()(state)
    return {
        **state,
        "story_board": result["story_board"],
        "resolved_layout_template": result.get("resolved_layout_template"),
        "layout_template_metadata": result.get("layout_template_metadata"),
        "template_selection_report": result.get("template_selection_report"),
        "tokens": result["tokens"],
        "current_agent": result["current_agent"],
        "errors": result["errors"]
    }
